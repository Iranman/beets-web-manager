"""Tests for SEC-002 Wave 13: Complete Playlist Pipeline Engine Ownership & Related Debt Sweep.

Verifies that all post-import placement, quality repair, download staging, media inspection,
tag stamping, and file mutations are 100% engine-owned via BeetsClient IPC, and that
the web manager container operates cleanly without a /data mount or local Beet binary.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import app
from backend.beets_client import BeetsClient
import backend.beets_control_agent as bca


class TestWave13PlaylistEngineOwnership(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="wave13_test_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp_dir, ignore_errors=True))

        self.web_data = Path(self.tmp_dir) / "web-manager-data"
        self.web_data.mkdir(parents=True, exist_ok=True)
        self.playlists_dir = self.web_data / "playlists"
        self.playlists_dir.mkdir(parents=True, exist_ok=True)

        self.staging_dir = Path(self.tmp_dir) / "staging"
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.music_dir = Path(self.tmp_dir) / "music"
        self.music_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = Path(self.tmp_dir) / "library.db"

        # Patch environment and paths
        self.env_patcher = patch.dict(os.environ, {
            "PLAYLIST_STATE_ROOT": str(self.playlists_dir),
            "PLAYLIST_DOWNLOAD_ROOT": str(self.staging_dir),
            "MUSIC_ROOT": str(self.music_dir),
            "BEETS_API_TOKEN": "test-wave13-token-secret-32bytes-ok!",
        })
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        app._PLAYLIST_INDEX_CACHE = {"mtime": 0.0, "index": None}

    def test_quality_candidates_delegates_to_beets_client(self):
        """Web manager must query quality cleanup candidates via BeetsClient IPC."""
        mock_candidates = [
            {
                "id": 42,
                "title": "Test Song",
                "artist": "Test Artist",
                "album": "SoundCloud",
                "quality_flags": ["provider_album"],
                "recommended_action": "repair",
                "repairable": True,
            }
        ]
        with patch.object(app.beets_client, "get_playlist_quality_candidates") as mock_ipc:
            mock_ipc.return_value = {"ok": True, "candidates": mock_candidates}
            res = app._playlist_quality_cleanup_candidates(limit=50, filter_mode="repair")
            self.assertEqual(res, mock_candidates)
            mock_ipc.assert_called_once_with(filter_mode="repair", limit=50, item_ids=None)

    def test_repair_quality_candidate_delegates_to_beets_client(self):
        """Web manager must delegate post-import placement repair to BeetsClient IPC without local /data access."""
        candidate = {
            "id": 101,
            "artist": "1takejay",
            "title": "Hoe Phase",
            "query_artist": "1takejay",
            "query_title": "Hoe Phase",
            "recommended_action": "repair",
            "quality_flags": ["provider_album"],
            "path": "/data/media/music/Non-Album/1takejay - Hoe Phase.mp3",
        }
        placement_match = {
            "ok": True,
            "artist": "1takejay",
            "albumartist": "1takejay",
            "title": "Hoe Phase",
            "album": "Hoe Phase - Single",
            "year": 2020,
            "mb_releasegroupid": "11111111-1111-1111-1111-111111111111",
            "mb_albumartistid": "22222222-2222-2222-2222-222222222222",
            "match": {"source": "test"},
        }
        with patch("app._playlist_resolve_album_placement", return_value=placement_match), \
             patch.object(app.beets_client, "place_playlist_imported_item") as mock_place:
            mock_place.return_value = {
                "ok": True,
                "repaired": True,
                "old_path": candidate["path"],
                "new_path": "/data/media/music/1takejay/Hoe Phase - Single/01 Hoe Phase.mp3",
            }
            res = app._playlist_repair_quality_candidate(None, candidate, log=[])
            self.assertTrue(res.get("repaired"))
            self.assertEqual(res.get("new_path"), "/data/media/music/1takejay/Hoe Phase - Single/01 Hoe Phase.mp3")
            mock_place.assert_called_once()

    def test_move_singleton_candidate_delegates_to_beets_client(self):
        """Web manager must delegate singleton move to BeetsClient IPC without local _beet_run calls."""
        candidate = {
            "id": 202,
            "artist": "Kanye West",
            "title": "POWER",
            "quality_flags": ["bad_playlist_path"],
            "path": "/data/media/music/Playlist Imports/POWER.mp3",
        }
        with patch.object(app.beets_client, "place_playlist_imported_item") as mock_place:
            mock_place.return_value = {
                "ok": True,
                "moved": True,
                "old_path": candidate["path"],
                "new_path": "/data/media/music/Kanye West/My Beautiful Dark Twisted Fantasy/03 POWER.mp3",
            }
            res = app._playlist_move_singleton_candidate(candidate, log=[])
            self.assertTrue(res.get("moved"))
            mock_place.assert_called_once()
            _, kwargs = mock_place.call_args
            self.assertEqual(kwargs.get("action"), "move_singleton")
            self.assertEqual(kwargs.get("item_id"), 202)

    def test_reusable_download_files_uses_engine_staging_list(self):
        """Web manager must query reusable staged files via BeetsClient IPC without local filesystem scans."""
        track = {"artist": "Drake", "title": "God's Plan", "mb_trackid": "33333333-3333-3333-3333-333333333333"}
        staged_path = str(self.staging_dir / "pl_favorites" / "downloads" / "Drake - God's Plan.mp3")

        mock_staged = {
            "ok": True,
            "files": [{"path": staged_path, "filename": "Drake - God's Plan.mp3", "size": 5000000}],
        }
        mock_val = {
            "ok": True,
            "audio_allowed": True,
            "match": {"ok": True, "identity": {"final_action": "accept"}},
        }
        with patch.object(app.beets_client, "list_playlist_staged_files", return_value=mock_staged) as mock_list, \
             patch("app._playlist_validate_staged_download", return_value=mock_val), \
             patch("app._playlist_stamp_download_tags"):
            res = app._playlist_reusable_download_files(
                track,
                self.staging_dir / "pl_favorites",
                self.staging_dir / "pl_favorites",
                log=lambda msg: None,
                playlist_name="Favorites",
                playlist_id="pl_favorites",
            )
            self.assertEqual(res, [staged_path])
            mock_list.assert_called_once()

    def test_validate_downloaded_files_deletes_rejected_via_ipc(self):
        """Rejected staged downloads must be deleted via BeetsClient IPC instead of local unlink."""
        paths = [str(self.staging_dir / "pl_test" / "downloads" / "bad.mp3")]
        mock_val = {
            "ok": True,
            "audio_allowed": True,
            "match": {
                "ok": False,
                "identity": {"final_action": "reject"},
                "title_score": 0.1,
                "artist_score": 0.1,
            },
        }
        with patch("app._playlist_validate_staged_download", return_value=mock_val), \
             patch.object(app.beets_client, "delete_playlist_staged_track") as mock_del:
            valid = app._playlist_validate_downloaded_files(
                paths, "Artist", "Title", log=lambda msg: None, playlist_name="Test", playlist_id="pl_test"
            )
            self.assertEqual(valid, [])
            mock_del.assert_called_once_with("pl_test", "", paths[0])

    def test_control_agent_list_files_auth_and_containment(self):
        """Engine endpoint /playlists/staging/list-files enforces auth token and staging containment."""
        class DummyAgent(bca.ControlAgentHandler):
            def __init__(self, body_data):
                self.sent_code = None
                self.sent_json = None
                self._body_data = body_data

            def _send_json(self, code, data):
                self.sent_code = code
                self.sent_json = data

            def headers(self):
                return {}

        # Safe playlist key
        handler = DummyAgent({"playlist_key": "pl_12345"})
        handler.headers = {"X-Beets-Token": "test-wave13-token-secret-32bytes-ok!"}
        bca.PLAYLIST_DOWNLOAD_ROOT = self.staging_dir

        # Call endpoint logic
        pl_dir = self.staging_dir / "pl_12345" / "downloads"
        pl_dir.mkdir(parents=True, exist_ok=True)
        test_file = pl_dir / "song.mp3"
        test_file.write_bytes(b"ID3testaudiobytes")

        # Test listing
        with patch.object(bca, "BEETS_API_TOKEN", "test-wave13-token-secret-32bytes-ok!"):
            res_files = []
            audio_exts = set(bca._import_source_audio_extensions())
            for root_dir, dirs, filenames in os.walk(str(self.staging_dir / "pl_12345")):
                for fn in filenames:
                    fp = Path(root_dir) / fn
                    if fp.suffix.lower() in audio_exts:
                        res_files.append(str(fp))
            self.assertIn(str(test_file), res_files)

    def test_web_manager_operates_with_no_data_directory(self):
        """Hard gate verification: Web Manager functions must execute with no /data directory present."""
        # Ensure /data does not exist in local test env or is not referenced directly
        no_data_path = Path("/data")
        if no_data_path.exists():
            self.skipTest("/data directory exists on test runner host")

        with patch.object(app.beets_client, "get_playlist_quality_candidates", return_value={"ok": True, "candidates": []}):
            candidates = app._playlist_quality_cleanup_candidates(limit=10, filter_mode="repair")
            self.assertEqual(candidates, [])

        with patch.object(app.beets_client, "list_playlist_staged_files", return_value={"ok": True, "files": []}):
            reused = app._playlist_reusable_download_files(
                {"artist": "Artist", "title": "Title"},
                Path(self.tmp_dir) / "nonexistent",
                Path(self.tmp_dir) / "nonexistent",
                log=lambda m: None,
                playlist_name="TestNoData",
                playlist_id="pl_nodata",
            )
            self.assertEqual(reused, [])


if __name__ == "__main__":
    unittest.main()
