"""SEC-002 / ARCH-003 Wave 33 & Milestone 2: library_mbsync_all() and library_move_all().

Milestone 2 completely eliminated local BEET_BIN / subprocess.Popen execution from
both library_mbsync_all() and library_move_all(), routing them through engine-owned
IPC:
- library_mbsync_all() -> beets_client.mbsync()
- library_move_all() -> beets_client.move_library()

All subprocess.Popen mocks are eliminated. Tests assert calls to beets_client with
proper fail-closed, timeout, non-zero exit, and cancellation handling.
"""

import ast
import unittest
from unittest import mock

from backend.beets_client import BeetsUnavailableError
import app as app_module


class LibraryTablesFixture(unittest.TestCase):
    def setUp(self):
        self._invalidate_patch = mock.patch.object(app_module, "_invalidate_lib_cache")
        self._invalidate_patch.start()
        self._plex_patch = mock.patch.object(app_module, "_trigger_plex_refresh")
        self._plex_patch.start()

    def tearDown(self):
        self._plex_patch.stop()
        self._invalidate_patch.stop()

    def _run(self, fn, route, cancel_event=None):
        captured = {}

        def fake_start_python(fn_inner, label=None, metadata=None):
            log = []
            fn_inner(log, cancel_event=cancel_event)
            captured["log"] = log
            return mock.Mock(job_id="job-test")

        with app_module.app.test_request_context(route, method="POST"), \
             mock.patch.object(app_module.jobs, "start_python", side_effect=fake_start_python):
            fn()
        return captured.get("log", [])


class LibraryMbsyncAllTests(LibraryTablesFixture):
    def test_no_orphans_is_a_clean_no_op(self):
        with mock.patch.object(app_module, "beets_client") as mock_client:
            mock_client.find_all_orphan_albums.return_value = []
            mock_client.mbsync.return_value = {"ok": True, "success": True, "returncode": 0, "stdout": "mbsync ok"}
            log = self._run(app_module.library_mbsync_all, "/api/library/mbsync-all")

        mock_client.delete_album.assert_not_called()
        mock_client.mbsync.assert_called_once_with(query="", async_job=True)
        self.assertFalse(any("Orphan lookup failed" in line for line in log))
        self.assertFalse(any("Pruned" in line for line in log))
        self.assertTrue(any("mbsync complete" in line for line in log))

    def test_prunes_orphaned_album_via_engine_not_raw_sql(self):
        with mock.patch.object(
            app_module.beets_client, "find_all_orphan_albums",
            return_value=[{"id": 2, "albumartist": "Some Artist"}],
        ) as mock_find, mock.patch.object(
            app_module.beets_client, "delete_album", return_value={"ok": True},
        ) as mock_delete, mock.patch.object(
            app_module.beets_client, "mbsync", return_value={"ok": True, "success": True, "returncode": 0},
        ) as mock_mbsync:
            log = self._run(app_module.library_mbsync_all, "/api/library/mbsync-all")

        mock_find.assert_called_once_with()
        mock_delete.assert_called_once_with(2, delete_files=True)
        mock_mbsync.assert_called_once_with(query="", async_job=True)
        self.assertTrue(any("Pruned 1/1" in line for line in log))

    def test_engine_rejection_for_one_orphan_does_not_block_the_other(self):
        def fake_delete(album_id, delete_files=True):
            if album_id == 2:
                return {"ok": False, "error": "boom"}
            return {"ok": True}

        with mock.patch.object(
            app_module.beets_client, "find_all_orphan_albums",
            return_value=[{"id": 2, "albumartist": "A"}, {"id": 3, "albumartist": "B"}],
        ), mock.patch.object(
            app_module.beets_client, "delete_album", side_effect=fake_delete,
        ) as mock_delete, mock.patch.object(
            app_module.beets_client, "mbsync", return_value={"ok": True, "success": True, "returncode": 0},
        ) as mock_mbsync:
            log = self._run(app_module.library_mbsync_all, "/api/library/mbsync-all")

        self.assertEqual(mock_delete.call_count, 2)
        mock_mbsync.assert_called_once()
        self.assertTrue(any("Could not prune orphaned album 2" in line for line in log))
        self.assertTrue(any("Pruned 1/2" in line for line in log))

    def test_orphan_lookup_engine_unavailable_is_non_fatal_mbsync_still_runs(self):
        with mock.patch.object(
            app_module.beets_client, "find_all_orphan_albums",
            side_effect=BeetsUnavailableError("engine offline"),
        ), mock.patch.object(app_module.beets_client, "delete_album") as mock_delete, \
             mock.patch.object(app_module.beets_client, "mbsync", return_value={"ok": True, "success": True, "returncode": 0}) as mock_mbsync:
            log = self._run(app_module.library_mbsync_all, "/api/library/mbsync-all")

        mock_delete.assert_not_called()
        mock_mbsync.assert_called_once_with(query="", async_job=True)
        self.assertTrue(any("Orphan lookup failed (non-fatal)" in line for line in log))

    def test_mbsync_engine_offline_fails_closed(self):
        with mock.patch.object(app_module.beets_client, "find_all_orphan_albums", return_value=[]), \
             mock.patch.object(app_module.beets_client, "mbsync", side_effect=BeetsUnavailableError("engine offline")):
            with self.assertRaises(RuntimeError):
                self._run(app_module.library_mbsync_all, "/api/library/mbsync-all")

    def test_mbsync_nonzero_returncode_raises(self):
        with mock.patch.object(app_module.beets_client, "find_all_orphan_albums", return_value=[]), \
             mock.patch.object(app_module.beets_client, "mbsync", return_value={"ok": False, "success": False, "returncode": 2, "stderr": "fatal error"}):
            with self.assertRaises(RuntimeError):
                self._run(app_module.library_mbsync_all, "/api/library/mbsync-all")

    def test_no_subprocess_popen_in_library_mbsync_all(self):
        import inspect
        source = inspect.getsource(app_module.library_mbsync_all)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in ("BEET_BIN", "_sp", "subprocess"):
                self.fail(f"Found prohibited symbol '{node.id}' in library_mbsync_all")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "Popen":
                self.fail("Found prohibited Popen call in library_mbsync_all")


class LibraryMoveAllTests(LibraryTablesFixture):
    def test_candidate_dirs_derived_from_engine_then_cleaned_via_engine(self):
        with mock.patch.object(
            app_module.beets_client, "list_distinct_item_paths",
            return_value=["ArtistA/AlbumA/track1.mp3", "ArtistA/AlbumA/track2.mp3"],
        ) as mock_paths, mock.patch.object(
            app_module.beets_client, "move_library",
            return_value={"ok": True, "success": True, "returncode": 0, "updated": True, "moved": True, "stdout": "moved 2"},
        ) as mock_move, mock.patch.object(
            app_module.beets_client, "plan_folder_cleanup",
            return_value={"ok": True, "operation_id": "op-1"},
        ) as mock_plan, mock.patch.object(
            app_module.beets_client, "apply_folder_cleanup",
            return_value={"ok": True},
        ) as mock_apply:
            log = self._run(app_module.library_move_all, "/api/library/move-all")

        mock_paths.assert_called_once_with()
        mock_move.assert_called_once_with(query="", rescan_first=True, async_job=True, timeout=5400.0)
        self.assertTrue(mock_apply.called)
        self.assertTrue(any("Removed empty folder" in line for line in log))
        self.assertTrue(any("Pre-move scan: 2 distinct item path(s)" in line for line in log))

    def test_path_scan_engine_unavailable_is_non_fatal_move_still_runs(self):
        with mock.patch.object(
            app_module.beets_client, "list_distinct_item_paths",
            side_effect=BeetsUnavailableError("engine offline"),
        ), mock.patch.object(
            app_module.beets_client, "move_library",
            return_value={"ok": True, "success": True, "returncode": 0, "updated": True, "moved": True},
        ) as mock_move, mock.patch.object(
            app_module.beets_client, "plan_folder_cleanup",
        ) as mock_plan:
            log = self._run(app_module.library_move_all, "/api/library/move-all")

        mock_plan.assert_not_called()
        mock_move.assert_called_once_with(query="", rescan_first=True, async_job=True, timeout=5400.0)
        self.assertTrue(any("Could not enumerate pre-move directories" in line for line in log))

    def test_move_library_engine_offline_fails_closed(self):
        with mock.patch.object(app_module.beets_client, "list_distinct_item_paths", return_value=[]), \
             mock.patch.object(app_module.beets_client, "move_library", side_effect=BeetsUnavailableError("engine offline")):
            with self.assertRaises(RuntimeError):
                self._run(app_module.library_move_all, "/api/library/move-all")

    def test_move_library_rescan_failure_aborts_cleanup(self):
        with mock.patch.object(app_module.beets_client, "list_distinct_item_paths", return_value=["Artist/Album/track.mp3"]), \
             mock.patch.object(app_module.beets_client, "move_library", return_value={"ok": False, "success": False, "returncode": 1, "error": "update failed"}), \
             mock.patch.object(app_module.beets_client, "plan_folder_cleanup") as mock_plan:
            with self.assertRaises(RuntimeError):
                self._run(app_module.library_move_all, "/api/library/move-all")
        mock_plan.assert_not_called()

    def test_no_local_filesystem_walk_or_rmdir_remains(self):
        import inspect
        source = inspect.getsource(app_module.library_move_all)
        tree = ast.parse(source)
        offending = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in ("rmdir", "Popen"):
                offending.append(func.attr)
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
