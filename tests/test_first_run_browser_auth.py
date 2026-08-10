"""Automated test suite for first-run browser authentication experience.

Covers:
- Fresh install auto-generation of initial browser password
- Credential precedence (explicit env password > persisted > generated)
- Password complexity verification
- Initial password lifecycle (.initial_admin_password removal upon replacement)
- Secret leakage prevention across endpoints, status payloads, and process output
- Clear separation between browser Basic Auth, API Bearer token, and Beets engine token
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


_MARKER_PWD = "Aa1!" + ("x" * 32)


class _CapturingLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@contextlib.contextmanager
def _capture_output_and_logs():
    out, err = io.StringIO(), io.StringIO()
    handler = _CapturingLogHandler()
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            yield out, err, handler.messages
    finally:
        root_logger.removeHandler(handler)


class FirstRunBrowserAuthTests(unittest.TestCase):
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
        ]
        for p in self.patches:
            p.start()

        self.client = app_module.app.test_client()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.env_patch.stop()
        self.tmpdir.cleanup()

    def test_fresh_install_generates_initial_browser_password(self):
        """When no browser password exists, an initial secure password is auto-generated."""
        app_module._bootstrap_browser_password_if_missing()

        self.assertTrue(self.initial_pwd_file.exists())
        generated_pwd = self.initial_pwd_file.read_text(encoding="utf-8").strip()

        # Meets password requirements
        self.assertGreaterEqual(len(generated_pwd), 32)
        self.assertEqual(app_module._password_requirements_unmet(generated_pwd), [])
        self.assertTrue(app_module._auth_secret_is_usable(generated_pwd))

        # Username defaults to admin
        self.assertEqual(app_module._security_auth_username(), "admin")

        # Basic Auth succeeds with generated credentials
        cred = base64.b64encode(f"admin:{generated_pwd}".encode("utf-8")).decode("utf-8")
        resp = self.client.get("/api/setup/status", headers={"Authorization": f"Basic {cred}"})
        self.assertEqual(resp.status_code, 200)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits not enforced on Windows")
    def test_initial_password_file_has_0600_permissions(self):
        app_module._bootstrap_browser_password_if_missing()
        mode = stat.S_IMODE(self.initial_pwd_file.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_restart_reuses_same_initial_password(self):
        """Restarting authentication reuses existing initial password without generating a new one."""
        app_module._bootstrap_browser_password_if_missing()
        pwd1 = self.initial_pwd_file.read_text(encoding="utf-8").strip()

        # Call bootstrapper again (simulating restart)
        app_module._bootstrap_browser_password_if_missing()
        pwd2 = self.initial_pwd_file.read_text(encoding="utf-8").strip()

        self.assertEqual(pwd1, pwd2)

    def test_explicit_environment_password_takes_precedence(self):
        """If BEETS_WEB_PASSWORD is set in env, no initial password file is created."""
        explicit_pwd = "MyExplicitSuperSecurePassword123!"
        with mock.patch.dict(os.environ, {"BEETS_WEB_PASSWORD": explicit_pwd}):
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
        app_module._bootstrap_browser_password_if_missing()
        self.assertTrue(self.initial_pwd_file.exists())

        new_pwd = "MyNewReplacementSecurePassword456!"
        # Simulate updating via setup environment helper
        with mock.patch.object(routes_setup, "_SETUP_ENV_FILE", self.data_dir / ".env"):
            routes_setup._write_env_file({"BEETS_WEB_PASSWORD": new_pwd}, [])

        self.assertFalse(self.initial_pwd_file.exists())
        self.assertEqual(app_module._security_auth_password(), new_pwd)

        # New password works for Basic Auth
        new_cred = base64.b64encode(f"admin:{new_pwd}".encode("utf-8")).decode("utf-8")
        resp = self.client.get("/api/setup/status", headers={"Authorization": f"Basic {new_cred}"})
        self.assertEqual(resp.status_code, 200)

    def test_secret_leakage_prevention(self):
        """Generated password must never appear in stdout, stderr, logs, or API responses."""
        with mock.patch.object(app_module, "generate_secure_browser_password", return_value=_MARKER_PWD), \
             _capture_output_and_logs() as (out, err, log_msgs):
            app_module._bootstrap_browser_password_if_missing()

        # Output banner contains notification and path but NOT raw password
        self.assertNotIn(_MARKER_PWD, out.getvalue())
        self.assertNotIn(_MARKER_PWD, err.getvalue())
        self.assertTrue(all(_MARKER_PWD not in m for m in log_msgs))

        # Check endpoints do not expose raw secret
        cred = base64.b64encode(f"admin:{_MARKER_PWD}".encode("utf-8")).decode("utf-8")
        status_resp = self.client.get("/api/setup/status", headers={"Authorization": f"Basic {cred}"})
        self.assertNotIn(_MARKER_PWD, status_resp.get_data(as_text=True))

        health_resp = self.client.get("/api/health")
        self.assertNotIn(_MARKER_PWD, health_resp.get_data(as_text=True))

    def test_token_separation_and_boundary_integrity(self):
        """Bearer token, Basic Auth browser password, and engine token are strictly separate."""
        bearer_token = "valid_test_bearer_token_32_chars_minimum!"
        app_module._bootstrap_browser_password_if_missing()
        browser_pwd = self.initial_pwd_file.read_text(encoding="utf-8").strip()

        # 1. Bearer token works for API requests
        resp_bearer = self.client.get("/api/setup/env", headers={"Authorization": f"Bearer {bearer_token}"})
        self.assertEqual(resp_bearer.status_code, 200)

        # 2. Bearer token CANNOT be used as Basic Auth password
        bad_basic = base64.b64encode(f"admin:{bearer_token}".encode("utf-8")).decode("utf-8")
        resp_bad_basic = self.client.get("/api/setup/env", headers={"Authorization": f"Basic {bad_basic}"})
        self.assertEqual(resp_bad_basic.status_code, 401)

        # 3. Browser Basic Auth password works for Basic Auth
        good_basic = base64.b64encode(f"admin:{browser_pwd}".encode("utf-8")).decode("utf-8")
        resp_good_basic = self.client.get("/api/setup/status", headers={"Authorization": f"Basic {good_basic}"})
        self.assertEqual(resp_good_basic.status_code, 200)

        # 4. Engine API token (BEETS_API_TOKEN) cannot authenticate Web Manager API
        engine_token = "beets_engine_internal_token_32_chars_min"
        with mock.patch.dict(os.environ, {"BEETS_API_TOKEN": engine_token}):
            engine_bearer = self.client.get("/api/setup/env", headers={"Authorization": f"Bearer {engine_token}"})
            self.assertEqual(engine_bearer.status_code, 401)


if __name__ == "__main__":
    unittest.main()
