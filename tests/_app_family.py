"""Patch a name on app.py *and* the owned modules extracted from it (ARCH-001).

app.py re-exports every function and global that moved into an owned service
module (backend/*_service.py, backend/app_runtime.py, ...). Code that moved
looks names up in its own module, so `mock.patch.object(app, "X", v)` alone no
longer reaches it. `patch_app_family(app, "X", v)` patches every module of the
family that binds the same object as app.X, with the same replacement.

It is a drop-in for `mock.patch.object` (context manager, decorator,
start()/stop()) and for `mock.patch("app.X", ...)` via `patch_app_family("app", "X")`.
"""

from __future__ import annotations

import functools
import importlib
import json
import sys
import types
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_SENTINEL = object()


def family_module_names() -> list:
    try:
        data = json.loads((_ROOT / "docs" / "arch001_app_ownership.json").read_text(encoding="utf-8"))
        mods = data.get("extracted_modules") or []
    except (OSError, ValueError):
        mods = []
    return [m.removesuffix(".py").replace("/", ".") for m in mods]


def _family_targets(app_module, name):
    original = getattr(app_module, name, _SENTINEL)
    targets = [app_module]
    if not isinstance(app_module, types.ModuleType) or not str(getattr(app_module, "__file__", "")).endswith("app.py"):
        return targets  # not the app module (e.g. the Flask object): plain patch
    for modname in family_module_names():
        mod = sys.modules.get(modname)
        if mod is None:
            try:
                mod = importlib.import_module(modname)
            except Exception:
                continue
        if mod is app_module:
            continue
        value = getattr(mod, name, _SENTINEL)
        if value is _SENTINEL:
            continue
        if original is _SENTINEL or value is original:
            targets.append(mod)
    return targets


class _FamilyPatch:
    def __init__(self, target, attribute, new=mock.DEFAULT, **kwargs):
        self._target = target
        self._attribute = attribute
        self._new = new
        self._kwargs = kwargs
        self._patchers = []

    def _resolve(self):
        target = self._target
        if isinstance(target, str):
            target = importlib.import_module(target)
        return target

    def start(self):
        app_module = self._resolve()
        targets = _family_targets(app_module, self._attribute)
        first = mock.patch.object(targets[0], self._attribute, self._new, **self._kwargs)
        replacement = first.start()
        self._patchers = [first]
        for mod in targets[1:]:
            p = mock.patch.object(mod, self._attribute, replacement)
            p.start()
            self._patchers.append(p)
        return replacement

    def stop(self):
        while self._patchers:
            self._patchers.pop().stop()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False

    def __call__(self, func):
        add_arg = self._new is mock.DEFAULT

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with _FamilyPatch(self._target, self._attribute, self._new, **self._kwargs) as value:
                if add_arg:
                    return func(*args, value, **kwargs)
                return func(*args, **kwargs)

        return wrapper


def patch_app_family(target, attribute, new=mock.DEFAULT, **kwargs):
    return _FamilyPatch(target, attribute, new, **kwargs)


def rebind_app_family(app_module, name, value):
    """Permanently rebind app.<name> and every family module sharing it.

    For tests that deliberately rebind a process-wide global (not a scoped
    patch): the owned modules that now hold the code see the same value."""
    for mod in _family_targets(app_module, name):
        setattr(mod, name, value)
