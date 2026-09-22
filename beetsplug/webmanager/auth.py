"""Authentication helpers for WebManager plugin endpoints."""

import hmac
import os
from functools import wraps
from flask import request, jsonify, current_app

_CACHED_KEY = None
_CACHED_MTIME = 0
_KEY_FILE_PATH = None


def set_api_key_file(path: str):
    """Set the API key file path configured for this plugin."""
    global _KEY_FILE_PATH, _CACHED_KEY, _CACHED_MTIME
    _KEY_FILE_PATH = path
    _CACHED_KEY = None
    _CACHED_MTIME = 0


def get_expected_api_key() -> str:
    """Read API key from configured file or environment variable.
    
    Fails closed if the key file is a symlink, missing, empty, or too short.
    """
    global _CACHED_KEY, _CACHED_MTIME, _KEY_FILE_PATH

    env_key = os.environ.get("BEETS_WEBMANAGER_API_KEY", "").strip()
    if env_key and len(env_key) >= 16:
        return env_key

    file_path = _KEY_FILE_PATH or os.environ.get(
        "BEETS_WEBMANAGER_API_KEY_FILE", "/config/.webmanager_api_key"
    )
    if not file_path or not os.path.isfile(file_path):
        return ""

    # Symlink safety check: reject symlinked key file
    if os.path.islink(file_path):
        return ""

    try:
        mtime = os.path.getmtime(file_path)
        if _CACHED_KEY is not None and mtime == _CACHED_MTIME:
            return _CACHED_KEY
        with open(file_path, "r", encoding="utf-8") as f:
            key = f.read().strip()
        if len(key) < 16:
            return ""
        _CACHED_KEY = key
        _CACHED_MTIME = mtime
        return key
    except Exception:
        return ""


def verify_token(token: str) -> bool:
    """Verify provided bearer token against expected API key."""
    expected = get_expected_api_key()
    if not expected or not token:
        return False
    return hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))


def require_webmanager_auth():
    """Blueprint before_request hook verifying Bearer token authorization."""
    auth_header = request.headers.get("Authorization", "")
    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()

    expected = get_expected_api_key()
    if not expected:
        return (
            jsonify(
                {
                    "error": "Unauthorized",
                    "message": "WebManager API key is not provisioned on Beets server.",
                }
            ),
            401,
        )

    if not token or not verify_token(token):
        return (
            jsonify(
                {
                    "error": "Unauthorized",
                    "message": "Invalid or missing Bearer token.",
                }
            ),
            401,
        )
    return None
