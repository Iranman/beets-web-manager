"""QA 0.4.0 re-verification of #339 against a REAL socket.

The #339 unit tests feed _plex_request a fake body whose read(n) returns tiny
chunks. A real http.client.HTTPResponse.read(65536) blocks until 64 KiB or
EOF, so a slow-drip Plex body smaller than that was read in one blocking call
and the deadline was only checked afterwards. End to end at ae99eb0 the index
fetch took 252 s against PLEX_INDEX_TIMEOUT=6, and one title search 19 s
against its 5 s budget.

expectedFailure until the owner fixes it. A bare read1() swap tried in QA
made this test error rather than pass (cause not investigated), so it is a
lead, not a verified fix.
"""
import contextlib
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import backend.plex_service as plex_service

BODY = b'{"MediaContainer": {"size": 0, "Metadata": []}}' + b" " * 400  # ~4.5 s at 0.01 s/byte


class _Drip(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        try:
            for i in range(len(BODY)):
                self.wfile.write(BODY[i:i + 1])
                self.wfile.flush()
                time.sleep(0.01)
        except OSError:
            pass


class PlexDeadlineRealSocketTests(unittest.TestCase):
    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Drip)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    @unittest.expectedFailure  # until the owner fixes the blocking body read; remove then
    def test_trickling_body_stops_at_the_deadline(self):
        @contextlib.contextmanager
        def opened(provider, req, *, timeout=None, max_attempts=None):
            # A real HTTPResponse (the outbound policy would refuse loopback).
            with urllib.request.urlopen(req, timeout=timeout) as r:
                yield r

        settings = {"url": f"http://127.0.0.1:{self.srv.server_address[1]}", "token": "t"}
        with mock.patch.object(plex_service, "_plex_settings", return_value=settings), \
                mock.patch.object(plex_service.provider_boundary, "opened", side_effect=opened):
            t0 = time.time()
            with self.assertRaises(TimeoutError):
                plex_service._plex_request("/library/sections/3/all", timeout=30, attempts=1,
                                           deadline=time.time() + 1.0)
            elapsed = time.time() - t0
        self.assertLess(elapsed, 2.0, f"deadline 1.0 s, took {elapsed:.1f} s")


if __name__ == "__main__":
    unittest.main()
