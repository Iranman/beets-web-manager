"""#220: every engine family on POST /api/transactions/<id>/apply gets the
controlled error contract (409 resource_busy, 503, fixed 500, no exception
text), and a metadata apply start failure never echoes its text (DELTA-3)."""

import unittest
from unittest import mock

from backend import resource_locks
from backend.beets_adapter import BeetsUnavailableError
from tests.test_staging_mutation_hardening import _RouteEnv


class GenericApplyErrorContractTests(_RouteEnv):
    CASES = (
        ("lock", resource_locks.ResourceLockConflictError("album:5", {"owner": "/secret/holder"}, "me"),
         409, "resource_busy"),
        ("timeout", TimeoutError("/secret/lock"), 409, "resource_busy"),
        ("unexpected", OSError("/secret/path"), 500, "apply_failed"),
        ("beets_unavailable", BeetsUnavailableError("/secret/engine"), 503, "BEETS_UNREACHABLE"),
    )

    def test_every_engine_family(self):
        import backend.composite_workflows as cw
        import routes_maintenance
        families = [f for f in routes_maintenance._ENGINE_FAMILIES if f != cw.ALBUM_CLEANUP_FAMILY]
        self.assertGreaterEqual(len(families), 7)
        for family in families:
            for name, exc, status, code in self.CASES:
                with self.subTest(family=family, case=name):
                    op = self.store.create(operation_type="Delete", status="Approved",
                                           metadata={"mutation_family": family})["id"]
                    entry = (mock.Mock(side_effect=exc), routes_maintenance._ENGINE_FAMILIES[family][1])
                    with mock.patch.dict(routes_maintenance._ENGINE_FAMILIES, {family: entry}):
                        resp = self.client.post(f"/api/transactions/{op}/apply")
                    body = resp.get_json()
                    self.assertEqual((resp.status_code, body["code"], body["ok"]), (status, code, False), body)
                    self.assertIs(body["mutated"], False)
                    self.assertNotIn("secret", str(body))

    def test_lock_conflict_after_claim_is_not_reported_unmutated(self):
        import routes_maintenance
        family = next(iter(routes_maintenance._ENGINE_FAMILIES))
        op = self.store.create(operation_type="Delete", status="Running",
                               metadata={"mutation_family": family})["id"]
        entry = (mock.Mock(side_effect=TimeoutError("/secret")), None)
        with mock.patch.dict(routes_maintenance._ENGINE_FAMILIES, {family: entry}):
            resp = self.client.post(f"/api/transactions/{op}/apply")
        body = resp.get_json()
        self.assertEqual(resp.status_code, 500, body)
        self.assertNotIn("mutated", body)
        self.assertNotIn("secret", str(body))

    def test_metadata_apply_start_failure_is_fixed_500(self):
        import app as app_module
        import backend.transaction_service as ts
        import routes_library
        with mock.patch.object(ts, "transactions", self.store), \
                mock.patch.object(routes_library, "transactions", self.store), \
                mock.patch.object(ts.jobs, "start_python", side_effect=ValueError("/secret/job")), \
                mock.patch.dict(app_module.app.config, {"TESTING": False, "PROPAGATE_EXCEPTIONS": False}):
            for url, payload in (("/api/transactions/{}/apply", None),
                                 ("/api/items/7/modify", {"fields": {"title": "New"}})):
                with self.subTest(url=url):
                    op = self.store.create(operation_type="Metadata Update", status="Approved",
                                           metadata={"item_id": 7, "pending_fields": {"title": "New"}})["id"]
                    if payload:
                        payload = {**payload, "apply_transaction_id": op}
                    resp = self.client.post(url.format(op), json=payload)
                    self.assertEqual(resp.status_code, 500, resp.get_json())
                    self.assertNotIn("secret", str(resp.get_json()))
                    self.assertEqual(self.store.get(op)["status"], "Failed")


class GenericRollbackErrorContractTests(_RouteEnv):
    """#227 SEC-227-1: an engine-family rollback gets its own controlled
    contract -- 409 resource_busy for a lock conflict (mutated: false only
    for families that lock before writing), 503 for Beets unavailable,
    otherwise a fixed 500 rollback_failed; never exception text."""

    def _rollback(self, op, family, exc):
        import routes_maintenance
        entry = (routes_maintenance._ENGINE_FAMILIES[family][0], mock.Mock(side_effect=exc))
        with mock.patch.dict(routes_maintenance._ENGINE_FAMILIES, {family: entry}):
            resp = self.client.post(f"/api/transactions/{op}/rollback")
        return resp.status_code, resp.get_json()

    def test_every_engine_family(self):
        import backend.composite_workflows as cw
        import routes_maintenance
        families = [f for f in routes_maintenance._ENGINE_FAMILIES if f != cw.ALBUM_CLEANUP_FAMILY]
        self.assertGreaterEqual(len(families), 7)
        for family in families:
            locks_first = family in routes_maintenance._ROLLBACK_LOCKS_BEFORE_WRITE
            cases = (
                ("lock", resource_locks.ResourceLockConflictError("album:5", {"owner": "/secret/h"}, "me"),
                 409, "resource_busy"),
                ("timeout", TimeoutError("/secret/lock"), 500, "rollback_failed"),
                ("unexpected", OSError("/secret/path"), 500, "rollback_failed"),
                ("beets_unavailable", BeetsUnavailableError("/secret/engine"), 503, "BEETS_UNREACHABLE"),
            )
            for name, exc, status, code in cases:
                with self.subTest(family=family, case=name):
                    op = self.store.create(operation_type="Delete", status="Completed",
                                           metadata={"mutation_family": family})["id"]
                    got, body = self._rollback(op, family, exc)
                    self.assertEqual((got, body["code"], body["ok"]), (status, code, False), body)
                    self.assertNotIn("secret", str(body))
                    if name == "lock" and locks_first:
                        self.assertIs(body["mutated"], False)
                    else:
                        self.assertNotIn("mutated", body)

    def test_lock_first_families_really_lock_before_any_write(self):
        """The real rollback executors behind _ROLLBACK_LOCKS_BEFORE_WRITE
        raise the conflict before any engine call or store write."""
        import backend.album_row_merge as arm
        import backend.untracked_recovery_service as urs
        cases = (
            (arm, arm.ALBUM_ROW_MERGE_FAMILY, "rollback_album_row_merge",
             {"engine_result": {"merge_id": "m1"}, "release_group_id": "rg", "target_album_id": 1,
              "source_album_ids": [2], "items": []}),
            (urs, urs.ATTACH_FAMILY, "untracked_rollback", {"engine_result": {"record_id": "r1"}, "album_id": 5}),
            (urs, urs.QUARANTINE_FAMILY, "untracked_rollback", {"engine_result": {"record_id": "r1"}}),
            (urs, urs.ATTACH_ALBUM_FAMILY, "untracked_rollback",
             {"engine_result": {"record_id": "r1"}, "release_group_id": "rg"}),
        )
        conflict = resource_locks.ResourceLockConflictError("album:5", {"owner": "/secret/h"}, "me")
        for module, family, engine_call, meta in cases:
            with self.subTest(family=family):
                op = self.store.create(operation_type="Delete", status="Completed",
                                       metadata={"mutation_family": family, **meta})["id"]
                ad = mock.Mock()
                with mock.patch.object(module, "beets_adapter", ad),                         mock.patch.object(resource_locks.ResourceLocks, "hold", side_effect=conflict):
                    resp = self.client.post(f"/api/transactions/{op}/rollback")
                body = resp.get_json()
                self.assertEqual((resp.status_code, body["code"], body["mutated"]), (409, "resource_busy", False))
                getattr(ad, engine_call).assert_not_called()
                self.assertEqual(self.store.get(op)["status"], "Completed")

    def test_no_lock_families_take_no_lock(self):
        """Item replacement, track quarantine and reviewed cleanup rollbacks
        hold no lock, so they are not in the proven set."""
        import backend.composite_workflows as cw
        import backend.duplicate_cleanup as dc
        import routes_maintenance
        for family in (cw.ITEM_FILE_REPLACEMENT_FAMILY, cw.TRACK_QUARANTINE_FAMILY, dc.REVIEWED_CLEANUP_FAMILY):
            self.assertNotIn(family, routes_maintenance._ROLLBACK_LOCKS_BEFORE_WRITE)

    def test_real_executor_failure_is_fixed_500_and_log_is_redacted(self):
        import backend.composite_workflows as cw
        op = self.store.create(operation_type="Delete", status="Completed", metadata={
            "mutation_family": cw.TRACK_QUARANTINE_FAMILY, "engine_result": {"quarantine_id": "q1"}})["id"]
        ad = mock.Mock(**{"rollback_quarantine_remove_items.side_effect": OSError("/secret password=hunter2")})
        with mock.patch.object(cw, "beets_adapter", ad), self.assertLogs("app", "ERROR") as logs:
            resp = self.client.post(f"/api/transactions/{op}/rollback")
        body = resp.get_json()
        self.assertEqual((resp.status_code, body["code"]), (500, "rollback_failed"), body)
        self.assertNotIn("mutated", body)
        self.assertNotIn("secret", str(body))
        text = "\n".join(logs.output)
        self.assertIn("OSError", text)
        self.assertNotIn("hunter2", text)


if __name__ == "__main__":
    unittest.main()
