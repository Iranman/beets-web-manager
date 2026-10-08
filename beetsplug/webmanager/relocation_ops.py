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
    rollback a restart interrupted resumes from the recorded paths. A crash
    between Beets' file move and its row store leaves the row at the moved
    path with no file and the file, untracked, at its restore path: a retry
    adopts it (stores the row, moves nothing) only when its size and mtime
    match the ``evidence`` the apply recorded; otherwise ``FILE_MISSING``. Each
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

Preserved torrent sources (plugin 1.15.0, capability
``album_relocation_link``): ``operations`` ({item id: "move"|"link"}) and
``art_operation`` mark files Web Manager judged to be in a seeding folder.
Those are never moved: Beets' ``Item.move()``/``move_art()`` run with
``HARDLINK`` and, when a hard link is impossible (``util.hardlink`` raises
``FilesystemError``: another filesystem, ``EXDEV``, or no permission), with
``COPY``. The row points at the new library file; the original keeps its
path, bytes and inode. The result lists each changed track's ``methods``
(``moved``/``linked``/``copied``) and ``art_method``, and the evidence holds
[size, mtime, dev, inode] of every original and of each library file made.
Their rollback never moves anything: once the original is proven unchanged
(same inode, size and mtime; else ``SOURCE_CHANGED``) and the library file
proven to be the one this relocation made (the original's inode for a link,
the recorded evidence for a copy; else ``LIBRARY_FILE_CHANGED``), the row is
pointed back at the original, and only then is the library file unlinked,
when it is untracked. A library file that is already gone is not an error.

Both hold ``ops.mutation_lock``. Refusals and undone failures (``UNDONE``:
every moved file was put back) are not kept under the Idempotency-Key, so a
retry after the operator fixes the cause runs.
"""

from __future__ import annotations

import os
import stat
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
    link_ops = {str(k): v for k, v in (data.get("operations") or {}).items()}
    art_op = data.get("art_operation") or "move"
    if (not set(link_ops) <= set(before["items"]) or art_op not in ("move", "link")
            or any(v not in ("move", "link") for v in link_ops.values())):
        raise _no("operations must map this album's track ids to move or link", "INVALID_REQUEST", 400)
    return album, before, link_ops, art_op


def _evidence(path: str) -> Any:
    """[size, mtime, dev, inode] of a file, or None. Moves keep size and mtime
    (``os.replace``, or copy + ``copystat`` across filesystems), so a file
    found at a restore path after a crash can be proven to be the one Beets
    moved; a linked or copied original keeps all four."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return [st.st_size, st.st_mtime, st.st_dev, st.st_ino]


_LINKED = ("linked", "copied")


def _same_file(path: str, ev: Any) -> bool:
    """``path`` is a regular file (no symlink) matching ``ev`` exactly."""
    try:
        st = os.lstat(path)
        return (stat.S_ISREG(st.st_mode) and st.st_size == int(ev[0]) and abs(st.st_mtime - float(ev[1])) < 1
                and (st.st_dev, st.st_ino) == (int(ev[2]), int(ev[3])))
    except (OSError, TypeError, ValueError, IndexError):
        return False


def _ours(path: str, method: Any, origin: Any, library: Any) -> bool:
    """``path`` is the library file this relocation made: a hard link of the
    original (its inode) or the copy whose evidence it recorded."""
    if method == "linked":
        try:
            st = os.lstat(path)
            return stat.S_ISREG(st.st_mode) and (st.st_dev, st.st_ino) == (int(origin[2]), int(origin[3]))
        except (OSError, TypeError, ValueError, IndexError):
            return False
    return method == "copied" and _same_file(path, library)


def _original(lib, restore: Any, now: str, origin: Any) -> Tuple[str, str]:
    """(path, root) of a linked/copied track's untouched original, proven by
    the inode, size and mtime recorded at apply; refuses otherwise."""
    path, root = _contained(restore)
    if os.path.splitext(path)[1].lower() != os.path.splitext(now)[1].lower():
        raise _no("restore path has another file extension", "EXTENSION_CHANGED", 400)
    if not _same_file(path, origin):
        raise _no("the original in the torrent folder is gone or changed since the relocation "
                  "(a tag write through a hard link changes it too)", "SOURCE_CHANGED")
    try:
        folder_ops._refuse_tracked(lib, path)
    except folder_ops._Refused:
        raise _no("restore path is a library item's path", "TARGET_EXISTS") from None
    return path, root


def _place(move, link: bool) -> str:
    """Run one Beets move: MOVE, or for a preserved torrent source HARDLINK,
    then COPY when a hard link is impossible. Never MOVE a linked file."""
    if not link:
        move(MoveOperation.MOVE)
        return "moved"
    try:
        move(MoveOperation.HARDLINK)
        return "linked"
    except util.FilesystemError as exc:  # EXDEV, EPERM (protected_hardlinks), no link support
        ops.log.info("hard link impossible (%s); copying instead", exc)
    move(MoveOperation.COPY)
    return "copied"


def _unlink_ours(lib, path: str, original: str, method: Any, origin: Any, library: Any) -> bool:
    """Remove a library file this relocation made, only while it is provably
    ours, untracked, and the original is still intact at its path."""
    try:
        path, _root = _contained(path)
        folder_ops._refuse_tracked(lib, path)
    except folder_ops._Refused:
        return False
    if not (_ours(path, method, origin, library) and _same_file(original, origin)):
        return False
    try:
        os.unlink(path)
    except OSError:
        return False
    _remove_empty_dirs(os.path.dirname(path))
    return True


def _adoptable(lib, now: str, restore: Any, evidence: Any) -> str:
    """The restore path when a crash between Beets' file move and its row
    store left the row at ``now`` (no file) and this track's file, proven by
    size and mtime, untracked at ``restore``; else ''. Nothing is moved."""
    if os.path.lexists(now):
        return ""
    try:
        target, _root = _contained(restore)
        size, mtime = int(evidence[0]), float(evidence[1])
    except (folder_ops._Refused, TypeError, ValueError, IndexError):
        return ""
    if (os.path.splitext(target)[1].lower() != os.path.splitext(now)[1].lower()
            or os.path.islink(target) or not os.path.isfile(target)):
        return ""
    st = _evidence(target)
    if st is None or st[0] != size or abs(st[1] - mtime) >= 1:
        return ""
    try:
        folder_ops._refuse_tracked(lib, target)
    except folder_ops._Refused:
        return ""
    return target


class _Undone(Exception):
    """The operation failed and every file it had moved was put back."""


def _relocate(lib, album, before, link_ops, art_op) -> Dict[str, Any]:
    evidence = {"items": {k: _evidence(p) for k, p in before["items"].items()},
                "artpath": _evidence(before["artpath"]) if before["artpath"] else None,
                "library": {}, "library_art": None}
    methods: Dict[str, str] = {}
    art_method = ""
    try:
        # Beets' Album.move(), per track: a preserved track links or copies.
        album.store()
        moved_dir = None
        for item in album.items():
            iid, old = str(item.id), item.path
            how = _place(lambda op, it=item: it.move(op, with_album=False), link_ops.get(iid) == "link")
            if item.path != old:
                methods[iid] = how
                evidence["library"][iid] = _evidence(_abs(lib, item.path))
                moved_dir = moved_dir or os.path.dirname(item.path)
        old_art = album.artpath
        how = _place(lambda op: album.move_art(op, item_dir=moved_dir), art_op == "link")
        album.store()
        if album.artpath and album.artpath != old_art:
            art_method = how
            evidence["library_art"] = _evidence(_abs(lib, album.artpath))
    except Exception:
        ops.log.exception("album relocation failed; putting moved files back")
        album = lib.get_album(album.id)
        for item in album.items():
            iid = str(item.id)
            back, now = before["items"].get(iid), _abs(lib, item.path)
            if not back or now == back:
                continue
            if methods.get(iid) in _LINKED:
                if _same_file(back, evidence["items"].get(iid)):
                    _adopt_item(lib, item, back)
                    _unlink_ours(lib, now, back, methods[iid], evidence["items"].get(iid),
                                 evidence["library"].get(iid))
            elif link_ops.get(iid) != "link" and os.path.exists(item.path):
                _move_item(lib, item, back)
        if _state(lib, album)["artpath"] != before["artpath"] or not all(
                os.path.exists(p) for p in before["items"].values()):
            raise
        raise _Undone() from None
    album = lib.get_album(album.id)
    after = _state(lib, album)
    moved = sorted(k for k, p in after["items"].items() if p != before["items"].get(k))
    dest = {str(it.id): _abs(lib, it.destination()) for it in album.items()}
    skipped = sorted(k for k in dest  # Item.move() skips a missing file
                     if k not in moved and dest[k] != before["items"].get(k))
    renamed = sorted(k for k in moved if after["items"][k] != dest[k])  # util.unique_path: name.1.ext
    return {"album_id": album.id, "before": before, "after": after, "moved": moved, "skipped": skipped,
            "renamed": renamed, "evidence": evidence, "methods": methods, "art_method": art_method}


def _check_rollback(lib, data: Dict[str, Any]) -> Tuple[Any, List[Tuple[Any, str, str]], Tuple[str, str]]:
    album = lib.get_album(int(data.get("album_id") or 0))
    if not album:
        raise _no("album not found", "ALBUM_NOT_FOUND")
    live = _state(lib, album)
    wanted = {str(e.get("id")): e for e in (data.get("items") or []) if isinstance(e, dict)}
    if not wanted or set(wanted) != set(live["items"]):
        raise _no("the album's tracks changed since it was relocated", "ALBUM_CHANGED")
    by_id = {str(it.id): it for it in album.items()}
    steps, targets, cleanup = [], set(), []
    for iid, entry in wanted.items():
        now = live["items"][iid]
        restore = entry.get("restore_path")
        method, origin, library = entry.get("method"), entry.get("evidence"), entry.get("library_evidence")
        if restore == now:
            # Never moved (skipped by the apply), or already back (an interrupted
            # rollback): a linked/copied track may still have its library file.
            if method in _LINKED and isinstance(entry.get("path"), str) and entry["path"] != now:
                cleanup.append((entry["path"], now, method, origin, library))
            continue
        if _abs(lib, entry.get("path")) != now:
            raise _no(f"item {iid} was moved again since the relocation", "ITEM_MOVED")
        if method in _LINKED:
            # The original never moved: point the row back, then drop our library file.
            target, root = _original(lib, restore, now, origin)
            if os.path.lexists(now):
                if not _ours(now, method, origin, library):
                    raise _no(f"item {iid}'s library file changed since the relocation", "LIBRARY_FILE_CHANGED")
                cleanup.append((now, target, method, origin, library))
            mode = "repoint"
        elif not os.path.isfile(now):
            # A crash after Beets moved the file back but before the row was stored.
            target, root, mode = _adoptable(lib, now, restore, origin), "", "adopt"
            if not target:
                raise _no(f"item {iid}'s file is missing", "FILE_MISSING")
        else:
            target, root = _safe_target(lib, restore, now)
            mode = "move"
        if target in targets:
            raise _no("two items would be restored to one path", "PATH_INVALID", 400)
        targets.add(target)
        steps.append((by_id[iid], now, target, root, mode))
    art_now, art_back = live["artpath"], str(data.get("restore_artpath") or "")
    art_root, art_mode = "", "move"
    art_method, art_origin = data.get("art_method"), data.get("art_evidence")
    art_library = data.get("art_library_evidence")
    if art_now == art_back:
        # Unchanged by the apply, or already back: a linked/copied cover may still have its library file.
        if art_method in _LINKED and isinstance(data.get("artpath"), str) and data["artpath"] not in ("", art_now):
            cleanup.append((data["artpath"], art_now, art_method, art_origin, art_library))
    elif _abs(lib, data.get("artpath")) != art_now:
        raise _no("the album's cover changed since the relocation", "ART_CHANGED")
    elif art_now and art_back:
        if art_back in targets:
            raise _no("the cover would be restored onto a track", "PATH_INVALID", 400)
        if art_method in _LINKED:
            art_back, art_root = _original(lib, art_back, art_now, art_origin)
            if os.path.lexists(art_now):
                if not _ours(art_now, art_method, art_origin, art_library):
                    raise _no("the album's library cover changed since the relocation", "LIBRARY_FILE_CHANGED")
                cleanup.append((art_now, art_back, art_method, art_origin, art_library))
            art_mode = "repoint"
        elif not os.path.isfile(art_now):
            art_back, art_mode = _adoptable(lib, art_now, art_back, art_origin), "adopt"
            if not art_back:
                raise _no("the album's cover file is missing", "FILE_MISSING")
        else:
            art_back, art_root = _safe_target(lib, art_back, art_now)
    elif art_now and not art_back:
        raise _no("the album's cover changed since the relocation", "ART_CHANGED")
    else:  # Beets dropped a missing cover's artpath: restore the field only
        art_back, _root = _contained(art_back)
        if os.path.splitext(art_back)[1].lower() not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
            raise _no("restore cover path is not an image", "EXTENSION_CHANGED", 400)
        try:
            folder_ops._refuse_tracked(lib, art_back)
        except folder_ops._Refused:
            raise _no("restore cover path is a library item's path", "TARGET_EXISTS") from None
    return album, steps, (art_now, art_back, art_root, art_mode), live, cleanup


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


def _adopt_item(lib, item, dest: str) -> None:
    """Store the row of a track whose file is already at ``dest``; no move."""
    with lib.transaction():
        item.path = os.fsencode(dest)
        item.store()


def _rollback(lib, album, steps, art, live, cleanup) -> Dict[str, Any]:
    art_now, art_back, art_root, art_mode = art
    done: List[Tuple[Any, str, str]] = []
    art_moved = art_set = False
    adopted = repointed = 0
    try:
        for item, now, back, root, mode in steps:
            if mode == "adopt":
                _adopt_item(lib, item, back)  # kept on failure: its row now matches its file
                adopted += 1
                continue
            done.append((item, now, mode))
            if mode == "repoint":
                _adopt_item(lib, item, back)  # the original never moved; nothing moves
                repointed += 1
            else:
                _move_item(lib, item, back, root)
        if art_back != art_now:
            with lib.transaction():
                if art_now and art_mode == "move":
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
        for item, now, mode in reversed(done):
            if mode == "repoint":
                if _abs(lib, item.path) != now:
                    _adopt_item(lib, item, now)  # its library file is still there: cleanup runs last
            elif os.path.exists(item.path) and _abs(lib, item.path) != now:
                _move_item(lib, item, now)
        if _state(lib, lib.get_album(album.id)) != live or not all(os.path.isfile(now) for _i, now, _m in done):
            raise  # not provably all back: ROLLBACK_FAILED, never "nothing changed"
        raise _Undone() from None
    # Rows are final: drop only the library files this relocation made.
    kept = [path for path, original, method, origin, library in cleanup
            if not _unlink_ours(lib, path, original, method, origin, library)]
    for vacated in {os.path.dirname(s[1]) for s in steps} | (
            {os.path.dirname(art_now)} if art_set and art_now else set()):
        _remove_empty_dirs(vacated)
    return {"album_id": album.id, "restored_items": len(steps), "adopted_items": adopted,
            "repointed_items": repointed, "removed_library_files": len(cleanup) - len(kept),
            "kept_library_files": kept, "restored_art": art_set, "after": _state(lib, lib.get_album(album.id))}


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
