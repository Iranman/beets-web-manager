"""QA #541 (added from the QA reproduction): a job-wrapper transaction whose job record is only in the job tail
must not be judged orphaned before the tail sweep has loaded it."""
import json
import threading
import time
import unittest
from unittest import mock

import backend.transaction_service as ts
import job_engine
from job_engine import JobStore
from tests.test_m2a_qa_verify import _Sync


class TailGateTests(_Sync):
    def test_wrapper_whose_job_is_in_the_tail_follows_the_job(self):
        self._tail_job_settles(retention_days=job_engine.JOB_RETENTION_DAYS)

    def test_wrapper_follows_a_tail_job_the_sweep_prunes(self):
        self._tail_job_settles(retention_days=0)
        self.assertFalse((self.data / "jobs" / "oldjob.json").exists())

    def _tail_job_settles(self, retention_days):
        tx = self.store.create(operation_type="Playlist Import", status="Running",
                               summary="Playlist sync (automatic)", reason=ts._JOB_WRAPPER_REASON,
                               source="playlist-sync",
                               metadata={"type": "playlist-sync", ts._JOB_WRAPPER_MARK: True})
        jobs_dir = self.data / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        old = jobs_dir / "oldjob.json"
        old.write_text(json.dumps(
            {"job_id": "oldjob", "label": "Playlist sync (automatic)", "status": "success",
             "created_at": now - 5, "finished_at": now - 4, "returncode": 0,
             "metadata": {"type": "playlist-sync", "transaction_id": tx["id"]}, "log_tail": ["done"]}),
            encoding="utf-8")
        import os
        os.utime(old, (1, 1))  # oldest: lands in the tail
        (jobs_dir / "newer.json").write_text(json.dumps(
            {"job_id": "newer", "label": "n", "status": "success", "created_at": now,
             "finished_at": now, "returncode": 0, "metadata": {}, "log_tail": []}), encoding="utf-8")
        time.sleep(0.01)
        gate = threading.Event()
        real_tail = JobStore._recover_tail

        def slow_tail(self_, paths, now_):
            gate.wait(5)
            real_tail(self_, paths, now_)

        with mock.patch.object(job_engine, "JOB_STARTUP_LOAD_LIMIT", 1), \
                mock.patch.object(job_engine, "JOB_RETENTION_DAYS", retention_days), \
                mock.patch.object(JobStore, "_recover_tail", slow_tail), \
                mock.patch.object(ts, "_RECONCILE_RETRY_SECONDS", 0.05):
            jobs = self.jobstore()
            self.install(jobs)
            time.sleep(0.3)  # several ticks while the tail is still loading
            self.assertEqual(self.store.get(tx["id"])["status"], "Running")
            gate.set()
            jobs._recover_thread.join(5)
            deadline = time.time() + 5
            while self.store.get(tx["id"])["status"] == "Running" and time.time() < deadline:
                time.sleep(0.05)
        self.assertEqual(self.store.get(tx["id"])["status"], "Completed")


if __name__ == "__main__":
    unittest.main()
