"""Import reconciliation orchestration over backend.import_reconciliation (ARCH-001).
"""

from __future__ import annotations

import os, re, time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from backend.app_runtime import _app_logger, AUDIO_EXT, MUSIC_ROOT, _extract_mb_uuid, _s
from backend.ai_batch_state_service import _ai_batch_commit, _ai_batch_mark_orphaned_ai_completed_for_review, _ai_batch_mark_stale_ai_running_folders, _ai_batch_recalculate_batch_state, _ai_batch_worker_registered, _is_music_format_policy_handled_error, _music_format_policy_review_note
from backend.cleanup_service import _artist_alias_key
from backend.app_runtime import _path_has_symlink_component_under, _path_is_under, _path_lexically_under, _safe_apply_error_message
from backend.beets_adapter import BeetsError, BeetsUnavailableError, BeetsAuthError, BeetsBadRequestError, BeetsNotFoundError
import backend.composite_workflows as composite_workflows
from backend.app_runtime import jobs
from backend.serializers import _import_review_path_text_error, _resolve_import_review_source_path

# ── ARCH-001 extracted code ──


_MUSIC_FORMAT_POLICY_REVIEW_STATUS = "format_policy_rejected"


def _resolve_import_review_selected_audio_file(raw: Any, source_root: Path) -> Optional[Path]:
    resolved, error = _resolve_import_review_cleanup_file(raw, source_root)
    if (
        error
        or resolved is None
        or not resolved.exists()
        or not resolved.is_file()
        or resolved.suffix.lower() not in AUDIO_EXT
    ):
        return None
    return resolved


def _resolve_import_review_cleanup_file(raw: Any, folder: Path) -> Tuple[Optional[Path], Optional[str]]:
    error = _import_review_path_text_error(raw, allow_relative=True)
    if error:
        return None, error
    candidate = Path(_s(raw).strip())
    if not candidate.is_absolute():
        candidate = folder / candidate
    if candidate == folder or not _path_lexically_under(candidate, folder):
        return None, "outside_review_folder"
    if _path_has_symlink_component_under(candidate, folder):
        return None, "symlink"
    if candidate.is_symlink():
        return None, "symlink"
    try:
        resolved = candidate.resolve(strict=False)
    except Exception:
        return None, "Invalid cleanup file path."
    if resolved == folder or not _path_is_under(resolved, folder):
        return None, "outside_review_folder"
    return resolved, None


def _remaining_audio_files(folder_path: str) -> List[Path]:
    source, error = _resolve_import_review_source_path(
        folder_path,
        allow_music=True,
        expected_type=None,
        require_exists=True,
    ) if _s(folder_path).strip() else (None, "source path missing")
    if error or source is None:
        return []
    try:
        if source.is_file() and source.suffix.lower() in AUDIO_EXT:
            return [source]
        if source.is_dir():
            files: List[Path] = []
            for path in sorted(
                [p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXT],
                key=lambda p: str(p).lower(),
            ):
                resolved = _resolve_import_review_selected_audio_file(str(path), source)
                if resolved:
                    files.append(resolved)
            return files
    except Exception:
        pass
    return []


def _import_review_auto_job_for_key(key: str):
    if not key:
        return None
    for job in jobs.all():
        meta = getattr(job, "metadata", {}) or {}
        if meta.get("import_review_auto_idempotency_key") == key:
            return job
    return None


def _review_status_key(status: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _s(status).strip().casefold()).strip("_")


def _import_job_resolved_as_already_in_library(job) -> bool:
    try:
        text = "\n".join(_s(line) for line in (getattr(job, "log", []) or [])[-80:]).casefold()
    except Exception:
        text = ""
    return "album already in library" in text and "source cleaned up" in text


def _set_pending_review_format_policy_rejected_item(item: Dict[str, Any], note: Any) -> Dict[str, Any]:
    item["status"] = _MUSIC_FORMAT_POLICY_REVIEW_STATUS
    item["status_note"] = _music_format_policy_review_note(note)
    item["updated_at"] = int(time.time())
    suggestion = item.get("suggestion") if isinstance(item.get("suggestion"), dict) else {}
    item["suggestion"] = suggestion
    suggestion["reason"] = item["status_note"]
    return item


def _stale_review_item_can_resume_import(item: Dict[str, Any]) -> bool:
    status_key = _review_status_key((item or {}).get("status"))
    if status_key not in {"remaining_files_review", "auto_enqueue_failed", "import_job_missing", "import_status_unknown", "import_enqueueing", "import_queued"}:
        return False
    suggestion = item.get("suggestion") if isinstance((item or {}).get("suggestion"), dict) else {}
    evidence = item.get("evidence") if isinstance((item or {}).get("evidence"), dict) else {}
    revalidation = evidence.get("revalidation") if isinstance(evidence.get("revalidation"), dict) else {}
    try:
        importable = int(
            revalidation.get("importable_track_count")
            or suggestion.get("match_count")
            or suggestion.get("track_match_count")
            or 0
        )
    except Exception:
        importable = 0
    release_group_id = _extract_mb_uuid(_s(
        suggestion.get("mb_releasegroupid")
        or (item or {}).get("mb_releasegroupid")
        or ""
    ))
    representative_id = _extract_mb_uuid(_s(
        suggestion.get("representative_mb_albumid")
        or suggestion.get("mb_albumid")
        or (item or {}).get("mb_albumid")
        or ""
    ))
    if suggestion.get("identity_validated") is False:
        return False
    return bool(importable > 0 and release_group_id and representative_id)


def _reconcile_pending_review_enqueue_item(item: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], bool]:
    status_key = _review_status_key((item or {}).get("status"))
    resume_statuses = {"remaining_files_review", "auto_enqueue_failed", "import_job_missing", "import_status_unknown"}
    active_statuses = {"import_enqueueing", "import_queued"}
    if status_key not in (active_statuses | resume_statuses):
        return item, False
    path = _s((item or {}).get("path", "")).strip()
    key = _s((item or {}).get("auto_import_idempotency_key", "")).strip()
    job_id = _s((item or {}).get("auto_import_job_id", "")).strip()
    job = jobs.get(job_id) if job_id else None
    if not job:
        job = _import_review_auto_job_for_key(key)
    changed = False
    if job:
        item["auto_import_job_id"] = job.job_id
        if job.status == "running":
            if status_key != "import_queued":
                item["status"] = "import_queued"
                item["status_note"] = "Import job is queued or running."
                changed = True
            return item, changed
        if job.status == "success":
            if _import_job_resolved_as_already_in_library(job):
                return None, True
            remaining = _remaining_audio_files(path)
            if remaining:
                item["status"] = "remaining_files_review"
                item["status_note"] = f"Partial import complete. {len(remaining)} unmatched file(s) remain in review."
                suggestion = item.get("suggestion") if isinstance(item.get("suggestion"), dict) else {}
                item["suggestion"] = suggestion
                suggestion["reason"] = item["status_note"]
                return item, True
            return None, True
        last = ""
        try:
            last = next((_s(line).strip() for line in reversed(job.log[-20:]) if _s(line).strip()), "")
        except Exception:
            last = ""
        if _is_music_format_policy_handled_error(last):
            if not _remaining_audio_files(path):
                return None, True
            return _set_pending_review_format_policy_rejected_item(item, last), True
        item["status"] = "auto_enqueue_failed"
        item["status_note"] = last or "Import job failed before completion."
        suggestion = item.get("suggestion") if isinstance(item.get("suggestion"), dict) else {}
        item["suggestion"] = suggestion
        suggestion["reason"] = item["status_note"]
        return item, True

    if status_key in resume_statuses:
        if _stale_review_item_can_resume_import(item):
            item["status"] = "ready_to_import"
            item["status_note"] = "Verified tracks are ready to import; unmatched files stay in review after import."
            suggestion = item.get("suggestion") if isinstance(item.get("suggestion"), dict) else {}
            item["suggestion"] = suggestion
            suggestion["reason"] = item["status_note"]
            return item, True
        return item, False

    try:
        updated_at = float(item.get("updated_at") or item.get("added_at") or 0)
    except Exception:
        updated_at = 0
    stale = not updated_at or time.time() - updated_at > 45
    if status_key == "import_enqueueing" or stale:
        if _stale_review_item_can_resume_import(item):
            item["status"] = "ready_to_import"
            item["status_note"] = "Verified tracks are ready to import; unmatched files stay in review after import."
            suggestion = item.get("suggestion") if isinstance(item.get("suggestion"), dict) else {}
            item["suggestion"] = suggestion
            suggestion["reason"] = item["status_note"]
            return item, True
        item["status"] = "auto_enqueue_failed"
        item["status_note"] = "Import enqueue did not create an active job; retry enqueue."
        suggestion = item.get("suggestion") if isinstance(item.get("suggestion"), dict) else {}
        item["suggestion"] = suggestion
        suggestion["reason"] = item["status_note"]
        return item, True
    return item, False


def _review_paths_equal(a: str, b: str) -> bool:
    """Used to authorize destructive Pending Review cleanup actions
    (_pending_review_matches), so this must fail closed: when either side
    cannot be trust-resolved (outside the approved roots, a symlink, or
    otherwise invalid), the two paths are never considered equal merely
    because their raw text normalizes the same way. A stored Pending
    Review entry that fails trusted resolution must not be treated as
    authorization evidence."""
    if a == b:
        return True
    left, left_error = _resolve_import_review_source_path(
        a,
        allow_music=True,
        expected_type=None,
        require_exists=False,
    ) if _s(a).strip() else (None, "path missing")
    right, right_error = _resolve_import_review_source_path(
        b,
        allow_music=True,
        expected_type=None,
        require_exists=False,
    ) if _s(b).strip() else (None, "path missing")
    if left is None or right is None or left_error or right_error:
        return False
    return str(left) == str(right)


def _import_review_reconcile_job_lookup(job_id: str, source_path: str, review_item_id: str, idempotency_key: str):
    job_id = _s(job_id).strip()
    source_path = _s(source_path).strip()
    review_item_id = _s(review_item_id).strip()
    idempotency_key = _s(idempotency_key).strip()
    if job_id:
        job = jobs.get(job_id)
        if job:
            return job
    if idempotency_key:
        job = _import_review_auto_job_for_key(idempotency_key)
        if job:
            return job
    candidates = []
    for candidate in jobs.all():
        meta = getattr(candidate, "metadata", {}) or {}
        if meta.get("type") != "import-folder":
            continue
        matched = False
        if idempotency_key and _s(meta.get("import_review_auto_idempotency_key")).strip() == idempotency_key:
            matched = True
        if review_item_id and _s(meta.get("review_item_id")).strip() == review_item_id:
            matched = True
        if source_path and _review_paths_equal(_s(meta.get("path")), source_path):
            matched = True
        if matched:
            candidates.append(candidate)
    if not candidates:
        return None
    candidates.sort(
        key=lambda candidate: (
            1 if getattr(candidate, "status", "") == "running" else 0,
            float(getattr(candidate, "created_at", 0) or 0),
        ),
        reverse=True,
    )
    return candidates[0]


_AI_BATCH_STALE_SECONDS = max(60, int(os.environ.get("AI_BATCH_STALE_SECONDS", "300") or "300"))


def _ai_batch_reconcile_state(state: Dict[str, Any]) -> Dict[str, Any]:
    now = time.time()
    job_id = _s(state.get("job_id"))
    job = jobs.get(job_id) if job_id else None
    batch_job_id = _s(state.get("batch_job_id"))
    registry_active = _ai_batch_worker_registered(batch_job_id)
    worker_alive = bool(job and job.status == "running") or registry_active
    state["worker_alive"] = worker_alive
    heartbeat = float(state.get("heartbeat_at") or 0)
    state["heartbeat_age_seconds"] = max(0.0, now - heartbeat) if heartbeat else None
    if registry_active:
        # A worker is registered (startup-reserved or actively running) for
        # this batch in this process. This call is happening from a route
        # handler thread, not the registered worker itself (the worker never
        # calls _ai_batch_reconcile_state -- see _ai_batch_active_workers'
        # docstring above), so `state` here may be the pre-reconciliation
        # snapshot from before that worker's own retry/requeue pass commits.
        # Recalculating/finalizing against it would stomp worker_alive back
        # to False via _ai_batch_recalculate_batch_state's finalize branch
        # (which trusts folder_states to be reconciled) and could persist a
        # premature terminal status ahead of the real worker's own
        # reconciliation. Defer entirely: the registered worker's own
        # commits are the sole source of truth until it releases its
        # registration.
        return state
    changed = _ai_batch_recalculate_batch_state(state)
    unfinished = int(state.get("folders_unfinished") or 0)
    if state.get("status") in {"running", "stale"} and not worker_alive:
        counts = state.get("folder_status_counts") or {}
        no_active_folder_work = not int(state.get("folders_running") or 0) and not int(state.get("folders_queued") or 0)
        if no_active_folder_work and int(counts.get("ai_completed") or 0) > 0:
            changed = _ai_batch_mark_orphaned_ai_completed_for_review(state) or changed
            changed = _ai_batch_recalculate_batch_state(state) or changed
            unfinished = int(state.get("folders_unfinished") or 0)
    if state.get("status") == "running" and not worker_alive and heartbeat and now - heartbeat > _AI_BATCH_STALE_SECONDS:
        if unfinished > 0:
            state["status"] = "stale"
            state["recovery_state"] = "stale_needs_recovery"
            state["current_step"] = "stale heartbeat"
            state["last_error"] = "Batch appears stuck; worker heartbeat is stale."
            changed = True
        else:
            changed = _ai_batch_recalculate_batch_state(state) or changed
    if state.get("status") == "stale":
        reason = _s(state.get("last_error") or "Batch appears stuck; worker heartbeat is stale.")
        changed = _ai_batch_mark_stale_ai_running_folders(state, reason) or changed
        changed = _ai_batch_recalculate_batch_state(state) or changed
    if changed:
        try:
            _ai_batch_commit(state, heartbeat=False)
            heartbeat = float(state.get("heartbeat_at") or 0)
            state["heartbeat_age_seconds"] = max(0.0, time.time() - heartbeat) if heartbeat else None
        except Exception:
            pass
    return state


# Hotfix v0.1.17 (BUG-4): long-controlled-mutation configuration for the
# artist_folder_reconcile_v1 apply step specifically -- not a global Beets
# API timeout change. Production evidence (a real TrueNAS v0.1.16
# deployment): `beet version` alone took 47.06s under real host load, and
# the previous flat 60s client timeout for this specific apply call fired
# while the engine was still genuinely executing the mutation (proven by
# inspection: it kept moving from file to file after the Web Manager
# reported "engine unavailable"). Only this operation-specific timeout is
# widened; /health, /version, and normal /status remain fast for
# unrelated reasons (see backend/beets_control_agent.py BUG-1/BUG-2) and
# do not need a larger timeout at all.
BEETS_ARTIST_RECONCILE_TIMEOUT_SECONDS = max(1.0, float(os.environ.get("BEETS_ARTIST_RECONCILE_TIMEOUT_SECONDS", "120") or "120"))


# How often to re-check the engine's authoritative transaction state after
# the initial Apply call's own HTTP response is lost, and the maximum total
# time to keep monitoring before giving up (still without ever calling
# Apply a second time). The Web Manager's job system is already
# asynchronous to the UI, so it is fine for a background job to monitor an
# engine operation for several minutes; it is not fine to declare the
# engine unavailable while the operation is still executing normally.
BEETS_LONG_OPERATION_POLL_SECONDS = max(0.5, float(os.environ.get("BEETS_LONG_OPERATION_POLL_SECONDS", "5") or "5"))


BEETS_LONG_OPERATION_MAX_SECONDS = max(1.0, float(os.environ.get("BEETS_LONG_OPERATION_MAX_SECONDS", "600") or "600"))


# Transaction statuses (backend/transaction_engine.py's TransactionStore)
# that mean the artist-folder-reconcile apply has reached a definitive
# outcome. Anything else (Preview/Pending/Approved/Running) means the
# engine has not finished yet -- keep polling, never re-Apply.
_ARTIST_RECONCILE_TERMINAL_SUCCESS_STATUSES = {"Completed"}


_ARTIST_RECONCILE_TERMINAL_FAILURE_STATUSES = {"Failed", "Rolled Back", "Partially Rolled Back", "Cancelled", "Recovery Required"}


def _apply_artist_folder_reconcile_resilient(
    op_id: str, log: List[str], *, cancel_event: Any = None, log_prefix: str = "Artist folder reconcile",
    _acceptance_failpoint: Optional[str] = None, skip_initial_apply: bool = False,
) -> Dict[str, Any]:
    """Apply an already-planned artist_folder_reconcile_v1 operation and
    survive a lost HTTP response without ever calling Apply a second time
    for the same operation_id (hotfix v0.1.17, BUG-4).

    skip_initial_apply=True skips the Apply call entirely and goes straight
    to the transaction-status poll loop below. Use this when the caller
    already knows -- from a prior, authoritative transaction status check,
    not merely from an assumption -- that Apply was already accepted by the
    engine for this operation_id (e.g. Clean All resuming after a process
    restart found the transaction status already "Running"): calling Apply
    again in that case would violate the same "never re-Apply" guarantee
    this function exists to uphold, just via a different code path.

    Production incident this fixes: the Web Manager's own client-side
    timeout fired on the Apply call while the Beets Engine kept executing
    the controlled mutation normally; the Web Manager logged "engine
    unavailable" and gave up, even though the operation went on to
    complete successfully on the engine side (confirmed by direct
    inspection: it kept moving from file to file after the Web Manager had
    already given up) and then failed to write its now-orphaned response
    back over the closed socket (BrokenPipeError -- see BUG-6).

    Once operation_id has been accepted for Apply, a client-side timeout
    or transport failure must never be interpreted as "the mutation did
    not happen": this function calls Apply at most once, and on any
    failure to receive that response, polls the engine's own authoritative
    transaction record (the exact same store execute_artist_folder_reconcile_apply()
    writes "Running"/"Completed"/"Failed" to) until it reaches a terminal
    status, bounded by BEETS_LONG_OPERATION_MAX_SECONDS. It never re-calls
    apply_artist_folder_reconcile() itself under any circumstance.

    _acceptance_failpoint is test-only infrastructure passed straight
    through to composite_workflows.apply_artist_folder_reconcile() -- the engine
    ignores it entirely unless booted with BEETS_ACCEPTANCE_MODE=1, which
    no real deployment ever sets. Always None in real production calls.

    Error classification matters here: a definite HTTP/application
    rejection (400/401/403/404) means the request WAS answered -- the
    engine explicitly refused it (bad operation_id, bad auth, operation not
    found/not in an applyable state). That is not "response was lost"; the
    response was received and it was "no". Treating it as lost and falling
    into the up-to-600s transaction poll below would misreport a definite,
    already-known rejection as a mystery, and (worse) risks a caller
    reacting to a stale/matching-by-coincidence transaction. Only genuine
    transport uncertainty -- BeetsUnavailableError (connection refused/
    reset, DNS failure, timeout, malformed response, 502/503/504) or an
    ambiguous 5xx BeetsError where the engine may have started mutating
    before failing to answer -- means the execution outcome is genuinely
    unknown and must be recovered via the authoritative transaction poll.
    """
    def _do_apply() -> Optional[Dict[str, Any]]:
        # CodeQL: information exposure through an exception -- {ex} can
        # carry internal URLs, paths, or transport details, and every log
        # line/"error" field below is job-visible (Clean All log, standalone
        # route job log/result). Log the real exception server-side only;
        # error_code/status_code/diagnostics are agent-controlled structured
        # fields, not exception text, and remain safe to expose.
        try:
            return composite_workflows.apply_artist_folder_reconcile(
                op_id, acceptance_failpoint=_acceptance_failpoint, timeout=BEETS_ARTIST_RECONCILE_TIMEOUT_SECONDS,
            )
        except (BeetsBadRequestError, BeetsAuthError, BeetsNotFoundError) as ex:
            _app_logger.error("%s: apply definitively rejected for op_id=%s: %s", log_prefix, op_id, ex, exc_info=True)
            safe_reason = _safe_apply_error_message(ex)
            log.append(f"{log_prefix}: Apply was rejected ({safe_reason}). Not retried and not polled.")
            return {
                "ok": False,
                "operation_id": op_id,
                "error": safe_reason,
                "error_code": getattr(ex, "error_code", "") or "",
                "status_code": getattr(ex, "status_code", 0) or 0,
            }
        except (BeetsUnavailableError, BeetsError) as ex:
            _app_logger.warning("%s: apply transport failure for op_id=%s, switching to transaction polling: %s", log_prefix, op_id, ex, exc_info=True)
            log.append(
                f"{log_prefix}: Apply response was lost ({_safe_apply_error_message(ex)}). The engine "
                f"operation may still be running -- monitoring its transaction state instead of retrying Apply."
            )
        except Exception as ex:
            _app_logger.warning("%s: unexpected apply transport failure for op_id=%s, switching to transaction polling: %s", log_prefix, op_id, ex, exc_info=True)
            log.append(
                f"{log_prefix}: Apply response was lost (unexpected error). The engine operation may "
                f"still be running -- monitoring its transaction state instead of retrying Apply."
            )
        return None

    if skip_initial_apply:
        log.append(
            f"{log_prefix}: op_id={op_id} Apply was already accepted by the engine (confirmed via prior "
            f"transaction status check); monitoring its transaction state instead of calling Apply again."
        )
    else:
        apply_outcome = _do_apply()
        if apply_outcome is not None:
            return apply_outcome

    deadline = time.monotonic() + BEETS_LONG_OPERATION_MAX_SECONDS
    last_status = ""
    while True:
        if cancel_event is not None and cancel_event.is_set():
            log.append(
                f"{log_prefix}: monitoring cancelled locally for op_id={op_id}; the engine operation itself "
                f"is not stopped by this and will be reconciled on the next run."
            )
            return {"ok": False, "operation_id": op_id, "status": last_status or "unknown",
                    "error": "Monitoring cancelled locally; engine operation may still be running.", "still_running": True}
        try:
            tx_res = composite_workflows.get_transaction(op_id)
            tx = tx_res.get("transaction") or {}
            last_status = str(tx.get("status") or "")
        except Exception as ex:
            # CodeQL: information exposure through an exception -- log the
            # real exception server-side only; the job-visible log line
            # gets a sanitized reason.
            _app_logger.warning("%s: transaction status check for op_id=%s failed, retrying: %s", log_prefix, op_id, ex, exc_info=True)
            log.append(f"{log_prefix}: transaction status check for op_id={op_id} failed ({_safe_apply_error_message(ex)}); retrying.")
            tx = None

        if tx is not None:
            if last_status in _ARTIST_RECONCILE_TERMINAL_SUCCESS_STATUSES:
                log.append(f"{log_prefix}: op_id={op_id} confirmed Completed via transaction status poll.")
                result = dict(tx)
                result.update({"ok": True, "operation_id": op_id, "status": last_status, "recovered_via_poll": True})
                return result
            if last_status in _ARTIST_RECONCILE_TERMINAL_FAILURE_STATUSES:
                log.append(f"{log_prefix}: op_id={op_id} confirmed {last_status} via transaction status poll.")
                return {"ok": False, "operation_id": op_id, "status": last_status,
                        "error": f"Engine reported {last_status}.", "recovered_via_poll": True}
            # Preview/Pending/Approved/Running: still genuinely in progress.

        if time.monotonic() >= deadline:
            log.append(
                f"{log_prefix}: op_id={op_id} is still running after "
                f"{BEETS_LONG_OPERATION_MAX_SECONDS:.0f}s of monitoring; it was NOT re-applied and "
                f"will be reconciled on the next run."
            )
            return {"ok": False, "operation_id": op_id, "status": last_status or "unknown",
                    "error": "Engine operation is still running; not re-applied.", "still_running": True}
        time.sleep(BEETS_LONG_OPERATION_POLL_SECONDS)


def _run_artist_folder_reconcile_for_alias_merge(
    source_folders: List[str], canonical: str, mb_artistid: str, log: List[str],
    *, fingerprint_confirmed: bool = False,
) -> Dict[str, Any]:
    """Delegate the artist-folder move/merge for an artist alias merge to the
    engine-owned artist_folder_reconcile_v1 transaction family -- the same
    family the "Clean: artist folder merge" maintenance feature
    (_apply_artist_folder_groups) uses -- instead of running per-album
    metadata-write + relocate calls that are not composed into a single
    rollback-capable transaction. No local fallback: if the engine is
    unreachable or rejects the plan, nothing is moved locally."""
    canonical_key = _artist_alias_key(canonical)
    target_path = MUSIC_ROOT / canonical
    candidates = []
    for name in sorted({n for n in source_folders if _artist_alias_key(n) != canonical_key}, key=lambda s: s.casefold()):
        src_path = MUSIC_ROOT / name
        if not src_path.is_dir():
            continue
        candidates.append({
            "source_path": str(src_path),
            "target_path": str(target_path),
            "source_name": name,
            "target_name": canonical,
            "source_mbid": mb_artistid,
            "target_mbid": mb_artistid,
            "fingerprint_confirmed": fingerprint_confirmed,
        })
    if not candidates:
        log.append("  No on-disk artist folder(s) to move (DB already reflects the merge).")
        return {"ok": True, "moved_files": 0, "quarantined_files": 0, "removed_dirs": 0}

    op_payload = {"root": str(MUSIC_ROOT), "mode": "scan_merge", "candidates": candidates}
    try:
        plan_res = composite_workflows.plan_artist_folder_reconcile(op_payload)
    except (BeetsUnavailableError, BeetsError) as ex:
        raise RuntimeError(f"Engine unavailable; artist folder move was not performed: {ex}") from ex
    if not plan_res.get("ok"):
        raise RuntimeError(f"Engine refused artist folder reconcile plan: {plan_res.get('error')}")
    op_id = plan_res.get("operation_id")
    if not op_id:
        log.append(f"  {plan_res.get('message') or 'No artist folder move was required.'}")
        return {"ok": True, "moved_files": 0, "quarantined_files": 0, "removed_dirs": 0}
    apply_res = _apply_artist_folder_reconcile_resilient(op_id, log, log_prefix="Artist folder move")
    if not apply_res.get("ok"):
        raise RuntimeError(f"Engine artist folder reconcile apply failed: {apply_res.get('error')}")
    log.append(
        f"  Engine artist_folder_reconcile_v1 (op_id={op_id}): "
        f"{apply_res.get('moved_files', 0)} file(s) moved, "
        f"{apply_res.get('quarantined_files', 0)} duplicate(s) quarantined, "
        f"{apply_res.get('removed_dirs', 0)} empty folder(s) removed."
    )
    return apply_res
