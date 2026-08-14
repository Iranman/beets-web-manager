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
                self.assertIn("Refusing to delete a file outside playlist download staging", str(cm.exception))
                self.assertTrue(outside_mp3.exists())

    def test_delete_staged_track_refuses_non_audio_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging_root = Path(tmp) / "playlist-staging"
            staging_root.mkdir()
            script_file = staging_root / "payload.py"
            script_file.write_text("import os; os.system('echo pwned')")

            with mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", staging_root):
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


if __name__ == "__main__":
    unittest.main()
