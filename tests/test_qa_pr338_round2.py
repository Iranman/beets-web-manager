"""QA (PR #338, fix round 2): default ports with the pinned-port check, the
redirect rules, PLEX_URL save hardening, and an unpatched end-to-end Plex
call to a real HTTP server on this host's own private (RFC 1918) address."""
import http.server
import ipaddress
import json
import os
import socket
import threading
import unittest
import urllib.request
from unittest import mock

from backend import provider_boundary, security
from backend.security import OutboundPolicyError
from tests.test_plex_outbound_diagnostics import PlexUrlSaveValidationTests, TOKEN
from tests.test_sec_outbound_configured_trust import _Net, _TrustBase


class DefaultPortTests(_TrustBase):
    def use(self, url):
        p = mock.patch.dict(provider_boundary._CONFIGURED_URL_RESOLVERS, {"plex": lambda: url})
        p.start()
        self.addCleanup(p.stop)

    def test_http_without_port_connects_to_80(self):
        self.use("http://plex.test")
        for url in ("http://plex.test/identity", "http://plex.test:80/identity"):
            net = _Net(self, {"plex.test": ["192.168.0.250"]})
            self.assertEqual(self.fetch("plex", url), b"ok")
            self.assertEqual(net.connected[0][:2], ("192.168.0.250", 80))

    def test_explicit_80_config_admits_portless_url(self):
        self.use("http://plex.test:80")
        net = _Net(self, {"plex.test": ["192.168.0.250"]})
        self.assertEqual(self.fetch("plex", "http://plex.test/identity"), b"ok")
        self.assertEqual(net.connected[0][:2], ("192.168.0.250", 80))

    def test_https_without_port_pins_443(self):
        self.use("https://plex.test")
        for url in ("https://plex.test/identity", "https://plex.test:443/identity"):
            net = _Net(self, {"plex.test": ["192.168.0.250"]})
            with self.assertRaises(Exception) as ctx:  # TLS against a plain-HTTP test server
                self.fetch("plex", url)
            self.assertNotIsInstance(ctx.exception, OutboundPolicyError)
            self.assertEqual(net.connected[0][:2], ("192.168.0.250", 443))

    def test_https_config_does_not_admit_http_on_443(self):
        self.use("https://plex.test")
        net = _Net(self, {"plex.test": ["192.168.0.250"]})
        with self.assertRaises(OutboundPolicyError):
            self.fetch("plex", "http://plex.test:443/identity")
        self.assertEqual(net.connected, [])

    def test_same_origin_redirect_between_default_and_explicit_port(self):
        self.use("http://plex.test")
        net = _Net(self, {"plex.test": ["192.168.0.250"]})
        net.server.redirects["/a"] = "http://plex.test:80/b"
        self.assertEqual(self.fetch("plex", "http://plex.test/a"), b"ok")
        self.assertEqual([c[1] for c in net.connected], [80, 80])

    def test_https_default_port_connection_mismatch_refused(self):
        conn = security._OperatorHTTPSConnection("plex.test", None, bwm_addresses=("192.168.0.250",), bwm_port=443)
        self.assertEqual(conn.port, 443)
        with self.assertRaises(OutboundPolicyError):
            conn._create_connection(("plex.test", 8443), 5)


class RedirectRuleTests(_TrustBase):
    def test_absolute_same_origin_redirect_followed_and_token_kept(self):
        net = _Net(self, {"plex.test": ["192.168.0.250"]})
        net.server.redirects["/a"] = "http://PLEX.test:32400/b"
        req = urllib.request.Request("http://plex.test:32400/a", headers={"X-Plex-Token": "t"})
        self.assertEqual(self.fetch("plex", req), b"ok")
        self.assertEqual(len(net.connected), 2)

    def test_cross_origin_to_other_private_service_refused(self):
        net = _Net(self, {"plex.test": ["192.168.0.250"], "lidarr.test": ["192.168.0.251"]})
        net.server.redirects["/a"] = "http://lidarr.test:8686/api"
        with self.assertRaises(OutboundPolicyError):
            self.fetch("plex", "http://plex.test:32400/a")
        self.assertEqual(len(net.connected), 1)

    def test_cross_origin_to_public_host_refused(self):
        net = _Net(self, {"plex.test": ["192.168.0.250"], "pub.example.test": ["93.184.216.34"]})
        net.server.redirects["/a"] = "http://pub.example.test/x"
        with self.assertRaises(OutboundPolicyError):
            self.fetch("plex", "http://plex.test:32400/a")
        self.assertEqual(len(net.connected), 1)

    def test_https_to_http_downgrade_refused_by_handler(self):
        rule = security.implicit_rule_for_url("https://plex.test:32400")
        handler = security.SafeRedirectHandler(trusted=rule)
        req = urllib.request.Request("https://plex.test:32400/a")
        with self.assertRaises(OutboundPolicyError):
            handler.redirect_request(req, None, 302, "Found", {}, "http://plex.test:32400/b")

    def test_into_origin_from_explicitly_allowlisted_host_refused(self):
        with mock.patch.dict("os.environ", {"BEETS_OUTBOUND_ALLOWLIST": "other.test:9000"}):
            net = _Net(self, {"plex.test": ["192.168.0.250"], "other.test": ["192.168.0.9"]})
            net.server.redirects["/a"] = "http://plex.test:32400/b"
            with self.assertRaises(OutboundPolicyError):
                self.fetch("plex", "http://other.test:9000/a")
            self.assertEqual([c[0] for c in net.connected], ["192.168.0.9"])

    def test_trust_does_not_survive_into_a_later_untrusted_call(self):
        _Net(self, {"plex.test": ["192.168.0.250"]})
        self.fetch("plex", "http://plex.test:32400/a")
        with self.assertRaises(OutboundPolicyError):
            urllib.request.urlopen("http://plex.test:32400/a", timeout=5)


class AddressTests(unittest.TestCase):
    def test_ipv4_compatible_forms(self):
        for ip in ("::8.8.8.8", "::a.b.c.d".replace("a.b.c.d", "192.168.0.1"), "::169.254.169.254", "::"):
            self.assertFalse(security.address_is_public(ipaddress.ip_address(ip)), ip)
        self.assertTrue(security.address_is_public(ipaddress.ip_address("2606:4700::1111")))
        self.assertTrue(security.address_is_public(ipaddress.ip_address("8.8.8.8")))

    def test_loopback_and_metadata_never_trusted(self):
        for url in ("http://127.0.0.1:32400", "http://169.254.169.254:80", "http://100.100.100.200:80",
                    "http://[::1]:32400", "http://[::ffff:169.254.169.254]:80"):
            rule = security.implicit_rule_for_url(url)
            with mock.patch("backend.security.socket.getaddrinfo",
                            lambda h, p, *a, **k: [(socket.AF_INET6 if ":" in h else socket.AF_INET,
                                                    socket.SOCK_STREAM, 6, "", (h.strip("[]"), p))]):
                with self.assertRaises(OutboundPolicyError, msg=url):
                    security.validate_outbound_url(url, trusted=rule)


class PlexUrlSaveExtraTests(PlexUrlSaveValidationTests):
    def test_more_token_smuggling_shapes_refused_without_echo(self):
        for url in (f"http://plex.lan:32400;X-Plex-Token={TOKEN}", f"http://plex.lan:32400/a;X-Plex-Token={TOKEN}",
                    f"http://plex.lan:32400/a;X-Plex-Token={TOKEN}/b",
                    "http://plex.lan:-1", "http://plex.lan:65536", "http://plex.lan:３２４００",
                    "http://[fd00::1", "javascript://plex.lan/"):
            with self.subTest(url=url):
                resp = self.save(url)
                self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
                self.assertEqual(resp.get_json()["error"], self.module.PLEX_URL_INVALID_MESSAGE)
                self.assertNotIn(TOKEN, resp.get_data(as_text=True))
                self.assertNotIn("PLEX_URL", self.env_file.read_text(encoding="utf-8"))

    def test_valid_shapes_still_save(self):
        for url in ("http://192.168.0.250:32400", "https://plex.lan", "http://[fd00::5]:32400/", "http://plex.lan:65535"):
            with self.subTest(url=url):
                self.assertEqual(self.save(url).status_code, 200)


def _private_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    addr = ipaddress.ip_address(ip)
    return ip if security.address_in_private_floor(addr) else None


class _PlexHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.server.seen.append((self.path, self.headers.get("X-Plex-Token")))
        body = json.dumps({"MediaContainer": {"machineIdentifier": "m1", "Directory": [
            {"key": "3", "type": "artist", "title": "Music"}]}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@unittest.skipUnless(_private_ip(), "host has no RFC1918/CGNAT/ULA address")
class EndToEndPlexOnPrivateAddress(unittest.TestCase):
    """No DNS/socket patching: real secure_urlopen, real pinned connection."""

    def setUp(self):
        self.ip = _private_ip()
        self.server = http.server.ThreadingHTTPServer((self.ip, 0), _PlexHandler)
        self.server.seen = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://{self.ip}:{self.server.server_address[1]}"
        for p in (mock.patch.object(urllib.request, "urlopen", security.secure_urlopen),
                  mock.patch.dict(os.environ, {"BEETS_OUTBOUND_ALLOWLIST": "", "PLEX_URL": self.url,
                                               "PLEX_TOKEN": "qa-secret-token"})):
            p.start()
            self.addCleanup(p.stop)
        from backend import plex_service
        self.plex = plex_service
        p = mock.patch.object(plex_service, "_plex_config_from_beets", return_value={})
        p.start()
        self.addCleanup(p.stop)
        provider_boundary.reset_provider_health()

    def test_library_sections_reaches_server_with_token(self):
        data = self.plex._plex_request("/library/sections", timeout=5, attempts=1)
        self.assertEqual(data["MediaContainer"]["machineIdentifier"], "m1")
        self.assertEqual(self.server.seen, [("/library/sections", "qa-secret-token")])

    def test_same_address_without_trust_is_blocked(self):
        with self.assertRaises(OutboundPolicyError) as ctx:
            with provider_boundary.opened("lidarr", self.url + "/library/sections", timeout=5, max_attempts=1):
                pass
        self.assertNotIn("qa-secret-token", str(ctx.exception))
        self.assertEqual(self.server.seen, [])

    def test_other_port_on_same_private_host_is_blocked(self):
        other = f"http://{self.ip}:{self.server.server_address[1] + 1}/library/sections"
        with self.assertRaises(OutboundPolicyError):
            with provider_boundary.opened("plex", other, timeout=5, max_attempts=1):
                pass


if __name__ == "__main__":
    unittest.main()
