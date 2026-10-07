"""Plan/Apply/Rollback orchestration over the shared TransactionStore (ARCH-001).
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple
from backend.app_runtime import _app_logger, EDITABLE_FIELDS, _s
from backend.library_service import _require_attach_stage_success
from backend.transaction_engine import metadata_diff
from backend.beets_adapter import lib
import backend.composite_workflows as composite_workflows
from backend.matching_service import _invalidate_lib_cache
from backend.app_runtime import jobs, transactions
from backend.job_service import _call_job_fn
from backend.import_review_service import _metadata_transaction_pending_fields

from backend.auth_service import _transaction_user_label

# ── ARCH-001 extracted code ──


_TRANSACTION_LABEL_BLOCKLIST = (
    "discography",
    "plex library refresh",
)


def _transaction_operation_from_job(label: str, metadata: Dict[str, Any]) -> Optional[str]:
    text = f"{metadata.get('type', '')} {label}".strip().lower()
    if not text or any(blocked in text for blocked in _TRANSACTION_LABEL_BLOCKLIST):
        return None
    if "playlist" in text:
        if any(word in text for word in ("import", "place", "repair", "sync", "quality")):
            return "Playlist Import"
    if "music-format" in text or "replace" in text or "replacement" in text:
        return "Replace"
    if "fetchart" in text or "art" in text or "artwork" in text:
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
    if "ai" in text or "suggest" in text:
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
    reason = str(explicit_payload.get("reason") or "Created from the existing job workflow before library changes are applied.")
    source = str(explicit_payload.get("source") or metadata.get("type") or "job")
    tx_metadata = {k: v for k, v in metadata.items() if k != "transaction"}
    if command:
        tx_metadata["command"] = [str(part) for part in command]
    return transactions.create(
        operation_type=str(operation_type),
        initiating_user=_transaction_user_label(),
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


def _install_transaction_job_hooks() -> None:
    if getattr(jobs, "_transaction_hooks_installed", False):
        return
    original_start_python = jobs.start_python

    def start_python_with_transaction(fn, label="", metadata=None):
        metadata_payload = dict(metadata or {})
        tx = _transaction_create_for_job(label, metadata_payload)
        tx_id = tx.get("id") if tx else ""
        if tx_id:
            metadata_payload["transaction_id"] = tx_id

            def wrapped(log, cancel=None, update_state=None):
                # The final status mirrors job_engine's own rule: a set cancel
                # event (or a "cancelled" exception) makes the job cancelled,
                # so the transaction is Cancelled too. Job sync no longer
                # corrects a final status afterwards (QA-217-7).
                def cancelled(ex=None):
                    return bool(cancel is not None and cancel.is_set()) or \
                        (ex is not None and str(ex).strip().lower() == "cancelled")

                transactions.update(tx_id, status="Running")
                try:
                    result = _call_job_fn(fn, log, cancel, update_state)
                except Exception as ex:
                    transactions.update(tx_id, status="Cancelled" if cancelled(ex) else "Failed")
                    transactions.append_log(tx_id, f"ERROR: {ex}")
                    raise
                next_status = "Preview" if metadata_payload.get("dry_run") or metadata_payload.get("preview") else "Completed"
                transactions.update(tx_id, status="Cancelled" if cancelled() else next_status)
                return result
        else:
            wrapped = fn
        job = original_start_python(wrapped, label=label, metadata=metadata_payload)
        if tx_id:
            transactions.attach_job(tx_id, job.job_id)
        return job

    jobs.start_python = start_python_with_transaction
    jobs._transaction_hooks_installed = True


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
    if tx.get("status") != "Approved":
        raise ValueError("Approve the transaction before applying it.")
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
            result = composite_workflows.update_item_metadata(item_id, fields)
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
    except Exception:
        transactions.update(transaction_id, status="Failed", logs=["The apply job could not be started."])
        raise
    transactions.attach_job(transaction_id, job.job_id)
    return job


# -- Transaction / Library Changes Endpoints ---------------------------------

