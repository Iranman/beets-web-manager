"""Pending Import Review queue store: load, add, resolve and remove queued folders (ARCH-001).

Shared by library, AI, import and review services; the queue file is
web-manager state, never Beets library state.
"""

from __future__ import annotations

import json, os, threading, time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from backend.app_runtime import AUDIO_EXT, DOWNLOADS_ROOT, MUSIC_ROOT, WEB_MANAGER_DATA_DIR, _app_logger, _path_is_under, _s
from backend.ai_batch_state_service import _get_ai_batch_store
from backend.import_reconciliation_service import _reconcile_pending_review_enqueue_item
from backend.beets_adapter import BeetsUnavailableError
from backend.beets_plugins import _atomic_write_text
import backend.composite_workflows as composite_workflows
from backend.serializers import REVIEW_ORIGIN_FIELDS, REVIEW_ORIGIN_LABELS, _normalize_review_origin_type, _review_origin_payload
import hashlib
from backend.serializers import _resolve_import_review_source_path
import backend.serializers as _serializers
from backend.ai_batch_state_service import _music_format_policy_review_note
from backend.import_reconciliation_service import _MUSIC_FORMAT_POLICY_REVIEW_STATUS, _remaining_audio_files, _review_paths_equal, _review_status_key
import backend.recovery_carrier as recovery_carrier

# ── ARCH-001 extracted code ──


def _recovery_carrier_review_identity(carrier: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Flatten a recovery carrier into the structured identity the Review row
    keeps. Best-effort: a store/shape error never breaks queueing."""
    try:
        return recovery_carrier.review_identity(carrier)
    except Exception:
        return {}


def _refresh_recovery_stage(evidence: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return a copy of a Review item's evidence with the ``recovery_carrier``
    packet recomputed FRESH from the live carrier (QA Finding 7 + Step 2).

    The packet stored at queue time is a snapshot (taken by the non-auto
    hand-off BEFORE the carrier advances / before the advancer computes the
    matching contract), so the queue can show a stale stage AND a stale
    confidence. Recomputing the whole ``review_identity`` on read reflects the
    carrier's current state+checkpoint, its per-track download/verification
    status, provenance and -- the Step-2 reconciliation -- the single
    contract-derived confidence + matching_decision. A missing/unreadable
    carrier keeps the stored snapshot unchanged -- never crash, never fabricate."""
    if not isinstance(evidence, dict):
        return evidence or {}
    rc = evidence.get("recovery_carrier")
    carrier_id = _s(rc.get("recovery_carrier_id")).strip() if isinstance(rc, dict) else ""
    if not carrier_id:
        return evidence
    try:
        carrier = recovery_carrier.get_carrier(carrier_id)
        if not carrier:
            return evidence  # carrier gone: keep the stored snapshot
        fresh_packet = recovery_carrier.review_identity(carrier)
    except Exception:
        return evidence  # unreadable store: keep the stored snapshot
    evidence = dict(evidence)
    # Merge the fresh packet over the stored snapshot so any queue-time-only
    # keys are preserved while stage/confidence/tracks/matching_decision refresh.
    evidence["recovery_carrier"] = {**rc, **fresh_packet}
    return evidence


def _queue_folder_for_manual_review(folder_path: str, suggestion: Optional[dict],
                                    reason: str, log: Optional[list] = None,
                                    allow_existing: bool = False,
                                    evidence: Optional[Dict[str, Any]] = None,
                                    origin: Optional[Any] = None,
                                    carrier: Optional[Dict[str, Any]] = None) -> bool:
    """Put a folder back in Import Review without failing the current job.

    ``carrier`` (Phase-2 missing-track recovery spine): when a recovery carrier
    exists for this folder, the Review row keeps the STRUCTURED album identity
    (existing_album_id, canonical mb_releasegroupid, the wanted/expected tracks,
    destination) instead of only the old `allow_existing` boolean + free-text
    suggestion. A carrier that names an existing album implies allow_existing,
    because the staged folder lives outside MUSIC_ROOT where
    ``_library_album_ids_for_folder`` cannot recover the linkage."""
    recovery = _recovery_carrier_review_identity(carrier)
    if recovery.get("existing_album_id"):
        allow_existing = True
    try:
        existing_ids = _library_album_ids_for_folder(folder_path)
    except Exception as ex:  # engine unavailable: never replace the caller's own error
        if log is not None:
            log.append(f"  WARN could not queue manual Review item: {ex}")
        return False
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
    if recovery:
        # Structured recovery identity survives the hand-off; the Review UI
        # (frontend) and preflight single-track mode (music-identity) read it.
        sug["recovery"] = True
        sug["recovery_carrier_id"] = recovery.get("recovery_carrier_id", "")
        sug["recovery_existing_album_id"] = recovery.get("existing_album_id", 0)
        sug["recovery_mb_releasegroupid"] = recovery.get("mb_releasegroupid", "")
        sug["recovery_release_id"] = recovery.get("release_id", "")
        sug["recovery_destination_path"] = recovery.get("destination_album_path", "")
        sug["recovery_wanted_tracks"] = recovery.get("wanted_tracks", [])
        sug["recovery_expected_tracks"] = recovery.get("expected_tracks", [])
    if allow_existing:
        sug["allow_existing_review"] = True
        sug["existing_album_ids"] = existing_ids or (
            [recovery["existing_album_id"]] if recovery.get("existing_album_id") else []
        )
    if sug.get("mb_albumid"):
        sug["rejected_mb_albumid"] = sug.get("mb_albumid")
    sug["mb_albumid"] = ""
    sug.pop("mb_url", None)
    # Force the user to verify the release instead of treating the bad match as importable.
    sug["mb_valid"] = False
    evidence = evidence or sug.get("review_evidence")
    if recovery:
        # Also surface the recovery identity inside the evidence packet so a
        # consumer that reads evidence (not the suggestion) still sees it.
        evidence = dict(evidence or {})
        evidence["recovery_carrier"] = recovery
    if evidence:
        sug["review_evidence"] = evidence
    try:
        _add_to_pending(folder_path, sug, allow_existing=allow_existing, evidence=evidence, origin=origin)
        if log is not None:
            kept = sug.get("kept_album_ids")
            log.append(
                f"  Queued folder for manual Review; album_id {', '.join(str(i) for i in kept)} "
                "imported by Beets was left in the library." if kept else
                "  Queued folder for manual Review; no library files were changed.")
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


_NO_AUDIO_NOT_RESOLVED = frozenset({"symlinked_audio", "unverifiable_folder"})


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
                             existing_album_ids: Optional[Iterable[Any]] = None,
                             *, no_audio_resolves: bool = True) -> bool:
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

    if no_audio_resolves and raw and not _review_folder_has_audio(raw):
        return True
    return False


def _pending_review_item_is_resolved(item: Dict[str, Any]) -> bool:
    suggestion = (item or {}).get("suggestion") or {}
    existing_ids = suggestion.get("existing_album_ids") or []
    path = _s((item or {}).get("path", ""))
    # A folder refused for linked or unreadable content has no audio pathlib
    # can see (links are never followed); while it exists, "no audio" does
    # not resolve its row (QA401-F1).
    refused = _s(((item or {}).get("evidence") or {}).get("conflict")) in _NO_AUDIO_NOT_RESOLVED
    keep = refused and bool(path.strip()) and os.path.lexists(path.strip())
    return _review_path_is_resolved(path, existing_ids, no_audio_resolves=not keep)


def _review_download_folder_is_gone(folder_path: str) -> bool:
    """A queued downloads-side folder that no longer exists at all.

    Same outcome as the full prune (a missing folder has no audio, and the
    #401 symlinked_audio/unverifiable_folder rows are only kept while their
    path exists), but local and cheap, so the lightweight queue listing also
    drops it. Limited to DOWNLOADS_ROOT, which this process can see; a missing
    downloads mount never prunes anything."""
    raw = _s(folder_path).strip()
    try:
        # Path.is_dir() re-raises EACCES/EIO/ESTALE (stale mount); the caller
        # would turn that into an empty queue and the next save would write it.
        root_ok = DOWNLOADS_ROOT.is_dir()
    except OSError:
        return False
    if not raw or not root_ok or not _path_is_under(Path(raw), DOWNLOADS_ROOT):
        return False
    # Only a definite "not there" counts; os.path.lexists also says False on
    # EACCES/EIO, which must keep the row (as the full prune does).
    try:
        os.lstat(raw)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False


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


# Re-entrant: writers hold it around _load_pending_reviews, which also takes it
# so a list reader's prune save can never write a stale snapshot (QA #410 F1).
_ai_pending_lock = threading.RLock()


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


class PendingReviewLoadError(RuntimeError):
    """The saved Review Queue could not be read (SEC-006). A load-modify-write
    caller must not save: the list it holds is not the queue."""


def _save_pending_reviews(items: list) -> None:
    """Atomic: a torn write can never leave a half-written queue (SEC-006)."""
    _AI_PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(_AI_PENDING_FILE, json.dumps(items, indent=2))


def _read_pending_snapshot() -> list:
    """Best-effort read of the queue file for the lock-free resolution pass
    (ARCH-033 N3). Errors become an empty snapshot; the authoritative read and
    its strict error handling happen under the lock in the locked loader."""
    try:
        items = json.loads(_AI_PENDING_FILE.read_text())
    except Exception:
        return []
    return items if isinstance(items, list) else []


def _review_resolution_sig(item: Dict[str, Any]) -> tuple:
    """The row fields the resolved/not-resolved decision depends on, so the
    locked pass can tell whether a row still matches the snapshot the decision
    was computed from and never drop a row a concurrent writer changed under it
    (ARCH-033 N3)."""
    suggestion = (item or {}).get("suggestion") or {}
    evidence = (item or {}).get("evidence") or {}
    return (
        bool(suggestion.get("allow_existing_review")),
        tuple(suggestion.get("existing_album_ids") or []),
        _s(evidence.get("conflict")),
    )


def _compute_prune_resolutions(snapshot: list) -> Dict[str, tuple]:
    """ARCH-033 N3: the Beets-backed "is this row resolved?" checks, computed
    WITHOUT the queue lock. Returns ``{path: (resolved, signature)}``. A row the
    checks could not evaluate (Beets unavailable) is simply absent, so the
    locked pass keeps it unpruned rather than waiting out Beets per row
    (SEC-006). No Beets HTTP call happens while ``_ai_pending_lock`` is held on
    the list-only load path, because this runs before the lock is taken."""
    resolutions: Dict[str, tuple] = {}
    for item in snapshot:
        item = item if isinstance(item, dict) else {}
        path = _s(item.get("path", ""))
        if not path:
            continue
        suggestion = item.get("suggestion") or {}
        try:
            resolved = bool(
                (_library_album_ids_for_folder(path)
                 and not suggestion.get("allow_existing_review"))
                or _pending_review_item_is_resolved(item))
        except BeetsUnavailableError:
            # Unknown is not resolved: stop here so every still-unchecked row
            # is kept unpruned by the locked pass (SEC-006).
            break
        resolutions[path] = (resolved, _review_resolution_sig(item))
    return resolutions


def _load_pending_reviews(*, prune_resolved: bool = True, strict: bool = False) -> list:
    # ARCH-033 N3: the Beets-backed resolution checks run BEFORE the lock, so a
    # slow Beets never serializes queue readers/writers and no Beets HTTP call
    # happens while _ai_pending_lock is held on this path. The locked pass
    # re-reads the queue and applies the precomputed decisions.
    resolutions: Dict[str, tuple] = {}
    if prune_resolved:
        resolutions = _compute_prune_resolutions(_read_pending_snapshot())
    with _ai_pending_lock:
        return _load_pending_reviews_locked(
            resolutions=resolutions, prune_resolved=prune_resolved, strict=strict)


def _load_pending_reviews_locked(*, resolutions: Dict[str, tuple], prune_resolved: bool, strict: bool) -> list:
    """The queue, pruned of resolved rows. A failed read returns [] for
    list-only callers; ``strict=True`` raises PendingReviewLoadError instead,
    so a caller that saves never writes a failed load over the queue.

    ``resolutions`` carries the Beets-backed resolution decisions computed
    outside the lock (ARCH-033 N3); this pass makes no Beets call itself."""
    try:
        try:
            items = json.loads(_AI_PENDING_FILE.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as ex:
            raise PendingReviewLoadError("Review Queue file could not be read.") from ex
        if not isinstance(items, list):
            raise PendingReviewLoadError("Review Queue file is not a list.")
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
            # An active import may have moved the folder itself; only its
            # reconciliation (above) decides that row.
            if (_review_status_key(item.get("status")) not in {"import_enqueueing", "import_queued"}
                    and _review_download_folder_is_gone(path)):
                changed = True
                resolved_items.append(item)
                continue
            if prune_resolved:
                # ARCH-033 N3: apply the decision computed outside the lock.
                # Only drop a row whose resolution-relevant fields still match
                # the snapshot the decision was made from, so a row a writer
                # changed in the window is kept (re-checked on the next load).
                decision = resolutions.get(path)
                if decision is not None and decision[0] and decision[1] == _review_resolution_sig(item):
                    changed = True
                    resolved_items.append(item)
                    continue
            filtered.append(item)
        if changed:
            try:
                _save_pending_reviews(filtered)
            except OSError as ex:
                _app_logger.warning("Review Queue prune not saved: %s", type(ex).__name__)
                if strict:
                    # A writer would save the pruned rows without their audit records.
                    raise PendingReviewLoadError("Review Queue prune could not be saved.") from ex
                return filtered
            for item in resolved_items:
                _record_ai_review_decision(
                    "auto_resolved",
                    item.get("path", ""),
                    item.get("suggestion") or {},
                    item.get("evidence") or {},
                    note="pruned from review queue after current library/source check",
                )
        return filtered
    except Exception as ex:
        _app_logger.warning("Review Queue load failed: %s", type(ex).__name__)
        if strict:
            if isinstance(ex, PendingReviewLoadError):
                raise
            raise PendingReviewLoadError("Review Queue could not be loaded.") from ex
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
        # ARCH-033 N3: no Beets call while the queue lock is held. This writer
        # already made its own library check above (outside the lock); pruning
        # other rows here would reintroduce a Beets call under the lock, and
        # the next list-only load prunes them anyway.
        items = _load_pending_reviews(prune_resolved=False, strict=True)
        updated = False
        for item in items:
            try:
                # Lexical compare only: no filesystem access on a request path.
                _same_path = (os.path.normpath(os.path.abspath(_s(item.get("path", ""))))
                              == os.path.normpath(os.path.abspath(_s(folder_path))))
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
        _save_pending_reviews(items)
    _record_ai_review_decision(
        "queued",
        folder_path,
        suggestion,
        evidence,
        note="updated pending review" if updated else "new pending review",
    )
    return True


# Folders Beets skipped during a Web Manager import. /webmanager/import runs
# Beets' importer without an import log, so beet.log never names them. The
# ledger lives here, beside the Pending Review store, so the one "folder is
# done" hook (_remove_pending_review_for_path) can drop a folder from both.
_IMPORT_SKIPPED_FILE = Path(os.environ["WEB_MANAGER_DATA_DIR"]) / "import_skipped.json"


_import_skipped_lock = threading.Lock()


def _load_recorded_import_skips() -> List[Dict[str, Any]]:
    try:
        rows = json.loads(_IMPORT_SKIPPED_FILE.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("path"), str) and r["path"].strip()]


def _write_recorded_import_skips(rows: List[Dict[str, Any]]) -> None:
    """Atomic write; the caller holds _import_skipped_lock."""
    _IMPORT_SKIPPED_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _IMPORT_SKIPPED_FILE.with_name(_IMPORT_SKIPPED_FILE.name + ".tmp")
    tmp.write_text(json.dumps(rows, indent=2))
    os.replace(tmp, _IMPORT_SKIPPED_FILE)


def _record_import_skips(source: str, skipped: List[Dict[str, Any]]) -> None:
    """Replace the recorded skips under ``source`` with this import's skips:
    one entry per folder, so a re-import never duplicates one and a folder
    that has since imported drops out."""
    src = _s(source)
    now = int(time.time())
    fresh: Dict[str, Dict[str, Any]] = {}
    for r in skipped:
        path = _s(r.get("path"))
        if path:
            fresh[path] = {"path": path, "reason": _s(r.get("reason")) or "not_matched",
                           "source": src, "skipped_at": now}
    with _import_skipped_lock:
        kept = [r for r in _load_recorded_import_skips()
                if r["path"] not in fresh and not Path(r["path"]).is_relative_to(src)]
        try:
            _write_recorded_import_skips((list(fresh.values()) + kept)[:1000])
        except OSError as ex:
            raise RuntimeError(f"Could not record skipped import folders ({type(ex).__name__}).") from ex


def _forget_import_skip(folder_path: str) -> bool:
    """Drop ``folder_path`` (and anything under it) from the skip ledger once
    the folder is imported or resolved (QA #391 N1). File-only, no Beets
    read; never raises, so it cannot fail the import that called it."""
    raw = _s(folder_path).strip()
    if not raw:
        return False
    target = Path(os.path.normpath(raw))
    try:
        with _import_skipped_lock:
            rows = _load_recorded_import_skips()
            kept = [r for r in rows if not Path(os.path.normpath(r["path"])).is_relative_to(target)]
            if len(kept) == len(rows):
                return False
            _write_recorded_import_skips(kept)
        return True
    except Exception as ex:  # QA N1: never fail the import that called it
        _app_logger.warning("could not update the import skip ledger: %s", type(ex).__name__)
        return False


def _remove_pending_review_for_path(folder_path: str, log: Optional[List[str]] = None,
                                    *, decision_note: str = "removed after import or repair",
                                    log_note: str = "repaired/imported folder") -> bool:
    raw = _s(folder_path).strip()
    if not raw:
        return False
    _forget_import_skip(raw)

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
            _save_pending_reviews(kept)
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
            # Type name only: the cleanup route returns this log (CodeQL #1425 class).
            log.append(f"  Pending Review cleanup warning: {type(ex).__name__}")
    return False


def _pending_review_has_path(folder_path: str, *, unknown: bool = False) -> bool:
    """True when a Review row points at ``folder_path``. When the queue
    cannot be read, returns ``unknown``: a caller about to delete the folder
    passes True so it keeps it (fail closed, #427)."""
    key = _pending_review_path_key(folder_path)
    if not key:
        return False
    try:
        # Strict load only for fail-closed callers; others keep the lenient
        # load (rows still visible when saving the pruned queue fails).
        return key in _pending_review_path_set(_load_pending_reviews(strict=unknown))
    except Exception:
        return unknown


def _pending_review_has_path_or_overlap(folder_path: str, *, unknown: bool = False) -> bool:
    """Like ``_pending_review_has_path`` but also True when a Review row points
    at an ANCESTOR or DESCENDANT of ``folder_path``, not only an exact match.

    A caller about to DELETE ``folder_path`` (a staging-folder cleanup, or the
    orphan-reconcile sweep over top-level staging folders) must keep it when any
    queued Review download lives INSIDE it -- a row at ``<folder>/CD 02`` while
    the sweep scans and would delete ``<folder>`` -- or CONTAINS it. Deleting
    either loses a not-yet-imported download and/or orphans a Review row (#427,
    ARCH-030 reconcile-overlap). Fail closed: an unreadable queue returns
    ``unknown``, exactly like ``_pending_review_has_path``."""
    key = _pending_review_path_key(folder_path)
    if not key:
        return False
    try:
        rows = _pending_review_path_set(_load_pending_reviews(strict=unknown))
    except Exception:
        return unknown
    # Keys are resolved, forward-slashed and rstripped of "/", so a path
    # boundary is exactly a "/" separator -- no false prefix match between
    # sibling names like ".../album" and ".../album-2".
    return any(
        row == key or row.startswith(key + "/") or key.startswith(row + "/")
        for row in rows
    )


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


def _trusted_stored_review_path(raw: Any) -> Optional[str]:
    """A stored Pending Review path, normalized, when it is still inside an
    approved Import Review root (or the library), is not a root itself, and
    no symlink moves it outside every approved root or into the music
    library from outside it; else None. The path is taken
    literally (never URL-decoded, #387), so folder names with ``%``, ``+``
    or backslashes keep working. Entries stored before POST /api/ai-pending-review
    validated paths are re-checked here wherever an entry authorizes an
    action, with no raw-text shortcut (security LOW-4)."""
    text = _s(raw).strip()
    if not text or chr(0) in text or not os.path.isabs(text):
        return None
    lexical = os.path.normpath(text)
    try:
        real = os.path.realpath(lexical)
    except (OSError, ValueError):
        return None
    roots = [os.path.normpath(str(r)) for r in _serializers._import_review_cleanup_roots(allow_music=True)]

    def _strictly_under(path: str, bases: List[str]) -> bool:
        return any(path.startswith(b.rstrip(os.sep) + os.sep) for b in bases)

    if lexical in roots or not _strictly_under(lexical, roots):
        return None
    # A symlink may move the folder to another approved root (#392 keeps that
    # working); it must never move it outside every root or onto one.
    real_roots = [os.path.realpath(r) for r in roots]
    if real in real_roots or not _strictly_under(real, real_roots):
        return None
    # Security LOW-5: never into the music library from outside it.
    music = os.path.normpath(str(MUSIC_ROOT))
    into_music = _strictly_under(real, [os.path.realpath(music)]) and not _strictly_under(lexical, [music])
    return None if into_music else lexical


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
        target = _trusted_stored_review_path(folder_path)
        return target is not None and any(
            _trusted_stored_review_path(_s((item or {}).get("path", ""))) == target for item in items)
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
                _save_pending_reviews(items)
            return changed
    except Exception:
        return False
