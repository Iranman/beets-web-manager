"""Plan/Apply/Rollback orchestration over the shared TransactionStore (ARCH-001).
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from backend.app_runtime import _app_logger, EDITABLE_FIELDS, _s
from backend.library_service import _require_attach_stage_success
from backend.transaction_engine import acting_as, current_actor, job_overlay, metadata_diff
from backend.beets_adapter import lib
import backend.composite_workflows as composite_workflows
from backend.matching_service import _invalidate_lib_cache
from backend.app_runtime import jobs, transactions
from backend.job_service import _call_job_fn, _job_progress
from job_engine import DuplicateJobError, cancel_honoured, cancel_requested, is_read_only_job, reports_failure
from backend.import_review_service import _metadata_transaction_pending_fields

from backend.auth_service import _transaction_user_label

# ── ARCH-001 extracted code ──


_TRANSACTION_LABEL_BLOCKLIST = (
    "discography",
    "plex library refresh",
)


def _word(text: str, word: str) -> bool:
    """``word`` as a whole word: "Artist" is not "art", "repair" is not "ai" (#325 F5)."""
    return re.search(rf"(?<![a-z]){word}(?![a-z])", text) is not None


def _transaction_operation_from_job(label: str, metadata: Dict[str, Any]) -> Optional[str]:
    text = f"{metadata.get('type', '')} {label}".strip().lower()
    if not text or any(blocked in text for blocked in _TRANSACTION_LABEL_BLOCKLIST):
        return None
    if "playlist" in text:
        if any(word in text for word in ("import", "place", "repair", "sync", "quality")):
            return "Playlist Import"
    if "music-format" in text or "replace" in text or "replacement" in text:
        return "Replace"
    if "fetchart" in text or "artwork" in text or _word(text, "art"):
        return "Artwork Update"
    if "dedup" in text or "duplicate" in text:
        return "Duplicate Removal"
    if "merge artist" in text or "artist-folder-merge" in text or "merge-artist" in text:
        return "Merge Artist"
    if "merge album" in text or "merge-split" in text or "release group merge" in text:
        return "Merge Album"
    if "split" in text:
        return "Split Album"
    if "import" in text or "tag+import" in text or "import+tag" in text:
        return "Import"
    if "mbsync" in text or "mbid" in text or "musicbrainz" in text or "mbsubmit" in text:
        return "MusicBrainz Match"
    if "acoustid" in text or "fingerprint" in text:
        return "AcoustID Match"
    if _word(text, "ai") or "suggest" in text:
        return "AI Suggestion"
    if "move" in text:
        return "Move"
    if "rename" in text or "normalize" in text or "stamp" in text:
        return "Rename"
    if "delete" in text or "remove" in text or "cleanup" in text or "clean" in text:
        return "Library Cleanup"
    if "repair" in text or "fix" in text:
        return "Repair"
    if "scan" in text or "rescan" in text:
        return "Rescan"
    if text.startswith("beet "):
        return "Repair"
    return None


_JOB_WRAPPER_REASON = "Created from the existing job workflow before library changes are applied."
_JOB_WRAPPER_MARK = "job_wrapper"


def _transaction_create_for_job(label: str, metadata: Optional[Dict[str, Any]], command: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    metadata = dict(metadata or {})
    explicit = metadata.get("transaction")
    if explicit is False:
        return None
    explicit_payload = explicit if isinstance(explicit, dict) else {}
    operation_type = (
        explicit_payload.get("operation_type")
        or metadata.get("transaction_operation")
        or metadata.get("operation_type")
        or _transaction_operation_from_job(label, metadata)
    )
    if not operation_type:
        return None
    text = f"{metadata.get('type', '')} {label}".lower()
    dry_run = bool(
        explicit_payload.get("dry_run")
        or metadata.get("dry_run")
        or metadata.get("preview")
        or "dry run" in text
        or "preview" in text
    )
    summary = str(explicit_payload.get("summary") or label or operation_type)
    reason = str(explicit_payload.get("reason") or _JOB_WRAPPER_REASON)
    source = str(explicit_payload.get("source") or metadata.get("type") or "job")
    tx_metadata = {k: v for k, v in metadata.items() if k != "transaction"}
    tx_metadata[_JOB_WRAPPER_MARK] = True  # only its job settles it (see _orphaned_job_wrapper)
    if command:
        tx_metadata["command"] = [str(part) for part in command]
    return transactions.create(
        operation_type=str(operation_type),
        status="Running",
        dry_run=dry_run,
        summary=summary,
        reason=reason,
        source=source,
        confidence=explicit_payload.get("confidence") if isinstance(explicit_payload.get("confidence"), dict) else None,
        rollback_available=bool(explicit_payload.get("rollback_available", False)),
        rollback_reason=str(explicit_payload.get("rollback_reason") or "Rollback data has not been captured for this workflow yet."),
        metadata=tx_metadata,
    )


#: How often a reconcile that could not finish is retried.
_RECONCILE_RETRY_SECONDS = 30.0


def _jobs_by_transaction(job_store) -> Dict[str, List[Any]]:
    """The jobs held in memory per transaction id, newest first. Never touches disk."""
    grouped: Dict[str, List[Any]] = {}
    for job in job_store.all():
        tx_id = (getattr(job, "metadata", None) or {}).get("transaction_id")
        if tx_id:
            grouped.setdefault(str(tx_id), []).append(job)
    return grouped


def _speaking_job(candidates: List[Any]) -> Any:
    """The job that speaks for a transaction: a live one, else the newest."""
    return next((j for j in candidates if j.status == "running"), candidates[0])


def overlay_running_jobs(rows: List[Dict[str, Any]], keys: Optional[Tuple[str, ...]] = None) -> None:
    """Show each Running row as its job would leave it (live log, or an outcome
    not yet written). In memory only, for GET routes: nothing is written, and a
    job record is never lazily loaded because loading can resolve and rewrite it."""
    running = [row for row in rows if row.get("status") == "Running"]
    if not running:
        return
    grouped = _jobs_by_transaction(jobs)
    for row in running:
        candidates = grouped.get(str(row.get("id")))
        if candidates:
            overlaid = job_overlay(row, _speaking_job(candidates))
            row.update(overlaid if keys is None else {k: overlaid[k] for k in keys if k in overlaid})


def _is_job_wrapper(row: Dict[str, Any]) -> bool:
    """Created by the job hook for a job and settled by nothing else: no engine
    evidence that transaction_recovery.sweep resolves. Records older than the
    marker are recognised by the hook's default reason."""
    meta = row.get("metadata") or {}
    if (meta.get("mutation_family") or meta.get("engine_request") or meta.get("engine_operation_id")
            or meta.get("rollback_request") or (meta.get("engine_result") or {}).get("mutation_started")
            or meta.get("mutation_started") or meta.get("filesystem_mutated")):
        return False  # transaction_recovery.sweep owns these
    return bool(meta.get(_JOB_WRAPPER_MARK)) or row.get("reason") == _JOB_WRAPPER_REASON


def _orphaned_job_wrapper(store, row: Dict[str, Any], before: float) -> None:
    """A job-wrapper transaction an earlier process left Running for which no
    job record exists, in memory or on disk (the process died before the job
    was persisted, or the record was deleted). Settled as job_engine settles
    the job it lost: Failed when that job type is read-only, else Recovery
    Required. Never Completed and never replayed."""
    meta = row.get("metadata") or {}
    if is_read_only_job(meta):
        status, note = "Failed", ("Recovered after restart: the read-only job for this transaction was "
                                  "interrupted and its job record is gone; nothing was changed. Start it again.")
    else:
        status, note = "Recovery Required", (
            "Recovered after restart: the process stopped while this job was running and its job record "
            "is gone, so whether it changed the library cannot be proven; check the library before retrying.")
    with store._lock:  # re-checked under the lock: a write since the list means it is live
        if (store.get(str(row["id"])).get("updated_at") or 0) >= before:
            return
        store.transition(str(row["id"]), "Running", status, append_logs=[note])


def _reconcile_running_transactions(job_store, store, before: Optional[float] = None,
                                    scan_done: Optional[bool] = None) -> bool:
    """Settle every Running transaction whose job has ended, whichever process
    or path lost the update. True when every record could be processed.

    Only a job that names the transaction in its metadata speaks for it, and a
    live job keeps it Running. A Running transaction with no such job is left
    alone: engine operations run without one, so its absence proves nothing.
    The exception is a job-wrapper transaction last written before ``before``
    (this process's start) with no job record in memory, by id, or among the
    tail sweep's on-disk records (``tail_transaction_jobs``): no job exists to
    settle it. Pass ``before`` only once the tail sweep has finished.

    The tail snapshots speak only for job-wrapper transactions: an engine
    transaction is reached only by jobs in memory or named by id (#545).

    ``scan_done`` says whether the startup job scan has finished, which
    decides how unbound rows are logged; it defaults to ``before is not
    None``, but a retry pass after judging passes it without ``before``."""
    if scan_done is None:
        scan_done = before is not None
    grouped = _jobs_by_transaction(job_store)
    tail = dict(getattr(job_store, "tail_transaction_jobs", None) or {})
    rows = _running_rows(store)
    ok, unbound = True, []
    for row in rows:
        tx_id = str(row["id"])
        meta = row.get("metadata") or {}
        candidates = list(grouped.get(tx_id) or [])
        if tail.get(tx_id) and _is_job_wrapper(row):
            known = {c.job_id for c in candidates}
            candidates += [j for j in tail[tx_id] if j.job_id not in known]
        # Beyond the startup window a job is on disk only; get() loads it by id.
        for job_id in (meta.get("rollback_job_id"), row.get("originating_job"), meta.get("job_id")):
            job = job_store.get(job_id) if job_id else None
            if (job is not None and job.job_id not in {c.job_id for c in candidates}
                    and str((getattr(job, "metadata", None) or {}).get("transaction_id")) == tx_id):
                candidates.append(job)
        if not candidates:
            if before is not None and (row.get("updated_at") or 0) < before and _is_job_wrapper(row):
                try:
                    _orphaned_job_wrapper(store, row, before)
                except Exception as ex:
                    ok = False
                    _app_logger.warning("transaction sync failed for %s, will retry: %s", tx_id, type(ex).__name__)
                continue
            unbound.append(tx_id)
            continue
        candidates.sort(key=lambda j: j.created_at, reverse=True)
        job = _speaking_job(candidates)
        if job.status == "running":
            continue
        try:
            store.update_from_job(tx_id, job)
        except Exception as ex:
            ok = False
            _app_logger.warning("transaction sync failed for %s, will retry: %s", tx_id, type(ex).__name__)
    if unbound and not scan_done:  # the startup job scan is still running
        _app_logger.debug("%d Running transaction(s) have no job record yet, pending the startup job scan: %s",
                          len(unbound), ", ".join(unbound[:20]))
    elif unbound:
        _app_logger.warning("%d Running transaction(s) have no job record and were left unchanged: %s",
                            len(unbound), ", ".join(unbound[:20]))
    return ok


#: Rows per ``store.list`` page while collecting Running transactions.
_RUNNING_PAGE = 500


def _running_rows(store) -> List[Dict[str, Any]]:
    """Every Running transaction, as one consistent snapshot (#545).

    Offsets shift when a row leaves Running between page reads, so the store
    lock is held across the whole paged read: every store write takes that
    lock, and the web app is one process. It is reentrant (``list`` takes it
    too), nothing else is acquired while it is held, and it is released
    before any transaction is settled, so it cannot deadlock against a
    writer or the settle path. The hold is one directory read per page."""
    rows: List[Dict[str, Any]] = []
    with store._lock:
        while True:
            page, total = store.list(status="Running", offset=len(rows), limit=_RUNNING_PAGE)
            rows += page
            if not page or len(rows) >= total:
                return rows


def _install_transaction_job_hooks() -> None:
    if getattr(jobs, "_transaction_hooks_installed", False):
        return
    original_start_python = jobs.start_python
    job_store, tx_store = jobs, transactions
    reconcile_due = threading.Event()
    reconcile_due.set()
    started = time.time()  # a job-wrapper transaction last written before this has no live job here
    orphans = {"judged": False}  # orphans are judged in one successful pass, not every tick

    def reconcile_tick():
        """Off the startup path (startup stays O(job window), ARCH-004). Due
        at boot, until the job tail sweep has finished, and after any failed
        write; otherwise a tick costs one flag check."""
        if not reconcile_due.is_set():
            return
        reconcile_due.clear()
        # Read first: the pass must see all of the tail. A sweep thread that
        # died is not a finished one (#545), so require its completion flag.
        scanned = getattr(job_store, "tail_scan_complete", None)
        settled = scanned is None or scanned.is_set()
        try:
            # The tail sweep has finished: every on-disk record naming a
            # transaction is in memory or in tail_transaction_jobs.
            # Judging orphans happens once; "the scan finished" (``settled``
            # here, never unset) holds for every later retry pass too (F2).
            judge = settled and not orphans["judged"]
            settled = _reconcile_running_transactions(job_store, tx_store, started if judge else None,
                                                      scan_done=settled) and settled
            if judge and settled:
                orphans["judged"] = True
                (getattr(job_store, "tail_transaction_jobs", None) or {}).clear()  # consumed; do not hold them
        except Exception:
            settled = False
            raise
        finally:
            if not settled:
                reconcile_due.set()

    def start_python_with_transaction(fn, label="", metadata=None):
        metadata_payload = dict(metadata or {})
        # NB-3: transactions the job creates on its own thread record whoever
        # started it: the parent job's actor, this request's verified user, or
        # "system" when neither did (a schedule).
        actor, inner = current_actor(), fn

        def fn(log, cancel=None, update_state=None):
            with acting_as(actor):
                return _call_job_fn(inner, log, cancel, update_state)

        # The duplicate check, the transaction and the start happen under one
        # store lock, so a refused start never records a transaction (QA F3).
        with jobs.start_guard(metadata_payload):
            existing = jobs.find_duplicate(metadata_payload)
            if existing is not None:  # refuse before a transaction is recorded (BA-6)
                raise DuplicateJobError(existing)
            return _start_with_transaction(fn, label, metadata_payload)

    def _start_with_transaction(fn, label, metadata_payload):
        tx = _transaction_create_for_job(label, metadata_payload)
        tx_id = tx.get("id") if tx else ""
        settles = transactions  # the store this job's transaction lives in
        if tx_id:
            metadata_payload["transaction_id"] = tx_id

            def wrapped(log, cancel=None, update_state=None):
                # The final status mirrors job_engine's own rule
                # (cancel_honoured): Cancelled only when the job stopped
                # because of the cancel. A request the job never saw cannot
                # undo work it already did, so the real outcome stands (D4).
                # Job sync no longer corrects a final status afterwards
                # (QA-217-7).
                progress = {"mutated": False}

                def tracked_state(updates=None, **kwargs):
                    # #317: a mutating job publishes ``mutation_started`` once
                    # it may have changed the library; a later cooperative
                    # cancel is then never audited as "nothing happened".
                    if (updates or {}).get("mutation_started") or kwargs.get("mutation_started"):
                        progress["mutated"] = True
                    if update_state is not None:
                        return update_state(updates, **kwargs)
                    return None

                def note(mutation_started=False, outcome_unknown=False):
                    # job_service.note_job_progress: a child job's Beets
                    # operation changed the library or outlived the wait (#317).
                    progress["mutated"] = progress["mutated"] or bool(mutation_started)
                    progress["unknown"] = progress.get("unknown") or bool(outcome_unknown)

                def settle(status, result=None):
                    if progress.get("unknown"):
                        transactions.update(tx_id, metadata={"engine_result": {"mutation_started": True,
                                                                               "outcome_unknown": True}})
                        transactions.append_log(tx_id, "A child job was still running a library operation when this "
                                                       f"job stopped waiting for it: recorded as Recovery Required, "
                                                       f"not {status}. Check the child job before retrying.")
                        progress["partial"] = True
                        return "Recovery Required"
                    return cancelled(result) if status == "Cancelled" else status

                def cancelled(result=None):
                    if not (progress["mutated"] or (isinstance(result, dict) and result.get("mutation_started"))):
                        return "Cancelled"
                    transactions.update(tx_id, metadata={"engine_result": {"mutation_started": True,
                                                                           "partial_cancel": True}})
                    transactions.append_log(tx_id, "Cancelled part-way after it started changing the library: "
                                                   "recorded as Failed (partially applied), not Cancelled. "
                                                   "Check the log for what changed.")
                    progress["partial"] = True
                    return "Failed"

                def finish(status):
                    transactions.update(tx_id, status=status)
                    if status != "Cancelled" and cancel_requested(cancel) and not progress.get("partial"):
                        transactions.append_log(tx_id, "Cancel requested, but the job had already done "
                                                       f"its work: recorded as {status}.")

                transactions.update(tx_id, status="Running")
                token = _job_progress.set(note)
                try:
                    result = _call_job_fn(fn, log, cancel, tracked_state)
                except Exception as ex:
                    finish(settle("Cancelled" if cancel_honoured(cancel, ex) else "Failed"))
                    transactions.append_log(tx_id, f"ERROR: {ex}")
                    raise
                finally:
                    _job_progress.reset(token)
                next_status = "Preview" if metadata_payload.get("dry_run") or metadata_payload.get("preview") else "Completed"
                if reports_failure(result):
                    next_status = "Failed"  # BA-3: the same rule as job_engine
                if isinstance(result, dict) and result.get("outcome_partly_unknown"):
                    next_status = "Recovery Required"  # lost contact mid-run: not provably Failed (#326 F2)
                finish(settle("Cancelled" if cancel_honoured(cancel) else next_status, result))
                return result
        else:
            wrapped = fn
        try:
            job = original_start_python(wrapped, label=label, metadata=metadata_payload)
        except Exception:
            if tx_id:  # no job runs, so the transaction must not stay Running (#229)
                transactions.update(tx_id, status="Failed")
            raise
        if tx_id:
            transactions.attach_job(tx_id, job.job_id)
        bound_tx = metadata_payload.get("transaction_id")
        if bound_tx:
            def settle_transaction(done):
                # The write point for job -> transaction state: once, when the
                # job ends, for every job naming a transaction, including one
                # whose route claimed it. Reading a transaction never writes it.
                try:
                    settles.update_from_job(str(bound_tx), done)
                except Exception:
                    reconcile_due.set()
                    raise

            job.add_done_callback(settle_transaction)
        return job

    jobs.start_python = start_python_with_transaction
    jobs._transaction_hooks_installed = True
    # A job the last process died in never reaches its done callback.
    jobs.every("transaction-reconcile", _RECONCILE_RETRY_SECONDS, reconcile_tick)


#: Statuses POST /api/transactions/<id>/cancel cancels outright (compare-and-set).
CANCELLABLE_STATUSES = ("Pending", "Preview", "Approved")


def transaction_cancel_eligibility(tx: Dict[str, Any]) -> Dict[str, Any]:
    """Whether the cancel route will act on ``tx`` (#356): the route and the
    detail payload's ``cancel`` field both use this, so they cannot drift.
    ``allowed``; ``mode`` "transaction" (not started: cancelled, nothing
    changes), "engine" (a library sync Beets is running: the cancel is sent to
    its operation) or None; ``code``; ``reason`` (operator text)."""
    status = tx.get("status")
    meta = tx.get("metadata") or {}
    if status in CANCELLABLE_STATUSES:
        return {"allowed": True, "mode": "transaction", "code": "not_started",
                "reason": "Not started yet: cancelling it changes nothing."}
    if (meta.get("mutation_family") == composite_workflows.MBSYNC_LIBRARY_FAMILY
            and meta.get("engine_operation_id") and status in ("Running", "Recovery Required")):
        return {"allowed": True, "mode": "engine", "code": "engine_operation",
                "engine_operation_id": str(meta["engine_operation_id"]),
                "reason": "Beets stops this sync after the album it is on; albums already synced keep their changes."}
    return {"allowed": False, "mode": None, "code": "not_cancellable",
            "reason": f"Only a Pending, Preview or Approved transaction can be cancelled (this one is {status})."}


def _sync_transactions_from_jobs() -> None:
    for job in jobs.all():
        metadata = getattr(job, "metadata", {}) or {}
        tx_id = metadata.get("transaction_id")
        if not tx_id:
            continue
        try:
            transactions.update_from_job(str(tx_id), job)
        except Exception as ex:
            _app_logger.debug("transaction sync failed for %s: %s", tx_id, ex)


# -- Transaction helpers for item metadata changes ---------------------------

def _item_transaction_fields(item) -> Dict[str, Any]:
    values: Dict[str, Any] = {}
    for field, _label in EDITABLE_FIELDS:
        try:
            values[field] = _s(getattr(item, field, "") or "")
        except Exception:
            values[field] = ""
    try:
        values["path"] = _s(getattr(item, "path", "") or "")
    except Exception:
        values["path"] = ""
    return values


def _item_metadata_transaction_payload(iid: int, fields: Dict[str, Any]) -> Tuple[Any, Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    item = lib.get_item(iid)
    if not item:
        raise KeyError(f"Item {iid} not found")
    current = _item_transaction_fields(item)
    proposed = dict(current)
    for key, value in fields.items():
        proposed[str(key)] = _s(value)
    diff_rows = metadata_diff(current, proposed)
    change = {
        "id": f"item:{iid}",
        "operation": "Metadata Update",
        "artist": proposed.get("artist") or current.get("artist") or "",
        "album": proposed.get("album") or current.get("album") or "",
        "track": proposed.get("title") or current.get("title") or f"item {iid}",
        "current_metadata": current,
        "new_metadata": proposed,
        "metadata_diff": diff_rows,
        "filesystem": [],
        "confidence": {"overall": 1.0},
        "reason": "Manual operator metadata edit.",
        "source": "User supplied fields",
    }
    rollback_op = {
        "type": "metadata_restore",
        "item_id": iid,
        "fields": {row["field"]: row.get("old") for row in diff_rows if row.get("changed")},
        "write_tags": False,
        "reason": "Restore metadata values captured before the edit.",
    }
    return item, current, proposed, {"change": change, "rollback_op": rollback_op, "diff_rows": diff_rows}


def _start_metadata_apply_transaction(transaction_id: str):
    tx = transactions.get(transaction_id)
    if tx.get("operation_type") != "Metadata Update":
        raise ValueError("Only metadata update transactions can be applied by this endpoint.")
    status = tx.get("status")
    if status in ("Preview", "Pending"):
        raise ValueError("Approve the transaction before applying it.")
    if status != "Approved":  # D15: a replay is told what happened, not to approve
        raise ValueError(f"This transaction cannot be applied again (status is {status}); nothing was changed.")
    metadata = tx.get("metadata") or {}
    item_id = int(metadata.get("item_id") or 0)
    if not item_id:
        raise ValueError("Metadata transaction is missing item id.")
    fields = _metadata_transaction_pending_fields(tx)
    if not fields:
        if transactions.transition(transaction_id, "Approved", "Completed", dry_run=False,
                                   counts={"items": 0, "changes": 0}) is None:
            raise ValueError("The transaction is no longer Approved; nothing was applied.")
        return None
    changed_fields = [str(v) for v in (metadata.get("changed_fields") or list(fields.keys()))]
    parts = [f"{k}={v}" for k, v in fields.items()]

    def _do(log, cancel_event=None):
        try:
            # #318: from here Beets may store the edit even if the tag write
            # fails (WRITE_FAILED), so a Failed apply keeps its rollback.
            transactions.update(transaction_id, metadata={"engine_result": {"mutation_started": True}})
            result = composite_workflows.update_item_metadata(item_id, fields, within_transaction=True)
            _require_attach_stage_success(result, "metadata update")
            _invalidate_lib_cache()
            transactions.update(transaction_id, status="Completed", logs=list(log)[-500:], counts={"items": 1, "changes": len(changed_fields)})
            return {"ok": True, "transaction_id": transaction_id, "changed_fields": changed_fields}
        except Exception as ex:
            transactions.update(transaction_id, status="Failed", logs=list(log)[-500:])
            transactions.append_log(transaction_id, f"ERROR: {ex}")
            raise

    # Claim before the job exists (#206 F3, #217): a cancel that landed after
    # the status check above wins (409, nothing started), and a job-status
    # sync can no longer race the claim.
    if transactions.transition(transaction_id, "Approved", "Running", dry_run=False) is None:
        raise ValueError("The transaction is no longer Approved; nothing was applied.")
    try:
        job = jobs.start_python(
            _do,
            label=f"Apply metadata transaction {transaction_id}",
            metadata={"transaction": False, "transaction_id": transaction_id, "type": "metadata-update", "item_id": item_id},
        )
    except Exception as ex:
        transactions.update(transaction_id, status="Failed", logs=["The apply job could not be started."])
        # Fixed text (#220 DELTA-3): callers echo ValueError text with a 409;
        # a start failure must reach the generic 500 handler instead.
        raise RuntimeError("The apply job could not be started.") from ex
    transactions.attach_job(transaction_id, job.job_id)
    return job


# -- Transaction / Library Changes Endpoints ---------------------------------

