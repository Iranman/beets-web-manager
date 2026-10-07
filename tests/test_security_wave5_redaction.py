"""Security wave 5 regressions: #208 (BEETS_WEB_URL userinfo in adapter
errors/logs), IA-15 (AI provider error body in job log/result) and IA-21
(Discogs token in the query string; unvalidated provider artist id in the
request path). Each test fails on 67f9fd0."""
import io
import json
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import backend.artwork_service as aws
import backend.beets_adapter as ba
import backend.matching_service as ms
import backend.musicbrainz_service as mbs

SECRET = "Sup3rSecretPw"
DISCOGS = "discogs-test-token-0123"


class BeetsAdapterUserinfoRedactionTests(unittest.TestCase):
    # A userinfo BEETS_WEB_URL is now refused before any request (#208 root
    # cause, see tests/test_beets_web_url_userinfo.py).
    def test_connection_and_timeout_errors_never_carry_userinfo(self):
        adapter = ba.BeetsAdapter(base_url=f"http://u:{SECRET}@beets:8337")
        for call in (lambda a: a.get_stats(), lambda a: a.open_item_file(1), lambda a: a.open_album_art(1)):
            with mock.patch.object(ba.urllib.request, "urlopen", side_effect=urllib.error.URLError("x")) as urlopen:
                with self.assertRaises(ba.BeetsAdapterError) as ctx:
                    call(adapter)
            urlopen.assert_not_called()
            self.assertNotIn(SECRET, str(ctx.exception))

    def test_requests_never_use_the_credentialed_url(self):
        adapter = ba.BeetsAdapter(base_url=f"http://u:{SECRET}@beets:8337")
        self.assertNotIn(SECRET, adapter._build_url("/stats"))

    def test_malformed_json_error_is_redacted(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.headers = {"Content-Type": "application/json"}
        resp.read.return_value = b"{not json"
        adapter = ba.BeetsAdapter(base_url=f"http://u:{SECRET}@beets:8337")
        with mock.patch.object(ba.urllib.request, "urlopen", return_value=resp):
            with self.assertRaises(ba.BeetsAdapterError) as ctx:
                adapter.get_stats()
        self.assertNotIn(SECRET, str(ctx.exception))


class AiTrackReviewErrorBodyTests(unittest.TestCase):
    def _review(self, exc):
        log = []
        with mock.patch.object(ms, "_ai_api_key", return_value="k"), \
                mock.patch.object(ms.provider_boundary, "opened", side_effect=exc):
            res = ms._ai_review_album_track_candidates(
                {"album": "A"}, [{"title": "t", "track": 1}], [{"id": 1, "title": "t"}], log=log)
        return res, "\n".join(log)

    def test_http_error_body_is_not_reported(self):
        body = json.dumps({"error": {"message": f"Incorrect API key provided: sk-{SECRET}"}}).encode()
        exc = urllib.error.HTTPError("https://ai.example/v1", 401, "Unauthorized", {}, io.BytesIO(body))
        res, log = self._review(exc)
        self.assertEqual(res["status"], "error")
        self.assertIn("401", res["error"])
        self.assertNotIn(SECRET, res["error"])
        self.assertNotIn(SECRET, log)

    def test_other_errors_are_redacted(self):
        res, log = self._review(RuntimeError(f"failed at https://u:{SECRET}@ai.example/ api_key={SECRET}"))
        self.assertNotIn(SECRET, res["error"])
        self.assertNotIn(SECRET, log)


class _DiscogsResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class DiscogsTokenPlacementTests(unittest.TestCase):
    def _capture(self, call, payloads):
        seen = []
        payloads = list(payloads)

        def opened(provider, req, **kw):
            seen.append(req)
            return _DiscogsResp(json.dumps(payloads.pop(0) if payloads else {}).encode())
        with mock.patch.object(aws, "DISCOGS_TOKEN", DISCOGS), mock.patch.object(mbs, "DISCOGS_TOKEN", DISCOGS), \
                mock.patch.object(aws.provider_boundary, "opened", side_effect=opened), \
                mock.patch.object(mbs.provider_boundary, "opened", side_effect=opened), \
                mock.patch.object(mbs.time, "sleep"):
            result = call()
        return result, seen

    def test_token_is_sent_only_in_the_authorization_header(self):
        calls = [
            lambda: aws._fetch_artist_image("Artist"),
            lambda: aws._fetch_album_art("Artist", "Album"),
            lambda: mbs._discogs_track_search("Title", "Artist"),
            lambda: mbs._fetch_release_group_art_discogs("Artist", "Album"),
            lambda: mbs._discogs_artist_discography("Artist"),
        ]
        pages = [{"results": [{"id": 42, "title": "Artist"}]}, {"releases": []}]
        for call in calls:
            _, seen = self._capture(call, pages)
            self.assertTrue(seen)
            for req in seen:
                self.assertNotIn(DISCOGS, req.full_url)
                self.assertNotIn("token", urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query))
                self.assertEqual(req.get_header("Authorization"), f"Discogs token={DISCOGS}")

    def test_non_integer_artist_id_never_reaches_the_path(self):
        result, seen = self._capture(lambda: mbs._discogs_artist_discography("Artist"),
                                     [{"results": [{"id": "../../users/me", "title": "Artist"}]}])
        self.assertFalse(result["ok"])
        self.assertEqual(len(seen), 1)  # only the search; no /artists/<id> request

    def test_integer_artist_id_is_used(self):
        _, seen = self._capture(lambda: mbs._discogs_artist_discography("Artist"),
                                [{"results": [{"id": "42", "title": "Artist"}]}, {"releases": []}])
        self.assertTrue(seen[1].full_url.startswith("https://api.discogs.com/artists/42/releases?"))


if __name__ == "__main__":
    unittest.main()
