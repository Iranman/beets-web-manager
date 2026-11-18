"""#434: a format-replacement whose verified replacement plan fails must fail
closed -- queue a reconciliation review naming BOTH rows with a stable reason
code, and never report success with nothing in Preview. Mirrors #432's failed
fill-merge semantics.

Fail-before/pass-after: with the fix reverted, the plan-refusal path logs only
and returns no ``needs_review`` flag, no review record is written, and the job
marks the track retryable ("no replacement available") instead of a terminal
review item.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.import_reconciliation as recon
import backend.replacement_service as rs
from backend.import_reconciliation import (
    REPLACEMENT_PLAN_FAILED,
    record_replacement_failed_review,
    load_reviews,
    resolve_review,
)


class RecordReplacementFailedReviewTests(unittest.TestCase):
    def test_names_both_rows_with_stable_reason_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviews.json"
            rec = record_replacement_failed_review(
                existing_item_id=10, imported_item_id=20,
                existing_album_id=1, imported_album_id=2,
                target_recording_id="REC-1", release_group_id="RG-1",
                reason="Could not verify by AcoustID that the replacement is the slot's recording",
                refusal_code="fingerprint_disagreement",
                existing_row={"id": 10, "path": "/music/a/10.flac", "artist": "A", "title": "S"},
                imported_row={"id": 20, "path": "/music/import/20.flac", "artist": "A", "title": "S"},
                path=path)
            self.assertIsNotNone(rec)
            stored = load_reviews(path)
            self.assertEqual(len(stored), 1)
            row = stored[0]
            # both rows named
            self.assertEqual(row["existing_item_id"], 10)
            self.assertEqual(row["imported_item_id"], 20)
            self.assertEqual(row["existing"]["item_id"], 10)
            self.assertEqual(row["imported"]["item_id"], 20)
            # stable reason code + underlying refusal code, resolvable surface
            self.assertEqual(row["reason_code"], REPLACEMENT_PLAN_FAILED)
            self.assertEqual(row["refusal_code"], "fingerprint_disagreement")
            self.assertEqual(row["kind"], "contested_slot")
            self.assertEqual(row["status"], "open")
            self.assertTrue(any(REPLACEMENT_PLAN_FAILED in r for r in row["reasons"]))

    def test_missing_row_id_queues_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviews.json"
            self.assertIsNone(record_replacement_failed_review(
                existing_item_id=0, imported_item_id=20, path=path))
            self.assertEqual(load_reviews(path), [])

    def test_queued_review_is_dismissable_keep_both(self):
        """The queued contested_slot is resolvable from the existing surface."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviews.json"
            rec = record_replacement_failed_review(
                existing_item_id=10, imported_item_id=20,
                existing_album_id=1, imported_album_id=2,
                reason="refused", refusal_code="destination_occupied", path=path)
            out = resolve_review(rec["review_id"], "keep_both", object(), path=path)
            self.assertTrue(out["ok"])


class _FakeAdapter:
    def __init__(self, items):
        self._items = items

    def get_item(self, item_id):
        return self._items.get(int(item_id or 0))


class RemoveOriginalFailClosedTests(unittest.TestCase):
    """_music_format_remove_original_after_replacement at the plan boundary."""

    def _run(self, plan_result, tmp):
        path = Path(tmp) / "reviews.json"
        items = {
            7: {"id": 7, "album_id": 1, "path": "/music/a/7.flac", "mb_trackid": "REC-7",
                "mb_releasegroupid": "RG-7", "mb_albumid": "REL-7", "artist": "A", "title": "S"},
            99: {"id": 99, "album_id": 2, "path": "/music/import/99.flac", "mb_trackid": "REC-7",
                 "artist": "A", "title": "S"},
        }
        log: list = []
        with mock.patch.object(rs._item_replacement, "tracked_item_id_for_path", return_value=99), \
             mock.patch.object(rs._item_replacement, "plan_verified_replacement", return_value=plan_result), \
             mock.patch.object(rs, "beets_adapter", _FakeAdapter(items)), \
             mock.patch.object(recon, "review_store_path", return_value=path), \
             mock.patch("pathlib.Path.is_file", return_value=True):
            result = rs._music_format_remove_original_after_replacement(
                "/music/a/7.flac", "/music/import/99.flac", {}, log,
                original_item_id=7, resolved=items[7], replacement={"path": "/music/import/99.flac"})
        return result, log, load_reviews(path)

    def test_plan_failure_fails_closed_and_reports_the_refused_pair(self):
        # The SEC-002 destructive target function does NOT queue the review
        # itself (its local call set is locked down); it reports the refused
        # pair so the loop can queue it. Behaviour end-to-end is covered by
        # QueueHelperTests + the loop source guard below.
        with tempfile.TemporaryDirectory() as tmp:
            result, log, reviews = self._run(
                {"ok": False, "code": "fingerprint_disagreement",
                 "error": "Could not verify by AcoustID"}, tmp)
            # not a bare success: no approval, flagged for review, both ids reported
            self.assertFalse(result.get("awaiting_approval"))
            self.assertTrue(result.get("needs_review"))
            self.assertEqual(result.get("original_item_id"), 7)
            self.assertEqual(result.get("source_item_id"), 99)
            self.assertEqual(result.get("refusal_code"), "fingerprint_disagreement")
            # the target function itself writes nothing to the review store
            self.assertEqual(reviews, [])

    def test_successful_plan_still_awaits_approval_and_queues_no_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, log, reviews = self._run(
                {"ok": True, "operation_id": "op-1"}, tmp)
            self.assertTrue(result.get("awaiting_approval"))
            self.assertFalse(result.get("needs_review"))
            self.assertEqual(result.get("operation_id"), "op-1")
            self.assertEqual(reviews, [])


class QueueHelperTests(unittest.TestCase):
    """_queue_replacement_failed_review (called by the loop, not the locked-down
    target function) writes the review naming both rows with the stable code."""

    def test_queue_helper_names_both_rows(self):
        items = {
            7: {"id": 7, "album_id": 1, "path": "/music/a/7.flac", "mb_trackid": "REC-7",
                "mb_releasegroupid": "RG-7", "mb_albumid": "REL-7", "artist": "A", "title": "S"},
            99: {"id": 99, "album_id": 2, "path": "/music/import/99.flac", "mb_trackid": "REC-7"},
        }
        log: list = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviews.json"
            with mock.patch.object(rs, "beets_adapter", _FakeAdapter(items)), \
                 mock.patch.object(recon, "review_store_path", return_value=path):
                rs._queue_replacement_failed_review(7, 99, items[7], "Could not verify by AcoustID",
                                                    "fingerprint_disagreement", log)
            reviews = load_reviews(path)
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["existing_item_id"], 7)
        self.assertEqual(reviews[0]["imported_item_id"], 99)
        self.assertEqual(reviews[0]["reason_code"], REPLACEMENT_PLAN_FAILED)
        self.assertEqual(reviews[0]["refusal_code"], "fingerprint_disagreement")
        self.assertTrue(any("Queued reconciliation review" in line for line in log))


class JobSurfacesNeedsReviewTests(unittest.TestCase):
    """The replace-rows loop queues the review and marks the plan-refusal
    terminal (not a retry loop), so the job never reports bare success."""

    def test_loop_queues_and_handles_needs_review_terminally(self):
        import inspect
        body = inspect.getsource(rs._music_format_replace_rows)
        self.assertIn('removal.get("needs_review")', body)
        self.assertIn("_queue_replacement_failed_review(", body)
        self.assertIn('status="Needs review"', body)
        self.assertIn("retryable=False", body)
        self.assertIn("REPLACEMENT_PLAN_FAILED", body)


if __name__ == "__main__":
    unittest.main()
