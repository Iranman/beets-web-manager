"""An incomplete audio inspection (ffprobe timeout on a busy host) defers an
acquisition download instead of deleting or quarantining it.

The requested-track gate used to treat an ffprobe timeout as a format failure
and delete/quarantine the staged file, and any import error made the slskd and
direct-source loops delete the staging folder. Now the file stays staged, nothing
imports, and the job fails with a retryable message. A real format failure
(inspection complete, not accepted) is still rejected as before.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.audio_preferences import DEFAULT_MUSIC_FORMAT_PREFERENCES
from tests._module_time import module_time

import backend.acquisition_service as acq
import backend.playlist_service as ps

_HANG = mock.patch(
    "backend.audio_preferences.subprocess.run",
    side_effect=lambda cmd, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, kw.get("timeout"))),
)
_CORRUPT = mock.patch(
    "backend.audio_preferences.subprocess.run",
    return_value=type("Proc", (), {"returncode": 1, "stdout": "", "stderr": "Invalid data found"})(),
)
_TRACK = {"title": "Song", "track": 1, "mb_trackid": "rec-1"}


class WantedTrackGate(unittest.TestCase):
    def _run(self, ffprobe):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        staged = Path(tmp.name) / "01 Song.flac"
        staged.write_bytes(b"x")
        handle, delete, stamp = mock.Mock(), mock.Mock(), mock.Mock()
        match = {"ok": True, "title_score": 1.0, "artist_score": 1.0,
                 "identity": {"final_action": "accept", "acoustid_match_score": 1.0}}
        with ffprobe, \
                mock.patch.object(ps, "_music_format_preferences", return_value=DEFAULT_MUSIC_FORMAT_PREFERENCES), \
                mock.patch.object(ps, "_handle_rejected_audio_download", handle), \
                mock.patch.object(acq, "_playlist_download_match", return_value=match), \
                mock.patch.object(acq, "_playlist_stamp_download_tags", stamp), \
                mock.patch.object(acq, "_playlist_identity_log", side_effect=str), \
                mock.patch.object(acq.composite_workflows, "delete_file", delete):
            try:
                acq._validate_wanted_download_identity_before_import(tmp.name, [_TRACK], [])
            except Exception as ex:  # noqa: BLE001 -- the test inspects the type
                return staged, handle, delete, stamp, ex
        self.fail("the gate must not pass an uninspected or rejected file")

    def test_ffprobe_timeout_keeps_the_staged_file(self):
        staged, handle, delete, stamp, ex = self._run(_HANG)
        handle.assert_not_called()  # never quarantined
        delete.assert_not_called()  # never deleted
        stamp.assert_not_called()  # never treated as verified
        self.assertTrue(staged.exists())
        self.assertIsInstance(ex, acq.AudioInspectionIncomplete)
        self.assertIn("retry", str(ex))

    def test_real_format_failure_is_still_rejected(self):
        staged, handle, delete, _stamp, ex = self._run(_CORRUPT)
        self.assertNotIsInstance(ex, getattr(acq, "AudioInspectionIncomplete", ()))
        self.assertIn("did not fingerprint-verify", str(ex))
        handle.assert_called_once()
        delete.assert_called_once_with(str(staged))


class SlskdLoopKeepsTheCandidate(unittest.TestCase):
    def test_incomplete_inspection_stops_without_discarding(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        m = dict(
            jobs=mock.Mock(),
            validated_downloads_root=mock.Mock(return_value=root),
            slskd_configured=mock.Mock(return_value=True),
            _slskd_fallback_methods=mock.Mock(return_value=["soundcloud"]),
            _direct_source_unavailable_reason=mock.Mock(return_value=""),
            _ytdlp_album_download=mock.Mock(),
            _resolve_album_release_for_import=mock.Mock(return_value="11111111-2222-3333-4444-555555555555"),
            _fetch_mb_release_tracklist=mock.Mock(return_value={"ok": False}),
            _slskd_search_and_queue=mock.Mock(
                return_value=("peer1", ["Music\\p\\01.flac"], str(root / "p"), "Music\\p")),
            _slskd_wait_downloads=mock.Mock(return_value=("", [])),
            _find_slskd_downloaded_files=mock.Mock(return_value=(str(root), [root / "01.flac"])),
            _stage_selected_audio_files=mock.Mock(return_value=str(root / "stage")),
            _validate_import_source_audio=mock.Mock(
                side_effect=acq.AudioInspectionIncomplete("Audio inspection did not complete; retry")),
            _delete_staged_import_folder=mock.Mock(),
            _slskd_cancel_queued_downloads=mock.Mock(),
            _slskd_cleanup_failed_candidate_files=mock.Mock(),
        )
        with mock.patch.multiple(acq, **m), module_time(acq, sleep=mock.Mock()):
            body, _ = acq.start_album_download({"artist": "A", "album": "B", "method": "slskd",
                                                "auto_import": True, "try_source_fallback": True})
            self.assertTrue(body["ok"], body)
            with self.assertRaises(acq.AudioInspectionIncomplete):
                m["jobs"].start_python.call_args[0][0]([])
        m["_delete_staged_import_folder"].assert_not_called()
        m["_slskd_cleanup_failed_candidate_files"].assert_not_called()
        self.assertEqual(m["_slskd_search_and_queue"].call_count, 1)
        m["_ytdlp_album_download"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
