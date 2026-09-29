"""Merge Beets album rows of one canonical album (ARCH-020).

POST /webmanager/album-row-merge
    {"target_album_id": T, "source_album_ids": [S, ...],
     "expected_release_group_id": RG, "expected_release_id": REL,
     "items": [{"item_id", "source_album_id", "sha256", "mb_trackid", "disc", "track"}, ...]}

An album-row merge is a library ownership change only: each listed item's
``album_id`` moves from its source row to the target row and nothing else is
written -- no tags, no file moves, no renames. It is refused unless:

* every album row carries the expected Release Group AND Release ID (one
  canonical album, one edition);
* ``items`` is exactly the set of items in the source rows, each still in
  its expected source row with unchanged Recording ID, disc, track and
  file content (SHA-256);
* no (disc, track) slot would be filled twice and every item has a track.

Postconditions (album_id and every identity field, path unchanged) are
verified before the emptied source rows are retired. Any failure restores
what was changed. The engine records a manifest whose id is derived from the
request's Idempotency-Key, so a retry -- even after a Beets restart -- replays
the recorded result instead of re-running.

POST /webmanager/album-row-merge/rollback  {"merge_id": "<32 hex>"}
    Re-creates retired source rows at their ORIGINAL album ids with their
    original metadata and moves every item back to its exact original row.
    Idempotent: a finished rollback replays its recorded result.

GET /webmanager/album-row-merge/<merge_id>
    The manifest status (for restart recovery on the Web Manager side).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional

from beets import config as beets_config
from flask import g, jsonify, request

from . import operations as ops
from .replace_ops import _SHA256, _error, _fspath, _inside_allowed, _jsonable, _sha256_file

_MERGE_ID = re.compile(r"[0-9a-f]{32}")
IDENTITY_FIELDS = ("mb_trackid", "mb_albumid", "mb_releasegroupid", "disc", "track", "path")
_MERGE_ROOT_OVERRIDE: Optional[str] = None


def set_merge_root(root: Optional[str]) -> None:
    """Override the manifest folder (tests only)."""
    global _MERGE_ROOT_OVERRIDE
    _MERGE_ROOT_OVERRIDE = root


def _merge_root() -> str:
    if _MERGE_ROOT_OVERRIDE:
        return os.path.abspath(_MERGE_ROOT_OVERRIDE)
    return os.path.abspath(os.path.join(beets_config.config_dir(), "webmanager-album-merges"))


def merge_id_for(key: str) -> str:
    return hashlib.sha256(("album-row-merge|" + key).encode("utf-8")).hexdigest()[:32]


def _manifest_path(merge_id: Any) -> Optional[str]:
    if not isinstance(merge_id, str) or not _MERGE_ID.fullmatch(merge_id):
        return None
    root = _merge_root()
    path = os.path.normpath(os.path.join(root, merge_id + ".json"))
    return path if path.startswith(root + os.sep) else None


def _load(merge_id: str) -> Optional[Dict[str, Any]]:
    path = _manifest_path(merge_id)
    try:
        with open(path, encoding="utf-8") as fh:  # type: ignore[arg-type]
            return json.load(fh)
    except (OSError, ValueError, TypeError):
        return None


def _save(manifest: Dict[str, Any]) -> None:
    os.makedirs(_merge_root(), exist_ok=True)
    path = _manifest_path(manifest["merge_id"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    os.replace(tmp, path)


def _s(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _album_snapshot(album) -> Dict[str, Any]:
    return {k: _jsonable(album.get(k)) for k in album.keys(computed=False)}


def _identity(item) -> Dict[str, Any]:
    return {k: (_fspath(item.path) if k == "path" else _jsonable(item.get(k))) for k in IDENTITY_FIELDS}


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


@ops.webmanager_bp.route("/album-row-merge/<string:merge_id>", methods=["GET"])
def get_album_row_merge(merge_id):
    manifest = _load(merge_id) if _manifest_path(merge_id) else None
    if manifest is None:
        return _error("no merge record", "MERGE_RECORD_NOT_FOUND", 404)
    return jsonify({"merge_id": merge_id, "status": manifest.get("status"), "result": manifest.get("result")})


@ops.webmanager_bp.route("/album-row-merge", methods=["POST"])
def run_album_row_merge():
    data = request.get_json(force=True, silent=True) or {}
    lib = g.lib
    key = request.headers.get("Idempotency-Key") or ""
    if not key:
        return _error("Idempotency-Key header is required", "IDEMPOTENCY_KEY_REQUIRED")
    merge_id = merge_id_for(key)
    recorded = _load(merge_id)
    if recorded and recorded.get("status") in ("applied", "rolled_back"):
        return jsonify({"operation_id": key, "replayed": True, "manifest_status": recorded.get("status"),
                        **(recorded.get("result") or {})})
    op_id, _fp, early = ops._idempotency_precheck("album_row_merge", data)
    if early is not None:
        return early

    def fail(message: str, code: str, status: int = 400):
        ops.update_operation(op_id, "failed", error=message, error_code=code)
        return _error(message, code, status)

    try:
        target_id = int(data.get("target_album_id"))
        source_ids = sorted({int(x) for x in data.get("source_album_ids") or []})
    except (TypeError, ValueError):
        return fail("album ids must be integers", "INVALID_IDS")
    expected_rg = _s(data.get("expected_release_group_id")).lower()
    expected_rel = _s(data.get("expected_release_id")).lower()
    if not source_ids or target_id in source_ids:
        return fail("need a target and distinct source album ids", "INVALID_IDS")
    if not expected_rg or not expected_rel:
        return fail("expected_release_group_id and expected_release_id are required", "IDENTITY_REQUIRED")

    albums = {}
    for aid in [target_id] + source_ids:
        album = lib.get_album(aid)
        if album is None:
            return fail(f"album {aid} not found", "ALBUM_NOT_FOUND", 404)
        if _s(album.get("mb_releasegroupid")).lower() != expected_rg:
            return fail(f"album {aid} is not in Release Group {expected_rg}", "RELEASE_GROUP_MISMATCH", 409)
        if _s(album.get("mb_albumid")).lower() != expected_rel:
            return fail(f"album {aid} is a different edition (Release ID)", "EDITION_DIFFERS", 409)
        albums[aid] = album

    requested = data.get("items")
    if not isinstance(requested, list) or not requested:
        return fail("items must be a non-empty list", "INVALID_ITEMS")
    by_id: Dict[int, Dict[str, Any]] = {}
    for entry in requested:
        try:
            by_id[int(entry["item_id"])] = entry
        except (TypeError, ValueError, KeyError):
            return fail("each item needs an integer item_id", "INVALID_ITEMS")
    in_sources = {it.id: it for sid in source_ids for it in albums[sid].items()}
    if set(by_id) != set(in_sources):
        return fail("items must be exactly the items of the source album rows", "SOURCE_NOT_FULLY_COVERED", 409)

    target_slots = {(int(it.disc or 1), int(it.track or 0)) for it in albums[target_id].items()}
    plan: List[Dict[str, Any]] = []
    for item_id, entry in sorted(by_id.items()):
        item = in_sources[item_id]
        if item.album_id != int(entry.get("source_album_id") or 0):
            return fail(f"item {item_id} is no longer in its expected source row", "ITEM_ALBUM_DRIFT", 409)
        if (_s(item.mb_trackid).lower() != _s(entry.get("mb_trackid")).lower()
                or int(item.disc or 1) != int(entry.get("disc") or 1) or int(item.track or 0) != int(entry.get("track") or 0)):
            return fail(f"item {item_id} identity changed since preview", "ITEM_IDENTITY_DRIFT", 409)
        if _s(item.mb_albumid).lower() not in ("", expected_rel):
            return fail(f"item {item_id} carries a different Release ID", "EDITION_DIFFERS", 409)
        slot = (int(item.disc or 1), int(item.track or 0))
        if not slot[1]:
            return fail(f"item {item_id} has no track position", "UNPOSITIONED_ITEM", 409)
        if slot in target_slots:
            return fail(f"slot {slot[0]}/{slot[1]} would be filled twice", "SLOT_OVERLAP", 409)
        target_slots.add(slot)
        path = _fspath(item.path)
        sha = entry.get("sha256")
        if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
            return fail(f"item {item_id}: sha256 required", "INVALID_SHA256")
        if not os.path.isfile(path) or not _inside_allowed(path):
            return fail(f"item {item_id} file missing or outside allowed roots", "ITEM_PATH_INVALID")
        if _sha256_file(path) != sha:
            return fail(f"item {item_id} file changed since preview", "ITEM_CONTENT_DRIFT", 409)
        plan.append({"item": item, "item_id": item_id, "source_album_id": item.album_id, "identity": _identity(item)})

    manifest = {
        "merge_id": merge_id, "operation_id": key, "status": "applying",
        "target_album_id": target_id, "source_album_ids": source_ids,
        "source_album_snapshots": {str(sid): _album_snapshot(albums[sid]) for sid in source_ids},
        "items": [{"item_id": p["item_id"], "source_album_id": p["source_album_id"], "identity": p["identity"]}
                  for p in plan],
        "retired_album_ids": [],
    }
    moved: List[Dict[str, Any]] = []
    try:
        with ops.mutation_lock:
            _save(manifest)
            for p in plan:
                p["item"].album_id = target_id
                p["item"].store()   # ownership only: no tag write, no file move
                moved.append(p)
            for p in plan:
                fresh = lib.get_item(p["item_id"])
                if fresh is None or fresh.album_id != target_id or _identity(fresh) != p["identity"]:
                    raise RuntimeError(f"postcondition failed for item {p['item_id']}")
            for sid in source_ids:
                if list(albums[sid].items()):
                    raise RuntimeError(f"source album {sid} still has items")
                albums[sid].remove(delete=False, with_items=False)
                manifest["retired_album_ids"].append(sid)
            result = {"success": True, "merge_id": merge_id, "target_album_id": target_id,
                      "retired_album_ids": list(manifest["retired_album_ids"]),
                      "moved_item_ids": [p["item_id"] for p in plan]}
            manifest.update(status="applied", result=result)
            _save(manifest)
        ops.update_operation(op_id, "succeeded", result=result)
        return jsonify({"operation_id": op_id, **result})
    except Exception:
        ops.log.exception("album-row-merge failed; restoring")
        try:
            with ops.mutation_lock:
                for sid in manifest["retired_album_ids"]:
                    if lib.get_album(sid) is None:
                        _restore_album_row(lib, sid, manifest["source_album_snapshots"][str(sid)])
                for p in moved:
                    item = lib.get_item(p["item_id"])
                    if item is not None and item.album_id != p["source_album_id"]:
                        item.album_id = p["source_album_id"]
                        item.store()
                manifest.update(status="compensated")
                _save(manifest)
        except Exception:
            ops.log.exception("album-row-merge compensation failed")
            manifest.update(status="recovery_required")
            try:
                _save(manifest)
            except Exception:
                pass
        ops.update_operation(op_id, "failed", error="Album merge failed", error_code="MERGE_FAILED")
        return _error("Album merge failed; the library was restored", "MERGE_FAILED", 500)


@ops.webmanager_bp.route("/album-row-merge/rollback", methods=["POST"])
def run_album_row_merge_rollback():
    data = request.get_json(force=True, silent=True) or {}
    lib = g.lib
    merge_id = data.get("merge_id")
    if _manifest_path(merge_id) is None:
        return _error("merge_id is not a valid engine album merge id", "INVALID_MERGE_ID")
    manifest = _load(merge_id)
    if manifest is None:
        return _error("no merge record for this id", "MERGE_RECORD_NOT_FOUND", 404)
    if manifest.get("status") == "rolled_back":
        return jsonify({"replayed": True, **(manifest.get("rollback_result") or {})})
    if manifest.get("status") != "applied":
        return _error(f"merge is {manifest.get('status')}, not applied", "MERGE_NOT_APPLIED", 409)

    target_id = int(manifest["target_album_id"])
    for sid in manifest["retired_album_ids"]:
        existing = lib.get_album(int(sid))
        if existing is not None:
            return _error(f"album id {sid} is in use again", "ALBUM_ID_REUSED", 409)
    for rec in manifest["items"]:
        item = lib.get_item(int(rec["item_id"]))
        if item is None or item.album_id != target_id:
            return _error(f"item {rec['item_id']} is no longer in the merged row", "ITEM_ALBUM_DRIFT", 409)
        if _identity(item) != rec["identity"]:
            return _error(f"item {rec['item_id']} changed since the merge", "ITEM_IDENTITY_DRIFT", 409)

    op_id, _fp, early = ops._idempotency_precheck("album_row_merge_rollback", data)
    if early is not None:
        return early
    restored_rows: List[int] = []
    moved_back: List[Dict[str, Any]] = []
    try:
        with ops.mutation_lock:
            for sid in manifest["retired_album_ids"]:
                _restore_album_row(lib, int(sid), manifest["source_album_snapshots"][str(sid)])
                restored_rows.append(int(sid))
            for rec in manifest["items"]:
                item = lib.get_item(int(rec["item_id"]))
                item.album_id = int(rec["source_album_id"])
                item.store()
                moved_back.append(rec)
            for rec in manifest["items"]:
                item = lib.get_item(int(rec["item_id"]))
                if item.album_id != int(rec["source_album_id"]) or _identity(item) != rec["identity"]:
                    raise RuntimeError(f"rollback postcondition failed for item {rec['item_id']}")
            result = {"success": True, "merge_id": merge_id,
                      "restored_album_ids": list(manifest["retired_album_ids"]),
                      "restored_items": [{"item_id": r["item_id"], "album_id": r["source_album_id"]}
                                         for r in manifest["items"]]}
            manifest.update(status="rolled_back", rollback_result=result)
            _save(manifest)
        ops.update_operation(op_id, "succeeded", result=result)
        return jsonify({"operation_id": op_id, **result})
    except Exception:
        ops.log.exception("album-row-merge rollback failed; returning to the merged state")
        try:
            with ops.mutation_lock:
                for rec in moved_back:
                    item = lib.get_item(int(rec["item_id"]))
                    if item is not None:
                        item.album_id = target_id
                        item.store()
                for sid in restored_rows:
                    album = lib.get_album(sid)
                    if album is not None and not list(album.items()):
                        album.remove(delete=False, with_items=False)
        except Exception:
            ops.log.exception("album-row-merge rollback compensation failed")
            manifest.update(status="recovery_required")
            _save(manifest)
        ops.update_operation(op_id, "failed", error="Rollback failed", error_code="ROLLBACK_FAILED")
        return _error("Rollback failed; the merged state was kept", "ROLLBACK_FAILED", 500)
