"""Regression tests for the SEC-002 main-branch CodeQL backlog remediation
(critical + newest-batch alerts: py/full-ssrf #18, py/path-injection
#327/#328/#331/#334/#335, py/clear-text-storage-sensitive-data #332,
py/stack-trace-exposure #401). See docs/TECHNICAL_DEBT.md SEC-002 for the
full alert inventory and per-alert classification.
"""

import json
import os
import stat
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import app as app_module


class AiBatchStateFileContainmentTests(unittest.TestCase):
    """Alerts #327/#328 (py/path-injection, app.py os.replace sink):
    _ai_batch_state_file() reduces batch_job_id to a bounded safe character
    class before it is ever joined onto _AI_BATCH_STATE_DIR, so no input can
    produce a path outside that directory."""

    def test_adversarial_batch_job_ids_stay_inside_state_dir(self):
        adversarial_ids = [
            "../../etc/passwd",
            "..\\..\\windows\\system32",
            "/etc/passwd",
            "..",
            "a/b/../../c",
            "id\x00.json",
            "id:with:colons",
        ]
        for raw in adversarial_ids:
            with self.subTest(raw=raw):
                path = app_module._ai_batch_state_file(raw)
                self.assertEqual(path.parent.resolve(), app_module._AI_BATCH_STATE_DIR.resolve())
                self.assertNotIn("/", path.name.replace(".json", ""))
                self.assertNotIn("\\", path.name)


class PluginJobConfigCommandAllowlistTests(unittest.TestCase):
    """Alert #331 (py/path-injection, app.py write_text sink):
    _write_plugin_job_beets_config()'s temp_path is only ever built from a
    command string drawn from the fixed _PLUGIN_CMDS allowlist (checked with
    exact membership before the call), so no request value can reach the
    write_text() sink unvalidated."""

    def test_plugin_cmds_allowlist_has_no_path_metacharacters(self):
        for cmd in app_module._PLUGIN_CMDS:
            self.assertNotIn("/", cmd)
            self.assertNotIn("\\", cmd)
            self.assertNotIn("..", cmd)

    def test_api_plugins_run_rejects_commands_outside_the_allowlist(self):
        with app_module.app.test_request_context(
            "/api/plugins/run", method="POST",
            data=json.dumps({"args": ["../../etc/passwd"]}),
            content_type="application/json",
        ):
            response = app_module.api_plugins_run()
        payload = response.get_json() if hasattr(response, "get_json") else response[0].get_json()
        status = response.status_code if hasattr(response, "status_code") else response[1]
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])


class JobBeetsConfigSecretPermissionTests(unittest.TestCase):
    """Alert #332 (py/clear-text-storage-sensitive-data, app.py write_text
    sink): _acoustid_key() (an environment-sourced API key) can flow into
    _write_job_beets_config()'s extra block. The generated temp config must
    not be left world-readable."""

    def test_generated_job_config_is_owner_only_permissions(self):
        tmp_dir = tempfile.mkdtemp(prefix="sec002-jobcfg-")
        try:
            target = os.path.join(tmp_dir, f"beets_acoustid_submit_{uuid.uuid4().hex}.yaml")
            result_path = app_module._write_job_beets_config(
                target, 'chroma:\n  auto: no\n  apikey: "super-secret-key"\n'
            )
            self.assertEqual(result_path, target)
            self.assertTrue(os.path.exists(target))
            if os.name != "nt":
                mode = stat.S_IMODE(os.stat(target).st_mode)
                self.assertEqual(mode, 0o600)
            contents = Path(target).read_text(encoding="utf-8")
            self.assertIn("super-secret-key", contents)
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)


class FolderImportTrackCountRootContainmentTests(unittest.TestCase):
    """Alerts #334/#335 (py/path-injection, app.py Path.is_dir/rglob sinks):
    _folder_import_track_count() must not stat or walk a folder outside the
    approved music/downloads roots, even though source_folder is reachable
    (through several layers of import-matching helpers) from request data."""

    def test_outside_root_folder_returns_zero_without_walking_it(self):
        outside = tempfile.mkdtemp(prefix="sec002-outside-root-")
        try:
            Path(outside, "track.flac").write_text("x", encoding="utf-8")
            with mock.patch.object(Path, "rglob") as mock_rglob:
                count = app_module._folder_import_track_count(outside)
            self.assertEqual(count, 0)
            mock_rglob.assert_not_called()
        finally:
            import shutil
            shutil.rmtree(outside, ignore_errors=True)

    def test_folder_under_music_root_is_still_counted(self):
        with mock.patch.object(app_module, "MUSIC_ROOT", Path(tempfile.gettempdir())):
            album_dir = Path(tempfile.mkdtemp(dir=tempfile.gettempdir(), prefix="sec002-inroot-"))
            try:
                (album_dir / "01.flac").write_text("x", encoding="utf-8")
                (album_dir / "02.mp3").write_text("x", encoding="utf-8")
                (album_dir / "notes.txt").write_text("x", encoding="utf-8")
                count = app_module._folder_import_track_count(str(album_dir))
                self.assertEqual(count, 2)
            finally:
                import shutil
                shutil.rmtree(album_dir, ignore_errors=True)


class FolderTrackSearchTitlesRootContainmentTests(unittest.TestCase):
    """Repository-wide CodeQL closure session, 2026-09-01 (alerts #8641/#8643
    at baseline, py/path-injection, app.py Path.is_dir/rglob sinks):
    _folder_track_search_titles() had no containment check on source_folder
    at all -- a real gap in the exact same pattern as
    FolderImportTrackCountRootContainmentTests above (#334/#335, Wave 1),
    just in a sibling function Wave 1 didn't reach. Same fix reused."""

    def test_outside_root_folder_returns_db_titles_only_without_walking_it(self):
        outside = tempfile.mkdtemp(prefix="sec002-outside-root-")
        try:
            Path(outside, "track.flac").write_text("x", encoding="utf-8")
            with mock.patch.object(Path, "rglob") as mock_rglob:
                titles = app_module._folder_track_search_titles(outside)
            mock_rglob.assert_not_called()
            self.assertEqual(titles, [])
        finally:
            import shutil
            shutil.rmtree(outside, ignore_errors=True)

    def test_folder_under_music_root_is_still_scanned(self):
        with mock.patch.object(app_module, "MUSIC_ROOT", Path(tempfile.gettempdir())):
            album_dir = Path(tempfile.mkdtemp(dir=tempfile.gettempdir(), prefix="sec002-inroot-"))
            try:
                (album_dir / "01 - Opening Track.flac").write_text("x", encoding="utf-8")
                titles = app_module._folder_track_search_titles(str(album_dir))
                self.assertTrue(titles)
            finally:
                import shutil
                shutil.rmtree(album_dir, ignore_errors=True)


class AlbumArtDeleteExceptionSanitizationTests(unittest.TestCase):
    """Alert #401 (py/stack-trace-exposure): album_delete_art() must not
    return raw filesystem or engine exception text in public JSON errors."""

    def test_delete_failure_does_not_leak_exception_text(self):
        leak = "LEAK_MARKER /internal/host/path errno=13 token=abc123"
        with app_module.app.test_request_context("/api/albums/42/art", method="DELETE"), \
             mock.patch.object(app_module.lib, "get_album", return_value=object()), \
             mock.patch.object(app_module.beets_client, "delete_album_art", side_effect=RuntimeError(leak)):
            response, status = app_module.album_delete_art(42)
        data = response.get_json()
        self.assertEqual(status, 500)
        self.assertEqual(data["error"], "Could not delete album artwork.")
        self.assertNotIn("LEAK_MARKER", json.dumps(data))
        self.assertNotIn("token=abc123", json.dumps(data))
        self.assertNotIn("/internal/host/path", json.dumps(data))


if __name__ == "__main__":
    unittest.main()
