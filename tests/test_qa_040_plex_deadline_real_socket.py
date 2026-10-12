"""QA 0.4.0 re-verification of #339 against a REAL socket.

The #339 unit tests feed _plex_request a fake body whose read(n) returns tiny
chunks. A real http.client.HTTPResponse.read(65536) blocks until 64 KiB or
EOF, so a slow-drip Plex body smaller than that was read in one blocking call
and the deadline was only checked afterwards. End to end at ae99eb0 the index
fetch took 252 s against PLEX_INDEX_TIMEOUT=6, and one title search 19 s
against its 5 s budget.

Fixed: the body is read with read1() (bytes as they arrive) and each recv
waits at most the time left. The bare read1() swap QA tried errored because,
once app is imported, urllib.request.urlopen is the outbound-policy one, which
refuses loopback; the tests open the socket with a plain opener.
"""
import contextlib
import json
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import backend.plex_service as plex_service

BODY = b'{"MediaContainer": {"size": 0, "Metadata": []}}' + b" " * 400  # ~4.5 s at 0.01 s/byte


FAST = b'{"MediaContainer": {"size": 1}}'


class _Drip(BaseHTTPRequestHandler):
    """``/fast...`` answers at once; anything else trickles one byte per
    10 ms. ``chunked`` in the query switches to chunked transfer."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        fast = self.path.startswith("/fast") or (self.server.drip_only and self.server.drip_only not in self.path)
        body = FAST if fast else BODY
        chunked = "chunked" in self.path
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Connection", "close")
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            step = len(body) if fast else 1
            for i in range(0, len(body), step):
                piece = body[i:i + step]
                self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece) if chunked else piece)
                self.wfile.flush()
                if not fast:
                    time.sleep(0.01)
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except OSError:
            pass


@contextlib.contextmanager
def _opened(provider, req, *, timeout=None, max_attempts=None):
    # A real HTTPResponse. A plain opener: once app is imported, urlopen is
    # the policy-checking one, which refuses loopback.
    opener = urllib.request.OpenerDirector()
    opener.add_handler(urllib.request.HTTPHandler())
    with opener.open(req, timeout=timeout) as r:
        yield r


class _RealPlex(unittest.TestCase):
    drip_only = ""

    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Drip)
        self.srv.drip_only = self.drip_only
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        settings = {"url": f"http://127.0.0.1:{self.srv.server_address[1]}", "token": "t"}
        for patcher in (mock.patch.object(plex_service, "_plex_settings", return_value=settings),
                        mock.patch.object(plex_service.provider_boundary, "opened", side_effect=_opened)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _timed(self, path, **kw):
        t0 = time.time()
        with self.assertRaises(TimeoutError):
            plex_service._plex_request(path, attempts=1, **kw)
        return time.time() - t0


class PlexDeadlineRealSocketTests(_RealPlex):
    def test_trickling_body_stops_at_the_deadline(self):
        elapsed = self._timed("/library/sections/3/all", timeout=30, deadline=time.time() + 1.0)
        self.assertLess(elapsed, 2.0, f"deadline 1.0 s, took {elapsed:.1f} s")

    def test_trickling_chunked_body_stops_at_the_deadline(self):
        elapsed = self._timed("/library/sections/3/all?chunked=1", timeout=30, deadline=time.time() + 1.0)
        self.assertLess(elapsed, 2.0, f"deadline 1.0 s, took {elapsed:.1f} s")

    def test_without_a_deadline_the_body_gets_timeout_in_total(self):
        elapsed = self._timed("/identity", timeout=1)
        self.assertLess(elapsed, 2.0, f"timeout 1 s, took {elapsed:.1f} s")

    def test_fast_bodies_are_read_whole(self):
        for path in ("/fast", "/fast?chunked=1"):
            with self.subTest(path=path):
                self.assertEqual(plex_service._plex_request(path, attempts=1, deadline=time.time() + 5),
                                 json.loads(FAST))
                self.assertEqual(plex_service._plex_request(path, attempts=1, timeout=5), json.loads(FAST))


class PlexSlowDripSyncTests(_RealPlex):
    """The index fetch and title searches against a Plex that trickles only
    its /all answers: the whole match step is about PLEX_INDEX_TIMEOUT +
    PLEX_API_TIMEOUT (NB-2, #339)."""
    drip_only = "/all"
    INDEX_TIMEOUT, API_TIMEOUT = 2, 1

    def _match(self, count):
        items = [{"artist": "A", "title": f"T{i}", "path": f"/music/a/{i}.flac"} for i in range(count)]
        log = []
        with mock.patch.multiple(plex_service, PLEX_INDEX_TIMEOUT=self.INDEX_TIMEOUT,
                                 PLEX_API_TIMEOUT=self.API_TIMEOUT, PLEX_SYNC_MAX_FALLBACK_SEARCHES=10,
                                 _plex_is_final_library_path=mock.Mock(return_value=True)), \
                mock.patch.dict(plex_service._PLEX_TRACK_INDEX_CACHE, {}, clear=True):
            t0 = time.time()
            keys, details = plex_service._plex_track_keys_for_items("3", items, log=log, return_details=True)
            return time.time() - t0, keys, details, log

    def test_title_searches_stop_at_their_shared_budget(self):  # NB-2
        with mock.patch.object(plex_service, "_plex_section_track_index", side_effect=OSError("index down")):
            elapsed, keys, details, _ = self._match(5)
        self.assertEqual(keys, [])
        self.assertLess(elapsed, self.API_TIMEOUT + 1.0, f"title-search budget {self.API_TIMEOUT} s, took {elapsed:.1f} s")
        reasons = [row["reason"] for row in details["pending_tracks"]]
        self.assertEqual(len(reasons), 5)
        self.assertIn("Plex title search time limit reached; retry later", reasons)

    def test_slow_drip_index_and_searches_are_bounded_end_to_end(self):
        elapsed, keys, details, log = self._match(5)
        print(f"\n[measured] slow-drip match step: {elapsed:.2f} s "
              f"(PLEX_INDEX_TIMEOUT={self.INDEX_TIMEOUT}, PLEX_API_TIMEOUT={self.API_TIMEOUT})")
        self.assertEqual(keys, [])
        self.assertEqual(details["index_status"], "timeout")
        self.assertLess(elapsed, self.INDEX_TIMEOUT + self.API_TIMEOUT + 1.0)


if __name__ == "__main__":
    unittest.main()