"""SEC-002 / ARCH-003 Wave 32: library_merge_artist()'s DB-side albumartist
rename (renaming/merging one artist into another across all their albums)
migrated from raw UPDATE albums/UPDATE items SQL onto
album_metadata_repair_v1, and its write+move step migrated from local
subprocess.run([BEET_BIN, "write"/"move", ...]) onto
beets_client.update_album_metadata(force_write_tags=True) +
beets_client.relocate_album() -- the same pattern already proven for
library_normalize_artists() in this same wave. Closes both the SQL-sink
problem and the "no local Beets execution fallback" architecture
violation for this route.
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


class LibraryMergeArtistTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name).resolve()
        self.db_path = self.tmp_path / "musiclibrary.db"
        with sqlite3.connect(self.db_path) as con:
            con.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, albumartist TEXT)")
            con.commit()

        @contextmanager
        def _mock_db_cm(*args, **kwargs):
            con = sqlite3.connect(self.db_path)
            if kwargs.get("row_factory") is not None:
                con.row_factory = kwargs["row_factory"]
            try:
                yield con
            finally:
                con.close()

        self._db_patch = mock.patch.object(app_module, "_db", side_effect=_mock_db_cm)
        self._db_patch.start()
        self._invalidate_patch = mock.patch.object(app_module, "_invalidate_lib_cache")
        self._invalidate_patch.start()

    def tearDown(self):
        self._invalidate_patch.stop()
        self._db_patch.stop()
        try:
            self.tmpdir.cleanup()
        except Exception:
            pass

    def _insert_album(self, album_id, albumartist):
        with sqlite3.connect(self.db_path) as con:
            con.execute("INSERT INTO albums (id, albumartist) VALUES (?, ?)", (album_id, albumartist))
            con.commit()

    def _run(self, from_artist, to_artist):
        payload = {"from_artist": from_artist, "to_artist": to_artist}
        captured = {}

        def fake_start_python(fn, label=None, metadata=None):
            log = []
            fn(log, cancel_event=None)
            captured["log"] = log
            return mock.Mock(job_id="job-test")

        with app_module.app.test_request_context(
            "/api/library/merge-artist", method="POST",
            data=json.dumps(payload), content_type="application/json",
        ), mock.patch.object(app_module.jobs, "start_python", side_effect=fake_start_python):
            app_module.library_merge_artist()
        return captured.get("log", [])

    def test_no_matching_albums_is_a_clean_no_op(self):
        with mock.patch.object(app_module.beets_client, "update_album_metadata") as mock_update, \
             mock.patch.object(app_module, "subprocess") as mock_subprocess:
            log = self._run("Ghost Artist", "New Artist")
        mock_update.assert_not_called()
        mock_subprocess.run.assert_not_called()
        self.assertTrue(any("No albums found" in line for line in log))

    def test_merges_via_engine_and_relocates_without_any_local_subprocess(self):
        self._insert_album(1, "Old Artist")
        self._insert_album(2, "Old Artist")

        with mock.patch.object(
            app_module.beets_client, "update_album_metadata",
            return_value={"ok": True, "album_fields_changed": 1, "items_changed": 3},
        ) as mock_update, mock.patch.object(
            app_module.beets_client, "relocate_album",
            return_value={"ok": True, "dest_dir": "/data/media/music/New Artist"},
        ) as mock_relocate, mock.patch.object(
            app_module, "subprocess",
        ) as mock_subprocess:
            log = self._run("Old Artist", "New Artist")

        self.assertEqual(mock_update.call_count, 2)
        mock_update.assert_any_call(1, {"albumartist": "New Artist"}, force_write_tags=True)
        mock_update.assert_any_call(2, {"albumartist": "New Artist"}, force_write_tags=True)
        self.assertEqual(mock_relocate.call_count, 2)
        mock_subprocess.run.assert_not_called()
        self.assertTrue(any("now under 'New Artist'" in line for line in log))

    def test_engine_rejection_for_one_album_does_not_block_the_other(self):
        self._insert_album(1, "Old Artist")
        self._insert_album(2, "Old Artist")

        def fake_update(aid, updates, **kwargs):
            if aid == 1:
                return {"ok": False, "error": "boom"}
            return {"ok": True}

        with mock.patch.object(
            app_module.beets_client, "update_album_metadata", side_effect=fake_update,
        ), mock.patch.object(
            app_module.beets_client, "relocate_album", return_value={"ok": True, "dest_dir": "x"},
        ) as mock_relocate:
            log = self._run("Old Artist", "New Artist")

        # Only album 2 succeeded and gets relocated.
        mock_relocate.assert_called_once_with(2, mode="rename")
        self.assertTrue(any("Engine rejected rename for album_id 1" in line for line in log))

    def test_engine_unavailable_is_logged_not_raised(self):
        self._insert_album(1, "Old Artist")
        with mock.patch.object(
            app_module.beets_client, "update_album_metadata",
            side_effect=BeetsUnavailableError("offline"),
        ), mock.patch.object(app_module.beets_client, "relocate_album") as mock_relocate:
            log = self._run("Old Artist", "New Artist")
        mock_relocate.assert_not_called()
        self.assertTrue(any("Engine unavailable" in line for line in log))


if __name__ == "__main__":
    unittest.main()
