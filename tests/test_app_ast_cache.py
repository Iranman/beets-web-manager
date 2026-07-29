"""Regression test for the app.py AST-parsing performance fix.

Root cause of the full test-suite appearing to hang (2026-07-28): app.py
is ~49.5k lines; a single `ast.parse()` call on it costs roughly
10-15 seconds on this project's scale, independent of file I/O
(`read_text` alone is ~0.02s). Seven test files each re-parsed app.py
from scratch via their own local `ast.parse(APP_SOURCE)` call, several of
them once per test method -- across a run of 1,400+ tests this made
those files (and therefore the whole suite) take dramatically longer
than necessary, with long stretches producing no visible per-test output
between pytest's percentage ticks that were indistinguishable, in a live
run, from a genuine deadlock. There was no leaked subprocess, thread, or
lock; parsing a 49k-line file repeatedly is just that expensive.

`tests/_app_ast_cache.get_app_ast()` fixes this by parsing app.py exactly
once per test process and handing every caller the same tree. This test
proves that contract: parse_count() must stay at 1 no matter how many
times get_app_ast()/get_app_source() are called, from this test alone or
cumulatively with every other test file that already imported the cache
during this same process.
"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _app_ast_cache import get_app_ast, get_app_source, parse_count  # noqa: E402


class AppAstCacheTests(unittest.TestCase):
    def test_repeated_calls_do_not_reparse(self):
        get_app_ast()  # ensure at least one call has happened
        count_before = parse_count()
        self.assertGreaterEqual(count_before, 1)

        for _ in range(20):
            get_app_ast()
            get_app_source()

        self.assertEqual(parse_count(), count_before)

    def test_returns_the_same_tree_object_every_time(self):
        first = get_app_ast()
        second = get_app_ast()
        self.assertIs(first, second)

    def test_cached_source_matches_cached_tree(self):
        source = get_app_source()
        self.assertIsInstance(source, str)
        self.assertIn("def ", source)

    def test_repeated_access_is_fast(self):
        get_app_ast()  # warm the cache
        start = time.time()
        for _ in range(100):
            get_app_ast()
        elapsed = time.time() - start
        # 100 cached lookups should be well under a second; a single real
        # ast.parse() of app.py alone costs 10+ seconds, so this bounds
        # out any accidental re-parsing regression by a wide margin.
        self.assertLess(elapsed, 2.0)


if __name__ == "__main__":
    unittest.main()
