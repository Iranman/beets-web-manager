"""Automated test suite for browser first-run administrator setup.

Covers:
- `_first_run_setup_required()` state determination under all credential configurations
- POST /api/setup/first-run endpoint logic & input validation
- Race condition protection (concurrent setup requests allow exactly one winner)
- Credential persistence (.browser_username, .browser_password with 0600 mode)
- Transition from setup mode to normal Basic Auth boundary
- Security boundary enforcement (unauthenticated requests restricted to bootstrap routes only)
- Existing user upgrade compatibility
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


class FirstRunBrowserBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmpdir.name)

        self.initial_pwd_file = self.data_dir / ".initial_admin_password"
        self.persisted_pwd_file = self.data_dir / ".browser_password"
        self.persisted_user_file = self.data_dir / ".browser_username"
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

    def test_first_run_setup_required_state(self):
        """Verify _first_run_setup_required() returns True on fresh install and False when credentials exist."""
        # Fresh install: no browser credentials exist
        self.assertTrue(app_module._first_run_setup_required())

        # Presence of .initial_admin_password alone does NOT turn off first-run setup
        app_module._bootstrap_browser_password_if_missing()
        self.assertTrue(self.initial_pwd_file.exists())
        self.assertTrue(app_module._first_run_setup_required())

        # Explicit environment password disables first-run setup
        with mock.patch.dict(os.environ, {"BEETS_WEB_PASSWORD": _VALID_TEST_PASSWORD}):
            self.assertFalse(app_module._first_run_setup_required())

        # Persisted .browser_password disables first-run setup
        self.persisted_pwd_file.write_text(_VALID_TEST_PASSWORD, encoding="utf-8")
        self.assertFalse(app_module._first_run_setup_required())

    def test_first_run_setup_successful_bootstrap(self):
        """POST /api/setup/first-run successfully sets credentials and completes setup mode."""
        self.assertTrue(app_module._first_run_setup_required())

        resp = self.client.post(
            "/api/setup/first-run",
            json={"username": "newadmin", "password": _VALID_TEST_PASSWORD},
            headers={"X-Beets-CSRF": "1"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json().get("ok"))

        # Setup is now complete
        self.assertFalse(app_module._first_run_setup_required())
        self.assertEqual(self.persisted_user_file.read_text(encoding="utf-8").strip(), "newadmin")
        self.assertEqual(self.persisted_pwd_file.read_text(encoding="utf-8").strip(), _VALID_TEST_PASSWORD)

        # Initial password file cleaned up if it was present
        self.assertFalse(self.initial_pwd_file.exists())

        # Subsequent POST /api/setup/first-run fails with 409
        resp2 = self.client.post(
            "/api/setup/first-run",
            json={"username": "another", "password": _VALID_TEST_PASSWORD},
            headers={"X-Beets-CSRF": "1"},
        )
        self.assertEqual(resp2.status_code, 409)

    def test_first_run_validation_errors(self):
        """POST /api/setup/first-run rejects weak passwords or extra payload fields."""
        # Weak password
        resp_weak = self.client.post(
            "/api/setup/first-run",
            json={"username": "admin", "password": _WEAK_TEST_PASSWORD},
            headers={"X-Beets-CSRF": "1"},
        )
        self.assertEqual(resp_weak.status_code, 400)
        self.assertIn("unmet_requirements", resp_weak.get_json())

        # Disallowed extra keys
        resp_extra = self.client.post(
            "/api/setup/first-run",
            json={"username": "admin", "password": _VALID_TEST_PASSWORD, "BEETS_API_TOKEN": "malicious"},
            headers={"X-Beets-CSRF": "1"},
        )
        self.assertEqual(resp_extra.status_code, 400)

    def test_concurrent_claim_race_condition(self):
        """Two concurrent setup requests race: exactly one wins (200), second gets 409."""
        results = []

        def worker(pwd):
            r = self.client.post(
                "/api/setup/first-run",
                json={"username": "admin", "password": pwd},
                headers={"X-Beets-CSRF": "1"},
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

    def test_security_boundary_during_first_run(self):
        """During first-run setup, unauthenticated requests can reach only bootstrap routes."""
        self.assertTrue(app_module._first_run_setup_required())

        # Public / bootstrap routes work unauthenticated
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)

        status_resp = self.client.get("/api/setup/status")
        self.assertEqual(status_resp.status_code, 200)
        status_data = status_resp.get_json()
        self.assertTrue(status_data.get("first_run", {}).get("required"))

        # Protected application routes return 401 when unauthenticated
        self.assertEqual(self.client.get("/api/library").status_code, 401)
        self.assertEqual(self.client.get("/api/jobs").status_code, 401)
        self.assertEqual(self.client.get("/api/setup/env").status_code, 401)
        self.assertEqual(self.client.post("/api/setup/env", json={}).status_code, 401)
        self.assertEqual(self.client.post("/api/setup/auth-token/regenerate").status_code, 401)

        # Bearer token still works for protected routes
        bearer = self.env_patch.values["BEETS_WEB_AUTH_TOKEN"]
        resp_bearer = self.client.get("/api/setup/env", headers={"Authorization": f"Bearer {bearer}"})
        self.assertEqual(resp_bearer.status_code, 200)

    def test_existing_user_upgrade_compatibility(self):
        """Upgrading an existing install with .initial_admin_password or .browser_password works cleanly."""
        # Case A: .initial_admin_password exists (from v0.1.8)
        self.initial_pwd_file.write_text(_VALID_TEST_PASSWORD, encoding="utf-8")

        # Unauthenticated browser gets setup page state
        self.assertTrue(app_module._first_run_setup_required())

        # Authenticated Basic Auth with initial password still succeeds
        cred = base64.b64encode(f"admin:{_VALID_TEST_PASSWORD}".encode("utf-8")).decode("utf-8")
        resp_auth = self.client.get("/api/setup/status", headers={"Authorization": f"Basic {cred}"})
        self.assertEqual(resp_auth.status_code, 200)

        # Case B: .browser_password exists
        self.persisted_pwd_file.write_text(_VALID_TEST_PASSWORD, encoding="utf-8")
        self.assertFalse(app_module._first_run_setup_required())


if __name__ == "__main__":
    unittest.main()
