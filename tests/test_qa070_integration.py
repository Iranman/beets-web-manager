"""QA v0.7.0 integration: flows that cross PRs #391 (review split), #394
(deterministic batch), #396 (skip ledger hook) and #399 (Missing IDs batch).

Real Pending Review store, real skip ledger, real Flask routes; only Beets
is faked."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class _Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        cls.client = app_module.app.test_client()

    def setUp(self):
        import backend.pending_review_store as store
        self.store = store
        self.tmp = Path(tempfile.mkdtemp(prefix="bwm_qa070_")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for target, kw in (
            ("os.environ", None),
            ("backend.pending_review_store._AI_PENDING_FILE", {"new": self.tmp / "pending.json"}),
            ("backend.pending_review_store._IMPORT_SKIPPED_FILE", {"new": self.tmp / "skipped.json"}),
            ("backend.pending_review_store._library_album_ids_for_folder", {"return_value": []}),
            ("backend.pending_review_store._review_path_is_resolved", {"return_value": False}),
            ("backend.pending_review_store._record_ai_review_decision", {"return_value": None}),
        ):
            p = (mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"}) if kw is None
                 else mock.patch(target, **kw))
            p.start()
            self.addCleanup(p.stop)


class BatchReviewRowThroughQueueTests(_Case):
    """A folder Beets skipped (#396 ledger) that the batch sends to review
    (#394) is listed once in the Review Queue (#391), is found by the path
    search the batch panel's "Open in review" link uses, and disappears from
    both stores once imported."""

    def queue(self, qs=""):
        res = self.client.get("/api/import/review-queue" + qs)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        return res.get_json()

    def test_skip_then_batch_review_then_import(self):
        import backend.ai_service as ai
        folder = self.tmp / "downloads" / "Artist - Album"
        folder.mkdir(parents=True)
        (folder / "01.flac").write_bytes(b"x")
        folder = str(folder)

        self.store._record_import_skips(str(self.tmp / "downloads"),
                                        [{"path": folder, "reason": "no_strong_match"}])
        body = self.queue()
        self.assertEqual([r["type"] for r in body["items"]], ["skipped"])

        ai._ai_batch_queue_pending_review(
            folder, {"album": "Album", "albumartist": "Artist", "reason": "Beets skipped it"},
            evidence={"origin_step": "beets_skip", "beets_skip_reason": "no_strong_match", "ai_rank": None},
            batch_job_id="batch-qa070")
        body = self.queue()
        self.assertEqual(body["total"], 1, body)
        row = body["items"][0]
        self.assertEqual((row["type"], row["origin_type"], row["source_batch_id"]),
                         ("pending_ai", "batch_import", "batch-qa070"))
        self.assertEqual(row["evidence"]["origin_step"], "beets_skip")
        self.assertEqual(body["counts"]["skipped"], 0)

        from urllib.parse import quote
        self.assertEqual(self.queue("?search=" + quote(folder))["total"], 1)

        self.assertTrue(self.store._remove_pending_review_for_path(folder, []))
        self.assertEqual(self.queue()["total"], 0)
        self.assertEqual(self.store._load_recorded_import_skips(), [])


class MissingIdsClaimedByReviewTests(_Case):
    """An album a Review Queue row claims carries the "In import review"
    badge on the Missing IDs list (#391) and is never proven by the batch
    fix (#399), so the batch cannot write under an open import decision."""

    def setUp(self):
        super().setUp()
        items = [{"id": 1, "album_id": 1, "track": 1, "path": "Art/Alb/01.flac"},
                 {"id": 2, "album_id": 2, "track": 1, "path": "Art/Other/01.flac"}]
        albums = [{"id": 1, "album": "Alb", "albumartist": "Art", "added": 2},
                  {"id": 2, "album": "Other", "albumartist": "Art", "added": 1}]
        adapter = mock.Mock(get_items=lambda: [dict(i) for i in items],
                            get_albums=lambda: [dict(a) for a in albums])
        for target, kw in (("backend.composite_workflows.beets_adapter", {"new": adapter}),
                           ("backend.library_service._load_album_mb_suggestions", {"return_value": {}})):
            p = mock.patch(target, **kw)
            p.start()
            self.addCleanup(p.stop)
        self.store._add_to_pending(str(self.tmp / "dl" / "Alb"),
                                   {"album": "Alb", "existing_album_ids": [1]}, allow_existing=True)

    def test_badge_and_batch_refusal(self):
        res = self.client.get("/api/library/missing-mb-ids?kind=album")
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        flags = {r["id"]: r["in_import_review"] for r in res.get_json()["items"]}
        self.assertEqual(flags, {"album:1": True, "album:2": False})

        import backend.missing_mb_batch as mmb
        rows, err = mmb.select_rows({"ids": ["album:1"]})
        self.assertEqual(err, "")
        album = mock.Mock(mb_albumid="", mb_releasegroupid="")
        with mock.patch.object(mmb.lib, "get_album", return_value=album), \
                mock.patch.object(mmb, "identify_import_folder", side_effect=AssertionError("must not identify")):
            proof = mmb.prove_row(rows[0])
        self.assertFalse(proof["proven"])
        self.assertEqual(proof["reasons"], ["in_import_review"])
        self.assertEqual(proof["write"], {})


if __name__ == "__main__":
    unittest.main()
