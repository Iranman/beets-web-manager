"""SEC-006: a failed Review Queue load must never be saved over the queue.

Triggers: Beets unavailable during the prune, an unreadable queue file, a
torn (non-JSON) queue file. Writers: _add_to_pending and
DELETE /api/ai-pending-review with a path. Saves are atomic.
"""

import json
import os
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


class QueueOverwriteTests(unittest.TestCase):
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

    def _beets_down_after_first_call(self):
        calls = {"n": 0}

        def lib_ids(path):
            calls["n"] += 1
            if calls["n"] == 1:
                return []
            raise BeetsUnavailableError("beets timed out")
        return {"side_effect": lib_ids}

    def _paths(self):
        return [i["path"] for i in json.loads(self.store.read_text())]

    # _add_to_pending
    def test_add_keeps_rows_when_beets_is_down_during_prune(self):
        self._patches(library_ids=self._beets_down_after_first_call())
        self.assertTrue(prs._add_to_pending(self.c, {"album": "X"}))
        self.assertEqual(self._paths(), [self.a, self.b, self.c])

    def test_add_refuses_when_queue_file_is_unreadable(self):
        self._patches(store=_Unreadable(self.store))
        with self.assertRaises(prs.PendingReviewLoadError):
            prs._add_to_pending(self.c, {"album": "X"})
        self.assertEqual(self.store.read_text(), self.original)

    def test_add_refuses_when_queue_file_is_torn(self):
        for torn in (self.original[:40], '{"not": "a list"}'):
            with self.subTest(torn=torn):
                self.store.write_text(torn)
                self._patches()
                with self.assertRaises(prs.PendingReviewLoadError):
                    prs._add_to_pending(self.c, {"album": "X"})
                self.assertEqual(self.store.read_text(), torn)

    def test_list_only_load_still_returns_empty_on_failure(self):
        self._patches(store=_Unreadable(self.store))
        self.assertEqual(prs._load_pending_reviews(), [])

    def test_missing_queue_file_is_an_empty_queue(self):
        self.store.unlink()
        self._patches()
        self.assertTrue(prs._add_to_pending(self.c, {"album": "X"}))
        self.assertEqual(self._paths(), [self.c])

    # Atomic save
    def test_failed_save_leaves_old_file_intact(self):
        self._patches()
        real_fdopen = os.fdopen

        def half_write(fd, *a, **k):
            fh = real_fdopen(fd, *a, **k)
            real_write = fh.write

            def write(text):
                real_write(text[:20])
                fh.flush()
                raise OSError(28, "No space left on device")
            fh.write = write
            return fh

        with mock.patch("backend.beets_plugins.os.fdopen", side_effect=half_write), \
                self.assertRaises(OSError):
            prs._add_to_pending(self.c, {"album": "X"})
        self.assertEqual(self.store.read_text(), self.original)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["downloads", "pending.json"])

    # DELETE /api/ai-pending-review {"path": ...}
    def _delete_a(self):
        import app as app_module  # noqa: F401  (registers routes)
        import routes_import
        with mock.patch.object(routes_import, "_record_ai_review_decision"), \
                routes_import.app.test_request_context(
                    "/api/ai-pending-review", method="DELETE", json={"path": self.a}):
            resp = routes_import.clear_ai_pending_review()
        if isinstance(resp, tuple):
            body, status = resp
            return status, json.dumps(body if isinstance(body, dict) else body.get_json())
        return resp.status_code, json.dumps(resp.get_json())

    def test_delete_path_keeps_other_rows_when_beets_is_down(self):
        self._patches(library_ids={"side_effect": BeetsUnavailableError("beets timed out")})
        status, _ = self._delete_a()
        self.assertEqual(status, 200)
        self.assertEqual(self._paths(), [self.b])

    def test_delete_path_refuses_when_queue_file_is_unreadable(self):
        self._patches(store=_Unreadable(self.store))
        status, body = self._delete_a()
        self.assertEqual(status, 503)
        self.assertNotIn("LEAK_MARKER", body)
        self.assertNotIn("Permission", body)
        self.assertEqual(self.store.read_text(), self.original)

    def test_delete_path_refuses_when_queue_file_is_torn(self):
        self.store.write_text(self.original[:40])
        self._patches()
        status, _ = self._delete_a()
        self.assertEqual(status, 503)
        self.assertEqual(self.store.read_text(), self.original[:40])


if __name__ == "__main__":
    unittest.main()
