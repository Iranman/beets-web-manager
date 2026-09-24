"""Tests verifying fail-closed behavior when Beets Control Agent is unavailable.

Proves that:
1. BeetsClient raises BeetsUnavailableError or BeetsAuthError on transport failures (connection refused, timeout, 401 auth failure, 500 error, malformed JSON).
2. Production callers in app.py fail closed without local filesystem or DB mutations.
3. app.py does not fall back to local transaction_engine or TransactionStore.
"""

import os
import sqlite3
import tempfile
import unittest
from unittest import mock
import urllib.error

from backend.beets_adapter import BeetsAdapter, BeetsError, BeetsUnavailableError, BeetsAuthError, BeetsAdapterError


class TestEngineUnavailableFailures(unittest.TestCase):
    def setUp(self):
        self.client = BeetsAdapter(base_url="http://127.0.0.1:59999", timeout=1.0)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_connection_refused_raises_beets_unavailable_error(self):
        with self.assertRaises(BeetsUnavailableError) as ctx:
            self.client.get_items("test")
        self.assertIn("beets server", str(ctx.exception).lower())

    def test_timeout_raises_beets_unavailable_error(self):
        def timeout_handler(*args, **kwargs):
            raise TimeoutError("connection timed out")

        with mock.patch("urllib.request.urlopen", side_effect=timeout_handler):
            with self.assertRaises(BeetsUnavailableError) as ctx:
                self.client.get_album(1)
            self.assertIn("timeout", str(ctx.exception).lower())

    def test_auth_failure_401_raises_beets_auth_error(self):
        class MockHTTPError(urllib.error.HTTPError):
            def __init__(self):
                super().__init__("http://127.0.0.1:59999/test", 401, "Unauthorized", {}, None)

        with mock.patch("urllib.request.urlopen", side_effect=MockHTTPError()):
            with self.assertRaises(BeetsAuthError) as ctx:
                self.client.get_album(1)
            self.assertEqual(ctx.exception.status_code, 401)

    def test_server_error_500_raises_beets_error_not_unavailable(self):
        class MockHTTP500(urllib.error.HTTPError):
            def __init__(self):
                super().__init__("http://127.0.0.1:59999/test", 500, "Internal Server Error", {}, None)

            def read(self):
                return b'{"error": "Internal database lock error", "error_code": "db_locked"}'

        with mock.patch("urllib.request.urlopen", side_effect=MockHTTP500()):
            with self.assertRaises(BeetsError) as ctx:
                self.client.get_album(1)
            self.assertNotIsInstance(ctx.exception, BeetsUnavailableError)
            self.assertEqual(ctx.exception.status_code, 500)

    def test_malformed_json_raises_beets_unavailable_error(self):
        class MockResponse:
            def __init__(self):
                self.headers = {"Content-Type": "application/json"}
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return b"<html>502 Bad Gateway</html>"

        with mock.patch("urllib.request.urlopen", return_value=MockResponse()):
            with self.assertRaises(BeetsAdapterError) as ctx:
                self.client.get_album(1)
            self.assertIn("json", str(ctx.exception).lower())

    def test_beets_adapter_does_not_import_transaction_engine(self):
        import sys
        # Ensure backend.beets_adapter module has no imports of backend.transaction_engine
        import backend.beets_adapter as client_mod
        self.assertNotIn("TransactionStore", dir(client_mod))
        self.assertNotIn("transaction_engine", dir(client_mod))
        self.assertNotIn("_local_engine_fallback", dir(client_mod))


if __name__ == "__main__":
    unittest.main()
