"""Close every JobStore a test creates before its temp directory is removed.

A job's final persisted write (and any transaction update its body makes)
runs on the job's own thread and can still be in flight when the test
returns; removing the temp directory under it fails with "Directory not
empty" (#286). Call ``close_job_stores_at_cleanup(self)`` in ``setUp`` right
after registering the temp directory's cleanup: cleanups run last-in first-out,
so every store is closed (heartbeat stopped, job threads joined) first.
"""

from unittest import mock

from job_engine import JobStore


def close_job_stores_at_cleanup(test) -> None:
    stores = []
    real_init = JobStore.__init__

    def init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        stores.append(self)

    def close_all():
        for store in stores:
            test.assertTrue(store.close(), "a job thread outlived the test")

    test.addCleanup(close_all)
    patcher = mock.patch.object(JobStore, "__init__", init)
    patcher.start()
    test.addCleanup(patcher.stop)
