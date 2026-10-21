"""Authentication for the webmanager operations.

Beets runs inside Web Manager (ADR 0006): the engine (backend/beets_embedded.py)
creates a per-process key with ``set_api_key`` and sends it on every in-process
call. The key exists only in memory; there is no key file and no environment
variable. Without a key every request is refused (fail closed).
"""

import hmac
import re
from typing import Optional
from flask import request, jsonify

_HEX_KEY_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_IN_MEMORY_KEY: Optional[str] = None


def _is_valid_key_format(key: str) -> bool:
    """At least 256 bits of entropy (64 hex characters)."""
    return bool(key and _HEX_KEY_PATTERN.match(key))


def set_api_key(key: Optional[str]) -> None:
    """Set this process' key (64 hex characters); ``None`` clears it."""
    global _IN_MEMORY_KEY
    _IN_MEMORY_KEY = key if key and _is_valid_key_format(key) else None


def get_expected_api_key() -> str:
    return _IN_MEMORY_KEY or ""


def verify_token(token: str) -> bool:
    """Constant-time comparison of a bearer token with this process' key."""
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

    if not get_expected_api_key():
        return (
            jsonify(
                {
                    "error": "WebManager API key is not configured",
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
