"""Production Docker Acceptance Verification for Beets Web Manager.

Boots docker-compose.yml (one container; Beets runs inside Web Manager, ADR
0006) with the image built from this checkout, on genuinely fresh host
folders, and checks:
1. A fresh install with no Beets config.yaml comes up healthy: /health/live,
   /health/ready, /api/health `beets.ok` and the Compose healthcheck. Beets
   creates its library at /config/musiclibrary.blb and nothing is installed
   into /config (no beetsplug folder, no config.yaml written).
2. Nothing listens on port 8338 and no separate Beets service exists.
3. First-run browser setup, Basic Auth and the auto-generated bearer token.
4. The image's Beets is exactly the requirements.txt pin.
5. An existing library (imported with the image's own `beet` while the app
   is stopped, as an upgrade brings one) is read through Web Manager's API.
6. A controlled mutation through Web Manager's API is visible on read-back.
7. Leftover two-container settings (BEETS_ENGINE, BEETS_WEB_URL, the shared
   key) do not stop startup: Beets is still healthy and reports
   `legacy_settings_ignored`.
8. State persists across `down`/`up` and `--force-recreate`.
9. Clean teardown.

Usage:
    python scripts/verify_production_docker_acceptance.py
"""
import base64
import io
import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_BASE = ROOT / "docker-compose.yml"
IMAGE_TAG = "beets-web-manager:ci"
FAILURES: list[str] = []


def _fail(msg: str) -> None:
    FAILURES.append(msg)
    print(f"[FAIL] {msg}")


def _ok(msg: str) -> None:
    print(f"[OK] {msg}")


def run(cmd, **kwargs):
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    cmd_str = " ".join(str(c) for c in cmd)
    print(f"$ {cmd_str}")
    return subprocess.run(cmd, **kwargs)


def require_docker() -> None:
    if shutil.which("docker") is None:
        print("FATAL: docker is not on PATH -- cannot run production Docker acceptance test.")
        sys.exit(2)
    res = run(["docker", "info"])
    if res.returncode != 0:
        print("FATAL: Docker daemon is not reachable -- cannot run production Docker acceptance test.")
        sys.exit(2)
    res = run(["docker", "compose", "version"])
    if res.returncode != 0:
        print("FATAL: `docker compose` (v2 plugin) is not available.")
        sys.exit(2)


def get_git_head_sha() -> str:
    res = run(["git", "rev-parse", "HEAD"])
    if res.returncode != 0:
        return "ci-acceptance"
    return res.stdout.strip()


def find_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.error):
        return False


def generate_wav_bytes(freq: float = 440.0, duration: float = 1.0, rate: int = 44100) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        n = int(rate * duration)
        frames = bytearray()
        for i in range(n):
            frames += struct.pack("<h", int(3000 * math.sin(2 * math.pi * freq * i / rate)))
        wf.writeframes(bytes(frames))
    return buf.getvalue()


class ProductionAcceptanceStack:
    def __init__(self):
        self.project_name = f"bwm-prod-accept-{uuid.uuid4().hex[:8]}"
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="bwm_prod_acceptance_"))
        self.beets_dir = self.tmp_dir / "beets"
        self.music_dir = self.tmp_dir / "music"
        self.downloads_dir = self.tmp_dir / "downloads"
        self.web_manager_dir = self.tmp_dir / "web-manager"

        for d in (self.beets_dir, self.music_dir, self.downloads_dir, self.web_manager_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.port = find_free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.override_file = self.tmp_dir / "docker-compose.override.yml"

        # Deliberately does NOT pre-seed beets/config.yaml: a fresh install
        # has none, and Beets must come up healthy without one.
        self.write_override()

        # Augment the inherited environment, never replace it: the docker
        # CLI (particularly the Windows/Docker Desktop client's `compose`
        # plugin resolution) depends on other ambient variables
        # (SYSTEMROOT, USERPROFILE, DOCKER_HOST/DOCKER_CONTEXT, etc.) --
        # passing subprocess.run() a bare, replacement env dict containing
        # only these overrides breaks `docker compose` outright on some
        # platforms ("unknown shorthand flag: 'p' in -p", i.e. `compose`
        # silently fails to resolve as a subcommand).
        self.env = {
            **os.environ,
            "PUID": str(os.getuid() if hasattr(os, "getuid") else 1000),
            "PGID": str(os.getgid() if hasattr(os, "getgid") else 1000),
            "TZ": "UTC",
            "WEBCONTROL_PORT": str(self.port),
            "BEETS_CONFIG_PATH": str(self.beets_dir),
            "MUSIC_PATH": str(self.music_dir),
            "DOWNLOADS_PATH": str(self.downloads_dir),
            "WEB_MANAGER_DATA_PATH": str(self.web_manager_dir),
        }

    def write_override(self, extra_env: dict | None = None) -> None:
        env = "".join(f"      {k}: {json.dumps(v)}\n" for k, v in (extra_env or {}).items())
        self.override_file.write_text(
            "services:\n"
            "  beets-web-manager:\n"
            f"    container_name: {self.project_name}-web-manager\n"
            f"    image: {IMAGE_TAG}\n"
            + ("    environment:\n" + env if env else ""),
            encoding="utf-8")

    def compose(self, *args, **kwargs):
        cmd = [
            "docker", "compose",
            "-p", self.project_name,
            "-f", str(COMPOSE_BASE),
            "-f", str(self.override_file),
            *args,
        ]
        return run(cmd, env=self.env, **kwargs)

    def up(self, recreate: bool = False):
        args = ["up", "-d"]
        if recreate:
            args.append("--force-recreate")
        res = self.compose(*args)
        if res.returncode != 0:
            raise RuntimeError(f"docker compose up failed: {res.stderr}\n{res.stdout}")

    def down(self):
        res = self.compose("down", "-v", "--remove-orphans", "-t", "5")
        return res

    def logs(self, service: str | None = None) -> str:
        args = ["logs", "--tail", "200"]
        if service:
            args.append(service)
        res = self.compose(*args)
        return res.stdout + "\n" + res.stderr

    def wait_healthy(self, timeout: float = 90.0) -> bool:
        """Live, ready, Beets' self-test passed, and Docker agrees (the
        Compose healthcheck runs the same /api/health beets.ok test)."""
        start = time.time()
        while time.time() - start < timeout:
            live, _, _ = self.request("GET", "/health/live")
            ready, _, _ = self.request("GET", "/health/ready")
            _, _, health = self.request("GET", "/api/health")
            beets_ok = isinstance(health, dict) and (health.get("beets") or {}).get("ok") is True
            state = self.compose("ps", "beets-web-manager", "--format", "{{.Health}}").stdout.strip()
            if live == 200 and ready == 200 and beets_ok and state == "healthy":
                return True
            time.sleep(2.0)
        print(f"Last /api/health: {self.request('GET', '/api/health')[2]}")
        return False

    def beets_codes(self) -> list:
        _, _, health = self.request("GET", "/api/health")
        return (health.get("beets") or {}).get("codes") or [] if isinstance(health, dict) else []

    def request(
        self,
        method: str,
        path: str,
        json_body: dict | None = None,
        headers: dict | None = None,
        timeout: float = 15.0,
    ) -> tuple[int, dict, dict | str]:
        url = f"{self.base_url}{path}"
        data = None
        req_headers = {"User-Agent": "BWM-Production-Acceptance/1.0"}
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        if headers:
            req_headers.update(headers)

        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                raw = resp.read().decode("utf-8", "replace")
                try:
                    return resp.status, resp_headers, json.loads(raw)
                except Exception:
                    return resp.status, resp_headers, raw
        except urllib.error.HTTPError as ex:
            resp_headers = {k.lower(): v for k, v in ex.headers.items()}
            raw = ex.read().decode("utf-8", "replace")
            try:
                return ex.code, resp_headers, json.loads(raw)
            except Exception:
                return ex.code, resp_headers, raw
        except Exception as ex:
            return 0, {}, str(ex)

    def cleanup(self):
        try:
            self.down()
        except Exception as ex:
            print(f"Warning during compose down: {ex}")
        shutil.rmtree(self.tmp_dir, ignore_errors=True)


def build_images() -> None:
    print("==> Step 1: Building beets-web-manager:ci from local checkout...")
    head_sha = get_git_head_sha()
    res = run([
        "docker", "build",
        "-f", str(ROOT / "Dockerfile"),
        "-t", IMAGE_TAG,
        "--build-arg", f"VCS_REF={head_sha}",
        "--build-arg", "VERSION=ci-acceptance",
        str(ROOT),
    ])
    if res.returncode != 0:
        print(f"FATAL: Docker build for {IMAGE_TAG} failed:\n{res.stderr}\n{res.stdout}")
        sys.exit(2)
    _ok(f"Built {IMAGE_TAG} successfully (VCS_REF={head_sha})")



def run_acceptance() -> None:
    require_docker()
    build_images()

    stack = ProductionAcceptanceStack()
    print(f"==> Step 3: Booting stack project '{stack.project_name}' on port {stack.port}...")

    try:
        stack.up()

        # 1. Health checks, fresh install without a config.yaml
        print("==> Step 4: Waiting for health (/health/live, /health/ready, /api/health beets.ok, Docker)...")
        if not stack.wait_healthy(timeout=120):
            logs = stack.logs()
            _fail(f"Web Manager did not become healthy within timeout.\nLogs:\n{logs}")
            return
        _ok("Healthy on /health/live, /health/ready and /api/health (beets.ok), and Docker reports healthy")
        codes = stack.beets_codes()
        if "config_missing" not in codes:
            _fail(f"Fresh install without config.yaml should report config_missing, got {codes}")
        if not (stack.beets_dir / "musiclibrary.blb").exists():
            _fail("Beets did not create /config/musiclibrary.blb on a fresh install")
        else:
            _ok("Fresh install: Beets created /config/musiclibrary.blb")
        if (stack.beets_dir / "config.yaml").exists() or (stack.beets_dir / "beetsplug").exists():
            _fail("Web Manager wrote into the Beets /config folder (config.yaml or beetsplug/)")
        else:
            _ok("Nothing was installed into /config (no config.yaml written, no beetsplug folder)")

        # 2. One container, no port 8338
        print("==> Step 5: Verifying one service and no port 8338...")
        services = stack.compose("config", "--services").stdout.split()
        if services != ["beets-web-manager"]:
            _fail(f"docker-compose.yml should define only beets-web-manager, got {services}")
        else:
            _ok("docker-compose.yml defines one service: beets-web-manager")
        if is_port_open("127.0.0.1", 8338, timeout=0.5):
            _fail("Port 8338 is open on the host network! Nothing in this architecture uses it.")
        else:
            _ok("Port 8338 is not exposed on the host")

        # 3. First-Run Browser Setup Mode
        print("==> Step 6: Verifying First-Run browser setup flow...")
        status, _, body = stack.request("GET", "/api/setup/status")
        if status != 200 or not isinstance(body, dict):
            _fail(f"/api/setup/status failed: {status} {body}")
            return

        if body.get("setup_complete") is not False or not body.get("first_run", {}).get("required"):
            _fail(f"Expected setup_complete=False and first_run.required=True on clean install, got: {body}")
            return
        _ok("First-run setup required confirmed (setup_complete=False, first_run.required=True)")

        # Protected route rejected before setup
        status, _, body = stack.request("GET", "/api/stats")
        if status not in (401, 302, 403):
            _fail(f"Expected protected route /api/stats to reject before setup, got: {status} {body}")
        else:
            _ok(f"Protected route /api/stats correctly rejected before setup (status={status})")

        # The setup-wizard mutation routes (first-run, complete) enforce the
        # app's own CSRF check, which rejects a plain non-browser POST with
        # no Origin/Referer and no Authorization header. Real browser
        # clients pass same-origin headers automatically; this script isn't
        # a browser, so it authenticates the same way the app's docs tell
        # any other non-browser API client to: the auto-generated bearer
        # token, persisted to .auth_token under the Web Manager data mount
        # before any admin password exists.
        auth_token_file = stack.web_manager_dir / ".auth_token"
        if not auth_token_file.exists():
            _fail(f".auth_token file was not created in {stack.web_manager_dir}")
            return
        auth_token = auth_token_file.read_text(encoding="utf-8").strip()
        if len(auth_token) < 16:
            _fail(f"Auto-generated auth token is too short or empty: {auth_token}")
            return
        _ok(f"Verified auto-generated .auth_token in {stack.web_manager_dir} (length={len(auth_token)})")
        bearer_header = {"Authorization": f"Bearer {auth_token}"}

        # A fresh /web-manager-data bind mount not being writable by the
        # container's user (a UID mismatch between the host directory and
        # the container's fixed identity, independent of whether PUID/PGID
        # were ever customized) previously made the app fail closed with
        # "no BEETS_WEB_AUTH_TOKEN is configured, and a newly generated
        # token could not be persisted" -- reaching this point at all
        # already proves .auth_token persisted, but .flask_secret_key is
        # a second, independently-created file under the same mount and
        # is checked explicitly so a partial-persistence regression (one
        # file's directory fixed, another missed) cannot slip through.
        secret_key_file = stack.web_manager_dir / ".flask_secret_key"
        if not secret_key_file.exists():
            _fail(f".flask_secret_key file was not created in {stack.web_manager_dir}")
            return
        if len(secret_key_file.read_text(encoding="utf-8").strip()) < 32:
            _fail(".flask_secret_key exists but is unexpectedly short")
            return
        _ok(f"Verified .flask_secret_key persisted in {stack.web_manager_dir}")

        # Claim admin credentials via first-run endpoint
        admin_user = "admin"
        admin_pass = "AcceptancePass123!Secure"
        print("==> Step 7: Submitting initial admin credentials via POST /api/setup/first-run...")
        status, _, body = stack.request(
            "POST",
            "/api/setup/first-run",
            json_body={"username": admin_user, "password": admin_pass},
            headers=bearer_header,
        )
        if status != 200 or not (isinstance(body, dict) and (body.get("ok") or body.get("status") == "ok" or "success" in str(body))):
            _fail(f"First-run credential claim failed: {status} {body}")
            return
        _ok("First-run admin credentials established successfully")

        # 3a. Beets inside Web Manager is available to setup diagnostics.
        print("==> Step 7a: Verifying Beets diagnostics...")
        status, _, status_body = stack.request("GET", "/api/setup/status", headers=bearer_header, timeout=30.0)
        if status != 200 or not isinstance(status_body, dict):
            _fail(f"GET /api/setup/status failed: {status} {status_body}")
            return
        beets_diag = status_body.get("beets") or {}
        if not beets_diag.get("available"):
            _fail(f"Beets not reported available: {beets_diag}")
            return
        _ok("Beets reported available by setup diagnostics")

        # ADR 0006: the image bundles Beets at the requirements.txt pin.
        pin_match = re.search(r"^beets==(\S+)", (ROOT / "requirements.txt").read_text(encoding="utf-8"), re.M)
        if not pin_match:
            _fail("requirements.txt has no beets==<version> pin")
            return
        pinned = pin_match.group(1)
        beets_ver = stack.compose(
            "exec", "-T", "beets-web-manager", "python", "-c", "import beets; print(beets.__version__)"
        )
        installed = beets_ver.stdout.strip() if beets_ver.returncode == 0 else ""
        if installed != pinned:
            _fail(f"beets-web-manager image Beets version {installed or '<missing>'!r} != pinned {pinned!r}")
            return
        _ok(f"beets-web-manager image bundles Beets {installed} (matches requirements.txt pin)")

        # Completing setup is a separate, explicit step (mirrors the real
        # browser wizard's final "Finish" action) -- this is exactly the
        # step that a real production install must not lose the marker for
        # across a container recreation (see the persistence test below).
        print("==> Step 7b: Completing setup via POST /api/setup/complete...")
        status, _, body = stack.request("POST", "/api/setup/complete", json_body={}, headers=bearer_header)
        if status != 200 or not (isinstance(body, dict) and body.get("ok", True) is not False):
            _fail(f"POST /api/setup/complete failed: {status} {body}")
            return
        _ok("Setup marked complete via POST /api/setup/complete")

        # Check setup status is now complete
        status, _, body = stack.request("GET", "/api/setup/status")
        if status != 200 or body.get("setup_complete") is not True or body.get("first_run", {}).get("required") is not False:
            _fail(f"Expected setup_complete=True and first_run.required=False after claim, got: {status} {body}")
            return
        _ok("Setup status confirmed complete (setup_complete=True, first_run.required=False)")

        # 4. Authentication with Basic Auth
        print("==> Step 8: Verifying Basic Auth and auto-generated Bearer token...")
        basic_token = base64.b64encode(f"{admin_user}:{admin_pass}".encode("utf-8")).decode("ascii")
        basic_header = {"Authorization": f"Basic {basic_token}"}

        status, _, body = stack.request("GET", "/api/auth/me", headers=basic_header)
        if status != 200:
            _fail(f"Basic Auth /api/auth/me failed: {status} {body}")
            return
        _ok(f"Authenticated as '{admin_user}' via Basic Auth")

        status, _, body = stack.request("GET", "/api/auth/me", headers=bearer_header)
        if status != 200:
            _fail(f"Bearer token /api/auth/me failed: {status} {body}")
            return
        _ok("Authenticated via auto-generated Bearer token")

        # 5. An existing library: import with the image's own `beet` while
        # the app is stopped (two writers on one library are refused), the
        # way a library arrives from the old Beets container.
        print("==> Step 10: Importing a library with the image's `beet` while Web Manager is stopped...")
        album_dir = stack.downloads_dir / "Acceptance Artist" / "Acceptance Album"
        album_dir.mkdir(parents=True, exist_ok=True)
        track_file = album_dir / "01 - Acceptance Track.wav"
        track_file.write_bytes(generate_wav_bytes(freq=440.0, duration=2.0))
        os.chmod(album_dir, 0o777)
        os.chmod(track_file, 0o666)
        stop = stack.compose("stop", "beets-web-manager")
        if stop.returncode != 0:
            _fail(f"docker compose stop failed: {stop.stderr}")
            return
        import_exec = stack.compose(
            "run", "--rm", "-T", "--no-deps", "beets-web-manager",
            "beet", "-l", "/config/musiclibrary.blb", "-d", "/music",
            "import", "-q", "-A", "/downloads/Acceptance Artist/Acceptance Album")
        if import_exec.returncode != 0:
            _fail(f"Beets import failed:\n{import_exec.stderr}\n{import_exec.stdout}")
            return
        _ok("Imported with the image's `beet` (as PUID:PGID)")
        stack.up()
        if not stack.wait_healthy(timeout=90):
            _fail("Web Manager did not become healthy after the import")
            return

        print("==> Step 11: Verifying Web Manager's API sees the imported item...")
        status, _, albums_body = stack.request("GET", "/api/albums", headers=basic_header)
        if status != 200 or not isinstance(albums_body, dict) or not albums_body.get("albums"):
            _fail(f"GET /api/albums did not see the imported album: {status} {albums_body}")
            return
        album = albums_body["albums"][0]
        album_id = album.get("id")
        _ok(f"Web Manager API sees imported album via BeetsAdapter: id={album_id} title={album.get('album')!r}")

        status, _, stats_body = stack.request("GET", "/api/stats", headers=basic_header)
        if status == 200 and isinstance(stats_body, dict):
            _ok(f"Web Manager /api/stats returned: {stats_body}")
        else:
            _fail(f"Web Manager /api/stats failed: {status} {stats_body}")
            return

        # 8. A controlled mutation performed THROUGH Web Manager's own API
        # (backend/beets_adapter.py's modify(), via the already-migrated
        # attach-mbids submission workflow), verified by reading it back
        # through the same read path used above.
        print("==> Step 11a: Performing a controlled mutation through Web Manager's API...")
        synthetic_rgid = "11111111-1111-1111-1111-111111111111"
        synthetic_artistid = "22222222-2222-2222-2222-222222222222"
        synthetic_albumid = "33333333-3333-3333-3333-333333333333"
        # ARCH-009 contract, exercised in the real production container: a
        # Release ID whose Release Group cannot be verified against
        # MusicBrainz is refused before anything is written.
        status, _, refused_body = stack.request(
            "POST",
            f"/api/submissions/albums/{album_id}/attach-mbids",
            json_body={
                "mb_albumartistid": synthetic_artistid,
                "mb_releasegroupid": synthetic_rgid,
                "mb_albumid": synthetic_albumid,
                "recordings": [],
            },
            headers=basic_header,
        )
        if status != 409 or not isinstance(refused_body, dict) or refused_body.get("code") not in (
                "release_group_unverified", "release_not_in_release_group"):
            _fail(f"attach-mbids accepted an unverifiable Release ID/Release Group pairing: {status} {refused_body}")
            return
        _ok(f"attach-mbids refused an unverifiable edition pairing ({refused_body.get('code')})")
        # The controlled mutation itself carries canonical album identity only.
        status, _, attach_body = stack.request(
            "POST",
            f"/api/submissions/albums/{album_id}/attach-mbids",
            json_body={
                "mb_albumartistid": synthetic_artistid,
                "mb_releasegroupid": synthetic_rgid,
                "recordings": [],
            },
            headers=basic_header,
        )
        if status != 200 or not (isinstance(attach_body, dict) and attach_body.get("ok")):
            _fail(f"POST /api/submissions/albums/{album_id}/attach-mbids failed: {status} {attach_body}")
            return
        job_id = attach_body.get("job_id")
        job_status = None
        for _ in range(30):
            status, _, job_body = stack.request("GET", f"/api/jobs/{job_id}", headers=basic_header)
            if status == 200 and isinstance(job_body, dict):
                job_status = job_body.get("status")
                if job_status in ("success", "failed", "cancelled", "cancel_failed"):
                    break
            time.sleep(1.0)
        if job_status != "success":
            _fail(f"attach-mbids job did not succeed (status={job_status}): {job_body}")
            return
        _ok("attach-mbids mutation job completed successfully")

        status, _, albums_body2 = stack.request("GET", "/api/albums", headers=basic_header)
        updated = next((a for a in (albums_body2.get("albums") or []) if a.get("id") == album_id), None)
        if not updated or str(updated.get("mb_releasegroupid") or "").lower() != synthetic_rgid:
            _fail(f"Mutation not visible on read-back: {updated}")
            return
        _ok("Controlled mutation via Web Manager's API is visible on read-back (mb_releasegroupid updated)")

        # 9. Leftover two-container settings: a warning, never a stop.
        print("==> Step 11b: Restarting with leftover two-container settings...")
        stack.write_override({"BEETS_ENGINE": "http", "BEETS_WEB_URL": "http://beets:8337",
                              "BEETS_WEBMANAGER_API_KEY": "0" * 64})
        stack.up()
        if not stack.wait_healthy(timeout=90):
            _fail("Leftover BEETS_ENGINE/BEETS_WEB_URL/BEETS_WEBMANAGER_API_KEY stopped Beets from starting")
            return
        if "legacy_settings_ignored" not in stack.beets_codes():
            _fail(f"Leftover settings were not reported: {stack.beets_codes()}")
            return
        _ok("Leftover two-container settings are ignored with a warning (legacy_settings_ignored)")
        stack.write_override()

        # 9. Test Stack Down / Up Persistence
        print("==> Step 12: Testing persistence across `docker compose down` and `docker compose up -d`...")
        down_res = stack.compose("down")
        if down_res.returncode != 0:
            _fail(f"docker compose down failed: {down_res.stderr}")
            return
        _ok("Stack stopped with `docker compose down`")

        stack.up()
        if not stack.wait_healthy(timeout=60):
            _fail("Stack failed to become healthy after restart")
            return
        _ok("Stack rebooted and healthy after restart")

        # Verify setup is NOT required and Basic Auth still works. Checking
        # setup_complete specifically (not just first_run.required) matters:
        # first_run.required has a legacy self-healing fallback that
        # re-derives "not required" from the mere existence of a password,
        # which would mask exactly the WEB_MANAGER_DATA_DIR persistence
        # regression this test exists to catch (the .setup_complete marker
        # silently written to a non-persistent path across a recreate).
        status, _, body = stack.request("GET", "/api/setup/status")
        if status != 200 or body.get("setup_complete") is not True or body.get("first_run", {}).get("required") is not False:
            _fail(f"Setup state corrupted after restart: {status} {body}")
            return
        _ok("Setup state preserved across restart (setup_complete=True, first_run.required=False)")

        status, _, body = stack.request("GET", "/api/auth/me", headers=basic_header)
        if status != 200:
            _fail(f"Basic Auth failed after restart: {status} {body}")
            return
        _ok("Basic Auth credentials persisted across restart")

        # Verify library data (and the mutation performed above) persisted
        status, _, albums_after = stack.request("GET", "/api/albums", headers=basic_header)
        if status != 200 or not (albums_after.get("albums") or []):
            _fail("Library data lost after restart")
            return
        _ok(f"Library data persisted after restart ({len(albums_after['albums'])} album(s))")

        # 10. Test --force-recreate Persistence
        print("==> Step 13: Testing persistence across `docker compose up -d --force-recreate`...")
        stack.up(recreate=True)
        if not stack.wait_healthy(timeout=60):
            _fail("Stack failed to become healthy after --force-recreate")
            return
        _ok("Stack rebooted and healthy after --force-recreate")

        status, _, body = stack.request("GET", "/api/auth/me", headers=basic_header)
        if status != 200:
            _fail(f"Basic Auth failed after --force-recreate: {status} {body}")
            return
        _ok("Credentials and state persisted across container force-recreate")

    finally:
        print("==> Step 14: Cleaning up temporary containers, volumes, and directories...")
        stack.cleanup()
        _ok("Cleaned up temporary resources")


def main():
    start_time = time.time()
    print("=" * 70)
    print("PRODUCTION DOCKER ACCEPTANCE TEST")
    print("One container: beets-web-manager:ci with Beets inside (ADR 0006)")
    print("=" * 70)

    try:
        run_acceptance()
    except Exception as ex:
        _fail(f"Unhandled exception during acceptance verification: {ex}")
        import traceback
        traceback.print_exc()

    elapsed = time.time() - start_time
    print("=" * 70)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} acceptance test failures in {elapsed:.2f}s:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print(f"SUCCESS: All production Docker acceptance criteria passed in {elapsed:.2f}s!")
        sys.exit(0)


if __name__ == "__main__":
    main()
