"""Tests for backend.beets_startup_guard -- the fail-closed preflight that
blocks the exact production incident this repository hit: a missing/empty
Beets library silently becoming "production" instead of blocking startup.

All databases here are temporary and disposable; nothing touches any real
Beets library or TrueNAS path.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.beets_startup_guard import check_library  # noqa: E402


def _make_valid_db(path: str, items: int = 0, albums: int = 0) -> None:
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, path BLOB)")
    cur.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, albumartist TEXT)")
    for i in range(albums):
        cur.execute("INSERT INTO albums (albumartist) VALUES (?)", (f"Artist {i}",))
    for i in range(items):
        cur.execute("INSERT INTO items (album_id, path) VALUES (?, ?)", (i % max(albums, 1), f"/x/{i}".encode()))
    con.commit()
    con.close()


class BeetsStartupGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.approved_root = self.tmpdir.name
        self.db_path = os.path.join(self.tmpdir.name, "musiclibrary.blb")

    # 1. Existing valid populated database passes.
    def test_valid_populated_database_passes(self):
        _make_valid_db(self.db_path, items=3144, albums=413)
        result = check_library(self.db_path, expect_existing=True, approved_root=self.approved_root)
        self.assertTrue(result.ok)
        self.assertEqual(result.items, 3144)
        self.assertEqual(result.albums, 413)

    # 2. Zero-byte database fails.
    def test_zero_byte_database_fails_in_existing_mode(self):
        Path(self.db_path).write_bytes(b"")
        result = check_library(self.db_path, expect_existing=True, approved_root=self.approved_root)
        self.assertFalse(result.ok)
        self.assertIn("zero_bytes", result.reason)

    # 3. Valid schema with zero items fails when existing-library mode is enabled.
    def test_valid_schema_zero_items_fails_in_existing_mode(self):
        _make_valid_db(self.db_path, items=0, albums=0)
        result = check_library(self.db_path, expect_existing=True, approved_root=self.approved_root)
        self.assertFalse(result.ok)
        self.assertIn("zero_items_and_albums", result.reason)

    # 4. Empty database is allowed only under explicit first-run mode.
    def test_missing_database_allowed_under_first_run_mode(self):
        result = check_library(self.db_path, expect_existing=False, approved_root=self.approved_root)
        self.assertTrue(result.ok)
        self.assertTrue(result.first_run)

    # 5. Missing database fails in existing-library mode.
    def test_missing_database_fails_in_existing_mode(self):
        result = check_library(self.db_path, expect_existing=True, approved_root=self.approved_root)
        self.assertFalse(result.ok)
        self.assertIn("library_missing", result.reason)

    # 6. Corrupt database fails.
    def test_corrupt_database_fails(self):
        Path(self.db_path).write_bytes(b"this is not a sqlite database at all, just garbage bytes")
        result = check_library(self.db_path, expect_existing=True, approved_root=self.approved_root)
        self.assertFalse(result.ok)

    # 7. Missing required tables fails.
    def test_missing_required_tables_fails(self):
        con = sqlite3.connect(self.db_path)
        con.execute("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")
        con.commit()
        con.close()
        result = check_library(self.db_path, expect_existing=True, approved_root=self.approved_root)
        self.assertFalse(result.ok)
        self.assertIn("missing_required_tables", result.reason)

    # 8. Symlink outside approved root fails.
    def test_symlink_outside_approved_root_fails(self):
        outside_dir = tempfile.TemporaryDirectory()
        self.addCleanup(outside_dir.cleanup)
        real_db = os.path.join(outside_dir.name, "real.blb")
        _make_valid_db(real_db, items=5, albums=1)
        link_path = os.path.join(self.tmpdir.name, "musiclibrary_link.blb")
        try:
            os.symlink(real_db, link_path)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported in this environment")
        result = check_library(link_path, expect_existing=True, approved_root=self.approved_root)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "symlink_escapes_approved_root")

    # 9. Item count below configured threshold fails.
    def test_item_count_below_threshold_fails(self):
        _make_valid_db(self.db_path, items=2, albums=1)
        result = check_library(
            self.db_path, expect_existing=True, min_items=10, min_albums=1, approved_root=self.approved_root
        )
        self.assertFalse(result.ok)
        self.assertIn("items_below_minimum_threshold", result.reason)

    # 10. Album count below configured threshold fails.
    def test_album_count_below_threshold_fails(self):
        _make_valid_db(self.db_path, items=100, albums=2)
        result = check_library(
            self.db_path, expect_existing=True, min_items=1, min_albums=10, approved_root=self.approved_root
        )
        self.assertFalse(result.ok)
        self.assertIn("albums_below_minimum_threshold", result.reason)

    # Extra: symlink INSIDE the approved root is fine.
    def test_symlink_inside_approved_root_passes(self):
        real_db = os.path.join(self.tmpdir.name, "real.blb")
        _make_valid_db(real_db, items=5, albums=1)
        link_path = os.path.join(self.tmpdir.name, "musiclibrary_link.blb")
        try:
            os.symlink(real_db, link_path)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported in this environment")
        result = check_library(link_path, expect_existing=True, approved_root=self.approved_root)
        self.assertTrue(result.ok)

    # Extra: a directory at the configured path is rejected, not silently accepted.
    def test_directory_at_library_path_fails(self):
        os.makedirs(self.db_path)
        result = check_library(self.db_path, expect_existing=True, approved_root=self.approved_root)
        self.assertFalse(result.ok)
        self.assertIn("not_a_regular_file", result.reason)

    # Extra: log_lines never includes anything resembling a secret/token.
    def test_log_lines_contain_no_secret_looking_content(self):
        _make_valid_db(self.db_path, items=5, albums=1)
        result = check_library(self.db_path, expect_existing=True, approved_root=self.approved_root)
        joined = "\n".join(result.log_lines())
        for forbidden in ("token", "password", "secret", "api_key"):
            self.assertNotIn(forbidden, joined.lower())


if __name__ == "__main__":
    unittest.main()
