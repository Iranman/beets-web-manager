"""Tests for routes_setup.py (first-run setup wizard API).

routes_setup imports `from app import app`, which would otherwise require
booting the full app.py (beets Library, all route modules, etc.). Instead we
stub sys.modules['app'] with a minimal Flask app before importing
routes_setup, matching how a real Flask blueprint would be exercised without
the rest of the application's side effects.
"""
import importlib
import json
import re
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
        # Status routes now apply the BEETSDIR containment check (#222), so
        # start from the shipped container pair, not a BEETSDIR another test
        # module left in os.environ. Tests patch their own values on top.
        env = mock.patch.dict(os.environ, {"BEETSDIR": "/config", "BEETS_CONFIG": "/config/config.yaml"})
        env.start()
        test_case.addCleanup(env.stop)
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

    def test_health_ready_fails_closed_when_engine_unavailable(self):
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
        self.assertIn("Beets engine unavailable", body["blocking_reasons"])

    def test_health_ready_without_beets_config_is_still_ready(self):
        # Regression (PR #466): the embedded engine falls back to Beets'
        # defaults when no config.yaml exists, so a MISSING config must NOT
        # block readiness.
        diagnostics = {
            "available": True,
            "remote_reachable": True,
            "version": "beets version 2.14.1",
            "plugin_loader_ok": True,
            "paths": {
                "config": {"ok": True, "writable": True},
                "downloads": {"ok": True, "writable": True},
                "beets_config": {"exists": False},
            },
        }
        with mock.patch.object(self.module, "_beets_plugin_diagnostics", return_value=diagnostics):
            r = self.client.get("/health/ready")

        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["blocking_reasons"], [])
        self.assertEqual(body["status"], "ready")

    def test_health_ready_blocks_when_required_plugin_failed(self):
        # D3 intent: a reachable engine whose required plugins did not load is
        # not ready -- independent of whether a config.yaml exists.
        diagnostics = {
            "available": True,
            "remote_reachable": True,
            "version": "beets version 2.14.1",
            "plugin_loader_ok": False,
            "paths": {
                "config": {"ok": True, "writable": True},
                "downloads": {"ok": True, "writable": True},
                "beets_config": {"exists": False},
            },
        }
        with mock.patch.object(self.module, "_beets_plugin_diagnostics", return_value=diagnostics):
            r = self.client.get("/health/ready")

        self.assertEqual(r.status_code, 503)
        self.assertIn("beets plugin loader failed", r.get_json()["blocking_reasons"])


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

    def test_status_does_not_expose_saved_settings(self):
        # Unauthenticated status used to echo saved settings (masked by name
        # only). Nothing read them, so they are gone: no name rule can leak.
        stored = {"qbit_password": "pw-plain-value", "odd_name": "cred-plain-value",
                  "nested": {"inner_token": "nested-plain-value"}, "ai_model": "visible-model"}
        record = {"settings": stored, "revision": "r1"}
        with mock.patch.object(self.module, "_load_settings_record", return_value=record), \
                mock.patch.object(self.module, "_load_settings", return_value=stored):
            r = self.client.get("/api/setup/status?refresh=1")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("settings", r.get_json())
        self.assertNotIn("plain-value", r.get_data(as_text=True))

    def test_status_sanitizes_remote_diagnostic_exceptions(self):
        sensitive = "/database/internal/path token=super-secret-key Traceback... File \"secret.py\", line 7"
        from backend.beets_adapter import beets_adapter
        with mock.patch.object(beets_adapter, "get_plugin_status", side_effect=RuntimeError(sensitive)):
            response = self.client.get("/api/setup/status")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["beets"]["diagnostic_error"], "Beets inside Web Manager is unavailable.")
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
            "ACOUSTID_API_KEY", "ACOUSTID_USER_KEY", "ACOUSTID_KEY",
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
                return b'{"status":"error","error":{"code":4,"message":"invalid API key"}}'

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
        self.assertIn("API key was rejected", body["error"])
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

    def test_posting_masked_key_keeps_saved_value(self):
        # #522: GET masks key/token settings; echoing that mask back must not overwrite.
        r = self.client.post("/api/setup/settings", json={"ai_api_key": "fake-saved-key"})
        rev = r.get_json()["revision"]
        masked = self.client.get("/api/setup/settings").get_json()["settings"]["ai_api_key"]
        self.assertEqual(masked, self.module._SECRET_MASK)
        r = self.client.post("/api/setup/settings", json={
            "ai_api_key": masked, "ai_model": "m2", "expected_revision": rev})
        self.assertTrue(r.get_json()["ok"])
        stored = self.module._load_settings()
        self.assertEqual(stored["ai_api_key"], "fake-saved-key")
        self.assertEqual(stored["ai_model"], "m2")

    def test_get_settings_masks_password_and_secret_and_keeps_on_echo(self):
        # qbit_pass is a secret only via its legacy alias (QBIT_PASS -> QBITTORRENT_PASSWORD).
        r = self.client.post("/api/setup/settings", json={
            "qbit_password": "pw-plain-value", "client_secret": "cs-x", "qbit_pass": "alias-x"})
        rev = r.get_json()["revision"]
        got = self.client.get("/api/setup/settings").get_json()["settings"]
        mask = self.module._SECRET_MASK
        self.assertEqual(got, {"qbit_password": mask, "client_secret": mask, "qbit_pass": mask})
        r = self.client.post("/api/setup/settings", json={
            "qbit_password": got["qbit_password"], "expected_revision": rev})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.module._load_settings()["qbit_password"], "pw-plain-value")

    def test_posting_empty_string_clears_settings_secret(self):
        r = self.client.post("/api/setup/settings", json={"ai_api_key": "fake-saved-key"})
        r = self.client.post("/api/setup/settings", json={"ai_api_key": "", "expected_revision": r.get_json()["revision"]})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.module._load_settings()["ai_api_key"], "")

    def test_get_settings_masks_nested_url_and_passwd_and_keeps_on_echo(self):
        # #570: nested secrets, URL userinfo and names like passwd were returned in full.
        stored_in = {
            "plex": {"url": "http://plex.invalid:32400", "token": "fake-nested-token",
                     "servers": [{"name": "a", "password": "fake-list-pw"}]},
            "feed_url": "https://fakeuser:fake-url-pw@feed.invalid/path",
            "alt_url": "https://fakeuser:fake-wide-pw＠feed.invalid/",
            "passwd": "fake-passwd",
            "ai_model": "m1",
        }
        r = self.client.post("/api/setup/settings", json=stored_in)
        rev = r.get_json()["revision"]
        got = self.client.get("/api/setup/settings").get_json()["settings"]
        mask = self.module._SECRET_MASK
        body = json.dumps(got)
        for leaked in ("fake-nested-token", "fake-list-pw", "fake-url-pw", "fake-wide-pw", "fakeuser", "fake-passwd"):
            self.assertNotIn(leaked, body)
        self.assertEqual(got["plex"]["token"], mask)
        self.assertEqual(got["plex"]["url"], "http://plex.invalid:32400")
        self.assertEqual(got["plex"]["servers"], [{"name": "a", "password": mask}])
        self.assertEqual(got["feed_url"], f"https://{mask}@feed.invalid/path")
        self.assertEqual(got["passwd"], mask)
        self.assertEqual(got["ai_model"], "m1")
        # Posting the whole masked document back leaves every stored value unchanged.
        r = self.client.post("/api/setup/settings", json={**got, "expected_revision": rev})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.module._load_settings(), stored_in)

    def test_falsy_secret_round_trips(self):
        # #570: a secret of 0 was masked to "" and the echo erased it.
        r = self.client.post("/api/setup/settings", json={"ai_api_key": 0})
        got = self.client.get("/api/setup/settings").get_json()["settings"]
        self.assertEqual(got["ai_api_key"], self.module._SECRET_MASK)
        r = self.client.post("/api/setup/settings", json={**got, "expected_revision": r.get_json()["revision"]})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.module._load_settings()["ai_api_key"], 0)

    def test_get_settings_masks_query_and_schemeless_secrets(self):
        # #585: secret query parameters and scheme-less userinfo were returned in full.
        stored_in = {
            "feed": "https://feed.invalid/x?api_key=fake-query-key&page=2",
            "cb": "https://app.invalid/cb?pa%73s=fake-enc-pass#access_token=fake-frag-token",
            "db": "fakedbuser:fake-db-pw@db.invalid:5432",
            "db_wide": "fakedbuser:fake-wide-db-pw＠db.invalid:5432",
            "contact": "someone@mail.invalid",
            "cache_bypass": "yes",
        }
        r = self.client.post("/api/setup/settings", json=stored_in)
        rev = r.get_json()["revision"]
        got = self.client.get("/api/setup/settings").get_json()["settings"]
        mask = self.module._SECRET_MASK
        body = json.dumps(got, ensure_ascii=False)
        for leaked in ("fake-query-key", "fake-enc-pass", "fake-frag-token", "fake-db-pw", "fake-wide-db-pw", "fakedbuser"):
            self.assertNotIn(leaked, body)
        self.assertEqual(got["feed"], f"https://feed.invalid/x?api_key={mask}&page=2")
        self.assertEqual(got["db"], f"{mask}@db.invalid:5432")
        self.assertEqual(got["db_wide"], mask)
        self.assertEqual(got["contact"], "someone@mail.invalid")
        self.assertEqual(got["cache_bypass"], "yes")
        r = self.client.post("/api/setup/settings", json={**got, "expected_revision": rev})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.module._load_settings(), stored_in)

    def test_masked_list_entries_restore_by_identity(self):
        # #585: a reordered list paired each masked secret with another entry.
        servers = [{"name": "a", "password": "fake-pw-a"}, {"name": "b", "password": "fake-pw-b"}]
        r = self.client.post("/api/setup/settings", json={"servers": servers})
        got = self.client.get("/api/setup/settings").get_json()["settings"]
        r = self.client.post("/api/setup/settings", json={
            "servers": list(reversed(got["servers"])), "expected_revision": r.get_json()["revision"]})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.module._load_settings()["servers"], list(reversed(servers)))
        # An edited entry still carrying a mask cannot be tied to a stored secret.
        rev = r.get_json()["revision"]
        edited = [{"name": "renamed", "password": self.module._SECRET_MASK}]
        r = self.client.post("/api/setup/settings", json={"servers": edited, "expected_revision": rev})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.module._load_settings()["servers"], list(reversed(servers)))

    def test_schemeless_userinfo_masked_when_token_embeds_a_url(self):
        # #593 QA F1: a "://" later in the token skipped scheme-less redaction.
        stored_in = {"db": "fakeu:fake-pw-20@db.invalid/x?next=http://y.invalid https://a:fake-pw-21@c.invalid"}
        r = self.client.post("/api/setup/settings", json=stored_in)
        got = self.client.get("/api/setup/settings").get_json()["settings"]
        mask = self.module._SECRET_MASK
        self.assertEqual(got["db"], f"{mask}@db.invalid/x?next=http://y.invalid https://{mask}@c.invalid")
        r = self.client.post("/api/setup/settings", json={**got, "expected_revision": r.get_json()["revision"]})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.module._load_settings(), stored_in)

    def test_encoded_at_in_second_url_fails_closed_per_token(self):
        # #593 security review: the fail-closed check ran only on the whole string.
        stored_in = {
            "two_wide": "https://a:fake-p1@h1.invalid https://b:fake-p2＠h2.invalid",
            "two_enc": "https://a:fake-p3@h1.invalid https://b:fake-p4%40h2.invalid",
            "q": "https://h.invalid/x?pwd=fake-p5&session=fake-p6",
        }
        r = self.client.post("/api/setup/settings", json=stored_in)
        got = self.client.get("/api/setup/settings").get_json()["settings"]
        mask = self.module._SECRET_MASK
        body = json.dumps(got, ensure_ascii=False)
        for leaked in ("fake-p1", "fake-p2", "fake-p3", "fake-p4", "fake-p5", "fake-p6"):
            self.assertNotIn(leaked, body)
        self.assertEqual(got["two_wide"], mask)
        r = self.client.post("/api/setup/settings", json={**got, "expected_revision": r.get_json()["revision"]})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.module._load_settings(), stored_in)

    def test_url_secret_variants_never_leak_and_round_trip(self):
        # #593 QA re-check: separators other than whitespace, encoded or
        # lookalike "@" anywhere, and nested "?" query parameters.
        cases = {
            "f1": "fakeu:fake-s01@db.invalid/x?next=http://y.invalid",
            "f1_enc": "fakeu:fake-s02%40db.invalid/x?next=http://y.invalid",
            "f1_wide": "fakeu:fake-s03＠db.invalid/x?next=http://y.invalid",
            "f1_two": "fakeu:fake-s04@db.invalid/x?n=http://y.invalid/z?m=https://q.invalid",
            "f3_space": "https://a:fake-s05@h1.invalid https://b:fake-s06%40h2.invalid",
            "f3_comma": "https://a:fake-s07@h1.invalid,https://b:fake-s08%40h2.invalid",
            "f3_comma_wide": "https://a:fake-s09@h1.invalid,https://b:fake-s10＠h2.invalid",
            "f3_rev_comma": "https://b:fake-s11%40h2.invalid,https://a:fake-s12@h1.invalid",
            "f3_quote": 'https://a:fake-s13@h1.invalid"https://b:fake-s14%40h2.invalid',
            "f3_newline": "https://a:fake-s15@h1.invalid\nhttps://b:fake-s16%40h2.invalid",
            "url_then_schemeless_enc": "https://a:fake-s17@h1.invalid,u:fake-s18%40h2.invalid",
            "sl": "fakedbuser:fake-s19@db.invalid:5432",
            "sl_enc_colon": "fakedbuser%3Afake-s20@db.invalid",
            "sl_two": "u:fake-s21@h1.invalid,v:fake-s22@h2.invalid",
            "sl_quoted": '"u:fake-s23@h.invalid"',
            "q": "https://feed.invalid/x?api_key=fake-s24&page=2",
            "q_frag": "https://a.invalid/cb#access_token=fake-s25",
            "q_nested": "https://a.invalid/login?next=https://b.invalid/cb?access_token=fake-s26",
            "q_schemeless": "db.invalid/x?password=fake-s27",
            "q_upper": "https://a.invalid/?API-KEY=fake-s28",
            "email": "someone@mail.invalid",
        }
        r = self.client.post("/api/setup/settings", json=cases)
        got = self.client.get("/api/setup/settings").get_json()["settings"]
        body = json.dumps(got, ensure_ascii=False)
        for value in cases.values():
            for secret in re.findall(r"fake-s\d\d", value):
                self.assertNotIn(secret, body)
        self.assertEqual(got["email"], "someone@mail.invalid")
        r = self.client.post("/api/setup/settings", json={**got, "expected_revision": r.get_json()["revision"]})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.module._load_settings(), cases)

    def test_settings_masking_residuals_599(self):
        # #599: free-text key=value, DSN passwords, spaced and user-only
        # tokens, "?"/";" inside a secret value (N1), and a literal mask in
        # a password defeating the fail-closed check (N2).
        cases = {
            "free": "note api_key=fake-r01 here",
            "free_start": "api_key=fake-r02",
            "dsn": "host=db.invalid password=fake-r03 user=x",
            "dsn_quoted": "host=db.invalid password='fake r04 \\' fake-r05' user=x",
            "dsn_unclosed": "host=db.invalid password='fake-r06 fake-r07",
            "spaced": "fakeu:pass fake-r08@db.invalid",
            "user_only": "fake-r09@git.invalid/org/repo",
            "n1_q": "https://h.invalid/?token=a?fake-r10",
            "n1_semi": "https://h.invalid/?token=a;fake-r11&page=2",
            "n2": "fakeu:fake-r12 x********@db.invalid",
            "n2_enc": "https://a:fake-r13@h1.invalid x:fake-r14********%40h2.invalid",
            "proxy_bypass": "proxy_bypass=1",
            "cache_bypass": "https://h.invalid/?cache_bypass=1",
            "email": "someone@mail.invalid",
            "plain": "mode=fast",
        }
        r = self.client.post("/api/setup/settings", json=cases)
        got = self.client.get("/api/setup/settings").get_json()["settings"]
        mask = self.module._SECRET_MASK
        body = json.dumps(got, ensure_ascii=False)
        for value in cases.values():
            for secret in re.findall(r"fake-r\d\d", value):
                self.assertNotIn(secret, body)
        self.assertEqual(got["free"], f"note api_key={mask} here")
        self.assertEqual(got["dsn"], f"host=db.invalid password={mask} user=x")
        self.assertEqual(got["dsn_quoted"], f"host=db.invalid password={mask} user=x")
        self.assertEqual(got["user_only"], f"{mask}@git.invalid/org/repo")
        self.assertEqual(got["n1_semi"], f"https://h.invalid/?token={mask}&page=2")
        for key in ("proxy_bypass", "cache_bypass", "email", "plain"):
            self.assertEqual(got[key], cases[key])
        r = self.client.post("/api/setup/settings", json={**got, "expected_revision": r.get_json()["revision"]})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.module._load_settings(), cases)

    def test_edited_scalar_keeping_a_mask_is_refused(self):
        # #599 N3: an edited URL kept the mask and saved it literally; a mask
        # posted for a list-valued key silently replaced the list.
        stored_in = {"feed_url": "https://u:fake-pw-n3@feed.invalid/a",
                     "urls": ["https://u:fake-pw-l@h.invalid"], "ai_api_key": "fake-key-n3"}
        r = self.client.post("/api/setup/settings", json=stored_in)
        rev = r.get_json()["revision"]
        got = self.client.get("/api/setup/settings").get_json()["settings"]
        mask = self.module._SECRET_MASK
        for edit in ({"feed_url": got["feed_url"].replace("/a", "/b")},
                     {"urls": mask},
                     {"ai_api_key": mask + "x"}):
            r = self.client.post("/api/setup/settings", json={**edit, "expected_revision": rev})
            self.assertEqual(r.status_code, 400, edit)
            self.assertEqual(r.get_json()["code"], "settings_unmatched_mask")
        self.assertEqual(self.module._load_settings(), stored_in)
        # The unchanged echo and the whole-secret mask still restore.
        r = self.client.post("/api/setup/settings", json={**got, "expected_revision": rev})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.module._load_settings(), stored_in)

    def test_ambiguous_masked_list_entry_is_refused(self):
        # #593 QA F2: entries identical once masked were paired by position.
        urls = ["https://alice:fake-pw-a@h.invalid", "https://bob:fake-pw-b@h.invalid"]
        r = self.client.post("/api/setup/settings", json={"urls": urls})
        rev = r.get_json()["revision"]
        got = self.client.get("/api/setup/settings").get_json()["settings"]
        self.assertEqual(got["urls"][0], got["urls"][1])
        r = self.client.post("/api/setup/settings", json={"urls": got["urls"][1:], "expected_revision": rev})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["code"], "settings_unmatched_mask")
        self.assertEqual(self.module._load_settings()["urls"], urls)
        # An unchanged round trip still restores both exactly.
        r = self.client.post("/api/setup/settings", json={**got, "expected_revision": rev})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.module._load_settings()["urls"], urls)

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

    def test_posting_masked_secret_keeps_saved_value(self):
        # #522: the GET mask echoed back must not replace the saved secret.
        self.env_file.write_text("PLEX_TOKEN=fake-saved-token\nLIDARR_API_KEY=fake-saved-lidarr\n", encoding="utf-8")
        variables = {v["name"]: v for v in self.client.get("/api/setup/env").get_json()["variables"]}
        # Every masked field GET returns uses the one mask POST recognises.
        self.assertEqual(variables["LIDARR_API_KEY"]["saved_value"], self.module._SECRET_MASK)
        self.assertEqual(variables["LIDARR_API_KEY"]["value"], self.module._SECRET_MASK)
        r = self.client.post("/api/setup/env", json={"variables": {
            "LIDARR_API_KEY": variables["LIDARR_API_KEY"]["saved_value"]}})
        self.assertEqual(r.get_json()["saved"], [])
        self.assertIn("LIDARR_API_KEY=fake-saved-lidarr", self.env_file.read_text(encoding="utf-8"))
        # (No non-secret key here: a saved value lands in os.environ and can be
        # captured by config_layers' boot snapshot, pinning it for later tests.)
        r = self.client.post("/api/setup/env", json={"variables": {"PLEX_TOKEN": self.module._SECRET_MASK}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["saved"], [])
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn("PLEX_TOKEN=fake-saved-token", text)
        self.assertNotIn(self.module._SECRET_MASK, text)
        # An explicit clear still works.
        self.client.post("/api/setup/env", json={
            "variables": {"PLEX_TOKEN": self.module._SECRET_MASK}, "clear": ["PLEX_TOKEN"]})
        self.assertNotIn("fake-saved-token", self.env_file.read_text(encoding="utf-8"))

    def test_secret_aliases_are_masked_and_not_saveable(self):
        self.env_file.write_text("QBIT_PASS=fake-saved-qbit\nQB_PASS=fake-saved-qb\n", encoding="utf-8")
        os.environ["QBIT_PASS"] = "fake-env-qbit"
        fakes = ("fake-saved-qbit", "fake-saved-qb", "fake-env-qbit")

        r = self.client.get("/api/setup/env")
        self.assertEqual(r.status_code, 200)
        variables = {item["name"]: item for item in r.get_json()["variables"]}
        self.assertTrue(variables["QBIT_PASS"]["secret"])
        self.assertTrue(variables["QB_PASS"]["secret"])
        for fake in fakes:
            self.assertNotIn(fake, r.get_data(as_text=True))

        r = self.client.post("/api/setup/env", json={"variables": {"DEMO_MODE": "0"}})
        self.assertEqual(r.status_code, 200)
        for fake in fakes:
            self.assertNotIn(fake, r.get_data(as_text=True))

        r = self.client.post("/api/setup/env", json={"variables": {"QBIT_PASS": "fake-new"}})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("fake-new", self.env_file.read_text(encoding="utf-8"))

        # #522: echoing the GET mask for an unsaveable alias is ignored, not a 400.
        r = self.client.post("/api/setup/env", json={"variables": {"QBIT_PASS": self.module._SECRET_MASK}})
        self.assertEqual((r.status_code, r.get_json()["saved"]), (200, []))
        self.assertIn("QBIT_PASS=fake-saved-qbit", self.env_file.read_text(encoding="utf-8"))

    def test_url_query_credential_is_masked_and_survives_an_echo(self):
        raw = "https://ai.test/v1?api_key=fake-query-credential"
        self.env_file.write_text(f"AI_BASE_URL={raw}\n", encoding="utf-8")
        r = self.client.get("/api/setup/env")
        self.assertNotIn("fake-query-credential", r.get_data(as_text=True))
        variables = {item["name"]: item for item in r.get_json()["variables"]}
        self.assertEqual(variables["AI_BASE_URL"]["value"], "https://ai.test/v1")
        r = self.client.post("/api/setup/env", json={"variables": {"AI_BASE_URL": "https://ai.test/v1"}})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("fake-query-credential", r.get_data(as_text=True))
        self.assertIn(raw, self.env_file.read_text(encoding="utf-8"))

    def test_url_fragment_is_masked(self):
        self.env_file.write_text("AI_BASE_URL=https://ai.test/v1#fake-fragment-credential\n", encoding="utf-8")
        r = self.client.get("/api/setup/env")
        self.assertNotIn("fake-fragment-credential", r.get_data(as_text=True))

    def test_diagnostic_text_redacts_every_secret_assignment_form(self):
        for text in (
            "QBIT_PASS=fake-diag", "failed: qb_pass: fake-diag", 'QB_PASS="fake-diag"',
            '{"QB_PASS":"fake-diag"}', "{'qb_pass': 'fake-diag'}", "QB_PASS=fake-diag,fake-diag",
            'QB_PASS="fake-diag fake-diag', "QB_PASS='fake-diag fake-diag", r'{"QB_PASS":"a\"fake-diag"}',
            '{"password":"fake-diag"}', "{'password': 'fake-diag'}", 'password="fake-diag fake-diag',
            '{"QBITTORRENT_PASSWORD":"fake-diag"}', '{"PLEX_TOKEN": "fake-diag"}',
            "PLEX_TOKEN=fake-diag,fake-diag",
            "pass=fake-diag", "db_pass: fake-diag", '{"pass":"fake-diag"}', "passwd=fake-diag", "{'passwd': 'fake-diag'}",
            "pwd=fake-diag", '{"db-pwd": "fake-diag"}', "passphrase=fake-diag", '{"ssh_passphrase":"fake-diag"}',
            '{"Authorization":"Bearer fake-diag"}', "{'authorization': 'Basic fake-diag'}",
            '{"Proxy-Authorization": "fake-diag"}', '"Authorization": Bearer fake-diag', "Authorization=Basic fake-diag",
        ):
            redacted = self.module._redact_diagnostic_text(text)
            self.assertNotIn("fake-diag", redacted, text)
            self.assertEqual(self.module._redact_diagnostic_text(redacted), redacted, text)

    def test_diagnostic_text_keeps_words_that_only_contain_a_secret_name(self):
        for text in ("password_policy=strict", "bypass=true", "compass: north", "passed: 3", "passing=1", "Bearer token expired"):
            self.assertEqual(self.module._redact_diagnostic_text(text), text)

    def test_authorization_header_keeps_its_scheme(self):
        self.assertEqual(self.module._redact_diagnostic_text("Authorization: Bearer fake-diag"), "Authorization: Bearer [redacted]")

    def test_plugins_report_redacts_categories(self):
        plugin = {"name": "x", "note": "QB_PASS=fake-diag", "errors": ['{"password":"fake-diag"}']}
        report = self.module._redact_plugins_report({"plugins": [dict(plugin)], "categories": {"required": [dict(plugin)]}})
        self.assertNotIn("fake-diag", repr(report))

    def test_plugin_routes_redact_their_reports(self):
        plugin = {"name": "x", "note": "QB_PASS=fake-diag", "errors": ["password=fake-diag"]}
        report = {"plugins": [plugin], "categories": {"required": [plugin]}, "summary": {"errors": ["token=fake-diag"]}}
        with mock.patch.object(self.module, "_checked_beets_config_path", return_value=(Path(self.tempdir.name, "config.yaml"), None)), \
                mock.patch.object(self.module, "_setup_csrf_failure", return_value=None), \
                mock.patch("backend.beets_plugins.verify_all_plugins", return_value=report):
            for response in (self.client.get("/api/setup/plugins"), self.client.post("/api/setup/plugins/verify")):
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("fake-diag", response.get_data(as_text=True))

    def test_alias_url_shown_value_posted_back_writes_nothing(self):
        os.environ["QBIT_URL"] = "http://qbit.test:1/x?k=fake-alias-credential"
        r = self.client.get("/api/setup/env")
        self.assertNotIn("fake-alias-credential", r.get_data(as_text=True))
        shown = {item["name"]: item for item in r.get_json()["variables"]}["QBITTORRENT_URL"]["value"]
        self.assertEqual(shown, "http://qbit.test:1/x")
        r = self.client.post("/api/setup/env", json={"variables": {"QBITTORRENT_URL": shown}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["saved"], [])
        self.assertFalse(self.env_file.exists())

    def test_canonical_row_sees_its_alias(self):
        os.environ["QBIT_URL"] = "http://qbit.test:8080"
        variables = {item["name"]: item for item in self.client.get("/api/setup/env").get_json()["variables"]}
        self.assertEqual(variables["QBITTORRENT_URL"]["value"], "http://qbit.test:8080")
        self.assertEqual(variables["QBITTORRENT_URL"]["source"], "environment")

    def test_unread_setting_is_retired_not_editable(self):
        variables = {item["name"] for item in self.client.get("/api/setup/env").get_json()["variables"]}
        self.assertNotIn("BEETS_VERSION_PROBE_TIMEOUT_SECONDS", variables)

    def test_env_only_names_keep_their_api_layer_labels(self):
        self.env_file.write_text("WEBCONTROL_THREADS=4\nQBIT_URL=http://qbit:8080\n", encoding="utf-8")
        variables = {item["name"]: item for item in self.client.get("/api/setup/env").get_json()["variables"]}
        for name in ("WEBCONTROL_THREADS", "QBIT_URL"):
            self.assertEqual(variables[name]["layer"], "unknown", name)
            self.assertEqual(variables[name]["apply"], "restart", name)
            self.assertFalse(variables[name]["editable"], name)
        r = self.client.post("/api/setup/env", json={"variables": {"WEBCONTROL_THREADS": "5"}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("it is a unknown setting", r.get_json()["error"])

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
            "variables": {"AI_MODEL": "claude-3.5-haiku"},
            "clear": [],
        })
        self.assertEqual(r.status_code, 200)
        get_r = self.client.get("/api/setup/env")
        self.assertEqual(get_r.status_code, 200)
        variables = {item["name"]: item for item in get_r.get_json()["variables"]}
        self.assertEqual(variables["AI_MODEL"]["value"], "claude-3.5-haiku")
        # PUID/TZ are Compose-pinned deployment settings: a saved value
        # could never take effect, so the save is rejected (config layers).
        r = self.client.post("/api/setup/env", json={"variables": {"PUID": "2000", "TZ": "Europe/London"}})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(variables["PUID"]["editable"])
        self.assertEqual(variables["PUID"]["layer"], "deployment")

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
            ("ACOUSTID_API_KEY", "Music Services & Metadata", True),
            ("ACOUSTID_USER_KEY", "Music Services & Metadata", True),
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
        self.assertEqual(self.module._mask("ab"), "********")

    def test_mask_reveals_no_characters_or_length(self):
        # FE-16: the old mask kept the first and last two characters.
        for secret in ("sk-1234567890", "a" * 64, "xyz"):
            masked = self.module._mask(secret)
            self.assertEqual(masked, "********")
            self.assertFalse(set(masked) & set(secret.replace("*", "")))

    def test_mask_empty_value(self):
        self.assertEqual(self.module._mask(""), "")

    def test_check_path_reports_missing_configured_path(self):
        result = self.module._check_path("", require_writable=True)
        self.assertFalse(result["exists"])
        self.assertEqual(result["error"], "not configured")


if __name__ == "__main__":
    unittest.main()


class RoutesSetupConfigLayerTests(unittest.TestCase):
    """BI-1/3/7/15/17: saved settings hold only application keys; local path
    checks use container roots and survive a Beets outage."""

    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
        self.client = self.flask_app.test_client()
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.env_file = root / ".env"
        self.example_file = root / ".env.example"
        self.example_file.write_text(
            "PUID=1000\nTZ=UTC\nBEETS_CONFIG_PATH=./beets\nDOWNLOADS_PATH=./downloads\nAI_MODEL=\nPLEX_URL=\n",
            encoding="utf-8",
        )
        self.module._SETUP_ENV_FILE = self.env_file
        self.module._ENV_EXAMPLE_FILE = self.example_file
        self._saved_env = dict(os.environ)
        for var in ("AI_MODEL", "PLEX_URL", "DOWNLOADS_PATH", "BEETS_CONFIG_PATH", "DOWNLOADS_ROOT", "MUSIC_ROOT"):
            os.environ.pop(var, None)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.tempdir.cleanup()
        os.environ.clear()
        os.environ.update(self._saved_env)

    def test_first_save_writes_only_saved_keys_never_the_template(self):
        self.assertFalse(self.env_file.exists())
        r = self.client.post("/api/setup/env", json={"variables": {"AI_MODEL": "m1"}})
        self.assertEqual(r.status_code, 200)
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn("AI_MODEL=m1", text)
        for leaked in ("DOWNLOADS_PATH", "BEETS_CONFIG_PATH", "PUID", "TZ", "PLEX_URL"):
            self.assertNotIn(leaked, text)
        self.assertNotIn("DOWNLOADS_PATH", os.environ)

    def test_save_rejects_every_non_application_layer(self):
        for key, value in (("DOWNLOADS_PATH", "/x"), ("PUID", "2000"), ("WEBCONTROL_PORT", "9000"),
                           ("MUSIC_ROOT", "/srv/music"), ("BEETS_CONFIG", "/config/x.yaml"), ("PLAYLIST_DIR", "/x")):
            r = self.client.post("/api/setup/env", json={"variables": {key: value}})
            self.assertEqual(r.status_code, 400, key)
        self.assertFalse(self.env_file.exists())

    def test_env_payload_reports_layers_apply_mode_and_migration(self):
        body = self.client.get("/api/setup/env").get_json()
        variables = {item["name"]: item for item in body["variables"]}
        self.assertEqual(variables["DOWNLOADS_PATH"]["layer"], "host")
        self.assertFalse(variables["DOWNLOADS_PATH"]["editable"])
        self.assertEqual(variables["DOWNLOADS_PATH"]["apply"], "deploy")
        self.assertEqual(variables["PUID"]["layer"], "deployment")
        self.assertFalse(variables["PUID"]["editable"])
        self.assertEqual(variables["AI_MODEL"]["layer"], "app")
        self.assertTrue(variables["AI_MODEL"]["editable"])
        self.assertEqual(variables["AI_MODEL"]["apply"], "live")
        for dead in ("PLAYLIST_DIR", "BEETS_SQLITE_TIMEOUT", "WEB_MANAGER_PATH"):
            self.assertNotIn(dead, variables)
        self.assertIn("removed_count", body["migration"])

    def test_downloads_check_uses_container_root_not_host_variable(self):
        with mock.patch.dict(os.environ, {"DOWNLOADS_PATH": "./downloads"}):
            report = self.module._local_paths_report(Path("/config/config.yaml"))
        self.assertEqual(Path(report["downloads"]["path"]).as_posix(), "/downloads")
        with mock.patch.dict(os.environ, {"DOWNLOADS_ROOT": "/srv/incoming"}):
            report = self.module._local_paths_report(Path("/config/config.yaml"))
        self.assertEqual(Path(report["downloads"]["path"]).as_posix(), "/srv/incoming")

    def test_config_file_report_is_ok_for_a_readable_file(self):
        cfg = Path(self.tempdir.name) / "config.yaml"
        cfg.write_text("directory: /music\n", encoding="utf-8")
        report = self.module._local_file_report(cfg)
        self.assertTrue(report["ok"])
        self.assertTrue(report["is_file"])
        self.assertFalse(self.module._local_file_report(Path(self.tempdir.name))["ok"])

    def _ok_paths(self):
        ok = {"path": "/x", "exists": True, "is_dir": True, "readable": True, "writable": True, "ok": True}
        return {"config": dict(ok), "music_library": dict(ok), "downloads": dict(ok, path="/y"),
                "beets_config": dict(ok, is_dir=False, is_file=True)}

    def test_beets_unavailable_is_the_single_primary_reason(self):
        from backend.beets_adapter import beets_adapter, BeetsAdapterConnectionError
        with mock.patch.object(beets_adapter, "get_plugin_status", side_effect=BeetsAdapterConnectionError("down")), \
             mock.patch.object(self.module, "_local_paths_report", return_value=self._ok_paths()):
            body = self.client.get("/api/setup/status?refresh=1").get_json()
        self.assertEqual(len(body["blocking_reasons"]), 1, body["blocking_reasons"])
        self.assertTrue(body["blocking_reasons"][0].startswith("Beets inside Web Manager is unavailable"))
        self.assertEqual(body["fpcalc"]["state"], "unknown")
        self.assertEqual(body["beets"]["fpcalc_state"], "unknown")
        self.assertTrue(body["paths"]["downloads"]["writable"])

    def test_beets_unavailable_still_reports_real_local_failures(self):
        from backend.beets_adapter import beets_adapter, BeetsAdapterConnectionError
        paths = self._ok_paths()
        paths["downloads"].update(writable=False, ok=False)
        with mock.patch.object(beets_adapter, "get_plugin_status", side_effect=BeetsAdapterConnectionError("down")), \
             mock.patch.object(self.module, "_local_paths_report", return_value=paths):
            body = self.client.get("/api/setup/status?refresh=1").get_json()
        self.assertEqual(len(body["blocking_reasons"]), 2)
        self.assertTrue(any("downloads" in reason for reason in body["blocking_reasons"]))

    def test_health_ready_beets_down_reports_no_false_local_failures(self):
        from backend.beets_adapter import beets_adapter, BeetsAdapterConnectionError
        with mock.patch.object(beets_adapter, "get_plugin_status", side_effect=BeetsAdapterConnectionError("down")), \
             mock.patch.object(self.module, "_local_paths_report", return_value=self._ok_paths()):
            r = self.client.get("/health/ready")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json()["blocking_reasons"], ["Beets engine unavailable"])
