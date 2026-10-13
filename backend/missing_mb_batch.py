"""Batch fix for library albums and tracks missing MusicBrainz IDs.

Only proven matches are written; everything else is reported as "needs
review" and left alone. Two steps:

* **Preview** (read-only job): for each selected row, gather evidence and
  decide proven / not proven with the canonical identity gates, then record
  a ``Preview`` transaction (family ``missing_mb_ids_batch_v1``) that holds
  every row's proof, the exact IDs that would be written, an evidence
  digest and a random ``confirm_token``.
* **Apply** (mutating job): needs that token. It claims the transaction
  (compare-and-set to ``Running``), records a pre-apply identity snapshot of
  every proven row and the library counts, then for each row re-proves the
  match, skips the row when the evidence digest changed, and writes it
  through the existing per-row writer the caller injects (``Match album``
  for albums, ``attach-recording`` for tracks). Each writer opens its own
  rollback-capable transaction. The first failed or unverifiable write stops
  the batch.

What "proven" means (no new thresholds; the identity code decides):

* album: ``identify_import_folder`` on the album's folder says Stage 2b
  ``auto_import.eligible`` (exactly one Release Group verified, every file
  proven by fingerprint or embedded Recording ID, no competing group, no
  hard conflict, no symlinked audio), the folder holds exactly the album's
  items, and the chosen release has as many tracks as the album;
* track (singleton): AcoustID answered and confirms exactly one recording
  (``confirmed_recording``: score >= 80, no other within 3 points), and
  exactly one trusted candidate is ``attach_without_review`` with
  fingerprint proof for that same recording.

AI is never consulted. Layering: a workflow helper (layer 5) like
``import_identity``; routes import it, it imports no route.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import backend.composite_workflows as composite_workflows
from backend.acoustid_service import _acoustid_lookup_cached_outcome, _album_item_abs_path
from backend.app_runtime import _app_logger, _path_is_under, _redact_security_text, _s, transactions
from backend.beets_adapter import beets_adapter, lib
from backend.import_identity import _recursive_audio, identify_import_folder
from backend.library_service import _missing_mb_rows, _reconstruct_track_recording_candidates
from backend.matching.folder_identity import confirmed_recording
from backend.transaction_engine import metadata_diff

FAMILY = "missing_mb_ids_batch_v1"
MAX_BATCH_ROWS = 50
SOURCE = "Library Missing IDs batch"
_ROW_ID_RE = re.compile(r"(album|item):([1-9]\d{0,9})")
_FINGERPRINT_PROOFS = frozenset({"acoustid_recording_id", "multi_source_deterministic"})

#: Fixed, user-facing text per row outcome. Exception detail goes to the
#: transaction log only, never to the response.
ROW_MESSAGES = {
    "proven": "Proven match; will be written on apply.",
    "needs_review": "Not proven; nothing will be written. Review this row by hand.",
    "written": "Written and verified.",
    "already_set": "Already had this ID; nothing was written.",
    "evidence_changed": "The evidence changed since the preview; nothing was written. Preview again.",
    "no_longer_proven": "The match is no longer proven; nothing was written. Review this row by hand.",
    "evidence_unavailable": "Evidence could not be gathered (Beets, MusicBrainz or AcoustID unavailable); "
                            "nothing was written.",
    "write_failed": "The write failed and the batch stopped. Open the row's transaction to check or roll back "
                    "any change.",
    "recovery_required": "The write could not be verified or rolled back and the batch stopped. Check this "
                         "album or track before retrying.",
    "cancelled": "Cancelled before this row was written.",
    "not_attempted": "Not attempted because an earlier row failed.",
}


class RecoveryRequired(RuntimeError):
    """A writer committed something it could not verify or roll back."""


class NotWritten(RuntimeError):
    """A writer's own gate refused the row before writing anything."""


def _has_link_component(path: str) -> bool:
    return os.path.realpath(path) != os.path.abspath(path)


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _token_hash(token: str) -> str:
    """Only this hash is stored; the raw token lives in the preview job result."""
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()


def _uuid(value: Any) -> str:
    return _s(value).strip().lower()


def _digest(proof: Dict[str, Any]) -> str:
    """Identity-relevant evidence only, so a stable match keeps its digest."""
    keep = {k: proof.get(k) for k in ("kind", "target_id", "write")}
    keep["evidence"] = {k: v for k, v in (proof.get("evidence") or {}).items() if k != "sources"}
    return hashlib.sha256(json.dumps(keep, sort_keys=True, default=str).encode()).hexdigest()


def _proof(kind: str, target_id: int, row_id: str, reasons: List[str], write: Dict[str, str],
           evidence: Dict[str, Any], current: Dict[str, str]) -> Dict[str, Any]:
    proven = not reasons and bool(write)
    out = {
        "row_id": row_id, "kind": kind, "target_id": int(target_id), "proven": proven,
        "status": "proven" if proven else "needs_review",
        "reasons": reasons, "write": write if proven else {}, "evidence": evidence, "current": current,
    }
    out["message"] = ROW_MESSAGES[out["status"]]
    out["evidence_digest"] = _digest(out)
    return out


# ── proof ────────────────────────────────────────────────────────────────────

def prove_album(album_id: int, row: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    row = row or {}
    row_id = f"album:{album_id}"
    album = lib.get_album(album_id)
    if album is None:
        return _proof("album", album_id, row_id, ["target_missing"], {}, {}, {})
    current = {"mb_albumid": _uuid(getattr(album, "mb_albumid", "")),
               "mb_releasegroupid": _uuid(getattr(album, "mb_releasegroupid", ""))}
    if current["mb_albumid"]:
        return _proof("album", album_id, row_id, ["already_has_release_id"], {}, {}, current)
    if row.get("in_import_review"):
        return _proof("album", album_id, row_id, ["in_import_review"], {}, {}, current)

    items = composite_workflows.find_all_items_by_album_id(int(album_id)) or []
    paths = [_album_item_abs_path(_s(it.get("path"))) for it in items]
    folder = _s(row.get("path")).strip()
    reasons: List[str] = []
    if not items or not folder or not all(paths):
        reasons.append("no_items")
    elif any(_has_link_component(p) for p in paths):
        # Match writes tags through the path: a link could reach another item's file.
        reasons.append("symlinked_item_path")
    elif not all(_path_is_under(Path(p), Path(folder)) for p in paths):
        reasons.append("items_outside_album_folder")
    else:
        # The folder must hold exactly this album's files, not just as many.
        folder_files, linked = _recursive_audio(folder)
        if linked:
            reasons.append("symlinked_audio_in_folder")
        item_set = {_norm_path(p) for p in paths}
        if len(item_set) != len(paths) or item_set != {_norm_path(f) for f in folder_files}:
            reasons.append(f"folder_files_mismatch:{len(folder_files)}/{len(items)}")
    if reasons:
        return _proof("album", album_id, row_id, reasons, {}, {"album_items": len(items)}, current)

    ident = identify_import_folder(folder)
    auto = ident.get("auto_import") or {}
    caps = ident.get("evidence_capabilities") or {}
    rgid, release_id = _uuid(auto.get("release_group_id")), _uuid(auto.get("release_id"))
    chosen = next((c for c in ident.get("candidates") or [] if c.get("release_group_id") == rgid), {})
    cev = chosen.get("evidence") or {}
    release_tracks = next((int(r.get("track_count") or 0) for r in cev.get("releases_evaluated") or []
                           if _uuid(r.get("release_id")) == release_id), 0)
    evidence = {
        "audio_files": int(caps.get("audio_files") or 0),
        "album_items": len(items),
        "release_track_count": release_tracks,
        "local_tracks_verified": int(cev.get("local_tracks_verified") or 0),
        "acoustid": _s(caps.get("acoustid")),
        "conflicts": sorted(ident.get("conflicts") or []),
        "auto_import_reasons": list(auto.get("reasons") or []),
        "sources": list(cev.get("sources") or []),
    }
    if not auto.get("eligible"):
        reasons.extend(auto.get("reasons") or ["not_proven"])
    if evidence["conflicts"]:
        reasons.append("conflicts:" + ",".join(evidence["conflicts"]))
    if evidence["audio_files"] != len(items):
        reasons.append(f"folder_audio_count_mismatch:{evidence['audio_files']}/{len(items)}")
    if auto.get("eligible") and release_tracks != len(items):
        reasons.append(f"track_count_mismatch:{release_tracks}/{len(items)}")
    write = {"mb_releasegroupid": rgid, "mb_albumid": release_id} if rgid and release_id else {}
    return _proof("album", album_id, row_id, reasons, write, evidence, current)


def prove_item(item_id: int, row: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    row_id = f"item:{item_id}"
    item = lib.get_item(item_id)
    if item is None:
        return _proof("item", item_id, row_id, ["target_missing"], {}, {}, {})
    current = {k: _uuid(getattr(item, k, "")) for k in ("mb_trackid", "mb_albumid", "mb_releasegroupid")}
    if int(getattr(item, "album_id", 0) or 0):
        return _proof("item", item_id, row_id, ["not_a_singleton"], {}, {}, current)
    if current["mb_trackid"]:
        return _proof("item", item_id, row_id, ["already_has_recording_id"], {}, {}, current)

    _cur, candidates, item_path, _fn = _reconstruct_track_recording_candidates(item, item_id)
    reasons: List[str] = []
    outcome = _acoustid_lookup_cached_outcome(item_path) if item_path else None
    fingerprint = confirmed_recording(list(outcome.data or [])) if outcome is not None and outcome.answered else ""
    if outcome is None or not outcome.answered:
        reasons.append("fingerprint_unavailable")
    elif not fingerprint:
        reasons.append("no_single_confirmed_fingerprint")

    def _safe(c: Dict[str, Any]) -> bool:
        d = c.get("decision") or {}
        return bool((d.get("action_eligibility") or {}).get("attach_without_review")
                    and not d.get("review_required") and not d.get("conflicts")
                    and d.get("identity_proof") in _FINGERPRINT_PROOFS)

    safe = [c for c in candidates if _safe(c)]
    chosen = safe[0] if len(safe) == 1 else {}
    if len(safe) != 1:
        reasons.append("no_safe_candidate" if not safe else "multiple_safe_candidates")
    recording = _uuid(chosen.get("mb_trackid"))
    resolved = _uuid(((chosen.get("matching_contract") or {}).get("identity") or {}).get("resolved_recording_id"))
    if chosen and (not recording or recording != resolved):
        reasons.append("recording_id_not_resolved")
    if chosen and fingerprint and recording != fingerprint:
        reasons.append("fingerprint_names_other_recording")
    decision = chosen.get("decision") or {}
    evidence = {
        "fingerprint_recording_id": fingerprint,
        "identity_proof": _s(decision.get("identity_proof")),
        "decision_version": _s(chosen.get("decision_version")),
        "candidates": len(candidates),
        "safe_candidates": len(safe),
    }
    write = {"mb_trackid": recording} if recording else {}
    if write:  # context the attach writer may also persist; shown, not required
        evidence["release_id"] = _uuid(chosen.get("release_id") or chosen.get("mb_albumid"))
        evidence["release_group_id"] = _uuid(chosen.get("release_group_id") or chosen.get("mb_releasegroupid"))
    return _proof("item", item_id, row_id, reasons, write, evidence, current)


def prove_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Prove one Missing-IDs row; evidence failures are "not proven", never raised."""
    kind = row.get("target_kind")
    target = int((row.get("album_id") if kind == "album" else row.get("item_id")) or 0)
    try:
        return prove_album(target, row) if kind == "album" else prove_item(target, row)
    except Exception as ex:  # any evidence failure fails closed
        _app_logger.warning("missing-mb batch: evidence for %s:%d failed: %s",
                            "album" if kind == "album" else "item", target, type(ex).__name__)
        return _proof(_s(kind), target, _s(row.get("id")), ["evidence_unavailable"], {}, {}, {})


# ── selection ────────────────────────────────────────────────────────────────

def select_rows(payload: Dict[str, Any]) -> tuple:
    """(rows, error). ``{"ids": [...]}`` or ``{"kind", "search", "limit"}``."""
    ids = payload.get("ids")
    kind = _s(payload.get("kind") or "all").strip().lower()
    if kind not in ("all", "album", "item"):
        return [], "kind must be one of: all, album, item"
    if ids is not None:
        if (not isinstance(ids, list) or not ids or len(ids) > MAX_BATCH_ROWS
                or not all(isinstance(i, str) for i in ids)):
            return [], f"ids must be a list of 1-{MAX_BATCH_ROWS} row ids like \"album:12\" or \"item:34\""
        matches = [_ROW_ID_RE.fullmatch(i) for i in ids]
        if not all(matches):
            return [], "each id must look like \"album:12\" or \"item:34\""
        wanted = list(dict.fromkeys(f"{m[1]}:{int(m[2])}" for m in matches))
        by_id = {r["id"]: r for r in _missing_mb_rows("")}
        rows = [by_id.get(i) or {"id": i, "target_kind": "", "missing": True} for i in wanted]
        return rows, ""
    try:
        limit = int(payload.get("limit") or MAX_BATCH_ROWS)
    except (TypeError, ValueError):
        limit = 0
    if not 1 <= limit <= MAX_BATCH_ROWS:
        return [], f"limit must be 1-{MAX_BATCH_ROWS}"
    rows = [r for r in _missing_mb_rows(_s(payload.get("search"))[:200]) if kind == "all" or r["target_kind"] == kind]
    rows.sort(key=lambda r: (-r["sort_ts"], r["id"]))
    return rows[:limit], ""


# ── preview ──────────────────────────────────────────────────────────────────

def run_preview(rows: List[Dict[str, Any]], log: List[str], cancel_event=None, update_state=None,
                store=None, user: Optional[str] = None) -> Dict[str, Any]:
    store = store or transactions
    proofs: List[Dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("cancelled")
        if row.get("missing"):
            proofs.append(_proof("", 0, _s(row.get("id")), ["not_in_missing_list"], {}, {}, {}))
        else:
            proofs.append(prove_row(row))
        log.append(f"[{index}/{len(rows)}] {proofs[-1]['row_id']}: {proofs[-1]['status']}")
        if update_state:
            update_state(progress={"done": index, "total": len(rows)})

    proven = [p for p in proofs if p["proven"]]
    counts = {"rows": len(proofs), "proven": len(proven), "needs_review": len(proofs) - len(proven)}
    if not proven:
        return {"ok": True, "transaction_id": None, "confirm_token": None, "counts": counts, "rows": proofs}
    token = secrets.token_hex(16)
    changes = [{
        "id": p["row_id"], "operation": "MusicBrainz Match", "source": SOURCE,
        "current_metadata": p["current"], "new_metadata": p["write"],
        "metadata_diff": metadata_diff(p["current"], p["write"]),
        "reason": "Proven by fingerprint/identity evidence.", "metadata": {"evidence": p["evidence"]},
    } for p in proven]
    tx = store.create(
        operation_type="MusicBrainz Match", status="Preview", dry_run=True, source=SOURCE, initiating_user=user,
        summary=f"Add missing MusicBrainz IDs: {len(proven)} proven of {len(proofs)} row(s)",
        reason="Batch fix of library rows missing MusicBrainz IDs; proven matches only.",
        changes=changes, rollback_available=False,
        rollback_reason="Each row is written by its own rollback-capable transaction; see metadata.results.",
        metadata={"mutation_family": FAMILY, "rows": proofs, "confirm_token_sha256": _token_hash(token), "requires_approval": True,
                  "apply_endpoint": "/api/library/missing-mb-ids/batch/apply"},
    )
    store.update(tx["id"], counts={"items": len(proven)})
    return {"ok": True, "transaction_id": tx["id"], "confirm_token": token, "counts": counts, "rows": proofs}


# ── apply ────────────────────────────────────────────────────────────────────

def check_apply_request(tx_id: str, token: Any, store=None) -> tuple:
    """(error_body, http_status) or (None, 200) when the apply may start."""
    store = store or transactions
    if not isinstance(token, str) or not token:
        return {"ok": False, "code": "confirm_token_required",
                "error": "Apply needs the confirm_token from the preview."}, 400
    try:
        tx = store.get(tx_id)
    except KeyError:
        return {"ok": False, "code": "not_found", "error": "Preview transaction not found."}, 404
    meta = tx.get("metadata") or {}
    if meta.get("mutation_family") != FAMILY:
        return {"ok": False, "code": "wrong_transaction", "error": "Not a Missing IDs batch preview."}, 400
    if not hmac.compare_digest(_s(meta.get("confirm_token_sha256")).encode(), _token_hash(token).encode()):
        return {"ok": False, "code": "confirm_token_mismatch",
                "error": "The confirm_token does not match this preview."}, 409
    if tx.get("status") not in ("Preview", "Approved"):
        return {"ok": False, "code": "not_preview",
                "error": f"This preview was already used (status {tx.get('status')}). Preview again."}, 409
    return None, 200


def _snapshot(proofs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pre-apply backup: library counts and every target's current identity
    and file paths, recorded in the batch transaction before any write."""
    stats = beets_adapter.get_stats() or {}
    rows = {}
    for p in proofs:
        if p["kind"] == "album":
            album = lib.get_album(p["target_id"])
            items = composite_workflows.find_all_items_by_album_id(p["target_id"]) or []
            rows[p["row_id"]] = {
                "mb_albumid": _uuid(getattr(album, "mb_albumid", "")) if album else "",
                "mb_releasegroupid": _uuid(getattr(album, "mb_releasegroupid", "")) if album else "",
                "item_ids": [int(it.get("id") or 0) for it in items],
                "paths": [_s(it.get("path")) for it in items],
            }
        else:
            item = lib.get_item(p["target_id"])
            rows[p["row_id"]] = {k: _uuid(getattr(item, k, "")) if item else ""
                                 for k in ("mb_trackid", "mb_albumid", "mb_releasegroupid")}
            rows[p["row_id"]]["paths"] = [_s(getattr(item, "path", ""))] if item else []
    return {"captured_at": time.time(), "albums": int(stats.get("albums") or 0),
            "items": int(stats.get("items") or 0), "rows": rows}


def _verify_written(proof: Dict[str, Any]) -> bool:
    write = proof["write"]
    if proof["kind"] == "album":
        album = lib.get_album(proof["target_id"])
        return bool(album) and all(_uuid(getattr(album, k, "")) == v for k, v in write.items())
    item = lib.get_item(proof["target_id"])
    return bool(item) and _uuid(getattr(item, "mb_trackid", "")) == write["mb_trackid"]


def _row_result(proof: Dict[str, Any], status: str, **extra: Any) -> Dict[str, Any]:
    return {"row_id": proof["row_id"], "kind": proof["kind"], "target_id": proof["target_id"],
            "status": status, "message": ROW_MESSAGES[status], "write": proof.get("write") or {}, **extra}


def run_apply(tx_id: str, *, apply_album: Callable[..., Dict[str, Any]], apply_item: Callable[..., Dict[str, Any]],
              log: List[str], cancel_event=None, update_state=None, store=None) -> Dict[str, Any]:
    """Apply a claimed preview. ``apply_album(album_id, proof, log)`` and
    ``apply_item(item_id, proof, log)`` are the existing per-row writers;
    each returns ``{"changed": bool, "transaction_ids": [...]}`` or raises
    (``RecoveryRequired`` when a committed change could not be undone)."""
    store = store or transactions
    tx = (store.transition(tx_id, "Preview", "Running") or store.transition(tx_id, "Approved", "Running"))
    if tx is None:
        return {"ok": False, "code": "not_preview", "error": "This preview was already used. Preview again."}
    previewed = [p for p in (tx.get("metadata") or {}).get("rows") or [] if p.get("proven")]
    results: List[Dict[str, Any]] = []

    def _finish(status: str, ok: bool) -> Dict[str, Any]:
        counts = {s: sum(1 for r in results if r["status"] == s) for s in {r["status"] for r in results}}
        store.update(tx_id, status=status, dry_run=False, metadata={"results": results, "result_counts": counts},
                     append_logs=list(log)[-200:])
        return {"ok": ok, "transaction_id": tx_id, "status": status, "counts": counts, "rows": results}

    try:
        snapshot = _snapshot(previewed)
    except Exception as ex:
        log.append(f"ERROR: pre-apply snapshot failed: {_redact_security_text(str(ex))[:200]}")
        results = [_row_result(p, "evidence_unavailable") for p in previewed]
        return _finish("Failed", False)
    store.update(tx_id, metadata={"pre_apply_snapshot": snapshot})
    log.append(f"Pre-apply snapshot recorded: {snapshot['albums']} albums, {snapshot['items']} items.")

    stop: Optional[str] = None
    missing: Optional[Dict[str, Dict[str, Any]]] = None
    for index, proof in enumerate(previewed, start=1):
        if stop:
            results.append(_row_result(proof, "not_attempted"))
            continue
        if cancel_event is not None and cancel_event.is_set():
            stop = "cancelled"
            results.append(_row_result(proof, "cancelled"))
            continue
        label = proof["row_id"]
        try:
            if missing is None:  # one library read per batch; prove_row re-reads each target itself
                missing = {r["id"]: r for r in _missing_mb_rows("")}
            row = missing.get(label)
            fresh = (prove_row(row) if row else
                     _proof(proof["kind"], proof["target_id"], label, ["not_in_missing_list"], {}, {}, {}))
        except Exception as ex:
            log.append(f"{label}: evidence unavailable: {type(ex).__name__}")
            fresh = _proof(proof["kind"], proof["target_id"], label, ["evidence_unavailable"], {}, {}, {})
        if not fresh["proven"]:
            status = "evidence_unavailable" if "evidence_unavailable" in fresh["reasons"] else "no_longer_proven"
            results.append(_row_result(proof, status, reasons=fresh["reasons"]))
            log.append(f"{label}: skipped ({', '.join(fresh['reasons'])})")
        elif fresh["evidence_digest"] != proof["evidence_digest"] or fresh["write"] != proof["write"]:
            results.append(_row_result(proof, "evidence_changed"))
            log.append(f"{label}: skipped, evidence changed since the preview")
        else:
            writer = apply_album if proof["kind"] == "album" else apply_item
            try:
                written = writer(proof["target_id"], proof, log) or {}
                tx_ids = list(written.get("transaction_ids") or [])
                if not _verify_written(proof):
                    raise RecoveryRequired(f"{label}: written IDs did not verify on re-read")
                results.append(_row_result(proof, "written" if written.get("changed", True) else "already_set",
                                           transaction_ids=tx_ids))
                log.append(f"{label}: written and verified")
            except NotWritten as ex:
                results.append(_row_result(proof, "no_longer_proven"))
                log.append(f"{label}: writer refused before writing: {_redact_security_text(str(ex))[:200]}")
            except RecoveryRequired as ex:
                stop = "recovery_required"
                results.append(_row_result(proof, "recovery_required"))
                log.append(f"{label}: RECOVERY REQUIRED: {_redact_security_text(str(ex))[:300]}")
            except Exception as ex:
                recovery = "recovery required" in str(ex).lower()
                stop = "recovery_required" if recovery else "write_failed"
                results.append(_row_result(proof, stop))
                log.append(f"{label}: ERROR: {_redact_security_text(str(ex))[:300]}")
        store.update(tx_id, metadata={"results": results})  # checkpoint after every row
        if update_state:
            update_state(progress={"done": index, "total": len(previewed)})

    if stop == "recovery_required":
        return _finish("Recovery Required", False)
    if stop == "write_failed":
        return _finish("Failed", False)
    if stop == "cancelled":
        return _finish("Cancelled", True)  # the job records itself as cancelled
    return _finish("Completed", True)
