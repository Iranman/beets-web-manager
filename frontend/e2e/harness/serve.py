"""Run the real app on a synthetic library for Playwright. Test tool only.

    python frontend/e2e/harness/serve.py --port 18337 [--no-frontend]

The root is rebuilt from nothing on every start, all state paths point inside
it, and non-loopback sockets are refused and logged in ``blocked-network.log``
(checked by the suite's global teardown). Provider HTTP is answered at the
seam provider_boundary calls through (``urllib.request.urlopen`` and
``open_public_url``) by the canned answers below; anything else is "unreachable",
which the app must already handle as a provider outage.
"""
import argparse
import io
import json
import os
import re
import shutil
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
_SCRUBBED = re.compile(r"^(BEETS|PLEX|LIDARR|SLSKD|QBIT|QB_|AI_|OPENAI|OPENROUTER|ACOUSTID|DISCOGS|LISTENBRAINZ|SPOTIFY)")


def isolated_root(port: int) -> Path:
    root = Path(tempfile.gettempdir()).resolve() / f"bwm-e2e-{port}"
    shutil.rmtree(root, ignore_errors=True)
    if root.exists():
        sys.exit(f"could not clear {root}; is another harness still running?")
    return root


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
        "WEB_MANAGER_DATA_DIR": str(paths["data"]),
        "METADATA_CACHE_DIR": str(paths["cache"]),
        "BEETS_TRANSACTION_DIR": str(paths["transactions"]),
        "AI_BATCH_STATE_DIR": str(paths["data"] / "ai_batch_jobs"),
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
    blocked_log = root / "blocked-network.log"
    calls_log = root / "provider-calls.log"
    real_connect = socket.socket.connect

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else ""
        if host in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, address)
        with blocked_log.open("a", encoding="utf-8") as f:
            f.write(f"{host}\n")
        raise OSError("E2E harness: non-loopback connection refused")

    socket.socket.connect = guarded_connect

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

    root = isolated_root(args.port)
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
