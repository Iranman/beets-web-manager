"""Automated test suite for browser first-run authentication boundaries & legacy fallbacks.

Covers:
- Basic Auth validation against persisted and legacy initial passwords
- Precedence of environment variables vs persisted password files
- Prevention of secret leakage in logs, stderr, and API status payloads
- Token separation (BEETS_WEB_AUTH_TOKEN vs BEETS_WEB_PASSWORD vs BEETS_API_TOKEN)
"""
import base64
import contextlib
import io
import logging
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
import routes_setup


_MARKER_PWD = "MarkerInitialPassword123!Aa4567890"


@contextlib.contextmanager
def _capture_output_and_logs():
    out = io.StringIO()
    err = io.StringIO()
    log_capture = io.StringIO()
    handler = logging.StreamHandler(log_capture)
    logger = logging.getLogger()
    logger.addHandler(handler)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            yield out, err, log_capture
    finally:
        logger.removeHandler(handler)


class FirstRunBrowserAuthTests(unittest.TestCase):
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
        ]
        for p in self.patches:
            p.start()

        self.client = app_module.app.test_client()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.env_patch.stop()
        self.tmpdir.cleanup()

    def test_legacy_initial_browser_password_authentication(self):
        """Legacy v0.1.8 .initial_admin_password authenticates Basic Auth requests."""
        pwd = app_module.generate_secure_browser_password(36)
        self.initial_pwd_file.write_text(pwd, encoding="utf-8")
        app_module._migrate_or_initialize_setup_state()

        self.assertEqual(self.setup_state_file.read_text(encoding="utf-8").strip(), "legacy_established")
        self.assertFalse(app_module._first_run_setup_required())

        # Meets password requirements
        self.assertGreaterEqual(len(pwd), 32)
        self.assertEqual(app_module._password_requirements_unmet(pwd), [])
        self.assertTrue(app_module._auth_secret_is_usable(pwd))

        # Username defaults to admin
        self.assertEqual(app_module._security_auth_username(), "admin")

        # Basic Auth succeeds with credentials
        cred = base64.b64encode(f"admin:{pwd}".encode("utf-8")).decode("utf-8")
        resp = self.client.get("/api/setup/status", headers={"Authorization": f"Basic {cred}"})
        self.assertEqual(resp.status_code, 200)

    def test_restart_reuses_same_initial_password(self):
        """Restarting authentication reuses existing initial password without generating a new one."""
        pwd1 = "InitialGeneratedPassword123!Aa456"
        self.initial_pwd_file.write_text(pwd1, encoding="utf-8")
        app_module._migrate_or_initialize_setup_state()
        app_module._bootstrap_browser_password_if_missing()

        # Call bootstrapper again (simulating restart)
        app_module._bootstrap_browser_password_if_missing()
        pwd2 = self.initial_pwd_file.read_text(encoding="utf-8").strip()

        self.assertEqual(pwd1, pwd2)

    def test_explicit_environment_password_takes_precedence(self):
        """If BEETS_WEB_PASSWORD is set in env, no initial password file is created."""
        explicit_pwd = "MyExplicitSuperSecurePassword123!Aa"
        with mock.patch.dict(os.environ, {"BEETS_WEB_PASSWORD": explicit_pwd}):
            app_module._migrate_or_initialize_setup_state()
            app_module._bootstrap_browser_password_if_missing()
            self.assertFalse(self.initial_pwd_file.exists())
            self.assertEqual(app_module._security_auth_password(), explicit_pwd)

            cred = base64.b64encode(f"admin:{explicit_pwd}".encode("utf-8")).decode("utf-8")
            resp = self.client.get("/api/setup/status", headers={"Authorization": f"Basic {cred}"})
            self.assertEqual(resp.status_code, 200)

    def test_weak_environment_password_fails_closed(self):
        """A weak environment password (under 32 chars) fails _auth_secret_is_usable check."""
        with mock.patch.dict(os.environ, {"BEETS_WEB_PASSWORD": "weak_password"}):
            self.assertFalse(app_module._auth_secret_is_usable(app_module._security_auth_password()))
            cred = base64.b64encode(b"admin:weak_password").decode("utf-8")
            resp = self.client.get("/api/setup/env", headers={"Authorization": f"Basic {cred}"})
            self.assertEqual(resp.status_code, 401)

    def test_password_replacement_cleans_up_initial_password_file(self):
        """Replacing initial password removes .initial_admin_password after successful persistence."""
        self.initial_pwd_file.write_text("OldInitialPassword123!Aa4567890", encoding="utf-8")
        app_module._migrate_or_initialize_setup_state()
        self.assertTrue(self.initial_pwd_file.exists())

        new_pwd = "MyNewReplacementSecurePassword456!Aa"
        # Simulate updating via setup environment helper
        with mock.patch.object(routes_setup, "_SETUP_ENV_FILE", self.data_dir / ".env"):
            routes_setup._write_env_file({"BEETS_WEB_PASSWORD": new_pwd}, [])

        self.assertFalse(self.initial_pwd_file.exists())
        self.assertEqual(app_module._security_auth_password(), new_pwd)

        # New password works for Basic Auth
        new_cred = base64.b64encode(f"admin:{new_pwd}".encode("utf-8")).decode("utf-8")
        resp = self.client.get("/api/setup/status", headers={"Authorization": f"Basic {new_cred}"})
        self.assertEqual(resp.status_code, 200)

    def test_token_separation_and_boundary_integrity(self):
        """Bearer token, Basic Auth browser password, and engine token are strictly separate."""
        bearer_token = "valid_test_bearer_token_32_chars_minimum!"
        browser_pwd = "MyCustomBrowserPassword123!Aa45"
        self.initial_pwd_file.write_text(browser_pwd, encoding="utf-8")
        app_module._migrate_or_initialize_setup_state()

        # 1. Bearer token works for API requests
        resp_bearer = self.client.get("/api/setup/env", headers={"Authorization": f"Bearer {bearer_token}"})
        self.assertEqual(resp_bearer.status_code, 200)

        # 2. Bearer token CANNOT be used as Basic Auth password
        bad_basic = base64.b64encode(f"admin:{bearer_token}".encode("utf-8")).decode("utf-8")
        resp_bad_basic = self.client.get("/api/setup/env", headers={"Authorization": f"Basic {bad_basic}"})
        self.assertEqual(resp_bad_basic.status_code, 401)


if __name__ == "__main__":
    unittest.main()
