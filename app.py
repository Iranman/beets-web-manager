#!/usr/bin/env python3
"""
Beets Web Manager — Flask application.

Creates the application, wires request hooks, security headers and static /
SPA serving, and registers the route modules (routes_*.py). All domain logic
lives in owned services (backend/*_service.py) over the one stock Beets
engine (backend.beets_adapter); see docs/arch001_service_decomposition.md.
"""
import datetime, gzip, os, re, secrets, sys, threading
from backend.security import install_secure_urllib
install_secure_urllib()
from backend.ai_batch_state_store import AiBatchStateConflictError
from pathlib import Path
from typing import Any, Dict, List, Optional

# Route modules import "app"; expose this running module under that name when
# Flask is launched from app.py so decorators attach to the active app instance.
if __name__ == "__main__":
    sys.modules["app"] = sys.modules[__name__]
else:
    sys.modules.setdefault("app", sys.modules[__name__])

# ── Owned services (ARCH-001) ─────────────────────────────────────────────────
# Names app.py itself uses; every other moved name resolves via __getattr__ below.
from backend.app_runtime import (  # noqa: E402
    AUDIO_EXT, EDITABLE_FIELDS, HOST, LIB_PATH, LIDARR_KEY, PORT, WEB_MANAGER_DATA_DIR, _app_logger,
    _env_int, _redact_security_text, _s,
)
from backend.setup_service import (  # noqa: E402
    _bootstrap_auth_token_if_missing, _bootstrap_browser_password_if_missing,
    _first_run_setup_required, _is_first_run_public_endpoint, _migrate_or_initialize_setup_state,
    _persist_bootstrap_secret_file,
)
from backend.ytdlp_service import (  # noqa: E402
    _install_ytdlp,
)
from backend.ai_service import (  # noqa: E402
    _AI_BATCH_STATE_DIR, _AI_REVIEW_DECISIONS_FILE,
)
from backend.library_service import (  # noqa: E402
    _legacy_local_scan_enabled,
)
from backend.playlist_service import (  # noqa: E402
    _start_playlist_auto_sync_worker, _start_playlist_index_warm_worker,
)
from backend.ai_batch_state_service import (  # noqa: E402
    _MUSIC_FORMAT_POLICY_HANDLED_MESSAGE, _get_ai_batch_store,
)
from backend.config_service import (  # noqa: E402
    _PLUGIN_MODULES, _bootstrap_beets_plugins, _install_optional_plugins,
    _repair_legacy_beets_config, _security_auth_configured,
)
from backend.artwork_service import (  # noqa: E402
    DISCOGS_TOKEN,
)
from backend.slskd_service import (  # noqa: E402
    SLSKD_API_KEY,
)
from backend.auth_service import (  # noqa: E402
    _auth_failure_rate_limit_response, _client_ip_is_lan, _content_security_policy, _csrf_request_allowed,
    _is_public_endpoint, _json_security_error, _rate_limit_profile_for_request,
    _rate_limit_response, _rate_limit_subject, _rate_limited, _request_authorized,
    _request_client_identity, _security_auth_disabled,
)
from backend.matching_service import (  # noqa: E402
    _ALBUM_MB_SUGGESTIONS_FILE,
)
from backend.transaction_service import (  # noqa: E402
    _install_transaction_job_hooks,
)
from backend.maintenance_service import (  # noqa: E402
    _FULL_SCAN_INTERVAL, _auto_scan_loop,
)


threading.Thread(target=_install_ytdlp, daemon=True).start()

threading.Thread(target=_install_optional_plugins, daemon=True).start()

from flask import Flask, Response, abort, jsonify, request, send_file
from backend.beets_adapter import beets_adapter
# Compatibility: engine error types callers historically imported from app.
from backend.beets_adapter import (  # noqa: F401
    BeetsAdapterConnectionError, BeetsAdapterTimeoutError, BeetsAuthError, BeetsBadRequestError,
    BeetsNotFoundError, BeetsUnavailableError,
)


app   = Flask(__name__)
# Flask's app.logger is logging.getLogger("app") (shared as _app_logger by every
# service module); touching it here attaches Flask's default handler.
if app.logger is not _app_logger:  # pragma: no cover - Flask naming invariant
    raise RuntimeError("Flask app logger is not the shared 'app' logger")
from backend.app_runtime import register_flask_app

register_flask_app(app)
APP_ROOT = Path(__file__).parent
REACT_DIST_DIR = APP_ROOT / "frontend" / "dist"
LEGACY_STATIC_DIR = APP_ROOT / "static"
_DEFAULT_MAX_CONTENT_LENGTH = 64 * 1024 * 1024

_FLASK_SECRET_KEY_FILE = Path(os.environ["BEETS_WEB_SECRET_KEY_FILE"]) if os.environ.get("BEETS_WEB_SECRET_KEY_FILE", "").strip() else (WEB_MANAGER_DATA_DIR / ".flask_secret_key")


def _get_or_create_flask_secret_key() -> str:
    env_key = os.environ.get("BEETS_WEB_SECRET_KEY", "").strip()
    if env_key:
        return env_key
    try:
        if _FLASK_SECRET_KEY_FILE.exists():
            k = _FLASK_SECRET_KEY_FILE.read_text(encoding="utf-8").strip()
            if len(k) >= 32:
                return k
    except Exception:
        pass
    new_key = secrets.token_hex(32)
    try:
        _persist_bootstrap_secret_file(_FLASK_SECRET_KEY_FILE, new_key)
    except Exception:
        pass
    return new_key

app.config.update(
    SECRET_KEY=_get_or_create_flask_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("BEETS_WEB_SESSION_COOKIE_SECURE", "0").strip().lower() in ("1", "true", "yes", "on"),
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=_env_int("BEETS_WEB_SESSION_HOURS", 168, minimum=1, maximum=8760)),
    MAX_CONTENT_LENGTH=_env_int("BEETS_WEB_MAX_CONTENT_LENGTH", _DEFAULT_MAX_CONTENT_LENGTH, minimum=1024 * 1024),
    MAX_FORM_MEMORY_SIZE=_env_int("BEETS_WEB_MAX_FORM_MEMORY_SIZE", 1024 * 1024, minimum=64 * 1024),
    MAX_FORM_PARTS=_env_int("BEETS_WEB_MAX_FORM_PARTS", 100, minimum=1, maximum=500),
)


_install_transaction_job_hooks()


_bootstrap_auth_token_if_missing()
_migrate_or_initialize_setup_state()
_bootstrap_browser_password_if_missing()


_repair_legacy_beets_config()


_bootstrap_beets_plugins()


@app.before_request
def _enforce_security_boundary():
    if _security_auth_disabled():
        return None

    if _first_run_setup_required():
        if _is_first_run_public_endpoint():
            return None
        if not _request_authorized():
            limited = _auth_failure_rate_limit_response()
            if limited is not None:
                return limited
            return _json_security_error(
                401,
                "First-run setup in progress. Authentication required for this endpoint.",
            )
    else:
        if _is_public_endpoint():
            return None

        if not _security_auth_configured():
            return _json_security_error(
                503,
                "Authentication is required. Set a strong BEETS_WEB_AUTH_TOKEN or BEETS_WEB_PASSWORD.",
            )
        if not _request_authorized():
            limited = _auth_failure_rate_limit_response()
            if limited is not None:
                return limited
            return _json_security_error(401, "Authentication required")

    if not _csrf_request_allowed():
        return _json_security_error(403, "CSRF check failed")

    if _client_ip_is_lan():
        return None

    bucket, limit, window_seconds = _rate_limit_profile_for_request()
    action_limited, action_retry = _rate_limited(
        bucket,
        _rate_limit_subject(include_auth=True),
        limit,
        window_seconds,
    )
    if action_limited:
        return _rate_limit_response(action_retry)
    return None


_COMPRESSIBLE_MIMETYPE_PREFIXES = ("application/json", "text/")
_COMPRESS_MIN_BYTES = 1024  # skip tiny responses; gzip overhead isn't worth it below this


@app.after_request
def _compress_response(response):
    """Gzip-compress JSON/text responses when the client accepts it.

    Registered before _set_security_headers below, so (per Flask's
    reverse-registration-order execution) it runs AFTER that hook --
    compression must happen last, once CSP header computation has already
    read the plaintext HTML body via get_data(as_text=True); compressing
    first would hand it undecodable gzip bytes.

    Added 2026-07-20: /api/library's summary response was measured at
    ~28.7MB uncompressed for a 545-artist library, sent with no
    Content-Encoding at all -- confirmed via curl that Accept-Encoding: gzip
    from the client was simply never honored. JSON compresses very well
    (typically 5-10x), so this targets the actual "page feels slow" residual
    once the server-side cache-warming fix (see _refresh_library_cache)
    eliminated the synchronous rebuild. Applies to every JSON/text response,
    not just /api/library -- general-purpose, zero risk to response content
    (bytes in, same bytes out after decompression), skips anything already
    encoded or served via direct_passthrough (file downloads, static assets
    with their own streaming).
    """
    try:
        if response.direct_passthrough:
            return response
        if response.headers.get("Content-Encoding"):
            return response
        accept_encoding = request.headers.get("Accept-Encoding", "")
        if "gzip" not in accept_encoding.lower():
            return response
        mimetype = (response.mimetype or "").lower()
        if not mimetype.startswith(_COMPRESSIBLE_MIMETYPE_PREFIXES):
            return response
        data = response.get_data()
        if len(data) < _COMPRESS_MIN_BYTES:
            return response
        compressed = gzip.compress(data, compresslevel=6)
        if len(compressed) >= len(data):
            return response
        response.set_data(compressed)
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Content-Length"] = str(len(compressed))
        existing_vary = response.headers.get("Vary", "")
        if "Accept-Encoding" not in existing_vary:
            response.headers["Vary"] = (
                f"{existing_vary}, Accept-Encoding" if existing_vary else "Accept-Encoding"
            )
    except Exception:
        pass
    return response


@app.after_request
def _set_security_headers(response):
    html_for_csp = None
    try:
        if response.mimetype == "text/html" and not response.direct_passthrough:
            html_for_csp = response.get_data(as_text=True)
    except Exception:
        html_for_csp = None
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Content-Security-Policy", _content_security_policy(html_for_csp))
    if request.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def _safe_static_file(base: Path, filename: str) -> Optional[Path]:
    raw = _s(filename)
    if "\x00" in raw or "\\" in raw:
        return None
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    try:
        root = base.resolve(strict=True)
    except Exception:
        return None
    target = (root / candidate).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target if target.exists() and target.is_file() else None

from werkzeug.exceptions import HTTPException as _WerkzeugHTTPException

@app.errorhandler(_WerkzeugHTTPException)
def _handle_http_error(exc):
    return jsonify({"ok": False, "error": exc.description, "status": exc.code}), exc.code

@app.errorhandler(AiBatchStateConflictError)
def _handle_ai_batch_state_conflict(exc):
    # Wave 26 independent review (section 25): AiBatchStateConflictError
    # was previously imported but never caught anywhere -- it fell through
    # to the generic Exception handler below, which returns 500, not 409.
    # A real CAS conflict is a real, expected outcome of concurrent
    # writers (see backend/ai_batch_state_store.py), not an unexpected
    # server error; every route that reaches _ai_batch_write_state /
    # save_batch_state / create_batch_state now reliably surfaces a real
    # 409 Conflict, without needing each call site to catch this
    # individually -- Flask/Werkzeug dispatch to the most specific
    # registered handler for the exception's class.
    return jsonify({"ok": False, "error": str(exc), "code": "ai_batch_state_conflict"}), 409

@app.errorhandler(Exception)
def _handle_unexpected_error(exc):
    import traceback as _tb
    _app_logger.error(
        "Unhandled exception in route: %s\n%s",
        _redact_security_text(exc),
        _redact_security_text(_tb.format_exc()),
    )
    return jsonify({"ok": False, "error": "Unexpected server error"}), 500


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return react_index_response()

@app.get("/assets/<path:filename>")
def react_assets(filename):
    """Serve Vite-built JS/CSS chunks at /assets/<file>."""
    for base in (REACT_DIST_DIR / "assets", LEGACY_STATIC_DIR / "assets"):
        target = _safe_static_file(base, filename)
        if target:
            return send_file(target)
    abort(404)


@app.get("/_next/static/<path:filename>")
def react_next_static(filename):
    """Serve Next static-export chunks from frontend/dist/_next/static."""
    target = _safe_static_file(REACT_DIST_DIR / "_next" / "static", filename)
    if target:
        return send_file(target)
    abort(404)


@app.get("/favicon.ico")
def favicon():
    for icon_path in (
        REACT_DIST_DIR / "favicon.ico",
        LEGACY_STATIC_DIR / "favicon.ico",
        APP_ROOT / "favicon.ico",
    ):
        if icon_path.exists() and icon_path.is_file():
            return send_file(icon_path)
    return Response(status=204)


def _health_checks() -> Dict[str, bool]:
    checks = {
        "app": True,
        "beets_web": False,
        "beets_webmanager_plugin": False,
        "lidarr_key": bool(LIDARR_KEY),
        "discogs_token": bool(DISCOGS_TOKEN),
        "slskd_key": bool(SLSKD_API_KEY),
        "openai_key": bool(os.environ.get("OPENAI_API_KEY")),
    }
    # 1. Probe stock Beets Web REST API (:8337)
    try:
        stats_data = beets_adapter.get_stats()
        checks["beets_web"] = isinstance(stats_data, dict) and "items" in stats_data
    except Exception:
        checks["beets_web"] = False

    # 2. Probe WebManager integration plugin (:8337)
    try:
        plugin_status = beets_adapter.get_plugin_status()
        checks["beets_webmanager_plugin"] = (
            isinstance(plugin_status, dict) and plugin_status.get("library_ready") is not False
        )
    except Exception:
        checks["beets_webmanager_plugin"] = False

    return checks


@app.get("/api/health")
def health():
    # Public/unauthenticated liveness. Dependency/readiness detail belongs on
    # /health/ready and authenticated /api/health/detail.
    return jsonify({"ok": True})


@app.get("/api/health/detail")
def health_detail():
    """Authenticated dependency diagnostics for the web manager."""
    checks = _health_checks()
    ok = checks["app"] and (checks["beets_web"] or checks["beets_webmanager_plugin"])
    return jsonify({"ok": ok, "checks": checks})


try:
    _ai_batch_migration_result = _get_ai_batch_store().migrate_legacy_files(
        _AI_BATCH_STATE_DIR, _AI_REVIEW_DECISIONS_FILE, _ALBUM_MB_SUGGESTIONS_FILE,
    )
    for _ai_migration_error in _ai_batch_migration_result.get("errors") or []:
        # Corrupt/unrecognized legacy AI state must be visible, not
        # silently discarded -- the file itself is left in place by
        # migrate_legacy_files(); this is the operator-visible signal.
        _app_logger.warning(
            "AI batch state migration: %s: %s",
            _ai_migration_error.get("file", "?"), _ai_migration_error.get("error", "?"),
        )
except Exception as _ai_migration_ex:
    _app_logger.warning("AI batch state migration failed unexpectedly: %s", _ai_migration_ex)


if _legacy_local_scan_enabled():
    threading.Thread(target=_auto_scan_loop, daemon=True).start()


# ── HTML ──────────────────────────────────────────────────────────────────────


def render_index():
    import json
    ef_js = json.dumps([[f, l] for f, l in EDITABLE_FIELDS])
    for index_path in (
        REACT_DIST_DIR / "index.html",
        LEGACY_STATIC_DIR / "index.html",
        APP_ROOT / "index.html",
    ):
        if index_path.exists():
            template = index_path.read_text(encoding="utf-8")
            return template.replace("__EDITABLE_FIELDS__", ef_js)
    abort(404)


def react_index_response():
    response = Response(render_index(), mimetype="text/html")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/<path:spa_path>")
def react_spa_fallback(spa_path):
    """Let direct browser loads of React routes resolve to the SPA entrypoint."""
    if spa_path.startswith("api/") or spa_path.startswith("assets/") or spa_path.startswith("_next/"):
        abort(404)
    return react_index_response()


# ── ARCH-001 compatibility ────────────────────────────────────────────────────
# Code moved out of app.py into owned modules (backend/*_service.py, routes_*.py).
# `app.<name>` and `from app import <name>` keep resolving for moved names
# (PEP 562), so callers and tests that address the old location still work.
# tests/test_arch001_architecture.py keeps this list equal to the inventory.
_ARCH001_OWNED_MODULES = (
    "backend.app_runtime",
    "backend.auth_service",
    "backend.config_service",
    "backend.setup_service",
    "backend.job_service",
    "backend.serializers",
    "backend.plex_service",
    "backend.acoustid_service",
    "backend.artwork_service",
    "backend.slskd_service",
    "backend.matching_service",
    "backend.musicbrainz_service",
    "backend.cleanup_service",
    "backend.ytdlp_service",
    "backend.playlist_service",
    "backend.ai_batch_state_service",
    "backend.import_reconciliation_service",
    "backend.pending_review_store",
    "backend.ai_evidence_service",
    "backend.library_service",
    "backend.ai_service",
    "backend.import_service",
    "backend.acquisition_service",
    "backend.replacement_service",
    "backend.maintenance_service",
    "backend.import_review_service",
    "backend.dedup_service",
    "backend.transaction_service",
    "routes_playlist",
    "routes_library",
    "routes_cleanup",
    "routes_import",
    "routes_maintenance",
    "routes_acquisition",
    "routes_system",
)


def __getattr__(name: str):
    import importlib
    for module_name in _ARCH001_OWNED_MODULES:
        module = _ROUTE_MODULE_INSTANCES.get(module_name) or sys.modules.get(module_name)
        if module is None:
            if module_name.startswith("routes_"):
                continue  # route modules load at the end of app.py, never on demand
            module = importlib.import_module(module_name)
        if name in module.__dict__:
            return module.__dict__[name]
    raise AttributeError(f"module 'app' has no attribute {name!r}")


# ── Route modules ─────────────────────────────────────────────────────────────
# Imported after all state is initialized; each module registers routes with app.
# A route module already loaded for an earlier `app` instance (a test that
# re-imports app.py) is loaded again privately so its routes register here too.
ROUTE_MODULES = (
    "routes_jobs", "routes_lidarr", "routes_setup", "routes_submissions",
    "routes_library", "routes_cleanup", "routes_import", "routes_playlist",
    "routes_acquisition", "routes_maintenance", "routes_system",
)


_ROUTE_MODULE_INSTANCES: Dict[str, Any] = {}


def _load_route_module(name: str):
    import importlib
    import importlib.util
    module = sys.modules.get(name)
    if module is None:
        module = importlib.import_module(name)
    elif getattr(module, "app", None) is not None and module.app is not app:
        # Loaded for an earlier app instance: register a private copy on this
        # one, leaving the earlier instance's module (and its routes) intact.
        spec = importlib.util.find_spec(name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    _ROUTE_MODULE_INSTANCES[name] = module
    return module


for _route_module_name in ROUTE_MODULES:
    _load_route_module(_route_module_name)


if __name__ == "__main__":
    _start_playlist_auto_sync_worker()
    if os.environ.get("PLAYLIST_WARM_INDEX", "0") not in ("0", "", "false", "False", "no"):
        _start_playlist_index_warm_worker()
    print(f"Beets Web Control → http://{HOST}:{PORT}")
    print(f"Library: {LIB_PATH}")
    # Waitress, not Flask's built-in dev server: pure-Python (works
    # identically on the Linux container and on Windows during local
    # development) and single-process/multi-threaded, matching the
    # threaded=True model this app was already built around — job stores,
    # caches, and dedup-scan state all live in module-level dicts that
    # assume one process. A pre-fork server (e.g. Gunicorn's default
    # worker model) would silently fragment that state across processes.
    from waitress import serve as _waitress_serve
    _waitress_serve(
        app,
        host=HOST,
        port=PORT,
        threads=_env_int("WEBCONTROL_THREADS", 8, minimum=1, maximum=64),
    )
