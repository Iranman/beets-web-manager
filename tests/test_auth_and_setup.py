"""Comprehensive test suite for First-Run Setup & Sign-In Page functionality.

Covers:
- First-run detection and redirection rules
- Inaccessibility of setup to unauthenticated users after setup completion
- Password hashing storage verification
- Login, logout, session cookie authorization
- Route protection (protected APIs return 401 JSON)
- Secrets masking in APIs
- MusicBrainz operation without AI configuration
- Beets connectivity requirement for setup completion
"""
import base64
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
import routes_setup
try:  # ARCH-001: patch app.py and the modules extracted from it
    from _app_family import patch_app_family  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_family import patch_app_family  # noqa: E402


class AuthAndSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmpdir.name)

        self.initial_pwd_file = self.data_dir / ".initial_admin_password"
        self.persisted_pwd_file = self.data_dir / ".browser_password"
        self.persisted_user_file = self.data_dir / ".browser_username"
        self.setup_state_file = self.data_dir / ".browser_setup_state"
        self.auth_token_file = self.data_dir / ".auth_token"
        self.setup_complete_file = self.data_dir / ".setup_complete"
        self.session_epoch_file = self.data_dir / ".session_epoch"

        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "BEETS_WEB_PASSWORD": "",
                "BEETS_WEB_USERNAME": "",
                "BEETS_WEB_AUTH_TOKEN": "valid_test_bearer_token_32_chars_minimum!",
                "BEETS_WEB_AUTH_DISABLED": "0",
                "WEB_MANAGER_DATA_DIR": str(self.data_dir),
            },
            clear=False,
        )
        self.env_patch.start()

        self.patches = [
            patch_app_family(app_module, "WEB_MANAGER_DATA_DIR", self.data_dir),
            patch_app_family(app_module, "_INITIAL_BROWSER_PASSWORD_FILE", self.initial_pwd_file),
            patch_app_family(app_module, "_PERSISTED_BROWSER_PASSWORD_FILE", self.persisted_pwd_file),
            patch_app_family(app_module, "_PERSISTED_BROWSER_USERNAME_FILE", self.persisted_user_file),
            patch_app_family(app_module, "_SESSION_EPOCH_FILE", self.session_epoch_file),
            patch_app_family(app_module, "_BROWSER_SETUP_STATE_FILE", self.setup_state_file),
            patch_app_family(app_module, "_GENERATED_AUTH_TOKEN_FILE", self.auth_token_file),
            mock.patch.object(routes_setup, "_SETUP_COMPLETE_MARKER", self.setup_complete_file),
            mock.patch.object(routes_setup, "_SETTINGS_FILE", self.data_dir / "app_settings.json"),
            mock.patch.object(routes_setup, "_SETUP_ENV_FILE", self.data_dir / ".env"),
        ]
        for p in self.patches:
            p.start()

        app_module.app.config["TESTING"] = True
        app_module._AUTH_RATE_LIMITS.clear()
        self.client = app_module.app.test_client()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.env_patch.stop()
        self.tmpdir.cleanup()

    def test_first_run_setup_redirection_and_completion_flow(self):
        """Uncompleted setup requires first-run setup; completed setup locks unauthenticated setup."""
        # 1. Initially setup is incomplete
        self.assertTrue(app_module._first_run_setup_required())

        # Public setup status endpoint works during setup
        status_resp = self.client.get("/api/setup/status")
        self.assertEqual(status_resp.status_code, 200)
        self.assertTrue(status_resp.get_json()["first_run"]["required"])

        # Protected API endpoint requires auth
        lib_resp = self.client.get("/api/library")
        self.assertEqual(lib_resp.status_code, 401)
        self.assertEqual(lib_resp.get_json()["error"], "First-run setup in progress. Authentication required for this endpoint.")

        headers = {"X-Beets-CSRF": "1", "Origin": "http://localhost"}

        # 2. Complete admin user creation via first-run
        admin_pass = "correct horse battery staple"
        first_run_resp = self.client.post("/api/setup/first-run", json={"username": "admin", "password": admin_pass}, headers=headers)
        self.assertEqual(first_run_resp.status_code, 200)
        self.assertTrue(first_run_resp.get_json()["ok"])

        # Password must be stored as a werkzeug hash, not plaintext!
        stored_pass = self.persisted_pwd_file.read_text(encoding="utf-8").strip()
        self.assertNotEqual(stored_pass, admin_pass)
        self.assertTrue(stored_pass.startswith(("scrypt:", "pbkdf2:", "argon2:")))

        # 3. Mark setup complete
        with mock.patch.object(routes_setup, "_beets_plugin_diagnostics", return_value={"remote_reachable": True}):
            complete_resp = self.client.post("/api/setup/complete", headers=headers)
        self.assertEqual(complete_resp.status_code, 200)
        self.assertTrue(complete_resp.get_json()["ok"])

        # Setup is now complete
        self.assertFalse(app_module._first_run_setup_required())

        # 4. Trying to call setup/first-run again unauthenticated returns 409
        subsequent_first_run = self.client.post("/api/setup/first-run", json={"username": "hacker", "password": "hacker password passphrase"}, headers=headers)
        self.assertEqual(subsequent_first_run.status_code, 409)

    def test_login_logout_session_flow(self):
        """User can log in via POST /api/login, receive a session cookie, and log out via POST /api/logout."""
        headers = {"X-Beets-CSRF": "1", "Origin": "http://localhost"}
        admin_pass = "correct horse battery staple"
        self.client.post("/api/setup/first-run", json={"username": "admin", "password": admin_pass}, headers=headers)
        with mock.patch.object(routes_setup, "_beets_plugin_diagnostics", return_value={"remote_reachable": True}):
            self.client.post("/api/setup/complete", headers=headers)

        # 1. Invalid login attempt
        bad_login = self.client.post("/api/login", json={"username": "admin", "password": "WrongPassword123!@#4567890AaBb"}, headers=headers)
        self.assertEqual(bad_login.status_code, 401)
        self.assertIn("Incorrect username or password", bad_login.get_json()["error"])

        # 2. Successful login attempt
        good_login = self.client.post("/api/login", json={"username": "admin", "password": admin_pass}, headers=headers)
        self.assertEqual(good_login.status_code, 200)
        self.assertTrue(good_login.get_json()["ok"])

        # 3. Check auth/me with session cookie
        me_resp = self.client.get("/api/auth/me")
        self.assertEqual(me_resp.status_code, 200)
        self.assertTrue(me_resp.get_json()["authenticated"])
        self.assertEqual(me_resp.get_json()["username"], "admin")

        # 4. Access protected API endpoint with session cookie
        lib_resp = self.client.get("/api/auth/me")
        self.assertEqual(lib_resp.status_code, 200)

        # 5. Logout
        logout_resp = self.client.post("/api/logout", headers=headers)
        self.assertEqual(logout_resp.status_code, 200)

        # 6. Check auth/me after logout
        me_logout = self.client.get("/api/auth/me")
        self.assertEqual(me_logout.status_code, 200)
        self.assertFalse(me_logout.get_json()["authenticated"])

    def test_failed_login_rate_limit_returns_429_and_valid_login_recovers(self):
        """Repeated bad logins receive 429; correct credentials are refused while limited and work again after the window."""
        headers = {"X-Beets-CSRF": "1", "Origin": "http://localhost"}
        admin_pass = "correct horse battery staple"
        self.client.post("/api/setup/first-run", json={"username": "admin", "password": admin_pass}, headers=headers)
        with mock.patch.object(routes_setup, "_beets_plugin_diagnostics", return_value={"remote_reachable": True}):
            self.client.post("/api/setup/complete", headers=headers)
        self.client.post("/api/logout", headers=headers)

        app_module._AUTH_RATE_LIMITS.clear()
        with mock.patch.dict(os.environ, {"BEETS_AUTH_RATE_LIMIT": "5", "BEETS_AUTH_RATE_WINDOW": "10"}, clear=False):
            for _ in range(5):
                res = self.client.post("/api/login", json={"username": "admin", "password": "wrong password"}, headers=headers)
                self.assertEqual(res.status_code, 401)
                self.assertEqual(res.get_json()["error"], "Incorrect username or password.")

            limited = self.client.post("/api/login", json={"username": "admin", "password": "wrong password"}, headers=headers)
            self.assertEqual(limited.status_code, 429)
            self.assertIn("Retry-After", limited.headers)

            # SEC-3: while limited, even the correct password is refused
            # (otherwise the limiter never slows a brute force) ...
            still_limited = self.client.post("/api/login", json={"username": "admin", "password": admin_pass}, headers=headers)
            self.assertEqual(still_limited.status_code, 429)

            # ... and the correct password works again once the window passes.
            import backend.auth_service as _auth_service
            later = _auth_service.time.time() + 11
            with mock.patch.object(_auth_service.time, "time", return_value=later):
                recovered = self.client.post("/api/login", json={"username": "admin", "password": admin_pass}, headers=headers)
            self.assertEqual(recovered.status_code, 200)
            self.assertTrue(recovered.get_json()["ok"])

    def test_secrets_excluded_and_masked_in_apis(self):
        """Secrets are masked in setup env and status APIs."""
        headers = {"X-Beets-CSRF": "1", "Origin": "http://localhost"}
        self.client.post("/api/setup/first-run", json={"username": "admin", "password": "correct horse battery staple"}, headers=headers)
        secret_key = "dummy-test-key-sample-1234567890"
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": secret_key}):
            env_resp = self.client.get("/api/setup/env")
            self.assertEqual(env_resp.status_code, 200)
            variables = env_resp.get_json()["variables"]
            openai_var = next((v for v in variables if v["name"] == "OPENAI_API_KEY"), None)
            self.assertIsNotNone(openai_var)
            self.assertTrue(openai_var["secret"])
            self.assertNotIn(secret_key, openai_var["value"])
            self.assertTrue("*" in openai_var["value"])

    def test_setup_complete_requires_authenticated_admin(self):
        """Anonymous callers cannot mark setup complete before claiming admin credentials."""
        headers = {"X-Beets-CSRF": "1", "Origin": "http://localhost"}
        res = self.client.post("/api/setup/complete", headers=headers)
        self.assertEqual(res.status_code, 401)
        self.assertFalse(self.setup_complete_file.exists())

    def test_first_run_creates_session_for_setup_continuation(self):
        """The same browser session that creates admin credentials can finish setup."""
        headers = {"X-Beets-CSRF": "1", "Origin": "http://localhost"}
        admin_pass = "correct horse battery staple"
        first_run_resp = self.client.post("/api/setup/first-run", json={"username": "admin", "password": admin_pass}, headers=headers)
        self.assertEqual(first_run_resp.status_code, 200)
        me_resp = self.client.get("/api/auth/me")
        self.assertTrue(me_resp.get_json()["authenticated"])
        with mock.patch.object(routes_setup, "_beets_plugin_diagnostics", return_value={"remote_reachable": True}):
            complete_resp = self.client.post("/api/setup/complete", headers=headers)
        self.assertEqual(complete_resp.status_code, 200)
        self.assertTrue(self.setup_complete_file.exists())

    def test_setup_completion_marker_not_written_if_state_persistence_fails(self):
        """Setup completion is not reported or marked durable if state persistence fails."""
        headers = {"X-Beets-CSRF": "1", "Origin": "http://localhost"}
        admin_pass = "correct horse battery staple"
        self.client.post("/api/setup/first-run", json={"username": "admin", "password": admin_pass}, headers=headers)
        with mock.patch.object(routes_setup, "_beets_plugin_diagnostics", return_value={"remote_reachable": True}), \
             patch_app_family(app_module, "_set_browser_setup_state", return_value=False):
            res = self.client.post("/api/setup/complete", headers=headers)
        self.assertEqual(res.status_code, 500)
        self.assertFalse(self.setup_complete_file.exists())
    def test_setup_completion_allows_only_one_success(self):
        """Concurrent setup completion requests must have exactly one winner."""
        headers = {"X-Beets-CSRF": "1", "Origin": "http://localhost"}
        admin_pass = "correct horse battery staple"
        self.client.post("/api/setup/first-run", json={"username": "admin", "password": admin_pass}, headers=headers)
        self.client.post("/api/logout", headers=headers)
        auth = base64.b64encode(f"admin:{admin_pass}".encode("utf-8")).decode("utf-8")
        request_headers = {**headers, "Authorization": f"Basic {auth}"}
        results = []
        lock = threading.Lock()

        def worker():
            client = app_module.app.test_client()
            response = client.post("/api/setup/complete", headers=request_headers)
            with lock:
                results.append(response.status_code)

        # Patch once around both threads, not once per thread: unittest.mock's
        # patch save/restore is a stack on the target attribute, not
        # thread-safe for concurrent enter/exit of the *same* target -- two
        # threads independently entering/exiting their own
        # `mock.patch.object(routes_setup, "_beets_plugin_diagnostics", ...)`
        # can interleave as enter(A) enter(B) exit(A) exit(B), which restores
        # "original" B's saved value (A's mock, captured when B entered) as
        # the final value instead of the true original -- permanently
        # replacing routes_setup._beets_plugin_diagnostics with a stale
        # MagicMock for every later test in the process. A real incident:
        # this leaked a `{"remote_reachable": True}` stub that broke
        # test_v0110_upgrade_and_matched_stack whenever it ran later in the
        # same suite. One patch shared by both threads has a single
        # enter/exit and cannot race with itself.
        with mock.patch.object(routes_setup, "_beets_plugin_diagnostics", return_value={"remote_reachable": True}):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertEqual(sorted(results), [200, 409])
        self.assertTrue(self.setup_complete_file.exists())

    def test_remember_me_controls_session_cookie_expiration(self):
        """remember=false creates a session cookie; remember=true creates a persistent cookie."""
        headers = {"X-Beets-CSRF": "1", "Origin": "http://localhost"}
        admin_pass = "correct horse battery staple"
        self.client.post("/api/setup/first-run", json={"username": "admin", "password": admin_pass}, headers=headers)
        with mock.patch.object(routes_setup, "_beets_plugin_diagnostics", return_value={"remote_reachable": True}):
            self.client.post("/api/setup/complete", headers=headers)
        self.client.post("/api/logout", headers=headers)

        session_login = self.client.post("/api/login", json={"username": "admin", "password": admin_pass, "remember": False}, headers=headers)
        session_cookie = "\n".join(session_login.headers.get_all("Set-Cookie"))
        self.assertNotIn("Expires=", session_cookie)
        self.client.post("/api/logout", headers=headers)

        remembered_login = self.client.post("/api/login", json={"username": "admin", "password": admin_pass, "remember": True}, headers=headers)
        remembered_cookie = "\n".join(remembered_login.headers.get_all("Set-Cookie"))
        self.assertIn("Expires=", remembered_cookie)

    def test_session_user_key_alone_does_not_authorize(self):
        """A stale or partial session must not authorize protected endpoints without authenticated=True."""
        headers = {"X-Beets-CSRF": "1", "Origin": "http://localhost"}
        admin_pass = "correct horse battery staple"
        self.client.post("/api/setup/first-run", json={"username": "admin", "password": admin_pass}, headers=headers)
        with mock.patch.object(routes_setup, "_beets_plugin_diagnostics", return_value={"remote_reachable": True}):
            self.client.post("/api/setup/complete", headers=headers)
        self.client.post("/api/logout", headers=headers)
        with self.client.session_transaction() as sess:
            sess["user"] = "admin"
        res = self.client.get("/api/library")
        self.assertEqual(res.status_code, 401)

    def test_sensitive_api_families_reject_direct_unauthenticated_requests(self):
        """Representative backend-only sensitive routes return JSON 401 without auth."""
        from werkzeug.security import generate_password_hash

        self.persisted_user_file.write_text("admin", encoding="utf-8")
        self.persisted_pwd_file.write_text(generate_password_hash("correct horse battery staple"), encoding="utf-8")
        self.setup_state_file.write_text("claimed", encoding="utf-8")
        self.setup_complete_file.write_text("complete", encoding="utf-8")
        self.assertFalse(app_module._first_run_setup_required())

        unauth = app_module.app.test_client()
        representative_routes = [
            ("get", "/api/jobs", None),
            ("get", "/api/config", None),
            ("get", "/api/wanted/lidarr", None),
            ("get", "/api/submissions/target?path=/tmp/review", None),
            ("post", "/api/submissions/reference-url", {"url": "https://example.com", "path": "/tmp/review"}),
            ("post", "/api/import/review-folder/delete", {"path": "/tmp/review"}),
            ("post", "/api/plugins/run", {"plugin": "fetchart"}),
            ("post", "/api/transactions/settings", {"enabled": True}),
        ]
        for method, route, body in representative_routes:
            with self.subTest(route=route):
                request_fn = getattr(unauth, method)
                response = request_fn(route, json=body) if body is not None else request_fn(route)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.content_type.split(";", 1)[0], "application/json")
                self.assertEqual(response.get_json()["error"], "Authentication required")
    def test_setup_env_password_mirror_is_hashed(self):
        """Saving BEETS_WEB_PASSWORD mirrors a hash to .browser_password, not plaintext."""
        headers = {"X-Beets-CSRF": "1", "Origin": "http://localhost"}
        admin_pass = "correct horse battery staple"
        self.client.post("/api/setup/first-run", json={"username": "admin", "password": admin_pass}, headers=headers)
        new_pass = "another durable passphrase"
        res = self.client.post("/api/setup/env", json={"variables": {"BEETS_WEB_PASSWORD": new_pass}}, headers=headers)
        self.assertEqual(res.status_code, 200)
        persisted = self.persisted_pwd_file.read_text(encoding="utf-8").strip()
        self.assertNotEqual(persisted, new_pass)
        self.assertTrue(persisted.startswith(("scrypt:", "pbkdf2:", "argon2:")))
        env_text = (self.data_dir / ".env").read_text(encoding="utf-8")
        self.assertNotIn(new_pass, env_text)
        self.assertIn("BEETS_WEB_PASSWORD=", env_text)

    def test_flask_secret_key_is_persisted_when_generated(self):
        """The generated Flask session secret survives process/container recreation."""
        secret_file = self.data_dir / ".flask_secret_key"
        with patch_app_family(app_module, "_FLASK_SECRET_KEY_FILE", secret_file), \
             mock.patch.dict(os.environ, {"BEETS_WEB_SECRET_KEY": ""}, clear=False):
            first = app_module._get_or_create_flask_secret_key()
            second = app_module._get_or_create_flask_secret_key()
        self.assertEqual(first, second)
        self.assertEqual(secret_file.read_text(encoding="utf-8").strip(), first)
        self.assertGreaterEqual(len(first), 32)

    def test_first_run_connection_probes_require_csrf(self):
        """First-run public connection probes require same-origin intent."""
        no_csrf = self.client.post("/api/setup/test/beets")
        self.assertEqual(no_csrf.status_code, 403)

        headers = {"X-Beets-CSRF": "1", "Origin": "http://localhost"}
        from backend.beets_adapter import beets_adapter
        with mock.patch.object(beets_adapter, "get_stats", return_value={"items": 5, "albums": 1}):
            ok_resp = self.client.post("/api/setup/test/beets", headers=headers)
        self.assertEqual(ok_resp.status_code, 200)
        self.assertTrue(ok_resp.get_json()["ok"])

    def test_stock_beets_down_fails_closed(self):
        """A2 fail-closed requirement: the primary stock Beets read test must
        FAIL when stock Beets (:8337) is unreachable. There is no legacy
        transport left that could ever make this test pass instead."""
        headers = {"X-Beets-CSRF": "1", "Origin": "http://localhost"}
        from backend.beets_adapter import beets_adapter, BeetsAdapterConnectionError
        with mock.patch.object(beets_adapter, "get_stats", side_effect=BeetsAdapterConnectionError("stock Beets unreachable")):
            resp = self.client.post("/api/setup/test/beets", headers=headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["status"], "failed")

    def test_stock_beets_up_but_plugin_unreachable_still_reports_connected(self):
        """The integration plugin handshake is independent: a plugin failure
        must not fail the primary stock Beets read test, but must be visible
        in its own sub-object."""
        headers = {"X-Beets-CSRF": "1", "Origin": "http://localhost"}
        from backend.beets_adapter import beets_adapter, BeetsAdapterAuthError
        with mock.patch.object(beets_adapter, "get_stats", return_value={"items": 5, "albums": 1}), \
             mock.patch.object(beets_adapter, "get_plugin_status", side_effect=BeetsAdapterAuthError("bad token")):
            resp = self.client.post("/api/setup/test/beets", headers=headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["plugin_status"]["ok"])

    def test_beets_unreachable_blocks_setup_completion_in_production(self):
        """If Beets engine is unreachable and not in testing mode, setup completion is blocked."""
        headers = {"X-Beets-CSRF": "1", "Origin": "http://localhost"}
        admin_pass = "correct horse battery staple"
        self.client.post("/api/setup/first-run", json={"username": "admin", "password": admin_pass}, headers=headers)
        app_module.app.config["TESTING"] = False
        try:
            with mock.patch.object(routes_setup, "_beets_plugin_diagnostics", return_value={"remote_reachable": False}):
                res = self.client.post("/api/setup/complete", headers=headers)
            self.assertEqual(res.status_code, 400)
            self.assertFalse(res.get_json()["ok"])
            self.assertIn("Beets engine control agent is unreachable", res.get_json()["error"])
            self.assertFalse(self.setup_complete_file.exists())
        finally:
            app_module.app.config["TESTING"] = True


class AccountCredentialChangeTests(AuthAndSetupTests):
    """#753 D2: authenticated in-app change of browser username/password via
    POST /api/setup/account. Inherits the real-app fixture (tmp data dir,
    patched credential files) from AuthAndSetupTests."""

    HEADERS = {"X-Beets-CSRF": "1", "Origin": "http://localhost"}

    def _bootstrap_authenticated(self, password="correct horse battery staple", username="admin"):
        """First-run creates the credential and an authenticated session; mark
        setup complete so the endpoint runs under the post-setup auth boundary."""
        self.client.post("/api/setup/first-run", json={"username": username, "password": password}, headers=self.HEADERS)
        with mock.patch.object(routes_setup, "_beets_plugin_diagnostics", return_value={"remote_reachable": True}):
            self.client.post("/api/setup/complete", headers=self.HEADERS)
        self.assertFalse(app_module._first_run_setup_required())

    def test_change_password_rehashes_and_rotates_effective_credential(self):
        old = "correct horse battery staple"
        new = "a-totally-different-passphrase-9x"
        self._bootstrap_authenticated(password=old)

        resp = self.client.post(
            "/api/setup/account",
            json={"current_password": old, "password": new},
            headers=self.HEADERS,
        )
        self.assertEqual(resp.status_code, 200, resp.get_json())
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["password_changed"])
        self.assertFalse(body["username_changed"])
        # The new password must never be echoed anywhere in the response.
        self.assertNotIn(new, resp.get_data(as_text=True))

        # Stored as a werkzeug hash, not plaintext.
        stored = self.persisted_pwd_file.read_text(encoding="utf-8").strip()
        self.assertNotEqual(stored, new)
        self.assertTrue(stored.startswith(("scrypt:", "pbkdf2:", "argon2:")))

        # Old password no longer authenticates; the new one does.
        self.client.post("/api/logout", headers=self.HEADERS)
        app_module._AUTH_RATE_LIMITS.clear()
        bad = self.client.post("/api/login", json={"username": "admin", "password": old}, headers=self.HEADERS)
        self.assertEqual(bad.status_code, 401)
        good = self.client.post("/api/login", json={"username": "admin", "password": new}, headers=self.HEADERS)
        self.assertEqual(good.status_code, 200, good.get_json())

    def test_wrong_current_password_rejected_and_nothing_changes(self):
        old = "correct horse battery staple"
        self._bootstrap_authenticated(password=old)
        before = self.persisted_pwd_file.read_text(encoding="utf-8")

        resp = self.client.post(
            "/api/setup/account",
            json={"current_password": "not-the-current-password-xyz", "password": "brand-new-passphrase-123456"},
            headers=self.HEADERS,
        )
        self.assertEqual(resp.status_code, 401)
        self.assertIn("Current password is incorrect", resp.get_json()["error"])
        # Untouched.
        self.assertEqual(self.persisted_pwd_file.read_text(encoding="utf-8"), before)

    def test_weak_new_password_rejected_with_requirements(self):
        old = "correct horse battery staple"
        self._bootstrap_authenticated(password=old)
        before = self.persisted_pwd_file.read_text(encoding="utf-8")

        resp = self.client.post(
            "/api/setup/account",
            json={"current_password": old, "password": "short"},
            headers=self.HEADERS,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(resp.get_json().get("unmet_requirements"))
        self.assertEqual(self.persisted_pwd_file.read_text(encoding="utf-8"), before)

    def test_change_username_only_leaves_password_intact(self):
        old = "correct horse battery staple"
        self._bootstrap_authenticated(password=old)
        pwd_before = self.persisted_pwd_file.read_text(encoding="utf-8")

        resp = self.client.post(
            "/api/setup/account",
            json={"current_password": old, "username": "curator"},
            headers=self.HEADERS,
        )
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertTrue(resp.get_json()["username_changed"])
        self.assertFalse(resp.get_json()["password_changed"])
        self.assertEqual(self.persisted_user_file.read_text(encoding="utf-8").strip(), "curator")
        self.assertEqual(self.persisted_pwd_file.read_text(encoding="utf-8"), pwd_before)

    def test_requires_authenticated_session(self):
        self._bootstrap_authenticated()
        self.client.post("/api/logout", headers=self.HEADERS)
        app_module._AUTH_RATE_LIMITS.clear()
        resp = self.client.post(
            "/api/setup/account",
            json={"current_password": "correct horse battery staple", "password": "new-passphrase-abcdefgh"},
            headers=self.HEADERS,
        )
        self.assertEqual(resp.status_code, 401)

    def test_empty_payload_is_rejected(self):
        old = "correct horse battery staple"
        self._bootstrap_authenticated(password=old)
        resp = self.client.post(
            "/api/setup/account",
            json={"current_password": old},
            headers=self.HEADERS,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Provide a new username", resp.get_json()["error"])

    def test_rate_limited_current_password_check_returns_429(self):
        old = "correct horse battery staple"
        self._bootstrap_authenticated(password=old)
        app_module._AUTH_RATE_LIMITS.clear()
        with mock.patch.dict(os.environ, {"BEETS_AUTH_RATE_LIMIT": "5", "BEETS_AUTH_RATE_WINDOW": "10"}, clear=False):
            # The IP auth bucket trips once attempts EXCEED the limit (min 5).
            for _ in range(5):
                r = self.client.post(
                    "/api/setup/account",
                    json={"current_password": "wrong-current", "password": "new-passphrase-abcdefgh"},
                    headers=self.HEADERS,
                )
                self.assertEqual(r.status_code, 401)
            over = self.client.post(
                "/api/setup/account",
                json={"current_password": "wrong-current", "password": "new-passphrase-abcdefgh"},
                headers=self.HEADERS,
            )
            self.assertEqual(over.status_code, 429)
            self.assertIn("Retry-After", over.headers)
            # SEC-3: while limited, even the CORRECT current password is refused.
            still_limited = self.client.post(
                "/api/setup/account",
                json={"current_password": old, "password": "new-passphrase-abcdefgh"},
                headers=self.HEADERS,
            )
            self.assertEqual(still_limited.status_code, 429)

    def test_username_with_unicode_line_separator_is_rejected(self):
        """#759 item 1: the username is read back via .splitlines()[0], so a
        value containing a Unicode line break (U+0085/U+2028/U+2029) would be
        silently truncated on read -- stored != login-effective. Validation
        must reject every such character so the two can never diverge."""
        old = "correct horse battery staple"
        self._bootstrap_authenticated(password=old)
        user_before = self.persisted_user_file.read_text(encoding="utf-8")

        for sep in ("\u0085", " ", " "):
            bad = f"cur{sep}ator"
            # Sanity: this is exactly the truncation the read path would cause.
            self.assertNotEqual(bad.splitlines()[0], bad, sep)
            resp = self.client.post(
                "/api/setup/account",
                json={"current_password": old, "username": bad},
                headers=self.HEADERS,
            )
            self.assertEqual(resp.status_code, 400, (repr(sep), resp.get_json()))
            self.assertIn("Invalid username format", resp.get_json()["error"])

        # No truncated username was ever persisted; the stored value is intact.
        self.assertEqual(self.persisted_user_file.read_text(encoding="utf-8"), user_before)

    def test_password_change_invalidates_other_sessions(self):
        """#759 item 2: rotating the password must force re-auth on every
        OTHER already-authenticated session, while the actor who rotated stays
        signed in and the new credential authenticates."""
        old = "correct horse battery staple"
        new = "a-brand-new-rotation-passphrase-2"
        self._bootstrap_authenticated(password=old)  # self.client authenticated

        # A second, independently authenticated session (another device).
        other = app_module.app.test_client()
        app_module._AUTH_RATE_LIMITS.clear()
        login = other.post("/api/login", json={"username": "admin", "password": old}, headers=self.HEADERS)
        self.assertEqual(login.status_code, 200, login.get_json())
        self.assertTrue(other.get("/api/auth/me").get_json()["authenticated"])

        # Rotate the password from the FIRST session.
        app_module._AUTH_RATE_LIMITS.clear()
        resp = self.client.post(
            "/api/setup/account",
            json={"current_password": old, "password": new},
            headers=self.HEADERS,
        )
        self.assertEqual(resp.status_code, 200, resp.get_json())

        # The OTHER session is now forced to re-auth (stale epoch -> rejected).
        self.assertFalse(other.get("/api/auth/me").get_json()["authenticated"])
        self.assertEqual(other.get("/api/library").status_code, 401)

        # The actor who rotated stays signed in (re-stamped to the new epoch).
        self.assertTrue(self.client.get("/api/auth/me").get_json()["authenticated"])

        # The new credential authenticates a fresh login.
        app_module._AUTH_RATE_LIMITS.clear()
        good = other.post("/api/login", json={"username": "admin", "password": new}, headers=self.HEADERS)
        self.assertEqual(good.status_code, 200, good.get_json())

    def test_username_only_change_does_not_invalidate_other_sessions(self):
        """#759 item 2 is scoped to the PASSWORD change: a username-only
        change must not rotate the epoch, so other sessions stay valid."""
        old = "correct horse battery staple"
        self._bootstrap_authenticated(password=old)

        other = app_module.app.test_client()
        app_module._AUTH_RATE_LIMITS.clear()
        self.assertEqual(
            other.post("/api/login", json={"username": "admin", "password": old}, headers=self.HEADERS).status_code,
            200,
        )
        self.assertTrue(other.get("/api/auth/me").get_json()["authenticated"])

        app_module._AUTH_RATE_LIMITS.clear()
        resp = self.client.post(
            "/api/setup/account",
            json={"current_password": old, "username": "curator"},
            headers=self.HEADERS,
        )
        self.assertEqual(resp.status_code, 200, resp.get_json())
        # Other session remains authorized (no password rotation occurred).
        self.assertTrue(other.get("/api/auth/me").get_json()["authenticated"])

    def test_unreadable_epoch_file_fails_closed(self):
        """#759 item 2 (fail-closed): a present-but-unparseable epoch file must
        reject every session rather than collapse to the baseline epoch 0 and
        silently re-admit an already-revoked cookie."""
        old = "correct horse battery staple"
        self._bootstrap_authenticated(password=old)  # actor authenticated at epoch 0
        self.assertTrue(self.client.get("/api/auth/me").get_json()["authenticated"])
        # Corrupt the epoch state (file exists but is not an integer).
        self.session_epoch_file.write_text("not-an-integer", encoding="utf-8")
        self.assertFalse(self.client.get("/api/auth/me").get_json()["authenticated"])
        self.assertEqual(self.client.get("/api/library").status_code, 401)

    def test_epoch_bump_failure_aborts_password_change(self):
        """#759 item 2 (fail-closed): if the session epoch cannot be advanced,
        the password rotation must abort with nothing changed -- never rotate
        the password while other sessions stay authorized."""
        old = "correct horse battery staple"
        self._bootstrap_authenticated(password=old)
        pwd_before = self.persisted_pwd_file.read_text(encoding="utf-8")

        other = app_module.app.test_client()
        app_module._AUTH_RATE_LIMITS.clear()
        self.assertEqual(
            other.post("/api/login", json={"username": "admin", "password": old}, headers=self.HEADERS).status_code,
            200,
        )
        self.assertTrue(other.get("/api/auth/me").get_json()["authenticated"])

        with mock.patch.object(app_module, "_bump_session_epoch", return_value=False):
            resp = self.client.post(
                "/api/setup/account",
                json={"current_password": old, "password": "a-fresh-rotation-passphrase-9"},
                headers=self.HEADERS,
            )
        self.assertEqual(resp.status_code, 500, resp.get_json())
        # Password untouched, other session still valid, old password still works.
        self.assertEqual(self.persisted_pwd_file.read_text(encoding="utf-8"), pwd_before)
        self.assertTrue(other.get("/api/auth/me").get_json()["authenticated"])
        app_module._AUTH_RATE_LIMITS.clear()
        self.assertEqual(
            self.client.get("/api/auth/me").get_json()["authenticated"], True
        )

    def test_plaintext_env_password_is_cleared_so_new_hash_takes_effect(self):
        """A non-blank BEETS_WEB_PASSWORD in the process env shadows the
        persisted hash; the endpoint must clear it so the change is real."""
        env_pwd = "env-set-admin-password-32chars!!"
        # Establish a completed, non-first-run install whose effective
        # password comes from the env var.
        self.setup_complete_file.write_text("done", encoding="utf-8")
        self.setup_state_file.write_text("claimed", encoding="utf-8")
        with mock.patch.dict(os.environ, {"BEETS_WEB_PASSWORD": env_pwd}, clear=False):
            self.assertTrue(app_module._check_password_value(env_pwd))
            with self.client.session_transaction() as sess:
                sess["authenticated"] = True
                sess["user"] = "admin"
            new = "fresh-in-app-managed-passphrase-7"
            resp = self.client.post(
                "/api/setup/account",
                json={"current_password": env_pwd, "password": new},
                headers=self.HEADERS,
            )
            self.assertEqual(resp.status_code, 200, resp.get_json())
            # Env var cleared in-process and the new hash is now effective.
            self.assertEqual(os.environ.get("BEETS_WEB_PASSWORD"), "")
            self.assertTrue(app_module._check_password_value(new))
            self.assertFalse(app_module._check_password_value(env_pwd))


if __name__ == "__main__":
    unittest.main()
