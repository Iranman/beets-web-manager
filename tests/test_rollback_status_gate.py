"""#219: the local metadata / recording-ID rollback restores captured values,
which is itself a library write. It must run only for an applied
(Completed) transaction, claimed with a Completed -> Running CAS; every other
status is refused with 409 and ``mutated: false`` before any restore runs."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import app as app_module
import routes_maintenance
from backend.transaction_engine import STATUSES, TransactionStore

OPS = {"metadata_restore": "_run_item_metadata_restore",
       "recording_id_restore": "_run_item_recording_id_restore"}


class RollbackStatusGateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = TransactionStore(str(Path(tmp.name) / "tx"))
        self.restores = {name: mock.Mock(return_value=True) for name in OPS.values()}
        self.start = mock.Mock(side_effect=lambda fn, label="", metadata=None: (fn([]), SimpleNamespace(job_id="j"))[1])
        patches = [mock.patch.object(routes_maintenance, "transactions", self.store),
                   mock.patch.object(routes_maintenance, "_sync_transactions_from_jobs"),
                   mock.patch.object(routes_maintenance.jobs, "start_python", self.start),
                   mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})]
        patches += [mock.patch.object(routes_maintenance, n, m) for n, m in self.restores.items()]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = app_module.app.test_client()

    def _tx(self, status, op_type="metadata_restore", operation="Metadata Update", **metadata):
        tx = self.store.create(operation_type=operation, status=status, summary="edit",
                               rollback_available=True, metadata=metadata)
        self.store.update(tx["id"], rollback={"available": True, "operations": [
            {"type": op_type, "item_id": 7, "fields": {"title": "old"}}]})
        return tx["id"]

    def _assert_refused(self, tid, status):
        res = self.client.post(f"/api/transactions/{tid}/rollback")
        body = res.get_json()
        self.assertEqual(res.status_code, 409, body)
        self.assertIs(body["mutated"], False)
        self.assertEqual(self.store.get(tid)["status"], status)
        self.start.assert_not_called()
        for restore in self.restores.values():
            restore.assert_not_called()

    def test_every_non_completed_status_is_refused_without_a_write(self):
        for status in sorted(STATUSES - {"Completed"}):
            for op_type, operation in (("metadata_restore", "Metadata Update"),
                                       ("recording_id_restore", "MusicBrainz Match")):
                with self.subTest(status=status, op=op_type):
                    self._assert_refused(self._tx(status, op_type, operation), status)

    def test_completed_rollback_restores_and_ends_rolled_back(self):
        for op_type, operation in (("metadata_restore", "Metadata Update"),
                                   ("recording_id_restore", "MusicBrainz Match")):
            with self.subTest(op=op_type):
                tid = self._tx("Completed", op_type, operation)
                res = self.client.post(f"/api/transactions/{tid}/rollback")
                self.assertEqual(res.status_code, 200, res.get_json())
                self.restores[OPS[op_type]].assert_called_once()
                self.assertEqual(self.store.get(tid)["status"], "Rolled Back")

    def test_second_rollback_of_the_same_transaction_is_refused(self):
        tid = self._tx("Completed")
        self.assertEqual(self.client.post(f"/api/transactions/{tid}/rollback").status_code, 200)
        self.restores["_run_item_metadata_restore"].reset_mock()
        self.start.reset_mock()
        self._assert_refused(tid, "Rolled Back")

    def test_failed_with_an_engine_apply_record_may_roll_back(self):
        tid = self._tx("Failed", engine_result={"applied": True})
        res = self.client.post(f"/api/transactions/{tid}/rollback")
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.store.get(tid)["status"], "Rolled Back")

    def test_job_start_failure_returns_fixed_500_and_restores_the_source_status(self):
        """SEC-223-2: a claim whose job never started must not strand Running."""
        self.start.side_effect = ValueError("/secret")
        for source, meta in (("Completed", {}), ("Failed", {"engine_result": {"applied": True}})):
            with self.subTest(source=source):
                tid = self._tx(source, **meta)
                res = self.client.post(f"/api/transactions/{tid}/rollback")
                self.assertEqual(res.status_code, 500)
                self.assertNotIn("/secret", res.get_data(as_text=True))
                self.assertEqual(res.get_json(), {"ok": False, "error": "Unexpected server error"})
                self.assertEqual(self.store.get(tid)["status"], source)
                self.restores["_run_item_metadata_restore"].assert_not_called()

    def test_cas_lost_to_a_concurrent_status_change_is_refused(self):
        """The status flips between the route's read and its claim."""
        tid = self._tx("Completed")
        real = self.store.transition

        def racing(tx_id, expected, new, **kw):
            self.store.update(tx_id, status="Running")
            return real(tx_id, expected, new, **kw)

        with mock.patch.object(self.store, "transition", side_effect=racing):
            self._assert_refused(tid, "Running")


if __name__ == "__main__":
    unittest.main()
