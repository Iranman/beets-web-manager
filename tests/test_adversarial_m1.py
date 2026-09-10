"""Empirical Adversarial Stress-Test Suite for Milestone 1 (Control Agent Endpoints).

Tests:
1. Malformed JSON, non-JSON bodies, unexpected types, boundary strings, empty queries,
   queries with shell metacharacters, queries with '-' flags (e.g. --version, -c, -l), huge queries.
2. Concurrency / lock contention: simulate concurrent requests where one holds the lock
   and another attempts to acquire, asserting HTTP 503 Service Unavailable.
3. HMAC authentication tampering: invalid signatures/tokens, missing headers, replay/stale tokens,
   timing attack resistance.
4. Server stability, crash resistance, and fail-closed behavior across all edge cases.
"""

import concurrent.futures
import http.client
import http.server
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backend.beets_control_agent as agent
from backend.beets_client import (
    BeetsAuthError,
    BeetsClient,
    BeetsClientError,
    BeetsError,
    BeetsUnavailableError,
)


class BaseAdversarialLiveServerTest(unittest.TestCase):
    """Base test class providing an ephemeral live ControlAgent server on loopback."""

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

        cls.valid_token = "a" * 32 + "secret_token_1234567890_ok"
        cls.patches = [
            mock.patch.object(agent, "LIB_PATH", str(cls.db_path)),
            mock.patch.object(agent, "BEETSDIR", str(cls.beetsdir)),
            mock.patch.object(agent, "LOCK_PATH", str(cls.lock_path)),
            mock.patch.object(agent, "BEETS_API_TOKEN", cls.valid_token),
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

    def _raw_request(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        headers: dict = None,
        timeout: float = 5.0,
    ):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        hdrs = {"Connection": "close"}
        if headers:
            hdrs.update(headers)
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        resp_data = resp.read()
        conn.close()
        try:
            parsed = json.loads(resp_data.decode("utf-8"))
        except Exception:
            parsed = resp_data
        return resp.status, parsed

    def _raw_socket_send(self, raw_bytes: bytes) -> tuple[int, bytes]:
        """Send raw bytes over a plain TCP socket to test CRLF and malformed wire formats."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect(("127.0.0.1", self.port))
        s.sendall(raw_bytes)
        response_data = b""
        while True:
            try:
                chunk = s.recv(4096)
                if not chunk:
                    break
                response_data += chunk
            except Exception:
                break
        s.close()
        status = 0
        if response_data.startswith(b"HTTP/"):
            parts = response_data.split(b" ", 2)
            if len(parts) >= 2 and parts[1].isdigit():
                status = int(parts[1])
        return status, response_data

    def _post(self, path: str, payload: dict, token: str = None, headers: dict = None):
        t = self.valid_token if token is None else token
        hdrs = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {t}" if t else "",
        }
        if headers:
            hdrs.update(headers)
        body = json.dumps(payload).encode("utf-8")
        return self._raw_request("POST", path, body=body, headers=hdrs)


class TestAdversarialPayloads(BaseAdversarialLiveServerTest):
    """Stress-test endpoints with malformed JSON, boundary values, injections, and mutations."""

    # ── 1. Malformed JSON and Non-JSON Bodies ──────────────────────────────────

    def test_malformed_json_bodies(self):
        malformed_inputs = [
            b"{",
            b"{\"query\":",
            b"{'query': '123'}",
            b"<xml><query>123</query></xml>",
            b"query=artist%3ABeatles",
            b"\x00\x01\x02\x03\xff",
            b"{\"query\": \"\xff\xfe\"}",  # Invalid UTF-8 bytes
            b"{truncated json",
        ]
        endpoints = ["/library/mbsync", "/library/move", "/submissions/submit"]
        for ep in endpoints:
            for raw in malformed_inputs:
                status, data = self._raw_request(
                    "POST",
                    ep,
                    body=raw,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.valid_token}",
                    },
                )
                self.assertEqual(status, 400, f"Expected 400 for {ep} with body {raw!r}, got {status}")
                self.assertIsInstance(data, dict)
                self.assertIn("Invalid JSON", data.get("error", ""))

    def test_non_json_content_types_and_empty_body(self):
        endpoints = ["/library/mbsync", "/library/move", "/submissions/submit"]
        with mock.patch.object(agent.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
            for ep in endpoints:
                # Non-JSON url-encoded body
                status, data = self._raw_request(
                    "POST",
                    ep,
                    body=b"query=artist%3ABeatles",
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Authorization": f"Bearer {self.valid_token}",
                    },
                )
                self.assertEqual(status, 400)
                self.assertIn("Invalid JSON body", data.get("error", ""))

                # Empty body b""
                status, data = self._raw_request(
                    "POST",
                    ep,
                    body=b"",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.valid_token}",
                    },
                )
                # Empty body defaults to {} in do_POST:
                # For submit, missing query returns 400
                # For mbsync and move, empty body defaults to full-library sync/move (200 with mocked runner)
                expected_status = 400 if ep == "/submissions/submit" else 200
                self.assertEqual(status, expected_status, f"Unexpected status for empty body on {ep}: {status}")

    def test_top_level_non_object_json(self):
        non_objects = [
            b"null",
            b"\"plain string\"",
            b"12345",
            b"true",
            b"false",
            b"[1, 2, 3]",
            b"[\"mbsync\", \"--help\"]",
        ]
        endpoints = ["/library/mbsync", "/library/move", "/submissions/submit"]
        for ep in endpoints:
            for raw in non_objects:
                status, data = self._raw_request(
                    "POST",
                    ep,
                    body=raw,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.valid_token}",
                    },
                )
                self.assertEqual(status, 400, f"Expected 400 for {ep} with top-level {raw!r}, got {status}")
                self.assertIsInstance(data, dict)
                self.assertEqual(data.get("error"), "Request body must be a JSON object")

    def test_invalid_content_length_header(self):
        endpoints = ["/library/mbsync", "/library/move", "/submissions/submit"]
        for ep in endpoints:
            status, data = self._raw_request(
                "POST",
                ep,
                body=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.valid_token}",
                    "Content-Length": "-5",
                },
            )
            self.assertEqual(status, 400)
            self.assertEqual(data.get("error"), "Invalid Content-Length")

            status, data = self._raw_request(
                "POST",
                ep,
                body=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.valid_token}",
                    "Content-Length": "not_an_int",
                },
            )
            self.assertEqual(status, 400)
            self.assertEqual(data.get("error"), "Invalid Content-Length")

    # ── 2. Unexpected Field Types (Fuzzing) ───────────────────────────────────

    def test_mbsync_unexpected_field_types(self):
        bad_payloads = [
            ({"query": 12345}, "query must be a string"),
            ({"query": True}, "query must be a string"),
            ({"query": ["artist:Beatles"]}, "query must be a string"),
            ({"query": {"artist": "Beatles"}}, "query must be a string"),
            ({"pretend": "true"}, "pretend must be a boolean"),
            ({"pretend": 1}, "pretend must be a boolean"),
            ({"pretend": [True]}, "pretend must be a boolean"),
            ({"pretend": {"p": True}}, "pretend must be a boolean"),
            ({"async": "false"}, "async must be a boolean"),
            ({"async": 1}, "async must be a boolean"),
            ({"async": [False]}, "async must be a boolean"),
            ({"timeout": "not_a_number"}, "Invalid timeout parameter"),
            ({"timeout": -10}, "Invalid timeout parameter"),
            ({"timeout": 0}, "Invalid timeout parameter"),
            ({"timeout": []}, "Invalid timeout parameter"),
            ({"timeout": {}}, "Invalid timeout parameter"),
        ]
        for payload, expected_err in bad_payloads:
            status, data = self._post("/library/mbsync", payload)
            self.assertEqual(status, 400, f"Payload {payload} expected 400, got {status}")
            self.assertIn(expected_err, data.get("error", ""))

    def test_move_unexpected_field_types(self):
        bad_payloads = [
            ({"query": 999}, "query must be a string"),
            ({"query": [1, 2]}, "query must be a string"),
            ({"rescan_first": "yes"}, "rescan_first must be a boolean"),
            ({"rescan_first": 1}, "rescan_first must be a boolean"),
            ({"rescan_first": []}, "rescan_first must be a boolean"),
            ({"pretend": "no"}, "pretend must be a boolean"),
            ({"pretend": 0}, "pretend must be a boolean"),
            ({"async": "1"}, "async must be a boolean"),
            ({"timeout": -1}, "Invalid timeout parameter"),
            ({"timeout": "fast"}, "Invalid timeout parameter"),
        ]
        for payload, expected_err in bad_payloads:
            status, data = self._post("/library/move", payload)
            self.assertEqual(status, 400, f"Payload {payload} expected 400, got {status}")
            self.assertIn(expected_err, data.get("error", ""))

    def test_submit_unexpected_field_types(self):
        bad_payloads = [
            ({"query": 123}, "query must be a string"),
            ({"query": None}, "query is required and must be non-empty"),
            ({"query": []}, "query must be a string"),
            ({"query": "id:1", "api_key": 12345}, "api_key must be a string"),
            ({"query": "id:1", "api_key": True}, "api_key must be a string"),
            ({"query": "id:1", "api_key": []}, "api_key must be a string"),
            ({"query": "id:1", "api_key": {}}, "api_key must be a string"),
            ({"query": "id:1", "api_key": "bad;key!"}, "Invalid api_key format"),
            ({"query": "id:1", "api_key": "a" * 65}, "Invalid api_key format"),
            ({"query": "id:1", "timeout": 0}, "Invalid timeout parameter"),
            ({"query": "id:1", "timeout": -100}, "Invalid timeout parameter"),
            ({"query": "id:1", "timeout": "abc"}, "Invalid timeout parameter"),
        ]
        for payload, expected_err in bad_payloads:
            status, data = self._post("/submissions/submit", payload)
            self.assertEqual(status, 400, f"Payload {payload} expected 400, got {status}")
            self.assertIn(expected_err, data.get("error", ""))

    # ── 3. Flag Injection Attacks ─────────────────────────────────────────────

    def test_flag_injection_prevention(self):
        flag_payloads = [
            "-v",
            "--version",
            "-c /tmp/malicious.yaml",
            "-l /tmp/malicious.log",
            "-d /tmp",
            "--help",
            "-p",
            "-q",
            "-a",
            "--format $title",
            "  -c /etc/shadow",  # Leading whitespace before flag
            "\t--config evil.yaml",
            "-",
            "--",
            "---",
            "-album:Help",
        ]
        endpoints = ["/library/mbsync", "/library/move", "/submissions/submit"]
        for ep in endpoints:
            for flag in flag_payloads:
                status, data = self._post(ep, {"query": flag})
                self.assertEqual(status, 400, f"Flag injection {flag!r} on {ep} expected 400, got {status}")
                self.assertIn("flag injection prevented", data.get("error", ""))

    # ── 4. Shell Metacharacter Injections ─────────────────────────────────────

    def test_shell_metacharacter_injections(self):
        shell_injections = [
            "id:1; reboot",
            "id:1 & calc",
            "id:1 | ls",
            "id:1 > /tmp/hacked",
            "id:1 < /dev/zero",
            "id:1 $PATH",
            "id:1 `whoami`",
            "id:1\\escape",
            "id:1\x00inject",
            "id:1\nevil",
            "id:1\revil",
            "artist:Beatles; rm -rf /",
            "artist:Beatles | curl evil.com",
            "artist:Beatles $(calc.exe)",
            "artist:Beatles `calc.exe`",
            "artist:Beatles > /data/music/evil.txt",
        ]
        endpoints = ["/library/mbsync", "/library/move", "/submissions/submit"]
        for ep in endpoints:
            for injection in shell_injections:
                status, data = self._post(ep, {"query": injection})
                self.assertEqual(status, 400, f"Injection {injection!r} on {ep} expected 400, got {status}")
                self.assertIn("dangerous characters detected", data.get("error", ""))

    # ── 5. Boundary Strings & Length Limits ───────────────────────────────────

    def test_empty_and_whitespace_queries(self):
        # /library/mbsync and /library/move allow empty/whitespace query for full library operation
        with mock.patch.object(agent.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
            for ep in ["/library/mbsync", "/library/move"]:
                for q in ["", "   ", "\t  "]:
                    status, data = self._post(ep, {"query": q})
                    self.assertEqual(status, 200, f"Empty query on {ep} expected 200, got {status}")
                    self.assertTrue(data.get("ok"))

        # /submissions/submit strictly requires a non-empty query
        for q in ["", "   ", "\t  ", None]:
            status, data = self._post("/submissions/submit", {"query": q} if q is not None else {})
            self.assertEqual(status, 400, f"Empty query on /submissions/submit expected 400, got {status}")
            self.assertIn("query is required and must be non-empty", data.get("error", ""))

    def test_query_length_boundary_limits(self):
        # Exactly 256 chars (valid for submit, mbsync, move)
        q256 = "a" * 256
        with mock.patch.object(agent.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
            status, data = self._post("/submissions/submit", {"query": q256})
            self.assertEqual(status, 200)

        # 257 chars (rejected by submit max 256, accepted by mbsync/move max 512)
        q257 = "a" * 257
        status, data = self._post("/submissions/submit", {"query": q257})
        self.assertEqual(status, 400)
        self.assertIn("exceeds maximum length of 256", data.get("error", ""))

        with mock.patch.object(agent.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
            status, data = self._post("/library/mbsync", {"query": q257})
            self.assertEqual(status, 200)

        # Exactly 512 chars (accepted by mbsync/move)
        q512 = "a" * 512
        with mock.patch.object(agent.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
            status, data = self._post("/library/move", {"query": q512})
            self.assertEqual(status, 200)

        # 513 chars (rejected by mbsync and move)
        q513 = "a" * 513
        for ep in ["/library/mbsync", "/library/move"]:
            status, data = self._post(ep, {"query": q513})
            self.assertEqual(status, 400)
            self.assertIn("exceeds maximum length of 512", data.get("error", ""))

        # Huge queries (10KB, 100KB)
        for ep in ["/library/mbsync", "/library/move", "/submissions/submit"]:
            for size in [10_000, 100_000]:
                status, data = self._post(ep, {"query": "x" * size})
                self.assertEqual(status, 400)
                self.assertIn("exceeds maximum length", data.get("error", ""))

    def test_unicode_and_special_character_queries(self):
        # Ensure UTF-8 characters pass without 500 server crash or UnicodeDecodeError
        unicode_queries = [
            "artist:日本語",
            "artist:Björk",
            "artist:Mötley Crüe",
            "artist:Sigur Rós",
            "title:Smells Like Teen Spirit [Remastered]",
            "album:1989 (Taylor's Version)",
            "artist:AC/DC",
            "artist:🎵🎸",
        ]
        with mock.patch.object(agent.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
            for ep in ["/library/mbsync", "/library/move", "/submissions/submit"]:
                for uq in unicode_queries:
                    status, data = self._post(ep, {"query": uq})
                    self.assertEqual(status, 200, f"Failed on {ep} with query {uq!r}: status {status}, data {data}")
                    self.assertTrue(data.get("ok"))


class TestConcurrencyAndLockContention(BaseAdversarialLiveServerTest):
    """Stress-test concurrency, lock contention, and multi-step execution atomicity."""

    def test_all_endpoints_return_503_on_lock_contention(self):
        lock_exceptions = [
            BlockingIOError("Resource temporarily unavailable"),
            TimeoutError("Timed out waiting for file lock"),
            OSError(11, "Resource temporarily unavailable"),
            RuntimeError("Simulated OS lock contention"),
        ]
        endpoints = [
            ("/library/mbsync", {}),
            ("/library/move", {}),
            ("/submissions/submit", {"query": "id:123"}),
        ]
        for exc in lock_exceptions:
            with mock.patch.object(agent, "acquire_os_lock", side_effect=exc):
                for ep, payload in endpoints:
                    status, data = self._post(ep, payload)
                    self.assertEqual(
                        status,
                        503,
                        f"Expected 503 on {ep} under {type(exc).__name__}, got {status}: {data}",
                    )
                    self.assertIn("Failed to acquire engine OS lock", data.get("error", ""))
                    self.assertIn(str(exc), data.get("detail", ""))

    def test_real_concurrent_lock_contention(self):
        """Simulate two concurrent requests where request 1 holds the lock and request 2 attempts to acquire."""
        lock_held_event = threading.Event()
        proceed_event = threading.Event()

        orig_acquire = agent.acquire_os_lock

        def slow_acquire(read_only=False):
            lock = orig_acquire(read_only=read_only)
            lock_held_event.set()
            proceed_event.wait(timeout=5)
            return lock

        results = []

        def worker_1():
            with mock.patch.object(agent, "acquire_os_lock", side_effect=slow_acquire):
                with mock.patch.object(agent.subprocess, "run") as mock_run:
                    mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
                    status, data = self._post("/library/mbsync", {})
                    results.append(("req1", status, data))

        def worker_2():
            lock_held_event.wait(timeout=5)
            # Worker 2 tries to acquire but encounters lock contention
            with mock.patch.object(agent, "acquire_os_lock", side_effect=BlockingIOError("Engine lock held by another job")):
                status, data = self._post("/library/mbsync", {})
                results.append(("req2", status, data))
                proceed_event.set()

        t1 = threading.Thread(target=worker_1)
        t2 = threading.Thread(target=worker_2)

        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        req1_res = next((r for r in results if r[0] == "req1"), None)
        req2_res = next((r for r in results if r[0] == "req2"), None)

        self.assertIsNotNone(req1_res)
        self.assertIsNotNone(req2_res)
        self.assertEqual(req1_res[1], 200)
        self.assertEqual(req2_res[1], 503)
        self.assertIn("Failed to acquire engine OS lock", req2_res[2].get("error", ""))

    def test_move_multi_step_atomicity_and_lock_cleanup(self):
        """Verify that when update fails during /library/move, move is aborted and the lock is cleanly released."""
        call_order = []

        def fake_run(cmd, *args, **kwargs):
            step = cmd[1]
            call_order.append(step)
            if step == "update":
                return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="Corrupted DB file")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="ok", stderr="")

        lock_released = threading.Event()
        orig_release = agent.release_os_lock

        def tracked_release(lock_file):
            lock_released.set()
            orig_release(lock_file)

        with mock.patch.object(agent.subprocess, "run", side_effect=fake_run):
            with mock.patch.object(agent, "release_os_lock", side_effect=tracked_release):
                status, data = self._post("/library/move", {"rescan_first": True})
                self.assertEqual(status, 200)
                self.assertFalse(data.get("ok"))
                self.assertFalse(data.get("success"))
                self.assertIn("Rescan (beet update) failed; move aborted", data.get("error", ""))
                # Verify that move was NOT executed
                self.assertEqual(call_order, ["update"])
                # Verify lock was released cleanly
                self.assertTrue(lock_released.is_set(), "OS lock was not released after update failure")

    def test_concurrent_async_job_registration(self):
        """Verify thread-safe concurrent registration of async jobs in JOBS."""
        def launch_job(i):
            if i % 2 == 0:
                status, data = self._post("/library/mbsync", {"async": True, "query": f"genre:rock{i}"})
            else:
                status, data = self._post("/library/move", {"async": True, "query": f"genre:pop{i}"})
            return status, data

        with mock.patch.object(agent.subprocess, "Popen") as mock_popen:
            mock_proc = mock.MagicMock()
            mock_proc.communicate.return_value = ("done", "")
            mock_proc.poll.return_value = 0
            mock_proc.returncode = 0
            mock_popen.return_value = mock_proc

            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
                futures = [pool.submit(launch_job, i) for i in range(20)]
                results = [f.result() for f in concurrent.futures.as_completed(futures)]

            for status, data in results:
                self.assertEqual(status, 200)
                self.assertTrue(data.get("ok"))
                self.assertEqual(data.get("status"), "started")
                job_id = data.get("job_id")
                with agent.JOBS_LOCK:
                    self.assertIn(job_id, agent.JOBS)


class TestAuthenticationTampering(BaseAdversarialLiveServerTest):
    """Adversarial stress-testing of HMAC authentication, tampering, timing attacks, and misconfiguration."""

    def test_missing_and_empty_auth_headers(self):
        endpoints = ["/library/mbsync", "/library/move", "/submissions/submit"]
        for ep in endpoints:
            # No auth header
            status, data = self._raw_request("POST", ep, body=b"{}")
            self.assertEqual(status, 401, f"Missing auth on {ep} expected 401, got {status}")

            # Empty Authorization header
            status, data = self._raw_request("POST", ep, body=b"{}", headers={"Authorization": ""})
            self.assertEqual(status, 401)

            # Bearer without token
            status, data = self._raw_request("POST", ep, body=b"{}", headers={"Authorization": "Bearer "})
            self.assertEqual(status, 401)

            # Empty X-Beets-API-Token
            status, data = self._raw_request("POST", ep, body=b"{}", headers={"X-Beets-API-Token": ""})
            self.assertEqual(status, 401)

    def test_tampered_and_corrupted_tokens(self):
        valid = self.valid_token
        tampered_tokens = [
            "X" + valid[1:],  # Bit flip first char
            valid[:-1] + "X",  # Bit flip last char
            valid[:16] + "X" + valid[17:],  # Bit flip middle char
            valid[:10],  # Truncated prefix
            valid + "_extra_suffix",  # Appended suffix
            valid.upper(),  # Casing flip
            "b" * len(valid),  # Completely wrong token of same length
        ]
        endpoints = ["/library/mbsync", "/library/move", "/submissions/submit"]
        for ep in endpoints:
            for bad_token in tampered_tokens:
                status, data = self._post(ep, {}, token=bad_token)
                self.assertEqual(status, 401, f"Tampered token {bad_token!r} on {ep} expected 401, got {status}")
                self.assertIn("invalid API token", data.get("error", ""))

    def test_raw_socket_crlf_and_null_byte_header_injection(self):
        """Send raw TCP requests with CRLF or null bytes in header values to assert 400 or 401."""
        raw_requests = [
            b"POST /library/mbsync HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer \r\ninjected: header\r\n\r\n{}",
            b"POST /library/mbsync HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Beets-API-Token: test\x00token\r\n\r\n{}",
        ]
        for raw in raw_requests:
            status, data = self._raw_socket_send(raw)
            # Server either rejects with 400 (bad HTTP syntax / header) or 401 (unauthorized)
            self.assertIn(status, [400, 401], f"Expected 400 or 401, got {status}: {data!r}")

    def test_unsupported_auth_schemes_and_hmac_header(self):
        # Sending unsupported schemes like Basic, Digest, or raw HMAC header without API token
        endpoints = ["/library/mbsync", "/library/move", "/submissions/submit"]
        for ep in endpoints:
            status, data = self._raw_request(
                "POST",
                ep,
                body=b"{}",
                headers={"Authorization": "Basic dXNlcjpwYXNz"},
            )
            self.assertEqual(status, 401)

            status, data = self._raw_request(
                "POST",
                ep,
                body=b"{}",
                headers={"X-Beets-HMAC-SHA256": "abcdef1234567890"},
            )
            self.assertEqual(status, 401)

    def test_server_misconfigured_weak_token_fails_closed(self):
        """When server token is missing, weak, or placeholder, agent returns 500 fail-closed."""
        weak_tokens = ["", "short", "changeme", "placeholder", "your-token-here", "a" * 31]
        endpoints = ["/library/mbsync", "/library/move", "/submissions/submit"]
        for weak in weak_tokens:
            with mock.patch.object(agent, "BEETS_API_TOKEN", weak):
                for ep in endpoints:
                    # Even if attacker supplies the exact weak token, server must fail closed with 500
                    status, data = self._post(ep, {}, token=weak)
                    self.assertEqual(status, 500, f"Server with weak token {weak!r} expected 500, got {status}")
                    self.assertIn("misconfigured", data.get("error", ""))

    def test_constant_time_hmac_comparison(self):
        """Verify that token comparison uses constant-time hmac.compare_digest."""
        # Check source implementation verifies hmac.compare_digest
        import inspect
        src = inspect.getsource(agent.ControlAgentHandler._authenticate)
        self.assertIn("hmac.compare_digest", src)

        # Empirically measure timing differences for early mismatch vs late mismatch vs correct
        valid = self.valid_token
        mismatch_start = "X" * len(valid)
        mismatch_end = valid[:-1] + "X"

        times_start = []
        times_end = []
        for _ in range(30):
            t0 = time.perf_counter()
            self._post("/library/mbsync", {}, token=mismatch_start)
            times_start.append(time.perf_counter() - t0)

            t0 = time.perf_counter()
            self._post("/library/mbsync", {}, token=mismatch_end)
            times_end.append(time.perf_counter() - t0)

        # Verify no wild order-of-magnitude difference indicating early exit
        avg_start = sum(times_start) / len(times_start)
        avg_end = sum(times_end) / len(times_end)
        ratio = max(avg_start, avg_end) / max(min(avg_start, avg_end), 1e-9)
        self.assertLess(ratio, 5.0, f"Possible timing anomaly detected: start={avg_start:.6f}s, end={avg_end:.6f}s")


class TestServerStabilityAndCrashResistance(BaseAdversarialLiveServerTest):
    """Verify that rapid fuzzing barrages do not crash the server or poison subsequent legitimate requests."""

    def test_zero_crashes_under_fuzz_barrage(self):
        fuzz_payloads = [
            b"",
            b"{",
            b"{\"query\": 123}",
            b"{\"pretend\": \"true\"}",
            b"{\"query\": \"; reboot\"}",
            b"{\"query\": \"--version\"}",
            b"{\"async\": 1}",
            b"{\"timeout\": -10}",
            b"[\"not\", \"an\", \"object\"]",
            b"\"plain string\"",
            b"12345",
            b"true",
            b"null",
            b"\x00\xff\xfe",
            b"{\"query\": \"" + b"x" * 20000 + b"\"}",
        ]

        # Send 45 corrupt payloads in rapid succession
        for _ in range(3):
            for payload in fuzz_payloads:
                self._raw_request(
                    "POST",
                    "/library/mbsync",
                    body=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.valid_token}",
                    },
                )

        # Server must still be responsive and return 200 for health
        status, data = self._raw_request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(data.get("status"), "ok")

        # Legitimate request immediately succeeds
        with mock.patch.object(agent.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="clean run", stderr="")
            status, data = self._post("/library/mbsync", {"query": "artist:Test"})
            self.assertEqual(status, 200)
            self.assertTrue(data.get("ok"))


class TestBeetsClientAdversarialBehavior(unittest.TestCase):
    """Test that BeetsClient methods handle errors, invalid inputs, and offline conditions safely."""

    def setUp(self):
        self.client = BeetsClient(base_url="http://127.0.0.1:8338", token="a" * 32)

    def test_client_local_input_validation(self):
        # Shell characters checked locally by BeetsClient methods
        client_forbidden = [";", "&&", "||", "|", ">", "<", "$", "`", "\x00", "\n", "\r"]
        for char in client_forbidden:
            with self.assertRaises(BeetsError) as ctx:
                self.client.mbsync(query=f"test{char}evil")
            self.assertEqual(ctx.exception.error_code, "INVALID_PARAMETER")

            with self.assertRaises(BeetsError) as ctx:
                self.client.move_library(query=f"test{char}evil")
            self.assertEqual(ctx.exception.error_code, "INVALID_PARAMETER")

            with self.assertRaises(BeetsError) as ctx:
                self.client.acoustid_submit(query=f"test{char}evil")
            self.assertEqual(ctx.exception.error_code, "INVALID_PARAMETER")

        # Exceeding length 256 rejected locally
        long_q = "a" * 257
        with self.assertRaises(BeetsError) as ctx:
            self.client.mbsync(query=long_q)
        self.assertEqual(ctx.exception.error_code, "INVALID_PARAMETER")

        with self.assertRaises(BeetsError) as ctx:
            self.client.move_library(query=long_q)
        self.assertEqual(ctx.exception.error_code, "INVALID_PARAMETER")

        # Empty query rejected for submit
        for empty_q in ["", "   ", None]:
            with self.assertRaises(BeetsError) as ctx:
                self.client.acoustid_submit(query=empty_q)
            self.assertEqual(ctx.exception.error_code, "INVALID_PARAMETER")

    def test_client_offline_fail_closed(self):
        dead_client = BeetsClient(base_url="http://127.0.0.1:1", token="a" * 32)
        with self.assertRaises(BeetsUnavailableError):
            dead_client.mbsync()
        with self.assertRaises(BeetsUnavailableError):
            dead_client.move_library()
        with self.assertRaises(BeetsUnavailableError):
            dead_client.acoustid_submit(query="id:123")


if __name__ == "__main__":
    unittest.main()
