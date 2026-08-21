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

    def test_beets_client_class_has_no_local_filesystem_mutation_calls(self):
        """SEC-002 / ARCH-003 Wave 24 final review (CRITICAL): a submitted
        revision of replace_album_art()/delete_album_art() decoded image
        bytes and wrote a staging file directly to this container's own
        local disk (`stg_base.mkdir()`, `temp_art.write_bytes()`), then
        probed local `Path.exists()`/`Path.is_file()` for candidate art
        files -- both silently reintroducing exactly the local-mutation
        fallback test_beets_client_is_pure_http_proxy's substring checks
        above were never actually strong enough to catch (it only checks
        that `_request`/error classes exist *somewhere* in the file, not
        that every method actually routes through them). This walks the
        real `BeetsClient` class body and proves no filesystem mutation
        call shape appears anywhere in it -- every method must be pure
        computation plus HTTP, matching the two-service architecture
        where only the engine container touches the media filesystem."""
        tree = ast.parse(self.beets_client_source)
        client_class = next(
            (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "BeetsClient"),
            None,
        )
        self.assertIsNotNone(client_class, "BeetsClient class not found in beets_client.py")

        banned_calls = {
            "mkdir", "makedirs", "unlink", "remove", "rename", "replace",
            "move", "rmdir", "removedirs", "rmtree", "copy", "copy2",
            "copyfile", "copytree", "write_text", "write_bytes", "touch",
        }
        found = []
        for node in ast.walk(client_class):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else (
                    node.func.id if isinstance(node.func, ast.Name) else None)
                if name in banned_calls:
                    found.append(f"call:{name}@{node.lineno}")
                elif isinstance(node.func, ast.Name) and node.func.id == "open":
                    found.append(f"call:open@{node.lineno}")
        self.assertEqual(found, [], f"found prohibited local-filesystem-mutation call node(s) in BeetsClient: {found}")

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

        src = self._function_source("import_folder_with_id")
        self.assertNotIn('base_import + ["import"', src)
        self.assertNotIn("subprocess.run(\n                base_import", src)

    def test_album_cleanup_apply_issue_has_no_direct_mutation_calls(self):
        """Wave 23 final review, finding #23: the original version of this
        test used source-text substring markers (`.mkdir(`, `shutil.move(`,
        ...), which is exactly the pattern the review brief flags as easy
        to evade (aliasing, helper indirection, getattr, reformatting) and
        also structurally fragile to false positives from comment prose
        (see the Wave 21/22 review history of this same test file, where an
        explanatory comment mentioning "shutil.move" in prose tripped a
        substring check). Rewritten to walk the actual AST: real `ast.Call`
        nodes matching the mutation call shapes, a real `ast.Name` load of
        `BEET_BIN`, and a real `with _db(...)` statement -- not text."""
        src = self._function_source("_album_cleanup_apply_issue")
        tree = ast.parse(src)
        found = []

        banned_calls = {
            "mkdir", "makedirs", "unlink", "remove", "rename", "replace",
            "move", "rmdir", "removedirs", "rmtree", "copy", "copy2",
            "copyfile", "copytree", "write_text", "write_bytes",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else (
                    node.func.id if isinstance(node.func, ast.Name) else None)
                if name in banned_calls:
                    found.append(f"call:{name}@{node.lineno}")
                elif isinstance(node.func, ast.Name) and node.func.id == "open":
                    found.append(f"call:open@{node.lineno}")
            elif isinstance(node, ast.Name) and node.id == "BEET_BIN":
                found.append(f"name:BEET_BIN@{node.lineno}")
            elif isinstance(node, ast.With):
                for item in node.items:
                    ctx = item.context_expr
                    if isinstance(ctx, ast.Call) and isinstance(ctx.func, ast.Name) and ctx.func.id == "_db":
                        found.append(f"with:_db@{node.lineno}")
        self.assertEqual(found, [], f"found prohibited local-mutation call node(s) in _album_cleanup_apply_issue: {found}")

    def test_mutation_inventory_domain_accounting_gate(self):
        """Wave 24: Verify every inventory entry carries a domain field and missing domain fails CI."""
        import json, tempfile
        from scripts.verify_arch003_mutation_inventory import verify_mutation_inventory

        self.assertTrue(verify_mutation_inventory(self.repo_root, check_mode=True))

        inv_path = self.repo_root / "security" / "arch003_mutation_inventory.json"
        data = json.loads(inv_path.read_text(encoding="utf-8"))
        for entry in data.get("inventory", []):
            self.assertTrue(bool(entry.get("domain")), f"entry {entry.get('key')} has missing or empty domain field")

        # Prove domain gate fails if domain is stripped from an ARCH003_BLOCKER sink
        if data.get("inventory"):
            test_data = dict(data)
            test_data["inventory"] = [dict(e) for e in data["inventory"]]
            blocker = next((e for e in test_data["inventory"] if e.get("classification") == "ARCH003_BLOCKER"), test_data["inventory"][0])
            blocker["classification"] = "ARCH003_BLOCKER"
            blocker["domain"] = ""
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                sec_dir = tmp_path / "security"
                sec_dir.mkdir()
                (sec_dir / "arch003_mutation_inventory.json").write_text(json.dumps(test_data), encoding="utf-8")
                (tmp_path / "backend").mkdir()
                (tmp_path / "backend" / "transaction_engine.py").write_text((self.repo_root / "backend" / "transaction_engine.py").read_text(encoding="utf-8"), encoding="utf-8")
                self.assertFalse(verify_mutation_inventory(tmp_path, check_mode=True))
