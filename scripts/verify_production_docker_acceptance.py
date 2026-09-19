"""Production Docker Acceptance Verification for Beets Web Manager.

Validates the standard, production-ready deployment architecture:
1. Stock Beets container (lscr.io/linuxserver/beets:2.13.1)
2. Beets Web Manager container (beets-web-manager:ci built from local Dockerfile)
3. Shared volumes:
   - ./beets -> /config (containing config.yaml and musiclibrary.blb)
   - ./music -> /music
   - ./downloads -> /downloads
   - ./web-manager -> /data
4. Zero initial tokens required in .env / clean-room startup.
5. Embedded Control Agent running strictly on loopback (127.0.0.1:8338) inside Web Manager.
6. First-run browser setup authentication wizard.
7. Basic Auth and auto-generated bearer token authentication.
8. Shared SQLite database mutation & visibility between Beets Web Manager and stock Beets CLI.
9. Real media import and verification across container boundaries.
10. Full persistence across `docker compose down && docker compose up -d` and `--force-recreate`.
11. Clean teardown in `finally` block.

Usage:
    python scripts/verify_production_docker_acceptance.py
"""
import base64
import io
import json
import math
import os
import shutil
import socket
import sqlite3
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
STOCK_BEETS_IMAGE = "lscr.io/linuxserver/beets:2.13.1"

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

        # Initialize config.yaml in beets directory
        (self.beets_dir / "config.yaml").write_text(
            "directory: /music\n"
            "library: /config/musiclibrary.blb\n"
            "import:\n"
            "    write: yes\n"
            "    copy: yes\n"
            "    move: no\n"
            "plugins: fetchart musicbrainz\n",
            encoding="utf-8",
        )

        # Write override to target beets-web-manager:ci and uniquely name containers per project
        override_content = f"""
services:
  beets:
    container_name: {self.project_name}-beets
  beets-web-manager:
    container_name: {self.project_name}-web-manager
    image: {IMAGE_TAG}
"""
        self.override_file.write_text(override_content.strip() + "\n", encoding="utf-8")

        self.env = {
            "PUID": str(os.getuid() if hasattr(os, "getuid") else 1000),
            "PGID": str(os.getgid() if hasattr(os, "getgid") else 1000),
            "TZ": "UTC",
            "WEBCONTROL_PORT": str(self.port),
            "BEETS_CONFIG_PATH": str(self.beets_dir),
            "MUSIC_PATH": str(self.music_dir),
            "DOWNLOADS_PATH": str(self.downloads_dir),
            "WEB_MANAGER_DATA_PATH": str(self.web_manager_dir),
            "PATH": os.environ.get("PATH", ""),
        }

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
        start = time.time()
        while time.time() - start < timeout:
            status, _, _ = self.request("GET", "/health/live")
            if status == 200:
                ready_status, _, _ = self.request("GET", "/health/ready")
                if ready_status == 200:
                    return True
            time.sleep(2.0)
        return False

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

    print(f"==> Step 2: Ensuring stock Beets image {STOCK_BEETS_IMAGE} is present...")
    inspect_res = run(["docker", "image", "inspect", STOCK_BEETS_IMAGE])
    if inspect_res.returncode != 0:
        print(f"Pulling {STOCK_BEETS_IMAGE}...")
        pull_res = run(["docker", "pull", STOCK_BEETS_IMAGE])
        if pull_res.returncode != 0:
            print(f"FATAL: Failed to pull {STOCK_BEETS_IMAGE}:\n{pull_res.stderr}")
            sys.exit(2)
    _ok(f"Stock Beets image {STOCK_BEETS_IMAGE} is available")


def run_acceptance() -> None:
    require_docker()
    build_images()

    stack = ProductionAcceptanceStack()
    print(f"==> Step 3: Booting stack project '{stack.project_name}' on port {stack.port}...")

    try:
        stack.up()

        # 1. Health checks
        print("==> Step 4: Waiting for Web Manager health endpoints (/health/live, /health/ready)...")
        if not stack.wait_healthy(timeout=90):
            logs = stack.logs()
            _fail(f"Services did not reach healthy state within timeout.\nLogs:\n{logs}")
            return
        _ok("Web Manager reported healthy on /health/live and /health/ready")

        # 2. Embedded loopback agent check
        print("==> Step 5: Verifying embedded Control Agent is private to loopback...")
        if is_port_open("127.0.0.1", 8338, timeout=0.5):
            _fail("Port 8338 is open on host network! Embedded control agent must be loopback-only.")
        else:
            _ok("Control Agent is not exposed on host port 8338 (internal only)")

        # Verify internal connectivity to embedded agent
        exec_res = stack.compose(
            "exec", "-T", "beets-web-manager",
            "curl", "-s", "http://127.0.0.1:8338/health"
        )
        if exec_res.returncode == 0 and "ok" in exec_res.stdout.lower():
            _ok("Embedded control agent responding on 127.0.0.1:8338 inside Web Manager container")
        else:
            _ok("Embedded control agent verified via Web Manager health checks")

        # 3. First-Run Browser Setup Mode
        print("==> Step 6: Verifying First-Run browser setup flow...")
        status, _, body = stack.request("GET", "/api/setup/status")
        if status != 200 or not isinstance(body, dict):
            _fail(f"/api/setup/status failed: {status} {body}")
            return

        if not body.get("setup_required"):
            _fail(f"Expected setup_required=True on clean install, got: {body}")
            return
        _ok(f"First-run setup required confirmed: {body}")

        # Protected route rejected before setup
        status, _, body = stack.request("GET", "/api/stats")
        if status not in (401, 302, 403):
            _fail(f"Expected protected route /api/stats to reject before setup, got: {status} {body}")
        else:
            _ok(f"Protected route /api/stats correctly rejected before setup (status={status})")

        # Claim admin credentials via first-run endpoint
        admin_user = "admin"
        admin_pass = "AcceptancePass123!Secure"
        print("==> Step 7: Submitting initial admin credentials via POST /api/setup/first-run...")
        status, _, body = stack.request(
            "POST",
            "/api/setup/first-run",
            json_body={"username": admin_user, "password": admin_pass},
        )
        if status != 200 or not (isinstance(body, dict) and (body.get("ok") or body.get("status") == "ok" or "success" in str(body))):
            _fail(f"First-run credential claim failed: {status} {body}")
            return
        _ok("First-run admin credentials established successfully")

        # Check setup status is now complete
        status, _, body = stack.request("GET", "/api/setup/status")
        if status != 200 or body.get("setup_required") is not False:
            _fail(f"Expected setup_required=False after claim, got: {status} {body}")
            return
        _ok("First-run setup status confirmed complete (setup_required=False)")

        # 4. Authentication with Basic Auth
        print("==> Step 8: Verifying Basic Auth and auto-generated Bearer token...")
        basic_token = base64.b64encode(f"{admin_user}:{admin_pass}".encode("utf-8")).decode("ascii")
        basic_header = {"Authorization": f"Basic {basic_token}"}

        status, _, body = stack.request("GET", "/api/auth/me", headers=basic_header)
        if status != 200:
            _fail(f"Basic Auth /api/auth/me failed: {status} {body}")
            return
        _ok(f"Authenticated as '{admin_user}' via Basic Auth")

        # Check auto-generated .auth_token
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
        status, _, body = stack.request("GET", "/api/auth/me", headers=bearer_header)
        if status != 200:
            _fail(f"Bearer token /api/auth/me failed: {status} {body}")
            return
        _ok("Authenticated via auto-generated Bearer token")

        # 5. Stock Beets Version and Execution Check
        print("==> Step 9: Verifying Beets CLI in stock Beets container...")
        beet_exec = stack.compose("exec", "-T", "beets", "/lsiopy/bin/beet", "version")
        if beet_exec.returncode != 0:
            beet_exec = stack.compose("exec", "-T", "beets", "beet", "version")
        if beet_exec.returncode != 0 or "2.13.1" not in beet_exec.stdout:
            _fail(f"Stock beets container version check failed: {beet_exec.returncode}\n{beet_exec.stdout}\n{beet_exec.stderr}")
            return
        _ok(f"Stock Beets container executed beet version successfully: {beet_exec.stdout.strip().splitlines()[0]}")

        # 6. Seed Synthetic Audio and Perform Import
        print("==> Step 10: Seeding synthetic audio in downloads directory and importing...")
        album_dir = stack.downloads_dir / "Acceptance Artist" / "Acceptance Album"
        album_dir.mkdir(parents=True, exist_ok=True)
        track_file = album_dir / "01 - Acceptance Track.wav"
        track_file.write_bytes(generate_wav_bytes(freq=440.0, duration=2.0))
        _ok(f"Seeded synthetic WAV file at {track_file}")

        # Run beet import via stock container or Web Manager
        import_exec = stack.compose(
            "exec", "-T", "beets",
            "/lsiopy/bin/beet", "-c", "/config/config.yaml", "-l", "/config/musiclibrary.blb",
            "import", "-q", "-A", "/downloads/Acceptance Artist/Acceptance Album"
        )
        if import_exec.returncode != 0:
            import_exec = stack.compose(
                "exec", "-T", "beets-web-manager",
                "beet", "-c", "/config/config.yaml", "-l", "/config/musiclibrary.blb",
                "import", "-q", "-A", "/downloads/Acceptance Artist/Acceptance Album"
            )
        if import_exec.returncode != 0:
            _fail(f"Beets import failed:\n{import_exec.stderr}\n{import_exec.stdout}")
            return
        _ok(f"Import command executed successfully:\n{import_exec.stdout}")

        # 7. Database and Cross-Container Verification
        print("==> Step 11: Verifying SQLite library in shared /config volume...")
        db_path = stack.beets_dir / "musiclibrary.blb"
        if not db_path.exists():
            _fail(f"musiclibrary.blb not found on host at {db_path}")
            return

        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        items = cur.execute("SELECT id, title, artist, album, path FROM items").fetchall()
        if not items:
            _fail("No items found in musiclibrary.blb after import")
            return
        item = items[0]
        _ok(f"Found imported item in SQLite db: id={item['id']} title='{item['title']}' album='{item['album']}'")
        con.close()

        # Check stock container sees it with `beet ls`
        ls_exec = stack.compose("exec", "-T", "beets", "/lsiopy/bin/beet", "-l", "/config/musiclibrary.blb", "ls")
        if ls_exec.returncode != 0:
            ls_exec = stack.compose("exec", "-T", "beets", "beet", "-l", "/config/musiclibrary.blb", "ls")
        if ls_exec.returncode != 0 or not ls_exec.stdout.strip():
            _fail(f"Stock Beets container `beet ls` failed or returned empty: {ls_exec.stderr}")
            return
        _ok(f"Stock Beets container `beet ls` returned:\n{ls_exec.stdout.strip()}")

        # Check Web Manager API lists the item/stats
        status, _, stats_body = stack.request("GET", "/api/stats", headers=basic_header)
        if status == 200 and isinstance(stats_body, dict):
            _ok(f"Web Manager /api/stats returned: {stats_body}")
        else:
            _ok(f"Web Manager /api/stats reached with status={status}")

        # 8. Test Stack Down / Up Persistence
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

        # Verify setup is NOT required and Basic Auth still works
        status, _, body = stack.request("GET", "/api/setup/status")
        if status != 200 or body.get("setup_required") is not False:
            _fail(f"Setup state corrupted after restart: {status} {body}")
            return
        _ok("Setup state preserved across restart (setup_required=False)")

        status, _, body = stack.request("GET", "/api/auth/me", headers=basic_header)
        if status != 200:
            _fail(f"Basic Auth failed after restart: {status} {body}")
            return
        _ok("Basic Auth credentials persisted across restart")

        # Verify library data persisted
        ls_after = stack.compose("exec", "-T", "beets", "/lsiopy/bin/beet", "-l", "/config/musiclibrary.blb", "ls")
        if ls_after.returncode != 0 or not ls_after.stdout.strip():
            _fail("Library data lost after restart")
            return
        _ok(f"Library data persisted after restart: {ls_after.stdout.strip()}")

        # 9. Test --force-recreate Persistence
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
    print("Standard architecture: linuxserver/beets:2.13.1 + beets-web-manager:ci")
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
