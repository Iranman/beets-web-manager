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
import tempfile
import time
import unicodedata
import urllib.parse
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

# Operator-only: they run commands, name files the app reads or moves files
# into, or control a security boundary. A web session must never be able to
# change them (an XSS or a stolen session would otherwise widen its own
# reach), so they are set only in the Compose/Docker environment. A value
# saved by an earlier release is removed by the settings migration.
OPERATOR_KEYS = frozenset({
    "BEETS_OUTBOUND_ALLOWLIST",          # outbound network boundary (SSRF)
    "BEETS_WEB_AUTH_DISABLED",           # turns authentication off
    "BEETS_TRUSTED_PROXIES",             # whose X-Forwarded-For is believed
    "BEETS_WEB_SESSION_COOKIE_SECURE",   # session cookie transport security
    "BEETS_AUTH_RATE_LIMIT",             # login brute-force limits
    "BEETS_AUTH_RATE_WINDOW",
    "BEETS_AUTH_ACCOUNT_RATE_LIMIT",
    "BEETS_AUTH_ACCOUNT_RATE_WINDOW",
    "SPOTIFLAC_CMD",                     # command line that is executed
    "YTDLP_JS_RUNTIMES",                 # may name an executable path (node:/path)
    "YTDLP_REMOTE_COMPONENTS",           # lets yt-dlp fetch and run remote solver code
    "YTDLP_COOKIE_FILE",                 # credential file path read by yt-dlp
    "YTDLP_NETRC_FILE",                  # credential file path read by yt-dlp
    "YTDLP_ALLOW_BROWSER_COOKIES",       # lets yt-dlp read browser credential stores
    "SLSKD_API_KEY_FILE",                # file whose first line is sent to SLSKD_URL
    "IMPORT_REVIEW_QUARANTINE_DIR",      # directory library files are moved into
    "MUSIC_FORMAT_QUARANTINE_DIR",       # directory library files are moved into
})

# Pinned by every shipped Compose file's environment: block, plus the
# operator-only keys above (same layer: Compose environment only).
DEPLOYMENT_KEYS = frozenset({"PUID", "PGID", "TZ", "WEBCONTROL_PORT"}) | OPERATOR_KEYS

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
    "BEETS_WEB_AUTH_MIN_LENGTH",
    "BEETS_WEB_PASSWORD_MIN_LENGTH",
    "BEETS_OUTBOUND_TIMEOUT_SECONDS",
    "BEETS_OUTBOUND_TOTAL_TIMEOUT_SECONDS",
    "BEETS_OUTBOUND_MAX_REDIRECTS",
    "BEETS_OUTBOUND_MAX_RESPONSE_BYTES",
    "BEETS_WEB_URL",
    "AI_BASE_URL",
    "AI_MODEL",
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
    "YTDLP_PO_PROVIDER_URL",
    "SPOTIFLAC_SERVICES",
    "SPOTIFLAC_AUTO_INSTALL",
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
    "BEETS_OUTBOUND_TOTAL_TIMEOUT_SECONDS",
    "DEMO_MODE",
    # PLEX_URL/PLEX_TOKEN are absent on purpose: plex_service._plex_settings
    # reads os.environ on every call, so they apply live.
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


# Names that had a non-blank value in the process environment before the
# saved settings file was loaded: values Docker/Compose set. Such a value
# wins over a saved one at every restart (load_saved_env_into_environ never
# overrides it), so saving one from the Settings page would apply until the
# next restart and then silently revert. None until the boot loader records it.
_BOOT_ENV_PINNED: Optional[frozenset] = None


def record_boot_environment(environ: Optional[Dict[str, str]] = None) -> None:
    """Snapshot the Docker-set names; called once, before the saved file loads."""
    global _BOOT_ENV_PINNED
    env = os.environ if environ is None else environ
    _BOOT_ENV_PINNED = frozenset(k for k, v in env.items() if str(v or "").strip())


def pinned_by_boot_environment(name: str) -> bool:
    """True when Docker/Compose set ``name`` (non-blank) at boot."""
    return _BOOT_ENV_PINNED is not None and name in _BOOT_ENV_PINNED


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
        # Only the name: the raw value is operator input and is not logged.
        _warn_once("Ignoring %s: container paths must be absolute; using the default.", canonical)
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
                _warn_once("%s is deprecated; set %s instead.", alias, canonical)
            return alias_value
    return default


def music_root(environ: Optional[Dict[str, str]] = None) -> str:
    return container_path("MUSIC_ROOT", "/music", environ=environ)


def downloads_root(environ: Optional[Dict[str, str]] = None) -> str:
    return container_path("DOWNLOADS_ROOT", "/downloads", environ=environ)


UNSAFE_DOWNLOADS_ROOT_MESSAGE = (
    "DOWNLOADS_ROOT is the filesystem root or overlaps the music library (MUSIC_ROOT), so it is "
    "not used for any download, import or cleanup operation. Set DOWNLOADS_ROOT to a separate "
    "downloads mount that is neither inside nor around the library."
)


def unsafe_root_reason(root, music_root_path) -> str:
    """Why a configured download/staging root must not authorize file
    operations, or "" when it may: the filesystem root, or a root that
    overlaps the music library (equal to it, inside it, or containing it)."""
    try:
        rp = Path(os.path.realpath(str(root)))
        mp = Path(os.path.realpath(str(music_root_path)))
    except (TypeError, ValueError):
        return "it is not a valid path"
    if rp == Path(rp.anchor):
        return "it is the filesystem root"
    if rp == mp or mp in rp.parents or rp in mp.parents:
        return "it overlaps the music library"
    return ""


def safe_roots(name: str, roots: Iterable, music_root_path) -> Tuple[Path, ...]:
    """``roots`` without the unsafe ones (fail closed); each dropped root is
    logged as an error naming the setting."""
    kept: List[Path] = []
    for root in roots:
        reason = unsafe_root_reason(root, music_root_path)
        if reason:
            log.error("%s entry %s is ignored: %s.", name, root, reason)
        elif Path(root) not in kept:
            kept.append(Path(root))
    return tuple(kept)


def beets_config_file(environ: Optional[Dict[str, str]] = None) -> str:
    return container_path("BEETS_CONFIG", "/config/config.yaml", environ=environ)


DEFAULT_BEETS_WEB_URL = "http://beets:8337"


def beets_web_url(environ: Optional[Dict[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    return (env.get("BEETS_WEB_URL", "") or "").strip() or DEFAULT_BEETS_WEB_URL


def url_has_userinfo(url: str) -> bool:
    """True when ``url`` carries (or would be read as carrying) userinfo:
    any "@", also percent-encoded (%40) or a compatibility form such as the
    fullwidth U+FF20 that NFKC normalization turns into "@" (#208)."""
    return "@" in unicodedata.normalize("NFKC", urllib.parse.unquote(url or ""))


# Where the authority part of a URL in free text can end at the latest.
_URL_AUTHORITY_END_RE = re.compile(r"[\s\"'<>`]|://")
_URL_AUTHORITY_END_KEEP_SPACES_RE = re.compile(r"[\r\n\"'<>`]|://")


def url_userinfo_spans(text: str, *, spaces_end_url: bool = True) -> List[Tuple[int, int]]:
    """(start, end) of every URL userinfo in ``text``: from just after
    ``scheme://`` to the LAST ``@`` before the next whitespace, quote or
    ``://``. A raw ``/ ? # @ :`` inside a password, or a user-only token
    (``https://TOKEN@host``), is therefore inside the span (#239 F-3,
    #351 N2). With ``spaces_end_url=False`` (one config line) a space does
    not end the URL either, so a password holding a space is covered too.
    Fails closed: an ``@`` later in a path (``http://h/a@b``) also yields a
    span, over-redacting rather than leaking. Linear in len(text)."""
    end_re = _URL_AUTHORITY_END_RE if spaces_end_url else _URL_AUTHORITY_END_KEEP_SPACES_RE
    spans: List[Tuple[int, int]] = []
    pos = 0
    while True:
        start = text.find("://", pos)
        if start < 0:
            return spans
        auth = start + 3
        m = end_re.search(text, auth)
        end = m.start() if m else len(text)
        at = text.rfind("@", auth, end)
        if at > auth:
            spans.append((auth, at))
        pos = max(end, auth)


def redact_url_userinfo_in_text(text: str, mask: str = "[REDACTED]") -> str:
    """``text`` with every URL userinfo (see url_userinfo_spans) replaced by ``mask``."""
    out: List[str] = []
    pos = 0
    for start, end in url_userinfo_spans(text):
        out.append(text[pos:start] + mask)
        pos = end
    out.append(text[pos:])
    return "".join(out)


def redact_url_userinfo(url: str) -> str:
    """``url`` without ``user:pass@``, query or fragment, for display/logs."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    if url_has_userinfo(url) and ("@" not in parts.netloc or not parts.scheme or not parts.netloc):
        # Unencoded / ? # in a password, no scheme, or "http:/u:p@h": the
        # parser did not see the userinfo, so fail closed.
        return "<redacted-url>"
    netloc = parts.netloc.rpartition("@")[2]
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


# Beets config.yaml keys whose values are secrets. The single list shared by
# the config editor's redaction (config_service) and the setup preview diff
# masker (beets_plugins). Includes the secret keys Beets and its bundled
# plugins mark ``redact=True``; identifiers Beets also redacts (user,
# username, userid, client_id, prefix, google_engine) are left out because
# the editor refuses to save a redacted placeholder.
SECRET_CONFIG_KEYS = frozenset({
    "apikey", "api_key", "api_token", "apisecret", "auth_token", "token", "user_token",
    "pass", "password", "passphrase", "pwd", "secret", "client_secret",
    "access_token", "refresh_token", "fanarttv_key", "genius_api_key",
    "google_key", "lastfm_key",
})
_SECRET_KEY_WORDS = ("password", "passwd", "passphrase", "secret", "token", "bearer", "credential")
_SECRET_QUERY_KEYS = ("token", "key", "secret", "pass", "auth", "signature", "sig")


def is_secret_config_key(key: str) -> bool:
    """A config.yaml key whose value is treated as a secret: the exact list
    above, any ``*key``/``*pass``/``*pwd`` suffix, ``auth*`` prefix, or a key
    containing a secret word (SEC-237-5). One predicate for the config editor
    and the setup preview diff."""
    k = str(key).strip().strip("\"'").strip().lower()
    if not k:
        return False
    return (
        k in SECRET_CONFIG_KEYS
        or k.endswith(("key", "pass", "pwd")) or k.startswith("auth")
        or any(word in k for word in _SECRET_KEY_WORDS)
    )


# ponytail: values shorter than this are not masked by substring (masking "a"
# everywhere would wreck the text); the line-based redaction still hides them
# on ordinary ``key: value`` lines.
_MIN_MASKED_SECRET_LEN = 4


def _url_secrets(value: str) -> List[str]:
    """Password from URL userinfo and values of secret-named query parameters."""
    found: List[str] = []
    if "://" not in value:
        return found
    for start, end in url_userinfo_spans(value):
        # Not urlsplit's password: it stops at a raw "/" (#239 F-3). A
        # user-only "https://name@host" stays visible here (often a plain
        # user name; masking it would make the editor refuse the line).
        _user, sep, password = value[start:end].partition(":")
        if sep and password:
            found.append(password)
    try:
        parts = urllib.parse.urlsplit(value.strip())
        for name, qv in urllib.parse.parse_qsl(parts.query, keep_blank_values=False):
            if any(word in name.lower() for word in _SECRET_QUERY_KEYS):
                found.append(qv)
    except ValueError:
        pass
    return found


def _walk_config(node, under_secret: bool, out_secret: set, out_all: Optional[list], seen: set) -> None:
    # Containers are visited once by id(): YAML aliases share objects, so a
    # "billion laughs" document is walked in linear time.
    if isinstance(node, (dict, list)):
        if id(node) in seen:
            return
        seen.add(id(node))
        items = node.items() if isinstance(node, dict) else ((None, v) for v in node)
        for k, v in items:
            if k is not None and out_all is not None and isinstance(k, str):
                out_all.append(k)
            _walk_config(v, under_secret or (k is not None and is_secret_config_key(k)), out_secret, out_all, seen)
        return
    if node is None or isinstance(node, bool):
        return
    text = str(node)
    if out_all is not None:
        out_all.append(text)
    if under_secret and text:
        out_secret.add(text)
    if isinstance(node, str):
        out_secret.update(s for s in _url_secrets(node) if s)


def _load_config_documents(text: str):
    import yaml  # local: keep this module import-light
    try:
        return list(yaml.safe_load_all(text))
    except Exception:  # yaml.YAMLError, RecursionError on absurd nesting, ...
        return None


def config_secret_values(text: str) -> Optional[set]:
    """Every secret scalar in config text, found by parsing it as YAML (JSON
    included): values under secret keys at any depth (flow mappings, explicit
    ``? key`` entries, anchors read through aliases) plus URL userinfo
    passwords and secret query parameters. None if the text does not parse."""
    docs = _load_config_documents(text)
    if docs is None:
        return None
    found: set = set()
    seen: set = set()
    for doc in docs:
        _walk_config(doc, False, found, None, seen)
    return found


def config_scalars(text: str) -> Optional[List[str]]:
    """Every key and scalar value in config text as strings; None if it does not parse."""
    docs = _load_config_documents(text)
    if docs is None:
        return None
    out: List[str] = []
    seen: set = set()
    for doc in docs:
        _walk_config(doc, False, set(), out, seen)
    return out


def mask_config_secret_values(original: str, shown: str, mask: str) -> str:
    """Mask, in ``shown`` (the line-redacted form of ``original``), every
    secret value of ``original`` that is still visible.

    Fails closed (ValueError, no detail) when a secret could not be located:
    it is not written verbatim in ``original`` (escapes, folding) and it is
    still present in the parsed masked text, or the masked text no longer
    parses so that cannot be checked (#324 F2)."""
    secrets = config_secret_values(original)
    if not secrets:
        return shown
    unlocated = []
    for value in sorted(secrets, key=len, reverse=True):
        if len(value) < _MIN_MASKED_SECRET_LEN:
            continue
        if value in shown:
            shown = shown.replace(value, mask)
        elif value not in original:
            unlocated.append(value)
    if unlocated:
        remaining = config_scalars(shown)
        if remaining is None or any(v in r for v in unlocated for r in remaining):
            raise ValueError("config secrets could not be redacted")
    return shown


# ── Private (mode 0600) files ────────────────────────────────────────────────

def create_private_file(path: Path, data: str, *, unique: bool = False) -> Path:
    """Create a new file holding ``data`` at mode 0600 and return its path.

    The mode is set atomically by ``os.open(O_CREAT|O_EXCL, 0o600)``, so the
    file never exists at the umask mode (it may hold secrets). An existing
    path (or symlink) is never reused: with ``unique`` a ``-1``, ``-2``...
    suffix is tried, otherwise ``FileExistsError`` is raised.
    """
    for attempt in range(100 if unique else 1):
        candidate = path if attempt == 0 else path.with_name(f"{path.name}-{attempt}")
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:
            if unique:
                continue
            raise
        try:
            if hasattr(os, "fchmod"):  # via the fd: never follows a swapped path
                try:
                    os.fchmod(fd, 0o600)
                except OSError as ex:
                    log.warning("Could not set mode 0600 on %s (%s)", candidate.name, type(ex).__name__)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data.encode("utf-8"))
        except BaseException:
            try:
                os.unlink(candidate)
            except OSError:
                pass
            raise
        return candidate
    raise FileExistsError(f"no free file name for {path.name}")


def replace_private_file(path: Path, data: str) -> None:
    """Atomically replace ``path`` with ``data`` via a fresh 0600 temp file.

    The temp name is unique (mkstemp: O_EXCL, mode 0600), never a fixed
    ``<name>.tmp``: two concurrent saves each rename their own file, where
    one used to unlink the other's temp and fail (#183)."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data.encode("utf-8"))
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def ensure_private_mode(path: Path) -> None:
    """chmod 0600; a failure (e.g. a filesystem without POSIX modes) is logged."""
    try:
        os.chmod(path, 0o600)
    except OSError as ex:
        log.warning("Could not set mode 0600 on %s (%s)", path.name, type(ex).__name__)


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
        backup = create_private_file(backup, text, unique=True)
        replace_private_file(env_file, "\n".join(kept).rstrip() + "\n" if kept else "")
    except Exception as ex:
        log.warning("Saved settings migration failed for %s (%s); leaving file unchanged", env_file, type(ex).__name__)
        return report

    report = {"removed": sorted(removed), "backup": backup.name, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        replace_private_file(migration_report_path(env_file), json.dumps(report, indent=2))
    except Exception as ex:
        log.warning("Could not write the settings migration report (%s)", type(ex).__name__)
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
