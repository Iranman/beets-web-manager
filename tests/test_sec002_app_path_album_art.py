"""SEC-002 Wave 5 regression tests for album-art path boundaries.

The selected CodeQL alerts traced request-controlled album-art folders into
Path()/write/chmod sinks. These tests exercise the replacement contract: app
routes bind artwork mutation to an album ID, and the Beets control agent owns
fixed-name, album-folder-scoped, atomic artwork writes and quarantine deletes.
"""

import base64
import json
import os
import sqlite3
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import app as app_module
from backend import beets_control_agent as control_agent
from backend.beets_client import BeetsError

try:
    from PIL import Image
except Exception:  # pragma: no cover - dependency availability is validated elsewhere
    Image = None


RGID = "11111111-1111-1111-1111-111111111111"
OTHER_RGID = "22222222-2222-2222-2222-222222222222"


def _png_bytes(color=(12, 34, 56), size=(16, 16)) -> bytes:
    if Image is None:
        raise unittest.SkipTest("Pillow unavailable")
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def _post_agent(path: str, payload: dict):
    handler = control_agent.ControlAgentHandler.__new__(control_agent.ControlAgentHandler)
    body = json.dumps(payload).encode("utf-8")
    handler.path = path
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = BytesIO(body)
    handler._authenticate = lambda: True
    responses = []
    handler._send_json = lambda code, data: responses.append((code, data))
    handler.do_POST()
    return responses[0]


def _delete_agent(path: str, payload: dict | None = None):
    handler = control_agent.ControlAgentHandler.__new__(control_agent.ControlAgentHandler)
    body = json.dumps(payload or {}).encode("utf-8")
    handler.path = path
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = BytesIO(body)
    handler._authenticate = lambda: True
    responses = []
    handler._send_json = lambda code, data: responses.append((code, data))
    handler.do_DELETE()
    return responses[0]


class ControlAgentAlbumArtMutationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        base_name = Path(self.tmp.name).name
        self.root = Path(f"/tmp/{base_name}")
        self.music = self.root / "music"
        self.outside = self.root / "outside"
        self.beetsdir = self.root / "config"
        self.trash = self.beetsdir / "album-art-trash"
        self.album_dir = self.music / "Artist" / "Album"
        for path in (self.album_dir, self.outside, self.beetsdir, self.trash):
            path.mkdir(parents=True, exist_ok=True)
        self.track = self.album_dir / "01 Track.flac"
        self.track.write_bytes(b"audio")
        self.db_path = self.beetsdir / "musiclibrary.blb"
        con = sqlite3.connect(self.db_path)
        con.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, album TEXT, albumartist TEXT, mb_releasegroupid TEXT, artpath TEXT)")
        con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, path TEXT)")
        con.execute(
            "INSERT INTO albums (id, album, albumartist, mb_releasegroupid, artpath) VALUES (1, 'Album', 'Artist', ?, '')",
            (RGID,),
        )
        con.execute("INSERT INTO items (id, album_id, path) VALUES (1, 1, ?)", (self.track.as_posix(),))
        con.commit()
        con.close()
        self.patches = [
            mock.patch.object(control_agent, "LIB_PATH", str(self.db_path)),
            mock.patch.object(control_agent, "LOCK_PATH", str(self.root / "agent.lock")),
            mock.patch.object(control_agent, "BEETSDIR", self.beetsdir.as_posix()),
            mock.patch.object(control_agent, "MUSIC_LIBRARY_PATH", self.music.as_posix()),
            mock.patch.object(control_agent, "ALBUM_ART_TRASH_ROOT", self.trash),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _payload(self, *, rgid=RGID, data=None):
        return {
            "image_data_b64": base64.b64encode(data or _png_bytes()).decode("ascii"),
            "source": "user_upload",
            "expected_mb_releasegroupid": rgid,
        }

    def _artpath(self):
        con = sqlite3.connect(self.db_path)
        value = con.execute("SELECT artpath FROM albums WHERE id=1").fetchone()[0]
        con.close()
        return value

    def test_replace_album_art_writes_fixed_name_and_updates_one_artpath(self):
        outside_sentinel = self.outside / "sentinel.txt"
        outside_sentinel.write_text("unchanged", encoding="utf-8")

        code, data = _post_agent("/albums/1/art", self._payload())

        self.assertEqual(code, 200, data)
        trusted = self.album_dir / "albumart.jpg"
        self.assertEqual(Path(data["artpath"]).resolve(strict=False), trusted.resolve(strict=False))
        self.assertTrue(trusted.exists())
        self.assertEqual(Path(self._artpath()).resolve(strict=False), trusted.resolve(strict=False))
        self.assertEqual(outside_sentinel.read_text(encoding="utf-8"), "unchanged")
        self.assertNotIn("../", json.dumps(data))

    def test_replace_album_art_rejects_release_group_mismatch_without_file_change(self):
        existing = self.album_dir / "albumart.jpg"
        existing.write_bytes(b"previous-cover")
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE albums SET artpath=? WHERE id=1", (existing.as_posix(),))
        con.commit()
        con.close()

        code, data = _post_agent("/albums/1/art", self._payload(rgid=OTHER_RGID))

        self.assertEqual(code, 409, data)
        self.assertEqual(data["error"], "Album identity changed before artwork update")
        self.assertEqual(existing.read_bytes(), b"previous-cover")
        self.assertEqual(Path(self._artpath()).resolve(strict=False), existing.resolve(strict=False))

    def test_replace_album_art_rejects_symlink_album_folder(self):
        link = self.music / "LinkedAlbum"
        try:
            link.symlink_to(self.outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        hostile_track = link / "01 Track.flac"
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE items SET path=? WHERE id=1", (hostile_track.as_posix(),))
        con.commit()
        con.close()
        outside_sentinel = self.outside / "sentinel.txt"
        outside_sentinel.write_text("unchanged", encoding="utf-8")

        code, data = _post_agent("/albums/1/art", self._payload())

        self.assertEqual(code, 400, data)
        self.assertEqual(data["error"], "Album folder could not be resolved safely")
        self.assertEqual(outside_sentinel.read_text(encoding="utf-8"), "unchanged")
        self.assertFalse((self.outside / "albumart.jpg").exists())

    def test_replace_album_art_rolls_back_existing_file_when_post_replace_step_fails(self):
        existing = self.album_dir / "albumart.jpg"
        existing.write_bytes(b"previous-cover")
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE albums SET artpath=? WHERE id=1", (existing.as_posix(),))
        con.commit()
        con.close()

        with mock.patch.object(control_agent, "_fsync_file", side_effect=RuntimeError("synthetic fsync failure")):
            code, data = _post_agent("/albums/1/art", self._payload())

        self.assertEqual(code, 500, data)
        self.assertEqual(data["error"], "Failed to replace album artwork")
        self.assertEqual(existing.read_bytes(), b"previous-cover")
        self.assertEqual(Path(self._artpath()).resolve(strict=False), existing.resolve(strict=False))

    def test_replace_album_art_preserves_existing_file_when_pre_backup_step_fails(self):
        existing = self.album_dir / "albumart.jpg"
        existing.write_bytes(b"previous-cover")
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE albums SET artpath=? WHERE id=1", (existing.as_posix(),))
        con.commit()
        con.close()

        with mock.patch.object(control_agent.os, "chmod", side_effect=RuntimeError("synthetic chmod failure")):
            code, data = _post_agent("/albums/1/art", self._payload())

        self.assertEqual(code, 500, data)
        self.assertEqual(data["error"], "Failed to replace album artwork")
        self.assertEqual(existing.read_bytes(), b"previous-cover")
        self.assertEqual(Path(self._artpath()).resolve(strict=False), existing.resolve(strict=False))
        self.assertFalse(any(self.album_dir.glob(".albumart-*.tmp")))

    def test_delete_album_art_rejects_trash_root_outside_config(self):
        existing = self.album_dir / "albumart.jpg"
        existing.write_bytes(b"previous-cover")
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE albums SET artpath=? WHERE id=1", (existing.as_posix(),))
        con.commit()
        con.close()

        with mock.patch.object(control_agent, "ALBUM_ART_TRASH_ROOT", self.outside / "trash"):
            code, data = _delete_agent("/albums/1/art")

        self.assertEqual(code, 403, data)
        self.assertEqual(data["error"], "Access denied for album artwork path")
        self.assertEqual(existing.read_bytes(), b"previous-cover")
        self.assertEqual(Path(self._artpath()).resolve(strict=False), existing.resolve(strict=False))

    def test_delete_album_art_rejects_symlink_leaf_and_preserves_outside_sentinel(self):
        sentinel = self.outside / "sentinel.jpg"
        sentinel.write_bytes(b"outside")
        link = self.album_dir / "albumart.jpg"
        try:
            link.symlink_to(sentinel)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE albums SET artpath=? WHERE id=1", (link.as_posix(),))
        con.commit()
        con.close()

        code, data = _delete_agent("/albums/1/art")

        self.assertIn(code, {400, 403}, data)
        self.assertIn(data["error"], {"Access denied for album artwork path", "Album folder could not be resolved safely"})
        self.assertTrue(link.is_symlink())
        self.assertEqual(sentinel.read_bytes(), b"outside")
        self.assertEqual(Path(self._artpath()).resolve(strict=False), link.resolve(strict=False))


class FlaskAlbumArtRouteBoundaryTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()
        self.album = SimpleNamespace(
            id=1,
            album="Album",
            albumartist="Artist",
            artist="Artist",
            mb_releasegroupid=RGID,
        )

    def _run_job_immediately(self, func, **kwargs):
        func([], None)
        return SimpleNamespace(job_id="job-immediate")

    def test_save_album_art_ignores_client_album_folder_destination(self):
        malicious_aldir = "/data/media/music/Artist/Album/../../Outside"
        captured = {}

        def fake_save(url, album_id, **kwargs):
            captured.update({"url": url, "album_id": album_id, **kwargs})
            return "/data/media/music/Artist/Album/albumart.jpg"

        with app_module.app.test_request_context("/api/save-album-art", method="POST", json={
                "album_id": 1,
                "artist": "Artist",
                "album": "Album",
                "aldir": malicious_aldir,
             }), \
             mock.patch.object(app_module.lib, "get_album", return_value=self.album), \
             mock.patch.object(app_module, "_album_art_status", return_value={"has_local_art": False}), \
             mock.patch.object(app_module, "_fetch_album_art", return_value="https://images.example/cover.png"), \
             mock.patch.object(app_module, "_save_art_to_disk", side_effect=fake_save):
            resp = app_module.save_album_art()

        data = resp.get_json()
        self.assertEqual(resp.status_code, 200, data)
        self.assertTrue(data["ok"], data)
        self.assertEqual(captured["album_id"], 1)
        self.assertEqual(captured["expected_mb_releasegroupid"], RGID)
        self.assertNotIn("Outside", captured["url"])

    def test_upload_album_art_uses_engine_operation_not_upload_filename(self):
        captured = {}

        def fake_replace(album_id, image_data_b64, **kwargs):
            captured.update({"album_id": album_id, "image_data_b64": image_data_b64, **kwargs})
            return {
                "ok": True,
                "artpath": "/data/media/music/Artist/Album/albumart.jpg",
                "image": {"format": "JPEG"},
            }

        with app_module.app.test_request_context(
                "/api/albums/1/art/upload",
                method="POST",
                data={"file": (BytesIO(_png_bytes()), "../outside-cover.png")},
                content_type="multipart/form-data",
             ), \
             mock.patch.object(app_module.lib, "get_album", return_value=self.album), \
             mock.patch.object(app_module.jobs, "start_python", side_effect=lambda func, **kwargs: self._run_job_immediately(func, **kwargs)), \
             mock.patch.object(app_module.beets_client, "replace_album_art", side_effect=fake_replace), \
             mock.patch.object(app_module, "_beet_run", return_value=SimpleNamespace(returncode=0)):
            resp = app_module.album_upload_art(1)

        data = resp.get_json()
        self.assertEqual(resp.status_code, 200, data)
        self.assertTrue(data["ok"], data)
        self.assertEqual(captured["album_id"], 1)
        self.assertEqual(captured["source"], "user_upload")
        self.assertEqual(captured["expected_mb_releasegroupid"], RGID)
        self.assertNotIn("outside-cover", json.dumps(captured))

    def test_delete_album_art_does_not_leak_remote_exception_text(self):
        leak = "LEAK_MARKER Traceback /tmp/internal.py token=abc"
        with app_module.app.test_request_context("/api/albums/1/art", method="DELETE"), \
             mock.patch.object(app_module.lib, "get_album", return_value=self.album), \
             mock.patch.object(app_module.beets_client, "delete_album_art", side_effect=RuntimeError(leak)):
            resp, status = app_module.album_delete_art(1)

        data = resp.get_json()
        self.assertEqual(status, 500, data)
        self.assertEqual(data["error"], "Could not delete album artwork.")
        self.assertNotIn("LEAK_MARKER", json.dumps(data))
        self.assertNotIn("Traceback", json.dumps(data))
        self.assertNotIn("token=abc", json.dumps(data))

    def test_delete_album_art_uses_engine_quarantine_operation(self):
        with app_module.app.test_request_context("/api/albums/1/art", method="DELETE"), \
             mock.patch.object(app_module.lib, "get_album", return_value=self.album), \
             mock.patch.object(app_module.beets_client, "delete_album_art", return_value={
                 "ok": True,
                 "removed": ["/data/media/music/Artist/Album/albumart.jpg"],
                 "removed_count": 1,
                 "quarantined_art": [{"source": "/data/media/music/Artist/Album/albumart.jpg", "quarantined": "/config/trash/x/albumart.jpg"}],
             }) as delete_art:
            resp = app_module.album_delete_art(1)

        data = resp.get_json()
        self.assertEqual(resp.status_code, 200, data)
        self.assertTrue(data["ok"], data)
        delete_art.assert_called_once_with(1)
        self.assertEqual(data["removed_count"], 1)


if __name__ == "__main__":
    unittest.main()