"""Tests for routes_setup.py (first-run setup wizard API).

routes_setup imports `from app import app`, which would otherwise require
booting the full app.py (beets Library, all route modules, etc.). Instead we
stub sys.modules['app'] with a minimal Flask app before importing
routes_setup, matching how a real Flask blueprint would be exercised without
the rest of the application's side effects.
"""
import importlib
import os
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock as mock
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

    def test_status_sanitizes_beets_diagnostic_exceptions(self):
        sensitive = "/database/internal/path token=super-secret-key Traceback... File \"secret.py\", line 7"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            config_dir.mkdir()
            config_file = config_dir / "config.yaml"
            config_file.write_text("plugins: musicbrainz\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"BEETSDIR": str(config_dir), "BEETS_CONFIG": str(config_file)}, clear=False), \
                 mock.patch.object(self.module, "_beet_binary", return_value=(True, "beet")), \
                 mock.patch.object(self.module.subprocess, "run", side_effect=RuntimeError(sensitive)):
                response = self.client.get("/api/setup/status")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["beets"]["diagnostic_error"], "Beets diagnostic command failed.")
        text = response.get_data(as_text=True)
        for forbidden in ("super-secret-key", "/database/internal/path", "Traceback", 'File "', "line 7"):
            self.assertNotIn(forbidden, text)

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


class RoutesSetupPluginLoaderDiagnosticsTests(unittest.TestCase):
    """Behavioral coverage for the supported Beets plugin-loader diagnostic
    (`beet -c <config> -vv version`) that replaced the unsupported
    `beet plugins` command, and for the strengthened diagnostic redaction.
    These mock only the subprocess boundary (`subprocess.run`) and exercise
    the real `_beets_plugin_diagnostics`/`_redact_diagnostic_text` helpers
    and the real `/api/setup/status` route -- not source-string assertions.
    """

    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app()
        self.client = self.flask_app.test_client()
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.config_dir = root / "config"
        self.config_dir.mkdir()
        self.config_file = self.config_dir / "config.yaml"

    def tearDown(self):
        self._tmp.cleanup()

    def _write_config(self, plugins="musicbrainz lastgenre listenbrainz discpath replaygain chroma"):
        self.config_file.write_text(
            f"plugins: {plugins}\n"
            "pluginpath:\n  - /config/beetsplug\n  - /app/beetsplug\n"
            "replaygain:\n    backend: ffmpeg\n"
            "listenbrainz:\n    token: some-listenbrainz-token\n",
            encoding="utf-8",
        )

    def _env(self):
        return mock.patch.dict(
            os.environ,
            {"BEETSDIR": str(self.config_dir), "BEETS_CONFIG": str(self.config_file)},
            clear=False,
        )

    # -- 1. Successful loader -------------------------------------------------
    def test_successful_loader_reports_configured_plugins_and_no_false_failures(self):
        self._write_config()
        fake_proc = mock.Mock(
            returncode=0,
            stdout=(
                "beets version 2.12.0\n"
                "Python version 3.12.0\n"
                "plugins: musicbrainz, lastgenre, listenbrainz, discpath, replaygain, chroma\n"
            ),
            stderr="",
        )
        with self._env(), \
             mock.patch.object(self.module, "_beet_binary", return_value=(True, "beet")), \
             mock.patch.object(self.module.subprocess, "run", return_value=fake_proc), \
             mock.patch.object(
                 self.module.shutil, "which",
                 side_effect=lambda name: f"/usr/bin/{name}" if name in ("fpcalc", "ffmpeg") else None,
             ):
            response = self.client.get("/api/setup/status")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        beets = body["beets"]
        self.assertTrue(beets["available"])
        self.assertTrue(beets["plugin_loader_ok"])
        self.assertFalse(beets["plugin_loader_timed_out"])
        self.assertEqual(beets["plugins_returncode"], 0)
        self.assertEqual(beets["plugin_failures"], [])
        for plugin in ("musicbrainz", "lastgenre", "listenbrainz", "discpath", "replaygain", "chroma"):
            self.assertIn(plugin, beets["configured_plugins"])
        for key in ("musicbrainz", "lastgenre", "listenbrainz", "discpath", "replaygain"):
            self.assertNotEqual(body["integrations"][key]["state"], "plugin_loader_failed")
        self.assertEqual(body["integrations"]["musicbrainz"]["state"], "configured")
        self.assertEqual(body["integrations"]["lastgenre"]["state"], "configured")
        self.assertEqual(body["integrations"]["discpath"]["state"], "configured")
        self.assertEqual(body["integrations"]["replaygain"]["state"], "configured")
        self.assertEqual(body["integrations"]["listenbrainz"]["state"], "configured")

    # -- 2. Unsupported-command regression -------------------------------------
    def test_loader_never_invokes_unsupported_plugins_command(self):
        self._write_config()
        captured_cmds = []

        def fake_run(cmd, **kwargs):
            captured_cmds.append(list(cmd))
            return mock.Mock(returncode=0, stdout="beets version 2.12.0\n", stderr="")

        with self._env(), \
             mock.patch.object(self.module, "_beet_binary", return_value=(True, "beet")), \
             mock.patch.object(self.module.subprocess, "run", side_effect=fake_run):
            self.client.get("/api/setup/status")

        self.assertTrue(captured_cmds)
        for cmd in captured_cmds:
            self.assertNotIn("plugins", cmd)
        self.assertEqual(
            captured_cmds[0],
            ["beet", "-c", str(self.config_file)] + list(self.module._BEET_LOADER_PROBE_ARGS),
        )

    # -- 3. Nonzero loader result ----------------------------------------------
    def test_nonzero_loader_result_marks_setup_warning_without_false_healthy_plugins(self):
        self._write_config()
        fake_proc = mock.Mock(returncode=1, stdout="", stderr="beet: error: unrecognized arguments: -vv\n")
        with self._env(), \
             mock.patch.object(self.module, "_beet_binary", return_value=(True, "beet")), \
             mock.patch.object(self.module.subprocess, "run", return_value=fake_proc):
            response = self.client.get("/api/setup/status")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["status"], "warning")
        self.assertFalse(body["beets"]["plugin_loader_ok"])
        self.assertTrue(body["beets"]["plugin_loader_error"])
        self.assertEqual(body["beets"]["configured_plugins"], [])
        self.assertEqual(body["integrations"]["musicbrainz"]["state"], "plugin_loader_failed")
        for key in ("lastgenre", "listenbrainz", "discpath", "replaygain"):
            self.assertNotEqual(body["integrations"][key]["state"], "configured")
        self.assertIn(
            "Beets plugin loader did not complete successfully",
            " ".join(body["blocking_reasons"]),
        )

    # -- 4. Plugin import failure -----------------------------------------------
    def test_plugin_import_failure_is_dependency_plugin_missing_without_crash(self):
        self._write_config()
        fake_proc = mock.Mock(
            returncode=1,
            stdout="",
            stderr="** error loading plugin lastgenre\nModuleNotFoundError: No module named 'pylast'\n",
        )
        with self._env(), \
             mock.patch.object(self.module, "_beet_binary", return_value=(True, "beet")), \
             mock.patch.object(self.module.subprocess, "run", return_value=fake_proc):
            response = self.client.get("/api/setup/status")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["integrations"]["lastgenre"]["state"], "dependency_plugin_missing")
        self.assertFalse(body["beets"]["plugin_loader_ok"])
        # Other integrations must not falsely claim the whole loader succeeded.
        for key in ("musicbrainz", "discpath", "replaygain", "listenbrainz"):
            self.assertNotEqual(body["integrations"][key]["state"], "configured")

    # -- 5. Timeout ---------------------------------------------------------------
    def test_timeout_returns_structured_response_without_raw_exception(self):
        self._write_config()
        timeout_exc = subprocess.TimeoutExpired(
            cmd=["beet", "-c", str(self.config_file), "-vv", "version"],
            timeout=12,
            output="partial stdout before timeout",
            stderr="partial stderr before timeout",
        )
        with self._env(), \
             mock.patch.object(self.module, "_beet_binary", return_value=(True, "beet")), \
             mock.patch.object(self.module.subprocess, "run", side_effect=timeout_exc):
            response = self.client.get("/api/setup/status")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["beets"]["plugin_loader_timed_out"])
        self.assertFalse(body["beets"]["plugin_loader_ok"])
        self.assertIsNone(body["beets"]["plugins_returncode"])
        text = response.get_data(as_text=True)
        self.assertNotIn("TimeoutExpired", text)
        self.assertNotIn("cmd=", text)
        # The endpoint must remain responsive after a timeout -- prove it by
        # calling it again immediately.
        with self._env(), \
             mock.patch.object(self.module, "_beet_binary", return_value=(True, "beet")), \
             mock.patch.object(self.module.subprocess, "run", side_effect=timeout_exc):
            second = self.client.get("/api/setup/status")
        self.assertEqual(second.status_code, 200)

    # -- 6. Redaction ---------------------------------------------------------------
    def test_redaction_covers_named_secrets_headers_cookies_urls_and_query_strings(self):
        self._write_config()
        secret_values = [
            "secret-token-xyz",
            "bearer-value-xyz",
            "cookie-value-xyz",
            "hunter2reallysecretvalue",
            "query-secret-xyz",
            "discogs-secret-xyz",
            "plex-secret-xyz",
        ]
        stdout = (
            'token: "secret-token-xyz"\n'
            "Authorization: Bearer bearer-value-xyz\n"
            "Cookie: session=cookie-value-xyz\n"
            "https://svcuser:hunter2reallysecretvalue@example.test/\n"
            "https://example.test/?api_key=query-secret-xyz\n"
            "DISCOGS_USER_TOKEN=discogs-secret-xyz\n"
            "PLEX_TOKEN=plex-secret-xyz\n"
        )
        fake_proc = mock.Mock(returncode=0, stdout=stdout, stderr="")
        with self._env(), \
             mock.patch.object(self.module, "_beet_binary", return_value=(True, "beet")), \
             mock.patch.object(self.module.subprocess, "run", return_value=fake_proc):
            response = self.client.get("/api/setup/status")

        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        for secret in secret_values:
            self.assertNotIn(secret, text)

    def test_redaction_exception_path_never_leaks_secrets(self):
        self._write_config()
        sensitive = (
            'Authorization: Bearer bearer-exc-secret Cookie: session=cookie-exc-secret '
            'token="kv-exc-secret" https://svcuser:hunter2excsecretvalue@example.test/'
        )
        with self._env(), \
             mock.patch.object(self.module, "_beet_binary", return_value=(True, "beet")), \
             mock.patch.object(self.module.subprocess, "run", side_effect=RuntimeError(sensitive)):
            response = self.client.get("/api/setup/status")

        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        for secret in ("bearer-exc-secret", "cookie-exc-secret", "kv-exc-secret", "hunter2excsecretvalue"):
            self.assertNotIn(secret, text)

    def test_redact_diagnostic_text_direct_pattern_coverage(self):
        module = self.module
        cases = [
            'api_key=abc123', 'api-key: abc123', 'token="abc123"', "password='abc123'",
            'secret: abc123', 'access_token=abc123', 'refresh_token=abc123',
            'client_secret=abc123', 'user_token=abc123', 'auth_token=abc123',
            'plex_token=abc123', 'lidarr_api_key=abc123', 'slskd_api_key=abc123',
            'Authorization: Bearer abc123', 'Authorization: Basic abc123',
            'Proxy-Authorization: abc123', 'Cookie: abc123', 'Set-Cookie: abc123',
            'X-Api-Key: abc123', '?api_key=abc123', '&token=abc123',
            '&access_token=abc123', '&auth=abc123', 'https://user:abc123@example.test/',
        ]
        for raw in cases:
            redacted = module._redact_diagnostic_text(raw)
            self.assertNotIn("abc123", redacted, raw)
            self.assertIn("[redacted]", redacted, raw)

    # -- 7. Optional credentials ------------------------------------------------
    def test_optional_credentials_absent_does_not_block_setup(self):
        # listenbrainz is enabled but has no token configured anywhere (config
        # or env) -- this must resolve to `not_configured`, not block setup.
        self.config_file.write_text(
            "plugins: musicbrainz discpath listenbrainz\n"
            "pluginpath:\n  - /config/beetsplug\n  - /app/beetsplug\n",
            encoding="utf-8",
        )
        fake_proc = mock.Mock(
            returncode=0,
            stdout="beets version 2.12.0\nplugins: musicbrainz, discpath, listenbrainz\n",
            stderr="",
        )
        stale_keys = (
            "OPENAI_API_KEY", "OPENROUTER_API_KEY", "AI_API_KEY",
            "DISCOGS_TOKEN", "DISCOGS_USER_TOKEN", "LISTENBRAINZ_TOKEN",
            "ACOUSTID_API_KEY", "ACOUSTID_KEY",
        )
        with self._env(), \
             mock.patch.object(self.module, "_beet_binary", return_value=(True, "beet")), \
             mock.patch.object(self.module.subprocess, "run", return_value=fake_proc):
            for key in stale_keys:
                os.environ.pop(key, None)
            response = self.client.get("/api/setup/status")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["integrations"]["ai"]["state"], "not_configured")
        self.assertEqual(body["integrations"]["discogs"]["state"], "installed_but_disabled")
        self.assertEqual(body["integrations"]["listenbrainz"]["state"], "not_configured")
        self.assertFalse(body["integrations"]["ai"]["required"])
        self.assertFalse(body["integrations"]["discogs"]["required"])


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
