"""Artwork image URLs supplied by a user or by a provider response are fetched
with the same public-only, address-pinned policy as the reference-URL fetch
(CodeQL #1350, F2): BEETS_OUTBOUND_ALLOWLIST is ignored, the socket connects
to the validated address, and redirects are re-validated hop by hop.

Converted paths:
  * backend.artwork_service._download_album_art_bytes  (POST /api/albums/<id>/art/url,
    _save_art_to_disk / Discogs and candidate artwork)
  * backend.artwork_service._cache_artist_image         (Discogs artist image)
  * backend.musicbrainz_service._release_art_download   (Cover Art Archive / Discogs)
Operator-configured endpoints keep validate_outbound_url() + the allowlist.
"""
import http.client
import io
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import app as app_module
import routes_library
import backend.artwork_service as artwork_service
import backend.musicbrainz_service as musicbrainz_service
from backend.artwork_service import AlbumArtRequestError

_PUBLIC = "93.184.216.34"
_CSRF_HEADERS = {"Origin": "http://localhost", "X-Beets-CSRF": "1"}


def fake_getaddrinfo(*ips):
    def _inner(host, port, *args, **kwargs):
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]
    return _inner


def _by_host(mapping, default=_PUBLIC):
    def _inner(host, port, *args, **kwargs):
        return fake_getaddrinfo(mapping.get(host, default))(host, port)
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


def _png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 10, 10)).save(buf, format="PNG")
    return buf.getvalue()


_ALLOWLIST_ENV = {"BEETS_OUTBOUND_ALLOWLIST": "127.0.0.1:8337,localhost:8337,beets:8337"}


class DownloadAlbumArtBytesTests(unittest.TestCase):
    def test_public_image_is_fetched_from_the_pinned_address(self):
        png = _png_bytes()
        with mock.patch("backend.security.socket.getaddrinfo", fake_getaddrinfo(_PUBLIC)), \
                mock.patch("backend.security._send_pinned",
                           return_value=_FakeResponse(200, png, {"Content-Type": "image/png"})) as send, \
                mock.patch("urllib.request.urlopen") as urlopen:
            data, info = artwork_service._download_album_art_bytes("https://img.example.test/cover.png")
        self.assertEqual(data, png)
        self.assertEqual(info["format"], "PNG")
        self.assertEqual(send.call_args[0][0].address, _PUBLIC)
        urlopen.assert_not_called()

    def test_allowlisted_internal_service_is_rejected_before_any_send(self):
        with mock.patch.dict("os.environ", _ALLOWLIST_ENV), \
                mock.patch("backend.security.socket.getaddrinfo", fake_getaddrinfo("127.0.0.1")), \
                mock.patch("backend.security._send_pinned") as send:
            for url in ("http://127.0.0.1:8337/item/1/file", "http://beets:8337/"):
                with self.subTest(url=url):
                    with self.assertRaises(AlbumArtRequestError) as ctx:
                        artwork_service._download_album_art_bytes(url)
                    self.assertEqual(ctx.exception.status, 400)
        send.assert_not_called()

    def test_dns_rebinding_cannot_redirect_the_connection(self):
        answers = iter([[_PUBLIC], ["127.0.0.1"], ["127.0.0.1"], ["127.0.0.1"]])
        connected = []

        def rebinding(host, port, *args, **kwargs):
            return fake_getaddrinfo(*next(answers))(host, port)

        def fake_create_connection(address, *args, **kwargs):
            connected.append(address)
            raise ConnectionRefusedError("stop before I/O")

        # The route pre-check and the sink each resolve once; patch the
        # pre-check away to look only at the sink's single lookup + connect.
        with mock.patch("backend.security.socket.getaddrinfo", side_effect=rebinding), \
                mock.patch.object(artwork_service, "resolve_public_target"), \
                mock.patch("http.client.socket.create_connection", side_effect=fake_create_connection):
            with self.assertRaises(AlbumArtRequestError):
                artwork_service._download_album_art_bytes("http://rebind.example.test/cover.png")
        self.assertEqual(connected, [(_PUBLIC, 80)])

    def test_redirect_to_metadata_address_is_blocked(self):
        sent = []

        def fake_send(target, headers, timeout):
            sent.append(target.host)
            return _FakeResponse(302, headers={"Location": "http://meta.example.test/latest/meta-data/"})

        with mock.patch("backend.security.socket.getaddrinfo",
                        side_effect=_by_host({"meta.example.test": "169.254.169.254"})), \
                mock.patch("backend.security._send_pinned", side_effect=fake_send):
            with self.assertRaises(AlbumArtRequestError):
                artwork_service._download_album_art_bytes("https://img.example.test/cover.png")
        self.assertEqual(sent, ["img.example.test"])


class CacheArtistImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(artwork_service, "ARTIST_IMAGE_CACHE_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_public_image_is_cached_via_pinned_fetch(self):
        with mock.patch("backend.security.socket.getaddrinfo", fake_getaddrinfo(_PUBLIC)), \
                mock.patch("backend.security._send_pinned",
                           return_value=_FakeResponse(200, _png_bytes(), {"Content-Type": "image/png"})) as send:
            url = artwork_service._cache_artist_image("Some Artist", "https://i.discogs.example.test/a.png")
        self.assertTrue(url.startswith("/api/artist-image-cache/"))
        self.assertEqual(send.call_args[0][0].address, _PUBLIC)

    def test_provider_supplied_internal_url_is_not_fetched(self):
        with mock.patch.dict("os.environ", _ALLOWLIST_ENV), \
                mock.patch("backend.security.socket.getaddrinfo", fake_getaddrinfo("127.0.0.1")), \
                mock.patch("backend.security._send_pinned") as send:
            self.assertEqual(artwork_service._cache_artist_image("A", "http://127.0.0.1:8337/x.png"), "")
        send.assert_not_called()
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_redirect_to_private_address_is_not_followed(self):
        sent = []

        def fake_send(target, headers, timeout):
            sent.append(target.host)
            return _FakeResponse(301, headers={"Location": "http://lan.example.test/admin.png"})

        with mock.patch("backend.security.socket.getaddrinfo",
                        side_effect=_by_host({"lan.example.test": "192.168.1.10"})), \
                mock.patch("backend.security._send_pinned", side_effect=fake_send):
            self.assertEqual(artwork_service._cache_artist_image("A", "https://i.discogs.example.test/a.png"), "")
        self.assertEqual(sent, ["i.discogs.example.test"])


class ReleaseArtDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(musicbrainz_service, "RELEASE_ART_CACHE_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mbid = "0" * 8 + "-0000-0000-0000-" + "1" * 12

    def test_cover_art_archive_redirect_is_followed_and_pinned(self):
        responses = [
            _FakeResponse(307, headers={"Location": "https://ia.archive.example.test/front-250.png"}),
            _FakeResponse(200, _png_bytes(), {"Content-Type": "image/png"}),
        ]
        sent = []

        def fake_send(target, headers, timeout):
            sent.append((target.host, target.address))
            return responses.pop(0)

        with mock.patch("backend.security.socket.getaddrinfo", fake_getaddrinfo(_PUBLIC)), \
                mock.patch("backend.security._send_pinned", side_effect=fake_send):
            url = musicbrainz_service._release_art_download(
                self.mbid, f"https://coverartarchive.org/release-group/{self.mbid}/front-250", "coverartarchive")
        self.assertTrue(url)
        self.assertEqual(sent, [("coverartarchive.org", _PUBLIC), ("ia.archive.example.test", _PUBLIC)])

    def test_provider_supplied_url_to_cgnat_host_is_not_fetched(self):
        with mock.patch("backend.security.socket.getaddrinfo", fake_getaddrinfo("100.64.0.7")), \
                mock.patch("backend.security._send_pinned") as send:
            self.assertEqual(musicbrainz_service._release_art_download(
                self.mbid, "https://img.discogs.example.test/r.png", "discogs"), "")
        send.assert_not_called()


class AlbumArtFromUrlRouteTests(unittest.TestCase):
    def _post(self, url):
        with app_module.app.test_request_context(
            "/api/albums/7/art/url", method="POST",
            data=json.dumps({"url": url}), content_type="application/json", headers=_CSRF_HEADERS,
        ), mock.patch.object(routes_library.lib, "get_album", return_value=mock.Mock(id=7)), \
                mock.patch.object(routes_library.jobs, "start_python",
                                  return_value=mock.Mock(job_id="job-1")) as start:
            response = routes_library.album_replace_art_from_url(7)
        return response, start

    def test_allowlisted_internal_service_is_rejected_at_the_route(self):
        with mock.patch.dict("os.environ", _ALLOWLIST_ENV), \
                mock.patch("backend.security.socket.getaddrinfo", fake_getaddrinfo("127.0.0.1")):
            response, start = self._post("http://127.0.0.1:8337/item/1/file")
        body, status = response
        self.assertEqual(status, 400)
        self.assertEqual(body.get_json()["error"], "Image URL is not allowed")
        start.assert_not_called()

    def test_public_url_starts_the_job(self):
        with mock.patch("backend.security.socket.getaddrinfo", fake_getaddrinfo(_PUBLIC)), \
                mock.patch.object(routes_library, "_album_art_expected_release_group", return_value=""):
            response, start = self._post("https://img.example.test/cover.png")
        start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
