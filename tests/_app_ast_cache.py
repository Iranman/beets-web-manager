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
import collections
import datetime
import functools
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import time
import typing
from collections import Counter, defaultdict, deque
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
    cast,
)
import unicodedata
import uuid

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


def load_app_symbols(
    names: Iterable[str] | set[str],
    namespace: Optional[Dict[str, Any]] = None,
    extra_ns: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Extract and exec selected top-level AST nodes from app.py into a namespace.

    Provides a comprehensive baseline namespace including standard typing symbols,
    common stdlib modules, and compiles AST nodes with `from __future__ import annotations`
    to guarantee consistent, version-independent annotation handling (Python 3.11-3.14+).
    """
    target_names = set(names)
    tree = get_app_ast()

    base_ns: Dict[str, Any] = {
        # typing
        "Any": Any,
        "Callable": Callable,
        "Dict": Dict,
        "Iterable": Iterable,
        "Iterator": Iterator,
        "List": List,
        "Mapping": Mapping,
        "NamedTuple": NamedTuple,
        "Optional": Optional,
        "Sequence": Sequence,
        "Set": Set,
        "Tuple": Tuple,
        "Union": Union,
        "cast": cast,
        "typing": typing,
        # stdlib
        "collections": collections,
        "Counter": Counter,
        "defaultdict": defaultdict,
        "deque": deque,
        "datetime": datetime,
        "functools": functools,
        "hashlib": hashlib,
        "itertools": itertools,
        "json": json,
        "math": math,
        "os": os,
        "re": re,
        "shutil": shutil,
        "sys": sys,
        "time": time,
        "uuid": uuid,
        "unicodedata": unicodedata,
        "Path": Path,
    }

    try:
        import backend.matching as _bm
        for attr in dir(_bm):
            if not attr.startswith("__"):
                base_ns[attr] = getattr(_bm, attr)
        base_ns["_canonical_album_track_score"] = _bm.album_track_score
        base_ns["_canonical_best_album_track_match"] = _bm.best_album_track_match
        base_ns["_canonical_normalize_artist"] = _bm.normalize_artist
        base_ns["_canonical_normalize_title"] = _bm.normalize_title
        base_ns["_canonical_similarity"] = _bm.similarity
        base_ns["_canonical_strip_track_filename_id_suffix"] = _bm.strip_track_filename_id_suffix
        base_ns["_canonical_title_variants"] = _bm.title_variants
        base_ns["_canonical_track_feature_variants"] = _bm.track_feature_variants
        base_ns["_canonical_track_filename_has_source_id_suffix"] = _bm.track_filename_has_source_id_suffix
        base_ns["_canonical_track_parenthetical_alias_variants"] = _bm.track_parenthetical_alias_variants
        base_ns["_canonical_track_path_prefixes"] = _bm.track_path_prefixes
        base_ns["_canonical_track_title_variants_for_matching"] = _bm.track_title_variants_for_matching
    except Exception:
        pass

    if namespace is not None:
        base_ns.update(namespace)
    if extra_ns is not None:
        base_ns.update(extra_ns)

    future_import = ast.ImportFrom(
        module="__future__",
        names=[ast.alias(name="annotations", asname=None)],
        level=0,
    )

    for node in tree.body:
        node_name = ""
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in target_names:
                    node_name = target.id
                    break
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id in target_names:
                node_name = node.target.id
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            node_name = node.name

        if node_name and node_name in target_names:
            mod = ast.Module(body=[future_import, node], type_ignores=[])
            ast.fix_missing_locations(mod)
            exec(compile(mod, str(_APP_PY_PATH), "exec"), base_ns)

    return base_ns

