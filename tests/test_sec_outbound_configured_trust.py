"""T1: operator-path outbound policy -- implicit trust of each service's own
configured URL, the address floor, the pinned connection (no DNS rebinding),
redirect handling and the "add host:port to the allowlist" hint.

Connections are pinned: the socket goes to an address the policy checked.
Tests resolve names with a fake getaddrinfo and record which (ip, port) the
socket was opened to; when a request is admitted, the recorded connection is
redirected to a local test server so the full urllib response path runs.
"""
import http.server
import ipaddress
import logging
import socket
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from backend import provider_boundary, security
from backend.security import (
    OutboundPolicy, OutboundPolicyError, allowlist_entry, implicit_rule_for_url, outbound_allowlist_ignored,
    parse_outbound_allowlist, validate_outbound_url,
)

_REAL_CREATE_CONNECTION = socket.create_connection
_REAL_GETADDRINFO = socket.getaddrinfo


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.server.seen.append((self.path, self.headers.get("Host")))
        location = self.server.redirects.get(self.path)
        if location:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _Net:
    """Fake DNS (host -> list of answers, or a list of successive answer
    lists) plus a recorder for every pinned socket connection."""

    def __init__(self, testcase, dns):
        self.dns = {k: list(v) for k, v in dns.items()}
        self.connected = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.seen, self.server.redirects = [], {}
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        testcase.addCleanup(self.server.server_close)
        testcase.addCleanup(self.server.shutdown)
        for patcher in (
            mock.patch("backend.security.socket.getaddrinfo", self.getaddrinfo),
            mock.patch("backend.security.socket.create_connection", self.create_connection),
            mock.patch.object(urllib.request, "urlopen", security.secure_urlopen),
        ):
            patcher.start()
            testcase.addCleanup(patcher.stop)

    def getaddrinfo(self, host, port, *a, **kw):
        if host == "127.0.0.1":  # the local test server itself
            return _REAL_GETADDRINFO(host, port, *a, **kw)
        answers = self.dns.get(host)
        if answers is None:
            raise socket.gaierror("no such host")
        if answers and isinstance(answers[0], list):  # successive lookups
            current = answers.pop(0) if len(answers) > 1 else answers[0]
        else:
            current = answers
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                for ip in current]

    def create_connection(self, address, timeout=None, source_address=None, **kw):
        self.connected.append((address[0], address[1], timeout))
        return _REAL_CREATE_CONNECTION(self.server.server_address, timeout)


class _TrustBase(unittest.TestCase):
    CONFIGURED = {"plex": "http://plex.test:32400", "lidarr": "http://lidarr.test:8686"}

    def setUp(self):
        env = mock.patch.dict("os.environ", {"BEETS_OUTBOUND_ALLOWLIST": "127.0.0.1:8337"})
        env.start()
        self.addCleanup(env.stop)
        resolvers = mock.patch.dict(provider_boundary._CONFIGURED_URL_RESOLVERS,
                                    {k: (lambda v=v: v) for k, v in self.CONFIGURED.items()}, clear=True)
        resolvers.start()
        self.addCleanup(resolvers.stop)
        provider_boundary.reset_provider_health()

    def fetch(self, provider, url, **kw):
        with provider_boundary.opened(provider, url, timeout=5, max_attempts=1, **kw) as response:
            return response.read()


class ImplicitTrustTests(_TrustBase):
    def test_private_configured_plex_url_is_allowed_for_plex_and_pinned(self):
        net = _Net(self, {"plex.test": ["192.168.0.250"]})
        self.assertEqual(self.fetch("plex", "http://plex.test:32400/identity"), b"ok")
        self.assertEqual(net.connected[0][:2], ("192.168.0.250", 32400))
        self.assertEqual(net.server.seen, [("/identity", "plex.test:32400")])

    def test_same_host_port_from_another_provider_is_blocked_with_hint(self):
        net = _Net(self, {"plex.test": ["192.168.0.250"]})
        with self.assertRaises(OutboundPolicyError) as ctx:
            self.fetch("lidarr", "http://plex.test:32400/identity")
        self.assertEqual(ctx.exception.hint,
                         "add plex.test:32400 to BEETS_OUTBOUND_ALLOWLIST or set LIDARR_URL to it")
        self.assertEqual(net.connected, [])
        self.assertIn("add plex.test:32400 to BEETS_OUTBOUND_ALLOWLIST",
                      provider_boundary.provider_health()["lidarr"]["last_message"])

    def test_untrusted_call_without_configured_trust_is_blocked(self):
        net = _Net(self, {"plex.test": ["192.168.0.250"]})
        with self.assertRaises(OutboundPolicyError):
            self.fetch("plex", "http://plex.test:32400/identity", trust_configured=False)
        self.assertEqual(net.connected, [])

    def test_different_port_or_scheme_is_blocked(self):
        net = _Net(self, {"plex.test": ["192.168.0.250"]})
        for url in ("http://plex.test:32401/", "https://plex.test:32400/", "http://plex.test/"):
            with self.subTest(url=url), self.assertRaises(OutboundPolicyError):
                self.fetch("plex", url)
        self.assertEqual(net.connected, [])

    def test_configured_url_resolving_to_loopback_or_metadata_is_refused(self):
        for ip in ("127.0.0.1", "::1", "169.254.169.254", "fd00:ec2::254", "::ffff:169.254.169.254",
                   "100.100.100.200", "169.254.10.1", "0.0.0.0", "224.0.0.1", "240.0.0.1"):
            with self.subTest(ip=ip):
                net = _Net(self, {"plex.test": [ip]})
                with self.assertRaises(OutboundPolicyError):
                    self.fetch("plex", "http://plex.test:32400/")
                self.assertEqual(net.connected, [])

    def test_mixed_private_and_loopback_answers_are_refused(self):
        net = _Net(self, {"plex.test": ["192.168.0.250", "127.0.0.1"]})
        with self.assertRaises(OutboundPolicyError):
            self.fetch("plex", "http://plex.test:32400/")
        self.assertEqual(net.connected, [])

    def test_ipv6_ula_and_cgnat_configured_urls_are_trusted(self):
        for configured, ip in (("http://[fd12::5]:32400", "fd12::5"), ("http://plex.test:32400", "100.64.1.1"),
                               ("http://plex.test:32400", "::ffff:10.0.0.9")):
            with self.subTest(ip=ip), mock.patch.dict(provider_boundary._CONFIGURED_URL_RESOLVERS,
                                                      {"plex": lambda c=configured: c}):
                net = _Net(self, {"plex.test": [ip], "fd12::5": [ip]})
                url = configured + "/identity"
                self.assertEqual(self.fetch("plex", url), b"ok")
                self.assertEqual(ipaddress.ip_address(net.connected[0][0]), ipaddress.ip_address(ip))

    def test_public_configured_url_follows_the_public_path(self):
        net = _Net(self, {"plex.test": ["93.184.216.34"]})
        self.assertEqual(self.fetch("plex", "http://plex.test:32400/"), b"ok")
        self.assertEqual(net.connected[0][0], "93.184.216.34")

    def test_docker_service_name_default_slskd_url_is_trusted(self):
        with mock.patch.dict(provider_boundary._CONFIGURED_URL_RESOLVERS, {"slskd": lambda: "http://slskd:5030"}):
            net = _Net(self, {"slskd": ["172.18.0.7"]})
            self.assertEqual(self.fetch("slskd", "http://slskd:5030/api/v0/application"), b"ok")
        self.assertEqual(net.connected[0][:2], ("172.18.0.7", 5030))


class RebindingAndRedirectTests(_TrustBase):
    def test_dns_rebinding_on_operator_path_is_blocked(self):
        # probe-17: the check sees a public answer, the connect-time lookup a
        # loopback one. Every lookup is checked and the socket only ever goes
        # to an address the same lookup validated.
        net = _Net(self, {"rebind.example.test": [["93.184.216.34"], ["127.0.0.1"]]})
        with self.assertRaises(OutboundPolicyError):
            security.secure_urlopen("http://rebind.example.test/x", timeout=5)
        self.assertNotIn("127.0.0.1", [c[0] for c in net.connected])

    def test_dns_rebinding_against_trusted_service_is_blocked(self):
        net = _Net(self, {"plex.test": [["192.168.0.250"], ["169.254.169.254"]]})
        with self.assertRaises(OutboundPolicyError):
            self.fetch("plex", "http://plex.test:32400/")
        self.assertEqual(net.connected, [])

    def test_cross_origin_redirect_from_trusted_endpoint_is_refused(self):
        net = _Net(self, {"plex.test": ["192.168.0.250"], "other.example.test": ["93.184.216.34"]})
        net.server.redirects["/start"] = "http://other.example.test/next"
        with self.assertRaises(OutboundPolicyError):
            self.fetch("plex", "http://plex.test:32400/start")
        self.assertEqual(len(net.connected), 1)

    def test_same_origin_redirect_from_trusted_endpoint_is_followed(self):
        net = _Net(self, {"plex.test": ["192.168.0.250"]})
        net.server.redirects["/start"] = "/next"
        self.assertEqual(self.fetch("plex", "http://plex.test:32400/start"), b"ok")
        self.assertEqual([c[0] for c in net.connected], ["192.168.0.250", "192.168.0.250"])

    def test_untrusted_redirect_hop_is_rechecked(self):
        net = _Net(self, {"pub.example.test": ["93.184.216.34"], "evil.example.test": ["10.0.0.5"]})
        net.server.redirects["/start"] = "http://evil.example.test/admin"
        with self.assertRaises(OutboundPolicyError):
            security.secure_urlopen("http://pub.example.test/start", timeout=5)
        self.assertEqual([c[0] for c in net.connected], ["93.184.216.34"])

    def test_timeout_reaches_the_pinned_socket_and_is_raised(self):
        net = _Net(self, {"plex.test": ["192.168.0.250"]})
        self.fetch("plex", "http://plex.test:32400/")
        self.assertEqual(net.connected[0][2], 5)
        with mock.patch("backend.security.socket.create_connection", side_effect=socket.timeout("timed out")):
            with self.assertRaises(OSError):
                self.fetch("plex", "http://plex.test:32400/")
        # urllib wraps the socket timeout in URLError, exactly as before pinning.
        self.assertEqual(provider_boundary.provider_health()["plex"]["last_outcome"], "unavailable")

    def test_proxy_environment_is_not_used(self):
        net = _Net(self, {"plex.test": ["192.168.0.250"]})
        with mock.patch.dict("os.environ", {"HTTP_PROXY": "http://proxy.example.test:3128",
                                            "http_proxy": "http://proxy.example.test:3128"}):
            self.fetch("plex", "http://plex.test:32400/")
        self.assertEqual(net.connected[0][:2], ("192.168.0.250", 32400))


class ExplicitRuleFloorTests(unittest.TestCase):
    def check(self, url, ip, allowlist):
        policy = OutboundPolicy(parse_outbound_allowlist(allowlist))
        with mock.patch("backend.security.socket.getaddrinfo",
                        lambda h, p, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, p))]):
            validate_outbound_url(url, policy=policy)

    def test_explicit_loopback_entries_remain_possible(self):
        self.check("http://localhost:8337/", "127.0.0.1", "localhost:8337")
        self.check("http://127.0.0.1:8337/", "127.0.0.1", "127.0.0.1:8337")

    def test_metadata_and_link_local_are_refused_even_when_listed(self):
        for url, ip, entry in (("http://169.254.169.254/", "169.254.169.254", "169.254.169.254:80"),
                               ("http://meta.lan/", "169.254.169.254", "meta.lan:80"),
                               ("http://ll.lan/", "169.254.3.4", "ll.lan:80"),
                               ("http://100.100.100.200/", "100.100.100.200", "100.64.0.0/10:80")):
            with self.subTest(entry=entry), self.assertRaises(OutboundPolicyError):
                self.check(url, ip, entry)

    def test_catch_all_cidrs_are_ignored_and_logged(self):
        rules = parse_outbound_allowlist("0.0.0.0/0:80,[::/0]:80,128.0.0.0/1:80,10.0.0.0/8:80")
        self.assertEqual([str(r.network) for r in rules], ["10.0.0.0/8"])
        self.assertEqual(outbound_allowlist_ignored("0.0.0.0/0:80,10.0.0.0/8:80"), ("0.0.0.0/0:80",))
        with self.assertRaises(OutboundPolicyError):
            self.check("http://127.0.0.1/", "127.0.0.1", "0.0.0.0/0:80")
        with mock.patch.dict("os.environ", {"BEETS_OUTBOUND_ALLOWLIST": "0.0.0.0/0:80"}), \
                mock.patch.object(urllib.request, security._INSTALLED_ATTR, False, create=True), \
                mock.patch.object(urllib.request, security._ORIGINAL_URLOPEN_ATTR, None, create=True), \
                mock.patch.object(urllib.request, "urlopen", urllib.request.urlopen), \
                self.assertLogs("beets_web.security", level=logging.ERROR) as logs:
            security.install_secure_urllib()
        self.assertIn("0.0.0.0/0:80 is ignored", "\n".join(logs.output))

    def test_blocked_private_target_names_the_needed_entry(self):
        with self.assertRaises(OutboundPolicyError) as ctx:
            self.check("http://192.168.0.250:32400/x?X-Plex-Token=abc", "192.168.0.250", "")
        self.assertEqual(ctx.exception.entry, "192.168.0.250:32400")
        self.assertEqual(ctx.exception.hint, "add 192.168.0.250:32400 to BEETS_OUTBOUND_ALLOWLIST")
        with self.assertRaises(OutboundPolicyError) as ctx:
            self.check("http://meta.example.test/", "169.254.169.254", "")
        self.assertEqual(ctx.exception.hint, "")


class HelperTests(unittest.TestCase):
    def test_malformed_or_credentialed_configured_urls_earn_no_rule(self):
        for url in ("", "plex", "ftp://plex.test:21", "http://u:p@plex.test:32400", "http://plex.test:99999",
                    "http://[bad", "http://", "http://a b:80", None):
            with self.subTest(url=url):
                self.assertIsNone(implicit_rule_for_url(url))
        rule = implicit_rule_for_url("HTTPS://Plex.Test")
        self.assertEqual((rule.kind, rule.host, rule.port, rule.scheme, rule.implicit),
                         ("host", "plex.test", 443, "https", True))

    def test_allowlist_hint_strips_userinfo_query_and_fragment(self):
        self.assertEqual(
            provider_boundary.allowlist_hint("http://u:secret@192.168.0.250:32400/x?X-Plex-Token=abc#frag", "plex"),
            "add 192.168.0.250:32400 to BEETS_OUTBOUND_ALLOWLIST or set PLEX_URL to it")
        self.assertEqual(allowlist_entry("http://[fd12::5]/"), "[fd12::5]:80")
        self.assertEqual(provider_boundary.allowlist_hint("https://x.test", "musicbrainz"),
                         "add x.test:443 to BEETS_OUTBOUND_ALLOWLIST")
        self.assertEqual(provider_boundary.allowlist_hint("not a url", "plex"), "")

    def test_allowlist_hint_omits_the_setting_when_the_url_already_is_it(self):
        with mock.patch.dict(provider_boundary._CONFIGURED_URL_RESOLVERS, {"plex": lambda: "http://Plex.Test:32400/"}):
            self.assertEqual(provider_boundary.allowlist_hint("http://plex.test:32400/identity", "plex"),
                             "add plex.test:32400 to BEETS_OUTBOUND_ALLOWLIST")
            for other in ("https://plex.test:32400/", "http://plex.test:32401/", "http://other.test:32400/"):
                with self.subTest(url=other):
                    self.assertTrue(provider_boundary.allowlist_hint(other, "plex").endswith(" or set PLEX_URL to it"))

    def test_every_operator_provider_has_a_configured_url_setting(self):
        for provider in ("plex", "lidarr", "slskd", "qbittorrent", "ytdlp-po", "ai"):
            self.assertIn(provider, provider_boundary.CONFIGURED_URL_SETTINGS)
            self.assertIn(provider, provider_boundary.POLICIES)
        self.assertEqual(provider_boundary.configured_url("musicbrainz"), "")


if __name__ == "__main__":
    unittest.main()
