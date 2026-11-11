"""#623: POST /api/import-review/auto-enqueue/reconcile changes a Review row's
status only when that stored row has auto-import state or a stored key, and
refuses an untrusted stored path like auto-enqueue does."""
import unittest
from unittest import mock

import routes_import
from tests.test_issue617_auto_enqueue_parity import _CSRF, _Base

URL = "/api/import-review/auto-enqueue/reconcile"


class ReconcileStatusGuardTests(_Base):
    def reconcile(self):
        with mock.patch.object(routes_import, "_mark_pending_review_status") as mark, \
                mock.patch.object(routes_import, "_import_review_auto_job_for_submission", return_value=None):
            resp = self.client.post(URL, headers=_CSRF, json={"path": str(self.folder)})
        return resp, mark

    def test_ready_row_never_enqueued_keeps_its_status(self):
        resp, mark = self.reconcile()
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertFalse(resp.get_json()["reconciled"])
        mark.assert_not_called()

    def test_row_with_stored_key_is_marked_and_told_to_revalidate(self):
        self.write_item(auto_import_idempotency_key="stored-key")
        resp, mark = self.reconcile()
        self.assertEqual(resp.status_code, 200, resp.get_json())
        mark.assert_called_once()
        self.assertEqual(mark.call_args.args[1], "auto_enqueue_failed")
        note = resp.get_json()["note"]
        self.assertIn("revalidate", note)
        self.assertNotIn("retry enqueue", note)

    def test_row_with_auto_import_state_is_marked(self):
        with mock.patch.object(routes_import, "_import_review_auto_state",
                               side_effect=lambda: mock.MagicMock(get=lambda k: {"status": "enqueueing"})):
            resp, mark = self.reconcile()
        self.assertEqual(resp.status_code, 200, resp.get_json())
        mark.assert_called_once()


class ReconcileUntrustedPathTests(_Base):
    trusted = False  # the temp folder is outside every approved import root

    def test_untrusted_stored_path_is_400_and_touches_nothing(self):
        self.write_item(auto_import_idempotency_key="stored-key")
        with mock.patch.object(routes_import, "_mark_pending_review_status") as mark, \
                mock.patch.object(routes_import, "_import_review_auto_update") as update:
            resp = self.client.post(URL, headers=_CSRF, json={"path": str(self.folder)})
        self.assertEqual(resp.status_code, 400, resp.get_json())
        mark.assert_not_called()
        update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
