import json
import os
import shutil
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

import app as flask_app
from backend import beets_control_agent
from backend.beets_client import BeetsUnavailableError


def _post_agent(path: str, payload: dict, *, token: str = "", auth_override: bool = True):
    handler = beets_control_agent.ControlAgentHandler.__new__(beets_control_agent.ControlAgentHandler)
    body = json.dumps(payload).encode("utf-8")
    handler.path = path
    handler.headers = {"Content-Length": str(len(body))}
    if token:
        handler.headers["X-Beets-API-Token"] = token
    handler.rfile = BytesIO(body)
    responses = []
    handler._send_json = lambda code, data: responses.append((code, data))
    if auth_override:
        handler._authenticate = lambda: True
    handler.do_POST()
    if not responses:
        raise AssertionError("ControlAgentHandler.do_POST sent no response")
    return responses[0]


class Wave12EngineImportHandoffTests(unittest.TestCase):
    """SEC-002 Wave 12 engine-owned playlist import handoff tests."""

    def setUp(self):
        if os.name == "nt":
            self.skipTest("control-agent safe path tests require POSIX absolute paths; covered in Linux/Docker")
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
        self.config_dir = Path(self.tmp) / "config"

        for d in (
            self.manifests_dir,
            self.jobs_dir,
            self.exports_dir,
            self.membership_dir,
            self.staging_dir,
            self.music_dir,
            self.outside_dir,
            self.config_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

        (self.outside_dir / "sentinel.txt").write_text("OUTSIDE_SENTINEL", encoding="utf-8")
        (self.music_dir / "library.mp3").write_text("MUSIC_SENTINEL", encoding="utf-8")

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
            mock.patch.object(beets_control_agent, "PLAYLIST_DOWNLOAD_ROOT", self.staging_dir),
            mock.patch.object(beets_control_agent, "MUSIC_ROOT", self.music_dir),
            mock.patch.object(beets_control_agent, "DOWNLOAD_PATH", str(self.staging_dir)),
            mock.patch.object(beets_control_agent, "MUSIC_LIBRARY_PATH", str(self.music_dir)),
            mock.patch.object(beets_control_agent, "BEETSDIR", str(self.config_dir)),
            mock.patch.object(beets_control_agent, "LOCK_PATH", str(self.config_dir / ".beet_db.lock")),
            mock.patch.object(beets_control_agent, "LIB_PATH", str(self.config_dir / "musiclibrary.blb")),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _playlist(self, name="Playlist A"):
        pid = flask_app._playlist_ensure_stable_id(name)
        key = flask_app._playlist_key(name, playlist_id=pid, allocate=False)
        return pid, key

    def _staged_file(self, key: str, filename: str = "track.mp3", content: bytes = b"audio") -> Path:
        path = self.staging_dir / key / "downloads" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_import_staged_requires_auth(self):
        token = "valid-secret-token-1234567890abcdef"
        payload = {"playlist_key": "playlist-a", "operation_id": "bad", "tracks": []}
        with mock.patch.object(beets_control_agent, "BEETS_API_TOKEN", token):
            code, data = _post_agent("/playlists/import-staged", payload, auth_override=False)
            self.assertEqual(code, 401)
            self.assertIn("Unauthorized", data.get("error", ""))

            code, data = _post_agent("/playlists/import-staged", payload, token="wrong-token", auth_override=False)
            self.assertEqual(code, 401)
            self.assertIn("Unauthorized", data.get("error", ""))

            code, data = _post_agent("/playlists/import-staged", payload, token=token, auth_override=False)
            self.assertEqual(code, 400)
            self.assertEqual(data.get("error_code"), "invalid_operation_id")

    def test_import_staged_cross_playlist_refusal(self):
        pid_a, key_a = self._playlist("Playlist A")
        _pid_b, key_b = self._playlist("Playlist B")
        staged_b_file = self._staged_file(key_b, "track_b.mp3", b"B")
        code, data = _post_agent("/playlists/import-staged", {
            "playlist_key": key_a,
            "playlist_id": pid_a,
            "operation_id": "pl-import-cross-playlist",
            "tracks": [{"track_id": "tr_1", "staged_path": str(staged_b_file)}],
        })
        self.assertEqual(code, 200)
        self.assertFalse(data.get("ok"))
        self.assertEqual(data.get("status"), "review_required")
        self.assertEqual(data["tracks"][0]["status"], "cross_playlist_target")
        self.assertTrue(staged_b_file.exists())

    def test_import_staged_refuses_leaf_symlink(self):
        pid, key = self._playlist()
        target = self.outside_dir / "outside.mp3"
        target.write_bytes(b"outside")
        link = self.staging_dir / key / "downloads" / "link.mp3"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        code, data = _post_agent("/playlists/import-staged", {
            "playlist_key": key,
            "playlist_id": pid,
            "operation_id": "pl-import-symlink-leaf",
            "tracks": [{"track_id": "tr_1", "staged_path": str(link)}],
        })
        self.assertEqual(code, 200)
        self.assertFalse(data.get("ok"))
        self.assertEqual(data["tracks"][0]["status"], "staged_track_symlink")
        self.assertEqual(target.read_bytes(), b"outside")

    def test_import_staged_refuses_parent_symlink(self):
        pid, key = self._playlist()
        parent_target = self.outside_dir / "download-parent"
        parent_target.mkdir()
        outside_file = parent_target / "evil.mp3"
        outside_file.write_bytes(b"outside")
        downloads_link = self.staging_dir / key / "downloads"
        downloads_link.parent.mkdir(parents=True, exist_ok=True)
        downloads_link.symlink_to(parent_target, target_is_directory=True)
        code, data = _post_agent("/playlists/import-staged", {
            "playlist_key": key,
            "playlist_id": pid,
            "operation_id": "pl-import-symlink-parent",
            "tracks": [{"track_id": "tr_1", "staged_path": str(downloads_link / "evil.mp3")}],
        })
        self.assertEqual(code, 200)
        self.assertFalse(data.get("ok"))
        self.assertEqual(data["tracks"][0]["status"], "staged_track_symlink")
        self.assertEqual(outside_file.read_bytes(), b"outside")

    def test_import_staged_refuses_operation_dir_symlink(self):
        pid, key = self._playlist()
        staged = self._staged_file(key, "track.mp3", b"audio")
        imports_root = self.staging_dir / key / "imports"
        imports_root.mkdir(parents=True)
        operation_link = imports_root / beets_control_agent._playlist_import_operation_storage_key("pl-import-operation-symlink")
        operation_link.symlink_to(self.outside_dir, target_is_directory=True)
        code, data = _post_agent("/playlists/import-staged", {
            "playlist_key": key,
            "playlist_id": pid,
            "operation_id": "pl-import-operation-symlink",
            "tracks": [{"track_id": "tr_1", "staged_path": str(staged)}],
        })
        self.assertEqual(code, 403)
        self.assertEqual(data.get("error_code"), "import_staging_symlink")
        self.assertTrue(staged.exists())

    def test_import_staged_refuses_music_root_source(self):
        pid, key = self._playlist()
        code, data = _post_agent("/playlists/import-staged", {
            "playlist_key": key,
            "playlist_id": pid,
            "operation_id": "pl-import-music-root",
            "tracks": [{"track_id": "tr_1", "staged_path": str(self.music_dir / "library.mp3")}],
        })
        self.assertEqual(code, 200)
        self.assertFalse(data.get("ok"))
        self.assertEqual(data["tracks"][0]["status"], "cross_playlist_target")

    def test_import_staged_refuses_non_audio(self):
        pid, key = self._playlist()
        note = self._staged_file(key, "note.txt", b"not audio")
        code, data = _post_agent("/playlists/import-staged", {
            "playlist_key": key,
            "playlist_id": pid,
            "operation_id": "pl-import-non-audio",
            "tracks": [{"track_id": "tr_1", "staged_path": str(note)}],
        })
        self.assertEqual(code, 200)
        self.assertFalse(data.get("ok"))
        self.assertEqual(data["tracks"][0]["status"], "staged_track_not_audio")
        self.assertTrue(note.exists())

    def test_missing_source_without_history_is_not_completed(self):
        pid, key = self._playlist()
        missing = self.staging_dir / key / "downloads" / "missing.mp3"
        code, data = _post_agent("/playlists/import-staged", {
            "playlist_key": key,
            "playlist_id": pid,
            "operation_id": "pl-import-missing-source",
            "tracks": [{"track_id": "tr_1", "staged_path": str(missing)}],
        })
        self.assertEqual(code, 200)
        self.assertFalse(data.get("ok"))
        self.assertEqual(data.get("status"), "review_required")
        self.assertEqual(data["tracks"][0]["status"], "staged_track_missing")

    def test_import_staged_idempotency_is_operation_backed(self):
        pid, key = self._playlist("Workout Mix")
        staged = self._staged_file(key, "track.mp3", b"audio")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout="sensitive /config/path", stderr="sensitive /data/path")

        payload = {
            "playlist_key": key,
            "playlist_id": pid,
            "operation_id": "pl-import-idempotent-real",
            "tracks": [{"track_id": "tr_1", "staged_path": str(staged)}],
        }
        with mock.patch.object(beets_control_agent.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(beets_control_agent, "_verify_imported_track_in_library", return_value=True):
            code, first = _post_agent("/playlists/import-staged", payload)
            self.assertEqual(code, 200, first)
            self.assertTrue(first.get("ok"), first)
            self.assertEqual(first.get("status"), "completed")
            self.assertEqual(first.get("imported_count"), 1)

            code, second = _post_agent("/playlists/import-staged", payload)
            self.assertEqual(code, 200, second)
            self.assertTrue(second.get("ok"), second)
            self.assertEqual(second.get("status"), "already_completed")
            self.assertEqual(second.get("imported_count"), 1)

        self.assertEqual(len(calls), 1)
        self.assertFalse(staged.exists())
        self.assertNotIn("stdout", first.get("beets", {}))
        self.assertNotIn("stderr", first.get("beets", {}))

    def test_different_operation_missing_source_does_not_inherit_completion(self):
        pid, key = self._playlist("Retry Mix")
        staged = self._staged_file(key, "track.mp3", b"audio")
        first_payload = {
            "playlist_key": key,
            "playlist_id": pid,
            "operation_id": "pl-import-first-operation",
            "tracks": [{"track_id": "tr_1", "staged_path": str(staged)}],
        }
        with mock.patch.object(beets_control_agent.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")), \
             mock.patch.object(beets_control_agent, "_verify_imported_track_in_library", return_value=True):
            code, first = _post_agent("/playlists/import-staged", first_payload)
        self.assertEqual(code, 200, first)
        self.assertTrue(first.get("ok"), first)

        second_payload = dict(first_payload)
        second_payload["operation_id"] = "pl-import-second-operation"
        code, second = _post_agent("/playlists/import-staged", second_payload)
        self.assertEqual(code, 200, second)
        self.assertFalse(second.get("ok"))
        self.assertEqual(second["tracks"][0]["status"], "staged_track_missing")

    def test_failed_first_try_retries_from_operation_dir(self):
        pid, key = self._playlist("Recover Mix")
        staged = self._staged_file(key, "track.mp3", b"audio")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return mock.Mock(returncode=2 if len(calls) == 1 else 0, stdout="", stderr="")

        payload = {
            "playlist_key": key,
            "playlist_id": pid,
            "operation_id": "pl-import-retry-failure",
            "tracks": [{"track_id": "tr_1", "staged_path": str(staged)}],
        }
        with mock.patch.object(beets_control_agent.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(beets_control_agent, "_verify_imported_track_in_library", return_value=True):
            code, first = _post_agent("/playlists/import-staged", payload)
            self.assertEqual(code, 200, first)
            self.assertFalse(first.get("ok"))
            self.assertEqual(first.get("status"), "failed")

            code, second = _post_agent("/playlists/import-staged", payload)
            self.assertEqual(code, 200, second)
            self.assertTrue(second.get("ok"), second)
            self.assertEqual(second.get("status"), "completed")
        self.assertEqual(len(calls), 2)

    def test_operation_specific_import_dir_ignores_old_files(self):
        pid, key = self._playlist("Operation Scoped")
        old_dir = self.staging_dir / key / "imports" / "pl-import-old-operation"
        old_dir.mkdir(parents=True)
        old_file = old_dir / "old.mp3"
        old_file.write_bytes(b"old")
        staged = self._staged_file(key, "new.mp3", b"new")
        seen_cmds = []

        def fake_run(cmd, **kwargs):
            seen_cmds.append(cmd)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(beets_control_agent.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(beets_control_agent, "_verify_imported_track_in_library", return_value=True):
            code, data = _post_agent("/playlists/import-staged", {
                "playlist_key": key,
                "playlist_id": pid,
                "operation_id": "pl-import-current-operation",
                "tracks": [{"track_id": "tr_1", "staged_path": str(staged)}],
            })
        self.assertEqual(code, 200, data)
        self.assertTrue(data.get("ok"), data)
        import_dir_name = Path(seen_cmds[0][-1]).name
        self.assertRegex(import_dir_name, r"^op_[0-9a-f]{64}$")
        self.assertNotEqual(import_dir_name, "pl-import-current-operation")
        self.assertTrue(old_file.exists())

    def test_playlist_run_import_downloaded_delegates_to_engine_ipc_without_local_media_access(self):
        clean_name = "Jazz Favorites"
        pid = flask_app._playlist_ensure_stable_id(clean_name)
        key = flask_app._playlist_key(clean_name, playlist_id=pid, allocate=False)
        staged_path = f"/data/torrents/music/Playlist Downloads/{key}/downloads/01 - Take Five.flac"
        trk = {"artist": "Dave Brubeck", "title": "Take Five", "id": "tr_takefive", "mb_trackid": "mbid-1"}
        flask_app._playlist_store_track_state(clean_name, trk, "downloaded", playlist_id=pid, staged_path=staged_path, mb_trackid="mbid-1")

        ipc_calls = []

        def fail_local_media(*args, **kwargs):
            raise AssertionError("web-manager attempted local media inspection")

        def mock_import_staged(pkey, playlist_id, tracks, operation_id=""):
            ipc_calls.append({"pkey": pkey, "playlist_id": playlist_id, "tracks": tracks, "operation_id": operation_id})
            return {
                "ok": True,
                "status": "completed",
                "imported_count": 0,
                "tracks": [{"track_id": "dave brubeck|take five", "status": "imported", "staged_path": staged_path}],
                "beets": {"returncode": 0, "message": "Beets import completed."},
            }

        with mock.patch.object(flask_app, "_playlist_library_index", return_value=[]), \
             mock.patch.object(flask_app, "_playlist_download_audio_allowed", side_effect=fail_local_media), \
             mock.patch.object(flask_app, "_playlist_download_match", side_effect=fail_local_media), \
             mock.patch.object(flask_app.beets_client, "inspect_playlist_staged_track", return_value={"ok": True, "exists": True, "authorized": True, "status": "ok"}), \
             mock.patch.object(flask_app.beets_client, "import_playlist_staged", side_effect=mock_import_staged), \
             mock.patch.object(flask_app.beets_client, "export_playlist_m3u", return_value={"ok": True, "created": True}), \
             mock.patch.object(flask_app.beets_client, "read_playlist_m3u", return_value={"ok": True, "exists": True, "items": []}), \
             mock.patch.object(flask_app, "_playlist_place_recent_imports_for_tracks", return_value={"placed": 0, "failed": 0, "results": []}):
            log = []
            res = flask_app._playlist_run_import_downloaded(clean_name, log, playlist_id=pid)

        self.assertEqual(res.get("imported_files"), 0)
        self.assertEqual(len(ipc_calls), 1)
        self.assertEqual(ipc_calls[0]["pkey"], key)
        self.assertEqual(ipc_calls[0]["playlist_id"], pid)
        self.assertRegex(ipc_calls[0]["operation_id"], r"^pl-import-[0-9a-f]{32}$")
        self.assertEqual(ipc_calls[0]["tracks"][0]["mb_trackid"], "mbid-1")

    def test_engine_unavailable_during_import_fails_truthfully(self):
        clean_name = "Unavailable"
        pid = flask_app._playlist_ensure_stable_id(clean_name)
        key = flask_app._playlist_key(clean_name, playlist_id=pid, allocate=False)
        staged_path = f"/data/torrents/music/Playlist Downloads/{key}/downloads/file.mp3"
        trk = {"artist": "A", "title": "B"}
        flask_app._playlist_store_track_state(clean_name, trk, "downloaded", playlist_id=pid, staged_path=staged_path, mb_trackid="mbid-1")
        with mock.patch.object(flask_app, "_playlist_library_index", return_value=[]), \
             mock.patch.object(flask_app.beets_client, "inspect_playlist_staged_track", return_value={"ok": True, "exists": True, "authorized": True, "status": "ok"}), \
             mock.patch.object(flask_app.beets_client, "import_playlist_staged", side_effect=BeetsUnavailableError("down")):
            with self.assertRaises(RuntimeError) as ctx:
                flask_app._playlist_run_import_downloaded(clean_name, [], playlist_id=pid)
        self.assertEqual(str(ctx.exception), "engine_unavailable")


if __name__ == "__main__":
    unittest.main()
