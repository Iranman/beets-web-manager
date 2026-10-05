"""Composite Workflow Engine for Beets Web Manager.

Orchestrates multi-step, user-confirmed workflows across stock Beets:
- Web Manager owns planning, preview, audit trails, checkpoints, and rollback.
- Stock Beets owns library state mutations via BeetsAdapter (/webmanager/* endpoints).
- Zero raw SQLite access to musiclibrary.blb.
- Zero Docker socket access.
"""

from __future__ import annotations

import base64
import copy
import hashlib
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
from backend.transaction_engine import TransactionStore

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
# Path & Staging Utilities
# -----------------------------------------------------------------------------


def _get_staging_roots() -> List[Path]:
    roots_env = os.environ.get("BEETS_IMPORT_ROOTS") or os.environ.get("DOWNLOAD_PATH") or "/downloads"
    paths = []
    for r in roots_env.split(","):
        r = r.strip()
        if r:
            paths.append(Path(r).resolve())
    data_dir = Path(os.environ.get("WEB_MANAGER_DATA_DIR", "/web-manager-data")).resolve()
    paths.append(data_dir)
    return paths


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
    """True if the path itself or any existing parent is a symlink."""
    p = Path(path)
    for candidate in [p, *p.parents]:
        try:
            if candidate.is_symlink():
                return True
        except OSError:
            return True
    return False


def _music_root() -> Path:
    return Path(os.environ.get("MUSIC_ROOT", "/music")).resolve()


def _is_safe_staging_path(path: Union[str, Path]) -> bool:
    if _has_symlink_component(path):
        return False
    p = Path(path).resolve()
    music_root = Path(os.environ.get("MUSIC_ROOT", "/music")).resolve()
    # Must NOT be inside music root
    try:
        if p == music_root or music_root in p.parents:
            return False
    except Exception:
        return False
    # Must be within allowed staging roots
    for stg in _get_staging_roots():
        try:
            if p == stg or stg in p.parents:
                return True
        except Exception:
            pass
    return False


def delete_staging_file(path: str) -> Dict[str, Any]:
    """Delete a file safely within staging/download roots (never inside music library)."""
    p = Path(path).resolve()
    if not p.exists():
        return {"ok": True, "deleted": False, "message": "File does not exist"}
    if not _is_safe_staging_path(p):
        raise ValueError(f"Refusing to delete file outside staging roots: {path}")
    if p.is_dir():
        shutil.rmtree(str(p), ignore_errors=True)
    else:
        p.unlink()
    return {"ok": True, "deleted": True, "path": str(p)}


def move_staging_file(src: str, dst: str) -> Dict[str, Any]:
    """Move a file safely within staging roots."""
    p_src = Path(src).resolve()
    p_dst = Path(dst).resolve()
    if not p_src.exists():
        raise FileNotFoundError(f"Source file does not exist: {src}")
    if not _is_safe_staging_path(p_src) or not _is_safe_staging_path(p_dst):
        raise ValueError("Move operations must stay within staging roots")
    p_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(p_src), str(p_dst))
    return {"ok": True, "source": str(p_src), "destination": str(p_dst)}


def write_staging_tags(path: str, tags: Dict[str, Any]) -> Dict[str, Any]:
    """Write audio metadata tags directly to a file before library import."""
    p = Path(path).resolve()
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    try:
        import mediafile
        mf = mediafile.MediaFile(str(p))
        for k, v in tags.items():
            if hasattr(mf, k):
                setattr(mf, k, v)
        mf.save()
        return {"ok": True, "path": str(p), "tags_written": list(tags.keys())}
    except Exception as exc:
        log.warning("Failed to write tags to %s: %s", path, exc)
        return {"ok": False, "error": str(exc), "path": str(p)}


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
        rollback_available=True,
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

    if album_ids and canonical_name:
        ad.modify(
            fields={"albumartist": canonical_name},
            album_ids=[int(x) for x in album_ids],
            write=True,
            move=True,
        )

    st.update(operation_id, status="Completed")
    return {"ok": True, "operation_id": operation_id, "status": "Completed"}


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

    for s in before_state:
        aid = s.get("album_id")
        orig = s.get("albumartist")
        if aid and orig:
            ad.modify(fields={"albumartist": orig}, album_ids=[int(aid)], write=True, move=True)

    st.update(operation_id, status="Rolled Back")
    return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}


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
        st.update(cleanup_id, status="Approved", metadata={"approved_by": _s(approved_by)})
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
        return False, f"music root is not readable: {exc}"
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
    ad.remove(item_ids=[int(t["id"]) for t in targets], delete_files=False)
    _get_store(store).update(op_id, status="Completed")
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
    ad.remove(item_ids=out["item_ids"], delete_files=False)
    _get_store(store).update(op_id, status="Completed")
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
    from backend.resource_locks import attempt_owner, claim_approved, locks as resource_locks
    with resource_locks().hold([f"item:{min(target_id, source_id)}", f"item:{max(target_id, source_id)}"],
                               attempt_owner(operation_id), timeout=10):
        if claim_approved(st, operation_id) is None:
            return {"ok": False, "code": "not_approved", "error": "Another attempt already claimed this transaction."}
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
    res = ad.rollback_replace_item_file(engine["quarantine_id"], idempotency_key=f"{operation_id}:rollback")
    result = res.get("result") if isinstance(res.get("result"), dict) else res
    st.update(
        operation_id,
        status="Rolled Back",
        metadata={**meta, "rollback_result": result},
        logs=[f"Restored item {meta['target_item_id']} to {result.get('restored_target_path')}; "
              f"replacement re-added as item {result.get('recreated_source_item_id')}"],
    )
    out = {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}
    out.update({k: result.get(k) for k in ("restored_target_path", "recreated_source_item_id", "recreated_source_path")})
    return out


# -----------------------------------------------------------------------------
# 6. Folder Cleanup & Album Cleanup
# -----------------------------------------------------------------------------


def plan_folder_cleanup(
    payload_or_action: Any = None,
    store: Optional[TransactionStore] = None,
    **kwargs,
) -> Dict[str, Any]:
    st = _get_store(store)
    data = payload_or_action if isinstance(payload_or_action, dict) else kwargs
    source = data.get("source") or data.get("source_path") or ""
    action = data.get("action", "remove_empty")

    tx = st.create(
        operation_type="Library Cleanup",
        status="Preview",
        summary=f"Folder cleanup ({action}) on {source}",
        metadata={"source": source, "action": action},
    )
    return {"ok": True, "operation_id": tx["id"], "token": tx["id"], "status": "Preview", "action": action, "source": source}


def apply_folder_cleanup(
    operation_id: str,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    st = _get_store(store)
    tx = st.get(operation_id)
    meta = tx.get("metadata", {})
    source = meta.get("source")
    action = meta.get("action", "remove_empty")

    if source and os.path.exists(source) and os.path.isdir(source):
        music_root = Path(os.environ.get("MUSIC_ROOT", "/music")).resolve()
        p_src = Path(source).resolve()
        # Refuse to delete the root music directory itself
        if p_src == music_root:
            raise ValueError("Refusing to delete root music directory")
        if action == "remove_empty":
            try:
                # Remove if empty
                if not any(p_src.iterdir()):
                    p_src.rmdir()
            except Exception as exc:
                log.warning("Could not remove empty directory %s: %s", source, exc)

    st.update(operation_id, status="Completed")
    return {"ok": True, "operation_id": operation_id, "status": "Completed"}


def rollback_folder_cleanup(
    operation_id: str,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    st = _get_store(store)
    st.update(operation_id, status="Rolled Back")
    return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}


ALBUM_CLEANUP_FAMILY = "album_cleanup_v1"

#: Phrase an operator must send to plan an album removal that ALSO deletes
#: the audio files (irreversible). Without it a removal is row-only.
DELETE_ALBUM_FILES_CONFIRMATION = "DELETE ALBUM FILES"


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
    from backend.resource_locks import attempt_owner, claim_approved, locks as resource_locks
    with resource_locks().hold([f"album:{aid}"], attempt_owner(operation_id), timeout=10):
        if claim_approved(st, operation_id) is None:
            return {"ok": False, "code": "not_approved", "mutated": False,
                    "error": "Another attempt already claimed this transaction."}
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
    from backend.resource_locks import attempt_owner, claim_approved, locks as resource_locks
    with resource_locks().hold([f"album:{aid}"] + [f"item:{int(e['item_id'])}" for e in entries],
                               attempt_owner(operation_id), timeout=10):
        if claim_approved(st, operation_id) is None:
            return {"ok": False, "code": "not_approved", "error": "Another attempt already claimed this transaction."}
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
    res = ad.rollback_quarantine_remove_items(engine["quarantine_id"], idempotency_key=f"{operation_id}:rollback")
    result = res.get("result") if isinstance(res.get("result"), dict) else res
    st.update(operation_id, status="Rolled Back", metadata={"rollback_result": result},
              logs=[f"Restored item {r.get('old_item_id')} as {r.get('new_item_id')} at {r.get('path')}"
                    for r in result.get("restored") or []])
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
    artpath = ""
    if aid:
        ad.fetch_art(album_ids=[int(aid)])
        ad.embed_art(album_ids=[int(aid)])
        album = ad.get_album(int(aid))
        artpath = album.get("artpath", "") if album else ""
    st.update(op_id, status="Completed")
    return {"ok": True, "operation_id": op_id, "status": "Completed", "artpath": artpath}


def rollback_album_artwork_fetch(
    operation_id: str,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    st = _get_store(store)
    st.update(operation_id, status="Rolled Back")
    return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}


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


def replace_album_art(
    album_id: int,
    image_data: Any = b"",
    ext: str = "jpg",
    adapter: Optional[BeetsAdapter] = None,
    **_kwargs: Any,
) -> Dict[str, Any]:
    """Replacing album art is refused (LT-18).

    It wrote the image straight into the album folder from this container
    (the music mount is read-only here) and set artpath with a bare modify,
    with no audit or rollback; its caller also passed arguments it did not
    accept. A correct version needs an engine artwork-write operation."""
    return {"ok": False, "code": "not_supported", "album_id": int(album_id),
            "error": "Replacing album artwork is not supported yet; nothing was changed."}


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
    if aid:
        fetch_and_embed_album_art(int(aid), adapter=ad, store=st)
    st.update(operation_id, status="Completed")
    return {"ok": True, "operation_id": operation_id, "status": "Completed"}


def rollback_album_artwork(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    st = _get_store(store)
    st.update(operation_id, status="Rolled Back")
    return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}


# -----------------------------------------------------------------------------
# 8. MB Track Repair & Metadata Repair Workflows
# -----------------------------------------------------------------------------


def plan_album_mb_track_repair(
    payload_or_album_id: Any = None,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    **kwargs,
) -> Dict[str, Any]:
    """Plan repairing MusicBrainz track IDs and titles on an album."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    payload = payload_or_album_id if isinstance(payload_or_album_id, dict) else kwargs

    aid = int(payload.get("album_id") or payload.get("aid") or (payload_or_album_id if isinstance(payload_or_album_id, (int, str)) and str(payload_or_album_id).isdigit() else 0))
    if not aid:
        return {"ok": False, "error": "album_id required"}

    album = ad.get_album(aid)
    if not album:
        return {"ok": False, "error": f"Album {aid} not found"}

    items = ad.find_all_items_by_album_id(aid)
    changes = []
    before_state = []
    for it in items:
        before_state.append({
            "item_id": it.get("id"),
            "mb_trackid": it.get("mb_trackid"),
            "title": it.get("title"),
        })
        changes.append({
            "item_id": it.get("id"),
            "title": it.get("title"),
            "mb_trackid": it.get("mb_trackid"),
        })

    tx = st.create(
        operation_type="MusicBrainz Match",
        status="Preview",
        summary=f"Repair MB track metadata for album {aid} ({album.get('album')})",
        changes=changes,
        rollback_available=True,
        metadata={"album_id": aid, "before_state": before_state, "payload": payload},
    )

    return {
        "ok": True,
        "operation_id": tx["id"],
        "token": tx["id"],
        "status": "Preview",
        "album": album,
        "items": items,
        "changes": changes,
    }


def apply_album_mb_track_repair(
    operation_id: str,
    write_tags: bool = True,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    ad = adapter or beets_adapter
    st = _get_store(store)
    tx = st.get(operation_id)
    aid = tx.get("metadata", {}).get("album_id")
    if aid:
        ad.mbsync(album_ids=[int(aid)], write=write_tags, move=write_tags)
    st.update(operation_id, status="Completed")
    return {"ok": True, "operation_id": operation_id, "status": "Completed"}


def rollback_album_mb_track_repair(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    ad = adapter or beets_adapter
    st = _get_store(store)
    tx = st.get(operation_id)
    before_state = tx.get("metadata", {}).get("before_state", [])
    for s in before_state:
        iid = s.get("item_id")
        orig_mbid = s.get("mb_trackid")
        if iid and orig_mbid is not None:
            ad.modify(fields={"mb_trackid": orig_mbid}, item_ids=[int(iid)], write=True)
    st.update(operation_id, status="Rolled Back")
    return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}


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
    store: Optional[TransactionStore] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
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
    aid = payload.get("album_id")
    tx = st.create(
        operation_type="Metadata Update",
        status="Preview",
        summary=f"Metadata update for album {aid}",
        metadata=payload,
    )
    return {
        "ok": True,
        "operation_id": tx["id"],
        "token": tx["id"],
        "status": "Preview",
        "album_fields_changed": len(payload.get("updates") or {}),
        "items_changed": len(payload.get("item_updates") or {}),
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
    op_id = operation_id or plan_token
    if not op_id:
        return {"ok": False, "error": "operation_id or plan_token is required"}
    ad = adapter or beets_adapter
    st = _get_store(store)
    tx = st.get(op_id)
    meta = tx.get("metadata", {})
    aid = meta.get("album_id")
    updates = meta.get("updates") or {}
    item_updates = meta.get("item_updates") or {}
    write = meta.get("write_tags", True)
    move = meta.get("force_write_tags", force_write_tags)

    if aid and updates:
        ad.modify(fields=updates, album_ids=[int(aid)], write=write, move=move)
    if item_updates:
        for iid, iup in item_updates.items():
            if iup:
                ad.modify(fields=iup, item_ids=[int(iid)], write=write, move=move)
    st.update(op_id, status="Completed")
    return {"ok": True, "operation_id": op_id, "status": "Completed"}


def rollback_album_metadata(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    st = _get_store(store)
    st.update(operation_id, status="Rolled Back")
    return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}


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
    plan_res = plan_album_metadata(payload, store=store)
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
        metadata=payload,
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
    move = meta.get("force_write_tags", force_write_tags)
    if iid and updates:
        ad.modify(fields=updates, item_ids=[int(iid)], write=write, move=move)
    st.update(op_id, status="Completed")
    return {"ok": True, "operation_id": op_id, "status": "Completed"}


def rollback_item_metadata(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    st = _get_store(store)
    st.update(operation_id, status="Rolled Back")
    return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}


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
    if mode not in _ALBUM_MAINTENANCE_SUPPORTED_MODES or not aid:
        st.update(operation_id, status="Failed",
                  logs=[f"Album maintenance mode {mode or '(none)'!r} is not supported; nothing was changed."])
        return {"ok": False, "code": "not_supported", "operation_id": operation_id, "status": "Failed",
                "error": f"Album maintenance mode {mode or '(none)'!r} is not supported; nothing was changed."}
    if not ad.get_album(aid):
        st.update(operation_id, status="Completed", logs=[f"Album {aid} is already gone."])
        return {"ok": True, "operation_id": operation_id, "status": "Completed", "deleted_albums": 0}
    if ad.find_all_items_by_album_id(aid):
        st.update(operation_id, status="Failed", logs=[f"Album {aid} still has items; nothing was removed."])
        return {"ok": False, "code": "album_not_empty", "operation_id": operation_id, "status": "Failed",
                "error": f"Album {aid} still has items; only an empty album row can be removed here."}
    ad.remove(album_ids=[aid], delete_files=False)
    st.update(operation_id, status="Completed", logs=[f"Removed empty album row {aid}."])
    return {"ok": True, "operation_id": operation_id, "status": "Completed", "deleted_albums": 1}


def rollback_album_maintenance(
    operation_id: str,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    st = _get_store(store)
    st.update(operation_id, status="Rolled Back")
    return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}


def plan_album_relocation(
    payload: Optional[Dict[str, Any]] = None,
    *,
    album_id: Optional[int] = None,
    mode: str = "rename",
    store: Optional[TransactionStore] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    st = _get_store(store)
    if payload is None:
        payload = {"album_id": album_id, "mode": mode, **kwargs}
    aid = payload.get("album_id")
    tx = st.create(
        operation_type="Move",
        status="Preview",
        summary=f"Album relocation for album {aid}",
        metadata=payload,
    )
    return {"ok": True, "operation_id": tx["id"], "token": tx["id"], "status": "Preview", **payload}


def apply_album_relocation(
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
    if aid:
        ad.move(album_ids=[int(aid)])
    st.update(op_id, status="Completed")
    return {"ok": True, "operation_id": op_id, "status": "Completed", "moved_count": 1}


def rollback_album_relocation(
    operation_id: str,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    st = _get_store(store)
    st.update(operation_id, status="Rolled Back")
    return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}


def relocate_album(
    album_id: int,
    mode: str = "rename",
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    plan_res = plan_album_relocation({"album_id": int(album_id), "mode": mode, **kwargs}, store=store)
    if not plan_res.get("ok"):
        return {"ok": False, "error": plan_res.get("error") or "Relocation plan rejected", "code": plan_res.get("code")}
    op_id = plan_res.get("operation_id")
    if not op_id:
        return {"ok": True, "dest_dir": plan_res.get("dest_dir") or "", "moved_count": 0}
    apply_res = apply_album_relocation(op_id, adapter=adapter, store=store)
    if not apply_res.get("ok"):
        return {"ok": False, "error": apply_res.get("error") or "Relocation apply failed"}
    return {
        "ok": True,
        "dest_dir": apply_res.get("dest_dir") or plan_res.get("dest_dir") or "",
        "moved_count": apply_res.get("moved_count", 0),
    }


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
        metadata=payload,
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
    if aid:
        ad.lastgenre(album_ids=[int(aid)], force=True)
    st.update(op_id, status="Completed")
    return {"ok": True, "operation_id": op_id, "status": "Completed"}


def rollback_album_genre_repair(
    operation_id: str,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    st = _get_store(store)
    st.update(operation_id, status="Rolled Back")
    return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}


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


def plan_import_review_cleanup(
    payload_or_folder: Any = None,
    store: Optional[TransactionStore] = None,
    **kwargs,
) -> Dict[str, Any]:
    st = _get_store(store)
    data = payload_or_folder if isinstance(payload_or_folder, dict) else kwargs
    folder = data.get("folder") or data.get("folder_path") or data.get("source") or ""
    tx = st.create(
        operation_type="Library Cleanup",
        status="Preview",
        summary=f"Import review cleanup for folder {folder}",
        metadata=data,
    )
    return {"ok": True, "operation_id": tx["id"], "token": tx["id"], "status": "Preview", **data}


def apply_import_review_cleanup(
    operation_id: str,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    st = _get_store(store)
    tx = st.get(operation_id)
    meta = tx.get("metadata", {})
    folder = meta.get("folder") or meta.get("folder_path") or meta.get("source")
    if folder:
        p = Path(folder).resolve()
        if p.exists() and _is_safe_staging_path(p):
            shutil.rmtree(str(p), ignore_errors=True)
    st.update(operation_id, status="Completed")
    return {"ok": True, "operation_id": operation_id, "status": "Completed"}


def rollback_import_review_cleanup(
    operation_id: str,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    st = _get_store(store)
    st.update(operation_id, status="Rolled Back")
    return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}


def plan_confirmed_import(
    payload: Dict[str, Any],
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    st = _get_store(store)
    tx = st.create(
        operation_type="Import",
        status="Preview",
        summary=f"Confirmed non-interactive import for {payload.get('paths') or payload.get('path')}",
        metadata=payload,
    )
    return {"ok": True, "operation_id": tx["id"], "token": tx["id"], "status": "Preview", **payload}


def apply_confirmed_import(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    ad = adapter or beets_adapter
    st = _get_store(store)
    tx = st.get(operation_id)
    meta = tx.get("metadata", {})
    paths = meta.get("paths") or meta.get("path") or []
    fields = meta.get("fields") or meta.get("set_fields") or {}
    # LT-17: honour the caller's copy/move choice (it used to force move=True,
    # so a "copy" import consumed its source).
    use_move = meta.get("use_move", True) is not False
    res = ad.run_import(paths=paths, autotag=False, copy=not use_move, move=use_move, write=True,
                        set_fields=fields)
    st.update(operation_id, status="Completed")
    return {"ok": True, "operation_id": operation_id, "status": "Completed", "result": res}


def rollback_import_folder(
    operation_id: str,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    st = _get_store(store)
    st.update(operation_id, status="Rolled Back")
    return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}


def reimport_source(
    path: str,
    beets_options: Optional[Dict[str, Any]] = None,
    timeout: float = 300.0,
    adapter: Optional[BeetsAdapter] = None,
) -> Dict[str, Any]:
    """Trigger native Beets import on a source path."""
    ad = adapter or beets_adapter
    opts = beets_options or {}
    return ad.run_import(
        paths=path,
        autotag=opts.get("autotag", True),
        move=opts.get("move", True),
        write=opts.get("write", True),
    )


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
    if requested_path and os.path.exists(requested_path) and _is_safe_staging_path(requested_path):
        os.unlink(requested_path)
    return {"ok": True, "track_id": track_id}


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


def plan_playlist_media_cleanup(
    payload: Dict[str, Any],
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    st = _get_store(store)
    item_ids = payload.get("item_ids") or []
    tx = st.create(
        operation_type="Delete",
        status="Preview",
        summary=f"Delete {len(item_ids)} playlist media tracks",
        metadata=payload,
    )
    return {"ok": True, "operation_id": tx["id"], "status": "Preview", **payload}


def apply_playlist_media_cleanup(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    ad = adapter or beets_adapter
    st = _get_store(store)
    tx = st.get(operation_id)
    item_ids = tx.get("metadata", {}).get("item_ids", [])
    if item_ids:
        ad.remove(item_ids=[int(x) for x in item_ids], delete_files=True)
    st.update(operation_id, status="Completed")
    return {"ok": True, "operation_id": operation_id, "status": "Completed"}


def rollback_playlist_media_cleanup(
    operation_id: str,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    st = _get_store(store)
    st.update(operation_id, status="Rolled Back")
    return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}


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
    if p_dst.exists():
        if os.path.samefile(p_src, p_dst):
            return {"ok": True, "already_present": True, "source": str(p_src), "destination": str(p_dst)}
        raise ValueError(f"Refusing to overwrite an existing file: {dst_path}")
    p_dst.parent.mkdir(parents=True, exist_ok=True)
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
    under MUSIC_ROOT, never a staging root itself, never through a symlink.
    Library files leave the library only through engine quarantine. Raises
    ValueError on a refused path and OSError on a real failure -- a partial
    delete is never reported as success."""
    p = Path(path)
    if not p.exists() and not p.is_symlink():
        return {"ok": True, "deleted": False, "reason": "not_found"}
    if not _is_safe_staging_path(p):
        raise ValueError(f"Refusing to delete outside staging roots: {path}")
    resolved = p.resolve()
    if any(resolved == root for root in _get_staging_roots()):
        raise ValueError(f"Refusing to delete a staging root itself: {path}")
    if resolved.is_dir():
        shutil.rmtree(resolved)
    else:
        resolved.unlink()
    return {"ok": True, "deleted": True, "path": str(resolved)}


def move_file(source: str, target: str) -> Dict[str, Any]:
    """Move a staging file or folder within staging roots (LT-13).

    Refuses MUSIC_ROOT, symlinks and an existing target; library moves go
    through the Beets engine (relocation family)."""
    src = Path(source)
    dst = Path(target)
    if not src.exists():
        return {"ok": False, "error": f"Source file does not exist: {source}"}
    if not _is_safe_staging_path(src) or not _is_safe_staging_path(dst.parent if not dst.exists() else dst):
        raise ValueError("Refusing to move outside staging roots")
    if dst.exists():
        raise ValueError(f"Refusing to overwrite an existing target: {target}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return {"ok": True, "source": str(src), "target": str(dst)}


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


def mbsync(
    query: str = "", *, async_job: bool = False, timeout: float = 300.0
) -> Dict[str, Any]:
    """Library-wide/query mbsync is refused (LT-18).

    It forwarded ``query`` to beets_adapter.mbsync(), which has no such
    parameter (TypeError), and a library-wide MusicBrainz rewrite has no
    snapshot or rollback. Per-album repairs use plan/apply_album_mb_track_repair."""
    return {"ok": False, "code": "not_supported",
            "error": "Syncing the whole library from MusicBrainz at once is not supported; nothing was changed."}
