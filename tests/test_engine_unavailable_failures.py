"""Fail-closed adapter errors when Beets (in-process, ADR 0006) cannot answer.

1. Beets could not start: every call raises BeetsUnavailableError.
2. Writes refused by the self-test: a 503 BEETS_EMBEDDED_DEGRADED error, not
   "unavailable", so reads keep working.
3. Error statuses keep their typed errors; a body that is not JSON is
   reported without its text.
4. The adapter has no local transaction-engine fallback.
"""

import unittest
from unittest import mock

from flask import Response

from backend import beets_embedded
from backend.beets_adapter import BeetsAdapter, BeetsAdapterError, BeetsAuthError, BeetsError, BeetsUnavailableError


def _status(code):
    return {"checks": [{"code": code, "ok": False, "severity": "error", "message": f"{code} message"}]}


class TestEngineUnavailableFailures(unittest.TestCase):
    def setUp(self):
        self.client = BeetsAdapter()

    def _answer(self, status, body, content_type="application/json"):
        engine = mock.Mock()
        engine.call.side_effect = lambda *a, **k: Response(body, status=status, content_type=content_type)
        return mock.patch.object(BeetsAdapter, "_engine", return_value=engine)

    def test_engine_that_could_not_start_raises_beets_unavailable_error(self):
        down = beets_embedded.EmbeddedBeetsUnavailable(_status("library_open_failed"))
        with mock.patch.object(beets_embedded, "engine", side_effect=down):
            with self.assertRaises(BeetsUnavailableError) as ctx:
                self.client.get_items("test")
        self.assertEqual(ctx.exception.error_code, "BEETS_EMBEDDED_UNAVAILABLE")
        self.assertIn("library_open_failed message", str(ctx.exception))

    def test_refused_write_is_degraded_not_unavailable(self):
        engine = mock.Mock()
        engine.call.side_effect = beets_embedded.EmbeddedBeetsUnavailable(_status("library_locked"))
        with mock.patch.object(BeetsAdapter, "_engine", return_value=engine):
            with self.assertRaises(BeetsError) as ctx:
                self.client._request("POST", "/webmanager/modify", json_data={})
        self.assertNotIsInstance(ctx.exception, BeetsUnavailableError)
        self.assertEqual((ctx.exception.status_code, ctx.exception.error_code), (503, "BEETS_EMBEDDED_DEGRADED"))

    def test_auth_failure_401_raises_beets_auth_error(self):
        with self._answer(401, b'{"error": "nope", "error_code": "UNAUTHORIZED"}'):
            with self.assertRaises(BeetsAuthError) as ctx:
                self.client.get_album(1)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_server_error_500_raises_beets_error_not_unavailable(self):
        with self._answer(500, b'{"error": "Internal database lock error", "error_code": "db_locked"}'):
            with self.assertRaises(BeetsError) as ctx:
                self.client.get_album(1)
        self.assertNotIsInstance(ctx.exception, BeetsUnavailableError)
        self.assertEqual(ctx.exception.status_code, 500)

    def test_malformed_json_is_reported_without_its_text(self):
        with self._answer(200, b"{not json <html>502</html>"):
            with self.assertRaises(BeetsAdapterError) as ctx:
                self.client.get_stats()
        self.assertEqual(ctx.exception.error_code, "MALFORMED_RESPONSE")
        self.assertNotIn("not json", str(ctx.exception))

    def test_handler_exception_text_is_not_reported(self):
        engine = mock.Mock()
        engine.call.side_effect = RuntimeError("/private/path secret-detail")
        with mock.patch.object(BeetsAdapter, "_engine", return_value=engine), \
                self.assertLogs("beets.adapter", level="ERROR"):
            with self.assertRaises(BeetsAdapterError) as ctx:
                self.client.get_stats()
        self.assertEqual(ctx.exception.error_code, "BEETS_UPSTREAM_ERROR")
        self.assertNotIn("secret-detail", str(ctx.exception))

    def test_beets_adapter_does_not_import_transaction_engine(self):
        import backend.beets_adapter as client_mod
        self.assertNotIn("TransactionStore", dir(client_mod))
        self.assertNotIn("transaction_engine", dir(client_mod))
        self.assertNotIn("_local_engine_fallback", dir(client_mod))


if __name__ == "__main__":
    unittest.main()
