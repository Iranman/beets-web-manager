"""SEC-002 / ARCH-003 Wave 20: Existing-Album Duplicate & Move Bookkeeping Controlled Mutation Boundary.

Comprehensive focused test suite for existing_album_reconcile_v1 mutation family.
"""
import ast
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from backend.transaction_engine import (
    TransactionStore,
    create_existing_album_reconcile_plan,
    execute_existing_album_reconcile_apply,
    rollback_existing_album_reconcile,
)
import app as app_module

ITEMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY,
    album TEXT,
    albumartist TEXT,
    mb_albumid TEXT,
    mb_releasegroupid TEXT,
    year INTEGER
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    album_id INTEGER,
    title TEXT,
    artist TEXT,
    album TEXT,
    albumartist TEXT,
    disc INTEGER,
    track INTEGER,
    path BLOB,
    mb_trackid TEXT,
    mb_albumid TEXT,
    mb_releasegroupid TEXT,
    length REAL,
    size INTEGER,
    mtime REAL
);
"""

RG_A = "aaaaaaaa-0000-0000-0000-000000000000"
RG_B = "bbbbbbbb-0000-0000-0000-000000000000"
REL_A = "11111111-1111-1111-1111-111111111111"
REC_1 = "33333333-3333-3333-3333-333333333331"
REC_2 = "33333333-3333-3333-3333-333333333332"


class Wave20FixtureBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

        self.music_root = self.tmp_path / "music"
        self.music_root.mkdir()
        self.staging_root = self.tmp_path / "staging"
        self.staging_root.mkdir()
        self.quarantine_root = self.tmp_path / "quarantine"
        self.quarantine_root.mkdir()
        self.outside_root = self.tmp_path / "outside"
        self.outside_root.mkdir()

        store_dir = self.tmp_path / "transactions"
        store_dir.mkdir()
        self.store = TransactionStore(root=str(store_dir))

        self.db_path = self.tmp_path / "musiclibrary.db"
        with sqlite3.connect(self.db_path) as con:
            con.executescript(ITEMS_SCHEMA)
            con.commit()

        app_module.app.config["TESTING"] = True

        @contextmanager
        def _mock_db_cm(*args, **kwargs):
            con = sqlite3.connect(self.db_path)
            if "text_factory" in kwargs:
                con.text_factory = kwargs["text_factory"]
            if "row_factory" in kwargs:
                con.row_factory = kwargs["row_factory"]
            try:
                yield con
            finally:
                con.close()

        self._db_patch = mock.patch.object(app_module, "_db", side_effect=_mock_db_cm)
        self._db_patch.start()

        self._env_patch = mock.patch.dict(os.environ, {
            "BEETS_WEB_AUTH_DISABLED": "1",
            "RECONCILE_QUARANTINE_DIR": str(self.quarantine_root),
        })
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._db_patch.stop()
        try:
            self._tmpdir.cleanup()
        except Exception:
            pass

    def _create_album(self, album_id: int, title: str, artist: str = "Test Artist", rg_id: str = RG_A, rel_id: str = REL_A):
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT INTO albums (id, album, albumartist, mb_albumid, mb_releasegroupid, year) VALUES (?, ?, ?, ?, ?, ?)",
                (album_id, title, artist, rel_id, rg_id, 2024),
            )
            con.commit()

    def _create_item(
        self,
        item_id: int,
        album_id: int,
        title: str,
        disc: int,
        track: int,
        filename: str,
        staging: bool = False,
        outside: bool = False,
        rec_id: str = REC_1,
        rg_id: str = RG_A,
    ) -> Path:
        root = self.outside_root if outside else (self.staging_root if staging else self.music_root)
        album_dir = root / f"album_{album_id}"
        album_dir.mkdir(parents=True, exist_ok=True)
        file_path = album_dir / filename
        file_path.write_bytes(b"AUDIO_DATA_" + str(item_id).encode())

        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT INTO items (id, album_id, title, artist, album, albumartist, disc, track, path, mb_trackid, mb_albumid, mb_releasegroupid, length) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item_id,
                    album_id,
                    title,
                    "Test Artist",
                    "Test Album",
                    "Test Artist",
                    disc,
                    track,
                    str(file_path),
                    rec_id,
                    REL_A,
                    rg_id,
                    180.0,
                ),
            )
            con.commit()
        return file_path


class PlanTests(Wave20FixtureBase):
    def test_create_plan_valid(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        # Survivor in existing album
        surv_path = self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.flac")
        # Move item (missing track position)
        move_path = self._create_item(20, 2, "Track 2", 1, 2, "track2_imported.flac", staging=True)
        # Duplicate item (same track position as survivor)
        dup_path = self._create_item(21, 2, "Track 1", 1, 1, "track1_imported.flac", staging=True)

        res = create_existing_album_reconcile_plan(
            self.store,
            {
                "imported_album_id": 2,
                "existing_album_id": 1,
                "move_item_ids": [20],
                "dup_item_ids": [21],
                "dup_details": [{"dup_item_id": 21, "survivor_item_ids": [10]}],
                "source_folder": str(self.staging_root),
            },
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
        )
        self.assertTrue(res.get("ok"), res)
        op_id = res["operation_id"]
        self.assertEqual(res["reconciled_moves"], 1)
        self.assertEqual(res["reconciled_duplicates"], 1)

        tx = self.store.get(op_id)
        self.assertEqual(tx["status"], "Preview")
        self.assertEqual(tx["metadata"]["mutation_family"], "existing_album_reconcile_v1")

    def test_plan_non_mutating(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.flac")
        move_path = self._create_item(20, 2, "Track 2", 1, 2, "track2_imported.flac", staging=True)
        dup_path = self._create_item(21, 2, "Track 1", 1, 1, "track1_imported.flac", staging=True)

        mtime_dup_before = dup_path.stat().st_mtime_ns

        res = create_existing_album_reconcile_plan(
            self.store,
            {
                "imported_album_id": 2,
                "existing_album_id": 1,
                "move_item_ids": [20],
                "dup_item_ids": [21],
                "dup_details": [{"dup_item_id": 21, "survivor_item_ids": [10]}],
                "source_folder": str(self.staging_root),
            },
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
        )
        self.assertTrue(res.get("ok"))

        # Verify DB items and files remain completely untouched
        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            move_row = con.execute("SELECT album_id FROM items WHERE id=20").fetchone()
            dup_row = con.execute("SELECT album_id FROM items WHERE id=21").fetchone()
            alb_row = con.execute("SELECT id FROM albums WHERE id=2").fetchone()

        self.assertEqual(move_row["album_id"], 2)
        self.assertEqual(dup_row["album_id"], 2)
        self.assertIsNotNone(alb_row)

        self.assertTrue(dup_path.exists())
        self.assertEqual(dup_path.stat().st_mtime_ns, mtime_dup_before)

    def test_plan_release_group_mismatch(self):
        self._create_album(1, "Existing Album", rg_id=RG_A)
        self._create_album(2, "Imported Temp Album", rg_id=RG_B)

        self._create_item(20, 2, "Track 1", 1, 1, "track1.flac", staging=True, rg_id=RG_B)

        res = create_existing_album_reconcile_plan(
            self.store,
            {
                "imported_album_id": 2,
                "existing_album_id": 1,
                "move_item_ids": [20],
                "source_folder": str(self.staging_root),
            },
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "reconcile_identity_mismatch")

    def test_plan_missing_survivor(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        dup_path = self._create_item(21, 2, "Track 1", 1, 1, "track1_imported.flac", staging=True)

        res = create_existing_album_reconcile_plan(
            self.store,
            {
                "imported_album_id": 2,
                "existing_album_id": 1,
                "dup_item_ids": [21],
                "dup_details": [{"dup_item_id": 21, "survivor_item_ids": [999]}],
                "source_folder": str(self.staging_root),
            },
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "reconcile_survivor_missing")

    def test_plan_symlink_rejected(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        target_file = self._create_item(20, 2, "Track 1", 1, 1, "real_track1.flac", staging=True)
        symlink_file = self.staging_root / "album_2" / "link_track1.flac"
        try:
            os.symlink(target_file, symlink_file)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks not supported on host environment")

        with sqlite3.connect(self.db_path) as con:
            con.execute("UPDATE items SET path=? WHERE id=20", (str(symlink_file),))
            con.commit()

        res = create_existing_album_reconcile_plan(
            self.store,
            {
                "imported_album_id": 2,
                "existing_album_id": 1,
                "move_item_ids": [20],
                "source_folder": str(self.staging_root),
            },
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "reconcile_symlink_rejected")

    def test_plan_hardlink_handling(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        surv_path = self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.flac")
        dup_path = self.staging_root / "album_2" / "track1_hardlink.flac"
        dup_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(surv_path, dup_path)
        except (OSError, NotImplementedError):
            self.skipTest("Hardlinks not supported on host environment")

        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT INTO items (id, album_id, title, artist, album, albumartist, disc, track, path, mb_trackid, mb_albumid, mb_releasegroupid, length) "
                "VALUES (21, 2, 'Track 1', 'Test Artist', 'Test Album', 'Test Artist', 1, 1, ?, ?, ?, ?, 180.0)",
                (str(dup_path), REC_1, REL_A, RG_A),
            )
            con.commit()

        res = create_existing_album_reconcile_plan(
            self.store,
            {
                "imported_album_id": 2,
                "existing_album_id": 1,
                "dup_item_ids": [21],
                "dup_details": [{"dup_item_id": 21, "survivor_item_ids": [10]}],
                "source_folder": str(self.staging_root),
            },
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
        )
        self.assertTrue(res.get("ok"), res)
        tx = self.store.get(res["operation_id"])
        dup_change = [c for c in tx["changes"] if c.get("item_id") == 21 or c.get("id") == "item:21"][0]
        self.assertTrue(dup_change.get("is_hardlink"))


class ApplyTests(Wave20FixtureBase):
    def test_apply_successful_reconciliation(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.flac")
        move_path = self._create_item(20, 2, "Track 2", 1, 2, "track2_imported.flac", staging=True)
        dup_path = self._create_item(21, 2, "Track 1", 1, 1, "track1_imported.flac", staging=True)

        plan_res = create_existing_album_reconcile_plan(
            self.store,
            {
                "imported_album_id": 2,
                "existing_album_id": 1,
                "move_item_ids": [20],
                "dup_item_ids": [21],
                "dup_details": [{"dup_item_id": 21, "survivor_item_ids": [10]}],
                "source_folder": str(self.staging_root),
            },
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
        )
        self.assertTrue(plan_res.get("ok"), plan_res)
        op_id = plan_res["operation_id"]

        apply_res = execute_existing_album_reconcile_apply(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(apply_res.get("ok"), apply_res)
        self.assertEqual(apply_res["status"], "Completed")

        # Verify DB state: move item reassigned to album 1, dup item deleted, empty album 2 deleted
        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            move_row = con.execute("SELECT album_id FROM items WHERE id=20").fetchone()
            dup_row = con.execute("SELECT COUNT(*) FROM items WHERE id=21").fetchone()[0]
            alb_row = con.execute("SELECT COUNT(*) FROM albums WHERE id=2").fetchone()[0]

        self.assertEqual(move_row["album_id"], 1)
        self.assertEqual(dup_row, 0)
        self.assertEqual(alb_row, 0)

        # Verify duplicate file unlinked
        self.assertFalse(dup_path.exists())

    def test_apply_stale_source_row(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.flac")
        self._create_item(20, 2, "Track 2", 1, 2, "track2_imported.flac", staging=True)

        plan_res = create_existing_album_reconcile_plan(
            self.store,
            {
                "imported_album_id": 2,
                "existing_album_id": 1,
                "move_item_ids": [20],
                "source_folder": str(self.staging_root),
            },
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
        )
        op_id = plan_res["operation_id"]

        # Mutate move item in DB after Plan
        with sqlite3.connect(self.db_path) as con:
            con.execute("UPDATE items SET album_id=99 WHERE id=20")
            con.commit()

        apply_res = execute_existing_album_reconcile_apply(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "reconcile_album_membership_changed")

    def test_apply_stale_survivor(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.flac")
        self._create_item(21, 2, "Track 1", 1, 1, "track1_imported.flac", staging=True)

        plan_res = create_existing_album_reconcile_plan(
            self.store,
            {
                "imported_album_id": 2,
                "existing_album_id": 1,
                "dup_item_ids": [21],
                "dup_details": [{"dup_item_id": 21, "survivor_item_ids": [10]}],
                "source_folder": str(self.staging_root),
            },
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
        )
        op_id = plan_res["operation_id"]

        # Delete survivor item row from DB after Plan
        with sqlite3.connect(self.db_path) as con:
            con.execute("DELETE FROM items WHERE id=10")
            con.commit()

        apply_res = execute_existing_album_reconcile_apply(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "reconcile_survivor_missing")

    def test_apply_mtime_changed(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.flac")
        move_path = self._create_item(20, 2, "Track 2", 1, 2, "track2_imported.flac", staging=True)

        plan_res = create_existing_album_reconcile_plan(
            self.store,
            {
                "imported_album_id": 2,
                "existing_album_id": 1,
                "move_item_ids": [20],
                "source_folder": str(self.staging_root),
            },
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
        )
        op_id = plan_res["operation_id"]

        # Modify file on disk to change mtime/size
        time.sleep(0.01)
        move_path.write_bytes(b"MODIFIED_AFTER_PLAN")

        apply_res = execute_existing_album_reconcile_apply(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "reconcile_toctou_mismatch")

    def test_apply_already_completed_replay(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.flac")
        self._create_item(20, 2, "Track 2", 1, 2, "track2_imported.flac", staging=True)

        plan_res = create_existing_album_reconcile_plan(
            self.store,
            {
                "imported_album_id": 2,
                "existing_album_id": 1,
                "move_item_ids": [20],
                "source_folder": str(self.staging_root),
            },
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
        )
        op_id = plan_res["operation_id"]

        apply1 = execute_existing_album_reconcile_apply(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(apply1.get("ok"))

        apply2 = execute_existing_album_reconcile_apply(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(apply2.get("ok"))
        self.assertTrue(apply2.get("already_completed"))


class ConcurrencyTests(Wave20FixtureBase):
    def test_concurrency_same_operation_twice(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.flac")
        self._create_item(20, 2, "Track 2", 1, 2, "track2_imported.flac", staging=True)

        plan_res = create_existing_album_reconcile_plan(
            self.store,
            {
                "imported_album_id": 2,
                "existing_album_id": 1,
                "move_item_ids": [20],
                "source_folder": str(self.staging_root),
            },
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
        )
        op_id = plan_res["operation_id"]

        results = []
        def _worker():
            r = execute_existing_album_reconcile_apply(
                self.store,
                op_id,
                db_path=str(self.db_path),
                music_allowed_roots=[str(self.music_root)],
            )
            results.append(r)

        t1 = threading.Thread(target=_worker)
        t2 = threading.Thread(target=_worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(results), 2)
        self.assertTrue(results[0].get("ok"))
        self.assertTrue(results[1].get("ok"))

    def test_concurrency_unrelated_albums(self):
        self._create_album(1, "Existing Album 1")
        self._create_album(2, "Imported Temp Album 1")
        self._create_album(3, "Existing Album 2")
        self._create_album(4, "Imported Temp Album 2")

        self._create_item(10, 1, "Track 1", 1, 1, "alb1_track1.flac")
        self._create_item(20, 2, "Track 2", 1, 2, "alb2_track2.flac", staging=True)
        self._create_item(30, 3, "Track 1", 1, 1, "alb3_track1.flac")
        self._create_item(40, 4, "Track 2", 1, 2, "alb4_track2.flac", staging=True)

        plan1 = create_existing_album_reconcile_plan(
            self.store,
            {
                "imported_album_id": 2,
                "existing_album_id": 1,
                "move_item_ids": [20],
                "source_folder": str(self.staging_root),
            },
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
        )
        plan2 = create_existing_album_reconcile_plan(
            self.store,
            {
                "imported_album_id": 4,
                "existing_album_id": 3,
                "move_item_ids": [40],
                "source_folder": str(self.staging_root),
            },
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
        )

        res1 = []
        res2 = []

        t1 = threading.Thread(
            target=lambda: res1.append(execute_existing_album_reconcile_apply(
                self.store, plan1["operation_id"], db_path=str(self.db_path), music_allowed_roots=[str(self.music_root)]
            ))
        )
        t2 = threading.Thread(
            target=lambda: res2.append(execute_existing_album_reconcile_apply(
                self.store, plan2["operation_id"], db_path=str(self.db_path), music_allowed_roots=[str(self.music_root)]
            ))
        )

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertTrue(res1[0].get("ok"), res1[0])
        self.assertTrue(res2[0].get("ok"), res2[0])


class RollbackTests(Wave20FixtureBase):
    def test_rollback_full_success(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.flac")
        self._create_item(20, 2, "Track 2", 1, 2, "track2_imported.flac", staging=True)
        dup_path = self._create_item(21, 2, "Track 1", 1, 1, "track1_imported.flac", outside=True)

        plan_res = create_existing_album_reconcile_plan(
            self.store,
            {
                "imported_album_id": 2,
                "existing_album_id": 1,
                "move_item_ids": [20],
                "dup_item_ids": [21],
                "dup_details": [{"dup_item_id": 21, "survivor_item_ids": [10]}],
                "source_folder": str(self.staging_root),
            },
            music_allowed_roots=[str(self.music_root), str(self.outside_root)],
            db_path=str(self.db_path),
        )
        self.assertTrue(plan_res.get("ok"), plan_res)
        op_id = plan_res["operation_id"]

        apply_res = execute_existing_album_reconcile_apply(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root), str(self.outside_root)],
        )
        self.assertTrue(apply_res.get("ok"), apply_res)

        rollback_res = rollback_existing_album_reconcile(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root), str(self.outside_root)],
        )
        self.assertTrue(rollback_res.get("ok"), rollback_res)
        self.assertEqual(rollback_res["status"], "Rolled Back")

        # Verify DB restored: move item album_id == 2, dup item row restored, album 2 row restored
        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            move_row = con.execute("SELECT album_id FROM items WHERE id=20").fetchone()
            dup_row = con.execute("SELECT album_id FROM items WHERE id=21").fetchone()
            alb_row = con.execute("SELECT id FROM albums WHERE id=2").fetchone()

        self.assertEqual(move_row["album_id"], 2)
        self.assertIsNotNone(dup_row)
        self.assertEqual(dup_row["album_id"], 2)
        self.assertIsNotNone(alb_row)

        # Verify quarantined duplicate file restored to original path
        self.assertTrue(dup_path.exists())


class Wave18InteractionTests(Wave20FixtureBase):
    def test_merge_job_routes_replace_rows_to_wave18_and_dup_move_to_wave20(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1 Old", 1, 1, "track1_existing.flac")
        self._create_item(20, 2, "Track 1 New", 1, 1, "track1_imported.flac", staging=True)
        self._create_item(21, 2, "Track 2 New", 1, 2, "track2_imported.flac", staging=True)

        plan_bulk_mock = mock.MagicMock(return_value={"ok": True, "operation_id": "op_bulk_18"})
        apply_bulk_mock = mock.MagicMock(return_value={"ok": True})
        plan_rec_mock = mock.MagicMock(return_value={"ok": True, "operation_id": "op_rec_20"})
        apply_rec_mock = mock.MagicMock(return_value={"ok": True})

        with mock.patch.object(app_module.beets_client, "plan_bulk_import_replacement", plan_bulk_mock), \
             mock.patch.object(app_module.beets_client, "apply_bulk_import_replacement", apply_bulk_mock), \
             mock.patch.object(app_module.beets_client, "plan_existing_album_reconcile", plan_rec_mock), \
             mock.patch.object(app_module.beets_client, "apply_existing_album_reconcile", apply_rec_mock):

            res = app_module._merge_imported_album_into_existing(
                2, 1, str(self.staging_root), [], mb_albumid=""
            )

        # Confirm reconcile transaction was invoked
        plan_rec_mock.assert_called_once()
        apply_rec_mock.assert_called_once_with("op_rec_20")


class RealProductionPathIntegrationTests(Wave20FixtureBase):
    def test_production_path_existing_album_reconciliation(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        # Item 10 in existing album 1 (disc 1, track 1)
        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.flac")
        # Item 20 in imported temp album 2 (disc 1, track 2 - missing track position)
        self._create_item(20, 2, "Track 2", 1, 2, "track2_imported.flac", staging=True)
        # Item 21 in imported temp album 2 (disc 1, track 1 - duplicate position)
        self._create_item(21, 2, "Track 1", 1, 1, "track1_imported.flac", staging=True)

        def _mock_plan(payload, **kwargs):
            return create_existing_album_reconcile_plan(
                self.store,
                payload,
                music_allowed_roots=[str(self.music_root)],
                db_path=str(self.db_path),
            )

        def _mock_apply(operation_id, **kwargs):
            return execute_existing_album_reconcile_apply(
                self.store,
                operation_id,
                db_path=str(self.db_path),
                music_allowed_roots=[str(self.music_root)],
            )

        # Mock _fetch_mb_release_tracklist and fingerprint check so _row_matches_target returns True for item 10 & 21
        mb_mock = mock.MagicMock(return_value={
            "ok": True,
            "tracks": [
                {"disc": 1, "track": 1, "mb_trackid": REC_1, "title": "Track 1"},
                {"disc": 1, "track": 2, "mb_trackid": REC_2, "title": "Track 2"},
            ],
        })

        with mock.patch.object(app_module.beets_client, "plan_existing_album_reconcile", side_effect=_mock_plan), \
             mock.patch.object(app_module.beets_client, "apply_existing_album_reconcile", side_effect=_mock_apply), \
             mock.patch.object(app_module, "_fetch_mb_release_tracklist", mb_mock), \
             mock.patch.object(app_module, "_guard_existing_track_can_block_downloaded_replacement", return_value=True):

            log = []
            res_aid = app_module._merge_imported_album_into_existing(
                2, 1, str(self.staging_root), log, mb_albumid=REL_A
            )
            self.assertEqual(res_aid, 1)

        # Confirm DB updated via complete production path
        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            move_row = con.execute("SELECT album_id FROM items WHERE id=20").fetchone()
            dup_cnt = con.execute("SELECT COUNT(*) FROM items WHERE id=21").fetchone()[0]
            alb_cnt = con.execute("SELECT COUNT(*) FROM albums WHERE id=2").fetchone()[0]

        self.assertEqual(move_row["album_id"], 1)
        self.assertEqual(dup_cnt, 0)
        self.assertEqual(alb_cnt, 0)


class WebManagerMutationProhibitionTests(unittest.TestCase):
    def test_merge_imported_album_into_existing_contains_no_direct_mutations(self):
        app_path = Path(app_module.__file__)
        source = app_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        merge_fn_def = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_merge_imported_album_into_existing":
                merge_fn_def = node
                break

        self.assertIsNotNone(merge_fn_def, "_merge_imported_album_into_existing not found in app.py")
        fn_source = ast.get_source_segment(source, merge_fn_def)

        # Verify no direct file unlink / rename / remove / move or SQL DELETE/UPDATE calls in _merge_imported_album_into_existing
        prohibited_strings = [
            "Path.unlink", "os.unlink", "os.remove", "os.rename", "os.replace",
            "shutil.move", "shutil.rmtree", "DELETE FROM items", "UPDATE items SET album_id",
            "DELETE FROM albums", "_beet_run",
        ]
        for p in prohibited_strings:
            self.assertNotIn(p, fn_source, f"Prohibited call '{p}' found in _merge_imported_album_into_existing")


if __name__ == "__main__":
    unittest.main()
