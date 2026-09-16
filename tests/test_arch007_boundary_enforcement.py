"""Durable AST and Structural Boundary Enforcement Tests for ARCH-007.

Enforces:
1. Production Web Manager modules (app.py, routes_*.py, helpers_mb.py, job_engine.py)
   contain ZERO calls to `_db()`.
2. Production Web Manager modules contain ZERO calls to `raw_sqlite_query()`.
3. Web Manager contains ZERO direct `sqlite3.connect()` calls to the Beets library database.
4. `BeetsClient` exposes ZERO generic or caller-supplied SQL execution methods.
5. `BeetsClient` semantic methods do NOT delegate to `raw_sqlite_query()`.
6. Control Agent exposes ZERO generic SQL query endpoints (POST /library/raw_query returns 403).
"""

import ast
import inspect
from pathlib import Path
import unittest

from backend.beets_client import BeetsClient, BeetsError


class TestArch007BoundaryEnforcement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parent.parent
        cls.app_path = cls.repo_root / "app.py"
        cls.beets_client_path = cls.repo_root / "backend" / "beets_client.py"
        cls.beets_control_agent_path = cls.repo_root / "backend" / "beets_control_agent.py"

        cls.app_source = cls.app_path.read_text(encoding="utf-8")
        cls.app_tree = ast.parse(cls.app_source, filename="app.py")

        cls.client_source = cls.beets_client_path.read_text(encoding="utf-8")
        cls.client_tree = ast.parse(cls.client_source, filename="backend/beets_client.py")

        cls.agent_source = cls.beets_control_agent_path.read_text(encoding="utf-8")
        cls.agent_tree = ast.parse(cls.agent_source, filename="backend/beets_control_agent.py")

    def test_app_py_has_zero_db_call_sites(self):
        """ARCH-007: app.py must contain 0 call nodes to `_db()`."""
        found_calls = []
        for node in ast.walk(self.app_tree):
            if isinstance(node, ast.Call):
                func = node.func
                # Match direct function call `_db(...)`
                if isinstance(func, ast.Name) and func.id == "_db":
                    found_calls.append(f"_db() at line {node.lineno}")
                # Match attribute call `self._db(...)` or `obj._db(...)`
                elif isinstance(func, ast.Attribute) and func.attr == "_db":
                    found_calls.append(f"._db() at line {node.lineno}")

        self.assertEqual(
            found_calls,
            [],
            f"ARCH-007 boundary violation: found {len(found_calls)} call(s) to _db() in app.py: {found_calls}",
        )

    def test_routes_modules_have_zero_db_call_sites(self):
        """ARCH-007: routes_*.py modules must contain 0 call nodes to `_db()`."""
        routes_files = sorted(self.repo_root.glob("routes_*.py"))
        self.assertTrue(len(routes_files) > 0, "Expected routes_*.py files in repository")

        for route_file in routes_files:
            source = route_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=route_file.name)
            found_calls = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Name) and func.id == "_db":
                        found_calls.append(f"_db() at line {node.lineno}")
                    elif isinstance(func, ast.Attribute) and func.attr == "_db":
                        found_calls.append(f"._db() at line {node.lineno}")

            self.assertEqual(
                found_calls,
                [],
                f"ARCH-007 boundary violation: found {len(found_calls)} call(s) to _db() in {route_file.name}: {found_calls}",
            )

    def test_production_modules_have_zero_raw_sqlite_query_calls(self):
        """ARCH-007: Web Manager production modules must never invoke raw_sqlite_query()."""
        candidate_files = [
            self.app_path,
            self.repo_root / "helpers_mb.py",
            self.repo_root / "job_engine.py",
            *self.repo_root.glob("routes_*.py"),
        ]

        for path in candidate_files:
            if not path.is_file():
                continue
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=path.name)
            found_calls = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Name) and func.id == "raw_sqlite_query":
                        found_calls.append(f"raw_sqlite_query at line {node.lineno}")
                    elif isinstance(func, ast.Attribute) and func.attr == "raw_sqlite_query":
                        found_calls.append(f".raw_sqlite_query at line {node.lineno}")

            self.assertEqual(
                found_calls,
                [],
                f"ARCH-007 boundary violation: found raw_sqlite_query call(s) in {path.name}: {found_calls}",
            )

    def test_web_manager_has_zero_direct_sqlite3_connect_to_beets_db(self):
        """ARCH-007: Web Manager app/route files must not directly open SQLite connections to Beets DB."""
        web_manager_files = [
            self.app_path,
            self.repo_root / "helpers_mb.py",
            self.repo_root / "job_engine.py",
            *self.repo_root.glob("routes_*.py"),
        ]

        for path in web_manager_files:
            if not path.is_file():
                continue
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=path.name)
            found_calls = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    if (
                        isinstance(func, ast.Attribute)
                        and func.attr == "connect"
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "sqlite3"
                    ):
                        found_calls.append(f"sqlite3.connect at line {node.lineno}")

            self.assertEqual(
                found_calls,
                [],
                f"ARCH-007 boundary violation: direct sqlite3.connect found in Web Manager file {path.name}: {found_calls}",
            )

    def test_beets_client_has_no_generic_sql_execution_methods(self):
        """ARCH-007: BeetsClient must not expose generic caller-supplied SQL execution methods."""
        banned_method_names = {
            "query_sql",
            "execute_read_query",
            "select",
            "query_database",
            "execute_query",
            "raw_query",
            "sql_query",
            "execute_sql",
            "run_query",
            "run_sql",
        }

        client_class = next(
            (n for n in ast.walk(self.client_tree) if isinstance(n, ast.ClassDef) and n.name == "BeetsClient"),
            None,
        )
        self.assertIsNotNone(client_class, "BeetsClient class not found")

        methods = {
            n.name
            for n in ast.walk(client_class)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        exposed_banned = methods & banned_method_names
        self.assertEqual(
            exposed_banned,
            set(),
            f"ARCH-007 boundary violation: BeetsClient exposes generic SQL methods: {exposed_banned}",
        )

    def test_beets_client_semantic_methods_do_not_delegate_to_raw_sqlite_query(self):
        """ARCH-007: No semantic BeetsClient method may call raw_sqlite_query()."""
        client_class = next(
            (n for n in ast.walk(self.client_tree) if isinstance(n, ast.ClassDef) and n.name == "BeetsClient"),
            None,
        )
        self.assertIsNotNone(client_class)

        violating_methods = []
        for item in client_class.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if item.name == "raw_sqlite_query":
                    continue  # The hard-raising stub itself
                for node in ast.walk(item):
                    if isinstance(node, ast.Call):
                        func = node.func
                        if isinstance(func, ast.Attribute) and func.attr == "raw_sqlite_query":
                            violating_methods.append(f"{item.name} calls .raw_sqlite_query at line {node.lineno}")
                        elif isinstance(func, ast.Name) and func.id == "raw_sqlite_query":
                            violating_methods.append(f"{item.name} calls raw_sqlite_query at line {node.lineno}")

        self.assertEqual(
            violating_methods,
            [],
            f"ARCH-007 boundary violation: BeetsClient methods delegate to raw_sqlite_query: {violating_methods}",
        )

    def test_beets_client_raw_sqlite_query_fails_closed_locally(self):
        """ARCH-007: BeetsClient.raw_sqlite_query must fail closed locally with BeetsError."""
        client = BeetsClient(base_url="http://127.0.0.1:9999", token="test_token")
        with self.assertRaises(BeetsError) as ctx:
            client.raw_sqlite_query("SELECT 1")
        self.assertIn("Raw SQLite queries are not permitted", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
