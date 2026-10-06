"""SEC-6 / F6 / BA-4 regression tests for the operator-path outbound policy.

Before the fix, the documented CIDR form ("10.0.0.0/24:8080") was rejected
("must include an explicit port") because urlsplit treated "/24:8080" as a
path, [IPv6]:port worked only by accident, and 100.64.0.0/10 (CGNAT, e.g.
Tailscale) and other non-global ranges were reachable without an allowlist
entry.
"""
import logging
import socket
import unittest
import urllib.request
from unittest import mock

from backend import security
from backend.security import (
    OutboundPolicy, OutboundPolicyError, outbound_allowlist_problem, parse_outbound_allowlist,
    validate_outbound_url,
)


def _dns(*ips):
    def inner(host, port, *a, **kw):
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]
    return inner


class AllowlistParsingTests(unittest.TestCase):
    def test_cidr_entries_parse(self):
        rules = parse_outbound_allowlist("10.0.0.0/24:8080,[fd00::/64]:443")
        self.assertEqual([(r.kind, str(r.network), r.port) for r in rules],
                         [("network", "10.0.0.0/24", 8080), ("network", "fd00::/64", 443)])

    def test_host_ip_and_ipv6_entries_parse(self):
        rules = parse_outbound_allowlist(" lidarr:8686 , 192.168.1.5:32400 ,[fd00::1]:8080, My_Plex.lan:32400")
        self.assertEqual([(r.kind, r.port) for r in rules], [("host", 8686), ("ip", 32400), ("ip", 8080), ("host", 32400)])
        self.assertEqual(rules[3].host, "my_plex.lan")

    def test_every_malformed_entry_raises_policy_error(self):
        for value in ("*", "*.local:80", "lidarr", "10.0.0.0/8", "fd00::1:80", "a b:80", "10.0.0.0/33:80",
                      "host:0", "host:65536", "host:abc", "[::1]80", "[::1:80", ":80", "ex%41mple:80", "a/b:80"):
            with self.subTest(value=value):
                with self.assertRaises(OutboundPolicyError):
                    parse_outbound_allowlist(value)
                self.assertTrue(outbound_allowlist_problem(value))

    def test_valid_allowlist_has_no_problem(self):
        self.assertIsNone(outbound_allowlist_problem("127.0.0.1:8337,localhost:8337,beets:8337,10.0.0.0/24:8080"))


class OperatorPathAddressPolicyTests(unittest.TestCase):
    def check(self, url, ip, allowlist=""):
        policy = OutboundPolicy(parse_outbound_allowlist(allowlist))
        with mock.patch("backend.security.socket.getaddrinfo", _dns(ip)):
            validate_outbound_url(url, policy=policy)

    def test_non_global_ranges_need_an_allowlist_entry(self):
        for ip in ("100.64.1.1", "100.127.255.254", "198.18.0.1", "192.0.0.170", "2001:db8::1",
                   "64:ff9b::a00:1", "2002:a00:1::1"):
            with self.subTest(ip=ip):
                with self.assertRaises(OutboundPolicyError):
                    self.check("http://svc.example.com:8080/", ip)

    def test_cidr_allowlist_admits_matching_hosts(self):
        self.check("http://svc.example.com:8080/", "100.64.1.1", allowlist="100.64.0.0/10:8080")
        self.check("http://10.0.0.7:8080/", "10.0.0.7", allowlist="10.0.0.0/24:8080")
        with self.assertRaises(OutboundPolicyError):
            self.check("http://10.0.1.7:8080/", "10.0.1.7", allowlist="10.0.0.0/24:8080")
        with self.assertRaises(OutboundPolicyError):
            self.check("http://10.0.0.7:9090/", "10.0.0.7", allowlist="10.0.0.0/24:8080")

    def test_public_addresses_still_allowed(self):
        self.check("https://musicbrainz.org/ws/2/", "138.201.227.205")


class StartupValidationTests(unittest.TestCase):
    def test_invalid_allowlist_is_reported_clearly_at_install(self):
        with mock.patch.dict("os.environ", {"BEETS_OUTBOUND_ALLOWLIST": "lidarr"}), \
                mock.patch.object(urllib.request, security._INSTALLED_ATTR, False), \
                mock.patch.object(urllib.request, "urlopen", urllib.request.urlopen), \
                self.assertLogs("beets_web.security", level=logging.ERROR) as logs:
            security.install_secure_urllib()
        self.assertIn("BEETS_OUTBOUND_ALLOWLIST is invalid", "\n".join(logs.output))

    def test_invalid_allowlist_fails_every_operator_request_closed(self):
        with mock.patch.dict("os.environ", {"BEETS_OUTBOUND_ALLOWLIST": "lidarr"}):
            with self.assertRaises(OutboundPolicyError):
                security.secure_urlopen("https://musicbrainz.org/")


if __name__ == "__main__":
    unittest.main()
