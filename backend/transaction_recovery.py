"""Finish engine-backed transactions a restart left Running (ARCH-004).

An Apply records ``status: Running`` and its engine request BEFORE calling
the engine, with the transaction id as the engine Idempotency-Key. If the
Web Manager stops before recording the outcome, the transaction stays
Running. This sweep resolves it from engine evidence and NEVER replays the
mutation:

* engine says applied/succeeded  -> the family's ``finish_*`` verification
  runs against live Beets and records Completed (or Recovery Required);
* engine says it compensated/failed -> Failed (the engine restored state);
* engine is still running          -> left Running, checked again later;
* no engine record at all          -> for manifest-backed ops the manifest
  is written before any change, so "no manifest" proves nothing changed:
  Failed. For registry-backed ops (replacement, reviewed cleanup, whose
  in-memory registry a Beets restart loses) completion cannot be proven:
  Recovery Required.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from backend.beets_adapter import BeetsAdapter, BeetsError, BeetsNotFoundError, beets_adapter
from backend.transaction_engine import TransactionStore

log = logging.getLogger("app.transaction_recovery")


def _families() -> Dict[str, Dict[str, Any]]:
    import backend.album_row_merge as album_row_merge
    import backend.composite_workflows as composite_workflows
    import backend.duplicate_cleanup as duplicate_cleanup
    import backend.untracked_recovery_service as untracked
    return {
        composite_workflows.ITEM_FILE_REPLACEMENT_FAMILY: {
            "kind": "registry", "finish": composite_workflows.finish_track_replacement},
        duplicate_cleanup.REVIEWED_CLEANUP_FAMILY: {
            "kind": "registry", "finish": duplicate_cleanup.finish_reviewed_cleanup},
        album_row_merge.ALBUM_ROW_MERGE_FAMILY: {
            "kind": "manifest", "finish": album_row_merge.finish_album_row_merge,
            "record": lambda ad, op, meta: ad.get_album_row_merge(album_row_merge.merge_id_for(op))},
        untracked.ATTACH_FAMILY: {
            "kind": "manifest", "finish": untracked.finish_recovery,
            "record": lambda ad, op, meta: ad.get_untracked_record(untracked.record_id_for("attach", op))},
        untracked.QUARANTINE_FAMILY: {
            "kind": "manifest", "finish": untracked.finish_recovery,
            "record": lambda ad, op, meta: ad.get_untracked_record(untracked.record_id_for("quarantine", op))},
    }


def _registry_status(ad: BeetsAdapter, op: str) -> Optional[Dict[str, Any]]:
    try:
        return ad.get_operation(op)
    except BeetsNotFoundError:
        return None


def resolve_transaction(tx: Dict[str, Any], *, adapter: Optional[BeetsAdapter] = None,
                        store: Optional[TransactionStore] = None) -> Dict[str, Any]:
    """Resolve one Running engine-backed transaction from engine evidence."""
    from backend.composite_workflows import _get_store
    ad = adapter or beets_adapter
    st = _get_store(store)
    op = tx["id"]
    meta = tx.get("metadata") or {}
    fam = _families().get(meta.get("mutation_family"))
    if fam is None or tx.get("status") != "Running":
        return {"operation_id": op, "action": "skipped"}
    if "engine_request" not in meta:
        # Running was never followed by an engine request: nothing was sent.
        st.update(op, status="Failed", logs=["Recovered after restart: the engine was never called."])
        return {"operation_id": op, "action": "failed_never_sent"}

    def mark(status: str, note: str) -> Dict[str, Any]:
        st.update(op, status=status, logs=[f"Recovered after restart: {note}"])
        return {"operation_id": op, "action": status, "note": note}

    if fam["kind"] == "manifest":
        try:
            record = fam["record"](ad, op, meta)
        except BeetsNotFoundError:
            record = None
        if record is None:
            reg = _registry_status(ad, op)
            if reg and reg.get("status") == "running":
                return {"operation_id": op, "action": "still_running"}
            return mark("Failed", "the engine has no record of this operation; nothing was changed")
        status = record.get("status")
        if status == "applied":
            out = fam["finish"](op, {"result": record.get("result") or {}}, adapter=ad, store=st)
            return {"operation_id": op, "action": "finished", "status": out.get("status")}
        if status == "compensated":
            return mark("Failed", "the engine restored the library after a failure")
        if status == "applying":
            reg = _registry_status(ad, op)
            if reg and reg.get("status") == "running":
                return {"operation_id": op, "action": "still_running"}
        return mark("Recovery Required", f"engine record is {status!r}; completion cannot be proven")

    reg = _registry_status(ad, op)
    if reg is None:
        return mark("Recovery Required", "the engine has no record (Beets restarted?); completion cannot be proven")
    if reg.get("status") == "running":
        return {"operation_id": op, "action": "still_running"}
    if reg.get("status") == "succeeded":
        out = fam["finish"](op, {"result": reg.get("result") or {}}, adapter=ad, store=st)
        return {"operation_id": op, "action": "finished", "status": out.get("status")}
    return mark("Failed", f"the engine reports {reg.get('status')!r} ({reg.get('error_code') or 'no code'})")


def sweep(*, adapter: Optional[BeetsAdapter] = None, store: Optional[TransactionStore] = None) -> List[Dict[str, Any]]:
    from backend.composite_workflows import _get_store
    st = _get_store(store)
    families = _families()
    results = []
    rows, _total = st.list(status="Running", limit=1000)
    for tx in rows:
        if (tx.get("metadata") or {}).get("mutation_family") in families:
            try:
                results.append(resolve_transaction(st.get(tx["id"]), adapter=adapter, store=st))
            except BeetsError as exc:
                results.append({"operation_id": tx["id"], "action": "engine_unavailable", "error": exc.error_code})
    return results


def start_background_sweep(*, attempts: int = 20, interval: float = 30.0,
                           sleep: Callable[[float], None] = time.sleep) -> threading.Thread:
    """Run the sweep at startup, retrying while the engine is unavailable or
    an engine operation is still running."""
    def run():
        for _ in range(attempts):
            try:
                results = sweep()
            except Exception:
                log.exception("transaction recovery sweep failed")
                results = [{"action": "engine_unavailable"}]
            for r in results:
                if r.get("action") not in ("skipped", "still_running", "engine_unavailable"):
                    log.warning("Recovered transaction %s: %s", r.get("operation_id"), r)
            if not any(r.get("action") in ("still_running", "engine_unavailable") for r in results):
                return
            sleep(interval)

    thread = threading.Thread(target=run, daemon=True, name="transaction-recovery")
    thread.start()
    return thread
