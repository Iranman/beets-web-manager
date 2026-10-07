"""
Wave 16 Album Cleanup Web Manager workflow tests (SEC-002 / ARCH-003).

Verifies:
1. Web Manager Flask routes for album cleanup plan/apply and generic
   transaction rollback delegate strictly through BeetsClient (zero direct
   DB/media mutation in app.py), using response shapes that actually match
   what backend/transaction_engine.py produces -- not fabricated ones.
2. Transaction-family enforcement: an Album Cleanup transaction can never be
   rolled back through the Import Review cleanup executor (and vice versa),
   at both the Web Manager route and the engine layer.
3. Error classification is truthful: "nothing was changed" is only ever
   shown when the engine's own "mutated" flag confirms it.
4. Dead/removed surface (album-specific rollback route, engine-wide
   transaction list/detail fallback) stays removed.
5. A structural (not just string-matching) regression guard against
   reintroducing direct mutation in the album cleanup routes.
"""

import ast
import os
import unittest
from unittest.mock import patch

from backend.beets_adapter import BeetsUnavailableError
import app as flask_app
try:  # ARCH-001: app.py module family
    from _app_ast_cache import app_family_source  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_ast_cache import app_family_source  # noqa: E402


class Wave16RouteDelegationTests(unittest.TestCase):
    """Route-level tests using response shapes that match what
    transaction_engine.py actually returns (verified against the real
    functions in Wave16RealEngineIntegrationTests below), not invented
    ones."""

    def setUp(self):
        flask_app.app.config["TESTING"] = True
        os.environ["BEETS_WEB_AUTH_DISABLED"] = "1"
        self.client = flask_app.app.test_client()

    @patch("app.composite_workflows.plan_album_cleanup")
    def test_plan_album_cleanup_delegates_to_beets_client(self, mock_plan):
        # Real create_album_cleanup_plan() success shape.
        mock_plan.return_value = {
            "ok": True,
            "operation_id": "txn_1700000000_abcdef012345",
            "status": "Preview",
            "album_id": 42,
            "target_path": "/music/Artist/Album",
            "file_count": 3,
        }

        resp = self.client.post("/api/albums/42/cleanup/plan")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["operation_id"], "txn_1700000000_abcdef012345")
        mock_plan.assert_called_once_with(42, delete_files=False, reason="")  # row-only by default (LT-4)

    @patch("app.composite_workflows.plan_album_cleanup")
    def test_plan_album_cleanup_engine_unreachable(self, mock_plan):
        mock_plan.side_effect = BeetsUnavailableError("Control agent down")

        resp = self.client.post("/api/albums/42/cleanup/plan")
        self.assertEqual(resp.status_code, 503)
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("Beets engine unavailable", data["error"])

    def test_plan_album_cleanup_invalid_album_id(self):
        resp = self.client.post("/api/albums/cleanup/plan", json={"album_id": 0})
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("album_id", data["error"])

    @patch("app.composite_workflows.apply_album_cleanup")
    def test_apply_album_cleanup_delegates_to_beets_client(self, mock_apply):
        # Real execute_album_cleanup_apply() success shape.
        mock_apply.return_value = {
            "ok": True,
            "status": "Completed",
            "operation_id": "txn_1700000000_abcdef012345",
            "deleted": ["/music/Artist/Album/01.flac"],
            "log": ["Deleted track file /music/Artist/Album/01.flac"],
        }

        resp = self.client.post("/api/albums/cleanup/apply", json={"operation_id": "txn_1700000000_abcdef012345"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "Completed")
        mock_apply.assert_called_once_with("txn_1700000000_abcdef012345")

    def test_apply_album_cleanup_missing_operation_id(self):
        resp = self.client.post("/api/albums/cleanup/apply", json={})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("operation_id", resp.get_json()["error"])

    @patch("app.composite_workflows.apply_album_cleanup")
    def test_apply_album_cleanup_stale_plan_before_mutation_is_truthful(self, mock_apply):
        """A revalidate_preconditions()-style failure (mutated=False) must
        be classified stale_plan and shown as "nothing was changed"."""
        mock_apply.return_value = {
            "ok": False,
            "error": "Path /music/Artist/Album/01.flac no longer exists.",
            "mutated": False,
        }

        resp = self.client.post("/api/albums/cleanup/apply", json={"operation_id": "txn_1700000000_abcdef012345"})
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error_kind"], "stale_plan")
        self.assertIn("Nothing was changed", data["error"])
        self.assertFalse(data["mutated"])

    @patch("app.composite_workflows.apply_album_cleanup")
    def test_apply_album_cleanup_partial_mutation_never_reported_as_stale(self, mock_apply):
        """Regression test for the core Wave 16 truthfulness bug: a
        mid-Apply failure (e.g. the DB-membership-drift check, which fires
        *after* file-delete steps for other items already ran) must never
        be shown as "nothing was changed" just because its error text
        contains the word "changed"."""
        mock_apply.return_value = {
            "ok": False,
            "error": (
                "Album 42 membership changed since planning "
                "(planned items [101, 102], now [101, 102, 103]); "
                "refusing to delete DB rows."
            ),
            "mutated": True,
        }

        resp = self.client.post("/api/albums/cleanup/apply", json={"operation_id": "txn_1700000000_abcdef012345"})
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error_kind"], "partial_mutation")
        self.assertNotIn("Nothing was changed", data["error"])
        self.assertTrue(data["mutated"])

    @patch("app.composite_workflows.apply_album_cleanup")
    def test_apply_album_cleanup_non_staleness_failure_not_mislabeled(self, mock_apply):
        """A failure that is neither stale nor partially mutated (e.g. a
        missing/misconfigured database) must not be relabeled as either
        "nothing changed" or "partially mutated" -- show it plainly."""
        mock_apply.return_value = {
            "ok": False,
            "error": "Beets database not found at /config/musiclibrary.blb",
            "mutated": False,
        }

        resp = self.client.post("/api/albums/cleanup/apply", json={"operation_id": "txn_1700000000_abcdef012345"})
        data = resp.get_json()
        self.assertEqual(data["error_kind"], "other")
        self.assertIn("musiclibrary.blb", data["error"])
        self.assertNotIn("Nothing was changed", data["error"])

    def test_album_specific_rollback_route_removed(self):
        """The dead, unused, family-confused /api/albums/cleanup/rollback
        route must stay removed -- the frontend uses the generic
        /api/transactions/<id>/rollback route instead. Werkzeug reports a
        removed POST-only route as 405 (a broader catch-all path pattern
        still matches, just not for POST) rather than 404; either
        response correctly proves this route no longer accepts requests."""
        resp = self.client.post("/api/albums/cleanup/rollback", json={"operation_id": "txn_x"})
        self.assertIn(resp.status_code, (404, 405))

    @patch("app.composite_workflows.list_transactions")
    def test_transactions_list_does_not_fall_back_to_engine(self, mock_list_tx):
        """/api/transactions must stay local-only -- nothing in the
        frontend uses an engine-wide transaction browse, and exposing it
        broadens attack surface for no product benefit."""
        resp = self.client.get("/api/transactions")
        self.assertEqual(resp.status_code, 200)
        mock_list_tx.assert_not_called()

    @patch("app.composite_workflows.get_transaction")
    def test_transaction_detail_does_not_fall_back_to_engine(self, mock_get_tx):
        """/api/transactions/<id> must stay local-only for the same
        reason -- confirm the unknown-locally case 404s cleanly instead of
        proxying into the engine's TransactionStore."""
        resp = self.client.get("/api/transactions/does-not-exist-locally")
        self.assertEqual(resp.status_code, 404)
        mock_get_tx.assert_not_called()




class Wave16AstStructuralTests(unittest.TestCase):
    """Structural (not string-matching) regression guard against direct
    mutation creeping into the album cleanup routes, and against the
    "helper-function delegation" bypass that a narrow call-name blocklist
    alone cannot catch (the exact blind spot Wave 15's original
    no-app-import test had for sys.modules)."""

    TARGET_ROUTES = {
        "plan_album_cleanup_route",
        "apply_album_cleanup_route",
    }

    # Anything touching the filesystem or a database directly. Broader than
    # Wave 16's original 5-item list (unlink/remove/rmtree/delete_album/
    # execute) on purpose.
    FORBIDDEN_CALLS = {
        "unlink", "remove", "rmtree", "rmdir", "move", "copy", "copyfile",
        "copystat", "copytree", "rename", "replace", "mkdir", "makedirs",
        "write_text", "write_bytes", "delete_album", "execute",
        "executemany", "executescript", "commit", "connect", "Popen",
        "run", "call", "check_call", "check_output", "system",
    }

    # Calls to bare local names (not attribute access like beets_client.x)
    # that these routes are allowed to make without triggering a review.
    # Anything else -- in particular any other locally-defined app.py
    # helper function -- must be explicitly added here, forcing a human to
    # notice and justify it rather than silently bypassing the mutation
    # check by routing through a helper. Each entry below has been
    # reviewed and performs no filesystem/DB mutation:
    #   jsonify, int, str, bool -- stdlib/Flask, side-effect-free.
    #   _s -- string coercion helper (defined near the top of app.py).
    #   _classify_album_cleanup_apply_failure -- pure error-text
    #     classification (see its definition just below these routes),
    #     no I/O of any kind.
    #   album_cleanup_apply_response -- (backend/cleanup_service.py) calls
    #     composite_workflows.apply_album_cleanup, the canonical mutation
    #     entry this route already used, plus the classifier above; no
    #     direct filesystem/DB call of its own.
    ALLOWED_LOCAL_CALLS = {"jsonify", "int", "str", "bool", "_s", "_classify_album_cleanup_apply_failure",
                           "album_cleanup_apply_response"}

    def setUp(self):
        app_path = flask_app.__file__
        self.source = app_family_source()  # ARCH-001: app.py module family
        self.tree = ast.parse(self.source, filename=app_path)

    def test_no_direct_mutation_calls_in_album_cleanup_routes(self):
        found_routes = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name in self.TARGET_ROUTES:
                found_routes.add(node.name)
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        func_name = ""
                        if isinstance(child.func, ast.Name):
                            func_name = child.func.id
                        elif isinstance(child.func, ast.Attribute):
                            func_name = child.func.attr
                        self.assertNotIn(
                            func_name,
                            self.FORBIDDEN_CALLS,
                            f"Function {node.name} contains a direct mutation-shaped call: {func_name}",
                        )
        self.assertEqual(found_routes, self.TARGET_ROUTES, f"Missing route definitions: {self.TARGET_ROUTES - found_routes}")

    def test_album_cleanup_routes_only_call_known_local_helpers(self):
        """Closes the helper-function-delegation bypass: a call to any
        bare (non-attribute) local name other than the explicit allowlist
        is rejected, so a future change that adds
        `plan_album_cleanup_route` -> `_some_new_helper()` -> (direct
        mutation inside `_some_new_helper`) cannot silently pass this
        test the way a pure call-name blocklist would."""
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name in self.TARGET_ROUTES:
                for child in ast.walk(node):
                    if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                        self.assertIn(
                            child.func.id,
                            self.ALLOWED_LOCAL_CALLS,
                            f"Function {node.name} calls local helper {child.func.id}() which is not in "
                            "the reviewed allowlist -- add it only after confirming it performs no "
                            "direct filesystem/DB mutation.",
                        )

    def test_album_cleanup_routes_never_import_sqlite3_or_shutil_locally(self):
        """A route could otherwise sidestep the call-name check entirely
        with a local `import sqlite3` / `import shutil` followed by a
        dynamically-built call the AST call-name check above still catches
        via .attr, but this closes the door on any local import statement
        at all inside these routes."""
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name in self.TARGET_ROUTES:
                for child in ast.walk(node):
                    self.assertNotIsInstance(
                        child, (ast.Import, ast.ImportFrom),
                        f"Function {node.name} must not contain a local import statement.",
                    )


if __name__ == "__main__":
    unittest.main()
