"""Unit and integration tests for the external Beets control agent architecture."""

import json
import logging
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest import mock

from app import _resolve_album_title_duplicate_candidate
from backend.beets_client import (
    BeetsAuthError,
    BeetsClient,
    BeetsCommandError,
    BeetsError,
    BeetsUnavailableError,
    RemoteAlbum,
    RemoteItem,
    RemoteLibrary,
    get_db_connection,
    parse_query_term,
)
from job_engine import Job, _beet_run, _parse_remote_beet_command
from backend.beets_control_agent import (
    ControlAgentHandler,
    _handle_delete_album,
    is_safe_path,
)

ROOT = Path(__file__).resolve().parents[1]


class TestBeetsControlAgentSecurity(unittest.TestCase):
    def test_is_safe_path_valid(self):
        self.assertTrue(is_safe_path("/config/config.yaml", ["config"]))
        self.assertTrue(is_safe_path("/data/media/music/Artist/Album", ["music"]))
        self.assertTrue(is_safe_path("/data/torrents/downloads", ["staging"]))
        self.assertTrue(is_safe_path("/tmp/test_file.tmp", ["tmp"]))

    def test_is_safe_path_relative_traversal(self):
        self.assertFalse(is_safe_path("../../etc/passwd"))
        self.assertFalse(is_safe_path("/config/../etc/passwd"))
        self.assertFalse(is_safe_path("relative/path"))

    def test_is_safe_path_null_bytes_and_backslashes(self):
        self.assertFalse(is_safe_path("/config/file.txt\x00.sh"))
        self.assertFalse(is_safe_path("\\config\\config.yaml"))
        self.assertFalse(is_safe_path("/data/media/music/..%2f..%2fetc/passwd"))

    def test_is_safe_path_symlink_escape(self):
        """Test that symlinks resolving outside allowed root are rejected."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            target = tmp_path / "secret.txt"
            target.write_text("secret")

            music_fake = tmp_path / "music_fake"
            music_fake.mkdir()
            symlink = music_fake / "escape_link"
            try:
                os.symlink(target, symlink)
            except (OSError, NotImplementedError):
                self.skipTest("Symlink creation not supported on this platform/user")

            with mock.patch("backend.beets_control_agent.MUSIC_LIBRARY_PATH", str(music_fake)):
                self.assertFalse(is_safe_path(str(symlink), ["music"]))

    def test_auth_token_fail_closed(self):
        with mock.patch("backend.beets_control_agent.BEETS_API_TOKEN", ""):
            handler = mock.MagicMock(spec=ControlAgentHandler)
            handler.headers = {}
            result = ControlAgentHandler._authenticate(handler)
            self.assertFalse(result)

    def test_raw_query_security_handler_execution(self):
        """Test raw SQL handler execution rejecting mutations and multi-statements."""
        handler = ControlAgentHandler.__new__(ControlAgentHandler)
        handler.headers = {"X-Beets-API-Token": "testtoken"}

        sent_responses = []
        def _send_json(code, data):
            sent_responses.append((code, data))

        handler._send_json = _send_json
        handler._authenticate = lambda: True

        forbidden_queries = [
            "UPDATE items SET title='hacked'",
            "DELETE FROM items",
            "INSERT INTO items DEFAULT VALUES",
            "DROP TABLE items",
            "ATTACH DATABASE '/tmp/x' AS x",
            "WITH changed AS (DELETE FROM items RETURNING *) SELECT * FROM changed",
            "PRAGMA writable_schema=ON",
            "/* comment */ DELETE FROM items",
            "SELECT * FROM items; DELETE FROM items;",
        ]

        for q in forbidden_queries:
            sent_responses.clear()
            handler.path = "/library/raw_query"
            handler.rfile = BytesIO(json.dumps({"query": q}).encode("utf-8"))
            handler.headers["Content-Length"] = str(len(json.dumps({"query": q})))
            handler.do_POST()
            self.assertTrue(len(sent_responses) > 0, f"No response sent for query: {q}")
            code, data = sent_responses[0]
            self.assertEqual(code, 400, f"Query '{q}' should have been rejected with 400, got {code}")
            self.assertIn("error", data)

    def test_s6_service_discovery_path_in_dockerfile(self):
        """Assert Dockerfile.beets uses LinuxServer supported /custom-services.d path."""
        dockerfile_path = ROOT / "Dockerfile.beets"
        self.assertTrue(dockerfile_path.exists(), "Dockerfile.beets must exist")
        content = dockerfile_path.read_text(encoding="utf-8")

        self.assertIn("/custom-services.d", content)
        self.assertIn("COPY docker/beets-agent-s6.sh /custom-services.d/beets-control-agent", content)
        self.assertIn("chmod +x /custom-services.d/beets-control-agent", content)
        self.assertNotIn("/etc/custom-services.d", content)

        compose_path = ROOT / "docker-compose.yml"
        if compose_path.exists():
            self.assertNotIn("/etc/custom-services.d", compose_path.read_text(encoding="utf-8"))


class TestBeetsClientRemoteOnly(unittest.TestCase):
    def test_get_db_connection_never_uses_local_sqlite(self):
        with tempfile.NamedTemporaryFile(suffix=".blb", delete=False) as tmp:
            tmp.write(b"dummy db")
            tmp_path = tmp.name

        try:
            conn = get_db_connection(tmp_path)
            self.assertEqual(conn.__class__.__name__, "RemoteSQLiteConnection")
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_beets_client_auth_failure_raises_beets_auth_error(self):
        client = BeetsClient(base_url="http://127.0.0.1:9999", token="invalid")
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_err = urllib.error.HTTPError(
                "http://127.0.0.1:9999", 401, "Unauthorized", {}, None
            )
            mock_urlopen.side_effect = mock_err
            with self.assertRaises(BeetsAuthError):
                client.version()

    def test_beets_client_unreachable_raises_beets_unavailable_error(self):
        client = BeetsClient(base_url="http://127.0.0.1:9999", token="test")
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
            with self.assertRaises(BeetsUnavailableError):
                client.version()

    def test_web_manager_source_code_contains_no_beets_imports(self):
        """Assert that web-manager production code contains zero beets imports."""
        files_to_check = [
            ROOT / "app.py",
            ROOT / "job_engine.py",
            ROOT / "routes_submissions.py",
            ROOT / "backend" / "beets_client.py",
        ]
        pattern = re.compile(r"^\s*(import\s+beets|from\s+beets\b|import\s+beetsplug|from\s+beetsplug\b)", re.MULTILINE)

        for file_path in files_to_check:
            if not file_path.exists():
                continue
            content = file_path.read_text(encoding="utf-8")
            matches = pattern.findall(content)
            self.assertEqual(matches, [], f"Found forbidden Beets import in {file_path.name}: {matches}")

    def test_unsupported_remote_library_query_raises_beets_error(self):
        """Assert that RemoteLibrary raises BeetsError for unsupported query shapes."""
        client = mock.MagicMock(spec=BeetsClient)
        rlib = RemoteLibrary(client)

        with self.assertRaises(BeetsError):
            rlib.items({"unsupported": "query"})

        with self.assertRaises(BeetsError):
            rlib.items("genre:jazz")

        with self.assertRaises(BeetsError):
            rlib.albums(12345)

        with self.assertRaises(BeetsError):
            rlib.albums("year:1959")

    def test_supported_remote_library_query_shapes(self):
        """Assert that RemoteLibrary handles supported query shapes cleanly via dedicated methods."""
        client = mock.MagicMock(spec=BeetsClient)
        client.find_all_items_by_album_id.return_value = [{"id": 1, "title": "Track 1"}]
        client.find_all_items_for_term.return_value = [{"id": 1, "title": "Track 1"}]
        client.find_all_albums_for_term.return_value = [{"id": 10, "album": "Kind of Blue"}]

        rlib = RemoteLibrary(client)

        items = rlib.items("album_id:10")
        client.find_all_items_by_album_id.assert_called_with(10)
        self.assertEqual(len(items), 1)

        items = rlib.items("album:Kind of Blue")
        client.find_all_items_for_term.assert_called_with("album:Kind of Blue")
        self.assertEqual(len(items), 1)

        items = rlib.items(["album:Kind of Blue"])
        self.assertEqual(len(items), 1)

        albums = rlib.albums("Kind of Blue")
        client.find_all_albums_for_term.assert_called_with("Kind of Blue")
        self.assertEqual(len(albums), 1)

    def test_large_library_client_auto_pagination(self):
        """Test that BeetsClient.list_all_items fetches all pages for >2500 items."""
        client = BeetsClient(base_url="http://127.0.0.1:8338", token="test")

        def mock_request(method, endpoint, data=None, timeout=None):
            if "offset=0" in endpoint:
                page = [{"id": i, "title": f"Track {i}"} for i in range(1, 1001)]
                return {"items": page, "count": 1000, "total": 2500, "offset": 0, "limit": 1000, "has_more": True, "next_offset": 1000}
            elif "offset=1000" in endpoint:
                page = [{"id": i, "title": f"Track {i}"} for i in range(1001, 2001)]
                return {"items": page, "count": 1000, "total": 2500, "offset": 1000, "limit": 1000, "has_more": True, "next_offset": 2000}
            elif "offset=2000" in endpoint:
                page = [{"id": i, "title": f"Track {i}"} for i in range(2001, 2501)]
                return {"items": page, "count": 500, "total": 2500, "offset": 2000, "limit": 1000, "has_more": False, "next_offset": None}
            return {}

        with mock.patch.object(client, "_request", side_effect=mock_request):
            items = client.list_all_items()
            self.assertEqual(len(items), 2500)
            self.assertEqual(items[0]["id"], 1)
            self.assertEqual(items[-1]["id"], 2500)

    def test_album_delete_partial_failure_status_contract(self):
        """Test partial failure response when database is deleted but files fail to delete."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "musiclibrary.blb"
            music_path = Path(tmp_dir) / "music"
            music_path.mkdir()

            track_file = music_path / "track1.flac"
            track_file.write_bytes(b"dummy audio")

            con = sqlite3.connect(str(db_path))
            con.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, album TEXT, path TEXT)")
            con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, path TEXT)")
            con.execute("INSERT INTO albums (id, album, path) VALUES (1, 'Test', ?)", (str(music_path),))
            con.execute("INSERT INTO items (id, album_id, path) VALUES (10, 1, ?)", (str(track_file),))
            con.commit()
            con.close()

            with mock.patch("backend.beets_control_agent.LIB_PATH", str(db_path)), \
                 mock.patch("backend.beets_control_agent.is_safe_path", return_value=True), \
                 mock.patch("os.unlink", side_effect=PermissionError("Permission denied")):
                code, res = _handle_delete_album(1, delete_files=True)
                self.assertEqual(code, 200)
                self.assertFalse(res["success"])
                self.assertEqual(res["status"], "partial_failure")
                self.assertTrue(res["database_deleted"])
                self.assertEqual(res["files_deleted"], 0)
                self.assertEqual(res["files_failed"], 1)
                self.assertTrue(len(res["file_errors"]) > 0)

    def test_docker_web_manager_image_has_no_beets_binary(self):
        """Test web-manager container image has no beet executable (skipped if Docker daemon unavailable)."""
        try:
            res = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=5)
            if res.returncode != 0:
                raise unittest.SkipTest("Docker daemon unavailable on this host")
        except Exception:
            raise unittest.SkipTest("Docker CLI unavailable on this host")

        res = subprocess.run(
            ["docker", "run", "--rm", "beets-web-manager:0.1.0", "command", "-v", "beet"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(res.returncode, 0, "beet binary should not be installed in beets-web-manager image")


class TestStructuredQueryRouting(unittest.TestCase):
    """Test client routing and control-agent server interpretation for structured queries against SQLite."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "musiclibrary.blb"

        con = sqlite3.connect(str(self.db_path))
        con.execute("""
            CREATE TABLE albums (
                id INTEGER PRIMARY KEY,
                album TEXT,
                artist TEXT,
                albumartist TEXT,
                mb_albumid TEXT,
                mb_releasegroupid TEXT,
                path TEXT
            )
        """)
        con.execute("""
            CREATE TABLE items (
                id INTEGER PRIMARY KEY,
                album_id INTEGER,
                title TEXT,
                artist TEXT,
                album TEXT,
                albumartist TEXT,
                mb_trackid TEXT,
                path TEXT
            )
        """)

        # Add representative albums
        con.execute("INSERT INTO albums VALUES (10, 'Voyager', '311', '311', 'album-mbid-aaa', 'rg-mbid-111', '/data/music/311/Voyager')")
        con.execute("INSERT INTO albums VALUES (20, 'From Chaos', '311', '311', 'album-mbid-bbb', 'rg-mbid-222', '/data/music/311/From Chaos')")
        # Row with literal text string matching query shape to prove text search is NOT used
        con.execute("INSERT INTO albums VALUES (30, 'album_id:100', 'mb_releasegroupid:rg-mbid-111', 'Artist', 'album-mbid-ccc', 'rg-mbid-333', '/data/music/other')")

        # Add representative items
        con.execute("INSERT INTO items VALUES (1, 100, 'Down', '311', 'Voyager', '311', 'track-mbid-111', '/data/music/311/Voyager/01.flac')")
        con.execute("INSERT INTO items VALUES (2, 100, 'Amber', '311', 'Voyager', '311', 'track-mbid-222', '/data/music/311/Voyager/02.flac')")
        con.execute("INSERT INTO items VALUES (3, 200, 'Beautiful Disaster', '311', 'From Chaos', '311', 'track-mbid-333', '/data/music/311/From Chaos/01.flac')")
        # Row with literal text string matching query shape
        con.execute("INSERT INTO items VALUES (4, 300, 'album_id:100', 'Artist', 'album_id:100', 'Artist', 'mb_trackid:track-mbid-111', '/data/music/fake')")

        con.commit()
        con.close()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_structured_album_id_routing_and_execution(self):
        with mock.patch("backend.beets_control_agent.LIB_PATH", str(self.db_path)):
            handler = ControlAgentHandler.__new__(ControlAgentHandler)
            handler.headers = {"Authorization": "Bearer test"}
            handler._authenticate = lambda: True

            client = BeetsClient(base_url="http://127.0.0.1:8338", token="test")
            rlib = RemoteLibrary(client)

            def mock_request(method, endpoint, data=None, timeout=None):
                parsed = urllib.parse.urlparse(endpoint)
                handler.path = parsed.path + "?" + parsed.query
                res_data = []

                def _send_json(code, d):
                    res_data.append((code, d))

                handler._send_json = _send_json
                handler.do_GET()
                return res_data[0][1]

            with mock.patch.object(client, "_request", side_effect=mock_request), \
                 mock.patch.object(client, "search_items_text") as mock_bare_text:

                items = rlib.items("album_id:100")
                self.assertEqual(len(items), 2)
                self.assertEqual([i.id for i in items], [1, 2])
                mock_bare_text.assert_not_called()

                with self.assertRaises(BeetsError):
                    rlib.items("album_id:notanint")

    def test_structured_mb_trackid_routing_and_execution(self):
        with mock.patch("backend.beets_control_agent.LIB_PATH", str(self.db_path)):
            client = BeetsClient(base_url="http://127.0.0.1:8338", token="test")
            rlib = RemoteLibrary(client)

            handler = ControlAgentHandler.__new__(ControlAgentHandler)
            handler.headers = {"Authorization": "Bearer test"}
            handler._authenticate = lambda: True

            def mock_request(method, endpoint, data=None, timeout=None):
                parsed = urllib.parse.urlparse(endpoint)
                handler.path = parsed.path + "?" + parsed.query
                res_data = []
                handler._send_json = lambda c, d: res_data.append((c, d))
                handler.do_GET()
                return res_data[0][1]

            with mock.patch.object(client, "_request", side_effect=mock_request):
                items = rlib.items("mb_trackid:track-mbid-111")
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0].id, 1)

                items2 = rlib.items("mbid:track-mbid-222")
                self.assertEqual(len(items2), 1)
                self.assertEqual(items2[0].id, 2)

                with self.assertRaises(BeetsError):
                    rlib.items("mb_trackid:")

    def test_structured_mb_albumid_routing_and_execution(self):
        with mock.patch("backend.beets_control_agent.LIB_PATH", str(self.db_path)):
            client = BeetsClient(base_url="http://127.0.0.1:8338", token="test")
            rlib = RemoteLibrary(client)

            handler = ControlAgentHandler.__new__(ControlAgentHandler)
            handler.headers = {"Authorization": "Bearer test"}
            handler._authenticate = lambda: True

            def mock_request(method, endpoint, data=None, timeout=None):
                parsed = urllib.parse.urlparse(endpoint)
                handler.path = parsed.path + "?" + parsed.query
                res_data = []
                handler._send_json = lambda c, d: res_data.append((c, d))
                handler.do_GET()
                return res_data[0][1]

            with mock.patch.object(client, "_request", side_effect=mock_request), \
                 mock.patch.object(client, "search_albums_text") as mock_bare_text:

                albums = rlib.albums("mb_albumid:album-mbid-aaa")
                self.assertEqual(len(albums), 1)
                self.assertEqual(albums[0].id, 10)
                mock_bare_text.assert_not_called()

                with self.assertRaises(BeetsError):
                    rlib.albums("mb_albumid:")

    def test_structured_mb_releasegroupid_routing_and_execution(self):
        with mock.patch("backend.beets_control_agent.LIB_PATH", str(self.db_path)):
            client = BeetsClient(base_url="http://127.0.0.1:8338", token="test")
            rlib = RemoteLibrary(client)

            handler = ControlAgentHandler.__new__(ControlAgentHandler)
            handler.headers = {"Authorization": "Bearer test"}
            handler._authenticate = lambda: True

            def mock_request(method, endpoint, data=None, timeout=None):
                parsed = urllib.parse.urlparse(endpoint)
                handler.path = parsed.path + "?" + parsed.query
                res_data = []
                handler._send_json = lambda c, d: res_data.append((c, d))
                handler.do_GET()
                return res_data[0][1]

            with mock.patch.object(client, "_request", side_effect=mock_request):
                albums = rlib.albums("mb_releasegroupid:rg-mbid-111")
                self.assertEqual(len(albums), 1)
                self.assertEqual(albums[0].id, 10)

                # Verify releasegroupid does NOT match releaseid or text
                albums_none = rlib.albums("mb_releasegroupid:album-mbid-aaa")
                self.assertEqual(len(albums_none), 0)

                with self.assertRaises(BeetsError):
                    rlib.albums("mb_releasegroupid:")

    def test_unknown_fields_raise(self):
        client = mock.MagicMock(spec=BeetsClient)
        rlib = RemoteLibrary(client)

        with self.assertRaises(BeetsError):
            rlib.items("unknown_field:123")

        with self.assertRaises(BeetsError):
            rlib.albums("unknown_field:123")


class TestSingletonQueryRouting(unittest.TestCase):
    """Real client and control-agent handler tests for singleton:true and singleton:false filtering."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "musiclibrary.blb"

        con = sqlite3.connect(str(self.db_path))
        con.execute("""
            CREATE TABLE items (
                id INTEGER PRIMARY KEY,
                album_id INTEGER,
                title TEXT,
                artist TEXT,
                album TEXT,
                path TEXT
            )
        """)

        # Item 1: album_id = NULL (singleton)
        con.execute("INSERT INTO items VALUES (1, NULL, 'Singleton 1', 'Artist A', '', '/data/music/s1.flac')")
        # Item 2: album_id = 0 (singleton per project evidence)
        con.execute("INSERT INTO items VALUES (2, 0, 'Singleton 2', 'Artist B', '', '/data/music/s2.flac')")
        # Item 3: album_id = 100 (album track)
        con.execute("INSERT INTO items VALUES (3, 100, 'Track 1', 'Artist C', 'Album 1', '/data/music/a1/t1.flac')")
        # Item 4: album_id = 200 (album track)
        con.execute("INSERT INTO items VALUES (4, 200, 'Track 2', 'Artist D', 'Album 2', '/data/music/a2/t2.flac')")

        con.commit()
        con.close()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _setup_handler_and_client(self):
        handler = ControlAgentHandler.__new__(ControlAgentHandler)
        handler.headers = {"Authorization": "Bearer test"}
        handler._authenticate = lambda: True

        client = BeetsClient(base_url="http://127.0.0.1:8338", token="test")
        rlib = RemoteLibrary(client)

        received_endpoints = []

        def mock_request(method, endpoint, data=None, timeout=None):
            received_endpoints.append(endpoint)
            parsed = urllib.parse.urlparse(endpoint)
            handler.path = parsed.path + "?" + parsed.query
            res_data = []

            def _send_json(code, d):
                res_data.append((code, d))

            handler._send_json = _send_json
            handler.do_GET()
            return res_data[0][1]

        return handler, client, rlib, mock_request, received_endpoints

    def test_singleton_true_returns_only_singletons(self):
        with mock.patch("backend.beets_control_agent.LIB_PATH", str(self.db_path)):
            _, client, rlib, mock_req, endpoints = self._setup_handler_and_client()
            with mock.patch.object(client, "_request", side_effect=mock_req):
                items = rlib.items("singleton:true")
                self.assertEqual(len(items), 2)
                self.assertEqual({i.id for i in items}, {1, 2})
                self.assertIn("/items?singleton=true", endpoints[0])

    def test_singleton_false_returns_only_album_tracks(self):
        with mock.patch("backend.beets_control_agent.LIB_PATH", str(self.db_path)):
            _, client, rlib, mock_req, endpoints = self._setup_handler_and_client()
            with mock.patch.object(client, "_request", side_effect=mock_req):
                items = rlib.items("singleton:false")
                self.assertEqual(len(items), 2)
                self.assertEqual({i.id for i in items}, {3, 4})
                self.assertIn("/items?singleton=false", endpoints[0])

    def test_singleton_false_does_not_return_all_items(self):
        """Regression test ensuring singleton=false sends singleton=false and does NOT return unfiltered library."""
        with mock.patch("backend.beets_control_agent.LIB_PATH", str(self.db_path)):
            _, client, rlib, mock_req, endpoints = self._setup_handler_and_client()
            with mock.patch.object(client, "_request", side_effect=mock_req):
                items = rlib.items("singleton:false")
                # Must return ONLY 2 items (album tracks), NOT all 4 items
                self.assertNotEqual(len(items), 4, "singleton:false must NOT return the entire library")
                self.assertEqual(len(items), 2)
                self.assertEqual(set(i.id for i in items), {3, 4})

    def test_singleton_case_insensitivity(self):
        with mock.patch("backend.beets_control_agent.LIB_PATH", str(self.db_path)):
            _, client, rlib, mock_req, _ = self._setup_handler_and_client()
            with mock.patch.object(client, "_request", side_effect=mock_req):
                items_true = rlib.items("singleton:TRUE")
                self.assertEqual({i.id for i in items_true}, {1, 2})

                items_false = rlib.items("singleton:False")
                self.assertEqual({i.id for i in items_false}, {3, 4})

                items_false_upper = rlib.items("singleton:FALSE")
                self.assertEqual({i.id for i in items_false_upper}, {3, 4})

    def test_singleton_invalid_values_raise(self):
        with mock.patch("backend.beets_control_agent.LIB_PATH", str(self.db_path)):
            _, client, rlib, mock_req, _ = self._setup_handler_and_client()
            with mock.patch.object(client, "_request", side_effect=mock_req):
                with self.assertRaises(BeetsError):
                    rlib.items("singleton:maybe")

                with self.assertRaises(BeetsError):
                    rlib.items("singleton:")

            # Test direct HTTP request with invalid singleton value returns 400 Bad Request
            handler = ControlAgentHandler.__new__(ControlAgentHandler)
            handler.headers = {"Authorization": "Bearer test"}
            handler._authenticate = lambda: True
            handler.path = "/items?singleton=invalid"
            sent_responses = []
            handler._send_json = lambda code, data: sent_responses.append((code, data))
            handler.do_GET()

            self.assertEqual(len(sent_responses), 1)
            code, data = sent_responses[0]
            self.assertEqual(code, 400)
            self.assertIn("error", data)

    def test_singleton_pagination_page2(self):
        """Test that page-two singleton matches are included completely."""
        client = BeetsClient(base_url="http://127.0.0.1:8338", token="test")
        rlib = RemoteLibrary(client)

        page1 = [{"id": i, "album_id": None} for i in range(1, 501)]
        page2 = [{"id": 501, "album_id": None}]

        def mock_request(method, endpoint, data=None, timeout=None):
            if "offset=0" in endpoint:
                return {"items": page1, "count": 500, "total": 501, "offset": 0, "limit": 500, "has_more": True, "next_offset": 500}
            elif "offset=500" in endpoint:
                return {"items": page2, "count": 1, "total": 501, "offset": 500, "limit": 500, "has_more": False, "next_offset": None}
            return {}

        with mock.patch.object(client, "_request", side_effect=mock_request):
            items = rlib.items("singleton:true")
            self.assertEqual(len(items), 501)
            self.assertEqual(items[-1].id, 501)


class TestRemoteLibraryQuerySemantics(unittest.TestCase):
    def setUp(self):
        self.client = mock.MagicMock(spec=BeetsClient)
        self.rlib = RemoteLibrary(self.client)

    def test_bare_word_item_search(self):
        item1 = {"id": 1, "artist": "311", "title": "Down", "album": "Voyager"}
        item3 = {"id": 3, "artist": "Daft Punk", "title": "Voyager", "album": "Random Access Memories"}
        self.client.find_all_items_for_term.return_value = [item1, item3]

        items = self.rlib.items("Voyager")
        self.client.find_all_items_for_term.assert_called_with("Voyager")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].title, "Down")
        self.assertEqual(items[1].artist, "Daft Punk")

    def test_bare_word_album_search(self):
        alb1 = {"id": 10, "albumartist": "311", "album": "Voyager"}
        self.client.find_all_albums_for_term.return_value = [alb1]

        albums = self.rlib.albums("Voyager")
        self.client.find_all_albums_for_term.assert_called_with("Voyager")
        self.assertEqual(len(albums), 1)
        self.assertEqual(albums[0].album, "Voyager")

    def test_two_term_intersection(self):
        item1 = {"id": 1, "artist": "311", "title": "Down", "album": "Voyager"}
        item2 = {"id": 2, "artist": "311", "title": "Amber", "album": "From Chaos"}
        item3 = {"id": 3, "artist": "Daft Punk", "title": "Voyager", "album": "Random Access Memories"}

        def mock_query(q):
            if q == "album:Voyager":
                return [item1, item3]
            elif q == "artist:311":
                return [item1, item2]
            return []

        self.client.find_all_items_for_term.side_effect = mock_query

        res = self.rlib.items(["album:Voyager", "artist:311"])
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].id, 1)

    def test_three_term_intersection(self):
        item1 = {"id": 1, "artist": "311", "title": "Down", "album": "Voyager"}
        item2 = {"id": 2, "artist": "311", "title": "Amber", "album": "From Chaos"}
        item3 = {"id": 3, "artist": "Daft Punk", "title": "Voyager", "album": "Random Access Memories"}

        def mock_query(q):
            if q == "album:Voyager":
                return [item1, item3]
            elif q == "artist:311":
                return [item1, item2]
            elif q == "title:Down":
                return [item1]
            return []

        self.client.find_all_items_for_term.side_effect = mock_query

        res = self.rlib.items(["album:Voyager", "artist:311", "title:Down"])
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].id, 1)

    def test_empty_intersection(self):
        item1 = {"id": 1, "artist": "311", "title": "Down", "album": "Voyager"}
        item2 = {"id": 2, "artist": "Daft Punk", "title": "Voyager", "album": "Random Access Memories"}

        def mock_query(q):
            if q == "title:Down":
                return [item1]
            elif q == "artist:Daft Punk":
                return [item2]
            return []

        self.client.find_all_items_for_term.side_effect = mock_query

        res = self.rlib.items(["title:Down", "artist:Daft Punk"])
        self.assertEqual(len(res), 0)

    def test_duplicate_prevention_in_intersection(self):
        item1 = {"id": 1, "artist": "311", "title": "Down", "album": "Voyager"}

        self.client.find_all_items_for_term.return_value = [item1, item1]
        res = self.rlib.items(["album:Voyager", "artist:311"])
        self.assertEqual(len(res), 1)

    def test_mixed_supported_and_unsupported_terms_raises(self):
        with self.assertRaises(BeetsError):
            self.rlib.items(["album:Voyager", "genre:rock"])

    def test_malformed_terms_raise(self):
        malformed_queries = [
            "album_id:nope",
            "singleton:maybe",
            "album:",
            ":value",
            ":",
        ]
        for mq in malformed_queries:
            with self.assertRaises(BeetsError, msg=f"Should raise for malformed: {mq}"):
                self.rlib.items(mq)

    def test_album_list_intersection(self):
        alb1 = {"id": 10, "albumartist": "311", "album": "Voyager"}
        alb2 = {"id": 20, "albumartist": "311", "album": "From Chaos"}

        def mock_album_query(q):
            if q == "album:Voyager":
                return [alb1]
            elif q == "artist:311":
                return [alb1, alb2]
            return []

        self.client.find_all_albums_for_term.side_effect = mock_album_query

        res = self.rlib.albums(["album:Voyager", "artist:311"])
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].id, 10)

    def test_real_multipage_http_intersection(self):
        """Test real multi-page HTTP request sequencing and page-2 intersection."""
        client = BeetsClient(base_url="http://127.0.0.1:8338", token="test")
        rlib = RemoteLibrary(client)

        term_a_page1 = [{"id": i, "album": "Voyager"} for i in range(1, 501)]
        term_a_page2 = [{"id": i, "album": "Voyager"} for i in range(501, 601)]

        term_b_page1 = [{"id": i, "artist": "311"} for i in range(1001, 1501)]
        term_b_page2 = [{"id": 550, "artist": "311"}] + [{"id": i, "artist": "311"} for i in range(1501, 1551)]

        http_requests = []

        def mock_request(method, endpoint, data=None, timeout=None):
            http_requests.append(endpoint)
            if "query=album%3AVoyager" in endpoint and "offset=0" in endpoint:
                return {"items": term_a_page1, "count": 500, "total": 600, "offset": 0, "limit": 500, "has_more": True, "next_offset": 500}
            elif "query=album%3AVoyager" in endpoint and "offset=500" in endpoint:
                return {"items": term_a_page2, "count": 100, "total": 600, "offset": 500, "limit": 500, "has_more": False, "next_offset": None}
            elif "query=artist%3A311" in endpoint and "offset=0" in endpoint:
                return {"items": term_b_page1, "count": 500, "total": 551, "offset": 0, "limit": 500, "has_more": True, "next_offset": 500}
            elif "query=artist%3A311" in endpoint and "offset=500" in endpoint:
                return {"items": term_b_page2, "count": 51, "total": 551, "offset": 500, "limit": 500, "has_more": False, "next_offset": None}
            return {}

        with mock.patch.object(client, "_request", side_effect=mock_request):
            res = rlib.items(["album:Voyager", "artist:311"])

            self.assertTrue(len([r for r in http_requests if "query=album%3AVoyager" in r]) >= 2)
            self.assertTrue(len([r for r in http_requests if "query=artist%3A311" in r]) >= 2)

            self.assertEqual(len(res), 1)
            self.assertEqual(res[0].id, 550)

    def test_bare_word_list_query_multipage_intersection(self):
        """Test bare-word list query ["311", "Voyager"] intersecting across multiple pages."""
        client = BeetsClient(base_url="http://127.0.0.1:8338", token="test")
        rlib = RemoteLibrary(client)

        page1_311 = [{"id": i, "artist": "311"} for i in range(1, 501)]
        page2_311 = [{"id": 550, "artist": "311", "album": "Voyager"}]

        page1_voyager = [{"id": i, "album": "Voyager"} for i in range(1000, 1500)]
        page2_voyager = [{"id": 550, "artist": "311", "album": "Voyager"}]

        def mock_request(method, endpoint, data=None, timeout=None):
            if "query=311" in endpoint and "offset=0" in endpoint:
                return {"items": page1_311, "count": 500, "total": 501, "offset": 0, "limit": 500, "has_more": True, "next_offset": 500}
            elif "query=311" in endpoint and "offset=500" in endpoint:
                return {"items": page2_311, "count": 1, "total": 501, "offset": 500, "limit": 500, "has_more": False, "next_offset": None}
            elif "query=Voyager" in endpoint and "offset=0" in endpoint:
                return {"items": page1_voyager, "count": 500, "total": 501, "offset": 0, "limit": 500, "has_more": True, "next_offset": 500}
            elif "query=Voyager" in endpoint and "offset=500" in endpoint:
                return {"items": page2_voyager, "count": 1, "total": 501, "offset": 500, "limit": 500, "has_more": False, "next_offset": None}
            return {}

        with mock.patch.object(client, "_request", side_effect=mock_request):
            res = rlib.items(["311", "Voyager"])
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0].id, 550)

    def test_pagination_failure_repeated_next_offset(self):
        client = BeetsClient(base_url="http://127.0.0.1:8338", token="test")
        def mock_request(method, endpoint, data=None, timeout=None):
            return {"items": [{"id": 1}], "count": 1, "total": 10, "offset": 0, "limit": 500, "has_more": True, "next_offset": 0}

        with mock.patch.object(client, "_request", side_effect=mock_request):
            with self.assertRaises(BeetsError):
                client.find_all_items_for_term("Voyager")

    def test_pagination_failure_empty_page_with_has_more(self):
        client = BeetsClient(base_url="http://127.0.0.1:8338", token="test")
        def mock_request(method, endpoint, data=None, timeout=None):
            return {"items": [], "count": 0, "total": 10, "offset": 0, "limit": 500, "has_more": True, "next_offset": 500}

        with mock.patch.object(client, "_request", side_effect=mock_request):
            with self.assertRaises(BeetsError):
                client.find_all_items_for_term("Voyager")

    def test_pagination_failure_inconsistent_total(self):
        client = BeetsClient(base_url="http://127.0.0.1:8338", token="test")
        def mock_request(method, endpoint, data=None, timeout=None):
            return {"items": [{"id": 1}], "count": 1, "total": 1, "offset": 0, "limit": 500, "has_more": True, "next_offset": 500}

        with mock.patch.object(client, "_request", side_effect=mock_request):
            with self.assertRaises(BeetsError):
                client.find_all_items_for_term("Voyager")

    def test_pagination_failure_missing_next_offset(self):
        client = BeetsClient(base_url="http://127.0.0.1:8338", token="test")
        def mock_request(method, endpoint, data=None, timeout=None):
            return {"items": [{"id": 1}], "count": 1, "total": 10, "offset": 0, "limit": 500, "has_more": True, "next_offset": None}

        with mock.patch.object(client, "_request", side_effect=mock_request):
            with self.assertRaises(BeetsError):
                client.find_all_items_for_term("Voyager")

    def test_pagination_failure_safety_ceiling(self):
        client = BeetsClient(base_url="http://127.0.0.1:8338", token="test")
        def mock_request(method, endpoint, data=None, timeout=None):
            offset = int(endpoint.split("offset=")[1].split("&")[0])
            items = [{"id": offset + i} for i in range(1, 101)]
            return {"items": items, "count": 100, "total": 1000, "offset": offset, "limit": 100, "has_more": True, "next_offset": offset + 100}

        with mock.patch.object(client, "_request", side_effect=mock_request):
            with self.assertRaises(BeetsError):
                client.find_all_items_for_term("Voyager", safety_ceiling=500)

    def test_duplicate_detection_functional_regression(self):
        """End-to-end regression test invoking production function _resolve_album_title_duplicate_candidate with realistic query boundary and candidate verification."""
        mock_library = mock.MagicMock(spec=RemoteLibrary)
        test_logger = logging.getLogger("test_dedup")

        cand1 = RemoteItem({"id": 1, "title": "Down", "album": "Voyager"})
        cand2 = RemoteItem({"id": 2, "title": "Amber", "album": "Voyager"})
        cand3 = RemoteItem({"id": 3, "title": "Down", "album": "From Chaos"})
        cand4 = RemoteItem({"id": 4, "title": "Amber", "album": "From Chaos"})

        # Realistic query-layer behavior: library.items("album:<X>") filters by album at the query boundary
        def mock_items(query=None):
            if query == "album:Voyager":
                return [cand1, cand2]
            elif query == "album:From Chaos":
                return [cand3, cand4]
            return []

        mock_library.items.side_effect = mock_items

        # Case 1: Same album ("Voyager") + Same title ("Down") -> Match
        mock_library.items.side_effect = lambda q: [cand1] if q == "album:Voyager" else []
        item, mtype = _resolve_album_title_duplicate_candidate(mock_library, "Voyager (2019)", "Down", logger_instance=test_logger)
        self.assertIsNotNone(item)
        self.assertEqual(item.id, 1)
        self.assertEqual(mtype, "album+title (100%)")

        # Case 2: Same album ("Voyager") + Different title ("Amber" vs requested "Down") -> No match
        mock_library.items.side_effect = lambda q: [cand2] if q == "album:Voyager" else []
        item, mtype = _resolve_album_title_duplicate_candidate(mock_library, "Voyager (2019)", "Down", logger_instance=test_logger)
        self.assertIsNone(item)
        self.assertEqual(mtype, "")

        # Case 3: Different album ("From Chaos") + Same title ("Down") -> Query layer for "album:Voyager" returns []
        mock_library.items.side_effect = lambda q: [cand3] if q == "album:From Chaos" else []
        item, mtype = _resolve_album_title_duplicate_candidate(mock_library, "Voyager", "Down", logger_instance=test_logger)
        self.assertIsNone(item)
        self.assertEqual(mtype, "")

        # Case 4: Different album ("From Chaos") + Different title ("Amber" vs requested "Down") -> Query layer returns []
        mock_library.items.side_effect = lambda q: [cand4] if q == "album:From Chaos" else []
        item, mtype = _resolve_album_title_duplicate_candidate(mock_library, "Voyager", "Down", logger_instance=test_logger)
        self.assertIsNone(item)
        self.assertEqual(mtype, "")

        # Case 5: Multiple candidates for Voyager -> Only Voyager/Down matched when searching for title "Down"
        mock_library.items.side_effect = lambda q: [cand2, cand1] if q == "album:Voyager" else []
        item, mtype = _resolve_album_title_duplicate_candidate(mock_library, "Voyager", "Down", logger_instance=test_logger)
        self.assertEqual(item.id, 1)

        # Case 6: Candidate-level album verification protection
        # Scenario where fake library query layer unexpectedly returns a candidate from a different album
        mock_library.items.side_effect = None
        mock_library.items.return_value = [RemoteItem({"id": 99, "title": "Down", "album": "From Chaos"})]
        item, mtype = _resolve_album_title_duplicate_candidate(mock_library, "Voyager", "Down", logger_instance=test_logger)
        self.assertIsNone(item, "Candidate-level album verification must reject items with non-matching album names")

        # Case 7: Expected query failure (BeetsError) -> Warning logged, safe fallback (None, "")
        mock_library.items.side_effect = BeetsError("Control agent timeout")
        with mock.patch.object(test_logger, "warning") as mock_warn:
            item, mtype = _resolve_album_title_duplicate_candidate(mock_library, "Voyager", "Down", logger_instance=test_logger)
            self.assertIsNone(item)
            self.assertEqual(mtype, "")
            mock_warn.assert_called_once()

        # Case 8: Unexpected programming exception (RuntimeError) -> Error logged with stack trace, re-raised
        mock_library.items.side_effect = RuntimeError("Unexpected memory failure")
        with mock.patch.object(test_logger, "error") as mock_err:
            with self.assertRaises(RuntimeError):
                _resolve_album_title_duplicate_candidate(mock_library, "Voyager", "Down", logger_instance=test_logger)
            mock_err.assert_called_once()


class TestCommandNormalization(unittest.TestCase):
    def test_literal_executable(self):
        parsed = _parse_remote_beet_command(["beet", "version"])
        self.assertEqual(parsed.subcommand, "version")
        self.assertEqual(parsed.args, [])

    def test_linuxserver_executable(self):
        parsed = _parse_remote_beet_command(["/lsiopy/bin/beet", "version"])
        self.assertEqual(parsed.subcommand, "version")
        self.assertEqual(parsed.args, [])

    def test_other_posix_executable(self):
        parsed = _parse_remote_beet_command(["/usr/local/bin/beet", "ls", "artist:311"])
        self.assertEqual(parsed.subcommand, "ls")
        self.assertEqual(parsed.args, ["artist:311"])

    def test_windows_executable(self):
        parsed = _parse_remote_beet_command([r"C:\Python\Scripts\beet.exe", "version"])
        self.assertEqual(parsed.subcommand, "version")
        self.assertEqual(parsed.args, [])

    def test_config_argument_stripped(self):
        parsed = _parse_remote_beet_command(["/lsiopy/bin/beet", "-c", "/config/config.yaml", "embedart", "-y", "album_id:20"])
        self.assertEqual(parsed.subcommand, "embedart")
        self.assertEqual(parsed.args, ["-y", "album_id:20"])

    def test_quoted_string_command(self):
        parsed = _parse_remote_beet_command('beet ls "album:From Chaos"')
        self.assertEqual(parsed.subcommand, "ls")
        self.assertEqual(parsed.args, ["album:From Chaos"])

        parsed_import = _parse_remote_beet_command('beet import "/data/torrents/music/Artist Name/Album Name"')
        self.assertEqual(parsed_import.subcommand, "import")
        self.assertEqual(parsed_import.args, ["/data/torrents/music/Artist Name/Album Name"])

    def test_empty_command_raises(self):
        with self.assertRaises(BeetsCommandError):
            _parse_remote_beet_command([])
        with self.assertRaises(BeetsCommandError):
            _parse_remote_beet_command("")

    def test_executable_only_command_raises(self):
        with self.assertRaises(BeetsCommandError):
            _parse_remote_beet_command(["/lsiopy/bin/beet"])

    def test_path_like_invalid_subcommand_raises(self):
        with self.assertRaises(BeetsCommandError):
            _parse_remote_beet_command(["/lsiopy/bin/beet", "/bin/sh"])
        with self.assertRaises(BeetsCommandError):
            _parse_remote_beet_command(["beet", r"C:\script.py"])

    def test_shell_chaining_attempt_raises(self):
        with self.assertRaises(BeetsCommandError):
            _parse_remote_beet_command(["beet", "ls", ";", "rm", "-rf"])
        with self.assertRaises(BeetsCommandError):
            _parse_remote_beet_command("beet ls && rm -rf /")

    @mock.patch("backend.beets_client.beets_client.run_command")
    def test_beet_run_uses_central_parser(self, mock_run):
        mock_run.return_value = {"returncode": 0, "stdout": "ok", "stderr": ""}
        log = []
        res = _beet_run(["/lsiopy/bin/beet", "-c", "/config/config.yaml", "embedart", "-y", "album_id:20"], log)
        self.assertEqual(res.returncode, 0)
        mock_run.assert_called_once_with("embedart", args=["-y", "album_id:20"], timeout=120.0, config_override="")

    @mock.patch("backend.beets_client.beets_client.start_job")
    @mock.patch("backend.beets_client.beets_client.get_job")
    def test_job_run_uses_central_parser(self, mock_get, mock_start):
        mock_start.return_value = "job-123"
        mock_get.return_value = {"status": "success", "stdout": ["done"], "stderr": []}
        job = Job("j1", ["/lsiopy/bin/beet", "-c", "/config/config.yaml", "import", "-q", "/data/torrents/music"])
        import time
        time.sleep(0.1)
        mock_start.assert_called_once_with("import", args=["-q", "/data/torrents/music"], label="/lsiopy/bin/beet -c /config/config.yaml import -q /data/torrents/music", config_override="")

    def test_beet_locked_script_exists_and_uses_flock(self):
        locked_script = ROOT / "docker" / "beet-locked"
        self.assertTrue(locked_script.exists())
        content = locked_script.read_text()
        self.assertIn("flock /config/.beet_db.lock /lsiopy/bin/beet", content)

    @mock.patch("backend.beets_client.beets_client.run_command")
    def test_app_py_beet_bin_command_normalization_regression(self, mock_run):
        from app import BEET_BIN
        mock_run.return_value = {"returncode": 0, "stdout": "ok", "stderr": ""}
        log = []
        cmd = [BEET_BIN, "-c", "/config/config.yaml", "embedart", "-y", "album_id:20"]
        res = _beet_run(cmd, log)
        self.assertEqual(res.returncode, 0)
        mock_run.assert_called_once_with("embedart", args=["-y", "album_id:20"], timeout=120.0, config_override="")

    def test_submission_commands_in_allowlist(self):
        """Verify submit (acoustid) and mbsubmit (musicbrainz) commands are in ALLOWED_COMMANDS."""
        from backend.beets_control_agent import ALLOWED_COMMANDS
        self.assertIn("submit", ALLOWED_COMMANDS)
        self.assertIn("mbsubmit", ALLOWED_COMMANDS)


class TestJobCancellation(unittest.TestCase):
    @mock.patch("backend.beets_client.beets_client.cancel_job")
    @mock.patch("backend.beets_client.beets_client.start_job")
    def test_cancel_before_dispatch(self, mock_start, mock_cancel):
        """Cancelling before start_job dispatch prevents remote job creation."""
        job = Job.__new__(Job)
        job.job_id = "pre-cancel-job"
        job.command = ["/lsiopy/bin/beet", "import", "/data/torrents/music"]
        job.label = "test"
        job.created_at = time.time()
        job.started_at = None
        job.finished_at = None
        job.returncode = None
        job.log = []
        job._remote_job_id = None
        job._cancel_requested = True
        job._lock = threading.Lock()

        job._run()

        mock_start.assert_not_called()
        mock_cancel.assert_not_called()
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(job.returncode, 130)

    @mock.patch("backend.beets_client.beets_client.cancel_job")
    @mock.patch("backend.beets_client.beets_client.start_job")
    @mock.patch("backend.beets_client.beets_client.get_job")
    def test_cancel_while_start_job_in_flight(self, mock_get, mock_start, mock_cancel):
        """Cancelling while start_job is in flight cancels remote ID once assigned."""
        import threading, time
        start_event = threading.Event()
        release_start = threading.Event()

        def slow_start_job(subcommand, args=None, label="", config_override="", source_path=""):
            start_event.set()
            release_start.wait(timeout=2.0)
            return "remote-job-888"

        mock_start.side_effect = slow_start_job
        mock_get.return_value = {"status": "running", "stdout": [], "stderr": []}

        job = Job("in-flight-cancel", ["/lsiopy/bin/beet", "import", "/data/torrents/music"])
        self.assertTrue(start_event.wait(timeout=2.0))

        # Main thread issues kill() before start_job() returns
        job.kill()

        release_start.set()
        time.sleep(0.3)

        mock_cancel.assert_called_with("remote-job-888")
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(job.returncode, 130)

    @mock.patch("backend.beets_client.beets_client.cancel_job")
    @mock.patch("backend.beets_client.beets_client.start_job")
    @mock.patch("backend.beets_client.beets_client.get_job")
    def test_cancel_after_id_assignment_during_polling(self, mock_get, mock_start, mock_cancel):
        """Cancelling while polling reaches remote process and sets cancelled status."""
        mock_start.return_value = "remote-job-777"
        mock_get.return_value = {"status": "running", "stdout": ["working"], "stderr": []}

        job = Job("poll-cancel", ["/lsiopy/bin/beet", "import", "/data/torrents/music"])
        time.sleep(0.1)

        job.kill()
        time.sleep(0.3)

        mock_cancel.assert_called_with("remote-job-777")
        self.assertEqual(job.status, "cancelled")

    @mock.patch("backend.beets_client.beets_client.cancel_job")
    @mock.patch("backend.beets_client.beets_client.start_job")
    @mock.patch("backend.beets_client.beets_client.get_job")
    def test_remote_success_cannot_overwrite_cancelled_status(self, mock_get, mock_start, mock_cancel):
        """If cancellation was requested, remote success status does not overwrite cancelled state."""
        mock_start.return_value = "remote-job-666"
        mock_get.return_value = {"status": "running", "stdout": ["working"], "stderr": []}

        job = Job("success-race", ["/lsiopy/bin/beet", "import", "/data/torrents/music"])
        time.sleep(0.05)
        job.kill()
        mock_get.return_value = {"status": "success", "returncode": 0, "stdout": ["done"], "stderr": []}
        time.sleep(0.3)

        self.assertEqual(job.status, "cancelled")
        self.assertEqual(job.returncode, 130)

    @mock.patch("backend.beets_client.beets_client.cancel_job")
    @mock.patch("backend.beets_client.beets_client.start_job")
    def test_start_job_failure_handled(self, mock_start, mock_cancel):
        """Failure during start_job records error log and sets failed status."""
        mock_start.side_effect = RuntimeError("Control agent unreachable")

        job = Job("start-fail", ["/lsiopy/bin/beet", "import", "/data/torrents/music"])
        time.sleep(0.2)

        self.assertEqual(job.status, "failed")
        self.assertEqual(job.returncode, 1)
        self.assertIn("ERROR: Control agent unreachable", job.log)

    @mock.patch("backend.beets_client.beets_client.cancel_job")
    @mock.patch("backend.beets_client.beets_client.start_job")
    @mock.patch("backend.beets_client.beets_client.get_job")
    def test_repeated_kill_calls_idempotent(self, mock_get, mock_start, mock_cancel):
        """Repeated kill calls do not duplicate logs or crash."""
        mock_start.return_value = "remote-job-555"
        mock_get.return_value = {"status": "running", "stdout": [], "stderr": []}

        job = Job("repeat-kill", ["/lsiopy/bin/beet", "import", "/data/torrents/music"])
        time.sleep(0.1)

        job.kill()
        job.kill()
        job.kill()
        time.sleep(0.3)

        self.assertEqual(job.log.count("[killed]"), 1)
        self.assertEqual(job.status, "cancelled")

    @mock.patch("backend.beets_client.beets_client.cancel_job")
    @mock.patch("backend.beets_client.beets_client.start_job")
    @mock.patch("backend.beets_client.beets_client.get_job")
    def test_kill_after_completion_is_no_op(self, mock_get, mock_start, mock_cancel):
        """Calling kill after job completes does not alter completed state or add log lines."""
        mock_start.return_value = "remote-job-444"
        mock_get.return_value = {"status": "success", "returncode": 0, "stdout": ["done"], "stderr": []}

        job = Job("post-complete-kill", ["/lsiopy/bin/beet", "version"])
        time.sleep(0.3)

        self.assertEqual(job.status, "success")
        job.kill()
        self.assertEqual(job.status, "success")
        self.assertNotIn("[killed]", job.log)


if __name__ == "__main__":
    unittest.main()
