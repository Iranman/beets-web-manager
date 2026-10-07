"""Library folder steps for Web Manager's folder_cleanup_v1 transactions.

POST /webmanager/folder-op
    {"op": "move_file", "source", "target"}   one untracked file, no overwrite
    {"op": "rename_dir", "source", "target"}  one untracked folder, no overwrite
    {"op": "remove_empty_dir", "path"}        one empty folder
    {"op": "create_dir", "path"}              re-create a removed folder (rollback)

Web Manager mounts the library read-only. It plans, approves, audits and rolls
back the transaction in its own store; this endpoint performs each single
filesystem step inside Beets, which owns every write to the library.

Every path must lie strictly inside Beets' library ``directory`` with no
symlink in any component. A step never touches a path that holds a library
item: tracked files are relocated only by Beets' own ``item.move()`` /
``album.move()`` (POST /webmanager/move), never here. Steps use Beets'
filesystem helpers (``util.move``, ``util.prune_dirs``, ``util.mkdirall``)
and run inside a library transaction; only ``rename_dir`` uses ``os.rename``,
because Beets has no API to rename a directory that holds no library items.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from beets import util
from beets.dbcore.query import PathQuery
from flask import g, jsonify, request

from . import operations as ops
from .engine_common import _error


class _Refused(Exception):
    def __init__(self, message: str, code: str, status: int = 409):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


def _library_root(lib) -> Optional[str]:
    directory = getattr(lib, "directory", None)
    if directory:
        return os.path.abspath(os.fsdecode(directory))
    value = ops.get_library_directory()
    return os.path.abspath(value) if value else None


def contained_path(raw: Any, root: str) -> str:
    """The absolute path, refusing anything outside the library root, the
    root itself, and a symlink in any component below the root."""
    if not isinstance(raw, str) or not raw or "\x00" in raw or not os.path.isabs(raw):
        raise _Refused("path must be an absolute path inside the Beets library directory", "PATH_INVALID", 400)
    path = os.path.abspath(raw)
    try:
        inside = os.path.commonpath([path, root]) == root
    except ValueError:
        inside = False
    if not inside or path == root:
        raise _Refused("path is outside the Beets library directory", "PATH_OUTSIDE_LIBRARY", 400)
    current = os.path.realpath(root)
    for part in os.path.relpath(path, root).split(os.sep):
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise _Refused("a path component is a symlink", "SYMLINK_REJECTED", 400)
    return path


def _refuse_tracked(lib, path: str) -> None:
    if any(True for _ in lib.items(PathQuery("path", os.fsencode(path)))):
        raise _Refused("the path holds library items; move them through Beets instead", "PATH_IS_TRACKED")


def _refuse_target(target: str) -> None:
    if os.path.lexists(target):
        raise _Refused("target already exists", "TARGET_EXISTS")
    if not os.path.isdir(os.path.dirname(target)):
        raise _Refused("target parent folder does not exist", "TARGET_PARENT_MISSING")


def _step(lib, root: str, data: dict) -> dict:
    op = data.get("op")
    if op in ("move_file", "rename_dir"):
        source = contained_path(data.get("source"), root)
        target = contained_path(data.get("target"), root)
        want_dir = op == "rename_dir"
        if not (os.path.isdir(source) if want_dir else os.path.isfile(source)):
            raise _Refused("source is missing or of the wrong type", "SOURCE_MISSING")
        _refuse_tracked(lib, source)
        _refuse_target(target)
        if want_dir:
            # Beets has no API to rename an untracked directory.
            os.rename(source, target)
        else:
            util.move(source, target, replace=False)
        return {"op": op, "source": source, "target": target}
    if op == "remove_empty_dir":
        path = contained_path(data.get("path"), root)
        if not os.path.isdir(path):
            raise _Refused("folder is missing", "SOURCE_MISSING")
        if os.listdir(path):
            raise _Refused("folder is not empty", "NOT_EMPTY")
        util.prune_dirs(path, clutter=())  # no root: removes only this folder
        if os.path.lexists(path):
            raise _Refused("folder could not be removed", "REMOVE_FAILED", 500)
        return {"op": op, "path": path}
    if op == "create_dir":
        path = contained_path(data.get("path"), root)
        if os.path.isdir(path):
            return {"op": op, "path": path, "existed": True}
        if os.path.lexists(path):
            raise _Refused("a file is in the way", "TARGET_EXISTS")
        util.mkdirall(os.path.join(path, "_"))  # creates path and any missing parents
        return {"op": op, "path": path, "existed": False}
    raise _Refused("op must be move_file, rename_dir, remove_empty_dir or create_dir", "INVALID_OP", 400)


@ops.webmanager_bp.route("/folder-op", methods=["POST"])
def run_folder_op():
    data = request.get_json(force=True, silent=True) or {}
    lib = g.lib
    root = _library_root(lib)
    if not root:
        return _error("Beets library directory is not configured", "LIBRARY_DIRECTORY_UNKNOWN", 503)
    op_id, _fp, early = ops._idempotency_precheck("folder_op", data)
    if early is not None:
        return early
    try:
        with lib.transaction():
            result = {"success": True, **_step(lib, root, data)}
    except _Refused as exc:
        ops.update_operation(op_id, "failed", error=exc.message, error_code=exc.code)
        return _error(exc.message, exc.code, exc.status)
    except Exception:
        ops.log.exception("folder op failed")
        ops.update_operation(op_id, "failed", error="Folder operation failed", error_code="FOLDER_OP_FAILED")
        return _error("Folder operation failed", "FOLDER_OP_FAILED", 500)
    ops.update_operation(op_id, "succeeded", result=result)
    return jsonify({"operation_id": op_id, **result})
