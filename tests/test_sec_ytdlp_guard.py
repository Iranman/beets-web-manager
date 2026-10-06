"""SEC-2 regression tests: yt-dlp must never be pointed at internal URLs.

yt-dlp opens its own connections (the application's outbound URL policy does
not apply), and before the fix POST /api/playlist/parse with
{"source": "url", "content": "http://127.0.0.1:<port>/latest/meta-data/"}
made the server fetch that URL through yt-dlp's generic extractor.
"""
import ast
import http.server
import socket
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import app as app_module  # noqa: F401  (boots the app family)
from backend import playlist_service, ytdlp_guard
from backend.ytdlp_guard import (
    YtdlpTargetRejected, check_ytdlp_target, ytdlp_guarded_options, ytdlp_target_allowed,
)

ROOT = Path(__file__).resolve().parents[1]

_INTERNAL_URLS = (
    "http://127.0.0.1:{port}/latest/meta-data/",
    "http://localhost:{port}/",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5:8686/api/v1/system/status",
    "http://100.64.1.1/",
    "http://[::ffff:127.0.0.1]:{port}/",
    "http://lidarr:8686/",
    "file:///etc/passwd",
    "https://youtube.com.attacker.example/watch?v=x",
    "https://attacker.example/youtube.com/watch?v=x",
)


def _public_dns(host, port, *a, **kw):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.0.1", port or 443))]


class _Recorder(http.server.BaseHTTPRequestHandler):
    hits = []

    def do_GET(self):  # pragma: no cover - must never be reached
        type(self).hits.append(self.path)
        body = b"<html><head><title>INTERNAL</title></head></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        pass


class PlaylistParseNoInternalFetchTests(unittest.TestCase):
    """Real yt-dlp, real loopback listener: zero hits for internal URLs."""

    @classmethod
    def setUpClass(cls):
        _Recorder.hits = []
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_internal_urls_never_reach_the_network(self):
        with mock.patch.object(playlist_service._ytdlp_ready, "wait", return_value=True):
            for template in _INTERNAL_URLS:
                url = template.format(port=self.port)
                with self.subTest(url=url):
                    body, status = playlist_service.parse_playlist_request({"source": "url", "content": url})
                    self.assertFalse(body.get("ok"))
                    self.assertIn("Unsupported playlist URL", body.get("error", ""))
        self.assertEqual(_Recorder.hits, [])

    def test_yt_dlp_is_never_constructed_for_a_rejected_url(self):
        fake = types.ModuleType("yt_dlp")
        fake.YoutubeDL = mock.Mock(side_effect=AssertionError("YoutubeDL must not be constructed"))
        with mock.patch.dict(sys.modules, {"yt_dlp": fake}), \
                mock.patch.object(playlist_service._ytdlp_ready, "wait", return_value=True):
            body, _status = playlist_service.parse_playlist_request(
                {"source": "url", "content": f"http://127.0.0.1:{self.port}/"})
        self.assertFalse(body["ok"])
        fake.YoutubeDL.assert_not_called()

    def test_allowlisted_host_resolving_privately_is_rejected(self):
        def private_dns(host, port, *a, **kw):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port or 443))]
        with mock.patch("socket.getaddrinfo", side_effect=private_dns):
            self.assertFalse(ytdlp_target_allowed("https://www.youtube.com/playlist?list=PL1"))

    def test_allowlisted_public_url_gets_guarded_options(self):
        fake_ydl = mock.MagicMock()
        fake_ydl.__enter__ = mock.Mock(return_value=fake_ydl)
        fake_ydl.__exit__ = mock.Mock(return_value=False)
        fake_ydl.extract_info.return_value = {"_type": "playlist", "entries": []}
        fake = types.ModuleType("yt_dlp")
        fake.YoutubeDL = mock.Mock(return_value=fake_ydl)
        with mock.patch.dict(sys.modules, {"yt_dlp": fake}), \
                mock.patch("socket.getaddrinfo", side_effect=_public_dns), \
                mock.patch.object(playlist_service, "_ytdlp_source_extractor_args", return_value={}), \
                mock.patch.object(playlist_service._ytdlp_ready, "wait", return_value=True):
            try:
                playlist_service.parse_playlist_request(
                    {"source": "url", "content": "https://www.youtube.com/playlist?list=PL1"})
            except Exception:
                pass  # downstream library matching is irrelevant here
        fake.YoutubeDL.assert_called_once()
        opts = fake.YoutubeDL.call_args.args[0]
        self.assertIn("-generic", opts["allowed_extractors"])


class ReferenceUrlYtdlpTests(unittest.TestCase):
    def test_non_media_host_falls_back_without_constructing_yt_dlp(self):
        import routes_submissions
        fake = types.ModuleType("yt_dlp")
        fake.YoutubeDL = mock.Mock(side_effect=AssertionError("YoutubeDL must not be constructed"))
        with mock.patch.dict(sys.modules, {"yt_dlp": fake}), \
                mock.patch("socket.getaddrinfo", side_effect=_public_dns), \
                mock.patch.object(routes_submissions._ytdlp_ready, "wait", return_value=True):
            with self.assertRaises(routes_submissions._YtdlpUnsupportedUrlError):
                routes_submissions._extract_ytdlp_info("https://example.org/some/article")
        fake.YoutubeDL.assert_not_called()


class GuardUnitTests(unittest.TestCase):
    def test_search_queries_are_allowed_without_dns(self):
        with mock.patch("socket.getaddrinfo", side_effect=AssertionError("no DNS for search")):
            for q in ("ytsearch5:artist album", "ytsearch:artist album full album", "scsearch1:x y", "ytsearchall:z"):
                check_ytdlp_target(q)

    def test_rejections(self):
        with mock.patch("socket.getaddrinfo", side_effect=_public_dns):
            for bad in ("", "javascript:alert(1)", "ftp://youtube.com/x", "https://evil.example/x",
                        "https://youtube.com.evil.example/x", "https://user:pw@youtube.com/x", "-o /tmp/x"):
                with self.subTest(target=bad):
                    with self.assertRaises(YtdlpTargetRejected):
                        check_ytdlp_target(bad)

    def test_subdomains_of_allowed_hosts(self):
        with mock.patch("socket.getaddrinfo", side_effect=_public_dns):
            for good in ("https://music.youtube.com/playlist?list=1", "https://youtu.be/abc",
                         "https://artist.bandcamp.com/album/x", "https://soundcloud.com/a/sets/b"):
                with self.subTest(target=good):
                    check_ytdlp_target(good)

    def test_guarded_options_copy_and_disable_generic(self):
        original = {"quiet": True}
        guarded = ytdlp_guarded_options(original, ["ytsearch1:x"])
        self.assertEqual(guarded["allowed_extractors"], ["default", "-generic"])
        self.assertNotIn("allowed_extractors", original)
        with self.assertRaises(YtdlpTargetRejected):
            ytdlp_guarded_options(original, ["http://127.0.0.1/"])

    def test_real_yt_dlp_honours_the_exclusion(self):
        import yt_dlp
        ydl = yt_dlp.YoutubeDL(ytdlp_guarded_options({"quiet": True}, []))
        self.assertNotIn("Generic", ydl._ies)
        self.assertIn("Youtube", ydl._ies)


class EveryYoutubeDlCallIsGuardedTests(unittest.TestCase):
    """Structural guard: every yt_dlp.YoutubeDL(...) in application code takes
    its options from ytdlp_guarded_options(...), which enforces the host
    allowlist and disables the generic extractor."""

    def _modules(self):
        files = [ROOT / "app.py", ROOT / "helpers_mb.py", ROOT / "job_engine.py"]
        files += sorted(ROOT.glob("routes_*.py")) + sorted((ROOT / "backend").glob("*.py"))
        return files

    def test_every_youtubedl_call_site_uses_the_guard(self):
        seen, offenders = 0, []
        for path in self._modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name != "YoutubeDL":
                    continue
                seen += 1
                first = node.args[0] if node.args else None
                inner = getattr(first, "func", None)
                inner_name = getattr(inner, "id", "") or getattr(inner, "attr", "")
                if not (isinstance(first, ast.Call) and inner_name == "ytdlp_guarded_options" and len(first.args) == 2):
                    offenders.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
        self.assertGreaterEqual(seen, 8)
        self.assertEqual(offenders, [], "wrap yt-dlp options with ytdlp_guarded_options(opts, targets)")

    def test_guard_module_exposes_no_bypass(self):
        self.assertEqual(ytdlp_guard.YTDLP_ALLOWED_EXTRACTORS, ["default", "-generic"])


if __name__ == "__main__":
    unittest.main()
