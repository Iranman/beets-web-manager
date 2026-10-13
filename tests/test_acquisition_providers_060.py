"""0.6.0 acquisition provider fixes (ACQ-1/2/3/5/6/7/12/13).

slskd preflight and real-error surfacing, fallback availability filtering,
the SpotiFLAC-free default chain, the opt-in PO token provider, SoundCloud
multi-candidate search and progress-line throttling. Every provider and
downloader is faked: no network, no credentials.
"""
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

acq = rt = replacement = slskd = yts = OutboundPolicyError = None


def setUpModule():
    # Deferred to run time: this module sorts before test_ai_batch_retry_race,
    # which must be the first to import app (at collection, in its temp env).
    global acq, rt, replacement, slskd, yts, OutboundPolicyError
    import app  # noqa: F401  -- loads the module family in order
    import backend.acquisition_service as acq
    import backend.app_runtime as rt
    import backend.replacement_service as replacement
    import backend.slskd_service as slskd
    import backend.ytdlp_service as yts
    from backend.security import OutboundPolicyError

NOT_CONFIGURED = "slskd is not configured (SLSKD_API_KEY)"


class _AcquisitionJobCase(unittest.TestCase):
    """Starts start_album_download with the job engine faked and returns the job body."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        downloads = Path(tmp.name).resolve()
        self.fallback = mock.Mock(side_effect=self._direct_download)
        self.direct_calls = []
        self.direct_error = None
        patches = [
            mock.patch.object(rt, "DOWNLOADS_ALLOWED_ROOTS", (downloads,)),
            mock.patch.object(rt, "TORRENT_SOURCE_ROOTS", ()),
            mock.patch.object(slskd, "DOWNLOADS_ROOT", downloads),
            mock.patch.object(slskd.time, "sleep"),
            mock.patch.object(acq, "_ytdlp_album_download", side_effect=self._direct_download),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        jobs_patcher = mock.patch.object(acq, "jobs")
        self.jobs = jobs_patcher.start()
        self.addCleanup(jobs_patcher.stop)

    def _direct_download(self, artist, album, year, dest_dir, log, track_count=0, source="ytdlp"):
        self.direct_calls.append(source)
        if self.direct_error:
            raise RuntimeError(self.direct_error)
        return 1

    def _start(self, **payload):
        body, status = acq.start_album_download(dict(
            {"artist": "A", "album": "B", "method": "slskd", "auto_import": False}, **payload))
        return body

    def _run_job(self):
        job_fn = self.jobs.start_python.call_args[0][0]
        log = []
        try:
            return job_fn(log, threading.Event()), log, None
        except RuntimeError as exc:
            return None, log, str(exc)


class SlskdPreflightTests(_AcquisitionJobCase):  # ACQ-1
    def test_missing_key_without_fallback_is_refused_before_any_job(self):
        with mock.patch.object(slskd, "SLSKD_API_KEY", ""):
            body = self._start()
        self.assertEqual(body, {"ok": False, "error": NOT_CONFIGURED})
        self.jobs.start_python.assert_not_called()

    def test_missing_key_with_fallback_goes_straight_to_direct_sources(self):
        with mock.patch.object(slskd, "SLSKD_API_KEY", ""), \
                mock.patch.object(acq, "_direct_source_unavailable_reason", return_value=""), \
                mock.patch.object(acq, "_slskd_search_and_queue") as search:
            body = self._start(try_source_fallback=True)
            self.assertTrue(body["ok"], body)
            result, log, error = self._run_job()
        self.assertIsNone(error, log)
        search.assert_not_called()
        self.assertIn(f"[slskd] Skipped: {NOT_CONFIGURED}; going straight to direct sources.", log)
        self.assertEqual(result["method"], "ytdlp")
        self.assertEqual(result["slskd_error"], NOT_CONFIGURED)

    def test_missing_key_and_no_available_fallback_fails_clearly(self):
        with mock.patch.object(slskd, "SLSKD_API_KEY", ""), \
                mock.patch.object(acq, "_direct_source_unavailable_reason",
                                  return_value="yt-dlp is not installed"):
            self._start(try_source_fallback=True)
            _result, log, error = self._run_job()
        self.assertEqual(error, f"SLSKD failed: {NOT_CONFIGURED}; no direct-source fallback is available")
        self.assertIn("[fallback] Skipped YouTube: yt-dlp is not installed", log)
        self.assertEqual(self.direct_calls, [])

    def test_configured_key_is_not_refused(self):
        with mock.patch.object(slskd, "SLSKD_API_KEY", "k"):
            self.assertTrue(self._start()["ok"])


class SlskdErrorSurfacingTests(_AcquisitionJobCase):  # ACQ-2
    def _fail_slskd_with(self, message):
        with mock.patch.object(slskd, "SLSKD_API_KEY", "k"), \
                mock.patch.object(acq, "_direct_source_unavailable_reason", return_value=""), \
                mock.patch.object(acq, "_slskd_search_and_queue", side_effect=RuntimeError(message)):
            self._start(try_source_fallback=True)
            return self._run_job()

    def test_real_slskd_error_is_logged_and_first_in_the_final_error(self):
        self.direct_error = "yt-dlp: no audio files downloaded"
        for slskd_error in ("slskd POST /searches → HTTP 401 (slskd rejected the API key; check SLSKD_API_KEY)",
                            "slskd POST /searches → HTTP 429 (slskd is rate limiting requests)",
                            "slskd POST /searches timed out",
                            "slskd POST /searches is unreachable",
                            "slskd POST /searches returned an unexpected response"):
            with self.subTest(slskd_error=slskd_error):
                _result, log, error = self._fail_slskd_with(slskd_error)
                self.assertTrue(error.startswith(f"SLSKD failed: {slskd_error}; direct-source fallback"), error)
                self.assertIn(f"[slskd] Failed: {slskd_error}", log)

    def test_credentials_in_an_error_are_redacted(self):
        self.direct_error = "nope"
        _result, log, error = self._fail_slskd_with("fetch http://user:hunter2@slskd:5030 failed")
        self.assertNotIn("hunter2", error)
        self.assertFalse([line for line in log if "hunter2" in str(line)])

    def test_success_needs_no_fallback(self):
        with mock.patch.object(slskd, "SLSKD_API_KEY", "k"), \
                mock.patch.object(acq, "_slskd_search_and_queue",
                                  return_value=("peer", ["Music\\B\\01.flac"], "", "Music\\B")), \
                mock.patch.object(acq, "_slskd_wait_downloads", return_value=("/x", [])), \
                mock.patch.object(acq, "_find_slskd_downloaded_files", return_value=("/x", ["/x/01.flac"])), \
                mock.patch.object(acq, "_direct_source_unavailable_reason") as availability:
            self._start(try_source_fallback=True)
            result, log, error = self._run_job()
        self.assertIsNone(error, log)
        self.assertEqual(result["method"], "slskd")
        availability.assert_not_called()  # checked only when a fallback is needed


class FallbackChainTests(_AcquisitionJobCase):  # ACQ-5, ACQ-12
    def test_unavailable_fallbacks_are_skipped_once_per_job_with_a_reason(self):
        reasons = {"spotiflac": "SpotiFLAC CLI is not installed (SPOTIFLAC_CMD)", "ytdlp": "", "soundcloud": ""}
        self.direct_error = "no audio"
        with mock.patch.object(slskd, "SLSKD_API_KEY", "k"), \
                mock.patch.object(acq, "_slskd_search_and_queue", side_effect=RuntimeError("slskd x timed out")), \
                mock.patch.object(acq, "_direct_source_unavailable_reason",
                                  side_effect=lambda m: reasons[m]) as availability:
            with mock.patch.object(slskd, "_SLSKD_FALLBACK_METHODS", "spotiflac,ytdlp,soundcloud"):
                self._start(try_source_fallback=True)
            _result, log, error = self._run_job()
        self.assertIn("[fallback] Skipped SpotiFLAC: SpotiFLAC CLI is not installed (SPOTIFLAC_CMD)", log)
        self.assertEqual(self.direct_calls, ["ytdlp", "soundcloud"])
        self.assertEqual(availability.call_count, 3)
        self.assertIn("YouTube: no audio; SoundCloud: no audio", error)

    def _metadata(self, payload):
        self.jobs.start_python.reset_mock()
        with mock.patch.object(slskd, "SLSKD_API_KEY", "k"):
            self._start(**payload)
        return self.jobs.start_python.call_args.kwargs["metadata"]

    @unittest.skipIf("SLSKD_FALLBACK_METHODS" in os.environ, "operator override set")
    def test_default_chain_has_no_spotiflac(self):
        self.assertEqual(slskd._slskd_fallback_methods(), ["ytdlp", "soundcloud"])
        self.assertEqual(self._metadata({"try_source_fallback": True})["fallback_methods"],
                         ["ytdlp", "soundcloud"])

    def test_requested_fallback_method_cannot_add_or_reorder_sources(self):  # QA F1
        with mock.patch.object(slskd, "_SLSKD_FALLBACK_METHODS", "ytdlp,soundcloud"):
            meta = self._metadata({"fallback_method": "spotiflac"})
            self.assertTrue(meta["try_source_fallback"])  # it may still switch the fallback on
            self.assertEqual(meta["fallback_methods"], ["ytdlp", "soundcloud"])
            self.assertEqual(acq._download_sources_status()["fallback_chain"], meta["fallback_methods"])

    def test_spotiflac_runs_only_when_configured(self):
        with mock.patch.object(slskd, "_SLSKD_FALLBACK_METHODS", "spotiflac,ytdlp,soundcloud"):
            self.assertEqual(self._metadata({"try_source_fallback": True})["fallback_methods"],
                             ["spotiflac", "ytdlp", "soundcloud"])

    def test_download_all_child_job_uses_the_configured_chain(self):  # QA F1
        item = {"artist": "A", "album": "B", "mbid": "00000000-0000-0000-0000-000000000001"}
        payload = acq._acq_download_payload(item, "slskd", True)
        self.assertNotIn("fallback_method", payload)
        with mock.patch.object(slskd, "_SLSKD_FALLBACK_METHODS", "ytdlp,soundcloud"):
            meta = self._metadata(payload)
            self.assertTrue(meta["try_source_fallback"])
            self.assertEqual(meta["fallback_methods"], ["ytdlp", "soundcloud"])
            self.assertEqual(acq._download_sources_status()["fallback_chain"], meta["fallback_methods"])

    def test_replacement_honours_try_alternate_source(self):  # QA F1 + F2
        row = {"artist": "A", "albumartist": "A", "album": "B", "title": "T", "track": 1}
        with mock.patch.object(slskd, "_SLSKD_FALLBACK_METHODS", "ytdlp,soundcloud"):
            for enabled in (True, False):
                with self.subTest(try_alternate_source=enabled):
                    prefs = {"replacement_fallback": {"try_alternate_source": enabled}}
                    payload = replacement._music_format_replacement_payload(row, prefs, "slskd")
                    self.assertNotIn("fallback_method", payload)
                    meta = self._metadata(payload)
                    self.assertIs(meta["try_source_fallback"], enabled)
                    self.assertEqual(meta["fallback_methods"], ["ytdlp", "soundcloud"])


class SourceAvailabilityTests(unittest.TestCase):  # ACQ-5
    def test_reasons(self):
        with mock.patch.object(yts.importlib.util, "find_spec", return_value=None):
            self.assertEqual(yts._direct_source_unavailable_reason("ytdlp"), "yt-dlp is not installed")
            self.assertEqual(yts._direct_source_unavailable_reason("soundcloud"), "yt-dlp is not installed")
        with mock.patch.object(yts.importlib.util, "find_spec", return_value=object()), \
                mock.patch.object(yts, "_ytdlp_js_runtime_status", return_value={"available": False}):
            self.assertEqual(yts._direct_source_unavailable_reason("ytdlp"),
                             "no JavaScript runtime (Deno) for YouTube")
            self.assertEqual(yts._direct_source_unavailable_reason("soundcloud"), "")
        with mock.patch.object(yts.importlib.util, "find_spec", return_value=object()), \
                mock.patch.object(yts, "_ytdlp_js_runtime_status", return_value={"available": True}):
            self.assertEqual(yts._direct_source_unavailable_reason("youtube"), "")
        with mock.patch.object(yts, "_spotiflac_status", return_value={"available": False}):
            self.assertIn("SPOTIFLAC_CMD", yts._direct_source_unavailable_reason("spotiflac"))
        with mock.patch.object(yts, "_spotiflac_status", return_value={"available": True}):
            self.assertEqual(yts._direct_source_unavailable_reason("spotiflac"), "")

    def test_queue_payload_reports_slskd_and_fallbacks(self):
        acq._source_probe_cache.update(at=0.0, fallbacks=None)  # probes are cached (QA F6)
        self.addCleanup(acq._source_probe_cache.update, at=0.0, fallbacks=None)
        with mock.patch.object(slskd, "SLSKD_API_KEY", ""), \
                mock.patch.object(acq, "_direct_source_unavailable_reason",
                                  side_effect=lambda m: "" if m == "soundcloud" else "missing"):
            status = acq._download_sources_status()
        self.assertFalse(status["slskd_configured"])
        self.assertEqual(status["slskd_error"], NOT_CONFIGURED)
        self.assertEqual(status["fallbacks"]["soundcloud"], {"available": True, "reason": ""})
        self.assertEqual(status["fallbacks"]["ytdlp"], {"available": False, "reason": "missing"})
        acq._source_probe_cache.update(at=0.0, fallbacks=None)
        with mock.patch.object(slskd, "SLSKD_API_KEY", "secret-value"), \
                mock.patch.object(acq, "_direct_source_unavailable_reason", return_value=""):
            status = acq._download_sources_status()
        self.assertEqual((status["slskd_configured"], status["slskd_error"]), (True, ""))
        self.assertNotIn("secret-value", repr(status))


class PoProviderTests(unittest.TestCase):  # ACQ-3, ACQ-13
    URL = "http://bgutil-provider:4416"

    def setUp(self):
        yts._po_provider_check.update(url="", at=0.0, usable=False)
        self.addCleanup(yts._po_provider_check.update, url="", at=0.0, usable=False)

    def test_blank_by_default(self):
        if "YTDLP_PO_PROVIDER_URL" not in os.environ:
            self.assertEqual(rt.YTDLP_PO_PROVIDER_URL, "")
        with mock.patch.object(yts, "YTDLP_PO_PROVIDER_URL", ""), \
                mock.patch.object(yts, "_ytdlp_po_provider_status") as probe:
            self.assertEqual(yts._ytdlp_base_extractor_args_for_source("ytdlp"), {})
        probe.assert_not_called()

    def test_reachable_provider_is_used_with_implicit_trust(self):
        rule = object()
        with mock.patch.object(yts, "YTDLP_PO_PROVIDER_URL", self.URL), \
                mock.patch.object(yts, "implicit_rule_for_url", return_value=rule) as implicit, \
                mock.patch.object(yts, "validate_outbound_url") as validate, \
                mock.patch.object(yts, "_ytdlp_po_provider_status", return_value={"reachable": True}) as probe:
            args = yts._ytdlp_base_extractor_args_for_source("ytdlp")
            yts._ytdlp_base_extractor_args_for_source("ytdlp")  # cached: no second probe
        self.assertEqual(args, {"youtubepot-bgutilhttp": {"base_url": [self.URL]}})
        implicit.assert_called_with(self.URL)
        validate.assert_called_once_with(self.URL, trusted=rule)
        probe.assert_called_once()
        self.assertEqual(yts._ytdlp_base_extractor_args_for_source("soundcloud"), {})

    def test_blocked_provider_is_dropped_with_a_warning_not_a_failure(self):
        with mock.patch.object(yts, "YTDLP_PO_PROVIDER_URL", self.URL), \
                mock.patch.object(yts, "validate_outbound_url",
                                  side_effect=OutboundPolicyError("blocked", entry="bgutil-provider:4416")), \
                mock.patch.object(yts, "_ytdlp_po_provider_status") as probe, \
                self.assertLogs("app", "WARNING") as logs:
            self.assertEqual(yts._ytdlp_base_extractor_args_for_source("ytdlp"), {})
        probe.assert_not_called()
        self.assertIn("blocked by the outbound network policy", logs.output[0])

    def test_status_never_echoes_userinfo_or_path(self):
        with mock.patch.object(yts, "YTDLP_PO_PROVIDER_URL", "http://u:hunter2@10.0.0.5:4416/tok"), \
                mock.patch.object(yts.provider_boundary, "opened", side_effect=OSError("down")):
            status = yts._ytdlp_po_provider_status()
        self.assertNotIn("hunter2", repr(status))
        self.assertNotIn("/tok", repr(status))
        self.assertTrue(status["url"].startswith("http://10.0.0.5:4416"))

    def test_unreachable_provider_is_dropped_with_a_warning(self):
        with mock.patch.object(yts, "YTDLP_PO_PROVIDER_URL", self.URL), \
                mock.patch.object(yts, "validate_outbound_url"), \
                mock.patch.object(yts, "_ytdlp_po_provider_status",
                                  return_value={"reachable": False, "error": "Could not reach the PO token provider."}), \
                self.assertLogs("app", "WARNING") as logs:
            self.assertEqual(yts._ytdlp_source_extractor_args("ytdlp", {"youtube": {"x": ["y"]}}),
                             {"youtube": {"x": ["y"]}})
        self.assertIn("unreachable", logs.output[0])


class ProgressThrottleTests(unittest.TestCase):  # ACQ-7
    def test_only_first_and_last_progress_lines_are_kept(self):
        log = []
        logger = yts._YtdlpJobLogger(log, {"count": 0})
        for line in ("[download] Destination: /x/song.webm",
                     "[download]   0.0% of    5.00MiB at  Unknown B/s ETA Unknown",
                     "[download]  12.3% of    5.00MiB at  1.00MiB/s ETA 00:04",
                     "[download] \x1b[0;94m 55.0%\x1b[0m of 5.00MiB",
                     "[download]  99.9% of    5.00MiB at  1.00MiB/s ETA 00:00",
                     "[download] 100% of    5.00MiB in 00:00:05 at 1.00MiB/s",
                     "[debug] noise"):
            logger.debug(line)
        self.assertEqual(log, ["  [yt-dlp] [download] Destination: /x/song.webm",
                               "  [yt-dlp] [download]   0.0% of    5.00MiB at  Unknown B/s ETA Unknown",
                               "  [yt-dlp] [download] 100% of    5.00MiB in 00:00:05 at 1.00MiB/s"])

    def test_bot_check_errors_are_still_counted(self):
        seen = {"count": 0}
        with mock.patch.object(yts, "_yt_bot_check_message", return_value=True):
            yts._YtdlpJobLogger([], seen).error("Sign in to confirm you're not a bot")
        self.assertEqual(seen["count"], 1)


def _fake_yt_dlp(entries, dest_dir, downloads):
    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, query, download=False):
            assert self.opts.get("extract_flat") == "in_playlist" and not download
            assert query.startswith("scsearch5:"), query
            return {"entries": entries}

        def download(self, targets):
            downloads.append(targets[0])
            if targets[0].endswith("/drm"):
                raise RuntimeError("ERROR: [soundcloud] 1: This video is DRM protected")
            Path(dest_dir, targets[0].rsplit("/", 1)[1] + ".mp3").write_bytes(b"x")

    return types.SimpleNamespace(YoutubeDL=FakeYDL)


class SoundcloudCandidateTests(unittest.TestCase):  # ACQ-6
    def test_duration_filter(self):
        ok = yts._soundcloud_duration_ok
        self.assertTrue(ok(205, 200_000))
        self.assertFalse(ok(225, 200_000))      # > 10% off
        self.assertFalse(ok(30, 200_000))       # a preview
        self.assertFalse(ok(30, 0))             # unknown MB length: previews still out
        self.assertTrue(ok(60, 0))
        self.assertTrue(ok(31, 30_000))         # a real 30 s interlude
        self.assertTrue(ok(None, 200_000))      # unknown result length: verified after download

    def test_candidates_are_tried_in_order_skipping_previews_and_drm(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dest = str(Path(tmp.name).resolve())
        downloads = []
        entries = [
            {"url": "https://soundcloud.com/a/drm", "duration": 199},
            {"url": "https://soundcloud.com/a/preview", "duration": 30},
            {"url": "http://soundcloud.com/a/plain-http", "duration": 200},
            "junk",
            {"url": "https://soundcloud.com/a/good", "duration": 205},
            {"url": "https://soundcloud.com/a/later", "duration": 200},
        ]
        ready = threading.Event()
        ready.set()
        log = []
        import backend.ytdlp_guard as guard
        with mock.patch.dict(sys.modules, {"yt_dlp": _fake_yt_dlp(entries, dest, downloads)}), \
                mock.patch.object(yts, "_ytdlp_ready", ready), \
                mock.patch.object(guard, "resolve_public_target"):  # no real DNS for soundcloud.com
            n = yts._ytdlp_missing_tracks_download(
                "Artist", "Album", "", dest, log,
                [{"disc": 1, "track": 1, "title": "Song", "duration_ms": 200_000}], source="soundcloud")
        self.assertEqual(n, 1)
        self.assertEqual(downloads, ["https://soundcloud.com/a/drm", "https://soundcloud.com/a/good"])
        self.assertIn("  [yt-dlp] Skipping https://soundcloud.com/a/drm: DRM protected", log)
        self.assertIn("  [yt-dlp] Skipping https://soundcloud.com/a/preview: length does not match", log)

    def test_search_failure_yields_no_candidates(self):
        class Broken:
            def __init__(self, opts):
                pass

            def __enter__(self):
                raise RuntimeError("HTTP Error 503")

            def __exit__(self, *exc):
                return False
        log = []
        self.assertEqual(yts._soundcloud_track_candidates(
            types.SimpleNamespace(YoutubeDL=Broken), {}, "scsearch5:x", 0, log), [])
        self.assertTrue(any("SoundCloud search error" in line for line in log))

    def test_wanted_tracks_keep_the_musicbrainz_length(self):
        rows = slskd._normalise_wanted_tracks([{"track": 1, "title": "A", "duration_ms": 1000},
                                               {"track": 2, "title": "B"}])
        self.assertEqual(rows[0]["duration_ms"], 1000)
        self.assertNotIn("duration_ms", rows[1])


if __name__ == "__main__":
    unittest.main()
