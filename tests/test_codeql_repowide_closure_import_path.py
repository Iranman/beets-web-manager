"""Regression coverage for a real vulnerability found during the
repository-wide CodeQL closure pass (CodeQL alerts 231-236, py/path-injection,
app.py lines 28811/28814/28863/28866/28873/28924 at baseline commit
7b05844d58d657ce6f1ae6c5b1724f5ba70ee257).

Before this fix, /api/import and /api/import/preflight ran
Path(path).mkdir(parents=True, exist_ok=True), Path(path).exists() and
os.walk(path) against the raw JSON request body value with *no* containment
check at all -- an authenticated caller could point "path" at any absolute
filesystem path the container process can reach:

  * /api/import would create arbitrary directories outside the configured
    torrent-source/library roots (mkdir(parents=True, exist_ok=True)).
  * /api/import/preflight would walk and report on the contents of any
    readable directory on the host/container filesystem (os.walk),
    disclosing directory names and audio-file counts outside the intended
    scope.

The engine-side reimport_source_atomic() validation (backend/beets_control_agent.py)
is a separate, later trust boundary and does not protect these earlier,
web-manager-side filesystem probes. See docs/security/codeql_repository_closure.md.

The same class of bug (alert 241, py/path-injection, app.py:29237 at
baseline) existed in /api/dedup/scan: Path(path).exists() and
scan_path.rglob("*") ran against the raw request body value with no
containment check, enumerating audio filenames (and, via later stat()
calls, file sizes) anywhere the container process could read.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module


class ImportSourcePathContainmentTests(unittest.TestCase):
    """Unit coverage of the validator itself, mirroring the established
    _folder_cleanup_path() test pattern (tests/test_sec002_app_path_folder_cleanup.py)."""

    def _roots(self, torrent_root: Path, music_root: Path):
        return mock.patch.multiple(
            app_module,
            TORRENT_SOURCE_ROOTS=(torrent_root,),
            MUSIC_ROOT=music_root,
            _IMPORT_SOURCE_ALLOWED_ROOTS=(torrent_root, music_root),
        )

    def test_outside_all_allowed_roots_rejected(self):
        # Deliberately a real absolute path outside either allowed root
        # (not a POSIX-literal like "/etc" -- pathlib.Path.is_absolute()
        # semantics for a bare "/..." string differ on Windows, and this
        # suite must be meaningful on whatever platform runs it).
        with tempfile.TemporaryDirectory() as tmp:
            torrent_root = Path(tmp) / "torrents"
            music_root = Path(tmp) / "music"
            outside = Path(tmp) / "elsewhere" / "evil"
            with self._roots(torrent_root, music_root):
                path, error = app_module._resolve_import_source_path(str(outside))
        self.assertIsNone(path)
        self.assertEqual(error, "Path is outside the allowed import source roots")

    def test_sibling_prefix_escape_rejected(self):
        # "/data/torrents/music-evil" must not be accepted merely because it
        # shares a string prefix with the allowed "/data/torrents/music" root.
        with tempfile.TemporaryDirectory() as tmp:
            torrent_root = Path(tmp) / "torrents" / "music"
            music_root = Path(tmp) / "music"
            torrent_root.parent.mkdir(parents=True)
            sibling = Path(tmp) / "torrents" / "music-evil"
            sibling.mkdir(parents=True)
            with self._roots(torrent_root, music_root):
                path, error = app_module._resolve_import_source_path(str(sibling))
        self.assertIsNone(path)
        self.assertEqual(error, "Path is outside the allowed import source roots")

    def test_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            torrent_root = Path(tmp) / "torrents"
            music_root = Path(tmp) / "music"
            outside = Path(tmp) / "outside"
            torrent_root.mkdir()
            music_root.mkdir()
            outside.mkdir()
            link = torrent_root / "escape"
            link.symlink_to(outside, target_is_directory=True)
            with self._roots(torrent_root, music_root):
                path, error = app_module._resolve_import_source_path(str(link))
        self.assertIsNone(path)
        self.assertEqual(error, "Path is outside the allowed import source roots")

    def test_relative_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            torrent_root = Path(tmp) / "torrents"
            music_root = Path(tmp) / "music"
            with self._roots(torrent_root, music_root):
                path, error = app_module._resolve_import_source_path("relative/path")
        self.assertIsNone(path)
        self.assertEqual(error, "Path must be absolute")

    def test_valid_path_under_torrent_root_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            torrent_root = Path(tmp) / "torrents"
            music_root = Path(tmp) / "music"
            torrent_root.mkdir()
            target = torrent_root / "Some Album"
            with self._roots(torrent_root, music_root):
                path, error = app_module._resolve_import_source_path(str(target))
        self.assertIsNone(error)
        self.assertEqual(path, target.resolve())

    def test_valid_path_under_music_root_accepted(self):
        # The "already organized, just untracked" reimport case sources
        # directly from within the music library root.
        with tempfile.TemporaryDirectory() as tmp:
            torrent_root = Path(tmp) / "torrents"
            music_root = Path(tmp) / "music"
            music_root.mkdir()
            target = music_root / "Artist" / "Album"
            with self._roots(torrent_root, music_root):
                path, error = app_module._resolve_import_source_path(str(target))
        self.assertIsNone(error)
        self.assertEqual(path, target.resolve())

    def test_empty_path_rejected(self):
        path, error = app_module._resolve_import_source_path("")
        self.assertIsNone(path)
        self.assertEqual(error, "Path is required")


class ImportRouteRejectsOutOfRootPathBeforeAnyFilesystemOp(unittest.TestCase):
    """Route-level proof: the dangerous mkdir()/os.walk() calls are never
    reached for an out-of-root path -- not merely "validated afterward"."""

    def _post_import(self, payload):
        with app_module.app.test_request_context(
            "/api/import", method="POST",
            data=json.dumps(payload), content_type="application/json",
        ):
            return app_module.start_import()

    def _post_preflight(self, payload):
        with app_module.app.test_request_context(
            "/api/import/preflight", method="POST",
            data=json.dumps(payload), content_type="application/json",
        ):
            return app_module.import_preflight()

    def test_start_import_rejects_outside_root_without_creating_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            torrent_root = Path(tmp) / "torrents"
            music_root = Path(tmp) / "music"
            outside_target = Path(tmp) / "outside" / "should-not-be-created"
            with mock.patch.multiple(
                app_module,
                TORRENT_SOURCE_ROOTS=(torrent_root,),
                MUSIC_ROOT=music_root,
                _IMPORT_SOURCE_ALLOWED_ROOTS=(torrent_root, music_root),
            ):
                response, status = self._post_import({"path": str(outside_target)})
            self.assertEqual(status, 400)
            self.assertEqual(
                response.get_json()["error"],
                "Path is outside the allowed import source roots",
            )
            self.assertFalse(outside_target.exists())
            self.assertFalse(outside_target.parent.exists())

    def test_preflight_rejects_outside_root_without_walking_filesystem(self):
        with tempfile.TemporaryDirectory() as tmp:
            torrent_root = Path(tmp) / "torrents"
            music_root = Path(tmp) / "music"
            outside = Path(tmp) / "elsewhere"
            with mock.patch.multiple(
                app_module,
                TORRENT_SOURCE_ROOTS=(torrent_root,),
                MUSIC_ROOT=music_root,
                _IMPORT_SOURCE_ALLOWED_ROOTS=(torrent_root, music_root),
            ), mock.patch("os.walk") as mock_walk:
                response, status = self._post_preflight({"path": str(outside)})
            mock_walk.assert_not_called()
            self.assertEqual(status, 400)
            self.assertEqual(
                response.get_json()["error"],
                "Path is outside the allowed import source roots",
            )


class DedupScanPathContainmentTests(unittest.TestCase):
    """Covers alert 241 (app.py:29237 at baseline) -- /api/dedup/scan's
    scan_path was never checked against any allowlist before .exists() and
    the later scan_path.rglob("*") walk."""

    def _roots(self, music_root: Path, downloads_root: Path):
        return mock.patch.object(app_module, "_BROWSE_ALLOWED_ROOTS", (music_root, downloads_root))

    def test_outside_allowed_roots_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            music_root = Path(tmp) / "music"
            downloads_root = Path(tmp) / "downloads"
            outside = Path(tmp) / "elsewhere"
            with self._roots(music_root, downloads_root):
                path, error = app_module._resolve_dedup_scan_path(str(outside))
        self.assertIsNone(path)
        self.assertEqual(error, "Path is outside the allowed scan roots")

    def test_valid_downloads_path_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            music_root = Path(tmp) / "music"
            downloads_root = Path(tmp) / "downloads"
            downloads_root.mkdir()
            target = downloads_root / "some-release"
            with self._roots(music_root, downloads_root):
                path, error = app_module._resolve_dedup_scan_path(str(target))
        self.assertIsNone(error)
        self.assertEqual(path, target.resolve())

    def test_dedup_scan_route_rejects_outside_root_without_walking_filesystem(self):
        with tempfile.TemporaryDirectory() as tmp:
            music_root = Path(tmp) / "music"
            downloads_root = Path(tmp) / "downloads"
            outside = Path(tmp) / "elsewhere"
            with self._roots(music_root, downloads_root), \
                 mock.patch.object(Path, "rglob") as mock_rglob:
                with app_module.app.test_request_context(
                    "/api/dedup/scan", method="POST",
                    data=json.dumps({"path": str(outside)}), content_type="application/json",
                ):
                    response, status = app_module.dedup_scan()
            mock_rglob.assert_not_called()
            self.assertEqual(status, 400)
            self.assertEqual(response.get_json()["error"], "Path is outside the allowed scan roots")


if __name__ == "__main__":
    unittest.main()
