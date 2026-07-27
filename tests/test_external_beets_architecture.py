"""Unit and integration tests for the external Beets control agent architecture."""

import importlib.util
import json
import os
import re
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from backend.beets_client import (
    BeetsAuthError,
    BeetsClient,
    BeetsError,
    BeetsUnavailableError,
    RemoteLibrary,
    get_db_connection,
)
from backend.beets_control_agent import (
    ControlAgentHandler,
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
                # Symlink target is secret.txt which is outside music_fake
                self.assertFalse(is_safe_path(str(symlink), ["music"]))

    def test_auth_token_fail_closed(self):
        with mock.patch("backend.beets_control_agent.BEETS_API_TOKEN", ""):
            handler = mock.MagicMock(spec=ControlAgentHandler)
            handler.headers = {}
            result = ControlAgentHandler._authenticate(handler)
            self.assertFalse(result)

    def test_raw_query_rejects_mutations(self):
        """Verify that raw_query rejects write operations."""
        forbidden_queries = [
            "UPDATE items SET title = 'hacked'",
            "DELETE FROM albums",
            "DROP TABLE items",
            "INSERT INTO items (title) VALUES ('bad')",
            "ATTACH DATABASE '/tmp/db.sqlite' AS aux",
            "PRAGMA user_version = 10",
        ]
        agent_code = (ROOT / "backend" / "beets_control_agent.py").read_text(encoding="utf-8")
        self.assertIn("Forbidden statement type: raw_query endpoint is strictly read-only SELECT queries", agent_code)


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
        """Assert that RemoteLibrary raises BeetsError for unsupported queries."""
        client = mock.MagicMock(spec=BeetsClient)
        rlib = RemoteLibrary(client)

        with self.assertRaises(BeetsError):
            rlib.items({"unsupported": "query"})

        with self.assertRaises(BeetsError):
            rlib.albums(12345)

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


if __name__ == "__main__":
    unittest.main()
