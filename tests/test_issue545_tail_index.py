"""#545: tail index robustness and scope (follow-ups from #541); #557 follow-ups from #551."""
import json
import os
import threading
import time
import types
import unittest
from unittest import mock

import backend.transaction_service as ts
import job_engine
from job_engine import JobStore
from tests.test_m2a_qa_verify import _Sync


def _no_jobs():
    return types.SimpleNamespace(all=lambda: [], get=lambda _jid: None, tail_transaction_jobs={})


class TailIndexTests(_Sync):
    def setUp(self):
        super().setUp()
        self.jobs_dir = self.data / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.mtime = 1000

    def wrapper(self):
        return self.store.create(operation_type="Playlist Import", status="Running",
                                 summary="Playlist sync (automatic)", reason=ts._JOB_WRAPPER_REASON,
                                 source="playlist-sync",
                                 metadata={"type": "playlist-sync", ts._JOB_WRAPPER_MARK: True})

    def record(self, job_id, status="success", metadata=None, **extra):
        """Written oldest first: each later record is newer on disk."""
        now = time.time()
        rec = {"job_id": job_id, "label": job_id, "status": status, "created_at": now - 5,
               "finished_at": None if status == "running" else now - 4, "returncode": 0,
               "metadata": {} if metadata is None else metadata, "log_tail": []}
        rec.update(extra)
        path = self.jobs_dir / f"{job_id}.json"
        path.write_text(json.dumps(rec), encoding="utf-8")
        self.mtime += 10
        os.utime(path, (self.mtime, self.mtime))

    def start(self, tail=None):
        """A store whose newest record is the only one loaded at startup."""
        self.record("newest")
        patches = [mock.patch.object(job_engine, "JOB_STARTUP_LOAD_LIMIT", 1),
                   mock.patch.object(ts, "_RECONCILE_RETRY_SECONDS", 0.05)]
        if tail is not None:
            patches.append(mock.patch.object(JobStore, "_recover_tail", tail))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        jobs = self.jobstore()
        self.install(jobs)
        return jobs

    def settle(self, tx_id, timeout=5):
        deadline = time.time() + timeout
        while self.store.get(tx_id)["status"] == "Running" and time.time() < deadline:
            time.sleep(0.05)
        return self.store.get(tx_id)["status"]

    # -- item 1 -------------------------------------------------------------
    def test_malformed_tail_record_does_not_end_the_sweep(self):
        tx = self.wrapper()
        self.record("oldjob", metadata={"type": "playlist-sync", "transaction_id": tx["id"]})
        self.record("badmeta", metadata=["not", "a", "dict"])  # read before oldjob (newer)
        self.record("badtime", finished_at="yesterday")
        jobs = self.start()
        jobs._recover_thread.join(5)
        self.assertEqual(self.settle(tx["id"]), "Completed")

    def test_dead_tail_thread_does_not_trigger_orphan_judging(self):
        tx = self.wrapper()
        self.record("oldjob", metadata={"type": "playlist-sync", "transaction_id": tx["id"]})

        def crashing_tail(self_, paths, now_):
            raise RuntimeError("tail sweep crashed")

        with mock.patch.object(threading, "excepthook", lambda _args: None):
            jobs = self.start(tail=crashing_tail)
            jobs._recover_thread.join(5)
        time.sleep(0.4)  # several reconcile ticks after the thread died
        self.assertEqual(self.store.get(tx["id"])["status"], "Running")

    # -- item 2 -------------------------------------------------------------
    def test_tail_snapshot_does_not_settle_an_engine_transaction(self):
        tx = self.store.create(operation_type="Metadata Update", status="Running",
                               metadata={"mutation_family": "metadata", "mutation_started": True})
        self.record("oldjob", metadata={"type": "metadata-batch", "transaction_id": tx["id"]})
        jobs = self.start()
        jobs._recover_thread.join(5)
        time.sleep(0.4)
        self.assertEqual(self.store.get(tx["id"])["status"], "Running")

    def test_tail_straggler_still_settles_an_engine_transaction(self):
        # The live case: an interrupted job in the tail is loaded into memory
        # and speaks for its transaction through _jobs_by_transaction.
        tx = self.store.create(operation_type="Metadata Update", status="Running",
                               metadata={"mutation_family": "metadata", "mutation_started": True})
        self.record("straggler", status="running",
                    metadata={"type": "metadata-batch", "transaction_id": tx["id"]})
        jobs = self.start()
        jobs._recover_thread.join(5)
        self.assertEqual(self.settle(tx["id"]), "Recovery Required")

    # -- item 3 -------------------------------------------------------------
    def test_paging_does_not_skip_a_running_transaction(self):
        # QA F1/F3: another thread settles rows between page reads, in two
        # consecutive passes of a re-read loop. Without one snapshot, S (never
        # written, oldest) is skipped both times and the second pass adds no
        # new id, so it was lost.
        s, k, b, a = (self.wrapper()["id"] for _ in range(4))
        for n, tx_id in enumerate((s, k, b, a)):  # list() orders by mtime, newest first
            os.utime(self.store._path(tx_id), (1000 + n, 1000 + n))
        # After list() call n, the writer settles these rows (page size 1):
        # a re-read loop's pass 1 reads A then K (B is gone), pass 2 reads K,
        # and each removal shifts S back past the next offset.
        script = {1: [a], 2: [b], 4: [k]}
        todo, done = [], threading.Event()
        wake = threading.Condition()

        def writer():
            while True:
                with wake:
                    wake.wait_for(lambda: todo)
                    ids = todo.pop(0)
                if ids is None:
                    return
                for tx_id in ids:
                    self.store.transition(tx_id, "Running", "Completed")
                done.set()

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        real_list, calls = self.store.list, []

        def churning_list(**kwargs):
            result = real_list(**kwargs)
            calls.append(1)
            if len(calls) in script:
                done.clear()
                with wake:
                    todo.append(script[len(calls)])
                    wake.notify()
                done.wait(0.3)  # a snapshot holds the store lock, so the writer waits it out
            return result

        with mock.patch.object(ts, "_RUNNING_PAGE", 1, create=True), \
                mock.patch.object(self.store, "list", churning_list):
            ids = {str(r["id"]) for r in ts._running_rows(self.store)}
        with wake:
            todo.append(None)
            wake.notify()
        thread.join(5)
        self.assertGreater(len(calls), 1)  # more than one page
        self.assertIn(s, ids)
        self.assertEqual(self.store.get(s)["status"], "Running")  # never written by the writer

    # -- item 4 -------------------------------------------------------------
    def test_orphan_judging_rechecks_updated_at_under_the_lock(self):
        tx = self.wrapper()
        row = self.store.get(tx["id"])
        before = time.time() + 0.001
        time.sleep(0.01)
        self.store.append_log(tx["id"], "the job wrote after the list")  # live since `before`
        ts._orphaned_job_wrapper(self.store, row, before)
        self.assertEqual(self.store.get(tx["id"])["status"], "Running")
        ts._orphaned_job_wrapper(self.store, row, time.time() + 1)  # control: no later write
        self.assertNotEqual(self.store.get(tx["id"])["status"], "Running")

    # -- startup log --------------------------------------------------------
    def test_unbound_log_is_debug_until_the_judging_pass(self):
        self.store.create(operation_type="Metadata Update", status="Running",
                          metadata={"mutation_family": "metadata"})
        with mock.patch.object(ts._app_logger, "warning") as warn, \
                mock.patch.object(ts._app_logger, "debug") as debug:
            ts._reconcile_running_transactions(_no_jobs(), self.store, None)
            warn.assert_not_called()
            self.assertIn("pending the startup job scan", debug.call_args[0][0])
            ts._reconcile_running_transactions(_no_jobs(), self.store, time.time())
            self.assertIn("left unchanged", warn.call_args[0][0])

    def test_retry_pass_after_judging_still_warns(self):
        # QA F2: once orphans are judged, a pass re-armed by a failed settle
        # runs without a cutoff; the scan has still finished, so an unbound
        # row is a warning, not "pending the startup job scan".
        self.store.create(operation_type="Metadata Update", status="Running",
                          metadata={"mutation_family": "metadata"})
        scanned = threading.Event()
        scanned.set()
        ticks = []
        fake = types.SimpleNamespace(start_python=lambda *a, **k: None, all=lambda: [], get=lambda _jid: None,
                                     tail_transaction_jobs={}, tail_scan_complete=scanned,
                                     every=lambda _name, _secs, fn: ticks.append(fn))
        self.install(fake)
        tick = ticks[0]
        due = tick.__closure__[tick.__code__.co_freevars.index("reconcile_due")].cell_contents
        tick()  # the judging pass
        due.set()  # as settle_transaction does when update_from_job fails
        with mock.patch.object(ts._app_logger, "warning") as warn, \
                mock.patch.object(ts._app_logger, "debug") as debug:
            tick()
        self.assertTrue(any("left unchanged" in c[0][0] for c in warn.call_args_list))
        self.assertFalse([c for c in debug.call_args_list if "pending" in c[0][0]])

    # -- #557 -----------------------------------------------------------------
    def _newer(self, older, newer):
        """List order is newest first: ``newer`` is reconciled before ``older``."""
        os.utime(self.store._path(older), (1000, 1000))
        os.utime(self.store._path(newer), (2000, 2000))

    # -- item 1 -------------------------------------------------------------
    def _corrupt_snapshot_is_confined_to_its_row(self, *corrupt_records):
        good, bad = self.wrapper(), self.wrapper()
        self._newer(good["id"], bad["id"])  # the corrupt row comes first
        self.record("goodjob", metadata={"type": "playlist-sync", "transaction_id": good["id"]})
        for name, extra in corrupt_records:
            self.record(name, metadata={"type": "playlist-sync", "transaction_id": bad["id"]})
            path = self.jobs_dir / f"{name}.json"
            stat = path.stat()
            path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), **extra}), encoding="utf-8")
            os.utime(path, (stat.st_mtime, stat.st_mtime))
        jobs = self.start()
        jobs._recover_thread.join(5)
        self.assertEqual(self.settle(good["id"]), "Completed")
        time.sleep(0.3)  # several more ticks, including the judging pass
        self.assertEqual(self.store.get(bad["id"])["status"], "Running")  # never settled on a corrupt record

    def test_unhashable_job_id_in_a_tail_snapshot(self):
        self._corrupt_snapshot_is_confined_to_its_row(("badid", {"job_id": ["not", "hashable"]}))

    def test_mixed_created_at_types_in_tail_snapshots(self):
        self._corrupt_snapshot_is_confined_to_its_row(("textjob", {"created_at": "yesterday"}),
                                                      ("numjob", {}))

    # -- item 2 -------------------------------------------------------------
    def test_orphan_recheck_and_transition_are_atomic_under_the_lock(self):
        # A competing write started right after the re-read must not land
        # before the transition: the store lock is held across both.
        tx = self.wrapper()
        row = self.store.get(tx["id"])
        wrote, seen = threading.Event(), {}
        real_get, real_transition = self.store.get, self.store.transition

        def writer():
            self.store.append_log(tx["id"], "competing write")
            wrote.set()

        def get(tx_id, **kwargs):
            result = real_get(tx_id, **kwargs)
            if "started" not in seen:
                seen["started"] = True
                threading.Thread(target=writer, daemon=True).start()
                wrote.wait(0.3)  # lands now unless this thread holds the lock
            return result

        def transition(*args, **kwargs):
            seen.setdefault("write_landed_first", wrote.is_set())
            return real_transition(*args, **kwargs)

        with mock.patch.object(self.store, "get", get), mock.patch.object(self.store, "transition", transition):
            ts._orphaned_job_wrapper(self.store, row, time.time() + 1)
        self.assertTrue(wrote.wait(5))
        self.assertIs(seen.get("write_landed_first"), False)

    # -- item 3 -------------------------------------------------------------
    def _assert_aborted_sweep_is_terminal(self, jobs):
        self.assertFalse(jobs.tail_scan_complete.is_set())
        self.assertTrue(jobs.tail_scan_aborted.is_set())

    def test_dead_sweep_is_aborted_and_the_reconcile_stops_waiting(self):
        tx = self.wrapper()  # no job anywhere: an orphan only a complete sweep could prove
        self.record("unrelated")  # a tail to sweep

        def crashing_tail(self_, paths, now_):
            raise RuntimeError("tail sweep crashed")

        with mock.patch.object(threading, "excepthook", lambda _args: None), \
                mock.patch.object(ts._app_logger, "warning") as warn:
            jobs = self.start(tail=crashing_tail)
            jobs._recover_thread.join(5)
            self._assert_aborted_sweep_is_terminal(jobs)
            calls = []
            real_rows = ts._running_rows
            with mock.patch.object(ts, "_running_rows", lambda s: calls.append(1) or real_rows(s)):
                time.sleep(0.5)  # ~10 ticks
        self.assertEqual(self.store.get(tx["id"])["status"], "Running")  # never judged orphaned
        self.assertLessEqual(len(calls), 1)  # one pass after the abort, then the reconcile settles
        self.assertTrue(any("left unchanged" in c[0][0] for c in warn.call_args_list))

    def test_sweep_stopped_by_close_is_aborted(self):
        tx = self.wrapper()
        self.record("oldjob", metadata={"type": "playlist-sync", "transaction_id": tx["id"]})
        gate = threading.Event()
        real_tail = JobStore._recover_tail

        def gated_tail(self_, paths, now_):
            gate.wait(5)
            real_tail(self_, paths, now_)

        jobs = self.start(tail=gated_tail)
        jobs._closed.set()  # what close() does first
        gate.set()
        jobs._recover_thread.join(5)
        self._assert_aborted_sweep_is_terminal(jobs)
        time.sleep(0.3)
        self.assertEqual(self.store.get(tx["id"])["status"], "Running")

    def test_finished_sweep_is_not_aborted(self):
        self.record("unrelated")
        jobs = self.start()
        jobs._recover_thread.join(5)
        self.assertTrue(jobs.tail_scan_complete.is_set())
        self.assertFalse(jobs.tail_scan_aborted.is_set())



if __name__ == "__main__":
    unittest.main()
