"""ARCH-007 Milestone 1 Challenger 1 Test Suite: Raw SQL Rejection & Parameter Security.

Adversarially stress-tests Control Agent and BeetsClient on branch fix/arch007-structured-read-closure:
1. Arbitrary SQL injection payloads across all query parameters and JSON bodies.
2. Generic query endpoints rejection (/query, /sql, /library/raw_query, etc.).
3. Calling `raw_sqlite_query` through direct calls, reflection, cursor wrappers, and mocked bypasses.
4. Header injection and path traversal attempts across folder endpoints.
5. Injected WHERE fragments, table names, and column lists.
6. Empirical verification of database immutability and SQLite PRAGMA integrity.
"""

import http.client
import json
import os
import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import backend.beets_control_agent as agent
from backend.beets_client import (
    BeetsAuthError,
    BeetsBadRequestError,
    BeetsClient,
    BeetsError,
    BeetsNotFoundError,
    BeetsUnavailableError,
    RemoteSQLiteCursor,
)


def _get_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Arch007ChallengerSqlSecurityTests(unittest.TestCase):
    """Milestone 1 Challenger 1: Adversarial security verification suite."""

    @classmethod
    def setUpClass(cls):
        cls.token = "arch007_challenger1_secret_token_1234567890_strong"
        cls.token_patch = mock.patch.object(agent, "BEETS_API_TOKEN", cls.token)
        cls.token_patch.start()

        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmpdir.name) / "musiclibrary.blb"
        cls.lib_patch = mock.patch.object(agent, "LIB_PATH", str(cls.db_path))
        cls.lib_patch.start()

        cls.music_dir = Path(cls.tmpdir.name) / "music"
        cls.music_dir.mkdir(parents=True, exist_ok=True)
        cls.music_patch = mock.patch.object(agent, "MUSIC_LIBRARY_PATH", str(cls.music_dir))
        cls.music_patch.start()

        cls._init_db()

        cls.port = _get_free_port()
        cls.httpd = agent.ThreadingHTTPServer(("127.0.0.1", cls.port), agent.ControlAgentHandler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

        cls.env_patcher = mock.patch.dict(os.environ, {
            "BEETS_OUTBOUND_ALLOWLIST": f"127.0.0.1:{cls.port}",
        })
        cls.env_patcher.start()

        cls.client = BeetsClient(base_url=f"http://127.0.0.1:{cls.port}", token=cls.token)

    @classmethod
    def tearDownClass(cls):
        cls.env_patcher.stop()
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.music_patch.stop()
        cls.lib_patch.stop()
        cls.token_patch.stop()
        cls.tmpdir.cleanup()

    @classmethod
    def _init_db(cls):
        con = sqlite3.connect(cls.db_path)
        cur = con.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS albums ("
            "  id INTEGER PRIMARY KEY, "
            "  album TEXT, "
            "  albumartist TEXT, "
            "  albumartist_credit TEXT, "
            "  artist TEXT, "
            "  genre TEXT, "
            "  year INTEGER, "
            "  mb_albumid TEXT, "
            "  mb_releasegroupid TEXT, "
            "  mb_albumartistid TEXT"
            ")"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS items ("
            "  id INTEGER PRIMARY KEY, "
            "  album_id INTEGER, "
            "  path TEXT, "
            "  title TEXT, "
            "  artist TEXT, "
            "  album TEXT, "
            "  albumartist TEXT, "
            "  genre TEXT, "
            "  year INTEGER, "
            "  track INTEGER, "
            "  disc INTEGER, "
            "  length REAL, "
            "  format TEXT, "
            "  bitrate INTEGER, "
            "  samplerate INTEGER, "
            "  bitdepth INTEGER, "
            "  mb_trackid TEXT, "
            "  mb_albumid TEXT, "
            "  added REAL"
            ")"
        )

        # Baseline Album 1: Complete with MBIDs
        cur.execute(
            "INSERT INTO albums (id, album, albumartist, artist, genre, year, mb_albumid, mb_releasegroupid, mb_albumartistid) "
            "VALUES (1, 'Adversarial OK Computer', 'Radiohead', 'Radiohead', 'Rock', 1997, "
            "  '8b556b6b-d366-3d23-bf7b-232185a5a1f2', '3a0b3d68-07e0-3165-b1a8-8b01bb363a0b', 'a74b1b7f-71a5-4011-9441-d0b5e4122711')"
        )
        t1_path = cls.music_dir / "Radiohead" / "01 Airbag.flac"
        t1_path.parent.mkdir(parents=True, exist_ok=True)
        t1_path.write_bytes(b"flac test content")

        cur.execute(
            "INSERT INTO items (id, album_id, path, title, artist, album, albumartist, genre, year, track, disc, length, format, bitrate, samplerate, bitdepth, mb_trackid, mb_albumid, added) "
            "VALUES (101, 1, ?, 'Airbag', 'Radiohead', 'Adversarial OK Computer', 'Radiohead', 'Rock', 1997, 1, 1, 284.0, 'FLAC', 850000, 44100, 16, "
            "  '4b1a134a-921c-4b6e-8212-8414cb2f57b8', '8b556b6b-d366-3d23-bf7b-232185a5a1f2', 1710000001.0)",
            (str(t1_path),)
        )

        # Baseline Album 2: Incomplete (missing mb_albumid)
        cur.execute(
            "INSERT INTO albums (id, album, albumartist, artist, genre, year, mb_albumid, mb_releasegroupid, mb_albumartistid) "
            "VALUES (2, 'Unmatched Demo Album', 'Unknown Band', 'Unknown Band', '', 2021, '', '', '')"
        )
        cur.execute(
            "INSERT INTO items (id, album_id, path, title, artist, album, albumartist, genre, year, track, disc, length, format, bitrate, samplerate, bitdepth, mb_trackid, mb_albumid, added) "
            "VALUES (201, 2, '/missing/demo/track1.mp3', 'Demo Track 1', 'Unknown Band', 'Unmatched Demo Album', 'Unknown Band', '', 2021, 1, 1, 180.0, 'MP3', 320000, 44100, 16, '', '', 1710000003.0)"
        )

        con.commit()
        con.close()

    def _get_counts(self):
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM albums")
        ac = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM items")
        ic = cur.fetchone()[0]
        con.close()
        return ac, ic

    def _http_raw(self, method: str, path: str, body: bytes = b"", headers: dict = None):
        h = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        if headers:
            h.update(headers)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(data.decode("utf-8"))
        except Exception:
            return resp.status, data.decode("utf-8")

    # ═════════════════════════════════════════════════════════════════════════════
    # 1. Arbitrary SQL Injections via Query Parameters & Request Bodies
    # ═════════════════════════════════════════════════════════════════════════════

    def test_sqli_payloads_in_query_parameters_strictly_rejected_or_safely_bound(self):
        """Stress-test SQL injection payloads across numeric and query parameters."""
        initial_ac, initial_ic = self._get_counts()

        sqli_numeric_targets = [
            "/review/queue/unmatched?limit={p}",
            "/review/queue/unmatched?offset={p}",
            "/stats/genres?missing_limit={p}",
            "/clean/rgid-groups?limit={p}",
            "/clean/rgid-groups?offset={p}",
            "/clean/rgid-groups?min_albums={p}",
            "/library/mbid-sticking/candidates?limit={p}",
            "/library/mbid-sticking/candidates?offset={p}",
            "/albums/{p}/mb-completeness",
            "/library/format-upgrades?limit={p}",
            "/library/format-upgrades?offset={p}",
            "/library/recording-replacements?mb_trackid=11111111-1111-1111-1111-111111111111&exclude_item_id={p}",
            "/library/recording-replacements?mb_trackid=11111111-1111-1111-1111-111111111111&limit={p}",
        ]

        numeric_payloads = [
            "' OR 1=1 --",
            "1; DROP TABLE items; --",
            "1 UNION SELECT 1,2,3",
            "1 OR 1=1",
            "0 OR 1=1",
            "-1 OR 1=1",
            "1' AND '1'='1",
            "0x31",
            "1\x00",
        ]

        for target in sqli_numeric_targets:
            for p in numeric_payloads:
                url = target.format(p=urllib.parse.quote(p))
                with self.subTest(url=url):
                    status, body = self._http_raw("GET", url)
                    self.assertEqual(status, 400, f"Expected 400 for {url}, got {status}: {body}")
                    self.assertIsInstance(body, dict)
                    self.assertFalse(body.get("ok", True))

        # Check DB integrity & immutability
        ac, ic = self._get_counts()
        self.assertEqual(ac, initial_ac)
        self.assertEqual(ic, initial_ic)

    def test_sqli_payloads_in_string_parameters_safely_bound_or_rejected(self):
        """Stress-test SQL injection payloads in string parameters (modes, UUIDs, filters)."""
        string_targets = [
            ("/clean/rgid-groups/{p}", 400),
            ("/library/mbid-sticking/candidates?mode={p}", 400),
            ("/library/format-upgrades?format={p}", 400),
            ("/library/recording-replacements?mb_trackid={p}", 400),
        ]

        string_payloads = [
            "' OR '1'='1",
            "'; DROP TABLE items; --",
            "' UNION SELECT sqlite_version(), 2, 3 --",
            "\" OR \"\"=\"",
            "admin'--",
            "mode'/*comment*/",
            "3a0b3d68-07e0-3165-b1a8-8b01bb363a0b' OR 1=1--",
        ]

        for target, expected_code in string_targets:
            for p in string_payloads:
                url = target.format(p=urllib.parse.quote(p))
                with self.subTest(url=url):
                    status, body = self._http_raw("GET", url)
                    self.assertEqual(status, expected_code, f"Expected {expected_code} for {url}, got {status}: {body}")

    def test_sqli_in_post_bodies_strictly_rejected(self):
        """Stress-test SQL injection payloads inside POST request JSON bodies."""
        post_targets = [
            ("/clean/rgid-groups/merge", {"target_album_id": "1 OR 1=1", "source_album_ids": [2]}),
            ("/clean/rgid-groups/merge", {"target_album_id": 1, "source_album_ids": ["2; DROP TABLE items;"]}),
            ("/clean/rgid-groups/merge", {"target_album_id": 1, "source_album_ids": [2], "mb_releasegroupid": "' OR 1=1 --"}),
            ("/clean/orphaned-items", {"item_ids": ["1 OR 1=1"]}),
            ("/clean/orphaned-items", {"item_ids": [-1]}),
            ("/clean/empty-albums", {"album_ids": ["1; DROP TABLE albums;"]}),
            ("/clean/empty-albums", {"album_ids": [0]}),
            ("/library/sync-deleted", {"limit": "100 OR 1=1"}),
            ("/library/albums/merge", {"target_album_id": "1 OR 1=1", "source_album_id": 2}),
            ("/library/albums/merge", {"target_album_id": 1, "source_album_id": "2; DROP TABLE albums;"}),
            ("/maintenance/artist-folders/stamp-mbids", {"mbid": "' OR 1=1 --"}),
        ]

        for path, body in post_targets:
            with self.subTest(path=path, body=body):
                status, resp = self._http_raw("POST", path, json.dumps(body).encode("utf-8"))
                self.assertEqual(status, 400, f"Expected 400 for POST {path}, got {status}: {resp}")

    def test_find_hardlink_candidates_search_sqli_safely_bound(self):
        """Search parameters in find-hardlink-candidates are safely bound with LIKE ? ESCAPE '\\'."""
        sqli_searches = [
            {"filename": "' OR 1=1 --"},
            {"title": "Airbag' UNION SELECT 1,2,3,4,5,6,7 --"},
            {"artist": "Radiohead'; DROP TABLE items; --"},
            {"filename": "track.mp3", "album": "' OR 'a'='a"},
        ]

        for payload in sqli_searches:
            with self.subTest(payload=payload):
                status, resp = self._http_raw("POST", "/library/find-hardlink-candidates", json.dumps(payload).encode("utf-8"))
                self.assertEqual(status, 200)
                self.assertTrue(resp.get("ok"))
                # Must treat payload as literal text and return 0 results
                self.assertEqual(resp.get("candidates"), [])
                self.assertEqual(resp.get("count"), 0)

    # ═════════════════════════════════════════════════════════════════════════════
    # 2. Generic Query Endpoints Rejection
    # ═════════════════════════════════════════════════════════════════════════════

    def test_generic_query_endpoints_strictly_rejected(self):
        """Verify Control Agent does not expose generic query endpoints under any HTTP method."""
        generic_endpoints = [
            ("POST", "/library/raw_query", 403),
            ("GET", "/library/raw_query", 404),
            ("PUT", "/library/raw_query", 404),
            ("DELETE", "/library/raw_query", 404),
            ("POST", "/query", 404),
            ("POST", "/sql", 404),
            ("POST", "/raw_query", 404),
            ("POST", "/db/query", 404),
            ("POST", "/database/query", 404),
            ("POST", "/library/query", 404),
            ("POST", "/api/query", 404),
            ("POST", "/api/sql", 404),
            ("POST", "/sqlite", 404),
            ("POST", "/execute", 404),
            ("POST", "/library/raw-query", 404),
            ("POST", "/library/rawsql", 404),
            ("POST", "/admin/query", 404),
            ("POST", "/admin/sql", 404),
            ("POST", "/library/execute-sql", 404),
            ("GET", "/db/raw", 404),
        ]

        for method, ep, expected_code in generic_endpoints:
            with self.subTest(method=method, endpoint=ep):
                status, body = self._http_raw(method, ep, b'{"query": "SELECT * FROM items"}')
                # 403 (for raw_query), 404 (Not Found), 405 (Method Not Allowed), 501 (Not Implemented)
                self.assertIn(status, [expected_code, 403, 404, 405, 501])
                if ep == "/library/raw_query" and method == "POST":
                    self.assertEqual(status, 403)
                    self.assertEqual(body.get("error"), "Raw SQL queries are not permitted")

    # ═════════════════════════════════════════════════════════════════════════════
    # 3. Calling `raw_sqlite_query` through Reflection, Mocking, or Direct Calls
    # ═════════════════════════════════════════════════════════════════════════════

    def test_raw_sqlite_query_direct_call_raises_locally(self):
        """Direct invocation of BeetsClient.raw_sqlite_query() raises BeetsError locally."""
        with self.assertRaises(BeetsError) as ctx:
            self.client.raw_sqlite_query("SELECT 1")
        self.assertIn("Raw SQLite queries are not permitted", str(ctx.exception))

    def test_raw_sqlite_query_via_reflection_raises_locally(self):
        """Calling raw_sqlite_query via getattr or __getattribute__ raises BeetsError locally."""
        fn = getattr(self.client, "raw_sqlite_query")
        with self.assertRaises(BeetsError) as ctx:
            fn("SELECT * FROM items")
        self.assertIn("Raw SQLite queries are not permitted", str(ctx.exception))

        fn2 = self.client.__getattribute__("raw_sqlite_query")
        with self.assertRaises(BeetsError) as ctx:
            fn2("DROP TABLE albums")
        self.assertIn("Raw SQLite queries are not permitted", str(ctx.exception))

    def test_remote_sqlite_cursor_execute_raises_locally(self):
        """RemoteSQLiteCursor.execute() calls raw_sqlite_query and raises BeetsError."""
        cursor = RemoteSQLiteCursor(self.client)
        with self.assertRaises(BeetsError) as ctx:
            cursor.execute("SELECT 1")
        self.assertIn("Raw SQLite queries are not permitted", str(ctx.exception))

    def test_bypassing_client_to_raw_query_fails_with_403(self):
        """Attempting to bypass client-side check by directly issuing _request() to /library/raw_query fails with 403."""
        with self.assertRaises(BeetsError) as ctx:
            self.client._request("POST", "/library/raw_query", {"query": "SELECT 1"})
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("Raw SQL queries are not permitted", str(ctx.exception))

    def test_no_beets_client_method_calls_raw_sqlite_query(self):
        """Verify that no BeetsClient semantic method delegates to raw_sqlite_query."""
        with mock.patch.object(self.client, "raw_sqlite_query", side_effect=RuntimeError("raw_sqlite_query called!")):
            # Methods should not trigger raw_sqlite_query
            try:
                self.client.get_library_stats()
            except RuntimeError:
                self.fail("get_library_stats called raw_sqlite_query!")
            except Exception:
                pass  # Network or other errors are fine, but raw_sqlite_query must not be called

    # ═════════════════════════════════════════════════════════════════════════════
    # 4. Header Injection & Path Traversal in Folder Endpoints
    # ═════════════════════════════════════════════════════════════════════════════

    def test_header_injection_and_unauthorized_access(self):
        """Control Agent rejects requests with invalid or missing authorization."""
        # Missing auth header
        status, body = self._http_raw("GET", "/stats/library", headers={"Authorization": ""})
        self.assertEqual(status, 401)

        # Invalid token
        status, body = self._http_raw("GET", "/stats/library", headers={"Authorization": "Bearer bad_token_12345"})
        self.assertEqual(status, 401)

        # Basic auth instead of bearer/X-Beets-API-Token
        status, body = self._http_raw("GET", "/stats/library", headers={"Authorization": "Basic dXNlcjpwYXNz"})
        self.assertEqual(status, 401)

    def test_path_traversal_in_resolve_folder(self):
        """POST /library/resolve-folder handles path traversal attempts safely."""
        traversal_paths = [
            "/data/media/music/../../../../etc/passwd",
            "../../../../etc",
            "/etc",
            "/data/media/music/../../../windows/system32",
        ]
        for p in traversal_paths:
            with self.subTest(folder_path=p):
                status, body = self._http_raw("POST", "/library/resolve-folder", json.dumps({"folder_path": p}).encode("utf-8"))
                # Either safely rejected (400) or safely bound returning empty results without leaking files
                self.assertIn(status, [200, 400])
                if status == 200:
                    self.assertEqual(body.get("album_ids"), [])
                    self.assertEqual(body.get("item_ids"), [])
                    self.assertEqual(body.get("track_count"), 0)

    def test_null_byte_in_resolve_folder_rejected(self):
        """POST /library/resolve-folder strictly rejects null bytes in folder_path with 400."""
        status, body = self._http_raw("POST", "/library/resolve-folder", json.dumps({"folder_path": "/data/media/music/\x00/evil"}).encode("utf-8"))
        self.assertEqual(status, 400)
        self.assertEqual(body.get("error_code"), "INVALID_PATH")

    # ── Adversarial Findings / Challenge Assertions ─────────────────────────────

    def test_CHALLENGE_stamp_mbids_fails_to_confine_to_music_library_path(self):
        """ADVERSARIAL CHALLENGE: POST /maintenance/artist-folders/stamp-mbids allows writing
        .artist_mbids.json outside MUSIC_LIBRARY_PATH when caller supplies a traversal folder_path.
        
        Expected secure behavior: HTTP 400 or 403 rejecting paths outside MUSIC_LIBRARY_PATH.
        Actual vulnerable behavior: HTTP 200 writing files anywhere on the container filesystem.
        """
        outside_test_dir = Path(self.tmpdir.name) / "outside_artist_folder"
        outside_test_dir.mkdir(parents=True, exist_ok=True)
        stamp_target = outside_test_dir / ".artist_mbids.json"

        # Attempt to stamp an artist folder outside MUSIC_LIBRARY_PATH
        status, body = self._http_raw("POST", "/maintenance/artist-folders/stamp-mbids", json.dumps({
            "folder_path": str(outside_test_dir),
            "mbid": "99999999-9999-9999-9999-999999999999",
            "dry_run": False,
        }).encode("utf-8"))

        # If secure, this must be rejected with 400 or 403, and the file must NOT exist.
        # This test asserts secure behavior and will FAIL if the vulnerability exists.
        self.assertIn(status, [400, 403], f"VULNERABILITY: stamp-mbids accepted path outside MUSIC_LIBRARY_PATH with HTTP {status}: {body}")
        self.assertFalse(stamp_target.exists(), "VULNERABILITY: .artist_mbids.json was written outside MUSIC_LIBRARY_PATH!")

    def test_CHALLENGE_album_mb_completeness_improper_path_segment_handling(self):
        """ADVERSARIAL CHALLENGE: GET /albums/<aid>/mb-completeness does not strictly enforce
        the 4-segment path schema (/albums/<int:aid>/mb-completeness), allowing injected path
        traversal or comment segments between the album ID and endpoint suffix.
        
        Expected secure behavior: HTTP 400 or 404 for invalid path schema.
        Actual vulnerable behavior: HTTP 200 treating injected segment as part of the route.
        """
        injected_path = "/albums/1/injected_segment/mb-completeness"
        status, body = self._http_raw("GET", injected_path)
        self.assertIn(status, [400, 404], f"VULNERABILITY: /albums/<aid>/mb-completeness allowed injected segments with HTTP {status}: {body}")

    def test_CHALLENGE_beets_client_maps_403_to_beets_auth_error(self):
        """ADVERSARIAL CHALLENGE: Verify Worker claim that HTTP 403 is mapped to BeetsAuthError.
        
        Worker handoff (section 1.3, line 49) states:
        'HTTP 401 / 403 -> BeetsAuthError (status_code=401/403)'
        Actual implementation in backend/beets_client.py lines 178-206:
        Only checks `if exc.code == 401: raise BeetsAuthError`. 403 falls through to base BeetsError.
        """
        with self.assertRaises(BeetsAuthError, msg="CLAIM FAILURE: HTTP 403 did not raise BeetsAuthError"):
            self.client._request("POST", "/library/raw_query", {"query": "SELECT 1"})

    # ═════════════════════════════════════════════════════════════════════════════
    # 5. Injected WHERE Fragments and Table Names
    # ═════════════════════════════════════════════════════════════════════════════

    def test_table_and_column_injection_ignored_or_rejected(self):
        """Endpoints ignore caller-controlled table and column injection parameters."""
        status, body = self._http_raw("GET", "/items?table=sqlite_master")
        self.assertEqual(status, 200)
        for it in body.get("items", []):
            self.assertIn("title", it)
            self.assertNotIn("sql", it)

        status, body = self._http_raw("GET", "/albums?table=sqlite_master&columns=id,sql")
        self.assertEqual(status, 200)
        for alb in body.get("albums", []):
            self.assertIn("album", alb)
            self.assertNotIn("sql", alb)

    def test_where_fragment_injection_ignored(self):
        """Endpoints ignore caller-supplied WHERE fragment parameters."""
        status, body = self._http_raw("GET", "/items?where=1=1")
        self.assertEqual(status, 200)

        status, body = self._http_raw("GET", "/albums?where=1=1")
        self.assertEqual(status, 200)

    def test_patch_endpoints_strictly_reject_sql_injected_fields(self):
        """PATCH /items/<id> strictly validates field names against allowlist."""
        malicious_field_keys = [
            "title = 'x' WHERE 1=1; --",
            "1=1",
            "nonexistent_col",
            "mb_trackid",  # protected identity field
            "id",          # protected primary key
            "path",        # protected file path
        ]

        for key in malicious_field_keys:
            with self.subTest(field_key=key):
                status, body = self._http_raw("PATCH", "/items/101", json.dumps({"fields": {key: "injected"}}).encode("utf-8"))
                self.assertIn(status, [400, 403])

    # ═════════════════════════════════════════════════════════════════════════════
    # 6. Empirical Database Integrity Verification
    # ═════════════════════════════════════════════════════════════════════════════

    def test_sqlite_pragmas_and_row_integrity_post_challenge(self):
        """Verify PRAGMA integrity_check and strict row immutability after all attacks."""
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("PRAGMA integrity_check")
        rows = cur.fetchall()
        self.assertEqual(rows, [("ok",)], f"SQLite integrity compromised: {rows}")

        # Assert baseline rows remain intact
        cur.execute("SELECT id, album, albumartist, mb_albumid FROM albums WHERE id=1")
        alb = cur.fetchone()
        self.assertEqual(alb[0], 1)
        self.assertEqual(alb[1], "Adversarial OK Computer")
        self.assertEqual(alb[2], "Radiohead")
        self.assertEqual(alb[3], "8b556b6b-d366-3d23-bf7b-232185a5a1f2")

        cur.execute("SELECT id, album_id, title, format, mb_trackid FROM items WHERE id=101")
        it = cur.fetchone()
        self.assertEqual(it[0], 101)
        self.assertEqual(it[1], 1)
        self.assertEqual(it[2], "Airbag")
        self.assertEqual(it[3], "FLAC")
        self.assertEqual(it[4], "4b1a134a-921c-4b6e-8212-8414cb2f57b8")

        con.close()


if __name__ == "__main__":
    unittest.main()
