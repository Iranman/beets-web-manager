"""Run the real app on a synthetic library for Playwright. Test tool only.

    python frontend/e2e/harness/serve.py --port 18337 [--no-frontend]

Importing the app has side effects outside any configured path: it moves
legacy state files out of ``/config``. So this refuses to start wherever that
could touch real data, and without an empty network namespace (see
``refuse_unsafe_host``). Inside one, each start gets a new root and all
overridable state paths point inside it.

An audit hook logs this process's non-loopback sockets and lookups, and any child
process outside a short list, in ``blocked-network.log``; global teardown fails
the suite if that file exists. Provider HTTP is answered at the seams it goes through (``urlopen``,
``open_public_url``, ``requests``) by the canned answers below; anything else is
"unreachable", which the app must already handle as a provider outage.
"""
import argparse
import io
import json
import os
import re
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
HARNESS = Path(__file__).resolve().parent
PASSWORD = "e2e-synthetic-password-not-a-secret"
#: Settings from the developer's shell must not reach real services.
_SCRUBBED = re.compile(
    r"^(BEETS|PLEX|LIDARR|SLSKD|QBIT|QB_|AI_|OPENAI|OPENROUTER|ACOUSTID|DISCOGS|LISTENBRAINZ|SPOTIFY|SETUP_|WEB_MANAGER"
    r"|MUSIC_|DOWNLOAD|YTDLP|IMPORT_REVIEW|METADATA|PLAYLIST|ALLOW_TORRENT|SPOTIFLAC|RECOVER_OWNED|DEMO_MODE)|_FILE$")
#: Port 1: nothing listens there, so these look like services that are down.
_DEAD_URL = "http://127.0.0.1:1"
#: What the app and ctypes.util.find_library legitimately run. None opens a socket for them.
_ALLOWED_CHILDREN = {"ffmpeg", "ffprobe", "fpcalc", "ld", "ldconfig"}
_LOOPBACK = {None, "", b"", "127.0.0.1", "::1", "localhost", b"127.0.0.1", b"::1", b"localhost"}


def _mounted(path: Path) -> bool:
    with open("/proc/self/mountinfo", encoding="utf-8") as f:
        points = [line.split()[4] for line in f]
    return any(point == str(path) or point.startswith(f"{path}/") for point in points)


def _in_throwaway_container(config: Path) -> bool:
    """The Dockerfile's marker, in a container, with nothing mounted at /config."""
    in_container = Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
    return os.environ.get("BWM_E2E_THROWAWAY") == "1" and in_container and not _mounted(config)


def _network_sealed() -> bool:
    """True when this namespace can only reach loopback."""
    with open("/proc/net/route", encoding="utf-8") as f:
        if len(f.readlines()) > 1:
            return False
    try:
        with open("/proc/net/ipv6_route", encoding="utf-8") as f:
            return all(line.split()[-1] == "lo" for line in f)
    except FileNotFoundError:
        return True


def refuse_unsafe_host(config: Path = Path("/config")) -> None:
    if os.name != "posix":
        sys.exit("E2E harness refused: Linux only. On Windows \"/config\" is a real folder on this drive. See frontend/e2e/README.md.")
    if config.is_symlink():  # the mount check below would look at the link, not at where the app's files end up
        sys.exit(f"E2E harness refused: {config} is a symlink and importing the app moves files out of it. "
                 "Run the suite in the throwaway container. See frontend/e2e/README.md.")
    if config.exists() and not _in_throwaway_container(config):
        sys.exit(f"E2E harness refused: {config} exists and importing the app moves files out of it. "
                 "Run the suite in the throwaway container. See frontend/e2e/README.md.")
    if not _network_sealed():
        sys.exit("E2E harness refused: this network namespace has routes. Use --network none or unshare -n. "
                 "See frontend/e2e/README.md.")


def isolated_root(port: int) -> Path:
    """A new directory for this run. Never reuses or deletes an existing one: a
    fixed path could be a host folder mounted there. env.ts makes the run directory."""
    run_dir = os.environ.get("BWM_E2E_RUN_DIR") or tempfile.mkdtemp(prefix="bwm-e2e-")
    root = Path(run_dir).resolve() / str(port)
    try:
        root.mkdir(parents=True)
    except FileExistsError:
        sys.exit(f"E2E harness refused: {root} already exists. Start each run with a new BWM_E2E_RUN_DIR.")
    return root


def install_network_audit(root: Path) -> None:
    """Runs before the app is imported. Audit events fire inside the C socket
    methods, so connect_ex and sendto are covered and no import can undo it."""
    log = root / "blocked-network.log"
    allowed = _LOOPBACK | {socket.gethostname()}

    def refuse(event, target):
        with log.open("a", encoding="utf-8") as f:
            f.write(f"{event} {target!r}\n")
        raise PermissionError(f"E2E harness: {event} refused")

    def hook(event, args):
        if event in ("socket.connect", "socket.sendto"):
            address = args[1]
            host = address[0] if isinstance(address, tuple) else None  # a str is a Unix socket
            if host not in allowed:
                refuse(event, host)
        elif event in ("socket.getaddrinfo", "socket.gethostbyname"):
            if args[0] not in allowed:
                refuse(event, args[0])
        elif event in ("subprocess.Popen", "os.posix_spawn", "os.exec"):
            # A child's sockets are invisible to this hook, so only known local tools may start.
            program = args[0] if args[0] else args[1][0]
            version_probe = list(args[1])[1:] == ["--version"]  # how the app detects yt-dlp's JS runtime
            if Path(os.fsdecode(program)).name not in _ALLOWED_CHILDREN and not version_probe:
                refuse(event, os.fsdecode(program))
        elif event in ("os.system", "os.spawn", "os.fork", "os.forkpty"):
            refuse(event, str(args)[:200])

    sys.addaudithook(hook)


def configure(root: Path, port: int) -> dict:
    paths = {name: root / name for name in ("config", "music", "downloads", "data", "cache", "transactions")}
    for path in paths.values():
        path.mkdir(parents=True)
    import yaml
    db = paths["config"] / "musiclibrary.blb"
    (paths["config"] / "config.yaml").write_text(yaml.safe_dump({
        "directory": str(paths["music"]),
        "library": str(db),
        "plugins": ["musicbrainz", "ftintitle"],  # the matcher, and one stock plugin for the plugin-status specs
        "webmanager": {"import_roots": [str(paths["downloads"])],
                       "allowed_roots": [str(paths["music"]), str(paths["downloads"])]},
    }), encoding="utf-8")
    (paths["data"] / ".setup_complete").write_text("e2e\n", encoding="utf-8")

    for name in [n for n in os.environ if _SCRUBBED.match(n)]:
        del os.environ[name]
    os.environ.update({
        "HOME": str(root), "USERPROFILE": str(root), "APPDATA": str(root),
        "BEETSDIR": str(paths["config"]),
        "BEETS_CONFIG": str(paths["config"] / "config.yaml"),
        "BEETS_LIBRARY": str(db),
        "LIB_PATH": str(db),
        "BEETS_LOG": str(paths["config"] / "beet.log"),
        "MUSIC_ROOT": str(paths["music"]),
        "DOWNLOADS_ROOT": str(paths["downloads"]),
        # Import accepts sources under these, so a value from the shell would reach host folders.
        "TORRENT_SOURCE_ROOTS": str(paths["downloads"]),
        "WEB_MANAGER_DATA_DIR": str(paths["data"]),
        "METADATA_CACHE_DIR": str(paths["cache"]),
        "BEETS_TRANSACTION_DIR": str(paths["transactions"]),
        "AI_BATCH_STATE_DIR": str(paths["data"] / "ai_batch_jobs"),
        "RECOVERY_CARRIER_STATE_DIR": str(paths["data"] / "recovery_carriers"),
        "UNMATCHED_DRAFT_DIR": str(paths["data"] / "unmatched_drafts"),
        "YTDLP_RUNTIME_BIN_DIR": str(paths["data"] / "yt-dlp"),
        # These default to /config, outside the root, whatever BEETSDIR says.
        "YTDLP_NETRC_FILE": str(paths["config"] / ".netrc"),
        "SLSKD_API_KEY_FILE": str(paths["config"] / "slskd_api_key"),
        "MUSIC_FORMAT_PREFS_FILE": str(paths["config"] / "music_format_preferences.json"),
        "MUSIC_FORMAT_REPLACEMENT_STATUS_FILE": str(paths["config"] / "music_format_replacements.json"),
        "MUSIC_FORMAT_QUARANTINE_DIR": str(paths["config"] / "music_format_quarantine"),
        "BEETS_SUBMISSION_DRAFTS": str(paths["config"] / "submission_drafts.json"),
        "IMPORT_REVIEW_QUARANTINE_DIR": str(paths["config"] / "import_review_quarantine"),
        "IMPORT_REVIEW_AUTO_IMPORTS_FILE": str(paths["config"] / "import_review_auto_imports.json"),
        "WEBCONTROL_PORT": str(port),
        "BEETS_WEB_USERNAME": "admin",
        "BEETS_WEB_PASSWORD": PASSWORD,
        "PROVIDER_MAX_ATTEMPTS": "1",
        "PLAYLIST_AUTO_SYNC": "0",
        # Fixture tracks are 1 s; the default treats anything under 45 s as a preview clip.
        "PLAYLIST_MIN_DOWNLOAD_SECONDS": "0",
        # Defaults name Compose services ("beets", "lidarr"), or read /config.
        "BEETS_WEB_URL": _DEAD_URL,
        "LIDARR_URL": _DEAD_URL,
        "LIDARR_URL_FILE": str(paths["config"] / "lidarr_url"),
        "LIDARR_API_KEY_FILE": str(paths["config"] / "lidarr_api_key"),
    })
    return paths


class CannedResponse(io.BytesIO):
    def __init__(self, url, body, status=200):
        super().__init__(body)
        self.status, self.code, self.url = status, status, url
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"

    def getcode(self):
        return self.status

    def geturl(self):
        return self.url

    def info(self):
        return self.headers


def musicbrainz_answer(path: str, fixture):
    artist = {"id": "dddddddd-1111-4111-8111-111111111111", "name": "Review Artist", "sort-name": "Review Artist",
              "disambiguation": "", "aliases": [], "type": "Group"}
    credit = [{"name": "Review Artist", "joinphrase": "", "artist": artist}]

    def track(n, title):
        recording = {"id": f"eeeeeeee-{n}{n}{n}{n}-4{n}{n}{n}-8{n}{n}{n}-{str(n) * 12}", "title": title, "length": 1000,
                     "disambiguation": "", "aliases": [], "artist-credit": credit, "isrcs": [], "relations": [],
                     "video": False, "first-release-date": "2010-01-01"}
        return {"id": f"ffffffff-{n}{n}{n}{n}-4{n}{n}{n}-8{n}{n}{n}-{str(n) * 12}", "position": n, "number": str(n),
                "title": title, "length": 1000, "artist-credit": credit, "recording": recording}

    release = {
        "id": fixture.REVIEW_RELEASE_ID, "title": "Review Album", "status": "Official", "date": "2010-01-01",
        "country": "XW", "artist-credit": credit, "asin": None, "barcode": None, "disambiguation": "",
        "packaging": None, "quality": "normal", "aliases": [], "label-info": [], "relations": [], "genres": [], "tags": [],
        "release-events": [{"date": "2010-01-01", "area": None}],
        "text-representation": {"language": "eng", "script": "Latn"},
        "release-group": {"id": fixture.REVIEW_GROUP_ID, "title": "Review Album", "primary-type": "Album",
                          "secondary-types": [], "disambiguation": "", "first-release-date": "2010-01-01",
                          "artist-credit": credit, "aliases": [], "genres": [], "tags": []},
        "media": [{"position": 1, "track-count": 2, "format": "Digital Media", "title": "",
                   "tracks": [track(1, "Review One"), track(2, "Review Two")]}],
    }
    path = path.rstrip("/")
    if path.endswith("/release"):
        return {"releases": [dict(release, score=100)], "release-count": 1, "count": 1}
    if "/release/" in path:
        return release
    if "/release-group/" in path:
        return {"id": fixture.REVIEW_GROUP_ID, "title": "Review Album", "primary-type": "Album",
                "first-release-date": "2010-01-01", "releases": [release], "artist-credit": release["artist-credit"]}
    if path.endswith("/release-group"):
        return {"release-groups": [], "count": 0}
    if path.endswith("/artist"):
        return {"artists": [], "count": 0}
    return {}


def install_network_stubs(root: Path, fixture) -> None:
    calls_log = root / "provider-calls.log"

    def stub_urlopen(request, *args, **kwargs):
        url = request if isinstance(request, str) else request.full_url
        parsed = urllib.parse.urlsplit(url)
        with calls_log.open("a", encoding="utf-8") as f:
            f.write(f"{parsed.hostname}{parsed.path}\n")  # no query: it can carry keys
        if parsed.hostname == "musicbrainz.org":
            return CannedResponse(url, json.dumps(musicbrainz_answer(parsed.path, fixture)).encode())
        raise urllib.error.URLError("E2E harness: provider stubbed as unreachable")

    urllib.request.urlopen = stub_urlopen
    from backend import provider_boundary
    provider_boundary.open_public_url = lambda url, **kw: stub_urlopen(url)

    # Embedded Beets' own MusicBrainz client uses requests, not provider_boundary.
    import requests

    def stub_send(self, request, **kwargs):
        try:
            canned = stub_urlopen(request.url)
        except urllib.error.URLError as exc:
            raise requests.ConnectionError(str(exc), request=request) from exc
        response = requests.Response()
        response.status_code, response.url, response.request = 200, request.url, request
        response.headers["Content-Type"] = "application/json"
        response._content = canned.read()
        return response

    requests.adapters.HTTPAdapter.send = stub_send


def install_clean_all_hold(root: Path) -> None:
    """While ``hold-clean-all`` exists, Clean All waits at its third task, after
    the first two checkpointed. A small synthetic library otherwise finishes
    before a spec can see "running" or press Stop. Cancellation behaves as in
    the real step: the task raises "cancelled" when the cancel event is set."""
    import routes_maintenance
    hold = root / "hold-clean-all"
    real_step = routes_maintenance._maintenance_root_folder_repair

    def held_step(log, cancel_event=None, **kwargs):
        while hold.exists():
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("cancelled")
            time.sleep(0.1)
        return real_step(log, cancel_event=cancel_event, **kwargs)

    routes_maintenance._maintenance_root_folder_repair = held_step


def seed_review_queue(paths, fixture) -> None:
    from backend.pending_review_store import _save_pending_reviews
    folder = str(paths["downloads"] / fixture.REVIEW_FOLDER)
    _save_pending_reviews([{
        "path": folder,
        "folder_path": folder,
        "folder_name": fixture.REVIEW_FOLDER,
        "album": "Review Album",
        "albumartist": "Review Artist",
        "artist": "Review Artist",
        "mb_albumid": fixture.REVIEW_RELEASE_ID,
        "mb_releasegroupid": fixture.REVIEW_GROUP_ID,
        "confidence": "medium",
        "reason": "Synthetic suggestion that needs a person to approve it",
        "review_evidence": {},
    }])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--no-frontend", action="store_true", help="serve with the frontend build missing")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (0.0.0.0 to debug a container from outside)")
    args = parser.parse_args()

    refuse_unsafe_host()
    root = isolated_root(args.port)
    install_network_audit(root)
    paths = configure(root, args.port)
    sys.path[:0] = [str(REPO), str(HARNESS)]
    os.chdir(root)

    import build_fixture
    build_fixture.build(paths["music"], paths["downloads"], paths["config"] / "musiclibrary.blb")

    import app as app_module  # the real app; same boot steps as app.py's __main__
    from backend import beets_embedded
    install_network_stubs(root, build_fixture)
    install_clean_all_hold(root)
    seed_review_queue(paths, build_fixture)
    if args.no_frontend:
        app_module.REACT_DIST_DIR = root / "no-such-build"
    beets_embedded.status()
    from waitress import serve
    print(f"E2E harness on http://127.0.0.1:{args.port} (root {root})", flush=True)
    serve(app_module.app, host=args.host, port=args.port, threads=8)


if __name__ == "__main__":
    main()
