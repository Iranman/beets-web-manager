"""Job engine lifecycle fixes (wave 5): BA-3 failed results, BA-6 duplicate
starts, BA-10 "clear done", #229 thread start failure, and the transaction
hook's matching rules."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from job_engine import DuplicateJobError, JobStore
from tests.test_staging_mutation_hardening import _RouteEnv


def _wait(job, timeout=5.0):
    deadline = time.time() + timeout
    while job.status == "running" and time.time() < deadline:
        time.sleep(0.01)


class FailedResultTests(unittest.TestCase):
    """BA-3: a job that returns {"ok": False} did not succeed."""

    def test_ok_false_result_is_failed(self):
        job = JobStore().start_python(lambda log: {"ok": False, "error": "engine refused"}, label="x")
        _wait(job)
        self.assertEqual((job.status, job.returncode), ("failed", 1))
        self.assertEqual(job.result, {"ok": False, "error": "engine refused"})
        self.assertIn("ERROR: engine refused", job.log)

    def test_other_results_still_succeed(self):
        for result in ({"ok": True}, {"n": 1}, None, [1], {"ok": None}):
            job = JobStore().start_python(lambda log, r=result: r, label="x")
            _wait(job)
            self.assertEqual(job.status, "success", result)

    def test_cancelled_job_with_ok_false_stays_cancelled(self):
        go = threading.Event()

        def body(log, cancel):
            go.wait(5)
            return {"ok": False}
        job = JobStore().start_python(body, label="x")
        job.kill()
        go.set()
        _wait(job)
        self.assertEqual(job.status, "cancelled")


class DuplicateStartTests(unittest.TestCase):
    """BA-6: an identical mutating job cannot start while one runs."""

    def setUp(self):
        self.go = threading.Event()
        self.addCleanup(self.go.set)
        self.store = JobStore()

    def _blocking(self, log):
        self.go.wait(5)

    def test_identical_mutating_job_is_refused(self):
        first = self.store.start_python(self._blocking, label="Move all", metadata={"type": "move-all"})
        with self.assertRaises(DuplicateJobError) as ctx:
            self.store.start_python(self._blocking, label="Move all", metadata={"type": "move-all"})
        self.assertIs(ctx.exception.job, first)
        self.assertEqual(len(self.store.all()), 1)

    def test_label_only_mutating_job_is_refused(self):
        self.store.start_python(self._blocking, label="Clean orphaned library items")
        with self.assertRaises(DuplicateJobError):
            self.store.start_python(self._blocking, label="Clean orphaned library items")

    def test_different_subject_or_read_only_may_run_together(self):
        self.store.start_python(self._blocking, label="Fix art", metadata={"type": "art", "album_id": 1})
        self.store.start_python(self._blocking, label="Fix art", metadata={"type": "art", "album_id": 2})
        self.store.start_python(self._blocking, label="Scan", metadata={"type": "dedup-scan"})
        self.store.start_python(self._blocking, label="Scan", metadata={"type": "dedup-scan"})
        self.assertEqual(len(self.store.all()), 4)

    def test_finished_job_does_not_block_a_new_start(self):
        self.go.set()
        first = self.store.start_python(self._blocking, label="Move all")
        _wait(first)
        self.store.start_python(self._blocking, label="Move all")
        self.assertEqual(len(self.store.all()), 2)


class ClearDoneTests(unittest.TestCase):
    """BA-10: "clear done" keeps jobs that still need recovery."""

    def test_recovery_required_survives_clear(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store = JobStore(root)
            done = store.start_python(lambda log: None, label="done")
            _wait(done)
            stuck = store.start_python(lambda log: None, label="stuck")
            _wait(stuck)
            stuck._terminal = "recovery_required"
            stuck.save(force=True)
            store.clear_finished()
            self.assertEqual([j.job_id for j in store.all()], [stuck.job_id])
            self.assertTrue((root / f"{stuck.job_id}.json").exists())
            self.assertFalse((root / f"{done.job_id}.json").exists())


class ThreadStartFailureTests(unittest.TestCase):
    """#229: a job whose thread cannot start is failed, not running forever."""

    def test_start_failure_marks_job_failed(self):
        store = JobStore()
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("can't start new thread")):
            with self.assertRaises(RuntimeError):
                store.start_python(lambda log: None, label="x")
        (job,) = store.all()
        self.assertEqual(job.status, "failed")
        self.assertIsNotNone(job.finished_at)
        # A failed start does not block the next identical start.
        store.start_python(lambda log: None, label="x")


class TransactionHookTests(_RouteEnv):
    """The hook-created transaction follows the same rules as the job."""

    def _hooked(self):
        import backend.transaction_service as ts
        store_jobs = JobStore(self.data / "jobs")
        for p in (mock.patch.object(ts, "jobs", store_jobs), mock.patch.object(ts, "transactions", self.store)):
            p.start()
            self.addCleanup(p.stop)
        ts._install_transaction_job_hooks()
        return store_jobs

    def test_ok_false_result_fails_the_transaction(self):
        jobs = self._hooked()
        job = jobs.start_python(lambda log: {"ok": False, "error": "no"}, label="Library cleanup",
                                metadata={"transaction": {"operation_type": "Delete"}})
        _wait(job)
        self.assertEqual(job.status, "failed")
        self.assertEqual(self.store.get(job.metadata["transaction_id"])["status"], "Failed")

    def test_thread_start_failure_fails_the_transaction(self):
        jobs = self._hooked()
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("no thread")):
            with self.assertRaises(RuntimeError):
                jobs.start_python(lambda log: None, label="Library cleanup",
                                  metadata={"transaction": {"operation_type": "Delete"}})
        (tx,) = self.store.list()[0]
        self.assertEqual(tx["status"], "Failed")

    def test_duplicate_is_refused_before_a_transaction_is_recorded(self):
        jobs = self._hooked()
        go = threading.Event()
        self.addCleanup(go.set)
        meta = {"transaction": {"operation_type": "Delete"}}
        jobs.start_python(lambda log: go.wait(5), label="Library cleanup", metadata=meta)
        with self.assertRaises(DuplicateJobError):
            jobs.start_python(lambda log: go.wait(5), label="Library cleanup", metadata=meta)
        self.assertEqual(self.store.list()[1], 1)

    def test_duplicate_maps_to_409(self):
        import app as app_module
        job = JobStore().start_python(lambda log: None, label="x")
        with app_module.app.test_request_context():
            resp = app_module.app.make_response(app_module.app.handle_user_exception(DuplicateJobError(job)))
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["code"], "job_already_running")
        self.assertEqual(resp.get_json()["job_id"], job.job_id)


class NoAdHocThreadTests(unittest.TestCase):
    """BA-16 / IA-18: these waits run inside the calling job, no helper thread."""

    def test_plex_refresh_runs_inline(self):
        import backend.plex_service as plex
        log = []
        with mock.patch.object(threading, "Thread", side_effect=AssertionError("thread started")),                 mock.patch.object(plex, "_plex_settings", return_value={"url": "http://plex", "token": "t"}),                 mock.patch.object(plex, "_plex_find_music_section", return_value=(None, "3", "Music")),                 mock.patch.object(plex, "_plex_request", return_value={}) as req:
            self.assertTrue(plex._trigger_plex_refresh(log, workflow="manual"))
        req.assert_called_once_with("/library/sections/3/refresh", timeout=10, attempts=1)
        self.assertEqual(log, ["  [plex] Refresh triggered (Music)"])

    def test_plex_refresh_failure_is_logged_not_raised(self):
        import backend.plex_service as plex
        log = []
        with mock.patch.object(plex, "_plex_settings", return_value={"url": "http://plex", "token": "t"}),                 mock.patch.object(plex, "_plex_find_music_section", side_effect=TimeoutError("timed out")):
            self.assertFalse(plex._trigger_plex_refresh(log, workflow="manual"))
        self.assertIn("timed out", log[-1])

    def test_ytdlp_metadata_extraction_starts_no_thread(self):
        import inspect
        import routes_submissions
        self.assertNotIn("Thread(", inspect.getsource(routes_submissions._extract_ytdlp_info))

    def test_slskd_busy_wait_honours_cancel(self):
        import backend.slskd_service as slskd
        cancel = threading.Event()
        cancel.set()
        busy = RuntimeError("HTTP 429: Only one concurrent operation is permitted")
        with mock.patch.object(slskd, "_slskd_req", side_effect=busy),                 mock.patch.object(slskd.time, "sleep", side_effect=AssertionError("slept")):
            with self.assertRaisesRegex(RuntimeError, "^cancelled$"):
                slskd._slskd_search_and_queue("A", "B", "", [], busy_retries=5, cancel_event=cancel)


class DownloadsRootTests(unittest.TestCase):
    """BA-12: download paths come from DOWNLOADS_ROOT, not one deployment."""

    def test_downloads_root_is_the_configured_container_root(self):
        from backend import app_runtime, config_layers
        self.assertEqual(app_runtime.DOWNLOADS_ROOT, Path(config_layers.downloads_root()))

    def test_no_maintainer_download_layout_in_these_modules(self):
        root = Path(__file__).resolve().parents[1]
        for rel in ("backend/app_runtime.py", "backend/serializers.py", "routes_import.py"):
            self.assertNotIn("/data/torrents", (root / rel).read_text(encoding="utf-8"), rel)


if __name__ == "__main__":
    unittest.main()
