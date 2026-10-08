"""The shared job contract (backend/job_contract.py, ARCH-004): a durable
``workflow:<name>`` lock held for the job's lifetime, a persisted contract
checkpoint, workflow progress republished into the job record, and
cancellation honoured while waiting."""

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import backend.job_contract as jc
from backend.resource_locks import ResourceLocks, set_locks, validate_key
from job_engine import JobStore
from tests._job_store_cleanup import close_job_stores_at_cleanup


#: A second OS process that holds a lock until its stdin closes. A plain
#: subprocess (not multiprocessing): it never re-imports the test runner.
_HOLDER = """
import sys
from pathlib import Path
from backend.resource_locks import ResourceLocks
registry = ResourceLocks(Path(sys.argv[1]))
registry.acquire([sys.argv[2]], "other-process", timeout=5)
print("ready", flush=True)
sys.stdin.readline()
registry.release([sys.argv[2]], "other-process")
"""


REPO = Path(__file__).resolve().parent.parent


def _wait(job, timeout=10.0):
    deadline = time.time() + timeout
    while job.status == "running" and time.time() < deadline:
        time.sleep(0.01)


class JobContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        close_job_stores_at_cleanup(self)
        self.root = Path(self._tmp.name)
        self.locks = ResourceLocks(self.root / "locks")
        set_locks(self.locks)
        self.addCleanup(set_locks, None)
        self.jobs = JobStore(self.root / "jobs")

    def _record(self, job):
        return json.loads((self.root / "jobs" / f"{job.job_id}.json").read_text(encoding="utf-8"))

    def test_workflow_keys_are_valid_level_one_locks(self):
        self.assertEqual(validate_key(jc.workflow_key("maintenance-runner")), "workflow:maintenance-runner")
        self.assertEqual(validate_key(jc.workflow_key("playlist-download-" + jc.slug("My Playlist / 2024"))),
                         "workflow:playlist-download-" + jc.slug("my playlist / 2024"))
        with self.assertRaises(ValueError):
            validate_key("workflow:Has Spaces")

    def test_job_holds_the_lock_while_running_and_releases_it(self):
        inside = threading.Event()
        release = threading.Event()
        seen = {}

        def work(log, cancel_event=None, update_state=None):
            seen["held"] = [r["key"] for r in self.locks.held()]
            inside.set()
            release.wait(5)
            return {"done": True}

        job = self.jobs.start_python(jc.guarded(work, workflow="clean-all"), label="x",
                                     metadata=jc.contract_metadata("clean-all", {"type": "maintenance-runner"}))
        self.assertTrue(inside.wait(5))
        self.assertEqual(seen["held"], ["workflow:clean-all"])
        checkpoint = self._record(job)["state"]["checkpoint"]
        self.assertEqual((checkpoint["workflow"], checkpoint["stage"], checkpoint["lock_keys"]),
                         ("clean-all", "running", ["workflow:clean-all"]))
        release.set()
        _wait(job)
        self.assertEqual((job.status, job.result), ("success", {"done": True}))
        self.assertEqual(self.locks.held(), [])
        self.assertTrue(job.metadata["mutating"])
        self.assertEqual(job.metadata["workflow_contract"]["lock_keys"], ["workflow:clean-all"])

    def test_every_call_shape_of_a_job_function_is_supported(self):
        calls = []
        shapes = (lambda log: calls.append(1),
                  lambda log, cancel_event: calls.append(2),
                  lambda log, cancel_event, update_state: calls.append(3))
        for fn in shapes:
            job = self.jobs.start_python(jc.guarded(fn, workflow="shape"), label="s")
            _wait(job)
            self.assertEqual(job.status, "success")
        self.assertEqual(calls, [1, 2, 3])

    def test_the_lock_is_released_when_the_job_fails(self):
        def boom(log):
            raise ValueError("nope")

        job = self.jobs.start_python(jc.guarded(boom, workflow="fails"), label="f")
        _wait(job)
        self.assertEqual(job.status, "failed")
        self.assertEqual(self.locks.held(), [])

    def test_a_second_process_holding_the_workflow_makes_the_job_wait_not_run(self):
        proc = subprocess.Popen([sys.executable, "-c", _HOLDER, str(self.root / "locks"), "workflow:shared"],
                                cwd=str(REPO), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.wait, 10)
        self.addCleanup(lambda: proc.stdin.closed or proc.stdin.close())
        self.assertEqual(proc.stdout.readline().strip(), "ready")
        ran = threading.Event()
        job = self.jobs.start_python(jc.guarded(lambda log: ran.set(), workflow="shared"), label="w")
        time.sleep(1.5)
        self.assertFalse(ran.is_set())  # never two at once
        self.assertEqual(self._record(job)["state"]["checkpoint"]["stage"], "waiting_for_lock")
        self.assertTrue(any("Waiting" in line for line in job.log))
        proc.stdin.close()  # the other process releases the workflow
        proc.wait(10)
        proc.stdout.close()
        _wait(job, 15)
        self.assertTrue(ran.is_set())
        self.assertEqual(job.status, "success")

    def test_waiting_gives_up_as_busy_and_never_runs(self):
        self.locks.acquire(["workflow:busy"], "someone-else")
        ran = []
        job = self.jobs.start_python(jc.guarded(lambda log: ran.append(1), workflow="busy", wait_seconds=0.5),
                                     label="b")
        _wait(job)
        self.assertEqual((job.status, ran), ("failed", []))
        self.assertTrue(any("already running in another process" in line for line in job.log))

    def test_cancel_while_waiting_stops_the_wait(self):
        self.locks.acquire(["workflow:cancel-me"], "someone-else")
        ran = []
        job = self.jobs.start_python(jc.guarded(lambda log: ran.append(1), workflow="cancel-me", wait_seconds=60),
                                     label="c")
        time.sleep(0.3)
        job.kill()
        _wait(job)
        self.assertEqual(ran, [])
        self.assertIn(job.status, ("cancelled", "failed"))

    def test_a_lock_left_by_a_dead_process_is_reclaimed(self):
        stale = {"key": "workflow:crashed", "owner": "job:crashed:dead", "host": "another-container", "pid": 7,
                 "pid_start": "1", "acquired_at": 1.0, "heartbeat_at": 1.0, "ttl": 120.0}
        (self.root / "locks" / "workflow__crashed.lock").write_text(json.dumps(stale), encoding="utf-8")
        job = self.jobs.start_python(jc.guarded(lambda log: "ran", workflow="crashed", wait_seconds=5), label="r")
        _wait(job)
        self.assertEqual((job.status, job.result), ("success", "ran"))

    def test_workflow_progress_is_republished_into_the_job_record(self):
        original = jc.PROGRESS_INTERVAL_SECONDS
        jc.PROGRESS_INTERVAL_SECONDS = 0.0
        self.addCleanup(setattr, jc, "PROGRESS_INTERVAL_SECONDS", original)
        position = {"phase": "download", "done": 0}
        release = threading.Event()

        def work(log, cancel_event=None, update_state=None):
            position.update(done=3)
            release.wait(5)

        job = self.jobs.start_python(
            jc.guarded(work, workflow="progress", progress=lambda: dict(position)), label="p")
        deadline = time.time() + 5
        progress = None
        while time.time() < deadline:
            try:
                progress = self._record(job)["state"]["checkpoint"].get("progress")
            except (OSError, ValueError, KeyError):
                progress = None
            if progress == {"phase": "download", "done": 3}:
                break
            time.sleep(0.05)
        release.set()
        _wait(job)
        self.assertEqual(progress, {"phase": "download", "done": 3})

    def test_a_restart_leaves_the_contract_checkpoint_in_a_recovery_required_job(self):
        release = threading.Event()
        started = threading.Event()

        def work(log, cancel_event=None, update_state=None):
            started.set()
            release.wait(5)

        job = self.jobs.start_python(jc.guarded(work, workflow="restart"), label="r",
                                     metadata=jc.contract_metadata("restart", {"type": "playlist-download"}))
        self.assertTrue(started.wait(5))
        record = self._record(job)
        self.assertEqual(record["status"], "running")
        # A new process reads the record of a job that was running when the old one died.
        recovered = JobStore(self.root / "jobs").get(job.job_id)
        self.assertEqual(recovered.status, "recovery_required")
        self.assertEqual(recovered.recovery["checkpoint"]["workflow"], "restart")
        release.set()
        _wait(job)


def _between(rel, start, end):
    source = (REPO / rel).read_text(encoding="utf-8")
    a = source.index(start)
    return source[a:source.index(end, a)]


class WorkflowAdoptionTests(unittest.TestCase):
    """The long-running mutating workflows named in ARCH-004 run under the
    contract, and take the durable lock AFTER their own in-process guard so
    a same-process duplicate is still refused exactly as before."""

    def test_each_workflow_uses_the_contract(self):
        sites = (
            ("routes_maintenance.py", "def start_maintenance_runner", "@app.get", 'workflow="maintenance-runner"'),
            ("routes_acquisition.py", '"type": "acquisition-download-all"', "@app.get", '"acquisition-download-all", log=log'),
            ("routes_acquisition.py", 'label="Music format replacement retry"', "return jsonify", "music-format-replace"),
            ("backend/acquisition_service.py", 'workflow = "download-import-"', "return", "job_contract.guarded(_do"),
            ("routes_import.py", "def _start_ai_batch_job", "worker_spawned = False", 'job_contract.held("ai-batch-"'),
            ("backend/import_service.py", '[reimport] Import slot acquired', "job = jobs.start_python",
             'job_contract.held("import-slot"'),
            ("backend/import_service.py", '[import] Import slot acquired', "if auto_import_idempotency_key",
             'job_contract.held("import-slot"'),
            ("backend/playlist_service.py", "def _playlist_start_direct_action", "label = {",
             'job_contract.enter("playlist-"'),
        )
        for rel, start, end, marker in sites:
            with self.subTest(site=f"{rel}: {marker}"):
                self.assertIn(marker, _between(rel, start, end))

    def test_playlist_download_takes_the_durable_lock_after_its_in_process_lock(self):
        body = _between("backend/playlist_service.py",
                        "def _run(job_log: Optional[List[str]] = None, cancel_event=None, update_state=None):",
                        "    job = jobs.start_python(")
        refuse = body.index("raise RuntimeError(_PLAYLIST_DUPLICATE_JOB_MESSAGE)")
        enter = body.index('job_contract.enter(')
        self.assertLess(body.index("runtime_lock.acquire(blocking=False)"), refuse)
        self.assertLess(refuse, enter)
        self.assertIn("contract.close()", body[enter:])
        # A failed entry gives the in-process lock back.
        self.assertIn("runtime_lock.release()", body[enter:body.index("if job_log is not None")])

    def test_download_all_takes_the_durable_lock_after_its_in_process_lock(self):
        body = _between("routes_acquisition.py", '"type": "acquisition-download-all"', "    job = jobs.start_python(")
        self.assertLess(body.index("_acq_download_all_lock.acquire(blocking=False)"), body.index("job_contract.enter("))
        self.assertIn("contract.close()", body)

    def test_nested_imports_inside_a_parent_slot_do_not_take_the_slot_again(self):
        body = _between("backend/import_service.py", "    def _do_locked(log, cancel_event=None):\n        if skip_import_lock:",
                        "    job = jobs.start_python(")
        skip_path = body[:body.index('log.append("[reimport] Queued')]
        self.assertNotIn("job_contract", skip_path)

    def test_two_playlists_do_not_share_a_lock_and_names_are_lock_safe(self):
        a, b = jc.slug("Road Trip / 2024"), jc.slug("Road Trip / 2025")
        self.assertNotEqual(a, b)
        self.assertEqual(jc.slug("  road trip / 2024 "), a)
        validate_key(jc.workflow_key("playlist-" + a))


if __name__ == "__main__":
    unittest.main()
