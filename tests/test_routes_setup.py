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

    def test_health_ready_uses_remote_control_agent_paths(self):
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

    def test_health_ready_fails_closed_when_remote_agent_unavailable(self):
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
        self.assertIn("beets control agent unavailable", body["blocking_reasons"])


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
        with mock.patch.object(self.module.beets_client, "get_status", side_effect=RuntimeError(sensitive)):
            response = self.client.get("/api/setup/status")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["beets"]["diagnostic_error"], "Beets control-agent status request failed.")
        self.assertEqual(body["beets"]["remote_error"], "request_failed")
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
    """Setup-status diagnostics must come from the authenticated Beets control
    agent, not a local beet executable or local Beets imports in the web
    manager container."""

    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
        self.client = self.flask_app.test_client()

    def _paths(self):
        return {
            "config": {"path": "/config", "exists": True, "is_dir": True, "readable": True, "writable": True, "ok": True},
            "music_library": {"path": "/data/media/music", "exists": True, "is_dir": True, "readable": True, "writable": False, "ok": True},
            "downloads": {"path": "/data/torrents/music", "exists": True, "is_dir": True, "readable": True, "writable": True, "ok": True},
            "beets_config": {"path": "/config/config.yaml", "exists": True, "is_file": True, "readable": True, "writable": True, "ok": True},
        }

    def _remote_status(self, **overrides):
        plugins = [
            "musicbrainz", "lastgenre", "listenbrainz", "discpath", "replaygain",
            "chroma", "fetchart", "discogs",
        ]
        status = {
            "status": "ok",
            "service": "beets-control-agent",
            "beets_version": "2.12.0",
            "beet_available": True,
            "plugin_loader_ok": True,
            "plugin_loader_returncode": 0,
            "plugin_loader_timed_out": False,
            "plugin_loader_error": "",
            "configured_plugins": list(plugins),
            "loaded_plugins": list(plugins),
            "installed_plugins": {name: True for name in plugins},
            "plugin_failures": [],
            "pluginpath": ["/config/beetsplug", "/app/beetsplug"],
            "replaygain_backend": "ffmpeg",
            "replaygain_command": "",
            "discogs_token_configured": True,
            "listenbrainz_token_configured": True,
            "fpcalc_available": True,
            "fpcalc_path": "/usr/bin/fpcalc",
            "ffmpeg_available": True,
            "ffmpeg_path": "/usr/bin/ffmpeg",
            "pyacoustid_available": True,
            "capabilities": {
                "fingerprinting": {"available": True},
                "acoustid_lookup": {"available": True},
                "acoustid_submission": {"available": False, "reason": "ACOUSTID_API_KEY is not configured"},
                "musicbrainz_submission": {"available": True},
                "fetchart": {"available": True},
                "replaygain": {"available": True},
                "lastgenre": {"available": True},
                "discogs": {"available": True},
                "listenbrainz": {"available": True},
                "discpath": {"available": True},
            },
            "commands": {
                "submit": {"available": True, "registered": True},
                "mbsubmit": {"available": True, "registered": True},
            },
            "paths": self._paths(),
            "beetsdir": "/config",
        }
        status.update(overrides)
        return status

    def _status_response(self, remote_status=None, *, side_effect=None):
        remote_status = self._remote_status() if remote_status is None else remote_status
        patch_kwargs = {"side_effect": side_effect} if side_effect is not None else {"return_value": remote_status}
        with mock.patch.object(self.module.beets_client, "get_status", **patch_kwargs) as get_status, \
             mock.patch.object(self.module, "_fetchart_namespace_probe", return_value={
                 "installed": False,
                 "importable_in_process": False,
                 "bundled_namespace_merged": False,
             }), \
             mock.patch.object(self.module.subprocess, "run", side_effect=AssertionError("local subprocess was used")), \
             mock.patch.object(self.module.shutil, "which", side_effect=AssertionError("local executable lookup was used")):
            response = self.client.get("/api/setup/status")
        return response, get_status

    def test_successful_remote_status_reports_version_plugins_and_capabilities(self):
        response, get_status = self._status_response()

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["beets"]["version"], "2.12.0")
        self.assertTrue(body["beets"]["remote_reachable"])
        self.assertTrue(body["beets"]["plugin_loader_ok"])
        self.assertIn("fetchart", body["beets"]["loaded_plugins"])
        self.assertIn("chroma", body["beets"]["loaded_plugins"])
        self.assertTrue(body["beets"]["capabilities"]["fingerprinting"]["available"])
        self.assertTrue(body["beets"]["capabilities"]["acoustid_lookup"]["available"])
        self.assertFalse(body["beets"]["capabilities"]["acoustid_submission"]["available"])
        self.assertTrue(body["beets"]["commands"]["submit"]["available"])
        self.assertTrue(body["beets"]["commands"]["mbsubmit"]["available"])
        # MusicBrainz is core Beets metadata capability, not a togglable
        # plugin -- "connected" reflects control-agent/plugin-loader health,
        # never plugins: list membership (see _musicbrainz_integration_status).
        self.assertEqual(body["integrations"]["musicbrainz"]["state"], "connected")
        self.assertEqual(body["integrations"]["musicbrainz"]["category"], "service")
        self.assertEqual(body["integrations"]["fetchart"]["category"], "beets_plugin")
        self.assertEqual(body["integrations"]["acoustid"]["state"], "configured")
        self.assertEqual(body["integrations"]["fetchart"]["state"], "configured")
        self.assertTrue(body["integrations"]["fetchart"]["operational"])
        self.assertFalse(body["integrations"]["fetchart"]["importable_in_process"])
        self.assertEqual(body["integrations"]["replaygain"]["state"], "configured")
        get_status.assert_called_once_with()

    def test_status_reuses_one_remote_snapshot_for_all_integrations(self):
        response, get_status = self._status_response()

        self.assertEqual(response.status_code, 200)
        get_status.assert_called_once_with()

    def test_remote_connection_failure_fails_closed(self):
        response, _ = self._status_response(
            side_effect=self.module.BeetsUnavailableError("connection refused token=super-secret-key")
        )

        body = response.get_json()
        self.assertFalse(body["beets"]["available"])
        self.assertFalse(body["beets"]["plugin_loader_ok"])
        self.assertEqual(body["beets"]["remote_error"], "unavailable")
        # remote_reachable is False here (control agent itself unreachable),
        # which _musicbrainz_integration_status reports as "unavailable" --
        # a more accurate state than "plugin_loader_failed" (that state
        # means the agent WAS reached but its plugin loader failed).
        self.assertEqual(body["integrations"]["musicbrainz"]["state"], "unavailable")
        self.assertNotIn("super-secret-key", response.get_data(as_text=True))

    def test_remote_authentication_failure_fails_closed(self):
        response, _ = self._status_response(
            side_effect=self.module.BeetsAuthError("401 token=super-secret-key")
        )

        body = response.get_json()
        self.assertFalse(body["beets"]["remote_reachable"])
        self.assertEqual(body["beets"]["remote_error"], "authentication_failed")
        self.assertFalse(body["beets"]["plugin_loader_ok"])
        self.assertNotIn("super-secret-key", response.get_data(as_text=True))

    def test_remote_timeout_fails_closed(self):
        response, _ = self._status_response(
            side_effect=self.module.BeetsUnavailableError("timed out contacting agent")
        )

        body = response.get_json()
        self.assertFalse(body["beets"]["plugin_loader_ok"])
        self.assertTrue(body["beets"]["plugin_loader_timed_out"])
        self.assertEqual(body["beets"]["remote_error"], "timeout")

    def test_malformed_remote_response_shape_fails_closed(self):
        response, _ = self._status_response({
            "status": "ok",
            "configured_plugins": "musicbrainz",
            "loaded_plugins": [],
        })

        body = response.get_json()
        self.assertFalse(body["beets"]["plugin_loader_ok"])
        self.assertEqual(body["beets"]["remote_error"], "malformed_response")
        self.assertEqual(body["beets"]["configured_plugins"], [])

    def test_wrong_capability_shape_fails_closed(self):
        response, _ = self._status_response(self._remote_status(capabilities=[]))

        body = response.get_json()
        self.assertFalse(body["beets"]["plugin_loader_ok"])
        self.assertEqual(body["beets"]["remote_error"], "malformed_response")

    def test_failed_fetchart_is_dependency_missing_and_redacted(self):
        status = self._remote_status(
            plugin_loader_ok=False,
            plugin_loader_returncode=1,
            plugin_loader_error="error loading plugin fetchart token=super-secret-key",
            loaded_plugins=["musicbrainz", "chroma", "replaygain", "discpath"],
            plugin_failures=["error loading plugin fetchart token=super-secret-key"],
        )
        response, _ = self._status_response(status)

        body = response.get_json()
        self.assertEqual(body["integrations"]["fetchart"]["state"], "dependency_plugin_missing")
        self.assertFalse(body["integrations"]["fetchart"]["operational"])
        self.assertNotIn("super-secret-key", response.get_data(as_text=True))

    def test_missing_chroma_does_not_block_independent_submission_commands(self):
        loaded = [p for p in self._remote_status()["loaded_plugins"] if p != "chroma"]
        response, _ = self._status_response(self._remote_status(loaded_plugins=loaded))

        body = response.get_json()
        self.assertEqual(body["integrations"]["acoustid"]["state"], "dependency_plugin_missing")
        self.assertTrue(body["beets"]["commands"]["submit"]["available"])
        self.assertTrue(body["beets"]["commands"]["mbsubmit"]["available"])

    def test_submit_and_mbsubmit_readiness_are_independent(self):
        status = self._remote_status(commands={
            "submit": {"available": False, "registered": False, "reason": "acoustid disabled"},
            "mbsubmit": {"available": True, "registered": True},
        })
        response, _ = self._status_response(status)

        body = response.get_json()
        self.assertFalse(body["beets"]["commands"]["submit"]["available"])
        self.assertTrue(body["beets"]["commands"]["mbsubmit"]["available"])

    def test_replaygain_backend_readiness_uses_remote_backend_tools(self):
        status = self._remote_status(ffmpeg_available=False, ffmpeg_path="")
        status["capabilities"]["replaygain"] = {
            "configured": True,
            "loaded": True,
            "available": False,
            "backend": "ffmpeg",
            "command": "",
            "ffmpeg_available": False,
        }
        response, _ = self._status_response(status)

        body = response.get_json()
        self.assertEqual(body["integrations"]["replaygain"]["state"], "dependency_plugin_missing")

    def test_replaygain_authoritative_command_capability_reports_configured(self):
        status = self._remote_status(
            replaygain_backend="command",
            replaygain_command="/usr/bin/mp3gain",
            ffmpeg_available=False,
            ffmpeg_path="",
        )
        status["capabilities"]["replaygain"] = {
            "configured": True,
            "loaded": True,
            "available": True,
            "backend": "command",
            "command": "/usr/bin/mp3gain",
            "ffmpeg_available": False,
        }
        response, _ = self._status_response(status)

        body = response.get_json()
        replaygain = body["integrations"]["replaygain"]
        self.assertEqual(replaygain["state"], "configured")
        self.assertIn("command backend (/usr/bin/mp3gain)", replaygain["note"])

    def test_discpath_custom_plugin_readiness_requires_remote_loaded_plugin(self):
        loaded = [p for p in self._remote_status()["loaded_plugins"] if p != "discpath"]
        response, _ = self._status_response(self._remote_status(loaded_plugins=loaded))

        body = response.get_json()
        self.assertEqual(body["integrations"]["discpath"]["state"], "dependency_plugin_missing")

    def test_optional_credentials_absent_does_not_block_setup(self):
        status = self._remote_status(
            discogs_token_configured=False,
            listenbrainz_token_configured=False,
        )
        stale_keys = (
            "OPENAI_API_KEY", "OPENROUTER_API_KEY", "AI_API_KEY",
            "DISCOGS_TOKEN", "DISCOGS_USER_TOKEN", "LISTENBRAINZ_TOKEN",
            "ACOUSTID_API_KEY", "ACOUSTID_KEY",
        )
        with mock.patch.dict(os.environ, {key: "" for key in stale_keys}, clear=False):
            response, _ = self._status_response(status)

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
