"""Transient MusicBrainz failures are retried by the provider boundary only (BA-5)."""
import io
import ssl
import unittest
import urllib.request
from unittest import mock

import app as app_module  # noqa: F401  -- loads the module family in order
import backend.musicbrainz_service as mbs
import backend.provider_boundary as pb


class MusicBrainzTransientRetryTests(unittest.TestCase):
    def setUp(self):
        pb.reset_provider_health()
        self.addCleanup(pb.reset_provider_health)
        for patcher in (mock.patch.dict("os.environ", {"PROVIDER_MAX_ATTEMPTS": ""}),
                        mock.patch.dict(pb.POLICIES, {"musicbrainz": pb.ProviderPolicy(3, 0.0)}),
                        mock.patch.object(mbs, "_folder_track_search_titles", return_value=["One"]),
                        mock.patch.object(mbs.time, "sleep")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_ssl_eof_is_retried_within_the_policy_then_reported_unavailable(self):
        eof = ssl.SSLEOFError(8, "EOF occurred in violation of protocol (_ssl.c:2427)")
        with mock.patch.object(urllib.request, "urlopen", side_effect=eof) as urlopen:
            with self.assertRaises(pb.ProviderError):
                mbs._mb_release_search_by_folder_tracks("/x", artist="Artist", log=[])
        self.assertEqual(urlopen.call_count, 3)

    def test_ssl_eof_then_success_recovers(self):
        eof = ssl.SSLEOFError(8, "EOF occurred in violation of protocol")
        calls = []

        def urlopen(*_a, **_k):
            calls.append(1)
            if len(calls) == 1:
                raise eof
            ok = io.BytesIO(b'{"recordings": []}')
            ok.status, ok.headers = 200, {}
            return ok

        with mock.patch.object(urllib.request, "urlopen", side_effect=urlopen):
            mbs._mb_release_search_by_folder_tracks("/x", artist="Artist", log=[])
        self.assertGreaterEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
