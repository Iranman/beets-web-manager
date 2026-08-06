"""SEC-002 Wave 5 regression tests for album-art path boundaries.

The selected CodeQL alerts traced request-controlled album-art folders into
Path()/write/chmod sinks. These tests exercise the replacement contract: app
routes bind artwork mutation to an album ID, and the Beets control agent owns
fixed-name, album-folder-scoped, atomic artwork writes and quarantine deletes.
"""

import base64
import errno
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


class EnvClampedArtworkLimitTests(unittest.TestCase):
    def test_negative_value_clamps_to_minimum(self):
        with mock.patch.dict(os.environ, {"X_TEST_LIMIT": "-5"}):
            self.assertEqual(
                control_agent._env_int_clamped("X_TEST_LIMIT", 10, minimum=1, maximum=100), 1
            )

    def test_zero_clamps_to_minimum(self):
        with mock.patch.dict(os.environ, {"X_TEST_LIMIT": "0"}):
            self.assertEqual(
                control_agent._env_int_clamped("X_TEST_LIMIT", 10, minimum=1, maximum=100), 1
            )

    def test_absurdly_large_value_clamps_to_maximum(self):
        with mock.patch.dict(os.environ, {"X_TEST_LIMIT": "999999999999999"}):
            self.assertEqual(
                control_agent._env_int_clamped("X_TEST_LIMIT", 10, minimum=1, maximum=100), 100
            )

    def test_non_numeric_value_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"X_TEST_LIMIT": "not-a-number"}):
            self.assertEqual(
                control_agent._env_int_clamped("X_TEST_LIMIT", 10, minimum=1, maximum=100), 10
            )

    def test_unset_uses_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("X_TEST_LIMIT", None)
            self.assertEqual(
                control_agent._env_int_clamped("X_TEST_LIMIT", 10, minimum=1, maximum=100), 10
            )

    def test_in_range_value_passes_through_unchanged(self):
        with mock.patch.dict(os.environ, {"X_TEST_LIMIT": "42"}):
            self.assertEqual(
                control_agent._env_int_clamped("X_TEST_LIMIT", 10, minimum=1, maximum=100), 42
            )

    def test_module_level_limits_are_never_non_positive(self):
        """Whatever the running environment set them to, the actual module
        constants used by the image-validation code path must always be
        strictly positive and within the documented hard ceiling."""
        self.assertGreater(control_agent.ALBUM_ART_MAX_BYTES, 0)
        self.assertLessEqual(control_agent.ALBUM_ART_MAX_BYTES, 100 * 1024 * 1024)
        self.assertGreater(control_agent.ALBUM_ART_MAX_PIXELS, 0)
        self.assertLessEqual(control_agent.ALBUM_ART_MAX_PIXELS, 200_000_000)
        self.assertGreater(control_agent.ALBUM_ART_MAX_DIMENSION, 0)
        self.assertLessEqual(control_agent.ALBUM_ART_MAX_DIMENSION, 20_000)


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

    def test_replace_album_art_rejects_dangling_symlink_destination_leaf(self):
        link = self.album_dir / "albumart.jpg"
        missing = self.outside / "missing-cover.jpg"
        try:
            link.symlink_to(missing)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        outside_sentinel = self.outside / "sentinel.txt"
        outside_sentinel.write_text("unchanged", encoding="utf-8")

        code, data = _post_agent("/albums/1/art", self._payload())

        self.assertEqual(code, 403, data)
        self.assertEqual(data["error"], "Access denied for album artwork path")
        self.assertTrue(link.is_symlink())
        self.assertFalse(missing.exists())
        self.assertEqual(outside_sentinel.read_text(encoding="utf-8"), "unchanged")
        self.assertEqual(self._artpath(), "")

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

    def test_delete_album_art_quarantines_across_devices_and_clears_artpath(self):
        existing = self.album_dir / "albumart.jpg"
        existing.write_bytes(b"previous-cover")
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE albums SET artpath=? WHERE id=1", (existing.as_posix(),))
        con.commit()
        con.close()
        real_replace = control_agent.os.replace

        def fake_replace(src, dst):
            if Path(src) == existing:
                raise OSError(errno.EXDEV, "synthetic cross-device link")
            return real_replace(src, dst)

        with mock.patch.object(control_agent.os, "replace", side_effect=fake_replace):
            code, data = _delete_agent("/albums/1/art")

        self.assertEqual(code, 200, data)
        self.assertEqual(data["removed_count"], 1)
        self.assertFalse(existing.exists())
        self.assertEqual(self._artpath(), "")
        quarantined = Path(data["quarantined_art"][0]["quarantined"])
        self.assertEqual(quarantined.read_bytes(), b"previous-cover")
        self.assertTrue(quarantined.resolve(strict=False).is_relative_to(self.trash.resolve(strict=False)))

    def test_replace_album_art_reports_durability_true_on_clean_write(self):
        code, data = _post_agent("/albums/1/art", self._payload())
        self.assertEqual(code, 200, data)
        self.assertIn("durable", data)
        if os.name != "nt":
            # _fsync_directory() opens the directory with os.O_DIRECTORY,
            # which is POSIX-only; this control agent only ever runs in
            # Linux containers in practice, so the "clean write is fully
            # durable" assertion is only meaningful on a POSIX runner (this
            # module's own CI target). On native Windows, os.O_DIRECTORY is
            # unavailable (getattr(..., 0) fallback) and directory-level
            # fsync is expected to fail even on a genuinely clean write.
            self.assertTrue(data["durable"], data)

    def test_replace_album_art_reports_durability_false_without_failing_when_fsync_fails(self):
        real_fsync = control_agent.os.fsync

        def flaky_fsync(fd):
            # Let the initial content-write fsync (before the rename)
            # succeed -- only fail post-rename durability fsyncs, matching
            # what a real fsync failure at that later stage would do.
            if getattr(flaky_fsync, "armed", False):
                raise OSError("synthetic fsync failure")
            flaky_fsync.armed = True
            return real_fsync(fd)

        with mock.patch.object(control_agent.os, "fsync", side_effect=flaky_fsync):
            code, data = _post_agent("/albums/1/art", self._payload())

        self.assertEqual(code, 200, data)
        self.assertFalse(data["durable"], data)
        trusted = self.album_dir / "albumart.jpg"
        self.assertTrue(trusted.exists())
        self.assertEqual(Path(self._artpath()).resolve(strict=False), trusted.resolve(strict=False))

    def test_replace_album_art_blocks_when_items_point_to_conflicting_directories(self):
        other_dir = self.music / "Artist" / "OtherAlbum"
        other_dir.mkdir(parents=True, exist_ok=True)
        other_track = other_dir / "01 Other.flac"
        other_track.write_bytes(b"audio")
        con = sqlite3.connect(self.db_path)
        con.execute("INSERT INTO items (id, album_id, path) VALUES (2, 1, ?)", (other_track.as_posix(),))
        con.commit()
        con.close()

        code, data = _post_agent("/albums/1/art", self._payload())

        self.assertEqual(code, 409, data)
        self.assertIn("conflicting directories", data["error"])
        # Neither candidate directory should have been mutated.
        self.assertFalse((self.album_dir / "albumart.jpg").exists())
        self.assertFalse((other_dir / "albumart.jpg").exists())
        self.assertEqual(self._artpath(), "")

    def test_replace_album_art_unifies_legitimate_multidisc_item_directories(self):
        disc1 = self.album_dir / "Disc 1"
        disc2 = self.album_dir / "Disc 2"
        disc1.mkdir(parents=True, exist_ok=True)
        disc2.mkdir(parents=True, exist_ok=True)
        (disc1 / "01 Track.flac").write_bytes(b"audio")
        (disc2 / "01 Track.flac").write_bytes(b"audio")
        con = sqlite3.connect(self.db_path)
        con.execute("DELETE FROM items")
        con.execute("INSERT INTO items (id, album_id, path) VALUES (1, 1, ?)", ((disc1 / "01 Track.flac").as_posix(),))
        con.execute("INSERT INTO items (id, album_id, path) VALUES (2, 1, ?)", ((disc2 / "01 Track.flac").as_posix(),))
        con.commit()
        con.close()

        code, data = _post_agent("/albums/1/art", self._payload())

        self.assertEqual(code, 200, data)
        self.assertEqual(Path(data["album_dir"]).resolve(strict=False), self.album_dir.resolve(strict=False))

    def test_claim_trash_target_exclusive_rejects_pre_existing_file(self):
        """Direct test of the atomic no-clobber primitive itself: unlike a
        target.exists() check (which a concurrent creation can race past
        undetected), O_CREAT|O_EXCL is a single atomic syscall that always
        correctly detects and rejects a pre-existing destination."""
        target = self.trash / "already-there.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"already here")

        with self.assertRaises(FileExistsError):
            control_agent._claim_trash_target_exclusive(target)

        self.assertEqual(target.read_bytes(), b"already here")

    def test_claim_trash_target_exclusive_creates_new_file(self):
        target = self.trash / "brand-new.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)

        control_agent._claim_trash_target_exclusive(target)

        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), b"")

    def test_delete_album_art_quarantine_target_does_not_clobber_pre_existing_file(self):
        """End-to-end contract check: a pre-existing collision at the
        computed trash path falls back to a suffixed name rather than being
        overwritten. (This scenario -- collision already present before the
        check -- is also caught by a naive target.exists() check; the
        atomicity guarantee itself is what
        test_claim_trash_target_exclusive_rejects_pre_existing_file proves.)
        """
        existing = self.album_dir / "albumart.jpg"
        existing.write_bytes(b"current-cover")
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE albums SET artpath=? WHERE id=1", (existing.as_posix(),))
        con.commit()
        con.close()

        # Pre-create a colliding file at the exact op-id-scoped trash path
        # the engine will compute, simulating a race winner that got there
        # first. The engine must not silently overwrite it.
        fixed_op_id = "fixedopid00000000000000000000"
        with mock.patch.object(control_agent.uuid, "uuid4") as mock_uuid4:
            mock_uuid4.return_value = SimpleNamespace(hex=fixed_op_id)
            target_dir = self.trash / fixed_op_id
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "albumart.jpg").write_bytes(b"race-winner-content")

            code, data = _delete_agent("/albums/1/art")

        self.assertEqual(code, 200, data)
        # The pre-existing race-winner content must be untouched.
        self.assertEqual((target_dir / "albumart.jpg").read_bytes(), b"race-winner-content")
        quarantined = Path(data["quarantined_art"][0]["quarantined"])
        self.assertNotEqual(quarantined, target_dir / "albumart.jpg")
        self.assertEqual(quarantined.read_bytes(), b"current-cover")

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

    def test_upload_album_art_does_not_leak_beets_error_text(self):
        # _replace_album_art_bytes() already converts BeetsError to a safe
        # RuntimeError internally, so this mocks the helper directly to
        # simulate a BeetsError reaching album_upload_art's own outer
        # handler -- exercising that handler's own sanitization as a
        # defense-in-depth layer, independent of the inner helper's.
        leak = "LEAK_MARKER Traceback http://beets:8338 token=abc"
        with app_module.app.test_request_context(
                "/api/albums/1/art/upload",
                method="POST",
                data={"file": (BytesIO(_png_bytes()), "cover.png")},
                content_type="multipart/form-data",
             ), \
             mock.patch.object(app_module.lib, "get_album", return_value=self.album), \
             mock.patch.object(app_module.jobs, "start_python", side_effect=lambda func, **kwargs: self._run_job_immediately(func, **kwargs)), \
             mock.patch.object(app_module, "_replace_album_art_bytes", side_effect=BeetsError(leak)):
            with self.assertRaises(RuntimeError) as ctx:
                app_module.album_upload_art(1)

        message = str(ctx.exception)
        self.assertEqual(message, "Could not update album artwork")
        self.assertNotIn("LEAK_MARKER", message)
        self.assertNotIn("Traceback", message)
        self.assertNotIn("token=abc", message)
        self.assertNotIn("beets:8338", message)

    def test_replace_art_from_url_does_not_leak_beets_error_text(self):
        leak = "LEAK_MARKER Traceback http://beets:8338 token=abc"
        with app_module.app.test_request_context(
                "/api/albums/1/art/url", method="POST", json={"url": "https://images.example/cover.png"},
             ), \
             mock.patch.object(app_module.lib, "get_album", return_value=self.album), \
             mock.patch.object(app_module, "validate_outbound_url", return_value=None), \
             mock.patch.object(app_module.jobs, "start_python", side_effect=lambda func, **kwargs: self._run_job_immediately(func, **kwargs)), \
             mock.patch.object(app_module, "_replace_album_art_from_url", side_effect=BeetsError(leak)):
            with self.assertRaises(RuntimeError) as ctx:
                app_module.album_replace_art_from_url(1)

        message = str(ctx.exception)
        self.assertEqual(message, "Could not update album artwork")
        self.assertNotIn("LEAK_MARKER", message)
        self.assertNotIn("Traceback", message)
        self.assertNotIn("token=abc", message)
        self.assertNotIn("beets:8338", message)

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

    def test_delete_album_art_does_not_leak_beets_error_text(self):
        leak = "LEAK_MARKER Traceback /tmp/internal.py token=abc"
        with app_module.app.test_request_context("/api/albums/1/art", method="DELETE"), \
             mock.patch.object(app_module.lib, "get_album", return_value=self.album), \
             mock.patch.object(app_module.beets_client, "delete_album_art", side_effect=BeetsError(leak)):
            resp, status = app_module.album_delete_art(1)

        data = resp.get_json()
        self.assertEqual(status, 400, data)
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