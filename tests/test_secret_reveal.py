"""Tests for on-demand secret reveal on the System / Environment page.

Covers three layers:
- RevealRouteLogicTests: route-level behavior (name validation, revealable
  gating, source precedence, /api/setup/env staying masked) against the
  lightweight stub-app harness shared with tests/test_routes_setup.py.
- RevealAuthenticationTests: authentication/reauthentication against the
  real Flask app (session/bearer auth, password-confirm reveal window,
  headless/token-only installs skipping the password prompt, rate limiting,
  no secret leakage into logs).
- FrontendWiringTests: source-level assertions that System.tsx keeps
  "viewing" and "editing" state genuinely separate (Show never marks a
  field dirty or feeds Save, Hide/timeout/tab-hidden/unmount/after-save all
  clear the revealed plaintext from component state).
"""
import importlib
import io
import json
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SYSTEM_SOURCE = (ROOT / "frontend" / "src" / "views" / "System.tsx").read_text(encoding="utf-8")
CLIENT_SOURCE = (ROOT / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
TYPES_SOURCE = (ROOT / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")

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
    """Matches tests/test_routes_setup.py's own harness exactly, so the two
    files exercise the identical route-registration path."""
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


class RevealRouteLogicTests(unittest.TestCase):
    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
        self.client = self.flask_app.test_client()
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.env_file = root / ".env"
        self.module._SETUP_ENV_FILE = self.env_file
        self._saved_env = dict(os.environ)
        for var in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "AI_API_KEY", "PLEX_TOKEN", "DISCOGS_TOKEN", "BEETS_WEB_AUTH_TOKEN"):
            os.environ.pop(var, None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    # 1. Configured revealable secret is marked revealable in the catalog.
    def test_configured_revealable_secret_is_marked_revealable(self):
        self.env_file.write_text("OPENAI_API_KEY=sk-test-value-0001\n", encoding="utf-8")
        r = self.client.get("/api/setup/env")
        variables = {v["name"]: v for v in r.get_json()["variables"]}
        self.assertTrue(variables["OPENAI_API_KEY"]["revealable"])
        self.assertTrue(variables["OPENAI_API_KEY"]["configured"])

    # 2. Unconfigured secret: reveal endpoint refuses to return a value.
    def test_unconfigured_revealable_secret_reveal_returns_not_configured(self):
        r = self.client.get("/api/setup/env")
        variables = {v["name"]: v for v in r.get_json()["variables"]}
        self.assertTrue(variables["DISCOGS_TOKEN"]["revealable"])
        self.assertFalse(variables["DISCOGS_TOKEN"]["configured"])

        r = self.client.post("/api/setup/env/DISCOGS_TOKEN/reveal")
        self.assertEqual(r.status_code, 404)
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertFalse(body.get("configured"))

    # 3. BEETS_WEB_PASSWORD never exposes a Show-equivalent: revealable is
    # always false, and the reveal endpoint refuses it outright even if
    # asked directly, regardless of whether it happens to be configured.
    def test_password_is_never_revealable(self):
        self.env_file.write_text("", encoding="utf-8")
        r = self.client.get("/api/setup/env")
        variables = {v["name"]: v for v in r.get_json()["variables"]}
        self.assertFalse(variables["BEETS_WEB_PASSWORD"]["revealable"])

        r = self.client.post("/api/setup/env/BEETS_WEB_PASSWORD/reveal")
        self.assertEqual(r.status_code, 403)
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertNotIn("value", body)

    # 5. Reject a real, non-secret variable name.
    def test_reveal_rejects_non_secret_variable(self):
        r = self.client.post("/api/setup/env/AI_MODEL/reveal")
        self.assertEqual(r.status_code, 404)
        self.assertFalse(r.get_json()["ok"])
        self.assertNotIn("value", r.get_json())

    # 6. Reject an unknown/arbitrary name -- not just "not secret", genuinely
    # unrecognized, including an attempt to probe unrelated process env vars.
    def test_reveal_rejects_unknown_variable(self):
        for bogus in ("NOT_A_REAL_SETTING", "PATH", "HOME"):
            with self.subTest(name=bogus):
                r = self.client.post(f"/api/setup/env/{bogus}/reveal")
                self.assertIn(r.status_code, (404, 400))
                self.assertFalse(r.get_json()["ok"])

    def test_reveal_rejects_path_traversal_attempt(self):
        # A `/`-containing name can never match the <name> route segment at
        # all -- confirms this resolves to a routing-level 404, never a 200
        # with a leaked value from outside the settings catalog.
        r = self.client.post("/api/setup/env/../../etc/passwd/reveal")
        self.assertEqual(r.status_code, 404)

    # 7. Reveal returns only the one requested secret's value, never others.
    def test_reveal_returns_only_the_requested_secret(self):
        self.env_file.write_text(
            "OPENAI_API_KEY=sk-only-this-one-0001\nPLEX_TOKEN=plex-should-not-appear-0002\n",
            encoding="utf-8",
        )
        r = self.client.post("/api/setup/env/OPENAI_API_KEY/reveal")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["name"], "OPENAI_API_KEY")
        self.assertEqual(body["value"], "sk-only-this-one-0001")
        raw = json.dumps(body)
        self.assertNotIn("plex-should-not-appear-0002", raw)
        self.assertEqual(set(body.keys()), {"ok", "name", "value", "configured"})

    # 8. GET and POST /api/setup/env never return plaintext, even when a
    # revealable secret is configured and reveal has already been used.
    def test_setup_env_never_returns_plaintext(self):
        self.env_file.write_text("OPENAI_API_KEY=sk-staymasked1\n", encoding="utf-8")
        self.client.post("/api/setup/env/OPENAI_API_KEY/reveal")

        r_get = self.client.get("/api/setup/env")
        self.assertNotIn("sk-staymasked1", json.dumps(r_get.get_json()))

        r_post = self.client.post("/api/setup/env", json={"variables": {"AI_MODEL": "gpt-4o"}})
        self.assertNotIn("sk-staymasked1", json.dumps(r_post.get_json()))
        variables = {v["name"]: v for v in r_post.get_json()["variables"]}
        self.assertIsNone(variables["OPENAI_API_KEY"]["effective_value"])

    # 9. A process-environment override reveals the environment value, not
    # a stale persisted one -- same precedence /api/setup/env already shows.
    def test_reveal_prefers_environment_over_persisted(self):
        self.env_file.write_text("OPENAI_API_KEY=persisted-stale-value\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "environment-live-value"}, clear=False):
            r = self.client.get("/api/setup/env")
            variables = {v["name"]: v for v in r.get_json()["variables"]}
            self.assertEqual(variables["OPENAI_API_KEY"]["source"], "environment")

            r = self.client.post("/api/setup/env/OPENAI_API_KEY/reveal")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["value"], "environment-live-value")

    # 10. No environment override: reveal returns the persisted value.
    def test_reveal_returns_persisted_value_when_no_environment_override(self):
        self.env_file.write_text("PLEX_TOKEN=persisted-plex-token-0001\n", encoding="utf-8")
        r = self.client.get("/api/setup/env")
        variables = {v["name"]: v for v in r.get_json()["variables"]}
        self.assertEqual(variables["PLEX_TOKEN"]["source"], "persisted")

        r = self.client.post("/api/setup/env/PLEX_TOKEN/reveal")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["value"], "persisted-plex-token-0001")

    # BEETS_WEB_AUTH_TOKEN specifically: persisted via its own dedicated
    # generated-token file, not the generic .env path -- confirm reveal
    # reads that file when .env has no override, matching the real
    # deployment topology this feature was built to reflect accurately.
    def test_reveal_reads_dedicated_auth_token_file_when_not_in_env(self):
        token_file = Path(self.tempdir.name) / ".auth_token"
        token_file.write_text("dedicated-file-token-value-0001", encoding="utf-8")
        with mock.patch.object(self.module, "_GENERATED_AUTH_TOKEN_FILE", token_file), \
             mock.patch.object(self.module, "_FALLBACK_AUTH_TOKEN_FILE", token_file):
            r = self.client.post("/api/setup/env/BEETS_WEB_AUTH_TOKEN/reveal")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["value"], "dedicated-file-token-value-0001")

    # 15. Clear still explicitly deletes the secret -- unaffected by the
    # reveal feature's addition.
    def test_clear_still_deletes_secret(self):
        self.env_file.write_text("PLEX_TOKEN=to-be-cleared-0001\n", encoding="utf-8")
        r = self.client.post("/api/setup/env", json={"variables": {}, "clear": ["PLEX_TOKEN"]})
        self.assertEqual(r.status_code, 200)
        variables = {v["name"]: v for v in r.get_json()["variables"]}
        self.assertFalse(variables["PLEX_TOKEN"]["configured"])
        r = self.client.post("/api/setup/env/PLEX_TOKEN/reveal")
        self.assertEqual(r.status_code, 404)

    # 16. Replacement behavior remains unchanged -- a typed replacement still
    # actually replaces the persisted value (which reveal would then return).
    def test_replacement_behavior_unchanged(self):
        self.env_file.write_text("PLEX_TOKEN=old-value-0001\n", encoding="utf-8")
        r = self.client.post("/api/setup/env", json={"variables": {"PLEX_TOKEN": "new-value-0002"}})
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/api/setup/env/PLEX_TOKEN/reveal")
        self.assertEqual(r.get_json()["value"], "new-value-0002")

    def test_reveal_sets_no_store_cache_headers(self):
        self.env_file.write_text("OPENAI_API_KEY=sk-cacheheader1\n", encoding="utf-8")
        r = self.client.post("/api/setup/env/OPENAI_API_KEY/reveal")
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")
        self.assertEqual(r.headers.get("Pragma"), "no-cache")


class RevealAuthenticationTests(unittest.TestCase):
    """Exercises reveal against the real app.py (real session/bearer auth,
    real _enforce_security_boundary before_request hook, real password
    hashing) -- the stub-app harness above has none of that registered."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_dir = Path(self.tmpdir.name)

        import app as app_module
        import routes_setup

        self.app_module = app_module
        self.routes_setup = routes_setup

        self.initial_pwd_file = self.data_dir / ".initial_admin_password"
        self.persisted_pwd_file = self.data_dir / ".browser_password"
        self.persisted_user_file = self.data_dir / ".browser_username"
        self.setup_state_file = self.data_dir / ".browser_setup_state"
        self.auth_token_file = self.data_dir / ".auth_token"
        self.setup_complete_file = self.data_dir / ".setup_complete"
        self.env_file = self.data_dir / ".env"

        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "BEETS_WEB_PASSWORD": "",
                "BEETS_WEB_USERNAME": "",
                "BEETS_WEB_AUTH_TOKEN": "valid_test_bearer_token_32_chars_minimum!",
                "BEETS_WEB_AUTH_DISABLED": "0",
                "WEB_MANAGER_DATA_DIR": str(self.data_dir),
                "OPENAI_API_KEY": "",
            },
            clear=False,
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        self.patches = [
            mock.patch.object(app_module, "WEB_MANAGER_DATA_DIR", self.data_dir),
            mock.patch.object(app_module, "_INITIAL_BROWSER_PASSWORD_FILE", self.initial_pwd_file),
            mock.patch.object(app_module, "_PERSISTED_BROWSER_PASSWORD_FILE", self.persisted_pwd_file),
            mock.patch.object(app_module, "_PERSISTED_BROWSER_USERNAME_FILE", self.persisted_user_file),
            mock.patch.object(app_module, "_BROWSER_SETUP_STATE_FILE", self.setup_state_file),
            mock.patch.object(app_module, "_GENERATED_AUTH_TOKEN_FILE", self.auth_token_file),
            mock.patch.object(routes_setup, "_SETUP_COMPLETE_MARKER", self.setup_complete_file),
            mock.patch.object(routes_setup, "_SETTINGS_FILE", self.data_dir / "app_settings.json"),
            mock.patch.object(routes_setup, "_SETUP_ENV_FILE", self.env_file),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])

        app_module.app.config["TESTING"] = True
        app_module._AUTH_RATE_LIMITS.clear()
        self.headers = {"X-Beets-CSRF": "1", "Origin": "http://localhost"}
        self.client = app_module.app.test_client()

    def _claim_admin(self, password="correct horse battery staple"):
        self.client.post("/api/setup/first-run", json={"username": "admin", "password": password}, headers=self.headers)
        return password

    # 4. Reveal requires an authenticated admin session -- a direct,
    # unauthenticated request is rejected the same way every other
    # sensitive route family already is.
    def test_reveal_requires_authentication(self):
        self.persisted_user_file.write_text("admin", encoding="utf-8")
        from werkzeug.security import generate_password_hash
        self.persisted_pwd_file.write_text(generate_password_hash("correct horse battery staple"), encoding="utf-8")
        self.setup_state_file.write_text("claimed", encoding="utf-8")
        self.setup_complete_file.write_text("complete", encoding="utf-8")

        unauth = self.app_module.app.test_client()
        r = unauth.post("/api/setup/env/OPENAI_API_KEY/reveal", headers=self.headers)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json()["error"], "Authentication required")

    def test_reveal_auth_endpoint_requires_authentication(self):
        self.persisted_user_file.write_text("admin", encoding="utf-8")
        from werkzeug.security import generate_password_hash
        self.persisted_pwd_file.write_text(generate_password_hash("correct horse battery staple"), encoding="utf-8")
        self.setup_state_file.write_text("claimed", encoding="utf-8")
        self.setup_complete_file.write_text("complete", encoding="utf-8")

        unauth = self.app_module.app.test_client()
        r = unauth.post("/api/setup/env/reveal-auth", json={"password": "whatever"}, headers=self.headers)
        self.assertEqual(r.status_code, 401)

    def test_reveal_requires_password_confirmation_when_browser_password_exists(self):
        password = self._claim_admin()
        self.env_file.write_text("OPENAI_API_KEY=sk-needs-reauth-0001\n", encoding="utf-8")

        r = self.client.post("/api/setup/env/OPENAI_API_KEY/reveal", headers=self.headers)
        self.assertEqual(r.status_code, 401)
        body = r.get_json()
        self.assertTrue(body.get("reauth_required"))
        self.assertNotIn("value", body)

        r = self.client.post("/api/setup/env/reveal-auth", json={"password": password}, headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

        r = self.client.post("/api/setup/env/OPENAI_API_KEY/reveal", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["value"], "sk-needs-reauth-0001")

    def test_reveal_auth_window_covers_multiple_subsequent_reveals(self):
        password = self._claim_admin()
        self.env_file.write_text(
            "OPENAI_API_KEY=sk-first-0001\nPLEX_TOKEN=plex-second-0002\n", encoding="utf-8",
        )
        self.client.post("/api/setup/env/reveal-auth", json={"password": password}, headers=self.headers)

        r1 = self.client.post("/api/setup/env/OPENAI_API_KEY/reveal", headers=self.headers)
        r2 = self.client.post("/api/setup/env/PLEX_TOKEN/reveal", headers=self.headers)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)

    def test_reveal_auth_rejects_wrong_password(self):
        self._claim_admin()
        r = self.client.post("/api/setup/env/reveal-auth", json={"password": "definitely-not-it"}, headers=self.headers)
        self.assertEqual(r.status_code, 401)
        self.assertFalse(r.get_json()["ok"])

        # Confirms the wrong password never opened a reveal window.
        self.env_file.write_text("OPENAI_API_KEY=sk-still-locked-0001\n", encoding="utf-8")
        r = self.client.post("/api/setup/env/OPENAI_API_KEY/reveal", headers=self.headers)
        self.assertEqual(r.status_code, 401)
        self.assertTrue(r.get_json().get("reauth_required"))

    def test_reveal_skips_password_prompt_for_token_only_headless_install(self):
        """No browser password configured at all (pure Bearer-token auth) --
        reveal must never ask for BEETS_WEB_PASSWORD, matching the
        application's existing auth modes."""
        self.setup_state_file.write_text("legacy_established", encoding="utf-8")
        self.setup_complete_file.write_text("complete", encoding="utf-8")
        self.env_file.write_text("OPENAI_API_KEY=sk-headless-0001\n", encoding="utf-8")
        bearer = {"Authorization": "Bearer valid_test_bearer_token_32_chars_minimum!", **self.headers}
        client = self.app_module.app.test_client()
        r = client.post("/api/setup/env/OPENAI_API_KEY/reveal", headers=bearer)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["value"], "sk-headless-0001")

    def test_reveal_auth_failures_are_rate_limited(self):
        self._claim_admin()
        responses = []
        for _ in range(40):
            r = self.client.post("/api/setup/env/reveal-auth", json={"password": "wrong"}, headers=self.headers)
            responses.append(r.status_code)
        self.assertIn(429, responses)

    # 17. No plaintext secret value appears in application logs across a
    # full reveal cycle (reauth failure, reauth success, successful reveal,
    # reveal of an unconfigured/unknown/non-secret name).
    def test_no_plaintext_secret_in_logs_across_reveal_cycle(self):
        marker = "MARKER-DO-NOT-LEAK-SECRET-0001"
        password = self._claim_admin()
        self.env_file.write_text(f"OPENAI_API_KEY={marker}\n", encoding="utf-8")

        log_capture = io.StringIO()
        handler = logging.StreamHandler(log_capture)
        self.app_module.app.logger.addHandler(handler)
        try:
            self.client.post("/api/setup/env/reveal-auth", json={"password": "wrong-first"}, headers=self.headers)
            self.client.post("/api/setup/env/reveal-auth", json={"password": password}, headers=self.headers)
            self.client.post("/api/setup/env/OPENAI_API_KEY/reveal", headers=self.headers)
            self.client.post("/api/setup/env/NOT_A_SETTING/reveal", headers=self.headers)
            self.client.post("/api/setup/env/AI_MODEL/reveal", headers=self.headers)
        finally:
            self.app_module.app.logger.removeHandler(handler)

        self.assertNotIn(marker, log_capture.getvalue())
        self.assertNotIn("wrong-first", log_capture.getvalue())
        self.assertNotIn(password, log_capture.getvalue())


class FrontendWiringTests(unittest.TestCase):
    """Source-level assertions that System.tsx keeps viewing and editing
    state genuinely separate, and never persists a revealed value anywhere
    but ephemeral component state."""

    def test_revealed_state_is_a_separate_object_from_form(self):
        self.assertIn("const [revealedSecrets, setRevealedSecrets] = useState<Record<string, string>>({});", SYSTEM_SOURCE)
        self.assertIn("const [form, setForm] = useState<Record<string, string>>({});", SYSTEM_SOURCE)

    # 11/12. Show must never mark a field dirty or feed Save: the `dirty`
    # memo and saveEnv's payload-building loop must only ever reference
    # `form`/`clearNames`, never `revealedSecrets`.
    def test_dirty_check_never_references_revealed_secrets(self):
        fn = SYSTEM_SOURCE[SYSTEM_SOURCE.index("const dirty = useMemo"):SYSTEM_SOURCE.index("const saveEnv = async")]
        self.assertNotIn("revealedSecrets", fn)

    def test_save_payload_never_references_revealed_secrets(self):
        start = SYSTEM_SOURCE.index("const saveEnv = async")
        end = SYSTEM_SOURCE.index("\n  };", start)
        fn = SYSTEM_SOURCE[start:end]
        self.assertNotIn("revealedSecrets[", fn)
        self.assertNotIn("revealedSecrets)", fn)
        self.assertIn("hideAllSecrets();", fn)

    def test_reveal_never_writes_into_form_state(self):
        start = SYSTEM_SOURCE.index("const performReveal = useCallback")
        end = SYSTEM_SOURCE.index("const handleShowSecret", start)
        fn = SYSTEM_SOURCE[start:end]
        self.assertNotIn("setForm", fn)

    # 13. Hide removes the plaintext from component state (not merely a
    # type=password toggle -- the value itself is deleted from state).
    def test_hide_deletes_plaintext_from_state(self):
        start = SYSTEM_SOURCE.index("const hideSecret = useCallback")
        end = SYSTEM_SOURCE.index("const hideAllSecrets", start)
        fn = SYSTEM_SOURCE[start:end]
        self.assertIn("delete next[name];", fn)

    # 14. An automatic timeout hides (and thus deletes) the revealed value.
    def test_automatic_timeout_hides_revealed_value(self):
        self.assertIn("REVEAL_AUTO_HIDE_MS = 45_000", SYSTEM_SOURCE)
        self.assertIn("window.setTimeout(() => hideSecret(name), REVEAL_AUTO_HIDE_MS)", SYSTEM_SOURCE)

    def test_hides_on_tab_inactive_and_on_unmount(self):
        self.assertIn("document.addEventListener('visibilitychange', onVisibilityChange)", SYSTEM_SOURCE)
        self.assertIn("if (document.hidden) hideAllSecrets();", SYSTEM_SOURCE)
        # Unmount cleanup: the plain useEffect(() => { return () => {...} }, [])
        # right after the timer/state declarations clears every pending timer.
        self.assertIn("revealTimers.current.forEach((id) => window.clearTimeout(id));", SYSTEM_SOURCE)

    def test_show_button_only_renders_when_revealable_and_configured(self):
        self.assertIn("const canReveal = variable.secret && variable.revealable === true && variable.configured;", SYSTEM_SOURCE)

    def test_copy_button_only_renders_while_revealed(self):
        # The Copy button lives inside the `isRevealed &&` block, never
        # alongside the masked "Configured" badge.
        revealed_block_start = SYSTEM_SOURCE.index("{isRevealed && (")
        revealed_block_end = SYSTEM_SOURCE.index("{revealError &&", revealed_block_start)
        block = SYSTEM_SOURCE[revealed_block_start:revealed_block_end]
        self.assertIn("Copy", block)

    def test_no_console_log_of_revealed_value(self):
        self.assertNotIn("console.log(revealedValue", SYSTEM_SOURCE)
        self.assertNotIn("console.log(revealedSecrets", SYSTEM_SOURCE)
        self.assertNotIn("console.log(result.value", SYSTEM_SOURCE)

    def test_revealed_value_never_written_to_browser_storage(self):
        # No actual localStorage/sessionStorage *calls* anywhere in the file
        # (a design-rationale comment mentioning the words is fine).
        for token in ("localStorage.setItem", "localStorage[", "sessionStorage.setItem", "sessionStorage["):
            self.assertNotIn(token, SYSTEM_SOURCE)

    def test_client_sends_reveal_requests_to_narrow_endpoint(self):
        self.assertIn("/api/setup/env/${encodeURIComponent(name)}/reveal", CLIENT_SOURCE)
        self.assertIn("/api/setup/env/reveal-auth", CLIENT_SOURCE)
        # No bulk/all-secrets reveal endpoint anywhere in the client.
        self.assertNotIn("/api/setup/env/reveal-all", CLIENT_SOURCE)

    def test_types_mark_revealable_as_optional_boolean(self):
        self.assertIn("revealable?: boolean;", TYPES_SOURCE)


if __name__ == "__main__":
    unittest.main()
