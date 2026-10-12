"""#234: no test may leave job_engine threads running after it ends.

Importing this module installs the guard for every TestCase in the run:
discovery imports all test modules before running any test, so the full
suite is covered (see tests/_job_threads.py). The tests below prove the
guard itself catches a leak and passes once the store is closed.
"""

import threading
import unittest

from job_engine import JobStore

try:
    from _job_threads import assert_no_new_job_threads, install_guard, job_threads
except ImportError:  # pragma: no cover
    from tests._job_threads import assert_no_new_job_threads, install_guard, job_threads

install_guard()


class JobThreadGuardTests(unittest.TestCase):
    def test_a_running_job_is_reported_and_a_closed_store_is_not(self):
        before = set(job_threads())
        go = threading.Event()
        store = JobStore()
        self.addCleanup(store.close)
        self.addCleanup(go.set)
        store.start_python(lambda log: go.wait(5), label="leaky")
        with self.assertRaisesRegex(AssertionError, r"PythonJob\._run"):
            assert_no_new_job_threads(before, grace=0.05)
        go.set()
        self.assertTrue(store.close())
        assert_no_new_job_threads(before, grace=0)

    def test_an_unclosed_durable_store_heartbeat_is_reported(self):
        import tempfile
        before = set(job_threads())
        with tempfile.TemporaryDirectory() as d:
            store = JobStore(d)
            self.addCleanup(store.close)
            with self.assertRaisesRegex(AssertionError, r"_heartbeat_loop"):
                assert_no_new_job_threads(before, grace=0.05)
            store.close()
        assert_no_new_job_threads(before, grace=0)


if __name__ == "__main__":
    unittest.main()
