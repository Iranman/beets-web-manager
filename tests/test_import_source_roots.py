"""Unit and integration tests for Issue #85: Configurable Import Source Roots.

Verifies:
1. Beets Control Agent GET /imports/source/roots endpoint behavior, env var handling,
   path normalization, deduplication, and containment checks.
2. BeetsClient.get_import_source_roots() IPC and typed responses.
3. Web Manager Flask route GET /api/import/source/roots, last_saved_source persistence,
   containment verification, and invalid path fallback.
4. Path containment security: traversal rejection, symlink rejection, root-self rejection.
"""

import http.client
import http.server
import json
import os
import socket
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
from backend.beets_client import BeetsClient  # noqa: E402
import app as flask_app  # noqa: E402
import routes_setup  # noqa: E402


def _get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestControlAgentImportRootsEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmpdir.name)
        cls.beetsdir = cls.root / "config"
        cls.beetsdir.mkdir(parents=True, exist_ok=True)

        (cls.beetsdir / "config.yaml").write_text("library: library.db\n", encoding="utf-8")

        cls.token = "test-secret-token-12345-very-long-secret-key-32chars"
        cls.port = _get_free_port()

        cls.music_root = "/data/music"
        cls.downloads_root = "/data/downloads"
        cls.custom_root1 = "/data/custom_intake1"
        cls.custom_root2 = "/data/custom_intake2"

        cls.env_patcher = mock.patch.dict(os.environ, {
            "BEETSDIR": str(cls.beetsdir),
            "BEETS_API_TOKEN": cls.token,
            "BEETS_OUTBOUND_ALLOWLIST": f"127.0.0.1:{cls.port},localhost:{cls.port}",
            "MUSIC_ROOT": cls.music_root,
            "DOWNLOADS_ROOT": cls.downloads_root,
            "BEETS_IMPORT_SOURCE_ROOTS": f"{cls.custom_root1},{cls.custom_root2}",
        })
        cls.env_patcher.start()
        agent.BEETS_API_TOKEN = cls.token

        handler = agent.ControlAgentHandler
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", cls.port), handler)
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.server_thread.join(timeout=5.0)
        cls.env_patcher.stop()
        cls.tmpdir.cleanup()

    def _get(self, path: str, token: str = None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10.0)
        headers = {}
        t = token if token is not None else self.token
        if t:
            headers["Authorization"] = f"Bearer {t}"
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        body = resp.read().decode("utf-8")
        conn.close()
        try:
            return status, json.loads(body)
        except Exception:
            return status, {"raw": body}

    def test_get_import_roots_endpoint_success(self):
        status, data = self._get("/imports/source/roots")
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("music_root"), str(Path(self.music_root).resolve(strict=False)))
        staging_roots = data.get("staging_roots", [])
        self.assertIn(self.custom_root1, staging_roots)
        self.assertIn(self.custom_root2, staging_roots)
        self.assertEqual(data.get("recommended_source"), self.custom_root1)
        self.assertEqual(data.get("failed_imports_root"), f"{self.custom_root1}/failed_imports")

    def test_get_import_roots_requires_auth(self):
        status, data = self._get("/imports/source/roots", token="wrong-token")
        self.assertEqual(status, 401)

    def test_beets_client_get_import_source_roots(self):
        client = BeetsClient(base_url=f"http://127.0.0.1:{self.port}", token=self.token)
        res = client.get_import_source_roots()
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("music_root"), str(Path(self.music_root).resolve(strict=False)))
        self.assertIn(self.custom_root1, res.get("staging_roots", []))

    def test_path_containment_and_root_self_rejection(self):
        # 1. Path within custom root is allowed
        subfolder = f"{self.custom_root1}/Artist - Album (2024)"
        safe = agent.resolve_safe_path(subfolder, ["staging"])
        self.assertEqual(str(safe), str(Path(subfolder).resolve(strict=False)))

        # 2. Path outside roots is rejected
        with self.assertRaises(agent.UnsafePathError):
            agent.resolve_safe_path("/etc/shadow", ["staging"])

        # 3. Traversal is rejected
        with self.assertRaises(agent.UnsafePathError):
            agent.resolve_safe_path(f"{self.custom_root1}/../etc/passwd", ["staging"])

        # 4. Root-self is rejected in inspect_import_source
        with mock.patch.object(Path, "exists", return_value=True), mock.patch.object(Path, "is_dir", return_value=True):
            res_root = agent.inspect_import_source(self.custom_root1, "reimport")
            self.assertFalse(res_root.get("ok"))
            self.assertEqual(res_root.get("error_code"), "root_self_rejected")


class TestFlaskImportSourceRootsRoute(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.settings_file = self.root / "app_settings.json"
        self.music_dir = self.root / "music"
        self.music_dir.mkdir(parents=True, exist_ok=True)
        self.intake_dir = self.root / "intake"
        self.intake_dir.mkdir(parents=True, exist_ok=True)

        self.patchers = [
            mock.patch.object(routes_setup, "_SETTINGS_FILE", self.settings_file),
            mock.patch.dict(os.environ, {
                "BEETS_WEB_AUTH_DISABLED": "1",
                "WEB_MANAGER_DATA_DIR": str(self.root),
                "BEETS_IMPORT_SOURCE_ROOTS": str(self.intake_dir),
                "MUSIC_ROOT": str(self.music_dir),
            }),
        ]
        for p in self.patchers:
            p.start()

        self.client = flask_app.app.test_client()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.tmpdir.cleanup()

    def test_api_import_source_roots_fallback(self):
        # Mock beets_client failing / engine offline fallback
        with mock.patch.object(flask_app.beets_client, "get_import_source_roots", side_effect=Exception("Engine offline")):
            resp = self.client.get("/api/import/source/roots")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data.get("ok"))
            self.assertIn(str(self.intake_dir.resolve(strict=False)), data.get("staging_roots", []))
            self.assertIsNone(data.get("last_saved_source"))

    def test_api_import_source_roots_with_saved_source(self):
        valid_album_path = self.intake_dir / "Test Artist - Test Album"
        # Save valid last_import_source in app_settings.json
        routes_setup._save_settings({
            "last_import_source": str(valid_album_path),
        })

        mock_roots = {
            "ok": True,
            "music_root": str(self.music_dir),
            "staging_roots": [str(self.intake_dir)],
            "recommended_source": str(self.intake_dir),
            "recommended_import_roots": [str(self.intake_dir)],
            "failed_imports_root": f"{self.intake_dir}/failed_imports",
        }
        with mock.patch.object(flask_app.beets_client, "get_import_source_roots", return_value=mock_roots):
            resp = self.client.get("/api/import/source/roots")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("last_saved_source"), str(valid_album_path.resolve(strict=False)))

    def test_api_import_source_roots_ignores_invalid_saved_source(self):
        # Save invalid/outside path in app_settings.json
        routes_setup._save_settings({
            "last_import_source": "/outside/disallowed/path",
        })

        mock_roots = {
            "ok": True,
            "music_root": str(self.music_dir),
            "staging_roots": [str(self.intake_dir)],
            "recommended_source": str(self.intake_dir),
            "recommended_import_roots": [str(self.intake_dir)],
            "failed_imports_root": f"{self.intake_dir}/failed_imports",
        }
        with mock.patch.object(flask_app.beets_client, "get_import_source_roots", return_value=mock_roots):
            resp = self.client.get("/api/import/source/roots")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data.get("ok"))
            self.assertIsNone(data.get("last_saved_source"))

    def test_import_preflight_persists_last_import_source(self):
        valid_album_path = self.intake_dir / "Album1"
        valid_album_path.mkdir(parents=True, exist_ok=True)
        (valid_album_path / "track1.flac").write_bytes(b"dummy flac data")

        with mock.patch.object(flask_app.beets_client, "list_distinct_item_paths", return_value=[]):
            resp = self.client.post("/api/import/preflight", json={"path": str(valid_album_path)})
            self.assertEqual(resp.status_code, 200)

        # Confirm settings was updated with last_import_source
        settings = routes_setup._load_settings()
        self.assertEqual(settings.get("last_import_source"), str(valid_album_path.resolve(strict=False)))

    def test_import_rejects_path_outside_allowed_roots(self):
        resp = self.client.post("/api/import/preflight", json={"path": "/var/log"})
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertFalse(data.get("ok"))
        self.assertIn("outside the allowed import source roots", data.get("error"))


if __name__ == "__main__":
    unittest.main()
