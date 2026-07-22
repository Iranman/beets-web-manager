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
import json
import os
import re
import secrets
import shutil
import subprocess
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
# The auth-bootstrap helpers below are imported lazily, inside the functions
# that use them, rather than here at module scope: tests/test_routes_setup.py
# exercises this module against a minimal stub `app` module (just a bare
# Flask() instance, none of app.py's real helpers) to test the setup wizard
# in isolation without booting the full app -- a module-level import of
# app.py-only names would break that stub import for every test in the file.

_SETTINGS_FILE = Path(os.environ.get("SETUP_SETTINGS_FILE", "/config/app_settings.json"))
_SETUP_COMPLETE_MARKER = Path(os.environ.get("SETUP_COMPLETE_FILE", "/config/.setup_complete"))
_SETUP_ENV_FILE = Path(os.environ.get("SETUP_ENV_FILE", "/config/.env"))
_ENV_EXAMPLE_FILE = Path(os.environ.get("SETUP_ENV_EXAMPLE_FILE", str(Path(__file__).parent / ".env.example")))
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_BLOCKED_ENV_NAMES = {"SETUP_ENV_FILE", "SETUP_ENV_EXAMPLE_FILE", "SETUP_SETTINGS_FILE", "SETUP_COMPLETE_FILE"}
_SECRET_ENV_PARTS = ("KEY", "TOKEN", "PASSWORD", "SECRET")
_PASSWORD_MIN_LENGTH_FLOOR = 12
_PASSWORD_SPECIAL_RE = re.compile(r"[^A-Za-z0-9]")
_PASSWORD_UPPER_RE = re.compile(r"[A-Z]")
_PASSWORD_LOWER_RE = re.compile(r"[a-z]")
_PASSWORD_DIGIT_RE = re.compile(r"[0-9]")
_FALLBACK_AUTH_TOKEN_FILE = Path(os.environ.get("BEETS_WEB_AUTH_TOKEN_FILE", "/config/.auth_token"))
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
        result["error"] = str(ex)
    return result



_BEETS_PLUGIN_FAILURE_PATTERNS = (
    "error loading plugin",
    "modulenotfounderror",
    "no module named",
    "initialization failed",
    "replaygain initialization failed",
)


def _redact_diagnostic_text(text: str) -> str:
    redacted = str(text or "")
    redacted = re.sub(
        r"(?i)((?:api[_-]?key|token|password|secret)\s*[:=]\s*)([^\s,'\"]+)",
        r"\1[redacted]",
        redacted,
    )
    return redacted


def _simple_yaml_scalar(value: str) -> str:
    return str(value or "").strip().strip('"\'')


def _parse_beets_config_summary(config_path: Path) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "plugins": [],
        "pluginpath": [],
        "replaygain_backend": "",
        "replaygain_command": "",
        "discogs_token_configured": False,
        "listenbrainz_token_configured": False,
    }
    try:
        lines = config_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return summary
    section = ""
    list_key = ""
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 0:
            list_key = ""
            if stripped.startswith("plugins:"):
                value = stripped.split(":", 1)[1].strip()
                if value:
                    summary["plugins"].extend(p for p in value.split() if p)
                else:
                    list_key = "plugins"
                section = "plugins"
                continue
            if stripped.startswith("pluginpath:"):
                value = stripped.split(":", 1)[1].strip()
                if value:
                    summary["pluginpath"].append(_simple_yaml_scalar(value))
                else:
                    list_key = "pluginpath"
                section = "pluginpath"
                continue
            section = stripped[:-1].strip() if stripped.endswith(":") else ""
            continue
        if list_key and stripped.startswith("-"):
            value = _simple_yaml_scalar(stripped[1:].strip())
            if value:
                if list_key == "plugins":
                    summary["plugins"].extend(p for p in value.split() if p)
                else:
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


def _beet_binary() -> Tuple[bool, str]:
    configured = os.environ.get("BEET_BIN", "beet").strip() or "beet"
    if os.path.isabs(configured) and Path(configured).exists():
        return True, configured
    resolved = shutil.which(configured)
    return bool(resolved), resolved or configured


def _run_beet_diagnostic(args: List[str], config_path: Path | None = None, timeout: int = 8) -> Dict[str, Any]:
    available, beet_bin = _beet_binary()
    if not available:
        return {"available": False, "path": beet_bin, "returncode": None, "stdout": "", "stderr": "beet executable not found"}
    cmd = [beet_bin]
    if config_path and config_path.exists():
        cmd.extend(["-c", str(config_path)])
    cmd.extend(args)
    env = {**os.environ}
    if config_path:
        env["BEETSDIR"] = str(config_path.parent)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return {
            "available": True,
            "path": beet_bin,
            "returncode": proc.returncode,
            "stdout": _redact_diagnostic_text(proc.stdout or ""),
            "stderr": _redact_diagnostic_text(proc.stderr or ""),
        }
    except Exception as ex:
        return {"available": True, "path": beet_bin, "returncode": None, "stdout": "", "stderr": str(ex)}


def _diagnostic_failure_lines(text: str) -> List[str]:
    failures: List[str] = []
    for line in str(text or "").splitlines():
        lower = line.lower()
        if any(pattern in lower for pattern in _BEETS_PLUGIN_FAILURE_PATTERNS):
            failures.append(_redact_diagnostic_text(line.strip()))
    return failures[:12]


def _beets_plugin_diagnostics(config_path: Path) -> Dict[str, Any]:
    config_summary = _parse_beets_config_summary(config_path)
    version_probe = _run_beet_diagnostic(["version"], timeout=8)
    plugin_probe = _run_beet_diagnostic(["plugins"], config_path=config_path, timeout=12) if config_path.exists() else {
        "available": version_probe.get("available"),
        "path": version_probe.get("path", ""),
        "returncode": None,
        "stdout": "",
        "stderr": "beets config missing",
    }
    combined = "\n".join([plugin_probe.get("stdout", ""), plugin_probe.get("stderr", "")])
    failures = _diagnostic_failure_lines(combined)
    version_text = (version_probe.get("stdout") or version_probe.get("stderr") or "").strip().splitlines()
    return {
        "available": bool(version_probe.get("available")),
        "path": version_probe.get("path", ""),
        "version": version_text[0] if version_text else "",
        "configured_plugins": config_summary["plugins"],
        "pluginpath": config_summary["pluginpath"],
        "plugin_failures": failures,
        "plugins_returncode": plugin_probe.get("returncode"),
        "replaygain_backend": config_summary.get("replaygain_backend") or "",
        "replaygain_command": config_summary.get("replaygain_command") or "",
        "discogs_token_configured": bool(
            os.environ.get("DISCOGS_TOKEN")
            or os.environ.get("DISCOGS_USER_TOKEN")
            or config_summary.get("discogs_token_configured")
        ),
        "listenbrainz_token_configured": bool(
            os.environ.get("LISTENBRAINZ_TOKEN")
            or config_summary.get("listenbrainz_token_configured")
        ),
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
        payload["note"] = note
    if detail:
        payload["detail"] = detail
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
    plugins = set(diagnostics.get("configured_plugins") or [])
    failures = list(diagnostics.get("plugin_failures") or [])
    failure = _plugin_failure_for(failures, name)
    if failure:
        return _integration_status(
            configured=False,
            required=required,
            state="dependency_plugin_missing",
            detail=failure,
        )
    if name not in plugins:
        return _integration_status(
            configured=False,
            required=required,
            state="installed_but_disabled",
            note="Plugin is installed but not enabled in config.yaml.",
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


@app.get("/api/setup/status")
def setup_status():
    """Readiness snapshot: paths, beets config, fpcalc, and each optional
    integration's configured/not-configured state (does not make live network
    calls — use the /api/setup/test/* endpoints for that)."""
    settings = _load_settings()

    config_check = _check_path(os.environ.get("BEETSDIR", "/config"), require_writable=True)
    music_check = _check_path("/data/media/music", require_writable=False)
    downloads_check = _check_path("/data/torrents", require_writable=True)
    beets_config_path = Path(os.environ.get("BEETS_CONFIG", "/config/config.yaml"))

    fpcalc_path = shutil.which("fpcalc")
    ffmpeg_path = shutil.which("ffmpeg")
    diagnostics = _beets_plugin_diagnostics(beets_config_path)
    configured_plugins = set(diagnostics.get("configured_plugins") or [])
    plugin_failures = list(diagnostics.get("plugin_failures") or [])
    replaygain_failure = _plugin_failure_for(plugin_failures, "replaygain")
    replaygain_backend = diagnostics.get("replaygain_backend") or ""
    replaygain_command = diagnostics.get("replaygain_command") or ""

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
        "acoustid": _integration_status(
            configured=bool(fpcalc_path and "chroma" in configured_plugins and not _plugin_failure_for(plugin_failures, "chroma")),
            state="configured" if fpcalc_path and "chroma" in configured_plugins and not _plugin_failure_for(plugin_failures, "chroma") else "dependency_plugin_missing",
            note="Fingerprinting available; AcoustID key is optional but improves rate limits." if fpcalc_path else "fpcalc is missing.",
        ),
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
            note="User plugins load from /config/beetsplug before bundled plugins in /app/beetsplug.",
        ),
        "replaygain": _integration_status(
            configured=bool(
                "replaygain" in configured_plugins
                and not replaygain_failure
                and replaygain_backend == "ffmpeg"
                and ffmpeg_path
                and not replaygain_command
            ),
            state=(
                "dependency_plugin_missing" if replaygain_failure
                else "configured" if "replaygain" in configured_plugins and replaygain_backend == "ffmpeg" and ffmpeg_path and not replaygain_command
                else "installed_but_disabled" if "replaygain" not in configured_plugins
                else "dependency_plugin_missing"
            ),
            note="ReplayGain uses the installed ffmpeg backend." if replaygain_backend == "ffmpeg" else "ReplayGain must use an installed backend.",
            detail=replaygain_failure,
        ),
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

    blocking: list = []
    if not config_check["writable"]:
        blocking.append(f"Cannot write to config path {config_check['path']}")
    if not music_check["exists"]:
        blocking.append(f"Music library path {music_check['path']} is not accessible")
    if not downloads_check["writable"]:
        blocking.append(f"Cannot write to downloads/staging path {downloads_check['path']}")
    if not beets_config_path.exists():
        blocking.append(
            f"Beets config not found at {beets_config_path} — copy config.yaml.example to config.yaml"
        )
    if not fpcalc_path:
        blocking.append("fpcalc (chromaprint) not found on PATH — AcoustID fingerprinting will not work")

    ready = not blocking
    demo_mode = os.environ.get("DEMO_MODE", "0").strip().lower() in ("1", "true", "yes", "on")
    try:
        from app import _auth_secret_is_usable, _security_auth_token, _security_auth_password, _GENERATED_AUTH_TOKEN_FILE
        token_configured = _auth_secret_is_usable(_security_auth_token())
        password_configured = _auth_secret_is_usable(_security_auth_password())
        token_auto_generated = token_configured and _GENERATED_AUTH_TOKEN_FILE.exists()
    except ImportError:
        token_configured = _fallback_auth_secret_usable(
            os.environ.get("BEETS_WEB_AUTH_TOKEN", "") or os.environ.get("BEETS_WEB_TOKEN", "")
        )
        password_configured = _fallback_auth_secret_usable(os.environ.get("BEETS_WEB_PASSWORD", ""))
        token_auto_generated = token_configured and _FALLBACK_AUTH_TOKEN_FILE.exists()
    auth_status = {
        "token_configured": token_configured,
        "token_auto_generated": token_auto_generated,
        "password_configured": password_configured,
    }
    return jsonify({
        "ok": True,
        "status": "ready" if ready else "warning",
        "version": _APP_VERSION,
        "demo_mode": demo_mode,
        "setup_complete": _SETUP_COMPLETE_MARKER.exists(),
        "blocking_reasons": blocking,
        "paths": {
            "config": config_check,
            "music_library": music_check,
            "downloads": downloads_check,
            "beets_config": {"path": str(beets_config_path), "exists": beets_config_path.exists()},
        },
        "fpcalc": {"available": bool(fpcalc_path), "path": fpcalc_path or ""},
        "beets": diagnostics,
        "auth": auth_status,
        "integrations": integrations,
        "settings": {k: (_mask(v) if "key" in k.lower() or "token" in k.lower() else v)
                     for k, v in settings.items()},
    })


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
        return jsonify({"ok": False, "error": f"Could not save environment file: {ex}"}), 500
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
    try:
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/models/{model}" if "openai.com" in base_url else f"{base_url.rstrip('/')}/models",
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
        return jsonify({"ok": False, "status": "failed",
                         "error": f"Could not reach the AI provider: {ex}"}), 200


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
        return jsonify({"ok": False, "status": "failed", "error": f"Could not reach MusicBrainz: {ex}"}), 200


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
        result.update({"ok": False, "status": "failed", "error": f"fpcalc failed to run: {ex}"})
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
        result.update({"ok": False, "status": "failed", "error": f"Could not reach AcoustID: {ex}"})
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
        return jsonify({"ok": False, "status": "failed",
                         "error": f"Could not reach Plex at {plex_url}: {ex}"}), 200


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
        return jsonify({"ok": False, "error": f"Could not save environment file: {ex}"}), 500
    try:
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(token, encoding="utf-8")
    except Exception:
        pass
    return jsonify({
        "ok": True,
        "token": token,
        "warning": "Save this token now — it will not be shown again.",
        "backup_path": backup_path,
    })


@app.post("/api/setup/complete")
def setup_mark_complete():
    """Mark first-run setup as done. Idempotent — safe to call repeatedly."""
    _SETUP_COMPLETE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    _SETUP_COMPLETE_MARKER.write_text("1", encoding="utf-8")
    return jsonify({"ok": True})


@app.get("/health/live")
def health_live():
    """Liveness probe: the process is up and answering HTTP. No dependency
    checks — a failing DB/path should not make Docker/k8s kill the container
    (that's what readiness is for)."""
    return jsonify({"status": "alive", "version": _APP_VERSION})


@app.get("/health/ready")
def health_ready():
    """Readiness probe: same underlying checks as /api/setup/status, boiled
    down to a single status a load balancer/orchestrator can act on."""
    config_check = _check_path(os.environ.get("BEETSDIR", "/config"), require_writable=True)
    downloads_check = _check_path("/data/torrents", require_writable=True)
    beets_config_path = Path(os.environ.get("BEETS_CONFIG", "/config/config.yaml"))
    blocking = []
    if not config_check["writable"]:
        blocking.append("config path not writable")
    if not downloads_check["writable"]:
        blocking.append("downloads path not writable")
    if not beets_config_path.exists():
        blocking.append("beets config missing")
    status = "ready" if not blocking else "warning"
    return jsonify({
        "status": status,
        "version": _APP_VERSION,
        "blocking_reasons": blocking,
    }), (200 if not blocking else 503)


@app.get("/health")
def health_root():
    """Alias for /api/health under the unprefixed convention most container
    orchestrators probe by default."""
    return health_live()
