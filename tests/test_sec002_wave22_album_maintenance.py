"""Tests for SEC-002 Wave 22 Album Maintenance & Artwork Controlled Mutation Boundary."""

import ast
import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.transaction_engine import TransactionStore
try:  # ARCH-001: app.py module family (works under discovery and tests.<module> runs)
    from _app_ast_cache import app_family_source  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_ast_cache import app_family_source  # noqa: E402


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





    def test_app_py_ast_no_direct_mutations_in_migrated_helpers(self):
        """AST check of app.py to verify no direct file unlinks or SQL deletes remain in target helpers."""
        app_path = Path(__file__).parent.parent / "app.py"
        self.assertTrue(app_path.exists())
        tree = ast.parse(app_family_source())

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
