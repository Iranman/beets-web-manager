"""#672: any per-row read error is confined to its row, and the bounded retry
budget is spent only by passes made after the startup job scan finished."""
import json
import os
import threading
import types
import unittest

import backend.transaction_service as ts
from tests.test_m2a_qa_verify import _Sync


class ReconcileRowErrorTests(_Sync):
    def test_corrupt_row_and_good_row_share_the_tick_counter(self):
        real = self.jobstore()
        bad = self.store.create(operation_type="Playlist Import", status="Running",
                                reason=ts._JOB_WRAPPER_REASON, metadata={ts._JOB_WRAPPER_MARK: True})
        good = self.store.create(operation_type="Delete", status="Running")
        job = real.start_python(lambda log, cancel_event=None: {"ok": True}, label="x",
                                metadata={"transaction": False, "transaction_id": good["id"], "type": "t"})
        self.wait(job)
        self.store.update(good["id"], metadata={"job_id": job.job_id})
        # hand-corrupted: metadata is not a dict, last written long ago (an orphan candidate)
        path = self.store._path(bad["id"])
        path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")),
                                    "metadata": "corrupt", "updated_at": 0}), encoding="utf-8")
        os.utime(self.store._path(good["id"]), (1000, 1000))
        os.utime(path, (2000, 2000))  # newest first: the corrupt row is read before the good one

        scanned = threading.Event()
        ticks = []
        fake = types.SimpleNamespace(start_python=lambda *a, **k: None, all=lambda: [], get=real.get,
                                     tail_transaction_jobs={}, tail_scan_complete=scanned,
                                     every=lambda _name, _secs, fn: ticks.append(fn))
        self.install(fake)
        tick = ticks[0]
        due = tick.__closure__[tick.__code__.co_freevars.index("reconcile_due")].cell_contents

        for _ in range(ts._UNREADABLE_RETRIES + 2):  # a slow startup scan: these passes spend no budget
            tick()
            self.assertTrue(due.is_set())
        self.assertEqual(self.store.get(good["id"])["status"], "Completed")  # not blocked by the corrupt row

        scanned.set()
        for _ in range(ts._UNREADABLE_RETRIES):  # the judging pass and its retries still re-arm
            tick()
            self.assertTrue(due.is_set())
        tick()  # budget spent: the corrupt row no longer keeps the reconcile due
        self.assertFalse(due.is_set())
        self.assertEqual(self.store.get(bad["id"])["status"], "Running")  # never settled or judged orphaned

    def test_clean_read_drops_the_row_count(self):
        tx = self.store.create(operation_type="Delete", status="Running")
        jobs = types.SimpleNamespace(all=lambda: [], get=lambda _jid: None, tail_transaction_jobs={})
        unreadable = {tx["id"]: ts._UNREADABLE_RETRIES}
        ts._reconcile_running_transactions(jobs, self.store, unreadable=unreadable)
        self.assertNotIn(tx["id"], unreadable)


if __name__ == "__main__":
    unittest.main()
