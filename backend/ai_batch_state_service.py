"""AI batch import state: per-batch store registry, folder status sets, worker registry and state commits (ARCH-001).
"""

from __future__ import annotations

import os, threading, time
from backend.ai_batch_state_store import AiBatchStateStore
from pathlib import Path
from typing import Any, Dict, Optional
from backend.app_runtime import WEB_MANAGER_DATA_DIR, _s

# ── ARCH-001 extracted code ──


_MUSIC_FORMAT_POLICY_HANDLED_MESSAGE = (
    "Downloaded audio does not match Music Format Preferences. "
    "Rejected files were handled according to settings."
)


_MUSIC_FORMAT_POLICY_REVIEW_NOTE = (
    "Audio rejected by Music Format Preferences; files were deleted or "
    "quarantined according to settings. Choose another source or update "
    "Music Format Preferences before retrying."
)


def _is_music_format_policy_handled_error(error_text: Any) -> bool:
    text = _s(error_text)
    return (
        _MUSIC_FORMAT_POLICY_HANDLED_MESSAGE in text
        or "Audio rejected: does not match Music Format Preferences; handled according to settings." in text
        or "Audio rejected by Music Format Preferences" in text
    )


def _music_format_policy_review_note(error_text: Any = "") -> str:
    text = _s(error_text).strip()
    if text.startswith("ERROR: "):
        text = text[7:].strip()
    if _MUSIC_FORMAT_POLICY_HANDLED_MESSAGE in text:
        return text
    return _MUSIC_FORMAT_POLICY_REVIEW_NOTE


# Wave 26 correction: a single global AiBatchStateStore whose own db_path
# attribute was reassigned in place (the previous _get_ai_batch_store())
# is not safe against concurrent access. Reassigning `.db_path` and then
# calling `_init_db()` are two separate, unsynchronized steps -- another
# thread (a lingering AI-batch worker thread from a previous request/test,
# or a genuine concurrent caller) can read the already-updated `.db_path`
# and open a connection to it before `_init_db()` has actually created the
# schema there, hitting "no such table: ai_batch_state". Re-importing
# app.py also does not happen per test module (Python caches modules), so
# whichever test module happened to import app.py first permanently owned
# this one mutable global for the rest of the process -- every other test
# module's own AI_BATCH_STATE_DIR override then raced to redirect it.
#
# Fixed with a small registry keyed by the resolved DB path: a distinct
# AI_BATCH_STATE_DIR always gets its own, independently-constructed
# AiBatchStateStore (construction -- which calls _init_db() synchronously
# -- happens once, under a lock, before the instance is ever published to
# the registry or returned to a caller), so no caller can ever observe a
# partially-initialized store, and no concurrent caller can silently
# redirect an existing store out from under another thread still using it.
_ai_batch_store_registry_lock = threading.Lock()


_ai_batch_store_registry: Dict[str, AiBatchStateStore] = {}


def _get_ai_batch_store() -> AiBatchStateStore:
    """Return the AiBatchStateStore for the current AI_BATCH_STATE_DIR,
    re-read on every call (deployments/tests may vary it), from a registry
    keyed by the resolved DB path rather than by mutating one shared
    object's db_path. Guaranteed schema-initialized before returning."""
    active_dir = Path(os.environ.get("AI_BATCH_STATE_DIR", str(WEB_MANAGER_DATA_DIR / "ai_batch_jobs")))
    db_path = active_dir / "ai_batch_state.db"
    key = os.path.normpath(os.path.abspath(str(db_path)))
    with _ai_batch_store_registry_lock:
        store = _ai_batch_store_registry.get(key)
        if store is None:
            store = AiBatchStateStore(db_path)
            _ai_batch_store_registry[key] = store
        return store


_AI_BATCH_MAX_AI_WORKERS = max(1, min(10, int(os.environ.get("AI_BATCH_MAX_AI_WORKERS", "3") or "3")))


_ai_batch_state_lock = threading.RLock()


# Sole arbiter of "is a worker starting or running for this batch_job_id in
# this process," authoritative for the worker's *entire* lifetime -- from
# the moment startup is reserved until the worker thread actually exits.
#
# An earlier version of this registry (_ai_batch_active_starts) was released
# as soon as _start_ai_batch_job()'s own start sequence finished (job_id
# persisted, worker unblocked), on the theory that JobStore's own
# job.status == "running" would remain the reliable signal after that point.
# That left a real post-start/pre-heartbeat window open: a second recover
# request could land after the first _start_ai_batch_job() call had already
# returned (reservation released) but before the newly-unblocked worker
# thread had reconciled/committed its first post-start state. During that
# window _ai_batch_recalculate_batch_state's finalize branch -- reading the
# still-pre-reconciliation, all-terminal-looking folder_states snapshot --
# stamps state["worker_alive"] = False, so the second request's own
# worker_alive check (fed by that stale snapshot) failed to see the new
# worker as active and started a second one for the same batch_job_id.
#
# Values:
#   missing from the dict  -- no worker reserved or active
#   None                   -- startup reserved, job_id not allocated yet
#   <job_id> (str)          -- worker created and active
#
# This registry is deliberately independent of state-file content -- it is
# the sole source of truth for "is a worker already starting/running for
# this batch," so it stays correct even when every thread's own state
# snapshot is stale.
#
# Process-local only: correct because this app's supported runtime is one
# Waitress process with a thread pool (see `if __name__ == "__main__":`,
# `waitress.serve(..., threads=_env_int("WEBCONTROL_THREADS", ...))`), not
# multiple worker processes or containers sharing one state directory. If
# that deployment model ever changes, this lock alone would no longer be
# sufficient and would need a durable/file-based reservation instead.
_ai_batch_worker_lock = threading.Lock()


_ai_batch_active_workers: Dict[str, Optional[str]] = {}


def _ai_batch_worker_registered(batch_job_id: str) -> bool:
    """True if a worker is startup-reserved or actively running for
    batch_job_id in this process, per the process-local active-worker
    registry -- independent of persisted state-file content."""
    if not batch_job_id:
        return False
    with _ai_batch_worker_lock:
        return batch_job_id in _ai_batch_active_workers


_AI_BATCH_TERMINAL_STATUSES = {"completed", "completed_with_warnings", "failed", "canceled"}


_AI_BATCH_QUEUED_FOLDER_STATUSES = {"queued", "ai_queued"}


_AI_BATCH_RUNNING_FOLDER_STATUSES = {"claimed", "scanning", "ai_running", "importing", "retrying"}


_AI_BATCH_DECISION_READY_STATUSES = {"ai_completed"}


_AI_BATCH_UNFINISHED_FOLDER_STATUSES = (
    _AI_BATCH_QUEUED_FOLDER_STATUSES
    | _AI_BATCH_RUNNING_FOLDER_STATUSES
    | _AI_BATCH_DECISION_READY_STATUSES
)


_AI_BATCH_STALE_AI_STATUSES = {"ai_running"}


_AI_BATCH_IMPORTED_FOLDER_STATUSES = {"completed", "imported"}


_AI_BATCH_REVIEW_FOLDER_STATUSES = {"review_created", "review_required"}


_AI_BATCH_POLICY_WARNING_STATUSES = {"policy_rejected", "completed_with_fallback", "handled_warning"}


_AI_BATCH_REPLACEMENT_FOLDER_STATUSES = {"replacement_queued", "replacement_unavailable"}


_AI_BATCH_FAILED_FOLDER_STATUSES = {"failed", "import_failed", "ai_failed", "timed_out"}


_AI_BATCH_SKIPPED_FOLDER_STATUSES = {"skipped", "canceled"}


_AI_BATCH_TERMINAL_FOLDER_STATUSES = (
    _AI_BATCH_IMPORTED_FOLDER_STATUSES
    | _AI_BATCH_REVIEW_FOLDER_STATUSES
    | _AI_BATCH_POLICY_WARNING_STATUSES
    | _AI_BATCH_REPLACEMENT_FOLDER_STATUSES
    | _AI_BATCH_FAILED_FOLDER_STATUSES
    | _AI_BATCH_SKIPPED_FOLDER_STATUSES
)


_AI_BATCH_RETRYABLE_FOLDER_STATUSES = {"failed", "import_failed", "ai_failed", "timed_out", "replacement_unavailable"}


def _ai_batch_write_state(state: Dict[str, Any]) -> None:
    with _ai_batch_state_lock:
        expected_rev = state.get("revision")
        updated = _get_ai_batch_store().save_batch_state(state, expected_revision=expected_rev)
        state["revision"] = updated.get("revision", 1)
        state["schema_version"] = updated.get("schema_version", 1)


def _ai_batch_public_state(state: Dict[str, Any]) -> Dict[str, Any]:
    public = dict(state)
    folder_states = state.get("folder_states") or {}
    public_folders = []
    for folder in folder_states.values():
        item = dict(folder or {})
        item.pop("ai_result", None)
        public_folders.append(item)
    public["folders"] = sorted(public_folders, key=lambda item: _s(item.get("source_folder")))[:250]
    public.pop("folder_states", None)
    return public


def _ai_batch_effective_folder_status(folder: Dict[str, Any]) -> str:
    status = _s((folder or {}).get("status")).strip()
    step = _s((folder or {}).get("current_step")).strip().casefold()
    reason = _s((folder or {}).get("failure_reason") or (folder or {}).get("ai_suggest_error"))
    if status == "ai_completed" and ((folder or {}).get("review_item_id") or "review" in step):
        return "review_created"
    if status == "ai_completed" and step in {"imported", "completed", "done"}:
        return "imported"
    if status == "failed" and _is_music_format_policy_handled_error(reason):
        return "policy_rejected"
    return status


def _ai_batch_normalize_folder_outcome(folder: Dict[str, Any]) -> str:
    status = _ai_batch_effective_folder_status(folder)
    reason = _s((folder or {}).get("failure_reason") or (folder or {}).get("ai_suggest_error"))
    if status == "policy_rejected" and folder.get("status") != "policy_rejected":
        folder["status"] = "policy_rejected"
        folder["current_step"] = "audio policy handled"
        folder["failure_reason"] = _music_format_policy_review_note(reason)
    elif status == "review_created" and folder.get("status") == "ai_completed":
        folder["status"] = "review_created"
    elif status == "imported" and folder.get("status") == "ai_completed":
        folder["status"] = "imported"
    return _s(folder.get("status") or status).strip()


def _ai_batch_recompute_counts(state: Dict[str, Any]) -> None:
    folders = list((state.get("folder_states") or {}).values())
    status_counts: Dict[str, int] = {}
    for folder in folders:
        status = _ai_batch_normalize_folder_outcome(folder)
        status_counts[status] = status_counts.get(status, 0) + 1
    state["total_folders_found"] = len(folders)
    state["folders_running"] = sum(status_counts.get(status, 0) for status in _AI_BATCH_RUNNING_FOLDER_STATUSES)
    state["folders_queued"] = sum(status_counts.get(status, 0) for status in _AI_BATCH_QUEUED_FOLDER_STATUSES)
    state["folders_completed"] = sum(status_counts.get(status, 0) for status in _AI_BATCH_IMPORTED_FOLDER_STATUSES)
    state["folders_review"] = sum(status_counts.get(status, 0) for status in _AI_BATCH_REVIEW_FOLDER_STATUSES)
    state["folders_warning"] = sum(status_counts.get(status, 0) for status in _AI_BATCH_POLICY_WARNING_STATUSES)
    state["folders_replacement_queued"] = status_counts.get("replacement_queued", 0)
    state["folders_replacement_unavailable"] = status_counts.get("replacement_unavailable", 0)
    state["folders_failed"] = sum(status_counts.get(status, 0) for status in _AI_BATCH_FAILED_FOLDER_STATUSES)
    state["folders_skipped"] = sum(status_counts.get(status, 0) for status in _AI_BATCH_SKIPPED_FOLDER_STATUSES)
    state["folders_unfinished"] = sum(status_counts.get(status, 0) for status in _AI_BATCH_UNFINISHED_FOLDER_STATUSES)
    # Counted per-folder rather than via status_counts: a folder that has
    # exhausted _AI_BATCH_MAX_FOLDER_RETRIES keeps its original status
    # (e.g. "failed") for operator-UI compatibility, so status alone can't
    # distinguish it from one that hasn't been retried yet. retry_exhausted
    # is the field that must exclude it here, or a folder stuck at its cap
    # would keep inflating folders_retryable forever.
    state["folders_retryable"] = sum(
        1 for folder in folders
        if _ai_batch_normalize_folder_outcome(folder) in _AI_BATCH_RETRYABLE_FOLDER_STATUSES
        and not folder.get("retry_exhausted")
    )
    state["folders_attention"] = (
        state["folders_review"]
        + state["folders_warning"]
        + state["folders_replacement_unavailable"]
        + state["folders_failed"]
    )
    state["folders_processed"] = sum(status_counts.get(status, 0) for status in _AI_BATCH_TERMINAL_FOLDER_STATUSES)
    state["folder_status_counts"] = dict(sorted(status_counts.items()))
    running = [f for f in folders if _ai_batch_effective_folder_status(f) in _AI_BATCH_RUNNING_FOLDER_STATUSES]
    state["current_folder_names"] = [Path(_s(f.get("source_folder"))).name for f in running[:_AI_BATCH_MAX_AI_WORKERS]]


def _ai_batch_terminal_summary(state: Dict[str, Any]) -> str:
    return (
        f"{int(state.get('folders_processed') or 0)} folder(s) processed; "
        f"{int(state.get('folders_attention') or 0)} need attention."
    )


def _ai_batch_recalculate_batch_state(state: Dict[str, Any], log: Optional[list] = None) -> bool:
    previous_status = _s(state.get("status"))
    _ai_batch_recompute_counts(state)
    total = int(state.get("total_folders_found") or 0)
    unfinished = int(state.get("folders_unfinished") or 0)
    processed = int(state.get("folders_processed") or 0)
    if previous_status in {"canceled"}:
        return False
    if unfinished > 0:
        # Self-heal a contradictory persisted state: folders genuinely still
        # unfinished (e.g. left at "ai_queued" by an interrupted/buggy prior
        # run) but state["status"] stuck at a terminal value from an earlier
        # premature finalize. Nothing else ever walks status back off a
        # terminal value, so without this a batch in this state stays stuck
        # forever -- recover/retry both short-circuit to a no-op "reconnect"
        # because they trust the (wrong) terminal status. Reopening to
        # "running" lets the existing stale-heartbeat detection in
        # _ai_batch_reconcile_state correctly classify it as recoverable.
        if previous_status in _AI_BATCH_TERMINAL_STATUSES:
            state["status"] = "running"
            state["current_step"] = "reopened: unfinished folder work found"
            state["completed_at"] = None
            if log is not None:
                log.append(
                    f"Batch status corrected: {unfinished} folder(s) still unfinished; "
                    f"reopening from {previous_status}."
                )
            return True
        return False
    if total <= 0 or processed < total:
        return False
    attention = int(state.get("folders_attention") or 0) + int(state.get("folders_skipped") or 0)
    next_status = "completed_with_warnings" if attention else "completed"
    changed = (
        previous_status != next_status
        or not state.get("completed_at")
        or bool(state.get("recovery_state"))
        or bool(state.get("worker_alive"))
        or bool(state.get("current_folder_names"))
        or bool(state.get("last_error"))
    )
    if changed:
        state["status"] = next_status
        state["current_step"] = "completed with warnings" if attention else "completed"
        state["completed_at"] = state.get("completed_at") or time.time()
        state["last_error"] = ""
        state["recovery_state"] = ""
        state["batch_summary"] = _ai_batch_terminal_summary(state)
        state["worker_alive"] = False
        state["current_folder_names"] = []
        if log is not None:
            log.append(f"Batch finalized: {state['current_step']} — {state['batch_summary']}")
    return changed


def _ai_batch_commit(state: Dict[str, Any], update_state=None, *, heartbeat: bool = True) -> None:
    now = time.time()
    if heartbeat:
        state["heartbeat_at"] = now
    state["updated_at"] = now
    _ai_batch_recalculate_batch_state(state)
    _ai_batch_recompute_counts(state)
    with _ai_batch_state_lock:
        _ai_batch_write_state(state)
    if update_state:
        update_state(_ai_batch_public_state(state))


def _ai_batch_mark_folder(state: Dict[str, Any], folder_id: str, **updates) -> Dict[str, Any]:
    folder = (state.get("folder_states") or {}).get(folder_id)
    if not folder:
        return {}
    folder.update(updates)
    status = _ai_batch_effective_folder_status(folder)
    if status in (_AI_BATCH_IMPORTED_FOLDER_STATUSES | _AI_BATCH_REVIEW_FOLDER_STATUSES | _AI_BATCH_POLICY_WARNING_STATUSES | _AI_BATCH_REPLACEMENT_FOLDER_STATUSES):
        state["last_completed_folder"] = folder.get("source_folder", "")
    if status in _AI_BATCH_FAILED_FOLDER_STATUSES:
        state["last_failed_folder"] = folder.get("source_folder", "")
        state["last_failed_reason"] = folder.get("failure_reason") or folder.get("ai_suggest_error") or "failed"
    return folder


def _ai_batch_mark_stale_ai_running_folders(state: Dict[str, Any], reason: str) -> bool:
    changed = False
    now = time.time()
    for fid, folder in list((state.get("folder_states") or {}).items()):
        if folder.get("status") not in _AI_BATCH_STALE_AI_STATUSES:
            continue
        _ai_batch_mark_folder(
            state,
            fid,
            status="timed_out",
            current_step="stale AI suggestion timed out",
            ai_suggest_status="timed_out",
            ai_suggest_completed_at=now,
            ai_suggest_error=reason,
            failure_reason=reason,
        )
        changed = True
    return changed


def _ai_batch_mark_orphaned_ai_completed_for_review(state: Dict[str, Any]) -> bool:
    changed = False
    for fid, folder in list((state.get("folder_states") or {}).items()):
        if folder.get("status") != "ai_completed" or not folder.get("ai_result"):
            continue
        _ai_batch_mark_folder(
            state,
            fid,
            status="review_required",
            current_step="AI suggestion ready for review",
            review_item_id=folder.get("review_item_id") or fid,
        )
        changed = True
    return changed
