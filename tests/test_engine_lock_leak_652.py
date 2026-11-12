"""#652: a transaction whose commit fails must not orphan Beets' _db_lock, and
the run_tests leak guard must name a unit that leaves the engine wedged."""

import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from beets.dbcore.db import DBAccessError, DBCustomFunctionError
from beets.library import Library

from backend import beets_embedded

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import run_tests  # noqa: E402


class FailedCommitReleasesLock(unittest.TestCase):
    def _orphan_attempt(self, guard: bool) -> bool:
        td = tempfile.mkdtemp()
        lib = Library(os.path.join(td, "lib.blb"))
        if guard:
            beets_embedded._release_lock_on_failed_exit(lib)
        lib._close()
        shutil.rmtree(td)  # the library's directory is gone: a new thread cannot connect
        errors = []

        def read():
            try:
                lib.albums()
            except Exception as ex:
                errors.append(ex)

        t = threading.Thread(target=read)
        t.start()
        t.join(10)
        self.assertEqual(len(errors), 1, errors)  # the failure still surfaces
        self.assertIsInstance(errors[0], (sqlite3.OperationalError, DBAccessError))
        return lib._db_lock.locked()

    def test_stock_beets_orphans_the_lock(self):
        self.assertTrue(self._orphan_attempt(guard=False))  # the bug this guards against

    def test_engine_library_releases_the_lock(self):
        self.assertFalse(self._orphan_attempt(guard=True))

    def test_nested_transactions_still_work(self):
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        lib = Library(os.path.join(td, "lib.blb"))
        self.addCleanup(lib._close)
        beets_embedded._release_lock_on_failed_exit(lib)
        with lib.transaction():
            with lib.transaction() as tx:
                tx.query("SELECT 1")
            self.assertTrue(lib._db_lock.locked())
        self.assertFalse(lib._db_lock.locked())

    def test_custom_function_error_leaves_another_threads_lock_alone(self):
        """Beets raises DBCustomFunctionError after the root commit already
        released the lock; a thread that took the lock in that window must
        keep it (QA finding on #658)."""
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        lib = Library(os.path.join(td, "lib.blb"))
        self.addCleanup(lib._close)
        beets_embedded._release_lock_on_failed_exit(lib)
        real = lib._db_lock
        took, done = threading.Event(), threading.Event()

        def other():
            real.acquire()
            took.set()
            done.wait(10)
            real.release()

        releases = []

        class Racy:  # on the first release, another thread wins the lock at once
            acquire, locked = real.acquire, real.locked

            def release(self):
                releases.append(1)
                real.release()
                if len(releases) == 1:
                    threading.Thread(target=other).start()
                    took.wait(10)

        lib._db_lock = Racy()
        try:
            with self.assertRaises(DBCustomFunctionError):
                with lib.transaction():
                    raise sqlite3.OperationalError("user-defined function raised exception")
            self.assertEqual(len(releases), 1)  # never released the other thread's lock
            self.assertTrue(real.locked())
        finally:
            done.set()


class RunTestsLeakGuard(unittest.TestCase):
    def test_held_engine_lock_is_reported_and_engine_reset(self):
        held = mock.Mock(**{"acquire.return_value": False})  # a lock nobody releases
        fake = types.SimpleNamespace(lib=types.SimpleNamespace(_db_lock=held, path=b":memory:"))
        mod = types.SimpleNamespace(_engine=fake, reset=mock.Mock())
        with mock.patch.dict(sys.modules, {"backend.beets_embedded": mod}):
            problems = run_tests._leaks(set())
        self.assertTrue(any("db lock" in p for p in problems), problems)
        mod.reset.assert_called_once_with()

    def test_clean_engine_is_not_reported(self):
        fake = types.SimpleNamespace(lib=types.SimpleNamespace(_db_lock=threading.Lock(), path=b":memory:"))
        mod = types.SimpleNamespace(_engine=fake, reset=mock.Mock())
        with mock.patch.dict(sys.modules, {"backend.beets_embedded": mod}):
            self.assertEqual(run_tests._leaks(set()), [])
        mod.reset.assert_not_called()

    def test_deleted_library_file_is_reported_and_engine_reset(self):
        fake = types.SimpleNamespace(lib=types.SimpleNamespace(
            _db_lock=threading.Lock(), path=os.fsencode(os.path.join(tempfile.gettempdir(), "gone-652.blb"))))
        mod = types.SimpleNamespace(_engine=fake, reset=mock.Mock())
        with mock.patch.dict(sys.modules, {"backend.beets_embedded": mod}):
            problems = run_tests._leaks(set())
        self.assertTrue(any("was deleted" in p for p in problems), problems)
        mod.reset.assert_called_once_with()

    def test_live_job_thread_is_reported(self):
        jt = types.SimpleNamespace(assert_no_new_job_threads=mock.Mock(side_effect=AssertionError("job thread alive")))
        with mock.patch.dict(sys.modules, {"_job_threads": jt, "backend.beets_embedded": types.SimpleNamespace()}):
            self.assertEqual(run_tests._leaks(set()), ["job thread alive"])


if __name__ == "__main__":
    unittest.main()
