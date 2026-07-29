"""Regression tests for PR #39 CodeQL alert families."""

import json
import shutil
import sqlite3
import sys
import tempfile
import types
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


def _delete_agent(path: str, payload: dict):
    handler = ControlAgentHandler.__new__(ControlAgentHandler)
    body = json.dumps(payload).encode("utf-8")
    handler.path = path
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = BytesIO(body)
    handler._authenticate = lambda: True
    responses = []
    handler._send_json = lambda code, data: responses.append((code, data))
    handler.do_DELETE()
    return responses[0]


def _encoded_dotdot(layers: int) -> str:
    dot = "%2e"
    for _ in range(layers - 1):
        dot = dot.replace("%", "%25")
    return f"{dot}{dot}"


def _api_path(path: Path) -> str:
    return str(path).replace("\\", "/")


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

    def test_resolve_safe_path_preserves_literal_percent_filename(self):
        """Regression test: resolve_safe_path must canonicalize from the
        original raw path, not its decoded form. Decoding is used only to
        detect a hidden traversal/separator/null-byte attack; a legitimate
        literal filename that happens to contain a percent-sign byte
        sequence (e.g. "50%20off.flac") must resolve to itself and not be
        silently rewritten to a different string ("50 off.flac") before it
        reaches the filesystem sink -- this API's contract is a raw
        filesystem path (values arrive in a JSON body, never URL-encoded by
        any real caller), so decoding must never change what gets operated on."""
        literal = f"{self.music}/convention%20album.flac"
        Path(literal).write_text("audio", encoding="utf-8")
        decoded_sibling = f"{self.music}/convention album.flac"
        self.assertFalse(Path(decoded_sibling).exists())

        result = resolve_safe_path(literal, ["music"])
        self.assertEqual(result, str(Path(literal).resolve()))
        self.assertNotEqual(result, str(Path(decoded_sibling).resolve()))

        with mock.patch.object(bca.os, "unlink") as unlink_mock:
            code, data = _post_agent("/files/delete", {"path": literal})
        self.assertEqual(code, 200, data)
        unlink_mock.assert_called_once_with(str(Path(literal).resolve()))

    def test_command_args_allow_beets_range_query_syntax(self):
        """Regression test: a blanket '".." in arg' substring check rejected
        legitimate Beets range-query syntax (e.g. `year:2020..2023`,
        `added:2020-01-01..2020-02-01`), which is never filesystem-joined --
        only an actual relative path segment ("..", "../x", "x/..") is a
        real traversal-relevant shape and must still be rejected."""
        source = Path(self.staging) / "import-source"
        source.mkdir()
        fake_result = mock.MagicMock(returncode=0, stdout="", stderr="")

        legitimate_args = ["year:2020..2023", "added:2020-01-01..2020-02-01", "artist:Weird Al..."]
        with mock.patch.object(bca.subprocess, "run", return_value=fake_result) as run_mock:
            code, data = _post_agent("/commands/execute", {
                "command": "ls",
                "args": legitimate_args,
            })
        self.assertEqual(code, 200, data)
        full_cmd = run_mock.call_args.args[0]
        self.assertEqual(full_cmd[-len(legitimate_args):], legitimate_args)

        for traversal_arg in ["..", "../etc/passwd", "foo/../bar", "./relative"]:
            with self.subTest(arg=traversal_arg):
                with mock.patch.object(bca.subprocess, "run") as run_mock:
                    code, data = _post_agent("/commands/execute", {
                        "command": "ls",
                        "args": [traversal_arg],
                    })
                self.assertEqual(code, 403, data)
                run_mock.assert_not_called()

    def test_resolve_safe_path_rejects_extended_malicious_inputs(self):
        bad_paths = [
            "",
            "   ",
            None,
            f"{self.music}/Artist/./Track.flac",
            f"{self.music}/{_encoded_dotdot(1)}/outside/secret.flac",
            f"{self.music}/{_encoded_dotdot(2)}/outside/secret.flac",
            f"{self.music}/{_encoded_dotdot(5)}/outside/secret.flac",
            f"{self.music}/{_encoded_dotdot(6)}/outside/secret.flac",
            f"{self.music}/Artist%2fTrack.flac",
            f"{self.outside}/secret.flac",
            f"{self.music}/Track.flac\x00.jpg",
            f"{self.music}\\Artist\\Track.flac",
            "C:/data/media/music/track.flac",
            "\\\\server\\share\\track.flac",
            "//server/share/track.flac",
        ]
        for candidate in bad_paths:
            with self.subTest(candidate=candidate):
                self.assertIsNone(resolve_safe_path(candidate, ["music"]))

    def test_symlink_directory_escape_is_rejected_where_supported(self):
        link_dir = f"{self.music}/escape-dir"
        try:
            Path(link_dir).symlink_to(Path(self.outside), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation is unavailable")
        self.assertIsNone(resolve_safe_path(f"{link_dir}/secret.flac", ["music"]))

    def test_delete_endpoint_uses_canonical_path_and_refuses_root(self):
        code, data = _post_agent("/files/delete", {"path": self.music})
        self.assertEqual(code, 403, data)
        self.assertEqual(data["code"], "allowed_root_operation_denied")

        safe_missing = f"{self.music}/Artist/missing.flac"
        code, data = _post_agent("/files/delete", {"path": safe_missing})
        self.assertEqual(code, 200, data)
        self.assertFalse(data["existed"])
        self.assertEqual(data["path"], str(Path(safe_missing).resolve()))

        existing = f"{self.music}/Artist/Track.flac"
        Path(existing).parent.mkdir(parents=True, exist_ok=True)
        Path(existing).write_text("audio", encoding="utf-8")
        with mock.patch.object(bca.os, "unlink") as unlink_mock:
            code, data = _post_agent("/files/delete", {"path": existing})
        self.assertEqual(code, 200, data)
        self.assertTrue(data["existed"])
        unlink_mock.assert_called_once_with(str(Path(existing).resolve()))

    def test_delete_endpoint_rejects_symlink_escape_before_unlink(self):
        outside_secret = Path(self.outside) / "secret.flac"
        outside_secret.write_text("secret", encoding="utf-8")
        link_path = Path(self.music) / "escape.flac"
        try:
            link_path.symlink_to(outside_secret)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation is unavailable")
        with mock.patch.object(bca.os, "unlink") as unlink_mock:
            code, data = _post_agent("/files/delete", {"path": _api_path(link_path)})
        self.assertEqual(code, 403, data)
        unlink_mock.assert_not_called()
        self.assertTrue(outside_secret.exists())

    def test_mkdir_endpoint_validates_parent_and_revalidates_after_creation(self):
        target = f"{self.music}/Artist/New Album"
        code, data = _post_agent("/files/mkdir", {"path": target})
        self.assertEqual(code, 200, data)
        self.assertEqual(data["path"], str(Path(target).resolve()))
        self.assertTrue(Path(target).is_dir())

        code, data = _post_agent("/files/mkdir", {"path": self.music})
        self.assertEqual(code, 403, data)
        self.assertEqual(data["code"], "allowed_root_operation_denied")

    def test_mkdir_rejects_symlink_parent_escape_where_supported(self):
        link_dir = Path(self.music) / "linked-parent"
        try:
            link_dir.symlink_to(Path(self.outside), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation is unavailable")
        target = link_dir / "child"
        code, data = _post_agent("/files/mkdir", {"path": _api_path(target)})
        self.assertEqual(code, 403, data)
        self.assertFalse((Path(self.outside) / "child").exists())

    def test_mkdir_rejects_parent_symlink_replacement_race(self):
        race_parent = Path(self.music) / "race-parent"
        race_parent.mkdir()
        target = race_parent / "child"

        def replace_parent_with_symlink(path, exist_ok=False):
            shutil.rmtree(race_parent)
            race_parent.symlink_to(Path(self.outside), target_is_directory=True)

        with mock.patch.object(bca, "acquire_os_lock", return_value=None), \
             mock.patch.object(bca, "release_os_lock"), \
             mock.patch.object(bca.os, "makedirs", side_effect=replace_parent_with_symlink):
            code, data = _post_agent("/files/mkdir", {"path": _api_path(target)})
        self.assertEqual(code, 403, data)
        self.assertFalse((Path(self.outside) / "child").exists())

    def test_move_endpoint_uses_canonical_paths_and_rejects_symlink_parent(self):
        src = Path(self.staging) / "download.flac"
        src.write_text("audio", encoding="utf-8")
        dst = Path(self.music) / "Artist" / "Album" / "download.flac"
        with mock.patch.object(bca.shutil, "move") as move_mock:
            code, data = _post_agent("/files/move", {"source_path": _api_path(src), "target_path": _api_path(dst)})
        self.assertEqual(code, 200, data)
        move_mock.assert_called_once_with(str(src.resolve()), str(dst.resolve()))
        self.assertEqual(data["source_path"], str(src.resolve()))
        self.assertEqual(data["target_path"], str(dst.resolve()))

        link_dir = Path(self.music) / "escape-destination"
        try:
            link_dir.symlink_to(Path(self.outside), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation is unavailable")
        with mock.patch.object(bca.shutil, "move") as move_mock:
            code, data = _post_agent("/files/move", {"source_path": _api_path(src), "target_path": _api_path(link_dir / "file.flac")})
        self.assertEqual(code, 403, data)
        move_mock.assert_not_called()

    def test_move_revalidates_parent_after_symlink_race(self):
        src = Path(self.staging) / "download.flac"
        src.write_text("audio", encoding="utf-8")
        race_parent = Path(self.music) / "race-move"
        race_parent.mkdir()
        dst = race_parent / "download.flac"

        def replace_parent_with_symlink(path, exist_ok=False):
            shutil.rmtree(race_parent)
            race_parent.symlink_to(Path(self.outside), target_is_directory=True)

        with mock.patch.object(bca, "acquire_os_lock", return_value=None), \
             mock.patch.object(bca, "release_os_lock"), \
             mock.patch.object(bca.os, "makedirs", side_effect=replace_parent_with_symlink), \
             mock.patch.object(bca.shutil, "move") as move_mock:
            code, data = _post_agent("/files/move", {"source_path": _api_path(src), "target_path": _api_path(dst)})
        self.assertEqual(code, 403, data)
        move_mock.assert_not_called()
        self.assertTrue(src.exists())

    def test_commands_execute_and_jobs_create_use_canonical_paths(self):
        source = Path(self.staging) / "import-source"
        source.mkdir()
        arg_file = Path(self.music) / "Artist" / "Track.flac"
        arg_file.parent.mkdir(parents=True, exist_ok=True)
        arg_file.write_text("audio", encoding="utf-8")

        fake_result = mock.MagicMock(returncode=0, stdout="", stderr="")
        with mock.patch.object(bca.subprocess, "run", return_value=fake_result) as run_mock:
            code, data = _post_agent("/commands/execute", {
                "command": "import",
                "source_path": _api_path(source),
                "args": ["--quiet", _api_path(arg_file)],
            })
        self.assertEqual(code, 200, data)
        full_cmd = run_mock.call_args.args[0]
        self.assertEqual(full_cmd[-4:], ["import", str(source.resolve()), "--quiet", str(arg_file.resolve())])

        created = {}

        class FakeJob:
            def __init__(self, job_id, command, label="", config_override=""):
                self.command = command
                created["job_id"] = job_id
                created["command"] = command

        with mock.patch.object(bca, "AgentJob", FakeJob):
            code, data = _post_agent("/jobs/create", {
                "command": "import",
                "source_path": _api_path(source),
                "args": ["--quiet", _api_path(arg_file)],
            })
        try:
            self.assertEqual(code, 200, data)
            self.assertEqual(created["command"], ["import", str(source.resolve()), "--quiet", str(arg_file.resolve())])
        finally:
            bca.JOBS.pop(created.get("job_id"), None)

    def test_command_path_rejections_do_not_echo_unsafe_values(self):
        unsafe = f"{self.staging}/{_encoded_dotdot(2)}/outside"
        with mock.patch.object(bca.subprocess, "run") as run_mock:
            code, data = _post_agent("/commands/execute", {"command": "import", "source_path": unsafe, "args": []})
        self.assertEqual(code, 403, data)
        run_mock.assert_not_called()
        self.assertNotIn(_encoded_dotdot(2), json.dumps(data))

        with mock.patch.object(bca.subprocess, "run") as run_mock:
            code, data = _post_agent("/commands/execute", {
                "command": "import",
                "source_path": self.staging,
                "args": ["--config=/etc/passwd"],
            })
        self.assertEqual(code, 403, data)
        run_mock.assert_not_called()
        self.assertNotIn("/etc/passwd", json.dumps(data))

        before_jobs = set(bca.JOBS)
        code, data = _post_agent("/jobs/create", {"command": "import", "source_path": unsafe, "args": []})
        self.assertEqual(code, 403, data)
        self.assertEqual(set(bca.JOBS), before_jobs)
        self.assertNotIn(_encoded_dotdot(2), json.dumps(data))

        code, data = _post_agent("/jobs/create", {
            "command": "import",
            "source_path": self.staging,
            "args": ["-c", "/etc/passwd"],
        })
        self.assertEqual(code, 403, data)
        self.assertEqual(set(bca.JOBS), before_jobs)
        self.assertNotIn("/etc/passwd", json.dumps(data))

    def test_tag_read_and_write_use_canonical_mediafile_path(self):
        track = Path(self.music) / "Artist" / "Track.flac"
        track.parent.mkdir(parents=True, exist_ok=True)
        track.write_text("audio", encoding="utf-8")
        seen_paths = []
        saved_paths = []

        class FakeMediaFile:
            def __init__(self, path):
                seen_paths.append(path)
                self.title = "Title"
                self.artist = "Artist"
                self.album = "Album"
                self.albumartist = "Artist"
                self.year = 2026
                self.track = 1
                self.tracktotal = 1
                self.disc = 1
                self.disctotal = 1
                self.genre = "Genre"
                self.mb_trackid = "recording-id"
                self.mb_albumid = "release-id"
                self.mb_artistid = "artist-id"
                self.mb_albumartistid = "albumartist-id"
                self.mb_releasegroupid = "release-group-id"

            def save(self):
                saved_paths.append(seen_paths[-1])

        beets_module = types.ModuleType("beets")
        mediafile_module = types.ModuleType("beets.mediafile")
        mediafile_module.MediaFile = FakeMediaFile
        with mock.patch.dict(sys.modules, {"beets": beets_module, "beets.mediafile": mediafile_module}):
            code, data = _post_agent("/tags/read", {"file_path": _api_path(track)})
            self.assertEqual(code, 200, data)
            self.assertEqual(data["tags"]["title"], "Title")
            code, data = _post_agent("/tags/write", {"file_path": _api_path(track), "tags": {"title": "New"}})
            self.assertEqual(code, 200, data)
        self.assertEqual(seen_paths, [str(track.resolve()), str(track.resolve())])
        self.assertEqual(saved_paths, [str(track.resolve())])

    def test_album_delete_uses_canonical_database_paths_and_sanitized_errors(self):
        db_path = Path(self.base) / "musiclibrary.blb"
        track = Path(self.music) / "Artist" / "Album" / "Track.flac"
        track.parent.mkdir(parents=True, exist_ok=True)
        track.write_text("audio", encoding="utf-8")
        outside_track = Path(self.outside) / "secret.flac"
        outside_track.write_text("secret", encoding="utf-8")
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, album TEXT, path TEXT)")
        con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, path TEXT)")
        con.execute("INSERT INTO albums (id, album, path) VALUES (1, 'Album', ?)", (self.music,))
        con.execute("INSERT INTO items (id, album_id, path) VALUES (10, 1, ?)", (_api_path(track),))
        con.execute("INSERT INTO items (id, album_id, path) VALUES (11, 1, ?)", (_api_path(outside_track),))
        con.commit()
        con.close()

        with mock.patch.object(bca, "LIB_PATH", str(db_path)), \
             mock.patch.object(bca.os, "unlink") as unlink_mock, \
             mock.patch.object(bca.os, "rmdir") as rmdir_mock:
            code, data = bca._handle_delete_album(1, delete_files=True)
        self.assertEqual(code, 200, data)
        unlink_mock.assert_any_call(str(track.resolve()))
        self.assertNotIn(mock.call(str(outside_track.resolve())), unlink_mock.mock_calls)
        rmdir_mock.assert_not_called()
        self.assertEqual(data["files_failed"], 1)
        self.assertNotIn(str(outside_track), json.dumps(data))
        self.assertNotIn("secret.flac", json.dumps(data))

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



class CodeQLPathModelIntegrityTests(unittest.TestCase):
    def test_path_barrier_model_is_narrow(self):
        model = Path(".github/codeql/extensions/pr39-path-sanitizers/models/beets-control-agent-path.yml")
        self.assertTrue(model.exists())
        text = model.read_text(encoding="utf-8")
        self.assertIn("extensible: barrierModel", text)
        self.assertIn("codeql/python-all", text)
        self.assertIn('"backend.beets_control_agent"', text)
        self.assertIn('"Member[resolve_safe_path].ReturnValue"', text)
        self.assertIn('"path-injection"', text)
        self.assertNotIn("Argument[", text)
        self.assertNotIn("sourceModel", text)
        self.assertNotIn("sinkModel", text)
        self.assertNotIn("barrierGuardModel", text)
        self.assertEqual(text.count("path-injection"), 1)

    def test_model_pack_does_not_weaken_codeql_policy(self):
        pack = Path(".github/codeql/extensions/pr39-path-sanitizers/codeql-pack.yml")
        self.assertTrue(pack.exists())
        pack_text = pack.read_text(encoding="utf-8")
        self.assertIn("extensionTargets:", pack_text)
        self.assertIn("codeql/python-all", pack_text)
        self.assertIn("dataExtensions:", pack_text)

        github_text = "\n".join(
            p.read_text(encoding="utf-8")
            for p in Path(".github").rglob("*.yml")
        )
        forbidden = [
            "continue-on-error",
            "paths-ignore",
            "paths:",
            "exclude:",
            "security-severity",
            "disable-default-queries",
        ]
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, github_text)
if __name__ == "__main__":
    unittest.main()
