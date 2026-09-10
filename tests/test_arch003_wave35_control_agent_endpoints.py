"""ARCH-003 (Wave 35): Milestone 1 Engine Control Agent Mutation Endpoints & BeetsClient.

Tests the narrow, server-owned, engine-side Control Agent endpoints and matching BeetsClient methods:
1. POST /library/mbsync: Narrow endpoint for `beet mbsync` with query validation, pretend mode,
   async job support, and exclusive OS locking.
2. POST /library/move: Narrow endpoint for `beet update` + `beet move` with query validation,
   rescan control, pretend mode, and exclusive OS locking.
3. POST /submissions/submit: Narrow endpoint for AcoustID fingerprint submission (`beet submit`)
   with plugin capability gating (`chroma`) and shared OS locking.
4. BeetsClient methods: `mbsync()`, `move_library()`, `acoustid_submit()` with fail-closed semantics.

Test Structure:
- ControlAgentEndpointLiveTests: Direct HTTP loopback socket tests hitting ControlAgentHandler via http.client.
- BeetsClientUnitTests: Unit tests for BeetsClient methods verifying payload building and client validation.
- BeetsClientRealIPCTests: Real client -> HTTP loopback socket -> ControlAgentHandler.
- EngineOfflineFailClosedTests: Fail-closed assertion ensuring BeetsUnavailableError when engine is offline.
"""

import http.client
import http.server
import json
import os
import socket
import sqlite3
import subprocess
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
from backend.beets_client import (  # noqa: E402
    BeetsAuthError,
    BeetsClient,
    BeetsClientError,
    BeetsError,
    BeetsUnavailableError,
)


def _get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ControlAgentEndpointLiveTests(unittest.TestCase):
    """Spins up real ControlAgentHandler over an ephemeral loopback socket."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmpdir.name)
        cls.beetsdir = cls.root / "config"
        cls.beetsdir.mkdir(parents=True, exist_ok=True)
        cls.db_path = cls.beetsdir / "musiclibrary.blb"
        cls.lock_path = cls.beetsdir / ".beet_db.lock"

        con = sqlite3.connect(cls.db_path)
        con.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, album TEXT)")
        con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, path TEXT)")
        con.commit()
        con.close()

        cls.token = "c" * 40
        cls.patches = [
            mock.patch.object(agent, "LIB_PATH", str(cls.db_path)),
            mock.patch.object(agent, "BEETSDIR", str(cls.beetsdir)),
            mock.patch.object(agent, "LOCK_PATH", str(cls.lock_path)),
            mock.patch.object(agent, "BEETS_API_TOKEN", cls.token),
            mock.patch.object(agent, "get_loaded_beet_plugins", return_value=["chroma", "mbsync"]),
        ]
        for p in cls.patches:
            p.start()

        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), agent.ControlAgentHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        for p in cls.patches:
            p.stop()
        cls.tmpdir.cleanup()

    def _post(self, path, payload, token=None, content_type="application/json", raw_data=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Connection": "close"}
        if content_type:
            headers["Content-Type"] = content_type
        use_token = token if token is not None else self.token
        if use_token:
            headers["Authorization"] = f"Bearer {use_token}"

        body_bytes = raw_data if raw_data is not None else (json.dumps(payload).encode("utf-8") if payload is not None else b"")
        headers["Content-Length"] = str(len(body_bytes))

        try:
            conn.request("POST", path, body=body_bytes, headers=headers)
            resp = conn.getresponse()
            raw_resp = resp.read().decode("utf-8")
            try:
                data = json.loads(raw_resp)
            except Exception:
                data = raw_resp
        finally:
            conn.close()
        return resp.status, data

    # ── POST /library/mbsync ───────────────────────────────────────────────────

    def test_mbsync_success_sync_default(self):
        with mock.patch.object(agent.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[agent.BEET_BIN, "mbsync"], returncode=0, stdout="mbsync: 42 items updated", stderr=""
            )
            status, data = self._post("/library/mbsync", {})
            self.assertEqual(status, 200)
            self.assertTrue(data.get("ok"))
            self.assertTrue(data.get("success"))
            self.assertEqual(data.get("returncode"), 0)
            self.assertIn("42 items updated", data.get("stdout"))
            executed_cmd = mock_run.call_args[0][0]
            self.assertEqual(executed_cmd, [agent.BEET_BIN, "mbsync"])

    def test_mbsync_with_query_and_pretend(self):
        with mock.patch.object(agent.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[agent.BEET_BIN, "mbsync", "-p", "artist:Radiohead"], returncode=0, stdout="preview", stderr=""
            )
            status, data = self._post("/library/mbsync", {"query": "artist:Radiohead", "pretend": True})
            self.assertEqual(status, 200)
            executed_cmd = mock_run.call_args[0][0]
            self.assertEqual(executed_cmd, [agent.BEET_BIN, "mbsync", "-p", "artist:Radiohead"])

    def test_mbsync_async_job(self):
        with mock.patch.object(agent.subprocess, "Popen") as mock_popen:
            mock_proc = mock.MagicMock()
            mock_proc.communicate.return_value = ("done", "")
            mock_proc.returncode = 0
            mock_proc.poll.return_value = 0
            mock_popen.return_value = mock_proc

            status, data = self._post("/library/mbsync", {"async": True, "query": "album:OK Computer"})
            self.assertEqual(status, 200)
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("status"), "started")
            self.assertTrue(data.get("job_id").startswith("mbsync-"))
            self.assertIn("/jobs/", data.get("endpoint"))

    def test_mbsync_beet_nonzero_exit(self):
        with mock.patch.object(agent.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[agent.BEET_BIN, "mbsync"], returncode=1, stdout="", stderr="MusicBrainz connection failed"
            )
            status, data = self._post("/library/mbsync", {})
            self.assertEqual(status, 200)
            self.assertFalse(data.get("ok"))
            self.assertFalse(data.get("success"))
            self.assertEqual(data.get("returncode"), 1)
            self.assertIn("MusicBrainz connection failed", data.get("stderr"))

    def test_mbsync_malformed_json_body(self):
        status, data = self._post("/library/mbsync", None, raw_data=b"not-a-json")
        self.assertEqual(status, 400)
        self.assertIn("Invalid JSON body", data.get("error", ""))

    def test_mbsync_non_object_body(self):
        status, data = self._post("/library/mbsync", ["query"])
        self.assertEqual(status, 400)
        self.assertIn("Request body must be a JSON object", data.get("error", ""))

    def test_mbsync_invalid_query_type(self):
        status, data = self._post("/library/mbsync", {"query": 12345})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "query must be a string")

    def test_mbsync_injection_query(self):
        for bad_query in ["artist:Foo; rm -rf /", "test && cat /etc/passwd", "album:Bar | reboot", "foo`id`", "foo$bar"]:
            status, data = self._post("/library/mbsync", {"query": bad_query})
            self.assertEqual(status, 400)
            self.assertIn("dangerous characters detected", data.get("error", ""))

    def test_mbsync_flag_injection_query(self):
        status, data = self._post("/library/mbsync", {"query": "-c /tmp/evil.yaml"})
        self.assertEqual(status, 400)
        self.assertIn("flag injection prevented", data.get("error", ""))

    def test_mbsync_invalid_pretend_type(self):
        status, data = self._post("/library/mbsync", {"pretend": "true"})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "pretend must be a boolean")

    def test_mbsync_invalid_async_type(self):
        status, data = self._post("/library/mbsync", {"async": 1})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "async must be a boolean")

    def test_mbsync_invalid_timeout_type(self):
        status, data = self._post("/library/mbsync", {"timeout": "invalid"})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "Invalid timeout parameter")

    def test_mbsync_missing_auth(self):
        status, data = self._post("/library/mbsync", {}, token="")
        self.assertEqual(status, 401)
        self.assertIn("Unauthorized", data.get("error", ""))

    def test_mbsync_bad_auth(self):
        status, data = self._post("/library/mbsync", {}, token="invalid-token-here")
        self.assertEqual(status, 401)

    def test_mbsync_lock_busy_503(self):
        with mock.patch.object(agent, "acquire_os_lock", side_effect=BlockingIOError("Lock busy")):
            status, data = self._post("/library/mbsync", {})
            self.assertEqual(status, 503)
            self.assertIn("Failed to acquire engine OS lock", data.get("error", ""))

    def test_mbsync_timeout_408(self):
        with mock.patch.object(agent.subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd=["mbsync"], timeout=10.0)):
            status, data = self._post("/library/mbsync", {"timeout": 10.0})
            self.assertEqual(status, 408)
            self.assertEqual(data.get("returncode"), 124)

    # ── POST /library/move ─────────────────────────────────────────────────────

    def test_move_success_default_rescan_and_move(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=f"{cmd[1]} completed", stderr="")

        with mock.patch.object(agent.subprocess, "run", side_effect=fake_run):
            status, data = self._post("/library/move", {})
            self.assertEqual(status, 200)
            self.assertTrue(data.get("ok"))
            self.assertTrue(data.get("success"))
            self.assertTrue(data.get("updated"))
            self.assertTrue(data.get("moved"))
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0], [agent.BEET_BIN, "update"])
            self.assertEqual(calls[1], [agent.BEET_BIN, "move"])

    def test_move_without_rescan(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="move completed", stderr="")

        with mock.patch.object(agent.subprocess, "run", side_effect=fake_run):
            status, data = self._post("/library/move", {"rescan_first": False})
            self.assertEqual(status, 200)
            self.assertTrue(data.get("ok"))
            self.assertFalse(data.get("updated"))
            self.assertTrue(data.get("moved"))
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0], [agent.BEET_BIN, "move"])

    def test_move_with_query_and_pretend(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=f"{cmd[1]} ok", stderr="")

        with mock.patch.object(agent.subprocess, "run", side_effect=fake_run):
            status, data = self._post("/library/move", {"query": "album:OK Computer", "pretend": True})
            self.assertEqual(status, 200)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0], [agent.BEET_BIN, "update", "-p", "album:OK Computer"])
            self.assertEqual(calls[1], [agent.BEET_BIN, "move", "-p", "album:OK Computer"])

    def test_move_rescan_failure_halts_and_aborts_move(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if cmd[1] == "update":
                return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="update disk error")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="move ok", stderr="")

        with mock.patch.object(agent.subprocess, "run", side_effect=fake_run):
            status, data = self._post("/library/move", {})
            self.assertEqual(status, 200)
            self.assertFalse(data.get("ok"))
            self.assertFalse(data.get("success"))
            self.assertFalse(data.get("updated"))
            self.assertFalse(data.get("moved"))
            self.assertEqual(data.get("returncode"), 1)
            self.assertIn("Rescan (beet update) failed; move aborted", data.get("error", ""))
            # Ensure move was NEVER called
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1], "update")

    def test_move_async_job(self):
        with mock.patch.object(agent.subprocess, "Popen") as mock_popen:
            mock_proc = mock.MagicMock()
            mock_proc.communicate.return_value = ("updated", "")
            mock_proc.returncode = 0
            mock_proc.poll.return_value = 0
            mock_popen.return_value = mock_proc

            status, data = self._post("/library/move", {"async": True})
            self.assertEqual(status, 200)
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("status"), "started")
            self.assertTrue(data.get("job_id").startswith("move-"))
            self.assertIn("/jobs/", data.get("endpoint"))

    def test_move_non_object_body(self):
        status, data = self._post("/library/move", [])
        self.assertEqual(status, 400)
        self.assertIn("Request body must be a JSON object", data.get("error", ""))

    def test_move_invalid_query_type(self):
        status, data = self._post("/library/move", {"query": ["invalid"]})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "query must be a string")

    def test_move_injection_query(self):
        status, data = self._post("/library/move", {"query": "test | cat /etc/passwd"})
        self.assertEqual(status, 400)
        self.assertIn("dangerous characters detected", data.get("error", ""))

    def test_move_invalid_rescan_type(self):
        status, data = self._post("/library/move", {"rescan_first": "no"})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "rescan_first must be a boolean")

    def test_move_invalid_pretend_type(self):
        status, data = self._post("/library/move", {"pretend": 0})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "pretend must be a boolean")

    def test_move_missing_auth(self):
        status, data = self._post("/library/move", {}, token="")
        self.assertEqual(status, 401)

    def test_move_bad_auth(self):
        status, data = self._post("/library/move", {}, token="wrong-token")
        self.assertEqual(status, 401)

    def test_move_lock_busy_503(self):
        with mock.patch.object(agent, "acquire_os_lock", side_effect=BlockingIOError("Lock held")):
            status, data = self._post("/library/move", {})
            self.assertEqual(status, 503)
            self.assertIn("Failed to acquire engine OS lock", data.get("error", ""))

    # ── POST /submissions/submit ───────────────────────────────────────────────

    def test_submit_success_query_only(self):
        with mock.patch.object(agent.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[agent.BEET_BIN, "submit", "id:12345"], returncode=0, stdout="Fingerprint submitted", stderr=""
            )
            status, data = self._post("/submissions/submit", {"query": "id:12345"})
            self.assertEqual(status, 200)
            self.assertTrue(data.get("ok"))
            self.assertTrue(data.get("success"))
            self.assertEqual(data.get("returncode"), 0)
            self.assertIn("Fingerprint submitted", data.get("stdout"))
            executed_cmd = mock_run.call_args[0][0]
            self.assertEqual(executed_cmd, [agent.BEET_BIN, "submit", "id:12345"])

    def test_submit_with_api_key_creates_isolated_config(self):
        with mock.patch.object(agent.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[agent.BEET_BIN, "-c", "tmp.yaml", "submit", "id:12345"], returncode=0, stdout="done", stderr=""
            )
            status, data = self._post("/submissions/submit", {"query": "id:12345", "api_key": "sec_key_123"})
            self.assertEqual(status, 200)
            executed_cmd = mock_run.call_args[0][0]
            self.assertIn("-c", executed_cmd)
            self.assertIn("submit", executed_cmd)
            self.assertIn("id:12345", executed_cmd)

    def test_submit_missing_query(self):
        status, data = self._post("/submissions/submit", {})
        self.assertEqual(status, 400)
        self.assertIn("query is required and must be non-empty", data.get("error", ""))

        status, data = self._post("/submissions/submit", {"query": "   "})
        self.assertEqual(status, 400)
        self.assertIn("query is required and must be non-empty", data.get("error", ""))

    def test_submit_invalid_query_type(self):
        status, data = self._post("/submissions/submit", {"query": 12345})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "query must be a string")

    def test_submit_injection_query(self):
        status, data = self._post("/submissions/submit", {"query": "id:1; reboot"})
        self.assertEqual(status, 400)
        self.assertIn("dangerous characters detected", data.get("error", ""))

    def test_submit_invalid_api_key_type(self):
        status, data = self._post("/submissions/submit", {"query": "id:1", "api_key": 999})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "api_key must be a string")

    def test_submit_invalid_api_key_format(self):
        status, data = self._post("/submissions/submit", {"query": "id:1", "api_key": "invalid;key!"})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("error"), "Invalid api_key format")

    def test_submit_capability_missing_409(self):
        with mock.patch.object(agent, "get_loaded_beet_plugins", return_value=[]):
            status, data = self._post("/submissions/submit", {"query": "id:123"})
            self.assertEqual(status, 409)
            self.assertEqual(data.get("error"), "AcoustID submission unavailable")
            self.assertIn("Chroma is configured", data.get("reason", ""))

    def test_submit_missing_auth(self):
        status, data = self._post("/submissions/submit", {"query": "id:1"}, token="")
        self.assertEqual(status, 401)

    def test_submit_bad_auth(self):
        status, data = self._post("/submissions/submit", {"query": "id:1"}, token="bad-token")
        self.assertEqual(status, 401)

    def test_submit_lock_busy_503(self):
        with mock.patch.object(agent, "acquire_os_lock", side_effect=BlockingIOError("Lock busy")):
            status, data = self._post("/submissions/submit", {"query": "id:123"})
            self.assertEqual(status, 503)
            self.assertIn("Failed to acquire engine OS lock", data.get("error", ""))


class BeetsClientUnitTests(unittest.TestCase):
    """Direct isolated unit tests for BeetsClient methods."""

    def setUp(self):
        self.client = BeetsClient(base_url="http://127.0.0.1:8338", token="test-token-at-least-32-chars-long!")

    def test_beets_client_error_alias(self):
        self.assertIs(BeetsClientError, BeetsError)
        self.assertTrue(issubclass(BeetsClientError, Exception))

    # mbsync
    def test_client_mbsync_defaults(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "returncode": 0}) as mock_req:
            res = self.client.mbsync()
            self.assertTrue(res["ok"])
            mock_req.assert_called_once_with(
                "POST",
                "/library/mbsync",
                {"query": "", "pretend": False, "async": False},
                timeout=7200.0,
            )

    def test_client_mbsync_custom_params(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "job_id": "j1"}) as mock_req:
            res = self.client.mbsync(query="artist:Radiohead", pretend=True, async_job=True)
            self.assertTrue(res["ok"])
            mock_req.assert_called_once_with(
                "POST",
                "/library/mbsync",
                {"query": "artist:Radiohead", "pretend": True, "async": True},
                timeout=15.0,
            )

    def test_client_mbsync_validates_query_rejection(self):
        with self.assertRaises(BeetsError) as ctx:
            self.client.mbsync(query=123)  # type: ignore
        self.assertEqual(ctx.exception.error_code, "INVALID_PARAMETER")

        with self.assertRaises(BeetsError) as ctx:
            self.client.mbsync(query="foo; rm -rf")
        self.assertEqual(ctx.exception.error_code, "INVALID_PARAMETER")

        with self.assertRaises(BeetsError) as ctx:
            self.client.mbsync(query="x" * 300)
        self.assertEqual(ctx.exception.error_code, "INVALID_PARAMETER")

    # move_library
    def test_client_move_library_defaults(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "updated": True, "moved": True}) as mock_req:
            res = self.client.move_library()
            self.assertTrue(res["ok"])
            mock_req.assert_called_once_with(
                "POST",
                "/library/move",
                {"query": "", "rescan_first": True, "pretend": False},
                timeout=3600.0,
            )

    def test_client_move_library_custom_params(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "job_id": "j2"}) as mock_req:
            res = self.client.move_library(query="album:OK", rescan_first=False, pretend=True, async_job=True)
            self.assertTrue(res["ok"])
            mock_req.assert_called_once_with(
                "POST",
                "/library/move",
                {"query": "album:OK", "rescan_first": False, "pretend": True, "async": True},
                timeout=15.0,
            )

    def test_client_move_library_validates_query(self):
        with self.assertRaises(BeetsError) as ctx:
            self.client.move_library(query="foo && bar")
        self.assertEqual(ctx.exception.error_code, "INVALID_PARAMETER")

    # acoustid_submit
    def test_client_acoustid_submit_success(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "returncode": 0}) as mock_req:
            res = self.client.acoustid_submit(query="id:42")
            self.assertTrue(res["ok"])
            mock_req.assert_called_once_with(
                "POST",
                "/submissions/submit",
                {"query": "id:42"},
                timeout=300.0,
            )

    def test_client_acoustid_submit_with_api_key(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True}) as mock_req:
            res = self.client.acoustid_submit(query="album_id:10", api_key="my_key_123", timeout=120.0)
            self.assertTrue(res["ok"])
            mock_req.assert_called_once_with(
                "POST",
                "/submissions/submit",
                {"query": "album_id:10", "api_key": "my_key_123"},
                timeout=120.0,
            )

    def test_client_acoustid_submit_validates_required_query(self):
        with self.assertRaises(BeetsError) as ctx:
            self.client.acoustid_submit(query="")
        self.assertEqual(ctx.exception.error_code, "INVALID_PARAMETER")

        with self.assertRaises(BeetsError) as ctx:
            self.client.acoustid_submit(query="   ")
        self.assertEqual(ctx.exception.error_code, "INVALID_PARAMETER")

        with self.assertRaises(BeetsError) as ctx:
            self.client.acoustid_submit(query="id:1; reboot")
        self.assertEqual(ctx.exception.error_code, "INVALID_PARAMETER")

    def test_client_acoustid_submit_validates_api_key(self):
        with self.assertRaises(BeetsError) as ctx:
            self.client.acoustid_submit(query="id:1", api_key="bad key!")
        self.assertEqual(ctx.exception.error_code, "INVALID_PARAMETER")


class BeetsClientRealIPCTests(unittest.TestCase):
    """Tests real BeetsClient instances executing against the live loopback HTTP socket."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmpdir.name)
        cls.beetsdir = cls.root / "config"
        cls.beetsdir.mkdir(parents=True, exist_ok=True)
        cls.db_path = cls.beetsdir / "musiclibrary.blb"
        cls.lock_path = cls.beetsdir / ".beet_db.lock"

        con = sqlite3.connect(cls.db_path)
        con.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, album TEXT)")
        con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, path TEXT)")
        con.commit()
        con.close()

        cls.token = "d" * 40
        cls.patches = [
            mock.patch.object(agent, "LIB_PATH", str(cls.db_path)),
            mock.patch.object(agent, "BEETSDIR", str(cls.beetsdir)),
            mock.patch.object(agent, "LOCK_PATH", str(cls.lock_path)),
            mock.patch.object(agent, "BEETS_API_TOKEN", cls.token),
            mock.patch.object(agent, "get_loaded_beet_plugins", return_value=["chroma", "mbsync"]),
        ]
        for p in cls.patches:
            p.start()

        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), agent.ControlAgentHandler)
        cls.port = cls.httpd.server_address[1]
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
        for p in cls.patches:
            p.stop()
        cls.tmpdir.cleanup()

    def setUp(self):
        self.client = BeetsClient(base_url=f"http://127.0.0.1:{self.port}", token=self.token)

    def test_mbsync_real_ipc(self):
        with mock.patch.object(agent.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[agent.BEET_BIN, "mbsync", "-p", "artist:Radiohead"], returncode=0, stdout="mbsync ok", stderr=""
            )
            res = self.client.mbsync(query="artist:Radiohead", pretend=True)
            self.assertTrue(res.get("ok"))
            self.assertEqual(res.get("returncode"), 0)
            self.assertIn("mbsync ok", res.get("stdout"))

    def test_move_library_real_ipc(self):
        with mock.patch.object(agent.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[agent.BEET_BIN, "move"], returncode=0, stdout="move ok", stderr=""
            )
            res = self.client.move_library(rescan_first=False)
            self.assertTrue(res.get("ok"))
            self.assertTrue(res.get("moved"))
            self.assertFalse(res.get("updated"))

    def test_acoustid_submit_real_ipc(self):
        with mock.patch.object(agent.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[agent.BEET_BIN, "submit", "id:123"], returncode=0, stdout="submit ok", stderr=""
            )
            res = self.client.acoustid_submit(query="id:123")
            self.assertTrue(res.get("ok"))
            self.assertEqual(res.get("returncode"), 0)
            self.assertIn("submit ok", res.get("stdout"))


class EngineOfflineFailClosedTests(unittest.TestCase):
    """Asserts BeetsUnavailableError when connecting to a dead/unreachable port."""

    def test_engine_offline_fails_closed_for_all_m1_methods(self):
        dead_port = _get_free_port()
        token = "e" * 40
        with mock.patch.dict(os.environ, {"BEETS_OUTBOUND_ALLOWLIST": f"127.0.0.1:{dead_port}"}):
            dead_client = BeetsClient(base_url=f"http://127.0.0.1:{dead_port}", token=token)
            with self.assertRaises(BeetsUnavailableError):
                dead_client.mbsync()
            with self.assertRaises(BeetsUnavailableError):
                dead_client.move_library()
            with self.assertRaises(BeetsUnavailableError):
                dead_client.acoustid_submit(query="id:123")


if __name__ == "__main__":
    unittest.main()
