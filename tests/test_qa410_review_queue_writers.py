"""QA #410 (SEC-006): writer paths the PR tests did not cover.

POST /api/ai-pending-review and POST /api/import/cleanup-stale on an
unreadable queue, list-only loads during a Beets outage, the manual-review
wrapper, and the batch final pass when the queue cannot be read.
"""

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import backend.pending_review_store as prs
from backend.beets_adapter import BeetsUnavailableError


class _Unreadable(type(Path())):
    def read_text(self, *a, **k):
        raise PermissionError(13, "Permission denied", "/private/LEAK_MARKER/pending.json")


class Qa410WriterTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.downloads = self.root / "downloads"
        self.a, self.b, self.c = (str(self.downloads / n) for n in "ABC")
        for p in (self.a, self.b, self.c):
            Path(p).mkdir(parents=True)
        self.store = self.root / "pending.json"
        self.original = json.dumps([
            {"path": p, "origin_type": "x", "suggestion": {"origin_type": "x"}} for p in (self.a, self.b)])
        self.store.write_text(self.original)

    def _patches(self, *, store=None, library_ids=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(prs, "_AI_PENDING_FILE", store or self.store))
        stack.enter_context(mock.patch.object(prs, "DOWNLOADS_ROOT", self.downloads))
        stack.enter_context(mock.patch.object(
            prs, "_library_album_ids_for_folder", **(library_ids or {"return_value": []})))
        stack.enter_context(mock.patch.object(prs, "_pending_review_item_is_resolved", return_value=False))
        stack.enter_context(mock.patch.object(
            prs, "_reconcile_pending_review_enqueue_item", side_effect=lambda i: (i, False)))
        stack.enter_context(mock.patch.object(prs, "_record_ai_review_decision"))
        return stack

    @staticmethod
    def _status_body(resp):
        if isinstance(resp, tuple):
            body, status = resp
            return status, json.dumps(body if isinstance(body, dict) else body.get_json())
        return resp.status_code, json.dumps(resp.get_json())

    def test_post_route_refuses_with_fixed_text_on_unreadable_queue(self):
        self._patches(store=_Unreadable(self.store))
        import app as app_module  # noqa: F401  (registers routes)
        import routes_import
        with mock.patch.object(routes_import, "_resolve_import_review_source_path",
                               return_value=(Path(self.c), None)), \
                routes_import.app.test_request_context(
                    "/api/ai-pending-review", method="POST", json={"path": self.c}):
            status, body = self._status_body(routes_import.add_ai_pending_review())
        self.assertEqual(status, 503)
        self.assertNotIn("LEAK_MARKER", body)
        self.assertNotIn("Permission", body)
        self.assertEqual(self.store.read_text(), self.original)

    def test_cleanup_stale_refuses_on_torn_queue(self):
        torn = self.original[:40]
        self.store.write_text(torn)
        self._patches()
        import app as app_module  # noqa: F401
        import routes_import
        with routes_import.app.test_request_context("/api/import/cleanup-stale", method="POST"):
            status, body = self._status_body(routes_import.import_cleanup_stale())
        self.assertNotEqual(status, 200)
        self.assertNotIn("Expecting", body)
        self.assertEqual(self.store.read_text(), torn)

    def test_list_load_during_beets_outage_keeps_rows_on_disk(self):
        self._patches(library_ids={"side_effect": BeetsUnavailableError("beets timed out")})
        self.assertEqual([i["path"] for i in prs._load_pending_reviews()], [self.a, self.b])
        self.assertEqual([i["path"] for i in json.loads(self.store.read_text())], [self.a, self.b])

    def test_manual_review_wrapper_reports_not_queued_on_unreadable_queue(self):
        self._patches(store=_Unreadable(self.store))
        log = []
        self.assertFalse(prs._queue_folder_for_manual_review(self.c, {"album": "X"}, "why", log=log))
        self.assertTrue(any("could not queue manual Review item" in line for line in log))
        self.assertFalse(any("LEAK_MARKER" in line for line in log))
        self.assertEqual(self.store.read_text(), self.original)

    def test_add_refuses_when_prune_save_fails(self):
        # ARCH-033 N3: _add_to_pending no longer prunes under the lock, so the
        # failing write is its own append save (a raw OSError, like SEC-006's
        # overwrite test). A failed write must still refuse (raise) and never
        # corrupt the queue; a later list-only read still prunes the resolved
        # row (its own failed prune save is non-fatal).
        self._patches(library_ids={"side_effect": lambda p: [7] if p == self.a else []})
        real_save = prs._save_pending_reviews
        calls = {"n": 0}

        def save(items):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(28, "No space left on device")
            real_save(items)
        with mock.patch.object(prs, "_save_pending_reviews", side_effect=save):
            with self.assertRaises(OSError):
                prs._add_to_pending(self.c, {"album": "X"})
            self.assertEqual(self.store.read_text(), self.original)
            # A list-only reader still gets the pruned view.
            calls["n"] = 0
            self.assertEqual([i["path"] for i in prs._load_pending_reviews()], [self.b])

    def test_batch_final_pass_survives_unreadable_queue(self):
        import backend.ai_service as ai
        state = {"batch_job_id": "b1", "folder_states": {
            "f1": {"source_folder": self.c, "status": "import_failed", "failure_reason": "Import failed"}}}
        with mock.patch.object(ai, "_pending_review_has_path", return_value=False), \
                mock.patch.object(ai, "_add_to_pending",
                                  side_effect=prs.PendingReviewLoadError("Review Queue could not be loaded.")), \
                mock.patch.object(ai, "_ai_batch_commit"):
            summary = ai._ai_batch_process_decisions(state, [])
        self.assertEqual(summary["errors"], 1)
        self.assertFalse(state["folder_states"]["f1"].get("review_item_id"))


if __name__ == "__main__":
    unittest.main()
