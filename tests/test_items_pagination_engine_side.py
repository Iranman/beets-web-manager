"""Integration tests proving GET /items on the Beets control agent applies
LIMIT/OFFSET inside the SQL query itself, rather than fetching the whole
`items` table and slicing it in Python. beets_client.get_items_page() (the
web-manager side) forwards offset/limit straight through as query-string
params -- this file is the other half of that contract: proof the engine
that receives them actually only touches `limit` rows per request.
"""
import http.client
import http.server
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
AGENT_SOURCE = (ROOT / "backend" / "beets_control_agent.py").read_text(encoding="utf-8")

import backend.beets_control_agent as agent  # noqa: E402


class ItemsQuerySourceTests(unittest.TestCase):
    def test_items_query_pushes_limit_offset_into_sql_not_python_slicing(self):
        """The /items handler must bind LIMIT/OFFSET as SQL parameters, not
        fetch every row and slice the Python list afterward."""
        idx = AGENT_SOURCE.index('if path == "/items":')
        end = AGENT_SOURCE.index('if path.startswith("/items/"):', idx)
        handler_src = AGENT_SOURCE[idx:end]
        self.assertIn("SELECT * FROM items", handler_src)
        self.assertIn("LIMIT ? OFFSET ?", handler_src)
        self.assertIn("SELECT COUNT(*) FROM items", handler_src)


class ItemsPaginationLiveEngineTests(unittest.TestCase):
    """Spins up the real ControlAgentHandler over HTTP against a temp SQLite
    library so pagination is exercised end-to-end, the way beets_client
    actually calls it."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(cls.tmpdir.name) / "musiclibrary.blb"
        con = sqlite3.connect(db_path)
        con.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, title TEXT, "
            "artist TEXT, albumartist TEXT, path TEXT, mb_trackid TEXT, track INTEGER, length REAL)"
        )
        for i in range(1, 6):
            con.execute(
                "INSERT INTO items (id, album_id, title, artist, albumartist, path, mb_trackid, track, length) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (i, 1, f"Track {i}", "Artist", "Artist", f"/music/track{i}.flac", "", i, 200.0),
            )
        con.commit()
        con.close()

        cls.token = "a" * 40
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
        # Uses http.client rather than urllib.request: once any test module
        # imports app.py in the same process, backend.security's outbound
        # SSRF allowlist patches urllib.request.urlopen for the whole
        # process and would (correctly) refuse this loopback test call too.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path, headers={"Authorization": f"Bearer {self.token}"})
            resp = conn.getresponse()
            body = resp.read()
        finally:
            conn.close()
        return json.loads(body)

    def test_limit_1_returns_exactly_one_item_and_true_total(self):
        data = self._get("/items?limit=1&offset=0")
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["total"], 5)
        self.assertEqual(data["items"][0]["id"], 1)
        self.assertTrue(data["has_more"])
        self.assertEqual(data["next_offset"], 1)

    def test_offset_advances_to_the_correct_row_not_the_full_set(self):
        data = self._get("/items?limit=1&offset=4")
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["id"], 5)
        self.assertEqual(data["total"], 5)
        self.assertFalse(data["has_more"])
        self.assertIsNone(data["next_offset"])

    def test_limit_50_returns_all_five_available_without_error(self):
        data = self._get("/items?limit=50&offset=0")
        self.assertEqual(len(data["items"]), 5)
        self.assertEqual(data["total"], 5)
        self.assertFalse(data["has_more"])


if __name__ == "__main__":
    unittest.main()
