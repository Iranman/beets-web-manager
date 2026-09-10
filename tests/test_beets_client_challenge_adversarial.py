"""Adversarial Challenge Test Suite for BeetsClient (Milestone 1).

Covers:
1. Dead / Offline Control Agent (fail-closed -> BeetsUnavailableError).
2. Connection and read timeouts (fail-closed -> BeetsUnavailableError).
3. HTTP Status Codes (400, 401, 408, 409, 500, 503) with structured JSON and non-JSON bodies.
4. Enforcement of zero subprocess / CLI fallback across all operations.
5. Comprehensive parameter validation, boundary stress tests, and injection attacks.
6. Multi-threaded concurrency stress testing.
"""

import ast
import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
import urllib.error

from backend.beets_client import (
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


class TestBeetsClientDeadAndOffline(unittest.TestCase):
    """Challenge 1: Fail-closed semantics when Control Agent is dead or unreachable."""

    def setUp(self):
        self.dead_port = _get_free_port()
        self.env_patcher = mock.patch.dict(os.environ, {
            "BEETS_OUTBOUND_ALLOWLIST": f"127.0.0.1:{self.dead_port}",
        })
        self.env_patcher.start()
        self.client = BeetsClient(
            base_url=f"http://127.0.0.1:{self.dead_port}",
            token="test-token-40-characters-long-auth-mock",
            timeout=1.0,
        )

    def tearDown(self):
        self.env_patcher.stop()

    def test_mbsync_dead_port_raises_unavailable(self):
        with self.assertRaises(BeetsUnavailableError) as ctx:
            self.client.mbsync()
        self.assertIn("unavailable", str(ctx.exception).lower())

    def test_move_library_dead_port_raises_unavailable(self):
        with self.assertRaises(BeetsUnavailableError) as ctx:
            self.client.move_library()
        self.assertIn("unavailable", str(ctx.exception).lower())

    def test_acoustid_submit_dead_port_raises_unavailable(self):
        with self.assertRaises(BeetsUnavailableError) as ctx:
            self.client.acoustid_submit(query="id:123")
        self.assertIn("unavailable", str(ctx.exception).lower())

    def test_unresolvable_host_raises_unavailable(self):
        client = BeetsClient(
            base_url="http://non-existent-domain-xyz-404-dead.local:8338",
            token="token",
            timeout=1.0,
        )
        with self.assertRaises(BeetsUnavailableError) as ctx:
            client.mbsync()
        self.assertIsInstance(ctx.exception, BeetsUnavailableError)


class TestBeetsClientTimeouts(unittest.TestCase):
    """Challenge 2: Connection and read timeout handling."""

    @classmethod
    def setUpClass(cls):
        # Start a raw socket server that accepts connections but never writes a response
        cls.hanging_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cls.hanging_sock.bind(("127.0.0.1", 0))
        cls.hanging_port = cls.hanging_sock.getsockname()[1]
        cls.hanging_sock.listen(5)
        cls.running = True

        cls.env_patcher = mock.patch.dict(os.environ, {
            "BEETS_OUTBOUND_ALLOWLIST": f"127.0.0.1:{cls.hanging_port}",
        })
        cls.env_patcher.start()

        def _drain_and_hang():
            while cls.running:
                try:
                    cls.hanging_sock.settimeout(0.2)
                    client_conn, _ = cls.hanging_sock.accept()
                    # Do not respond, just hold or sleep
                    threading.Thread(target=lambda c: time.sleep(2), args=(client_conn,), daemon=True).start()
                except socket.timeout:
                    continue
                except Exception:
                    break

        cls.server_thread = threading.Thread(target=_drain_and_hang, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.running = False
        cls.hanging_sock.close()
        cls.server_thread.join(timeout=2)
        cls.env_patcher.stop()

    def test_mbsync_read_timeout_raises_unavailable(self):
        client = BeetsClient(base_url=f"http://127.0.0.1:{self.hanging_port}", token="tok", timeout=0.2)
        with self.assertRaises(BeetsUnavailableError) as ctx:
            client.mbsync(timeout=0.2)
        self.assertTrue(
            "unavailable" in str(ctx.exception).lower() or "timed out" in str(ctx.exception).lower()
        )

    def test_move_library_read_timeout_raises_unavailable(self):
        client = BeetsClient(base_url=f"http://127.0.0.1:{self.hanging_port}", token="tok", timeout=0.2)
        with self.assertRaises(BeetsUnavailableError) as ctx:
            client.move_library(timeout=0.2)
        self.assertTrue(
            "unavailable" in str(ctx.exception).lower() or "timed out" in str(ctx.exception).lower()
        )

    def test_acoustid_submit_read_timeout_raises_unavailable(self):
        client = BeetsClient(base_url=f"http://127.0.0.1:{self.hanging_port}", token="tok", timeout=0.2)
        with self.assertRaises(BeetsUnavailableError) as ctx:
            client.acoustid_submit(query="id:1", timeout=0.2)
        self.assertTrue(
            "unavailable" in str(ctx.exception).lower() or "timed out" in str(ctx.exception).lower()
        )

    def test_synthetic_timeout_error_raises_unavailable(self):
        client = BeetsClient(base_url="http://127.0.0.1:8338", token="tok")
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("Request timed out")):
            with self.assertRaises(BeetsUnavailableError) as ctx:
                client.mbsync()
            self.assertIn("timed out", str(ctx.exception).lower())


class MockHTTPHandler(http.server.BaseHTTPRequestHandler):
    """Configurable HTTP handler for testing error responses."""
    response_code = 200
    response_headers = {"Content-Type": "application/json"}
    response_body = b'{"ok": true}'

    def do_POST(self):
        # Read incoming request body to prevent connection aborts
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 0:
            self.rfile.read(content_length)

        self.send_response(self.response_code)
        for k, v in self.response_headers.items():
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(self.response_body)))
        self.end_headers()
        self.wfile.write(self.response_body)

    def log_message(self, format, *args):
        pass


class TestBeetsClientHTTPStatusResponses(unittest.TestCase):
    """Challenge 3: Verify client error handling for 400, 401, 408, 409, 500, 503."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockHTTPHandler)
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
        cls.thread.join(timeout=2)

    def setUp(self):
        self.client = BeetsClient(base_url=f"http://127.0.0.1:{self.port}", token="valid-token")

    def _configure_server(self, code: int, body_dict_or_str, content_type: str = "application/json"):
        MockHTTPHandler.response_code = code
        MockHTTPHandler.response_headers = {"Content-Type": content_type}
        if isinstance(body_dict_or_str, dict):
            MockHTTPHandler.response_body = json.dumps(body_dict_or_str).encode("utf-8")
        elif isinstance(body_dict_or_str, str):
            MockHTTPHandler.response_body = body_dict_or_str.encode("utf-8")
        else:
            MockHTTPHandler.response_body = body_dict_or_str

    # ── 400 Bad Request ────────────────────────────────────────────────────────
    def test_status_400_raises_beets_error(self):
        self._configure_server(400, {"error": "Invalid query parameter", "error_code": "INVALID_QUERY"})
        for method, args in [
            (self.client.mbsync, ()),
            (self.client.move_library, ()),
            (self.client.acoustid_submit, ("id:123",)),
        ]:
            with self.subTest(method=method.__name__):
                with self.assertRaises(BeetsError) as ctx:
                    method(*args)
                self.assertNotIsInstance(ctx.exception, BeetsAuthError)
                self.assertNotIsInstance(ctx.exception, BeetsUnavailableError)
                self.assertEqual(ctx.exception.status_code, 400)
                self.assertEqual(ctx.exception.error_code, "INVALID_QUERY")
                self.assertIn("Invalid query parameter", str(ctx.exception))

    # ── 401 Unauthorized ──────────────────────────────────────────────────────
    def test_status_401_raises_beets_auth_error(self):
        self._configure_server(401, {"error": "Unauthorized", "error_code": "AUTH_FAILED"})
        for method, args in [
            (self.client.mbsync, ()),
            (self.client.move_library, ()),
            (self.client.acoustid_submit, ("id:123",)),
        ]:
            with self.subTest(method=method.__name__):
                with self.assertRaises(BeetsAuthError) as ctx:
                    method(*args)
                self.assertEqual(ctx.exception.status_code, 401)
                self.assertEqual(ctx.exception.error_code, "AUTH_FAILED")
                self.assertIn("Authentication with Beets Control Agent failed", str(ctx.exception))

    # ── 408 Request Timeout ───────────────────────────────────────────────────
    def test_status_408_raises_beets_error(self):
        self._configure_server(408, {"error": "Command 'mbsync' timed out", "error_code": "TIMEOUT", "returncode": 124})
        for method, args in [
            (self.client.mbsync, ()),
            (self.client.move_library, ()),
            (self.client.acoustid_submit, ("id:123",)),
        ]:
            with self.subTest(method=method.__name__):
                with self.assertRaises(BeetsError) as ctx:
                    method(*args)
                self.assertEqual(ctx.exception.status_code, 408)
                self.assertEqual(ctx.exception.error_code, "TIMEOUT")
                self.assertIn("timed out", str(ctx.exception))

    # ── 409 Conflict ──────────────────────────────────────────────────────────
    def test_status_409_raises_beets_error(self):
        self._configure_server(409, {"error": "AcoustID submission unavailable", "error_code": "CAPABILITY_MISSING"})
        with self.assertRaises(BeetsError) as ctx:
            self.client.acoustid_submit("id:123")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.error_code, "CAPABILITY_MISSING")
        self.assertIn("submission unavailable", str(ctx.exception))

    # ── 500 Internal Server Error ─────────────────────────────────────────────
    def test_status_500_raises_beets_error(self):
        self._configure_server(500, {"error": "Internal subprocess failure", "error_code": "ENGINE_CRASH"})
        for method, args in [
            (self.client.mbsync, ()),
            (self.client.move_library, ()),
            (self.client.acoustid_submit, ("id:123",)),
        ]:
            with self.subTest(method=method.__name__):
                with self.assertRaises(BeetsError) as ctx:
                    method(*args)
                self.assertEqual(ctx.exception.status_code, 500)
                self.assertEqual(ctx.exception.error_code, "ENGINE_CRASH")
                self.assertIn("Internal subprocess failure", str(ctx.exception))

    # ── 503 Service Unavailable ───────────────────────────────────────────────
    def test_status_503_raises_beets_error(self):
        self._configure_server(503, {"error": "Failed to acquire engine OS lock", "error_code": "LOCK_BUSY"})
        for method, args in [
            (self.client.mbsync, ()),
            (self.client.move_library, ()),
            (self.client.acoustid_submit, ("id:123",)),
        ]:
            with self.subTest(method=method.__name__):
                with self.assertRaises(BeetsError) as ctx:
                    method(*args)
                self.assertEqual(ctx.exception.status_code, 503)
                self.assertEqual(ctx.exception.error_code, "LOCK_BUSY")
                self.assertIn("Failed to acquire engine OS lock", str(ctx.exception))

    # ── Non-JSON HTML Gateway Error (e.g. 502/504 HTML page from proxy) ──────
    def test_non_json_html_error_raises_beets_error_gracefully(self):
        self._configure_server(502, "<html><head><title>502 Bad Gateway</title></head><body>Bad Gateway</body></html>", content_type="text/html")
        with self.assertRaises(BeetsError) as ctx:
            self.client.mbsync()
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("502", str(ctx.exception))
        self.assertIn("Bad Gateway", str(ctx.exception))


class TestNoSubprocessFallback(unittest.TestCase):
    """Challenge 4: Ensure zero local subprocess fallback exists anywhere in BeetsClient."""

    def test_ast_proves_no_subprocess_imports_or_calls(self):
        client_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend", "beets_client.py"))
        with open(client_path, "r", encoding="utf-8") as f:
            source = f.read()

        tree = ast.parse(source)
        for node in ast.walk(tree):
            # No import subprocess
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name, "subprocess", "beets_client.py must NOT import subprocess")
            elif isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "subprocess", "beets_client.py must NOT import from subprocess")

            # No os.system, os.popen, os.spawn
            if isinstance(node, ast.Attribute):
                if node.attr in ("system", "popen", "spawnl", "spawnle", "spawnlp", "spawnv", "spawnve"):
                    self.fail(f"beets_client.py calls forbidden execution function: os.{node.attr}")

        # String search: no /lsiopy/bin/beet or BEET_BIN
        self.assertNotIn("/lsiopy/bin/beet", source)
        self.assertNotIn("BEET_BIN", source)

    def test_runtime_subprocess_mock_never_invoked(self):
        """Even under severe errors, BeetsClient must NEVER fall back to subprocess."""
        client = BeetsClient(base_url="http://127.0.0.1:1", token="tok", timeout=0.1)
        with mock.patch("subprocess.run") as mock_run, mock.patch("subprocess.Popen") as mock_popen:
            # Under connection refused
            try:
                client.mbsync()
            except BeetsUnavailableError:
                pass
            try:
                client.move_library()
            except BeetsUnavailableError:
                pass
            try:
                client.acoustid_submit("id:1")
            except BeetsUnavailableError:
                pass

            mock_run.assert_not_called()
            mock_popen.assert_not_called()


class TestParameterValidationAndEdgeCases(unittest.TestCase):
    """Challenge 5: Local parameter validation in BeetsClient before network dispatch."""

    def setUp(self):
        self.client = BeetsClient(base_url="http://127.0.0.1:8338", token="tok")

    def test_mbsync_query_type_validation(self):
        for bad_val in [123, 45.6, True, [], {}, object()]:
            with self.subTest(bad_val=bad_val):
                with self.assertRaises(BeetsError) as ctx:
                    self.client.mbsync(query=bad_val)  # type: ignore
                self.assertEqual(ctx.exception.error_code, "INVALID_PARAMETER")

    def test_mbsync_query_forbidden_chars_local_rejection(self):
        forbidden = [";", "&&", "||", "|", ">", "<", "$", "`", "\x00", "\n", "\r"]
        for char in forbidden:
            bad_q = f"artist:Radiohead{char}evil"
            with self.subTest(char=char):
                with self.assertRaises(BeetsError) as ctx:
                    self.client.mbsync(query=bad_q)
                self.assertEqual(ctx.exception.error_code, "INVALID_PARAMETER")
                self.assertIn("forbidden", str(ctx.exception).lower())

    def test_mbsync_query_length_boundary(self):
        # 256 chars -> valid
        valid_q = "a" * 256
        with mock.patch.object(self.client, "_request", return_value={"ok": True}) as mock_req:
            res = self.client.mbsync(query=valid_q)
            self.assertTrue(res["ok"])
            mock_req.assert_called_once()

        # 257 chars -> rejected locally before network
        invalid_q = "a" * 257
        with mock.patch.object(self.client, "_request") as mock_req:
            with self.assertRaises(BeetsError) as ctx:
                self.client.mbsync(query=invalid_q)
            self.assertEqual(ctx.exception.error_code, "INVALID_PARAMETER")
            self.assertIn("exceeds maximum length", str(ctx.exception))
            mock_req.assert_not_called()

    def test_mbsync_unicode_query_accepted(self):
        unicode_q = "artist:Björk 日本語"
        with mock.patch.object(self.client, "_request", return_value={"ok": True}) as mock_req:
            res = self.client.mbsync(query=unicode_q)
            self.assertTrue(res["ok"])
            mock_req.assert_called_once_with(
                "POST", "/library/mbsync",
                {"query": unicode_q, "pretend": False, "async": False},
                timeout=7200.0,
            )

    def test_mbsync_async_parameter_precedence(self):
        # async_ takes precedence over async_job
        with mock.patch.object(self.client, "_request", return_value={"ok": True}) as mock_req:
            self.client.mbsync(async_job=False, async_=True)
            self.assertTrue(mock_req.call_args[0][2]["async"])

        with mock.patch.object(self.client, "_request", return_value={"ok": True}) as mock_req:
            self.client.mbsync(async_job=True, async_=False)
            self.assertFalse(mock_req.call_args[0][2]["async"])

    def test_move_library_query_length_boundary(self):
        valid_q = "b" * 256
        with mock.patch.object(self.client, "_request", return_value={"ok": True}) as mock_req:
            self.client.move_library(query=valid_q)
            mock_req.assert_called_once()

        invalid_q = "b" * 257
        with mock.patch.object(self.client, "_request") as mock_req:
            with self.assertRaises(BeetsError) as ctx:
                self.client.move_library(query=invalid_q)
            self.assertEqual(ctx.exception.error_code, "INVALID_PARAMETER")
            mock_req.assert_not_called()

    def test_move_library_boolean_parameters(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True}) as mock_req:
            self.client.move_library(rescan_first=False, pretend=True)
            payload = mock_req.call_args[0][2]
            self.assertIs(payload["rescan_first"], False)
            self.assertIs(payload["pretend"], True)

    def test_acoustid_submit_query_required(self):
        for empty in ["", "   ", None]:
            with self.subTest(empty=empty):
                with mock.patch.object(self.client, "_request") as mock_req:
                    with self.assertRaises(BeetsError) as ctx:
                        self.client.acoustid_submit(query=empty)  # type: ignore
                    self.assertEqual(ctx.exception.error_code, "INVALID_PARAMETER")
                    mock_req.assert_not_called()

    def test_acoustid_submit_api_key_validation(self):
        # Non-string
        with self.assertRaises(BeetsError) as ctx:
            self.client.acoustid_submit(query="id:1", api_key=12345)  # type: ignore
        self.assertEqual(ctx.exception.error_code, "INVALID_PARAMETER")

        # Invalid characters (spaces, punctuation)
        for bad_key in ["key with spaces", "key;evil", "key!@#$", "k" * 65]:
            with self.subTest(bad_key=bad_key):
                with self.assertRaises(BeetsError) as ctx:
                    self.client.acoustid_submit(query="id:1", api_key=bad_key)
                self.assertEqual(ctx.exception.error_code, "INVALID_PARAMETER")

        # Valid 64-char key
        valid_key = "a" * 64
        with mock.patch.object(self.client, "_request", return_value={"ok": True}) as mock_req:
            res = self.client.acoustid_submit(query="id:1", api_key=valid_key)
            self.assertTrue(res["ok"])
            self.assertEqual(mock_req.call_args[0][2]["api_key"], valid_key)


class TestClientConcurrencyStress(unittest.TestCase):
    """Challenge 6: Multi-threaded concurrency stress test."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockHTTPHandler)
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
        cls.thread.join(timeout=2)

    def test_concurrent_requests_remain_isolated_and_consistent(self):
        MockHTTPHandler.response_code = 200
        MockHTTPHandler.response_headers = {"Content-Type": "application/json"}
        MockHTTPHandler.response_body = b'{"ok": true, "returncode": 0}'

        client = BeetsClient(base_url=f"http://127.0.0.1:{self.port}", token="tok")

        def worker(idx):
            if idx % 3 == 0:
                res = client.mbsync(query=f"artist:Artist{idx}")
            elif idx % 3 == 1:
                res = client.move_library(query=f"album:Album{idx}")
            else:
                res = client.acoustid_submit(query=f"id:{idx}")
            return res.get("ok")

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(worker, i) for i in range(50)]
            results = [f.result() for f in futures]

        self.assertEqual(len(results), 50)
        self.assertTrue(all(results))

class TestLiveControlAgentAdversarialQueries(unittest.TestCase):
    """Challenge 7: End-to-end integration between BeetsClient and live ControlAgentHandler."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        import sqlite3
        import backend.beets_control_agent as agent

        cls.agent = agent
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

        cls.token = "f" * 40
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
        cls.thread.join(timeout=2)
        for p in cls.patches:
            p.stop()
        cls.tmpdir.cleanup()

    def setUp(self):
        self.client = BeetsClient(base_url=f"http://127.0.0.1:{self.port}", token=self.token)

    def test_flag_injection_rejection_end_to_end(self):
        for bad_query in ["-c /tmp/evil.yaml", "--version", "-p"]:
            with self.subTest(bad_query=bad_query):
                with self.assertRaises(BeetsError) as ctx:
                    self.client.mbsync(query=bad_query)
                self.assertEqual(ctx.exception.status_code, 400)
                self.assertIn("flag injection prevented", str(ctx.exception))

                with self.assertRaises(BeetsError) as ctx:
                    self.client.move_library(query=bad_query)
                self.assertEqual(ctx.exception.status_code, 400)
                self.assertIn("flag injection prevented", str(ctx.exception))

                with self.assertRaises(BeetsError) as ctx:
                    self.client.acoustid_submit(query=bad_query)
                self.assertEqual(ctx.exception.status_code, 400)
                self.assertIn("flag injection prevented", str(ctx.exception))

    def test_backslash_and_ampersand_rejection_end_to_end(self):
        for bad_query in ["foo\\bar", "foo&bar"]:
            with self.subTest(bad_query=bad_query):
                with self.assertRaises(BeetsError) as ctx:
                    self.client.mbsync(query=bad_query)
                self.assertEqual(ctx.exception.status_code, 400)
                self.assertIn("dangerous characters detected", str(ctx.exception))

                with self.assertRaises(BeetsError) as ctx:
                    self.client.move_library(query=bad_query)
                self.assertEqual(ctx.exception.status_code, 400)
                self.assertIn("dangerous characters detected", str(ctx.exception))

                with self.assertRaises(BeetsError) as ctx:
                    self.client.acoustid_submit(query=bad_query)
                self.assertEqual(ctx.exception.status_code, 400)
                self.assertIn("dangerous characters detected", str(ctx.exception))

    def test_acoustid_submit_query_length_rejection_end_to_end(self):
        long_query = "id:" + ("1" * 300)
        with self.assertRaises(BeetsError) as ctx:
            self.client.acoustid_submit(query=long_query)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("exceeds maximum length of 256", str(ctx.exception))

    def test_acoustid_submit_missing_capability_409_end_to_end(self):
        with mock.patch.object(self.agent, "get_loaded_beet_plugins", return_value=[]):
            with self.assertRaises(BeetsError) as ctx:
                self.client.acoustid_submit(query="id:123")
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertIn("AcoustID submission unavailable", str(ctx.exception))

    def test_lock_busy_503_end_to_end(self):
        with mock.patch.object(self.agent, "acquire_os_lock", side_effect=BlockingIOError("Lock busy")):
            with self.assertRaises(BeetsError) as ctx:
                self.client.mbsync()
            self.assertEqual(ctx.exception.status_code, 503)
            self.assertIn("Failed to acquire engine OS lock", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
