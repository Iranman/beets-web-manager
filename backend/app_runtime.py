"""Process configuration and shared runtime primitives for Beets Web Manager (ARCH-001 foundation layer).

Boot environment loading, environment-derived settings, filesystem roots, shared regexes and small primitives used across every domain. This module never imports app.py, a route module or a domain service.
"""

from __future__ import annotations

import logging, os, platform, re, shutil, sqlite3, subprocess, threading, time
import urllib.error, urllib.parse, urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

# The Flask application's logger (Flask names it after the import name, "app").
# Service modules log through this object instead of reaching for app.logger.
_app_logger = logging.getLogger("app")

# ── ARCH-001 extracted code ──


# Convenience aliases used throughout (matches inline import pattern in functions)
_ur = urllib.request


_up = urllib.parse


# Single source of truth for where Web Manager's own durable state lives.
# Set (not just read) as early as possible, before any other module in this
# process reads WEB_MANAGER_DATA_DIR: routes_setup.py and
# backend/web_manager_config_store.py each independently default to
# "/web-manager-data" when the env var is unset, so unless the unified
# single-compose layout's actual mount ("/data") is exported here first,
# every one of app.py's own /web-manager-data-prefixed constants below,
# plus every other module's fallback, silently disagrees with each other
# and with WEB_MANAGER_DATA_DIR -- state written under one path (the real
# bind mount, /data) can never be found again at the other (the container's
# un-mounted, non-persistent built-in /web-manager-data directory).
# Whether the deployment named its data dir explicitly (containers do); only
# then are Jobs durable by default -- a test process must not write job
# records into whatever directory the default happens to point at.
_DATA_DIR_EXPLICIT = "WEB_MANAGER_DATA_DIR" in os.environ
os.environ.setdefault(
    "WEB_MANAGER_DATA_DIR",
    "/data" if os.path.exists("/data") else "/web-manager-data",
)


# ── Data helpers ──────────────────────────────────────────────────────────────

def _s(v: Any) -> str:
    # Canonical text coercion. Bytes (e.g. Beets paths) decode as UTF-8 rather
    # than rendering as "b'...'". app.py previously defined this twice; the
    # later, bytes-aware definition was the one in effect and is kept here.
    return v.decode("utf-8", errors="replace") if isinstance(v, bytes) else str(v or "")


_BOOT_ENV_BLOCKED_NAMES = {"SETUP_ENV_FILE", "SETUP_ENV_EXAMPLE_FILE", "SETUP_SETTINGS_FILE", "SETUP_COMPLETE_FILE"}


def _decode_boot_env_value(raw: str) -> str:
    value = (raw or "").strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        inner = value[1:-1]
        return (
            inner.replace(r"\n", "\n")
            .replace(r"\r", "\r")
            .replace(r"\"", '"')
            .replace(r"\\", "\\")
        )
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value


def _setup_env_file_path() -> Path:
    return Path(os.environ.get("SETUP_ENV_FILE", os.path.join(os.environ["WEB_MANAGER_DATA_DIR"], ".env")))


def _load_persisted_setup_env_at_boot() -> None:
    """Load saved application settings before clients read os.environ.

    First runs the one-time migration that removes host/deployment/container
    and dead keys earlier releases copied into the file from .env.example
    (e.g. DOWNLOADS_PATH=./downloads, BEETS_CONFIG_PATH=./beets), then loads
    only application-layer keys (backend.config_layers). A non-blank value
    already in the process environment (Docker) always wins.
    """
    from backend import config_layers

    env_file = _setup_env_file_path()
    config_layers.migrate_saved_env_file(env_file)
    config_layers.load_saved_env_into_environ(
        env_file,
        decode=_decode_boot_env_value,
        blocked=_BOOT_ENV_BLOCKED_NAMES,
    )


_load_persisted_setup_env_at_boot()


os.environ.setdefault("BEETSDIR", "/config")


# ── yt-dlp: probe preinstalled tools; runtime package/binary installs are disabled
_ytdlp_ready = threading.Event()


_YTDLP_PIP_PACKAGE = os.environ.get("YTDLP_PIP_PACKAGE", "yt-dlp[default,curl-cffi]").strip() or "yt-dlp[default,curl-cffi]"


_YTDLP_PIP_FALLBACK_PACKAGE = os.environ.get("YTDLP_PIP_FALLBACK_PACKAGE", "yt-dlp[default]").strip() or "yt-dlp[default]"


_YTDLP_BGUTIL_PIP_PACKAGE = os.environ.get("YTDLP_BGUTIL_PIP_PACKAGE", "bgutil-ytdlp-pot-provider==2.0.1").strip()


_YTDLP_RUNTIME_BIN_DIR = Path(os.environ.get("YTDLP_RUNTIME_BIN_DIR", "/config/yt-dlp/bin"))


_YTDLP_AUTH_SMOKE_TTL = int(os.environ.get("YTDLP_AUTH_SMOKE_TTL", "300") or "300")


_ytdlp_auth_smoke_lock = threading.Lock()


_ytdlp_auth_smoke_cache: Dict[str, Dict[str, Any]] = {}


_plugin_install_log: List[str] = []   # visible via /api/plugins/install-log


def _config_file_first_line(env_name: str, default_path: str) -> str:
    key_path = Path(os.environ.get(env_name, default_path))
    try:
        value = key_path.read_text(encoding="utf-8", errors="ignore").strip()
        return value.splitlines()[0].strip() if value else ""
    except Exception:
        return ""


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)) or default)
    except Exception:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _env_float(name: str, default: float, *, minimum: Optional[float] = None, maximum: Optional[float] = None) -> float:
    try:
        value = float(os.environ.get(name, str(default)) or default)
    except Exception:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


LIB_PATH  = os.environ.get("BEETS_LIBRARY", "")


LOG_FILE  = os.environ.get("BEETS_LOG", "/config/beet.log")


HOST      = "0.0.0.0"


PORT      = int(os.environ.get("WEBCONTROL_PORT", "8337"))


PLEX_URL      = os.environ.get("PLEX_URL",   "").rstrip("/")


PLEX_TOKEN    = os.environ.get("PLEX_TOKEN", "")


PLEX_API_TIMEOUT = _env_int("PLEX_API_TIMEOUT", 20, minimum=5)


PLEX_SCAN_TIMEOUT = _env_int("PLEX_SCAN_TIMEOUT", 90, minimum=0)


PLEX_INDEX_TIMEOUT = _env_int("PLEX_INDEX_TIMEOUT", 90, minimum=20)


PLEX_INDEX_PAGE_SIZE = _env_int("PLEX_INDEX_PAGE_SIZE", 500, minimum=100, maximum=2000)


PLEX_INDEX_CACHE_TTL = _env_int("PLEX_INDEX_CACHE_TTL", 600, minimum=0)


PLEX_SYNC_MAX_FALLBACK_SEARCHES = _env_int("PLEX_SYNC_MAX_FALLBACK_SEARCHES", 10, minimum=0)


PLEX_SYNC_MIN_MATCH_RATIO = _env_float("PLEX_SYNC_MIN_MATCH_RATIO", 0.85, minimum=0.0, maximum=1.0)


PLEX_SYNC_MAX_UNMATCHED_REPLACE = _env_int("PLEX_SYNC_MAX_UNMATCHED_REPLACE", 25, minimum=0)


PLEX_PLAYLIST_CHUNK_SIZE = _env_int("PLEX_PLAYLIST_CHUNK_SIZE", 200, minimum=25, maximum=500)


LIDARR_URL    = (
    os.environ.get("LIDARR_URL", "").strip()
    or _config_file_first_line("LIDARR_URL_FILE", "/config/lidarr_url")
    or "http://lidarr:8686"
).rstrip("/")


LIDARR_KEY    = (
    os.environ.get("LIDARR_API_KEY", "").strip()
    or _config_file_first_line("LIDARR_API_KEY_FILE", "/config/lidarr_api_key")
)


WEB_MANAGER_DATA_DIR = Path(os.environ["WEB_MANAGER_DATA_DIR"])


PLAYLIST_STATE_ROOT = WEB_MANAGER_DATA_DIR / "playlists"


PLAYLIST_MANIFESTS_DIR = PLAYLIST_STATE_ROOT / "manifests"


PLAYLIST_JOB_STATE_DIR = Path(os.environ.get("PLAYLIST_JOB_STATE_DIR", "")) or (PLAYLIST_STATE_ROOT / "jobs")


PLAYLIST_EXPORTS_DIR = PLAYLIST_STATE_ROOT / "exports"


PLAYLIST_MEMBERSHIP_DIR = PLAYLIST_STATE_ROOT / "membership"


from backend import config_layers as _config_layers  # noqa: E402  (leaf module, no backend imports)

# Container-side roots (backend.config_layers): absolute paths inside this
# container that must match the Compose mount targets. Never derived from the
# host-side Compose variables (MUSIC_PATH/DOWNLOADS_PATH/...).
_MUSIC_ROOT_SETTING = Path(_config_layers.music_root())
DOWNLOADS_CONTAINER_ROOT = Path(_config_layers.downloads_root())


PLAYLIST_PATH_ROOT_ALIASES = [
    value.strip().replace("\\", "/").rstrip("/")
    for value in (
        os.environ.get("PLAYLIST_PATH_ROOT_ALIASES")
        or os.environ.get("PLEX_MUSIC_ROOT")
        or str(_MUSIC_ROOT_SETTING)
    ).split(",")
    if value.strip()
]


PLAYLIST_AUTO_SYNC_ENABLED = os.environ.get("PLAYLIST_AUTO_SYNC", "1").strip().lower() not in {"0", "false", "no", "off"}


PLAYLIST_AUTO_SYNC_INTERVAL = max(60, int(os.environ.get("PLAYLIST_AUTO_SYNC_INTERVAL", "300") or "300"))


PLAYLIST_MIN_DOWNLOAD_SECONDS = max(0, int(os.environ.get("PLAYLIST_MIN_DOWNLOAD_SECONDS", "45") or "45"))


PLAYLIST_DOWNLOAD_BATCH_SIZE = _env_int("PLAYLIST_DOWNLOAD_BATCH_SIZE", 0, minimum=0)


PLAYLIST_DOWNLOAD_METHODS = os.environ.get("PLAYLIST_DOWNLOAD_METHODS", "slskd,spotiflac,ytdlp,soundcloud")


# Absolute only (config_layers.container_path): an empty or relative value
# falls back to the default instead of meaning the working directory (#269 F-3).
PLAYLIST_DOWNLOAD_ROOT = Path(_config_layers.container_path(
    "PLAYLIST_DOWNLOAD_ROOT",
    str(DOWNLOADS_CONTAINER_ROOT / "music" / "Playlist Downloads"),
))


PLAYLIST_PIPELINE_STATES = {
    "pending", "available", "searching", "downloaded", "waiting_import",
    "importing", "imported", "plex_synced", "failed", "missing",
    "review_required", "removed", "excluded",
}


SLSKD_URL     = os.environ.get("SLSKD_URL",     "http://slskd:5030")


# The downloads/staging mount (DOWNLOADS_ROOT, default /downloads; see
# docs/CONFIGURATION.md) -- the same path the setup check tests.
DOWNLOADS_ROOT = DOWNLOADS_CONTAINER_ROOT


# DOWNLOADS_ROOT as an allowlist entry: empty when it is "/" or overlaps the
# library, so a misconfiguration fails closed (the setup check blocks on it).
DOWNLOADS_ALLOWED_ROOTS = _config_layers.safe_roots("DOWNLOADS_ROOT", [DOWNLOADS_ROOT], _MUSIC_ROOT_SETTING)


# PLAYLIST_DOWNLOAD_ROOT under the same rule: empty when it is "/" or overlaps
# the library, so it is neither allowlisted nor app-managed (#268 S-4).
PLAYLIST_DOWNLOAD_ALLOWED_ROOTS = _config_layers.safe_roots(
    "PLAYLIST_DOWNLOAD_ROOT", [PLAYLIST_DOWNLOAD_ROOT], _MUSIC_ROOT_SETTING)


def validated_downloads_root() -> Path:
    """DOWNLOADS_ROOT for creating download folders, or RuntimeError (with the
    setup-block message) when it is "/" or overlaps the library (#251 F-1)."""
    if not DOWNLOADS_ALLOWED_ROOTS:
        raise RuntimeError(_config_layers.UNSAFE_DOWNLOADS_ROOT_MESSAGE)
    return DOWNLOADS_ALLOWED_ROOTS[0]


DEFAULT_TORRENT_SOURCE_ROOTS = str(DOWNLOADS_ROOT)


TORRENT_SOURCE_ROOTS = _config_layers.safe_roots("TORRENT_SOURCE_ROOTS", (
    value.strip()
    for value in os.environ.get("TORRENT_SOURCE_ROOTS", DEFAULT_TORRENT_SOURCE_ROOTS).split(",")
    if value.strip()
), _MUSIC_ROOT_SETTING)


TORRENT_SOURCE_MOVE_ALLOWED = _env_flag("ALLOW_TORRENT_SOURCE_MOVE", False)


QBIT_URL = (
    os.environ.get("QBITTORRENT_URL", "").strip()
    or os.environ.get("QBIT_URL", "").strip()
    or os.environ.get("QB_URL", "").strip()
    or _config_file_first_line("QBITTORRENT_URL_FILE", "/config/qbittorrent_url")
).rstrip("/")


QBIT_USERNAME = (
    os.environ.get("QBITTORRENT_USERNAME", "").strip()
    or os.environ.get("QBIT_USER", "").strip()
    or os.environ.get("QB_USER", "").strip()
    or _config_file_first_line("QBITTORRENT_USERNAME_FILE", "/config/qbittorrent_username")
)


QBIT_PASSWORD = (
    os.environ.get("QBITTORRENT_PASSWORD", "").strip()
    or os.environ.get("QBIT_PASS", "").strip()
    or os.environ.get("QB_PASS", "").strip()
    or _config_file_first_line("QBITTORRENT_PASSWORD_FILE", "/config/qbittorrent_password")
)


QBIT_CATEGORY = (
    os.environ.get("QBITTORRENT_CATEGORY", "").strip()
    or os.environ.get("QBIT_CATEGORY", "").strip()
    or "music"
)


QBIT_FILTER = (
    os.environ.get("QBITTORRENT_FILTER", "").strip()
    or os.environ.get("QBIT_FILTER", "").strip()
    or "errored"
)


QBIT_PATH_ALIASES = os.environ.get("QBIT_PATH_ALIASES", "")


QBIT_REPAIR_ALLOWED_ROOTS = _config_layers.safe_roots("QBIT_REPAIR_ALLOWED_ROOTS", (
    value.strip()
    for value in os.environ.get(
        "QBIT_REPAIR_ALLOWED_ROOTS",
        str(DOWNLOADS_ROOT),
    ).split(",")
    if value.strip()
), _MUSIC_ROOT_SETTING)


YTDLP_COOKIE_FILE = os.environ.get("YTDLP_COOKIE_FILE", "").strip()


YTDLP_NETRC_FILE = os.environ.get("YTDLP_NETRC_FILE", "/config/.netrc").strip()


YTDLP_REQUIRE_YOUTUBE_AUTH = os.environ.get(
    "YTDLP_REQUIRE_YOUTUBE_AUTH", "0"
).strip().lower() in {"1", "true", "yes", "on"}


YTDLP_PO_PROVIDER_URL = os.environ.get(
    "YTDLP_PO_PROVIDER_URL", "http://bgutil-provider:4416"
).strip().rstrip("/")


YTDLP_ALLOW_BROWSER_COOKIES = os.environ.get(
    "YTDLP_ALLOW_BROWSER_COOKIES", "0"
).strip().lower() in {"1", "true", "yes", "on"}


YTDLP_COOKIES_FROM_BROWSER = (
    os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "")
    or os.environ.get("YTDLP_COOKIES_BROWSER", "")
).strip()


YTDLP_COOKIES_FROM_BROWSER_FALLBACK = os.environ.get(
    "YTDLP_COOKIES_FROM_BROWSER_FALLBACK",
    "",
).strip()


YTDLP_COOKIE_FALLBACKS = (
    Path("/config/yt-dlp/cookies.txt"),
    Path("/config/ytdlp_cookies.txt"),
    Path("/config/cookies.txt"),
)


_YTDLP_COOKIE_REJECTED_FILE = Path("/config/yt-dlp/cookies.rejected.json")


# ── Derived constants ──────────────────────────────────────────────────────────
# The Beets library as mounted in this container -- the single setting for
# where library files live (stock stack: /music, read-only). Configuration
# only: it never authorizes deleting anything (see backend/dedup_authorization).
MUSIC_ROOT   = _MUSIC_ROOT_SETTING


CONFIG_FILE  = "/config/config.yaml"        # main beets config


# Unmatched-draft review metadata (submission text + JSON, no audio) is
# web-manager orchestration state, not authoritative media -- it must not
# live under MUSIC_ROOT, which the web manager neither owns nor (per the
# shipped Compose topology) has mounted at all. /web-manager-data is the
# one path volume every shipped Compose file actually gives this container
# (SEC-002 Wave 8 architecture review).
UNMATCHED_DRAFT_ROOT = Path(os.environ["UNMATCHED_DRAFT_DIR"]) if os.environ.get("UNMATCHED_DRAFT_DIR", "").strip() else (WEB_MANAGER_DATA_DIR / "unmatched_drafts")


METADATA_CACHE_ROOT = Path(os.environ.get("METADATA_CACHE_DIR", "/config/.cache/metadata"))


ARTIST_IMAGE_CACHE_DIR = METADATA_CACHE_ROOT / "artist-images"


RELEASE_ART_CACHE_DIR = METADATA_CACHE_ROOT / "release-art"


ART_REPAIR_LAST_FILE = METADATA_CACHE_ROOT / "art-repair-last.json"


MAINTENANCE_RUNNER_LAST_FILE = METADATA_CACHE_ROOT / "maintenance-runner-last.json"


ALBUM_FOLDER_CLEANUP_LAST_FILE = METADATA_CACHE_ROOT / "album-folder-cleanup-last.json"


ROOT_FOLDER_REPAIR_LAST_FILE = METADATA_CACHE_ROOT / "root-folder-repair-last.json"


RGID_RESOLUTION_STATE_FILE = METADATA_CACHE_ROOT / "rgid-resolution-state.json"


AUDIO_EXT    = frozenset({                  # all audio extensions the app handles
    '.flac', '.mp3', '.m4a', '.ogg', '.opus', '.wav',
    '.aiff', '.aif', '.ape', '.wv', '.mpc', '.dsf', '.dff', '.webm',
})


_ANSI_RE    = re.compile(r'\x1b\[[0-9;]*m')   # strip terminal colour codes


_MB_UUID_RE = re.compile(                      # MusicBrainz UUID validator
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)


def _is_valid_mb_uuid(value: Any) -> bool:
    return bool(_MB_UUID_RE.match(_s(value).strip()))


_YEAR_SFXRE = re.compile(r'\s*[\(\[]\d{4}[\)\]]\s*$')  # trailing year in album name


_DISC_CACHE_TTL = 1800   # seconds before Discogs discography cache expires


_MB_RELEASE_TRACKLIST_CACHE_TTL = 300


_MB_RELEASE_TRACKLIST_DISK_CACHE_TTL = _env_int("MB_RELEASE_TRACKLIST_DISK_CACHE_TTL", 604800, minimum=0)


_MB_RELEASE_TRACKLIST_CACHE_DIR = METADATA_CACHE_ROOT / "mb-release-tracklists"


_MB_RELEASE_TRACKLIST_CACHE: Dict[str, Any] = {}


_MB_RELEASE_TRACKLIST_CACHE_LOCK = threading.Lock()


_MB_TRACK_PREFLIGHT_MATCH_THRESHOLD = 0.82


_MB_TRACK_REPAIR_MATCH_THRESHOLD = 0.72


_UNRESOLVED_TEMPLATE_TOKEN_RE = re.compile(
    r'%\w+\{[^}]+\}'
    r'|\$(?:disc_subfolder|albumartist|album|artist|title|track|disc|year|'
    r'mb_[A-Za-z0-9_]+)'
    r'|\{[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12},\}'
    # Literal text placeholders that should have been replaced with a real UUID:
    r'|\{(?:Album\s+MbId|Track\s+ArtistMbId)\}',
    re.IGNORECASE,
)


# Matches {Album MbId} / {Album Mbid} / {Track ArtistMbId} — literal text placeholders in
# folder/file names where a real UUID was never substituted.
_LITERAL_PLACEHOLDER_RE = re.compile(
    r'\{(?:Album\s+MbId|Track\s+ArtistMbId)\}',
    re.IGNORECASE,
)


_MALFORMED_RELEASE_GROUP_STAMP_RE = re.compile(
    r'\{([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}),\}',
    re.IGNORECASE,
)


# AI confidence thresholds for automated import decisions (use these instead of magic strings)
# Downloads batch import: "high" auto-imports; "medium" auto-imports only when preflight is strong
_AI_BATCH_MIN_CONF        = "high"   # minimum confidence for unconditional auto-import


_AI_BATCH_MEDIUM_RATIO    = 0.80     # preflight match ratio needed to auto-import at medium confidence


# Library repair (light-confirm / missing-track): lower bar once a release is already validated
_AI_REPAIR_MIN_CONF       = "medium" # minimum confidence for auto-repair of existing library albums


_AI_MISSING_MIN_CONF      = "medium" # minimum confidence for missing-track gap-fill repair


_AI_CONF_ORDER            = {"low": 0, "medium": 1, "high": 2}


_AI_USE_CASE_THRESHOLDS   = {
    "fresh_import": {
        "auto_confidence": _AI_BATCH_MIN_CONF,
        "review_confidence": "medium",
        "medium_preflight_ratio": _AI_BATCH_MEDIUM_RATIO,
        "requires_mb_release": True,
    },
    "light_confirm": {
        "auto_confidence": _AI_REPAIR_MIN_CONF,
        "preflight_ratio": 0.60,
        "requires_mb_release": True,
    },
    "missing_track": {
        "auto_confidence": _AI_MISSING_MIN_CONF,
        "preflight_ratio": 0.60,
        "requires_mb_release": True,
    },
}


def _extract_mb_uuid(value: str) -> str:
    """Return the first MusicBrainz UUID from a raw UUID or MB URL."""
    match = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        _s(value),
        re.I,
    )
    return match.group(0).lower() if match else ""


_MISSING_TRACK_FILE_MATCH_SCORE = 0.86


_MISSING_TRACK_TITLE_FUZZY_SCORE = 0.88


def _read_beets_plugin_list(config_path: str = "/config/config.yaml") -> List[str]:
    """Read the configured plugin names without requiring PyYAML."""
    plugins: List[str] = []
    in_plugins = False
    try:
        with open(config_path, encoding="utf-8") as f:
            for raw in f:
                stripped = raw.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if in_plugins and raw[:1].isspace():
                    if stripped.startswith("-"):
                        plugins.extend(p for p in stripped[1:].strip().split() if p)
                    continue
                in_plugins = False
                if stripped.startswith("plugins:"):
                    plugins.extend(p for p in stripped.split(":", 1)[1].strip().split() if p)
                    in_plugins = True
    except Exception:
        pass
    return plugins


_ARTIST_FOLDER_PATH_TEMPLATE = "$albumartist%if{$mb_albumartistid, ($mb_albumartistid),}"


_DEFAULT_ALBUM_PATH_TEMPLATE = _ARTIST_FOLDER_PATH_TEMPLATE + "/$album (%left{$year,4})%if{$mb_releasegroupid, {$mb_releasegroupid$}}/$albumartist - $album - %right{00$track,2} - $title"


_SINGLE_TRACK_PATH_TEMPLATE = _ARTIST_FOLDER_PATH_TEMPLATE + "/$album (%left{$year,4})%if{$mb_releasegroupid, {$mb_releasegroupid$}}/$artist - $album - %right{00$track,2} - $title ($disc)%if{$mb_artistid,{$mb_artistid$}}"


def _sqlite_is_locked_error(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and (
        "database is locked" in str(exc).lower()
        or "database is busy" in str(exc).lower()
    )


def _sqlite_write_retry(label: str, fn, *, log=None, attempts: int = 5):
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not _sqlite_is_locked_error(exc) or attempt >= attempts:
                raise
            message = f"database locked while {label}; retrying {attempt + 1}/{attempts}"
            if isinstance(log, list) and message not in log[-5:]:
                log.append(message)
            time.sleep(min(2.0, 0.2 * (2 ** (attempt - 1))))




EDITABLE_FIELDS = [
    ("title",       "Title"),
    ("artist",      "Artist"),
    ("album",       "Album"),
    ("albumartist", "Album Artist"),
    ("year",        "Year"),
    ("genre",       "Genre"),
    ("track",       "Track #"),
    ("tracktotal",  "Total Tracks"),
    ("disc",        "Disc #"),
    ("disctotal",   "Total Discs"),
    ("label",       "Label"),
    ("comments",    "Comments"),
    ("mb_trackid",  "MB Track ID"),
    ("mb_albumid",  "MB Album ID"),
    ("mb_artistid", "MB Artist ID"),
]


_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|password|secret|authorization|cookie|client[_-]?secret)"
    r"(\s*[:=]\s*)(?:(?:Bearer|Basic)\s+)?(\[REDACTED\]|[^\s,;}\]\"]+)"
)


_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


_REDACTED_SECRET = "[REDACTED]"


def _redact_secret_assignment_match(match: "re.Match[str]") -> str:
    if match.group(3) == _REDACTED_SECRET:
        return match.group(0)
    return f"{match.group(1)}{match.group(2)}{_REDACTED_SECRET}"


# Matches userinfo credentials embedded directly in a URL, e.g.
# "https://user:password@example.test/" -- these don't have a
# "keyword: value" shape so _SECRET_ASSIGNMENT_RE never sees them.
#
# The username segment excludes ":" (unlike the password segment) so the
# two adjacent runs can never both stretch across the same ":" -- that
# exclusion, not a length cap, is what makes the ":" delimiter unambiguous
# and rules out the polynomial-time backtracking CodeQL flagged originally.
# A length cap on top of that would only make matching fail (and therefore
# fail to redact) for any credential longer than the cap, so neither
# segment is length-limited here.
_URL_CREDENTIALS_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s/@:]+:[^\s/@]+@")


def _redact_security_text(value: Any) -> str:
    text = _s(value)
    text = _CONTROL_CHAR_RE.sub("?", text)
    text = _URL_CREDENTIALS_RE.sub(lambda m: f"{m.group(1)}{_REDACTED_SECRET}@", text)
    return _SECRET_ASSIGNMENT_RE.sub(_redact_secret_assignment_match, text)


def _split_mbid_values(value: str) -> List[str]:
    try:
        parts = _split_beets_multi(value)
    except NameError:
        parts = re.split(r'[\0;,]', str(value or ""))
    ids = []
    for part in parts:
        part = (part or "").strip().lower()
        if _MB_UUID_RE.match(part):
            ids.append(part)
    return ids


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except Exception:
        return False


def _split_beets_multi(value: str) -> List[str]:
    """Split beets multi-value artist fields without splitting normal names."""
    text = _s(value).strip()
    if not text:
        return []
    if "\0" in text:
        parts = text.split("\0")
    elif ";" in text:
        parts = text.split(";")
    else:
        parts = [text]
    return [p.strip() for p in parts if p and p.strip()]


def _split_collab_credit(value: str, known_artists: Optional[set] = None) -> List[str]:
    """Conservatively split collaboration credits for display aliases.

    We only split text credits when at least one side already exists as an artist
    in the library. That avoids turning band names like "Earth, Wind & Fire"
    into separate artists.
    """
    text = _normalize_name(_s(value)).strip()
    if not text:
        return []
    parts = [p.strip() for p in re.split(r"\s+(?:&|and|x|X|\+|with)\s+", text) if p.strip()]
    if len(parts) < 2:
        return [text]
    if known_artists:
        known_norm = {a.casefold() for a in known_artists}
        if not any(p.casefold() in known_norm for p in parts):
            return [text]
    return parts


def _path_lexically_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
    except Exception:
        return False


def _path_has_symlink_component_under(path: Path, root: Path, *, include_leaf: bool = True) -> bool:
    try:
        relative = path.relative_to(root)
    except Exception:
        return True
    current = root
    parts = relative.parts if include_leaf else relative.parts[:-1]
    for part in parts:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except Exception:
            return True
    return False


# Unicode punctuation → ASCII equivalents
_UNICODE_NORM = [
    # Hyphens/dashes
    ("‐", "-"),  # ‐ HYPHEN
    ("‑", "-"),  # ‑ NON-BREAKING HYPHEN
    ("‒", "-"),  # ‒ FIGURE DASH
    ("–", "-"),  # – EN DASH
    ("—", "-"),  # — EM DASH
    ("―", "-"),  # ― HORIZONTAL BAR
    ("﹘", "-"),  # ﹘ SMALL EM DASH
    ("﹣", "-"),  # ﹣ SMALL HYPHEN-MINUS
    ("－", "-"),  # － FULLWIDTH HYPHEN-MINUS
    # Quotes
    ("‘", "'"),  # ' LEFT SINGLE QUOTATION MARK
    ("’", "'"),  # ' RIGHT SINGLE QUOTATION MARK
    ("‚", "'"),  # ‚ SINGLE LOW-9 QUOTATION MARK
    ("‛", "'"),  # ‛ SINGLE HIGH-REVERSED-9 QUOTATION MARK
    ("“", '"'),  # " LEFT DOUBLE QUOTATION MARK
    ("”", '"'),  # " RIGHT DOUBLE QUOTATION MARK
    ("„", '"'),  # „ DOUBLE LOW-9 QUOTATION MARK
    # Other punctuation
    ("…", "..."),  # … HORIZONTAL ELLIPSIS
    (" ", " "),    # non-breaking space
    ("⁠", ""),     # WORD JOINER
]


def _normalize_name(s: str) -> str:
    for old, new in _UNICODE_NORM:
        s = s.replace(old, new)
    return s.strip()


def _path_under(path: Path, root: Path) -> bool:
    try:
        raw_p = str(path)
        raw_r = str(root)
        if "\x00" in raw_p or "\x00" in raw_r:
            return False
        rp = Path(os.path.realpath(raw_p))
        rr = Path(os.path.realpath(raw_r))
        return rp == rr or rr in rp.parents
    except Exception:
        return False


def _safe_path_component(value: Any, fallback: str = "untitled") -> str:
    import unicodedata
    text = _s(value).strip() or fallback
    text = text.replace("/", "_").replace("\\", "_")
    text = re.sub(r'[\x00-\x1f<>:"?*|]', "_", text)
    # Unicode format/control characters (bidi overrides such as U+202E, zero-
    # width joiners/spaces, BOM, etc.) aren't covered by the ASCII-only class
    # above but can still make a folder name render deceptively even though
    # it's already fully contained under the trusted root -- strip them too
    # rather than merely relying on path containment for what is ultimately
    # a display-spoofing concern, not a traversal one.
    text = "".join("_" if unicodedata.category(ch) in ("Cf", "Cc") else ch for ch in text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.rstrip(". ")
    return text or fallback


def _same_resolved_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except Exception:
        return str(left) == str(right)


def _safe_beets_error_message(
    ex: Exception, *, bad_request: str, not_found: str, generic: str, unexpected: str,
) -> str:
    """Map a Beets client exception to a short, safe, user-facing message --
    never str(ex), which can carry internal URLs, paths, or transport
    internals (CodeQL: information exposure through an exception). The real
    exception must still be logged server-side by the caller (e.g.
    app.logger.error(..., exc_info=True)); this is only what may reach an
    HTTP response, a job result field, or a job-visible log line."""
    if isinstance(ex, BeetsAuthError):
        return "Authentication with Beets Control Agent failed."
    if isinstance(ex, BeetsBadRequestError):
        return bad_request
    if isinstance(ex, BeetsNotFoundError):
        return not_found
    if isinstance(ex, BeetsUnavailableError):
        return "Beets Control Agent is unavailable."
    if isinstance(ex, BeetsError):
        return generic
    return unexpected


def _safe_inventory_error_message(ex: Exception) -> str:
    """Sanitized message for an artist-folder engine inventory failure --
    see _safe_beets_error_message()."""
    return _safe_beets_error_message(
        ex,
        bad_request="Beets Control Agent rejected the inventory request.",
        not_found="The configured music library was not found by the Beets Engine.",
        generic="Beets Control Agent could not provide the artist-folder inventory.",
        unexpected="Artist-folder inventory failed.",
    )


def _safe_operation_status_error_message(ex: Exception) -> str:
    """Sanitized message for a failed saved-operation transaction status
    lookup (Clean All resume) -- see _safe_beets_error_message()."""
    return _safe_beets_error_message(
        ex,
        bad_request="Beets Control Agent rejected the status request.",
        not_found="Beets Control Agent no longer recognizes the saved operation.",
        generic="Beets Control Agent could not confirm the saved operation's status.",
        unexpected="Could not confirm the saved operation's status.",
    )


def _safe_apply_error_message(ex: Exception) -> str:
    """Sanitized message for a failed/rejected artist-folder reconcile
    Apply call, or a failed status poll following one -- see
    _safe_beets_error_message()."""
    return _safe_beets_error_message(
        ex,
        bad_request="Beets Control Agent rejected the apply request.",
        not_found="Beets Control Agent no longer recognizes this operation.",
        generic="Beets Control Agent could not complete the apply request.",
        unexpected="The apply request failed.",
    )


# ── Playlist helpers ──────────────────────────────────────────────────────────

def _norm(s):
    return re.sub(r"[^\w\s]", "", (s or "").lower()).strip()



@contextmanager
def _db(path=None, *, text_factory=None, row_factory=None):
    """Fail closed: the Web Manager never opens the Beets library database.

    Stock Beets owns musiclibrary.blb; reads go through backend.beets_adapter.
    This name survives only as a patch target for older tests. (It used to
    route to a removed control-agent helper and would have raised NameError.)
    """
    raise RuntimeError("direct Beets database access is not available in the Web Manager")
    yield  # pragma: no cover


# Imported last, after the boot environment above is loaded: backend.beets_adapter
# builds its module-level client from BEETS_WEB_URL / API-key settings at import.
from backend.beets_adapter import (  # noqa: E402
    BeetsAuthError, BeetsBadRequestError, BeetsError, BeetsNotFoundError, BeetsUnavailableError,
)


# ── Process-wide service singletons ──
# One in-memory job store and one transaction store per Web Manager process.
# Every service module shares these objects; app.py re-exports them.
from job_engine import JobStore  # noqa: E402
from backend.transaction_engine import TransactionStore  # noqa: E402

def _durable_jobs_dir() -> Optional[Path]:
    """<data dir>/jobs when Jobs should be durable (ARCH-004), else None.
    BEETS_WEB_DURABLE_JOBS=1/0 forces it on/off."""
    flag = os.environ.get("BEETS_WEB_DURABLE_JOBS", "").strip().lower()
    enabled = flag in ("1", "true", "yes", "on") if flag else _DATA_DIR_EXPLICIT
    return Path(os.environ["WEB_MANAGER_DATA_DIR"]) / "jobs" if enabled else None


jobs = JobStore(_durable_jobs_dir())
transactions = TransactionStore()


# ── Flask application handle ──
# Service modules never import app.py. Code that must run inside an
# application context from a worker thread asks for the registered app.
_FLASK_APP = None


def register_flask_app(flask_app) -> None:
    global _FLASK_APP
    _FLASK_APP = flask_app


def registered_flask_app():
    if _FLASK_APP is None:
        raise RuntimeError("the Flask application has not been registered")
    return _FLASK_APP
