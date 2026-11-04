"""#545: tail index robustness and scope (follow-ups from #541)."""
import json
import os
import threading
import time
import types
import unittest
from unittest import mock

import backend.transaction_service as ts
import job_engine
from job_engine import JobStore
from tests.test_m2a_qa_verify import _Sync


def _no_jobs():
    return types.SimpleNamespace(all=lambda: [], get=lambda _jid: None, tail_transaction_jobs={})


class TailIndexTests(_Sync):
    def setUp(self):
        super().setUp()
        self.jobs_dir = self.data / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.mtime = 1000

    def wrapper(self):
        return self.store.create(operation_type="Playlist Import", status="Running",
                                 summary="Playlist sync (automatic)", reason=ts._JOB_WRAPPER_REASON,
                                 source="playlist-sync",
                                 metadata={"type": "playlist-sync", ts._JOB_WRAPPER_MARK: True})

    def record(self, job_id, status="success", metadata=None, **extra):
        """Written oldest first: each later record is newer on disk."""
        now = time.time()
        rec = {"job_id": job_id, "label": job_id, "status": status, "created_at": now - 5,
               "finished_at": None if status == "running" else now - 4, "returncode": 0,
               "metadata": {} if metadata is None else metadata, "log_tail": []}
        rec.update(extra)
        path = self.jobs_dir / f"{job_id}.json"
        path.write_text(json.dumps(rec), encoding="utf-8")
        self.mtime += 10
        os.utime(path, (self.mtime, self.mtime))

    def start(self, tail=None):
        """A store whose newest record is the only one loaded at startup."""
        self.record("newest")
        patches = [mock.patch.object(job_engine, "JOB_STARTUP_LOAD_LIMIT", 1),
                   mock.patch.object(ts, "_RECONCILE_RETRY_SECONDS", 0.05)]
        if tail is not None:
            patches.append(mock.patch.object(JobStore, "_recover_tail", tail))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        jobs = self.jobstore()
        self.install(jobs)
        return jobs

    def settle(self, tx_id, timeout=5):
        deadline = time.time() + timeout
        while self.store.get(tx_id)["status"] == "Running" and time.time() < deadline:
            time.sleep(0.05)
        return self.store.get(tx_id)["status"]

    # -- item 1 -------------------------------------------------------------
    def test_malformed_tail_record_does_not_end_the_sweep(self):
        tx = self.wrapper()
        self.record("oldjob", metadata={"type": "playlist-sync", "transaction_id": tx["id"]})
        self.record("badmeta", metadata=["not", "a", "dict"])  # read before oldjob (newer)
        self.record("badtime", finished_at="yesterday")
        jobs = self.start()
        jobs._recover_thread.join(5)
        self.assertEqual(self.settle(tx["id"]), "Completed")

    def test_dead_tail_thread_does_not_trigger_orphan_judging(self):
        tx = self.wrapper()
        self.record("oldjob", metadata={"type": "playlist-sync", "transaction_id": tx["id"]})

        def crashing_tail(self_, paths, now_):
            raise RuntimeError("tail sweep crashed")

        with mock.patch.object(threading, "excepthook", lambda _args: None):
            jobs = self.start(tail=crashing_tail)
            jobs._recover_thread.join(5)
        time.sleep(0.4)  # several reconcile ticks after the thread died
        self.assertEqual(self.store.get(tx["id"])["status"], "Running")

    # -- item 2 -------------------------------------------------------------
    def test_tail_snapshot_does_not_settle_an_engine_transaction(self):
        tx = self.store.create(operation_type="Metadata Update", status="Running",
                               metadata={"mutation_family": "metadata", "mutation_started": True})
        self.record("oldjob", metadata={"type": "metadata-batch", "transaction_id": tx["id"]})
        jobs = self.start()
        jobs._recover_thread.join(5)
        time.sleep(0.4)
        self.assertEqual(self.store.get(tx["id"])["status"], "Running")

    def test_tail_straggler_still_settles_an_engine_transaction(self):
        # The live case: an interrupted job in the tail is loaded into memory
        # and speaks for its transaction through _jobs_by_transaction.
        tx = self.store.create(operation_type="Metadata Update", status="Running",
                               metadata={"mutation_family": "metadata", "mutation_started": True})
        self.record("straggler", status="running",
                    metadata={"type": "metadata-batch", "transaction_id": tx["id"]})
        jobs = self.start()
        jobs._recover_thread.join(5)
        self.assertEqual(self.settle(tx["id"]), "Recovery Required")

    # -- item 3 -------------------------------------------------------------
    def test_paging_does_not_skip_a_running_transaction(self):
        txs = [self.wrapper() for _ in range(3)]
        real_list = self.store.list
        calls = []

        def churning_list(**kwargs):
            page, total = real_list(**kwargs)
            calls.append(1)
            if len(calls) == 1:  # another writer settles a row between page reads
                self.store.transition(str(page[0]["id"]), "Running", "Completed")
            return page, total

        with mock.patch.object(ts, "_RUNNING_PAGE", 1, create=True), \
                mock.patch.object(self.store, "list", churning_list):
            self.assertTrue(ts._reconcile_running_transactions(_no_jobs(), self.store, time.time() + 60))
        self.assertNotIn("Running", [self.store.get(t["id"])["status"] for t in txs])

    # -- item 4 -------------------------------------------------------------
    def test_orphan_judging_rechecks_updated_at_under_the_lock(self):
        tx = self.wrapper()
        row = self.store.get(tx["id"])
        before = time.time() + 0.001
        time.sleep(0.01)
        self.store.append_log(tx["id"], "the job wrote after the list")  # live since `before`
        ts._orphaned_job_wrapper(self.store, row, before)
        self.assertEqual(self.store.get(tx["id"])["status"], "Running")
        ts._orphaned_job_wrapper(self.store, row, time.time() + 1)  # control: no later write
        self.assertNotEqual(self.store.get(tx["id"])["status"], "Running")

    # -- startup log --------------------------------------------------------
    def test_unbound_log_is_debug_until_the_judging_pass(self):
        self.store.create(operation_type="Metadata Update", status="Running",
                          metadata={"mutation_family": "metadata"})
        with mock.patch.object(ts._app_logger, "warning") as warn, \
                mock.patch.object(ts._app_logger, "debug") as debug:
            ts._reconcile_running_transactions(_no_jobs(), self.store, None)
            warn.assert_not_called()
            self.assertIn("pending the startup job scan", debug.call_args[0][0])
            ts._reconcile_running_transactions(_no_jobs(), self.store, time.time())
            self.assertIn("left unchanged", warn.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
