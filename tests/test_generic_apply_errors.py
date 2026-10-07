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


if __name__ == "__main__":
    unittest.main()
