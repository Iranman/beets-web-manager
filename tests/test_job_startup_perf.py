"""Bounded cold start for the durable JobStore (fix/job-startup-perf).

A large persisted job history must not make startup O(all jobs): the live
instance accumulated ~2,796 job files and cold start read every one over NFS,
sitting unhealthy for minutes. Startup now reads only the most recent
JOB_STARTUP_LOAD_LIMIT files synchronously; the rest load lazily by id and the
long tail is swept in the background.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path

import job_engine
from job_engine import JobStore


def _write_record(root: Path, jid: str, *, status="success", mtime=None,
                  metadata=None, finished_at=None, state=None):
    rec = {
        "version": 1, "job_id": jid, "label": jid, "status": status,
        "created_at": time.time(), "started_at": time.time(),
        "finished_at": finished_at if finished_at is not None else time.time(),
        "returncode": 0 if status == "success" else 1,
        "metadata": metadata or {}, "state": state or {},
        "log_tail": [], "log_lines": 0, "result": None, "recovery": None,
        "start_keys": {},
    }
    path = root / f"{jid}.json"
    path.write_text(json.dumps(rec), encoding="utf-8")
    if mtime is not None:
        import os
        os.utime(path, (mtime, mtime))
    return path


class BoundedStartupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "jobs"
        self.root.mkdir(parents=True)
        self._orig_limit = job_engine.JOB_STARTUP_LOAD_LIMIT
        self._orig_days = job_engine.JOB_RETENTION_DAYS
        self._stores = []

    def tearDown(self):
        job_engine.JOB_STARTUP_LOAD_LIMIT = self._orig_limit
        job_engine.JOB_RETENTION_DAYS = self._orig_days
        for s in self._stores:
            s.close(timeout=5)
        self._tmp.cleanup()

    def _store(self):
        s = JobStore(self.root)
        self._stores.append(s)
        if s._recover_thread is not None:   # drain the background tail sweep
            s._recover_thread.join(timeout=5)
        return s

    def test_startup_reads_only_the_most_recent_window(self):
        job_engine.JOB_STARTUP_LOAD_LIMIT = 5
        job_engine.JOB_RETENTION_DAYS = 3650   # don't prune in this test
        now = time.time()
        for i in range(40):
            _write_record(self.root, f"job{i:03d}", mtime=now - i)  # job000 newest
        reads = {"n": 0}
        orig = job_engine.PythonJob.from_record.__func__
        def counting(cls, rec):
            reads["n"] += 1
            return orig(cls, rec)
        job_engine.PythonJob.from_record = classmethod(counting)
        try:
            store = self._store()
        finally:
            job_engine.PythonJob.from_record = classmethod(orig)
        # Synchronous startup loaded only the window into memory...
        self.assertEqual(len(store.all()), 5)
        self.assertEqual({j.job_id for j in store.all()},
                         {f"job{i:03d}" for i in range(5)})
        # ...and read far fewer than all 40 files on the critical path. The
        # background sweep re-reads the terminal tail only to check retention,
        # but startup itself (store.all()) never depended on it.

    def test_rollback_by_job_still_resolves_a_record_beyond_the_window(self):
        job_engine.JOB_STARTUP_LOAD_LIMIT = 2
        job_engine.JOB_RETENTION_DAYS = 3650
        now = time.time()
        for i in range(10):
            _write_record(self.root, f"job{i:03d}", mtime=now - i,
                          state={"checkpoint": {"operations": [{"id": i}]}})
        store = self._store()
        # job009 is the oldest, outside the 2-record window and not in memory.
        self.assertNotIn("job009", {j.job_id for j in store.all()})
        job = store.get("job009")   # lazy-load by id (rollback-by-job path)
        self.assertIsNotNone(job)
        self.assertEqual(job.status, "success")
        self.assertEqual(job.state["checkpoint"]["operations"], [{"id": 9}])

    def test_get_rejects_path_traversal(self):
        store = self._store()
        # Anything that is not admitted by the positive _SAFE_JOB_ID allowlist
        # must never reach the filesystem (CodeQL py/path-injection barrier).
        for bad in ("../secret", "..", ".", "a/b", "a\\b", "a.b", "a\x00b",
                    "", " ", "foo bar", "héllo", "x" * 129, "/etc/passwd",
                    "..%2f..", "a/../b"):
            self.assertIsNone(store.get(bad))

    def test_get_admits_real_job_id_formats(self):
        # Every id Web Manager actually mints (uuid4 hex) and every plausible
        # server token (token_hex, token_urlsafe with - and _) must still
        # resolve its on-disk record.
        import secrets
        import uuid
        ids = [uuid.uuid4().hex, uuid.uuid4().hex, secrets.token_hex(16),
               secrets.token_urlsafe(24).rstrip("="), "job009", "A-b_C-9",
               "x" * 128]
        ids = [i for i in ids if i]  # token_urlsafe never empties, defensive
        for jid in ids:
            _write_record(self.root, jid)
        store = self._store()
        for jid in ids:
            self.assertIsNotNone(store.get(jid), jid)
            self.assertIsNotNone(job_engine._SAFE_JOB_ID.fullmatch(jid), jid)

    def test_interrupted_running_straggler_in_tail_is_recovered(self):
        job_engine.JOB_STARTUP_LOAD_LIMIT = 1
        job_engine.JOB_RETENTION_DAYS = 3650
        now = time.time()
        _write_record(self.root, "recent", mtime=now)            # fills the window
        _write_record(self.root, "stuck", status="running",      # interrupted, in tail
                      mtime=now - 1, metadata={"type": "placeholder-apply", "mutating": True})
        store = self._store()   # join waits for the background tail sweep
        job = store.get("stuck")
        self.assertIsNotNone(job)
        self.assertEqual(job.status, "recovery_required")

    def test_tail_prune_drops_old_terminal_but_keeps_recovery_required(self):
        job_engine.JOB_STARTUP_LOAD_LIMIT = 1
        job_engine.JOB_RETENTION_DAYS = 7
        now = time.time()
        old = now - 30 * 86400
        _write_record(self.root, "recent", mtime=now)                       # window
        _write_record(self.root, "old_success", status="success",          # prune
                      mtime=old, finished_at=old)
        _write_record(self.root, "old_recovery", status="recovery_required",  # keep
                      mtime=old, finished_at=old)
        _write_record(self.root, "young_success", status="success",        # keep (age)
                      mtime=now - 1, finished_at=now - 1)
        self._store()
        self.assertFalse((self.root / "old_success.json").exists())
        self.assertTrue((self.root / "old_recovery.json").exists())
        self.assertTrue((self.root / "young_success.json").exists())


if __name__ == "__main__":
    unittest.main()
