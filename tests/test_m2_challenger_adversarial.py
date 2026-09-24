"""Milestone 2 Empirical Adversarial Challenge Tests.

Adversarially challenges library_mbsync_all and library_move_all in app.py:
1. Simulate engine offline (BeetsUnavailableError): Verify fail-closed, log structured error, zero subprocess fallback.
2. Simulate remote job failure (non-zero return code): Check job status and server diagnostics.
3. Simulate user cancellation (cancel_event.set()): Verify cancel_job call and clean termination.
4. Verify timeout handling and empty directory cleanup in move_all.
5. Verify zero subprocess.Popen invocation under any error or normal condition.
"""

import ast
import inspect
import threading
import subprocess
import time
import unittest
from unittest import mock

import app as app_module
from backend.beets_adapter import BeetsUnavailableError, BeetsError


class M2AdversarialBase(unittest.TestCase):
    def setUp(self):
        self._inv_patch = mock.patch.object(app_module, "_invalidate_lib_cache")
        self._inv_patch.start()
        self._plex_patch = mock.patch.object(app_module, "_trigger_plex_refresh")
        self._plex_patch.start()

        # Guard against any Beets subprocess invocation in app
        self._beet_subprocess_calls = []
        real_popen = subprocess.Popen

        def _guarded_popen(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args")
            cmd_str = str(cmd)
            if "beet" in cmd_str.lower():
                self._beet_subprocess_calls.append((args, kwargs))
                raise RuntimeError(f"Forbidden local Beets subprocess execution: {cmd}")
            return real_popen(*args, **kwargs)

        self._popen_patch = mock.patch("subprocess.Popen", side_effect=_guarded_popen)
        self._popen_patch.start()

    def tearDown(self):
        self._popen_patch.stop()
        self._plex_patch.stop()
        self._inv_patch.stop()

    def _run_job_sync(self, route_fn, route_path, cancel_event=None):
        """Invoke route endpoint and run the resulting PythonJob synchronously to completion."""
        captured_job = {}

        with app_module.app.test_request_context(route_path, method="POST"):
            resp = route_fn()
            data = resp.get_json() if hasattr(resp, "get_json") else resp[0].get_json()
            job_id = data.get("job_id")
            job = app_module.jobs.get(job_id)
            captured_job["job"] = job

        # Wait for PythonJob background thread to complete
        if captured_job.get("job"):
            job = captured_job["job"]
            deadline = time.monotonic() + 5.0
            while job.finished_at is None and time.monotonic() < deadline:
                time.sleep(0.05)
            return job
        return None


class TestEngineOfflineFailClosed(M2AdversarialBase):
    """Challenge 1: Simulate engine offline (BeetsUnavailableError)."""

    def test_mbsync_all_engine_offline_on_orphan_lookup_and_mbsync(self):
        """When engine is offline, mbsync must fail closed, log error, and not call subprocess."""
        with mock.patch.object(app_module.composite_workflows, "find_all_orphan_albums", side_effect=BeetsUnavailableError("Engine connection refused")), \
             mock.patch.object(app_module.composite_workflows, "mbsync", side_effect=BeetsUnavailableError("Engine connection refused")):

            job = self._run_job_sync(app_module.library_mbsync_all, "/api/library/mbsync-all")

            self.assertIsNotNone(job)
            self.assertEqual(job.status, "failed")
            self.assertEqual(job.returncode, 1)
            self.assertTrue(any("ERROR:" in line for line in job.log))
            self.assertTrue(any("Engine connection refused" in line for line in job.log))
            self.assertEqual(self._beet_subprocess_calls, [])

    def test_move_all_engine_offline_on_path_scan_and_move(self):
        """When engine is offline, move_all must fail closed, log error, and not call subprocess."""
        with mock.patch.object(app_module.composite_workflows, "list_distinct_item_paths", side_effect=BeetsUnavailableError("Engine unreachable")), \
             mock.patch.object(app_module.composite_workflows, "move_library", side_effect=BeetsUnavailableError("Engine unreachable")), \
             mock.patch.object(app_module.composite_workflows, "plan_folder_cleanup") as mock_plan:

            job = self._run_job_sync(app_module.library_move_all, "/api/library/move-all")

            self.assertIsNotNone(job)
            self.assertEqual(job.status, "failed")
            self.assertEqual(job.returncode, 1)
            self.assertTrue(any("ERROR:" in line for line in job.log))
            self.assertTrue(any("Engine unreachable" in line for line in job.log))
            mock_plan.assert_not_called()
            self.assertEqual(self._beet_subprocess_calls, [])


class TestRemoteJobFailureDiagnostics(M2AdversarialBase):
    """Challenge 2: Simulate remote job failure (non-zero return code)."""

    def test_mbsync_remote_failure_rc2_sets_failed_status(self):
        """When remote mbsync returns returncode=2 (fatal exit), job status must be failed."""
        remote_job_id = "mbsync-remote-fail-2"
        responses = [
            {"status": "running", "stdout": ["syncing tracks..."], "stderr": []},
            {"status": "failed", "returncode": 2, "stdout": [], "stderr": ["fatal database lock error", "aborting"]},
        ]

        with mock.patch.object(app_module.composite_workflows, "find_all_orphan_albums", return_value=[]), \
             mock.patch.object(app_module.composite_workflows, "mbsync", return_value={"ok": True, "job_id": remote_job_id}), \
             mock.patch.object(app_module.composite_workflows, "get_job", side_effect=responses):

            job = self._run_job_sync(app_module.library_mbsync_all, "/api/library/mbsync-all")

            self.assertIsNotNone(job)
            self.assertEqual(job.status, "failed")
            self.assertEqual(job.returncode, 1)
            self.assertTrue(any("fatal database lock error" in line for line in job.log))
            self.assertEqual(self._beet_subprocess_calls, [])

    def test_mbsync_remote_failure_rc1_status_investigation(self):
        """Examine job status when remote mbsync returns returncode=1 (error exit)."""
        remote_job_id = "mbsync-remote-fail-1"
        responses = [
            {"status": "failed", "returncode": 1, "stdout": [], "stderr": ["tag update error for track 42"]},
        ]

        with mock.patch.object(app_module.composite_workflows, "find_all_orphan_albums", return_value=[]), \
             mock.patch.object(app_module.composite_workflows, "mbsync", return_value={"ok": True, "job_id": remote_job_id}), \
             mock.patch.object(app_module.composite_workflows, "get_job", side_effect=responses):

            job = self._run_job_sync(app_module.library_mbsync_all, "/api/library/mbsync-all")

            self.assertIsNotNone(job)
            # Server diagnostics are logged
            self.assertTrue(any("tag update error for track 42" in line for line in job.log))
            # Observe actual status for rc=1
            print(f"\n[EMPIRICAL OBSERVE] mbsync rc=1 -> job.status={job.status}, returncode={job.returncode}")

    def test_move_all_remote_failure_rc1_cleanup_and_status_investigation(self):
        """Examine move_all behavior when remote move_library fails with returncode=1 (e.g. rescan/update failed)."""
        remote_job_id = "move-remote-fail-1"
        responses = [
            {"status": "failed", "returncode": 1, "stdout": [], "stderr": ["rescan disk error: file corrupt"]},
        ]

        with mock.patch.object(app_module.composite_workflows, "list_distinct_item_paths", return_value=["Artist/Album/track.mp3"]), \
             mock.patch.object(app_module.composite_workflows, "move_library", return_value={"ok": True, "job_id": remote_job_id}), \
             mock.patch.object(app_module.composite_workflows, "get_job", side_effect=responses), \
             mock.patch.object(app_module.composite_workflows, "plan_folder_cleanup") as mock_plan:

            job = self._run_job_sync(app_module.library_move_all, "/api/library/move-all")

            self.assertIsNotNone(job)
            # Server diagnostics are logged
            self.assertTrue(any("rescan disk error: file corrupt" in line for line in job.log))
            print(f"\n[EMPIRICAL OBSERVE] move_all rc=1 -> job.status={job.status}, returncode={job.returncode}, plan_folder_cleanup.called={mock_plan.called}")


class TestUserCancellation(M2AdversarialBase):
    """Challenge 3: Simulate user cancellation (cancel_event.set())."""

    def test_mbsync_cancellation_during_remote_execution(self):
        """Cancelling mbsync must invoke beets_client.cancel_job and append [cancelled]."""
        remote_job_id = "mbsync-remote-cancel"
        cancel_called = threading.Event()

        def fake_cancel_job(jid):
            if jid == remote_job_id:
                cancel_called.set()
            return {"ok": True}

        def fake_get_job(jid):
            time.sleep(0.05)
            return {"status": "running", "stdout": ["processing..."], "stderr": []}

        with mock.patch.object(app_module.composite_workflows, "find_all_orphan_albums", return_value=[]), \
             mock.patch.object(app_module.composite_workflows, "mbsync", return_value={"ok": True, "job_id": remote_job_id}), \
             mock.patch.object(app_module.composite_workflows, "get_job", side_effect=fake_get_job), \
             mock.patch.object(app_module.composite_workflows, "cancel_job", side_effect=fake_cancel_job):

            with app_module.app.test_request_context("/api/library/mbsync-all", method="POST"):
                resp = app_module.library_mbsync_all()
                job_id = resp.get_json()["job_id"]
                job = app_module.jobs.get(job_id)

            # Let it start polling, then trigger cancellation
            time.sleep(0.1)
            job.kill()

            deadline = time.time() + 5.0
            while job.finished_at is None and time.time() < deadline:
                time.sleep(0.05)

            self.assertTrue(cancel_called.is_set(), "composite_workflows.cancel_job was not called with remote_job_id")
            self.assertTrue(any("[cancelled]" in line for line in job.log))
            self.assertEqual(self._beet_subprocess_calls, [])

    def test_move_all_cancellation_aborts_empty_dir_cleanup(self):
        """Cancelling move_all must invoke beets_client.cancel_job and not execute folder cleanup."""
        remote_job_id = "move-remote-cancel"
        cancel_called = threading.Event()

        def fake_cancel_job(jid):
            if jid == remote_job_id:
                cancel_called.set()
            return {"ok": True}

        def fake_get_job(jid):
            time.sleep(0.05)
            return {"status": "running", "stdout": ["updating..."], "stderr": []}

        with mock.patch.object(app_module.composite_workflows, "list_distinct_item_paths", return_value=["Artist/Album/track.mp3"]), \
             mock.patch.object(app_module.composite_workflows, "move_library", return_value={"ok": True, "job_id": remote_job_id}), \
             mock.patch.object(app_module.composite_workflows, "get_job", side_effect=fake_get_job), \
             mock.patch.object(app_module.composite_workflows, "cancel_job", side_effect=fake_cancel_job), \
             mock.patch.object(app_module.composite_workflows, "plan_folder_cleanup") as mock_plan:

            with app_module.app.test_request_context("/api/library/move-all", method="POST"):
                resp = app_module.library_move_all()
                job_id = resp.get_json()["job_id"]
                job = app_module.jobs.get(job_id)

            time.sleep(0.1)
            job.kill()

            deadline = time.time() + 5.0
            while job.finished_at is None and time.time() < deadline:
                time.sleep(0.05)

            self.assertTrue(cancel_called.is_set(), "composite_workflows.cancel_job was not called")
            self.assertTrue(any("[cancelled]" in line for line in job.log))
            mock_plan.assert_not_called()
            self.assertEqual(self._beet_subprocess_calls, [])


class TestTimeoutHandlingAndCleanup(M2AdversarialBase):
    """Challenge 4: Verify timeout handling and empty directory cleanup in move_all."""

    def test_move_all_remote_status_timeout_aborts_cleanup(self):
        """When remote status is timeout, move_all must abort folder cleanup."""
        remote_job_id = "move-remote-timeout"
        responses = [
            {"status": "timeout", "returncode": 124, "stdout": [], "stderr": ["Command timed out"]},
        ]

        with mock.patch.object(app_module.composite_workflows, "list_distinct_item_paths", return_value=["Artist/Album/track.mp3"]), \
             mock.patch.object(app_module.composite_workflows, "move_library", return_value={"ok": True, "job_id": remote_job_id}), \
             mock.patch.object(app_module.composite_workflows, "get_job", side_effect=responses), \
             mock.patch.object(app_module.composite_workflows, "plan_folder_cleanup") as mock_plan:

            job = self._run_job_sync(app_module.library_move_all, "/api/library/move-all")

            self.assertIsNotNone(job)
            self.assertTrue(any("timed out" in line for line in job.log))
            mock_plan.assert_not_called()
            print(f"\n[EMPIRICAL OBSERVE] move_all remote timeout -> job.status={job.status}, returncode={job.returncode}")

    def test_move_all_local_deadline_timeout_cancels_remote_and_aborts_cleanup(self):
        """When local deadline expires, move_all must cancel remote job and abort folder cleanup."""
        remote_job_id = "move-local-timeout"
        cancel_called = threading.Event()
        move_called = [False]

        def fake_move(*args, **kwargs):
            move_called[0] = True
            return {"ok": True, "job_id": remote_job_id}

        def fake_cancel(jid):
            cancel_called.set()
            return {"ok": True}

        # Mock time.time() to simulate deadline expiration during polling
        real_time = time.time
        start_time = real_time()

        def fake_time():
            if move_called[0]:
                return start_time + 6000.0
            return start_time

        with mock.patch.object(app_module.composite_workflows, "list_distinct_item_paths", return_value=["Artist/Album/track.mp3"]), \
             mock.patch.object(app_module.composite_workflows, "move_library", side_effect=fake_move), \
             mock.patch.object(app_module.composite_workflows, "get_job", return_value={"status": "running"}), \
             mock.patch.object(app_module.composite_workflows, "cancel_job", side_effect=fake_cancel), \
             mock.patch.object(app_module.composite_workflows, "plan_folder_cleanup") as mock_plan, \
             mock.patch.object(app_module.time, "time", side_effect=fake_time):

            job = self._run_job_sync(app_module.library_move_all, "/api/library/move-all")

            self.assertIsNotNone(job)
            self.assertTrue(cancel_called.is_set())
            self.assertTrue(any("timed out" in line for line in job.log))
            mock_plan.assert_not_called()
            print(f"\n[EMPIRICAL OBSERVE] move_all local timeout -> job.status={job.status}, returncode={job.returncode}")

    def test_move_all_clean_success_executes_empty_dir_cleanup_via_engine(self):
        """On clean success (rc=0), empty directory cleanup plans and applies via engine IPC."""
        remote_job_id = "move-clean-success"
        planned = []
        applied = []

        def fake_plan(payload):
            src = payload["source"]
            planned.append(src)
            return {"ok": True, "operation_id": f"op-{src}"}

        def fake_apply(op_id):
            applied.append(op_id)
            return {"ok": True}

        with mock.patch.object(app_module.composite_workflows, "list_distinct_item_paths", return_value=["ArtistA/AlbumA/track1.mp3", "ArtistA/AlbumA/track2.mp3"]), \
             mock.patch.object(app_module.composite_workflows, "move_library", return_value={"ok": True, "job_id": remote_job_id}), \
             mock.patch.object(app_module.composite_workflows, "get_job", return_value={"status": "success", "returncode": 0, "stdout": ["moved 2 tracks"]}), \
             mock.patch.object(app_module.composite_workflows, "plan_folder_cleanup", side_effect=fake_plan) as mock_plan, \
             mock.patch.object(app_module.composite_workflows, "apply_folder_cleanup", side_effect=fake_apply) as mock_apply:

            job = self._run_job_sync(app_module.library_move_all, "/api/library/move-all")

            self.assertIsNotNone(job)
            self.assertEqual(job.status, "success")
            self.assertEqual(job.returncode, 0)
            self.assertTrue(len(planned) > 0)
            self.assertEqual(len(planned), len(applied))
            self.assertTrue(any("Removed empty folder" in line for line in job.log))
            self.assertTrue(any("Cleaned up" in line for line in job.log))
            self.assertEqual(self._beet_subprocess_calls, [])


if __name__ == "__main__":
    unittest.main()
