"""Configuration layer model for Beets Web Manager.

Every variable the product knows about belongs to exactly one layer. The
layer decides who may set it, whether the app may read it, and whether the
Settings page may save it:

``host``
    Docker Compose interpolation only (bind-mount sources, image tag, the
    published port's bind address). These are paths/values on the *host*;
    they mean nothing inside a container and application code must never
    read them. Shown read-only on the System page, for reference.
``deployment``
    Set in the Compose ``environment:`` block and read by the container
    (PUID/PGID/TZ/WEBCONTROL_PORT). Every shipped Compose file pins them, so
    a value saved from the Settings page could never take effect; they are
    read-only in the app and changing them means editing Compose and
    recreating the container.
``container``
    Absolute paths inside the Web Manager container (MUSIC_ROOT,
    DOWNLOADS_ROOT, BEETS_CONFIG, ...). They must match the Compose mount
    targets, so they also belong to the Compose ``environment:`` block, not
    to saved application settings.
``app`` / ``secret``
    Application settings and credentials. These are the only keys the
    Settings page may save to ``/web-manager-data/.env`` and the only keys
    the boot loader copies from that file into the process environment.
``dead``
    Names the app used to expose but never reads. Never loaded; removed from
    the saved settings file by the one-time migration.

This module is a leaf: it imports nothing from ``backend`` so the lowest
layer (``backend.app_runtime``) can use it during environment boot.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

log = logging.getLogger("beets.config_layers")

LAYER_HOST = "host"
LAYER_DEPLOYMENT = "deployment"
LAYER_CONTAINER = "container"
LAYER_APP = "app"
LAYER_SECRET = "secret"
LAYER_DEAD = "dead"
LAYER_UNKNOWN = "unknown"

# Restart semantics for a change to a variable.
APPLY_LIVE = "live"          # takes effect for the running process
APPLY_RESTART = "restart"    # needs a Web Manager restart (read at import time)
APPLY_DEPLOY = "deploy"      # needs a Compose edit and container recreation

# Compose interpolation only. Application code must never read these.
HOST_KEYS = frozenset({
    "MUSIC_PATH",
    "DOWNLOADS_PATH",
    "BEETS_CONFIG_PATH",
    "WEB_MANAGER_DATA_PATH",
    "BEETS_WEB_MANAGER_DATA_PATH",
    "BEETS_WEB_MANAGER_VERSION",
    "BEETS_WEB_BIND_ADDRESS",
})

# Pinned by every shipped Compose file's environment: block.
DEPLOYMENT_KEYS = frozenset({"PUID", "PGID", "TZ", "WEBCONTROL_PORT"})

# Container-side absolute paths (must match the Compose mount targets).
CONTAINER_KEYS = frozenset({
    "MUSIC_ROOT",
    "DOWNLOADS_ROOT",
    "BEETS_CONFIG",
    "BEETS_LOG",
    "BEETSDIR",
    "WEB_MANAGER_DATA_DIR",
    "BEETS_TRANSACTION_DIR",
    # Deprecated container aliases (see DEPRECATED_CONTAINER_ALIASES).
    "MUSIC_LIBRARY_PATH",
    "BEETS_MUSIC_DIR",
    "DOWNLOAD_PATH",
    # Deprecated: direct Beets DB path. Web Manager must not open the Beets
    # library; removal is tracked with BI-12 (library-transaction owner).
    "BEETS_LIBRARY",
})

# Exposed in the past, never read by the application.
DEAD_KEYS = frozenset({"PLAYLIST_DIR", "BEETS_SQLITE_TIMEOUT", "WEB_MANAGER_PATH"})

# Deprecated container aliases -> canonical container variable.
DEPRECATED_CONTAINER_ALIASES: Dict[str, str] = {
    "MUSIC_LIBRARY_PATH": "MUSIC_ROOT",
    "BEETS_MUSIC_DIR": "MUSIC_ROOT",
    "DOWNLOAD_PATH": "DOWNLOADS_ROOT",
}

# Application settings and secrets the Settings page may save and the boot
# loader may load. Every key in routes_setup._SETTING_METADATA, .env.example
# and the built-in fallback template must be classified (enforced by tests).
APP_KEYS = frozenset({
    "DEMO_MODE",
    "BEETS_LONG_OPERATION_MAX_SECONDS",
    "BEETS_LONG_OPERATION_POLL_SECONDS",
    "BEETS_ARTIST_RECONCILE_TIMEOUT_SECONDS",
    "BEETS_VERSION_PROBE_TIMEOUT_SECONDS",
    "BEETS_WEB_USERNAME",
    "BEETS_WEB_AUTH_DISABLED",
    "BEETS_WEB_AUTH_MIN_LENGTH",
    "BEETS_WEB_PASSWORD_MIN_LENGTH",
    "BEETS_TRUSTED_PROXIES",
    "BEETS_WEB_SESSION_COOKIE_SECURE",
    "BEETS_OUTBOUND_TIMEOUT_SECONDS",
    "BEETS_OUTBOUND_MAX_REDIRECTS",
    "BEETS_OUTBOUND_MAX_RESPONSE_BYTES",
    "BEETS_OUTBOUND_ALLOWLIST",
    "BEETS_WEB_URL",
    "AI_BASE_URL",
    "AI_MODEL",
    "IMPORT_REVIEW_QUARANTINE_DIR",
    "MUSIC_FORMAT_QUARANTINE_DIR",
    "PLEX_URL",
    "PLEX_MUSIC_SECTION",
    "LIDARR_URL",
    "SLSKD_URL",
    "SLSKD_SLSK_USERNAME",
    "QBITTORRENT_URL",
    "QBITTORRENT_USERNAME",
    "QBITTORRENT_CATEGORY",
    "SPOTIFY_CLIENT_ID",
    "PLAYLIST_AUTO_SYNC",
    "PLAYLIST_AUTO_SYNC_INTERVAL",
    "PLAYLIST_DOWNLOAD_METHODS",
    "PLAYLIST_MIN_DOWNLOAD_SECONDS",
    "YTDLP_JS_RUNTIMES",
    "YTDLP_PO_PROVIDER_URL",
    "YTDLP_COOKIE_FILE",
    "YTDLP_ALLOW_BROWSER_COOKIES",
    "YTDLP_NETRC_FILE",
    "SPOTIFLAC_SERVICES",
    "SPOTIFLAC_CMD",
    "SPOTIFLAC_AUTO_INSTALL",
    "SLSKD_API_KEY_FILE",
})

SECRET_KEYS = frozenset({
    "BEETS_WEB_PASSWORD",
    "BEETS_WEB_AUTH_TOKEN",
    "BEETS_WEBMANAGER_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "AI_API_KEY",
    "ACOUSTID_API_KEY",
    "ACOUSTID_USER_KEY",
    "ACOUSTID_KEY",
    "DISCOGS_TOKEN",
    "DISCOGS_USER_TOKEN",
    "LISTENBRAINZ_TOKEN",
    "SPOTIFY_CLIENT_SECRET",
    "PLEX_TOKEN",
    "LIDARR_API_KEY",
    "SLSKD_API_KEY",
    "SLSKD_SLSK_PASSWORD",
    "QBITTORRENT_PASSWORD",
})

LOADABLE_LAYERS = frozenset({LAYER_APP, LAYER_SECRET})

# Keys whose value is read once at import time by a module constant, so a
# Settings change needs a restart even though the boot loader supports it.
_RESTART_APP_KEYS = frozenset({
    "BEETS_WEB_URL",
    "BEETS_WEBMANAGER_API_KEY",
    "BEETS_WEB_AUTH_DISABLED",
    "BEETS_TRUSTED_PROXIES",
    "BEETS_WEB_SESSION_COOKIE_SECURE",
    "DEMO_MODE",
    "PLEX_URL",
    "PLEX_TOKEN",
    "LIDARR_URL",
    "LIDARR_API_KEY",
    "SLSKD_URL",
    "QBITTORRENT_URL",
    "QBITTORRENT_USERNAME",
    "QBITTORRENT_PASSWORD",
    "QBITTORRENT_CATEGORY",
    "PLAYLIST_AUTO_SYNC",
    "PLAYLIST_AUTO_SYNC_INTERVAL",
    "PLAYLIST_DOWNLOAD_METHODS",
    "PLAYLIST_MIN_DOWNLOAD_SECONDS",
    "YTDLP_PO_PROVIDER_URL",
    "YTDLP_COOKIE_FILE",
    "YTDLP_ALLOW_BROWSER_COOKIES",
    "YTDLP_NETRC_FILE",
})


def classify(name: str) -> str:
    """Return the layer of an environment variable name."""
    key = (name or "").strip()
    if key in HOST_KEYS:
        return LAYER_HOST
    if key in DEAD_KEYS:
        return LAYER_DEAD
    if key in DEPLOYMENT_KEYS:
        return LAYER_DEPLOYMENT
    if key in CONTAINER_KEYS:
        return LAYER_CONTAINER
    if key in SECRET_KEYS:
        return LAYER_SECRET
    if key in APP_KEYS:
        return LAYER_APP
    return LAYER_UNKNOWN


def apply_mode(name: str) -> str:
    """How a change to ``name`` takes effect: live, restart or deploy."""
    layer = classify(name)
    if layer in (LAYER_HOST, LAYER_DEPLOYMENT, LAYER_CONTAINER):
        return APPLY_DEPLOY
    if name in _RESTART_APP_KEYS:
        return APPLY_RESTART
    return APPLY_LIVE


def is_loadable(name: str) -> bool:
    """True when a saved value for ``name`` may enter the process env."""
    return classify(name) in LOADABLE_LAYERS


def is_saveable(name: str) -> bool:
    """True when the Settings page may save ``name``."""
    return classify(name) in LOADABLE_LAYERS


# ── Container paths ──────────────────────────────────────────────────────────

def _absolute_or_none(value: str) -> Optional[str]:
    text = (value or "").strip()
    if not text:
        return None
    # Container paths are POSIX; os.path.isabs also accepts a native absolute
    # path so the same code runs in local (non-container) test environments.
    if not (text.startswith("/") or os.path.isabs(text)):
        return None
    return text


_warned_aliases: set = set()


def _warn_once(message: str, *args) -> None:
    key = message % args if args else message
    if key in _warned_aliases:
        return
    _warned_aliases.add(key)
    log.warning(message, *args)


def container_path(canonical: str, default: str, *, environ: Optional[Dict[str, str]] = None) -> str:
    """Resolve a container-side absolute path from its canonical variable,
    then its deprecated aliases, then ``default``.

    Relative values (a host-side value such as ``./downloads`` that leaked
    into the container) are ignored with a warning, never used: a container
    path must be absolute.
    """
    env = os.environ if environ is None else environ
    raw = env.get(canonical, "")
    value = _absolute_or_none(raw)
    if value:
        return value
    if raw.strip():
        _warn_once("Ignoring %s=%r: container paths must be absolute; using the default.", canonical, raw.strip())
    for alias, target in DEPRECATED_CONTAINER_ALIASES.items():
        if target != canonical:
            continue
        alias_raw = env.get(alias, "")
        alias_value = _absolute_or_none(alias_raw)
        if alias_value:
            # Dockerfile ENV sets MUSIC_LIBRARY_PATH=/music and
            # DOWNLOAD_PATH=/downloads; only warn when an operator chose a
            # different value through the deprecated name.
            if alias_value != default:
                _warn_once("%s is deprecated; set %s instead (using %s).", alias, canonical, alias_value)
            return alias_value
    return default


def music_root(environ: Optional[Dict[str, str]] = None) -> str:
    return container_path("MUSIC_ROOT", "/music", environ=environ)


def downloads_root(environ: Optional[Dict[str, str]] = None) -> str:
    return container_path("DOWNLOADS_ROOT", "/downloads", environ=environ)


def beets_config_file(environ: Optional[Dict[str, str]] = None) -> str:
    return container_path("BEETS_CONFIG", "/config/config.yaml", environ=environ)


DEFAULT_BEETS_WEB_URL = "http://beets:8337"


def beets_web_url(environ: Optional[Dict[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    return (env.get("BEETS_WEB_URL", "") or "").strip() or DEFAULT_BEETS_WEB_URL


# ── Saved settings file (/web-manager-data/.env) ─────────────────────────────

_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
MIGRATION_REPORT_NAME = ".env.migration.json"


def _parse_line_key(raw_line: str) -> Optional[str]:
    stripped = raw_line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    candidate = stripped[7:].strip() if stripped.startswith("export ") else stripped
    if "=" not in candidate:
        return None
    key = candidate.split("=", 1)[0].strip()
    return key if _ENV_NAME_RE.match(key) else None


def keys_to_migrate(keys: Iterable[str]) -> List[str]:
    """Saved keys that must not live in the application settings file."""
    out: List[str] = []
    for key in keys:
        if classify(key) in (LAYER_HOST, LAYER_DEPLOYMENT, LAYER_CONTAINER, LAYER_DEAD) and key not in out:
            out.append(key)
    return out


def migration_report_path(env_file: Path) -> Path:
    return env_file.with_name(MIGRATION_REPORT_NAME)


def migrate_saved_env_file(env_file: Path) -> Dict[str, object]:
    """One-time, idempotent removal of non-application keys from the saved
    settings file.

    Earlier releases seeded the file from the whole ``.env.example`` template
    on the first Settings save, which copied host-side Compose values such
    as ``DOWNLOADS_PATH=./downloads`` and ``BEETS_CONFIG_PATH=./beets`` into
    it; the boot loader then exported them into the process environment.
    This rewrites the file without host, deployment, container and dead
    keys, after saving a ``.env.bak-migration-<timestamp>`` backup (mode
    0600, it may hold secrets). Only key names are logged or reported.
    Returns the report dict (``removed`` is empty when nothing changed).
    """
    report: Dict[str, object] = {"removed": [], "backup": "", "at": None}
    try:
        text = env_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return report
    except Exception as ex:
        log.warning("Saved settings migration skipped: cannot read %s (%s)", env_file, type(ex).__name__)
        return report

    lines = text.splitlines()
    removed: List[str] = []
    kept: List[str] = []
    for line in lines:
        key = _parse_line_key(line)
        if key is not None and key in keys_to_migrate([key]):
            if key not in removed:
                removed.append(key)
            continue
        kept.append(line)
    if not removed:
        return report

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = env_file.with_name(f"{env_file.name}.bak-migration-{stamp}")
    try:
        backup.write_text(text, encoding="utf-8")
        try:
            os.chmod(backup, 0o600)
        except Exception:
            pass
        tmp = env_file.with_name(f"{env_file.name}.migrate.tmp")
        tmp.write_text("\n".join(kept).rstrip() + "\n" if kept else "", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        tmp.replace(env_file)
    except Exception as ex:
        log.warning("Saved settings migration failed for %s (%s); leaving file unchanged", env_file, type(ex).__name__)
        return report

    report = {"removed": sorted(removed), "backup": str(backup), "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        migration_report_path(env_file).write_text(json.dumps(report, indent=2), encoding="utf-8")
    except Exception:
        pass
    log.warning(
        "Removed %d deployment-only key(s) from saved settings %s: %s (backup: %s)",
        len(removed), env_file, ", ".join(sorted(removed)), backup.name,
    )
    return report


def read_migration_report(env_file: Path) -> Dict[str, object]:
    """Last migration report, for the System page/status (names only)."""
    try:
        data = json.loads(migration_report_path(env_file).read_text(encoding="utf-8"))
    except Exception:
        return {"removed": [], "removed_count": 0, "backup": "", "at": None}
    removed = [str(k) for k in (data.get("removed") or []) if isinstance(k, str)]
    return {
        "removed": removed,
        "removed_count": len(removed),
        "backup": Path(str(data.get("backup") or "")).name,
        "at": data.get("at"),
    }


def load_saved_env_into_environ(
    env_file: Path,
    *,
    environ: Optional[Dict[str, str]] = None,
    decode=None,
    blocked: Iterable[str] = (),
) -> Tuple[List[str], List[str]]:
    """Load application-layer keys from the saved settings file into the
    environment, never overriding a non-blank existing value.

    Returns ``(loaded, ignored)`` key-name lists. Non-application keys
    (host/deployment/container/dead/unknown) are ignored, never exported.
    """
    env = os.environ if environ is None else environ
    loaded: List[str] = []
    ignored: List[str] = []
    blocked_set = set(blocked)
    try:
        text = env_file.read_text(encoding="utf-8")
    except Exception:
        return loaded, ignored
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        candidate = stripped[7:].strip() if stripped.startswith("export ") else stripped
        if "=" not in candidate:
            continue
        key, raw_value = candidate.split("=", 1)
        key = key.strip()
        if key in blocked_set or not _ENV_NAME_RE.match(key):
            continue
        if not is_loadable(key):
            if key not in ignored:
                ignored.append(key)
            continue
        if env.get(key, "").strip():
            continue
        value = decode(raw_value) if decode else raw_value.strip()
        if "\n" in value or "\r" in value or len(value) > 4096:
            continue
        env[key] = value
        loaded.append(key)
    if ignored:
        log.warning("Ignored non-application key(s) in saved settings %s: %s", env_file, ", ".join(ignored))
    return loaded, ignored
