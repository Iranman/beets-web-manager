"""
Unit tests for SEC-002 Wave 10: Persistent Playlist Identity + Complete M3U Lifecycle.
"""

from io import BytesIO
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import app as flask_app
from backend.beets_client import BeetsClient
import backend.beets_control_agent as agent
from backend.beets_control_agent import ControlAgentHandler


def _post_agent(path: str, payload: dict):
    handler = ControlAgentHandler.__new__(ControlAgentHandler)
    body = json.dumps(payload).encode("utf-8")
    handler.path = path
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = BytesIO(body)
    handler._authenticate = lambda: True
    responses = []
    handler._send_json = lambda code, data: responses.append((code, data))
    handler.do_POST()
    return responses[0]


def _get_agent(path: str):
    handler = ControlAgentHandler.__new__(ControlAgentHandler)
    handler.path = path
    handler.headers = {}
    handler._authenticate = lambda: True
    responses = []
    handler._send_json = lambda code, data: responses.append((code, data))
    handler.do_GET()
    return responses[0]


class TestWave10PlaylistIdentityAndM3ULifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)
        self.data_dir = self.base_dir / "web-manager-data"
        self.playlists_dir = self.data_dir / "playlists"
        self.manifests_dir = self.playlists_dir / "manifests"
        self.exports_dir = self.playlists_dir / "exports"
        self.legacy_playlist_dir = self.base_dir / "playlists"

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.playlists_dir.mkdir(parents=True, exist_ok=True)
        self.manifests_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self.legacy_playlist_dir.mkdir(parents=True, exist_ok=True)

        self.app_patches = [
            patch.object(flask_app, "WEB_MANAGER_DATA_DIR", self.data_dir),
            patch.object(flask_app, "PLAYLIST_STATE_ROOT", self.playlists_dir),
            patch.object(flask_app, "PLAYLIST_MANIFESTS_DIR", self.manifests_dir),
            patch.object(flask_app, "PLAYLIST_EXPORTS_DIR", self.exports_dir),
            patch.object(flask_app, "PLAYLIST_DIR", self.legacy_playlist_dir),
        ]
        for p in self.app_patches:
            p.start()

        self.agent_patches = [
            patch.object(agent, "PLAYLIST_DIR", self.legacy_playlist_dir),
            patch.object(agent, "MUSIC_ROOT", self.base_dir),
            patch.object(agent, "PLAYLIST_DOWNLOAD_ROOT", self.base_dir),
        ]
        for p in self.agent_patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.agent_patches):
            p.stop()
        for p in reversed(self.app_patches):
            p.stop()
        self.tmp_dir.cleanup()

    def test_provider_playlist_identity(self):
        """Provider namespace + immutable provider ID derives deterministic playlist_id."""
        pl_id1 = flask_app._playlist_ensure_stable_id("My Spot", provider_id="spotify:37i9dQZF1DXcBWIGoYBM5M")
        pl_id2 = flask_app._playlist_ensure_stable_id("My Spot", provider_id="spotify:37i9dQZF1DXcBWIGoYBM5M")
        self.assertEqual(pl_id1, "spotify:37i9dQZF1DXcBWIGoYBM5M")
        self.assertEqual(pl_id1, pl_id2)

        key1 = flask_app._playlist_key("My Spot", playlist_id=pl_id1)
        self.assertTrue(key1.startswith("spotify_37i9dQZF1DXcBWIGoYBM5M_"))

    def test_local_uuid_persistence(self):
        """Playlists without provider ID get permanent internal pl_<uuid> persisted to index and manifest."""
        pl_id = flask_app._playlist_ensure_stable_id("Local Mix")
        self.assertTrue(pl_id.startswith("pl_"))

        # Re-querying with the same display name should return the same persisted playlist_id from index
        pl_id_again = flask_app._playlist_ensure_stable_id("Local Mix")
        self.assertEqual(pl_id, pl_id_again)

        # Index file must contain the mapping
        index_file = flask_app.PLAYLIST_STATE_ROOT / "index.json"
        self.assertTrue(index_file.exists())
        index_data = json.loads(index_file.read_text(encoding="utf-8"))
        self.assertIn(pl_id, index_data)

    def test_rename_invariance(self):
        """Renaming display name updates manifest name metadata without altering playlist_id or playlist_key."""
        name_orig = "Roadtrip Rock"
        pl_id = flask_app._playlist_ensure_stable_id(name_orig)
        key_orig = flask_app._playlist_key(name_orig, playlist_id=pl_id)

        # Save initial manifest
        flask_app._playlist_write_manifest(
            name_orig,
            desired_tracks=[{"artist": "Queen", "title": "Bohemian Rhapsody"}],
            matched_tracks=[{"artist": "Queen", "title": "Bohemian Rhapsody", "path": "/data/media/music/queen.mp3"}],
            playlist_id=pl_id,
        )

        # Rename display name to "Summer Roadtrip Rock"
        name_new = "Summer Roadtrip Rock"
        pl_id_renamed = flask_app._playlist_ensure_stable_id(name_new, playlist_id=pl_id)
        key_renamed = flask_app._playlist_key(name_new, playlist_id=pl_id_renamed)

        # Updating manifest with same playlist_id
        flask_app._playlist_write_manifest(
            name_new,
            desired_tracks=[{"artist": "Queen", "title": "Bohemian Rhapsody"}],
            matched_tracks=[{"artist": "Queen", "title": "Bohemian Rhapsody", "path": "/data/media/music/queen.mp3"}],
            playlist_id=pl_id,
        )

        self.assertEqual(pl_id, pl_id_renamed)
        self.assertEqual(key_orig, key_renamed)

    def test_same_name_playlist_coexistence(self):
        """Two playlists with identical display names get distinct internal IDs and keys."""
        pl_id1 = flask_app._playlist_ensure_stable_id("Favorites")
        key1 = flask_app._playlist_key("Favorites", playlist_id=pl_id1)

        # Force a new playlist creation with same display name by providing explicit new playlist_id
        import uuid
        pl_id2 = f"pl_{uuid.uuid4().hex[:12]}"
        pl_id2_saved = flask_app._playlist_ensure_stable_id("Favorites", playlist_id=pl_id2)
        key2 = flask_app._playlist_key("Favorites", playlist_id=pl_id2)

        self.assertNotEqual(pl_id1, pl_id2_saved)
        self.assertNotEqual(key1, key2)

    def test_sanitization_and_truncation_collision_separation(self):
        """Name variants with identical sanitized stems (e.g. 'A/B' vs 'A\\B') get distinct playlist IDs."""
        pl_id_a = flask_app._playlist_ensure_stable_id("A/B")
        pl_id_b = flask_app._playlist_ensure_stable_id("A\\B")
        pl_id_c = flask_app._playlist_ensure_stable_id("A:B")

        self.assertNotEqual(pl_id_a, pl_id_b)
        self.assertNotEqual(pl_id_a, pl_id_c)
        self.assertNotEqual(pl_id_b, pl_id_c)

    def test_engine_m3u_endpoints(self):
        """Test BeetsClient M3U methods export, read, delete, list against mocked engine responses."""
        beets_client = BeetsClient("http://localhost:8337")

        with patch.object(beets_client, "_request") as mock_req:
            # 1. read_playlist_m3u
            mock_req.return_value = {"ok": True, "exists": True, "key": "test_key", "items": []}
            res = beets_client.read_playlist_m3u("test_key")
            self.assertTrue(res["ok"])
            mock_req.assert_called_with("POST", "/playlists/m3u/read", {"playlist_key": "test_key", "fallback_name": ""})

            # 2. delete_playlist_m3u
            mock_req.return_value = {"ok": True, "deleted": True, "key": "test_key"}
            res = beets_client.delete_playlist_m3u("test_key")
            self.assertTrue(res["ok"])
            mock_req.assert_called_with("POST", "/playlists/m3u/delete", {"playlist_key": "test_key", "fallback_name": ""})

            # 3. list_playlist_m3u
            mock_req.return_value = {"ok": True, "playlists": [{"key": "test_key", "name": "Test"}]}
            res = beets_client.list_playlist_m3u()
            self.assertTrue(res["ok"])
            mock_req.assert_called_with("POST", "/playlists/m3u/list")

    def test_m3u_write_read_delete_agent_handlers(self):
        """Test engine agent /playlists/export_m3u, /playlists/m3u/read, /playlists/m3u/delete direct execution."""
        if os.name != "posix":
            self.skipTest("resolve_safe_path requires POSIX-absolute input; covered by Docker/Linux runtime")

        sample_track = self.base_dir / "pink_floyd_money.flac"
        sample_track.touch()

        # Export M3U
        code, data = _post_agent("/playlists/export_m3u", {
            "playlist_key": "test_rock_123",
            "name": "Test Rock",
            "items": [
                {"artist": "Pink Floyd", "title": "Money", "path": str(sample_track)}
            ]
        })
        self.assertEqual(code, 200)
        self.assertTrue(data.get("ok"))

        # Read M3U
        code, data = _post_agent("/playlists/m3u/read", {
            "playlist_key": "test_rock_123",
            "fallback_name": "Test Rock"
        })
        self.assertEqual(code, 200)
        self.assertTrue(data.get("ok"))
        self.assertTrue(data.get("exists"))
        self.assertEqual(len(data.get("items")), 1)
        self.assertEqual(data["items"][0]["artist"], "Pink Floyd")

        # List M3U
        code, data = _get_agent("/playlists/m3u/list")
        self.assertEqual(code, 200)
        self.assertTrue(data.get("ok"))
        self.assertTrue(any(p["key"] == "test_rock_123" for p in data.get("playlists", [])))

        # Delete M3U
        code, data = _post_agent("/playlists/m3u/delete", {
            "playlist_key": "test_rock_123"
        })
        self.assertEqual(code, 200)
        self.assertTrue(data.get("ok"))
        self.assertTrue(data.get("deleted"))

    def test_legacy_playlist_migration(self):
        """Legacy {clean_name}.playlist.json and {clean_name}.m3u automatically read and migrated without data loss."""
        # Create legacy manifest in PLAYLIST_DIR
        legacy_manifest = self.legacy_playlist_dir / "Legacy Party.playlist.json"
        legacy_manifest.write_text(json.dumps({
            "name": "Legacy Party",
            "desired_tracks": [{"artist": "Daft Punk", "title": "One More Time"}],
            "matched_tracks": [{"artist": "Daft Punk", "title": "One More Time", "path": "/data/media/music/daft_punk.mp3"}]
        }), encoding="utf-8")

        # Create legacy M3U in PLAYLIST_DIR
        legacy_m3u = self.legacy_playlist_dir / "Legacy Party.m3u"
        legacy_m3u.write_text("#EXTM3U\n#EXTINF:-1,Daft Punk - One More Time\n/data/media/music/daft_punk.mp3\n", encoding="utf-8")

        manifest = flask_app._playlist_read_manifest("Legacy Party")
        self.assertEqual(manifest["name"], "Legacy Party")
        self.assertIn("playlist_id", manifest)
        self.assertTrue(manifest["playlist_id"].startswith("pl_"))

        # Verify existence check works for legacy file
        self.assertTrue(flask_app._playlist_saved_playlist_exists("Legacy Party"))


if __name__ == "__main__":
    unittest.main()
