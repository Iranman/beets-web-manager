"""SEC-002 / ARCH-003 Wave 31: album_merge_split_album() migrated from raw
UPDATE items SET album_id=.../DELETE FROM albums SQL (plus a local
shutil.copy2(LIB_PATH, ...) DB backup that cannot actually reach LIB_PATH
in the supported two-service deployment) onto album_duplicate_merge_v1's
new partial/adopt mode via BeetsClient.merge_split_album_items(). This
migration also adds a real Release-Group identity check the route never
had before -- see tests/test_beets_transaction_engine.py for the
engine-level proof of that check; these tests prove the route wires
selection (physical-location-under-target-folder filtering, which stays
a local read) through to the engine correctly and surfaces engine
rejection/identity-conflict errors rather than swallowing them.
"""

import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import app as app_module


class AlbumMergeSplitAlbumTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name).resolve()
        self.music_root = self.tmp_path / "music"
        self.music_root.mkdir()
        self.db_path = self.tmp_path / "musiclibrary.db"
        with sqlite3.connect(self.db_path) as con:
            con.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, album TEXT, albumartist TEXT)")
            con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, path BLOB, title TEXT, disc INT, track INT)")
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

    def _insert_item(self, item_id, album_id, path, title="Track", disc=1, track=1):
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT INTO items (id, album_id, path, title, disc, track) VALUES (?, ?, ?, ?, ?, ?)",
                (item_id, album_id, str(path).encode("utf-8"), title, disc, track),
            )
            con.commit()

    def _run(self, target_id, payload):
        with app_module.app.test_request_context(
            f"/api/albums/{target_id}/merge-split-album", method="POST",
            data=json.dumps(payload), content_type="application/json",
        ):
            captured = {}

            def fake_start_python(fn, label=None, metadata=None):
                log = []
                result = fn(log)
                captured["log"] = log
                captured["result"] = result
                return mock.Mock(job_id="job-test")

            with mock.patch.object(app_module.jobs, "start_python", side_effect=fake_start_python):
                response = app_module.album_merge_split_album(target_id)
            return response, captured.get("log", []), captured.get("result")

    def test_dry_run_never_calls_engine(self):
        target_dir = self.music_root / "Artist" / "Album"
        target_dir.mkdir(parents=True)
        self._insert_album(1)
        self._insert_album(2)
        self._insert_item(10, 1, target_dir / "01 Track.mp3")
        self._insert_item(20, 2, target_dir / "02 Track.mp3")

        with mock.patch.object(app_module.beets_client, "merge_split_album_items") as mock_merge:
            response, log, result = self._run(1, {"source_album_id": 2, "item_ids": [20], "dry_run": True})
        mock_merge.assert_not_called()
        self.assertTrue(result.get("dry_run"))
        self.assertEqual(result.get("item_count"), 1)

    def test_real_run_only_merges_items_physically_under_target_folder(self):
        target_dir = self.music_root / "Artist" / "Album"
        target_dir.mkdir(parents=True)
        elsewhere = self.music_root / "Artist" / "Other Album"
        elsewhere.mkdir(parents=True)
        self._insert_album(1)
        self._insert_album(2)
        self._insert_item(10, 1, target_dir / "01 Track.mp3")
        # item 20 physically sits inside target's own folder (the real
        # "split album" case: file already there, DB just disagrees).
        self._insert_item(20, 2, target_dir / "02 Track.mp3")
        # item 21 belongs to source but its file is NOT under target's
        # folder -- must be skipped, never sent to the engine.
        self._insert_item(21, 2, elsewhere / "03 Track.mp3")

        with mock.patch.object(
            app_module.beets_client, "merge_split_album_items",
            return_value={"ok": True, "moved": 1, "source_album_deleted": False},
        ) as mock_merge:
            response, log, result = self._run(1, {"source_album_id": 2, "item_ids": [20, 21], "dry_run": False, "confirmed": True})
        mock_merge.assert_called_once_with(1, 2, [20])
        self.assertTrue(any("Skipped id:21" in line for line in log))
        self.assertEqual(result.get("item_count"), 1)

    def test_engine_identity_rejection_is_raised_not_swallowed(self):
        target_dir = self.music_root / "Artist" / "Album"
        target_dir.mkdir(parents=True)
        self._insert_album(1)
        self._insert_album(2)
        self._insert_item(10, 1, target_dir / "01 Track.mp3")
        self._insert_item(20, 2, target_dir / "02 Track.mp3")

        with mock.patch.object(
            app_module.beets_client, "merge_split_album_items",
            return_value={"ok": False, "error": "item(s) [20] carry a conflicting release-group id", "code": "album_duplicate_merge_identity_mismatch"},
        ):
            with app_module.app.test_request_context(
                "/api/albums/1/merge-split-album", method="POST",
                data=json.dumps({"source_album_id": 2, "item_ids": [20], "dry_run": False, "confirmed": True}),
                content_type="application/json",
            ):
                def fake_start_python(fn, label=None, metadata=None):
                    with self.assertRaises(RuntimeError) as ctx:
                        fn([])
                    self.assertIn("conflicting", str(ctx.exception))
                    return mock.Mock(job_id="job-test")
                with mock.patch.object(app_module.jobs, "start_python", side_effect=fake_start_python):
                    app_module.album_merge_split_album(1)

    def test_no_local_backup_file_written(self):
        """The removed shutil.copy2(LIB_PATH, ...) step must not be
        replaced by some other local-file side effect -- recovery is the
        engine transaction's job now."""
        target_dir = self.music_root / "Artist" / "Album"
        target_dir.mkdir(parents=True)
        self._insert_album(1)
        self._insert_album(2)
        self._insert_item(10, 1, target_dir / "01 Track.mp3")
        self._insert_item(20, 2, target_dir / "02 Track.mp3")

        before = set(self.tmp_path.iterdir())
        with mock.patch.object(
            app_module.beets_client, "merge_split_album_items",
            return_value={"ok": True, "moved": 1, "source_album_deleted": True},
        ):
            self._run(1, {"source_album_id": 2, "item_ids": [20], "dry_run": False, "confirmed": True})
        after = set(self.tmp_path.iterdir())
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
