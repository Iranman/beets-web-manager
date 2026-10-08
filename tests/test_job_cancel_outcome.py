"""D4: a late cancel never overrides what a job actually did.

A job is cancelled only when it stopped because of the request: it raised
"cancelled", or it saw the request (``is_set()``/``wait()`` returned True)
and then returned or raised. A request the job never saw arrived after its
work was done, or while a step it does not interrupt ran (a Beets import):
the real outcome is recorded, with a log line about the late cancel. The
hook-created transaction follows the same rule."""

import threading
import time
import unittest
from unittest import mock

from job_engine import JobStore
from tests.test_staging_mutation_hardening import _RouteEnv


def _wait(job, timeout=5.0):
    deadline = time.time() + timeout
    while job.status == "running" and time.time() < deadline:
        time.sleep(0.01)


class _Gate:
    """A job body that blocks until released, with a cancel arriving meanwhile."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, store, body, **kwargs):
        def gated(log, cancel, update_state=None):
            self.started.set()
            self.release.wait(5)
            return body(log, cancel)

        job = store.start_python(gated, label=kwargs.pop("label", "x"), **kwargs)
        assert self.started.wait(5)
        job.kill()  # the cancel button, while the job's work is in progress
        self.release.set()
        _wait(job)
        return job


LATE = "[cancel requested, but the job had already done its work"


class JobOutcomeTests(unittest.TestCase):
    def test_work_that_finished_unaware_of_the_cancel_succeeds(self):
        # The live RC repro: an import cancelled while Beets imported.
        job = _Gate().run(JobStore(), lambda log, cancel: {"albums_imported": 1})
        self.assertEqual((job.status, job.returncode), ("success", 0))
        self.assertEqual(job.result, {"albums_imported": 1})
        self.assertTrue(any(line.startswith(LATE) and "success" in line for line in job.log), job.log)

    def test_a_failed_result_after_an_unseen_cancel_is_failed(self):
        job = _Gate().run(JobStore(), lambda log, cancel: {"ok": False, "error": "beets refused"})
        self.assertEqual(job.status, "failed")
        self.assertIn("ERROR: beets refused", job.log)
        self.assertTrue(any(line.startswith(LATE) and "failed" in line for line in job.log), job.log)

    def test_an_error_after_an_unseen_cancel_is_failed_not_cancelled(self):
        def body(log, cancel):
            raise RuntimeError("Beets import failed")
        job = _Gate().run(JobStore(), body)
        self.assertEqual(job.status, "failed")
        self.assertIn("ERROR: Beets import failed", job.log)

    def test_a_job_that_sees_the_cancel_and_returns_is_cancelled(self):
        def body(log, cancel):
            if cancel.is_set():
                log.append("[cancelled]")
                return None
            return {"done": True}
        job = _Gate().run(JobStore(), body)
        self.assertEqual(job.status, "cancelled")
        self.assertFalse(any(line.startswith(LATE) for line in job.log))

    def test_a_job_that_sees_the_cancel_through_wait_is_cancelled(self):
        job = _Gate().run(JobStore(), lambda log, cancel: {"stopped": cancel.wait(1)})
        self.assertEqual(job.status, "cancelled")

    def test_a_job_that_raises_because_of_the_cancel_is_cancelled(self):
        def body(log, cancel):
            if cancel.is_set():
                raise RuntimeError("cancelled")
        job = _Gate().run(JobStore(), body)
        self.assertEqual(job.status, "cancelled")

    def test_kill_after_the_job_finished_does_not_rewrite_its_status(self):
        job = JobStore().start_python(lambda log: {"albums_imported": 1}, label="x")
        _wait(job)
        job.kill()
        self.assertEqual(job.status, "success")
        self.assertEqual(job.to_record()["status"], "success")


class TransactionOutcomeTests(_RouteEnv):
    def _hooked(self):
        import backend.transaction_service as ts
        store_jobs = JobStore(self.data / "jobs")
        for p in (mock.patch.object(ts, "jobs", store_jobs), mock.patch.object(ts, "transactions", self.store)):
            p.start()
            self.addCleanup(p.stop)
        ts._install_transaction_job_hooks()
        self.addCleanup(store_jobs.close)  # before the patches stop (#286)
        return store_jobs

    def _run(self, body):
        job = _Gate().run(self._hooked(), body, label="Import: album",
                          metadata={"transaction": {"operation_type": "Import"}})
        self.assertTrue(self._tx_settled(job))
        return job, self.store.get(job.metadata["transaction_id"])

    def _tx_settled(self, job):
        # The transaction is written on the job's thread; wait for it.
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.store.get(job.metadata["transaction_id"])["status"] != "Running":
                return True
            time.sleep(0.01)
        return False

    def test_a_mutation_that_finished_is_never_labelled_cancelled(self):
        job, tx = self._run(lambda log, cancel: {"albums_imported": 1})
        self.assertEqual((job.status, tx["status"]), ("success", "Completed"))
        self.assertTrue(any("Cancel requested, but the job had already done its work: recorded as Completed."
                            in line for line in tx.get("logs") or []), tx.get("logs"))

    def test_a_mutation_that_failed_after_an_unseen_cancel_is_failed(self):
        def body(log, cancel):
            raise RuntimeError("Beets import failed")
        job, tx = self._run(body)
        self.assertEqual((job.status, tx["status"]), ("failed", "Failed"))

    def test_a_job_that_stopped_for_the_cancel_is_cancelled(self):
        def body(log, cancel):
            if cancel.is_set():
                raise RuntimeError("cancelled")
            return {"albums_imported": 1}
        job, tx = self._run(body)
        self.assertEqual((job.status, tx["status"]), ("cancelled", "Cancelled"))


if __name__ == "__main__":
    unittest.main()
