import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as flask_app
from backend.beets_client import BeetsUnavailableError
PlaylistStateError = flask_app.PlaylistStateError


class Wave11PlaylistJobStateSecurityTests(unittest.TestCase):
    """SEC-002 Wave 11: Playlist Job-State Authority & Checkpoint Security.
    Verifies that web-manager-owned job checkpoints are stored safely under
    PLAYLIST_JOB_STATE_DIR, keyed by persistent playlist_id, redact secrets,
    isolate same-named playlists, and sanitize untrusted stored paths on resume.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data_dir = Path(self.tmp) / "web-manager-data"
        self.playlists_dir = self.data_dir / "playlists"
        self.manifests_dir = self.playlists_dir / "manifests"
        self.jobs_dir = self.playlists_dir / "jobs"
        self.exports_dir = self.playlists_dir / "exports"
        self.staging_dir = Path(self.tmp) / "staging"
        self.music_dir = Path(self.tmp) / "music"
        self.outside_dir = Path(self.tmp) / "outside"

        for d in (self.manifests_dir, self.jobs_dir, self.exports_dir, self.staging_dir, self.music_dir, self.outside_dir):
            d.mkdir(parents=True, exist_ok=True)

        (self.outside_dir / "sentinel.txt").write_text("OUTSIDE_SENTINEL", encoding="utf-8")
        (self.music_dir / "library.flac").write_text("MUSIC_SENTINEL", encoding="utf-8")

        self.patchers = [
            mock.patch.object(flask_app, "WEB_MANAGER_DATA_DIR", self.data_dir),
            mock.patch.object(flask_app, "PLAYLIST_STATE_ROOT", self.playlists_dir),
            mock.patch.object(flask_app, "PLAYLIST_MANIFESTS_DIR", self.manifests_dir),
            mock.patch.object(flask_app, "PLAYLIST_JOB_STATE_DIR", self.jobs_dir),
            mock.patch.object(flask_app, "PLAYLIST_EXPORTS_DIR", self.exports_dir),
            mock.patch.object(flask_app, "PLAYLIST_INDEX_PATH", self.playlists_dir / "index.json"),
            mock.patch.object(flask_app, "PLAYLIST_DOWNLOAD_ROOT", self.staging_dir),
            mock.patch.object(flask_app, "MUSIC_ROOT", self.music_dir),
            mock.patch.object(flask_app, "_PLAYLIST_INDEX_CACHE", {"mtime": 0.0, "data": {}}),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_checkpoint_saved_under_job_state_dir_atomic(self):
        pid = flask_app._playlist_ensure_stable_id("Road Trip")
        job_key = flask_app._playlist_job_key_payload("Road Trip", "url", "", [], [], playlist_id=pid)
        jid = flask_app._playlist_job_id_for_key(job_key)

        state = {
            "job_id": jid,
            "job_key": job_key,
            "playlist_id": pid,
            "name": "Road Trip",
            "status": "running",
            "phase": "downloading",
            "tracks": [{"artist": "Daft Punk", "title": "One More Time"}],
            "log": ["Downloading track 1"],
        }
        flask_app._playlist_save_job_state(state)

        ckpt_file = flask_app._playlist_job_state_path(jid)
        self.assertTrue(ckpt_file.exists())
        self.assertEqual(ckpt_file.parent.resolve(), self.jobs_dir.resolve())

        saved = json.loads(ckpt_file.read_text(encoding="utf-8"))
        self.assertEqual(saved.get("playlist_id"), pid)
        self.assertEqual(saved.get("status"), "running")

    def test_checkpoint_keyed_by_playlist_id(self):
        pid_a = flask_app._playlist_ensure_stable_id("Party Mix")
        pid_b = flask_app._playlist_ensure_stable_id("Party Mix")
        self.assertNotEqual(pid_a, pid_b)

        key_a = flask_app._playlist_job_key_payload("Party Mix", "url", "a", [], [], playlist_id=pid_a)
        key_b = flask_app._playlist_job_key_payload("Party Mix", "url", "b", [], [], playlist_id=pid_b)

        jid_a = flask_app._playlist_job_id_for_key(key_a)
        jid_b = flask_app._playlist_job_id_for_key(key_b)
        self.assertNotEqual(jid_a, jid_b)

        state_a = {"job_id": jid_a, "job_key": key_a, "playlist_id": pid_a, "name": "Party Mix", "status": "downloaded"}
        state_b = {"job_id": jid_b, "job_key": key_b, "playlist_id": pid_b, "name": "Party Mix", "status": "running"}

        flask_app._playlist_save_job_state(state_a)
        flask_app._playlist_save_job_state(state_b)

        rows_a = flask_app._playlist_saved_job_states_for_name("Party Mix", playlist_id=pid_a)
        rows_b = flask_app._playlist_saved_job_states_for_name("Party Mix", playlist_id=pid_b)

        self.assertEqual(len(rows_a), 1)
        self.assertEqual(rows_a[0]["job_id"], jid_a)
        self.assertEqual(len(rows_b), 1)
        self.assertEqual(rows_b[0]["job_id"], jid_b)

    def test_same_name_checkpoint_deletion_isolation(self):
        pid_a = flask_app._playlist_ensure_stable_id("Summer Hits")
        pid_b = flask_app._playlist_ensure_stable_id("Summer Hits")

        key_a = flask_app._playlist_job_key_payload("Summer Hits", "url", "a", [], [], playlist_id=pid_a)
        key_b = flask_app._playlist_job_key_payload("Summer Hits", "url", "b", [], [], playlist_id=pid_b)

        jid_a = flask_app._playlist_job_id_for_key(key_a)
        jid_b = flask_app._playlist_job_id_for_key(key_b)

        flask_app._playlist_save_job_state({"job_id": jid_a, "job_key": key_a, "playlist_id": pid_a, "name": "Summer Hits"})
        flask_app._playlist_save_job_state({"job_id": jid_b, "job_key": key_b, "playlist_id": pid_b, "name": "Summer Hits"})

        self.assertTrue(flask_app._playlist_job_state_path(jid_a).exists())
        self.assertTrue(flask_app._playlist_job_state_path(jid_b).exists())

        with mock.patch.object(flask_app.beets_client, "delete_playlist_m3u", return_value={"ok": True, "deleted": True}), \
             flask_app.app.test_request_context("/api/playlists/Summer%20Hits", method="DELETE", data=json.dumps({"playlist_id": pid_a}), content_type="application/json"):
            flask_app.playlist_delete("Summer Hits")

        remaining_b = flask_app._playlist_saved_job_states_for_name("Summer Hits", playlist_id=pid_b)
        self.assertEqual(len(remaining_b), 1)
        self.assertEqual(remaining_b[0]["job_id"], jid_b)

    def test_resumed_checkpoint_untrusted_stale_and_malicious_paths_sanitized(self):
        clean_name = "Workout"
        pid = flask_app._playlist_ensure_stable_id(clean_name)

        outside_file = str(self.outside_dir / "sentinel.txt")
        music_file = str(self.music_dir / "library.flac")
        traversal_file = str(self.staging_dir / ".." / "outside" / "sentinel.txt")

        trk1 = {"artist": "Artist1", "title": "Title1"}
        trk2 = {"artist": "Artist2", "title": "Title2"}
        trk3 = {"artist": "Artist3", "title": "Title3"}

        flask_app._playlist_store_track_state(clean_name, trk1, "downloaded", staged_path=outside_file)
        flask_app._playlist_store_track_state(clean_name, trk2, "downloaded", staged_path=music_file)
        flask_app._playlist_store_track_state(clean_name, trk3, "downloaded", staged_path=traversal_file)

        logger_messages = []
        def mock_log(msg):
            logger_messages.append(msg)

        reconciled = flask_app._playlist_reconcile_staged_files(clean_name, self.staging_dir, [trk1, trk2, trk3], mock_log)
        self.assertEqual(reconciled, 3)

        states = flask_app._playlist_manifest_track_states(clean_name)
        for key in states:
            self.assertEqual(states[key]["status"], "pending")
            self.assertEqual(states[key]["staged_path"], "")

        self.assertEqual((self.outside_dir / "sentinel.txt").read_text(encoding="utf-8"), "OUTSIDE_SENTINEL")
        self.assertEqual((self.music_dir / "library.flac").read_text(encoding="utf-8"), "MUSIC_SENTINEL")

    def test_checkpoint_secret_redaction(self):
        jid = "pl-testredact12345"
        state = {
            "job_id": jid,
            "name": "Secret Test",
            "plex_token": "SUPER_SECRET_PLEX_TOKEN_12345",
            "api_key": "SLSKD_API_KEY_SECRET",
            "nested": {"password": "MY_PRIVATE_PASSWORD"},
            "log": ["Connecting with token=SUPER_SECRET_PLEX_TOKEN_12345 to server"],
        }
        flask_app._playlist_save_job_state(state)

        saved = json.loads(flask_app._playlist_job_state_path(jid).read_text(encoding="utf-8"))
        self.assertEqual(saved.get("plex_token"), "[REDACTED]")
        self.assertEqual(saved.get("api_key"), "[REDACTED]")
        self.assertEqual(saved.get("nested", {}).get("password"), "[REDACTED]")
        self.assertNotIn("SUPER_SECRET_PLEX_TOKEN_12345", json.dumps(saved))

    def test_corrupt_checkpoint_json_fails_safely(self):
        pid = flask_app._playlist_ensure_stable_id("Corrupt Test")
        corrupt_path = self.jobs_dir / "pl-corrupt12345.json"
        corrupt_path.write_text("{this is not valid json...", encoding="utf-8")

        rows = flask_app._playlist_saved_job_states_for_name("Corrupt Test", playlist_id=pid)
        self.assertEqual(rows, [])


class Wave11PlexStableIdentitySecurityTests(unittest.TestCase):
    """SEC-002 Wave 11: Plex Stable Playlist Identity Security.
    Verifies that ratingKey is persisted and used as authoritative for Plex
    updates/deletions, unambiguous title fallback occurs only when 1 match exists,
    and same-named app playlists cannot cross-target or combine verification counts.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data_dir = Path(self.tmp) / "web-manager-data"
        self.playlists_dir = self.data_dir / "playlists"
        self.manifests_dir = self.playlists_dir / "manifests"
        self.jobs_dir = self.playlists_dir / "jobs"
        self.exports_dir = self.playlists_dir / "exports"

        for d in (self.manifests_dir, self.jobs_dir, self.exports_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.patchers = [
            mock.patch.object(flask_app, "WEB_MANAGER_DATA_DIR", self.data_dir),
            mock.patch.object(flask_app, "PLAYLIST_STATE_ROOT", self.playlists_dir),
            mock.patch.object(flask_app, "PLAYLIST_MANIFESTS_DIR", self.manifests_dir),
            mock.patch.object(flask_app, "PLAYLIST_JOB_STATE_DIR", self.jobs_dir),
            mock.patch.object(flask_app, "PLAYLIST_EXPORTS_DIR", self.exports_dir),
            mock.patch.object(flask_app, "PLAYLIST_INDEX_PATH", self.playlists_dir / "index.json"),
            mock.patch.object(flask_app, "_PLAYLIST_INDEX_CACHE", {"mtime": 0.0, "data": {}}),
            mock.patch.object(flask_app, "_plex_settings", return_value={"token": "test-token", "url": "http://plex:32400"}),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_plex_delete_uses_stored_rating_key(self):
        pid = flask_app._playlist_ensure_stable_id("Chill Hits")
        manifest = {
            "name": "Chill Hits",
            "playlist_id": pid,
            "last_plex": {"rating_key": "998877", "complete": True},
        }
        flask_app._playlist_write_manifest("Chill Hits", [], playlist_id=pid)
        flask_app._playlist_replace_manifest("Chill Hits", manifest)

        with mock.patch.object(flask_app, "_plex_request") as mock_req:
            mock_req.return_value = {}
            deleted = flask_app._plex_delete_playlist_by_rating_key("998877")
            self.assertEqual(deleted, 1)
            mock_req.assert_called_once_with("/playlists/998877", method="DELETE", timeout=10)

    def test_plex_unambiguous_title_fallback_succeeds_for_single_match(self):
        mock_playlists = [
            {"title": "Focus Zone", "ratingKey": "12345", "smart": "0"},
            {"title": "Workout", "ratingKey": "67890", "smart": "0"},
        ]
        with mock.patch.object(flask_app, "_plex_audio_playlists", return_value=mock_playlists), \
             mock.patch.object(flask_app, "_plex_request") as mock_req:
            mock_req.return_value = {}
            deleted, err = flask_app._plex_delete_playlist_by_title_unambiguous("Focus Zone")
            self.assertEqual(deleted, 1)
            self.assertEqual(err, "")
            mock_req.assert_called_once_with("/playlists/12345", method="DELETE", timeout=10)

    def test_plex_ambiguous_title_fallback_fails_closed(self):
        mock_playlists = [
            {"title": "Focus Zone", "ratingKey": "11111", "smart": "0"},
            {"title": "Focus Zone", "ratingKey": "22222", "smart": "0"},
        ]
        with mock.patch.object(flask_app, "_plex_audio_playlists", return_value=mock_playlists), \
             mock.patch.object(flask_app, "_plex_request") as mock_req:
            deleted, err = flask_app._plex_delete_playlist_by_title_unambiguous("Focus Zone")
            self.assertEqual(deleted, 0)
            self.assertEqual(err, "ambiguous_plex_playlist")
            mock_req.assert_not_called()

    def test_same_name_app_playlists_isolated_in_plex(self):
        pid_a = flask_app._playlist_ensure_stable_id("Favorites")
        pid_b = flask_app._playlist_ensure_stable_id("Favorites")

        manifest_a = {"name": "Favorites", "playlist_id": pid_a, "last_plex": {"rating_key": "10001"}}
        manifest_b = {"name": "Favorites", "playlist_id": pid_b, "last_plex": {"rating_key": "10002"}}

        flask_app._playlist_write_manifest("Favorites", [], playlist_id=pid_a)
        flask_app._playlist_replace_manifest("Favorites", manifest_a)

        flask_app._playlist_write_manifest("Favorites", [], playlist_id=pid_b)
        flask_app._playlist_replace_manifest("Favorites", manifest_b)

        with mock.patch.object(flask_app, "_plex_delete_playlist_by_rating_key") as mock_del:
            mock_del.return_value = 1
            res_a = flask_app._plex_replace_playlist_safely("Favorites", pid_a, manifest_a)
            self.assertEqual(res_a["replaced"], 1)
            self.assertFalse(res_a["ambiguous"])
            mock_del.assert_called_once_with("10001", log=None)

    def test_plex_verification_count_measures_target_playlist_only(self):
        mock_playlists = [
            {"title": "Summer Vibes", "ratingKey": "70001"},
            {"title": "Summer Vibes", "ratingKey": "70002"},
        ]
        def mock_items_req(path, timeout=20):
            if "70001" in path:
                return {"MediaContainer": {"Metadata": [{"ratingKey": "t1"}, {"ratingKey": "t2"}]}}
            if "70002" in path:
                return {"MediaContainer": {"Metadata": [{"ratingKey": "t3"}, {"ratingKey": "t4"}, {"ratingKey": "t5"}]}}
            return {}

        with mock.patch.object(flask_app, "_plex_audio_playlists", return_value=mock_playlists), \
             mock.patch.object(flask_app, "_plex_request", side_effect=mock_items_req):

            keys_target, rkey, am = flask_app._plex_playlist_rating_keys_by_key_or_title(rating_key="70001", title="Summer Vibes")
            self.assertEqual(keys_target, ["t1", "t2"])
            self.assertEqual(rkey, "70001")
            self.assertFalse(am)

            keys_ambig, rkey_ambig, am_ambig = flask_app._plex_playlist_rating_keys_by_key_or_title(rating_key="", title="Summer Vibes")
            self.assertEqual(keys_ambig, [])
            self.assertTrue(am_ambig)


if __name__ == "__main__":
    unittest.main()
