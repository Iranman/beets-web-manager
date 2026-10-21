"""scripts/run_tests.py must never drop a test class when it splits a module."""
import contextlib
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.append(str(ROOT / "scripts"))  # workers unpickle run_tests._run by name
import run_tests  # noqa: E402

MODULE = "_bwm_runtest_only_probe"
SOURCE = '''import unittest

class RunTestOnly(unittest.TestCase):
    def runTest(self):
        self.fail("must run")

class Normal(unittest.TestCase):
    def test_ok(self):
        pass
'''


class SplitModuleKeepsRunTestOnlyClassesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, True))
        Path(tmp, MODULE + ".py").write_text(SOURCE, encoding="utf-8")
        sys.path.insert(0, tmp)  # spawned workers inherit sys.path
        self.addCleanup(sys.path.remove, tmp)
        self.addCleanup(sys.modules.pop, MODULE, None)

    def test_classes_includes_a_runtest_only_class(self):
        self.assertEqual(run_tests._classes(MODULE), [f"{MODULE}.Normal", f"{MODULE}.RunTestOnly"])

    def test_failing_runtest_only_class_fails_the_run(self):
        # Unknown modules are estimated at 60 s, over --split-seconds, so this
        # module is split into classes: the failing runTest class must still run.
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = run_tests.main(["-j", "1", "--slowest", "0", MODULE])
        self.assertNotEqual(code, 0, out.getvalue())
        self.assertIn("Ran 2 tests", out.getvalue())
        self.assertIn(f"{MODULE}.RunTestOnly", out.getvalue())


HANG_MODULE = "_bwm_runtest_hang_probe"
HANG_SOURCE = '''import time, unittest

class Hang(unittest.TestCase):
    def test_hang(self):
        time.sleep(3600)  # wedge a worker, like the #466 Beets db-lock deadlock
'''


class StallWatchdogFailsInsteadOfHangingTests(unittest.TestCase):
    """#466: a wedged worker must fail the run within the stall timeout, not
    block to GitHub's 6h job ceiling. run_tests calls os._exit on a stall, so
    this runs it in a subprocess."""

    @unittest.skipUnless(sys.platform != "win32", "watchdog SIGABRT dump is POSIX-only")
    def test_a_hung_unit_trips_the_watchdog(self):
        import os
        import subprocess
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, True))
        Path(tmp, HANG_MODULE + ".py").write_text(HANG_SOURCE, encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([tmp, str(ROOT / "scripts"), env.get("PYTHONPATH", "")])
        env["RUN_TESTS_STALL_TIMEOUT"] = "20"  # > worker startup (imports app.py), < the 3600s hang
        proc = subprocess.run(
            [sys.executable, "-c",
             f"import sys, run_tests; sys.exit(run_tests.main(['-j','1','--slowest','0','{HANG_MODULE}']))"],
            env=env, capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(proc.returncode, 1, (proc.stdout + proc.stderr)[-3000:])
        self.assertIn("wedged", proc.stderr)
        self.assertIn(HANG_MODULE, proc.stderr)


class SuiteEnvSurvivesTests(unittest.TestCase):
    def test_no_test_module_calls_patch_stopall(self):
        # stopall also stops the suite-wide env patcher test_ai_batch_retry_race
        # starts at import, so later tests in the same process lose that env.
        offenders = [p.name for p in (ROOT / "tests").glob("test_*.py")
                     if re.search(r"patch\.stopall\s*[(),]", p.read_text(encoding="utf-8"))]
        self.assertEqual(offenders, [], "stop your own patchers instead of mock.patch.stopall")


if __name__ == "__main__":
    unittest.main()
