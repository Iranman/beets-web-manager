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


_MISSING_MODULE = object()
_STUBBED_MODULES = ("app", "routes_setup", "routes_submissions", "routes_jobs", "routes_lidarr")


def _snapshot_stubbed_modules():
    return {name: sys.modules.get(name, _MISSING_MODULE) for name in _STUBBED_MODULES}


def _restore_stubbed_modules(snapshot):
    for name, module in snapshot.items():
        if module is _MISSING_MODULE:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def _load_routes_setup_against_stub_app(test_case=None):
    """(Re)import routes_setup against a fresh stub `app` module so each test
    gets independent route registration state."""
    from flask import Flask
    snapshot = _snapshot_stubbed_modules()
    stub = types.ModuleType("app")
    stub.__routes_setup_test_stub__ = True
    stub.app = Flask(__name__)
    sys.modules["app"] = stub
    sys.modules.pop("routes_setup", None)
    module = importlib.import_module("routes_setup")
    if test_case is not None:
        test_case.addCleanup(_restore_stubbed_modules, snapshot)
    return stub.app, module



def tearDownModule():
    """Best-effort guard for interrupted tests; normal cleanup is per-test."""
    if getattr(sys.modules.get("app"), "__routes_setup_test_stub__", False):
        for name in _STUBBED_MODULES:
            sys.modules.pop(name, None)
class RoutesSetupHealthTests(unittest.TestCase):
    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
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
        self.assertIn(r.status_code, (200, 503))
        body = r.get_json()
        self.assertIn(body["status"], ("ready", "warning"))
        self.assertIsInstance(body["blocking_reasons"], list)

    def test_health_ready_uses_remote_stock_beets_paths(self):
        diagnostics = {
            "available": True,
            "remote_reachable": True,
            "version": "beets version 2.4.0",
            "plugin_loader_ok": True,
            "paths": {
                "config": {"ok": True, "writable": True},
                "downloads": {"ok": True, "writable": True},
                "beets_config": {"exists": True},
            },
        }
        with mock.patch.object(self.module, "_beets_plugin_diagnostics", return_value=diagnostics):
            r = self.client.get("/health/ready")

        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["blocking_reasons"], [])
        self.assertTrue(body["beets"]["remote_reachable"])

    def test_health_ready_fails_closed_when_stock_beets_unavailable(self):
        diagnostics = {
            "available": False,
            "remote_reachable": False,
            "version": "",
            "plugin_loader_ok": False,
            "paths": {},
        }
        with mock.patch.object(self.module, "_beets_plugin_diagnostics", return_value=diagnostics):
            r = self.client.get("/health/ready")

        self.assertEqual(r.status_code, 503)
        body = r.get_json()
        self.assertEqual(body["status"], "warning")
        self.assertIn("stock Beets unavailable", body["blocking_reasons"])


class RoutesSetupStatusTests(unittest.TestCase):
    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
        self.client = self.flask_app.test_client()

    def test_status_never_crashes_on_missing_paths(self):
        r = self.client.get("/api/setup/status")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertIn(body["status"], ("ready", "warning"))
        self.assertIn("integrations", body)
        for key in ("ai", "musicbrainz", "acoustid", "discogs", "lastgenre", "listenbrainz", "discpath", "fetchart", "replaygain", "plex", "lidarr", "slskd"):
            self.assertIn(key, body["integrations"])
            self.assertIn("state", body["integrations"][key])
        self.assertIn("beets", body)
        self.assertIn("plugin_failures", body["beets"])

    def test_status_masks_secret_looking_settings(self):
        self.client.post("/api/setup/settings", json={"ai_api_key": "sk-verysecretvalue123"})
        r = self.client.get("/api/setup/status")
        settings = r.get_json()["settings"]
        self.assertNotIn("verysecretvalue123", str(settings))

    def test_status_sanitizes_remote_diagnostic_exceptions(self):
        sensitive = "/database/internal/path token=super-secret-key Traceback... File \"secret.py\", line 7"
        from backend.beets_adapter import beets_adapter
        with mock.patch.object(beets_adapter, "get_plugin_status", side_effect=RuntimeError(sensitive)):
            response = self.client.get("/api/setup/status")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["beets"]["diagnostic_error"], "Stock Beets is unavailable.")
        self.assertEqual(body["beets"]["remote_error"], "unavailable")
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


class RoutesSetupStatusBuildFailureSanitizationTests(unittest.TestCase):
    """Repository-wide CodeQL closure session, 2026-09-01 (py/stack-trace-exposure):
    /api/setup/status and /api/setup/diagnostics both returned
    str(ex)/f"...{ex}" verbatim when _build_setup_status_payload() itself
    raised (a genuinely broad except around a large diagnostics
    aggregation), unlike the already-hardened get_status() RuntimeError
    path covered by test_status_sanitizes_remote_diagnostic_exceptions
    above. Both now log server-side and return a fixed message."""

    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
        self.client = self.flask_app.test_client()

    def test_status_build_failure_is_sanitized_with_no_prior_cache(self):
        leak = "LEAK_MARKER token=super-secret-key /internal/db/path"
        with mock.patch.object(self.module, "_build_setup_status_payload", side_effect=RuntimeError(leak)):
            response = self.client.get("/api/setup/status")
        self.assertEqual(response.status_code, 503)
        body = response.get_json()
        self.assertEqual(body["error"], "Could not build setup status.")
        text = response.get_data(as_text=True)
        self.assertNotIn("LEAK_MARKER", text)
        self.assertNotIn("super-secret-key", text)

    def test_status_build_failure_falls_back_to_sanitized_stale_cache(self):
        leak = "LEAK_MARKER token=super-secret-key /internal/db/path"
        # Prime the cache with one real success first.
        self.client.get("/api/setup/status")
        with mock.patch.object(self.module, "_build_setup_status_payload", side_effect=RuntimeError(leak)):
            response = self.client.get("/api/setup/status?refresh=1")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body.get("stale"))
        self.assertEqual(body["refresh_error"], "Could not refresh setup status.")
        text = response.get_data(as_text=True)
        self.assertNotIn("LEAK_MARKER", text)
        self.assertNotIn("super-secret-key", text)

    def test_diagnostics_build_failure_is_sanitized(self):
        leak = "LEAK_MARKER token=super-secret-key /internal/db/path"
        with mock.patch.object(self.module, "_build_setup_status_payload", side_effect=RuntimeError(leak)):
            response = self.client.get("/api/setup/diagnostics")
        self.assertEqual(response.status_code, 503)
        body = response.get_json()
        self.assertEqual(body["error"], "Could not build setup diagnostics.")
        text = response.get_data(as_text=True)
        self.assertNotIn("LEAK_MARKER", text)
        self.assertNotIn("super-secret-key", text)


class RoutesSetupRemoteBeetsDiagnosticsTests(unittest.TestCase):
    """Setup-status diagnostics must come from the authenticated stock-Beets
    integration plugin (via BeetsAdapter), never a local `beet` executable
    or the deleted control agent."""

    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
        self.client = self.flask_app.test_client()

    def _plugin_status(self, **overrides):
        status = {
            "protocol_version": "1.0",
            "plugin_version": "1.0.0",
            "beets_version": "2.14.1",
            "capabilities": ["import", "modify", "remove", "move", "operations", "status", "mbsubmit"],
            "loaded_plugins": [
                "musicbrainz", "lastgenre", "listenbrainz", "discpath", "replaygain",
                "chroma", "fetchart", "discogs",
            ],
            "library_ready": True,
            "upstream_web_readonly": True,
            "plugin_mutations_enabled": True,
        }
        status.update(overrides)
        return status

    def _plugins_report(self, *, all_required_healthy=True, enabled_names=None, errors=None):
        # Mirrors verify_all_plugins()'s real per-plugin shape (name/enabled/
        # loaded/healthy) closely enough for _beets_plugin_diagnostics()'s
        # `configured_plugins = {p["name"] for p in ... if p.get("enabled")}`
        # to behave the same way it would against a real report.
        if enabled_names is None:
            enabled_names = self._plugin_status()["loaded_plugins"]
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

    def _status_response(self, plugin_status=None, *, side_effect=None, plugins_report=None):
        from backend.beets_adapter import beets_adapter
        plugin_status = self._plugin_status() if plugin_status is None else plugin_status
        patch_kwargs = {"side_effect": side_effect} if side_effect is not None else {"return_value": plugin_status}
        with mock.patch.object(beets_adapter, "get_plugin_status", **patch_kwargs) as get_status, \
             mock.patch.object(beets_adapter, "get_stats", return_value={"items": 0, "albums": 0}), \
             mock.patch("backend.beets_plugins.verify_all_plugins", return_value=plugins_report or self._plugins_report()), \
             mock.patch("subprocess.run", side_effect=AssertionError("local subprocess was used")), \
             mock.patch("shutil.which", side_effect=AssertionError("local executable lookup was used")):
            response = self.client.get("/api/setup/status")
        return response, get_status

    def test_successful_remote_status_reports_version_plugins_and_capabilities(self):
        response, get_status = self._status_response()

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["beets"]["version"], "2.14.1")
        self.assertTrue(body["beets"]["remote_reachable"])
        self.assertTrue(body["beets"]["plugin_loader_ok"])
        self.assertIn("fetchart", body["beets"]["loaded_plugins"])
        self.assertIn("chroma", body["beets"]["loaded_plugins"])
        # MusicBrainz is core Beets metadata capability, not a togglable
        # plugin -- "connected" reflects plugin-handshake health, never
        # plugins: list membership (see _musicbrainz_integration_status).
        self.assertEqual(body["integrations"]["musicbrainz"]["state"], "connected")
        self.assertEqual(body["integrations"]["musicbrainz"]["category"], "service")
        self.assertEqual(body["integrations"]["fetchart"]["category"], "beets_plugin")
        get_status.assert_called_once_with()

    def test_status_reuses_one_remote_snapshot_for_all_integrations(self):
        response, get_status = self._status_response()
        self.assertEqual(response.status_code, 200)
        get_status.assert_called_once_with()

    def test_remote_connection_failure_fails_closed(self):
        from backend.beets_adapter import BeetsAdapterConnectionError
        response, _ = self._status_response(side_effect=BeetsAdapterConnectionError("connection refused token=super-secret-key"))

        body = response.get_json()
        self.assertFalse(body["beets"]["available"])
        self.assertFalse(body["beets"]["plugin_loader_ok"])
        self.assertEqual(body["beets"]["remote_error"], "unavailable")
        # remote_reachable is False here (plugin itself unreachable), which
        # _musicbrainz_integration_status reports as "unavailable" -- a more
        # accurate state than "plugin_loader_failed" (that state means the
        # plugin WAS reached but its plugin loader failed).
        self.assertEqual(body["integrations"]["musicbrainz"]["state"], "unavailable")
        self.assertNotIn("super-secret-key", response.get_data(as_text=True))

    def test_remote_authentication_failure_fails_closed(self):
        from backend.beets_adapter import BeetsAdapterAuthError
        response, _ = self._status_response(side_effect=BeetsAdapterAuthError("401 token=super-secret-key"))

        body = response.get_json()
        self.assertFalse(body["beets"]["remote_reachable"])
        self.assertEqual(body["beets"]["remote_error"], "authentication_failed")
        self.assertFalse(body["beets"]["plugin_loader_ok"])
        self.assertNotIn("super-secret-key", response.get_data(as_text=True))

    def test_remote_timeout_fails_closed(self):
        from backend.beets_adapter import BeetsAdapterTimeoutError
        response, _ = self._status_response(side_effect=BeetsAdapterTimeoutError("timed out contacting plugin"))

        body = response.get_json()
        self.assertFalse(body["beets"]["plugin_loader_ok"])
        self.assertTrue(body["beets"]["plugin_loader_timed_out"])
        self.assertEqual(body["beets"]["remote_error"], "timeout")

    def test_malformed_remote_response_shape_fails_closed(self):
        response, _ = self._status_response({"beets_version": "2.14.1"})  # no protocol_version

        body = response.get_json()
        self.assertFalse(body["beets"]["plugin_loader_ok"])
        self.assertEqual(body["beets"]["remote_error"], "malformed_response")
        self.assertEqual(body["beets"]["configured_plugins"], [])

    def test_failed_fetchart_is_dependency_missing_and_redacted(self):
        response, _ = self._status_response(
            plugins_report=self._plugins_report(
                all_required_healthy=False,
                errors=["fetchart: error loading plugin fetchart token=super-secret-key"],
            ),
        )

        body = response.get_json()
        self.assertFalse(body["beets"]["plugin_loader_ok"])
        self.assertNotIn("super-secret-key", response.get_data(as_text=True))

    def test_missing_chroma_does_not_block_independent_submission_commands(self):
        base = self._plugin_status()
        status = self._plugin_status(
            loaded_plugins=[p for p in base["loaded_plugins"] if p != "chroma"],
            capabilities=[c for c in base["capabilities"] if c != "mbsubmit"],
        )
        response, _ = self._status_response(status)

        body = response.get_json()
        self.assertEqual(body["integrations"]["acoustid"]["state"], "dependency_plugin_missing")

    def test_replaygain_backend_readiness_uses_remote_backend_tools(self):
        response, _ = self._status_response()
        body = response.get_json()
        # No local ffmpeg/config.yaml in this stub-app fixture -- replaygain
        # genuinely has nothing configured to report, which is the honest
        # "dependency_plugin_missing" state, not a fabricated success.
        self.assertIn(body["integrations"]["replaygain"]["state"], ("dependency_plugin_missing", "installed_but_disabled"))

    def test_discpath_custom_plugin_readiness_requires_remote_loaded_plugin(self):
        base = self._plugin_status()
        status = self._plugin_status(loaded_plugins=[p for p in base["loaded_plugins"] if p != "discpath"])
        response, _ = self._status_response(status)

        body = response.get_json()
        self.assertEqual(body["integrations"]["discpath"]["state"], "dependency_plugin_missing")

    def test_optional_credentials_absent_does_not_block_setup(self):
        stale_keys = (
            "OPENAI_API_KEY", "OPENROUTER_API_KEY", "AI_API_KEY",
            "DISCOGS_TOKEN", "DISCOGS_USER_TOKEN", "LISTENBRAINZ_TOKEN",
            "ACOUSTID_API_KEY", "ACOUSTID_KEY",
        )
        with mock.patch.dict(os.environ, {key: "" for key in stale_keys}, clear=False):
            response, _ = self._status_response()

        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["integrations"]["ai"]["state"], "not_configured")
        self.assertEqual(body["integrations"]["discogs"]["state"], "not_configured")
        self.assertEqual(body["integrations"]["listenbrainz"]["state"], "not_configured")
        self.assertFalse(body["integrations"]["ai"]["required"])
        self.assertFalse(body["integrations"]["discogs"]["required"])

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

    def test_redact_diagnostic_text_cookie_header_redacts_entire_value(self):
        module = self.module
        cases = [
            "Cookie: session=alpha; refresh=bravo",
            "Cookie: a=alpha; b=bravo; c=charlie",
            "Set-Cookie: session=alpha; Path=/; HttpOnly",
            "Set-Cookie: access=alpha; refresh=bravo; Secure; SameSite=Lax",
            "cookie: lower=alpha; second=bravo",
        ]
        for raw in cases:
            redacted = module._redact_diagnostic_text(raw)
            for value in ("alpha", "bravo", "charlie"):
                self.assertNotIn(value, redacted, raw)
            self.assertIn("[redacted]", redacted, raw)

    def test_redact_diagnostic_text_does_not_consume_following_line(self):
        module = self.module
        raw = "Cookie: session=alpha; refresh=bravo\nnext diagnostic line unaffected"
        redacted = module._redact_diagnostic_text(raw)
        self.assertNotIn("alpha", redacted)
        self.assertNotIn("bravo", redacted)
        self.assertIn("next diagnostic line unaffected", redacted)

    def test_redact_diagnostic_text_is_idempotent(self):
        module = self.module
        cases = [
            "token=abc",
            "Authorization: Bearer abc",
            "Cookie: session=abc; refresh=def",
            "https://user:password@example.test/",
            "https://example.test/?api_key=abc&token=def",
            "DISCOGS_USER_TOKEN=abc",
        ]
        for raw in cases:
            first = module._redact_diagnostic_text(raw)
            second = module._redact_diagnostic_text(first)
            third = module._redact_diagnostic_text(second)
            self.assertEqual(first, second, raw)
            self.assertEqual(second, third, raw)
            self.assertNotIn("]]", first, raw)

class RoutesSetupTestConnectionTests(unittest.TestCase):
    """These hit the /api/setup/test/* endpoints without real credentials —
    verifying they degrade to a clear not_configured/failed response rather
    than crashing or reporting false success."""

    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
        self.client = self.flask_app.test_client()

    def test_ai_test_without_key_reports_not_configured(self):
        r = self.client.post("/api/setup/test/ai", json={})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["status"], "not_configured")

    def test_ai_test_invalid_key_reports_failed_auth(self):
        exc = self.module.urllib.error.HTTPError(
            "https://api.openai.com/v1/models/gpt-4o-mini",
            401,
            "Unauthorized",
            {},
            None,
        )
        with mock.patch.object(self.module.urllib.request, "urlopen", side_effect=exc):
            r = self.client.post(
                "/api/setup/test/ai",
                json={"api_key": "sk-invalid-test-key", "model": "gpt-4o-mini"},
            )
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["status"], "failed")
        self.assertIn("rejected the API key", body["error"])
        self.assertNotIn("sk-invalid-test-key", repr(body))

    def test_plex_test_invalid_token_reports_failed_auth(self):
        exc = self.module.urllib.error.HTTPError(
            "http://plex.example/library/sections",
            401,
            "Unauthorized",
            {},
            None,
        )
        with mock.patch.object(self.module.urllib.request, "urlopen", side_effect=exc):
            r = self.client.post(
                "/api/setup/test/plex",
                json={"url": "http://plex.example", "token": "invalid-plex-token"},
            )
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["status"], "failed")
        self.assertIn("invalid or expired", body["error"])
        self.assertNotIn("invalid-plex-token", repr(body))

    def test_acoustid_test_invalid_key_reports_failed_auth(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"status":"error","error":{"message":"invalid API key"}}'

        diagnostics = {
            "remote_reachable": True,
            "loaded_plugins": ["chroma"],
            "fpcalc_path": "/usr/bin/fpcalc",
            "capabilities": {
                "acoustid_lookup": {
                    "fpcalc_available": True,
                    "chroma_loaded": True,
                    "pyacoustid_available": True,
                }
            },
        }
        with mock.patch.object(self.module, "_beets_plugin_diagnostics", return_value=diagnostics), \
             mock.patch.object(self.module.urllib.request, "urlopen", return_value=Response()):
            r = self.client.post("/api/setup/test/acoustid", json={"api_key": "invalid-acoustid-key"})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["status"], "failed")
        self.assertIn("invalid API key", body["error"])
        self.assertNotIn("invalid-acoustid-key", repr(body))

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
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
        self.client = self.flask_app.test_client()
        # Module defaults point at /config/*, which isn't writable (or may
        # not even exist) outside the real container — isolate to a temp
        # dir so this test doesn't depend on host filesystem layout.
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.module._SETTINGS_FILE = root / "app_settings.json"
        self.module._SETUP_COMPLETE_MARKER = root / ".setup_complete"
        self.env_patch = mock.patch.dict(os.environ, {"WEB_MANAGER_DATA_DIR": str(root)}, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
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

    def test_complete_marker_allows_only_one_success(self):
        r1 = self.client.post("/api/setup/complete")
        r2 = self.client.post("/api/setup/complete")
        self.assertTrue(r1.get_json()["ok"])
        self.assertEqual(r2.status_code, 409)
        self.assertFalse(r2.get_json()["ok"])
        self.assertTrue(self.module._SETUP_COMPLETE_MARKER.exists())


class RoutesSetupEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
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
            "DEMO_MODE=0\n"
            "\n"
            "# Host volume paths -- mirrors the real repo .env.example, which\n"
            "# writes a literal placeholder value here even though the running\n"
            "# container never sees it (see MUSIC_PATH's own metadata entry).\n"
            "MUSIC_PATH=./music\n",
            encoding="utf-8",
        )
        self.module._SETUP_ENV_FILE = self.env_file
        self.module._ENV_EXAMPLE_FILE = self.example_file
        self._saved_env = dict(os.environ)
        # Clear specific env vars that might leak from test harness
        for var in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "AI_API_KEY", "AI_BASE_URL", "AI_MODEL", "PLEX_URL", "PLEX_TOKEN", "LIDARR_API_KEY", "DEMO_MODE"):
            os.environ.pop(var, None)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.tempdir.cleanup()
        os.environ.clear()
        os.environ.update(self._saved_env)

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

    def test_env_save_rejects_non_editable_path_variable(self):
        # MUSIC_PATH/BEETS_CONFIG_PATH/DOWNLOADS_PATH/WEB_MANAGER_DATA_PATH are
        # host-side docker-compose.yml bind-mount interpolation variables that
        # this container can never observe or change -- writing to them here
        # would silently no-op against the real deployment, so the save
        # endpoint must reject the attempt outright rather than pretending it
        # took effect.
        self.env_file.write_text("", encoding="utf-8")
        r = self.client.post("/api/setup/env", json={
            "variables": {"MUSIC_PATH": "/some/other/music"},
        })
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("MUSIC_PATH=/some/other/music", self.env_file.read_text(encoding="utf-8"))

    def test_env_clear_rejects_non_editable_path_variable(self):
        self.env_file.write_text("MUSIC_PATH=/existing/music\n", encoding="utf-8")
        r = self.client.post("/api/setup/env", json={
            "variables": {},
            "clear": ["MUSIC_PATH"],
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("MUSIC_PATH=/existing/music", self.env_file.read_text(encoding="utf-8"))

    def test_env_example_literal_value_never_overrides_metadata_none_default(self):
        # Found live against the real deployed .env.example (which writes
        # "MUSIC_PATH=./music" as a template placeholder): _env_catalog()
        # must let the curated metadata's explicit default=None win over
        # that literal value, since the running container can never actually
        # see it -- showing "./music" here would resurrect the exact
        # fabricated-host-path bug this metadata entry exists to fix. This
        # test's fixture .env.example (setUp) deliberately mirrors the real
        # repo's MUSIC_PATH=./music line so this regresses if the precedence
        # in _env_catalog is ever flipped back.
        r = self.client.get("/api/setup/env")
        self.assertEqual(r.status_code, 200)
        variables = {item["name"]: item for item in r.get_json()["variables"]}
        self.assertIsNone(variables["MUSIC_PATH"]["default"])
        self.assertNotEqual(variables["MUSIC_PATH"]["value"], "./music")

    def test_env_displays_effective_values_and_defaults(self):
        r = self.client.get("/api/setup/env")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        variables = {item["name"]: item for item in body["variables"]}

        # PUID / PGID
        self.assertIn("PUID", variables)
        self.assertEqual(variables["PUID"]["default"], "1000")
        self.assertTrue(variables["PUID"]["configured"])
        self.assertEqual(variables["PUID"]["section"], "System & Environment")

        # TZ / WEBCONTROL_PORT
        self.assertIn("TZ", variables)
        self.assertEqual(variables["TZ"]["default"], "UTC")
        self.assertIn("WEBCONTROL_PORT", variables)
        self.assertEqual(variables["WEBCONTROL_PORT"]["default"], "8337")

        # Username
        self.assertIn("BEETS_WEB_USERNAME", variables)
        self.assertEqual(variables["BEETS_WEB_USERNAME"]["value"], "admin")
        self.assertEqual(variables["BEETS_WEB_USERNAME"]["default"], "admin")

        # AI settings
        self.assertIn("AI_MODEL", variables)
        self.assertEqual(variables["AI_MODEL"]["value"], "gpt-4o-mini")
        self.assertEqual(variables["AI_MODEL"]["default"], "gpt-4o-mini")
        self.assertIn("AI_BASE_URL", variables)
        self.assertEqual(variables["AI_BASE_URL"]["value"], "https://api.openai.com/v1")

        # Paths: BEETS_CONFIG_PATH/MUSIC_PATH/DOWNLOADS_PATH/WEB_MANAGER_DATA_PATH
        # are docker-compose.yml's own host-side bind-mount interpolation
        # variables -- never forwarded into this container's environment,
        # so this application has no way to know (or change) the real host
        # path. No fabricated "./music"-style default is shown, and the
        # field is not editable from here (the deployment's own .env /
        # docker-compose.yml is the only place that can change it).
        self.assertIn("MUSIC_PATH", variables)
        self.assertEqual(variables["MUSIC_PATH"]["container_path"], "/music")
        self.assertIsNone(variables["MUSIC_PATH"]["default"])
        self.assertFalse(variables["MUSIC_PATH"]["editable"])

    def test_env_effective_value_overrides_default(self):
        self.env_file.write_text("AI_MODEL=custom-llm-model-1\nMUSIC_PATH=/custom/host/music\n", encoding="utf-8")
        r = self.client.get("/api/setup/env")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        variables = {item["name"]: item for item in body["variables"]}

        self.assertEqual(variables["AI_MODEL"]["value"], "custom-llm-model-1")
        self.assertEqual(variables["AI_MODEL"]["source"], "persisted")
        self.assertEqual(variables["AI_MODEL"]["default"], "gpt-4o-mini")

        self.assertEqual(variables["MUSIC_PATH"]["value"], "/custom/host/music")
        self.assertEqual(variables["MUSIC_PATH"]["source"], "persisted")
        self.assertEqual(variables["MUSIC_PATH"]["container_path"], "/music")

    def test_env_runtime_override_and_source(self):
        with mock.patch.dict(os.environ, {"PUID": "1001", "TZ": "America/New_York"}):
            r = self.client.get("/api/setup/env")
            self.assertEqual(r.status_code, 200)
            variables = {item["name"]: item for item in r.get_json()["variables"]}
            self.assertEqual(variables["PUID"]["value"], "1001")
            self.assertEqual(variables["PUID"]["source"], "environment")
            self.assertEqual(variables["TZ"]["value"], "America/New_York")
            self.assertEqual(variables["TZ"]["source"], "environment")

    def test_secret_configured_and_unconfigured_states(self):
        self.env_file.write_text("OPENAI_API_KEY=sk-testsecretkey123\n", encoding="utf-8")
        r = self.client.get("/api/setup/env")
        self.assertEqual(r.status_code, 200)
        variables = {item["name"]: item for item in r.get_json()["variables"]}

        # Configured secret
        self.assertTrue(variables["OPENAI_API_KEY"]["secret"])
        self.assertTrue(variables["OPENAI_API_KEY"]["configured"])
        self.assertNotIn("sk-testsecretkey123", variables["OPENAI_API_KEY"]["value"])
        self.assertTrue(variables["OPENAI_API_KEY"]["has_value"])
        self.assertEqual(variables["OPENAI_API_KEY"]["source"], "persisted")

        # Unconfigured secret
        self.assertTrue(variables["OPENROUTER_API_KEY"]["secret"])
        self.assertFalse(variables["OPENROUTER_API_KEY"]["configured"])
        self.assertEqual(variables["OPENROUTER_API_KEY"]["value"], "")
        self.assertEqual(variables["OPENROUTER_API_KEY"]["source"], "not_configured")

    def test_untouched_secret_survives_save(self):
        self.env_file.write_text("OPENAI_API_KEY=sk-importantkey123\n", encoding="utf-8")
        # Save without touching OPENAI_API_KEY (sending empty string without clear)
        r = self.client.post("/api/setup/env", json={
            "variables": {"AI_MODEL": "gpt-4o", "OPENAI_API_KEY": ""},
            "clear": [],
        })
        self.assertEqual(r.status_code, 200)
        file_content = self.env_file.read_text(encoding="utf-8")
        self.assertIn("OPENAI_API_KEY=sk-importantkey123", file_content)
        self.assertIn("AI_MODEL=gpt-4o", file_content)

    def test_new_secret_replaces_existing_secret(self):
        self.env_file.write_text("OPENAI_API_KEY=sk-old-key-12345\n", encoding="utf-8")
        r = self.client.post("/api/setup/env", json={
            "variables": {"OPENAI_API_KEY": "sk-new-key-67890"},
            "clear": [],
        })
        self.assertEqual(r.status_code, 200)
        file_content = self.env_file.read_text(encoding="utf-8")
        self.assertIn("OPENAI_API_KEY=sk-new-key-67890", file_content)

    def test_configuration_precedence(self):
        # Process env > persisted .env > default
        self.env_file.write_text("AI_MODEL=model-from-file\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"AI_MODEL": "model-from-env"}):
            r = self.client.get("/api/setup/env")
            variables = {item["name"]: item for item in r.get_json()["variables"]}
            self.assertEqual(variables["AI_MODEL"]["value"], "model-from-env")
            self.assertEqual(variables["AI_MODEL"]["source"], "environment")

        # When process env is unset/empty, file takes precedence
        with mock.patch.dict(os.environ, {"AI_MODEL": ""}):
            r = self.client.get("/api/setup/env")
            variables = {item["name"]: item for item in r.get_json()["variables"]}
            self.assertEqual(variables["AI_MODEL"]["value"], "model-from-file")
            self.assertEqual(variables["AI_MODEL"]["source"], "persisted")

    def test_page_refresh_after_save_returns_updated_state(self):
        r = self.client.post("/api/setup/env", json={
            "variables": {"PUID": "2000", "TZ": "Europe/London", "AI_MODEL": "claude-3.5-haiku"},
            "clear": [],
        })
        self.assertEqual(r.status_code, 200)
        get_r = self.client.get("/api/setup/env")
        self.assertEqual(get_r.status_code, 200)
        variables = {item["name"]: item for item in get_r.get_json()["variables"]}
        self.assertEqual(variables["PUID"]["value"], "2000")
        self.assertEqual(variables["TZ"]["value"], "Europe/London")
        self.assertEqual(variables["AI_MODEL"]["value"], "claude-3.5-haiku")

    def test_revealable_secrets_return_plaintext_via_reveal_endpoint(self):
        self.env_file.write_text("OPENAI_API_KEY=sk-testsecretkey123\nPLEX_TOKEN=plex-secret-token-67890\n", encoding="utf-8")
        
        # 1. GET /api/setup/env never returns plaintext
        get_r = self.client.get("/api/setup/env")
        self.assertEqual(get_r.status_code, 200)
        variables = {item["name"]: item for item in get_r.get_json()["variables"]}
        self.assertTrue(variables["OPENAI_API_KEY"]["secret"])
        self.assertTrue(variables["OPENAI_API_KEY"]["revealable"])
        self.assertTrue(variables["OPENAI_API_KEY"]["configured"])
        self.assertIsNone(variables["OPENAI_API_KEY"]["effective_value"])
        self.assertNotEqual(variables["OPENAI_API_KEY"]["value"], "sk-testsecretkey123")

        # 2. POST /api/setup/env/<name>/reveal returns exact plaintext
        reveal_r = self.client.post("/api/setup/env/OPENAI_API_KEY/reveal")
        self.assertEqual(reveal_r.status_code, 200)
        body = reveal_r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["name"], "OPENAI_API_KEY")
        self.assertEqual(body["value"], "sk-testsecretkey123")

        # 3. PLEX_TOKEN reveal
        reveal_plex = self.client.post("/api/setup/env/PLEX_TOKEN/reveal")
        self.assertEqual(reveal_plex.status_code, 200)
        self.assertEqual(reveal_plex.get_json()["value"], "plex-secret-token-67890")

    def test_password_is_never_revealable(self):
        get_r = self.client.get("/api/setup/env")
        self.assertEqual(get_r.status_code, 200)
        variables = {item["name"]: item for item in get_r.get_json()["variables"]}
        self.assertTrue(variables["BEETS_WEB_PASSWORD"]["secret"])
        self.assertFalse(variables["BEETS_WEB_PASSWORD"]["revealable"])

        reveal_r = self.client.post("/api/setup/env/BEETS_WEB_PASSWORD/reveal")
        self.assertEqual(reveal_r.status_code, 403)
        self.assertFalse(reveal_r.get_json()["ok"])
        self.assertIn("cannot be revealed", reveal_r.get_json()["error"])

    def test_overridden_status_and_saved_value_display(self):
        # Persisted has one value, environment has another
        self.env_file.write_text("AI_MODEL=persisted-model-v1\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"AI_MODEL": "environment-model-v2"}):
            r = self.client.get("/api/setup/env")
            self.assertEqual(r.status_code, 200)
            variables = {item["name"]: item for item in r.get_json()["variables"]}
            ai_var = variables["AI_MODEL"]
            self.assertEqual(ai_var["effective_value"], "environment-model-v2")
            self.assertEqual(ai_var["saved_value"], "persisted-model-v1")
            self.assertTrue(ai_var["has_saved_value"])
            self.assertTrue(ai_var["is_overridden"])
            self.assertEqual(ai_var["source"], "environment")
            self.assertIn("overrides", ai_var["status_message"].lower())

    def test_comprehensive_setting_inventory_and_metadata(self):
        r = self.client.get("/api/setup/env")
        self.assertEqual(r.status_code, 200)
        variables = {item["name"]: item for item in r.get_json()["variables"]}

        expected_keys = [
            ("PUID", "System & Environment", False),
            ("PGID", "System & Environment", False),
            ("TZ", "System & Environment", False),
            ("WEBCONTROL_PORT", "System & Environment", False),
            ("DEMO_MODE", "System & Environment", False),
            ("BEETS_SQLITE_TIMEOUT", "System & Environment", False),
            ("BEETS_LONG_OPERATION_MAX_SECONDS", "System & Environment", False),
            ("BEETS_WEB_USERNAME", "Authentication & Security", False),
            ("BEETS_WEB_PASSWORD", "Authentication & Security", True),
            ("BEETS_WEB_AUTH_TOKEN", "Authentication & Security", True),
            ("BEETS_TRUSTED_PROXIES", "Authentication & Security", False),
            ("BEETS_OUTBOUND_TIMEOUT_SECONDS", "Authentication & Security", False),
            ("OPENAI_API_KEY", "AI & LLM Services", True),
            ("OPENROUTER_API_KEY", "AI & LLM Services", True),
            ("AI_MODEL", "AI & LLM Services", False),
            ("BEETS_CONFIG", "Beets Core & Engine", False),
            ("BEETS_LIBRARY", "Beets Core & Engine", False),
            ("MUSIC_PATH", "Storage & Paths", False),
            ("DOWNLOADS_PATH", "Storage & Paths", False),
            ("BEETS_CONFIG_PATH", "Storage & Paths", False),
            ("PLAYLIST_DIR", "Storage & Paths", False),
            ("ACOUSTID_API_KEY", "Music Services & Metadata", True),
            ("DISCOGS_TOKEN", "Music Services & Metadata", True),
            ("LISTENBRAINZ_TOKEN", "Music Services & Metadata", True),
            ("SPOTIFY_CLIENT_ID", "Music Services & Metadata", False),
            ("PLEX_URL", "Media Server Integrations", False),
            ("PLEX_TOKEN", "Media Server Integrations", True),
            ("LIDARR_URL", "Media Server Integrations", False),
            ("LIDARR_API_KEY", "Media Server Integrations", True),
            ("SLSKD_URL", "Media Server Integrations", False),
            ("SLSKD_API_KEY", "Media Server Integrations", True),
            ("QBITTORRENT_URL", "Media Server Integrations", False),
            ("PLAYLIST_AUTO_SYNC", "Playlists & Download Providers", False),
            ("PLAYLIST_DOWNLOAD_METHODS", "Playlists & Download Providers", False),
            ("SPOTIFLAC_SERVICES", "Playlists & Download Providers", False),
        ]

        for key, section, is_secret in expected_keys:
            self.assertIn(key, variables, f"Missing expected setting key: {key}")
            self.assertEqual(variables[key]["section"], section, f"Key {key} section mismatch")
            self.assertEqual(variables[key]["secret"], is_secret, f"Key {key} secret mismatch")



class RoutesSetupHelperTests(unittest.TestCase):
    def setUp(self):
        _, self.module = _load_routes_setup_against_stub_app(self)

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
