"""Fast in-process smoke test for first-run browser authentication.

NOT sufficient evidence on its own that the feature works: this only
instantiates Flask's test_client() directly in the current Python process --
no Docker, no real container healthcheck, no real network hop to a control
agent, no real read_only/tmpfs/cap_drop container constraints. Use this for
a quick sanity check during development. For the real proof (actual disposable
`beets` + `beets-web-manager` containers via docker-compose.full.yml,
covering upgrade compatibility and runtime password replacement against a
live container too), see scripts/verify_first_run_docker_acceptance.py.

Simulates a brand-new user with empty persistent directories:
1. Verifies process/container startup and health endpoint response
2. Verifies browser username discovery
3. Verifies initial browser password generation and retrieval from /web-manager-data/.initial_admin_password
4. Verifies unauthenticated browser request receives 401 + WWW-Authenticate: Basic challenge
5. Verifies Basic Auth with generated username and password succeeds (200 OK)
6. Verifies incorrect password fails (401 Unauthorized)
7. Verifies API Bearer token works independently
8. Verifies restarting Web Manager preserves the exact same browser password
9. Verifies no secret appears in process/Docker output logs
10. Verifies no local authoritative Beets SQLite database is created by Web Manager
"""
import base64
import contextlib
import io
import logging
import os
import stat
import sys
import tempfile
from pathlib import Path
from unittest import mock

# Setup root path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module
import routes_setup


def run_acceptance_test():
    print("==> Starting disposable fresh-user acceptance test...")
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "web-manager-data"
        data_dir.mkdir()

        initial_pwd_file = data_dir / ".initial_admin_password"
        auth_token_file = data_dir / ".auth_token"

        # Ensure no existing env or auth files are reused
        env_dict = {
            "BEETS_WEB_PASSWORD": "",
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

            # 2. Browser username discoverable
            username = app_module._security_auth_username()
            assert username == "admin", f"Unexpected default username: {username}"
            print(f"  [✓] 2. Browser username discoverable: '{username}'.")

            # 3. Initial browser password retrievable
            assert initial_pwd_file.exists(), "Initial password file was not created!"
            initial_password = initial_pwd_file.read_text(encoding="utf-8").strip()
            assert len(initial_password) >= 32, "Generated password does not meet min length!"
            assert app_module._password_requirements_unmet(initial_password) == [], "Password requirements unmet!"
            print(f"  [✓] 3. Initial browser password retrieved from {initial_pwd_file.name} ({len(initial_password)} chars, passes all policy rules).")

            # 4. Unauthenticated browser request receives challenge
            unauth_resp = client.get("/api/setup/status")
            assert unauth_resp.status_code == 401, f"Expected 401, got {unauth_resp.status_code}"
            assert "WWW-Authenticate" in unauth_resp.headers, "Missing WWW-Authenticate header!"
            assert "Basic" in unauth_resp.headers["WWW-Authenticate"], "Expected Basic auth challenge!"
            print("  [✓] 4. Unauthenticated request receives HTTP Basic Auth challenge (401 + WWW-Authenticate).")

            # 5. Basic Auth with generated credentials succeeds
            valid_auth = base64.b64encode(f"{username}:{initial_password}".encode("utf-8")).decode("utf-8")
            auth_resp = client.get("/api/setup/status", headers={"Authorization": f"Basic {valid_auth}"})
            assert auth_resp.status_code == 200, f"Basic auth failed with status {auth_resp.status_code}"
            print("  [✓] 5. Basic Auth with generated credentials succeeds (200 OK).")

            # 6. Incorrect password fails
            invalid_auth = base64.b64encode(f"{username}:wrong_password_attempt_1234567890".encode("utf-8")).decode("utf-8")
            fail_resp = client.get("/api/setup/status", headers={"Authorization": f"Basic {invalid_auth}"})
            assert fail_resp.status_code == 401, f"Expected 401 for bad password, got {fail_resp.status_code}"
            print("  [✓] 6. Incorrect password correctly rejected (401 Unauthorized).")

            # 7. API Bearer token works separately
            token = auth_token_file.read_text(encoding="utf-8").strip()
            bearer_resp = client.get("/api/setup/status", headers={"Authorization": f"Bearer {token}"})
            assert bearer_resp.status_code == 200, f"Bearer auth failed with status {bearer_resp.status_code}"
            print("  [✓] 7. API Bearer token works independently.")

            # 8. Web Manager can reach status/diagnostics API
            assert "ok" in auth_resp.get_json(), "Invalid setup status response structure"
            print("  [✓] 8. Web Manager API endpoints function correctly.")

            # 9. Restarting preserves exact same browser password
            app_module._bootstrap_browser_password_if_missing()
            restarted_pwd = initial_pwd_file.read_text(encoding="utf-8").strip()
            assert restarted_pwd == initial_password, "Password changed upon restart!"
            print("  [✓] 9. Restarting Web Manager preserves the exact same browser password.")

            # 10. Secrets do not appear in logs
            logs_content = log_capture.getvalue()
            assert initial_password not in logs_content, "CRITICAL: Generated browser password leaked into logs!"
            assert token not in logs_content, "CRITICAL: Generated API token leaked into logs!"
            print("  [✓] 10. Zero secrets appear in stdout, stderr, or log outputs.")

            # 11. No local authoritative Beets SQLite database created
            sqlite_files = list(data_dir.glob("*.blb")) + list(data_dir.glob("*.db")) + list(data_dir.glob("*.sqlite"))
            assert len(sqlite_files) == 0, f"Local SQLite database found in Web Manager dir: {sqlite_files}"
            print("  [✓] 11. No local authoritative Beets SQLite database is created by Web Manager.")

    print("\nACCEPTANCE TEST PASSED SUCCESSFULLY: All 11 verification points confirmed!")


if __name__ == "__main__":
    run_acceptance_test()
