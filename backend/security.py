"""Security helpers for outbound requests and abuse controls."""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

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
_SENSITIVE_REDIRECT_HEADERS = {
    "authorization",
    "cookie",
    "x-api-key",
    "x-auth-token",
    "x-forwarded-authorization",
    "proxy-authorization",
}
_DEFAULT_TIMEOUT = float(os.environ.get("BEETS_OUTBOUND_TIMEOUT_SECONDS", "20") or "20")
_DEFAULT_MAX_BYTES = int(os.environ.get("BEETS_OUTBOUND_MAX_RESPONSE_BYTES", str(20 * 1024 * 1024)) or str(20 * 1024 * 1024))
_DEFAULT_MAX_REDIRECTS = int(os.environ.get("BEETS_OUTBOUND_MAX_REDIRECTS", "5") or "5")
_ORIGINAL_URLOPEN_ATTR = "_beets_original_urlopen"
_INSTALLED_ATTR = "_beets_secure_urlopen_installed"


class OutboundPolicyError(ValueError):
    """Raised when an outbound request violates the SSRF policy."""


@dataclass(frozen=True)
class OutboundAllowRule:
    kind: str
    port: int
    host: str = ""
    ip: Optional[ipaddress._BaseAddress] = None
    network: Optional[ipaddress._BaseNetwork] = None


@dataclass(frozen=True)
class OutboundPolicy:
    allow_rules: Tuple[OutboundAllowRule, ...]
    max_response_bytes: int = _DEFAULT_MAX_BYTES
    max_redirects: int = _DEFAULT_MAX_REDIRECTS
    timeout_seconds: float = _DEFAULT_TIMEOUT


def _clean_host(value: str) -> str:
    return (value or "").strip().rstrip(".").lower()


def _parse_host_port(raw: str) -> Tuple[str, int]:
    value = (raw or "").strip()
    if not value or "*" in value:
        raise OutboundPolicyError("outbound allowlist entries must be exact host/IP/CIDR plus port")
    parsed = urllib.parse.urlsplit("//" + value)
    host = parsed.hostname or ""
    port = parsed.port
    if not host or port is None:
        raise OutboundPolicyError("outbound allowlist entries must include an explicit port")
    if port < 1 or port > 65535:
        raise OutboundPolicyError("outbound allowlist port is out of range")
    return _clean_host(host), int(port)


def parse_outbound_allowlist(raw: Optional[str] = None) -> Tuple[OutboundAllowRule, ...]:
    """Parse BEETS_OUTBOUND_ALLOWLIST.

    Entries must be exact and port-scoped, for example:
      192.168.0.250:32400,lidarr:8686,10.0.0.0/24:8080
    Wildcards and suffix matching are deliberately unsupported.
    """
    value = os.environ.get("BEETS_OUTBOUND_ALLOWLIST", "") if raw is None else raw
    rules: List[OutboundAllowRule] = []
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        host, port = _parse_host_port(item)
        try:
            if "/" in host:
                rules.append(OutboundAllowRule(kind="network", port=port, network=ipaddress.ip_network(host, strict=False)))
            else:
                rules.append(OutboundAllowRule(kind="ip", port=port, ip=ipaddress.ip_address(host)))
            continue
        except ValueError:
            if "/" in host:
                raise OutboundPolicyError("invalid outbound allowlist CIDR")
        if any(ch.isspace() for ch in host) or any(ord(ch) < 32 for ch in host):
            raise OutboundPolicyError("invalid outbound allowlist host")
        rules.append(OutboundAllowRule(kind="host", port=port, host=host))
    return tuple(rules)


def current_outbound_policy() -> OutboundPolicy:
    return OutboundPolicy(allow_rules=parse_outbound_allowlist())


def _url_port(parsed: urllib.parse.SplitResult) -> int:
    if parsed.port is not None:
        return int(parsed.port)
    return 443 if parsed.scheme.lower() == "https" else 80


def _origin(value: str) -> Tuple[str, str, int]:
    parsed = urllib.parse.urlsplit(value)
    return (parsed.scheme.lower(), _clean_host(parsed.hostname or ""), _url_port(parsed))


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
    check_ip = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
    if check_ip in _METADATA_IPS:
        return True
    return any((
        check_ip.is_loopback,
        check_ip.is_private,
        check_ip.is_link_local,
        check_ip.is_multicast,
        check_ip.is_unspecified,
        check_ip.is_reserved,
    ))


def _allow_rule_matches(rule: OutboundAllowRule, host: str, port: int, addresses: Tuple[ipaddress._BaseAddress, ...]) -> bool:
    if rule.port != port:
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
        return bool(addresses) and all(((addr.ipv4_mapped if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped else addr) in rule.network) for addr in addresses)
    return False


def _is_allowlisted(host: str, port: int, addresses: Tuple[ipaddress._BaseAddress, ...], policy: OutboundPolicy) -> bool:
    return any(_allow_rule_matches(rule, host, port, addresses) for rule in policy.allow_rules)


def validate_outbound_url(url: str, *, policy: Optional[OutboundPolicy] = None) -> None:
    policy = policy or current_outbound_policy()
    parsed = urllib.parse.urlsplit(str(url or ""))
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise OutboundPolicyError("outbound URL scheme is not allowed")
    if parsed.username or parsed.password:
        raise OutboundPolicyError("outbound URL credentials are not allowed")
    host = _clean_host(parsed.hostname or "")
    if not host or any(ord(ch) < 32 for ch in host):
        raise OutboundPolicyError("outbound URL host is invalid")
    port = _url_port(parsed)
    try:
        addresses = _resolve_host(host, port)
    except OutboundPolicyError:
        LOG.warning("blocked outbound request to unresolved host: %s", redact_url_for_log(url))
        raise
    allowlisted = _is_allowlisted(host, port, addresses, policy)
    if not allowlisted and _host_is_local_or_internal(host):
        LOG.warning("blocked outbound request to internal host: %s", redact_url_for_log(url))
        raise OutboundPolicyError("outbound host is internal and not allowlisted")
    prohibited = [addr for addr in addresses if _address_is_prohibited(addr)]
    if prohibited and not allowlisted:
        LOG.warning("blocked outbound request to prohibited address: %s", redact_url_for_log(url))
        raise OutboundPolicyError("outbound host resolves to a prohibited address")
    if allowlisted:
        return


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


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, policy: Optional[OutboundPolicy] = None):
        self.policy = policy or current_outbound_policy()
        self.max_redirections = self.policy.max_redirects
        self.max_repeats = self.policy.max_redirects
        super().__init__()

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_outbound_url(newurl, policy=self.policy)
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None:
            strip_cross_origin_sensitive_headers(new_req, req.full_url, newurl)
        return new_req


class LimitedHTTPResponse:
    def __init__(self, response: Any, max_bytes: int):
        self._response = response
        self._max_bytes = max(1, int(max_bytes or _DEFAULT_MAX_BYTES))
        self._read = 0

    def read(self, amt: Optional[int] = None) -> bytes:
        remaining = self._max_bytes - self._read
        if remaining < 0:
            raise OutboundPolicyError("outbound response exceeded size limit")
        if amt is None or amt < 0:
            data = self._response.read(remaining + 1)
        else:
            data = self._response.read(min(int(amt), remaining + 1))
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
    policy = current_outbound_policy()
    target = _request_url(url)
    validate_outbound_url(target, policy=policy)
    handlers: List[Any] = [SafeRedirectHandler(policy)]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    effective_timeout = policy.timeout_seconds if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout
    response = opener.open(url, data=data, timeout=effective_timeout)
    return LimitedHTTPResponse(response, policy.max_response_bytes)


def install_secure_urllib() -> None:
    if getattr(urllib.request, _INSTALLED_ATTR, False):
        return
    setattr(urllib.request, _ORIGINAL_URLOPEN_ATTR, urllib.request.urlopen)
    urllib.request.urlopen = secure_urlopen
    setattr(urllib.request, _INSTALLED_ATTR, True)


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
