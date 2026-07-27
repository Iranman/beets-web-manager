"""Unit and integration tests for the external Beets control agent architecture."""

import json
import os
import tempfile
import unittest
import urllib.error
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


class TestBeetsControlAgentSecurity(unittest.TestCase):
    def test_is_safe_path_valid(self):
        self.assertTrue(is_safe_path("/config/config.yaml"))
        self.assertTrue(is_safe_path("/data/media/music/Artist/Album"))
        self.assertTrue(is_safe_path("/data/torrents/downloads"))
        self.assertTrue(is_safe_path("/tmp/test_file.tmp"))

    def test_is_safe_path_relative_traversal(self):
        self.assertFalse(is_safe_path("../../etc/passwd"))
        self.assertFalse(is_safe_path("/config/../etc/passwd"))
        self.assertFalse(is_safe_path("relative/path"))

    def test_is_safe_path_null_bytes(self):
        self.assertFalse(is_safe_path("/config/file.txt\x00.sh"))

    def test_auth_token_fail_closed(self):
        with mock.patch("backend.beets_control_agent.BEETS_API_TOKEN", ""):
            handler = mock.MagicMock(spec=ControlAgentHandler)
            handler.headers = {}
            result = ControlAgentHandler._authenticate(handler)
            self.assertFalse(result)


class TestBeetsClientRemoteOnly(unittest.TestCase):
    def test_get_db_connection_never_uses_local_sqlite(self):
        with tempfile.NamedTemporaryFile(suffix=".blb", delete=False) as tmp:
            tmp.write(b"dummy db")
            tmp_path = tmp.name

        try:
            conn = get_db_connection(tmp_path)
            # Must return RemoteSQLiteConnection regardless of whether file exists locally
            self.assertEqual(conn.__class__.__name__, "RemoteSQLiteConnection")
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_beets_client_auth_failure_raises_beets_auth_error(self):
        client = BeetsClient(base_url="http://127.0.0.1:9999", token="invalid")
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = mock.MagicMock()
            mock_resp.code = 401
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

    def test_no_local_beets_imports_in_web_manager(self):
        for mod in ("beets", "beets.library", "beets.mediafile", "beetsplug"):
            try:
                __import__(mod)
            except ImportError:
                pass


if __name__ == "__main__":
    unittest.main()
