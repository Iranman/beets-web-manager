"""Comprehensive Unit & Behavioral Tests for Beets Transaction Engine Families (SEC-002 Wave 22).

Covers all transaction families:
- folder_cleanup_v1
- playlist_media_cleanup_v1
- album_maintenance_v1 (deduplicate, remove_tracks)
- album_artwork_v1
- import_folder_v1
"""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend import transaction_engine


class TestBeetsTransactionEngineFamilies(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name).resolve()
        self.music_dir = self.root / "music"
        self.music_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir = self.root / "staging"
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.quarantine_dir = self.root / "quarantine"
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = self.root / "beets.db"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB, album_id INT, title TEXT, artist TEXT, album TEXT)")
            conn.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, album TEXT, albumartist TEXT, mb_albumid TEXT, mb_releasegroupid TEXT, artpath BLOB)")
            conn.execute("CREATE TABLE item_attributes (id INTEGER PRIMARY KEY, entity_id INT, key TEXT, value TEXT)")

        self.store_path = self.root / "transactions.db"
        self.store = transaction_engine.TransactionStore(str(self.store_path))

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        try:
            self.tmpdir.cleanup()
        except Exception:
            pass

    # ── 1. folder_cleanup_v1 ──────────────────────────────────────────────────

    def test_folder_cleanup_plan_nonmutation(self):
        src = self.music_dir / "old_album"
        dst = self.music_dir / "new_album"
        src.mkdir()
        (src / "track1.mp3").write_bytes(b"audio content")

        plan = transaction_engine.create_folder_cleanup_plan(
            self.store,
            {"action": "safe_rename", "source": str(src), "target": str(dst)},
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path)
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        self.assertTrue(src.exists())
        self.assertFalse(dst.exists())

    def test_folder_cleanup_apply_success_and_idempotency(self):
        src = self.music_dir / "old_album"
        dst = self.music_dir / "new_album"
        src.mkdir()
        (src / "track1.mp3").write_bytes(b"audio content")

        plan = transaction_engine.create_folder_cleanup_plan(
            self.store,
            {"action": "safe_rename", "source": str(src), "target": str(dst)},
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path)
        )
        op_id = plan["operation_id"]

        apply1 = transaction_engine.execute_folder_cleanup_apply(
            self.store, op_id, music_allowed_roots=[str(self.music_dir)], db_path=str(self.db_path)
        )
        self.assertTrue(apply1.get("ok"))
        self.assertTrue(dst.exists())
        self.assertFalse(src.exists())

        # Replay / Idempotency check: second apply returns success without duplicate mutation
        apply2 = transaction_engine.execute_folder_cleanup_apply(
            self.store, op_id, music_allowed_roots=[str(self.music_dir)], db_path=str(self.db_path)
        )
        self.assertTrue(apply2.get("ok"))
        self.assertTrue(apply2.get("idempotent", True))

    def test_folder_cleanup_allowed_roots_enforcement(self):
        outside = self.root / "outside_dir"
        outside.mkdir()
        dst = self.music_dir / "new_album"

        plan = transaction_engine.create_folder_cleanup_plan(
            self.store,
            {"action": "safe_rename", "source": str(outside), "target": str(dst)},
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path)
        )
        self.assertFalse(plan.get("ok"))
        self.assertIn("allowed root", plan.get("error", "").lower())

    def test_folder_cleanup_rollback(self):
        src = self.root / "old_album"
        dst = self.root / "new_album"
        src.mkdir()
        (src / "track1.mp3").write_bytes(b"audio content")

        plan = transaction_engine.create_folder_cleanup_plan(
            self.store,
            {"action": "safe_rename", "source": str(src), "target": str(dst)},
            music_allowed_roots=[str(self.root)],
            db_path=str(self.db_path)
        )
        op_id = plan["operation_id"]
        transaction_engine.execute_folder_cleanup_apply(self.store, op_id, music_allowed_roots=[str(self.root)])

        rollback = transaction_engine.rollback_folder_cleanup(self.store, op_id, music_allowed_roots=[str(self.root)])
        self.assertTrue(rollback.get("ok"))
        self.assertTrue(src.exists())
        self.assertFalse(dst.exists())

    # ── 2. playlist_media_cleanup_v1 ──────────────────────────────────────────

    def test_playlist_media_cleanup_plan_and_apply(self):
        file1 = self.music_dir / "song1.mp3"
        file1.write_bytes(b"song content")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO items (id, path) VALUES (10, ?)", (str(file1).encode("utf-8"),))

        plan = transaction_engine.create_playlist_media_cleanup_plan(
            self.store,
            {"item_ids": [10]},
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path)
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        op_id = plan["operation_id"]

        apply = transaction_engine.execute_playlist_media_cleanup_apply(
            self.store, op_id, music_allowed_roots=[str(self.music_dir)], db_path=str(self.db_path)
        )
        self.assertTrue(apply.get("ok"))
        self.assertFalse(file1.exists())

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT id FROM items WHERE id=10").fetchone()
            self.assertIsNone(row)

    # ── 3. album_maintenance_v1 (deduplicate, remove_tracks) ──

    def test_album_maintenance_remove_tracks_mode(self):
        file1 = self.music_dir / "track1.mp3"
        file1.write_bytes(b"track 1 content")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO albums (id, album) VALUES (1, 'Test Album')")
            conn.execute("INSERT INTO items (id, path, album_id) VALUES (100, ?, 1)", (str(file1).encode("utf-8"),))

        plan = transaction_engine.create_album_maintenance_plan(
            self.store,
            {
                "mode": "remove_tracks",
                "album_id": 1,
                "item_ids": [100],
                "delete_files": True
            },
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path)
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        op_id = plan["operation_id"]

        apply = transaction_engine.execute_album_maintenance_apply(
            self.store, op_id, music_allowed_roots=[str(self.music_dir)], db_path=str(self.db_path)
        )
        self.assertTrue(apply.get("ok"))
        self.assertFalse(file1.exists())

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT id FROM items WHERE id=100").fetchone()
            self.assertIsNone(row)

    def test_album_maintenance_deduplicate_mode(self):
        file1 = self.music_dir / "dup_track.mp3"
        file1.write_bytes(b"audio stream")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO albums (id, album) VALUES (5, 'Album')")
            conn.execute("INSERT INTO items (id, path, album_id) VALUES (200, ?, 5)", (str(file1).encode("utf-8"),))

        plan = transaction_engine.create_album_maintenance_plan(
            self.store,
            {
                "mode": "deduplicate",
                "album_id": 5,
                "to_delete": [{"id": 200, "path": str(file1)}]
            },
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path)
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        op_id = plan["operation_id"]

        apply = transaction_engine.execute_album_maintenance_apply(
            self.store, op_id, music_allowed_roots=[str(self.music_dir)], db_path=str(self.db_path)
        )
        self.assertTrue(apply.get("ok"))
        self.assertFalse(file1.exists())

    # ── 4. album_artwork_v1 ───────────────────────────────────────────────────

    def test_album_artwork_plan(self):
        cover = self.staging_dir / "cover.jpg"
        cover.write_bytes(b"JPEG artwork binary data")
        target_dir = self.music_dir / "Artist" / "Album"
        target_dir.mkdir(parents=True)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO albums (id, album) VALUES (10, 'Album')")
            conn.execute("INSERT INTO items (id, path, album_id) VALUES (50, ?, 10)", (str(target_dir / "01.mp3").encode("utf-8"),))

        plan = transaction_engine.create_album_artwork_plan(
            self.store,
            {
                "mode": "move",
                "album_id": 10,
                "candidates": [{"source": str(cover)}]
            },
            music_allowed_roots=[str(self.root)],
            db_path=str(self.db_path)
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))

    # ── 5. import_folder_v1 ───────────────────────────────────────────────────

    def test_import_folder_plan_and_apply(self):
        import_src = self.staging_dir / "Artist - Album (2024)"
        import_src.mkdir()
        (import_src / "01 - Track.mp3").write_bytes(b"mp3 content")

        plan = transaction_engine.create_import_folder_plan(
            self.store,
            {
                "source_folder": str(import_src),
                "mb_albumid": "11111111-1111-1111-1111-111111111111"
            },
            music_allowed_roots=[str(self.root)],
            staging_allowed_roots=[str(self.root)],
            db_path=str(self.db_path)
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        op_id = plan["operation_id"]

        apply = transaction_engine.execute_import_folder_apply(
            self.store, op_id, music_allowed_roots=[str(self.root)], db_path=str(self.db_path)
        )
        self.assertTrue(apply.get("ok"))


if __name__ == "__main__":
    unittest.main()
