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
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
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
LOCK_PATH = os.environ.get("BEETS_LOCK_PATH", os.path.join(BEETSDIR, ".beet_db.lock"))
LIB_PATH = os.path.join(BEETSDIR, "musiclibrary.blb")
BEET_BIN = os.environ.get("BEET_BIN", "beet")


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


def get_loaded_beet_plugins() -> set:
    """Return the set of plugins Beets actually finished loading, parsed from
    `beet version`'s own "plugins: a, b, c" line.

    Unlike a Python import check (e.g. importlib.util.find_spec), this only
    includes plugins that survived Beets' own load() step, so a plugin that is
    importable on disk but silently fails to initialize (as beetsplug.chroma
    can, even with fpcalc/pyacoustid present) is correctly excluded.
    """
    return set(_beet_version_snapshot().get("loaded_plugins") or [])


def _agent_status_payload() -> dict[str, Any]:
    snapshot = _beet_version_snapshot()
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

    plugin_names = {
        "chroma", "discogs", "discpath", "fetchart", "lastgenre", "listenbrainz",
        "mbsubmit", "mbsync", "musicbrainz", "replaygain",
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

    return {
        "status": "ok",
        "service": "beets-control-agent",
        "agent_version": "1.0.0",
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


def _import_source_signature(entries: list[dict[str, Any]]) -> str:
    """A cheap staleness fingerprint (NOT recording identity -- see the
    caller). Two inspections of an unchanged source produce the same
    signature; any added/removed/resized/retimestamped file changes it."""
    parts = sorted(f"{e['relative_path']}:{e['size']}:{e['mtime_ns']}" for e in entries)
    return hashlib.sha256("|".join(parts).encode("utf-8", "surrogatepass")).hexdigest()


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

    for root_type in allowed_types:
        for candidate_root in _allowed_root_paths([root_type]):
            try:
                if trusted == Path(os.path.realpath(candidate_root)):
                    return {"ok": False, "error_code": "root_self_rejected"}
            except Exception:
                continue

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

    start_after_rel = ""
    if cursor and isinstance(cursor, str):
        try:
            decoded_json = json.loads(base64.b64decode(cursor.encode("utf-8")).decode("utf-8"))
            if isinstance(decoded_json, dict):
                start_after_rel = str(decoded_json.get("last_rel", ""))
        except Exception:
            return {"ok": False, "error_code": "invalid_cursor"}

    audio_exts = _import_source_audio_extensions()
    candidates: list[dict[str, Any]] = []
    dirs_visited = 0
    files_examined = 0
    limit_reached = False
    last_processed_rel = ""

    stack = [trusted]
    resuming = bool(start_after_rel)

    while stack and len(candidates) < max_candidates and not limit_reached:
        current = stack.pop()
        dirs_visited += 1

        if dirs_visited > max_dirs:
            limit_reached = True
            break

        try:
            rel_dir = str(current.relative_to(trusted))
        except Exception:
            rel_dir = ""

        if resuming:
            if rel_dir <= start_after_rel:
                try:
                    children = sorted(current.iterdir())
                    for child in reversed(children):
                        if not child.is_symlink() and child.is_dir():
                            stack.append(child)
                except Exception:
                    pass
                continue
            else:
                resuming = False

        last_processed_rel = rel_dir

        try:
            children = sorted(current.iterdir())
        except Exception:
            continue

        dir_audio_files: list[tuple[Path, os.stat_result, str]] = []

        for child in children:
            files_examined += 1
            if files_examined > max_files:
                limit_reached = True
                break

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

        if limit_reached:
            break

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
    if limit_reached or stack:
        if last_processed_rel:
            token_payload = {"last_rel": last_processed_rel}
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
    try:
        trusted_src = resolve_safe_path(
            source_path, ["staging", "music"], require_exists=True, expected_type="dir"
        )
    except UnsafePathError:
        return {"ok": False, "error_code": "invalid_path"}

    for candidate_root in _allowed_root_paths(["staging", "music"]):
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
    except Exception as ex:
        if safe_dest.exists():
            try:
                shutil.rmtree(str(safe_dest))
            except Exception:
                pass
        return {"ok": False, "error_code": "copy_failed", "message": f"Preservation copy failed: {ex}"}
    finally:
        release_os_lock(lock_file)


def verify_deterministic_identity(
    source_inspect: dict[str, Any],
    expected_identity: dict[str, Any],
) -> dict[str, Any]:
    if not expected_identity or not isinstance(expected_identity, dict):
        return {"ok": True}

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
        if source_album_mbids and target_mb_albumid not in source_album_mbids:
            return {
                "ok": False,
                "error_code": "identity_mismatch",
                "message": f"Source embedded MusicBrainz album ID conflicts with target {target_mb_albumid}",
            }
    if target_mb_rgid:
        if source_rg_mbids and target_mb_rgid not in source_rg_mbids:
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
                if not row:
                    return {"ok": False, "error_code": "identity_mismatch", "message": f"Target album ID {target_existing_album_id} not found in library"}
                db_mb_albumid = str(row["mb_albumid"] or "").strip().lower()
                if db_mb_albumid and source_album_mbids and db_mb_albumid not in source_album_mbids:
                    return {"ok": False, "error_code": "identity_mismatch", "message": "Source audio MBID conflicts with library album"}
        except Exception:
            pass

    return {"ok": True}


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
    config_override = str(options.get("config_override") or "").strip()
    duplicate_action = str(options.get("duplicate_action") or "remove").strip()

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

        cmd_args = ["import", "-q", "--noincremental", "asis", "--copy" if preserved_path else "--move"]
        if duplicate_action:
            cmd_args.extend(["-D", duplicate_action])
        if mb_albumid:
            cmd_args.extend(["--search-id", mb_albumid])
        cmd_args.append(import_target_path)
        full_cmd.extend(cmd_args)

        env = os.environ.copy()
        env["BEETSDIR"] = BEETSDIR
        res = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=180,
            env=env
        )

        if res.returncode >= 2:
            return {
                "ok": False,
                "error_code": "import_failed",
                "message": f"Beets import failed with return code {res.returncode}",
                "stdout": res.stdout,
                "stderr": res.stderr,
            }

        aid = None
        try:
            with sqlite3.connect(LIB_PATH, timeout=10) as con:
                con.row_factory = sqlite3.Row
                cur = con.cursor()
                if mb_albumid:
                    row = cur.execute("SELECT id FROM albums WHERE mb_albumid = ?", (mb_albumid,)).fetchone()
                    if row:
                        aid = row[0]
                if aid is None:
                    row = cur.execute("SELECT id FROM albums ORDER BY id DESC LIMIT 1").fetchone()
                    if row:
                        aid = row[0]
        except Exception:
            pass

        return {
            "ok": True,
            "album_id": aid,
            "preserved_path": preserved_path,
            "source_path": str(trusted),
            "source_signature": curr_signature,
            "returncode": res.returncode,
            "stdout": res.stdout,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error_code": "import_failed", "message": "Beets import timed out after 180s"}
    except Exception as ex:
        return {"ok": False, "error_code": "import_failed", "message": f"Beets import failed: {ex}"}
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

        if path == "/status":
            self._send_json(200, _agent_status_payload())
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

        content_len = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_len) if content_len > 0 else b"{}"

        try:
            body = json.loads(post_data.decode("utf-8")) if post_data else {}
        except Exception:
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        if path == "/library/raw_query":
            self._send_json(403, {"error": "Raw SQL queries are not permitted"})
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
                tags = {}
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
                    }
                except Exception:
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
                self._send_json(200, {"tags": tags})
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
    httpd = HTTPServer(server_address, ControlAgentHandler)
    print(f"[BeetsControlAgent] Listening on 0.0.0.0:{PORT} (LOCK={LOCK_PATH})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run_agent()
