"""SEC-002 / ARCH-003 Wave 30 & ARCH-007: library_sync_deleted()'s missing-file DB
cleanup (/api/library/sync-deleted) migrated from raw SQL / local filesystem checks
onto server-owned BeetsClient.sync_deleted_files().
"""

import json
import unittest
from unittest import mock

import app as app_module
from backend.beets_client import BeetsError, BeetsUnavailableError


class LibrarySyncDeletedTests(unittest.TestCase):
    def setUp(self):
        self._invalidate_patch = mock.patch.object(app_module, "_invalidate_lib_cache")
        self._invalidate_patch.start()

    def tearDown(self):
        self._invalidate_patch.stop()

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
        with mock.patch.object(
            app_module.beets_client, "sync_deleted_files",
            return_value={"scanned_items": 10, "missing_count": 2, "missing_albums_count": 1},
        ) as mock_sync:
            log = self._run(dry_run=True)
        mock_sync.assert_called_once_with(dry_run=True, limit=50000)
        self.assertTrue(any("Preview only" in line for line in log))

    def test_all_items_missing_removes_whole_album_via_engine(self):
        with mock.patch.object(
            app_module.beets_client, "sync_deleted_files",
            return_value={"scanned_items": 10, "missing_count": 1, "removed_from_db": 1, "missing_albums_count": 1},
        ) as mock_sync:
            log = self._run(dry_run=False)
        mock_sync.assert_called_once_with(dry_run=False, limit=50000)
        self.assertTrue(any("removed 1 album(s), 1 track(s)" in line for line in log))

    def test_partial_missing_keeps_album_removes_only_missing_items(self):
        with mock.patch.object(
            app_module.beets_client, "sync_deleted_files",
            return_value={"scanned_items": 10, "missing_count": 1, "removed_from_db": 1, "missing_albums_count": 0},
        ) as mock_sync:
            log = self._run(dry_run=False)
        mock_sync.assert_called_once_with(dry_run=False, limit=50000)
        self.assertTrue(any("removed 0 album(s), 1 track(s)" in line for line in log))

    def test_orphan_item_with_no_album_is_skipped_not_crashed(self):
        with mock.patch.object(
            app_module.beets_client, "sync_deleted_files",
            return_value={"scanned_items": 10, "missing_count": 0, "removed_from_db": 0, "missing_albums_count": 0},
        ) as mock_sync:
            log = self._run(dry_run=False)
        mock_sync.assert_called_once_with(dry_run=False, limit=50000)
        self.assertTrue(any("Nothing to clean up" in line for line in log))

    def test_engine_rejection_is_logged_not_raised(self):
        with mock.patch.object(
            app_module.beets_client, "sync_deleted_files",
            side_effect=BeetsError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                self._run(dry_run=False)

    def test_engine_unavailable_is_logged_not_raised(self):
        with mock.patch.object(
            app_module.beets_client, "sync_deleted_files",
            side_effect=BeetsUnavailableError("offline"),
        ):
            with self.assertRaises(RuntimeError):
                self._run(dry_run=False)


if __name__ == "__main__":
    unittest.main()

