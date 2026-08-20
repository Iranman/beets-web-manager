"""Beets Control Agent — runs inside the beets container.

Provides a secure, restricted internal HTTP control interface to the
authoritative Beets installation and database.
"""

try:
    import fcntl
except ImportError:
    fcntl = None
import base64
import errno
import binascii
import hashlib
import io
import hmac
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

# Portable helper module (stdlib + ffprobe only, no Flask/app.py coupling).
# This file itself is deliberately standalone (no other project imports)
# because it's copied out of the repo and run as a lone script inside the
# engine image -- see Dockerfile.beets, which copies audio_preferences.py
# alongside it as a sibling for exactly this import to resolve there. The
# fallback lets this same file also import cleanly as backend.beets_control_agent
# from a full repo checkout (unit tests, tooling), where audio_preferences
# is a package member instead of a sibling script.
try:
    import audio_preferences
except ImportError:
    from backend import audio_preferences

try:
    import transaction_engine
except ImportError:
    from backend import transaction_engine

try:
    import matching_contract
except ImportError:
    from backend import matching_contract

# Configuration
PORT = int(os.environ.get("BEETS_AGENT_PORT", "8338"))
BEETS_API_TOKEN = os.environ.get("BEETS_API_TOKEN", "")
BEETS_API_TOKEN_MIN_LENGTH = 32
# Known human-instructional placeholder words -- an obviously-weak value like
# "changeme" is non-empty and would otherwise silently authenticate every
# control-agent request if only an emptiness check were enforced.
_PLACEHOLDER_API_TOKENS = {
    "changeme", "change-me", "change_me", "changeit", "example",
    "placeholder", "password", "secret", "token", "test", "default",
    "changeme_required_strong_token", "replace_with_a_generated_strong_token",
    "your-token-here", "your_token_here",
}
BEETSDIR = os.environ.get("BEETSDIR", "/config")
MUSIC_LIBRARY_PATH = os.environ.get("MUSIC_LIBRARY_PATH", "/data/media/music")
DOWNLOAD_PATH = os.environ.get("DOWNLOAD_PATH", "/data/torrents")
# Path objects for the playlist-specific engine endpoints below
# (/playlists/staging/ensure, /playlists/staging/delete-track,
# /playlists/export_m3u), which do their own narrow per-playlist
# containment checks in addition to (not instead of) the broader
# resolve_safe_path()/_allowed_root_paths() role-based containment above.
# MUSIC_ROOT intentionally aliases MUSIC_LIBRARY_PATH rather than reading a
# second, independently-settable env var for the same real directory -- two
# names for one path is how they drift apart. PLAYLIST_DIR/
# PLAYLIST_DOWNLOAD_ROOT use the same env var names and defaults as app.py's
# module-level constants of the same name so one operator-set value governs
# both containers (SEC-002 Wave 9 second final review: these three names
# were referenced by the endpoint code below but never defined anywhere in
# this module, which meant every call to any of the three endpoints raised
# an uncaught NameError in production -- not just a security gap, the
# feature could never have worked at all).
MUSIC_ROOT = Path(MUSIC_LIBRARY_PATH)
PLAYLIST_DIR = Path(os.environ.get("PLAYLIST_DIR", "/data/media/music/playlists"))
PLAYLIST_DOWNLOAD_ROOT = Path(os.environ.get(
    "PLAYLIST_DOWNLOAD_ROOT",
    "/data/torrents/music/Playlist Downloads",
))
LOCK_PATH = os.environ.get("BEETS_LOCK_PATH", os.path.join(BEETSDIR, ".beet_db.lock"))
LIB_PATH = os.path.join(BEETSDIR, "musiclibrary.blb")
BEET_BIN = os.environ.get("BEET_BIN", "beet")

_txn_store = transaction_engine.TransactionStore(
    root=os.environ.get("BEETS_TRANSACTION_DIR") or os.path.join(BEETSDIR, "transactions")
)


def _env_int_clamped(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Read an integer env override, clamped to a safe [minimum, maximum]
    range. An operator setting a negative, zero, or absurdly large value
    (accidentally or via a compromised .env) must not be able to disable
    or effectively remove an image-safety limit -- the clamp always wins,
    it is not merely a validation warning."""
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


PLAYLIST_M3U_MAX_BYTES = _env_int_clamped(
    "BEETS_PLAYLIST_M3U_MAX_BYTES", 2 * 1024 * 1024, minimum=1024, maximum=50 * 1024 * 1024
)
PLAYLIST_M3U_MAX_LINES = _env_int_clamped(
    "BEETS_PLAYLIST_M3U_MAX_LINES", 20000, minimum=1, maximum=500000
)
PLAYLIST_M3U_MAX_LINE_LENGTH = _env_int_clamped(
    "BEETS_PLAYLIST_M3U_MAX_LINE_LENGTH", 4096, minimum=256, maximum=65536
)
PLAYLIST_M3U_MAX_ITEMS = _env_int_clamped(
    "BEETS_PLAYLIST_M3U_MAX_ITEMS", 10000, minimum=1, maximum=200000
)
PLAYLIST_M3U_LIST_MAX_FILES = _env_int_clamped(
    "BEETS_PLAYLIST_M3U_LIST_MAX_FILES", 1000, minimum=1, maximum=50000
)
# Bounds are deliberately generous (real-world cover art is almost always
# well under these) but finite: an unbounded ALBUM_ART_MAX_PIXELS/BYTES
# combined with a highly compressible crafted image is a decompression-bomb
# style resource-exhaustion vector regardless of how the limit was set.
ALBUM_ART_MAX_BYTES = _env_int_clamped(
    "BEETS_ALBUM_ART_MAX_BYTES", 15 * 1024 * 1024, minimum=64 * 1024, maximum=100 * 1024 * 1024
)
ALBUM_ART_MAX_PIXELS = _env_int_clamped(
    "BEETS_ALBUM_ART_MAX_PIXELS", 50_000_000, minimum=100_000, maximum=200_000_000
)
ALBUM_ART_MAX_DIMENSION = _env_int_clamped(
    "BEETS_ALBUM_ART_MAX_DIMENSION", 12_000, minimum=64, maximum=20_000
)
ALBUM_ART_FILENAME = "albumart.jpg"
ALBUM_ART_TRASH_ROOT = Path(os.environ.get("BEETS_ALBUM_ART_TRASH_DIR", os.path.join(BEETSDIR, "album-art-trash")))

ALLOWED_COMMANDS = {
    "import", "update", "write", "move", "modify", "ls", "stats", "fields",
    "mbsync", "fetchart", "embedart", "lastgenre", "lastimport", "alt",
    "version", "config", "check", "remove", "rm", "submit", "mbsubmit"
}

# PATCH /items/<id> and /albums/<id> build SQL from caller-supplied field
# keys. SQL parameters can only bind values, never column names, so every
# accepted key must come from these fixed allowlists.
ITEM_EDITABLE_FIELDS = {
    "title", "artist", "album", "albumartist", "year", "genre", "track",
    "tracktotal", "disc", "disctotal", "label", "comments",
    "mb_trackid", "mb_albumid", "mb_artistid", "mb_releasegroupid",
}
ALBUM_EDITABLE_FIELDS = {
    "album", "albumartist", "year", "genre", "label", "comments",
    "mb_albumid", "mb_albumartistid", "mb_releasegroupid",
}
_ITEM_UPDATE_STATEMENTS = {name: f"UPDATE items SET {name} = ? WHERE id = ?" for name in ITEM_EDITABLE_FIELDS}
_ALBUM_UPDATE_STATEMENTS = {name: f"UPDATE albums SET {name} = ? WHERE id = ?" for name in ALBUM_EDITABLE_FIELDS}
_TAG_WRITE_FIELDS = {
    "title", "artist", "album", "albumartist", "year", "month", "day",
    "track", "tracktotal", "disc", "disctotal", "genre", "comments",
    "lyrics", "composer", "mb_trackid", "mb_albumid", "mb_artistid",
    "mb_albumartistid", "mb_releasegroupid",
}


def _invalid_field_names(fields: dict, allowed: set) -> list:
    """Return caller-supplied field names not present in the allowlist."""
    return [k for k in fields.keys() if k not in allowed]


def _validated_update_assignments(fields: Any, allowed_statements: dict[str, str]) -> tuple[list[tuple[str, Any]], list[str]]:
    if not isinstance(fields, dict):
        return [], ["fields"]
    assignments: list[tuple[str, Any]] = []
    invalid: list[str] = []
    for key, value in fields.items():
        statement = allowed_statements.get(str(key))
        if statement is None:
            invalid.append(str(key))
            continue
        assignments.append((statement, value))
    return assignments, invalid


def _strip_sql_comments(text: str) -> str:
    """Remove SQL comments with a linear scan."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        two = text[i:i + 2]
        if two == "/*":
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        if two == "--":
            end = text.find("\n", i + 2)
            i = n if end == -1 else end
            continue
        out.append(text[i])
        i += 1
    return "".join(out)
JOBS = {}
JOBS_LOCK = threading.Lock()

# Commands that require a specific plugin to have actually finished loading in the
# authoritative Beets engine before they can run, and the response returned when
# that plugin did not load. Keyed by ALLOWED_COMMANDS entry.
_COMMAND_CAPABILITY_REQUIREMENTS = {
    "submit": ("chroma", "AcoustID submission unavailable", "Chroma is configured but the submit command is not registered"),
    "mbsubmit": ("mbsubmit", "MusicBrainz submission unavailable", "mbsubmit command is not registered"),
}


_BEETS_PLUGIN_FAILURE_PATTERNS = (
    "error loading plugin",
    "failed loading plugin",
    "failed to load plugin",
    "no module named",
    "modulenotfounderror",
    "importerror",
    "traceback",
)


def _redact_agent_status_text(text: str) -> str:
    """Small defense-in-depth redactor for diagnostic text returned over the
    internal status API. The web manager redacts again before browser output,
    but the agent should not deliberately serialize obvious secrets either.
    """
    redacted = str(text or "")
    redacted = re.sub(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)\S+", r"\1[redacted]", redacted)
    redacted = re.sub(r"(?i)(cookie\s*:\s*).*$", r"\1[redacted]", redacted, flags=re.MULTILINE)
    redacted = re.sub(
        r"(?i)(api[_-]?key|token|password|secret|user[_-]?token)(\s*[:=]\s*)([^\s,'\"&]+)",
        r"\1\2[redacted]",
        redacted,
    )
    redacted = re.sub(r"https?://([^:/\s]+):([^@/\s]+)@", r"https://\1:[redacted]@", redacted)
    return redacted


def _diagnostic_failure_lines(text: str) -> list[str]:
    failures: list[str] = []
    for line in str(text or "").splitlines():
        lower = line.lower()
        if any(pattern in lower for pattern in _BEETS_PLUGIN_FAILURE_PATTERNS):
            failures.append(_redact_agent_status_text(line.strip()))
    return failures[:12]


# ---------------------------------------------------------------------------
# config.yaml access (BUG-1 fix)
#
# Beets' config.yaml lives on the *engine* container's own /config mount
# (BEETSDIR) -- the web-manager container has never had this file since the
# two-service architecture cutover (2026-07-28); it has its own, separate
# /config volume for its own app_settings.json/env state. app.py's
# /api/config route used to read/write BEETSDIR-relative paths directly off
# its own (wrong) local filesystem, which always 500'd in the real deployed
# topology. These agent-side endpoints are the authoritative access point;
# app.py now proxies through BeetsClient instead of touching the file
# itself. Redaction of secret-looking lines happens in app.py (existing,
# already-reviewed _redact_config_content()) once the raw content crosses
# the trusted agent/web-manager boundary -- not duplicated here.
# ---------------------------------------------------------------------------
_AGENT_CONFIG_PATH = os.path.join(BEETSDIR, "config.yaml")
_AGENT_CONFIG_BAK_PATH = os.path.join(BEETSDIR, "config.yaml.bak")
_AGENT_CONFIG_CONTENT_MAX_BYTES = _env_int_clamped(
    "BEETS_CONFIG_CONTENT_MAX_BYTES",
    1024 * 1024,
    minimum=1024,
    maximum=8 * 1024 * 1024,
)
_AGENT_CONFIG_REQUEST_MAX_BYTES = _AGENT_CONFIG_CONTENT_MAX_BYTES + 4096
_AGENT_CONFIG_LOCK = threading.Lock()


def _read_agent_config_file() -> dict[str, Any]:
    """Read config.yaml from BEETSDIR. Raises OSError on any I/O failure --
    callers translate that into a structured, stable-coded HTTP response
    rather than leaking a raw traceback."""
    with _AGENT_CONFIG_LOCK:
        with open(_AGENT_CONFIG_PATH, "r", encoding="utf-8") as fh:
            content = fh.read()
        has_backup = os.path.exists(_AGENT_CONFIG_BAK_PATH)
        backup_ts = os.path.getmtime(_AGENT_CONFIG_BAK_PATH) if has_backup else None
    return {"content": content, "has_backup": has_backup, "backup_ts": backup_ts}


def _write_agent_config_file(content: str) -> dict[str, Any]:
    """Back up the existing config.yaml (if any), then overwrite it.
    Raises OSError on failure; callers translate to a structured response."""
    if len(content.encode("utf-8")) > _AGENT_CONFIG_CONTENT_MAX_BYTES:
        raise ValueError("config content is too large")
    with _AGENT_CONFIG_LOCK:
        if os.path.exists(_AGENT_CONFIG_PATH):
            shutil.copy2(_AGENT_CONFIG_PATH, _AGENT_CONFIG_BAK_PATH)
        tmp_path = _AGENT_CONFIG_PATH + f".tmp.{uuid.uuid4().hex}"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp_path, _AGENT_CONFIG_PATH)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        backed_up = os.path.exists(_AGENT_CONFIG_BAK_PATH)
    return {"ok": True, "backed_up": backed_up}


def _revert_agent_config_file() -> dict[str, Any]:
    """Restore config.yaml from its most recent backup. Raises
    FileNotFoundError if no backup exists, OSError on other I/O failure."""
    with _AGENT_CONFIG_LOCK:
        if not os.path.exists(_AGENT_CONFIG_BAK_PATH):
            raise FileNotFoundError(_AGENT_CONFIG_BAK_PATH)
        shutil.copy2(_AGENT_CONFIG_BAK_PATH, _AGENT_CONFIG_PATH)
    return {"ok": True}


def _simple_yaml_scalar(raw: str) -> str:
    value = str(raw or "").strip()
    if "#" in value:
        value = value.split("#", 1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value.strip()


def _parse_beets_config_summary() -> dict[str, Any]:
    summary: dict[str, Any] = {
        "plugins": [],
        "pluginpath": [],
        "replaygain_backend": "",
        "replaygain_command": "",
        "discogs_token_configured": False,
        "listenbrainz_token_configured": False,
    }
    config_path = os.path.join(BEETSDIR, "config.yaml")
    try:
        lines = open(config_path, "r", encoding="utf-8").read().splitlines()
    except Exception:
        return summary

    section = ""
    list_key = ""
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()
        if indent == 0 and ":" in stripped:
            key, rest = stripped.split(":", 1)
            section = key.strip()
            list_key = ""
            value = _simple_yaml_scalar(rest)
            if section == "plugins" and value:
                summary["plugins"].extend(value.replace(",", " ").split())
            elif section == "pluginpath" and value:
                summary["pluginpath"].append(value)
            continue
        if stripped.startswith("-"):
            value = _simple_yaml_scalar(stripped[1:])
            if section == "plugins" and value:
                summary["plugins"].extend(value.replace(",", " ").split())
            elif section == "pluginpath" and value:
                summary["pluginpath"].append(value)
            continue
        if section == "replaygain" and stripped.startswith("backend:"):
            summary["replaygain_backend"] = _simple_yaml_scalar(stripped.split(":", 1)[1])
        elif section == "replaygain" and stripped.startswith("command:"):
            summary["replaygain_command"] = _simple_yaml_scalar(stripped.split(":", 1)[1])
        elif section == "discogs" and stripped.startswith("user_token:"):
            summary["discogs_token_configured"] = bool(_simple_yaml_scalar(stripped.split(":", 1)[1]))
        elif section == "listenbrainz" and stripped.startswith("token:"):
            summary["listenbrainz_token_configured"] = bool(_simple_yaml_scalar(stripped.split(":", 1)[1]))

    summary["plugins"] = list(dict.fromkeys(summary["plugins"]))
    summary["pluginpath"] = list(dict.fromkeys(summary["pluginpath"]))
    return summary


def _beet_version_snapshot(timeout: int = 5) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "available": False,
        "version": "",
        "loaded_plugins": [],
        "plugin_failures": [],
        "returncode": None,
        "timed_out": False,
        "error": "",
    }
    try:
        res = subprocess.run([BEET_BIN, "version"], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        snapshot["available"] = True
        snapshot["timed_out"] = True
        snapshot["error"] = "Beets version probe timed out."
        return snapshot
    except Exception:
        snapshot["error"] = "Beets version probe failed."
        return snapshot

    snapshot["available"] = True
    snapshot["returncode"] = res.returncode
    stdout = res.stdout if isinstance(res.stdout, str) else ""
    stderr = res.stderr if isinstance(res.stderr, str) else ""
    combined = "\n".join([stdout, stderr])
    snapshot["plugin_failures"] = _diagnostic_failure_lines(combined)

    version_lines = combined.strip().splitlines()
    for line in version_lines:
        if line.lower().startswith("beets version"):
            snapshot["version"] = line.strip()
            break
    if not snapshot["version"] and version_lines:
        snapshot["version"] = version_lines[0].strip()

    for line in stdout.splitlines():
        if line.startswith("plugins:"):
            loaded = [name.strip() for name in line[len("plugins:"):].split(",") if name.strip()]
            snapshot["loaded_plugins"] = list(dict.fromkeys(loaded))
            break

    if snapshot["plugin_failures"]:
        snapshot["error"] = snapshot["plugin_failures"][0]
    elif res.returncode != 0:
        snapshot["error"] = "Beets version probe failed."
    return snapshot


_BEET_VERSION_CACHE_LOCK = threading.Lock()
_BEET_VERSION_CACHE: Optional[dict[str, Any]] = None
_BEET_VERSION_CACHE_TS: float = 0.0
_BEET_VERSION_CACHE_TTL_SECONDS = float(os.environ.get("BEETS_VERSION_CACHE_TTL_SECONDS", "30"))
# How long a previously-good snapshot may still be served (diagnostics_fresh
# =False) after a refresh attempt times out/fails, e.g. transient host load.
# Bounded so a genuinely, permanently broken `beet` binary is eventually
# correctly reported as failed rather than "good" forever.
_BEET_VERSION_CACHE_MAX_STALE_SECONDS = float(os.environ.get("BEETS_VERSION_CACHE_MAX_STALE_SECONDS", "300"))
# Per-probe subprocess timeout. Was hardcoded to 5s; production evidence
# (v0.1.11 TrueNAS rollout) showed `beet version` taking up to ~29s under
# real host memory pressure vs. ~0.5s warm. Widened modestly -- caching
# below is what actually bounds how often this cost is paid, not a larger
# timeout alone -- to reduce false-timeout probes without letting one
# uncached cold request block for the full production worst case.
_BEET_VERSION_PROBE_TIMEOUT_SECONDS = int(os.environ.get("BEETS_VERSION_PROBE_TIMEOUT_SECONDS", "12"))


def _cached_beet_version_snapshot(*, force: bool = False) -> dict[str, Any]:
    """Single-flight cached wrapper around _beet_version_snapshot() (BUG-3,
    v0.1.12).

    `beet version` used to run fresh on *every* /status request and every
    capability-gated /commands/execute or /jobs/create call
    (get_loaded_beet_plugins() -> require_command_capability()). Combined
    with the control agent's previous single-threaded HTTPServer, a single
    slow probe blocked every other request behind it, including Docker's
    own cheap /health liveness check -- see run_agent()'s
    ThreadingHTTPServer comment for that half of BUG-3's fix. This wrapper
    caches the last snapshot for _BEET_VERSION_CACHE_TTL_SECONDS and
    single-flights concurrent refreshes under one lock (the whole
    check-build-store sequence runs under the same lock, mirroring
    routes_setup.py's /api/setup/status cache design) so N simultaneous
    callers past the TTL trigger exactly one subprocess launch, not N.

    Only a genuine probe failure (subprocess timeout, or `beet` not
    launchable at all) is treated as "the probe failed" -- a completed run
    that reports plugin_failures or a non-zero returncode is still real,
    current ground truth and is always cached/returned directly, never
    discarded in favor of stale data. On an actual failure, a still-recent
    known-good cached snapshot is returned again (annotated
    diagnostics_fresh=False) instead of being replaced by an empty/failed
    result -- this was BUG-3's actual observed symptom: plugin_loader_ok
    =false and "0 plugins loaded" reported during a transient slow probe,
    despite ~20/20 plugins genuinely loading against a warm probe moments
    later. Adds diagnostics_fresh / diagnostics_cache_age_seconds /
    diagnostics_refresh_error to the snapshot dict; callers that only read
    the pre-existing keys (available/version/loaded_plugins/...) are
    unaffected.
    """
    global _BEET_VERSION_CACHE, _BEET_VERSION_CACHE_TS
    with _BEET_VERSION_CACHE_LOCK:
        now = time.monotonic()
        if not force and _BEET_VERSION_CACHE is not None and (now - _BEET_VERSION_CACHE_TS) < _BEET_VERSION_CACHE_TTL_SECONDS:
            result = dict(_BEET_VERSION_CACHE)
            result["diagnostics_fresh"] = True
            result["diagnostics_cache_age_seconds"] = round(now - _BEET_VERSION_CACHE_TS, 2)
            result["diagnostics_refresh_error"] = ""
            return result

        fresh = _beet_version_snapshot(timeout=_BEET_VERSION_PROBE_TIMEOUT_SECONDS)
        probe_failed = bool(fresh.get("timed_out")) or not fresh.get("available")

        if not probe_failed:
            _BEET_VERSION_CACHE = fresh
            _BEET_VERSION_CACHE_TS = now
            result = dict(fresh)
            result["diagnostics_fresh"] = True
            result["diagnostics_cache_age_seconds"] = 0.0
            result["diagnostics_refresh_error"] = ""
            return result

        refresh_error = fresh.get("error") or ("Beets version probe timed out." if fresh.get("timed_out") else "Beets version probe failed.")
        if _BEET_VERSION_CACHE is not None and (now - _BEET_VERSION_CACHE_TS) < _BEET_VERSION_CACHE_MAX_STALE_SECONDS:
            result = dict(_BEET_VERSION_CACHE)
            result["diagnostics_fresh"] = False
            result["diagnostics_cache_age_seconds"] = round(now - _BEET_VERSION_CACHE_TS, 2)
            result["diagnostics_refresh_error"] = refresh_error
            return result

        result = dict(fresh)
        result["diagnostics_fresh"] = False
        result["diagnostics_cache_age_seconds"] = None
        result["diagnostics_refresh_error"] = refresh_error
        return result


def get_loaded_beet_plugins() -> set:
    """Return the set of plugins Beets actually finished loading, parsed from
    `beet version`'s own "plugins: a, b, c" line.

    Unlike a Python import check (e.g. importlib.util.find_spec), this only
    includes plugins that survived Beets' own load() step, so a plugin that is
    importable on disk but silently fails to initialize (as beetsplug.chroma
    can, even with fpcalc/pyacoustid present) is correctly excluded. Reads
    the cached snapshot (BUG-3) rather than launching its own subprocess --
    this is called on every capability-gated /commands/execute and
    /jobs/create request, which used to mean a fresh `beet version` launch
    per command dispatch.
    """
    return set(_cached_beet_version_snapshot().get("loaded_plugins") or [])


def _agent_status_payload(*, force_refresh: bool = False) -> dict[str, Any]:
    snapshot = _cached_beet_version_snapshot(force=force_refresh)
    config_summary = _parse_beets_config_summary()
    loaded_plugins = set(snapshot.get("loaded_plugins") or [])
    configured_plugins = set(config_summary.get("plugins") or [])

    import importlib.util
    fpcalc_path = shutil.which("fpcalc") or ""
    ffmpeg_path = shutil.which("ffmpeg") or ""
    pyacoustid_available = importlib.util.find_spec("acoustid") is not None

    def _path_status(path: str, *, require_writable: bool = False) -> dict[str, Any]:
        exists = os.path.exists(path)
        is_dir = os.path.isdir(path)
        readable = bool(exists and os.access(path, os.R_OK))
        writable = bool(exists and os.access(path, os.W_OK))
        return {
            "path": path,
            "exists": exists,
            "is_dir": is_dir,
            "readable": readable,
            "writable": writable,
            "ok": bool(exists and readable and (writable if require_writable else True)),
        }

    config_path = os.path.join(BEETSDIR, "config.yaml")
    remote_paths = {
        "config": _path_status(BEETSDIR, require_writable=True),
        "music_library": _path_status(MUSIC_LIBRARY_PATH),
        "downloads": _path_status(DOWNLOAD_PATH, require_writable=True),
        "beets_config": {"path": config_path, "exists": os.path.exists(config_path)},
    }

    discpath_installed = (
        os.path.exists("/opt/beets-web-manager-agent/beetsplug/discpath.py")
        or os.path.exists(os.path.join(BEETSDIR, "beetsplug", "discpath.py"))
    )

    # NOTE: "musicbrainz" is deliberately excluded from this set. Unlike
    # every other name here, it is not a togglable Beets plugin -- Beets has
    # no "musicbrainz" entry in its `plugins:` enable list, since MusicBrainz
    # autotagging is core, always-on Beets functionality (configured only
    # via the `musicbrainz:` config section, not plugin enable/disable).
    # `name in loaded_plugins` would always be False for it in a real
    # deployment, permanently misreporting a working MusicBrainz connection
    # as an unloaded/disabled plugin. See plugin_flags["musicbrainz"] below
    # for its actual (loader-health-based, not membership-based) status.
    plugin_names = {
        "chroma", "discogs", "discpath", "fetchart", "lastgenre", "listenbrainz",
        "mbsubmit", "mbsync", "replaygain",
    }
    plugin_flags = {name: name in loaded_plugins for name in sorted(plugin_names)}

    commands = {
        "submit": {
            "available": "chroma" in loaded_plugins,
            "registered": "chroma" in loaded_plugins,
            "required_plugin": "chroma",
        },
        "mbsubmit": {
            "available": "mbsubmit" in loaded_plugins,
            "registered": "mbsubmit" in loaded_plugins,
            "required_plugin": "mbsubmit",
        },
    }

    capabilities = {
        "fingerprinting": {
            "available": bool(fpcalc_path and "chroma" in loaded_plugins),
            "fpcalc_available": bool(fpcalc_path),
            "chroma_loaded": "chroma" in loaded_plugins,
        },
        "acoustid_lookup": {
            "available": bool(fpcalc_path and pyacoustid_available and "chroma" in loaded_plugins),
            "fpcalc_available": bool(fpcalc_path),
            "pyacoustid_available": pyacoustid_available,
            "chroma_loaded": "chroma" in loaded_plugins,
        },
        "acoustid_submission": {
            "available": bool(commands["submit"]["available"]),
            "command": "submit",
            "chroma_loaded": "chroma" in loaded_plugins,
        },
        "musicbrainz_submission": {
            "available": bool(commands["mbsubmit"]["available"]),
            "command": "mbsubmit",
            "mbsubmit_loaded": "mbsubmit" in loaded_plugins,
        },
        "fetchart": {
            "configured": "fetchart" in configured_plugins,
            "loaded": "fetchart" in loaded_plugins,
            "available": "fetchart" in loaded_plugins,
        },
        "replaygain": {
            "configured": "replaygain" in configured_plugins,
            "loaded": "replaygain" in loaded_plugins,
            "available": bool("replaygain" in loaded_plugins and (config_summary.get("replaygain_backend") != "ffmpeg" or ffmpeg_path)),
            "backend": config_summary.get("replaygain_backend") or "",
            "command": config_summary.get("replaygain_command") or "",
            "ffmpeg_available": bool(ffmpeg_path),
        },
        "lastgenre": {"configured": "lastgenre" in configured_plugins, "loaded": "lastgenre" in loaded_plugins},
        "discogs": {"configured": "discogs" in configured_plugins, "loaded": "discogs" in loaded_plugins},
        "listenbrainz": {"configured": "listenbrainz" in configured_plugins, "loaded": "listenbrainz" in loaded_plugins},
        "discpath": {"configured": "discpath" in configured_plugins, "loaded": "discpath" in loaded_plugins, "installed": discpath_installed},
    }

    plugin_loader_ok = bool(
        snapshot.get("available")
        and snapshot.get("returncode") == 0
        and not snapshot.get("timed_out")
        and not snapshot.get("plugin_failures")
    )
    # Core capability, not plugin membership: MusicBrainz is available
    # whenever `beet` itself runs and the plugin loader completed cleanly.
    plugin_flags["musicbrainz"] = bool(snapshot.get("available")) and plugin_loader_ok

    return {
        "status": "ok",
        "service": "beets-control-agent",
        "agent_version": "1.0.0",
        "engine_release": os.environ.get("BEETS_WEB_MANAGER_VERSION") or os.environ.get("BEETS_ENGINE_RELEASE") or "0.1.10",
        "engine_revision": os.environ.get("BEETS_ENGINE_REVISION") or os.environ.get("VCS_REF") or "",
        "control_api_version": 1,
        "beets_version": snapshot.get("version") or "",
        "beet_available": bool(snapshot.get("available")),
        "db_healthy": os.path.exists(LIB_PATH) and os.access(LIB_PATH, os.R_OK),
        "config_healthy": os.path.exists(os.path.join(BEETSDIR, "config.yaml")),
        "fpcalc_available": bool(fpcalc_path),
        "fpcalc_path": fpcalc_path,
        "ffmpeg_available": bool(ffmpeg_path),
        "ffmpeg_path": ffmpeg_path,
        "pyacoustid_available": pyacoustid_available,
        "plugins": plugin_flags,
        "configured_plugins": list(config_summary.get("plugins") or []),
        "loaded_plugins": list(snapshot.get("loaded_plugins") or []),
        "installed_plugins": {"discpath": discpath_installed},
        "pluginpath": list(config_summary.get("pluginpath") or []),
        "plugin_failures": list(snapshot.get("plugin_failures") or []),
        "plugin_loader_ok": plugin_loader_ok,
        "plugin_loader_returncode": snapshot.get("returncode"),
        "plugin_loader_timed_out": bool(snapshot.get("timed_out")),
        "plugin_loader_error": _redact_agent_status_text(str(snapshot.get("error") or "")),
        # BUG-3 (v0.1.12): whether the fields above came from a fresh probe
        # this call, or a still-recent cached one (diagnostics_fresh=False)
        # served because the most recent refresh attempt itself failed/timed
        # out -- never because the cache degrades known-good data on its own.
        "diagnostics_fresh": bool(snapshot.get("diagnostics_fresh", True)),
        "diagnostics_cache_age_seconds": snapshot.get("diagnostics_cache_age_seconds"),
        "diagnostics_refresh_error": _redact_agent_status_text(str(snapshot.get("diagnostics_refresh_error") or "")),
        "capabilities": capabilities,
        "commands": commands,
        "paths": remote_paths,
        "replaygain_backend": config_summary.get("replaygain_backend") or "",
        "replaygain_command": config_summary.get("replaygain_command") or "",
        "discogs_token_configured": bool(os.environ.get("DISCOGS_TOKEN") or os.environ.get("DISCOGS_USER_TOKEN") or config_summary.get("discogs_token_configured")),
        "listenbrainz_token_configured": bool(os.environ.get("LISTENBRAINZ_TOKEN") or config_summary.get("listenbrainz_token_configured")),
        "beetsdir": BEETSDIR,
    }


def require_command_capability(command: str) -> Optional[dict]:
    """Return an error dict if `command` requires a runtime plugin capability that
    is not currently loaded in the authoritative Beets engine, else None.

    This is deliberately independent of the web-manager's own readiness check:
    a caller hitting this agent's endpoints directly (bypassing the web-manager
    route) must still be blocked before any subprocess or job is created.
    """
    requirement = _COMMAND_CAPABILITY_REQUIREMENTS.get(command)
    if requirement is None:
        return None
    required_plugin, error_msg, reason = requirement
    if required_plugin in get_loaded_beet_plugins():
        return None
    return {"error": error_msg, "reason": reason}


def _decode_untrusted_path(raw_path: str) -> Optional[str]:
    decoded = raw_path
    for _ in range(5):
        try:
            next_decoded = urllib.parse.unquote(decoded)
        except Exception:
            return None
        if next_decoded == decoded:
            return decoded
        decoded = next_decoded
    return None


def _allowed_root_paths(allowed_types: list = None) -> list[str]:
    all_roots = {
        "config": [os.path.abspath(BEETSDIR), "/config"],
        "music": [os.path.abspath(MUSIC_LIBRARY_PATH), "/data/media/music"],
        "staging": [os.path.abspath(DOWNLOAD_PATH), "/data/torrents"],
        "tmp": ["/tmp", tempfile.gettempdir()],
    }
    roots_to_check: list[str] = []
    if allowed_types:
        for root_type in allowed_types:
            roots_to_check.extend(all_roots.get(root_type, []))
    else:
        for root_list in all_roots.values():
            roots_to_check.extend(root_list)
    return roots_to_check


def _path_is_within(candidate: str, root: str) -> bool:
    try:
        real_candidate = os.path.realpath(candidate)
        real_root = os.path.realpath(root)
        return os.path.commonpath([real_candidate, real_root]) == real_root
    except Exception:
        return False


class UnsafePathError(ValueError):
    """Raised by resolve_safe_path() when the input cannot be trusted."""


def resolve_safe_path(
    path: object,
    allowed_types: list = None,
    *,
    require_exists: bool = False,
    expected_type: str = None,
) -> Path:
    """Validate untrusted path input and return the one trusted, canonical
    Path callers may use for filesystem operations.

    Raises UnsafePathError for every unsafe condition instead of returning a
    bool/None -- a caller that checked a boolean and then still operated on
    the original tainted string left a gap (and a static-analysis blind
    spot) for mixed encodings, symlink components, and prefix-collision
    paths. There is no code path that produces a Path from untrusted input
    without it having passed every check below.
    """
    if not path or not isinstance(path, str):
        raise UnsafePathError("path must be a non-empty string")
    if "\x00" in path or "\\" in path:
        raise UnsafePathError("path contains a null byte or backslash")
    # The absolute-path requirement must hold for the RAW string, not just its
    # decoded form: canonicalization below deliberately uses `path` (the raw
    # string), so a value whose raw form is relative but whose fully-decoded
    # form happens to be absolute (e.g. "%2Fdata%2Fmusic%2Ffile.flac") must
    # not be allowed to slip past on the strength of its decoded copy alone --
    # os.path.abspath() would silently anchor a relative raw string to the
    # process's cwd instead of raising here.
    if not path.startswith("/"):
        raise UnsafePathError("path must be absolute")

    decoded = _decode_untrusted_path(path)
    if decoded is None:
        raise UnsafePathError("path failed to decode")
    if "\x00" in decoded or "\\" in decoded:
        raise UnsafePathError("decoded path contains a null byte or backslash")
    if not decoded.startswith("/"):
        raise UnsafePathError("path must be absolute")

    parts = decoded.split("/")
    if ".." in parts or "." in parts:
        raise UnsafePathError("path contains a traversal segment")

    # Canonicalize from the ORIGINAL raw path, not the decoded form above:
    # decoding is used only to detect a hidden traversal/separator/null-byte
    # attack encoded into the request. Every path endpoint here receives
    # values in a JSON body, never URL-encoded by any real caller, so a
    # legitimate literal filename that merely contains a percent-sign byte
    # sequence (e.g. "50%20off.flac") must resolve to itself -- it must not
    # be silently rewritten to a different string ("50 off.flac") before it
    # reaches the filesystem sink. The traversal/separator/null checks above
    # already ran against the fully-decoded form, so nothing unsafe can slip
    # through by canonicalizing the original string here instead.
    abs_path = os.path.abspath(path)
    real_path = os.path.realpath(abs_path)
    trusted: Optional[Path] = None
    for root in _allowed_root_paths(allowed_types):
        if _path_is_within(real_path, root):
            trusted = Path(real_path)
            break
    if trusted is None:
        raise UnsafePathError("path is outside every allowed root")

    if require_exists and not trusted.exists():
        raise UnsafePathError("path does not exist")
    if expected_type == "file" and trusted.exists() and not trusted.is_file():
        raise UnsafePathError("path is not a regular file")
    if expected_type == "dir" and trusted.exists() and not trusted.is_dir():
        raise UnsafePathError("path is not a directory")

    return trusted


# SEC-002 Wave 8 ARCH-003: which trusted roots each import-source-inspection
# operation may resolve against. The caller (web manager) never chooses its
# own root -- it only names the operation it's performing, and the engine
# decides what that operation is allowed to touch. "reimport" covers both
# already-in-library untracked audio and staged/downloaded audio, matching
# reimport_disk()'s two pre-existing scenarios; "ai_batch_discovery" is
# staging-primary but also allows "music" since the AI Batch Import UI is an
# explicit, authenticated, free-text operator field that has always allowed
# pointing at the library itself.
IMPORT_SOURCE_OPERATIONS: dict[str, list[str]] = {
    "reimport": ["music", "staging"],
    "ai_batch_discovery": ["music", "staging"],
}

IMPORT_INSPECT_MAX_ENTRIES_SCANNED = _env_int_clamped(
    "BEETS_IMPORT_INSPECT_MAX_ENTRIES", 20_000, minimum=100, maximum=200_000
)
IMPORT_INSPECT_MAX_AUDIO_FILES = _env_int_clamped(
    "BEETS_IMPORT_INSPECT_MAX_AUDIO_FILES", 500, minimum=1, maximum=5_000
)


def _import_source_audio_extensions() -> set[str]:
    return set(audio_preferences._AUDIO_EXT_TO_FORMAT.keys())


PLAYLIST_IMPORT_MAX_REQUEST_BYTES = _env_int_clamped(
    "BEETS_PLAYLIST_IMPORT_MAX_REQUEST_BYTES", 256_000, minimum=16_384, maximum=2_000_000
)
PLAYLIST_IMPORT_MAX_TRACKS = _env_int_clamped(
    "BEETS_PLAYLIST_IMPORT_MAX_TRACKS", 100, minimum=1, maximum=500
)
PLAYLIST_STAGING_LIST_FILES_MAX = _env_int_clamped(
    "BEETS_PLAYLIST_STAGING_LIST_FILES_MAX", 500, minimum=1, maximum=5000
)
# Matches the web-manager's PLAYLIST_MIN_DOWNLOAD_SECONDS acceptance-gate
# default (app.py) so a track that is accepted on download is not
# immediately re-flagged as a too-short "preview" by the quality scanner.
PLAYLIST_QUALITY_PREVIEW_THRESHOLD_SECONDS = _env_int_clamped(
    "PLAYLIST_MIN_DOWNLOAD_SECONDS", 45, minimum=0, maximum=3600
)
PLAYLIST_IMPORT_MAX_TIMEOUT_SECONDS = _env_int_clamped(
    "BEETS_PLAYLIST_IMPORT_MAX_TIMEOUT_SECONDS", 900, minimum=60, maximum=3600
)
PLAYLIST_IMPORT_PER_TRACK_TIMEOUT_SECONDS = _env_int_clamped(
    "BEETS_PLAYLIST_IMPORT_PER_TRACK_TIMEOUT_SECONDS", 120, minimum=10, maximum=300
)
_PLAYLIST_ID_RE = re.compile(r"^pl_[0-9a-f]{32}$")
_PLAYLIST_KEY_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,160}$")
_PLAYLIST_OPERATION_ID_RE = re.compile(r"^pl-import-[a-zA-Z0-9_.-]{1,180}$")
_PLAYLIST_TRACK_ID_RE = re.compile(r"^[^\x00/\\]{1,240}$")
_PLAYLIST_OPERATION_STORAGE_KEY_RE = re.compile(r"^op_[0-9a-f]{32}$")
_PLAYLIST_IMPORT_OPERATIONS_ROOT_NAME = ".playlist-import-operations"
_PLAYLIST_IMPORT_INDEX_NAME = "index.json"


def _bounded_text(value: object, *, max_len: int) -> str:
    text = str(value or "").strip()
    if len(text) > max_len:
        raise ValueError("too_long")
    if "\x00" in text:
        raise ValueError("invalid")
    return text


def _valid_playlist_key(value: str) -> bool:
    return bool(value and _PLAYLIST_KEY_RE.fullmatch(value) and ".." not in value)


def _valid_playlist_id(value: str) -> bool:
    return bool(value and _PLAYLIST_ID_RE.fullmatch(value))


def _valid_playlist_import_operation_id(value: str) -> bool:
    return bool(value and _PLAYLIST_OPERATION_ID_RE.fullmatch(value) and ".." not in value)


def _valid_playlist_import_track_id(value: str) -> bool:
    return bool(value and _PLAYLIST_TRACK_ID_RE.fullmatch(value))


def _playlist_import_new_operation_storage_key() -> str:
    return f"op_{uuid.uuid4().hex}"


def _valid_playlist_import_storage_key(value: str) -> bool:
    return bool(value and _PLAYLIST_OPERATION_STORAGE_KEY_RE.fullmatch(value))


def _safe_playlist_import_error(status: str) -> str:
    messages = {
        "invalid_path": "Staged track path is invalid.",
        "cross_playlist_target": "Staged track is outside this playlist's staging directory.",
        "staged_track_symlink": "Staged track path contains a symlink.",
        "staged_track_not_audio": "Staged track is not an allowed audio file.",
        "staged_track_missing": "Staged track is missing.",
        "invalid_track_id": "Track identifier is invalid.",
        "identity_mismatch": "Staged track metadata does not match the expected track.",
        "review_required": "Staged track identity could not be confirmed by fingerprint; review is required.",
        "identity_verification_unavailable": "Deterministic audio identity verification is currently unavailable.",
        "tag_write_failed": "Could not safely prepare staged track metadata for import.",
        "import_failed": "Beets import failed.",
        "operation_state_corrupt": "Playlist import operation state is corrupt.",
    }
    return messages.get(status, "Playlist import could not continue.")


def _playlist_import_operations_root() -> Path:
    return resolve_safe_path(
        str(PLAYLIST_DOWNLOAD_ROOT / _PLAYLIST_IMPORT_OPERATIONS_ROOT_NAME),
        ["staging"],
    )


def _playlist_import_operations_index_path(operations_root: Path) -> Path:
    return resolve_safe_path(str(operations_root / _PLAYLIST_IMPORT_INDEX_NAME), ["staging"])


def _playlist_import_read_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise UnsafePathError("operation state is corrupt") from exc
    if not isinstance(data, dict):
        raise UnsafePathError("operation state is corrupt")
    return data


def _playlist_import_write_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.parent / f".{state_path.name}.{uuid.uuid4().hex}.tmp"
    payload = json.dumps(state, sort_keys=True, indent=2, default=_json_default)
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, state_path)
    try:
        dir_fd = os.open(str(state_path.parent), os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        pass


def _playlist_item_belongs_to_playlist(playlist_key: str, item_id: int) -> bool:
    """Authorization gate for /playlists/place-imported: confirm item_id was
    actually imported as part of a playlist-import operation recorded under
    this playlist_key, before permitting any mutation or move.

    Without this check, any caller who can reach the engine's HTTP API with
    a bare item_id could rewrite metadata and move the file for ANY item in
    the library, not just items that belong to a playlist import -- there
    was previously no association at all between the item_id in the
    request body and the playlist_key also in the request body. Reuses the
    existing playlist-import operation-state index (SEC-002 Wave 12/13)
    rather than adding a new tracking mechanism.
    """
    if not _valid_playlist_key(playlist_key) or item_id <= 0:
        return False
    try:
        operations_root = _playlist_import_operations_root()
        index_path = _playlist_import_operations_index_path(operations_root)
        index_state = _playlist_import_read_state(index_path)
    except (UnsafePathError, OSError, ValueError):
        return False
    operations = index_state.get("operations") if isinstance(index_state.get("operations"), dict) else {}
    for op_state in operations.values():
        if not isinstance(op_state, dict):
            continue
        if str(op_state.get("playlist_key") or "") != playlist_key:
            continue
        tracks = op_state.get("tracks") if isinstance(op_state.get("tracks"), dict) else {}
        for track_row in tracks.values():
            if not isinstance(track_row, dict):
                continue
            if track_row.get("status") not in ("imported", "already_imported"):
                continue
            try:
                if int(track_row.get("item_id") or 0) == item_id:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def _read_engine_media_tags(path: Path) -> dict[str, Any]:
    try:
        from beets.mediafile import MediaFile
        mf = MediaFile(path)
        return {
            "title": str(getattr(mf, "title", "") or ""),
            "artist": str(getattr(mf, "artist", "") or ""),
            "album": str(getattr(mf, "album", "") or ""),
            "albumartist": str(getattr(mf, "albumartist", "") or ""),
            "year": int(getattr(mf, "year", 0) or 0),
            "mb_trackid": str(getattr(mf, "mb_trackid", "") or ""),
        }
    except Exception:
        return {}


def _norm_import_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _import_text_similarity(expected: str, candidate: str) -> float:
    """Bounded, conservative text similarity in [0, 1]. Deliberately simple
    (exact match after normalization, or one containing the other) rather
    than importing app.py's richer fuzzy scorers -- this module is a
    standalone engine script with no app.py/Flask coupling (see the
    audio_preferences import fallback above)."""
    a, b = _norm_import_text(expected), _norm_import_text(candidate)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85
    return 0.0


_PLAYLIST_ACOUSTID_LOOKUP_LOCK = threading.Lock()
_PLAYLIST_ACOUSTID_NEXT_LOOKUP_AT = 0.0
try:
    _PLAYLIST_ACOUSTID_MIN_INTERVAL_SECONDS = max(
        0.0, float(os.environ.get("ACOUSTID_MIN_INTERVAL_SECONDS", "0.35") or "0.35")
    )
except Exception:
    _PLAYLIST_ACOUSTID_MIN_INTERVAL_SECONDS = 0.35


def _playlist_import_fpcalc_available() -> bool:
    return bool(shutil.which("fpcalc") or Path("/usr/bin/fpcalc").exists())


def _engine_acoustid_lookup(path: Path) -> Optional[list[dict[str, Any]]]:
    """Run fpcalc + an AcoustID API lookup entirely engine-side (mirrors
    helpers_mb.py's _acoustid_lookup() output shape/policy -- AcoustID
    fingerprint evidence is this project's primary deterministic
    track-identity verification, tags are supporting evidence only).
    Returns None on a lookup-level failure (network/API error, distinct
    from "ran fine, no candidates found" which returns []), so the caller
    can tell "verification failed to run" apart from "verification ran and
    found nothing" -- both must fail closed, but for different reasons."""
    fpcalc = shutil.which("fpcalc") or "/usr/bin/fpcalc"
    if not Path(fpcalc).exists():
        return None
    try:
        r = subprocess.run([fpcalc, "-json", str(path)], capture_output=True, text=True, timeout=30)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        fp_data = json.loads(r.stdout)
        duration = int(fp_data.get("duration") or 0)
        fingerprint = str(fp_data.get("fingerprint") or "").strip()
        if not fingerprint or duration < 5:
            return None
    except Exception:
        return None

    aid_key = os.environ.get("ACOUSTID_API_KEY") or "8XaBELgH"
    params = urllib.parse.urlencode({
        "client": aid_key,
        "meta": "recordings releases releasegroups",
        "duration": duration,
        "fingerprint": fingerprint,
        "format": "json",
    })
    req = urllib.request.Request(
        f"https://api.acoustid.org/v2/lookup?{params}",
        headers={"User-Agent": "BeetsWebManager-Engine/1.0"},
    )
    data: dict[str, Any] = {}
    got_response = False
    for attempt in range(2):
        try:
            with _PLAYLIST_ACOUSTID_LOOKUP_LOCK:
                global _PLAYLIST_ACOUSTID_NEXT_LOOKUP_AT
                now = time.monotonic()
                if _PLAYLIST_ACOUSTID_NEXT_LOOKUP_AT > now:
                    time.sleep(_PLAYLIST_ACOUSTID_NEXT_LOOKUP_AT - now)
                _PLAYLIST_ACOUSTID_NEXT_LOOKUP_AT = time.monotonic() + _PLAYLIST_ACOUSTID_MIN_INTERVAL_SECONDS
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            got_response = True
            break
        except Exception:
            if attempt >= 1:
                return None
            time.sleep(1.0)
    if not got_response or data.get("status") != "ok":
        return None

    out: list[dict[str, Any]] = []
    seen_mbids: set = set()
    for result in (data.get("results") or [])[:5]:
        try:
            confidence = int(round((result.get("score") or 0) * 100))
        except Exception:
            confidence = 0
        acoustid_id = str(result.get("id") or "")
        for rec in (result.get("recordings") or [])[:3]:
            mb_id = str(rec.get("id") or "")
            if not mb_id or mb_id in seen_mbids:
                continue
            seen_mbids.add(mb_id)
            out.append({
                "score": confidence,
                "acoustid_id": acoustid_id,
                "mb_trackid": mb_id,
                "title": str(rec.get("title") or ""),
                "artist": " / ".join(a.get("name", "") for a in (rec.get("artists") or [])),
            })
    return out


def _playlist_import_identity_ok(path: Path, item: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """Deterministic pre-import identity gate. AcoustID fingerprint
    evidence is required to authorize import; embedded tags are used only
    as a fast-fail conflict pre-check (tags are mutable, attacker/user-
    controlled metadata -- matching tags alone never authorizes import,
    only fingerprint evidence does). This is evaluated on the *original*
    staged source, before any tag is written onto it, so the evidence is
    never self-fulfilling: the engine cannot verify a value it wrote
    itself (SEC-002 Wave 12 second final review)."""
    expected_mbid = str(item.get("mb_trackid") or "").strip()
    expected_artist = str(item.get("artist") or "").strip()
    expected_title = str(item.get("title") or "").strip()

    tags = _read_engine_media_tags(path)
    actual_mbid = str(tags.get("mb_trackid") or "").strip()
    if expected_mbid and actual_mbid and actual_mbid != expected_mbid:
        return False, "identity_mismatch", {"mb_trackid": actual_mbid}
    if expected_title and tags.get("title"):
        if _norm_import_text(tags.get("title")) != _norm_import_text(expected_title):
            return False, "identity_mismatch", {"title": tags.get("title", "")}
    if expected_artist and tags.get("artist"):
        if _norm_import_text(tags.get("artist")) != _norm_import_text(expected_artist):
            return False, "identity_mismatch", {"artist": tags.get("artist", "")}

    if not _playlist_import_fpcalc_available():
        return False, "identity_verification_unavailable", {"reason": "fpcalc_unavailable"}

    candidates = _engine_acoustid_lookup(path)
    if candidates is None:
        return False, "identity_verification_unavailable", {"reason": "acoustid_lookup_failed"}
    if not candidates:
        return False, "review_required", {"reason": "no_acoustid_result"}

    confirmed: Optional[dict[str, Any]] = None
    for cand in candidates:
        cand_mbid = str(cand.get("mb_trackid") or "").strip().lower()
        mbid_ok = bool(expected_mbid and cand_mbid and cand_mbid == expected_mbid.lower())
        title_ok = bool(expected_title and _import_text_similarity(expected_title, cand.get("title", "")) >= 0.78)
        artist_ok = bool(
            not expected_artist
            or _import_text_similarity(expected_artist, cand.get("artist", "")) >= 0.6
        )
        if mbid_ok or (title_ok and artist_ok):
            confirmed = cand
            break

    if not confirmed:
        return False, "identity_mismatch", {
            "reason": "acoustid_no_matching_candidate",
            "top_candidate_mb_trackid": candidates[0].get("mb_trackid", ""),
        }

    return True, "verified", {
        "mb_trackid": confirmed.get("mb_trackid", ""),
        "acoustid_id": confirmed.get("acoustid_id", ""),
        "acoustid_score": confirmed.get("score", 0),
    }


def _verify_imported_track_in_library(item: dict[str, Any]) -> Optional[int]:
    """Confirm the imported track landed in the Beets library and return its
    item id (or None if it can't be confirmed). The returned id is
    persisted into the playlist import operation state so later playlist
    operations (e.g. /playlists/place-imported) can verify an item_id
    actually belongs to the playlist operation that imported it, instead of
    trusting a bare caller-supplied item_id."""
    mb_trackid = str(item.get("mb_trackid") or "").strip()
    if not mb_trackid:
        return None
    if not os.path.exists(LIB_PATH):
        return None
    try:
        conn = sqlite3.connect(LIB_PATH)
        try:
            row = conn.execute(
                "SELECT id FROM items WHERE mb_trackid = ? LIMIT 1",
                (mb_trackid,),
            ).fetchone()
            return int(row[0]) if row else None
        finally:
            conn.close()
    except Exception:
        return None


def _import_source_signature(entries: list[dict[str, Any]]) -> str:
    """A cheap staleness fingerprint (NOT recording identity -- see the
    caller). Two inspections of an unchanged source produce the same
    signature; any added/removed/resized/retimestamped file changes it."""
    parts = sorted(f"{e['relative_path']}:{e['size']}:{e['mtime_ns']}" for e in entries)
    return hashlib.sha256("|".join(parts).encode("utf-8", "surrogatepass")).hexdigest()


def _read_media_tags_best_effort(safe_file_path: Path) -> dict[str, Any]:
    """Read tags from an already-validated (resolve_safe_path()'d) file path.

    Shared by /tags/read and inspect_import_source() -- the latter reuses
    this so the web manager can get title/album/mb_* evidence for a folder
    matching decision from the SAME bounded, already-authorized engine-side
    directory walk, instead of needing a second local media read of its own
    (which it cannot safely do -- it has no /data mount).
    """
    tags: dict[str, Any] = {}
    try:
        from beets.mediafile import MediaFile
        mf = MediaFile(safe_file_path)
        tags = {
            "title": mf.title,
            "artist": mf.artist,
            "album": mf.album,
            "albumartist": mf.albumartist,
            "year": mf.year,
            "track": mf.track,
            "tracktotal": mf.tracktotal,
            "disc": mf.disc,
            "disctotal": mf.disctotal,
            "genre": mf.genre,
            "mb_trackid": mf.mb_trackid,
            "mb_albumid": mf.mb_albumid,
            "mb_artistid": mf.mb_artistid,
            "mb_albumartistid": mf.mb_albumartistid,
            "mb_releasegroupid": getattr(mf, "mb_releasegroupid", None),
            "length": getattr(mf, "length", None),
        }
    except Exception:
        try:
            import mutagen
            f = mutagen.File(safe_file_path, easy=True)
            if f is not None:
                tags = {
                    "title": (f.get("title") or [""])[0],
                    "artist": (f.get("artist") or [""])[0],
                    "album": (f.get("album") or [""])[0],
                    "albumartist": (f.get("albumartist") or [""])[0],
                    "year": (f.get("date") or [0])[0],
                    "track": (f.get("tracknumber") or [0])[0],
                    "genre": (f.get("genre") or [""])[0],
                }
        except Exception:
            tags = {}
    return tags


def inspect_import_source(source_path: object, operation: str) -> dict[str, Any]:
    """Engine-authoritative validation + bounded audio inventory for an
    import/reimport source. Returns a plain result dict (never raises for
    caller-facing conditions) so /imports/source/inspect can map it to a
    stable JSON response without leaking exception internals.

    This is the fix for the ARCH-003 gap: the web manager has no local
    filesystem access to MUSIC_LIBRARY_PATH/DOWNLOAD_PATH in the shipped
    Compose topology, so it cannot validate existence, reject symlinks, or
    inspect audio properties itself. The engine can and must do all of
    that, since it is the only place those roots are actually mounted.
    """
    allowed_types = IMPORT_SOURCE_OPERATIONS.get(operation)
    if allowed_types is None:
        return {"ok": False, "error_code": "invalid_operation"}

    try:
        trusted = resolve_safe_path(
            source_path, allowed_types, require_exists=True, expected_type="dir",
        )
    except UnsafePathError:
        return {"ok": False, "error_code": "invalid_path"}

    # Root-self is refused for the same reason the web-manager-side
    # validator refuses it: "import the entire library/staging root as one
    # album" is never a legitimate request and would be a uniquely
    # dangerous one to silently accept.
    for root_type in allowed_types:
        for candidate_root in _allowed_root_paths([root_type]):
            try:
                if trusted == Path(os.path.realpath(candidate_root)):
                    return {"ok": False, "error_code": "root_self_rejected"}
            except Exception:
                continue

    entries: list[dict[str, Any]] = []
    audio_exts = _import_source_audio_extensions()
    scanned = 0
    truncated_scan = False
    truncated_audio = False
    try:
        stack = [trusted]
        while stack:
            current = stack.pop()
            try:
                children = sorted(current.iterdir())
            except Exception:
                continue
            for child in children:
                if scanned >= IMPORT_INSPECT_MAX_ENTRIES_SCANNED:
                    truncated_scan = True
                    break
                scanned += 1
                try:
                    # Do not follow symlinked directories out of the
                    # trusted root -- resolve_safe_path() already fully
                    # dereferenced the root itself, but nothing below stops
                    # a symlink planted *inside* the tree from pointing
                    # elsewhere.
                    if child.is_symlink():
                        continue
                    if child.is_dir():
                        stack.append(child)
                        continue
                    if not child.is_file():
                        continue
                except Exception:
                    continue
                if child.suffix.lower() not in audio_exts:
                    continue
                if len(entries) >= IMPORT_INSPECT_MAX_AUDIO_FILES:
                    truncated_audio = True
                    continue
                try:
                    rel = str(child.relative_to(trusted))
                    st = child.stat()
                except Exception:
                    continue
                properties = audio_preferences.inspect_audio_file(str(child))
                # Merge in tag evidence (title/album/mb_*) from the same
                # already-validated path, using the same bounded walk --
                # this is what lets a web-manager-side folder/import
                # matching decision get real title/album/mb_releasegroupid
                # evidence without the web manager reading the file itself.
                try:
                    tag_evidence = _read_media_tags_best_effort(child)
                except Exception:
                    tag_evidence = {}
                if tag_evidence:
                    properties = {**properties, **{k: v for k, v in tag_evidence.items() if v not in (None, "")}}
                entries.append({
                    "relative_path": rel,
                    "size": st.st_size,
                    "mtime_ns": st.st_mtime_ns,
                    "properties": properties,
                })
            if truncated_scan:
                break
    except Exception:
        return {"ok": False, "error_code": "inspection_failed"}

    entries.sort(key=lambda e: e["relative_path"])
    return {
        "ok": True,
        "canonical_path": str(trusted),
        "type": "dir",
        "audio_count": len(entries),
        "audio_files": entries,
        "truncated_scan": truncated_scan,
        "truncated_audio": truncated_audio,
        "source_signature": _import_source_signature(entries),
    }


# SEC-002 Wave 8 ARCH-003: Discovery, Preservation, and Atomic Reimport

IMPORT_DISCOVER_MAX_DIRS_VISITED = _env_int_clamped(
    "BEETS_IMPORT_DISCOVER_MAX_DIRS", 10_000, minimum=10, maximum=50_000
)
IMPORT_DISCOVER_MAX_FILES_EXAMINED = _env_int_clamped(
    "BEETS_IMPORT_DISCOVER_MAX_FILES", 50_000, minimum=100, maximum=200_000
)
IMPORT_DISCOVER_MAX_CANDIDATES = _env_int_clamped(
    "BEETS_IMPORT_DISCOVER_MAX_CANDIDATES", 100, minimum=1, maximum=500
)


def discover_import_sources(
    source_path: object,
    operation: str = "ai_batch_discovery",
    cursor: str = None,
    limits: dict = None,
) -> dict[str, Any]:
    op = str(operation or "ai_batch_discovery")
    allowed_types = IMPORT_SOURCE_OPERATIONS.get(op)
    if allowed_types is None:
        return {"ok": False, "error_code": "invalid_operation"}

    try:
        trusted = resolve_safe_path(
            source_path, allowed_types, require_exists=True, expected_type="dir"
        )
    except UnsafePathError:
        return {"ok": False, "error_code": "invalid_path"}

    # Deliberately no root-self rejection here, unlike inspect_import_source()/
    # preserve_import_source()/reimport_source_atomic(): those operate on the
    # assumption that source_path IS one candidate album, where "the whole
    # trusted root" is a nonsensical/dangerous input. Discovery's entire
    # purpose is the opposite -- scanning a whole root for multiple candidate
    # subdirectories -- and its default, most common caller
    # (start_ai_batch_import()'s scan_path defaults to "/data/torrents/music",
    # i.e. the trusted staging root itself) passes the root directly. Rejecting
    # root-self here would break AI Batch Import's primary use case outright.

    req_limits = limits if isinstance(limits, dict) else {}
    max_dirs = _env_int_clamped(
        "BEETS_DISCOVER_DIRS",
        int(req_limits.get("max_dirs", IMPORT_DISCOVER_MAX_DIRS_VISITED)),
        minimum=10,
        maximum=50_000,
    )
    max_files = _env_int_clamped(
        "BEETS_DISCOVER_FILES",
        int(req_limits.get("max_files", IMPORT_DISCOVER_MAX_FILES_EXAMINED)),
        minimum=100,
        maximum=200_000,
    )
    max_candidates = _env_int_clamped(
        "BEETS_DISCOVER_CANDIDATES",
        int(req_limits.get("max_candidates", IMPORT_DISCOVER_MAX_CANDIDATES)),
        minimum=1,
        maximum=500,
    )

    # The cursor encodes the EXACT remaining DFS stack (as relative paths)
    # at the moment traversal paused, not a lexicographic "resume after this
    # name" marker. A lexicographic marker looks equivalent to DFS order but
    # is not: DFS visits a directory's children (name "parent/child") before
    # any lexicographically-later sibling, but "/" (0x2F) sorts ABOVE space,
    # "-", and "." (0x20/0x2D/0x2E) -- extremely common characters in real
    # album/artist folder names ("Artist - Album") -- so a sibling named
    # "Parent-suffix" can sort *before* "Parent/child" as a string while
    # still being visited *after* it in DFS order. Resuming by skipping
    # every rel_dir <= the lexicographic marker then silently skips that
    # sibling's whole subtree: a real, silent candidate-loss bug, not a
    # theoretical one. Storing the actual stack sidesteps the comparison
    # entirely -- resuming is just "restore the stack and keep going",
    # which is correct by construction for a stack-based DFS.
    #
    # canonical_root and operation are bound into the cursor so a cursor
    # from a different root/operation (stale reuse, or a client trying to
    # splice cursors across calls) is rejected outright rather than mixing
    # partial results from two different traversals. This is not
    # cryptographic -- source_path/operation are independently
    # re-validated via resolve_safe_path() on every call regardless of
    # cursor content, so a tampered cursor can never grant access to a
    # path the caller wasn't already authorized to discover; the binding
    # exists purely for traversal correctness, not as a security boundary.
    resume_stack_rel: list[str] | None = None
    if cursor and isinstance(cursor, str):
        try:
            decoded_json = json.loads(base64.b64decode(cursor.encode("utf-8")).decode("utf-8"))
        except Exception:
            return {"ok": False, "error_code": "invalid_cursor"}
        if not isinstance(decoded_json, dict) or decoded_json.get("v") != 1:
            return {"ok": False, "error_code": "invalid_cursor"}
        if decoded_json.get("root") != str(trusted) or decoded_json.get("op") != op:
            return {"ok": False, "error_code": "invalid_cursor"}
        raw_stack = decoded_json.get("stack")
        if not isinstance(raw_stack, list) or not all(isinstance(p, str) for p in raw_stack):
            return {"ok": False, "error_code": "invalid_cursor"}
        resume_stack_rel = raw_stack

    audio_exts = _import_source_audio_extensions()
    candidates: list[dict[str, Any]] = []
    dirs_visited = 0
    files_examined = 0
    limit_reached = False

    if resume_stack_rel is not None:
        stack = [trusted / rel if rel != "." else trusted for rel in resume_stack_rel]
    else:
        stack = [trusted]

    while stack and len(candidates) < max_candidates and not limit_reached:
        current = stack.pop()
        dirs_visited += 1

        if dirs_visited > max_dirs:
            # Restore the directory this iteration popped (it was never
            # processed) before capturing the continuation stack below.
            stack.append(current)
            limit_reached = True
            break

        try:
            children = sorted(current.iterdir())
        except Exception:
            continue

        # A directory is processed all-or-nothing: if examining its full
        # child list would exceed the per-page file budget, defer the
        # *whole* directory to the next page (push it back untouched)
        # rather than pushing only some of its subdirectories onto the
        # stack and silently dropping the rest -- that would either
        # duplicate or lose entries once paired with the stack-based
        # continuation cursor above.
        if files_examined + len(children) > max_files:
            stack.append(current)
            limit_reached = True
            break

        dir_audio_files: list[tuple[Path, os.stat_result, str]] = []

        for child in children:
            files_examined += 1

            try:
                if child.is_symlink():
                    continue
                if child.is_dir():
                    stack.append(child)
                    continue
                if not child.is_file():
                    continue
            except Exception:
                continue

            if child.suffix.lower() in audio_exts:
                try:
                    rel_file = str(child.relative_to(trusted))
                    st = child.stat()
                    dir_audio_files.append((child, st, rel_file))
                except Exception:
                    continue

        if dir_audio_files:
            dir_entries = [
                {"relative_path": rel_file, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
                for _, st, rel_file in dir_audio_files
            ]
            cand_rel = str(current.relative_to(trusted)) if current != trusted else "."

            mb_ids = {}
            try:
                first_audio = str(dir_audio_files[0][0])
                from beets.mediafile import MediaFile
                mf = MediaFile(first_audio)
                if getattr(mf, "mb_albumid", None):
                    mb_ids["mb_albumid"] = mf.mb_albumid
                if getattr(mf, "mb_releasegroupid", None):
                    mb_ids["mb_releasegroupid"] = mf.mb_releasegroupid
                if getattr(mf, "mb_artistid", None):
                    mb_ids["mb_artistid"] = mf.mb_artistid
            except Exception:
                pass

            candidates.append({
                "canonical_path": str(current),
                "relative_path": cand_rel,
                "audio_file_count": len(dir_audio_files),
                "source_signature": _import_source_signature(dir_entries),
                "musicbrainz_ids": mb_ids,
                "relative_files": [rel for _, _, rel in dir_audio_files],
            })

    continuation_token = None
    if stack:
        stack_rel = [
            str(p.relative_to(trusted)) if p != trusted else "."
            for p in stack
        ]
        token_payload = {"v": 1, "root": str(trusted), "op": op, "stack": stack_rel}
        continuation_token = base64.b64encode(json.dumps(token_payload).encode("utf-8")).decode("utf-8")

    return {
        "ok": True,
        "canonical_root": str(trusted),
        "candidates": candidates,
        "complete": not (limit_reached or stack),
        "limit_reached": limit_reached,
        "continuation": continuation_token,
        "dirs_visited": dirs_visited,
        "files_examined": files_examined,
    }


def preserve_import_source(
    source_path: object,
    expected_source_signature: str = None,
    plan_id: str = None,
) -> dict[str, Any]:
    # staging-only, deliberately: this exists to protect torrent/download
    # sources from Beets mutating them during import, not to make a
    # protective copy of already-organized library content -- library
    # content never needs this operation, so it is not in scope for it
    # (least privilege; SEC-002 Wave 8 review).
    try:
        trusted_src = resolve_safe_path(
            source_path, ["staging"], require_exists=True, expected_type="dir"
        )
    except UnsafePathError:
        return {"ok": False, "error_code": "invalid_path"}

    for candidate_root in _allowed_root_paths(["staging"]):
        try:
            if trusted_src == Path(os.path.realpath(candidate_root)):
                return {"ok": False, "error_code": "root_self_rejected"}
        except Exception:
            continue

    inspect_res = inspect_import_source(str(trusted_src), "reimport")
    if not inspect_res.get("ok"):
        return {"ok": False, "error_code": inspect_res.get("error_code", "inspection_failed")}

    curr_signature = inspect_res.get("source_signature", "")
    if expected_source_signature and curr_signature != expected_source_signature:
        return {
            "ok": False,
            "error_code": "stale_source",
            "message": "Source signature changed since inspection",
        }

    src_hash = hashlib.sha256(str(trusted_src).encode("utf-8")).hexdigest()[:12]
    plan_part = re.sub(r"[^a-zA-Z0-9_-]", "_", str(plan_id or ""))[:20]
    folder_name = f"preserve_{plan_part}_{src_hash}" if plan_part else f"preserve_{src_hash}"

    preserved_root = Path(os.path.realpath(os.path.join(DOWNLOAD_PATH, ".preserved_staging")))
    dest_path = preserved_root / folder_name

    try:
        safe_dest = resolve_safe_path(str(dest_path), ["staging"])
    except UnsafePathError:
        return {"ok": False, "error_code": "invalid_destination"}

    lock_file = acquire_os_lock(read_only=False)
    try:
        if safe_dest.exists():
            if safe_dest.is_symlink():
                return {"ok": False, "error_code": "collision", "message": "Destination is a symlink"}

            dest_inspect = inspect_import_source(str(safe_dest), "reimport")
            if dest_inspect.get("ok") and dest_inspect.get("source_signature") == curr_signature:
                return {
                    "ok": True,
                    "already_preserved": True,
                    "preserved_path": str(safe_dest),
                    "source_signature": curr_signature,
                    "audio_count": dest_inspect.get("audio_count", 0),
                }
            else:
                return {"ok": False, "error_code": "collision", "message": "Destination exists with conflicting contents"}

        audio_files = inspect_res.get("audio_files") or []
        total_bytes = sum(int(f.get("size", 0)) for f in audio_files)

        preserved_root.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(str(preserved_root))
        if usage.free < total_bytes + (10 * 1024 * 1024):
            return {"ok": False, "error_code": "insufficient_space", "message": "Not enough free space for preservation copy"}

        safe_dest.mkdir(parents=True, exist_ok=True)
        copied_count = 0
        for entry in audio_files:
            rel = entry["relative_path"]
            src_file = trusted_src / rel
            dst_file = safe_dest / rel

            if src_file.is_symlink():
                continue

            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src_file), str(dst_file))
            copied_count += 1

        dest_inspect = inspect_import_source(str(safe_dest), "reimport")
        if not dest_inspect.get("ok") or dest_inspect.get("source_signature") != curr_signature:
            if safe_dest.exists():
                shutil.rmtree(str(safe_dest))
            return {"ok": False, "error_code": "copy_failed", "message": "Preservation copy post-verification failed"}

        return {
            "ok": True,
            "already_preserved": False,
            "preserved_path": str(safe_dest),
            "source_signature": curr_signature,
            "copied_files_count": copied_count,
            "audio_count": dest_inspect.get("audio_count", 0),
        }
    except Exception:
        if safe_dest.exists():
            try:
                shutil.rmtree(str(safe_dest))
            except Exception:
                pass
        return {"ok": False, "error_code": "copy_failed", "message": "Preservation copy failed"}
    finally:
        release_os_lock(lock_file)


def verify_deterministic_identity(
    source_inspect: dict[str, Any],
    expected_identity: dict[str, Any],
) -> dict[str, Any]:
    """Fail-closed by design: every path that cannot positively confirm the
    source belongs to the expected identity returns review_required, never
    ok: True. Missing embedded MusicBrainz tags, a missing
    expected_identity argument, and a DB verification error are all
    *absence of evidence*, not evidence of a match -- silently treating
    them as success would let path containment plus an unverified caller
    claim stand in for actual identity proof (SEC-002 Wave 8, Claude's
    independent review of the original implementation, which did exactly
    that)."""
    if not expected_identity or not isinstance(expected_identity, dict):
        return {"ok": False, "error_code": "review_required", "message": "No expected identity supplied to verify against"}

    target_mb_albumid = str(expected_identity.get("mb_albumid") or "").strip().lower()
    target_mb_rgid = str(expected_identity.get("mb_releasegroupid") or "").strip().lower()
    target_existing_album_id = expected_identity.get("existing_album_id")
    target_track_count = expected_identity.get("track_count")

    audio_files = source_inspect.get("audio_files") or []
    source_audio_count = len(audio_files)

    if target_track_count and isinstance(target_track_count, int) and target_track_count > 0:
        if source_audio_count != target_track_count:
            return {
                "ok": False,
                "error_code": "identity_mismatch",
                "message": f"Track count mismatch (source has {source_audio_count}, target expects {target_track_count})",
            }

    source_album_mbids: set[str] = set()
    source_rg_mbids: set[str] = set()
    for entry in audio_files:
        props = entry.get("properties") or {}
        if props.get("mb_albumid"):
            source_album_mbids.add(str(props["mb_albumid"]).strip().lower())
        if props.get("mb_releasegroupid"):
            source_rg_mbids.add(str(props["mb_releasegroupid"]).strip().lower())

    if target_mb_albumid:
        if not source_album_mbids:
            return {
                "ok": False,
                "error_code": "review_required",
                "message": "Source audio has no embedded MusicBrainz album ID to verify against the target",
            }
        if target_mb_albumid not in source_album_mbids:
            return {
                "ok": False,
                "error_code": "identity_mismatch",
                "message": f"Source embedded MusicBrainz album ID conflicts with target {target_mb_albumid}",
            }
    if target_mb_rgid:
        if not source_rg_mbids:
            return {
                "ok": False,
                "error_code": "review_required",
                "message": "Source audio has no embedded MusicBrainz Release Group ID to verify against the target",
            }
        if target_mb_rgid not in source_rg_mbids:
            return {
                "ok": False,
                "error_code": "identity_mismatch",
                "message": f"Source embedded MusicBrainz Release Group ID conflicts with target {target_mb_rgid}",
            }

    if target_existing_album_id:
        try:
            with sqlite3.connect(LIB_PATH, timeout=10) as con:
                con.row_factory = sqlite3.Row
                cur = con.cursor()
                row = cur.execute("SELECT id, mb_albumid FROM albums WHERE id = ?", (target_existing_album_id,)).fetchone()
                item_rows = cur.execute("SELECT path FROM items WHERE album_id = ?", (target_existing_album_id,)).fetchall()
        except Exception:
            # A verification query that failed to run is not proof the
            # target is valid -- treat it exactly like "could not confirm",
            # not "confirmed". Never let a DB error silently authorize a
            # mutation this check exists specifically to gate.
            return {"ok": False, "error_code": "identity_verification_failed", "message": "Could not verify existing album identity against the library database"}
        if not row:
            return {"ok": False, "error_code": "identity_mismatch", "message": f"Target album ID {target_existing_album_id} not found in library"}
        db_mb_albumid = str(row["mb_albumid"] or "").strip().lower()
        if db_mb_albumid and source_album_mbids and db_mb_albumid not in source_album_mbids:
            return {"ok": False, "error_code": "identity_mismatch", "message": "Source audio MBID conflicts with library album"}
        if not source_album_mbids:
            # existing_album_id alone only proves that DB row exists -- it
            # says nothing about whether THIS source folder's content is
            # that album's content. Without embedded tags to cross-check
            # (the branch above), the only other legitimate proof is that
            # this source folder is where the album's own library items
            # already live: an unrelated trusted folder plus an arbitrary
            # existing_album_id must never be treated as authorized just
            # because the row happens to exist (found via adversarial
            # testing during Claude's final review of this exact code path
            # -- the original version returned ok: True here for any
            # trusted-but-unrelated, untagged source).
            source_canonical = str(source_inspect.get("canonical_path") or "")
            bound = False
            for item_row in item_rows:
                raw_path = str(item_row["path"] or "")
                if not raw_path:
                    continue
                item_abs = raw_path if raw_path.startswith("/") else os.path.join(MUSIC_LIBRARY_PATH, raw_path)
                if source_canonical and _path_is_within(item_abs, source_canonical):
                    bound = True
                    break
            if not bound:
                return {
                    "ok": False,
                    "error_code": "review_required",
                    "message": "Could not verify this source folder is the existing album's own library location, and the source has no embedded tags to cross-check",
                }

    if not target_mb_albumid and not target_mb_rgid and not target_existing_album_id:
        # A caller that supplied expected_identity but none of its actual
        # identity fields cannot have anything positively confirmed either.
        return {"ok": False, "error_code": "review_required", "message": "No verifiable identity fields were supplied"}

    return {"ok": True}


_MB_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_REIMPORT_ALLOWED_DUPLICATE_ACTIONS = {"skip", "keep", "remove", "merge"}
_REIMPORT_CONFIG_OVERRIDE_MAX_CHARS = 8192


def _reimport_timeout_for_count(count: int, minimum: int = 300, maximum: int = 1200) -> int:
    """Scale the Beets import subprocess timeout with the freshly re-inspected
    audio_count, mirroring reimport_disk()'s own _beet_import_timeout_for_count()
    formula on the web-manager side. A flat 180s timeout regardless of album
    size regresses large albums (production compatibility fix, SEC-002 Wave 8
    final mutation binding) -- computed from the engine's own authoritative
    reinspection, never trusted from the caller."""
    return max(minimum, min(maximum, 180 + (max(0, int(count or 0)) * 20)))


def reimport_source_atomic(
    source_path: object,
    expected_source_signature: str = None,
    expected_deterministic_identity: dict = None,
    beets_options: dict = None,
) -> dict[str, Any]:
    try:
        trusted = resolve_safe_path(
            source_path, ["music", "staging"], require_exists=True, expected_type="dir"
        )
    except UnsafePathError:
        return {"ok": False, "error_code": "invalid_path"}

    target_existing_album_id_for_lookup = None
    if isinstance(expected_deterministic_identity, dict):
        target_existing_album_id_for_lookup = expected_deterministic_identity.get("existing_album_id")

    for candidate_root in _allowed_root_paths(["music", "staging"]):
        try:
            if trusted == Path(os.path.realpath(candidate_root)):
                return {"ok": False, "error_code": "root_self_rejected"}
        except Exception:
            continue

    source_inspect = inspect_import_source(str(trusted), "reimport")
    if not source_inspect.get("ok"):
        return {"ok": False, "error_code": source_inspect.get("error_code", "inspection_failed")}

    curr_signature = source_inspect.get("source_signature", "")
    if expected_source_signature and curr_signature != expected_source_signature:
        return {
            "ok": False,
            "error_code": "stale_source",
            "message": "Source contents modified since inspection",
        }

    id_check = verify_deterministic_identity(source_inspect, expected_deterministic_identity)
    if not id_check.get("ok"):
        return id_check

    is_torrent_staged = False
    try:
        staging_roots = [os.path.realpath(r) for r in _allowed_root_paths(["staging"])]
        is_torrent_staged = any(_path_is_within(str(trusted), r) for r in staging_roots)
    except Exception:
        pass

    import_target_path = str(trusted)
    preserved_path = None
    if is_torrent_staged:
        pres_res = preserve_import_source(str(trusted), expected_source_signature=curr_signature)
        if not pres_res.get("ok"):
            return pres_res
        preserved_path = pres_res.get("preserved_path")
        import_target_path = preserved_path

    options = beets_options if isinstance(beets_options, dict) else {}
    mb_albumid = str(options.get("mb_albumid") or "").strip()
    if mb_albumid and not _MB_UUID_RE.match(mb_albumid):
        return {"ok": False, "error_code": "invalid_beets_options", "message": "mb_albumid is not a valid MusicBrainz UUID"}
    config_override = str(options.get("config_override") or "")
    if len(config_override) > _REIMPORT_CONFIG_OVERRIDE_MAX_CHARS:
        return {"ok": False, "error_code": "invalid_beets_options", "message": "config_override is too large"}
    duplicate_action = str(options.get("duplicate_action") or "remove").strip().lower()
    if duplicate_action not in _REIMPORT_ALLOWED_DUPLICATE_ACTIONS:
        return {"ok": False, "error_code": "invalid_beets_options", "message": "duplicate_action is not one of the allowed values"}

    if not preserved_path and not config_override:
        # Already-in-library ("already organized, just untracked by Beets")
        # sources must be tagged in place, never relocated -- this matches
        # reimport_disk()'s own established config (copy: no / move: no).
        # A bare `--move` flag (the original implementation's default for
        # this branch) would relocate correctly-placed files according to
        # Beets' path template for no reason; only the preserved-staging
        # branch (a disposable protected copy made specifically to be
        # organized into the library) should ever move/copy anything.
        # duplicate_action has no CLI flag (see below) so it must be
        # included here too, or this default-config path would silently
        # drop it.
        config_override = f"import:\n  copy: no\n  move: no\n  duplicate_action: {duplicate_action}\n"

    lock_file = acquire_os_lock(read_only=False)
    tmp_cfg_path = None
    try:
        full_cmd = [BEET_BIN]
        if config_override:
            tmp_cfg_path = f"/tmp/beets_reimport_cfg_{uuid.uuid4().hex}.yaml"
            with open(tmp_cfg_path, "w", encoding="utf-8") as f:
                f.write(config_override)
            os.chmod(tmp_cfg_path, 0o600)
            full_cmd.extend(["-c", tmp_cfg_path])

        # Beets' `import` has no `-D`/`--duplicate-action` CLI flag at all
        # (confirmed against real `beet import --help`) -- duplicate
        # handling is config-only (`import.duplicate_action` in YAML).
        # The validated duplicate_action value above is applied via
        # config_override (either the caller's own, which reimport_disk()
        # already sets correctly, or the default block just above), never
        # a CLI flag. `--quiet-fallback` must directly precede its `asis`
        # value -- a bare trailing "asis" is parsed as an extra positional
        # PATH argument, which does not exist and aborts the whole import
        # before it ever reaches the real target (found and proven against
        # a real beet binary during Claude's final review of this exact
        # code path; the previous version of this command could never
        # succeed against a live engine).
        cmd_args = ["import", "-q", "--noincremental", "--quiet-fallback", "asis"]
        if preserved_path:
            # The preserved copy is disposable and exists specifically so
            # Beets can safely relocate/organize it -- copy (not move) it
            # into the library so the preserved staging copy is left intact
            # for the caller to clean up only after confirming success.
            cmd_args.append("--copy")
        if mb_albumid:
            cmd_args.extend(["--search-id", mb_albumid])
        cmd_args.append(import_target_path)
        full_cmd.extend(cmd_args)

        env = os.environ.copy()
        env["BEETSDIR"] = BEETSDIR
        proc_timeout = _reimport_timeout_for_count(source_inspect.get("audio_count", 0))
        res = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=proc_timeout,
            env=env
        )

        if res.returncode >= 2:
            return {
                "ok": False,
                "error_code": "import_failed",
                "message": "Beets import failed",
            }

        aid = None
        db_error = False
        try:
            with sqlite3.connect(LIB_PATH, timeout=10) as con:
                con.row_factory = sqlite3.Row
                cur = con.cursor()
                if mb_albumid:
                    row = cur.execute("SELECT id FROM albums WHERE mb_albumid = ?", (mb_albumid,)).fetchone()
                    if row:
                        aid = row[0]
                elif target_existing_album_id_for_lookup:
                    row = cur.execute("SELECT id FROM albums WHERE id = ?", (target_existing_album_id_for_lookup,)).fetchone()
                    if row:
                        aid = row[0]
        except Exception:
            db_error = True

        # Never fall back to "most recently inserted album row": under
        # concurrency, or if Beets resolved to an unexpected release, that
        # can silently attribute this import's result to the wrong album.
        # A result the caller cannot deterministically confirm is reported
        # as such, not guessed.
        return {
            "ok": True,
            "album_id": aid,
            "album_id_verified": aid is not None,
            "album_lookup_failed": db_error,
            "preserved_path": preserved_path,
            "source_path": str(trusted),
            "source_signature": curr_signature,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error_code": "import_failed", "message": "Beets import timed out"}
    except Exception:
        return {"ok": False, "error_code": "import_failed", "message": "Beets import failed"}
    finally:
        if tmp_cfg_path and os.path.exists(tmp_cfg_path):
            try:
                os.unlink(tmp_cfg_path)
            except Exception:
                pass
        release_os_lock(lock_file)


def _path_has_symlink_component(path: Path, root: Path, *, include_leaf: bool = True) -> bool:
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


def _clean_playlist_name(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[\\/]+", "_", text)
    text = re.sub(r"[^A-Za-z0-9._ -]+", "_", text).strip(" .")
    return text[:160] or "Playlist"


def _valid_playlist_m3u_key(value: object) -> str:
    key = str(value or "").strip()
    if not key or not re.match(r"^[A-Za-z0-9_.-]{1,180}$", key) or ".." in key:
        raise UnsafePathError("invalid playlist key")
    return key


def _playlist_m3u_path_for_key(key: str) -> Path:
    safe_key = _valid_playlist_m3u_key(key)
    root = PLAYLIST_DIR.resolve(strict=False)
    target = (root / f"{safe_key}.m3u").resolve(strict=False)
    if target == root or not _path_is_within(str(target), str(root)):
        raise UnsafePathError("playlist M3U path escapes PLAYLIST_DIR")
    if _path_has_symlink_component(target, root, include_leaf=False):
        raise UnsafePathError("playlist M3U path contains a symlinked parent")
    return target


def _playlist_m3u_target(playlist_key: str, fallback_name: str = "") -> tuple[Path, str]:
    key = str(playlist_key or "").strip()
    if key:
        return _playlist_m3u_path_for_key(key), key
    if fallback_name:
        legacy_key = _clean_playlist_name(fallback_name)
        return _playlist_m3u_path_for_key(legacy_key), legacy_key
    raise UnsafePathError("missing playlist key")


def _playlist_m3u_response_row(path: Path) -> dict:
    st = path.stat()
    return {
        "playlist_key": path.stem,
        "key": path.stem,
        "name": path.name,
        "display_name": path.stem,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "exists": True,
    }


def _create_exclusive_temp_file(path: Path, content: str) -> None:
    """Create `path` as a brand-new file and write `content` to it,
    refusing to follow a pre-existing symlink or silently overwrite an
    existing file at that exact path. Path.write_text() uses a plain
    'w'-mode open, which has neither property -- it happily follows a
    pre-existing symlink planted at the predicted tempfile name (SEC-002
    Wave 10 second final review)."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _bounded_read_text(path: Path, max_bytes: int) -> str:
    """Read at most max_bytes+1 bytes and raise if that bound is exceeded,
    rather than trusting a stat() taken before the read -- the file can
    grow between the size check and the read otherwise (SEC-002 Wave 10
    second final review: M3U read growth-race)."""
    with open(path, "rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("m3u_too_large")
    return data.decode("utf-8", errors="replace")


def _parse_m3u_content(content: str) -> list[dict]:
    items = []
    last_extinf = ""
    lines = content.splitlines()
    if len(lines) > PLAYLIST_M3U_MAX_LINES:
        raise ValueError("m3u_too_large")
    for line in lines:
        if len(line) > PLAYLIST_M3U_MAX_LINE_LENGTH:
            raise ValueError("m3u_malformed")
        sline = line.strip()
        if not sline:
            continue
        if sline.startswith("#EXTINF"):
            last_extinf = sline
            continue
        if sline.startswith("#"):
            continue
        artist, title = "", ""
        if last_extinf and "," in last_extinf:
            label = last_extinf.split(",", 1)[1].strip()
            if " - " in label:
                artist, title = label.split(" - ", 1)
            else:
                title = label
        if not title:
            title = Path(sline.replace("\\", "/")).stem
        items.append({"artist": artist.strip(), "title": title.strip(), "path": sline})
        if len(items) > PLAYLIST_M3U_MAX_ITEMS:
            raise ValueError("m3u_too_large")
        last_extinf = ""
    return items


def _library_rewrite_path(raw: object, *, require_exists: bool, expected_type: str | None) -> Path:
    if not raw or not isinstance(raw, str):
        raise UnsafePathError("path must be a non-empty string")
    raw_abs = Path(os.path.abspath(raw))
    music_root = Path(os.path.realpath(MUSIC_LIBRARY_PATH))
    try:
        if raw_abs != music_root and not raw_abs.is_relative_to(music_root):
            raise UnsafePathError("path is outside the music library")
    except AttributeError:
        try:
            raw_abs.relative_to(music_root)
        except ValueError as exc:
            raise UnsafePathError("path is outside the music library") from exc
    if raw_abs == music_root:
        raise UnsafePathError("path cannot be the music library root")
    include_leaf = require_exists
    if _path_has_symlink_component(raw_abs, music_root, include_leaf=include_leaf):
        raise UnsafePathError("path cannot contain symlink components")
    trusted = resolve_safe_path(raw, ["music"], require_exists=require_exists, expected_type=expected_type)
    if trusted == music_root:
        raise UnsafePathError("path cannot be the music library root")
    return trusted


def _library_db_values(path: Path) -> tuple[str, str]:
    abs_value = str(path)
    try:
        rel_value = path.relative_to(Path(os.path.realpath(MUSIC_LIBRARY_PATH))).as_posix()
    except Exception:
        rel_value = abs_value
    return rel_value, abs_value


class AlbumArtOperationError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = int(status or 400)


_DISC_DIR_RE = re.compile(r"^(?:disc|cd|disk)\s*\d+$", re.I)
_ALBUM_ART_FIXED_NAMES = (
    "albumart.jpg", "albumart.jpeg", "albumart.png", "albumart.webp",
    "folder.jpg", "folder.jpeg", "folder.png", "folder.webp",
    "cover.jpg", "cover.jpeg", "cover.png", "cover.webp",
    "front.jpg", "front.jpeg", "front.png", "front.webp",
)


def _db_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _trusted_music_db_path(raw: Any, *, require_exists: bool = False, expected_type: str | None = None) -> Path:
    text = _db_text(raw).strip()
    if not text:
        raise UnsafePathError("path is empty")
    if "\x00" in text or "\\" in text:
        raise UnsafePathError("path contains a null byte or backslash")
    if re.match(r"^[A-Za-z]:", text) or text.startswith("//"):
        raise UnsafePathError("windows and UNC paths are not allowed")
    if text.startswith("/"):
        raw_path = text
    else:
        music_root_text = _db_text(MUSIC_LIBRARY_PATH).replace("\\", "/").rstrip("/")
        raw_path = f"{music_root_text}/{text.lstrip('/')}"
    return _library_rewrite_path(raw_path, require_exists=require_exists, expected_type=expected_type)


def _require_album_art_path(path: Path, album_dir: Path, *, require_exists: bool = False) -> Path:
    music_root = Path(os.path.realpath(MUSIC_LIBRARY_PATH))
    candidate = Path(os.path.abspath(str(path)))
    try:
        candidate.relative_to(album_dir)
    except Exception as exc:
        raise UnsafePathError("artwork path is outside the album folder") from exc
    if candidate == music_root or candidate == album_dir:
        raise UnsafePathError("artwork path cannot be a root directory")
    if _path_has_symlink_component(candidate, music_root, include_leaf=require_exists):
        raise UnsafePathError("artwork path cannot contain symlink components")
    if candidate.is_symlink():
        raise UnsafePathError("artwork path cannot be a symlink")
    if candidate.exists():
        if not candidate.is_file():
            raise UnsafePathError("artwork path is not a regular file")
    elif require_exists:
        raise UnsafePathError("artwork path does not exist")
    return candidate


def _album_art_album_dir(cur: sqlite3.Cursor, album_id: int) -> tuple[sqlite3.Row, Path]:
    cur.execute("SELECT id, album, albumartist, mb_releasegroupid, artpath FROM albums WHERE id = ?", (album_id,))
    album = cur.fetchone()
    if not album:
        raise AlbumArtOperationError("Album not found", 404)
    cur.execute(
        "SELECT path FROM items WHERE album_id = ? AND path IS NOT NULL ORDER BY id LIMIT 25",
        (album_id,),
    )
    rows = cur.fetchall()
    if not rows:
        raise AlbumArtOperationError("Album folder could not be resolved", 400)
    music_root = Path(os.path.realpath(MUSIC_LIBRARY_PATH))
    resolved_dir: Optional[Path] = None
    for row in rows:
        try:
            item_path = _trusted_music_db_path(row["path"], require_exists=False)
            album_dir = item_path.parent
            if _DISC_DIR_RE.match(album_dir.name):
                album_dir = album_dir.parent
            album_dir = Path(os.path.abspath(str(album_dir)))
            if album_dir == music_root:
                continue
            album_dir.relative_to(music_root)
            if not album_dir.exists() or not album_dir.is_dir():
                continue
            if _path_has_symlink_component(album_dir, music_root, include_leaf=True):
                continue
        except Exception:
            continue
        if resolved_dir is None:
            resolved_dir = album_dir
        elif album_dir != resolved_dir:
            # Items disagree about which directory the album actually lives
            # in (e.g. a stale or corrupted database row) -- never silently
            # mutate based on whichever item happened to resolve first.
            # Legitimate multidisc layouts (Album/Disc 1, Album/Disc 2, ...)
            # are already unified to their shared parent above and do not
            # trigger this.
            raise AlbumArtOperationError(
                "Album items are split across conflicting directories; refusing to guess the album folder",
                409,
            )
    if resolved_dir is None:
        raise AlbumArtOperationError("Album folder could not be resolved safely", 400)
    return album, resolved_dir


def _check_album_art_identity(album: sqlite3.Row, expected_rgid: Any) -> str:
    actual = _db_text(album["mb_releasegroupid"]).strip().lower()
    expected = _db_text(expected_rgid).strip().lower()
    if expected and expected != actual:
        raise AlbumArtOperationError("Album identity changed before artwork update", 409)
    return actual


def _normalise_album_art_image(data: bytes) -> tuple[bytes, dict[str, Any]]:
    if not data or len(data) < 32:
        raise AlbumArtOperationError("Cover image is empty or too small", 400)
    if len(data) > ALBUM_ART_MAX_BYTES:
        raise AlbumArtOperationError("Cover image exceeds the size limit", 400)
    try:
        from PIL import Image, ImageOps
        Image.MAX_IMAGE_PIXELS = ALBUM_ART_MAX_PIXELS
    except Exception as exc:
        raise AlbumArtOperationError("Image validation is unavailable", 500) from exc
    try:
        with Image.open(io.BytesIO(data)) as probe:
            image_format = (probe.format or "").upper()
            width, height = probe.size
            frames = int(getattr(probe, "n_frames", 1) or 1)
            if image_format not in {"JPEG", "PNG", "WEBP"}:
                raise AlbumArtOperationError("Unsupported image type", 400)
            if frames != 1 or bool(getattr(probe, "is_animated", False)):
                raise AlbumArtOperationError("Animated artwork is not supported", 400)
            if width < 1 or height < 1:
                raise AlbumArtOperationError("Image dimensions are invalid", 400)
            if width > ALBUM_ART_MAX_DIMENSION or height > ALBUM_ART_MAX_DIMENSION:
                raise AlbumArtOperationError("Image dimensions exceed the limit", 400)
            if width * height > ALBUM_ART_MAX_PIXELS:
                raise AlbumArtOperationError("Image pixel count exceeds the limit", 400)
            probe.verify()
        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image)
            if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
            out = io.BytesIO()
            image.save(out, format="JPEG", quality=90, optimize=True)
            normalized = out.getvalue()
    except AlbumArtOperationError:
        raise
    except Exception as exc:
        name = type(exc).__name__
        if name in {"DecompressionBombError", "DecompressionBombWarning", "UnidentifiedImageError"}:
            raise AlbumArtOperationError("Image could not be safely decoded", 400) from exc
        raise AlbumArtOperationError("Image could not be safely decoded", 400) from exc
    if not normalized or len(normalized) > ALBUM_ART_MAX_BYTES:
        raise AlbumArtOperationError("Normalized artwork exceeds the size limit", 400)
    return normalized, {
        "format": "JPEG",
        "mime": "image/jpeg",
        "extension": ".jpg",
        "width": width,
        "height": height,
        "bytes": len(normalized),
    }


def _decode_album_art_payload(body: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    raw = body.get("image_data_b64")
    if not isinstance(raw, str) or not raw.strip():
        raise AlbumArtOperationError("Cover image data is required", 400)
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AlbumArtOperationError("Cover image data is invalid", 400) from exc
    return _normalise_album_art_image(data)


def _fsync_file(path: Path) -> bool:
    """Best-effort durability step for an already-written, already-renamed
    file. Returns False (and logs) rather than silently swallowing on
    failure -- callers that report success must qualify it with the
    combined result of this and _fsync_directory() rather than presenting
    an unqualified durability guarantee this function alone cannot
    provide."""
    try:
        with open(path, "rb") as handle:
            os.fsync(handle.fileno())
        return True
    except Exception as exc:
        print(f"[BeetsControlAgent] WARNING: fsync failed for {path.name}: {type(exc).__name__}")
        return False


def _fsync_directory(path: Path) -> bool:
    flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
    fd = None
    ok = False
    try:
        fd = os.open(str(path), flags)
        os.fsync(fd)
        ok = True
    except Exception as exc:
        print(f"[BeetsControlAgent] WARNING: directory fsync failed for {path.name}: {type(exc).__name__}")
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
    return ok


def _replace_or_copy_unlink(src: Path, dst: Path) -> None:
    try:
        os.replace(src, dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    shutil.copy2(src, dst, follow_symlinks=False)
    _fsync_file(dst)
    _fsync_directory(dst.parent)
    src.unlink()

def _album_art_trash_root() -> Path:
    trash_root = Path(os.path.realpath(str(ALBUM_ART_TRASH_ROOT)))
    beets_root = Path(os.path.realpath(BEETSDIR))
    try:
        trash_root.relative_to(beets_root)
    except ValueError as exc:
        raise UnsafePathError("album artwork trash root is outside the Beets config root") from exc
    if _path_has_symlink_component(trash_root, beets_root, include_leaf=False):
        raise UnsafePathError("album artwork trash root cannot contain symlink components")
    return trash_root


def _replace_album_art_locked(con: sqlite3.Connection, album_id: int, body: dict[str, Any]) -> dict[str, Any]:
    cur = con.cursor()
    album, album_dir = _album_art_album_dir(cur, album_id)
    rgid = _check_album_art_identity(album, body.get("expected_mb_releasegroupid"))
    image_bytes, image_info = _decode_album_art_payload(body)
    music_root = Path(os.path.realpath(MUSIC_LIBRARY_PATH))
    if _path_has_symlink_component(album_dir, music_root, include_leaf=True):
        raise UnsafePathError("album folder cannot contain symlink components")
    dest = _require_album_art_path(album_dir / ALBUM_ART_FILENAME, album_dir, require_exists=False)
    tmp_path: Path | None = None
    backup_path: Path | None = None
    replaced_existing = False
    dest_replaced = False
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".albumart-", suffix=".tmp", dir=str(album_dir))
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(image_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o644)
        _require_album_art_path(dest, album_dir, require_exists=False)
        if dest.exists():
            if dest.is_symlink() or not dest.is_file():
                raise UnsafePathError("existing artwork is not a regular file")
            backup_path = album_dir / f".albumart-backup-{uuid.uuid4().hex}.jpg"
            os.replace(dest, backup_path)
            replaced_existing = True
        os.replace(tmp_path, dest)
        tmp_path = None
        dest_replaced = True
        # Best-effort durability for the already-successful rename -- a
        # failure here does not undo an otherwise-correct replace (the
        # content itself was already fsync'd above before the rename), but
        # must not be silently reported as a fully durable write either.
        durable = _fsync_file(dest) and _fsync_directory(album_dir)
        cur.execute("UPDATE albums SET artpath = ? WHERE id = ?", (str(dest), album_id))
        if cur.rowcount != 1:
            raise AlbumArtOperationError("Album artpath update did not affect exactly one row", 500)
        con.commit()
        if backup_path and backup_path.exists():
            try:
                backup_path.unlink()
            except Exception:
                pass
        return {
            "ok": True,
            "album_id": album_id,
            "artpath": str(dest),
            "album_dir": str(album_dir),
            "mb_releasegroupid": rgid,
            "source": _db_text(body.get("source") or "user")[:40],
            "replaced_existing": replaced_existing,
            "durable": durable,
            "image": image_info,
        }
    except Exception:
        con.rollback()
        if dest_replaced:
            try:
                if dest.exists():
                    dest.unlink()
            except Exception:
                pass
        if backup_path and backup_path.exists():
            try:
                os.replace(backup_path, dest)
            except Exception:
                pass
        raise
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def _album_art_delete_candidates(album: sqlite3.Row, album_dir: Path) -> list[Path]:
    seen: set[str] = set()
    candidates: list[Path] = []
    raw_art = _db_text(album["artpath"]).replace("\x00", "").strip()
    if raw_art:
        current = _trusted_music_db_path(raw_art, require_exists=False)
        current = _require_album_art_path(current, album_dir, require_exists=False)
        if current.exists():
            candidates.append(current)
            seen.add(str(current.resolve(strict=False)))
    for name in _ALBUM_ART_FIXED_NAMES:
        candidate = _require_album_art_path(album_dir / name, album_dir, require_exists=False)
        key = str(candidate.resolve(strict=False))
        if key in seen:
            continue
        if candidate.exists():
            candidates.append(candidate)
            seen.add(key)
    return candidates


def _claim_trash_target_exclusive(target: Path) -> None:
    """Atomically claim `target` as a brand-new, exclusively-created empty
    file. Raises FileExistsError if anything is already there.

    This exists because the obvious check-then-act pattern (`if
    target.exists(): ... ; if target.exists(): raise`) leaves a real race
    window between the check and the later os.replace()/shutil.copy2() in
    _replace_or_copy_unlink() -- both of those silently overwrite an
    existing destination and must never be relied on as a no-clobber
    primitive by themselves. O_CREAT|O_EXCL is the actual atomic
    "create-if-absent" operation; a random UUID in the path name only
    reduces collision probability, it is not itself a security boundary.
    """
    fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)


def _delete_album_art_locked(con: sqlite3.Connection, album_id: int) -> dict[str, Any]:
    cur = con.cursor()
    album, album_dir = _album_art_album_dir(cur, album_id)
    candidates = _album_art_delete_candidates(album, album_dir)
    op_id = uuid.uuid4().hex
    moved: list[dict[str, str]] = []
    try:
        trash_root = _album_art_trash_root()
        for src in candidates:
            _require_album_art_path(src, album_dir, require_exists=True)
            target = trash_root / op_id / src.name
            target.parent.mkdir(parents=True, exist_ok=True)
            if _path_has_symlink_component(target.parent, trash_root, include_leaf=True):
                raise UnsafePathError("album artwork trash path cannot contain symlink components")
            try:
                _claim_trash_target_exclusive(target)
            except FileExistsError:
                target = target.with_name(f"{target.stem}-{uuid.uuid4().hex[:8]}{target.suffix}")
                try:
                    _claim_trash_target_exclusive(target)
                except FileExistsError as exc:
                    raise UnsafePathError("album artwork trash target already exists") from exc
            _replace_or_copy_unlink(src, target)
            moved.append({"source": str(src), "quarantined": str(target)})
        cur.execute("UPDATE albums SET artpath = '' WHERE id = ?", (album_id,))
        if cur.rowcount != 1:
            raise AlbumArtOperationError("Album artpath clear did not affect exactly one row", 500)
        con.commit()
        return {
            "ok": True,
            "album_id": album_id,
            "album_dir": str(album_dir),
            "removed": [row["source"] for row in moved],
            "removed_count": len(moved),
            "quarantined_art": moved,
        }
    except Exception:
        con.rollback()
        for row in reversed(moved):
            src = Path(row["quarantined"])
            dst = Path(row["source"])
            try:
                if src.exists() and not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    _replace_or_copy_unlink(src, dst)
            except Exception:
                pass
        raise


def is_safe_path(path: object, allowed_types: list = None) -> bool:
    """Boolean convenience wrapper for call sites that only need a check
    (e.g. validating job args), not the resolved Path itself."""
    try:
        resolve_safe_path(path, allowed_types)
        return True
    except UnsafePathError:
        return False


_DOTDOT_SEGMENT_RE = re.compile(r"(^|/)\.\.(/|$)")


def _sanitize_command_path_args(args: Any, allowed_types: list) -> list:
    """Validate a command/job argument list, replacing any absolute-path
    argument with its canonical resolved value (never the original
    request string). Raises UnsafePathError for any unsafe argument.

    Rejects ".." only as an actual path segment (bounded by "/" or the
    whole string), not as a bare substring: Beets' own supported query
    syntax uses ".." for range queries (e.g. `year:2020..2023`,
    `added:2020-01-01..2020-02-01`), which a blanket substring check would
    incorrectly reject even though these args are never filesystem-joined.
    A relative arg is still only relevant to traversal for a command like
    `import` that can accept a second positional path, so a genuine
    "../"-shaped segment must still be blocked.
    """
    if not isinstance(args, list):
        raise UnsafePathError("args must be a list")

    sanitized: list = []
    for arg in args:
        s_arg = str(arg)
        if s_arg in ("-c", "--config") or s_arg.startswith(("-c=", "--config=")):
            raise UnsafePathError("unsupported config override in args")
        if (
            s_arg == "." or s_arg.startswith("./")
            or _DOTDOT_SEGMENT_RE.search(s_arg)
            or "\\" in s_arg or "\x00" in s_arg
        ):
            raise UnsafePathError("invalid path parameter in args")
        if s_arg.startswith("/"):
            sanitized.append(str(resolve_safe_path(s_arg, allowed_types)))
        else:
            sanitized.append(s_arg)
    return sanitized


def acquire_os_lock(read_only: bool = False):
    """Acquire an OS file lock on LOCK_PATH for Beets concurrency protection."""
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lock_file = open(LOCK_PATH, "a+")
    if fcntl is not None:
        mode = fcntl.LOCK_SH if read_only else fcntl.LOCK_EX
        fcntl.flock(lock_file.fileno(), mode)
    return lock_file


def release_os_lock(lock_file):
    """Release the OS file lock."""
    if lock_file:
        try:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
        except Exception:
            pass


class AgentJob:
    def __init__(self, job_id: str, command: list, label: str = "", config_override: str = ""):
        self.job_id = job_id
        self.command = command
        self.label = label or " ".join(command)
        self.config_override = config_override
        self.created_at = time.time()
        self.started_at = None
        self.finished_at = None
        self.returncode = None
        self.stdout = []
        self.stderr = []
        self.proc = None
        self._cancel = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self):
        self._cancel.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                time.sleep(0.2)
                if self.proc.poll() is None:
                    self.proc.kill()
            except Exception:
                pass

    def _run(self):
        self.started_at = time.time()
        lock_file = None
        tmp_cfg_path = None
        try:
            mutating = self.command and self.command[0] in {
                "import", "update", "write", "move", "modify", "mbsync",
                "fetchart", "embedart", "lastgenre", "alt", "remove", "rm"
            }
            lock_file = acquire_os_lock(read_only=not mutating)

            full_cmd = [BEET_BIN]
            if self.config_override:
                tmp_cfg_path = f"/tmp/beets_job_cfg_{self.job_id}.yaml"
                with open(tmp_cfg_path, "w", encoding="utf-8") as f:
                    f.write(self.config_override)
                os.chmod(tmp_cfg_path, 0o600)
                full_cmd.extend(["-c", tmp_cfg_path])

            full_cmd.extend(self.command)
            env = os.environ.copy()
            env["BEETSDIR"] = BEETSDIR

            self.proc = subprocess.Popen(
                full_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env
            )
            out, err = self.proc.communicate(timeout=600)
            self.returncode = self.proc.returncode
            if out:
                self.stdout = out.splitlines()[-5000:]
            if err:
                self.stderr = err.splitlines()[-5000:]
        except subprocess.TimeoutExpired:
            if self.proc:
                self.proc.kill()
            self.returncode = 124
            self.stderr.append("Command timed out after 600 seconds")
        except Exception as exc:
            self.returncode = 1
            self.stderr.append(f"Execution error: {exc}")
        finally:
            if tmp_cfg_path and os.path.exists(tmp_cfg_path):
                try:
                    os.unlink(tmp_cfg_path)
                except Exception:
                    pass
            release_os_lock(lock_file)
            self.finished_at = time.time()

    def to_dict(self, include_logs=True):
        status = "running"
        if self.finished_at is not None:
            if self._cancel.is_set():
                status = "cancelled"
            else:
                status = "success" if self.returncode == 0 else "failed"

        d = {
            "job_id": self.job_id,
            "label": self.label,
            "status": status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "returncode": self.returncode,
        }
        if include_logs:
            d["stdout"] = self.stdout
            d["stderr"] = self.stderr
        return d


def _handle_delete_album(album_id: int, delete_files: bool = True) -> tuple:
    lock = acquire_os_lock(read_only=False)
    try:
        if not os.path.exists(LIB_PATH):
            return 404, {"error": "Database musiclibrary.blb not found", "status": "failed", "database_deleted": False}

        con = sqlite3.connect(LIB_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            cur = con.cursor()
            cur.execute("SELECT * FROM albums WHERE id = ?", (album_id,))
            album_row = cur.fetchone()
            if not album_row:
                return 404, {"error": f"Album {album_id} not found", "status": "failed", "database_deleted": False}

            album_dict = dict(album_row)
            album_path = album_dict.get("path")
            if isinstance(album_path, bytes):
                try:
                    album_path = album_path.decode("utf-8")
                except Exception:
                    album_path = str(album_path)

            cur.execute("SELECT * FROM items WHERE album_id = ?", (album_id,))
            item_rows = cur.fetchall()
            item_files = []
            for item in item_rows:
                ipath = item["path"]
                if isinstance(ipath, bytes):
                    try:
                        ipath = ipath.decode("utf-8")
                    except Exception:
                        ipath = str(ipath)
                if ipath:
                    item_files.append(ipath)

            cur.execute("BEGIN TRANSACTION")
            cur.execute("DELETE FROM items WHERE album_id = ?", (album_id,))
            items_deleted = cur.rowcount
            cur.execute("DELETE FROM albums WHERE id = ?", (album_id,))
            albums_deleted = cur.rowcount
            con.commit()

            files_deleted = 0
            file_errors = []
            if delete_files:
                # Database rows are already committed above at this point.
                # Everything below is best-effort filesystem cleanup, and
                # must never be allowed to propagate an exception up to the
                # outer handler -- doing so would incorrectly report
                # database_deleted: False for a delete that already
                # succeeded. Stored item/album paths come from the
                # database, not the current request, but are still
                # untrusted: a corrupt or maliciously-written row must not
                # be able to direct a delete outside the approved roots.
                # Resolve each one through resolve_safe_path() and operate
                # only on the returned Path (never the raw stored string),
                # and refuse an approved root itself the same way
                # /files/delete does.
                try:
                    for fpath in item_files:
                        if not fpath:
                            continue
                        try:
                            safe_fpath = resolve_safe_path(fpath, ["music", "staging"])
                        except UnsafePathError:
                            file_errors.append("Skipped unsafe album item path")
                            continue
                        if any(str(safe_fpath) == os.path.realpath(root) for root in _allowed_root_paths(["music", "staging"])):
                            file_errors.append("Skipped allowed root path")
                            continue
                        if safe_fpath.exists():
                            try:
                                safe_fpath.unlink()
                                files_deleted += 1
                            except Exception:
                                file_errors.append("Failed to delete album item file")

                    if album_path:
                        try:
                            safe_album_path = resolve_safe_path(album_path, ["music"])
                        except UnsafePathError:
                            safe_album_path = None
                        if safe_album_path is not None and not any(
                            str(safe_album_path) == os.path.realpath(root) for root in _allowed_root_paths(["music"])
                        ):
                            try:
                                album_dir_is_empty = safe_album_path.is_dir() and not os.listdir(safe_album_path)
                            except Exception:
                                album_dir_is_empty = False
                            if album_dir_is_empty:
                                try:
                                    safe_album_path.rmdir()
                                except Exception:
                                    pass
                except Exception:
                    file_errors.append("Unexpected error during file cleanup")

            files_failed = len(file_errors)
            if files_failed > 0:
                status_str = "partial_failure"
                success_flag = False
            else:
                status_str = "success"
                success_flag = True

            return 200, {
                "success": success_flag,
                "status": status_str,
                "database_deleted": True,
                "album_id": album_id,
                "items_deleted": items_deleted,
                "albums_deleted": albums_deleted,
                "files_deleted": files_deleted,
                "files_failed": files_failed,
                "file_errors": file_errors,
            }
        except Exception as exc:
            con.rollback()
            return 500, {"error": f"Database transaction failed: {exc}", "status": "failed", "database_deleted": False}
        finally:
            con.close()
    finally:
        release_os_lock(lock)


def _json_default(obj):
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8", errors="replace")
        except Exception:
            return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _engine_playlist_quality_for_item(item, path_text: str) -> dict[str, Any]:
    album = str(getattr(item, "album", "") or "").strip()
    albumartist = str(getattr(item, "albumartist", "") or "").strip()
    try:
        length = float(getattr(item, "length", 0) or 0)
    except (ValueError, TypeError):
        length = 0.0
    try:
        bitrate = int(getattr(item, "bitrate", 0) or 0)
    except (ValueError, TypeError):
        bitrate = 0
    fmt = str(getattr(item, "format", "") or Path(path_text).suffix.lstrip(".")).strip()
    path_l = str(path_text).replace("\\", "/").lower()
    flags: list[str] = []

    if length > 0 and length < 180:
        flags.append("preview_risk")
    if not album:
        flags.append("blank_album")
    if album.lower() in ("soundcloud", "youtube", "spotify", "slskd", "spotiflac", "soulseek", "playlist", "downloads"):
        flags.append("provider_album")
    if (
        "various artists -  -" in path_l
        or ("compilations/ (2016)" in path_l and not album)
        or path_l.startswith("non-album/")
        or "/non-album/" in path_l
        or path_l.startswith("playlist imports/")
        or "/playlist imports/" in path_l
    ):
        flags.append("bad_playlist_path")

    try:
        if path_text and not Path(path_text).exists():
            flags.append("missing_file")
    except Exception:
        flags.append("path_check_failed")

    quality = "ok"
    if "preview_risk" in flags or "missing_file" in flags:
        quality = "bad"
    elif flags:
        quality = "review"

    return {
        "quality": quality,
        "quality_flags": flags,
        "length": round(length, 1),
        "format": fmt,
        "bitrate": bitrate,
        "albumartist": albumartist,
    }


def _engine_playlist_quality_row_payload(row: sqlite3.Row) -> dict[str, Any]:
    path_text = str(row["path"] if hasattr(row, "keys") and "path" in row.keys() else row[6])
    item = type("PlaylistQualityRow", (), {})()
    row_keys = set(row.keys()) if hasattr(row, "keys") else set()
    for field in (
        "id", "title", "artist", "album", "albumartist", "length", "bitrate",
        "format", "path", "mb_trackid", "mb_albumid", "track", "disc", "year",
        "added",
    ):
        try:
            setattr(item, field, row[field] if field in row_keys else "")
        except Exception:
            setattr(item, field, "")

    payload = {
        "id": int(row["id"] if "id" in row_keys else 0),
        "title": str(row["title"] if "title" in row_keys else ""),
        "artist": str(row["artist"] if "artist" in row_keys else ""),
        "album": str(row["album"] if "album" in row_keys else ""),
        "albumartist": str(row["albumartist"] if "albumartist" in row_keys else ""),
        "path": path_text,
    }
    for field in ("mb_trackid", "mb_albumid"):
        if field in row_keys:
            payload[field] = str(row[field] or "").strip()
    for field in ("track", "disc", "year"):
        if field in row_keys:
            try:
                payload[field] = int(float(row[field] or 0))
            except Exception:
                payload[field] = 0
    if "added" in row_keys:
        try:
            payload["added"] = float(row["added"] or 0)
        except Exception:
            payload["added"] = 0.0

    payload.update(_engine_playlist_quality_for_item(item, path_text))
    album_item_count = 0
    if "album_item_count" in row_keys:
        try:
            album_item_count = int(row["album_item_count"] or 0)
        except Exception:
            album_item_count = 0
        payload["album_item_count"] = album_item_count

    flags = set(payload.get("quality_flags") or [])
    if "preview_risk" in flags:
        payload["recommended_action"] = "delete_preview"
        payload["repairable"] = False
    elif flags:
        payload["recommended_action"] = "repair"
        payload["repairable"] = bool(payload.get("artist") and payload.get("title"))
    else:
        payload["recommended_action"] = "keep"
        payload["repairable"] = False
    return payload


def _engine_playlist_quality_cleanup_candidates(con: sqlite3.Connection,
                                                limit: int = 200,
                                                item_ids: Optional[list[int]] = None,
                                                filter_mode: str = "all") -> list[dict[str, Any]]:
    mode = str(filter_mode or "all").strip().lower()
    threshold = PLAYLIST_QUALITY_PREVIEW_THRESHOLD_SECONDS
    preview_paths = (
        "(path LIKE '%Playlist Downloads%' OR path LIKE 'Compilations/%' "
        "OR path LIKE 'Non-Album/%' OR path LIKE '%/Non-Album/%' "
        "OR path LIKE '%/Playlist Imports/%' OR path LIKE 'Playlist Imports/%')"
    )
    provider_album_sql = (
        "lower(trim(COALESCE(album,''))) IN "
        "('soundcloud','youtube','spotify','slskd','spotiflac','soulseek','playlist','downloads')"
    )
    repair_paths = (
        "("
        " path LIKE '%Various Artists -  - %'"
        " OR path LIKE 'Non-Album/%'"
        " OR path LIKE '%/Non-Album/%'"
        " OR path LIKE '%/Playlist Imports/%'"
        " OR path LIKE 'Playlist Imports/%'"
        " OR COALESCE(album,'')=''"
        " OR (COALESCE(album,'')='' AND COALESCE(albumartist,'')='Various Artists')"
        " OR (COALESCE(album,'')<>'' AND COALESCE(title,'')<>'' "
        "     AND lower(trim(album))=lower(trim(title)) "
        "     AND (SELECT COUNT(*) FROM items i2 WHERE i2.album_id=items.album_id) <= 2)"
        f" OR {provider_album_sql}"
        ")"
    )
    if mode in {"preview", "preview_only", "delete_preview"}:
        where = f"((length > 0 AND length < ?) AND {preview_paths})"
        params: list[Any] = [threshold]
    elif mode in {"repair", "repairable"}:
        where = f"{repair_paths} AND NOT (length > 0 AND length < ?)"
        params = [threshold]
    else:
        where = f"({repair_paths} OR ((length > 0 AND length < ?) AND {preview_paths}))"
        params = [threshold]

    id_filter = ""
    if item_ids:
        ids = [int(i) for i in item_ids if int(i) > 0]
        if ids:
            id_filter = " AND id IN (" + ",".join("?" for _ in ids) + ")"
            params.extend(ids)
    params.append(max(1, min(int(limit or 200), 2000)))

    sql = (
        "SELECT id,title,artist,album,albumartist,length,path,format,bitrate, "
        "mb_trackid,mb_albumid,track,disc,year,added, "
        "(SELECT COUNT(*) FROM items i2 WHERE i2.album_id=items.album_id) AS album_item_count "
        f"FROM items WHERE {where}{id_filter} "
        "ORDER BY id DESC LIMIT ?"
    )
    cur = con.cursor()
    rows = cur.execute(sql, params).fetchall()
    return [_engine_playlist_quality_row_payload(row) for row in rows]


AUDIO_EXT = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aiff", ".wv", ".ape", ".alac", ".aac"}


def _path_lexically_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
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


def _unique_import_review_cleanup_path(path: Path) -> Path:
    if not path.exists():
        return path
    for idx in range(1, 1000):
        candidate = path.with_name(f"{path.stem}.{idx}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem}.{uuid.uuid4().hex[:8]}{path.suffix}")





class ControlAgentHandler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, data: dict):
        body = json.dumps(data, indent=2, default=_json_default).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authenticate(self) -> bool:
        if not beets_api_token_is_usable(BEETS_API_TOKEN):
            self._send_json(500, {"error": "Control Agent misconfigured: BEETS_API_TOKEN is missing or too weak"})
            return False

        header_token = self.headers.get("X-Beets-API-Token", "")
        if not header_token:
            auth_header = self.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                header_token = auth_header[7:]

        if not hmac.compare_digest(header_token.strip(), BEETS_API_TOKEN.strip()):
            self._send_json(401, {"error": "Unauthorized: invalid API token"})
            return False
        return True

    def log_message(self, format, *args):
        pass  # Quiet HTTP handler logging

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        if path == "/health":
            self._send_json(200, {
                "status": "ok",
                "service": "beets-control-agent",
                "agent_version": "1.0.0",
            })
            return

        if not self._authenticate():
            return

        if path == "/transactions":
            offset = int(params.get("offset", [0])[0])
            limit = int(params.get("limit", [50])[0])
            status_f = params.get("status", [""])[0]
            op_f = params.get("operation", [""])[0]
            q_f = params.get("query", [""])[0]
            job_f = params.get("job", [""])[0]
            rows, total = _txn_store.list(offset=offset, limit=limit, status=status_f, operation=op_f, query=q_f, job=job_f)
            self._send_json(200, {"ok": True, "transactions": rows, "total": total, "offset": offset, "limit": limit})
            return

        if path.startswith("/transactions/"):
            tx_id = path.split("/transactions/")[1].strip()
            try:
                fmt = params.get("format", ["json"])[0]
                if fmt in {"markdown", "md", "csv"}:
                    content, mime = _txn_store.export(tx_id, fmt)
                    self.send_response(200)
                    self.send_header("Content-Type", f"{mime}; charset=utf-8")
                    self.send_header("Content-Length", str(len(content.encode("utf-8"))))
                    self.end_headers()
                    self.wfile.write(content.encode("utf-8"))
                    return
                tx_data = _txn_store.get(tx_id, offset=int(params.get("offset", [0])[0]), limit=int(params.get("limit", [1000])[0]))
                self._send_json(200, {"ok": True, "transaction": tx_data})
            except KeyError:
                self._send_json(404, {"error": "Transaction not found"})
            return

        if path == "/status":
            force_refresh = params.get("refresh", ["0"])[0] == "1" or params.get("force", ["0"])[0] == "1"
            self._send_json(200, _agent_status_payload(force_refresh=force_refresh))
            return

        if path == "/version":
            status = _agent_status_payload()
            self._send_json(200, {"agent_version": "1.0.0", "beets_version": status.get("beets_version") or ""})
            return

        if path == "/capabilities":
            self._send_json(200, {
                "allowed_commands": list(ALLOWED_COMMANDS),
                "os_locking": True,
                "strict_path_validation": True,
                "read_only_raw_query": False,
            })
            return

        if path == "/config":
            try:
                self._send_json(200, _read_agent_config_file())
            except FileNotFoundError:
                self._send_json(404, {"error": "config.yaml not found", "error_code": "config_not_found"})
            except PermissionError:
                self._send_json(403, {"error": "config.yaml is not readable", "error_code": "config_permission_denied"})
            except OSError as exc:
                self._send_json(500, {"error": "Could not read config.yaml", "error_code": "config_read_failed", "detail": _redact_agent_status_text(str(exc))})
            return

        if path == "/items":
            album_id = params.get("album_id", [None])[0]
            path_val = params.get("path", [None])[0]
            mbid = params.get("mbid", [None])[0] or params.get("mb_trackid", [None])[0]
            album_text = params.get("album", [None])[0]
            artist_text = params.get("artist", [None])[0]
            title_text = params.get("title", [None])[0]
            query = params.get("query", [None])[0]
            offset = max(int(params.get("offset", [0])[0]), 0)
            limit = min(max(int(params.get("limit", [500])[0]), 1), 2000)

            if not os.path.exists(LIB_PATH):
                self._send_json(404, {"error": "Database file musiclibrary.blb not found"})
                return

            lock = acquire_os_lock(read_only=True)
            try:
                con = sqlite3.connect(LIB_PATH, timeout=10)
                con.row_factory = sqlite3.Row
                cur = con.cursor()

                where_clause = ""
                sql_params = []
                if album_id is not None:
                    where_clause = "WHERE album_id = ?"
                    sql_params.append(int(album_id))
                elif path_val is not None:
                    where_clause = "WHERE path = ?"
                    sql_params.append(path_val)
                elif mbid is not None:
                    where_clause = "WHERE mb_trackid = ?"
                    sql_params.append(mbid)
                elif "singleton" in params:
                    s_raw = params["singleton"][0]
                    if s_raw is None or str(s_raw).strip().lower() not in {"true", "false", "1", "0"}:
                        self._send_json(400, {"error": f"Invalid singleton parameter value: {s_raw!r}"})
                        con.close()
                        return
                    s_bool = str(s_raw).strip().lower() in {"true", "1"}
                    if s_bool:
                        where_clause = "WHERE (album_id IS NULL OR album_id = 0)"
                    else:
                        where_clause = "WHERE (album_id IS NOT NULL AND album_id != 0)"
                elif album_text is not None:
                    where_clause = "WHERE album LIKE ? ESCAPE '\\'"
                    escaped = album_text.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                    sql_params.append(f"%{escaped}%")
                elif artist_text is not None:
                    where_clause = "WHERE artist LIKE ? ESCAPE '\\'"
                    escaped = artist_text.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                    sql_params.append(f"%{escaped}%")
                elif title_text is not None:
                    where_clause = "WHERE title LIKE ? ESCAPE '\\'"
                    escaped = title_text.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                    sql_params.append(f"%{escaped}%")
                elif query is not None:
                    q_str = str(query).strip()
                    if not q_str:
                        where_clause = "WHERE 1 = 0"
                    elif q_str.startswith("album_id:"):
                        where_clause = "WHERE album_id = ?"
                        sql_params.append(int(q_str.split(":", 1)[1].strip()))
                    elif q_str.startswith("album:"):
                        where_clause = "WHERE album LIKE ? ESCAPE '\\'"
                        escaped = q_str[6:].strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                        sql_params.append(f"%{escaped}%")
                    elif q_str.startswith("artist:"):
                        where_clause = "WHERE artist LIKE ? ESCAPE '\\'"
                        escaped = q_str[7:].strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                        sql_params.append(f"%{escaped}%")
                    elif q_str.startswith("title:"):
                        where_clause = "WHERE title LIKE ? ESCAPE '\\'"
                        escaped = q_str[6:].strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                        sql_params.append(f"%{escaped}%")
                    elif q_str.startswith("path:"):
                        where_clause = "WHERE path = ?"
                        sql_params.append(q_str[5:].strip())
                    elif q_str.startswith("mb_trackid:") or q_str.startswith("mbid:"):
                        where_clause = "WHERE mb_trackid = ?"
                        sql_params.append(q_str.split(":", 1)[1].strip())
                    elif q_str.lower().startswith("singleton:"):
                        s_val = q_str.split(":", 1)[1].strip().lower()
                        if s_val in {"true", "1"}:
                            where_clause = "WHERE (album_id IS NULL OR album_id = 0)"
                        elif s_val in {"false", "0"}:
                            where_clause = "WHERE (album_id IS NOT NULL AND album_id != 0)"
                        else:
                            self._send_json(400, {"error": f"Invalid singleton query value: {s_val!r}"})
                            con.close()
                            return
                    else:
                        escaped = q_str.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                        pattern = f"%{escaped}%"
                        where_clause = "WHERE (title LIKE ? ESCAPE '\\' OR artist LIKE ? ESCAPE '\\' OR album LIKE ? ESCAPE '\\' OR albumartist LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\')"
                        sql_params.extend([pattern, pattern, pattern, pattern, pattern])

                cur.execute(f"SELECT COUNT(*) FROM items {where_clause}", sql_params)
                total_count = cur.fetchone()[0]

                cur.execute(f"SELECT * FROM items {where_clause} ORDER BY id LIMIT ? OFFSET ?", sql_params + [limit, offset])
                rows = [dict(r) for r in cur.fetchall()]
                con.close()

                has_more = (offset + len(rows)) < total_count
                next_offset = (offset + len(rows)) if has_more else None

                self._send_json(200, {
                    "items": rows,
                    "count": len(rows),
                    "total": total_count,
                    "offset": offset,
                    "limit": limit,
                    "has_more": has_more,
                    "next_offset": next_offset
                })
            except Exception as exc:
                self._send_json(500, {"error": f"Database error: {exc}"})
            finally:
                release_os_lock(lock)
            return

        if path.startswith("/items/"):
            try:
                item_id = int(path.split("/")[2])
            except ValueError:
                self._send_json(400, {"error": "Invalid item ID"})
                return

            if not os.path.exists(LIB_PATH):
                self._send_json(404, {"error": "Database file musiclibrary.blb not found"})
                return

            lock = acquire_os_lock(read_only=True)
            try:
                con = sqlite3.connect(LIB_PATH, timeout=10)
                con.row_factory = sqlite3.Row
                cur = con.cursor()
                cur.execute("SELECT * FROM items WHERE id = ?", (item_id,))
                row = cur.fetchone()
                if not row:
                    self._send_json(404, {"error": f"Item {item_id} not found"})
                else:
                    self._send_json(200, {"item": dict(row)})
            except Exception as exc:
                self._send_json(500, {"error": f"Database error: {exc}"})
            finally:
                release_os_lock(lock)
            return

        if path == "/albums":
            query = params.get("query", [None])[0]
            mb_albumid = params.get("mb_albumid", [None])[0]
            mb_releasegroupid = params.get("mb_releasegroupid", [None])[0]
            offset = max(int(params.get("offset", [0])[0]), 0)
            limit = min(max(int(params.get("limit", [500])[0]), 1), 2000)

            if not os.path.exists(LIB_PATH):
                self._send_json(404, {"error": "Database file musiclibrary.blb not found"})
                return

            lock = acquire_os_lock(read_only=True)
            try:
                con = sqlite3.connect(LIB_PATH, timeout=10)
                con.row_factory = sqlite3.Row
                cur = con.cursor()

                where_clause = ""
                sql_params = []
                if mb_albumid:
                    where_clause = "WHERE mb_albumid = ?"
                    sql_params.append(mb_albumid)
                elif mb_releasegroupid:
                    where_clause = "WHERE mb_releasegroupid = ?"
                    sql_params.append(mb_releasegroupid)
                elif query is not None:
                    q_str = str(query).strip()
                    if not q_str:
                        where_clause = "WHERE 1 = 0"
                    elif q_str.startswith("album:"):
                        where_clause = "WHERE album LIKE ? ESCAPE '\\'"
                        escaped = q_str[6:].strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                        sql_params.append(f"%{escaped}%")
                    elif q_str.startswith("artist:"):
                        where_clause = "WHERE albumartist LIKE ? ESCAPE '\\' OR artist LIKE ? ESCAPE '\\'"
                        escaped = q_str[7:].strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                        pattern = f"%{escaped}%"
                        sql_params.extend([pattern, pattern])
                    else:
                        escaped = q_str.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                        pattern = f"%{escaped}%"
                        where_clause = "WHERE (album LIKE ? ESCAPE '\\' OR albumartist LIKE ? ESCAPE '\\' OR artist LIKE ? ESCAPE '\\')"
                        sql_params.extend([pattern, pattern, pattern])

                cur.execute(f"SELECT COUNT(*) FROM albums {where_clause}", sql_params)
                total_count = cur.fetchone()[0]

                cur.execute(f"SELECT * FROM albums {where_clause} ORDER BY id LIMIT ? OFFSET ?", sql_params + [limit, offset])
                rows = [dict(r) for r in cur.fetchall()]
                con.close()

                has_more = (offset + len(rows)) < total_count
                next_offset = (offset + len(rows)) if has_more else None

                self._send_json(200, {
                    "albums": rows,
                    "count": len(rows),
                    "total": total_count,
                    "offset": offset,
                    "limit": limit,
                    "has_more": has_more,
                    "next_offset": next_offset
                })
            except Exception as exc:
                self._send_json(500, {"error": f"Database error: {exc}"})
            finally:
                release_os_lock(lock)
            return

        if path.startswith("/albums/"):
            parts = path.split("/")
            if len(parts) == 3:
                try:
                    album_id = int(parts[2])
                except ValueError:
                    self._send_json(400, {"error": "Invalid album ID"})
                    return

                if not os.path.exists(LIB_PATH):
                    self._send_json(404, {"error": "Database file musiclibrary.blb not found"})
                    return

                lock = acquire_os_lock(read_only=True)
                try:
                    con = sqlite3.connect(LIB_PATH, timeout=10)
                    con.row_factory = sqlite3.Row
                    cur = con.cursor()
                    cur.execute("SELECT * FROM albums WHERE id = ?", (album_id,))
                    arow = cur.fetchone()
                    if not arow:
                        self._send_json(404, {"error": f"Album {album_id} not found"})
                    else:
                        adict = dict(arow)
                        cur.execute("SELECT * FROM items WHERE album_id = ? ORDER BY disc, track", (album_id,))
                        adict["items"] = [dict(r) for r in cur.fetchall()]
                        self._send_json(200, {"album": adict})
                except Exception as exc:
                    self._send_json(500, {"error": f"Database error: {exc}"})
                finally:
                    release_os_lock(lock)
                return

        if path == "/jobs":
            with JOBS_LOCK:
                job_list = [j.to_dict(include_logs=False) for j in JOBS.values()]
            self._send_json(200, {"jobs": job_list})
            return

        if path.startswith("/jobs/"):
            job_id = path[6:]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                self._send_json(404, {"error": "Job not found"})
                return
            self._send_json(200, {"job": job.to_dict(include_logs=True)})
            return

        self._send_json(404, {"error": f"Endpoint not found: {path}"})

    def do_POST(self):
        if not self._authenticate():
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        try:
            content_len = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self._send_json(400, {"error": "Invalid Content-Length"})
            return
        if content_len < 0:
            self._send_json(400, {"error": "Invalid Content-Length"})
            return
        if path == "/config" and content_len > _AGENT_CONFIG_REQUEST_MAX_BYTES:
            self._send_json(413, {"error": "config content is too large", "error_code": "config_too_large"})
            return
        if path == "/playlists/import-staged" and content_len > PLAYLIST_IMPORT_MAX_REQUEST_BYTES:
            self._send_json(413, {"error": "playlist import request is too large", "error_code": "request_too_large"})
            return
        post_data = self.rfile.read(content_len) if content_len > 0 else b"{}"

        try:
            body = json.loads(post_data.decode("utf-8")) if post_data else {}
        except Exception:
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        if path == "/library/raw_query":
            self._send_json(403, {"error": "Raw SQL queries are not permitted"})
            return

        if path == "/config":
            content = body.get("content") if isinstance(body, dict) else None
            if not isinstance(content, str) or not content.strip():
                self._send_json(400, {"error": "content must be a non-empty string", "error_code": "config_empty"})
                return
            try:
                self._send_json(200, _write_agent_config_file(content))
            except ValueError:
                self._send_json(413, {"error": "config content is too large", "error_code": "config_too_large"})
            except PermissionError:
                self._send_json(403, {"error": "config.yaml is not writable", "error_code": "config_permission_denied"})
            except OSError as exc:
                self._send_json(500, {"error": "Could not save config.yaml", "error_code": "config_write_failed", "detail": _redact_agent_status_text(str(exc))})
            return

        if path == "/config/revert":
            try:
                self._send_json(200, _revert_agent_config_file())
            except FileNotFoundError:
                self._send_json(404, {"error": "No backup found", "error_code": "config_backup_not_found"})
            except PermissionError:
                self._send_json(403, {"error": "config.yaml is not writable", "error_code": "config_permission_denied"})
            except OSError as exc:
                self._send_json(500, {"error": "Could not revert config.yaml", "error_code": "config_revert_failed", "detail": _redact_agent_status_text(str(exc))})
            return

        if path.startswith("/albums/") and path.endswith("/art"):
            parts = path.split("/")
            try:
                album_id = int(parts[2])
            except (ValueError, IndexError):
                self._send_json(400, {"error": "Invalid album ID"})
                return
            lock_file = acquire_os_lock(read_only=False)
            con = None
            try:
                con = sqlite3.connect(LIB_PATH, timeout=10)
                con.row_factory = sqlite3.Row
                result = _replace_album_art_locked(con, album_id, body if isinstance(body, dict) else {})
                self._send_json(200, result)
            except AlbumArtOperationError as exc:
                self._send_json(exc.status, {"error": exc.message})
            except UnsafePathError:
                self._send_json(403, {"error": "Access denied for album artwork path"})
            except Exception:
                self._send_json(500, {"error": "Failed to replace album artwork"})
            finally:
                if con is not None:
                    con.close()
                release_os_lock(lock_file)
            return

        if path == "/library/rewrite-path":
            old_raw = body.get("old_path", "")
            new_raw = body.get("new_path", "")
            try:
                old_path = _library_rewrite_path(old_raw, require_exists=False, expected_type=None)
                new_path = _library_rewrite_path(new_raw, require_exists=True, expected_type="file")
            except UnsafePathError:
                self._send_json(403, {"error": "Access denied for library path rewrite"})
                return
            if old_path == new_path:
                self._send_json(400, {"error": "Source and destination paths must differ"})
                return

            lock_file = acquire_os_lock(read_only=False)
            con = None
            try:
                old_path = _library_rewrite_path(str(old_path), require_exists=False, expected_type=None)
                new_path = _library_rewrite_path(str(new_path), require_exists=True, expected_type="file")
                old_rel, old_abs = _library_db_values(old_path)
                new_rel, _new_abs = _library_db_values(new_path)
                con = sqlite3.connect(LIB_PATH, timeout=10)
                cur = con.cursor()
                changed_items = 0
                changed_artpaths = 0
                cur.execute(
                    "UPDATE items SET path=? WHERE path=? OR path=?",
                    (new_rel, old_rel, old_abs),
                )
                changed_items += max(0, cur.rowcount)
                cur.execute(
                    "UPDATE items SET path=? WHERE path=? OR path=?",
                    (new_rel.encode(), old_rel.encode(), old_abs.encode()),
                )
                changed_items += max(0, cur.rowcount)
                # Isolated per-statement: albums.artpath legitimately has no
                # matching row for most item-path rewrites (artpath is a
                # per-album field, not per-item), and a schema/type error on
                # one form must not discard a real count already earned by
                # the other -- unlike the previous behavior, which reset the
                # whole artpath count to 0 on any exception, silently
                # under-reporting (while still committing) a partially
                # successful update.
                try:
                    cur.execute(
                        "UPDATE albums SET artpath=? WHERE artpath=? OR artpath=?",
                        (new_rel, old_rel, old_abs),
                    )
                    changed_artpaths += max(0, cur.rowcount)
                except Exception:
                    pass
                try:
                    cur.execute(
                        "UPDATE albums SET artpath=? WHERE artpath=? OR artpath=?",
                        (new_rel.encode(), old_rel.encode(), old_abs.encode()),
                    )
                    changed_artpaths += max(0, cur.rowcount)
                except Exception:
                    pass
                changed_total = changed_items + changed_artpaths
                if changed_total == 0:
                    con.rollback()
                    self._send_json(404, {"error": "No stored library rows matched the source path"})
                    return
                con.commit()
                self._send_json(200, {
                    "ok": True,
                    "items_changed": changed_items,
                    "artpaths_changed": changed_artpaths,
                    "changed": changed_total,
                    "old_path": str(old_path),
                    "new_path": str(new_path),
                    "stored_path": new_rel,
                })
            except Exception:
                if con is not None:
                    try:
                        con.rollback()
                    except Exception:
                        pass
                self._send_json(500, {"error": "Failed to rewrite library path"})
            finally:
                if con is not None:
                    con.close()
                release_os_lock(lock_file)
            return

        if path.startswith("/albums/") and path.endswith("/artpath"):
            parts = path.split("/")
            try:
                album_id = int(parts[2])
            except ValueError:
                self._send_json(400, {"error": "Invalid album ID"})
                return

            artpath = body.get("artpath", "")
            try:
                safe_artpath = resolve_safe_path(artpath, ["music"])
            except UnsafePathError:
                self._send_json(403, {"error": "Access denied for artpath outside allowed roots"})
                return

            lock_file = acquire_os_lock(read_only=False)
            try:
                con = sqlite3.connect(LIB_PATH, timeout=10)
                con.row_factory = sqlite3.Row
                cur = con.cursor()
                cur.execute("SELECT * FROM albums WHERE id = ?", (album_id,))
                if not cur.fetchone():
                    self._send_json(404, {"error": f"Album {album_id} not found"})
                    return
                try:
                    safe_artpath = resolve_safe_path(artpath, ["music"])
                except UnsafePathError:
                    self._send_json(403, {"error": "Access denied for artpath outside allowed roots"})
                    return
                cur.execute("UPDATE albums SET artpath = ? WHERE id = ?", (str(safe_artpath), album_id))
                con.commit()
                con.close()
                self._send_json(200, {"ok": True, "album_id": album_id, "artpath": str(safe_artpath)})
            except Exception:
                self._send_json(500, {"error": "Failed to set artpath"})
            finally:
                release_os_lock(lock_file)
            return

        if path == "/imports/review/cleanup/plan":
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            allowed_roots = [
                os.environ.get("DOWNLOADS_ROOT", "/downloads"),
                os.environ.get("PLAYLIST_DOWNLOAD_ROOT", "/data/torrents/music/Playlist Downloads"),
                os.environ.get("TORRENT_SOURCE_ROOTS", "/torrents"),
                tempfile.gettempdir(),
                os.environ.get("IMPORT_REVIEW_QUARANTINE_DIR", "/config/import_review_quarantine"),
                music_root_env,
            ]
            res = transaction_engine.execute_import_review_cleanup_plan(
                _txn_store, body, allowed_roots, music_root=music_root_env,
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/imports/review/cleanup/apply":
            op_id = str(body.get("operation_id") or "").strip()
            if not op_id:
                self._send_json(400, {"ok": False, "error": "operation_id required"})
                return
            quarantine_root = os.environ.get("IMPORT_REVIEW_QUARANTINE_DIR", "/config/import_review_quarantine")
            res = transaction_engine.execute_import_review_cleanup_apply(_txn_store, op_id, quarantine_root)
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/albums/cleanup/plan":
            try:
                album_id = int(body.get("album_id") or 0)
            except Exception:
                album_id = 0
            allowed_roots = [
                os.environ.get("DOWNLOADS_ROOT", "/downloads"),
                os.environ.get("MUSIC_ROOT", "/music"),
            ]
            res = transaction_engine.create_album_cleanup_plan(_txn_store, album_id, allowed_roots=allowed_roots, payload=body)
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/albums/cleanup/apply":
            op_id = str(body.get("operation_id") or "").strip()
            if not op_id:
                self._send_json(400, {"ok": False, "error": "operation_id required"})
                return
            res = transaction_engine.execute_album_cleanup_apply(_txn_store, op_id)
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/imports/review/cleanup/rollback":
            op_id = str(body.get("operation_id") or "").strip()
            if not op_id:
                self._send_json(400, {"ok": False, "error": "operation_id required"})
                return
            res = transaction_engine.rollback_import_review_cleanup(_txn_store, op_id)
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/tracks/replacement/plan":
            # SEC-002 Wave 17 final review: role-specific roots, not one
            # combined set. An "original" must be a Beets-owned item under
            # the music library root; a "candidate" must come from an
            # approved acquisition/staging root -- never the reverse, and
            # never accepted merely because a path happens to be under
            # *some* trusted root.
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            original_allowed_roots = [music_root_env]
            candidate_allowed_roots = [
                os.environ.get("DOWNLOADS_ROOT", "/downloads"),
                os.environ.get("PLAYLIST_DOWNLOAD_ROOT", "/data/torrents/music/Playlist Downloads"),
                os.environ.get("TORRENT_SOURCE_ROOTS", "/torrents"),
                tempfile.gettempdir(),
            ]
            res = transaction_engine.create_track_replacement_plan(
                _txn_store, body,
                original_allowed_roots=original_allowed_roots,
                candidate_allowed_roots=candidate_allowed_roots,
                db_path=MUSIC_LIBRARY_PATH,
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/tracks/replacement/apply":
            op_id = str(body.get("operation_id") or "").strip()
            if not op_id:
                self._send_json(400, {"ok": False, "error": "operation_id required"})
                return
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            quarantine_root = os.environ.get("REPLACEMENT_QUARANTINE_DIR", "/config/track_replacement_quarantine")
            original_allowed_roots = [music_root_env]
            candidate_allowed_roots = [
                os.environ.get("DOWNLOADS_ROOT", "/downloads"),
                os.environ.get("PLAYLIST_DOWNLOAD_ROOT", "/data/torrents/music/Playlist Downloads"),
                os.environ.get("TORRENT_SOURCE_ROOTS", "/torrents"),
                tempfile.gettempdir(),
            ]
            res = transaction_engine.execute_track_replacement_apply(
                _txn_store, op_id, db_path=MUSIC_LIBRARY_PATH,
                original_allowed_roots=original_allowed_roots,
                candidate_allowed_roots=candidate_allowed_roots,
                quarantine_allowed_root=quarantine_root,
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/tracks/replacement/rollback":
            op_id = str(body.get("operation_id") or "").strip()
            if not op_id:
                self._send_json(400, {"ok": False, "error": "operation_id required"})
                return
            quarantine_root = os.environ.get("REPLACEMENT_QUARANTINE_DIR", "/config/track_replacement_quarantine")
            res = transaction_engine.rollback_track_replacement(
                _txn_store, op_id, db_path=MUSIC_LIBRARY_PATH, quarantine_allowed_root=quarantine_root,
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/imports/bulk-replacement/plan":
            # SEC-002 Wave 18 final review: bulk_import_replacement_v1 does
            # not own or trigger import (see create_bulk_import_replacement_plan's
            # docstring) -- both the old items being retired AND the new,
            # already-imported replacement items live under the SAME music
            # library root. There is no separate staging/candidate root
            # for this family; unlike track_replacement_v1, a "candidate"
            # here is never an unimported file sitting in a download
            # directory.
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            quarantine_root = os.environ.get("REPLACEMENT_QUARANTINE_DIR", "/config/track_replacement_quarantine")
            res = transaction_engine.create_bulk_import_replacement_plan(
                _txn_store, body,
                original_allowed_roots=[music_root_env],
                db_path=MUSIC_LIBRARY_PATH,
                quarantine_base_root=quarantine_root,
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/imports/bulk-replacement/apply":
            op_id = str(body.get("operation_id") or "").strip()
            if not op_id:
                self._send_json(400, {"ok": False, "error": "operation_id required"})
                return
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            quarantine_root = os.environ.get("REPLACEMENT_QUARANTINE_DIR", "/config/track_replacement_quarantine")
            res = transaction_engine.execute_bulk_import_replacement_apply(
                _txn_store, op_id,
                db_path=MUSIC_LIBRARY_PATH,
                original_allowed_roots=[music_root_env],
                quarantine_base_root=quarantine_root,
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/imports/bulk-replacement/rollback":
            op_id = str(body.get("operation_id") or "").strip()
            if not op_id:
                self._send_json(400, {"ok": False, "error": "operation_id required"})
                return
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            quarantine_root = os.environ.get("REPLACEMENT_QUARANTINE_DIR", "/config/track_replacement_quarantine")
            res = transaction_engine.rollback_bulk_import_replacement(
                _txn_store, op_id,
                db_path=MUSIC_LIBRARY_PATH,
                original_allowed_roots=[music_root_env],
                quarantine_base_root=quarantine_root,
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/albums/mb-track-repair/plan":
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            res = transaction_engine.create_album_mb_track_repair_plan(
                _txn_store, body,
                music_allowed_roots=[music_root_env],
                db_path=MUSIC_LIBRARY_PATH,
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/albums/mb-track-repair/apply":
            op_id = str(body.get("operation_id") or "").strip()
            if not op_id:
                self._send_json(400, {"ok": False, "error": "operation_id required"})
                return
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            write_tags = bool(body.get("write_tags", True))
            res = transaction_engine.execute_album_mb_track_repair_apply(
                _txn_store, op_id,
                db_path=MUSIC_LIBRARY_PATH,
                music_allowed_roots=[music_root_env],
                write_tags=write_tags,
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/albums/mb-track-repair/rollback":
            op_id = str(body.get("operation_id") or "").strip()
            if not op_id:
                self._send_json(400, {"ok": False, "error": "operation_id required"})
                return
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            res = transaction_engine.rollback_album_mb_track_repair(
                _txn_store, op_id,
                db_path=MUSIC_LIBRARY_PATH,
                music_allowed_roots=[music_root_env],
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/albums/existing-reconcile/plan":
            # SEC-002 Wave 20 final review: `source_folder` in the request
            # body must NEVER be trusted to expand allowed filesystem
            # roots by itself -- that is a client-controlled path becoming
            # its own authorization, exactly the trust-boundary defect
            # this review closes. The engine validates any claimed
            # source_folder against this SERVER-configured staging root
            # list (same roots track_replacement/bulk_import_replacement
            # already use for candidate files) before treating anything
            # under it as authorized; an unvalidated source_folder is
            # simply ignored, not silently accepted.
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            quarantine_root = os.environ.get("RECONCILE_QUARANTINE_DIR", "/config/reconcile_quarantine")
            staging_allowed_roots = [
                os.environ.get("DOWNLOADS_ROOT", "/downloads"),
                os.environ.get("PLAYLIST_DOWNLOAD_ROOT", "/data/torrents/music/Playlist Downloads"),
                os.environ.get("TORRENT_SOURCE_ROOTS", "/torrents"),
                tempfile.gettempdir(),
            ]
            res = transaction_engine.create_existing_album_reconcile_plan(
                _txn_store, body,
                music_allowed_roots=[music_root_env],
                staging_allowed_roots=staging_allowed_roots,
                quarantine_base_root=quarantine_root,
                db_path=MUSIC_LIBRARY_PATH,
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/albums/existing-reconcile/apply":
            op_id = str(body.get("operation_id") or "").strip()
            if not op_id:
                self._send_json(400, {"ok": False, "error": "operation_id required"})
                return
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            quarantine_root = os.environ.get("RECONCILE_QUARANTINE_DIR", "/config/reconcile_quarantine")
            staging_allowed_roots = [
                os.environ.get("DOWNLOADS_ROOT", "/downloads"),
                os.environ.get("PLAYLIST_DOWNLOAD_ROOT", "/data/torrents/music/Playlist Downloads"),
                os.environ.get("TORRENT_SOURCE_ROOTS", "/torrents"),
                tempfile.gettempdir(),
            ]
            res = transaction_engine.execute_existing_album_reconcile_apply(
                _txn_store, op_id,
                db_path=MUSIC_LIBRARY_PATH,
                music_allowed_roots=[music_root_env],
                staging_allowed_roots=staging_allowed_roots,
                quarantine_base_root=quarantine_root,
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/albums/existing-reconcile/rollback":
            op_id = str(body.get("operation_id") or "").strip()
            if not op_id:
                self._send_json(400, {"ok": False, "error": "operation_id required"})
                return
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            res = transaction_engine.rollback_existing_album_reconcile(
                _txn_store, op_id,
                db_path=MUSIC_LIBRARY_PATH,
                music_allowed_roots=[music_root_env],
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/artists/reconcile/plan":
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            quarantine_root = os.environ.get("RECONCILE_QUARANTINE_DIR", "/config/reconcile_quarantine")
            res = transaction_engine.create_artist_folder_reconcile_plan(
                _txn_store, body,
                music_allowed_roots=[music_root_env],
                quarantine_base_root=quarantine_root,
                db_path=MUSIC_LIBRARY_PATH,
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/artists/reconcile/apply":
            op_id = str(body.get("operation_id") or "").strip()
            if not op_id:
                self._send_json(400, {"ok": False, "error": "operation_id required"})
                return
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            quarantine_root = os.environ.get("RECONCILE_QUARANTINE_DIR", "/config/reconcile_quarantine")
            res = transaction_engine.execute_artist_folder_reconcile_apply(
                _txn_store, op_id,
                db_path=MUSIC_LIBRARY_PATH,
                music_allowed_roots=[music_root_env],
                quarantine_base_root=quarantine_root,
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/artists/reconcile/rollback":
            op_id = str(body.get("operation_id") or "").strip()
            if not op_id:
                self._send_json(400, {"ok": False, "error": "operation_id required"})
                return
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            res = transaction_engine.rollback_artist_folder_reconcile(
                _txn_store, op_id,
                db_path=MUSIC_LIBRARY_PATH,
                music_allowed_roots=[music_root_env],
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/albums/maintenance/plan":
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            quarantine_root = os.environ.get("RECONCILE_QUARANTINE_DIR", "/config/reconcile_quarantine")
            res = transaction_engine.create_album_maintenance_plan(
                _txn_store, body,
                music_allowed_roots=[music_root_env],
                quarantine_base_root=quarantine_root,
                db_path=MUSIC_LIBRARY_PATH,
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/albums/maintenance/apply":
            op_id = str(body.get("operation_id") or "").strip()
            if not op_id:
                self._send_json(400, {"ok": False, "error": "operation_id required"})
                return
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            quarantine_root = os.environ.get("RECONCILE_QUARANTINE_DIR", "/config/reconcile_quarantine")
            res = transaction_engine.execute_album_maintenance_apply(
                _txn_store, op_id,
                db_path=MUSIC_LIBRARY_PATH,
                music_allowed_roots=[music_root_env],
                quarantine_base_root=quarantine_root,
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/albums/maintenance/rollback":
            op_id = str(body.get("operation_id") or "").strip()
            if not op_id:
                self._send_json(400, {"ok": False, "error": "operation_id required"})
                return
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            res = transaction_engine.rollback_album_maintenance(
                _txn_store, op_id,
                db_path=MUSIC_LIBRARY_PATH,
                music_allowed_roots=[music_root_env],
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/albums/artwork/plan":
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            stg_roots = [os.environ.get("DOWNLOADS_ROOT", "/downloads"), os.environ.get("STAGING_ROOT", "/staging")]
            quarantine_root = os.environ.get("RECONCILE_QUARANTINE_DIR", "/config/reconcile_quarantine")
            res = transaction_engine.create_album_artwork_plan(
                _txn_store, body,
                music_allowed_roots=[music_root_env],
                staging_allowed_roots=stg_roots,
                quarantine_base_root=quarantine_root,
                db_path=MUSIC_LIBRARY_PATH,
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/albums/artwork/apply":
            op_id = str(body.get("operation_id") or "").strip()
            if not op_id:
                self._send_json(400, {"ok": False, "error": "operation_id required"})
                return
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            quarantine_root = os.environ.get("RECONCILE_QUARANTINE_DIR", "/config/reconcile_quarantine")
            res = transaction_engine.execute_album_artwork_apply(
                _txn_store, op_id,
                db_path=MUSIC_LIBRARY_PATH,
                music_allowed_roots=[music_root_env],
                quarantine_base_root=quarantine_root,
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/albums/artwork/rollback":
            op_id = str(body.get("operation_id") or "").strip()
            if not op_id:
                self._send_json(400, {"ok": False, "error": "operation_id required"})
                return
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            res = transaction_engine.rollback_album_artwork(
                _txn_store, op_id,
                db_path=MUSIC_LIBRARY_PATH,
                music_allowed_roots=[music_root_env],
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        # ── import_folder_v1 ──────────────────────────────────────────────────
        if path == "/import/plan":
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            stg_roots = [os.environ.get("DOWNLOADS_ROOT", "/downloads"), os.environ.get("STAGING_ROOT", "/staging"), music_root_env]
            quarantine_root = os.environ.get("RECONCILE_QUARANTINE_DIR", "/config/reconcile_quarantine")
            res = transaction_engine.create_import_folder_plan(
                _txn_store, body,
                db_path=MUSIC_LIBRARY_PATH,
                music_allowed_roots=[music_root_env],
                staging_allowed_roots=stg_roots,
                quarantine_base_root=quarantine_root,
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/import/apply":
            op_id = str(body.get("operation_id") or "").strip()
            if not op_id:
                self._send_json(400, {"ok": False, "error": "operation_id required"})
                return
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            quarantine_root = os.environ.get("RECONCILE_QUARANTINE_DIR", "/config/reconcile_quarantine")
            res = transaction_engine.execute_import_folder_apply(
                _txn_store, op_id,
                db_path=MUSIC_LIBRARY_PATH,
                music_allowed_roots=[music_root_env],
                quarantine_base_root=quarantine_root,
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/import/rollback":
            op_id = str(body.get("operation_id") or "").strip()
            if not op_id:
                self._send_json(400, {"ok": False, "error": "operation_id required"})
                return
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            res = transaction_engine.rollback_import_folder(
                _txn_store, op_id,
                db_path=MUSIC_LIBRARY_PATH,
                music_allowed_roots=[music_root_env],
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        # ── folder_cleanup_v1 ─────────────────────────────────────────────────
        if path == "/folders/cleanup/plan":
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            res = transaction_engine.create_folder_cleanup_plan(
                _txn_store, body,
                db_path=MUSIC_LIBRARY_PATH,
                music_allowed_roots=[music_root_env],
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/folders/cleanup/apply":
            op_id = str(body.get("operation_id") or "").strip()
            if not op_id:
                self._send_json(400, {"ok": False, "error": "operation_id required"})
                return
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            res = transaction_engine.execute_folder_cleanup_apply(
                _txn_store, op_id,
                db_path=MUSIC_LIBRARY_PATH,
                music_allowed_roots=[music_root_env],
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/folders/cleanup/rollback":
            op_id = str(body.get("operation_id") or "").strip()
            if not op_id:
                self._send_json(400, {"ok": False, "error": "operation_id required"})
                return
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            res = transaction_engine.rollback_folder_cleanup(
                _txn_store, op_id,
                music_allowed_roots=[music_root_env],
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        # ── playlist_media_cleanup_v1 ──────────────────────────────────────────
        if path == "/playlists/media-cleanup/plan":
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            quarantine_root = os.environ.get("RECONCILE_QUARANTINE_DIR", "/config/reconcile_quarantine")
            res = transaction_engine.create_playlist_media_cleanup_plan(
                _txn_store, body,
                db_path=MUSIC_LIBRARY_PATH,
                music_allowed_roots=[music_root_env],
                quarantine_base_root=quarantine_root,
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/playlists/media-cleanup/apply":
            op_id = str(body.get("operation_id") or "").strip()
            if not op_id:
                self._send_json(400, {"ok": False, "error": "operation_id required"})
                return
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            quarantine_root = os.environ.get("RECONCILE_QUARANTINE_DIR", "/config/reconcile_quarantine")
            res = transaction_engine.execute_playlist_media_cleanup_apply(
                _txn_store, op_id,
                db_path=MUSIC_LIBRARY_PATH,
                music_allowed_roots=[music_root_env],
                quarantine_base_root=quarantine_root,
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return

        if path == "/playlists/media-cleanup/rollback":
            op_id = str(body.get("operation_id") or "").strip()
            if not op_id:
                self._send_json(400, {"ok": False, "error": "operation_id required"})
                return
            music_root_env = os.environ.get("MUSIC_ROOT", "/music")
            res = transaction_engine.rollback_playlist_media_cleanup(
                _txn_store, op_id,
                db_path=MUSIC_LIBRARY_PATH,
                music_allowed_roots=[music_root_env],
            )
            code = 200 if res.get("ok") else 400
            self._send_json(code, res)
            return





        if path == "/imports/source/inspect":
            # SEC-002 Wave 8 ARCH-003: read-only, bounded inspection of an
            # import/reimport source -- the engine-side counterpart to
            # web-manager path validation the web manager can no longer do
            # itself (it has no local view of MUSIC_LIBRARY_PATH/
            # DOWNLOAD_PATH in the shipped Compose topology). Not a general
            # filesystem browser: it only validates against a fixed,
            # operation-specific root policy and returns audio evidence,
            # never raw file contents.
            source_path = body.get("source_path", "")
            operation = body.get("operation", "")
            result = inspect_import_source(source_path, operation)
            if not result.get("ok"):
                status = {
                    "invalid_operation": 400,
                    "invalid_path": 403,
                    "root_self_rejected": 403,
                }.get(result.get("error_code"), 500)
                self._send_json(status, {"ok": False, "error": result.get("error_code", "inspection_failed")})
                return
            self._send_json(200, result)
            return

        if path == "/imports/source/discover":
            source_path = body.get("source_path", "")
            operation = body.get("operation", "ai_batch_discovery")
            cursor = body.get("cursor")
            limits = body.get("limits")
            result = discover_import_sources(source_path, operation, cursor, limits)
            if not result.get("ok"):
                status = {
                    "invalid_operation": 400,
                    "invalid_path": 403,
                    "root_self_rejected": 403,
                    "invalid_cursor": 400,
                }.get(result.get("error_code"), 500)
                self._send_json(status, {"ok": False, "error": result.get("error_code", "discovery_failed")})
                return
            self._send_json(200, result)
            return

        if path == "/imports/source/preserve":
            source_path = body.get("source_path", "")
            expected_source_signature = body.get("expected_source_signature")
            plan_id = body.get("plan_id")
            result = preserve_import_source(source_path, expected_source_signature, plan_id)
            if not result.get("ok"):
                status = {
                    "invalid_path": 403,
                    "root_self_rejected": 403,
                    "stale_source": 409,
                    "collision": 409,
                    "insufficient_space": 400,
                    "copy_failed": 500,
                }.get(result.get("error_code"), 500)
                self._send_json(status, {"ok": False, "error": result.get("error_code", "preservation_failed"), "message": result.get("message", "")})
                return
            self._send_json(200, result)
            return

        if path == "/imports/reimport":
            source_path = body.get("source_path", "")
            expected_source_signature = body.get("expected_source_signature")
            expected_deterministic_identity = body.get("expected_deterministic_identity")
            beets_options = body.get("beets_options")
            result = reimport_source_atomic(
                source_path, expected_source_signature, expected_deterministic_identity, beets_options
            )
            if not result.get("ok"):
                status = {
                    "invalid_path": 403,
                    "root_self_rejected": 403,
                    "stale_source": 409,
                    "identity_mismatch": 409,
                    "review_required": 409,
                    "import_failed": 500,
                }.get(result.get("error_code"), 500)
                self._send_json(status, {"ok": False, "error": result.get("error_code", "reimport_failed"), "message": result.get("message", "")})
                return
            self._send_json(200, result)
            return

        if path == "/commands/execute":
            command = body.get("command")
            args = body.get("args", [])
            timeout = body.get("timeout", 120)
            config_override = body.get("config_override", "")
            source_path = body.get("source_path", "")

            if not command or command not in ALLOWED_COMMANDS:
                self._send_json(400, {"error": f"Command '{command}' is not in the allowlist"})
                return

            capability_error = require_command_capability(command)
            if capability_error is not None:
                self._send_json(409, capability_error)
                return

            safe_source_path = None
            if source_path:
                allowed_roots = ["staging"] if command == "import" else ["music", "staging"]
                try:
                    safe_source_path = resolve_safe_path(source_path, allowed_roots)
                except UnsafePathError:
                    self._send_json(403, {"error": "Access denied for source_path outside allowed roots"})
                    return

            try:
                sanitized_args = _sanitize_command_path_args(args, ["music", "staging", "config", "tmp"])
            except UnsafePathError:
                self._send_json(403, {"error": "Invalid path parameter in command args"})
                return

            cmd_list = [command]
            if safe_source_path is not None:
                cmd_list.append(str(safe_source_path))
            cmd_list.extend(sanitized_args)

            mutating = command in {
                "import", "update", "write", "move", "modify", "mbsync",
                "fetchart", "embedart", "lastgenre", "alt", "remove", "rm"
            }

            lock_file = acquire_os_lock(read_only=not mutating)
            tmp_cfg_path = None
            try:
                full_cmd = [BEET_BIN]
                if config_override:
                    tmp_cfg_path = f"/tmp/beets_exec_cfg_{uuid.uuid4().hex}.yaml"
                    with open(tmp_cfg_path, "w", encoding="utf-8") as f:
                        f.write(config_override)
                    os.chmod(tmp_cfg_path, 0o600)
                    full_cmd.extend(["-c", tmp_cfg_path])

                full_cmd.extend(cmd_list)
                env = os.environ.copy()
                env["BEETSDIR"] = BEETSDIR
                res = subprocess.run(
                    full_cmd,
                    capture_output=True,
                    text=True,
                    timeout=float(timeout),
                    env=env
                )
                self._send_json(200, {
                    "returncode": res.returncode,
                    "stdout": res.stdout,
                    "stderr": res.stderr,
                })
            except subprocess.TimeoutExpired:
                self._send_json(408, {"error": f"Command '{command}' timed out after {timeout}s", "returncode": 124})
            except Exception:
                self._send_json(500, {"error": "Command execution error"})
            finally:
                if tmp_cfg_path and os.path.exists(tmp_cfg_path):
                    try:
                        os.unlink(tmp_cfg_path)
                    except Exception:
                        pass
                release_os_lock(lock_file)
            return

        if path == "/jobs/create":
            command = body.get("command")
            args = body.get("args", [])
            label = body.get("label", "")
            config_override = body.get("config_override", "")
            source_path = body.get("source_path", "")

            if not command or command not in ALLOWED_COMMANDS:
                self._send_json(400, {"error": f"Command '{command}' is not in the allowlist"})
                return

            capability_error = require_command_capability(command)
            if capability_error is not None:
                self._send_json(409, capability_error)
                return

            safe_source_path = None
            if source_path:
                allowed_roots = ["staging"] if command == "import" else ["music", "staging"]
                try:
                    safe_source_path = resolve_safe_path(source_path, allowed_roots)
                except UnsafePathError:
                    self._send_json(403, {"error": "Access denied for source_path outside allowed roots"})
                    return

            try:
                sanitized_args = _sanitize_command_path_args(args, ["music", "staging", "config", "tmp"])
            except UnsafePathError:
                self._send_json(403, {"error": "Invalid path parameter in job args"})
                return

            job_id = uuid.uuid4().hex
            cmd_list = [command]
            if safe_source_path is not None:
                cmd_list.append(str(safe_source_path))
            cmd_list.extend(sanitized_args)

            job = AgentJob(job_id, cmd_list, label=label, config_override=config_override)
            with JOBS_LOCK:
                JOBS[job_id] = job

            self._send_json(200, {"job_id": job_id, "status": "started"})
            return

        if path.startswith("/jobs/") and path.endswith("/cancel"):
            parts = path.split("/")
            job_id = parts[2]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                self._send_json(404, {"error": "Job not found"})
                return
            job.cancel()
            self._send_json(200, {"job_id": job_id, "status": "cancel_requested"})
            return

        if path == "/tags/read":
            file_path = body.get("file_path", "")
            try:
                safe_file_path = resolve_safe_path(file_path, ["music", "staging"])
            except UnsafePathError:
                self._send_json(403, {"error": "Access denied for path outside allowed roots"})
                return
            if not safe_file_path.exists():
                self._send_json(404, {"error": "File not found"})
                return

            try:
                self._send_json(200, {"tags": _read_media_tags_best_effort(safe_file_path)})
            except Exception:
                self._send_json(500, {"error": "Failed to read tags"})
            return

        if path == "/tags/write":
            file_path = body.get("file_path", "")
            tags = body.get("tags", {})
            try:
                safe_file_path = resolve_safe_path(file_path, ["music", "staging"])
            except UnsafePathError:
                self._send_json(403, {"error": "Access denied for path outside allowed roots"})
                return
            if not safe_file_path.exists():
                self._send_json(404, {"error": "File not found"})
                return
            if not isinstance(tags, dict):
                self._send_json(400, {"error": "tags must be an object"})
                return
            unsafe_tags = sorted(str(k) for k in tags.keys() if str(k) not in _TAG_WRITE_FIELDS)
            if unsafe_tags:
                self._send_json(400, {"error": "Unsupported tag field"})
                return

            lock_file = acquire_os_lock(read_only=False)
            try:
                # Re-resolve inside the lock: the caller's path is re-validated
                # (not just re-checked-for-existence) to close the TOCTOU gap
                # between the pre-lock check above and lock acquisition.
                try:
                    safe_file_path = resolve_safe_path(file_path, ["music", "staging"])
                except UnsafePathError:
                    self._send_json(403, {"error": "Access denied for path outside allowed roots"})
                    return
                if not safe_file_path.exists():
                    self._send_json(403, {"error": "Access denied for path outside allowed roots"})
                    return
                written = False
                try:
                    from beets.mediafile import MediaFile
                    mf = MediaFile(safe_file_path)
                    for k, v in tags.items():
                        if hasattr(mf, k):
                            setattr(mf, k, v)
                    mf.save()
                    written = True
                except Exception:
                    import mutagen
                    f = mutagen.File(safe_file_path, easy=True)
                    if f is not None:
                        for k, v in tags.items():
                            f[k] = v
                        f.save()
                        written = True
                if written:
                    self._send_json(200, {"ok": True, "file_path": str(safe_file_path)})
                else:
                    self._send_json(500, {"error": "Failed to write tags to file"})
            except Exception:
                self._send_json(500, {"error": "Failed to write tags"})
            finally:
                release_os_lock(lock_file)
            return
        if path == "/files/move":
            src = body.get("source_path", "")
            dst = body.get("target_path", "")
            try:
                safe_src = resolve_safe_path(src, ["music", "staging"])
                safe_dst = resolve_safe_path(dst, ["music", "staging"])
            except UnsafePathError:
                self._send_json(403, {"error": "Access denied for path outside allowed roots"})
                return
            if not safe_src.exists():
                self._send_json(404, {"error": "Source path not found"})
                return

            lock_file = acquire_os_lock(read_only=False)
            try:
                # Re-resolve inside the lock to close the TOCTOU gap between
                # the pre-lock check above and lock acquisition.
                try:
                    safe_src = resolve_safe_path(src, ["music", "staging"])
                    safe_dst = resolve_safe_path(dst, ["music", "staging"])
                except UnsafePathError:
                    self._send_json(403, {"error": "Access denied for path outside allowed roots"})
                    return
                if not safe_src.exists():
                    self._send_json(403, {"error": "Access denied for path outside allowed roots"})
                    return
                safe_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(safe_src), str(safe_dst))
                self._send_json(200, {"ok": True, "source_path": str(safe_src), "target_path": str(safe_dst)})
            except Exception:
                self._send_json(500, {"error": "Failed to move file"})
            finally:
                release_os_lock(lock_file)
            return

        if path == "/files/delete":
            target = body.get("path", "")
            try:
                safe_target = resolve_safe_path(target, ["music", "staging"])
            except UnsafePathError:
                self._send_json(403, {"error": "Access denied for path outside allowed roots"})
                return
            if any(str(safe_target) == os.path.realpath(root) for root in _allowed_root_paths(["music", "staging"])):
                self._send_json(403, {"error": "Refusing to delete an allowed-root directory itself"})
                return
            if not safe_target.exists():
                self._send_json(200, {"ok": True, "path": str(safe_target), "existed": False})
                return

            lock_file = acquire_os_lock(read_only=False)
            try:
                # Re-resolve inside the lock to close the TOCTOU gap between
                # the pre-lock check above and lock acquisition.
                try:
                    safe_target = resolve_safe_path(target, ["music", "staging"])
                except UnsafePathError:
                    self._send_json(403, {"error": "Access denied for path outside allowed roots"})
                    return
                if safe_target.is_dir():
                    shutil.rmtree(safe_target)
                else:
                    safe_target.unlink()
                self._send_json(200, {"ok": True, "path": str(safe_target), "existed": True})
            except Exception:
                self._send_json(500, {"error": "Failed to delete path"})
            finally:
                release_os_lock(lock_file)
            return

        if path == "/files/mkdir":
            target = body.get("path", "")
            try:
                safe_target = resolve_safe_path(target, ["music", "staging"])
            except UnsafePathError:
                self._send_json(403, {"error": "Access denied for path outside allowed roots"})
                return

            lock_file = acquire_os_lock(read_only=False)
            try:
                # Re-resolve inside the lock to close the TOCTOU gap between
                # the pre-lock check above and lock acquisition.
                try:
                    safe_target = resolve_safe_path(target, ["music", "staging"])
                except UnsafePathError:
                    self._send_json(403, {"error": "Access denied for path outside allowed roots"})
                    return
                safe_target.mkdir(parents=True, exist_ok=True)
                self._send_json(200, {"ok": True, "path": str(safe_target)})
            except Exception:
                self._send_json(500, {"error": "Failed to create directory"})
            finally:
                release_os_lock(lock_file)
            return

        if path == "/files/hardlink":
            src = body.get("source_path", "")
            dst = body.get("target_path", "")
            expected_size = body.get("expected_size")

            # Fixed roles, not caller-chosen: this endpoint exists for one
            # operation (qBittorrent hardlink repair -- relink a torrent-side
            # gap from the authoritative library copy), so the source must
            # resolve under the music library and the destination under a
            # torrent/staging or tmp root. It is deliberately not a generic
            # cross-root hardlink primitive.
            source_roots = ["music"]
            dest_roots = ["staging", "tmp"]

            if expected_size is not None:
                if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size <= 0:
                    self._send_json(400, {"error": "Invalid expected_size value"})
                    return

            # Note on symlinks: resolve_safe_path() fully dereferences
            # symlinks via realpath() before returning, so safe_src/safe_dst
            # are already the resolved target -- is_symlink() on them (or on
            # the raw caller-supplied strings) cannot change the actual
            # security boundary here. Containment is enforced on the fully
            # resolved path either way, so a symlink source/destination
            # grants no capability beyond what naming its resolved target
            # directly would; and the identity check against a stat taken
            # under lock immediately before os.link() (below) already closes
            # the practical TOCTOU window regardless of whether a symlink
            # was involved. An earlier version of this endpoint checked
            # os.path.islink() on the raw request strings specifically to
            # reject symlinks outright; that check was both ineffective (see
            # above) and, when corrected to check the raw string, introduced
            # a genuine CodeQL py/path-injection flow (a fresh filesystem
            # call fed directly by unresolved request data) for no real
            # security benefit, so it was removed rather than reintroduced
            # in a disguised form.
            try:
                safe_src = resolve_safe_path(src, source_roots)
                safe_dst = resolve_safe_path(dst, dest_roots)
            except UnsafePathError:
                self._send_json(403, {"error": "Access denied for path outside allowed roots"})
                return

            if not safe_src.exists() or not safe_src.is_file():
                self._send_json(400, {"error": "Source path must be a regular non-symlink file"})
                return

            lock_file = acquire_os_lock(read_only=False)
            try:
                try:
                    safe_src = resolve_safe_path(src, source_roots)
                    safe_dst = resolve_safe_path(dst, dest_roots)
                except UnsafePathError:
                    self._send_json(403, {"error": "Access denied for path outside allowed roots"})
                    return

                if not safe_src.exists() or not safe_src.is_file():
                    self._send_json(400, {"error": "Source path must be a regular non-symlink file"})
                    return

                src_stat = safe_src.stat()
                if expected_size is not None:
                    if src_stat.st_size != expected_size:
                        self._send_json(400, {"error": "Source file size does not match expected size"})
                        return

                if safe_dst.exists():
                    if safe_dst.is_symlink():
                        self._send_json(409, {"error": "Target path exists and is a symlink"})
                        return
                    dst_stat = safe_dst.stat()
                    if dst_stat.st_dev == src_stat.st_dev and dst_stat.st_ino == src_stat.st_ino:
                        self._send_json(200, {"ok": True, "already_present": True})
                        return
                    else:
                        self._send_json(409, {"error": "Target path exists and is a different file"})
                        return

                safe_dst.parent.mkdir(parents=True, exist_ok=True)

                # mkdir(exist_ok=True) accepts a pre-existing parent without
                # checking whether it is a symlink; re-validate the parent
                # explicitly so a symlink swapped in between the check above
                # and this point cannot redirect the link outside the
                # destination root.
                if safe_dst.parent.is_symlink():
                    self._send_json(500, {"error": "Failed to create hardlink"})
                    return
                try:
                    safe_dst_parent_reresolved = resolve_safe_path(str(safe_dst.parent), dest_roots)
                except UnsafePathError:
                    self._send_json(500, {"error": "Failed to create hardlink"})
                    return
                if safe_dst_parent_reresolved != safe_dst.parent:
                    self._send_json(500, {"error": "Failed to create hardlink"})
                    return

                parent_stat = safe_dst.parent.stat()

                if src_stat.st_dev != parent_stat.st_dev:
                    self._send_json(400, {"error": "Cross-device hardlink not supported"})
                    return

                try:
                    os.link(str(safe_src), str(safe_dst), follow_symlinks=False)
                except NotImplementedError:
                    # This production target is Linux, where follow_symlinks=False
                    # is always supported for os.link(); fail closed rather than
                    # silently falling back to a call that could follow a
                    # symlink source on a platform that lacks the guarantee.
                    self._send_json(500, {"error": "Failed to create hardlink"})
                    return
                except OSError as ex:
                    if ex.errno == errno.EXDEV:
                        self._send_json(400, {"error": "Cross-device hardlink not supported"})
                    elif ex.errno == errno.EEXIST:
                        self._send_json(409, {"error": "Target path exists"})
                    else:
                        self._send_json(500, {"error": "Failed to create hardlink"})
                    return

                if not safe_dst.exists() or safe_dst.is_symlink():
                    try:
                        if safe_dst.exists() or safe_dst.is_symlink():
                            safe_dst.unlink()
                    except Exception:
                        pass
                    self._send_json(500, {"error": "Post-link verification failed"})
                    return

                dst_stat_new = safe_dst.stat()
                if dst_stat_new.st_dev != src_stat.st_dev or dst_stat_new.st_ino != src_stat.st_ino:
                    try:
                        safe_dst.unlink()
                    except Exception:
                        pass
                    self._send_json(500, {"error": "Post-link identity mismatch"})
                    return

                self._send_json(200, {"ok": True, "linked": True})
            except Exception:
                self._send_json(500, {"error": "Failed to create hardlink"})
            finally:
                release_os_lock(lock_file)
            return

        if path == "/playlists/staging/ensure":
            playlist_key = str(body.get("playlist_key") or "").strip()
            if not playlist_key or not re.match(r"^[a-zA-Z0-9_.-]{1,160}$", playlist_key) or ".." in playlist_key:
                self._send_json(400, {"error": "Invalid or missing playlist_key"})
                return

            staging_root = PLAYLIST_DOWNLOAD_ROOT.resolve(strict=False)
            playlist_dir = staging_root / playlist_key
            try:
                safe_playlist_dir = resolve_safe_path(str(playlist_dir), ["staging"])
            except UnsafePathError:
                self._send_json(403, {"error": "Access denied for staging path outside allowed roots"})
                return

            lock_file = acquire_os_lock(read_only=False)
            try:
                try:
                    safe_playlist_dir = resolve_safe_path(str(playlist_dir), ["staging"])
                except UnsafePathError:
                    self._send_json(403, {"error": "Access denied for staging path outside allowed roots"})
                    return
                safe_playlist_dir.mkdir(parents=True, exist_ok=True)
                downloads_dir = safe_playlist_dir / "downloads"
                imports_dir = safe_playlist_dir / "imports"
                downloads_dir.mkdir(parents=True, exist_ok=True)
                imports_dir.mkdir(parents=True, exist_ok=True)
                self._send_json(200, {
                    "ok": True,
                    "staging_root": str(safe_playlist_dir),
                    "downloads_dir": str(downloads_dir),
                    "imports_dir": str(imports_dir),
                })
            except Exception:
                self._send_json(500, {"error": "Failed to ensure playlist staging directory"})
            finally:
                release_os_lock(lock_file)
            return

        if path == "/playlists/staging/delete-track":
            playlist_key = str(body.get("playlist_key") or "").strip()
            track_id = str(body.get("track_id") or "").strip()
            requested_path = str(body.get("requested_path") or "").strip()

            if not playlist_key or not re.match(r"^[a-zA-Z0-9_.-]{1,160}$", playlist_key) or ".." in playlist_key:
                self._send_json(400, {"error": "Invalid or missing playlist_key"})
                return

            staging_root = PLAYLIST_DOWNLOAD_ROOT.resolve(strict=False)
            playlist_staging = (staging_root / playlist_key).resolve(strict=False)
            music_root = MUSIC_ROOT.resolve(strict=False)

            if not requested_path:
                self._send_json(400, {"error": "Missing requested_path"})
                return

            try:
                safe_target = resolve_safe_path(requested_path, ["staging"])
            except UnsafePathError:
                self._send_json(403, {"error": "Access denied for path outside allowed roots"})
                return

            if _path_is_within(str(safe_target), str(music_root)):
                self._send_json(403, {"error": "Refusing to delete a Beets library file"})
                return

            # Containment MUST be scoped to this playlist's own staging subtree,
            # not the shared staging_root every playlist lives under -- an "OR
            # staging_root" clause here is meaningless (every playlist's files
            # are also "under staging_root"), and would let a caller for
            # playlist A delete a file that actually belongs to playlist B
            # (SEC-002 Wave 9 second final review: engine cross-playlist
            # deletion gap). track_id is not independently authoritative here
            # either -- the engine has no manifest/track-state access, so the
            # web-manager layer is required to resolve track_id to the
            # server-owned staged path and pass that (not a raw browser value)
            # as requested_path before calling this endpoint.
            if not _path_is_within(str(safe_target), str(playlist_staging)):
                self._send_json(403, {"error": "Refusing to delete a file outside this playlist's own staging directory"})
                return

            if str(safe_target) in (str(staging_root), str(playlist_staging), str(music_root)) or safe_target.is_dir():
                self._send_json(403, {"error": "Refusing to delete staging root directory"})
                return

            ext = safe_target.suffix.lower()
            if ext not in _import_source_audio_extensions():
                self._send_json(403, {"error": "Refusing to delete a non-audio staging file"})
                return

            if _path_has_symlink_component(safe_target, staging_root):
                self._send_json(403, {"error": "Refusing to delete a symlinked path"})
                return

            if not safe_target.exists():
                self._send_json(200, {"ok": True, "deleted": False, "already_absent": True, "path": str(safe_target)})
                return

            lock_file = acquire_os_lock(read_only=False)
            try:
                try:
                    safe_target = resolve_safe_path(requested_path, ["staging"])
                except UnsafePathError:
                    self._send_json(403, {"error": "Access denied for path outside allowed roots"})
                    return
                if _path_is_within(str(safe_target), str(music_root)):
                    self._send_json(403, {"error": "Refusing to delete a Beets library file"})
                    return
                if not _path_is_within(str(safe_target), str(playlist_staging)):
                    self._send_json(403, {"error": "Refusing to delete a file outside this playlist's own staging directory"})
                    return
                if safe_target.is_dir() or str(safe_target) in (str(staging_root), str(playlist_staging)):
                    self._send_json(403, {"error": "Refusing to delete staging root directory"})
                    return
                if _path_has_symlink_component(safe_target, staging_root):
                    self._send_json(403, {"error": "Refusing to delete a symlinked path"})
                    return

                if safe_target.exists():
                    safe_target.unlink()
                    self._send_json(200, {"ok": True, "deleted": True, "already_absent": False, "path": str(safe_target)})
                else:
                    self._send_json(200, {"ok": True, "deleted": False, "already_absent": True, "path": str(safe_target)})
            except Exception:
                self._send_json(500, {"error": "Failed to delete staged track file"})
            finally:
                release_os_lock(lock_file)
            return

        if path == "/playlists/staging/inspect-track":
            playlist_key = str(body.get("playlist_key") or "").strip()
            track_id = str(body.get("track_id") or "").strip()
            requested_path = str(body.get("requested_path") or "").strip()

            if not playlist_key or not re.match(r"^[a-zA-Z0-9_.-]{1,160}$", playlist_key) or ".." in playlist_key:
                self._send_json(400, {"error": "Invalid or missing playlist_key", "error_code": "invalid_playlist_key"})
                return
            if not requested_path:
                self._send_json(400, {"error": "Missing requested_path", "error_code": "missing_requested_path"})
                return

            staging_root = PLAYLIST_DOWNLOAD_ROOT.resolve(strict=False)
            music_root = MUSIC_ROOT.resolve(strict=False)

            def _inspect_result(*, exists: bool, authorized: bool, status: str) -> None:
                self._send_json(200, {
                    "ok": True,
                    "exists": bool(exists),
                    "authorized": bool(authorized),
                    "playlist_key": playlist_key,
                    "track_id": track_id,
                    "status": status,
                })

            try:
                safe_target = resolve_safe_path(requested_path, ["staging"])
            except UnsafePathError:
                _inspect_result(exists=False, authorized=False, status="invalid_path")
                return

            if _path_is_within(str(safe_target), str(music_root)):
                _inspect_result(exists=False, authorized=False, status="library_path")
                return
            try:
                relative_parts = safe_target.relative_to(staging_root).parts
            except ValueError:
                _inspect_result(exists=False, authorized=False, status="invalid_path")
                return
            if not relative_parts or relative_parts[0] != playlist_key:
                _inspect_result(exists=False, authorized=False, status="outside_playlist_staging")
                return
            if len(relative_parts) == 1:
                _inspect_result(exists=False, authorized=False, status="root_or_directory")
                return
            if _path_has_symlink_component(safe_target, staging_root):
                _inspect_result(exists=False, authorized=False, status="symlink_path")
                return

            try:
                verified_target = resolve_safe_path(requested_path, ["staging"], require_exists=True, expected_type="file")
            except UnsafePathError as exc:
                msg = str(exc).lower()
                if "does not exist" in msg:
                    _inspect_result(exists=False, authorized=False, status="missing")
                    return
                if "not a regular file" in msg or "not a file" in msg:
                    _inspect_result(exists=True, authorized=False, status="not_file")
                    return
                _inspect_result(exists=False, authorized=False, status="invalid_path")
                return
            if verified_target.suffix.lower() not in _import_source_audio_extensions():
                _inspect_result(exists=True, authorized=False, status="non_audio")
                return
            _inspect_result(exists=True, authorized=True, status="ok")
            return

        if path == "/playlists/import-staged":
            playlist_key = str(body.get("playlist_key") or "").strip()
            playlist_id = str(body.get("playlist_id") or "").strip()
            operation_id = str(body.get("operation_id") or "").strip()
            tracks_payload = body.get("tracks")
            if not isinstance(tracks_payload, list):
                tracks_payload = []

            if not _valid_playlist_key(playlist_key):
                self._send_json(400, {"error": "Invalid or missing playlist_key", "error_code": "invalid_playlist_key"})
                return
            if playlist_id and not _valid_playlist_id(playlist_id):
                self._send_json(400, {"error": "Invalid playlist_id", "error_code": "invalid_playlist_id"})
                return
            if not _valid_playlist_import_operation_id(operation_id):
                self._send_json(400, {"error": "Invalid or missing operation_id", "error_code": "invalid_operation_id"})
                return
            if len(tracks_payload) > PLAYLIST_IMPORT_MAX_TRACKS:
                self._send_json(413, {"error": "Too many playlist import tracks", "error_code": "too_many_tracks"})
                return

            staging_root = PLAYLIST_DOWNLOAD_ROOT.resolve(strict=False)
            music_root = MUSIC_ROOT.resolve(strict=False)

            try:
                safe_playlist_staging = resolve_safe_path(str(PLAYLIST_DOWNLOAD_ROOT / playlist_key), ["staging"])
                safe_playlist_staging.relative_to(staging_root)
                operations_root = _playlist_import_operations_root()
                index_path = _playlist_import_operations_index_path(operations_root)
            except (ValueError, UnsafePathError):
                self._send_json(400, {"error": "Invalid playlist staging root", "error_code": "invalid_playlist_key"})
                return

            lock_file = acquire_os_lock(read_only=False)
            tmp_cfg_path = ""
            try:
                if _path_has_symlink_component(safe_playlist_staging, staging_root):
                    self._send_json(403, {"error": "Playlist staging path is not safe", "error_code": "playlist_staging_symlink"})
                    return
                if operations_root.exists() and _path_has_symlink_component(operations_root, staging_root, include_leaf=True):
                    self._send_json(403, {"error": "Playlist import operation state is not safe", "error_code": "operation_state_symlink"})
                    return

                try:
                    index_state = _playlist_import_read_state(index_path)
                except UnsafePathError:
                    self._send_json(409, {
                        "ok": False,
                        "status": "review_required",
                        "error_code": "operation_state_corrupt",
                        "message": _safe_playlist_import_error("operation_state_corrupt"),
                    })
                    return
                operations = index_state.get("operations") if isinstance(index_state.get("operations"), dict) else {}
                if index_state and not isinstance(index_state.get("operations"), dict):
                    self._send_json(409, {
                        "ok": False,
                        "status": "review_required",
                        "error_code": "operation_state_corrupt",
                        "message": _safe_playlist_import_error("operation_state_corrupt"),
                    })
                    return
                for known_operation_id, known_state in operations.items():
                    if not _valid_playlist_import_operation_id(str(known_operation_id)):
                        self._send_json(409, {"ok": False, "status": "review_required", "error_code": "operation_state_corrupt"})
                        return
                    if not isinstance(known_state, dict) or not _valid_playlist_import_storage_key(str(known_state.get("storage_key") or "")):
                        self._send_json(409, {"ok": False, "status": "review_required", "error_code": "operation_state_corrupt"})
                        return

                operation_state = operations.get(operation_id) if isinstance(operations.get(operation_id), dict) else {}
                if operation_state.get("playlist_key") and operation_state.get("playlist_key") != playlist_key:
                    self._send_json(409, {"ok": False, "status": "review_required", "error_code": "operation_playlist_mismatch"})
                    return
                if operation_state.get("playlist_id") and playlist_id and operation_state.get("playlist_id") != playlist_id:
                    self._send_json(409, {"ok": False, "status": "review_required", "error_code": "operation_playlist_mismatch"})
                    return
                storage_key = str(operation_state.get("storage_key") or "")
                if not storage_key:
                    storage_key = _playlist_import_new_operation_storage_key()
                if not _valid_playlist_import_storage_key(storage_key):
                    self._send_json(409, {"ok": False, "status": "review_required", "error_code": "operation_state_corrupt"})
                    return
                try:
                    operation_dir = resolve_safe_path(str(operations_root / storage_key), ["staging"])
                    operation_dir.relative_to(operations_root.resolve(strict=False))
                except (ValueError, UnsafePathError):
                    self._send_json(403, {"error": "Playlist import operation staging is not safe", "error_code": "import_staging_symlink"})
                    return
                if operation_dir.exists() and _path_has_symlink_component(operation_dir, operations_root):
                    self._send_json(403, {"error": "Playlist import operation staging is not safe", "error_code": "import_staging_symlink"})
                    return

                saved_tracks = operation_state.get("tracks") if isinstance(operation_state.get("tracks"), dict) else {}
                if operation_state.get("status") == "completed":
                    completed_tracks = [dict(row) for row in saved_tracks.values() if isinstance(row, dict)]
                    self._send_json(200, {
                        "ok": True,
                        "status": "already_completed",
                        "operation_id": operation_id,
                        "playlist_key": playlist_key,
                        "playlist_id": playlist_id or operation_state.get("playlist_id", ""),
                        "imported_count": int(operation_state.get("imported_count") or 0),
                        "tracks": completed_tracks,
                        "beets": {"returncode": 0, "message": "operation already completed"},
                    })
                    return

                operation_state = {
                    **operation_state,
                    "operation_id": operation_id,
                    "playlist_key": playlist_key,
                    "playlist_id": playlist_id,
                    "storage_key": storage_key,
                    "status": operation_state.get("status") or "in_progress",
                    "updated_at": time.time(),
                    "tracks": saved_tracks,
                }

                def _save_operation_state() -> None:
                    operations[operation_id] = operation_state
                    index_state["operations"] = operations
                    _playlist_import_write_state(index_path, index_state)

                _save_operation_state()
                operation_dir.mkdir(parents=True, exist_ok=True)
                if _path_has_symlink_component(operation_dir, operations_root):
                    self._send_json(403, {"error": "Playlist import operation staging is not safe", "error_code": "import_staging_symlink"})
                    return

                valid_entries = []
                track_results = []
                seen_sources = set()

                for index, item in enumerate(tracks_payload, start=1):
                    if not isinstance(item, dict):
                        continue
                    try:
                        track_id = _bounded_text(item.get("track_id") or item.get("id") or f"tr_{index}", max_len=240)
                        staged_path_str = _bounded_text(item.get("staged_path") or item.get("path") or "", max_len=1024)
                        artist = _bounded_text(item.get("artist") or "", max_len=240)
                        title = _bounded_text(item.get("title") or "", max_len=240)
                        album = _bounded_text(item.get("album") or "", max_len=240)
                        albumartist = _bounded_text(item.get("albumartist") or "", max_len=240)
                        mb_trackid = _bounded_text(item.get("mb_trackid") or "", max_len=120)
                    except ValueError:
                        track_results.append({"track_id": "", "status": "invalid_track_id", "error": _safe_playlist_import_error("invalid_track_id")})
                        continue
                    if not _valid_playlist_import_track_id(track_id):
                        track_results.append({"track_id": track_id[:80], "status": "invalid_track_id", "error": _safe_playlist_import_error("invalid_track_id")})
                        continue

                    saved_row = saved_tracks.get(track_id) if isinstance(saved_tracks.get(track_id), dict) else {}
                    if saved_row.get("status") in {"imported", "already_imported"}:
                        result_row = dict(saved_row)
                        result_row["status"] = "already_imported"
                        track_results.append(result_row)
                        continue

                    if not staged_path_str:
                        track_results.append({"track_id": track_id, "status": "staged_track_missing", "error": _safe_playlist_import_error("staged_track_missing")})
                        continue

                    safe_source: Optional[Path] = None
                    saved_import_path = str(saved_row.get("operation_path") or "").strip()
                    if saved_import_path:
                        try:
                            candidate = resolve_safe_path(saved_import_path, ["staging"], require_exists=True, expected_type="file")
                            candidate.relative_to(operation_dir.resolve(strict=False))
                            safe_source = candidate
                        except Exception:
                            safe_source = None

                    if safe_source is None:
                        try:
                            raw_source = Path(os.path.abspath(staged_path_str))
                            raw_under_staging = os.path.commonpath([str(raw_source), str(staging_root)]) == str(staging_root)
                            if raw_under_staging and _path_has_symlink_component(raw_source, staging_root):
                                track_results.append({"track_id": track_id, "status": "staged_track_symlink", "error": _safe_playlist_import_error("staged_track_symlink")})
                                continue
                        except Exception:
                            track_results.append({"track_id": track_id, "status": "invalid_path", "error": _safe_playlist_import_error("invalid_path")})
                            continue
                        try:
                            safe_source = resolve_safe_path(staged_path_str, ["staging"], require_exists=True, expected_type="file")
                        except UnsafePathError as exc:
                            try:
                                resolve_safe_path(staged_path_str, ["music"], require_exists=True, expected_type="file")
                            except UnsafePathError:
                                pass
                            else:
                                track_results.append({"track_id": track_id, "status": "cross_playlist_target", "error": _safe_playlist_import_error("cross_playlist_target")})
                                continue
                            msg = str(exc).lower()
                            err_code = "staged_track_missing" if "does not exist" in msg else "staged_track_symlink" if "symlink" in msg else "invalid_path"
                            track_results.append({"track_id": track_id, "status": err_code, "error": _safe_playlist_import_error(err_code)})
                            continue

                    if _path_is_within(str(safe_source), str(music_root)):
                        track_results.append({"track_id": track_id, "status": "cross_playlist_target", "error": _safe_playlist_import_error("cross_playlist_target")})
                        continue

                    try:
                        relative_parts = safe_source.relative_to(staging_root).parts
                    except ValueError:
                        track_results.append({"track_id": track_id, "status": "cross_playlist_target", "error": _safe_playlist_import_error("cross_playlist_target")})
                        continue
                    in_operation_dir = False
                    try:
                        safe_source.relative_to(operation_dir.resolve(strict=False))
                        in_operation_dir = True
                    except Exception:
                        in_operation_dir = False
                    if not in_operation_dir and (not relative_parts or relative_parts[0] != playlist_key):
                        track_results.append({"track_id": track_id, "status": "cross_playlist_target", "error": _safe_playlist_import_error("cross_playlist_target")})
                        continue
                    if _path_has_symlink_component(safe_source, staging_root):
                        track_results.append({"track_id": track_id, "status": "staged_track_symlink", "error": _safe_playlist_import_error("staged_track_symlink")})
                        continue
                    source_ext = {ext: ext for ext in _import_source_audio_extensions()}.get(safe_source.suffix.lower())
                    if not source_ext:
                        track_results.append({"track_id": track_id, "status": "staged_track_not_audio", "error": _safe_playlist_import_error("staged_track_not_audio")})
                        continue

                    source_key = str(safe_source).casefold()
                    if source_key in seen_sources:
                        continue
                    seen_sources.add(source_key)

                    identity_ok, identity_status, identity_evidence = _playlist_import_identity_ok(safe_source, {
                        "artist": artist,
                        "title": title,
                        "mb_trackid": mb_trackid,
                    })
                    if not identity_ok:
                        track_results.append({"track_id": track_id, "status": identity_status, "error": _safe_playlist_import_error(identity_status)})
                        continue

                    dest_file = safe_source
                    if not in_operation_dir:
                        dest_file = operation_dir / f"track-{index:04d}{source_ext}"
                        suffix = 1
                        while dest_file.exists() or dest_file.is_symlink():
                            suffix += 1
                            dest_file = operation_dir / f"track-{index:04d}-{suffix}{source_ext}"
                        try:
                            trusted_dest = resolve_safe_path(str(dest_file), ["staging"])
                            trusted_dest.relative_to(operation_dir.resolve(strict=False))
                            shutil.move(str(resolve_safe_path(str(safe_source), ["staging"], require_exists=True, expected_type="file")), str(trusted_dest))
                            dest_file = trusted_dest
                        except Exception:
                            track_results.append({"track_id": track_id, "status": "import_failed", "error": _safe_playlist_import_error("import_failed")})
                            continue

                    tags_to_write = {}
                    if artist:
                        tags_to_write["artist"] = artist
                        tags_to_write["albumartist"] = albumartist or artist
                    if title:
                        tags_to_write["title"] = title
                        tags_to_write["album"] = album or title
                    if mb_trackid:
                        tags_to_write["mb_trackid"] = mb_trackid
                    year_value = item.get("year", 0)
                    try:
                        year_int = int(year_value or 0)
                    except Exception:
                        year_int = 0
                    if year_int > 0:
                        tags_to_write["year"] = year_int

                    if tags_to_write:
                        try:
                            from beets.mediafile import MediaFile
                            mf = MediaFile(dest_file)
                            for k, v in tags_to_write.items():
                                if hasattr(mf, k) and k in _TAG_WRITE_FIELDS:
                                    setattr(mf, k, v)
                            mf.save()
                        except Exception:
                            track_results.append({"track_id": track_id, "status": "tag_write_failed", "error": _safe_playlist_import_error("tag_write_failed")})
                            saved_tracks[track_id] = {
                                "track_id": track_id,
                                "status": "tag_write_failed",
                                "operation_path": str(dest_file),
                            }
                            operation_state["tracks"] = saved_tracks
                            operation_state["status"] = "partial"
                            operation_state["updated_at"] = time.time()
                            _save_operation_state()
                            continue

                    entry = {
                        "track_id": track_id,
                        "staged_path": str(dest_file),
                        "operation_path": str(dest_file),
                        "artist": artist,
                        "title": title,
                        "mb_trackid": mb_trackid,
                        "identity_status": identity_status,
                        "identity_evidence": identity_evidence,
                    }
                    saved_tracks[track_id] = {**entry, "status": "moved_to_import_staging"}
                    operation_state["tracks"] = saved_tracks
                    operation_state["status"] = "moved_to_import_staging"
                    operation_state["updated_at"] = time.time()
                    _save_operation_state()
                    valid_entries.append(entry)

                if not valid_entries:
                    completed = [dict(row) for row in saved_tracks.values() if isinstance(row, dict) and row.get("status") in {"imported", "already_imported"}]
                    if completed and len(completed) == len(saved_tracks):
                        status = "already_completed"
                        ok = True
                    else:
                        status = "review_required" if track_results else "staged_track_missing"
                        ok = False
                    self._send_json(200, {
                        "ok": ok,
                        "status": status,
                        "operation_id": operation_id,
                        "playlist_key": playlist_key,
                        "playlist_id": playlist_id,
                        "imported_count": 0,
                        "tracks": track_results or completed,
                        "beets": {"returncode": 0 if ok else 1, "message": "No valid files to import"},
                    })
                    return

                cmd = [
                    BEET_BIN,
                    "import", "-q", "--singletons", "--noincremental", "--move", "--quiet-fallback", "asis",
                    str(operation_dir),
                ]

                env = os.environ.copy()
                env["BEETSDIR"] = BEETSDIR
                proc_timeout = min(
                    PLAYLIST_IMPORT_MAX_TIMEOUT_SECONDS,
                    max(60, len(valid_entries) * PLAYLIST_IMPORT_PER_TRACK_TIMEOUT_SECONDS),
                )

                try:
                    res = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=proc_timeout,
                        env=env,
                    )
                except subprocess.TimeoutExpired:
                    operation_state["status"] = "failed"
                    operation_state["error_code"] = "import_timeout"
                    operation_state["updated_at"] = time.time()
                    _save_operation_state()
                    self._send_json(504, {"ok": False, "status": "failed", "error_code": "import_timeout", "message": "Beets import timed out."})
                    return
                except Exception:
                    operation_state["status"] = "failed"
                    operation_state["error_code"] = "import_failed"
                    operation_state["updated_at"] = time.time()
                    _save_operation_state()
                    self._send_json(500, {"ok": False, "status": "failed", "error_code": "import_failed", "message": _safe_playlist_import_error("import_failed")})
                    return

                imported_count = 0
                verified_count = 0
                for entry in valid_entries:
                    track_id = entry["track_id"]
                    if res.returncode < 2:
                        verified_item_id = _verify_imported_track_in_library(entry)
                        if verified_item_id:
                            entry["status"] = "imported"
                            entry["item_id"] = verified_item_id
                            verified_count += 1
                            imported_count += 1
                        else:
                            entry["status"] = "imported_unverified"
                            entry["error"] = "Imported track could not be verified in the Beets library by MusicBrainz recording ID."
                    else:
                        entry["status"] = "failed"
                        entry["error"] = _safe_playlist_import_error("import_failed")
                    saved_tracks[track_id] = dict(entry)
                    track_results.append(entry)

                if res.returncode < 2 and verified_count == len(valid_entries):
                    top_status = "completed"
                    ok = True
                    operation_state["status"] = "completed"
                elif res.returncode < 2:
                    top_status = "review_required"
                    ok = False
                    operation_state["status"] = "partial"
                else:
                    top_status = "failed"
                    ok = False
                    operation_state["status"] = "failed"

                operation_state["tracks"] = saved_tracks
                operation_state["imported_count"] = imported_count
                operation_state["beets_returncode"] = int(res.returncode)
                operation_state["updated_at"] = time.time()
                _save_operation_state()

                self._send_json(200, {
                    "ok": ok,
                    "status": top_status,
                    "operation_id": operation_id,
                    "playlist_key": playlist_key,
                    "playlist_id": playlist_id,
                    "imported_count": imported_count,
                    "tracks": track_results,
                    "beets": {
                        "returncode": res.returncode,
                        "message": "Beets import completed." if res.returncode < 2 else "Beets import failed.",
                    },
                })
            finally:
                try:
                    if tmp_cfg_path and os.path.exists(tmp_cfg_path):
                        os.remove(tmp_cfg_path)
                except Exception:
                    pass
            return

        if path == "/playlists/staging/list-files":
            playlist_key = str(body.get("playlist_key") or "").strip()
            playlist_id = str(body.get("playlist_id") or "").strip()

            if not _valid_playlist_key(playlist_key):
                self._send_json(400, {"error": "Invalid or missing playlist_key", "error_code": "invalid_playlist_key"})
                return

            staging_root = PLAYLIST_DOWNLOAD_ROOT.resolve(strict=False)
            try:
                safe_playlist_staging = resolve_safe_path(str(PLAYLIST_DOWNLOAD_ROOT / playlist_key), ["staging"])
                safe_playlist_staging.relative_to(staging_root)
            except (ValueError, UnsafePathError):
                self._send_json(400, {"error": "Invalid playlist staging root", "error_code": "invalid_playlist_key"})
                return

            if _path_has_symlink_component(safe_playlist_staging, staging_root):
                self._send_json(403, {"error": "Playlist staging path is not safe", "error_code": "playlist_staging_symlink"})
                return

            # Scope is intentionally just "downloads": that is the pre-import
            # staging area this endpoint exists to inventory (see
            # _playlist_reusable_download_files in the web manager). Walking
            # the playlist root itself was both redundant (it double-listed
            # everything already under downloads/) and overbroad (it would
            # also surface files under imports/ and the operation-state
            # index, which are not reusable pre-import downloads).
            search_dir = safe_playlist_staging / "downloads"
            files = []
            audio_exts = set(_import_source_audio_extensions())
            seen_paths = set()
            truncated = False

            lock_file = acquire_os_lock(read_only=True)
            try:
                if search_dir.exists() and search_dir.is_dir():
                    for root_dir, dirs, filenames in os.walk(str(search_dir)):
                        root_path = Path(root_dir)
                        if _path_has_symlink_component(root_path, staging_root):
                            dirs[:] = []
                            continue
                        for fn in filenames:
                            if len(files) >= PLAYLIST_STAGING_LIST_FILES_MAX:
                                truncated = True
                                break
                            fp = root_path / fn
                            if fp.suffix.lower() not in audio_exts:
                                continue
                            if fp.is_symlink():
                                continue
                            str_path = str(fp)
                            if str_path in seen_paths:
                                continue
                            seen_paths.add(str_path)
                            try:
                                st = fp.stat()
                            except OSError:
                                continue
                            files.append({
                                "path": str_path,
                                "filename": fn,
                                "size": int(st.st_size),
                                "mtime": float(st.st_mtime),
                            })
                        if truncated:
                            break
            finally:
                release_os_lock(lock_file)

            self._send_json(200, {
                "ok": True,
                "playlist_key": playlist_key,
                "playlist_id": playlist_id,
                "files": files,
                "truncated": truncated,
            })
            return

        if path == "/playlists/staging/validate-track":
            playlist_key = str(body.get("playlist_key") or "").strip()
            requested_path = str(body.get("requested_path") or "").strip()
            artist = str(body.get("artist") or "").strip()
            title = str(body.get("title") or "").strip()
            expected_mb_trackid = str(body.get("expected_mb_trackid") or "").strip()

            if not _valid_playlist_key(playlist_key):
                self._send_json(400, {"error": "Invalid or missing playlist_key", "error_code": "invalid_playlist_key"})
                return
            if not requested_path:
                self._send_json(400, {"error": "Missing requested_path", "error_code": "missing_requested_path"})
                return

            staging_root = PLAYLIST_DOWNLOAD_ROOT.resolve(strict=False)
            music_root = MUSIC_ROOT.resolve(strict=False)

            try:
                safe_target = resolve_safe_path(requested_path, ["staging"], require_exists=True, expected_type="file")
            except UnsafePathError:
                self._send_json(403, {"error": "Access denied for requested path"})
                return

            if _path_is_within(str(safe_target), str(music_root)):
                self._send_json(403, {"error": "Refusing to validate a library file as staging"})
                return

            try:
                relative_parts = safe_target.relative_to(staging_root).parts
            except ValueError:
                self._send_json(403, {"error": "Path outside playlist staging"})
                return

            if not relative_parts or relative_parts[0] != playlist_key:
                self._send_json(403, {"error": "Path outside this playlist's staging directory"})
                return

            if _path_has_symlink_component(safe_target, staging_root):
                self._send_json(403, {"error": "Path contains symlinks"})
                return

            if safe_target.suffix.lower() not in _import_source_audio_extensions():
                self._send_json(400, {"error": "File is not an allowed audio format"})
                return

            st = safe_target.stat()
            file_size = int(st.st_size)

            prefs = body.get("preferences") if isinstance(body.get("preferences"), dict) else {}
            format_res = audio_preferences.validate_audio_file_preferences(str(safe_target), prefs) if hasattr(audio_preferences, "validate_audio_file_preferences") else {"ok": True, "message": ""}

            item_dict = {"artist": artist, "title": title, "mb_trackid": expected_mb_trackid}
            ident_ok, ident_status, ident_info = _playlist_import_identity_ok(safe_target, item_dict)

            self._send_json(200, {
                "ok": True,
                "audio_allowed": bool(format_res.get("ok", True)),
                "format_message": str(format_res.get("message") or ""),
                "identity": {
                    "identity_ok": ident_ok,
                    "identity_status": ident_status,
                    "info": ident_info,
                    "final_action": "accept" if ident_ok else "review",
                },
                "match": {
                    "ok": ident_ok,
                    "identity_status": ident_status,
                    "title_score": 1.0 if ident_ok else 0.0,
                    "artist_score": 1.0 if ident_ok else 0.0,
                },
                "size": file_size,
                "path": str(safe_target),
            })
            return

        if path == "/playlists/quality-candidates":
            filter_mode = str(body.get("filter_mode") or "all").strip().lower()
            limit = int(body.get("limit") or 200)
            item_ids = body.get("item_ids")
            if not isinstance(item_ids, list):
                item_ids = None

            lock_file = acquire_os_lock(read_only=True)
            try:
                con = sqlite3.connect(LIB_PATH, timeout=10)
                con.row_factory = sqlite3.Row
                try:
                    candidates = _engine_playlist_quality_cleanup_candidates(con, limit=limit, item_ids=item_ids, filter_mode=filter_mode)
                    self._send_json(200, {"ok": True, "candidates": candidates})
                finally:
                    con.close()
            except Exception:
                self._send_json(500, {"error": "Failed to fetch quality candidates"})
            finally:
                release_os_lock(lock_file)
            return

        if path == "/playlists/place-imported":
            playlist_key = str(body.get("playlist_key") or "").strip()
            playlist_id = str(body.get("playlist_id") or "").strip()
            item_id = int(body.get("item_id") or 0)
            placement = body.get("placement") if isinstance(body.get("placement"), dict) else {}
            action = str(body.get("action") or "repair").strip().lower()

            if not _valid_playlist_key(playlist_key):
                self._send_json(400, {"error": "Invalid or missing playlist_key", "error_code": "invalid_playlist_key"})
                return
            if item_id <= 0:
                self._send_json(400, {"error": "Invalid item_id"})
                return
            # Authorization: item_id must belong to a playlist-import
            # operation recorded under this exact playlist_key. Without
            # this, any caller could mutate/move an arbitrary library item
            # by supplying an unrelated item_id alongside a playlist_key it
            # has no real relationship to.
            if not _playlist_item_belongs_to_playlist(playlist_key, item_id):
                self._send_json(403, {
                    "error": f"Item {item_id} was not imported as part of playlist {playlist_key}",
                    "error_code": "item_not_in_playlist",
                })
                return

            lock_file = acquire_os_lock(read_only=False)
            try:
                con = sqlite3.connect(LIB_PATH, timeout=10)
                con.row_factory = sqlite3.Row
                try:
                    cur = con.cursor()
                    cur.execute("SELECT * FROM items WHERE id = ?", (item_id,))
                    item_row = cur.fetchone()
                    if not item_row:
                        self._send_json(404, {"error": f"Item {item_id} not found"})
                        return

                    old_path_text = str(item_row["path"] if hasattr(item_row, "keys") and "path" in item_row.keys() else item_row[6])

                    # Back up the Beets DB before any mutation, matching the
                    # rollback/audit guarantee the web-manager side used to
                    # provide before this mutation moved into the engine.
                    backup_path = f"{LIB_PATH}.bak-{int(time.time())}-playlist-place-{item_id}"
                    try:
                        shutil.copy2(LIB_PATH, backup_path)
                    except Exception:
                        self._send_json(500, {"error": "Could not back up Beets library database before placement"})
                        return

                    if action == "move_singleton":
                        job_cfg = f"/tmp/beets-playlist-singleton-move-{item_id}-{uuid.uuid4().hex}.yaml"
                        cfg = _write_playlist_import_beets_config(job_cfg)
                        cmd = [BEET_BIN, "-c", cfg, "move", f"id:{item_id}"]
                        env = os.environ.copy()
                        env["BEETSDIR"] = BEETSDIR
                        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)
                        try:
                            if os.path.exists(job_cfg):
                                os.remove(job_cfg)
                        except Exception:
                            pass
                        cur.execute("SELECT path FROM items WHERE id = ?", (item_id,))
                        new_row = cur.fetchone()
                        new_path_text = str(new_row["path"]) if new_row else ""
                        moved = bool(
                            res.returncode < 2
                            and new_path_text
                            and new_path_text.replace("\\", "/").lower() != old_path_text.replace("\\", "/").lower()
                        )
                        self._send_json(200, {
                            "ok": True,
                            "moved": moved,
                            "backup": backup_path,
                            "old_path": old_path_text,
                            "new_path": new_path_text,
                            "returncode": res.returncode,
                        })
                        return

                    artist = str(placement.get("artist") or item_row["artist"] or "").strip()
                    title = str(placement.get("title") or item_row["title"] or "").strip()
                    album = str(placement.get("album") or item_row["album"] or "").strip()
                    albumartist = str(placement.get("albumartist") or placement.get("artist") or item_row["albumartist"] or artist).strip()
                    year = int(float(placement.get("year") or item_row["year"] or 0))
                    mb_trackid = str(placement.get("mb_trackid") or "").strip()
                    mb_albumid = str(placement.get("mb_albumid") or "").strip()
                    mb_releasegroupid = str(placement.get("mb_releasegroupid") or "").strip()
                    mb_albumartistid = str(placement.get("mb_albumartistid") or "").strip()

                    cur.execute(
                        "UPDATE items SET artist=?, title=?, album=?, albumartist=?, year=?, mb_trackid=?, mb_albumid=?, mb_releasegroupid=?, mb_albumartistid=? WHERE id=?",
                        (artist, title, album, albumartist, year, mb_trackid, mb_albumid, mb_releasegroupid, mb_albumartistid, item_id)
                    )
                    con.commit()

                    job_cfg = f"/tmp/beets-playlist-place-{item_id}-{uuid.uuid4().hex}.yaml"
                    cfg = _write_playlist_import_beets_config(job_cfg)
                    env = os.environ.copy()
                    env["BEETSDIR"] = BEETSDIR
                    proc_w = subprocess.run([BEET_BIN, "-c", cfg, "write", f"id:{item_id}"], capture_output=True, text=True, timeout=90, env=env)
                    proc_m = subprocess.run([BEET_BIN, "-c", cfg, "move", f"id:{item_id}"], capture_output=True, text=True, timeout=180, env=env)
                    try:
                        if os.path.exists(job_cfg):
                            os.remove(job_cfg)
                    except Exception:
                        pass

                    cur.execute("SELECT path FROM items WHERE id = ?", (item_id,))
                    new_row = cur.fetchone()
                    new_path_text = str(new_row["path"]) if new_row else ""
                    # Both the tag write and the file move must succeed for
                    # this to count as "repaired" -- a failed write with a
                    # successful move previously still reported
                    # repaired=true, leaving stale/incorrect tags on a
                    # relocated file with no indication anything was wrong.
                    repaired = proc_w.returncode < 2 and proc_m.returncode < 2 and bool(new_path_text)
                    reason = "" if repaired else (
                        f"beet write failed rc={proc_w.returncode}" if proc_w.returncode >= 2
                        else f"beet move failed rc={proc_m.returncode}" if proc_m.returncode >= 2
                        else "final path could not be verified after move"
                    )

                    response = {
                        "ok": True,
                        "repaired": repaired,
                        "backup": backup_path,
                        "item_id": item_id,
                        "old_path": old_path_text,
                        "new_path": new_path_text,
                        "artist": artist,
                        "title": title,
                        "album": album,
                        "albumartist": albumartist,
                        "returncode": proc_m.returncode,
                        "write_returncode": proc_w.returncode,
                    }
                    if reason:
                        response["reason"] = reason
                    self._send_json(200, response)
                finally:
                    con.close()
            except Exception:
                self._send_json(500, {"error": f"Failed to place item {item_id}"})
            finally:
                release_os_lock(lock_file)
            return

        if path == "/playlists/export_m3u":
            playlist_key = str(body.get("playlist_key") or "").strip()
            display_name = str(body.get("display_name") or "").strip()
            raw_items = body.get("items") or []

            try:
                safe_m3u = _playlist_m3u_path_for_key(playlist_key)
            except UnsafePathError:
                self._send_json(400, {"error": "Invalid or missing playlist_key", "error_code": "invalid_playlist_key"})
                return

            lines = ["#EXTM3U\n"]
            item_count = 0
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                item_path = str(item.get("path") or "").strip()
                if not item_path:
                    continue
                try:
                    safe_item_path = resolve_safe_path(item_path, ["music", "staging"])
                except UnsafePathError:
                    continue
                # "staging" is a broad, shared role covering every
                # playlist's download directory (and the wider torrents
                # root) -- an entry that resolves under this playlist's own
                # staging subtree is fine, but one under a *different*
                # playlist's subtree (or elsewhere in staging) must not
                # silently become an authorized entry in this playlist's
                # M3U just because it satisfies the broad role check
                # (SEC-002 Wave 10 second final review: same containment
                # class as Wave 9's cross-playlist staged-deletion fix).
                staging_base = PLAYLIST_DOWNLOAD_ROOT.resolve(strict=False)
                if _path_is_within(str(safe_item_path), str(staging_base)):
                    own_staging = (staging_base / playlist_key).resolve(strict=False)
                    if not _path_is_within(str(safe_item_path), str(own_staging)):
                        continue
                artist = re.sub(r"[\r\n]+", " ", str(item.get("artist") or "").strip())
                title = re.sub(r"[\r\n]+", " ", str(item.get("title") or "").strip())
                label = f"{artist} - {title}".strip(" -") or safe_item_path.name
                if len(label) > PLAYLIST_M3U_MAX_LINE_LENGTH:
                    label = label[:PLAYLIST_M3U_MAX_LINE_LENGTH]
                lines.append(f"#EXTINF:-1,{label}\n")
                lines.append(f"{safe_item_path}\n")
                item_count += 1
                if item_count > PLAYLIST_M3U_MAX_ITEMS:
                    self._send_json(413, {"error": "M3U has too many entries", "error_code": "m3u_too_large"})
                    return

            content = "".join(lines)
            if len(content.encode("utf-8")) > PLAYLIST_M3U_MAX_BYTES:
                self._send_json(413, {"error": "M3U is too large", "error_code": "m3u_too_large"})
                return

            lock_file = acquire_os_lock(read_only=False)
            try:
                # Re-resolve/revalidate under the lock, immediately before
                # mutation -- the earlier _playlist_m3u_path_for_key() call
                # happened before the lock was held (SEC-002 Wave 10 second
                # final review: the app-level lock only serializes
                # cooperating application operations against each other; it
                # is not a filesystem-level guarantee against a parent path
                # changing between that check and this mutation).
                try:
                    safe_m3u = _playlist_m3u_path_for_key(playlist_key)
                except UnsafePathError:
                    self._send_json(403, {"error": "Access denied for M3U export path outside allowed roots", "error_code": "m3u_forbidden"})
                    return
                safe_m3u.parent.mkdir(parents=True, exist_ok=True)
                tmp_m3u = None
                for _attempt in range(5):
                    candidate = safe_m3u.parent / f"{safe_m3u.stem}.{uuid.uuid4().hex[:8]}.tmp"
                    try:
                        _create_exclusive_temp_file(candidate, content)
                        tmp_m3u = candidate
                        break
                    except FileExistsError:
                        continue
                if tmp_m3u is None:
                    self._send_json(500, {"error": "Failed to export M3U file", "error_code": "m3u_export_failed"})
                    return
                tmp_m3u.replace(safe_m3u)
                self._send_json(200, {"ok": True, "playlist_key": playlist_key, "display_name": display_name, "size": safe_m3u.stat().st_size})
            except Exception:
                self._send_json(500, {"error": "Failed to export M3U file", "error_code": "m3u_export_failed"})
            finally:
                release_os_lock(lock_file)
            return

        if path == "/playlists/m3u/read":
            playlist_key = str(body.get("playlist_key") or "").strip()
            fallback_name = str(body.get("fallback_name") or "").strip()
            try:
                safe_m3u, resolved_key = _playlist_m3u_target(playlist_key, fallback_name)
            except UnsafePathError:
                self._send_json(400, {"error": "Invalid or missing playlist_key", "error_code": "invalid_playlist_key"})
                return

            if not safe_m3u.exists():
                self._send_json(200, {"ok": True, "items": [], "playlist_key": resolved_key, "exists": False})
                return

            # Symlink check, size bound, and the read itself all happen
            # under the lock, re-resolving immediately beforehand -- a
            # pre-lock check (the previous shape here) validates a
            # filesystem object that can still be swapped out before the
            # lock is acquired (SEC-002 Wave 10 second final review). The
            # size bound is enforced via a bounded read (_bounded_read_text),
            # not a stat() taken before the read, since the file can grow
            # between a pre-read stat and the read itself.
            lock_file = acquire_os_lock(read_only=True)
            try:
                try:
                    safe_m3u, resolved_key = _playlist_m3u_target(playlist_key, fallback_name)
                except UnsafePathError:
                    self._send_json(400, {"error": "Invalid or missing playlist_key", "error_code": "invalid_playlist_key"})
                    return
                if not safe_m3u.exists():
                    self._send_json(200, {"ok": True, "items": [], "playlist_key": resolved_key, "exists": False})
                    return
                if _path_has_symlink_component(safe_m3u, PLAYLIST_DIR.resolve(strict=False)):
                    self._send_json(403, {"error": "Refusing to read symlinked M3U path", "error_code": "m3u_forbidden"})
                    return
                content = _bounded_read_text(safe_m3u, PLAYLIST_M3U_MAX_BYTES)
                items = _parse_m3u_content(content)
                self._send_json(200, {"ok": True, "items": items, "playlist_key": resolved_key, "exists": True, "size": safe_m3u.stat().st_size})
            except ValueError as exc:
                code = str(exc) or "m3u_malformed"
                status = 413 if code == "m3u_too_large" else 400
                self._send_json(status, {"error": "M3U is too large" if code == "m3u_too_large" else "M3U is malformed", "error_code": code})
            except Exception:
                self._send_json(500, {"error": "Failed to read M3U file", "error_code": "m3u_read_failed"})
            finally:
                release_os_lock(lock_file)
            return

        if path == "/playlists/m3u/delete":
            playlist_key = str(body.get("playlist_key") or "").strip()
            fallback_name = str(body.get("fallback_name") or "").strip()
            if not playlist_key and fallback_name:
                self._send_json(409, {"error": "Legacy M3U delete requires an unambiguous playlist_key", "error_code": "legacy_m3u_ambiguous"})
                return
            try:
                safe_m3u, resolved_key = _playlist_m3u_target(playlist_key, fallback_name)
            except UnsafePathError:
                self._send_json(400, {"error": "Invalid or missing playlist_key", "error_code": "invalid_playlist_key"})
                return

            lock_file = acquire_os_lock(read_only=False)
            try:
                try:
                    safe_m3u, resolved_key = _playlist_m3u_target(playlist_key, fallback_name)
                except UnsafePathError:
                    self._send_json(400, {"error": "Invalid or missing playlist_key", "error_code": "invalid_playlist_key"})
                    return
                if safe_m3u.exists():
                    if safe_m3u.is_dir() or _path_has_symlink_component(safe_m3u, PLAYLIST_DIR.resolve(strict=False)):
                        self._send_json(403, {"error": "Refusing to delete unsafe M3U path", "error_code": "m3u_forbidden"})
                        return
                    safe_m3u.unlink()
                    self._send_json(200, {"ok": True, "deleted": True, "already_absent": False, "playlist_key": resolved_key})
                else:
                    self._send_json(200, {"ok": True, "deleted": False, "already_absent": True, "playlist_key": resolved_key})
            except Exception:
                self._send_json(500, {"error": "Failed to delete M3U file", "error_code": "m3u_delete_failed"})
            finally:
                release_os_lock(lock_file)
            return

        if path == "/playlists/m3u/list":
            playlist_dir = PLAYLIST_DIR.resolve(strict=False)
            if not playlist_dir.exists():
                self._send_json(200, {"ok": True, "files": [], "playlists": []})
                return

            lock_file = acquire_os_lock(read_only=True)
            try:
                files = []
                for p in sorted(playlist_dir.glob("*.m3u"), key=lambda x: x.name.lower()):
                    if len(files) >= PLAYLIST_M3U_LIST_MAX_FILES:
                        break
                    try:
                        safe_p = _playlist_m3u_path_for_key(p.stem)
                        if safe_p != p.resolve(strict=False):
                            continue
                        if safe_p.exists() and safe_p.is_file() and not _path_has_symlink_component(safe_p, playlist_dir):
                            files.append(_playlist_m3u_response_row(safe_p))
                    except Exception:
                        pass
                self._send_json(200, {"ok": True, "files": files, "playlists": files, "truncated": len(files) >= PLAYLIST_M3U_LIST_MAX_FILES})
            except Exception:
                self._send_json(500, {"error": "Failed to list M3U files", "error_code": "m3u_list_failed"})
            finally:
                release_os_lock(lock_file)
            return
        self._send_json(404, {"error": f"Endpoint not found: {path}"})

    def do_PATCH(self):
        if not self._authenticate():
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        content_len = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_len) if content_len > 0 else b"{}"

        try:
            body = json.loads(post_data.decode("utf-8")) if post_data else {}
        except Exception:
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        fields = body.get("fields", {})

        if path.startswith("/items/"):
            try:
                item_id = int(path.split("/")[2])
            except ValueError:
                self._send_json(400, {"error": "Invalid item ID"})
                return

            if not fields:
                self._send_json(400, {"error": "No fields provided"})
                return
            assignments, invalid_fields = _validated_update_assignments(fields, _ITEM_UPDATE_STATEMENTS)
            if invalid_fields:
                self._send_json(400, {"error": "Unsupported item field"})
                return

            invalid_fields = _invalid_field_names(fields, ITEM_EDITABLE_FIELDS)
            if invalid_fields:
                self._send_json(403, {"error": f"Field(s) not editable: {', '.join(invalid_fields)}"})
                return

            lock_file = acquire_os_lock(read_only=False)
            try:
                con = sqlite3.connect(LIB_PATH, timeout=10)
                con.row_factory = sqlite3.Row
                cur = con.cursor()
                cur.execute("SELECT * FROM items WHERE id = ?", (item_id,))
                if not cur.fetchone():
                    self._send_json(404, {"error": f"Item {item_id} not found"})
                    return

                for statement, value in assignments:
                    cur.execute(statement, (value, item_id))
                con.commit()
                cur.execute("SELECT * FROM items WHERE id = ?", (item_id,))
                updated = dict(cur.fetchone())
                con.close()
                self._send_json(200, {"success": True, "item": updated})
            except Exception as exc:
                self._send_json(500, {"error": f"Failed to update item: {exc}"})
            finally:
                release_os_lock(lock_file)
            return

        if path.startswith("/albums/"):
            try:
                album_id = int(path.split("/")[2])
            except ValueError:
                self._send_json(400, {"error": "Invalid album ID"})
                return

            if not fields:
                self._send_json(400, {"error": "No fields provided"})
                return
            assignments, invalid_fields = _validated_update_assignments(fields, _ALBUM_UPDATE_STATEMENTS)
            if invalid_fields:
                self._send_json(400, {"error": "Unsupported album field"})
                return

            invalid_fields = _invalid_field_names(fields, ALBUM_EDITABLE_FIELDS)
            if invalid_fields:
                self._send_json(403, {"error": f"Field(s) not editable: {', '.join(invalid_fields)}"})
                return

            lock_file = acquire_os_lock(read_only=False)
            try:
                con = sqlite3.connect(LIB_PATH, timeout=10)
                con.row_factory = sqlite3.Row
                cur = con.cursor()
                cur.execute("SELECT * FROM albums WHERE id = ?", (album_id,))
                if not cur.fetchone():
                    self._send_json(404, {"error": f"Album {album_id} not found"})
                    return

                for statement, value in assignments:
                    cur.execute(statement, (value, album_id))
                con.commit()
                cur.execute("SELECT * FROM albums WHERE id = ?", (album_id,))
                updated = dict(cur.fetchone())
                con.close()
                self._send_json(200, {"success": True, "album": updated})
            except Exception as exc:
                self._send_json(500, {"error": f"Failed to update album: {exc}"})
            finally:
                release_os_lock(lock_file)
            return

        self._send_json(404, {"error": f"Endpoint not found: {path}"})

    def do_DELETE(self):
        if not self._authenticate():
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        content_len = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_len) if content_len > 0 else b"{}"
        try:
            body = json.loads(post_data.decode("utf-8")) if post_data else {}
        except Exception:
            body = {}

        if path.startswith("/albums/") and path.endswith("/art"):
            parts = path.split("/")
            try:
                album_id = int(parts[2])
            except (ValueError, IndexError):
                self._send_json(400, {"error": "Invalid album ID"})
                return
            lock_file = acquire_os_lock(read_only=False)
            con = None
            try:
                con = sqlite3.connect(LIB_PATH, timeout=10)
                con.row_factory = sqlite3.Row
                result = _delete_album_art_locked(con, album_id)
                self._send_json(200, result)
            except AlbumArtOperationError as exc:
                self._send_json(exc.status, {"error": exc.message})
            except UnsafePathError:
                self._send_json(403, {"error": "Access denied for album artwork path"})
            except Exception:
                self._send_json(500, {"error": "Failed to delete album artwork"})
            finally:
                if con is not None:
                    con.close()
                release_os_lock(lock_file)
            return

        if path.startswith("/albums/") and path.endswith("/artpath"):
            parts = path.split("/")
            try:
                album_id = int(parts[2])
            except ValueError:
                self._send_json(400, {"error": "Invalid album ID"})
                return

            lock_file = acquire_os_lock(read_only=False)
            try:
                con = sqlite3.connect(LIB_PATH, timeout=10)
                con.row_factory = sqlite3.Row
                cur = con.cursor()
                cur.execute("SELECT * FROM albums WHERE id = ?", (album_id,))
                if not cur.fetchone():
                    self._send_json(404, {"error": f"Album {album_id} not found"})
                    return
                cur.execute("UPDATE albums SET artpath = '' WHERE id = ?", (album_id,))
                con.commit()
                con.close()
                self._send_json(200, {"ok": True, "album_id": album_id, "artpath": ""})
            except Exception as exc:
                self._send_json(500, {"error": f"Failed to clear artpath: {exc}"})
            finally:
                release_os_lock(lock_file)
            return

        if path.startswith("/albums/"):
            parts = path.split("/")
            if len(parts) == 3:
                try:
                    album_id = int(parts[2])
                except ValueError:
                    self._send_json(400, {"error": "Invalid album ID"})
                    return

                delete_files = body.get("delete_files", True)
                code, res = _handle_delete_album(album_id, delete_files=delete_files)
                self._send_json(code, res)
                return

        self._send_json(404, {"error": f"Endpoint not found: {path}"})


def beets_api_token_is_usable(value: str) -> bool:
    """Fail-closed strength check for BEETS_API_TOKEN: rejects empty,
    too-short, and known-placeholder values so a weak/example credential
    (e.g. "changeme") cannot silently grant real authenticated control-agent
    access -- an emptiness check alone does not catch this."""
    token = (value or "").strip()
    if len(token) < BEETS_API_TOKEN_MIN_LENGTH:
        return False
    if token.lower() in _PLACEHOLDER_API_TOKENS:
        return False
    return True


def run_agent():
    if not beets_api_token_is_usable(BEETS_API_TOKEN):
        raise RuntimeError(
            "BEETS_API_TOKEN environment variable is required and must be a strong, "
            f"non-placeholder value of at least {BEETS_API_TOKEN_MIN_LENGTH} characters"
        )
    server_address = ("0.0.0.0", PORT)
    # ThreadingHTTPServer, not the plain single-threaded HTTPServer (BUG-3,
    # v0.1.12): the previous plain HTTPServer handles exactly one request at
    # a time, so a slow /status call (launching `beet version`, which can
    # take up to ~29s under real host load -- confirmed on the v0.1.11
    # TrueNAS production rollout) blocked EVERY other request behind it,
    # including Docker's own /health liveness check (already cheap -- a
    # hardcoded dict, no subprocess) and even a concurrent /health call from
    # a different client -- both were observed timing out with
    # BrokenPipeError in production logs purely from queuing behind an
    # in-flight /status call, not from their own cost. This is the root
    # cause the rest of BUG-3's fix (caching, below) reduces the *frequency*
    # of, but threading is what stops one slow request from starving every
    # other endpoint regardless of frequency. daemon_threads=True so
    # in-flight request threads don't block process shutdown.
    httpd = ThreadingHTTPServer(server_address, ControlAgentHandler)
    httpd.daemon_threads = True
    print(f"[BeetsControlAgent] Listening on 0.0.0.0:{PORT} (LOCK={LOCK_PATH})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run_agent()
