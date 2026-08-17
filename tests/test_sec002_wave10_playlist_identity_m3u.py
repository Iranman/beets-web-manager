"""
Unit tests for SEC-002 Wave 10: Persistent Playlist Identity + Complete M3U Lifecycle.
"""

from io import BytesIO
import concurrent.futures
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
        self.jobs_dir = self.playlists_dir / "jobs"
        self.membership_dir = self.playlists_dir / "membership"
        self.legacy_playlist_dir = self.base_dir / "playlists"

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.playlists_dir.mkdir(parents=True, exist_ok=True)
        self.manifests_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.membership_dir.mkdir(parents=True, exist_ok=True)
        self.legacy_playlist_dir.mkdir(parents=True, exist_ok=True)

        self.app_patches = [
            patch.object(flask_app, "WEB_MANAGER_DATA_DIR", self.data_dir),
            patch.object(flask_app, "PLAYLIST_STATE_ROOT", self.playlists_dir),
            patch.object(flask_app, "PLAYLIST_MANIFESTS_DIR", self.manifests_dir),
            patch.object(flask_app, "PLAYLIST_EXPORTS_DIR", self.exports_dir),
            patch.object(flask_app, "PLAYLIST_JOB_STATE_DIR", self.jobs_dir),
            patch.object(flask_app, "PLAYLIST_MEMBERSHIP_DIR", self.membership_dir),
            patch.object(flask_app, "PLAYLIST_DIR", self.legacy_playlist_dir),
        ]
        for p in self.app_patches:
            p.start()

        self.agent_patches = [
            patch.object(agent, "PLAYLIST_DIR", self.legacy_playlist_dir),
            patch.object(agent, "MUSIC_LIBRARY_PATH", str(self.base_dir)),
            patch.object(agent, "DOWNLOAD_PATH", str(self.base_dir)),
            patch.object(agent, "MUSIC_ROOT", self.base_dir),
            patch.object(agent, "PLAYLIST_DOWNLOAD_ROOT", self.base_dir),
            patch.object(agent, "LOCK_PATH", str(self.base_dir / ".beet_db.lock")),
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
        """Provider identity maps to a stable internal pl_<uuid> without becoming the primary key."""
        pl_id1 = flask_app._playlist_ensure_stable_id("My Spot", provider_id="spotify:37i9dQZF1DXcBWIGoYBM5M")
        pl_id2 = flask_app._playlist_ensure_stable_id("My Spot", provider_id="spotify:37i9dQZF1DXcBWIGoYBM5M")
        self.assertRegex(pl_id1, r"^pl_[0-9a-f]{32}$")
        self.assertEqual(pl_id1, pl_id2)

        key1 = flask_app._playlist_key("My Spot", playlist_id=pl_id1)
        self.assertTrue(key1.startswith(f"{pl_id1}_"))
        index_data = json.loads((flask_app.PLAYLIST_STATE_ROOT / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index_data[pl_id1]["provider_identity"], "spotify:37i9dQZF1DXcBWIGoYBM5M")

    def test_local_uuid_persistence(self):
        """Playlists without provider ID get permanent internal pl_<uuid> persisted to index."""
        pl_id = flask_app._playlist_ensure_stable_id("Local Mix")
        self.assertRegex(pl_id, r"^pl_[0-9a-f]{32}$")

        resolved = flask_app._playlist_resolve_stable_id("Local Mix")
        self.assertEqual(pl_id, resolved)

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
        """Two normal create operations with identical display names get distinct internal IDs and keys."""
        pl_id1 = flask_app._playlist_ensure_stable_id("Favorites")
        key1 = flask_app._playlist_key("Favorites", playlist_id=pl_id1)
        pl_id2 = flask_app._playlist_ensure_stable_id("Favorites")
        key2 = flask_app._playlist_key("Favorites", playlist_id=pl_id2)

        self.assertRegex(pl_id1, r"^pl_[0-9a-f]{32}$")
        self.assertRegex(pl_id2, r"^pl_[0-9a-f]{32}$")
        self.assertNotEqual(pl_id1, pl_id2)
        self.assertNotEqual(key1, key2)
        with self.assertRaises(flask_app.PlaylistStateError) as cm:
            flask_app._playlist_resolve_stable_id("Favorites")
        self.assertEqual(cm.exception.code, "ambiguous_playlist")

    def test_playlist_create_route_uses_one_id_per_create_and_allows_same_name(self):
        """The normal create route must not allocate separate M3U and manifest identities."""
        exported_keys = []

        def fake_export(playlist_key, display_name, items):
            exported_keys.append(playlist_key)
            return {"ok": True, "playlist_key": playlist_key, "size": 12}

        payload = {
            "name": "Favorites",
            "items": [{"id": 1, "path": "/data/media/music/a.mp3", "artist": "A", "title": "One"}],
            "desired_tracks": [{"artist": "A", "title": "One"}],
        }
        responses = []
        with patch.object(flask_app.beets_client, "export_playlist_m3u", side_effect=fake_export), \
             patch.object(flask_app, "_plex_settings", return_value={}):
            for _ in range(2):
                with flask_app.app.test_request_context(
                    "/api/playlist/create",
                    method="POST",
                    data=json.dumps(payload),
                    content_type="application/json",
                ):
                    response = flask_app.playlist_create()
                data = response.get_json()
                self.assertTrue(data.get("ok"), data)
                responses.append(data)

        self.assertEqual(exported_keys, [row["playlist_key"] for row in responses])
        self.assertNotEqual(responses[0]["playlist_id"], responses[1]["playlist_id"])
        self.assertNotEqual(responses[0]["playlist_key"], responses[1]["playlist_key"])
        index_data = json.loads((flask_app.PLAYLIST_STATE_ROOT / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(set(index_data), {row["playlist_id"] for row in responses})
        manifest_files = list(self.manifests_dir.glob("*.playlist.json"))
        self.assertEqual(len(manifest_files), 2)
        for row in responses:
            manifest = json.loads(Path(row["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["playlist_id"], row["playlist_id"])
            self.assertIn(row["playlist_key"], row["manifest"])
            self.assertTrue(any(row["playlist_key"] in path.name for path in manifest_files))
            self.assertEqual(row["m3u"], f"engine:{row['playlist_key']}.m3u")

    def test_playlist_create_engine_unavailable_fails_truthfully(self):
        payload = {
            "name": "Offline Engine",
            "items": [{"id": 1, "path": "/data/media/music/a.mp3", "artist": "A", "title": "One"}],
            "desired_tracks": [{"artist": "A", "title": "One"}],
        }
        with patch.object(
            flask_app.beets_client,
            "export_playlist_m3u",
            side_effect=flask_app.BeetsUnavailableError("agent unavailable"),
        ), patch.object(flask_app, "_plex_settings", return_value={}):
            with flask_app.app.test_request_context(
                "/api/playlist/create",
                method="POST",
                data=json.dumps(payload),
                content_type="application/json",
            ):
                response, status = flask_app.playlist_create()

        data = response.get_json()
        self.assertEqual(status, 503)
        self.assertFalse(data.get("ok"), data)
        self.assertEqual(data.get("error_code"), "engine_unavailable")
        self.assertIn("Engine is unavailable", data.get("error", ""))
        self.assertEqual(list(self.manifests_dir.glob("*.json")), [])

    def test_engine_m3u_manifest_merge_uses_manifest_name(self):
        pl_id = flask_app._playlist_ensure_stable_id("Favorites")
        key = flask_app._playlist_key("Favorites", playlist_id=pl_id)
        flask_app._playlist_write_manifest(
            "Favorites",
            desired_tracks=[{"artist": "A", "title": "One"}],
            matched_tracks=[{"artist": "A", "title": "One", "path": "/data/media/music/a.mp3"}],
            playlist_id=pl_id,
        )
        with patch.object(flask_app.beets_client, "list_playlist_m3u", return_value={
            "ok": True,
            "playlists": [{"playlist_key": key, "key": key, "display_name": key}],
        }):
            records, diagnostics = flask_app._playlist_saved_playlist_records([])
        self.assertEqual(diagnostics, [])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["name"], "Favorites")
        self.assertTrue(records[0]["has_m3u"])
        self.assertTrue(records[0]["has_manifest"])

    def test_playlist_list_falls_back_to_engine_m3u_when_library_index_unavailable(self):
        pl_id = flask_app._playlist_ensure_stable_id("Favorites")
        key = flask_app._playlist_key("Favorites", playlist_id=pl_id)
        manifest_path = self.manifests_dir / f"{key}.playlist.json"
        manifest_path.write_text(json.dumps({
            "version": 2,
            "playlist_id": pl_id,
            "name": "Favorites",
        }), encoding="utf-8")

        with patch.object(flask_app.beets_client, "list_playlist_m3u", return_value={
            "ok": True,
            "playlists": [{"playlist_key": key, "key": key, "display_name": key}],
        }), patch.object(flask_app.beets_client, "read_playlist_m3u", return_value={
            "ok": True,
            "exists": True,
            "items": [{"path": "/music/track-one.mp3", "artist": "A", "title": "One"}],
        }), patch.object(
            flask_app,
            "_playlist_library_index",
            side_effect=flask_app.BeetsError("Database file musiclibrary.blb not found"),
        ):
            with flask_app.app.test_request_context("/api/playlists"):
                response = flask_app.list_playlists()

        data = response.get_json()
        self.assertEqual(len(data["playlists"]), 1)
        row = data["playlists"][0]
        self.assertEqual(row["name"], "Favorites")
        self.assertEqual(row["playlist_id"], pl_id)
        self.assertEqual(row["m3u_path"], f"engine:{key}.m3u")
        self.assertEqual(row["tracks"], 1)
        self.assertEqual(row["available"], 0)
        self.assertEqual(row["missing"], 1)

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

        # A real deployment's PLAYLIST_DOWNLOAD_ROOT (/data/torrents/music/...)
        # and MUSIC_LIBRARY_PATH (/data/media/music) are distinct directory
        # trees; this fixture's shared self.base_dir would otherwise put a
        # plain library track "under" the staging root by coincidence and
        # incorrectly trip the cross-playlist staging-entry containment
        # check (SEC-002 Wave 10 second final review) meant only for actual
        # staged-download paths, not ordinary library tracks.
        with patch.object(agent, "PLAYLIST_DOWNLOAD_ROOT", self.base_dir / "staging-root-unused"):
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
        code, data = _post_agent("/playlists/m3u/list", {})
        self.assertEqual(code, 200)
        self.assertTrue(data.get("ok"))
        self.assertTrue(any(p["key"] == "test_rock_123" for p in data.get("playlists", [])))
        self.assertNotIn("path", data.get("playlists", [])[0])

        # Delete M3U
        code, data = _post_agent("/playlists/m3u/delete", {
            "playlist_key": "test_rock_123"
        })
        self.assertEqual(code, 200)
        self.assertTrue(data.get("ok"))
        self.assertTrue(data.get("deleted"))

    def test_read_side_does_not_allocate_identity_for_legacy_name_only_manifest(self):
        """Legacy name-only manifest reads are side-effect free until a write path allocates identity."""
        legacy_manifest = self.manifests_dir / "Legacy Party.playlist.json"
        legacy_manifest.write_text(json.dumps({
            "name": "Legacy Party",
            "desired_tracks": [{"artist": "Daft Punk", "title": "One More Time"}],
        }), encoding="utf-8")

        manifest = flask_app._playlist_read_manifest("Legacy Party")
        self.assertEqual(manifest["name"], "Legacy Party")
        self.assertNotIn("playlist_id", manifest)
        self.assertFalse((flask_app.PLAYLIST_STATE_ROOT / "index.json").exists())
        self.assertTrue(flask_app._playlist_saved_playlist_exists("Legacy Party"))

    def test_state_corruption_fails_closed(self):
        index_file = flask_app.PLAYLIST_STATE_ROOT / "index.json"
        index_file.write_text("{not json", encoding="utf-8")
        with self.assertRaises(flask_app.PlaylistStateError) as cm:
            flask_app._playlist_resolve_stable_id("Broken")
        self.assertEqual(cm.exception.code, "playlist_state_corrupt")

    def test_identity_persist_failure_returns_no_usable_id(self):
        with patch.object(flask_app, "_playlist_atomic_json_replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                flask_app._playlist_ensure_stable_id("No Persist")
        self.assertFalse((flask_app.PLAYLIST_STATE_ROOT / "index.json").exists())
        self.assertFalse(any(self.manifests_dir.glob("*.playlist.json")))

    def test_concurrent_creation_preserves_all_index_updates(self):
        names = [f"Concurrent {i}" for i in range(12)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(flask_app._playlist_ensure_stable_id, names))
        self.assertEqual(len(ids), len(set(ids)))
        index_data = json.loads((flask_app.PLAYLIST_STATE_ROOT / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(set(ids), set(index_data.keys()))


class TestWave10SecondFinalReviewCorrectionTests(unittest.TestCase):
    """Regression tests for the corrections made during Claude's independent
    final review of PR #83 (SEC-002 Wave 10). Each test proves the specific
    vulnerable behavior the correction closes."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)
        self.data_dir = self.base_dir / "web-manager-data"
        self.playlists_dir = self.data_dir / "playlists"
        self.manifests_dir = self.playlists_dir / "manifests"
        self.exports_dir = self.playlists_dir / "exports"
        self.jobs_dir = self.playlists_dir / "jobs"
        self.membership_dir = self.playlists_dir / "membership"
        for d in (self.data_dir, self.playlists_dir, self.manifests_dir,
                  self.exports_dir, self.jobs_dir, self.membership_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.app_patches = [
            patch.object(flask_app, "WEB_MANAGER_DATA_DIR", self.data_dir),
            patch.object(flask_app, "PLAYLIST_STATE_ROOT", self.playlists_dir),
            patch.object(flask_app, "PLAYLIST_MANIFESTS_DIR", self.manifests_dir),
            patch.object(flask_app, "PLAYLIST_EXPORTS_DIR", self.exports_dir),
            patch.object(flask_app, "PLAYLIST_JOB_STATE_DIR", self.jobs_dir),
            patch.object(flask_app, "PLAYLIST_MEMBERSHIP_DIR", self.membership_dir),
        ]
        for p in self.app_patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.app_patches):
            p.stop()
        self.tmp_dir.cleanup()

    # -- Plex same-name deletion safety --------------------------------

    def test_plex_create_audio_playlist_returns_rating_key(self):
        """_plex_create_audio_playlist() must return the Plex ratingKey it
        computes, not discard it -- callers need it to delete/replace this
        exact playlist later without falling back to title matching."""
        with patch.object(flask_app, "_plex_request", return_value={
            "MediaContainer": {"Metadata": [{"ratingKey": "9999"}]}
        }):
            added, rating_key = flask_app._plex_create_audio_playlist("machine1", "Favorites", ["k1", "k2"])
        self.assertEqual(added, 2)
        self.assertEqual(rating_key, "9999")

    def test_plex_replace_prefers_stored_rating_key(self):
        """When a prior sync recorded a ratingKey, replace must delete by
        that exact key, never by title (which could hit another playlist)."""
        with patch.object(flask_app, "_plex_delete_playlist_by_rating_key", return_value=1) as mock_by_key, \
             patch.object(flask_app, "_plex_delete_playlist_by_title") as mock_by_title:
            result = flask_app._plex_replace_playlist_safely(
                "Favorites", "pl_aaa", {"last_plex": {"rating_key": "555"}})
        mock_by_key.assert_called_once_with("555", log=None)
        mock_by_title.assert_not_called()
        self.assertEqual(result["replaced"], 1)
        self.assertFalse(result["ambiguous"])

    def test_plex_replace_fails_closed_when_ambiguous(self):
        """No stored ratingKey and another live playlist shares this name:
        must NOT fall back to deleting-by-title (which could delete the
        other playlist's Plex playlist), must skip and report ambiguity."""
        flask_app._playlist_ensure_stable_id("Favorites", playlist_id="pl_" + "b" * 32)
        with patch.object(flask_app, "_plex_delete_playlist_by_rating_key") as mock_by_key, \
             patch.object(flask_app, "_plex_delete_playlist_by_title") as mock_by_title:
            result = flask_app._plex_replace_playlist_safely("Favorites", "pl_" + "a" * 32, {})
        mock_by_key.assert_not_called()
        mock_by_title.assert_not_called()
        self.assertTrue(result["ambiguous"])
        self.assertEqual(result["replaced"], 0)

    def test_plex_replace_falls_back_to_title_when_unambiguous(self):
        """No stored ratingKey, but no other live playlist shares this name
        either (legacy manifest predating this review, or genuinely the
        only playlist with this name): legacy title-based replace remains
        the safe fallback."""
        with patch.object(flask_app, "_plex_delete_playlist_by_rating_key") as mock_by_key, \
             patch.object(flask_app, "_plex_delete_playlist_by_title_unambiguous", return_value=(1, "")) as mock_by_title:
            result = flask_app._plex_replace_playlist_safely("Solo Playlist", "pl_" + "c" * 32, {})
        mock_by_key.assert_not_called()
        mock_by_title.assert_called_once_with("Solo Playlist", log=None)
        self.assertEqual(result["replaced"], 1)

    def test_other_live_pids_with_name_excludes_self_and_unrelated(self):
        pid_a = flask_app._playlist_ensure_stable_id("Shared Name")
        pid_b = "pl_" + "d" * 32
        flask_app._playlist_ensure_stable_id("Shared Name", playlist_id=pid_b)
        flask_app._playlist_ensure_stable_id("Unrelated")
        others = flask_app._playlist_other_live_pids_with_name("Shared Name", exclude_pid=pid_a)
        self.assertEqual(others, [pid_b])

    # -- Internal path/state disclosure ----------------------------------

    def test_state_error_payload_never_contains_a_path(self):
        """PlaylistStateError's client-facing payload must be a fixed, safe
        message keyed by .code -- never str(exc), which can (and, before
        this fix, did) embed a concrete container filesystem path."""
        exc = flask_app.PlaylistStateError(
            "playlist_state_unavailable",
            f"Playlist state directory is unavailable: {self.playlists_dir / 'manifests'}",
        )
        payload = flask_app._playlist_state_error_payload(exc)
        self.assertNotIn(str(self.playlists_dir), payload["error"])
        self.assertNotIn("/", payload["error"].replace("Playlist state storage is currently unavailable.", ""))
        self.assertEqual(payload["error_code"], "playlist_state_unavailable")
        self.assertEqual(flask_app._playlist_state_error_status(exc), 503)

    def test_ensure_state_dirs_failure_message_has_no_path(self):
        """The actual raise site must not embed the path either -- the safe
        mapping only helps if nothing upstream already leaked it into the
        exception's own message that some other caller might still log or
        surface directly."""
        with patch("pathlib.Path.mkdir", side_effect=OSError("Permission denied")):
            with self.assertRaises(flask_app.PlaylistStateError) as cm:
                flask_app._playlist_ensure_state_dirs()
        self.assertNotIn(str(self.playlists_dir), str(cm.exception))
        self.assertNotIn(str(self.data_dir), str(cm.exception))
        self.assertEqual(cm.exception.code, "playlist_state_unavailable")

    # -- Manifest corruption is not silently treated as absent -----------

    def test_read_manifest_raise_on_corrupt_distinguishes_from_absent(self):
        pid = flask_app._playlist_ensure_stable_id("Corrupt Test")
        key = flask_app._playlist_key("Corrupt Test", playlist_id=pid)
        manifest_path = self.manifests_dir / f"{key}.playlist.json"
        manifest_path.write_text("{not valid json", encoding="utf-8")

        # Lenient (default) callers still degrade gracefully.
        self.assertEqual(flask_app._playlist_read_manifest("Corrupt Test", playlist_id=pid), {})

        # The write path must not conflate corrupt-with-absent.
        with self.assertRaises(flask_app.PlaylistStateError) as cm:
            flask_app._playlist_read_manifest("Corrupt Test", playlist_id=pid, raise_on_corrupt=True)
        self.assertEqual(cm.exception.code, "manifest_corrupt")

    def test_write_manifest_refuses_to_overwrite_corrupt_manifest(self):
        """A write following a corrupt read must not silently synthesize a
        fresh manifest over the corrupt one, discarding whatever state
        (track history, Plex sync results, import progress) it still held."""
        pid = flask_app._playlist_ensure_stable_id("Corrupt Overwrite Test")
        key = flask_app._playlist_key("Corrupt Overwrite Test", playlist_id=pid)
        manifest_path = self.manifests_dir / f"{key}.playlist.json"
        original_bytes = b'{"broken": '
        manifest_path.write_bytes(original_bytes)

        with self.assertRaises(flask_app.PlaylistStateError) as cm:
            flask_app._playlist_write_manifest(
                "Corrupt Overwrite Test", desired_tracks=[], playlist_id=pid)
        self.assertEqual(cm.exception.code, "manifest_corrupt")
        self.assertEqual(manifest_path.read_bytes(), original_bytes, "corrupt manifest must survive untouched")

    # -- Index cleanup on delete -----------------------------------------

    def test_delete_retires_index_entry_so_recreate_is_unambiguous(self):
        """After deleting a playlist, its index entry must not remain as an
        orphan that a later same-named create could collide/be confused
        with (a stale entry sharing the display name can otherwise trip a
        false ambiguous_playlist error, or misattribute state)."""
        pid = flask_app._playlist_ensure_stable_id("Ephemeral")
        index_data = flask_app._playlist_load_index()
        self.assertIn(pid, index_data)

        with flask_app._PLAYLIST_STATE_LOCK:
            index_data = flask_app._playlist_load_index()
            del index_data[pid]
            flask_app._playlist_save_index(index_data)

        # Recreating the same name must not raise ambiguous_playlist and
        # must not resolve back to the retired id.
        new_pid = flask_app._playlist_ensure_stable_id("Ephemeral")
        self.assertNotEqual(new_pid, pid)
        others = flask_app._playlist_other_live_pids_with_name("Ephemeral", exclude_pid=new_pid)
        self.assertEqual(others, [])


class TestWave10EngineM3USecondFinalReviewTests(unittest.TestCase):
    """Real ControlAgentHandler.do_POST invocations for the engine-side M3U
    tempfile/TOCTOU corrections made during Claude's independent final
    review of PR #83."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.playlist_dir = Path(self.tmp_dir.name) / "playlists"
        self.playlist_dir.mkdir(parents=True, exist_ok=True)
        self.agent_patches = [
            patch.object(agent, "PLAYLIST_DIR", self.playlist_dir),
            patch.object(agent, "acquire_os_lock", return_value=None),
            patch.object(agent, "release_os_lock"),
        ]
        for p in self.agent_patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.agent_patches):
            p.stop()
        self.tmp_dir.cleanup()

    def test_export_does_not_follow_preexisting_symlink_at_tempfile_name(self):
        """A pre-existing symlink at the predicted tempfile name must not be
        followed and written through -- the outside target must survive."""
        if os.name == "nt":
            self.skipTest("os.O_NOFOLLOW / real symlink semantics require POSIX; covered by Docker/Linux runtime")
        outside = Path(self.tmp_dir.name) / "outside-sentinel.txt"
        outside.write_text("do not touch")
        key = "mykey"

        import uuid as uuid_mod
        fixed_suffix = "aaaaaaaa"
        planted = self.playlist_dir / f"{key}.{fixed_suffix}.tmp"
        planted.symlink_to(outside)

        with patch.object(uuid_mod, "uuid4", return_value=type("U", (), {"hex": fixed_suffix * 4})()):
            code, data = _post_agent("/playlists/export_m3u", {
                "playlist_key": key,
                "display_name": "My Playlist",
                "items": [],
            })
        self.assertEqual(outside.read_text(), "do not touch", "symlink target must not be written through")

    def test_read_revalidates_symlink_under_lock(self):
        """A symlinked M3U must be refused on read -- either by
        _playlist_m3u_path_for_key()'s own resolve()-then-containment check
        (Path.resolve() follows a leaf symlink, so one pointing outside
        PLAYLIST_DIR fails containment before the explicit
        _path_has_symlink_component() check is even reached) or by that
        explicit check itself. Either way the outside content must never
        reach the response."""
        if os.name == "nt":
            self.skipTest("real symlink semantics require POSIX; covered by Docker/Linux runtime")
        outside = Path(self.tmp_dir.name) / "outside-secret.m3u"
        outside.write_text("#EXTM3U\n/etc/passwd\n")
        key = "symlinked"
        link_path = self.playlist_dir / f"{key}.m3u"
        link_path.symlink_to(outside)

        code, data = _post_agent("/playlists/m3u/read", {"playlist_key": key})
        self.assertIn(code, (400, 403), data)
        self.assertIn(data.get("error_code"), ("invalid_playlist_key", "m3u_forbidden"))
        self.assertNotIn("/etc/passwd", json.dumps(data))

    def test_export_m3u_bounded_read_and_growth_race(self):
        """A file that grows past the byte bound between a stat and a read
        must still be refused -- the bound is enforced by the read itself,
        not a pre-read stat()."""
        key = "growing"
        target = self.playlist_dir / f"{key}.m3u"
        target.write_text("#EXTM3U\n", encoding="utf-8")
        with patch.object(agent, "PLAYLIST_M3U_MAX_BYTES", 16):
            target.write_text("#EXTM3U\n" + ("x" * 100) + "\n", encoding="utf-8")
            code, data = _post_agent("/playlists/m3u/read", {"playlist_key": key})
        self.assertEqual(code, 413, data)
        self.assertEqual(data.get("error_code"), "m3u_too_large")

    def test_export_m3u_refuses_cross_playlist_staging_entry(self):
        """An item path under a *different* playlist's own staging subtree
        must not be silently authorized into this playlist's M3U merely
        because both sit under the shared PLAYLIST_DOWNLOAD_ROOT."""
        with tempfile.TemporaryDirectory() as staging_root_dir:
            staging_root = Path(staging_root_dir)
            other_playlist_dir = staging_root / "other-playlist" / "downloads"
            other_playlist_dir.mkdir(parents=True)
            other_track = other_playlist_dir / "track.mp3"
            other_track.write_bytes(b"audio data belonging to another playlist")

            with patch.object(agent, "PLAYLIST_DOWNLOAD_ROOT", staging_root):
                code, data = _post_agent("/playlists/export_m3u", {
                    "playlist_key": "this-playlist",
                    "display_name": "This Playlist",
                    "items": [{"artist": "A", "title": "T", "path": str(other_track)}],
                })
        self.assertEqual(code, 200, data)
        m3u_path = self.playlist_dir / "this-playlist.m3u"
        if m3u_path.exists():
            self.assertNotIn(str(other_track), m3u_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
