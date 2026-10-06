"""S1 containment follow-up regressions (PR #174 QA FAIL + security F1-F5).

F1  data dir, transactions DB, *.db files and backups are never deleted/moved.
F2  a staging root itself is never a move source or destination.
F3  removal re-checks the resolved entry with lstat (no symlink swap).
F4  relative paths are absolutized before the symlink-component check.
F5  album-cleanup approval needs the delete phrase; rollback/dedup check CAS.
Plus: row removal failures end Failed, rows-only internal cleanup, the Clean
All safe rename through the engine, and the MUSIC_ROOT prefix check.

Everything runs against temp directories and a fake adapter.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.composite_workflows as cw
from tests.test_wave0_s1_containment import FakeAdapter, _Env


def _can_symlink() -> bool:
    with tempfile.TemporaryDirectory() as d:
        try:
            os.symlink(d, os.path.join(d, "l"), target_is_directory=True)
            return True
        except (OSError, NotImplementedError, AttributeError):
            return False


CAN_SYMLINK = _can_symlink()


class ProtectedDataTests(_Env):
    """F1: the Web Manager data dir and every database file are off-limits."""

    def _targets(self):
        (self.data / "backups").mkdir(exist_ok=True)
        tx_db = self.data / "transactions.db"
        tx_db.write_bytes(b"db")
        dl_db = self.dl / "x.db"
        dl_db.write_bytes(b"db")
        backup = self.data / "backups" / "x"
        backup.write_bytes(b"bk")
        return [self.data, tx_db, dl_db, backup]

    def test_delete_staging_file_refuses_protected(self):
        for target in self._targets():
            with self.assertRaises(ValueError, msg=str(target)):
                cw.delete_staging_file(str(target))
            self.assertTrue(target.exists(), target)

    def test_move_staging_file_refuses_protected_source_and_destination(self):
        src_ok = self.dl / "ok.flac"
        src_ok.write_bytes(b"a")
        for target in self._targets():
            with self.assertRaises(ValueError, msg=f"src {target}"):
                cw.move_staging_file(str(target), str(self.dl / "moved"))
            self.assertTrue(target.exists(), target)
        with self.assertRaises(ValueError):
            cw.move_staging_file(str(src_ok), str(self.dl / "y.db"))
        with self.assertRaises(ValueError):
            cw.move_staging_file(str(src_ok), str(self.data / "backups" / "ok.flac"))
        self.assertTrue(src_ok.exists())

    def test_move_file_refuses_protected(self):
        for target in self._targets():
            with self.assertRaises(ValueError, msg=str(target)):
                cw.move_file(str(target), str(self.dl / "moved"))
            self.assertTrue(target.exists(), target)
        src_ok = self.dl / "ok.flac"
        src_ok.write_bytes(b"a")
        with self.assertRaises(ValueError):
            cw.move_file(str(src_ok), str(self.data / "transactions2.db"))
        self.assertTrue(src_ok.exists())


class StagingRootTests(_Env):
    """F2: a staging root is neither moved nor overwritten."""

    def test_move_file_refuses_staging_root(self):
        (self.dl / "keep.flac").write_bytes(b"a")
        other = self.root / "other"
        other.mkdir()
        with self.assertRaises(ValueError):
            cw.move_file(str(self.dl), str(self.dl / "sub"))
        src = self.dl / "a.flac"
        src.write_bytes(b"a")
        with self.assertRaises(ValueError):
            cw.move_file(str(src), str(self.dl))
        self.assertTrue(src.exists())
        self.assertTrue((self.dl / "keep.flac").exists())

    def test_move_staging_file_refuses_staging_root(self):
        (self.dl / "keep.flac").write_bytes(b"a")
        with self.assertRaises(ValueError):
            cw.move_staging_file(str(self.dl), str(self.dl / "sub"))
        src = self.dl / "a.flac"
        src.write_bytes(b"a")
        with self.assertRaises(ValueError):
            cw.move_staging_file(str(src), str(self.dl))
        self.assertTrue(src.exists())

    def test_delete_staging_file_refuses_staging_root(self):
        (self.dl / "keep.flac").write_bytes(b"a")
        with self.assertRaises(ValueError):
            cw.delete_staging_file(str(self.dl))
        self.assertTrue((self.dl / "keep.flac").exists())


@unittest.skipUnless(CAN_SYMLINK, "symlinks are not available on this platform/user")
class SymlinkRecheckTests(_Env):
    """F3 + F4."""

    def test_remove_resolved_refuses_symlink(self):
        victim = self.music / "Album"
        victim.mkdir()
        (victim / "01.flac").write_bytes(b"a")
        link = self.dl / "swap"
        os.symlink(victim, link, target_is_directory=True)
        with self.assertRaises(ValueError):
            cw._remove_resolved(link)
        self.assertTrue((victim / "01.flac").exists())

    def test_remove_resolved_refuses_swapped_entry(self):
        folder = self.dl / "junk"
        folder.mkdir()
        resolved = folder.resolve()
        real_lstat = os.lstat
        victim = self.music / "Album"
        victim.mkdir()
        (victim / "01.flac").write_bytes(b"a")
        calls = {"n": 0}

        def swapping_lstat(p, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:  # swap between the first and the re-check
                os.rmdir(folder)
                os.symlink(victim, folder, target_is_directory=True)
            return real_lstat(p, *a, **kw)

        with mock.patch.object(cw.os, "lstat", side_effect=swapping_lstat):
            with self.assertRaises(ValueError):
                cw._remove_resolved(resolved)
        self.assertTrue((victim / "01.flac").exists())

    def test_no_audio_cleanup_refuses_symlinked_folder(self):
        import backend.cleanup_service as cs
        victim = self.music / "Album"
        victim.mkdir()
        (victim / "cover.jpg").write_bytes(b"x")
        link = self.dl / "junk"
        os.symlink(victim, link, target_is_directory=True)
        with mock.patch.object(cs, "MUSIC_ROOT", self.music), \
                mock.patch.object(cs, "FOLDER_CLEAN_ROOTS", [self.music, self.dl]), \
                mock.patch.object(cs, "_scan_no_audio_folder_candidates",
                                  return_value={"folders": [{"path": str(link), "files": 1, "bytes": 1}]}):
            res = cs._delete_no_audio_folders(str(self.dl), [str(link)], dry_run=False, log=[])
        self.assertFalse(res["ok"])
        self.assertTrue((victim / "cover.jpg").exists())

    def test_relative_path_through_symlink_is_detected(self):
        real = self.root / "real"
        real.mkdir()
        (real / "f.flac").write_bytes(b"a")
        os.symlink(real, self.dl / "lnk", target_is_directory=True)
        cwd = os.getcwd()
        os.chdir(self.dl)
        self.addCleanup(os.chdir, cwd)
        self.assertTrue(cw._has_symlink_component(os.path.join("lnk", "f.flac")))
        self.assertTrue(cw._has_symlink_component(Path("lnk") / "f.flac"))


class ApproveRouteTests(_Env):
    """F5: approving a file-deleting album cleanup needs the explicit phrase."""

    @classmethod
    def setUpClass(cls):
        # Import the app once inside a throwaway data dir: app import opens
        # SQLite files there, which Windows cannot delete while open.
        cls._import_tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        with mock.patch.dict(os.environ, {"WEB_MANAGER_DATA_DIR": cls._import_tmp.name}):
            import app  # noqa: F401

    @classmethod
    def tearDownClass(cls):
        cls._import_tmp.cleanup()

    def setUp(self):
        super().setUp()
        import app as app_module
        p = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        p.start()
        self.addCleanup(p.stop)
        self.client = app_module.app.test_client()

    def test_album_cleanup_delete_files_needs_phrase(self):
        import routes_maintenance
        tx = self.store.create(operation_type="Delete", status="Preview",
                               metadata={"mutation_family": cw.ALBUM_CLEANUP_FAMILY, "delete_files": True,
                                         "album_id": 8})
        with mock.patch.object(routes_maintenance, "transactions", self.store):
            bare = self.client.post(f"/api/transactions/{tx['id']}/approve")
            self.assertEqual(bare.status_code, 400, bare.get_json())
            self.assertEqual(bare.get_json()["code"], "confirmation_required")
            wrong = self.client.post(f"/api/transactions/{tx['id']}/approve",
                                     json={"confirm_delete_files": "yes"})
            self.assertEqual(wrong.status_code, 400)
            self.assertEqual(self.store.get(tx["id"])["status"], "Preview")
            ok = self.client.post(f"/api/transactions/{tx['id']}/approve",
                                  json={"confirm_delete_files": cw.DELETE_ALBUM_FILES_CONFIRMATION})
            self.assertEqual(ok.status_code, 200, ok.get_json())
        self.assertEqual(self.store.get(tx["id"])["status"], "Approved")

    def test_row_only_album_cleanup_and_unknown_id(self):
        import routes_maintenance
        tx = self.store.create(operation_type="Delete", status="Preview",
                               metadata={"mutation_family": cw.ALBUM_CLEANUP_FAMILY, "delete_files": False})
        with mock.patch.object(routes_maintenance, "transactions", self.store):
            self.assertEqual(self.client.post(f"/api/transactions/{tx['id']}/approve").status_code, 200)
            self.assertEqual(self.client.post("/api/transactions/nope/approve").status_code, 404)


class RollbackCasTests(_Env):
    """F5: a lost compare-and-set during rollback is reported, not hidden."""

    def test_track_quarantine_rollback_reports_conflict(self):
        tx = self.store.create(operation_type="Delete", status="Completed",
                               metadata={"mutation_family": cw.TRACK_QUARANTINE_FAMILY,
                                         "engine_result": {"quarantine_id": "b" * 32}})
        ad = FakeAdapter()
        ad.rollback_quarantine_remove_items = lambda qid, idempotency_key=None: {
            "ok": True, "result": {"ok": True, "restored": []}}
        with mock.patch.object(self.store, "transition", return_value=None):
            res = cw.rollback_track_quarantine(tx["id"], adapter=ad, store=self.store)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "conflict")


class _RaisingAdapter(FakeAdapter):
    def remove(self, item_ids=None, album_ids=None, delete_files=False, idempotency_key=None):
        self.calls.append(("remove", sorted(item_ids or []), sorted(album_ids or []), delete_files))
        raise RuntimeError("engine refused")


class RowRemovalFailureTests(_Env):
    """sync_deleted / orphan cleanup end Failed when the engine raises."""

    def _adapter(self):
        present = self.media("A/B/01.flac")
        missing = str(self.music / "A/B/02.flac")
        return _RaisingAdapter(items={2: {"id": 2, "album_id": 3, "path": missing},
                                      **{i: {"id": i, "album_id": 3, "path": present} for i in range(10, 30)}},
                               albums={3: {"id": 3}})

    def test_sync_deleted_marks_failed(self):
        ad = self._adapter()
        res = cw.sync_deleted_files(dry_run=False, adapter=ad, item_ids=[2], store=self.store)
        self.assertFalse(res["ok"], res)
        self.assertEqual(res["code"], "remove_failed")
        self.assertEqual(self.store.get(res["operation_id"])["status"], "Failed")

    def test_clean_orphaned_marks_failed(self):
        ad = self._adapter()
        res = cw.clean_orphaned_items(item_ids=[2], dry_run=False, adapter=ad, store=self.store)
        self.assertFalse(res["ok"], res)
        self.assertEqual(res["code"], "remove_failed")
        self.assertEqual(self.store.get(res["operation_id"])["status"], "Failed")


class RowsOnlyCleanupTests(_Env):
    """Item 1: internal stale-row cleanup is rows-only, approved, claimed once."""

    def test_rows_only_and_single_apply(self):
        path = self.media("A/01.flac")
        ad = FakeAdapter(items={1: {"id": 1, "album_id": None, "path": path}})
        res = cw.remove_item_rows_keep_files([1], reason="t", approved_by="test", adapter=ad, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["files_deleted"], 0)
        self.assertEqual(ad.destructive_calls(), [("remove", [1], [], False)])
        self.assertTrue(os.path.exists(path))
        tx = self.store.get(res["operation_id"])
        self.assertEqual(tx["status"], "Completed")
        self.assertEqual(tx["metadata"]["approved_by"], "test")
        again = cw.apply_playlist_media_cleanup(res["operation_id"], adapter=ad, store=self.store)
        self.assertEqual(again["code"], "already_applied")
        self.assertEqual(len(ad.destructive_calls()), 1)
        rb = cw.rollback_playlist_media_cleanup(res["operation_id"], store=self.store)
        self.assertEqual(rb["code"], "not_supported")

    def test_unapproved_plan_is_refused(self):
        ad = FakeAdapter(items={1: {"id": 1, "album_id": None, "path": self.media("A/01.flac")}})
        plan = cw.plan_playlist_media_cleanup({"item_ids": [1]}, adapter=ad, store=self.store)
        res = cw.apply_playlist_media_cleanup(plan["operation_id"], adapter=ad, store=self.store)
        self.assertEqual(res["code"], "not_approved")
        self.assertEqual(ad.destructive_calls(), [])


class SafeRenameTests(_Env):
    """Item 2: Clean All folder renames go through the folder_cleanup engine."""

    def test_rename_inside_music_root(self):
        src = self.music / "Artist" / "Old Name"
        src.mkdir(parents=True)
        (src / "01.flac").write_bytes(b"a")
        dst = self.music / "Artist" / "New Name"
        res = cw.safe_rename_library_folder(str(src), str(dst), approved_by="test", store=self.store)
        self.assertTrue(res.get("ok"), res)
        self.assertTrue((dst / "01.flac").exists())
        self.assertFalse(src.exists())
        self.assertEqual(self.store.get(res["operation_id"])["status"], "Completed")

    def test_target_outside_music_root_refused(self):
        src = self.music / "Artist" / "Old"
        src.mkdir(parents=True)
        (src / "01.flac").write_bytes(b"a")
        res = cw.safe_rename_library_folder(str(src), str(self.dl / "Old"), approved_by="test", store=self.store)
        self.assertFalse(res.get("ok"), res)
        self.assertTrue((src / "01.flac").exists())

    def test_clean_all_safe_renames_use_engine(self):
        import backend.maintenance_service as ms
        src = self.music / "Artist" / "Old Name"
        src.mkdir(parents=True)
        (src / "01.flac").write_bytes(b"a")
        dst = self.music / "Artist" / "New Name"
        rows = [{"folder": str(src), "proposed_folder": str(dst), "safe": True, "db_item_count": 0}]
        log = []
        real = cw.safe_rename_library_folder
        def via_test_store(s, t, approved_by):
            return real(s, t, approved_by=approved_by, store=self.store)

        with mock.patch.object(ms, "MUSIC_ROOT", self.music),                 mock.patch.object(cw, "safe_rename_library_folder", side_effect=via_test_store) as eng,                 mock.patch.object(cw, "move_file", side_effect=AssertionError("move_file used")):
            res = ms._maintenance_safe_folder_renames(rows, log, None)
        self.assertEqual(res["renamed"], 1, log)
        self.assertEqual(res["errors"], 0, log)
        eng.assert_called_once()
        self.assertTrue((dst / "01.flac").exists())
        rows, _total = self.store.list(status="Completed")
        self.assertEqual(len(rows), 1, rows)


class PrefixTests(unittest.TestCase):
    def test_sibling_prefix_is_not_under(self):
        from backend.app_runtime import _path_is_under
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(_path_is_under(Path(d) / "music2" / "x", Path(d) / "music"))
            self.assertTrue(_path_is_under(Path(d) / "music" / "x", Path(d) / "music"))


if __name__ == "__main__":
    unittest.main()
