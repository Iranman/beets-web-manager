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


class ConfigSecretRedactionLinearTimeTests(unittest.TestCase):
    """Regression tests for the CodeQL py/polynomial-redos fix on PR #80:
    _CONFIG_SECRET_LINE_RE (a regex run against untrusted, browser-supplied
    config content) was replaced with a deterministic, linear-time,
    regex-free line scan (_redact_config_line / _redact_config_content /
    _contains_redacted_config_secret). These tests prove behavior parity
    with the old regex-based redaction for all currently supported keys,
    plus that pathological inputs that would have stressed a backtracking
    regex complete in effectively constant time."""

    ALL_SECRET_KEYS = (
        "apikey", "api_key", "api_token", "auth_token", "token", "user_token",
        "pass", "password", "secret", "client_secret", "access_token",
        "refresh_token",
    )

    def test_every_supported_secret_key_is_redacted(self):
        for key in self.ALL_SECRET_KEYS:
            raw = f"{key}: super-secret-value-for-{key}\n"
            out = app_module._redact_config_content(raw)
            self.assertNotIn(f"super-secret-value-for-{key}", out, key)
            self.assertIn(f'{key}: "[REDACTED]"', out, key)

    def test_matching_is_case_insensitive(self):
        raw = "API_TOKEN: MixedCaseSecretValue\nAuth_Token: AnotherSecretValue\n"
        out = app_module._redact_config_content(raw)
        self.assertNotIn("MixedCaseSecretValue", out)
        self.assertNotIn("AnotherSecretValue", out)
        # Original key spelling/case is preserved in the output line.
        self.assertIn('API_TOKEN: "[REDACTED]"', out)
        self.assertIn('Auth_Token: "[REDACTED]"', out)

    def test_indentation_is_preserved(self):
        raw = "discogs:\n    user_token: nested-secret\n"
        out = app_module._redact_config_content(raw)
        self.assertNotIn("nested-secret", out)
        self.assertIn('    user_token: "[REDACTED]"\n', out)

    def test_harmless_keys_are_untouched(self):
        raw = "directory: /data/media/music\nratelimit: 5\nlibrary: /config/musiclibrary.blb\n"
        out = app_module._redact_config_content(raw)
        self.assertEqual(out, raw)

    def test_normal_config_text_is_unchanged_besides_secrets(self):
        raw = (
            "musicbrainz:\n"
            "  ratelimit: 5\n"
            "  pass: mb-password-secret\n"
            "chroma:\n"
            "  apikey: acoustid-secret-value\n"
        )
        out = app_module._redact_config_content(raw)
        self.assertIn("musicbrainz:\n", out)
        self.assertIn("  ratelimit: 5\n", out)
        self.assertIn("chroma:\n", out)
        self.assertNotIn("mb-password-secret", out)
        self.assertNotIn("acoustid-secret-value", out)

    def test_lf_and_crlf_both_preserved(self):
        lf = "pass: secret-lf\nharmless: keep\n"
        crlf = "pass: secret-crlf\r\nharmless: keep\r\n"
        out_lf = app_module._redact_config_content(lf)
        out_crlf = app_module._redact_config_content(crlf)
        self.assertNotIn("\r", out_lf)
        self.assertIn('pass: "[REDACTED]"\n', out_lf)
        self.assertIn("\r\n", out_crlf)
        self.assertIn('pass: "[REDACTED]"\r\n', out_crlf)
        self.assertIn("harmless: keep\r\n", out_crlf)

    def test_very_long_whitespace_heavy_input_completes_quickly(self):
        raw = (" " * 500_000) + "x: y\n" + ("\t" * 200_000) + "\n"
        start = time.monotonic()
        app_module._redact_config_content(raw)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 2.0, "whitespace-heavy input must not exhibit pathological runtime")

    def test_long_non_matching_line_completes_quickly(self):
        raw = ("not_a_secret_key" * 100_000) + ": some_value\n"
        start = time.monotonic()
        app_module._redact_config_content(raw)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 2.0, "a long non-matching key must not exhibit pathological runtime")

    def test_secret_key_with_very_long_value_completes_quickly_and_is_redacted(self):
        raw = "password: " + ("y" * 1_000_000) + "\n"
        start = time.monotonic()
        out = app_module._redact_config_content(raw)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 2.0, "a very long secret value must not exhibit pathological runtime")
        self.assertNotIn("y" * 1000, out)
        self.assertIn('password: "[REDACTED]"', out)

    def test_text_with_no_colon_is_returned_unchanged(self):
        raw = "just some prose with no colon at all\nanother plain line\n"
        self.assertEqual(app_module._redact_config_content(raw), raw)

    def test_text_with_many_colons_completes_quickly(self):
        raw = (":" * 100_000) + "\n"
        start = time.monotonic()
        app_module._redact_config_content(raw)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 2.0, "many colons on one line must not exhibit pathological runtime")

    def test_actual_secret_values_are_absent_from_redacted_output(self):
        raw = (
            "musicbrainz:\n  pass: mb-password-secret\n"
            "chroma:\n  apikey: acoustid-secret-value\n"
            "discogs:\n  user_token: super-secret-value\n"
        )
        out = app_module._redact_config_content(raw)
        for secret in ("mb-password-secret", "acoustid-secret-value", "super-secret-value"):
            self.assertNotIn(secret, out)

    def test_contains_redacted_config_secret_true_for_matching_secret_line(self):
        placeholder = 'discogs:\n  user_token: "[REDACTED]"\n'
        self.assertTrue(app_module._contains_redacted_config_secret(placeholder))

    def test_contains_redacted_config_secret_false_for_normal_config(self):
        raw = "musicbrainz:\n  ratelimit: 5\n"
        self.assertFalse(app_module._contains_redacted_config_secret(raw))

    def test_contains_redacted_config_secret_false_when_placeholder_text_is_not_a_secret_value(self):
        """The literal string "[REDACTED]" appearing somewhere that is not
        the value of a secret key (e.g. free-text notes, or a real secret
        line elsewhere with a real value) must not trip the save-guard --
        only a secret key whose own value carries the placeholder should.
        This is a deliberate precision improvement over the old regex-based
        check, which only tested for the placeholder string's presence
        anywhere in the whole document alongside an unrelated secret-key
        line match."""
        raw = 'pass: a-real-current-password\nnotes: "[REDACTED]" appeared in an old export, unrelated\n'
        self.assertFalse(app_module._contains_redacted_config_secret(raw))

    def test_contains_redacted_config_secret_handles_empty_and_none_input(self):
        self.assertFalse(app_module._contains_redacted_config_secret(""))
        self.assertFalse(app_module._contains_redacted_config_secret(None))


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
