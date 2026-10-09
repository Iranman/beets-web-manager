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
Their rollback never moves anything and trusts only this plugin's own
durable record of the apply (``apply_operation_id``, kept
``DURABLE_RETENTION_SECONDS``): methods, paths and evidence come from it,
never from the request. Once the original is proven unchanged (same inode,
size and mtime; else ``SOURCE_CHANGED``) and the library file proven to be
the one this relocation made (the original's inode for a link, else the
recorded library evidence; else ``LIBRARY_FILE_CHANGED``), the row is pointed
back at the original, and only then is the library file unlinked: untracked,
strictly inside the Beets library directory, and re-checked by inode through
an ``O_NOFOLLOW`` directory walk right before ``unlinkat``. A successful
rollback marks the record ``rolled_back``. Without a valid record (expired,
unknown, another album's, failed, or rolled back) a linked/copied track or
cover is never pointed back (``RELOCATION_RECORD_MISSING``: both files stay),
and request evidence never adopts a file. A library file already gone is not
an error.

Copy-on-write (``break_hard_link``, on Beets' ``write`` event, which every
``Item.write``/``try_write`` sends first: Web Manager edits, mbsync, embedart,
``beet write``): a file with more than one link is first replaced by its own
copy (``os.replace`` of a same-folder copy; mode, owner when allowed and times
set on its descriptor), so a tag or embedded-art write never reaches a seeding
torrent's bytes. ``after_write`` refreshes the library evidence in the
relocation record for that same item when the file before the write was the
one the record proved ours, so a rollback still proves and removes the copy. Covers are never written in
place: ``Album.set_art`` removes the name and copies a new file.

Both hold ``ops.mutation_lock``. Refusals and undone failures (``UNDONE``:
every moved file was put back) are not kept under the Idempotency-Key, so a
retry after the operator fixes the cause runs.
"""

from __future__ import annotations

import copy
import os
import shutil
import stat
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from beets import util
from beets.library import WriteError
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


def _ev_of(st: os.stat_result) -> List[Any]:
    return [st.st_size, st.st_mtime, st.st_dev, st.st_ino]


def _same_inode(a: Any, ev: Any) -> bool:
    try:
        return (int(a[2]), int(a[3])) == (int(ev[2]), int(ev[3]))
    except (TypeError, ValueError, IndexError):
        return False


def _matches(a: Any, ev: Any) -> bool:
    """Evidence ``a`` is the file ``ev`` recorded: size, mtime (1 s), dev, inode."""
    try:
        return int(a[0]) == int(ev[0]) and abs(float(a[1]) - float(ev[1])) < 1 and _same_inode(a, ev)
    except (TypeError, ValueError, IndexError):
        return False


def _same_file(path: str, ev: Any) -> bool:
    """``path`` is a regular file (no symlink) matching ``ev`` exactly."""
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return False
    return stat.S_ISREG(st.st_mode) and _matches(_ev_of(st), ev)


def _ours_ev(a: Any, method: Any, origin: Any, library: Any) -> bool:
    """Evidence ``a`` (of a regular file) is the library file this relocation
    made: a hard link of the original (its inode), or the file its recorded
    library evidence matches (a copy, or a link a tag write broke into its own
    copy)."""
    return method in _LINKED and ((method == "linked" and _same_inode(a, origin)) or _matches(a, library))


def _ours(path: str, method: Any, origin: Any, library: Any) -> bool:
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return False
    return stat.S_ISREG(st.st_mode) and _ours_ev(_ev_of(st), method, origin, library)


def _original(lib, restore: Any, now: str, origin: Any) -> Tuple[str, str]:
    """(path, root) of a linked/copied track's untouched original, proven by
    the inode, size and mtime recorded at apply; refuses otherwise."""
    path, root = _contained(restore)
    if os.path.splitext(path)[1].lower() != os.path.splitext(now)[1].lower():
        raise _no("restore path has another file extension", "EXTENSION_CHANGED", 400)
    if not _same_file(path, origin):
        raise _no("the original in the torrent folder is gone or changed since the relocation", "SOURCE_CHANGED")
    try:
        folder_ops._refuse_tracked(lib, path)
    except folder_ops._Refused:
        raise _no("restore path is a library item's path", "TARGET_EXISTS") from None
    return path, root


def _place(lib, move, link: bool, dest: str = "") -> str:
    """Run one Beets move: MOVE, or for a preserved torrent source HARDLINK,
    then COPY when a hard link is impossible. Never MOVE a linked file. A
    failure after the link or copy was made (a partial copy, or the row store)
    leaves a file at ``dest`` no row points at: removed when ``dest`` was free
    before, is untracked and inside the library; else logged."""
    if not link:
        move(MoveOperation.MOVE)
        return "moved"
    was_free = bool(dest) and not os.path.lexists(dest)
    try:
        try:
            move(MoveOperation.HARDLINK)
            return "linked"
        except util.FilesystemError as exc:  # EXDEV, EPERM (protected_hardlinks), no link support
            ops.log.info("hard link impossible ({}); copying instead", exc)
        move(MoveOperation.COPY)
        return "copied"
    except Exception:
        if was_free and os.path.lexists(dest) and not _unlink_new(lib, dest):
            ops.log.warning("a failed link or copy left an untracked file in the library: {}", dest)
        raise


def _lib_root(lib) -> str:
    return os.path.abspath(os.fsdecode(lib.directory))


def _unlink_at(root: str, path: str, ident: Tuple[int, int]) -> bool:
    """Unlink ``path`` (strictly below ``root``) only while it is still the
    regular file ``ident`` (dev, inode). The parent is reached from ``root``
    with ``O_NOFOLLOW`` directory fds, so a folder swapped for a symlink after
    the checks is refused, and the inode is re-checked right before
    ``unlinkat``."""
    parts = os.path.relpath(path, root).split(os.sep)
    if os.unlink not in os.supports_dir_fd:  # ponytail: no dir_fd (Windows): path-based, same checks
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != ident:
            return False
        os.unlink(path)
        return True
    flags = os.O_RDONLY | os.O_DIRECTORY
    fd = os.open(root, flags)
    try:
        for name in parts[:-1]:
            nfd = os.open(name, flags | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
        st = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != ident:
            return False
        os.unlink(parts[-1], dir_fd=fd)
        return True
    finally:
        os.close(fd)


def _unlink_new(lib, path: str) -> bool:
    """Remove an untracked file inside the Beets library directory (one this
    relocation just created). Never anything outside the library."""
    root = _lib_root(lib)
    try:
        path = folder_ops.contained_path(path, root)
        folder_ops._refuse_tracked(lib, path)
        st = os.lstat(path)
        return _unlink_at(root, path, (st.st_dev, st.st_ino))
    except (folder_ops._Refused, OSError):
        return False


def _unlink_ours(lib, path: str, original: str, method: Any, origin: Any, library: Any) -> bool:
    """Remove a library file this relocation made, only while it is provably
    ours, untracked, strictly inside the Beets library directory, and the
    original is still intact at its own (other) path."""
    root = _lib_root(lib)
    try:
        path, _root = _contained(path)
        path = folder_ops.contained_path(path, root)
        folder_ops._refuse_tracked(lib, path)
    except folder_ops._Refused:
        return False
    try:
        st = os.lstat(path)  # one lstat: the identity proven ours is the one unlinked
        if (path == original or not stat.S_ISREG(st.st_mode)
                or not (_ours_ev(_ev_of(st), method, origin, library) and _same_file(original, origin))
                or not _unlink_at(root, path, (st.st_dev, st.st_ino))):
            return False
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
            how = _place(lib, lambda op, it=item: it.move(op, with_album=False), link_ops.get(iid) == "link",
                         _abs(lib, item.destination()))
            if item.path != old:
                methods[iid] = how
                evidence["library"][iid] = _evidence(_abs(lib, item.path))
                moved_dir = moved_dir or os.path.dirname(item.path)
        old_art = album.artpath
        art_dest = _abs(lib, album.art_destination(old_art, item_dir=moved_dir)) if old_art else ""
        how = _place(lib, lambda op: album.move_art(op, item_dir=moved_dir), art_op == "link", art_dest)
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


def _apply_record(op_id: Any, album_id: int) -> Optional[Dict[str, Any]]:
    """This plugin's own durable result of the relocation being rolled back
    (its Idempotency-Key), or None when it is unknown, expired, or already
    rolled back (a replay then proves nothing and removes nothing)."""
    if not isinstance(op_id, str) or not op_id:
        return None
    with ops._operations_lock:
        op = ops._operations.get(op_id)
        if (not op or op.get("type") != "album_relocation" or op.get("status") != "succeeded"
                or (op.get("result") or {}).get("rolled_back")):
            return None
        rec = copy.deepcopy(op.get("result") or {})
    return rec if rec.get("album_id") == album_id else None


def _from_record(rec: Dict[str, Any], iid: str, restore: Any) -> Tuple[Any, Tuple[Any, Any], str]:
    """(method, (original evidence, library evidence), library path made) of
    one track, from the record; a linked/copied track must be restored to the
    original path the record holds."""
    ev = rec.get("evidence") or {}
    method = (rec.get("methods") or {}).get(iid)
    if method in _LINKED and restore != ((rec.get("before") or {}).get("items") or {}).get(iid):
        raise _no(f"item {iid}'s restore path is not the one the relocation recorded", "STALE_PLAN")
    return (method, ((ev.get("items") or {}).get(iid), (ev.get("library") or {}).get(iid)),
            ((rec.get("after") or {}).get("items") or {}).get(iid) or "")


def _check_rollback(lib, data: Dict[str, Any]) -> Tuple[Any, List[Tuple[Any, str, str]], Tuple[str, str]]:
    album = lib.get_album(int(data.get("album_id") or 0))
    if not album:
        raise _no("album not found", "ALBUM_NOT_FOUND")
    live = _state(lib, album)
    wanted = {str(e.get("id")): e for e in (data.get("items") or []) if isinstance(e, dict)}
    if not wanted or set(wanted) != set(live["items"]):
        raise _no("the album's tracks changed since it was relocated", "ALBUM_CHANGED")
    by_id = {str(it.id): it for it in album.items()}
    rec = _apply_record(data.get("apply_operation_id"), album.id)
    steps, targets, cleanup = [], set(), []
    for iid, entry in wanted.items():
        now = live["items"][iid]
        restore = entry.get("restore_path")
        if rec is not None:  # the plugin's own record decides; payload copies are ignored
            method, (origin, library), made = _from_record(rec, iid, restore)
        else:  # no record: payload evidence proves nothing; only a plain move goes back
            method, origin, library, made = entry.get("method"), None, None, ""
        if restore == now:
            # Never moved (skipped by the apply), or already back (an interrupted
            # rollback): a linked/copied track may still have its library file.
            if method in _LINKED and made and made != now and os.path.lexists(made):
                cleanup.append((made, now, method, origin, library))
            continue
        if _abs(lib, entry.get("path")) != now:
            raise _no(f"item {iid} was moved again since the relocation", "ITEM_MOVED")
        if method in _LINKED and rec is None:
            raise _no(f"item {iid} was linked or copied and this relocation's record is gone; both files "
                      "are kept as they are", "RELOCATION_RECORD_MISSING")
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
    if rec is not None:
        ev = rec.get("evidence") or {}
        art_method, art_origin, art_library = rec.get("art_method"), ev.get("artpath"), ev.get("library_art")
        art_made = (rec.get("after") or {}).get("artpath") or ""
        if art_method in _LINKED and art_back != (rec.get("before") or {}).get("artpath"):
            raise _no("the cover's restore path is not the one the relocation recorded", "STALE_PLAN")
    else:
        art_method, art_origin, art_library, art_made = data.get("art_method"), None, None, ""
    if art_now == art_back:
        # Unchanged by the apply, or already back: a linked/copied cover may still have its library file.
        if art_method in _LINKED and art_made and art_made != art_now and os.path.lexists(art_made):
            cleanup.append((art_made, art_now, art_method, art_origin, art_library))
    elif _abs(lib, data.get("artpath")) != art_now:
        raise _no("the album's cover changed since the relocation", "ART_CHANGED")
    elif art_now and art_back:
        if art_back in targets:
            raise _no("the cover would be restored onto a track", "PATH_INVALID", 400)
        if art_method in _LINKED and rec is None:
            raise _no("the cover was linked or copied and this relocation's record is gone; both files are kept "
                      "as they are", "RELOCATION_RECORD_MISSING")
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
    return album, steps, (art_now, art_back, art_root, art_mode), live, cleanup, (
        data.get("apply_operation_id") if rec is not None else "")


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


def _rollback(lib, album, steps, art, live, cleanup, apply_id) -> Dict[str, Any]:
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
    if apply_id:  # a replay (any key) then proves nothing and removes nothing
        with ops._operations_lock:
            op = ops._operations.get(apply_id)
            if op and isinstance(op.get("result"), dict):
                op["result"]["rolled_back"] = True
                ops._save_durable_locked()
    return {"album_id": album.id, "restored_items": len(steps), "adopted_items": adopted,
            "repointed_items": repointed, "removed_library_files": len(cleanup) - len(kept),
            "kept_library_files": kept, "restored_art": art_set, "after": _state(lib, lib.get_album(album.id))}


_pre_write: Dict[str, Any] = {}  # path -> evidence of the file just before Beets wrote it


def break_hard_link(item=None, path=None, tags=None, **_kw) -> None:
    """Beets ``write`` listener: before a tag write, give a file with more
    than one link its own inode, so the write never reaches the other name
    (a seeding torrent's file). The copy is made in the same folder; mode,
    owner (when allowed) and times are set on its open descriptor, never by
    name, and both names are re-checked by inode right before ``os.replace``.
    Any failure raises ``WriteError``: ``try_write`` logs it and skips the
    write, so the shared bytes are never written. Also notes the file's
    identity before the write, for ``note_library_write``."""
    p = os.fsdecode(path if path is not None else item.path)
    key = os.path.normpath(p)
    try:
        st = os.lstat(p)
    except OSError:
        return  # Beets reports the missing file itself
    if not stat.S_ISREG(st.st_mode):
        return
    _pre_write[key] = _ev_of(st)
    if st.st_nlink < 2:
        return
    try:
        fd, tmp = tempfile.mkstemp(prefix=".webmanager-cow-", dir=os.path.dirname(p))
    except OSError as exc:
        raise WriteError(util.bytestring_path(p), exc) from exc
    try:
        with os.fdopen(fd, "wb") as out, open(p, "rb") as src:
            if (os.fstat(src.fileno()).st_dev, os.fstat(src.fileno()).st_ino) != (st.st_dev, st.st_ino):
                raise OSError("the file changed while its hard link was being broken")
            shutil.copyfileobj(src, out)
            out.flush()
            ofd = out.fileno()
            if hasattr(os, "fchown"):  # POSIX: on the descriptor, never by name
                os.fchmod(ofd, stat.S_IMODE(st.st_mode))
                try:
                    os.fchown(ofd, st.st_uid, st.st_gid)
                except OSError:
                    pass  # not allowed: the copy keeps Beets' own owner
                os.utime(ofd, ns=(st.st_atime_ns, st.st_mtime_ns))
            os.fsync(ofd)
            made = os.fstat(ofd)
        tst = os.lstat(tmp)
        if (tst.st_dev, tst.st_ino) != (made.st_dev, made.st_ino):
            raise OSError("the copy was swapped while a hard link was being broken")
        if not hasattr(os, "fchown"):  # ponytail: Windows has no fchown/fd utime; by name, after the check above
            shutil.copystat(p, tmp, follow_symlinks=False)
        now = os.lstat(p)
        if not stat.S_ISREG(now.st_mode) or (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino):
            raise OSError("the file was swapped while its hard link was being broken")
        os.replace(tmp, p)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise WriteError(util.bytestring_path(p), exc) from exc
    ops.log.info("broke a hard link before a tag write: {} now has its own copy", p)
    _refresh_library_evidence(item, key, _ev_of(st))
    _pre_write[key] = _evidence(key)


def note_library_write(item=None, path=None, **_kw) -> None:
    """Beets ``after_write`` listener: see ``_refresh_library_evidence``."""
    key = os.path.normpath(os.fsdecode(path if path is not None else item.path))
    _refresh_library_evidence(item, key, _pre_write.pop(key, None))


def _refresh_library_evidence(item, p: str, pre: Any) -> None:
    """When ``item`` was written at the library path a relocation linked or
    copied for that same item, and the file just before the write (``pre``)
    was the one the record proves ours, record its new size, mtime and inode
    in that relocation's durable result, so its rollback still proves the
    file is ours. Anything else (another item or file at that path, no
    pre-write identity, a rolled-back record) leaves the record as it was,
    and the rollback then keeps the file (``LIBRARY_FILE_CHANGED``) rather
    than guess. Only in the process serving Web Manager (any webmanager
    request binds the registry); a ``beet write`` elsewhere changes nothing."""
    iid = str(getattr(item, "id", None) or "")
    if not ops._durable_file or not iid or pre is None:
        return
    ev = _evidence(p)
    changed = False
    with ops._operations_lock:
        # ponytail: linear scan of at most MAX_COMPLETED_OPERATIONS records per write; index by path if it shows up
        for op in ops._operations.values():
            rec = op.get("result") if op.get("type") == "album_relocation" else None
            if not isinstance(rec, dict) or op.get("status") != "succeeded" or rec.get("rolled_back"):
                continue
            evs = rec.get("evidence") or {}
            if (((rec.get("after") or {}).get("items") or {}).get(iid) == p
                    and _ours_ev(pre, (rec.get("methods") or {}).get(iid), (evs.get("items") or {}).get(iid),
                                 (evs.get("library") or {}).get(iid))):
                rec.setdefault("evidence", {}).setdefault("library", {})[iid] = ev
                changed = True
        if changed:
            ops._save_durable_locked()


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
