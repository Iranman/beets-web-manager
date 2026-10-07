"""#208 root cause: a BEETS_WEB_URL with user:pass@ never authenticated
(urllib sends no Basic header and resolves "u:pw@host" as the host), and a
URL without a port raised an uncaught http.client.InvalidURL whose message
held the password. Such a URL is now refused at adapter construction and in
setup status, and HTTPException/ValueError from urllib are reported with the
redacted URL only. Each test fails on main."""
import http.client
import logging
import os
import unittest
import urllib.error
from unittest import mock

import backend.beets_adapter as ba

try:
    from test_routes_setup import _load_routes_setup_against_stub_app
except ImportError:  # pragma: no cover
    from tests.test_routes_setup import _load_routes_setup_against_stub_app

SECRET = "Sup3rSecretPw"
USERINFO_URLS = (
    f"http://u:{SECRET}@beets:8337",          # with port
    f"http://u:{SECRET}@beets",               # no port (was InvalidURL)
    "http://u:Sup3r%40SecretPw@beets:8337",   # percent-encoded password
    f"http://u:{SECRET}/x@beets:8337",        # "/" hides the "@" from urlsplit
)


def _calls():
    return (
        lambda a: a.get_stats(),
        lambda a: a.get_plugin_status(),
        lambda a: a.open_item_file(1),
        lambda a: a.open_album_art(1),
    )


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class UserinfoUrlRefusedTests(unittest.TestCase):
    def setUp(self):
        self.logs = _LogCapture()
        root = logging.getLogger()
        root.addHandler(self.logs)
        self.addCleanup(root.removeHandler, self.logs)
        old_level = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, old_level)

    def _assert_clean(self, text):
        self.assertNotIn(SECRET, text)
        self.assertNotIn("Sup3r", text)

    def test_construction_refuses_userinfo_url_and_never_sends_it(self):
        for url in USERINFO_URLS:
            adapter = ba.BeetsAdapter(base_url=url)
            self.assertEqual(adapter.config_error_code, ba.BEETS_WEB_URL_USERINFO_CODE, url)
            self._assert_clean(adapter.base_url)
            self._assert_clean(adapter.get_item_file_url(1))
            self._assert_clean(adapter.get_album_art_url(1))
            for call in _calls():
                with mock.patch.object(ba.urllib.request, "urlopen") as urlopen:
                    with self.assertRaises(ba.BeetsAdapterConnectionError) as ctx:
                        call(adapter)
                urlopen.assert_not_called()
                self._assert_clean(str(ctx.exception))
                self.assertEqual(ctx.exception.error_code, "BEETS_WEB_URL_USERINFO")
                self.assertIn("remove them from BEETS_WEB_URL", str(ctx.exception))
        self._assert_clean("\n".join(self.logs.lines))

    def test_userinfo_url_from_environment_is_refused(self):
        with mock.patch.dict(os.environ, {"BEETS_WEB_URL": f"http://u:{SECRET}@beets"}):
            adapter = ba.BeetsAdapter()
        self.assertEqual(adapter.config_error_code, ba.BEETS_WEB_URL_USERINFO_CODE)
        with self.assertRaises(ba.BeetsAdapterConnectionError) as ctx:
            adapter.get_stats()
        self._assert_clean(str(ctx.exception))

    def test_plain_url_is_not_refused(self):
        adapter = ba.BeetsAdapter(base_url="http://beets:8337/")
        self.assertEqual(adapter.config_error_code, "")
        self.assertEqual(adapter.get_item_file_url(3), "http://beets:8337/item/3/file")


class HttpClientExceptionTests(unittest.TestCase):
    """InvalidURL / HTTPException / ValueError never escape the adapter raw."""

    def test_real_invalid_url_is_a_connection_error(self):
        adapter = ba.BeetsAdapter(base_url="http://127.0.0.1:notaport", timeout=2)
        for call in _calls():
            with self.assertRaises(ba.BeetsAdapterConnectionError) as ctx:
                call(adapter)
            self.assertIn("Cannot connect to Beets server", str(ctx.exception))

    def test_exception_text_is_never_logged_or_raised(self):
        adapter = ba.BeetsAdapter(base_url="http://beets:8337")
        errors = (
            http.client.InvalidURL(f"nonnumeric port: '{SECRET}@beets'"),
            http.client.IncompleteRead(SECRET.encode()),
            ValueError(f"unknown url type: {SECRET}"),
        )
        for exc in errors:
            for call in _calls():
                with mock.patch.object(ba.urllib.request, "urlopen", side_effect=exc), \
                        self.assertLogs("beets.adapter", level="WARNING") as logs:
                    with self.assertRaises(ba.BeetsAdapterConnectionError) as ctx:
                        call(adapter)
                self.assertNotIn(SECRET, str(ctx.exception))
                self.assertIn("http://beets:8337", str(ctx.exception))
                self.assertNotIn(SECRET, "\n".join(logs.output))
                self.assertIsNone(ctx.exception.__cause__)
                self.assertTrue(ctx.exception.__suppress_context__)


class SetupStatusUserinfoTests(unittest.TestCase):
    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
        self.client = self.flask_app.test_client()

    def _status(self, url):
        from backend.beets_adapter import beets_adapter
        with mock.patch.dict(os.environ, {"BEETS_WEB_URL": url}), \
                mock.patch.object(beets_adapter, "get_plugin_status", side_effect=RuntimeError("down")):
            self.module._invalidate_setup_status_cache()
            response = self.client.get("/api/setup/status?refresh=1")
        self.assertEqual(response.status_code, 200)
        return response

    def test_userinfo_url_is_a_blocking_reason_and_warning(self):
        for url in USERINFO_URLS:
            response = self._status(url)
            text = response.get_data(as_text=True)
            self.assertNotIn(SECRET, text, url)
            self.assertNotIn("Sup3r", text, url)
            body = response.get_json()
            self.assertIn(ba.BEETS_WEB_URL_USERINFO_MESSAGE, body["blocking_reasons"])
            ids = [w["id"] for w in body.get("warnings", [])]
            self.assertIn(ba.BEETS_WEB_URL_USERINFO_CODE, ids)

    def test_plain_url_has_no_userinfo_warning(self):
        body = self._status("http://beets:8337").get_json()
        self.assertNotIn(ba.BEETS_WEB_URL_USERINFO_CODE, [w["id"] for w in body.get("warnings", [])])
        self.assertNotIn(ba.BEETS_WEB_URL_USERINFO_MESSAGE, body["blocking_reasons"])


if __name__ == "__main__":
    unittest.main()
