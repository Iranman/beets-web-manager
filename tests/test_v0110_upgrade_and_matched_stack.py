"""Automated test suite for Beets Web Manager matched-stack upgrade & compatibility.

Migrated onto the stock-Beets architecture: diagnostics now come from the
webmanager integration plugin's handshake (BeetsAdapter.get_plugin_status())
plus a real local `verify_all_plugins()` report, never a remote control-agent
`get_status()` call. Covers:
1. ReplayGain backend capability integration (command backend / ffmpeg
   backend, disabled, missing, loader failed)
2. Integration-plugin protocol-version compatibility detection (replaces the
   deleted control-agent engine_release/control_api_version concept)
3. Existing user upgrade safety (credentials preserved, zero local SQLite DB
   ownership)
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
        self.beets_config_path = self.data_dir / "config.yaml"

        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "BEETS_WEB_PASSWORD": "",
                "BEETS_WEB_USERNAME": "",
                "BEETS_WEB_AUTH_TOKEN": "valid_test_bearer_token_32_chars_minimum!",
                "BEETS_WEB_AUTH_DISABLED": "0",
                "BEETS_WEB_MANAGER_VERSION": "0.1.10",
                "WEB_MANAGER_DATA_DIR": str(self.data_dir),
                "BEETS_CONFIG": str(self.beets_config_path),
            },
            clear=False,
        )
        self.env_patch.start()

        self.patches = [
            mock.patch.object(app_module, "WEB_MANAGER_DATA_DIR", self.data_dir),
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

    def _sample_plugin_status(self, **kwargs):
        base = {
            "protocol_version": "1.0",
            "plugin_version": "1.0.0",
            "beets_version": "2.13.1",
            "capabilities": ["import", "modify", "remove", "move", "operations", "status", "mbsubmit"],
            "loaded_plugins": ["musicbrainz", "chroma", "fetchart", "replaygain"],
            "library_ready": True,
            "upstream_web_readonly": True,
            "plugin_mutations_enabled": True,
        }
        base.update(kwargs)
        return base

    def _plugins_report(self, *, enabled_names=None, all_required_healthy=True, errors=None):
        if enabled_names is None:
            enabled_names = ["musicbrainz", "chroma", "fetchart", "replaygain"]
        plugins = [{"name": name, "enabled": True, "loaded": True, "healthy": True} for name in enabled_names]
        return {
            "ok": all_required_healthy,
            "all_required_healthy": all_required_healthy,
            "required_count": 1,
            "required_healthy_count": 1 if all_required_healthy else 0,
            "plugins": plugins,
            "categories": {"required": [], "optional": [], "integration": []},
            "summary": {"total": len(plugins), "healthy": len(plugins), "errors": errors or []},
        }

    def _diagnostics(self, *, plugin_status=None, plugins_report=None, config_text=""):
        self.beets_config_path.parent.mkdir(parents=True, exist_ok=True)
        self.beets_config_path.write_text(config_text, encoding="utf-8")
        plugin_status = plugin_status if plugin_status is not None else self._sample_plugin_status()
        plugins_report = plugins_report if plugins_report is not None else self._plugins_report()
        with mock.patch("backend.beets_adapter.beets_adapter.get_plugin_status", return_value=plugin_status), \
             mock.patch("backend.beets_adapter.beets_adapter.get_stats", return_value={"items": 0, "albums": 0}), \
             mock.patch("backend.beets_plugins.verify_all_plugins", return_value=plugins_report):
            return routes_setup._beets_plugin_diagnostics(self.beets_config_path)

    # -------------------------------------------------------------------------
    # ReplayGain Backend Integration Tests
    # -------------------------------------------------------------------------
    def test_replaygain_command_backend_reports_configured(self):
        """ReplayGain configured with backend: command / command: /usr/bin/mp3gain in config.yaml reports state=configured."""
        diag = self._diagnostics(config_text="replaygain:\n  backend: command\n  command: /usr/bin/mp3gain\n")
        rg = routes_setup._replaygain_integration_status(diag, "/usr/bin/ffmpeg")
        self.assertTrue(rg["configured"])
        self.assertEqual(rg["state"], "configured")
        self.assertIn("command backend (/usr/bin/mp3gain)", rg["note"])

    def test_replaygain_ffmpeg_backend_reports_configured(self):
        """ReplayGain configured with backend: ffmpeg reports state=configured."""
        diag = self._diagnostics(config_text="replaygain:\n  backend: ffmpeg\n")
        rg = routes_setup._replaygain_integration_status(diag, "/usr/bin/ffmpeg")
        self.assertTrue(rg["configured"])
        self.assertEqual(rg["state"], "configured")
        self.assertIn("ffmpeg backend", rg["note"])

    def test_replaygain_disabled_reports_installed_but_disabled(self):
        """ReplayGain not among the enabled plugins reports state=installed_but_disabled."""
        diag = self._diagnostics(
            plugins_report=self._plugins_report(enabled_names=["musicbrainz"]),
            config_text="",
        )
        rg = routes_setup._replaygain_integration_status(diag, "/usr/bin/ffmpeg")
        self.assertFalse(rg["configured"])
        self.assertEqual(rg["state"], "installed_but_disabled")

    def test_replaygain_not_loaded_reports_dependency_plugin_missing(self):
        """ReplayGain enabled but not loaded by stock Beets reports dependency_plugin_missing."""
        diag = self._diagnostics(
            plugin_status=self._sample_plugin_status(loaded_plugins=["musicbrainz"]),
            plugins_report=self._plugins_report(enabled_names=["musicbrainz", "replaygain"]),
            config_text="replaygain:\n  backend: command\n  command: /usr/bin/mp3gain\n",
        )
        rg = routes_setup._replaygain_integration_status(diag, "/usr/bin/ffmpeg")
        self.assertFalse(rg["configured"])
        self.assertEqual(rg["state"], "dependency_plugin_missing")

    def test_replaygain_loader_failed_reports_loader_failed(self):
        """Plugin loader failure (unrelated to replaygain specifically) reports plugin_loader_failed state."""
        diag = self._diagnostics(
            plugins_report=self._plugins_report(
                all_required_healthy=False,
                errors=["musicbrainz: error loading plugin"],
            ),
        )
        rg = routes_setup._replaygain_integration_status(diag, "/usr/bin/ffmpeg")
        self.assertFalse(rg["configured"])
        self.assertEqual(rg["state"], "plugin_loader_failed")

    # -------------------------------------------------------------------------
    # Integration Plugin Protocol-Version Compatibility Tests
    # -------------------------------------------------------------------------
    def test_version_compatibility_matched_release_passes(self):
        """A matched integration-plugin protocol version (1.0) reports compatible=True."""
        diag = self._diagnostics(plugin_status=self._sample_plugin_status(protocol_version="1.0"))
        compat = diag.get("engine_compatibility", {})
        self.assertTrue(compat.get("compatible"))
        self.assertEqual(compat.get("state"), "compatible")

    def test_version_compatibility_mismatch_detected(self):
        """A mismatched integration-plugin protocol version reports compatible=False with a diagnostic message."""
        with mock.patch("backend.beets_adapter.beets_adapter.get_plugin_status", return_value=self._sample_plugin_status(protocol_version="0.9")), \
             mock.patch("backend.beets_adapter.beets_adapter.get_stats", return_value={"items": 0, "albums": 0}), \
             mock.patch("backend.beets_plugins.verify_all_plugins", return_value=self._plugins_report()):
            self.beets_config_path.parent.mkdir(parents=True, exist_ok=True)
            self.beets_config_path.write_text("", encoding="utf-8")
            diag = routes_setup._beets_plugin_diagnostics(self.beets_config_path)
            compat = diag.get("engine_compatibility", {})
            self.assertFalse(compat.get("compatible"))
            self.assertEqual(compat.get("state"), "incompatible")
            self.assertIn("protocol version", compat.get("message", "").lower())

    # -------------------------------------------------------------------------
    # Existing User Upgrade & Database Preservation Tests
    # -------------------------------------------------------------------------
    def test_existing_v018_upgrade_preserves_credentials_and_state(self):
        """Upgrading an existing install preserves credentials without recreating the setup-complete marker."""
        self.initial_pwd_file.write_text("MyExistingAdminPassword123!Aa456", encoding="utf-8")
        app_module._migrate_or_initialize_setup_state()

        with mock.patch("backend.beets_adapter.beets_adapter.get_plugin_status", return_value=self._sample_plugin_status()), \
             mock.patch("backend.beets_adapter.beets_adapter.get_stats", return_value={"items": 0, "albums": 0}), \
             mock.patch("backend.beets_plugins.verify_all_plugins", return_value=self._plugins_report()):
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
