"""#208 root cause: a BEETS_WEB_URL with user:pass@ never authenticated
(urllib sends no Basic header and resolves "u:pw@host" as the host), and a
URL without a port raised an uncaught http.client.InvalidURL whose message
held the password. Such a URL is now refused at adapter construction and in
setup status, and HTTPException/ValueError from urllib are reported with the
redacted URL only. Each test fails on main."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.beets_adapter as ba

try:
    from test_routes_setup import _load_routes_setup_against_stub_app
except ImportError:  # pragma: no cover
    from tests.test_routes_setup import _load_routes_setup_against_stub_app

CANARY = "zz-canary-208"  # non-secret marker; must never appear in output
USERINFO_URLS = (
    f"http://u:{CANARY}@beets:8337",          # with port
    f"http://u:{CANARY}@beets",               # no port (was InvalidURL)
    "http://u:zz%40canary-208@beets:8337",   # percent-encoded userinfo
    f"http://u:{CANARY}/x@beets:8337",        # "/" hides the "@" from urlsplit
    f"http://u:{CANARY}\uff20beets:8337",     # fullwidth @ (NFKC -> "@")
    f"http://u%3A{CANARY}%40beets:8337",      # whole userinfo percent-encoded
)


def _calls():
    return (
        lambda a: a.get_stats(),
        lambda a: a.get_plugin_status(),
        lambda a: a.open_item_file(1),
        lambda a: a.open_album_art(1),
    )


class SetupEnvUrlRedactionTests(unittest.TestCase):
    """#183 F1: GET /api/setup/env never returns user:pass@ of a URL setting,
    and saving the form back does not overwrite the stored value."""

    PLEX = f"http://u:{CANARY}@plex:32400"
    AI = f"https://u:{CANARY}@ai.example/v1"
    LIDARR = f"http://u:{CANARY}\uff20lidarr:8686"

    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
        self.client = self.flask_app.test_client()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env_file = Path(tmp.name) / ".env"
        self.env_file.write_text(
            f"PLEX_URL={self.PLEX}\nAI_BASE_URL={self.AI}\nLIDARR_URL={self.LIDARR}\n"
            f"BEETS_WEB_URL=http://u:{CANARY}@saved:8337\n",
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
        os.environ["BEETS_WEB_URL"] = f"http://u:{CANARY}@env:8337"

    def _variables(self):
        response = self.client.get("/api/setup/env")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(CANARY, response.get_data(as_text=True))
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
        self.assertNotIn(CANARY, response.get_data(as_text=True))
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
            self.assertNotIn(CANARY, response.get_data(as_text=True))
            self.assertEqual(response.get_json()["error"], ba.BEETS_WEB_URL_USERINFO_MESSAGE)
            self.assertEqual(response.get_json()["code"], ba.BEETS_WEB_URL_USERINFO_CODE)
        self.assertIn(f"BEETS_WEB_URL=http://u:{CANARY}@saved:8337", self.env_file.read_text(encoding="utf-8"))

    def test_saving_plain_beets_web_url_removes_the_credentials(self):
        # BEETS_WEB_URL is exempt from the keep-stored rule: the plain URL is
        # the remediation for a refused userinfo URL.
        response = self.client.post("/api/setup/env", json={"variables": {"BEETS_WEB_URL": "http://saved:8337"}})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn("BEETS_WEB_URL=http://saved:8337", text)
        self.assertNotIn(CANARY + "@saved", text)


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
            self.assertNotIn(CANARY, response.get_data(as_text=True))
            self.assertEqual(response.get_json()["code"], ba.BEETS_WEB_URL_USERINFO_CODE)
        self.assertNotIn(CANARY, self.client.get("/api/setup/settings").get_data(as_text=True))

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
            self.assertNotIn(CANARY, text, url)
            self.assertNotIn("canary-208", text, url)
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
