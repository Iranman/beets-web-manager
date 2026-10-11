"""Security helpers for outbound requests and abuse controls."""
from __future__ import annotations

import contextvars
import functools
import http.client
import io
import ipaddress
import logging
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

LOG = logging.getLogger("beets_web.security")
_ALLOWED_SCHEMES = {"http", "https"}
_LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
_INTERNAL_NAME_SUFFIXES = (".localhost", ".local", ".lan", ".internal", ".home.arpa")
_INTERNAL_NAMES = {
    "docker",
    "host.docker.internal",
    "gateway.docker.internal",
    "kubernetes",
}
_METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("100.100.100.200"),
    ipaddress.ip_address("fd00:ec2::254"),
}
# Request headers that carry a credential and must never follow a redirect to
# a different origin. Includes every provider auth header this app sends
# (Plex X-Plex-Token, Lidarr/slskd X-Api-Key, qBittorrent cookie, Discogs/AI
# Authorization) -- SEC-1.
_SENSITIVE_REDIRECT_HEADERS = {
    "authorization",
    "cookie",
    "x-api-key",
    "x-auth-token",
    "x-forwarded-authorization",
    "proxy-authorization",
    "x-plex-token",
    "x-emby-token",
    "x-mediabrowser-token",
    "x-listenbrainz-token",
}
_DEFAULT_TIMEOUT = float(os.environ.get("BEETS_OUTBOUND_TIMEOUT_SECONDS", "20") or "20")
_DEFAULT_MAX_BYTES = int(os.environ.get("BEETS_OUTBOUND_MAX_RESPONSE_BYTES", str(20 * 1024 * 1024)) or str(20 * 1024 * 1024))
_DEFAULT_MAX_REDIRECTS = int(os.environ.get("BEETS_OUTBOUND_MAX_REDIRECTS", "5") or "5")
# Wall-clock budget for one user-supplied-URL fetch (every DNS answer tried,
# every redirect hop, and the whole body read). The per-operation timeout
# above only bounds a single socket wait, so a server dripping one byte per
# few seconds could otherwise hold a worker for hours (SEC-7).
def _total_timeout_setting() -> float:
    raw = os.environ.get("BEETS_OUTBOUND_TOTAL_TIMEOUT_SECONDS", "") or "60"
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if not (0 < value < float("inf")):  # also rejects nan
        LOG.warning("BEETS_OUTBOUND_TOTAL_TIMEOUT_SECONDS must be a positive number; using 60.")
        return 60.0
    return value


_DEFAULT_TOTAL_TIMEOUT = _total_timeout_setting()
_READ_CHUNK = 64 * 1024
_ORIGINAL_URLOPEN_ATTR = "_beets_original_urlopen"
_INSTALLED_ATTR = "_beets_secure_urlopen_installed"


class OutboundPolicyError(ValueError):
    """Raised when an outbound request violates the SSRF policy.

    ``entry`` is the exact ``host:port`` allowlist entry that would admit the
    refused target (no userinfo, path, query or fragment), or "" when no
    entry could (metadata, link-local, a public-looking failure ...).
    ``hint`` is a fixed, non-secret sentence naming that entry."""

    def __init__(self, message: str = "", *, entry: str = ""):
        super().__init__(message)
        self.entry = entry
        self.hint = f"add {entry} to BEETS_OUTBOUND_ALLOWLIST" if entry else ""


@dataclass(frozen=True)
class OutboundAllowRule:
    """One allowlist rule. Operator entries (BEETS_OUTBOUND_ALLOWLIST) match
    any scheme; an ``implicit`` rule is derived from a service's configured
    URL and also pins the scheme (see ``implicit_rule_for_url``)."""
    kind: str
    port: int
    host: str = ""
    ip: Optional[ipaddress._BaseAddress] = None
    network: Optional[ipaddress._BaseNetwork] = None
    scheme: str = ""
    implicit: bool = False


@dataclass(frozen=True)
class OutboundPolicy:
    allow_rules: Tuple[OutboundAllowRule, ...]
    max_response_bytes: int = _DEFAULT_MAX_BYTES
    max_redirects: int = _DEFAULT_MAX_REDIRECTS
    timeout_seconds: float = _DEFAULT_TIMEOUT


def _clean_host(value: str) -> str:
    return (value or "").strip().rstrip(".").lower()


_ALLOWLIST_HOSTNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?)*$")


def _parse_host_port(raw: str) -> Tuple[str, int]:
    """Split one allowlist entry into (host, port).

    Accepted forms: ``host:port``, ``IPv4:port``, ``IPv4-CIDR:port``,
    ``[IPv6]:port`` and ``[IPv6-CIDR]:port``. The port is always the text
    after the last colon outside brackets, so a CIDR's ``/prefix`` is never
    mistaken for a URL path (F6). Every malformed entry raises
    OutboundPolicyError."""
    value = (raw or "").strip()
    if not value or "*" in value:
        raise OutboundPolicyError("outbound allowlist entries must be exact host/IP/CIDR plus port")
    if value.startswith("["):
        close = value.find("]")
        if close < 0:
            raise OutboundPolicyError("outbound allowlist IPv6 entry is missing ']'")
        host, rest = value[1:close], value[close + 1:]
        if not rest.startswith(":"):
            raise OutboundPolicyError("outbound allowlist entries must include an explicit port")
        port_text = rest[1:]
    else:
        host, sep, port_text = value.rpartition(":")
        if not sep or not host:
            raise OutboundPolicyError("outbound allowlist entries must include an explicit port")
        if ":" in host:
            raise OutboundPolicyError("outbound allowlist IPv6 entries must be bracketed, e.g. [fd00::1]:8080")
    if not port_text.isdigit():
        raise OutboundPolicyError("outbound allowlist port must be a number")
    port = int(port_text)
    if port < 1 or port > 65535:
        raise OutboundPolicyError("outbound allowlist port is out of range")
    host = _clean_host(host)
    if not host:
        raise OutboundPolicyError("outbound allowlist entries must include a host")
    return host, port


DEFAULT_OUTBOUND_ALLOWLIST = "127.0.0.1:8337,localhost:8337,beets:8337"

# The local-network address floor. A service URL the operator configured
# (PLEX_URL, LIDARR_URL, ...) is trusted only when every address it resolves
# to lies in one of these: RFC 1918, CGNAT/shared (RFC 6598, e.g. Tailscale)
# and IPv6 ULA. Loopback is added only for explicit operator entries.
_PRIVATE_FLOOR = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7"))
_LOOPBACK_NETS = tuple(ipaddress.ip_network(n) for n in ("127.0.0.0/8", "::1/128"))


def _network_within_floor(network: ipaddress._BaseNetwork) -> bool:
    """True when a CIDR entry lies wholly inside the private floor or
    loopback. Anything wider (0.0.0.0/0, ::/0, a range spanning public or
    link-local space) is a catch-all and is never honoured."""
    return any(network.version == base.version and network.subnet_of(base)  # type: ignore[arg-type]
               for base in _PRIVATE_FLOOR + _LOOPBACK_NETS)


def _parse_allowlist(value: str) -> Tuple[Tuple[OutboundAllowRule, ...], Tuple[str, ...]]:
    """(rules, ignored catch-all CIDR entries). Malformed entries raise."""
    rules: List[OutboundAllowRule] = []
    ignored: List[str] = []
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        host, port = _parse_host_port(item)
        if "/" in host:
            try:
                network = ipaddress.ip_network(host, strict=False)
            except ValueError as exc:
                raise OutboundPolicyError("invalid outbound allowlist CIDR") from exc
            if not _network_within_floor(network):
                ignored.append(item)
                continue
            rules.append(OutboundAllowRule(kind="network", port=port, network=network))
            continue
        try:
            rules.append(OutboundAllowRule(kind="ip", port=port, ip=ipaddress.ip_address(host)))
            continue
        except ValueError:
            pass
        if not _ALLOWLIST_HOSTNAME_RE.match(host):
            raise OutboundPolicyError("invalid outbound allowlist host")
        rules.append(OutboundAllowRule(kind="host", port=port, host=host))
    return tuple(rules), tuple(ignored)


def _allowlist_setting() -> str:
    return os.environ.get("BEETS_OUTBOUND_ALLOWLIST", DEFAULT_OUTBOUND_ALLOWLIST)


def parse_outbound_allowlist(raw: Optional[str] = None) -> Tuple[OutboundAllowRule, ...]:
    """Parse BEETS_OUTBOUND_ALLOWLIST (operator-only: Compose/Docker env).

    Entries must be exact and port-scoped, for example:
      192.168.1.100:32400,lidarr:8686,10.0.0.0/24:8080
    Wildcards and suffix matching are deliberately unsupported. A CIDR entry
    wider than the private/CGNAT/ULA/loopback ranges (0.0.0.0/0 and the
    like) is ignored and reported at startup (``outbound_allowlist_ignored``).
    """
    return _parse_allowlist(_allowlist_setting() if raw is None else raw)[0]


def outbound_allowlist_ignored(raw: Optional[str] = None) -> Tuple[str, ...]:
    """Catch-all CIDR entries that are ignored. Never raises."""
    try:
        return _parse_allowlist(_allowlist_setting() if raw is None else raw)[1]
    except OutboundPolicyError:
        return ()


def outbound_allowlist_problem(raw: Optional[str] = None) -> Optional[str]:
    """A human-readable description of what is wrong with
    BEETS_OUTBOUND_ALLOWLIST, or None when it parses. Never raises."""
    try:
        parse_outbound_allowlist(raw)
        return None
    except OutboundPolicyError as exc:
        return str(exc)


def current_outbound_policy() -> OutboundPolicy:
    return OutboundPolicy(allow_rules=parse_outbound_allowlist())


def _url_port(parsed: urllib.parse.SplitResult) -> int:
    if parsed.port is not None:
        return int(parsed.port)
    return 443 if parsed.scheme.lower() == "https" else 80


def _origin(value: str) -> Tuple[str, str, int]:
    parsed = urllib.parse.urlsplit(value)
    return (parsed.scheme.lower(), _clean_host(parsed.hostname or ""), _url_port(parsed))


def same_endpoint_url(supplied: str, configured: str) -> bool:
    """True when two service base URLs name the same endpoint: same scheme,
    host (case-insensitive), effective port and path (ignoring a trailing
    slash). Credentials, query and fragment never make two URLs equal."""
    try:
        a = urllib.parse.urlsplit(str(supplied or "").strip())
        b = urllib.parse.urlsplit(str(configured or "").strip())
        if not a.scheme or not b.scheme or not a.hostname or not b.hostname:
            return False
        if a.username or a.password or a.query or a.fragment:
            return False
        return (
            a.scheme.lower() == b.scheme.lower()
            and _clean_host(a.hostname) == _clean_host(b.hostname)
            and _url_port(a) == _url_port(b)
            and (a.path or "/").rstrip("/") == (b.path or "/").rstrip("/")
        )
    except ValueError:
        return False


def redact_url_for_log(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        netloc = host
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path[:120], "", ""))
    except Exception:
        return "<invalid-url>"


def _host_is_local_or_internal(host: str) -> bool:
    clean = _clean_host(host)
    if clean in _LOCAL_NAMES or clean in _INTERNAL_NAMES:
        return True
    if clean.endswith(_INTERNAL_NAME_SUFFIXES):
        return True
    if "." not in clean and ":" not in clean:
        return True
    return False


def _resolve_host(host: str, port: int) -> Tuple[ipaddress._BaseAddress, ...]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OutboundPolicyError("outbound host could not be resolved") from exc
    addresses = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        raw_ip = sockaddr[0]
        try:
            addresses.append(ipaddress.ip_address(raw_ip))
        except ValueError:
            raise OutboundPolicyError("outbound host resolved to an invalid address")
    if not addresses:
        raise OutboundPolicyError("outbound host did not resolve to an address")
    return tuple(dict.fromkeys(addresses))


def _address_is_prohibited(ip: ipaddress._BaseAddress) -> bool:
    """Operator-path policy (BA-4/SEC-6): anything that is not a globally
    routable unicast address -- private, loopback, link-local, CGNAT/shared
    (100.64/10, e.g. Tailscale), benchmarking, documentation, reserved, and
    IPv6 forms embedding such an IPv4 -- needs an allowlist rule."""
    return not address_is_public(ip)


def _plain_address(ip: ipaddress._BaseAddress) -> ipaddress._BaseAddress:
    """An IPv4-mapped IPv6 address (::ffff:a.b.c.d) as the IPv4 it maps."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _is_metadata_address(ip: ipaddress._BaseAddress) -> bool:
    """Cloud-metadata endpoints, also in any IPv6 form embedding one."""
    return ip in _METADATA_IPS or _embedded_ipv4(ip) in _METADATA_IPS


def address_in_private_floor(ip: ipaddress._BaseAddress, *, allow_loopback: bool = False) -> bool:
    """True for RFC 1918, CGNAT (100.64/10) and IPv6 ULA addresses (and, with
    ``allow_loopback``, loopback) -- never a cloud-metadata address, even
    one inside those ranges (100.100.100.200)."""
    if _is_metadata_address(ip):
        return False
    plain = _plain_address(ip)
    nets = _PRIVATE_FLOOR + (_LOOPBACK_NETS if allow_loopback else ())
    return any(plain.version == net.version and plain in net for net in nets)


def _allow_rule_matches(rule: OutboundAllowRule, host: str, port: int, addresses: Tuple[ipaddress._BaseAddress, ...],
                        scheme: str = "") -> bool:
    """Whether ``rule`` admits this target. Every rule also requires every
    resolved address to be on the local-network floor (private, CGNAT, ULA;
    loopback too for an explicit operator entry), so no entry -- host,
    IP or CIDR -- can ever admit metadata, link-local, unspecified,
    multicast or reserved space (F3)."""
    if rule.port != port or (rule.scheme and rule.scheme != scheme):
        return False
    if not addresses or not all(address_in_private_floor(a, allow_loopback=not rule.implicit) for a in addresses):
        return False
    clean_host = _clean_host(host)
    if rule.kind == "host":
        return clean_host == rule.host
    if rule.kind == "ip" and rule.ip is not None:
        try:
            host_ip = ipaddress.ip_address(clean_host)
        except ValueError:
            return False
        return host_ip == rule.ip
    if rule.kind == "network" and rule.network is not None:
        return all(_plain_address(addr) in rule.network for addr in addresses)
    return False


def _is_allowlisted(host: str, port: int, addresses: Tuple[ipaddress._BaseAddress, ...], policy: OutboundPolicy,
                    scheme: str = "", trusted: Optional[OutboundAllowRule] = None) -> bool:
    rules = policy.allow_rules + ((trusted,) if trusted is not None else ())
    return any(_allow_rule_matches(rule, host, port, addresses, scheme) for rule in rules)


def allowlist_entry(url: str) -> str:
    """The exact ``host:port`` BEETS_OUTBOUND_ALLOWLIST entry for ``url``
    (IPv6 bracketed; userinfo, path, query and fragment dropped), or ""
    for a URL that has no such entry. Never raises."""
    try:
        parsed = urllib.parse.urlsplit(str(url or "").strip())
        host = _clean_host(parsed.hostname or "")
        if parsed.scheme.lower() not in _ALLOWED_SCHEMES or not host or any(ord(ch) <= 32 for ch in host):
            return ""
        port = _url_port(parsed)
    except ValueError:
        return ""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def implicit_rule_for_url(url: str) -> Optional[OutboundAllowRule]:
    """The one exact scheme+host+port rule a service's configured URL
    (PLEX_URL, LIDARR_URL, ...) earns for that service's own calls, or None
    for a blank, malformed or credential-bearing URL. The rule only ever
    admits addresses on the private floor (never loopback or metadata)."""
    try:
        parsed = urllib.parse.urlsplit(str(url or "").strip())
        scheme = parsed.scheme.lower()
        host = _clean_host(parsed.hostname or "")
        if scheme not in _ALLOWED_SCHEMES or not host or parsed.username or parsed.password:
            return None
        if any(ord(ch) <= 32 for ch in host):
            return None
        port = _url_port(parsed)
    except ValueError:
        return None
    try:
        return OutboundAllowRule(kind="ip", port=port, ip=ipaddress.ip_address(host), scheme=scheme, implicit=True)
    except ValueError:
        pass
    if not _ALLOWLIST_HOSTNAME_RE.match(host):
        return None
    return OutboundAllowRule(kind="host", port=port, host=host, scheme=scheme, implicit=True)


# The implicit rule of the provider call in progress (provider_boundary.opened
# sets it around exactly one urlopen); a context variable, so it never leaks
# into another thread's or another provider's request.
_TRUSTED_RULE: "contextvars.ContextVar[Optional[OutboundAllowRule]]" = contextvars.ContextVar(
    "beets_trusted_outbound_rule", default=None)


@contextmanager
def trusted_service_endpoint(rule: Optional[OutboundAllowRule]) -> Iterator[None]:
    """Admit ``rule`` (from ``implicit_rule_for_url``) for the operator-path
    requests made inside this block, and for nothing else."""
    token = _TRUSTED_RULE.set(rule)
    try:
        yield
    finally:
        _TRUSTED_RULE.reset(token)


def _check_operator_target(url: str, policy: OutboundPolicy,
                           trusted: Optional[OutboundAllowRule] = None) -> Tuple[ipaddress._BaseAddress, ...]:
    """Validate an operator-path URL and return the addresses it may be
    connected to. The caller connects only to these (no second lookup)."""
    parsed = urllib.parse.urlsplit(str(url or ""))
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise OutboundPolicyError("outbound URL scheme is not allowed")
    if parsed.username or parsed.password:
        raise OutboundPolicyError("outbound URL credentials are not allowed")
    host = _clean_host(parsed.hostname or "")
    if not host or any(ord(ch) <= 32 for ch in host):
        raise OutboundPolicyError("outbound URL host is invalid")
    try:
        port = _url_port(parsed)
    except ValueError as exc:
        raise OutboundPolicyError("outbound URL port is invalid") from exc
    try:
        addresses = _resolve_host(host, port)
    except OutboundPolicyError:
        LOG.warning("blocked outbound request to unresolved host: %s", redact_url_for_log(url))
        raise
    if any(_is_metadata_address(addr) for addr in addresses):
        LOG.warning("blocked outbound request to a cloud-metadata address: %s", redact_url_for_log(url))
        raise OutboundPolicyError("outbound host resolves to a cloud-metadata address")
    if _is_allowlisted(host, port, addresses, policy, scheme, trusted):
        return addresses
    # An entry can only help when every address is on the floor (loopback
    # included: explicit entries may name it).
    entry = allowlist_entry(url) if all(address_in_private_floor(a, allow_loopback=True) for a in addresses) else ""
    if _host_is_local_or_internal(host):
        LOG.warning("blocked outbound request to internal host: %s", redact_url_for_log(url))
        raise OutboundPolicyError("outbound host is internal and not allowlisted", entry=entry)
    if not all(address_is_public(addr) for addr in addresses):
        LOG.warning("blocked outbound request to prohibited address: %s", redact_url_for_log(url))
        raise OutboundPolicyError("outbound host resolves to a prohibited address", entry=entry)
    return addresses


def validate_outbound_url(url: str, *, policy: Optional[OutboundPolicy] = None,
                          trusted: Optional[OutboundAllowRule] = None) -> None:
    """Raise OutboundPolicyError unless the operator path may reach ``url``.
    A check only: the connection itself is made by ``secure_urlopen``,
    which validates and pins in one step."""
    _check_operator_target(url, policy or current_outbound_policy(), trusted)


def _request_url(req_or_url: Any) -> str:
    if isinstance(req_or_url, urllib.request.Request):
        return req_or_url.full_url
    return str(req_or_url or "")


def strip_cross_origin_sensitive_headers(req: urllib.request.Request, old_url: str, new_url: str) -> urllib.request.Request:
    if _origin(old_url) == _origin(new_url):
        return req
    for store in (req.headers, req.unredirected_hdrs):
        for key in list(store.keys()):
            if key.lower() in _SENSITIVE_REDIRECT_HEADERS:
                del store[key]
    return req


def rule_names_origin(rule: OutboundAllowRule, url: str) -> bool:
    """True when ``url``'s scheme, host and port are exactly ``rule``'s."""
    try:
        scheme, host, port = _origin(url)
    except ValueError:
        return False
    if port != rule.port or (rule.scheme and scheme != rule.scheme):
        return False
    if rule.kind == "host":
        return host == rule.host
    try:
        return rule.ip is not None and ipaddress.ip_address(host) == rule.ip
    except ValueError:
        return False


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-checks every redirect hop (the pinned handlers below also re-resolve,
    re-check and re-pin it before connecting). A request admitted by a
    service's implicit rule may only be redirected within that same origin."""

    def __init__(self, policy: Optional[OutboundPolicy] = None, trusted: Optional[OutboundAllowRule] = None):
        self.policy = policy or current_outbound_policy()
        self.trusted = trusted
        self.max_redirections = self.policy.max_redirects
        self.max_repeats = self.policy.max_redirects
        super().__init__()

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if (self.trusted is not None and rule_names_origin(self.trusted, req.full_url)
                and _origin(newurl) != _origin(req.full_url)):
            LOG.warning("blocked cross-origin redirect from a configured service URL: %s",
                        redact_url_for_log(req.full_url))
            raise OutboundPolicyError("cross-origin redirect from a configured service URL is refused")
        validate_outbound_url(newurl, policy=self.policy, trusted=self.trusted)
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None:
            strip_cross_origin_sensitive_headers(new_req, req.full_url, newurl)
        return new_req


class _PinnedConnectionMixin:
    """Connect to the validated addresses only (tried in resolver order) --
    never to whatever a second DNS lookup of ``host`` would return (F2). The
    original hostname stays ``self.host``, so urllib's Host header, TLS SNI
    and certificate verification all use it."""

    def __init__(self, host, port=None, *, bwm_addresses: Tuple[str, ...] = (), **kwargs):
        super().__init__(host, port, **kwargs)  # type: ignore[call-arg]
        self._bwm_addresses = tuple(bwm_addresses)
        self._create_connection = self._bwm_create_connection

    def _bwm_create_connection(self, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
        last_exc: Optional[OSError] = None
        for ip in self._bwm_addresses:
            try:
                return socket.create_connection((ip, address[1]), timeout, source_address)
            except OSError as exc:
                last_exc = exc
        raise last_exc or OSError("no validated address to connect to")


class _OperatorHTTPConnection(_PinnedConnectionMixin, http.client.HTTPConnection):
    pass


class _OperatorHTTPSConnection(_PinnedConnectionMixin, http.client.HTTPSConnection):
    pass


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, policy: OutboundPolicy, trusted: Optional[OutboundAllowRule]):
        super().__init__()
        self._policy, self._trusted = policy, trusted

    def http_open(self, req):
        addresses = tuple(str(a) for a in _check_operator_target(req.full_url, self._policy, self._trusted))
        return self.do_open(functools.partial(_OperatorHTTPConnection, bwm_addresses=addresses), req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, policy: OutboundPolicy, trusted: Optional[OutboundAllowRule], context=None):
        super().__init__(context=context)
        self._policy, self._trusted = policy, trusted

    def https_open(self, req):
        addresses = tuple(str(a) for a in _check_operator_target(req.full_url, self._policy, self._trusted))
        return self.do_open(functools.partial(_OperatorHTTPSConnection, bwm_addresses=addresses), req,
                            context=self._context)


class OutboundDeadlineExceeded(TimeoutError):
    """A user-supplied-URL fetch ran past its total wall-clock budget."""


class LimitedHTTPResponse:
    def __init__(self, response: Any, max_bytes: int, *, deadline: Optional[float] = None,
                 op_timeout: Optional[float] = None):
        self._response = response
        self._max_bytes = max(1, int(max_bytes or _DEFAULT_MAX_BYTES))
        self._read = 0
        self._deadline = deadline
        self._op_timeout = op_timeout

    def _remaining_time(self) -> float:
        remaining = self._deadline - time.monotonic()  # type: ignore[operator]
        if remaining <= 0:
            raise OutboundDeadlineExceeded("outbound request exceeded its total time limit")
        return remaining

    def _read_raw(self, want: int) -> bytes:
        if self._deadline is None:
            return self._response.read(want)
        # Read in bounded chunks, re-checking the deadline and shrinking the
        # socket timeout to what is left, so no single wait can overrun it.
        sock = getattr(self._response, "_bwm_sock", None)
        parts: List[bytes] = []
        got = 0
        while got < want:
            remaining = self._remaining_time()
            if sock is not None:
                limit = remaining if self._op_timeout is None else min(remaining, self._op_timeout)
                try:
                    sock.settimeout(max(0.05, limit))
                except OSError:
                    pass
            # read1() returns after at most one socket read, so a dripping
            # server cannot keep one call alive past the deadline.
            reader = getattr(self._response, "read1", None) or self._response.read
            chunk = reader(min(_READ_CHUNK, want - got))
            if not chunk:
                break
            parts.append(chunk)
            got += len(chunk)
        self._remaining_time()
        return b"".join(parts)

    def read(self, amt: Optional[int] = None) -> bytes:
        remaining = self._max_bytes - self._read
        if remaining < 0:
            raise OutboundPolicyError("outbound response exceeded size limit")
        if amt is None or amt < 0:
            data = self._read_raw(remaining + 1)
        else:
            data = self._read_raw(min(int(amt), remaining + 1))
        self._read += len(data or b"")
        if self._read > self._max_bytes:
            raise OutboundPolicyError("outbound response exceeded size limit")
        return data

    def __enter__(self):
        self._response.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._response.__exit__(exc_type, exc, tb)

    def __iter__(self):
        return iter(self._response)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)


def secure_urlopen(url, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, *, cafile=None, capath=None, cadefault=False, context=None):
    """The operator path (installed as ``urllib.request.urlopen``).

    Each hop is resolved once, every answer checked, and the socket connected
    to a checked address (no rebinding window, F2). Admitted: public
    addresses; BEETS_OUTBOUND_ALLOWLIST entries; and, inside
    ``trusted_service_endpoint``, the calling service's own configured URL
    when it resolves to the private floor. HTTP(S)_PROXY is not used: a
    proxy would make the connection to an address we never checked."""
    policy = current_outbound_policy()
    trusted = _TRUSTED_RULE.get()
    target = _request_url(url)
    validate_outbound_url(target, policy=policy, trusted=trusted)
    handlers: List[Any] = [
        urllib.request.ProxyHandler({}),
        SafeRedirectHandler(policy, trusted),
        _PinnedHTTPHandler(policy, trusted),
        _PinnedHTTPSHandler(policy, trusted, context=context),
    ]
    opener = urllib.request.build_opener(*handlers)
    effective_timeout = policy.timeout_seconds if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout
    response = opener.open(url, data=data, timeout=effective_timeout)
    return LimitedHTTPResponse(response, policy.max_response_bytes)


def install_secure_urllib() -> None:
    if getattr(urllib.request, _INSTALLED_ATTR, False):
        return
    problem = outbound_allowlist_problem()
    if problem:
        # Validated once at startup so a typo is reported clearly instead of
        # surfacing later as unrelated provider failures. Not fatal: every
        # outbound request still fails closed (OutboundPolicyError) until it
        # is fixed, and the Settings page must stay reachable to fix it.
        LOG.error(
            "BEETS_OUTBOUND_ALLOWLIST is invalid (%s); all outbound requests to "
            "operator-configured services will be refused until it is fixed. "
            "Use comma-separated host:port, IP:port, CIDR:port or [IPv6]:port entries.",
            problem,
        )
    for entry in outbound_allowlist_ignored():
        LOG.error(
            "BEETS_OUTBOUND_ALLOWLIST entry %s is ignored: a CIDR entry must lie inside the private "
            "(10/8, 172.16/12, 192.168/16), CGNAT (100.64/10), IPv6 ULA (fc00::/7) or loopback ranges.",
            entry,
        )
    setattr(urllib.request, _ORIGINAL_URLOPEN_ATTR, urllib.request.urlopen)
    urllib.request.urlopen = secure_urlopen
    setattr(urllib.request, _INSTALLED_ATTR, True)


# -- user-supplied URLs: public internet only, connection pinned --------------
#
# validate_outbound_url()/secure_urlopen() above protect *operator-configured*
# endpoints (Plex, Lidarr, Beets ...): they honour BEETS_OUTBOUND_ALLOWLIST and
# let urllib re-resolve the host at connect time. Neither is safe for a URL an
# API caller typed in: the allowlist would let it reach the operator's internal
# services, and the second DNS lookup lets a rebinding resolver answer a public
# address to the check and an internal one to the connect. open_public_url()
# therefore resolves once, requires every answer to be globally routable,
# never consults the allowlist, and connects the socket to the exact address
# it validated (TLS still verifies the certificate for the original hostname).
# Every redirect hop is re-resolved, re-validated and re-pinned the same way.

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


@dataclass(frozen=True)
class PinnedTarget:
    """A validated public request target. ``address`` is the IP the socket
    connects to; ``host`` is only used for the Host header, SNI and the TLS
    certificate check. ``addresses`` holds every validated DNS answer, in
    resolver order, so a dead first answer can fall back to the next one --
    each of them already passed the public-address check."""
    scheme: str
    host: str
    port: int
    address: str
    path: str
    addresses: Tuple[str, ...] = ()

    @property
    def host_header(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        default = 443 if self.scheme == "https" else 80
        return host if self.port == default else f"{host}:{self.port}"


def _embedded_ipv4(ip: ipaddress._BaseAddress) -> Optional[ipaddress.IPv4Address]:
    if not isinstance(ip, ipaddress.IPv6Address):
        return None
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    if ip in _NAT64_PREFIX:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def address_is_public(ip: ipaddress._BaseAddress) -> bool:
    """True only for a globally routable unicast address. Stricter than
    _address_is_prohibited(): also rejects shared/CGNAT space (100.64/10),
    benchmarking, documentation and any IPv6 form embedding a non-public
    IPv4 address (IPv4-mapped, 6to4, Teredo, NAT64)."""
    if ip in _METADATA_IPS or not ip.is_global or ip.is_multicast:
        return False
    embedded = _embedded_ipv4(ip)
    if embedded is not None and (embedded in _METADATA_IPS or not embedded.is_global or embedded.is_multicast):
        return False
    return True


def resolve_public_target(url: str) -> PinnedTarget:
    """Validate a user-supplied URL and pin it to one resolved public address.

    Raises OutboundPolicyError for a non-http(s) scheme, embedded credentials,
    an internal-looking hostname, an unresolvable host, or when *any* DNS
    answer is not a public address. BEETS_OUTBOUND_ALLOWLIST is deliberately
    ignored."""
    parsed = urllib.parse.urlsplit(str(url or ""))
    raw_scheme = parsed.scheme.lower()
    if raw_scheme == "https":
        scheme = "https"
    elif raw_scheme == "http":
        scheme = "http"
    else:
        raise OutboundPolicyError("outbound URL scheme is not allowed")
    if parsed.username or parsed.password:
        raise OutboundPolicyError("outbound URL credentials are not allowed")
    host = _clean_host(parsed.hostname or "")
    if not host or any(ord(ch) <= 32 for ch in host):
        raise OutboundPolicyError("outbound URL host is invalid")
    try:
        port = _url_port(parsed)
    except ValueError as exc:
        raise OutboundPolicyError("outbound URL port is invalid") from exc
    if _host_is_local_or_internal(host):
        LOG.warning("blocked user-supplied URL to internal host: %s", redact_url_for_log(url))
        raise OutboundPolicyError("outbound host is internal")
    addresses = _resolve_host(host, port)
    if not all(address_is_public(addr) for addr in addresses):
        LOG.warning("blocked user-supplied URL resolving to a non-public address: %s", redact_url_for_log(url))
        raise OutboundPolicyError("outbound host resolves to a prohibited address")
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    if any(ord(ch) <= 32 or ord(ch) == 127 for ch in path):
        raise OutboundPolicyError("outbound URL path is invalid")
    validated = tuple(str(addr) for addr in addresses)
    return PinnedTarget(scheme=scheme, host=host, port=port, address=validated[0], path=path, addresses=validated)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a pinned IP while verifying the certificate for ``server_hostname``."""

    def __init__(self, address: str, port: int, *, server_hostname: str, timeout: float,
                 context: ssl.SSLContext):
        super().__init__(address, port, timeout=timeout, context=context)
        self._pinned_server_hostname = server_hostname

    def connect(self) -> None:
        http.client.HTTPConnection.connect(self)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._pinned_server_hostname)


def _send_pinned(target: PinnedTarget, headers: Mapping[str, str], timeout: float) -> http.client.HTTPResponse:
    """One GET to the pinned address. No proxy, no automatic redirect."""
    conn: http.client.HTTPConnection
    if target.scheme == "https":
        conn = _PinnedHTTPSConnection(target.address, target.port, server_hostname=target.host,
                                      timeout=timeout, context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(target.address, target.port, timeout=timeout)
    send_headers = {k: v for k, v in headers.items() if k.lower() not in {"host", "connection"}}
    send_headers.update({"Host": target.host_header, "Connection": "close", "Accept-Encoding": "identity"})
    try:
        conn.request("GET", target.path, headers=send_headers)
        response = conn.getresponse()
        response._bwm_sock = conn.sock  # type: ignore[attr-defined]  # lets reads shrink the timeout
        return response
    except BaseException:
        conn.close()
        raise


def _is_certificate_error(exc: BaseException) -> bool:
    seen = 0
    current: Optional[BaseException] = exc
    while current is not None and seen < 5:
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        reason = getattr(current, "reason", None)
        if isinstance(reason, ssl.SSLCertVerificationError):
            return True
        current = current.__cause__ or current.__context__
        seen += 1
    return False


def _send_pinned_any(target: PinnedTarget, headers: Mapping[str, str], timeout: float,
                     deadline: float) -> http.client.HTTPResponse:
    """Try each validated address in turn (SEC-7/IA-09). Only a connection
    failure moves on to the next address; a TLS certificate failure is
    final (another address of the same name will not fix it) and an HTTP
    response of any status is returned as-is."""
    candidates = target.addresses or (target.address,)
    last_exc: Optional[BaseException] = None
    for address in candidates:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OutboundDeadlineExceeded("outbound request exceeded its total time limit")
        attempt = target if address == target.address else replace(target, address=address)
        try:
            return _send_pinned(attempt, headers, min(timeout, remaining))
        except (ssl.SSLCertVerificationError, OutboundPolicyError):
            raise
        except OSError as exc:
            if _is_certificate_error(exc):
                raise
            last_exc = exc
            continue
    assert last_exc is not None
    raise last_exc


def open_public_url(url: str, *, headers: Optional[Mapping[str, str]] = None, timeout: Optional[float] = None,
                    max_redirects: Optional[int] = None, max_bytes: Optional[int] = None,
                    total_timeout: Optional[float] = None) -> LimitedHTTPResponse:
    """GET a user-supplied URL on the public internet (see block comment).

    Returns a size-limited response usable as a context manager; raises
    OutboundPolicyError on a policy violation and urllib.error.HTTPError for
    an HTTP error status, so provider_boundary classifies it as usual.
    ``timeout`` bounds each socket wait; ``total_timeout`` (default
    BEETS_OUTBOUND_TOTAL_TIMEOUT_SECONDS) bounds the whole fetch including
    the body read, raising OutboundDeadlineExceeded (a TimeoutError)."""
    policy = current_outbound_policy()
    hops = policy.max_redirects if max_redirects is None else max(0, int(max_redirects))
    effective_timeout = policy.timeout_seconds if timeout is None else float(timeout)
    budget = _DEFAULT_TOTAL_TIMEOUT if total_timeout is None else float(total_timeout)
    deadline = time.monotonic() + max(0.1, budget)
    current = str(url or "")
    for _ in range(hops + 1):
        target = resolve_public_target(current)
        response = _send_pinned_any(target, dict(headers or {}), effective_timeout, deadline)
        if response.status in _REDIRECT_STATUSES:
            location = response.getheader("Location") or ""
            response.close()
            if not location:
                raise urllib.error.HTTPError(redact_url_for_log(current), response.status,
                                             "redirect without Location", response.headers, io.BytesIO())
            current = urllib.parse.urljoin(current, location)
            continue
        if response.status >= 400:
            response.close()
            raise urllib.error.HTTPError(redact_url_for_log(current), response.status, response.reason,
                                         response.headers, io.BytesIO())
        response.url = current  # type: ignore[attr-defined]  # urllib-compatible .url
        return LimitedHTTPResponse(response, policy.max_response_bytes if max_bytes is None else max_bytes,
                                   deadline=deadline, op_timeout=effective_timeout)
    raise OutboundPolicyError("outbound request exceeded the redirect limit")


def direct_peer_is_trusted(peer: str, trusted_cidrs: Iterable[str]) -> bool:
    try:
        ip = ipaddress.ip_address(peer or "")
    except ValueError:
        return False
    for raw in trusted_cidrs:
        raw = (raw or "").strip()
        if not raw or raw == "*":
            continue
        try:
            if ip in ipaddress.ip_network(raw, strict=False):
                return True
        except ValueError:
            continue
    return False


def bounded_rate_key_store_sweep(store: Dict[str, Any], *, now: Optional[float] = None, max_age: float = 3600, max_keys: int = 4096) -> None:
    now = time.time() if now is None else now
    stale = [key for key, bucket in store.items() if now - float(bucket.get("updated", now)) > max_age]
    for key in stale:
        store.pop(key, None)
    if len(store) <= max_keys:
        return
    for key, _ in sorted(store.items(), key=lambda item: float(item[1].get("updated", 0)))[: len(store) - max_keys]:
        store.pop(key, None)
