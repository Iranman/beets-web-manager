"""QA #379: edge cases for the 0.6.0 acquisition provider fixes.

Cancel on the no-key fallback path, no partial Lidarr list on the second
(unmonitored) pass or a runaway totalRecords, SoundCloud candidates that the
media-host guard must refuse, and no credential in slskd/PO-provider text.
No network: provider calls and DNS are faked.
"""
import logging
import threading
import types
import unittest
import urllib.error
import urllib.request
from unittest import mock

from tests.test_integrations_reliability import _BoundaryCase, _Resp, _WANTED, _wanted_page, http_error

import backend.acquisition_service as acq
import backend.app_runtime as rt
import backend.slskd_service as slskd
import backend.ytdlp_service as yts
import backend.ytdlp_guard as guard
import tests.test_acquisition_providers_060 as t060
from tests.test_acquisition_providers_060 import _AcquisitionJobCase


def setUpModule():
    t060.setUpModule()  # _AcquisitionJobCase reads that module's globals


SECRET = "qa379-not-a-real-key-7f3e"


class NoKeyFallbackCancel(_AcquisitionJobCase):
    def test_cancel_before_the_direct_sources_downloads_nothing(self):
        with mock.patch.object(slskd, "SLSKD_API_KEY", ""), \
                mock.patch.object(acq, "_direct_source_unavailable_reason", return_value=""), \
                mock.patch.object(acq, "_slskd_search_and_queue") as search:
            self.assertTrue(self._start(try_source_fallback=True)["ok"])
            job_fn = self.jobs.start_python.call_args[0][0]
            cancel = threading.Event()
            cancel.set()
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                job_fn([], cancel)
        search.assert_not_called()
        self.assertEqual(self.direct_calls, [])


class SlskdKeyNeverLeaks(_AcquisitionJobCase):
    def test_rejected_key_reaches_log_and_error_with_hint_but_not_the_key(self):
        self.direct_error = "no audio"
        with mock.patch.object(slskd, "SLSKD_API_KEY", SECRET), \
                mock.patch.object(slskd, "SLSKD_URL", "http://slskd.test:5030"), \
                mock.patch.object(acq, "_direct_source_unavailable_reason", return_value=""), \
                mock.patch.object(urllib.request, "urlopen", side_effect=http_error(401, body=SECRET.encode())):
            self._start(try_source_fallback=True)
            _result, log, error = self._run_job()
        self.assertIn("rejected the API key", error)
        self.assertTrue(error.startswith("SLSKD failed: "), error)
        self.assertNotIn(SECRET, error)
        self.assertFalse([line for line in log if SECRET in str(line)])


class LidarrNeverPartial(_BoundaryCase):
    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(acq, "LIDARR_KEY", "k"),
                        mock.patch.object(acq, "LIDARR_URL", "http://lidarr.test:8686")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_runaway_total_records_is_refused_after_one_request(self):
        with mock.patch.object(urllib.request, "urlopen",
                               side_effect=[_wanted_page([_WANTED], total=10 ** 9)]) as urlopen:
            rows, error = acq.fetch_lidarr_wanted()
        self.assertEqual(rows, [])
        self.assertTrue(error)
        self.assertEqual(urlopen.call_count, 1)

    def test_unmonitored_pass_failure_drops_the_monitored_rows_too(self):
        def fake(req, timeout=None):
            if "monitored=false" in req.full_url:
                raise http_error(401)
            return _wanted_page([_WANTED])
        with mock.patch.object(urllib.request, "urlopen", side_effect=fake):
            self.assertEqual(acq.fetch_lidarr_wanted(include_unmonitored=True),
                             ([], "Lidarr rejected the API key"))

    def test_cancel_during_unmonitored_pass_returns_no_partial_list(self):
        checks = iter([False, True])
        with mock.patch.object(urllib.request, "urlopen", side_effect=[_wanted_page([_WANTED])]):
            self.assertEqual(acq.fetch_lidarr_wanted(include_unmonitored=True, cancel=lambda: next(checks)),
                             ([], acq.LIDARR_WANTED_CANCELLED))

    def test_progress_spans_both_passes(self):
        seen = []

        def fake(req, timeout=None):
            if "monitored=false" in req.full_url:
                return _wanted_page([dict(_WANTED, id=9, monitored=False)])
            return _wanted_page([_WANTED])
        with mock.patch.object(urllib.request, "urlopen", side_effect=fake):
            rows, error = acq.fetch_lidarr_wanted(include_unmonitored=True,
                                                  progress=lambda d, t: seen.append((d, t)))
        self.assertEqual((len(rows), error), (2, ""))
        self.assertEqual(seen, [(1, 1), (2, 2)])


class SoundcloudCandidateGuard(unittest.TestCase):
    def test_only_https_soundcloud_results_are_downloaded(self):
        entries = [
            {"webpage_url": "http://soundcloud.com/a/x", "url": "https://soundcloud.com/a/x", "duration": 200},
            {"url": "https://evil.example/a/y", "duration": 200},
            {"url": "ftp://soundcloud.com/a/z", "duration": 200},
            {"url": "https://soundcloud.com/a/good", "duration": 200},
        ]
        constructed = []

        class FakeYDL:
            def __init__(self, opts):
                constructed.append(opts)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, query, download=False):
                return {"entries": entries}

        log = []
        with mock.patch.object(guard, "resolve_public_target"):
            urls = yts._soundcloud_track_candidates(
                types.SimpleNamespace(YoutubeDL=FakeYDL), {}, "scsearch5:x", 200_000, log)
            # The candidate list filters scheme; the media-host guard refuses the
            # rest before yt-dlp is ever constructed for it.
            self.assertEqual(urls, ["https://evil.example/a/y", "https://soundcloud.com/a/good"])
            self.assertFalse(guard.ytdlp_target_allowed("https://evil.example/a/y"))
            self.assertTrue(guard.ytdlp_target_allowed("https://soundcloud.com/a/good"))


class PoProviderCredentialsNeverLogged(unittest.TestCase):
    def setUp(self):
        yts._po_provider_check.update(url="", at=0.0, usable=False)
        self.addCleanup(yts._po_provider_check.update, url="", at=0.0, usable=False)

    def test_credentialed_internal_url_is_dropped_without_echoing_it(self):
        url = f"http://u:{SECRET}@10.0.0.5:4416/path-token"
        with mock.patch.object(yts, "YTDLP_PO_PROVIDER_URL", url), \
                mock.patch.object(rt, "YTDLP_PO_PROVIDER_URL", url), \
                mock.patch.object(urllib.request, "urlopen", side_effect=OSError("down")) as urlopen, \
                self.assertLogs(level=logging.DEBUG) as logs:
            self.assertEqual(yts._ytdlp_base_extractor_args_for_source("ytdlp"), {})
            urlopen.assert_not_called()  # a credential-bearing URL earns no implicit trust
            status = yts._ytdlp_po_provider_status()
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertNotIn(SECRET, repr(status))
        self.assertNotIn("path-token", repr(status))


if __name__ == "__main__":
    unittest.main()
