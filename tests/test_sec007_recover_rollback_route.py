"""SEC-007 regression: the recover-import rollback route must not echo raw
exception text to the client. CodeQL py/stack-trace-exposure #1443 flagged the
``_s(exc)`` returned for a KeyError/ValueError from ``transactions.rollback()``
in routes_maintenance.api_transaction_rollback(); it now returns a bounded
message and logs only the exception type server-side.
"""

import json
import unittest
from unittest import mock

import routes_maintenance as rm
from app import app


class RecoverRollbackRouteStackTraceTests(unittest.TestCase):
    def _call(self, side_effect):
        tx = {"status": "Completed",
              "metadata": {"mutation_family": rm.RECOVER_IMPORT_FAMILY}}
        fake_tx = mock.Mock()
        fake_tx.get.return_value = tx
        fake_tx.rollback.side_effect = side_effect
        with app.test_request_context("/api/transactions/tx1/rollback", method="POST"), \
             mock.patch.object(rm, "transactions", fake_tx), \
             mock.patch.object(rm, "_sync_transactions_from_jobs", lambda: None), \
             mock.patch.object(rm, "rollback_eligibility", return_value={"allowed": True}):
            response = rm.api_transaction_rollback("tx1")
        body, status = response
        return body.get_json(), status

    def test_valueerror_detail_is_not_exposed(self):
        data, status = self._call(ValueError("LEAK_MARKER /internal/path token=secret-abc"))
        self.assertEqual(status, 409)
        self.assertNotIn("LEAK_MARKER", json.dumps(data))
        self.assertNotIn("secret-abc", json.dumps(data))
        self.assertNotIn("/internal/path", json.dumps(data))
        self.assertEqual(data["error"], "Rollback could not be performed for this transaction.")

    def test_keyerror_detail_is_not_exposed(self):
        data, status = self._call(KeyError("LEAK_MARKER-missing-tx"))
        self.assertEqual(status, 409)
        self.assertNotIn("LEAK_MARKER", json.dumps(data))
        self.assertEqual(data["error"], "Rollback could not be performed for this transaction.")


if __name__ == "__main__":
    unittest.main()
