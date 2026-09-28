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

_REPO_ROOT = Path(__file__).resolve().parents[1]
_APP_PY_PATH = _REPO_ROOT / "app.py"
_cached_tree: Optional[ast.Module] = None
_cached_source: Optional[str] = None
_parse_count = 0


def app_family_paths() -> list:
    """app.py plus every module extracted from it (ARCH-001), in a stable order.

    Helpers that tests extract from source keep working after a function
    moves out of app.py into an owned module: the family is parsed as one
    combined module. The list is maintained by scripts/arch001_extract.py in
    docs/arch001_app_ownership.json ("extracted_modules").
    """
    inventory = _REPO_ROOT / "docs" / "arch001_app_ownership.json"
    try:
        modules = json.loads(inventory.read_text(encoding="utf-8")).get("extracted_modules") or []
    except Exception:
        modules = []
    # The foundation layer came from the top of app.py, so it precedes it;
    # domain modules follow in extraction order.
    before = [_REPO_ROOT / m for m in modules if m == "backend/app_runtime.py"]
    after = [_REPO_ROOT / m for m in modules if m != "backend/app_runtime.py"]
    return [p for p in before + [_APP_PY_PATH] + after if p.exists()]


def _bound_names(node: ast.stmt) -> list:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    targets = []
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    names = []
    for target in targets:
        for sub in ast.walk(target):
            if isinstance(sub, ast.Name):
                names.append(sub.id)
    return names


def _segment(lines: list, node: ast.stmt) -> str:
    start = node.lineno
    decorators = getattr(node, "decorator_list", None) or []
    if decorators:
        start = min(start, min(d.lineno for d in decorators))
    return "".join(lines[start - 1:node.end_lineno])


_ORDER_FILE = _REPO_ROOT / "tests" / "arch001_original_app_order.json"


def _original_order() -> Dict[str, int]:
    try:
        units = json.loads(_ORDER_FILE.read_text(encoding="utf-8"))["units"]
    except (OSError, ValueError, KeyError):
        units = []
    return {name: i for i, name in enumerate(units)}


@functools.lru_cache(maxsize=1)
def _family_source_cached(stamp: tuple) -> str:
    """Lay the module family out in original (pre-ARCH-001) app.py order.

    Every top-level unit named in tests/arch001_original_app_order.json takes
    its original position; a unit without an original position follows the
    unit before it in its current file. ARCH-001 re-export blocks vanish, and
    extracted modules' own imports/docstrings trail at the end.
    """
    order = _original_order()
    entries = []
    tail = []
    for file_idx, path in enumerate(app_family_paths()):
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        tree = ast.parse(text)
        is_app = path == _APP_PY_PATH
        anchor = -1
        seq = 0
        prev_end = 0
        pending = ""
        for node_idx, node in enumerate(tree.body):
            trivia = "".join(lines[prev_end:node.lineno - 1])  # leading comments travel with the unit
            node_text = "".join(lines[node.lineno - 1:node.end_lineno])
            prev_end = node.end_lineno
            if is_app and isinstance(node, ast.ImportFrom) and "ARCH-001 extracted" in lines[node.lineno - 1]:
                pending += trivia  # a re-export block vanishes; its comments stay in place
                continue
            chunk = pending + trivia + node_text
            pending = ""
            if not is_app and (isinstance(node, (ast.Import, ast.ImportFrom)) or (
                    node_idx == 0 and isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant))):
                tail.append(chunk)
                continue
            idx = next((order[n] for n in _bound_names(node) if n in order), None)
            if idx is not None:
                anchor = idx
                key = (idx, 0, file_idx, seq)
            else:
                key = (anchor, 1, file_idx, seq)
            seq += 1
            entries.append((key, chunk if chunk.endswith("\n") else chunk + "\n"))
    entries.sort(key=lambda e: e[0])
    return "".join(text for _, text in entries) + "\n\n" + "".join(tail)


def app_family_source() -> str:
    """Source of the app.py module family, laid out as the original app.py,
    so marker-to-marker source slices used by older tests keep their meaning."""
    stamp = tuple((str(p), p.stat().st_mtime_ns) for p in app_family_paths())
    return _family_source_cached(stamp)


def app_unit_source(name: str) -> str:
    """Source of one top-level definition, wherever it lives in the family."""
    source = app_family_source()
    lines = source.splitlines(keepends=True)
    for node in get_app_ast().body:
        if name in _bound_names(node):
            return _segment(lines, node)
    raise KeyError(name)


def load_app_closure(roots: Iterable[str], namespace: Dict[str, Any]) -> Dict[str, Any]:
    """Exec `roots` plus every top-level unit they transitively reference.

    Names already present in `namespace` are treated as provided stubs and
    are not loaded from source. Units run in family (original) order.
    """
    import logging
    import threading
    for mod in (collections, datetime, functools, hashlib, itertools, json, logging, math, os, re,
                shutil, sys, threading, time, typing, unicodedata, uuid):
        namespace.setdefault(mod.__name__, mod)
    for alias in ("Any", "Callable", "Dict", "Iterable", "List", "Optional", "Set", "Tuple", "Union"):
        namespace.setdefault(alias, getattr(typing, alias))
    namespace.setdefault("Path", Path)
    tree = get_app_ast()
    lines = app_family_source().splitlines(keepends=True)
    defs: Dict[str, ast.stmt] = {}
    for node in tree.body:
        for bound in _bound_names(node):
            defs.setdefault(bound, node)
    wanted: Dict[int, ast.stmt] = {}
    stack = list(roots)
    while stack:
        name = stack.pop()
        if name in namespace or name not in defs:
            continue
        node = defs[name]
        if id(node) in wanted:
            continue
        wanted[id(node)] = node
        local = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Del)) and sub is not node:
                local.add(sub.id)
            elif isinstance(sub, ast.arg):
                local.add(sub.arg)
            elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and sub is not node:
                local.add(sub.name)
            elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                local.update((a.asname or a.name).split(".")[0] for a in sub.names)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            local = set()
        stack.extend(
            sub.id for sub in ast.walk(node)
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load) and sub.id not in local
        )
    for node in sorted(wanted.values(), key=lambda n: n.lineno):
        exec(compile("from __future__ import annotations\n" + _segment(lines, node).lstrip(), str(_APP_PY_PATH), "exec"), namespace)
    return namespace


_SLICE_TREES: Dict[int, ast.Module] = {}


def source_between(src: str, start_marker: str, end_marker: str) -> str:
    """src[start_marker:end_marker], as older tests slice it.

    ARCH-001 moves functions into owned modules, so an end marker that used
    to follow a function can now precede it or live elsewhere. When the end
    marker no longer follows the start, the slice ends with the top-level
    definition that contains the start marker.
    """
    start = src.index(start_marker)
    end = src.find(end_marker, start)
    if end != -1:
        return src[start:end]
    key = hash(src)
    tree = _SLICE_TREES.get(key)
    if tree is None:
        tree = get_app_ast() if src == app_family_source() else ast.parse(src)
        _SLICE_TREES[key] = tree
    line = src.count("\n", 0, start) + 1
    lines = src.splitlines(keepends=True)
    for node in tree.body:
        if node.lineno <= line <= node.end_lineno:
            stop = sum(len(l) for l in lines[:node.end_lineno])
            return src[start:stop]
    raise ValueError(f"{start_marker!r} is not inside a top-level definition")


def get_app_ast() -> ast.Module:
    """Return the parsed AST of the app.py module family, parsing only once."""
    global _cached_tree, _cached_source, _parse_count
    if _cached_tree is None:
        _cached_source = app_family_source()
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

