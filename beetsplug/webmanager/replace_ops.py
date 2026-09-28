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
    The engine records both items' snapshots in a manifest inside its own
    quarantine folder and returns the folder's id (``quarantine_id``).

POST /webmanager/replace-item-file/rollback
    {"quarantine_id": "<32 hex>"}
    Reads the engine's own manifest -- never paths or snapshots supplied by
    the caller -- puts the replacement file back at the source's old path as
    a re-created singleton row, restores the quarantined original to the
    target's old path, and restores the target row.

Both run under the shared mutation lock and the idempotency registry. The
Web Manager decides *whether* to replace (fingerprint proof, a reviewed and
approved transaction); this module only performs the change inside Beets.
"""

import json
import os
import re
import shutil
import uuid
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
_QUARANTINE_ID = re.compile(r"[0-9a-f]{32}")
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


def _inside_allowed(path: str) -> bool:
    return ops.is_strict_descendant(path, ops.get_allowed_roots())


def _error(message: str, code: str, status: int = 400):
    return jsonify({"error": message, "error_code": code}), status


def _load_pair(lib, data: Dict[str, Any]) -> Tuple[Optional[Item], Optional[Item], Optional[Tuple[str, str, int]]]:
    try:
        target_id = int(data.get("target_item_id"))
        source_id = int(data.get("source_item_id"))
    except (TypeError, ValueError):
        return None, None, ("target_item_id and source_item_id must be integers", "INVALID_IDS", 400)
    if target_id == source_id:
        return None, None, ("target and source must be different items", "SAME_ITEM", 400)
    target, source = lib.get_item(target_id), lib.get_item(source_id)
    if target is None or source is None:
        return None, None, ("target or source item not found", "ITEM_NOT_FOUND", 404)
    if not target.album_id:
        return None, None, ("target item is not attached to an album slot", "TARGET_NOT_IN_ALBUM", 400)
    source_path = _fspath(source.path)
    if not os.path.isfile(source_path) or not _inside_allowed(source_path):
        return None, None, ("source file missing or outside allowed roots", "SOURCE_PATH_INVALID", 400)
    if not _inside_allowed(_fspath(target.path)):
        return None, None, ("target path outside allowed roots", "TARGET_PATH_INVALID", 400)
    return target, source, None


def _manifest_dir(quarantine_id: str) -> Optional[str]:
    """The engine's folder for one replacement, or None for a malformed id."""
    if not isinstance(quarantine_id, str) or not _QUARANTINE_ID.fullmatch(quarantine_id):
        return None
    root = _quarantine_root()
    folder = os.path.normpath(os.path.join(root, quarantine_id))
    if not folder.startswith(root + os.sep):
        return None
    return folder


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
        message, code, status = err
        ops.update_operation(op_id, "failed", error=message, error_code=code)
        return _error(message, code, status)

    target_snapshot, source_snapshot = _snapshot(target), _snapshot(source)
    old_target_path = _fspath(target.path)
    source_path = _fspath(source.path)
    quarantine_id = uuid.uuid4().hex
    qdir = os.path.join(_quarantine_root(), quarantine_id)
    quarantine_path = ""
    try:
        with ops.mutation_lock:
            os.makedirs(qdir)
            if os.path.exists(old_target_path):
                quarantine_path = os.path.join(qdir, os.path.basename(old_target_path))
                shutil.move(old_target_path, quarantine_path)

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

            manifest = {
                "target_item_id": target.id,
                "old_target_path": old_target_path,
                "new_target_path": new_target_path,
                "source_path": source_path,
                "quarantine_path": quarantine_path,
                "target_snapshot": target_snapshot,
                "source_snapshot": source_snapshot,
            }
            with open(os.path.join(qdir, MANIFEST_NAME), "w", encoding="utf-8") as fh:
                json.dump(manifest, fh)

        result = {
            "success": True,
            "target_item_id": target.id,
            "removed_source_item_id": source_snapshot.get("id"),
            "old_target_path": old_target_path,
            "new_target_path": new_target_path,
            "quarantine_id": quarantine_id,
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
                if quarantine_path and os.path.exists(quarantine_path) and not os.path.exists(old_target_path):
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
    folder = _manifest_dir(data.get("quarantine_id"))
    if folder is None:
        return _error("quarantine_id is not a valid engine replacement id", "INVALID_QUARANTINE_ID")
    try:
        with open(os.path.join(folder, MANIFEST_NAME), encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return _error("no engine replacement record for this id", "REPLACEMENT_NOT_FOUND", 404)

    target_snapshot = manifest.get("target_snapshot") or {}
    source_snapshot = manifest.get("source_snapshot") or {}
    old_target_path = _fspath(manifest.get("old_target_path"))
    old_source_path = _fspath(manifest.get("source_path"))
    quarantine_path = _fspath(manifest.get("quarantine_path"))
    target = lib.get_item(manifest.get("target_item_id"))
    if target is None:
        return _error("target item not found", "ITEM_NOT_FOUND", 404)
    for path in (old_target_path, old_source_path):
        if not path or not _inside_allowed(path):
            return _error("recorded path is outside allowed roots", "SNAPSHOT_PATH_INVALID")
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
