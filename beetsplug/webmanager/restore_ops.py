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
    the folder's id. The manifest is written BEFORE the first change and
    updated after every file move and row removal, so a hard crash leaves a
    record of exactly what happened (ARCH-023).

POST /webmanager/import/rollback/undo
    {"quarantine_id": "<32 hex>"}
    Reads the engine's own manifest, moves each file back to where it was
    and re-adds its library row (at its original id when that id is free).
    Every quarantine source is checked before anything is restored: a
    missing file is never papered over by re-adding a row with no media.

The Web Manager decides *whether* a removal may happen and *whether* an
import may be undone (a reviewed and approved transaction); this module
only performs the change inside Beets.
"""

import hashlib
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


def _write_manifest(folder: str, manifest: Dict[str, Any]) -> None:
    """Write the manifest atomically: a crash never leaves truncated JSON.

    ``os.replace`` is atomic on one filesystem, so the reader either sees the
    previous complete manifest or this one (H2)."""
    path = os.path.join(folder, MANIFEST_NAME)
    tmp = path + ".tmp"
    # O_NOFOLLOW: a symlink planted at the .tmp name (by something with write
    # access to the engine's own quarantine dir) must not redirect this write
    # outside the folder. O_TRUNC overwrites a stale tmp left by a prior crash.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _contained(path: str, folder: str) -> bool:
    """Whether ``path`` really is inside ``folder`` (resolved, not lexical).

    A ``..`` component or a symlink must not let a manifest name a file
    outside the engine's own quarantine folder."""
    try:
        root = os.path.realpath(folder)
        resolved = os.path.realpath(path)
        return resolved != root and os.path.commonpath([resolved, root]) == root
    except (OSError, ValueError):
        return False


def _move_no_clobber(src: str, dst: str) -> None:
    """Move ``src`` to ``dst`` without ever replacing an existing file.

    The destination is reserved with ``O_CREAT|O_EXCL``, so a file that
    appears between the preflight and the move makes the move fail instead of
    being overwritten. Bytes are copied (quarantine and library may be on
    different filesystems), verified against the source, and only then is the
    source removed."""
    parent = os.path.dirname(dst)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            shutil.copyfileobj(inp, out)
            out.flush()
            os.fsync(out.fileno())
        shutil.copystat(src, dst)
        if _sha256_file(src) != _sha256_file(dst):
            raise OSError(f"copy of {src} did not verify")
        os.remove(src)
    except Exception:
        try:
            os.remove(dst)
        except OSError:
            pass
        raise


def _row_matches(row, snapshot: Dict[str, Any]) -> bool:
    """Whether a live row already equals a recorded snapshot (resumed undo)."""
    for key, want in (snapshot or {}).items():
        if key == "id":
            continue
        if key == "path":
            if _fspath(row.get("path")) != _fspath(want):
                return False
        elif row.get(key) != want:
            return False
    return True


def _readd(lib, snapshot: Dict[str, Any], path: str, *, album_id: Optional[int] = None):
    """Re-add a removed row -- at its original id when that id is free.

    The album row must exist first: a missing parent used to be quietly
    turned into ``album_id=None``, which orphaned every re-added track as a
    singleton next to an empty album (H1). Refusing here makes the caller
    restore the album first instead.
    """
    from beets.library import Item

    fields = {k: v for k, v in snapshot.items() if k not in ("id", "path")}
    target_album = album_id if album_id is not None else snapshot.get("album_id")
    if target_album is not None:
        album_id_int = int(target_album)
        if lib.get_album(album_id_int) is None:
            raise ValueError(f"album row {album_id_int} must be restored before its items")
        fields["album_id"] = album_id_int
    else:
        fields["album_id"] = None
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

    # Idempotency first: a retry must replay the original outcome, not fail
    # against the library state this operation already changed (H5).
    op_id, _fingerprint, early = ops._idempotency_precheck("album_row_remove", data)
    if early is not None:
        return early

    def fail(message: str, code: str, status: int = 400):
        ops.update_operation(op_id, "failed", error=message, error_code=code)
        return _error(message, code, status)

    album = lib.get_album(album_id)
    if album is None:
        return fail("album not found", "ALBUM_NOT_FOUND", 404)
    members = [i.id for i in lib.items(f"album_id:{album_id}")]
    if members:
        return fail(f"album {album_id} still holds {len(members)} item(s)",
                    "ALBUM_NOT_EMPTY", 409)

    row_id = uuid.uuid4().hex
    folder = os.path.join(_quarantine_root(), row_id)
    snapshot = _album_snapshot(album)
    manifest: Optional[Dict[str, Any]] = None
    removed = False
    try:
        with ops.mutation_lock:
            # Re-assert emptiness under the lock the removal takes: an item
            # may have been added between the check above and here.
            album = lib.get_album(album_id)
            if album is None:
                return fail("album not found", "ALBUM_NOT_FOUND", 404)
            live_members = [i.id for i in lib.items(f"album_id:{album_id}")]
            if live_members:
                return fail(f"album {album_id} still holds {len(live_members)} item(s)",
                            "ALBUM_NOT_EMPTY", 409)
            snapshot = _album_snapshot(album)
            os.makedirs(folder)
            # The record exists before the row does not: a crash between the
            # two still leaves the undo (H2).
            manifest = {"kind": ALBUM_ROW_KIND, "album_id": album_id,
                        "snapshot": snapshot, "status": "applying"}
            _write_manifest(folder, manifest)
            album.remove(delete=False, with_items=False)
            removed = True
            manifest["status"] = "applied"
            _write_manifest(folder, manifest)
        result = {"success": True, "album_id": album_id, "album_row_id": row_id,
                  "removed": 1}
        ops.update_operation(op_id, "succeeded", result=result)
        return jsonify({"operation_id": op_id, **result})
    except Exception:
        ops.log.exception("album-row remove failed")
        if not removed:
            return fail("Remove failed", "REMOVE_FAILED", 500)
        # The row was removed but a later write failed. Put it back before
        # reporting failure: a 500 that left the library changed would be a
        # false negative, and the operator has no row id to undo it with.
        try:
            with ops.mutation_lock:
                existing = lib.get_album(album_id)
                if existing is None:
                    _restore_album_row(lib, album_id, snapshot)
                elif not _row_matches(existing, snapshot):
                    raise OSError("the album id was reused before compensation")
                if manifest is not None:
                    manifest["status"] = "compensated"
                    _write_manifest(folder, manifest)
            return fail("Remove failed after the album row was removed; it was restored.",
                        "REMOVE_FAILED", 500)
        except Exception:
            ops.log.exception("album-row remove compensation failed")
            return fail("Remove failed after the album row was removed; check the library "
                        f"and the engine record {row_id} before retrying.",
                        "REMOVE_PARTIAL", 500)


@ops.webmanager_bp.route("/album-row/rollback", methods=["POST"])
def run_album_row_rollback():
    """Re-create a removed album row from the engine's own manifest."""
    data = request.get_json(force=True, silent=True) or {}
    lib = g.lib
    folder = _manifest_dir(data.get("album_row_id"))
    # Pure shape validation stays here; everything that reads or changes the
    # library comes after the idempotency check (H5).
    if folder is None:
        return _error("album_row_id is not a valid engine id", "INVALID_ALBUM_ROW_ID")

    op_id, _fingerprint, early = ops._idempotency_precheck("album_row_rollback", data)
    if early is not None:
        return early

    def fail(message: str, code: str, status: int = 400):
        ops.update_operation(op_id, "failed", error=message, error_code=code)
        return _error(message, code, status)

    try:
        with open(os.path.join(folder, MANIFEST_NAME), encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return fail("no engine album-row record for this id", "ALBUM_ROW_NOT_FOUND", 404)
    if not isinstance(manifest, dict) or manifest.get("kind") != ALBUM_ROW_KIND:
        return fail("that record is not an album-row removal", "WRONG_RECORD_KIND")
    manifest_status = manifest.get("status")
    if manifest_status not in ("applied", "applying"):
        return fail("that album row was already restored", "ALBUM_ROW_ALREADY_RESTORED", 409)
    album_id = manifest.get("album_id")
    snapshot = manifest.get("snapshot")
    if not isinstance(album_id, int) or not isinstance(snapshot, dict):
        return fail("the engine album-row record is malformed", "ALBUM_ROW_RECORD_INVALID")
    try:
        with ops.mutation_lock:
            existing = lib.get_album(album_id)
            if existing is not None:
                # A crash before removal leaves the original row intact.
                # Check it under the lock before recording this no-op.
                if manifest_status != "applying" or not _row_matches(existing, snapshot):
                    return fail(f"album {album_id} exists again", "ALBUM_ID_OCCUPIED", 409)
                manifest["status"] = "restored"
                _write_manifest(folder, manifest)
                result = {"success": True, "album_id": album_id, "restored": False,
                          "already_present": True}
            else:
                album = _restore_album_row(lib, album_id, snapshot)
                manifest["status"] = "restored"
                _write_manifest(folder, manifest)
                result = {"success": True, "album_id": album.id, "restored": True}
        ops.update_operation(op_id, "succeeded", result=result)
        return jsonify({"operation_id": op_id, **result})
    except Exception:
        ops.log.exception("album-row rollback failed")
        return fail("Rollback failed", "ROLLBACK_FAILED", 500)


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

    # Precheck before the album/item lookups: a retry after a successful
    # rollback must replay the original result instead of ALBUM_NOT_FOUND (H5).
    op_id, _fingerprint, early = ops._idempotency_precheck("import_rollback", data)
    if early is not None:
        return early

    def fail(message: str, code: str, status: int = 400):
        ops.update_operation(op_id, "failed", error=message, error_code=code)
        return _error(message, code, status)

    album = lib.get_album(album_id)
    if album is None:
        return fail("album not found", "ALBUM_NOT_FOUND", 404)

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
    album_snapshot = _album_snapshot(album)
    records: List[Dict[str, Any]] = []
    try:
        for index, step in enumerate(plan):
            records.append({
                "item_id": step["snapshot"].get("id"),
                "original_path": step["path"],
                "quarantine_path": (os.path.join(qdir, "files",
                                                 f"{index:03d}_{os.path.basename(step['path'])}")
                                    if move_files else ""),
                "sha256": _sha256_file(step["path"]) if move_files else "",
                "snapshot": step["snapshot"],
                "file_moved": False,
                "row_removed": False,
            })
    except Exception:
        # A file changed or became unreadable between validation and here.
        # Nothing has been moved or removed yet.
        ops.log.exception("import rollback could not hash its sources")
        return fail("The import could not be prepared; nothing was changed.", "ROLLBACK_FAILED", 500)
    manifest: Dict[str, Any] = {"kind": IMPORT_ROLLBACK_KIND, "album_id": album_id,
                                "album_snapshot": album_snapshot, "items": records,
                                "files_moved": move_files, "status": "applying"}
    try:
        with ops.mutation_lock:
            # H4: the live album must still hold exactly the rows the import
            # created. Extra or missing rows mean the album changed since the
            # import, and removing it would orphan or misreport items.
            live_ids = {int(i.id) for i in lib.items(f"album_id:{album_id}")}
            if live_ids != seen:
                extra = sorted(live_ids - seen)
                missing = sorted(seen - live_ids)
                return fail("The album's items changed since the import "
                            f"(extra: {extra or 'none'}, missing: {missing or 'none'}); "
                            "review the album before undoing the import.",
                            "ALBUM_MEMBERSHIP_CHANGED", 409)
            album = lib.get_album(album_id)
            if album is None:
                return fail("album not found", "ALBUM_NOT_FOUND", 404)
            manifest["album_snapshot"] = _album_snapshot(album)
            album_snapshot = manifest["album_snapshot"]
            # The path/hash plan was built before the lock. Re-read both now so
            # a concurrent move or replacement cannot make us quarantine the
            # wrong bytes (Medium TOCTOU).
            for step, record in zip(plan, records):
                live = lib.get_item(int(record["item_id"]))
                if live is None or int(live.album_id) != album_id:
                    return fail(f"item {record['item_id']} changed while the rollback was preparing",
                                "ALBUM_MEMBERSHIP_CHANGED", 409)
                live_path = _fspath(live.path)
                if live_path != record["original_path"]:
                    return fail(f"item {record['item_id']} moved while the rollback was preparing",
                                "ITEM_PATH_CHANGED", 409)
                if move_files:
                    if not os.path.isfile(live_path):
                        return fail(f"item {record['item_id']}: file disappeared", "ITEM_PATH_INVALID")
                    if _sha256_file(live_path) != record["sha256"]:
                        return fail(f"item {record['item_id']}: file changed while the rollback was preparing",
                                    "ITEM_CONTENT_CHANGED", 409)
                step["item"] = live
                record["snapshot"] = _snapshot(live)
            os.makedirs(os.path.join(qdir, "files"))
            # Written before the first move: a crash now has a record (H2).
            _write_manifest(qdir, manifest)
            for index, step in enumerate(plan):
                record = records[index]
                if move_files:
                    _move_no_clobber(step["path"], record["quarantine_path"])
                    record["file_moved"] = True
                    _write_manifest(qdir, manifest)
                step["item"].remove(delete=False, with_album=False)
                record["row_removed"] = True
                _write_manifest(qdir, manifest)
            album.remove(delete=False, with_items=False)
            manifest["status"] = "applied"
            _write_manifest(qdir, manifest)
        result = {"success": True, "quarantine_id": quarantine_id, "album_id": album_id,
                  "removed": [{"item_id": r["item_id"], "original_path": r["original_path"],
                               "quarantine_path": r["quarantine_path"]} for r in records],
                  "files_moved": move_files}
        ops.update_operation(op_id, "succeeded", result=result)
        return jsonify({"operation_id": op_id, **result})
    except Exception:
        ops.log.exception("import rollback failed; restoring")
        compensated = True
        try:
            with ops.mutation_lock:
                # H1: the album row comes back FIRST, so every re-added item
                # keeps its album instead of becoming a singleton.
                if lib.get_album(album_id) is None:
                    _restore_album_row(lib, album_id, album_snapshot)
                for record in reversed(records):
                    qpath = record.get("quarantine_path") or ""
                    original = record["original_path"]
                    if record.get("file_moved") and qpath and os.path.isfile(qpath) \
                            and not os.path.exists(original):
                        _move_no_clobber(qpath, original)
                    if record.get("file_moved") and not os.path.exists(original):
                        compensated = False
                    if record.get("row_removed") and lib.get_item(record["item_id"]) is None:
                        try:
                            _readd(lib, record["snapshot"], original, album_id=album_id)
                        except Exception:
                            compensated = False
                manifest["status"] = "compensated" if compensated else "incomplete"
                _write_manifest(qdir, manifest)
        except Exception:
            compensated = False
            ops.log.exception("import rollback restore failed")
            try:
                manifest["status"] = "incomplete"
                _write_manifest(qdir, manifest)
            except Exception:
                pass
        message = ("Rollback failed; the import was left in place."
                   if compensated else
                   "Rollback failed and part of the import changed; the engine's quarantine "
                   f"record {quarantine_id} lists the steps that ran. Inspect the library before retrying.")
        return fail(message, "ROLLBACK_FAILED", 500)


@ops.webmanager_bp.route("/import/rollback/undo", methods=["POST"])
def run_import_rollback_undo():
    """Put an undone import back: the files where they were, the rows back."""
    data = request.get_json(force=True, silent=True) or {}
    lib = g.lib
    folder = _manifest_dir(data.get("quarantine_id"))
    if folder is None:
        return _error("quarantine_id is not a valid engine quarantine id", "INVALID_QUARANTINE_ID")

    # Precheck before reading the manifest: a retry of a completed undo must
    # replay the original success, not IMPORT_ALREADY_UNDONE (H5).
    op_id, _fingerprint, early = ops._idempotency_precheck("import_rollback_undo", data)
    if early is not None:
        return early

    def fail(message: str, code: str, status: int = 400):
        ops.update_operation(op_id, "failed", error=message, error_code=code)
        return _error(message, code, status)

    try:
        with open(os.path.join(folder, MANIFEST_NAME), encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return fail("no engine import record for this id", "IMPORT_RECORD_NOT_FOUND", 404)
    if not isinstance(manifest, dict) or manifest.get("kind") != IMPORT_ROLLBACK_KIND:
        return fail("that record is not an import rollback", "WRONG_RECORD_KIND")
    status = manifest.get("status")
    if status in ("compensated", "incomplete"):
        return fail("That import rollback failed and was not completed; inspect the library "
                    "before undoing it.", "IMPORT_ROLLBACK_INCOMPLETE", 409)
    if status not in ("applied", "applying"):
        return fail("that import was already undone", "IMPORT_ALREADY_UNDONE", 409)
    # ``applying`` is a rollback interrupted after its durable manifest but
    # before its final status write. Every row/file below is preflighted
    # before mutation, so undo can resume from whatever partial state it
    # recorded (H2). ``compensated``/``incomplete`` are still refused: those
    # records say the engine could not prove the partial state.
    records = manifest.get("items") or []
    album_id = manifest.get("album_id")
    if not isinstance(album_id, int):
        return fail("the engine import record is malformed", "IMPORT_RECORD_INVALID")
    try:
        with ops.mutation_lock:
            album_snapshot = manifest.get("album_snapshot") or {}
            album = lib.get_album(album_id)
            if album is not None and not _row_matches(album, album_snapshot):
                return fail(f"album id {album_id} is taken again", "ALBUM_ID_OCCUPIED", 409)

            # H6: preflight EVERY source before changing anything. A record
            # whose quarantined file is gone (and whose original is not the
            # byte-identical one) must refuse, not re-add a row with no media.
            # The checks run under mutation_lock and the moves use O_EXCL, so
            # a file that appears later is never clobbered.
            moves: List[Dict[str, Any]] = []
            for record in records:
                if not isinstance(record, dict):
                    return fail("the engine import record is malformed", "IMPORT_RECORD_INVALID")
                original = _fspath(record.get("original_path"))
                qpath = _fspath(record.get("quarantine_path"))
                if not original or not _inside_allowed(original):
                    return fail("recorded path is invalid", "RECORD_PATH_INVALID")
                if qpath and not _contained(qpath, folder):
                    return fail("recorded path is invalid", "RECORD_PATH_INVALID")
                want_sha = str(record.get("sha256") or "")
                snapshot = record.get("snapshot") or {}
                # A directory (or a dangling symlink) at the quarantine path is
                # not a quarantined file; treat it as missing instead of
                # handing it to the hasher or the mover.
                q_exists = bool(qpath) and os.path.isfile(qpath)
                o_exists = os.path.exists(original)
                if q_exists and want_sha and _sha256_file(qpath) != want_sha:
                    return fail(f"quarantined file for item {record.get('item_id')} changed; "
                                "nothing was restored", "QUARANTINE_FILE_CHANGED", 409)
                if qpath:
                    if q_exists and o_exists and want_sha and _sha256_file(original) == want_sha:
                        # A previous undo copied the file back but did not
                        # remove the quarantine copy.
                        move = False
                    elif q_exists and o_exists:
                        return fail(f"{original} is occupied", "ORIGINAL_PATH_OCCUPIED", 409)
                    elif q_exists:
                        move = True
                    elif o_exists and want_sha and _sha256_file(original) == want_sha:
                        # A previous partial undo already put this file back.
                        move = False
                    elif o_exists:
                        return fail(f"{original} is occupied", "ORIGINAL_PATH_OCCUPIED", 409)
                    else:
                        return fail(f"quarantined file for item {record.get('item_id')} is missing; "
                                    "nothing was restored", "QUARANTINE_FILE_MISSING", 409)
                elif not os.path.isfile(original):
                    return fail(f"file for item {record.get('item_id')} is missing; nothing was restored",
                                "ITEM_PATH_INVALID", 409)
                else:
                    move = False

                # Is the row already back from a previous partial undo?
                has_row = False
                for live in (lib.items(f"album_id:{album_id}") if album is not None else []):
                    if _fspath(live.path) == original:
                        if not _row_matches(live, snapshot):
                            return fail(f"an item at {original} changed since the import; "
                                        "nothing was restored", "ITEM_PATH_OCCUPIED", 409)
                        has_row = True
                        break
                old_id = record.get("item_id")
                if not has_row and str(old_id).isdigit():
                    row = lib.get_item(int(old_id))
                    has_row = row is not None and _row_matches(row, snapshot)
                moves.append({"record": record, "original": original, "qpath": qpath,
                              "move": move, "has_row": has_row, "snapshot": snapshot})

            if album is None:
                album = _restore_album_row(lib, album_id, album_snapshot)
            restored = []
            for step in moves:
                record = step["record"]
                original = step["original"]
                if step["move"]:
                    _move_no_clobber(step["qpath"], original)
                item = None
                if step["has_row"]:
                    for live in lib.items(f"album_id:{album.id}"):
                        if _fspath(live.path) == original and _row_matches(live, step["snapshot"]):
                            item = live
                            break
                    if item is None:
                        raise OSError(f"the restored row for {original} disappeared")
                else:
                    item = _readd(lib, step["snapshot"], original, album_id=album.id)
                restored.append({"old_item_id": record.get("item_id"), "new_item_id": item.id,
                                 "path": original, "album_id": item.album_id})
            manifest["status"] = "undone"
            _write_manifest(folder, manifest)
        result = {"success": True, "restored": restored, "album_id": album.id}
        ops.update_operation(op_id, "succeeded", result=result)
        return jsonify({"operation_id": op_id, **result})
    except Exception:
        ops.log.exception("import rollback undo failed")
        return fail("Undo failed", "ROLLBACK_FAILED", 500)


@ops.webmanager_bp.route("/item-path/restore", methods=["POST"])
def run_item_path_restore():
    """Serialize the exact-path restore with every other plugin mutation."""
    with ops.mutation_lock:
        return _run_item_path_restore()


def _run_item_path_restore():
    """Put explicit tracks back at the exact paths an earlier apply moved them
    away from (C3).

    Beets' own move uses the configured path format, which can rename a file
    (`01 Track.wav` -> `01 Track 1.wav`) even when the album artist is the only
    field restored. An artist-folder rollback therefore cannot rely on
    ``beet modify -a`` alone: it asks this op for the original paths. The op is
    content-idempotent -- a file already at the target is accepted when its
    SHA-256 matches, so a crash between the move and the row update resumes --
    and it never replaces an existing file (``_move_no_clobber``).
    """
    data = request.get_json(force=True, silent=True) or {}
    entries = data.get("items")
    lib = g.lib
    op_id, _fingerprint, early = ops._idempotency_precheck("item_path_restore", data)
    if early is not None:
        return early

    def fail(message: str, code: str, status: int = 400):
        ops.update_operation(op_id, "failed", error=message, error_code=code)
        return _error(message, code, status)

    if not isinstance(entries, list) or not entries:
        return fail("items must be a non-empty list", "INVALID_ITEMS")
    if len(entries) > MAX_ITEMS:
        return fail(f"at most {MAX_ITEMS} paths per request", "TOO_MANY_ITEMS")

    prepared: List[Dict[str, Any]] = []
    targets: set = set()
    for raw in entries:
        if not isinstance(raw, dict):
            return fail("each item must be an object", "INVALID_ITEMS")
        try:
            iid = int(raw.get("id"))
        except (TypeError, ValueError):
            return fail("item id must be an integer", "INVALID_ITEM_ID")
        want = raw.get("path")
        if not isinstance(want, str) or not want.strip() or "\x00" in want:
            return fail("path must be a non-empty string", "INVALID_PATH")
        # Use the canonical value admitted by the containment resolver, not
        # the request spelling after a boolean check. This is the same
        # source-to-filesystem boundary used by the other plugin operations.
        target = ops.resolve_safe_descendant(want, ops.get_allowed_roots())
        if target is None:
            return fail("path outside allowed roots", "PATH_NOT_ALLOWED", 403)
        expected = str(raw.get("sha256") or "").lower()
        if expected and not (len(expected) == 64 and all(c in "0123456789abcdef" for c in expected)):
            return fail("sha256 must be a hex digest", "INVALID_SHA256")
        item = lib.get_item(iid)
        if item is None:
            return fail(f"item {iid} not found", "ITEM_NOT_FOUND", 404)
        src = _fspath(item.path)
        if src and not _inside_allowed(src):
            # Never move a file from outside the allowed roots into the
            # library: a stale or tampered row must not become a copy channel.
            return fail("source path outside allowed roots", "PATH_NOT_ALLOWED", 403)
        key = os.path.normcase(os.path.realpath(target))
        if key in targets:
            return fail("two items target the same path", "DUPLICATE_TARGET")
        targets.add(key)
        prepared.append({"id": iid, "from": src, "to": target,
                         "sha256": expected, "mode": ""})

    # Preflight every entry before touching a file. A destination that
    # already exists is never overwritten: the only accepted occupant is the
    # very file this operation already moved there (same digest), which is
    # the resume path after a crash.
    for p in prepared:
        src, dst = p["from"], p["to"]
        if os.path.isdir(dst):
            return fail(f"destination is a directory: {dst}", "DESTINATION_IS_DIRECTORY", 409)
        if src and os.path.exists(src) and not os.path.isfile(src):
            return fail(f"source is not a file: {src}", "SOURCE_NOT_A_FILE", 409)
        same = bool(src) and os.path.normcase(os.path.realpath(src)) == os.path.normcase(os.path.realpath(dst))
        if same:
            if not os.path.isfile(dst):
                return fail(f"file is missing: {dst}", "SOURCE_MISSING", 409)
            p["mode"] = "done"
        elif os.path.exists(dst):
            if not src or not os.path.exists(src):
                if not p["sha256"] or _sha256_file(dst) != p["sha256"]:
                    return fail(f"destination already exists: {dst}", "DESTINATION_OCCUPIED", 409)
                p["mode"] = "row"
            else:
                return fail(f"destination already exists: {dst}", "DESTINATION_OCCUPIED", 409)
        else:
            if not src or not os.path.exists(src):
                return fail(f"file is missing: {src or dst}", "SOURCE_MISSING", 409)
            p["mode"] = "move"

    restored: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for p in prepared:
        try:
            if p["mode"] == "move":
                if p["sha256"] and _sha256_file(p["from"]) != p["sha256"]:
                    failed.append({"id": p["id"], "error_code": "HASH_MISMATCH",
                                   "error": f"file changed since the rollback began: {p['from']}"})
                    continue
                _move_no_clobber(p["from"], p["to"])
            if p["sha256"] and _sha256_file(p["to"]) != p["sha256"]:
                failed.append({"id": p["id"], "error_code": "HASH_MISMATCH",
                               "error": f"file did not verify after the restore: {p['to']}"})
                continue
            with lib.transaction():
                live = lib.get_item(p["id"])
                if live is None:
                    failed.append({"id": p["id"], "error_code": "ITEM_NOT_FOUND",
                                   "error": f"item {p['id']} disappeared"})
                    continue
                live.path = os.fsencode(p["to"])
                live.store()
            live = lib.get_item(p["id"])
            if live is None or os.path.normcase(os.path.abspath(_fspath(live.path))) != os.path.normcase(p["to"]):
                failed.append({"id": p["id"], "error_code": "ROW_NOT_RESTORED",
                               "error": f"item {p['id']} still points elsewhere"})
                continue
            restored.append({"id": p["id"], "path": p["to"], "resumed": p["mode"] != "move"})
        except Exception as exc:
            ops.log.exception("item path restore failed for %s", p["id"])
            failed.append({"id": p["id"], "error_code": "MOVE_FAILED", "error": str(exc)})

    result = {"success": not failed, "restored": restored, "failed": failed}
    if failed:
        ops.update_operation(op_id, "failed", error="Restore did not finish for every item",
                             error_code="RESTORE_FAILED", result=result)
    else:
        ops.update_operation(op_id, "succeeded", result=result)
    return jsonify({"operation_id": op_id, **result})
