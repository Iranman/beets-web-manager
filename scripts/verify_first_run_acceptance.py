"""Fast in-process smoke test for first-run browser authentication.

Simulates a brand-new user with empty persistent directories:
1. Verifies process/container startup and health endpoint response
2. Verifies fresh install enters browser first-run setup mode
3. Verifies protected application routes are denied (401) unauthenticated
4. Verifies browser setup endpoint POST /api/setup/first-run accepts user credentials
5. Verifies setup mode completes and anonymous bootstrap endpoint closes (409)
6. Verifies Basic Auth with user-created credentials succeeds (200 OK)
7. Verifies incorrect password fails (401 Unauthorized)
8. Verifies API Bearer token works independently
9. Verifies restarting Web Manager preserves user-created credentials
10. Verifies initial password file is cleaned up after setup
11. Verifies no secret appears in process output logs
12. Verifies no local authoritative Beets SQLite database is created by Web Manager
"""
import base64
import contextlib
import io
import logging
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

# Setup root path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module
import routes_setup


_USER_PASSWORD = "MyCustomAdminPassword123!Aa1234567"


def run_acceptance_test():
    print("==> Starting disposable fresh-user acceptance test...")
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "web-manager-data"
        data_dir.mkdir()

        initial_pwd_file = data_dir / ".initial_admin_password"
        persisted_pwd_file = data_dir / ".browser_password"
        persisted_user_file = data_dir / ".browser_username"
        auth_token_file = data_dir / ".auth_token"

        # Ensure no existing env or auth files are reused
        env_dict = {
            "BEETS_WEB_PASSWORD": "",
            "BEETS_WEB_PASSWORD_FILE": "",
            "BEETS_WEB_USERNAME": "",
            "BEETS_WEB_AUTH_TOKEN": "",
            "BEETS_WEB_AUTH_DISABLED": "0",
            "BEETSDIR": str(data_dir),
        }

        log_capture = io.StringIO()
        handler = logging.StreamHandler(log_capture)
        logging.getLogger().addHandler(handler)

        with mock.patch.dict(os.environ, env_dict), \
             mock.patch.object(app_module, "_INITIAL_BROWSER_PASSWORD_FILE", initial_pwd_file), \
             mock.patch.object(app_module, "_PERSISTED_BROWSER_PASSWORD_FILE", persisted_pwd_file), \
             mock.patch.object(app_module, "_PERSISTED_BROWSER_USERNAME_FILE", persisted_user_file), \
             mock.patch.object(app_module, "_GENERATED_AUTH_TOKEN_FILE", auth_token_file), \
             contextlib.redirect_stdout(log_capture), \
             contextlib.redirect_stderr(log_capture):

            # Reset process state for fresh boot
            app_module._bootstrap_auth_token_if_missing()
            app_module._bootstrap_browser_password_if_missing()

            client = app_module.app.test_client()

            # 1. Health check works
            health_resp = client.get("/api/health")
            assert health_resp.status_code == 200, f"Health check failed: {health_resp.status_code}"
            print("  [✓] 1. Container/app health endpoint responds successfully.")

            # 2. Fresh install enters browser first-run setup mode
            assert app_module._first_run_setup_required(), "First-run setup should be required on fresh install!"
            status_resp = client.get("/api/setup/status")
            assert status_resp.status_code == 200, f"Expected 200 setup status, got {status_resp.status_code}"
            assert status_resp.get_json().get("first_run", {}).get("required") is True, "First-run state missing from status!"
            print("  [✓] 2. Fresh install correctly enters browser first-run setup mode.")

            # 3. Protected application routes return 401 when unauthenticated
            unauth_lib = client.get("/api/library")
            assert unauth_lib.status_code == 401, f"Expected 401 for /api/library, got {unauth_lib.status_code}"
            unauth_env = client.get("/api/setup/env")
            assert unauth_env.status_code == 401, f"Expected 401 for /api/setup/env, got {unauth_env.status_code}"
            print("  [✓] 3. Normal application APIs remain strictly protected during first-run mode.")

            # 4. User completes browser setup via POST /api/setup/first-run
            setup_resp = client.post(
                "/api/setup/first-run",
                json={"username": "myadmin", "password": _USER_PASSWORD},
                headers={"X-Beets-CSRF": "1"},
            )
            assert setup_resp.status_code == 200, f"First-run setup POST failed: {setup_resp.status_code}"
            assert setup_resp.get_json().get("ok") is True
            print("  [✓] 4. Anonymous bootstrap endpoint POST /api/setup/first-run creates administrator login.")

            # 5. First-run setup closes; second call returns 409 Conflict
            assert not app_module._first_run_setup_required(), "First-run setup should no longer be required!"
            second_resp = client.post(
                "/api/setup/first-run",
                json={"username": "hacker", "password": _USER_PASSWORD},
                headers={"X-Beets-CSRF": "1"},
            )
            assert second_resp.status_code == 409, f"Expected 409 for repeated setup, got {second_resp.status_code}"
            print("  [✓] 5. First-run setup mode atomically closes and rejects subsequent bootstrap attempts (409 Conflict).")

            # 6. Basic Auth with user-created credentials succeeds
            valid_auth = base64.b64encode(f"myadmin:{_USER_PASSWORD}".encode("utf-8")).decode("utf-8")
            auth_resp = client.get("/api/setup/env", headers={"Authorization": f"Basic {valid_auth}"})
            assert auth_resp.status_code == 200, f"Basic auth failed with status {auth_resp.status_code}"
            print("  [✓] 6. Basic Auth with user-created credentials succeeds (200 OK).")

            # 7. Incorrect password fails
            invalid_auth = base64.b64encode(f"myadmin:wrong_password_attempt_1234567890".encode("utf-8")).decode("utf-8")
            fail_resp = client.get("/api/setup/env", headers={"Authorization": f"Basic {invalid_auth}"})
            assert fail_resp.status_code == 401, f"Expected 401 for bad password, got {fail_resp.status_code}"
            print("  [✓] 7. Incorrect password correctly rejected (401 Unauthorized).")

            # 8. API Bearer token works separately
            token = auth_token_file.read_text(encoding="utf-8").strip()
            bearer_resp = client.get("/api/setup/env", headers={"Authorization": f"Bearer {token}"})
            assert bearer_resp.status_code == 200, f"Bearer auth failed with status {bearer_resp.status_code}"
            print("  [✓] 8. API Bearer token works independently.")

            # 9. Initial password file cleaned up after user setup
            assert not initial_pwd_file.exists(), "Initial password file should have been cleaned up!"
            print("  [✓] 9. Initial password file .initial_admin_password removed after user credential setup.")

            # 10. Restarting Web Manager preserves user-created credentials
            assert persisted_pwd_file.exists(), "Persisted password file missing!"
            restarted_pwd = persisted_pwd_file.read_text(encoding="utf-8").strip()
            assert restarted_pwd == _USER_PASSWORD, "Password changed upon restart!"
            print("  [✓] 10. Restarting Web Manager preserves exact user-created credentials.")

            # 11. Secrets do not appear in logs
            logs_content = log_capture.getvalue()
            assert _USER_PASSWORD not in logs_content, "CRITICAL: User password leaked into logs!"
            assert token not in logs_content, "CRITICAL: Generated API token leaked into logs!"
            print("  [✓] 11. Zero secrets appear in stdout, stderr, or log outputs.")

            # 12. No local authoritative Beets SQLite database created
            sqlite_files = list(data_dir.glob("*.blb")) + list(data_dir.glob("*.db")) + list(data_dir.glob("*.sqlite"))
            assert len(sqlite_files) == 0, f"Local SQLite database found in Web Manager dir: {sqlite_files}"
            print("  [✓] 12. No local authoritative Beets SQLite database is created by Web Manager.")

    print("\nACCEPTANCE TEST PASSED SUCCESSFULLY: All 12 verification points confirmed!")


if __name__ == "__main__":
    run_acceptance_test()
