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

    # SEC-002 / ARCH-003 Wave 22 final review, findings #39/#40: an
    # `assertIn("beets_client.plan_xxx(", app_source)` proves only that
    # the engine call exists *somewhere* in the file -- it says nothing
    # about whether the legacy local mutation it was supposed to replace
    # is actually gone, which is exactly how a real local subprocess
    # import and a real local artwork-move fallback both survived
    # alongside a passing version of this same test in the original
    # submission. Prove absence, not just presence, for the functions
    # this review actually closed the loop on.

    _NO_DIRECT_MUTATION_MARKERS = (
        "shutil.move(", "shutil.rmtree(", "shutil.copy(", "shutil.copy2(",
        ".unlink(", "os.rename(", "os.remove(", "os.replace(",
        "TransactionStore(", "transaction_engine.",
        "execute_album_artwork_apply(", "create_album_artwork_plan(",
    )

    def _function_source(self, name: str) -> str:
        tree = ast.parse(self.app_source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return ast.get_source_segment(self.app_source, node) or ""
        self.fail(f"{name} not found in app.py")

    def test_move_artwork_to_target_contains_no_direct_mutations(self):
        """SEC-002 Wave 22 final review, finding #11 (CRITICAL): this
        function previously fell through to a full local
        shutil.move-based implementation whenever the engine call failed
        or reported non-ok -- a second, parallel, unmigrated mutation
        path in production. It must now delegate exclusively to the
        engine and fail closed (no local move) on any engine failure."""
        src = self._function_source("_move_artwork_to_target")
        for marker in self._NO_DIRECT_MUTATION_MARKERS:
            self.assertNotIn(marker, src, f"found prohibited local-mutation marker {marker!r} in _move_artwork_to_target")

    def test_import_folder_local_mutation_is_a_documented_known_exception(self):
        """SEC-002 Wave 22 final review, finding #4/#6 (CRITICAL, NOT
        fully closed this wave): import_folder_with_id still performs the
        real Beets import via a local `subprocess.run`, because
        import_folder_v1's engine-side Apply has no real Beets import
        mechanism wired in yet (finding #6) -- and it would be worse to
        delete the only working import path than to leave it running
        locally with this fact documented and tested for, rather than
        silently passing a same-shape assertIn check that proves nothing
        about whether the legacy path was actually removed. This test
        exists so that closing that gap requires deliberately updating
        this test, not accidentally leaving it stale."""
        src = self._function_source("import_folder_with_id")
        self.assertIn("still runs locally", src)
        self.assertIn('base_import + ["import"', src)
