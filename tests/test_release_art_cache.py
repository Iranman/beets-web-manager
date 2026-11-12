"""Persistent release-art cache behind GET /api/albums/<id>/art step 4.

Cover art from Cover Art Archive / Discogs is downloaded once into
METADATA_CACHE_DIR/release-art (keyed by release-group MBID) and served from
disk afterwards. All network I/O is mocked at backend.security._send_pinned.
"""
import http.client
import io
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import app as app_module
import routes_library
import backend.artwork_service as artwork_service
import backend.musicbrainz_service as mbs
import backend.provider_boundary as pb

RG = "11111111-1111-1111-1111-111111111111"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'
_PUBLIC = "93.184.216.34"


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


def _resolve(host, port, *args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC, port))]


class _CacheCase(unittest.TestCase):
    def setUp(self):
        pb.reset_provider_health()
        self.addCleanup(pb.reset_provider_health)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name) / "release-art"
        self.sent = []
        self.responses = []
        for patcher in (
            mock.patch.object(mbs, "RELEASE_ART_CACHE_DIR", self.cache),
            mock.patch.object(artwork_service, "RELEASE_ART_CACHE_DIR", self.cache),
            mock.patch.object(routes_library, "RELEASE_ART_CACHE_DIR", self.cache),
            mock.patch.object(mbs, "DISCOGS_TOKEN", ""),
            mock.patch("backend.security.socket.getaddrinfo", _resolve),
            mock.patch("backend.security._send_pinned", side_effect=self._send),
            mock.patch.dict(pb.POLICIES, {n: pb.ProviderPolicy(p.max_attempts, 0.0) for n, p in pb.POLICIES.items()}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _send(self, target, headers, timeout):
        self.sent.append(target.host)
        return self.responses.pop(0)

    def ok(self, body=PNG):
        return _FakeResponse(200, body, {"Content-Type": "image/png"})


class DownloadOnceTests(_CacheCase):
    def _album(self):
        return SimpleNamespace(id=7, artpath="", mb_releasegroupid=RG, mb_albumid="",
                               albumartist="A", artist="A", album="B")

    def _get(self):
        with app_module.app.test_request_context(f"/api/albums/7/art"), \
                mock.patch.object(routes_library.beets_adapter, "open_album_art", side_effect=RuntimeError("no art")), \
                mock.patch.object(routes_library.lib, "get_album", return_value=self._album()), \
                mock.patch.object(routes_library.lib, "items", return_value=[]):
            resp = routes_library.album_art(7)
            if isinstance(resp, tuple):
                return resp[1], b"", {}
            resp.direct_passthrough = False
            data = resp.get_data()
            resp.close()
            return resp.status_code, data, resp.headers

    def test_first_request_downloads_second_is_served_from_disk(self):
        self.responses = [self.ok()]
        status, body, headers = self._get()
        self.assertEqual((status, body), (200, PNG))
        self.assertIn("max-age=604800", headers.get("Cache-Control", ""))
        self.assertEqual(headers.get("Content-Type"), "image/png")
        self.assertTrue((self.cache / f"{RG}.png").is_file())
        status, body, _ = self._get()
        self.assertEqual((status, body), (200, PNG))
        self.assertEqual(self.sent, ["coverartarchive.org"])  # one remote call total

    def test_nothing_anywhere_is_still_a_404(self):
        self.responses = [_FakeResponse(404)]
        self.assertEqual(self._get()[0], 404)
        self.assertEqual(self._get()[0], 404)  # negative cache: no second remote call
        self.assertEqual(self.sent, ["coverartarchive.org"])

    def test_negative_entry_does_not_hide_art_that_appears_locally(self):
        self.responses = [_FakeResponse(404)]
        self.assertEqual(self._get()[0], 404)
        with tempfile.TemporaryDirectory() as root:
            cover = Path(root) / "Artist" / "Album" / "cover.jpg"
            cover.parent.mkdir(parents=True)
            cover.write_bytes(JPEG + b"\x00" * 2000)
            album = self._album()
            album.artpath = "Artist/Album/cover.jpg"  # relative to MUSIC_ROOT
            with mock.patch.object(routes_library, "MUSIC_ROOT", Path(root)), \
                    mock.patch.object(artwork_service, "MUSIC_ROOT", Path(root)), \
                    mock.patch.object(self, "_album", return_value=album):
                status, body, _ = self._get()
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(JPEG[:4]))

    def test_miss_expires(self):
        self.responses = [_FakeResponse(404)]
        mbs._ensure_release_group_art(RG)
        self.assertEqual(artwork_service._release_art_cache_info(RG), {"miss": True})
        later = time.time() + artwork_service._RELEASE_ART_NEGATIVE_TTL + 1
        with mock.patch.object(artwork_service.time, "time", return_value=later):
            self.assertEqual(artwork_service._release_art_cache_info(RG), {})


class PayloadAndHostTests(_CacheCase):
    def test_svg_and_non_image_payloads_are_rejected(self):
        for body in (SVG, b"<html>not an image</html>" + b" " * 64):
            with self.subTest(body=body[:10]):
                self.responses = [self.ok(body)]
                self.assertEqual(mbs._release_art_download(RG, "https://coverartarchive.org/x", "coverartarchive"), "")
        self.assertFalse(any(self.cache.glob(f"{RG}.*")))

    def test_oversize_payload_is_rejected(self):
        self.responses = [self.ok(PNG + b"\x00" * (mbs._RELEASE_ART_MAX_BYTES + 1))]
        self.assertEqual(mbs._release_art_download(RG, "https://coverartarchive.org/x", "coverartarchive"), "")
        self.assertFalse(any(self.cache.glob(f"{RG}.*")))

    def test_non_allowlisted_or_plain_http_host_is_never_contacted(self):
        for url in ("https://evil.example.com/a.png", "http://coverartarchive.org/a.png",
                    "https://coverartarchive.org.evil.com/a.png", "https://notdiscogs.com/a.png"):
            with self.subTest(url=url):
                self.assertEqual(mbs._release_art_download(RG, url, "discogs"), "")
        self.assertEqual(self.sent, [])

    def test_redirect_off_the_allowlist_is_not_followed(self):
        self.responses = [_FakeResponse(302, headers={"Location": "https://169.254.169.254.nip.io/x.png"})]
        self.assertEqual(mbs._release_art_download(RG, "https://coverartarchive.org/x", "coverartarchive"), "")
        self.assertEqual(self.sent, ["coverartarchive.org"])

    def test_caa_redirect_to_archive_org_is_followed(self):
        self.responses = [_FakeResponse(307, headers={"Location": "https://ia800.us.archive.org/f.png"}), self.ok()]
        self.assertTrue(mbs._release_art_download(RG, "https://coverartarchive.org/x", "coverartarchive"))
        self.assertEqual(self.sent, ["coverartarchive.org", "ia800.us.archive.org"])

    def test_traversal_keys_are_refused(self):
        for key in ("../../etc/passwd", "..\\..\\x", RG + "/../x", RG[:-1] + "/"):
            with self.subTest(key=key):
                self.assertEqual(mbs._ensure_release_group_art(key), {"ok": False, "error": "invalid mbid"})
                self.assertEqual(mbs._release_art_download(key, "https://coverartarchive.org/x", "x"), "")
                self.assertEqual(routes_library.release_art_cache(key), ("", 404))
        self.assertEqual(self.sent, [])
        self.assertFalse(self.cache.exists() and any(self.cache.iterdir()))

    def test_download_refuses_trailing_newline_and_cache_paths_stay_inside(self):
        self.assertEqual(mbs._release_art_download(RG + "\n", "https://coverartarchive.org/x", "x"), "")
        self.assertEqual(self.sent, [])
        self.cache.mkdir(parents=True)
        self.assertEqual(mbs._release_art_cache_file(RG + ".png").parent, Path(os.path.realpath(self.cache)))
        for name in ("../x.png", "sub/x.png", ""):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    mbs._release_art_cache_file(name)
        self.assertEqual(routes_library.release_art_cache(RG + "\n"), ("", 404))

    def test_legacy_entry_mime_is_never_trusted(self):
        self.cache.mkdir(parents=True)
        import json as _json
        for body, stored, served in ((PNG, "text/html", "image/png"), (SVG, "image/svg+xml", None),
                                     (b"<html><script>", "image/jpeg; charset=x", "image/jpeg")):
            with self.subTest(stored=stored):
                (self.cache / f"{RG}.jpg").write_bytes(body)
                (self.cache / f"{RG}.json").write_text(_json.dumps({"image_file": f"{RG}.jpg", "mime": stored}))
                info = artwork_service._release_art_cache_info(RG)
                self.assertEqual(info.get("mime"), served)
                with app_module.app.test_request_context():
                    resp = routes_library.release_art_cache(RG)
                if served is None:
                    self.assertEqual(resp, ("", 404))
                else:
                    self.assertEqual(resp.mimetype, served)
                    resp.close()


class EvictionTests(_CacheCase):
    def test_oldest_images_are_evicted_over_the_cap(self):
        self.cache.mkdir(parents=True)
        ids = [f"{i}" * 8 + "-0000-0000-0000-000000000000" for i in range(1, 5)]
        for age, mbid in enumerate(ids):  # ids[0] newest ... ids[3] oldest
            img = self.cache / f"{mbid}.png"
            img.write_bytes(PNG + b"\x00" * 1000)
            (self.cache / f"{mbid}.json").write_text("{}")
            os.utime(img, (time.time() - age * 100,) * 2)
        size = (self.cache / f"{ids[0]}.png").stat().st_size
        with mock.patch.object(mbs, "_RELEASE_ART_CACHE_MAX_BYTES", size * 2):
            mbs._release_art_evict(keep=f"{ids[3]}.png")
        left = sorted(p.name for p in self.cache.iterdir())
        # ids[3] is oldest but kept (just written); ids[2], ids[1] go; ids[0] stays.
        self.assertEqual(left, sorted([f"{ids[0]}.json", f"{ids[0]}.png", f"{ids[3]}.json", f"{ids[3]}.png"]))


class ConcurrencyTests(_CacheCase):
    def test_concurrent_requests_download_once(self):
        gate = threading.Event()

        def slow_send(target, headers, timeout):
            self.sent.append(target.host)
            gate.wait(5)
            return self.ok()

        results = []
        with mock.patch("backend.security._send_pinned", side_effect=slow_send):
            threads = [threading.Thread(target=lambda: results.append(mbs._ensure_release_group_art(RG)))
                       for _ in range(4)]
            for t in threads:
                t.start()
            time.sleep(0.2)
            gate.set()
            for t in threads:
                t.join(10)
        self.assertEqual(self.sent, ["coverartarchive.org"])
        self.assertEqual(len(results), 4)
        self.assertTrue(all(r["ok"] for r in results))
        self.assertEqual((self.cache / f"{RG}.png").read_bytes(), PNG)


if __name__ == "__main__":
    unittest.main()
