"""Tests for SEC-002 Wave 22 Album Maintenance & Artwork Controlled Mutation Boundary."""

import ast
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.transaction_engine import (
    TransactionStore,
    create_album_maintenance_plan,
    execute_album_maintenance_apply,
    rollback_album_maintenance,
    create_album_artwork_plan,
    execute_album_artwork_apply,
    rollback_album_artwork,
)


class Wave22AlbumMaintenanceTests(unittest.TestCase):
    """Test suite for Wave 22 album maintenance & artwork engine transaction boundaries."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.music_root = self.root / "music"
        self.music_root.mkdir(parents=True, exist_ok=True)
        self.db_path = str(self.root / "library.db")

        # Initialize SQLite DB schema for beets library
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE albums (
                id INTEGER PRIMARY KEY,
                albumartist TEXT,
                album TEXT,
                mb_albumid TEXT,
                mb_releasegroupid TEXT,
                year INTEGER,
                artpath BLOB
            )
        """)
        cur.execute("""
            CREATE TABLE items (
                id INTEGER PRIMARY KEY,
                album_id INTEGER,
                title TEXT,
                track INTEGER,
                disc INTEGER,
                path BLOB,
                mb_trackid TEXT
            )
        """)
        con.commit()
        con.close()

        self.store_dir = self.root / "transactions"
        self.store = TransactionStore(root=str(self.store_dir))
        self.quarantine_dir = self.root / "quarantine"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_plan_album_maintenance_preview_non_mutating(self):
        """Plan creation produces a Preview transaction without modifying files or DB."""
        album_dir = self.music_root / "Artist" / "Album"
        album_dir.mkdir(parents=True, exist_ok=True)
        file1 = album_dir / "track1.flac"
        file1.write_bytes(b"audio content 1")

        con = sqlite3.connect(self.db_path)
        con.execute("INSERT INTO albums (id, albumartist, album) VALUES (1, 'Artist', 'Album')")
        con.execute("INSERT INTO items (id, album_id, title, track, path) VALUES (10, 1, 'Track 1', 1, ?)", (str(file1).encode("utf-8"),))
        con.commit()
        con.close()

        payload = {
            "mode": "remove_tracks",
            "album_id": 1,
            "item_ids": [10],
            "delete_files": True,
            "clean_empty_folders": True,
        }
        res = create_album_maintenance_plan(
            self.store, payload,
            music_allowed_roots=[str(self.music_root)],
            db_path=self.db_path,
            quarantine_base_root=str(self.quarantine_dir),
        )

        self.assertTrue(res.get("ok"))
        op_id = res["operation_id"]
        tx = self.store.get(op_id)
        self.assertEqual(tx["status"], "Preview")
        self.assertEqual(tx["metadata"]["mutation_family"], "album_maintenance_v1")

        # File and DB row must remain untouched after Plan
        self.assertTrue(file1.exists())
        con = sqlite3.connect(self.db_path)
        row = con.execute("SELECT id FROM items WHERE id=10").fetchone()
        con.close()
        self.assertIsNotNone(row)

    def test_album_maintenance_apply_and_rollback_lifecycle(self):
        """Complete lifecycle: Plan -> Apply -> Rollback for track retirement."""
        album_dir = self.music_root / "Artist" / "Album"
        album_dir.mkdir(parents=True, exist_ok=True)
        file1 = album_dir / "track1.flac"
        file1.write_bytes(b"audio content track 1")

        con = sqlite3.connect(self.db_path)
        con.execute("INSERT INTO albums (id, albumartist, album) VALUES (2, 'Artist', 'Album')")
        con.execute("INSERT INTO items (id, album_id, title, track, path) VALUES (20, 2, 'Track 1', 1, ?)", (str(file1).encode("utf-8"),))
        con.commit()
        con.close()

        payload = {
            "mode": "remove_tracks",
            "album_id": 2,
            "item_ids": [20],
            "delete_files": True,
            "clean_empty_folders": False,
        }
        plan_res = create_album_maintenance_plan(
            self.store, payload,
            music_allowed_roots=[str(self.music_root)],
            db_path=self.db_path,
            quarantine_base_root=str(self.quarantine_dir),
        )
        op_id = plan_res["operation_id"]

        # Apply
        apply_res = execute_album_maintenance_apply(
            self.store, op_id,
            music_allowed_roots=[str(self.music_root)],
            db_path=self.db_path,
            quarantine_base_root=str(self.quarantine_dir),
        )
        self.assertTrue(apply_res.get("ok"), msg=apply_res.get("error"))
        self.assertTrue(apply_res.get("mutated"))
        self.assertFalse(file1.exists())  # Quarantined

        con = sqlite3.connect(self.db_path)
        item_row = con.execute("SELECT id FROM items WHERE id=20").fetchone()
        album_row = con.execute("SELECT id FROM albums WHERE id=2").fetchone()
        con.close()
        self.assertIsNone(item_row)
        self.assertIsNone(album_row)  # Album retired since 0 tracks remain

        # Rollback
        rb_res = rollback_album_maintenance(
            self.store, op_id,
            music_allowed_roots=[str(self.music_root)],
            db_path=self.db_path,
        )
        self.assertTrue(rb_res.get("ok"))
        self.assertEqual(rb_res.get("files_restored"), 1)
        self.assertTrue(file1.exists())  # Restored from quarantine

        con = sqlite3.connect(self.db_path)
        item_row = con.execute("SELECT id FROM items WHERE id=20").fetchone()
        album_row = con.execute("SELECT id FROM albums WHERE id=2").fetchone()
        con.close()
        self.assertIsNotNone(item_row)
        self.assertIsNotNone(album_row)

    def test_album_maintenance_toctou_revalidation(self):
        """TOCTOU stat change between Plan and Apply causes Apply to fail closed."""
        album_dir = self.music_root / "Artist" / "Album"
        album_dir.mkdir(parents=True, exist_ok=True)
        file1 = album_dir / "track1.flac"
        file1.write_bytes(b"initial audio content")

        con = sqlite3.connect(self.db_path)
        con.execute("INSERT INTO albums (id, albumartist, album) VALUES (3, 'Artist', 'Album')")
        con.execute("INSERT INTO items (id, album_id, title, track, path) VALUES (30, 3, 'Track 1', 1, ?)", (str(file1).encode("utf-8"),))
        con.commit()
        con.close()

        payload = {
            "mode": "remove_tracks",
            "album_id": 3,
            "item_ids": [30],
            "delete_files": True,
        }
        plan_res = create_album_maintenance_plan(
            self.store, payload,
            music_allowed_roots=[str(self.music_root)],
            db_path=self.db_path,
            quarantine_base_root=str(self.quarantine_dir),
        )
        op_id = plan_res["operation_id"]

        # Mutate file size to trigger TOCTOU stat failure
        file1.write_bytes(b"modified audio content with different length")

        apply_res = execute_album_maintenance_apply(
            self.store, op_id,
            music_allowed_roots=[str(self.music_root)],
            db_path=self.db_path,
            quarantine_base_root=str(self.quarantine_dir),
        )
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "album_maintenance_toctou_mismatch")

    def test_album_artwork_apply_and_rollback_lifecycle(self):
        """Artwork Plan -> Apply -> Rollback lifecycle."""
        album_dir = self.music_root / "Artist" / "Album"
        album_dir.mkdir(parents=True, exist_ok=True)
        art_file = album_dir / "cover.jpg"
        art_file.write_bytes(b"jpeg artwork data")

        con = sqlite3.connect(self.db_path)
        con.execute("INSERT INTO albums (id, albumartist, album, artpath) VALUES (4, 'Artist', 'Album', ?)", (str(art_file).encode("utf-8"),))
        con.commit()
        con.close()

        payload = {
            "mode": "quarantine",
            "album_id": 4,
            "candidates": [{"source": str(art_file)}],
        }
        plan_res = create_album_artwork_plan(
            self.store, payload,
            music_allowed_roots=[str(self.music_root)],
            db_path=self.db_path,
            quarantine_base_root=str(self.quarantine_dir),
        )
        op_id = plan_res["operation_id"]

        apply_res = execute_album_artwork_apply(
            self.store, op_id,
            music_allowed_roots=[str(self.music_root)],
            db_path=self.db_path,
            quarantine_base_root=str(self.quarantine_dir),
        )
        self.assertTrue(apply_res.get("ok"))
        self.assertFalse(art_file.exists())

        con = sqlite3.connect(self.db_path)
        row = con.execute("SELECT artpath FROM albums WHERE id=4").fetchone()
        con.close()
        self.assertIsNone(row[0])

        rb_res = rollback_album_artwork(
            self.store, op_id,
            music_allowed_roots=[str(self.music_root)],
            db_path=self.db_path,
        )
        self.assertTrue(rb_res.get("ok"))
        self.assertTrue(art_file.exists())

        con = sqlite3.connect(self.db_path)
        row = con.execute("SELECT artpath FROM albums WHERE id=4").fetchone()
        con.close()
        self.assertIsNotNone(row[0])

    def test_app_py_ast_no_direct_mutations_in_migrated_helpers(self):
        """AST check of app.py to verify no direct file unlinks or SQL deletes remain in target helpers."""
        app_path = Path(__file__).parent.parent / "app.py"
        self.assertTrue(app_path.exists())
        tree = ast.parse(app_path.read_text(encoding="utf-8"))

        target_func_names = {
            "_remove_album_track_items",
            "_album_art_quarantine_current",
            "_album_art_restore_quarantine",
        }

        class MutationVisitor(ast.NodeVisitor):
            def __init__(self):
                self.current_func = None
                self.violations = []

            def visit_FunctionDef(self, node):
                old_func = self.current_func
                if node.name in target_func_names:
                    self.current_func = node.name
                    self.generic_visit(node)
                    self.current_func = old_func
                else:
                    self.generic_visit(node)

            def visit_Call(self, node):
                if self.current_func:
                    # Check for Path.unlink, shutil.move, rmdir
                    if isinstance(node.func, ast.Attribute):
                        attr = node.func.attr
                        if attr in ("unlink", "rmdir"):
                            self.violations.append((self.current_func, f"Direct {attr} call"))
                        elif attr == "move" and isinstance(node.func.value, ast.Name) and node.func.value.id == "shutil":
                            self.violations.append((self.current_func, "Direct shutil.move call"))
                self.generic_visit(node)

        visitor = MutationVisitor()
        visitor.visit(tree)
        self.assertEqual(visitor.violations, [], f"Direct mutation violations found in app.py: {visitor.violations}")


if __name__ == "__main__":
    unittest.main()
