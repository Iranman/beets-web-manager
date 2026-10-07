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
import backend.provider_boundary as provider_boundary
import copy
import importlib.util
import json
import os
import posixpath
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import jsonify, request, session

# Imported after app.py has already defined app (circular-but-OK pattern,
# matches routes_jobs.py / routes_lidarr.py).
from app import app  # noqa: E402
from backend import config_layers  # noqa: E402
from backend.web_manager_config_store import (  # noqa: E402
    WebManagerConfigStore,
    WebManagerConfigStoreConflictError,
    WebManagerConfigStoreError,
)
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
_default_data_dir = os.environ.get("WEB_MANAGER_DATA_DIR", "/web-manager-data")
_SETTINGS_FILE = Path(os.environ.get("SETUP_SETTINGS_FILE", f"{_default_data_dir}/app_settings.json"))
_SETUP_COMPLETE_MARKER = Path(os.environ.get("SETUP_COMPLETE_FILE", f"{_default_data_dir}/.setup_complete"))
_SETUP_ENV_FILE = Path(os.environ.get("SETUP_ENV_FILE", f"{_default_data_dir}/.env"))
_ENV_EXAMPLE_FILE = Path(os.environ.get("SETUP_ENV_EXAMPLE_FILE", str(Path(__file__).parent / ".env.example")))
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_BLOCKED_ENV_NAMES = {"SETUP_ENV_FILE", "SETUP_ENV_EXAMPLE_FILE", "SETUP_SETTINGS_FILE", "SETUP_COMPLETE_FILE"}
_SECRET_ENV_PARTS = ("KEY", "TOKEN", "PASSWORD", "SECRET")
_PASSWORD_MIN_LENGTH_FLOOR = 12
_PASSWORD_MIN_LENGTH_DEFAULT = 16
_FALLBACK_AUTH_TOKEN_FILE = Path(os.environ.get("BEETS_WEB_AUTH_TOKEN_FILE", f"{_default_data_dir}/.auth_token"))
_PERSISTED_BROWSER_PASSWORD_FILE = Path(os.environ.get("BEETS_WEB_PERSISTED_PASSWORD_FILE", f"{_default_data_dir}/.browser_password"))
_INITIAL_BROWSER_PASSWORD_FILE = Path(os.environ.get("BEETS_WEB_INITIAL_PASSWORD_FILE", f"{_default_data_dir}/.initial_admin_password"))
_PERSISTED_BROWSER_USERNAME_FILE = Path(os.environ.get("BEETS_WEB_PERSISTED_USERNAME_FILE", f"{_default_data_dir}/.browser_username"))
_GENERATED_AUTH_TOKEN_FILE = _FALLBACK_AUTH_TOKEN_FILE
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
    """Browser passwords use a passphrase-friendly floor, separate from the
    machine API bearer-token floor enforced by app.py's _auth_secret_is_usable().
    """
    try:
        configured = int(os.environ.get("BEETS_WEB_PASSWORD_MIN_LENGTH", str(_PASSWORD_MIN_LENGTH_DEFAULT)))
    except ValueError:
        configured = _PASSWORD_MIN_LENGTH_DEFAULT
    configured = max(_PASSWORD_MIN_LENGTH_FLOOR, min(256, configured))
    return configured


def _password_looks_placeholder(value: str) -> bool:
    secret = (value or "").strip()
    compact = re.sub(r"[^a-z0-9]+", "", secret.lower())
    if "${" in secret or "?" in secret:
        return True
    if compact in _FALLBACK_PLACEHOLDER_AUTH_SECRETS:
        return True
    if compact.count("password") >= 2:
        return True
    return any(marker in compact for marker in ("changeme", "placeholder", "example", "setinenv"))


def _password_requirements_unmet(password: str) -> List[str]:
    """Returns unmet BEETS_WEB_PASSWORD requirements (empty list = passes)."""
    unmet: List[str] = []
    min_length = _password_min_length()
    if len(password) < min_length:
        unmet.append(f"at least {min_length} characters")
    if _password_looks_placeholder(password):
        unmet.append("not a placeholder password")
    return unmet
# Display-only catalog seed used when the image's .env.example is missing.
# It is never written to the saved settings file and lists application-layer
# keys only (backend/config_layers.py); Compose/host/container values live in
# docker-compose.yml.
_FALLBACK_ENV_TEMPLATE = """# Required owner/admin authentication
BEETS_WEB_AUTH_TOKEN=
BEETS_WEB_PASSWORD=
BEETS_WEB_USERNAME=admin
BEETS_WEB_AUTH_DISABLED=0
BEETS_WEB_AUTH_MIN_LENGTH=32
BEETS_WEB_PASSWORD_MIN_LENGTH=16
BEETS_TRUSTED_PROXIES=
BEETS_OUTBOUND_ALLOWLIST=

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
ACOUSTID_USER_KEY=
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


def _config_store_for_target(target_file: Path) -> Tuple[WebManagerConfigStore, str]:
    """Resolve the durable, authoritative WebManagerConfigStore for a Web
    Manager state file. Web Manager durable state lives beneath
    WEB_MANAGER_DATA_DIR only -- a target that does not resolve under it is
    rejected rather than promoting its own parent directory to a writable
    config root (that would let any caller-supplied path widen the trust
    boundary). Tests may inject WEB_MANAGER_DATA_DIR to a temporary path;
    every Web Manager state file constant defaults beneath it, so this holds
    for both production defaults and test injection."""
    target = target_file.expanduser().resolve(strict=False)
    data_root = Path(os.environ.get("WEB_MANAGER_DATA_DIR", "/web-manager-data")).expanduser().resolve(strict=False)
    try:
        rel = target.relative_to(data_root)
    except ValueError as exc:
        raise WebManagerConfigStoreError(
            f"{target} is not beneath the Web Manager data root {data_root}"
        ) from exc
    return WebManagerConfigStore(data_root), rel.as_posix()


def _settings_store_for_target() -> Tuple[WebManagerConfigStore, str]:
    return _config_store_for_target(_SETTINGS_FILE)


def _remove_config_file(target_file: Path) -> bool:
    store, relative_name = _config_store_for_target(target_file)
    record = store.read_text_record(relative_name)
    if not record.get("exists"):
        return False
    store.remove(relative_name, expected_revision=record.get("revision"))
    return True


def _load_settings_record() -> Dict[str, Any]:
    try:
        store, relative_name = _settings_store_for_target()
        record = store.read_text_record(relative_name)
        if not record.get("exists"):
            return {"settings": {}, "revision": None}
        return {
            "settings": json.loads(record.get("content") or "{}"),
            "revision": record.get("revision"),
        }
    except Exception:
        return {"settings": {}, "revision": None}


def _load_settings() -> Dict[str, Any]:
    return dict(_load_settings_record().get("settings") or {})


def _save_settings(data: Dict[str, Any], *, expected_revision: Optional[str] = None) -> Dict[str, Any]:
    store, relative_name = _settings_store_for_target()
    return store.save_json(
        relative_name,
        data,
        is_secret=False,
        expected_revision=expected_revision,
    )


def _setup_csrf_failure():
    """Require same-origin intent for first-run POST probes that make outbound checks."""
    if getattr(sys.modules.get("app"), "__routes_setup_test_stub__", False):
        return None
    try:
        from app import _csrf_request_allowed, _json_security_error, _security_auth_disabled
    except ImportError:
        return None
    if _security_auth_disabled():
        return None
    if not _csrf_request_allowed():
        return _json_security_error(403, "CSRF check failed")
    return None

_SECRET_MASK = "********"


def _mask(value: str) -> str:
    """A fixed placeholder for any configured secret (FE-16). It reveals
    neither characters nor length; the plaintext is only available through
    the re-authenticated reveal endpoint."""
    return _SECRET_MASK if str(value or "") else ""


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


_SETTING_METADATA: Dict[str, Dict[str, Any]] = {
    # 1. System & Environment
    "PUID": {
        "section": "System & Environment",
        "default": "1000",
        "description": "User ID for container file ownership and execution permissions",
        "secret": False,
        "editable": True,
        "restart_required": True,
        "type": "string",
    },
    "PGID": {
        "section": "System & Environment",
        "default": "1000",
        "description": "Group ID for container file ownership and execution permissions",
        "secret": False,
        "editable": True,
        "restart_required": True,
        "type": "string",
    },
    "TZ": {
        "section": "System & Environment",
        "default": "UTC",
        "description": "Timezone for scheduled jobs, logs, and timestamp formatting",
        "secret": False,
        "editable": True,
        "restart_required": True,
        "type": "string",
    },
    "WEBCONTROL_PORT": {
        "section": "System & Environment",
        "default": "8337",
        "description": "HTTP port published by Beets Web Manager UI and API",
        "secret": False,
        "editable": True,
        "restart_required": True,
        "type": "integer",
    },
    "DEMO_MODE": {
        "section": "System & Environment",
        "default": "0",
        "description": "Run in synthetic demo mode with simulated music data",
        "secret": False,
        "editable": True,
        "restart_required": True,
        "type": "boolean",
    },
    "BEETS_LOG": {
        "section": "System & Environment",
        "default": "/config/beet.log",
        "container_path": "/config/beet.log",
        "description": "Destination file for Beets engine execution logs",
        "secret": False,
        "editable": False,
        "restart_required": True,
        "type": "string",
    },
    "BEETS_LONG_OPERATION_MAX_SECONDS": {
        "section": "System & Environment",
        "default": "1800",
        "description": "Maximum execution window for long background operations before timeout",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "integer",
    },
    "BEETS_LONG_OPERATION_POLL_SECONDS": {
        "section": "System & Environment",
        "default": "2",
        "description": "Status polling interval in seconds for background jobs",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "integer",
    },

    # 2. Authentication & Security
    "BEETS_WEB_USERNAME": {
        "section": "Authentication & Security",
        "default": "admin",
        "description": "Username for browser login and basic authentication",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "string",
    },
    "BEETS_WEB_PASSWORD": {
        "section": "Authentication & Security",
        "default": None,
        "description": "Password for browser login (stored securely as a one-way hash)",
        "secret": True,
        "editable": True,
        "revealable": False,
        "restart_required": False,
        "type": "password",
    },
    "BEETS_WEB_AUTH_TOKEN": {
        "section": "Authentication & Security",
        "default": None,
        "description": "Bearer token for API, webhooks, and headless script clients",
        "secret": True,
        "editable": True,
        "revealable": True,
        "restart_required": False,
        "type": "secret",
    },
    "BEETS_WEB_AUTH_DISABLED": {
        "section": "Authentication & Security",
        "default": "0",
        "description": "Disable all authentication (1 = disabled, 0 = enabled)",
        "secret": False,
        "editable": True,
        "restart_required": True,
        "type": "boolean",
    },
    "BEETS_TRUSTED_PROXIES": {
        "section": "Authentication & Security",
        "default": "",
        "description": "Trusted reverse proxy CIDR ranges (comma-separated)",
        "secret": False,
        "editable": True,
        "restart_required": True,
        "type": "string",
    },
    "BEETS_WEB_SESSION_COOKIE_SECURE": {
        "section": "Authentication & Security",
        "default": "0",
        "description": "Require HTTPS for session cookies (1 = secure HTTPS-only, 0 = standard)",
        "secret": False,
        "editable": True,
        "restart_required": True,
        "type": "boolean",
    },
    "BEETS_OUTBOUND_TIMEOUT_SECONDS": {
        "section": "Authentication & Security",
        "default": "30",
        "description": "Outbound HTTP request timeout in seconds",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "integer",
    },
    "BEETS_OUTBOUND_MAX_REDIRECTS": {
        "section": "Authentication & Security",
        "default": "5",
        "description": "Maximum permitted HTTP redirects for external fetches",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "integer",
    },
    "BEETS_OUTBOUND_MAX_RESPONSE_BYTES": {
        "section": "Authentication & Security",
        "default": "10485760",
        "description": "Maximum response payload size in bytes for outbound requests",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "integer",
    },
    "BEETS_OUTBOUND_ALLOWLIST": {
        "section": "Authentication & Security",
        "default": "",
        "description": "Comma-separated whitelist of allowed external hostnames/IPs",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "string",
    },

    # 3. AI & LLM Services
    "OPENAI_API_KEY": {
        "section": "AI & LLM Services",
        "default": None,
        "description": "OpenAI API key for candidate evaluation and matching",
        "secret": True,
        "editable": True,
        "revealable": True,
        "restart_required": False,
        "type": "secret",
    },
    "OPENROUTER_API_KEY": {
        "section": "AI & LLM Services",
        "default": None,
        "description": "OpenRouter API key for LLM provider routing",
        "secret": True,
        "editable": True,
        "revealable": True,
        "restart_required": False,
        "type": "secret",
    },
    "AI_API_KEY": {
        "section": "AI & LLM Services",
        "default": None,
        "description": "Generic API key for custom OpenAI-compatible endpoints",
        "secret": True,
        "editable": True,
        "revealable": True,
        "restart_required": False,
        "type": "secret",
    },
    "AI_BASE_URL": {
        "section": "AI & LLM Services",
        "default": "https://api.openai.com/v1",
        "description": "Base endpoint URL for AI model requests",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "string",
    },
    "AI_MODEL": {
        "section": "AI & LLM Services",
        "default": "gpt-4o-mini",
        "description": "Active AI model identifier for candidate evaluation",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "string",
    },

    # 4. Beets Core & Engine
    "BEETS_WEB_URL": {
        "section": "Beets Core & Engine",
        "default": "http://beets:8337",
        "description": "Primary read transport: Stock Beets Web API (http://beets:8337)",
        "secret": False,
        "editable": True,
        "restart_required": True,
        "type": "string",
    },
    "BEETS_WEBMANAGER_API_KEY": {
        "section": "Beets Core & Engine",
        "default": None,
        "description": "Integration plugin: Bearer-authenticated /webmanager/* API key",
        "secret": True,
        "editable": True,
        "revealable": True,
        "restart_required": True,
        "type": "secret",
    },
    "BEETS_CONFIG": {
        "section": "Beets Core & Engine",
        "default": "/config/config.yaml",
        "container_path": "/config/config.yaml",
        "description": "Container path to authoritative Beets YAML configuration",
        "secret": False,
        "editable": False,
        "restart_required": True,
        "type": "string",
    },
    "BEETS_LIBRARY": {
        "section": "Beets Core & Engine",
        "default": None,
        "container_path": "/config/musiclibrary.blb",
        "description": "Deprecated: Web Manager never opens the Beets library database. Kept only for reference; it will be removed.",
        "secret": False,
        "editable": False,
        "deprecated": True,
        "restart_required": True,
        "type": "string",
    },
    "BEETS_VERSION_PROBE_TIMEOUT_SECONDS": {
        "section": "Beets Core & Engine",
        "default": "45",
        "description": "Timeout in seconds for remote Beets engine diagnostics probes",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "integer",
    },

    # 5. Storage & Paths
    "MUSIC_PATH": {
        "section": "Storage & Paths",
        "default": None,
        "container_path": "/music",
        "description": "Music library storage directory (mounted into container at /music)",
        "secret": False,
        "editable": False,
        "restart_required": True,
        "type": "string",
    },
    "DOWNLOADS_PATH": {
        "section": "Storage & Paths",
        "default": None,
        "container_path": "/downloads",
        "description": "Incoming downloads directory (mounted into container at /downloads)",
        "secret": False,
        "editable": False,
        "restart_required": True,
        "type": "string",
    },
    "BEETS_CONFIG_PATH": {
        "section": "Storage & Paths",
        "default": None,
        "container_path": "/config",
        "description": "Beets configuration directory (mounted into container at /config)",
        "secret": False,
        "editable": False,
        "restart_required": True,
        "type": "string",
    },
    "WEB_MANAGER_DATA_PATH": {
        "section": "Storage & Paths",
        "default": None,
        "container_path": "/web-manager-data",
        "description": "Web Manager persistent state directory (mounted at /web-manager-data)",
        "secret": False,
        "editable": False,
        "restart_required": True,
        "type": "string",
    },
    "IMPORT_REVIEW_QUARANTINE_DIR": {
        "section": "Storage & Paths",
        "default": "/config/quarantine",
        "container_path": "/config/quarantine",
        "description": "Quarantine storage path for ambiguous or conflicting import files",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "string",
    },
    "MUSIC_FORMAT_QUARANTINE_DIR": {
        "section": "Storage & Paths",
        "default": "/config/music_format_quarantine",
        "container_path": "/config/music_format_quarantine",
        "description": "Quarantine storage path for displaced lower-quality audio formats",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "string",
    },

    # 6. Music Services & Metadata
    "ACOUSTID_API_KEY": {
        "section": "Music Services & Metadata",
        "default": None,
        "description": "AcoustID application API key (acoustid.org/new-application) used for fingerprint lookups",
        "secret": True,
        "editable": True,
        "revealable": True,
        "restart_required": False,
        "type": "secret",
    },
    "ACOUSTID_USER_KEY": {
        "section": "Music Services & Metadata",
        "default": None,
        "description": "AcoustID user API key (your account page) used only for fingerprint submissions",
        "secret": True,
        "editable": True,
        "revealable": True,
        "restart_required": False,
        "type": "secret",
    },
    "DISCOGS_TOKEN": {
        "section": "Music Services & Metadata",
        "default": None,
        "description": "Discogs personal access token for artwork & releases",
        "secret": True,
        "editable": True,
        "revealable": True,
        "restart_required": False,
        "type": "secret",
    },
    "LISTENBRAINZ_TOKEN": {
        "section": "Music Services & Metadata",
        "default": None,
        "description": "ListenBrainz user token for scrobbling and history syncing",
        "secret": True,
        "editable": True,
        "revealable": True,
        "restart_required": False,
        "type": "secret",
    },
    "SPOTIFY_CLIENT_ID": {
        "section": "Music Services & Metadata",
        "default": "",
        "description": "Spotify Developer Application Client ID for playlist queries",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "string",
    },
    "SPOTIFY_CLIENT_SECRET": {
        "section": "Music Services & Metadata",
        "default": None,
        "description": "Spotify Developer Application Client Secret",
        "secret": True,
        "editable": True,
        "revealable": True,
        "restart_required": False,
        "type": "secret",
    },

    # 7. Media Server Integrations
    "PLEX_URL": {
        "section": "Media Server Integrations",
        "default": "http://localhost:32400",
        "description": "Plex Media Server URL (e.g. http://plex:32400)",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "string",
    },
    "PLEX_TOKEN": {
        "section": "Media Server Integrations",
        "default": None,
        "description": "Plex authentication token (X-Plex-Token)",
        "secret": True,
        "editable": True,
        "revealable": True,
        "restart_required": False,
        "type": "secret",
    },
    "PLEX_MUSIC_SECTION": {
        "section": "Media Server Integrations",
        "default": "",
        "description": "Plex Music library section name or section key",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "string",
    },
    "LIDARR_URL": {
        "section": "Media Server Integrations",
        "default": "http://localhost:8686",
        "description": "Lidarr server URL (e.g. http://lidarr:8686)",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "string",
    },
    "LIDARR_API_KEY": {
        "section": "Media Server Integrations",
        "default": None,
        "description": "Lidarr API key for wanted albums and artist sync",
        "secret": True,
        "editable": True,
        "revealable": True,
        "restart_required": False,
        "type": "secret",
    },
    "SLSKD_URL": {
        "section": "Media Server Integrations",
        "default": "http://slskd:5030",
        "description": "Soulseek / slskd daemon URL (e.g. http://slskd:5030)",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "string",
    },
    "SLSKD_API_KEY": {
        "section": "Media Server Integrations",
        "default": None,
        "description": "Soulseek / slskd API key for automated acquisition",
        "secret": True,
        "editable": True,
        "revealable": True,
        "restart_required": False,
        "type": "secret",
    },
    "QBITTORRENT_URL": {
        "section": "Media Server Integrations",
        "default": "http://localhost:8080",
        "description": "qBittorrent WebUI URL for torrent management",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "string",
    },
    "QBITTORRENT_USERNAME": {
        "section": "Media Server Integrations",
        "default": "admin",
        "description": "qBittorrent WebUI authentication username",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "string",
    },
    "QBITTORRENT_PASSWORD": {
        "section": "Media Server Integrations",
        "default": None,
        "description": "qBittorrent WebUI authentication password",
        "secret": True,
        "editable": True,
        "revealable": True,
        "restart_required": False,
        "type": "secret",
    },
    "QBITTORRENT_CATEGORY": {
        "section": "Media Server Integrations",
        "default": "music",
        "description": "qBittorrent download category assigned to music downloads",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "string",
    },

    # 8. Playlists & Download Providers
    "PLAYLIST_AUTO_SYNC": {
        "section": "Playlists & Download Providers",
        "default": "1",
        "description": "Enable automated background playlist acquisition and sync",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "boolean",
    },
    "PLAYLIST_AUTO_SYNC_INTERVAL": {
        "section": "Playlists & Download Providers",
        "default": "300",
        "description": "Background playlist synchronization interval in seconds",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "integer",
    },
    "PLAYLIST_DOWNLOAD_METHODS": {
        "section": "Playlists & Download Providers",
        "default": "slskd,spotiflac,ytdlp,soundcloud",
        "description": "Priority order of acquisition providers for missing tracks",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "string",
    },
    "PLAYLIST_MIN_DOWNLOAD_SECONDS": {
        "section": "Playlists & Download Providers",
        "default": "45",
        "description": "Minimum pacing delay in seconds between download requests",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "integer",
    },
    "YTDLP_JS_RUNTIMES": {
        "section": "Playlists & Download Providers",
        "default": "deno,node,quickjs",
        "description": "JavaScript runtime engines for yt-dlp signature extraction",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "string",
    },
    "SPOTIFLAC_SERVICES": {
        "section": "Playlists & Download Providers",
        "default": "spotify,deezer,tidal,qobuz",
        "description": "Streaming services supported by spotiflac acquisition provider",
        "secret": False,
        "editable": True,
        "restart_required": False,
        "type": "string",
    },
}


def _detect_system_timezone() -> Optional[str]:
    tz = os.environ.get("TZ", "").strip()
    if tz:
        return tz
    for tz_file in (Path("/etc/timezone"), Path("/etc/TZ")):
        try:
            if tz_file.exists():
                content = tz_file.read_text(encoding="utf-8").strip()
                if content:
                    return content
        except Exception:
            pass
    try:
        import datetime
        dt_tz = datetime.datetime.now().astimezone().tzinfo
        if dt_tz:
            tz_name = str(dt_tz)
            if tz_name and tz_name != "None":
                return tz_name
    except Exception:
        pass
    try:
        if time.tzname and time.tzname[0]:
            return time.tzname[0]
    except Exception:
        pass
    return None


def _resolve_setting_item(
    name: str,
    meta: Dict[str, Any],
    persisted: Dict[str, str],
) -> Dict[str, Any]:
    secret = bool(meta.get("secret") or _is_secret_env(name))
    section = meta.get("section") or "General"
    default_val = meta.get("default")
    container_path = meta.get("container_path")
    host_path = meta.get("host_path")
    description = meta.get("description")
    editable = meta.get("editable", True)
    restart_required = bool(meta.get("restart_required", False))
    var_type = meta.get("type") or ("secret" if secret else "string")
    revealable = bool(secret and meta.get("revealable"))

    persisted_val = persisted.get(name, "").strip() if name in persisted else ""
    has_persisted = name in persisted and bool(persisted_val)
    env_val = os.environ.get(name, "").strip()

    # 1. BEETS_WEB_PASSWORD
    if name == "BEETS_WEB_PASSWORD":
        pass_file = _PERSISTED_BROWSER_PASSWORD_FILE
        init_file = _INITIAL_BROWSER_PASSWORD_FILE
        try:
            import sys
            app_m = sys.modules.get("app")
            if app_m and hasattr(app_m, "_PERSISTED_BROWSER_PASSWORD_FILE"):
                pass_file = getattr(app_m, "_PERSISTED_BROWSER_PASSWORD_FILE")
            if app_m and hasattr(app_m, "_INITIAL_BROWSER_PASSWORD_FILE"):
                init_file = getattr(app_m, "_INITIAL_BROWSER_PASSWORD_FILE")
        except Exception:
            pass

        has_stored = bool((pass_file and pass_file.exists() and pass_file.stat().st_size > 0) or
                          (init_file and init_file.exists() and init_file.stat().st_size > 0) or
                          has_persisted)
        configured = bool(env_val or has_stored)
        source = "environment" if env_val else ("persisted" if has_stored else "not_configured")
        is_overridden = bool(env_val and has_stored)

        return {
            "name": name,
            "section": section,
            "secret": True,
            "revealable": False,
            "configured": configured,
            "editable": True,
            "restart_required": False,
            "type": "password",
            "value": "********" if configured else "",
            "effective_value": None,
            "saved_value": "********" if has_stored else None,
            "has_saved_value": has_stored,
            "is_overridden": is_overridden,
            "default": None,
            "container_path": container_path,
            "host_path": host_path,
            "description": description or "Password for browser login (stored securely as a one-way hash)",
            "source": source,
            "status_message": "Stored as a one-way password hash" if configured else "Not configured",
            "has_value": configured,
            "runtime_has_value": bool(env_val),
            "runtime_value": "********" if env_val else "",
        }

    # 2. BEETS_WEB_AUTH_TOKEN
    if name == "BEETS_WEB_AUTH_TOKEN":
        token_file = _GENERATED_AUTH_TOKEN_FILE
        try:
            import sys
            app_m = sys.modules.get("app")
            if app_m and hasattr(app_m, "_GENERATED_AUTH_TOKEN_FILE"):
                token_file = getattr(app_m, "_GENERATED_AUTH_TOKEN_FILE")
        except Exception:
            pass
        has_stored = bool(has_persisted or
                          (token_file and token_file.exists() and token_file.stat().st_size > 0) or
                          (_FALLBACK_AUTH_TOKEN_FILE.exists() and _FALLBACK_AUTH_TOKEN_FILE.stat().st_size > 0))
        configured = bool(env_val or has_stored)
        source = "environment" if env_val else ("persisted" if has_stored else "not_configured")
        is_overridden = bool(env_val and has_stored)

        return {
            "name": name,
            "section": section,
            "secret": True,
            "revealable": True,
            "configured": configured,
            "editable": editable,
            "restart_required": False,
            "type": "secret",
            "value": "********" if configured else "",
            "effective_value": None,
            "saved_value": "********" if has_stored else None,
            "has_saved_value": has_stored,
            "is_overridden": is_overridden,
            "default": default_val,
            "container_path": container_path,
            "host_path": host_path,
            "description": description,
            "source": source,
            "status_message": "Active (Environment)" if source == "environment" else ("Saved" if configured else "Not configured"),
            "has_value": configured,
            "runtime_has_value": bool(env_val),
            "runtime_value": "********" if env_val else "",
        }

    # 3. BEETS_WEB_USERNAME
    if name == "BEETS_WEB_USERNAME":
        user_file = _PERSISTED_BROWSER_USERNAME_FILE
        try:
            import sys
            app_m = sys.modules.get("app")
            if app_m and hasattr(app_m, "_PERSISTED_BROWSER_USERNAME_FILE"):
                user_file = getattr(app_m, "_PERSISTED_BROWSER_USERNAME_FILE")
        except Exception:
            pass
        file_user = ""
        if user_file and user_file.exists():
            try:
                file_user = user_file.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
            except Exception:
                pass
        saved_val = file_user or persisted_val or None
        has_saved = bool(saved_val)

        if env_val:
            eff_val = env_val
            source = "environment"
            is_overridden = bool(saved_val and saved_val != env_val)
        elif saved_val:
            eff_val = saved_val
            source = "persisted"
            is_overridden = False
        else:
            eff_val = "admin"
            source = "default"
            is_overridden = False

        return {
            "name": name,
            "section": section,
            "secret": False,
            "revealable": False,
            "configured": True,
            "editable": editable,
            "restart_required": False,
            "type": "string",
            "value": eff_val,
            "effective_value": eff_val,
            "saved_value": saved_val,
            "has_saved_value": has_saved,
            "is_overridden": is_overridden,
            "default": "admin",
            "container_path": container_path,
            "host_path": host_path,
            "description": description,
            "source": source,
            "status_message": f"Docker environment overrides saved username ({saved_val})" if is_overridden else "Active",
            "has_value": True,
            "runtime_has_value": bool(env_val),
            "runtime_value": env_val or eff_val,
        }

    # 4. PUID / PGID
    if name in ("PUID", "PGID"):
        runtime_detected = None
        if name == "PUID" and hasattr(os, "getuid"):
            try:
                runtime_detected = str(os.getuid())
            except Exception:
                pass
        elif name == "PGID" and hasattr(os, "getgid"):
            try:
                runtime_detected = str(os.getgid())
            except Exception:
                pass

        saved_val = persisted_val or None
        has_saved = bool(saved_val)

        if env_val:
            eff_val = env_val
            source = "environment"
            is_overridden = bool(saved_val and saved_val != env_val)
        elif saved_val:
            eff_val = saved_val
            source = "persisted"
            is_overridden = False
        elif runtime_detected:
            eff_val = runtime_detected
            source = "runtime"
            is_overridden = False
        else:
            eff_val = "1000"
            source = "default"
            is_overridden = False

        return {
            "name": name,
            "section": section,
            "secret": False,
            "revealable": False,
            "configured": True,
            "editable": editable,
            "restart_required": True,
            "type": "string",
            "value": eff_val,
            "effective_value": eff_val,
            "saved_value": saved_val,
            "has_saved_value": has_saved,
            "is_overridden": is_overridden,
            "default": "1000",
            "container_path": container_path,
            "host_path": host_path,
            "description": description,
            "source": source,
            "status_message": f"Docker environment overrides saved value ({saved_val})" if is_overridden else ("Saved — restart required" if source == "persisted" else "Active"),
            "has_value": True,
            "runtime_has_value": bool(env_val),
            "runtime_value": env_val or eff_val,
        }

    # 5. TZ
    if name == "TZ":
        detected_tz = _detect_system_timezone()
        saved_val = persisted_val or None
        has_saved = bool(saved_val)

        if env_val:
            eff_val = env_val
            source = "environment"
            is_overridden = bool(saved_val and saved_val != env_val)
        elif saved_val:
            eff_val = saved_val
            source = "persisted"
            is_overridden = False
        elif detected_tz:
            eff_val = detected_tz
            source = "runtime"
            is_overridden = False
        else:
            eff_val = "UTC"
            source = "default"
            is_overridden = False

        return {
            "name": name,
            "section": section,
            "secret": False,
            "revealable": False,
            "configured": True,
            "editable": editable,
            "restart_required": True,
            "type": "string",
            "value": eff_val,
            "effective_value": eff_val,
            "saved_value": saved_val,
            "has_saved_value": has_saved,
            "is_overridden": is_overridden,
            "default": "UTC",
            "container_path": container_path,
            "host_path": host_path,
            "description": description,
            "source": source,
            "status_message": f"Docker environment overrides saved timezone ({saved_val})" if is_overridden else ("Saved — restart required" if source == "persisted" else "Active"),
            "has_value": True,
            "runtime_has_value": bool(env_val),
            "runtime_value": env_val or eff_val,
        }

    # 6. AI_BASE_URL
    if name == "AI_BASE_URL":
        saved_val = persisted_val or None
        has_saved = bool(saved_val)
        if env_val:
            eff_val = env_val
            source = "environment"
            is_overridden = bool(saved_val and saved_val != env_val)
        elif saved_val:
            eff_val = saved_val
            source = "persisted"
            is_overridden = False
        elif (os.environ.get("OPENROUTER_API_KEY") or persisted.get("OPENROUTER_API_KEY")) and not (os.environ.get("OPENAI_API_KEY") or persisted.get("OPENAI_API_KEY")):
            eff_val = "https://openrouter.ai/api/v1"
            source = "default"
            is_overridden = False
        else:
            eff_val = "https://api.openai.com/v1"
            source = "default"
            is_overridden = False

        return {
            "name": name,
            "section": section,
            "secret": False,
            "revealable": False,
            "configured": True,
            "editable": editable,
            "restart_required": False,
            "type": "string",
            "value": eff_val,
            "effective_value": eff_val,
            "saved_value": saved_val,
            "has_saved_value": has_saved,
            "is_overridden": is_overridden,
            "default": "https://api.openai.com/v1",
            "container_path": container_path,
            "host_path": host_path,
            "description": description,
            "source": source,
            "status_message": f"Docker environment overrides saved value ({saved_val})" if is_overridden else "Active",
            "has_value": True,
            "runtime_has_value": bool(env_val),
            "runtime_value": env_val or eff_val,
        }

    # 7. AI_MODEL
    if name == "AI_MODEL":
        saved_val = persisted_val or None
        has_saved = bool(saved_val)
        if env_val:
            eff_val = env_val
            source = "environment"
            is_overridden = bool(saved_val and saved_val != env_val)
        elif saved_val:
            eff_val = saved_val
            source = "persisted"
            is_overridden = False
        elif (os.environ.get("OPENROUTER_API_KEY") or persisted.get("OPENROUTER_API_KEY")) and not (os.environ.get("OPENAI_API_KEY") or persisted.get("OPENAI_API_KEY")):
            eff_val = "openai/gpt-4o-mini"
            source = "default"
            is_overridden = False
        else:
            eff_val = "gpt-4o-mini"
            source = "default"
            is_overridden = False

        return {
            "name": name,
            "section": section,
            "secret": False,
            "revealable": False,
            "configured": True,
            "editable": editable,
            "restart_required": False,
            "type": "string",
            "value": eff_val,
            "effective_value": eff_val,
            "saved_value": saved_val,
            "has_saved_value": has_saved,
            "is_overridden": is_overridden,
            "default": "gpt-4o-mini",
            "container_path": container_path,
            "host_path": host_path,
            "description": description,
            "source": source,
            "status_message": f"Docker environment overrides saved value ({saved_val})" if is_overridden else "Active",
            "has_value": True,
            "runtime_has_value": bool(env_val),
            "runtime_value": env_val or eff_val,
        }

    # 8. Generic Secret Variable
    if secret:
        has_stored = has_persisted
        if name == "SLSKD_API_KEY":
            slskd_key_file = Path(os.environ.get("SLSKD_API_KEY_FILE", "/config/slskd_api_key"))
            if slskd_key_file.exists() and slskd_key_file.stat().st_size > 0:
                has_stored = True

        configured = bool(env_val or has_stored)
        source = "environment" if env_val else ("persisted" if has_stored else "not_configured")
        is_overridden = bool(env_val and has_stored)
        masked_val = _mask(env_val or persisted_val) if configured else ""

        return {
            "name": name,
            "section": section,
            "secret": True,
            "revealable": revealable,
            "configured": configured,
            "editable": editable,
            "restart_required": restart_required,
            "type": "secret",
            "value": masked_val,
            "effective_value": None,
            "saved_value": "••••••••••••••••" if has_stored else None,
            "has_saved_value": has_stored,
            "is_overridden": is_overridden,
            "default": default_val,
            "container_path": container_path,
            "host_path": host_path,
            "description": description,
            "source": source,
            "status_message": "Docker environment overrides saved key" if is_overridden else ("Configured" if configured else "Not configured"),
            "has_value": configured,
            "runtime_has_value": bool(env_val),
            "runtime_value": _mask(env_val) if env_val else "",
        }

    # 9. Generic Non-Secret Variable
    saved_val = persisted_val or None
    has_saved = bool(saved_val)
    if env_val:
        eff_val = env_val
        source = "environment"
        is_overridden = bool(saved_val and saved_val != env_val)
    elif saved_val:
        eff_val = saved_val
        source = "persisted"
        is_overridden = False
    elif default_val is not None and default_val != "":
        eff_val = str(default_val)
        source = "default"
        is_overridden = False
    else:
        eff_val = ""
        source = "not_configured"
        is_overridden = False

    configured = bool(eff_val)
    return {
        "name": name,
        "section": section,
        "secret": False,
        "revealable": False,
        "configured": configured,
        "editable": editable,
        "restart_required": restart_required,
        "type": var_type,
        "value": eff_val,
        "effective_value": eff_val,
        "saved_value": saved_val,
        "has_saved_value": has_saved,
        "is_overridden": is_overridden,
        "default": default_val,
        "container_path": container_path,
        "host_path": host_path,
        "description": description,
        "source": source,
        "status_message": f"Docker environment overrides saved value ({saved_val})" if is_overridden else ("Saved — restart required" if (source == "persisted" and restart_required) else ("Active" if configured else "Not configured")),
        "has_value": configured,
        "runtime_has_value": bool(env_val),
        "runtime_value": env_val or eff_val,
    }


def _beets_web_url() -> str:
    """BEETS_WEB_URL for status/diagnostics display, userinfo redacted."""
    return config_layers.redact_url_userinfo(config_layers.beets_web_url())


_LAYER_NOTES = {
    config_layers.LAYER_HOST: "Host-side Docker Compose value (bind-mount source). Shown for reference; change it in your Compose .env and recreate the stack.",
    config_layers.LAYER_DEPLOYMENT: "Set in the Compose environment: block. Change it there and recreate the container.",
    config_layers.LAYER_CONTAINER: "Container path; must match the Compose mount. Change it in the Compose environment: block and recreate the container.",
}


def _annotate_layer(key: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    """Attach the configuration layer and how a change applies. Only
    application-layer keys stay editable (backend.config_layers)."""
    layer = config_layers.classify(key)
    if layer in (config_layers.LAYER_APP, config_layers.LAYER_SECRET):
        apply = config_layers.APPLY_RESTART if (entry.get("restart_required") or config_layers.apply_mode(key) == config_layers.APPLY_RESTART) else config_layers.APPLY_LIVE
    elif layer == config_layers.LAYER_UNKNOWN:
        apply = config_layers.APPLY_RESTART
    else:
        apply = config_layers.APPLY_DEPLOY
    entry["layer"] = layer
    entry["apply"] = apply
    entry["restart_required"] = apply != config_layers.APPLY_LIVE
    entry["editable"] = bool(entry.get("editable", True)) and config_layers.is_saveable(key)
    note = _LAYER_NOTES.get(layer)
    if note:
        entry["layer_note"] = note
    return entry


def _env_catalog() -> Dict[str, Dict[str, Any]]:
    entries, values = _parse_env_text(_env_example_text())
    catalog: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        if entry.get("type") != "var":
            continue
        key = str(entry.get("key") or "")
        if key in _BLOCKED_ENV_NAMES:
            continue
        meta = _SETTING_METADATA.get(key, {})
        catalog[key] = {
            "name": key,
            "section": meta.get("section") or entry.get("section") or "General",
            "default": meta.get("default", values.get(key, "")),
            "secret": meta.get("secret", _is_secret_env(key)),
            "container_path": meta.get("container_path"),
            "host_path": meta.get("host_path"),
            "description": meta.get("description"),
            "editable": meta.get("editable", True),
            "restart_required": bool(meta.get("restart_required", False)),
            "type": meta.get("type") or ("secret" if meta.get("secret") else "string"),
            "revealable": bool(meta.get("secret", False) and meta.get("revealable")),
        }
        _annotate_layer(key, catalog[key])
    for key, meta in _SETTING_METADATA.items():
        if key not in catalog and key not in _BLOCKED_ENV_NAMES:
            catalog[key] = {
                "name": key,
                "section": meta.get("section") or "General",
                "default": meta.get("default", ""),
                "secret": meta.get("secret", _is_secret_env(key)),
                "container_path": meta.get("container_path"),
                "host_path": meta.get("host_path"),
                "description": meta.get("description"),
                "editable": meta.get("editable", True),
                "restart_required": bool(meta.get("restart_required", False)),
                "type": meta.get("type") or ("secret" if meta.get("secret") else "string"),
                "revealable": bool(meta.get("secret", False) and meta.get("revealable")),
            }
            _annotate_layer(key, catalog[key])
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
            meta = _SETTING_METADATA.get(key, {})
            catalog[key] = {
                "name": key,
                "section": meta.get("section") or "Custom",
                "default": meta.get("default", ""),
                "secret": meta.get("secret", _is_secret_env(key)),
                "container_path": meta.get("container_path"),
                "host_path": meta.get("host_path"),
                "description": meta.get("description"),
                "editable": meta.get("editable", True),
                "restart_required": bool(meta.get("restart_required", False)),
                "type": meta.get("type") or ("secret" if meta.get("secret") else "string"),
                "revealable": bool(meta.get("secret", False) and meta.get("revealable")),
            }
            _annotate_layer(key, catalog[key])
            names.append(key)
    variables = []
    for name in names:
        meta = catalog[name]
        var_item = _resolve_setting_item(name, meta, persisted)
        for field in ("layer", "apply", "layer_note"):
            if field in meta:
                var_item[field] = meta[field]
        var_item["restart_required"] = bool(meta.get("restart_required"))
        if not meta.get("editable", True):
            var_item["editable"] = False
        variables.append(var_item)
    payload: Dict[str, Any] = {
        "ok": True,
        "env_file": str(_SETUP_ENV_FILE),
        "exists": exists,
        "example_file": str(_ENV_EXAMPLE_FILE),
        "restart_required_after_save": True,
        "variables": variables,
        "migration": config_layers.read_migration_report(_SETUP_ENV_FILE),
    }
    if extra:
        payload.update(extra)
    return payload


def _resolve_secret_effective_value(name: str) -> Optional[str]:
    """Return the actual effective plaintext for a revealable secret setting,
    using the exact same source precedence _resolve_setting_item() already
    uses for that setting's `source`/`configured` fields (non-empty process
    environment > persisted configuration > other supported secret store) --
    never a second, independent resolution path. Returns None when the
    setting is not a secret, is not marked `revealable` in
    _SETTING_METADATA, or has no effective value right now.

    This function is intentionally never called by _setup_env_payload() (or
    anything it calls) -- /api/setup/env must keep returning masked values
    only. It exists solely for the narrowly-scoped POST
    /api/setup/env/<name>/reveal endpoint below.
    """
    meta = _SETTING_METADATA.get(name)
    if not meta or not meta.get("secret") or not meta.get("revealable"):
        return None

    env_val = os.environ.get(name, "").strip()
    if env_val:
        return env_val

    _, persisted, _ = _load_env_file()
    persisted_val = persisted.get(name, "").strip()
    if persisted_val:
        return persisted_val

    if name == "BEETS_WEB_AUTH_TOKEN":
        token_file = _GENERATED_AUTH_TOKEN_FILE
        try:
            import sys
            app_m = sys.modules.get("app")
            if app_m and hasattr(app_m, "_GENERATED_AUTH_TOKEN_FILE"):
                token_file = getattr(app_m, "_GENERATED_AUTH_TOKEN_FILE")
        except Exception:
            pass
        for candidate in (token_file, _FALLBACK_AUTH_TOKEN_FILE):
            try:
                if candidate and candidate.exists():
                    val = candidate.read_text(encoding="utf-8").strip()
                    if val:
                        return val
            except Exception:
                pass
        return None

    if name == "SLSKD_API_KEY":
        slskd_key_file = Path(os.environ.get("SLSKD_API_KEY_FILE", "/config/slskd_api_key"))
        try:
            if slskd_key_file.exists():
                val = slskd_key_file.read_text(encoding="utf-8").strip()
                if val:
                    return val
        except Exception:
            pass
        return None

    return persisted_val or None


def _write_env_file(updates: Dict[str, str], clear: List[str]) -> str:
    catalog = _env_catalog()
    entries, persisted, exists = _load_env_file()
    editable = set(catalog.keys()) | set(persisted.keys()) | set(_SETTING_METADATA.keys())

    def _reject_if_not_editable(key: str) -> None:
        if key in _BLOCKED_ENV_NAMES or not _ENV_NAME_RE.match(key) or key not in editable:
            raise ValueError(f"{key} is not an editable setup environment variable")
        # Only application-layer keys may be saved (backend.config_layers):
        # host-side Compose values, Compose-pinned deployment values,
        # container paths and dead keys can never take effect from here.
        if not config_layers.is_saveable(key):
            layer = config_layers.classify(key)
            raise ValueError(
                f"{key} cannot be changed from this application -- it is a {layer} setting "
                "(set it in Docker Compose and recreate the container)"
            )
        # A name can be a recognized/allowed key (above) while still being
        # explicitly non-editable through this UI/API -- e.g.
        # BEETS_CONFIG_PATH/MUSIC_PATH/DOWNLOADS_PATH/WEB_MANAGER_DATA_PATH
        # exist only as docker-compose.yml's own host-side bind-mount
        # interpolation and are never forwarded into this container, so
        # writing a "new value" for one here could never have any real
        # effect -- reject it instead of silently accepting a no-op write
        # and reporting success.
        if _SETTING_METADATA.get(key, {}).get("editable", True) is False:
            raise ValueError(f"{key} cannot be changed from this application -- it is a host-side deployment setting")

    for key, value in list(updates.items()):
        _reject_if_not_editable(key)
        if "\n" in value or "\r" in value:
            raise ValueError(f"{key} cannot contain newlines")
        if len(value) > 4096:
            raise ValueError(f"{key} is too long")
    if updates.get("BEETS_WEB_PASSWORD"):
        unmet = _password_requirements_unmet(updates["BEETS_WEB_PASSWORD"])
        if unmet:
            raise ValueError("Password does not meet requirements: needs " + ", ".join(unmet) + ".")
    for key in clear:
        _reject_if_not_editable(key)

    # Never seed the saved settings file from the .env.example template:
    # that copied host-side Compose values (DOWNLOADS_PATH=./downloads,
    # BEETS_CONFIG_PATH=./beets, ...) into the app's environment (BI-1).
    # The file holds only keys that were explicitly saved.

    password_update = updates.get("BEETS_WEB_PASSWORD")
    desired = dict(updates)
    if password_update:
        # Browser passwords persist only as Werkzeug hashes in .browser_password.
        # Keep /web-manager-data/.env from becoming a plaintext password store.
        desired["BEETS_WEB_PASSWORD"] = ""
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
        # Created 0600 via O_EXCL (it holds secrets); API callers get the
        # file name only, never the container path.
        backup = config_layers.create_private_file(
            _SETUP_ENV_FILE.with_name(f"{_SETUP_ENV_FILE.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"),
            _SETUP_ENV_FILE.read_text(encoding="utf-8"),
            unique=True,
        )
        backup_path = backup.name
    config_layers.replace_private_file(_SETUP_ENV_FILE, "\n".join(lines).rstrip() + "\n")
    for key, value in desired.items():
        os.environ[key] = value
    try:
        import sys
        app_m = sys.modules.get("app")
        target_pass_file = getattr(app_m, "_PERSISTED_BROWSER_PASSWORD_FILE", _PERSISTED_BROWSER_PASSWORD_FILE) if app_m else _PERSISTED_BROWSER_PASSWORD_FILE
        target_init_file = getattr(app_m, "_INITIAL_BROWSER_PASSWORD_FILE", _INITIAL_BROWSER_PASSWORD_FILE) if app_m else _INITIAL_BROWSER_PASSWORD_FILE
        target_user_file = getattr(app_m, "_PERSISTED_BROWSER_USERNAME_FILE", _PERSISTED_BROWSER_USERNAME_FILE) if app_m else _PERSISTED_BROWSER_USERNAME_FILE
        persist_fn = getattr(app_m, "_persist_file_atomically", None) if app_m else None
        cleanup_fn = getattr(app_m, "_cleanup_initial_browser_password_if_replaced", None) if app_m else None

        if persist_fn is None:
            def persist_fn(target_file: Path, content: str) -> bool:
                try:
                    store, relative_name = _config_store_for_target(target_file)
                    current = store.read_text_record(relative_name)
                    store.save_text(
                        relative_name,
                        content,
                        is_secret=True,
                        expected_revision=current.get("revision"),
                    )
                    return True
                except Exception:
                    return False

        if updates.get("BEETS_WEB_PASSWORD"):
            from werkzeug.security import generate_password_hash
            persist_fn(target_pass_file, generate_password_hash(updates["BEETS_WEB_PASSWORD"]))
        elif "BEETS_WEB_PASSWORD" in clear:
            _remove_config_file(target_pass_file)
            _remove_config_file(target_init_file)

        if updates.get("BEETS_WEB_USERNAME"):
            persist_fn(target_user_file, updates["BEETS_WEB_USERNAME"])
        elif "BEETS_WEB_USERNAME" in clear:
            _remove_config_file(target_user_file)

        if cleanup_fn is not None:
            cleanup_fn()
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


def _redact_plugins_report(plugins_report: Dict[str, Any]) -> Dict[str, Any]:
    """`verify_all_plugins()` surfaces raw plugin-loader exception text (e.g.
    `summary.errors` and per-plugin `errors`/`note`) that can contain the same
    secret-bearing substrings `_redact_diagnostic_text()` scrubs elsewhere.
    This report is embedded verbatim under the top-level `"plugins"` key of
    `/api/setup/status`, a separate path from `diagnostics["plugin_failures"]`,
    so it needs its own redaction pass before being returned to the browser."""
    report = copy.deepcopy(plugins_report or {})
    summary = report.get("summary")
    if isinstance(summary, dict) and isinstance(summary.get("errors"), list):
        summary["errors"] = [_redact_diagnostic_text(str(line)) for line in summary["errors"]]
    plugins = report.get("plugins")
    if isinstance(plugins, list):
        for plugin in plugins:
            if not isinstance(plugin, dict):
                continue
            for field in ("errors", "note"):
                value = plugin.get(field)
                if isinstance(value, list):
                    plugin[field] = [_redact_diagnostic_text(str(line)) for line in value]
                elif isinstance(value, str):
                    plugin[field] = _redact_diagnostic_text(value)
    return report


def _remote_failure_shape(
    error: str,
    *,
    timed_out: bool = False,
    diagnostic_error: str = "",
    remote_error: str = "unavailable",
) -> Dict[str, Any]:
    safe_error = _redact_diagnostic_text(error or "Stock Beets is unavailable.")
    return {
        "available": False,
        "path": _beets_web_url(),
        "version": "",
        "diagnostic_error": _redact_diagnostic_text(diagnostic_error),
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
        "remote_reachable": False,
        "remote_error": remote_error,
        "paths": {},
        "engine_compatibility": {
            "compatible": False,
            "installed": False,
            "state": remote_error,
            "plugin_version": "",
            "protocol_version": "",
            "expected_protocol_version": _EXPECTED_PROTOCOL_VERSION,
            "message": diagnostic_error,
        },
    }


def _local_path_report(path: Path, *, require_writable: bool = False) -> Dict[str, Any]:
    """Report a Web Manager-local mounted path's real state.

    Replaces the legacy control-agent path-report shape, which described
    paths as seen by the (now-deleted) remote control agent. Web Manager
    now owns /config and /downloads directly, so these are real local
    filesystem checks, not a remote proxy."""
    try:
        exists = path.exists()
        is_dir = path.is_dir() if exists else False
        readable = exists and os.access(path, os.R_OK)
        writable = exists and os.access(path, os.W_OK)
    except Exception:
        exists = is_dir = readable = writable = False
    ok = exists and is_dir and readable and (writable if require_writable else True)
    return {
        "path": str(path),
        "exists": exists,
        "is_dir": is_dir,
        "readable": readable,
        "writable": writable,
        "ok": ok,
    }


def _local_file_report(path: Path, *, require_writable: bool = False) -> Dict[str, Any]:
    """File variant of _local_path_report (BI-15): a config FILE is ok when
    it exists, is a regular file and is readable -- never "is_dir"."""
    try:
        exists = path.exists()
        is_file = path.is_file() if exists else False
        readable = exists and os.access(path, os.R_OK)
        writable = exists and os.access(path, os.W_OK)
    except Exception:
        exists = is_file = readable = writable = False
    ok = exists and is_file and readable and (writable if require_writable else True)
    return {
        "path": str(path),
        "exists": exists,
        "is_dir": False,
        "is_file": is_file,
        "readable": readable,
        "writable": writable,
        "ok": ok,
    }


def _web_manager_downloads_root() -> Path:
    """Downloads/staging mount inside THIS container: DOWNLOADS_ROOT (default
    /downloads). Never DOWNLOADS_PATH -- that is the host-side Compose
    bind-mount source and means nothing in here (BI-3)."""
    return Path(config_layers.downloads_root())


def _checked_beets_config_path() -> Tuple[Path, str]:
    """BEETS_CONFIG through the same BEETSDIR containment check that
    provisioning and the config editors use (#222 QA F2), so status and
    provisioning agree. On refusal: the raw path (for local mount checks
    only) and a fixed user-facing message; otherwise an empty message.
    The message is a constant, never str(exc) (SEC-237-1)."""
    from backend.config_manager import CONFIG_PATH_ERROR_MESSAGE, ConfigPathError, get_config_path
    try:
        return get_config_path(), ""
    except ConfigPathError:
        return Path(config_layers.beets_config_file()), CONFIG_PATH_ERROR_MESSAGE


def _local_paths_report(config_path: Path) -> Dict[str, Any]:
    """Web Manager's own mounts, checked locally. Independent of whether
    stock Beets is reachable (BI-7)."""
    music = _local_path_report(_web_manager_music_root())
    music["path"] = config_layers.music_root()
    downloads = _local_path_report(_web_manager_downloads_root(), require_writable=True)
    downloads["path"] = config_layers.downloads_root()
    return {
        "config": _local_path_report(config_path.parent, require_writable=True),
        "music_library": music,
        "downloads": downloads,
        "beets_config": _local_file_report(config_path),
    }


def _web_manager_music_root() -> Path:
    """Where the Beets library is mounted inside THIS (Web Manager) container.

    Same rule as backend.app_runtime.MUSIC_ROOT (env MUSIC_ROOT, default
    /music, the path every shipped compose file mounts), read at call time
    so the setup check never carries its own hard-coded library path
    (#143). It is a Web Manager-local check: Web Manager reads library
    files itself (duplicate scan, fingerprints, artwork), so its own mount
    is what has to be readable. Beets' own `directory` lives in the other
    container and is not a path this process can test."""
    return Path(config_layers.music_root())


def _parse_replaygain_settings(config_text: str) -> Tuple[str, str]:
    """Best-effort local parse of config.yaml's replaygain: backend/command.

    Web Manager cannot inspect binaries inside the stock Beets container
    (ffmpeg/bs1770gain/etc.), so this reports what's *configured*, not
    whether the backend binary is actually present there."""
    backend = ""
    command = ""
    m = re.search(r"^replaygain:\s*$", config_text, re.MULTILINE)
    if not m:
        return backend, command
    block = config_text[m.end():]
    backend_m = re.search(r"^\s+backend:\s*(\S+)", block, re.MULTILINE)
    if backend_m:
        backend = backend_m.group(1).strip().strip("'\"")
    command_m = re.search(r"^\s+command:\s*(\S+)", block, re.MULTILINE)
    if command_m:
        command = command_m.group(1).strip().strip("'\"")
    return backend, command


def _beets_plugin_diagnostics(config_path: Path) -> Dict[str, Any]:
    """Return Beets plugin diagnostics from stock Beets (via BeetsAdapter),
    never from a local `beet` executable or the deleted control agent.

    The returned shape preserves the public setup-status fields the
    frontend already consumes, but every value now comes from either the
    webmanager integration plugin's live handshake or a real local
    filesystem check of Web Manager's own /config and /downloads mounts.
    Any connection or handshake failure fails closed: plugin_loader_ok is
    false and no configured/loaded plugin is trusted.
    """
    from backend.beets_adapter import beets_adapter, BeetsAdapterAuthError, BeetsAdapterTimeoutError

    def _remote_beets_diagnostics_failure(*args, **kwargs) -> Dict[str, Any]:
        failure = _remote_failure_shape(*args, **kwargs)
        # Local mounts are still checked for real; fpcalc lives in the Beets
        # container, so while Beets is unreachable it is "unknown", not
        # "missing" (BI-7).
        failure["paths"] = _local_paths_report(config_path)
        failure["fpcalc_state"] = "unknown"
        return failure

    try:
        plugin_status = beets_adapter.get_plugin_status()
    except BeetsAdapterAuthError:
        return _remote_beets_diagnostics_failure(
            "Stock Beets integration plugin authentication failed.",
            diagnostic_error="Stock Beets integration plugin authentication failed.",
            remote_error="authentication_failed",
        )
    except BeetsAdapterTimeoutError:
        return _remote_beets_diagnostics_failure(
            "Stock Beets integration plugin status request timed out.",
            timed_out=True,
            diagnostic_error="Stock Beets integration plugin status request timed out.",
            remote_error="timeout",
        )
    except Exception:
        return _remote_beets_diagnostics_failure(
            "Stock Beets is unavailable.",
            diagnostic_error="Stock Beets is unavailable.",
            remote_error="unavailable",
        )

    if not isinstance(plugin_status, dict) or not plugin_status.get("protocol_version"):
        return _remote_beets_diagnostics_failure(
            "Malformed stock Beets integration plugin status response.",
            diagnostic_error="Malformed stock Beets integration plugin status response.",
            remote_error="malformed_response",
        )

    try:
        stats = beets_adapter.get_stats()
        beets_version = str(plugin_status.get("beets_version") or "")
    except Exception:
        stats = {}
        beets_version = str(plugin_status.get("beets_version") or "")

    loaded_plugins = list(dict.fromkeys(str(p) for p in (plugin_status.get("loaded_plugins") or []) if str(p).strip()))

    try:
        from backend.beets_plugins import verify_all_plugins
        plugins_report = verify_all_plugins(
            config_path.parent, config_file=config_path, loaded_plugins=loaded_plugins
        )
        configured_plugins = sorted({
            p["name"] for p in plugins_report.get("plugins", []) if p.get("enabled")
        })
        plugin_failures = plugins_report.get("summary", {}).get("errors", [])
        plugin_loader_ok = bool(plugins_report.get("all_required_healthy", True))
    except Exception as ex:
        app.logger.warning("Plugin verification failed in diagnostics: %s", ex)
        configured_plugins = []
        plugin_failures = []
        plugin_loader_ok = True

    loader_error = ""
    if not plugin_loader_ok and plugin_failures:
        loader_error = "; ".join(plugin_failures[:3])
    elif not plugin_loader_ok:
        loader_error = "One or more required Beets plugins are not healthy."

    config_text = ""
    try:
        if config_path.exists():
            config_text = config_path.read_text(encoding="utf-8")
    except Exception:
        pass
    replaygain_backend, replaygain_command = _parse_replaygain_settings(config_text)

    capabilities = set(plugin_status.get("capabilities") or [])
    # mbsubmit capability implies the chroma plugin (and therefore
    # pyacoustid/fpcalc) is available inside the stock Beets container --
    # Web Manager has no local binary of its own to check.
    mbsubmit_available = "mbsubmit" in capabilities

    # Fingerprinting needs the chroma plugin loaded (#143: no chroma ->
    # blocking, unchanged). Plugin >= 1.6.0 additionally probes fpcalc on
    # PATH inside the Beets container (BI-14), so a loaded chroma with no
    # binary is now also reported as missing. Older plugins cannot probe,
    # so for them the capability inference alone decides, as before.
    binaries_reported = "fpcalc_available" in plugin_status
    fpcalc_available = mbsubmit_available
    if binaries_reported:
        fpcalc_available = mbsubmit_available and bool(plugin_status.get("fpcalc_available"))
    fpcalc_state = "available" if fpcalc_available else "missing"
    if "ffmpeg_available" in plugin_status:
        ffmpeg_available: Any = bool(plugin_status.get("ffmpeg_available"))
    else:
        ffmpeg_available = bool(replaygain_backend == "ffmpeg")

    return {
        "available": True,
        "path": _beets_web_url(),
        "version": beets_version,
        "diagnostic_error": "",
        "plugin_loader_ok": plugin_loader_ok,
        "plugin_loader_timed_out": False,
        "plugin_loader_error": _redact_diagnostic_text(loader_error),
        "diagnostics_fresh": True,
        "diagnostics_cache_age_seconds": 0,
        "diagnostics_refresh_error": "",
        "configured_plugins": configured_plugins,
        "loaded_plugins": loaded_plugins,
        "installed_plugins": {},
        "pluginpath": ["/config/beetsplug"],
        "plugin_failures": [_redact_diagnostic_text(str(line)) for line in (plugin_failures or [])][:12],
        "replaygain_backend": replaygain_backend,
        "replaygain_command": replaygain_command,
        "discogs_token_configured": bool(os.environ.get("DISCOGS_TOKEN") or os.environ.get("DISCOGS_USER_TOKEN")),
        "listenbrainz_token_configured": bool(os.environ.get("LISTENBRAINZ_TOKEN")),
        "fpcalc_available": fpcalc_available,
        "fpcalc_path": "",
        "ffmpeg_available": ffmpeg_available,
        "ffmpeg_path": "",
        "pyacoustid_available": mbsubmit_available,
        "binaries_reported_by_plugin": binaries_reported,
        "beets_library": _plugin_library_report(plugin_status),
        "capabilities": {
            "acoustid_lookup": {
                "fpcalc_available": fpcalc_available,
                "chroma_loaded": mbsubmit_available,
                "pyacoustid_available": mbsubmit_available,
            },
        },
        "remote_reachable": True,
        "remote_error": "",
        "paths": _local_paths_report(config_path),
        "fpcalc_state": fpcalc_state,
        "engine_compatibility": _check_integration_plugin_compatibility(plugin_status),
    }


_EXPECTED_PROTOCOL_VERSION = "1.0"

_UNKNOWN = "unknown"


def _plugin_library_report(plugin_status: Dict[str, Any]) -> Dict[str, Any]:
    """Beets' own view of its library and roots, as reported by the
    webmanager plugin (>= 1.6.0). Beets stays authoritative: Web Manager
    reports and compares these, never rewrites them. Fields an older
    plugin does not send are the string "unknown"."""

    def _field(name: str) -> Any:
        if name not in plugin_status:
            return _UNKNOWN
        return plugin_status.get(name)

    return {
        "library_directory": _field("library_directory"),
        "library_path": _field("library_path"),
        "allowed_roots": _field("allowed_roots"),
        "import_roots": _field("import_roots"),
        "web_include_paths": _field("web_include_paths"),
    }


def _bundled_plugin_version() -> str:
    """Version of the webmanager plugin shipped in THIS Web Manager image
    (what provisioning copies into /config/beetsplug).

    Loaded by file path: importing ``beetsplug.webmanager.version`` would run
    the package ``__init__``, which needs ``beets`` -- not installed in the
    Web Manager image."""
    try:
        from backend.beets_plugins import SOURCE_BEETSPLUG_DIR
        spec = importlib.util.spec_from_file_location(
            "_bwm_bundled_webmanager_version",
            SOURCE_BEETSPLUG_DIR / "webmanager" / "version.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return str(module.PLUGIN_VERSION)
    except Exception:
        return ""


def _check_integration_plugin_compatibility(plugin_status: Dict[str, Any]) -> Dict[str, Any]:
    """Compare the running webmanager integration plugin's handshake against
    what this Web Manager version expects. Replaces the legacy control-agent
    "engine_release"/"control_api_version" compatibility concept -- the
    only thing that can now be incompatible is the integration PLUGIN
    protocol, never a separate "engine" release."""
    protocol_version = str(plugin_status.get("protocol_version") or "").strip()
    plugin_version = str(plugin_status.get("plugin_version") or "").strip()

    installed = bool(protocol_version)
    compatible = installed and protocol_version == _EXPECTED_PROTOCOL_VERSION
    message = ""
    if not installed:
        state = "not_installed"
        message = "The webmanager integration plugin is not reachable on stock Beets."
    elif not compatible:
        state = "incompatible"
        message = (
            f"Integration plugin protocol version ({protocol_version}) does not match "
            f"the version Web Manager expects ({_EXPECTED_PROTOCOL_VERSION}). "
            "Restart stock Beets after upgrading Web Manager."
        )
    else:
        state = "compatible"

    # BI-4: same protocol, but Beets is still running an older/newer copy of
    # the plugin than the one this image provisioned (provisioning updates
    # /config/beetsplug, Beets only loads plugins at start-up). Still
    # compatible -- never blocking -- but the new diagnostics fields stay
    # "unknown" until stock Beets is restarted.
    bundled_version = _bundled_plugin_version()
    restart_required = bool(
        compatible and bundled_version and plugin_version and plugin_version != bundled_version
    )
    if restart_required:
        state = "restart_required"
        message = (
            f"Stock Beets is running webmanager plugin {plugin_version}, but this Web Manager "
            f"provisioned {bundled_version}. Restart the Beets container to load it."
        )

    return {
        "compatible": compatible,
        "installed": installed,
        "state": state,
        "plugin_version": plugin_version,
        "bundled_plugin_version": bundled_version,
        "restart_required": restart_required,
        "protocol_version": protocol_version,
        "expected_protocol_version": _EXPECTED_PROTOCOL_VERSION,
        "message": message,
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


def _musicbrainz_integration_status(diagnostics: Dict[str, Any]) -> Dict[str, Any]:
    """MusicBrainz is core Beets metadata capability, not an optional plugin.

    Beets has no "musicbrainz" entry in its `plugins:` enable list --
    release/recording autotagging against MusicBrainz is always active
    whenever the engine itself is reachable and its plugin loader completed,
    configurable only via the `musicbrainz:` config *section* (host,
    mirror, rate limit), never via plugin enable/disable. Routing this
    through the same configured_plugins/loaded_plugins membership check used
    for real optional plugins (fetchart, discogs, mbsync, ...) made
    MusicBrainz permanently report "installed but disabled" in real
    deployments, since no plugin by that name ever appears to be enabled --
    exactly the "MusicBrainz is just another plugin row" bug this avoids.
    Reports control-agent/plugin-loader health only, never plugin
    membership, and never depends on any AI provider credential.
    """
    if not diagnostics.get("remote_reachable"):
        return _integration_status(
            configured=False, required=True, state="unavailable",
            note="Beets control agent is unreachable; MusicBrainz status cannot be confirmed.",
        )
    if not diagnostics.get("plugin_loader_ok"):
        return _loader_failed_status(diagnostics, required=True)
    return _integration_status(
        configured=True, required=True, state="connected",
        note="Core Beets metadata source; always available, no user API key required.",
    )


# Integration keys that represent an external/network service (MusicBrainz,
# AcoustID, AI, Plex, ...) as opposed to a togglable Beets plugin
# (fetchart, embedart, scrub, mbsync, ...). Used only to tag each entry's
# "category" in the setup-status response so the frontend can render
# "External Services" and "Beets Capabilities / Plugins" as visually
# distinct groups instead of one flat, undifferentiated list -- see
# docs/CONFIGURATION.md's "MusicBrainz is not a plugin" note.
_SERVICE_INTEGRATION_KEYS = {"musicbrainz", "acoustid", "ai", "plex", "lidarr", "slskd"}


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


def _acoustid_integration_status(diagnostics: Dict[str, Any], fpcalc_available: bool) -> Dict[str, Any]:
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
    if not fpcalc_available:
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
    from helpers_mb import acoustid_api_key
    if not acoustid_api_key():
        # IA-12: no built-in fallback key -- lookups need the operator's key.
        return _integration_status(
            configured=False,
            state="not_configured",
            note="AcoustID not configured: set ACOUSTID_API_KEY (an application key from acoustid.org/new-application).",
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
    # Consume remote engine capability if available
    capabilities = diagnostics.get("capabilities")
    rg_cap = capabilities.get("replaygain") if isinstance(capabilities, dict) else None

    if isinstance(rg_cap, dict) and rg_cap.get("available"):
        note_text = "ReplayGain is loaded and available."
        if replaygain_backend == "command":
            cmd_info = f" ({replaygain_command})" if replaygain_command else ""
            note_text = f"ReplayGain uses command backend{cmd_info}."
        elif replaygain_backend == "ffmpeg":
            note_text = "ReplayGain uses the installed ffmpeg backend."
        elif replaygain_backend:
            note_text = f"ReplayGain uses the {replaygain_backend} backend."

        return _integration_status(
            configured=True,
            state="configured",
            note=note_text,
        )

    if replaygain_backend == "ffmpeg" and (ffmpeg_path or diagnostics.get("ffmpeg_available")) and not replaygain_command:
        return _integration_status(
            configured=True,
            state="configured",
            note="ReplayGain uses the installed ffmpeg backend.",
        )

    if replaygain_backend == "command" or replaygain_command:
        return _integration_status(
            configured=True,
            state="configured",
            note=f"ReplayGain uses command backend ({replaygain_command or 'configured'}).",
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


_INCLUDE_PATHS_ACTION_ENDPOINT = "/api/setup/beets-config/include-paths"


def _norm_container_path(value: str) -> str:
    """Normalise a POSIX container path for comparison (no filesystem access)."""
    value = str(value or "").strip().replace("\\", "/")
    if not value:
        return ""
    return posixpath.normpath(value)


def _path_is_within(child: str, parent: str) -> bool:
    child_n = _norm_container_path(child)
    parent_n = _norm_container_path(parent)
    if not child_n or not parent_n:
        return False
    if parent_n == "/":
        return True
    return child_n == parent_n or child_n.startswith(parent_n.rstrip("/") + "/")


def _beets_setup_warnings(
    diagnostics: Dict[str, Any],
    music_path: str,
    downloads_path: str,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Non-blocking Beets configuration warnings plus their fix actions.

    Every check is skipped when the running plugin cannot report the field
    ("unknown" from plugins older than 1.6.0), so an old plugin never
    produces a false warning.
    """
    warnings: List[Dict[str, Any]] = []
    actions: List[Dict[str, Any]] = []

    compat = diagnostics.get("engine_compatibility") if isinstance(diagnostics.get("engine_compatibility"), dict) else {}
    if compat.get("restart_required"):
        warnings.append({
            "id": "beets_restart_required",
            "severity": "warning",
            "message": str(compat.get("message") or "Restart the Beets container to load the provisioned webmanager plugin."),
        })

    library = diagnostics.get("beets_library") if isinstance(diagnostics.get("beets_library"), dict) else {}

    if library.get("web_include_paths") is False:
        warnings.append({
            "id": "beets_web_include_paths_disabled",
            "severity": "warning",
            "message": (
                "Beets web.include_paths is disabled, so Beets does not return file paths. "
                "Path-based features (untracked-folder scan, sync of deleted files, move cleanup) "
                "will refuse to run until it is enabled and the Beets container is restarted."
            ),
            "action": "enable_web_include_paths",
        })
        actions.append({
            "id": "enable_web_include_paths",
            "label": "Enable web.include_paths in the Beets config",
            "method": "POST",
            "endpoint": _INCLUDE_PATHS_ACTION_ENDPOINT,
            "restart_required": True,
        })

    library_directory = library.get("library_directory")
    if isinstance(library_directory, str) and library_directory and library_directory != _UNKNOWN and music_path:
        if _norm_container_path(library_directory) != _norm_container_path(music_path):
            warnings.append({
                "id": "music_root_mismatch",
                "severity": "warning",
                "message": (
                    f"Beets reports its library directory as {library_directory}, but Web Manager's "
                    f"MUSIC_ROOT is {music_path}. Both containers must see the library at the same "
                    "container path; set MUSIC_ROOT or the Beets 'directory' so they match."
                ),
            })

    import_roots = library.get("import_roots")
    if isinstance(import_roots, list) and import_roots and downloads_path:
        if not any(_path_is_within(downloads_path, str(root)) for root in import_roots):
            warnings.append({
                "id": "downloads_root_not_import_root",
                "severity": "warning",
                "message": (
                    f"Web Manager's DOWNLOADS_ROOT {downloads_path} is not under any Beets import root "
                    f"({', '.join(str(r) for r in import_roots)}). Imports from it will be rejected by "
                    "the webmanager plugin; mount downloads at the same path in both containers or add "
                    "it to webmanager.import_roots."
                ),
            })

    return warnings, actions


def _build_setup_status_payload() -> Dict[str, Any]:
    settings = _load_settings()

    beets_config_path, config_path_error = _checked_beets_config_path()
    diagnostics = _beets_plugin_diagnostics(beets_config_path)
    remote_paths = diagnostics.get("paths") if isinstance(diagnostics.get("paths"), dict) else {}

    # Every path check is a local check of THIS container's mounts and is
    # computed whether or not stock Beets answered (BI-7).
    local_paths = remote_paths if remote_paths else _local_paths_report(beets_config_path)
    config_check = dict(local_paths["config"])
    music_check = dict(local_paths["music_library"])
    downloads_check = dict(local_paths["downloads"])
    config_file_report = dict(local_paths["beets_config"])
    if config_path_error:
        # Provisioning and the config editors refuse this path: not usable.
        config_file_report["exists"] = False
        config_file_report["error"] = config_path_error
    beets_config_exists = bool(config_file_report.get("exists"))
    beets_config_report_path = str(config_file_report.get("path") or beets_config_path)
    beets_reachable = bool(diagnostics.get("available"))

    # Stock Beets reports fpcalc availability (chroma/mbsubmit loaded) but
    # never a binary path -- the binary lives in the other container. Gate
    # on availability, not on a path that is always empty.
    fpcalc_available = bool(diagnostics.get("fpcalc_available"))
    fpcalc_state = str(diagnostics.get("fpcalc_state") or ("available" if fpcalc_available else "missing"))
    fpcalc_path = str(diagnostics.get("fpcalc_path") or "") if fpcalc_available else ""
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
        "musicbrainz": _musicbrainz_integration_status(diagnostics),
        "acoustid": _acoustid_integration_status(diagnostics, fpcalc_available),
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
    for key, entry in integrations.items():
        if isinstance(entry, dict):
            entry["category"] = "service" if key in _SERVICE_INTEGRATION_KEYS else "beets_plugin"

    blocking = []
    if not beets_reachable:
        # One primary reason while stock Beets is unreachable; everything
        # that depends on it (plugins, fpcalc, compatibility) is unknown,
        # not failed.
        remote_error = str(diagnostics.get("remote_error") or "unavailable")
        blocking.append(
            f"Stock Beets is unavailable ({remote_error}) at {_beets_web_url()} - check that the beets "
            "container is running and that BEETS_WEB_URL points to it"
        )
    if not config_check["writable"]:
        blocking.append(f"Cannot write to config directory {config_check['path']}")
    if not music_check["readable"]:
        blocking.append(
            f"Music library path {music_check['path']} is not accessible - mount the library there "
            "or set MUSIC_ROOT to where it is mounted in the Web Manager container"
        )
    if not downloads_check["writable"]:
        blocking.append(
            f"Cannot write to downloads/staging path {downloads_check['path']} - mount your downloads "
            "folder there or set DOWNLOADS_ROOT to where it is mounted in the Web Manager container"
        )
    if config_path_error:
        blocking.append(config_path_error)
    elif not beets_config_exists:
        blocking.append(
            f"Beets config not found at {beets_config_report_path} - copy config.yaml.example to config.yaml"
        )
    if beets_reachable:
        # Only a "missing" state blocks; "unknown" (Beets unreachable) never does.
        if fpcalc_state == "missing":
            blocking.append("fpcalc (chromaprint) not found on PATH — AcoustID fingerprinting will not work")
        if beets_config_exists and not diagnostics.get("plugin_loader_ok"):
            blocking.append(
                "Beets plugin loader did not complete successfully — see beets.plugin_loader_error for details."
            )
        engine_compat = diagnostics.get("engine_compatibility") if isinstance(diagnostics.get("engine_compatibility"), dict) else {}
        if engine_compat and not engine_compat.get("compatible", True):
            compat_msg = engine_compat.get("message") or "Beets engine compatibility mismatch."
            blocking.append(f"Beets engine compatibility mismatch: {compat_msg}")

    warnings, actions = (
        _beets_setup_warnings(diagnostics, str(music_check.get("path") or ""), str(downloads_check.get("path") or ""))
        if beets_reachable
        else ([], [])
    )
    if config_path_error:
        warnings.insert(0, {
            "id": "beets_config_path_invalid",
            "severity": "warning",
            "message": config_path_error,
        })

    ready = not blocking
    demo_mode = os.environ.get("DEMO_MODE", "0").strip().lower() in ("1", "true", "yes", "on")
    try:
        from app import (
            _auth_secret_is_usable,
            _browser_password_is_usable,
            _security_auth_token,
            _security_auth_password,
            _security_auth_username,
            _first_run_setup_required,
            _GENERATED_AUTH_TOKEN_FILE,
            _INITIAL_BROWSER_PASSWORD_FILE,
        )
        token_configured = _auth_secret_is_usable(_security_auth_token())
        password_configured = _browser_password_is_usable(_security_auth_password())
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

    try:
        from backend.beets_plugins import verify_all_plugins
        plugins_report = verify_all_plugins(
            beets_config_path.parent,
            config_file=beets_config_path,
            loaded_plugins=diagnostics.get("loaded_plugins"),
            available_binaries={
                "fpcalc": bool(diagnostics.get("fpcalc_available")),
                "ffmpeg": bool(diagnostics.get("ffmpeg_available")),
            },
        )
    except Exception as ex:
        app.logger.warning("Plugin verification failed in status payload: %s", ex)
        plugins_report = {
            "ok": True,
            "all_required_healthy": True,
            "required_count": 0,
            "required_healthy_count": 0,
            "plugins": [],
            "categories": {"required": [], "optional": [], "integration": []},
            "summary": {"total": 0, "healthy": 0, "errors": []},
        }

    redacted_plugins_report = _redact_plugins_report(plugins_report)

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
        "warnings": warnings,
        "actions": actions,
        "paths": {
            "config": config_check,
            "music_library": music_check,
            "downloads": downloads_check,
            "beets_config": config_file_report,
        },
        "fpcalc": {"available": fpcalc_available, "path": fpcalc_path, "state": fpcalc_state},
        "settings_migration": config_layers.read_migration_report(_SETUP_ENV_FILE),
        "beets": diagnostics,
        "plugins": redacted_plugins_report,
        "plugins_ready": bool(plugins_report.get("all_required_healthy", False)),
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
            # SEC-002 CodeQL repository-wide closure finding:
            # _build_setup_status_payload() aggregates config/plugin/path/
            # integration diagnostics from many sources (file reads, beets
            # plugin probes, provider connectivity checks) -- a genuinely
            # broad except whose exception text must never reach the client
            # unsanitized, matching the established pattern already applied
            # elsewhere in this file (setup_save_env/setup_test_ai/etc.,
            # SEC-002 Wave 2).
            app.logger.warning("setup_status rebuild failed: %s: %s", type(ex).__name__, ex)
            has_prior = _STATUS_CACHE_DATA is not None
            age = (now - _STATUS_CACHE_TS) if has_prior else None
            if has_prior and age < _STATUS_CACHE_MAX_STALE_SECONDS:
                res = dict(_STATUS_CACHE_DATA)
                res["cached"] = True
                res["stale"] = True
                res["cache_age_seconds"] = round(age, 2)
                res["refresh_error"] = "Could not refresh setup status."
                return jsonify(res)
            return jsonify({"error": "Could not build setup status.", "status": "failed"}), 503

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
        # SEC-002 CodeQL repository-wide closure finding: same broad-except
        # sanitization as setup_status() above.
        app.logger.warning("setup_diagnostics rebuild failed: %s: %s", type(ex).__name__, ex)
        return jsonify({"error": "Could not build setup diagnostics.", "status": "failed"}), 503
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


_REVEAL_AUTH_WINDOW_SECONDS = 300  # 5 minutes


def _reveal_reauth_applicable() -> bool:
    """Whether an extra password-confirmation gate applies to secret reveal
    at all for this install. Only real when a usable browser password
    exists -- a token/headless-only install (no browser credentials
    configured) has nothing extra to confirm, so it is never asked for one.
    Every reveal request still requires the normal authenticated admin
    session/bearer token already enforced for this whole route family; this
    only adds a second factor for the browser-password auth mode, where a
    stolen/left-open session cookie alone would otherwise be enough."""
    try:
        from app import _browser_password_is_usable, _security_auth_password
    except ImportError:
        return False
    try:
        return bool(_browser_password_is_usable(_security_auth_password()))
    except Exception:
        return False


def _reveal_authorized() -> bool:
    if not _reveal_reauth_applicable():
        return True
    try:
        from flask import session
        until = session.get("reveal_authorized_until")
        return bool(until and float(until) > time.time())
    except Exception:
        return False


@app.post("/api/setup/env/reveal-auth")
def setup_env_reveal_auth():
    """Confirm the current administrator password before allowing secret
    reveal, and open a short reveal-authorization window on this session so
    the operator is not re-prompted for every individual field. Only
    applies when this install actually has a browser password configured
    (see _reveal_reauth_applicable) -- never asked on a token/headless-only
    install, matching the application's existing auth modes."""
    csrf_failure = _setup_csrf_failure()
    if csrf_failure is not None:
        return csrf_failure

    if not _reveal_reauth_applicable():
        # _reveal_authorized() never consults the session in this mode
        # (token/headless install, no browser password to confirm) -- no
        # session write needed here either.
        return jsonify({"ok": True, "authorized": True})

    try:
        from app import _verify_password, _auth_failure_rate_limit_response
    except ImportError:
        return jsonify({"ok": False, "error": "Reauthentication is not available."}), 503

    payload = request.get_json(silent=True) or {}
    supplied = str(payload.get("password") or "")
    if not _verify_password(supplied):
        try:
            limited = _auth_failure_rate_limit_response()
            if limited is not None:
                return limited
        except Exception:
            pass
        return jsonify({"ok": False, "error": "Incorrect password."}), 401

    from flask import session
    session["reveal_authorized_until"] = time.time() + _REVEAL_AUTH_WINDOW_SECONDS
    return jsonify({"ok": True, "authorized": True})


@app.post("/api/setup/env/<name>/reveal")
def setup_env_reveal(name: str):
    """Return the single, effective plaintext value for one revealable
    secret setting. Deliberately the ONLY place in this application that
    returns secret plaintext through the setup API -- /api/setup/env (GET
    and POST) always stays masked, matching every other secret field (see
    _resolve_secret_effective_value's own docstring). Requires the normal
    authenticated admin session, enforced in production by app.py's global
    before_request security boundary (this route is not in the public
    endpoint allowlist) plus, when this install has a browser password, a
    short-lived reveal authorization from POST .../reveal-auth above.
    """
    csrf_failure = _setup_csrf_failure()
    if csrf_failure is not None:
        return csrf_failure

    name = str(name or "").strip()
    meta = _SETTING_METADATA.get(name)
    if meta is None or not meta.get("secret"):
        return jsonify({"ok": False, "error": "Not a recognized secret setting."}), 404
    if not meta.get("revealable"):
        return jsonify({"ok": False, "error": "This value cannot be revealed."}), 403

    if not _reveal_authorized():
        return jsonify({
            "ok": False,
            "error": "Password confirmation required.",
            "reauth_required": True,
        }), 401

    try:
        value = _resolve_secret_effective_value(name)
    except Exception as ex:
        app.logger.warning("Secret reveal failed for %s: %s", name, type(ex).__name__)
        return jsonify({"ok": False, "error": "Could not retrieve the value."}), 500

    if value is None:
        return jsonify({"ok": False, "error": "Not configured.", "configured": False}), 404

    response = jsonify({"ok": True, "name": name, "value": value, "configured": True})
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


@app.post("/api/setup/test/ai")
def setup_test_ai():
    """Live connectivity test against the configured (or posted) AI provider."""
    csrf_failure = _setup_csrf_failure()
    if csrf_failure is not None:
        return csrf_failure

    payload = request.get_json(silent=True) or {}
    from backend.auth_service import probe_may_use_stored_secret
    configured_base_url = os.environ.get("AI_BASE_URL") or "https://api.openai.com/v1"
    supplied_base_url = str(payload.get("base_url") or "").strip()
    base_url = supplied_base_url or configured_base_url
    model = payload.get("model") or os.environ.get("AI_MODEL") or "gpt-4o-mini"
    api_key = str(payload.get("api_key") or "").strip()
    if not api_key and probe_may_use_stored_secret(supplied_base_url, configured_base_url):
        # SEC-1: a stored key is only ever sent to the operator-configured
        # endpoint, and only for an authenticated caller -- never to a
        # caller-supplied base_url, never during anonymous first-run setup.
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("AI_API_KEY") or ""
    if not api_key:
        return jsonify({"ok": False, "status": "not_configured",
                         "error": "No AI API key to test. Enter the API key for this base URL, or set OPENAI_API_KEY (or your provider's key) and retry."}), 200
    base_host = (urllib.parse.urlsplit(base_url).hostname or "").lower()
    is_openai_host = base_host == "api.openai.com" or base_host.endswith(".api.openai.com")
    try:
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/models/{model}" if is_openai_host else f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with provider_boundary.opened("ai", req, timeout=10, max_attempts=1) as r:
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
    csrf_failure = _setup_csrf_failure()
    if csrf_failure is not None:
        return csrf_failure

    try:
        req = urllib.request.Request(
            "https://musicbrainz.org/ws/2/release/?query=release:test&limit=1&fmt=json",
            headers={"User-Agent": "BeetsWebManager/1.0 (+https://github.com/)"},
        )
        with provider_boundary.opened("musicbrainz", req, timeout=10, max_attempts=1) as r:
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


# AcoustID web-service error codes (acoustid-server acoustid/api/errors.py).
# The lookup handler validates the client key (4) before it decodes the
# fingerprint (3) or duration (8), so 3/8 prove the key was accepted.
_ACOUSTID_PROBE_KEY_ACCEPTED_CODES = frozenset({3, 8})   # invalid fingerprint / invalid duration
_ACOUSTID_PROBE_KEY_REJECTED_CODES = frozenset({4})      # invalid API key
_ACOUSTID_PROBE_UNAVAILABLE_CODES = frozenset({5, 13})   # internal error / service unavailable
_ACOUSTID_PROBE_RATE_LIMITED_CODES = frozenset({14})     # too many requests


def _acoustid_error_code(data: Any) -> Optional[int]:
    """The numeric ``error.code`` of an AcoustID JSON body, or None."""
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, bool):
        return None
    if isinstance(code, int):
        return code
    if isinstance(code, str) and code.strip().isdigit():
        return int(code.strip())
    return None


def _classify_acoustid_probe(http_status: Optional[int], data: Any) -> Dict[str, Any]:
    """Classify an AcoustID lookup answer by error code, then HTTP status.

    Never echoes provider text: every message is a fixed string here."""
    if isinstance(data, dict) and data.get("status") == "ok":
        return {"ok": True, "status": "ready"}
    code = _acoustid_error_code(data)
    if code in _ACOUSTID_PROBE_KEY_ACCEPTED_CODES:
        return {"ok": True, "status": "ready"}
    if code in _ACOUSTID_PROBE_KEY_REJECTED_CODES:
        return {"ok": False, "status": "failed", "reason": "auth_failed",
                "error": "AcoustID API key was rejected."}
    if code in _ACOUSTID_PROBE_UNAVAILABLE_CODES:
        return {"ok": False, "status": "failed", "reason": "unavailable",
                "error": "AcoustID service unavailable. Try again later."}
    if code in _ACOUSTID_PROBE_RATE_LIMITED_CODES:
        return {"ok": False, "status": "failed", "reason": "rate_limited",
                "error": "AcoustID rate limited the request. Try again later."}
    if code is None and http_status == 429:
        return {"ok": False, "status": "failed", "reason": "rate_limited",
                "error": "AcoustID rate limited the request. Try again later."}
    if code is None and http_status is not None and http_status >= 500:
        return {"ok": False, "status": "failed", "reason": "unavailable",
                "error": "AcoustID service unavailable. Try again later."}
    if code is not None:
        return {"ok": False, "status": "failed", "reason": "provider_error",
                "error": f"AcoustID returned an unexpected error (code {code})."}
    return {"ok": False, "status": "failed", "reason": "bad_response",
            "error": "AcoustID returned an unexpected response."}


@app.post("/api/setup/test/acoustid")
def setup_test_acoustid():
    """Distinguish Beets-engine fingerprint readiness from API-key validity."""
    csrf_failure = _setup_csrf_failure()
    if csrf_failure is not None:
        return csrf_failure

    payload = request.get_json(silent=True) or {}
    from helpers_mb import ACOUSTID_NOT_CONFIGURED_MESSAGE, acoustid_api_key
    api_key = str(payload.get("api_key") or "").strip() or acoustid_api_key()
    diagnostics = _beets_plugin_diagnostics(Path(os.environ.get("BEETS_CONFIG", "/config/config.yaml")))
    capabilities = diagnostics.get("capabilities") if isinstance(diagnostics.get("capabilities"), dict) else {}
    acoustid_cap = capabilities.get("acoustid_lookup") if isinstance(capabilities.get("acoustid_lookup"), dict) else {}
    result: Dict[str, Any] = {
        "remote_reachable": bool(diagnostics.get("remote_reachable")),
        "fpcalc_available": bool(acoustid_cap.get("fpcalc_available", diagnostics.get("fpcalc_available"))),
        "fpcalc_path": str(diagnostics.get("fpcalc_path") or ""),
        "chroma_loaded": bool(acoustid_cap.get("chroma_loaded") or "chroma" in diagnostics.get("loaded_plugins", [])),
        "pyacoustid_available": bool(acoustid_cap.get("pyacoustid_available", diagnostics.get("pyacoustid_available"))),
    }
    if not result["remote_reachable"]:
        result.update({"ok": False, "status": "failed", "error": "Beets control agent is unavailable."})
        return jsonify(result), 200
    if not result["chroma_loaded"]:
        result.update({"ok": False, "status": "plugin_disabled", "error": "Beets chroma plugin is not enabled in the engine."})
        return jsonify(result), 200
    if not result["fpcalc_available"]:
        result.update({"ok": False, "status": "missing_dependency", "error": "fpcalc (Chromaprint) is not available in the Beets engine."})
        return jsonify(result), 200
    if not result["pyacoustid_available"]:
        result.update({"ok": False, "status": "missing_dependency", "error": "pyacoustid is not available in the Beets engine."})
        return jsonify(result), 200
    if not api_key:
        # IA-12: there is no built-in fallback key; lookups need the
        # operator's own application key.
        result.update({"ok": False, "status": "not_configured", "reason": "not_configured",
                       "error": ACOUSTID_NOT_CONFIGURED_MESSAGE})
        return jsonify(result), 200
    try:
        params = urllib.parse.urlencode({
            "client": api_key, "format": "json",
            "duration": "1", "fingerprint": "AQAAA0mUaEkSRZEeJk-eHtWMh4",
        })
        req = urllib.request.Request(f"https://api.acoustid.org/v2/lookup?{params}")
        http_status: Optional[int] = 200
        try:
            with provider_boundary.opened("acoustid", req, timeout=10, max_attempts=1) as r:
                raw = r.read()
        except urllib.error.HTTPError as http_err:
            # provider_boundary re-raises the original HTTPError; AcoustID puts
            # its error code in the body of 400/429/5xx answers.
            http_status = http_err.code
            try:
                raw = http_err.read() or b""
            except Exception:
                raw = b""
            finally:
                http_err.close()
        try:
            data: Any = json.loads(raw) if raw else None
        except ValueError:
            data = None
        outcome = _classify_acoustid_probe(http_status, data)
        probe_code = _acoustid_error_code(data)
        if not outcome["ok"] and (probe_code == 6 or (probe_code is None and http_status in (401, 403))):
            # IA-11: code 6 (invalid user key) and a bare 401/403 are key
            # rejections, not "unexpected" provider errors.
            outcome = {"ok": False, "status": "failed", "reason": "auth_failed",
                       "error": "AcoustID API key was rejected."}
        if not outcome["ok"]:
            app.logger.warning("AcoustID connectivity test failed: http=%s code=%s reason=%s",
                               http_status, _acoustid_error_code(data), outcome.get("reason"))
        result.update(outcome)
    except Exception as ex:
        app.logger.warning("AcoustID connectivity test failed: %s", type(ex).__name__)
        result.update({"ok": False, "status": "failed", "reason": "unreachable",
                       "error": "Could not reach AcoustID."})
    return jsonify(result), 200


@app.post("/api/setup/test/plex")
def setup_test_plex():
    csrf_failure = _setup_csrf_failure()
    if csrf_failure is not None:
        return csrf_failure

    payload = request.get_json(silent=True) or {}
    from backend.auth_service import probe_may_use_stored_secret
    configured_plex_url = (os.environ.get("PLEX_URL") or "").strip()
    supplied_plex_url = str(payload.get("url") or "").strip()
    plex_url = (supplied_plex_url or configured_plex_url).rstrip("/")
    plex_token = str(payload.get("token") or "").strip()
    if not plex_token and configured_plex_url and probe_may_use_stored_secret(supplied_plex_url, configured_plex_url):
        # SEC-1: the stored PLEX_TOKEN only ever goes to the configured
        # PLEX_URL, and only for an authenticated caller.
        plex_token = os.environ.get("PLEX_TOKEN") or ""
    if not plex_url or not plex_token:
        return jsonify({"ok": False, "status": "not_configured",
                         "error": "A Plex URL and token are both required to test Plex. Enter the token for this URL, or set PLEX_URL and PLEX_TOKEN."}), 200
    try:
        req = urllib.request.Request(f"{plex_url}/library/sections", headers={"X-Plex-Token": plex_token})
        with provider_boundary.opened("plex", req, timeout=10, max_attempts=1) as r:
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
    record = _load_settings_record()
    settings = dict(record.get("settings") or {})
    return jsonify({"ok": True, "revision": record.get("revision"), "settings": {
        k: (_mask(v) if "key" in k.lower() or "token" in k.lower() else v) for k, v in settings.items()
    }})


@app.post("/api/setup/settings")
def setup_save_settings():
    """Persist wizard-configured values that don't have a dedicated env var
    (e.g. selected AI model). Real secrets should be set via .env / Docker
    secrets, not through this endpoint -- this file is not treated as a secret
    store, only as a record of non-secret selections."""
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "expected a JSON object"}), 400
    expected_revision = payload.pop("expected_revision", None)
    store, relative_name = _settings_store_for_target()
    record = store.read_text_record(relative_name)
    if record.get("exists") and not expected_revision:
        return jsonify({"ok": False, "error": "expected_revision is required", "code": "settings_missing_revision"}), 428
    settings = dict(json.loads(record.get("content") or "{}"))
    settings.update(payload)
    try:
        result = store.save_json(relative_name, settings, is_secret=False, expected_revision=expected_revision)
    except WebManagerConfigStoreConflictError:
        return jsonify({"ok": False, "error": "settings changed; reload before saving", "code": "settings_revision_conflict"}), 409
    except WebManagerConfigStoreError:
        return jsonify({"ok": False, "error": "Could not save settings", "code": "settings_save_failed"}), 500
    return jsonify({"ok": True, "revision": result.get("revision")})


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
        from app import _persist_file_atomically
        _persist_file_atomically(token_file, token)
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


def _claim_setup_completion_marker() -> bool:
    """Atomically claim the setup-completion marker in Web Manager storage."""
    try:
        store, relative_name = _config_store_for_target(_SETUP_COMPLETE_MARKER)
        rec = store.read_text_record(relative_name)
        if rec.get("exists"):
            return False
        store.save_text(relative_name, "1", is_secret=True, expected_revision=None)
        return True
    except WebManagerConfigStoreConflictError:
        return False
    except Exception as ex:
        try:
            app.logger.error("Could not claim setup completion marker: %s", type(ex).__name__)
        except Exception:
            pass
        return False


def _migrate_legacy_setup_complete_if_established() -> None:
    """BUG-2 (v0.1.12): back-fill .setup_complete for installs that were
    established (a real admin credential exists and first-run is not
    required) before the two-step setup_first_run -> setup_complete flow
    existed, or that otherwise reached "claimed"/"legacy_established"
    browser-setup state without ever calling POST /api/setup/complete.

    _first_run_setup_required() already treats such installs as fully set
    up and never reopens first-run for them -- but the frontend's route
    guard (frontend/src/App.tsx) additionally gates on setup_complete
    specifically, so a real logged-in administrator on such an install was
    shown the First-Run Setup wizard instead of their dashboard on every
    visit. Confirmed live on the v0.1.11 TrueNAS production rollout (state
    was "legacy_established", .setup_complete had never been created) and
    corrected there by hand; this is the automatic version of that fix so
    it isn't a manual, one-off correction on every future affected install.

    Fail-closed: only acts when _has_explicit_browser_password() -- the
    same authoritative, freshly-reverified signal _first_run_setup_required()
    itself relies on, not merely a cached state-file string -- confirms a
    real, currently-usable credential exists right now. A fresh install,
    or an install whose credential has since been deleted/corrupted, is
    correctly left alone; first-run setup remains required for it. Uses
    the same O_CREAT|O_EXCL claim as setup_mark_complete() itself, so this
    is safe under concurrent worker/startup races and a no-op (not an
    error) on every later restart once the marker exists. Never touches
    username/password/token/Flask-secret/browser-setup-state files -- this
    only ever creates the completion marker, and only that.
    """
    if getattr(sys.modules.get("app"), "__routes_setup_test_stub__", False):
        return
    if _SETUP_COMPLETE_MARKER.exists():
        return
    try:
        from app import _browser_password_is_usable, _first_run_setup_required, _security_auth_password
    except ImportError:
        return
    try:
        if _first_run_setup_required():
            return
        # _has_explicit_browser_password() is deliberately NOT used here: it
        # checks the env var / file / persisted-hash paths but not the
        # legacy .initial_admin_password fallback, so it would wrongly
        # fail-closed on exactly the "legacy_established" installs this
        # migration exists for. _security_auth_password() resolves the
        # same full priority chain _first_run_setup_required()'s own
        # "claimed"/"legacy_established" state was originally derived
        # from (see _migrate_or_initialize_setup_state()), including that
        # fallback -- the complete, currently-effective credential.
        if not _browser_password_is_usable(_security_auth_password()):
            return
    except Exception:
        # Any failure evaluating the fail-closed evidence must not create
        # the marker -- an established install just retries this migration
        # on its next restart instead.
        return
    _claim_setup_completion_marker()


_migrate_legacy_setup_complete_if_established()


@app.post("/api/setup/first-run")
def setup_first_run():
    """Narrow bootstrap endpoint for initial browser credentials during first-run setup."""
    from app import (
        _first_run_setup_required,
        _password_requirements_unmet,
        _browser_password_is_usable,
        _persist_file_atomically,
        _cleanup_initial_browser_password_if_replaced,
        _set_browser_setup_state,
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
        if unmet or not _browser_password_is_usable(password):
            msg = "Password does not meet requirements: " + ", ".join(unmet) if unmet else "Password is not usable"
            return jsonify({"ok": False, "error": msg, "unmet_requirements": unmet}), 400

        from werkzeug.security import generate_password_hash
        hashed_password = generate_password_hash(password)
        user_ok = _persist_file_atomically(_PERSISTED_BROWSER_USERNAME_FILE, username)
        pass_ok = _persist_file_atomically(_PERSISTED_BROWSER_PASSWORD_FILE, hashed_password)

        if not (user_ok and pass_ok):
            return jsonify({"ok": False, "error": "Failed to persist credentials to storage"}), 500

        # Credentials are already durable at this point. Persisting the
        # "claimed" marker must also succeed before we can report success --
        # if it fails, do NOT return 200 (an ambiguous/partial write must
        # never be reported as complete). The system stays safe either way:
        # _first_run_setup_required() also treats a persisted browser
        # password as claimed, so anonymous bootstrap will not reopen even
        # if this specific write failed -- but the caller must be told the
        # truth so an operator can investigate storage health.
        if not _set_browser_setup_state("claimed"):
            # Anonymous retry is generally NOT available here: the persisted
            # browser password already makes _first_run_setup_required()
            # false, so don't imply "retry" as an anonymous bootstrap path.
            return jsonify({
                "ok": False,
                "error": "Credentials were saved, but setup state could not be finalized. "
                         "Sign in with the credentials you just created, and check Web "
                         "Manager storage permissions.",
            }), 500

        _cleanup_initial_browser_password_if_replaced()
        session.clear()
        session.permanent = True
        session["authenticated"] = True
        session["user"] = username
        _invalidate_setup_status_cache()

        return jsonify({"ok": True, "message": "First-run setup complete"})


@app.post("/api/setup/test/beets")
def setup_test_beets():
    """Live connectivity test against Stock Beets Web & Integration plugin.

    Fail-closed architecture: the primary "stock Beets read test"
    (`ok`/`status` at the top level of this response) tests ONLY stock
    Beets' native Web API on :8337 (`beets_adapter.get_stats()`). The
    WebManager integration plugin handshake (`/webmanager/status`) is
    tested independently and reported in its own sub-object, never folded
    into the primary `ok` value.
    """
    csrf_failure = _setup_csrf_failure()
    if csrf_failure is not None:
        return csrf_failure

    from backend.beets_adapter import beets_adapter

    # 1. PRIMARY: stock Beets native Web REST API on :8337 (/stats). This
    #    alone determines the top-level ok/status/error fields. Nothing
    #    else -- not the plugin handshake, not the legacy agent -- can make
    #    this pass if stock Beets itself is unreachable.
    stock_ok = False
    stock_version = "stock"
    stock_error: Optional[str] = None
    try:
        stats = beets_adapter.get_stats()
        stock_ok = isinstance(stats, dict) and "items" in stats
        if not stock_ok:
            stock_error = "Stock Beets Web API returned an unexpected response."
    except Exception as ex:
        app.logger.warning("setup_test_beets: stock Beets read test failed: %s", ex)
        stock_error = "Could not connect to stock Beets Web API. Check that the stock Beets container is running and reachable on :8337."

    # 2. Integration plugin handshake, tested independently of #1's result.
    plugin_result: Dict[str, Any] = {"ok": False}
    try:
        plugin_status = beets_adapter.get_plugin_status()
        if isinstance(plugin_status, dict) and plugin_status.get("protocol_version"):
            plugin_version = str(plugin_status.get("beets_version") or "unknown")
            plugin_result = {
                "ok": True,
                "beets_version": plugin_version,
                "plugin_version": plugin_status.get("plugin_version", "1.0.0"),
                "protocol_version": plugin_status.get("protocol_version", "1.0"),
                "library_ready": plugin_status.get("library_ready", True),
            }
            if stock_ok:
                stock_version = plugin_version
        else:
            plugin_result = {"ok": False, "error": "WebManager plugin handshake returned an unexpected response."}
    except Exception as ex:
        app.logger.warning("setup_test_beets: integration plugin handshake failed: %s", ex)
        plugin_result = {"ok": False, "error": "WebManager integration plugin is unreachable or not authenticated."}

    if stock_ok:
        return jsonify({
            "ok": True,
            "status": "connected",
            "version": stock_version,
            "beets_version": stock_version,
            "message": "Connected to Stock Beets Web API",
            "plugin_status": plugin_result,
        })

    return jsonify({
        "ok": False,
        "status": "failed",
        "error": stock_error or "Could not connect to stock Beets. Check BEETS_WEB_URL and ensure stock Beets is running on :8337.",
        "plugin_status": plugin_result,
    }), 200


@app.post("/api/setup/complete")
def setup_mark_complete():
    """Mark first-run setup as done. Validates Beets connectivity and establishes session."""
    stub_mode = getattr(sys.modules.get("app"), "__routes_setup_test_stub__", False)
    try:
        from app import (
            _browser_password_is_usable,
            _security_auth_password,
            _security_auth_username,
            _set_browser_setup_state,
        )
    except ImportError:
        if not stub_mode:
            raise

        def _browser_password_is_usable(value: str) -> bool:
            return True

        def _security_auth_password() -> str:
            return "stub-browser-password"

        def _security_auth_username() -> str:
            return "admin"

        def _set_browser_setup_state(state: str) -> bool:
            return True

    with _FIRST_RUN_LOCK:
        if _SETUP_COMPLETE_MARKER.exists():
            return jsonify({"ok": False, "error": "Setup already completed"}), 409

        beets_config_path = Path(os.environ.get("BEETS_CONFIG", "/config/config.yaml"))
        diagnostics = _beets_plugin_diagnostics(beets_config_path)
        is_test_env = app.config.get("TESTING") or stub_mode or os.environ.get("BEETS_SKIP_ENGINE_CHECK", "").strip().lower() in ("1", "true", "yes", "on")
        if not diagnostics.get("remote_reachable") and not is_test_env:
            return jsonify({
                "ok": False,
                "error": "Cannot complete setup: Beets engine control agent is unreachable. Check Docker service and configuration."
            }), 400

        # Enforce that required Beets plugins are healthy before completing setup
        if not is_test_env:
            try:
                from backend.beets_plugins import verify_all_plugins
                plugins_check = verify_all_plugins(beets_config_path.parent)
                if not plugins_check.get("all_required_healthy", True):
                    unhealthy = [
                        p["name"] for p in plugins_check.get("plugins", [])
                        if p.get("category") == "REQUIRED" and not p.get("healthy")
                    ]
                    if unhealthy:
                        return jsonify({
                            "ok": False,
                            "error": f"Cannot complete setup: required Beets plugins are not yet healthy ({', '.join(unhealthy)}). Please run plugin installation and configuration first.",
                            "unhealthy_plugins": unhealthy,
                        }), 400
            except Exception as exc:
                app.logger.warning("Plugin verification during setup_mark_complete encountered error: %s", exc)

        if not _browser_password_is_usable(_security_auth_password()):
            return jsonify({"ok": False, "error": "Cannot complete setup: administrator credentials are not configured."}), 409

        if not _set_browser_setup_state("claimed"):
            return jsonify({"ok": False, "error": "Could not persist browser setup state."}), 500

        if not _claim_setup_completion_marker():
            # Either a concurrent/prior call already completed setup, or a
            # durable-storage failure occurred. Either way, do not report
            # success -- the caller must not be told setup finished twice.
            return jsonify({"ok": False, "error": "Setup already completed"}), 409

        try:
            session.clear()
            session.permanent = True
            session["authenticated"] = True
            session["user"] = _security_auth_username()
        except RuntimeError:
            if not stub_mode:
                raise

        _invalidate_setup_status_cache()
        return jsonify({"ok": True, "message": "Setup completed successfully."})


@app.get("/health/live")
def health_live():
    """Liveness probe: the process is up and answering HTTP. No dependency
    checks — a failing DB/path should not make Docker/k8s kill the container
    (that's what readiness is for)."""
    return jsonify({"status": "alive", "version": _APP_VERSION})


@app.get("/health/ready")
def health_ready():
    """Readiness probe: stock Beets reachable over the plugin handshake plus
    this container's own mounts (checked locally, independent of Beets)."""
    config_path, config_path_error = _checked_beets_config_path()
    diagnostics = _beets_plugin_diagnostics(config_path)
    paths = diagnostics.get("paths") if isinstance(diagnostics.get("paths"), dict) and diagnostics.get("paths") else _local_paths_report(config_path)

    blocking = []
    reachable = bool(diagnostics.get("remote_reachable"))
    if not reachable:
        blocking.append("stock Beets unavailable")
    if not (paths.get("config") or {}).get("writable"):
        blocking.append("config path not writable")
    if not (paths.get("downloads") or {}).get("writable"):
        blocking.append("downloads path not writable")
    beets_config = paths.get("beets_config") or {}
    if config_path_error:
        blocking.append("beets config path invalid")
    elif not beets_config.get("exists"):
        blocking.append("beets config missing")
    if reachable and beets_config.get("exists") and not diagnostics.get("plugin_loader_ok"):
        blocking.append("beets plugin loader failed")

    status = "ready" if not blocking else "warning"
    return jsonify({
        "status": status,
        "version": _APP_VERSION,
        "blocking_reasons": blocking,
        "beets": {
            "available": bool(diagnostics.get("available")),
            "remote_reachable": reachable,
            "version": diagnostics.get("version") or "",
            "plugin_loader_ok": bool(diagnostics.get("plugin_loader_ok")),
        },
    }), (200 if not blocking else 503)


@app.get("/health")
def health_root():
    """Alias for /api/health under the unprefixed convention most container
    orchestrators probe by default."""
    return health_live()


@app.get("/api/plugins/status")
@app.get("/api/setup/plugins")
def plugins_status():
    """Return comprehensive Beets plugin verification report across categories."""
    beets_config_path, config_path_error = _checked_beets_config_path()
    if config_path_error:
        return jsonify({"ok": False, "all_required_healthy": False, "error": config_path_error}), 409
    try:
        from backend.beets_plugins import verify_all_plugins
        report = verify_all_plugins(beets_config_path.parent, config_file=beets_config_path)
    except Exception as exc:
        app.logger.error("plugins_status failed: %s", exc, exc_info=True)
        report = {
            "ok": False,
            "all_required_healthy": False,
            "required_count": 0,
            "required_healthy_count": 0,
            "plugins": [],
            "recommended_missing": [],
            "categories": {"required": [], "recommended": [], "optional": [], "integration": []},
            "summary": {"total": 0, "healthy": 0, "errors": ["Beets plugin verification failed."]},
        }
    return jsonify(report)


@app.post("/api/plugins/provision")
@app.post("/api/setup/plugins/provision")
def plugins_provision():
    """Copy bundled plugins to /config/beetsplug, safely update config.yaml, and re-verify."""
    csrf_failure = _setup_csrf_failure()
    if csrf_failure is not None:
        return csrf_failure
    from backend.config_manager import CONFIG_PATH_ERROR_MESSAGE, ConfigPathError, get_config_path
    try:
        # get_config_path() refuses a BEETS_CONFIG outside BEETSDIR (S-3).
        beets_config_path = get_config_path()
        from backend.beets_plugins import provision_and_verify
        result = provision_and_verify(beets_config_path.parent, config_file=beets_config_path)
    except ConfigPathError as exc:
        return jsonify({"ok": False, "all_required_healthy": False, "error": CONFIG_PATH_ERROR_MESSAGE}), exc.status_code
    except Exception as exc:
        app.logger.error("plugins_provision failed: %s", exc, exc_info=True)
        return jsonify({
            "ok": False,
            "all_required_healthy": False,
            "error": "Beets plugin provisioning failed.",
        }), 500
    _invalidate_setup_status_cache()
    return jsonify(result)


@app.post("/api/plugins/verify")
@app.post("/api/setup/plugins/verify")
def plugins_verify():
    """Re-run plugin verification without mutating any configuration files."""
    csrf_failure = _setup_csrf_failure()
    if csrf_failure is not None:
        return csrf_failure
    beets_config_path, config_path_error = _checked_beets_config_path()
    if config_path_error:
        return jsonify({"ok": False, "all_required_healthy": False, "error": config_path_error}), 409
    try:
        from backend.beets_plugins import verify_all_plugins
        result = verify_all_plugins(beets_config_path.parent, config_file=beets_config_path)
    except Exception as exc:
        app.logger.error("plugins_verify failed: %s", exc, exc_info=True)
        return jsonify({
            "ok": False,
            "all_required_healthy": False,
            "error": "Beets plugin verification failed.",
        }), 500
    return jsonify(result)


# ── Opt-in Beets config edits (BI-5 / BI-6) ──────────────────────────────────
# These are the ONLY setup routes that edit an existing Beets config.yaml.
# Each one is an explicit user action (POST + CSRF), writes a timestamped
# backup before touching the file, writes atomically, never changes a key it
# was not asked to change, and reports restart_required because stock Beets
# reads config.yaml only at startup.


def _beets_config_edit_error(exc: Exception, operation: str):
    from backend.beets_plugins import BeetsConfigEditError
    from backend.config_manager import CONFIG_PATH_ERROR_MESSAGE, ConfigError, ConfigPathError
    if isinstance(exc, ConfigPathError):
        # Path-policy refusal (BEETS_CONFIG outside BEETSDIR); fixed text.
        return jsonify({"ok": False, "error": CONFIG_PATH_ERROR_MESSAGE}), exc.status_code
    if isinstance(exc, ConfigError):
        return jsonify({"ok": False, "error": "Beets config update failed."}), exc.status_code
    if isinstance(exc, ValueError):
        return jsonify({"ok": False, "error": "Unsupported plugin selection."}), 400
    if isinstance(exc, BeetsConfigEditError):
        # The message is generated by us (no file contents or paths).
        return jsonify({"ok": False, "error": str(exc)}), 409
    app.logger.error("%s failed: %s", operation, exc, exc_info=True)
    return jsonify({"ok": False, "error": "Beets config update failed."}), 500


# Literal path (not the constant) so the AST endpoint inventory sees it.
@app.post("/api/setup/beets-config/include-paths")
def setup_beets_config_include_paths():
    """Fix action for the beets_web_include_paths_disabled warning: set
    web.include_paths to yes in the Beets config (backup first)."""
    csrf_failure = _setup_csrf_failure()
    if csrf_failure is not None:
        return csrf_failure
    try:
        from backend.beets_plugins import ensure_web_include_paths
        from backend.config_manager import get_config_path
        beets_config_path = get_config_path()
        result = ensure_web_include_paths(beets_config_path)
    except Exception as exc:
        return _beets_config_edit_error(exc, "setup_beets_config_include_paths")
    _invalidate_setup_status_cache()
    return jsonify(result)


@app.get("/api/setup/plugins/recommended")
def setup_plugins_recommended_preview():
    """Read-only preview (unified diff) of enabling the recommended plugins."""
    try:
        from backend.beets_plugins import preview_recommended_plugins
        from backend.config_manager import get_config_path
        beets_config_path = get_config_path()
        result = preview_recommended_plugins(beets_config_path)
    except Exception as exc:
        return _beets_config_edit_error(exc, "setup_plugins_recommended_preview")
    return jsonify(result)


@app.post("/api/setup/plugins/recommended/apply")
def setup_plugins_recommended_apply():
    """Explicit opt-in: add the selected recommended plugins to config.yaml.

    Body: {"plugins": ["fetchart", ...]} -- every name must come from the
    recommended list returned by the preview route.
    """
    csrf_failure = _setup_csrf_failure()
    if csrf_failure is not None:
        return csrf_failure
    body = request.get_json(silent=True)
    plugins = body.get("plugins") if isinstance(body, dict) else None
    if not isinstance(plugins, list) or not all(isinstance(p, str) for p in plugins):
        return jsonify({"ok": False, "error": "Body must be {\"plugins\": [names]}."}), 400
    try:
        from backend.beets_plugins import apply_recommended_plugins
        from backend.config_manager import get_config_path
        beets_config_path = get_config_path()
        result = apply_recommended_plugins(beets_config_path, plugins)
    except Exception as exc:
        return _beets_config_edit_error(exc, "setup_plugins_recommended_apply")
    _invalidate_setup_status_cache()
    return jsonify(result)

