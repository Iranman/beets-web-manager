import json
import os
import re
import shutil
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

import app as flask_app
import backend.beets_control_agent as agent
from backend.beets_client import BeetsUnavailableError

PlaylistStateError = flask_app.PlaylistStateError


def _post_agent(path: str, payload: dict):
    body = json.dumps(payload).encode("utf-8")
    handler = agent.ControlAgentHandler.__new__(agent.ControlAgentHandler)
    handler.path = path
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = BytesIO(body)
    handler._authenticate = lambda: True
    responses = []
    handler._send_json = lambda code, data: responses.append((code, data))
    handler.do_POST()
    if len(responses) != 1:
        raise AssertionError(f"expected exactly one response, got {responses!r}")
    return responses[0]


class Wave11PlaylistJobStateSecurityTests(unittest.TestCase):
    """SEC-002 Wave 11: Playlist Job-State Authority & Checkpoint Security."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data_dir = Path(self.tmp) / "web-manager-data"
        self.playlists_dir = self.data_dir / "playlists"
        self.manifests_dir = self.playlists_dir / "manifests"
        self.jobs_dir = self.playlists_dir / "jobs"
        self.exports_dir = self.playlists_dir / "exports"
        self.membership_dir = self.playlists_dir / "membership"
        self.staging_dir = Path(self.tmp) / "staging"
        self.music_dir = Path(self.tmp) / "music"
        self.outside_dir = Path(self.tmp) / "outside"

        for d in (
            self.manifests_dir,
            self.jobs_dir,
            self.exports_dir,
            self.membership_dir,
            self.staging_dir,
            self.music_dir,
            self.outside_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

        (self.outside_dir / "sentinel.txt").write_text("OUTSIDE_SENTINEL", encoding="utf-8")
        (self.music_dir / "library.flac").write_text("MUSIC_SENTINEL", encoding="utf-8")

        self.patchers = [
            mock.patch.object(flask_app, "WEB_MANAGER_DATA_DIR", self.data_dir),
            mock.patch.object(flask_app, "PLAYLIST_STATE_ROOT", self.playlists_dir),
            mock.patch.object(flask_app, "PLAYLIST_MANIFESTS_DIR", self.manifests_dir),
            mock.patch.object(flask_app, "PLAYLIST_JOB_STATE_DIR", self.jobs_dir),
            mock.patch.object(flask_app, "PLAYLIST_EXPORTS_DIR", self.exports_dir),
            mock.patch.object(flask_app, "PLAYLIST_MEMBERSHIP_DIR", self.membership_dir),
            mock.patch.object(flask_app, "PLAYLIST_INDEX_PATH", self.playlists_dir / "index.json"),
            mock.patch.object(flask_app, "PLAYLIST_DOWNLOAD_ROOT", self.staging_dir),
            mock.patch.object(flask_app, "MUSIC_ROOT", self.music_dir),
            mock.patch.object(flask_app, "_PLAYLIST_INDEX_CACHE", {"mtime": 0.0, "data": {}}),
            mock.patch.object(flask_app, "_pl_dl_jobs", {}),
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
            "playlist_key": job_key["playlist_key"],
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
        self.assertEqual(saved.get("playlist_key"), job_key["playlist_key"])
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

    def test_playlist_id_and_playlist_key_are_not_interchangeable(self):
        pid = flask_app._playlist_ensure_stable_id("Identity Test")
        job_key = flask_app._playlist_job_key_payload("Identity Test", "url", "", [], [], playlist_id=pid)
        playlist_key = flask_app._playlist_key("Identity Test", playlist_id=pid, allocate=False)

        self.assertRegex(pid, r"^pl_[0-9a-f]{32}$")
        self.assertNotEqual(playlist_key, pid)
        self.assertEqual(job_key["playlist_id"], pid)
        self.assertEqual(job_key["playlist_key"], playlist_key)
        self.assertNotEqual(job_key["playlist_id"], job_key["playlist_key"])

    def test_playlist_download_route_resolves_persisted_id_without_manual_payload_id(self):
        pid = flask_app._playlist_ensure_stable_id("Production Path")
        expected_key = flask_app._playlist_key("Production Path", playlist_id=pid, allocate=False)
        payload = {
            "name": "Production Path",
            "tracks": [{"artist": "Artist", "title": "Song"}],
            "all_tracks": [{"artist": "Artist", "title": "Song"}],
            "download_only": True,
        }
        fake_job = type("FakeJob", (), {"job_id": "jobs-1"})()
        with mock.patch.object(flask_app.beets_client, "ensure_playlist_staging", return_value={"ok": True}), \
             mock.patch.object(flask_app.jobs, "start_python", return_value=fake_job), \
             flask_app.app.test_request_context("/api/playlist/download", method="POST", json=payload):
            response = flask_app.playlist_download()

        data = response.get_json()
        self.assertTrue(data["ok"])
        saved = flask_app._playlist_load_job_state(data["job_id"], strict=True)
        self.assertEqual(saved["playlist_id"], pid)
        self.assertEqual(saved["playlist_key"], expected_key)
        self.assertEqual(saved["job_key"]["playlist_id"], pid)
        self.assertEqual(saved["job_key"]["playlist_key"], expected_key)

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
        self.assertFalse(flask_app._playlist_job_state_path(jid_a).exists())

    def test_resumed_checkpoint_untrusted_paths_are_revalidated_engine_side(self):
        clean_name = "Workout"
        pid = flask_app._playlist_ensure_stable_id(clean_name)

        paths = [
            str(self.outside_dir / "sentinel.txt"),
            str(self.music_dir / "library.flac"),
            str(self.staging_dir / ".." / "outside" / "sentinel.txt"),
        ]
        tracks = [
            {"artist": "Artist1", "title": "Title1"},
            {"artist": "Artist2", "title": "Title2"},
            {"artist": "Artist3", "title": "Title3"},
        ]
        for track, staged_path in zip(tracks, paths):
            flask_app._playlist_store_track_state(clean_name, track, "downloaded", playlist_id=pid, staged_path=staged_path)

        calls = []
        def fake_inspect(playlist_key, track_id, requested_path):
            calls.append((playlist_key, track_id, requested_path))
            return {"ok": True, "exists": False, "authorized": False, "status": "invalid_path"}

        logger_messages = []
        with mock.patch.object(flask_app.beets_client, "inspect_playlist_staged_track", side_effect=fake_inspect):
            reconciled = flask_app._playlist_reconcile_staged_files(
                clean_name, self.staging_dir, tracks, logger_messages.append, playlist_id=pid)
        self.assertEqual(reconciled, 3)
        self.assertEqual(len(calls), 3)

        states = flask_app._playlist_manifest_track_states(clean_name, playlist_id=pid)
        for key in states:
            self.assertEqual(states[key]["status"], "pending")
            self.assertEqual(states[key]["staged_path"], "")
            self.assertEqual(states[key]["failure_reason"], "stale_or_missing")

        self.assertEqual((self.outside_dir / "sentinel.txt").read_text(encoding="utf-8"), "OUTSIDE_SENTINEL")
        self.assertEqual((self.music_dir / "library.flac").read_text(encoding="utf-8"), "MUSIC_SENTINEL")

    def test_engine_unavailable_does_not_mark_checkpoint_missing(self):
        clean_name = "Engine Down"
        pid = flask_app._playlist_ensure_stable_id(clean_name)
        track = {"artist": "Artist", "title": "Title"}
        flask_app._playlist_store_track_state(
            clean_name, track, "downloaded", playlist_id=pid, staged_path="/data/staging/track.mp3")
        logs = []
        with mock.patch.object(
            flask_app.beets_client,
            "inspect_playlist_staged_track",
            side_effect=BeetsUnavailableError("engine down"),
        ):
            reconciled = flask_app._playlist_reconcile_staged_files(
                clean_name, Path(), [track], logs.append, playlist_id=pid)
        self.assertEqual(reconciled, 0)
        state = flask_app._playlist_manifest_track_states(clean_name, playlist_id=pid)
        row = next(iter(state.values()))
        self.assertEqual(row["status"], "downloaded")
        self.assertIn("engine staging validation unavailable", "\n".join(logs))

    def test_cross_playlist_resume_rejects_other_playlist_staging(self):
        pid_a = flask_app._playlist_ensure_stable_id("Playlist A")
        pid_b = flask_app._playlist_ensure_stable_id("Playlist B")
        key_a = flask_app._playlist_key("Playlist A", playlist_id=pid_a, allocate=False)
        key_b = flask_app._playlist_key("Playlist B", playlist_id=pid_b, allocate=False)
        victim = self.staging_dir / key_b / "downloads" / "b.mp3"
        victim.parent.mkdir(parents=True)
        victim.write_bytes(b"audio")
        track = {"artist": "B", "title": "Song"}
        flask_app._playlist_store_track_state("Playlist A", track, "downloaded", playlist_id=pid_a, staged_path=str(victim))

        def fake_inspect(playlist_key, track_id, requested_path):
            self.assertEqual(playlist_key, key_a)
            self.assertNotEqual(playlist_key, key_b)
            return {"ok": True, "exists": True, "authorized": False, "status": "outside_playlist_staging"}

        with mock.patch.object(flask_app.beets_client, "inspect_playlist_staged_track", side_effect=fake_inspect):
            reconciled = flask_app._playlist_reconcile_staged_files(
                "Playlist A", Path(), [track], lambda msg: None, playlist_id=pid_a)
        self.assertEqual(reconciled, 1)
        self.assertTrue(victim.exists())
        row = next(iter(flask_app._playlist_manifest_track_states("Playlist A", playlist_id=pid_a).values()))
        self.assertEqual(row["status"], "pending")

    def test_checkpoint_secret_redaction(self):
        jid = "pl-" + "a" * 20
        state = {
            "job_id": jid,
            "name": "Secret Test",
            "plex_token": "SUPER_SECRET_PLEX_TOKEN_12345",
            "api_key": "SLSKD_API_KEY_SECRET",
            "Authorization": "Bearer SUPER_SECRET_AUTH_VALUE",
            "Cookie": "session=SUPER_SECRET_COOKIE_VALUE",
            "Set-Cookie": "session=SUPER_SECRET_SET_COOKIE_VALUE",
            "signed_url": "https://example.invalid/file?X-Amz-Signature=SUPER_SECRET_SIGNATURE",
            "headers": {"Authorization": "Bearer HEADER_SECRET", "Cookie": "HEADER_COOKIE=1"},
            "nested": {"password": "MY_PRIVATE_PASSWORD"},
            "list": ["Authorization: Bearer LIST_SECRET", "safe operational note"],
            "log": ["Connecting with token=SUPER_SECRET_PLEX_TOKEN_12345 to server"],
        }
        flask_app._playlist_save_job_state(state)

        saved = json.loads(flask_app._playlist_job_state_path(jid).read_text(encoding="utf-8"))
        saved_text = json.dumps(saved)
        self.assertEqual(saved.get("plex_token"), "[REDACTED]")
        self.assertEqual(saved.get("api_key"), "[REDACTED]")
        self.assertEqual(saved.get("Authorization"), "[REDACTED]")
        self.assertEqual(saved.get("headers", {}).get("Authorization"), "[REDACTED]")
        self.assertEqual(saved.get("nested", {}).get("password"), "[REDACTED]")
        self.assertIn("safe operational note", saved_text)
        for secret in (
            "SUPER_SECRET_PLEX_TOKEN_12345",
            "SUPER_SECRET_AUTH_VALUE",
            "SUPER_SECRET_COOKIE_VALUE",
            "SUPER_SECRET_SET_COOKIE_VALUE",
            "SUPER_SECRET_SIGNATURE",
            "HEADER_SECRET",
            "HEADER_COOKIE",
            "LIST_SECRET",
        ):
            self.assertNotIn(secret, saved_text)

    def test_corrupt_checkpoint_json_fails_safely_for_strict_load(self):
        pid = flask_app._playlist_ensure_stable_id("Corrupt Test")
        corrupt_path = self.jobs_dir / ("pl-" + "b" * 20 + ".json")
        corrupt_path.write_text("{this is not valid json...", encoding="utf-8")

        rows = flask_app._playlist_saved_job_states_for_name("Corrupt Test", playlist_id=pid)
        self.assertEqual(rows, [])
        with self.assertRaises(PlaylistStateError) as cm:
            flask_app._playlist_saved_job_states_for_name("Corrupt Test", playlist_id=pid, strict=True)
        self.assertEqual(cm.exception.code, "checkpoint_corrupt")

    def test_unrelated_corrupt_checkpoint_does_not_block_strict_resume_of_another_playlist(self):
        """SEC-002 Wave 11 second final review: a corrupt checkpoint
        belonging to playlist B must not deny/DoS a strict-mode resume of
        an unrelated playlist A that has its own valid checkpoint."""
        pid_a = flask_app._playlist_ensure_stable_id("Playlist A")
        job_key_a = flask_app._playlist_job_key_payload("Playlist A", "url", "", [], [], playlist_id=pid_a)
        jid_a = flask_app._playlist_job_id_for_key(job_key_a)
        flask_app._playlist_save_job_state({
            "job_id": jid_a,
            "job_key": job_key_a,
            "playlist_id": pid_a,
            "playlist_key": job_key_a["playlist_key"],
            "name": "Playlist A",
            "status": "running",
            "tracks": [],
        })

        # Playlist B's checkpoint file is corrupt -- unrelated to A.
        corrupt_path = self.jobs_dir / ("pl-" + "c" * 20 + ".json")
        corrupt_path.write_text("{not valid json at all...", encoding="utf-8")

        rows = flask_app._playlist_saved_job_states_for_name("Playlist A", playlist_id=pid_a, strict=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["job_id"], jid_a)
        # The corrupt file must survive untouched -- it is not this call's
        # job to clean it up, only to not let it block an unrelated playlist.
        self.assertTrue(corrupt_path.exists())

    def test_invalid_job_id_grammar_rejected(self):
        invalid_ids = [
            "../outside",
            "/tmp/outside",
            "pl-abc/def",
            "pl-abc\\def",
            "pl-abc\x00def",
            "pl-" + "a" * 200,
            "pl_" + "a" * 32,
        ]
        for jid in invalid_ids:
            with self.subTest(jid=jid):
                self.assertFalse(flask_app._playlist_valid_job_id(jid))
                with self.assertRaises(PlaylistStateError):
                    flask_app._playlist_job_state_path(jid)

    def test_job_state_delete_unlinks_symlink_without_touching_target(self):
        jid = "pl-" + "c" * 20
        target = self.outside_dir / "sentinel.txt"
        link = self.jobs_dir / f"{jid}.json"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not supported")
        self.assertTrue(flask_app._playlist_delete_job_state(jid))
        self.assertFalse(link.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "OUTSIDE_SENTINEL")

    def test_job_state_delete_rejects_symlinked_parent(self):
        jid = "pl-" + "d" * 20
        symlink_jobs = Path(self.tmp) / "jobs-link"
        try:
            os.symlink(self.outside_dir, symlink_jobs, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not supported")
        with mock.patch.object(flask_app, "PLAYLIST_JOB_STATE_DIR", symlink_jobs):
            with self.assertRaises(PlaylistStateError):
                flask_app._playlist_delete_job_state(jid)
        self.assertEqual((self.outside_dir / "sentinel.txt").read_text(encoding="utf-8"), "OUTSIDE_SENTINEL")

    def test_job_state_load_refuses_symlinked_checkpoint(self):
        """SEC-002 Wave 11 second final review: reads must refuse a
        symlinked checkpoint leaf the same way writes/deletes already do --
        Path.read_text() otherwise follows it and returns arbitrary
        outside content as if it were this playlist's own checkpoint."""
        jid = "pl-" + "e" * 20
        secret = self.outside_dir / "checkpoint-secret.json"
        secret.write_text(json.dumps({"leaked": "OUTSIDE_SENTINEL"}), encoding="utf-8")
        link = self.jobs_dir / f"{jid}.json"
        try:
            os.symlink(secret, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not supported")

        state = flask_app._playlist_load_job_state(jid)
        self.assertEqual(state, {}, "symlinked checkpoint must not be read")
        with self.assertRaises(PlaylistStateError):
            flask_app._playlist_load_job_state(jid, strict=True)

    def test_job_states_for_name_skips_symlinked_checkpoint(self):
        """Same guarantee via the glob-based lister used by resume/delete."""
        pid = flask_app._playlist_ensure_stable_id("Symlink Scan Test")
        secret = self.outside_dir / "checkpoint-secret2.json"
        secret.write_text(json.dumps({"leaked": "OUTSIDE_SENTINEL"}), encoding="utf-8")
        link = self.jobs_dir / ("pl-" + "f" * 20 + ".json")
        try:
            os.symlink(secret, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not supported")

        rows = flask_app._playlist_saved_job_states_for_name("Symlink Scan Test", playlist_id=pid)
        self.assertEqual(rows, [])
        self.assertFalse(any(row.get("leaked") for row in rows))


class Wave11ControlAgentStagingInspectTests(unittest.TestCase):
    def setUp(self):
        if os.name == "nt":
            self.skipTest("resolve_safe_path requires POSIX-absolute input; covered by Docker/Linux runtime")
        self.tmp = tempfile.mkdtemp()
        self.staging = Path(self.tmp) / "staging"
        self.music = Path(self.tmp) / "music"
        self.staging.mkdir(parents=True)
        self.music.mkdir(parents=True)
        self.patchers = [
            mock.patch.object(agent, "PLAYLIST_DOWNLOAD_ROOT", self.staging),
            mock.patch.object(agent, "MUSIC_ROOT", self.music),
            mock.patch.object(agent, "DOWNLOAD_PATH", str(self.staging)),
            mock.patch.object(agent, "MUSIC_LIBRARY_PATH", str(self.music)),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_inspect_authorizes_only_calling_playlist_subtree(self):
        key_a = "pl_a_key"
        key_b = "pl_b_key"
        a_file = self.staging / key_a / "downloads" / "a.mp3"
        b_file = self.staging / key_b / "downloads" / "b.mp3"
        a_file.parent.mkdir(parents=True)
        b_file.parent.mkdir(parents=True)
        a_file.write_bytes(b"audio")
        b_file.write_bytes(b"audio")

        code, data = _post_agent("/playlists/staging/inspect-track", {
            "playlist_key": key_a,
            "track_id": "track-a",
            "requested_path": str(a_file),
        })
        self.assertEqual(code, 200)
        self.assertTrue(data["authorized"])
        self.assertTrue(data["exists"])
        self.assertEqual(data["status"], "ok")
        self.assertNotIn("path", data)

        code, data = _post_agent("/playlists/staging/inspect-track", {
            "playlist_key": key_a,
            "track_id": "track-b",
            "requested_path": str(b_file),
        })
        self.assertEqual(code, 200)
        self.assertFalse(data["authorized"])
        self.assertFalse(data["exists"])
        self.assertEqual(data["status"], "outside_playlist_staging")
        self.assertTrue(b_file.exists())

    def test_inspect_reports_missing_and_non_audio_without_throwing(self):
        key = "pl_key"
        missing = self.staging / key / "downloads" / "missing.mp3"
        text_file = self.staging / key / "downloads" / "note.txt"
        text_file.parent.mkdir(parents=True)
        text_file.write_text("not audio", encoding="utf-8")

        code, data = _post_agent("/playlists/staging/inspect-track", {
            "playlist_key": key,
            "track_id": "missing",
            "requested_path": str(missing),
        })
        self.assertEqual(code, 200)
        self.assertFalse(data["authorized"])
        self.assertFalse(data["exists"])
        self.assertEqual(data["status"], "missing")

        code, data = _post_agent("/playlists/staging/inspect-track", {
            "playlist_key": key,
            "track_id": "txt",
            "requested_path": str(text_file),
        })
        self.assertEqual(code, 200)
        self.assertFalse(data["authorized"])
        self.assertTrue(data["exists"])
        self.assertEqual(data["status"], "non_audio")


class Wave11PlexStableIdentitySecurityTests(unittest.TestCase):
    """SEC-002 Wave 11: Plex Stable Playlist Identity Security."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data_dir = Path(self.tmp) / "web-manager-data"
        self.playlists_dir = self.data_dir / "playlists"
        self.manifests_dir = self.playlists_dir / "manifests"
        self.jobs_dir = self.playlists_dir / "jobs"
        self.exports_dir = self.playlists_dir / "exports"
        self.membership_dir = self.playlists_dir / "membership"

        for d in (self.manifests_dir, self.jobs_dir, self.exports_dir, self.membership_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.patchers = [
            mock.patch.object(flask_app, "WEB_MANAGER_DATA_DIR", self.data_dir),
            mock.patch.object(flask_app, "PLAYLIST_STATE_ROOT", self.playlists_dir),
            mock.patch.object(flask_app, "PLAYLIST_MANIFESTS_DIR", self.manifests_dir),
            mock.patch.object(flask_app, "PLAYLIST_JOB_STATE_DIR", self.jobs_dir),
            mock.patch.object(flask_app, "PLAYLIST_EXPORTS_DIR", self.exports_dir),
            mock.patch.object(flask_app, "PLAYLIST_MEMBERSHIP_DIR", self.membership_dir),
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

    def test_same_name_plex_delete_route_uses_supplied_playlist_rating_key(self):
        pid_a = flask_app._playlist_ensure_stable_id("Favorites")
        pid_b = flask_app._playlist_ensure_stable_id("Favorites")
        manifest_a = {"name": "Favorites", "playlist_id": pid_a, "last_plex": {"rating_key": "10001"}}
        manifest_b = {"name": "Favorites", "playlist_id": pid_b, "last_plex": {"rating_key": "10002"}}
        flask_app._playlist_write_manifest("Favorites", [], playlist_id=pid_a)
        flask_app._playlist_replace_manifest("Favorites", manifest_a)
        flask_app._playlist_write_manifest("Favorites", [], playlist_id=pid_b)
        flask_app._playlist_replace_manifest("Favorites", manifest_b)

        with mock.patch.object(flask_app.beets_client, "delete_playlist_m3u", return_value={"ok": True, "deleted": True}), \
             mock.patch.object(flask_app, "_plex_delete_playlist_by_rating_key", return_value=1) as mock_del, \
             flask_app.app.test_request_context("/api/playlists/Favorites", method="DELETE", json={"playlist_id": pid_a, "delete_plex": True}):
            resp = flask_app.playlist_delete("Favorites")
        data = resp.get_json()
        self.assertTrue(data["ok"])
        mock_del.assert_called_once_with("10001")
        self.assertNotIn("10002", str(mock_del.call_args_list))

    def test_plex_title_fallback_route_fails_closed_when_ambiguous(self):
        pid = flask_app._playlist_ensure_stable_id("Focus Zone")
        flask_app._playlist_write_manifest("Focus Zone", [], playlist_id=pid)
        with mock.patch.object(flask_app.beets_client, "delete_playlist_m3u", return_value={"ok": True, "deleted": True}), \
             mock.patch.object(flask_app, "_plex_audio_playlists", return_value=[
                 {"title": "Focus Zone", "ratingKey": "11111", "smart": "0"},
                 {"title": "Focus Zone", "ratingKey": "22222", "smart": "0"},
             ]), \
             mock.patch.object(flask_app, "_plex_request") as mock_req, \
             flask_app.app.test_request_context("/api/playlists/Focus%20Zone", method="DELETE", json={"playlist_id": pid, "delete_plex": True}):
            resp = flask_app.playlist_delete("Focus Zone")
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data.get("plex_deleted"), 0)
        self.assertIn("ambiguous", data.get("plex_error", "").lower())
        mock_req.assert_not_called()

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
            keys_target, rkey, am, stale = flask_app._plex_playlist_rating_keys_by_key_or_title(rating_key="70001", title="Summer Vibes")
            self.assertEqual(keys_target, ["t1", "t2"])
            self.assertEqual(rkey, "70001")
            self.assertFalse(am)
            self.assertFalse(stale)

            keys_ambig, rkey_ambig, am_ambig, stale_ambig = flask_app._plex_playlist_rating_keys_by_key_or_title(rating_key="", title="Summer Vibes")
            self.assertEqual(keys_ambig, [])
            self.assertTrue(am_ambig)
            self.assertFalse(stale_ambig)

    def test_stale_rating_key_does_not_silently_rebind_by_title(self):
        """A stored ratingKey that no longer resolves in Plex must not fall
        through to a same-title lookup -- that could silently bind this
        app playlist's identity to a completely different Plex playlist
        that merely shares a title (SEC-002 Wave 11 second final review).
        Scenario: app playlist A stored ratingKey=10001 (deleted from
        Plex); Plex currently has an unrelated playlist ratingKey=20002
        also titled "Favorites"."""
        mock_playlists = [
            {"title": "Favorites", "ratingKey": "20002", "smart": "0"},
        ]

        def mock_items_req(path, timeout=20):
            if "20002" in path:
                return {"MediaContainer": {"Metadata": [{"ratingKey": "unrelated-track"}]}}
            return {}

        with mock.patch.object(flask_app, "_plex_audio_playlists", return_value=mock_playlists), \
             mock.patch.object(flask_app, "_plex_request", side_effect=mock_items_req):
            keys, found_rkey, is_ambig, is_stale = flask_app._plex_playlist_rating_keys_by_key_or_title(
                rating_key="10001", title="Favorites")

        self.assertTrue(is_stale, "a stored ratingKey that 404s in Plex must be reported stale")
        self.assertEqual(found_rkey, "", "must not silently adopt playlist-20002's ratingKey")
        self.assertEqual(keys, [], "must not return playlist-20002's tracks as if they belonged to A")
        self.assertFalse(is_ambig)

    def test_stale_rating_key_verification_does_not_persist_wrong_key(self):
        """End-to-end: _create_playlist_outputs()'s verification step must
        not persist a wrongly-adopted ratingKey into the manifest when the
        prior stored key is stale and a same-titled Plex playlist exists."""
        pid = flask_app._playlist_ensure_stable_id("Favorites")
        flask_app._playlist_write_manifest(
            "Favorites", desired_tracks=[], playlist_id=pid,
        )
        manifest = flask_app._playlist_read_manifest("Favorites", playlist_id=pid)
        manifest["last_plex"] = {"rating_key": "10001"}
        flask_app._playlist_replace_manifest("Favorites", manifest)

        mock_playlists = [
            {"title": "Favorites", "ratingKey": "20002", "smart": "0"},
        ]
        with mock.patch.object(flask_app.beets_client, "export_playlist_m3u",
                                return_value={"ok": True, "playlist_key": "irrelevant"}), \
             mock.patch.object(flask_app, "_plex_audio_playlists", return_value=mock_playlists), \
             mock.patch.object(flask_app, "_plex_request", return_value={}), \
             mock.patch.object(flask_app, "_plex_find_music_section", return_value=(None, "", "")):
            flask_app._create_playlist_outputs(
                "Favorites", [{"artist": "A", "title": "T", "path": "/data/media/music/Artist/track.flac"}],
                playlist_id=pid, sync_plex=True,
            )

        final_manifest = flask_app._playlist_read_manifest("Favorites", playlist_id=pid)
        persisted_key = (final_manifest.get("last_plex") or {}).get("rating_key")
        self.assertNotEqual(persisted_key, "20002", "must not persist another playlist's ratingKey")


if __name__ == "__main__":
    unittest.main()