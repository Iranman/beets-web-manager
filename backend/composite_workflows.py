"""Composite Workflow Engine for Beets Web Manager.

Orchestrates multi-step, user-confirmed workflows across stock Beets:
- Web Manager owns planning, preview, audit trails, checkpoints, and rollback.
- Stock Beets owns library state mutations via BeetsAdapter (/webmanager/* endpoints).
- Zero raw SQLite access to musiclibrary.blb.
- Zero Docker socket access.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import errno
import hashlib
import inspect
import json
import logging
import os
import re
import shutil
import stat
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from backend.beets_adapter import (
    BeetsAdapter,
    BeetsAdapterAuthError,
    BeetsAdapterConnectionError,
    BeetsAdapterError,
    BeetsAdapterNotFoundError,
    BeetsAdapterTimeoutError,
    BeetsAuthError,
    BeetsBadRequestError,
    BeetsClient,
    BeetsClientError,
    BeetsCommandError,
    BeetsError,
    BeetsNotFoundError,
    BeetsUnavailableError,
    RemoteAlbum,
    RemoteItem,
    StockBeetsLibrary,
    beets_adapter,
    lib,
)
from backend.config_manager import (
    ConfigConflictError,
    ConfigError,
    ConfigValidationError,
    get_config,
    revert_config,
    save_config,
)
from backend.transaction_engine import ROLLBACK_UNAPPLIED_STATUSES, TransactionStore

log = logging.getLogger("beets.workflows")

AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".aiff", ".aif", ".wma", ".alac", ".ape"
}

_default_store: Optional[TransactionStore] = None


def get_default_store() -> TransactionStore:
    """Resolve the global default TransactionStore instance."""
    global _default_store
    if _default_store is None:
        data_root = os.environ.get("WEB_MANAGER_DATA_DIR", "/web-manager-data")
        tx_dir = os.environ.get("BEETS_TRANSACTION_DIR", f"{data_root}/transactions")
        _default_store = TransactionStore(tx_dir)
    return _default_store


def _get_store(store: Optional[TransactionStore]) -> TransactionStore:
    return store or get_default_store()


def _decode_path(val: Any) -> str:
    if isinstance(val, (bytes, bytearray)):
        return val.decode("utf-8", errors="replace")
    return str(val or "")


def _s(val: Any) -> str:
    return _decode_path(val)


# -----------------------------------------------------------------------------
# Status compare-and-set for the adapter-backed families (#218, #224)
# -----------------------------------------------------------------------------


def _claim_apply(st: TransactionStore, operation_id: str, **updates: Any) -> Optional[Dict[str, Any]]:
    """CAS Approved|Preview -> Running before an apply writes anything (#218).
    None when the transaction is in any other status: a cancel, a second
    apply or a finished run won, and nothing may be written."""
    for source in ("Approved", "Preview"):
        tx = st.transition(operation_id, source, "Running", **updates)
        if tx is not None:
            return tx
    return None


def _apply_refused(st: TransactionStore, operation_id: str) -> Dict[str, Any]:
    status = st.get(operation_id).get("status")
    return {"ok": False, "code": "not_applicable", "operation_id": operation_id, "status": status,
            "mutated": False,
            "error": f"Only a Preview or Approved transaction can be applied (this one is {status}); nothing was changed."}


def _apply_claimed(st: TransactionStore, operation_id: str,
                   write: Callable[[], Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    """The plain adapter-backed apply families (#218): CAS Preview|Approved
    -> Running before ``write`` runs (refused, writing nothing, otherwise),
    then Running -> Completed, or Failed when ``write`` returns ok=False
    (``mutated: False`` there means it wrote nothing, so no apply record is
    kept). ``write`` may return a ``log`` line and extra result fields. An
    exception ends Failed -- Recovery Required when a transport error leaves
    the outcome unknown -- and is re-raised for the caller's error mapping."""
    if _claim_apply(st, operation_id, metadata={"engine_result": {"mutation_started": True}}) is None:
        return _apply_refused(st, operation_id)
    try:
        res = dict(write() or {})
    except Exception as exc:
        unknown = _transport_error(exc)
        st.transition(operation_id, "Running", "Recovery Required" if unknown else "Failed",
                      logs=[f"Apply raised {type(exc).__name__}"
                            + ("; the outcome is unknown, so it is not retried." if unknown else ".")])
        raise
    logs = {"logs": [res.pop("log")]} if res.get("log") else {}
    if res.get("ok") is False:
        clear = {"metadata": {"engine_result": None}} if res.get("mutated") is False else {}
        st.transition(operation_id, "Running", "Failed", **(logs or {"logs": [_s(res.get("error"))]}), **clear)
        return {**res, "operation_id": operation_id, "status": "Failed"}
    st.transition(operation_id, "Running", "Completed", **logs)
    return {**res, "ok": True, "operation_id": operation_id, "status": "Completed"}


def _claim_rollback(st: TransactionStore, operation_id: str, to: str = "Running") -> Optional[Dict[str, Any]]:
    """CAS Completed (or Failed with an apply record) -> ``to`` (#224). A
    Preview, Approved or Cancelled transaction never wrote anything, so it is
    never marked Rolled Back."""
    tx = st.get(operation_id)
    applied_failed = tx.get("status") == "Failed" and (tx.get("metadata") or {}).get("engine_result")
    source = "Failed" if applied_failed else "Completed"
    claimed = st.transition(operation_id, source, to)
    return None if claimed is None else {**claimed, "claimed_from": source}


def _rollback_refused(st: TransactionStore, operation_id: str) -> Dict[str, Any]:
    status = st.get(operation_id).get("status")
    return {"ok": False, "code": "rollback_not_eligible", "operation_id": operation_id, "status": status,
            "error": f"Only an applied transaction can be rolled back (status is {status})."}


def engine_rollback_refusal(tx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Refusal for an engine-family rollback of ``tx`` in an unapplied
    status, or None. The caller then finishes with a CAS from the status it
    read (``st.transition(id, tx["status"], ...)``)."""
    status = tx.get("status")
    if status not in ROLLBACK_UNAPPLIED_STATUSES:
        return None
    return {"ok": False, "code": "rollback_not_eligible", "operation_id": tx.get("id"), "status": status,
            "mutated": False, "error": f"Only an applied transaction can be rolled back (status is {status})."}


def _rollback_conflict(operation_id: str) -> Dict[str, Any]:
    return {"ok": False, "code": "conflict", "operation_id": operation_id,
            "error": "The transaction changed state during rollback; reload and check it."}


def _rollback_noop(operation_id: str, store: Optional[TransactionStore]) -> Dict[str, Any]:
    """Rollback for families that record nothing to restore: CAS only (#224).
    ponytail: marks Rolled Back without restoring anything; TECHNICAL_DEBT
    tracks giving these families real before-state capture."""
    st = _get_store(store)
    if _claim_rollback(st, operation_id, "Rolled Back") is None:
        return _rollback_refused(st, operation_id)
    return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}


#: Fields a rollback never writes back: file/row bookkeeping, not metadata.
_RESTORE_SKIP = frozenset({
    "id", "album_id", "path", "artpath", "mtime", "added", "items", "length", "bitrate", "bitrate_mode",
    "bitdepth", "samplerate", "channels", "format", "filesize", "encoder", "encoder_info", "encoder_settings",
})


def _restorable(row: Optional[Dict[str, Any]], fields: Optional[Any] = None) -> Dict[str, Any]:
    """Scalar metadata of a Beets row (optionally only ``fields``) for a
    before-state snapshot."""
    row = row or {}
    keys = list(fields) if fields is not None else list(row.keys())
    return {k: row.get(k) for k in keys
            if k not in _RESTORE_SKIP and (row.get(k) is None or isinstance(row.get(k), (str, int, float, bool)))}


def _restore_rows(ad: BeetsAdapter, before: Dict[str, Any], write: bool) -> Tuple[int, int]:
    """Write captured values back where they differ from the live row.
    ``before`` = {"album": {"id", "fields"}, "items": [{"id", "fields"}]}.
    Returns (restored rows, failed rows)."""
    restored = failed = 0
    album = before.get("album") or {}
    targets = [("album", album)] if album.get("id") else []
    targets += [("item", it) for it in before.get("items") or []]
    for kind, snap in targets:
        rid = int(snap["id"])
        try:
            live = ad.get_album(rid) if kind == "album" else ad.get_item(rid)
            if not live:
                failed += 1
                continue
            diff = {k: v for k, v in (snap.get("fields") or {}).items() if live.get(k) != v}
            if diff:
                ids = {"album_ids": [rid]} if kind == "album" else {"item_ids": [rid]}
                res = ad.modify(fields=diff, write=write, move=True, **ids)
                if isinstance(res, dict) and res.get("ok") is False:
                    failed += 1
                    continue
            restored += 1
        except Exception:
            failed += 1
    return restored, failed


def _finish_rollback(st: TransactionStore, operation_id: str, restored: int, failed: int) -> Dict[str, Any]:
    final = "Rolled Back" if not failed else ("Partially Rolled Back" if restored else "Failed")
    st.transition(operation_id, "Running", final, counts={"rollback_ok": restored, "rollback_failed": failed})
    return {"ok": not failed, "operation_id": operation_id, "status": final,
            "rollback_ok": restored, "rollback_failed": failed}


# -----------------------------------------------------------------------------
# Path & Staging Utilities
# -----------------------------------------------------------------------------


def _data_dir() -> Path:
    return Path(os.environ.get("WEB_MANAGER_DATA_DIR", "/web-manager-data")).resolve()


def _get_staging_roots() -> List[Path]:
    """Roots under which staging helpers may touch files: the configured
    downloads root (``config_layers.downloads_root``) plus
    ``<data dir>/playlist_staging``. The data directory itself is NOT a
    staging root -- it holds transactions.db, caches and backups (S1/F1)."""
    from backend.config_layers import downloads_root, music_root, safe_roots
    # #235: a downloads root that is "/" or overlaps the library is dropped.
    return [Path(r).resolve() for r in safe_roots("DOWNLOADS_ROOT", [downloads_root()], music_root())] + [
        (_data_dir() / "playlist_staging").resolve()]


_PROTECTED_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3", ".blb")


def _is_protected_data_path(resolved: Path) -> bool:
    """True for paths a staging helper must never delete or move (S1/F1):
    the data dir, any ancestor of it, anything inside it outside
    ``playlist_staging``, its backups, and any database file anywhere."""
    data_dir = _data_dir()
    if resolved == data_dir or resolved in data_dir.parents:
        return True
    name = resolved.name.lower()
    if name.endswith(_PROTECTED_SUFFIXES):
        return True
    if data_dir in resolved.parents:
        staging = (data_dir / "playlist_staging").resolve()
        if resolved != staging and staging not in resolved.parents:
            return True
    backups = (data_dir / "backups").resolve()
    if resolved == backups or backups in resolved.parents:
        return True
    return False


def _is_within_music_root(path: Union[str, Path]) -> bool:
    """True if path resolves inside MUSIC_ROOT -- the only tree these
    artist/album folder-inventory helpers are meant to walk or list."""
    music_root = Path(os.environ.get("MUSIC_ROOT", "/music")).resolve()
    try:
        p = Path(path).resolve()
    except Exception:
        return False
    return p == music_root or music_root in p.parents


def _has_symlink_component(path: Union[str, Path]) -> bool:
    """True if the path itself or any existing parent is a symlink. Relative
    paths are absolutized first so every real parent is checked (S1/F4).

    Every lexical prefix is checked twice (#265 QA): as written, because the
    kernel follows ``link`` in ``root/link/..`` before applying ``..``; and
    normalized, because resolve() applies ``..`` lexically after a missing or
    non-directory component, so ``root/nx/../link`` follows ``link`` though
    lstat of the written form fails."""
    p = Path(path)
    if not p.is_absolute():
        p = Path(os.getcwd()) / p
    for prefix in [p, *p.parents]:
        for candidate in (prefix, Path(os.path.normpath(str(prefix)))):
            try:
                if candidate.is_symlink():
                    return True
            except OSError:
                return True
    return False


def _music_root() -> Path:
    from backend.config_layers import music_root
    return Path(music_root()).resolve()


def _staging_contained_text(path: Union[str, Path]) -> Optional[str]:
    """The normalized absolute text of ``path`` if it is a staging root or
    lies under one (component-wise: ``root + os.sep`` prefix, so
    ``/downloads2`` is not under ``/downloads``), else None. No filesystem
    access happens before this check (CodeQL #1373). Roots are resolved; an
    accepted path is compared as text, which matches its resolved form
    because _is_safe_staging_path also refuses any symlinked component."""
    norm = os.path.normpath(os.path.abspath(str(path)))
    for stg in _get_staging_roots():
        root_text = str(stg)
        if norm == root_text:
            return root_text
        if norm.startswith(os.path.join(root_text, "")):
            return norm
    return None


def _is_safe_staging_path(path: Union[str, Path]) -> bool:
    contained = _staging_contained_text(path)
    if contained is None:
        return False
    if _has_symlink_component(path):
        return False
    p = Path(contained).resolve()
    music_root = Path(os.environ.get("MUSIC_ROOT", "/music")).resolve()
    # Must NOT be the music root, inside it, or an ancestor of it (#182)
    try:
        if p == music_root or music_root in p.parents or p in music_root.parents:
            return False
    except Exception:
        return False
    # Never the data dir, its databases or backups (S1/F1)
    if _is_protected_data_path(p):
        return False
    # Must be within allowed staging roots
    for stg in _get_staging_roots():
        try:
            if p == stg or stg in p.parents:
                return True
        except Exception:
            pass
    return False


def _is_staging_root(resolved: Path) -> bool:
    return any(resolved == root for root in _get_staging_roots())


def _same_entry(before: os.stat_result, now: os.stat_result) -> bool:
    return (now.st_ino, now.st_dev, stat.S_IFMT(now.st_mode)) == (
        before.st_ino, before.st_dev, stat.S_IFMT(before.st_mode))


class _ValidatedPath(type(Path())):
    """A resolved staging path carrying the lstat identity seen when it was
    validated (``None`` if it did not exist then). Paths derived from it
    (``.parent``, ``/``) carry no identity (#182)."""
    identity: Optional[os.stat_result] = None


def _validated_staging_target(path: Union[str, Path], what: str) -> Path:
    """Resolve once, refuse anything outside staging, a staging root itself,
    protected data, or a symlink. Returns the resolved path to operate on,
    with its lstat identity captured for the destructive helpers (#182)."""
    raw = Path(path)
    if not _is_safe_staging_path(raw):
        raise ValueError(f"Refusing to {what} outside staging roots: {path}")
    resolved = _ValidatedPath(raw.resolve())
    try:
        resolved.identity = os.lstat(str(resolved))
    except FileNotFoundError:
        pass
    if _is_staging_root(resolved):
        raise ValueError(f"Refusing to {what} a staging root itself: {path}")
    if _is_protected_data_path(resolved) or _has_symlink_component(resolved):
        raise ValueError(f"Refusing to {what} a protected path: {path}")
    return resolved


def _require_dir_fd_support() -> None:
    """Staging mutations are fd-relative only (#206 F1). Fail closed where the
    platform cannot do that; never fall back to path-based operations."""
    ops = (os.open, os.stat, os.mkdir, os.rmdir, os.unlink, os.rename)
    if not (hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")
            and all(op in os.supports_dir_fd for op in ops)
            and shutil.rmtree.avoids_symlink_attacks):
        raise ValueError("Refusing a staging mutation: fd-relative filesystem operations are unavailable.")


def _staging_parts(resolved: Path) -> Tuple[Path, Tuple[str, ...]]:
    """The most specific staging root strictly above ``resolved``, and the
    path components below it."""
    roots = [r for r in _get_staging_roots() if r in resolved.parents]
    if not roots:
        raise ValueError(f"Refusing to touch a path outside staging roots: {resolved}")
    root = max(roots, key=lambda r: len(r.parts))
    return root, resolved.relative_to(root).parts


@contextlib.contextmanager
def _staging_dir_fd(root: Path, parts: Tuple[str, ...], created: Optional[List[Tuple[int, str]]] = None):
    """Yield an fd for ``root/parts...``, opened one component at a time with
    O_DIRECTORY|O_NOFOLLOW so a component swapped for a symlink is refused,
    not followed (#206 F1). With ``created`` given, missing components are
    made with ``mkdir(dir_fd=)``; if the caller's block then raises, those
    directories are removed again while still empty (#206 F2)."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fds = [os.open(str(root), flags)]
    try:
        for name in parts:
            try:
                fd = os.open(name, flags, dir_fd=fds[-1])
            except FileNotFoundError:
                if created is None:
                    raise
                os.mkdir(name, dir_fd=fds[-1])
                created.append((fds[-1], name))
                fd = os.open(name, flags, dir_fd=fds[-1])
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ValueError("Refusing to operate through a symlink or non-directory component.") from exc
                raise
            fds.append(fd)
        yield fds[-1]
    except BaseException:
        for parent_fd, name in reversed(created or []):
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                pass
        raise
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _check_leaf(parent_fd: int, name: str, resolved: Path) -> os.stat_result:
    """Right before a destructive call: ``name`` in ``parent_fd`` must still be
    the entry :func:`_validated_staging_target` saw (st_dev/st_ino/type). An
    unvalidated path is refused (S1/F3, #182)."""
    expected = getattr(resolved, "identity", None)
    try:
        now = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        now = None
    if expected is None or now is None or not _same_entry(expected, now):
        raise ValueError(f"Path changed since validation: {resolved}")
    return now


def _remove_resolved(resolved: Path) -> None:
    """Delete a path returned by :func:`_validated_staging_target`, fd-relative
    from its staging root, so a parent swapped for a symlink cannot redirect
    the delete (#206 F1)."""
    _require_dir_fd_support()
    root, parts = _staging_parts(resolved)
    with _staging_dir_fd(root, parts[:-1]) as parent_fd:
        now = _check_leaf(parent_fd, parts[-1], resolved)
        if stat.S_ISDIR(now.st_mode):
            shutil.rmtree(parts[-1], dir_fd=parent_fd)
        else:
            os.unlink(parts[-1], dir_fd=parent_fd)


def _copy_file_across_fs(src_fd: int, src_name: str, dst_fd: int, dst_name: str,
                         expected: os.stat_result) -> None:
    """EXDEV fallback for :func:`_move_resolved` (staging roots may be on
    different mounts). Regular files only; a directory is refused and nothing
    is moved. The source is opened O_NOFOLLOW and identity-checked, the target
    is created O_CREAT|O_EXCL|O_NOFOLLOW, the copy is fsynced, and the source
    is unlinked only if it is still the same entry; otherwise the copy is
    removed and the source is left in place."""
    if not stat.S_ISREG(expected.st_mode):
        raise ValueError("Refusing a cross-filesystem move of a directory; nothing was moved.")
    sfd = os.open(src_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=src_fd)
    try:
        st = os.fstat(sfd)
        if not _same_entry(expected, st):
            raise ValueError("Source changed since validation; nothing was moved.")
        dfd = os.open(dst_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dst_fd)
        try:
            with open(sfd, "rb", closefd=False) as fin, open(dfd, "wb", closefd=False) as fout:
                shutil.copyfileobj(fin, fout, 1 << 20)
            os.fchmod(dfd, stat.S_IMODE(st.st_mode))
            os.utime(dfd, ns=(st.st_atime_ns, st.st_mtime_ns))
            os.fsync(dfd)
            os.close(dfd)
            dfd = -1
            now = os.stat(src_name, dir_fd=src_fd, follow_symlinks=False)
            if not _same_entry(st, now):
                raise ValueError("Source changed during the copy; nothing was moved.")
        except BaseException:
            if dfd >= 0:
                os.close(dfd)
            os.unlink(dst_name, dir_fd=dst_fd)
            raise
        os.unlink(src_name, dir_fd=src_fd)
    finally:
        os.close(sfd)


def _move_resolved(src: Path, dst: Path) -> None:
    """Move a validated source to a validated target, fd-relative from their
    staging roots (#206 F1). Every refusal happens before a target parent is
    created, and parents created for a move that then fails are removed
    (#182, #206 F2). On one filesystem this is a single ``rename``; across
    filesystems a regular file is copied then unlinked through fds
    (:func:`_copy_file_across_fs`) and a directory is refused."""
    _require_dir_fd_support()
    if getattr(dst, "identity", None) is not None or _has_symlink_component(dst.parent) \
            or os.path.lexists(str(dst)):
        raise ValueError(f"Refusing to move onto an existing or symlinked target: {dst}")
    s_root, s_parts = _staging_parts(src)
    d_root, d_parts = _staging_parts(dst)
    with _staging_dir_fd(s_root, s_parts[:-1]) as s_fd:
        expected = _check_leaf(s_fd, s_parts[-1], src)
        with _staging_dir_fd(d_root, d_parts[:-1], created=[]) as d_fd:
            try:
                os.stat(d_parts[-1], dir_fd=d_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError(f"Refusing to move onto an existing or symlinked target: {dst}")
            try:
                os.rename(s_parts[-1], d_parts[-1], src_dir_fd=s_fd, dst_dir_fd=d_fd)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                _copy_file_across_fs(s_fd, s_parts[-1], d_fd, d_parts[-1], expected)


def delete_staging_file(path: str) -> Dict[str, Any]:
    """Delete a file safely within staging/download roots (never inside the
    music library, never the data dir or a staging root). A failed delete
    raises -- it is never reported as success."""
    p = Path(path)
    if not p.exists() and not p.is_symlink():
        return {"ok": True, "deleted": False, "message": "File does not exist"}
    resolved = _validated_staging_target(p, "delete")
    _remove_resolved(resolved)
    return {"ok": True, "deleted": True, "path": str(resolved)}


def move_staging_file(src: str, dst: str) -> Dict[str, Any]:
    """Move a file safely within staging roots."""
    if not Path(src).exists():
        raise FileNotFoundError(f"Source file does not exist: {src}")
    p_src = _validated_staging_target(src, "move")
    p_dst = _validated_staging_target(dst, "move to")
    _move_resolved(p_src, p_dst)
    return {"ok": True, "source": str(p_src), "destination": str(p_dst)}


def write_staging_tags(path: str, tags: Dict[str, Any]) -> Dict[str, Any]:
    """Write hint tags to a playlist download BEFORE Beets imports it.

    The one documented exception to "tags are written by Beets" (see
    docs/ARCHITECTURE.md, Non-Negotiable Rules): the file is not in the
    library yet, and Beets' importer re-tags it from MusicBrainz on import.
    Confined like the other staging helpers: the path must be a regular file
    under a staging root (downloads root or ``<data dir>/playlist_staging``),
    never under MUSIC_ROOT, never protected data, with no symlinked component
    and no second hard link.
    It is opened fd-relative with O_NOFOLLOW and must still be the validated
    entry, so a component swapped for a symlink cannot redirect the write.
    A refusal raises ValueError; a tag error returns ``ok: False``."""
    resolved = _validated_staging_target(path, "tag")
    expected = getattr(resolved, "identity", None)
    if expected is None or not stat.S_ISREG(expected.st_mode):
        raise FileNotFoundError(f"File not found: {path}")
    _require_dir_fd_support()
    root, parts = _staging_parts(resolved)
    with _staging_dir_fd(root, parts[:-1]) as parent_fd:
        try:
            fd = os.open(parts[-1], os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY, dir_fd=parent_fd)
        except OSError as exc:
            raise ValueError(f"Path changed since validation: {resolved}") from exc
        with open(fd, "r+b") as fh:
            opened = os.fstat(fh.fileno())
            if not _same_entry(expected, opened):
                raise ValueError(f"Path changed since validation: {resolved}")
            # Tags are rewritten in place: a hardlink (e.g. one made for
            # seeding) would change the library file sharing the inode.
            if opened.st_nlink != 1:
                raise ValueError(f"Refusing to write tags to a hardlinked file: {resolved}")
            try:
                import mediafile
                mf = mediafile.MediaFile(fh)
                for k, v in tags.items():
                    if hasattr(mf, k):
                        setattr(mf, k, v)
                fh.seek(0)  # mutagen re-reads the file object on save
                mf.save()
            except Exception as exc:
                log.warning("Failed to write tags to %s: %s", resolved, exc)
                return {"ok": False, "error": str(exc), "path": str(resolved)}
    return {"ok": True, "path": str(resolved), "tags_written": list(tags.keys())}


# -----------------------------------------------------------------------------
# 1. merge-album / merge-split-album / duplicate-merge
# -----------------------------------------------------------------------------


#: Payload keys of the retired in-place merge that rewrote identity on the
#: moved items (album-level fields, Recording ID, disc/track). Refused: an
#: album-row merge is an ownership change only.
_IDENTITY_REWRITE_KEYS = ("adopt_target_fields", "item_field_overrides", "reassign_fields")


def plan_album_duplicate_merge(
    payload: Dict[str, Any],
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Plan moving album rows (or some of their items) into a retained row.

    Delegates to the one album-row merge authority
    (backend.album_row_merge.plan_rows_merge): same Release Group and Release
    ID, free slots, unchanged identity and content; nothing but item
    ownership changes. Accepts ``source_album_ids`` or ``source_album_id``
    and an optional ``item_ids`` subset."""
    import backend.album_row_merge as album_row_merge
    if any(payload.get(k) for k in _IDENTITY_REWRITE_KEYS):
        return {"ok": False, "code": "identity_rewrite_not_supported",
                "error": "An album-row merge never rewrites Recording, Release or track identity on the items it "
                         "moves. Attach the correct recording to each item first, then merge."}
    sources = payload.get("source_album_ids") or payload.get("source_ids")
    if not sources and (payload.get("source_album_id") or payload.get("source_id")):
        sources = [payload.get("source_album_id") or payload.get("source_id")]
    if isinstance(sources, (int, str)):
        sources = [sources]
    res = album_row_merge.plan_rows_merge(
        payload.get("target_album_id") or payload.get("target_id") or 0, sources or [], payload.get("item_ids"),
        reason=_s(payload.get("reason")), adapter=adapter, store=store)
    if res.get("ok"):
        res.update(token=res["operation_id"], item_count=len(res["moves"]), changes=res["moves"])
    return res


def apply_album_duplicate_merge(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    approved_by: str = "operator merge request",
) -> Dict[str, Any]:
    """Apply a planned album-row merge for a caller that holds the operator's
    decision (a merge request or an import the operator started)."""
    import backend.album_row_merge as album_row_merge
    res = album_row_merge.approve_and_apply(operation_id, approved_by=approved_by, adapter=adapter, store=store)
    if res.get("ok"):
        moved = len(_get_store(store).get(operation_id).get("metadata", {}).get("items") or [])
        res.update(moved=moved, merged_items_count=moved, source_albums_removed=len(res.get("retired_album_ids") or []))
    elif not res.get("error"):
        res["error"] = "; ".join(res.get("verification_problems") or []) or "Album-row merge did not verify."
    return res


def rollback_album_duplicate_merge(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Restore the original rows and item ownership through the engine."""
    import backend.album_row_merge as album_row_merge
    return album_row_merge.rollback_album_row_merge(operation_id, adapter=adapter, store=store)


def merge_duplicate_albums(
    target_album_id: int,
    source_album_ids: Any,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Plan and apply a whole-row merge (one source row id or a list)."""
    sources = [source_album_ids] if isinstance(source_album_ids, (int, str)) else list(source_album_ids or [])
    p_res = plan_album_duplicate_merge({"target_album_id": target_album_id, "source_album_ids": sources},
                                       adapter=adapter, store=store)
    if not p_res.get("ok"):
        return p_res
    return apply_album_duplicate_merge(p_res["operation_id"], adapter=adapter, store=store)


def merge_split_album_items(
    target_album_id: int,
    source_album_id: int,
    item_ids: List[int],
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Move the given items of one source row into the target row; the
    source row is retired only if that empties it."""
    p_res = plan_album_duplicate_merge(
        {"target_album_id": target_album_id, "source_album_ids": [source_album_id],
         "item_ids": [int(x) for x in item_ids or []]}, adapter=adapter, store=store)
    if not p_res.get("ok"):
        return p_res
    res = apply_album_duplicate_merge(p_res["operation_id"], adapter=adapter, store=store)
    if res.get("ok"):
        res.update(items_reassigned=res.get("moved", 0),
                   source_album_deleted=int(source_album_id) in (res.get("retired_album_ids") or []))
    return res


# -----------------------------------------------------------------------------
# 2. merge-artist / artist-folder reconcile
# -----------------------------------------------------------------------------


def plan_artist_folder_reconcile(
    payload_or_root: Any = None,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    **kwargs,
) -> Dict[str, Any]:
    """Plan reconciling artist folders or normalizing album artist names."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    payload = payload_or_root if isinstance(payload_or_root, dict) else kwargs

    artist_name = payload.get("artist") or payload.get("albumartist") or ""
    canonical_name = payload.get("canonical_name") or payload.get("target_artist") or artist_name
    album_ids = payload.get("album_ids") or []

    if not album_ids and artist_name:
        albums = ad.find_all_albums_by_albumartist(artist_name)
        album_ids = [a.get("id") for a in albums if a.get("id")]

    changes = []
    before_state = []
    for aid in album_ids:
        alb = ad.get_album(int(aid))
        if alb:
            before_state.append({
                "album_id": aid,
                "albumartist": alb.get("albumartist"),
                "artist": alb.get("artist"),
            })
            changes.append({
                "album_id": aid,
                "album": alb.get("album"),
                "old_albumartist": alb.get("albumartist"),
                "new_albumartist": canonical_name,
            })

    tx = st.create(
        operation_type="Merge Artist",
        status="Preview",
        summary=f"Reconcile {len(album_ids)} albums for artist '{canonical_name}'",
        changes=changes,
        rollback_available=False,  # #228: no rollback.operations and no engine family
        metadata={
            "canonical_name": canonical_name,
            "album_ids": album_ids,
            "before_state": before_state,
        },
    )

    return {
        "ok": True,
        "operation_id": tx["id"],
        "token": tx["id"],
        "status": "Preview",
        "canonical_name": canonical_name,
        "changes": changes,
    }


def apply_artist_folder_reconcile(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Execute artist folder / albumartist normalization."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    tx = st.get(operation_id)
    meta = tx.get("metadata", {})
    canonical_name = meta.get("canonical_name")
    album_ids = meta.get("album_ids", [])

    def write() -> None:
        if album_ids and canonical_name:
            ad.modify(fields={"albumartist": canonical_name}, album_ids=[int(x) for x in album_ids],
                      write=True, move=True)

    return _apply_claimed(st, operation_id, write)


def rollback_artist_folder_reconcile(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Roll back artist normalization by restoring original albumartist values."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    tx = st.get(operation_id)
    before_state = tx.get("metadata", {}).get("before_state", [])

    if _claim_rollback(st, operation_id) is None:
        return _rollback_refused(st, operation_id)
    restored = failed = 0
    for snap in before_state:
        aid = snap.get("album_id")
        orig = snap.get("albumartist")
        if aid and orig:
            ok, bad = _restore_rows(ad, {"album": {"id": aid, "fields": {"albumartist": orig}}}, write=True)
            restored, failed = restored + ok, failed + bad
    return _finish_rollback(st, operation_id, restored, failed)


# -----------------------------------------------------------------------------
# 3. existing-album reconcile
# -----------------------------------------------------------------------------


def _reconcile_duplicate_pairs(payload: Dict[str, Any]) -> List[Dict[str, int]]:
    """(imported duplicate -> existing survivor) pairs of a reconcile payload."""
    pairs = []
    for detail in payload.get("dup_details") or []:
        survivors = [int(x) for x in detail.get("survivor_item_ids") or [] if x]
        if detail.get("dup_item_id") and survivors:
            pairs.append({"delete_item_id": int(detail["dup_item_id"]), "keep_item_id": survivors[0]})
    return pairs


def plan_existing_album_reconcile(
    payload_or_target_id: Any = None,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    **kwargs,
) -> Dict[str, Any]:
    """Plan reconciling an imported album row with the existing row of the
    same release. Nothing changes here.

    * ``move_item_ids`` (imported items filling free slots) become one
      album-row merge plan (ownership only; see plan_album_duplicate_merge).
    * ``dup_details`` (imported copies of slots the existing row already
      holds) are recorded and become a reviewed duplicate cleanup when the
      plan is applied -- removed only with audio proof and an approval.
    * With neither, the whole imported row is merged.
    """
    import backend.duplicate_cleanup as duplicate_cleanup
    ad = adapter or beets_adapter
    st = _get_store(store)
    payload = payload_or_target_id if isinstance(payload_or_target_id, dict) else kwargs

    target_aid = int(payload.get("existing_album_id") or payload.get("target_album_id") or payload.get("album_id") or 0)
    imported_aid = int(payload.get("imported_album_id") or payload.get("source_album_id") or 0)
    if not target_aid or not imported_aid or target_aid == imported_aid:
        return {"ok": False, "code": "invalid_ids", "error": "An existing and a different imported album row are required."}
    if not ad.get_album(target_aid):
        return {"ok": False, "error": f"Target album {target_aid} not found"}

    pairs = _reconcile_duplicate_pairs(payload)
    move_ids = [int(x) for x in payload.get("move_item_ids") or []]
    reason = _s(payload.get("reason")) or "Existing album reconciliation"
    if move_ids or not pairs:
        plan = plan_album_duplicate_merge(
            {"target_album_id": target_aid, "source_album_ids": [imported_aid],
             "item_ids": move_ids or None, "reason": reason}, adapter=adapter, store=store)
        if not plan.get("ok"):
            return plan
        st.update(plan["operation_id"], metadata={"pending_duplicate_pairs": pairs, "reconcile_reason": reason})
        return {**plan, "duplicate_pairs": pairs}
    # Only duplicates: the cleanup itself is the plan (Preview, needs approval).
    plan = duplicate_cleanup.plan_reviewed_cleanup(pairs, reason=reason, adapter=adapter, store=store,
                                                   allow_sibling_row_retire=True)
    if not plan.get("ok"):
        return {**plan, "error": plan.get("error") or "No duplicate passed re-verification."}
    return {**plan, "token": plan["operation_id"], "duplicate_pairs": pairs}


def apply_existing_album_reconcile(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    approve_duplicates: bool = False,
    approved_by: str = "import reconciliation",
) -> Dict[str, Any]:
    """Apply a reconcile plan: the ownership moves now; the duplicate copies
    through a reviewed cleanup that is applied only when the caller holds a
    reviewer's decision (``approve_duplicates``) and otherwise stays in
    Preview for an operator."""
    import backend.duplicate_cleanup as duplicate_cleanup
    st = _get_store(store)
    try:
        meta = st.get(operation_id).get("metadata") or {}
    except KeyError:
        return {"ok": False, "code": "not_found", "error": "Transaction not found"}

    def _apply_cleanup(cleanup_id: str) -> Dict[str, Any]:
        if not approve_duplicates:
            return {"ok": True, "operation_id": cleanup_id, "status": "Preview", "awaiting_approval": True}
        from backend.resource_locks import approve_preview
        try:
            approved = approve_preview(st, cleanup_id, _s(approved_by))
        except KeyError:
            return {"ok": False, "code": "not_found", "error": "Transaction not found"}
        if approved is None:
            return {"ok": False, "code": "not_preview", "error": "Only a Preview transaction can be approved; it was cancelled or already finished."}
        return duplicate_cleanup.apply_reviewed_cleanup(cleanup_id, adapter=adapter, store=store)

    if meta.get("mutation_family") == duplicate_cleanup.REVIEWED_CLEANUP_FAMILY:
        res = _apply_cleanup(operation_id)
        return {**res, "cleanup_operation_id": operation_id, "cleanup_status": res.get("status")}

    res = apply_album_duplicate_merge(operation_id, adapter=adapter, store=store, approved_by=approved_by)
    if not res.get("ok"):
        return res
    pairs = meta.get("pending_duplicate_pairs") or []
    res.update(cleanup_operation_id=None, cleanup_status="none", cleanup_skipped=[])
    if pairs:
        plan = duplicate_cleanup.plan_reviewed_cleanup(
            pairs, reason=_s(meta.get("reconcile_reason")), adapter=adapter, store=store,
            allow_sibling_row_retire=True)
        res["cleanup_skipped"] = plan.get("skipped") or []
        if not plan.get("ok"):
            res["cleanup_status"] = "not_proven"
            if approve_duplicates:
                res.update(ok=False, error="The duplicate copy could not be proven; both copies were kept.")
            return res
        cleanup = _apply_cleanup(plan["operation_id"])
        res.update(cleanup_operation_id=plan["operation_id"], cleanup_status=cleanup.get("status"))
        if not cleanup.get("ok"):
            res.update(ok=False, error=cleanup.get("error") or "Duplicate cleanup did not verify.")
    return res


def rollback_existing_album_reconcile(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    import backend.duplicate_cleanup as duplicate_cleanup
    family = (_get_store(store).get(operation_id).get("metadata") or {}).get("mutation_family")
    if family == duplicate_cleanup.REVIEWED_CLEANUP_FAMILY:
        return duplicate_cleanup.rollback_reviewed_cleanup(operation_id, adapter=adapter, store=store)
    return rollback_album_duplicate_merge(operation_id, adapter=adapter, store=store)


# -----------------------------------------------------------------------------
# 4. Clean All & Library Cleanup Workflows
# -----------------------------------------------------------------------------


#: Above this share of rows "missing", the music mount is assumed broken and
#: nothing is removed (same rule as Clean All's missing-files stage).
MISSING_ROW_RATIO_CAP = 0.5


def _item_abs_path(raw: Any) -> str:
    """Absolute path of a Beets item as seen from this container."""
    p = _decode_path(raw)
    if not p:
        return ""
    if os.path.isabs(p) or p.startswith("/"):
        return p
    return str(_music_root() / p)


def _music_root_usable() -> Tuple[bool, str]:
    """The music mount must exist and be non-empty before a missing file can
    be told apart from a missing mount."""
    root = _music_root()
    try:
        if not root.is_dir():
            return False, f"music root is not accessible: {root}"
        if not any(root.iterdir()):
            return False, f"music root is empty (mount missing?): {root}"
    except OSError as exc:
        log.warning("Music root %s is not readable: %s", root, exc)
        return False, f"music root is not readable ({type(exc).__name__}): {root}"
    return True, ""


def _record_row_removal(store: Optional[TransactionStore], *, summary: str, rows: List[Dict[str, Any]],
                        reason: str) -> str:
    """Audit record for a row-only removal (files untouched). The row
    snapshots are kept so a removed row can be re-attached by hand."""
    st = _get_store(store)
    tx = st.create(
        operation_type="Library Cleanup",
        status="Running",
        summary=summary,
        reason=reason,
        changes=[{"action": "remove_row_keep_file", "item_id": r.get("id"), "path": _decode_path(r.get("path"))}
                 for r in rows],
        rollback_available=False,
        rollback_reason="Row-only removal: the audio files were not touched. Re-attach a file through "
                        "untracked recovery if it reappears.",
        metadata={"mutation_family": "missing_row_removal_v1", "row_snapshots": rows},
    )
    return tx["id"]


def _apply_row_removal(ad: BeetsAdapter, store: Optional[TransactionStore], op_id: str,
                       item_ids: List[int]) -> Optional[str]:
    """Remove the rows and finish the audit record. On an exception the
    transaction is marked Failed (never left Running) and the error text is
    returned so the caller can report it honestly."""
    st = _get_store(store)
    try:
        ad.remove(item_ids=item_ids, delete_files=False)
    except Exception as exc:  # report honestly; Beets state is unknown
        err = f"{type(exc).__name__}: {exc}"
        try:
            st.update(op_id, status="Failed", error=err)
        except Exception:
            log.exception("Could not mark row-removal transaction %s Failed", op_id)
        return err
    st.update(op_id, status="Completed")
    return None


def _verified_missing(ad: BeetsAdapter, item_ids: List[int]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Re-read each requested item from live Beets; return (missing, skipped).
    An item is "missing" only if it still exists in Beets and its file is
    absent on disk now. Anything else is skipped with a reason."""
    missing, skipped = [], []
    for iid in item_ids:
        item = ad.get_item(int(iid))
        if not item:
            skipped.append({"id": int(iid), "reason": "not_in_library"})
            continue
        path = _item_abs_path(item.get("path"))
        if not path:
            skipped.append({"id": int(iid), "reason": "no_path"})
        elif os.path.lexists(path):
            skipped.append({"id": int(iid), "reason": "file_present"})
        else:
            missing.append(item)
    return missing, skipped


def sync_deleted_files(
    dry_run: bool = True,
    limit: int = 50000,
    adapter: Optional[BeetsAdapter] = None,
    item_ids: Optional[List[int]] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Find Beets rows whose files are gone and (apply) remove those ROWS.

    Never deletes files. Preview lists the missing rows; apply acts only on
    the ``item_ids`` a preview returned, re-verified now (LT-2), and refuses
    when the music root is unusable or too many rows look missing."""
    ad = adapter or beets_adapter
    ok_root, root_reason = _music_root_usable()
    item_records = ad.list_item_paths(details=True)
    scanned = len(item_records)
    base = {"ok": True, "dry_run": dry_run, "scanned": scanned, "scanned_items": scanned,
            "removed_from_db": 0, "missing_albums_count": 0}
    if not ok_root:
        return {**base, "ok": False, "code": "music_root_unusable", "error": root_reason,
                "missing_count": 0, "missing_items": [], "missing_item_ids": []}
    missing_items = [rec for rec in item_records[:limit]
                     if rec.get("path") and not os.path.lexists(_item_abs_path(rec.get("path")))]
    if scanned and len(missing_items) / scanned >= MISSING_ROW_RATIO_CAP:
        return {**base, "ok": False, "code": "too_many_missing",
                "error": f"{len(missing_items)}/{scanned} rows look missing; possible mount issue. Nothing removed.",
                "missing_count": len(missing_items), "missing_items": missing_items[:100], "missing_item_ids": []}
    result = {**base, "missing_count": len(missing_items), "missing_items": missing_items[:100],
              "missing_item_ids": [int(r["id"]) for r in missing_items if r.get("id")]}
    if dry_run:
        return result
    if not item_ids:
        return {**result, "ok": False, "code": "planned_ids_required",
                "error": "Apply needs the item_ids from a preview; nothing was removed."}
    planned = {int(x) for x in item_ids}
    still_missing = {int(r["id"]) for r in missing_items if r.get("id")}
    targets, skipped = _verified_missing(ad, sorted(planned & still_missing))
    skipped += [{"id": i, "reason": "no_longer_missing_or_not_planned"} for i in sorted(planned - still_missing)]
    if not targets:
        return {**result, "skipped": skipped}
    op_id = _record_row_removal(store, summary=f"Remove {len(targets)} Beets row(s) whose files are missing",
                                rows=targets, reason="sync deleted files")
    err = _apply_row_removal(ad, store, op_id, [int(t["id"]) for t in targets])
    if err:
        return {**result, "ok": False, "code": "remove_failed", "error": err, "skipped": skipped,
                "operation_id": op_id}
    return {**result, "removed_from_db": len(targets), "skipped": skipped, "operation_id": op_id}


def clean_orphaned_items(
    item_ids: Optional[List[int]] = None,
    dry_run: bool = True,
    candidate_ids: Optional[List[int]] = None,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Remove the Beets ROWS of explicitly requested items whose files are
    missing on disk (LT-1). Never deletes files. Each id is re-verified
    against live Beets and the disk; anything else is skipped. An empty
    request is refused -- it never widens to the whole library."""
    ad = adapter or beets_adapter
    ids = [int(x) for x in (item_ids if item_ids is not None else candidate_ids) or [] if int(x) > 0]
    empty = {"dry_run": dry_run, "selected": 0, "removed_count": 0, "orphaned_count": 0,
             "item_ids": [], "orphaned_items": [], "skipped": []}
    if not ids:
        return {**empty, "ok": False, "code": "empty_selection",
                "error": "No item ids were given; nothing was removed."}
    ok_root, root_reason = _music_root_usable()
    if not ok_root:
        return {**empty, "ok": False, "code": "music_root_unusable", "error": root_reason}
    targets, skipped = _verified_missing(ad, sorted(set(ids)))
    total = int((ad.get_stats() or {}).get("items") or 0)
    if total and len(targets) / total >= MISSING_ROW_RATIO_CAP:
        return {**empty, "ok": False, "code": "too_many_missing", "skipped": skipped,
                "error": f"{len(targets)}/{total} rows look missing; possible mount issue. Nothing removed."}
    out = {"ok": True, "dry_run": dry_run, "selected": len(targets), "removed_count": 0,
           "orphaned_count": len(targets), "item_ids": [int(t["id"]) for t in targets],
           "orphaned_items": [{"id": t.get("id"), "artist": t.get("artist", ""), "title": t.get("title", ""),
                               "path": _decode_path(t.get("path"))} for t in targets],
           "skipped": skipped}
    if dry_run or not targets:
        return out
    op_id = _record_row_removal(store, summary=f"Remove {len(targets)} Beets row(s) whose files are missing",
                                rows=targets, reason="orphaned item cleanup")
    err = _apply_row_removal(ad, store, op_id, out["item_ids"])
    if err:
        return {**out, "ok": False, "code": "remove_failed", "error": err, "operation_id": op_id}
    return {**out, "removed_count": len(targets), "operation_id": op_id}


def clean_empty_albums(
    album_ids: Optional[List[int]] = None,
    dry_run: bool = True,
    candidate_ids: Optional[List[int]] = None,
    adapter: Optional[BeetsAdapter] = None,
) -> Dict[str, Any]:
    """Remove explicitly requested album rows that have no items (verified
    live). An empty request is refused; files are never touched."""
    ad = adapter or beets_adapter
    ids = [int(x) for x in (album_ids if album_ids is not None else candidate_ids) or [] if int(x) > 0]
    if not ids:
        return {"ok": False, "code": "empty_selection", "error": "No album ids were given; nothing was removed.",
                "dry_run": dry_run, "empty_albums_count": 0, "album_ids": [], "removed_count": 0}
    empty = [aid for aid in sorted(set(ids)) if ad.get_album(aid) and not ad.find_all_items_by_album_id(aid)]
    if not dry_run and empty:
        ad.remove(album_ids=empty, delete_files=False)
    return {"ok": True, "dry_run": dry_run, "empty_albums_count": len(empty), "album_ids": empty,
            "removed_count": 0 if dry_run else len(empty)}


def scan_library_integrity(adapter: Optional[BeetsAdapter] = None) -> Dict[str, Any]:
    """Counts-only integrity summary (see get_library_health for the report)."""
    report = get_library_health(adapter=adapter, orphan_sample_limit=0, duplicate_limit=0, empty_limit=0)
    return {"ok": True, "total_items": report["item_row_count"], "total_albums": report["album_row_count"],
            "missing_files_sample": report["orphaned_item_count"]}


def get_library_health(
    adapter: Optional[BeetsAdapter] = None,
    *,
    orphan_sample_limit: int = 100,
    duplicate_limit: int = 100,
    empty_limit: int = 100,
) -> Dict[str, Any]:
    """Read-only library health report from live Beets reads (LT-18).

    "Orphaned" items are rows whose file is missing on disk (as seen from
    this container); when the music root is unusable none are reported,
    because a missing mount would make every row look orphaned.
    rgid_duplicate_groups is returned untruncated (the caller splits it by
    operator resolution first). Duplicate groups carry no merge_safe flag:
    merging is decided by the album-row merge planner, never by this report."""
    ad = adapter or beets_adapter
    albums = ad.get_albums() or []
    items = ad.get_items() or []
    ok_root, root_reason = _music_root_usable()
    by_album: Dict[int, List[Dict[str, Any]]] = {}
    for it in items:
        if it.get("album_id") is not None:
            by_album.setdefault(int(it["album_id"]), []).append(it)
    orphans: List[Dict[str, Any]] = []
    if ok_root:
        orphans = [it for it in items if it.get("path") and not os.path.lexists(_item_abs_path(it.get("path")))]
    album_rows = {int(a["id"]): a for a in albums if a.get("id") is not None}
    empty = [a for aid, a in album_rows.items() if not by_album.get(aid)]

    def _album_view(a: Dict[str, Any]) -> Dict[str, Any]:
        aitems = by_album.get(int(a["id"]), [])
        aldir = str(Path(_decode_path(aitems[0].get("path"))).parent) if aitems else ""
        return {"album_id": int(a["id"]), "albumartist": a.get("albumartist", ""), "album": a.get("album", ""),
                "year": a.get("year") or 0, "track_count": len(aitems), "mb_albumid": a.get("mb_albumid", ""),
                "mb_releasegroupid": a.get("mb_releasegroupid", ""), "aldir": aldir}

    name_groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    rg_groups: Dict[str, List[Dict[str, Any]]] = {}
    for a in album_rows.values():
        key = (_s(a.get("albumartist")).strip().lower(), _s(a.get("album")).strip().lower())
        if key[1]:
            name_groups.setdefault(key, []).append(a)
        rg = _s(a.get("mb_releasegroupid")).strip().lower()
        if rg:
            rg_groups.setdefault(rg, []).append(a)
    dup_albums = [{"albumartist": g[0].get("albumartist", ""), "album": g[0].get("album", ""), "count": len(g),
                   "albums": [_album_view(a) for a in g]} for g in name_groups.values() if len(g) > 1]
    rg_dups = [{"mb_releasegroupid": rg, "count": len(g), "albums": [_album_view(a) for a in g]}
               for rg, g in rg_groups.items() if len(g) > 1]
    summary = {
        "database_rows_scanned": len(items) + len(albums), "albums_count": len(albums), "tracks_count": len(items),
        "duplicate_album_groups": len(dup_albums), "same_release_group_id_groups": len(rg_dups),
        "orphaned_items": len(orphans), "empty_albums": len(empty), "missing_files": len(orphans),
    }
    return {
        "ok": True,
        "music_root_usable": ok_root, "music_root_problem": root_reason,
        "duplicate_albums": dup_albums[:max(0, int(duplicate_limit))], "duplicate_album_count": len(dup_albums),
        "rgid_duplicate_groups": rg_dups, "rgid_duplicate_group_count": len(rg_dups),
        "rgid_resolved_groups": [], "rgid_resolved_group_count": 0,
        "orphaned_items": [{"id": it.get("id"), "title": it.get("title", ""), "artist": it.get("artist", ""),
                            "album": it.get("album", ""), "path": _decode_path(it.get("path"))}
                           for it in orphans[:max(0, int(orphan_sample_limit))]],
        "orphaned_item_count": len(orphans),
        "orphaned_item_ids": [int(it["id"]) for it in orphans if it.get("id") is not None],
        "empty_albums": [{"album_id": int(a["id"]), "albumartist": a.get("albumartist", ""),
                          "album": a.get("album", ""), "year": a.get("year") or 0}
                         for a in empty[:max(0, int(empty_limit))]],
        "empty_album_count": len(empty),
        "database_rows_scanned": summary["database_rows_scanned"],
        "album_row_count": len(albums), "item_row_count": len(items),
        "final_summary": summary,
    }


def plan_library_cleanup(
    payload_or_action: Any = None,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    **kwargs,
) -> Dict[str, Any]:
    st = _get_store(store)
    data = payload_or_action if isinstance(payload_or_action, dict) else kwargs
    paths = data.get("paths") or []
    changes = [{"path": p, "action": "remove"} for p in paths]
    tx = st.create(
        operation_type="Library Cleanup",
        status="Preview",
        summary=f"Clean up {len(paths)} duplicate files",
        changes=changes,
        metadata=data,
    )
    return {"ok": True, "operation_id": tx["id"], "status": "Preview", "changes": changes}


# -----------------------------------------------------------------------------
# 5. Track Replacement & Bulk Import Replacement
# -----------------------------------------------------------------------------


ITEM_FILE_REPLACEMENT_FAMILY = "item_file_replacement_v1"

# Identity fields the replacement must preserve on the album-slot item.
_REPLACEMENT_IDENTITY_FIELDS = (
    "album_id", "mb_trackid", "mb_albumid", "mb_releasegroupid", "disc", "track",
)


def _replacement_side(item: Dict[str, Any]) -> Dict[str, Any]:
    side = {k: item.get(k) for k in _REPLACEMENT_IDENTITY_FIELDS}
    side.update({
        "item_id": item.get("id"),
        "path": _decode_path(item.get("path")),
        "title": item.get("title", ""),
        "format": item.get("format", ""),
        "bitrate": item.get("bitrate"),
        "samplerate": item.get("samplerate"),
        "bitdepth": item.get("bitdepth"),
    })
    return side


def _same_library_path(a: Any, b: Any) -> bool:
    """Equal paths, allowing one side to be library-relative (the stock
    Beets web API reports relative paths; the engine op returns absolute)."""
    a, b = _decode_path(a), _decode_path(b)
    if not a or not b:
        return False
    if a == b:
        return True
    rel, full = (a, b) if not a.startswith("/") else (b, a)
    return not rel.startswith("/") and full.startswith("/") and full.endswith("/" + rel)


def plan_track_replacement(
    payload: Dict[str, Any],
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Preview replacing an album-slot item's audio file with another
    tracked library item's file (same recording, better copy).

    Planning never mutates. Apply requires the transaction to be Approved,
    then asks the Beets engine to do the whole change (see
    beetsplug/webmanager/replace_ops.py): the album item keeps its identity
    and tags, its old file is quarantined by the engine, the replacement's
    own library row is removed, and Beets moves the file to its canonical
    path. Replacing from an untracked staged file is not supported here --
    that would need a local write into the read-only music mount.
    """
    ad = adapter or beets_adapter
    st = _get_store(store)

    target_id = int(payload.get("original_item_id") or payload.get("item_id") or payload.get("target_id") or 0)
    source_id = int(payload.get("replacement_item_id") or payload.get("source_item_id") or 0)
    if not source_id:
        return {
            "ok": False,
            "code": "staged_replacement_unsupported",
            "error": "Replacement must be a tracked library item (replacement_item_id); "
                     "replacing from an untracked staged file is not supported.",
        }
    if not target_id or target_id == source_id:
        return {"ok": False, "code": "invalid_items", "error": "A distinct original item and replacement item are required."}

    target = ad.get_item(target_id)
    source = ad.get_item(source_id)
    if not target:
        return {"ok": False, "code": "item_not_found", "error": f"Item {target_id} not found in library"}
    if not source:
        return {"ok": False, "code": "item_not_found", "error": f"Replacement item {source_id} not found in library"}
    if not target.get("album_id"):
        return {"ok": False, "code": "target_not_in_album",
                "error": f"Item {target_id} is not attached to an album slot"}

    before, candidate = _replacement_side(target), _replacement_side(source)
    displace = payload.get("displace_destination") or None
    if displace and not re.fullmatch(r"[0-9a-f]{64}", _s(displace.get("sha256"))):
        return {"ok": False, "code": "invalid_displacement", "error": "Displacement needs the occupant's SHA-256."}
    changes = [{
        "item_id": target_id,
        "title": before["title"],
        "old_path": before["path"],
        "old_format": before["format"],
        "replacement_item_id": source_id,
        "replacement_path": candidate["path"],
        "replacement_format": candidate["format"],
        "preserved": {k: before[k] for k in _REPLACEMENT_IDENTITY_FIELDS},
        "displaced_occupant": displace,
    }]
    tx = st.create(
        operation_type="Replace",
        status="Preview",
        summary=f"Replace the {before['format'] or 'audio'} file of item {target_id} ({before['title']}) "
                f"with item {source_id}'s {candidate['format'] or 'audio'} file",
        changes=changes,
        rollback_available=True,
        metadata={
            "mutation_family": ITEM_FILE_REPLACEMENT_FAMILY,
            "target_item_id": target_id,
            "source_item_id": source_id,
            "before": before,
            "candidate": candidate,
            "reason": _s(payload.get("reason")),
            "matching_contract": payload.get("matching_contract") or {},
            "displace_destination": displace,
        },
    )
    return {
        "ok": True,
        "operation_id": tx["id"],
        "token": tx["id"],
        "status": "Preview",
        "requires_approval": True,
        "displace_destination": displace,
        "target_item": before,
        "replacement_item": candidate,
        "changes": changes,
    }


def apply_track_replacement(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Apply an Approved item-file replacement through the Beets engine,
    then verify the album slot kept its identity and points to the new file."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    try:
        tx = st.get(operation_id)
    except KeyError:
        return {"ok": False, "code": "not_found", "error": "Transaction not found"}
    meta = tx.get("metadata") or {}
    if meta.get("mutation_family") != ITEM_FILE_REPLACEMENT_FAMILY:
        return {"ok": False, "code": "wrong_family", "error": "Not an item file replacement transaction."}
    if meta.get("engine_result"):
        return {"ok": False, "code": "already_applied", "error": "This replacement was already applied."}
    if tx.get("status") != "Approved":
        return {"ok": False, "code": "not_approved", "error": "Approve the transaction before applying it."}

    target_id, source_id = int(meta["target_item_id"]), int(meta["source_item_id"])
    from backend.resource_locks import attempt_owner, claim_approved, claim_refusal, locks as resource_locks
    with resource_locks().hold([f"item:{min(target_id, source_id)}", f"item:{max(target_id, source_id)}"],
                               attempt_owner(operation_id), timeout=10):
        if claim_approved(st, operation_id) is None:
            return {"ok": False, "code": "not_approved", "error": claim_refusal(st, operation_id)}
        # Recorded before the engine call: a restart mid-call is finished from
        # engine evidence by backend/transaction_recovery.py, never replayed.
        st.update(operation_id, status="Running", metadata={"engine_request": {"operation_id": operation_id}})
        try:
            displace = meta.get("displace_destination") or {}
            res = ad.replace_item_file(target_id, source_id, idempotency_key=operation_id,
                                       displace_destination_sha256=displace.get("sha256") or None)
        except Exception:
            st.update(operation_id, status="Failed",
                      logs=["Engine replace-item-file failed; the engine restored the original file."])
            raise
        return finish_track_replacement(operation_id, res, adapter=ad, store=st)


def finish_track_replacement(
    operation_id: str,
    res: Dict[str, Any],
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Verify an applied replacement from the engine's result and record the
    outcome (also used by restart recovery -- never re-applies)."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    meta = st.get(operation_id).get("metadata") or {}
    target_id, source_id = int(meta["target_item_id"]), int(meta["source_item_id"])
    before = meta.get("before") or {}
    engine = res.get("result") if isinstance(res.get("result"), dict) else res

    after_item = ad.get_item(target_id) or {}
    after = _replacement_side(after_item) if after_item else {}
    problems = [k for k in _REPLACEMENT_IDENTITY_FIELDS if _s(after.get(k)) != _s(before.get(k))]
    if not after or not _same_library_path(after.get("path"), engine.get("new_target_path")):
        problems.append("path")
    status = "Completed" if not problems else "Recovery Required"
    st.update(
        operation_id,
        status=status,
        metadata={**meta, "engine_result": engine, "after": after, "verification_problems": problems},
        logs=[
            f"Item {target_id} now points to {engine.get('new_target_path')}",
            f"Old file quarantined at {engine.get('quarantine_path') or '(none)'}",
            f"Replacement item {source_id} row removed (its file now belongs to item {target_id})",
        ] + ([f"Verification mismatch: {', '.join(problems)}"] if problems else []),
    )
    return {
        "ok": not problems,
        "operation_id": operation_id,
        "status": status,
        "new_path": engine.get("new_target_path"),
        "quarantined_to": engine.get("quarantine_path") or "",
        "before": before,
        "after": after,
        "verification_problems": problems,
    }


def rollback_track_replacement(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Undo an applied item-file replacement through the Beets engine."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    try:
        tx = st.get(operation_id)
    except KeyError:
        return {"ok": False, "code": "not_found", "error": "Transaction not found"}
    meta = tx.get("metadata") or {}
    engine = meta.get("engine_result") or {}
    if meta.get("mutation_family") != ITEM_FILE_REPLACEMENT_FAMILY or not engine.get("quarantine_id"):
        return {"ok": False, "code": "not_applied", "error": "No applied item file replacement to roll back."}
    if tx.get("status") == "Rolled Back":
        return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}
    refusal = engine_rollback_refusal(tx)
    if refusal:
        return refusal
    res = ad.rollback_replace_item_file(engine["quarantine_id"], idempotency_key=f"{operation_id}:rollback")
    result = res.get("result") if isinstance(res.get("result"), dict) else res
    if st.transition(
        operation_id, tx.get("status"), "Rolled Back",
        metadata={**meta, "rollback_result": result},
        logs=[f"Restored item {meta['target_item_id']} to {result.get('restored_target_path')}; "
              f"replacement re-added as item {result.get('recreated_source_item_id')}"],
    ) is None:
        return _rollback_conflict(operation_id)
    out = {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}
    out.update({k: result.get(k) for k in ("restored_target_path", "recreated_source_item_id", "recreated_source_path")})
    return out


# -----------------------------------------------------------------------------
# 6. Folder Cleanup & Album Cleanup
# -----------------------------------------------------------------------------


def _library_refs_under(folder: Any, adapter: Optional[BeetsAdapter] = None) -> List[Dict[str, Any]]:
    """Beets items whose file lies under ``folder``, read through the adapter
    (BA-7: Web Manager never opens the Beets library file). Raises when Beets
    is unavailable, so a folder change fails closed.
    ponytail: item paths only; an album's artpath is not checked (an album
    with art but no items under the folder is not seen).

    A pure string comparison of normalized absolute paths: nothing here
    touches the filesystem with the caller's path (CodeQL #1381). Paths
    under the configured MUSIC_ROOT as written are rewritten onto its
    realpath (F-243-2), so a symlinked MUSIC_ROOT matches however Beets
    stores the item path (relative items are already built on the realpath)."""
    from backend.config_layers import music_root
    written = os.path.abspath(os.path.normpath(music_root()))
    real = os.path.realpath(music_root())

    def norm(p: Any) -> str:
        n = os.path.abspath(os.path.normpath(str(p)))
        if n == written or n.startswith(written.rstrip(os.sep) + os.sep):
            n = real + n[len(written):]
        return os.path.normcase(n)

    target = norm(_decode_path(folder))
    prefix = target.rstrip(os.sep) + os.sep
    refs = []
    for rec in (adapter or beets_adapter).list_item_paths(details=True):
        if not rec.get("path"):
            continue
        p = norm(_item_abs_path(rec.get("path")))
        if p == target or p.startswith(prefix):
            refs.append({"table": "items", "id": rec.get("id"), "path": _decode_path(rec.get("path"))})
    return refs


#: Actions that only ever remove an EMPTY folder (the engine re-checks that at
#: plan and apply), so no tracked file can be under it and the adapter scan is
#: skipped: move-all plans one per candidate folder.
_EMPTY_ONLY_ACTIONS = frozenset({"remove_empty", "remove_empty_source"})


def _refs_refusal(folder: Any, action: Any = "") -> Optional[Dict[str, Any]]:
    if str(action or "remove_empty").strip() in _EMPTY_ONLY_ACTIONS:
        return None
    refs = _library_refs_under(folder)
    if not refs:
        return None
    return {"ok": False, "code": "folder_cleanup_db_references", "mutated": False, "references": refs[:20],
            "error": "The folder still holds files Beets tracks; nothing was changed."}


def plan_folder_cleanup(
    payload_or_action: Any = None,
    store: Optional[TransactionStore] = None,
    **kwargs,
) -> Dict[str, Any]:
    """Plan a folder cleanup through the canonical folder_cleanup_v1 engine
    (BA-2): the old local plan accepted any action and its apply reported
    Completed without doing a safe_rename/merge at all."""
    from backend.transaction_engine import create_folder_cleanup_plan
    data = dict(payload_or_action if isinstance(payload_or_action, dict) else kwargs)
    refusal = _refs_refusal(data.get("source") or data.get("source_folder") or data.get("source_path") or "",
                            data.get("action") or data.get("mode"))
    if refusal:
        return refusal
    return create_folder_cleanup_plan(_get_store(store), data)


def apply_folder_cleanup(
    operation_id: str,
    store: Optional[TransactionStore] = None,
    approved_by: str = "operator (folder cleanup)",
) -> Dict[str, Any]:
    """Approve, claim (CAS) and apply a folder_cleanup_v1 plan. The result
    says what really happened: ``mutated``, ``moved_records``, ``removed_dirs``."""
    from backend.transaction_engine import execute_folder_cleanup_apply
    from backend.resource_locks import claim_approved, claim_refusal
    st = _get_store(store)
    if st.get(operation_id).get("status") == "Completed":
        return execute_folder_cleanup_apply(st, operation_id)
    if not _approve_preview(st, operation_id, approved_by) and st.get(operation_id).get("status") != "Approved":
        return _apply_refused(st, operation_id)
    meta = st.get(operation_id).get("metadata") or {}
    refusal = _refs_refusal(meta.get("source") or "", meta.get("action"))
    if refusal:
        st.transition(operation_id, "Approved", "Failed", logs=["Apply refused: the folder gained Beets items."])
        return {**refusal, "operation_id": operation_id}
    if claim_approved(st, operation_id) is None:
        return {"ok": False, "code": "not_applicable", "operation_id": operation_id, "mutated": False,
                "error": claim_refusal(st, operation_id)}
    try:
        return execute_folder_cleanup_apply(st, operation_id)
    except Exception as exc:
        st.update(operation_id, status="Failed", logs=[f"Apply raised: {type(exc).__name__}"])
        raise


def rollback_folder_cleanup(
    operation_id: str,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Undo a folder_cleanup_v1 apply through the engine's own records."""
    from backend.transaction_engine import rollback_folder_cleanup as engine_rollback
    st = _get_store(store)
    claimed = _claim_rollback(st, operation_id)
    if claimed is None:
        return _rollback_refused(st, operation_id)
    return engine_rollback(st, operation_id, claimed_from=claimed["claimed_from"])


def safe_rename_library_folder(
    source: str,
    target: str,
    approved_by: str,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Rename one library folder through the canonical folder_cleanup_v1
    engine (plan -> Approved -> claimed -> apply), for callers whose own run
    is the operator's explicit confirmation (Clean All ``folder_safe_renames``).

    The engine refuses the library root itself, paths outside MUSIC_ROOT,
    symlinks, a missing target parent, an existing target and folders the
    Beets DB still references; it re-checks the folder identity at apply and
    records the rename so it can be rolled back. Never deletes media."""
    from backend.transaction_engine import create_folder_cleanup_plan, execute_folder_cleanup_apply
    from backend.resource_locks import attempt_owner, claim_approved, claim_refusal, locks as resource_locks
    st = _get_store(store)
    refusal = _refs_refusal(source, "safe_rename")
    if refusal:
        return {"ok": False, "renamed": False, "code": refusal["code"], "error": refusal["error"]}
    plan = create_folder_cleanup_plan(
        st, {"action": "safe_rename", "source": source, "target": target})
    if not plan.get("ok"):
        return {"ok": False, "renamed": False, "code": plan.get("code") or "plan_failed",
                "error": plan.get("error") or "Rename plan was refused."}
    op_id = plan["operation_id"]
    if not _approve_preview(st, op_id, approved_by):
        return {"ok": False, "renamed": False, "operation_id": op_id, "code": "not_approved",
                "error": "The rename preview could not be approved (status changed)."}
    with resource_locks().hold(["workflow:folder-safe-rename"], attempt_owner(op_id), timeout=10):
        if claim_approved(st, op_id) is None:
            return {"ok": False, "renamed": False, "operation_id": op_id, "code": "already_applied",
                    "error": claim_refusal(st, op_id)}
        try:
            res = execute_folder_cleanup_apply(st, op_id)
        except Exception as exc:
            st.update(op_id, status="Failed", logs=[f"Apply raised: {exc}"])
            raise
    if not res.get("ok"):
        return {"ok": False, "renamed": False, "operation_id": op_id,
                "code": res.get("code") or "apply_failed", "error": res.get("error") or "Rename failed."}
    return {"ok": True, "renamed": bool(res.get("moved_records")), "operation_id": op_id,
            "status": res.get("status"), "moved_records": res.get("moved_records") or []}


ALBUM_CLEANUP_FAMILY = "album_cleanup_v1"

#: Phrase an operator must send to plan an album removal that ALSO deletes
#: the audio files (irreversible). Without it a removal is row-only.
DELETE_ALBUM_FILES_CONFIRMATION = "DELETE ALBUM FILES"


def _accepts_kwarg(fn: Callable[..., Any], name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True
    return name in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


def rollback_album_cleanup(operation_id: str, store: Optional[TransactionStore] = None) -> Dict[str, Any]:
    """Album removal has no engine rollback (LT-4); reported honestly."""
    return {"ok": False, "code": "not_supported", "operation_id": operation_id,
            "error": "Album cleanup has no rollback; re-attach kept files through untracked recovery."}


def _transport_error(exc: BaseException) -> bool:
    """A client-side transport failure: the engine may still have applied."""
    return isinstance(exc, (BeetsAdapterTimeoutError, BeetsAdapterConnectionError))


def plan_album_cleanup(
    album_id: int,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    *,
    delete_files: bool = False,
    reason: str = "",
) -> Dict[str, Any]:
    """Preview removing an album row and its item rows (LT-4).

    Row-only by default: the files stay on disk. ``delete_files`` must be
    decided here, by the caller that holds the operator's explicit
    confirmation; it is recorded on the plan and Apply never widens it."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    album = ad.get_album(int(album_id))
    if not album:
        return {"ok": False, "error": f"Album {album_id} not found"}

    items = ad.find_all_items_by_album_id(int(album_id))
    item_ids = sorted(int(it.get("id")) for it in items if it.get("id"))
    tx = st.create(
        operation_type="Delete",
        status="Preview",
        summary=(f"Remove album {album_id} ({album.get('album')}) and {len(items)} track row(s)"
                 + (" AND DELETE THEIR FILES" if delete_files else "; files stay on disk")),
        reason=_s(reason),
        rollback_available=False,
        rollback_reason=("Files were deleted by Beets; there is no rollback." if delete_files else
                         "Row-only removal: re-attach the files through untracked recovery."),
        metadata={"mutation_family": ALBUM_CLEANUP_FAMILY, "album_id": int(album_id), "item_ids": item_ids,
                  "delete_files": bool(delete_files), "album_snapshot": album,
                  "item_snapshots": items},
    )
    return {"ok": True, "operation_id": tx["id"], "token": tx["id"], "status": "Preview",
            "requires_approval": True, "delete_files": bool(delete_files), "album": album, "items": items}


def apply_album_cleanup(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Apply an Approved album removal exactly as planned (LT-4).

    Refuses an unapproved, already-applied or drifted plan (the album's item
    set must still equal the planned one). Never re-sent: a second apply is
    refused."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    try:
        tx = st.get(operation_id)
    except KeyError:
        return {"ok": False, "code": "not_found", "error": "Transaction not found", "mutated": False}
    meta = tx.get("metadata") or {}
    if meta.get("mutation_family") != ALBUM_CLEANUP_FAMILY:
        return {"ok": False, "code": "wrong_family", "error": "Not an album cleanup transaction.", "mutated": False}
    if meta.get("engine_result") or tx.get("status") in ("Completed", "Running"):
        return {"ok": False, "code": "already_applied", "error": "This album cleanup was already applied.",
                "mutated": False}
    if tx.get("status") != "Approved":
        return {"ok": False, "code": "not_approved", "error": "Approve the transaction before applying it.",
                "mutated": False}
    aid = int(meta["album_id"])
    from backend.resource_locks import attempt_owner, claim_approved, claim_refusal, locks as resource_locks
    with resource_locks().hold([f"album:{aid}"], attempt_owner(operation_id), timeout=10):
        if claim_approved(st, operation_id) is None:
            return {"ok": False, "code": "not_approved", "mutated": False,
                    "error": claim_refusal(st, operation_id)}
        live = sorted(int(it.get("id")) for it in ad.find_all_items_by_album_id(aid) if it.get("id"))
        if not ad.get_album(aid) or live != list(meta.get("item_ids") or []):
            st.update(operation_id, status="Failed", logs=["Album changed since the plan; nothing was removed."])
            return {"ok": False, "code": "stale_plan", "mutated": False,
                    "error": "The album changed since it was planned; nothing was removed. Plan again."}
        st.update(operation_id, metadata={"engine_request": {"album_id": aid}})
        try:
            res = ad.remove(album_ids=[aid], delete_files=bool(meta.get("delete_files")),
                            idempotency_key=operation_id)
        except Exception as exc:
            if _transport_error(exc):
                st.append_log(operation_id, "Engine call outcome unknown (transport error); left Running "
                                            "for verification -- do not re-apply.")
            else:
                st.update(operation_id, status="Failed", logs=["Engine refused the album removal."])
            raise
        gone = not ad.get_album(aid)
        status = "Completed" if gone else "Recovery Required"
        st.update(operation_id, status=status, metadata={"engine_result": res if isinstance(res, dict) else {}},
                  logs=[f"Removed album {aid} and {len(live)} item row(s); "
                        f"files {'deleted' if meta.get('delete_files') else 'kept on disk'}."])
    return {"ok": gone, "operation_id": operation_id, "status": status, "mutated": True,
            "delete_files": bool(meta.get("delete_files")), "removed_item_ids": live,
            "deleted": live if meta.get("delete_files") else [],
            **({} if gone else {"error": "Album row still present after removal.", "code": "verification_failed"})}


def finish_album_cleanup(
    operation_id: str,
    res: Dict[str, Any],
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Restart recovery for an album cleanup left Running: verify from live
    Beets, never re-apply."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    meta = st.get(operation_id).get("metadata") or {}
    aid = int(meta.get("album_id") or 0)
    gone = bool(aid) and not ad.get_album(aid)
    status = "Completed" if gone else "Recovery Required"
    engine = res.get("result") if isinstance(res.get("result"), dict) else res
    st.update(operation_id, status=status, metadata={"engine_result": engine or {"recovered": True}},
              logs=[f"Recovered after restart: album {aid} {'is gone' if gone else 'is still present'}."])
    return {"ok": gone, "operation_id": operation_id, "status": status}


def remove_album_rows_after_failed_import(
    album_id: int,
    *,
    reason: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Undo the album ROW a failed import created -- files are never
    deleted (LT-17). Used only by automatic import rollback paths."""
    plan = plan_album_cleanup(album_id, adapter=adapter, store=store, delete_files=False, reason=reason)
    if not plan.get("ok"):
        return plan
    st = _get_store(store)
    if st.transition(plan["operation_id"], "Preview", "Approved",
                     metadata={"approved_by": f"automatic import rollback (row-only): {_s(reason)}"}) is None:
        return {"ok": False, "code": "not_approved", "error": "Could not approve the row-only removal."}
    return apply_album_cleanup(plan["operation_id"], adapter=adapter, store=store)




# -----------------------------------------------------------------------------
# 6b. Track quarantine (operator-selected bad tracks; never deleted)
# -----------------------------------------------------------------------------


TRACK_QUARANTINE_FAMILY = "track_quarantine_v1"


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def plan_track_quarantine(
    album_id: int,
    item_ids: List[int],
    *,
    reason: str = "",
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    create: bool = True,
) -> Dict[str, Any]:
    """Preview moving operator-selected tracks of one album into the engine
    quarantine (row removed, file kept and restorable). Read-only.

    Every item must still be in ``album_id`` with its file present; its
    SHA-256 is pinned so the engine refuses a file that changed. Selecting
    every item of the album is refused (the engine never empties an album
    row implicitly; use an album cleanup). ``create=False`` validates only."""
    ad = adapter or beets_adapter
    ids = sorted({int(x) for x in item_ids or [] if str(x).strip().lstrip("-").isdigit() and int(x) > 0})
    if not ids:
        return {"ok": False, "code": "empty_selection", "error": "No tracks were selected."}
    members = {int(it.get("id")) for it in ad.find_all_items_by_album_id(int(album_id)) if it.get("id")}
    if not members:
        return {"ok": False, "code": "album_not_found", "error": f"Album {album_id} has no tracks."}
    if set(ids) >= members:
        return {"ok": False, "code": "album_would_empty",
                "error": "Every track of the album was selected; remove the album through an album cleanup."}
    entries: List[Dict[str, Any]] = []
    problems: List[Dict[str, Any]] = []
    for iid in ids:
        item = ad.get_item(iid)
        if not item or iid not in members:
            problems.append({"item_id": iid, "reason": "not_in_album"})
            continue
        path = _item_abs_path(item.get("path"))
        if not path or not os.path.isfile(path) or os.path.islink(path):
            problems.append({"item_id": iid, "reason": "file_missing"})
            continue
        entries.append({"item_id": iid, "sha256": _file_sha256(path), "path": path,
                        "title": item.get("title", ""), "track": item.get("track"), "disc": item.get("disc"),
                        "mb_trackid": item.get("mb_trackid", "")})
    if problems:
        return {"ok": False, "code": "selection_invalid", "problems": problems,
                "error": "Some selected tracks are not removable; nothing was planned."}
    if not create:
        return {"ok": True, "dry_run": True, "items": entries, "album_id": int(album_id)}
    tx = _get_store(store).create(
        operation_type="Delete",
        status="Preview",
        summary=f"Quarantine {len(entries)} track(s) of album {album_id} (files kept, restorable)",
        reason=_s(reason),
        changes=[{"action": "quarantine_remove", "item_id": e["item_id"], "path": e["path"], "title": e["title"]}
                 for e in entries],
        rollback_available=True,
        metadata={"mutation_family": TRACK_QUARANTINE_FAMILY, "album_id": int(album_id), "items": entries},
    )
    return {"ok": True, "operation_id": tx["id"], "status": "Preview", "requires_approval": True,
            "album_id": int(album_id), "items": entries}


def apply_track_quarantine(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Apply an Approved track quarantine through the Beets engine."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    try:
        tx = st.get(operation_id)
    except KeyError:
        return {"ok": False, "code": "not_found", "error": "Transaction not found"}
    meta = tx.get("metadata") or {}
    if meta.get("mutation_family") != TRACK_QUARANTINE_FAMILY:
        return {"ok": False, "code": "wrong_family", "error": "Not a track quarantine transaction."}
    if meta.get("engine_result"):
        return {"ok": False, "code": "already_applied", "error": "This quarantine was already applied."}
    if tx.get("status") != "Approved":
        return {"ok": False, "code": "not_approved", "error": "Approve the transaction before applying it."}
    entries = meta.get("items") or []
    aid = int(meta["album_id"])
    from backend.resource_locks import attempt_owner, claim_approved, claim_refusal, locks as resource_locks
    with resource_locks().hold([f"album:{aid}"] + [f"item:{int(e['item_id'])}" for e in entries],
                               attempt_owner(operation_id), timeout=10):
        if claim_approved(st, operation_id) is None:
            return {"ok": False, "code": "not_approved", "error": claim_refusal(st, operation_id)}
        items_before = int((ad.get_stats() or {}).get("items") or 0)
        st.update(operation_id, status="Running", metadata={"engine_request": {"items_before": items_before}})
        try:
            res = ad.quarantine_remove_items([{"item_id": int(e["item_id"]), "sha256": e["sha256"]} for e in entries],
                                             idempotency_key=operation_id)
        except Exception as exc:
            if _transport_error(exc):
                st.append_log(operation_id, "Engine call outcome unknown (transport error); left Running for "
                                            "the recovery sweep -- do not re-apply.")
            else:
                st.update(operation_id, status="Failed", logs=["Engine refused the quarantine; nothing was removed."])
            raise
        return finish_track_quarantine(operation_id, res, adapter=ad, store=st)


def finish_track_quarantine(
    operation_id: str,
    res: Dict[str, Any],
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Verify an applied quarantine from engine evidence (also used by the
    restart recovery sweep -- never re-applies)."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    meta = st.get(operation_id).get("metadata") or {}
    engine = res.get("result") if isinstance(res.get("result"), dict) else res
    entries = meta.get("items") or []
    problems = [f"item {e['item_id']} still in library" for e in entries if ad.get_item(int(e["item_id"]))]
    items_before = int(((meta.get("engine_request") or {}).get("items_before")) or 0)
    items_after = int((ad.get_stats() or {}).get("items") or 0)
    if items_before and items_before - items_after != len(entries):
        problems.append(f"library item count changed by {items_before - items_after}, expected {len(entries)}")
    status = "Completed" if not problems else "Recovery Required"
    st.update(operation_id, status=status,
              metadata={"engine_result": engine, "verification_problems": problems, "items_after": items_after},
              logs=[f"Quarantined item {r.get('item_id')}: {r.get('quarantine_path')}" for r in engine.get("removed") or []]
              + [f"Verification problem: {p}" for p in problems])
    return {"ok": not problems, "operation_id": operation_id, "status": status,
            "quarantine_id": engine.get("quarantine_id"), "removed": engine.get("removed") or [],
            "verification_problems": problems}


def rollback_track_quarantine(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Restore quarantined tracks through the engine's own manifest."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    try:
        tx = st.get(operation_id)
    except KeyError:
        return {"ok": False, "code": "not_found", "error": "Transaction not found"}
    meta = tx.get("metadata") or {}
    engine = meta.get("engine_result") or {}
    if meta.get("mutation_family") != TRACK_QUARANTINE_FAMILY or not engine.get("quarantine_id"):
        return {"ok": False, "code": "not_applied", "error": "No applied track quarantine to roll back."}
    if tx.get("status") == "Rolled Back":
        return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}
    refusal = engine_rollback_refusal(tx)
    if refusal:
        return refusal
    res = ad.rollback_quarantine_remove_items(engine["quarantine_id"], idempotency_key=f"{operation_id}:rollback")
    result = res.get("result") if isinstance(res.get("result"), dict) else (res or {})
    if res.get("ok") is False or result.get("ok") is False:
        st.append_log(operation_id, f"Engine rollback refused or failed: {result.get('error') or res.get('error')}")
        return {"ok": False, "code": "rollback_failed", "operation_id": operation_id,
                "status": tx.get("status"), "error": result.get("error") or res.get("error") or "Rollback failed."}
    moved = st.transition(operation_id, tx.get("status"), "Rolled Back", metadata={"rollback_result": result},
                          logs=[f"Restored item {r.get('old_item_id')} as {r.get('new_item_id')} at {r.get('path')}"
                                for r in result.get("restored") or []])
    if moved is None:
        return _rollback_conflict(operation_id)
    return {"ok": True, "operation_id": operation_id, "status": "Rolled Back", "restored": result.get("restored") or []}


# -----------------------------------------------------------------------------
# 7. Artwork Workflows
# -----------------------------------------------------------------------------


def plan_album_artwork_fetch(
    payload_or_album_id: Any = None,
    *,
    album_id: Optional[int] = None,
    store: Optional[TransactionStore] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    st = _get_store(store)
    payload = payload_or_album_id if isinstance(payload_or_album_id, dict) else kwargs
    aid = payload.get("album_id") or album_id or (payload_or_album_id if isinstance(payload_or_album_id, int) else None)
    if not aid:
        return {"ok": False, "error": "album_id is required"}
    data = {"album_id": int(aid)}
    tx = st.create(
        operation_type="Artwork Update",
        status="Preview",
        summary=f"Fetch and embed artwork for album {aid}",
        metadata=data,
    )
    return {"ok": True, "operation_id": tx["id"], "token": tx["id"], "status": "Preview", **data}


def apply_album_artwork_fetch(
    operation_id: Optional[str] = None,
    *,
    plan_token: Optional[str] = None,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    op_id = operation_id or plan_token
    if not op_id:
        return {"ok": False, "error": "operation_id or plan_token is required"}
    ad = adapter or beets_adapter
    st = _get_store(store)
    tx = st.get(op_id)
    aid = tx.get("metadata", {}).get("album_id")

    def write() -> Dict[str, Any]:
        artpath = ""
        if aid:
            ad.fetch_art(album_ids=[int(aid)])
            ad.embed_art(album_ids=[int(aid)])
            album = ad.get_album(int(aid))
            artpath = album.get("artpath", "") if album else ""
        return {"artpath": artpath}

    return _apply_claimed(st, op_id, write)


def rollback_album_artwork_fetch(
    operation_id: str,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    return _rollback_noop(operation_id, store)


def fetch_and_embed_album_art(
    album_id: int,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    timeout: float = 90.0,
) -> Dict[str, Any]:
    """Fetch cover art and embed it into tracks via real Beets plugins."""
    plan_res = plan_album_artwork_fetch({"album_id": int(album_id)}, store=store)
    if not plan_res.get("ok"):
        return {"ok": False, "error": plan_res.get("error") or "Artwork fetch plan rejected", "code": plan_res.get("code")}
    op_id = plan_res.get("operation_id")
    if not op_id:
        return {"ok": True, "album_id": int(album_id)}
    apply_res = apply_album_artwork_fetch(op_id, adapter=adapter, store=store)
    if not apply_res.get("ok"):
        return apply_res
    return {
        "ok": True,
        "status": "Completed",
        "album_id": int(album_id),
        "artpath": apply_res.get("artpath", ""),
        "operation_id": op_id,
    }


def delete_album_art(
    album_id: int,
    adapter: Optional[BeetsAdapter] = None,
) -> Dict[str, Any]:
    """Clear album artpath and art."""
    ad = adapter or beets_adapter
    aid = int(album_id)
    ad.modify(fields={"artpath": ""}, album_ids=[aid])
    return {"ok": True, "album_id": aid}


#: Operator-supplied album cover set through Beets (plugin 1.12.0,
#: POST /webmanager/album-art): Album.set_art + embedart's art_set listener.
ALBUM_ART_REPLACE_FAMILY = "album_art_replace_v1"

#: Engine refusals that changed nothing, with the text shown to the operator.
_ART_REFUSALS = {
    "INVALID_IMAGE": "The image is not a JPEG, PNG or WebP file.",
    "IMAGE_TOO_LARGE": "The image is larger than 15 MB.",
    "IMAGE_HASH_MISMATCH": "The image was damaged on the way to Beets; try again.",
    "IDENTITY_CHANGED": "The album's release group changed since the artwork was chosen; nothing was changed.",
    "ALBUM_NOT_FOUND": "Beets no longer has this album.",
    "ALBUM_EMPTY": "The album has no tracks, so Beets has no folder for its artwork.",
    "DESTINATION_PATH_INVALID": "The album folder is outside the folders Beets may write to.",
    "OLD_ART_PATH_INVALID": "The album's current cover is outside the folders Beets may write to.",
    "BEETS_NOT_FOUND": "Restart the beets container so it loads webmanager plugin 1.12.0; nothing was changed.",
}


def plan_album_art_replace(
    album_id: int,
    image: bytes,
    *,
    source: str = "",
    expected_mb_releasegroupid: str = "",
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Preview setting ``image`` as the album's cover: records the current
    artpath and the image's hash. The image itself is not stored."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    aid = int(album_id)
    if not image:
        return {"ok": False, "code": "invalid_image", "error": "No image was supplied."}
    album = ad.get_album(aid, expand=False)
    if not album:
        return {"ok": False, "code": "not_found", "error": "Album not found"}
    rgid = _s(album.get("mb_releasegroupid")).strip().lower()
    expected = _s(expected_mb_releasegroupid).strip().lower()
    if expected and rgid != expected:
        return {"ok": False, "code": "identity_changed", "error": _ART_REFUSALS["IDENTITY_CHANGED"]}
    digest = hashlib.sha256(image).hexdigest()
    old_artpath = _s(album.get("artpath"))
    tx = st.create(
        operation_type="Artwork Update",
        status="Preview",
        summary=f"Set the cover of album {aid} ({_s(album.get('album'))}) from {source or 'an uploaded image'}",
        source=_s(source),
        changes=[{"type": "album_art_replace", "album_id": aid, "old_artpath": old_artpath,
                  "image_sha256": digest, "image_bytes": len(image)}],
        rollback_available=True,
        metadata={"mutation_family": ALBUM_ART_REPLACE_FAMILY, "album_id": aid, "image_sha256": digest,
                  "image_bytes": len(image), "expected_mb_releasegroupid": expected or rgid,
                  "before": {"artpath": old_artpath}, "source": _s(source)},
    )
    return {"ok": True, "operation_id": tx["id"], "token": tx["id"], "status": "Preview",
            "album_id": aid, "old_artpath": old_artpath, "image_sha256": digest}


def apply_album_art_replace(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    *,
    image: Optional[bytes] = None,
) -> Dict[str, Any]:
    """Apply an Approved cover replacement through Beets. ``image`` must be
    the planned image (checked by hash); it is not kept between requests, so
    an apply without it is refused and changes nothing."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    try:
        tx = st.get(operation_id)
    except KeyError:
        return {"ok": False, "code": "not_found", "error": "Transaction not found"}
    meta = tx.get("metadata") or {}
    if meta.get("mutation_family") != ALBUM_ART_REPLACE_FAMILY:
        return {"ok": False, "code": "wrong_family", "error": "Not an album artwork replacement transaction."}
    if meta.get("engine_result"):
        return {"ok": False, "code": "already_applied", "error": "This artwork replacement was already applied."}
    if tx.get("status") != "Approved":
        return {"ok": False, "code": "not_approved", "error": "Approve the transaction before applying it."}
    if not image or hashlib.sha256(image).hexdigest() != meta.get("image_sha256"):
        return {"ok": False, "code": "image_unavailable", "mutated": False,
                "error": "The planned image is not available; upload it again."}

    aid = int(meta["album_id"])
    from backend.resource_locks import attempt_owner, claim_approved, claim_refusal, locks as resource_locks
    with resource_locks().hold([f"album:{aid}"], attempt_owner(operation_id), timeout=10):
        if claim_approved(st, operation_id) is None:
            return {"ok": False, "code": "not_approved", "error": claim_refusal(st, operation_id)}
        # Recorded before the engine call: a restart mid-call is finished from
        # engine evidence by backend/transaction_recovery.py, never replayed.
        st.update(operation_id, status="Running", metadata={"engine_request": {"operation_id": operation_id}})
        try:
            res = ad.set_album_art(aid, image, expected_mb_releasegroupid=meta.get("expected_mb_releasegroupid") or "",
                                   idempotency_key=operation_id)
        except BeetsAdapterError as exc:
            if _transport_error(exc):
                st.update(operation_id, status="Recovery Required",
                          logs=["Beets did not answer; whether the artwork changed is unknown."])
                raise
            code = exc.error_code or ""
            message = _ART_REFUSALS.get(code)
            st.update(operation_id, status="Failed",
                      logs=[f"Beets refused the artwork change ({code or 'error'}); "
                            + ("nothing was changed." if message else
                               "Beets tried to put the previous artwork back; check the album.")])
            # Only a known refusal proves nothing changed; any other failure
            # depends on the plugin's compensation, which may itself have failed.
            return {"ok": False, "code": code.lower() or "beets_error", "mutated": False if message else None,
                    "operation_id": operation_id, "status": "Failed",
                    "error": message or "Beets could not set the artwork; check the album's cover."}
        return finish_album_art_replace(operation_id, res, adapter=ad, store=st)


def finish_album_art_replace(
    operation_id: str,
    res: Dict[str, Any],
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Verify the engine's result against live Beets and record it (also
    used by restart recovery -- never re-applies)."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    meta = st.get(operation_id).get("metadata") or {}
    engine = res.get("result") if isinstance(res.get("result"), dict) else res
    album = ad.get_album(int(meta["album_id"]), expand=False) or {}
    artpath = _s(album.get("artpath"))
    # embedart's remove_art_file may leave the art embedded only (artpath "").
    same = album and (artpath == _s(engine.get("artpath")) or _same_library_path(artpath, engine.get("artpath")))
    problems = [] if same else ["artpath"]
    status = "Completed" if not problems else "Recovery Required"
    st.update(
        operation_id, status=status,
        metadata={**meta, "engine_result": engine, "after": {"artpath": artpath}, "verification_problems": problems},
        logs=[f"Album {meta['album_id']} cover is now {artpath or '(embedded only)'}",
              f"Embedded into {engine.get('embedded_items', 0)} of {engine.get('item_count', 0)} tracks",
              f"Previous cover kept by Beets (engine id {engine.get('art_id')})"]
             + ([f"Verification mismatch: {', '.join(problems)}"] if problems else []),
    )
    return {"ok": not problems, "operation_id": operation_id, "status": status, "album_id": int(meta["album_id"]),
            "artpath": artpath, "embedded_items": engine.get("embedded_items", 0),
            "verification_problems": problems}


def rollback_album_art_replace(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Put the previous cover file, artpath and embedded art back through
    Beets (the engine restores from its own manifest)."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    try:
        tx = st.get(operation_id)
    except KeyError:
        return {"ok": False, "code": "not_found", "error": "Transaction not found"}
    meta = tx.get("metadata") or {}
    engine = meta.get("engine_result") or {}
    if meta.get("mutation_family") != ALBUM_ART_REPLACE_FAMILY or not engine.get("art_id"):
        return {"ok": False, "code": "not_applied", "error": "No applied artwork replacement to roll back."}
    if tx.get("status") == "Rolled Back":
        return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}
    refusal = engine_rollback_refusal(tx)
    if refusal:
        return refusal
    res = ad.rollback_album_art(engine["art_id"], idempotency_key=f"{operation_id}:rollback")
    result = res.get("result") if isinstance(res.get("result"), dict) else res
    restored = _s(result.get("restored_artpath"))
    if st.transition(
        operation_id, tx.get("status"), "Rolled Back",
        metadata={**meta, "rollback_result": result},
        logs=[f"Restored album {meta['album_id']} cover to {restored or '(none)'}; "
              f"embedded art restored on {result.get('restored_embedded_items', 0)} tracks"],
    ) is None:
        return _rollback_conflict(operation_id)
    return {"ok": True, "operation_id": operation_id, "status": "Rolled Back", "restored_artpath": restored}


def replace_album_art(
    album_id: int,
    image_data: Any = b"",
    ext: str = "jpg",
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    *,
    source: str = "",
    expected_mb_releasegroupid: str = "",
    **_kwargs: Any,
) -> Dict[str, Any]:
    """Set an operator-supplied cover (upload or URL) through Beets as one
    audited, reversible transaction: plan, record the operator's request as
    the approval, apply. ``image_data`` is bytes or base64 text; ``ext`` is
    ignored (Beets names the file from the image bytes)."""
    try:
        image = base64.b64decode(image_data, validate=True) if isinstance(image_data, str) else bytes(image_data or b"")
    except (ValueError, TypeError):
        return {"ok": False, "code": "invalid_image", "error": "The image data is not valid."}
    st = _get_store(store)
    plan = plan_album_art_replace(album_id, image, source=source,
                                  expected_mb_releasegroupid=expected_mb_releasegroupid, adapter=adapter, store=st)
    if not plan.get("ok"):
        return plan
    from backend.resource_locks import approve_preview
    if approve_preview(st, plan["operation_id"], f"operator artwork {source or 'upload'}") is None:
        return {"ok": False, "code": "not_preview", "operation_id": plan["operation_id"],
                "error": "The artwork change was cancelled before it ran."}
    return apply_album_art_replace(plan["operation_id"], adapter=adapter, store=st, image=image)


def plan_album_artwork(
    payload: Dict[str, Any],
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    st = _get_store(store)
    aid = int(payload.get("album_id") or 0)
    tx = st.create(
        operation_type="Artwork Update",
        status="Preview",
        summary=f"Update artwork for album {aid}",
        metadata=payload,
    )
    return {"ok": True, "operation_id": tx["id"], "token": tx["id"], "status": "Preview", **payload}


def apply_album_artwork(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    ad = adapter or beets_adapter
    st = _get_store(store)
    tx = st.get(operation_id)
    meta = tx.get("metadata", {})
    aid = meta.get("album_id")

    def write() -> Optional[Dict[str, Any]]:
        if not aid:
            return None
        res = fetch_and_embed_album_art(int(aid), adapter=ad, store=st)
        return None if res.get("ok") else {"ok": False, "error": res.get("error") or "Artwork fetch failed."}

    return _apply_claimed(st, operation_id, write)


def rollback_album_artwork(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    return _rollback_noop(operation_id, store)


# -----------------------------------------------------------------------------
# 8. MB Track Repair & Metadata Repair Workflows
# -----------------------------------------------------------------------------


_MB_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _uuid_or_blank(value: Any) -> str:
    text = _s(value).strip().lower()
    return text if _MB_UUID.match(text) else ""


def _release_group_for_release(release_id: str) -> str:
    """Authoritative Release Group of a Release from MusicBrainz ("" if
    unknown). Same lookup as musicbrainz_service._mb_release_group_for_release,
    which this adapter-layer module cannot import."""
    from helpers_mb import _fetch_mb_release_candidate
    return _s((_fetch_mb_release_candidate(release_id) or {}).get("mb_releasegroupid") or "").strip().lower()


def _identity_write_error(fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The invariant every album identity write must hold (MI-1/MI-2): a
    Release Group field is never blanked or malformed, and a Release ID is
    never written without its Release Group. Pure; Plan and Apply both run it."""
    if "mb_releasegroupid" in fields and not _uuid_or_blank(fields.get("mb_releasegroupid")):
        return {"ok": False, "code": "release_group_blank",
                "error": "Refusing to blank or write an invalid Release Group ID."}
    if _s(fields.get("mb_albumid")).strip() and not _uuid_or_blank(fields.get("mb_releasegroupid")):
        return {"ok": False, "code": "release_group_required",
                "error": "Refusing to write a Release ID without its verified Release Group ID."}
    return None


def _verified_album_identity(album: Dict[str, Any], release_id: str, release_group_id: str = "", *,
                             allow_establish: bool, rg_explicit: bool = False) -> Dict[str, Any]:
    """Verify (release, release group) for an album row through the ARCH-009
    contract. Refuses an unverifiable pairing, a Release from another Release
    Group than the album's (unless the caller explicitly supplies the new,
    verified Release Group: a relink), and establishing a Release Group on an
    album that has none unless ``allow_establish``."""
    from backend.identity_contract import verify_album_identity
    identity = verify_album_identity(release_group_id, release_id,
                                     resolve_release_group=_release_group_for_release)
    if not identity.ok:
        return {"ok": False, "code": identity.code, "error": identity.error}
    current_rg = _uuid_or_blank(album.get("mb_releasegroupid"))
    if current_rg and current_rg != identity.release_group_id and not rg_explicit:
        return {"ok": False, "code": "repair_identity_mismatch",
                "error": f"Release {identity.release_id} belongs to release group {identity.release_group_id}, "
                         f"but the album is {current_rg}; refusing to change canonical identity implicitly."}
    if not current_rg and not allow_establish:
        return {"ok": False, "code": "repair_rg_not_established",
                "error": "The album has no Release Group ID; this repair may not establish one."}
    return {"ok": True, "release_id": identity.release_id, "release_group_id": identity.release_group_id}


def _engine_ok(res: Any) -> Any:
    """Raise when the engine answered a write with ok=false."""
    if isinstance(res, dict) and res.get("ok") is False:
        raise RuntimeError(_s(res.get("error") or res.get("code") or "engine refused the write"))
    return res


def _album_identity_snapshot(album: Dict[str, Any]) -> Dict[str, str]:
    return {k: _s(album.get(k)).strip().lower() for k in ("mb_albumid", "mb_releasegroupid")}


def plan_album_mb_track_repair(
    payload_or_album_id: Any = None,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    **kwargs,
) -> Dict[str, Any]:
    """Plan an MusicBrainz sync of an album's tracks (``mbsync``).

    The plan validates and records everything Apply will do (MI-1): the
    target Release and its verified Release Group, whether a Release Group
    may be established, and any explicit Recording IDs (``track_mbids``).
    Options mbsync cannot honor (``target_tracks``, ``zero_unmatched``) are
    refused rather than silently widened to the whole album. mbsync never
    assigns Recording IDs from text or position, so no ``mb_trackid`` is
    written from fuzzy alignment here (MI-18)."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    payload = dict(payload_or_album_id if isinstance(payload_or_album_id, dict) else kwargs)

    aid = int(payload.get("album_id") or payload.get("aid") or (payload_or_album_id if isinstance(payload_or_album_id, (int, str)) and str(payload_or_album_id).isdigit() else 0))
    if not aid:
        return {"ok": False, "error": "album_id required"}
    unsupported = sorted(k for k in ("target_tracks", "zero_unmatched") if payload.get(k))
    if unsupported:
        return {"ok": False, "code": "repair_option_unsupported",
                "error": f"MusicBrainz sync cannot honor {', '.join(unsupported)}; nothing was planned."}

    album = ad.get_album(aid)
    if not album:
        return {"ok": False, "error": f"Album {aid} not found"}
    items = ad.find_all_items_by_album_id(aid)
    by_id = {int(it.get("id") or 0): it for it in items}

    track_mbids: Dict[str, str] = {}
    for raw_id, raw_mbid in (payload.get("track_mbids") or {}).items():
        iid, mbid = int(raw_id), _uuid_or_blank(raw_mbid)
        if iid not in by_id:
            return {"ok": False, "code": "repair_item_not_in_album", "error": f"Item {iid} is not on album {aid}."}
        if not mbid:
            return {"ok": False, "code": "invalid_recording_id", "error": f"Invalid Recording ID for item {iid}."}
        track_mbids[str(iid)] = mbid

    current = _album_identity_snapshot(album)
    target_rel = _s(payload.get("mb_albumid")).strip().lower() or current["mb_albumid"]
    target_rg = ""
    if target_rel:
        verdict = _verified_album_identity(album, target_rel,
                                           allow_establish=bool(payload.get("allow_establish_release_group")))
        if not verdict["ok"]:
            return verdict
        target_rg = verdict["release_group_id"]
    elif not track_mbids:
        return {"ok": False, "code": "repair_no_release",
                "error": "The album has no Release ID to sync from; nothing was planned."}

    before = {"album": {"id": aid, "fields": _restorable(album)},
              "items": [{"id": int(it.get("id") or 0), "fields": _restorable(it)} for it in items]}
    changes = [{"item_id": it.get("id"), "title": it.get("title"), "mb_trackid": it.get("mb_trackid"),
                "new_mb_trackid": track_mbids.get(str(it.get("id")))} for it in items]
    plan = {"album_id": aid, "release_id": target_rel, "release_group_id": target_rg,
            "establish_release_group": bool(target_rg) and not current["mb_releasegroupid"],
            "track_mbids": track_mbids, "live_identity": current,
            "live_track_ids": {k: _s(by_id[int(k)].get("mb_trackid")).strip().lower() for k in track_mbids}}
    tx = st.create(
        operation_type="MusicBrainz Match",
        status="Preview",
        summary=f"Repair MB track metadata for album {aid} ({album.get('album')})",
        changes=changes,
        # #228: the generic rollback route cannot reach this family (no
        # rollback.operations); only rollback_album_mb_track_repair can (ARCH-023).
        rollback_available=False,
        metadata={"album_id": aid, "before_state": before, "payload": payload, "plan": plan},
    )
    return {
        "ok": True,
        "operation_id": tx["id"],
        "token": tx["id"],
        "status": "Preview",
        "album": album,
        "items": items,
        "changes": changes,
        "updated": len(track_mbids),
    }


def apply_album_mb_track_repair(
    operation_id: str,
    write_tags: bool = True,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Apply exactly what the plan validated, after re-checking it against
    the live library (MI-1)."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    tx = st.get(operation_id)
    plan = (tx.get("metadata") or {}).get("plan")
    if not plan:
        return {"ok": False, "code": "repair_plan_missing", "operation_id": operation_id,
                "error": "This transaction has no validated repair plan; re-plan it."}
    if tx.get("status") not in ("Preview", "Approved"):
        return _apply_refused(st, operation_id)
    aid = int(plan["album_id"])
    album = ad.get_album(aid)
    stale = ""
    if not album:
        stale = f"Album {aid} no longer exists."
    elif _album_identity_snapshot(album) != plan["live_identity"]:
        stale = "The album's Release or Release Group changed since the plan."
    else:
        live_items = {str(it.get("id")): it for it in ad.find_all_items_by_album_id(aid)}
        for iid, before_mbid in (plan.get("live_track_ids") or {}).items():
            if iid not in live_items or _s(live_items[iid].get("mb_trackid")).strip().lower() != before_mbid:
                stale = f"Item {iid} changed since the plan."
                break
    if stale:
        st.transition(operation_id, tx.get("status"), "Failed", logs=[f"Apply refused: {stale} Nothing was changed."])
        return {"ok": False, "code": "repair_plan_stale", "operation_id": operation_id, "mutated": False,
                "error": f"{stale} Nothing was changed; re-plan it."}
    target_rel, target_rg = plan.get("release_id") or "", plan.get("release_group_id") or ""
    album_fields = {"mb_albumid": target_rel, "mb_releasegroupid": target_rg} if target_rel else {}
    refusal = _identity_write_error(album_fields)
    if refusal:
        return {**refusal, "operation_id": operation_id, "mutated": False}
    if _claim_apply(st, operation_id, metadata={"engine_result": {"mutation_started": True}}) is None:
        return _apply_refused(st, operation_id)
    try:
        if target_rel and (target_rel, target_rg) != (plan["live_identity"]["mb_albumid"],
                                                     plan["live_identity"]["mb_releasegroupid"]):
            _engine_ok(ad.modify(fields=album_fields, album_ids=[aid], write=write_tags, move=False))
        for iid, mbid in (plan.get("track_mbids") or {}).items():
            _engine_ok(ad.modify(fields={"mb_trackid": mbid}, item_ids=[int(iid)], write=write_tags, move=False))
        if target_rel:
            _engine_ok(ad.mbsync(album_ids=[aid], write=write_tags, move=write_tags))
        after = _album_identity_snapshot(ad.get_album(aid) or {})
    except Exception as ex:
        st.transition(operation_id, "Running", "Failed", logs=[f"Apply failed: {type(ex).__name__}: {str(ex)[:200]}"])
        return {"ok": False, "code": "repair_apply_failed", "operation_id": operation_id, "mutated": True,
                "rollback_available": True, "error": f"MusicBrainz repair failed: {str(ex)[:200]}"}
    if target_rel and after != {"mb_albumid": target_rel, "mb_releasegroupid": target_rg}:
        st.transition(operation_id, "Running", "Failed",
                      logs=[f"Album identity did not verify after the sync: {after}"])
        return {"ok": False, "code": "repair_identity_unverified", "operation_id": operation_id, "mutated": True,
                "rollback_available": True,
                "error": "The album's Release/Release Group did not verify after the sync; roll it back."}
    st.transition(operation_id, "Running", "Completed",
                  metadata={"engine_result": {"album_identity": after, "track_mbids": plan.get("track_mbids") or {}}})
    return {"ok": True, "operation_id": operation_id, "status": "Completed",
            "updated": len(plan.get("track_mbids") or {})}


def rollback_album_mb_track_repair(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Restore every album and track value captured at plan time."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    before = (st.get(operation_id).get("metadata") or {}).get("before_state")
    if not isinstance(before, dict):
        return {"ok": False, "code": "rollback_unavailable", "operation_id": operation_id,
                "error": "This transaction recorded no before-state to restore."}
    if _claim_rollback(st, operation_id) is None:
        return _rollback_refused(st, operation_id)
    restored, failed = _restore_rows(ad, before, write=True)
    return _finish_rollback(st, operation_id, restored, failed)


#: Identity fields a per-item metadata update may not write (album-level).
_ALBUM_IDENTITY_FIELDS = ("mb_albumid", "mb_releasegroupid")


ALBUM_METADATA_FAMILY = "album_metadata_repair_v1"
ITEM_METADATA_FAMILY = "item_metadata_repair_v1"
#: Album rename / move to library through Beets (plugin 1.14.0,
#: POST /webmanager/album-relocation). Older album_relocation_v1 records
#: (no before-state) stay unrollbackable; this family records every path.
ALBUM_RELOCATION_FAMILY = "album_move_v1"
GENRE_REPAIR_FAMILY = "genre_repair_v1"


def plan_album_metadata(
    payload: Optional[Dict[str, Any]] = None,
    *,
    album_id: Optional[int] = None,
    album_fields: Optional[Dict[str, Any]] = None,
    track_fields: Optional[Dict[Any, Dict[str, Any]]] = None,
    updates: Optional[Dict[str, Any]] = None,
    item_updates: Optional[Dict[Any, Dict[str, Any]]] = None,
    force_write_tags: bool = False,
    write_tags: bool = True,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Plan an album/track metadata write. Identity writes are validated
    (MI-2): a Release ID is written only with its verified Release Group
    (resolved from MusicBrainz when not supplied); a Release from another
    Release Group than the album's needs that Release Group stated
    explicitly; a Release Group is never blanked; per-item album identity
    writes are refused. The live values of every field written are captured
    so Apply can detect a stale plan and rollback can restore them."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    if payload is None:
        payload = {
            "album_id": album_id,
            "updates": album_fields if album_fields is not None else (updates or {}),
            "item_updates": track_fields if track_fields is not None else (item_updates or {}),
            "force_write_tags": force_write_tags,
            "write_tags": write_tags,
            **kwargs,
        }
    payload = dict(payload)
    aid = int(payload.get("album_id") or 0)
    album_updates = dict(payload.get("updates") or {})
    per_item = {str(k): dict(v or {}) for k, v in (payload.get("item_updates") or {}).items()}
    if any(f in fields for fields in per_item.values() for f in _ALBUM_IDENTITY_FIELDS):
        return {"ok": False, "code": "item_identity_write_refused",
                "error": "Release and Release Group IDs are album identity; write them on the album, not per item."}
    album = ad.get_album(aid) if aid else None
    if not album:
        return {"ok": False, "code": "album_not_found", "error": f"Album {aid} not found"}
    refusal = _identity_write_error({k: v for k, v in album_updates.items() if k == "mb_releasegroupid"})
    if refusal:
        return refusal
    release_id = _s(album_updates.get("mb_albumid")).strip().lower()
    if release_id:
        # An operator's explicit Release choice (manual match, import review,
        # duplicate resolver) may move the album to that Release's own
        # Release Group, which is still resolved and verified from MusicBrainz;
        # an inferred or automatic Release may not.
        verdict = _verified_album_identity(
            album, release_id, _s(album_updates.get("mb_releasegroupid")), allow_establish=True,
            rg_explicit="mb_releasegroupid" in album_updates or bool(payload.get("release_selected_by_operator")))
        if not verdict["ok"]:
            return verdict
        album_updates["mb_albumid"] = verdict["release_id"]
        album_updates["mb_releasegroupid"] = verdict["release_group_id"]
    elif "mb_releasegroupid" in album_updates and _uuid_or_blank(album.get("mb_albumid")):
        # A Release-Group-only write must agree with the album's Release.
        verdict = _verified_album_identity(album, _uuid_or_blank(album.get("mb_albumid")),
                                           _s(album_updates.get("mb_releasegroupid")),
                                           allow_establish=True, rg_explicit=True)
        if not verdict["ok"]:
            return verdict
        album_updates["mb_releasegroupid"] = verdict["release_group_id"]
    refusal = _identity_write_error(album_updates)
    if refusal:
        return refusal
    payload["updates"], payload["item_updates"] = album_updates, per_item
    # Music-identity F-2: an operator-selected move to another Release Group
    # is recorded, so the audit shows it and the caller can log it.
    old_rg = _uuid_or_blank(album.get("mb_releasegroupid"))
    new_rg = _uuid_or_blank(album_updates.get("mb_releasegroupid"))
    summary = f"Metadata update for album {aid}"
    if old_rg and new_rg and old_rg != new_rg:
        payload["release_group_change"] = {"from": old_rg, "to": new_rg,
                                           "operator_selected": bool(payload.get("release_selected_by_operator"))}
        summary += f" (Release Group {old_rg} -> {new_rg}, operator-selected)"
    before: Dict[str, Any] = {"album": {"id": aid, "fields": _restorable(album, album_updates)}, "items": []}
    for iid, fields in per_item.items():
        item = ad.get_item(int(iid))
        if not item or int(item.get("album_id") or 0) != aid:
            return {"ok": False, "code": "item_not_in_album", "error": f"Item {iid} is not on album {aid}."}
        before["items"].append({"id": int(iid), "fields": _restorable(item, fields)})
    tx = st.create(
        operation_type="Metadata Update",
        status="Preview",
        summary=summary,
        rollback_available=True,
        rollback_reason="Restores the album and track values captured before the update.",
        metadata={**payload, "before_state": before, "mutation_family": ALBUM_METADATA_FAMILY},
    )
    return {
        "ok": True,
        "operation_id": tx["id"],
        "token": tx["id"],
        "status": "Preview",
        "album_fields_changed": len(album_updates),
        "items_changed": len(per_item),
        **payload,
    }


def apply_album_metadata(
    operation_id: Optional[str] = None,
    *,
    plan_token: Optional[str] = None,
    force_write_tags: bool = False,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Re-validate the plan against the live rows, then write it (MI-2)."""
    op_id = operation_id or plan_token
    if not op_id:
        return {"ok": False, "error": "operation_id or plan_token is required"}
    ad = adapter or beets_adapter
    st = _get_store(store)
    tx = st.get(op_id)
    meta = tx.get("metadata", {})
    before = meta.get("before_state")
    if not isinstance(before, dict):
        return {"ok": False, "code": "metadata_plan_missing", "operation_id": op_id,
                "error": "This transaction has no validated metadata plan; re-plan it."}
    if tx.get("status") not in ("Preview", "Approved"):
        return _apply_refused(st, op_id)
    aid = int(meta.get("album_id") or 0)
    updates = meta.get("updates") or {}
    item_updates = meta.get("item_updates") or {}
    refusal = _identity_write_error(updates) or (
        {"ok": False, "code": "item_identity_write_refused", "error": "Per-item album identity writes are refused."}
        if any(f in fields for fields in item_updates.values() for f in _ALBUM_IDENTITY_FIELDS) else None)
    if refusal:
        return {**refusal, "operation_id": op_id, "mutated": False}
    rows = [("album", before.get("album") or {})] + [("item", it) for it in before.get("items") or []]
    for kind, snap in rows:
        live = ad.get_album(int(snap["id"])) if kind == "album" else ad.get_item(int(snap["id"]))
        if not live or _restorable(live, (snap.get("fields") or {}).keys()) != snap.get("fields"):
            st.transition(op_id, tx.get("status"), "Failed",
                          logs=[f"Apply refused: {kind} {snap.get('id')} changed since the plan. Nothing was changed."])
            return {"ok": False, "code": "metadata_plan_stale", "operation_id": op_id, "mutated": False,
                    "error": f"The {kind} {snap.get('id')} changed since the plan; nothing was changed. Re-plan it."}
    write = meta.get("write_tags", True)
    move = meta.get("force_write_tags", force_write_tags)
    if _claim_apply(st, op_id, metadata={"engine_result": {"mutation_started": True}}) is None:
        return _apply_refused(st, op_id)
    try:
        if aid and updates:
            _engine_ok(ad.modify(fields=updates, album_ids=[aid], write=write, move=move))
        for iid, iup in item_updates.items():
            if iup:
                _engine_ok(ad.modify(fields=iup, item_ids=[int(iid)], write=write, move=move))
    except Exception as ex:
        st.transition(op_id, "Running", "Failed", logs=[f"Apply failed: {type(ex).__name__}: {str(ex)[:200]}"])
        return {"ok": False, "code": "metadata_apply_failed", "operation_id": op_id, "mutated": True,
                "rollback_available": True, "error": f"Metadata update failed: {str(ex)[:200]}"}
    st.transition(op_id, "Running", "Completed", metadata={"engine_result": {"album_fields": sorted(updates),
                                                                              "items": sorted(item_updates)}})
    return {"ok": True, "operation_id": op_id, "status": "Completed"}


def rollback_album_metadata(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Restore the album and track values captured at plan time (MI-2)."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    meta = st.get(operation_id).get("metadata") or {}
    before = meta.get("before_state")
    if not isinstance(before, dict):
        return {"ok": False, "code": "rollback_unavailable", "operation_id": operation_id,
                "error": "This transaction recorded no before-state to restore."}
    if _claim_rollback(st, operation_id) is None:
        return _rollback_refused(st, operation_id)
    restored, failed = _restore_rows(ad, before, write=bool(meta.get("write_tags", True)))
    return _finish_rollback(st, operation_id, restored, failed)


def update_album_metadata(
    album_id: int,
    updates: Dict[str, Any],
    item_updates: Optional[Dict[str, Any]] = None,
    *,
    force_write_tags: bool = False,
    write_tags: bool = True,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    payload = {
        "album_id": int(album_id),
        "updates": updates,
        "item_updates": item_updates or {},
        "force_write_tags": force_write_tags,
        "write_tags": write_tags,
        **kwargs,
    }
    plan_res = plan_album_metadata(payload, adapter=adapter, store=store)
    if not plan_res.get("ok"):
        return {"ok": False, "error": plan_res.get("error") or "Metadata plan rejected", "code": plan_res.get("code")}
    op_id = plan_res.get("operation_id")
    if not op_id:
        return {"ok": True, "album_fields_changed": 0, "items_changed": 0}
    apply_res = apply_album_metadata(op_id, force_write_tags=force_write_tags, adapter=adapter, store=store)
    if not apply_res.get("ok"):
        return {"ok": False, "error": apply_res.get("error") or "Metadata apply failed", "code": apply_res.get("code")}
    return {
        "ok": True,
        "album_fields_changed": plan_res.get("album_fields_changed", 0),
        "items_changed": plan_res.get("items_changed", 0),
        "operation_id": op_id,
        **({"release_group_change": plan_res["release_group_change"]}
           if plan_res.get("release_group_change") else {}),
    }


def plan_item_metadata(
    payload: Optional[Dict[str, Any]] = None,
    *,
    item_id: Optional[int] = None,
    updates: Optional[Dict[str, Any]] = None,
    force_write_tags: bool = False,
    write_tags: bool = True,
    store: Optional[TransactionStore] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    st = _get_store(store)
    if payload is None:
        payload = {
            "item_id": item_id,
            "updates": updates or {},
            "force_write_tags": force_write_tags,
            "write_tags": write_tags,
            **kwargs,
        }
    iid = payload.get("item_id")
    tx = st.create(
        operation_type="Metadata Update",
        status="Preview",
        summary=f"Metadata update for item {iid}",
        metadata={**payload, "mutation_family": ITEM_METADATA_FAMILY},
    )
    return {
        "ok": True,
        "operation_id": tx["id"],
        "token": tx["id"],
        "status": "Preview",
        "item_fields_changed": len(payload.get("updates") or {}),
        **payload,
    }


def apply_item_metadata(
    operation_id: Optional[str] = None,
    *,
    plan_token: Optional[str] = None,
    force_write_tags: bool = False,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    op_id = operation_id or plan_token
    if not op_id:
        return {"ok": False, "error": "operation_id or plan_token is required"}
    ad = adapter or beets_adapter
    st = _get_store(store)
    tx = st.get(op_id)
    meta = tx.get("metadata", {})
    iid = meta.get("item_id")
    updates = meta.get("updates") or {}
    write = meta.get("write_tags", True)
    # Intended: a forced tag rewrite also re-places the file for path-format
    # fields, exactly as BeetsAdapter.update_item_metadata does.
    move = meta.get("force_write_tags", force_write_tags)

    def _write() -> None:
        if iid and updates:
            ad.modify(fields=updates, item_ids=[int(iid)], write=write, move=move)

    return _apply_claimed(st, op_id, _write)


def rollback_item_metadata(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    return _rollback_noop(operation_id, store)


def update_item_metadata(
    item_id: int,
    updates: Dict[str, Any],
    *,
    force_write_tags: bool = False,
    write_tags: bool = True,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    payload = {
        "item_id": int(item_id),
        "updates": updates,
        "force_write_tags": force_write_tags,
        "write_tags": write_tags,
        **kwargs,
    }
    plan_res = plan_item_metadata(payload, store=store)
    if not plan_res.get("ok"):
        return {"ok": False, "error": plan_res.get("error") or "Item metadata plan rejected", "code": plan_res.get("code")}
    op_id = plan_res.get("operation_id")
    if not op_id:
        return {"ok": True, "item_fields_changed": 0}
    apply_res = apply_item_metadata(op_id, force_write_tags=force_write_tags, adapter=adapter, store=store)
    if not apply_res.get("ok"):
        return {"ok": False, "error": apply_res.get("error") or "Item metadata apply failed", "code": apply_res.get("code")}
    return {"ok": True, "item_fields_changed": plan_res.get("item_fields_changed", 0)}


# -----------------------------------------------------------------------------
# 9. Album Maintenance & Relocation
# -----------------------------------------------------------------------------


#: Modes apply_album_maintenance actually implements. Everything else is
#: refused (LT-3): removing tracks goes through the track quarantine family,
#: duplicates through backend.duplicate_cleanup, renames through relocation.
_ALBUM_MAINTENANCE_SUPPORTED_MODES = frozenset({"remove_album"})


def plan_album_maintenance(
    payload: Dict[str, Any],
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    st = _get_store(store)
    tx = st.create(
        operation_type="Repair",
        status="Preview",
        summary="Album maintenance",
        metadata=payload,
    )
    return {"ok": True, "operation_id": tx["id"], "token": tx["id"], "status": "Preview", **payload}


def apply_album_maintenance(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Apply an album-maintenance plan.

    Only ``remove_album`` of an album row that is empty right now is
    implemented (row only, never files). Every other mode used to fall
    through to an unrequested ``move`` while reporting Completed; it is now
    refused with ``not_supported`` and the transaction is marked Failed."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    tx = st.get(operation_id)
    meta = tx.get("metadata", {})
    mode = _s(meta.get("mode"))
    aid = int(meta.get("album_id") or 0)
    if tx.get("status") in ("Completed", "Failed", "Rolled Back", "Running"):
        return {"ok": False, "code": "already_applied", "operation_id": operation_id,
                "error": f"Transaction is {tx.get('status')}; it cannot be applied again."}

    def write() -> Dict[str, Any]:
        if mode not in _ALBUM_MAINTENANCE_SUPPORTED_MODES or not aid:
            msg = f"Album maintenance mode {mode or '(none)'!r} is not supported; nothing was changed."
            return {"ok": False, "code": "not_supported", "mutated": False, "error": msg, "log": msg}
        if not ad.get_album(aid):
            return {"deleted_albums": 0, "log": f"Album {aid} is already gone."}
        if ad.find_all_items_by_album_id(aid):
            return {"ok": False, "code": "album_not_empty", "mutated": False,
                    "log": f"Album {aid} still has items; nothing was removed.",
                    "error": f"Album {aid} still has items; only an empty album row can be removed here."}
        ad.remove(album_ids=[aid], delete_files=False)
        return {"deleted_albums": 1, "log": f"Removed empty album row {aid}."}

    return _apply_claimed(st, operation_id, write)


def rollback_album_maintenance(
    operation_id: str,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    return _rollback_noop(operation_id, store)


#: Engine refusals that changed nothing, with the text shown to the operator.
_RELOCATION_REFUSALS = {
    "STALE_PLAN": "The album's tracks changed since the move was planned; nothing was moved.",
    "ALBUM_NOT_FOUND": "Beets no longer has this album; nothing was changed.",
    "ALBUM_CHANGED": "The album's tracks changed since it was moved, so it cannot be moved back; nothing was changed.",
    "ITEM_MOVED": "A track was moved again since; nothing was changed.",
    "ART_CHANGED": "The album's cover changed since it was moved; nothing was changed.",
    "FILE_MISSING": "A moved track's or the cover's file is no longer where Beets put it; nothing was changed.",
    "TARGET_EXISTS": "Something is already at a track's or the cover's old location; nothing was overwritten or changed.",
    "PATH_OUTSIDE_ROOTS": "An old location is outside the folders Beets may write to; nothing was changed.",
    "SYMLINK_REJECTED": "An old location runs through a symlink; nothing was changed.",
    "EXTENSION_CHANGED": "A track's file type changed since it was moved; nothing was changed.",
    "PATH_INVALID": "The recorded old locations are not usable; nothing was changed.",
    "UNDONE": "Beets could not finish and put every file back; nothing was changed.",
    "BEETS_NOT_FOUND": "Restart the beets container so it loads webmanager plugin 1.15.0; nothing was changed.",
    "INVALID_REQUEST": "Beets refused the request as malformed; nothing was changed.",
    "SOURCE_CHANGED": "A torrent original is gone or changed since the album was linked into the library "
                      "(a tag write through a hard link changes it too); nothing was changed.",
    "LIBRARY_FILE_CHANGED": "A library file made from a torrent original changed since; nothing was changed.",
}


#: Rollback refusals (HTTP 409): the album changed since it was moved.
RELOCATION_ROLLBACK_REFUSALS = ("album_not_found", "album_changed", "item_moved", "art_changed", "file_missing",
                                "target_exists", "source_changed", "library_file_changed")


def _album_item_paths(album: Dict[str, Any], ad: BeetsAdapter, aid: int) -> Dict[str, str]:
    items = album.get("items") if isinstance(album.get("items"), list) else ad.get_items(f"album_id:{aid}")
    return {str(it.get("id")): _s(it.get("path")) for it in items or []}


def _preserved_torrent_file(path: str) -> bool:
    """The shared torrent-source rule for one file (#321); fails closed."""
    try:
        # Lazy: library_service sits above this module and imports it.
        from backend.library_service import _preserve_torrent_source_file
        return bool(_preserve_torrent_source_file(path))
    except Exception:
        log.warning("Could not evaluate the torrent-source rule for %s; keeping the original", path)
        return True


def plan_album_relocation(
    payload: Optional[Dict[str, Any]] = None,
    *,
    album_id: Optional[int] = None,
    mode: str = "rename",
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Preview moving an album to where Beets' path templates put it
    ("rename" or "move" to library): records every track's current path,
    the album folders and the cover path, so the apply can refuse a stale
    plan and the rollback can move everything back through Beets."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    payload = dict(payload or {"album_id": album_id, "mode": mode})
    aid = int(payload.get("album_id") or 0)
    mode = "move" if payload.get("mode") == "move" else "rename"
    album = ad.get_album(aid, expand=True) if aid else None
    if not album:
        return {"ok": False, "code": "not_found", "error": "Album not found"}
    paths = _album_item_paths(album, ad, aid)
    if not paths or not all(paths.values()):
        return {"ok": False, "code": "no_paths",
                "error": "Beets reported no file paths for this album (no tracks, or web.include_paths is off)."}
    before = {"items": paths, "artpath": _s(album.get("artpath")),
              "folders": sorted({os.path.dirname(p) for p in paths.values()})}
    # A preserved torrent source (a seeding folder) is hard linked or copied, never moved (#332).
    before["operations"] = {k: "link" if _preserved_torrent_file(p) else "move" for k, p in paths.items()}
    before["art_operation"] = "link" if before["artpath"] and _preserved_torrent_file(before["artpath"]) else "move"
    verb = "Move to library" if mode == "move" else "Rename"
    tx = st.create(
        operation_type="Move" if mode == "move" else "Rename",
        status="Preview",
        summary=f"{verb}: album {aid} ({_s(album.get('albumartist'))} - {_s(album.get('album'))})",
        changes=[{"type": "album_relocation", "album_id": aid, "mode": mode, "old_paths": paths,
                  "old_artpath": before["artpath"], "old_folders": before["folders"],
                  "operations": before["operations"], "art_operation": before["art_operation"]}],
        rollback_available=True,
        metadata={"mutation_family": ALBUM_RELOCATION_FAMILY, "album_id": aid, "mode": mode, "before": before},
    )
    return {"ok": True, "operation_id": tx["id"], "token": tx["id"], "status": "Preview",
            "album_id": aid, "mode": mode, "before": before}


def apply_album_relocation(
    operation_id: Optional[str] = None,
    *,
    plan_token: Optional[str] = None,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Apply an Approved relocation through Beets' Album.move(). The engine
    request is recorded first, so a restart mid-call is resolved from engine
    evidence and the recorded paths (backend/transaction_recovery.py), never
    replayed."""
    op_id = operation_id or plan_token or ""
    ad = adapter or beets_adapter
    st = _get_store(store)
    try:
        tx = st.get(op_id)
    except KeyError:
        return {"ok": False, "code": "not_found", "error": "Transaction not found"}
    meta = tx.get("metadata") or {}
    if meta.get("mutation_family") != ALBUM_RELOCATION_FAMILY:
        return {"ok": False, "code": "wrong_family", "error": "Not an album relocation transaction."}
    if meta.get("engine_result"):
        return {"ok": False, "code": "already_applied", "error": "This relocation was already applied."}
    if tx.get("status") != "Approved":
        return {"ok": False, "code": "not_approved", "error": "Approve the transaction before applying it."}
    aid = int(meta["album_id"])
    links = {k: v for k, v in (meta["before"].get("operations") or {}).items() if v == "link"}
    art_op = meta["before"].get("art_operation") or "move"
    if links or art_op == "link":
        # An older plugin ignores "operations" and would MOVE a seeding torrent.
        try:
            capable = "album_relocation_link" in (ad.get_plugin_status().get("capabilities") or [])
        except BeetsAdapterError:
            capable = False
        if not capable:
            st.update(op_id, status="Failed", logs=[
                "Not moved: the album is in a preserved torrent source and Beets has no webmanager plugin "
                "1.15.0 (capability album_relocation_link) to link it; nothing was changed."])
            return {"ok": False, "code": "plugin_outdated", "mutated": False, "operation_id": op_id,
                    "status": "Failed",
                    "error": "Restart the beets container so it loads webmanager plugin 1.15.0, which links "
                             "a seeding torrent's files instead of moving them; nothing was changed."}
    from backend.resource_locks import attempt_owner, claim_approved, claim_refusal, locks as resource_locks
    with resource_locks().hold([f"album:{aid}"], attempt_owner(op_id), timeout=10):
        if claim_approved(st, op_id) is None:
            return {"ok": False, "code": "not_approved", "error": claim_refusal(st, op_id)}
        st.update(op_id, status="Running", metadata={"engine_request": {"operation_id": op_id}})
        try:
            res = ad.relocate_album(aid, meta["before"]["items"], idempotency_key=op_id,
                                    **({"operations": links, "art_operation": art_op}
                                       if links or art_op == "link" else {}))
        except BeetsAdapterError as exc:
            if _transport_error(exc):
                st.update(op_id, status="Recovery Required",
                          logs=["Beets did not answer; whether the album moved is unknown."])
                raise
            code = exc.error_code or ""
            message = _RELOCATION_REFUSALS.get(code)
            # Only a known refusal (or a move Beets undid) proves nothing changed.
            status = "Failed" if message else "Recovery Required"
            st.update(op_id, status=status,
                      logs=[f"Beets refused the album move ({code or 'error'}); "
                            + ("nothing was changed." if message else "check the album's files.")])
            return {"ok": False, "code": code.lower() or "beets_error", "mutated": False if message else None,
                    "operation_id": op_id, "status": status,
                    "error": message or "Beets could not move the album; check its files."}
        return finish_album_relocation(op_id, res, adapter=ad, store=st)


def _paths_match(live: Dict[str, str], want: Dict[str, str]) -> bool:
    return set(live) == set(want) and all(
        live[k] == want[k] or _same_library_path(live[k], want[k]) for k in want)


def _same_art(a: Any, b: Any) -> bool:
    return _s(a) == _s(b) or _same_library_path(a, b)


def _live_relocation_state(ad: BeetsAdapter, aid: int) -> Optional[Dict[str, Any]]:
    album = ad.get_album(aid, expand=True)
    if not album:
        return None
    return {"items": _album_item_paths(album, ad, aid), "artpath": _s(album.get("artpath"))}


def finish_album_relocation(
    operation_id: str,
    res: Dict[str, Any],
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Check the engine's before/after against the plan and live Beets and
    record it (also used by restart recovery -- never re-applies)."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    meta = st.get(operation_id).get("metadata") or {}
    aid = int(meta["album_id"])
    engine = res.get("result") if isinstance(res.get("result"), dict) else res
    engine_before = (engine.get("before") or {}).get("items") or {}
    after = engine.get("after") or {}
    after_items = after.get("items") or {}
    live = _live_relocation_state(ad, aid)
    problems = []
    if live is None or not _paths_match(live["items"], after_items):
        problems.append("item paths")
    if live is not None and not _same_art(live["artpath"], after.get("artpath")):
        problems.append("artpath")
    if not _paths_match(meta["before"]["items"], engine_before):
        problems.append("planned paths")
    moved = sum(1 for k, p in after_items.items() if p != engine_before.get(k))
    methods = engine.get("methods") or {}
    planned = meta["before"].get("operations") or {}
    if any(planned.get(k) == "link" and p != engine_before.get(k) and methods.get(k) not in ("linked", "copied")
           for k, p in after_items.items()):
        problems.append("torrent source moved")  # never expected: the plugin links or copies these
    kept = {m: sum(1 for v in methods.values() if v == m) for m in ("linked", "copied")}
    # Item.move() skips a track whose file is missing: a partial move. Failed
    # (with the engine result) stays rollbackable; the rollback moves back
    # only the tracks that moved.
    skipped = [str(k) for k in engine.get("skipped") or []]
    folders = sorted({os.path.dirname(p) for p in after_items.values()})
    status = "Recovery Required" if problems else "Failed" if skipped else "Completed"
    art_before = _s((engine.get("before") or {}).get("artpath"))
    st.update(
        operation_id, status=status,
        metadata={"engine_result": engine, "after": {**after, "folders": folders}, "verification_problems": problems},
        logs=[f"Beets moved {moved} of {len(after_items)} tracks of album {aid} to "
              f"{', '.join(folders) or '(nowhere)'}; cover: {_s(after.get('artpath')) or '(none)'}"]
             + ([f"Preserved torrent source left in place: {kept['linked']} track(s) hard linked and "
                 f"{kept['copied']} copied into the library"
                 + (f"; cover {engine['art_method']}" if engine.get("art_method") in ("linked", "copied") else "")]
                if kept["linked"] or kept["copied"] or engine.get("art_method") in ("linked", "copied") else [])
             + ([f"Partial move: Beets skipped tracks {', '.join(skipped)} (file missing); "
                 "roll back to move the others back"] if skipped else [])
             + ([f"Beets cleared the cover path {art_before} (file missing); rollback restores it"]
                if art_before and not _s(after.get("artpath")) else [])
             + ([f"Warning: Beets renamed tracks {', '.join(engine['renamed'])} (name.1.ext) because "
                 "their template path was taken"] if engine.get("renamed") else [])
             + ([f"Verification mismatch: {', '.join(problems)}"] if problems else []),
    )
    out = {"ok": not problems and not skipped, "operation_id": operation_id, "status": status, "album_id": aid,
           "dest_dir": folders[0] if folders else "", "moved_count": moved, "skipped": skipped,
           "renamed": list(engine.get("renamed") or []), "linked_count": kept["linked"],
           "copied_count": kept["copied"], "verification_problems": problems}
    if skipped:
        out.update(code="partial_move", error=f"Beets skipped {len(skipped)} track(s) whose file is missing; "
                                              "the others moved. Roll back from Transactions to undo.")
    return out


def album_relocation_unchanged(ad: BeetsAdapter, meta: Dict[str, Any]) -> bool:
    """True when live Beets still has the album exactly as planned AND every
    planned file is still there (restart recovery: proves an unconfirmed apply
    changed nothing). Rows alone are not proof: a crash between Beets' file
    move and its row store leaves the row at the old path with the file gone.
    Web Manager stats the paths through its own read-only mounts; a path it
    cannot see counts as changed (Recovery Required), never as unchanged."""
    live = _live_relocation_state(ad, int(meta["album_id"]))
    before = meta.get("before") or {}
    files = [*(before.get("items") or {}).values(), *([before["artpath"]] if before.get("artpath") else [])]
    return (live is not None and _paths_match(live["items"], before.get("items") or {})
            and _same_art(live["artpath"], before.get("artpath"))
            and all(os.path.isfile(p) for p in files))


def rollback_album_relocation(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Move every track and the cover back to where they were, through
    Beets. Beets refuses -- and changes nothing -- when the album changed
    since (moved again, tracks added or removed, cover changed, album gone)
    or an old location is occupied; it never overwrites."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    try:
        tx = st.get(operation_id)
    except KeyError:
        return {"ok": False, "code": "not_found", "error": "Transaction not found"}
    meta = tx.get("metadata") or {}
    engine = meta.get("engine_result") or {}
    if meta.get("mutation_family") != ALBUM_RELOCATION_FAMILY or not engine.get("after"):
        return {"ok": False, "code": "not_applied", "error": "No applied album move to roll back."}
    if tx.get("status") == "Rolled Back":
        return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}
    refusal = engine_rollback_refusal(tx)
    if refusal:
        return refusal
    aid = int(meta["album_id"])
    before, after = engine.get("before") or {}, engine["after"]
    evidence = engine.get("evidence") or {}
    methods = engine.get("methods") or {}
    items = [{"id": int(k), "path": p, "restore_path": (before.get("items") or {}).get(k),
              "evidence": (evidence.get("items") or {}).get(k),
              **({"method": methods[k], "library_evidence": (evidence.get("library") or {}).get(k)}
                 if methods.get(k) in ("linked", "copied") else {})}
             for k, p in (after.get("items") or {}).items()]
    art_kept = engine.get("art_method") if engine.get("art_method") in ("linked", "copied") else ""
    key = f"{operation_id}:rollback"
    # An earlier attempt whose outcome was never recorded (Beets or Web Manager
    # died mid-rollback) may have moved some tracks back.
    interrupted = bool(meta.get("rollback_request")) and not meta["rollback_request"].get("outcome")
    from backend.resource_locks import attempt_owner, locks as resource_locks
    with resource_locks().hold([f"album:{aid}"], attempt_owner(operation_id), timeout=10):
        # Recorded before the call: a retry sends the same key and paths; Beets
        # treats tracks already back as done and adopts a file a crash left at
        # its old path when size and mtime prove it, so it resumes, never replays blindly.
        request_meta = {"idempotency_key": key, "items": items, "outcome": None}
        st.update(operation_id, metadata={"rollback_request": request_meta})
        try:
            res = ad.rollback_album_relocation(aid, items, _s(after.get("artpath")), _s(before.get("artpath")),
                                               idempotency_key=key, art_evidence=evidence.get("artpath"),
                                               **({"art_method": art_kept,
                                                   "art_library_evidence": evidence.get("library_art")}
                                                  if art_kept else {}))
        except BeetsAdapterError as exc:
            if _transport_error(exc):
                st.transition(operation_id, tx.get("status"), "Recovery Required", logs=[
                    "Beets did not answer the rollback; some tracks may already be back. Retry the rollback: "
                    "Beets resumes from the recorded paths and refuses what it cannot prove."])
                raise
            code = exc.error_code or ""
            message = _RELOCATION_REFUSALS.get(code)
            if message and interrupted:
                st.transition(operation_id, tx.get("status"), "Recovery Required",
                              metadata={"rollback_request": {**request_meta, "outcome": "refused"}},
                              logs=[f"Rollback refused by Beets ({code}) after an earlier rollback attempt was "
                                    "interrupted; the album may be partly moved back. Check its files."])
                return {"ok": False, "code": code.lower(), "mutated": None, "operation_id": operation_id,
                        "status": "Recovery Required",
                        "error": "An earlier rollback was interrupted and this one was refused "
                                 f"({code}); the album may be partly moved back. Check its files."}
            if message:
                st.update(operation_id, metadata={"rollback_request": {**request_meta, "outcome": "refused"}},
                          logs=[f"Rollback refused by Beets ({code}); nothing was changed."])
                return {"ok": False, "code": code.lower(), "mutated": False, "operation_id": operation_id,
                        "status": tx.get("status"), "error": message}
            st.transition(operation_id, tx.get("status"), "Recovery Required",
                          logs=[f"Rollback failed in Beets ({code or 'error'}); check the album's files."])
            return {"ok": False, "code": code.lower() or "beets_error", "mutated": None,
                    "operation_id": operation_id, "status": "Recovery Required",
                    "error": "Beets could not move the album back; check its files."}
        result = res.get("result") if isinstance(res.get("result"), dict) else res
        live = _live_relocation_state(ad, aid)
        ok = (live is not None and _paths_match(live["items"], before.get("items") or {})
              and _same_art(live["artpath"], before.get("artpath")))
        status = "Rolled Back" if ok else "Recovery Required"
        if st.transition(
            operation_id, tx.get("status"), status,
            metadata={"rollback_result": result, "rollback_request": {**request_meta, "outcome": "succeeded"}},
            logs=[f"Moved {result.get('restored_items', 0) - (result.get('adopted_items') or 0) - (result.get('repointed_items') or 0)} "
                  f"tracks of album {aid} back to "
                  f"{', '.join(meta['before'].get('folders') or []) or '(unknown)'}"
                  + ("; cover restored" if result.get("restored_art") else "")]
                 + ([f"{result['adopted_items']} track(s) were already back after an interrupted rollback "
                     "(size and mtime matched); their rows were updated without moving anything"]
                    if result.get("adopted_items") else [])
                 + ([f"{result['repointed_items']} track(s) point at their untouched torrent originals again; "
                     f"removed {result.get('removed_library_files') or 0} library file(s) this move had linked "
                     "or copied"] if result.get("repointed_items") or result.get("removed_library_files") else [])
                 + ([f"Kept library file(s) that could not be proven to be this move's: "
                     f"{', '.join(result['kept_library_files'])}"] if result.get("kept_library_files") else [])
                 + ([] if ok else ["Verification mismatch: paths after rollback"]),
        ) is None:
            return _rollback_conflict(operation_id)
    return {"ok": ok, "operation_id": operation_id, "status": status,
            "restored": int(result.get("restored_items") or 0)}


def relocate_album(
    album_id: int,
    mode: str = "rename",
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Rename / move one album as one audited, reversible transaction: plan,
    record the operator's request as the approval, apply through Beets."""
    st = _get_store(store)
    plan = plan_album_relocation(album_id=int(album_id), mode=mode, adapter=adapter, store=st)
    if not plan.get("ok"):
        return {"ok": False, "error": plan.get("error") or "Relocation plan rejected", "code": plan.get("code")}
    op_id = plan["operation_id"]
    from backend.resource_locks import approve_preview
    if approve_preview(st, op_id, f"operator album {plan.get('mode') or mode}") is None:
        return {"ok": False, "code": "not_preview", "operation_id": op_id,
                "error": "The album move was cancelled before it ran."}
    res = apply_album_relocation(op_id, adapter=adapter, store=st)
    if not res.get("ok"):
        return {"ok": False, "operation_id": op_id, "code": res.get("code"), "status": res.get("status"),
                "error": res.get("error") or "Relocation apply failed"}
    return {"ok": True, "operation_id": op_id, "dest_dir": res.get("dest_dir") or "",
            "moved_count": res.get("moved_count", 0)}


def move_library(*_args: Any, adapter: Optional[BeetsAdapter] = None, **_kwargs: Any) -> Dict[str, Any]:
    """Whole-library move (LT-18): refused.

    It used to move every album in one unaudited call with no plan or
    rollback, and its only caller passed arguments it did not accept. A
    library-wide move needs a planned, per-album relocation family first."""
    return {"ok": False, "code": "not_supported",
            "error": "Moving the whole library at once is not supported; nothing was moved. "
                     "Relocate albums individually."}


# -----------------------------------------------------------------------------
# 10. Genre Repair
# -----------------------------------------------------------------------------


def plan_album_genre_repair(
    payload: Optional[Dict[str, Any]] = None,
    *,
    album_id: Optional[int] = None,
    force: bool = False,
    timeout: float = 180.0,
    store: Optional[TransactionStore] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    st = _get_store(store)
    if payload is None:
        payload = {"album_id": album_id, "force": force, "timeout": timeout, **kwargs}
    aid = payload.get("album_id")
    tx = st.create(
        operation_type="Repair",
        status="Preview",
        summary=f"Genre repair for album {aid}",
        metadata={**payload, "mutation_family": GENRE_REPAIR_FAMILY},
    )
    return {"ok": True, "operation_id": tx["id"], "token": tx["id"], "status": "Preview", **payload}


def apply_album_genre_repair(
    operation_id: Optional[str] = None,
    *,
    plan_token: Optional[str] = None,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    op_id = operation_id or plan_token
    if not op_id:
        return {"ok": False, "error": "operation_id or plan_token is required"}
    ad = adapter or beets_adapter
    st = _get_store(store)
    tx = st.get(op_id)
    aid = tx.get("metadata", {}).get("album_id")

    def write() -> None:
        if aid:
            ad.lastgenre(album_ids=[int(aid)], force=True)

    return _apply_claimed(st, op_id, write)


def rollback_album_genre_repair(
    operation_id: str,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    return _rollback_noop(operation_id, store)


def repair_album_genre(
    album_id: int,
    force: bool = False,
    timeout: float = 180.0,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    payload = {"album_id": int(album_id), "force": bool(force), "timeout": timeout, **kwargs}
    plan_res = plan_album_genre_repair(payload, store=store)
    if not plan_res.get("ok"):
        return {"ok": False, "error": plan_res.get("error") or "Genre repair plan rejected", "code": plan_res.get("code")}
    op_id = plan_res.get("operation_id")
    if not op_id:
        return {"ok": True, "output": plan_res.get("summary") or ""}
    apply_res = apply_album_genre_repair(op_id, adapter=adapter, store=store)
    if not apply_res.get("ok"):
        return {"ok": False, "error": apply_res.get("error") or "Genre repair apply failed", "code": apply_res.get("code")}
    return {"ok": True, "output": apply_res.get("output") or "", "genre_after": apply_res.get("genre_after")}


# -----------------------------------------------------------------------------
# 11. Import Review Cleanup & Confirmed Import
# -----------------------------------------------------------------------------


IMPORT_REVIEW_CLEANUP_FAMILY = "import_review_cleanup_v1"


def _import_review_quarantine_root() -> str:
    return os.environ.get("IMPORT_REVIEW_QUARANTINE_DIR") or str(_data_dir() / "import_review_quarantine")


def _import_review_allowed_roots(allow_music: bool) -> List[str]:
    from backend.serializers import _import_review_cleanup_roots
    roots: List[str] = []
    # The configured downloads root (DOWNLOADS_ROOT) stays
    # cleanable, exactly as the pre-engine apply allowed.
    for root in list(_import_review_cleanup_roots(allow_music=allow_music)) + _get_staging_roots():
        try:
            resolved = Path(root).resolve()
        except Exception:
            continue
        if _is_protected_data_path(resolved) or str(resolved) in roots:
            continue
        roots.append(str(resolved))
    return roots


def plan_import_review_cleanup(
    payload_or_folder: Any = None,
    store: Optional[TransactionStore] = None,
    **kwargs,
) -> Dict[str, Any]:
    """Preview an Import Review folder/file cleanup through the canonical
    engine planner (``import_review_cleanup_v1``): root containment, per-file
    stat preconditions, server-derived quarantine. Nothing is touched here;
    the plan must be Approved before :func:`apply_import_review_cleanup`."""
    from backend.transaction_engine import _normpath_within_roots, execute_import_review_cleanup_plan
    st = _get_store(store)
    if isinstance(payload_or_folder, dict):
        data = dict(payload_or_folder)
    elif payload_or_folder:
        data = {"path": str(payload_or_folder), **kwargs}
    else:
        data = dict(kwargs)
    if not data.get("path"):
        data["path"] = data.get("folder") or data.get("folder_path") or data.get("source") or ""
    if not data.get("path"):
        return {"ok": False, "error": "A folder or file path is required."}
    # F-3: album_id alone never opens the library (the caller verifies it).
    allow_music = bool(data.get("confirmed_wrong_library_folder") or data.get("allow_library_delete"))
    library_quarantined = False
    try:
        # realpath (symlinks collapsed, like resolve()) + normpath prefix
        # containment: classification only, never a filesystem operation.
        in_library = _normpath_within_roots(os.path.realpath(str(data["path"])), [_music_root()])
    except Exception:
        in_library = False
    if in_library and str(data.get("action") or "").strip().lower() not in (
            "quarantine_rejected", "quarantine_duplicate"):
        # S1: files inside the music library are never hard-deleted by Import
        # Review cleanup -- they are moved to the server-derived quarantine
        # (RECOVERABLE, rollback available) even when a delete was requested.
        data["action"] = "quarantine_rejected"
        library_quarantined = True
    try:
        res = execute_import_review_cleanup_plan(
            st, data, _import_review_allowed_roots(allow_music), music_root=str(_music_root()))
    except ValueError as exc:
        log.warning("Import Review cleanup preview rejected: %s", exc)
        return {"ok": False, "code": "invalid_request", "error": "Invalid cleanup request."}
    if res.get("ok") and res.get("operation_id"):
        res.setdefault("token", res["operation_id"])
        res["library_paths_quarantined"] = library_quarantined
    return res


def _approve_preview(st: TransactionStore, operation_id: str, approved_by: str) -> bool:
    """CAS Preview -> Approved for callers whose own request already is the
    operator's explicit confirmation. False if anyone else moved it first."""
    return st.transition(operation_id, "Preview", "Approved", metadata={"approved_by": approved_by}) is not None


def apply_import_review_cleanup(
    operation_id: str,
    store: Optional[TransactionStore] = None,
    approved_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply an Approved Import Review cleanup exactly once.

    ``approved_by`` is for callers whose own request already is the
    operator's explicit confirmation: a Preview plan is CAS-approved in the
    same store first (refused if anyone else moved it).

    Requires status Approved, claims it (CAS Approved -> Running) under the
    ``workflow:import-review-cleanup`` lock, then runs the canonical engine
    apply. A delete or move that fails is reported as a failure -- never as
    Completed -- with the per-file details."""
    st = _get_store(store)
    try:
        tx = st.get(operation_id)
    except KeyError:
        return {"ok": False, "code": "not_found", "error": "Transaction not found"}
    meta = tx.get("metadata") or {}
    status = tx.get("status")
    family = meta.get("mutation_family")
    if family and family != IMPORT_REVIEW_CLEANUP_FAMILY:
        # Checked before the Preview -> Approved CAS (#182): never approve
        # another family's plan as a side effect of this call.
        return {"ok": False, "code": "wrong_family", "operation_id": operation_id,
                "error": "Not an Import Review cleanup transaction."}
    if approved_by and status == "Preview":
        if not _approve_preview(st, operation_id, approved_by):
            return {"ok": False, "code": "not_approved", "operation_id": operation_id,
                    "error": "Cleanup preview changed state before it could be approved."}
        status = "Approved"
    if status in ("Completed", "Running", "Failed", "Partially Rolled Back", "Rolled Back") or meta.get("engine_result"):
        return {"ok": False, "code": "already_applied", "operation_id": operation_id, "status": status,
                "error": f"This cleanup was already applied (status {status})."}
    if status != "Approved":
        return {"ok": False, "code": "not_approved", "operation_id": operation_id, "status": status,
                "error": "Approve the cleanup preview before applying it."}
    from backend.resource_locks import attempt_owner, claim_approved, claim_refusal, locks as resource_locks
    with resource_locks().hold(["workflow:import-review-cleanup"], attempt_owner(operation_id), timeout=10):
        if claim_approved(st, operation_id) is None:
            return {"ok": False, "code": "not_approved", "operation_id": operation_id,
                    "error": claim_refusal(st, operation_id)}
        try:
            if meta.get("mutation_family") == IMPORT_REVIEW_CLEANUP_FAMILY:
                return _apply_engine_import_review_cleanup(st, operation_id)
            return _apply_legacy_folder_cleanup(st, operation_id, meta)
        except Exception as exc:
            log.exception("Import Review cleanup apply failed; the transaction is marked Failed")
            reason = f"Cleanup failed ({type(exc).__name__}); see server logs."
            st.update(operation_id, status="Failed", metadata={"engine_result": {"ok": False, "error": reason}},
                      logs=[reason])
            return {"ok": False, "code": "failed", "operation_id": operation_id, "status": "Failed",
                    "error": reason, "deleted": [], "moved": [], "skipped": []}


def _apply_engine_import_review_cleanup(st: TransactionStore, operation_id: str) -> Dict[str, Any]:
    from backend.transaction_engine import execute_import_review_cleanup_apply
    res = execute_import_review_cleanup_apply(st, operation_id, quarantine_root=_import_review_quarantine_root())
    skipped = list(res.get("skipped") or [])
    failures = [e for e in skipped if isinstance(e, dict) and str(e.get("reason") or "").startswith("failed_")]
    current = st.get(operation_id).get("status")
    if res.get("ok") and not failures and current == "Completed":
        st.update(operation_id, metadata={"engine_result": {"ok": True, "status": "Completed"}})
        return res
    deleted = list(res.get("deleted") or [])
    moved = list(res.get("moved") or [])
    partial = bool(deleted or moved)
    final = "Partially Rolled Back" if partial else "Failed"
    detail = res.get("error") or ("; ".join(f"{e.get('file')}: {e.get('reason')}" for e in failures)
                                  or f"engine finished with status {current}")
    st.update(operation_id, status=final,
              metadata={"engine_result": {"ok": False, "status": final, "failures": failures, "error": detail}},
              logs=[f"Cleanup did not complete: {detail}"])
    return {**res, "ok": False, "code": "partial" if partial else "failed", "status": final,
            "operation_id": operation_id, "error": f"Cleanup did not complete: {detail}",
            "failures": failures, "deleted": deleted, "moved": moved, "skipped": skipped}


def _apply_legacy_folder_cleanup(st: TransactionStore, operation_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    """Pre-engine transactions that only carry ``folder``: a single staging
    folder delete through the validated, lstat-rechecked helper."""
    folder = meta.get("folder") or meta.get("folder_path") or meta.get("source") or meta.get("path")
    if not folder:
        st.update(operation_id, status="Failed", metadata={"engine_result": {"ok": False, "error": "no folder"}},
                  logs=["Cleanup plan names no folder; nothing was deleted."])
        return {"ok": False, "code": "failed", "operation_id": operation_id, "status": "Failed",
                "error": "Cleanup plan names no folder.", "deleted": [], "moved": [], "skipped": []}
    p = Path(folder)
    if not p.exists() and not p.is_symlink():
        st.update(operation_id, status="Completed", metadata={"engine_result": {"ok": True, "deleted": []}},
                  logs=[f"{folder} was already gone; nothing to delete."])
        return {"ok": True, "operation_id": operation_id, "status": "Completed", "deleted": [], "moved": [],
                "skipped": [{"file": str(folder), "reason": "not_found"}]}
    resolved = _validated_staging_target(p, "delete")
    _remove_resolved(resolved)
    if os.path.lexists(str(resolved)):
        st.update(operation_id, status="Failed",
                  metadata={"engine_result": {"ok": False, "error": "folder still exists after delete"}},
                  logs=[f"{resolved} still exists after the delete; reported as failed."])
        return {"ok": False, "code": "failed", "operation_id": operation_id, "status": "Failed",
                "error": f"{resolved} still exists after the delete.", "deleted": [], "moved": [], "skipped": []}
    st.update(operation_id, status="Completed", metadata={"engine_result": {"ok": True, "deleted": [str(resolved)]}},
              logs=[f"Deleted {resolved}"])
    return {"ok": True, "operation_id": operation_id, "status": "Completed", "deleted": [str(resolved)],
            "moved": [], "skipped": []}


def rollback_import_review_cleanup(
    operation_id: str,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Restore quarantined files of an engine cleanup. Deletes are not
    reversible and are reported as ``not_supported`` -- never as Rolled Back."""
    st = _get_store(store)
    try:
        tx = st.get(operation_id)
    except KeyError:
        return {"ok": False, "code": "not_found", "error": "Transaction not found"}
    meta = tx.get("metadata") or {}
    if meta.get("mutation_family") != IMPORT_REVIEW_CLEANUP_FAMILY or not meta.get("rollback_available"):
        return {"ok": False, "code": "not_supported", "operation_id": operation_id,
                "error": "This cleanup deleted files (or was never applied); there is nothing to restore."}
    if tx.get("status") == "Rolled Back":
        return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}
    from backend.transaction_engine import rollback_import_review_cleanup as engine_rollback
    expected = [s for s in (meta.get("steps") or []) if s.get("type") == "move_quarantine"
                and s.get("status") == "completed"]
    res = engine_rollback(st, operation_id)
    if not res.get("ok"):
        return {"code": "rollback_failed", **res, "ok": False}
    restored = res.get("restored") or []
    if len(restored) < len(expected):
        st.transition(operation_id, "Rolled Back", "Partially Rolled Back",
                      logs=[f"Only {len(restored)} of {len(expected)} quarantined files were restored."])
        return {**res, "ok": False, "code": "partial", "status": "Partially Rolled Back",
                "error": f"Only {len(restored)} of {len(expected)} quarantined files were restored."}
    return res


#: Error codes the webmanager plugin's POST /webmanager/import answers with,
#: mapped to an operator-facing message. Nothing was imported in any of them.
_IMPORT_REFUSALS = {
    "AUTOTAG_NOT_ALLOWED": (
        "The webmanager Beets plugin refuses imports that use Beets' own autotagger "
        "(AUTOTAG_NOT_ALLOWED). Web Manager imports through Beets' importer and "
        "MusicBrainz lookup, so this plugin version cannot import. Restart the Beets "
        "container so it loads the current webmanager plugin (1.8.0 or later), then retry. "
        "Nothing was imported."),
    "PATH_NOT_ALLOWED": (
        "The import source is not inside one of the Beets plugin's import roots "
        "(webmanager.import_roots, default /downloads). Nothing was imported."),
    "SOURCE_NOT_FOUND": "Beets cannot see the import source folder. Nothing was imported.",
    "INVALID_PATHS": "No import source folder was given. Nothing was imported.",
    "INVALID_DUPLICATE_ACTION": "The import request was malformed. Nothing was imported.",
}


def _import_refused(ex: Exception) -> Dict[str, Any]:
    """A clean ok=false result for a Beets import the plugin rejected or
    could not run. Never carries the raw upstream body."""
    code = _s(getattr(ex, "error_code", "") or "").upper()
    if code in _IMPORT_REFUSALS:
        return {"ok": False, "code": code.lower(), "mutated": False, "error": _IMPORT_REFUSALS[code]}
    if isinstance(ex, (BeetsAdapterConnectionError, BeetsAdapterTimeoutError)):
        return {"ok": False, "code": "beets_unavailable", "mutated": None,
                "error": "Beets did not answer the import request; check the Beets container and the library."}
    return {"ok": False, "code": (code or "import_failed").lower(), "mutated": None,
            "error": "Beets reported that the import failed; see the Beets log for details."}


_QUIET_FALLBACKS = ("skip", "asis")
_CONFIRMED_DUPLICATE_ACTIONS = ("skip", "keep")


def plan_confirmed_import(
    payload: Dict[str, Any],
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Record a confirmed import of one source folder as a chosen MusicBrainz
    Release (``mb_albumid``; ``mb_releasegroupid`` when known). Nothing runs
    until apply_confirmed_import()."""
    st = _get_store(store)
    source = payload.get("source_folder") or payload.get("paths") or payload.get("path")
    tx = st.create(
        operation_type="Import",
        status="Preview",
        summary=f"Confirmed import of {source} as MusicBrainz release {payload.get('mb_albumid') or '?'}",
        metadata=payload,
    )
    return {"ok": True, "operation_id": tx["id"], "token": tx["id"], "status": "Preview", **payload}


def apply_confirmed_import(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    acceptance_failpoint: Optional[str] = None,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Import the planned folder with Beets' own importer, pinned to the
    confirmed Release: autotag on, ``search_ids=[mb_albumid]`` (``beet import
    -q --search-id``), quiet fallback ``skip``. Beets looks the Release up on
    MusicBrainz, applies it and places the files; Web Manager only verifies
    the album row it produced. A source Beets would not confidently match is
    skipped by Beets and reported as ``not_imported`` -- never imported as-is.

    Copy unless the plan says ``use_move: True`` (the caller has already
    applied the preserved-torrent-source rule) or ``in_place: True`` (a
    folder already inside the library: neither copy nor move). ``acceptance_failpoint`` is
    accepted for the old engine's acceptance harness and ignored: the
    webmanager plugin has no failpoints. Every outcome leaves the transaction
    Completed or Failed, never Preview."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    tx = st.get(operation_id)
    meta = tx.get("metadata") or {}
    paths = meta.get("source_folder") or meta.get("paths") or meta.get("path") or []
    release_id = _uuid_or_blank(meta.get("mb_albumid"))
    planned_rg = _s(meta.get("mb_releasegroupid")).strip().lower()
    in_place = meta.get("in_place") is True
    use_move = meta.get("use_move") is True and not in_place
    duplicate_action = _s(meta.get("duplicate_action") or "skip").lower()

    def _fail(expected: str, code: str, error: str, mutated: Any = False,
              album_ids: Optional[List[int]] = None) -> Dict[str, Any]:
        # album_ids: the rows Beets imported and this failure keeps (never removed here).
        extra = {"metadata": {"engine_result": {"mutation_started": True, "kept_album_ids": album_ids}}}             if album_ids else {}
        st.transition(operation_id, expected, "Failed", logs=[f"Apply failed ({code}): {error}"], **extra)
        return {"ok": False, "code": code, "operation_id": operation_id, "status": "Failed",
                "mutated": mutated, "error": error, "album_ids": album_ids or []}

    if not paths or not release_id or (planned_rg and not _uuid_or_blank(planned_rg)) \
            or duplicate_action not in _CONFIRMED_DUPLICATE_ACTIONS:
        status = tx.get("status")
        if status not in ("Preview", "Approved"):
            return _apply_refused(st, operation_id)
        return _fail(status, "invalid_plan",
                     "The import plan needs a source folder and a valid MusicBrainz Release ID; nothing was imported.")
    if _claim_apply(st, operation_id, metadata={"engine_result": {"mutation_started": True}}) is None:
        return _apply_refused(st, operation_id)
    try:
        before = {int(a.get("id") or 0) for a in ad.find_all_albums_by_mb_albumid(release_id)}
        ad.run_import(paths=paths, autotag=True, search_ids=[release_id], quiet_fallback="skip",
                      duplicate_action=duplicate_action, copy=not (use_move or in_place), move=use_move, write=True,
                      timeout=timeout)
        new = [a for a in ad.find_all_albums_by_mb_albumid(release_id) if int(a.get("id") or 0) not in before]
    except Exception as ex:  # any failure ends Failed, never a stuck Running/Preview row
        refused = _import_refused(ex)
        return _fail("Running", refused["code"], refused["error"], refused["mutated"])
    if not new:
        return _fail("Running", "not_imported",
                     "Beets did not import this folder as the selected release (no confident match, "
                     "or the release is already in the library). Nothing was imported; review the folder.")
    if len(new) > 1:
        return _fail("Running", "import_ambiguous",
                     f"Beets created {len(new)} album rows for this release; review them before continuing.",
                     mutated=True, album_ids=[int(a.get("id") or 0) for a in new])
    album = new[0]
    album_id = int(album.get("id") or 0)
    got_rg = _s(album.get("mb_releasegroupid")).strip().lower()
    if planned_rg and got_rg != planned_rg:
        return _fail("Running", "release_group_mismatch",
                     f"Beets imported album {album_id} with a different Release Group than the one confirmed; "
                     "review it before continuing.", mutated=True, album_ids=[album_id])
    item_ids = [int(i.get("id")) for i in ad.find_all_items_by_album_id(album_id) if i.get("id") is not None]
    st.transition(operation_id, "Running", "Completed",
                  metadata={"engine_result": {"album_id": album_id, "item_ids": item_ids,
                                              "mb_releasegroupid": got_rg}})
    return {"ok": True, "operation_id": operation_id, "status": "Completed", "album_id": album_id,
            "item_ids": item_ids, "album_id_verified": True, "mb_releasegroupid": got_rg}


def rollback_import_folder(
    operation_id: str,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    return _rollback_noop(operation_id, store)


def _album_count(ad: BeetsAdapter) -> Optional[int]:
    try:
        return int(ad.get_stats().get("albums"))
    except Exception:
        return None


def reimport_source(
    path: str,
    beets_options: Optional[Dict[str, Any]] = None,
    timeout: float = 300.0,
    adapter: Optional[BeetsAdapter] = None,
) -> Dict[str, Any]:
    """``beet import -q`` on a source folder through the webmanager plugin:
    Beets' own importer and autotagger decide the match and place the files.

    beets_options: ``copy`` (wins over ``move``), ``move`` (default False),
    ``write`` (default True), ``quiet_fallback`` (``skip`` default; ``asis``
    only when the caller asks, since it imports unmatched albums without a
    release group) and ``search_id`` (a Release ID, Beets' ``--search-id``).

    Returns ok=False with a clean error when the plugin refuses or fails.
    On success ``not_matched`` lists what Beets skipped (left in place for
    review): the plugin's ``skipped_paths`` when it reports them, else the
    whole source when no album was added. ``not_matched_known`` is False
    when albums were added but the plugin did not say which folders it
    skipped."""
    ad = adapter or beets_adapter
    opts = beets_options or {}
    move = bool(opts.get("move")) and not opts.get("copy")
    fallback = _s(opts.get("quiet_fallback") or "skip").strip().lower()
    if fallback not in _QUIET_FALLBACKS:
        return {"ok": False, "code": "invalid_fallback", "mutated": False,
                "error": "quiet_fallback must be 'skip' or 'asis'."}
    search_id = _s(opts.get("search_id")).strip()
    before = _album_count(ad)
    try:
        res = ad.run_import(paths=path, autotag=True, copy=not move, move=move,
                            write=opts.get("write", True) is not False,
                            search_ids=[search_id] if search_id else None,
                            quiet_fallback=fallback, timeout=timeout)
    except BeetsAdapterError as ex:
        return _import_refused(ex)
    after = _album_count(ad)
    added = after - before if before is not None and after is not None else None
    reported = res.get("skipped_paths") if isinstance(res, dict) else None
    if isinstance(reported, list):
        not_matched, known = [_s(x) for x in reported if _s(x)], True
    elif added == 0:
        not_matched, known = [path], True
    else:
        # ponytail: plugin 1.6.x does not report per-folder outcome; ARCH-024 adds skipped_paths.
        not_matched, known = [], False
    # Plugin 1.10.0 adds why Beets skipped each folder.
    reasons = {_s(r.get("path")): _s(r.get("reason")) for r in (res.get("skipped") or [])
               if isinstance(r, dict)} if isinstance(res, dict) and isinstance(res.get("skipped"), list) else {}
    skipped = [{"path": p, "reason": reasons.get(p) or "not_matched"} for p in not_matched]
    return {"ok": True, "copy": not move, "move": move, "quiet_fallback": fallback,
            "albums_imported": added, "not_matched": not_matched, "skipped": skipped,
            "not_matched_known": known, "result": res}


# -----------------------------------------------------------------------------
# 12. Playlist Cleanup & M3U Workflows
# -----------------------------------------------------------------------------


def _get_playlist_dir() -> Path:
    base = Path(os.environ.get("WEB_MANAGER_DATA_DIR", "/web-manager-data")) / "playlists"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _sanitize_playlist_key(playlist_key: str) -> str:
    safe_key = "".join(c for c in _decode_path(playlist_key) if c.isalnum() or c in ("-", "_")).strip()
    return safe_key or "playlist"


def _get_playlist_m3u_path(playlist_key: str, display_name: str = "") -> Path:
    return _get_playlist_dir() / f"{_sanitize_playlist_key(playlist_key)}.m3u"


def read_playlist_m3u(playlist_key: str, fallback_name: str = "") -> Dict[str, Any]:
    p = _get_playlist_m3u_path(playlist_key, fallback_name)
    if not p.exists():
        return {"ok": False, "exists": False, "tracks": []}
    lines = p.read_text(encoding="utf-8").splitlines()
    tracks = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    return {"ok": True, "exists": True, "playlist_key": playlist_key, "tracks": tracks, "count": len(tracks)}


def export_playlist_m3u(playlist_key: str, display_name: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    p = _get_playlist_m3u_path(playlist_key, display_name)
    lines = ["#EXTM3U\n"]
    for it in items:
        path = _decode_path(it.get("path") or "")
        title = it.get("title") or "Unknown"
        artist = it.get("artist") or "Unknown"
        length = int(it.get("length") or 0)
        lines.append(f"#EXTINF:{length},{artist} - {title}\n")
        lines.append(f"{path}\n")
    p.write_text("".join(lines), encoding="utf-8")
    return {"ok": True, "playlist_key": playlist_key, "path": str(p), "count": len(items)}


def delete_playlist_m3u(playlist_key: str, fallback_name: str = "") -> Dict[str, Any]:
    p = _get_playlist_m3u_path(playlist_key, fallback_name)
    if p.exists():
        p.unlink()
    return {"ok": True, "playlist_key": playlist_key}


def list_playlist_m3u() -> Dict[str, Any]:
    p_dir = _get_playlist_dir()
    files = [f.name for f in p_dir.glob("*.m3u")]
    return {"ok": True, "playlists": files}


def ensure_playlist_staging(playlist_key: str, playlist_id: str = "", name: str = "") -> Dict[str, Any]:
    stg_dir = Path(os.environ.get("WEB_MANAGER_DATA_DIR", "/web-manager-data")) / "playlist_staging" / _sanitize_playlist_key(playlist_key)
    stg_dir.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "path": str(stg_dir)}


def delete_playlist_staged_track(playlist_key: str, track_id: str, requested_path: str = "") -> Dict[str, Any]:
    """Delete one staged track file. A refused delete (outside staging, the
    data dir, a staging root, a symlink or a directory) is reported as
    ok=False -- never as success (S1)."""
    if not requested_path or not os.path.lexists(requested_path):
        return {"ok": True, "deleted": False, "track_id": track_id}
    try:
        resolved = _validated_staging_target(requested_path, "delete")
        if resolved.is_dir():
            raise ValueError(f"Refusing to delete a directory as a staged track: {requested_path}")
        _remove_resolved(resolved)
    except (ValueError, OSError) as exc:
        # The detail names absolute staging paths: server log only (#206 F5).
        log.warning("Staged track delete refused or failed for %r: %s", requested_path, exc)
        return {"ok": False, "deleted": False, "track_id": track_id,
                "error": "Could not delete the staged track file."}
    return {"ok": True, "deleted": True, "track_id": track_id}


def inspect_playlist_staged_track(playlist_key: str, track_id: str, requested_path: str = "") -> Dict[str, Any]:
    if not requested_path or not _is_safe_staging_path(requested_path):
        return {"ok": False, "exists": False}
    p = Path(requested_path).resolve()
    if not p.exists():
        return {"ok": False, "exists": False}
    return {"ok": True, "exists": True, "path": str(p), "size": p.stat().st_size}


def list_playlist_staged_files(playlist_key: str, playlist_id: str = "") -> Dict[str, Any]:
    stg_dir = Path(os.environ.get("WEB_MANAGER_DATA_DIR", "/web-manager-data")) / "playlist_staging" / _sanitize_playlist_key(playlist_key)
    if not stg_dir.exists():
        return {"ok": True, "files": []}
    files = [str(f) for f in stg_dir.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS]
    return {"ok": True, "files": files}


def place_playlist_imported_item(playlist_key: str, track_id: str, item_id: int) -> Dict[str, Any]:
    return {"ok": True, "playlist_key": playlist_key, "track_id": track_id, "item_id": item_id}


def get_playlist_quality_candidates(playlist_key: str, track_id: str, adapter: Optional[BeetsAdapter] = None) -> Dict[str, Any]:
    return {"ok": True, "candidates": []}


def validate_playlist_staged_track(playlist_key: str, track_id: str, requested_path: str) -> Dict[str, Any]:
    if not requested_path or not _is_safe_staging_path(requested_path):
        return {"ok": True, "valid": False, "path": requested_path}
    p = Path(requested_path).resolve()
    valid = p.exists() and p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    return {"ok": True, "valid": valid, "path": requested_path}


def import_playlist_staged(playlist_key: str, track_id: str, item_id: int, adapter: Optional[BeetsAdapter] = None) -> Dict[str, Any]:
    return {"ok": True, "playlist_key": playlist_key, "track_id": track_id, "item_id": item_id}


PLAYLIST_MEDIA_CLEANUP_FAMILY = "playlist_media_cleanup_v1"


def plan_playlist_media_cleanup(
    payload: Dict[str, Any],
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Preview removing playlist-media library ROWS. Files are kept on disk:
    this workflow never deletes media (S1). The plan must be Approved before
    :func:`apply_playlist_media_cleanup`."""
    st = _get_store(store)
    item_ids: List[int] = []
    for raw in payload.get("item_ids") or []:
        try:
            iid = int(raw)
        except (TypeError, ValueError):
            continue
        if iid > 0 and iid not in item_ids:
            item_ids.append(iid)
    if not item_ids:
        return {"ok": False, "error": "No valid item ids to remove."}
    reason = str(payload.get("reason") or "playlist media cleanup")
    tx = st.create(
        operation_type="Delete",
        status="Preview",
        summary=f"Remove {len(item_ids)} playlist media rows from the library (files kept on disk)",
        rollback_available=False,
        metadata={"mutation_family": PLAYLIST_MEDIA_CLEANUP_FAMILY, "item_ids": item_ids,
                  "delete_files": False, "reason": reason},
    )
    return {"ok": True, "operation_id": tx["id"], "status": "Preview", "item_ids": item_ids,
            "delete_files": False, "reason": reason}


def apply_playlist_media_cleanup(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Remove the planned rows (``delete_files=False``) exactly once, only
    from an Approved plan, and verify through the engine that they are gone."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    try:
        tx = st.get(operation_id)
    except KeyError:
        return {"ok": False, "code": "not_found", "error": "Transaction not found"}
    meta = tx.get("metadata") or {}
    if meta.get("mutation_family") != PLAYLIST_MEDIA_CLEANUP_FAMILY:
        return {"ok": False, "code": "wrong_family", "error": "Not a playlist media cleanup transaction."}
    status = tx.get("status")
    if meta.get("engine_result") or status in ("Completed", "Running", "Recovery Required", "Failed"):
        return {"ok": False, "code": "already_applied", "operation_id": operation_id, "status": status,
                "error": f"This cleanup was already applied (status {status})."}
    if status != "Approved":
        return {"ok": False, "code": "not_approved", "operation_id": operation_id, "status": status,
                "error": "Approve the cleanup preview before applying it."}
    item_ids = [int(x) for x in meta.get("item_ids") or []]
    from backend.resource_locks import attempt_owner, claim_approved, claim_refusal, locks as resource_locks
    with resource_locks().hold([f"item:{i}" for i in sorted(item_ids)], attempt_owner(operation_id), timeout=10):
        if claim_approved(st, operation_id) is None:
            return {"ok": False, "code": "not_approved", "operation_id": operation_id,
                    "error": claim_refusal(st, operation_id)}
        st.update(operation_id, metadata={"engine_request": {"item_ids": item_ids, "delete_files": False}})
        # Decide up front whether the adapter takes the key (#182): a
        # TypeError raised inside remove() is a failure, never a retry.
        key = {"idempotency_key": operation_id} if _accepts_kwarg(ad.remove, "idempotency_key") else {}
        try:
            res = ad.remove(item_ids=item_ids, delete_files=False, **key)
        except Exception as exc:
            if _transport_error(exc):
                st.append_log(operation_id, "Engine call outcome unknown (transport error); left Running for "
                                            "the recovery sweep -- do not re-apply.")
            else:
                st.update(operation_id, status="Failed", logs=[f"Engine refused the removal: {exc}"])
            raise
        getter = getattr(ad, "get_item", None)
        remaining: List[int] = []
        if callable(getter):
            for iid in item_ids:
                try:
                    if getter(iid):
                        remaining.append(iid)
                except Exception:
                    pass
        removed = [i for i in item_ids if i not in remaining]
        final = "Completed" if not remaining else "Recovery Required"
        st.update(operation_id, status=final,
                  metadata={"engine_result": res if isinstance(res, dict) else {"result": res},
                            "removed_item_ids": removed, "remaining_item_ids": remaining},
                  logs=[f"Removed {len(removed)} library rows; files kept on disk."]
                  + [f"Item {i} is still in the library" for i in remaining])
        return {"ok": not remaining, "operation_id": operation_id, "status": final, "deleted_items": removed,
                "remaining_items": remaining, "files_kept": True, "files_deleted": 0}


def remove_item_rows_keep_files(
    item_ids: List[int],
    reason: str,
    approved_by: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Plan -> Approve -> Apply a rows-only removal for an internal caller whose
    own (already operator-confirmed) workflow decided these stale rows must go.
    Files are never touched. Approval is recorded with ``approved_by`` through a
    CAS, so the transaction log shows who authorised it; the apply itself is
    the same claimed, locked, verified path as the UI flow."""
    st = _get_store(store)
    plan = plan_playlist_media_cleanup({"item_ids": item_ids, "reason": reason}, adapter=adapter, store=st)
    if not plan.get("ok"):
        return plan
    op_id = plan["operation_id"]
    if not _approve_preview(st, op_id, approved_by):
        return {"ok": False, "code": "not_approved", "operation_id": op_id,
                "error": "The cleanup preview could not be approved (it changed state)."}
    return apply_playlist_media_cleanup(op_id, adapter=adapter, store=st)


def rollback_playlist_media_cleanup(
    operation_id: str,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Row removal has no engine rollback; the files are still on disk and can
    be re-imported. Reported honestly as not supported."""
    return {"ok": False, "code": "not_supported", "operation_id": operation_id,
            "error": "Library rows were removed but the files were kept on disk; re-import them to restore."}


# -----------------------------------------------------------------------------
# 13. Query and Discovery Helpers
# -----------------------------------------------------------------------------


def resolve_folder_to_albums(
    folder_path: str,
    since: Optional[float] = None,
    adapter: Optional[BeetsAdapter] = None,
) -> List[int]:
    """Find all distinct album IDs associated with files under folder_path."""
    if not _is_within_music_root(folder_path):
        return []
    ad = adapter or beets_adapter
    p_norm = _decode_path(folder_path).rstrip("/\\")
    items = ad.get_items()
    matched_aids: Set[int] = set()
    for it in items:
        it_path = _decode_path(it.get("path"))
        if it_path.startswith(p_norm):
            aid = it.get("album_id")
            if aid is not None:
                matched_aids.add(int(aid))
    return sorted(matched_aids)


def get_folder_items(
    folder_path: Union[str, List[str]],
    adapter: Optional[BeetsAdapter] = None,
) -> List[Dict[str, Any]]:
    """Return items whose path starts with folder_path, or with any of its
    entries when a list of prefixes is given (e.g. several album folders'
    worth of candidate paths in one lookup)."""
    prefixes = folder_path if isinstance(folder_path, list) else [folder_path]
    norm_prefixes = [_decode_path(p).rstrip("/\\") for p in prefixes]
    norm_prefixes = [p for p in norm_prefixes if p and _is_within_music_root(p)]
    if not norm_prefixes:
        return []
    ad = adapter or beets_adapter
    items = ad.get_items()
    return [
        it for it in items
        if any(_decode_path(it.get("path")).startswith(p) for p in norm_prefixes)
    ]


def get_artist_folder_inventory(
    root: str,
    adapter: Optional[BeetsAdapter] = None,
) -> List[Dict[str, Any]]:
    if not _is_within_music_root(root):
        return []
    p = Path(root).resolve()
    if not p.exists() or not p.is_dir():
        return []
    results = []
    for d in sorted(p.iterdir()):
        if d.is_dir():
            count = sum(1 for _, _, files in os.walk(str(d)) for f in files if Path(f).suffix.lower() in AUDIO_EXTENSIONS)
            results.append({"name": d.name, "path": str(d), "audio_files": count})
    return results


def get_artist_folder_album_mbids(
    folder: str,
    adapter: Optional[BeetsAdapter] = None,
) -> List[Dict[str, Any]]:
    ad = adapter or beets_adapter
    aids = resolve_folder_to_albums(folder, adapter=ad)
    mbids = []
    for aid in aids:
        alb = ad.get_album(aid)
        if alb:
            mbids.append({
                "album_id": aid,
                "album": alb.get("album"),
                "mb_albumid": alb.get("mb_albumid"),
                "mb_releasegroupid": alb.get("mb_releasegroupid"),
            })
    return mbids


def get_artist_alias_groups(adapter: Optional[BeetsAdapter] = None) -> List[Dict[str, Any]]:
    ad = adapter or beets_adapter
    counts = ad.get_artist_counts()
    return [{"artist": k, "albums": v.get("albums", 0), "tracks": v.get("tracks", 0)} for k, v in sorted(counts.items())]


def get_mbid_sticking_candidates(adapter: Optional[BeetsAdapter] = None) -> List[Dict[str, Any]]:
    ad = adapter or beets_adapter
    albums = ad.get_albums()
    candidates = []
    for a in albums:
        if not a.get("mb_albumid") or not a.get("mb_releasegroupid"):
            candidates.append(a)
    return candidates


def get_rgid_group_detail(rgid: str, adapter: Optional[BeetsAdapter] = None) -> Dict[str, Any]:
    ad = adapter or beets_adapter
    albums = ad.find_all_albums_by_releasegroupid(rgid)
    return {"ok": True, "mb_releasegroupid": rgid, "albums": albums, "count": len(albums)}


def get_unmatched_review_items(
    adapter: Optional[BeetsAdapter] = None,
    limit: int = 500,
    offset: int = 0,
    include_singletons: bool = True,
) -> Dict[str, Any]:
    """Return albums, and optionally singleton items, missing a MusicBrainz
    identity, for the Import Review "Needs MB ID" queue.

    Albums come from the `albums` table (missing mb_albumid), annotated
    with their first item's id/path and a track count -- beetsplug.web
    doesn't expose either directly, so this derives them from a single
    pass over all items rather than one lookup per album.

    Singletons are already-imported items with no album row (album_id is
    NULL) missing mb_trackid. They can never appear in the albums-table
    query above, so without this they stay invisible to Import Review even
    though Library's disk-folder grouping already flags them.
    """
    ad = adapter or beets_adapter
    all_items = ad.get_items()

    by_album: Dict[int, List[Dict[str, Any]]] = {}
    singleton_candidates: List[Dict[str, Any]] = []
    for it in all_items:
        album_id = it.get("album_id")
        if album_id:
            by_album.setdefault(int(album_id), []).append(it)
        elif not it.get("mb_trackid"):
            singleton_candidates.append(it)

    unmatched_albums: List[Dict[str, Any]] = []
    for a in ad.get_albums():
        if a.get("mb_albumid"):
            continue
        aid = int(a["id"])
        album_items = sorted(
            by_album.get(aid, []),
            key=lambda it: (int(it.get("track") or 0), int(it.get("id") or 0)),
        )
        first_item = album_items[0] if album_items else {}
        row = dict(a)
        row["first_item_id"] = first_item.get("id", 0)
        row["first_item_path"] = first_item.get("path", "")
        row["tracks"] = len(album_items)
        unmatched_albums.append(row)
    unmatched_albums.sort(key=lambda r: float(r.get("added") or 0))

    singleton_candidates.sort(key=lambda it: float(it.get("added") or 0))

    end = offset + limit if limit else None
    return {
        "albums": unmatched_albums[offset:end],
        "singletons": singleton_candidates[offset:end] if include_singletons else [],
    }


def inspect_import_source(
    source_path: str,
    operation: str = "import",
    timeout: float = 60.0,
) -> Dict[str, Any]:
    # source_path reaches here from several callers (reimport scans, album
    # cleanup, manual discovery) with inconsistent upstream validation --
    # enforce containment here, at the actual filesystem-walking sink,
    # rather than trusting every present and future caller to do it first.
    if not _is_within_music_root(source_path) and not _is_safe_staging_path(source_path):
        return {"ok": False, "exists": False, "error": f"Path is outside approved roots: {source_path}"}
    p = Path(source_path).resolve()
    if not p.exists():
        return {"ok": False, "exists": False, "error": f"Path not found: {source_path}"}
    audio_files = []
    if p.is_dir():
        for root_dir, _, files in os.walk(str(p)):
            for f in files:
                if Path(f).suffix.lower() in AUDIO_EXTENSIONS:
                    audio_files.append(os.path.join(root_dir, f))
    elif p.suffix.lower() in AUDIO_EXTENSIONS:
        audio_files.append(str(p))

    return {
        "ok": True,
        "exists": True,
        "path": str(p),
        "is_dir": p.is_dir(),
        "audio_file_count": len(audio_files),
        "audio_files": audio_files[:50],
    }


def discover_import_sources(roots: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    target_roots = [Path(r) for r in roots] if roots else _get_staging_roots()
    found = []
    for root in target_roots:
        if root.exists() and root.is_dir():
            for child in sorted(root.iterdir()):
                if child.is_dir() and not child.name.startswith("."):
                    insp = inspect_import_source(str(child))
                    if insp.get("audio_file_count", 0) > 0:
                        found.append(insp)
    return found


def find_files_for_hardlink(
    filename: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    limit: int = 50,
    adapter: Optional[BeetsAdapter] = None,
) -> List[Dict[str, Any]]:
    ad = adapter or beets_adapter
    items = ad.get_items()
    matches = []
    fname_lower = filename.lower().strip()
    for it in items:
        p_str = _decode_path(it.get("path"))
        if fname_lower and fname_lower in Path(p_str).name.lower():
            matches.append(it)
            if len(matches) >= limit:
                break
    return matches


def create_hardlink(src_path: str, dst_path: str, expected_size: Optional[int] = None) -> Dict[str, Any]:
    """Hardlink a file into a staging/download root (LT-18).

    The target must be inside a staging root (never MUSIC_ROOT, no symlink
    component); the source must be a regular file of ``expected_size`` when
    given. An existing target is accepted only when it already IS the source
    (``already_present``); anything else is refused, never overwritten."""
    p_src = Path(src_path)
    p_dst = Path(dst_path)
    if not p_src.is_file() or p_src.is_symlink():
        raise FileNotFoundError(f"Source file not found: {src_path}")
    if expected_size is not None and p_src.stat().st_size != int(expected_size):
        raise ValueError("Source file size changed; nothing was linked.")
    if not _is_safe_staging_path(p_dst.parent if not p_dst.exists() else p_dst):
        raise ValueError("Refusing to link outside staging roots")
    p_dst = p_dst.parent.resolve() / p_dst.name
    if _is_protected_data_path(p_dst) or _is_staging_root(p_dst):
        raise ValueError("Refusing to link onto a protected path")
    if p_dst.exists():
        if os.path.samefile(p_src, p_dst):
            return {"ok": True, "already_present": True, "source": str(p_src), "destination": str(p_dst)}
        raise ValueError(f"Refusing to overwrite an existing file: {dst_path}")
    p_dst.parent.mkdir(parents=True, exist_ok=True)
    # Re-check right before linking (S1/F3): no symlink swapped into the
    # destination chain and nothing created at the target meanwhile.
    if _has_symlink_component(p_dst.parent) or os.path.lexists(str(p_dst)):
        raise ValueError("Destination changed during validation; nothing was linked.")
    os.link(str(p_src), str(p_dst))
    return {"ok": True, "already_present": False, "source": str(p_src), "destination": str(p_dst)}


def repoint_item_db_path(
    item_id: int,
    album_id: int,
    old_path: str,
    new_path: str,
    adapter: Optional[BeetsAdapter] = None,
) -> Dict[str, Any]:
    ad = adapter or beets_adapter
    ad.modify(fields={"path": new_path}, item_ids=[int(item_id)])
    return {"ok": True, "item_id": item_id, "new_path": new_path}


def cancel_job(job_id: str) -> Dict[str, Any]:
    """The engine has no cancel endpoint (LT-12): report that honestly
    instead of claiming a cancellation."""
    return {"ok": False, "job_id": job_id, "cancelled": False, "code": "not_supported",
            "error": "The Beets engine cannot cancel a running operation."}


def get_job(job_id: str, adapter: Optional[BeetsAdapter] = None) -> Dict[str, Any]:
    """Status of an engine operation from its registry (LT-12). It used to
    return a constant success for any id."""
    ad = adapter or beets_adapter
    try:
        op = ad.get_operation(str(job_id)) or {}
    except BeetsNotFoundError:
        return {"id": job_id, "status": "failed", "returncode": 1, "stdout": [], "stderr": [],
                "error": "The engine has no record of this operation."}
    raw = _s(op.get("status")).lower()
    status = {"running": "running", "succeeded": "success", "failed": "failed"}.get(raw, "failed")
    return {"id": job_id, "status": status, "returncode": 0 if status == "success" else (None if status == "running" else 1),
            "stdout": [], "stderr": [_s(op.get("error"))] if op.get("error") else [], "result": op.get("result")}


def clear_album_artpath(
    album_id: int, adapter: Optional[BeetsAdapter] = None
) -> Dict[str, Any]:
    ad = adapter or beets_adapter
    ad.modify(fields={"artpath": ""}, album_ids=[int(album_id)], write=False)
    return {"ok": True, "album_id": album_id}


def set_album_artpath(
    album_id: int, artpath: str, adapter: Optional[BeetsAdapter] = None
) -> Dict[str, Any]:
    ad = adapter or beets_adapter
    ad.modify(fields={"artpath": artpath}, album_ids=[int(album_id)], write=False)
    return {"ok": True, "album_id": album_id, "artpath": artpath}


def delete_album(
    album_id: int,
    delete_files: bool = False,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Remove an EMPTY album row (LT-3). An album that still has items is
    refused: removing tracks needs the approved track quarantine family and
    removing a whole album needs an approved album cleanup. Files are never
    deleted here, whatever ``delete_files`` says."""
    ad = adapter or beets_adapter
    plan_res = plan_album_maintenance(
        {"mode": "remove_album", "album_id": int(album_id), "delete_files": False, "source": "delete_album"},
        store=store,
    )
    if not plan_res.get("ok"):
        return plan_res
    return apply_album_maintenance(plan_res["operation_id"], adapter=ad, store=store)


def delete_file(path: str) -> Dict[str, Any]:
    """Delete a staging file or folder (LT-13).

    Only paths inside a staging/download root are accepted: never anything
    under MUSIC_ROOT, never a staging root itself, never the Web Manager data
    dir, its databases or backups, never through a symlink. The path is
    resolved once and re-checked with lstat right before the delete. Library
    files leave the library only through engine quarantine. Raises
    ValueError on a refused path and OSError on a real failure -- a partial
    delete is never reported as success."""
    p = Path(path)
    if not p.exists() and not p.is_symlink():
        return {"ok": True, "deleted": False, "reason": "not_found"}
    resolved = _validated_staging_target(p, "delete")
    _remove_resolved(resolved)
    return {"ok": True, "deleted": True, "path": str(resolved)}


def move_file(source: str, target: str) -> Dict[str, Any]:
    """Move a staging file or folder within staging roots (LT-13).

    Refuses MUSIC_ROOT, a staging root itself (source or target), the data
    dir and its databases/backups, symlinks and an existing target; library
    moves go through the Beets engine (relocation family)."""
    src = Path(source)
    if not src.exists():
        return {"ok": False, "error": f"Source file does not exist: {source}"}
    p_src = _validated_staging_target(src, "move")
    p_dst = _validated_staging_target(target, "move to")
    if os.path.lexists(str(p_dst)):
        raise ValueError(f"Refusing to overwrite an existing target: {target}")
    _move_resolved(p_src, p_dst)
    return {"ok": True, "source": str(p_src), "target": str(p_dst)}


def find_albums_with_mbid(
    limit: int = 100,
    sort: str = "desc",
    adapter: Optional[BeetsAdapter] = None,
) -> List[Dict[str, Any]]:
    ad = adapter or beets_adapter
    albums = ad.get_albums()
    results = [a for a in albums if a.get("mb_albumid")]
    if sort == "desc":
        results.sort(key=lambda x: int(x.get("id") or 0), reverse=True)
    else:
        results.sort(key=lambda x: int(x.get("id") or 0))
    return results[:limit]


def get_library_stats(*, timeout: float = 15.0) -> Dict[str, Any]:
    return lib.get_library_stats()


def get_transaction(
    transaction_id: str,
    *,
    format: str = "json",
    timeout: float = 15.0,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    st = _get_store(store)
    rec = st.get(transaction_id)
    if not rec:
        raise BeetsNotFoundError(f"Transaction {transaction_id} not found")
    return {"ok": True, "transaction": rec}


def list_transactions(
    *,
    limit: int = 50,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    st = _get_store(store)
    txs = st.list(limit=limit) if hasattr(st, "list") else []
    return {"ok": True, "transactions": txs}


def move_album_to_library(
    album_id: int, adapter: Optional[BeetsAdapter] = None, store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Move an album to its Beets path-template location through the album
    relocation family (LT-18: it called adapter.move() with arguments that
    do not exist and read a missing returncode)."""
    return relocate_album(int(album_id), mode="move", adapter=adapter, store=store)


_ALLOWED_COMMANDS = frozenset({"mbsubmit"})


def run_command(
    command: str,
    args: Optional[List[str]] = None,
    timeout: float = 60.0,
    adapter: Optional[BeetsAdapter] = None,
) -> Dict[str, Any]:
    """Arbitrary/legacy Beets commands are not supported (LT-12).

    This used to return a fabricated "mbsubmit ... completed" without
    contacting the engine. The stock-Beets engine has no command runner;
    AcoustID submission goes through beets_adapter.mbsubmit() (the
    submissions workflow). Nothing is executed here."""
    if command not in _ALLOWED_COMMANDS:
        raise ValueError(
            f"Prohibited command '{command}'. Arbitrary command execution is not permitted; "
            f"only allowlisted operations {_ALLOWED_COMMANDS} are allowed."
        )
    for arg in args or []:
        if not isinstance(arg, str):
            raise ValueError(f"Invalid argument type: {type(arg)}")
        if any(char in arg for char in (";", "|", "&", "$", "`", "\n", "\r")):
            raise ValueError(f"Illegal character in command argument: {arg!r}")
    return {"ok": False, "code": "not_supported", "returncode": 1, "stdout": "", "stderr": "",
            "error": "Generating a MusicBrainz submission through the Beets engine is not supported; "
                     "nothing was run. Use the submissions workflow."}


def write_tags(file_path: str, tags: Dict[str, Any]) -> Dict[str, Any]:
    return write_staging_tags(file_path, tags)


# Adapter / Library delegation helpers
def get_album(album_id: int) -> Optional[Dict[str, Any]]:
    return lib.get_album(album_id)


def get_item(item_id: int) -> Optional[Dict[str, Any]]:
    return lib.get_item(item_id)


def find_all_items_by_album_id(album_id: int) -> List[Dict[str, Any]]:
    return beets_adapter.find_all_items_by_album_id(int(album_id))


def find_all_albums_by_mb_albumid(mb_albumid: str) -> List[Dict[str, Any]]:
    return beets_adapter.find_all_albums_by_mb_albumid(mb_albumid)


def find_all_albums_by_albumartist(albumartist: str) -> List[Dict[str, Any]]:
    return beets_adapter.find_all_albums_by_albumartist(albumartist)


def find_all_albums_by_releasegroupid(rgid: str) -> List[Dict[str, Any]]:
    return beets_adapter.find_all_albums_by_releasegroupid(rgid)


def find_all_items_by_mbid(mb_trackid: str) -> List[Dict[str, Any]]:
    return beets_adapter.find_all_items_by_mbid(mb_trackid)


def find_all_orphan_albums() -> List[Dict[str, Any]]:
    return beets_adapter.find_all_orphan_albums()


def find_item_by_path(path: str) -> Optional[Dict[str, Any]]:
    return beets_adapter.find_item_by_path(path)


def find_items_by_query(query: Any) -> List[Dict[str, Any]]:
    return beets_adapter.find_items_by_query(query)


def find_albums_by_query(query: Any) -> List[Dict[str, Any]]:
    return beets_adapter.find_albums_by_query(query)


def get_items_page(offset: int = 0, limit: int = 50) -> Dict[str, Any]:
    return beets_adapter.get_items_page(offset=offset, limit=limit)


def list_distinct_albumartists() -> List[str]:
    return beets_adapter.list_distinct_albumartists()


def list_distinct_item_paths() -> List[str]:
    return beets_adapter.list_distinct_item_paths()


def get_album_cleanup_index() -> List[Dict[str, Any]]:
    return beets_adapter.get_album_cleanup_index()


#: Library-wide MusicBrainz sync run by Beets' own mbsync inside Beets
#: (plugin 1.13.0, POST /webmanager/mbsync/library).
MBSYNC_LIBRARY_FAMILY = "mbsync_library_v1"
MBSYNC_LIBRARY_ROLLBACK_REASON = (
    "Beets' mbsync keeps no undo, so this sync cannot be rolled back. The change log lists the old "
    "and new values of the first 200 changed albums or singles, so they can be put back by hand. "
    "Files are never moved; tags written to files stay written.")
_MBSYNC_LIBRARY_REFUSALS = {
    "BEETS_NOT_FOUND": "Restart the beets container so it loads webmanager plugin 1.13.0; nothing was changed.",
    "CAPABILITY_UNAVAILABLE": "Enable Beets' mbsync plugin (add mbsync to plugins: in Beets' config.yaml and "
                              "restart Beets); nothing was changed.",
    "ALREADY_RUNNING": "Beets is already syncing the library from MusicBrainz; wait for it to finish.",
}
_MBSYNC_LIBRARY_COUNTS = ("albums_total", "singletons_total", "targets", "processed", "changed_albums",
                          "changed_singletons", "changed_items", "unchanged", "not_found", "skipped_no_id",
                          "skipped_empty", "skipped_missing", "failed_count", "write_failed_count")


def _mbsync_change_rows(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The plugin's change log as transaction change rows."""
    def diff(fields):
        return [{"field": k, "old": v[0], "new": v[1], "changed": True} for k, v in (fields or {}).items()
                if isinstance(v, list) and len(v) == 2]
    rows = []
    for entry in entries:
        base = {"operation": "MusicBrainz Sync", "artist": _s(entry.get("artist")),
                "album": _s(entry.get("album")), "source": "Beets mbsync"}
        if entry.get("album_fields"):
            rows.append({**base, "id": f"album:{entry.get('id')}", "track": "",
                         "metadata_diff": diff(entry["album_fields"])})
        for item in entry.get("items") or []:
            rows.append({**base, "id": f"item:{item.get('item_id')}", "track": _s(item.get("title")),
                         "metadata_diff": diff(item.get("fields"))})
    return rows


def mbsync_library(
    log: List[str],
    cancel_event: Any = None,
    *,
    transaction_id: str = "",
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    poll_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    max_poll_errors: int = 30,
) -> Dict[str, Any]:
    """Sync the whole library from MusicBrainz with Beets' own mbsync,
    run inside Beets by the webmanager plugin (LT-18). Web Manager only
    starts it, follows it, forwards a cancel and records what Beets
    reports changing on ``transaction_id``. Tag writes follow Beets'
    import.write, as `beet mbsync` does; files are never moved. A target
    that failed, or whose tags could not be written, fails the job
    (MBSYNC_PARTIAL) with the counts; the other targets keep their changes.

    A cancel stops Beets after the album it is on; albums already synced
    keep their changes. There is no rollback (MBSYNC_LIBRARY_ROLLBACK_REASON)."""
    from job_engine import cancel_requested

    ad = adapter or beets_adapter
    st = _get_store(store) if transaction_id else None
    try:
        started = ad.mbsync_library(idempotency_key=f"mbsync-library-{transaction_id or os.urandom(8).hex()}")
    except BeetsAdapterError as ex:
        code = ex.error_code
        return {"ok": False, "code": code, "mutated": False,
                "error": _MBSYNC_LIBRARY_REFUSALS.get(code) or f"Beets refused the library sync ({code}); nothing was changed."}
    op_id = _s(started.get("operation_id"))
    write, move = bool(started.get("write")), bool(started.get("move"))
    log.append(f"Beets is syncing the library from MusicBrainz with mbsync (operation {op_id}; "
               f"write tags {'yes' if write else 'no'} per Beets' import.write; files are never moved).")
    if st:
        st.update(transaction_id, metadata={"mutation_family": MBSYNC_LIBRARY_FAMILY, "engine_operation_id": op_id,
                                            "write": write, "move": move})

    cancel_sent, errors, logged = False, 0, 0
    while True:
        if not cancel_sent and cancel_requested(cancel_event):
            cancel_sent = True
            try:
                ad.cancel_mbsync_library(op_id)
                log.append("Cancel sent: Beets stops after the album it is on; albums already synced keep their changes.")
            except BeetsAdapterError as ex:
                log.append(f"Beets did not take the cancel ({ex.error_code}); the sync had already finished.")
        try:
            op = ad.get_operation(op_id) or {}
            errors = 0
        except BeetsAdapterNotFoundError:
            return {"ok": False, "code": "operation_lost", "operation_id": op_id, "mutated": None,
                    "error": "Beets no longer knows this sync (was Beets restarted?). It stopped there; albums "
                             "synced before that keep their changes."}
        except BeetsAdapterError as ex:
            errors += 1
            if errors >= max_poll_errors:
                return {"ok": False, "code": "engine_unreachable", "operation_id": op_id, "mutated": None,
                        "error": f"Lost contact with Beets ({ex.error_code}); the sync may still be running there."}
            sleep(poll_seconds)
            continue
        status, result = op.get("status"), op.get("result") or {}
        processed = int(result.get("processed") or 0)
        if processed >= logged + 25 or (status != "running" and processed > logged):
            logged = processed
            log.append(f"Synced {processed} of {result.get('targets')}: "
                       f"{int(result.get('changed_albums') or 0) + int(result.get('changed_singletons') or 0)} changed.")
        if status in ("succeeded", "failed"):
            break
        sleep(poll_seconds)

    counts = {k: int(result.get(k) or 0) for k in _MBSYNC_LIBRARY_COUNTS}
    changed = counts["changed_albums"] + counts["changed_singletons"]
    rows = _mbsync_change_rows(result.get("changes") or [])
    cancelled = bool(result.get("cancelled"))
    write_failed = result.get("write_failed") or []
    partial = counts["failed_count"] + counts["write_failed_count"]
    out: Dict[str, Any] = {"ok": status == "succeeded" and not partial, "operation_id": op_id,
                           "cancelled": cancelled, "write": write, "move": move, "summary": counts,
                           "changed": changed, "mutated": changed > 0, "failed": result.get("failed") or [],
                           "write_failed": write_failed, "changes_truncated": bool(result.get("changes_truncated"))}
    if status != "succeeded":
        out.update(code=_s(op.get("error_code")) or "mbsync_failed",
                   error=f"Beets' library sync failed: {_s(op.get('error')) or 'unknown error'}.")
    elif partial:
        parts = []
        if counts["failed_count"]:
            parts.append(f"{counts['failed_count']} albums or singles could not be synced (see the log)")
        if counts["write_failed_count"]:
            parts.append(f"{counts['write_failed_count']} tracks' tags could not be written (their Beets "
                         "database fields did change)")
        out.update(code="MBSYNC_PARTIAL",
                   error=f"{'; '.join(parts)}. {counts['processed']} of {counts['targets']} were synced.")
    if st:
        st.update(transaction_id, changes=rows, counts={"albums": counts["changed_albums"],
                                                        "items": counts["changed_items"], "changes": len(rows)},
                  metadata={"engine_result": {**counts, "cancelled": cancelled, "aborted": bool(result.get("aborted")),
                                              "failed": out["failed"], "write_failed": write_failed,
                                              "changes_truncated": out["changes_truncated"]}})
    for f in out["failed"][:20]:
        log.append(f"  [warn] {f.get('kind')} {f.get('id')}: {f.get('error')}")
    for f in write_failed[:20]:
        log.append(f"  [warn] item {f.get('item_id')} ({f.get('title')}): tags not written to {f.get('path')}")
    log.append(f"{counts['processed']} of {counts['targets']} synced: {changed} changed ({counts['changed_items']} "
               f"tracks), {counts['unchanged']} unchanged, {counts['not_found']} not found at MusicBrainz, "
               f"{counts['failed_count']} failed, {counts['write_failed_count']} tag writes failed, "
               f"{counts['skipped_no_id']} without a MusicBrainz ID skipped.")
    if cancelled and cancel_event is not None:
        cancel_event.is_set()  # Beets stopped because of the cancel: the job records it Cancelled
        log.append("[cancelled] Beets stopped the sync; the albums synced before that keep their changes.")
    return out
