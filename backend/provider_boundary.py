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

Only ``confirmed`` and ``no_result`` are answers. Every other outcome means
"we could not ask" and must never be treated -- or cached -- as "no match".

``call_with_retry`` retries only ``rate_limited``, ``unavailable`` and
``transient_error``, a bounded number of times, honouring a provider's
Retry-After (capped). Messages are redacted before they are stored or logged.
"""

from __future__ import annotations

import enum
import re
import socket
import time
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


class ProviderOutcome(str, enum.Enum):
    CONFIRMED = "confirmed"
    NO_RESULT = "no_result"
    AMBIGUOUS = "ambiguous"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"
    AUTHENTICATION_ERROR = "authentication_error"
    TRANSIENT_ERROR = "transient_error"


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
    return ProviderOutcome.TRANSIENT_ERROR


def classify_exception(exc: BaseException) -> ProviderError:
    """Map a network exception onto a ProviderError (never "no result")."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, urllib.error.HTTPError):
        return ProviderError(classify_http(exc.code, exc.headers), f"HTTP {exc.code}", status_code=exc.code,
                             retry_after=_retry_after(exc.headers))
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return ProviderError(ProviderOutcome.TRANSIENT_ERROR, "timed out")
    if isinstance(exc, urllib.error.URLError):
        return ProviderError(ProviderOutcome.UNAVAILABLE, f"unreachable: {exc.reason}")
    if isinstance(exc, (ConnectionError, OSError)):
        return ProviderError(ProviderOutcome.TRANSIENT_ERROR, type(exc).__name__)
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
