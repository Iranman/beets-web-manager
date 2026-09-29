"""
Track replacement route boundary guard (SEC-002 Wave 17, kept after the
v0.1.40 retirement of the legacy direct-SQLite replacement engine).

The item replacement routes and the music-format pipeline step must never
mutate files or the database themselves: they only call the one
replacement authority (backend.item_replacement), which previews the
canonical engine-backed item-file replacement. Behaviour is covered by
tests/test_replacement_authority.py and tests/test_item_file_replacement_routes.py.
"""

import ast
import unittest

import app as flask_app
try:  # ARCH-001: app.py module family
    from _app_ast_cache import app_family_source  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_ast_cache import app_family_source  # noqa: E402


class AstStructuralTests(unittest.TestCase):
    """Structural (not just two-call-name) regression guard, matching the
    approach established in Wave 16 review -- broad enough to catch
    mutation hidden behind a local helper function, not just a bare
    unlink()/shutil.move() call in the target function's own body."""

    TARGET_FUNCTIONS = {
        "_music_format_remove_original_after_replacement",
        "item_replacement_plan",
        "item_replacement_apply",
    }
    FORBIDDEN_CALLS = {
        "unlink", "remove", "rmtree", "rmdir", "move", "copy", "copyfile",
        "copystat", "copytree", "rename", "replace", "mkdir", "makedirs",
        "write_text", "write_bytes", "execute", "executemany",
        "executescript", "commit", "connect", "Popen", "run", "call",
        "check_call", "check_output", "system",
    }
    ALLOWED_LOCAL_CALLS = {
        "jsonify", "int", "str", "bool", "float", "_s", "len", "getattr",
        # Everything else goes through the one replacement authority
        # (backend.item_replacement, called as a module attribute): it
        # re-proves identity by AcoustID, checks the canonical destination
        # and previews the canonical item-file replacement transaction.
    }

    def setUp(self):
        app_path = flask_app.__file__
        self.source = app_family_source()  # ARCH-001: app.py module family
        self.tree = ast.parse(self.source, filename=app_path)

    def test_no_direct_mutation_calls(self):
        found = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name in self.TARGET_FUNCTIONS:
                found.add(node.name)
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        func_name = ""
                        if isinstance(child.func, ast.Name):
                            func_name = child.func.id
                        elif isinstance(child.func, ast.Attribute):
                            func_name = child.func.attr
                        self.assertNotIn(
                            func_name, self.FORBIDDEN_CALLS,
                            f"Function {node.name} contains a direct mutation-shaped call: {func_name}",
                        )
        self.assertEqual(found, self.TARGET_FUNCTIONS, f"Missing function definitions: {self.TARGET_FUNCTIONS - found}")

    def test_only_calls_known_local_helpers(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name in self.TARGET_FUNCTIONS:
                for child in ast.walk(node):
                    if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                        self.assertIn(
                            child.func.id, self.ALLOWED_LOCAL_CALLS,
                            f"Function {node.name} calls local helper {child.func.id}() which is not in "
                            "the reviewed allowlist.",
                        )

    def test_no_local_import_statements(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name in self.TARGET_FUNCTIONS:
                for child in ast.walk(node):
                    self.assertNotIsInstance(child, (ast.Import, ast.ImportFrom),
                                              f"Function {node.name} must not contain a local import statement.")


if __name__ == "__main__":
    unittest.main()
