"""Tests for routes_setup.py (first-run setup wizard API).

routes_setup imports `from app import app`, which would otherwise require
booting the full app.py (beets Library, all route modules, etc.). Instead we
stub sys.modules['app'] with a minimal Flask app before importing
routes_setup, matching how a real Flask blueprint would be exercised without
the rest of the application's side effects.
"""
import importlib
import sys
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
        for key in ("ai", "musicbrainz", "acoustid", "plex"):
            self.assertIn(key, body["integrations"])

    def test_status_masks_secret_looking_settings(self):
        self.client.post("/api/setup/settings", json={"ai_api_key": "sk-verysecretvalue123"})
        r = self.client.get("/api/setup/status")
        settings = r.get_json()["settings"]
        self.assertNotIn("verysecretvalue123", str(settings))


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
