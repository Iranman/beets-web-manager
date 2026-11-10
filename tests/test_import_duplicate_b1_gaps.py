"""B1 gaps left by test_import_duplicate_already_in_library.py: an old plugin
that sends no ``duplicate_evidence`` key at all, a folder whose every task is a
proven duplicate (with and without a partial import), one proven plus one
unproven duplicate, and a qBittorrent re-run over several torrents that must
add no Review row. Beets is stubbed at ``reimport_source``.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from tests import test_import_duplicate_already_in_library as d
    from tests import test_import_qbittorrent_source as q
except ImportError:  # pragma: no cover
    import test_import_duplicate_already_in_library as d
    import test_import_qbittorrent_source as q


def setUpModule():
    q.setUpModule()


class B1GapTests(q._Batch):
    # d.BatchTests' fixture and helpers, without re-running its own tests.
    skipped = d.BatchTests.skipped
    run_local = d.BatchTests.run_local

    def setUp(self):
        super().setUp()
        p = mock.patch.object(q.ai, "identify_import_folder", return_value={
            "candidates": [], "warnings": [], "auto_import": {"eligible": False, "reasons": ["no_proof"]}})
        p.start()
        self.addCleanup(p.stop)

    def test_old_plugin_without_an_evidence_key_goes_to_review(self):
        state = self.run_local("Old", lambda f: [{"path": str(f), "reason": "duplicate"}])
        self.assertEqual(state["status"], "review_created", self.log)
        self.assertEqual(self.queued.call_args.kwargs["evidence"]["duplicate_rule"], "no_duplicate_evidence")

    def test_every_task_a_proven_duplicate_is_already_in_library(self):
        state = self.run_local("All", lambda f: [self.skipped(f / "CD1", d.evidence()),
                                                 self.skipped(f / "CD2", d.evidence())])
        self.assertEqual(state["status"], "skipped", self.log)
        self.assertEqual(state["failure_reason"], "already in library")
        self.assertEqual(state["already_in_library_evidence"]["album_ids"], [5])
        self.queued.assert_not_called()

    def test_one_proven_and_one_unproven_duplicate_goes_to_review(self):
        state = self.run_local("Mixed", lambda f: [self.skipped(f / "CD1", d.evidence()),
                                                   self.skipped(f / "CD2", d.evidence(distance=0.05))])
        self.assertEqual(state["status"], "review_created", self.log)
        self.assertEqual(self.queued.call_args.kwargs["evidence"]["duplicate_rule"], "match_not_strong")

    def test_partial_import_with_every_skipped_path_proven_is_imported(self):
        self.audio("Part/CD1/01.flac")
        folder = self.dl / "Part"
        self.local_walk.side_effect = lambda root, log=None, unreadable=None: [str(folder)]
        self.result = {"ok": True, "albums_imported": 1, "imported_album_ids": [7],
                       "skipped": [self.skipped(folder / "CD2", d.evidence()),
                                   self.skipped(folder / "CD3", d.evidence())],
                       "not_matched": [str(folder / "CD2"), str(folder / "CD3")], "not_matched_known": True}
        out, folders = self.run_batch(source="local", qbit_inventory=None)
        state = folders[str(folder)]
        self.assertEqual(state["status"], "imported", self.log)
        self.assertIn("2 skipped path(s) are already in the library", state["note"])
        self.queued.assert_not_called()

    def test_qbittorrent_rerun_over_imported_torrents_adds_no_review_row(self):
        names = ["Album A", "Album B", "Album C"]
        for name in names:
            self.torrent(name, ["01.flac", "02.flac"])
        out, folders = self.run_batch()  # first run: Beets imports all three
        self.assertEqual({f["status"] for f in folders.values()}, {"imported"}, self.log)
        # Second run (a new batch): Beets now skips each as a proven duplicate.
        self.batch_id += "-rerun"
        self.reimport.side_effect = lambda f, opts, timeout: {
            "ok": True, "albums_imported": 0, "imported_album_ids": [],
            "skipped": [self.skipped(f, d.evidence())], "not_matched": [f], "not_matched_known": True}
        with mock.patch.object(q.ai, "identify_import_folder",
                               side_effect=AssertionError("Stage 2 for a proven duplicate")):
            out, folders = self.run_batch()
        self.assertEqual(len(folders), 3)
        for name in names:
            state = folders[str(self.dl / name)]
            self.assertEqual((state["status"], state["failure_reason"]), ("skipped", "already in library"), self.log)
        self.queued.assert_not_called()
        self.assertEqual(out["folders_review"], 0)


if __name__ == "__main__":
    unittest.main()
