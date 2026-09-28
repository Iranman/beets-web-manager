"""Pending Import Review queue store: load, add, resolve and remove queued folders (ARCH-001).

Shared by library, AI, import and review services; the queue file is
web-manager state, never Beets library state.
"""

from __future__ import annotations

import json, os, threading, time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from backend.app_runtime import AUDIO_EXT, MUSIC_ROOT, WEB_MANAGER_DATA_DIR, _s
from backend.ai_batch_state_service import _get_ai_batch_store
from backend.import_reconciliation_service import _reconcile_pending_review_enqueue_item
from backend.beets_adapter import BeetsUnavailableError
import backend.composite_workflows as composite_workflows
from backend.serializers import REVIEW_ORIGIN_FIELDS, REVIEW_ORIGIN_LABELS, _normalize_review_origin_type, _review_origin_payload
import hashlib
from backend.serializers import _resolve_import_review_source_path
from backend.ai_batch_state_service import _music_format_policy_review_note
from backend.import_reconciliation_service import _MUSIC_FORMAT_POLICY_REVIEW_STATUS, _remaining_audio_files, _review_paths_equal

# ── ARCH-001 extracted code ──


def _queue_folder_for_manual_review(folder_path: str, suggestion: Optional[dict],
                                    reason: str, log: Optional[list] = None,
                                    allow_existing: bool = False,
                                    evidence: Optional[Dict[str, Any]] = None,
                                    origin: Optional[Any] = None) -> bool:
    """Put a folder back in Import Review without failing the current job."""
    existing_ids = _library_album_ids_for_folder(folder_path)
    if existing_ids and not allow_existing:
        if log is not None:
            log.append(
                "  Skipped Pending Review: folder already belongs to "
                f"Beets album_id(s) {existing_ids}."
            )
        return False
    sug = dict(suggestion or {})
    sug["confidence"] = "low"
    sug["reason"] = reason
    if allow_existing:
        sug["allow_existing_review"] = True
        sug["existing_album_ids"] = existing_ids
    if sug.get("mb_albumid"):
        sug["rejected_mb_albumid"] = sug.get("mb_albumid")
    sug["mb_albumid"] = ""
    sug.pop("mb_url", None)
    # Force the user to verify the release instead of treating the bad match as importable.
    sug["mb_valid"] = False
    evidence = evidence or sug.get("review_evidence")
    if evidence:
        sug["review_evidence"] = evidence
    try:
        _add_to_pending(folder_path, sug, allow_existing=allow_existing, evidence=evidence, origin=origin)
        if log is not None:
            log.append("  Queued folder for manual Review; no library files were changed.")
        return True
    except Exception as ex:
        if log is not None:
            log.append(f"  WARN could not queue manual Review item: {ex}")
    return False


def _is_music_root_path(path_value: str) -> bool:
    """True when a beets path belongs to /data/media/music.
    Beets may store paths either absolute or relative to the library root.
    """
    p = _s(path_value)
    if not p:
        return False
    if not p.startswith("/"):
        return True
    try:
        Path(p).resolve(strict=False).relative_to(MUSIC_ROOT.resolve(strict=False))
        return True
    except Exception:
        root = str(MUSIC_ROOT).rstrip("/")
        return p == root or p.startswith(root + "/")


def _library_album_ids_for_folder(folder_path: str) -> List[int]:
    """Return Beets album IDs that already have items under a music-root folder."""
    raw = _s(folder_path).strip()
    if not raw or "\x00" in raw or "\\" in raw:
        return []
    try:
        res = composite_workflows.resolve_folder_to_albums(raw)
        return res.get("album_ids", [])
    except BeetsUnavailableError:
        raise
    except Exception:
        return []


def _review_album_is_resolved(album_id: int) -> bool:
    try:
        aid = int(album_id or 0)
    except Exception:
        aid = 0
    if aid <= 0:
        return False

    try:
        album_row = composite_workflows.get_album(aid)
        if not album_row:
            return True
        rows = composite_workflows.find_all_items_by_album_id(aid)
    except BeetsUnavailableError:
        raise
    except Exception:
        return False

    music_rows = [
        _s(row.get("path"))
        for row in rows
        if _is_music_root_path(_s(row.get("path")))
    ]
    if not music_rows:
        return False
    if not _s(album_row.get("mb_albumid")).strip():
        return False
    try:
        return all(composite_workflows.find_item_by_path(raw_path) is not None for raw_path in music_rows)
    except BeetsUnavailableError:
        raise
    except Exception:
        return False


def _review_folder_has_audio(folder_path: str) -> bool:
    raw = _s(folder_path).strip()
    if not raw:
        return False
    try:
        folder = Path(raw)
        if not folder.exists():
            return False
        if folder.is_file():
            return folder.suffix.lower() in AUDIO_EXT
        return any(
            path.is_file() and path.suffix.lower() in AUDIO_EXT
            for path in folder.rglob("*")
        )
    except Exception:
        return True


def _review_path_is_resolved(folder_path: str,
                             existing_album_ids: Optional[Iterable[Any]] = None) -> bool:
    raw = _s(folder_path).strip()
    album_ids = []
    for value in existing_album_ids or []:
        try:
            aid = int(value or 0)
        except Exception:
            aid = 0
        if aid > 0:
            album_ids.append(aid)
    if raw:
        album_ids.extend(_library_album_ids_for_folder(raw))

    unique_ids = sorted(set(album_ids))
    if unique_ids and all(_review_album_is_resolved(aid) for aid in unique_ids):
        return True

    if raw and not _review_folder_has_audio(raw):
        return True
    return False


def _pending_review_item_is_resolved(item: Dict[str, Any]) -> bool:
    suggestion = (item or {}).get("suggestion") or {}
    existing_ids = suggestion.get("existing_album_ids") or []
    return _review_path_is_resolved(_s((item or {}).get("path", "")), existing_ids)


def _apply_review_origin(target: Dict[str, Any], origin: Dict[str, Any]) -> None:
    for field in REVIEW_ORIGIN_FIELDS:
        value = origin.get(field)
        if value not in (None, ""):
            target[field] = value
    if "origin_type" not in target:
        target["origin_type"] = "unknown"
    if "origin_label" not in target:
        target["origin_label"] = REVIEW_ORIGIN_LABELS.get(target.get("origin_type"), "Unknown source")


# ── AI Batch Import ──────────────────────────────────────────────────────────

# Wave 26 (PR #101) Docker acceptance round: found live, not just by
# inspection -- a real AI-reviewed-import acceptance run reached the
# review-queueing path and crashed with
# "[Errno 2] No such file or directory: '/config/ai_pending_review.json'".
# /config is the Beets ENGINE's mount (BEETSDIR); beets-web-manager has no
# mount there at all in either documented compose topology (see
# WEB_MANAGER_DATA_DIR's own definition and _AI_BATCH_STATE_DIR's identical,
# already-fixed history above) -- this file was simply never reachable in
# the real, security-hardened deployment. _AI_REVIEW_DECISIONS_FILE and
# _ALBUM_MB_SUGGESTIONS_FILE are exempt from this same fix: both are now
# read-only, one-time legacy migration SOURCES for AiBatchStateStore
# (migrate_legacy_files(), called once at import time below) rather than
# live read/write paths, so leaving their default under the same
# unreachable /config root is inert, not broken -- migrate_legacy_files()
# already handles a missing source file as "nothing to migrate", not an
# error.
_AI_PENDING_FILE = Path(os.environ.get("AI_PENDING_REVIEW_FILE", str(WEB_MANAGER_DATA_DIR / "ai_pending_review.json")))


_ai_pending_lock = threading.Lock()


_ai_review_decision_lock = threading.Lock()


def _record_ai_review_decision(action: str, folder_path: str,
                               suggestion: Optional[Dict[str, Any]] = None,
                               evidence: Optional[Dict[str, Any]] = None,
                               note: str = "") -> None:
    """Append a compact review lifecycle event for later threshold tuning."""
    try:
        suggestion = suggestion or {}
        evidence = evidence or suggestion.get("review_evidence") or {}
        entry = {
            "at": int(time.time()),
            "action": action,
            "path": folder_path,
            "folder_name": Path(folder_path).name if folder_path else "",
            "mb_albumid": suggestion.get("mb_albumid") or suggestion.get("rejected_mb_albumid") or "",
            "album": suggestion.get("album", ""),
            "artist": suggestion.get("albumartist", "") or suggestion.get("artist", ""),
            "confidence": suggestion.get("confidence", ""),
            "reason": suggestion.get("reason", ""),
            "note": note,
            "evidence": evidence,
        }
        with _ai_review_decision_lock:
            try:
                _get_ai_batch_store().record_review_decision(entry)
            except Exception:
                pass
    except Exception:
        pass


def _load_pending_reviews(*, prune_resolved: bool = True) -> list:
    try:
        if _AI_PENDING_FILE.exists():
            items = json.loads(_AI_PENDING_FILE.read_text())
            if not isinstance(items, list):
                return []
            filtered = []
            resolved_items = []
            changed = False
            for item in items:
                item = item if isinstance(item, dict) else {}
                reconciled_item, reconcile_changed = _reconcile_pending_review_enqueue_item(item)
                if reconcile_changed:
                    changed = True
                if reconciled_item is None:
                    resolved_items.append(item)
                    continue
                item = reconciled_item
                path = _s((item or {}).get("path", ""))
                suggestion = (item or {}).get("suggestion") or {}
                origin_info = _review_origin_payload(
                    path,
                    suggestion,
                    item=item,
                    evidence=(item or {}).get("evidence") or {},
                )
                if not item.get("origin_type") or not suggestion.get("origin_type"):
                    suggestion = dict(suggestion or {})
                    _apply_review_origin(item, origin_info)
                    _apply_review_origin(suggestion, origin_info)
                    item["suggestion"] = suggestion
                    changed = True
                if prune_resolved:
                    if path and _library_album_ids_for_folder(path) and not suggestion.get("allow_existing_review"):
                        changed = True
                        resolved_items.append(item)
                        continue
                    if _pending_review_item_is_resolved(item or {}):
                        changed = True
                        resolved_items.append(item)
                        continue
                filtered.append(item)
            if changed:
                _AI_PENDING_FILE.write_text(json.dumps(filtered, indent=2))
                for item in resolved_items:
                    _record_ai_review_decision(
                        "auto_resolved",
                        item.get("path", ""),
                        item.get("suggestion") or {},
                        item.get("evidence") or {},
                        note="pruned from review queue after current library/source check",
                    )
            return filtered
    except Exception:
        pass
    return []


def _add_to_pending(folder_path: str, suggestion: dict, allow_existing: bool = False,
                    evidence: Optional[Dict[str, Any]] = None,
                    origin: Optional[Any] = None):
    if _library_album_ids_for_folder(folder_path) and not allow_existing:
        return False
    suggestion = dict(suggestion or {})
    evidence = evidence or suggestion.get("review_evidence")
    origin_info = _review_origin_payload(folder_path, suggestion, origin=origin, evidence=evidence)
    _apply_review_origin(suggestion, origin_info)
    if evidence:
        suggestion["review_evidence"] = evidence
    with _ai_pending_lock:
        items = _load_pending_reviews()
        updated = False
        for item in items:
            try:
                _same_path = Path(item.get("path", "")).resolve() == Path(folder_path).resolve()
            except Exception:
                _same_path = item.get("path") == folder_path
            if _same_path:
                existing_origin = _review_origin_payload(
                    folder_path,
                    item.get("suggestion") or {},
                    item=item,
                    evidence=item.get("evidence") or {},
                )
                chosen_origin = origin_info
                if (
                    _normalize_review_origin_type(chosen_origin.get("origin_type")) == "unknown"
                    and _normalize_review_origin_type(existing_origin.get("origin_type")) != "unknown"
                ):
                    chosen_origin = existing_origin
                    _apply_review_origin(suggestion, chosen_origin)
                item["folder_name"] = Path(folder_path).name
                if suggestion:
                    item["suggestion"] = suggestion
                item["added_at"] = int(time.time())
                if evidence:
                    item["evidence"] = evidence
                _apply_review_origin(item, chosen_origin)
                updated = True
                break
        if not updated:
            entry: Dict[str, Any] = {
                "path":        folder_path,
                "folder_name": Path(folder_path).name,
                "suggestion":  suggestion or {},
                "added_at":    int(time.time()),
            }
            if evidence:
                entry["evidence"] = evidence
            _apply_review_origin(entry, origin_info)
            items.append(entry)
        _AI_PENDING_FILE.write_text(json.dumps(items, indent=2))
    _record_ai_review_decision(
        "queued",
        folder_path,
        suggestion,
        evidence,
        note="updated pending review" if updated else "new pending review",
    )
    return True


def _remove_pending_review_for_path(folder_path: str, log: Optional[List[str]] = None,
                                    *, decision_note: str = "removed after import or repair",
                                    log_note: str = "repaired/imported folder") -> bool:
    raw = _s(folder_path).strip()
    if not raw:
        return False

    def _same_path(a: str, b: str) -> bool:
        if a == b:
            return True
        try:
            return str(Path(a).resolve(strict=False)) == str(Path(b).resolve(strict=False))
        except Exception:
            return a.replace("\\", "/").rstrip("/") == b.replace("\\", "/").rstrip("/")

    try:
        with _ai_pending_lock:
            if not _AI_PENDING_FILE.exists():
                return False
            items = json.loads(_AI_PENDING_FILE.read_text())
            if not isinstance(items, list):
                return False
            kept = []
            removed_items = []
            removed = 0
            for item in items:
                path = _s((item or {}).get("path", ""))
                if path and _same_path(path, raw):
                    removed += 1
                    removed_items.append(item)
                    continue
                kept.append(item)
            if not removed:
                return False
            _AI_PENDING_FILE.write_text(json.dumps(kept, indent=2))
        for item in removed_items:
            _record_ai_review_decision(
                "resolved",
                item.get("path", raw),
                item.get("suggestion") or {},
                item.get("evidence") or {},
                note=decision_note,
            )
        if log is not None:
            suffix = "y" if removed == 1 else "ies"
            log.append(f"  Removed {removed} Pending Review entr{suffix} for {log_note}.")
        return True
    except Exception as ex:
        if log is not None:
            log.append(f"  Pending Review cleanup warning: {ex}")
    return False


def _pending_review_has_path(folder_path: str) -> bool:
    key = _pending_review_path_key(folder_path)
    if not key:
        return False
    try:
        return key in _pending_review_path_set()
    except Exception:
        return False


def _pending_review_path_key(folder_path: str) -> str:
    raw = _s(folder_path).strip()
    if not raw:
        return ""
    try:
        return str(Path(raw).resolve(strict=False)).replace("\\", "/").rstrip("/").casefold()
    except Exception:
        return raw.replace("\\", "/").rstrip("/").casefold()


def _pending_review_path_set(items: Optional[List[Dict[str, Any]]] = None) -> set:
    rows = items if items is not None else (_load_pending_reviews() or [])
    return {
        key
        for key in (_pending_review_path_key(_s((item or {}).get("path", ""))) for item in rows)
        if key
    }


def _import_review_folder_signature(folder_path: str) -> str:
    """Hash of the audio file list (name/size/mtime) so a stored AI Suggest
    result can be checked for staleness against a folder that changed since
    the suggestion was generated (files added/removed/replaced)."""
    source, err = _resolve_import_review_source_path(folder_path, allow_music=True, expected_type="dir")
    if err or not source:
        return ""
    entries = []
    try:
        for p in sorted(source.rglob("*"), key=lambda x: str(x).lower()):
            if not p.is_symlink() and p.is_file() and p.suffix.lower() in AUDIO_EXT:
                st = p.stat()
                entries.append(f"{p.name}:{st.st_size}:{int(st.st_mtime)}")
    except Exception:
        return ""
    if not entries:
        return ""
    raw = "|".join(entries)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _review_id_matches_path(review_item_id: str, folder_path: str) -> bool:
    review_item_id = _s(review_item_id).strip()
    if not review_item_id:
        return True
    expected = "pending:" + _s(folder_path).strip()
    if review_item_id == expected:
        return True
    if review_item_id.startswith("pending:"):
        return _review_paths_equal(review_item_id[len("pending:"):], folder_path)
    return False


def _pending_review_matches(folder_path: str, review_item_id: str = "") -> bool:
    folder_path = _s(folder_path).strip()
    if not folder_path or not _review_id_matches_path(review_item_id, folder_path):
        return False
    try:
        if not _AI_PENDING_FILE.exists():
            return False
        items = json.loads(_AI_PENDING_FILE.read_text())
        if not isinstance(items, list):
            return False
        return any(_review_paths_equal(_s((item or {}).get("path", "")), folder_path) for item in items)
    except Exception:
        return False


def _finalize_pending_review_format_policy_rejection(folder_path: str, note: Any,
                                                     *, job_id: str = "",
                                                     idempotency_key: str = "",
                                                     log: Optional[List[str]] = None) -> Dict[str, Any]:
    status_note = _music_format_policy_review_note(note)
    remaining = _remaining_audio_files(folder_path)
    if not remaining:
        removed = _remove_pending_review_for_path(
            folder_path,
            log,
            decision_note=status_note,
            log_note="handled audio-policy rejection",
        )
        return {
            "status": "resolved",
            "pending_review_exists": False,
            "pending_review_removed": bool(removed),
            "remaining_audio_count": 0,
            "note": status_note,
        }
    _mark_pending_review_status(
        folder_path,
        _MUSIC_FORMAT_POLICY_REVIEW_STATUS,
        status_note,
        job_id=job_id,
        idempotency_key=idempotency_key,
    )
    return {
        "status": _MUSIC_FORMAT_POLICY_REVIEW_STATUS,
        "pending_review_exists": True,
        "pending_review_removed": False,
        "remaining_audio_count": len(remaining),
        "note": status_note,
    }


def _mark_pending_review_status(folder_path: str, status: str, note: str = "",
                                *, job_id: str = "", idempotency_key: str = "") -> bool:
    raw = _s(folder_path).strip()
    if not raw:
        return False

    def _same_path(a: str, b: str) -> bool:
        if a == b:
            return True
        try:
            return str(Path(a).resolve(strict=False)) == str(Path(b).resolve(strict=False))
        except Exception:
            return a.replace("\\", "/").rstrip("/") == b.replace("\\", "/").rstrip("/")

    try:
        with _ai_pending_lock:
            if not _AI_PENDING_FILE.exists():
                return False
            items = json.loads(_AI_PENDING_FILE.read_text())
            if not isinstance(items, list):
                return False
            changed = False
            for item in items:
                path = _s((item or {}).get("path", ""))
                if not path or not _same_path(path, raw):
                    continue
                item["status"] = status
                item["status_note"] = note
                item["updated_at"] = int(time.time())
                if job_id:
                    item["auto_import_job_id"] = job_id
                if idempotency_key:
                    item["auto_import_idempotency_key"] = idempotency_key
                suggestion = item.get("suggestion") if isinstance(item.get("suggestion"), dict) else {}
                if suggestion is not item.get("suggestion"):
                    item["suggestion"] = suggestion
                if note and status in {"auto_enqueue_failed", "remaining_files_review", _MUSIC_FORMAT_POLICY_REVIEW_STATUS}:
                    suggestion["reason"] = note
                changed = True
                break
            if changed:
                _AI_PENDING_FILE.write_text(json.dumps(items, indent=2))
            return changed
    except Exception:
        return False
