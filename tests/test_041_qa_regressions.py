"""Independent QA regression tests for 0.4.1 (gaps next to the implementers' tests).

- #317: a mutating child that published ``mutation_started`` and then stopped
  on the cancel; the idle-stall and timeout paths of ``_wait_for_child_job``.
"""

import threading
import time
import unittest
from unittest import mock

import tests.test_041_transaction_followups as tf  # module import: its classes are not re-collected here


class ChildJobQaTests(tf.ChildJobReconcileTests):
    """Reuses the implementer's fixture; its own tests are not re-run here."""

    for _name in [n for n in dir(tf.ChildJobReconcileTests) if n.startswith("test_")]:
        locals()[_name] = None  # not callable, so unittest does not collect it
    del _name

    def _run_parent(self, child, **wait_kwargs):
        started = threading.Event()

        def parent(log, cancel_event, update_state=None):
            started.set()
            return self.js._wait_for_child_job(child.job_id, log, cancel_event, prefix="import", **wait_kwargs)

        job = self.jobs.start_python(parent, label="Parent",
                                     metadata={"transaction": {"operation_type": "Library Cleanup"}})
        self.assertTrue(started.wait(5))
        return job

    def _settled(self, job):
        # #384: the transaction turns terminal before its ERROR line is
        # appended, so wait for the parent job itself: it ends only after
        # every transaction write of its wrapper.
        self.assertTrue(tf._wait(lambda: job.status != "running"))
        tx_id = job.metadata["transaction_id"]
        self.assertNotEqual(self.store.get(tx_id)["status"], "Running")
        return self.store.get(tx_id)

    def test_child_that_published_mutation_started_then_stopped_is_partial_not_cancelled(self):
        def child_body(log, cancel, update_state):
            update_state({"mutation_started": True})  # changed some items...
            while not cancel.is_set():
                time.sleep(0.01)
            raise RuntimeError("cancelled")  # ...then honoured the cancel
        child = self._child(child_body)
        job = self._run_parent(child, timeout=60)
        self.assertTrue(tf._wait(lambda: child.status == "running"))
        job.kill()
        tx = self._settled(job)
        self.assertEqual(child.status, "cancelled")
        self.assertEqual(tx["status"], "Failed")
        self.assertTrue(tx["metadata"]["engine_result"]["partial_cancel"])
        self.assertIn("may have changed the library", " ".join(job.log))

    def test_idle_stalled_child_that_then_finishes_returns_its_result(self):
        def child_body(log, cancel):
            while not cancel.requested:  # no log lines, no state: looks stalled
                time.sleep(0.01)
            return {"ok": True, "imported": 1}
        child = self._child(child_body)
        job = self._run_parent(child, timeout=0, idle_timeout=0.1)
        tx = self._settled(job)
        self.assertEqual(tx["status"], "Completed")
        self.assertEqual(job.result, {"ok": True, "imported": 1})
        self.assertIn("finished its work after all", " ".join(job.log))

    def test_timed_out_child_still_running_after_the_wait_is_recovery_required(self):
        self.js.CHILD_SETTLE_SECONDS = 0.2
        child = self._child(lambda log, cancel: self.release.wait(30))  # ignores the stop request
        job = self._run_parent(child, timeout=0.1)
        tx = self._settled(job)
        self.assertEqual(child.status, "running")
        self.assertEqual(tx["status"], "Recovery Required")
        self.assertTrue(tx["metadata"]["engine_result"]["outcome_unknown"])
        self.assertIn("outcome is unknown", " ".join(map(str, tx["logs"])))



if __name__ == "__main__":
    unittest.main()
