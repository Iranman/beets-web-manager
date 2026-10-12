"""0.4.0 QA non-blocking findings in the transaction layer.

NB-1: a restart that leaves Clean All recovery_required also releases that
job's durable workflow lock, so a resume starts at once (not a heartbeat ttl
later); a lock whose holder is alive still blocks, and a start refused for a
busy lock records no job and no transaction.

NB-3: every transaction records who started it -- the request's verified
user, the user whose request started the job that creates it, or "system"
when no operator did.
"""

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import backend.job_contract as jc
from backend import transaction_engine as te
from backend.resource_locks import ResourceLocks, set_locks
from job_engine import JobStore
from tests._job_store_cleanup import close_job_stores_at_cleanup
from tests.test_job_contract import _HOLDER, REPO, _wait
from tests.test_staging_mutation_hardening import _RouteEnv

KEY = "workflow:maintenance-runner"


def _dead_pid():
    """A pid whose process has exited (the old container's Web Manager)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _clean_all_guard(fn):
    # The guard Clean All starts under (routes_maintenance.start_maintenance_runner).
    return jc.guarded(fn, workflow="maintenance-runner", fail_fast_in_process=True, wait_seconds=0)


class _Locks(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        close_job_stores_at_cleanup(self)
        self.root = Path(self._tmp.name)
        self.locks = ResourceLocks(self.root / "locks")
        set_locks(self.locks)
        self.addCleanup(set_locks, None)

    def hold_in_other_process(self):
        proc = subprocess.Popen([sys.executable, "-c", _HOLDER, str(self.root / "locks"), KEY],
                                cwd=str(REPO), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(lambda: proc.stdin.closed or proc.stdin.close())
        self.assertEqual(proc.stdout.readline().strip(), "ready")
        return proc


class RestartReleasesInterruptedLockTests(_Locks):
    def test_restart_then_immediate_resume_runs(self):
        jobs = JobStore(self.root / "jobs")
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def first_run(log, cancel_event=None, update_state=None):
            started.set()
            release.wait(10)

        job = jobs.start_python(_clean_all_guard(first_run), label="Clean All",
                                metadata=jc.contract_metadata("maintenance-runner", {"type": "maintenance-runner"}))
        self.assertTrue(started.wait(5))
        # The old container dies mid-run: its lock heartbeat is fresh (well
        # inside the ttl), but the process that holds it is gone.
        path = self.root / "locks" / "workflow__maintenance-runner.lock"
        rec = json.loads(path.read_text(encoding="utf-8"))
        rec.update(pid=_dead_pid(), pid_start=None, heartbeat_at=time.time())
        path.write_text(json.dumps(rec), encoding="utf-8")

        restarted = JobStore(self.root / "jobs")  # the new process loads the job records
        recovered = restarted.get(job.job_id)
        self.assertEqual(recovered.status, "recovery_required")
        self.assertEqual(recovered.recovery["released_locks"], [KEY])
        self.assertEqual(self.locks.held(), [])
        self.assertIsNone(jc.live_holder("maintenance-runner"))

        resumed = restarted.start_python(_clean_all_guard(lambda log: "resumed"), label="Clean All",
                                         metadata=jc.contract_metadata("maintenance-runner"))
        _wait(resumed)
        self.assertEqual((resumed.status, resumed.result), ("success", "resumed"))

    def test_a_live_holder_keeps_its_lock_and_still_blocks(self):
        self.hold_in_other_process()
        holder = jc.live_holder("maintenance-runner")
        self.assertEqual((holder or {}).get("owner"), "other-process")
        # Even a recovery naming that owner never takes a live process's lock.
        self.assertEqual(jc.release_interrupted({"owner": "other-process"}), [])
        self.assertEqual([r["owner"] for r in self.locks.held()], ["other-process"])
        ran = []
        job = JobStore(self.root / "jobs").start_python(_clean_all_guard(lambda log: ran.append(1)), label="x")
        _wait(job)
        self.assertEqual((job.status, ran), ("failed", []))  # D1: never two at once

    def test_recovery_releases_only_the_interrupted_jobs_owner(self):
        self.locks.acquire(["workflow:other"], "job:other:live")
        stale = {"key": KEY, "owner": "job:maintenance-runner:dead", "host": "old-container", "pid": 7,
                 "pid_start": "1", "acquired_at": time.time(), "heartbeat_at": time.time(), "ttl": 120.0}
        (self.root / "locks" / "workflow__maintenance-runner.lock").write_text(json.dumps(stale), encoding="utf-8")
        self.assertEqual(jc.release_interrupted({"owner": "job:maintenance-runner:dead"}), [KEY])
        self.assertEqual([r["key"] for r in self.locks.held()], ["workflow:other"])
        self.assertEqual(jc.release_interrupted({}), [])


class CleanAllBusyStartTests(_RouteEnv):
    """A Clean All start refused for a busy workflow lock records nothing."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        set_locks(ResourceLocks(self.root / "locks"))
        self.addCleanup(set_locks, None)
        import backend.transaction_service as ts
        self.job_tx = te.TransactionStore(str(self.root / "job-transactions"))  # the job wrapper's store
        p = mock.patch.object(ts, "transactions", self.job_tx)
        p.start()
        self.addCleanup(p.stop)

    def test_busy_lock_returns_409_with_no_job_and_no_transaction(self):
        from backend.app_runtime import jobs
        _Locks.hold_in_other_process(self)
        jobs_before = {j.job_id for j in jobs.all()}
        resp = self.client.post("/api/jobs/maintenance-runner", json={})
        self.assertEqual(resp.status_code, 409, resp.get_json())
        self.assertEqual(resp.get_json()["code"], "workflow_busy")
        self.assertEqual({j.job_id for j in jobs.all()}, jobs_before)
        self.assertEqual((self.job_tx.list(limit=10)[1], self.store.list(limit=10)[1]), (0, 0))


class TransactionActorTests(_RouteEnv):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tx = te.TransactionStore(self._tmp.name)

    def _as_alice(self):
        import app as app_module
        from flask import session
        ctx = app_module.app.test_request_context("/")
        ctx.push()
        self.addCleanup(ctx.pop)
        session["authenticated"], session["user"] = True, "alice"

    def test_unattended_creation_is_system(self):
        self.assertEqual(self.tx.create(operation_type="Delete")["initiating_user"], "system")
        target = Path(self._tmp.name) / "staged"
        target.mkdir()
        _op, plan = self.tx.create_import_review_cleanup_plan(target_path=str(target), allowed_roots=[self._tmp.name])
        self.assertEqual(self.tx.get(_op)["initiating_user"], "system")

    def test_request_creation_records_the_verified_user(self):
        self._as_alice()
        self.assertEqual(self.tx.create(operation_type="Delete")["initiating_user"], "alice")
        self.assertEqual(self.tx.create(operation_type="Delete", initiating_user="bob")["initiating_user"], "bob")

    def test_composite_workflow_in_a_request_records_the_user(self):
        """delete_album_art used to record the transaction_engine default."""
        import backend.composite_workflows as cw
        ad = mock.Mock()
        ad.get_album.return_value = {"id": 5, "artpath": "/music/A/B/cover.jpg", "album": "B"}
        self._as_alice()
        with mock.patch("backend.resource_locks.approve_preview", return_value=None):
            cw.delete_album_art(5, adapter=ad, store=self.tx)  # stops after recording the plan
        self.assertEqual([t["initiating_user"] for t in self.tx.list(limit=5)[0]], ["alice"])

    def test_a_job_records_the_user_whose_request_started_it(self):
        from backend.app_runtime import jobs
        made = []

        def work(log):
            made.append(self.tx.create(operation_type="Delete")["initiating_user"])

        self._as_alice()
        job = jobs.start_python(work, label="nb3 attribution probe", metadata={"type": "nb3-probe"})
        _wait(job)
        self.assertEqual(made, ["alice"])

    def test_a_job_no_request_started_records_system(self):
        from backend.app_runtime import jobs
        made = []
        result = {}

        def start():  # a scheduler thread: no request context
            result["job"] = jobs.start_python(
                lambda log: made.append(self.tx.create(operation_type="Delete")["initiating_user"]),
                label="nb3 unattended probe", metadata={"type": "nb3-probe-system"})
        t = threading.Thread(target=start)
        t.start()
        t.join(5)
        _wait(result["job"])
        self.assertEqual(made, ["system"])


if __name__ == "__main__":
    unittest.main()
