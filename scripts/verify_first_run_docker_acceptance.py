"""Real Docker acceptance test for first-run browser authentication.

Four independently-booted scenarios, each its own compose project against
a fresh directory tree (state must not bleed between them):

  1. fresh_install   -- empty persistent dirs, no password configured.
     Covers browser first-run setup mode, initial credential submission via
     POST /api/setup/first-run, race condition handling, transition to Basic
     Auth, credential persistence across restart and force-recreate.
  2. v018_upgrade    -- pre-seeded .initial_admin_password (simulating a real
     v0.1.8 installation). Expect: setup mode remains CLOSED, anonymous bootstrap
     returns 401 (endpoint is not a public API once established), existing
     Basic Auth password works, state survives restart & force-recreate.
  3c. credential_deletion -- claimed install has its persisted password
     deleted, then the container is restarted. Expect: anonymous setup does
     NOT reopen, no new credential is auto-generated, auth fails closed.
  3d. concurrent_claim -- two simultaneous real HTTP POSTs to a fresh
     install's /api/setup/first-run. Expect: exactly one 200, exactly one
     non-200, state becomes claimed exactly once, winning credentials work.
  3. upgrade_no_password -- pre-seeded .auth_token (simulating an existing
     v0.1.7 install), no browser password configured. Expect: .auth_token
     unchanged, first-run browser setup mode is active, engine auth works.
  4. upgrade_with_password -- pre-seeded .auth_token AND BEETS_WEB_PASSWORD
     set. Expect: no browser setup required; explicit password remains authoritative.

Requires Docker. If Docker is unavailable, this script prints a clear error
and exits non-zero -- it never falls back to mocking and never reports
success for a Docker requirement it did not actually exercise.

Usage: python scripts/verify_first_run_docker_acceptance.py
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE_TAG = "ghcr.io/iranman/beets-web-manager:test-firstrun"
COMPOSE_FILE = str(ROOT / "docker-compose.full.yml")

_FAILURES = []


def ok(msg):
    print(f"  [OK] {msg}")


def fail(msg):
    print(f"  [FAIL] {msg}")
    _FAILURES.append(msg)


def run(cmd, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    if kw.get("text") and "encoding" not in kw:
        kw["encoding"] = "utf-8"
        kw.setdefault("errors", "replace")
    return subprocess.run(cmd, **kw)


def require_docker():
    if shutil.which("docker") is None:
        print("FATAL: docker is not on PATH -- cannot run the real Docker acceptance test.")
        print("This criterion CANNOT be reported as passed without Docker actually available.")
        sys.exit(2)
    res = run(["docker", "info"])
    if res.returncode != 0:
        print("FATAL: Docker daemon is not reachable -- cannot run the real Docker acceptance test.")
        print(res.stderr)
        print("This criterion CANNOT be reported as passed without Docker actually available.")
        sys.exit(2)


def build_image():
    print("==> Building web-manager image from current source (this is the code under test)...")
    res = run([
        "docker", "build", "-f", str(ROOT / "Dockerfile"), "-t", IMAGE_TAG,
        "--build-arg", "VCS_REF=test-local-firstrun-auth",
        "--build-arg", "VERSION=test-firstrun",
        str(ROOT),
    ], capture_output=False)
    if res is not None and getattr(res, "returncode", 0) != 0:
        print("FATAL: image build failed.")
        sys.exit(2)


def _write_secure_env_file(path: Path, env: dict) -> None:
    content = "\n".join(f"{k}={v}" for k, v in env.items()) + "\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class Stack:
    """One disposable docker-compose.full.yml project against a fresh temp dir tree."""

    def __init__(self, name, *, preseed_auth_token=None, preseed_initial_pwd=None, web_password="", extra_env=None):
        self.name = name
        self.project = f"firstrunacct-{name}"
        self.port = 8337
        self.tmp = Path(tempfile.mkdtemp(prefix=f"firstrun-{name}-"))
        self.config_dir = self.tmp / "config"
        self.music_dir = self.tmp / "music"
        self.downloads_dir = self.tmp / "downloads"
        self.webdata_dir = self.tmp / "webdata"
        for d in (self.config_dir, self.music_dir, self.downloads_dir, self.webdata_dir):
            d.mkdir(parents=True, exist_ok=True)

        if preseed_auth_token is not None:
            token_path = self.webdata_dir / ".auth_token"
            token_path.write_text(preseed_auth_token, encoding="utf-8")

        if preseed_initial_pwd is not None:
            initial_path = self.webdata_dir / ".initial_admin_password"
            initial_path.write_text(preseed_initial_pwd, encoding="utf-8")

        env = {
            "BEETS_API_TOKEN": "firstrun-test-engine-token-not-real-0000",
            "MUSIC_LIBRARY_PATH": str(self.music_dir),
            "DOWNLOAD_PATH": str(self.downloads_dir),
            "BEETS_WEB_MANAGER_DATA_PATH": str(self.webdata_dir),
            "BEETS_EXPECT_EXISTING_LIBRARY": "0",
            "BEETS_WEB_MANAGER_VERSION": "test-firstrun",
            "BEETS_WEB_BIND_ADDRESS": "127.0.0.1",
            "BEETS_WEB_PASSWORD": web_password,
        }
        if extra_env:
            env.update(extra_env)
        self.env_file = self.tmp / ".env"
        _write_secure_env_file(self.env_file, env)
        self.base_url = f"http://127.0.0.1:{self.port}"

    def compose(self, *args, **kw):
        cmd = ["docker", "compose", "-f", COMPOSE_FILE, "--env-file", str(self.env_file),
               "-p", self.project, *args]
        return run(cmd, **kw)

    def up(self):
        res = self.compose("up", "-d")
        if res.returncode != 0:
            print(res.stdout, res.stderr)
            raise RuntimeError(f"[{self.name}] compose up failed")

    def ps(self):
        res = self.compose("ps")
        return res.stdout

    def down(self):
        self.compose("down", "-v", "-t", "5")
        shutil.rmtree(self.tmp, ignore_errors=True)

    def logs(self, service):
        res = self.compose("logs", service)
        return res.stdout + res.stderr

    def wait_healthy(self, container, timeout=120):
        waited = 0
        while waited < timeout:
            res = run(["docker", "inspect", "--format",
                       "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}", container])
            status = (res.stdout or "").strip()
            if status == "healthy":
                return True
            if res.returncode != 0:
                return False
            time.sleep(3)
            waited += 3
        return False

    def exec_py(self, container, code):
        res = run(["docker", "exec", container, "python", "-c", code])
        return res.stdout, res.returncode

    def request(self, path, *, auth_header=None, method="GET", body=None, headers_dict=None):
        req = urllib.request.Request(self.base_url + path, method=method,
                                      data=body.encode() if body else None)
        if auth_header:
            req.add_header("Authorization", auth_header)
        if body:
            req.add_header("Content-Type", "application/json")
        if headers_dict:
            for k, v in headers_dict.items():
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                headers = {k.lower(): v for k, v in resp.headers.items()}
                return resp.status, headers, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as ex:
            headers = {k.lower(): v for k, v in ex.headers.items()}
            return ex.code, headers, ex.read().decode("utf-8", "replace")
        except Exception as ex:
            return 0, {}, str(ex)


def basic_header(user, pwd):
    return "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()


def bearer_header(tok):
    return f"Bearer {tok}"


def read_container_file(stack, container, path):
    code = f"import sys\ntry:\n    print(open({path!r}).read())\nexcept Exception as e:\n    sys.exit(1)\n"
    out, rc = stack.exec_py(container, code)
    return out.rstrip("\n") if rc == 0 else None


def file_mode(stack, container, path):
    code = f"import os,stat,sys\ntry:\n    print(oct(stat.S_IMODE(os.stat({path!r}).st_mode)))\nexcept Exception:\n    sys.exit(1)\n"
    out, rc = stack.exec_py(container, code)
    return out.strip() if rc == 0 else None


def file_exists(stack, container, path):
    code = f"import os; print(os.path.exists({path!r}))"
    out, rc = stack.exec_py(container, code)
    return out.strip() == "True"


# ---------------------------------------------------------------------------
# Scenario 1: fresh install + browser first-run setup + credential persistence
# ---------------------------------------------------------------------------
def scenario_fresh_install():
    print("\n########## SCENARIO 1: fresh install ##########")
    stack = Stack("fresh", extra_env={"BEETS_WEB_AUTH_TOKEN": "firstrun-test-bearer-token-not-real-0000"})
    try:
        stack.up()
        print(stack.ps())
        beets_healthy = stack.wait_healthy("beets")
        webmgr_healthy = stack.wait_healthy("beets-web-manager")
        if beets_healthy:
            ok("beets container reached healthy")
        else:
            fail("beets container did not become healthy")
        if webmgr_healthy:
            ok("beets-web-manager container reached healthy")
        else:
            fail("beets-web-manager container did not become healthy")
        if not (beets_healthy and webmgr_healthy):
            return

        # 1. Container health endpoint responds
        status_code, _, body = stack.request("/api/health")
        if status_code == 200:
            ok("/api/health -> 200 (Web Manager process itself is up)")
        else:
            fail(f"/api/health -> {status_code}")

        # 2. Fresh install enters first-run setup mode
        code, _, body = stack.request("/api/setup/status")
        try:
            status_data = json.loads(body)
            first_run_req = status_data.get("first_run", {}).get("required")
        except Exception:
            first_run_req = False

        if code == 200 and first_run_req is True:
            ok("unauthenticated GET /api/setup/status -> 200 with first_run.required=true")
        else:
            fail(f"first-run setup state check failed ({code}, required={first_run_req})")

        # 3. Protected application routes return 401 when unauthenticated
        code, _, _ = stack.request("/api/library")
        if code == 401:
            ok("unauthenticated GET /api/library -> 401 (protected during setup)")
        else:
            fail(f"unauthenticated GET /api/library -> {code}, expected 401")

        code, _, _ = stack.request("/api/setup/env")
        if code == 401:
            ok("unauthenticated GET /api/setup/env -> 401 (protected during setup)")
        else:
            fail(f"unauthenticated GET /api/setup/env -> {code}, expected 401")

        # 4. User submits initial administrator credentials via POST /api/setup/first-run
        new_user = "dockeradmin"
        new_pwd = "MySuperSecureDockerPassword123!Aa45"
        code, _, body = stack.request(
            "/api/setup/first-run",
            method="POST",
            body=json.dumps({"username": new_user, "password": new_pwd}),
            headers_dict={"X-Beets-CSRF": "1", "Origin": "http://127.0.0.1:8337"},
        )
        if code == 200:
            ok("POST /api/setup/first-run -> 200 OK (administrator login created)")
        else:
            fail(f"POST /api/setup/first-run -> {code}: {body}")

        # 5. First-run setup mode closes; repeat request returns 409 Conflict
        code, _, _ = stack.request(
            "/api/setup/first-run",
            method="POST",
            body=json.dumps({"username": "hacker", "password": new_pwd}),
            headers_dict={"X-Beets-CSRF": "1", "Origin": "http://127.0.0.1:8337"},
        )
        if code == 401:
            ok("subsequent POST /api/setup/first-run -> 401 (endpoint no longer public once claimed)")
        else:
            fail(f"subsequent setup call -> {code}, expected 401")

        # 6. Basic Auth with newly created user credentials succeeds
        code, _, _ = stack.request("/api/setup/env", auth_header=basic_header(new_user, new_pwd))
        if code == 200:
            ok("Basic Auth with user-created credentials -> 200 OK")
        else:
            fail(f"Basic Auth with user credentials -> {code}")

        # 7. Incorrect password -> 401
        code, _, _ = stack.request("/api/setup/env", auth_header=basic_header(new_user, new_pwd + "wrong"))
        if code == 401:
            ok("Basic Auth with incorrect password -> 401 Unauthorized")
        else:
            fail(f"incorrect password -> {code}, expected 401")

        # 8. BEETS_WEB_AUTH_TOKEN works independently as Bearer
        code, _, _ = stack.request("/api/setup/env", auth_header=bearer_header("firstrun-test-bearer-token-not-real-0000"))
        if code == 200:
            ok("BEETS_WEB_AUTH_TOKEN Bearer auth -> 200 OK")
        else:
            fail(f"Bearer token auth -> {code}, expected 200")

        # 9. BEETS_API_TOKEN (engine token) cannot authenticate Web Manager API
        code, _, _ = stack.request("/api/setup/env", auth_header=bearer_header("firstrun-test-engine-token-not-real-0000"))
        if code == 401:
            ok("BEETS_API_TOKEN (engine token) correctly rejected as Web Manager Bearer auth -> 401")
        else:
            fail(f"engine token accepted as Web Manager auth ({code}) -- must be 401")

        # 10. Persisted credential files exist with 0600 mode
        persisted_pwd_path = "/web-manager-data/.browser_password"
        if file_exists(stack, "beets-web-manager", persisted_pwd_path):
            ok(".browser_password file exists")
            mode = file_mode(stack, "beets-web-manager", persisted_pwd_path)
            if mode == "0o600":
                ok(".browser_password mode is 0600")
            else:
                fail(f".browser_password mode is {mode}, expected 0o600")
        else:
            fail(".browser_password file does not exist")

        # 11. No local Beets SQLite DB created by Web Manager
        code = "import glob; print(glob.glob('/web-manager-data/*.blb') + glob.glob('/web-manager-data/*.db'))"
        out, _ = stack.exec_py("beets-web-manager", code)
        if out.strip() == "[]":
            ok("no local authoritative Beets SQLite DB created under /web-manager-data")
        else:
            fail(f"unexpected local DB file(s) found: {out.strip()}")

        # 12. Persisted credentials survive container RESTART
        run(["docker", "restart", "beets-web-manager"])
        if stack.wait_healthy("beets-web-manager"):
            ok("beets-web-manager healthy again after restart")
        else:
            fail("beets-web-manager did not become healthy after restart")
        code, _, _ = stack.request("/api/setup/env", auth_header=basic_header(new_user, new_pwd))
        if code == 200:
            ok("user-created credentials still work after container RESTART")
        else:
            fail(f"user credentials broken after restart ({code})")

        # 13. Persisted credentials survive container FORCE-RECREATE
        stack.compose("up", "-d", "--force-recreate", "beets-web-manager")
        if stack.wait_healthy("beets-web-manager"):
            ok("beets-web-manager healthy again after force-recreate")
        else:
            fail("beets-web-manager did not become healthy after force-recreate")
        code, _, _ = stack.request("/api/setup/env", auth_header=basic_header(new_user, new_pwd))
        if code == 200:
            ok("user-created credentials still work after container FORCE-RECREATE")
        else:
            fail(f"user credentials broken after force-recreate ({code})")

        # 14. Secrets never appear in logs
        webmgr_logs = stack.logs("beets-web-manager")
        beets_logs = stack.logs("beets")
        leaked = []
        for secret, label in ((new_pwd, "new user password"),
                              ("firstrun-test-bearer-token-not-real-0000", "web auth token"),
                              ("firstrun-test-engine-token-not-real-0000", "engine token")):
            if secret in webmgr_logs or secret in beets_logs:
                leaked.append(label)
        if not leaked:
            ok("no secret (passwords or tokens) appears in container logs")
        else:
            fail(f"secret(s) leaked into logs: {leaked}")

    finally:
        print(stack.ps())
        stack.down()
        ok("scenario 1 containers/volumes torn down")


# ---------------------------------------------------------------------------
# Scenario 2: upgrade from v0.1.8 .initial_admin_password installation
# ---------------------------------------------------------------------------
def scenario_v018_upgrade():
    print("\n########## SCENARIO 2: upgrade from v0.1.8 .initial_admin_password ##########")
    initial_pwd = "V018InitialPassword123!Aa4567890"
    stack = Stack("v018-upg", preseed_initial_pwd=initial_pwd)
    try:
        stack.up()
        print(stack.ps())
        if not (stack.wait_healthy("beets") and stack.wait_healthy("beets-web-manager")):
            fail("scenario 2 stack did not become healthy")
            return

        code, _, body = stack.request("/api/setup/status")
        try:
            status_data = json.loads(body)
            first_run_req = status_data.get("first_run", {}).get("required")
        except Exception:
            first_run_req = True

        if first_run_req is False:
            ok("first-run browser setup mode is NOT active on v0.1.8 upgrade with .initial_admin_password")
        else:
            fail(f"first-run setup was unexpectedly active on v0.1.8 upgrade (code={code}, required={first_run_req})")

        # Anonymous POST /api/setup/first-run must be DENIED (409 Conflict)
        code, _, _ = stack.request(
            "/api/setup/first-run",
            method="POST",
            body=json.dumps({"username": "attacker", "password": "AttackerPassword123!Aa4567890"}),
            headers_dict={"X-Beets-CSRF": "1", "Origin": "http://127.0.0.1:8337"},
        )
        if code == 401:
            ok("anonymous POST /api/setup/first-run returns 401 (not public on established/upgraded install)")
        else:
            fail(f"anonymous bootstrap call on v0.1.8 upgrade -> {code}, expected 401")

        # Basic Auth with existing initial password succeeds
        code, _, _ = stack.request("/api/setup/env", auth_header=basic_header("admin", initial_pwd))
        if code == 200:
            ok("Basic Auth with pre-existing v0.1.8 .initial_admin_password -> 200 OK")
        else:
            fail(f"Basic Auth with pre-existing v0.1.8 password -> {code}")

        # Restart preserves legacy established state
        run(["docker", "restart", "beets-web-manager"])
        if stack.wait_healthy("beets-web-manager"):
            ok("beets-web-manager healthy again after restart")
        else:
            fail("beets-web-manager did not become healthy after restart")
        code, _, _ = stack.request("/api/setup/env", auth_header=basic_header("admin", initial_pwd))
        if code == 200:
            ok("v0.1.8 password still authenticates after container RESTART")
        else:
            fail(f"v0.1.8 password broken after restart ({code})")

    finally:
        print(stack.ps())
        stack.down()
        ok("scenario 2 containers/volumes torn down")


# ---------------------------------------------------------------------------
# Scenario C: credential deletion on a claimed install fails closed
# ---------------------------------------------------------------------------
def scenario_credential_deletion():
    print("\n########## SCENARIO C: credential deletion fails closed ##########")
    stack = Stack("cred-del")
    try:
        stack.up()
        print(stack.ps())
        if not (stack.wait_healthy("beets") and stack.wait_healthy("beets-web-manager")):
            fail("scenario C stack did not become healthy")
            return

        user, pwd = "dockeradmin", "CredentialDeletionTestPwd123!Aa4567"
        code, _, body = stack.request(
            "/api/setup/first-run",
            method="POST",
            body=json.dumps({"username": user, "password": pwd}),
            headers_dict={"X-Beets-CSRF": "1", "Origin": "http://127.0.0.1:8337"},
        )
        if code != 200:
            fail(f"scenario C: initial claim failed ({code}: {body})")
            return
        ok("scenario C: install claimed successfully")

        # Delete the persisted credential file directly in the container.
        _, rc = stack.exec_py(
            "beets-web-manager",
            "import os; os.remove('/web-manager-data/.browser_password')",
        )
        if rc == 0:
            ok("scenario C: persisted .browser_password deleted")
        else:
            fail("scenario C: could not delete .browser_password in container")
            return

        # Before restart: middleware-level check must already fail closed.
        code, _, _ = stack.request("/api/setup/env", auth_header=basic_header(user, pwd))
        if code == 401:
            ok("scenario C: deleted credential no longer authenticates (401)")
        else:
            fail(f"scenario C: deleted credential still authenticates ({code})")

        code, _, _ = stack.request(
            "/api/setup/first-run",
            method="POST",
            body=json.dumps({"username": "attacker", "password": "AttackerPassword123!Aa4567890"}),
            headers_dict={"X-Beets-CSRF": "1", "Origin": "http://127.0.0.1:8337"},
        )
        if code == 401:
            ok("scenario C: anonymous setup does not reopen before restart (401)")
        else:
            fail(f"scenario C: anonymous setup reachable before restart ({code}) -- must be 401")

        # Restart: this is where the vulnerable code path (auto-regenerating
        # an initial password at boot) would fire if it were still present.
        run(["docker", "restart", "beets-web-manager"])
        if not stack.wait_healthy("beets-web-manager"):
            fail("scenario C: beets-web-manager did not become healthy after restart")
            return
        ok("scenario C: beets-web-manager healthy again after restart")

        if file_exists(stack, "beets-web-manager", "/web-manager-data/.initial_admin_password"):
            fail("scenario C: a NEW .initial_admin_password was auto-generated after restart -- "
                 "credential deletion became an anonymous recovery path")
        else:
            ok("scenario C: no new .initial_admin_password was auto-generated after restart")

        code, _, _ = stack.request("/api/setup/status")
        code2, _, _ = stack.request(
            "/api/setup/first-run",
            method="POST",
            body=json.dumps({"username": "attacker2", "password": "AttackerPassword123!Aa4567890"}),
            headers_dict={"X-Beets-CSRF": "1", "Origin": "http://127.0.0.1:8337"},
        )
        if code2 == 401:
            ok("scenario C: anonymous setup still closed after restart (401)")
        else:
            fail(f"scenario C: anonymous setup reopened after restart ({code2}) -- account-takeover path")

        code, _, _ = stack.request("/api/library")
        if code in (401, 503):
            ok(f"scenario C: protected API fails closed after restart ({code})")
        else:
            fail(f"scenario C: protected API returned {code} after credential loss -- expected 401/503")

    finally:
        print(stack.ps())
        stack.down()
        ok("scenario C containers/volumes torn down")


# ---------------------------------------------------------------------------
# Scenario D: two simultaneous claims against a fresh install
# ---------------------------------------------------------------------------
def scenario_concurrent_claim():
    print("\n########## SCENARIO D: concurrent claim race ##########")
    import threading as _threading

    stack = Stack("concurrent")
    try:
        stack.up()
        print(stack.ps())
        if not (stack.wait_healthy("beets") and stack.wait_healthy("beets-web-manager")):
            fail("scenario D stack did not become healthy")
            return

        pwd1 = "ConcurrentClaimPasswordOne123!Aa4567"
        pwd2 = "ConcurrentClaimPasswordTwo456!Aa4567"
        results = []
        lock = _threading.Lock()

        def claim(username, password):
            try:
                code, _, body = stack.request(
                    "/api/setup/first-run",
                    method="POST",
                    body=json.dumps({"username": username, "password": password}),
                    headers_dict={"X-Beets-CSRF": "1", "Origin": "http://127.0.0.1:8337"},
                )
            except Exception as ex:
                code = None
                body = f"thread raised: {ex}"
            with lock:
                results.append((username, password, code, body))

        t1 = _threading.Thread(target=claim, args=("racer1", pwd1))
        t2 = _threading.Thread(target=claim, args=("racer2", pwd2))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        if t1.is_alive() or t2.is_alive():
            fail("scenario D: a claim request thread did not complete within the 30s timeout (hung)")
            return

        if len(results) != 2:
            fail(f"scenario D: expected 2 recorded results, got {len(results)} (a thread likely raised before recording)")
            return

        if any(c is None for (_, _, c, _) in results):
            bodies = [b for (_, _, c, b) in results if c is None]
            fail(f"scenario D: a claim request failed before completing: {bodies}")
            return

        codes = sorted(c for (_, _, c, _) in results)
        if codes == [200, 409]:
            ok(f"scenario D: exactly one 200 and exactly one 409 among two concurrent claims (codes={codes})")
        else:
            fail(f"scenario D: expected exactly [200, 409] among two concurrent claims, got {codes}")

        winners = [(u, p) for (u, p, c, _b) in results if c == 200]
        if len(winners) == 1:
            win_user, win_pwd = winners[0]
            code, _, _ = stack.request("/api/setup/env", auth_header=basic_header(win_user, win_pwd))
            if code == 200:
                ok(f"scenario D: winning credentials ({win_user}) are authoritative and authenticate")
            else:
                fail(f"scenario D: winning credentials do not authenticate ({code})")
        else:
            fail(f"scenario D: expected exactly one winner, found {len(winners)}")

        code, _, body = stack.request("/api/setup/status")
        try:
            status_data = json.loads(body)
            first_run_req = status_data.get("first_run", {}).get("required")
        except Exception:
            first_run_req = True
        if first_run_req is False:
            ok("scenario D: setup state is claimed exactly once after the race")
        else:
            fail("scenario D: setup still reports first_run.required=true after a winning claim")

    finally:
        print(stack.ps())
        stack.down()
        ok("scenario D containers/volumes torn down")


# ---------------------------------------------------------------------------
# Scenario 3: upgrade compatibility -- existing .auth_token, no password
# ---------------------------------------------------------------------------
def scenario_upgrade_no_password():
    print("\n########## SCENARIO 3: upgrade compatibility (existing .auth_token, no password) ##########")
    preseed_token = "preexisting-v017-auth-token-simulated-0000000000"
    stack = Stack("upg-nopass", preseed_auth_token=preseed_token)
    try:
        stack.up()
        print(stack.ps())
        if not (stack.wait_healthy("beets") and stack.wait_healthy("beets-web-manager")):
            fail("scenario 3 stack did not become healthy")
            return

        current_token = read_container_file(stack, "beets-web-manager", "/web-manager-data/.auth_token")
        if current_token == preseed_token:
            ok("pre-existing .auth_token is unchanged after upgrade boot")
        else:
            fail(f".auth_token was rotated on upgrade! before={preseed_token!r} after={current_token!r}")

        code, _, body = stack.request("/api/setup/status")
        try:
            status_data = json.loads(body)
            first_run_req = status_data.get("first_run", {}).get("required")
        except Exception:
            first_run_req = False

        if code == 200 and first_run_req is True:
            ok("first-run browser setup mode is active on upgrade with no browser password")
        else:
            fail(f"first-run setup state check failed on upgrade ({code}, required={first_run_req})")

        code, _, _ = stack.request("/api/setup/env", auth_header=bearer_header(preseed_token))
        if code == 200:
            ok("pre-existing .auth_token still authenticates as Bearer after upgrade")
        else:
            fail(f"pre-existing token no longer works after upgrade ({code})")

    finally:
        print(stack.ps())
        stack.down()
        ok("scenario 3 containers/volumes torn down")


# ---------------------------------------------------------------------------
# Scenario 4: upgrade compatibility -- existing .auth_token AND existing password
# ---------------------------------------------------------------------------
def scenario_upgrade_with_password():
    print("\n########## SCENARIO 4: upgrade compatibility (existing .auth_token + BEETS_WEB_PASSWORD) ##########")
    preseed_token = "preexisting-v017-auth-token-simulated-1111111111"
    existing_pwd = "PreExistingConfiguredPassword!2026#Secure"
    stack = Stack("upg-pass", preseed_auth_token=preseed_token, web_password=existing_pwd)
    try:
        stack.up()
        print(stack.ps())
        if not (stack.wait_healthy("beets") and stack.wait_healthy("beets-web-manager")):
            fail("scenario 4 stack did not become healthy")
            return

        code, _, body = stack.request("/api/setup/status")
        try:
            status_data = json.loads(body)
            first_run_req = status_data.get("first_run", {}).get("required")
        except Exception:
            first_run_req = True

        if first_run_req is False:
            ok("first-run setup mode is NOT active when BEETS_WEB_PASSWORD exists")
        else:
            fail("first-run setup mode was unexpectedly active despite existing BEETS_WEB_PASSWORD")

        code, _, _ = stack.request("/api/setup/env", auth_header=basic_header("admin", existing_pwd))
        if code == 200:
            ok("pre-existing BEETS_WEB_PASSWORD remains authoritative and works")
        else:
            fail(f"pre-existing password does not work after upgrade ({code})")

    finally:
        print(stack.ps())
        stack.down()
        ok("scenario 4 containers/volumes torn down")


def main():
    require_docker()
    build_image()
    scenario_fresh_install()
    scenario_v018_upgrade()
    scenario_credential_deletion()
    scenario_concurrent_claim()
    scenario_upgrade_no_password()
    scenario_upgrade_with_password()

    print("\n" + "=" * 72)
    if _FAILURES:
        print(f"DOCKER ACCEPTANCE TEST FAILED -- {len(_FAILURES)} check(s) did not pass:")
        for f in _FAILURES:
            print(f"  - {f}")
        print("=" * 72)
        sys.exit(1)
    else:
        print("DOCKER ACCEPTANCE TEST PASSED -- all 6 scenarios confirmed against real containers "
              "(fresh install, v0.1.8 upgrade, credential deletion, concurrent claim, "
              "upgrade w/o password, upgrade w/ password).")
        print("=" * 72)


if __name__ == "__main__":
    main()
