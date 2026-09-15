"""ARCH-007 (Wave 34): real, structured, engine-side read endpoints that
replace app.py's raw `_db()` SELECTs in library_merge_artist(),
library_normalize_artists(), _run_normalize_artists_if_needed(),
library_mbsync_all(), and library_move_all(). `_db()` always routes
through backend.beets_client.raw_sqlite_query(), which is a hard,
unconditional `raise BeetsError(...)` -- a deliberate architectural
boundary against caller-supplied SQL, not a degraded compatibility path --
so these five callers were completely non-functional for their read step
in the real two-service topology (confirmed by real Docker acceptance
testing, not theoretical). This adds no general query surface: every new
endpoint is a fixed, server-owned WHERE clause, narrow and workflow-
specific to exactly the data these callers need.

Two layers of proof, mirroring tests/test_items_pagination_engine_side.py:
1. ControlAgentEndpointLiveTests spins up the real ControlAgentHandler
   over a real HTTP socket against a temp SQLite library and hits the new
   endpoints directly with http.client -- proves the engine's own SQL
   shape and response contract.
2. BeetsClientRealIPCTests does the same but through the real
   backend.beets_client.BeetsClient methods (find_all_albums_by_
   albumartist / find_all_orphan_albums / list_distinct_albumartists /
   list_distinct_item_paths) -- proves the full client -> HTTP -> engine
   -> SQLite stack app.py's routes actually call, not just the engine
   half in isolation.
"""

import http.client
import http.server
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backend.beets_control_agent as agent  # noqa: E402
from backend.beets_client import BeetsClient, BeetsError, BeetsUnavailableError  # noqa: E402


def _get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ControlAgentEndpointLiveTests(unittest.TestCase):
    """Spins up the real ControlAgentHandler over HTTP against a temp
    SQLite library, the same pattern test_items_pagination_engine_side.py
    already established for /items."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(cls.tmpdir.name) / "musiclibrary.blb"
        con = sqlite3.connect(db_path)
        con.execute(
            "CREATE TABLE albums (id INTEGER PRIMARY KEY, album TEXT, albumartist TEXT, artist TEXT)"
        )
        con.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, path TEXT)"
        )
        # Album 1: "Bob" -- has items (not orphaned).
        con.execute("INSERT INTO albums (id, album, albumartist, artist) VALUES (1, 'Album Bob', 'Bob', 'Bob')")
        con.execute("INSERT INTO items (id, album_id, path) VALUES (1, 1, '/music/Bob/track1.flac')")
        # Album 2: "Bobby" -- a substring superset of "Bob"; must NOT match
        # an exact-match query for "Bob". Also has items.
        con.execute("INSERT INTO albums (id, album, albumartist, artist) VALUES (2, 'Album Bobby', 'Bobby', 'Bobby')")
        con.execute("INSERT INTO items (id, album_id, path) VALUES (2, 2, '/music/Bobby/track1.flac')")
        # Album 3: "Bob" again -- a second album under the same exact
        # albumartist, to prove multi-row exact match works.
        con.execute("INSERT INTO albums (id, album, albumartist, artist) VALUES (3, 'Album Bob 2', 'Bob', 'Bob')")
        con.execute("INSERT INTO items (id, album_id, path) VALUES (3, 3, '/music/Bob/track2.flac')")
        # Album 4: orphaned -- zero item rows reference it.
        con.execute("INSERT INTO albums (id, album, albumartist, artist) VALUES (4, 'Ghost Album', 'Ghost Artist', 'Ghost Artist')")
        # A bytes-typed path column value, matching real Beets DB storage.
        con.execute("INSERT INTO items (id, album_id, path) VALUES (5, 1, ?)", (b"/music/Bob/track3.flac",))
        con.commit()
        con.close()

        cls.token = "b" * 40
        cls.lib_patch = mock.patch.object(agent, "LIB_PATH", str(db_path))
        cls.token_patch = mock.patch.object(agent, "BEETS_API_TOKEN", cls.token)
        cls.lib_patch.start()
        cls.token_patch.start()

        cls.httpd = http.server.HTTPServer(("127.0.0.1", 0), agent.ControlAgentHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.lib_patch.stop()
        cls.token_patch.stop()
        cls.tmpdir.cleanup()

    def _get(self, path):
        # http.client, not urllib.request -- see
        # test_items_pagination_engine_side.py's own docstring note on why
        # (backend.security's outbound SSRF allowlist patches
        # urllib.request.urlopen process-wide once app.py is imported).
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path, headers={"Authorization": f"Bearer {self.token}"})
            resp = conn.getresponse()
            body = resp.read()
        finally:
            conn.close()
        return resp.status, json.loads(body)

    def test_albums_albumartist_exact_match_excludes_substring_superset(self):
        status, data = self._get("/albums?albumartist=Bob")
        self.assertEqual(status, 200)
        ids = sorted(a["id"] for a in data["albums"])
        self.assertEqual(ids, [1, 3])
        self.assertEqual(data["total"], 2)

    def test_albums_albumartist_exact_match_for_the_superset_name_itself(self):
        status, data = self._get("/albums?albumartist=Bobby")
        self.assertEqual(status, 200)
        ids = [a["id"] for a in data["albums"]]
        self.assertEqual(ids, [2])

    def test_albums_albumartist_no_match_returns_empty_not_error(self):
        status, data = self._get("/albums?albumartist=Nobody")
        self.assertEqual(status, 200)
        self.assertEqual(data["albums"], [])
        self.assertEqual(data["total"], 0)

    def test_albums_orphan_true_returns_only_the_zero_item_album(self):
        status, data = self._get("/albums?orphan=true")
        self.assertEqual(status, 200)
        ids = [a["id"] for a in data["albums"]]
        self.assertEqual(ids, [4])

    def test_albums_orphan_false_returns_only_albums_with_items(self):
        status, data = self._get("/albums?orphan=false")
        self.assertEqual(status, 200)
        ids = sorted(a["id"] for a in data["albums"])
        self.assertEqual(ids, [1, 2, 3])

    def test_albums_orphan_invalid_value_is_a_400_not_a_500(self):
        status, data = self._get("/albums?orphan=maybe")
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_library_albumartists_returns_distinct_non_empty_values(self):
        status, data = self._get("/library/albumartists")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(data["albumartists"]), ["Bob", "Bobby", "Ghost Artist"])
        self.assertEqual(data["count"], 3)

    def test_library_item_paths_returns_distinct_paths_decoded_to_str(self):
        status, data = self._get("/library/item-paths")
        self.assertEqual(status, 200)
        paths = sorted(data["paths"])
        self.assertEqual(
            paths,
            sorted([
                "/music/Bob/track1.flac",
                "/music/Bobby/track1.flac",
                "/music/Bob/track2.flac",
                "/music/Bob/track3.flac",
            ]),
        )
        # The bytes-typed row (item id=5) must come back as a plain str,
        # not a repr of a bytes object.
        self.assertTrue(all(isinstance(p, str) for p in paths))

    def test_library_item_paths_exceeding_response_cap_is_413_not_silent_truncation(self):
        with mock.patch.object(agent, "MAX_ITEM_PATHS_RESPONSE", 2):
            status, data = self._get("/library/item-paths")
        self.assertEqual(status, 413)
        self.assertIn("error", data)

    def test_health_endpoint_still_unauthenticated_and_unaffected(self):
        # Regression guard: the new endpoints must not have disturbed
        # unauthenticated /health handling (checked before _authenticate()).
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", "/health")
            resp = conn.getresponse()
            body = json.loads(resp.read())
        finally:
            conn.close()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body["status"], "ok")


class BeetsClientRealIPCTests(unittest.TestCase):
    """Same fixture, but exercised through the real BeetsClient methods
    app.py's routes actually call -- proves the full client -> HTTP ->
    engine -> SQLite stack, not just the engine half in isolation."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(cls.tmpdir.name) / "musiclibrary.blb"
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, album TEXT, albumartist TEXT, artist TEXT)")
        con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, path TEXT)")
        con.execute("INSERT INTO albums (id, album, albumartist, artist) VALUES (1, 'Album Bob', 'Bob', 'Bob')")
        con.execute("INSERT INTO items (id, album_id, path) VALUES (1, 1, '/music/Bob/track1.flac')")
        con.execute("INSERT INTO albums (id, album, albumartist, artist) VALUES (2, 'Ghost Album', 'Ghost Artist', 'Ghost Artist')")
        con.commit()
        con.close()

        cls.token = "c" * 40
        cls.lib_patch = mock.patch.object(agent, "LIB_PATH", str(db_path))
        cls.token_patch = mock.patch.object(agent, "BEETS_API_TOKEN", cls.token)
        cls.lib_patch.start()
        cls.token_patch.start()

        cls.port = _get_free_port()
        cls.httpd = http.server.HTTPServer(("127.0.0.1", cls.port), agent.ControlAgentHandler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

        # Real client -> real socket -> real handler, same as
        # test_album_lifecycle_wave24_real_ipc.py's own SSRF-allowlist
        # pattern: allow exactly this test's own ephemeral loopback port.
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
        cls.lib_patch.stop()
        cls.token_patch.stop()
        cls.tmpdir.cleanup()

    def setUp(self):
        self.client = BeetsClient(base_url=f"http://127.0.0.1:{self.port}", token=self.token)

    def test_find_all_albums_by_albumartist_real_ipc(self):
        rows = self.client.find_all_albums_by_albumartist("Bob")
        self.assertEqual([r["id"] for r in rows], [1])

    def test_find_all_albums_by_albumartist_rejects_empty(self):
        with self.assertRaises(BeetsError):
            self.client.find_all_albums_by_albumartist("")

    def test_find_all_orphan_albums_real_ipc(self):
        rows = self.client.find_all_orphan_albums()
        self.assertEqual([r["id"] for r in rows], [2])

    def test_list_distinct_albumartists_real_ipc(self):
        values = self.client.list_distinct_albumartists()
        self.assertEqual(sorted(values), ["Bob", "Ghost Artist"])

    def test_list_distinct_item_paths_real_ipc(self):
        values = self.client.list_distinct_item_paths()
        self.assertEqual(values, ["/music/Bob/track1.flac"])

    def test_engine_offline_fails_closed_for_all_four_new_methods(self):
        """No local mutation/fallback, and no silent empty-list masking a
        real transport failure -- an unreachable engine must surface as
        BeetsUnavailableError from every one of the four new read methods,
        the same fail-closed contract every other BeetsClient method has."""
        dead_port = _get_free_port()  # nothing listening here
        with mock.patch.dict(os.environ, {"BEETS_OUTBOUND_ALLOWLIST": f"127.0.0.1:{dead_port}"}):
            dead_client = BeetsClient(base_url=f"http://127.0.0.1:{dead_port}", token=self.token)
            with self.assertRaises(BeetsUnavailableError):
                dead_client.find_all_albums_by_albumartist("Bob")
            with self.assertRaises(BeetsUnavailableError):
                dead_client.find_all_orphan_albums()
            with self.assertRaises(BeetsUnavailableError):
                dead_client.list_distinct_albumartists()
            with self.assertRaises(BeetsUnavailableError):
                dead_client.list_distinct_item_paths()


if __name__ == "__main__":
    unittest.main()
