"""QA regressions for PR #174 (Wave 0 S1 containment).

Tests marked expectedFailure document open Wave 0 defects found during
independent QA; they must flip to passing (remove the decorator) once the
owning fix lands. The other tests pin contracts PR #174 introduced.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.composite_workflows as cw
from backend.transaction_engine import TransactionStore


class RecordingAdapter:
    def __init__(self):
        self.remove_calls = []

    def remove(self, item_ids=None, album_ids=None, delete_files=False, idempotency_key=None):
        self.remove_calls.append({"item_ids": list(item_ids or []), "album_ids": list(album_ids or []),
                                  "delete_files": bool(delete_files)})
        return {"ok": True}


class _Env(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.music, self.dl, self.data = root / "music", root / "downloads", root / "data"
        for d in (self.music, self.dl, self.data):
            d.mkdir()
        env = mock.patch.dict(os.environ, {"MUSIC_ROOT": str(self.music), "DOWNLOAD_PATH": str(self.dl),
                                           "BEETS_IMPORT_ROOTS": str(self.dl),
                                           "WEB_MANAGER_DATA_DIR": str(self.data)})
        env.start()
        self.addCleanup(env.stop)
        self.store = TransactionStore(str(self.data / "tx"))


class PlaylistMediaCleanupTests(_Env):
    @unittest.expectedFailure  # QA-174-F1: unapproved apply deletes media files
    def test_unapproved_preview_never_deletes_media_files(self):
        ad = RecordingAdapter()
        plan = cw.plan_playlist_media_cleanup({"item_ids": [1, 2]}, adapter=ad, store=self.store)
        self.assertEqual(plan["status"], "Preview")
        try:
            cw.apply_playlist_media_cleanup(plan["operation_id"], adapter=ad, store=self.store)
        except Exception:
            pass  # refusing is acceptable
        self.assertFalse(any(c["delete_files"] for c in ad.remove_calls),
                         "Preview-only playlist media cleanup must not delete files")

    @unittest.expectedFailure  # QA-174-F1: second apply repeats the removal
    def test_apply_is_not_repeatable(self):
        ad = RecordingAdapter()
        plan = cw.plan_playlist_media_cleanup({"item_ids": [3]}, adapter=ad, store=self.store)
        self.store.transition(plan["operation_id"], "Preview", "Approved")
        cw.apply_playlist_media_cleanup(plan["operation_id"], adapter=ad, store=self.store)
        try:
            cw.apply_playlist_media_cleanup(plan["operation_id"], adapter=ad, store=self.store)
        except Exception:
            pass
        self.assertLessEqual(len(ad.remove_calls), 1)


class ImportReviewCleanupTests(_Env):
    @unittest.expectedFailure  # QA-174-F4 / LT-12: reports Completed when nothing was removed
    def test_failed_cleanup_is_not_reported_completed(self):
        folder = self.dl / "Album"
        folder.mkdir()
        (folder / "a.flac").write_bytes(b"x")
        tx = self.store.create(operation_type="Delete", status="Approved", summary="t",
                               metadata={"folder": str(folder)})
        with mock.patch.object(cw.shutil, "rmtree", lambda *a, **k: None):
            try:
                res = cw.apply_import_review_cleanup(tx["id"], store=self.store)
            except Exception:
                res = {"ok": False}
        self.assertTrue(folder.exists())
        self.assertFalse(res.get("ok") and res.get("status") == "Completed")


class MoveFileContainmentTests(_Env):
    def test_library_folder_rename_is_refused(self):
        # Pins the PR #174 LT-13 contract that breaks Clean All folder_safe_renames (QA-174-F2).
        src = self.music / "Artist" / "Old"
        src.mkdir(parents=True)
        (src / "t.flac").write_bytes(b"x")
        with self.assertRaises(ValueError):
            cw.move_file(str(src), str(self.music / "Artist" / "New"))
        self.assertTrue((src / "t.flac").exists())

    def test_staging_move_refuses_existing_target_and_escape(self):
        src = self.dl / "a.flac"
        src.write_bytes(b"x")
        (self.dl / "b.flac").write_bytes(b"y")
        with self.assertRaises(ValueError):
            cw.move_file(str(src), str(self.dl / "b.flac"))
        with self.assertRaises(ValueError):
            cw.move_file(str(src), str(self.music / "a.flac"))
        self.assertEqual(src.read_bytes(), b"x")
        self.assertEqual((self.dl / "b.flac").read_bytes(), b"y")
        self.assertTrue(cw.move_file(str(src), str(self.dl / "sub" / "a.flac"))["ok"])


if __name__ == "__main__":
    unittest.main()
