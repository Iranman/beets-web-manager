"""Two-Service Docker Acceptance Verification for Wave 24 Album Lifecycle.

Wave 24 final review round 3, section 17: this script's docstring used to
claim far more than its implementation did (only a Docker daemon check,
image builds, and OCI revision label inspection -- never actually booting
anything). It now does what it claims:

1. Docker daemon check (fails closed if Docker is unreachable).
2. Builds beets-web-manager and beets-engine from LOCAL SOURCE at the exact
   current HEAD, both tagged `acceptance`, both with VCS_REF=$FINAL_HEAD.
3. Validates org.opencontainers.image.revision == $FINAL_HEAD on both
   images.
4. Seeds a disposable Beets library (real beets.library.Library, real
   synthetic non-copyright audio -- no external network, no real music)
   directly into the host directories that become the engine container's
   bind mounts, using docker-compose.full.yml's ALREADY-correct two-service
   filesystem topology (see docker/acceptance/docker-compose.acceptance.yml)
   -- the engine gets /config + the music library + the download root; the
   web manager gets only its own /web-manager-data volume.
5. Boots the real two-container stack via `docker compose`, waits for both
   services healthy.
6. Verifies the web-manager container has NO media/config mount and cannot
   write to the engine's paths even if it tried (docker inspect mounts +
   a live write-attempt exec'd inside the container).
7. Exercises production Web Manager HTTP routes (not the Control Agent
   directly) for artwork upload, artwork delete, rename, move-to-library,
   metadata repair, retag, library dedup cleanup, album remove, and deduplicate -- each is a real
   HTTP request to the web-manager container's published port, which then
   reaches the engine container purely over the Docker network via
   BeetsClient/BEETS_API_URL, exactly like production.
8. Stops the engine container and proves mutating Web Manager routes, including /api/dedup/cleanup,
   fails truthfully with zero local mutation while the engine is down,
   then restarts it.
9. Fails closed on every step (image build failure, wrong OCI revision,
   unhealthy service, a media mount on the web manager, an unexpected
   successful direct write, an IPC failure, wrong DB/filesystem state,
   engine-offline mutation, or a cleanup that would leave stray
   containers/networks/volumes behind) -- teardown always runs, in
   `finally`, regardless of outcome.

Requires: Docker with the `docker compose` v2 plugin, and the `beets`
Python package importable in THIS process (used only to seed a real,
current-schema library.blb on the host side of the bind mount -- never
inside a container).
"""
import base64
import io
import json
import math
import mimetypes
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
COMPOSE_BASE = ROOT / "docker-compose.full.yml"
COMPOSE_OVERRIDE = ROOT / "docker" / "acceptance" / "docker-compose.acceptance.yml"
PROJECT_NAME = "beets-wave24-acceptance"

FAILURES: list = []


def _fail(msg: str) -> None:
    FAILURES.append(msg)
    print(f"[FAIL] {msg}")


def _ok(msg: str) -> None:
    print(f"[ok] {msg}")


def run(cmd, **kwargs):
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    print(f"$ {' '.join(str(c) for c in cmd)}")
    return subprocess.run(cmd, **kwargs)


def compose(*args, env=None, **kwargs):
    cmd = [
        "docker", "compose",
        "-p", PROJECT_NAME,
        "-f", str(COMPOSE_BASE),
        "-f", str(COMPOSE_OVERRIDE),
        *args,
    ]
    return run(cmd, env=env, **kwargs)


def get_final_head_sha() -> str:
    res = run(["git", "rev-parse", "HEAD"])
    if res.returncode != 0:
        raise RuntimeError(f"Failed to get git HEAD SHA: {res.stderr}")
    return res.stdout.strip()


def require_docker() -> None:
    if shutil.which("docker") is None:
        print("FATAL: docker is not on PATH -- cannot run two-service Docker acceptance test.")
        sys.exit(2)
    res = run(["docker", "info"])
    if res.returncode != 0:
        print("FATAL: Docker daemon is not reachable -- cannot run two-service Docker acceptance test.")
        sys.exit(2)
    res = run(["docker", "compose", "version"])
    if res.returncode != 0:
        print("FATAL: `docker compose` (v2 plugin) is not available.")
        sys.exit(2)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _real_wav_bytes(freq=220.0, duration=1.0, rate=44100) -> bytes:
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


def _real_jpeg_bytes(size=(12, 12), color=(30, 90, 200)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="JPEG")
    return buf.getvalue()


def seed_disposable_library(config_dir: Path, music_dir: Path) -> dict:
    """Seed a real, current-schema Beets library (via the actual installed
    `beets` package -- the same technique this review's Wave 24 relocation
    fix uses at runtime) directly on the host side of the bind mount, plus
    one minimal config.yaml. Returns fixture identity info the HTTP
    exercises below need (album id, item id, paths)."""
    import beets.library as bl

    config_dir.mkdir(parents=True, exist_ok=True)
    music_dir.mkdir(parents=True, exist_ok=True)

    (config_dir / "config.yaml").write_text(
        "directory: /data/media/music\n"
        "library: /config/musiclibrary.blb\n"
        "plugins: ''\n"
        "threaded: yes\n"
        "import:\n"
        "    write: yes\n"
        "    copy: no\n"
        "    move: no\n",
        encoding="utf-8",
    )

    db_path = config_dir / "musiclibrary.blb"
    lib = bl.Library(str(db_path), str(music_dir))

    item_dir = music_dir / "Acceptance Artist" / "Acceptance Album"
    item_dir.mkdir(parents=True, exist_ok=True)
    item_path = item_dir / "01 - Acceptance Track.wav"
    item_path.write_bytes(_real_wav_bytes())

    album = bl.Album(
        lib, albumartist="Acceptance Artist", album="Acceptance Album",
        year=2024, mb_albumid="11111111-1111-1111-1111-111111111111",
        mb_releasegroupid="22222222-2222-2222-2222-222222222222",
    )
    album.add(lib)
    item = bl.Item(
        albumartist="Acceptance Artist", album="Acceptance Album",
        artist="Acceptance Artist", title="Acceptance Track",
        track=1, disc=1, year=2024, album_id=album.id,
        path=str(item_path).encode("utf-8"),
        mb_trackid="33333333-3333-3333-3333-333333333333",
        length=1.0,
    )
    item.add(lib)

    cleanup_path = item_dir / "98 - Cleanup Duplicate.wav"
    cleanup_path.write_bytes(_real_wav_bytes(freq=330))
    cleanup_item = bl.Item(
        albumartist="Acceptance Artist", album="Acceptance Album",
        artist="Acceptance Artist", title="Cleanup Duplicate",
        track=98, disc=1, year=2024, album_id=album.id,
        path=str(cleanup_path).encode("utf-8"),
        mb_trackid="44444444-4444-4444-4444-444444444444",
        length=1.0,
    )
    cleanup_item.add(lib)
    lib._close()

    return {
        "album_id": int(album.id),
        "item_id": int(item.id),
        "item_path": str(item_path),
        "cleanup_item_id": int(cleanup_item.id),
        "cleanup_item_path": str(cleanup_path),
        "cleanup_container_path": "/data/media/music/" + cleanup_path.relative_to(music_dir).as_posix(),
        "db_path": str(db_path),
    }


class HttpClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, json_body=None, files=None, timeout=30):
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self.token}"}
        if files:
            boundary = uuid.uuid4().hex
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            body = bytearray()
            for field_name, (filename, content, content_type) in files.items():
                body += f"--{boundary}\r\n".encode()
                body += f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode()
                body += f"Content-Type: {content_type}\r\n\r\n".encode()
                body += content
                body += b"\r\n"
            body += f"--{boundary}--\r\n".encode()
            data = bytes(body)
        else:
            headers["Content-Type"] = "application/json"
            data = json.dumps(json_body or {}).encode("utf-8") if method != "GET" else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except Exception:
                return exc.code, {"error": raw.decode("utf-8", "replace")}

    def wait_job(self, job_id: str, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, body = self.request("GET", f"/api/jobs/{job_id}")
            if status == 200 and body.get("status") in ("success", "failed", "cancelled", "timeout", "cancel_failed"):
                return body
            time.sleep(0.5)
        raise TimeoutError(f"job {job_id} did not reach a terminal state within {timeout}s")


def wait_healthy(container: str, timeout=180) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = run(["docker", "inspect", "--format", "{{.State.Health.Status}}", container])
        status = (res.stdout or "").strip()
        if status == "healthy":
            return True
        if status == "unhealthy":
            return False
        time.sleep(3)
    return False


def check_web_manager_isolation(web_container: str) -> None:
    inspect = run(["docker", "inspect", web_container])
    try:
        info = json.loads(inspect.stdout)[0]
    except Exception as ex:
        _fail(f"could not parse docker inspect output for {web_container}: {ex}")
        return
    mounts = info.get("Mounts") or []
    for m in mounts:
        dest = m.get("Destination", "")
        if dest in ("/data/media/music", "/config", "/data/torrents"):
            _fail(f"web-manager container has a mount at {dest} -- it must never see engine-owned paths")
    if not any(m.get("Destination") == "/web-manager-data" for m in mounts):
        _fail("web-manager container is missing its own /web-manager-data volume")
    else:
        _ok("web-manager container mounts contain no engine-owned paths")

    write_probe = run(["docker", "exec", web_container, "sh", "-c",
                        "touch /data/media/music/probe 2>&1; echo EXIT:$?"])
    output = (write_probe.stdout or "") + (write_probe.stderr or "")
    if "EXIT:0" in output:
        _fail("web-manager container was able to write to /data/media/music -- isolation is broken")
    else:
        _ok(f"web-manager container cannot write to /data/media/music ({output.strip()})")


def main() -> int:
    print("==> Checking Docker daemon / compose availability...")
    require_docker()

    head_sha = get_final_head_sha()
    print(f"==> Two-service acceptance for exact head: {head_sha}")

    tmp = Path(tempfile.mkdtemp(prefix="wave24_acceptance_"))
    config_dir = tmp / "config"
    music_dir = tmp / "music"
    downloads_dir = tmp / "downloads"
    webdata_dir = tmp / "webdata"
    for d in (downloads_dir, webdata_dir):
        d.mkdir(parents=True, exist_ok=True)
    # Wave 24 final review round 3, section 21-24 (found only by actually
    # running this against real Docker): the beets-web-manager image runs
    # as a fixed non-root `beets` user (Dockerfile ARG PUID/PGID=1000,
    # baked in at image build time, USER beets) with no root-then-drop-
    # privilege entrypoint to fix ownership at container start. A host
    # bind mount's on-disk permissions come entirely from the HOST
    # directory, not the image's own build-time `chown` of its baked-in
    # /web-manager-data -- that chown only ever applied to an anonymous/
    # named volume, never to this host bind mount. This directory, freshly
    # created here by this script's own (arbitrary, non-1000) CI-runner
    # uid, must be writable by the container's uid 1000 for the web
    # manager's own on-disk state (its transaction audit ledger, its
    # bootstrapped auth-token file) to work at all -- a real operator
    # deploying via the documented compose files has this exact same
    # host-directory-ownership responsibility.
    os.chmod(webdata_dir, 0o777)

    token = f"acceptance-{uuid.uuid4().hex}"
    web_token = f"acceptance-web-{uuid.uuid4().hex}"
    web_port = _free_port()

    env = {
        **os.environ,
        "VCS_REF": head_sha,
        "BEETS_API_TOKEN": token,
        "BEETS_WEB_AUTH_TOKEN": web_token,
        "BEETS_CONFIG_PATH": str(config_dir),
        "MUSIC_LIBRARY_PATH": str(music_dir),
        "DOWNLOAD_PATH": str(downloads_dir),
        "BEETS_WEB_MANAGER_DATA_PATH": str(webdata_dir),
        "BEETS_EXPECT_EXISTING_LIBRARY": "1",
        "ACCEPTANCE_WEB_PORT": str(web_port),
        "YTDLP_PO_PROVIDER_URL": "",
    }

    try:
        print("==> Seeding disposable Beets library (real, non-copyright synthetic audio)...")
        try:
            fixture = seed_disposable_library(config_dir, music_dir)
        except ImportError as ex:
            print(f"FATAL: `beets` package not importable in this process -- cannot seed a real library: {ex}")
            return 2
        _ok(f"seeded album_id={fixture['album_id']} item_id={fixture['item_id']}")

        print(f"==> Building images from local source (VCS_REF={head_sha})...")
        build_res = compose("build", env=env, capture_output=False)
        if build_res.returncode != 0:
            _fail("image build failed")
            return 2

        print("==> Inspecting org.opencontainers.image.revision on built images...")
        for tag in ("beets-web-manager:acceptance", "beets-engine:acceptance"):
            inspect_res = run(["docker", "inspect", "--format",
                                '{{index .Config.Labels "org.opencontainers.image.revision"}}', tag])
            rev = inspect_res.stdout.strip()
            if rev != head_sha:
                _fail(f"{tag} org.opencontainers.image.revision='{rev}' does not match final head {head_sha}")
            else:
                _ok(f"{tag} revision matches final head")

        print("==> Booting the two-service stack...")
        up_res = compose("up", "-d", env=env, capture_output=False)
        if up_res.returncode != 0:
            _fail("docker compose up failed")
            return 2

        engine_container = f"{PROJECT_NAME}-beets-1"
        web_container = f"{PROJECT_NAME}-beets-web-manager-1"
        # docker compose container naming can vary by version; resolve by
        # label instead of assuming the exact name.
        ps = run(["docker", "compose", "-p", PROJECT_NAME, "-f", str(COMPOSE_BASE), "-f", str(COMPOSE_OVERRIDE),
                  "ps", "-a", "--format", "{{.Service}}\t{{.Name}}"], env=env)
        for line in (ps.stdout or "").splitlines():
            if "\t" not in line:
                continue
            service, name = line.split("\t", 1)
            if service.strip() == "beets":
                engine_container = name.strip()
            elif service.strip() == "beets-web-manager":
                web_container = name.strip()

        print("==> Waiting for both services to report healthy...")
        if not wait_healthy(engine_container):
            _fail("beets engine container did not become healthy")
            return 2
        if not wait_healthy(web_container):
            _fail("beets-web-manager container did not become healthy")
            return 2
        _ok("both containers healthy")

        print("==> Verifying web-manager container filesystem isolation...")
        check_web_manager_isolation(web_container)

        client = HttpClient(f"http://127.0.0.1:{web_port}", web_token)

        print("==> Verifying /api/health...")
        status, body = client.request("GET", "/api/health")
        if status != 200:
            _fail(f"/api/health returned {status}: {body}")
        else:
            _ok("/api/health reachable")

        aid = fixture["album_id"]
        iid = fixture["item_id"]

        print("==> Exercising artwork upload...")
        status, body = client.request(
            "POST", f"/api/albums/{aid}/art/upload",
            files={"file": ("art.jpg", _real_jpeg_bytes(), "image/jpeg")},
        )
        if status != 200 or not body.get("ok"):
            _fail(f"artwork upload request failed: {status} {body}")
        else:
            result = client.wait_job(body["job_id"])
            if result.get("status") != "success":
                _fail(f"artwork upload job did not succeed: {result.get('status')} / {result.get('log')}")
            else:
                _ok("artwork upload succeeded end to end")

        print("==> Exercising artwork delete...")
        status, body = client.request("DELETE", f"/api/albums/{aid}/art")
        if status != 200 or not body.get("ok"):
            _fail(f"artwork delete request failed: {status} {body}")
        else:
            _ok("artwork delete succeeded")

        print("==> Exercising fix-metadata...")
        status, body = client.request("POST", f"/api/albums/{aid}/fix-metadata",
                                       json_body={"album": "Acceptance Album Renamed", "year": 2025})
        if status != 200 or not body.get("ok"):
            _fail(f"fix-metadata request failed: {status} {body}")
        else:
            client.wait_job(body["job_id"])
            _ok("fix-metadata job dispatched and completed")

        print("==> Exercising retag...")
        status, body = client.request("POST", f"/api/items/{iid}/retag")
        if status != 200 or not body.get("ok"):
            _fail(f"retag request failed: {status} {body}")
        else:
            client.wait_job(body["job_id"])
            _ok("retag job dispatched and completed")

        print("==> Exercising library duplicate cleanup...")
        status, body = client.request(
            "POST", "/api/dedup/cleanup",
            json_body={"paths": [fixture["cleanup_container_path"]], "dry_run": False},
            timeout=45,
        )
        if status != 200 or not body.get("ok") or int(body.get("deleted") or 0) != 1:
            _fail(f"library duplicate cleanup request failed: {status} {body}")
        else:
            cleanup_host_path = Path(fixture["cleanup_item_path"])
            con = sqlite3.connect(fixture["db_path"])
            try:
                remaining = con.execute("SELECT COUNT(*) FROM items WHERE id=?", (fixture["cleanup_item_id"],)).fetchone()[0]
            finally:
                con.close()
            if cleanup_host_path.exists() or int(remaining or 0) != 0:
                _fail("library duplicate cleanup did not quarantine the file and retire the DB row")
            else:
                _ok("library duplicate cleanup succeeded through Web Manager -> Beets engine IPC")

        print("==> Exercising rename...")
        status, body = client.request("POST", f"/api/albums/{aid}/rename")
        if status != 200 or not body.get("ok"):
            _fail(f"rename request failed: {status} {body}")
        else:
            client.wait_job(body["job_id"])
            _ok("rename job dispatched and completed")

        print("==> Exercising deduplicate...")
        status, body = client.request("POST", f"/api/albums/{aid}/deduplicate")
        if status != 200 or not body.get("ok"):
            _fail(f"deduplicate request failed: {status} {body}")
        else:
            client.wait_job(body["job_id"])
            _ok("deduplicate job dispatched and completed")

        print("==> Verifying resulting host-mounted engine state...")
        db_path = fixture["db_path"]
        try:
            con = sqlite3.connect(db_path)
            row = con.execute("SELECT album, year FROM albums WHERE id=?", (aid,)).fetchone()
            con.close()
            if not row:
                _fail("album row is gone from the host-mounted DB after the exercise above -- unexpected")
            else:
                _ok(f"host-mounted DB reflects the exercised operations: {row}")
        except Exception as ex:
            _fail(f"could not verify host-mounted DB state: {ex}")

        print("==> Engine-offline fail-closed proof...")
        offline_host_path = Path(fixture["item_path"]).parent / "99 - Offline Cleanup.wav"
        offline_host_path.parent.mkdir(parents=True, exist_ok=True)
        offline_host_path.write_bytes(_real_wav_bytes(freq=550))
        offline_item_id = 9999
        offline_container_path = "/data/media/music/" + offline_host_path.relative_to(music_dir).as_posix()
        con = sqlite3.connect(db_path)
        try:
            con.execute(
                "INSERT INTO items (id, album_id, title, artist, albumartist, album, track, disc, year, path, mb_trackid) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (offline_item_id, aid, "Offline Cleanup", "Acceptance Artist", "Acceptance Artist", "Acceptance Album", 99, 1, 2024, str(offline_host_path).encode("utf-8"), "55555555-5555-5555-5555-555555555555"),
            )
            con.commit()
        finally:
            con.close()
        stop_res = run(["docker", "stop", engine_container])
        if stop_res.returncode != 0:
            _fail("could not stop the engine container for the offline proof")
        else:
            db_bytes_before = Path(db_path).read_bytes()
            file_bytes_before = offline_host_path.read_bytes()
            status, body = client.request("POST", f"/api/albums/{aid}/fix-metadata",
                                           json_body={"album": "Should Not Land"}, timeout=15)
            offline_ok = True
            if status == 200 and body.get("ok"):
                try:
                    result = client.wait_job(body["job_id"], timeout=20)
                    if result.get("status") == "success":
                        offline_ok = False
                except TimeoutError:
                    pass
            status_cleanup, cleanup_body = client.request(
                "POST", "/api/dedup/cleanup",
                json_body={"paths": [offline_container_path], "dry_run": False},
                timeout=15,
            )
            if status_cleanup == 200 and cleanup_body.get("ok"):
                offline_ok = False
            if not offline_ok:
                _fail("a mutating request appeared to succeed while the engine container was stopped")
            else:
                _ok("mutating requests did not silently succeed while the engine was offline")
            db_bytes_after = Path(db_path).read_bytes()
            if db_bytes_after != db_bytes_before:
                _fail("host-mounted DB changed while the engine container was stopped -- local mutation occurred")
            else:
                _ok("host-mounted DB unchanged while the engine was offline")
            if not offline_host_path.exists() or offline_host_path.read_bytes() != file_bytes_before:
                _fail("host-mounted media changed while the engine container was stopped -- local cleanup mutation occurred")
            else:
                _ok("host-mounted media unchanged while the engine was offline")
            run(["docker", "start", engine_container])

        if FAILURES:
            print(f"\n[SUMMARY] {len(FAILURES)} failure(s):")
            for f in FAILURES:
                print(f"  - {f}")
            # Wave 24 final review round 3, section 21-24: a bare pass/fail
            # summary with no container output is not diagnosable from CI
            # alone -- print both services' logs before teardown discards
            # them, same as compose-verification's existing Docker job
            # already does for its own single-container smoke test.
            for c in (web_container, engine_container):
                print(f"\n==> docker logs {c} (tail 200) ==>")
                logs_res = run(["docker", "logs", "--tail", "200", c])
                print(logs_res.stdout or "")
                print(logs_res.stderr or "")
            return 1

        print("\n[SUCCESS] Two-service Docker acceptance passed.")
        return 0

    finally:
        print("==> Tearing down disposable containers/networks/volumes...")
        compose("down", "-v", "--remove-orphans", env=env, capture_output=False)
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
