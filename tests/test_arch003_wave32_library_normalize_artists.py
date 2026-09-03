"""SEC-002 / ARCH-003 Wave 32: library_normalize_artists()'s DB-side
albumartist rename migrated from raw UPDATE albums/UPDATE items SQL onto
album_metadata_repair_v1, and its write+move step migrated from local
subprocess.run([BEET_BIN, "write"/"move", ...]) onto
beets_client.update_album_metadata(force_write_tags=True) +
beets_client.relocate_album(). This closes both the SQL-sink problem and
the "no local Beets execution fallback" architecture violation for this
route -- the same migration already applied to the sibling auto-triggered
function _run_normalize_artists_if_needed() in Wave 30, confirmed to be
literally duplicate logic before migrating.
"""

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import app as app_module
from backend.beets_client import BeetsError, BeetsUnavailableError


class LibraryNormalizeArtistsTests(unittest.TestCase):
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

    def _run(self):
        captured = {}

        def fake_start_python(fn, label=None, metadata=None):
            log = []
            fn(log, cancel_event=None)
            captured["log"] = log
            return mock.Mock(job_id="job-test")

        with app_module.app.test_request_context(
            "/api/library/normalize-artists", method="POST",
        ), mock.patch.object(app_module.jobs, "start_python", side_effect=fake_start_python):
            app_module.library_normalize_artists()
        return captured.get("log", [])

    def test_no_op_when_nothing_needs_normalizing(self):
        self._insert_album(1, "Clean Artist")
        with mock.patch.object(app_module.beets_client, "update_album_metadata") as mock_update, \
             mock.patch.object(app_module, "subprocess") as mock_subprocess:
            log = self._run()
        mock_update.assert_not_called()
        mock_subprocess.run.assert_not_called()
        self.assertTrue(any("No artist names needed normalization" in line for line in log))

    def test_normalizes_via_engine_and_relocates_without_any_local_subprocess(self):
        dirty = "Wu‐Tang Clan"
        clean = app_module._normalize_albumartist(dirty)
        self.assertNotEqual(dirty, clean)
        self._insert_album(1, dirty)

        with mock.patch.object(
            app_module.beets_client, "update_album_metadata",
            return_value={"ok": True, "album_fields_changed": 1, "items_changed": 3},
        ) as mock_update, mock.patch.object(
            app_module.beets_client, "relocate_album",
            return_value={"ok": True, "dest_dir": "/data/media/music/Wu-Tang Clan"},
        ) as mock_relocate, mock.patch.object(
            app_module, "subprocess",
        ) as mock_subprocess:
            log = self._run()

        mock_update.assert_called_once_with(1, {"albumartist": clean}, force_write_tags=True)
        mock_relocate.assert_called_once_with(1, mode="rename")
        # No local `beet write`/`beet move` subprocess execution at all --
        # the architecture-violation half of this migration, not just the
        # SQL-sink half.
        mock_subprocess.run.assert_not_called()
        self.assertTrue(any("Renamed:" in line for line in log))
        self.assertTrue(any("Relocated album 1" in line for line in log))

    def test_engine_rejection_is_logged_not_raised_and_album_not_relocated(self):
        dirty = "Wu‐Tang Clan"
        self._insert_album(1, dirty)
        with mock.patch.object(
            app_module.beets_client, "update_album_metadata",
            return_value={"ok": False, "error": "boom"},
        ), mock.patch.object(app_module.beets_client, "relocate_album") as mock_relocate:
            log = self._run()
        mock_relocate.assert_not_called()
        self.assertTrue(any("Engine rejected normalize" in line for line in log))

    def test_engine_unavailable_is_logged_not_raised(self):
        dirty = "Wu‐Tang Clan"
        self._insert_album(1, dirty)
        with mock.patch.object(
            app_module.beets_client, "update_album_metadata",
            side_effect=BeetsUnavailableError("offline"),
        ), mock.patch.object(app_module.beets_client, "relocate_album") as mock_relocate:
            log = self._run()
        mock_relocate.assert_not_called()
        self.assertTrue(any("Engine unavailable" in line for line in log))


if __name__ == "__main__":
    unittest.main()
