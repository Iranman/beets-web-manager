"""CodeQL #1350 (py/full-ssrf, routes_submissions._fetch_open_graph_metadata).

A pasted reference URL is fetched with backend.security.open_public_url():
public addresses only (BEETS_OUTBOUND_ALLOWLIST ignored), resolved once and
the socket pinned to the validated address, every redirect hop re-validated.
"""
import http.client
import io
import socket
import unittest
import urllib.error
from unittest import mock

import backend.provider_boundary as pb
from backend import security
from backend.security import OutboundPolicyError, open_public_url, resolve_public_target


def fake_getaddrinfo(*ips):
    def _inner(host, port, *args, **kwargs):
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]
    return _inner


class _FakeResponse(io.BytesIO):
    def __init__(self, status=200, body=b"", headers=None):
        super().__init__(body)
        self.status = status
        self.reason = "OK" if status < 400 else "ERR"
        self.headers = http.client.HTTPMessage()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def getheader(self, name, default=None):
        return self.headers.get(name, default)


class ResolvePublicTargetTests(unittest.TestCase):
    def blocked(self, url, *ips):
        with mock.patch("backend.security.socket.getaddrinfo", fake_getaddrinfo(*(ips or ("93.184.216.34",)))):
            with self.assertRaises(OutboundPolicyError):
                resolve_public_target(url)

    def test_rejects_non_public_answers(self):
        for ip in ("127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.10", "169.254.169.254", "100.100.100.200",
                   "100.64.0.1", "0.0.0.0", "198.18.0.1", "224.0.0.1", "240.0.0.1",
                   "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:169.254.169.254",
                   "2002:7f00:1::1", "64:ff9b::a9fe:a9fe", "fd00:ec2::254"):
            with self.subTest(ip=ip):
                self.blocked("http://rebind.example.test/x", ip)

    def test_rejects_when_any_answer_is_not_public(self):
        self.blocked("https://mixed.example.test/", "93.184.216.34", "127.0.0.1")

    def test_rejects_schemes_credentials_and_internal_names(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://example.com/", "javascript:alert(1)",
                    "https://user:pw@example.com/", "http://localhost/", "http://beets:8337/",
                    "http://host.docker.internal/", "http://nas.local/", "http://example.com:99999/"):
            with self.subTest(url=url):
                self.blocked(url)

    def test_ignores_the_operator_allowlist(self):
        env = {"BEETS_OUTBOUND_ALLOWLIST": "127.0.0.1:8337,localhost:8337,beets:8337,10.0.0.7:8080"}
        with mock.patch.dict("os.environ", env):
            # validate_outbound_url() (operator endpoints) accepts these ...
            with mock.patch("backend.security.socket.getaddrinfo", fake_getaddrinfo("127.0.0.1")):
                security.validate_outbound_url("http://127.0.0.1:8337/")
            # ... a user-supplied URL never may.
            self.blocked("http://127.0.0.1:8337/", "127.0.0.1")
            self.blocked("http://beets:8337/", "172.18.0.5")
            self.blocked("http://10.0.0.7:8080/", "10.0.0.7")

    def test_pins_the_validated_address_and_keeps_host_for_tls(self):
        with mock.patch("backend.security.socket.getaddrinfo", fake_getaddrinfo("93.184.216.34")):
            target = resolve_public_target("https://Public.Example.test:8443/a/b?q=1#frag")
        self.assertEqual((target.scheme, target.host, target.port, target.address, target.path),
                         ("https", "public.example.test", 8443, "93.184.216.34", "/a/b?q=1"))
        self.assertEqual(target.host_header, "public.example.test:8443")


class OpenPublicUrlTests(unittest.TestCase):
    def test_dns_rebinding_cannot_change_the_connected_address(self):
        # First lookup (validation) is public, any later lookup would be
        # loopback: the socket must still go to the validated address and
        # the hostname must not be resolved a second time.
        answers = iter([["93.184.216.34"], ["127.0.0.1"], ["127.0.0.1"]])

        def rebinding(host, port, *args, **kwargs):
            return fake_getaddrinfo(*next(answers))(host, port)

        connected = []

        def fake_create_connection(address, *args, **kwargs):
            connected.append(address)
            raise ConnectionRefusedError("stop before I/O")

        with mock.patch("backend.security.socket.getaddrinfo", side_effect=rebinding) as gai, \
                mock.patch("http.client.socket.create_connection", side_effect=fake_create_connection):
            with self.assertRaises(ConnectionRefusedError):
                open_public_url("http://rebind.example.test/page")
        self.assertEqual(connected, [("93.184.216.34", 80)])
        self.assertEqual(gai.call_count, 1)

    def test_https_connects_to_pinned_ip_and_verifies_original_hostname(self):
        wrapped = {}

        class _Ctx:
            def wrap_socket(self, sock, server_hostname=None):
                wrapped["server_hostname"] = server_hostname
                raise ConnectionAbortedError("stop before TLS")

        sock = mock.MagicMock()
        with mock.patch("backend.security.socket.getaddrinfo", fake_getaddrinfo("93.184.216.34")), \
                mock.patch("backend.security.ssl.create_default_context", return_value=_Ctx()), \
                mock.patch("http.client.socket.create_connection", return_value=sock) as cc:
            with self.assertRaises(ConnectionAbortedError):
                open_public_url("https://public.example.test/x")
        self.assertEqual(cc.call_args[0][0], ("93.184.216.34", 443))
        self.assertEqual(wrapped["server_hostname"], "public.example.test")

    def test_redirect_to_internal_host_is_blocked_before_connecting(self):
        sent = []

        def fake_send(target, headers, timeout):
            sent.append(target)
            return _FakeResponse(302, headers={"Location": "http://metadata.example.test/latest/meta-data/"})

        def gai(host, port, *args, **kwargs):
            ip = "169.254.169.254" if host == "metadata.example.test" else "93.184.216.34"
            return fake_getaddrinfo(ip)(host, port)

        with mock.patch("backend.security.socket.getaddrinfo", side_effect=gai), \
                mock.patch("backend.security._send_pinned", side_effect=fake_send):
            with self.assertRaises(OutboundPolicyError):
                open_public_url("https://public.example.test/start")
        self.assertEqual([t.host for t in sent], ["public.example.test"])

    def test_redirect_to_non_http_scheme_is_blocked(self):
        with mock.patch("backend.security.socket.getaddrinfo", fake_getaddrinfo("93.184.216.34")), \
                mock.patch("backend.security._send_pinned",
                           return_value=_FakeResponse(301, headers={"Location": "file:///etc/passwd"})):
            with self.assertRaises(OutboundPolicyError):
                open_public_url("https://public.example.test/start")

    def test_public_redirect_is_followed_repinned_and_bounded(self):
        responses = [_FakeResponse(302, headers={"Location": "/next"}), _FakeResponse(200, b"<title>ok</title>")]
        sent = []

        def fake_send(target, headers, timeout):
            sent.append((target.path, target.address, headers["User-Agent"]))
            return responses.pop(0)

        with mock.patch("backend.security.socket.getaddrinfo", fake_getaddrinfo("93.184.216.34")), \
                mock.patch("backend.security._send_pinned", side_effect=fake_send):
            with open_public_url("https://public.example.test/start", headers={"User-Agent": "ua"}) as resp:
                self.assertEqual(resp.read(), b"<title>ok</title>")
        self.assertEqual(sent, [("/start", "93.184.216.34", "ua"), ("/next", "93.184.216.34", "ua")])

        loop = lambda *a, **k: _FakeResponse(302, headers={"Location": "/again"})  # noqa: E731
        with mock.patch("backend.security.socket.getaddrinfo", fake_getaddrinfo("93.184.216.34")), \
                mock.patch("backend.security._send_pinned", side_effect=loop):
            with self.assertRaises(OutboundPolicyError):
                open_public_url("https://public.example.test/start", max_redirects=3)

    def test_http_error_status_raises_httperror_for_provider_classification(self):
        with mock.patch("backend.security.socket.getaddrinfo", fake_getaddrinfo("93.184.216.34")), \
                mock.patch("backend.security._send_pinned", return_value=_FakeResponse(404)):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                open_public_url("https://public.example.test/missing")
        self.assertEqual(pb.classify_exception(ctx.exception).outcome, pb.ProviderOutcome.REJECTED)

    def test_response_size_is_limited(self):
        with mock.patch("backend.security.socket.getaddrinfo", fake_getaddrinfo("93.184.216.34")), \
                mock.patch("backend.security._send_pinned", return_value=_FakeResponse(200, b"x" * 50)):
            with open_public_url("https://public.example.test/", max_bytes=10) as resp:
                with self.assertRaises(OutboundPolicyError):
                    resp.read()


class ReferenceUrlSinkTests(unittest.TestCase):
    def test_fetch_open_graph_metadata_goes_through_pinned_public_fetch(self):
        from routes_submissions import _fetch_open_graph_metadata

        html = b'<meta property="og:title" content="Album Title"><title>x</title>'
        with mock.patch("backend.security.socket.getaddrinfo", fake_getaddrinfo("93.184.216.34")), \
                mock.patch("backend.security._send_pinned", return_value=_FakeResponse(200, html)) as send, \
                mock.patch("urllib.request.urlopen") as urlopen:
            result = _fetch_open_graph_metadata("https://public.example.test/album")
        self.assertEqual(result["raw"]["title"], "Album Title")
        self.assertEqual(send.call_args[0][0].address, "93.184.216.34")
        urlopen.assert_not_called()

    def test_fetch_open_graph_metadata_rejects_allowlisted_internal_service(self):
        from routes_submissions import _fetch_open_graph_metadata

        with mock.patch.dict("os.environ", {"BEETS_OUTBOUND_ALLOWLIST": "127.0.0.1:8337"}), \
                mock.patch("backend.security.socket.getaddrinfo", fake_getaddrinfo("127.0.0.1")), \
                mock.patch("backend.security._send_pinned") as send:
            with self.assertRaises(OutboundPolicyError):
                _fetch_open_graph_metadata("http://127.0.0.1:8337/")
        send.assert_not_called()

    def test_route_validator_rejects_allowlisted_internal_service(self):
        from routes_submissions import _validate_reference_url

        with mock.patch.dict("os.environ", {"BEETS_OUTBOUND_ALLOWLIST": "127.0.0.1:8337"}), \
                mock.patch("backend.security.socket.getaddrinfo", fake_getaddrinfo("127.0.0.1")):
            with self.assertRaises(ValueError):
                _validate_reference_url("http://127.0.0.1:8337/")


class ProviderBoundaryOpenerTests(unittest.TestCase):
    def setUp(self):
        pb.reset_provider_health()
        self.addCleanup(pb.reset_provider_health)

    def test_custom_opener_is_used_and_outcome_recorded(self):
        calls = []

        def opener(request, timeout=None):
            calls.append((request, timeout))
            return _FakeResponse(200, b"body")

        with mock.patch("urllib.request.urlopen") as urlopen:
            with pb.opened("reference-url", "https://public.example.test/", timeout=7, opener=opener) as resp:
                self.assertEqual(resp.read(), b"body")
        urlopen.assert_not_called()
        self.assertEqual(calls, [("https://public.example.test/", 7)])
        self.assertEqual(pb.provider_health()["reference-url"]["last_outcome"], "confirmed")

    def test_policy_refusal_is_rejected_and_never_retried(self):
        calls = []

        def opener(request, timeout=None):
            calls.append(request)
            raise OutboundPolicyError("outbound host resolves to a prohibited address")

        self.assertGreater(pb.policy_for("artwork").max_attempts, 1)
        with self.assertRaises(OutboundPolicyError):
            with pb.opened("artwork", "https://img.example.test/a.png", timeout=5, opener=opener,
                           sleep=lambda _s: None):
                pass
        self.assertEqual(len(calls), 1)
        self.assertEqual(pb.provider_health()["artwork"]["last_outcome"], "rejected")


if __name__ == "__main__":
    unittest.main()
