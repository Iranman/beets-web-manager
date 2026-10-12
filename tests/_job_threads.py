"""Keep job_engine threads from outliving the test that started them (#234).

A PythonJob thread, or a JobStore heartbeat/schedule thread, that outlives
its test keeps running while later tests patch globals and tear down temp
dirs and sqlite connections; that is the suspected cause of the CI exit-139
segfaults. Tests that start jobs register one of these as a cleanup:

* a store the test owns: ``self.addCleanup(store.close)`` (job_engine API);
* the shared ``backend.app_runtime.jobs`` store:
  ``self.addCleanup(join_jobs, jobs)``, which cancels the jobs still running
  and joins every job thread without stopping that store's heartbeat.

Register it *before* ``addCleanup(gate.set)``: cleanups run last-in first-out,
so the gate opens first and the join then waits for the released job. When
patches registered later must still be active while the job finishes, join in
``tearDown`` instead (it runs before any cleanup).

``install_guard()`` (installed by test_job_thread_guard.py for the whole
discovered run) fails a test whose job threads are still alive after its
cleanups and a grace period.
"""

import sys
import threading
import time
import unittest

_JOB_THREAD_TARGETS = ("PythonJob._run", "JobStore._heartbeat_loop", "JobStore.every.<locals>.loop")
GRACE_SECONDS = 10.0


def _target_name(thread):
    # Thread.run() deletes _target when it returns, so read it defensively.
    return getattr(getattr(thread, "_target", None), "__qualname__", "")


def job_threads():
    """Live threads running job_engine code, except the long-lived heartbeat
    and schedules of the app-wide store (they live as long as the process)."""
    runtime = sys.modules.get("backend.app_runtime")
    store = getattr(runtime, "jobs", None)
    exempt = {getattr(store, "_heartbeat", None), *getattr(store, "_schedules", {}).values()}
    return [t for t in threading.enumerate()
            if t not in exempt
            and _target_name(t) in _JOB_THREAD_TARGETS]


def join_jobs(store, timeout=10.0):
    """Cancel ``store``'s running jobs and join every job thread it started."""
    for job in store.all():
        if job.status == "running":
            job.kill()
    deadline = time.monotonic() + timeout
    for thread in list(store._threads):
        if thread.is_alive():  # a test that mocks Thread.start leaves one unstarted
            thread.join(max(0.0, deadline - time.monotonic()))


def assert_no_new_job_threads(before, grace=GRACE_SECONDS):
    """Wait up to ``grace`` seconds for job threads not in ``before`` to end;
    raise AssertionError naming those still alive."""
    deadline = time.monotonic() + grace
    leaked = {t: f"{t.name} ({_target_name(t)})" for t in job_threads() if t not in before}
    for thread in leaked:
        thread.join(max(0.0, deadline - time.monotonic()))
    alive = [name for t, name in leaked.items() if t.is_alive()]
    if alive:
        raise AssertionError(
            f"job threads still alive {grace:g}s after the test ended: {alive}. "
            "Close the JobStore or join its jobs in a cleanup (tests/_job_threads.py).")


def install_guard():
    """Check every TestCase for leaked job threads after its cleanups."""
    original = unittest.TestCase._callSetUp
    if getattr(original, "_job_thread_guard", False):
        return

    def _callSetUp(self):
        # Registered before setUp's own cleanups, so it runs after all of them.
        self.addCleanup(assert_no_new_job_threads, set(job_threads()))
        original(self)

    _callSetUp._job_thread_guard = True
    unittest.TestCase._callSetUp = _callSetUp
