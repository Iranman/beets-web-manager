"""Issue #547: the replacement flow defers on an incomplete audio inspection.

An ffprobe timeout is not a verdict on the audio, so it must neither push a
replacement through nor use up one of the limited replacement attempts.
"""

import unittest
from unittest import mock

import backend.replacement_service as rs
from backend.audio_preferences import AUDIO_INSPECTION_INCOMPLETE, AudioInspectionIncomplete
from backend.job_service import CHILD_OUTCOME_UNKNOWN, ChildJobFailed

INCOMPLETE = {"ok": False, "inspection_incomplete": True,
              "reasons": ["ffprobe failed: timed out"],
              "message": "Audio inspection incomplete: ffprobe failed: timed out"}
REJECTED = {"ok": False, "inspection_incomplete": False, "reasons": ["lossy"], "message": "Rejected: lossy"}

ROW = {"item_id": 7, "album_id": 3, "path": "/music/a/original.mp3", "artist": "Artist",
       "title": "Song", "disc": 1, "track": 1, "attempt_count": 1,
       "mb_trackid": "11111111-1111-1111-1111-111111111111", "status": "Needs replacement"}

CANDIDATE = {"id": 8, "path": "/music/a/candidate.flac", "mb_trackid": ROW["mb_trackid"],
             "disc": 1, "track": 1, "title": "Song", "artist": "Artist"}


class ReplaceRowsRetryRecheckTests(unittest.TestCase):
    """Item 1: a retry whose re-check of the current file times out defers."""

    def run_replace(self, validation):
        marked = []
        with mock.patch.object(rs, "_music_format_preferences", return_value={}), \
             mock.patch.object(rs, "_load_music_format_replacement_statuses", return_value={"tracks": [dict(ROW)]}), \
             mock.patch.object(rs, "_music_format_library_rows", return_value=[]), \
             mock.patch.object(rs, "_music_format_hydrate_status_row", side_effect=lambda row, _rows: dict(row)), \
             mock.patch.object(rs, "_validate_audio_file_preferences", return_value=validation), \
             mock.patch.object(rs, "_mark_music_format_needs_replacement", side_effect=marked.extend), \
             mock.patch.object(rs, "_music_format_resolve_replacement_identity",
                               return_value={"ok": False, "reason": "stop here"}) as resolve, \
             mock.patch.object(rs, "start_album_download") as download:
            log = []
            result = rs._music_format_replace_rows(log)
        return result, marked, resolve, download, log

    def test_incomplete_recheck_defers_without_replacing(self):
        result, marked, resolve, download, log = self.run_replace(INCOMPLETE)
        resolve.assert_not_called()
        download.assert_not_called()
        self.assertEqual(result["deferred"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(len(marked), 1)
        state = marked[0]
        self.assertEqual(state["status"], "Replacement deferred")
        self.assertTrue(state["retryable"])
        self.assertTrue(state["queued_retry"])
        self.assertEqual(state["attempt_count"], 1, "a deferral must not use up an attempt")
        self.assertIn("inspection", state["reason"].lower())
        self.assertTrue(any("deferred" in line.lower() for line in log))

    def test_rejected_recheck_still_proceeds(self):
        _result, _marked, resolve, _download, _log = self.run_replace(REJECTED)
        resolve.assert_called_once()


class FindVerifiedReplacementTests(unittest.TestCase):
    """Item 2: a candidate whose inspection timed out does not count as tried."""

    def find(self, validation):
        with mock.patch.object(rs.composite_workflows, "find_all_items_by_mbid", return_value=[dict(CANDIDATE)]), \
             mock.patch.object(rs.composite_workflows, "find_all_items_by_album_id", return_value=[]), \
             mock.patch.object(rs, "_validate_audio_file_preferences", return_value=validation):
            return rs._music_format_find_verified_replacement(dict(ROW), {})

    def test_incomplete_candidate_raises_inspection_incomplete(self):
        with self.assertRaises(AudioInspectionIncomplete):
            self.find(INCOMPLETE)

    def test_rejected_candidate_returns_nothing(self):
        self.assertEqual(self.find(REJECTED), {})


class ReplaceRowsChildJobDeferralTests(unittest.TestCase):
    """Follow-up: the download child job reports an inspection that did not
    finish, or an unknown outcome, through ChildJobFailed.reason_code."""

    def run_with_child_failure(self, error):
        marked = []
        resolved = {**ROW, "ok": True, "mb_releasegroupid": "22222222-2222-2222-2222-222222222222"}
        with mock.patch.object(rs, "_music_format_preferences", return_value={}), \
             mock.patch.object(rs, "_load_music_format_replacement_statuses", return_value={"tracks": [dict(ROW)]}), \
             mock.patch.object(rs, "_music_format_library_rows", return_value=[]), \
             mock.patch.object(rs, "_music_format_hydrate_status_row", side_effect=lambda row, _rows: dict(row)), \
             mock.patch.object(rs, "_validate_audio_file_preferences", return_value=REJECTED), \
             mock.patch.object(rs, "_mark_music_format_needs_replacement", side_effect=marked.extend), \
             mock.patch.object(rs, "_music_format_resolve_replacement_identity", return_value=resolved), \
             mock.patch.object(rs, "_music_format_replacement_payload", return_value={"artist": "Artist", "album": "Album"}), \
             mock.patch.object(rs, "start_album_download"), \
             mock.patch.object(rs, "_json_from_flask_response", return_value={"ok": True, "job_id": "j1"}), \
             mock.patch.object(rs, "_wait_for_child_job", side_effect=error), \
             mock.patch.object(rs, "_music_format_find_verified_replacement") as find:
            result = rs._music_format_replace_rows([])
        find.assert_not_called()
        return result, marked[-1]

    def test_inspection_incomplete_and_unknown_outcome_defer(self):
        for code in (AUDIO_INSPECTION_INCOMPLETE, CHILD_OUTCOME_UNKNOWN):
            with self.subTest(code=code):
                # Message text deliberately says nothing about inspection: the
                # decision must come from the reason code alone.
                result, final = self.run_with_child_failure(ChildJobFailed("child job failed", code))
                self.assertEqual(result["deferred"], 1)
                self.assertEqual(result["failed"], 0)
                self.assertEqual(final["status"], "Replacement deferred")
                self.assertTrue(final["retryable"])
                self.assertEqual(final["attempt_count"], ROW["attempt_count"])

    def test_other_child_failure_still_uses_an_attempt(self):
        for error in (ChildJobFailed("Audio inspection incomplete", ""), ChildJobFailed("boom", "other_code")):
            with self.subTest(error=error):
                result, final = self.run_with_child_failure(error)
                self.assertEqual(result["deferred"], 0)
                self.assertEqual(result["failed"], 1)
                self.assertNotEqual(final["status"], "Replacement deferred")
                self.assertEqual(final["attempt_count"], ROW["attempt_count"] + 1)


class ReplaceRowsCandidateDeferralTests(unittest.TestCase):
    def test_incomplete_candidate_keeps_attempt_and_stays_retryable(self):
        marked = []
        resolved = {**ROW, "ok": True, "mb_releasegroupid": "22222222-2222-2222-2222-222222222222"}
        with mock.patch.object(rs, "_music_format_preferences", return_value={}), \
             mock.patch.object(rs, "_load_music_format_replacement_statuses", return_value={"tracks": [dict(ROW)]}), \
             mock.patch.object(rs, "_music_format_library_rows", return_value=[]), \
             mock.patch.object(rs, "_music_format_hydrate_status_row", side_effect=lambda row, _rows: dict(row)), \
             mock.patch.object(rs, "_validate_audio_file_preferences", return_value=REJECTED), \
             mock.patch.object(rs, "_mark_music_format_needs_replacement", side_effect=marked.extend), \
             mock.patch.object(rs, "_music_format_resolve_replacement_identity", return_value=resolved), \
             mock.patch.object(rs, "_music_format_replacement_payload", return_value={"artist": "Artist", "album": "Album"}), \
             mock.patch.object(rs, "start_album_download"), \
             mock.patch.object(rs, "_json_from_flask_response", return_value={"ok": True, "job_id": "j1"}), \
             mock.patch.object(rs, "_wait_for_child_job"), \
             mock.patch.object(rs, "_music_format_find_verified_replacement",
                               side_effect=AudioInspectionIncomplete("candidate inspection timed out")), \
             mock.patch.object(rs, "_music_format_remove_original_after_replacement") as remove:
            result = rs._music_format_replace_rows([])
        remove.assert_not_called()
        self.assertEqual(result["deferred"], 1)
        self.assertEqual(result["failed"], 0)
        final = marked[-1]
        self.assertEqual(final["status"], "Replacement deferred")
        self.assertTrue(final["retryable"])
        self.assertEqual(final["attempt_count"], ROW["attempt_count"], "a timed-out candidate must not use up an attempt")


if __name__ == "__main__":
    unittest.main()
