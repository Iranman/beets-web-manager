"""Remove tracked items from the library, keeping their files in quarantine.

POST /webmanager/quarantine-remove-items
    {"items": [{"item_id": N, "sha256": "<64 hex>"}, ...]}
    For every item: the file is moved into an engine-owned quarantine folder
    (never deleted) and the library row is removed through Beets. Nothing is
    changed unless every item still exists, its file is inside the allowed
    roots and its content still hashes to the reviewed SHA-256. The engine
    records snapshots in a manifest and returns the folder's id.

POST /webmanager/quarantine-remove-items/rollback
    {"quarantine_id": "<32 hex>"}
    Reads the engine's own manifest, moves each file back to where it was and
    re-adds its library row (a new id; back into its album row when that
    album still exists).

The Web Manager decides *which* copies may go (duplicate proof, keeper
policy, album-slot gate, a reviewed and approved transaction); this module
only performs the change inside Beets.
"""

import json
import os
import shutil
import uuid
from typing import Any, Dict, List

from beets.library import Item
from flask import g, jsonify, request

from . import operations as ops
from .replace_ops import (
    MANIFEST_NAME,
    _SHA256,
    _error,
    _fspath,
    _inside_allowed,
    _manifest_dir,
    _quarantine_root,
    _sha256_file,
    _snapshot,
)

MAX_ITEMS = 200


def _readd(lib, snapshot: Dict[str, Any], path: str) -> Item:
    fields = {k: v for k, v in snapshot.items() if k not in ("id", "path")}
    item = Item(**fields)
    item.path = os.fsencode(path)
    album_id = snapshot.get("album_id")
    item.album_id = album_id if album_id and lib.get_album(album_id) is not None else None
    lib.add(item)
    return item


@ops.webmanager_bp.route("/quarantine-remove-items", methods=["POST"])
def run_quarantine_remove_items():
    data = request.get_json(force=True, silent=True) or {}
    lib = g.lib
    op_id, _fingerprint, early = ops._idempotency_precheck("quarantine_remove_items", data)
    if early is not None:
        return early

    def fail(message: str, code: str, status: int = 400):
        ops.update_operation(op_id, "failed", error=message, error_code=code)
        return _error(message, code, status)

    requested = data.get("items")
    if not isinstance(requested, list) or not requested or len(requested) > MAX_ITEMS:
        return fail(f"items must be a non-empty list of at most {MAX_ITEMS}", "INVALID_ITEMS")

    # Validate everything before changing anything.
    plan: List[Dict[str, Any]] = []
    seen = set()
    for entry in requested:
        try:
            item_id = int((entry or {}).get("item_id"))
        except (TypeError, ValueError, AttributeError):
            return fail("item_id must be an integer", "INVALID_IDS")
        sha = (entry or {}).get("sha256")
        if not (isinstance(sha, str) and _SHA256.fullmatch(sha)):
            return fail(f"item {item_id}: sha256 must be a SHA-256 hex digest", "INVALID_SHA256")
        if item_id in seen:
            return fail(f"item {item_id} listed twice", "DUPLICATE_ITEM")
        seen.add(item_id)
        item = lib.get_item(item_id)
        if item is None:
            return fail(f"item {item_id} not found", "ITEM_NOT_FOUND", 404)
        path = _fspath(item.path)
        if not os.path.isfile(path) or not _inside_allowed(path):
            return fail(f"item {item_id}: file missing or outside allowed roots", "ITEM_PATH_INVALID")
        if _sha256_file(path) != sha:
            return fail(f"item {item_id}: file changed since it was reviewed", "ITEM_CHANGED", 409)
        plan.append({"item": item, "path": path, "snapshot": _snapshot(item)})

    quarantine_id = uuid.uuid4().hex
    qdir = os.path.join(_quarantine_root(), quarantine_id)
    done: List[Dict[str, Any]] = []
    try:
        with ops.mutation_lock:
            os.makedirs(os.path.join(qdir, "files"))
            for index, step in enumerate(plan):
                qpath = os.path.join(qdir, "files", f"{index:03d}_{os.path.basename(step['path'])}")
                shutil.move(step["path"], qpath)
                record = {"item_id": step["snapshot"].get("id"), "original_path": step["path"],
                          "quarantine_path": qpath, "snapshot": step["snapshot"], "row_removed": False}
                done.append(record)
                step["item"].remove(delete=False, with_album=True)
                record["row_removed"] = True
            with open(os.path.join(qdir, MANIFEST_NAME), "w", encoding="utf-8") as fh:
                json.dump({"kind": "quarantine_remove_items", "items": done}, fh)
        result = {
            "success": True,
            "quarantine_id": quarantine_id,
            "removed": [{"item_id": r["item_id"], "original_path": r["original_path"],
                         "quarantine_path": r["quarantine_path"]} for r in done],
        }
        ops.update_operation(op_id, "succeeded", result=result)
        return jsonify({"operation_id": op_id, **result})
    except Exception:
        ops.log.exception("quarantine-remove-items failed; restoring")
        try:
            with ops.mutation_lock:
                for record in reversed(done):
                    if os.path.exists(record["quarantine_path"]) and not os.path.exists(record["original_path"]):
                        shutil.move(record["quarantine_path"], record["original_path"])
                    if record["row_removed"] and lib.get_item(record["item_id"]) is None:
                        _readd(lib, record["snapshot"], record["original_path"])
        except Exception:
            ops.log.exception("quarantine-remove-items restore failed")
        ops.update_operation(op_id, "failed", error="Remove failed", error_code="REMOVE_FAILED")
        return _error("Remove failed", "REMOVE_FAILED", 500)


@ops.webmanager_bp.route("/quarantine-remove-items/rollback", methods=["POST"])
def run_quarantine_remove_items_rollback():
    data = request.get_json(force=True, silent=True) or {}
    lib = g.lib
    folder = _manifest_dir(data.get("quarantine_id"))
    if folder is None:
        return _error("quarantine_id is not a valid engine quarantine id", "INVALID_QUARANTINE_ID")
    try:
        with open(os.path.join(folder, MANIFEST_NAME), encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return _error("no engine removal record for this id", "REMOVAL_NOT_FOUND", 404)
    if manifest.get("kind") != "quarantine_remove_items":
        return _error("that quarantine record is not an item removal", "WRONG_RECORD_KIND")
    records = manifest.get("items") or []
    for record in records:
        original = _fspath(record.get("original_path"))
        qpath = _fspath(record.get("quarantine_path"))
        if not original or not _inside_allowed(original) or not qpath.startswith(folder + os.sep):
            return _error("recorded path is invalid", "RECORD_PATH_INVALID")
        if os.path.exists(original):
            return _error(f"{original} is occupied", "ORIGINAL_PATH_OCCUPIED", 409)

    op_id, _fingerprint, early = ops._idempotency_precheck("quarantine_remove_items_rollback", data)
    if early is not None:
        return early
    restored = []
    try:
        with ops.mutation_lock:
            for record in records:
                original = _fspath(record["original_path"])
                os.makedirs(os.path.dirname(original), exist_ok=True)
                shutil.move(_fspath(record["quarantine_path"]), original)
                item = _readd(lib, record.get("snapshot") or {}, original)
                restored.append({"old_item_id": record.get("item_id"), "new_item_id": item.id,
                                 "path": original, "album_id": item.album_id})
        result = {"success": True, "restored": restored}
        ops.update_operation(op_id, "succeeded", result=result)
        return jsonify({"operation_id": op_id, **result})
    except Exception:
        ops.log.exception("quarantine-remove-items rollback failed")
        ops.update_operation(op_id, "failed", error="Rollback failed", error_code="ROLLBACK_FAILED")
        return _error("Rollback failed", "ROLLBACK_FAILED", 500)
