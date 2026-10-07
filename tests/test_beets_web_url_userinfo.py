"""#208 root cause: a BEETS_WEB_URL with user:pass@ never authenticated
(urllib sends no Basic header and resolves "u:pw@host" as the host), and a
URL without a port raised an uncaught http.client.InvalidURL whose message
held the password. Such a URL is now refused at adapter construction and in
setup status, and HTTPException/ValueError from urllib are reported with the
redacted URL only. Each test fails on main."""
import http.client
import logging
import os
import tempfile
import unittest
from pathlib import Path
import urllib.error
from unittest import mock

import backend.beets_adapter as ba
from backend.security import OutboundPolicyError

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
    f"http://u:{SECRET}\uff20beets:8337",     # fullwidth @ (NFKC -> "@")
    f"http://u%3A{SECRET}%40beets:8337",      # whole userinfo percent-encoded
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


class RequestConstructionTests(unittest.TestCase):
    """#208 F2: Request() parses the URL; its ValueError can quote the URL,
    so it is built inside the try and reported like any connection error."""

    def test_request_construction_error_is_wrapped(self):
        adapter = ba.BeetsAdapter(base_url="http://beets:8337")
        for call in _calls():
            with mock.patch.object(ba.urllib.request, "Request", side_effect=ValueError(f"bad netloc {SECRET}")), \
                    self.assertLogs("beets.adapter", level="WARNING") as logs:
                with self.assertRaises(ba.BeetsAdapterConnectionError) as ctx:
                    call(adapter)
            self.assertNotIn(SECRET, str(ctx.exception))
            self.assertNotIn(SECRET, "\n".join(logs.output))


class SetupEnvUrlRedactionTests(unittest.TestCase):
    """#183 F1: GET /api/setup/env never returns user:pass@ of a URL setting,
    and saving the form back does not overwrite the stored value."""

    PLEX = f"http://u:{SECRET}@plex:32400"
    AI = f"https://u:{SECRET}@ai.example/v1"
    LIDARR = f"http://u:{SECRET}\uff20lidarr:8686"

    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
        self.client = self.flask_app.test_client()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env_file = Path(tmp.name) / ".env"
        self.env_file.write_text(
            f"PLEX_URL={self.PLEX}\nAI_BASE_URL={self.AI}\nLIDARR_URL={self.LIDARR}\n"
            f"BEETS_WEB_URL=http://u:{SECRET}@saved:8337\n",
            encoding="utf-8",
        )
        self.module._SETUP_ENV_FILE = self.env_file
        self.module._ENV_EXAMPLE_FILE = Path(tmp.name) / ".env.example"
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for var in ("PLEX_URL", "AI_BASE_URL", "LIDARR_URL"):
            os.environ.pop(var, None)
        # Environment overrides a different saved value: status_message
        # used to quote the saved one.
        os.environ["BEETS_WEB_URL"] = f"http://u:{SECRET}@env:8337"

    def _variables(self):
        response = self.client.get("/api/setup/env")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(SECRET, response.get_data(as_text=True))
        return {v["name"]: v for v in response.get_json()["variables"]}

    def test_url_settings_are_returned_without_userinfo(self):
        variables = self._variables()
        self.assertEqual(variables["PLEX_URL"]["value"], "http://plex:32400")
        self.assertEqual(variables["PLEX_URL"]["saved_value"], "http://plex:32400")
        self.assertEqual(variables["AI_BASE_URL"]["effective_value"], "https://ai.example/v1")
        self.assertEqual(variables["BEETS_WEB_URL"]["value"], "http://env:8337")
        self.assertEqual(variables["BEETS_WEB_URL"]["saved_value"], "http://saved:8337")
        self.assertTrue(variables["BEETS_WEB_URL"]["is_overridden"])

    def test_saving_the_redacted_form_keeps_the_stored_value(self):
        variables = self._variables()
        echo = {name: variables[name]["value"] for name in ("PLEX_URL", "AI_BASE_URL", "LIDARR_URL")}
        response = self.client.post("/api/setup/env", json={"variables": echo})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertNotIn(SECRET, response.get_data(as_text=True))
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn(f"PLEX_URL={self.PLEX}", text)
        self.assertIn(f"AI_BASE_URL={self.AI}", text)
        self.assertIn(f"LIDARR_URL={self.LIDARR}", text)

    def test_a_real_change_is_still_saved(self):
        response = self.client.post("/api/setup/env", json={"variables": {"PLEX_URL": "http://plex2:32400"}})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertIn("PLEX_URL=http://plex2:32400", self.env_file.read_text(encoding="utf-8"))


    def test_env_save_refuses_userinfo_beets_web_url(self):
        for url in USERINFO_URLS:
            response = self.client.post("/api/setup/env", json={"variables": {"BEETS_WEB_URL": url}})
            self.assertEqual(response.status_code, 400, url)
            self.assertNotIn(SECRET, response.get_data(as_text=True))
            self.assertEqual(response.get_json()["error"], ba.BEETS_WEB_URL_USERINFO_MESSAGE)
        self.assertIn(f"BEETS_WEB_URL=http://u:{SECRET}@saved:8337", self.env_file.read_text(encoding="utf-8"))

    def test_saving_plain_beets_web_url_removes_the_credentials(self):
        # BEETS_WEB_URL is exempt from the keep-stored rule: the plain URL is
        # the remediation for a refused userinfo URL.
        response = self.client.post("/api/setup/env", json={"variables": {"BEETS_WEB_URL": "http://saved:8337"}})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn("BEETS_WEB_URL=http://saved:8337", text)
        self.assertNotIn(SECRET + "@saved", text)


class SetupSettingsUserinfoTests(unittest.TestCase):
    """QA F3: POST /api/setup/settings refuses a userinfo BEETS_WEB_URL at save."""

    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
        self.client = self.flask_app.test_client()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.module._SETTINGS_FILE = Path(tmp.name) / "app_settings.json"
        patch = mock.patch.dict(os.environ, {"WEB_MANAGER_DATA_DIR": tmp.name})
        patch.start()
        self.addCleanup(patch.stop)

    def test_userinfo_beets_web_url_is_refused(self):
        for url in USERINFO_URLS:
            response = self.client.post("/api/setup/settings", json={"BEETS_WEB_URL": url})
            self.assertEqual(response.status_code, 400, url)
            self.assertNotIn(SECRET, response.get_data(as_text=True))
            self.assertEqual(response.get_json()["code"], ba.BEETS_WEB_URL_USERINFO_CODE)
        self.assertNotIn(SECRET, self.client.get("/api/setup/settings").get_data(as_text=True))

    def test_plain_beets_web_url_is_saved(self):
        response = self.client.post("/api/setup/settings", json={"BEETS_WEB_URL": "http://beets:8337"})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))


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


# Adopted from QA (PR #247): plain-URL error branches and policy reasons.
PLAIN_URL = "http://beets:8337"


class PlainUrlErrorBranchTests(unittest.TestCase):
    def test_connection_and_timeout_branches_name_the_host(self):
        cases = [
            (urllib.error.URLError("refused"), lambda a: a.get_stats(), ba.BeetsAdapterConnectionError),
            (TimeoutError("timed out"), lambda a: a.get_stats(), ba.BeetsAdapterTimeoutError),
            (OSError("unreachable"), lambda a: a.get_stats(), ba.BeetsAdapterConnectionError),
            (urllib.error.URLError("refused"), lambda a: a.open_item_file(1), ba.BeetsAdapterConnectionError),
            (TimeoutError("timed out"), lambda a: a.open_album_art(1), ba.BeetsAdapterTimeoutError),
        ]
        adapter = ba.BeetsAdapter(base_url=PLAIN_URL)
        for exc, call, expected in cases:
            with mock.patch.object(ba.urllib.request, "urlopen", side_effect=exc):
                with self.assertRaises(expected) as ctx:
                    call(adapter)
            self.assertIn(PLAIN_URL, str(ctx.exception))

    def test_malformed_json_branch_still_reached(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.headers = {"Content-Type": "application/json"}
        resp.read.return_value = b"{not json"
        adapter = ba.BeetsAdapter(base_url=PLAIN_URL)
        with mock.patch.object(ba.urllib.request, "urlopen", return_value=resp):
            with self.assertRaises(ba.BeetsAdapterError) as ctx:
                adapter.get_stats()
        self.assertEqual(ctx.exception.error_code, "MALFORMED_RESPONSE")


class OutboundPolicyReasonTests(unittest.TestCase):
    """OutboundPolicyError subclasses ValueError; its fixed reason text (for
    example the response size limit) should stay in the adapter log."""

    def test_policy_reason_is_logged(self):
        adapter = ba.BeetsAdapter(base_url=PLAIN_URL)
        for call in (lambda a: a.get_stats(), lambda a: a.open_item_file(1)):
            exc = OutboundPolicyError("outbound response exceeded size limit")
            with mock.patch.object(ba.urllib.request, "urlopen", side_effect=exc), \
                    self.assertLogs("beets.adapter", level="WARNING") as logs:
                with self.assertRaises(ba.BeetsAdapterConnectionError):
                    call(adapter)
            self.assertIn("exceeded size limit", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
