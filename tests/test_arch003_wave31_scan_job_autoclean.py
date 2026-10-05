"""SEC-002 / ARCH-003 Wave 31 & ARCH-007: _do_scan_job()'s auto-clean-stale-DB-entries
step migrated from raw SQLite queries onto structured BeetsClient methods:
- get_library_stats()
- sync_deleted_files()
- clean_empty_albums()
"""

import unittest
from unittest import mock

import app as app_module
from backend.beets_adapter import BeetsError, BeetsUnavailableError


class ScanJobAutoCleanTests(unittest.TestCase):
    def setUp(self):
        self._thread_patch = mock.patch.object(app_module.threading, "Thread")
        self._thread_patch.start()

    def tearDown(self):
        self._thread_patch.stop()

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

    def test_scan_job_only_reports_missing_rows(self):
        """LT-2 (Wave 0): the background scan previews missing rows and never
        removes rows or album records; removal is an operator action."""
        with mock.patch.object(
            app_module.composite_workflows, "get_library_stats",
            return_value={"tracks": 100, "albums": 10},
        ) as mock_stats, mock.patch.object(
            app_module.composite_workflows, "sync_deleted_files",
            return_value={"ok": True, "missing_count": 2, "removed_from_db": 0},
        ) as mock_sync, mock.patch.object(
            app_module.composite_workflows, "clean_empty_albums",
        ) as mock_empty:
            log = self._run_scan()

        mock_stats.assert_called_once()
        mock_sync.assert_called_once_with(dry_run=True, limit=50000)
        mock_empty.assert_not_called()
        self.assertTrue(any("phase:read-db rows:100" in line for line in log))
        self.assertFalse(any("cleaned:" in line for line in log))
        self.assertTrue(any("not removed" in line for line in log))
        self.assertTrue(any("tracks:100" in line for line in log))
        self.assertTrue(any("albums:10" in line for line in log))
        self.assertTrue(any("missing:2" in line for line in log))
        self.assertTrue(any("removed:0" in line for line in log))

    def test_scan_job_no_stale_entries(self):
        with mock.patch.object(
            app_module.composite_workflows, "get_library_stats",
            return_value={"tracks": 50, "albums": 5},
        ), mock.patch.object(
            app_module.composite_workflows, "sync_deleted_files",
            return_value={"missing_count": 0, "removed_from_db": 0},
        ), mock.patch.object(
            app_module.composite_workflows, "clean_empty_albums",
            return_value={"removed_count": 0},
        ):
            log = self._run_scan()

        self.assertTrue(any("phase:read-db rows:50" in line for line in log))
        self.assertFalse(any("cleaned:" in line for line in log))
        self.assertTrue(any("tracks:50" in line for line in log))
        self.assertTrue(any("albums:5" in line for line in log))
        self.assertTrue(any("missing:0" in line for line in log))
        self.assertTrue(any("removed:0" in line for line in log))

    def test_engine_unavailable_is_logged_and_raises(self):
        with mock.patch.object(
            app_module.composite_workflows, "get_library_stats",
            side_effect=BeetsUnavailableError("offline"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self._run_scan()
            self.assertIn("Library scan failed", str(ctx.exception))

    def test_engine_error_during_sync_is_logged_and_raises(self):
        with mock.patch.object(
            app_module.composite_workflows, "get_library_stats",
            return_value={"tracks": 50, "albums": 5},
        ), mock.patch.object(
            app_module.composite_workflows, "sync_deleted_files",
            side_effect=BeetsError("sync failure"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self._run_scan()
            self.assertIn("Library scan failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

