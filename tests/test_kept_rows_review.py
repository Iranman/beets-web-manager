"""Re-QA of #295: a review queued after Beets imported (verification failed,
rows kept) says so, and the AI batch records the kept album_ids too."""
import unittest
from unittest import mock

import backend.pending_review_store as prs



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


if __name__ == "__main__":
    unittest.main()
