"""ARCH-019: a rollback job must never report success before the
TransactionStore has durably committed its terminal status.

Root cause: the route started the rollback job and only THEN wrote
``status="Running"``. A job that finished first had its committed
"Rolled Back" overwritten by that later write, while the job itself
reported success. The route now marks Running before the job starts and
afterwards touches metadata only; the job writes the terminal status before
it returns, so its success implies the status is committed.
"""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import app as app_module
import routes_maintenance
from backend.transaction_engine import TransactionStore
from job_engine import JobStore
from tests._job_store_cleanup import close_job_stores_at_cleanup


def _rollback_tx(store):
    tx = store.create(operation_type="Metadata Update", status="Completed", summary="edit",
                      rollback_available=True)
    store.update(tx["id"], rollback={"available": True, "operations": [
        {"type": "metadata_restore", "item_id": 7, "fields": {"title": "old"}}]})
    return tx["id"]


class RollbackOrderingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        close_job_stores_at_cleanup(self)
        self.store = TransactionStore(str(Path(self._tmp.name) / "tx"))
        for p in (mock.patch.object(routes_maintenance, "transactions", self.store),
                  mock.patch.object(routes_maintenance, "_run_item_metadata_restore", return_value=True),
                  mock.patch.object(routes_maintenance, "_sync_transactions_from_jobs"),
                  mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})):
            p.start()
            self.addCleanup(p.stop)
        self.client = app_module.app.test_client()

    def test_a_job_that_finishes_before_the_route_returns_keeps_rolled_back(self):
        """The old ordering fails this: the post-start write overwrote the
        job's committed "Rolled Back" with "Running"."""
        def instant_job(fn, label="", metadata=None):
            fn([])  # finishes before start_python even returns
            return SimpleNamespace(job_id="job-instant")

        tid = _rollback_tx(self.store)
        with mock.patch.object(routes_maintenance.jobs, "start_python", instant_job):
            res = self.client.post(f"/api/transactions/{tid}/rollback")
        self.assertEqual(res.status_code, 200, res.get_json())
        tx = self.store.get(tid)
        self.assertEqual(tx["status"], "Rolled Back")
        self.assertEqual(tx["metadata"]["rollback_job_id"], "job-instant")

    def test_under_delay_and_polling_success_implies_the_status_is_committed(self):
        """A durable JobStore, a slow TransactionStore write and a poller:
        whenever the job reads success, the store already says Rolled Back."""
        jobs = JobStore(Path(self._tmp.name) / "jobs")
        real_update = self.store.update

        def slow_update(tid, **kw):
            if kw.get("status") in ("Rolled Back", "Partially Rolled Back"):
                time.sleep(0.2)
            return real_update(tid, **kw)

        violations = []
        stop = threading.Event()
        tid = _rollback_tx(self.store)

        def poll(job_id_holder):
            while not stop.is_set():
                job = jobs.get(job_id_holder.get("id") or "")
                if job is not None and job.status == "success" and self.store.get(tid)["status"] != "Rolled Back":
                    violations.append(self.store.get(tid)["status"])
                time.sleep(0.005)

        holder = {}
        poller = threading.Thread(target=poll, args=(holder,))
        poller.start()
        with mock.patch.object(routes_maintenance, "jobs", jobs), \
                mock.patch.object(self.store, "update", side_effect=slow_update):
            res = self.client.post(f"/api/transactions/{tid}/rollback")
            holder["id"] = res.get_json()["job_id"]
            deadline = time.time() + 5
            while jobs.get(holder["id"]).status == "running" and time.time() < deadline:
                time.sleep(0.01)
        stop.set()
        poller.join()
        self.assertEqual(jobs.get(holder["id"]).status, "success")
        self.assertEqual(violations, [])
        self.assertEqual(self.store.get(tid)["status"], "Rolled Back")
        # ...and the durable job record agrees after a "restart".
        self.assertEqual(JobStore(Path(self._tmp.name) / "jobs").get(holder["id"]).status, "success")

    def test_route_marks_running_before_the_job_starts(self):
        seen = {}

        def capture(fn, label="", metadata=None):
            seen["status_at_start"] = self.store.get(tid)["status"]
            return SimpleNamespace(job_id="job-x")

        tid = _rollback_tx(self.store)
        with mock.patch.object(routes_maintenance.jobs, "start_python", capture):
            self.client.post(f"/api/transactions/{tid}/rollback")
        self.assertEqual(seen["status_at_start"], "Running")


if __name__ == "__main__":
    unittest.main()
