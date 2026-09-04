"""SEC-002 / ARCH-003 Wave 33: library_mbsync_all() and library_move_all().

library_mbsync_all()'s orphaned-album prune migrated from a raw local
`DELETE FROM albums WHERE id IN (...)` SQL sink onto
beets_client.delete_album() (album_maintenance_v1's mode="remove_album",
the same controlled per-album removal path already used elsewhere for an
album with zero items). This also fixes a real, latent NameError bug in
the code it replaces (see app.py's inline comment).

library_move_all()'s trailing empty-directory cleanup migrated from a
local os.walk(MUSIC_ROOT)+Path.rmdir() sweep -- which had no filesystem
to walk in the real, only-supported deployment topology (the web-manager
container has no mount into MUSIC_ROOT) and so was silently a no-op
there -- onto a DB-derived candidate-directory list (a read, not a
mutation) fed one at a time through beets_client.plan_folder_cleanup()/
apply_folder_cleanup() (folder_cleanup_v1), which runs inside the engine
container that actually has real filesystem access.

`beet mbsync`/`beet update`/`beet move` themselves remain local BEET_BIN
subprocess invocations -- see docs/TECHNICAL_DEBT.md for why (no per-
album engine analog exists for `beet mbsync`'s own MB-matching logic or
`beet update`'s DB-vs-disk resync logic); this wave closes the two raw
mutation *sinks* each route also had, not the whole BEET_BIN dependency.
"""

import ast
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import app as app_module


def _fake_popen(returncode=0, stdout_lines=None):
    proc = mock.Mock()
    proc.stdout = iter(stdout_lines or [])
    proc.wait.return_value = returncode
    proc.kill = mock.Mock()
    proc.communicate = mock.Mock()
    return proc


class LibraryTablesFixture(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name).resolve()
        self.db_path = self.tmp_path / "musiclibrary.db"
        with sqlite3.connect(self.db_path) as con:
            con.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, albumartist TEXT)")
            con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, path TEXT)")
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
        self._plex_patch = mock.patch.object(app_module, "_trigger_plex_refresh")
        self._plex_patch.start()

    def tearDown(self):
        self._plex_patch.stop()
        self._invalidate_patch.stop()
        self._db_patch.stop()
        try:
            self.tmpdir.cleanup()
        except Exception:
            pass

    def _insert_album(self, album_id):
        with sqlite3.connect(self.db_path) as con:
            con.execute("INSERT INTO albums (id, albumartist) VALUES (?, ?)", (album_id, "Some Artist"))
            con.commit()

    def _insert_item(self, item_id, album_id, path):
        with sqlite3.connect(self.db_path) as con:
            con.execute("INSERT INTO items (id, album_id, path) VALUES (?, ?, ?)", (item_id, album_id, path))
            con.commit()

    def _run(self, fn, route):
        captured = {}

        def fake_start_python(fn_inner, label=None, metadata=None):
            log = []
            fn_inner(log, cancel_event=None)
            captured["log"] = log
            return mock.Mock(job_id="job-test")

        with app_module.app.test_request_context(route, method="POST"), \
             mock.patch.object(app_module.jobs, "start_python", side_effect=fake_start_python):
            fn()
        return captured.get("log", [])


class LibraryMbsyncAllTests(LibraryTablesFixture):
    def test_no_orphans_is_a_clean_no_op(self):
        self._insert_album(1)
        self._insert_item(1, 1, "artist/album/track1.mp3")
        with mock.patch.object(app_module, "beets_client") as mock_client, \
             mock.patch("subprocess.Popen", return_value=_fake_popen()):
            log = self._run(app_module.library_mbsync_all, "/api/library/mbsync-all")
        mock_client.delete_album.assert_not_called()
        # The original code's NameError-on-empty-orphans bug must not
        # reappear -- no warning about it in the log.
        self.assertFalse(any("Orphan lookup failed" in line for line in log))
        self.assertFalse(any("Pruned" in line for line in log))

    def test_prunes_orphaned_album_via_engine_not_raw_sql(self):
        self._insert_album(1)
        self._insert_item(1, 1, "artist/album/track1.mp3")
        self._insert_album(2)  # orphaned: no items reference album_id=2
        with mock.patch.object(
            app_module.beets_client, "delete_album", return_value={"ok": True},
        ) as mock_delete, mock.patch("subprocess.Popen", return_value=_fake_popen()):
            log = self._run(app_module.library_mbsync_all, "/api/library/mbsync-all")
        mock_delete.assert_called_once_with(2, delete_files=True)
        self.assertTrue(any("Pruned 1/1" in line for line in log))

    def test_engine_rejection_for_one_orphan_does_not_block_the_other(self):
        self._insert_album(2)
        self._insert_album(3)

        def fake_delete(album_id, delete_files=True):
            if album_id == 2:
                return {"ok": False, "error": "boom"}
            return {"ok": True}

        with mock.patch.object(
            app_module.beets_client, "delete_album", side_effect=fake_delete,
        ) as mock_delete, mock.patch("subprocess.Popen", return_value=_fake_popen()):
            log = self._run(app_module.library_mbsync_all, "/api/library/mbsync-all")
        self.assertEqual(mock_delete.call_count, 2)
        self.assertTrue(any("Could not prune orphaned album 2" in line for line in log))
        self.assertTrue(any("Pruned 1/2" in line for line in log))


class LibraryMoveAllTests(LibraryTablesFixture):
    def test_candidate_dirs_derived_from_db_then_cleaned_via_engine(self):
        self._insert_album(1)
        self._insert_item(1, 1, "ArtistA/AlbumA/track1.mp3")
        self._insert_item(2, 1, "ArtistA/AlbumA/track2.mp3")

        with mock.patch.object(
            app_module.beets_client, "plan_folder_cleanup",
            return_value={"ok": True, "operation_id": "op-1"},
        ) as mock_plan, mock.patch.object(
            app_module.beets_client, "apply_folder_cleanup",
            return_value={"ok": True},
        ) as mock_apply, mock.patch("subprocess.Popen", return_value=_fake_popen()):
            log = self._run(app_module.library_move_all, "/api/library/move-all")

        # Both the album dir and its ancestor (ArtistA) must have been
        # considered as candidates.
        planned_bodies = [c.args[0] if c.args else c.kwargs for c in mock_plan.call_args_list]
        planned_sources_from_bodies = {b.get("source") for b in planned_bodies if isinstance(b, dict)}
        self.assertIn(str(app_module.MUSIC_ROOT / "ArtistA" / "AlbumA"), planned_sources_from_bodies)
        self.assertIn(str(app_module.MUSIC_ROOT / "ArtistA"), planned_sources_from_bodies)
        self.assertTrue(mock_apply.called)
        self.assertTrue(any("Removed empty folder" in line for line in log))

    def test_expected_rejection_codes_are_silently_skipped(self):
        self._insert_album(1)
        self._insert_item(1, 1, "ArtistA/AlbumA/track1.mp3")
        with mock.patch.object(
            app_module.beets_client, "plan_folder_cleanup",
            return_value={"ok": False, "code": "folder_cleanup_not_empty", "error": "Directory is not empty"},
        ), mock.patch.object(
            app_module.beets_client, "apply_folder_cleanup",
        ) as mock_apply, mock.patch("subprocess.Popen", return_value=_fake_popen()):
            log = self._run(app_module.library_move_all, "/api/library/move-all")
        mock_apply.assert_not_called()
        self.assertFalse(any("Folder cleanup plan rejected" in line for line in log))
        self.assertFalse(any("Removed empty folder" in line for line in log))

    def test_unexpected_rejection_code_is_logged(self):
        self._insert_album(1)
        self._insert_item(1, 1, "ArtistA/AlbumA/track1.mp3")
        with mock.patch.object(
            app_module.beets_client, "plan_folder_cleanup",
            return_value={"ok": False, "code": "folder_cleanup_symlink_rejected", "error": "Symlink rejected"},
        ), mock.patch("subprocess.Popen", return_value=_fake_popen()):
            log = self._run(app_module.library_move_all, "/api/library/move-all")
        self.assertTrue(any("Folder cleanup plan rejected" in line for line in log))

    def test_no_local_filesystem_walk_or_rmdir_remains(self):
        """Structural regression (AST-based, not a text/comment match): the
        architecture-violation half of this migration (not just the SQL/
        mutation-sink half) -- no real call to os.walk(...) or
        <path>.rmdir(...) survives in the route's code."""
        import inspect
        source = inspect.getsource(app_module.library_move_all)
        tree = ast.parse(source)
        offending = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "rmdir":
                offending.append("rmdir")
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "walk"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            ):
                offending.append("os.walk")
        self.assertEqual(offending, [])


if __name__ == "__main__":
    unittest.main()
