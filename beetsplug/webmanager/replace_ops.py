"""Replace an album slot's audio file with another tracked copy of the same recording.

POST /webmanager/replace-item-file
    {"target_item_id": T, "source_item_id": S}
    The target item keeps its identity: album membership, Recording ID,
    Release ID, Release Group ID, disc/track and every other tag. Only its
    audio file changes: the old file moves into an engine-owned quarantine
    folder (never deleted), the source file becomes the target item's file,
    the target's tags are written into it, and Beets moves it to its
    canonical path (so a FLAC gets a .flac name). The source item's library
    row is then removed -- its file now belongs to the target.

POST /webmanager/replace-item-file/rollback
    {"target_item_id": T, "target_snapshot": {...}, "source_snapshot": {...},
     "quarantine_path": "..."}
    Puts the replacement file back at the source's old path as a re-created
    singleton row, restores the quarantined original to the target's old
    path, and restores the target row.

Both run under the shared mutation lock and the idempotency registry. The
Web Manager decides *whether* to replace (fingerprint proof, a reviewed and
approved transaction); this module only performs the change inside Beets.
"""

import os
import shutil
from typing import Any, Dict, Optional, Tuple

from beets import config as beets_config
from beets.library import Item
from flask import g, jsonify, request

from . import operations as ops

# Audio properties that describe the file itself; everything else on the
# target row is identity/metadata that must survive the replacement.
AUDIO_PROPERTY_FIELDS = frozenset({
    "length", "bitrate", "bitrate_mode", "encoder_info", "encoder_settings",
    "format", "samplerate", "bitdepth", "channels",
})
_NOT_RESTORED = frozenset({"id", "path", "mtime"}) | AUDIO_PROPERTY_FIELDS


_QUARANTINE_ROOT_OVERRIDE: Optional[str] = None


def set_quarantine_root(root: Optional[str]) -> None:
    """Override the engine quarantine folder (tests only)."""
    global _QUARANTINE_ROOT_OVERRIDE
    _QUARANTINE_ROOT_OVERRIDE = root


def _quarantine_root() -> str:
    if _QUARANTINE_ROOT_OVERRIDE:
        return _QUARANTINE_ROOT_OVERRIDE
    return os.path.join(beets_config.config_dir(), "webmanager-quarantine")


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


def _inside_allowed(path: str) -> bool:
    return ops.is_strict_descendant(path, ops.get_allowed_roots())


def _error(message: str, code: str, status: int = 400):
    return jsonify({"error": message, "error_code": code}), status


def _load_pair(lib, data: Dict[str, Any]) -> Tuple[Optional[Item], Optional[Item], Optional[tuple]]:
    try:
        target_id = int(data.get("target_item_id"))
        source_id = int(data.get("source_item_id"))
    except (TypeError, ValueError):
        return None, None, _error("target_item_id and source_item_id must be integers", "INVALID_IDS")
    if target_id == source_id:
        return None, None, _error("target and source must be different items", "SAME_ITEM")
    target, source = lib.get_item(target_id), lib.get_item(source_id)
    if target is None or source is None:
        return None, None, _error("target or source item not found", "ITEM_NOT_FOUND", 404)
    if not target.album_id:
        return None, None, _error("target item is not attached to an album slot", "TARGET_NOT_IN_ALBUM")
    source_path = _fspath(source.path)
    if not os.path.isfile(source_path) or not _inside_allowed(source_path):
        return None, None, _error("source file missing or outside allowed roots", "SOURCE_PATH_INVALID")
    target_path = _fspath(target.path)
    if not _inside_allowed(target_path):
        return None, None, _error("target path outside allowed roots", "TARGET_PATH_INVALID")
    return target, source, None


@ops.webmanager_bp.route("/replace-item-file", methods=["POST"])
def run_replace_item_file():
    data = request.get_json(force=True, silent=True) or {}
    lib = g.lib
    # Replay check first: after a successful replace the source row is gone,
    # so a retry must replay the stored result rather than re-validate.
    op_id, _fingerprint, early = ops._idempotency_precheck("replace_item_file", data)
    if early is not None:
        return early
    target, source, err = _load_pair(lib, data)
    if err is not None:
        body, status = err
        ops.update_operation(op_id, "failed", error=body.get_json()["error"], error_code=body.get_json()["error_code"])
        return err

    target_snapshot, source_snapshot = _snapshot(target), _snapshot(source)
    old_target_path = _fspath(target.path)
    source_path = _fspath(source.path)
    quarantine_path = ""
    moved_to_quarantine = False
    try:
        with ops.mutation_lock:
            if os.path.exists(old_target_path):
                qdir = os.path.join(_quarantine_root(), op_id)
                os.makedirs(qdir, exist_ok=True)
                quarantine_path = os.path.join(qdir, os.path.basename(old_target_path))
                shutil.move(old_target_path, quarantine_path)
                moved_to_quarantine = True

            # The target keeps every identity/metadata field; only the file
            # (and the audio properties read from it) change.
            keep = {k: v for k, v in target.items() if k not in _NOT_RESTORED}
            target.path = os.fsencode(source_path)
            target.read()
            target.update(keep)
            target.store()
            source.remove(delete=False, with_album=True)
            target.try_write()
            target.move()
            target.store()
            new_target_path = _fspath(target.path)

        result = {
            "success": True,
            "target_item_id": target.id,
            "removed_source_item_id": source_snapshot.get("id"),
            "old_target_path": old_target_path,
            "new_target_path": new_target_path,
            "quarantine_path": quarantine_path,
            "format": _jsonable(target.get("format")),
            "target_snapshot": target_snapshot,
            "source_snapshot": source_snapshot,
        }
        ops.update_operation(op_id, "succeeded", result=result)
        return jsonify({"operation_id": op_id, **result})
    except Exception:
        ops.log.exception("replace-item-file failed; restoring")
        try:
            with ops.mutation_lock:
                if moved_to_quarantine and not os.path.exists(old_target_path):
                    shutil.move(quarantine_path, old_target_path)
                restored = lib.get_item(target_snapshot.get("id"))
                if restored is not None:
                    restored.update({k: v for k, v in target_snapshot.items() if k not in ("id", "path")})
                    restored.path = os.fsencode(old_target_path)
                    restored.store()
        except Exception:
            ops.log.exception("replace-item-file restore failed")
        ops.update_operation(op_id, "failed", error="Replace failed", error_code="REPLACE_FAILED")
        return _error("Replace failed", "REPLACE_FAILED", 500)


@ops.webmanager_bp.route("/replace-item-file/rollback", methods=["POST"])
def run_replace_item_file_rollback():
    data = request.get_json(force=True, silent=True) or {}
    lib = g.lib
    try:
        target_id = int(data.get("target_item_id"))
    except (TypeError, ValueError):
        return _error("target_item_id must be an integer", "INVALID_IDS")
    target_snapshot = data.get("target_snapshot") or {}
    source_snapshot = data.get("source_snapshot") or {}
    quarantine_path = _fspath(data.get("quarantine_path"))
    if not isinstance(target_snapshot, dict) or not isinstance(source_snapshot, dict):
        return _error("snapshots must be objects", "INVALID_SNAPSHOT")
    old_target_path = _fspath(target_snapshot.get("path"))
    old_source_path = _fspath(source_snapshot.get("path"))
    target = lib.get_item(target_id)
    if target is None:
        return _error("target item not found", "ITEM_NOT_FOUND", 404)
    for path in (old_target_path, old_source_path):
        if not path or not _inside_allowed(path):
            return _error("snapshot path missing or outside allowed roots", "SNAPSHOT_PATH_INVALID")
    if quarantine_path and not quarantine_path.startswith(_quarantine_root() + os.sep):
        return _error("quarantine path is not an engine quarantine file", "QUARANTINE_PATH_INVALID")
    current_path = _fspath(target.path)
    if os.path.exists(old_source_path) and os.path.abspath(old_source_path) != os.path.abspath(current_path):
        return _error("the source's old path is occupied", "SOURCE_PATH_OCCUPIED", 409)

    op_id, _fingerprint, early = ops._idempotency_precheck("replace_item_file_rollback", data)
    if early is not None:
        return early
    try:
        with ops.mutation_lock:
            # 1. the replacement file goes back to where the source had it,
            #    as a re-created singleton row with its original tags.
            os.makedirs(os.path.dirname(old_source_path), exist_ok=True)
            shutil.move(current_path, old_source_path)
            fields = {k: v for k, v in source_snapshot.items() if k not in ("id", "path")}
            recreated = Item(**fields)
            recreated.path = os.fsencode(old_source_path)
            recreated.album_id = None
            lib.add(recreated)
            recreated.try_write()
            # 2. the original target file comes back from quarantine.
            if quarantine_path and os.path.exists(quarantine_path):
                os.makedirs(os.path.dirname(old_target_path), exist_ok=True)
                shutil.move(quarantine_path, old_target_path)
            target.update({k: v for k, v in target_snapshot.items() if k not in ("id", "path")})
            target.path = os.fsencode(old_target_path)
            target.store()
        result = {
            "success": True,
            "target_item_id": target.id,
            "restored_target_path": old_target_path,
            "recreated_source_item_id": recreated.id,
            "recreated_source_path": old_source_path,
        }
        ops.update_operation(op_id, "succeeded", result=result)
        return jsonify({"operation_id": op_id, **result})
    except Exception:
        ops.log.exception("replace-item-file rollback failed")
        ops.update_operation(op_id, "failed", error="Rollback failed", error_code="ROLLBACK_FAILED")
        return _error("Rollback failed", "ROLLBACK_FAILED", 500)
