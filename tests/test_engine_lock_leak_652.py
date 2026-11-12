"""#652: a transaction whose commit fails must not orphan Beets' _db_lock, and
the run_tests leak guard must name a unit that leaves the engine wedged."""

import os
import shutil
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
