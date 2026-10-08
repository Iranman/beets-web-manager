"""Re-QA of #295: a review queued after Beets imported (verification failed,
rows kept) says so, and the AI batch records the kept album_ids too."""
import unittest
from unittest import mock

import backend.ai_service as ai
import backend.pending_review_store as prs

REL = "11111111-1111-1111-1111-111111111111"
RG = "22222222-2222-2222-2222-222222222222"


class QueueMessageTests(unittest.TestCase):
    def _queue(self, suggestion):
        log = []
        with mock.patch.object(prs, "_library_album_ids_for_folder", return_value=[9]), \
                mock.patch.object(prs, "_add_to_pending"):
            self.assertTrue(prs._queue_folder_for_manual_review(
                "/downloads/A", suggestion, "reason", log, allow_existing=True))
        return log[-1]

    def test_kept_rows_message_names_the_album(self):
        msg = self._queue({"kept_album_ids": [9]})
        self.assertIn("album_id 9", msg)
        self.assertNotIn("no library files were changed", msg)

    def test_plain_review_message_unchanged(self):
        self.assertIn("no library files were changed", self._queue({}))


class AiBatchKeptRowsTests(unittest.TestCase):
    def test_verification_failure_records_kept_album_ids_in_review(self):
        folder = "/downloads/Artist - Album"
        state = {"batch_job_id": "b1", "folder_states": {"f1": {
            "source_folder": folder,
            "ai_result": {"ok": True, "suggestion": {"confidence": "high", "mb_valid": True,
                                                     "mb_albumid": REL}}}}}
        queued = mock.MagicMock(return_value="f1")
        with mock.patch.object(ai, "_ai_batch_effective_folder_status", return_value="ai_completed"), \
                mock.patch.object(ai, "_ai_batch_mark_folder"), \
                mock.patch.object(ai, "_ai_batch_commit"), \
                mock.patch.object(ai, "_folder_release_preflight", return_value={"ok": True}), \
                mock.patch.object(ai, "_ai_auto_import_allowed", return_value=True), \
                mock.patch.object(ai, "_ai_batch_build_evidence", return_value={}), \
                mock.patch.object(ai, "_ai_batch_queue_pending_review", queued), \
                mock.patch.object(ai, "_preserve_torrent_source_path", return_value=True), \
                mock.patch.object(ai, "_validate_import_source_audio"), \
                mock.patch.object(ai, "_fetch_mb_release_tracklist",
                                  return_value={"ok": True, "release_group": RG, "tracks": []}), \
                mock.patch.object(ai.composite_workflows, "plan_confirmed_import",
                                  return_value={"ok": True, "operation_id": "op-1"}), \
                mock.patch.object(ai.composite_workflows, "apply_confirmed_import",
                                  return_value={"ok": False, "code": "release_group_mismatch",
                                                "error": "different Release Group", "album_ids": [9]}):
            ai._ai_batch_process_decisions(state, [])
        queued.assert_called_once()
        self.assertEqual(queued.call_args.args[1]["kept_album_ids"], [9])


if __name__ == "__main__":
    unittest.main()
