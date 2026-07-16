import threading
import time
import unittest

from job_engine import JobStore


def wait_done(job, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.status != "running":
            return
        time.sleep(0.01)
    raise AssertionError(f"job {job.job_id} did not finish")


class JobStoreRetentionTests(unittest.TestCase):
    def test_python_job_exposes_structured_state_without_breaking_result(self):
        jobs = JobStore()

        def run(log, cancel_event, update_state):
            update_state({
                "category": "Cleanup",
                "current_task": "Scanning folder names",
                "scanned_count": 4,
                "total_count": 10,
                "safe_count": 2,
                "needs_review_count": 1,
            })
            log.append("scan complete")
            return {
                "ok": True,
                "final_summary": {
                    "total_folders_scanned": 10,
                    "safe_to_fix": 2,
                    "needs_review": 1,
                },
            }

        job = jobs.start_python(run, label="structured scan", metadata={"type": "folder-placeholder-scan"})
        wait_done(job)

        payload = job.to_dict(include_log=True)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["result"]["ok"], True)
        self.assertEqual(payload["log"], ["scan complete"])
        self.assertEqual(payload["state"]["job_id"], job.job_id)
        self.assertEqual(payload["state"]["job_name"], "structured scan")
        self.assertEqual(payload["state"]["status"], "success")
        self.assertEqual(payload["state"]["category"], "Cleanup")
        self.assertEqual(payload["state"]["current_task"], "Scanning folder names")
        self.assertEqual(payload["state"]["scanned_count"], 4)
        self.assertEqual(payload["state"]["total_count"], 10)
        self.assertIn("duration_seconds", payload["state"])

    def test_legacy_python_job_without_structured_state_still_works(self):
        jobs = JobStore()
        job = jobs.start_python(lambda log: log.append("legacy complete"), label="legacy")
        wait_done(job)

        payload = job.to_dict(include_log=True)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["log"], ["legacy complete"])
        self.assertNotIn("state", payload)

    def test_python_job_compact_payload_omits_large_result(self):
        jobs = JobStore()

        def run(log):
            log.append("done")
            return {
                "ok": True,
                "issues": [{"id": i, "status": "Needs review"} for i in range(50)],
                "summary": {"total": 50, "safe": 12},
                "message": "x" * 300,
            }

        job = jobs.start_python(run, label="large result")
        wait_done(job)

        compact = job.to_dict(include_result=False)
        self.assertNotIn("result", compact)
        self.assertEqual(compact["result_summary"]["type"], "dict")
        self.assertEqual(compact["result_summary"]["sizes"]["issues"], 50)
        self.assertEqual(compact["result_summary"]["sizes"]["summary"], 2)
        self.assertLessEqual(len(compact["result_summary"]["scalars"]["message"]), 160)

        detail = job.to_dict(include_log=True)
        self.assertIn("result", detail)
        self.assertEqual(len(detail["result"]["issues"]), 50)
        self.assertEqual(detail["log"], ["done"])
    def test_prune_finished_preserves_recent_metadata_and_running_jobs(self):
        jobs = JobStore()
        release_running = threading.Event()

        old_plain = jobs.start_python(lambda log: log.append("old"), label="old plain")
        recent_plain = jobs.start_python(lambda log: log.append("recent"), label="recent plain")
        metadata_job = jobs.start_python(
            lambda log: {"ok": True},
            label="Clean result",
            metadata={"type": "library-health-scan"},
        )
        running_job = jobs.start_python(
            lambda log, cancel_event: release_running.wait(2),
            label="still running",
        )

        try:
            for job in (old_plain, recent_plain, metadata_job):
                wait_done(job)

            now = time.time()
            old_plain.finished_at = now - 7200
            recent_plain.finished_at = now - 60
            metadata_job.finished_at = now - 7200

            jobs.prune_finished(
                max_age_seconds=3600,
                metadata_max_age_seconds=86400,
                max_finished=10,
            )

            kept_ids = {job.job_id for job in jobs.all()}
            self.assertNotIn(old_plain.job_id, kept_ids)
            self.assertIn(recent_plain.job_id, kept_ids)
            self.assertIn(metadata_job.job_id, kept_ids)
            self.assertIn(running_job.job_id, kept_ids)
            self.assertEqual(jobs.get(metadata_job.job_id).result, {"ok": True})
        finally:
            release_running.set()

    def test_clear_finished_still_removes_all_finished_jobs(self):
        jobs = JobStore()
        done = jobs.start_python(lambda log: log.append("done"), label="done")
        running_gate = threading.Event()
        running = jobs.start_python(
            lambda log, cancel_event: running_gate.wait(2),
            label="running",
        )

        try:
            wait_done(done)
            jobs.clear_finished()
            kept_ids = {job.job_id for job in jobs.all()}
            self.assertNotIn(done.job_id, kept_ids)
            self.assertIn(running.job_id, kept_ids)
        finally:
            running_gate.set()


if __name__ == "__main__":
    unittest.main()
