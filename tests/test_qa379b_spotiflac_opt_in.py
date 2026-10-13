"""QA #379 re-check (F1/F2): SpotiFLAC runs only when SLSKD_FALLBACK_METHODS lists it.

End to end through the job body: slskd fails, SpotiFLAC is "installed" (every
source reports available), and each real caller's payload -- Find Music as the
frontend sends it (fallback_method=spotiflac), Download All, track replacement --
must try exactly the configured chain. No network: every download is faked.
"""
import unittest
from unittest import mock

import backend.acquisition_service as acq
import backend.replacement_service as replacement
import backend.slskd_service as slskd
import tests.test_acquisition_providers_060 as t060
from tests.test_acquisition_providers_060 import _AcquisitionJobCase


def setUpModule():
    t060.setUpModule()  # _AcquisitionJobCase reads that module's globals


ROW = {"artist": "A", "albumartist": "A", "album": "B", "title": "T", "track": 1}


class SpotiflacOptInEndToEnd(_AcquisitionJobCase):
    def _chain_tried(self, payload, configured):
        self.direct_error = "no audio"
        self.jobs.start_python.reset_mock()
        self.direct_calls = []

        def spotiflac(*_a, **_k):
            self.direct_calls.append("spotiflac")
            raise RuntimeError("no audio")

        def missing(artist, album, year, dest_dir, log, tracks, source="ytdlp"):
            return self._direct_download(artist, album, year, dest_dir, log, source=source)

        with mock.patch.object(slskd, "SLSKD_API_KEY", "k"), \
                mock.patch.object(slskd, "_SLSKD_FALLBACK_METHODS", configured), \
                mock.patch.object(acq, "_slskd_search_and_queue", side_effect=RuntimeError("slskd x timed out")), \
                mock.patch.object(acq, "_direct_source_unavailable_reason", return_value=""), \
                mock.patch.object(acq, "_spotiflac_album_download", side_effect=spotiflac), \
                mock.patch.object(acq, "_spotiflac_missing_tracks_download", side_effect=spotiflac), \
                mock.patch.object(acq, "_ytdlp_missing_tracks_download", side_effect=missing):
            # Identity resolution is not under test; keep the job off MusicBrainz.
            body = acq.start_album_download(dict(payload, auto_import=False, mb_albumid=""))[0]
            self.assertTrue(body["ok"], body)
            chain = acq._download_sources_status()["fallback_chain"]
            _result, log, error = self._run_job()
        self.assertIsNotNone(error, log)
        self.assertEqual(self.direct_calls, chain)  # what is reported is what runs
        return self.direct_calls, log

    def _payloads(self):
        find_music = {"artist": "A", "album": "B", "method": "slskd", "fallback_method": "spotiflac"}
        download_all = acq._acq_download_payload(
            {"artist": "A", "album": "B", "mbid": "00000000-0000-0000-0000-000000000001"}, "slskd", True)
        replace = replacement._music_format_replacement_payload(
            ROW, {"replacement_fallback": {"try_alternate_source": True}}, "slskd")
        return {"find_music": find_music, "download_all": download_all, "replacement": replace}

    def test_installed_but_unconfigured_spotiflac_never_runs(self):
        for name, payload in self._payloads().items():
            with self.subTest(caller=name):
                calls, log = self._chain_tried(payload, "ytdlp,soundcloud")
                self.assertEqual(calls, ["ytdlp", "soundcloud"])
                self.assertFalse([line for line in log if "SpotiFLAC" in str(line)], log)

    def test_configured_spotiflac_still_runs_in_configured_order(self):
        for name, payload in self._payloads().items():
            with self.subTest(caller=name):
                calls, _log = self._chain_tried(payload, "soundcloud,ytdlp,spotiflac")
                self.assertEqual(calls, ["soundcloud", "ytdlp", "spotiflac"])

    def test_replacement_with_alternate_source_off_tries_no_direct_source(self):
        payload = replacement._music_format_replacement_payload(
            ROW, {"replacement_fallback": {"try_alternate_source": False}}, "slskd")
        with mock.patch.object(slskd, "SLSKD_API_KEY", "k"), \
                mock.patch.object(slskd, "_SLSKD_FALLBACK_METHODS", "spotiflac,ytdlp"), \
                mock.patch.object(acq, "_slskd_search_and_queue", side_effect=RuntimeError("slskd x timed out")), \
                mock.patch.object(acq, "_direct_source_unavailable_reason", return_value="") as availability, \
                mock.patch.object(acq, "_spotiflac_missing_tracks_download") as spotiflac, \
                mock.patch.object(acq, "_ytdlp_missing_tracks_download") as ytdlp:
            self.assertTrue(acq.start_album_download(dict(payload, auto_import=False))[0]["ok"])
            _result, _log, error = self._run_job()
        self.assertIn("slskd x timed out", error or "")
        self.assertEqual(self.direct_calls, [])
        availability.assert_not_called()
        spotiflac.assert_not_called()
        ytdlp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
