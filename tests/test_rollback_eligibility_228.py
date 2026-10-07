"""#228: the server computes rollback eligibility (rollback.allowed + reason)
from the same gate POST /api/transactions/<id>/rollback applies, and the
two plan builders that could never be rolled back no longer claim they can.

Every eligibility case is checked against what the route actually does."""

from unittest import mock

import backend.composite_workflows as cw
from tests.test_staging_mutation_hardening import _RouteEnv

_META_OP = {"type": "metadata_restore", "item_id": 7, "fields": {"title": "Old"}}


class RollbackEligibilityTests(_RouteEnv):
    def setUp(self):
        super().setUp()
        import routes_maintenance
        self.rm = routes_maintenance
        p = mock.patch.object(routes_maintenance, "_run_item_metadata_restore", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def _local(self, status, operations=(_META_OP,), available=True, **meta):
        tx = self.store.create(operation_type="Metadata Update", status=status, metadata=meta)
        self.store.update(tx["id"], rollback={"available": available, "operations": list(operations)})
        return tx["id"]

    def _engine(self, status, family=cw.ITEM_FILE_REPLACEMENT_FAMILY, **meta):
        return self.store.create(operation_type="Replace", status=status,
                                 metadata={"mutation_family": family, **meta})["id"]

    def _verdict(self, tx_id):
        return self.rm.rollback_eligibility(self.store.get(tx_id))

    def _detail_rollback(self, tx_id):
        return self.client.get(f"/api/transactions/{tx_id}").get_json()["transaction"]["rollback"]

    def _rollback(self, tx_id):
        return self.client.post(f"/api/transactions/{tx_id}/rollback")

    # -- local families --------------------------------------------------
    def test_completed_metadata_restore_is_allowed_and_route_accepts(self):
        tx = self._local("Completed")
        self.assertEqual(self._verdict(tx), {"allowed": True, "code": "allowed", "reason": ""})
        rb = self._detail_rollback(tx)
        self.assertEqual((rb["available"], rb["allowed"], rb["allowed_code"], rb["allowed_reason"]),
                         (True, True, "allowed", ""))
        self.assertEqual(self._rollback(tx).status_code, 200)

    def test_failed_with_engine_result_is_allowed(self):
        tx = self._local("Failed", engine_result={"ok": True})
        self.assertTrue(self._verdict(tx)["allowed"])
        self.assertEqual(self._rollback(tx).status_code, 200)

    def test_not_completed_statuses_are_refused_like_the_route(self):
        for status in ("Preview", "Approved", "Cancelled", "Failed", "Rolled Back", "Running"):
            tx = self._local(status)
            verdict = self._verdict(tx)
            self.assertEqual((verdict["allowed"], verdict["code"]), (False, "not_completed"), status)
            resp = self._rollback(tx)
            self.assertEqual(resp.status_code, 409, status)
            self.assertEqual(resp.get_json()["error"], verdict["reason"], status)

    def test_no_operations_is_refused_like_the_route(self):
        tx = self._local("Completed", operations=())
        verdict = self._verdict(tx)
        self.assertEqual((verdict["allowed"], verdict["code"]), (False, "unavailable"))
        resp = self._rollback(tx)
        self.assertEqual((resp.status_code, resp.get_json()["error"]), (409, verdict["reason"]))

    def test_unavailable_is_refused_like_the_route(self):
        tx = self._local("Completed", available=False)
        self.assertEqual(self._verdict(tx)["code"], "unavailable")
        self.assertEqual(self._rollback(tx).status_code, 409)

    def test_unsupported_operation_is_refused_like_the_route(self):
        tx = self._local("Completed", operations=({"type": "file_move", "item_id": 1},))
        verdict = self._verdict(tx)
        self.assertEqual((verdict["allowed"], verdict["code"]), (False, "unsupported_operation"))
        resp = self._rollback(tx)
        self.assertEqual((resp.status_code, resp.get_json()["error"]), (409, verdict["reason"]))

    def test_composite_family_in_the_store_is_refused(self):
        """Library cleanup / folder cleanup / import review cleanup records
        carry no local operations: not allowed, and the route answers 409."""
        tx = self.store.create(operation_type="Library Cleanup", status="Completed",
                               rollback_available=True, metadata={"mutation_family": "folder_cleanup_v1"})["id"]
        self.assertFalse(self._verdict(tx)["allowed"])
        self.assertEqual(self._rollback(tx).status_code, 409)

    # -- engine families -------------------------------------------------
    def test_engine_family_without_engine_result_is_refused_like_the_executor(self):
        tx = self._engine("Completed")
        self.assertEqual(self._verdict(tx)["code"], "not_applied")
        resp = self._rollback(tx)
        self.assertEqual((resp.status_code, resp.get_json()["code"]), (400, "not_applied"))

    def test_engine_family_with_engine_result_is_allowed_from_any_status(self):
        for status in ("Completed", "Failed", "Recovery Required"):
            tx = self._engine(status, engine_result={"quarantine_id": "q1"})
            self.assertTrue(self._verdict(tx)["allowed"], status)

    def test_engine_family_already_rolled_back_is_not_allowed(self):
        tx = self._engine("Rolled Back", engine_result={"quarantine_id": "q1"})
        self.assertEqual(self._verdict(tx)["code"], "already_rolled_back")
        # The route answers a no-op success: nothing is left to roll back.
        self.assertEqual(self._rollback(tx).get_json()["status"], "Rolled Back")

    def test_album_cleanup_is_not_supported(self):
        tx = self._engine("Completed", family=cw.ALBUM_CLEANUP_FAMILY, engine_result={"ok": True})
        self.assertEqual(self._verdict(tx)["code"], "not_supported")
        self.assertEqual(self._rollback(tx).get_json()["code"], "not_supported")

    # -- responses -------------------------------------------------------
    def test_list_rows_carry_eligibility(self):
        ok = self._local("Completed")
        no = self._local("Preview")
        rows = {r["id"]: r["rollback"] for r in self.client.get("/api/transactions").get_json()["transactions"]}
        self.assertEqual((rows[ok]["allowed"], rows[no]["allowed"]), (True, False))
        self.assertEqual(rows[no]["allowed_code"], "not_completed")
        self.assertIn("available", rows[ok])

    def test_engine_rollback_that_needs_recovery_reports_mutated(self):
        """#228 F3: the engine ran the rollback but could not verify it."""
        from app import app
        res = {"ok": False, "status": "Recovery Required", "rollback_problems": ["x"]}
        with app.test_request_context():
            resp, status = self.rm._item_file_replacement_response(lambda _id: res, "t1",
                                                                    rollback_family="album_row_merge_v1")
        self.assertEqual(status, 400)
        self.assertIs(resp.get_json()["mutated"], True)


class HonestPlanFlagsTests(_RouteEnv):
    """#228 F4: these plans record no rollback operations and no engine
    family, so they must not claim rollback is available."""

    def test_artist_folder_reconcile_plan(self):
        ad = mock.MagicMock()
        ad.get_album.return_value = {"album": "A", "albumartist": "X", "artist": "X"}
        plan = cw.plan_artist_folder_reconcile({"artist": "X", "album_ids": [1]}, adapter=ad, store=self.store)
        tx_id = plan.get("operation_id") or plan["transaction"]["id"]
        self.assertFalse(self.store.get(tx_id)["rollback"]["available"])

    def test_album_mb_track_repair_plan(self):
        ad = mock.MagicMock()
        rel, rg = "aaaaaaaa-0000-4000-8000-00000000000a", "aaaaaaaa-1111-4000-8000-00000000000a"
        ad.get_album.return_value = {"id": 5, "album": "A", "mb_albumid": rel, "mb_releasegroupid": rg}
        ad.find_all_items_by_album_id.return_value = [{"id": 1, "album_id": 5, "title": "t", "mb_trackid": ""}]
        with mock.patch.object(cw, "_release_group_for_release", return_value=rg):
            plan = cw.plan_album_mb_track_repair({"album_id": 5}, adapter=ad, store=self.store)
        self.assertTrue(plan.get("ok"), plan)
        tx_id = plan.get("operation_id") or plan["transaction"]["id"]
        self.assertFalse(self.store.get(tx_id)["rollback"]["available"])
