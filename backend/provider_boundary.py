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

import datetime
import email.utils
import enum
import http.client
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, Optional

from backend.security import (OutboundPolicyError, allowlist_entry, implicit_rule_for_url, open_public_url,
                              trusted_service_endpoint)


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
                 retry_after: Optional[float] = None, provider: str = ""):
        super().__init__(redact(message))
        self.outcome, self.status_code, self.retry_after = outcome, status_code, retry_after
        self.provider = provider  # a POLICIES key when known; names the provider in API errors


# Fixed, user-facing provider names (API error text never carries exception text).
PROVIDER_DISPLAY_NAMES = {
    "musicbrainz": "MusicBrainz", "acoustid": "AcoustID", "discogs": "Discogs", "plex": "Plex",
    "lidarr": "Lidarr", "slskd": "slskd", "spotify": "Spotify", "listenbrainz": "ListenBrainz",
    "artwork": "The artwork source", "ai": "The AI provider", "qbittorrent": "qBittorrent",
    "ytdlp-po": "The yt-dlp PO token provider",
}


def unavailable_message(exc: BaseException) -> str:
    name = PROVIDER_DISPLAY_NAMES.get(getattr(exc, "provider", ""), "An external provider")
    return f"{name} is unavailable; try again later"


def _retry_after(headers: Any) -> Optional[float]:
    """Retry-After as seconds: delta-seconds or an HTTP-date (RFC 9110)."""
    try:
        raw = headers.get("Retry-After") if headers is not None else None
    except Exception:
        return None
    if raw in (None, ""):
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        when = email.utils.parsedate_to_datetime(str(raw))
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds())


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
        # The hint is a fixed sentence naming only host:port (F6).
        hint = getattr(exc, "hint", "")
        return ProviderError(ProviderOutcome.REJECTED,
                             "blocked by outbound URL policy" + (f"; {hint}" if hint else ""))
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


def _provider_answered(outcome: ProviderOutcome, status_code: Optional[int]) -> bool:
    """True when the provider itself answered: an answer, or a 4xx refusal
    of this particular request (a MusicBrainz 404, an AcoustID 400). Those
    say nothing about the provider's health. A REJECTED with no status code
    (TLS failure, outbound-policy block) is ours to fix and counts as a
    failure."""
    if outcome in ANSWERS:
        return True
    return outcome == ProviderOutcome.REJECTED and status_code is not None


def _record(provider: str, outcome: ProviderOutcome, *, attempts: int, status_code: Optional[int] = None,
            message: str = "", reason: Optional[str] = None) -> None:
    now = time.time()
    with _HEALTH_LOCK:
        row = _HEALTH.setdefault(provider, {"provider": provider, "calls": 0, "failures": 0, "retries": 0,
                                            "last_success_at": None, "last_failure_at": None})
        row["calls"] += 1
        row["retries"] += max(0, attempts - 1)
        row.update(last_outcome=outcome.value, last_status_code=status_code, last_attempts=attempts,
                   last_message=redact(message), last_reason=reason)
        if _provider_answered(outcome, status_code):
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
        reason = OUTBOUND_NOT_ALLOWLISTED if _not_allowlisted(exc) else None
        _record(provider, err.outcome, attempts=attempts, status_code=err.status_code, message=str(err),
                reason=reason)
        return False
    delay = err.retry_after if err.retry_after is not None else policy.base_backoff * (2 ** (attempts - 1))
    sleep(min(MAX_RETRY_AFTER_SECONDS, delay))
    return True


# What a failing provider or transport raises (see classify_exception);
# ValueError covers a malformed body (json.JSONDecodeError).
_PROVIDER_FAILURES = (ProviderError, OSError, http.client.HTTPException, OutboundPolicyError, ValueError)


@contextmanager
def _yielding(provider: str, response: Any, attempts: int) -> Iterator[Any]:
    """Yield the response; record the outcome only once the caller's block
    is done, so a body that fails mid-read (IncompleteRead, timeout,
    oversize, malformed) is recorded as the failure it is, not as CONFIRMED.
    The body is streamed by the caller, so such a failure is not retried."""
    status = getattr(response, "status", None)
    try:
        if hasattr(response, "__enter__"):
            with response as entered:
                yield entered
        else:
            yield response
    except GeneratorExit:
        raise
    except _PROVIDER_FAILURES as exc:
        # Only provider/transport failures count against the provider; a
        # bug in the caller's block (KeyError, ...) re-raises unrecorded.
        err = classify_exception(exc)
        _record(provider, err.outcome, attempts=attempts, status_code=err.status_code or status,
                message=f"response body: {err}")
        raise
    else:
        _record(provider, ProviderOutcome.CONFIRMED, attempts=attempts, status_code=status)
    finally:
        close = getattr(response, "close", None)
        if callable(close) and not hasattr(response, "__enter__"):
            close()


# -- the configured endpoint each operator-hosted provider trusts ---------------

#: provider -> the setting that names its endpoint. Each of these providers
#: implicitly trusts exactly the scheme, host and port of its own configured
#: URL, for its own calls only, and only when that URL resolves to private
#: (RFC 1918), CGNAT or IPv6 ULA addresses (security.implicit_rule_for_url).
#: Fixed public APIs (MusicBrainz, AcoustID, ...) need no trust.
CONFIGURED_URL_SETTINGS: Dict[str, str] = {
    "plex": "PLEX_URL",
    "lidarr": "LIDARR_URL",
    "slskd": "SLSKD_URL",
    "qbittorrent": "QBITTORRENT_URL",
    "ytdlp-po": "YTDLP_PO_PROVIDER_URL",
    "ai": "AI_BASE_URL",
}
#: The backend.app_runtime constant each service actually calls (read at
#: boot, with its *_FILE fallback and default), so the trusted endpoint is
#: always the one the service uses. AI reads AI_BASE_URL live; Plex
#: registers its own resolver (env, then the Beets config.yaml stanza).
_RUNTIME_URL_CONSTANTS = {"lidarr": "LIDARR_URL", "slskd": "SLSKD_URL", "qbittorrent": "QBIT_URL",
                          "ytdlp-po": "YTDLP_PO_PROVIDER_URL"}
_CONFIGURED_URL_RESOLVERS: Dict[str, Callable[[], str]] = {}


def register_configured_url(provider: str, resolver: Callable[[], str]) -> None:
    """Let the service that owns a provider's settings resolve its URL."""
    if provider not in CONFIGURED_URL_SETTINGS:
        raise ValueError(f"{provider!r} has no configured URL setting")
    _CONFIGURED_URL_RESOLVERS[provider] = resolver


def configured_url(provider: str) -> str:
    """The URL ``provider`` is configured to call, or "" (never raises)."""
    setting = CONFIGURED_URL_SETTINGS.get(provider)
    if not setting:
        return ""
    try:
        resolver = _CONFIGURED_URL_RESOLVERS.get(provider)
        if resolver is not None:
            return str(resolver() or "").strip()
        constant = _RUNTIME_URL_CONSTANTS.get(provider)
        # Looked up, never imported: importing app_runtime here would run the
        # process boot from a low-level module. Every running app has it.
        runtime = sys.modules.get("backend.app_runtime")
        if constant and runtime is not None:
            return str(getattr(runtime, constant, "") or "").strip()
        return (os.environ.get(setting) or "").strip()
    except Exception:
        return ""


def allowlist_hint(url: str, provider: Optional[str] = None) -> str:
    """A fixed, non-secret sentence telling the operator how to admit
    ``url``, e.g. "add 192.168.1.20:32400 to BEETS_OUTBOUND_ALLOWLIST or set
    PLEX_URL to it". Names only host:port (no userinfo, path, query or
    fragment); "" for a URL that has no allowlist entry."""
    entry = allowlist_entry(url)
    if not entry:
        return ""
    setting = CONFIGURED_URL_SETTINGS.get(provider or "")
    return f"add {entry} to BEETS_OUTBOUND_ALLOWLIST" + (f" or set {setting} to it" if setting else "")


#: Reason code for a request the outbound policy refused that an allowlist
#: entry (or setting the service's own URL) would admit.
OUTBOUND_NOT_ALLOWLISTED = "outbound_not_allowlisted"


def _not_allowlisted(exc: BaseException) -> bool:
    # Only a refusal an entry could fix: a DNS failure or a cloud-metadata
    # target is also an OutboundPolicyError, but carries no entry.
    return isinstance(exc, OutboundPolicyError) and bool(getattr(exc, "entry", ""))


def outbound_block(exc: BaseException, provider: str, url: Optional[str] = None) -> Optional[Dict[str, str]]:
    """User-facing details for a call ``provider`` could not make because the
    outbound policy refused ``url`` (default: the provider's configured URL),
    or None for any other failure.

    Returns ``{"reason", "error", "hint"}``. The text is built from fixed
    sentences and the target's host:port only (``allowlist_hint``), never
    from the exception text, so it is safe to return from an API."""
    if not _not_allowlisted(exc):
        return None
    configured = configured_url(provider)
    target = configured if url is None else url
    # Suggest setting the service URL only when it is not already that URL.
    same = bool(configured) and allowlist_entry(target) == allowlist_entry(configured)
    hint = allowlist_hint(target, None if same else provider)
    name = PROVIDER_DISPLAY_NAMES.get(provider, "The service")
    error = f"{name} is blocked by the outbound network policy."
    if hint:
        error += f" To allow it, {hint}."
    return {"reason": OUTBOUND_NOT_ALLOWLISTED, "error": error, "hint": hint}

def _request_url(request: Any) -> str:
    if isinstance(request, str):
        return request
    return str(getattr(request, "full_url", "") or "")


@contextmanager
def opened(provider: str, request: Any, *, timeout: Optional[float] = None, max_attempts: Optional[int] = None,
           sleep: Callable[[float], None] = time.sleep, trust_configured: bool = True) -> Iterator[Any]:
    """Open ``request`` at ``provider`` and yield the response (see module doc).

    For operator-configured endpoints and fixed provider APIs only: the
    connection is made by ``urllib.request.urlopen`` (the allowlist-aware,
    address-pinned ``backend.security.secure_urlopen`` once installed). A
    URL supplied by a user or by a provider response must use
    ``opened_public`` instead.

    The provider's own configured URL (``configured_url``) is trusted for
    this call when it resolves to private/CGNAT/ULA addresses. A caller that
    sends a URL it did not take from configuration (a setup probe testing a
    typed-in URL) passes ``trust_configured=False``.

    Raises the original exception after the last attempt; an
    OutboundPolicyError carries ``hint`` (``allowlist_hint``). ``max_attempts``
    overrides the provider's policy (a connectivity test passes 1)."""
    policy = policy_for(provider)
    limit = _attempt_limit(policy, max_attempts, _method_of(request))
    trusted = implicit_rule_for_url(configured_url(provider)) if trust_configured else None
    attempts = 0
    while True:
        attempts += 1
        try:
            with trusted_service_endpoint(trusted):
                if timeout is None:
                    response = urllib.request.urlopen(request)
                else:
                    response = urllib.request.urlopen(request, timeout=timeout)
            break
        except Exception as exc:  # classified, recorded, then retried or re-raised unchanged
            if isinstance(exc, OutboundPolicyError) and getattr(exc, "entry", ""):
                exc.hint = allowlist_hint(_request_url(request), provider)
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
