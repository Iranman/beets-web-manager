"""Album relocation (rename / move to library) with an exact way back.

POST /webmanager/album-relocation
    {"album_id": N, "expected_paths": {"<item id>": "<path>", ...}}
    Beets' own ``Album.move()`` moves the album's files and cover to where
    its path templates put them. Refused (409 ``STALE_PLAN``) unless the album
    still has exactly the planned items at the planned paths. The result
    lists every item's absolute path, and the cover path, before and after,
    plus ``moved`` and ``skipped`` item ids: ``Item.move()`` skips a track
    whose file is missing (a warning only), so a partial move is reported,
    never hidden. Beets drops ``artpath`` when the cover file is missing;
    the before-state keeps it so the rollback can put the field back.

POST /webmanager/album-relocation/rollback
    {"album_id": N, "items": [{"id", "path", "restore_path"}, ...],
     "artpath": "<now>", "restore_artpath": "<before>"}
    Moves each file back with Beets' ``Item.move_file()`` and the cover with
    ``util.move`` (no overwrite), then stores the rows. Every check runs
    before anything moves; refused, with nothing changed, when the album is
    gone (``ALBUM_NOT_FOUND``), its item set changed (``ALBUM_CHANGED``), an
    item is no longer at ``path`` (``ITEM_MOVED``), the cover changed
    (``ART_CHANGED``), a moved file is gone (``FILE_MISSING``), a restore
    path is occupied or tracked (``TARGET_EXISTS``), or a restore path is
    unsafe (outside the allowed roots, a symlink component, another
    extension). A track already at its restore path counts as done, so a
    rollback a restart interrupted resumes from the recorded paths. Each
    track's file move and row store share one Beets transaction, and the
    path is checked after the move: a ``unique_path`` rename
    (``name.1.ext``) is a failure, never accepted. A failure part-way puts
    the files already moved back (``UNDONE``) or, when that cannot be
    proven, fails (``ROLLBACK_FAILED``). Vacated folders are removed only
    when empty, with ``os.rmdir`` (never ``util.prune_dirs``, which
    ``rmtree``s after its own emptiness check).

Within one filesystem ``util.move`` is ``os.replace``, which keeps owner and
mode. Across filesystems Beets copies, copies mode and times (not owner),
then removes the source; nothing here chowns.

Both hold ``ops.mutation_lock``. Refusals and undone failures (``UNDONE``:
every moved file was put back) are not kept under the Idempotency-Key, so a
retry after the operator fixes the cause runs.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple

from beets import util
from beets.util import MoveOperation
from flask import g, jsonify, request

from . import operations as ops
from .engine_common import _error
from . import folder_ops  # attributes read at call time: folder_ops imports operations


def _no(message: str, code: str, status: int = 409) -> Exception:
    return folder_ops._Refused(message, code, status)


def _abs(lib, value: Any) -> str:
    path = os.fsdecode(value) if isinstance(value, bytes) else str(value or "")
    if path and not os.path.isabs(path):
        path = os.path.join(os.fsdecode(lib.directory), path)
    return os.path.normpath(path) if path else ""


def _state(lib, album) -> Dict[str, Any]:
    return {"items": {str(it.id): _abs(lib, it.path) for it in album.items()},
            "artpath": _abs(lib, album.artpath)}


def _root_of(path: str) -> str:
    roots = [os.path.abspath(r) for r in ops.get_allowed_roots()]
    return next((r for r in roots if path.startswith(r + os.sep)), "")


def _contained(path: Any) -> Tuple[str, str]:
    """(path, root): a normalized absolute path strictly inside an allowed
    root, with no symlink component below that root."""
    if not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path:
        raise _no("restore path must be a normalized absolute path", "PATH_INVALID", 400)
    root = _root_of(path)
    if not root:
        raise _no("restore path is outside the allowed roots", "PATH_OUTSIDE_ROOTS", 400)
    # Use the contained value: refuses the root itself, NUL and symlink components below the root.
    return folder_ops.contained_path(path, root), root


def _safe_target(lib, path: Any, current: str) -> Tuple[str, str]:
    path, root = _contained(path)
    if os.path.splitext(path)[1].lower() != os.path.splitext(current)[1].lower():
        raise _no("restore path has another file extension", "EXTENSION_CHANGED", 400)
    if os.path.lexists(path):
        raise _no("restore path is occupied", "TARGET_EXISTS")
    try:
        folder_ops._refuse_tracked(lib, path)
    except folder_ops._Refused:
        raise _no("restore path is a library item's path", "TARGET_EXISTS") from None
    return path, root


def _check_relocate(lib, data: Dict[str, Any]) -> Tuple[Any, Dict[str, Any]]:
    album = lib.get_album(int(data.get("album_id") or 0))
    if not album:
        raise _no("album not found", "ALBUM_NOT_FOUND")
    before = _state(lib, album)
    expected = {str(k): _abs(lib, v) for k, v in (data.get("expected_paths") or {}).items()}
    if not expected or expected != before["items"]:
        raise _no("the album changed since the relocation was planned", "STALE_PLAN")
    return album, before


class _Undone(Exception):
    """The operation failed and every file it had moved was put back."""


def _relocate(lib, album, before) -> Dict[str, Any]:
    try:
        album.move(operation=MoveOperation.MOVE)
    except Exception:
        ops.log.exception("album relocation failed; putting moved files back")
        album = lib.get_album(album.id)
        for item in album.items():
            back = before["items"].get(str(item.id))
            if back and _abs(lib, item.path) != back and os.path.exists(item.path):
                _move_item(lib, item, back)
        if _state(lib, album)["artpath"] != before["artpath"] or not all(
                os.path.exists(p) for p in before["items"].values()):
            raise
        raise _Undone() from None
    album = lib.get_album(album.id)
    after = _state(lib, album)
    moved = sorted(k for k, p in after["items"].items() if p != before["items"].get(k))
    skipped = sorted(str(it.id) for it in album.items()  # Item.move() skips a missing file
                     if str(it.id) not in moved and _abs(lib, it.destination()) != before["items"].get(str(it.id)))
    return {"album_id": album.id, "before": before, "after": after, "moved": moved, "skipped": skipped}


def _check_rollback(lib, data: Dict[str, Any]) -> Tuple[Any, List[Tuple[Any, str, str]], Tuple[str, str]]:
    album = lib.get_album(int(data.get("album_id") or 0))
    if not album:
        raise _no("album not found", "ALBUM_NOT_FOUND")
    live = _state(lib, album)
    wanted = {str(e.get("id")): e for e in (data.get("items") or []) if isinstance(e, dict)}
    if not wanted or set(wanted) != set(live["items"]):
        raise _no("the album's tracks changed since it was relocated", "ALBUM_CHANGED")
    by_id = {str(it.id): it for it in album.items()}
    steps, targets = [], set()
    for iid, entry in wanted.items():
        now = live["items"][iid]
        restore = entry.get("restore_path")
        if restore == now:
            continue  # never moved (skipped by the apply), or already back (an interrupted rollback)
        if _abs(lib, entry.get("path")) != now:
            raise _no(f"item {iid} was moved again since the relocation", "ITEM_MOVED")
        if not os.path.isfile(now):
            raise _no(f"item {iid}'s file is missing", "FILE_MISSING")
        target, root = _safe_target(lib, restore, now)
        if target in targets:
            raise _no("two items would be restored to one path", "PATH_INVALID", 400)
        targets.add(target)
        steps.append((by_id[iid], now, target, root))
    art_now, art_back = live["artpath"], str(data.get("restore_artpath") or "")
    art_root = ""
    if art_now == art_back:
        pass  # unchanged by the apply, or already back
    elif _abs(lib, data.get("artpath")) != art_now:
        raise _no("the album's cover changed since the relocation", "ART_CHANGED")
    elif art_now and art_back:
        if not os.path.isfile(art_now):
            raise _no("the album's cover file is missing", "FILE_MISSING")
        if art_back in targets:
            raise _no("the cover would be restored onto a track", "PATH_INVALID", 400)
        art_back, art_root = _safe_target(lib, art_back, art_now)
    elif art_now and not art_back:
        raise _no("the album's cover changed since the relocation", "ART_CHANGED")
    else:
        _contained(art_back)  # Beets dropped a missing cover's artpath: restore the field only
    return album, steps, (art_now, art_back, art_root), live


def _move_item(lib, item, dest: str, root: str = "") -> None:
    """Move one track and store its row in one Beets transaction. A
    ``unique_path`` rename (dest taken meanwhile) is a failure, never a result."""
    with lib.transaction():
        util.mkdirall(os.fsencode(dest))
        if root:
            folder_ops.contained_path(dest, root)  # re-check after mkdirall, right before the move
        item.move_file(os.fsencode(dest), MoveOperation.MOVE)
        item.store()
    if _abs(lib, item.path) != dest:
        raise RuntimeError("Beets moved the file to another path")


def _remove_empty_dirs(path: str) -> None:
    """Remove ``path`` and its parents below their allowed root while each
    is empty. ``os.rmdir`` removes only an empty folder, atomically."""
    root = _root_of(path)
    while root and path.startswith(root + os.sep):
        try:
            os.rmdir(path)
        except OSError:
            return
        path = os.path.dirname(path)


def _rollback(lib, album, steps, art, live) -> Dict[str, Any]:
    art_now, art_back, art_root = art
    done: List[Tuple[Any, str]] = []
    art_moved = art_set = False
    try:
        for item, now, back, root in steps:
            done.append((item, now))
            _move_item(lib, item, back, root)
        if art_back != art_now:
            with lib.transaction():
                if art_now:
                    util.mkdirall(os.fsencode(art_back))
                    folder_ops.contained_path(art_back, art_root)
                    util.move(os.fsencode(art_now), os.fsencode(art_back))  # no overwrite
                    art_moved = True
                album.artpath = os.fsencode(art_back)
                album.store()
                art_set = True
    except Exception:
        ops.log.exception("album relocation rollback failed; putting moved files back")
        if art_moved and not art_set:
            util.move(os.fsencode(art_back), os.fsencode(art_now))
        for item, now in reversed(done):
            if os.path.exists(item.path) and _abs(lib, item.path) != now:
                _move_item(lib, item, now)
        if _state(lib, lib.get_album(album.id)) != live or not all(os.path.isfile(now) for _i, now in done):
            raise  # not provably all back: ROLLBACK_FAILED, never "nothing changed"
        raise _Undone() from None
    for vacated in {os.path.dirname(now) for _i, now, _b, _r in steps} | (
            {os.path.dirname(art_now)} if art_moved else set()):
        _remove_empty_dirs(vacated)
    return {"album_id": album.id, "restored_items": len(steps), "restored_art": art_set,
            "after": _state(lib, lib.get_album(album.id))}


def _run(op_type: str, check, run, failure_code: str):
    data = request.get_json(force=True, silent=True) or {}
    lib = g.lib
    ops.bind_durable_registry(lib)
    with ops.mutation_lock:
        # A known key replays its outcome; a refusal below is not registered.
        _op, _fp, early = ops._idempotency_precheck(op_type, data, register=False)
        if early is not None:
            return early
        try:
            checked = check(lib, data)
        except folder_ops._Refused as exc:
            return _error(exc.message, exc.code, exc.status)
        except (TypeError, ValueError, AttributeError):
            return _error("malformed request", "INVALID_REQUEST", 400)
        op_id, _fp, early = ops._idempotency_precheck(op_type, data)
        if early is not None:
            return early
        try:
            result = {"success": True, **run(lib, *checked)}
        except _Undone:
            with ops._operations_lock:  # nothing changed: forget the key so a retry runs
                ops._operations.pop(op_id, None)
            return _error(f"{op_type} failed; every moved file was put back", "UNDONE", 500)
        except Exception:
            ops.log.exception("%s failed", op_type)
            ops.update_operation(op_id, "failed", error=f"{op_type} failed", error_code=failure_code)
            return _error(f"{op_type} failed", failure_code, 500)
        ops.update_operation(op_id, "succeeded", result=result)
    return jsonify({"operation_id": op_id, **result})


@ops.webmanager_bp.route("/album-relocation", methods=["POST"])
def run_album_relocation():
    return _run("album_relocation", _check_relocate, _relocate, "MOVE_FAILED")


@ops.webmanager_bp.route("/album-relocation/rollback", methods=["POST"])
def run_album_relocation_rollback():
    return _run("album_relocation_rollback", _check_rollback, _rollback, "ROLLBACK_FAILED")
