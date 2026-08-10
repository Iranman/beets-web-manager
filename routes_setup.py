"""First-run setup wizard API — registered on app after app.py initializes.

Read-only status/test endpoints plus a single settings-persistence endpoint.
Does not change how app.py itself loads config: env vars and config.yaml
remain authoritative. This module only adds:
  - GET  /api/setup/status         readiness snapshot for the wizard/health page
  - GET  /api/setup/env            masked .env editor metadata
  - POST /api/setup/env            update allowed .env keys and apply them to this process
  - POST /api/setup/test/ai        live AI provider connectivity test
  - POST /api/setup/test/musicbrainz
  - POST /api/setup/test/acoustid  fpcalc + AcoustID API test
  - POST /api/setup/test/plex
  - GET/POST /api/setup/settings   persisted settings not covered by env/config.yaml
  - GET  /health, /health/live, /health/ready   standard Docker/k8s-style probes
    (in addition to the existing /api/health — these use the unprefixed
    convention most container orchestrators expect by default)
"""
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple

from flask import jsonify, request

# Imported after app.py has already defined app (circular-but-OK pattern,
# matches routes_jobs.py / routes_lidarr.py).
from app import app  # noqa: E402
from backend.beets_client import BeetsAuthError, BeetsError, BeetsUnavailableError, beets_client  # noqa: E402
# The auth-bootstrap helpers below are imported lazily, inside the functions
# that use them, rather than here at module scope: tests/test_routes_setup.py
# exercises this module against a minimal stub `app` module (just a bare
# Flask() instance, none of app.py's real helpers) to test the setup wizard
# in isolation without booting the full app -- a module-level import of
# app.py-only names would break that stub import for every test in the file.

# Default to /web-manager-data, not /config: neither docker-compose.yml nor
# docker-compose.full.yml mounts /config on the beets-web-manager service at
# all (that mount belongs to the beets engine service) -- combined with this
# service's read_only:true root filesystem, a /config default means every
# settings save (including the System page's browser-password change) fails
# closed with a 500 in the actual shipped configuration. /web-manager-data
# is the one mount this service is guaranteed to own and can write to
# (already used for .auth_token/.browser_password/.initial_admin_password).
_SETTINGS_FILE = Path(os.environ.get("SETUP_SETTINGS_FILE", "/web-manager-data/app_settings.json"))
_SETUP_COMPLETE_MARKER = Path(os.environ.get("SETUP_COMPLETE_FILE", "/web-manager-data/.setup_complete"))
_SETUP_ENV_FILE = Path(os.environ.get("SETUP_ENV_FILE", "/web-manager-data/.env"))
_ENV_EXAMPLE_FILE = Path(os.environ.get("SETUP_ENV_EXAMPLE_FILE", str(Path(__file__).parent / ".env.example")))
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_BLOCKED_ENV_NAMES = {"SETUP_ENV_FILE", "SETUP_ENV_EXAMPLE_FILE", "SETUP_SETTINGS_FILE", "SETUP_COMPLETE_FILE"}
_SECRET_ENV_PARTS = ("KEY", "TOKEN", "PASSWORD", "SECRET")
_PASSWORD_MIN_LENGTH_FLOOR = 12
_PASSWORD_SPECIAL_RE = re.compile(r"[^A-Za-z0-9]")
_PASSWORD_UPPER_RE = re.compile(r"[A-Z]")
_PASSWORD_LOWER_RE = re.compile(r"[a-z]")
_PASSWORD_DIGIT_RE = re.compile(r"[0-9]")
_FALLBACK_AUTH_TOKEN_FILE = Path(os.environ.get("BEETS_WEB_AUTH_TOKEN_FILE", "/web-manager-data/.auth_token"))
_FALLBACK_PLACEHOLDER_AUTH_SECRETS = {
    "admin", "password", "password1", "changeme", "changeit", "secret", "token",
    "default", "example", "letmein", "beets", "beetsweb", "setinenv", "setastrongownertoken",
}


def _fallback_auth_secret_usable(value: str) -> bool:
    """Standalone duplicate of app.py's _auth_secret_is_usable (length +
    placeholder check only, skipping the ${...}/? shell-substitution guard).
    Used only if app.py's real helper can't be imported -- e.g. routes_setup
    loaded against a minimal stub `app` module in tests/test_routes_setup.py
    -- so /api/setup/status degrades gracefully instead of 500ing."""
    secret = (value or "").strip()
    if len(secret) < 32:
        return False
    compact = re.sub(r"[^a-z0-9]+", "", secret.lower())
    return compact not in _FALLBACK_PLACEHOLDER_AUTH_SECRETS


def _password_min_length() -> int:
    """Never let the password-strength minimum be weaker than app.py's real
    auth-secret usability floor (_MIN_AUTH_SECRET_LENGTH, default 32, bounded
    [24,256] via BEETS_WEB_AUTH_MIN_LENGTH) -- otherwise a password can pass
    this check, save successfully, and still fail _auth_secret_is_usable()
    on the very next request, locking the browser out immediately after a
    "successful" save. Real incident this fixes (2026-07-20): an 18-char
    password satisfied the original hardcoded 12-char floor here, saved, and
    then 401'd on every subsequent request because it was under app.py's
    separate 32-char gate. _PASSWORD_MIN_LENGTH_FLOOR (12, the literal spec
    minimum) only applies if the operator lowers BEETS_WEB_AUTH_MIN_LENGTH
    below it, which _env_int's own [24,256] bound never actually allows in
    practice -- so in effect this always resolves to the real auth floor.
    """
    try:
        configured = int(os.environ.get("BEETS_WEB_AUTH_MIN_LENGTH", "32"))
    except ValueError:
        configured = 32
    configured = max(24, min(256, configured))
    return max(_PASSWORD_MIN_LENGTH_FLOOR, configured)


def _password_requirements_unmet(password: str) -> List[str]:
    """Returns unmet BEETS_WEB_PASSWORD requirements (empty list = passes).

    Requirements match what the setup UI displays and enforces client-side:
    at least _password_min_length() characters, one uppercase letter, one
    lowercase letter, one number, and one special (non-alphanumeric)
    character.
    """
    unmet: List[str] = []
    min_length = _password_min_length()
    if len(password) < min_length:
        unmet.append(f"at least {min_length} characters")
    if not _PASSWORD_UPPER_RE.search(password):
        unmet.append("an uppercase letter")
    if not _PASSWORD_LOWER_RE.search(password):
        unmet.append("a lowercase letter")
    if not _PASSWORD_DIGIT_RE.search(password):
        unmet.append("a number")
    if not _PASSWORD_SPECIAL_RE.search(password):
        unmet.append("a special character")
    return unmet
_FALLBACK_ENV_TEMPLATE = """# Required owner/admin authentication
BEETS_WEB_AUTH_TOKEN=
BEETS_WEB_PASSWORD=
BEETS_WEB_USERNAME=admin
BEETS_WEB_AUTH_DISABLED=0
BEETS_WEB_AUTH_MIN_LENGTH=32
BEETS_TRUSTED_PROXIES=
BEETS_OUTBOUND_ALLOWLIST=

# Core Beets paths
BEETS_LIBRARY=/config/musiclibrary.blb
BEETS_CONFIG=/config/config.yaml
BEETS_LOG=/config/beet.log
WEBCONTROL_PORT=8337

# AI provider keys
OPENAI_API_KEY=
OPENROUTER_API_KEY=
AI_API_KEY=
AI_BASE_URL=
AI_MODEL=

# Plex and Arr services
PLEX_URL=
PLEX_TOKEN=
LIDARR_URL=
LIDARR_API_KEY=

# Music metadata providers
ACOUSTID_API_KEY=
ACOUSTID_KEY=
DISCOGS_TOKEN=
DISCOGS_USER_TOKEN=
LISTENBRAINZ_TOKEN=

# SLSKD and Soulseek
SLSKD_SLSK_USERNAME=
SLSKD_SLSK_PASSWORD=
SLSKD_API_KEY=
SLSKD_API_KEY_FILE=/config/slskd_api_key

# Spotify playlist parsing
SPOTIFY_CLIENT_ID=
SPOTIFY_CLIENT_SECRET=

# yt-dlp and direct-source helpers
YTDLP_COOKIE_FILE=/config/yt-dlp/cookies.txt
YTDLP_ALLOW_BROWSER_COOKIES=0
YTDLP_NETRC_FILE=/config/.netrc
YTDLP_PO_PROVIDER_URL=http://bgutil-provider:4416
YTDLP_JS_RUNTIMES=deno,node,quickjs
SPOTIFLAC_AUTO_INSTALL=0
SPOTIFLAC_CMD=

# Docker service credentials
PUID=1000
PGID=1000
BEETS_UID=1000
BEETS_GID=1000
DIGARR_INITIAL_PASSWORD=
POSTGRES_PASSWORD=

# Demo mode
DEMO_MODE=0
"""


def _app_version() -> str:
    for candidate in (Path(__file__).parent / "VERSION", Path("/app/VERSION")):
        try:
            return candidate.read_text(encoding="utf-8").strip()
        except Exception:
            continue
    return "unknown"


_APP_VERSION = _app_version()


def _load_settings() -> Dict[str, Any]:
    try:
        return json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_settings(data: Dict[str, Any]) -> None:
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(_SETTINGS_FILE)


def _mask(value: str) -> str:
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


def _is_secret_env(name: str) -> bool:
    upper = name.upper()
    return any(part in upper for part in _SECRET_ENV_PARTS)


def _decode_env_value(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        inner = raw[1:-1]
        if raw[0] == '"':
            return (
                inner
                .replace("\\n", "\n")
                .replace("\\r", "\r")
                .replace('\\"', '"')
                .replace("\\\\", "\\")
            )
        return inner
    return raw


def _format_env_value(value: str) -> str:
    value = str(value or "")
    if value == "":
        return ""
    needs_quotes = (
        value != value.strip()
        or any(ch in value for ch in (" ", "\t", "#", '"', "'", "\\", "\n", "\r"))
    )
    if not needs_quotes:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return f'"{escaped}"'


def _parse_env_text(text: str) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    entries: List[Dict[str, Any]] = []
    values: Dict[str, str] = {}
    section = "General"
    previous_blank = True
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            entries.append({"type": "blank", "raw": raw_line})
            previous_blank = True
            continue
        if stripped.startswith("#"):
            comment = stripped.lstrip("#").strip()
            if comment and previous_blank:
                section = comment
            entries.append({"type": "comment", "raw": raw_line, "section": section})
            previous_blank = False
            continue
        candidate = stripped[7:].strip() if stripped.startswith("export ") else stripped
        if "=" not in candidate:
            entries.append({"type": "raw", "raw": raw_line})
            previous_blank = False
            continue
        key, raw_value = candidate.split("=", 1)
        key = key.strip()
        if not _ENV_NAME_RE.match(key):
            entries.append({"type": "raw", "raw": raw_line})
            previous_blank = False
            continue
        value = _decode_env_value(raw_value)
        values[key] = value
        entries.append({"type": "var", "raw": raw_line, "key": key, "value": value, "section": section})
        previous_blank = False
    return entries, values


def _read_text_if_exists(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _env_example_text() -> str:
    return _read_text_if_exists(_ENV_EXAMPLE_FILE) or _FALLBACK_ENV_TEMPLATE


def _env_catalog() -> Dict[str, Dict[str, Any]]:
    entries, values = _parse_env_text(_env_example_text())
    catalog: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        if entry.get("type") != "var":
            continue
        key = str(entry.get("key") or "")
        if key in _BLOCKED_ENV_NAMES:
            continue
        catalog[key] = {
            "name": key,
            "section": entry.get("section") or "General",
            "default": values.get(key, ""),
            "secret": _is_secret_env(key),
        }
    return catalog


def _load_env_file() -> Tuple[List[Dict[str, Any]], Dict[str, str], bool]:
    text = _read_text_if_exists(_SETUP_ENV_FILE)
    if not text:
        return [], {}, False
    entries, values = _parse_env_text(text)
    return entries, values, True


def _setup_env_payload(extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
    catalog = _env_catalog()
    _, persisted, exists = _load_env_file()
    names = list(catalog.keys())
    for key in persisted:
        if key not in catalog and _ENV_NAME_RE.match(key) and key not in _BLOCKED_ENV_NAMES:
            catalog[key] = {
                "name": key,
                "section": "Custom",
                "default": "",
                "secret": _is_secret_env(key),
            }
            names.append(key)
    variables = []
    for name in names:
        meta = catalog[name]
        if name in persisted:
            raw_value = persisted[name]
            source = "file"
        elif name in os.environ:
            raw_value = os.environ.get(name, "")
            source = "process"
        else:
            raw_value = str(meta.get("default") or "")
            source = "example"
        runtime_value = os.environ.get(name, "")
        secret = bool(meta.get("secret"))
        variables.append({
            "name": name,
            "section": meta.get("section") or "General",
            "secret": secret,
            "has_value": bool(raw_value),
            "value": _mask(raw_value) if secret else raw_value,
            "source": source,
            "runtime_has_value": bool(runtime_value),
            "runtime_value": _mask(runtime_value) if secret else runtime_value,
        })
    payload: Dict[str, Any] = {
        "ok": True,
        "env_file": str(_SETUP_ENV_FILE),
        "exists": exists,
        "example_file": str(_ENV_EXAMPLE_FILE),
        "restart_required_after_save": True,
        "variables": variables,
    }
    if extra:
        payload.update(extra)
    return payload


def _write_env_file(updates: Dict[str, str], clear: List[str]) -> str:
    catalog = _env_catalog()
    entries, persisted, exists = _load_env_file()
    editable = set(catalog.keys()) | set(persisted.keys())
    for key, value in list(updates.items()):
        if key in _BLOCKED_ENV_NAMES or not _ENV_NAME_RE.match(key) or key not in editable:
            raise ValueError(f"{key} is not an editable setup environment variable")
        if "\n" in value or "\r" in value:
            raise ValueError(f"{key} cannot contain newlines")
        if len(value) > 4096:
            raise ValueError(f"{key} is too long")
    if updates.get("BEETS_WEB_PASSWORD"):
        unmet = _password_requirements_unmet(updates["BEETS_WEB_PASSWORD"])
        if unmet:
            raise ValueError("Password does not meet requirements: needs " + ", ".join(unmet) + ".")
    for key in clear:
        if key in _BLOCKED_ENV_NAMES or not _ENV_NAME_RE.match(key) or key not in editable:
            raise ValueError(f"{key} is not an editable setup environment variable")

    if not entries:
        entries, _ = _parse_env_text(_env_example_text())

    desired = dict(updates)
    for key in clear:
        desired[key] = ""

    lines: List[str] = []
    seen = set()
    for entry in entries:
        if entry.get("type") == "var":
            key = str(entry.get("key") or "")
            if key in desired:
                lines.append(f"{key}={_format_env_value(desired[key])}")
                seen.add(key)
            else:
                lines.append(str(entry.get("raw") or ""))
        else:
            lines.append(str(entry.get("raw") or ""))
    missing = [key for key in desired if key not in seen]
    if missing:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Added by setup")
        for key in missing:
            lines.append(f"{key}={_format_env_value(desired[key])}")

    _SETUP_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    backup_path = ""
    if exists:
        backup = _SETUP_ENV_FILE.with_name(f"{_SETUP_ENV_FILE.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(_SETUP_ENV_FILE, backup)
        backup_path = str(backup)
    tmp = _SETUP_ENV_FILE.with_suffix(_SETUP_ENV_FILE.suffix + ".tmp")
    tmp.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    tmp.replace(_SETUP_ENV_FILE)
    for key, value in desired.items():
        os.environ[key] = value
    try:
        from app import (
            _cleanup_initial_browser_password_if_replaced,
            _PERSISTED_BROWSER_PASSWORD_FILE,
            _PERSISTED_BROWSER_USERNAME_FILE,
            _persist_file_atomically,
        )
        if updates.get("BEETS_WEB_PASSWORD"):
            _persist_file_atomically(_PERSISTED_BROWSER_PASSWORD_FILE, updates["BEETS_WEB_PASSWORD"])
        elif "BEETS_WEB_PASSWORD" in clear and _PERSISTED_BROWSER_PASSWORD_FILE.exists():
            _PERSISTED_BROWSER_PASSWORD_FILE.unlink(missing_ok=True)

        if updates.get("BEETS_WEB_USERNAME"):
            _persist_file_atomically(_PERSISTED_BROWSER_USERNAME_FILE, updates["BEETS_WEB_USERNAME"])
        elif "BEETS_WEB_USERNAME" in clear and _PERSISTED_BROWSER_USERNAME_FILE.exists():
            _PERSISTED_BROWSER_USERNAME_FILE.unlink(missing_ok=True)

        _cleanup_initial_browser_password_if_replaced()
    except Exception:
        pass
    return backup_path


def _check_path(path_value: str, *, require_writable: bool) -> Dict[str, Any]:
    result: Dict[str, Any] = {"path": path_value, "exists": False, "writable": False, "error": ""}
    if not path_value:
        result["error"] = "not configured"
        return result
    p = Path(path_value)
    try:
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
        result["exists"] = p.is_dir() or p.is_file()
        if require_writable:
            probe = p / ".setup_write_test" if p.is_dir() else p
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            result["writable"] = True
        else:
            result["writable"] = os.access(str(p), os.W_OK)
    except Exception as ex:
        app.logger.warning("Path check failed for %r: %s", path_value, type(ex).__name__)
        result["error"] = "Could not verify this path."
    return result



_REDACTED = "[redacted]"

# Named key/value secrets: matches any identifier ending in one of the
# listed keywords (api_key/token/password/secret/auth), regardless of what
# prefix precedes it -- e.g. lidarr_api_key, slskd_api_key, plex_token,
# discogs_user_token, access_token, refresh_token, client_secret,
# user_token, auth_token all end in a covered keyword and are redacted by
# this single pattern without needing to enumerate every provider name.
#
# The negative lookahead after the separator keeps this idempotent:
# `_redact_diagnostic_text()` is applied repeatedly to the same text today
# (remote status normalization, failure-line extraction, and
# integration payload serialization each call it), so a value that is
# already exactly the `[redacted]` placeholder (optionally quoted, any
# case) must be left alone rather than re-matched -- otherwise the bare
# alternative's excluded-character class (which stops short of `]` so it
# doesn't swallow a trailing brace/bracket from surrounding structured
# text) partially re-matches the placeholder and strands its closing
# bracket, appending an extra `]` on every additional pass.
_SECRET_KV_RE = re.compile(
    r"""(?ix)
    \b([a-z0-9_-]*?(?:api[_-]?key|client[_-]?secret|token|password|secret|auth))
    (\s*[:=]\s*)
    (?!['"]?\[redacted\]['"]?)
    (?:"([^"]*)"|'([^']*)'|([^\s,;}\]&]+))
    """,
    re.VERBOSE,
)

# Authorization / Proxy-Authorization headers, with or without a Bearer/Basic
# scheme prefix (the scheme itself is not secret and is kept for context).
_AUTH_HEADER_RE = re.compile(r"(?i)\b(Authorization|Proxy-Authorization)(\s*:\s*)(?:(Bearer|Basic)(\s+))?(\S+)")

# Cookie / Set-Cookie header values -- redact the entire header value through
# end-of-line (not just the first token) so multi-value headers like
# `Cookie: session=alpha; refresh=bravo` don't leave later cookie pairs
# exposed. `[^\r\n]*` stops at the line boundary so it never consumes a
# following diagnostic line, and replacing the whole match unconditionally
# with the header name/separator/marker makes this self-healing and
# idempotent regardless of what (if anything) an earlier redaction pass
# left behind in the header value.
_COOKIE_HEADER_RE = re.compile(r"(?im)\b(Set-Cookie|Cookie)(\s*:\s*)[^\r\n]*")

# Credentials embedded in a URL's userinfo, e.g. https://user:password@host/
_URL_CREDENTIAL_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s/@]+@")

# Defense-in-depth cleanup: collapses a `[redacted]`/`[REDACTED]` marker
# followed by one or more stray `]` characters back down to a single clean
# marker. This mops up the specific corruption shape produced when *any*
# secret-shaped regex (including app.py's imported `_redact_security_text`,
# which this module does not control) re-scans text that already contains
# our placeholder: its bare-value character class also stops short of `]`,
# so it partially re-matches the placeholder and strands a trailing `]`
# rather than leaving it untouched. Applied as the last step of
# `_redact_diagnostic_text()` so the function is a safe fixed point after
# one pass no matter which upstream layer produced the placeholder.
_STRAY_REDACTED_BRACKET_RE = re.compile(r"(?i)(\[redacted\])\]+")


def _redact_diagnostic_text(text: str) -> str:
    """Redact secret-bearing diagnostic text before it can reach the System
    page. Covers named key/value secrets (quoted or bare, any prefix ending
    in api_key/token/password/secret/auth), Authorization/Proxy-Authorization
    headers (Bearer/Basic or bare), Cookie/Set-Cookie headers (the entire
    header value through end-of-line, not just the first cookie pair),
    query-string credentials, and credentials embedded in a URL's userinfo.

    This function may be called more than once on the same text as remote
    diagnostics are normalized and then serialized through integration
    payloads, so it must be idempotent: calling it again on its own output
    must return that output unchanged, with no accumulation of stray `]`
    characters and no corruption of an existing placeholder."""
    redacted = str(text or "")
    try:
        # Reuse app.py's generic secret-assignment/control-char sanitizer
        # (added for CodeQL exception sanitization) as a first pass when the
        # real app module is importable. routes_setup is also exercised in
        # tests against a bare stub `app` module (see test_routes_setup.py),
        # so this must degrade gracefully rather than raise.
        from app import _redact_security_text
        redacted = _redact_security_text(redacted)
    except Exception:
        pass
    redacted = _AUTH_HEADER_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{m.group(3) or ''}{m.group(4) or ''}{_REDACTED}",
        redacted,
    )
    redacted = _COOKIE_HEADER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", redacted)
    redacted = _URL_CREDENTIAL_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}@", redacted)
    redacted = _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", redacted)
    redacted = _STRAY_REDACTED_BRACKET_RE.sub(lambda m: m.group(1), redacted)
    return redacted


def _remote_beets_diagnostics_failure(
    error: str,
    *,
    timed_out: bool = False,
    diagnostic_error: str = "",
    remote_error: str = "unavailable",
) -> Dict[str, Any]:
    safe_error = _redact_diagnostic_text(error or "Beets control-agent status is unavailable.")
    return {
        "available": False,
        "path": os.environ.get("BEETS_API_URL", "http://beets:8338"),
        "version": "",
        "diagnostic_error": _redact_diagnostic_text(diagnostic_error),
        "plugins_returncode": None,
        "plugin_loader_returncode": None,
        "plugin_loader_ok": False,
        "plugin_loader_timed_out": bool(timed_out),
        "plugin_loader_error": safe_error,
        "configured_plugins": [],
        "loaded_plugins": [],
        "installed_plugins": {},
        "pluginpath": [],
        "plugin_failures": [],
        "replaygain_backend": "",
        "replaygain_command": "",
        "discogs_token_configured": bool(os.environ.get("DISCOGS_TOKEN") or os.environ.get("DISCOGS_USER_TOKEN")),
        "listenbrainz_token_configured": bool(os.environ.get("LISTENBRAINZ_TOKEN")),
        "fpcalc_available": False,
        "fpcalc_path": "",
        "ffmpeg_available": False,
        "ffmpeg_path": "",
        "pyacoustid_available": False,
        "capabilities": {},
        "commands": {},
        "remote_reachable": False,
        "remote_error": remote_error,
        "paths": {},
    }


def _beets_plugin_diagnostics(config_path: Path) -> Dict[str, Any]:
    """Return Beets plugin diagnostics from the authoritative remote control
    agent, never from a local `beet` executable in the web-manager process.

    The returned shape preserves the public setup-status fields used by the
    frontend while making the source of truth the authenticated control-agent
    `/status` response. Any connection, authentication, timeout, or malformed
    response fails closed: plugin_loader_ok is false and no configured/loaded
    plugin is trusted.
    """
    try:
        remote_status = beets_client.get_status()
    except BeetsAuthError:
        return _remote_beets_diagnostics_failure(
            "Beets control-agent authentication failed.",
            diagnostic_error="Beets control-agent authentication failed.",
            remote_error="authentication_failed",
        )
    except BeetsUnavailableError as ex:
        text = str(ex).lower()
        timed_out = "timed out" in text or "timeout" in text
        return _remote_beets_diagnostics_failure(
            "Beets control-agent status request timed out." if timed_out else "Beets control agent is unavailable.",
            timed_out=timed_out,
            diagnostic_error="Beets control agent is unavailable.",
            remote_error="timeout" if timed_out else "unavailable",
        )
    except TimeoutError:
        return _remote_beets_diagnostics_failure(
            "Beets control-agent status request timed out.",
            timed_out=True,
            diagnostic_error="Beets control-agent status request timed out.",
            remote_error="timeout",
        )
    except BeetsError:
        return _remote_beets_diagnostics_failure(
            "Beets control-agent status request failed.",
            diagnostic_error="Beets control-agent status request failed.",
            remote_error="request_failed",
        )
    except Exception:
        app.logger.error("Remote Beets diagnostics failed: %s", "unexpected_error")
        return _remote_beets_diagnostics_failure(
            "Beets control-agent status request failed.",
            diagnostic_error="Beets control-agent status request failed.",
            remote_error="request_failed",
        )

    if not isinstance(remote_status, dict) or remote_status.get("status") != "ok":
        return _remote_beets_diagnostics_failure(
            "Malformed Beets control-agent status response.",
            diagnostic_error="Malformed Beets control-agent status response.",
            remote_error="malformed_response",
        )

    configured_plugins = remote_status.get("configured_plugins")
    loaded_plugins = remote_status.get("loaded_plugins")
    plugin_failures = remote_status.get("plugin_failures")
    commands = remote_status.get("commands")
    capabilities = remote_status.get("capabilities")
    if not isinstance(configured_plugins, list) or not isinstance(loaded_plugins, list):
        return _remote_beets_diagnostics_failure(
            "Malformed Beets control-agent status response.",
            diagnostic_error="Malformed Beets control-agent status response.",
            remote_error="malformed_response",
        )
    if plugin_failures is not None and not isinstance(plugin_failures, list):
        return _remote_beets_diagnostics_failure(
            "Malformed Beets control-agent status response.",
            diagnostic_error="Malformed Beets control-agent status response.",
            remote_error="malformed_response",
        )
    if commands is not None and not isinstance(commands, dict):
        return _remote_beets_diagnostics_failure(
            "Malformed Beets control-agent status response.",
            diagnostic_error="Malformed Beets control-agent status response.",
            remote_error="malformed_response",
        )
    if capabilities is not None and not isinstance(capabilities, dict):
        return _remote_beets_diagnostics_failure(
            "Malformed Beets control-agent status response.",
            diagnostic_error="Malformed Beets control-agent status response.",
            remote_error="malformed_response",
        )

    loader_ok = bool(remote_status.get("plugin_loader_ok"))
    loader_error = str(remote_status.get("plugin_loader_error") or "")
    if not loader_ok and not loader_error:
        loader_error = "Beets plugin loader did not complete successfully."

    return {
        "available": bool(remote_status.get("beet_available", True)),
        "path": str(remote_status.get("beetsdir") or os.environ.get("BEETS_API_URL", "http://beets:8338")),
        "version": str(remote_status.get("beets_version") or ""),
        "diagnostic_error": "",
        "plugins_returncode": remote_status.get("plugin_loader_returncode"),
        "plugin_loader_returncode": remote_status.get("plugin_loader_returncode"),
        "plugin_loader_ok": loader_ok,
        "plugin_loader_timed_out": bool(remote_status.get("plugin_loader_timed_out")),
        "plugin_loader_error": _redact_diagnostic_text(loader_error),
        "configured_plugins": list(dict.fromkeys(str(p) for p in configured_plugins if str(p).strip())),
        "loaded_plugins": list(dict.fromkeys(str(p) for p in loaded_plugins if str(p).strip())),
        "installed_plugins": remote_status.get("installed_plugins") if isinstance(remote_status.get("installed_plugins"), dict) else {},
        "pluginpath": remote_status.get("pluginpath") if isinstance(remote_status.get("pluginpath"), list) else [],
        "plugin_failures": [_redact_diagnostic_text(str(line)) for line in (plugin_failures or [])][:12],
        "replaygain_backend": str(remote_status.get("replaygain_backend") or ""),
        "replaygain_command": str(remote_status.get("replaygain_command") or ""),
        "discogs_token_configured": bool(remote_status.get("discogs_token_configured")),
        "listenbrainz_token_configured": bool(remote_status.get("listenbrainz_token_configured")),
        "fpcalc_available": bool(remote_status.get("fpcalc_available")),
        "fpcalc_path": str(remote_status.get("fpcalc_path") or ""),
        "ffmpeg_available": bool(remote_status.get("ffmpeg_available")),
        "ffmpeg_path": str(remote_status.get("ffmpeg_path") or ""),
        "pyacoustid_available": bool(remote_status.get("pyacoustid_available")),
        "capabilities": capabilities or {},
        "commands": commands or {},
        "remote_reachable": True,
        "remote_error": "",
        "paths": remote_status.get("paths") if isinstance(remote_status.get("paths"), dict) else {},
    }

def _plugin_failure_for(failures: List[str], plugin: str) -> str:
    plugin_l = plugin.lower()
    for failure in failures:
        lower = failure.lower()
        if plugin_l in lower or (plugin_l == "replaygain" and "replaygain" in lower):
            return failure
    return ""


def _integration_status(
    *,
    configured: bool,
    required: bool = False,
    state: str | None = None,
    note: str = "",
    detail: str = "",
) -> Dict[str, Any]:
    resolved_state = state or ("configured" if configured else "not_configured")
    payload: Dict[str, Any] = {
        "configured": bool(configured),
        "required": bool(required),
        "state": resolved_state,
    }
    if note:
        # Defense-in-depth: note/detail text can originate from sanitized
        # diagnostic output built elsewhere in this module, but redacting
        # again here means no integration payload can leak a secret even if
        # a future caller forgets to sanitize before passing it in.
        payload["note"] = _redact_diagnostic_text(note)
    if detail:
        payload["detail"] = _redact_diagnostic_text(detail)
    return payload


def _plugin_integration_status(
    name: str,
    diagnostics: Dict[str, Any],
    *,
    required: bool = False,
    configured_when_enabled: bool = True,
    token_configured: bool | None = None,
    note: str = "",
) -> Dict[str, Any]:
    configured_plugins = set(diagnostics.get("configured_plugins") or [])
    loaded_plugins = set(diagnostics.get("loaded_plugins") or [])
    failures = list(diagnostics.get("plugin_failures") or [])
    failure = _plugin_failure_for(failures, name)
    if failure:
        return _integration_status(
            configured=False,
            required=required,
            state="dependency_plugin_missing",
            detail=failure,
        )
    if not diagnostics.get("plugin_loader_ok"):
        # The loader itself did not complete successfully (nonzero exit,
        # timeout, missing config, missing executable) for a reason other
        # than this specific plugin -- do not report this plugin as
        # configured/healthy just because its name appears in config.yaml;
        # `configured_plugins` is already empty in this case (see
        # _beets_plugin_diagnostics), but this explicit check keeps the
        # invariant true even if that ever changes.
        return _integration_status(
            configured=False,
            required=required,
            state="plugin_loader_failed",
            detail=str(diagnostics.get("plugin_loader_error") or ""),
            note="Beets plugin loader did not complete successfully; plugin state cannot be confirmed.",
        )
    if name not in configured_plugins:
        return _integration_status(
            configured=False,
            required=required,
            state="installed_but_disabled",
            note="Plugin is installed but not enabled in config.yaml.",
        )
    if name not in loaded_plugins:
        return _integration_status(
            configured=False,
            required=required,
            state="dependency_plugin_missing",
            note="Plugin is enabled in config.yaml but was not loaded by the Beets engine.",
        )
    if token_configured is not None:
        return _integration_status(
            configured=bool(token_configured),
            required=required,
            state="configured" if token_configured else "not_configured",
            note=note,
        )
    return _integration_status(
        configured=configured_when_enabled,
        required=required,
        state="configured" if configured_when_enabled else "not_configured",
        note=note,
    )


def _loader_failed_status(diagnostics: Dict[str, Any], *, required: bool = False) -> Dict[str, Any]:
    """Shared fallback for integrations that depend on the plugin loader
    having actually run successfully (used when there's no single named
    plugin to blame via _plugin_failure_for)."""
    return _integration_status(
        configured=False,
        required=required,
        state="plugin_loader_failed",
        detail=str(diagnostics.get("plugin_loader_error") or ""),
        note="Beets plugin loader did not complete successfully; plugin state cannot be confirmed.",
    )


def _acoustid_integration_status(diagnostics: Dict[str, Any], fpcalc_path: str | None) -> Dict[str, Any]:
    plugin_failures = list(diagnostics.get("plugin_failures") or [])
    chroma_failure = _plugin_failure_for(plugin_failures, "chroma")
    if chroma_failure:
        return _integration_status(
            configured=False,
            state="dependency_plugin_missing",
            detail=chroma_failure,
            note="Chroma plugin failed to load.",
        )
    if not diagnostics.get("plugin_loader_ok"):
        return _loader_failed_status(diagnostics)
    if not fpcalc_path:
        return _integration_status(
            configured=False,
            state="dependency_plugin_missing",
            note="fpcalc is missing.",
        )
    configured_plugins = set(diagnostics.get("configured_plugins") or [])
    loaded_plugins = set(diagnostics.get("loaded_plugins") or [])
    if "chroma" not in configured_plugins:
        return _integration_status(
            configured=False,
            state="installed_but_disabled",
            note="Chroma plugin is installed but not enabled in config.yaml.",
        )
    if "chroma" not in loaded_plugins:
        return _integration_status(
            configured=False,
            state="dependency_plugin_missing",
            note="Chroma is enabled but was not loaded by the Beets engine.",
        )
    if not diagnostics.get("pyacoustid_available"):
        return _integration_status(
            configured=False,
            state="dependency_plugin_missing",
            note="pyacoustid is missing in the Beets engine.",
        )
    return _integration_status(
        configured=True,
        state="configured",
        note="Fingerprinting and AcoustID lookup support are available; submission also requires a user key.",
    )


def _replaygain_integration_status(diagnostics: Dict[str, Any], ffmpeg_path: str | None) -> Dict[str, Any]:
    plugin_failures = list(diagnostics.get("plugin_failures") or [])
    replaygain_failure = _plugin_failure_for(plugin_failures, "replaygain")
    if replaygain_failure:
        return _integration_status(
            configured=False,
            state="dependency_plugin_missing",
            detail=replaygain_failure,
            note="ReplayGain plugin failed to load.",
        )
    if not diagnostics.get("plugin_loader_ok"):
        return _loader_failed_status(diagnostics)
    configured_plugins = set(diagnostics.get("configured_plugins") or [])
    loaded_plugins = set(diagnostics.get("loaded_plugins") or [])
    replaygain_backend = diagnostics.get("replaygain_backend") or ""
    replaygain_command = diagnostics.get("replaygain_command") or ""
    if "replaygain" not in configured_plugins:
        return _integration_status(
            configured=False,
            state="installed_but_disabled",
            note="ReplayGain must use an installed backend.",
        )
    if "replaygain" not in loaded_plugins:
        return _integration_status(
            configured=False,
            state="dependency_plugin_missing",
            note="ReplayGain is enabled but was not loaded by the Beets engine.",
        )
    if replaygain_backend == "ffmpeg" and ffmpeg_path and not replaygain_command:
        return _integration_status(
            configured=True,
            state="configured",
            note="ReplayGain uses the installed ffmpeg backend.",
        )
    return _integration_status(
        configured=False,
        state="dependency_plugin_missing",
        note="ReplayGain must use an installed backend.",
    )


def _fetchart_namespace_probe() -> Dict[str, bool]:
    """In-process check that beetsplug.fetchart actually resolves from the
    installed Beets distribution, and that beetsplug's own __path__ shows a
    real merge of more than one directory (bundled + installed) rather than
    one directory exclusively shadowing the other -- the exact failure a
    beetsplug/__init__.py package initializer previously caused. Never
    raises; a broken import here is itself the signal.

    Deliberately avoids matching the literal string "site-packages" in a
    resolved module's path -- that string is specific to one installation
    layout and is absent for editable installs, some distro dist-packages
    layouts, and other valid installed-package locations. Instead this
    checks that the resolved module does not live under this app's own
    bundled code directory, and cross-checks with importlib.metadata that
    beets itself is a real installed distribution.
    """
    importable_in_process = False
    installed = False
    bundled_namespace_merged = False
    app_root = str(Path(__file__).resolve().parent)

    def _is_bundled(path_str: str) -> bool:
        resolved = str(Path(path_str).resolve())
        return resolved == app_root or resolved.startswith(app_root + os.sep)

    try:
        spec = importlib.util.find_spec("beetsplug.fetchart")
        importable_in_process = spec is not None
        origin_is_bundled = bool(spec and spec.origin) and _is_bundled(spec.origin)
        try:
            import importlib.metadata as _metadata
            _metadata.distribution("beets")
            beets_distribution_installed = True
        except Exception:
            beets_distribution_installed = False
        installed = importable_in_process and beets_distribution_installed and not origin_is_bundled
        beetsplug_spec = importlib.util.find_spec("beetsplug")
        path_entries = [str(p) for p in list(getattr(beetsplug_spec, "submodule_search_locations", None) or [])]
        bundled_namespace_merged = (
            len(path_entries) > 1 and any(not _is_bundled(p) for p in path_entries)
        )
    except Exception:
        pass
    return {
        "importable_in_process": importable_in_process,
        "installed": installed,
        "bundled_namespace_merged": bundled_namespace_merged,
    }


def _fetchart_integration_status(diagnostics: Dict[str, Any]) -> Dict[str, Any]:
    """FetchArt diagnostic distinguishing configured/installed/importable/
    loadable-by-the-real-loader/namespace-merged/operational -- never
    reports operational merely because "fetchart" appears in config.yaml,
    find_spec() resolves something, or the dependency package is installed.
    Operational requires the authoritative Beets control-agent status to have
    loaded it, with no recognized plugin-failure pattern, in addition to
    every other signal.
    """
    plugin_failures = list(diagnostics.get("plugin_failures") or [])
    fetchart_failure = _plugin_failure_for(plugin_failures, "fetchart")
    configured_plugins = set(diagnostics.get("configured_plugins") or [])
    loaded_plugins = set(diagnostics.get("loaded_plugins") or [])
    configured = "fetchart" in configured_plugins
    loaded = "fetchart" in loaded_plugins
    probe = _fetchart_namespace_probe()
    loadable_by_beet_cli = bool(diagnostics.get("plugin_loader_ok")) and loaded and not fetchart_failure
    operational = bool(configured and loadable_by_beet_cli)

    if fetchart_failure:
        base = _integration_status(
            configured=False, required=True, state="dependency_plugin_missing",
            detail=fetchart_failure, note="FetchArt plugin failed to load.",
        )
    elif not diagnostics.get("plugin_loader_ok"):
        base = _loader_failed_status(diagnostics, required=True)
    elif not configured:
        base = _integration_status(
            configured=False, required=True, state="installed_but_disabled",
            note="FetchArt is installed but not enabled in config.yaml.",
        )
    elif not loaded:
        base = _integration_status(
            configured=False, required=True, state="dependency_plugin_missing",
            note="FetchArt is enabled but was not loaded by the Beets engine.",
        )
    else:
        base = _integration_status(
            configured=True, required=True, state="configured",
            note="Album artwork fetch is loaded and operational." if operational
            else "FetchArt is enabled but not confirmed operational.",
        )
    return {
        **base,
        "installed": probe["installed"],
        "importable_in_process": probe["importable_in_process"],
        "loadable_by_beet_cli": loadable_by_beet_cli,
        "bundled_namespace_merged": probe["bundled_namespace_merged"],
        "operational": operational,
    }


_STATUS_CACHE_LOCK = threading.Lock()
_STATUS_CACHE_DATA: Dict[str, Any] | None = None
_STATUS_CACHE_TS: float = 0.0
_STATUS_CACHE_TTL_SECONDS: float = 10.0
# How long a previously-good snapshot may still be served (marked "stale")
# after a rebuild attempt fails, e.g. the control agent goes down mid-outage.
# Bounded so a dead engine can never make /api/setup/status report old
# "healthy" data forever -- past this window a failed rebuild is a hard 503.
_STATUS_CACHE_MAX_STALE_SECONDS: float = 60.0


def _invalidate_setup_status_cache() -> None:
    """Drop the cached /api/setup/status snapshot so the next request rebuilds
    it from the live control agent instead of serving a now-outdated one.
    Call after any change that can affect setup status (env/config writes,
    auth token regeneration, setup completion)."""
    global _STATUS_CACHE_DATA, _STATUS_CACHE_TS
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE_DATA = None
        _STATUS_CACHE_TS = 0.0


def _build_setup_status_payload() -> Dict[str, Any]:
    settings = _load_settings()

    beets_config_path = Path(os.environ.get("BEETS_CONFIG", "/config/config.yaml"))
    diagnostics = _beets_plugin_diagnostics(beets_config_path)
    remote_paths = diagnostics.get("paths") if isinstance(diagnostics.get("paths"), dict) else {}

    def _remote_path(name: str, default_path: str, *, require_writable: bool = False) -> Dict[str, Any]:
        data = remote_paths.get(name)
        if isinstance(data, dict):
            return {
                "path": str(data.get("path") or default_path),
                "exists": bool(data.get("exists")),
                "is_dir": bool(data.get("is_dir")),
                "readable": bool(data.get("readable")),
                "writable": bool(data.get("writable")),
                "ok": bool(data.get("ok")),
            }
        return {
            "path": default_path,
            "exists": False,
            "is_dir": False,
            "readable": False,
            "writable": False,
            "ok": False,
        }

    config_check = _remote_path("config", "/config", require_writable=True)
    music_check = _remote_path("music_library", "/data/media/music")
    downloads_check = _remote_path("downloads", "/data/torrents", require_writable=True)
    remote_config_file = remote_paths.get("beets_config") if isinstance(remote_paths.get("beets_config"), dict) else {}
    beets_config_exists = bool(remote_config_file.get("exists"))
    beets_config_report_path = str(remote_config_file.get("path") or beets_config_path)

    fpcalc_path = diagnostics.get("fpcalc_path") if diagnostics.get("fpcalc_available") else ""
    ffmpeg_path = diagnostics.get("ffmpeg_path") if diagnostics.get("ffmpeg_available") else ""

    integrations = {
        "ai": _integration_status(
            configured=bool(
                os.environ.get("OPENAI_API_KEY")
                or os.environ.get("OPENROUTER_API_KEY")
                or os.environ.get("AI_API_KEY")
            ),
            state="configured" if (
                os.environ.get("OPENAI_API_KEY")
                or os.environ.get("OPENROUTER_API_KEY")
                or os.environ.get("AI_API_KEY")
            ) else "not_configured",
            note="Optional - not configured." if not (
                os.environ.get("OPENAI_API_KEY")
                or os.environ.get("OPENROUTER_API_KEY")
                or os.environ.get("AI_API_KEY")
            ) else "Optional provider configured.",
        ),
        "musicbrainz": _plugin_integration_status(
            "musicbrainz",
            diagnostics,
            required=True,
            note="Public MusicBrainz metadata source; no user API key required.",
        ),
        "acoustid": _acoustid_integration_status(diagnostics, fpcalc_path),
        "discogs": _plugin_integration_status(
            "discogs",
            diagnostics,
            token_configured=bool(diagnostics.get("discogs_token_configured")),
            note="Set DISCOGS_TOKEN or DISCOGS_USER_TOKEN to enable Discogs candidates.",
        ),
        "lastgenre": _plugin_integration_status("lastgenre", diagnostics),
        "listenbrainz": _plugin_integration_status(
            "listenbrainz",
            diagnostics,
            token_configured=bool(diagnostics.get("listenbrainz_token_configured")),
            note="Set LISTENBRAINZ_TOKEN to enable ListenBrainz submission.",
        ),
        "discpath": _plugin_integration_status(
            "discpath",
            diagnostics,
            note="User plugins load from /config/beetsplug before bundled plugins in /opt/beets-web-manager-agent/beetsplug.",
        ),
        "fetchart": _fetchart_integration_status(diagnostics),
        "embedart": _plugin_integration_status(
            "embedart",
            diagnostics,
            note="EmbedArt plugin embeds artwork files directly into audio file tags.",
        ),
        "scrub": _plugin_integration_status(
            "scrub",
            diagnostics,
            note="Scrub plugin strips unwanted tags prior to writing official Beets metadata.",
        ),
        "zero": _plugin_integration_status(
            "zero",
            diagnostics,
            note="Zero plugin nulls out selected fields upon import.",
        ),
        "ftintitle": _plugin_integration_status(
            "ftintitle",
            diagnostics,
            note="FtInTitle plugin moves featured artist names into track titles.",
        ),
        "mbsync": _plugin_integration_status(
            "mbsync",
            diagnostics,
            note="MBSync plugin fetches updated release/recording metadata from MusicBrainz.",
        ),
        "bpsync": _plugin_integration_status(
            "bpsync",
            diagnostics,
            note="BPSync plugin syncs Beatport metadata.",
        ),
        "replaygain": _replaygain_integration_status(diagnostics, ffmpeg_path),
        "plex": _integration_status(
            configured=bool(os.environ.get("PLEX_URL") and os.environ.get("PLEX_TOKEN")),
            note="Application Plex workflow; no default Beets plexsync plugin is required.",
        ),
        "lidarr": _integration_status(
            configured=bool(os.environ.get("LIDARR_URL") and os.environ.get("LIDARR_API_KEY")),
        ),
        "slskd": _integration_status(
            configured=bool(os.environ.get("SLSKD_URL") and (os.environ.get("SLSKD_API_KEY") or os.environ.get("SLSKD_API_KEY_FILE"))),
        ),
    }

    blocking = []
    if not config_check["writable"]:
        blocking.append(f"Cannot write to config directory {config_check['path']}")
    if not music_check["readable"]:
        blocking.append(f"Music library path {music_check['path']} is not accessible")
    if not downloads_check["writable"]:
        blocking.append(f"Cannot write to downloads/staging path {downloads_check['path']}")
    if not beets_config_exists:
        blocking.append(
            f"Beets config not found at {beets_config_report_path} - copy config.yaml.example to config.yaml"
        )
    if not fpcalc_path:
        blocking.append("fpcalc (chromaprint) not found on PATH — AcoustID fingerprinting will not work")
    if beets_config_exists and not diagnostics.get("plugin_loader_ok"):
        blocking.append(
            "Beets plugin loader did not complete successfully — see beets.plugin_loader_error for details."
        )

    ready = not blocking
    demo_mode = os.environ.get("DEMO_MODE", "0").strip().lower() in ("1", "true", "yes", "on")
    try:
        from app import (
            _auth_secret_is_usable,
            _security_auth_token,
            _security_auth_password,
            _security_auth_username,
            _first_run_setup_required,
            _GENERATED_AUTH_TOKEN_FILE,
            _INITIAL_BROWSER_PASSWORD_FILE,
        )
        token_configured = _auth_secret_is_usable(_security_auth_token())
        password_configured = _auth_secret_is_usable(_security_auth_password())
        token_auto_generated = token_configured and _GENERATED_AUTH_TOKEN_FILE.exists()
        password_auto_generated = password_configured and _INITIAL_BROWSER_PASSWORD_FILE.exists()
        username = _security_auth_username()
        first_run_req = _first_run_setup_required()
    except ImportError:
        token_configured = _fallback_auth_secret_usable(
            os.environ.get("BEETS_WEB_AUTH_TOKEN", "") or os.environ.get("BEETS_WEB_TOKEN", "")
        )
        password_configured = _fallback_auth_secret_usable(os.environ.get("BEETS_WEB_PASSWORD", ""))
        token_auto_generated = token_configured and _FALLBACK_AUTH_TOKEN_FILE.exists()
        password_auto_generated = False
        username = os.environ.get("BEETS_WEB_USERNAME", "admin").strip() or "admin"
        first_run_req = not password_configured
    auth_status = {
        "token_configured": token_configured,
        "token_auto_generated": token_auto_generated,
        "password_configured": password_configured,
        "password_auto_generated": password_auto_generated,
        "username": username,
        "first_run_required": first_run_req,
    }
    return {
        "ok": True,
        "status": "ready" if ready else "warning",
        "version": _APP_VERSION,
        "demo_mode": demo_mode,
        "setup_complete": _SETUP_COMPLETE_MARKER.exists(),
        "first_run": {
            "required": first_run_req,
        },
        "blocking_reasons": blocking,
        "paths": {
            "config": config_check,
            "music_library": music_check,
            "downloads": downloads_check,
            "beets_config": {"path": beets_config_report_path, "exists": beets_config_exists},
        },
        "fpcalc": {"available": bool(fpcalc_path), "path": fpcalc_path or ""},
        "beets": diagnostics,
        "auth": auth_status,
        "integrations": integrations,
        "settings": {k: (_mask(v) if "key" in k.lower() or "token" in k.lower() else v)
                     for k, v in settings.items()},
    }


@app.get("/api/setup/status")
def setup_status():
    """Readiness snapshot, single-flight cached for _STATUS_CACHE_TTL_SECONDS.

    The whole check-build-store sequence runs under one lock so N concurrent
    requests past the TTL trigger exactly one upstream rebuild against the
    control agent, not N of them (no cache-stampede thundering herd). Uses
    time.monotonic() so a wall-clock adjustment (NTP, DST, manual change)
    can never make the cache look older or younger than it really is.

    A failed rebuild serves the last-known snapshot marked "stale": true
    (bounded by _STATUS_CACHE_MAX_STALE_SECONDS) instead of either wiping out
    a still-useful snapshot or silently reporting it as fresh/healthy
    forever. Past that bound, or with no prior snapshot at all, a failed
    rebuild is a hard 503 -- never a masked 200.
    """
    global _STATUS_CACHE_DATA, _STATUS_CACHE_TS
    force = request.args.get("refresh", "0") == "1" or request.args.get("force", "0") == "1"

    with _STATUS_CACHE_LOCK:
        now = time.monotonic()
        if not force and _STATUS_CACHE_DATA is not None and (now - _STATUS_CACHE_TS) < _STATUS_CACHE_TTL_SECONDS:
            res = dict(_STATUS_CACHE_DATA)
            res["cached"] = True
            res["stale"] = False
            res["cache_age_seconds"] = round(now - _STATUS_CACHE_TS, 2)
            return jsonify(res)

        try:
            payload = _build_setup_status_payload()
        except Exception as ex:
            has_prior = _STATUS_CACHE_DATA is not None
            age = (now - _STATUS_CACHE_TS) if has_prior else None
            if has_prior and age < _STATUS_CACHE_MAX_STALE_SECONDS:
                res = dict(_STATUS_CACHE_DATA)
                res["cached"] = True
                res["stale"] = True
                res["cache_age_seconds"] = round(age, 2)
                res["refresh_error"] = str(ex)
                return jsonify(res)
            return jsonify({"error": str(ex), "status": "failed"}), 503

        _STATUS_CACHE_DATA = payload
        _STATUS_CACHE_TS = now
        res = dict(payload)
        res["cached"] = False
        res["stale"] = False
        res["cache_age_seconds"] = 0.0
        return jsonify(res)


@app.get("/api/setup/diagnostics")
def setup_diagnostics():
    """Explicit fresh setup diagnostics report: always bypasses the cache and
    rebuilds from the live control agent (unlike /api/setup/status, which
    previously called through to this same cached path despite the docstring
    promising otherwise), then re-primes the cache with the fresh result so
    the next /api/setup/status call is also fresh."""
    global _STATUS_CACHE_DATA, _STATUS_CACHE_TS
    try:
        payload = _build_setup_status_payload()
    except Exception as ex:
        return jsonify({"error": str(ex), "status": "failed"}), 503
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE_DATA = payload
        _STATUS_CACHE_TS = time.monotonic()
    res = dict(payload)
    res["cached"] = False
    res["stale"] = False
    res["cache_age_seconds"] = 0.0
    return jsonify(res)


@app.get("/api/setup/env")
def setup_get_env():
    """Return editable .env metadata with secret values masked."""
    return jsonify(_setup_env_payload())


@app.post("/api/setup/env")
def setup_save_env():
    """Persist setup-managed environment variables to a .env-style file.

    Blank secret fields are ignored unless the key is explicitly listed in
    `clear`, so password inputs do not accidentally erase credentials.
    """
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "expected a JSON object"}), 400
    raw_updates = payload.get("variables") or {}
    raw_clear = payload.get("clear") or []
    if not isinstance(raw_updates, dict) or not isinstance(raw_clear, list):
        return jsonify({"ok": False, "error": "expected variables object and clear list"}), 400

    updates: Dict[str, str] = {}
    for key, raw_value in raw_updates.items():
        key = str(key)
        value = "" if raw_value is None else str(raw_value)
        if _is_secret_env(key) and value == "" and key not in raw_clear:
            continue
        updates[key] = value
    clear = [str(key) for key in raw_clear]
    if not updates and not clear:
        return jsonify(_setup_env_payload({
            "saved": [],
            "backup_path": "",
            "process_applied": False,
        }))
    try:
        backup_path = _write_env_file(updates, clear)
    except ValueError as ex:
        return jsonify({"ok": False, "error": str(ex)}), 400
    except Exception as ex:
        app.logger.warning("Could not save environment file: %s", type(ex).__name__)
        return jsonify({"ok": False, "error": "Could not save environment file."}), 500
    _invalidate_setup_status_cache()
    return jsonify(_setup_env_payload({
        "saved": sorted(set(updates) | set(clear)),
        "backup_path": backup_path,
        "process_applied": True,
    }))


@app.post("/api/setup/test/ai")
def setup_test_ai():
    """Live connectivity test against the configured (or posted) AI provider."""
    payload = request.get_json(silent=True) or {}
    api_key = payload.get("api_key") or os.environ.get("OPENAI_API_KEY") or os.environ.get("AI_API_KEY")
    base_url = payload.get("base_url") or os.environ.get("AI_BASE_URL") or "https://api.openai.com/v1"
    model = payload.get("model") or os.environ.get("AI_MODEL") or "gpt-4o-mini"
    if not api_key:
        return jsonify({"ok": False, "status": "not_configured",
                         "error": "No AI API key configured. Set OPENAI_API_KEY (or your provider's key) and retry."}), 200
    base_host = (urllib.parse.urlsplit(base_url).hostname or "").lower()
    is_openai_host = base_host == "api.openai.com" or base_host.endswith(".api.openai.com")
    try:
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/models/{model}" if is_openai_host else f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        return jsonify({"ok": True, "status": "ready", "model": model})
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            error = "The AI provider rejected the API key. Verify the key, selected provider, base URL, and model."
        elif exc.code == 404:
            error = f"Model {model!r} was not found for this provider/account."
        else:
            error = f"AI provider returned HTTP {exc.code}."
        return jsonify({"ok": False, "status": "failed", "error": error}), 200
    except Exception as ex:
        app.logger.warning("AI provider connectivity test failed: %s", type(ex).__name__)
        return jsonify({"ok": False, "status": "failed",
                         "error": "Could not reach the AI provider."}), 200


@app.post("/api/setup/test/musicbrainz")
def setup_test_musicbrainz():
    """MusicBrainz needs no API key for lookups — this just confirms reachability
    and a well-formed User-Agent (MusicBrainz blocks generic/missing UAs)."""
    try:
        req = urllib.request.Request(
            "https://musicbrainz.org/ws/2/release/?query=release:test&limit=1&fmt=json",
            headers={"User-Agent": "BeetsWebManager/1.0 (+https://github.com/)"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            json.loads(r.read())
        return jsonify({"ok": True, "status": "ready"})
    except urllib.error.HTTPError as exc:
        if exc.code == 503:
            error = "MusicBrainz rate-limited this request (max ~1 req/sec per IP). Try again shortly."
        else:
            error = f"MusicBrainz returned HTTP {exc.code}."
        return jsonify({"ok": False, "status": "failed", "error": error}), 200
    except Exception as ex:
        app.logger.warning("MusicBrainz connectivity test failed: %s", type(ex).__name__)
        return jsonify({"ok": False, "status": "failed", "error": "Could not reach MusicBrainz."}), 200


@app.post("/api/setup/test/acoustid")
def setup_test_acoustid():
    """Distinguishes fpcalc availability from API-key validity — a key string
    existing is not treated as 'configured' without a real lookup."""
    payload = request.get_json(silent=True) or {}
    api_key = payload.get("api_key") or os.environ.get("ACOUSTID_API_KEY") or os.environ.get("ACOUSTID_KEY")
    fpcalc_path = shutil.which("fpcalc")
    result: Dict[str, Any] = {"fpcalc_available": bool(fpcalc_path)}
    if not fpcalc_path:
        result.update({"ok": False, "status": "failed",
                       "error": "fpcalc (chromaprint) is not installed or not on PATH."})
        return jsonify(result), 200
    try:
        proc = subprocess.run([fpcalc_path, "-version"], capture_output=True, text=True, timeout=5)
        result["fpcalc_version"] = (proc.stdout or proc.stderr or "").strip()
    except Exception as ex:
        app.logger.warning("fpcalc failed to run: %s", type(ex).__name__)
        result.update({"ok": False, "status": "failed", "error": "fpcalc failed to run."})
        return jsonify(result), 200
    if not api_key:
        result.update({"ok": False, "status": "not_configured",
                       "error": "No AcoustID API key configured — lookups will use a shared, rate-limited test key."})
        return jsonify(result), 200
    try:
        params = urllib.parse.urlencode({
            "client": api_key, "format": "json",
            "duration": "1", "fingerprint": "AQAAA0mUaEkSRZEeJk-eHtWMh4",  # tiny placeholder for a key/connectivity check
        })
        req = urllib.request.Request(f"https://api.acoustid.org/v2/lookup?{params}")
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        if data.get("status") == "error":
            result.update({"ok": False, "status": "failed",
                           "error": data.get("error", {}).get("message", "AcoustID rejected the request.")})
        else:
            result.update({"ok": True, "status": "ready"})
    except Exception as ex:
        app.logger.warning("AcoustID connectivity test failed: %s", type(ex).__name__)
        result.update({"ok": False, "status": "failed", "error": "Could not reach AcoustID."})
    return jsonify(result), 200


@app.post("/api/setup/test/plex")
def setup_test_plex():
    payload = request.get_json(silent=True) or {}
    plex_url = (payload.get("url") or os.environ.get("PLEX_URL") or "").rstrip("/")
    plex_token = payload.get("token") or os.environ.get("PLEX_TOKEN")
    if not plex_url or not plex_token:
        return jsonify({"ok": False, "status": "not_configured",
                         "error": "PLEX_URL and PLEX_TOKEN are both required to test Plex."}), 200
    try:
        req = urllib.request.Request(f"{plex_url}/library/sections", headers={"X-Plex-Token": plex_token})
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
        libraries = []
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(body)
            libraries = [d.get("title") for d in root.findall("Directory") if d.get("type") == "artist"]
        except Exception:
            pass
        return jsonify({"ok": True, "status": "ready", "music_libraries": libraries})
    except urllib.error.HTTPError as exc:
        error = "Plex token is invalid or expired." if exc.code in (401, 403) else f"Plex returned HTTP {exc.code}."
        return jsonify({"ok": False, "status": "failed", "error": error}), 200
    except Exception as ex:
        app.logger.warning("Plex connectivity test failed: %s", type(ex).__name__)
        return jsonify({"ok": False, "status": "failed",
                         "error": "Could not reach Plex."}), 200


@app.get("/api/setup/settings")
def setup_get_settings():
    settings = _load_settings()
    return jsonify({"ok": True, "settings": {
        k: (_mask(v) if "key" in k.lower() or "token" in k.lower() else v) for k, v in settings.items()
    }})


@app.post("/api/setup/settings")
def setup_save_settings():
    """Persist wizard-configured values that don't have a dedicated env var
    (e.g. selected AI model). Real secrets should be set via .env / Docker
    secrets, not through this endpoint — this file is not treated as a secret
    store, only as a record of non-secret selections."""
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "expected a JSON object"}), 400
    settings = _load_settings()
    settings.update(payload)
    _save_settings(settings)
    return jsonify({"ok": True})


@app.post("/api/setup/auth-token/regenerate")
def setup_regenerate_auth_token():
    """Generate a fresh BEETS_WEB_AUTH_TOKEN and persist it, both to the
    editable .env file (so it's visible/masked like any other secret in the
    System page) and to the dedicated auto-generation file app.py's startup
    bootstrap reads (so a future restart with no other config still finds a
    usable token instead of silently locking itself out again).

    The plaintext value is returned exactly once, here, at generation time --
    it is never included in any other response (GET /api/setup/env always
    masks it, matching every other secret field).
    """
    try:
        from app import generate_secure_auth_token, _GENERATED_AUTH_TOKEN_FILE
        token = generate_secure_auth_token()
        token_file = _GENERATED_AUTH_TOKEN_FILE
    except ImportError:
        token = secrets.token_urlsafe(32)
        token_file = _FALLBACK_AUTH_TOKEN_FILE
    try:
        backup_path = _write_env_file({"BEETS_WEB_AUTH_TOKEN": token}, [])
    except ValueError as ex:
        return jsonify({"ok": False, "error": str(ex)}), 400
    except Exception as ex:
        app.logger.warning("Could not save environment file: %s", type(ex).__name__)
        return jsonify({"ok": False, "error": "Could not save environment file."}), 500
    try:
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(token, encoding="utf-8")
    except Exception:
        pass
    _invalidate_setup_status_cache()
    return jsonify({
        "ok": True,
        "token": token,
        "warning": "Save this token now — it will not be shown again.",
        "backup_path": backup_path,
    })


_FIRST_RUN_LOCK = threading.Lock()


@app.post("/api/setup/first-run")
def setup_first_run():
    """Narrow bootstrap endpoint for initial browser credentials during first-run setup."""
    from app import (
        _first_run_setup_required,
        _password_requirements_unmet,
        _auth_secret_is_usable,
        _persist_file_atomically,
        _cleanup_initial_browser_password_if_replaced,
        _PERSISTED_BROWSER_USERNAME_FILE,
        _PERSISTED_BROWSER_PASSWORD_FILE,
        _csrf_request_allowed,
        _json_security_error,
    )

    if not _csrf_request_allowed():
        return _json_security_error(403, "CSRF check failed")

    with _FIRST_RUN_LOCK:
        if not _first_run_setup_required():
            return jsonify({"ok": False, "error": "Setup already completed"}), 409

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "Invalid request body"}), 400

        allowed_keys = {"username", "password"}
        if any(k not in allowed_keys for k in payload.keys()):
            return jsonify({"ok": False, "error": "Only username and password may be provided"}), 400

        raw_user = payload.get("username", "")
        raw_pass = payload.get("password", "")

        if not isinstance(raw_user, str) or not isinstance(raw_pass, str):
            return jsonify({"ok": False, "error": "Username and password must be strings"}), 400

        username = raw_user.strip() or "admin"
        if len(username) > 64 or re.search(r"[\r\n\x00-\x1f]", username):
            return jsonify({"ok": False, "error": "Invalid username format"}), 400

        password = raw_pass.strip()
        unmet = _password_requirements_unmet(password)
        if unmet or not _auth_secret_is_usable(password):
            msg = "Password does not meet complexity rules: " + ", ".join(unmet) if unmet else "Password is not usable"
            return jsonify({"ok": False, "error": msg, "unmet_requirements": unmet}), 400

        user_ok = _persist_file_atomically(_PERSISTED_BROWSER_USERNAME_FILE, username)
        pass_ok = _persist_file_atomically(_PERSISTED_BROWSER_PASSWORD_FILE, password)

        if not (user_ok and pass_ok):
            return jsonify({"ok": False, "error": "Failed to persist credentials to storage"}), 500

        _cleanup_initial_browser_password_if_replaced()
        _invalidate_setup_status_cache()

        return jsonify({"ok": True, "message": "First-run setup complete"})


@app.post("/api/setup/complete")
def setup_mark_complete():
    """Mark first-run setup as done. Idempotent — safe to call repeatedly."""
    _SETUP_COMPLETE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    _SETUP_COMPLETE_MARKER.write_text("1", encoding="utf-8")
    _invalidate_setup_status_cache()
    return jsonify({"ok": True})


@app.get("/health/live")
def health_live():
    """Liveness probe: the process is up and answering HTTP. No dependency
    checks — a failing DB/path should not make Docker/k8s kill the container
    (that's what readiness is for)."""
    return jsonify({"status": "alive", "version": _APP_VERSION})


@app.get("/health/ready")
def health_ready():
    """Readiness probe backed by the authoritative Beets control agent."""
    diagnostics = _beets_plugin_diagnostics(Path(os.environ.get("BEETS_CONFIG", "/config/config.yaml")))
    remote_paths = diagnostics.get("paths") if isinstance(diagnostics.get("paths"), dict) else {}

    def _remote_path_ok(name: str, *, writable: bool = False) -> bool:
        data = remote_paths.get(name)
        if not isinstance(data, dict):
            return False
        return bool(data.get("writable") if writable else data.get("ok"))

    blocking = []
    if not diagnostics.get("remote_reachable"):
        blocking.append("beets control agent unavailable")
    if not _remote_path_ok("config", writable=True):
        blocking.append("config path not writable")
    if not _remote_path_ok("downloads", writable=True):
        blocking.append("downloads path not writable")
    beets_config = remote_paths.get("beets_config") if isinstance(remote_paths.get("beets_config"), dict) else {}
    if not beets_config.get("exists"):
        blocking.append("beets config missing")
    if beets_config.get("exists") and not diagnostics.get("plugin_loader_ok"):
        blocking.append("beets plugin loader failed")

    status = "ready" if not blocking else "warning"
    return jsonify({
        "status": status,
        "version": _APP_VERSION,
        "blocking_reasons": blocking,
        "beets": {
            "available": bool(diagnostics.get("available")),
            "remote_reachable": bool(diagnostics.get("remote_reachable")),
            "version": diagnostics.get("version") or "",
            "plugin_loader_ok": bool(diagnostics.get("plugin_loader_ok")),
        },
    }), (200 if not blocking else 503)


@app.get("/health")
def health_root():
    """Alias for /api/health under the unprefixed convention most container
    orchestrators probe by default."""
    return health_live()
