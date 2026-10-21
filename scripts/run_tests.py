#!/usr/bin/env python3
"""Run the backend unittest suite across worker processes.

Runs the same tests as ``python -m unittest discover -s tests -p "test_*.py"``:
every ``tests/test_*.py`` module is imported as a top-level module with
``tests/`` on ``sys.path``, as discover does. Modules are handed out
longest-first (by ``scripts/test_durations.json``) to ``--jobs`` worker
processes, each running them with the stock ``unittest`` runner. A module
recorded as slower than ``--split-seconds`` is handed out one TestCase class
at a time. The summary and exit code follow unittest's.

    python scripts/run_tests.py                   # all modules, os.cpu_count() workers
    python scripts/run_tests.py -j 4 -v           # 4 workers, per-test output
    python scripts/run_tests.py test_job_contract # selected modules only
    python scripts/run_tests.py --save-durations  # refresh the balancing data
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import importlib
import io
import json
import multiprocessing
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
DURATIONS = os.path.join(ROOT, "scripts", "test_durations.json")
# Under discover, this module is imported (at collection, before any test
# runs) and sets the process-wide test environment: temp BEETSDIR, data and
# transaction dirs, then imports app.py inside them. Other modules rely on
# that (see the "must be the first to import app" notes in tests/), so every
# worker imports it first too.
SUITE_ENV_MODULE = "test_ai_batch_retry_race"


def _init_worker() -> None:
    # Match `python -m unittest discover -s tests` run from the repo root:
    # tests/ first, then the root (cwd), and not this script's own directory.
    scripts = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [TESTS, ROOT] + [p for p in sys.path if p not in (TESTS, ROOT, scripts, "")]
    try:
        importlib.import_module(SUITE_ENV_MODULE)
    except Exception:
        pass  # its own tests report the import error when they run


def _classes(module: str) -> list[str]:
    """TestCase classes the loader would collect from module ([] = run whole)."""
    try:
        mod = importlib.import_module(module)
    except Exception:
        return []
    if hasattr(mod, "load_tests"):
        return []
    loader = unittest.defaultTestLoader
    return [
        f"{module}.{name}" for name in dir(mod)
        if isinstance(obj := getattr(mod, name), type) and issubclass(obj, unittest.TestCase)
        and loader.loadTestsFromTestCase(obj).countTestCases()  # counts runTest-only classes too
    ]


def _run(name: str, verbosity: int) -> dict:
    stream = io.StringIO()
    start = time.monotonic()
    loader = unittest.defaultTestLoader
    try:
        suite = loader.loadTestsFromName(name)
    except unittest.SkipTest as exc:  # module-level skip: reported as discover does
        suite = unittest.loader._make_skipped_test(name, exc, unittest.TestSuite)
    except Exception:  # any other import-time error: a failing test, not a dead runner
        suite, _ = unittest.loader._make_failed_import_test(name, unittest.TestSuite)
    result = unittest.TextTestRunner(stream=stream, verbosity=verbosity).run(suite)
    return {
        "name": name,
        "run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "ok": result.wasSuccessful(),
        "output": stream.getvalue(),
        "seconds": time.monotonic() - start,
    }


def _dump_worker_stacks(pool) -> None:
    """SIGABRT every pool worker so `python -X faulthandler` prints each one's
    thread stacks to stderr -- the evidence for why a worker is wedged."""
    import signal
    sigabrt = getattr(signal, "SIGABRT", None)
    if sigabrt is None or os.name != "posix":
        return
    for pid in list(getattr(pool, "_processes", {}) or {}):
        try:
            os.kill(pid, sigabrt)
        except OSError:
            pass
    time.sleep(5)  # let the faulthandler output reach stderr before we exit


def _modules() -> list[str]:
    return sorted(
        f[:-3] for f in os.listdir(TESTS)
        if f.startswith("test_") and f.endswith(".py") and f[:-3].isidentifier()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("modules", nargs="*", help="test module names (default: all)")
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("-v", "--verbose", action="store_true", help="per-test output for every module")
    parser.add_argument("--split-seconds", type=float, default=30.0,
                        help="run modules recorded slower than this one class at a time")
    parser.add_argument("--slowest", type=int, default=10, help="list the N slowest modules (0: none)")
    parser.add_argument("--save-durations", action="store_true",
                        help=f"write module timings to {os.path.relpath(DURATIONS, ROOT)}")
    args = parser.parse_args(argv)

    modules = [m[:-3] if m.endswith(".py") else m for m in (args.modules or _modules())]
    modules = [m.replace("\\", "/").rsplit("/", 1)[-1].removeprefix("tests.") for m in modules]
    try:
        with open(DURATIONS, encoding="utf-8") as fh:
            known = json.load(fh)
    except (OSError, ValueError):
        known = {}

    verbosity = 2 if args.verbose else 1
    results: list[dict] = []
    start = time.monotonic()
    # spawn: workers start from a clean interpreter on every OS (no fork-with-threads).
    ctx = multiprocessing.get_context("spawn")
    with cf.ProcessPoolExecutor(max_workers=max(1, args.jobs), mp_context=ctx, initializer=_init_worker) as pool:
        try:
            # Estimated seconds per unit; unknown (new) modules go early so they can't become the tail.
            units = {m: known.get(m, 60.0) for m in modules}
            big = [m for m in modules if units[m] > args.split_seconds]
            for m, classes in zip(big, pool.map(_classes, big)):
                if classes:
                    share = units.pop(m) / len(classes)
                    units.update({c: share for c in classes})
            futures = {pool.submit(_run, u, verbosity): u for u in sorted(units, key=units.get, reverse=True)}
            # Watchdog: no single test unit may wedge the whole run. If no
            # unit finishes within RUN_TESTS_STALL_TIMEOUT seconds a worker is
            # hung (e.g. a deadlock), so dump every worker's thread stacks and
            # fail, instead of blocking to the 6h CI job ceiling (#466). The
            # window resets whenever any unit finishes, so a legitimately slow
            # unit is fine as long as the run keeps making progress.
            stall = float(os.environ.get("RUN_TESTS_STALL_TIMEOUT", "900"))
            pending = set(futures)
            while pending:
                done, pending = cf.wait(pending, timeout=stall, return_when=cf.FIRST_COMPLETED)
                if not done:
                    stuck = sorted(futures[f] for f in pending)
                    print(f"\nrun_tests: no test unit finished in {stall:.0f}s -- a worker is "
                          f"wedged. Still running ({len(stuck)}): {' '.join(stuck)}", file=sys.stderr)
                    _dump_worker_stacks(pool)
                    sys.stdout.flush()
                    sys.stderr.flush()
                    os._exit(1)  # the worker is deadlocked; a clean shutdown would join it forever
                for fut in done:
                    res = fut.result()
                    results.append(res)
                    if args.verbose or not res["ok"]:
                        sys.stdout.write(f"\n===== {res['name']} =====\n{res['output']}")
                        sys.stdout.flush()
        except cf.process.BrokenProcessPool:
            done = {r["name"] for r in results}
            lost = sorted(set(units) - done)
            print(f"\nA worker process died (see faulthandler output above). "
                  f"Unfinished ({len(lost)}): {' '.join(lost)}", file=sys.stderr)
            return 1
    elapsed = time.monotonic() - start

    per_module: dict[str, float] = {}
    for r in results:
        mod = r["name"].split(".", 1)[0]
        per_module[mod] = per_module.get(mod, 0.0) + r["seconds"]
    if args.slowest:
        print(f"\nSlowest {args.slowest} modules (worker seconds):")
        for mod, secs in sorted(per_module.items(), key=lambda kv: kv[1], reverse=True)[: args.slowest]:
            print(f"  {secs:8.1f}s  {mod}")
    if args.save_durations:
        known.update({m: round(s, 1) for m, s in per_module.items()})
        with open(DURATIONS, "w", encoding="utf-8") as fh:
            json.dump(dict(sorted(known.items())), fh, indent=0)
            fh.write("\n")

    total = {k: sum(r[k] for r in results) for k in
             ("run", "failures", "errors", "skipped", "expected_failures", "unexpected_successes")}
    print("-" * 70)
    print(f"Ran {total['run']} tests in {elapsed:.3f}s ({len(per_module)} modules, {len(results)} units, {args.jobs} workers)\n")
    labels = [("failures", "failures"), ("errors", "errors"), ("skipped", "skipped"),
              ("expected_failures", "expected failures"), ("unexpected_successes", "unexpected successes")]
    detail = ", ".join(f"{label}={total[k]}" for k, label in labels if total[k])
    ok = all(r["ok"] for r in results)
    if ok:
        print("OK" + (f" ({detail})" if detail else ""))
    else:
        failed = sorted(r["name"] for r in results if not r["ok"])
        print(f"FAILED ({detail})\nFailing: {' '.join(failed)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
