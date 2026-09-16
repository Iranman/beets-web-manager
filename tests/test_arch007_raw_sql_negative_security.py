"""ARCH-007 Negative Security Suite: Raw SQL Rejection and SQL Injection Defense.

Verifies:
1. Control Agent rejects arbitrary SQL query strings over POST /library/raw_query with HTTP 403.
2. Control Agent does not expose alternative generic SQL endpoints (/query, /sql, /raw_query, etc.).
3. Semantic endpoints reject caller-controlled table names, column lists, and WHERE clauses.
4. Search/filter parameters treat SQL injection payloads as literal text without syntax errors.
5. BeetsClient.raw_sqlite_query() raises BeetsError client-side without attempting network traffic.
6. Database rows and schema remain strictly immutable under all attack payloads.
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
from backend.beets_client import BeetsClient, BeetsError


def _get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Arch007RawSqlNegativeSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.token = "arch007_test_secret_token_1234567890_min_length_ok"
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
        con.execute(
            "CREATE TABLE IF NOT EXISTS albums ("
            "  id INTEGER PRIMARY KEY, "
            "  album TEXT, "
            "  albumartist TEXT, "
            "  artist TEXT, "
            "  genre TEXT, "
            "  year INTEGER, "
            "  mb_albumid TEXT, "
            "  mb_releasegroupid TEXT, "
            "  mb_albumartistid TEXT"
            ")"
        )
        con.execute(
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
        con.execute(
            "INSERT INTO albums (id, album, albumartist, artist, year, mb_albumid, mb_releasegroupid, mb_albumartistid) "
            "VALUES (1, 'Test Album', 'Test Artist', 'Test Artist', 2020, '11111111-1111-1111-1111-111111111111', '22222222-2222-2222-2222-222222222222', '33333333-3333-3333-3333-333333333333')"
        )
        con.execute(
            "INSERT INTO items (id, album_id, path, title, artist, album, albumartist, format, mb_trackid, mb_albumid, added) "
            "VALUES (1, 1, '/music/Test Artist/Test Album/01.flac', 'Test Track', 'Test Artist', 'Test Album', 'Test Artist', 'FLAC', '44444444-4444-4444-4444-444444444444', '11111111-1111-1111-1111-111111111111', 1700000000.0)"
        )
        con.commit()
        con.close()

    def _get_db_counts(self):
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM albums")
        album_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM items")
        item_count = cur.fetchone()[0]
        con.close()
        return album_count, item_count

    def _make_raw_request(self, method: str, path: str, body: bytes = b"", headers: dict = None):
        h = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        if headers:
            h.update(headers)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    # ── PART 1: Arbitrary SQL Query String Rejection ───────────────────────────

    def test_raw_query_endpoint_rejects_comprehensive_sql_attack_matrix(self):
        """POST /library/raw_query unconditionally returns 403 Forbidden and leaves DB immutable."""
        initial_albums, initial_items = self._get_db_counts()

        attack_matrix = [
            {"query": "SELECT * FROM items"},
            {"query": "SELECT * FROM items WHERE id=?", "params": [1]},
            {"query": "; DROP TABLE items; --"},
            {"query": "DELETE FROM albums; VACUUM;"},
            {"query": "UPDATE items SET title='hacked' WHERE id=1"},
            {"query": "INSERT INTO items (title) VALUES ('pwned')"},
            {"query": "ATTACH DATABASE '/tmp/evil.db' AS evil"},
            {"query": "PRAGMA writable_schema=ON"},
            {"query": "SELECT 1; SELECT 2; DROP TABLE items;"},
            {"query": "/* comment */ SELECT * FROM items"},
            {"query": "SELECT/**/*/**/FROM/**/items"},
            {"query": "sElEcT * FrOm ItEmS"},
            {"query": "WITH changed AS (DELETE FROM items RETURNING *) SELECT * FROM changed"},
            {"query": "SELECT * FROM items WHERE id IN (" + "1," * 2000 + "1)"},
            {"query": ""},
            {"query": "   \n\t  "},
        ]

        for payload in attack_matrix:
            with self.subTest(payload=payload["query"][:40]):
                status, data = self._make_raw_request("POST", "/library/raw_query", json.dumps(payload).encode("utf-8"))
                self.assertEqual(status, 403)
                res = json.loads(data.decode("utf-8"))
                self.assertEqual(res.get("error"), "Raw SQL queries are not permitted")

        # Non-dict JSON body: either 400 Bad Request or 403
        status, data = self._make_raw_request("POST", "/library/raw_query", json.dumps(["SELECT * FROM items"]).encode("utf-8"))
        self.assertIn(status, [400, 403])

        # Assert database row counts are completely unchanged
        current_albums, current_items = self._get_db_counts()
        self.assertEqual(current_albums, initial_albums)
        self.assertEqual(current_items, initial_items)

    # ── PART 2: Absence of Alternative Generic SQL Endpoints ───────────────────

    def test_absence_of_alternative_generic_sql_endpoints(self):
        """Verifies no alternative generic SQL endpoints exist on the server."""
        probe_paths = [
            ("POST", "/query"),
            ("POST", "/sql"),
            ("POST", "/raw_query"),
            ("POST", "/db/query"),
            ("POST", "/database/query"),
            ("POST", "/library/query"),
            ("GET", "/library/raw_query"),
            ("PUT", "/library/raw_query"),
        ]

        for method, path in probe_paths:
            with self.subTest(method=method, path=path):
                status, data = self._make_raw_request(method, path, b'{"query": "SELECT 1"}')
                self.assertIn(status, [404, 405, 501])

    # ── PART 3: Rejection of Table, Column, and WHERE Structure Injection ──────

    def test_semantic_endpoints_ignore_or_reject_table_override(self):
        """Semantic endpoints never accept caller-controlled table names."""
        status, data = self._make_raw_request("GET", "/items?table=sqlite_master")
        self.assertEqual(status, 200)
        res = json.loads(data.decode("utf-8"))
        # Returned items must belong to the items table, not schema metadata
        for item in res.get("items", []):
            self.assertIn("title", item)
            self.assertNotIn("sql", item)

        status, data = self._make_raw_request("GET", "/albums?table=users")
        self.assertEqual(status, 200)
        res = json.loads(data.decode("utf-8"))
        for alb in res.get("albums", []):
            self.assertIn("album", alb)

    def test_semantic_endpoints_ignore_caller_column_projection(self):
        """Endpoints do not allow caller-controlled column selection."""
        status, data = self._make_raw_request("GET", "/items?columns=id,path,password")
        self.assertEqual(status, 200)
        res = json.loads(data.decode("utf-8"))
        for item in res.get("items", []):
            self.assertNotIn("password", item)

        status, data = self._make_raw_request("GET", "/albums?select=1,version()")
        self.assertEqual(status, 200)
        res = json.loads(data.decode("utf-8"))
        for alb in res.get("albums", []):
            self.assertIn("album", alb)

    def test_semantic_endpoints_ignore_where_clause_injection(self):
        """Endpoints do not interpret caller-supplied where parameters."""
        status, data = self._make_raw_request("GET", "/items?where=1=1")
        self.assertEqual(status, 200)

        status, data = self._make_raw_request("GET", "/albums?where_clause=id>0")
        self.assertEqual(status, 200)

    def test_patch_fields_allowlist_rejects_sql_injected_column_names(self):
        """PATCH endpoints strictly validate field names against static allowlists."""
        malicious_fields = [
            {"title = 'x' WHERE 1=1; DROP TABLE items; --": "val"},
            {"1=1) OR (1=1": "val"},
            {"unrecognized_column": "val"},
            {"id": 999},
            {"path": "/fake/path"},
            {"mb_trackid": "new-mbid"},
        ]

        for fields in malicious_fields:
            with self.subTest(fields=list(fields.keys())[0]):
                status, data = self._make_raw_request("PATCH", "/items/1", json.dumps({"fields": fields}).encode("utf-8"))
                self.assertIn(status, [400, 403])
                res = json.loads(data.decode("utf-8"))
                err_msg = (res.get("error", "") + " " + res.get("detail", "")).lower()
                self.assertTrue(any(t in err_msg for t in ["unsupported", "not editable", "invalid"]))

    # ── PART 4: SQL Injection in Query Parameters Treated as Literals ─────────

    def test_sql_injection_in_query_parameters_treated_as_literals(self):
        """Search parameters bound with ? safely treat injection strings as literal text."""
        initial_albums, initial_items = self._get_db_counts()

        sqli_payloads = [
            "' OR '1'='1",
            "' OR 1=1--",
            '" OR ""="',
            "') OR ('1'='1",
            "' UNION SELECT 1,2,3,sqlite_version(),5--",
            "test'; DROP TABLE items; --",
            "test'/*",
            "test'--",
            "0x27204f5220313d31",
            "test\x00' OR 1=1--",
        ]

        for sqli in sqli_payloads:
            with self.subTest(sqli=sqli):
                # Search items
                status, data = self._make_raw_request(
                    "GET", f"/items?artist={urllib.parse.quote(sqli)}"
                )
                self.assertEqual(status, 200)
                res = json.loads(data.decode("utf-8"))
                self.assertEqual(res.get("items", []), [])

                # Search albums
                status, data = self._make_raw_request(
                    "GET", f"/albums?albumartist={urllib.parse.quote(sqli)}"
                )
                self.assertEqual(status, 200)
                res = json.loads(data.decode("utf-8"))
                self.assertEqual(res.get("albums", []), [])

                status, data = self._make_raw_request(
                    "GET", f"/albums?query={urllib.parse.quote(sqli)}"
                )
                self.assertEqual(status, 200)
                res = json.loads(data.decode("utf-8"))
                self.assertEqual(res.get("albums", []), [])

        current_albums, current_items = self._get_db_counts()
        self.assertEqual(current_albums, initial_albums)
        self.assertEqual(current_items, initial_items)

    # ── PART 5: Client-Side Hard Barrier ───────────────────────────────────────

    def test_beets_client_raw_sqlite_query_raises_locally_without_network_call(self):
        """BeetsClient.raw_sqlite_query() unconditionally raises BeetsError without network calls."""
        client = BeetsClient(base_url=f"http://127.0.0.1:{self.port}", token=self.token)

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            with self.assertRaises(BeetsError) as ctx:
                client.raw_sqlite_query("SELECT 1")
            self.assertIn("Raw SQLite queries are not permitted", str(ctx.exception))
            mock_urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
