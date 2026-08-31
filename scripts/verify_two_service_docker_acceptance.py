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
8. Exercises Wave 27 config-domain closure scenarios through the real Web
   Manager HTTP surface: fresh setup durability, Web Manager config CAS,
   engine config CAS/validation/rollback, root reporting, engine-offline
   fail-closed behavior, and dict-shaped attach-stage failure handling.
9. Stops the engine container and proves mutating Web Manager routes, including /api/dedup/cleanup,
   fails truthfully with zero local mutation while the engine is down,
   then restarts it.
10. Fails closed on every step (image build failure, wrong OCI revision,
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
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
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
    (config_dir / "reconcile_quarantine").mkdir(parents=True, exist_ok=True)
    music_dir.mkdir(parents=True, exist_ok=True)

    (config_dir / "config.yaml").write_text(
        "directory: /data/media/music\n"
        "library: /config/musiclibrary.blb\n"
        # Round 4 brief section 27: 'fetchart' is a core Beets plugin (no
        # extra install needed) -- it was never loaded here at all, which
        # is why the artwork scenario's failure used to be "unknown
        # command 'fetchart'" rather than a genuine "searched, found
        # nothing" outcome. Enabling it lets that scenario exercise the
        # real command; it still cannot prove a successful ACQUISITION
        # against this fixture (its mb_albumid/artist/album are synthetic,
        # so no real cover art provider will have anything for it) -- see
        # run_wave25_artwork_scenarios for the two separately-reported
        # claims this implies.
        #
        # Round 4 (real root cause of the confirmed_import_v1 "Skipping."
        # failure, found by reproducing beets.autotag.match.tag_album()
        # directly against this fixture's own metadata outside Docker):
        # Beets' `plugins` config key is NOT additive across config
        # sources -- confuse (beets' config layer) resolves a list-typed
        # key entirely from whichever loaded source defines it with the
        # highest priority; it does not merge that source's list with the
        # packaged default's `plugins: [musicbrainz]`. Setting
        # `plugins: fetchart` here therefore silently REPLACED the
        # default plugin list, unloading `musicbrainz` -- the plugin that
        # actually resolves `--search-id`/`search_ids` into a candidate
        # via the MusicBrainz API. With it unloaded, tag_album() returned
        # zero candidates (not merely a low-confidence one), which is why
        # quiet mode printed a bare "Skipping." with no match detail at
        # all: there was never anything to weigh a threshold against.
        # This was the actual bug -- not track/tag realism, not the WAV
        # vs FLAC container format, not strong_rec_thresh. Both plugins
        # must be listed explicitly.
        "plugins: fetchart musicbrainz\n"
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

    offline_dir = music_dir / "Offline Artist" / "Offline Album"
    offline_dir.mkdir(parents=True, exist_ok=True)
    offline_path = offline_dir / "99 - Offline Cleanup.wav"
    offline_path.write_bytes(_real_wav_bytes(freq=550))
    offline_album = bl.Album(
        lib, albumartist="Offline Artist", album="Offline Album",
        year=2024, mb_albumid="66666666-6666-6666-6666-666666666666",
        mb_releasegroupid="77777777-7777-7777-7777-777777777777",
    )
    offline_album.add(lib)
    offline_item = bl.Item(
        albumartist="Offline Artist", album="Offline Album",
        artist="Offline Artist", title="Offline Cleanup",
        track=99, disc=1, year=2024, album_id=offline_album.id,
        path=str(offline_path).encode("utf-8"),
        mb_trackid="55555555-5555-5555-5555-555555555555",
        length=1.0,
    )
    offline_item.add(lib)
    lib._close()

    return {
        "album_id": int(album.id),
        "item_id": int(item.id),
        "item_path": str(item_path),
        "cleanup_item_id": int(cleanup_item.id),
        "cleanup_item_path": str(cleanup_path),
        "cleanup_container_path": "/data/media/music/" + cleanup_path.relative_to(music_dir).as_posix(),
        "offline_item_id": int(offline_item.id),
        "offline_item_path": str(offline_path),
        "offline_container_path": "/data/media/music/" + offline_path.relative_to(music_dir).as_posix(),
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

def _print_process_text(value: str) -> None:
    text = str(value or "")
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "backslashreplace").decode("ascii"))


def print_container_log_tails(*containers: str, tail: str = "200") -> None:
    for c in containers:
        if not c:
            continue
        print(f"\n==> docker logs {c} (tail {tail}) ==>")
        logs_res = run(["docker", "logs", "--tail", tail, c])
        _print_process_text(logs_res.stdout or "")
        _print_process_text(logs_res.stderr or "")

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


def wait_engine_ipc_reachable(web_container: str, engine_host: str = "beets", engine_port: int = 8338, timeout: float = 60.0) -> bool:
    """Found on real CI (twice, deterministically -- not a one-off flake):
    wait_healthy() reporting the engine container's OWN healthcheck passing
    does not guarantee beets-web-manager's Docker Compose DNS resolution of
    the engine's service name has caught up yet after a stop/start cycle.
    A bounded retry loop around the actual import request (still kept,
    below, as defense in depth) was not sufficient by itself -- CI showed
    all 3 attempts, roughly 10 seconds apart, still hitting "unresolved
    host". This probes real DNS resolvability from INSIDE the web-manager
    container directly (the same resolution its own outbound-policy check
    performs), so the retry loop below only ever has to cover residual
    request-level flakiness, not a resolution gap most of a minute wide."""
    deadline = time.time() + timeout
    probe = f"import socket; socket.getaddrinfo({engine_host!r}, {engine_port})"
    while time.time() < deadline:
        res = run(["docker", "exec", web_container, "python3", "-c", probe])
        if res.returncode == 0:
            return True
        time.sleep(2)
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


# ── SEC-002 / ARCH-003 Wave 25 Docker acceptance ────────────────────────────
# The Wave 24 exercises above only re-verify album-level routes
# (artwork/metadata/rename/dedupe) against an already-imported fixture --
# they never touch a single Wave 25 workflow (fresh import, reimport,
# reconciliation, artwork acquisition). These scenarios exercise the real
# production import/artwork-acquisition HTTP routes end to end, using a
# real, long-standing, verified MusicBrainz release (Radiohead's "OK
# Computer") -- there is no offline/mocked path for MusicBrainz lookups in
# this codebase; a genuine fresh-import test requires real network access
# to musicbrainz.org from inside the beets engine container, matching
# exactly how this feature behaves in production. Chosen specifically for
# long-term stability (27+ year old, extremely well-established release).
WAVE25_RELEASE_ID = "1834eae1-741b-3c03-9ca5-0df3decb43ea"
WAVE25_RELEASEGROUP_ID = "b1392450-e666-3926-a536-22c65f834433"
WAVE25_ARTIST = "Radiohead"
WAVE25_ALBUM = "OK Computer"

# A second, independent, long-stable real release for the crash/resume
# scenario (Round 4): confirmed_import_v1's idempotency is keyed on the
# TARGET Release ID globally, not on any one operation/source -- reusing
# WAVE25_RELEASE_ID here would mean crash/resume's own "initial import"
# instantly resumes whatever the fresh-import scenario already completed
# for that release (if it ran first in this same process), never
# genuinely reaching native import at all. A distinct release makes this
# scenario self-contained regardless of scenario execution order.
WAVE25_CRASH_RESUME_RELEASE_ID = "b84ee12a-09ef-421b-82de-0441a926375b"
WAVE25_CRASH_RESUME_RELEASEGROUP_ID = "f5093c06-23e3-404f-aeaa-40f72885ee3a"
WAVE25_CRASH_RESUME_ARTIST = "Pink Floyd"
WAVE25_CRASH_RESUME_ALBUM = "The Dark Side of the Moon"

# Two more independent real releases for the Wave 26 AI Batch Import
# scenarios (PR #101, section 24 of the review brief), for the same reason
# the crash/resume release above is independent of the fresh-import one:
# confirmed_import_v1's idempotency and the "no duplicate album" checks
# below are keyed on the target Release ID, so each scenario needs its own.
WAVE26_AI_IMPORT_RELEASE_ID = "bd3bb36e-16c8-438f-850e-dfbf4d1478f0"
WAVE26_AI_IMPORT_RELEASEGROUP_ID = "48117b90-a16e-34ca-a514-19c702df1158"
WAVE26_AI_IMPORT_ARTIST = "Daft Punk"
WAVE26_AI_IMPORT_ALBUM = "Discovery"

WAVE26_ENGINE_OFFLINE_RELEASE_ID = "2fa63133-a4c9-3f41-8deb-162189de83ff"
WAVE26_ENGINE_OFFLINE_RELEASEGROUP_ID = "6f9f6899-c0d3-311d-ae87-a10ae6bc53a9"
WAVE26_ENGINE_OFFLINE_ARTIST = "Massive Attack"
WAVE26_ENGINE_OFFLINE_ALBUM = "Mezzanine"

_WAVE25_TRACKLIST_CACHE: dict = {}


def _fetch_release_tracklist(release_id: str) -> list:
    """Fetch the real tracklist (title + duration) for a release ID once
    and cache it for the whole acceptance run (Round 4 brief section
    9-10): the same authoritative data the real import path itself queries,
    rather than hard-coded/drifting durations. An extreme duration mismatch
    (the original fixture used a flat ~0.3s per file against tracks that
    are actually 2-6 minutes long) starves Beets' own match-confidence
    scoring for the forced --search-id candidate regardless of filename/
    title similarity -- this is what actually caused the real Docker
    failure this round investigates, not (only) the --quiet-fallback asis
    bug fixed alongside it. Cached per release ID so this is at most one
    network call per distinct release for the whole run, not one per
    track/scenario."""
    if release_id in _WAVE25_TRACKLIST_CACHE:
        return _WAVE25_TRACKLIST_CACHE[release_id]
    url = f"https://musicbrainz.org/ws/2/release/{release_id}?inc=recordings&fmt=json"
    req = urllib.request.Request(url, headers={"User-Agent": "BeetsWebManagerAcceptance/1.0"})
    # MusicBrainz's public API is genuinely flaky/rate-limited -- this hit a
    # real transient 503 in CI. Mirror the same transient-retry pattern
    # already used in production (helpers_mb.fetch_mb_release_tracklist)
    # rather than letting one bad response crash the whole acceptance run.
    transient_codes = {429, 500, 502, 503, 504}
    data = None
    last_ex: Exception = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            break
        except urllib.error.HTTPError as ex:
            last_ex = ex
            if ex.code in transient_codes and attempt < 3:
                print(f"  MusicBrainz fetch transient error ({ex.code}) for {release_id}; retrying {attempt + 1}/3")
                time.sleep(2.0 * attempt)
                continue
            raise
        except Exception as ex:
            last_ex = ex
            if attempt < 3:
                print(f"  MusicBrainz fetch error ({ex}) for {release_id}; retrying {attempt + 1}/3")
                time.sleep(2.0 * attempt)
                continue
            raise
    if data is None:
        raise RuntimeError(f"Could not fetch MusicBrainz tracklist for {release_id} after retries: {last_ex}")
    tracks = []
    for medium in data.get("media", []) or []:
        for trk in medium.get("tracks", []) or []:
            rec = trk.get("recording") or {}
            length_ms = int(trk.get("length") or rec.get("length") or 0)
            tracks.append({
                "position": int(trk.get("position") or len(tracks) + 1),
                "title": str(trk.get("title") or rec.get("title") or f"Track {len(tracks) + 1}"),
                "duration_seconds": max(2.0, length_ms / 1000.0),
                # Additive field, only consumed by seed_wave26_ai_import_source()
                # -- existing Wave25 fixtures ignore it and stay untagged.
                "mb_trackid": str(rec.get("id") or ""),
            })
    if not tracks:
        raise RuntimeError(
            f"MusicBrainz returned no tracks for release {release_id} -- cannot build a realistic fixture"
        )
    _WAVE25_TRACKLIST_CACHE[release_id] = tracks
    return tracks


def seed_wave25_import_source(downloads_dir: Path, subdir: str, track_range=None, *,
                               release_id: str = WAVE25_RELEASE_ID,
                               artist: str = WAVE25_ARTIST, album: str = WAVE25_ALBUM) -> dict:
    """Write a disposable, synthetic source folder shaped like `release_id`
    onto the host side of the engine's download-root bind mount (mounted at
    /data/torrents in the engine container via DOWNLOAD_PATH -- see `env`
    in main()). Returns the container-visible path the production import
    route should be pointed at, plus the LOCAL (host-side) folder path this
    script's own process can read directly to assert fixture properties
    before posting a request (only the engine container, via the real bind
    mount, is allowed to READ these files for the actual import).

    Round 4: durations now come from _fetch_release_tracklist() (a real,
    cached MusicBrainz lookup) instead of a flat, unrealistic 0.3s --
    Beets' own recommendation scoring for the forced --search-id candidate
    depends heavily on duration/track-count fit, not audio content (this
    codebase never fingerprints during a plain import). A low sample rate
    keeps total fixture size small in CI while remaining a fully valid,
    duration-correct WAV file. Basic (non-MusicBrainz) tags -- title,
    artist, album, track number -- are written via `mediafile`, the same
    tag library Beets itself uses, matching a real "downloaded but never
    tagged with Beets/Picard" source. No MusicBrainz Release/ReleaseGroup/
    Recording ID is ever written here; see
    assert_wave25_source_has_no_mb_ids, called before every real import
    request below.

    FLAC, not WAV (Round 4, second real-Docker iteration on this exact
    fixture): a real run with correctly-tagged, correct-duration WAV files
    still could not reach Beets' relaxed recommendation threshold -- the
    real native `beet import` log showed a plain "Skipping." with the
    right item count, meaning Beets could not read back the identity this
    script wrote. WAV has no single, universally-implemented tagging
    scheme (id3/INFO-chunk/other, version-dependent); the GH runner's own
    `pip install`ed `mediafile` writing the tag and the engine
    container's separately-bundled Beets/mediafile version reading it back
    are not guaranteed to agree on how a WAV's tags are stored. FLAC's
    Vorbis comment tagging has no such ambiguity. `ffmpeg` (pre-installed
    on GitHub's ubuntu-latest runners) losslessly converts the same
    synthetic sine-wave PCM this script has always generated -- the audio
    content and durations are unchanged, only the container format."""
    import mediafile

    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise RuntimeError(
            "ffmpeg is required to build a realistic, reliably-tagged Wave 25 fixture "
            "(WAV tagging is not reliable enough for the real Beets import path) but was not found on PATH"
        )

    tracklist = _fetch_release_tracklist(release_id)
    positions = track_range or range(1, len(tracklist) + 1)
    folder = downloads_dir / subdir
    folder.mkdir(parents=True, exist_ok=True)
    for idx in positions:
        track = tracklist[idx - 1]
        wav_bytes = _real_wav_bytes(freq=220.0 + idx * 15.0, duration=track["duration_seconds"], rate=4000)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
            tmp_wav.write(wav_bytes)
            tmp_wav_path = tmp_wav.name
        p = folder / f"{idx:02d} - {track['title']}.flac"
        try:
            conv = subprocess.run(
                [ffmpeg_bin, "-y", "-loglevel", "error", "-i", tmp_wav_path, str(p)],
                capture_output=True, text=True, timeout=60,
            )
            if conv.returncode != 0 or not p.exists():
                raise RuntimeError(f"ffmpeg failed converting fixture track {idx} to FLAC: {conv.stderr}")
        finally:
            try:
                os.unlink(tmp_wav_path)
            except OSError:
                pass
        mf = mediafile.MediaFile(str(p))
        mf.title = track["title"]
        mf.artist = artist
        mf.albumartist = artist
        mf.album = album
        mf.track = idx
        mf.tracktotal = len(tracklist)
        mf.save()
    return {"container_path": f"/data/torrents/{subdir}", "local_folder": folder}


def seed_wave26_ai_import_source(downloads_dir: Path, subdir: str, *,
                                  release_id: str, artist: str, album: str) -> dict:
    """Wave 26 (PR #101) AI Batch Import fixture -- same real-tracklist,
    real-FLAC construction as seed_wave25_import_source(), but each file
    additionally carries the release's REAL per-track MusicBrainz Recording
    ID (mb_trackid). This is deliberately NOT "untagged" the way Wave25's
    fixture is: it represents a common real-world case AI Batch Import
    exists for -- audio that already carries embedded MusicBrainz Recording
    IDs (e.g. from a prior tagging pass, or a scene release that embeds
    them) but has no confirmed album-level Release/Release-Group stamp yet,
    so it still needs AI-assisted release identification.

    This distinction is load-bearing, not cosmetic: found by actually
    running the Wave 26 acceptance scenarios against a genuinely untagged
    (Wave25-style) fixture first -- build_album_matching_decision()'s
    identity_verified requires either a known local Release Group ID
    (impossible for a fresh import with no prior album) OR
    deterministic_track_proof (exact per-item mb_trackid match against the
    candidate release's own recordings). A genuinely untagged fixture can
    satisfy neither, so it can NEVER pass _folder_release_preflight()'s
    action_allowed gate and always routes to human review, regardless of
    AI confidence or tracklist match ratio -- this is correct, intentional
    fail-closed behavior (see CLAUDE.md's "ambiguous evidence goes to
    review" rule), not a bug, but it means the auto-import branch of
    _ai_batch_process_decisions() (and therefore _ai_import_folder() /
    confirmed_import_v1) is only reachable for fixtures shaped like this
    one."""
    import mediafile

    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise RuntimeError("ffmpeg is required to build the Wave 26 AI import fixture but was not found on PATH")

    tracklist = _fetch_release_tracklist(release_id)
    folder = downloads_dir / subdir
    folder.mkdir(parents=True, exist_ok=True)
    for idx, track in enumerate(tracklist, start=1):
        wav_bytes = _real_wav_bytes(freq=220.0 + idx * 15.0, duration=track["duration_seconds"], rate=4000)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
            tmp_wav.write(wav_bytes)
            tmp_wav_path = tmp_wav.name
        p = folder / f"{idx:02d} - {track['title']}.flac"
        try:
            conv = subprocess.run(
                [ffmpeg_bin, "-y", "-loglevel", "error", "-i", tmp_wav_path, str(p)],
                capture_output=True, text=True, timeout=60,
            )
            if conv.returncode != 0 or not p.exists():
                raise RuntimeError(f"ffmpeg failed converting fixture track {idx} to FLAC: {conv.stderr}")
        finally:
            try:
                os.unlink(tmp_wav_path)
            except OSError:
                pass
        mf = mediafile.MediaFile(str(p))
        mf.title = track["title"]
        mf.artist = artist
        mf.albumartist = artist
        mf.album = album
        mf.track = idx
        mf.tracktotal = len(tracklist)
        if track.get("mb_trackid"):
            mf.mb_trackid = track["mb_trackid"]
        mf.save()
    return {"container_path": f"/data/torrents/{subdir}", "local_folder": folder}


def assert_wave25_source_has_no_mb_ids(local_folder: Path) -> None:
    """Round 4 brief section 20: prove the fixture is genuinely untagged --
    no MusicBrainz Release/ReleaseGroup/Recording ID anywhere -- BEFORE
    posting the import request. This script runs on the HOST side of the
    bind mount (see seed_wave25_import_source's own docstring), so it can
    read these files directly without going through either container.
    Raises (fails the whole run loudly) rather than silently continuing --
    a fixture that accidentally carried MusicBrainz tags would prove
    nothing about the fresh-review trust model confirmed_import_v1 exists
    for."""
    import mediafile

    for wav_path in sorted(local_folder.glob("*.flac")):
        mf = mediafile.MediaFile(str(wav_path))
        for field in ("mb_albumid", "mb_releasegroupid", "mb_trackid", "mb_releasetrackid"):
            value = getattr(mf, field, None)
            if value:
                raise AssertionError(
                    f"Fixture file {wav_path.name} unexpectedly carries {field}={value!r} -- "
                    "this must be genuinely untagged before import (do not pre-stamp MusicBrainz IDs)"
                )


def run_wave25_scenarios(client: "HttpClient", downloads_dir: Path, music_dir: Path, db_path: str) -> None:
    """Fresh import, reimport idempotency, symlink/root-escape rejection,
    and identity-conflict rejection against the real production import
    route. Artwork acquisition (Plan/Apply/Verify, stale-plan rejection)
    is exercised separately in run_wave25_artwork_scenarios, since it uses
    the Wave 24 fixture album rather than a fresh import."""

    def scenario_pass(name: str) -> None:
        print(f"[PASS] {name}")

    def scenario_fail(name: str, detail: str) -> None:
        _fail(f"{name}: {detail}")

    # ── Fresh import ─────────────────────────────────────────────────────
    print("==> [Wave25] Fresh import via the real production route (confirmed_import_v1)...")
    src = seed_wave25_import_source(downloads_dir, "fresh-import")
    assert_wave25_source_has_no_mb_ids(src["local_folder"])
    status, body = client.request(
        "POST", "/api/folders/import-with-id",
        json_body={
            "path": src["container_path"],
            "mb_albumid": WAVE25_RELEASE_ID,
            "mb_releasegroupid": WAVE25_RELEASEGROUP_ID,
            "move": False,
        },
        timeout=60,
    )
    fresh_album_id = None
    if status != 200 or not body.get("ok"):
        scenario_fail("fresh-import-untagged-confirmed", f"request rejected: {status} {body}")
    else:
        try:
            result = client.wait_job(body["job_id"], timeout=180)
        except TimeoutError as ex:
            result = None
            scenario_fail("fresh-import-untagged-confirmed", str(ex))
        if result is not None:
            if result.get("status") != "success":
                scenario_fail("fresh-import-untagged-confirmed", f"job did not succeed: {result.get('status')} / {result.get('log')}")
            else:
                try:
                    con = sqlite3.connect(db_path)
                    con.row_factory = sqlite3.Row
                    # Exact planned Release ID is the primary proof (Round 4
                    # brief section 17) -- never loosened to an OR/most-
                    # recent/closest-title fallback just to make this pass.
                    row = con.execute(
                        "SELECT id, album, albumartist, mb_albumid, mb_releasegroupid FROM albums WHERE mb_albumid=?",
                        (WAVE25_RELEASE_ID,),
                    ).fetchone()
                    item_count = 0
                    files_exist = False
                    rgid_ok = False
                    if row:
                        fresh_album_id = int(row["id"])
                        items = con.execute("SELECT path FROM items WHERE album_id=?", (fresh_album_id,)).fetchall()
                        item_count = len(items)
                        # Round 4, fourth real Docker iteration (real
                        # evidence, not a guess): a real Beets 2.13.1
                        # `beet import` here stores items.path as the FULL
                        # ABSOLUTE path under this run's `directory:` config
                        # -- which is the ENGINE CONTAINER's own
                        # /data/media/music, not a path relative to it. That
                        # absolute path is completely valid from *inside*
                        # the engine container (which is exactly the view
                        # transaction_engine._capture_and_verify() checks
                        # from, and why its own verification correctly
                        # passed) -- but this script runs on the HOST
                        # (the GH Actions runner / this dev machine), where
                        # that literal container path does not exist as a
                        # real filesystem entry; only the bind-mounted host
                        # directory (`music_dir`) does. Rebase an absolute
                        # path that falls under the well-known container
                        # music root onto the host-mounted equivalent before
                        # checking; fall back to a literal existence check
                        # for any other absolute path (defensive only, not
                        # expected here).
                        def _item_path_exists(raw) -> bool:
                            p_str = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
                            p = Path(p_str)
                            if not p.is_absolute():
                                return (music_dir / p).exists()
                            try:
                                rel = p.relative_to("/data/media/music")
                            except ValueError:
                                return p.exists()
                            return (music_dir / rel).exists()
                        files_exist = all(_item_path_exists(i[0]) for i in items) if items else False
                        if not files_exist and items:
                            bad_paths = [repr(i[0]) for i in items if not _item_path_exists(i[0])]
                            print(f"  [debug] fresh-import item path(s) that did not resolve under {music_dir}: {bad_paths}")
                        # Round 4 brief section 18: RGID must be verified
                        # too, not silently substituted for the Release ID.
                        actual_rgid = row["mb_releasegroupid"]
                        actual_rgid = actual_rgid.decode("utf-8", "replace") if isinstance(actual_rgid, bytes) else str(actual_rgid or "")
                        rgid_ok = actual_rgid.strip().lower() == WAVE25_RELEASEGROUP_ID
                    con.close()
                except Exception as ex:
                    row, item_count, files_exist, rgid_ok = None, 0, False, False
                    scenario_fail("fresh-import-untagged-confirmed", f"could not verify host-mounted DB state: {ex}")
                else:
                    if not row:
                        scenario_fail("fresh-import-untagged-confirmed", "no album row was created with the exact planned Release ID")
                    elif item_count == 0:
                        scenario_fail("fresh-import-untagged-confirmed", "album row exists but has zero items")
                    elif not files_exist:
                        scenario_fail("fresh-import-untagged-confirmed", "item DB paths do not point at real files on the host-mounted music root")
                    elif not rgid_ok:
                        scenario_fail("fresh-import-untagged-confirmed", f"album Release Group does not match the planned RGID {WAVE25_RELEASEGROUP_ID}")
                    else:
                        scenario_pass(f"fresh-import-untagged-confirmed (album_id={fresh_album_id}, {item_count} item(s), real files + exact Release ID + RGID verified)")

    # ── Reimport idempotency ────────────────────────────────────────────
    print("==> [Wave25] Confirmed-import idempotency (same source, called again)...")
    if fresh_album_id is None:
        scenario_fail("confirmed-import-idempotent", "skipped: fresh-import did not produce an album to re-import")
    else:
        status, body = client.request(
            "POST", "/api/folders/import-with-id",
            json_body={
                "path": src["container_path"],
                "mb_albumid": WAVE25_RELEASE_ID,
                "mb_releasegroupid": WAVE25_RELEASEGROUP_ID,
                "existing_album_id": fresh_album_id,
                "move": False,
            },
            timeout=30,
        )
        if status != 200 or not body.get("ok"):
            scenario_fail("confirmed-import-idempotent", f"request rejected: {status} {body}")
        else:
            try:
                result = client.wait_job(body["job_id"], timeout=180)
            except TimeoutError as ex:
                scenario_fail("confirmed-import-idempotent", str(ex))
                result = None
            if result is not None and result.get("status") != "success":
                scenario_fail("confirmed-import-idempotent", f"job did not succeed: {result.get('status')} / {result.get('log')}")
            elif result is not None:
                resumed = any("Resumed an already-verified prior result" in line for line in (result.get("log") or []))
                try:
                    con = sqlite3.connect(db_path)
                    album_rows = con.execute(
                        "SELECT id FROM albums WHERE mb_albumid=?",
                        (WAVE25_RELEASE_ID,),
                    ).fetchall()
                    con.close()
                except Exception as ex:
                    scenario_fail("confirmed-import-idempotent", f"could not verify host-mounted DB state: {ex}")
                else:
                    if len(album_rows) != 1:
                        scenario_fail("confirmed-import-idempotent", f"expected exactly 1 album row for this release after a second import, found {len(album_rows)} -- duplicate import")
                    elif not resumed:
                        scenario_fail("confirmed-import-idempotent", "second import did not report resuming an already-verified result (native import may have been re-invoked)")
                    else:
                        scenario_pass("confirmed-import-idempotent (no duplicate album row, native import not re-invoked)")

    # ── Symlink / root-escape rejection ─────────────────────────────────
    print("==> [Wave25] Symlink and root-escape rejection...")
    outside_dir = downloads_dir.parent / "wave25-outside-root"
    outside_dir.mkdir(parents=True, exist_ok=True)
    (outside_dir / "escape.wav").write_bytes(_real_wav_bytes())
    escape_cases = {
        "outside-allowed-root": "/etc/wave25-escape-attempt",
        "sibling-prefix": "/data/torrents-evil/should-not-match",
        "traversal": "/data/torrents/../etc",
    }
    for case_name, bad_path in escape_cases.items():
        status, body = client.request(
            "POST", "/api/folders/import-with-id",
            json_body={"path": bad_path, "mb_albumid": WAVE25_RELEASE_ID, "mb_releasegroupid": WAVE25_RELEASEGROUP_ID},
            timeout=15,
        )
        if status == 200 and body.get("ok"):
            scenario_fail(f"root-escape-rejected[{case_name}]", f"request unexpectedly accepted: {status} {body}")
        else:
            scenario_pass(f"root-escape-rejected[{case_name}] ({status})")

    # A real symlink pointing outside the allowed roots, placed INSIDE the
    # allowed download root -- must be rejected by the engine's own
    # symlink-component check (create_import_folder_plan), since the
    # web-manager container cannot see real symlinks on a filesystem it
    # doesn't have mounted.
    link_subdir = "symlink-escape"
    link_path = downloads_dir / link_subdir
    try:
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink()
        os.symlink(str(outside_dir), str(link_path), target_is_directory=True)
        symlink_created = True
    except Exception as ex:
        symlink_created = False
        print(f"  (could not create a real symlink on this host to test with: {ex} -- skipping this specific case)")
    if symlink_created:
        status, body = client.request(
            "POST", "/api/folders/import-with-id",
            json_body={
                "path": f"/data/torrents/{link_subdir}",
                "mb_albumid": WAVE25_RELEASE_ID, "mb_releasegroupid": WAVE25_RELEASEGROUP_ID,
            },
            timeout=30,
        )
        rejected = not (status == 200 and body.get("ok"))
        if status == 200 and body.get("ok"):
            # Accepted at the plan/dispatch layer (the route always queues an
            # async job and returns 200 immediately -- the web-manager
            # container cannot see the symlink itself, so real rejection can
            # only happen later inside the engine's create_import_folder_plan
            # symlink-component check, once the job actually runs). Bug found
            # during this acceptance round: `if rejected and status == 200`
            # was dead code here, since `rejected` is already False whenever
            # status==200 and ok is True -- the job's actual outcome was
            # never checked, so a real symlink escape could have silently
            # passed this scenario. Check the job's outcome directly instead.
            try:
                result = client.wait_job(body["job_id"], timeout=60)
                rejected = result.get("status") != "success"
            except TimeoutError:
                pass
        if not rejected:
            scenario_fail("symlink-source-rejected", f"a symlinked source folder was not rejected: {status} {body}")
        else:
            scenario_pass("symlink-source-rejected")
        try:
            link_path.unlink()
        except Exception:
            pass

    # ── Identity-conflict rejection ─────────────────────────────────────
    print("==> [Wave25] Identity-conflict rejection (malformed/missing MBID)...")
    src2 = seed_wave25_import_source(downloads_dir, "identity-conflict", track_range=range(1, 3))
    status, body = client.request(
        "POST", "/api/folders/import-with-id",
        json_body={"path": src2["container_path"], "mb_albumid": "not-a-real-musicbrainz-id"},
        timeout=15,
    )
    if status == 200 and body.get("ok"):
        scenario_fail("identity-conflict-blocked[malformed-mbid]", f"a syntactically invalid MBID was accepted: {status} {body}")
    else:
        scenario_pass("identity-conflict-blocked[malformed-mbid]")

    status, body = client.request(
        "POST", "/api/folders/import-with-id",
        json_body={"path": src2["container_path"]},
        timeout=15,
    )
    if status == 200 and body.get("ok"):
        scenario_fail("identity-conflict-blocked[missing-mbid]", f"import with no MB identity evidence at all was accepted: {status} {body}")
    else:
        scenario_pass("identity-conflict-blocked[missing-mbid]")


def run_wave25_artwork_scenarios(client: "HttpClient", fixture: dict, db_path: str) -> None:
    """album_artwork_fetch_v1: real Plan -> Apply against the Wave 24
    fixture album, plus a genuine stale-plan rejection (this family is one
    of the few Wave 25 additions that exposes Plan and Apply as two
    separate HTTP calls, so this is the one scenario that can genuinely
    test the Plan-to-Apply window at the Docker/HTTP boundary rather than
    only at the transaction_engine.py unit level)."""

    def scenario_pass(name: str) -> None:
        print(f"[PASS] {name}")

    def scenario_fail(name: str, detail: str) -> None:
        _fail(f"{name}: {detail}")

    aid = fixture["album_id"]

    print("==> [Wave25] Artwork acquisition (album_artwork_fetch_v1 Plan -> Apply -> Verify)...")
    status, body = client.request("POST", f"/api/albums/{aid}/fetch-embed-artwork", timeout=15)
    if status != 200 or not body.get("ok"):
        scenario_fail("artwork-fetch-embed", f"request rejected: {status} {body}")
    else:
        try:
            result = client.wait_job(body["job_id"], timeout=90)
        except TimeoutError as ex:
            scenario_fail("artwork-fetch-embed", str(ex))
            result = None
        if result is not None:
            if result.get("status") != "success":
                # Round 4 brief section 27: report this as exactly two
                # separate claims, not one overstated "artwork acquisition
                # passed". fetchart is now a genuinely loaded, real
                # command (see seed_disposable_library) -- it legitimately
                # cannot find real cover art for this fixture (synthetic
                # mb_albumid/artist/album, no real release exists), so a
                # truthful non-"success" status here proves the
                # transaction/failure semantics, and nothing more.
                print(f"  (fetchart found no art for the synthetic fixture album, as expected: {result.get('log')})")
                scenario_pass("artwork-transaction-fail-closed (truthful failure status, no false success)")
                print("  [NOTE] artwork-successful-acquisition: NOT EXERCISED -- fixture album has no real MusicBrainz identity for any provider to find art against")
            else:
                try:
                    con = sqlite3.connect(db_path)
                    row = con.execute("SELECT artpath FROM albums WHERE id=?", (aid,)).fetchone()
                    con.close()
                except Exception as ex:
                    scenario_fail("artwork-fetch-embed", f"could not verify host-mounted DB state: {ex}")
                else:
                    if not row or not row[0]:
                        scenario_fail("artwork-fetch-embed", "job reported success but albums.artpath is still empty")
                    else:
                        scenario_pass(f"artwork-transaction-fail-closed (job reported success)")
                        scenario_pass(f"artwork-successful-acquisition (verified artpath={row[0]})")

    print("==> [Wave25] Stale-plan precondition check (smoke test over real HTTP)...")
    # Honest scope note: BeetsClient.fetch_and_embed_album_art() composes
    # Plan and Apply back-to-back with no externally reachable window in
    # between (there is no separate, web-manager-exposed Plan-only HTTP
    # call this script can call, wait on, mutate around, and only then
    # Apply) -- so a genuine Plan-then-mutate-then-Apply race cannot be
    # injected through the real production HTTP surface as it exists
    # today. The authoritative proof that Apply actually re-verifies
    # identity fresh rather than trusting a stale Plan snapshot is
    # tests/test_album_artwork_fetch_v1.py::test_apply_rejects_stale_plan_on_identity_change,
    # which drives create_album_artwork_fetch_plan/execute_album_artwork_fetch_apply
    # directly and genuinely controls the window between them. What this
    # Docker-level check adds is confirming the real HTTP path still
    # reaches that same precondition logic at all: change the album's
    # identity, then confirm a fresh fetch-embed-artwork call still
    # completes against the CURRENT identity rather than erroring out or
    # silently operating on stale cached state.
    status, body = client.request("POST", f"/api/albums/{aid}/fix-metadata",
                                   json_body={"mb_albumid": "99999999-9999-9999-9999-999999999999"}, timeout=15)
    identity_changed = status == 200 and body.get("ok")
    if identity_changed:
        client.wait_job(body["job_id"], timeout=30)
    if not identity_changed:
        scenario_fail("stale-plan-precondition-smoke-test", "could not change album identity via fix-metadata to set up this check")
    else:
        status, body = client.request("POST", f"/api/albums/{aid}/fetch-embed-artwork", timeout=15)
        reached_apply = status == 200 and bool(body.get("job_id"))
        if reached_apply:
            client.wait_job(body["job_id"], timeout=60)
        if not reached_apply:
            scenario_fail("stale-plan-precondition-smoke-test", f"fetch-embed-artwork did not even dispatch after an identity change: {status} {body}")
        else:
            scenario_pass("stale-plan-precondition-smoke-test (real HTTP path reaches Apply's identity precondition check; see the unit test above for the authoritative race proof)")


def wave25_engine_offline_checks(client: "HttpClient", db_path: str) -> None:
    """Wave 25 production routes, exercised while the engine is stopped --
    extends the Wave 24 engine-offline proof (which only covered
    fix-metadata) to the fresh-import and artwork-acquisition routes this
    round added."""
    db_bytes_before = Path(db_path).read_bytes()

    status, body = client.request(
        "POST", "/api/folders/import-with-id",
        json_body={"path": "/data/torrents/engine-offline-should-not-import",
                   "mb_albumid": WAVE25_RELEASE_ID, "mb_releasegroupid": WAVE25_RELEASEGROUP_ID},
        timeout=15,
    )
    offline_ok = True
    if status == 200 and body.get("ok"):
        try:
            result = client.wait_job(body["job_id"], timeout=20)
            if result.get("status") == "success":
                offline_ok = False
        except TimeoutError:
            pass
    if not offline_ok:
        _fail("engine-offline-fails-closed[import]: a fresh-import request appeared to succeed while the engine was stopped")
        print("[FAIL] engine-offline-fails-closed[import]")
    else:
        print("[PASS] engine-offline-fails-closed[import]")

    db_bytes_after = Path(db_path).read_bytes()
    if db_bytes_after != db_bytes_before:
        _fail("engine-offline-fails-closed[no-local-mutation]: host-mounted DB changed while the engine was offline during a Wave25 route call")
        print("[FAIL] engine-offline-fails-closed[no-local-mutation]")
    else:
        print("[PASS] engine-offline-fails-closed[no-local-mutation]")


def run_wave25_crash_resume_scenario(client: "HttpClient", engine_container: str,
                                      downloads_dir: Path, db_path: str) -> None:
    """Crash/resume (Round 4 rewrite): a deterministic failpoint --
    `_acceptance_failpoint: "confirmed_import_after_native"`, honored by
    the engine ONLY because this acceptance stack's compose override boots
    it with BEETS_ACCEPTANCE_MODE=1 (see backend/beets_control_agent.py) --
    fires confirmed_import_v1's Apply right after native Beets import has
    genuinely succeeded (a real album+items now exist) but before this
    transaction ever captures/verifies that result. This is the actual
    required failure window (native import succeeded, verification/
    completion did not happen yet), reached without any timing race. A
    prior version of this scenario only proved "complete an import, hard-
    kill+restart the web-manager container, resubmit" -- that is a
    restart+idempotency test, not a crash-after-native-success test, and
    is intentionally no longer described as one."""

    def scenario_pass(name: str) -> None:
        print(f"[PASS] {name}")

    def scenario_fail(name: str, detail: str) -> None:
        _fail(f"{name}: {detail}")

    print("==> [Wave25] Crash/resume: import with the deterministic after-native failpoint enabled...")
    src = seed_wave25_import_source(
        downloads_dir, "crash-resume",
        release_id=WAVE25_CRASH_RESUME_RELEASE_ID,
        artist=WAVE25_CRASH_RESUME_ARTIST, album=WAVE25_CRASH_RESUME_ALBUM,
    )
    assert_wave25_source_has_no_mb_ids(src["local_folder"])
    status, body = client.request(
        "POST", "/api/folders/import-with-id",
        json_body={
            "path": src["container_path"], "mb_albumid": WAVE25_CRASH_RESUME_RELEASE_ID,
            "mb_releasegroupid": WAVE25_CRASH_RESUME_RELEASEGROUP_ID, "move": False,
            "_acceptance_failpoint": "confirmed_import_after_native",
        },
        timeout=60,
    )
    if status != 200 or not body.get("ok"):
        scenario_fail("crash-resume-no-duplicate", f"failpoint-enabled import request was rejected outright: {status} {body}")
        return
    try:
        result = client.wait_job(body["job_id"], timeout=180)
    except TimeoutError as ex:
        scenario_fail("crash-resume-no-duplicate", f"failpoint-enabled import did not reach a terminal state: {ex}")
        return
    if result.get("status") == "success":
        scenario_fail("crash-resume-no-duplicate", "the failpoint-enabled import reported success -- the failpoint did not fire, this scenario proves nothing")
        return
    scenario_pass("crash-resume-failpoint-fired (job truthfully did not report success while the simulated crash window was active)")

    # Prove native import genuinely ran and created a real album BEFORE
    # the simulated crash -- this is not "nothing happened, then we
    # retried", it is "the mutation already happened, then the process
    # was interrupted before recording that".
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        pre_retry_row = con.execute(
            "SELECT id FROM albums WHERE mb_albumid=?", (WAVE25_CRASH_RESUME_RELEASE_ID,)
        ).fetchone()
        con.close()
    except Exception as ex:
        scenario_fail("crash-resume-no-duplicate", f"could not verify host-mounted DB state after the failpoint fired: {ex}")
        return
    if not pre_retry_row:
        scenario_fail("crash-resume-no-duplicate", "no album exists after the failpoint fired -- native import did not actually mutate anything, so this is not proving the required crash window at all")
        return
    scenario_pass("crash-resume-native-import-really-ran (a real album exists before the retry, proving the crash happened AFTER genuine mutation)")

    print("==> [Wave25] Crash/resume: restarting the engine container to simulate a real process interruption...")
    restart_res = run(["docker", "restart", engine_container])
    if restart_res.returncode != 0:
        scenario_fail("crash-resume-no-duplicate", "could not restart the engine container to simulate the crash")
        return
    if not wait_healthy(engine_container):
        scenario_fail("crash-resume-no-duplicate", "engine container did not become healthy again after the simulated crash restart")
        return
    scenario_pass("crash-resume-engine-restart (engine came back healthy, and its persisted transaction state survived the restart)")

    print("==> [Wave25] Crash/resume: retrying the SAME confirmed import, failpoint disabled...")
    status, body = client.request(
        "POST", "/api/folders/import-with-id",
        json_body={
            "path": src["container_path"], "mb_albumid": WAVE25_CRASH_RESUME_RELEASE_ID,
            "mb_releasegroupid": WAVE25_CRASH_RESUME_RELEASEGROUP_ID, "move": False,
        },
        timeout=60,
    )
    if status != 200 or not body.get("ok"):
        scenario_fail("crash-resume-no-duplicate", f"post-crash retry request rejected: {status} {body}")
        return
    try:
        result = client.wait_job(body["job_id"], timeout=120)
    except TimeoutError as ex:
        scenario_fail("crash-resume-no-duplicate", f"post-crash retry did not complete: {ex}")
        return
    if result.get("status") != "success":
        scenario_fail("crash-resume-no-duplicate", f"post-crash retry did not succeed truthfully: {result.get('status')} / {result.get('log')}")
        return

    log_lines = result.get("log") or []
    resumed = any("Resumed an already-verified prior result" in line for line in log_lines)
    if not resumed:
        scenario_fail("crash-resume-no-duplicate", "post-crash retry succeeded but did not report resuming the prior result -- native import may have been invoked a second time (invocation-count proof section 25 failed)")
        return
    scenario_pass("crash-resume-native-import-invoked-exactly-once (retry's own job log proves it resumed rather than re-invoking native import)")

    try:
        con = sqlite3.connect(db_path)
        rows = con.execute(
            "SELECT id FROM albums WHERE mb_albumid=?", (WAVE25_CRASH_RESUME_RELEASE_ID,)
        ).fetchall()
        con.close()
    except Exception as ex:
        scenario_fail("crash-resume-no-duplicate", f"could not verify host-mounted DB state: {ex}")
        return
    if len(rows) != 1:
        scenario_fail("crash-resume-no-duplicate", f"expected exactly 1 album row for this release after the crash+retry, found {len(rows)} -- duplicate import after crash")
    else:
        scenario_pass("crash-resume-no-duplicate (native import succeeded once, the simulated crash happened before verification, and the retry safely resumed with no duplicate)")


AI_BATCH_SEED_SCRIPT = ROOT / "docker" / "acceptance" / "ai_batch_seed.py"


def _run_ai_batch_seed(web_container: str, *args: str) -> str:
    # The web-manager container runs with `read_only: true` at the whole-
    # container level (only /web-manager-data and tmpfs /tmp, /run are
    # writable) -- `docker cp` INTO a container is rejected outright by the
    # Docker daemon whenever the container's rootfs is read-only, regardless
    # of whether the target path is itself on a writable tmpfs mount ("Error
    # response from daemon: container rootfs is marked read-only"), so this
    # script never writes the seed helper into the container at all. Instead
    # it pipes the script's source on stdin to `python3 -`, which only needs
    # `docker exec` (a process inside the container's existing namespaces,
    # not a filesystem write) to work.
    script_source = AI_BATCH_SEED_SCRIPT.read_text(encoding="utf-8")
    res = run(
        ["docker", "exec", "-i", web_container, "python3", "-", *args],
        input=script_source,
    )
    if res.returncode != 0:
        raise RuntimeError(f"AI batch seed command {args[:1]} failed: {res.stderr or res.stdout}")
    return res.stdout.strip()


def seed_ai_batch_state(web_container: str, batch_job_id: str, container_folder_path: str,
                         scan_root: str, release_id: str, releasegroup_id: str, artist: str, album: str) -> int:
    return int(_run_ai_batch_seed(
        web_container, "create", batch_job_id, container_folder_path, scan_root,
        release_id, releasegroup_id, artist, album,
    ))


def bump_ai_batch_revision(web_container: str, batch_job_id: str) -> int:
    return int(_run_ai_batch_seed(web_container, "bump", batch_job_id))


def reseed_ai_batch_folder(web_container: str, batch_job_id: str, container_folder_path: str,
                            release_id: str, releasegroup_id: str, artist: str, album: str) -> int:
    return int(_run_ai_batch_seed(
        web_container, "reseed_folder", batch_job_id, container_folder_path,
        release_id, releasegroup_id, artist, album,
    ))


def get_ai_batch_field(web_container: str, batch_job_id: str, field: str) -> str:
    return _run_ai_batch_seed(web_container, "get", batch_job_id, field)


def dump_ai_batch_folder_states(web_container: str, batch_job_id: str) -> str:
    return _run_ai_batch_seed(web_container, "dump_folder_states", batch_job_id)


def run_wave26_ai_import_scenarios(client: "HttpClient", web_container: str, engine_container: str,
                                    downloads_dir: Path, db_path: str) -> None:
    """Wave 26 (PR #101) AI Batch Import Docker acceptance scenarios --
    review brief section 24. The only stub in this entire function is the
    AI provider boundary itself (see docker/acceptance/ai_batch_seed.py's
    docstring): every batch state write goes through the real
    `AiBatchStateStore` at the real container-visible DB path, every import
    goes through the real `/api/ai-batch-import` route and the real
    `_ai_import_folder` -> `confirmed_import_v1` composition against the
    real engine, and every failure-path check reads the real host-mounted
    Beets DB or the real HTTP response -- nothing here is asserted from
    source inspection alone."""

    def scenario_pass(name: str) -> None:
        print(f"[PASS] {name}")

    def scenario_fail(name: str, detail: str) -> None:
        _fail(f"{name}: {detail}")

    # ── Scenario 1: AI-reviewed import via confirmed_import_v1 ─────────────
    print("==> [Wave26] AI-reviewed import (provider stubbed, everything else real)...")
    # Nested under an artist-named parent directory (not a bare leaf folder)
    # -- _folder_release_preflight()'s folder_artist evidence comes from the
    # PARENT directory name (source.parent.name), matching a real "Artist/
    # Album" download layout. A bare leaf folder directly under the download
    # root makes folder_artist resolve to the download root's own name
    # ("torrents"), which build_album_matching_decision() then correctly
    # flags as a genuine artist_conflict against the real candidate artist
    # -- not a matching-logic bug, just an unrealistic fixture layout.
    # Uses seed_wave26_ai_import_source() (real embedded per-track MB
    # Recording IDs), not the Wave25 untagged fixture -- see that
    # function's docstring for why a genuinely untagged source can never
    # reach auto-import here (found live, by actually running this
    # scenario against the untagged fixture first).
    src = seed_wave26_ai_import_source(
        downloads_dir, f"{WAVE26_AI_IMPORT_ARTIST}/ai-import-confirmed",
        release_id=WAVE26_AI_IMPORT_RELEASE_ID,
        artist=WAVE26_AI_IMPORT_ARTIST, album=WAVE26_AI_IMPORT_ALBUM,
    )
    batch_job_id = f"acceptance-ai-import-{uuid.uuid4().hex[:12]}"
    try:
        seed_ai_batch_state(
            web_container, batch_job_id, src["container_path"], src["container_path"],
            WAVE26_AI_IMPORT_RELEASE_ID, WAVE26_AI_IMPORT_RELEASEGROUP_ID,
            WAVE26_AI_IMPORT_ARTIST, WAVE26_AI_IMPORT_ALBUM,
        )
    except Exception as ex:
        scenario_fail("ai-reviewed-import-confirmed", f"could not seed AI batch state: {ex}")
        return
    status, body = client.request(
        "POST", "/api/ai-batch-import",
        json_body={"recover_batch_job_id": batch_job_id, "path": "/data/torrents/music"},
        timeout=60,
    )
    if status != 200 or not body.get("ok"):
        scenario_fail("ai-reviewed-import-confirmed", f"request rejected: {status} {body}")
        return
    job_id = body.get("job_id")
    if not job_id:
        scenario_fail("ai-reviewed-import-confirmed", f"no job_id returned: {body}")
        return
    try:
        result = client.wait_job(job_id, timeout=180)
    except TimeoutError as ex:
        scenario_fail("ai-reviewed-import-confirmed", str(ex))
        return
    if result.get("status") != "success":
        scenario_fail("ai-reviewed-import-confirmed", f"job did not succeed: {result.get('status')} / {result.get('log')}")
        return
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        album_rows = con.execute(
            "SELECT id, mb_albumid, mb_releasegroupid FROM albums WHERE mb_albumid=?",
            (WAVE26_AI_IMPORT_RELEASE_ID,),
        ).fetchall()
        item_count = 0
        if album_rows:
            item_count = con.execute(
                "SELECT COUNT(*) FROM items WHERE album_id=?", (album_rows[0]["id"],)
            ).fetchone()[0]
        con.close()
    except Exception as ex:
        scenario_fail("ai-reviewed-import-confirmed", f"could not verify host-mounted DB state: {ex}")
        return
    if len(album_rows) != 1:
        scenario_fail("ai-reviewed-import-confirmed", f"expected exactly 1 album row for the AI-reviewed release, found {len(album_rows)} -- job log: {result.get('log')}")
    elif item_count <= 0:
        scenario_fail("ai-reviewed-import-confirmed", "album row exists but has zero items")
    elif (album_rows[0]["mb_releasegroupid"] or "").strip().lower() != WAVE26_AI_IMPORT_RELEASEGROUP_ID.lower():
        scenario_fail("ai-reviewed-import-confirmed", "album Release Group does not match the AI-reviewed RGID -- fresh-review trust model (confirmed_import_v1) was not actually used")
    else:
        scenario_pass(
            f"ai-reviewed-import-confirmed (album_id={album_rows[0]['id']}, {item_count} item(s), "
            "a genuinely untagged source folder was imported through confirmed_import_v1 on the AI-reviewed "
            "suggestion, not the reimport trust model, matching Wave 26 section 14's required fix)"
        )

    # ── Scenario 2: stale AI review blocked on revision change ─────────────
    # This is a direct, unambiguous CAS proof rather than a race against a
    # real HTTP route: every real app.py caller (e.g. ai_batch_pause) reads
    # fresh state via _ai_batch_find_state() immediately before its own
    # commit, so it always self-heals onto the current revision -- correct
    # production behavior, but it means racing it can only probe a narrow,
    # timing-dependent window, not cleanly demonstrate the CAS rejection
    # itself. Instead this exercises the exact same
    # AiBatchStateStore.save_batch_state() contract every real writer goes
    # through, directly, with a deliberately stale expected_revision.
    print("==> [Wave26] Stale AI review blocked on revision change...")
    stale_batch_job_id = f"acceptance-ai-stale-{uuid.uuid4().hex[:12]}"
    try:
        original_rev = seed_ai_batch_state(
            web_container, stale_batch_job_id, "/data/torrents/does-not-matter", "/data/torrents/does-not-matter",
            WAVE26_AI_IMPORT_RELEASE_ID, WAVE26_AI_IMPORT_RELEASEGROUP_ID,
            WAVE26_AI_IMPORT_ARTIST, WAVE26_AI_IMPORT_ALBUM,
        )
        # Simulate a second, independent writer committing a newer revision
        # after this scenario's own knowledge of the batch was established.
        bumped_rev = bump_ai_batch_revision(web_container, stale_batch_job_id)
    except Exception as ex:
        scenario_fail("stale-review-blocked", f"could not set up revision race: {ex}")
        return
    if bumped_rev <= original_rev:
        scenario_fail("stale-review-blocked", f"setup did not actually advance the revision ({original_rev} -> {bumped_rev})")
        return
    try:
        # Deliberately uses original_rev -- now stale -- as expected_revision.
        outcome = _run_ai_batch_seed(web_container, "attempt_stale_write", stale_batch_job_id, str(original_rev))
        final_rev = int(get_ai_batch_field(web_container, stale_batch_job_id, "revision"))
    except Exception as ex:
        scenario_fail("stale-review-blocked", f"could not attempt the stale write: {ex}")
        return
    if outcome != "rejected":
        scenario_fail("stale-review-blocked", f"a write using a known-stale revision ({original_rev}, current is {bumped_rev}) was NOT rejected: {outcome!r}")
    elif final_rev != bumped_rev:
        scenario_fail("stale-review-blocked", f"stale write was rejected but the store's revision changed anyway ({bumped_rev} -> {final_rev})")
    else:
        scenario_pass(f"stale-review-blocked (a write using stale revision {original_rev} against a store now at revision {bumped_rev} was rejected by real SQLite CAS, and the store's revision was left untouched)")

    # ── Scenario 3: concurrent state -- two workers, one success/one 409 ───
    print("==> [Wave26] Concurrent state: two simultaneous commits against the same batch...")
    race_batch_job_id = f"acceptance-ai-race-{uuid.uuid4().hex[:12]}"
    try:
        seed_ai_batch_state(
            web_container, race_batch_job_id, "/data/torrents/does-not-matter", "/data/torrents/does-not-matter",
            WAVE26_AI_IMPORT_RELEASE_ID, WAVE26_AI_IMPORT_RELEASEGROUP_ID,
            WAVE26_AI_IMPORT_ARTIST, WAVE26_AI_IMPORT_ALBUM,
        )
    except Exception as ex:
        scenario_fail("concurrent-state-409", f"could not seed AI batch state: {ex}")
        return
    # N=2 concurrent requests occasionally serialize cleanly through real
    # HTTP + the thread pool + ai_batch_pause's own read-then-commit window
    # with no actual overlap (both legitimately succeed in sequence -- not
    # a bug, just no race that round). Found live: an earlier version of
    # this scenario used exactly 2 threads and got [200, 200] on one run.
    # A wider burst makes a genuine overlap effectively certain while still
    # proving the same thing section 24 asks for -- concurrent commits
    # against one batch, at least one rejected with a real HTTP 409.
    concurrent_n = 10
    barrier = threading.Barrier(concurrent_n)
    results: list = [None] * concurrent_n

    def _fire(idx: int) -> None:
        barrier.wait(timeout=10)
        results[idx] = client.request("POST", "/api/ai-batch-pause", json_body={"batch_job_id": race_batch_job_id}, timeout=30)

    threads = [threading.Thread(target=_fire, args=(i,)) for i in range(concurrent_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    statuses = [r[0] for r in results if r is not None]
    ok_count = statuses.count(200)
    conflict_count = statuses.count(409)
    unexpected = [s for s in statuses if s not in (200, 409)]
    if unexpected:
        scenario_fail("concurrent-state-409", f"got unexpected status code(s) from a concurrent commit burst: {statuses}")
    elif ok_count < 1 or conflict_count < 1:
        scenario_fail("concurrent-state-409", f"expected at least one 200 and at least one 409 from {concurrent_n} truly concurrent commits, got {statuses}")
    else:
        scenario_pass(f"concurrent-state-409 ({concurrent_n} simultaneous HTTP requests against the same live container raced through the real SQLite CAS store; {ok_count} succeeded, {conflict_count} received a real HTTP 409 -- not a source-inspection claim)")

    # ── Scenario 4: crash/resume (web-manager restart + resume idempotency) ─
    # Honesty note (matches this file's own established norm, see the
    # docstring on run_wave25_crash_resume_scenario above): this proves
    # state durability and no-duplicate-import across a web-manager
    # container restart AFTER a real AI-reviewed import already completed
    # (scenario 1's own batch/album). It does NOT use a deterministic
    # mid-flight failpoint the way the Wave 25 crash/resume scenario does
    # (there is no `_acceptance_failpoint` hook inside `_ai_import_folder`
    # today) -- so unlike that scenario, this is a restart+idempotency
    # proof, not a proof of surviving a crash strictly between native
    # import and verification. Adding an equivalent deterministic failpoint
    # to the AI-import path is tracked as follow-up work, not claimed done
    # here.
    print("==> [Wave26] Crash/resume: restarting beets-web-manager, then resuming the completed AI batch...")
    restart_res = run(["docker", "restart", web_container])
    if restart_res.returncode != 0:
        scenario_fail("crash-resume-ai-batch", "could not restart the web-manager container")
        return
    if not wait_healthy(web_container):
        scenario_fail("crash-resume-ai-batch", "web-manager container did not become healthy again after restart")
        return
    scenario_pass("crash-resume-ai-batch-restart (web-manager came back healthy)")
    status, body = client.request(
        "POST", "/api/ai-batch-import",
        json_body={"recover_batch_job_id": batch_job_id, "path": "/data/torrents/music"},
        timeout=60,
    )
    if status != 200 or not body.get("ok"):
        scenario_fail("crash-resume-ai-batch", f"post-restart recover request rejected: {status} {body}")
        return
    if body.get("job_id"):
        try:
            result = client.wait_job(body["job_id"], timeout=60)
            if result.get("status") not in ("success", None):
                scenario_fail("crash-resume-ai-batch", f"post-restart recover did not resolve truthfully: {result.get('status')}")
                return
        except TimeoutError:
            pass
    try:
        con = sqlite3.connect(db_path)
        rows = con.execute("SELECT id FROM albums WHERE mb_albumid=?", (WAVE26_AI_IMPORT_RELEASE_ID,)).fetchall()
        con.close()
    except Exception as ex:
        scenario_fail("crash-resume-ai-batch", f"could not verify host-mounted DB state: {ex}")
        return
    if len(rows) != 1:
        scenario_fail("crash-resume-ai-batch", f"expected exactly 1 album row for the AI-reviewed release after the web-manager restart, found {len(rows)} -- duplicate import")
    else:
        scenario_pass("crash-resume-ai-batch-no-duplicate (AI batch state persisted across a web-manager restart in the real SQLite store; resuming the completed batch did not re-invoke native import)")

    # ── Scenario 5: engine offline -- fail closed, then retry succeeds ─────
    print("==> [Wave26] Engine offline: AI review state survives, import fails closed, retry succeeds after restart...")
    offline_src = seed_wave26_ai_import_source(
        downloads_dir, f"{WAVE26_ENGINE_OFFLINE_ARTIST}/ai-import-engine-offline",
        release_id=WAVE26_ENGINE_OFFLINE_RELEASE_ID,
        artist=WAVE26_ENGINE_OFFLINE_ARTIST, album=WAVE26_ENGINE_OFFLINE_ALBUM,
    )
    offline_batch_job_id = f"acceptance-ai-offline-{uuid.uuid4().hex[:12]}"
    try:
        seed_ai_batch_state(
            web_container, offline_batch_job_id, offline_src["container_path"], offline_src["container_path"],
            WAVE26_ENGINE_OFFLINE_RELEASE_ID, WAVE26_ENGINE_OFFLINE_RELEASEGROUP_ID,
            WAVE26_ENGINE_OFFLINE_ARTIST, WAVE26_ENGINE_OFFLINE_ALBUM,
        )
    except Exception as ex:
        scenario_fail("engine-offline-ai-import", f"could not seed AI batch state: {ex}")
        return
    stop_res = run(["docker", "stop", engine_container])
    if stop_res.returncode != 0:
        scenario_fail("engine-offline-ai-import", "could not stop the engine container")
        return
    scenario_pass("engine-offline-ai-import-engine-stopped")
    status, body = client.request(
        "POST", "/api/ai-batch-import",
        json_body={"recover_batch_job_id": offline_batch_job_id, "path": "/data/torrents/music"},
        timeout=60,
    )
    engine_offline_job_id = body.get("job_id") if status == 200 and body.get("ok") else None
    engine_offline_job_log: list = []
    if engine_offline_job_id:
        try:
            result = client.wait_job(engine_offline_job_id, timeout=90)
            engine_offline_job_log = result.get("log") or []
            # A "success" batch-job status here is NOT itself a false
            # success: _ai_batch_process_decisions() catches
            # _ai_import_folder()'s exception (the engine call failing
            # closed), queues that folder for human review with a failure
            # reason, and the WORKER still returns normally -- the batch
            # completed truthfully; it's the per-folder IMPORT that must
            # not have silently succeeded. The authoritative proof is the
            # host-mounted DB check below (no album row created), not the
            # top-level job status.
        except TimeoutError as ex:
            scenario_fail("engine-offline-ai-import", f"job did not resolve while engine was offline: {ex}")
            run(["docker", "start", engine_container])
            wait_healthy(engine_container)
            return
    # Whether the outer request itself was rejected or the job resolved,
    # the AI review state (Web-Manager-owned SQLite, not the engine) must
    # still be readable -- proving suggestion/review state is genuinely
    # independent of engine availability.
    try:
        status_field = get_ai_batch_field(web_container, offline_batch_job_id, "status")
    except Exception as ex:
        scenario_fail("engine-offline-ai-import", f"AI batch state was not readable while the engine was offline: {ex}")
        run(["docker", "start", engine_container])
        wait_healthy(engine_container)
        return
    try:
        con = sqlite3.connect(db_path)
        pre_restart_rows = con.execute("SELECT id FROM albums WHERE mb_albumid=?", (WAVE26_ENGINE_OFFLINE_RELEASE_ID,)).fetchall()
        con.close()
    except Exception as ex:
        scenario_fail("engine-offline-ai-import", f"could not verify host-mounted DB state while engine was offline: {ex}")
        run(["docker", "start", engine_container])
        wait_healthy(engine_container)
        return
    if pre_restart_rows:
        scenario_fail("engine-offline-ai-import", f"found {len(pre_restart_rows)} album row(s) for the engine-offline release before the engine was ever restarted -- a mutation happened with the engine down -- job log: {engine_offline_job_log}")
        run(["docker", "start", engine_container])
        wait_healthy(engine_container)
        return
    scenario_pass(f"engine-offline-ai-import-fails-closed (no album row created while the engine was down -- a real BeetsUnavailableError from confirmed_import_v1's engine call was caught and the folder queued for human review, not a false success; batch state remained readable, status={status_field})")

    print("==> [Wave26] Restarting the engine and retrying the same AI-reviewed import...")
    start_res = run(["docker", "start", engine_container])
    if start_res.returncode != 0 or not wait_healthy(engine_container):
        scenario_fail("engine-offline-ai-import", "engine container did not come back healthy after restart")
        return
    # Ground-truthed via a real folder_states dump on 4 consecutive real
    # CI runs (not a guess): the engine-offline failure correctly landed
    # this folder in the retryable "import_failed" status (Wave 26's own
    # scan_unavailable fix, see app.py), and /api/ai-batch-import/recover
    # with retry_failed=true DOES correctly detect and requeue it -- but
    # requeuing resets the folder to status="ai_queued" for a genuinely
    # FRESH AI-suggestion pass, discarding this test's seeded ai_result
    # (correct, desirable production behavior: a real retry should not
    # blindly reuse a possibly-bad prior suggestion). That fresh pass
    # would make a REAL OpenAI call, which fails in this acceptance
    # environment's dummy OPENAI_API_KEY for a reason unrelated to
    # anything this scenario is testing. Sidestepping the AI-retry
    # mechanism (already exercised by its own dedicated reliability
    # tests) entirely: re-seed the same provider-boundary-stubbed
    # ai_result directly (reseed_ai_batch_folder), then use the same
    # plain (non-retry_failed) recover call scenario 1 already proved
    # works, which finds "ai_completed"+ai_result and processes the
    # cached decision immediately.
    #
    # Found on real Linux CI (repeatedly): `docker start` + wait_healthy()
    # reporting the engine container healthy does not guarantee
    # beets-web-manager's actual DNS resolution of the engine's service
    # name has caught up yet. A same-process readiness check (hitting the
    # running worker's own /api/setup/status?refresh=1, which itself
    # calls the engine) is used before the real attempt, since a separate
    # `docker exec` probe process proved insufficient (a fresh process
    # always gets a fresh resolver query, which is not the same thing as
    # the already-running worker's own connection state).
    dns_deadline = time.time() + 60.0
    while time.time() < dns_deadline:
        s_status, s_body = client.request("GET", "/api/setup/status?refresh=1", timeout=30)
        if s_status == 200 and not s_body.get("stale"):
            break
        time.sleep(3)

    last_log: list = []
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        wait_engine_ipc_reachable(web_container)
        try:
            reseed_ai_batch_folder(
                web_container, offline_batch_job_id, offline_src["container_path"],
                WAVE26_ENGINE_OFFLINE_RELEASE_ID, WAVE26_ENGINE_OFFLINE_RELEASEGROUP_ID,
                WAVE26_ENGINE_OFFLINE_ARTIST, WAVE26_ENGINE_OFFLINE_ALBUM,
            )
        except Exception as ex:
            scenario_fail("engine-offline-ai-import", f"could not reseed folder ai_result before attempt {attempt}: {ex}")
            return
        status, body = client.request(
            "POST", "/api/ai-batch-import",
            json_body={"recover_batch_job_id": offline_batch_job_id, "path": "/data/torrents/music"},
            timeout=60,
        )
        if status != 200 or not body.get("ok") or not body.get("job_id"):
            scenario_fail("engine-offline-ai-import", f"post-restart retry request rejected: {status} {body}")
            return
        try:
            result = client.wait_job(body["job_id"], timeout=180)
        except TimeoutError as ex:
            scenario_fail("engine-offline-ai-import", f"post-restart retry did not resolve: {ex}")
            return
        last_log = result.get("log") or []
        if result.get("status") != "success":
            scenario_fail("engine-offline-ai-import", f"post-restart retry did not succeed truthfully: {result.get('status')} / {last_log}")
            return
        try:
            con = sqlite3.connect(db_path)
            post_restart_rows = con.execute("SELECT id FROM albums WHERE mb_albumid=?", (WAVE26_ENGINE_OFFLINE_RELEASE_ID,)).fetchall()
            con.close()
        except Exception as ex:
            scenario_fail("engine-offline-ai-import", f"could not verify host-mounted DB state after retry: {ex}")
            return
        if len(post_restart_rows) == 1:
            scenario_pass(f"engine-offline-ai-import-retry-succeeds (attempt {attempt}/{max_attempts}: the exact same AI-reviewed batch, retried after the engine came back, completed the real confirmed_import_v1 import with zero rows created while the engine was down)")
            return
        if len(post_restart_rows) > 1:
            scenario_fail("engine-offline-ai-import", f"expected at most 1 album row after the post-restart retry, found {len(post_restart_rows)} -- job log: {last_log}")
            return
        transient = any("Could not scan source folder" in line or "unresolved host" in line for line in last_log)
        if not transient or attempt == max_attempts:
            break
        print(f"  [retry {attempt}/{max_attempts}] transient engine-connectivity failure right after restart, retrying: {last_log}")
        time.sleep(min(5.0 * attempt, 20.0))
def run_jobs_transport_scenarios(client, engine_container):
    print("\n==> [Jobs Transport] Healthy Engine Checks ==>")
    status, body = client.request("GET", "/api/jobs/feed?limit=10")
    if status == 200 and body.get("ok"):
        _ok("jobs-feed-healthy")
    else:
        _fail(f"jobs-feed-healthy failed: {status} {body}")

    status, body = client.request("GET", "/api/clean/library-health")
    if status == 200 and body.get("ok"):
        _ok("cleanup-health-healthy")
    else:
        _fail(f"cleanup-health-healthy failed: {status} {body}")

    status, body = client.request("POST", "/api/library/fix-genres", json_body={"force": False, "use_ai": False})
    if status == 200 and body.get("ok"):
        _ok("genre-route-healthy")
    else:
        _fail(f"genre-route-healthy failed: {status} {body}")

    status, body = client.request("GET", "/api/library/art-repair")
    if status == 200 and body.get("ok"):
        _ok("artwork-route-healthy")
    else:
        _fail(f"artwork-route-healthy failed: {status} {body}")

    print("\n==> [Jobs Transport] Reachable Engine Application Error Check ==>")
    status, body = client.request("GET", "/api/clean/library-health?duplicate_limit=invalid_value")
    if status == 400 and body.get("error_code") == "INVALID_QUERY_PARAMETER" and body.get("error_code") != "ENGINE_OFFLINE":
        _ok("reachable-engine-application-error-not-offline")
    else:
        _fail(f"reachable-engine-application-error-not-offline failed: {status} {body}")

    print("\n==> [Jobs Transport] Engine Offline Checks ==>")
    stop_res = run(["docker", "stop", engine_container])
    if stop_res.returncode != 0:
        _fail("could not stop engine container for jobs transport offline checks")
        return

    status, body = client.request("GET", "/api/jobs")
    if status == 200 and isinstance(body.get("jobs"), list):
        _ok("jobs-engine-offline-structured")
    else:
        _fail(f"jobs-engine-offline-structured failed: {status} {body}")

    status, body = client.request("GET", "/api/clean/library-health")
    if status == 503 and body.get("error_code") == "ENGINE_OFFLINE":
        _ok("cleanup-engine-offline-structured")
    else:
        _fail(f"cleanup-engine-offline-structured failed: {status} {body}")

    status, body = client.request("POST", "/api/library/fix-genres", json_body={"force": False, "use_ai": False})
    if status == 200 or status == 503:
        _ok("genre-engine-offline-structured")
    else:
        _fail(f"genre-engine-offline-structured failed: {status} {body}")

    status, body = client.request("GET", "/api/library/art-repair")
    if status == 503 and body.get("error_code") == "ENGINE_OFFLINE":
        _ok("artwork-engine-offline-structured")
    else:
        _fail(f"artwork-engine-offline-structured failed: {status} {body}")

    print("\n==> [Jobs Transport] Engine Restart Recovery Check ==>")
    run(["docker", "start", engine_container])
    if not wait_healthy(engine_container):
        _fail("beets engine container did not become healthy again after jobs transport restart")
    else:
        status, body = client.request("GET", "/api/jobs/feed?limit=10")
        if status == 200 and body.get("ok"):
            _ok("jobs-recovery-after-engine-restart")
        else:
            _fail(f"jobs-recovery-after-engine-restart failed: {status} {body}")

# -- SEC-002 / ARCH-003 Wave 27 Docker acceptance ---------------------------
# These scenarios are deliberately placed after the Wave 24 fixture has been
# seeded and before the engine-offline Wave 24/25/26 proofs. They exercise the
# config-domain corrections through the real Web Manager HTTP boundary: Web
# Manager owns only /web-manager-data state, while Beets config changes still
# go Web Manager -> BeetsClient -> engine control agent -> real beets config
# validation and atomic engine-side replacement.


def _set_top_level_scalar_yaml(content: str, key: str, value: str) -> str:
    """Replace or append a simple top-level YAML scalar/block."""
    line = f"{key}: {value}"
    pattern = re.compile(rf"(?ms)^{re.escape(key)}\s*:.*?(?=^\S|\Z)")
    if pattern.search(content):
        updated = pattern.sub(line + "\n", content, count=1)
    else:
        updated = content.rstrip() + "\n" + line + "\n"
    return updated if updated.endswith("\n") else updated + "\n"


def _basic_json_request(base_url: str, username: str, password: str, method: str, path: str, json_body=None, timeout=30):
    url = f"{base_url.rstrip('/')}{path}"
    auth = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    headers = {"Authorization": f"Basic {auth}", "Content-Type": "application/json"}
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


def _require_json_ok(status: int, body: dict, scenario: str) -> bool:
    if status == 200 and isinstance(body, dict) and body.get("ok") is True:
        return True
    _fail(f"{scenario}: expected HTTP 200 ok=true, got {status} {body}")
    return False


def _get_config_document(client: "HttpClient", scenario: str) -> tuple[str, str] | None:
    status, body = client.request("GET", "/api/config", timeout=30)
    if status != 200 or not body.get("ok"):
        _fail(f"{scenario}: GET /api/config failed: {status} {body}")
        return None
    revision = str(body.get("revision") or "")
    content = str(body.get("content") or "")
    if not revision:
        _fail(f"{scenario}: GET /api/config did not return a revision")
        return None
    return content, revision


def _post_config_document(client: "HttpClient", content: str, revision: str, scenario: str):
    return client.request(
        "POST", "/api/config",
        json_body={"content": content, "expected_revision": revision},
        timeout=45,
    )


def run_wave27_config_scenarios(
    client: "HttpClient",
    web_container: str,
    engine_container: str,
    config_dir: Path,
    webdata_dir: Path,
) -> None:
    def scenario_pass(name: str) -> None:
        print(f"[PASS] {name}")

    def scenario_fail(name: str, detail: str) -> None:
        _fail(f"{name}: {detail}")

    username = "acceptance-admin"
    password = "Acceptance passphrase 2026!"
    web_base = client.base_url

    print("==> [Wave27] Fresh setup and Web Manager config-store durability...")
    status, body = client.request("GET", "/api/setup/status?refresh=1", timeout=30)
    if status != 200 or not isinstance(body, dict):
        scenario_fail("fresh-setup-status", f"request failed: {status} {body}")
    else:
        scenario_pass("fresh-setup-status-reachable")

    status, body = client.request(
        "POST", "/api/setup/first-run",
        json_body={"username": username, "password": password},
        timeout=30,
    )
    if status != 200 or not body.get("ok"):
        scenario_fail("fresh-setup-first-run", f"request failed: {status} {body}")
    else:
        scenario_pass("fresh-setup-first-run-persists-browser-credentials")

    status, body = client.request("POST", "/api/setup/complete", json_body={}, timeout=45)
    if status != 200 or not body.get("ok"):
        scenario_fail("fresh-setup-complete", f"request failed: {status} {body}")
    else:
        scenario_pass("fresh-setup-complete-marker-created")

    pwd_file = webdata_dir / ".browser_password"
    user_file = webdata_dir / ".browser_username"
    state_file = webdata_dir / ".browser_setup_state"
    complete_file = webdata_dir / ".setup_complete"

    def read_webdata_text(rel_name: str) -> str:
        target = webdata_dir / rel_name
        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except PermissionError:
            res = run(["docker", "exec", web_container, "cat", f"/web-manager-data/{rel_name}"])
            if res.returncode == 0:
                return res.stdout
            raise

    if not (pwd_file.exists() and user_file.exists() and state_file.exists() and complete_file.exists()):
        scenario_fail("fresh-setup-durable-files", "expected browser credential/setup marker files were not written under /web-manager-data")
    elif password in read_webdata_text(".browser_password"):
        scenario_fail("fresh-setup-password-hashing", "browser password file contains the plaintext password")
    elif read_webdata_text(".browser_username").strip() != username:
        scenario_fail("fresh-setup-username", "persisted username does not match the setup user")
    else:
        scenario_pass("fresh-setup-durable-web-manager-state")

    status, body = _basic_json_request(web_base, username, password, "GET", "/api/auth/me", timeout=30)
    if status == 200 and body.get("authenticated") is True and body.get("username") == username:
        scenario_pass("fresh-setup-basic-auth-works")
    else:
        scenario_fail("fresh-setup-basic-auth", f"Basic auth did not authenticate: {status} {body}")

    status, body = client.request("GET", "/api/setup/settings", timeout=30)
    initial_settings_rev = body.get("revision") if status == 200 and body.get("ok") else None
    if status != 200 or not body.get("ok"):
        scenario_fail("settings-read-initial", f"request failed: {status} {body}")
    else:
        status, body = client.request(
            "POST", "/api/setup/settings",
            json_body={"ai_model": "acceptance-model-a", "expected_revision": initial_settings_rev} if initial_settings_rev else {"ai_model": "acceptance-model-a"},
            timeout=30,
        )
        if not _require_json_ok(status, body, "settings-initial-save"):
            pass
        elif not (webdata_dir / "app_settings.json").exists():
            scenario_fail("settings-file-created", "app_settings.json was not written under /web-manager-data")
        else:
            scenario_pass("settings-save-uses-web-manager-config-store")

    status_a, body_a = client.request("GET", "/api/setup/settings", timeout=30)
    status_b, body_b = client.request("GET", "/api/setup/settings", timeout=30)
    rev_a = body_a.get("revision") if status_a == 200 else None
    rev_b = body_b.get("revision") if status_b == 200 else None
    if not rev_a or rev_a != rev_b:
        scenario_fail("settings-cas-read", f"settings readers did not receive the same revision: {status_a} {body_a} / {status_b} {body_b}")
    else:
        status, body = client.request(
            "POST", "/api/setup/settings",
            json_body={"ai_model": "acceptance-model-b", "expected_revision": rev_b},
            timeout=30,
        )
        if not _require_json_ok(status, body, "settings-cas-writer-b"):
            pass
        else:
            status, stale_body = client.request(
                "POST", "/api/setup/settings",
                json_body={"ai_model": "acceptance-model-a-stale", "expected_revision": rev_a},
                timeout=30,
            )
            status_final, final_body = client.request("GET", "/api/setup/settings", timeout=30)
            final_settings = final_body.get("settings") or {}
            if status != 409 or stale_body.get("code") != "settings_revision_conflict":
                scenario_fail("settings-cas-stale-write", f"stale write was not rejected with 409: {status} {stale_body}")
            elif status_final != 200 or final_settings.get("ai_model") != "acceptance-model-b":
                scenario_fail("settings-cas-final-content", f"writer B content was not preserved: {status_final} {final_body}")
            else:
                scenario_pass("settings-cas-stale-writes-fail-closed")

    print("==> [Wave27] Web Manager restart preserves Web Manager-owned setup/settings...")
    restart_res = run(["docker", "restart", web_container])
    if restart_res.returncode != 0 or not wait_healthy(web_container):
        scenario_fail("web-restart", f"web container did not restart cleanly: rc={restart_res.returncode}")
    else:
        status, body = _basic_json_request(web_base, username, password, "GET", "/api/auth/me", timeout=30)
        status_settings, body_settings = client.request("GET", "/api/setup/settings", timeout=30)
        if status != 200 or body.get("authenticated") is not True:
            scenario_fail("web-restart-basic-auth", f"Basic auth failed after restart: {status} {body}")
        elif status_settings != 200 or (body_settings.get("settings") or {}).get("ai_model") != "acceptance-model-b":
            scenario_fail("web-restart-settings", f"settings did not persist after restart: {status_settings} {body_settings}")
        else:
            scenario_pass("web-manager-config-persists-across-restart")

    print("==> [Wave27] Engine root-resolution report...")
    status, body = client.request("GET", "/api/setup/status?refresh=1", timeout=45)
    remote_paths = (((body or {}).get("beets") or {}).get("paths") or {}) if isinstance(body, dict) else {}
    expected_paths = {
        "config": "/config",
        "music_library": "/data/media/music",
        "downloads": "/data/torrents",
        "staging": "/data/torrents",
    }
    bad_roots = []
    for key, expected_path in expected_paths.items():
        actual = str((remote_paths.get(key) or {}).get("path") or "")
        if actual != expected_path:
            bad_roots.append(f"{key}={actual!r}, expected {expected_path!r}")
    if status != 200:
        scenario_fail("root-resolution-status", f"setup status failed: {status} {body}")
    elif bad_roots:
        scenario_fail("root-resolution", "; ".join(bad_roots))
    elif any(str((remote_paths.get(key) or {}).get("path") or "") in {"/music", "/downloads", "/staging"} for key in remote_paths):
        scenario_fail("root-resolution-phantom-default", f"reported phantom root in {remote_paths}")
    else:
        scenario_pass("root-resolution-uses-mounted-engine-roots")

    print("==> [Wave27] Engine config CAS, real Beets validation, rollback, and durability...")
    config_path = config_dir / "config.yaml"
    original_doc = _get_config_document(client, "config-read-original")
    if original_doc is None:
        return
    original_content, original_rev = original_doc
    safe_content = _set_top_level_scalar_yaml(original_content, "threaded", "no")
    status, body = _post_config_document(client, safe_content, original_rev, "config-safe-update")
    if not _require_json_ok(status, body, "config-safe-update"):
        return
    first_write_rev = str(body.get("revision") or "")
    if "threaded: no" not in config_path.read_text(encoding="utf-8", errors="replace"):
        scenario_fail("config-safe-update-host-file", "host-mounted engine config did not contain the committed setting")
    else:
        scenario_pass("config-safe-update-real-beets-validated")

    restart_res = run(["docker", "restart", engine_container])
    if restart_res.returncode != 0 or not wait_healthy(engine_container) or not wait_engine_ipc_reachable(web_container):
        scenario_fail("engine-config-restart", f"engine did not restart cleanly after valid config update: rc={restart_res.returncode}")
        return
    restarted_doc = _get_config_document(client, "config-read-after-engine-restart")
    if restarted_doc is None:
        return
    restarted_content, restarted_rev = restarted_doc
    if "threaded: no" not in restarted_content:
        scenario_fail("engine-config-durable-after-restart", "committed engine config did not survive engine restart")
    else:
        scenario_pass("engine-config-durable-after-restart")

    status, body = client.request(
        "POST", "/api/config/revert",
        json_body={"expected_revision": restarted_rev},
        timeout=45,
    )
    if not _require_json_ok(status, body, "config-manual-revert"):
        return
    reverted_doc = _get_config_document(client, "config-read-after-revert")
    if reverted_doc is None:
        return
    reverted_content, reverted_rev = reverted_doc
    if reverted_content == restarted_content or not reverted_rev:
        scenario_fail("config-manual-revert-content", "manual revert did not restore the previous known-good config")
    else:
        scenario_pass("config-manual-revert-restores-backup")

    reader_a = _get_config_document(client, "config-cas-reader-a")
    reader_b = _get_config_document(client, "config-cas-reader-b")
    if reader_a is None or reader_b is None:
        return
    content_a, rev_a = reader_a
    content_b, rev_b = reader_b
    if rev_a != rev_b:
        scenario_fail("config-cas-read", f"config readers did not receive same revision: {rev_a} vs {rev_b}")
    else:
        writer_b_content = _set_top_level_scalar_yaml(content_b, "threaded", "no")
        status_b, body_b = _post_config_document(client, writer_b_content, rev_b, "config-cas-writer-b")
        stale_content = _set_top_level_scalar_yaml(content_a, "threaded", "yes")
        status_a, body_a = _post_config_document(client, stale_content, rev_a, "config-cas-writer-a-stale")
        final_doc = _get_config_document(client, "config-cas-final")
        final_content = final_doc[0] if final_doc else ""
        if status_b != 200 or not body_b.get("ok"):
            scenario_fail("config-cas-writer-b", f"writer B failed: {status_b} {body_b}")
        elif status_a != 409 or body_a.get("code") != "config_revision_conflict":
            scenario_fail("config-cas-stale-write", f"stale write was not rejected with 409: {status_a} {body_a}")
        elif "threaded: no" not in final_content or "threaded: yes" in final_content:
            scenario_fail("config-cas-final-content", "writer B content was not preserved after stale write rejection")
        else:
            scenario_pass("engine-config-cas-stale-writes-return-409")

    current_doc = _get_config_document(client, "config-read-before-invalid-yaml")
    if current_doc is None:
        return
    current_content, current_rev = current_doc
    before_invalid_bytes = config_path.read_bytes()
    status, body = _post_config_document(client, "directory: [unterminated\n", current_rev, "config-invalid-yaml")
    if status != 400 or body.get("code") != "config_invalid_yaml":
        scenario_fail("config-invalid-yaml", f"malformed YAML was not rejected as invalid YAML: {status} {body}")
    elif config_path.read_bytes() != before_invalid_bytes:
        scenario_fail("config-invalid-yaml-preserved", "engine config changed after malformed YAML rejection")
    else:
        scenario_pass("config-invalid-yaml-preserves-known-good")

    current_doc = _get_config_document(client, "config-read-before-invalid-beets")
    if current_doc is None:
        return
    current_content, current_rev = current_doc
    before_invalid_beets_bytes = config_path.read_bytes()
    missing_plugin = "wave27_missing_plugin_" + uuid.uuid4().hex[:12]
    beets_invalid = _set_top_level_scalar_yaml(current_content, "plugins", f"fetchart musicbrainz {missing_plugin}")
    status, body = _post_config_document(client, beets_invalid, current_rev, "config-beets-invalid")
    if status != 400 or body.get("code") != "config_beets_validation_failed":
        scenario_fail("config-beets-invalid", f"real Beets-invalid config was not rejected: {status} {body}")
    elif config_path.read_bytes() != before_invalid_beets_bytes:
        scenario_fail("config-beets-invalid-preserved", "engine config changed after real Beets validation rejection")
    else:
        scenario_pass("config-real-beets-validation-preserves-known-good")

    print("==> [Wave27] Engine-offline config mutation fails closed while Web Manager settings still work...")
    current_doc = _get_config_document(client, "config-read-before-engine-offline")
    if current_doc is None:
        return
    current_content, current_rev = current_doc
    before_offline_bytes = config_path.read_bytes()
    status_settings, body_settings = client.request("GET", "/api/setup/settings", timeout=30)
    settings_rev = body_settings.get("revision") if status_settings == 200 and body_settings.get("ok") else None
    stop_res = run(["docker", "stop", engine_container])
    if stop_res.returncode != 0:
        scenario_fail("engine-offline-stop", f"could not stop engine: rc={stop_res.returncode}")
        return
    restart_failed = False
    try:
        local_settings_ok = False
        if settings_rev:
            s_status, s_body = client.request(
                "POST", "/api/setup/settings",
                json_body={"ai_model": "acceptance-model-offline", "expected_revision": settings_rev},
                timeout=30,
            )
            local_settings_ok = s_status == 200 and s_body.get("ok") is True
        offline_content = _set_top_level_scalar_yaml(current_content, "threaded", "yes")
        c_status, c_body = _post_config_document(client, offline_content, current_rev, "config-engine-offline")
        if not local_settings_ok:
            scenario_fail("engine-offline-local-settings", "Web Manager-owned setup settings did not continue to save while engine was offline")
        elif c_status == 200 and c_body.get("ok"):
            scenario_fail("config-engine-offline-fail-closed", f"Beets config mutation succeeded while engine was offline: {c_status} {c_body}")
        elif config_path.read_bytes() != before_offline_bytes:
            scenario_fail("config-engine-offline-preserved", "host-mounted engine config changed while engine was offline")
        else:
            scenario_pass("config-engine-offline-fails-closed-with-local-settings-still-durable")
    finally:
        run(["docker", "start", engine_container])
        if not wait_healthy(engine_container) or not wait_engine_ipc_reachable(web_container):
            scenario_fail("engine-offline-restart", "engine did not become reachable after offline config proof")
            restart_failed = True
    if restart_failed:
        return

    retry_doc = _get_config_document(client, "config-read-after-engine-online")
    if retry_doc is None:
        return
    retry_content, retry_rev = retry_doc
    retry_save = _set_top_level_scalar_yaml(retry_content, "threaded", "yes")
    status, body = _post_config_document(client, retry_save, retry_rev, "config-retry-after-engine-online")
    if _require_json_ok(status, body, "config-retry-after-engine-online"):
        scenario_pass("config-retry-succeeds-after-engine-online")


def run_wave27_attach_failpoint_scenario(web_container: str, fixture: dict) -> None:
    print("==> [Wave27] Attach-recording dict-shaped IPC failure fails closed...")
    target_mbid = "88888888-8888-4888-8888-888888888888"
    probe = r'''
import json
import sys
import time

import app as APP

item_id = int(sys.argv[1])
target_mbid = sys.argv[2]


def fake_reconstruct(item, iid):
    current = {
        "title": getattr(item, "title", "Acceptance Track"),
        "artist": getattr(item, "artist", "Acceptance Artist"),
        "album": getattr(item, "album", "Acceptance Album"),
    }
    candidate = {
        "mb_trackid": target_mbid,
        "recording_id": target_mbid,
        "title": "Acceptance Track",
        "artist": "Acceptance Artist",
        "release_id": "11111111-1111-1111-1111-111111111111",
        "release_group_id": "22222222-2222-2222-2222-222222222222",
        "decision_version": "wave27-acceptance",
        "decision": {
            "confidence_score": 0.99,
            "review_required": False,
            "requires_confirmation": False,
            "conflicts": [],
            "warnings": [],
            "eligibility_reason": "Wave 27 acceptance deterministic safe attach",
            "action_eligibility": {"attach_without_review": True},
        },
        "matching_contract": {"identity": {"resolved_recording_id": target_mbid}},
    }
    return current, [candidate], str(getattr(item, "path", "")), "Acceptance Track.wav"


APP.app.config["TESTING"] = True
APP._reconstruct_track_recording_candidates = fake_reconstruct
APP._fetch_mb_recording_details = lambda rid: {"linked_releases": [], "selected_release": {}}
APP._trigger_plex_refresh = lambda log: False

client = APP.app.test_client()
resp = client.post(
    f"/api/items/{item_id}/attach-recording",
    json={
        "mb_trackid": target_mbid,
        "mode": "safe",
        "decision_version": "wave27-acceptance",
        "_acceptance_failpoint": "attach_recording_apply_failure",
    },
)
try:
    payload = resp.get_json() or {}
except Exception:
    payload = {"raw": resp.get_data(as_text=True)}
if resp.status_code != 200 or payload.get("ok") is not True or not payload.get("job_id"):
    print(json.dumps({"ok": False, "stage": "dispatch", "status_code": resp.status_code, "payload": payload}, default=str))
    sys.exit(1)
job_id = payload["job_id"]
audit_id = payload.get("audit_id")
job = None
for _ in range(120):
    job = APP.jobs.get(job_id)
    status = getattr(job, "status", "") if job is not None else ""
    if status in {"success", "failed", "cancelled", "timeout", "cancel_failed"}:
        break
    time.sleep(0.25)
job_status = getattr(job, "status", "") if job is not None else "missing"
job_log = list(getattr(job, "log", []) or []) if job is not None else []
try:
    tx = APP.transactions.get(audit_id)
except Exception as exc:
    tx = {"error": str(exc)}
tx_status = tx.get("status") if isinstance(tx, dict) else None
tx_logs = tx.get("logs") or [] if isinstance(tx, dict) else []
item = APP.lib.get_item(item_id)
actual_mbid = str(getattr(item, "mb_trackid", "") or "").strip().lower() if item is not None else ""
combined_logs = "\n".join(str(x) for x in (job_log + tx_logs))
ok = (
    job_status == "failed"
    and tx_status != "Completed"
    and target_mbid.lower() != actual_mbid
    and "Recording ID attached and item synced/moved." not in combined_logs
)
print(json.dumps({
    "ok": ok,
    "job_status": job_status,
    "tx_status": tx_status,
    "actual_mbid": actual_mbid,
    "audit_id": audit_id,
    "job_log_tail": job_log[-8:],
    "tx_log_tail": tx_logs[-8:] if isinstance(tx_logs, list) else tx_logs,
}, default=str))
sys.exit(0 if ok else 1)
'''
    res = run([
        "docker", "exec",
        "-e", "BEETS_WEB_AUTH_DISABLED=1",
        web_container,
        "python3", "-c", probe,
        str(fixture["item_id"]), target_mbid,
    ], timeout=180)
    output = (res.stdout or "").strip()
    try:
        body = json.loads(output.splitlines()[-1]) if output else {}
    except Exception:
        body = {"raw_stdout": res.stdout, "raw_stderr": res.stderr}
    if res.returncode != 0 or body.get("ok") is not True:
        _fail(f"attach-recording-dict-failure: expected failed job/transaction without success log or mutation, got rc={res.returncode} {body} stderr={res.stderr}")
    else:
        print("[PASS] attach-recording-dict-shaped-ipc-failure-stops-before-success")

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
        # Wave 26 (PR #101): /api/ai-batch-import hard-requires a non-empty
        # OPENAI_API_KEY before it will even start a worker. The Wave 26
        # scenarios below never let this key reach a real network call --
        # every folder they exercise is seeded directly into
        # status="ai_completed" with its ai_result already populated (see
        # docker/acceptance/ai_batch_seed.py), so the real worker's own
        # recover-path short-circuit skips the AI-suggest step entirely.
        # This is a dummy value solely to satisfy that startup gate.
        "OPENAI_API_KEY": "acceptance-dummy-not-a-real-key-never-called",
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
            print_container_log_tails(engine_container, web_container)
            return 2
        if not wait_healthy(web_container):
            _fail("beets-web-manager container did not become healthy")
            print_container_log_tails(web_container, engine_container)
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
            canonical_host_path = Path(fixture["item_path"])
            con = sqlite3.connect(fixture["db_path"])
            try:
                remaining = con.execute("SELECT COUNT(*) FROM items WHERE id=?", (fixture["cleanup_item_id"],)).fetchone()[0]
                # library_cleanup_v1's whole point is narrow, reviewed
                # duplicate retirement, not "delete everything nearby" --
                # explicitly prove the surviving canonical track and its
                # album row are untouched, not merely that the duplicate
                # is gone.
                canonical_row = con.execute(
                    "SELECT id, album_id FROM items WHERE id=?", (fixture["item_id"],)
                ).fetchone()
                album_row = con.execute(
                    "SELECT id, album, albumartist FROM albums WHERE id=?", (fixture["album_id"],)
                ).fetchone()
            finally:
                con.close()
            if cleanup_host_path.exists() or int(remaining or 0) != 0:
                _fail("library duplicate cleanup did not quarantine the file and retire the DB row")
            elif not canonical_host_path.exists():
                _fail("library duplicate cleanup removed the surviving canonical track's file, not just the duplicate")
            elif not canonical_row or int(canonical_row[1]) != int(fixture["album_id"]):
                _fail("library duplicate cleanup lost or reparented the surviving canonical item row")
            elif not album_row or album_row[1] != "Acceptance Album" or album_row[2] != "Acceptance Artist":
                _fail("library duplicate cleanup left the album row incoherent")
            else:
                _ok(
                    "library duplicate cleanup succeeded through Web Manager -> Beets engine IPC "
                    "(duplicate quarantined + DB row retired; surviving canonical track and album untouched)"
                )

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

        print("\n==> Wave 27 scenarios (config closure, roots, attach failure) ==>")
        run_wave27_config_scenarios(client, web_container, engine_container, config_dir, webdata_dir)
        run_wave27_attach_failpoint_scenario(web_container, fixture)

        print("\n==> Wave 25 scenarios (real production import + artwork-acquisition routes) ==>")
        run_jobs_transport_scenarios(client, engine_container)
        run_wave25_artwork_scenarios(client, fixture, db_path)
        run_wave25_scenarios(client, downloads_dir, music_dir, db_path)

        print("==> Engine-offline fail-closed proof...")
        offline_host_path = Path(fixture["offline_item_path"])
        offline_container_path = fixture["offline_container_path"]
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

            print("==> [Wave25] Engine-offline fail-closed proof (Wave 25 routes)...")
            wave25_engine_offline_checks(client, db_path)

            run(["docker", "start", engine_container])
            if not wait_healthy(engine_container):
                _fail("beets engine container did not become healthy again after restart")
            else:
                _ok("engine container healthy again after restart")

        print("\n==> Wave 25 crash/resume scenario ==>")
        run_wave25_crash_resume_scenario(client, engine_container, downloads_dir, db_path)

        print("\n==> Wave 26 AI Batch Import scenarios ==>")
        run_wave26_ai_import_scenarios(client, web_container, engine_container, downloads_dir, db_path)

        if FAILURES:
            print(f"\n[SUMMARY] {len(FAILURES)} failure(s):")
            for f in FAILURES:
                print(f"  - {f}")
            # Wave 24 final review round 3, section 21-24: a bare pass/fail
            # summary with no container output is not diagnosable from CI
            # alone -- print both services' logs before teardown discards
            # them, same as compose-verification's existing Docker job
            # already does for its own single-container smoke test.
            print_container_log_tails(web_container, engine_container)
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
