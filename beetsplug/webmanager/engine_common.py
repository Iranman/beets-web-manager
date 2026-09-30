"""Helpers shared by the engine op modules (replace/remove/merge/untracked).

A leaf module: it imports nothing from this package at load time, so the op
modules can be imported in any order (operations imports them at its end,
and each of them imports operations).
"""

import hashlib
import os
import re
from typing import Any, Dict, Optional

from beets import config as beets_config
from beets.library import Item
from flask import jsonify

_QUARANTINE_ID = re.compile(r"[0-9a-f]{32}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
MANIFEST_NAME = "manifest.json"


_QUARANTINE_ROOT_OVERRIDE: Optional[str] = None


def set_quarantine_root(root: Optional[str]) -> None:
    """Override the engine quarantine folder (tests only)."""
    global _QUARANTINE_ROOT_OVERRIDE
    _QUARANTINE_ROOT_OVERRIDE = root


def _quarantine_root() -> str:
    if _QUARANTINE_ROOT_OVERRIDE:
        return os.path.abspath(_QUARANTINE_ROOT_OVERRIDE)
    return os.path.abspath(os.path.join(beets_config.config_dir(), "webmanager-quarantine"))


def _fspath(value: Any) -> str:
    if isinstance(value, bytes):
        return os.fsdecode(value)
    return str(value or "")


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return os.fsdecode(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _snapshot(item: Item) -> Dict[str, Any]:
    return {key: _jsonable(item.get(key)) for key in item.keys(computed=False)}


def _album_snapshot(album) -> Dict[str, Any]:
    return {k: _jsonable(album.get(k)) for k in album.keys(computed=False)}


def _restore_album_row(lib, album_id: int, snapshot: Dict[str, Any]):
    """Re-create a retired album row at its ORIGINAL id with its metadata.
    Beets' Model.add always allocates a new id, so the row is inserted with
    its id through the library's own transaction, then filled and stored
    through the normal model API."""
    with lib.transaction() as tx:
        tx.mutate("INSERT INTO albums (id) VALUES (?)", (int(album_id),))
    album = lib.get_album(int(album_id))
    album.update({k: v for k, v in snapshot.items() if k != "id"})
    album.store()
    return album


def _inside_allowed(path: str) -> bool:
    from . import operations as ops  # at call time: operations imports the op modules
    return ops.is_strict_descendant(path, ops.get_allowed_roots())


def _error(message: str, code: str, status: int = 400):
    return jsonify({"error": message, "error_code": code}), status


def _manifest_dir(quarantine_id: str) -> Optional[str]:
    """The engine's folder for one replacement, or None for a malformed id."""
    if not isinstance(quarantine_id, str) or not _QUARANTINE_ID.fullmatch(quarantine_id):
        return None
    root = _quarantine_root()
    folder = os.path.normpath(os.path.join(root, quarantine_id))
    if not folder.startswith(root + os.sep):
        return None
    return folder


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
