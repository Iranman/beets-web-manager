"""Regression coverage for CodeQL alerts #6093/#6095 (py/path-injection,
app.py:6093/6095 at baseline commit 7b05844d58d657ce6f1ae6c5b1724f5ba70ee257),
_folder_release_preflight() -- closed in the repository-wide CodeQL closure
session's second pass at this function.

_folder_release_preflight()'s local rglob() scan had no containment check at
all, unlike its sibling _folder_import_track_count() (fixed in Wave 1,
docs/TECHNICAL_DEBT.md SEC-002 backlog #334/#335). A first fix attempt that
copied the sibling's check verbatim (aborting the whole preflight for an
out-of-root folder) broke two established tests in
test_sec002_wave14_mb_identity_matching.py that deliberately exercise real
local-file matching against arbitrary non-MUSIC_ROOT temp directories.

Investigation found the actual architecture: when the local scan finds
nothing, _folder_release_preflight() already falls through to
beets_client.inspect_import_source() -- the engine-side inspection call,
which independently re-validates the folder against its own
resolve_safe_path() allowed-root policy on the control agent
(backend/beets_control_agent.py:inspect_import_source()), regardless of
what this function passes it (see that function's own docstring: "the web
manager has no local filesystem access ... this asks the engine ... to do
it"). Gating the local shortcut to MUSIC_ROOT/DOWNLOADS_ROOT and falling
through to that already-authoritative path (rather than aborting, and rather
than leaving the local shortcut unguarded) closes the alert without
weakening any caller's behavior -- the two broken tests were relying on an
implicit, unmocked local-disk side channel that bypassed the engine's real
validation entirely; they now mock beets_client.inspect_import_source
instead, exercising the function through its actual production path for a
folder the web manager container has no local mount for.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module


class FolderReleasePreflightLocalScanContainmentTests(unittest.TestCase):
    """Unit-level proof that the local rglob() shortcut itself is gated,
    independent of the engine-fallback integration covered in
    test_sec002_wave14_mb_identity_matching.py."""

    def _mb_mock(self):
        return {
            "ok": True,
            "release_title": "Some Release",
            "release_artist": "Some Artist",
            "release_group": "88888888-8888-8888-8888-888888888888",
            "tracks": [{"title": "Track One", "track": 1}],
        }

    def test_outside_root_folder_does_not_walk_local_disk(self):
        # A real, readable, non-empty directory outside every configured
        # root must never be walked locally -- proven by asserting the
        # engine-fallback path (inspect_import_source) is the one actually
        # invoked, not a bypassed local rglob().
        with tempfile.TemporaryDirectory() as tmp:
            music_root = Path(tmp) / "music"
            downloads_root = Path(tmp) / "downloads"
            outside = Path(tmp) / "outside" / "Some Artist" / "Some Release"
            outside.mkdir(parents=True)
            (outside / "secret_track.mp3").write_bytes(b"x")

            with mock.patch.object(app_module, "MUSIC_ROOT", music_root), \
                 mock.patch.object(app_module, "DOWNLOADS_ROOT", downloads_root), \
                 mock.patch.object(app_module, "_fetch_mb_release_tracklist", return_value=self._mb_mock()), \
                 mock.patch.object(app_module.beets_client, "inspect_import_source") as mock_inspect:
                mock_inspect.return_value = {"ok": True, "audio_files": []}
                app_module._folder_release_preflight(
                    str(outside), "11111111-1111-1111-1111-111111111111"
                )

        mock_inspect.assert_called_once()
        self.assertEqual(mock_inspect.call_args[0][0], str(outside))

    def test_sibling_prefix_escape_does_not_walk_local_disk(self):
        # "<music_root>-evil" must not be treated as under music_root merely
        # because it shares a string prefix.
        with tempfile.TemporaryDirectory() as tmp:
            music_root = Path(tmp) / "music"
            downloads_root = Path(tmp) / "downloads"
            music_root.mkdir()
            sibling = Path(tmp) / "music-evil" / "Some Artist" / "Some Release"
            sibling.mkdir(parents=True)
            (sibling / "track.mp3").write_bytes(b"x")

            with mock.patch.object(app_module, "MUSIC_ROOT", music_root), \
                 mock.patch.object(app_module, "DOWNLOADS_ROOT", downloads_root), \
                 mock.patch.object(app_module, "_fetch_mb_release_tracklist", return_value=self._mb_mock()), \
                 mock.patch.object(app_module.beets_client, "inspect_import_source") as mock_inspect:
                mock_inspect.return_value = {"ok": True, "audio_files": []}
                app_module._folder_release_preflight(
                    str(sibling), "11111111-1111-1111-1111-111111111111"
                )

        mock_inspect.assert_called_once()

    def test_symlink_leaf_escape_does_not_walk_local_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            music_root = Path(tmp) / "music"
            downloads_root = Path(tmp) / "downloads"
            outside = Path(tmp) / "outside"
            music_root.mkdir()
            outside.mkdir()
            (outside / "track.mp3").write_bytes(b"x")
            link = music_root / "Escaped Album"
            link.symlink_to(outside, target_is_directory=True)

            with mock.patch.object(app_module, "MUSIC_ROOT", music_root), \
                 mock.patch.object(app_module, "DOWNLOADS_ROOT", downloads_root), \
                 mock.patch.object(app_module, "_fetch_mb_release_tracklist", return_value=self._mb_mock()), \
                 mock.patch.object(app_module.beets_client, "inspect_import_source") as mock_inspect:
                mock_inspect.return_value = {"ok": True, "audio_files": []}
                app_module._folder_release_preflight(
                    str(link), "11111111-1111-1111-1111-111111111111"
                )

        # _path_is_under() resolves through the symlink to `outside`, which
        # is itself outside every allowed root, so this must still be
        # gated to the engine-fallback path rather than walking the
        # symlink's real (out-of-root) target locally.
        mock_inspect.assert_called_once()

    def test_folder_under_music_root_is_still_scanned_locally(self):
        # The fix must not break the legitimate, intended case: a folder
        # genuinely under MUSIC_ROOT should still use the fast local path
        # and never need the engine fallback.
        with tempfile.TemporaryDirectory() as tmp:
            music_root = Path(tmp) / "music"
            downloads_root = Path(tmp) / "downloads"
            album_dir = music_root / "Some Artist" / "Some Release"
            album_dir.mkdir(parents=True)
            (album_dir / "01 Track One.mp3").write_bytes(b"x")

            with mock.patch.object(app_module, "MUSIC_ROOT", music_root), \
                 mock.patch.object(app_module, "DOWNLOADS_ROOT", downloads_root), \
                 mock.patch.object(app_module, "_fetch_mb_release_tracklist", return_value=self._mb_mock()), \
                 mock.patch.object(app_module.beets_client, "inspect_import_source") as mock_inspect:
                res = app_module._folder_release_preflight(
                    str(album_dir), "11111111-1111-1111-1111-111111111111"
                )

        mock_inspect.assert_not_called()
        self.assertEqual(res.get("audio_count"), 1)

    def test_nonexistent_folder_falls_through_to_engine_gracefully(self):
        with tempfile.TemporaryDirectory() as tmp:
            music_root = Path(tmp) / "music"
            downloads_root = Path(tmp) / "downloads"
            missing = Path(tmp) / "outside" / "does-not-exist"

            with mock.patch.object(app_module, "MUSIC_ROOT", music_root), \
                 mock.patch.object(app_module, "DOWNLOADS_ROOT", downloads_root), \
                 mock.patch.object(app_module, "_fetch_mb_release_tracklist", return_value=self._mb_mock()), \
                 mock.patch.object(app_module.beets_client, "inspect_import_source") as mock_inspect:
                mock_inspect.side_effect = RuntimeError("Beets Control Agent is unavailable")
                res = app_module._folder_release_preflight(
                    str(missing), "11111111-1111-1111-1111-111111111111"
                )

        self.assertFalse(res.get("ok"))
        self.assertTrue(res.get("scan_unavailable"))


if __name__ == "__main__":
    unittest.main()
