"""SEC-002 / ARCH-003 Wave 31: fix_leaked_db_paths()'s DB-only repoint
migrated from raw UPDATE items SET path=? WHERE id=? SQL onto a new,
validated album_maintenance_v1 primitive (deduplicate mode's fix_updates
"repoint_db" payload via BeetsClient.repoint_item_db_path()). Selection
(the leaked-template-token scan) stays a local, non-mutating read; the
actual DB path correction goes through the engine, which now validates
root containment, rejects symlinks, requires the new path to already
exist as a real file, and TOCTOU-revalidates it immediately before
writing (previously entirely unvalidated -- see
backend/transaction_engine.py's create_album_maintenance_plan for the
security fix this migration depends on).
"""

import json
import unittest
from unittest import mock

import app as app_module
from backend.beets_client import BeetsError, BeetsUnavailableError


def _scan_row(item_id, album_id, db_path, resolved_path, safe=True, skip_reason=""):
    return {
        "item_id": item_id,
        "album_id": album_id,
        "db_path": db_path,
        "abs_path": f"/data/media/music/{db_path}",
        "resolved_path": resolved_path,
        "file_exists_at_db_path": False,
        "file_exists_at_resolved": safe,
        "safe": safe,
        "skip_reason": skip_reason,
    }


class FixLeakedDbPathsTests(unittest.TestCase):
    def _run(self, payload):
        with app_module.app.test_request_context(
            "/api/library/leaked-db-paths/fix", method="POST",
            data=json.dumps(payload), content_type="application/json",
        ):
            captured = {}

            def fake_start_python(fn, label=None, metadata=None):
                log = []
                fn(log, cancel_event=None, update_state=lambda *_a, **_k: None)
                captured["log"] = log
                return mock.Mock(job_id="job-test")

            with mock.patch.object(app_module.jobs, "start_python", side_effect=fake_start_python):
                app_module.fix_leaked_db_paths()
            return captured.get("log", [])

    def test_dry_run_never_calls_engine(self):
        row = _scan_row(1, 10, "Artist/%the{}/track.mp3", "/data/media/music/Artist/track.mp3")
        with mock.patch.object(app_module, "_scan_leaked_db_paths", return_value=[row]), \
             mock.patch.object(app_module.beets_client, "repoint_item_db_path") as mock_repoint:
            log = self._run({"dry_run": True})
        mock_repoint.assert_not_called()
        self.assertTrue(any("[dry-run]" in line for line in log))

    def test_real_run_calls_engine_with_raw_old_path_and_computed_new_path(self):
        # _db_path_value()'s forward-slash-prefix string matching against
        # MUSIC_ROOT is Linux-path-shaped (production only ever runs on
        # Linux); mock it directly here so this test verifies THIS
        # migration's wiring (raw old_path passed through unchanged, the
        # computed new_path forwarded) without depending on how Path
        # renders separators on the platform running the test suite.
        row = _scan_row(1, 10, "Artist/%the{}/track.mp3", "/data/media/music/Artist/track.mp3")
        with mock.patch.object(app_module, "_scan_leaked_db_paths", return_value=[row]), \
             mock.patch.object(app_module, "_db_path_value", return_value="Artist/track.mp3") as mock_db_path_value, \
             mock.patch.object(
                 app_module.beets_client, "repoint_item_db_path",
                 return_value={"ok": True, "repointed": True},
             ) as mock_repoint, \
             mock.patch.object(app_module, "_invalidate_lib_cache"):
            log = self._run({"dry_run": False, "confirmed": True})
        mock_db_path_value.assert_called_once()
        mock_repoint.assert_called_once_with(1, 10, "Artist/%the{}/track.mp3", "Artist/track.mp3")
        self.assertTrue(any("Fixed item 1" in line for line in log))

    def test_item_with_no_album_id_is_skipped_not_crashed(self):
        row = _scan_row(2, 0, "orphan/%the{}/track.mp3", "/data/media/music/orphan/track.mp3")
        with mock.patch.object(app_module, "_scan_leaked_db_paths", return_value=[row]), \
             mock.patch.object(app_module, "MUSIC_ROOT", app_module.Path("/data/media/music")), \
             mock.patch.object(app_module.beets_client, "repoint_item_db_path") as mock_repoint:
            log = self._run({"dry_run": False, "confirmed": True})
        mock_repoint.assert_not_called()
        self.assertTrue(any("has no album_id" in line for line in log))

    def test_engine_rejection_is_logged_not_raised(self):
        row = _scan_row(1, 10, "Artist/%the{}/track.mp3", "/data/media/music/Artist/track.mp3")
        with mock.patch.object(app_module, "_scan_leaked_db_paths", return_value=[row]), \
             mock.patch.object(app_module, "MUSIC_ROOT", app_module.Path("/data/media/music")), \
             mock.patch.object(
                 app_module.beets_client, "repoint_item_db_path",
                 return_value={"ok": False, "error": "boom"},
             ):
            log = self._run({"dry_run": False, "confirmed": True})
        self.assertTrue(any("ERROR item 1: boom" in line for line in log))

    def test_engine_unavailable_is_logged_not_raised(self):
        row = _scan_row(1, 10, "Artist/%the{}/track.mp3", "/data/media/music/Artist/track.mp3")
        with mock.patch.object(app_module, "_scan_leaked_db_paths", return_value=[row]), \
             mock.patch.object(app_module, "MUSIC_ROOT", app_module.Path("/data/media/music")), \
             mock.patch.object(
                 app_module.beets_client, "repoint_item_db_path",
                 side_effect=BeetsUnavailableError("offline"),
             ):
            log = self._run({"dry_run": False, "confirmed": True})
        self.assertTrue(any("engine unavailable" in line for line in log))

    def test_confirmation_required_before_real_apply(self):
        with app_module.app.test_request_context(
            "/api/library/leaked-db-paths/fix", method="POST",
            data=json.dumps({"dry_run": False}), content_type="application/json",
        ):
            response = app_module.fix_leaked_db_paths()
        body, status = response
        self.assertEqual(status, 400)
        self.assertFalse(body.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
