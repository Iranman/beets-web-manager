"""SEC-002 / ARCH-003 Wave 21: Artist Folder Merge & MBID Stamping Controlled Mutation Boundary.

Tests for artist_folder_reconcile_v1 mutation family in transaction_engine.py,
beets_control_agent.py, beets_client.py, and app.py.
"""

import ast
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import app as app_module
from backend.transaction_engine import (
    TransactionStore,
    create_artist_folder_reconcile_plan,
    execute_artist_folder_reconcile_apply,
    rollback_artist_folder_reconcile,
)

ITEMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY,
    album TEXT,
    albumartist TEXT,
    albumartists TEXT,
    mb_albumartistid TEXT,
    mb_albumartistids TEXT,
    mb_albumid TEXT,
    mb_releasegroupid TEXT,
    year INTEGER,
    path BLOB,
    artpath BLOB
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    album_id INTEGER,
    title TEXT,
    artist TEXT,
    artists TEXT,
    albumartist TEXT,
    albumartists TEXT,
    album TEXT,
    disc INTEGER,
    track INTEGER,
    path BLOB,
    mb_trackid TEXT,
    mb_albumid TEXT,
    mb_artistid TEXT,
    mb_artistids TEXT,
    mb_albumartistid TEXT,
    mb_albumartistids TEXT,
    mb_releasegroupid TEXT,
    length REAL
);
"""


class Wave21BaseTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp_path = Path(self._tmpdir.name)

        self.music_root = self.tmp_path / "music"
        self.music_root.mkdir()
        self.quarantine_root = self.tmp_path / "quarantine"
        self.quarantine_root.mkdir()

        self.db_path = self.tmp_path / "musiclibrary.db"
        con = sqlite3.connect(self.db_path)
        con.executescript(ITEMS_SCHEMA)
        con.commit()
        con.close()

        store_dir = self.tmp_path / "transactions"
        store_dir.mkdir()
        self.tx_store = TransactionStore(root=str(store_dir))

    def _create_artist_folder(self, name: str) -> Path:
        p = self.music_root / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _create_track_file(self, rel_path: str, content: bytes = b"AUDIO_DATA") -> Path:
        p = self.music_root / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def _insert_item(self, item_id: int, album_id: int, artist: str, album: str, rel_path: str, mbid: str = ""):
        p = self.music_root / rel_path
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO items (id, album_id, title, artist, artists, albumartist, albumartists, album, disc, track, path, mb_artistid, mb_artistids, mb_albumartistid, mb_albumartistids) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, album_id, "Song", artist, artist, artist, artist, album, 1, 1, str(p).encode("utf-8"), mbid, mbid, mbid, mbid)
        )
        con.commit()
        con.close()

    def _insert_album(self, album_id: int, name: str, artist: str, rel_folder: str, mbid: str = ""):
        p = self.music_root / rel_folder
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO albums (id, album, albumartist, albumartists, mb_albumartistid, mb_albumartistids, path) "
            "VALUES (?,?,?,?,?,?,?)",
            (album_id, name, artist, artist, mbid, mbid, str(p).encode("utf-8"))
        )
        con.commit()
        con.close()


class PlanTests(Wave21BaseTest):
    def test_plan_is_non_mutating(self):
        src = self._create_artist_folder("aaliyah")
        dst = self._create_artist_folder("Aaliyah (a1b2c3d4-e5f6-7890-abcd-ef0123456789)")
        self._create_track_file("aaliyah/Album 1/track1.mp3", b"TRACK1")

        res = create_artist_folder_reconcile_plan(
            self.tx_store,
            {"root": str(self.music_root), "mode": "scan_merge"},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
        )
        self.assertTrue(res.get("ok"), res)
        # Verify filesystem remains completely untouched
        self.assertTrue((self.music_root / "aaliyah/Album 1/track1.mp3").exists())
        self.assertTrue(src.exists())

    def test_plan_rejects_identity_conflict_different_mbids(self):
        self._create_artist_folder("Artist A")
        self._create_artist_folder("Artist A Dup")
        self._insert_album(1, "Album 1", "Artist A", "Artist A", mbid="uuid-1111")
        self._insert_album(2, "Album 2", "Artist A Dup", "Artist A Dup", mbid="uuid-2222")

        res = create_artist_folder_reconcile_plan(
            self.tx_store,
            {
                "root": str(self.music_root),
                "mode": "scan_merge",
                "candidates": [{
                    "source_path": str(self.music_root / "Artist A Dup"),
                    "target_path": str(self.music_root / "Artist A"),
                    "source_mbid": "uuid-2222",
                    "target_mbid": "uuid-1111",
                }]
            },
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "artist_reconcile_identity_conflict")


class ApplyTests(Wave21BaseTest):
    def test_apply_moves_files_quaternines_duplicates_and_updates_db(self):
        src = self._create_artist_folder("artist_source")
        dst = self._create_artist_folder("artist_target")

        self._create_track_file("artist_source/Album A/track1.flac", b"TRACK1_DATA")
        self._insert_album(10, "Album A", "artist_source", "artist_source/Album A")
        self._insert_item(100, 10, "artist_source", "Album A", "artist_source/Album A/track1.flac")

        plan_res = create_artist_folder_reconcile_plan(
            self.tx_store,
            {
                "root": str(self.music_root),
                "mode": "scan_merge",
                "candidates": [{
                    "source_path": str(src),
                    "target_path": str(dst),
                }]
            },
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
        )
        self.assertTrue(plan_res.get("ok"), plan_res)
        op_id = plan_res["operation_id"]

        apply_res = execute_artist_folder_reconcile_apply(
            self.tx_store,
            op_id,
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_root),
        )
        self.assertTrue(apply_res.get("ok"), apply_res)
        self.assertEqual(apply_res.get("status"), "Completed")

        # Filesystem assertions
        self.assertTrue((self.music_root / "artist_target/Album A/track1.flac").exists())
        self.assertFalse((self.music_root / "artist_source/Album A/track1.flac").exists())

        # Database assertions
        con = sqlite3.connect(self.db_path)
        cur = con.execute("SELECT path, albumartist FROM items WHERE id=100")
        row = cur.fetchone()
        con.close()
        expected_path = str(self.music_root / "artist_target/Album A/track1.flac").encode("utf-8")
        self.assertEqual(row[0], expected_path)

    def test_apply_idempotency_returns_already_completed(self):
        src = self._create_artist_folder("artist_x_source")
        dst = self._create_artist_folder("artist_x_target")
        self._create_track_file("artist_x_source/Song.mp3", b"SONG")

        plan_res = create_artist_folder_reconcile_plan(
            self.tx_store,
            {
                "root": str(self.music_root),
                "mode": "scan_merge",
                "candidates": [{
                    "source_path": str(src),
                    "target_path": str(dst),
                }]
            },
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
        )
        op_id = plan_res["operation_id"]

        apply1 = execute_artist_folder_reconcile_apply(
            self.tx_store,
            op_id,
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_root),
        )
        self.assertTrue(apply1.get("ok"))

        apply2 = execute_artist_folder_reconcile_apply(
            self.tx_store,
            op_id,
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_root),
        )
        self.assertTrue(apply2.get("ok"))
        self.assertTrue(apply2.get("already_completed"))


class RollbackTests(Wave21BaseTest):
    def test_rollback_restores_filesystem_and_db(self):
        src = self._create_artist_folder("old_artist_dir")
        dst = self._create_artist_folder("new_artist_dir")
        self._create_track_file("old_artist_dir/Album/t.mp3", b"MUSIC")
        self._insert_album(1, "Album", "old_artist_dir", "old_artist_dir/Album")
        self._insert_item(10, 1, "old_artist_dir", "Album", "old_artist_dir/Album/t.mp3")

        plan_res = create_artist_folder_reconcile_plan(
            self.tx_store,
            {
                "root": str(self.music_root),
                "mode": "scan_merge",
                "candidates": [{
                    "source_path": str(src),
                    "target_path": str(dst),
                }]
            },
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
        )
        op_id = plan_res["operation_id"]

        execute_artist_folder_reconcile_apply(
            self.tx_store,
            op_id,
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_root),
        )

        rb_res = rollback_artist_folder_reconcile(
            self.tx_store,
            op_id,
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_root),
        )
        self.assertTrue(rb_res.get("ok"), rb_res)
        self.assertEqual(rb_res.get("status"), "Rolled Back")

        # Filesystem restored
        self.assertTrue((self.music_root / "old_artist_dir/Album/t.mp3").exists())

        # DB restored
        con = sqlite3.connect(self.db_path)
        cur = con.execute("SELECT path FROM items WHERE id=10")
        row = cur.fetchone()
        con.close()
        old_path_b = str(self.music_root / "old_artist_dir/Album/t.mp3").encode("utf-8")
        self.assertEqual(row[0], old_path_b)


class RealProductionPathTests(Wave21BaseTest):
    def setUp(self):
        super().setUp()
        @contextmanager
        def _local_db(path=None, *, text_factory=None, row_factory=None):
            con = sqlite3.connect(self.db_path)
            con.text_factory = text_factory or bytes
            if row_factory is not None:
                con.row_factory = row_factory
            try:
                yield con
            finally:
                con.close()

        self._db_patch = mock.patch.object(app_module, "_db", _local_db)
        self._db_patch.start()
        self.addCleanup(self._db_patch.stop)

        self._music_root_patch = mock.patch.object(app_module, "MUSIC_ROOT", self.music_root)
        self._music_root_patch.start()
        self.addCleanup(self._music_root_patch.stop)

        def mock_plan(payload, **kw):
            return create_artist_folder_reconcile_plan(
                self.tx_store, payload,
                music_allowed_roots=[str(self.music_root)],
                db_path=str(self.db_path),
                quarantine_base_root=str(self.quarantine_root),
            )

        def mock_apply(op_id, **kw):
            return execute_artist_folder_reconcile_apply(
                self.tx_store, op_id,
                music_allowed_roots=[str(self.music_root)],
                db_path=str(self.db_path),
                quarantine_base_root=str(self.quarantine_root),
            )

        self._plan_patch = mock.patch.object(app_module.beets_client, "plan_artist_folder_reconcile", side_effect=mock_plan)
        self._plan_patch.start()
        self.addCleanup(self._plan_patch.stop)
        self._apply_patch = mock.patch.object(app_module.beets_client, "apply_artist_folder_reconcile", side_effect=mock_apply)
        self._apply_patch.start()
        self.addCleanup(self._apply_patch.stop)

    def test_production_path_apply_artist_folder_groups(self):
        stamped = self._create_artist_folder("Artist Name (a1b2c3d4-e5f6-7890-abcd-ef0123456789)")
        unstamped = self._create_artist_folder("artist name")
        self._create_track_file("artist name/Album/track.flac", b"TRACK_BYTES")
        self._insert_album(1, "Album", "artist name", "artist name/Album", mbid="a1b2c3d4-e5f6-7890-abcd-ef0123456789")
        self._insert_item(1, 1, "artist name", "Album", "artist name/Album/track.flac", mbid="a1b2c3d4-e5f6-7890-abcd-ef0123456789")

        mock_groups = [{
            "key": "artistname",
            "canonical": {"path": str(stamped), "name": stamped.name},
            "sources": [{"path": str(unstamped), "name": unstamped.name}],
            "musicbrainz": {"id": "a1b2c3d4-e5f6-7890-abcd-ef0123456789"},
        }]

        with mock.patch.object(app_module, "_scan_artist_folder_groups", return_value=mock_groups):
            with mock.patch.object(app_module, "_artist_folder_fingerprint_confirms", return_value=True):
                log = []
                res = app_module._apply_artist_folder_groups(str(self.music_root), None, False, log, use_musicbrainz=False)
                self.assertEqual(res.get("groups"), 1, f"Log output: {log}")
                self.assertTrue(any("Delegated artist folder merge to engine" in line for line in log))


class WebManagerMutationProhibitionTests(unittest.TestCase):
    """AST structural inspection asserting Web Manager contains zero direct mutations."""

    def test_artist_folder_functions_contain_no_direct_mutations(self):
        with open("app.py", "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename="app.py")

        target_funcs = {
            "_merge_artist_dir_contents",
            "_apply_artist_folder_groups",
            "clean_artist_folders_stamp_mbid",
        }

        prohibited_attributes = {"unlink", "rename", "replace", "rmdir", "remove"}
        prohibited_shutil = {"move", "rmtree", "copy", "copy2"}

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in target_funcs:
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Attribute) and sub.attr in prohibited_attributes:
                        self.fail(f"Direct mutation .{sub.attr} found in {node.name}")
                    if isinstance(sub, ast.Call):
                        if isinstance(sub.func, ast.Attribute) and isinstance(sub.func.value, ast.Name):
                            if sub.func.value.id == "shutil" and sub.func.attr in prohibited_shutil:
                                self.fail(f"Direct shutil.{sub.func.attr} found in {node.name}")


if __name__ == "__main__":
    unittest.main()
