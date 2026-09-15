"""SEC-002 / ARCH-003 Wave 30: clean_merge_duplicate_album(), clean_rgid_group_merge(),
and clean_rgid_group_relink() migrated from raw UPDATE/DELETE album-row SQL onto
the new album_duplicate_merge_v1 family (the first two) and
beets_client.update_album_metadata() / album_metadata_repair_v1 (the third),
via BeetsClient. These tests prove the routes call the engine rather than
mutating the Beets DB directly, and preserve the interactive log output
(moved count, inherited fields, release-relink summary) real callers depend on.
"""

import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import app as app_module

SAME_MBID = "10101010-1010-1010-1010-101010101010"
SAME_RGID = "20202020-2020-2020-2020-202020202020"


class _MergeTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name).resolve()
        self.db_path = self.tmp_path / "musiclibrary.db"
        with sqlite3.connect(self.db_path) as con:
            con.executescript(
                """
                CREATE TABLE albums (
                    id INTEGER PRIMARY KEY, album TEXT, albumartist TEXT,
                    mb_albumid TEXT, mb_releasegroupid TEXT, year INTEGER, label TEXT
                );
                CREATE TABLE items (
                    id INTEGER PRIMARY KEY, album_id INTEGER, disc INTEGER, track INTEGER,
                    title TEXT, mb_trackid TEXT, path BLOB
                );
                """
            )
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
        self._invalidate_patch = mock.patch.object(app_module, "_invalidate_lib_cache")
        self._invalidate_patch.start()

    def tearDown(self):
        self._invalidate_patch.stop()
        self._db_patch.stop()
        try:
            self.tmpdir.cleanup()
        except Exception:
            pass

    def _insert_album(self, album_id, artist="Artist", album="Album", mbid=SAME_MBID, rgid=SAME_RGID, year=0, label=""):
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT INTO albums (id, album, albumartist, mb_albumid, mb_releasegroupid, year, label) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (album_id, album, artist, mbid, rgid, year, label),
            )
            con.commit()

    def _insert_item(self, item_id, album_id, disc=1, track=1, mbid=""):
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT INTO items (id, album_id, disc, track, title, mb_trackid, path) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (item_id, album_id, disc, track, "Track", mbid, b""),
            )
            con.commit()

    def _run_job_body(self, route_fn, payload, url):
        with app_module.app.test_request_context(url, method="POST", data=json.dumps(payload), content_type="application/json"):
            captured = {}

            def fake_start_python(fn, label=None, metadata=None):
                log = []
                fn(log, cancel_event=None)
                captured["log"] = log
                captured["result"] = None
                return mock.Mock(job_id="job-test")

            with mock.patch.object(app_module.jobs, "start_python", side_effect=fake_start_python):
                response = route_fn()
            return response, captured.get("log", [])


class CleanMergeDuplicateAlbumTests(_MergeTestBase):
    def test_merge_routes_through_engine_and_logs_moved_and_inherited(self):
        # _library_duplicate_merge_safety requires BOTH rows to already carry
        # a non-blank, matching MusicBrainz release id -- test year/label
        # inheritance instead (target blank, source has it), which is the
        # realistic case: a merge safety-gated on identity, inheriting only
        # secondary metadata the target happens to be missing.
        self._insert_album(101, artist="Artist", album="Album", mbid=SAME_MBID, year=0, label="")
        self._insert_album(102, artist="Artist", album="Album", mbid=SAME_MBID, year=2001, label="Cool Label")
        self._insert_item(11, 101, disc=1, track=1)
        self._insert_item(12, 102, disc=1, track=2)

        with mock.patch.object(
            app_module.beets_client, "merge_duplicate_albums",
            return_value={"ok": True, "moved": 1, "inherit_fields": {"year": 2001, "label": "Cool Label"}},
        ) as mock_merge:
            response, log = self._run_job_body(
                app_module.clean_merge_duplicate_album,
                {"target_album_id": 101, "source_album_id": 102},
                "/api/clean/merge-duplicate-album",
            )
        mock_merge.assert_called_once_with(101, 102)
        self.assertTrue(any("Moved 1 item" in line for line in log))
        self.assertTrue(any("Inherited year=" in line for line in log))
        self.assertTrue(any("Deleted source album row 102" in line for line in log))

    def test_merge_engine_rejection_raises_not_swallowed(self):
        self._insert_album(101, artist="Artist", album="Album", mbid=SAME_MBID)
        self._insert_album(102, artist="Artist", album="Album", mbid=SAME_MBID)
        self._insert_item(11, 101, disc=1, track=1)
        self._insert_item(12, 102, disc=1, track=2)

        with mock.patch.object(
            app_module.beets_client, "merge_duplicate_albums",
            return_value={"ok": False, "error": "engine down"},
        ), app_module.app.test_request_context(
            "/api/clean/merge-duplicate-album", method="POST",
            data=json.dumps({"target_album_id": 101, "source_album_id": 102}),
            content_type="application/json",
        ):
            def fake_start_python(fn, label=None, metadata=None):
                with self.assertRaises(RuntimeError):
                    fn([], cancel_event=None)
                return mock.Mock(job_id="job-test")
            with mock.patch.object(app_module.jobs, "start_python", side_effect=fake_start_python):
                app_module.clean_merge_duplicate_album()


class CleanRgidGroupMergeTests(_MergeTestBase):
    def test_merge_routes_through_engine(self):
        self._insert_album(201, artist="Artist", album="Album A", mbid=SAME_MBID, rgid=SAME_RGID, year=0, label="")
        self._insert_album(202, artist="Artist", album="Album B", mbid=SAME_MBID, rgid=SAME_RGID, year=1995, label="")
        self._insert_item(21, 201, disc=1, track=1)
        self._insert_item(22, 202, disc=1, track=2)

        with mock.patch.object(
            app_module.beets_client, "merge_duplicate_albums",
            return_value={"ok": True, "moved": 1, "inherit_fields": {"year": 1995}},
        ) as mock_merge, mock.patch.object(app_module, "_clear_rgid_resolution") as mock_clear:
            response, log = self._run_job_body(
                app_module.clean_rgid_group_merge,
                {"mb_releasegroupid": SAME_RGID, "target_album_id": 201, "source_album_id": 202},
                "/api/clean/rgid-group/merge",
            )
        mock_merge.assert_called_once_with(201, 202)
        mock_clear.assert_called_once_with(SAME_RGID)
        self.assertTrue(any("Moved 1 item" in line for line in log))


class CleanRgidGroupRelinkTests(_MergeTestBase):
    def test_relink_routes_through_update_album_metadata(self):
        self._insert_album(301, artist="Artist", album="Album", mbid="", rgid="")

        with mock.patch.object(
            app_module.beets_client, "update_album_metadata",
            return_value={"ok": True, "album_fields_changed": 2},
        ) as mock_update, mock.patch.object(
            app_module, "_repair_album_mbid_sticking_once",
            return_value={"changed": True},
        ):
            response, log = self._run_job_body(
                app_module.clean_rgid_group_relink,
                {"album_id": 301, "mb_albumid": SAME_MBID, "mb_releasegroupid": SAME_RGID},
                "/api/clean/rgid-group/relink",
            )
        mock_update.assert_called_once_with(301, {"mb_albumid": SAME_MBID, "mb_releasegroupid": SAME_RGID})

    def test_relink_engine_rejection_raises_not_swallowed(self):
        self._insert_album(301, artist="Artist", album="Album", mbid="", rgid="")
        with mock.patch.object(
            app_module.beets_client, "update_album_metadata",
            return_value={"ok": False, "error": "engine down"},
        ):
            with app_module.app.test_request_context(
                "/api/clean/rgid-group/relink", method="POST",
                data=json.dumps({"album_id": 301, "mb_albumid": SAME_MBID, "mb_releasegroupid": SAME_RGID}),
                content_type="application/json",
            ):
                def fake_start_python(fn, label=None, metadata=None):
                    with self.assertRaises(RuntimeError):
                        fn([], cancel_event=None)
                    return mock.Mock(job_id="job-test")
                with mock.patch.object(app_module.jobs, "start_python", side_effect=fake_start_python):
                    app_module.clean_rgid_group_relink()


if __name__ == "__main__":
    unittest.main()
