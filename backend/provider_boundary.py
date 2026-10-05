"""Typed outcomes, bounded retries and redaction for external providers (ARCH-006).

A provider call yields a ``ProviderResult`` whose ``outcome`` is one of:

* ``confirmed``            -- the provider answered with a match.
* ``no_result``            -- the provider answered and had nothing.
* ``ambiguous`` / ``conflict`` -- set by callers that interpret a confirmed
  answer (several equally good candidates / contradicting evidence).
* ``unavailable``          -- 5xx, missing local tool (e.g. fpcalc).
* ``rate_limited``         -- 429 or the provider's own throttling code.
* ``authentication_error`` -- 401/403 or a rejected API key.
* ``transient_error``      -- timeouts, connection resets, malformed replies.
* ``rejected``             -- the provider refused this request (a 4xx such
  as 400 or 404): asking again unchanged cannot help, so it is never
  retried. Whether a 404 means "no such record" is the caller's call.

Only ``confirmed`` and ``no_result`` are answers. Every other outcome means
"we could not ask" and must never be treated -- or cached -- as "no match".

``call_with_retry`` retries only ``rate_limited``, ``unavailable`` and
``transient_error``, a bounded number of times, honouring a provider's
Retry-After (capped). Messages are redacted before they are stored or logged.

``opened(provider, request, timeout=...)`` is the one way application code
opens an HTTP connection to a provider (``opened_public(provider, url, ...)``
when the URL came from a user or a provider response). It is a drop-in for
``urllib.request.urlopen`` used as a context manager: the same response
object, and on final failure the ORIGINAL exception, so a call site's own
error handling is unchanged. What it adds, uniformly for every provider:

* the provider's policy (``POLICIES``): how many attempts, which backoff;
* bounded retries of retryable outcomes -- only for requests that are safe
  to repeat (GET/HEAD), never for a POST;
* the classified outcome of every call in ``provider_health()``, redacted.
"""

from __future__ import annotations

import enum
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, Optional

from backend.security import OutboundPolicyError, open_public_url


class ProviderOutcome(str, enum.Enum):
    CONFIRMED = "confirmed"
    NO_RESULT = "no_result"
    AMBIGUOUS = "ambiguous"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"
    AUTHENTICATION_ERROR = "authentication_error"
    TRANSIENT_ERROR = "transient_error"
    REJECTED = "rejected"
    #: The provider needs a credential the operator has not configured. Like
    #: UNAVAILABLE it means "could not ask" -- never an answer, never cached,
    #: never "no match" -- but retrying cannot help until it is configured.
    NOT_CONFIGURED = "not_configured"


ANSWERS = frozenset({ProviderOutcome.CONFIRMED, ProviderOutcome.NO_RESULT,
                     ProviderOutcome.AMBIGUOUS, ProviderOutcome.CONFLICT})
RETRYABLE = frozenset({ProviderOutcome.RATE_LIMITED, ProviderOutcome.UNAVAILABLE, ProviderOutcome.TRANSIENT_ERROR})
MAX_RETRY_AFTER_SECONDS = 30.0

_SECRET_PATTERNS = [
    re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]{6,}", re.IGNORECASE),
    re.compile(r"((?:api[_\-]?key|client|token|password|secret|fingerprint)=)[^&\s'\"]+", re.IGNORECASE),
    re.compile(r"((?:api[_\-]?key|token|password|secret|authorization)\s*[:=]\s*['\"]?)[^'\"\s,}]{4,}", re.IGNORECASE),
]
_SENSITIVE_KEYS = frozenset({"api_key", "apikey", "token", "access_token", "secret", "password", "authorization",
                             "auth_token", "plex_token", "openai_api_key", "client", "fingerprint"})


def redact(value: Any) -> Any:
    """Scrub secrets from strings, dicts and lists (never mutates the input)."""
    if isinstance(value, str):
        for pattern in _SECRET_PATTERNS:
            value = pattern.sub(r"\1[REDACTED]", value)
        return value
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if str(k).lower() in _SENSITIVE_KEYS else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact(v) for v in value)
    return value


@dataclass
class ProviderResult:
    provider: str
    outcome: ProviderOutcome
    data: Any = None
    message: str = ""
    status_code: Optional[int] = None
    retry_after: Optional[float] = None
    attempts: int = 1
    from_cache: bool = False
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def answered(self) -> bool:
        return self.outcome in ANSWERS

    def to_dict(self) -> Dict[str, Any]:
        return {"provider": self.provider, "outcome": self.outcome.value, "message": redact(self.message),
                "status_code": self.status_code, "attempts": self.attempts, "from_cache": self.from_cache}


class ProviderError(Exception):
    """Raised inside a provider call to report a classified failure."""

    def __init__(self, outcome: ProviderOutcome, message: str = "", *, status_code: Optional[int] = None,
                 retry_after: Optional[float] = None):
        super().__init__(redact(message))
        self.outcome, self.status_code, self.retry_after = outcome, status_code, retry_after


def _retry_after(headers: Any) -> Optional[float]:
    try:
        raw = headers.get("Retry-After") if headers is not None else None
        return max(0.0, float(raw)) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def classify_http(code: int, headers: Any = None) -> ProviderOutcome:
    if code in (401, 403):
        return ProviderOutcome.AUTHENTICATION_ERROR
    if code == 429:
        return ProviderOutcome.RATE_LIMITED
    if code >= 500:
        return ProviderOutcome.UNAVAILABLE
    if code == 408:
        return ProviderOutcome.TRANSIENT_ERROR
    if 400 <= code < 500:
        return ProviderOutcome.REJECTED  # the request itself was refused: never retried
    return ProviderOutcome.TRANSIENT_ERROR


def _certificate_failure(exc: BaseException) -> bool:
    current: Optional[BaseException] = exc
    for _ in range(5):
        if current is None:
            return False
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        if isinstance(getattr(current, "reason", None), ssl.SSLCertVerificationError):
            return True
        current = current.__cause__ or current.__context__
    return False


def classify_exception(exc: BaseException) -> ProviderError:
    """Map a network exception onto a ProviderError (never "no result")."""
    if isinstance(exc, ProviderError):
        return exc
    if _certificate_failure(exc):
        # SEC-7: a certificate that does not verify will not verify on the
        # next attempt either; retrying only repeats the handshake (and, for
        # an interception attempt, the exposure). Final, never retried.
        return ProviderError(ProviderOutcome.REJECTED, "TLS certificate verification failed")
    if isinstance(exc, urllib.error.HTTPError):
        return ProviderError(classify_http(exc.code, exc.headers), f"HTTP {exc.code}", status_code=exc.code,
                             retry_after=_retry_after(exc.headers))
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return ProviderError(ProviderOutcome.TRANSIENT_ERROR, "timed out")
    if isinstance(exc, urllib.error.URLError):
        return ProviderError(ProviderOutcome.UNAVAILABLE, f"unreachable: {exc.reason}")
    if isinstance(exc, (ConnectionError, OSError)):
        return ProviderError(ProviderOutcome.TRANSIENT_ERROR, type(exc).__name__)
    if isinstance(exc, OutboundPolicyError):
        # The outbound URL policy refused the target: asking again cannot
        # help, and a retry would only repeat the DNS lookup and request.
        return ProviderError(ProviderOutcome.REJECTED, "blocked by outbound URL policy")
    if isinstance(exc, ValueError):
        return ProviderError(ProviderOutcome.TRANSIENT_ERROR, "malformed response")
    return ProviderError(ProviderOutcome.TRANSIENT_ERROR, type(exc).__name__)


def call_with_retry(provider: str, fn: Callable[[], ProviderResult], *, max_attempts: int = 3,
                    base_backoff: float = 1.0, sleep: Callable[[float], None] = time.sleep) -> ProviderResult:
    """Run ``fn`` (which returns a ProviderResult or raises), retrying only
    retryable outcomes, at most ``max_attempts`` times in total."""
    attempts = 0
    while True:
        attempts += 1
        try:
            result = fn()
            result.attempts = attempts
            return result
        except BaseException as exc:  # noqa: BLE001 -- classified, never swallowed as "no result"
            err = classify_exception(exc)
        if err.outcome not in RETRYABLE or attempts >= max_attempts:
            return ProviderResult(provider, err.outcome, message=str(err), status_code=err.status_code,
                                  retry_after=err.retry_after, attempts=attempts)
        delay = err.retry_after if err.retry_after is not None else base_backoff * (2 ** (attempts - 1))
        sleep(min(MAX_RETRY_AFTER_SECONDS, delay))


# -- one entry point for every provider ------------------------------------------

@dataclass(frozen=True)
class ProviderPolicy:
    """How a provider is called: total attempts for a repeatable request and
    the first backoff (doubled each retry, capped by MAX_RETRY_AFTER_SECONDS)."""
    max_attempts: int = 1
    base_backoff: float = 1.0


#: Public metadata services throttle and hiccup: a bounded retry is worth it.
#: Services on the operator's own network answer or do not: one retry.
#: The AI provider is only ever POSTed to, so it is never repeated.
POLICIES: Dict[str, ProviderPolicy] = {
    "musicbrainz": ProviderPolicy(3, 1.0),
    "acoustid": ProviderPolicy(2, 1.0),
    "discogs": ProviderPolicy(2, 1.0),
    "spotify": ProviderPolicy(2, 1.0),
    "artwork": ProviderPolicy(2, 0.5),
    "reference-url": ProviderPolicy(1),
    "plex": ProviderPolicy(2, 0.5),
    "lidarr": ProviderPolicy(2, 0.5),
    "slskd": ProviderPolicy(2, 0.5),
    "qbittorrent": ProviderPolicy(2, 0.5),
    "ytdlp-po": ProviderPolicy(1),
    "ai": ProviderPolicy(1),
}
_SAFE_METHODS = frozenset({"GET", "HEAD"})
_HEALTH_LOCK = threading.Lock()
_HEALTH: Dict[str, Dict[str, Any]] = {}


def policy_for(provider: str) -> ProviderPolicy:
    """The provider's policy. ``PROVIDER_MAX_ATTEMPTS`` (an integer) caps
    every provider's attempts -- 1 turns retries off."""
    policy = POLICIES.get(provider)
    if policy is None:
        raise ValueError(f"unknown provider {provider!r}; add it to provider_boundary.POLICIES")
    cap = os.environ.get("PROVIDER_MAX_ATTEMPTS", "").strip()
    if cap.isdigit() and int(cap) >= 1:
        return ProviderPolicy(min(policy.max_attempts, int(cap)), policy.base_backoff)
    return policy


def _record(provider: str, outcome: ProviderOutcome, *, attempts: int, status_code: Optional[int] = None,
            message: str = "") -> None:
    now = time.time()
    with _HEALTH_LOCK:
        row = _HEALTH.setdefault(provider, {"provider": provider, "calls": 0, "failures": 0, "retries": 0,
                                            "last_success_at": None, "last_failure_at": None})
        row["calls"] += 1
        row["retries"] += max(0, attempts - 1)
        row.update(last_outcome=outcome.value, last_status_code=status_code, last_attempts=attempts,
                   last_message=redact(message))
        if outcome == ProviderOutcome.CONFIRMED:
            row["last_success_at"] = now
        else:
            row["failures"] += 1
            row["last_failure_at"] = now


def provider_health() -> Dict[str, Dict[str, Any]]:
    """The last classified outcome per provider since this process started
    (redacted; no URLs, no keys)."""
    with _HEALTH_LOCK:
        known = {name: dict(row) for name, row in _HEALTH.items()}
    return {name: known.get(name, {"provider": name, "calls": 0, "failures": 0, "retries": 0,
                                   "last_outcome": None})
            for name in sorted(set(POLICIES) | set(known))}


def reset_provider_health() -> None:
    with _HEALTH_LOCK:
        _HEALTH.clear()


def _method_of(request: Any) -> str:
    if isinstance(request, str):
        return "GET"
    try:
        return str(request.get_method()).upper()
    except Exception:
        return "GET"


def _attempt_limit(policy: ProviderPolicy, max_attempts: Optional[int], method: str) -> int:
    limit = max(1, max_attempts if max_attempts is not None else policy.max_attempts)
    if method not in _SAFE_METHODS:
        limit = 1  # a POST is never repeated by the boundary
    return limit


def _after_failure(provider: str, policy: ProviderPolicy, exc: BaseException, *, attempts: int, limit: int,
                   sleep: Callable[[float], None]) -> bool:
    """Classify a failed attempt. Records it and returns False when it is
    final (the caller re-raises the original exception); otherwise sleeps
    the backoff and returns True (try again)."""
    err = classify_exception(exc)
    if err.outcome not in RETRYABLE or attempts >= limit:
        _record(provider, err.outcome, attempts=attempts, status_code=err.status_code, message=str(err))
        return False
    delay = err.retry_after if err.retry_after is not None else policy.base_backoff * (2 ** (attempts - 1))
    sleep(min(MAX_RETRY_AFTER_SECONDS, delay))
    return True


@contextmanager
def _yielding(provider: str, response: Any, attempts: int) -> Iterator[Any]:
    _record(provider, ProviderOutcome.CONFIRMED, attempts=attempts,
            status_code=getattr(response, "status", None))
    try:
        if hasattr(response, "__enter__"):
            with response as entered:
                yield entered
        else:
            yield response
    finally:
        close = getattr(response, "close", None)
        if callable(close) and not hasattr(response, "__enter__"):
            close()


@contextmanager
def opened(provider: str, request: Any, *, timeout: Optional[float] = None, max_attempts: Optional[int] = None,
           sleep: Callable[[float], None] = time.sleep) -> Iterator[Any]:
    """Open ``request`` at ``provider`` and yield the response (see module doc).

    For operator-configured endpoints and fixed provider APIs only: the
    connection is made by ``urllib.request.urlopen`` (the allowlist-aware
    ``backend.security.secure_urlopen`` once installed). A URL supplied by a
    user or by a provider response must use ``opened_public`` instead.

    Raises the original exception after the last attempt. ``max_attempts``
    overrides the provider's policy (a connectivity test passes 1)."""
    policy = policy_for(provider)
    limit = _attempt_limit(policy, max_attempts, _method_of(request))
    attempts = 0
    while True:
        attempts += 1
        try:
            if timeout is None:
                response = urllib.request.urlopen(request)
            else:
                response = urllib.request.urlopen(request, timeout=timeout)
            break
        except Exception as exc:  # classified, recorded, then retried or re-raised unchanged
            if not _after_failure(provider, policy, exc, attempts=attempts, limit=limit, sleep=sleep):
                raise
    with _yielding(provider, response, attempts) as entered:
        yield entered


@contextmanager
def opened_public(provider: str, url: str, *, timeout: Optional[float] = None,
                  headers: Optional[Dict[str, str]] = None, max_bytes: Optional[int] = None,
                  max_attempts: Optional[int] = None,
                  sleep: Callable[[float], None] = time.sleep) -> Iterator[Any]:
    """GET a URL supplied by a user or by a provider response, at ``provider``.

    Same policy, bounded retries, classification and health record as
    ``opened``, but the connection is made only by
    ``backend.security.open_public_url``: public addresses only,
    BEETS_OUTBOUND_ALLOWLIST ignored, the socket pinned to the validated
    address and every redirect hop re-validated (CodeQL #1350). There is
    deliberately no path from here to ``urlopen``."""
    policy = policy_for(provider)
    limit = _attempt_limit(policy, max_attempts, "GET")
    attempts = 0
    while True:
        attempts += 1
        try:
            response = open_public_url(url, headers=headers, timeout=timeout, max_bytes=max_bytes)
            break
        except Exception as exc:  # classified, recorded, then retried or re-raised unchanged
            if not _after_failure(provider, policy, exc, attempts=attempts, limit=limit, sleep=sleep):
                raise
    with _yielding(provider, response, attempts) as entered:
        yield entered
