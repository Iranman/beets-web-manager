"""ARCH-033 Review Queue follow-ups (0.7.1, QA #410 N1-N3).

N3: a list-only Review Queue load must not make a Beets call while the global
    queue lock is held (the prune runs lock-free, then applies under the lock).
N1: a partial-import folder whose Review enqueue failed must not re-run Stage 1
    on retry (idempotent hand-off); the Review row is re-created from the stash.
N2: the final-pass review for such a folder must preserve the original evidence
    (conflict / skipped_paths / kept_album_ids / deterministic_decision).
"""
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import backend.pending_review_store as prs
from backend.pending_review_store import PendingReviewLoadError

import tests.test_import_batch_phase_b as pb


def setUpModule():
    pb.setUpModule()
    global ai
    ai = pb.ai


class Arch033N3LockTests(unittest.TestCase):
    """N3: no Beets HTTP call while the global queue lock is held."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.downloads = self.root / "downloads"
        self.a, self.b = (str(self.downloads / n) for n in "AB")
        for p in (self.a, self.b):
            Path(p).mkdir(parents=True)
        self.store = self.root / "pending.json"
        self.store.write_text(json.dumps(
            [{"path": p, "origin_type": "x", "suggestion": {"origin_type": "x"}} for p in (self.a, self.b)]))
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(prs, "_AI_PENDING_FILE", self.store))
        stack.enter_context(mock.patch.object(prs, "DOWNLOADS_ROOT", self.downloads))
        stack.enter_context(mock.patch.object(prs, "_pending_review_item_is_resolved", return_value=False))
        stack.enter_context(mock.patch.object(
            prs, "_reconcile_pending_review_enqueue_item", side_effect=lambda i: (i, False)))
        stack.enter_context(mock.patch.object(prs, "_record_ai_review_decision"))

    def test_prune_makes_no_beets_call_while_the_queue_lock_is_held(self):
        lock_free_during_call = []

        def library_ids(path):
            # Stand-in for the Beets-backed resolution check. Probe from another
            # thread whether the queue lock is held while this "Beets call" runs:
            # a non-blocking acquire fails iff the lock is held by any thread.
            probe = []

            def check():
                got = prs._ai_pending_lock.acquire(blocking=False)
                if got:
                    prs._ai_pending_lock.release()
                probe.append(got)  # True => the lock was free

            t = threading.Thread(target=check)
            t.start()
            t.join()
            lock_free_during_call.append(probe[0])
            return [7] if path == self.a else []

        with mock.patch.object(prs, "_library_album_ids_for_folder", side_effect=library_ids):
            result = prs._load_pending_reviews()

        self.assertTrue(lock_free_during_call, "the Beets-backed resolution check never ran")
        self.assertTrue(all(lock_free_during_call),
                        "a Beets call ran while _ai_pending_lock was held (ARCH-033 N3 regression)")
        # The prune still happens: A resolved to a library album, so it is dropped.
        self.assertEqual([i["path"] for i in result], [self.b])


class Arch033BatchHandoffTests(pb._Batch):
    """N1/N2: an idempotent, evidence-preserving hand-off when the Review
    enqueue fails after Beets already imported part of a folder."""

    def _make_partial(self):
        disc2 = str(Path(self.folders[0]) / "CD2")
        self.reimport[self.folders[0]] = {
            "ok": True, "albums_imported": 1, "imported_album_ids": [7],
            "skipped": [{"path": disc2, "reason": "no_strong_match"}],
            "not_matched": [disc2], "not_matched_known": True}
        return disc2

    def test_n2_final_pass_preserves_partial_import_evidence(self):
        disc2 = self._make_partial()
        calls = []

        def queued(folder, suggestion, evidence=None, batch_job_id=""):
            calls.append({"folder": folder, "suggestion": suggestion, "evidence": evidence})
            # The inline enqueue (first call for A) fails; the final pass retries.
            if sum(1 for c in calls if c["folder"] == folder) == 1:
                raise PendingReviewLoadError("Review Queue could not be loaded.")
            return "rid"

        self.queued.side_effect = queued
        _, folders = self.run_batch()

        a = folders[self.folders[0]]
        # The final pass attaches a Review row; the folder keeps its outcome
        # status for the batch summary (existing _queue design). N1 keeps it
        # safe on retry via the stashed imported album id.
        self.assertEqual(a["review_item_id"], "rid")
        self.assertEqual(a.get("imported_album_ids"), [7])
        a_calls = [c for c in calls if c["folder"] == self.folders[0]]
        self.assertEqual(len(a_calls), 2, "inline enqueue should fail, final pass should retry")
        ev = a_calls[-1]["evidence"] or {}
        # N2: the original evidence survived instead of being replaced by a minimal row.
        self.assertEqual(ev.get("skipped_paths"), [disc2])
        self.assertEqual(ev.get("kept_album_ids"), [7])
        self.assertEqual(ev.get("origin_step"), "beets_partial")
        self.assertEqual((a_calls[-1]["suggestion"] or {}).get("kept_album_ids"), [7])

    def test_n1_retry_does_not_reimport_an_already_imported_folder(self):
        disc2 = str(Path(self.folders[0]) / "CD2")
        folder = self.folders[0]
        # State as left by a prior run: Beets imported album 7, but the Review
        # row could not be written, so the folder is retryable with the payload
        # and the imported album id stashed.
        state = ai._ai_batch_initial_state(self.batch_id, str(self.tmp))
        fs = ai._ai_batch_folder_state(self.batch_id, folder)
        fs.update({
            "status": "ai_queued", "review_item_id": "",
            "imported_album_ids": [7],
            "pending_review": {
                "suggestion": {"artist": "Artist", "album": "Album", "kept_album_ids": [7], "reason": "partial"},
                "evidence": {"origin_step": "beets_partial", "skipped_paths": [disc2], "kept_album_ids": [7]},
                "status": "review_required", "current_step": "imported in part"},
        })
        state["folder_states"][fs["folder_id"]] = fs

        self.log = []
        ai._ai_batch_process_folder(state, fs["folder_id"], self.log)

        reimported = [c.args[0] for c in self.stubs["reimport_source"].call_args_list]
        self.assertNotIn(folder, reimported, "ARCH-033 N1: a retry must not re-run Stage 1")
        # The Review row is re-created from the stash (with its original evidence).
        self.assertEqual(self.queued.call_count, 1)
        ev = self.queued.call_args.kwargs.get("evidence") or {}
        self.assertEqual(ev.get("skipped_paths"), [disc2])
        self.assertEqual(fs["status"], "review_required")
        self.assertEqual(fs["review_item_id"], "rid")
        self.assertNotIn("pending_review", fs, "the stash is dropped once the row exists")


if __name__ == "__main__":
    unittest.main()
