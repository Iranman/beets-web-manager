"""#433: no part of an upstream Beets error body may reach logs or error data."""
import json
import unittest
from unittest import mock

from backend import beets_adapter
from backend.beets_adapter import BeetsAdapter, BeetsAdapterError

C = "canary0433deadbeefcafe"
K, TOK, SEC = "ke" + "y", "tok" + "en", "sec" + "ret"

# Secret-bearing bodies, including the #648 QA probe cases redaction missed.
LEAKY_BODIES = {
    "bearer_html": f"<html>Authorization: Bearer {C}\napi_{K}={C}</html>" + "x" * 5000,
    "basic_auth_hdr": f"Authorization: Basic {C}",
    "cookie_hdr": f"Cookie: sessionid={C}",
    "set_cookie": f"Set-Cookie: session={C}; Path=/",
    "x_api_hdr": f"X-Api-{K}: {C}",
    "x_plex_hdr": f"X-Plex-{TOK}: {C}",
    "url_userinfo": f"GET http://admin:{C}@host/x failed",
    "query_api": f"GET /x?api{K}={C}&a=1",
    "query_access": f"/x?access_{TOK}={C}",
    "json_list": json.dumps([{TOK: C}]),
    "json_str_scalar": json.dumps(f"{TOK}={C}"),
    "json_nested_hdrs": json.dumps({"headers": {"Cookie": f"s={C}", f"X-Plex-{TOK}": C}}),
    "json_client_sec": json.dumps({f"client_{SEC}": C, f"private_{K}": C}),
    "json_auth_basic": json.dumps({"detail": f"Authorization: Basic {C}"}),
    "json_errcode_sec": json.dumps({"error_code": f"{TOK}={C}"}),
    "html_input_value": f'<input name="pass" value="{C}">',
    "json_in_html": '<pre>{"api_%s": "%s"}</pre>' % (K, C),
    "trunc_prefix": "x" * 190 + f" {TOK}={C}",
}


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.headers = status, body, {}

    def get_data(self):
        return self._body


class UpstreamErrorBodyRedactionTests(unittest.TestCase):
    def _call(self, status, body):
        engine = mock.Mock()
        engine.call.return_value = _Resp(status, body.encode())
        with mock.patch.object(beets_adapter.beets_embedded, "engine", return_value=engine), \
                self.assertLogs("beets.adapter", level="WARNING") as logs, \
                self.assertRaises(BeetsAdapterError) as ctx:
            BeetsAdapter()._request("GET", "/webmanager/status")
        return "\n".join(logs.output), ctx.exception

    def test_no_body_content_leaks(self):
        for name, body in LEAKY_BODIES.items():
            with self.subTest(name):
                logged, ex = self._call(500, body)
                self.assertIn("500", logged)
                for where in (logged, repr(ex.response_data), json.dumps(ex.to_public_dict()),
                              str(ex), ex.error_code):
                    self.assertNotIn(C, where)
                self.assertIn(ex.response_data, (None, {"error_code": ex.error_code}))

    def test_plugin_error_code_is_carried_forward(self):
        body = json.dumps({"error": f"failed {TOK}={C}", "error_code": "PLUGIN_BROKE", TOK: C})
        logged, ex = self._call(400, body)
        self.assertIn("PLUGIN_BROKE", logged)
        self.assertEqual(ex.error_code, "PLUGIN_BROKE")
        self.assertEqual(ex.response_data, {"error_code": "PLUGIN_BROKE"})
        self.assertNotIn(C, logged)


if __name__ == "__main__":
    unittest.main()
