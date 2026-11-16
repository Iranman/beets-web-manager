"""GET /api/release-art `level` whitelist (CAA release vs release-group entity).

The edition thumbnails on the import-review cards key off a RELEASE mbid, so the
fetcher must build the Cover Art Archive `release/{mbid}` URL when level=release,
default to `release-group`, and never interpolate an unvalidated level into the
host path. Network I/O is replaced by capturing the URL at `_release_art_download`.
"""
import unittest
from unittest import mock

import app as app_module
import routes_library
import backend.musicbrainz_service as mbs

MBID = "11111111-1111-1111-1111-111111111111"


class ReleaseArtLevelTests(unittest.TestCase):
    def setUp(self):
        self.urls = []

        def _capture(mbid, url, source):
            self.urls.append(url)
            return f"/api/release-art-cache/{mbid}?v=1"

        for patcher in (
            mock.patch.object(mbs, "_release_art_download", side_effect=_capture),
            mock.patch.object(mbs, "_release_art_cache_info", return_value={}),
            mock.patch.object(mbs, "_fetch_release_group_art_discogs", return_value=""),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _call(self, query):
        with app_module.app.test_request_context(query):
            return routes_library.release_art_api().get_json()

    def test_level_release_builds_release_url(self):
        res = self._call(f"/api/release-art?level=release&mbid={MBID}")
        self.assertTrue(res["ok"])
        self.assertEqual(self.urls, [f"https://coverartarchive.org/release/{MBID}/front-250"])

    def test_injection_level_coerces_to_release_group(self):
        res = self._call(f"/api/release-art?level=../evil&mbid={MBID}")
        self.assertTrue(res["ok"])
        self.assertEqual(self.urls, [f"https://coverartarchive.org/release-group/{MBID}/front-250"])

    def test_default_level_is_release_group(self):
        res = self._call(f"/api/release-art?mbid={MBID}")
        self.assertTrue(res["ok"])
        self.assertEqual(self.urls, [f"https://coverartarchive.org/release-group/{MBID}/front-250"])


if __name__ == "__main__":
    unittest.main()
