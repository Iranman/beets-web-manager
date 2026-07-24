import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TransactionIntegrationSourceTests(unittest.TestCase):
    def test_backend_exposes_transaction_api_and_job_hook(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("transactions = TransactionStore()", source)
        self.assertIn("def _install_transaction_job_hooks()", source)
        self.assertIn("metadata_payload[\"transaction_id\"] = tx_id", source)
        self.assertIn('@app.get("/api/transactions")', source)
        self.assertIn('@app.get("/api/transactions/<transaction_id>")', source)
        self.assertIn('@app.post("/api/transactions/<transaction_id>/apply")', source)
        self.assertIn('@app.post("/api/transactions/<transaction_id>/rollback")', source)
        self.assertIn('@app.get("/api/transactions/<transaction_id>/export")', source)

    def test_item_modify_creates_preview_diff_and_requires_approval(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        start = source.index('@app.post("/api/items/<int:iid>/modify")')
        end = source.index('@app.post("/api/items/<int:iid>/retag")', start)
        route = source[start:end]
        self.assertIn('approved_tx_id = _s(payload.get("apply_transaction_id")', route)
        self.assertIn('status="Preview"', route)
        self.assertIn('requires_approval', route)
        self.assertIn('operation_type="Metadata Update"', route)
        self.assertIn('changes=[tx_payload["change"]]', route)
        self.assertIn('"operations": [tx_payload["rollback_op"]]', route)
        self.assertNotIn('jobs.start_python(', route)

    def test_transaction_apply_executes_approved_metadata_update(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        start = source.index('def _start_metadata_apply_transaction')
        end = source.index('@app.post("/api/items/<int:iid>/modify")', start)
        helper = source[start:end]
        self.assertIn('if tx.get("status") != "Approved"', helper)
        self.assertIn('_metadata_transaction_pending_fields(tx)', helper)
        self.assertIn('jobs.start_python(', helper)
        self.assertIn('status="Completed"', helper)

    def test_transaction_rollback_executes_metadata_restore_operations(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        start = source.index('@app.post("/api/transactions/<transaction_id>/rollback")')
        end = source.index('@app.get("/api/transactions/<transaction_id>/export")', start)
        route = source[start:end]
        self.assertIn('_ROLLBACK_OP_TYPES = {"metadata_restore", "recording_id_restore", "playlist_track_restore"}', route)
        self.assertIn('_run_item_metadata_restore(item_id, fields, log', route)
        self.assertIn('status = "Rolled Back" if failed_count == 0 else "Partially Rolled Back"', route)
        self.assertIn('metadata={"transaction": False, "transaction_id": transaction_id', route)

    def test_frontend_links_library_changes_page(self):
        app_source = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        shell_source = (ROOT / "frontend" / "src" / "components" / "layout" / "Shell.tsx").read_text(encoding="utf-8")
        page_source = (ROOT / "frontend" / "src" / "views" / "LibraryChanges.tsx").read_text(encoding="utf-8")
        client_source = (ROOT / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")

        self.assertIn("import LibraryChanges from './views/LibraryChanges'", app_source)
        self.assertIn('path="changes"', app_source)
        self.assertNotIn("{ to: '/changes'", shell_source)
        self.assertIn("navigate('/changes')", shell_source)
        self.assertIn("Library Changes", shell_source)
        self.assertIn("getTransactions", page_source)
        self.assertIn("rollbackTransaction", page_source)
        self.assertIn("applyTransaction", page_source)
        self.assertIn(">Apply</Button>", page_source)
        self.assertIn("transactionExportUrl", client_source)


if __name__ == "__main__":
    unittest.main()