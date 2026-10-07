"""QA for #220: the generic apply contract against a REAL engine executor
(track quarantine), not a mocked one -- mutated:false only before the claim."""

import contextlib
import unittest
from unittest import mock

import backend.composite_workflows as cw
from backend import resource_locks
from backend.beets_adapter import BeetsAdapterTimeoutError
from tests.test_staging_mutation_hardening import _RouteEnv


class RealExecutorContractTests(_RouteEnv):
    def _op(self):
        return self.store.create(operation_type="Delete", status="Approved", metadata={
            "mutation_family": cw.TRACK_QUARANTINE_FAMILY, "album_id": 5,
            "items": [{"item_id": 7, "sha256": "ab" * 32}]})["id"]

    def _post(self, op, ad, hold):
        with mock.patch.object(cw, "beets_adapter", ad), \
                mock.patch.object(resource_locks.ResourceLocks, "hold", hold):
            resp = self.client.post(f"/api/transactions/{op}/apply")
        return resp.status_code, resp.get_json()

    def test_lock_held_before_claim_is_409_unmutated(self):
        op, ad = self._op(), mock.Mock()
        conflict = resource_locks.ResourceLockConflictError("album:5", {"owner": "/secret/holder"}, "me")
        status, body = self._post(op, ad, mock.Mock(side_effect=conflict))
        self.assertEqual((status, body["code"], body["mutated"]), (409, "resource_busy", False), body)
        self.assertNotIn("secret", str(body))
        self.assertEqual(self.store.get(op)["status"], "Approved")
        ad.quarantine_remove_items.assert_not_called()

    def test_timeout_after_claim_never_reports_unmutated(self):
        free = lambda *a, **k: contextlib.nullcontext()  # noqa: E731
        for exc, want_status, want_tx in ((TimeoutError("/secret"), 500, "Failed"),
                                          (BeetsAdapterTimeoutError("/secret"), 503, "Running")):
            with self.subTest(exc=type(exc).__name__):
                op = self._op()
                ad = mock.Mock(**{"get_stats.return_value": {"items": 10},
                                  "quarantine_remove_items.side_effect": exc})
                status, body = self._post(op, ad, free)
                self.assertEqual(status, want_status, body)
                self.assertNotIn("mutated", body)
                self.assertNotIn("secret", str(body))
                self.assertEqual(self.store.get(op)["status"], want_tx)
                ad.quarantine_remove_items.assert_called_once()


if __name__ == "__main__":
    unittest.main()
