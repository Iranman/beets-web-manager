"""The one contract every rollback follows.

A rollback is a library mutation, so it gets the same guarantees an apply
gets -- and one more: it may only report success when the library was
actually restored.

Every rollback in Web Manager follows these steps, in this order:

1. the transaction must prove that the mutation was applied. A transaction
   that never wrote anything (Preview, Approved, Cancelled) is refused, and
   a transaction that recorded no before-state is refused with
   ``rollback_unavailable`` -- never reported as rolled back;
2. the resource locks the apply held are taken, so an apply, a cleanup or a
   second rollback cannot touch the same album or item at the same time;
3. the transaction is claimed with a compare-and-set into ``Running``. The
   CAS is what stops two concurrent rollbacks from both reaching Beets;
4. the restore runs and is verified;
5. the transaction moves from ``Running`` to its final status.

A rollback interrupted between 3 and 5 leaves the transaction ``Running``
with a ``rollback_request`` that has no outcome. The restart sweep
(backend/transaction_recovery.py) turns that into ``Recovery Required`` --
never into a false "Rolled Back".

This module mutates nothing in the library: it takes locks and moves the
transaction record only.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence

from backend.resource_locks import (
    ResourceLockConflictError,
    attempt_owner,
    locks as resource_locks,
)
from backend.transaction_engine import TransactionStore

log = logging.getLogger("beets.workflows")

#: The statuses a rollback may start from. Each one proves the mutation was
#: applied: ``Completed`` (applied and verified), ``Recovery Required``
#: (applied, but the result could not be proven), ``Partially Rolled Back``
#: (an earlier rollback restored part of it and the rest may be retried) and
#: ``Failed`` with an engine apply record (the engine reported a change
#: before the failure). A Preview, Approved or Cancelled transaction never
#: wrote anything, so it is never marked Rolled Back.
ROLLBACK_SOURCE_STATUSES = ("Completed", "Recovery Required", "Partially Rolled Back")

#: How long a rollback waits for the locks its apply held before it gives
#: up. The same timeout the applies use.
ROLLBACK_LOCK_TIMEOUT = 10.0


def rollback_owner(operation_id: str) -> str:
    """The lock owner a rollback of ``operation_id`` uses. Locks are
    re-entrant per owner, so two concurrent attempts must not share one;
    the ``:rollback`` suffix keeps the audit trail readable."""
    return attempt_owner(f"{operation_id}:rollback")


def eligible_status(tx: Dict[str, Any], *, unknown_ok: bool = False) -> Optional[str]:
    """The status a rollback of ``tx`` may start from, or None when the
    transaction was never applied (so there is nothing to undo).

    ``unknown_ok``: the family can restore even when the apply's outcome is
    unknown (the transaction is ``Recovery Required``). The engine families
    can -- their manifest says exactly what changed. A composite family
    whose before-state was never written cannot, so its ``Recovery
    Required`` transaction is left for the operator instead of being
    reported as rolled back.
    """
    status = tx.get("status")
    meta = tx.get("metadata") or {}
    if status == "Failed" and meta.get("engine_result"):
        return "Failed"
    if status == "Recovery Required":
        return str(status) if unknown_ok else None
    return str(status) if status in ROLLBACK_SOURCE_STATUSES else None


def refusal(code: str, operation_id: str, status: Optional[str], error: str) -> Dict[str, Any]:
    """The refusal body every rollback returns when it must not run."""
    return {"ok": False, "code": code, "operation_id": operation_id, "status": status,
            "mutated": False, "error": error}


def not_eligible(operation_id: str, status: Optional[str]) -> Dict[str, Any]:
    return refusal("rollback_not_eligible", operation_id, status,
                   f"Only an applied transaction can be rolled back (status is {status}).")


def conflict(operation_id: str) -> Dict[str, Any]:
    return refusal("conflict", operation_id, None,
                   "The transaction changed state during rollback; reload and check it.")


def unavailable(operation_id: str, status: Optional[str], error: str) -> Dict[str, Any]:
    """The refusal for a transaction that was applied but recorded nothing
    to restore. It is never reported as rolled back."""
    return refusal("rollback_unavailable", operation_id, status, error)


@contextlib.contextmanager
def rollback_guard(
    store: TransactionStore,
    operation_id: str,
    keys: Sequence[str],
    *,
    timeout: float = ROLLBACK_LOCK_TIMEOUT,
    family: str = "",
    unknown_ok: bool = False,
) -> Iterator[Dict[str, Any]]:
    """Take the rollback locks and claim the transaction for a rollback.

    Yields ``{"ok": True, "transaction": tx}`` when the rollback may run, or
    ``{"ok": False, "code": ..., "status": ...}`` when it must not -- the
    caller then returns that refusal without touching the library.

    The locks are held for the whole ``with`` block, and the transaction is
    claimed with a compare-and-set into ``Running`` before the block runs,
    so a second rollback of the same transaction is refused instead of
    reaching Beets twice.
    """
    owner = rollback_owner(operation_id)
    wanted = [str(k) for k in keys if str(k).strip()]
    with contextlib.ExitStack() as stack:
        try:
            held = stack.enter_context(resource_locks().hold(wanted, owner, timeout=timeout))
        except ResourceLockConflictError as exc:
            log.info("Rollback of %s waited out the lock on %s", operation_id, exc.key)
            yield refusal("rollback_busy", operation_id, None,
                          f"The resource {exc.key} is busy with another operation; nothing was rolled back. "
                          "Try again.")
            return
        try:
            tx = store.get(operation_id)
        except KeyError:
            yield refusal("not_found", operation_id, None, "Transaction not found")
            return
        status = tx.get("status")
        source = eligible_status(tx, unknown_ok=unknown_ok)
        if source is None:
            yield not_eligible(operation_id, status)
            return
        claimed = store.transition(operation_id, source, "Running")
        if claimed is None:
            # Another rollback (or an apply) won the race for this
            # transaction while this one waited for the locks.
            yield conflict(operation_id)
            return
        store.update(operation_id, metadata={"rollback_request": {
            "started_at": time.time(), "family": family, "owner": owner, "outcome": None}})
        yield {"ok": True, "transaction": claimed, "owner": owner, "keys": held}


def finish(
    store: TransactionStore,
    operation_id: str,
    *,
    restored: int = 0,
    failed: int = 0,
    status: Optional[str] = None,
    logs: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Move a claimed rollback from ``Running`` to its final status and
    record the outcome. The outcome is written even when the restore partly
    failed, so the restart sweep never mistakes an interrupted rollback for
    a finished one.

    ``recorded`` is False when the final compare-and-set lost: something
    else moved the transaction out of ``Running`` while the restore ran.
    The restore still happened; the caller reports the lost race instead of
    pretending the status was written.
    """
    final = status or ("Rolled Back" if not failed else ("Partially Rolled Back" if restored else "Failed"))
    try:
        meta = store.get(operation_id).get("metadata") or {}
    except KeyError:
        meta = {}
    request = dict(meta.get("rollback_request") or {})
    request["outcome"] = final
    recorded = store.transition(
        operation_id, "Running", final,
        counts={"rollback_ok": restored, "rollback_failed": failed},
        metadata={"rollback_request": request, **(metadata or {})},
        append_logs=list(logs or []),
    )
    if recorded is None:
        log.warning("Rollback of %s finished but the transaction is no longer Running", operation_id)
        return {"ok": not failed, "operation_id": operation_id, "status": final,
                "rollback_ok": restored, "rollback_failed": failed, "recorded": False}
    return {"ok": not failed, "operation_id": operation_id, "status": final,
            "rollback_ok": restored, "rollback_failed": failed, "recorded": True}


def abandon(
    store: TransactionStore,
    operation_id: str,
    *,
    status: str = "Failed",
    logs: Optional[List[str]] = None,
) -> None:
    """Give a claimed rollback back a sane status when it could not run.

    A rollback that claimed the transaction and then failed must never leave
    it ``Running``: that would look like an interrupted rollback to the
    restart sweep and to the operator. ``Recovery Required`` is used when
    the outcome of the restore itself is unknown.
    """
    request = {"outcome": status, "finished_at": time.time()}
    store.transition(operation_id, "Running", status,
                     metadata={"rollback_request": request},
                     append_logs=list(logs or []))
