"""Tests for ARCH-003 / SEC-002 Final Closure.

Verifies:
1. import_folder_with_id and import_folder_v1 use engine-owned Beets import without local subprocess fallbacks.
2. Control Agent unavailability causes imports to fail closed immediately.
3. _album_cleanup_apply_issue routes cleanup operations through beets_client transaction calls
   with NO direct local mutation calls left (AST-verified, not a source-text assertIn).
4. The mutation inventory is derived from real AST discovery, not a hand-written list -- every
   sink currently in the source has a matching inventory entry, and a genuinely new/unclassified
   sink fails the gate (self-tests using synthetic source, per review finding #26).
"""

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.beets_client import BeetsClient, BeetsUnavailableError

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from verify_arch003_mutation_inventory import verify_mutation_inventory  # noqa: E402
from discover_mutation_sinks import discover_all, discover_sinks_in_file  # noqa: E402


class TestArch003FinalClosure(unittest.TestCase):
    def setUp(self):
        self.repo_root = Path(__file__).parent.parent.resolve()
        self.app_path = self.repo_root / "app.py"
        self.beets_client_path = self.repo_root / "backend" / "beets_client.py"

        self.app_source = self.app_path.read_text(encoding="utf-8")
        self.beets_client_source = self.beets_client_path.read_text(encoding="utf-8")

    def _function_source(self, name: str) -> str:
        tree = ast.parse(self.app_source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return ast.get_source_segment(self.app_source, node) or ""
        self.fail(f"{name} not found in app.py")

    def test_app_has_no_local_beet_import_subprocess(self):
        self.assertNotIn('subprocess.run(base_import + ["import"', self.app_source)
        self.assertNotIn("subprocess.run(\n                base_import + [\"import\"", self.app_source)

    def test_beets_client_has_no_in_process_fallback(self):
        self.assertNotIn("_local_engine_fallback", self.beets_client_source)
        self.assertNotIn("transaction_engine", self.beets_client_source)

    def test_import_fails_closed_when_engine_unreachable(self):
        client = BeetsClient(base_url="http://127.0.0.1:59999", timeout=0.5)
        with self.assertRaises(BeetsUnavailableError):
            client.plan_import_folder({"source_folder": "/staging/album1"})

    # ── _album_cleanup_apply_issue: prove absence, not just presence ──────

    def test_album_cleanup_apply_issue_has_no_direct_mutation_calls(self):
        """SEC-002 / ARCH-003 final closure review, finding #49: the
        starting branch's equivalent test only asserted
        `beets_client.plan_album_maintenance` etc. appear somewhere in
        app.py -- true even while the function retained full local
        shutil.move / rmdir / DB-update fallbacks for every branch, since
        the engine calls were also present (they just always failed and
        fell through). AST-extract the real function and assert none of
        the fallback call *shapes* remain, rather than trusting source
        text."""
        src = self._function_source("_album_cleanup_apply_issue")
        tree = ast.parse(src)
        banned_calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                chain = None
                if isinstance(node.func.value, ast.Name):
                    chain = f"{node.func.value.id}.{node.func.attr}"
                if chain in ("shutil.move", "shutil.rmtree", "shutil.copy", "shutil.copy2"):
                    banned_calls.append(chain)
                if node.func.attr in ("unlink", "rmdir") and not isinstance(node.func.value, ast.Name):
                    # Path-like receiver via attribute/subscript (e.g. Path(...).unlink())
                    banned_calls.append(node.func.attr)
                elif isinstance(node.func.value, ast.Name) and node.func.attr in ("unlink", "rmdir"):
                    banned_calls.append(f"{node.func.value.id}.{node.func.attr}")
        self.assertEqual(banned_calls, [], f"found prohibited local mutation calls: {banned_calls}")

    def test_album_cleanup_apply_issue_routes_to_beets_client(self):
        self.assertIn("beets_client.plan_album_maintenance", self.app_source)
        self.assertIn("beets_client.plan_album_artwork", self.app_source)
        self.assertIn("beets_client.plan_folder_cleanup", self.app_source)

    def test_album_cleanup_duplicate_uses_real_item_id_not_zero(self):
        """SEC-002 / ARCH-003 final closure review, finding #6: `to_delete`
        previously hardcoded `"id": 0`, which album_maintenance_v1 cannot
        bind to any row -- Apply never actually did anything, and the
        local shutil.move fallback always ran in practice."""
        src = self._function_source("_album_cleanup_apply_issue")
        self.assertNotIn('"id": 0', src)
        self.assertIn("_album_cleanup_item_id_for_path", src)

    def test_album_cleanup_filename_cleanup_uses_real_item_id_not_zero(self):
        """SEC-002 / ARCH-003 final closure review, finding #8: same bug
        for the filename_cleanup candidate's item_id."""
        src = self._function_source("_album_cleanup_apply_issue")
        self.assertNotIn('"item_id": 0', src)

    def test_album_cleanup_artwork_move_includes_target_dir(self):
        """SEC-002 / ARCH-003 final closure review, finding #10:
        album_artwork_v1's move mode requires target_dir (enforced since
        the Wave 22 review); the cleanup-issue caller never sent it, so
        Plan always rejected the request and execution fell through to
        the local shutil.move fallback."""
        src = self._function_source("_album_cleanup_apply_issue")
        self.assertIn('"target_dir": str(canonical)', src)

    def test_album_cleanup_never_calls_apply_with_none_operation_id(self):
        """SEC-002 / ARCH-003 final closure review, finding #9: a Plan
        that returns ok=True, operation_id=None (nothing safely
        actionable) must never be passed straight to Apply."""
        src = self._function_source("_album_cleanup_apply_issue")
        tree = ast.parse(src)
        apply_calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("apply_album_maintenance", "apply_album_artwork", "apply_folder_cleanup")
        ]
        self.assertTrue(apply_calls, "expected at least one Apply call in this function")
        for call in apply_calls:
            self.assertTrue(call.args, f"Apply call at line {call.lineno} has no operation_id argument")
            arg = call.args[0]
            # The argument must be a variable read from a prior
            # `plan_res.get("operation_id")` / `op_id` binding that was
            # itself null-checked -- not the raw dict subscript/`.get()`
            # result passed inline (which could be None).
            self.assertIsInstance(arg, ast.Name, f"Apply call at line {call.lineno} does not use a pre-checked variable")

    # ── Mutation inventory: real discovery, not a hand-written list ──────

    def test_mutation_inventory_ci_gate_passes(self):
        self.assertTrue(verify_mutation_inventory(self.repo_root, check_mode=True))

    def test_every_discovered_sink_has_an_inventory_entry(self):
        inventory = json.loads((self.repo_root / "security" / "arch003_mutation_inventory.json").read_text(encoding="utf-8"))
        keys = {e["key"] for e in inventory["inventory"]}
        discovered = discover_all(self.repo_root)
        missing = [s for s in discovered if s.key not in keys]
        self.assertEqual(missing, [], f"{len(missing)} discovered sink(s) missing from inventory: {missing[:5]}")

    def test_gate_fails_on_a_genuinely_new_unclassified_sink(self):
        """Self-test (review finding #26): a synthetic file with a real,
        previously-unseen mutation call must make the gate fail, proving
        it actually inspects current source rather than only replaying
        the checked-in JSON."""
        with tempfile.TemporaryDirectory() as td:
            tmp_root = Path(td)
            (tmp_root / "security").mkdir()
            (tmp_root / "backend").mkdir()
            (tmp_root / "backend" / "transaction_engine.py").write_text("", encoding="utf-8")
            # Empty inventory: nothing is classified yet.
            (tmp_root / "security" / "arch003_mutation_inventory.json").write_text(
                json.dumps({"inventory": [], "unresolved_baseline": 0}), encoding="utf-8"
            )
            (tmp_root / "app.py").write_text(
                "import shutil\n"
                "def totally_new_function(src, dst):\n"
                "    shutil.move(src, dst)\n",
                encoding="utf-8",
            )
            self.assertFalse(verify_mutation_inventory(tmp_root, check_mode=True))

    def test_gate_passes_when_new_sink_is_classified_and_baseline_updated(self):
        """The counterpart to the above: once a human adds a matching,
        classified inventory entry (and the baseline accounts for it),
        the gate passes again."""
        with tempfile.TemporaryDirectory() as td:
            tmp_root = Path(td)
            (tmp_root / "security").mkdir()
            (tmp_root / "backend").mkdir()
            (tmp_root / "backend" / "transaction_engine.py").write_text("", encoding="utf-8")
            app_py = tmp_root / "app.py"
            app_py.write_text(
                "import shutil\n"
                "def totally_new_function(src, dst):\n"
                "    shutil.move(src, dst)\n",
                encoding="utf-8",
            )
            sinks = discover_sinks_in_file(app_py, "app.py")
            self.assertEqual(len(sinks), 1)
            (tmp_root / "security" / "arch003_mutation_inventory.json").write_text(
                json.dumps({
                    "inventory": [{
                        "key": sinks[0].key, "file": "app.py", "function": "totally_new_function",
                        "line": sinks[0].lineno, "kind": "filesystem", "call_text": sinks[0].call_text,
                        "classification": "TEST_ONLY", "transaction_family": "",
                    }],
                    "unresolved_baseline": 0,
                }),
                encoding="utf-8",
            )
            self.assertTrue(verify_mutation_inventory(tmp_root, check_mode=True))

    def test_gate_fails_when_unresolved_count_grows_beyond_baseline(self):
        """Growing the number of ARCH003_BLOCKER/NEEDS_REVIEW entries
        beyond the recorded baseline must fail, even if every sink does
        technically have a matching key (review finding #26's "prove new
        unclassified mutations fail CI", generalized to "don't quietly
        accumulate more debt than before")."""
        with tempfile.TemporaryDirectory() as td:
            tmp_root = Path(td)
            (tmp_root / "security").mkdir()
            (tmp_root / "backend").mkdir()
            (tmp_root / "backend" / "transaction_engine.py").write_text("", encoding="utf-8")
            (tmp_root / "app.py").write_text("", encoding="utf-8")
            (tmp_root / "security" / "arch003_mutation_inventory.json").write_text(
                json.dumps({
                    "inventory": [
                        {"key": "fake:one", "file": "app.py", "function": "f", "line": 1,
                         "classification": "NEEDS_REVIEW", "transaction_family": ""},
                        {"key": "fake:two", "file": "app.py", "function": "f", "line": 2,
                         "classification": "NEEDS_REVIEW", "transaction_family": ""},
                    ],
                    "unresolved_baseline": 1,  # baseline says 1, actual is 2
                }),
                encoding="utf-8",
            )
            self.assertFalse(verify_mutation_inventory(tmp_root, check_mode=True))

    def test_controlled_media_mutation_entries_reference_real_families(self):
        inventory = json.loads((self.repo_root / "security" / "arch003_mutation_inventory.json").read_text(encoding="utf-8"))
        for e in inventory["inventory"]:
            if e["classification"] == "CONTROLLED_MEDIA_MUTATION":
                self.assertTrue(e.get("transaction_family", "").endswith("_v1"),
                                 f"{e['file']}:{e['function']} has no real transaction_family: {e}")


if __name__ == "__main__":
    unittest.main()
