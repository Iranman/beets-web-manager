"""Tests for routes_setup.py (first-run setup wizard API).

routes_setup imports `from app import app`, which would otherwise require
booting the full app.py (beets Library, all route modules, etc.). Instead we
stub sys.modules['app'] with a minimal Flask app before importing
routes_setup, matching how a real Flask blueprint would be exercised without
the rest of the application's side effects.
"""
import importlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _load_routes_setup_against_stub_app():
    """(Re)import routes_setup against a fresh stub `app` module so each test
    gets independent route registration state."""
    from flask import Flask
    stub = types.ModuleType("app")
    stub.app = Flask(__name__)
    sys.modules["app"] = stub
    sys.modules.pop("routes_setup", None)
    module = importlib.import_module("routes_setup")
    return stub.app, module


class RoutesSetupHealthTests(unittest.TestCase):
    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app()
        self.client = self.flask_app.test_client()

    def test_health_live_reports_alive_and_version(self):
        r = self.client.get("/health/live")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["status"], "alive")
        self.assertIn("version", body)

    def test_health_root_is_alias_for_live(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], "alive")

    def test_health_ready_reports_blocking_reasons_when_not_ready(self):
        r = self.client.get("/health/ready")
        # In a bare test environment /config and beets config won't exist,
        # so this must report 503/warning, never crash.
        self.assertIn(r.status_code, (200, 503))
        body = r.get_json()
        self.assertIn(body["status"], ("ready", "warning"))
        self.assertIsInstance(body["blocking_reasons"], list)


class RoutesSetupStatusTests(unittest.TestCase):
    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app()
        self.client = self.flask_app.test_client()

    def test_status_never_crashes_on_missing_paths(self):
        r = self.client.get("/api/setup/status")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertIn(body["status"], ("ready", "warning"))
        self.assertIn("integrations", body)
        for key in ("ai", "musicbrainz", "acoustid", "discogs", "lastgenre", "listenbrainz", "discpath", "replaygain", "plex", "lidarr", "slskd"):
            self.assertIn(key, body["integrations"])
            self.assertIn("state", body["integrations"][key])
        self.assertIn("beets", body)
        self.assertIn("plugin_failures", body["beets"])

    def test_status_masks_secret_looking_settings(self):
        self.client.post("/api/setup/settings", json={"ai_api_key": "sk-verysecretvalue123"})
        r = self.client.get("/api/setup/status")
        settings = r.get_json()["settings"]
        self.assertNotIn("verysecretvalue123", str(settings))

    def test_status_reports_demo_mode_flag(self):
        r = self.client.get("/api/setup/status")
        self.assertIn("demo_mode", r.get_json())
        self.assertFalse(r.get_json()["demo_mode"])  # not set in test env

    def test_status_reports_demo_mode_true_when_env_set(self):
        import os
        os.environ["DEMO_MODE"] = "1"
        try:
            r = self.client.get("/api/setup/status")
            self.assertTrue(r.get_json()["demo_mode"])
        finally:
            del os.environ["DEMO_MODE"]


class RoutesSetupTestConnectionTests(unittest.TestCase):
    """These hit the /api/setup/test/* endpoints without real credentials —
    verifying they degrade to a clear not_configured/failed response rather
    than crashing or reporting false success."""

    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app()
        self.client = self.flask_app.test_client()

    def test_ai_test_without_key_reports_not_configured(self):
        r = self.client.post("/api/setup/test/ai", json={})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["status"], "not_configured")

    def test_plex_test_without_credentials_reports_not_configured(self):
        r = self.client.post("/api/setup/test/plex", json={})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["status"], "not_configured")

    def test_acoustid_test_reports_fpcalc_availability_explicitly(self):
        r = self.client.post("/api/setup/test/acoustid", json={})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertIn("fpcalc_available", body)


class RoutesSetupSettingsPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app()
        self.client = self.flask_app.test_client()
        # Module defaults point at /config/*, which isn't writable (or may
        # not even exist) outside the real container — isolate to a temp
        # dir so this test doesn't depend on host filesystem layout.
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.module._SETTINGS_FILE = root / "app_settings.json"
        self.module._SETUP_COMPLETE_MARKER = root / ".setup_complete"
        self.addCleanup(self.tempdir.cleanup)
        self.addCleanup(self._cleanup_settings_file)

    def _cleanup_settings_file(self):
        try:
            self.module._SETTINGS_FILE.unlink(missing_ok=True)
            self.module._SETUP_COMPLETE_MARKER.unlink(missing_ok=True)
        except Exception:
            pass

    def test_save_and_read_settings_round_trip(self):
        r = self.client.post("/api/setup/settings", json={"ai_model": "gpt-4o-mini"})
        self.assertTrue(r.get_json()["ok"])
        r = self.client.get("/api/setup/settings")
        self.assertEqual(r.get_json()["settings"]["ai_model"], "gpt-4o-mini")

    def test_settings_rejects_non_object_payload(self):
        r = self.client.post("/api/setup/settings", json=["not", "an", "object"])
        self.assertEqual(r.status_code, 400)

    def test_complete_marker_is_idempotent(self):
        r1 = self.client.post("/api/setup/complete")
        r2 = self.client.post("/api/setup/complete")
        self.assertTrue(r1.get_json()["ok"])
        self.assertTrue(r2.get_json()["ok"])
        self.assertTrue(self.module._SETUP_COMPLETE_MARKER.exists())


class RoutesSetupEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app()
        self.client = self.flask_app.test_client()
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.env_file = root / ".env"
        self.example_file = root / ".env.example"
        self.example_file.write_text(
            "# Plex and Arr services\n"
            "PLEX_URL=\n"
            "PLEX_TOKEN=\n"
            "LIDARR_API_KEY=\n"
            "\n"
            "# Demo mode\n"
            "DEMO_MODE=0\n",
            encoding="utf-8",
        )
        self.module._SETUP_ENV_FILE = self.env_file
        self.module._ENV_EXAMPLE_FILE = self.example_file
        self._saved_env = {
            name: os.environ.get(name)
            for name in ("PLEX_URL", "PLEX_TOKEN", "LIDARR_API_KEY", "DEMO_MODE")
        }
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.tempdir.cleanup()
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_env_get_masks_secret_values(self):
        self.env_file.write_text("PLEX_URL=http://plex:32400\nPLEX_TOKEN=supersecretvalue\n", encoding="utf-8")
        r = self.client.get("/api/setup/env")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        variables = {item["name"]: item for item in body["variables"]}
        self.assertEqual(variables["PLEX_URL"]["value"], "http://plex:32400")
        self.assertTrue(variables["PLEX_TOKEN"]["has_value"])
        self.assertNotIn("supersecretvalue", str(body))

    def test_env_save_updates_file_and_applies_process_env(self):
        self.env_file.write_text("PLEX_URL=http://old:32400\nPLEX_TOKEN=oldsecretvalue\n", encoding="utf-8")
        r = self.client.post("/api/setup/env", json={
            "variables": {
                "PLEX_URL": "http://new:32400",
                "PLEX_TOKEN": "",
            },
        })
        self.assertEqual(r.status_code, 200)
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn("PLEX_URL=http://new:32400", text)
        self.assertIn("PLEX_TOKEN=oldsecretvalue", text)
        self.assertEqual(os.environ["PLEX_URL"], "http://new:32400")
        self.assertTrue(r.get_json()["backup_path"])

    def test_env_save_can_clear_secret_explicitly(self):
        self.env_file.write_text("PLEX_TOKEN=oldsecretvalue\n", encoding="utf-8")
        r = self.client.post("/api/setup/env", json={
            "variables": {"PLEX_TOKEN": ""},
            "clear": ["PLEX_TOKEN"],
        })
        self.assertEqual(r.status_code, 200)
        self.assertIn("PLEX_TOKEN=\n", self.env_file.read_text(encoding="utf-8"))
        self.assertEqual(os.environ["PLEX_TOKEN"], "")

    def test_env_save_rejects_unlisted_variable(self):
        r = self.client.post("/api/setup/env", json={"variables": {"PYTHONPATH": "x"}})
        self.assertEqual(r.status_code, 400)


class RoutesSetupHelperTests(unittest.TestCase):
    def setUp(self):
        _, self.module = _load_routes_setup_against_stub_app()

    def test_mask_short_value(self):
        self.assertEqual(self.module._mask("ab"), "**")

    def test_mask_long_value_keeps_edges(self):
        masked = self.module._mask("sk-1234567890")
        self.assertTrue(masked.startswith("sk"))
        self.assertTrue(masked.endswith("90"))
        self.assertNotIn("1234567890"[:6], masked)

    def test_mask_empty_value(self):
        self.assertEqual(self.module._mask(""), "")

    def test_check_path_reports_missing_configured_path(self):
        result = self.module._check_path("", require_writable=True)
        self.assertFalse(result["exists"])
        self.assertEqual(result["error"], "not configured")


if __name__ == "__main__":
    unittest.main()
