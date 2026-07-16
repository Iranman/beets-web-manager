import socket
import unittest
import urllib.request
from unittest import mock

from backend.security import (
    OutboundPolicy,
    OutboundPolicyError,
    SafeRedirectHandler,
    parse_outbound_allowlist,
    strip_cross_origin_sensitive_headers,
    validate_outbound_url,
)


def fake_getaddrinfo(*ips):
    def _inner(host, port, *args, **kwargs):
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]
    return _inner


class OutboundSecurityTests(unittest.TestCase):
    def assert_blocked(self, url, *ips, allowlist=""):
        policy = OutboundPolicy(parse_outbound_allowlist(allowlist))
        with mock.patch("backend.security.socket.getaddrinfo", fake_getaddrinfo(*ips)):
            with self.assertRaises(OutboundPolicyError):
                validate_outbound_url(url, policy=policy)

    def assert_allowed(self, url, *ips, allowlist=""):
        policy = OutboundPolicy(parse_outbound_allowlist(allowlist))
        with mock.patch("backend.security.socket.getaddrinfo", fake_getaddrinfo(*ips)):
            validate_outbound_url(url, policy=policy)

    def test_blocks_loopback_private_link_local_metadata_and_unspecified(self):
        cases = [
            ("http://127.0.0.1:80/", "127.0.0.1"),
            ("http://127.1:80/", "127.0.0.1"),
            ("http://2130706433:80/", "127.0.0.1"),
            ("http://0x7f000001:80/", "127.0.0.1"),
            ("http://[::1]:80/", "::1"),
            ("http://0.0.0.0:80/", "0.0.0.0"),
            ("http://10.0.0.5:80/", "10.0.0.5"),
            ("http://172.16.1.1:80/", "172.16.1.1"),
            ("http://192.168.1.20:80/", "192.168.1.20"),
            ("http://[::ffff:127.0.0.1]:80/", "::ffff:127.0.0.1"),
            ("http://169.254.169.254/latest/meta-data/", "169.254.169.254"),
            ("http://169.254.1.20:80/", "169.254.1.20"),
        ]
        for url, ip in cases:
            with self.subTest(url=url):
                self.assert_blocked(url, ip)

    def test_blocks_internal_hostnames_and_embedded_credentials(self):
        self.assert_blocked("http://localhost:8337/", "127.0.0.1")
        self.assert_blocked("http://lidarr:8686/api/v1/system/status", "172.20.0.3")
        with self.assertRaises(OutboundPolicyError):
            validate_outbound_url("https://user:pass@example.com/")

    def test_blocks_hostname_when_any_dns_answer_is_prohibited(self):
        self.assert_blocked("https://metadata.example/", "93.184.216.34", "169.254.169.254")

    def test_allows_public_hosts_and_explicit_arr_allowlist(self):
        self.assert_allowed("https://musicbrainz.org/ws/2/release", "138.201.227.205")
        self.assert_allowed(
            "http://192.168.0.250:8686/api/v1/system/status",
            "192.168.0.250",
            allowlist="192.168.0.250:8686",
        )
        self.assert_allowed(
            "http://lidarr:8686/api/v1/system/status",
            "172.20.0.3",
            allowlist="lidarr:8686",
        )

    def test_redirect_targets_are_revalidated(self):
        policy = OutboundPolicy(parse_outbound_allowlist(""))
        handler = SafeRedirectHandler(policy)
        req = urllib.request.Request("https://example.com/start")
        with mock.patch("backend.security.socket.getaddrinfo", fake_getaddrinfo("127.0.0.1")):
            with self.assertRaises(OutboundPolicyError):
                handler.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1/admin")

    def test_cross_origin_redirect_strips_authorization_headers(self):
        req = urllib.request.Request(
            "https://api.example.test/start",
            headers={"Authorization": "Bearer token", "X-Api-Key": "key", "Accept": "application/json"},
        )
        new_req = urllib.request.Request(
            "https://other.example.test/next",
            headers={"Authorization": "Bearer token", "X-Api-Key": "key", "Accept": "application/json"},
        )
        stripped = strip_cross_origin_sensitive_headers(new_req, req.full_url, new_req.full_url)
        lowered = {key.lower() for key in stripped.headers}
        self.assertNotIn("authorization", lowered)
        self.assertNotIn("x-api-key", lowered)
        self.assertIn("accept", lowered)

    def test_allowlist_rejects_wildcards_and_requires_ports(self):
        for value in ("*", "*.local:80", "lidarr", "10.0.0.0/8"):
            with self.subTest(value=value):
                with self.assertRaises(OutboundPolicyError):
                    parse_outbound_allowlist(value)


if __name__ == "__main__":
    unittest.main()
