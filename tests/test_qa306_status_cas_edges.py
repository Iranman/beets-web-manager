"""QA #306 edge cases around the #218/#224/#300 status transitions: cancel
after the claim, double rollback, Failed with and without an apply record,
Recovery Required, and the folder-cleanup deferral through the real route.
Temp stores, temp directories and fake adapters only."""

import unittest
from unittest import mock

import backend.composite_workflows as cw
from backend import transaction_engine as te
from backend.beets_adapter import BeetsAdapterTimeoutError
from tests._folder_ops_local import LocalFolderOps
from tests.test_staging_mutation_hardening import _RouteEnv
from tests.test_wave0_s1_containment import FakeAdapter, _Env

_REPLACE_META = {"mutation_family": cw.ITEM_FILE_REPLACEMENT_FAMILY, "target_item_id": 1, "source_item_id": 2}


def _adapter():
    ad = mock.Mock()
    ad.get_album.return_value = {"id": 1}
    ad.find_all_items_by_album_id.return_value = []
    ad.rollback_replace_item_file.return_value = {"result": {"restored_target_path": "/m/a.flac"}}
    return ad


class CompositeApplyEdges(_Env):
    def test_refusal_that_wrote_nothing_is_not_rollback_eligible(self):
        op = cw.plan_album_maintenance({"mode": "remove_album", "album_id": 1}, store=self.store)["operation_id"]
        ad = _adapter()
        ad.find_all_items_by_album_id.return_value = [{"id": 9}]
        res = cw.apply_album_maintenance(op, adapter=ad, store=self.store)
        self.assertEqual((res["ok"], res["code"], res["status"]), (False, "album_not_empty", "Failed"))
        self.assertIsNone(self.store.get(op)["metadata"].get("engine_result"))
        ad.remove.assert_not_called()
        rb = cw.rollback_album_maintenance(op, store=self.store)
        self.assertEqual((rb["ok"], rb["code"]), (False, "rollback_not_eligible"))
        self.assertEqual(self.store.get(op)["status"], "Failed")

    def test_failed_after_a_write_attempt_keeps_its_apply_record(self):
        op = cw.plan_album_relocation({"album_id": 1}, store=self.store)["operation_id"]
        ad = _adapter()
        ad.move.side_effect = RuntimeError("refused")
        with self.assertRaises(RuntimeError):
            cw.apply_album_relocation(op, adapter=ad, store=self.store)
        tx = self.store.get(op)
        self.assertEqual(tx["status"], "Failed")
        self.assertTrue(tx["metadata"]["engine_result"]["mutation_started"])
        self.assertEqual(cw.rollback_album_relocation(op, store=self.store)["status"], "Rolled Back")

    def test_recovery_required_is_never_marked_rolled_back(self):
        op = cw.plan_album_genre_repair({"album_id": 1}, store=self.store)["operation_id"]
        ad = _adapter()
        ad.lastgenre.side_effect = BeetsAdapterTimeoutError("timed out")
        with self.assertRaises(BeetsAdapterTimeoutError):
            cw.apply_album_genre_repair(op, adapter=ad, store=self.store)
        rb = cw.rollback_album_genre_repair(op, store=self.store)
        self.assertEqual((rb["ok"], rb["code"]), (False, "rollback_not_eligible"))
        self.assertEqual(self.store.get(op)["status"], "Recovery Required")


class EngineRollbackEdges(_Env):
    def _tx(self, status, engine_result=True):
        meta = dict(_REPLACE_META, **({"engine_result": {"quarantine_id": "q1"}} if engine_result else {}))
        return self.store.create(operation_type="Replace", status=status, metadata=meta)["id"]

    def test_failed_without_apply_record_is_not_applied(self):
        op, ad = self._tx("Failed", engine_result=False), _adapter()
        res = cw.rollback_track_replacement(op, adapter=ad, store=self.store)
        self.assertEqual((res["ok"], res["code"]), (False, "not_applied"))
        ad.rollback_replace_item_file.assert_not_called()

    def test_failed_and_recovery_required_with_apply_record_roll_back(self):
        for status in ("Failed", "Recovery Required"):
            with self.subTest(status=status):
                op, ad = self._tx(status), _adapter()
                self.assertTrue(cw.rollback_track_replacement(op, adapter=ad, store=self.store)["ok"])
                self.assertEqual(self.store.get(op)["status"], "Rolled Back")

    def test_second_rollback_is_idempotent_and_calls_nothing(self):
        op, ad = self._tx("Completed"), _adapter()
        self.assertTrue(cw.rollback_track_replacement(op, adapter=ad, store=self.store)["ok"])
        again = cw.rollback_track_replacement(op, adapter=ad, store=self.store)
        self.assertEqual((again["ok"], again["status"]), (True, "Rolled Back"))
        self.assertEqual(ad.rollback_replace_item_file.call_count, 1)


class RouteEdges(_RouteEnv):
    def test_cancel_after_the_claim_is_refused_and_apply_completes(self):
        op = cw.plan_album_relocation({"album_id": 1}, store=self.store)["operation_id"]
        ad, seen = _adapter(), []
        ad.move.side_effect = lambda **_k: seen.append(self.client.post(f"/api/transactions/{op}/cancel"))
        res = cw.apply_album_relocation(op, adapter=ad, store=self.store)
        self.assertEqual((seen[0].status_code, seen[0].get_json()["code"]), (409, "not_cancellable"))
        self.assertEqual((res["status"], self.store.get(op)["status"]), ("Completed", "Completed"))

    def test_engine_rollback_route_refuses_unapplied_with_409(self):
        ad = _adapter()
        with mock.patch.object(cw, "beets_adapter", ad):
            for status in ("Cancelled", "Running"):
                op = self.store.create(operation_type="Replace", status=status,
                                       metadata=dict(_REPLACE_META, engine_result={"quarantine_id": "q1"}))["id"]
                resp = self.client.post(f"/api/transactions/{op}/rollback")
                self.assertEqual((resp.status_code, resp.get_json()["code"]), (409, "rollback_not_eligible"), status)
                self.assertEqual(self.store.get(op)["status"], status)
        ad.rollback_replace_item_file.assert_not_called()

    def test_engine_rollback_route_reports_a_lost_race_as_409(self):
        """N2: a concurrent rollback finished first; the final CAS loses."""
        op = self.store.create(operation_type="Replace", status="Completed",
                               metadata=dict(_REPLACE_META, engine_result={"quarantine_id": "q1"}))["id"]
        ad = _adapter()

        def concurrent(*_a, **_k):
            self.store.update(op, status="Rolled Back")
            return {"result": {"restored_target_path": "/m/a.flac"}}
        ad.rollback_replace_item_file.side_effect = concurrent
        with mock.patch.object(cw, "beets_adapter", ad):
            resp = self.client.post(f"/api/transactions/{op}/rollback")
        self.assertEqual((resp.status_code, resp.get_json()["code"]), (409, "conflict"))

    def test_folder_rollback_route_defers_then_restores(self):
        p = mock.patch.object(te, "_FOLDER_STEP_RETRY_DELAY", 0)
        p.start()
        self.addCleanup(p.stop)
        empty = self.music / "Empty"
        empty.mkdir()
        plan = te.create_folder_cleanup_plan(self.store, {"action": "remove_empty", "source": str(empty)})
        op = plan["operation_id"]
        self.store.transition(op, "Preview", "Approved")
        held = mock.Mock()
        held.folder_op.side_effect = BeetsAdapterTimeoutError("read timed out")
        self.assertEqual(te.execute_folder_cleanup_apply(self.store, op, adapter=held)["status"], "Failed")

        running = mock.Mock()
        running.folder_op.return_value = {"operation_id": f"{op}:apply:0", "status": "running"}
        with mock.patch.object(cw, "beets_adapter", FakeAdapter()), \
                mock.patch.object(te, "_folder_adapter", lambda a=None: a or running):
            resp = self.client.post(f"/api/transactions/{op}/rollback")
            self.assertEqual((resp.status_code, resp.get_json()["code"]), (409, "rollback_deferred"))
            self.assertEqual(self.store.get(op)["status"], "Failed")
            self.assertTrue(empty.is_dir())

            # Beets finishes the held step; the retry replays it, then restores.
            empty.rmdir()
            done = mock.Mock(wraps=LocalFolderOps(self.music))
            done.folder_op.side_effect = lambda o, k, **p: (
                {"status": "succeeded", "result": {"success": True}} if k.endswith(":apply:0")
                else LocalFolderOps(self.music).folder_op(o, k, **p))
            with mock.patch.object(te, "_folder_adapter", lambda a=None: a or done):
                resp = self.client.post(f"/api/transactions/{op}/rollback")
            self.assertEqual(resp.status_code, 200, resp.get_json())
            self.assertEqual(self.store.get(op)["status"], "Rolled Back")
            self.assertTrue(empty.is_dir())

    def test_rollback_reentered_during_a_rollback_is_refused(self):
        src, dst = self.music / "A" / "Albm", self.music / "A" / "Album"
        src.mkdir(parents=True)
        plan = te.create_folder_cleanup_plan(self.store, {"action": "safe_rename", "source": str(src),
                                                          "target": str(dst)})
        op = plan["operation_id"]
        self.store.transition(op, "Preview", "Approved")
        self.assertTrue(te.execute_folder_cleanup_apply(self.store, op, adapter=LocalFolderOps(self.music))["ok"])
        inner, local = [], LocalFolderOps(self.music)

        def step(o, k, **p):
            if not inner:
                inner.append(cw.rollback_folder_cleanup(op, store=self.store))
            return local.folder_op(o, k, **p)

        ad = mock.Mock()
        ad.folder_op.side_effect = step
        with mock.patch.object(te, "_folder_adapter", lambda a=None: a or ad):
            res = cw.rollback_folder_cleanup(op, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertEqual((inner[0]["ok"], inner[0]["code"]), (False, "rollback_not_eligible"))
        self.assertEqual(ad.folder_op.call_count, 1)
        self.assertTrue(src.is_dir() and not dst.exists())


if __name__ == "__main__":
    unittest.main()
