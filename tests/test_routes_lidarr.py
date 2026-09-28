import unittest
from pathlib import Path
try:  # ARCH-001: app.py module family (works under discovery and tests.<module> runs)
    from _app_ast_cache import app_family_source  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_ast_cache import app_family_source  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = app_family_source()
ROUTES_SOURCE = (ROOT / "routes_lidarr.py").read_text(encoding="utf-8")
GITIGNORE_SOURCE = (ROOT / ".gitignore").read_text(encoding="utf-8")


class LidarrRoutesStaticTests(unittest.TestCase):
    def test_route_module_is_loaded_and_tracked(self):
        self.assertIn('"routes_lidarr"', APP_SOURCE)  # listed in app.ROUTE_MODULES (ARCH-001)
        self.assertTrue((ROOT / "routes_lidarr.py").exists())

    def test_expected_frontend_routes_exist(self):
        self.assertIn('@app.get("/api/wanted/lidarr")', ROUTES_SOURCE)
        self.assertIn('@app.get("/api/lidarr/artist-albums-by-name")', ROUTES_SOURCE)
        self.assertIn('@app.post("/api/lidarr/command")', ROUTES_SOURCE)

    def test_lidarr_command_is_allowlisted(self):
        self.assertIn('name != "AlbumSearch"', ROUTES_SOURCE)
        self.assertIn('"unsupported Lidarr command"', ROUTES_SOURCE)
        self.assertIn('value > 0', ROUTES_SOURCE)

    def test_lidarr_api_key_is_not_returned(self):
        self.assertIn('"X-Api-Key": LIDARR_KEY', ROUTES_SOURCE)
        for line in ROUTES_SOURCE.splitlines():
            if "jsonify(" in line:
                self.assertNotIn("LIDARR_KEY", line)

    def test_local_playwright_mcp_state_is_ignored(self):
        self.assertIn(".playwright-mcp/", GITIGNORE_SOURCE)


if __name__ == "__main__":
    unittest.main()
