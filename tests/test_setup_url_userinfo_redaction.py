"""#183 F1 / #208: URL settings never come back from GET /api/setup/env with
user:pass@ in any field, and saving the redacted form back keeps the stored
value. (BEETS_WEB_URL, where these tests started, went away with the
separate Beets container.)"""
import os
import tempfile
import unittest
from pathlib import Path

try:
    from test_routes_setup import _load_routes_setup_against_stub_app
except ImportError:  # pragma: no cover
    from tests.test_routes_setup import _load_routes_setup_against_stub_app

CANARY = "zz-canary-208"  # non-secret marker; must never appear in output


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
            f"SLSKD_URL=http://u:{CANARY}@saved:5030\n",
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
        os.environ["SLSKD_URL"] = f"http://u:{CANARY}@env:5030"

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
        self.assertEqual(variables["SLSKD_URL"]["value"], "http://env:5030")
        self.assertEqual(variables["SLSKD_URL"]["saved_value"], "http://saved:5030")
        self.assertTrue(variables["SLSKD_URL"]["is_overridden"])

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


if __name__ == "__main__":
    unittest.main()
