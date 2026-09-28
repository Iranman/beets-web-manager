"""Session, configuration, plugin and diagnostics routes (ARCH-001).
"""

from __future__ import annotations

from flask import jsonify, request
from backend.audio_preferences import save_music_format_preferences as _save_music_format_preferences
from backend.beets_adapter import BeetsError, BeetsUnavailableError, BeetsAuthError
import backend.composite_workflows as composite_workflows
from backend.config_manager import ConfigConflictError, ConfigError, ConfigValidationError
from backend.ai_service import _run_ai_matching_regressions
from backend.app_runtime import _app_logger, _env_int, _plugin_install_log, _s
from backend.auth_service import _auth_failure_rate_limit_response, _constant_time_equal, _csrf_request_allowed, _json_security_error, _request_authorized, _security_auth_disabled, _security_auth_username, _verify_password
from backend.config_service import _contains_redacted_config_secret, _plugin_status_payload, _redact_config_content
from backend.playlist_service import _music_format_preferences
from backend.setup_service import _first_run_setup_required
from app import app  # noqa: E402  (route modules load after app.py defines app)
from backend.serializers import _config_error_response

# ── ARCH-001 extracted code ──


@app.post("/api/login")
@app.post("/login")
def api_login():
    if _security_auth_disabled():
        return jsonify({"ok": True, "message": "Auth is disabled", "username": "admin"})

    if not _csrf_request_allowed():
        return _json_security_error(403, "CSRF check failed")

    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "").strip()
    remember = payload.get("remember") is True

    if not username or not password:
        return jsonify({"ok": False, "error": "Username and password are required."}), 400

    expected_user = _security_auth_username()
    user_match = _constant_time_equal(username, expected_user)
    pass_match = _verify_password(password)

    if not (user_match and pass_match):
        limited = _auth_failure_rate_limit_response()
        if limited is not None:
            return limited
        return jsonify({"ok": False, "error": "Incorrect username or password."}), 401

    from flask import session
    session.clear()
    session.permanent = remember
    session["authenticated"] = True
    session["user"] = expected_user

    return jsonify({"ok": True, "username": expected_user, "message": "Sign-in successful"})


@app.post("/api/logout")
@app.post("/logout")
def api_logout():
    if not _csrf_request_allowed():
        return _json_security_error(403, "CSRF check failed")
    try:
        from flask import session
        session.clear()
    except Exception:
        pass
    return jsonify({"ok": True, "message": "Logged out"})


@app.get("/api/auth/me")
def api_auth_me():
    if _security_auth_disabled():
        return jsonify({
            "ok": True,
            "authenticated": True,
            "username": "admin",
            "auth_disabled": True,
            "first_run_required": False,
            "setup_complete": True,
        })
    auth_ok = _request_authorized()
    first_run_req = _first_run_setup_required()
    from routes_setup import _SETUP_COMPLETE_MARKER
    setup_complete = _SETUP_COMPLETE_MARKER.exists() and not first_run_req
    username = _security_auth_username() if auth_ok else ""
    return jsonify({
        "ok": True,
        "authenticated": auth_ok,
        "username": username,
        "first_run_required": first_run_req,
        "setup_complete": setup_complete,
    })


@app.get("/api/debug/ai-matching-regressions")
def api_ai_matching_regressions():
    return jsonify(_run_ai_matching_regressions())


# ── Music Format Preferences ─────────────────────────────────────────────────

@app.get("/api/settings/music-format")
def get_music_format_preferences():
    return jsonify({"ok": True, "preferences": _music_format_preferences()})


@app.post("/api/settings/music-format")
def save_music_format_preferences_route():
    payload = request.get_json(silent=True) or {}
    try:
        prefs = _save_music_format_preferences(payload.get("preferences") or payload)
    except Exception as ex:
        _app_logger.warning("Could not save music format preferences: %s", type(ex).__name__)
        return jsonify({"ok": False, "error": "Could not save preferences."}), 400
    return jsonify({"ok": True, "preferences": prefs})


_CONFIG_CONTENT_MAX_CHARS = _env_int(
    "BEETS_CONFIG_CONTENT_MAX_CHARS",
    1024 * 1024,
    minimum=1024,
    maximum=8 * 1024 * 1024,
)


@app.get("/api/config")
def get_config():
    try:
        result = composite_workflows.get_config()
    except BeetsAuthError:
        return jsonify({"ok": False, "error": "Beets engine authentication failed.", "code": "beets_auth_failed"}), 502
    except BeetsUnavailableError as exc:
        return jsonify({"ok": False, "error": "Beets engine is unavailable.", "code": "beets_unavailable"}), 503
    except BeetsError as exc:
        return _config_error_response(exc)
    except ConfigError as exc:
        return jsonify({"ok": False, "error": str(exc), "code": getattr(exc, "error_code", "config_read_failed")}), getattr(exc, "status_code", 502)
    text = str(result.get("content") or "")
    return jsonify({
        "ok": True,
        "content": _redact_config_content(text),
        "redacted": True,
        "revision": result.get("revision"),
        "has_backup": bool(result.get("has_backup")),
        "backup_ts": result.get("backup_ts"),
    })


@app.post("/api/config")
def save_config():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "Invalid JSON body", "code": "config_invalid_json"}), 400
    content = payload.get("content", "")
    if not isinstance(content, str):
        return jsonify({"ok": False, "error": "content must be a string", "code": "config_invalid_content"}), 400
    if len(content) > _CONFIG_CONTENT_MAX_CHARS:
        return jsonify({"ok": False, "error": "Config content is too large", "code": "config_too_large"}), 413
    if not content.strip():
        return jsonify({"ok": False, "error": "Empty config rejected", "code": "config_empty"}), 400
    if _contains_redacted_config_secret(content):
        return jsonify({"ok": False, "error": "Refusing to save redacted secret placeholders", "code": "config_redacted_placeholder"}), 400
    expected_revision = _s(payload.get("expected_revision") or payload.get("revision") or "").strip()
    if not expected_revision:
        return jsonify({"ok": False, "error": "expected_revision is required", "code": "config_missing_revision"}), 428
    try:
        result = composite_workflows.save_config(content, expected_revision=expected_revision)
    except BeetsAuthError:
        return jsonify({"ok": False, "error": "Beets engine authentication failed.", "code": "beets_auth_failed"}), 502
    except BeetsUnavailableError as exc:
        if exc.error_code:
            return _config_error_response(exc)
        return jsonify({"ok": False, "error": "Beets engine is unavailable.", "code": "beets_unavailable"}), 503
    except BeetsError as exc:
        return _config_error_response(exc)
    except ConfigConflictError:
        return jsonify({"ok": False, "error": "Config was changed by another writer; reload before saving.", "code": "config_revision_conflict"}), 409
    except ConfigValidationError as exc:
        return jsonify({"ok": False, "error": "Invalid Beets configuration YAML.", "code": "config_invalid_yaml"}), 400
    except ConfigError as exc:
        return jsonify({"ok": False, "error": str(exc), "code": getattr(exc, "error_code", "config_write_failed")}), getattr(exc, "status_code", 502)
    return jsonify({"ok": True, "backed_up": bool(result.get("backed_up")), "revision": result.get("revision")})


@app.post("/api/config/revert")
def revert_config():
    payload = request.get_json(silent=True) or {}
    expected_revision = _s(payload.get("expected_revision") or payload.get("revision") or "").strip()
    if not expected_revision:
        return jsonify({"ok": False, "error": "expected_revision is required", "code": "config_missing_revision"}), 428
    try:
        result = composite_workflows.revert_config(expected_revision=expected_revision)
    except BeetsAuthError:
        return jsonify({"ok": False, "error": "Beets engine authentication failed.", "code": "beets_auth_failed"}), 502
    except BeetsUnavailableError as exc:
        if exc.error_code:
            return _config_error_response(exc)
        return jsonify({"ok": False, "error": "Beets engine is unavailable.", "code": "beets_unavailable"}), 503
    except BeetsError as exc:
        return _config_error_response(exc)
    except ConfigConflictError:
        return jsonify({"ok": False, "error": "Config was changed by another writer; reload before saving.", "code": "config_revision_conflict"}), 409
    except ConfigValidationError as exc:
        return jsonify({"ok": False, "error": "Invalid Beets configuration YAML.", "code": "config_invalid_yaml"}), 400
    except ConfigError as exc:
        return jsonify({"ok": False, "error": str(exc), "code": getattr(exc, "error_code", "config_revert_failed")}), getattr(exc, "status_code", 502)
    return jsonify({"ok": True, "revision": result.get("revision")})


@app.get("/api/plugins/install-log")
def api_plugins_install_log():
    """Return the background plugin installer log."""
    return jsonify({"ok": True, "log": _plugin_install_log})


@app.get("/api/plugins/installed")
def api_plugins_installed():
    """Return which optional plugins are installed in the Python env."""
    return jsonify(_plugin_status_payload())
