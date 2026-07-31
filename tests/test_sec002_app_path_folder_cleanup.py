"""Regression tests for the SEC-002 app.py path-injection wave 1: the
folder-cleanup/rename/merge maintenance route family
(/api/clean/folder-placeholder/*), covering 32 open py/path-injection
alerts. See docs/TECHNICAL_DEBT.md SEC-002 for the full alert inventory
and per-alert classification.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module


class FolderCleanupPathContainmentTests(unittest.TestCase):
    """Baseline containment behavior of _folder_cleanup_path(), already
    proven safe in the wave-3 stack-trace-exposure sweep; re-verified
    here as the foundation the rest of this cluster depends on."""

    def test_outside_root_rejected(self):
        with mock.patch.object(app_module, "MUSIC_ROOT", Path(tempfile.gettempdir()) / "music-only"):
            path, error = app_module._folder_cleanup_path("/etc")
        self.assertIsNone(path)
        self.assertEqual(error, "Path is outside the configured music library")

    def test_relative_path_resolved_against_music_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            music_root = Path(tmp)
            (music_root / "Artist").mkdir()
            with mock.patch.object(app_module, "MUSIC_ROOT", music_root):
                path, error = app_module._folder_cleanup_path("Artist")
            self.assertIsNone(error)
            self.assertEqual(path, (music_root / "Artist").resolve())

    def test_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            music_root = Path(tmp) / "music"
            outside = Path(tmp) / "outside"
            music_root.mkdir()
            outside.mkdir()
            link = music_root / "escape"
            link.symlink_to(outside, target_is_directory=True)
            with mock.patch.object(app_module, "MUSIC_ROOT", music_root):
                path, error = app_module._folder_cleanup_path(str(link))
            self.assertIsNone(path)
            self.assertEqual(error, "Path is outside the configured music library")


class ApprovedRootSelfRefusalTests(unittest.TestCase):
    """_path_under()/relative_to() treat MUSIC_ROOT as "under" itself
    (relative_to returns '.'), so before this fix every destructive
    folder-cleanup action (remove_empty_source, safe_rename,
    merge_source_files) would accept the music library root itself as a
    valid source -- _folder_cleanup_is_approved_root() closes this."""

    def test_path_under_treats_root_as_under_itself(self):
        # Documents the underlying library behavior this fix must guard
        # against, so the guard's necessity stays visible if that
        # semantic ever appears to change.
        with mock.patch.object(app_module, "MUSIC_ROOT", Path("/data/media/music")):
            self.assertTrue(app_module._path_under(Path("/data/media/music"), app_module.MUSIC_ROOT))

    def test_is_approved_root_true_for_root_itself(self):
        with mock.patch.object(app_module, "MUSIC_ROOT", Path("/data/media/music")):
            self.assertTrue(app_module._folder_cleanup_is_approved_root(Path("/data/media/music")))

    def test_is_approved_root_false_for_subfolder(self):
        with mock.patch.object(app_module, "MUSIC_ROOT", Path("/data/media/music")):
            self.assertFalse(app_module._folder_cleanup_is_approved_root(Path("/data/media/music/Artist")))

    def _post_apply(self, payload):
        with app_module.app.test_request_context(
            "/api/clean/folder-placeholder/apply", method="POST",
            data=json.dumps(payload), content_type="application/json",
        ):
            return app_module.apply_folder_placeholder_action_api()

    def test_remove_empty_source_refuses_the_root_itself(self):
        with mock.patch.object(app_module, "MUSIC_ROOT", Path("/data/media/music")), \
             mock.patch.object(app_module, "_folder_cleanup_path", return_value=(Path("/data/media/music"), None)), \
             mock.patch.object(Path, "rmdir") as mock_rmdir:
            response = self._post_apply({
                "action": "remove_empty_source", "confirmed": True,
                "source_path": "/data/media/music", "preview_token": "x",
            })
        data = response[0].get_json()
        self.assertEqual(response[1], 400)
        self.assertIn("root itself", data["error"])
        mock_rmdir.assert_not_called()

    def test_safe_rename_refuses_the_root_itself_as_source(self):
        with mock.patch.object(app_module, "MUSIC_ROOT", Path("/data/media/music")), \
             mock.patch.object(app_module, "_folder_cleanup_path", return_value=(Path("/data/media/music"), None)), \
             mock.patch.object(Path, "rename") as mock_rename:
            response = self._post_apply({
                "action": "safe_rename", "confirmed": True,
                "source_path": "/data/media/music", "target_path": "/data/media/moved-away",
            })
        data = response[0].get_json()
        self.assertEqual(response[1], 400)
        self.assertIn("root itself", data["error"])
        mock_rename.assert_not_called()

    def test_merge_source_files_refuses_the_root_itself_as_source(self):
        with mock.patch.object(app_module, "MUSIC_ROOT", Path("/data/media/music")), \
             mock.patch.object(app_module, "_folder_cleanup_path", return_value=(Path("/data/media/music"), None)), \
             mock.patch.object(app_module, "_folder_cleanup_merge_preview") as mock_preview:
            response = self._post_apply({
                "action": "merge_source_files", "confirmed": True,
                "source_path": "/data/media/music", "target_path": "/data/media/other",
                "preview_token": "x",
            })
        data = response[0].get_json()
        self.assertEqual(response[1], 400)
        self.assertIn("root itself", data["error"])
        mock_preview.assert_not_called()

    def test_mark_reviewed_is_not_blocked_by_root_refusal(self):
        # mark_reviewed/skip are non-destructive no-ops; the root-refusal
        # guard must not accidentally block them.
        with mock.patch.object(app_module, "MUSIC_ROOT", Path("/data/media/music")), \
             mock.patch.object(app_module, "_folder_cleanup_path", return_value=(Path("/data/media/music"), None)):
            response = self._post_apply({
                "action": "mark_reviewed", "confirmed": True,
                "source_path": "/data/media/music",
            })
        data = response.get_json()
        self.assertTrue(data["ok"])


class SafeRenamesJobRootAndDestinationContainmentTests(unittest.TestCase):
    """apply_safe_folder_placeholder_renames_job(): the request-supplied
    source_paths branch computed a destination (src.parent / clean_name)
    with no containment re-check -- safe for an ordinary subfolder (whose
    parent is still under MUSIC_ROOT), but if src were ever the root
    itself, src.parent falls outside MUSIC_ROOT entirely with nothing to
    stop the rename. Fixed with an explicit root refusal plus a
    destination containment check immediately before every rename()."""

    def _run_job_body(self, source_paths):
        payload = {"source_paths": source_paths}
        with app_module.app.test_request_context(
            "/api/clean/folder-placeholder/apply-safe-renames", method="POST",
            data=json.dumps(payload), content_type="application/json",
        ):
            app_module.request.get_json(silent=True)
            captured = {}

            def fake_start_python(fn, label=None, metadata=None):
                log = []
                fn(log, cancel_event=None, update_state=lambda *_a, **_k: None)
                captured["log"] = log
                return mock.Mock(job_id="job-test")

            with mock.patch.object(app_module.jobs, "start_python", side_effect=fake_start_python):
                app_module.request.json  # noqa: B018 - force context binding parity with the real route
                # Re-invoke the actual route function so its closure captures
                # this request context's payload.
                response = app_module.apply_safe_folder_placeholder_renames_job()
            return response, captured.get("log", [])

    def test_root_itself_is_skipped_not_renamed(self):
        with mock.patch.object(app_module, "MUSIC_ROOT", Path("/data/media/music")), \
             mock.patch.object(
                 app_module, "_folder_cleanup_path",
                 return_value=(Path("/data/media/music"), None),
             ), \
             mock.patch.object(Path, "rename") as mock_rename:
            _response, log = self._run_job_body(["/data/media/music"])
        self.assertTrue(any("refusing to rename the music library root" in line for line in log))
        mock_rename.assert_not_called()

    def test_destination_outside_root_is_skipped(self):
        # Simulate a row that (however it was constructed) points outside
        # MUSIC_ROOT, proving the pre-rename containment check is a real
        # second gate, not just a construction-time assumption.
        with mock.patch.object(app_module, "MUSIC_ROOT", Path("/data/media/music")), \
             mock.patch.object(app_module, "_folder_cleanup_path", return_value=None), \
             mock.patch.object(
                 app_module, "_scan_folder_name_placeholders",
                 return_value=[{
                     "safe": True, "target_exists": False, "db_item_count": 0,
                     "folder": "/data/media/music/Weird [Placeholder]",
                     "proposed_folder": "/etc/passwd",
                 }],
             ), \
             mock.patch.object(Path, "rename") as mock_rename:
            _response, log = self._run_job_body(None)
        self.assertTrue(any("path outside music library" in line for line in log))
        mock_rename.assert_not_called()


if __name__ == "__main__":
    unittest.main()
