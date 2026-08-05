"""Tests verifying that Beets Web Manager never creates or queries a local Beets SQLite database."""
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
COMPOSE_ARRS_SOURCE = (ROOT / "docker-compose.arrs.yml").read_text(encoding="utf-8")
COMPOSE_MAIN_SOURCE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")


class NoLocalBeetsDatabaseTests(unittest.TestCase):
    def test_app_py_has_no_direct_sqlite3_connect_calls(self):
        """Web manager must route all library queries via RemoteSQLiteConnection / BeetsClient."""
        self.assertNotIn("sqlite3.connect(LIB_PATH)", APP_SOURCE)
        self.assertNotIn("sqlite3.connect('/config/musiclibrary.blb')", APP_SOURCE)
        self.assertNotIn('sqlite3.connect("/config/musiclibrary.blb")', APP_SOURCE)

    def test_lib_path_default_does_not_point_to_config_musiclibrary(self):
        """LIB_PATH default in app.py must not point to /config/musiclibrary.blb."""
        self.assertNotIn('LIB_PATH  = os.environ.get("BEETS_LIBRARY", "/config/musiclibrary.blb")', APP_SOURCE)

    def test_production_compose_does_not_mount_config_into_web_manager(self):
        """Web manager service in production compose must not mount engine's /config directory."""
        # Find beets-web-manager block in docker-compose.yml
        web_manager_block = COMPOSE_MAIN_SOURCE.split("beets-web-manager:")[1]
        self.assertNotIn(":/config", web_manager_block)

    def test_beets_unavailable_returns_503(self):
        """When Beets control agent is unavailable, routes return 503 rather than attempting local DB fallback."""
        import app as app_module
        from backend.beets_client import BeetsUnavailableError

        with mock.patch.dict("os.environ", {"BEETS_WEB_AUTH_DISABLED": "1"}):
            with app_module.app.test_client() as client:
                with mock.patch.object(app_module.beets_client, "get_items_page", side_effect=BeetsUnavailableError("Unavailable")):
                    res = client.get("/api/library?limit=1")
                    self.assertEqual(res.status_code, 503)
                    data = res.get_json()
                    self.assertEqual(data.get("status"), "unavailable")


if __name__ == "__main__":
    unittest.main()
