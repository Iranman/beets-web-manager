import ast
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.transaction_engine import (
    TransactionStore,
    create_bulk_import_replacement_plan,
    execute_bulk_import_replacement_apply,
    rollback_bulk_import_replacement,
)


class TestSEC002Wave18BulkImportReplacement(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="wave18_test_")
        self.music_root = os.path.join(self.tmp_dir, "music")
        self.staging_root = os.path.join(self.tmp_dir, "staging")
        self.quarantine_root = os.path.join(self.tmp_dir, "quarantine")
        self.db_path = os.path.join(self.tmp_dir, "musiclibrary.db")

        os.makedirs(self.music_root, exist_ok=True)
        os.makedirs(self.staging_root, exist_ok=True)
        os.makedirs(self.quarantine_root, exist_ok=True)

        self._init_db()
        self.store = TransactionStore(os.path.join(self.tmp_dir, "transactions.json"))

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _init_db(self):
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS albums (
                id INTEGER PRIMARY KEY,
                album TEXT,
                albumartist TEXT,
                mb_albumid TEXT,
                mb_releasegroupid TEXT,
                year INTEGER
            )
        """)
        cur.execute("""
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
            )
        """)

        # Insert 2 existing library items
        item1_path = os.path.join(self.music_root, "track01.flac")
        item2_path = os.path.join(self.music_root, "track02.flac")

        with open(item1_path, "wb") as f:
            f.write(b"FLAC_DATA_TRACK_1_ORIGINAL")
        with open(item2_path, "wb") as f:
            f.write(b"FLAC_DATA_TRACK_2_ORIGINAL")

        st1 = os.stat(item1_path)
        st2 = os.stat(item2_path)

        cur.execute(
            "INSERT INTO albums (id, album, albumartist, mb_albumid, mb_releasegroupid, year) VALUES (10, 'Album A', 'Artist A', 'mb_alb_1', 'rg_111', 2024)"
        )
        cur.execute(
            "INSERT INTO items (id, album_id, title, artist, album, albumartist, disc, track, path, mb_trackid, mb_albumid, mb_releasegroupid, length, size, mtime) "
            "VALUES (101, 10, 'Track 1', 'Artist A', 'Album A', 'Artist A', 1, 1, ?, 'tr_101', 'mb_alb_1', 'rg_111', 180.0, ?, ?)",
            (item1_path.encode("utf-8"), st1.st_size, st1.st_mtime),
        )
        cur.execute(
            "INSERT INTO items (id, album_id, title, artist, album, albumartist, disc, track, path, mb_trackid, mb_albumid, mb_releasegroupid, length, size, mtime) "
            "VALUES (102, 10, 'Track 2', 'Artist A', 'Album A', 'Artist A', 1, 2, ?, 'tr_102', 'mb_alb_1', 'rg_111', 200.0, ?, ?)",
            (item2_path.encode("utf-8"), st2.st_size, st2.st_mtime),
        )
        con.commit()
        con.close()

    def test_plan_creation_non_mutating(self):
        source_dir = os.path.join(self.staging_root, "new_album")
        os.makedirs(source_dir, exist_ok=True)
        with open(os.path.join(source_dir, "new01.flac"), "wb") as f:
            f.write(b"NEW_FLAC_1")

        payload = {
            "existing_album_id": 10,
            "old_item_ids": [101, 102],
            "source_folder": source_dir,
            "mb_releasegroupid": "rg_111",
            "reason": "Test plan creation",
        }

        res = create_bulk_import_replacement_plan(
            self.store,
            payload,
            db_path=self.db_path,
            original_allowed_roots=[self.music_root],
            candidate_allowed_roots=[self.staging_root],
            quarantine_base_root=self.quarantine_root,
        )

        self.assertTrue(res.get("ok"), res)
        op_id = res["operation_id"]
        tx = self.store.get(op_id)
        self.assertEqual(tx["status"], "Preview")
        self.assertEqual(tx["metadata"]["mutation_family"], "bulk_import_replacement_v1")

        # Verify plan creation is completely non-mutating
        self.assertTrue(os.path.exists(os.path.join(self.music_root, "track01.flac")))
        self.assertTrue(os.path.exists(os.path.join(self.music_root, "track02.flac")))

        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM items WHERE album_id = 10")
        count = cur.fetchone()[0]
        con.close()
        self.assertEqual(count, 2)

    def test_apply_lifecycle_and_quarantine(self):
        source_dir = os.path.join(self.staging_root, "new_album")
        os.makedirs(source_dir, exist_ok=True)
        with open(os.path.join(source_dir, "new01.flac"), "wb") as f:
            f.write(b"NEW_FLAC_1")

        plan_res = create_bulk_import_replacement_plan(
            self.store,
            {
                "existing_album_id": 10,
                "old_item_ids": [101, 102],
                "source_folder": source_dir,
                "mb_releasegroupid": "rg_111",
            },
            db_path=self.db_path,
            original_allowed_roots=[self.music_root],
            candidate_allowed_roots=[self.staging_root],
            quarantine_base_root=self.quarantine_root,
        )
        self.assertTrue(plan_res.get("ok"), plan_res)
        op_id = plan_res["operation_id"]

        apply_res = execute_bulk_import_replacement_apply(
            self.store,
            op_id,
            db_path=self.db_path,
            original_allowed_roots=[self.music_root],
            candidate_allowed_roots=[self.staging_root],
            quarantine_base_root=self.quarantine_root,
        )
        self.assertTrue(apply_res.get("ok"), apply_res)
        self.assertEqual(apply_res["status"], "Completed")
        self.assertEqual(apply_res["retired_count"], 2)

        # Original files moved from music root to quarantine
        self.assertFalse(os.path.exists(os.path.join(self.music_root, "track01.flac")))
        self.assertFalse(os.path.exists(os.path.join(self.music_root, "track02.flac")))

        # DB rows deleted
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM items WHERE id IN (101, 102)")
        count = cur.fetchone()[0]
        con.close()
        self.assertEqual(count, 0)

    def test_apply_idempotency(self):
        source_dir = os.path.join(self.staging_root, "new_album")
        os.makedirs(source_dir, exist_ok=True)
        with open(os.path.join(source_dir, "new01.flac"), "wb") as f:
            f.write(b"NEW_FLAC_1")

        plan_res = create_bulk_import_replacement_plan(
            self.store,
            {
                "existing_album_id": 10,
                "old_item_ids": [101, 102],
                "source_folder": source_dir,
            },
            db_path=self.db_path,
            original_allowed_roots=[self.music_root],
            candidate_allowed_roots=[self.staging_root],
            quarantine_base_root=self.quarantine_root,
        )
        op_id = plan_res["operation_id"]

        first_res = execute_bulk_import_replacement_apply(
            self.store,
            op_id,
            db_path=self.db_path,
            original_allowed_roots=[self.music_root],
            candidate_allowed_roots=[self.staging_root],
            quarantine_base_root=self.quarantine_root,
        )
        self.assertTrue(first_res.get("ok"))

        second_res = execute_bulk_import_replacement_apply(
            self.store,
            op_id,
            db_path=self.db_path,
            original_allowed_roots=[self.music_root],
            candidate_allowed_roots=[self.staging_root],
            quarantine_base_root=self.quarantine_root,
        )
        self.assertTrue(second_res.get("ok"))
        self.assertTrue(second_res.get("already_completed"))
        self.assertEqual(second_res["status"], "Completed")

    def test_toctou_stat_mismatch_refusal(self):
        source_dir = os.path.join(self.staging_root, "new_album")
        os.makedirs(source_dir, exist_ok=True)
        with open(os.path.join(source_dir, "new01.flac"), "wb") as f:
            f.write(b"NEW_FLAC_1")

        plan_res = create_bulk_import_replacement_plan(
            self.store,
            {
                "existing_album_id": 10,
                "old_item_ids": [101, 102],
                "source_folder": source_dir,
            },
            db_path=self.db_path,
            original_allowed_roots=[self.music_root],
            candidate_allowed_roots=[self.staging_root],
            quarantine_base_root=self.quarantine_root,
        )
        op_id = plan_res["operation_id"]

        # Mutate track01.flac size on disk to trigger TOCTOU failure
        item1_path = os.path.join(self.music_root, "track01.flac")
        with open(item1_path, "ab") as f:
            f.write(b"_APPENDED_CORRUPTED_BYTES")

        apply_res = execute_bulk_import_replacement_apply(
            self.store,
            op_id,
            db_path=self.db_path,
            original_allowed_roots=[self.music_root],
            candidate_allowed_roots=[self.staging_root],
            quarantine_base_root=self.quarantine_root,
        )
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "toctou_mismatch")

    def test_release_group_mismatch_refusal(self):
        source_dir = os.path.join(self.staging_root, "new_album")
        os.makedirs(source_dir, exist_ok=True)
        with open(os.path.join(source_dir, "new01.flac"), "wb") as f:
            f.write(b"NEW_FLAC_1")

        plan_res = create_bulk_import_replacement_plan(
            self.store,
            {
                "existing_album_id": 10,
                "old_item_ids": [101, 102],
                "source_folder": source_dir,
                "mb_releasegroupid": "rg_DIFFERENT_ALBUM_999",
            },
            db_path=self.db_path,
            original_allowed_roots=[self.music_root],
            candidate_allowed_roots=[self.staging_root],
            quarantine_base_root=self.quarantine_root,
        )
        self.assertFalse(plan_res.get("ok"))
        self.assertEqual(plan_res.get("code"), "release_group_mismatch")

    def test_rollback_restores_files_and_rows(self):
        source_dir = os.path.join(self.staging_root, "new_album")
        os.makedirs(source_dir, exist_ok=True)
        with open(os.path.join(source_dir, "new01.flac"), "wb") as f:
            f.write(b"NEW_FLAC_1")

        plan_res = create_bulk_import_replacement_plan(
            self.store,
            {
                "existing_album_id": 10,
                "old_item_ids": [101, 102],
                "source_folder": source_dir,
            },
            db_path=self.db_path,
            original_allowed_roots=[self.music_root],
            candidate_allowed_roots=[self.staging_root],
            quarantine_base_root=self.quarantine_root,
        )
        op_id = plan_res["operation_id"]

        apply_res = execute_bulk_import_replacement_apply(
            self.store,
            op_id,
            db_path=self.db_path,
            original_allowed_roots=[self.music_root],
            candidate_allowed_roots=[self.staging_root],
            quarantine_base_root=self.quarantine_root,
        )
        self.assertTrue(apply_res.get("ok"))

        rollback_res = rollback_bulk_import_replacement(
            self.store,
            op_id,
            db_path=self.db_path,
            original_allowed_roots=[self.music_root],
        )
        self.assertTrue(rollback_res.get("ok"), rollback_res)
        self.assertEqual(rollback_res["status"], "Rolled Back")
        self.assertEqual(rollback_res["restored_files_count"], 2)
        self.assertEqual(rollback_res["db_restored_count"], 2)

        # Files restored
        self.assertTrue(os.path.exists(os.path.join(self.music_root, "track01.flac")))
        self.assertTrue(os.path.exists(os.path.join(self.music_root, "track02.flac")))

        # DB rows restored
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM items WHERE id IN (101, 102)")
        count = cur.fetchone()[0]
        con.close()
        self.assertEqual(count, 2)

    def test_app_py_ast_no_direct_mutations(self):
        with open("app.py", "r", encoding="utf-8") as f:
            source = f.read()

        tree = ast.parse(source, filename="app.py")

        class RefactoringVisitor(ast.NodeVisitor):
            def __init__(self):
                self.in_merge_func = False
                self.direct_unlinks = 0
                self.direct_deletes = 0

            def visit_FunctionDef(self, node):
                if node.name == "_merge_imported_album_into_existing":
                    old_state = self.in_merge_func
                    self.in_merge_func = True
                    self.generic_visit(node)
                    self.in_merge_func = old_state
                else:
                    self.generic_visit(node)

            def visit_Attribute(self, node):
                if self.in_merge_func and node.attr == "unlink":
                    self.direct_unlinks += 1
                self.generic_visit(node)

            def visit_Constant(self, node):
                if self.in_merge_func and isinstance(node.value, str):
                    if "DELETE FROM items" in node.value:
                        self.direct_deletes += 1
                self.generic_visit(node)

        visitor = RefactoringVisitor()
        visitor.visit(tree)

        self.assertEqual(visitor.direct_unlinks, 0, "Found direct unlink() call in _merge_imported_album_into_existing")
        self.assertEqual(visitor.direct_deletes, 0, "Found direct DELETE FROM items query in _merge_imported_album_into_existing")


if __name__ == "__main__":
    unittest.main()
