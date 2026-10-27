"""Missing-track recovery LIFECYCLE owner (ARCH-030 Step 2).

``recovery_carrier`` is the durable identity SPINE (schema/CAS/one-to-many
staged folders). This module is the one service that OWNS the carrier state
machine's forward progress: it re-evaluates stuck / review_required carriers
against the canonical matching contract and, when the contract authorizes the
action, hands the staged files to the existing verified-insertion entrypoint.

It exists because the generic new-album AI batch deliberately EXCLUDES
carrier-bound folders (``ai_service._ai_batch_find_audio_dirs``) and nothing
else advanced a carrier that landed in Review -- a permanent "Pending AI"
stall. This is that missing advancer: a bounded, idempotent ``job_engine`` job.

Layering (ARCH-001): this sits ABOVE recovery_carrier and reuses
matching_service (the gate) + import_service (the transaction-backed insertion)
WITHOUT reimplementing either. It never opens the Beets library directly, never
runs its own transaction/rollback, and never lowers an identity threshold --
``action_allowed`` on the matching contract is the single authorization field.
The insertion entrypoint (``start_folder_import_with_id``) consumes the CAS
claim + reconcile + durable rollback that every recovery merge already uses;
Step 4 (library-transaction) makes that entrypoint honor the gate directly.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional

import backend.job_contract as job_contract
import backend.recovery_carrier as recovery_carrier
from backend.app_runtime import jobs, _s

logger = logging.getLogger(__name__)

# One advancer run at a time, across processes/restarts (reuses job_contract).
ADVANCER_WORKFLOW = "recovery-advance"
ADVANCER_JOB_TYPE = "recovery-advance"
# Bounded: a run drains at most this many carriers so one job never scans the
# whole backlog unbounded. The next run picks up the rest (oldest-first).
DEFAULT_ADVANCE_LIMIT = 25


def _attach_decision(carrier_id: str, decision: Dict[str, Any], reason: str) -> None:
    """Record the matching contract (and a bounded reason) onto the carrier so
    the Review row's single confidence/status derives from the contract, not a
    stale coarse suggestion. Keeps the carrier's current state; best-effort
    (a CAS loss/other write just means a later run re-records it)."""
    try:
        current = recovery_carrier.get_carrier(carrier_id)
        if not current:
            return
        recovery_carrier.advance_state(
            carrier_id, _s(current.get("state")) or "created",
            payload_extra={"matching_decision": decision or {},
                           "advancer_reason": _s(reason)},
            expected_revision=int(current.get("revision") or 0),
        )
    except Exception:
        pass


def _first_staged_folder_with_audio(carrier: Dict[str, Any]) -> str:
    """Newest staged folder of the carrier that still exists on disk. Every
    attempt's folder is resolvable (one-to-many), so a re-stage never strands
    the carrier on an orphaned path. Returns "" when none is present."""
    from backend.ytdlp_service import _audio_files_in_dir
    cid = _s(carrier.get("carrier_id"))
    candidates = recovery_carrier.staged_folders_for(cid)
    legacy = _s(carrier.get("staged_folder_path"))
    if legacy and legacy not in candidates:
        candidates.append(legacy)
    for folder in candidates:
        raw = _s(folder).strip()
        if not raw or not os.path.isdir(raw):
            continue
        try:
            if _audio_files_in_dir(raw):
                return raw
        except Exception:
            continue
    return ""


def _reevaluate_carrier(carrier: Dict[str, Any], log: List[str]) -> Dict[str, Any]:
    """Re-run the canonical matching preflight for one carrier's staged files.
    Returns ``{folder, decision, ok, reason}``. ``decision`` is the
    matching_decision contract; ``ok`` is True only when the contract
    authorizes the action (``action_allowed``)."""
    from backend.matching_service import (
        _carrier_expected_as_wanted, _folder_release_preflight, _preflight_rejection_reason)
    cid = _s(carrier.get("carrier_id"))
    folder = _first_staged_folder_with_audio(carrier)
    if not folder:
        return {"folder": "", "decision": {}, "ok": False,
                "reason": "no staged audio folder is present on disk to re-verify"}
    release = _s(carrier.get("release_id")) or _s(carrier.get("mb_releasegroupid"))
    wanted = _carrier_expected_as_wanted(carrier)
    try:
        result = _folder_release_preflight(
            folder, release, int(carrier.get("existing_album_id") or 0),
            log=log, fingerprint_evidence=True, wanted_tracks=wanted)
    except Exception as ex:
        logger.warning("recovery advancer preflight failed for carrier %s: %s", cid, type(ex).__name__)
        return {"folder": folder, "decision": {}, "ok": False,
                "reason": f"re-verification could not run ({type(ex).__name__}); retry later",
                "retryable": True}
    decision = result.get("matching_decision") or {}
    ok = bool(result.get("ok")) and bool(decision.get("action_allowed"))
    reason = "" if ok else (_preflight_rejection_reason(result)
                            or _s(decision.get("explanation")) or "identity not verified")
    return {"folder": folder, "decision": decision, "ok": ok, "reason": reason}


def _hand_off_to_insertion(carrier: Dict[str, Any], folder: str, log: List[str]) -> bool:
    """Hand a gate-passing recovery to the existing verified-insertion
    entrypoint (transaction-backed; carrier CAS claim + reconcile + rollback).
    Never imports here directly. Returns True when a job was accepted."""
    from backend import import_service
    cid = _s(carrier.get("carrier_id"))
    payload = {
        "path": folder,
        "mb_releasegroupid": _s(carrier.get("mb_releasegroupid")),
        "mb_albumid": _s(carrier.get("release_id")) or _s(carrier.get("mb_releasegroupid")),
        "existing_album_id": int(carrier.get("existing_album_id") or 0),
        "wanted_tracks": list(carrier.get("expected_tracks") or carrier.get("wanted_tracks") or []),
        "recovery_carrier_id": cid,
        "queue_review": True,
        "trigger_plex": True,
        "trigger_plex_context": "recovery",
    }
    try:
        body, status = import_service.start_folder_import_with_id(payload)
    except Exception as ex:  # DuplicateJobError (another job owns it) or other
        log.append(f"[recovery-advance] carrier {cid}: insertion hand-off not started "
                   f"({type(ex).__name__}); will retry next run.")
        return False
    if isinstance(body, dict) and body.get("ok") is False:
        log.append(f"[recovery-advance] carrier {cid}: insertion refused "
                   f"(status {status}); kept for review.")
        return False
    log.append(f"[recovery-advance] carrier {cid}: verified subset handed to insertion "
               f"(job {isinstance(body, dict) and body.get('job_id') or '?'}).")
    return True


def advance_stuck_carriers(limit: int = DEFAULT_ADVANCE_LIMIT,
                           log: Optional[List[str]] = None) -> Dict[str, Any]:
    """Bounded, idempotent sweep: for each stuck / review_required carrier,
    re-evaluate its staged files against the matching contract; hand gate-
    passing ones (incl. the deterministic-metadata-slot subset) to the
    verified-insertion entrypoint, and leave the rest in MANUAL review with an
    accurate, contract-derived reason. Terminal / mid-import carriers are owned
    by their own job and skipped. Returns a summary."""
    log = log if log is not None else []
    summary: Dict[str, Any] = {"scanned": 0, "handed_off": 0, "kept_review": 0, "skipped": 0,
                               "carriers": []}
    try:
        carriers = recovery_carrier.list_carriers_by_state(
            recovery_carrier.ADVANCER_STATES, limit=int(max(1, limit)))
    except Exception as ex:
        log.append(f"[recovery-advance] could not list carriers ({type(ex).__name__}).")
        summary["error"] = type(ex).__name__
        return summary
    for carrier in carriers:
        cid = _s(carrier.get("carrier_id"))
        summary["scanned"] += 1
        # Re-read: a concurrent job may have claimed/advanced it since the list.
        fresh = recovery_carrier.get_carrier(cid) or carrier
        if _s(fresh.get("state")) not in recovery_carrier.ADVANCER_STATES:
            summary["skipped"] += 1
            summary["carriers"].append({"carrier_id": cid, "outcome": "skipped_not_advanceable"})
            continue
        evaluation = _reevaluate_carrier(fresh, log)
        decision = evaluation["decision"]
        _attach_decision(cid, decision, evaluation["reason"])
        if evaluation["ok"]:
            if _hand_off_to_insertion(fresh, evaluation["folder"], log):
                summary["handed_off"] += 1
                summary["carriers"].append({"carrier_id": cid, "outcome": "handed_off"})
                continue
            # Hand-off refused/raced: fall through and keep it in review.
        # Keep (or place) the carrier in MANUAL review with an accurate reason.
        try:
            if _s(fresh.get("state")) != "review_required":
                recovery_carrier.advance_state(cid, "review_required")
        except Exception:
            pass
        summary["kept_review"] += 1
        summary["carriers"].append({"carrier_id": cid, "outcome": "kept_review",
                                    "reason": evaluation["reason"]})
        if evaluation["reason"]:
            log.append(f"[recovery-advance] carrier {cid}: kept in review -- {evaluation['reason']}")
    log.append(f"[recovery-advance] done: scanned {summary['scanned']}, "
               f"handed_off {summary['handed_off']}, kept_review {summary['kept_review']}, "
               f"skipped {summary['skipped']}.")
    return summary


def start_recovery_advance_job(limit: int = DEFAULT_ADVANCE_LIMIT):
    """Start the bounded advancer as ONE guarded, de-duplicated ``job_engine``
    job (the replacement advancer the AI-batch exclusion assumes exists). A
    second start while one runs is refused by the workflow lock / dedupe_key,
    so the sweep is never run concurrently with itself."""
    def _job(log: List[str]) -> Dict[str, Any]:
        return advance_stuck_carriers(limit=limit, log=log)

    guarded = job_contract.guarded(_job, workflow=ADVANCER_WORKFLOW, fail_fast_in_process=True)
    return jobs.start_python(
        guarded, label="Advance missing-track recovery",
        metadata=job_contract.contract_metadata(
            ADVANCER_WORKFLOW, {"type": ADVANCER_JOB_TYPE, "mutating": True,
                                "dedupe_key": ADVANCER_JOB_TYPE}))
