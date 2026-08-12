"""Automated test suite for browser first-run administrator setup & migration safety.

Covers:
- `_first_run_setup_required()` state determination under all credential configurations
- Durable setup state file (.browser_setup_state: fresh / claimed / legacy_established)
- v0.1.8 upgrade migration safety (.initial_admin_password does NOT expose anonymous setup)
- Credential deletion fail-closed protection (missing password on claimed install fails closed, no setup reopen)
- POST /api/setup/first-run endpoint logic & input validation
- Race condition protection (concurrent setup requests allow exactly one winner)
- Credential persistence (.browser_username, .browser_password with 0600 mode)
- Transition from setup mode to normal Basic Auth boundary
- Security boundary enforcement (unauthenticated requests restricted to bootstrap routes only)
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


_VALID_TEST_PASSWORD = "Aa1!" + ("x" * 32)
_WEAK_TEST_PASSWORD = "weakpassword"

# Flask's test client defaults to Host: localhost with no port, so this is a
# genuinely same-origin Origin header for _same_origin_url(). CSRF protection
# requires both a valid same-origin signal AND the X-Beets-CSRF header --
# these tests must exercise the real check, not a weakened header-only one.
_SAME_ORIGIN_CSRF_HEADERS = {"X-Beets-CSRF": "1", "Origin": "http://localhost"}


class FirstRunBrowserBootstrapTests(unittest.TestCase):
    def setUp(self):
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
                "BEETS_WEB_PASSWORD_FILE": "",
                "BEETS_WEB_USERNAME": "",
                "BEETS_WEB_AUTH_TOKEN": "valid_test_bearer_token_32_chars_minimum!",
                "BEETS_WEB_AUTH_DISABLED": "0",
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
            mock.patch.object(routes_setup, "_STATUS_CACHE_DATA", None),
            mock.patch.object(routes_setup, "_STATUS_CACHE_TS", 0.0),
        ]
        for p in self.patches:
            p.start()

        self.client = app_module.app.test_client()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.env_patch.stop()
        self.tmpdir.cleanup()

    def test_fresh_install_behavior(self):
        """A genuinely fresh install enters browser setup mode and does not generate .initial_admin_password."""
        app_module._migrate_or_initialize_setup_state()
        self.assertTrue(app_module._first_run_setup_required())
        self.assertFalse(self.initial_pwd_file.exists())
        self.assertEqual(self.setup_state_file.read_text(encoding="utf-8").strip(), "fresh")

        # GET /api/setup/status reports first_run.required == True
        status_resp = self.client.get("/api/setup/status")
        self.assertEqual(status_resp.status_code, 200)
        self.assertTrue(status_resp.get_json().get("first_run", {}).get("required"))

        # Protected API is denied 401
        self.assertEqual(self.client.get("/api/library").status_code, 401)

        # POST /api/setup/first-run completes setup
        resp = self.client.post(
            "/api/setup/first-run",
            json={"username": "freshadmin", "password": _VALID_TEST_PASSWORD},
            headers=_SAME_ORIGIN_CSRF_HEADERS,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(app_module._first_run_setup_required())
        self.assertEqual(self.setup_state_file.read_text(encoding="utf-8").strip(), "claimed")

        # A different unauthenticated browser is stopped at the auth boundary:
        # once claimed, first-run is no longer a public endpoint at all, so an
        # anonymous caller gets 401, never reaching the route's own 409 check.
        anon_client = app_module.app.test_client()
        resp2 = anon_client.post(
            "/api/setup/first-run",
            json={"username": "hacker", "password": _VALID_TEST_PASSWORD},
            headers=_SAME_ORIGIN_CSRF_HEADERS,
        )
        self.assertEqual(resp2.status_code, 401)

    def test_v018_upgrade_migration_security(self):
        """Scenario: upgrade from v0.1.8 generated-password installation.

        Given:
        - .initial_admin_password exists from v0.1.8
        - no .browser_password
        - no .browser_setup_state marker

        Require:
        - state migrates to legacy_established
        - first_run.required == False
        - GET / does NOT expose anonymous claim setup UI
        - POST /api/setup/first-run is rejected (401: not a public endpoint once established)
        - existing generated password still authenticates with Basic Auth
        - restart / force-recreate preserves legacy state and password
        """
        # Hardcoded dummy constant, disposable tempfile -- not a real secret.
        self.initial_pwd_file.write_text(_VALID_TEST_PASSWORD, encoding="utf-8")  # codeql[py/clear-text-storage-sensitive-data]
        os.chmod(self.initial_pwd_file, 0o600)
        app_module._migrate_or_initialize_setup_state()

        self.assertEqual(self.setup_state_file.read_text(encoding="utf-8").strip(), "legacy_established")
        self.assertFalse(app_module._first_run_setup_required())

        # POST /api/setup/first-run is rejected: legacy_established means it
        # is no longer publicly reachable, so this is a 401, not a 409.
        setup_resp = self.client.post(
            "/api/setup/first-run",
            json={"username": "attacker", "password": _VALID_TEST_PASSWORD},
            headers=_SAME_ORIGIN_CSRF_HEADERS,
        )
        self.assertEqual(setup_resp.status_code, 401)

        # Existing initial password still authenticates Basic Auth
        cred = base64.b64encode(f"admin:{_VALID_TEST_PASSWORD}".encode("utf-8")).decode("utf-8")
        resp_auth = self.client.get("/api/setup/env", headers={"Authorization": f"Basic {cred}"})
        self.assertEqual(resp_auth.status_code, 200)

        # Restart simulation: state remains legacy_established and password remains valid
        app_module._migrate_or_initialize_setup_state()
        self.assertFalse(app_module._first_run_setup_required())
        self.assertTrue(self.initial_pwd_file.exists())

    def test_credential_deletion_fails_closed(self):
        """Credential deletion on a completed/claimed install fails closed instead of reopening setup."""
        # 1. Complete setup
        self.client.post(
            "/api/setup/first-run",
            json={"username": "admin", "password": _VALID_TEST_PASSWORD},
            headers=_SAME_ORIGIN_CSRF_HEADERS,
        )
        self.assertEqual(self.setup_state_file.read_text(encoding="utf-8").strip(), "claimed")

        # 2. Simulate deleted password file
        self.persisted_pwd_file.unlink(missing_ok=True)

        # 3. Setup MUST NOT reopen for a different unauthenticated browser
        # (claimed -> not a public endpoint -> 401)
        self.assertFalse(app_module._first_run_setup_required())
        anon_client = app_module.app.test_client()
        setup_resp = anon_client.post(
            "/api/setup/first-run",
            json={"username": "attacker", "password": _VALID_TEST_PASSWORD},
            headers=_SAME_ORIGIN_CSRF_HEADERS,
        )
        self.assertEqual(setup_resp.status_code, 401)

        # 4. Unauthenticated request to protected endpoint fails closed (503 / 401)
        resp = anon_client.get("/api/library")
        self.assertIn(resp.status_code, (401, 503))

    def test_credential_deletion_does_not_regenerate_on_restart(self):
        """A claimed install whose credential file is later deleted must fail
        closed across a restart, not mint a fresh working password."""
        self.client.post(
            "/api/setup/first-run",
            json={"username": "admin", "password": _VALID_TEST_PASSWORD},
            headers=_SAME_ORIGIN_CSRF_HEADERS,
        )
        self.persisted_pwd_file.unlink(missing_ok=True)
        self.initial_pwd_file.unlink(missing_ok=True)

        # Simulate the boot-time bootstrap sequence a restart/force-recreate runs.
        app_module._migrate_or_initialize_setup_state()
        app_module._bootstrap_browser_password_if_missing()

        self.assertFalse(
            self.initial_pwd_file.exists(),
            "a new .initial_admin_password must never be auto-generated for an "
            "already-established install -- that would be an anonymous "
            "account-takeover path via container logs",
        )
        self.assertFalse(app_module._auth_secret_is_usable(app_module._security_auth_password()))
        self.assertFalse(app_module._first_run_setup_required())

    def test_csrf_same_origin_with_header_allowed(self):
        """fresh + same-origin + correct CSRF header -> first-run claim allowed."""
        app_module._migrate_or_initialize_setup_state()
        resp = self.client.post(
            "/api/setup/first-run",
            json={"username": "admin", "password": _VALID_TEST_PASSWORD},
            headers={"X-Beets-CSRF": "1", "Origin": "http://localhost"},
        )
        self.assertEqual(resp.status_code, 200)

    def test_csrf_cross_origin_with_header_rejected(self):
        """fresh + cross-origin + correct CSRF header -> rejected."""
        app_module._migrate_or_initialize_setup_state()
        resp = self.client.post(
            "/api/setup/first-run",
            json={"username": "admin", "password": _VALID_TEST_PASSWORD},
            headers={"X-Beets-CSRF": "1", "Origin": "http://evil.example"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(app_module._first_run_setup_required())

    def test_csrf_same_origin_missing_header_rejected(self):
        """fresh + same-origin + missing CSRF header -> rejected."""
        app_module._migrate_or_initialize_setup_state()
        resp = self.client.post(
            "/api/setup/first-run",
            json={"username": "admin", "password": _VALID_TEST_PASSWORD},
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(app_module._first_run_setup_required())

    def test_claimed_bootstrap_endpoint_not_anonymously_reachable(self):
        """Once claimed, POST /api/setup/first-run must behave like any other
        protected mutating endpoint -- it must not remain a permanent
        anonymous public API just because first-run bootstrap exists."""
        self.client.post(
            "/api/setup/first-run",
            json={"username": "admin", "password": _VALID_TEST_PASSWORD},
            headers=_SAME_ORIGIN_CSRF_HEADERS,
        )
        self.assertFalse(app_module._first_run_setup_required())

        resp = self.client.post(
            "/api/setup/first-run",
            json={"username": "attacker", "password": _VALID_TEST_PASSWORD},
            headers=_SAME_ORIGIN_CSRF_HEADERS,
        )
        # Must not be treated as public/anonymous (no 200, no silent bypass).
        self.assertIn(resp.status_code, (401, 409, 503))

    def test_claim_persistence_fails_closed_when_state_write_fails(self):
        """Credential write succeeds but durable state persistence fails ->
        the endpoint must NOT report 200 setup complete."""
        app_module._migrate_or_initialize_setup_state()

        with mock.patch.object(app_module, "_persist_file_atomically") as mocked:
            def fake_persist(target_file, content):
                if target_file == app_module._BROWSER_SETUP_STATE_FILE:
                    return False
                target_file.parent.mkdir(parents=True, exist_ok=True)
                target_file.write_text(content, encoding="utf-8")
                return True

            mocked.side_effect = fake_persist

            resp = self.client.post(
                "/api/setup/first-run",
                json={"username": "admin", "password": _VALID_TEST_PASSWORD},
                headers=_SAME_ORIGIN_CSRF_HEADERS,
            )

        self.assertNotEqual(resp.status_code, 200)
        self.assertEqual(resp.status_code, 500)
        body = resp.get_data(as_text=True)
        self.assertNotIn(_VALID_TEST_PASSWORD, body)
        # The error text must not imply an anonymous retry is possible --
        # the persisted password already closes anonymous bootstrap -- and
        # should point the operator at signing in, not "retry".
        error_text = resp.get_json().get("error", "")
        self.assertIn("sign in", error_text.lower())
        self.assertNotIn("retry", error_text.lower())

    def test_setup_status_stays_public_after_claim(self):
        """GET /api/setup/status must remain anonymously reachable even after
        claim/migration -- the frontend calls it unauthenticated on every
        page load, in every state, to decide which UI to render."""
        self.client.post(
            "/api/setup/first-run",
            json={"username": "admin", "password": _VALID_TEST_PASSWORD},
            headers=_SAME_ORIGIN_CSRF_HEADERS,
        )
        self.assertFalse(app_module._first_run_setup_required())

        resp = self.client.get("/api/setup/status")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json().get("first_run", {}).get("required"))

    def test_setup_env_and_settings_never_anonymous_during_first_run(self):
        """Unrelated setup APIs must not become anonymous just because
        first-run bootstrap exists."""
        app_module._migrate_or_initialize_setup_state()
        self.assertTrue(app_module._first_run_setup_required())

        self.assertEqual(self.client.get("/api/setup/env").status_code, 401)
        self.assertEqual(self.client.get("/api/setup/settings").status_code, 401)

    def test_first_run_validation_errors(self):
        """POST /api/setup/first-run rejects weak passwords or extra payload fields."""
        app_module._migrate_or_initialize_setup_state()

        # Weak password
        resp_weak = self.client.post(
            "/api/setup/first-run",
            json={"username": "admin", "password": _WEAK_TEST_PASSWORD},
            headers=_SAME_ORIGIN_CSRF_HEADERS,
        )
        self.assertEqual(resp_weak.status_code, 400)
        self.assertIn("unmet_requirements", resp_weak.get_json())

        # Disallowed extra keys
        resp_extra = self.client.post(
            "/api/setup/first-run",
            json={"username": "admin", "password": _VALID_TEST_PASSWORD, "BEETS_API_TOKEN": "malicious"},
            headers=_SAME_ORIGIN_CSRF_HEADERS,
        )
        self.assertEqual(resp_extra.status_code, 400)

    def test_concurrent_claim_race_condition(self):
        """Two concurrent setup requests race: exactly one wins (200), second gets 409."""
        app_module._migrate_or_initialize_setup_state()
        results = []

        def worker(pwd):
            r = self.client.post(
                "/api/setup/first-run",
                json={"username": "admin", "password": pwd},
                headers=_SAME_ORIGIN_CSRF_HEADERS,
            )
            results.append(r.status_code)

        pwd1 = _VALID_TEST_PASSWORD
        pwd2 = _VALID_TEST_PASSWORD + "Extra!"

        t1 = threading.Thread(target=worker, args=(pwd1,))
        t2 = threading.Thread(target=worker, args=(pwd2,))

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertIn(200, results)
        self.assertIn(409, results)
        self.assertEqual(len(results), 2)
        self.assertEqual(self.setup_state_file.read_text(encoding="utf-8").strip(), "claimed")


if __name__ == "__main__":
    unittest.main()
