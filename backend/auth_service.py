"""HTTP authentication, CSRF, rate limiting and credential persistence for the Web Manager (ARCH-001).

Web layer: these helpers read Flask's request/session and are called by the
before_request hooks and auth routes that stay in app.py.
"""

from __future__ import annotations

import base64, hashlib, hmac, os, re, secrets, string, threading, time
import urllib.error, urllib.parse, urllib.request
from backend.security import bounded_rate_key_store_sweep, direct_peer_is_trusted
from backend.web_manager_config_store import WebManagerConfigStore, WebManagerConfigStoreError
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from backend.app_runtime import _app_logger, WEB_MANAGER_DATA_DIR, _env_int, _s
from backend.app_runtime import _redact_security_text
from flask import g, has_request_context, jsonify, request

# ── ARCH-001 extracted code ──


def _yt_bot_check_message(msg: str) -> bool:
    text = (msg or "").lower()
    return (
        ("sign in to confirm" in text and ("not a bot" in text or "cookies" in text))
        or "use --cookies-from-browser or --cookies" in text
    )


def _yt_browser_cookie_unavailable_message(msg: str) -> bool:
    text = (msg or "").lower()
    return (
        "could not find chrome cookies" in text
        or "could not find chromium cookies" in text
        or "could not find browser cookies" in text
        or "browser profile" in text and "not found" in text
        or "cookiesfrombrowser" in text and "not found" in text
    )


def _yt_auth_source_failed_message(msg: str) -> bool:
    return _yt_bot_check_message(msg) or _yt_browser_cookie_unavailable_message(msg)


def _app_config_store_for_target(target_file: Path) -> Tuple[WebManagerConfigStore, str]:
    """Resolve the durable, authoritative WebManagerConfigStore for a Web
    Manager state file. Web Manager durable state lives beneath
    WEB_MANAGER_DATA_DIR only -- a target that does not resolve under it is
    rejected rather than promoting its own parent directory to a writable
    config root (that would let any caller-supplied path widen the trust
    boundary). Tests may inject WEB_MANAGER_DATA_DIR to a temporary path;
    every Web Manager state file constant defaults beneath it, so this holds
    for both production defaults and test injection."""
    target = Path(target_file).expanduser().resolve(strict=False)
    data_root = WEB_MANAGER_DATA_DIR.expanduser().resolve(strict=False)
    try:
        rel = target.relative_to(data_root)
    except ValueError as exc:
        raise WebManagerConfigStoreError(
            f"{target} is not beneath the Web Manager data root {data_root}"
        ) from exc
    return WebManagerConfigStore(data_root), rel.as_posix()


def _persist_app_config_text(target_file: Path, content: str, *, is_secret: bool = True) -> bool:
    try:
        store, relative_name = _app_config_store_for_target(target_file)
        current = store.read_text_record(relative_name)
        store.save_text(
            relative_name,
            content,
            is_secret=is_secret,
            expected_revision=current.get("revision"),
        )
        return True
    except (WebManagerConfigStoreError, OSError) as ex:
        try:
            _app_logger.error("Failed to persist %s: %s", target_file, type(ex).__name__)
        except Exception:
            pass
        return False


def _persist_file_atomically(target_file: Path, content: str) -> bool:
    return _persist_app_config_text(target_file, content, is_secret=True)


_AUTH_PUBLIC_ENDPOINTS = {
    ("GET", "health"),
    ("HEAD", "health"),
    ("GET", "react_assets"),
    ("HEAD", "react_assets"),
    ("GET", "react_next_static"),
    ("HEAD", "react_next_static"),
    ("GET", "favicon"),
    ("HEAD", "favicon"),
    ("GET", "index"),
    ("HEAD", "index"),
    ("GET", "react_spa_fallback"),
    ("HEAD", "react_spa_fallback"),
    ("GET", "health_live"),
    ("HEAD", "health_live"),
    ("GET", "health_ready"),
    ("HEAD", "health_ready"),
    ("GET", "health_root"),
    ("HEAD", "health_root"),
    ("GET", "setup_status"),
    ("HEAD", "setup_status"),
    ("POST", "api_login"),
    ("POST", "api_logout"),
    ("GET", "api_auth_me"),
    ("HEAD", "api_auth_me"),
}


_MIN_AUTH_SECRET_LENGTH = _env_int("BEETS_WEB_AUTH_MIN_LENGTH", 32, minimum=24, maximum=256)


_PLACEHOLDER_AUTH_SECRETS = {
    "admin", "password", "password1", "changeme", "changeit", "secret", "token",
    "default", "example", "letmein", "beets", "beetsweb", "setinenv", "setastrongownertoken",
}


_AUTH_RATE_LIMITS: Dict[str, Dict[str, Any]] = {}


_AUTH_RATE_LIMIT_LOCK = threading.Lock()


def _first_config_secret(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _security_auth_token() -> str:
    return _first_config_secret("BEETS_WEB_AUTH_TOKEN", "BEETS_WEB_TOKEN")


_GENERATED_AUTH_TOKEN_FILE = Path(
    os.environ.get("BEETS_WEB_AUTH_TOKEN_FILE", str(WEB_MANAGER_DATA_DIR / ".auth_token"))
)


_PERSISTED_BROWSER_PASSWORD_FILE = Path(
    os.environ.get("BEETS_WEB_PERSISTED_PASSWORD_FILE", str(WEB_MANAGER_DATA_DIR / ".browser_password"))
)


_INITIAL_BROWSER_PASSWORD_FILE = Path(
    os.environ.get("BEETS_WEB_INITIAL_PASSWORD_FILE", str(WEB_MANAGER_DATA_DIR / ".initial_admin_password"))
)


_PERSISTED_BROWSER_USERNAME_FILE = Path(
    os.environ.get("BEETS_WEB_PERSISTED_USERNAME_FILE", str(WEB_MANAGER_DATA_DIR / ".browser_username"))
)


def _security_auth_password() -> str:
    value = _first_config_secret("BEETS_WEB_PASSWORD")
    if value:
        return value
    path = os.environ.get("BEETS_WEB_PASSWORD_FILE", "").strip()
    if path:
        try:
            val = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
            if val:
                return val
        except Exception:
            pass
    try:
        if _PERSISTED_BROWSER_PASSWORD_FILE.exists():
            val = _PERSISTED_BROWSER_PASSWORD_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
            if val:
                return val
    except Exception:
        pass
    try:
        if _INITIAL_BROWSER_PASSWORD_FILE.exists():
            val = _INITIAL_BROWSER_PASSWORD_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
            if val:
                return val
    except Exception:
        pass
    return ""


def _security_auth_username() -> str:
    try:
        if _PERSISTED_BROWSER_USERNAME_FILE.exists():
            p_user = _PERSISTED_BROWSER_USERNAME_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
            if p_user:
                return p_user
    except Exception:
        pass
    val = os.environ.get("BEETS_WEB_USERNAME", "").strip()
    if val:
        return val
    return "admin"


_HASHED_PASSWORD_PREFIXES = ("scrypt:", "pbkdf2:", "argon2:")


def _browser_password_min_length() -> int:
    return _env_int("BEETS_WEB_PASSWORD_MIN_LENGTH", 16, minimum=12, maximum=256)


def _password_looks_placeholder(value: str) -> bool:
    secret = (value or "").strip()
    lowered = secret.lower()
    compact = re.sub(r"[^a-z0-9]+", "", lowered)
    if "${" in secret or "?" in secret:
        return True
    if compact in _PLACEHOLDER_AUTH_SECRETS:
        return True
    if compact.count("password") >= 2:
        return True
    return any(marker in compact for marker in ("changeme", "placeholder", "example", "setinenv"))


def _browser_password_is_usable(value: str) -> bool:
    secret = (value or "").strip()
    if not secret:
        return False
    if secret.startswith(_HASHED_PASSWORD_PREFIXES):
        return len(secret) >= _MIN_AUTH_SECRET_LENGTH
    if len(secret) < _browser_password_min_length():
        return False
    return not _password_looks_placeholder(secret)


def _password_requirements_unmet(password: str) -> List[str]:
    """Returns unmet BEETS_WEB_PASSWORD requirements (empty list = passes)."""
    unmet: List[str] = []
    min_length = _browser_password_min_length()
    if len(password) < min_length:
        unmet.append(f"at least {min_length} characters")
    if _password_looks_placeholder(password):
        unmet.append("not a placeholder password")
    return unmet


def generate_secure_browser_password(length: int = 36) -> str:
    """Generate a cryptographically secure browser password meeting all complexity rules."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()_+-=[]{}|;:,.<>?"
    while True:
        upper = secrets.choice(string.ascii_uppercase)
        lower = secrets.choice(string.ascii_lowercase)
        digit = secrets.choice(string.digits)
        special = secrets.choice("!@#$%^&*()_+-=[]{}|;:,.<>?")
        rest = [secrets.choice(alphabet) for _ in range(max(0, length - 4))]
        chars = [upper, lower, digit, special] + rest
        secrets.SystemRandom().shuffle(chars)
        pwd = "".join(chars)
        if not _password_requirements_unmet(pwd) and _browser_password_is_usable(pwd):
            return pwd


def _auth_secret_is_usable(value: str) -> bool:
    secret = (value or "").strip()
    if len(secret) < _MIN_AUTH_SECRET_LENGTH:
        return False
    lowered = secret.lower()
    compact = re.sub(r"[^a-z0-9]+", "", lowered)
    if "${" in secret or "?" in secret:
        return False
    if compact in _PLACEHOLDER_AUTH_SECRETS:
        return False
    if any(marker in compact for marker in ("changeme", "placeholder", "example", "setinenv")):
        return False
    return True


def _has_explicit_browser_password() -> bool:
    value = _first_config_secret("BEETS_WEB_PASSWORD")
    if value and _browser_password_is_usable(value):
        return True
    path = os.environ.get("BEETS_WEB_PASSWORD_FILE", "").strip()
    if path:
        try:
            val = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
            if val and _browser_password_is_usable(val):
                return True
        except Exception:
            pass
    try:
        if _PERSISTED_BROWSER_PASSWORD_FILE.exists():
            val = _PERSISTED_BROWSER_PASSWORD_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
            if val and _browser_password_is_usable(val):
                return True
    except Exception:
        pass
    return False


def _security_auth_disabled() -> bool:
    return os.environ.get("BEETS_WEB_AUTH_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def generate_secure_auth_token() -> str:
    """256-bit-entropy URL-safe token, well above _MIN_AUTH_SECRET_LENGTH and
    never a placeholder string -- suitable for signing/bearer authentication."""
    return secrets.token_urlsafe(32)


def _persist_generated_auth_token(token: str) -> None:
    """Persist generated token atomically to BEETS_WEB_AUTH_TOKEN_FILE (/web-manager-data/.auth_token)."""
    _persist_file_atomically(_GENERATED_AUTH_TOKEN_FILE, token)


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest((left or "").encode("utf-8"), (right or "").encode("utf-8"))


def _verify_password(supplied: str) -> bool:
    """Check the browser password. Inside a request, the attempt is
    rate-limited *before* the password is evaluated (SEC-3): while the
    client IP's auth bucket or the account-wide bucket is exhausted, every
    attempt -- including a correct one -- is refused without running the
    hash check, and callers' _auth_failure_rate_limit_response() turns that
    into a 429. Every rejected attempt is recorded in the account bucket."""
    in_request = has_request_context()
    if in_request:
        g._bwm_password_attempted = True
        if _password_attempts_limited():
            return False
    result = _check_password_value(supplied)
    if in_request and not result:
        _record_account_auth_failure()
    return result


def _check_password_value(supplied: str) -> bool:
    expected = _security_auth_password()
    if not _browser_password_is_usable(expected) or not supplied:
        return False
    if expected.startswith(("scrypt:", "pbkdf2:", "argon2:")):
        try:
            from werkzeug.security import check_password_hash
            return check_password_hash(expected, supplied)
        except Exception:
            return False
    return _constant_time_equal(supplied, expected)


def _auth_ip_limit() -> Tuple[int, int]:
    return (
        _env_int("BEETS_AUTH_RATE_LIMIT", 30, minimum=5, maximum=1000),
        _env_int("BEETS_AUTH_RATE_WINDOW", 60, minimum=10, maximum=3600),
    )


def _auth_account_limit() -> Tuple[int, int]:
    return (
        _env_int("BEETS_AUTH_ACCOUNT_RATE_LIMIT", 100, minimum=10, maximum=10000),
        _env_int("BEETS_AUTH_ACCOUNT_RATE_WINDOW", 300, minimum=10, maximum=86400),
    )


_ACCOUNT_RATE_SUBJECT = "browser-account"


def _password_attempts_limited() -> bool:
    ip_limit, ip_window = _auth_ip_limit()
    acct_limit, acct_window = _auth_account_limit()
    ip_limited, _ = _rate_limit_peek("auth", _rate_limit_subject(include_auth=False), ip_limit, ip_window)
    acct_limited, _ = _rate_limit_peek("auth-account", _ACCOUNT_RATE_SUBJECT, acct_limit, acct_window)
    return ip_limited or acct_limited


def _record_account_auth_failure() -> None:
    acct_limit, acct_window = _auth_account_limit()
    _rate_limited("auth-account", _ACCOUNT_RATE_SUBJECT, acct_limit, acct_window)


def _bearer_authorized(header: str) -> bool:
    token = _security_auth_token()
    if not _auth_secret_is_usable(token) or not header.lower().startswith("bearer "):
        return False
    return _constant_time_equal(header.split(" ", 1)[1].strip(), token)


def _basic_authorized(header: str) -> bool:
    password = _security_auth_password()
    if not _browser_password_is_usable(password) or not header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(header.split(" ", 1)[1].strip(), validate=True).decode("utf-8")
    except Exception:
        return False
    username, sep, supplied_password = raw.partition(":")
    if not sep:
        return False
    # SEC-11: evaluate both, so a wrong username costs the same as a wrong
    # password (no username timing oracle).
    user_ok = _constant_time_equal(username, _security_auth_username())
    password_ok = _verify_password(supplied_password)
    return user_ok and password_ok


def _session_authorized() -> bool:
    try:
        from flask import session
        return bool(session.get("authenticated") is True and session.get("user"))
    except Exception:
        return False


def _request_authorized() -> bool:
    header = request.headers.get("Authorization", "")
    return _bearer_authorized(header) or _basic_authorized(header) or _session_authorized()


def probe_may_use_stored_secret(supplied_url: str, configured_url: str) -> bool:
    """SEC-1: may a connectivity probe fall back to a *stored* credential?

    Only for an authenticated caller (never during anonymous first-run
    setup), and only when the probe targets the operator-configured endpoint
    -- either no URL was supplied or the supplied URL is that same endpoint.
    A caller-supplied URL must always come with a caller-supplied key, so a
    stored secret can never be sent to a host the caller chose."""
    if not _security_auth_disabled():
        try:
            if not _request_authorized():
                return False
        except RuntimeError:
            return False
    supplied = (supplied_url or "").strip()
    if not supplied:
        return True
    from backend.security import same_endpoint_url
    return same_endpoint_url(supplied, configured_url)


def _trusted_proxy_cidrs() -> List[str]:
    raw = os.environ.get("BEETS_TRUSTED_PROXIES", "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def _valid_client_ip(value: str) -> bool:
    try:
        import ipaddress as _ipaddress
        _ipaddress.ip_address((value or "").strip())
        return True
    except Exception:
        return False


def _request_client_identity() -> str:
    """The client IP used for rate limiting and the LAN exemption.

    SEC-4: forwarded headers are honoured only from a trusted proxy, and
    X-Forwarded-For is walked right to left -- each proxy *appends* the
    address it received from, so only the entries added by trusted proxies
    are reliable; the first untrusted hop from the right is the client.
    The leftmost entry is whatever the client chose to send and is never
    trusted on its own."""
    peer = (request.remote_addr or "").strip()
    trusted = _trusted_proxy_cidrs()
    if not direct_peer_is_trusted(peer, trusted):
        return peer or "unknown"
    raw_xff = ",".join(request.headers.getlist("X-Forwarded-For"))
    hops = [hop.strip() for hop in raw_xff.split(",") if hop.strip()]
    if hops:
        for hop in reversed(hops):
            if not _valid_client_ip(hop):
                # A malformed entry beyond the trusted chain: stop at the
                # last address a trusted proxy vouched for.
                return peer or "unknown"
            if not direct_peer_is_trusted(hop, trusted):
                return hop
        return hops[0]
    real_ip = request.headers.get("X-Real-IP", "").strip()
    if _valid_client_ip(real_ip):
        return real_ip
    return peer or "unknown"


def _rate_limit_subject(include_auth: bool = False) -> str:
    base = _request_client_identity()
    if not include_auth:
        return base
    header = request.headers.get("Authorization", "")
    digest = hashlib.sha256(header.encode("utf-8", errors="ignore")).hexdigest()[:16] if header else "noauth"
    return f"{base}:{digest}"


def _rate_limit_profile_for_request() -> Tuple[str, int, int]:
    path = (request.path or "").lower()
    endpoint = (request.endpoint or "").lower()
    method = request.method.upper()
    scope = f"{path} {endpoint}"
    if "ytdlp" in scope or "youtube" in scope:
        return "ytdlp", 10, 300
    if "ai" in scope or "openai" in scope or "genre" in scope:
        return "ai", 12, 300
    if "fingerprint" in scope or "acoustid" in scope:
        return "fingerprint", 12, 300
    if "replace" in scope or "replacement" in scope:
        return "replacement", 6, 300
    if "cleanup" in scope or "clean" in scope or "delete" in scope or "merge" in scope or "move" in scope:
        return "cleanup", 8, 300
    if "scan" in scope or request.args.get("refresh") == "1":
        return "scan", 12, 300
    if "playlist" in scope and method not in {"GET", "HEAD", "OPTIONS"}:
        return "playlist-write", 20, 300
    if "search" in scope:
        return "search", 60, 60
    if method not in {"GET", "HEAD", "OPTIONS"}:
        return "write", 120, 60
    return "read", 600, 60


def _rate_limit_peek(bucket: str, subject: str, limit: int, window_seconds: int) -> Tuple[bool, int]:
    """Like _rate_limited() but never records an event."""
    now = time.time()
    key = f"{bucket}:{hashlib.sha256(subject.encode('utf-8', errors='ignore')).hexdigest()[:32]}"
    with _AUTH_RATE_LIMIT_LOCK:
        entry = _AUTH_RATE_LIMITS.get(key) or {}
        events = [float(ts) for ts in entry.get("events", []) if now - float(ts) < window_seconds]
        if len(events) >= limit:
            return True, max(1, int(window_seconds - (now - min(events))))
        return False, 0


def _rate_limited(bucket: str, subject: str, limit: int, window_seconds: int) -> Tuple[bool, int]:
    now = time.time()
    key = f"{bucket}:{hashlib.sha256(subject.encode('utf-8', errors='ignore')).hexdigest()[:32]}"
    with _AUTH_RATE_LIMIT_LOCK:
        bounded_rate_key_store_sweep(_AUTH_RATE_LIMITS, now=now, max_age=3600, max_keys=8192)
        entry = _AUTH_RATE_LIMITS.setdefault(key, {"events": [], "updated": now})
        events = [float(ts) for ts in entry.get("events", []) if now - float(ts) < window_seconds]
        entry["updated"] = now
        if len(events) >= limit:
            retry_after = max(1, int(window_seconds - (now - min(events))))
            entry["events"] = events
            return True, retry_after
        events.append(now)
        entry["events"] = events[-limit:]
        return False, 0


def _rate_limit_response(retry_after: int):
    response = jsonify({"ok": False, "error": "Rate limit exceeded; retry later"})
    response.status_code = 429
    response.headers["Retry-After"] = str(max(1, int(retry_after or 1)))
    return response


def _same_origin_url(value: str) -> bool:
    if not value:
        return False
    try:
        parsed = urllib.parse.urlparse(value)
    except Exception:
        return False
    host = request.host.split("@")[-1].lower()
    return bool(parsed.scheme in {"http", "https"} and parsed.netloc.lower() == host)


def _explicit_authorization_header_present() -> bool:
    header = request.headers.get("Authorization", "").strip().lower()
    return header.startswith("bearer ") or header.startswith("basic ")


def _csrf_request_allowed() -> bool:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return True

    browser_same_origin = False
    origin = request.headers.get("Origin", "")
    if origin:
        if origin.strip().lower() == "null" or not _same_origin_url(origin):
            return False
        browser_same_origin = True

    referer = request.headers.get("Referer", "")
    if referer:
        if not _same_origin_url(referer):
            return False
        browser_same_origin = True

    fetch_site = request.headers.get("Sec-Fetch-Site", "").lower()
    if fetch_site:
        if fetch_site not in {"same-origin", "same-site", "none"}:
            return False
        browser_same_origin = True

    if _explicit_authorization_header_present():
        return True

    return browser_same_origin and request.headers.get("X-Beets-CSRF") == "1"


def _json_security_error(status: int, message: str):
    response = jsonify({"ok": False, "error": message})
    response.status_code = status
    if status == 401:
        response.headers["WWW-Authenticate"] = 'Basic realm="Beets Web Control", charset="UTF-8"'
    return response


_CONFIRMATION_REASON_MAX_LEN = 500


def _sanitize_confirmation_reason(value: Any) -> str:
    """Safe, bounded, single-line projection of a user-supplied
    confirmed-review confirmation reason. This is untrusted free text that
    flows into the transaction reason/metadata, the per-change reason, and
    job logs -- normalize newlines/tabs to spaces, redact anything that
    looks like a secret (Authorization/Cookie/api_key/password/URL
    credentials), collapse whitespace, and bound the length. Callers must
    use only this sanitized value everywhere and never store or log the
    raw one."""
    text = _s(value)
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", " ").replace("\t", " ")
    text = _redact_security_text(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > _CONFIRMATION_REASON_MAX_LEN:
        text = text[:_CONFIRMATION_REASON_MAX_LEN].rstrip()
    return text


def _is_public_endpoint() -> bool:
    endpoint = request.endpoint or ""
    method = request.method
    return (method, endpoint) in _AUTH_PUBLIC_ENDPOINTS


def _auth_failure_rate_limit_response():
    ip_limit, ip_window = _auth_ip_limit()
    auth_limited, auth_retry = _rate_limited(
        "auth",
        _rate_limit_subject(include_auth=False),
        ip_limit,
        ip_window,
    )
    if auth_limited:
        return _rate_limit_response(auth_retry)
    # The account-wide bucket only answers requests that actually tried a
    # password, so credential-less anonymous requests are not turned into
    # 429s for everyone once an attacker has exhausted it.
    if getattr(g, "_bwm_password_attempted", False):
        acct_limit, acct_window = _auth_account_limit()
        acct_limited, acct_retry = _rate_limit_peek("auth-account", _ACCOUNT_RATE_SUBJECT, acct_limit, acct_window)
        if acct_limited:
            return _rate_limit_response(acct_retry)
    return None


_INLINE_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)


def _inline_script_csp_hashes(html: Optional[str]) -> List[str]:
    if not html:
        return []
    hashes: List[str] = []
    for match in _INLINE_SCRIPT_RE.finditer(html):
        script_body = match.group(1)
        if not script_body.strip():
            continue
        digest = base64.b64encode(hashlib.sha256(script_body.encode("utf-8")).digest()).decode("ascii")
        hashes.append(f"'sha256-{digest}'")
    return list(dict.fromkeys(hashes))


def _content_security_policy(html: Optional[str] = None) -> str:
    script_src = " ".join(["'self'", *_inline_script_csp_hashes(html)])
    return (
        "default-src 'self'; "
        f"script-src {script_src}; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        "font-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'self'"
    )


def _client_ip_is_lan() -> bool:
    try:
        import ipaddress as _ipaddress
        ip = _ipaddress.ip_address(_request_client_identity())
        return bool(ip.is_private or ip.is_loopback)
    except Exception:
        return False


def _transaction_user_label() -> str:
    try:
        auth = request.authorization
        if auth and auth.username:
            return str(auth.username)
    except RuntimeError:
        pass
    return os.environ.get("BEETS_WEB_USERNAME", "admin").strip() or "operator"
