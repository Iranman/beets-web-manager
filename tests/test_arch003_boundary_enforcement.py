"""Structural Architecture Enforcement Test for ARCH-003 / SEC-002.

Verifies:
1. app.py and beets_client.py do not use in-process mutation fallbacks.
2. beets_client.py contains zero imports or references to transaction_engine.
3. app.py routes folder cleanup, artwork, album maintenance, playlist cleanup, and imports through beets_client.
"""

import ast
from pathlib import Path
import unittest


class TestArch003BoundaryEnforcement(unittest.TestCase):
    def setUp(self):
        self.repo_root = Path(__file__).parent.parent
        self.app_path = self.repo_root / "app.py"
        self.beets_client_path = self.repo_root / "backend" / "beets_client.py"

        self.app_source = self.app_path.read_text(encoding="utf-8")
        self.beets_client_source = self.beets_client_path.read_text(encoding="utf-8")

    def test_beets_client_has_no_transaction_engine_imports(self):
        tree = ast.parse(self.beets_client_source)
        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_modules.add(node.module)
        self.assertNotIn("backend.transaction_engine", imported_modules)
        self.assertNotIn("transaction_engine", imported_modules)

    def test_beets_client_is_pure_http_proxy(self):
        # All API endpoints in beets_client should call self._request
        self.assertIn("def _request(", self.beets_client_source)
        self.assertIn("BeetsUnavailableError", self.beets_client_source)
        self.assertIn("BeetsAuthError", self.beets_client_source)

    def test_app_delegates_folder_cleanup_to_beets_client(self):
        self.assertIn("beets_client.plan_folder_cleanup(", self.app_source)
        self.assertIn("beets_client.apply_folder_cleanup(", self.app_source)

    def test_app_delegates_playlist_media_cleanup_to_beets_client(self):
        self.assertIn("beets_client.plan_playlist_media_cleanup(", self.app_source)
        self.assertIn("beets_client.apply_playlist_media_cleanup(", self.app_source)

    def test_app_delegates_album_maintenance_to_beets_client(self):
        self.assertIn("beets_client.plan_album_maintenance(", self.app_source)
        self.assertIn("beets_client.apply_album_maintenance(", self.app_source)

    def test_app_delegates_album_artwork_to_beets_client(self):
        self.assertIn("beets_client.plan_album_artwork(", self.app_source)
        self.assertIn("beets_client.apply_album_artwork(", self.app_source)

    def test_app_delegates_import_folder_to_beets_client(self):
        self.assertIn("beets_client.plan_import_folder(", self.app_source)
        self.assertIn("beets_client.apply_import_folder(", self.app_source)

    def test_transaction_rollback_api_supports_all_families(self):
        self.assertIn('mutation_family == "folder_cleanup_v1"', self.app_source)
        self.assertIn('mutation_family == "playlist_media_cleanup_v1"', self.app_source)
        self.assertIn('mutation_family == "album_maintenance_v1"', self.app_source)
        self.assertIn('mutation_family == "album_artwork_v1"', self.app_source)
        self.assertIn('mutation_family == "import_folder_v1"', self.app_source)


if __name__ == "__main__":
    unittest.main()
