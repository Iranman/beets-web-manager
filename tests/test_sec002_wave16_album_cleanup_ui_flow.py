"""
Unit and integration tests for Wave 16: Album Cleanup Web Manager Workflow (SEC-002 / ARCH-003).

Verifies:
1. Web Manager Flask routes for album cleanup plan, apply, rollback, and transaction inspection
   delegate strictly through BeetsClient (zero direct DB/media mutations in app.py).
2. Engine unreachable errors fail closed with 503 HTTP status.
3. Stale plan refusal handling returns clear human-readable error messages.
4. Transaction inspection and rollback endpoints integrate smoothly.
"""

import ast
import os
import unittest
from unittest.mock import MagicMock, patch

from backend.beets_client import BeetsError, BeetsUnavailableError
import app as flask_app


class TestWave16AlbumCleanupUiFlow(unittest.TestCase):

    def setUp(self):
        flask_app.app.config["TESTING"] = True
        os.environ["BEETS_WEB_AUTH_DISABLED"] = "1"
        self.client = flask_app.app.test_client()

    @patch("app.beets_client.plan_album_cleanup")
    def test_plan_album_cleanup_delegates_to_beets_client(self, mock_plan):
        mock_plan.return_value = {
            "ok": True,
            "operation_id": "tx_clean_123",
            "album_id": 42,
            "reversibility": {
                "status": "RECOVERABLE",
                "rollback_available": True,
                "summary": "Full backup available",
            },
            "proposed_changes": {
                "track_file_unlinks": ["/music/artist/album/01.flac"],
                "item_db_rows_deleted": [101],
                "album_db_row_deleted": True,
            },
        }

        resp = self.client.post("/api/albums/42/cleanup/plan")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["operation_id"], "tx_clean_123")
        mock_plan.assert_called_once_with(42)

    @patch("app.beets_client.plan_album_cleanup")
    def test_plan_album_cleanup_engine_unreachable(self, mock_plan):
        mock_plan.side_effect = BeetsUnavailableError("Control agent down")

        resp = self.client.post("/api/albums/42/cleanup/plan")
        self.assertEqual(resp.status_code, 503)
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("Beets engine unavailable", data["error"])

    @patch("app.beets_client.apply_album_cleanup")
    def test_apply_album_cleanup_delegates_to_beets_client(self, mock_apply):
        mock_apply.return_value = {
            "ok": True,
            "operation_id": "tx_clean_123",
            "status": "COMPLETED",
            "logs": ["Removed track files", "Deleted DB rows"],
        }

        resp = self.client.post("/api/albums/cleanup/apply", json={"operation_id": "tx_clean_123"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "COMPLETED")
        mock_apply.assert_called_once_with("tx_clean_123")

    @patch("app.beets_client.apply_album_cleanup")
    def test_apply_album_cleanup_stale_plan_refusal(self, mock_apply):
        mock_apply.side_effect = BeetsError(
            "Album structure modified after plan creation. Precondition failed."
        )

        resp = self.client.post("/api/albums/cleanup/apply", json={"operation_id": "tx_clean_123"})
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("The album changed after this cleanup plan was created", data["error"])

    @patch("app.beets_client.rollback_import_review_cleanup")
    def test_rollback_album_cleanup_delegates(self, mock_rollback):
        mock_rollback.return_value = {
            "ok": True,
            "operation_id": "tx_clean_123",
            "status": "ROLLED_BACK",
            "restored_files": ["/music/artist/album/01.flac"],
        }

        resp = self.client.post("/api/albums/cleanup/rollback", json={"operation_id": "tx_clean_123"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "ROLLED_BACK")
        mock_rollback.assert_called_once_with("tx_clean_123")

    @patch("app.beets_client.get_transaction")
    def test_get_transaction_fallback_to_engine(self, mock_get_tx):
        mock_get_tx.return_value = {
            "ok": True,
            "transaction": {
                "id": "tx_clean_123",
                "status": "COMPLETED",
                "operation_type": "album_cleanup",
            },
        }

        resp = self.client.get("/api/transactions/tx_clean_123")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["transaction"]["id"], "tx_clean_123")
        mock_get_tx.assert_called_once_with("tx_clean_123")

    @patch("app.beets_client.list_transactions")
    def test_list_transactions_fallback_to_engine(self, mock_list_tx):
        mock_list_tx.return_value = {
            "ok": True,
            "transactions": [
                {
                    "id": "tx_clean_123",
                    "status": "COMPLETED",
                    "operation_type": "album_cleanup",
                }
            ],
            "total": 1,
        }

        resp = self.client.get("/api/transactions")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertGreaterEqual(len(data["transactions"]), 1)

    def test_app_has_no_direct_album_cleanup_mutations_ast(self):
        """Verify app.py album cleanup routes do not perform direct file unlinks,
        directory deletions, or DB writes, and instead delegate to beets_client."""
        app_path = flask_app.__file__
        with open(app_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=app_path)

        target_routes = {
            "plan_album_cleanup_route",
            "apply_album_cleanup_route",
            "rollback_album_cleanup_route",
        }
        found_routes = set()

        forbidden_calls = {"unlink", "remove", "rmtree", "delete_album", "execute"}

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in target_routes:
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
                            forbidden_calls,
                            f"Function {node.name} contains direct mutation call: {func_name}"
                        )

        self.assertEqual(found_routes, target_routes, f"Missing route definitions: {target_routes - found_routes}")


if __name__ == "__main__":
    unittest.main()
