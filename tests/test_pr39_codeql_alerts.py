"""Regression tests for PR #39 CodeQL alert families."""

import json
import sqlite3
import tempfile
import unittest
import uuid
from io import BytesIO
from pathlib import Path
from unittest import mock

import app as app_module
import routes_submissions
import scripts.validate_compose_security as compose_security
from backend import beets_control_agent as bca
from backend.beets_client import BeetsClient, BeetsError
from backend.beets_control_agent import ControlAgentHandler, resolve_safe_path


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


def _patch_agent(path: str, payload: dict):
    handler = ControlAgentHandler.__new__(ControlAgentHandler)
    body = json.dumps(payload).encode("utf-8")
    handler.path = path
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = BytesIO(body)
    handler._authenticate = lambda: True
    responses = []
    handler._send_json = lambda code, data: responses.append((code, data))
    handler.do_PATCH()
    return responses[0]


class ControlAgentPathBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        base_name = Path(self.tmp.name).name
        self.base = f"/tmp/{base_name}"
        self.music = f"{self.base}/music"
        self.staging = f"{self.base}/staging"
        self.outside = f"{self.base}/outside"
        Path(self.music).mkdir(parents=True, exist_ok=True)
        Path(self.staging).mkdir(parents=True, exist_ok=True)
        Path(self.outside).mkdir(parents=True, exist_ok=True)
        self.patches = [
            mock.patch.object(bca, "MUSIC_LIBRARY_PATH", self.music),
            mock.patch.object(bca, "DOWNLOAD_PATH", self.staging),
            mock.patch.object(bca, "LOCK_PATH", f"{self.base}/agent.lock"),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def test_resolve_safe_path_rejects_traversal_and_prefix_collision(self):
        good = f"{self.music}/Artist/Track.flac"
        Path(good).parent.mkdir(parents=True, exist_ok=True)
        Path(good).write_text("audio", encoding="utf-8")
        self.assertEqual(resolve_safe_path(good, ["music"]), str(Path(good).resolve()))

        bad_paths = [
            f"{self.music}/../outside/secret.flac",
            f"{self.music}/%2e%2e/outside/secret.flac",
            f"{self.music}/%252e%252e/outside/secret.flac",
            f"{self.base}/music-other/secret.flac",
            f"{self.outside}/secret.flac",
            "C:\\data\\media\\music\\track.flac",
            "\\\\server\\share\\track.flac",
            f"{self.music}\\Artist\\Track.flac",
        ]
        for candidate in bad_paths:
            with self.subTest(candidate=candidate):
                self.assertIsNone(resolve_safe_path(candidate, ["music"]))

    def test_symlink_escape_is_rejected_where_supported(self):
        outside_secret = f"{self.outside}/secret.flac"
        Path(outside_secret).write_text("secret", encoding="utf-8")
        link_path = f"{self.music}/escape.flac"
        try:
            Path(link_path).symlink_to(Path(outside_secret))
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation is unavailable")
        self.assertIsNone(resolve_safe_path(link_path, ["music"]))

    def test_delete_endpoint_rejects_encoded_escape_before_mutation(self):
        outside_secret = f"{self.outside}/secret.flac"
        Path(outside_secret).write_text("secret", encoding="utf-8")
        code, data = _post_agent("/files/delete", {"path": f"{self.music}/%252e%252e/outside/secret.flac"})
        self.assertEqual(code, 403)
        self.assertIn("allowed roots", data["error"])
        self.assertTrue(Path(outside_secret).exists())

    def test_mkdir_endpoint_rejects_prefix_collision(self):
        code, _ = _post_agent("/files/mkdir", {"path": f"{self.base}/music-other/new"})
        self.assertEqual(code, 403)
        self.assertFalse(Path(f"{self.base}/music-other/new").exists())


class ControlAgentSqlBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.db_path = Path(self.tmp.name) / "musiclibrary.blb"
        base_name = Path(self.tmp.name).name
        self.music = f"/tmp/{base_name}/music"
        self.outside = f"/tmp/{base_name}/outside"
        Path(self.music).mkdir(parents=True, exist_ok=True)
        Path(self.outside).mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db_path)
        con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, title TEXT, artist TEXT)")
        con.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, album TEXT, albumartist TEXT, artpath TEXT)")
        con.execute("INSERT INTO items (id, title, artist) VALUES (1, 'Safe title', 'Artist')")
        con.execute("INSERT INTO albums (id, album, albumartist) VALUES (1, 'Safe album', 'Artist')")
        con.commit()
        con.close()
        self.patches = [
            mock.patch.object(bca, "LIB_PATH", str(self.db_path)),
            mock.patch.object(bca, "LOCK_PATH", str(Path(self.tmp.name) / "agent.lock")),
            mock.patch.object(bca, "MUSIC_LIBRARY_PATH", self.music),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def test_raw_query_endpoint_rejects_all_sql(self):
        for query in (
            "SELECT '/* not a comment */' AS marker, title FROM items WHERE title = ?",
            "SELECT * FROM items; SELECT * FROM albums",
            "UPDATE items SET title='owned' WHERE id=1",
            "WITH changed AS (DELETE FROM items RETURNING *) SELECT * FROM changed",
            "PRAGMA database_list",
        ):
            with self.subTest(query=query):
                code, data = _post_agent("/library/raw_query", {"query": query, "params": ["Safe title"]})
                self.assertEqual(code, 403, data)
                self.assertEqual(data["error"], "Raw SQL queries are not permitted")

        con = sqlite3.connect(self.db_path)
        title = con.execute("SELECT title FROM items WHERE id=1").fetchone()[0]
        con.close()
        self.assertEqual(title, "Safe title")

    def test_raw_sqlite_client_helper_fails_closed_locally(self):
        client = BeetsClient(base_url="http://127.0.0.1:1", token="unused")
        with self.assertRaisesRegex(BeetsError, "Raw SQLite queries are not permitted"):
            client.raw_sqlite_query("SELECT * FROM items")

    def test_item_patch_rejects_injected_column_name(self):
        code, data = _patch_agent("/items/1", {"fields": {"title = 'owned', artist": "ignored"}})
        self.assertEqual(code, 400, data)
        con = sqlite3.connect(self.db_path)
        row = con.execute("SELECT title, artist FROM items WHERE id=1").fetchone()
        con.close()
        self.assertEqual(row, ("Safe title", "Artist"))

    def test_album_patch_updates_allowlisted_column(self):
        code, data = _patch_agent("/albums/1", {"fields": {"album": "Updated album"}})
        self.assertEqual(code, 200, data)
        self.assertEqual(data["album"]["album"], "Updated album")

    def test_album_artpath_endpoint_persists_only_canonical_music_path(self):
        cover = f"{self.music}/Artist/cover.jpg"
        Path(cover).parent.mkdir(parents=True, exist_ok=True)
        Path(cover).write_text("cover", encoding="utf-8")
        code, data = _post_agent("/albums/1/artpath", {"artpath": cover})
        self.assertEqual(code, 200, data)
        self.assertEqual(data["artpath"], str(Path(cover).resolve()))

        escape = f"{self.music}/%252e%252e/outside/secret.jpg"
        code, data = _post_agent("/albums/1/artpath", {"artpath": escape})
        self.assertEqual(code, 403, data)
        self.assertNotIn("%252e%252e", json.dumps(data))

        con = sqlite3.connect(self.db_path)
        artpath = con.execute("SELECT artpath FROM albums WHERE id=1").fetchone()[0]
        con.close()
        self.assertEqual(artpath, str(Path(cover).resolve()))


class ComposeValidatorRegexTests(unittest.TestCase):
    def test_image_tag_check_handles_adversarial_path_without_regex_backtracking(self):
        image = "./" + "9/" * 2000 + "beets-engine"
        self.assertTrue(compose_security._image_lacks_tag_or_digest(image))

    def test_image_tag_check_distinguishes_registry_port_from_tag(self):
        self.assertTrue(compose_security._image_lacks_tag_or_digest("localhost:5000/beets-engine"))
        self.assertFalse(compose_security._image_lacks_tag_or_digest("localhost:5000/beets-engine:2.4.0"))
        self.assertFalse(compose_security._image_lacks_tag_or_digest("example.test/beets@sha256:" + "a" * 64))


class PublicExceptionResponseTests(unittest.TestCase):
    def test_submission_readiness_does_not_return_remote_exception_text(self):
        leak = "LEAK_MARKER Traceback /tmp/internal.py line 12 Authorization: Bearer abc"
        with mock.patch.object(routes_submissions.beets_client, "get_status", side_effect=RuntimeError(leak)), \
             mock.patch.object(routes_submissions, "_acoustid_key", return_value=""), \
             mock.patch.object(routes_submissions, "_config_has_acoustid_key", return_value=False):
            readiness = routes_submissions._submission_readiness()
        self.assertEqual(readiness["reason"], "Beets control agent status is unavailable")
        self.assertNotIn("LEAK_MARKER", json.dumps(readiness))
        self.assertNotIn("Traceback", json.dumps(readiness))
        self.assertNotIn("Bearer abc", json.dumps(readiness))

    def test_album_delete_art_does_not_return_remote_exception_text(self):
        leak = "LEAK_MARKER Traceback /tmp/internal.py line 12 token=abc"
        album_dir = Path(tempfile.gettempdir()) / f"pr39-art-{uuid.uuid4().hex}"
        album_dir.mkdir(parents=True, exist_ok=True)
        try:
            with app_module.app.test_request_context("/api/albums/42/art", method="DELETE"), \
                 mock.patch.object(app_module.lib, "get_album", return_value=object()), \
                 mock.patch.object(app_module, "_album_dir_for_art", return_value=album_dir), \
                 mock.patch.object(app_module, "_album_stored_art_path", return_value=""), \
                 mock.patch.object(app_module, "_path_is_under", return_value=True), \
                 mock.patch.object(app_module, "_ALBUM_ART_NAMES", ()), \
                 mock.patch.object(app_module.beets_client, "clear_album_artpath", side_effect=RuntimeError(leak)):
                response, status = app_module.album_delete_art(42)
            data = response.get_json()
            self.assertEqual(status, 500)
            self.assertEqual(data["error"], "Could not clear artpath.")
            self.assertNotIn("LEAK_MARKER", json.dumps(data))
            self.assertNotIn("Traceback", json.dumps(data))
            self.assertNotIn("token=abc", json.dumps(data))
        finally:
            try:
                album_dir.rmdir()
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()