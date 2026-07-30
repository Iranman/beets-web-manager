"""Shared, process-wide cache for `ast.parse(app.py)`.

Several test files extract individual top-level functions/assignments out
of app.py's source (via `ast.parse` + selective `exec`) so they can unit
test pure helpers without importing the whole Flask app. app.py is ~49.5k
lines; `ast.parse` alone costs roughly 10-15 seconds per call on this
project's scale, independent of file I/O (read_text itself is ~0.02s).
Every test file that re-parsed it separately paid that cost once per call
-- across 7 files, several of which call their loader once per test
method, this made the full suite take dramatically longer than it needed
to and looked indistinguishable from a hang during a live run (long
stretches with no visible per-test output between pytest's percentage
ticks). There was no actual deadlock, leaked subprocess, or leaked
thread -- parsing a 49k-line file repeatedly is just that expensive.

get_app_ast() parses app.py exactly once per test process and returns the
same tree to every caller. Callers must not mutate the returned tree;
`ast.fix_missing_locations` on individual extracted nodes (which is what
every caller already does) does not touch the shared tree itself, since
each caller builds its own `ast.Module(body=[node], ...)` wrapper node
around a node object taken from the tree -- the wrapper is what gets
mutated/compiled, not the cached tree, so sharing the tree across callers
is safe.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Optional

_APP_PY_PATH = Path(__file__).resolve().parents[1] / "app.py"
_cached_tree: Optional[ast.Module] = None
_cached_source: Optional[str] = None
_parse_count = 0


def get_app_ast() -> ast.Module:
    """Return the parsed AST of app.py, parsing only on first call."""
    global _cached_tree, _cached_source, _parse_count
    if _cached_tree is None:
        _cached_source = _APP_PY_PATH.read_text(encoding="utf-8")
        _cached_tree = ast.parse(_cached_source)
        _parse_count += 1
    return _cached_tree


def get_app_source() -> str:
    """Return app.py's source text (read once, alongside the cached AST)."""
    get_app_ast()
    assert _cached_source is not None
    return _cached_source


def parse_count() -> int:
    """Number of times app.py has actually been parsed this process. Test-only."""
    return _parse_count
