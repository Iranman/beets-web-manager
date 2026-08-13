"""Regression tests for BUG-1 (v0.1.12): /api/config returned 500 in the
real deployed two-service topology because it read/wrote a local
/config/config.yaml path that only ever existed on the Beets *engine*
container, not the web-manager container -- they have separate /config
mounts since the 2026-07-28 architecture cutover. Routes now proxy through
beets_client to the control agent's /config endpoints, matching the
architecture already used for library reads/writes.

Covers: config readable, config unavailable (engine unreachable/timeout),
permission denied, malformed/engine-error response, and that secret
redaction still happens before content reaches the browser.
"""
import json
import tempfile
import threading
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

import app as app_module
import backend.beets_control_agent as control_agent_module
from backend.beets_client import BeetsAuthError, BeetsError, BeetsUnavailableError
from backend.beets_control_agent import ControlAgentHandler


def _split(response):
    """Route view functions return either a bare Response (200 path) or a
    (Response, status) tuple (non-200 path) -- normalize both to
    (status_code, json_body) for direct (non-test-client) view calls."""
    if isinstance(response, tuple):
        resp, status = response
        return status, resp.get_json()
    return response.status_code, response.get_json()


class ConfigReadableTests(unittest.TestCase):
    def test_get_config_success_returns_redacted_content(self):
        raw = (
            "musicbrainz:\n"
            "  ratelimit: 5\n"
            "  pass: mb-password-secret\n"
            "chroma:\n"
            "  apikey: acoustid-secret-value\n"
            "discogs:\n"
            "  user_token: super-secret-value\n"
        )
        with app_module.app.test_request_context("/api/config"), \
             mock.patch.object(app_module.beets_client, "get_config",
                                return_value={"content": raw, "has_backup": True, "backup_ts": 12345.0}):
            response = app_module.get_config()
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertNotIn("super-secret-value", body["content"])
        self.assertNotIn("mb-password-secret", body["content"])
        self.assertNotIn("acoustid-secret-value", body["content"])
        self.assertIn("[REDACTED]", body["content"])
        self.assertTrue(body["redacted"])
        self.assertTrue(body["has_backup"])
        self.assertEqual(body["backup_ts"], 12345.0)

    def test_get_config_calls_engine_not_local_filesystem(self):
        """The route must not touch the local filesystem at all -- proves
        the fix actually changed the data path, not just the error message."""
        with app_module.app.test_request_context("/api/config"), \
             mock.patch.object(app_module.beets_client, "get_config",
                                return_value={"content": "x: 1\n", "has_backup": False, "backup_ts": None}) as mock_get, \
             mock.patch.object(app_module.Path, "read_text", side_effect=AssertionError("must not read local filesystem")):
            response = app_module.get_config()
        self.assertEqual(response.status_code, 200)
        mock_get.assert_called_once_with()


class ConfigUnavailableTests(unittest.TestCase):
    """Engine unreachable/timeout must produce a structured, non-500 response
    -- not the old generic crash, and not a raw exception leak."""

    def test_get_config_engine_unavailable(self):
        with app_module.app.test_request_context("/api/config"), \
             mock.patch.object(app_module.beets_client, "get_config",
                                side_effect=BeetsUnavailableError("Beets Control Agent is unavailable at http://beets:8338: timed out")):
            status, body = _split(app_module.get_config())
        self.assertEqual(status, 503)
        self.assertFalse(body["ok"])
        self.assertEqual(body["code"], "beets_unavailable")
        self.assertNotIn("8338", json.dumps(body))

    def test_save_config_engine_unavailable(self):
        with app_module.app.test_request_context(
            "/api/config", method="POST",
            data=json.dumps({"content": "musicbrainz:\n  ratelimit: 5\n"}),
            content_type="application/json",
        ), mock.patch.object(app_module.beets_client, "save_config", side_effect=BeetsUnavailableError("timeout")):
            status, body = _split(app_module.save_config())
        self.assertEqual(status, 503)
        self.assertEqual(body["code"], "beets_unavailable")

    def test_get_config_engine_auth_failure(self):
        with app_module.app.test_request_context("/api/config"), \
             mock.patch.object(app_module.beets_client, "get_config", side_effect=BeetsAuthError("401")):
            status, body = _split(app_module.get_config())
        self.assertEqual(status, 502)
        self.assertEqual(body["code"], "beets_auth_failed")

    def test_structured_agent_50x_preserves_config_error_code(self):
        with app_module.app.test_request_context("/api/config"), \
             mock.patch.object(
                 app_module.beets_client,
                 "get_config",
                 side_effect=BeetsUnavailableError(
                     "Beets Control Agent error: Could not read config.yaml",
                     error_code="config_read_failed",
                     status_code=500,
                 ),
             ):
            status, body = _split(app_module.get_config())
        self.assertEqual(status, 502)
        self.assertEqual(body["code"], "config_read_failed")
        self.assertNotEqual(body["code"], "beets_unavailable")


class ConfigPermissionDeniedTests(unittest.TestCase):
    def test_get_config_permission_denied(self):
        with app_module.app.test_request_context("/api/config"), \
             mock.patch.object(app_module.beets_client, "get_config",
                                side_effect=BeetsError("Beets API request error: config.yaml is not readable",
                                                        error_code="config_permission_denied", status_code=403)):
            status, body = _split(app_module.get_config())
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], "config_permission_denied")

    def test_get_config_not_found(self):
        with app_module.app.test_request_context("/api/config"), \
             mock.patch.object(app_module.beets_client, "get_config",
                                side_effect=BeetsError("not found", error_code="config_not_found", status_code=404)):
            status, body = _split(app_module.get_config())
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "config_not_found")


class ConfigMalformedResponseTests(unittest.TestCase):
    def test_unrecognized_engine_error_code_falls_back_to_generic_502(self):
        """An error_code the client doesn't recognize (future engine version,
        unexpected proxy error page, ...) must still produce a safe,
        structured response instead of a raw/blank one."""
        with app_module.app.test_request_context("/api/config"), \
             mock.patch.object(app_module.beets_client, "get_config",
                                side_effect=BeetsError("Beets API request error: HTTP 418: <html>...</html>",
                                                        error_code="", status_code=418)):
            status, body = _split(app_module.get_config())
        self.assertEqual(status, 502)
        self.assertFalse(body["ok"])
        self.assertNotIn("<html>", json.dumps(body))


class ConfigSaveValidationTests(unittest.TestCase):
    """Save-time validation is unchanged by BUG-1's fix -- still enforced
    before ever contacting the engine."""

    def test_save_config_rejects_empty_content(self):
        with app_module.app.test_request_context(
            "/api/config", method="POST",
            data=json.dumps({"content": "   "}),
            content_type="application/json",
        ):
            status, body = _split(app_module.save_config())
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "config_empty")

    def test_save_config_rejects_malformed_json_before_engine_call(self):
        with app_module.app.test_request_context(
            "/api/config", method="POST", data="{not-json", content_type="application/json"
        ), mock.patch.object(app_module.beets_client, "save_config") as mock_save:
            status, body = _split(app_module.save_config())
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "config_invalid_json")
        mock_save.assert_not_called()

    def test_save_config_rejects_non_string_content_before_engine_call(self):
        with app_module.app.test_request_context(
            "/api/config", method="POST",
            data=json.dumps({"content": {"path": "not-allowed"}}),
            content_type="application/json",
        ), mock.patch.object(app_module.beets_client, "save_config") as mock_save:
            status, body = _split(app_module.save_config())
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "config_invalid_content")
        mock_save.assert_not_called()

    def test_save_config_rejects_oversized_content_before_engine_call(self):
        with app_module.app.test_request_context(
            "/api/config", method="POST",
            data=json.dumps({"content": "x" * 9}),
            content_type="application/json",
        ), mock.patch.object(app_module, "_CONFIG_CONTENT_MAX_CHARS", 8), \
             mock.patch.object(app_module.beets_client, "save_config") as mock_save:
            status, body = _split(app_module.save_config())
        self.assertEqual(status, 413)
        self.assertEqual(body["code"], "config_too_large")
        mock_save.assert_not_called()

    def test_save_config_rejects_redacted_placeholder_roundtrip(self):
        placeholder = 'discogs:\n  user_token: "[REDACTED]"\n'
        with app_module.app.test_request_context(
            "/api/config", method="POST",
            data=json.dumps({"content": placeholder}),
            content_type="application/json",
        ):
            status, body = _split(app_module.save_config())
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "config_redacted_placeholder")

    def test_save_config_engine_too_large_preserves_413(self):
        with app_module.app.test_request_context(
            "/api/config", method="POST",
            data=json.dumps({"content": "musicbrainz:\n  ratelimit: 5\n"}),
            content_type="application/json",
        ), mock.patch.object(
            app_module.beets_client,
            "save_config",
            side_effect=BeetsError("too large", error_code="config_too_large", status_code=413),
        ):
            status, body = _split(app_module.save_config())
        self.assertEqual(status, 413)
        self.assertEqual(body["code"], "config_too_large")

    def test_save_config_success(self):
        with app_module.app.test_request_context(
            "/api/config", method="POST",
            data=json.dumps({"content": "musicbrainz:\n  ratelimit: 5\n"}),
            content_type="application/json",
        ), mock.patch.object(app_module.beets_client, "save_config",
                              return_value={"ok": True, "backed_up": True}) as mock_save:
            response = app_module.save_config()
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["backed_up"])
        mock_save.assert_called_once_with("musicbrainz:\n  ratelimit: 5\n")


class ConfigRevertTests(unittest.TestCase):
    def test_revert_config_no_backup(self):
        with app_module.app.test_request_context("/api/config/revert", method="POST"), \
             mock.patch.object(app_module.beets_client, "revert_config",
                                side_effect=BeetsError("no backup", error_code="config_backup_not_found", status_code=404)):
            status, body = _split(app_module.revert_config())
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "config_backup_not_found")

    def test_revert_config_success(self):
        with app_module.app.test_request_context("/api/config/revert", method="POST"), \
             mock.patch.object(app_module.beets_client, "revert_config", return_value={"ok": True}):
            response = app_module.revert_config()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])


class ControlAgentConfigEndpointTests(unittest.TestCase):
    """Real handler tests for the new agent-side /config, /config (POST),
    and /config/revert endpoints backing BUG-1's fix -- these operate on
    BEETSDIR (the engine's own mount), never a user-supplied path, so
    there is no traversal surface to test against."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.config_path = root / "config.yaml"
        self.backup_path = root / "config.yaml.bak"
        self.patches = [
            mock.patch.object(control_agent_module, "_AGENT_CONFIG_PATH", str(self.config_path)),
            mock.patch.object(control_agent_module, "_AGENT_CONFIG_BAK_PATH", str(self.backup_path)),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _get(self, path):
        handler = ControlAgentHandler.__new__(ControlAgentHandler)
        handler.headers = {"Authorization": "Bearer test-token"}
        handler._authenticate = lambda: True
        handler.path = path
        responses = []
        handler._send_json = lambda code, data: responses.append((code, data))
        handler.do_GET()
        self.assertEqual(len(responses), 1, f"expected exactly one response for GET {path}")
        return responses[0]

    def _post(self, path, body):
        payload = json.dumps(body).encode("utf-8")
        return self._post_raw(path, payload)

    def _post_raw(self, path, payload, *, content_length=None):
        handler = ControlAgentHandler.__new__(ControlAgentHandler)
        handler.headers = {"Authorization": "Bearer test-token"}
        handler._authenticate = lambda: True
        handler.path = path
        handler.rfile = BytesIO(payload)
        handler.headers["Content-Length"] = str(len(payload) if content_length is None else content_length)
        responses = []
        handler._send_json = lambda code, data: responses.append((code, data))
        handler.do_POST()
        self.assertEqual(len(responses), 1, f"expected exactly one response for POST {path}")
        return responses[0]

    def test_get_config_not_found(self):
        code, data = self._get("/config")
        self.assertEqual(code, 404)
        self.assertEqual(data["error_code"], "config_not_found")

    def test_post_then_get_config_round_trips(self):
        code, data = self._post("/config", {"content": "musicbrainz:\n  ratelimit: 5\n"})
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"])
        self.assertFalse(data["backed_up"], "no prior file existed, so nothing to back up yet")

        code, data = self._get("/config")
        self.assertEqual(code, 200)
        self.assertEqual(data["content"], "musicbrainz:\n  ratelimit: 5\n")
        self.assertFalse(data["has_backup"])

    def test_second_save_creates_backup_and_revert_restores_it(self):
        self._post("/config", {"content": "version: 1\n"})
        code, data = self._post("/config", {"content": "version: 2\n"})
        self.assertEqual(code, 200)
        self.assertTrue(data["backed_up"])

        code, data = self._get("/config")
        self.assertEqual(data["content"], "version: 2\n")
        self.assertTrue(data["has_backup"])

        code, data = self._post("/config/revert", {})
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"])

        code, data = self._get("/config")
        self.assertEqual(data["content"], "version: 1\n")

    def test_revert_without_backup_returns_404(self):
        code, data = self._post("/config/revert", {})
        self.assertEqual(code, 404)
        self.assertEqual(data["error_code"], "config_backup_not_found")

    def test_post_config_rejects_empty_content(self):
        code, data = self._post("/config", {"content": "   "})
        self.assertEqual(code, 400)
        self.assertEqual(data["error_code"], "config_empty")

    def test_post_config_rejects_missing_content_key(self):
        code, data = self._post("/config", {})
        self.assertEqual(code, 400)
        self.assertEqual(data["error_code"], "config_empty")

    def test_post_config_rejects_invalid_content_length(self):
        code, data = self._post_raw("/config", b"{}", content_length="not-an-int")
        self.assertEqual(code, 400)
        self.assertEqual(data["error"], "Invalid Content-Length")

    def test_post_config_rejects_oversized_request_before_reading_file(self):
        with mock.patch.object(control_agent_module, "_AGENT_CONFIG_REQUEST_MAX_BYTES", 10):
            code, data = self._post_raw("/config", b"{" + (b"x" * 64) + b"}")
        self.assertEqual(code, 413)
        self.assertEqual(data["error_code"], "config_too_large")
        self.assertFalse(self.config_path.exists())

    def test_post_config_rejects_oversized_content(self):
        with mock.patch.object(control_agent_module, "_AGENT_CONFIG_CONTENT_MAX_BYTES", 8), \
             mock.patch.object(control_agent_module, "_AGENT_CONFIG_REQUEST_MAX_BYTES", 1024):
            code, data = self._post("/config", {"content": "x" * 9})
        self.assertEqual(code, 413)
        self.assertEqual(data["error_code"], "config_too_large")
        self.assertFalse(self.config_path.exists())

    def test_concurrent_config_writes_are_serialized(self):
        self.config_path.write_text("version: 0\n", encoding="utf-8")
        active_copy = 0
        max_active_copy = 0
        counter_lock = threading.Lock()
        errors = []
        real_copy2 = control_agent_module.shutil.copy2

        def slow_copy2(src, dst, *args, **kwargs):
            nonlocal active_copy, max_active_copy
            with counter_lock:
                active_copy += 1
                max_active_copy = max(max_active_copy, active_copy)
            try:
                time.sleep(0.02)
                return real_copy2(src, dst, *args, **kwargs)
            finally:
                with counter_lock:
                    active_copy -= 1

        def worker(i):
            try:
                control_agent_module._write_agent_config_file(f"version: {i}\n")
            except Exception as exc:  # pragma: no cover - failure path only
                errors.append(exc)

        with mock.patch.object(control_agent_module.shutil, "copy2", side_effect=slow_copy2):
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(max_active_copy, 1, "config backup/write operations must not overlap under ThreadingHTTPServer")
        self.assertIn(self.config_path.read_text(encoding="utf-8"), {f"version: {i}\n" for i in range(1, 8)})
        self.assertEqual(list(self.config_path.parent.glob("config.yaml.tmp.*")), [])

    def test_get_config_permission_denied_is_translated(self):
        with mock.patch.object(control_agent_module, "_read_agent_config_file",
                                side_effect=PermissionError("denied")):
            code, data = self._get("/config")
        self.assertEqual(code, 403)
        self.assertEqual(data["error_code"], "config_permission_denied")

    def test_get_config_unexpected_oserror_is_sanitized(self):
        with mock.patch.object(control_agent_module, "_read_agent_config_file",
                                side_effect=OSError("LEAK_MARKER /some/host/path")):
            code, data = self._get("/config")
        self.assertEqual(code, 500)
        self.assertEqual(data["error_code"], "config_read_failed")
        # detail is redacted via _redact_agent_status_text, but the path
        # itself isn't a recognized secret pattern -- assert the *message*
        # field (what a caller displays) never carries raw exception text.
        self.assertNotIn("LEAK_MARKER", data["error"])


if __name__ == "__main__":
    unittest.main()
