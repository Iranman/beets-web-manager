"""Unit and integration tests for the external Beets control agent architecture."""

import importlib.util
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
    beets_client,
)
from backend.beets_control_agent import (
    ALLOWED_COMMANDS,
    JOBS,
    ControlAgentHandler,
    _handle_delete_album,
    is_safe_path,
    resolve_safe_path,
    require_command_capability,
)
from job_engine import Job, JobStore, _beet_run, _parse_remote_beet_command
from routes_submissions import _submission_readiness

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
        if os.name != "posix":
            self.skipTest("Path-root semantics require a POSIX filesystem")
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
                with self.assertRaises(UnsafePathError):
                    resolve_safe_path(str(symlink), ["music"])

    def test_resolve_safe_path_returns_canonical_symlink_target(self):
        """Filesystem sinks must operate on the resolved (symlink-followed)
        path resolve_safe_path() returns, not the original caller-supplied
        string, or a check-then-use gap re-opens between validation and use.
        A within-root symlink must resolve to its real, allowed target."""
        if os.name != "posix":
            # resolve_safe_path()/is_safe_path() require a POSIX-style
            # absolute path (matching the real Linux container runtime this
            # code actually ships in); a Windows temp-dir path never starts
            # with "/", so this can't be meaningfully exercised here.
            self.skipTest("Path-root semantics require a POSIX filesystem")
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            music_root = tmp_path / "music_real"
            music_root.mkdir()
            real_file = music_root / "track.flac"
            real_file.write_bytes(b"audio")

            alias_dir = music_root / "alias"
            try:
                os.symlink(music_root, alias_dir)
            except (OSError, NotImplementedError):
                self.skipTest("Symlink creation not supported on this platform/user")

            with mock.patch("backend.beets_control_agent.MUSIC_LIBRARY_PATH", str(music_root)):
                resolved = resolve_safe_path(str(alias_dir / "track.flac"), ["music"])
                self.assertIsNotNone(resolved)
                self.assertEqual(Path(resolved), real_file.resolve())
                # is_safe_path() must agree with resolve_safe_path() on the
                # same input (boolean wrapper stays consistent with the
                # canonical-path-returning implementation it wraps).
                self.assertTrue(is_safe_path(str(alias_dir / "track.flac"), ["music"]))

    def test_auth_token_fail_closed(self):
        with mock.patch("backend.beets_control_agent.BEETS_API_TOKEN", ""):
            handler = mock.MagicMock(spec=ControlAgentHandler)
            handler.headers = {}
            result = ControlAgentHandler._authenticate(handler)
            self.assertFalse(result)

    def test_raw_query_security_handler_execution(self):
        """Test raw SQL handler execution is disabled fail-closed."""
        handler = ControlAgentHandler.__new__(ControlAgentHandler)
        handler.headers = {"X-Beets-API-Token": "testtoken"}

        sent_responses = []
        def _send_json(code, data):
            sent_responses.append((code, data))

        handler._send_json = _send_json
        handler._authenticate = lambda: True

        forbidden_queries = [
            "SELECT * FROM items",
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
            self.assertEqual(code, 403, f"Query '{q}' should have been rejected with 403, got {code}")
            self.assertEqual(data.get("error"), "Raw SQL queries are not permitted")

    def test_raw_query_rejects_oversized_query_before_processing(self):
        """Raw SQL is retired, so oversized SQL is denied before parsing."""
        handler = ControlAgentHandler.__new__(ControlAgentHandler)
        handler.headers = {"X-Beets-API-Token": "testtoken"}

        sent_responses = []
        def _send_json(code, data):
            sent_responses.append((code, data))

        handler._send_json = _send_json
        handler._authenticate = lambda: True

        oversized_query = "SELECT * FROM items WHERE id IN (" + ",".join(str(n) for n in range(5000)) + ")"
        self.assertGreater(len(oversized_query), 10_000)

        handler.path = "/library/raw_query"
        body = json.dumps({"query": oversized_query}).encode("utf-8")
        handler.rfile = BytesIO(body)
        handler.headers["Content-Length"] = str(len(body))
        handler.do_POST()

        self.assertEqual(len(sent_responses), 1)
        code, data = sent_responses[0]
        self.assertEqual(code, 403)
        self.assertEqual(data["error"], "Raw SQL queries are not permitted")

    def test_raw_query_within_length_limit_is_rejected_fail_closed(self):
        handler = ControlAgentHandler.__new__(ControlAgentHandler)
        handler.headers = {"X-Beets-API-Token": "testtoken"}

        sent_responses = []
        def _send_json(code, data):
            sent_responses.append((code, data))

        handler._send_json = _send_json
        handler._authenticate = lambda: True

        handler.path = "/library/raw_query"
        body = json.dumps({"query": "SELECT * FROM items"}).encode("utf-8")
        handler.rfile = BytesIO(body)
        handler.headers["Content-Length"] = str(len(body))
        with mock.patch("backend.beets_control_agent.os.path.exists", return_value=False):
            handler.do_POST()

        code, data = sent_responses[0]
        self.assertEqual(code, 403)
        self.assertEqual(data["error"], "Raw SQL queries are not permitted")
        self.assertNotIn("too long", data.get("error", ""))

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

    def test_engine_image_disables_inherited_beet_web_service(self):
        """The engine image must run the control agent, not the LSIO beet web service."""
        content = (ROOT / "Dockerfile.beets").read_text(encoding="utf-8")

        self.assertIn("/etc/s6-overlay/s6-rc.d/user/contents.d/svc-beets", content)
        self.assertIn("rm -f /etc/s6-overlay/s6-rc.d/user/contents.d/svc-beets", content)
        self.assertNotIn("beet web", content)

    def test_public_api_health_is_liveness_only(self):
        import app as app_module

        with mock.patch.object(app_module, "_health_checks", side_effect=AssertionError("dependency probe used")):
            response = app_module.app.test_client().get("/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True})

    def test_legacy_local_scan_loop_is_opt_in(self):
        content = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn("BEETS_ENABLE_LEGACY_LOCAL_SCAN", content)
        self.assertIn("/web-manager-data/last_scan.txt", content)
        self.assertIn("if _legacy_local_scan_enabled():", content)
        self.assertIn("Legacy local library scan is disabled", content)


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

    def test_beets_client_malformed_json_raises_sanitized_unavailable_error(self):
        client = BeetsClient(base_url="http://127.0.0.1:9999", token="test")
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = mock.MagicMock()
            mock_response.read.return_value = b"{not-valid-json token=secret}"
            mock_urlopen.return_value.__enter__.return_value = mock_response
            with self.assertRaises(BeetsUnavailableError) as cm:
                client.get_status()
        self.assertIn("malformed JSON", str(cm.exception))
        self.assertNotIn("secret", str(cm.exception))

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
                 mock.patch("backend.beets_control_agent.resolve_safe_path", side_effect=lambda p, *a, **kw: Path(p) if isinstance(p, str) else p), \
                 mock.patch.object(Path, "unlink", side_effect=PermissionError("Permission denied")):
                code, res = _handle_delete_album(1, delete_files=True)
                self.assertEqual(code, 200)
                self.assertFalse(res["success"])
                self.assertEqual(res["status"], "partial_failure")
                self.assertTrue(res["database_deleted"])
                self.assertEqual(res["files_deleted"], 0)
                self.assertEqual(res["files_failed"], 1)
                self.assertTrue(len(res["file_errors"]) > 0)

    def test_docker_web_manager_image_has_no_beets_binary(self):
        """Test web-manager container image has no beet executable (skipped if
        Docker or the image is unavailable on this host).

        Known gap: .github/workflows/unit-tests.yml (the job this test runs
        in during CI) never builds any web-manager image -- only
        docker-build.yml does, tagged `beets-web-manager:ci`, in a separate
        job that does not run this Python test suite. So in CI this check
        currently always skips; it only runs for real on a host (local dev,
        or a future combined workflow) where one of these tags was already
        built. Checking both tags at least makes it capable of running for
        real wherever either image happens to be present, rather than being
        permanently tied to a tag CI never produces.
        """
        try:
            res = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=5)
            if res.returncode != 0:
                raise unittest.SkipTest("Docker daemon unavailable on this host")
        except Exception:
            raise unittest.SkipTest("Docker CLI unavailable on this host")

        candidate_tags = ["beets-web-manager:0.1.0", "beets-web-manager:ci"]
        image_tag = None
        for tag in candidate_tags:
            img_inspect = subprocess.run(["docker", "image", "inspect", tag], capture_output=True, text=True, timeout=5)
            if img_inspect.returncode == 0:
                image_tag = tag
                break
        if image_tag is None:
            raise unittest.SkipTest(f"None of {candidate_tags} built locally on this host")

        res = subprocess.run(
            ["docker", "run", "--rm", image_tag, "command", "-v", "beet"],
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
    """Regression tests for thread-safe remote job cancellation state machine."""

    @mock.patch("backend.beets_client.beets_client.start_job")
    @mock.patch("backend.beets_client.beets_client.cancel_job")
    def test_cancel_before_dispatch(self, mock_cancel, mock_start):
        """If cancellation is requested before dispatch, start_job is never called."""
        job = Job.__new__(Job)
        job.job_id = "pre-dispatch"
        job.command = ["/lsiopy/bin/beet", "import", "/data/torrents"]
        job.label = "pre-dispatch"
        job.created_at = time.time()
        job.started_at = None
        job.finished_at = None
        job.returncode = None
        job.log = []
        job._remote_job_id = None
        job._cancel_requested = True
        job._cancel_failed = False
        job._state = "created"
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
        """If cancellation is requested while start_job is in flight, cancel_job is called once ID arrives."""
        start_entered = threading.Event()
        start_proceed = threading.Event()

        def slow_start(*args, **kwargs):
            start_entered.set()
            start_proceed.wait(timeout=2.0)
            return "remote-job-999"

        mock_start.side_effect = slow_start
        mock_get.return_value = {"status": "cancelled", "returncode": 130, "stdout": ["cancelled"], "stderr": []}

        job = Job("in-flight", ["/lsiopy/bin/beet", "import", "/data/torrents"])

        self.assertTrue(start_entered.wait(timeout=2.0))
        job.kill()
        start_proceed.set()

        time.sleep(0.4)
        mock_cancel.assert_called_with("remote-job-999")
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(job.returncode, 130)

    @mock.patch("backend.beets_client.beets_client.cancel_job")
    @mock.patch("backend.beets_client.beets_client.start_job")
    @mock.patch("backend.beets_client.beets_client.get_job")
    def test_cancel_after_id_assignment_during_polling(self, mock_get, mock_start, mock_cancel):
        """Cancelling during polling triggers remote cancel_job and waits for confirmed remote status."""
        mock_start.return_value = "remote-job-123"
        mock_get.return_value = {"status": "running", "stdout": ["importing..."], "stderr": []}

        job = Job("poll-cancel", ["/lsiopy/bin/beet", "import", "/data/torrents"])
        time.sleep(0.1)

        mock_get.return_value = {"status": "cancelled", "returncode": 130, "stdout": ["killed"], "stderr": []}
        job.kill()
        time.sleep(0.4)

        mock_cancel.assert_called_with("remote-job-123")
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(job.returncode, 130)

    @mock.patch("backend.beets_client.beets_client.cancel_job")
    @mock.patch("backend.beets_client.beets_client.start_job")
    @mock.patch("backend.beets_client.beets_client.get_job")
    def test_remote_success_before_cancel_accepted(self, mock_get, mock_start, mock_cancel):
        """If remote completed with success before cancel was accepted, local status becomes success with log warning."""
        mock_start.return_value = "remote-job-777"
        mock_get.return_value = {"status": "running", "stdout": ["working"], "stderr": []}

        job = Job("success-first", ["/lsiopy/bin/beet", "import", "/data/torrents"])
        time.sleep(0.05)
        job.kill()
        mock_get.return_value = {"status": "success", "returncode": 0, "stdout": ["imported!"], "stderr": []}
        time.sleep(0.3)

        self.assertEqual(job.status, "success")
        self.assertEqual(job.returncode, 0)
        self.assertTrue(any("already completed" in line for line in job.log))

    @mock.patch("backend.beets_client.beets_client.cancel_job")
    @mock.patch("backend.beets_client.beets_client.start_job")
    @mock.patch("backend.beets_client.beets_client.get_job")
    def test_cancel_api_exception_does_not_report_cancelled(self, mock_get, mock_start, mock_cancel):
        """If cancel_job API raises an error and remote state is unconfirmed, status does not become cancelled."""
        mock_start.return_value = "remote-job-fail-cancel"
        mock_get.return_value = {"status": "running", "stdout": ["working"], "stderr": []}

        job = Job("fail-cancel-api", ["/lsiopy/bin/beet", "import", "/data/torrents"])
        time.sleep(0.05)
        mock_cancel.side_effect = BeetsUnavailableError("Network timeout on cancel")
        mock_get.side_effect = BeetsUnavailableError("Network timeout on status")
        job.kill()
        time.sleep(0.4)

        self.assertNotEqual(job.status, "cancelled")
        self.assertEqual(job.status, "cancel_failed")
        self.assertEqual(job.returncode, 1)

    @mock.patch("backend.beets_client.beets_client.cancel_job")
    @mock.patch("backend.beets_client.beets_client.start_job")
    @mock.patch("backend.beets_client.beets_client.get_job")
    def test_repeated_kill_calls_idempotent(self, mock_get, mock_start, mock_cancel):
        """Repeated kill calls on active job append [killed] at most once."""
        mock_start.return_value = "remote-job-idem"
        mock_get.return_value = {"status": "running", "stdout": [], "stderr": []}

        job = Job("idempotent-kill", ["/lsiopy/bin/beet", "import", "/data/torrents"])
        time.sleep(0.05)
        job.kill()
        job.kill()
        job.kill()
        mock_get.return_value = {"status": "cancelled", "returncode": 130, "stdout": [], "stderr": []}
        time.sleep(0.3)

        self.assertEqual(job.log.count("[killed]"), 1)
        self.assertEqual(job.status, "cancelled")

    @mock.patch("backend.beets_client.beets_client.cancel_job")
    @mock.patch("backend.beets_client.beets_client.start_job")
    @mock.patch("backend.beets_client.beets_client.get_job")
    def test_kill_after_completion_is_no_op(self, mock_get, mock_start, mock_cancel):
        """Calling kill on completed job is a no-op."""
        mock_start.return_value = "remote-job-done"
        mock_get.return_value = {"status": "success", "returncode": 0, "stdout": ["done"], "stderr": []}

        job = Job("completed-kill", ["/lsiopy/bin/beet", "import", "/data/torrents"])
        time.sleep(0.3)
        self.assertEqual(job.status, "success")

        job.kill()
        mock_cancel.assert_not_called()
        self.assertEqual(job.status, "success")
        self.assertNotIn("[killed]", job.log)

    @mock.patch("backend.beets_client.beets_client.cancel_job")
    @mock.patch("backend.beets_client.beets_client.start_job")
    @mock.patch("backend.beets_client.beets_client.get_job")
    def test_cancel_confirmation_timeout_becomes_cancel_failed(self, mock_get, mock_start, mock_cancel):
        """If the remote agent stays reachable but never reports a terminal status,
        the bounded confirmation deadline must eventually give up rather than wait forever."""
        mock_start.return_value = "remote-job-stuck"
        mock_get.return_value = {"status": "running", "stdout": [], "stderr": []}

        job = Job("stuck-cancel", ["/lsiopy/bin/beet", "import", "/data/torrents"], cancel_confirm_timeout=0.3)
        time.sleep(0.1)
        job.kill()
        time.sleep(0.8)

        self.assertEqual(job.status, "cancel_failed")
        self.assertNotEqual(job.status, "cancelled")
        self.assertNotEqual(job.returncode, 0)
        self.assertTrue(any("not confirmed" in line for line in job.log))
        self.assertEqual(
            sum(1 for line in job.log if "not confirmed" in line), 1,
            "confirmation-timeout diagnostic must not be logged more than once",
        )

    @mock.patch("backend.beets_client.beets_client.cancel_job")
    @mock.patch("backend.beets_client.beets_client.start_job")
    @mock.patch("backend.beets_client.beets_client.get_job")
    def test_remote_success_just_before_cancel_timeout_is_preserved(self, mock_get, mock_start, mock_cancel):
        """A real success reported before the confirmation deadline expires must win over the timeout."""
        call_state = {"n": 0}

        def get_job_side_effect(job_id):
            call_state["n"] += 1
            if call_state["n"] < 2:
                return {"status": "running", "stdout": [], "stderr": []}
            return {"status": "success", "returncode": 0, "stdout": ["done"], "stderr": []}

        mock_start.return_value = "remote-job-late-success"
        mock_get.side_effect = get_job_side_effect

        job = Job("late-success", ["/lsiopy/bin/beet", "import", "/data/torrents"], cancel_confirm_timeout=5.0)
        time.sleep(0.05)
        job.kill()
        time.sleep(0.6)

        self.assertEqual(job.status, "success")
        self.assertEqual(job.returncode, 0)

    @mock.patch("backend.beets_client.beets_client.cancel_job")
    @mock.patch("backend.beets_client.beets_client.start_job")
    @mock.patch("backend.beets_client.beets_client.get_job")
    def test_remote_failed_just_before_cancel_timeout_is_preserved(self, mock_get, mock_start, mock_cancel):
        """A real failure reported before the confirmation deadline expires must win over the timeout."""
        call_state = {"n": 0}

        def get_job_side_effect(job_id):
            call_state["n"] += 1
            if call_state["n"] < 2:
                return {"status": "running", "stdout": [], "stderr": []}
            return {"status": "failed", "returncode": 2, "stdout": [], "stderr": ["boom"]}

        mock_start.return_value = "remote-job-late-fail"
        mock_get.side_effect = get_job_side_effect

        job = Job("late-fail", ["/lsiopy/bin/beet", "import", "/data/torrents"], cancel_confirm_timeout=5.0)
        time.sleep(0.05)
        job.kill()
        time.sleep(0.6)

        self.assertEqual(job.status, "failed")
        self.assertEqual(job.returncode, 2)

    @mock.patch("backend.beets_client.beets_client.cancel_job")
    @mock.patch("backend.beets_client.beets_client.start_job")
    @mock.patch("backend.beets_client.beets_client.get_job")
    def test_remote_cancelled_just_before_cancel_timeout_is_preserved(self, mock_get, mock_start, mock_cancel):
        """A genuine remote cancellation confirmation before the deadline expires must be reported as cancelled."""
        call_state = {"n": 0}

        def get_job_side_effect(job_id):
            call_state["n"] += 1
            if call_state["n"] < 2:
                return {"status": "running", "stdout": [], "stderr": []}
            return {"status": "cancelled", "returncode": 130, "stdout": [], "stderr": []}

        mock_start.return_value = "remote-job-late-cancel"
        mock_get.side_effect = get_job_side_effect

        job = Job("late-cancel", ["/lsiopy/bin/beet", "import", "/data/torrents"], cancel_confirm_timeout=5.0)
        time.sleep(0.05)
        job.kill()
        time.sleep(0.6)

        self.assertEqual(job.status, "cancelled")
        self.assertEqual(job.returncode, 130)

    @mock.patch("backend.beets_client.beets_client.cancel_job")
    @mock.patch("backend.beets_client.beets_client.start_job")
    @mock.patch("backend.beets_client.beets_client.get_job")
    def test_cancel_job_negative_response_does_not_report_cancelled(self, mock_get, mock_start, mock_cancel):
        """cancel_job() repeatedly raising (a rejected/negative response) must not be
        treated as a confirmed cancellation, and must resolve via the bounded timeout."""
        mock_start.return_value = "remote-job-reject-cancel"
        mock_get.return_value = {"status": "running", "stdout": [], "stderr": []}
        mock_cancel.side_effect = BeetsError("cancellation rejected by agent")

        job = Job("reject-cancel", ["/lsiopy/bin/beet", "import", "/data/torrents"], cancel_confirm_timeout=0.3)
        time.sleep(0.1)
        job.kill()
        time.sleep(0.8)

        self.assertNotEqual(job.status, "cancelled")
        self.assertEqual(job.status, "cancel_failed")


class TestAcoustIDChromaCapability(unittest.TestCase):
    """Tests for AcoustID submit and Chroma plugin capability detection."""

    @mock.patch("routes_submissions._read_beets_plugin_list", return_value=["chroma", "mbsubmit"])
    @mock.patch("backend.beets_client.beets_client.get_status")
    def test_submission_readiness_queries_remote_capabilities(self, mock_status, mock_plugins):
        """_submission_readiness includes chroma, fpcalc, and pyacoustid from remote Beets agent."""
        mock_status.return_value = {
            "status": "ok",
            "fpcalc_available": True,
            "fpcalc_path": "/usr/bin/fpcalc",
            "pyacoustid_available": True,
            "plugins": {
                "discpath": True,
                "chroma": True,
                "mbsubmit": True,
                "musicbrainz": True,
            }
        }

        from routes_submissions import _submission_readiness
        readiness = _submission_readiness()
        self.assertTrue(readiness["plugins"]["chroma"])
        self.assertTrue(readiness["plugins"]["mbsubmit"])
        self.assertTrue(readiness["fpcalc_available"])
        self.assertTrue(readiness["pyacoustid_available"])
        self.assertTrue(readiness["remote_reachable"])

    @mock.patch("backend.beets_client.beets_client.get_status")
    def test_submission_readiness_fails_closed_on_connection_failure(self, mock_status):
        """A remote connection failure must report every capability unavailable,
        never fall back to a local find_spec()/shutil.which() guess."""
        mock_status.side_effect = BeetsUnavailableError("Beets Control Agent is unavailable at 10.0.0.5:8338")

        from routes_submissions import _submission_readiness
        readiness = _submission_readiness()
        self.assertFalse(readiness["remote_reachable"])
        self.assertFalse(readiness["plugins"]["chroma"])
        self.assertFalse(readiness["plugins"]["mbsubmit"])
        self.assertFalse(readiness["fpcalc_available"])
        self.assertFalse(readiness["pyacoustid_available"])
        self.assertEqual(readiness["reason"], "Beets control agent status is unavailable")
        self.assertNotIn("10.0.0.5", readiness["reason"])
        self.assertNotIn("Beets Control Agent is unavailable at", readiness["reason"])
        self.assertNotIn("Beets Control Agent is unavailable", readiness["reason"])
    @mock.patch("backend.beets_client.beets_client.get_status")
    def test_submission_readiness_fails_closed_on_authentication_failure(self, mock_status):
        """A remote authentication failure must also fail closed, not silently
        report a stale/local capability guess."""
        mock_status.side_effect = BeetsAuthError("401 Unauthorized")

        from routes_submissions import _submission_readiness
        readiness = _submission_readiness()
        self.assertFalse(readiness["remote_reachable"])
        self.assertFalse(readiness["plugins"]["chroma"])
        self.assertFalse(readiness["plugins"]["mbsubmit"])

    @mock.patch("backend.beets_client.beets_client.get_status")
    def test_submission_readiness_fails_closed_on_malformed_response(self, mock_status):
        """A response missing the expected "status": "ok" shape must fail closed
        rather than partially trusting whatever fields happen to be present."""
        mock_status.return_value = {"unexpected": "shape"}

        from routes_submissions import _submission_readiness
        readiness = _submission_readiness()
        self.assertFalse(readiness["remote_reachable"])
        self.assertFalse(readiness["plugins"]["chroma"])

    @mock.patch("backend.beets_client.beets_client.get_status")
    def test_submission_readiness_reports_chroma_unavailable_when_agent_says_so(self, mock_status):
        """When the agent reaches a genuine 'ok' status but reports chroma as not
        loaded (e.g. importable-but-failed-to-load), readiness must propagate that,
        not silently flip it to available."""
        mock_status.return_value = {
            "status": "ok",
            "fpcalc_available": True,
            "pyacoustid_available": True,
            "plugins": {"chroma": False, "mbsubmit": True, "musicbrainz": True},
        }

        from routes_submissions import _submission_readiness
        readiness = _submission_readiness()
        self.assertTrue(readiness["remote_reachable"])
        self.assertFalse(readiness["plugins"]["chroma"])
        self.assertTrue(readiness["plugins"]["mbsubmit"])

    @mock.patch("backend.beets_client.beets_client.get_status")
    def test_submission_readiness_reports_fpcalc_and_pyacoustid_unavailable(self, mock_status):
        mock_status.return_value = {
            "status": "ok",
            "fpcalc_available": False,
            "pyacoustid_available": False,
            "plugins": {"chroma": False, "mbsubmit": True, "musicbrainz": True},
        }

        from routes_submissions import _submission_readiness
        readiness = _submission_readiness()
        self.assertFalse(readiness["fpcalc_available"])
        self.assertFalse(readiness["pyacoustid_available"])

    @mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
    @mock.patch("routes_submissions._submission_readiness")
    @mock.patch("routes_submissions.lib.get_album")
    def test_acoustid_submit_blocked_when_chroma_missing(self, mock_get_album, mock_readiness):
        """album_acoustid_submit returns 400 error when chroma plugin is missing."""
        from app import app
        mock_get_album.return_value = mock.MagicMock(id=42)
        mock_readiness.return_value = {
            "plugins": {"chroma": False, "mbsubmit": True},
            "fpcalc_available": True,
            "pyacoustid_available": True,
        }

        with app.test_client() as client:
            resp = client.post("/api/albums/42/acoustid-submit")
            self.assertEqual(resp.status_code, 400)
            data = resp.get_json()
            self.assertFalse(data["ok"])
            self.assertIn("chroma plugin is not enabled", data["error"])

    @mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
    @mock.patch("routes_submissions._submission_readiness")
    @mock.patch("routes_submissions.lib.get_album")
    def test_acoustid_submit_blocked_when_fpcalc_missing(self, mock_get_album, mock_readiness):
        """album_acoustid_submit returns 400 error when fpcalc binary is missing."""
        from app import app
        mock_get_album.return_value = mock.MagicMock(id=42)
        mock_readiness.return_value = {
            "plugins": {"chroma": True, "mbsubmit": True},
            "fpcalc_available": False,
            "pyacoustid_available": True,
        }

        with app.test_client() as client:
            resp = client.post("/api/albums/42/acoustid-submit")
            self.assertEqual(resp.status_code, 400)
            data = resp.get_json()
            self.assertFalse(data["ok"])
            self.assertIn("fpcalc", data["error"])

    def test_mbsubmit_remains_available_independently(self):
        """mbsubmit command is allowed independently of chroma/fpcalc in ALLOWED_COMMANDS."""
        from backend.beets_control_agent import ALLOWED_COMMANDS
        self.assertIn("mbsubmit", ALLOWED_COMMANDS)
        self.assertIn("submit", ALLOWED_COMMANDS)

    def _run_status_with_beet_version_stdout(self, stdout: str) -> dict:
        handler = ControlAgentHandler.__new__(ControlAgentHandler)
        handler.headers = {"Authorization": "Bearer test-token"}
        handler._authenticate = lambda: True
        handler.path = "/status"
        responses = []
        handler._send_json = lambda code, data: responses.append((code, data))

        fake_result = mock.MagicMock(stdout=stdout, stderr="", returncode=0)
        with mock.patch("backend.beets_control_agent.subprocess.run", return_value=fake_result):
            handler.do_GET()

        self.assertEqual(len(responses), 1)
        code, data = responses[0]
        self.assertEqual(code, 200)
        return data

    def test_status_reports_chroma_unavailable_when_importable_but_not_loaded(self):
        """A plugin that is importable but absent from beet version's loaded-plugins line
        must be reported unavailable, not merely because its module can be imported."""
        stdout = (
            "beets version 2.4.0\n"
            "Python version 3.12.11\n"
            "plugins: convert, deezer, discpath, embedart, fetchart, lastgenre, "
            "mbsubmit, mbsync, musicbrainz, scrub\n"
        )
        data = self._run_status_with_beet_version_stdout(stdout)
        self.assertFalse(data["plugins"]["chroma"], "chroma is not in the loaded-plugins line and must report unavailable")
        self.assertFalse(data["capabilities"]["fingerprinting"]["available"])
        self.assertFalse(data["capabilities"]["fingerprinting"]["chroma_loaded"])
        self.assertTrue(data["plugins"]["mbsubmit"])
        self.assertTrue(data["plugins"]["musicbrainz"])

    def test_status_reports_chroma_available_when_actually_loaded(self):
        """When chroma genuinely appears in beet version's loaded-plugins line, report it available."""
        stdout = (
            "beets version 2.4.0\n"
            "Python version 3.12.11\n"
            "plugins: chroma, convert, deezer, discpath, embedart, fetchart, lastgenre, "
            "mbsubmit, mbsync, musicbrainz, scrub\n"
        )
        data = self._run_status_with_beet_version_stdout(stdout)
        self.assertTrue(data["plugins"]["chroma"])
        self.assertTrue(data["plugins"]["mbsubmit"])


class TestControlAgentCapabilityEnforcement(unittest.TestCase):
    """Real handler tests proving the control agent itself blocks capability-gated
    commands, independent of whatever the web-manager route already checks."""

    def _post(self, path, body, headers=None):
        handler = ControlAgentHandler.__new__(ControlAgentHandler)
        handler.headers = dict(headers) if headers is not None else {"Authorization": "Bearer test-token"}
        handler._authenticate = lambda: True
        handler.path = path
        payload = json.dumps(body).encode("utf-8")
        handler.rfile = BytesIO(payload)
        handler.headers["Content-Length"] = str(len(payload))
        responses = []
        handler._send_json = lambda code, data: responses.append((code, data))
        handler.do_POST()
        self.assertEqual(len(responses), 1, f"expected exactly one response for {path}")
        return responses[0]

    def test_commands_execute_accepts_submit_when_available(self):
        with mock.patch("backend.beets_control_agent.get_loaded_beet_plugins", return_value={"chroma"}), \
             mock.patch("backend.beets_control_agent.acquire_os_lock", return_value=None), \
             mock.patch("backend.beets_control_agent.release_os_lock"), \
             mock.patch("backend.beets_control_agent.subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="usage", stderr="")
            code, data = self._post("/commands/execute", {"command": "submit", "args": ["--help"]})
            self.assertEqual(code, 200)
            mock_run.assert_called_once()

    def test_commands_execute_rejects_submit_when_unavailable(self):
        with mock.patch("backend.beets_control_agent.get_loaded_beet_plugins", return_value=set()), \
             mock.patch("backend.beets_control_agent.subprocess.run") as mock_run:
            code, data = self._post("/commands/execute", {"command": "submit", "args": ["--help"]})
            self.assertEqual(code, 409)
            self.assertIn("reason", data)
            mock_run.assert_not_called()

    def test_jobs_create_accepts_submit_when_available(self):
        fake_proc = mock.MagicMock()
        fake_proc.communicate.return_value = ("usage", "")
        fake_proc.returncode = 0
        with mock.patch("backend.beets_control_agent.get_loaded_beet_plugins", return_value={"chroma"}), \
             mock.patch("backend.beets_control_agent.acquire_os_lock", return_value=None), \
             mock.patch("backend.beets_control_agent.release_os_lock"), \
             mock.patch("backend.beets_control_agent.subprocess.Popen", return_value=fake_proc):
            before_ids = set(JOBS)
            code, data = self._post("/jobs/create", {"command": "submit", "args": ["--help"]})
            try:
                self.assertEqual(code, 200)
                self.assertIn("job_id", data)
                self.assertEqual(set(JOBS) - before_ids, {data["job_id"]})
            finally:
                for jid in list(JOBS):
                    if jid not in before_ids:
                        del JOBS[jid]

    def test_jobs_create_rejects_submit_when_unavailable(self):
        with mock.patch("backend.beets_control_agent.get_loaded_beet_plugins", return_value=set()):
            before_ids = set(JOBS)
            code, data = self._post("/jobs/create", {"command": "submit", "args": ["--help"]})
            self.assertEqual(code, 409)
            self.assertIn("reason", data)
            self.assertEqual(set(JOBS), before_ids, "no job object should be created when capability is unavailable")

    def test_mbsubmit_independently_accepted_when_available(self):
        with mock.patch("backend.beets_control_agent.get_loaded_beet_plugins", return_value={"mbsubmit"}), \
             mock.patch("backend.beets_control_agent.acquire_os_lock", return_value=None), \
             mock.patch("backend.beets_control_agent.release_os_lock"), \
             mock.patch("backend.beets_control_agent.subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="usage", stderr="")
            code, data = self._post("/commands/execute", {"command": "mbsubmit", "args": ["--help"]})
            self.assertEqual(code, 200)

    def test_mbsubmit_independently_rejected_when_unavailable(self):
        with mock.patch("backend.beets_control_agent.get_loaded_beet_plugins", return_value={"chroma"}), \
             mock.patch("backend.beets_control_agent.subprocess.run") as mock_run:
            code, data = self._post("/commands/execute", {"command": "mbsubmit", "args": ["--help"]})
            self.assertEqual(code, 409)
            mock_run.assert_not_called()

    def test_missing_authentication_returns_401(self):
        handler = ControlAgentHandler.__new__(ControlAgentHandler)
        handler.headers = {}
        handler.path = "/commands/execute"
        payload = json.dumps({"command": "submit", "args": []}).encode("utf-8")
        handler.rfile = BytesIO(payload)
        handler.headers["Content-Length"] = str(len(payload))
        responses = []
        handler._send_json = lambda code, data: responses.append((code, data))
        with mock.patch("backend.beets_control_agent.BEETS_API_TOKEN", "real-token-that-is-long-enough-1234567890"):
            handler.do_POST()
        self.assertEqual(responses[0][0], 401)

    def test_invalid_authentication_returns_401(self):
        handler = ControlAgentHandler.__new__(ControlAgentHandler)
        handler.headers = {"Authorization": "Bearer wrong-token"}
        handler.path = "/commands/execute"
        payload = json.dumps({"command": "submit", "args": []}).encode("utf-8")
        handler.rfile = BytesIO(payload)
        handler.headers["Content-Length"] = str(len(payload))
        responses = []
        handler._send_json = lambda code, data: responses.append((code, data))
        with mock.patch("backend.beets_control_agent.BEETS_API_TOKEN", "real-token-that-is-long-enough-1234567890"):
            handler.do_POST()
        self.assertEqual(responses[0][0], 401)

    def test_unknown_command_still_returns_allowlist_error(self):
        with mock.patch("backend.beets_control_agent.get_loaded_beet_plugins", return_value={"chroma", "mbsubmit"}):
            code, data = self._post("/commands/execute", {"command": "disallowed_plugin_xyz", "args": []})
            self.assertEqual(code, 400)
            self.assertIn("not in the allowlist", data["error"])

    def test_capability_failure_returns_agent_provided_reason(self):
        with mock.patch("backend.beets_control_agent.get_loaded_beet_plugins", return_value=set()):
            code, data = self._post("/commands/execute", {"command": "submit", "args": []})
            self.assertEqual(code, 409)
            self.assertEqual(data["reason"], "Chroma is configured but the submit command is not registered")

    def test_real_capability_probe_rejects_when_no_plugins_loaded(self):
        """Exercises the real get_loaded_beet_plugins()/require_command_capability()
        probe (not a mock of the capability dict) against a stubbed `beet version`."""
        fake_version = mock.MagicMock(
            stdout="beets version 2.4.0\nPython version 3.12.11\nplugins: discpath\n",
            returncode=0,
        )
        with mock.patch("backend.beets_control_agent.subprocess.run", return_value=fake_version):
            error = require_command_capability("submit")
            self.assertIsNotNone(error)
            self.assertIn("reason", error)
            error2 = require_command_capability("mbsubmit")
            self.assertIsNotNone(error2)

    def test_real_capability_probe_accepts_when_plugin_loaded(self):
        fake_version = mock.MagicMock(
            stdout="beets version 2.4.0\nPython version 3.12.11\nplugins: chroma, discpath, mbsubmit\n",
            returncode=0,
        )
        with mock.patch("backend.beets_control_agent.subprocess.run", return_value=fake_version):
            self.assertIsNone(require_command_capability("submit"))
            self.assertIsNone(require_command_capability("mbsubmit"))


_BEETS_AVAILABLE = importlib.util.find_spec("beets") is not None


@unittest.skipUnless(_BEETS_AVAILABLE, "beets is not importable in this environment")
class TestChromaPluginClassResolution(unittest.TestCase):
    """Tests for the Beets 2.4.0 Chroma plugin class-resolution fix, patch script, and AcoustID boundaries.

    The web manager intentionally has zero direct Beets dependency (see
    docs/ARCHITECTURE.md), so `beets` is not installed in the CI
    python-tests environment. These tests exercise real Beets plugin-loader
    behavior and docker/beets/apply_patches.py (which itself does `import
    beets`), so they can only run where beets happens to be available
    (e.g. a developer machine with it pip-installed for this purpose, or a
    dedicated real-runtime validation job) -- matching the existing
    convention in tests/test_plugin_path_precedence.py.
    """

    def test_apply_patch_replaces_original_block_exactly_once(self):
        """apply_patch() replaces ORIGINAL_BLOCK with PATCHED_BLOCK when original source is unpatched."""
        from docker.beets.apply_patches import apply_patch, ORIGINAL_BLOCK, PATCHED_BLOCK
        mock_file = mock.mock_open(read_data=ORIGINAL_BLOCK)
        with mock.patch("beets.__version__", "2.4.0"), \
             mock.patch("builtins.open", mock_file), \
             mock.patch("importlib.reload"), \
             mock.patch("beets.plugins._get_plugin") as mock_get_plugin:
            mock_plugin = mock.MagicMock()
            mock_plugin.__class__.__module__ = "beetsplug.chroma"
            mock_plugin.__class__.__name__ = "AcoustidPlugin"
            # bpsync is not installed in this synthetic environment; return
            # None for it so the (correctly non-swallowing) bpsync sanity
            # check takes its "not importable, skip" path instead of being
            # compared against the chroma mock.
            mock_get_plugin.side_effect = lambda name: mock_plugin if name == "chroma" else None

            with mock.patch.dict("sys.modules", {"beetsplug.chroma": mock.MagicMock(AcoustidPlugin=mock_plugin.__class__)}):
                apply_patch()

        # Verify file write was called with PATCHED_BLOCK
        written_data = "".join(call.args[0] for call in mock_file().write.call_args_list)
        self.assertEqual(written_data, PATCHED_BLOCK)

    def test_apply_patch_correct_upstream_predicate_inserted(self):
        """PATCHED_BLOCK in apply_patches contains the upstream-equivalent startswith predicate."""
        from docker.beets.apply_patches import PATCHED_BLOCK
        self.assertIn("obj.__module__.startswith(f\"{mod.__name__}.\")", PATCHED_BLOCK)

    def test_apply_patch_accepts_already_patched_source(self):
        """apply_patch() accepts source already containing PATCHED_BLOCK without rewriting file."""
        from docker.beets.apply_patches import apply_patch, PATCHED_BLOCK
        mock_file = mock.mock_open(read_data=PATCHED_BLOCK)
        with mock.patch("beets.__version__", "2.4.0"), \
             mock.patch("builtins.open", mock_file), \
             mock.patch("importlib.reload"), \
             mock.patch("beets.plugins._get_plugin") as mock_get_plugin:
            mock_plugin = mock.MagicMock()
            mock_plugin.__class__.__module__ = "beetsplug.chroma"
            mock_plugin.__class__.__name__ = "AcoustidPlugin"
            # bpsync is not installed in this synthetic environment; return
            # None for it so the (correctly non-swallowing) bpsync sanity
            # check takes its "not importable, skip" path instead of being
            # compared against the chroma mock.
            mock_get_plugin.side_effect = lambda name: mock_plugin if name == "chroma" else None

            with mock.patch.dict("sys.modules", {"beetsplug.chroma": mock.MagicMock(AcoustidPlugin=mock_plugin.__class__)}):
                apply_patch()

        # File write should not have been called
        mock_file().write.assert_not_called()

    def test_apply_patch_already_patched_source_runs_sanity_checks(self):
        """apply_patch() on already-patched source still executes runtime sanity check and fails if check fails."""
        from docker.beets.apply_patches import apply_patch, PATCHED_BLOCK
        mock_file = mock.mock_open(read_data=PATCHED_BLOCK)
        with mock.patch("beets.__version__", "2.4.0"), \
             mock.patch("builtins.open", mock_file), \
             mock.patch("importlib.reload"), \
             mock.patch("beets.plugins._get_plugin", return_value=None):
            with mock.patch.dict("sys.modules", {"beetsplug.chroma": mock.MagicMock()}):
                with self.assertRaises(RuntimeError) as ctx:
                    apply_patch()
                self.assertIn("Sanity check failed for chroma", str(ctx.exception))

    def test_apply_patch_rejects_partial_patch(self):
        """apply_patch() raises RuntimeError when source contains a partial or unknown patch state."""
        from docker.beets.apply_patches import apply_patch
        partial_content = "def _get_plugin(): if obj.__module__ == mod.__name__: pass"
        mock_file = mock.mock_open(read_data=partial_content)
        with mock.patch("beets.__version__", "2.4.0"), \
             mock.patch("builtins.open", mock_file):
            with self.assertRaises(RuntimeError) as ctx:
                apply_patch()
            self.assertIn("Invalid source state", str(ctx.exception))

    def test_apply_patch_rejects_both_blocks_present(self):
        """apply_patch() raises RuntimeError when both ORIGINAL_BLOCK and PATCHED_BLOCK exist."""
        from docker.beets.apply_patches import apply_patch, ORIGINAL_BLOCK, PATCHED_BLOCK
        both_content = f"{ORIGINAL_BLOCK}\n{PATCHED_BLOCK}"
        mock_file = mock.mock_open(read_data=both_content)
        with mock.patch("beets.__version__", "2.4.0"), \
             mock.patch("builtins.open", mock_file):
            with self.assertRaises(RuntimeError) as ctx:
                apply_patch()
            self.assertIn("Invalid source state", str(ctx.exception))

    def test_apply_patch_rejects_neither_block_present(self):
        """apply_patch() raises RuntimeError when neither ORIGINAL_BLOCK nor PATCHED_BLOCK is present."""
        from docker.beets.apply_patches import apply_patch
        mock_file = mock.mock_open(read_data="def _get_plugin(): pass")
        with mock.patch("beets.__version__", "2.4.0"), \
             mock.patch("builtins.open", mock_file):
            with self.assertRaises(RuntimeError) as ctx:
                apply_patch()
            self.assertIn("Invalid source state", str(ctx.exception))

    def test_apply_patch_rejects_multiple_original_blocks(self):
        """apply_patch() raises RuntimeError when ORIGINAL_BLOCK occurs more than once."""
        from docker.beets.apply_patches import apply_patch, ORIGINAL_BLOCK
        multi_content = f"{ORIGINAL_BLOCK}\n{ORIGINAL_BLOCK}"
        mock_file = mock.mock_open(read_data=multi_content)
        with mock.patch("beets.__version__", "2.4.0"), \
             mock.patch("builtins.open", mock_file):
            with self.assertRaises(RuntimeError) as ctx:
                apply_patch()
            self.assertIn("Invalid source state", str(ctx.exception))

    def test_apply_patch_rejects_multiple_patched_blocks(self):
        """apply_patch() raises RuntimeError when PATCHED_BLOCK occurs more than once."""
        from docker.beets.apply_patches import apply_patch, PATCHED_BLOCK
        multi_content = f"{PATCHED_BLOCK}\n{PATCHED_BLOCK}"
        mock_file = mock.mock_open(read_data=multi_content)
        with mock.patch("beets.__version__", "2.4.0"), \
             mock.patch("builtins.open", mock_file):
            with self.assertRaises(RuntimeError) as ctx:
                apply_patch()
            self.assertIn("Invalid source state", str(ctx.exception))

    def test_apply_patch_rejects_version_mismatch(self):
        """apply_patch() raises RuntimeError when installed Beets version is not 2.4.0."""
        from docker.beets.apply_patches import apply_patch
        with mock.patch("beets.__version__", "2.5.0"):
            with self.assertRaises(RuntimeError) as ctx:
                apply_patch()
            self.assertIn("expected Beets 2.4.0", str(ctx.exception))

    @staticmethod
    def _exec_block(block_src, mod):
        """Execute a literal ORIGINAL_BLOCK/PATCHED_BLOCK string from
        docker/beets/apply_patches.py as real code against a synthetic
        module, so tests exercise the actual production constant byte for
        byte instead of re-deriving its selection logic independently.

        ``block_src`` is written to sit at the same indentation and use the
        same free variables (``namespace``, ``name``, ``inspect``,
        ``GenericAlias``, ``BeetsPlugin``) as the real body of
        beets.plugins._get_plugin, so it is wrapped in a matching function
        signature here rather than altered in any way.
        """
        import inspect
        import types
        from types import GenericAlias
        import beets.plugins as beets_plugins_module

        # block_src's own leading whitespace becomes the function body's
        # indentation verbatim, so the literal production string is used
        # completely unmodified (no re-indentation arithmetic that could
        # itself drift from the real constant).
        first_line = block_src.splitlines()[0]
        indent = first_line[: len(first_line) - len(first_line.lstrip())]
        func_src = "def _select(namespace, name):\n" + block_src + "\n" + indent + "return None\n"
        exec_globals = {
            "inspect": inspect,
            "GenericAlias": GenericAlias,
            "BeetsPlugin": beets_plugins_module.BeetsPlugin,
        }
        exec(compile(func_src, "<production_block>", "exec"), exec_globals)
        namespace = types.SimpleNamespace(**{mod.__name__.rsplit(".", 1)[-1]: mod})
        return exec_globals["_select"](namespace, mod.__name__.rsplit(".", 1)[-1])

    def test_unpatched_original_block_selects_musicbrainz_plugin_first(self):
        """The real, unpatched ORIGINAL_BLOCK reproduces the upstream defect: it
        selects the imported MusicBrainzPlugin instead of chroma's own
        AcoustidPlugin, because it has no module-origin check at all."""
        from docker.beets.apply_patches import ORIGINAL_BLOCK
        import beets.plugins

        class FakeMusicBrainzPlugin(beets.plugins.BeetsPlugin):
            __module__ = "beetsplug.musicbrainz"

        class FakeAcoustidPlugin(beets.plugins.BeetsPlugin):
            __module__ = "beetsplug.chroma"

        class FakeChromaModule:
            __name__ = "beetsplug.chroma"

        mod = FakeChromaModule()
        mod.__dict__["MusicBrainzPlugin"] = FakeMusicBrainzPlugin
        mod.__dict__["AcoustidPlugin"] = FakeAcoustidPlugin

        selected = self._exec_block(ORIGINAL_BLOCK, mod)
        self.assertIsInstance(selected, FakeMusicBrainzPlugin)
        self.assertNotIsInstance(selected, FakeAcoustidPlugin)

    def test_patched_block_selects_chroma_acoustid_plugin(self):
        """The real PATCHED_BLOCK selects AcoustidPlugin over the imported
        MusicBrainzPlugin for the chroma module."""
        from docker.beets.apply_patches import PATCHED_BLOCK
        import beets.plugins

        class FakeMusicBrainzPlugin(beets.plugins.BeetsPlugin):
            __module__ = "beetsplug.musicbrainz"

        class FakeAcoustidPlugin(beets.plugins.BeetsPlugin):
            __module__ = "beetsplug.chroma"

        class FakeChromaModule:
            __name__ = "beetsplug.chroma"

        mod = FakeChromaModule()
        mod.__dict__["MusicBrainzPlugin"] = FakeMusicBrainzPlugin
        mod.__dict__["AcoustidPlugin"] = FakeAcoustidPlugin

        selected = self._exec_block(PATCHED_BLOCK, mod)
        self.assertIsInstance(selected, FakeAcoustidPlugin)
        self.assertNotIsInstance(selected, FakeMusicBrainzPlugin)

    def test_patched_block_accepts_package_submodule_plugin(self):
        """The real PATCHED_BLOCK accepts a plugin class defined in a package
        submodule (e.g. beetsplug.bandcamp.search) of the requested module."""
        from docker.beets.apply_patches import PATCHED_BLOCK
        import beets.plugins

        class FakeSubmodulePlugin(beets.plugins.BeetsPlugin):
            __module__ = "beetsplug.bandcamp.search"

        class FakeBandcampModule:
            __name__ = "beetsplug.bandcamp"

        mod = FakeBandcampModule()
        mod.__dict__["SubmodulePlugin"] = FakeSubmodulePlugin

        selected = self._exec_block(PATCHED_BLOCK, mod)
        self.assertIsInstance(selected, FakeSubmodulePlugin)

    def test_patched_block_rejects_foreign_plugin_outside_namespace(self):
        """The real PATCHED_BLOCK rejects a foreign plugin class whose module
        is neither the requested module nor one of its submodules."""
        from docker.beets.apply_patches import PATCHED_BLOCK
        import beets.plugins

        class FakeForeignPlugin(beets.plugins.BeetsPlugin):
            __module__ = "beetsplug.other"

        class FakeChromaModule:
            __name__ = "beetsplug.chroma"

        mod = FakeChromaModule()
        mod.__dict__["ForeignPlugin"] = FakeForeignPlugin

        selected = self._exec_block(PATCHED_BLOCK, mod)
        self.assertIsNone(selected)

    def test_patched_block_resolves_bpsync_correctly(self):
        """The real PATCHED_BLOCK selects BPSyncPlugin over the imported
        BeatportPlugin for the bpsync module."""
        from docker.beets.apply_patches import PATCHED_BLOCK
        import beets.plugins

        class FakeBeatportPlugin(beets.plugins.BeetsPlugin):
            __module__ = "beetsplug.beatport"

        class FakeBPSyncPlugin(beets.plugins.BeetsPlugin):
            __module__ = "beetsplug.bpsync"

        class FakeBPSyncModule:
            __name__ = "beetsplug.bpsync"

        mod = FakeBPSyncModule()
        mod.__dict__["BeatportPlugin"] = FakeBeatportPlugin
        mod.__dict__["BPSyncPlugin"] = FakeBPSyncPlugin

        selected = self._exec_block(PATCHED_BLOCK, mod)
        self.assertIsInstance(selected, FakeBPSyncPlugin)
        self.assertNotIsInstance(selected, FakeBeatportPlugin)

    def test_missing_acoustid_key_blocks_submission_routes(self):
        """album_acoustid_submit and item_acoustid_submit block submission when AcoustID key is missing."""
        from app import app
        with mock.patch("routes_submissions._submission_readiness") as mock_readiness:
            mock_readiness.return_value = {
                "plugins": {"chroma": True},
                "fpcalc_available": True,
                "pyacoustid_available": True,
                "acoustid_key_configured": False,
                "remote_reachable": True,
            }
            with app.test_request_context():
                from routes_submissions import album_acoustid_submit, item_acoustid_submit
                with mock.patch("routes_submissions.lib.get_album") as mock_album:
                    mock_album.return_value = mock.MagicMock(items=lambda: [])
                    res, code = album_acoustid_submit(1)
                    self.assertEqual(code, 400)
                    self.assertIn("AcoustID API key is not configured", res.json["error"])

                with mock.patch("routes_submissions.lib.get_item") as mock_item:
                    mock_item.return_value = mock.MagicMock(album_id=None)
                    res, code = item_acoustid_submit(1)
                    self.assertEqual(code, 400)
                    self.assertIn("AcoustID API key is not configured", res.json["error"])

    def test_acoustid_submission_boundary_interception(self):
        """Verify that submit_items reaches PyAcoustID boundary with expected
        parameters without external calls.

        Two independent safety barriers are used so that no real AcoustID
        request can occur even if one of them is somehow bypassed:
        (1) sys.modules["acoustid"] is replaced with a mock before the first
        import of beetsplug.chroma, so the real PyAcoustID networking code is
        never reached; (2) socket.socket construction is additionally
        blocked for the duration of the real submit_items() call, so even a
        hypothetical direct socket-level fallback would raise instead of
        reaching the network.
        """
        import socket
        import sys
        mock_acoustid = mock.MagicMock()
        submitted_calls = []

        def mock_submit(apikey, userkey, data, timeout=None):
            submitted_calls.append({
                "apikey": apikey,
                "userkey": userkey,
                "data": list(data),
                "timeout": timeout,
            })
            return {"status": "ok", "submissions": [{"id": 100, "status": "pending"}]}

        mock_acoustid.submit = mock_submit
        mock_acoustid.API_KEY = "1vOwZtEn"

        def _blocked_socket(*args, **kwargs):
            raise AssertionError(
                "Real socket construction attempted during a test that must "
                "make zero external AcoustID/network calls."
            )

        with mock.patch.dict(sys.modules, {"acoustid": mock_acoustid}):
            import beetsplug.chroma
            import beets.library

            mock_item = mock.MagicMock(spec=beets.library.Item)
            mock_item.length = 180
            mock_item.mb_trackid = "99999999-9999-9999-9999-999999999999"
            mock_item.path = b"/fake/path.mp3"

            with mock.patch("beetsplug.chroma.fingerprint_item", return_value="AQAA_FAKE_FINGERPRINT"), \
                 mock.patch("socket.socket", side_effect=_blocked_socket):
                log = mock.MagicMock()
                beetsplug.chroma.submit_items(log, "user_key_777", [mock_item])

        self.assertEqual(len(submitted_calls), 1)
        call = submitted_calls[0]
        self.assertEqual(call["apikey"], beetsplug.chroma.API_KEY)
        self.assertEqual(call["userkey"], "user_key_777")
        self.assertEqual(len(call["data"]), 1)
        self.assertEqual(call["data"][0]["fingerprint"], "AQAA_FAKE_FINGERPRINT")
        self.assertEqual(call["data"][0]["mbid"], "99999999-9999-9999-9999-999999999999")
        self.assertEqual(call["data"][0]["duration"], 180)


class ComposeSecurityValidatorTests(unittest.TestCase):
    """Real negative-path coverage for scripts/validate_compose_security.py,
    not just a single pass/fail smoke check."""

    def test_baseline_files_pass(self):
        import scripts.validate_compose_security as vcs
        self.assertEqual(vcs.main(), 0)

    def test_locally_built_image_with_upstream_digest_suffix_fails(self):
        import scripts.validate_compose_security as vcs
        errors: list = []
        vcs._check_image_digest_semantics(
            "beets", "beets-engine:2.4.0@sha256:" + "a" * 64, has_build=True, errors=errors
        )
        self.assertTrue(any("must not attach a digest" in e for e in errors), errors)

    def test_locally_built_image_plain_tag_passes_when_dockerfile_pinned(self):
        import scripts.validate_compose_security as vcs
        errors: list = []
        vcs._check_image_digest_semantics("beets", "beets-engine:2.4.0", has_build=True, errors=errors)
        self.assertEqual(errors, [])

    def test_locally_built_image_fails_when_dockerfile_base_not_pinned(self):
        import scripts.validate_compose_security as vcs
        with mock.patch.object(vcs, "_dockerfile_beets_base_is_approved", return_value=False):
            errors: list = []
            vcs._check_image_digest_semantics("beets", "beets-engine:2.4.0", has_build=True, errors=errors)
            self.assertTrue(any("does not pin the approved" in e for e in errors), errors)

    def test_pulled_third_party_image_without_digest_fails(self):
        import scripts.validate_compose_security as vcs
        errors: list = []
        vcs._check_image_digest_semantics(
            "bgutil-provider", "brainicism/bgutil-ytdlp-pot-provider:1.3.1-deno", has_build=False, errors=errors
        )
        self.assertTrue(any("not digest-pinned" in e for e in errors), errors)

    def test_pulled_third_party_image_with_digest_passes(self):
        import scripts.validate_compose_security as vcs
        errors: list = []
        vcs._check_image_digest_semantics(
            "bgutil-provider",
            "brainicism/bgutil-ytdlp-pot-provider:1.3.1-deno@sha256:" + "b" * 64,
            has_build=False,
            errors=errors,
        )
        self.assertEqual(errors, [])

    def test_locally_built_image_with_no_tag_fails(self):
        """CodeQL py/polynomial-redos (#356): the old ^[^:@/]+(?:/[^:@]+)*$
        regex was ambiguous (both groups accept the same non-colon,
        non-at characters) and worst-case polynomial on crafted input.
        Replaced with a plain substring check; this proves it still
        detects the untagged case it was written for."""
        import scripts.validate_compose_security as vcs
        errors: list = []
        vcs._check_image_digest_semantics("beets", "beets-engine", has_build=True, errors=errors)
        self.assertTrue(any("has no tag" in e for e in errors), errors)

    def test_pulled_third_party_image_with_no_tag_or_digest_fails(self):
        import scripts.validate_compose_security as vcs
        errors: list = []
        vcs._check_image_digest_semantics(
            "bgutil-provider", "brainicism/bgutil-ytdlp-pot-provider", has_build=False, errors=errors
        )
        self.assertTrue(any("has no tag or digest" in e for e in errors), errors)

    def test_image_digest_check_has_no_regex_backtracking_on_adversarial_input(self):
        """Regression guard for the class of bug: an input engineered to
        exploit the old ambiguous regex's backtracking must resolve
        near-instantly now that it's a plain substring check."""
        import time

        import scripts.validate_compose_security as vcs
        adversarial = "a" * 50_000 + "!"
        errors: list = []
        start = time.monotonic()
        vcs._check_image_digest_semantics("beets", adversarial, has_build=True, errors=errors)
        self.assertLess(time.monotonic() - start, 1.0)

    def test_hardcoded_lan_ip_in_outbound_allowlist_fails(self):
        import scripts.validate_compose_security as vcs
        errors: list = []
        vcs._check_no_hardcoded_lan_allowlist(
            'BEETS_OUTBOUND_ALLOWLIST: "192.168.1.5:32400"', "synthetic.yml", errors
        )
        self.assertTrue(errors)

    def test_env_var_passthrough_outbound_allowlist_passes(self):
        import scripts.validate_compose_security as vcs
        errors: list = []
        vcs._check_no_hardcoded_lan_allowlist(
            'BEETS_OUTBOUND_ALLOWLIST: "${BEETS_OUTBOUND_ALLOWLIST:-}"', "synthetic.yml", errors
        )
        self.assertEqual(errors, [])

    def test_empty_outbound_allowlist_passes(self):
        import scripts.validate_compose_security as vcs
        errors: list = []
        vcs._check_no_hardcoded_lan_allowlist("BEETS_OUTBOUND_ALLOWLIST=", "synthetic.env", errors)
        self.assertEqual(errors, [])

    def _write_compose_variant(self, tmpdir, beets_block, web_block=""):
        from pathlib import Path
        content = "services:\n  beets:\n" + beets_block
        if web_block:
            content += "\n  beets-web-manager:\n" + web_block
        path = Path(tmpdir) / "docker-compose.synthetic.yml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_public_8338_binding_fails(self):
        import scripts.validate_compose_security as vcs
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_compose_variant(
                tmpdir,
                "    image: beets-engine:2.4.0\n    ports:\n      - \"8338:8338\"\n",
                "    ports:\n      - \"127.0.0.1:8337:8337\"\n",
            )
            errors: list = []
            with mock.patch.object(vcs, "_dockerfile_beets_base_is_approved", return_value=True):
                vcs._check_compose_variant(path, errors, [])
            self.assertTrue(any("internal-only or bind to loopback" in e for e in errors), errors)

    def test_internal_only_8338_passes(self):
        import scripts.validate_compose_security as vcs
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_compose_variant(
                tmpdir,
                "    build:\n      context: .\n      dockerfile: Dockerfile.beets\n    image: beets-engine:2.4.0\n    expose:\n      - \"8338\"\n",
                "    ports:\n      - \"127.0.0.1:8337:8337\"\n",
            )
            errors: list = []
            with mock.patch.object(vcs, "_dockerfile_beets_base_is_approved", return_value=True):
                vcs._check_compose_variant(path, errors, [])
            self.assertEqual(errors, [])

    def test_loopback_only_8337_passes_via_web_manager_service(self):
        import scripts.validate_compose_security as vcs
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_compose_variant(
                tmpdir,
                "    build:\n      context: .\n      dockerfile: Dockerfile.beets\n    image: beets-engine:2.4.0\n    expose:\n      - \"8338\"\n",
                "    ports:\n      - \"127.0.0.1:${WEBCONTROL_PORT:-8337}:8337\"\n",
            )
            errors: list = []
            with mock.patch.object(vcs, "_dockerfile_beets_base_is_approved", return_value=True):
                vcs._check_compose_variant(path, errors, [])
            self.assertEqual(errors, [])

    def test_docker_socket_mount_fails(self):
        import scripts.validate_compose_security as vcs
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_compose_variant(
                tmpdir,
                "    image: beets-engine:2.4.0\n    volumes:\n      - /var/run/docker.sock:/var/run/docker.sock\n",
            )
            errors: list = []
            with mock.patch.object(vcs, "_dockerfile_beets_base_is_approved", return_value=True):
                vcs._check_compose_variant(path, errors, [])
            self.assertTrue(any("Docker socket" in e for e in errors), errors)

    def test_both_compose_files_are_validated(self):
        import scripts.validate_compose_security as vcs
        errors: list = []
        vcs._check_compose_variant(vcs.STANDALONE_COMPOSE, errors, [])
        vcs._check_compose_variant(vcs.ARRS_COMPOSE, errors, [])
        self.assertEqual(errors, [])


class SecretScannerTests(unittest.TestCase):
    """Real negative-path coverage for scripts/security_secret_scan.py."""

    def test_baseline_repo_passes(self):
        import scripts.security_secret_scan as sss
        with mock.patch("sys.argv", ["security_secret_scan.py"]):
            self.assertEqual(sss.main(), 0)

    def test_real_looking_secret_is_detected(self):
        import scripts.security_secret_scan as sss
        # Built at runtime (not a contiguous literal) so this test fixture
        # itself is never mistaken for a real committed secret by a
        # whole-repo scan of this very test file.
        fake_key = "AKIA" + "ABCDEFGHIJKLMNOP"
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "leaked.env"
            path.write_text(f"AWS_SECRET={fake_key}\n", encoding="utf-8")
            findings = sss.scan_file(path)
            self.assertTrue(any(rule == "aws-access-key" for _, rule in findings))

    def test_placeholder_accepted_only_in_env_example(self):
        import scripts.security_secret_scan as sss
        example_path = Path("some/dir/.env.example")
        other_path = Path("some/dir/real.env")
        self.assertTrue(sss.is_placeholder("changeme", example_path))
        self.assertFalse(sss.is_placeholder("changeme", other_path))

    def test_weak_placeholder_rejected_outside_example_context(self):
        import scripts.security_secret_scan as sss
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "real_config.env"
            path.write_text("SOME_API_KEY=changeme\n", encoding="utf-8")
            findings = sss.scan_file(path)
            self.assertIn((1, "config-secret-assignment"), findings)

    def test_scanner_exception_does_not_hide_same_value_elsewhere(self):
        import scripts.security_secret_scan as sss
        with tempfile.TemporaryDirectory() as tmpdir:
            example_path = Path(tmpdir) / ".env.example"
            example_path.write_text("BEETS_API_TOKEN=changeme\n", encoding="utf-8")
            real_path = Path(tmpdir) / "other_service.env"
            real_path.write_text("BEETS_API_TOKEN=changeme\n", encoding="utf-8")
            self.assertEqual(sss.scan_file(example_path), [])
            self.assertEqual(sss.scan_file(real_path), [(1, "config-secret-assignment")])

    def test_ci_workflow_placeholder_env_is_narrowly_scoped(self):
        import scripts.security_secret_scan as sss
        workflow_path = sss.ROOT / ".github" / "workflows" / "docker-build.yml"
        non_workflow_path = sss.ROOT / "docker-compose.yml"
        self.assertTrue(sss.is_ci_only_workflow_file(workflow_path))
        self.assertFalse(sss.is_ci_only_workflow_file(non_workflow_path))


class BeetsApiTokenStrengthTests(unittest.TestCase):
    """Coverage for backend/beets_control_agent.py's BEETS_API_TOKEN
    strength/placeholder validation, fixing the gap where a non-empty but
    trivially weak value like 'changeme' previously granted real
    authenticated control-agent access."""

    def test_blank_token_rejected(self):
        from backend.beets_control_agent import beets_api_token_is_usable
        self.assertFalse(beets_api_token_is_usable(""))
        self.assertFalse(beets_api_token_is_usable("   "))

    def test_placeholder_token_rejected(self):
        from backend.beets_control_agent import beets_api_token_is_usable
        self.assertFalse(beets_api_token_is_usable("changeme"))
        self.assertFalse(beets_api_token_is_usable("CHANGEME"))
        self.assertFalse(beets_api_token_is_usable("changeme_required_strong_token"))

    def test_short_weak_token_rejected(self):
        from backend.beets_control_agent import beets_api_token_is_usable
        self.assertFalse(beets_api_token_is_usable("short-token-16ch"))

    def test_valid_generated_token_accepted(self):
        from backend.beets_control_agent import beets_api_token_is_usable
        self.assertTrue(beets_api_token_is_usable("a" * 32))
        self.assertTrue(beets_api_token_is_usable("Xk9pQ2vR7mN4tL8wZ1cB5dF3gH6jK0sA"))

    def test_run_agent_raises_on_weak_token(self):
        import backend.beets_control_agent as bca
        with mock.patch.object(bca, "BEETS_API_TOKEN", "changeme"):
            with self.assertRaises(RuntimeError):
                bca.run_agent()

    def test_authenticate_returns_500_on_weak_configured_token(self):
        from backend.beets_control_agent import ControlAgentHandler
        handler = ControlAgentHandler.__new__(ControlAgentHandler)
        handler.headers = {}
        responses = []
        handler._send_json = lambda code, data: responses.append((code, data))
        with mock.patch("backend.beets_control_agent.BEETS_API_TOKEN", "changeme"):
            result = handler._authenticate()
        self.assertFalse(result)
        self.assertEqual(responses[0][0], 500)


class FetchartOperationalReadinessTests(unittest.TestCase):
    """Coverage for the readiness fix in routes_setup.py's
    _fetchart_integration_status(): operational must depend on the real
    plugin-loader probe result, never on local Beets package
    importability alone (which is unavailable by design in the web-manager
    process and would previously force a permanent false negative in any
    environment -- e.g. CI -- where beetsplug.fetchart happens not to be
    locally importable even though the loader probe genuinely succeeded)."""

    def _diagnostics(self, **overrides):
        base = {
            "configured_plugins": {"fetchart"},
            "loaded_plugins": {"fetchart"},
            "plugin_failures": [],
            "plugin_loader_ok": True,
        }
        base.update(overrides)
        return base

    def test_configured_and_loader_ok_is_operational(self):
        from routes_setup import _fetchart_integration_status
        status = _fetchart_integration_status(self._diagnostics())
        self.assertTrue(status["operational"])

    def test_loader_failure_is_not_operational(self):
        from routes_setup import _fetchart_integration_status
        status = _fetchart_integration_status(self._diagnostics(plugin_loader_ok=False))
        self.assertFalse(status["operational"])

    def test_missing_plugin_loader_field_is_not_operational(self):
        from routes_setup import _fetchart_integration_status
        diagnostics = self._diagnostics()
        del diagnostics["plugin_loader_ok"]
        status = _fetchart_integration_status(diagnostics)
        self.assertFalse(status["operational"])

    def test_plugin_failure_pattern_is_not_operational(self):
        from routes_setup import _fetchart_integration_status
        status = _fetchart_integration_status(
            self._diagnostics(plugin_failures=["** error loading plugin fetchart"])
        )
        self.assertFalse(status["operational"])

    def test_not_configured_is_not_operational_even_if_loader_ok(self):
        from routes_setup import _fetchart_integration_status
        status = _fetchart_integration_status(self._diagnostics(configured_plugins=set()))
        self.assertFalse(status["operational"])

    def test_local_beets_absent_does_not_force_false_negative(self):
        """The removed probe['installed']/probe['importable_in_process']/
        probe['bundled_namespace_merged'] gate previously required
        beetsplug.fetchart to be importable in-process; confirm operational
        can now be True even when the (still-computed, still-displayed)
        namespace probe reports everything unavailable."""
        from routes_setup import _fetchart_integration_status
        with mock.patch(
            "routes_setup._fetchart_namespace_probe",
            return_value={"importable_in_process": False, "installed": False, "bundled_namespace_merged": False},
        ):
            status = _fetchart_integration_status(self._diagnostics())
        self.assertTrue(status["operational"])
        self.assertFalse(status["importable_in_process"])


class BeetsEngineImageVerifierTests(unittest.TestCase):
    """Real failure-path coverage for scripts/verify_beets_engine_image.py,
    plus safety checks (no shell interpolation of untrusted values, network
    disabled for offline checks)."""

    def test_unsafe_image_identifier_rejected(self):
        import scripts.verify_beets_engine_image as vbi
        with self.assertRaises(SystemExit):
            vbi._require_safe_identifier("image; rm -rf /", "--image")

    def test_unsafe_plugin_identifier_rejected(self):
        import scripts.verify_beets_engine_image as vbi
        with self.assertRaises(SystemExit):
            vbi._require_safe_identifier("chroma'); import os; os.system('id", "--require-plugin")

    def test_safe_identifier_accepted(self):
        import scripts.verify_beets_engine_image as vbi
        self.assertEqual(vbi._require_safe_identifier("beets-engine:2.4.0", "--image"), "beets-engine:2.4.0")

    def test_missing_image_fails(self):
        import scripts.verify_beets_engine_image as vbi
        mock_inspect = mock.MagicMock(returncode=1, stdout="", stderr="No such image")
        with mock.patch.object(vbi, "_run_docker", return_value=mock_inspect):
            errors: list = []
            self.assertFalse(vbi.check_image_exists("nonexistent:image", errors))
            self.assertTrue(any("does not exist locally" in e for e in errors))

    def test_wrong_version_fails(self):
        import scripts.verify_beets_engine_image as vbi
        mock_proc = mock.MagicMock(returncode=0, stdout="beets version 2.5.0\n", stderr="")
        with mock.patch.object(vbi, "run_container_shell", return_value=mock_proc):
            errors: list = []
            vbi.check_version("beets-engine:ci", "2.4.0", errors)
            self.assertTrue(any("Expected Beets version" in e for e in errors))

    def test_missing_plugin_fails(self):
        import scripts.verify_beets_engine_image as vbi
        mock_proc = mock.MagicMock(returncode=1, stdout="FAILED: returned None", stderr="")
        with mock.patch.object(vbi, "run_container_python", return_value=mock_proc):
            errors: list = []
            vbi.check_required_plugin("beets-engine:ci", "chroma", errors)
            self.assertTrue(any("failed to resolve" in e for e in errors))

    def test_duplicate_forbidden_plugin_fails(self):
        import scripts.verify_beets_engine_image as vbi
        mock_proc = mock.MagicMock(
            returncode=0,
            stdout="plugins: chroma, convert, musicbrainz, musicbrainz\n",
            stderr="",
        )
        with mock.patch.object(vbi, "run_container_shell", return_value=mock_proc):
            errors: list = []
            vbi.check_forbid_duplicate_plugin("beets-engine:ci", "musicbrainz", ["chroma", "musicbrainz"], errors)
            self.assertTrue(any("duplicated" in e for e in errors))

    def test_non_duplicated_plugin_passes(self):
        import scripts.verify_beets_engine_image as vbi
        mock_proc = mock.MagicMock(
            returncode=0,
            stdout="plugins: chroma, convert, musicbrainz\n",
            stderr="",
        )
        with mock.patch.object(vbi, "run_container_shell", return_value=mock_proc):
            errors: list = []
            vbi.check_forbid_duplicate_plugin("beets-engine:ci", "musicbrainz", ["chroma", "musicbrainz"], errors)
            self.assertEqual(errors, [])

    def test_bpsync_class_resolution_failure_always_fails(self):
        # Even the module/class import step failing is a fatal regression --
        # worse than the known incompatibility, since that step is expected
        # to always succeed.
        import scripts.verify_beets_engine_image as vbi
        mock_proc = mock.MagicMock(returncode=1, stdout="FAILED: <class 'beetsplug.beatport.BeatportPlugin'>", stderr="")
        with mock.patch.object(vbi, "run_container_python", return_value=mock_proc):
            errors: list = []
            vbi.check_bpsync("beets-engine:ci", errors)
            self.assertTrue(any("bpsync module/class resolution failed" in e for e in errors))

    def test_bpsync_operational_when_loaded_via_real_beets_loader(self):
        # Not just importable -- actually appears in the real loader's
        # "plugins:" line, meaning construction and setup succeeded.
        import scripts.verify_beets_engine_image as vbi
        resolve_ok = mock.MagicMock(returncode=0, stdout="RESOLVED: beetsplug.bpsync BPSyncPlugin", stderr="")
        loader_ok = mock.MagicMock(returncode=0, stdout="plugins: bpsync\n", stderr="")
        with mock.patch.object(vbi, "run_container_python", return_value=resolve_ok), \
             mock.patch.object(vbi, "run_container_shell", return_value=loader_ok):
            errors: list = []
            vbi.check_bpsync("beets-engine:ci", errors)
            self.assertEqual(errors, [])

    def test_bpsync_known_incompatibility_is_non_fatal_by_default(self):
        # Reproduces the real, live-verified failure: BPSyncPlugin resolves
        # (importable, correct class) but Beets' real loader fails to
        # construct it because BeatportPlugin.setup() requires a session
        # argument bpsync.py's __init__ never provides. Not enabled in
        # production, so this must not fail the build unless explicitly
        # required.
        import scripts.verify_beets_engine_image as vbi
        resolve_ok = mock.MagicMock(returncode=0, stdout="RESOLVED: beetsplug.bpsync BPSyncPlugin", stderr="")
        loader_incompatible = mock.MagicMock(
            returncode=0,
            stdout="plugins: \n",
            stderr=(
                "** error loading plugin bpsync\n"
                "TypeError: BeatportPlugin.setup() missing 1 required positional argument: 'session'\n"
            ),
        )
        with mock.patch.object(vbi, "run_container_python", return_value=resolve_ok), \
             mock.patch.object(vbi, "run_container_shell", return_value=loader_incompatible):
            errors: list = []
            vbi.check_bpsync("beets-engine:ci", errors, require_operational=False)
            self.assertEqual(errors, [])

    def test_bpsync_known_incompatibility_fails_when_required_operational(self):
        import scripts.verify_beets_engine_image as vbi
        resolve_ok = mock.MagicMock(returncode=0, stdout="RESOLVED: beetsplug.bpsync BPSyncPlugin", stderr="")
        loader_incompatible = mock.MagicMock(
            returncode=0,
            stdout="plugins: \n",
            stderr=(
                "** error loading plugin bpsync\n"
                "TypeError: BeatportPlugin.setup() missing 1 required positional argument: 'session'\n"
            ),
        )
        with mock.patch.object(vbi, "run_container_python", return_value=resolve_ok), \
             mock.patch.object(vbi, "run_container_shell", return_value=loader_incompatible):
            errors: list = []
            vbi.check_bpsync("beets-engine:ci", errors, require_operational=True)
            self.assertTrue(any("required to be operational but is not" in e for e in errors))

    def test_bpsync_unexpected_failure_is_always_fatal(self):
        # A failure that does NOT match the known setup()-signature error
        # must never be silently absorbed into the "known incompatibility,
        # non-fatal" bucket -- that would hide a real, new regression.
        import scripts.verify_beets_engine_image as vbi
        resolve_ok = mock.MagicMock(returncode=0, stdout="RESOLVED: beetsplug.bpsync BPSyncPlugin", stderr="")
        loader_broken = mock.MagicMock(
            returncode=1,
            stdout="",
            stderr="ImportError: some completely different, undiagnosed failure\n",
        )
        with mock.patch.object(vbi, "run_container_python", return_value=resolve_ok), \
             mock.patch.object(vbi, "run_container_shell", return_value=loader_broken):
            errors: list = []
            vbi.check_bpsync("beets-engine:ci", errors, require_operational=False)
            self.assertTrue(any("new regression" in e for e in errors))

    def test_required_command_help_failure_fails(self):
        import scripts.verify_beets_engine_image as vbi
        mock_proc = mock.MagicMock(returncode=1, stdout="", stderr="error: unknown command 'submit'")
        with mock.patch.object(vbi, "run_container_shell", return_value=mock_proc):
            errors: list = []
            vbi.check_command_help("beets-engine:ci", "submit", ["chroma"], errors)
            self.assertTrue(any("failed with exit code" in e for e in errors))

    def test_malformed_plugin_output_fails(self):
        import scripts.verify_beets_engine_image as vbi
        mock_proc = mock.MagicMock(returncode=0, stdout="unexpected garbage, no OK prefix", stderr="")
        with mock.patch.object(vbi, "run_container_python", return_value=mock_proc):
            errors: list = []
            vbi.check_required_plugin("beets-engine:ci", "chroma", errors)
            self.assertTrue(any("failed to resolve" in e for e in errors))

    def test_containers_run_with_network_none(self):
        import scripts.verify_beets_engine_image as vbi
        with mock.patch.object(vbi, "_run_docker", return_value=mock.MagicMock(returncode=0, stdout="", stderr="")) as mock_run:
            vbi.run_container_shell("beets-engine:ci", "echo hi")
            args = mock_run.call_args[0][0]
            self.assertIn("--network", args)
            self.assertIn("none", args)

    def test_docker_binary_missing_reports_clear_error(self):
        import scripts.verify_beets_engine_image as vbi
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("no docker")):
            with self.assertRaises(vbi.DockerCommandError):
                vbi._run_docker(["version"])

    def test_end_to_end_argument_parsing_success(self):
        import scripts.verify_beets_engine_image as vbi
        mock_inspect = mock.MagicMock(returncode=0, stdout="", stderr="")
        mock_version = mock.MagicMock(returncode=0, stdout="beets version 2.4.0\n", stderr="")
        mock_agent_files = mock.MagicMock(returncode=0, stdout="", stderr="")
        mock_help = mock.MagicMock(returncode=0, stdout="", stderr="")

        def fake_run_docker(args, input_text=None):
            if args[:2] == ["image", "inspect"]:
                return mock_inspect
            joined = " ".join(args)
            if "beet version" in joined:
                return mock_version
            if "test -f" in joined:
                return mock_agent_files
            return mock_help

        with mock.patch.object(vbi, "_run_docker", side_effect=fake_run_docker):
            with mock.patch("sys.argv", [
                "verify_beets_engine_image.py",
                "--image", "test-image:latest",
                "--require-version", "2.4.0",
                "--require-command-help", "mbsubmit",
            ]):
                self.assertEqual(vbi.main(), 0)


class TestPluginFirstArchitectureGuardrails(unittest.TestCase):
    def test_agents_md_has_plugin_first_section(self):
        content = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("## Plugin-First Beets Architecture", content)
        self.assertIn("Check Beets core and installed plugins before implementing", content)
        self.assertIn("built-in Beets web plugin (`beet web`) is the sole intentional replacement", content)
        self.assertIn("No local `beet`, `fpcalc`, `ffprobe`", content)

    def test_claude_md_points_to_agents_md_and_plugin_requirements(self):
        content = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("Read `AGENTS.md`", content)
        self.assertIn("Existing Beets/plugin capability reviewed:", content)

    def test_agent_workflow_has_plugin_evaluation_directive(self):
        content = (ROOT / "docs" / "AGENT_WORKFLOW.md").read_text(encoding="utf-8")
        self.assertIn("Evaluate Beets core and installed plugins before implementing", content)

    def test_review_md_has_plugin_first_checklist(self):
        content = (ROOT / "REVIEW.md").read_text(encoding="utf-8")
        self.assertIn("## Plugin-First Ownership and Container Boundaries", content)
        self.assertIn("No hidden local subprocess fallback", content)

    def test_web_manager_runtime_has_no_local_fpcalc_execution(self):
        files_to_check = [ROOT / "helpers_mb.py", ROOT / "app.py"]
        for fpath in files_to_check:
            content = fpath.read_text(encoding="utf-8")
            self.assertNotIn('shutil.which("fpcalc")', content, f"Found local fpcalc probe in {fpath.name}")
            self.assertNotIn('subprocess.run([fpcalc', content, f"Found local fpcalc subprocess call in {fpath.name}")

    def test_requirements_txt_excludes_plugin_only_packages(self):
        req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        for pkg in ("pylast", "pylistenbrainz", "Pillow", "beautifulsoup4"):
            self.assertNotIn(f"{pkg}==", req, f"{pkg} should be removed from web manager requirements.txt")

    def test_dockerfile_uses_ffmpeg_and_excludes_git(self):
        df = (ROOT / "Dockerfile").read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertIn("ffmpeg", df)
        self.assertNotIn("apt-get install -y --no-install-recommends \\\n    git", df)

    def test_acoustid_lookup_client_and_control_agent(self):
        from backend.beets_client import BeetsClient, beets_client
        self.assertTrue(hasattr(beets_client, "acoustid_lookup"))

        success_body = json.dumps(
            {"ok": True, "status": "matched", "candidates": [{"score": 90, "title": "Test"}]}
        ).encode("utf-8")
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = success_body
        mock_resp.__enter__.return_value = mock_resp
        with mock.patch("backend.beets_client.urllib.request.urlopen", return_value=mock_resp):
            res = beets_client.acoustid_lookup("/data/torrents/music/test.flac")
            self.assertTrue(res.get("ok"))
            self.assertEqual(res.get("status"), "matched")
            self.assertEqual(len(res.get("candidates")), 1)

    def test_acoustid_lookup_client_preserves_structured_error_status(self):
        """acoustid_lookup() must not collapse a structured non-2xx response
        (e.g. 503 'unavailable' for a missing ACOUSTID_API_KEY) into a bare
        exception the way the shared _request() helper does for every other
        endpoint -- regression test for a real bug only found via live
        two-container testing: _request() treats any >=500 status as a
        generic BeetsUnavailableError, discarding the engine's actual
        {"status": "unavailable"} body."""
        from backend.beets_client import BeetsClient, beets_client, BeetsAuthError, BeetsUnavailableError

        error_body = json.dumps(
            {"ok": False, "status": "unavailable", "fingerprint_available": False,
             "error": "ACOUSTID_API_KEY environment variable not configured"}
        ).encode("utf-8")

        def raise_503(*args, **kwargs):
            resp = mock.MagicMock()
            resp.read.return_value = error_body
            raise urllib.error.HTTPError("http://beets:8338/audio/acoustid-lookup", 503, "Service Unavailable", {}, resp)

        with mock.patch("backend.beets_client.urllib.request.urlopen", side_effect=raise_503):
            res = beets_client.acoustid_lookup("/data/torrents/music/test.flac")
            self.assertFalse(res.get("ok"))
            self.assertEqual(res.get("status"), "unavailable")

        # A genuine 401 must still raise BeetsAuthError, not be swallowed.
        def raise_401(*args, **kwargs):
            resp = mock.MagicMock()
            resp.read.return_value = b"{}"
            raise urllib.error.HTTPError("http://beets:8338/audio/acoustid-lookup", 401, "Unauthorized", {}, resp)

        with mock.patch("backend.beets_client.urllib.request.urlopen", side_effect=raise_401):
            with self.assertRaises(BeetsAuthError):
                beets_client.acoustid_lookup("/data/torrents/music/test.flac")

        # A transport-level failure with no parseable body still reports
        # unavailable via an exception (no structured body to preserve).
        def raise_connection_error(*args, **kwargs):
            raise urllib.error.URLError("connection refused")

        with mock.patch("backend.beets_client.urllib.request.urlopen", side_effect=raise_connection_error):
            with self.assertRaises(BeetsUnavailableError):
                beets_client.acoustid_lookup("/data/torrents/music/test.flac")

    def test_acoustid_lookup_status_preserves_distinct_statuses(self):
        """helpers_mb._acoustid_lookup_status must not collapse unavailable,
        timeout, provider_error, and no_match into the same bare []."""
        import helpers_mb
        from backend.beets_client import beets_client

        cases = [
            {"ok": False, "status": "unavailable", "candidates": []},
            {"ok": False, "status": "timeout", "candidates": []},
            {"ok": False, "status": "provider_error", "candidates": []},
            {"ok": True, "status": "no_match", "candidates": []},
            {"ok": True, "status": "matched", "candidates": [{"score": 95, "mb_trackid": "abc"}]},
        ]
        for case in cases:
            with mock.patch.object(beets_client, "acoustid_lookup", return_value=case):
                result = helpers_mb._acoustid_lookup_status("/data/torrents/music/test.flac")
                self.assertEqual(result.get("status"), case["status"])
                self.assertEqual(result.get("candidates"), case["candidates"])

    def test_acoustid_lookup_status_survives_control_agent_exception(self):
        """A control-agent/network exception must return a status dict, not
        raise -- regression test for a NameError (missing `logging` import)
        that previously escaped this exact except-block uncaught."""
        import helpers_mb
        from backend.beets_client import beets_client

        with mock.patch.object(beets_client, "acoustid_lookup", side_effect=RuntimeError("boom")):
            result = helpers_mb._acoustid_lookup_status("/data/torrents/music/test.flac")
            self.assertFalse(result.get("ok"))
            self.assertEqual(result.get("status"), "analysis_error")
            self.assertEqual(helpers_mb._acoustid_lookup("/data/torrents/music/test.flac"), [])

    def test_acoustid_cache_does_not_persist_transient_failures(self):
        """A transient engine outage (unavailable/timeout/provider_error) must
        not be written to the never-expiring disk cache, or a temporary
        outage would become a permanent false no-match for that file."""
        import app as app_module

        with tempfile.TemporaryDirectory() as tmp_dir:
            audio_path = Path(tmp_dir) / "track.flac"
            audio_path.write_bytes(b"fake audio bytes")

            with mock.patch.object(app_module, "_ACOUSTID_FILE_CACHE_DIR", Path(tmp_dir) / "cache"), \
                 mock.patch.object(app_module, "_acoustid_lookup_status",
                                    return_value={"ok": False, "status": "unavailable", "candidates": []}) as mock_status:
                result = app_module._acoustid_lookup_cached(str(audio_path))
                self.assertEqual(result, [])
                cache_files = list((Path(tmp_dir) / "cache").rglob("*.json")) if (Path(tmp_dir) / "cache").exists() else []
                self.assertEqual(cache_files, [], "transient failure must not be written to the permanent disk cache")

                # A subsequent call must retry the engine rather than serve a
                # stale cached negative result.
                app_module._acoustid_lookup_cached(str(audio_path))
                self.assertEqual(mock_status.call_count, 2)

    def test_acoustid_cache_persists_terminal_outcomes(self):
        """A genuine matched/no_match/invalid_media result IS cached, since
        it is determined by the file's own content, not engine availability."""
        import app as app_module

        with tempfile.TemporaryDirectory() as tmp_dir:
            audio_path = Path(tmp_dir) / "track.flac"
            audio_path.write_bytes(b"fake audio bytes")

            with mock.patch.object(app_module, "_ACOUSTID_FILE_CACHE_DIR", Path(tmp_dir) / "cache"), \
                 mock.patch.object(app_module, "_acoustid_lookup_status",
                                    return_value={"ok": True, "status": "no_match", "candidates": []}) as mock_status:
                app_module._acoustid_lookup_cached(str(audio_path))
                app_module._acoustid_lookup_cached(str(audio_path))
                self.assertEqual(mock_status.call_count, 1, "a terminal outcome should be served from cache on the second call")


if __name__ == "__main__":
    unittest.main()
