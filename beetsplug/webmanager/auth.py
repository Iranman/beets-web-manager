"""Authentication helpers for WebManager plugin endpoints."""

import hmac
import os
import re
from typing import Optional
from flask import request, jsonify

_HEX_KEY_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_KEY_FILE_PATH: Optional[str] = None


def _is_valid_key_format(key: str) -> bool:
    """Validate that the API key has at least 256 bits entropy (64 hex characters)."""
    return bool(key and _HEX_KEY_PATTERN.match(key))


def set_api_key_file(path: str):
    """Set the API key file path configured for this plugin."""
    global _KEY_FILE_PATH
    _KEY_FILE_PATH = path


def get_expected_api_key() -> str:
    """Read API key from configured file or environment variable.

    Fails closed if the key file is a symlink, missing, empty, or not 64 hex characters.
    Reads directly from the file to eliminate cache staleness on key rotation.
    """
    global _KEY_FILE_PATH

    env_key = os.environ.get("BEETS_WEBMANAGER_API_KEY", "").strip()
    if env_key:
        return env_key if _is_valid_key_format(env_key) else ""

    if _KEY_FILE_PATH:
        if not os.path.isfile(_KEY_FILE_PATH) or os.path.islink(_KEY_FILE_PATH):
            return ""
        try:
            with open(_KEY_FILE_PATH, "r", encoding="utf-8") as f:
                key = f.read().strip()
            if _is_valid_key_format(key):
                return key
        except Exception:
            pass
        return ""

    candidates = []
    if os.environ.get("BEETS_WEBMANAGER_API_KEY_FILE"):
        candidates.append(os.environ["BEETS_WEBMANAGER_API_KEY_FILE"])
    if os.environ.get("BEETS_CONFIG"):
        candidates.append(os.path.join(os.path.dirname(os.environ["BEETS_CONFIG"]), ".webmanager_api_key"))
    if os.environ.get("BEETSDIR"):
        candidates.append(os.path.join(os.environ["BEETSDIR"], ".webmanager_api_key"))
    candidates.append("/config/.webmanager_api_key")

    for file_path in candidates:
        if not file_path or not os.path.isfile(file_path) or os.path.islink(file_path):
            continue
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                key = f.read().strip()
            if _is_valid_key_format(key):
                return key
        except Exception:
            pass
    return ""


def verify_token(token: str) -> bool:
    """Verify provided bearer token against expected API key using constant-time comparison."""
    expected = get_expected_api_key()
    if not expected or not token or not _is_valid_key_format(token):
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
                    "error": "WebManager API key is not configured or invalid on Beets server",
                    "error_code": "AUTH_NOT_CONFIGURED",
                }
            ),
            401,
        )

    if not token or not verify_token(token):
        return (
            jsonify(
                {
                    "error": "Invalid or missing Bearer token",
                    "error_code": "UNAUTHORIZED",
                }
            ),
            401,
        )
    return None
