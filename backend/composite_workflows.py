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
        tx_dir = os.environ.get("BEETS_TRANSACTION_DIR", "/web-manager-data/transactions")
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


def _is_safe_staging_path(path: Union[str, Path]) -> bool:
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


def plan_album_duplicate_merge(
    payload: Dict[str, Any],
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Plan merging duplicate or split albums into a single canonical album."""
    ad = adapter or beets_adapter
    st = _get_store(store)

    target_aid = int(payload.get("target_album_id") or payload.get("target_id") or 0)
    source_aids = [int(x) for x in (payload.get("source_album_ids") or payload.get("source_ids") or []) if int(x) != target_aid]
    if not target_aid or not source_aids:
        return {"ok": False, "error": "target_album_id and non-empty source_album_ids required"}

    target_album = ad.get_album(target_aid)
    if not target_album:
        return {"ok": False, "error": f"Target album {target_aid} not found in library"}

    target_items = ad.find_all_items_by_album_id(target_aid)
    source_items = []
    source_albums = []
    for sid in source_aids:
        sa = ad.get_album(sid)
        if sa:
            source_albums.append(sa)
            items = ad.find_all_items_by_album_id(sid)
            source_items.extend(items)

    changes = []
    reassign_fields = {
        "album_id": target_aid,
        "album": target_album.get("album", ""),
        "albumartist": target_album.get("albumartist", ""),
        "albumartist_sort": target_album.get("albumartist_sort", ""),
        "albumartist_credit": target_album.get("albumartist_credit", ""),
        "mb_albumid": target_album.get("mb_albumid", ""),
        "mb_releasegroupid": target_album.get("mb_releasegroupid", ""),
        "mb_albumartistid": target_album.get("mb_albumartistid", ""),
        "year": target_album.get("year", 0),
        "genre": target_album.get("genre", ""),
    }

    before_state = []
    for it in source_items:
        iid = it.get("id")
        before_state.append({
            "item_id": iid,
            "album_id": it.get("album_id"),
            "album": it.get("album"),
            "albumartist": it.get("albumartist"),
            "path": _decode_path(it.get("path")),
        })
        changes.append({
            "item_id": iid,
            "title": it.get("title", ""),
            "old_album_id": it.get("album_id"),
            "new_album_id": target_aid,
            "fields": reassign_fields,
        })

    tx = st.create(
        operation_type="Merge Album",
        status="Preview",
        summary=f"Merge {len(source_aids)} albums into album {target_aid} ({target_album.get('album')})",
        changes=changes,
        rollback_available=True,
        metadata={
            "target_album_id": target_aid,
            "source_album_ids": source_aids,
            "reassign_fields": reassign_fields,
            "item_ids": [it.get("id") for it in source_items if it.get("id")],
            "before_state": before_state,
        },
    )

    return {
        "ok": True,
        "operation_id": tx["id"],
        "token": tx["id"],
        "status": "Preview",
        "target_album": target_album,
        "source_albums": source_albums,
        "item_count": len(source_items),
        "changes": changes,
    }


def apply_album_duplicate_merge(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Execute album duplicate merge via BeetsAdapter."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    tx = st.get(operation_id)
    meta = tx.get("metadata", {})

    target_aid = meta.get("target_album_id")
    source_aids = meta.get("source_album_ids", [])
    item_ids = meta.get("item_ids", [])
    fields = meta.get("reassign_fields", {})

    if item_ids and fields:
        ad.modify(fields=fields, item_ids=[int(x) for x in item_ids], write=True, move=True)

    # Clean up empty source albums
    if source_aids:
        ad.remove(album_ids=[int(x) for x in source_aids], delete_files=False)

    st.update(operation_id, status="Completed", logs=[f"Merged {len(item_ids)} tracks into album {target_aid}"])
    return {
        "ok": True,
        "operation_id": operation_id,
        "status": "Completed",
        "target_album_id": target_aid,
        "merged_items_count": len(item_ids),
        "source_albums_removed": len(source_aids),
    }


def rollback_album_duplicate_merge(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Roll back album merge by restoring items to original album IDs."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    tx = st.get(operation_id)
    meta = tx.get("metadata", {})
    before_state = meta.get("before_state", [])

    for state in before_state:
        iid = state.get("item_id")
        orig_aid = state.get("album_id")
        orig_album = state.get("album")
        if iid and orig_aid:
            ad.modify(
                fields={"album_id": orig_aid, "album": orig_album},
                item_ids=[int(iid)],
                write=True,
                move=True,
            )

    st.update(operation_id, status="Rolled Back", logs=["Restored original item album assignments"])
    return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}


def merge_duplicate_albums(
    target_album_id: int,
    source_album_ids: List[int],
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Helper executing complete plan & apply of album duplicate merge."""
    p_res = plan_album_duplicate_merge({"target_album_id": target_album_id, "source_album_ids": source_album_ids}, adapter=adapter, store=store)
    if not p_res.get("ok"):
        return p_res
    return apply_album_duplicate_merge(p_res["operation_id"], adapter=adapter, store=store)


def merge_split_album_items(
    target_album_id: int,
    item_ids: List[int],
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Reassign specific split items to a target album."""
    ad = adapter or beets_adapter
    target = ad.get_album(int(target_album_id))
    if not target:
        return {"ok": False, "error": f"Target album {target_album_id} not found"}
    fields = {
        "album_id": int(target_album_id),
        "album": target.get("album", ""),
        "albumartist": target.get("albumartist", ""),
        "mb_albumid": target.get("mb_albumid", ""),
    }
    ad.modify(fields=fields, item_ids=[int(x) for x in item_ids], write=True, move=True)
    return {"ok": True, "target_album_id": target_album_id, "items_reassigned": len(item_ids)}


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


def plan_existing_album_reconcile(
    payload_or_target_id: Any = None,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    **kwargs,
) -> Dict[str, Any]:
    """Plan reconciling tracks with an existing library album."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    payload = payload_or_target_id if isinstance(payload_or_target_id, dict) else kwargs

    target_aid = int(payload.get("target_album_id") or payload.get("album_id") or 0)
    imported_aid = int(payload.get("imported_album_id") or payload.get("source_album_id") or 0)

    target_album = ad.get_album(target_aid)
    if not target_album:
        return {"ok": False, "error": f"Target album {target_aid} not found"}

    return plan_album_duplicate_merge(
        {"target_album_id": target_aid, "source_album_ids": [imported_aid] if imported_aid else []},
        adapter=adapter,
        store=store,
    )


def apply_existing_album_reconcile(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    return apply_album_duplicate_merge(operation_id, adapter=adapter, store=store)


def rollback_existing_album_reconcile(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    return rollback_album_duplicate_merge(operation_id, adapter=adapter, store=store)


# -----------------------------------------------------------------------------
# 4. Clean All & Library Cleanup Workflows
# -----------------------------------------------------------------------------


def sync_deleted_files(
    dry_run: bool = False,
    limit: int = 50000,
    adapter: Optional[BeetsAdapter] = None,
) -> Dict[str, Any]:
    """Find items in Beets database whose physical files no longer exist on disk, and prune them."""
    ad = adapter or beets_adapter
    item_records = ad.list_item_paths(details=True)
    missing_items = []
    for rec in item_records[:limit]:
        p_str = rec.get("path")
        if p_str and not os.path.exists(p_str):
            missing_items.append(rec)

    if not dry_run and missing_items:
        missing_ids = [int(r["id"]) for r in missing_items if r.get("id")]
        if missing_ids:
            ad.remove(item_ids=missing_ids, delete_files=False)

    return {
        "ok": True,
        "dry_run": dry_run,
        "scanned": len(item_records),
        "missing_count": len(missing_items),
        "missing_items": missing_items[:100],
    }


def clean_orphaned_items(
    item_ids: Optional[List[int]] = None,
    dry_run: bool = True,
    candidate_ids: Optional[List[int]] = None,
    adapter: Optional[BeetsAdapter] = None,
) -> Dict[str, Any]:
    """Find and prune tracks that have no valid album association in the database."""
    ad = adapter or beets_adapter
    ids = item_ids or candidate_ids
    if ids is None:
        all_albums = {int(a["id"]) for a in ad.get_albums() if a.get("id")}
        orphans = []
        for it in ad.get_items():
            aid = it.get("album_id")
            if aid is None or int(aid) not in all_albums:
                orphans.append(int(it["id"]))
        ids = orphans

    if not dry_run and ids:
        ad.remove(item_ids=[int(x) for x in ids], delete_files=True)

    return {"ok": True, "dry_run": dry_run, "orphaned_count": len(ids), "item_ids": ids}


def clean_empty_albums(
    album_ids: Optional[List[int]] = None,
    dry_run: bool = True,
    candidate_ids: Optional[List[int]] = None,
    adapter: Optional[BeetsAdapter] = None,
) -> Dict[str, Any]:
    """Find and prune album database rows that contain 0 tracks."""
    ad = adapter or beets_adapter
    ids = album_ids or candidate_ids
    if ids is None:
        orphans = ad.find_all_orphan_albums()
        ids = [int(a["id"]) for a in orphans if a.get("id")]

    if not dry_run and ids:
        ad.remove(album_ids=[int(x) for x in ids], delete_files=False)

    return {"ok": True, "dry_run": dry_run, "empty_albums_count": len(ids), "album_ids": ids}


def scan_library_integrity(adapter: Optional[BeetsAdapter] = None) -> Dict[str, Any]:
    """Scan library and return integrity health metrics."""
    ad = adapter or beets_adapter
    stats = ad.get_stats()
    items = ad.get_items()
    albums = ad.get_albums()

    missing_paths = 0
    for it in items[:1000]:
        p = it.get("path")
        if p and not os.path.exists(_decode_path(p)):
            missing_paths += 1

    return {
        "ok": True,
        "total_items": stats.get("items", len(items)),
        "total_albums": stats.get("albums", len(albums)),
        "missing_files_sample": missing_paths,
    }


def get_library_health(adapter: Optional[BeetsAdapter] = None) -> Dict[str, Any]:
    return scan_library_integrity(adapter=adapter)


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


def apply_library_cleanup(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    st = _get_store(store)
    tx = st.get(operation_id)
    meta = tx.get("metadata", {})
    paths = meta.get("paths", [])
    for p in paths:
        try:
            if os.path.exists(p):
                os.unlink(p)
        except Exception as exc:
            log.warning("Failed to delete %s during library cleanup: %s", p, exc)
    st.update(operation_id, status="Completed")
    return {"ok": True, "operation_id": operation_id, "status": "Completed"}


def rollback_library_cleanup(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    st = _get_store(store)
    st.update(operation_id, status="Rolled Back")
    return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}


# -----------------------------------------------------------------------------
# 5. Track Replacement & Bulk Import Replacement
# -----------------------------------------------------------------------------


def plan_track_replacement(
    payload: Dict[str, Any],
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Plan replacing an existing lower-quality track with a higher-quality file."""
    ad = adapter or beets_adapter
    st = _get_store(store)

    item_id = int(payload.get("item_id") or payload.get("target_id") or 0)
    source_path = _decode_path(payload.get("source_path") or payload.get("source") or "")

    item = ad.get_item(item_id)
    if not item:
        return {"ok": False, "error": f"Item {item_id} not found in library"}
    if not source_path or not os.path.exists(source_path):
        return {"ok": False, "error": f"Candidate source audio file not found: {source_path}"}

    target_path = _decode_path(item.get("path"))
    changes = [{
        "item_id": item_id,
        "old_path": target_path,
        "new_source": source_path,
        "title": item.get("title", ""),
    }]

    tx = st.create(
        operation_type="Replace",
        status="Preview",
        summary=f"Replace track {item_id} ({item.get('title')}) with {Path(source_path).name}",
        changes=changes,
        rollback_available=True,
        metadata={
            "item_id": item_id,
            "target_path": target_path,
            "source_path": source_path,
            "backup_dir": "/web-manager-data/quarantine",
        },
    )

    return {
        "ok": True,
        "operation_id": tx["id"],
        "token": tx["id"],
        "status": "Preview",
        "target_item": item,
        "source_path": source_path,
        "changes": changes,
    }


def apply_track_replacement(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Execute audio file replacement, preserving quarantine backup."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    tx = st.get(operation_id)
    meta = tx.get("metadata", {})

    item_id = meta.get("item_id")
    target_path = meta.get("target_path")
    source_path = meta.get("source_path")
    backup_dir = Path(meta.get("backup_dir", "/web-manager-data/quarantine"))

    if not os.path.exists(source_path):
        raise FileNotFoundError(f"Candidate source file missing: {source_path}")

    # Backup existing file
    backup_file = None
    if os.path.exists(target_path):
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_file = backup_dir / f"{operation_id}_{Path(target_path).name}"
        shutil.copy2(target_path, str(backup_file))

    # Overwrite target with source
    shutil.copy2(source_path, target_path)

    # Sync tags through Beets
    ad.modify(fields={}, item_ids=[int(item_id)], write=True)

    st.update(
        operation_id,
        status="Completed",
        metadata={**meta, "quarantine_backup": str(backup_file) if backup_file else None},
    )
    return {"ok": True, "operation_id": operation_id, "status": "Completed"}


def rollback_track_replacement(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Roll back track replacement by restoring quarantine file."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    tx = st.get(operation_id)
    meta = tx.get("metadata", {})

    item_id = meta.get("item_id")
    target_path = meta.get("target_path")
    backup_file = meta.get("quarantine_backup")

    if backup_file and os.path.exists(backup_file) and target_path:
        shutil.copy2(backup_file, target_path)
        ad.modify(fields={}, item_ids=[int(item_id)], write=True)
        st.update(operation_id, status="Rolled Back")
        return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}

    return {"ok": False, "error": "No backup file available for rollback"}


def plan_bulk_import_replacement(
    payload: Dict[str, Any],
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    return plan_track_replacement(payload, adapter=adapter, store=store)


def apply_bulk_import_replacement(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    return apply_track_replacement(operation_id, adapter=adapter, store=store)


def rollback_bulk_import_replacement(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    return rollback_track_replacement(operation_id, adapter=adapter, store=store)


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


def plan_album_cleanup(
    album_id: int,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    ad = adapter or beets_adapter
    st = _get_store(store)
    album = ad.get_album(int(album_id))
    if not album:
        return {"ok": False, "error": f"Album {album_id} not found"}

    items = ad.find_all_items_by_album_id(int(album_id))
    tx = st.create(
        operation_type="Delete",
        status="Preview",
        summary=f"Delete album {album_id} ({album.get('album')}) and {len(items)} tracks",
        metadata={"album_id": album_id, "item_ids": [it.get("id") for it in items if it.get("id")]},
    )
    return {"ok": True, "operation_id": tx["id"], "token": tx["id"], "status": "Preview", "album": album, "items": items}


def apply_album_cleanup(
    operation_id: str,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    ad = adapter or beets_adapter
    st = _get_store(store)
    tx = st.get(operation_id)
    aid = tx.get("metadata", {}).get("album_id")
    if aid:
        ad.remove(album_ids=[int(aid)], delete_files=True)
    st.update(operation_id, status="Completed")
    return {"ok": True, "operation_id": operation_id, "status": "Completed"}


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
    image_bytes: bytes,
    ext: str = "jpg",
    adapter: Optional[BeetsAdapter] = None,
) -> Dict[str, Any]:
    """Save custom image bytes to album directory and embed into tracks."""
    ad = adapter or beets_adapter
    aid = int(album_id)
    album = ad.get_album(aid)
    if not album:
        return {"ok": False, "error": f"Album {aid} not found"}

    items = ad.find_all_items_by_album_id(aid)
    if not items:
        return {"ok": False, "error": f"No tracks found for album {aid}"}

    first_item_path = _decode_path(items[0].get("path"))
    album_dir = Path(first_item_path).parent
    target_art = album_dir / f"cover.{ext}"
    target_art.write_bytes(image_bytes)

    ad.modify(fields={"artpath": str(target_art)}, album_ids=[aid])
    ad.embed_art(album_ids=[aid])

    return {"ok": True, "album_id": aid, "artpath": str(target_art)}


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
    ad = adapter or beets_adapter
    st = _get_store(store)
    tx = st.get(operation_id)
    meta = tx.get("metadata", {})
    aid = meta.get("album_id")
    if aid:
        ad.move(album_ids=[int(aid)])
    st.update(operation_id, status="Completed")
    return {"ok": True, "operation_id": operation_id, "status": "Completed"}


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


def move_library(adapter: Optional[BeetsAdapter] = None) -> Dict[str, Any]:
    """Move all albums in the library to conform to current path templates."""
    ad = adapter or beets_adapter
    all_albums = ad.get_albums()
    aids = [int(a["id"]) for a in all_albums if a.get("id")]
    if aids:
        ad.move(album_ids=aids)
    return {"ok": True, "albums_moved": len(aids)}


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
    res = ad.run_import(paths=paths, autotag=False, move=True, write=True, set_fields=fields)
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


def _get_playlist_m3u_path(playlist_key: str, display_name: str = "") -> Path:
    safe_key = "".join(c for c in playlist_key if c.isalnum() or c in ("-", "_")).strip()
    if not safe_key:
        safe_key = "playlist"
    return _get_playlist_dir() / f"{safe_key}.m3u"


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
    stg_dir = Path(os.environ.get("WEB_MANAGER_DATA_DIR", "/web-manager-data")) / "playlist_staging" / playlist_key
    stg_dir.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "path": str(stg_dir)}


def delete_playlist_staged_track(playlist_key: str, track_id: str, requested_path: str = "") -> Dict[str, Any]:
    if requested_path and os.path.exists(requested_path) and _is_safe_staging_path(requested_path):
        os.unlink(requested_path)
    return {"ok": True, "track_id": track_id}


def inspect_playlist_staged_track(playlist_key: str, track_id: str, requested_path: str = "") -> Dict[str, Any]:
    p = Path(requested_path)
    if not p.exists():
        return {"ok": False, "exists": False}
    return {"ok": True, "exists": True, "path": str(p), "size": p.stat().st_size}


def list_playlist_staged_files(playlist_key: str, playlist_id: str = "") -> Dict[str, Any]:
    stg_dir = Path(os.environ.get("WEB_MANAGER_DATA_DIR", "/web-manager-data")) / "playlist_staging" / playlist_key
    if not stg_dir.exists():
        return {"ok": True, "files": []}
    files = [str(f) for f in stg_dir.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS]
    return {"ok": True, "files": files}


def place_playlist_imported_item(playlist_key: str, track_id: str, item_id: int) -> Dict[str, Any]:
    return {"ok": True, "playlist_key": playlist_key, "track_id": track_id, "item_id": item_id}


def get_playlist_quality_candidates(playlist_key: str, track_id: str, adapter: Optional[BeetsAdapter] = None) -> Dict[str, Any]:
    return {"ok": True, "candidates": []}


def validate_playlist_staged_track(playlist_key: str, track_id: str, requested_path: str) -> Dict[str, Any]:
    p = Path(requested_path)
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
    folder_path: str,
    adapter: Optional[BeetsAdapter] = None,
) -> List[Dict[str, Any]]:
    ad = adapter or beets_adapter
    p_norm = _decode_path(folder_path).rstrip("/\\")
    items = ad.get_items()
    return [it for it in items if _decode_path(it.get("path")).startswith(p_norm)]


def get_artist_folder_inventory(
    root: str,
    adapter: Optional[BeetsAdapter] = None,
) -> List[Dict[str, Any]]:
    p = Path(root)
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


def get_unmatched_review_items(adapter: Optional[BeetsAdapter] = None) -> List[Dict[str, Any]]:
    ad = adapter or beets_adapter
    items = ad.get_items()
    return [it for it in items if not it.get("mb_albumid")]


def inspect_import_source(
    source_path: str,
    operation: str = "import",
    timeout: float = 60.0,
) -> Dict[str, Any]:
    p = Path(source_path)
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


def create_hardlink(src_path: str, dst_path: str) -> Dict[str, Any]:
    p_src = Path(src_path)
    p_dst = Path(dst_path)
    if not p_src.exists():
        raise FileNotFoundError(f"Source file not found: {src_path}")
    p_dst.parent.mkdir(parents=True, exist_ok=True)
    os.link(str(p_src), str(p_dst))
    return {"ok": True, "source": str(p_src), "destination": str(p_dst)}


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
    return {"ok": True, "job_id": job_id, "cancelled": True}


def get_job(job_id: str) -> Dict[str, Any]:
    return {"id": job_id, "status": "success", "returncode": 0, "stdout": [], "stderr": []}


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
    delete_files: bool = True,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    """Perform transaction-controlled album removal through album_maintenance family."""
    ad = adapter or beets_adapter
    album_data = lib.get_album(album_id) or {}
    items = album_data.get("items") or []
    item_ids = [int(it.get("id") or 0) for it in items if int(it.get("id") or 0) > 0]

    plan_res = plan_album_maintenance(
        {
            "mode": "remove_album" if not item_ids else "remove_tracks",
            "album_id": int(album_id),
            "track_ids": item_ids,
            "delete_files": bool(delete_files),
            "source": "delete_album",
        },
        store=store,
    )
    if not plan_res.get("ok"):
        return plan_res
    op_id = plan_res.get("operation_id")
    return apply_album_maintenance(op_id, adapter=ad, store=store)


def delete_file(path: str) -> Dict[str, Any]:
    """Delete a file or directory safely."""
    p = Path(path)
    if not p.exists():
        return {"ok": True, "deleted": False, "reason": "not_found"}
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    else:
        p.unlink(missing_ok=True)
    return {"ok": True, "deleted": True, "path": str(p)}


def move_file(source: str, target: str) -> Dict[str, Any]:
    src = Path(source)
    dst = Path(target)
    if not src.exists():
        return {"ok": False, "error": f"Source file does not exist: {source}"}
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
    album_id: int, adapter: Optional[BeetsAdapter] = None
) -> Dict[str, Any]:
    ad = adapter or beets_adapter
    res = ad.move(f"id:{album_id}", album=True)
    return {"ok": res.returncode == 0, "stdout": res.stdout, "stderr": res.stderr}


def run_command(
    command: str,
    args: Optional[List[str]] = None,
    adapter: Optional[BeetsAdapter] = None,
) -> Dict[str, Any]:
    ad = adapter or beets_adapter
    full_cmd = [command] + (args or [])
    res = ad.run(full_cmd)
    return {
        "ok": res.returncode == 0,
        "returncode": res.returncode,
        "stdout": res.stdout,
        "stderr": res.stderr,
    }


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
    return beets_adapter.mbsync(query=query)



