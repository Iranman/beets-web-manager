"""#433: an upstream Beets error body must not reach logs or error data raw."""
import json
import unittest
from unittest import mock

from backend import beets_adapter
from backend.beets_adapter import BeetsAdapter, BeetsAdapterError

CANARY = "canary0433deadbeefcafe"


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.headers = status, body, {}

    def get_data(self):
        return self._body


class UpstreamErrorBodyRedactionTests(unittest.TestCase):
    def _call(self, status, body):
        engine = mock.Mock()
        engine.call.return_value = _Resp(status, body)
        with mock.patch.object(beets_adapter.beets_embedded, "engine", return_value=engine), \
                self.assertLogs("beets.adapter", level="WARNING") as logs, \
                self.assertRaises(BeetsAdapterError) as ctx:
            BeetsAdapter()._request("GET", "/webmanager/status")
        return "\n".join(logs.output), ctx.exception

    def test_html_body_echoing_headers_is_redacted(self):
        body = (f"<html>proxy error\nAuthorization: Bearer {CANARY}\napi_key={CANARY}</html>"
                + "x" * 5000).encode()
        logged, ex = self._call(502, body)
        self.assertIn("502", logged)
        self.assertNotIn(CANARY, logged)
        self.assertLess(len(logged), 500)
        self.assertNotIn(CANARY, repr(ex.response_data))
        self.assertNotIn(CANARY, json.dumps(ex.to_public_dict()))

    def test_json_body_keeps_error_code_and_redacts_secrets(self):
        body = json.dumps({"error_code": "PLUGIN_BROKE", "token": CANARY,
                           "detail": f"password={CANARY}"}).encode()
        logged, ex = self._call(500, body)
        self.assertIn("PLUGIN_BROKE", logged)
        self.assertEqual(ex.error_code, "PLUGIN_BROKE")
        self.assertNotIn(CANARY, logged)
        self.assertNotIn(CANARY, repr(ex.response_data))


if __name__ == "__main__":
    unittest.main()
