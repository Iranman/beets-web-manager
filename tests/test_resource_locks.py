"""Durable hierarchical resource locks (backend/resource_locks.py)."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from backend.resource_locks import (
    HOST, LockOrderError, ResourceLockConflictError, ResourceLocks, validate_key,
)

ROOT = Path(__file__).resolve().parents[1]
KEYS = ("library-global", "album-merge:ef4b6576-ac7c-4f72-bee6-e7a6b6cf019d", "album:1935", "item:25264",
        "playlist:road-trip", "untracked-inventory")

_CHILD = r'''
import sys, time, json
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from backend.resource_locks import ResourceLocks, ResourceLockConflictError
locks = ResourceLocks(Path(sys.argv[2]))
start = float(sys.argv[4])
while time.time() < start:
    time.sleep(0.001)
try:
    locks.acquire([sys.argv[3]], sys.argv[5], timeout=0.3)
    print("ACQUIRED", flush=True)
    time.sleep(1.5)
    locks.release([sys.argv[3]], sys.argv[5])
except ResourceLockConflictError:
    print("CONFLICT", flush=True)
'''


class ResourceLockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "locks"
        self.locks = ResourceLocks(self.dir)

    def test_every_key_kind_is_persisted_and_released(self):
        for key in KEYS:
            self.locks.acquire([key], f"owner-{key}", timeout=1)
            files = list(self.dir.glob("*.lock"))
            rec = json.loads(files[0].read_text())
            self.assertEqual((rec["key"], rec["owner"], rec["host"], rec["pid"]), (key, f"owner-{key}", HOST, os.getpid()))
            self.locks.release([key], f"owner-{key}")
            self.assertEqual(list(self.dir.glob("*.lock")), [])

    def test_ownership_survives_a_new_registry_instance(self):
        self.locks.acquire(["album:1"], "tx-1")
        restarted = ResourceLocks(self.dir)  # a restarted process sees the same record
        with self.assertRaises(ResourceLockConflictError) as ctx:
            restarted.acquire(["album:1"], "tx-2", timeout=0.1)
        self.assertEqual(ctx.exception.holder["owner"], "tx-1")

    def test_library_global_conflicts_with_everything(self):
        self.locks.acquire(["item:5"], "a")
        with self.assertRaises(ResourceLockConflictError):
            self.locks.acquire(["library-global"], "b", timeout=0.1)
        self.locks.release(["item:5"], "a")
        self.locks.acquire(["library-global"], "b")
        for key in KEYS[1:]:
            with self.assertRaises(ResourceLockConflictError):
                self.locks.acquire([key], "c", timeout=0.05)

    def test_distinct_keys_do_not_conflict(self):
        self.locks.acquire(["album:1", "item:2"], "a")
        self.locks.acquire(["album:3", "item:4", "playlist:x", "untracked-inventory"], "b")
        self.assertEqual(len(self.locks.held()), 6)

    def test_hierarchy_order_is_enforced(self):
        self.locks.acquire(["item:5"], "a")
        with self.assertRaises(LockOrderError):
            self.locks.acquire(["album:1"], "a")  # coarser after finer
        self.locks.acquire(["item:6"], "a")        # same level, later key: fine

    def test_nested_acquisition_cannot_deadlock(self):
        """A holds album:1 and wants item:9; B holds item:9 and would want
        album:1 -- the order rule refuses B's second step instead of letting
        both wait forever."""
        self.locks.acquire(["album:1"], "A")
        self.locks.acquire(["item:9"], "B")
        with self.assertRaises(LockOrderError):
            self.locks.acquire(["album:1"], "B")
        with self.assertRaises(ResourceLockConflictError):
            self.locks.acquire(["item:9"], "A", timeout=0.05)

    def test_reentrant_for_the_same_owner(self):
        self.locks.acquire(["album:1"], "a")
        self.locks.acquire(["album:1"], "a")
        self.locks.release(["album:1"], "a")
        self.assertEqual(len(self.locks.held()), 1)
        self.locks.release(["album:1"], "a")
        self.assertEqual(self.locks.held(), [])

    def _write_foreign(self, key, *, pid, heartbeat_age, host=HOST, ttl=1.0):
        self.dir.mkdir(parents=True, exist_ok=True)
        rec = {"key": key, "owner": "ghost", "host": host, "pid": pid, "pid_start": None,
               "acquired_at": time.time() - heartbeat_age, "heartbeat_at": time.time() - heartbeat_age, "ttl": ttl}
        (self.dir / (key.replace(":", "__") + ".lock")).write_text(json.dumps(rec))

    def test_stale_lock_of_a_dead_process_is_reclaimed(self):
        proc = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
        dead_pid = int(proc.stdout.strip())
        self._write_foreign("album:7", pid=dead_pid, heartbeat_age=60)
        self.locks.acquire(["album:7"], "new-owner", timeout=0.5)
        self.assertEqual(self.locks.held()[0]["owner"], "new-owner")

    def test_a_live_long_operation_is_never_stolen(self):
        """Heartbeat long expired, but the owner process is alive: not stale."""
        self._write_foreign("album:8", pid=os.getpid(), heartbeat_age=3600)
        with self.assertRaises(ResourceLockConflictError):
            self.locks.acquire(["album:8"], "thief", timeout=0.1)

    def test_other_host_lock_is_reclaimed_only_after_heartbeat_expiry(self):
        self._write_foreign("item:1", pid=1, heartbeat_age=0, host="other-container", ttl=60)
        with self.assertRaises(ResourceLockConflictError):
            self.locks.acquire(["item:1"], "me", timeout=0.1)
        self._write_foreign("item:2", pid=1, heartbeat_age=120, host="other-container", ttl=60)
        self.locks.acquire(["item:2"], "me", timeout=0.1)

    def test_hold_keeps_the_heartbeat_fresh_and_always_releases(self):
        with self.locks.hold(["album:1"], "long-op", ttl=0.3):
            first = self.locks.held()[0]["heartbeat_at"]
            time.sleep(0.5)
            self.assertGreater(self.locks.held()[0]["heartbeat_at"], first)
        self.assertEqual(self.locks.held(), [])
        with self.assertRaises(RuntimeError):
            with self.locks.hold(["album:1"], "x"):
                raise RuntimeError("boom")
        self.assertEqual(self.locks.held(), [])

    def test_threads_contending_get_exactly_one_holder(self):
        results = []
        barrier = threading.Barrier(8)

        def worker(i):
            barrier.wait()
            try:
                self.locks.acquire(["album-merge:ef4b6576-ac7c-4f72-bee6-e7a6b6cf019d"], f"t{i}", timeout=0.05)
                results.append(i)
            except ResourceLockConflictError:
                pass

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 1)

    def test_two_processes_never_both_own_the_same_key(self):
        start = time.time() + 1.0
        procs = [subprocess.Popen([sys.executable, "-c", _CHILD, str(ROOT), str(self.dir), "album:1935",
                                   str(start), f"proc-{i}"], stdout=subprocess.PIPE, text=True) for i in range(4)]
        outs = [p.communicate(timeout=30)[0].strip() for p in procs]
        self.assertEqual(sorted(outs).count("ACQUIRED"), 1, outs)
        self.assertEqual(sorted(outs).count("CONFLICT"), 3, outs)

    def test_invalid_keys_are_rejected(self):
        for bad in ("album:x", "../etc", "item:1;rm", "library", ""):
            with self.assertRaises(ValueError):
                validate_key(bad)


if __name__ == "__main__":
    unittest.main()
