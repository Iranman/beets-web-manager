"""SEC-002 Wave 9: Playlist, Plex Sync & Staging-Manifest Path Security.

Comprehensive regression tests for playlist staging, manifest generation,
M3U security, Plex path translation, and staged track deletion safety.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module


class Wave9PlaylistPathSanitizationTests(unittest.TestCase):
    """Test suite for _clean_playlist_name and playlist staging root containment."""

    def test_clean_playlist_name_strips_traversal_and_separators(self):
        cases = [
            ("../../etc/passwd", "etc_passwd"),
            ("..\\..\\windows\\system32", "windows_system32"),
            ("normal-playlist", "normal-playlist"),
            ("My/Cool\\Playlist", "My_Cool_Playlist"),
            ("..", "Playlist"),
            (".", "Playlist"),
            ("  ...  ", "Playlist"),
            ("\x00\r\n\u200eBadName", "BadName"),
            ("<>:\"/\\|?*", "Playlist"),
        ]
        for raw, expected in cases:
            cleaned = app_module._clean_playlist_name(raw)
            self.assertNotIn("/", cleaned)
            self.assertNotIn("\\", cleaned)
            self.assertNotIn("..", cleaned)
            self.assertFalse(cleaned.startswith("."))
            self.assertEqual(cleaned, expected)

    def test_get_playlist_staging_root_containment(self):
        root = app_module.get_playlist_staging_root("../../../outside")
        staging_dir = app_module.PLAYLIST_DOWNLOAD_ROOT.resolve(strict=False)
        self.assertTrue(app_module._path_is_under(root.resolve(strict=False), staging_dir))
        self.assertNotIn("..", root.name)

    def test_playlist_ensure_staging_dirs_rejects_outside_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "staging"
            tmp_root.mkdir()
            with mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", tmp_root):
                app_module._playlist_ensure_staging_dirs("../../../outside")
                created_root = app_module.get_playlist_staging_root("../../../outside").resolve(strict=False)
                self.assertTrue(app_module._path_is_under(created_root, tmp_root.resolve(strict=False)))


class Wave9AtomicJsonReplaceSecurityTests(unittest.TestCase):
    """Test suite for _playlist_atomic_json_replace containment enforcement."""

    def test_atomic_json_replace_refuses_outside_target_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside_dir = Path(tmp) / "outside"
            outside_dir.mkdir()
            outside_file = outside_dir / "target.json"
            sentinel = outside_dir / "sentinel.txt"
            sentinel.write_text("protected content")

            with self.assertRaises(ValueError):
                app_module._playlist_atomic_json_replace(
                    outside_file,
                    {"data": "malicious"},
                    save_key="test",
                    label="test json",
                )

            self.assertFalse(outside_file.exists())
            self.assertEqual(sentinel.read_text(), "protected content")

    def test_atomic_json_replace_valid_inside_allowed_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "playlists"
            state_dir.mkdir()
            target_file = state_dir / "my_playlist.playlist.json"

            with mock.patch.object(app_module, "PLAYLIST_DIR", state_dir), \
                 mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", state_dir):
                app_module._playlist_atomic_json_replace(
                    target_file,
                    {"name": "my_playlist", "version": 2},
                    save_key="test_key",
                    label="playlist manifest",
                )

            self.assertTrue(target_file.exists())
            data = json.loads(target_file.read_text(encoding="utf-8"))
            self.assertEqual(data["name"], "my_playlist")


class Wave9StagedTrackDeletionSecurityTests(unittest.TestCase):
    """Test suite for _playlist_delete_staged_track_file containment and symlink safety."""

    def test_delete_staged_track_refuses_deletion_outside_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging_root = Path(tmp) / "playlist-staging"
            staging_root.mkdir()
            outside_dir = Path(tmp) / "outside"
            outside_dir.mkdir()
            outside_mp3 = outside_dir / "secret.mp3"
            outside_mp3.write_bytes(b"audio data")

            with mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", staging_root):
                with self.assertRaises(RuntimeError) as cm:
                    app_module._playlist_delete_staged_track_file(
                        "MyPlaylist",
                        {"id": "tr1"},
                        requested_path=str(outside_mp3),
                    )
                self.assertIn("Refusing to delete a file outside this playlist's own staging directory", str(cm.exception))
                self.assertTrue(outside_mp3.exists())

    def test_delete_staged_track_refuses_non_audio_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging_root = Path(tmp) / "playlist-staging"
            staging_root.mkdir()
            with mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", staging_root):
                # Nested under this playlist's own staging subfolder --
                # real staged files always live there, never directly
                # under the shared PLAYLIST_DOWNLOAD_ROOT (see the
                # cross-playlist containment narrowing above).
                playlist_staging = app_module.get_playlist_staging_root("MyPlaylist")
                playlist_staging.mkdir(parents=True)
                script_file = playlist_staging / "payload.py"
                script_file.write_text("import os; os.system('echo pwned')")
                with self.assertRaises(RuntimeError) as cm:
                    app_module._playlist_delete_staged_track_file(
                        "MyPlaylist",
                        {"id": "tr2"},
                        requested_path=str(script_file),
                    )
                self.assertIn("Refusing to delete a non-audio staging file", str(cm.exception))
                self.assertTrue(script_file.exists())

    def test_delete_staged_track_refuses_library_music_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging_root = Path(tmp) / "playlist-staging"
            staging_root.mkdir()
            music_root = Path(tmp) / "music"
            music_root.mkdir()
            library_track = music_root / "Artist" / "Album" / "01 - Track.mp3"
            library_track.parent.mkdir(parents=True)
            library_track.write_bytes(b"library track")

            with mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", staging_root), \
                 mock.patch.object(app_module, "MUSIC_ROOT", music_root):
                with self.assertRaises(RuntimeError) as cm:
                    app_module._playlist_delete_staged_track_file(
                        "MyPlaylist",
                        {"id": "tr3"},
                        requested_path=str(library_track),
                    )
                self.assertIn("Refusing to delete a Beets library file", str(cm.exception))
                self.assertTrue(library_track.exists())

    def test_delete_staged_track_deletes_valid_staged_mp3(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging_root = Path(tmp) / "playlist-staging"
            playlist_state_dir = Path(tmp) / "playlists"
            staging_root.mkdir()
            playlist_state_dir.mkdir()

            with mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", staging_root), \
                 mock.patch.object(app_module, "PLAYLIST_DIR", playlist_state_dir):
                playlist_dir = app_module.get_playlist_staging_root("MyPlaylist")
                playlist_downloads = playlist_dir / "downloads"
                playlist_downloads.mkdir(parents=True)
                staged_mp3 = playlist_downloads / "01 - Track One.mp3"
                staged_mp3.write_bytes(b"audio data")

                res = app_module._playlist_delete_staged_track_file(
                    "MyPlaylist",
                    {"id": "tr4"},
                    requested_path=str(staged_mp3),
                )
                self.assertTrue(res["deleted"])
                self.assertFalse(staged_mp3.exists())
                self.assertTrue((playlist_state_dir / "MyPlaylist.playlist.json").exists())


class Wave9M3UAndPlexTranslationSecurityTests(unittest.TestCase):
    """Test suite for M3U generation and Plex path containment."""

    def test_create_playlist_outputs_sanitizes_m3u_newlines(self):
        with tempfile.TemporaryDirectory() as tmp:
            playlist_dir = Path(tmp) / "playlists"
            playlist_dir.mkdir()

            items = [
                {
                    "artist": "Artist\nInjected Header",
                    "title": "Title\r\nInjected Track",
                    "path": "/data/media/music/Artist/Album/01.mp3",
                }
            ]

            with mock.patch.object(app_module, "PLAYLIST_DIR", playlist_dir), \
                 mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", playlist_dir), \
                 mock.patch.object(app_module, "_plex_settings", return_value={"token": ""}):
                app_module._create_playlist_outputs("CleanPlaylist", items, sync_plex=False)

            m3u_file = playlist_dir / "CleanPlaylist.m3u"
            self.assertTrue(m3u_file.exists())
            content = m3u_file.read_text(encoding="utf-8")
            lines = content.splitlines()

            # Ensure header lines don't break into extra EXTINF entries
            extinf_lines = [l for l in lines if l.startswith("#EXTINF")]
            self.assertEqual(len(extinf_lines), 1)
            self.assertIn("Artist Injected Header - Title Injected Track", extinf_lines[0])

    def test_plex_path_containment_rejects_prefix_collision(self):
        with mock.patch.object(app_module, "MUSIC_ROOT", Path("/data/media/music")):
            # /data/media/music2 must NOT be considered inside /data/media/music
            self.assertFalse(app_module._plex_path_is_under("/data/media/music2/song.mp3", "/data/media/music"))
            self.assertTrue(app_module._plex_path_is_under("/data/media/music/Artist/Album/01.mp3", "/data/media/music"))


class Wave9FinalReviewCorrectionTests(unittest.TestCase):
    """Regression tests for the corrections made during Claude's independent
    final review of PR #82 (SEC-002 Wave 9). Each test proves the specific
    vulnerable behavior the correction closes, not just the new code shape."""

    def test_unauthorized_absolute_path_does_not_gain_basename_authority(self):
        """An absolute path outside every allowed root (/etc/passwd, or a
        real file under an unrelated same-prefix root like .../music2) must
        not be silently reinterpreted as MUSIC_ROOT / <its basename> -- that
        invents a new, different, plausible-looking library path from
        attacker input instead of rejecting it."""
        with mock.patch.object(app_module, "MUSIC_ROOT", Path("/data/media/music")), \
             mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", Path("/data/torrents/music/Playlist Downloads")), \
             mock.patch.object(app_module, "DOWNLOADS_ROOT", Path("/data/torrents/music")), \
             mock.patch.object(app_module, "PLAYLIST_PATH_ROOT_ALIASES", []):
            for unauthorized in ("/etc/passwd", "/data/media/music2/song.mp3", "/secret/outside/song.mp3"):
                resolved = app_module._playlist_resolve_item_path(unauthorized)
                self.assertEqual(resolved, app_module._PLAYLIST_UNRESOLVED_PATH, unauthorized)
                # The dangerous old behavior: MUSIC_ROOT / Path(unauthorized).name
                self.assertNotEqual(resolved, Path("/data/media/music") / Path(unauthorized).name, unauthorized)
                self.assertFalse(resolved.exists())

    def test_empty_path_does_not_resolve_to_library_root(self):
        """An empty item path must never become authority for the library
        root itself -- root-self must not silently satisfy downstream
        containment/existence checks."""
        with mock.patch.object(app_module, "MUSIC_ROOT", Path("/data/media/music")):
            resolved = app_module._playlist_resolve_item_path("")
            self.assertNotEqual(resolved, app_module.MUSIC_ROOT)
            self.assertEqual(resolved, app_module._PLAYLIST_UNRESOLVED_PATH)
            self.assertFalse(resolved.exists())

    def test_relative_traversal_path_does_not_escape_music_root(self):
        """A relative item path containing traversal segments must not
        resolve outside MUSIC_ROOT once a caller calls .resolve()/.exists()
        on the returned path."""
        with tempfile.TemporaryDirectory() as tmp:
            music_root = Path(tmp) / "music"
            music_root.mkdir()
            outside_marker = Path(tmp) / "outside-secret.txt"
            outside_marker.write_text("do not expose")
            with mock.patch.object(app_module, "MUSIC_ROOT", music_root):
                resolved = app_module._playlist_resolve_item_path("../outside-secret.txt")
                self.assertEqual(resolved, app_module._PLAYLIST_UNRESOLVED_PATH)
                self.assertFalse(resolved.exists())

    def test_relative_path_within_music_root_still_resolves_normally(self):
        """The fix must not regress the legitimate case: a plain
        library-relative path still resolves under MUSIC_ROOT."""
        with mock.patch.object(app_module, "MUSIC_ROOT", Path("/data/media/music")):
            resolved = app_module._playlist_resolve_item_path("Artist/Album/01 - Track.mp3")
            self.assertEqual(resolved, Path("/data/media/music/Artist/Album/01 - Track.mp3"))

    def test_playlist_name_sanitization_collision_is_a_known_unresolved_defect(self):
        """_clean_playlist_name() maps distinct raw names to the same
        sanitized identity -- confirmed, real, NOT fixed by this review
        (would require a stable playlist-ID identity scheme, out of this
        narrow review's scope; see docs/TECHNICAL_DEBT.md). This test
        documents/locks the current (unsafe) behavior so it is not
        silently forgotten, rather than asserting it is safe."""
        collisions = ["A/B", "A\\B", "A:B", "A?B"]
        cleaned = {app_module._clean_playlist_name(name) for name in collisions}
        self.assertEqual(cleaned, {"A_B"}, "distinct playlist names collide to the same sanitized identity")

        truncation_a = "X" * 120 + "AAAA"
        truncation_b = "X" * 120 + "BBBB"
        self.assertEqual(
            app_module._clean_playlist_name(truncation_a),
            app_module._clean_playlist_name(truncation_b),
            "distinct long playlist names collide after 120-char truncation",
        )

    def test_cross_playlist_staged_deletion_is_refused(self):
        """A track-action delete_staged request for playlist A must not be
        able to delete a file staged for playlist B, even though both sit
        under the same shared PLAYLIST_DOWNLOAD_ROOT -- this was reachable
        end-to-end from POST /api/playlists/<name>/tracks/action's raw,
        attacker-controlled payload["path"], which flows straight into
        requested_path with no server-side ownership check."""
        with tempfile.TemporaryDirectory() as tmp:
            shared_root = Path(tmp) / "Playlist Downloads"
            with mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", shared_root):
                playlist_b_dir = app_module.get_playlist_staging_root("Playlist B") / "downloads"
                playlist_b_dir.mkdir(parents=True)
                victim_track = playlist_b_dir / "01 - Victim Track.mp3"
                victim_track.write_bytes(b"audio data belonging to playlist B")

                with self.assertRaises(RuntimeError) as cm:
                    app_module._playlist_delete_staged_track_file(
                        "Playlist A",
                        {"id": "cross-playlist-attack"},
                        requested_path=str(victim_track),
                    )
                self.assertIn("outside this playlist's own staging directory", str(cm.exception))
                self.assertTrue(victim_track.exists(), "playlist B's staged track must survive playlist A's delete request")

    def test_atomic_json_replace_no_longer_trusts_music_root_or_system_temp(self):
        """MUSIC_ROOT and the whole system temp directory were previously
        accepted allowed_roots for _playlist_atomic_json_replace() with no
        production caller ever targeting either -- both must now be
        refused."""
        with tempfile.TemporaryDirectory() as tmp:
            music_root = Path(tmp) / "music"
            music_root.mkdir()
            with mock.patch.object(app_module, "MUSIC_ROOT", music_root), \
                 mock.patch.object(app_module, "PLAYLIST_DIR", Path(tmp) / "playlists"), \
                 mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", Path(tmp) / "staging"), \
                 mock.patch.object(app_module, "PLAYLIST_JOB_STATE_DIR", Path(tmp) / "jobs"):
                with self.assertRaises(ValueError):
                    app_module._playlist_atomic_json_replace(
                        music_root / "not-state.json", {"x": 1}, save_key="t",
                    )
                system_tmp = Path(tempfile.gettempdir()) / "some-other-app-file.json"
                with self.assertRaises(ValueError):
                    app_module._playlist_atomic_json_replace(
                        system_tmp, {"x": 1}, save_key="t2",
                    )

    def test_atomic_json_replace_accepts_playlist_job_state_dir(self):
        """PLAYLIST_JOB_STATE_DIR (the real root _playlist_save_job_state()
        writes checkpoints under) was previously *absent* from
        allowed_roots entirely -- every real job-state save raised
        ValueError in every environment. Confirmed fixed here."""
        with tempfile.TemporaryDirectory() as tmp:
            job_state_dir = Path(tmp) / "playlist_jobs"
            job_state_dir.mkdir()
            with mock.patch.object(app_module, "PLAYLIST_JOB_STATE_DIR", job_state_dir), \
                 mock.patch.object(app_module, "PLAYLIST_DIR", Path(tmp) / "playlists"), \
                 mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", Path(tmp) / "staging"):
                target = job_state_dir / "pl-deadbeef.json"
                app_module._playlist_atomic_json_replace(target, {"job_id": "pl-deadbeef"}, save_key="job")
                self.assertTrue(target.exists())


if __name__ == "__main__":
    unittest.main()
