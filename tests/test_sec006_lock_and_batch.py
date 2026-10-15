"""SEC-006 follow-ups from QA #410: a list reader's prune save holds the queue
lock (no lost update), and a batch folder whose review item cannot be queued
keeps its own outcome and stays retryable."""
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import backend.pending_review_store as prs
import tests.test_qa394_reverify4 as r4
from tests.test_qa394_reverify4 import e2e

setUpModule = r4.setUpModule


class ListPruneHoldsLockTests(unittest.TestCase):
    def test_writer_add_during_a_list_prune_is_not_lost(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        downloads = root / "downloads"
        a, b, c = (str(downloads / n) for n in "ABC")
        for p in (a, b, c):
            Path(p).mkdir(parents=True)
        store = root / "pending.json"
        store.write_text(json.dumps([{"path": p, "origin_type": "x", "suggestion": {"origin_type": "x"}}
                                     for p in (a, b)]))
        paused, release, writer_done = threading.Event(), threading.Event(), threading.Event()

        def lib_ids(path):
            if threading.current_thread().name == "lister" and path == a:
                paused.set()
                release.wait(10)
                return [1]  # A is in the library now: the list prune drops it and saves
            return []

        with ExitStack() as stack:
            for name, kw in (("_AI_PENDING_FILE", {"new": store}), ("DOWNLOADS_ROOT", {"new": downloads}),
                             ("_library_album_ids_for_folder", {"side_effect": lib_ids}),
                             ("_pending_review_item_is_resolved", {"return_value": False}),
                             ("_reconcile_pending_review_enqueue_item", {"side_effect": lambda i: (i, False)}),
                             ("_record_ai_review_decision", {})):
                stack.enter_context(mock.patch.object(prs, name, **kw))
            lister = threading.Thread(target=prs._load_pending_reviews, name="lister")
            writer = threading.Thread(target=lambda: (prs._add_to_pending(c, {"album": "C"}), writer_done.set()))
            lister.start()
            self.assertTrue(paused.wait(10))
            writer.start()
            writer_done.wait(1.0)  # unlocked prune: the add lands now and the stale save then drops it
            release.set()
            lister.join(10)
            writer.join(10)
        self.assertEqual([i["path"] for i in json.loads(store.read_text())], [b, c])


class BatchFolderKeepsOutcomeTests(r4.RealRootsBatch):
    def test_unreadable_queue_keeps_the_folders_own_failure_and_retryability(self):
        self.audio("A/Album/01.flac")
        conflict = {"reason": "Symlinked audio refused", "conflict": "symlinked_audio", "paths": ["x"]}
        with mock.patch.object(e2e.ai, "_ai_batch_preimport_check", return_value=([], conflict)), \
                mock.patch.object(e2e.ai, "_ai_batch_queue_pending_review",
                                  side_effect=prs.PendingReviewLoadError("Review Queue could not be loaded.")):
            _, out = self.start_batch()
        folder = out["folders"][0]
        self.assertEqual(folder["status"], "import_failed", "\n".join(self.log))
        self.assertIn("review_queue_unreadable", folder["failure_reason"])
        self.assertIn("symlinked_audio", folder["failure_reason"])
        self.assertFalse(folder.get("review_item_id"))


if __name__ == "__main__":
    unittest.main()
