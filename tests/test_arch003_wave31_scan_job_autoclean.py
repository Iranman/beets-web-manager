"""SEC-002 / ARCH-003 Wave 31: _do_scan_job()'s auto-clean-stale-DB-entries
step migrated from raw batched DELETE FROM items/albums SQL onto
album_maintenance_v1, the same per-album-grouped pattern as
library_sync_deleted() (Wave 30), plus a separate read-only sweep for any
album left with zero items (reusing beets_client.delete_album(), the same
primitive _clean_remove_empty_albums() already uses) to reproduce the
original global "any empty album, not just ones this scan found" DELETE
FROM albums WHERE id NOT IN (...) semantics.

This whole auto-scan path is legacy, opt-in-only code
(_legacy_local_scan_enabled(), gated behind BEETS_ENABLE_LEGACY_LOCAL_SCAN)
that never runs in the supported external-engine deployment by default --
confirmed by reading app.py's own gate before migrating it.
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


class ScanJobAutoCleanTests(unittest.TestCase):
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
        self._thread_patch = mock.patch.object(app_module.threading, "Thread")
        self._thread_patch.start()

    def tearDown(self):
        self._thread_patch.stop()
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

    def _insert_item(self, item_id, album_id, relative_path):
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT INTO items (id, album_id, path) VALUES (?, ?, ?)",
                (item_id, album_id, relative_path.encode("utf-8")),
            )
            con.commit()

    def _run_scan(self):
        captured = {}

        def fake_start_python(fn, label=None, metadata=None):
            log = []
            fn(log, cancel_event=None)
            captured["log"] = log
            return mock.Mock(job_id="job-test")

        with mock.patch.object(app_module.jobs, "start_python", side_effect=fake_start_python):
            app_module._do_scan_job()
        return captured.get("log", [])

    def test_stale_item_removed_via_engine_grouped_by_album(self):
        # A real file must exist somewhere so root_accessible/disk_files
        # is non-empty (the auto-clean block requires both).
        (self.music_root / "keep.mp3").write_bytes(b"audio")
        (self.music_root / "keep2.mp3").write_bytes(b"audio")
        (self.music_root / "keep3.mp3").write_bytes(b"audio")
        self._insert_album(1)
        self._insert_item(10, 1, "keep.mp3")
        self._insert_item(11, 1, "missing.mp3")
        # Pad with extra present items so 1 missing stays well under the
        # >50%-missing safety guard (a 1-of-2 fixture would trip it).
        self._insert_item(12, 1, "keep2.mp3")
        self._insert_item(13, 1, "keep3.mp3")

        with mock.patch.object(
            app_module.beets_client, "plan_album_maintenance",
            return_value={"ok": True, "operation_id": "op-1"},
        ) as mock_plan, mock.patch.object(
            app_module.beets_client, "apply_album_maintenance",
            return_value={"ok": True, "deleted_items": 1, "deleted_albums": 0},
        ) as mock_apply, mock.patch.object(
            app_module.beets_client, "delete_album",
        ) as mock_delete_album:
            log = self._run_scan()

        mock_plan.assert_called_once_with({
            "mode": "remove_tracks",
            "album_id": 1,
            "item_ids": [11],
            "delete_files": False,
            "clean_empty_folders": False,
        })
        mock_apply.assert_called_once_with("op-1")
        # Album 1 still has item 10 -- the global empty-album sweep must
        # not touch it.
        mock_delete_album.assert_not_called()
        self.assertTrue(any("cleaned:1 stale" in line for line in log))

    def test_global_sweep_removes_preexisting_empty_album_unrelated_to_scan(self):
        (self.music_root / "keep.mp3").write_bytes(b"audio")
        self._insert_album(1)
        # At least one missing file is needed to enter the cleanup block
        # at all (matches the original code's own gating); padded with
        # present items to stay under the >50%-missing guard.
        (self.music_root / "keep2.mp3").write_bytes(b"audio")
        (self.music_root / "keep3.mp3").write_bytes(b"audio")
        self._insert_item(10, 1, "keep.mp3")
        self._insert_item(12, 1, "keep2.mp3")
        self._insert_item(13, 1, "keep3.mp3")
        self._insert_item(11, 1, "missing.mp3")
        # Album 2 is already empty before this scan even runs -- not
        # something the stale-item pass above would ever find or touch.
        self._insert_album(2)

        with mock.patch.object(
            app_module.beets_client, "plan_album_maintenance",
            return_value={"ok": True, "operation_id": "op-1"},
        ), mock.patch.object(
            app_module.beets_client, "apply_album_maintenance",
            return_value={"ok": True, "deleted_items": 1, "deleted_albums": 0},
        ), mock.patch.object(
            app_module.beets_client, "delete_album",
            return_value={"ok": True},
        ) as mock_delete_album:
            self._run_scan()

        mock_delete_album.assert_called_once_with(2, delete_files=False)

    def test_item_with_no_album_id_is_skipped_not_crashed(self):
        (self.music_root / "keep1.mp3").write_bytes(b"audio")
        (self.music_root / "keep2.mp3").write_bytes(b"audio")
        (self.music_root / "keep3.mp3").write_bytes(b"audio")
        self._insert_item(96, None, "keep1.mp3")
        self._insert_item(97, None, "keep2.mp3")
        self._insert_item(98, None, "keep3.mp3")
        self._insert_item(99, None, "missing.mp3")

        with mock.patch.object(app_module.beets_client, "plan_album_maintenance") as mock_plan,              mock.patch.object(app_module.beets_client, "delete_album"):
            log = self._run_scan()
        mock_plan.assert_not_called()
        self.assertTrue(any("no album_id" in line for line in log))

    def test_over_50_percent_missing_skips_cleanup_entirely(self):
        self._insert_album(1)
        self._insert_item(10, 1, "missing1.mp3")
        self._insert_item(11, 1, "missing2.mp3")
        # A real file must exist for disk_files to be non-empty and
        # root_accessible True, but the vast majority of DB rows are
        # still "missing" -- must trip the >50% guard.
        (self.music_root / "keep.mp3").write_bytes(b"audio")

        with mock.patch.object(app_module.beets_client, "plan_album_maintenance") as mock_plan, \
             mock.patch.object(app_module.beets_client, "delete_album") as mock_delete_album:
            log = self._run_scan()
        mock_plan.assert_not_called()
        mock_delete_album.assert_not_called()
        self.assertTrue(any("skipping DB cleanup" in line for line in log))

    def test_engine_unavailable_is_logged_not_raised(self):
        (self.music_root / "keep.mp3").write_bytes(b"audio")
        (self.music_root / "keep2.mp3").write_bytes(b"audio")
        (self.music_root / "keep3.mp3").write_bytes(b"audio")
        self._insert_album(1)
        self._insert_item(10, 1, "keep.mp3")
        self._insert_item(12, 1, "keep2.mp3")
        self._insert_item(13, 1, "keep3.mp3")
        self._insert_item(11, 1, "missing.mp3")

        with mock.patch.object(
            app_module.beets_client, "plan_album_maintenance",
            side_effect=BeetsUnavailableError("offline"),
        ), mock.patch.object(app_module.beets_client, "delete_album"):
            log = self._run_scan()
        self.assertTrue(any("DB cleanup engine unavailable" in line for line in log))


if __name__ == "__main__":
    unittest.main()
