"""QA #306 round 2: N1 startup sweep safety and BUSY folder-rollback deferral
through the composite wrapper / route. Temp stores and fake adapters only."""

import time
import unittest
from unittest import mock

import backend.composite_workflows as cw
from backend import transaction_engine as te
from backend import transaction_recovery
from backend.beets_adapter import BeetsAdapterError
from tests._folder_ops_local import LocalFolderOps
from tests.test_staging_mutation_hardening import _RouteEnv
from tests.test_wave0_s1_containment import FakeAdapter, _Env


def _adapter():
    ad = mock.Mock()
    ad.get_album.return_value = {"id": 1}
    ad.find_all_items_by_album_id.return_value = []
    return ad


class SweepSafety(_Env):
    def test_a_live_apply_claimed_after_start_is_never_touched(self):
        started = time.time()
        op = cw.plan_album_relocation({"album_id": 1}, store=self.store)["operation_id"]
        ad, seen = _adapter(), []
        # The background sweep retries for minutes; one pass lands mid-write.
        ad.move.side_effect = lambda **_k: seen.append(
            transaction_recovery.sweep(adapter=ad, store=self.store, before=started))
        res = cw.apply_album_relocation(op, adapter=ad, store=self.store)
        self.assertEqual(seen, [[]])
        self.assertEqual((res["status"], self.store.get(op)["status"]), ("Completed", "Completed"))

    def test_only_claimed_composite_applies_are_resolved(self):
        later = time.time() + 1
        engine = self.store.create(operation_type="Replace", status="Running", metadata={
            "mutation_family": cw.ITEM_FILE_REPLACEMENT_FAMILY, "engine_request": {"operation_id": "x"},
            "engine_result": {"mutation_started": True}})["id"]
        job = self.store.create(operation_type="Job", status="Running", metadata={})["id"]
        folder = self.store.create(operation_type="Cleanup", status="Running", metadata={
            "mutation_family": "folder_cleanup_v1", "mutation_started": True,
            "engine_result": {"moved_records": [], "removed_dirs": []}})["id"]
        ad = mock.Mock()
        ad.get_operation.return_value = {"status": "running"}
        transaction_recovery.sweep(adapter=ad, store=self.store, before=later)
        self.assertEqual([self.store.get(x)["status"] for x in (engine, job, folder)], ["Running"] * 3)

    def test_recovery_required_after_restart_rollback_eligibility(self):
        """What an operator can do next with a swept apply."""
        out = {}
        for name, plan, apply, rollback in [
            ("item_metadata", lambda: cw.plan_item_metadata({"item_id": 5, "updates": {"title": "T"}}, store=self.store),
             cw.apply_item_metadata, cw.rollback_item_metadata),
            ("album_relocation", lambda: cw.plan_album_relocation({"album_id": 1}, store=self.store),
             cw.apply_album_relocation, cw.rollback_album_relocation)]:
            op = plan()["operation_id"]
            ad = _adapter()
            ad.modify.side_effect = ad.move.side_effect = KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                apply(op, adapter=ad, store=self.store)
            transaction_recovery.sweep(adapter=ad, store=self.store, before=time.time() + 1)
            self.assertEqual(self.store.get(op)["status"], "Recovery Required")
            out[name] = rollback(op, store=self.store).get("code")
        print("\nQA306 recovery-required rollback codes:", out)


class BusyThroughComposite(_RouteEnv):
    def _busy(self):
        ad = mock.Mock()
        ad.folder_op.side_effect = BeetsAdapterError("busy", status_code=503, error_code="BUSY")
        return ad

    def test_route_busy_on_a_completed_cleanup_keeps_completed(self):
        (self.music / "Empty").mkdir()
        op = te.create_folder_cleanup_plan(self.store, {"action": "remove_empty",
                                                        "source": str(self.music / "Empty")})["operation_id"]
        self.store.transition(op, "Preview", "Approved")
        te.execute_folder_cleanup_apply(self.store, op, adapter=LocalFolderOps(self.music))
        self.assertEqual(self.store.get(op)["status"], "Completed")
        busy = self._busy()
        with mock.patch.object(cw, "beets_adapter", FakeAdapter()), \
                mock.patch.object(te, "_folder_adapter", lambda a=None: a or busy):
            resp = self.client.post(f"/api/transactions/{op}/rollback")
        self.assertEqual((resp.status_code, resp.get_json()["code"]), (409, "rollback_deferred"))
        self.assertEqual(self.store.get(op)["status"], "Completed")

    def test_partial_restore_then_busy_then_retry_completes(self):
        srcs = [self.music / "A" / n for n in ("one.flac", "two.flac")]
        for s in srcs:
            s.parent.mkdir(parents=True, exist_ok=True)
            s.write_bytes(b"x")
        (self.music / "B").mkdir()
        op = te.create_folder_cleanup_plan(self.store, {"action": "merge", "source": str(self.music / "A"),
                                                        "target": str(self.music / "B")})
        self.assertTrue(op.get("ok"), op)
        op = op["operation_id"]
        self.store.transition(op, "Preview", "Approved")
        self.assertTrue(te.execute_folder_cleanup_apply(self.store, op, adapter=LocalFolderOps(self.music))["ok"])
        n = len(self.store.get(op)["metadata"]["moved_records"])
        self.assertGreaterEqual(n, 2)
        local, calls = LocalFolderOps(self.music), []

        def first_ok_then_busy(o, k, **p):
            calls.append(k)
            if len(calls) > 1:
                raise BeetsAdapterError("busy", status_code=503, error_code="BUSY")
            return local.folder_op(o, k, **p)

        ad = mock.Mock()
        ad.folder_op.side_effect = first_ok_then_busy
        with mock.patch.object(te, "_folder_adapter", lambda a=None: a or ad):
            res = cw.rollback_folder_cleanup(op, store=self.store)
        self.assertEqual((res["code"], res["mutated"]), ("rollback_deferred", True))
        self.assertNotIn("files_restored_count", self.store.get(op)["metadata"])
        with mock.patch.object(te, "_folder_adapter", lambda a=None: a or LocalFolderOps(self.music)):
            res = cw.rollback_folder_cleanup(op, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.store.get(op)["status"], "Rolled Back")
        self.assertTrue(all(s.is_file() for s in srcs))


if __name__ == "__main__":
    unittest.main()
