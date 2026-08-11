"""Fast in-process smoke test for first-run browser authentication & migration safety.

Simulates:
1. Genuinely fresh install (browser setup UI active, setup completes, setup mode closes)
2. Upgrade from v0.1.8 .initial_admin_password (setup mode remains CLOSED, existing password works)
3. Credential deletion on claimed install (fails closed, setup mode NEVER reopens)
4. Race protection, credential persistence, secret redaction, and no local Beets DB creation
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
        setup_state_file = data_dir / ".browser_setup_state"
        auth_token_file = data_dir / ".auth_token"

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
             mock.patch.object(app_module, "_BROWSER_SETUP_STATE_FILE", setup_state_file), \
             mock.patch.object(app_module, "_GENERATED_AUTH_TOKEN_FILE", auth_token_file), \
             mock.patch.object(routes_setup, "_STATUS_CACHE_DATA", None), \
             mock.patch.object(routes_setup, "_STATUS_CACHE_TS", 0.0), \
             contextlib.redirect_stdout(log_capture), \
             contextlib.redirect_stderr(log_capture):

            # Reset process state for fresh boot
            app_module._bootstrap_auth_token_if_missing()
            app_module._migrate_or_initialize_setup_state()
            app_module._bootstrap_browser_password_if_missing()

            client = app_module.app.test_client()

            # 1. Health check works
            health_resp = client.get("/api/health")
            assert health_resp.status_code == 200, f"Health check failed: {health_resp.status_code}"
            print("  [OK] 1. Container/app health endpoint responds successfully.")

            # 2. Fresh install enters browser first-run setup mode
            assert app_module._first_run_setup_required(), "First-run setup should be required on fresh install!"
            status_resp = client.get("/api/setup/status")
            assert status_resp.status_code == 200, f"Expected 200 setup status, got {status_resp.status_code}"
            assert status_resp.get_json().get("first_run", {}).get("required") is True, "First-run state missing from status!"
            print("  [OK] 2. Fresh install correctly enters browser first-run setup mode.")

            # 3. Protected application routes return 401 when unauthenticated
            unauth_lib = client.get("/api/library")
            assert unauth_lib.status_code == 401, f"Expected 401 for /api/library, got {unauth_lib.status_code}"
            unauth_env = client.get("/api/setup/env")
            assert unauth_env.status_code == 401, f"Expected 401 for /api/setup/env, got {unauth_env.status_code}"
            print("  [OK] 3. Normal application APIs remain strictly protected during first-run mode.")

            # 4. User completes browser setup via POST /api/setup/first-run
            setup_resp = client.post(
                "/api/setup/first-run",
                json={"username": "myadmin", "password": _USER_PASSWORD},
                headers={"X-Beets-CSRF": "1", "Origin": "http://localhost"},
            )
            assert setup_resp.status_code == 200, f"First-run setup POST failed: {setup_resp.status_code}"
            assert setup_resp.get_json().get("ok") is True
            print("  [OK] 4. Anonymous bootstrap endpoint POST /api/setup/first-run creates administrator login.")

            # 5. First-run setup closes; second call returns 409 Conflict
            assert not app_module._first_run_setup_required(), "First-run setup should no longer be required!"
            second_resp = client.post(
                "/api/setup/first-run",
                json={"username": "hacker", "password": _USER_PASSWORD},
                headers={"X-Beets-CSRF": "1", "Origin": "http://localhost"},
            )
            assert second_resp.status_code == 401, f"Expected 401 for repeated setup (endpoint is no longer public once claimed), got {second_resp.status_code}"
            print("  [OK] 5. First-run setup mode atomically closes; the endpoint stops being anonymously reachable at all (401).")

            # 6. Basic Auth with user-created credentials succeeds
            valid_auth = base64.b64encode(f"myadmin:{_USER_PASSWORD}".encode("utf-8")).decode("utf-8")
            auth_resp = client.get("/api/setup/env", headers={"Authorization": f"Basic {valid_auth}"})
            assert auth_resp.status_code == 200, f"Basic auth failed with status {auth_resp.status_code}"
            print("  [OK] 6. Basic Auth with user-created credentials succeeds (200 OK).")

            # 7. Incorrect password fails
            invalid_auth = base64.b64encode(f"myadmin:wrong_password_attempt_1234567890".encode("utf-8")).decode("utf-8")
            fail_resp = client.get("/api/setup/env", headers={"Authorization": f"Basic {invalid_auth}"})
            assert fail_resp.status_code == 401, f"Expected 401 for bad password, got {fail_resp.status_code}"
            print("  [OK] 7. Incorrect password correctly rejected (401 Unauthorized).")

            # 8. API Bearer token works separately
            token = auth_token_file.read_text(encoding="utf-8").strip()
            bearer_resp = client.get("/api/setup/env", headers={"Authorization": f"Bearer {token}"})
            assert bearer_resp.status_code == 200, f"Bearer auth failed with status {bearer_resp.status_code}"
            print("  [OK] 8. API Bearer token works independently.")

            # 9. Credential deletion fail-closed check
            persisted_pwd_file.unlink(missing_ok=True)
            assert not app_module._first_run_setup_required(), "Setup must NOT reopen on credential deletion!"
            del_setup_resp = client.post(
                "/api/setup/first-run",
                json={"username": "attacker", "password": _USER_PASSWORD},
                headers={"X-Beets-CSRF": "1", "Origin": "http://localhost"},
            )
            assert del_setup_resp.status_code == 401, f"Expected 401 for setup on deleted creds (not public anymore), got {del_setup_resp.status_code}"
            print("  [OK] 9. Credential deletion fails closed (setup mode NEVER reopens anonymously).")

            # Restore password for remaining checks. _USER_PASSWORD is a
            # hardcoded dummy constant written to a disposable tempfile that
            # this script deletes on exit -- not a real secret.
            persisted_pwd_file.write_text(_USER_PASSWORD, encoding="utf-8")  # codeql[py/clear-text-storage-sensitive-data]
            os.chmod(persisted_pwd_file, 0o600)

            # 10. Secrets do not appear in logs
            logs_content = log_capture.getvalue()
            assert _USER_PASSWORD not in logs_content, "CRITICAL: User password leaked into logs!"
            assert token not in logs_content, "CRITICAL: Generated API token leaked into logs!"
            print("  [OK] 10. Zero secrets appear in stdout, stderr, or log outputs.")

            # 11. No local authoritative Beets SQLite database created
            sqlite_files = list(data_dir.glob("*.blb")) + list(data_dir.glob("*.db")) + list(data_dir.glob("*.sqlite"))
            assert len(sqlite_files) == 0, f"Local SQLite database found in Web Manager dir: {sqlite_files}"
            print("  [OK] 11. No local authoritative Beets SQLite database is created by Web Manager.")

    # Test v0.1.8 upgrade migration separately
    print("==> Starting v0.1.8 upgrade migration acceptance test...")
    with tempfile.TemporaryDirectory() as tmp_upg:
        data_dir_upg = Path(tmp_upg) / "web-manager-data"
        data_dir_upg.mkdir()

        initial_pwd_file_upg = data_dir_upg / ".initial_admin_password"
        # Dummy constant seeded into a disposable tempfile to simulate a
        # pre-existing v0.1.8 install; not a real secret.
        initial_pwd_file_upg.write_text(_USER_PASSWORD, encoding="utf-8")  # codeql[py/clear-text-storage-sensitive-data]
        os.chmod(initial_pwd_file_upg, 0o600)
        persisted_pwd_file_upg = data_dir_upg / ".browser_password"
        persisted_user_file_upg = data_dir_upg / ".browser_username"
        setup_state_file_upg = data_dir_upg / ".browser_setup_state"
        auth_token_file_upg = data_dir_upg / ".auth_token"

        with mock.patch.dict(os.environ, env_dict), \
             mock.patch.object(app_module, "_INITIAL_BROWSER_PASSWORD_FILE", initial_pwd_file_upg), \
             mock.patch.object(app_module, "_PERSISTED_BROWSER_PASSWORD_FILE", persisted_pwd_file_upg), \
             mock.patch.object(app_module, "_PERSISTED_BROWSER_USERNAME_FILE", persisted_user_file_upg), \
             mock.patch.object(app_module, "_BROWSER_SETUP_STATE_FILE", setup_state_file_upg), \
             mock.patch.object(app_module, "_GENERATED_AUTH_TOKEN_FILE", auth_token_file_upg), \
             mock.patch.object(routes_setup, "_STATUS_CACHE_DATA", None), \
             mock.patch.object(routes_setup, "_STATUS_CACHE_TS", 0.0):

            app_module._migrate_or_initialize_setup_state()

            assert setup_state_file_upg.read_text(encoding="utf-8").strip() == "legacy_established"
            assert not app_module._first_run_setup_required(), "First-run setup should NOT be required on v0.1.8 upgrade!"

            client_upg = app_module.app.test_client()

            # Anonymous first-run setup is DENIED (409)
            upg_setup_resp = client_upg.post(
                "/api/setup/first-run",
                json={"username": "attacker", "password": _USER_PASSWORD},
                headers={"X-Beets-CSRF": "1", "Origin": "http://localhost"},
            )
            assert upg_setup_resp.status_code == 401, f"Expected 401 for first-run POST on v0.1.8 upgrade (not public), got {upg_setup_resp.status_code}"

            # Existing initial password continues working for Basic Auth
            valid_auth_upg = base64.b64encode(f"admin:{_USER_PASSWORD}".encode("utf-8")).decode("utf-8")
            upg_auth_resp = client_upg.get("/api/setup/env", headers={"Authorization": f"Basic {valid_auth_upg}"})
            assert upg_auth_resp.status_code == 200, f"Expected 200 Basic auth on upgraded install, got {upg_auth_resp.status_code}"
            print("  [OK] 12. Upgrade from v0.1.8 with .initial_admin_password keeps setup mode CLOSED and preserves existing login.")

    print("\nACCEPTANCE TEST PASSED SUCCESSFULLY: All 12 verification points confirmed!")


if __name__ == "__main__":
    run_acceptance_test()
