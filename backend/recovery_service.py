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
import time
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


# A staging folder younger than this is never cleaned up: it may be the target
# of an in-flight download whose carrier binding has not been written yet (the
# bind happens once files land). A true old-scheme orphan is far older.
ORPHAN_MIN_AGE_S = 3600


def _staging_root() -> str:
    """Realpath of the one validated recovery staging root
    (``<downloads>/_beets_missing_import``), or "" if it cannot be resolved or if
    that root is itself a symlink.

    SEC-007/#325 (Finding B): ``realpath`` would silently FOLLOW a symlinked
    ``_beets_missing_import`` (attacker pre-plant or a benign relocation symlink)
    to its target -- e.g. the whole ``<downloads>`` root -- and ``root`` would
    then equal that target, so the per-child realpath/commonpath containment
    check in ``reconcile_orphan_staging_folders`` would accept the target's aged,
    unowned top-level entries for cleanup (destroying not-yet-imported downloads).
    Failing closed on a symlinked root (``lstat`` via ``islink``, which never
    follows the final component) keeps ``root`` the genuine staging directory, so
    cleanup can only ever touch entries truly inside it."""
    try:
        from backend.app_runtime import validated_downloads_root
        path = str(validated_downloads_root() / "_beets_missing_import")
        if os.path.islink(path):
            return ""
        return os.path.realpath(path)
    except Exception:
        return ""


def _live_carrier_prefixes(limit: int = 500) -> Dict[str, str]:
    """``rec-<identity_key[:16]>`` -> carrier_id for every NON-terminal carrier,
    so a Step-3-scheme orphan folder whose deterministic prefix matches a live
    carrier can be ADOPTED (re-bound) instead of re-minted. The identity comes
    from the carrier record; the folder name only carries a server-generated
    token, never identity derived from the name.

    F8 (fail-closed): a carrier-store read error PROPAGATES here instead of being
    swallowed into an empty map. An empty return therefore means "store read OK,
    no live carriers", which is a trustworthy negative; a raise means "ownership
    UNKNOWN", which the caller treats as not-deletable (a folder matching an
    unread live-carrier prefix must not be mistaken for unowned)."""
    out: Dict[str, str] = {}
    live_states = [s for s in recovery_carrier.CARRIER_STATES
                   if s not in recovery_carrier.TERMINAL_STATES]
    for c in recovery_carrier.list_carriers_by_state(live_states, limit=limit):
        key = _s(c.get("recovery_identity_key")).strip().lower()
        if key:
            out.setdefault("rec-" + key[:16], _s(c.get("carrier_id")))
    return out


def _prefix_owner(folder_name: str, live_prefixes: Dict[str, str]) -> str:
    for prefix, cid in live_prefixes.items():
        if folder_name == prefix or folder_name.startswith(prefix + "-"):
            return cid
    return ""


def reconcile_orphan_staging_folders(limit: int = DEFAULT_ADVANCE_LIMIT,
                                     log: Optional[List[str]] = None) -> Dict[str, Any]:
    """ARCH-030 Step 4 (SEC-007/#325 containment): adopt or clean up orphaned
    non-terminal staging folders under ``_beets_missing_import``.

    Step 3 made staging deterministic (``rec-<16hex>`` reuse) but deliberately
    left pre-existing per-attempt ``<uuid>-...`` folders behind. This bounded,
    best-effort sweep:

    * ADOPTS (re-binds, never re-mints) a folder still bound to a carrier, or one
      whose deterministic ``rec-<16hex>`` prefix matches a LIVE carrier;
    * CLEANS UP a folder that belongs to no carrier and is safely removable,
      through the controlled staging-delete path (``composite_workflows.delete_file``),
      which enforces the SEC-007/#325 model: realpath/commonpath containment inside
      the validated staging root, no symlink escape, never a staging root itself,
      NEVER MUSIC_ROOT or protected data, fd-relative, fail-closed.

    Never touches the live music library and never deletes real media outside the
    staging root. A folder younger than ``ORPHAN_MIN_AGE_S`` is left alone."""
    import backend.composite_workflows as composite_workflows
    log = log if log is not None else []
    summary: Dict[str, Any] = {"scanned": 0, "adopted": 0, "removed": 0,
                               "kept": 0, "skipped": 0}
    root = _staging_root()
    if not root or not os.path.isdir(root):
        return summary
    # F8 (fail-closed): if the live-carrier prefix read fails, ownership is
    # UNKNOWN for EVERY folder this run (a folder could match a live carrier we
    # could not read), so no deletion is permitted -- recorded per-folder below.
    try:
        live_prefixes = _live_carrier_prefixes()
        prefixes_err: Optional[str] = None
    except Exception as ex:
        live_prefixes = {}
        prefixes_err = type(ex).__name__
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return summary
    now = time.time()
    cap = int(max(1, limit))
    for entry in entries:
        if summary["scanned"] >= cap:
            break
        # Immediate child directories only; a symlinked child is never followed.
        try:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        child = os.path.join(root, entry.name)
        summary["scanned"] += 1
        # Containment pre-check (fail-closed): the realpath must stay strictly
        # inside the staging root. A component that resolves outside (symlink
        # escape, mount) is skipped and never handed to the delete path.
        try:
            real = os.path.realpath(child)
            if real == root or os.path.commonpath([real, root]) != root:
                summary["skipped"] += 1
                continue
        except (OSError, ValueError):
            summary["skipped"] += 1
            continue
        # ADOPT: a folder the carrier store still resolves (side table or legacy
        # last-bound column) belongs to a carrier -- keep it, never delete.
        # F8 (fail-closed): a lookup that RAISES is ownership-UNKNOWN, not
        # proof-of-unowned; remember it so cleanup is refused below.
        folder_err: Optional[str] = None
        try:
            bound = recovery_carrier.load_for_folder(child)
        except Exception as ex:
            bound = None
            folder_err = type(ex).__name__
        if bound:
            summary["kept"] += 1
            continue
        # ADOPT by deterministic prefix: a Step-3-scheme folder whose rec-<hex>
        # prefix matches a live carrier is re-bound instead of re-minted.
        owner = _prefix_owner(entry.name, live_prefixes)
        if owner:
            try:
                recovery_carrier.bind_staged_folder(owner, child)
                summary["adopted"] += 1
                log.append(f"[recovery-reconcile] adopted staging folder for carrier {owner}.")
            except Exception:
                summary["skipped"] += 1
            continue
        # CLEANUP gate (F8, fail-closed): delete ONLY when ownership positively
        # resolved as unowned. If either carrier-store read failed (this folder's
        # lookup raised, or the live-carrier prefix read raised), ownership is
        # UNKNOWN -- the folder may belong to a LIVE carrier -- so skip, never
        # delete. Log the exception type + folder name only (no secrets).
        store_err = folder_err or prefixes_err
        if store_err:
            summary["skipped"] += 1
            log.append(f"[recovery-reconcile] skipped {entry.name} "
                       f"(carrier-store unavailable: {store_err}).")
            continue
        # CLEANUP: no carrier owns it. Age guard against an in-flight download.
        try:
            if now - os.stat(child).st_mtime < ORPHAN_MIN_AGE_S:
                summary["skipped"] += 1
                continue
        except OSError:
            summary["skipped"] += 1
            continue
        try:
            res = composite_workflows.delete_file(child)
            if res.get("deleted"):
                summary["removed"] += 1
                log.append(f"[recovery-reconcile] removed orphaned staging folder {entry.name}.")
            else:
                summary["skipped"] += 1
        except (ValueError, OSError) as ex:
            # delete_file fails closed on a refused/unsafe path or a platform
            # without fd-relative ops; nothing is removed.
            summary["skipped"] += 1
            log.append(f"[recovery-reconcile] skipped {entry.name} ({type(ex).__name__}).")
    if summary["adopted"] or summary["removed"]:
        log.append(f"[recovery-reconcile] done: adopted {summary['adopted']}, "
                   f"removed {summary['removed']}, kept {summary['kept']}, "
                   f"skipped {summary['skipped']}.")
    return summary


def start_recovery_advance_job(limit: int = DEFAULT_ADVANCE_LIMIT):
    """Start the bounded advancer as ONE guarded, de-duplicated ``job_engine``
    job (the replacement advancer the AI-batch exclusion assumes exists). A
    second start while one runs is refused by the workflow lock / dedupe_key,
    so the sweep is never run concurrently with itself."""
    def _job(log: List[str]) -> Dict[str, Any]:
        summary = advance_stuck_carriers(limit=limit, log=log)
        summary["orphan_reconcile"] = reconcile_orphan_staging_folders(limit=limit, log=log)
        return summary

    guarded = job_contract.guarded(_job, workflow=ADVANCER_WORKFLOW, fail_fast_in_process=True)
    return jobs.start_python(
        guarded, label="Advance missing-track recovery",
        metadata=job_contract.contract_metadata(
            ADVANCER_WORKFLOW, {"type": ADVANCER_JOB_TYPE, "mutating": True,
                                "dedupe_key": ADVANCER_JOB_TYPE}))
