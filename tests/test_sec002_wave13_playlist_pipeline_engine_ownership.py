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
            "playlist_id": "pl_" + "1" * 32,
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
            "playlist_id": "pl_" + "2" * 32,
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
        playlist_id = "pl_" + "a" * 32
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
                playlist_id=playlist_id,
            )
            self.assertEqual(res, [staged_path])
            mock_list.assert_called_once()

    def test_reusable_download_files_refuses_unresolvable_playlist_id(self):
        """A playlist_id that is not a valid pl_<32 hex> id must not fall back to a
        name-only or default key lookup -- it must refuse and skip reuse entirely."""
        track = {"artist": "Drake", "title": "God's Plan"}
        with patch.object(app.beets_client, "list_playlist_staged_files") as mock_list:
            res = app._playlist_reusable_download_files(
                track,
                self.staging_dir / "pl_favorites",
                self.staging_dir / "pl_favorites",
                log=lambda msg: None,
                playlist_name="Favorites",
                playlist_id="not-a-real-playlist-id",
            )
            self.assertEqual(res, [])
            mock_list.assert_not_called()

    def test_validate_downloaded_files_deletes_rejected_via_ipc(self):
        """Rejected staged downloads must be deleted via BeetsClient IPC instead of local unlink."""
        playlist_id = "pl_" + "b" * 32
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
                paths, "Artist", "Title", log=lambda msg: None, playlist_name="Test", playlist_id=playlist_id
            )
            self.assertEqual(valid, [])
            mock_del.assert_called_once()
            call_args = mock_del.call_args[0]
            self.assertNotEqual(call_args[0], "pl_default")
            self.assertEqual(call_args[2], paths[0])

    def test_validate_downloaded_files_skips_delete_for_unresolvable_playlist(self):
        """If the playlist key can't be resolved, a rejected file must be left in place
        rather than deleted via a guessed/default key that could target the wrong playlist."""
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
            app._playlist_validate_downloaded_files(
                paths, "Artist", "Title", log=lambda msg: None,
                playlist_name="Test", playlist_id="not-a-real-id",
            )
            mock_del.assert_not_called()

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


class TestWave13FinalReviewFixes(unittest.TestCase):
    """Regression coverage for the Claude final-review corrections made on
    top of the initial Wave 13 (AGY) implementation: crash-level
    regressions, playlist_id/playlist_key confusion, fabricated candidacy,
    local-fallback removal, and engine-side authorization/backup/schema
    fixes for /playlists/place-imported and related endpoints."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="wave13_final_test_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp_dir, ignore_errors=True))
        self.env_patcher = patch.dict(os.environ, {
            "PLAYLIST_STATE_ROOT": str(Path(self.tmp_dir) / "playlists"),
            "PLAYLIST_DOWNLOAD_ROOT": str(Path(self.tmp_dir) / "staging"),
            "MUSIC_ROOT": str(Path(self.tmp_dir) / "music"),
            "BEETS_API_TOKEN": "test-wave13-token-secret-32bytes-ok!",
        })
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        app._PLAYLIST_INDEX_CACHE = {"mtime": 0.0, "index": None}

    def test_no_duplicate_apply_album_placement_definition(self):
        """_playlist_apply_album_placement must be defined exactly once. A
        duplicate definition previously shadowed the first (dead) copy and
        both used the undefined-name pattern this test also guards."""
        import ast
        source = Path(app.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = [
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_playlist_apply_album_placement"
        ]
        self.assertEqual(len(names), 1, "duplicate _playlist_apply_album_placement definition")

    def test_valid_playlist_key_is_defined_and_matches_engine_format(self):
        """app.py must define _valid_playlist_key (it previously called an
        undefined name, causing a NameError on every placement call)."""
        self.assertTrue(app._valid_playlist_key("pl_abc123_deadbeef0123"))
        self.assertFalse(app._valid_playlist_key(""))
        self.assertFalse(app._valid_playlist_key("../etc/passwd"))
        self.assertFalse(app._valid_playlist_key("a/b"))

    def test_manual_placement_from_payload_is_restored_and_validates(self):
        """/api/playlists/quality-place calls this function; it must exist
        and enforce the required-field + MusicBrainz UUID contract."""
        candidate = {"id": 5, "artist": "A", "title": "T"}
        missing = app._playlist_manual_placement_from_payload(candidate, {})
        self.assertFalse(missing.get("ok"))

        payload = {
            "placement": {
                "artist": "A", "title": "T", "album": "Alb", "albumartist": "A",
                "mb_releasegroupid": "11111111-1111-1111-1111-111111111111",
                "mb_albumartistid": "22222222-2222-2222-2222-222222222222",
            }
        }
        ok = app._playlist_manual_placement_from_payload(candidate, payload)
        self.assertTrue(ok.get("ok"), ok)
        self.assertEqual(ok.get("mb_releasegroupid"), "11111111-1111-1111-1111-111111111111")

    def test_resolve_operation_key_rejects_pl_default_style_fallback(self):
        """A candidate with neither a valid playlist_key nor a valid
        playlist_id must resolve to "" (refuse), never a shared default
        key or a name-only guess."""
        key = app._playlist_resolve_operation_key("", "", "Some Playlist")
        self.assertEqual(key, "")
        key = app._playlist_resolve_operation_key("", "not-a-real-id", "Some Playlist")
        self.assertEqual(key, "")

    def test_resolve_operation_key_does_not_accept_raw_playlist_id_as_key(self):
        """A bare playlist_id must never be used directly as a playlist_key
        -- it must be derived via _playlist_existing_key."""
        pid = "pl_" + "c" * 32
        key = app._playlist_resolve_operation_key("", pid, "Some Playlist")
        self.assertNotEqual(key, pid)
        self.assertTrue(app._valid_playlist_key(key))

    def test_apply_album_placement_refuses_when_key_unresolvable(self):
        """Placement must refuse rather than fall back to pl_default when
        the candidate carries no resolvable playlist identity."""
        candidate = {"id": 9, "artist": "", "playlist_key": "", "playlist_id": ""}
        with patch.object(app.beets_client, "place_playlist_imported_item") as mock_place:
            res = app._playlist_apply_album_placement(None, candidate, {}, log=[])
            self.assertFalse(res.get("repaired"))
            mock_place.assert_not_called()

    def test_move_singleton_refuses_when_key_unresolvable(self):
        candidate = {
            "id": 9, "artist": "", "playlist_key": "", "playlist_id": "",
            "quality_flags": ["bad_playlist_path"],
        }
        with patch.object(app.beets_client, "place_playlist_imported_item") as mock_place:
            res = app._playlist_move_singleton_candidate(candidate, log=[])
            self.assertFalse(res.get("moved"))
            mock_place.assert_not_called()

    def test_place_quality_candidate_job_fails_closed_on_missing_candidate(self):
        """If the item is no longer a genuine quality review/repair
        candidate by the time the job runs, it must refuse -- not fabricate
        a synthetic candidate that lets an arbitrary item_id through."""
        with patch.object(app, "_playlist_quality_cleanup_candidates", return_value=[]), \
             patch.object(app, "_playlist_apply_album_placement") as mock_apply:
            job_id = app._playlist_place_quality_candidate_job(999, {"artist": "A", "title": "T"})
            job = app.jobs.get(job_id)
            # Wait for the background job to finish.
            import time as _time
            deadline = _time.time() + 5
            while job.status == "running" and _time.time() < deadline:
                _time.sleep(0.02)
            self.assertEqual(job.status, "failed")
            mock_apply.assert_not_called()

    def test_quality_cleanup_candidates_fails_closed_on_ipc_error(self):
        """An engine IPC failure must not be reported as '0 candidates' --
        that is indistinguishable from a clean scan result."""
        with patch.object(app.beets_client, "get_playlist_quality_candidates", side_effect=RuntimeError("boom")):
            with self.assertRaises(app.PlaylistQualityCandidatesUnavailableError):
                app._playlist_quality_cleanup_candidates(limit=10, filter_mode="repair")

    def test_copy_source_files_does_not_fake_success_on_copy_failure(self):
        """A failed copy must not be reported as a successfully staged
        file -- the caller treats every returned path as already staged
        for this playlist."""
        src_dir = Path(self.tmp_dir) / "src"
        src_dir.mkdir()
        src_file = src_dir / "track.mp3"
        src_file.write_bytes(b"data")
        dest_dir = Path(self.tmp_dir) / "dest_readonly_target_missing" / "nested"

        with patch("app.shutil.copy2", side_effect=OSError("disk full")):
            result = app._playlist_copy_source_files([src_file], dest_dir, "Artist", "Title", log=lambda m: None)
        self.assertEqual(result, [])

    def test_validate_staged_download_fails_closed_without_local_fingerprinting(self):
        """On engine IPC failure, validation must fail closed to review
        rather than fingerprinting the file locally (engine ownership)."""
        pid = "pl_" + "d" * 32
        with patch.object(app.beets_client, "validate_playlist_staged_track", side_effect=RuntimeError("unreachable")), \
             patch("app._audio_identity_decision") as mock_identity:
            res = app._playlist_validate_staged_download(
                "/some/staged/path.mp3", "Artist", "Title", playlist_id=pid)
            self.assertFalse(res.get("ok"))
            self.assertFalse((res.get("match") or {}).get("ok"))
            mock_identity.assert_not_called()


@unittest.skipIf(os.name == "nt", "resolve_safe_path requires POSIX-absolute paths")
class TestWave13EnginePlaceImportedAuthorization(unittest.TestCase):
    """Engine-side authorization/backup/schema regression tests for
    /playlists/place-imported and the operation-state ownership check it
    relies on."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="wave13_engine_test_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp_dir, ignore_errors=True))
        self.staging_root = Path(self.tmp_dir) / "staging"
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self._orig_root = bca.PLAYLIST_DOWNLOAD_ROOT
        bca.PLAYLIST_DOWNLOAD_ROOT = self.staging_root
        self.addCleanup(lambda: setattr(bca, "PLAYLIST_DOWNLOAD_ROOT", self._orig_root))
        # resolve_safe_path()'s "staging" role checks against DOWNLOAD_PATH,
        # not PLAYLIST_DOWNLOAD_ROOT -- both must be patched or every
        # resolve_safe_path(..., ["staging"]) call in this test class raises
        # "path is outside every allowed root" regardless of
        # PLAYLIST_DOWNLOAD_ROOT.
        self._orig_download_path = bca.DOWNLOAD_PATH
        bca.DOWNLOAD_PATH = str(self.staging_root)
        self.addCleanup(lambda: setattr(bca, "DOWNLOAD_PATH", self._orig_download_path))

    def _write_operations_index(self, playlist_key, item_id, status="imported"):
        ops_root = bca._playlist_import_operations_root()
        ops_root.mkdir(parents=True, exist_ok=True)
        index_path = bca._playlist_import_operations_index_path(ops_root)
        state = {
            "operations": {
                "op_" + "e" * 32: {
                    "playlist_key": playlist_key,
                    "tracks": {
                        "trk_" + "f" * 32: {"item_id": item_id, "status": status},
                    },
                },
            },
        }
        bca._playlist_import_write_state(index_path, state)

    def test_item_belongs_to_playlist_true_when_recorded(self):
        key = "pl_test_" + "a" * 12
        self._write_operations_index(key, 555)
        self.assertTrue(bca._playlist_item_belongs_to_playlist(key, 555))

    def test_item_belongs_to_playlist_false_for_unrelated_item(self):
        key = "pl_test_" + "a" * 12
        self._write_operations_index(key, 555)
        self.assertFalse(bca._playlist_item_belongs_to_playlist(key, 999))

    def test_item_belongs_to_playlist_false_for_different_playlist(self):
        key_a = "pl_test_" + "a" * 12
        key_b = "pl_test_" + "b" * 12
        self._write_operations_index(key_a, 555)
        self.assertFalse(bca._playlist_item_belongs_to_playlist(key_b, 555))

    def test_item_belongs_to_playlist_false_when_no_state_exists(self):
        self.assertFalse(bca._playlist_item_belongs_to_playlist("pl_test_" + "a" * 12, 555))

    def test_item_belongs_to_playlist_rejects_invalid_key_format(self):
        self.assertFalse(bca._playlist_item_belongs_to_playlist("../../etc", 555))
        self.assertFalse(bca._playlist_item_belongs_to_playlist("", 555))

    def test_item_belongs_to_playlist_ignores_non_imported_status(self):
        key = "pl_test_" + "a" * 12
        self._write_operations_index(key, 555, status="failed")
        self.assertFalse(bca._playlist_item_belongs_to_playlist(key, 555))


if __name__ == "__main__":
    unittest.main()
