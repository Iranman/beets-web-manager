"""SEC-002 / ARCH-003 Wave 30: `_clean_remove_orphaned_items()` and
`_clean_remove_empty_albums()` migrated from raw `DELETE FROM items`/
`DELETE FROM albums` SQL onto album_maintenance_v1 (remove_tracks /
remove_album modes) via BeetsClient. These are read-then-mutate helpers:
selection (which items are orphaned / which albums are empty) stays a
local, non-mutating query; only the actual delete goes through the
engine transaction boundary.
"""

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import app as app_module
from backend.beets_client import BeetsError, BeetsUnavailableError

SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY,
    album TEXT,
    albumartist TEXT
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    album_id INTEGER,
    artist TEXT,
    title TEXT,
    path BLOB
);
"""


class _CleanupTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name).resolve()
        self.db_path = self.tmp_path / "musiclibrary.db"
        with sqlite3.connect(self.db_path) as con:
            con.executescript(SCHEMA)
            con.commit()

        @contextmanager
        def _mock_db_cm(*args, **kwargs):
            con = sqlite3.connect(self.db_path)
            if "text_factory" in kwargs and kwargs["text_factory"] is not None:
                con.text_factory = kwargs["text_factory"]
            if "row_factory" in kwargs and kwargs["row_factory"] is not None:
                con.row_factory = kwargs["row_factory"]
            try:
                yield con
            finally:
                con.close()

        self._db_patch = mock.patch.object(app_module, "_db", side_effect=_mock_db_cm)
        self._db_patch.start()
        self._plex_patch = mock.patch.object(app_module, "_trigger_plex_refresh")
        self._plex_patch.start()
        self._invalidate_patch = mock.patch.object(app_module, "_invalidate_lib_cache")
        self._invalidate_patch.start()

    def tearDown(self):
        self._invalidate_patch.stop()
        self._plex_patch.stop()
        self._db_patch.stop()
        try:
            self.tmpdir.cleanup()
        except Exception:
            pass

    def _insert_album(self, album_id, artist="Artist", album="Album"):
        with sqlite3.connect(self.db_path) as con:
            con.execute("INSERT INTO albums (id, albumartist, album) VALUES (?, ?, ?)", (album_id, artist, album))
            con.commit()

    def _insert_item(self, item_id, album_id, path, artist="Artist", title="Title"):
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT INTO items (id, album_id, artist, title, path) VALUES (?, ?, ?, ?, ?)",
                (item_id, album_id, artist, title, str(path).encode("utf-8")),
            )
            con.commit()

    def _album_exists(self, album_id):
        with sqlite3.connect(self.db_path) as con:
            row = con.execute("SELECT id FROM albums WHERE id=?", (album_id,)).fetchone()
        return row is not None


class RemoveOrphanedItemsTests(_CleanupTestBase):
    def test_dry_run_never_calls_engine(self):
        self._insert_album(1)
        self._insert_item(101, 1, self.tmp_path / "missing.mp3")
        with mock.patch.object(app_module.beets_client, "plan_album_maintenance") as mock_plan:
            log = []
            res = app_module._clean_remove_orphaned_items([101], dry_run=True, log=log)
        mock_plan.assert_not_called()
        self.assertTrue(res["ok"])
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["removed"], 1)

    def test_real_file_is_skipped_not_removed(self):
        real_file = self.tmp_path / "real.mp3"
        real_file.write_bytes(b"audio")
        self._insert_album(1)
        self._insert_item(101, 1, real_file)
        with mock.patch.object(app_module.beets_client, "plan_album_maintenance") as mock_plan:
            log = []
            res = app_module._clean_remove_orphaned_items([101], dry_run=False, log=log)
        mock_plan.assert_not_called()
        self.assertEqual(res["removed"], 0)
        self.assertEqual(res["skipped"], 1)

    def test_orphaned_item_routes_through_album_maintenance_remove_tracks(self):
        self._insert_album(1)
        self._insert_item(101, 1, self.tmp_path / "missing.mp3")
        with mock.patch.object(
            app_module.beets_client, "plan_album_maintenance",
            return_value={"ok": True, "operation_id": "op-1"},
        ) as mock_plan, mock.patch.object(
            app_module.beets_client, "apply_album_maintenance",
            return_value={"ok": True, "deleted_items": 1, "deleted_albums": 0},
        ) as mock_apply:
            log = []
            res = app_module._clean_remove_orphaned_items([101], dry_run=False, log=log)
        mock_plan.assert_called_once_with({
            "mode": "remove_tracks",
            "album_id": 1,
            "item_ids": [101],
            "delete_files": False,
            "clean_empty_folders": False,
        })
        mock_apply.assert_called_once_with("op-1")
        self.assertTrue(res["ok"])
        self.assertEqual(res["removed"], 1)

    def test_items_grouped_by_album_one_engine_call_per_album(self):
        self._insert_album(1)
        self._insert_album(2)
        self._insert_item(101, 1, self.tmp_path / "missing1.mp3")
        self._insert_item(102, 1, self.tmp_path / "missing2.mp3")
        self._insert_item(201, 2, self.tmp_path / "missing3.mp3")
        with mock.patch.object(
            app_module.beets_client, "plan_album_maintenance",
            return_value={"ok": True, "operation_id": "op-x"},
        ) as mock_plan, mock.patch.object(
            app_module.beets_client, "apply_album_maintenance",
            return_value={"ok": True, "deleted_items": 1, "deleted_albums": 0},
        ):
            log = []
            app_module._clean_remove_orphaned_items([101, 102, 201], dry_run=False, log=log)
        self.assertEqual(mock_plan.call_count, 2)
        calls_by_album = {c.args[0]["album_id"]: c.args[0]["item_ids"] for c in mock_plan.call_args_list}
        self.assertEqual(sorted(calls_by_album[1]), [101, 102])
        self.assertEqual(calls_by_album[2], [201])

    def test_item_with_no_album_id_is_skipped_not_crashed(self):
        self._insert_item(999, None, self.tmp_path / "missing.mp3")
        with mock.patch.object(app_module.beets_client, "plan_album_maintenance") as mock_plan:
            log = []
            res = app_module._clean_remove_orphaned_items([999], dry_run=False, log=log)
        mock_plan.assert_not_called()
        self.assertTrue(res["ok"])
        self.assertEqual(res["removed"], 0)
        self.assertTrue(any("no album_id" in line for line in log))

    def test_engine_unavailable_is_logged_and_does_not_raise(self):
        self._insert_album(1)
        self._insert_item(101, 1, self.tmp_path / "missing.mp3")
        with mock.patch.object(
            app_module.beets_client, "plan_album_maintenance",
            side_effect=BeetsUnavailableError("offline"),
        ):
            log = []
            res = app_module._clean_remove_orphaned_items([101], dry_run=False, log=log)
        self.assertTrue(res["ok"])
        self.assertEqual(res["removed"], 0)
        self.assertTrue(any("Engine unavailable" in line for line in log))

    def test_engine_rejection_is_logged_and_does_not_raise(self):
        self._insert_album(1)
        self._insert_item(101, 1, self.tmp_path / "missing.mp3")
        with mock.patch.object(
            app_module.beets_client, "plan_album_maintenance",
            return_value={"ok": False, "error": "boom"},
        ):
            log = []
            res = app_module._clean_remove_orphaned_items([101], dry_run=False, log=log)
        self.assertTrue(res["ok"])
        self.assertEqual(res["removed"], 0)
        self.assertTrue(any("Engine rejected" in line for line in log))


class RemoveEmptyAlbumsTests(_CleanupTestBase):
    def test_dry_run_never_calls_engine(self):
        self._insert_album(1)
        with mock.patch.object(app_module.beets_client, "delete_album") as mock_del:
            log = []
            res = app_module._clean_remove_empty_albums([1], dry_run=True, log=log)
        mock_del.assert_not_called()
        self.assertEqual(res["removed"], 1)

    def test_non_empty_album_is_skipped_not_deleted(self):
        self._insert_album(1)
        self._insert_item(101, 1, self.tmp_path / "track.mp3")
        with mock.patch.object(app_module.beets_client, "delete_album") as mock_del:
            log = []
            res = app_module._clean_remove_empty_albums([1], dry_run=False, log=log)
        mock_del.assert_not_called()
        self.assertEqual(res["removed"], 0)
        self.assertEqual(res["skipped"], 1)

    def test_empty_album_routes_through_delete_album(self):
        self._insert_album(1)
        with mock.patch.object(
            app_module.beets_client, "delete_album",
            return_value={"ok": True, "status": "completed"},
        ) as mock_del:
            log = []
            res = app_module._clean_remove_empty_albums([1], dry_run=False, log=log)
        mock_del.assert_called_once_with(1, delete_files=False)
        self.assertTrue(res["ok"])
        self.assertEqual(res["removed"], 1)

    def test_engine_rejection_is_logged_and_does_not_raise(self):
        self._insert_album(1)
        with mock.patch.object(
            app_module.beets_client, "delete_album",
            return_value={"ok": False, "error": "boom"},
        ):
            log = []
            res = app_module._clean_remove_empty_albums([1], dry_run=False, log=log)
        self.assertTrue(res["ok"])
        self.assertEqual(res["removed"], 0)
        self.assertTrue(any("Engine rejected" in line for line in log))

    def test_engine_unavailable_is_logged_and_does_not_raise(self):
        self._insert_album(1)
        with mock.patch.object(
            app_module.beets_client, "delete_album",
            side_effect=BeetsError("down"),
        ):
            log = []
            res = app_module._clean_remove_empty_albums([1], dry_run=False, log=log)
        self.assertTrue(res["ok"])
        self.assertEqual(res["removed"], 0)
        self.assertTrue(any("Engine unavailable" in line for line in log))


if __name__ == "__main__":
    unittest.main()
