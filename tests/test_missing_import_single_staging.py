"""#427: a missing-track download is staged once per attempt, and a retried
release keeps one Review row instead of one per peer or source."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.import_service as imp
import backend.library_service as ls
import backend.pending_review_store as prs

REL = "a0000000-0000-0000-0000-00000000000a"
OTHER_REL = "b0000000-0000-0000-0000-00000000000b"
TARGETS = [{"track": 2, "disc": 1, "title": "Two"}, {"track": 4, "disc": 1, "title": "Four"}]


class SingleStagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.downloads = root / "downloads"
        self.stage_root = self.downloads / "_beets_missing_import"
        self.downloads.mkdir()
        self.store = root / "pending.json"
        self.decisions = []
        patches = [
            mock.patch.object(imp, "validated_downloads_root", return_value=self.downloads),
            mock.patch.object(imp, "DOWNLOADS_ROOT", self.downloads),
            mock.patch.object(imp, "MUSIC_ROOT", root / "music"),
            mock.patch.object(ls, "DOWNLOADS_ALLOWED_ROOTS", [self.downloads]),
            mock.patch.object(prs, "DOWNLOADS_ALLOWED_ROOTS", [self.downloads], create=True),
            mock.patch.object(prs, "DOWNLOADS_ROOT", self.downloads),
            mock.patch.object(prs, "_AI_PENDING_FILE", self.store),
            mock.patch.object(prs, "_library_album_ids_for_folder", return_value=[]),
            mock.patch.object(prs, "_review_origin_payload", return_value={"origin_type": "x"}),
            mock.patch.object(prs, "_reconcile_pending_review_enqueue_item",
                              side_effect=lambda i: (i, False)),
            mock.patch.object(prs, "_record_ai_review_decision",
                              side_effect=lambda action, path, *a, **k: self.decisions.append((action, path))),
            # Staging-only delete stand-in for the engine's classified delete.
            mock.patch.object(prs.composite_workflows, "delete_file",
                              side_effect=lambda p: shutil.rmtree(p)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _download(self, n):
        dl = self.downloads / "slskd" / f"peer{n}" / "Album"
        dl.mkdir(parents=True)
        files = []
        for name in ("02 two.flac", "04 four.flac"):
            f = dl / name
            f.write_bytes(b"synthetic")
            files.append(f)
        (dl / "05 extra.flac").write_bytes(b"synthetic")
        return dl, files

    def _staged_folders(self):
        return sorted(p for p in self.stage_root.iterdir()) if self.stage_root.exists() else []

    def _rows(self):
        return json.loads(self.store.read_text()) if self.store.exists() else []

    def _failed_attempt(self, n, release=REL):
        """One peer: acquisition stages, import re-scans and stages, Beets
        refuses (Review queued), acquisition cleans its staged folder."""
        log = []
        dl, files = self._download(n)
        staged_a = imp._stage_selected_audio_files(dl, files, "Artist", "Album", log, target_tracks=TARGETS)
        useful = sorted(p for p in Path(staged_a).rglob("*.flac"))
        import_dir = imp._stage_selected_audio_files(staged_a, useful, "Artist", "Album", log,
                                                     target_tracks=TARGETS)
        self.assertTrue(prs._queue_folder_for_manual_review(
            import_dir, {"mb_albumid": release}, "not matched", log, allow_existing=True))
        imp._delete_staged_import_folder(staged_a, log)
        return staged_a, import_dir

    def test_import_does_not_restage_an_app_staged_folder(self):
        staged_a, import_dir = self._failed_attempt(1)
        self.assertEqual(import_dir, staged_a)
        folders = self._staged_folders()
        self.assertEqual(folders, [Path(staged_a)])
        rows = self._rows()
        self.assertEqual([r["path"] for r in rows], [staged_a])
        self.assertTrue(Path(rows[0]["path"]).is_dir())  # not dangling

    def test_retries_replace_the_release_review_row(self):
        other_a, _ = self._failed_attempt(0, release=OTHER_REL)
        for n in (1, 2, 3):
            last_a, _ = self._failed_attempt(n)
        rows = self._rows()
        self.assertEqual(sorted(r["path"] for r in rows), sorted([other_a, last_a]))
        self.assertEqual(self._staged_folders(), sorted([Path(other_a), Path(last_a)]))
        self.assertEqual(sum(1 for a, _ in self.decisions if a == "resolved"), 2)

    def test_cleanup_still_removes_a_folder_without_review(self):
        log = []
        dl, files = self._download(1)
        staged = imp._stage_selected_audio_files(dl, files, "Artist", "Album", log, target_tracks=TARGETS)
        self.assertTrue(imp._delete_staged_import_folder(staged, log))
        self.assertEqual(self._staged_folders(), [])

    def test_subset_of_a_staged_folder_is_still_staged(self):
        log = []
        dl, files = self._download(1)
        staged = imp._stage_selected_audio_files(dl, files, "Artist", "Album", log, target_tracks=TARGETS)
        one = sorted(Path(staged).rglob("*.flac"))[:1]
        again = imp._stage_selected_audio_files(staged, one, "Artist", "Album", log,
                                                target_tracks=TARGETS[:1])
        self.assertNotEqual(again, staged)


if __name__ == "__main__":
    unittest.main()
