"""SEC-7 / IA-09 regression tests for open_public_url().

Before the fix: only the first DNS answer was ever tried; a TLS certificate
failure was classified as transient and retried; and the timeout bounded
each socket wait only, so a server dripping bytes could hold a fetch open
indefinitely.
"""
import http.server
import socket
import ssl
import threading
import time
import unittest
import urllib.error
from unittest import mock

from backend import provider_boundary as pb
from backend import security
from backend.security import LimitedHTTPResponse, OutboundDeadlineExceeded, PinnedTarget, open_public_url

_A, _B = "93.184.216.34", "93.184.216.35"


def _dns(*ips):
    def inner(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]
    return inner


class _Resp:
    def __init__(self, status=200, body=b"ok"):
        self.status, self.reason, self._body = status, "OK", body
        self.headers = {}

    def getheader(self, name, default=None):
        return default

    def read(self, amt=None):
        data, self._body = (self._body, b"") if amt is None else (self._body[:amt], self._body[amt:])
        return data

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class AddressFallbackTests(unittest.TestCase):
    def test_second_answer_is_used_when_first_refuses(self):
        tried = []

        def send(target, headers, timeout):
            tried.append(target.address)
            if target.address == _A:
                raise ConnectionRefusedError("down")
            return _Resp(body=b"hello")

        with mock.patch("backend.security.socket.getaddrinfo", _dns(_A, _B)), \
                mock.patch("backend.security._send_pinned", side_effect=send):
            with open_public_url("https://img.example.test/x") as resp:
                self.assertEqual(resp.read(), b"hello")
        self.assertEqual(tried, [_A, _B])

    def test_every_answer_failing_raises_the_last_connection_error(self):
        with mock.patch("backend.security.socket.getaddrinfo", _dns(_A, _B)), \
                mock.patch("backend.security._send_pinned", side_effect=ConnectionRefusedError("down")) as send:
            with self.assertRaises(ConnectionRefusedError):
                open_public_url("https://img.example.test/x")
        self.assertEqual(send.call_count, 2)

    def test_non_public_answer_still_rejects_the_whole_name(self):
        with mock.patch("backend.security.socket.getaddrinfo", _dns(_A, "10.0.0.1")), \
                mock.patch("backend.security._send_pinned") as send:
            with self.assertRaises(security.OutboundPolicyError):
                open_public_url("https://img.example.test/x")
        send.assert_not_called()

    def test_http_error_status_does_not_fall_back(self):
        with mock.patch("backend.security.socket.getaddrinfo", _dns(_A, _B)), \
                mock.patch("backend.security._send_pinned", return_value=_Resp(status=503)) as send:
            with self.assertRaises(urllib.error.HTTPError):
                open_public_url("https://img.example.test/x")
        self.assertEqual(send.call_count, 1)


class CertificateFailureTests(unittest.TestCase):
    def test_certificate_error_is_final_across_addresses(self):
        with mock.patch("backend.security.socket.getaddrinfo", _dns(_A, _B)), \
                mock.patch("backend.security._send_pinned",
                           side_effect=ssl.SSLCertVerificationError(1, "certificate verify failed")) as send:
            with self.assertRaises(ssl.SSLCertVerificationError):
                open_public_url("https://img.example.test/x")
        self.assertEqual(send.call_count, 1)

    def test_certificate_error_is_rejected_not_retried_by_the_boundary(self):
        for exc in (ssl.SSLCertVerificationError(1, "bad"), urllib.error.URLError(ssl.SSLCertVerificationError(1, "bad"))):
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(pb.classify_exception(exc).outcome, pb.ProviderOutcome.REJECTED)
        sleeps = []
        with mock.patch("backend.security.socket.getaddrinfo", _dns(_A)), \
                mock.patch("backend.security._send_pinned",
                           side_effect=ssl.SSLCertVerificationError(1, "bad")) as send:
            with self.assertRaises(ssl.SSLCertVerificationError):
                with pb.opened_public("artwork", "https://img.example.test/x", sleep=sleeps.append):
                    pass
        self.assertEqual(send.call_count, 1)
        self.assertEqual(sleeps, [])

    def test_ordinary_connection_errors_remain_retryable(self):
        self.assertIn(pb.classify_exception(ConnectionRefusedError()).outcome, pb.RETRYABLE)


class _FakeClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


class TotalDeadlineUnitTests(unittest.TestCase):
    """Deterministic: a fake clock advances one second per byte."""

    def test_drip_read_stops_at_the_deadline(self):
        clock = _FakeClock()
        sock = mock.Mock()

        class Drip:
            _bwm_sock = sock

            def read1(self, n):
                clock.now += 1.0
                return b"x"

        resp = LimitedHTTPResponse(Drip(), 10_000, deadline=clock.now + 5.0, op_timeout=20.0)
        with mock.patch.object(security.time, "monotonic", clock.monotonic):
            with self.assertRaises(OutboundDeadlineExceeded):
                resp.read()
        self.assertLessEqual(clock.now - 1000.0, 6.0)
        timeouts = [c.args[0] for c in sock.settimeout.call_args_list]
        self.assertTrue(timeouts and max(timeouts) <= 5.0)
        self.assertEqual(timeouts, sorted(timeouts, reverse=True))

    def test_deadline_exceeded_is_a_timeout(self):
        self.assertTrue(issubclass(OutboundDeadlineExceeded, TimeoutError))

    def test_no_deadline_keeps_plain_reads(self):
        resp = LimitedHTTPResponse(_Resp(body=b"abc"), 100)
        self.assertEqual(resp.read(), b"abc")


class _DripHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "1000")
        self.end_headers()
        try:
            for _ in range(1000):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.2)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, *a):
        pass


class TotalDeadlineRealSocketTests(unittest.TestCase):
    """A real dripping server: each byte arrives well inside the per-op
    timeout, so only the total deadline can stop the read."""

    def test_real_drip_server_is_cut_off(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _DripHandler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        target = PinnedTarget(scheme="http", host="drip.example.test", port=server.server_port,
                              address="127.0.0.1", path="/")
        start = time.monotonic()
        raw = security._send_pinned(target, {}, 5.0)
        resp = LimitedHTTPResponse(raw, 10_000, deadline=start + 1.0, op_timeout=5.0)
        with self.assertRaises(OutboundDeadlineExceeded):
            resp.read()
        raw.close()
        # Generous bound: without the deadline this read takes ~200 s.
        self.assertLess(time.monotonic() - start, 6.0)


if __name__ == "__main__":
    unittest.main()
