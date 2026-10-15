"""QA #412: the album-download wait is progress-aware, a stalled download is
still stopped, a cancel still cancels, and the Download All loop really passes
the progress-aware wait (behavioural, not a source grep)."""

import threading
import time
import unittest
from unittest import mock

import tests.test_041_qa_regressions as qa
import tests.test_041_transaction_followups as tf
import tests.test_acquisition_queue_followups_070 as dl


def setUpModule():
    dl.setUpModule()


class StallAndCancelTests(qa.ChildJobQaTests):
    for _name in [n for n in dir(qa.ChildJobQaTests) if n.startswith("test_")]:
        locals()[_name] = None
    del _name

    def test_child_that_goes_silent_past_the_deadline_is_stopped(self):
        def child_body(log, cancel):
            end = time.time() + 0.3
            while time.time() < end:  # progress until past the deadline...
                log.append("[slskd] 4/8 file(s) downloaded")
                time.sleep(0.02)
            while not cancel.requested:  # ...then a stall: no lines, no state
                time.sleep(0.01)
            raise RuntimeError("cancelled")
        child = self._child(child_body)
        job = self._run_parent(child, timeout=0.1, idle_timeout=0.5)
        self.assertTrue(tf._wait(lambda: job.status != "running"))
        self.assertEqual(job.status, "failed", "\n".join(job.log))
        self.assertEqual(child.status, "cancelled")
        self.assertIn("Still making progress", " ".join(job.log))
        self.assertIn("timed out", " ".join(job.log))

    def test_cancel_while_past_the_deadline_still_cancels_the_child(self):
        def child_body(log, cancel):
            while not cancel.requested:
                log.append("[slskd] 4/8 file(s) downloaded")
                time.sleep(0.02)
            raise RuntimeError("cancelled")
        child = self._child(child_body)
        job = self._run_parent(child, timeout=0.1, idle_timeout=5)
        self.assertTrue(tf._wait(lambda: "Still making progress" in " ".join(job.log)))
        job.kill()
        self.assertTrue(tf._wait(lambda: job.status != "running"))
        self.assertEqual(job.status, "cancelled", "\n".join(job.log))
        self.assertTrue(tf._wait(lambda: child.status == "cancelled"))


class DownloadAllCallerTests(dl.DownloadPatchTests):
    for _name in [n for n in dir(dl.DownloadPatchTests) if n.startswith("test_")]:
        locals()[_name] = None
    del _name

    def test_download_all_waits_with_an_idle_timeout(self):
        started = mock.Mock(return_value=mock.Mock(job_id="batch"))
        with mock.patch.object(dl.base.routes.jobs, "start_python", started):
            res = self.client.post("/api/acquisition/download-all", json={"keys": [self.rows[0]["key"]]})
        self.assertEqual(res.status_code, 200, res.get_json())
        wait = mock.Mock(return_value={"aldir": "/a", "imported": True, "import_result": None})
        with mock.patch.object(dl.base.routes, "_acq_start_download_job", return_value="child"), \
                mock.patch.object(dl.base.routes, "_wait_for_child_job", wait), \
                mock.patch.object(dl.base.routes, "_invalidate_lib_cache"), \
                mock.patch.object(dl.base.routes, "_save_acq_download_all_last"), \
                mock.patch.object(dl.base.routes.job_contract, "enter"):
            started.call_args.args[0]([], threading.Event(), lambda **kw: None)
        kwargs = wait.call_args.kwargs
        self.assertGreater(kwargs.get("idle_timeout") or 0, 0, kwargs)
        self.assertGreater(kwargs.get("timeout") or 0, kwargs["idle_timeout"], kwargs)


if __name__ == "__main__":
    unittest.main()
