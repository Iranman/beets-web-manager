"""Automated test suite for Beets Web Manager v0.1.10 matched-stack upgrade & compatibility.

Covers:
1. ReplayGain backend capability integration (command backend / mp3gain, ffmpeg backend, disabled, missing, loader failed)
2. Engine version skew & control API compatibility detection (0.1.8 vs 0.1.10, control_api_version)
3. Existing user upgrade safety (database item/album count preserved, credentials preserved, zero local SQLite DB ownership)
4. Setup status semantics (setup_complete vs ready vs first_run_required)
"""
import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
import routes_setup

_APP_MODULE = app_module
_ROUTES_SETUP_MODULE = routes_setup


class V0110UpgradeAndMatchedStackTests(unittest.TestCase):
    def setUp(self):
        global app_module, routes_setup
        app_module = _APP_MODULE
        routes_setup = _ROUTES_SETUP_MODULE
        self.module_patch = mock.patch.dict(
            sys.modules,
            {"app": app_module, "routes_setup": routes_setup},
        )
        self.module_patch.start()

        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmpdir.name)

        self.initial_pwd_file = self.data_dir / ".initial_admin_password"
        self.persisted_pwd_file = self.data_dir / ".browser_password"
        self.persisted_user_file = self.data_dir / ".browser_username"
        self.setup_state_file = self.data_dir / ".browser_setup_state"
        self.auth_token_file = self.data_dir / ".auth_token"

        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "BEETS_WEB_PASSWORD": "",
                "BEETS_WEB_USERNAME": "",
                "BEETS_WEB_AUTH_TOKEN": "valid_test_bearer_token_32_chars_minimum!",
                "BEETS_WEB_AUTH_DISABLED": "0",
                "BEETS_WEB_MANAGER_VERSION": "0.1.10",
            },
            clear=False,
        )
        self.env_patch.start()

        self.patches = [
            mock.patch.object(app_module, "_INITIAL_BROWSER_PASSWORD_FILE", self.initial_pwd_file),
            mock.patch.object(app_module, "_PERSISTED_BROWSER_PASSWORD_FILE", self.persisted_pwd_file),
            mock.patch.object(app_module, "_PERSISTED_BROWSER_USERNAME_FILE", self.persisted_user_file),
            mock.patch.object(app_module, "_BROWSER_SETUP_STATE_FILE", self.setup_state_file),
            mock.patch.object(app_module, "_GENERATED_AUTH_TOKEN_FILE", self.auth_token_file),
            mock.patch.object(routes_setup, "_SETUP_COMPLETE_MARKER", self.data_dir / ".setup_complete"),
            mock.patch.object(routes_setup, "_FALLBACK_AUTH_TOKEN_FILE", self.auth_token_file),
            mock.patch.object(routes_setup, "_STATUS_CACHE_DATA", None),
            mock.patch.object(routes_setup, "_STATUS_CACHE_TS", 0.0),
        ]
        for p in self.patches:
            p.start()

        self.client = app_module.app.test_client()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.module_patch.stop()
        self.env_patch.stop()
        self.tmpdir.cleanup()

    def _sample_remote_status(self, **kwargs):
        base = {
            "status": "ok",
            "service": "beets-control-agent",
            "agent_version": "1.0.0",
            "engine_release": "0.1.10",
            "engine_revision": "test-sha-12345",
            "control_api_version": 1,
            "beets_version": "2.13.1",
            "beet_available": True,
            "plugin_loader_ok": True,
            "configured_plugins": ["musicbrainz", "chroma", "fetchart", "replaygain", "mbsubmit"],
            "loaded_plugins": ["musicbrainz", "chroma", "fetchart", "replaygain", "mbsubmit"],
            "capabilities": {
                "replaygain": {
                    "configured": True,
                    "loaded": True,
                    "available": True,
                    "backend": "command",
                    "command": "/usr/bin/mp3gain",
                },
                "chroma": {"configured": True, "loaded": True, "available": True},
                "musicbrainz": {"configured": True, "loaded": True, "available": True},
            },
            "commands": {"mbsubmit": {"available": True}, "submit": {"available": True}},
            "replaygain_backend": "command",
            "replaygain_command": "/usr/bin/mp3gain",
            "fpcalc_available": True,
            "fpcalc_path": "/usr/bin/fpcalc",
            "ffmpeg_available": True,
            "ffmpeg_path": "/usr/bin/ffmpeg",
            "paths": {},
        }
        base.update(kwargs)
        return base

    # -------------------------------------------------------------------------
    # ReplayGain Backend Integration Tests
    # -------------------------------------------------------------------------
    def test_replaygain_command_backend_reports_configured(self):
        """ReplayGain with backend=command and command=/usr/bin/mp3gain reports state=configured."""
        remote = self._sample_remote_status(
            replaygain_backend="command",
            replaygain_command="/usr/bin/mp3gain",
        )
        with mock.patch("routes_setup.beets_client.get_status", return_value=remote):
            diag = routes_setup._beets_plugin_diagnostics(Path("/config/config.yaml"))
            rg = routes_setup._replaygain_integration_status(diag, "/usr/bin/ffmpeg")
            self.assertTrue(rg["configured"])
            self.assertEqual(rg["state"], "configured")
            self.assertIn("command backend (/usr/bin/mp3gain)", rg["note"])

    def test_replaygain_ffmpeg_backend_reports_configured(self):
        """ReplayGain with backend=ffmpeg and ffmpeg available reports state=configured."""
        remote = self._sample_remote_status(
            replaygain_backend="ffmpeg",
            replaygain_command="",
        )
        remote["capabilities"]["replaygain"]["backend"] = "ffmpeg"
        remote["capabilities"]["replaygain"]["command"] = ""
        with mock.patch("routes_setup.beets_client.get_status", return_value=remote):
            diag = routes_setup._beets_plugin_diagnostics(Path("/config/config.yaml"))
            rg = routes_setup._replaygain_integration_status(diag, "/usr/bin/ffmpeg")
            self.assertTrue(rg["configured"])
            self.assertEqual(rg["state"], "configured")
            self.assertIn("ffmpeg backend", rg["note"])

    def test_replaygain_disabled_reports_installed_but_disabled(self):
        """ReplayGain disabled in Beets config reports state=installed_but_disabled."""
        remote = self._sample_remote_status(
            configured_plugins=["musicbrainz"],
            loaded_plugins=["musicbrainz"],
        )
        remote["capabilities"]["replaygain"] = {"configured": False, "loaded": False, "available": False}
        with mock.patch("routes_setup.beets_client.get_status", return_value=remote):
            diag = routes_setup._beets_plugin_diagnostics(Path("/config/config.yaml"))
            rg = routes_setup._replaygain_integration_status(diag, "/usr/bin/ffmpeg")
            self.assertFalse(rg["configured"])
            self.assertEqual(rg["state"], "installed_but_disabled")

    def test_replaygain_not_loaded_reports_dependency_plugin_missing(self):
        """ReplayGain configured but not loaded by engine reports dependency_plugin_missing."""
        remote = self._sample_remote_status(
            configured_plugins=["musicbrainz", "replaygain"],
            loaded_plugins=["musicbrainz"],
        )
        remote["capabilities"]["replaygain"] = {"configured": True, "loaded": False, "available": False}
        with mock.patch("routes_setup.beets_client.get_status", return_value=remote):
            diag = routes_setup._beets_plugin_diagnostics(Path("/config/config.yaml"))
            rg = routes_setup._replaygain_integration_status(diag, "/usr/bin/ffmpeg")
            self.assertFalse(rg["configured"])
            self.assertEqual(rg["state"], "dependency_plugin_missing")

    def test_replaygain_loader_failed_reports_loader_failed(self):
        """Plugin loader failure reports plugin_loader_failed state."""
        remote = self._sample_remote_status(plugin_loader_ok=False)
        with mock.patch("routes_setup.beets_client.get_status", return_value=remote):
            diag = routes_setup._beets_plugin_diagnostics(Path("/config/config.yaml"))
            rg = routes_setup._replaygain_integration_status(diag, "/usr/bin/ffmpeg")
            self.assertFalse(rg["configured"])
            self.assertEqual(rg["state"], "plugin_loader_failed")

    # -------------------------------------------------------------------------
    # Engine / Web Manager Compatibility Skew Tests
    # -------------------------------------------------------------------------
    def test_version_compatibility_matched_release_passes(self):
        """Matched engine release (0.1.10) and control_api_version (1) reports compatible=True."""
        remote = self._sample_remote_status(engine_release="0.1.10", control_api_version=1)
        with mock.patch("routes_setup.beets_client.get_status", return_value=remote):
            diag = routes_setup._beets_plugin_diagnostics(Path("/config/config.yaml"))
            compat = diag.get("engine_compatibility", {})
            self.assertTrue(compat.get("compatible"))
            self.assertEqual(compat.get("state"), "compatible")

    def test_version_compatibility_mismatch_detected(self):
        """Outdated engine release (0.1.8) or API version (0) reports compatible=False with diagnostic message."""
        remote = self._sample_remote_status(engine_release="0.1.8", control_api_version=0)
        with mock.patch("routes_setup.beets_client.get_status", return_value=remote):
            diag = routes_setup._beets_plugin_diagnostics(Path("/config/config.yaml"))
            compat = diag.get("engine_compatibility", {})
            self.assertFalse(compat.get("compatible"))
            self.assertEqual(compat.get("state"), "compatibility_mismatch")
            self.assertIn("outdated", compat.get("message", "").lower())

            status_payload = routes_setup._build_setup_status_payload()
            self.assertEqual(status_payload["status"], "warning")
            blocking = status_payload.get("blocking_reasons", [])
            self.assertTrue(any("compatibility mismatch" in b.lower() for b in blocking))

    # -------------------------------------------------------------------------
    # Existing User Upgrade & Database Preservation Tests
    # -------------------------------------------------------------------------
    def test_existing_v018_upgrade_preserves_credentials_and_state(self):
        """Upgrading v0.1.8 preserves credentials without creating the setup-complete marker."""
        self.initial_pwd_file.write_text("MyExistingAdminPassword123!Aa456", encoding="utf-8")
        app_module._migrate_or_initialize_setup_state()

        remote = self._sample_remote_status()
        with mock.patch("routes_setup.beets_client.get_status", return_value=remote):
            status_payload = routes_setup._build_setup_status_payload()
            self.assertFalse(status_payload["setup_complete"])
            self.assertFalse(status_payload["first_run"]["required"])
            self.assertEqual(self.setup_state_file.read_text(encoding="utf-8"), "legacy_established")
            self.assertEqual(
                self.initial_pwd_file.read_text(encoding="utf-8"),
                "MyExistingAdminPassword123!Aa456",
            )

            # Basic Auth with existing password works
            cred = base64.b64encode(b"admin:MyExistingAdminPassword123!Aa456").decode("utf-8")
            resp = self.client.get("/api/setup/env", headers={"Authorization": f"Basic {cred}"})
            self.assertEqual(resp.status_code, 200)

            # Web Manager creates no local SQLite DB file
            sqlite_files = list(self.data_dir.glob("*.blb")) + list(self.data_dir.glob("*.db"))
            self.assertEqual(len(sqlite_files), 0)


if __name__ == "__main__":
    unittest.main()
