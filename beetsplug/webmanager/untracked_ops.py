"""Untracked-file recovery and cleanup inside Beets (ARCH-021).

POST /webmanager/untracked/attach
    {"path", "sha256", "album_id" | null,
     "expected": {"mb_trackid", "mb_albumid", "disc", "track"}}
    Adds an untracked audio file to the library as an item -- into an album
    row's free (disc, track) slot, or as a singleton when album_id is null.
    Beets reads the file's own tags; they must match ``expected`` exactly
    (the identity the Web Manager proved). Nothing is written to the file
    and nothing is moved. Rollback removes the row again (file untouched).

POST /webmanager/untracked/quarantine
    {"files": [{"path", "sha256"}, ...]}
    Moves untracked files whose content still hashes to the reviewed SHA-256
    into the engine quarantine (never deleted). Refuses the whole request if
    any file is tracked, missing, outside the allowed roots or changed.
    Rollback moves every file back; it refuses (never half-succeeds) when a
    quarantined file is missing or an original path is occupied.

POST /webmanager/untracked/rollback  {"record_id": "<32 hex>"}
GET  /webmanager/untracked/<record_id>

Each operation's record id is derived from its Idempotency-Key, so a retry
-- even after a Beets restart -- replays the recorded result.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from typing import Any, Dict, List, Optional

from beets.dbcore.query import PathQuery
from beets.library import Item
from flask import g, jsonify, request

from . import operations as ops
from .engine_common import _SHA256, _error, _fspath, _quarantine_root, _sha256_file

_RECORD_ID = re.compile(r"[0-9a-f]{32}")


def record_id_for(kind: str, key: str) -> str:
    return hashlib.sha256(f"untracked-{kind}|{key}".encode("utf-8")).hexdigest()[:32]


def _records_root() -> str:
    return os.path.join(_quarantine_root(), "untracked")


def _record_dir(record_id: Any) -> Optional[str]:
    if not isinstance(record_id, str) or not _RECORD_ID.fullmatch(record_id):
        return None
    root = os.path.abspath(_records_root())
    folder = os.path.normpath(os.path.join(root, record_id))
    return folder if folder.startswith(root + os.sep) else None


def _load(record_id: str) -> Optional[Dict[str, Any]]:
    folder = _record_dir(record_id)
    try:
        with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as fh:  # type: ignore[arg-type]
            return json.load(fh)
    except (OSError, ValueError, TypeError):
        return None


def _save(manifest: Dict[str, Any]) -> None:
    folder = _record_dir(manifest["record_id"])
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, "manifest.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    os.replace(tmp, os.path.join(folder, "manifest.json"))


def _safe_audio_path(raw: Any) -> Optional[str]:
    """The real path of a caller-named file, only if it lies strictly inside
    an allowed root (resolved; symlinks followed before the check)."""
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return None
    real = os.path.realpath(os.path.abspath(raw))
    for root in ops.get_allowed_roots():
        base = os.path.realpath(os.path.abspath(root))
        if real.startswith(base + os.sep):
            return real
    return None


def _is_tracked(lib, path: str) -> bool:
    return any(True for _ in lib.items(PathQuery("path", os.fsencode(path))))


def _s(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _replay(kind: str):
    key = request.headers.get("Idempotency-Key") or ""
    if not key:
        return None, None, _error("Idempotency-Key header is required", "IDEMPOTENCY_KEY_REQUIRED")
    record_id = record_id_for(kind, key)
    recorded = _load(record_id)
    if recorded and recorded.get("status") in ("applied", "rolled_back"):
        return record_id, recorded, jsonify({"operation_id": key, "replayed": True,
                                             "record_status": recorded.get("status"),
                                             **(recorded.get("result") or {})})
    return record_id, recorded, None


@ops.webmanager_bp.route("/untracked/<string:record_id>", methods=["GET"])
def get_untracked_record(record_id):
    manifest = _load(record_id) if _record_dir(record_id) else None
    if manifest is None:
        return _error("no record", "RECORD_NOT_FOUND", 404)
    return jsonify({"record_id": record_id, "kind": manifest.get("kind"), "status": manifest.get("status"),
                    "result": manifest.get("result")})


@ops.webmanager_bp.route("/untracked/attach", methods=["POST"])
def run_untracked_attach():
    data = request.get_json(force=True, silent=True) or {}
    lib = g.lib
    record_id, _recorded, early = _replay("attach")
    if early is not None:
        return early
    op_id, _fp, early = ops._idempotency_precheck("untracked_attach", data)
    if early is not None:
        return early

    def fail(message: str, code: str, status: int = 400):
        ops.update_operation(op_id, "failed", error=message, error_code=code)
        return _error(message, code, status)

    path = _safe_audio_path(data.get("path"))
    if path is None:
        return fail("path is outside the allowed roots", "PATH_INVALID")
    if not os.path.isfile(path):
        return fail("file not found", "FILE_NOT_FOUND", 404)
    sha = data.get("sha256")
    if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
        return fail("sha256 required", "INVALID_SHA256")
    if _is_tracked(lib, path):
        return fail("file is already tracked", "FILE_IS_TRACKED", 409)
    if _sha256_file(path) != sha:
        return fail("file changed since review", "CONTENT_DRIFT", 409)
    expected = data.get("expected") or {}
    album = None
    if data.get("album_id") is not None:
        try:
            album = lib.get_album(int(data["album_id"]))
        except (TypeError, ValueError):
            album = None
        if album is None:
            return fail("album not found", "ALBUM_NOT_FOUND", 404)
    item = Item.from_path(path)
    found = {"mb_trackid": _s(item.mb_trackid).lower(), "mb_albumid": _s(item.mb_albumid).lower(),
             "disc": int(item.disc or 1), "track": int(item.track or 0)}
    wanted = {"mb_trackid": _s(expected.get("mb_trackid")).lower(), "mb_albumid": _s(expected.get("mb_albumid")).lower(),
              "disc": int(expected.get("disc") or 1), "track": int(expected.get("track") or 0)}
    if not wanted["mb_trackid"] or found != wanted:
        return fail("the file's tags do not match the approved identity", "IDENTITY_MISMATCH", 409)
    if album is not None:
        if _s(album.mb_albumid).lower() != wanted["mb_albumid"]:
            return fail("the album row is a different release", "EDITION_DIFFERS", 409)
        taken = {(int(i.disc or 1), int(i.track or 0)) for i in album.items()}
        if (wanted["disc"], wanted["track"]) in taken:
            return fail("that album slot is already filled", "SLOT_OCCUPIED", 409)

    manifest = {"record_id": record_id, "kind": "attach", "operation_id": op_id, "status": "applying",
                "path": path, "sha256": sha, "album_id": album.id if album else None}
    try:
        with ops.mutation_lock:
            _save(manifest)
            if album is not None:
                item.album_id = album.id
            lib.add(item)          # a library row only: no tag write, no move
            fresh = lib.get_item(item.id)
            if fresh is None or _fspath(fresh.path) != path or _s(fresh.mb_trackid).lower() != wanted["mb_trackid"]:
                raise RuntimeError("attach postcondition failed")
            result = {"success": True, "record_id": record_id, "item_id": item.id,
                      "album_id": item.album_id, "path": path}
            manifest.update(status="applied", item_id=item.id, result=result)
            _save(manifest)
        ops.update_operation(op_id, "succeeded", result=result)
        return jsonify({"operation_id": op_id, **result})
    except Exception:
        ops.log.exception("untracked attach failed; removing the row again")
        try:
            with ops.mutation_lock:
                if item.id:  # remove through the object in hand, not a fresh lookup
                    item.remove(delete=False, with_album=False)
                manifest.update(status="compensated")
                _save(manifest)
        except Exception:
            ops.log.exception("untracked attach compensation failed")
            manifest.update(status="recovery_required")
            try:
                _save(manifest)
            except Exception:
                pass
        ops.update_operation(op_id, "failed", error="Attach failed", error_code="ATTACH_FAILED")
        return _error("Attach failed; nothing was kept", "ATTACH_FAILED", 500)


@ops.webmanager_bp.route("/untracked/quarantine", methods=["POST"])
def run_untracked_quarantine():
    data = request.get_json(force=True, silent=True) or {}
    lib = g.lib
    record_id, _recorded, early = _replay("quarantine")
    if early is not None:
        return early
    op_id, _fp, early = ops._idempotency_precheck("untracked_quarantine", data)
    if early is not None:
        return early

    def fail(message: str, code: str, status: int = 400):
        ops.update_operation(op_id, "failed", error=message, error_code=code)
        return _error(message, code, status)

    files = data.get("files")
    if not isinstance(files, list) or not files or len(files) > 200:
        return fail("files must be a non-empty list of at most 200", "INVALID_FILES")
    plan: List[Dict[str, Any]] = []
    seen = set()
    for entry in files:
        path = _safe_audio_path((entry or {}).get("path") if isinstance(entry, dict) else None)
        sha = (entry or {}).get("sha256") if isinstance(entry, dict) else None
        if path is None:
            return fail("a path is outside the allowed roots", "PATH_INVALID")
        if path in seen:
            return fail("a path is listed twice", "DUPLICATE_PATH")
        seen.add(path)
        if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
            return fail("sha256 required for every file", "INVALID_SHA256")
        if not os.path.isfile(path):
            return fail(f"{path} not found", "FILE_NOT_FOUND", 404)
        if _is_tracked(lib, path):
            return fail(f"{path} is tracked", "FILE_IS_TRACKED", 409)
        if _sha256_file(path) != sha:
            return fail(f"{path} changed since review", "CONTENT_DRIFT", 409)
        plan.append({"path": path, "sha256": sha})

    folder = _record_dir(record_id)
    manifest = {"record_id": record_id, "kind": "quarantine", "operation_id": op_id, "status": "applying", "files": []}
    try:
        with ops.mutation_lock:
            _save(manifest)
            os.makedirs(os.path.join(folder, "files"), exist_ok=True)
            for index, step in enumerate(plan):
                qpath = os.path.join(folder, "files", f"{index:03d}_{os.path.basename(step['path'])}")
                shutil.move(step["path"], qpath)
                manifest["files"].append({"original_path": step["path"], "quarantine_path": qpath,
                                          "sha256": step["sha256"]})
                _save(manifest)
            result = {"success": True, "record_id": record_id, "quarantined": list(manifest["files"])}
            manifest.update(status="applied", result=result)
            _save(manifest)
        ops.update_operation(op_id, "succeeded", result=result)
        return jsonify({"operation_id": op_id, **result})
    except Exception:
        ops.log.exception("untracked quarantine failed; restoring")
        try:
            with ops.mutation_lock:
                for rec in reversed(manifest["files"]):
                    if os.path.exists(rec["quarantine_path"]) and not os.path.exists(rec["original_path"]):
                        shutil.move(rec["quarantine_path"], rec["original_path"])
                manifest.update(status="compensated")
                _save(manifest)
        except Exception:
            ops.log.exception("untracked quarantine compensation failed")
        ops.update_operation(op_id, "failed", error="Quarantine failed", error_code="QUARANTINE_FAILED")
        return _error("Quarantine failed; files were restored", "QUARANTINE_FAILED", 500)


@ops.webmanager_bp.route("/untracked/rollback", methods=["POST"])
def run_untracked_rollback():
    data = request.get_json(force=True, silent=True) or {}
    lib = g.lib
    record_id = data.get("record_id")
    if _record_dir(record_id) is None:
        return _error("record_id is not a valid engine record id", "INVALID_RECORD_ID")
    manifest = _load(record_id)
    if manifest is None:
        return _error("no record for this id", "RECORD_NOT_FOUND", 404)
    if manifest.get("status") == "rolled_back":
        return jsonify({"replayed": True, **(manifest.get("rollback_result") or {})})
    if manifest.get("status") != "applied":
        return _error(f"record is {manifest.get('status')}, not applied", "NOT_APPLIED", 409)

    if manifest["kind"] == "attach":
        item = lib.get_item(int(manifest["item_id"]))
        if item is None or _fspath(item.path) != manifest["path"]:
            return _error("the attached item changed or is gone", "ITEM_DRIFT", 409)
    else:
        for rec in manifest["files"]:
            if not os.path.isfile(rec["quarantine_path"]):
                return _error(f"quarantined file missing: {rec['quarantine_path']}", "QUARANTINE_FILE_MISSING", 409)
            if os.path.exists(rec["original_path"]):
                return _error(f"{rec['original_path']} is occupied", "ORIGINAL_PATH_OCCUPIED", 409)

    op_id, _fp, early = ops._idempotency_precheck("untracked_rollback", data)
    if early is not None:
        return early
    restored: List[str] = []
    try:
        with ops.mutation_lock:
            if manifest["kind"] == "attach":
                lib.get_item(int(manifest["item_id"])).remove(delete=False, with_album=False)
                result = {"success": True, "record_id": record_id, "removed_item_id": manifest["item_id"],
                          "path": manifest["path"], "file_untouched": os.path.isfile(manifest["path"])}
            else:
                for rec in manifest["files"]:
                    os.makedirs(os.path.dirname(rec["original_path"]), exist_ok=True)
                    shutil.move(rec["quarantine_path"], rec["original_path"])
                    restored.append(rec["original_path"])
                result = {"success": True, "record_id": record_id, "restored": restored}
            manifest.update(status="rolled_back", rollback_result=result)
            _save(manifest)
        ops.update_operation(op_id, "succeeded", result=result)
        return jsonify({"operation_id": op_id, **result})
    except Exception:
        ops.log.exception("untracked rollback failed")
        try:
            with ops.mutation_lock:
                by_orig = {r["original_path"]: r for r in manifest.get("files") or []}
                for orig in restored:
                    shutil.move(orig, by_orig[orig]["quarantine_path"])
        except Exception:
            ops.log.exception("untracked rollback compensation failed")
            manifest.update(status="recovery_required")
            _save(manifest)
        ops.update_operation(op_id, "failed", error="Rollback failed", error_code="ROLLBACK_FAILED")
        return _error("Rollback failed; the applied state was kept", "ROLLBACK_FAILED", 500)
