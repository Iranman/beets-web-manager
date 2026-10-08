"""Durable JobStore (ARCH-004): persisted records, checkpoints, heartbeats and
deterministic restart resolution. A job that was running when the process
stopped is never re-run: read-only jobs become ``failed`` (safe to start
again), everything else ``recovery_required``."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import job_engine
from job_engine import JobStore, TERMINAL_STATUSES
from tests._job_store_cleanup import close_job_stores_at_cleanup


def _wait(job, timeout=5.0):
    deadline = time.time() + timeout
    while job.status == "running" and time.time() < deadline:
        time.sleep(0.01)


class DurableJobStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        close_job_stores_at_cleanup(self)
        self.dir = Path(self._tmp.name) / "jobs"

    def _record(self, jid):
        return json.loads((self.dir / f"{jid}.json").read_text(encoding="utf-8"))

    def _final_record(self, jid, timeout=5.0):
        """The durable record once it reaches a terminal status. The in-memory
        status flips a moment before the final write lands; a crash in that
        window resolves conservatively (recovery_required), never to a rerun."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                rec = self._record(jid)
                if rec["status"] in TERMINAL_STATUSES:
                    return rec
            except (OSError, ValueError):
                pass
            time.sleep(0.005)
        return self._record(jid)

    def test_without_a_directory_nothing_is_written(self):
        store = JobStore()
        job = store.start_python(lambda log: log.append("hi"), label="mem")
        _wait(job)
        self.assertIsNone(store.root)
        self.assertEqual(job.status, "success")

    def test_finished_job_is_persisted_with_result_and_log(self):
        store = JobStore(self.dir)
        job = store.start_python(lambda log: (log.append("done"), {"n": 3})[1], label="x",
                                 metadata={"type": "dedup-scan"})
        _wait(job)
        rec = self._final_record(job.job_id)
        self.assertEqual((rec["status"], rec["result"], rec["log_tail"]), ("success", {"n": 3}, ["done"]))
        self.assertEqual(rec["metadata"]["type"], "dedup-scan")

    def test_checkpoints_are_persisted_immediately(self):
        store = JobStore(self.dir)
        release = threading.Event()

        def fn(log, cancel, update_state):
            update_state(checkpoint={"processed": 500})
            release.wait(5)

        job = store.start_python(fn, metadata={"type": "untracked-inventory"})
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                if self._record(job.job_id)["state"].get("checkpoint") == {"processed": 500}:
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.01)
        self.assertEqual(self._record(job.job_id)["state"]["checkpoint"], {"processed": 500})
        self.assertEqual(self._record(job.job_id)["status"], "running")
        release.set()
        _wait(job)

    def test_progress_writes_are_throttled(self):
        store = JobStore(self.dir)
        writes = []
        original = store._write
        store._write = lambda job: (writes.append(1), original(job))

        def fn(log, cancel, update_state):
            for i in range(200):
                update_state(processed=i)

        job = store.start_python(fn)
        job._persist = store._write
        _wait(job)
        self.assertLess(len(writes), 10)  # start + finish + a throttled few, not 200

    def _running_record(self, jid, metadata, checkpoint=None):
        self.dir.mkdir(parents=True, exist_ok=True)
        rec = {"version": 1, "job_id": jid, "label": "old", "status": "running", "created_at": 1.0,
               "started_at": 1.0, "finished_at": None, "returncode": None, "heartbeat_at": 2.0,
               "metadata": metadata, "state": {"checkpoint": checkpoint} if checkpoint else {},
               "log_tail": ["step 1"], "log_lines": 1, "result": None, "recovery": None}
        (self.dir / f"{jid}.json").write_text(json.dumps(rec), encoding="utf-8")

    def test_restart_resolves_interrupted_jobs_deterministically(self):
        self._running_record("scan1", {"type": "dedup-scan"})
        self._running_record("merge1", {"type": "merge-duplicate-album"}, checkpoint={"moved": [1, 2]})
        self._running_record("unknown1", {"type": "something-new"})
        self._running_record("flagged1", {"type": "something-new", "mutating": False})
        store = JobStore(self.dir)
        status = {j.job_id: j.status for j in store.all()}
        self.assertEqual(status, {"scan1": "failed", "merge1": "recovery_required",
                                  "unknown1": "recovery_required", "flagged1": "failed"})
        merge = store.get("merge1")
        self.assertEqual(merge.recovery["checkpoint"], {"moved": [1, 2]})
        self.assertEqual(merge.recovery["last_heartbeat_at"], 2.0)
        self.assertEqual(self._record("merge1")["status"], "recovery_required")
        for job in store.all():
            self.assertIn(job.status, TERMINAL_STATUSES)
        # A second restart changes nothing: the resolution is itself durable.
        again = {j.job_id: j.status for j in JobStore(self.dir).all()}
        self.assertEqual(again, status)

    def test_recovery_required_records_are_never_auto_pruned(self):
        self._running_record("merge1", {"type": "merge-duplicate-album"})
        store = JobStore(self.dir)
        store.prune_finished(max_age_seconds=0, metadata_max_age_seconds=0, max_finished=0)
        self.assertIsNotNone(store.get("merge1"))
        self.assertTrue((self.dir / "merge1.json").exists())

    def test_cancellation_is_persisted_and_survives_restart(self):
        store = JobStore(self.dir)
        started = threading.Event()

        def fn(log, cancel):
            started.set()
            while not cancel.is_set():
                time.sleep(0.01)

        job = store.start_python(fn, metadata={"type": "merge-duplicate-album"})
        started.wait(5)
        job.kill()
        _wait(job)
        self.assertEqual(self._final_record(job.job_id)["status"], "cancelled")
        self.assertEqual(JobStore(self.dir).get(job.job_id).status, "cancelled")

    def test_pruned_and_cleared_jobs_are_deleted_from_disk(self):
        store = JobStore(self.dir)
        job = store.start_python(lambda log: None)
        _wait(job)
        store.clear_finished()
        self.assertFalse((self.dir / f"{job.job_id}.json").exists())

    def test_close_waits_for_the_final_write_and_stops_the_heartbeat(self):
        # #286: the in-memory status flips before the final write lands, so a
        # caller removing the directory must close() the store first.
        real_write = JobStore._write

        def slow_final_write(store, job):
            if job.finished_at is not None:
                time.sleep(0.2)
            real_write(store, job)

        with mock.patch.object(JobStore, "_write", slow_final_write):
            store = JobStore(self.dir)
            job = store.start_python(lambda log: None)
            _wait(job)
            self.assertTrue(store.close())
        self.assertEqual(self._record(job.job_id)["status"], "success")
        self.assertFalse(store._heartbeat.is_alive())
        self.assertEqual(list(self.dir.glob(".*.tmp")), [])

    def test_heartbeat_refreshes_running_jobs(self):
        original = job_engine.HEARTBEAT_SECONDS
        job_engine.HEARTBEAT_SECONDS = 0.05
        self.addCleanup(setattr, job_engine, "HEARTBEAT_SECONDS", original)
        store = JobStore(self.dir)
        release = threading.Event()
        job = store.start_python(lambda log, cancel: release.wait(5))
        first = None
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                hb = self._record(job.job_id).get("heartbeat_at")
            except (OSError, ValueError):
                hb = None
            if first is None:
                first = hb
            elif hb and hb > first:
                break
            time.sleep(0.02)
        self.assertGreater(self._record(job.job_id)["heartbeat_at"], first)
        release.set()
        _wait(job)


if __name__ == "__main__":
    unittest.main()
