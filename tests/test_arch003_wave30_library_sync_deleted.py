"""SEC-002 / ARCH-003 Wave 30: library_sync_deleted()'s missing-file DB
cleanup (/api/library/sync-deleted) migrated from raw batched
DELETE FROM items/albums SQL onto album_maintenance_v1 (remove_tracks
mode), the same pattern as _clean_remove_orphaned_items(). Selection
(which item rows have no backing file on disk) is a local,
non-mutating filesystem check; deletion is one engine Plan/Apply call
per affected album.
"""

import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import app as app_module
from backend.beets_client import BeetsError, BeetsUnavailableError


class LibrarySyncDeletedTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name).resolve()
        self.music_root = self.tmp_path / "music"
        self.music_root.mkdir()
        self.db_path = self.tmp_path / "musiclibrary.db"
        with sqlite3.connect(self.db_path) as con:
            con.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, albumartist TEXT, album TEXT)")
            con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, path BLOB)")
            con.commit()

        @contextmanager
        def _mock_db_cm(*args, **kwargs):
            con = sqlite3.connect(self.db_path)
            if kwargs.get("text_factory") is not None:
                con.text_factory = kwargs["text_factory"]
            if kwargs.get("row_factory") is not None:
                con.row_factory = kwargs["row_factory"]
            try:
                yield con
            finally:
                con.close()

        self._db_patch = mock.patch.object(app_module, "_db", side_effect=_mock_db_cm)
        self._db_patch.start()
        self._root_patch = mock.patch.object(app_module, "MUSIC_ROOT", self.music_root)
        self._root_patch.start()
        self._invalidate_patch = mock.patch.object(app_module, "_invalidate_lib_cache")
        self._invalidate_patch.start()

    def tearDown(self):
        self._invalidate_patch.stop()
        self._root_patch.stop()
        self._db_patch.stop()
        try:
            self.tmpdir.cleanup()
        except Exception:
            pass

    def _insert_album(self, album_id, artist="Artist", album="Album"):
        with sqlite3.connect(self.db_path) as con:
            con.execute("INSERT INTO albums (id, albumartist, album) VALUES (?, ?, ?)", (album_id, artist, album))
            con.commit()

    def _insert_item(self, item_id, album_id, path):
        # Store MUSIC_ROOT-relative paths (real Beets convention) rather
        # than absolute ones: library_sync_deleted._do's own existence
        # check uses a Unix-only `p.startswith("/")` test to decide whether
        # to resolve against MUSIC_ROOT (pre-existing, unrelated to this
        # migration -- production only ever runs on Linux). A relative
        # path exercises the same `Path(MUSIC_ROOT + "/" + rel).exists()`
        # resolution on every platform this test suite runs on.
        with sqlite3.connect(self.db_path) as con:
            con.execute("INSERT INTO items (id, album_id, path) VALUES (?, ?, ?)", (item_id, album_id, str(path).encode("utf-8")))
            con.commit()

    def _run(self, dry_run, confirmed=True):
        payload = {"dry_run": dry_run, "confirmed": confirmed}
        with app_module.app.test_request_context(
            "/api/library/sync-deleted", method="POST",
            data=json.dumps(payload), content_type="application/json",
        ):
            captured = {}

            def fake_start_python(fn, label=None, metadata=None):
                log = []
                fn(log, cancel_event=None, update_state=lambda *_a, **_k: None)
                captured["log"] = log
                return mock.Mock(job_id="job-test")

            with mock.patch.object(app_module.jobs, "start_python", side_effect=fake_start_python):
                app_module.library_sync_deleted()
            return captured.get("log", [])

    def test_dry_run_never_calls_engine(self):
        self._insert_album(1)
        self._insert_item(11, 1, "missing.mp3")
        with mock.patch.object(app_module.beets_client, "plan_album_maintenance") as mock_plan:
            log = self._run(dry_run=True)
        mock_plan.assert_not_called()
        self.assertTrue(any("Preview only" in line for line in log))

    def test_all_items_missing_removes_whole_album_via_engine(self):
        self._insert_album(1)
        self._insert_item(11, 1, "missing.mp3")
        with mock.patch.object(
            app_module.beets_client, "plan_album_maintenance",
            return_value={"ok": True, "operation_id": "op-1"},
        ) as mock_plan, mock.patch.object(
            app_module.beets_client, "apply_album_maintenance",
            return_value={"ok": True, "deleted_items": 1, "deleted_albums": 1},
        ) as mock_apply:
            log = self._run(dry_run=False)
        mock_plan.assert_called_once_with({
            "mode": "remove_tracks",
            "album_id": 1,
            "item_ids": [11],
            "delete_files": False,
            "clean_empty_folders": False,
        })
        mock_apply.assert_called_once_with("op-1")
        self.assertTrue(any("removed 1 album(s), 1 track(s)" in line for line in log))

    def test_partial_missing_keeps_album_removes_only_missing_items(self):
        self._insert_album(1)
        real_file = self.music_root / "real.mp3"
        real_file.write_bytes(b"audio")
        self._insert_item(11, 1, "real.mp3")
        self._insert_item(12, 1, "missing.mp3")
        with mock.patch.object(
            app_module.beets_client, "plan_album_maintenance",
            return_value={"ok": True, "operation_id": "op-1"},
        ) as mock_plan, mock.patch.object(
            app_module.beets_client, "apply_album_maintenance",
            return_value={"ok": True, "deleted_items": 1, "deleted_albums": 0},
        ):
            log = self._run(dry_run=False)
        mock_plan.assert_called_once_with({
            "mode": "remove_tracks",
            "album_id": 1,
            "item_ids": [12],
            "delete_files": False,
            "clean_empty_folders": False,
        })
        self.assertTrue(any("removed 0 album(s), 1 track(s)" in line for line in log))

    def test_orphan_item_with_no_album_is_skipped_not_crashed(self):
        self._insert_item(99, None, "missing.mp3")
        with mock.patch.object(app_module.beets_client, "plan_album_maintenance") as mock_plan:
            log = self._run(dry_run=False)
        mock_plan.assert_not_called()
        self.assertTrue(any("no album_id" in line for line in log))

    def test_engine_rejection_is_logged_not_raised(self):
        self._insert_album(1)
        self._insert_item(11, 1, "missing.mp3")
        with mock.patch.object(
            app_module.beets_client, "plan_album_maintenance",
            return_value={"ok": False, "error": "boom"},
        ):
            log = self._run(dry_run=False)
        self.assertTrue(any("engine rejected sync plan" in line for line in log))

    def test_engine_unavailable_is_logged_not_raised(self):
        self._insert_album(1)
        self._insert_item(11, 1, "missing.mp3")
        with mock.patch.object(
            app_module.beets_client, "plan_album_maintenance",
            side_effect=BeetsUnavailableError("offline"),
        ):
            log = self._run(dry_run=False)
        self.assertTrue(any("engine unavailable syncing album_id" in line for line in log))


if __name__ == "__main__":
    unittest.main()
