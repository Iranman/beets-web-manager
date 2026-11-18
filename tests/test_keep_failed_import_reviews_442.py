"""#442 / #446: a verification-FAILED import must keep its manual Review entry
until the user EXPLICITLY dismisses it, instead of being silently pruned.

The terminal import-failure codes ``existing_album_merge_failed``,
``release_group_mismatch`` and ``import_ambiguous`` are stamped onto the Review
row's evidence as ``import_failure_code``. Such a row must survive every
auto-resolution path -- the staged folder being deleted, the move-import leaving
no audio, and the referenced merge-target album looking healthy -- and only
clear on an explicit dismiss or a successful re-import/repair.

Both directions are tested: the terminal rows survive (the reported bug), and a
non-terminal row with the *same* conditions still prunes (the inverse risk --
not making every review immune).
"""
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import backend.pending_review_store as prs

TERMINAL_CODES = ["existing_album_merge_failed", "release_group_mismatch", "import_ambiguous"]


def _row(path, *, code=None):
    row = {
        "path": path,
        "origin_type": "x",
        "suggestion": {"origin_type": "x", "allow_existing_review": True, "kept_album_ids": [7]},
    }
    if code is not None:
        row["evidence"] = {"import_failure_code": code}
    return row


class KeepFailedImportReviewTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.downloads = self.root / "downloads"
        self.downloads.mkdir(parents=True)
        self.store = self.root / "pending.json"
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(prs, "_AI_PENDING_FILE", self.store))
        stack.enter_context(mock.patch.object(prs, "DOWNLOADS_ROOT", self.downloads))
        stack.enter_context(mock.patch.object(prs, "_record_ai_review_decision"))
        stack.enter_context(mock.patch.object(
            prs, "_reconcile_pending_review_enqueue_item", side_effect=lambda i: (i, False)))
        # No Beets: the referenced-album check never runs for a terminal row
        # (short-circuited) and returns "no library album" otherwise.
        stack.enter_context(mock.patch.object(prs, "_library_album_ids_for_folder", return_value=[]))

    def _folder(self, name):
        p = self.downloads / name
        p.mkdir()
        return str(p)

    def _write(self, rows):
        self.store.write_text(json.dumps(rows))

    def _paths(self):
        return [i["path"] for i in prs._load_pending_reviews()]

    # (a) staged folder deleted -------------------------------------------------
    def test_terminal_rows_survive_when_staged_folder_is_deleted(self):
        for code in TERMINAL_CODES:
            with self.subTest(code=code):
                path = self._folder(code)
                self._write([_row(path, code=code)])
                Path(path).rmdir()  # acquisition deleted the staged folder
                self.assertIn(path, self._paths(),
                              f"{code} row was pruned when the folder was deleted")

    # (a') move-import left no audio --------------------------------------------
    def test_terminal_rows_survive_when_folder_has_no_audio(self):
        for code in TERMINAL_CODES:
            with self.subTest(code=code):
                path = self._folder(code)  # exists but empty: no audio
                self._write([_row(path, code=code)])
                self.assertIn(path, self._paths(),
                              f"{code} row was pruned as no-audio-resolved")

    # (a'') referenced album looks healthy --------------------------------------
    def test_terminal_rows_survive_when_referenced_album_is_healthy(self):
        path = self._folder("rg")
        self._write([_row(path, code="release_group_mismatch")])
        with mock.patch.object(prs, "_library_album_ids_for_folder", return_value=[7]), \
                mock.patch.object(prs, "_review_album_is_resolved", return_value=True):
            self.assertIn(path, self._paths(),
                          "terminal row was pruned because the kept album looked healthy")

    # (b) explicit dismiss clears it, durably -----------------------------------
    def test_dismiss_removes_terminal_row_and_it_stays_gone(self):
        path = self._folder("gone")
        self._write([_row(path, code="existing_album_merge_failed")])
        # Explicit removal (what the dismiss endpoint / a successful repair does).
        self.assertTrue(prs._remove_pending_review_for_path(path))
        self.assertNotIn(path, self._paths())
        # Survives a cleanup sweep / restart: a fresh prune-load never re-adds it.
        self.assertEqual(self._paths(), [])

    # (c) inverse risk: a non-terminal row still prunes -------------------------
    def test_non_terminal_row_is_still_pruned_when_folder_gone(self):
        term = self._folder("keep")
        plain = self._folder("drop")
        other = self._folder("drop2")
        self._write([
            _row(term, code="import_ambiguous"),
            _row(plain, code=None),                 # ordinary review, no code
            _row(other, code="membership_changed"),  # a code NOT in the terminal set
        ])
        for p in (plain, other):
            Path(p).rmdir()
        paths = self._paths()
        self.assertIn(term, paths, "terminal row must survive")
        self.assertNotIn(plain, paths, "ordinary review must still prune when folder gone")
        self.assertNotIn(other, paths, "a non-terminal code must still prune when folder gone")

    def test_predicate_matches_only_the_three_codes(self):
        for code in TERMINAL_CODES:
            self.assertTrue(prs._review_is_terminal_import_failure({"evidence": {"import_failure_code": code}}))
        for bad in ("membership_changed", "import_verify_failed", "", None):
            self.assertFalse(prs._review_is_terminal_import_failure({"evidence": {"import_failure_code": bad}}))
        self.assertFalse(prs._review_is_terminal_import_failure({}))

    # Finding B: a same-path re-queue must REPLACE the evidence packet --------
    def test_requeue_as_non_terminal_clears_terminal_immunity(self):
        # Re-queuing the SAME path as an ordinary review with no evidence must
        # drop the stale terminal code (not inherit it), so the row loses its
        # immunity and prunes normally. Before the fix, _add_to_pending only
        # replaced evidence when the new packet was truthy, so the old code
        # stayed and the now-ordinary row gained permanent immunity.
        path = self._folder("requeue")  # empty folder => no-audio resolving condition
        self._write([_row(path, code="existing_album_merge_failed")])
        self.assertIn(path, self._paths())  # terminal: immune to the no-audio prune
        prs._add_to_pending(path, {"origin_type": "x"}, allow_existing=True, evidence=None)
        stored = json.loads(self.store.read_text())
        self.assertEqual(len(stored), 1, "re-queue must update in place, not append")
        self.assertFalse(prs._review_is_terminal_import_failure(stored[0]),
                         "re-queue must clear the stale terminal import_failure_code")
        self.assertNotIn(path, self._paths(),
                         "the now-ordinary re-queued row must prune normally")

    # Finding C: the N3 concurrency signature must track the terminal flag ----
    def test_resolution_sig_distinguishes_terminal_from_ordinary(self):
        base = {"path": "/d/x",
                "suggestion": {"allow_existing_review": True, "existing_album_ids": [7]},
                "evidence": {"conflict": "c"}}
        terminal = {**base, "evidence": {"conflict": "c",
                                         "import_failure_code": "release_group_mismatch"}}
        self.assertNotEqual(prs._review_resolution_sig(base),
                            prs._review_resolution_sig(terminal),
                            "a row converted to terminal under the lock must change its signature")


class ReimportStampsFailureCodeTests(unittest.TestCase):
    """The import flow stamps the terminal code onto the Review evidence."""

    def _run(self, failure_code):
        import backend.import_service as isvc
        captured = {}

        def fake_queue(folder, suggestion, reason, log=None, allow_existing=False,
                       evidence=None, origin=None, carrier=None):
            captured["evidence"] = evidence
            return True

        ctx = mock.Mock()
        ctx.log = []
        with mock.patch.object(isvc, "_queue_folder_for_manual_review", side_effect=fake_queue):
            # _reimport_keep_for_review always raises to fail the job after queueing.
            with self.assertRaises(RuntimeError):
                isvc._reimport_keep_for_review(ctx, "/dl/a", "mbid", "reason", [7],
                                               failure_code=failure_code)
        return captured.get("evidence")

    def test_terminal_code_is_stamped(self):
        for code in TERMINAL_CODES:
            with self.subTest(code=code):
                self.assertEqual(self._run(code), {"import_failure_code": code})

    def test_no_code_leaves_evidence_unset(self):
        self.assertIsNone(self._run(""))


class CleanupStaleKeepsTerminalTests(unittest.TestCase):
    """Finding A: the 'Clean up stale' button (POST /api/import/cleanup-stale)
    must NOT remove a terminal import-failure row whose staged folder was
    deleted, while it must still remove an ordinary row whose folder is gone.
    Before the fix the folder-gone branch had no terminal guard, so the user's
    own #1 scenario (deleted staged folder) silently pruned the terminal row."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.downloads = self.root / "downloads"
        self.downloads.mkdir(parents=True)
        self.store = self.root / "pending.json"
        # Both folders are GONE on disk (never created), so the folder-gone
        # branch fires for both rows.
        self.term = str(self.downloads / "terminal")
        self.plain = str(self.downloads / "plain")
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(prs, "_AI_PENDING_FILE", self.store))
        stack.enter_context(mock.patch.object(prs, "DOWNLOADS_ROOT", self.downloads))
        stack.enter_context(mock.patch.object(prs, "_record_ai_review_decision"))
        stack.enter_context(mock.patch.object(prs, "_library_album_ids_for_folder", return_value=[]))
        stack.enter_context(mock.patch.object(
            prs, "_reconcile_pending_review_enqueue_item", side_effect=lambda i: (i, False)))
        self.store.write_text(json.dumps([
            _row(self.term, code="existing_album_merge_failed"),
            _row(self.plain, code=None),
        ]))

    def test_cleanup_stale_keeps_terminal_but_removes_plain(self):
        import app  # noqa: F401  (registers routes)
        import routes_import
        with routes_import.app.test_request_context("/api/import/cleanup-stale", method="POST"):
            resp = routes_import.import_cleanup_stale()
        body = resp.get_json()
        self.assertTrue(body["ok"])
        remaining = [i["path"] for i in prs._load_pending_reviews()]
        self.assertIn(self.term, remaining,
                      "terminal row was removed by cleanup-stale when its folder was gone (bug A)")
        self.assertNotIn(self.plain, remaining,
                         "ordinary gone-folder row must still be removed by cleanup-stale")


if __name__ == "__main__":
    unittest.main()
