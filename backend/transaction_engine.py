"""Durable transaction records for library-changing jobs.

The transaction store is intentionally small and file-backed so it can sit
beside the existing JobStore without changing the job architecture.
"""
from __future__ import annotations

import csv
import errno
import hashlib
import io
import json
import logging
import os
import re
import shutil
import stat
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
import urllib.parse

LOG = logging.getLogger("beets_web.transaction_engine")


STATUSES = {
    "Pending",
    "Preview",
    "Approved",
    "Running",
    "Completed",
    "Cancelled",
    "Failed",
    "Rolled Back",
    "Partially Rolled Back",
    # Wave 25 Docker acceptance round: several Apply paths already called
    # store.update(operation_id, status="Recovery Required") after a
    # partial failure where automatic rollback could not be completed --
    # but _status()/TransactionStore.update() silently downgrade any
    # status not in this set back to "Pending", so every one of those
    # calls was actually leaving the record looking like a fresh,
    # not-yet-started transaction instead of flagging it for operator
    # attention. Added here so the status this codebase has been trying
    # to set since Wave 20-ish actually persists.
    "Recovery Required",
}

TRANSACTION_TYPES = {
    "Import",
    "Rename",
    "Metadata Update",
    "Artwork Update",
    "Move",
    "Delete",
    "Replace",
    "Merge Artist",
    "Merge Album",
    "Split Album",
    "Playlist Import",
    "Library Cleanup",
    "Duplicate Removal",
    "AcoustID Match",
    "MusicBrainz Match",
    "AI Suggestion",
    "Repair",
    "Rescan",
}

DEFAULT_SETTINGS: Dict[str, Any] = {
    "enabled": True,
    "backups_enabled": True,
    "rollback_enabled": True,
    "backup_retention_days": 30,
    "automatic_approval_threshold": 0.98,
    "require_review_below_threshold": True,
    "maximum_undo_history": 250,
    "dry_run_by_default": False,
}

_TRANSACTION_ID_RE = re.compile(r"^txn_\d+_[0-9a-zA-Z_]{8,64}$")

# SEC-002 Wave 16 final review: the control agent serves requests via
# ThreadingHTTPServer, so two Apply requests for the SAME operation_id can
# genuinely run concurrently in separate threads. TransactionStore's own
# _lock only guards individual get()/update() calls, not a whole multi-step
# Apply operation -- two threads could each read the same step as "pending"
# before either persists its "completed" status, racing through the same
# destructive step. This per-operation-id lock registry serializes Apply
# calls for the SAME transaction (never blocking unrelated transactions'
# Apply calls against each other) so retries/concurrent double-clicks are
# genuinely idempotent rather than racing. Entries are intentionally never
# removed: transaction ids are unique and Apply is a rare, deliberate,
# low-frequency user action, so the dict's steady-state size (one small
# Lock object per transaction ever applied, for the life of the process) is
# negligible -- and popping entries would reopen the exact race this exists
# to close (a thread already blocked on the old Lock object while a new
# request creates a fresh one for the same id).
_apply_locks_guard = threading.Lock()
_apply_locks: Dict[str, threading.Lock] = {}


def _get_apply_lock(operation_id: str) -> Any:
    with _apply_locks_guard:
        lock = _apply_locks.get(operation_id)
        if lock is None:
            lock = threading.RLock()
            _apply_locks[operation_id] = lock
        return lock


#: Statuses an Apply never starts from: the transaction was cancelled,
#: already applied, or already undone.
_APPLY_TERMINAL = frozenset({"Cancelled", "Completed", "Rolled Back", "Partially Rolled Back"})


def _claim_apply_running(store: "TransactionStore", operation_id: str, observed_status: Any,
                         metadata: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Compare-and-set the status this Apply validated -> Running (#218).
    None, changing nothing, when a cancel (or anything else) moved the
    transaction since it was read, or it was read in a finished status."""
    if observed_status in _APPLY_TERMINAL:
        return None
    return store.transition(operation_id, str(observed_status), "Running", metadata=metadata)


def _claim_lost(store: "TransactionStore", operation_id: str) -> Dict[str, Any]:
    try:
        status = store.get(operation_id).get("status")
    except KeyError:
        status = "missing"
    return {"ok": False, "code": "transaction_state_changed", "operation_id": operation_id, "status": status,
            "mutated": False,
            "error": f"The transaction is {status}, so Apply did not start; nothing was changed."}


# SEC-002 Wave 17 final review: _get_apply_lock above only serializes two
# Apply calls for the SAME operation_id. It does nothing to stop two
# DIFFERENT transactions that both target the same underlying resource
# (e.g. two separately-Planned replacements for the same Beets item id)
# from applying concurrently and racing on the same file/DB row. This is a
# second, orthogonal lock registry keyed by an arbitrary caller-chosen
# resource key (e.g. f"item:{item_id}") -- same never-released-entries
# rationale as _apply_locks above.
_resource_locks_guard = threading.Lock()
_resource_locks: Dict[str, threading.Lock] = {}


def _get_resource_lock(key: str) -> Any:
    with _resource_locks_guard:
        lock = _resource_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _resource_locks[key] = lock
        return lock


# SEC-002 Wave 17 final review: shared, hardened path-safety helpers,
# extracted from what were previously private closures inside
# _execute_import_review_cleanup_apply_locked (Wave 15/16). Every mutation
# family should use these SAME helpers rather than re-implementing
# (and potentially under-implementing) symlink-component-walk logic per
# family -- see the Wave 17 track replacement functions below for the
# second real user.
def _is_symlink_path(p: Path) -> bool:
    try:
        return p.is_symlink() or os.path.islink(str(p)) or stat.S_ISLNK(os.lstat(str(p)).st_mode)
    except Exception:
        return False


def _path_has_symlink_under(path: Path, base: Path) -> bool:
    """True if `path` itself, or any component between it and `base`
    (inclusive of `base`), is a symlink. Walking every parent component --
    not just the leaf -- is what actually defends against a symlinked
    *parent directory* being substituted in after Plan time; a leaf-only
    is_symlink() check misses that entirely."""
    try:
        curr = path
        while curr != base and base in curr.parents:
            if _is_symlink_path(curr):
                return True
            curr = curr.parent
        if curr == base and _is_symlink_path(curr):
            return True
    except Exception:
        return True
    return False


def _now() -> float:
    return time.time()


def _safe_json(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _s(val: Any) -> str:
    if isinstance(val, bytes):
        return val.decode("utf-8", "replace")
    return str(val or "")


def _status(value: str) -> str:
    candidate = str(value or "").strip()
    return candidate if candidate in STATUSES else "Pending"


def _operation_type(value: str) -> str:
    candidate = str(value or "").strip()
    return candidate if candidate in TRANSACTION_TYPES else "Repair"


def _empty_confidence() -> Dict[str, Optional[float]]:
    return {
        "overall": None,
        "ai": None,
        "acoustid": None,
        "musicbrainz": None,
        "artwork": None,
    }


def _new_id() -> str:
    h = str(uuid.uuid4().hex)
    if _TRANSACTION_ID_RE.fullmatch(h):
        return h
    return f"txn_{int(_now())}_{h[:12]}"


def metadata_diff(
    current: Dict[str, Any],
    proposed: Dict[str, Any],
    *,
    include_unchanged: bool = False,
) -> List[Dict[str, Any]]:
    """Return field-level metadata changes in a UI-friendly shape."""
    keys = sorted(set(current or {}) | set(proposed or {}))
    rows: List[Dict[str, Any]] = []
    for key in keys:
        old = (current or {}).get(key)
        new = (proposed or {}).get(key)
        changed = old != new
        if changed or include_unchanged:
            rows.append({
                "field": str(key),
                "old": old,
                "new": new,
                "changed": changed,
            })
    return rows


def _result_counts(result: Any) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    if not isinstance(result, dict):
        return counts
    for key in (
        "items",
        "item_count",
        "items_affected",
        "affected",
        "files",
        "files_changed",
        "changed",
        "changes",
        "warnings",
        "errors",
        "removed",
        "moved",
        "renamed",
        "updated",
    ):
        value = result.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            counts[key] = value
        elif isinstance(value, (list, tuple, set, dict)):
            counts[key] = len(value)
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    for key, value in summary.items():
        if isinstance(value, int) and key not in counts:
            counts[str(key)] = value
    return counts


def _summarize_result(result: Any) -> Any:
    if result is None:
        return None
    if isinstance(result, dict):
        scalars: Dict[str, Any] = {}
        sizes: Dict[str, int] = {}
        for key, value in result.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                scalars[str(key)] = value if not isinstance(value, str) else value[:240]
            elif isinstance(value, (list, tuple, set, dict)):
                sizes[str(key)] = len(value)
        return {"type": "dict", "scalars": scalars, "sizes": sizes}
    if isinstance(result, (list, tuple, set)):
        return {"type": "list", "count": len(result)}
    return {"type": type(result).__name__, "value": str(result)[:240]}


_ROOT_LOCKS: Dict[str, Any] = {}
_ROOT_LOCKS_GUARD = threading.Lock()


class TransactionStore:
    def __init__(self, root: Optional[str] = None):
        base = (
            root
            or os.environ.get("BEETS_TRANSACTION_DIR")
            or f"{os.environ.get('WEB_MANAGER_DATA_DIR', '/web-manager-data')}/transactions"
        )
        self.root = Path(base)
        # One lock per directory, shared by every store instance on it, so
        # transition() is a real compare-and-set even when a route's store
        # (app_runtime.transactions) and an apply path's store
        # (composite_workflows.get_default_store()) are distinct objects.
        # In-process only: the web app is one waitress process (threads).
        with _ROOT_LOCKS_GUARD:
            self._lock = _ROOT_LOCKS.setdefault(os.path.abspath(str(base)), threading.RLock())

    def _ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, transaction_id: str) -> Path:
        safe = str(transaction_id or "").strip()
        if not _TRANSACTION_ID_RE.fullmatch(safe):
            raise KeyError("Invalid transaction id")
        return self.root / f"{safe}.json"

    def _settings_path(self) -> Path:
        return self.root / "settings.json"

    def _read(self, transaction_id: str) -> Dict[str, Any]:
        path = self._path(transaction_id)
        if not path.exists():
            raise KeyError("Transaction not found")
        return json.loads(path.read_text(encoding="utf-8"))

    def _write(self, tx: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure()
        tx["updated_at"] = _now()
        payload = _safe_json(tx)
        path = self._path(payload["id"])
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
        return payload

    def create(
        self,
        *,
        operation_type: str,
        initiating_user: str = "operator",
        originating_job: Optional[str] = None,
        status: str = "Pending",
        dry_run: bool = False,
        summary: str = "",
        reason: str = "",
        source: str = "",
        confidence: Optional[Dict[str, Any]] = None,
        changes: Optional[List[Dict[str, Any]]] = None,
        rollback_available: bool = False,
        rollback_reason: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = _now()
        tx = {
            "id": _new_id(),
            "created_at": now,
            "updated_at": now,
            "initiating_user": initiating_user or "operator",
            "originating_job": originating_job,
            "operation_type": _operation_type(operation_type),
            "status": _status(status),
            "dry_run": bool(dry_run),
            "summary": summary or "",
            "reason": reason or "",
            "source": source or "",
            "confidence": {**_empty_confidence(), **(confidence or {})},
            "counts": {
                "items": 0,
                "files": 0,
                "changes": len(changes or []),
                "warnings": 0,
                "errors": 0,
            },
            "rollback": {
                "available": bool(rollback_available),
                "reason": rollback_reason or (
                    "" if rollback_available else "Rollback data has not been captured for this transaction."
                ),
                "operations": [],
            },
            "backup": {
                "available": False,
                "paths": [],
                "retention_days": DEFAULT_SETTINGS["backup_retention_days"],
            },
            "changes": changes or [],
            "logs": [],
            "metadata": metadata or {},
            "settings": self.settings(),
        }
        with self._lock:
            return self._write(tx)

    def settings(self) -> Dict[str, Any]:
        self._ensure()
        path = self._settings_path()
        if not path.exists():
            return dict(DEFAULT_SETTINGS)
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return dict(DEFAULT_SETTINGS)
        settings = dict(DEFAULT_SETTINGS)
        if isinstance(loaded, dict):
            settings.update({k: loaded[k] for k in DEFAULT_SETTINGS if k in loaded})
        return settings

    def save_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            settings = self.settings()
            for key, default in DEFAULT_SETTINGS.items():
                if key not in updates:
                    continue
                value = updates[key]
                if isinstance(default, bool):
                    settings[key] = bool(value)
                elif isinstance(default, int):
                    settings[key] = max(0, int(value))
                elif isinstance(default, float):
                    settings[key] = max(0.0, min(1.0, float(value)))
                else:
                    settings[key] = value
            self._ensure()
            path = self._settings_path()
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(settings, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(path)
            return settings
    def get(self, transaction_id: str, *, offset: int = 0, limit: Optional[int] = None) -> Dict[str, Any]:
        with self._lock:
            tx = self._read(transaction_id)
        changes = list(tx.get("changes") or [])
        total = len(changes)
        offset = max(0, int(offset or 0))
        if limit is not None:
            limit = max(1, min(1000, int(limit)))
            tx["changes"] = changes[offset:offset + limit]
            tx["changes_offset"] = offset
            tx["changes_limit"] = limit
        tx["changes_total"] = total
        return tx

    def summary(self, tx: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": tx.get("id"),
            "created_at": tx.get("created_at"),
            "updated_at": tx.get("updated_at"),
            "initiating_user": tx.get("initiating_user"),
            "originating_job": tx.get("originating_job"),
            "operation_type": tx.get("operation_type"),
            "status": tx.get("status"),
            "dry_run": bool(tx.get("dry_run")),
            "summary": tx.get("summary") or "",
            "reason": tx.get("reason") or "",
            "source": tx.get("source") or "",
            "confidence": tx.get("confidence") or _empty_confidence(),
            "counts": tx.get("counts") or {},
            "rollback": {
                "available": bool((tx.get("rollback") or {}).get("available")),
                "reason": (tx.get("rollback") or {}).get("reason") or "",
            },
            "metadata": tx.get("metadata") or {},
        }

    def list(
        self,
        *,
        offset: int = 0,
        limit: int = 50,
        status: str = "",
        operation: str = "",
        query: str = "",
        job: str = "",
    ) -> Tuple[List[Dict[str, Any]], int]:
        self._ensure()
        status_lc = status.strip().lower()
        operation_lc = operation.strip().lower()
        query_lc = query.strip().lower()
        job = job.strip()
        rows: List[Dict[str, Any]] = []
        with self._lock:
            paths = sorted(self.root.glob("txn_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for path in paths:
                try:
                    tx = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                row = self.summary(tx)
                if status_lc and str(row.get("status") or "").lower() != status_lc:
                    continue
                if operation_lc and str(row.get("operation_type") or "").lower() != operation_lc:
                    continue
                if job and str(row.get("originating_job") or "") != job:
                    continue
                if query_lc:
                    haystack = json.dumps({
                        "summary": row.get("summary"),
                        "reason": row.get("reason"),
                        "source": row.get("source"),
                        "metadata": row.get("metadata"),
                        "changes": tx.get("changes") or [],
                    }, ensure_ascii=False, default=str).lower()
                    if query_lc not in haystack:
                        continue
                rows.append(row)
        total = len(rows)
        offset = max(0, int(offset or 0))
        limit = max(1, min(500, int(limit or 50)))
        return rows[offset:offset + limit], total

    def update(self, transaction_id: str, **updates: Any) -> Dict[str, Any]:
        with self._lock:
            tx = self._read(transaction_id)
            for key, value in updates.items():
                if key == "status":
                    tx[key] = _status(str(value))
                elif key == "operation_type":
                    tx[key] = _operation_type(str(value))
                elif key == "counts" and isinstance(value, dict):
                    tx.setdefault("counts", {}).update(value)
                elif key == "confidence" and isinstance(value, dict):
                    tx.setdefault("confidence", _empty_confidence()).update(value)
                elif key == "metadata" and isinstance(value, dict):
                    tx.setdefault("metadata", {}).update(value)
                elif key == "rollback" and isinstance(value, dict):
                    tx.setdefault("rollback", {}).update(value)
                else:
                    tx[key] = value
            return self._write(tx)

    def transition(self, transaction_id: str, expected_status: str, new_status: str,
                   **updates: Any) -> Optional[Dict[str, Any]]:
        """Compare-and-set status change: move ``expected_status`` ->
        ``new_status`` (plus ``updates``) atomically under the store lock.
        Returns None, changing nothing, when the current status differs."""
        with self._lock:
            tx = self._read(transaction_id)
            if tx.get("status") != _status(str(expected_status)):
                return None
            return self.update(transaction_id, status=new_status, **updates)

    def attach_job(self, transaction_id: str, job_id: str) -> Dict[str, Any]:
        return self.update(transaction_id, originating_job=job_id, metadata={"job_id": job_id})

    def append_log(self, transaction_id: str, message: str) -> Dict[str, Any]:
        with self._lock:
            tx = self._read(transaction_id)
            logs = list(tx.get("logs") or [])
            logs.append(str(message))
            tx["logs"] = logs[-500:]
            return self._write(tx)

    def update_from_job(self, transaction_id: str, job: Any) -> Dict[str, Any]:
        status = getattr(job, "status", "")
        dry_run = False
        metadata = getattr(job, "metadata", {}) or {}
        dry_run = bool(metadata.get("dry_run") or metadata.get("preview"))
        if status == "running":
            next_status = "Running"
        elif status == "success":
            next_status = "Preview" if dry_run else "Completed"
        elif status in {"cancelled", "killed"}:
            next_status = "Cancelled"
        else:
            next_status = "Failed"

        result = getattr(job, "result", None)
        updates: Dict[str, Any] = {
            "status": next_status,
            "originating_job": getattr(job, "job_id", None),
            "metadata": {"job_id": getattr(job, "job_id", None), **metadata},
        }
        counts = _result_counts(result)
        if counts:
            updates["counts"] = counts
        result_summary = _summarize_result(result)
        if result_summary is not None:
            updates["result_summary"] = result_summary
        if getattr(job, "log", None):
            updates["logs"] = list(getattr(job, "log"))[-500:]
        with self._lock:
            # Job state only advances a transaction that is already Running
            # (claimed before its job started). It never claims an Approved
            # one, nor rewrites Cancelled or another final status (#217).
            if self._read(transaction_id).get("status") != "Running":
                updates.pop("status")
            return self.update(transaction_id, **updates)

    def rollback(self, transaction_id: str) -> Dict[str, Any]:
        with self._lock:
            tx = self._read(transaction_id)
            rollback = tx.get("rollback") or {}
            operations = rollback.get("operations") or []
            if not rollback.get("available") or not operations:
                raise ValueError(rollback.get("reason") or "Rollback unavailable.")
            # Operation execution is intentionally conservative. Existing
            # workflows must add explicit reversible operations before rollback
            # becomes active for them.
            raise ValueError("Rollback operations are recorded but no executor is registered for this transaction.")

    def export_json(self, transaction_id: str) -> str:
        return json.dumps(self.get(transaction_id), indent=2, sort_keys=True)

    def export_markdown(self, transaction_id: str) -> str:
        tx = self.get(transaction_id)
        lines = [
            f"# Transaction {tx['id']}",
            "",
            f"- Operation: {tx.get('operation_type')}",
            f"- Status: {tx.get('status')}",
            f"- Dry run: {'yes' if tx.get('dry_run') else 'no'}",
            f"- Originating job: {tx.get('originating_job') or 'none'}",
            f"- Summary: {tx.get('summary') or 'none'}",
            f"- Reason: {tx.get('reason') or 'none'}",
            "",
            "## Confidence",
            "",
        ]
        confidence = tx.get("confidence") or {}
        for key in ("overall", "ai", "acoustid", "musicbrainz", "artwork"):
            value = confidence.get(key)
            lines.append(f"- {key}: {value if value is not None else 'unknown'}")
        lines.extend(["", "## Changes", ""])
        changes = tx.get("changes") or []
        if not changes:
            lines.append("No item-level changes were captured for this transaction.")
        for idx, change in enumerate(changes, 1):
            label = change.get("track") or change.get("album") or change.get("artist") or change.get("id") or idx
            lines.extend([
                f"### {idx}. {label}",
                "",
                f"- Operation: {change.get('operation') or tx.get('operation_type')}",
                f"- Reason: {change.get('reason') or tx.get('reason') or 'none'}",
                f"- Source: {change.get('source') or tx.get('source') or 'none'}",
            ])
            for row in change.get("metadata_diff") or []:
                if row.get("changed"):
                    lines.append(f"- {row.get('field')}: `{row.get('old')}` -> `{row.get('new')}`")
            for fs in change.get("filesystem") or []:
                lines.append(f"- {fs.get('operation')}: `{fs.get('old')}` -> `{fs.get('new')}`")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def export_csv(self, transaction_id: str) -> str:
        tx = self.get(transaction_id)
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=[
                "transaction_id",
                "operation",
                "status",
                "artist",
                "album",
                "track",
                "reason",
                "source",
                "overall_confidence",
            ],
        )
        writer.writeheader()
        for change in tx.get("changes") or []:
            confidence = change.get("confidence") or tx.get("confidence") or {}
            writer.writerow({
                "transaction_id": tx.get("id"),
                "operation": change.get("operation") or tx.get("operation_type"),
                "status": tx.get("status"),
                "artist": change.get("artist") or "",
                "album": change.get("album") or "",
                "track": change.get("track") or "",
                "reason": change.get("reason") or tx.get("reason") or "",
                "source": change.get("source") or tx.get("source") or "",
                "overall_confidence": confidence.get("overall"),
            })
        return buf.getvalue()

    def export(self, transaction_id: str, fmt: str) -> Tuple[str, str]:
        fmt = str(fmt or "json").strip().lower()
        if fmt == "markdown" or fmt == "md":
            return self.export_markdown(transaction_id), "text/markdown"
        if fmt == "csv":
            return self.export_csv(transaction_id), "text/csv"
        return self.export_json(transaction_id), "application/json"

    def create_import_review_cleanup_plan(
        self,
        *,
        target_path: str,
        allowed_roots: List[str],
        source_paths: Optional[List[str]] = None,
        expected_states: Optional[Dict[str, Any]] = None,
        reversibility: str = "RECOVERABLE",
        payload: Optional[Dict[str, Any]] = None,
        created_by: str = "operator",
    ) -> Tuple[str, Dict[str, Any]]:
        """Create a plan transaction with strict root containment and symlink checks."""
        resolved_target = Path(target_path).resolve()
        resolved_roots = [Path(r).resolve() for r in (allowed_roots or [])]

        # 1. Symlink check
        raw_sources = source_paths or [target_path]
        if os.path.islink(target_path):
            raise ValueError(f"Symlinks are not permitted: {target_path}")
        for sp in raw_sources:
            if os.path.islink(sp):
                raise ValueError(f"Symlinks are not permitted: {sp}")

        resolved_target = Path(target_path).resolve()
        resolved_roots = [Path(r).resolve() for r in (allowed_roots or [])]

        # 2. Root containment check
        target_allowed = any(
            resolved_target == r or r in resolved_target.parents
            for r in resolved_roots
        )
        if not target_allowed:
            raise ValueError(f"Target path {target_path} is outside allowed root boundaries.")

        sources = [Path(sp).resolve() for sp in raw_sources]
        for src in sources:
            src_allowed = any(
                src == r or r in src.parents for r in resolved_roots
            )
            if not src_allowed:
                raise ValueError(f"Source path {src} is outside allowed root boundaries.")

        tx = self.create(
            operation_type="Library Cleanup",
            initiating_user=created_by,
            status="Preview",
            summary=f"Import review cleanup plan for {resolved_target.name}",
            metadata={
                "target_path": str(resolved_target),
                "allowed_roots": [str(r) for r in resolved_roots],
                "source_paths": [str(s) for s in sources],
                "expected_states": expected_states or {},
                "reversibility": reversibility,
                "payload": payload or {},
            },
        )
        op_id = tx["id"]
        return op_id, tx

    def revalidate_preconditions(self, transaction_id: str) -> Tuple[bool, Optional[str]]:
        """Revalidate preconditions before applying transaction (TOCTOU race check)."""
        tx = self.get(transaction_id)
        if not tx:
            return False, f"Transaction {transaction_id} not found."

        if tx.get("status") in {"Completed", "Rolled Back"}:
            return True, None

        meta = tx.get("metadata") or {}
        payload = meta.get("payload") or {}
        target_path = meta.get("target_path")
        expected_states = meta.get("expected_states") or {}

        # A source whose step already durably completed (persisted to disk
        # by a prior Apply attempt, including one that then crashed before
        # finishing the rest of the transaction) is *expected* to be gone
        # or moved -- that is the correct, already-applied outcome for this
        # transaction, not evidence of unexpected external tampering.
        # Re-checking its original pre-mutation stat here would otherwise
        # permanently wedge crash-recovery retries: the file legitimately
        # no longer exists at that path, so "no longer exists" would always
        # fire, and the transaction could neither finish nor be marked
        # Completed.
        already_done_sources = {
            step.get("source")
            for step in (meta.get("steps") or [])
            if step.get("source") and step.get("status") in {"completed", "irreversible_completed", "rolled_back"}
        }

        # 1. Check symlink / existence / size / mtime / ino / dev for recorded paths
        for path_str, state in expected_states.items():
            if path_str in already_done_sources:
                continue
            p = Path(path_str)
            if os.path.islink(p) or p.is_symlink():
                return False, f"Path {path_str} became a symlink."
            if not p.exists():
                return False, f"Path {path_str} no longer exists."
            try:
                st = os.lstat(str(p))
            except Exception:
                return False, f"Could not stat path {path_str}."

            if stat.S_ISLNK(st.st_mode):
                return False, f"Path {path_str} became a symlink."

            expected_size = state.get("size")
            expected_mtime = state.get("mtime")
            expected_mtime_ns = state.get("mtime_ns")
            expected_ino = state.get("ino")
            expected_dev = state.get("dev")

            if expected_size is not None and st.st_size != expected_size:
                return False, f"Path {path_str} size changed (expected {expected_size}, got {st.st_size})."
            if expected_mtime_ns is not None and st.st_mtime_ns != expected_mtime_ns:
                return False, f"Path {path_str} mtime_ns changed."
            elif expected_mtime is not None and abs(st.st_mtime - expected_mtime) > 0.001:
                return False, f"Path {path_str} mtime changed."
            if expected_ino is not None and st.st_ino != expected_ino:
                return False, f"Path {path_str} inode changed (expected {expected_ino}, got {st.st_ino})."
            if expected_dev is not None and st.st_dev != expected_dev:
                return False, f"Path {path_str} device changed."

        # 2. Directory growth race check (only for full-folder operations)
        raw_files = payload.get("files")
        if target_path and not raw_files:
            p_target = Path(target_path)
            if p_target.is_dir() and not os.path.islink(p_target) and not p_target.is_symlink():
                current_files = set()
                try:
                    p_resolved = p_target.resolve(strict=False)
                    for f in p_target.rglob("*"):
                        if f.is_file() and not os.path.islink(f) and not f.is_symlink():
                            try:
                                f_res = f.resolve(strict=False)
                                if f_res == p_resolved or p_resolved in f_res.parents:
                                    current_files.add(str(f_res))
                            except Exception:
                                pass
                except Exception:
                    pass
                expected_files = set(expected_states.keys())
                unexpected = current_files - expected_files
                if unexpected:
                    return False, f"Directory structure changed; unexpected file(s) added: {list(unexpected)[:3]}"

        return True, None


def _cfg_music_root() -> str:
    """MUSIC_ROOT with its documented aliases (``config_layers``)."""
    from backend.config_layers import music_root
    return music_root()


def _import_review_library_refusal(target: Any, delete_sources: List[str], music_roots: List[Any],
                                   library_delete_allowed: bool) -> Optional[Dict[str, Any]]:
    """Security F2 (#235 review): a cleanup target that CONTAINS the music
    library is always refused, and an irreversible delete of a file inside
    the library needs the explicit library-delete gate. Checked at plan and
    again at apply.

    String-only containment (normpath prefix, CodeQL #1383): the target and
    sources were resolved and contained at plan time. Each configured music
    root is compared both as written and as its realpath, so a symlinked
    MUSIC_ROOT is still recognized."""
    forms: List[str] = []
    for root in music_roots:
        for form in (os.path.normpath(str(root)), os.path.realpath(str(root))):
            if form not in forms:
                forms.append(form)
    tgt = os.path.normpath(str(target))
    for root in forms:
        if root != tgt and (tgt == os.path.dirname(tgt) or _normpath_within_roots(root, [Path(tgt)])):
            return {"ok": False, "code": "import_review_target_contains_library", "mutated": False,
                    "error": f"Cleanup target {tgt} contains the music library {root}; refusing."}
    if library_delete_allowed:
        return None
    for src in delete_sources:
        if _normpath_within_roots(str(src), [Path(r) for r in forms]):
            return {"ok": False, "code": "import_review_library_delete_refused", "mutated": False,
                    "error": f"{os.path.normpath(str(src))} is inside the music library; deleting it needs the library-delete gate."}
    return None


def execute_import_review_cleanup_plan(
    store: TransactionStore,
    payload: Dict[str, Any],
    allowed_roots: List[str],
    *,
    music_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute plan creation for import review folder/files with strict validation and durable step tracking."""
    raw_path = str(payload.get("path") or payload.get("folder") or "").strip()
    if not raw_path:
        return {"ok": False, "error": "Review path is required."}

    # URL decoding budget & null-byte check
    current_decode = raw_path
    for _ in range(3):
        decoded = urllib.parse.unquote(current_decode)
        if decoded == current_decode:
            break
        current_decode = decoded
    if "\x00" in current_decode or "%00" in raw_path or "\x00" in raw_path:
        return {"ok": False, "error": "Path contains unsafe encoded characters."}

    action = str(payload.get("action") or "delete").strip().lower()
    raw_files = payload.get("files") if isinstance(payload.get("files"), list) else []
    confirmed_wrong_library_folder = bool(
        payload.get("confirmed_wrong_library_folder") or payload.get("allow_library_delete")
    )
    target_path = Path(current_decode)

    if os.path.islink(raw_path) or os.path.islink(target_path) or target_path.is_symlink():
        return {"ok": False, "error": f"Symlinks are not permitted: {raw_path}"}

    try:
        resolved_target = target_path.resolve(strict=False)
    except Exception:
        return {"ok": False, "error": "Invalid review folder path."}

    resolved_roots = [Path(r).resolve(strict=False) for r in (allowed_roots or [])]

    # Check root-itself refusal
    for r in resolved_roots:
        if resolved_target == r:
            return {"ok": False, "error": f"Cannot delete approved root {r} itself."}

    # Check music library refusal if not confirmed wrong library folder.
    # SEC-002 Wave 15 final review: this module must not depend on app.py
    # (it also runs inside the engine container, where app.py is never
    # imported) -- the caller (beets_control_agent.py, which already
    # resolves MUSIC_ROOT for allowed_roots) passes it explicitly instead
    # of this function reaching into sys.modules["app"] or re-deriving it
    # from os.environ on its own.
    from backend.config_layers import music_root as _configured_music_root
    music_root_path = Path(music_root or _configured_music_root()).resolve(strict=False)
    # F-3: album_id is not a gate -- the engine cannot prove the album owns
    # the target. The route verifies that and sets the explicit gate.
    library_delete_allowed = confirmed_wrong_library_folder

    # Defense in depth for #235: an allowed root that is "/" or overlaps the
    # library is refused. The library root itself is governed by the
    # library-delete gate (target and delete-source checks below).
    from backend.config_layers import unsafe_root_reason
    for r in resolved_roots:
        if os.path.realpath(str(r)) == os.path.realpath(str(music_root_path)):
            continue
        reason = unsafe_root_reason(r, music_root_path)
        if reason:
            return {"ok": False, "code": "import_review_unsafe_root", "mutated": False,
                    "error": f"Allowed cleanup root {r} is refused: {reason}."}

    if (resolved_target == music_root_path or music_root_path in resolved_target.parents) and not library_delete_allowed:
        return {"ok": False, "error": f"Review folder path {resolved_target} is inside music library."}
    refusal = _import_review_library_refusal(resolved_target, [], [music_root_path], library_delete_allowed)
    if refusal:
        return refusal

    expected_states: Dict[str, Any] = {}
    sources: List[str] = []
    skipped: List[Dict[str, str]] = []
    steps: List[Dict[str, Any]] = []
    step_idx = 1

    if raw_files:
        for f in raw_files:
            try:
                f_raw = str(f).strip()
                if not f_raw or "\x00" in f_raw or "%00" in f_raw:
                    skipped.append({"file": f_raw, "reason": "invalid_path"})
                    continue

                norm_raw = f_raw.replace("\\", "/")
                parts_raw = [p for p in norm_raw.split("/") if p]
                if any(p in {".", ".."} for p in parts_raw) or f_raw.startswith(("/", "\\")):
                    skipped.append({"file": f_raw, "reason": "outside_review_folder"})
                    continue

                # 1. Try literal path under resolved_target first
                literal_fp = Path(f_raw)
                if not literal_fp.is_absolute():
                    literal_fp = resolved_target / literal_fp
                
                try:
                    literal_resolved = literal_fp.resolve(strict=False)
                except Exception:
                    literal_resolved = None

                fp_to_use = None
                if literal_resolved and literal_resolved.exists() and literal_resolved.is_file() and not literal_fp.is_symlink() and not literal_resolved.is_symlink():
                    if literal_resolved != resolved_target and resolved_target in literal_resolved.parents:
                        fp_to_use = literal_resolved

                if not fp_to_use:
                    f_decode = f_raw
                    for _ in range(3):
                        dec = urllib.parse.unquote(f_decode)
                        if dec == f_decode:
                            break
                        f_decode = dec

                    if "\x00" in f_decode:
                        skipped.append({"file": f_raw, "reason": "invalid_path"})
                        continue

                    norm_f = f_decode.replace("\\", "/")
                    parts = [p for p in norm_f.split("/") if p]
                    if any(p in {".", ".."} for p in parts) or f_raw.startswith(("/", "\\")) or Path(f_decode).is_absolute():
                        skipped.append({"file": f_raw, "reason": "outside_review_folder"})
                        continue

                    fp = Path(f_decode)
                    if not fp.is_absolute():
                        fp = resolved_target / fp
                    fp_resolved = fp.resolve(strict=False)

                    if fp_resolved == resolved_target or resolved_target not in fp_resolved.parents:
                        skipped.append({"file": f_raw, "reason": "outside_review_folder"})
                        continue

                    # Check for symlinks in path components
                    has_symlink = False
                    curr_check = fp
                    while curr_check != resolved_target and resolved_target in curr_check.parents:
                        if curr_check.is_symlink() or os.path.islink(str(curr_check)):
                            has_symlink = True
                            break
                        curr_check = curr_check.parent

                    if has_symlink or os.path.islink(fp) or os.path.islink(fp_resolved) or fp.is_symlink() or fp_resolved.is_symlink():
                        skipped.append({"file": f_raw, "reason": "symlink"})
                        continue

                    if fp_resolved.exists() and fp_resolved.is_file():
                        fp_to_use = fp_resolved
                    else:
                        skipped.append({"file": f_raw, "reason": "not_found"})
                        continue

                if fp_to_use:
                    st = os.lstat(str(fp_to_use))
                    expected_states[str(fp_to_use)] = {
                        "size": st.st_size,
                        "mtime_ns": st.st_mtime_ns,
                        "mtime": st.st_mtime,
                        "dev": st.st_dev,
                        "ino": st.st_ino,
                        "is_file": True,
                    }
                    sources.append(str(fp_to_use))
                    is_quarantine = action in {"quarantine_rejected", "quarantine_duplicate"}
                    steps.append({
                        "step_id": f"step_{step_idx}",
                        "type": "move_quarantine" if is_quarantine else "delete_file",
                        "source": str(fp_to_use),
                        "status": "pending",
                        "reversibility": "RECOVERABLE" if is_quarantine else "IRREVERSIBLE",
                    })
                    step_idx += 1

            except Exception:
                skipped.append({"file": str(f), "reason": "invalid_path"})
    else:
        if resolved_target.is_dir():
            for fp in resolved_target.rglob("*"):
                if fp.is_file() and not os.path.islink(fp) and not fp.is_symlink():
                    st = os.lstat(str(fp.resolve()))
                    expected_states[str(fp.resolve())] = {
                        "size": st.st_size,
                        "mtime_ns": st.st_mtime_ns,
                        "mtime": st.st_mtime,
                        "dev": st.st_dev,
                        "ino": st.st_ino,
                        "is_file": True,
                    }
                    sources.append(str(fp.resolve()))
                    is_quarantine = action in {"quarantine_rejected", "quarantine_duplicate"}
                    steps.append({
                        "step_id": f"step_{step_idx}",
                        "type": "move_quarantine" if is_quarantine else "delete_file",
                        "source": str(fp.resolve()),
                        "status": "pending",
                        "reversibility": "RECOVERABLE" if is_quarantine else "IRREVERSIBLE",
                    })
                    step_idx += 1

    refusal = _import_review_library_refusal(
        resolved_target, [s["source"] for s in steps if s["type"] == "delete_file"], [music_root_path],
        library_delete_allowed)
    if refusal:
        return refusal

    payload_with_skipped = {**payload, "skipped": skipped}
    is_recoverable = action in {"quarantine_rejected", "quarantine_duplicate"}

    try:
        op_id, tx = store.create_import_review_cleanup_plan(
            target_path=str(resolved_target),
            allowed_roots=allowed_roots,
            source_paths=sources,
            expected_states=expected_states,
            reversibility="RECOVERABLE" if is_recoverable else "IRREVERSIBLE",
            payload=payload_with_skipped,
        )

        # Attach mutation_family, steps, and rollback_available to transaction metadata
        tx_meta = tx.get("metadata") or {}
        tx_meta["mutation_family"] = "import_review_cleanup_v1"
        tx_meta["music_root"] = str(music_root_path)
        tx_meta["steps"] = steps
        tx_meta["rollback_available"] = is_recoverable
        store.update(op_id, metadata=tx_meta)

        return {
            "ok": True,
            "operation_id": op_id,
            "status": "Preview",
            "action": action,
            "skipped": skipped,
            "skipped_count": len(skipped),
        }
    except ValueError as ex:
        return {"ok": False, "error": str(ex)}


def execute_import_review_cleanup_apply(
    store: TransactionStore,
    operation_id: str,
    quarantine_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute plan application for import review folder/files with durable steps and server-derived quarantine.

    Serializes concurrent Apply calls for the SAME operation_id (see
    _get_apply_lock) -- the control agent's ThreadingHTTPServer means two
    requests for the same transaction can genuinely arrive in parallel
    threads, and without this lock they could each read the same step as
    still-pending before either persists its completion.
    """
    with _get_apply_lock(operation_id):
        return _execute_import_review_cleanup_apply_locked(store, operation_id, quarantine_root)


def _execute_import_review_cleanup_apply_locked(
    store: TransactionStore,
    operation_id: str,
    quarantine_root: Optional[str] = None,
) -> Dict[str, Any]:
    # store.get() raises KeyError for BOTH a malformed id (fails
    # TransactionStore's id-format regex) and a well-formed but unknown
    # id -- it never returns a falsy value, so the "if not tx" pattern
    # here needs this except clause to actually be reachable. SEC-002
    # Wave 16 final review: a malformed operation_id must return a clean
    # {"ok": False, ...} result, not propagate an uncaught KeyError up
    # through the HTTP layer.
    try:
        tx = store.get(operation_id)
    except KeyError:
        tx = None
    if not tx:
        return {"ok": False, "error": f"Transaction {operation_id} not found."}

    meta = tx.get("metadata") or {}
    payload = meta.get("payload") or {}
    skipped = payload.get("skipped", [])

    if meta.get("mutation_family") and meta.get("mutation_family") != "import_review_cleanup_v1":
        return {"ok": False, "error": f"Transaction {operation_id} is not an import_review_cleanup_v1 operation."}

    if tx.get("status") == "Completed":
        return {
            "ok": True,
            "status": "Completed",
            "operation_id": operation_id,
            "deleted": meta.get("deleted", []),
            "moved": meta.get("moved", []),
            "skipped": skipped,
            "log": tx.get("logs", []),
        }

    is_valid, err = store.revalidate_preconditions(operation_id)
    if not is_valid:
        return {"ok": False, "error": err or "Precondition revalidation failed."}

    action = str(payload.get("action") or "delete").strip().lower()
    expected_states = meta.get("expected_states") or {}
    steps = meta.get("steps") or []

    # F2: re-check at apply against the recorded AND the current music root.
    from backend.config_layers import music_root as _configured_music_root
    library_delete_allowed = bool(
        payload.get("confirmed_wrong_library_folder") or payload.get("allow_library_delete"))
    roots = [_configured_music_root()] + ([str(meta["music_root"])] if meta.get("music_root") else [])
    refusal = _import_review_library_refusal(
        str(meta.get("target_path", "")),
        [s.get("source", "") for s in steps if s.get("type") == "delete_file"
         and s.get("status") not in ("completed", "irreversible_completed")],
        roots, library_delete_allowed)
    if refusal:
        return refusal

    log: List[str] = []
    deleted: List[str] = []
    moved: List[Dict[str, str]] = []

    target_path = Path(meta.get("target_path", ""))
    q_base = Path(quarantine_root or os.environ.get("IMPORT_REVIEW_QUARANTINE_DIR", "/config/import_review_quarantine"))
    op_q_dir = q_base / time.strftime("%Y%m%d") / operation_id

    # _is_symlink_path / _path_has_symlink_under are the module-level
    # shared helpers (see their definitions near the top of this file) --
    # this function previously defined its own private copies here.

    # Process steps durably
    for step in steps:
        st_status = step.get("status")
        if st_status in {"completed", "irreversible_completed"}:
            if step.get("type") == "move_quarantine" and step.get("destination"):
                moved.append({"source": step.get("source"), "quarantined": step.get("destination")})
            elif step.get("type") == "delete_file" and step.get("source"):
                deleted.append(step.get("source"))
            continue

        step["status"] = "running"
        try:
            step_type = step.get("type")
            src_str = step.get("source")
            if not src_str:
                step["status"] = "completed"
                continue
            src = Path(src_str)

            # Stat revalidation right before step execution
            if not src.exists():
                step["status"] = "completed"
                continue

            if _is_symlink_path(src):
                skipped.append({"file": str(src), "reason": "symlink"})
                step["status"] = "completed"
                continue

            # Compare current stat vs expected -- immediately before mutating,
            # not just once at the top of this function. Checks device+inode
            # (identifies the exact underlying file, not merely a look-alike at
            # the same path) and size+mtime_ns (catches an in-place content
            # swap on the same inode, e.g. truncate+rewrite, which a bare
            # inode check alone would miss).
            exp_st = expected_states.get(src_str) or {}
            try:
                st = os.lstat(str(src))
                mismatch = None
                if exp_st.get("ino") is not None and st.st_ino != exp_st["ino"]:
                    mismatch = "inode changed"
                elif exp_st.get("dev") is not None and st.st_dev != exp_st["dev"]:
                    mismatch = "device changed"
                elif exp_st.get("size") is not None and st.st_size != exp_st["size"]:
                    mismatch = "size changed"
                elif exp_st.get("mtime_ns") is not None and st.st_mtime_ns != exp_st["mtime_ns"]:
                    mismatch = "mtime changed"
                if mismatch:
                    step["status"] = "failed"
                    store.update(operation_id, status="Failed", logs=log + [f"File replacement detected at {src} ({mismatch})"])
                    return {"ok": False, "error": f"File replacement detected at {src} ({mismatch})"}
            except Exception:
                step["status"] = "completed"
                skipped.append({"file": str(src), "reason": "invalid_path"})
                continue

            if step_type == "move_quarantine":
                rel = src.relative_to(target_path) if target_path in src.parents else Path(src.name)
                dest = op_q_dir / rel
                step["destination"] = str(dest)

                try:
                    dest_parent = dest.parent
                    if _path_has_symlink_under(dest_parent, q_base):
                        skipped.append({"file": str(src), "reason": "symlink"})
                        step["status"] = "completed"
                        continue

                    dest_parent.mkdir(parents=True, exist_ok=True)

                    if _path_has_symlink_under(dest_parent, q_base) or _is_symlink_path(dest) or dest.is_symlink():
                        skipped.append({"file": str(src), "reason": "symlink"})
                        step["status"] = "completed"
                        continue

                    q_resolved = q_base.resolve(strict=False)
                    dest_resolved = dest_parent.resolve(strict=False)
                    if dest_resolved == q_resolved or q_resolved not in dest_resolved.parents:
                        skipped.append({"file": str(src), "reason": "unsafe_destination"})
                        step["status"] = "completed"
                        continue

                    # shutil.move() silently descends into an existing directory
                    # destination (moving src *inside* it, using src's own
                    # basename) rather than treating dest as the literal target
                    # -- the exact gotcha the pre-Wave-15 app.py code for this
                    # same quarantine operation was written to avoid. Refuse
                    # outright if anything already exists at the exact chosen
                    # leaf, and use os.rename()/copy instead of shutil.move so
                    # dest is always the literal target, never a directory to
                    # land inside.
                    if dest.exists() or dest.is_symlink():
                        skipped.append({"file": str(src), "reason": "unsafe_destination"})
                        step["status"] = "completed"
                        continue
                except Exception:
                    skipped.append({"file": str(src), "reason": "unsafe_destination"})
                    step["status"] = "completed"
                    continue

                try:
                    try:
                        os.rename(str(src), str(dest))
                    except OSError as exc:
                        if getattr(exc, "errno", None) != errno.EXDEV:
                            raise
                        shutil.copyfile(str(src), str(dest))
                        try:
                            shutil.copystat(str(src), str(dest))
                        except Exception:
                            pass
                        src.unlink()

                    dest_res = dest.resolve(strict=False)
                    is_invalid = _is_symlink_path(dest) or dest.is_symlink() or not dest.is_file() or dest_res == q_resolved or q_resolved not in dest_res.parents

                    if is_invalid:
                        # The source was swapped for a symlink (or dest was
                        # otherwise replaced) in the instant between our checks
                        # and the rename/copy syscall: undo rather than report a
                        # false success.
                        try:
                            if not src.exists() and dest.exists() and not dest.is_symlink() and dest.is_file():
                                os.rename(str(dest), str(src))
                            elif dest.exists() or dest.is_symlink():
                                os.unlink(str(dest))
                        except Exception:
                            pass
                        skipped.append({"file": str(src), "reason": "symlink"})
                        step["status"] = "completed"
                        continue

                    moved.append({"source": str(src), "quarantined": str(dest)})
                    log.append(f"Quarantined {src} -> {dest}")
                    step["status"] = "completed"
                except Exception as ex:
                    skipped.append({"file": str(src), "reason": f"failed_move: {ex}"})
                    step["status"] = "completed"

            elif step_type == "delete_file":
                try:
                    src.unlink()
                    deleted.append(str(src))
                    log.append(f"Deleted {src}")
                    step["status"] = "irreversible_completed"
                except Exception as ex:
                    skipped.append({"file": str(src), "reason": f"failed_delete: {ex}"})
                    step["status"] = "completed"
        finally:
            # Persist per-step progress durably (not just at the very end
            # of this function) so a crash mid-loop cannot lose track of
            # which steps already mutated the filesystem. Without this, a
            # crash after this step legitimately completed but before the
            # function's final store.update() call would leave the step
            # recorded as "pending" on disk -- a retry would then either
            # try to redo an already-completed destructive mutation, or
            # (via revalidate_preconditions' expected_states check) refuse
            # to proceed at all because the file it already correctly
            # deleted/moved no longer matches its pre-mutation stat.
            store.update(operation_id, metadata={"steps": steps})

    # Cleanup empty parent directories
    if target_path.exists() and target_path.is_dir():
        for d in sorted([p for p in target_path.rglob("*") if p.is_dir()], key=lambda x: len(x.parts), reverse=True):
            try:
                if not any(d.iterdir()):
                    d.rmdir()
            except Exception:
                pass
        if not any(target_path.iterdir()):
            try:
                target_path.rmdir()
                log.append(f"Deleted empty directory {target_path}")
            except Exception:
                pass

    has_failed = any(s.get("status") == "failed" for s in steps)
    final_status = "Completed" if not has_failed else "Partially Rolled Back"

    has_irreversible = any(s.get("status") == "irreversible_completed" for s in steps)
    rollback_avail = meta.get("rollback_available", True) and not has_irreversible and not has_failed

    store.update(
        operation_id,
        status=final_status,
        applied_at=time.time(),
        metadata={**meta, "deleted": deleted, "moved": moved, "steps": steps, "rollback_available": rollback_avail},
        logs=log,
    )

    return {
        "ok": not has_failed,
        "status": final_status,
        "operation_id": operation_id,
        "deleted": deleted,
        "moved": moved,
        "skipped": skipped,
        "log": log,
    }


def rollback_import_review_cleanup(
    store: TransactionStore,
    operation_id: str,
) -> Dict[str, Any]:
    """Rollback a recoverable import review cleanup transaction by moving files back from quarantine.

    This executor's restore logic only understands "move_quarantine" steps
    (the Import Review cleanup shape). It must never be reached for a
    transaction from a different mutation family (e.g. "album_cleanup_v1",
    whose steps are delete_file/delete_db_record/remove_dir) -- silently
    finding zero matching steps and reporting a false "Rolled Back" success
    without having restored anything. SEC-002 Wave 16 final review: callers
    (Web Manager routes, other engine endpoints) must not be relied on to
    enforce this; it is enforced here, at the authoritative layer, matching
    the same explicit mutation_family check execute_album_cleanup_apply()
    already performs for its own family.
    """
    try:
        tx = store.get(operation_id)
    except KeyError:
        tx = None
    if not tx:
        return {"ok": False, "error": f"Transaction {operation_id} not found."}

    meta = tx.get("metadata") or {}
    mutation_family = meta.get("mutation_family")
    if mutation_family and mutation_family != "import_review_cleanup_v1":
        return {
            "ok": False,
            "error": (
                f"Transaction {operation_id} is a {mutation_family!r} operation "
                "and cannot be rolled back through the import review cleanup "
                "rollback executor."
            ),
        }

    if not meta.get("rollback_available"):
        return {"ok": False, "error": "Rollback unavailable: transaction contains irreversible steps or has already been rolled back."}

    steps = meta.get("steps") or []
    log: List[str] = []
    restored: List[str] = []

    for step in steps:
        if step.get("type") == "move_quarantine" and step.get("status") == "completed":
            src_str = step.get("source")
            dest_str = step.get("destination")
            if src_str and dest_str:
                dest = Path(dest_str)
                src = Path(src_str)
                if not dest.exists() or dest.is_symlink():
                    continue
                # Refuse rather than silently overwrite/descend if something
                # has since reappeared at the original path (a new file
                # created there since quarantine, or -- via shutil.move's
                # directory-descend behavior -- a directory), and refuse if
                # any existing parent component of the restore target is a
                # symlink.
                if src.exists() or src.is_symlink():
                    log.append(f"Skipped restore of {dest}: {src} already exists")
                    continue
                parent = src.parent
                existing_parent = parent
                while not existing_parent.exists() and existing_parent != existing_parent.parent:
                    existing_parent = existing_parent.parent
                if existing_parent.is_symlink():
                    log.append(f"Skipped restore of {dest}: unsafe parent directory for {src}")
                    continue
                try:
                    parent.mkdir(parents=True, exist_ok=True)
                    if src.exists() or src.is_symlink():
                        log.append(f"Skipped restore of {dest}: {src} already exists")
                        continue
                    try:
                        os.rename(str(dest), str(src))
                    except OSError as exc:
                        if getattr(exc, "errno", None) != errno.EXDEV:
                            raise
                        shutil.copyfile(str(dest), str(src))
                        try:
                            shutil.copystat(str(dest), str(src))
                        except Exception:
                            pass
                        dest.unlink()
                except Exception as ex:
                    log.append(f"Failed to restore {dest} -> {src}: {ex}")
                    continue
                restored.append(src_str)
                log.append(f"Restored {dest} -> {src}")
                step["status"] = "rolled_back"

    tx_meta = {**meta, "rollback_available": False, "steps": steps}
    store.update(
        operation_id,
        status="Rolled Back",
        applied_at=time.time(),
        metadata=tx_meta,
        logs=tx.get("logs", []) + log,
    )

    return {
        "ok": True,
        "status": "Rolled Back",
        "operation_id": operation_id,
        "restored": restored,
        "log": log,
    }


def _bulk_replacement_toctou_check(path_str: str, expected_stat: Optional[Dict[str, Any]], roots_resolved: List[Path]) -> Optional[str]:
    """Full dev/inode/size/mtime_ns TOCTOU re-verification plus a fresh
    root-containment + parent-symlink-component walk, immediately before
    mutation. Returns an error string, or None if the file is unchanged
    and safe."""
    if not path_str:
        return "missing path"
    if os.path.islink(path_str):
        return "symlink at leaf"
    p = Path(path_str)
    if p.is_symlink():
        return "symlink at leaf"
    abs_p = Path(os.path.normpath(str(p if p.is_absolute() else p.absolute())))
    root = next((r for r in roots_resolved if abs_p == r or r in abs_p.parents), None)
    if root is None:
        return "root escape"
    if _path_has_symlink_under(abs_p, root):
        return "symlink component"
    try:
        resolved = abs_p.resolve(strict=False)
    except Exception:
        return "unresolvable path"
    if resolved.is_symlink():
        return "symlink target"
    if not (resolved == root or root in resolved.parents):
        return "root escape after resolve"
    if not resolved.exists() or not resolved.is_file():
        return "file missing"
    if expected_stat:
        st_now = resolved.stat()
        if expected_stat.get("dev") is not None and st_now.st_dev != expected_stat["dev"]:
            return "device changed"
        if expected_stat.get("inode") is not None and st_now.st_ino != expected_stat["inode"]:
            return "inode changed"
        if expected_stat.get("size") is not None and st_now.st_size != expected_stat["size"]:
            return "size changed"
        mtime_ns_now = getattr(st_now, "st_mtime_ns", int(st_now.st_mtime * 1e9))
        if expected_stat.get("mtime_ns") is not None and mtime_ns_now != expected_stat["mtime_ns"]:
            return "mtime changed"
    return None



def _normpath_within_roots(path_str: str, roots: List[Path]) -> bool:
    """Textual `os.path.normpath` + prefix containment check -- the idiom
    CodeQL's py/path-injection query recognizes as a sanitizing barrier.
    Always paired with a resolve()-based check (`_resolve_path_role` /
    `_path_under`), which is what actually matters at runtime since it
    also collapses symlinks; this one exists purely so static analysis can
    see the same containment guarantee those checks already provide."""
    try:
        norm = os.path.normpath(path_str)
    except Exception:
        return False
    for r in roots:
        try:
            r_norm = os.path.normpath(str(r))
        except Exception:
            continue
        if norm == r_norm or norm.startswith(r_norm + os.sep):
            return True
    return False


@contextmanager
def _lock_resources(resource_keys: List[str]):
    locks = [_get_resource_lock(k) for k in sorted(set(resource_keys or []))]
    with ExitStack() as stack:
        for l in locks:
            stack.enter_context(l)
        yield


def _path_under(child: Path, parent: Path) -> bool:
    try:
        if parent.drive and not child.drive:
            child = Path(parent.drive + str(child))
        elif child.drive and not parent.drive:
            parent = Path(child.drive + str(parent))
        child_norm = os.path.normcase(os.path.normpath(str(child)))
        parent_norm = os.path.normcase(os.path.normpath(str(parent)))
        sep = os.path.normcase(os.sep)
        if child_norm != parent_norm and not child_norm.startswith(parent_norm + sep):
            return False
    except Exception:
        return False
    try:
        c_res = child.resolve(strict=False)
        p_res = parent.resolve(strict=False)
        c_res.relative_to(p_res)
        return True
    except Exception:
        return False


# SEC-002 / ARCH-003 Wave 24 final review round 4 (CodeQL closure): a single,
# narrow path-validation primitive for the artwork family, consolidating the
# containment + symlink-rejection pattern that was previously re-checked
# ad hoc at each call site with slightly different combinations of
# _path_under()/_path_has_symlink_under(). This does not change the
# underlying safety model -- `_path_under` (component-aware containment via
# Path.relative_to(), not string-prefix matching) and
# `_path_has_symlink_under` (walks every parent component, not just the
# leaf) remain the actual safety primitives -- it exists so every artwork
# mutation sink consumes ONE proven-safe, resolved Path rather than each
# caller separately re-deriving (and potentially under-deriving) the same
# check from the original tainted string/Path.
#
# `allowed_roots` must always be server/operator-owned (env config, or a
# value already itself proven contained by a prior call to this same
# primitive -- e.g. Apply re-validating target_dir before using it as the
# allowed root for each per-item destination). Never pass a DB value,
# request payload value, or transaction-metadata value as an allowed root:
# doing so would let attacker- or DB-controlled data enlarge the trust
# boundary instead of merely being validated against it.
def validate_path_under_allowed_roots(
    candidate: Any,
    allowed_roots: Iterable[Any],
    *,
    reject_symlinks: bool = True,
) -> Optional[Path]:
    """Prove `candidate` is contained under one of `allowed_roots` and
    return the canonical, resolved Path that is actually permitted to reach
    a filesystem sink. Fails closed (returns None) on any ambiguity,
    resolution error, missing containment, or rejected symlink component --
    callers must treat None as "reject", never as "no opinion"."""
    try:
        cand = candidate if isinstance(candidate, Path) else Path(str(candidate))
    except Exception:
        return None
    roots: List[Path] = []
    for r in allowed_roots or []:
        if not r:
            continue
        try:
            roots.append(r if isinstance(r, Path) else Path(str(r)))
        except Exception:
            continue
    selected_root: Optional[Path] = None
    for root in roots:
        if _path_under(cand, root):
            selected_root = root
            break
    if selected_root is None:
        return None
    if reject_symlinks and _path_has_symlink_under(cand, selected_root):
        return None
    # Textual barrier on the exact value that reaches the sink, in the form
    # CodeQL py/path-injection recognises (alerts #1385/#1386). Exact
    # component-wise containment is _path_under above.
    cand_text = os.path.normpath(os.path.abspath(os.fspath(cand)))
    if not cand_text.startswith(os.path.normpath(os.path.abspath(os.fspath(selected_root)))):
        return None
    try:
        resolved = Path(cand_text).resolve(strict=False)
        # normpath collapses ".." before symlinks are followed, so with
        # reject_symlinks=False the value returned can differ from the one
        # _path_under checked; prove the returned value is contained too.
        resolved.relative_to(selected_root.resolve(strict=False))
        return resolved
    except Exception:
        return None


# ── artwork image-format sniffing (used by the artwork upload routes) ────────
_ARTWORK_ALLOWED_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})


def _sniff_unsupported_image_format(data: bytes, allowed: Any = _ARTWORK_ALLOWED_FORMATS) -> str:
    """Name a recognised image format outside ``allowed``, or return "".

    Uses only Pillow's prefix ``accept`` checks on the first 16 bytes (what
    Image.open itself does to pick a plugin), never a plugin's parser, so a
    payload restricted out by ``formats=`` can still be reported as "wrong
    type" rather than "corrupt" without being decoded."""
    from PIL import Image
    Image.init()
    prefix = bytes(data[:16])
    for fmt, entry in Image.OPEN.items():
        accept = entry[1] if isinstance(entry, tuple) and len(entry) > 1 else None
        if fmt in allowed or accept is None:
            continue
        try:
            if accept(prefix):
                return fmt
        except Exception:
            continue
    return ""



def _cleanup_resolve_path(path: Path) -> Path:
    try:
        raw = os.fspath(path)
    except TypeError:
        raw = str(path)
    return Path(os.path.abspath(os.path.normpath(raw)))


def _cleanup_validate_path_under_roots(candidate: Any, roots: List[Path]) -> Optional[Path]:
    try:
        text = os.fspath(candidate) if isinstance(candidate, Path) else str(candidate)
    except Exception:
        return None
    if not _normpath_within_roots(text, roots):
        return None
    return validate_path_under_allowed_roots(text, roots)


def _cleanup_normalize_roots(values: Optional[List[str]], fallback: List[str]) -> List[Path]:
    roots: List[Path] = []
    for raw in list(values or []) + list(fallback or []):
        value = _s(raw).strip()
        if not value:
            continue
        root = _cleanup_resolve_path(Path(value))
        if root not in roots:
            roots.append(root)
    return roots


def _cleanup_root_for_path(path: Path, roots: List[Path]) -> Optional[Path]:
    matches = [root for root in roots if _path_under(path, root)]
    if not matches:
        return None
    return max(matches, key=lambda p: len(p.parts))


def _cleanup_dir_stat_record(path: Path, root: Path) -> Dict[str, Any]:
    if not _path_under(path, root) or _path_has_symlink_under(path, root):
        raise ValueError("cleanup directory outside validated root")
    st = path.stat()
    return {"dev": st.st_dev, "ino": st.st_ino, "mtime_ns": st.st_mtime_ns, "type": "directory"}


def _cleanup_dir_stat_matches(path: Path, expected: Dict[str, Any], root: Path) -> bool:
    if not expected:
        return False
    try:
        st = path.stat()
    except OSError:
        return False
    return st.st_dev == expected.get("dev") and st.st_ino == expected.get("ino")


def _cleanup_stat_matches(path: Path, expected: Dict[str, Any], root: Path) -> bool:
    if not expected:
        return False
    expected_stat = dict(expected)
    if "ino" in expected_stat and "inode" not in expected_stat:
        expected_stat["inode"] = expected_stat["ino"]
    try:
        return _bulk_replacement_toctou_check(str(path), expected_stat, [root]) is None
    except Exception:
        return False


# ── folder_cleanup_v1 ────────────────────────────────────────────────────────

def create_folder_cleanup_plan(
    store: TransactionStore,
    payload: Dict[str, Any],
    *,
    music_allowed_roots: Optional[List[str]] = None,
    staging_allowed_roots: Optional[List[str]] = None,
    quarantine_base_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a non-mutating preview plan for folder/placeholder cleanup actions.

    Beets references are not checked here: Web Manager never opens the Beets
    library file (BA-7). Callers check them through the adapter
    (composite_workflows._library_refs_under)."""
    action = str(payload.get("action") or payload.get("mode") or "remove_empty").strip()
    src_folder = str(payload.get("source") or payload.get("source_folder") or payload.get("source_path") or "").strip()
    target_folder = str(payload.get("target") or payload.get("target_folder") or payload.get("target_path") or payload.get("proposed_path") or "").strip()

    if not src_folder:
        return {"ok": False, "error": "source folder required", "code": "folder_cleanup_invalid_payload"}

    allowed_roots = _cleanup_normalize_roots(music_allowed_roots, [_cfg_music_root()])
    src_display = _cleanup_resolve_path(Path(src_folder))
    if not _normpath_within_roots(src_folder, allowed_roots):
        return {"ok": False, "error": f"Folder outside allowed root: {src_display}", "code": "folder_cleanup_path_out_of_root"}
    src_p = _cleanup_validate_path_under_roots(src_folder, allowed_roots)
    if src_p is None:
        return {"ok": False, "error": f"Symlink rejected: {src_display}", "code": "folder_cleanup_symlink_rejected"}
    root = _cleanup_root_for_path(src_p, allowed_roots)
    if root is None:
        return {"ok": False, "error": f"Folder outside allowed root: {src_display}", "code": "folder_cleanup_path_out_of_root"}

    src_path_text = os.path.abspath(os.path.normpath(str(src_p)))
    root_text = os.path.abspath(os.path.normpath(str(root)))
    if src_path_text == root_text:
        return {"ok": False, "error": "Refusing to modify allowed root directory itself", "code": "folder_cleanup_root_refused"}

    file_moves: List[Dict[str, Any]] = []
    dir_removals: List[Any] = []
    dir_renames: List[Dict[str, Any]] = []

    if action in ("remove_empty_source", "remove_empty"):
        src_path_text = os.path.abspath(os.path.normpath(str(src_p)))
        root_text = os.path.abspath(os.path.normpath(str(root)))
        if src_path_text != root_text and not src_path_text.startswith(root_text + os.sep):
            return {"ok": False, "error": f"Folder outside allowed root: {src_display}", "code": "folder_cleanup_path_out_of_root"}
        src_p = Path(src_path_text)
        try:
            unexpected = [child.name for child in src_p.iterdir()]
        except FileNotFoundError:
            pass
        except NotADirectoryError:
            return {"ok": False, "error": f"Source is not a directory: {src_p}", "code": "folder_cleanup_not_directory"}
        except OSError as ex:
            return {"ok": False, "error": f"Could not inspect directory: {ex}", "code": "folder_cleanup_scan_failed"}
        else:
            if unexpected:
                return {"ok": False, "error": "Directory is not empty", "code": "folder_cleanup_not_empty", "unexpected_entries": sorted(unexpected)[:20]}
            dir_removals.append({"path": str(src_p), "expected_empty": True, "stat": _cleanup_dir_stat_record(src_p, root)})
    elif action in ("safe_rename", "rename_folder"):
        if not target_folder:
            return {"ok": False, "error": "target folder required", "code": "folder_cleanup_invalid_payload"}
        tgt_p = _cleanup_resolve_path(Path(target_folder))
        tgt_display = str(tgt_p)
        if not _normpath_within_roots(str(tgt_p), allowed_roots):
            return {"ok": False, "error": f"Target outside allowed root: {tgt_display}", "code": "folder_cleanup_path_out_of_root"}
        tgt_root = _cleanup_root_for_path(tgt_p, allowed_roots)
        if tgt_root is None:
            return {"ok": False, "error": f"Target outside allowed root: {tgt_display}", "code": "folder_cleanup_path_out_of_root"}
        if not src_p.exists() or not src_p.is_dir():
            return {"ok": False, "error": f"Source is not a directory: {src_display}", "code": "folder_cleanup_not_directory"}
        if not tgt_p.parent.exists() or not tgt_p.parent.is_dir():
            return {"ok": False, "error": "Target parent directory does not exist", "code": "folder_cleanup_target_parent_missing"}
        if _path_has_symlink_under(tgt_p.parent, tgt_root):
            return {"ok": False, "error": f"Symlink rejected: {tgt_display}", "code": "folder_cleanup_symlink_rejected"}
        if tgt_p.exists() or tgt_p.is_symlink():
            return {"ok": False, "error": "Target folder already exists", "code": "folder_cleanup_target_exists"}
        dir_renames.append({"source": str(src_p), "target": str(tgt_p), "stat": _cleanup_dir_stat_record(src_p, root)})
    elif action in ("merge_source_files", "merge"):
        if not target_folder:
            return {"ok": False, "error": "target folder required", "code": "folder_cleanup_invalid_payload"}
        tgt_p = _cleanup_resolve_path(Path(target_folder))
        tgt_display = str(tgt_p)
        if not _normpath_within_roots(str(tgt_p), allowed_roots):
            return {"ok": False, "error": f"Target outside allowed root: {tgt_display}", "code": "folder_cleanup_path_out_of_root"}
        tgt_root = _cleanup_root_for_path(tgt_p, allowed_roots)
        if tgt_root is None:
            return {"ok": False, "error": f"Target outside allowed root: {tgt_display}", "code": "folder_cleanup_path_out_of_root"}
        if not src_p.exists() or not src_p.is_dir():
            return {"ok": False, "error": f"Source is not a directory: {src_display}", "code": "folder_cleanup_not_directory"}
        if not tgt_p.exists() or not tgt_p.is_dir():
            return {"ok": False, "error": "Target folder does not exist", "code": "folder_cleanup_target_missing"}
        if src_p.resolve(strict=False) == tgt_p.resolve(strict=False):
            return {"ok": False, "error": "Source and target folders must differ", "code": "folder_cleanup_invalid_payload"}
        if _path_has_symlink_under(tgt_p, tgt_root):
            return {"ok": False, "error": f"Symlink rejected: {tgt_display}", "code": "folder_cleanup_symlink_rejected"}
        source_dirs: List[Path] = []
        for f in src_p.rglob("*"):
            if f.is_dir():
                source_dirs.append(f)
                continue
            if not f.is_file():
                continue
            if _path_has_symlink_under(f, root):
                return {"ok": False, "error": f"Symlink rejected: {f}", "code": "folder_cleanup_symlink_rejected"}
            rel = f.relative_to(src_p)
            dest_f = tgt_p / rel
            if not _path_under(dest_f, tgt_p):
                return {"ok": False, "error": f"Target outside merge folder: {dest_f}", "code": "folder_cleanup_path_out_of_root"}
            if not dest_f.parent.exists() or not dest_f.parent.is_dir():
                return {"ok": False, "error": "Target subfolder does not exist; cleanup apply will not create folders", "code": "folder_cleanup_target_parent_missing"}
            if _path_has_symlink_under(dest_f.parent, tgt_root):
                return {"ok": False, "error": f"Symlink rejected: {dest_f}", "code": "folder_cleanup_symlink_rejected"}
            if dest_f.exists() or dest_f.is_symlink():
                return {"ok": False, "error": "Target file already exists", "code": "folder_cleanup_target_exists"}
            st = f.stat()
            file_moves.append({
                "source": str(f),
                "target": str(dest_f),
                "stat": {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns},
            })
        if not file_moves:
            return {"ok": False, "error": "No source-only files are available to merge", "code": "folder_cleanup_noop"}
        for d in sorted(source_dirs, key=lambda p: len(p.parts), reverse=True):
            dir_removals.append({"path": str(d), "expected_empty": True, "stat": _cleanup_dir_stat_record(d, root)})
        dir_removals.append({"path": str(src_p), "expected_empty": True, "stat": _cleanup_dir_stat_record(src_p, root)})
    else:
        return {"ok": False, "error": f"Unsupported folder cleanup action: {action}", "code": "folder_cleanup_invalid_payload"}

    resource_keys = [f"folder:{hashlib.sha256(str(src_p).encode('utf-8', 'surrogateescape')).hexdigest()}"]
    if target_folder:
        resource_keys.append(f"folder:{hashlib.sha256(str(_cleanup_resolve_path(Path(target_folder))).encode('utf-8', 'surrogateescape')).hexdigest()}")

    tx = store.create(
        operation_type="Folder Cleanup",
        status="Preview",
        summary=f"Folder cleanup ({action}): {src_p.name}",
        rollback_available=bool(dir_removals or file_moves or dir_renames),
        rollback_reason="Empty directory cleanup can recreate removed directories; moves/renames can be moved back." if (dir_removals or file_moves or dir_renames) else "No mutation is planned.",
        metadata={
            "mutation_family": "folder_cleanup_v1",
            "action": action,
            "source": str(src_p),
            "target": target_folder,
            "file_moves": file_moves,
            "dir_removals": dir_removals,
            "dir_renames": dir_renames,
            "resource_keys": resource_keys,
            "allowed_roots": [str(root) for root in allowed_roots],
            "created_at": _now(),
        },
    )
    return {
        "ok": True,
        "operation_id": tx["id"],
        "action": action,
        "moves_count": len(file_moves),
        "removals_count": len(dir_removals),
    }

def _folder_adapter(adapter: Any = None) -> Any:
    if adapter is not None:
        return adapter
    from backend import beets_adapter as _beets_adapter  # at call time, so tests can patch it
    return _beets_adapter.beets_adapter


#: A step Beets may have done without confirming it (lost response, timeout,
#: 202 still running). Its idempotency key is replayed to learn the outcome.
FOLDER_OP_UNCONFIRMED = "FOLDER_OP_UNCONFIRMED"
_FOLDER_STEP_ATTEMPTS = 3
#: Operator text for a failed plugin step, by error code. Static on purpose:
#: Beets' own reply text never reaches an API response.
FOLDER_STEP_MESSAGES = {
    "BEETS_NOT_FOUND": "the webmanager plugin needs 1.7.0; restart Beets after the plugin update",
    "BEETS_UNREACHABLE": "Beets is unreachable",
    FOLDER_OP_UNCONFIRMED: "Beets did not confirm the step; the transaction recorded it so rollback can undo it",
    "NOT_EMPTY": "the folder is not empty",
    "PATH_IS_TRACKED": "the path holds Beets library items; move them through Beets",
    "TARGET_EXISTS": "the target already exists",
    "TARGET_PARENT_MISSING": "the target's parent folder does not exist",
    "SOURCE_MISSING": "the source is missing",
    "SYMLINK_REJECTED": "a path component is a symlink",
    "PATH_OUTSIDE_LIBRARY": "the path is outside the Beets library directory",
    "REMOVE_FAILED": "Beets could not remove the folder",
    "FOLDER_OP_FAILED": "Beets could not perform the step",
    "LIBRARY_DIRECTORY_UNKNOWN": "the Beets library directory is not configured",
}
_FOLDER_STEP_RETRY_DELAY = 1.0


def _folder_step(adapter: Any, key: str, op: str, **paths: str) -> Optional[str]:
    """Run one folder step inside Beets (Web Manager mounts the library
    read-only). None when Beets confirmed it, else a short reason: an error
    code, never raw upstream text. A reply that leaves the outcome unknown is
    retried with the same idempotency key, which makes the plugin report the
    first attempt's result instead of running the step twice; if it stays
    unknown the reason starts with ``FOLDER_OP_UNCONFIRMED``."""
    for attempt in range(_FOLDER_STEP_ATTEMPTS):
        if attempt:
            time.sleep(_FOLDER_STEP_RETRY_DELAY)
        try:
            res = adapter.folder_op(op, key, **paths)
        except Exception as exc:
            code = getattr(exc, "error_code", "") or type(exc).__name__
            if not getattr(exc, "status_code", None):  # no HTTP reply (connection error/timeout)
                last = f"{FOLDER_OP_UNCONFIRMED} ({code})"  # no reply: Beets may have done it
                continue
            if code == "BEETS_NOT_FOUND":
                return "BEETS_NOT_FOUND (the webmanager plugin needs 1.7.0; restart Beets after the plugin update)"
            return str(code)
        if isinstance(res, dict) and (res.get("success") is True or res.get("status") == "succeeded"):
            return None
        last = FOLDER_OP_UNCONFIRMED
    return last


def execute_folder_cleanup_apply(
    store: TransactionStore,
    operation_id: str,
    *,
    music_allowed_roots: Optional[List[str]] = None,
    adapter: Any = None,
) -> Dict[str, Any]:
    """Apply an Approved folder_cleanup_v1 plan. Web Manager re-checks every
    step against its read-only view of the library, then Beets performs it
    through the webmanager plugin (``folder_op``). Each confirmed step is
    recorded at once (``engine_result``), so a failure part-way ends Failed
    with exactly what changed and rollback can undo it."""
    if not _TRANSACTION_ID_RE.match(operation_id):
        return {"ok": False, "error": "Invalid transaction ID format", "code": "folder_cleanup_invalid_id"}

    with _get_apply_lock(operation_id):
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": f"Transaction {operation_id} not found", "code": "folder_cleanup_not_found"}

        meta = tx.get("metadata") or {}
        if meta.get("mutation_family") != "folder_cleanup_v1":
            return {"ok": False, "error": "Transaction is not a folder_cleanup_v1 operation", "code": "folder_cleanup_family_mismatch"}

        status = tx.get("status")
        if status == "Completed":
            return {"ok": True, "operation_id": operation_id, "status": "Completed",
                    "mutated": bool(meta.get("filesystem_mutated")), "idempotent": True}
        # CAS source: Approved, or Running claimed by the caller (claim_approved)
        # before any step started. Preview, Failed, Cancelled and an apply that
        # already started are refused.
        if not (status == "Approved" or (status == "Running" and not meta.get("mutation_started")
                                         and not meta.get("engine_result"))):
            return {"ok": False, "code": "not_approved", "operation_id": operation_id, "status": status, "mutated": False,
                    "error": f"Only an Approved transaction can be applied (this one is {status}); nothing was changed."}

        resource_keys = meta.get("resource_keys") or []
        allowed_roots = _cleanup_normalize_roots(music_allowed_roots or meta.get("allowed_roots"), [_cfg_music_root()])
        ad = _folder_adapter(adapter)
        moved_records: List[Dict[str, Any]] = []
        removed_dirs: List[str] = []

        def _record(done: str) -> None:
            result = {"moved_records": list(moved_records), "removed_dirs": list(removed_dirs)}
            store.update(operation_id, metadata={"filesystem_mutated": True, "engine_result": result, **result})
            store.append_log(operation_id, f"Beets: {done}")

        def _step(op: str, **paths: str) -> Optional[str]:
            return _folder_step(ad, f"{operation_id}:apply:{len(moved_records) + len(removed_dirs)}", op, **paths)

        def _fail(msg: str, code: str, step_error: Optional[str] = None) -> Dict[str, Any]:
            mutated = bool(moved_records or removed_dirs)
            step_code = step_error.split(" ", 1)[0] if step_error else None
            if step_code is not None and step_code not in FOLDER_STEP_MESSAGES:
                step_code = "FOLDER_OP_FAILED"  # only allowlisted codes leave the engine
            store.append_log(operation_id, f"Apply failed: {msg}")
            store.update(operation_id, status="Failed", rollback={
                "available": mutated,
                "reason": "Rollback can restore recorded moves/directories." if mutated else "No mutation was performed."})
            return {"ok": False, "error": msg, "code": code, "mutated": mutated, "rollback_available": mutated,
                    "operation_id": operation_id, "status": "Failed",
                    "moved_records": list(moved_records), "removed_dirs": list(removed_dirs),
                    "step_error_code": step_code,
                    "step_error_message": FOLDER_STEP_MESSAGES.get(step_code) if step_code else None}

        with _lock_resources(resource_keys):
            file_moves = meta.get("file_moves") or []
            for fm in file_moves:
                sp = Path(fm["source"])
                root = _cleanup_root_for_path(sp, allowed_roots)
                if root is None:
                    return _fail(f"Source outside allowed roots: {sp}", "folder_cleanup_path_out_of_root")
                if _path_has_symlink_under(sp, root):
                    return _fail(f"Symlink detected on source: {sp}", "folder_cleanup_symlink_rejected")
                if not sp.exists() or not sp.is_file():
                    return _fail(f"Source file missing: {sp}", "folder_cleanup_toctou_mismatch")
                if "stat" in fm and not _cleanup_stat_matches(sp, fm["stat"], root):
                    return _fail(f"Source file stat changed: {sp}", "folder_cleanup_toctou_mismatch")
                tp = Path(fm["target"])
                tp_root = _cleanup_root_for_path(tp, allowed_roots)
                if tp_root is None:
                    return _fail(f"Target outside allowed roots: {tp}", "folder_cleanup_path_out_of_root")
                if not tp.parent.exists() or not tp.parent.is_dir():
                    return _fail(f"Target parent directory missing: {tp.parent}", "folder_cleanup_target_parent_missing")
                if _path_has_symlink_under(tp.parent, tp_root):
                    return _fail(f"Symlink detected on target: {tp}", "folder_cleanup_symlink_rejected")
                if tp.exists() or tp.is_symlink():
                    return _fail(f"Target already exists: {tp}", "folder_cleanup_target_exists")

            if _claim_apply_running(store, operation_id, status, {"mutation_started": True}) is None:
                return _claim_lost(store, operation_id)

            for fm in file_moves:
                sp = Path(fm["source"])
                tp = Path(fm["target"])
                if not sp.exists() or not sp.is_file():
                    return _fail(f"Source file missing: {sp}", "folder_cleanup_toctou_mismatch")
                if tp.exists() or tp.is_symlink():
                    return _fail(f"Target already exists: {tp}", "folder_cleanup_target_exists")
                err = _step("move_file", source=str(sp), target=str(tp))
                if err and err.startswith(FOLDER_OP_UNCONFIRMED):
                    moved_records.append({"source": str(sp), "target": str(tp), "kind": "file", "unconfirmed": True})
                    _record(f"move {sp} -> {tp} not confirmed; recorded for rollback")
                if err:
                    return _fail(f"Move failed {sp} -> {tp}: {err}", "folder_cleanup_move_failed", err)
                moved_records.append({"source": str(sp), "target": str(tp), "kind": "file"})
                _record(f"moved {sp} -> {tp}")

            for dr in meta.get("dir_renames") or []:
                sp = Path(dr["source"])
                tp = Path(dr["target"])
                root = _cleanup_root_for_path(sp, allowed_roots)
                tp_root = _cleanup_root_for_path(tp, allowed_roots)
                if root is None or tp_root is None:
                    return _fail(f"Folder rename outside allowed roots: {sp} -> {tp}", "folder_cleanup_path_out_of_root")
                if _path_has_symlink_under(sp, root) or _path_has_symlink_under(tp.parent, tp_root):
                    return _fail(f"Symlink detected on folder rename path: {sp} -> {tp}", "folder_cleanup_symlink_rejected")
                if not sp.exists() or not sp.is_dir():
                    return _fail(f"Rename source missing: {sp}", "folder_cleanup_toctou_mismatch")
                if dr.get("stat") and not _cleanup_dir_stat_matches(sp, dr["stat"], root):
                    return _fail(f"Rename source changed since plan: {sp}", "folder_cleanup_toctou_mismatch")
                if not tp.parent.exists() or not tp.parent.is_dir():
                    return _fail(f"Target parent directory missing: {tp.parent}", "folder_cleanup_target_parent_missing")
                if tp.exists() or tp.is_symlink():
                    return _fail(f"Rename target already exists: {tp}", "folder_cleanup_target_exists")
                err = _step("rename_dir", source=str(sp), target=str(tp))
                if err and err.startswith(FOLDER_OP_UNCONFIRMED):
                    moved_records.append({"source": str(sp), "target": str(tp), "kind": "dir", "unconfirmed": True})
                    _record(f"folder rename {sp} -> {tp} not confirmed; recorded for rollback")
                if err:
                    return _fail(f"Folder rename failed {sp} -> {tp}: {err}", "folder_cleanup_rename_failed", err)
                moved_records.append({"source": str(sp), "target": str(tp), "kind": "dir"})
                _record(f"renamed folder {sp} -> {tp}")

            for dr in meta.get("dir_removals") or []:
                spec = dr if isinstance(dr, dict) else {"path": dr}
                dp = Path(spec["path"])
                root = _cleanup_root_for_path(dp, allowed_roots)
                if root is None:
                    return _fail(f"Directory outside allowed roots: {dp}", "folder_cleanup_path_out_of_root")
                if _path_has_symlink_under(dp, root):
                    return _fail(f"Symlink detected on directory: {dp}", "folder_cleanup_symlink_rejected")
                if not dp.exists():
                    continue
                if not dp.is_dir():
                    return _fail(f"Removal target is not a directory: {dp}", "folder_cleanup_not_directory")
                try:
                    unexpected = [child.name for child in dp.iterdir()]
                except Exception as ex:
                    return _fail(f"Could not inspect directory before removal: {ex}", "folder_cleanup_scan_failed")
                if unexpected:
                    return _fail(f"Directory is not empty: {dp}", "folder_cleanup_not_empty")
                expected = spec.get("stat")
                if expected:
                    try:
                        current = _cleanup_dir_stat_record(dp, root)
                    except Exception:
                        return _fail(f"Directory disappeared before removal: {dp}", "folder_cleanup_toctou_mismatch")
                    if current.get("dev") != expected.get("dev") or current.get("ino") != expected.get("ino"):
                        return _fail(f"Directory identity changed since plan: {dp}", "folder_cleanup_toctou_mismatch")
                err = _step("remove_empty_dir", path=str(dp))
                if err and err.startswith(FOLDER_OP_UNCONFIRMED):
                    removed_dirs.append(str(dp))
                    _record(f"removal of {dp} not confirmed; recorded for rollback")
                if err:
                    return _fail(f"Directory removal failed {dp}: {err}", "folder_cleanup_remove_failed", err)
                removed_dirs.append(str(dp))
                _record(f"removed empty folder {dp}")

            mutated = bool(moved_records or removed_dirs)
            store.update(operation_id, status="Completed", metadata={
                "filesystem_mutated": mutated,
                "moved_records": moved_records,
                "removed_dirs": removed_dirs,
                "completed_at": _now(),
            })

            return {
                "ok": True,
                "operation_id": operation_id,
                "status": "Completed",
                "mutated": mutated,
                "moved_records": moved_records,
                "removed_dirs": removed_dirs,
                "changed_count": len(moved_records) + len(removed_dirs),
            }


#: Statuses a folder cleanup rollback starts from; each also needs the apply
#: record (``engine_result``). Running is a caller's own claim (#224 CAS).
_FOLDER_ROLLBACK_FROM = frozenset({"Completed", "Failed", "Running"})


def rollback_folder_cleanup(
    store: TransactionStore,
    operation_id: str,
    *,
    music_allowed_roots: Optional[List[str]] = None,
    adapter: Any = None,
) -> Dict[str, Any]:
    """Roll back an applied folder_cleanup_v1 transaction through Beets:
    re-create removed folders, then move every recorded file/folder back, in
    reverse order. A step that cannot be proven restored is counted failed."""
    if not _TRANSACTION_ID_RE.match(operation_id):
        return {"ok": False, "error": "Invalid transaction ID format", "code": "folder_cleanup_invalid_id"}

    with _get_apply_lock(operation_id):
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": f"Transaction {operation_id} not found", "code": "folder_cleanup_not_found"}

        meta = tx.get("metadata") or {}
        if meta.get("mutation_family") != "folder_cleanup_v1":
            return {"ok": False, "error": "Transaction is not a folder_cleanup_v1 operation", "code": "folder_cleanup_family_mismatch"}

        status = tx.get("status")
        if status == "Rolled Back":
            return {"ok": False, "error": "Transaction is already rolled back.",
                    "code": "folder_cleanup_already_rolled_back", "status": status}
        refused = {"ok": False, "code": "rollback_not_eligible", "operation_id": operation_id, "mutated": False}
        if not meta.get("engine_result") or status not in _FOLDER_ROLLBACK_FROM:
            return {**refused, "status": status, "error": f"Only an applied transaction can be rolled back (status is {status})."}
        if status != "Running" and store.transition(operation_id, str(status), "Running") is None:
            now = store.get(operation_id).get("status")
            return {**refused, "status": now, "error": f"Only an applied transaction can be rolled back (status is {now})."}

        resource_keys = meta.get("resource_keys") or []
        allowed_roots = _cleanup_normalize_roots(music_allowed_roots or meta.get("allowed_roots"), [_cfg_music_root()])
        ad = _folder_adapter(adapter)
        attempt = f"{operation_id}:rollback:{int(_now() * 1000)}"
        steps = [0]
        problems: List[str] = []

        def _step(op: str, **paths: str) -> Optional[str]:
            steps[0] += 1
            return _folder_step(ad, f"{attempt}:{steps[0]}", op, **paths)

        def _usable(p: Path) -> bool:
            root = _cleanup_root_for_path(p, allowed_roots)
            return root is not None and not _path_has_symlink_under(p.parent, root)

        with _lock_resources(resource_keys):
            dirs_restored = dirs_failed = 0
            for dr in reversed(meta.get("removed_dirs") or []):
                dp = Path(dr)
                if not _usable(dp):
                    dirs_failed += 1
                    problems.append(f"Not restored (outside roots or symlink): {dp}")
                    continue
                err = None if dp.is_dir() else _step("create_dir", path=str(dp))
                if err:
                    dirs_failed += 1
                    problems.append(f"Could not re-create {dp}: {err}")
                else:
                    dirs_restored += 1

            files_restored = files_failed = 0
            for mr in reversed(meta.get("moved_records") or []):
                sp = Path(mr["source"])
                tp = Path(mr["target"])
                if sp.exists() and not tp.exists():
                    files_restored += 1  # already back (an earlier attempt)
                    continue
                if not (tp.exists() and not sp.exists() and _usable(sp) and _usable(tp)):
                    files_failed += 1
                    problems.append(f"Cannot move back {tp} -> {sp}: paths changed since apply")
                    continue
                err = None if sp.parent.is_dir() else _step("create_dir", path=str(sp.parent))
                kind = mr.get("kind") or ("dir" if tp.is_dir() else "file")
                err = err or _step("rename_dir" if kind == "dir" else "move_file", source=str(tp), target=str(sp))
                if err:
                    files_failed += 1
                    problems.append(f"Could not move back {tp} -> {sp}: {err}")
                else:
                    files_restored += 1

            ok = files_failed == 0 and dirs_failed == 0
            final_status = "Rolled Back" if ok else ("Partially Rolled Back" if files_restored or dirs_restored else "Failed")
            for problem in problems:
                store.append_log(operation_id, f"Rollback: {problem}")
            store.append_log(operation_id, f"Rollback {final_status}: {files_restored + dirs_restored} restored, "
                                           f"{files_failed + dirs_failed} failed.")
            store.update(operation_id, status=final_status,
                         rollback={"available": False,
                                   "reason": "" if ok else "Folder cleanup rollback incomplete; manual recovery required."},
                         metadata={
                             "rollback_available": False,
                             "files_restored_count": files_restored,
                             "files_failed_count": files_failed,
                             "dirs_restored_count": dirs_restored,
                             "dirs_failed_count": dirs_failed,
                             "rolled_back_at": _now(),
                         })

            return {"ok": ok, "operation_id": operation_id, "status": final_status,
                    "mutated": bool(files_restored or dirs_restored),
                    "files_restored": files_restored, "dirs_restored": dirs_restored,
                    "files_failed": files_failed, "dirs_failed": dirs_failed}
