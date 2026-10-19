"""#406 follow-up: an import refusal that is not about the downloaded copy
stops the slskd peer loop after one candidate; a candidate-specific or
missing reason code keeps trying peers. Also: slskd cancels by transfer id."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._module_time import module_time

import backend.acquisition_service as acq
import backend.slskd_service as slskd

_MBID = "11111111-2222-3333-4444-555555555555"


class NonCandidateRefusalStopsPeerLoop(unittest.TestCase):
    def _run(self, refusal):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        peers = iter(f"peer{i}" for i in range(1, 100))

        def search(*_a, **_k):
            peer = next(peers)
            return peer, [f"Music\\{peer}\\01.flac"], str(root / peer), f"Music\\{peer}"

        patches = dict(
            jobs=mock.Mock(),
            validated_downloads_root=mock.Mock(return_value=root),
            slskd_configured=mock.Mock(return_value=True),
            _slskd_fallback_methods=mock.Mock(return_value=["soundcloud"]),
            _direct_source_unavailable_reason=mock.Mock(return_value=""),
            _ytdlp_album_download=mock.Mock(side_effect=RuntimeError("fell back")),
            _resolve_album_release_for_import=mock.Mock(return_value=_MBID),
            _fetch_mb_release_tracklist=mock.Mock(return_value={"ok": False}),
            _slskd_search_and_queue=mock.Mock(side_effect=search),
            _slskd_wait_downloads=mock.Mock(return_value=("", [])),
            _find_slskd_downloaded_files=mock.Mock(return_value=(str(root), [root / "01.flac"])),
            _stage_selected_audio_files=mock.Mock(return_value=str(root / "stage")),
            _validate_import_source_audio=mock.Mock(),
            _start_reimport_disk_job_internal=mock.Mock(return_value="job1"),
            _wait_for_child_job=mock.Mock(side_effect=RuntimeError(
                "import job failed: ERROR: Could not resolve a MusicBrainz release ID "
                f"for import{refusal}; queued for Review")),
            _delete_staged_import_folder=mock.Mock(),
            _slskd_cancel_queued_downloads=mock.Mock(),
            _slskd_cleanup_failed_candidate_files=mock.Mock(),
        )
        m = patches
        with mock.patch.multiple(acq, **patches), \
                module_time(acq, sleep=mock.Mock()):
            body, _ = acq.start_album_download({
                "artist": "A", "album": "B", "method": "slskd",
                "auto_import": True, "try_source_fallback": True,
            })
            self.assertTrue(body["ok"], body)
            job_fn = m["jobs"].start_python.call_args[0][0]
            log = []
            with self.assertRaisesRegex(RuntimeError, "direct-source fallback also failed"):
                job_fn(log)
        return m, log

    def test_non_candidate_code_stops_after_one_candidate(self):
        m, log = self._run(" [reason_code=acoustid_unavailable]")
        self.assertEqual(m["_slskd_search_and_queue"].call_count, 1)
        # The failed candidate is still cleaned up, and fallback still runs.
        m["_slskd_cancel_queued_downloads"].assert_called_once()
        m["_slskd_cleanup_failed_candidate_files"].assert_called_once()
        m["_delete_staged_import_folder"].assert_called_once()
        m["_ytdlp_album_download"].assert_called_once()
        stop = [line for line in log if "Not trying other Soulseek peers" in line]
        self.assertEqual(len(stop), 1, log)
        self.assertIn("acoustid_unavailable", stop[0])
        self.assertIn("would be refused the same way", stop[0])
        self.assertFalse([line for line in log if "Trying the next peer" in line], log)

    def test_candidate_specific_code_keeps_trying_peers(self):
        m, log = self._run(" [reason_code=file_unverified]")
        self.assertEqual(m["_slskd_search_and_queue"].call_count, 12)
        self.assertFalse([line for line in log if "Not trying other" in line])

    def test_missing_code_keeps_trying_peers(self):
        m, log = self._run("")
        self.assertEqual(m["_slskd_search_and_queue"].call_count, 12)
        self.assertFalse([line for line in log if "Not trying other" in line])


class CancelQueuedDownloadsUsesTransferIds(unittest.TestCase):
    TRANSFERS = {"username": "peer", "directories": [{"files": [
        {"id": "aaaa-1", "filename": "Music\\Album\\01.flac"},
        {"id": "bbbb-2", "filename": "Music\\Album\\02.flac"},
    ]}]}

    def test_deletes_by_id_and_skips_unknown_files(self):
        calls = []

        def req(method, path, *_a):
            calls.append((method, path))
            return self.TRANSFERS if method == "GET" else {}

        log = []
        with mock.patch.object(slskd, "_slskd_req", side_effect=req):
            slskd._slskd_cancel_queued_downloads(
                "peer", ["Music\\Album\\01.flac", "Music\\Album\\02.flac", "Music\\Album\\03.flac"], log)
        self.assertEqual(calls, [
            ("GET", "transfers/downloads/peer"),
            ("DELETE", "transfers/downloads/peer/aaaa-1?remove=true"),
            ("DELETE", "transfers/downloads/peer/bbbb-2?remove=true"),
        ])
        self.assertIn("Cancelled 2 queued transfer(s)", log[0])
        self.assertFalse([line for line in log if "WARN" in line], log)

    def test_list_failure_warns_once_and_returns(self):
        log = []
        with mock.patch.object(slskd, "_slskd_req", side_effect=RuntimeError("HTTP 503")):
            slskd._slskd_cancel_queued_downloads("peer", ["a.flac"], log)
        self.assertEqual(len(log), 1)
        self.assertIn("could not list transfers", log[0])


if __name__ == "__main__":
    unittest.main()
