"""Remove an album row or an imported release, reversibly.

POST /webmanager/album-row/remove
    {"album_id": N}
    Removes an EMPTY album row from the library. The row is snapshotted
    into an engine-owned manifest folder first, and the folder's id is
    returned, so the removal can be undone. An album row that still holds
    items is refused (ALBUM_NOT_EMPTY): emptying a row is the duplicate
    cleanup's job, not this one's.

POST /webmanager/album-row/rollback
    {"album_row_id": "<32 hex>"}
    Reads the engine's own manifest -- never a snapshot supplied by the
    caller -- and re-creates the album row at its ORIGINAL id with its
    original metadata.

POST /webmanager/import/rollback
    {"album_id": N, "item_ids": [..], "move_files": true}
    Undoes a confirmed import: every imported item's file is moved into an
    engine-owned quarantine folder (never deleted) and its library row is
    removed; the imported album row is removed as well. With
    ``move_files: false`` the files stay where they are and only the rows
    go -- what an in-place import needs, because there the files are the
    operator's own. The engine records snapshots in a manifest and returns
    the folder's id.

POST /webmanager/import/rollback/undo
    {"quarantine_id": "<32 hex>"}
    Reads the engine's own manifest, moves each file back to where it was
    and re-adds its library row (at its original id when that id is free).

The Web Manager decides *whether* a removal may happen and *whether* an
import may be undone (a reviewed and approved transaction); this module
only performs the change inside Beets.
"""

import json
import os
import shutil
import uuid
from typing import Any, Dict, List, Optional

from flask import g, jsonify, request

from . import operations as ops
from .engine_common import (
    MANIFEST_NAME,
    _album_snapshot,
    _error,
    _fspath,
    _inside_allowed,
    _manifest_dir,
    _quarantine_root,
    _restore_album_row,
    _snapshot,
)

MAX_ITEMS = 500
ALBUM_ROW_KIND = "album_row_remove"
IMPORT_ROLLBACK_KIND = "import_rollback"


def _readd(lib, snapshot: Dict[str, Any], path: str):
    """Re-add a removed row -- at its original id when that id is free (the
    same technique as merge_ops._restore_album_row), else under a new id."""
    from beets.library import Item

    fields = {k: v for k, v in snapshot.items() if k not in ("id", "path")}
    album_id = snapshot.get("album_id")
    fields["album_id"] = album_id if album_id and lib.get_album(album_id) is not None else None
    old_id = snapshot.get("id")
    if isinstance(old_id, int) and old_id > 0 and lib.get_item(old_id) is None:
        with lib.transaction() as tx:
            tx.mutate("INSERT INTO items (id) VALUES (?)", (old_id,))
        item = lib.get_item(old_id)
        item.update(fields)
        item.path = os.fsencode(path)
        item.store()
        return item
    item = Item(**fields)
    item.path = os.fsencode(path)
    lib.add(item)
    return item


@ops.webmanager_bp.route("/album-row/remove", methods=["POST"])
def run_album_row_remove():
    """Remove one empty album row, reversibly."""
    data = request.get_json(force=True, silent=True) or {}
    lib = g.lib
    try:
        album_id = int(data.get("album_id"))
    except (TypeError, ValueError):
        return _error("album_id must be an integer", "INVALID_ALBUM_ID")
    album = lib.get_album(album_id)
    if album is None:
        return _error("album not found", "ALBUM_NOT_FOUND", 404)
    members = [i.id for i in lib.items(f"album_id:{album_id}")]
    if members:
        return _error(f"album {album_id} still holds {len(members)} item(s)",
                      "ALBUM_NOT_EMPTY", 409)

    op_id, _fingerprint, early = ops._idempotency_precheck("album_row_remove", data)
    if early is not None:
        return early

    def fail(message: str, code: str, status: int = 400):
        ops.update_operation(op_id, "failed", error=message, error_code=code)
        return _error(message, code, status)

    row_id = uuid.uuid4().hex
    folder = os.path.join(_quarantine_root(), row_id)
    snapshot = _album_snapshot(album)
    try:
        with ops.mutation_lock:
            os.makedirs(folder)
            with open(os.path.join(folder, MANIFEST_NAME), "w", encoding="utf-8") as fh:
                json.dump({"kind": ALBUM_ROW_KIND, "album_id": album_id,
                           "snapshot": snapshot, "status": "applied"}, fh)
            album.remove(delete=False, with_items=False)
        result = {"success": True, "album_id": album_id, "album_row_id": row_id,
                  "removed": 1}
        ops.update_operation(op_id, "succeeded", result=result)
        return jsonify({"operation_id": op_id, **result})
    except Exception:
        ops.log.exception("album-row remove failed")
        ops.update_operation(op_id, "failed", error="Remove failed", error_code="REMOVE_FAILED")
        return fail("Remove failed", "REMOVE_FAILED", 500)


@ops.webmanager_bp.route("/album-row/rollback", methods=["POST"])
def run_album_row_rollback():
    """Re-create a removed album row from the engine's own manifest."""
    data = request.get_json(force=True, silent=True) or {}
    lib = g.lib
    folder = _manifest_dir(data.get("album_row_id"))
    if folder is None:
        return _error("album_row_id is not a valid engine id", "INVALID_ALBUM_ROW_ID")
    try:
        with open(os.path.join(folder, MANIFEST_NAME), encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return _error("no engine album-row record for this id", "ALBUM_ROW_NOT_FOUND", 404)
    if not isinstance(manifest, dict) or manifest.get("kind") != ALBUM_ROW_KIND:
        return _error("that record is not an album-row removal", "WRONG_RECORD_KIND")
    if manifest.get("status") != "applied":
        return _error("that album row was already restored", "ALBUM_ROW_ALREADY_RESTORED", 409)
    album_id = manifest.get("album_id")
    snapshot = manifest.get("snapshot")
    if not isinstance(album_id, int) or not isinstance(snapshot, dict):
        return _error("the engine album-row record is malformed", "ALBUM_ROW_RECORD_INVALID")
    if lib.get_album(album_id) is not None:
        return _error(f"album {album_id} exists again", "ALBUM_ID_OCCUPIED", 409)

    op_id, _fingerprint, early = ops._idempotency_precheck("album_row_rollback", data)
    if early is not None:
        return early
    try:
        with ops.mutation_lock:
            album = _restore_album_row(lib, album_id, snapshot)
            manifest["status"] = "restored"
            with open(os.path.join(folder, MANIFEST_NAME), "w", encoding="utf-8") as fh:
                json.dump(manifest, fh)
        result = {"success": True, "album_id": album.id, "restored": True}
        ops.update_operation(op_id, "succeeded", result=result)
        return jsonify({"operation_id": op_id, **result})
    except Exception:
        ops.log.exception("album-row rollback failed")
        ops.update_operation(op_id, "failed", error="Rollback failed", error_code="ROLLBACK_FAILED")
        return _error("Rollback failed", "ROLLBACK_FAILED", 500)


@ops.webmanager_bp.route("/import/rollback", methods=["POST"])
def run_import_rollback():
    """Undo a confirmed import: quarantine the imported files, drop the rows."""
    data = request.get_json(force=True, silent=True) or {}
    lib = g.lib
    try:
        album_id = int(data.get("album_id"))
    except (TypeError, ValueError):
        return _error("album_id must be an integer", "INVALID_ALBUM_ID")
    requested = data.get("item_ids")
    if not isinstance(requested, list) or not requested:
        return _error("item_ids must be a non-empty list", "INVALID_ITEM_IDS")
    if len(requested) > MAX_ITEMS:
        return _error(f"item_ids must hold at most {MAX_ITEMS} entries", "INVALID_ITEM_IDS")
    move_files = bool(data.get("move_files", True))
    album = lib.get_album(album_id)
    if album is None:
        return _error("album not found", "ALBUM_NOT_FOUND", 404)

    op_id, _fingerprint, early = ops._idempotency_precheck("import_rollback", data)
    if early is not None:
        return early

    def fail(message: str, code: str, status: int = 400):
        ops.update_operation(op_id, "failed", error=message, error_code=code)
        return _error(message, code, status)

    # Validate every item before changing anything.
    plan: List[Dict[str, Any]] = []
    seen = set()
    for raw in requested:
        try:
            item_id = int(raw)
        except (TypeError, ValueError):
            return fail("item_ids must be integers", "INVALID_ITEM_IDS")
        if item_id in seen:
            return fail(f"item {item_id} listed twice", "DUPLICATE_ITEM")
        seen.add(item_id)
        item = lib.get_item(item_id)
        if item is None:
            return fail(f"item {item_id} not found", "ITEM_NOT_FOUND", 404)
        if item.album_id != album_id:
            return fail(f"item {item_id} is not in album {album_id}", "ITEM_NOT_IN_ALBUM", 409)
        path = _fspath(item.path)
        if move_files:
            if not os.path.isfile(path):
                return fail(f"item {item_id}: file missing", "ITEM_PATH_INVALID")
            if not _inside_allowed(path):
                return fail(f"item {item_id}: file outside allowed roots", "ITEM_PATH_INVALID")
        plan.append({"item": item, "path": path, "snapshot": _snapshot(item)})

    quarantine_id = uuid.uuid4().hex
    qdir = os.path.join(_quarantine_root(), quarantine_id)
    done: List[Dict[str, Any]] = []
    album_snapshot = _album_snapshot(album)
    try:
        with ops.mutation_lock:
            os.makedirs(os.path.join(qdir, "files"))
            for index, step in enumerate(plan):
                record: Dict[str, Any] = {"item_id": step["snapshot"].get("id"),
                                          "original_path": step["path"],
                                          "quarantine_path": "", "snapshot": step["snapshot"],
                                          "row_removed": False}
                if move_files:
                    qpath = os.path.join(qdir, "files", f"{index:03d}_{os.path.basename(step['path'])}")
                    shutil.move(step["path"], qpath)
                    record["quarantine_path"] = qpath
                done.append(record)
                step["item"].remove(delete=False, with_album=False)
                record["row_removed"] = True
            album.remove(delete=False, with_items=False)
            with open(os.path.join(qdir, MANIFEST_NAME), "w", encoding="utf-8") as fh:
                json.dump({"kind": IMPORT_ROLLBACK_KIND, "album_id": album_id,
                           "album_snapshot": album_snapshot, "items": done,
                           "files_moved": move_files, "status": "applied"}, fh)
        result = {"success": True, "quarantine_id": quarantine_id, "album_id": album_id,
                  "removed": [{"item_id": r["item_id"], "original_path": r["original_path"],
                               "quarantine_path": r["quarantine_path"]} for r in done],
                  "files_moved": move_files}
        ops.update_operation(op_id, "succeeded", result=result)
        return jsonify({"operation_id": op_id, **result})
    except Exception:
        ops.log.exception("import rollback failed; restoring")
        try:
            with ops.mutation_lock:
                for record in reversed(done):
                    if os.path.exists(record.get("quarantine_path") or "") \
                            and not os.path.exists(record["original_path"]):
                        shutil.move(record["quarantine_path"], record["original_path"])
                    if record.get("row_removed") and lib.get_item(record["item_id"]) is None:
                        _readd(lib, record["snapshot"], record["original_path"])
                if lib.get_album(album_id) is None:
                    _restore_album_row(lib, album_id, album_snapshot)
        except Exception:
            ops.log.exception("import rollback restore failed")
        ops.update_operation(op_id, "failed", error="Rollback failed", error_code="ROLLBACK_FAILED")
        return fail("Rollback failed", "ROLLBACK_FAILED", 500)


@ops.webmanager_bp.route("/import/rollback/undo", methods=["POST"])
def run_import_rollback_undo():
    """Put an undone import back: the files where they were, the rows back."""
    data = request.get_json(force=True, silent=True) or {}
    lib = g.lib
    folder = _manifest_dir(data.get("quarantine_id"))
    if folder is None:
        return _error("quarantine_id is not a valid engine quarantine id", "INVALID_QUARANTINE_ID")
    try:
        with open(os.path.join(folder, MANIFEST_NAME), encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return _error("no engine import record for this id", "IMPORT_RECORD_NOT_FOUND", 404)
    if not isinstance(manifest, dict) or manifest.get("kind") != IMPORT_ROLLBACK_KIND:
        return _error("that record is not an import rollback", "WRONG_RECORD_KIND")
    if manifest.get("status") != "applied":
        return _error("that import was already undone", "IMPORT_ALREADY_UNDONE", 409)
    records = manifest.get("items") or []
    album_id = manifest.get("album_id")
    if not isinstance(album_id, int):
        return _error("the engine import record is malformed", "IMPORT_RECORD_INVALID")
    if lib.get_album(album_id) is not None:
        return _error(f"album id {album_id} is taken again", "ALBUM_ID_OCCUPIED", 409)
    for record in records:
        original = _fspath(record.get("original_path"))
        qpath = _fspath(record.get("quarantine_path"))
        if not original or not _inside_allowed(original):
            return _error("recorded path is invalid", "RECORD_PATH_INVALID")
        if qpath and not qpath.startswith(folder + os.sep):
            return _error("recorded path is invalid", "RECORD_PATH_INVALID")
        if qpath and os.path.exists(original):
            return _error(f"{original} is occupied", "ORIGINAL_PATH_OCCUPIED", 409)

    op_id, _fingerprint, early = ops._idempotency_precheck("import_rollback_undo", data)
    if early is not None:
        return early
    restored = []
    try:
        with ops.mutation_lock:
            album = _restore_album_row(lib, album_id, manifest.get("album_snapshot") or {})
            for record in records:
                original = _fspath(record["original_path"])
                qpath = _fspath(record.get("quarantine_path"))
                if qpath and os.path.exists(qpath):
                    os.makedirs(os.path.dirname(original), exist_ok=True)
                    shutil.move(qpath, original)
                item = _readd(lib, record.get("snapshot") or {}, original)
                restored.append({"old_item_id": record.get("item_id"), "new_item_id": item.id,
                                 "path": original, "album_id": item.album_id})
            manifest["status"] = "undone"
            with open(os.path.join(folder, MANIFEST_NAME), "w", encoding="utf-8") as fh:
                json.dump(manifest, fh)
        result = {"success": True, "restored": restored, "album_id": album.id}
        ops.update_operation(op_id, "succeeded", result=result)
        return jsonify({"operation_id": op_id, **result})
    except Exception:
        ops.log.exception("import rollback undo failed")
        ops.update_operation(op_id, "failed", error="Undo failed", error_code="ROLLBACK_FAILED")
        return _error("Undo failed", "ROLLBACK_FAILED", 500)
