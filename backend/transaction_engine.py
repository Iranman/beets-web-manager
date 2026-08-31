"""Durable transaction records for library-changing jobs.

The transaction store is intentionally small and file-backed so it can sit
beside the existing JobStore without changing the job architecture.
"""
from __future__ import annotations

import base64
import csv
import datetime
import errno
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
import urllib.parse
import unicodedata

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


def _safe_rename(src: Path, dest: Path) -> None:
    """Atomically move src to dest as the literal target, never descending
    into dest if it happens to already exist as a directory.

    shutil.move() silently moves src *inside* dest (using src's own
    basename) when dest already exists as a directory, rather than
    treating dest as the literal target -- the exact Wave 15 regression
    this helper exists to make structurally impossible for every mutation
    family, instead of re-deriving the same os.rename()+EXDEV-fallback
    pattern inline in each one. Callers are still responsible for refusing
    up front if something already exists at the exact destination leaf
    (this only prevents the directory-descend ambiguity, not overwrites).
    """
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


class TransactionStore:
    def __init__(self, root: Optional[str] = None):
        base = root or os.environ.get("BEETS_TRANSACTION_DIR") or "/config/transactions"
        self.root = Path(base)
        self._lock = threading.RLock()

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
    album_id = int(payload.get("album_id") or 0)
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
    music_root_cand = music_root or os.environ.get("MUSIC_ROOT") or os.environ.get("BEETS_MUSIC_DIR") or "/music"
    music_root_path = Path(music_root_cand).resolve(strict=False)

    if (resolved_target == music_root_path or music_root_path in resolved_target.parents) and not confirmed_wrong_library_folder and album_id <= 0:
        return {"ok": False, "error": f"Review folder path {resolved_target} is inside music library."}

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


def create_album_cleanup_plan(
    store: TransactionStore,
    album_id: int,
    db_path: Optional[str] = None,
    allowed_roots: Optional[List[str]] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create transaction plan for deleting an album from Beets DB and disk (SEC-002 Wave 15)."""
    if album_id <= 0:
        return {"ok": False, "error": "Invalid album_id"}

    lib_db = db_path or os.environ.get("BEETS_DB_PATH", os.path.expanduser("~/.config/beets/musiclibrary.blb"))
    if not os.path.exists(lib_db):
        return {"ok": False, "error": f"Beets database file not found at {lib_db}"}

    try:
        con = sqlite3.connect(lib_db, timeout=10)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT id, path FROM items WHERE album_id = ?", (album_id,))
        rows = cur.fetchall()
        cur.execute("SELECT id, artpath FROM albums WHERE id = ?", (album_id,))
        album_row = cur.fetchone()
        con.close()
    except Exception as ex:
        return {"ok": False, "error": f"Database query failed: {ex}"}

    if not rows and not album_row:
        return {"ok": False, "error": f"Album {album_id} not found in database."}

    # SEC-002 Wave 15 final review: capture the exact item ids planned for
    # deletion, not just their paths -- Apply must delete precisely this
    # set of DB rows, never a broad "WHERE album_id = ?" that could also
    # sweep up items added to this album_id between Plan and Apply whose
    # files were never planned, precondition-checked, or deleted.
    item_ids: List[int] = []
    item_paths: List[Path] = []
    expected_states: Dict[str, Any] = {}
    sources: List[str] = []

    for r in rows:
        raw_p = r["path"]
        if isinstance(raw_p, bytes):
            try:
                raw_p = raw_p.decode("utf-8", "surrogateescape")
            except Exception:
                raw_p = raw_p.decode("utf-8", "ignore")
        p = Path(raw_p)
        item_paths.append(p)
        item_ids.append(int(r["id"]))

    if not item_paths:
        return {"ok": False, "error": f"Album {album_id} has no track items in database."}

    album_dir = item_paths[0].parent.resolve(strict=False)
    music_root = Path(os.environ.get("BEETS_MUSIC_DIR", os.environ.get("MUSIC_ROOT", "/music"))).resolve(strict=False)

    roots = [music_root] + [Path(r).resolve(strict=False) for r in (allowed_roots or [])]
    is_under_root = any(album_dir == r or r in album_dir.parents for r in roots)
    if not is_under_root:
        return {"ok": False, "error": f"Album path {album_dir} is outside authorized music roots."}

    if album_dir == music_root:
        return {"ok": False, "error": f"Refusing to delete music root directory {music_root} itself."}

    steps: List[Dict[str, Any]] = []
    step_idx = 1

    for p in item_paths:
        try:
            resolved_p = p.resolve(strict=False)
            if resolved_p.exists() and resolved_p.is_file() and not p.is_symlink() and not resolved_p.is_symlink():
                st = os.lstat(str(resolved_p))
                expected_states[str(resolved_p)] = {
                    "size": st.st_size,
                    "mtime_ns": st.st_mtime_ns,
                    "mtime": st.st_mtime,
                    "dev": st.st_dev,
                    "ino": st.st_ino,
                    "is_file": True,
                }
                sources.append(str(resolved_p))
                steps.append({
                    "step_id": f"step_{step_idx}",
                    "type": "delete_file",
                    "source": str(resolved_p),
                    "status": "pending",
                    "reversibility": "IRREVERSIBLE",
                })
                step_idx += 1
        except Exception:
            pass

    steps.append({
        "step_id": f"step_{step_idx}",
        "type": "delete_db_record",
        "album_id": album_id,
        "item_ids": item_ids,
        "db_path": str(lib_db),
        "status": "pending",
        "reversibility": "IRREVERSIBLE",
    })
    step_idx += 1

    steps.append({
        "step_id": f"step_{step_idx}",
        "type": "remove_dir",
        "target": str(album_dir),
        "status": "pending",
        "reversibility": "IRREVERSIBLE",
    })

    tx_payload = payload or {}
    tx_payload["album_id"] = album_id
    tx_payload["db_path"] = str(lib_db)

    tx = store.create(
        operation_type="Library Cleanup",
        initiating_user="operator",
        status="Preview",
        summary=f"Album cleanup plan for album_id={album_id} ({album_dir.name})",
        metadata={
            "mutation_family": "album_cleanup_v1",
            "album_id": album_id,
            "item_ids": item_ids,
            "target_path": str(album_dir),
            "allowed_roots": [str(r) for r in roots],
            "source_paths": sources,
            "expected_states": expected_states,
            "reversibility": "IRREVERSIBLE",
            "rollback_available": False,
            "steps": steps,
            "payload": tx_payload,
        },
    )
    op_id = tx["id"]
    return {
        "ok": True,
        "operation_id": op_id,
        "status": "Preview",
        "album_id": album_id,
        "target_path": str(album_dir),
        "file_count": len(sources),
    }


def execute_album_cleanup_apply(
    store: TransactionStore,
    operation_id: str,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute album cleanup plan application with durable steps and DB consistency (SEC-002 Wave 15).

    Serializes concurrent Apply calls for the SAME operation_id -- see the
    identical rationale on execute_import_review_cleanup_apply.
    """
    with _get_apply_lock(operation_id):
        return _execute_album_cleanup_apply_locked(store, operation_id, db_path)


def _execute_album_cleanup_apply_locked(
    store: TransactionStore,
    operation_id: str,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        tx = store.get(operation_id)
    except KeyError:
        tx = None
    if not tx:
        return {"ok": False, "error": f"Transaction {operation_id} not found.", "mutated": False}

    meta = tx.get("metadata") or {}
    if meta.get("mutation_family") != "album_cleanup_v1":
        return {"ok": False, "error": f"Transaction {operation_id} is not an album_cleanup_v1 operation.", "mutated": False}

    if tx.get("status") == "Completed":
        return {
            "ok": True,
            "status": "Completed",
            "operation_id": operation_id,
            "deleted": meta.get("deleted", []),
            "log": tx.get("logs", []),
        }

    is_valid, err = store.revalidate_preconditions(operation_id)
    if not is_valid:
        # SEC-002 Wave 16 final review: this check runs before the step
        # loop below has touched anything, so "mutated" is always False
        # here -- callers (Web Manager routes / UI) may truthfully tell
        # the user nothing changed. Contrast with the mid-loop failures
        # below, which set "mutated": bool(deleted) since earlier steps in
        # THIS apply call may have already deleted files irreversibly.
        return {"ok": False, "error": err or "Precondition revalidation failed.", "mutated": False}

    album_id = meta.get("album_id")
    steps = meta.get("steps") or []
    expected_states = meta.get("expected_states") or {}
    log: List[str] = []
    deleted: List[str] = []

    lib_db = db_path or meta.get("payload", {}).get("db_path") or os.environ.get("BEETS_DB_PATH", os.path.expanduser("~/.config/beets/musiclibrary.blb"))

    for step in steps:
        st_status = step.get("status")
        if st_status in {"completed", "irreversible_completed"}:
            if step.get("type") == "delete_file" and step.get("source"):
                deleted.append(step.get("source"))
            continue

        step["status"] = "running"
        try:
            step_type = step.get("type")

            if step_type == "delete_file":
                src_str = step.get("source")
                if src_str:
                    src = Path(src_str)
                    if src.exists():
                        if src.is_symlink() or os.path.islink(str(src)):
                            step["status"] = "failed"
                            store.update(operation_id, status="Failed", logs=log + [f"Symlink detected at {src}"])
                            return {"ok": False, "error": f"Symlink detected at {src}", "mutated": bool(deleted)}
                        # Immediately-before-mutation precondition re-check
                        # (same device/inode/size/mtime_ns comparison as the
                        # import-review cleanup path), not just the one-time
                        # revalidate_preconditions() call at the top of this
                        # function.
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
                                return {"ok": False, "error": f"File replacement detected at {src} ({mismatch})", "mutated": bool(deleted)}
                        except OSError as ex:
                            step["status"] = "failed"
                            store.update(operation_id, status="Failed", logs=log + [f"Could not stat {src}: {ex}"])
                            return {"ok": False, "error": f"Could not stat {src}: {ex}", "mutated": bool(deleted)}
                        try:
                            src.unlink()
                            deleted.append(src_str)
                            log.append(f"Deleted track file {src_str}")
                            step["status"] = "irreversible_completed"
                        except Exception as ex:
                            step["status"] = "failed"
                            store.update(operation_id, status="Failed", logs=log + [f"Failed deleting {src_str}: {ex}"])
                            return {"ok": False, "error": f"Failed deleting {src_str}: {ex}", "mutated": bool(deleted)}
                    else:
                        step["status"] = "completed"

            elif step_type == "delete_db_record":
                planned_item_ids = sorted(int(i) for i in (step.get("item_ids") or []))
                if not album_id or not os.path.exists(lib_db):
                    step["status"] = "failed"
                    store.update(operation_id, status="Failed", logs=log + [f"Beets database not found at {lib_db}"])
                    return {"ok": False, "error": f"Beets database not found at {lib_db}", "mutated": bool(deleted)}
                try:
                    # NOTE: `with sqlite3.connect(...) as con:` only wraps the
                    # transaction (commit-on-success / rollback-on-exception) --
                    # it does NOT close the connection on exit. Leaving the
                    # connection open here would hold a live handle/lock on the
                    # Beets DB file after every album-cleanup apply, which can
                    # block subsequent operations (including the engine's own
                    # retries) from opening the same database. Open and close
                    # explicitly instead.
                    con = sqlite3.connect(lib_db, timeout=10)
                    try:
                        cur = con.cursor()
                        # SEC-002 Wave 15 final review: re-verify the album's
                        # CURRENT item membership exactly matches what was
                        # planned (and whose files were just deleted above)
                        # before touching the DB at all. A broad
                        # "WHERE album_id = ?" delete would also remove rows
                        # for any item added to this album_id between Plan and
                        # Apply -- rows whose files were never planned,
                        # precondition-checked, or deleted -- leaving the DB
                        # claiming those files are gone when they are not.
                        cur.execute("SELECT id FROM items WHERE album_id = ?", (album_id,))
                        current_item_ids = sorted(int(row[0]) for row in cur.fetchall())
                        if current_item_ids != planned_item_ids:
                            step["status"] = "failed"
                            msg = (
                                f"Album {album_id} membership changed since planning "
                                f"(planned items {planned_item_ids}, now {current_item_ids}); "
                                "refusing to delete DB rows."
                            )
                            store.update(operation_id, status="Failed", logs=log + [msg])
                            return {"ok": False, "error": msg, "mutated": bool(deleted)}
                        if planned_item_ids:
                            placeholders = ",".join("?" for _ in planned_item_ids)
                            cur.execute(f"DELETE FROM items WHERE id IN ({placeholders})", planned_item_ids)
                        cur.execute("DELETE FROM albums WHERE id = ?", (album_id,))
                        con.commit()
                    finally:
                        con.close()
                    log.append(f"Deleted album {album_id} and {len(planned_item_ids)} item(s) from Beets database {lib_db}")
                    step["status"] = "irreversible_completed"
                except Exception as ex:
                    step["status"] = "failed"
                    store.update(operation_id, status="Failed", logs=log + [f"DB deletion for album {album_id} failed: {ex}"])
                    return {"ok": False, "error": f"DB deletion for album {album_id} failed: {ex}", "mutated": bool(deleted)}

            elif step_type == "remove_dir":
                target_str = step.get("target")
                if target_str:
                    target_path = Path(target_str)
                    if target_path.exists() and target_path.is_dir():
                        try:
                            if not any(target_path.iterdir()):
                                target_path.rmdir()
                                log.append(f"Deleted empty album directory {target_path}")
                        except Exception:
                            pass
                    step["status"] = "completed"
        finally:
            # Persist per-step progress durably for the same crash-recovery
            # reason as execute_import_review_cleanup_apply: a crash after
            # this step legitimately completed but before the function's
            # final store.update() call must not leave it recorded as
            # "pending" on disk.
            store.update(operation_id, metadata={"steps": steps})

    store.update(
        operation_id,
        status="Completed",
        applied_at=time.time(),
        metadata={**meta, "deleted": deleted, "steps": steps, "rollback_available": False},
        logs=log,
    )

    return {
        "ok": True,
        "status": "Completed",
        "operation_id": operation_id,
        "deleted": deleted,
        "log": log,
    }


def _decode_row_bytes(row: Dict[str, Any]) -> Dict[str, Any]:
    """Beets stores several sqlite columns (notably `path`) as raw bytes.
    Decode every bytes value for safe JSON storage / display, using the
    same surrogateescape convention as the rest of this codebase's raw-path
    handling, so a rollback re-insert can encode it back byte-for-byte."""
    out: Dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, bytes):
            try:
                out[k] = v.decode("utf-8", "surrogateescape")
            except Exception:
                out[k] = v.decode("utf-8", "ignore")
        else:
            out[k] = v
    return out


def _fetch_authoritative_item_row(db_path: str, item_id: int) -> Optional[Dict[str, Any]]:
    """Read the full, current Beets `items` row for item_id directly from
    the engine's own SQLite access -- the only authority for what path,
    recording id, etc. this item actually has right now. Returns None if
    not found. Raises on a genuine DB access failure (caller decides how
    to report that)."""
    con = sqlite3.connect(db_path, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()
        cur.execute("SELECT * FROM items WHERE id = ?", (item_id,))
        row = cur.fetchone()
    finally:
        con.close()
    if row is None:
        return None
    return _decode_row_bytes(dict(row))


def create_track_replacement_plan(
    store: TransactionStore,
    payload: Dict[str, Any],
    *,
    original_allowed_roots: List[str],
    candidate_allowed_roots: List[str],
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a track replacement plan.

    SEC-002 Wave 17 final review -- this is a from-scratch rewrite of the
    original implementation, which had several confirmed defects: a
    client-controlled quarantine root, a transaction-id/quarantine-path
    mismatch bug, no verification that original_item_id actually owns the
    supplied original_path, a matching-authority check that only fired
    when a matching_contract happened to be supplied (never REQUIRED one),
    and a single undifferentiated allowed_roots list letting an "original"
    be accepted from a downloads root and a "candidate" from the music
    root. All fixed here:

    - original_item_id is the ONLY authority for the original file's
      location: the engine reads the item's current path/mb_trackid
      directly from the Beets DB. A client-supplied original_path, if
      given, is used only as expected-state evidence and must exactly
      match the engine's own record or the plan is refused.
    - The candidate must live under a role-specific
      candidate/staging/acquisition root; the original must be a
      Beets-owned item under the music-library root. A candidate can
      never be accepted from the music root, nor an original from a
      downloads root, just because both happen to be in "some" trusted
      root.
    - matching_contract is REQUIRED, not optional, and is cross-checked
      against the item's actual current Recording ID from the DB (not
      merely trusted at face value). A contract whose only identity_source
      is "ai"/"ai_suggestion" is rejected outright -- AI is advisory only.
    - The quarantine root always comes from server configuration
      (REPLACEMENT_QUARANTINE_DIR); any client-supplied quarantine_root in
      the payload is ignored entirely.
    - The quarantine path is computed AFTER store.create() returns the
      real, durable transaction id, so it can never diverge from the
      operation_id the caller actually receives.
    - The full original item row is captured (schema-agnostic, via
      SELECT *) so rollback can genuinely re-insert it rather than
      guessing at a handful of columns.
    """
    original_item_id = int(payload.get("original_item_id") or payload.get("item_id") or 0)
    if original_item_id <= 0:
        return {"ok": False, "error": "original_item_id is required.", "code": "item_id_required"}

    replacement_path_raw = str(payload.get("replacement_path") or payload.get("candidate_path") or payload.get("replacement") or "").strip()
    if not replacement_path_raw:
        return {"ok": False, "error": "Replacement path is required.", "code": "replacement_path_required"}

    if not db_path or not os.path.exists(db_path):
        return {"ok": False, "error": f"Beets database not found at {db_path}.", "code": "db_not_found"}

    # URL decoding budget & null-byte check (client-supplied replacement path only;
    # the original path is engine-derived below, never taken from the client).
    cur_decode = replacement_path_raw
    for _ in range(3):
        dec = urllib.parse.unquote(cur_decode)
        if dec == cur_decode:
            break
        cur_decode = dec
    if "\x00" in cur_decode or "%00" in replacement_path_raw or "\x00" in replacement_path_raw:
        return {"ok": False, "error": "Path contains unsafe encoded characters.", "code": "invalid_path"}

    # --- Candidate validation (role: candidate/staging root only) ---
    if os.path.islink(replacement_path_raw):
        return {"ok": False, "error": "Symlinks are not permitted for track replacement.", "code": "symlink_rejected"}
    repl_p = Path(replacement_path_raw)
    if repl_p.is_symlink():
        return {"ok": False, "error": "Symlinks are not permitted for track replacement.", "code": "symlink_rejected"}

    # Normalize (collapse ".."/".") WITHOUT resolving symlinks yet -- the
    # component-by-component symlink walk below only means anything if it
    # runs on a path that could still *contain* a symlink component;
    # Path.resolve() would already have transparently followed and erased
    # every symlink in the path before the walk ever saw it, making the
    # walk a structural no-op. Root-containment is checked on this
    # normalized-but-unresolved form first; resolve() is only used
    # afterward, for the file's actual on-disk identity.
    abs_repl = Path(os.path.normpath(str(repl_p if repl_p.is_absolute() else repl_p.absolute())))
    candidate_roots_resolved = [Path(r).resolve(strict=False) for r in (candidate_allowed_roots or [])]
    candidate_root = next((r for r in candidate_roots_resolved if abs_repl == r or r in abs_repl.parents), None)
    if candidate_root is None:
        return {"ok": False, "error": f"Replacement path {abs_repl} is outside approved candidate/staging roots.", "code": "candidate_outside_roots"}
    if _path_has_symlink_under(abs_repl, candidate_root):
        return {"ok": False, "error": f"Symlink component detected in replacement path {abs_repl}.", "code": "symlink_rejected"}

    try:
        resolved_repl = abs_repl.resolve(strict=False)
    except Exception:
        return {"ok": False, "error": "Invalid replacement path.", "code": "invalid_path"}
    if resolved_repl.is_symlink():
        return {"ok": False, "error": "Symlinks are not permitted for track replacement.", "code": "symlink_rejected"}
    # Defense in depth: confirm resolution didn't itself land outside the
    # matched root (covers any resolve()-only edge case the lexical walk
    # above didn't anticipate).
    if not (resolved_repl == candidate_root or candidate_root in resolved_repl.parents):
        return {"ok": False, "error": f"Replacement path {resolved_repl} is outside approved candidate/staging roots.", "code": "candidate_outside_roots"}

    if not resolved_repl.exists() or not resolved_repl.is_file():
        return {"ok": False, "error": f"Replacement file {resolved_repl} does not exist or is not a regular file.", "code": "candidate_missing"}
    st_repl = resolved_repl.stat()
    if st_repl.st_size <= 0:
        return {"ok": False, "error": f"Replacement file {resolved_repl} is empty.", "code": "candidate_empty"}

    # --- Original validation (role: music-library root only, bound by item_id) ---
    try:
        original_row = _fetch_authoritative_item_row(db_path, original_item_id)
    except Exception as exc:
        return {"ok": False, "error": f"Failed to read original item record: {exc}", "code": "db_read_failed"}
    if original_row is None:
        return {"ok": False, "error": f"Beets item {original_item_id} not found.", "code": "item_not_found"}

    original_path_raw = str(original_row.get("path") or "")
    if not original_path_raw:
        return {"ok": False, "error": f"Beets item {original_item_id} has no recorded path.", "code": "item_path_missing"}

    # Client-supplied original_path (if given at all) is expected-state
    # evidence ONLY -- it must exactly match what the engine's own record
    # says, never override it. This is what stops a client from pairing
    # an item ID from Track A with a path belonging to Track B.
    client_original_path = str(payload.get("original_path") or payload.get("original") or "").strip()
    if client_original_path:
        try:
            client_resolved = Path(client_original_path).resolve(strict=False)
            record_resolved = Path(original_path_raw).resolve(strict=False)
        except Exception:
            return {"ok": False, "error": "Invalid original_path.", "code": "invalid_path"}
        if client_resolved != record_resolved:
            return {
                "ok": False,
                "error": "Supplied original_path does not match the path currently recorded for this item.",
                "code": "item_path_mismatch",
            }

    if os.path.islink(original_path_raw):
        return {"ok": False, "error": "Symlinks are not permitted for track replacement.", "code": "symlink_rejected"}
    orig_p = Path(original_path_raw)
    if orig_p.is_symlink():
        return {"ok": False, "error": "Symlinks are not permitted for track replacement.", "code": "symlink_rejected"}

    # See the identical comment on the candidate path above: containment
    # and the symlink-component walk both run on the normalized-but-not-
    # symlink-resolved form first, since resolve() would erase any
    # symlink component before the walk could ever see it.
    abs_orig = Path(os.path.normpath(str(orig_p if orig_p.is_absolute() else orig_p.absolute())))
    original_roots_resolved = [Path(r).resolve(strict=False) for r in (original_allowed_roots or [])]
    original_root = next((r for r in original_roots_resolved if abs_orig == r or r in abs_orig.parents), None)
    if original_root is None:
        return {"ok": False, "error": f"Original path {abs_orig} is outside the music library root.", "code": "original_outside_roots"}
    if _path_has_symlink_under(abs_orig, original_root):
        return {"ok": False, "error": f"Symlink component detected in original path {abs_orig}.", "code": "symlink_rejected"}

    try:
        resolved_orig = abs_orig.resolve(strict=False)
    except Exception:
        return {"ok": False, "error": "Invalid original path.", "code": "invalid_path"}
    if resolved_orig.is_symlink():
        return {"ok": False, "error": "Symlinks are not permitted for track replacement.", "code": "symlink_rejected"}
    if not (resolved_orig == original_root or original_root in resolved_orig.parents):
        return {"ok": False, "error": f"Original path {resolved_orig} is outside the music library root.", "code": "original_outside_roots"}

    if not resolved_orig.exists() or not resolved_orig.is_file():
        return {"ok": False, "error": f"Original file {resolved_orig} does not exist or is not a regular file.", "code": "original_missing"}
    st_orig = resolved_orig.stat()
    if st_orig.st_size <= 0:
        return {"ok": False, "error": f"Original file {resolved_orig} is empty.", "code": "original_empty"}

    if resolved_orig == resolved_repl or (st_orig.st_ino == st_repl.st_ino and st_orig.st_dev == st_repl.st_dev):
        return {"ok": False, "error": "Original and replacement paths point to the exact same file.", "code": "same_file"}

    # --- Matching authority (REQUIRED, engine-verified against the DB) ---
    matching_contract = payload.get("matching_contract") if isinstance(payload.get("matching_contract"), dict) else {}
    identity_source = str(matching_contract.get("identity_source") or "").strip().lower()
    replacement_recording_id = str(
        matching_contract.get("replacement_recording_id") or matching_contract.get("expected_recording_id") or ""
    ).strip().lower()
    original_recording_id_claim = str(matching_contract.get("original_recording_id") or "").strip().lower()
    orig_rgid_claim = str(matching_contract.get("original_release_group_id") or "").strip().lower()
    repl_rgid_claim = str(matching_contract.get("replacement_release_group_id") or "").strip().lower()

    if not identity_source or identity_source in ("ai", "ai_suggestion", "ai_only"):
        return {
            "ok": False,
            "error": "Replacement requires non-AI identity evidence (e.g. AcoustID fingerprint match); AI suggestions are advisory only and cannot authorize a replacement by themselves.",
            "code": "ai_only_evidence_rejected",
        }
    if not replacement_recording_id:
        return {
            "ok": False,
            "error": "Replacement candidate is missing verified Recording ID evidence.",
            "code": "recording_id_required",
        }

    actual_original_recording_id = str(original_row.get("mb_trackid") or "").strip().lower()
    if actual_original_recording_id and original_recording_id_claim and actual_original_recording_id != original_recording_id_claim:
        return {
            "ok": False,
            "error": "Matching evidence does not match the original item's current Recording ID -- the evidence appears to be stale or built against a different item.",
            "code": "original_recording_id_mismatch",
        }
    # A higher-quality file is not sufficient evidence: if the original
    # already has a known Recording ID, the candidate's verified Recording
    # ID must match it exactly. This is the actual guard against "a FLAC
    # of the wrong song replacing an MP3 of the right one."
    if actual_original_recording_id and replacement_recording_id != actual_original_recording_id:
        return {
            "ok": False,
            "error": "Replacement candidate's verified Recording ID does not match the original track's Recording ID.",
            "code": "recording_id_mismatch",
        }
    if orig_rgid_claim and repl_rgid_claim and orig_rgid_claim != repl_rgid_claim:
        return {
            "ok": False,
            "error": "Candidate belongs to a different Release Group than the original track.",
            "code": "release_group_mismatch",
        }

    orig_stat_data = {
        "inode": st_orig.st_ino,
        "dev": st_orig.st_dev,
        "size": st_orig.st_size,
        "mtime_ns": getattr(st_orig, "st_mtime_ns", int(st_orig.st_mtime * 1e9)),
    }
    repl_stat_data = {
        "inode": st_repl.st_ino,
        "dev": st_repl.st_dev,
        "size": st_repl.st_size,
        "mtime_ns": getattr(st_repl, "st_mtime_ns", int(st_repl.st_mtime * 1e9)),
    }

    change_entry = {
        "id": f"item:{original_item_id}",
        "operation": "Track Replacement",
        "mutation_family": "track_replacement_v1",
        "original_item_id": original_item_id,
        "original_path": str(resolved_orig),
        "original_stat": orig_stat_data,
        "replacement_path": str(resolved_repl),
        "replacement_stat": repl_stat_data,
        "matching_contract": matching_contract,
    }

    # store.create() generates the durable transaction id itself -- do NOT
    # invent a separate id up front and use it to build the quarantine
    # path; that produced the exact "quarantine stored under txn_B while
    # the transaction is actually txn_A" bug this review found. Compute
    # the quarantine path from the REAL id, after create() returns it.
    tx_data = store.create(
        operation_type="Replace",
        initiating_user=str(payload.get("initiating_user") or "operator"),
        status="Preview",
        summary=f"Replace track {resolved_orig.name} with {resolved_repl.name}",
        reason=str(payload.get("reason") or "Audio quality replacement"),
        source="Track Replacement",
        rollback_available=True,
        changes=[change_entry],
        metadata={
            "mutation_family": "track_replacement_v1",
            "reversibility": "RECOVERABLE",
            "original_item_id": original_item_id,
            "original_path": str(resolved_orig),
            "original_stat": orig_stat_data,
            "original_item_row": original_row,
            "replacement_path": str(resolved_repl),
            "replacement_stat": repl_stat_data,
            "matching_contract": matching_contract,
            "item_ids": [original_item_id],
            "apply_steps": [
                {"step": "original_quarantined", "status": "pending"},
                {"step": "db_row_removed", "status": "pending"},
            ],
        },
    )
    op_id = tx_data["id"]

    # Server-derived quarantine root only -- a client-supplied
    # quarantine_root in the payload is never read at all.
    quarantine_root = os.environ.get("REPLACEMENT_QUARANTINE_DIR", "/config/track_replacement_quarantine")
    date_str = time.strftime("%Y%m%d")
    quarantine_target = str(Path(quarantine_root) / "library" / date_str / op_id / resolved_orig.name)

    updated_meta = dict(tx_data.get("metadata") or {})
    updated_meta["quarantine_target"] = quarantine_target
    tx_data = store.update(op_id, metadata=updated_meta)

    return {
        "ok": True,
        "operation_id": op_id,
        "plan": tx_data,
        "quarantine_target": quarantine_target,
        "reversibility": "RECOVERABLE",
    }


def execute_track_replacement_apply(
    store: TransactionStore,
    operation_id: str,
    *,
    db_path: Optional[str] = None,
    original_allowed_roots: Optional[List[str]] = None,
    candidate_allowed_roots: Optional[List[str]] = None,
    quarantine_allowed_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply a track replacement plan.

    Serializes on BOTH the operation id (repeated/concurrent Apply calls
    for the SAME transaction) and the target Beets item id (two DIFFERENT
    transactions racing to apply against the SAME item) -- see
    _get_apply_lock / _get_resource_lock. The item lock is acquired first
    (outer) and the operation lock second (inner): every caller only ever
    holds a lock unique to its own operation_id as the inner lock, so this
    ordering can never deadlock across concurrent calls.
    """
    try:
        tx_peek = store.get(operation_id)
    except KeyError:
        tx_peek = None
    if not tx_peek:
        return {"ok": False, "error": f"Transaction {operation_id} not found.", "code": "not_found", "mutated": False}

    meta_peek = tx_peek.get("metadata") or {}
    item_id_peek = int(meta_peek.get("original_item_id") or 0)
    resource_key = f"item:{item_id_peek}" if item_id_peek > 0 else f"path:{meta_peek.get('original_path', '')}"

    with _get_resource_lock(resource_key):
        with _get_apply_lock(operation_id):
            return _execute_track_replacement_apply_locked(
                store,
                operation_id,
                db_path=db_path,
                original_allowed_roots=original_allowed_roots,
                candidate_allowed_roots=candidate_allowed_roots,
                quarantine_allowed_root=quarantine_allowed_root,
            )


def _track_replacement_fail(
    store: TransactionStore, operation_id: str, log: str, error: str, *, code: str, mutated: bool,
) -> Dict[str, Any]:
    store.update(operation_id, status="Failed", logs=[log])
    return {"ok": False, "error": error, "code": code, "mutated": mutated, "retryable": not mutated}


def _execute_track_replacement_apply_locked(
    store: TransactionStore,
    operation_id: str,
    *,
    db_path: Optional[str] = None,
    original_allowed_roots: Optional[List[str]] = None,
    candidate_allowed_roots: Optional[List[str]] = None,
    quarantine_allowed_root: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        tx = store.get(operation_id)
    except KeyError:
        tx = None
    if not tx:
        return {"ok": False, "error": f"Transaction {operation_id} not found.", "code": "not_found", "mutated": False}

    meta = tx.get("metadata") or {}
    mutation_family = meta.get("mutation_family")
    if mutation_family != "track_replacement_v1":
        return {
            "ok": False,
            "error": f"Transaction {operation_id} is not a track_replacement_v1 operation.",
            "code": "wrong_mutation_family",
            "mutated": False,
        }

    if tx.get("status") == "Completed":
        return {
            "ok": True,
            "already_completed": True,
            "operation_id": operation_id,
            "status": "Completed",
            "quarantined_to": meta.get("quarantine_target"),
        }

    orig_path_str = meta.get("original_path") or ""
    orig_stat_expected = meta.get("original_stat") or {}
    repl_path_str = meta.get("replacement_path") or ""
    repl_stat_expected = meta.get("replacement_stat") or {}
    quarantine_target_str = meta.get("quarantine_target") or ""
    original_item_id = int(meta.get("original_item_id") or 0)
    original_item_row = meta.get("original_item_row") or {}
    apply_steps = meta.get("apply_steps") or [
        {"step": "original_quarantined", "status": "pending"},
        {"step": "db_row_removed", "status": "pending"},
    ]
    steps_by_name = {s["step"]: s for s in apply_steps}

    def _persist_step(name: str, status: str, note: str = "") -> None:
        steps_by_name[name]["status"] = status
        if note:
            steps_by_name[name]["note"] = note
        store.update(operation_id, metadata={**meta, "apply_steps": list(steps_by_name.values())})

    already_quarantined = steps_by_name.get("original_quarantined", {}).get("status") == "completed"
    already_db_removed = steps_by_name.get("db_row_removed", {}).get("status") == "completed"

    q_target_p = Path(quarantine_target_str)
    orig_p = Path(orig_path_str)

    if not quarantine_allowed_root:
        return {"ok": False, "error": "Quarantine root is not configured.", "code": "quarantine_root_missing", "mutated": already_quarantined}
    q_root_resolved = Path(quarantine_allowed_root).resolve(strict=False)

    if not already_quarantined:
        # --- Re-verify original TOCTOU + root containment + full symlink walk ---
        # Containment and the symlink-component walk both run BEFORE
        # resolve() -- resolve() transparently follows and erases symlinks,
        # which would make a walk over the already-resolved path a
        # structural no-op (there being nothing left to find).
        original_roots_resolved = [Path(r).resolve(strict=False) for r in (original_allowed_roots or [])]
        if not orig_p.exists() or orig_p.is_symlink() or os.path.islink(orig_path_str):
            return _track_replacement_fail(store, operation_id, f"Original file missing or symlink at {orig_path_str}",
                                            f"File replacement detected at {orig_path_str} (missing or symlink)",
                                            code="original_toctou_failed", mutated=False)
        original_root = next((r for r in original_roots_resolved if orig_p == r or r in orig_p.parents), None)
        if original_root is None:
            return _track_replacement_fail(store, operation_id, f"Original path {orig_p} no longer under an approved root",
                                            f"Original path {orig_p} is outside the music library root", code="original_outside_roots", mutated=False)
        if _path_has_symlink_under(orig_p, original_root):
            return _track_replacement_fail(store, operation_id, f"Symlink component detected under {orig_p}",
                                            f"Symlink component detected in original path {orig_p}", code="symlink_rejected", mutated=False)
        try:
            resolved_orig = orig_p.resolve(strict=False)
        except Exception:
            return _track_replacement_fail(store, operation_id, f"Failed to resolve {orig_path_str}",
                                            f"Invalid path {orig_path_str}", code="invalid_path", mutated=False)
        if resolved_orig.is_symlink() or not (resolved_orig == original_root or original_root in resolved_orig.parents):
            return _track_replacement_fail(store, operation_id, f"Symlink target detected at {orig_path_str}",
                                            f"Symlinks are not permitted at {orig_path_str}", code="symlink_rejected", mutated=False)

        st_orig_now = resolved_orig.stat()
        orig_mtime_now = getattr(st_orig_now, "st_mtime_ns", int(st_orig_now.st_mtime * 1e9))
        if orig_stat_expected:
            mismatch = None
            if st_orig_now.st_ino != orig_stat_expected.get("inode") or st_orig_now.st_dev != orig_stat_expected.get("dev"):
                mismatch = "inode/device changed"
            elif st_orig_now.st_size != orig_stat_expected.get("size"):
                mismatch = "size changed"
            elif orig_mtime_now != orig_stat_expected.get("mtime_ns"):
                mismatch = "mtime changed"
            if mismatch:
                return _track_replacement_fail(store, operation_id, f"Original file replacement detected at {orig_path_str} ({mismatch})",
                                                f"File replacement detected at {orig_path_str} ({mismatch})", code="original_toctou_failed", mutated=False)

        # --- Re-verify candidate TOCTOU (full signature -- inode, dev, size, mtime_ns) + root containment + symlink walk ---
        candidate_roots_resolved = [Path(r).resolve(strict=False) for r in (candidate_allowed_roots or [])]
        repl_p = Path(repl_path_str)
        if not repl_p.exists() or repl_p.is_symlink() or os.path.islink(repl_path_str):
            return _track_replacement_fail(store, operation_id, f"Replacement candidate missing or symlink at {repl_path_str}",
                                            f"File replacement detected at {repl_path_str} (missing or symlink)", code="candidate_toctou_failed", mutated=False)
        candidate_root = next((r for r in candidate_roots_resolved if repl_p == r or r in repl_p.parents), None)
        if candidate_root is None:
            return _track_replacement_fail(store, operation_id, f"Replacement path {repl_p} no longer under an approved root",
                                            f"Replacement path {repl_p} is outside approved candidate/staging roots", code="candidate_outside_roots", mutated=False)
        if _path_has_symlink_under(repl_p, candidate_root):
            return _track_replacement_fail(store, operation_id, f"Symlink component detected under {repl_p}",
                                            f"Symlink component detected in replacement path {repl_p}", code="symlink_rejected", mutated=False)
        try:
            resolved_repl = repl_p.resolve(strict=False)
        except Exception:
            return _track_replacement_fail(store, operation_id, f"Failed to resolve {repl_path_str}",
                                            f"Invalid path {repl_path_str}", code="invalid_path", mutated=False)
        if resolved_repl.is_symlink() or not (resolved_repl == candidate_root or candidate_root in resolved_repl.parents):
            return _track_replacement_fail(store, operation_id, f"Symlink target detected at {repl_path_str}",
                                            f"Symlinks are not permitted at {repl_path_str}", code="symlink_rejected", mutated=False)

        st_repl_now = resolved_repl.stat()
        repl_mtime_now = getattr(st_repl_now, "st_mtime_ns", int(st_repl_now.st_mtime * 1e9))
        if repl_stat_expected:
            mismatch = None
            if st_repl_now.st_ino != repl_stat_expected.get("inode") or st_repl_now.st_dev != repl_stat_expected.get("dev"):
                mismatch = "inode/device changed"
            elif st_repl_now.st_size != repl_stat_expected.get("size"):
                mismatch = "size changed"
            elif repl_mtime_now != repl_stat_expected.get("mtime_ns"):
                mismatch = "mtime changed"
            if mismatch:
                return _track_replacement_fail(store, operation_id, f"Replacement candidate replacement detected at {repl_path_str} ({mismatch})",
                                                f"File replacement detected at {repl_path_str} ({mismatch})", code="candidate_toctou_failed", mutated=False)

        # --- Quarantine the original (candidate is left exactly where it
        # is -- it is already in its own intended location; Wave 17's
        # original implementation moved it onto the original's path,
        # which does not match the pre-Wave-17 semantics of this
        # operation and was a real behavioral regression this review
        # caught and reverted). ---
        if q_target_p.exists() or q_target_p.is_symlink():
            return _track_replacement_fail(store, operation_id, f"Quarantine destination already exists at {q_target_p}",
                                            f"Unsafe quarantine destination at {q_target_p}", code="quarantine_destination_exists", mutated=False)
        q_parent = q_target_p.parent
        try:
            q_parent.mkdir(parents=True, exist_ok=True)
            if q_root_resolved not in q_parent.resolve(strict=False).parents and q_parent.resolve(strict=False) != q_root_resolved:
                return _track_replacement_fail(store, operation_id, f"Quarantine destination {q_target_p} escaped the quarantine root",
                                                "Unsafe quarantine destination", code="quarantine_destination_exists", mutated=False)
            if _path_has_symlink_under(q_parent, q_root_resolved):
                return _track_replacement_fail(store, operation_id, f"Symlink detected under quarantine destination {q_target_p}",
                                                "Unsafe quarantine destination", code="symlink_rejected", mutated=False)
            os.chmod(str(q_parent), 0o700)
        except Exception as exc:
            return _track_replacement_fail(store, operation_id, f"Failed to prepare quarantine directory: {exc}",
                                            f"Failed to prepare quarantine directory: {exc}", code="quarantine_prepare_failed", mutated=False)

        try:
            _safe_rename(resolved_orig, q_target_p)
        except Exception as exc:
            return _track_replacement_fail(store, operation_id, f"Quarantine move failed: {exc}",
                                            "Failed to quarantine original file.", code="quarantine_move_failed", mutated=False)

        _persist_step("original_quarantined", "completed")
    else:
        # Crash-recovery resume: the original was already quarantined by a
        # prior attempt at this same Apply call. Sanity-check the
        # quarantine target is actually there before proceeding, rather
        # than assuming.
        if not q_target_p.exists():
            return {
                "ok": False,
                "error": f"Transaction records the original as already quarantined, but nothing is present at {q_target_p}. Manual investigation required.",
                "code": "inconsistent_recovery_state",
                "mutated": True,
            }

    # --- Remove the original's DB row (the replacement is a separately,
    # already-tracked library item; this operation's job is to retire the
    # original's row, not to redirect it onto the replacement's file). ---
    if not already_db_removed:
        if not db_path or not os.path.exists(db_path):
            return {"ok": False, "error": f"Beets database not found at {db_path}.", "code": "db_not_found", "mutated": True}
        try:
            con = sqlite3.connect(db_path, timeout=10)
            try:
                cur = con.cursor()
                cur.execute("SELECT * FROM items WHERE id = ?", (original_item_id,))
                current_row = cur.fetchone()
                if current_row is None:
                    con.close()
                    return {
                        "ok": False,
                        "error": f"Beets item {original_item_id} no longer exists; refusing to report success without confirming DB state.",
                        "code": "item_vanished",
                        "mutated": True,
                    }
                cur.execute("DELETE FROM items WHERE id = ?", (original_item_id,))
                rowcount = cur.rowcount
                if rowcount != 1:
                    con.rollback()
                    con.close()
                    return {
                        "ok": False,
                        "error": f"Expected to delete exactly 1 row for item {original_item_id}, affected {rowcount}; rolled back the DB change.",
                        "code": "db_rowcount_mismatch",
                        "mutated": True,
                    }
                con.commit()
            finally:
                con.close()
        except Exception as exc:
            return {
                "ok": False,
                "error": f"Database update failed after the original file was already quarantined: {exc}",
                "code": "db_update_failed",
                "mutated": True,
            }
        _persist_step("db_row_removed", "completed")

    # --- Post-apply verification: confirm the DB row is genuinely gone
    # and the quarantined original is genuinely present, before ever
    # claiming Completed. ---
    try:
        con = sqlite3.connect(db_path, timeout=10)
        try:
            still_present = con.execute("SELECT COUNT(*) FROM items WHERE id = ?", (original_item_id,)).fetchone()[0]
        finally:
            con.close()
    except Exception as exc:
        return {"ok": False, "error": f"Post-apply verification failed: {exc}", "code": "verification_failed", "mutated": True}
    if still_present:
        return {"ok": False, "error": f"Post-apply verification failed: item {original_item_id} row still present.", "code": "verification_failed", "mutated": True}
    if not q_target_p.exists() or q_target_p.stat().st_size <= 0:
        return {"ok": False, "error": "Post-apply verification failed: quarantined original is missing or empty.", "code": "verification_failed", "mutated": True}

    store.update(
        operation_id,
        status="Completed",
        applied_at=time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        logs=["Original quarantined and its database row removed; replacement left in place."],
        metadata={**meta, "apply_steps": list(steps_by_name.values()), "mutated": True, "original_item_row": original_item_row},
    )

    return {
        "ok": True,
        "operation_id": operation_id,
        "status": "Completed",
        "quarantined_to": str(q_target_p),
        "replacement_path": repl_path_str,
    }


def rollback_track_replacement(
    store: TransactionStore,
    operation_id: str,
    *,
    db_path: Optional[str] = None,
    quarantine_allowed_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Rollback a completed track replacement transaction by restoring the
    original file from quarantine and re-inserting its captured DB row.

    SEC-002 Wave 17 final review: the original implementation destroyed
    the replacement's slot via unlink() BEFORE confirming the quarantined
    original could actually be restored -- if the restore then failed, the
    user could lose both the replacement and the original. This version
    never removes anything from the destination until the quarantined
    original has already been moved into a temporary rollback holding
    location successfully; only once that has genuinely succeeded does it
    touch the destination, and only the actual quarantined original (never
    the replacement file itself, which was never moved during Apply and is
    not touched here either) is restored.
    """
    try:
        tx = store.get(operation_id)
    except KeyError:
        tx = None
    if not tx:
        return {"ok": False, "error": f"Transaction {operation_id} not found.", "code": "not_found"}

    meta = tx.get("metadata") or {}
    mutation_family = meta.get("mutation_family")
    if mutation_family != "track_replacement_v1":
        return {
            "ok": False,
            "error": f"Transaction {operation_id} is a {mutation_family!r} operation and cannot be rolled back through the track replacement rollback executor.",
            "code": "wrong_mutation_family",
        }

    if not meta.get("rollback_available", True):
        return {"ok": False, "error": "Rollback unavailable for this transaction.", "code": "rollback_unavailable"}

    if tx.get("status") != "Completed":
        return {"ok": False, "error": f"Cannot rollback transaction in status {tx.get('status')}.", "code": "wrong_status"}

    original_item_id = int(meta.get("original_item_id") or 0)
    orig_path_str = meta.get("original_path") or ""
    quarantine_target_str = meta.get("quarantine_target") or ""
    original_item_row = meta.get("original_item_row") or {}

    with _get_apply_lock(operation_id):
        # Re-fetch after acquiring the lock in case a concurrent rollback
        # attempt already completed while we were waiting.
        tx = store.get(operation_id)
        if tx.get("status") != "Completed":
            return {"ok": False, "error": f"Transaction is no longer in a rollback-eligible state (status={tx.get('status')}).", "code": "wrong_status"}

        q_p = Path(quarantine_target_str)
        orig_p = Path(orig_path_str)

        if not quarantine_allowed_root:
            return {"ok": False, "error": "Quarantine root is not configured.", "code": "quarantine_root_missing"}
        q_root_resolved = Path(quarantine_allowed_root).resolve(strict=False)

        if not q_p.exists():
            return {"ok": False, "error": f"Quarantine backup missing at {quarantine_target_str}.", "code": "quarantine_missing"}
        if q_p.is_symlink() or _path_has_symlink_under(q_p, q_root_resolved):
            return {"ok": False, "error": "Symlink detected on the quarantined original; refusing to restore.", "code": "symlink_rejected"}
        try:
            if q_p.resolve(strict=False) != q_root_resolved and q_root_resolved not in q_p.resolve(strict=False).parents:
                return {"ok": False, "error": "Quarantine path is outside the trusted quarantine root.", "code": "quarantine_outside_root"}
        except Exception:
            return {"ok": False, "error": "Invalid quarantine path.", "code": "invalid_path"}

        if orig_p.exists() or orig_p.is_symlink():
            return {
                "ok": False,
                "error": f"Something already exists at the original path {orig_path_str}; refusing to overwrite it during rollback.",
                "code": "original_path_occupied",
            }

        # Move the quarantined original into a transaction-owned rollback
        # holding location FIRST. Only after that genuinely succeeds do we
        # treat the destination as safe to populate -- nothing at the
        # original destination is ever destroyed before the restore
        # target is confirmed available.
        holding_p = q_p.with_name(q_p.name + ".rollback-holding")
        try:
            _safe_rename(q_p, holding_p)
        except Exception as exc:
            return {"ok": False, "error": f"Failed to stage quarantined original for restoration: {exc}", "code": "restore_stage_failed"}

        orig_p.parent.mkdir(parents=True, exist_ok=True)
        try:
            _safe_rename(holding_p, orig_p)
        except Exception as exc:
            # Restoration itself failed -- move the file back to its
            # original quarantine slot rather than leaving it stranded
            # under a ".rollback-holding" name with nothing pointing at
            # it, and report Failed (not Rolled Back).
            try:
                _safe_rename(holding_p, q_p)
            except Exception:
                pass
            store.update(operation_id, status="Failed", logs=[f"Rollback restore failed: {exc}"])
            return {"ok": False, "error": f"Failed to restore original file: {exc}", "code": "restore_failed"}

        # Restore the DB row from the captured full-row snapshot.
        db_restored = False
        db_error = ""
        if db_path and os.path.exists(db_path) and original_item_row:
            try:
                columns = list(original_item_row.keys())
                placeholders = ",".join("?" for _ in columns)
                col_list = ",".join(columns)
                values = [original_item_row[c] for c in columns]
                con = sqlite3.connect(db_path, timeout=10)
                try:
                    cur = con.cursor()
                    cur.execute("SELECT COUNT(*) FROM items WHERE id = ?", (original_item_id,))
                    if cur.fetchone()[0] > 0:
                        con.close()
                        # Something re-used this item id since Apply --
                        # do NOT clobber it. Filesystem restore already
                        # succeeded; report the partial state truthfully.
                        store.update(operation_id, status="Partially Rolled Back",
                                     logs=[f"Filesystem restored, but item id {original_item_id} already exists in the DB; DB row was not restored."])
                        return {
                            "ok": True,
                            "operation_id": operation_id,
                            "status": "Partially Rolled Back",
                            "warning": "Filesystem restored; DB row was not restored because the item id is now in use.",
                        }
                    cur.execute(f"INSERT INTO items ({col_list}) VALUES ({placeholders})", values)
                    if cur.rowcount != 1:
                        con.rollback()
                        raise RuntimeError(f"expected to insert 1 row, affected {cur.rowcount}")
                    con.commit()
                finally:
                    con.close()
                db_restored = True
            except Exception as exc:
                db_error = str(exc)

        if not db_restored:
            store.update(operation_id, status="Partially Rolled Back",
                         logs=[f"Filesystem restored, but DB row restoration failed: {db_error}" if db_error else "Filesystem restored, but no DB row snapshot was available to restore."])
            return {
                "ok": True,
                "operation_id": operation_id,
                "status": "Partially Rolled Back",
                "warning": f"Filesystem restored; DB restoration failed: {db_error}" if db_error else "Filesystem restored; no DB snapshot available.",
            }

        store.update(operation_id, status="Rolled Back", logs=["Track replacement rolled back: original file and database row restored."],
                     metadata={**meta, "rollback_available": False})
        return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}


def create_bulk_import_replacement_plan(
    store: TransactionStore,
    payload: Dict[str, Any],
    *,
    db_path: str,
    original_allowed_roots: List[str],
    quarantine_base_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a bulk import-time replacement plan (SEC-002 Wave 18 final review).

    This is a from-scratch rewrite. The original implementation had
    several confirmed defects, the most severe being that
    _merge_imported_album_into_existing (its only real caller) had become
    a silent no-op due to an unrelated indentation bug in the same diff --
    but even taken at face value, the transaction never verified that a
    replacement had actually been imported before allowing old library
    state to be retired, had no explicit old->new item mapping (only a
    flat list of old item ids, with new items "discovered" by whatever
    else was left in the album), and enforced no per-track identity
    evidence at all (only an optional, easily-omitted album-level RGID
    check).

    Architecture decision (Model B, not Model A): by the time this
    function's only real caller runs, Beets has ALREADY imported the
    replacement media as its own library item(s) with a real path under
    the music library root -- _merge_imported_album_into_existing's whole
    job is reconciling that already-imported result against a
    pre-existing album, not triggering an import itself. So this
    transaction does not own or trigger import (no import_executor
    callback across the engine IPC boundary -- passing an arbitrary
    Python callback across a process/IPC boundary is not a sound engine
    API, and there is no legitimate substitute for it here since the real
    import already happened before Plan is ever created). It owns
    retirement of old items in favor of already-imported, already-
    identified new items, bound together by an explicit, caller-supplied
    old_item_id -> new_item_id mapping that this function independently
    verifies before any Plan can be created.

    Guarantees:
    - Non-mutating: creates transaction metadata only.
    - Explicit mapping required: every old item retired must be named in
      `mappings` together with the specific new (already-imported) item
      that replaces it. There is no "resolve every row currently in the
      album" fallback -- an unrelated pre-existing row can never
      masquerade as a verified replacement.
    - Per-mapping identity evidence required: every mapping must carry a
      non-AI-only `identity_source`; the new item must be a real,
      currently-existing Beets row under the music library root with a
      non-empty file; Release Group ID is cross-checked between old and
      new when either side has one recorded.
    - Path safety: null-byte checks, non-symlink checks (full parent
      component walk, not just the leaf), and music-library-root
      containment for both old and new item paths.
    """
    if not db_path or not os.path.exists(db_path):
        return {"ok": False, "error": f"Beets database not found at {db_path}.", "code": "db_not_found"}

    existing_album_id = int(payload.get("existing_album_id") or payload.get("album_id") or 0)

    raw_mappings = payload.get("mappings")
    if not isinstance(raw_mappings, list) or not raw_mappings:
        return {"ok": False, "error": "Explicit old_item_id -> new_item_id mappings are required.", "code": "replacement_mapping_missing"}

    forced_ids: Optional[set] = None
    forced_ids_raw = payload.get("old_item_ids")
    if forced_ids_raw:
        try:
            forced_ids = {int(v) for v in forced_ids_raw if str(v).isdigit() and int(v) > 0}
        except Exception:
            return {"ok": False, "error": "old_item_ids contains a non-integer value.", "code": "invalid_payload"}

    parsed_mappings: List[Dict[str, Any]] = []
    seen_old_ids: set = set()
    seen_new_ids: set = set()
    for entry in raw_mappings:
        if not isinstance(entry, dict):
            return {"ok": False, "error": "Each mapping entry must be an object.", "code": "invalid_payload"}
        try:
            old_id = int(entry.get("old_item_id") or 0)
            new_id = int(entry.get("new_item_id") or 0)
        except Exception:
            return {"ok": False, "error": "Mapping old_item_id/new_item_id must be integers.", "code": "invalid_payload"}
        if old_id <= 0 or new_id <= 0:
            return {"ok": False, "error": "Each mapping requires a positive old_item_id and new_item_id.", "code": "invalid_payload"}
        if old_id == new_id:
            return {"ok": False, "error": f"old_item_id and new_item_id cannot be the same item ({old_id}).", "code": "replacement_mapping_ambiguous"}
        if old_id in seen_old_ids:
            return {"ok": False, "error": f"old_item_id {old_id} appears in more than one mapping.", "code": "replacement_mapping_ambiguous"}
        if new_id in seen_new_ids:
            return {"ok": False, "error": f"new_item_id {new_id} is claimed as the replacement for more than one old item.", "code": "replacement_mapping_ambiguous"}
        seen_old_ids.add(old_id)
        seen_new_ids.add(new_id)

        identity_source = str(entry.get("identity_source") or "").strip().lower()
        if identity_source in {"", "ai", "ai_suggestion", "ai_only"}:
            return {
                "ok": False,
                "error": f"Mapping for old item {old_id} has no acceptable identity_source (AI-only evidence is not sufficient).",
                "code": "ai_only_evidence_rejected",
            }

        parsed_mappings.append({"old_item_id": old_id, "new_item_id": new_id, "identity_source": identity_source})

    if forced_ids is not None and forced_ids != seen_old_ids:
        return {
            "ok": False,
            "error": "old_item_ids does not match the set of old_item_id values referenced by mappings.",
            "code": "replacement_mapping_ambiguous",
        }

    old_item_ids = sorted(seen_old_ids)

    # --- Validate old items (role: music-library root only, bound by item_id) ---
    orig_roots_resolved = [Path(r).resolve(strict=False) for r in (original_allowed_roots or [])]
    old_items_by_id: Dict[int, Dict[str, Any]] = {}
    for old_id in old_item_ids:
        try:
            item_row = _fetch_authoritative_item_row(db_path, old_id)
        except Exception as exc:
            return {"ok": False, "error": f"Failed to read item {old_id} record: {exc}", "code": "db_read_failed"}
        if item_row is None:
            return {"ok": False, "error": f"Beets item {old_id} not found in database.", "code": "item_not_found"}

        item_path_raw = str(item_row.get("path") or "")
        if not item_path_raw or "\x00" in item_path_raw:
            return {"ok": False, "error": f"Beets item {old_id} has no usable recorded path.", "code": "item_path_missing"}
        if os.path.islink(item_path_raw):
            return {"ok": False, "error": f"Symlinks are not permitted for item {old_id}.", "code": "symlink_rejected"}

        item_p = Path(item_path_raw)
        if item_p.is_symlink():
            return {"ok": False, "error": f"Symlinks are not permitted for item {old_id}.", "code": "symlink_rejected"}

        abs_item = Path(os.path.normpath(str(item_p if item_p.is_absolute() else item_p.absolute())))
        orig_root = next((r for r in orig_roots_resolved if abs_item == r or r in abs_item.parents), None)
        if orig_root is None:
            return {"ok": False, "error": f"Item {old_id} path {abs_item} is outside the music library root.", "code": "original_outside_roots"}
        if _path_has_symlink_under(abs_item, orig_root):
            return {"ok": False, "error": f"Symlink component detected in item {old_id} path {abs_item}.", "code": "symlink_rejected"}

        try:
            resolved_item = abs_item.resolve(strict=False)
        except Exception:
            return {"ok": False, "error": f"Invalid path for item {old_id}.", "code": "invalid_path"}
        if resolved_item.is_symlink():
            return {"ok": False, "error": f"Symlink target detected for item {old_id}.", "code": "symlink_rejected"}
        if not (resolved_item == orig_root or orig_root in resolved_item.parents):
            return {"ok": False, "error": f"Item {old_id} path {resolved_item} is outside the music library root.", "code": "original_outside_roots"}

        st_item = resolved_item.stat() if resolved_item.exists() and resolved_item.is_file() else None
        old_items_by_id[old_id] = {
            "id": old_id,
            "album_id": int(item_row.get("album_id") or 0),
            "path": str(resolved_item),
            "title": str(item_row.get("title") or ""),
            "disc": int(item_row.get("disc") or 1),
            "track": int(item_row.get("track") or 0),
            "mb_trackid": str(item_row.get("mb_trackid") or "").strip().lower(),
            "mb_releasegroupid": str(item_row.get("mb_releasegroupid") or "").strip().lower(),
            "stat": {
                "inode": st_item.st_ino,
                "dev": st_item.st_dev,
                "size": st_item.st_size,
                "mtime_ns": getattr(st_item, "st_mtime_ns", int(st_item.st_mtime * 1e9)),
            } if st_item else None,
            "raw_row": item_row,
        }

    # --- Validate new items (role: ALSO the music-library root -- already
    # imported by Beets before this transaction runs; never a staging path) ---
    new_items_by_id: Dict[int, Dict[str, Any]] = {}
    for mapping in parsed_mappings:
        new_id = mapping["new_item_id"]
        if new_id in new_items_by_id:
            continue
        try:
            new_row = _fetch_authoritative_item_row(db_path, new_id)
        except Exception as exc:
            return {"ok": False, "error": f"Failed to read replacement item {new_id} record: {exc}", "code": "db_read_failed"}
        if new_row is None:
            return {
                "ok": False,
                "error": f"Replacement item {new_id} not found in database -- import may not have completed.",
                "code": "bulk_replacement_import_required",
            }

        new_path_raw = str(new_row.get("path") or "")
        if not new_path_raw or "\x00" in new_path_raw:
            return {"ok": False, "error": f"Replacement item {new_id} has no usable recorded path.", "code": "item_path_missing"}
        if os.path.islink(new_path_raw):
            return {"ok": False, "error": f"Symlinks are not permitted for replacement item {new_id}.", "code": "symlink_rejected"}

        new_p = Path(new_path_raw)
        if new_p.is_symlink():
            return {"ok": False, "error": f"Symlinks are not permitted for replacement item {new_id}.", "code": "symlink_rejected"}

        abs_new = Path(os.path.normpath(str(new_p if new_p.is_absolute() else new_p.absolute())))
        new_root = next((r for r in orig_roots_resolved if abs_new == r or r in abs_new.parents), None)
        if new_root is None:
            return {"ok": False, "error": f"Replacement item {new_id} path {abs_new} is outside the music library root.", "code": "candidate_outside_roots"}
        if _path_has_symlink_under(abs_new, new_root):
            return {"ok": False, "error": f"Symlink component detected in replacement item {new_id} path {abs_new}.", "code": "symlink_rejected"}

        try:
            resolved_new = abs_new.resolve(strict=False)
        except Exception:
            return {"ok": False, "error": f"Invalid path for replacement item {new_id}.", "code": "invalid_path"}
        if resolved_new.is_symlink():
            return {"ok": False, "error": f"Symlink target detected for replacement item {new_id}.", "code": "symlink_rejected"}
        if not (resolved_new == new_root or new_root in resolved_new.parents):
            return {"ok": False, "error": f"Replacement item {new_id} path {resolved_new} is outside the music library root.", "code": "candidate_outside_roots"}

        if not resolved_new.exists() or not resolved_new.is_file():
            return {"ok": False, "error": f"Replacement item {new_id} file does not exist.", "code": "bulk_replacement_import_required"}
        st_new = resolved_new.stat()
        if st_new.st_size <= 0:
            return {"ok": False, "error": f"Replacement item {new_id} file is empty.", "code": "candidate_empty"}

        new_items_by_id[new_id] = {
            "id": new_id,
            "path": str(resolved_new),
            "title": str(new_row.get("title") or ""),
            "mb_trackid": str(new_row.get("mb_trackid") or "").strip().lower(),
            "mb_releasegroupid": str(new_row.get("mb_releasegroupid") or "").strip().lower(),
            "stat": {
                "inode": st_new.st_ino,
                "dev": st_new.st_dev,
                "size": st_new.st_size,
                "mtime_ns": getattr(st_new, "st_mtime_ns", int(st_new.st_mtime * 1e9)),
            },
        }

    # --- Per-mapping matching authority ---
    for mapping in parsed_mappings:
        old = old_items_by_id[mapping["old_item_id"]]
        new = new_items_by_id[mapping["new_item_id"]]

        old_rgid = old.get("mb_releasegroupid")
        new_rgid = new.get("mb_releasegroupid")
        if old_rgid and new_rgid and old_rgid != new_rgid:
            return {
                "ok": False,
                "error": f"Old item {old['id']} belongs to Release Group {old_rgid}, but replacement item {new['id']} belongs to {new_rgid}.",
                "code": "release_group_mismatch",
            }

        # "forced_replacement_verified" is the automatic music-format
        # retry pipeline's identity_source (SEC-002 Wave 17 precedent):
        # that caller already ran a real AcoustID fingerprint match
        # against the specific original item before ever starting the
        # import that produced this new item, so a missing Recording ID
        # tag on the freshly-imported file does not by itself invalidate
        # already-established evidence. Every other identity_source must
        # be backed by an actual Recording ID on the new item.
        if mapping["identity_source"] != "forced_replacement_verified" and not new.get("mb_trackid"):
            return {
                "ok": False,
                "error": f"Replacement item {new['id']} has no Recording ID and identity_source does not carry independent verification.",
                "code": "recording_id_required",
            }

        mapping["old_recording_id"] = old.get("mb_trackid") or ""
        mapping["new_recording_id"] = new.get("mb_trackid") or ""
        mapping["old_path"] = old["path"]
        mapping["new_path"] = new["path"]

    # --- Create transaction ---
    old_items = [old_items_by_id[i] for i in old_item_ids]
    tx_data = store.create(
        operation_type="Replace",
        initiating_user=str(payload.get("initiating_user") or "operator"),
        status="Preview",
        summary=f"Bulk import replacement for album {existing_album_id or 'items'} ({len(old_items)} track(s))",
        reason=str(payload.get("reason") or "Bulk import-time replacement"),
        source="Bulk Import Replacement",
        changes=[{
            "id": f"album:{existing_album_id}" if existing_album_id else f"items:{len(old_items)}",
            "operation": "Bulk Import Replacement",
            "mutation_family": "bulk_import_replacement_v1",
            "existing_album_id": existing_album_id,
            "old_item_ids": old_item_ids,
            "mappings": parsed_mappings,
        }],
        metadata={
            "mutation_family": "bulk_import_replacement_v1",
            "reversibility": "RECOVERABLE",
            "rollback_available": True,
            "existing_album_id": existing_album_id,
            "source_folder": str(payload.get("source_folder") or payload.get("aldir") or payload.get("folder") or ""),
            "old_items": old_items,
            "old_item_ids": old_item_ids,
            "mappings": parsed_mappings,
            "apply_steps": [
                {"step": "mappings_reverified", "status": "pending"},
                {"step": "old_items_quarantined", "status": "pending"},
                {"step": "old_db_rows_retired", "status": "pending"},
            ],
        },
    )

    op_id = tx_data["id"]

    # Compute server quarantine target for each old item AFTER store.create()
    # returns the real, durable transaction id -- SEC-002 Wave 17's exact
    # quarantine-path/transaction-id bug fix applies equally here.
    base_quarantine = quarantine_base_root or os.environ.get("REPLACEMENT_QUARANTINE_DIR", "/config/track_replacement_quarantine")
    date_str = time.strftime("%Y%m%d")
    quarantine_targets = {}
    for old in old_items:
        orig_name = Path(old["path"]).name
        q_target = str(Path(base_quarantine) / "library" / date_str / op_id / f"{old['id']}_{orig_name}")
        old["quarantine_target"] = q_target
        quarantine_targets[str(old["id"])] = q_target

    store.update(op_id, metadata={
        **tx_data["metadata"],
        "old_items": old_items,
        "quarantine_targets": quarantine_targets,
    })

    return {
        "ok": True,
        "operation_id": op_id,
        "plan": store.get(op_id),
        "mappings": parsed_mappings,
        "old_item_ids": old_item_ids,
    }


def execute_bulk_import_replacement_apply(
    store: TransactionStore,
    operation_id: str,
    *,
    db_path: str,
    original_allowed_roots: List[str],
    quarantine_base_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a bulk import-time replacement apply (SEC-002 Wave 18 final
    review). Acquires deterministic, sorted resource locks on the existing
    album id and every old item id BEFORE the per-operation apply lock, so
    two transactions that both touch item 101 (whether or not they are the
    same transaction, and whether or not they share an album) cannot race,
    while transactions touching entirely disjoint albums/items remain
    fully parallel."""
    try:
        peek = store.get(operation_id)
    except KeyError:
        peek = None
    if not peek:
        return {"ok": False, "error": f"Transaction {operation_id} not found.", "code": "malformed_transaction_id", "mutated": False, "retryable": False}

    meta = peek.get("metadata") or {}
    lock_keys = set()
    album_id = int(meta.get("existing_album_id") or 0)
    if album_id:
        lock_keys.add(f"album:{album_id}")
    for old_id in (meta.get("old_item_ids") or []):
        try:
            lock_keys.add(f"item:{int(old_id)}")
        except Exception:
            continue

    with ExitStack() as stack:
        for key in sorted(lock_keys):
            stack.enter_context(_get_resource_lock(key))
        with _get_apply_lock(operation_id):
            return _execute_bulk_import_replacement_apply_locked(
                store, operation_id,
                db_path=db_path,
                original_allowed_roots=original_allowed_roots,
                quarantine_base_root=quarantine_base_root,
            )


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


def _execute_bulk_import_replacement_apply_locked(
    store: TransactionStore,
    operation_id: str,
    *,
    db_path: str,
    original_allowed_roots: List[str],
    quarantine_base_root: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        tx = store.get(operation_id)
    except KeyError:
        tx = None
    if not tx:
        return {"ok": False, "error": f"Transaction {operation_id} not found.", "code": "malformed_transaction_id", "mutated": False, "retryable": False}

    if tx.get("status") == "Completed":
        return {"ok": True, "already_completed": True, "operation_id": operation_id, "status": "Completed"}

    meta = tx.get("metadata") or {}
    mutation_family = meta.get("mutation_family")
    if mutation_family != "bulk_import_replacement_v1":
        return {
            "ok": False,
            "error": f"Transaction {operation_id} is a {mutation_family!r} operation and cannot be applied through the bulk import replacement executor.",
            "code": "family_mismatch",
            "mutated": False,
            "retryable": False,
        }

    old_items = meta.get("old_items") or []
    old_item_ids = meta.get("old_item_ids") or [o.get("id") for o in old_items if o.get("id")]
    mappings = meta.get("mappings") or []
    if not old_items or not old_item_ids or not mappings:
        return _track_replacement_fail(store, operation_id, "Plan is missing old_items/mappings.", "Transaction plan is corrupt or incomplete.", code="plan_corrupt", mutated=False)

    steps = meta.get("apply_steps") or []
    step_status = {s.get("step"): s.get("status") for s in steps}

    def _persist_step(name: str, status: str, **extra: Any) -> None:
        # `extra` (e.g. quarantined_files=...) is merged into the
        # TOP-LEVEL transaction metadata, not just nested inside the step
        # record -- rollback and any later resumed Apply read
        # meta["quarantined_files"] directly, and a failure return must
        # still be able to see whatever was durably mutated so far.
        nonlocal steps, meta
        found = False
        for s in steps:
            if s.get("step") == name:
                s["status"] = status
                found = True
                break
        if not found:
            steps.append({"step": name, "status": status})
        meta = {**meta, "apply_steps": steps, **extra}
        store.update(operation_id, metadata=meta)

    orig_roots_resolved = [Path(r).resolve(strict=False) for r in (original_allowed_roots or [])]

    # --- Step 1: re-verify every mapping (old AND new item) before any
    # mutation. All-or-nothing: if any single mapping fails, refuse the
    # entire apply -- no old item is retired unless every required
    # replacement in this transaction is independently re-confirmed. ---
    if step_status.get("mappings_reverified") != "completed":
        for old in old_items:
            err = _bulk_replacement_toctou_check(old.get("path") or "", old.get("stat"), orig_roots_resolved)
            if err:
                return _track_replacement_fail(
                    store, operation_id, f"Old item {old.get('id')} TOCTOU/{err}",
                    f"File replacement detected at old item {old.get('id')} ({err}).",
                    code="toctou_mismatch", mutated=False,
                )
        for mapping in mappings:
            new_path = mapping.get("new_path") or ""
            # Re-derive expected identity from the authoritative DB row
            # rather than trusting client-shaped Plan metadata alone.
            try:
                new_row = _fetch_authoritative_item_row(db_path, int(mapping["new_item_id"]))
            except Exception as exc:
                return _track_replacement_fail(
                    store, operation_id, f"Failed to re-read replacement item {mapping.get('new_item_id')}: {exc}",
                    "Failed to re-verify a replacement item.", code="db_read_failed", mutated=False,
                )
            if new_row is None:
                return _track_replacement_fail(
                    store, operation_id, f"Replacement item {mapping.get('new_item_id')} vanished before Apply.",
                    f"Replacement item {mapping.get('new_item_id')} no longer exists.",
                    code="bulk_replacement_import_required", mutated=False,
                )
            current_path = str(new_row.get("path") or "")
            if current_path != new_path:
                return _track_replacement_fail(
                    store, operation_id, f"Replacement item {mapping.get('new_item_id')} path changed since Plan.",
                    f"Replacement item {mapping.get('new_item_id')} path changed since Plan -- refusing to proceed.",
                    code="toctou_mismatch", mutated=False,
                )
            err = _bulk_replacement_toctou_check(new_path, None, orig_roots_resolved)
            if err:
                return _track_replacement_fail(
                    store, operation_id, f"Replacement item {mapping.get('new_item_id')} {err}",
                    f"Replacement item {mapping.get('new_item_id')} failed re-verification ({err}).",
                    code="toctou_mismatch", mutated=False,
                )
        _persist_step("mappings_reverified", "completed")

    # --- Step 2: quarantine old files, one at a time, in deterministic
    # (sorted) order, with durable per-item progress so a crash mid-loop
    # resumes correctly instead of re-quarantining an already-moved file
    # or losing track of what already happened. ---
    quarantine_targets = meta.get("quarantine_targets") or {}
    already_quarantined = {q.get("old_id") for q in (meta.get("quarantined_files") or [])}
    quarantined_files = list(meta.get("quarantined_files") or [])
    base_quarantine = quarantine_base_root or os.environ.get("REPLACEMENT_QUARANTINE_DIR", "/config/track_replacement_quarantine")
    try:
        quarantine_root_resolved = Path(base_quarantine).resolve(strict=False)
    except Exception:
        return _track_replacement_fail(store, operation_id, "Invalid quarantine root.", "Server quarantine root is misconfigured.", code="invalid_path", mutated=False)

    def _fail_mid_quarantine(log_msg: str, error_msg: str, code: str) -> Dict[str, Any]:
        # Persist whatever was already durably quarantined BEFORE
        # returning the failure -- without this, a partial-quarantine
        # failure would report mutated=True but leave meta["quarantined_files"]
        # stale/empty, stranding the transaction with no visible recovery
        # material for rollback to act on.
        _persist_step("old_items_quarantined", "in_progress", quarantined_files=quarantined_files)
        return _track_replacement_fail(store, operation_id, log_msg, error_msg, code=code, mutated=bool(quarantined_files))

    if step_status.get("old_items_quarantined") != "completed":
        for old in sorted(old_items, key=lambda o: int(o["id"])):
            old_id = int(old["id"])
            if old_id in already_quarantined:
                continue
            orig_path_str = old.get("path") or ""
            q_target_str = old.get("quarantine_target") or quarantine_targets.get(str(old_id)) or ""
            if not q_target_str:
                return _fail_mid_quarantine(
                    f"Missing quarantine target for item {old_id}.",
                    f"No quarantine destination computed for item {old_id}.", "invalid_path",
                )
            q_p = Path(q_target_str)
            # Lexical (normpath), NOT filesystem-resolved, containment
            # check: the quarantine subdirectory (.../library/<date>/<op_id>)
            # does not exist yet on a transaction's first Apply, and two
            # different transactions' Apply calls create sibling
            # directories under the same quarantine root concurrently
            # (different op_ids, different old items -- meant to proceed
            # in parallel). resolve(strict=False) walking a not-yet-
            # existing path while a SIBLING directory is being created by
            # another thread is not reliably race-free on every platform,
            # and silently falling back to the unresolved form on any
            # resolve() exception compared an incomparable path
            # representation against the already-resolved quarantine
            # root -- spuriously failing this check under concurrency.
            # quarantine_root_resolved and q_target_str were both derived
            # from the SAME base_quarantine value, so a pure string-level
            # normpath comparison is sufficient and touches no filesystem
            # state that could be racing.
            q_parent_normalized = Path(os.path.normpath(str(q_p.parent)))
            if quarantine_root_resolved not in q_parent_normalized.parents and q_parent_normalized != quarantine_root_resolved:
                return _fail_mid_quarantine(
                    f"Quarantine target for item {old_id} escapes the trusted quarantine root.",
                    f"Unsafe quarantine destination for item {old_id}.", "invalid_path",
                )

            orig_p = Path(orig_path_str)
            if not orig_p.exists():
                # A crash-recovery retry may land here if the file was
                # already physically moved but the durable step wasn't
                # marked complete yet -- only treat this as safe if the
                # file is now actually sitting at the quarantine target.
                if q_p.exists():
                    quarantined_files.append({"old_id": old_id, "old_path": orig_path_str, "quarantine_target": q_target_str})
                    _persist_step("old_items_quarantined", "in_progress", quarantined_files=quarantined_files)
                    continue
                return _fail_mid_quarantine(
                    f"Old item {old_id} file vanished before quarantine.",
                    f"Old item {old_id}'s file no longer exists.", "toctou_mismatch",
                )

            if _path_has_symlink_under(q_p.parent, quarantine_root_resolved) or (q_p.parent.exists() and _is_symlink_path(q_p.parent)):
                return _fail_mid_quarantine(
                    f"Symlink component in quarantine destination for item {old_id}.",
                    f"Unsafe quarantine destination for item {old_id}.", "symlink_rejected",
                )

            try:
                q_p.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.chmod(str(q_p.parent), 0o700)
                except Exception:
                    pass
                if q_p.exists() or q_p.is_symlink():
                    return _fail_mid_quarantine(
                        f"Quarantine destination for item {old_id} already exists.",
                        f"Quarantine destination for item {old_id} already exists.", "quarantine_destination_exists",
                    )
                _safe_rename(orig_p, q_p)
            except Exception as exc:
                return _fail_mid_quarantine(
                    f"Quarantine move failed for item {old_id}: {exc}",
                    f"Quarantine move failed for item {old_id}.", "retirement_failed",
                )

            quarantined_files.append({"old_id": old_id, "old_path": orig_path_str, "quarantine_target": q_target_str})
            _persist_step("old_items_quarantined", "in_progress", quarantined_files=quarantined_files)

        _persist_step("old_items_quarantined", "completed", quarantined_files=quarantined_files)

    # --- Step 3: retire old DB rows -- one bulk DELETE (efficient), but
    # only after every row was individually pre-validated above, with the
    # exact affected rowcount verified and a post-delete re-read
    # confirming every intended id is actually gone. ---
    if step_status.get("old_db_rows_retired") != "completed":
        if not db_path or not os.path.exists(db_path):
            return _track_replacement_fail(store, operation_id, "DB missing at retirement time.", "Beets database not found.", code="db_not_found", mutated=True)
        try:
            con = sqlite3.connect(db_path, timeout=10)
            try:
                cur = con.cursor()
                # Re-read live rows for the exact expected set immediately
                # before deleting -- a Plan-time snapshot is not enough;
                # confirm nothing about ownership/identity changed and
                # that all ids are still actually present.
                placeholders = ",".join("?" for _ in old_item_ids)
                cur.execute(f"SELECT id FROM items WHERE id IN ({placeholders})", old_item_ids)
                present_ids = {int(r[0]) for r in cur.fetchall()}
                missing_ids = sorted(set(old_item_ids) - present_ids)
                if missing_ids:
                    con.close()
                    return _track_replacement_fail(
                        store, operation_id, f"Old items already gone before retirement: {missing_ids}",
                        f"Old items {missing_ids} no longer exist -- refusing to proceed.",
                        code="old_item_stale", mutated=True,
                    )
                cur.execute(f"DELETE FROM items WHERE id IN ({placeholders})", old_item_ids)
                deleted_count = cur.rowcount
                if deleted_count != len(old_item_ids):
                    con.rollback()
                    con.close()
                    return _track_replacement_fail(
                        store, operation_id, f"DB rowcount mismatch: expected {len(old_item_ids)}, deleted {deleted_count}",
                        "Database retirement affected an unexpected number of rows; rolled back.",
                        code="db_rowcount_mismatch", mutated=True,
                    )
                con.commit()
                # Post-delete verification: confirm every intended id is
                # actually gone, never equate commit() with success.
                cur.execute(f"SELECT id FROM items WHERE id IN ({placeholders})", old_item_ids)
                still_present = [int(r[0]) for r in cur.fetchall()]
            finally:
                con.close()
        except Exception as exc:
            return _track_replacement_fail(
                store, operation_id, f"DB row deletion failed for old items: {exc}",
                "Database retirement failed.", code="db_update_failed", mutated=True,
            )
        if still_present:
            return _track_replacement_fail(
                store, operation_id, f"Old items still present after delete: {still_present}",
                "Database retirement did not take effect as expected.", code="verification_failed", mutated=True,
            )
        _persist_step("old_db_rows_retired", "completed", retired_item_ids=old_item_ids)
        meta = store.get(operation_id).get("metadata") or {}

    store.update(
        operation_id,
        status="Completed",
        applied_at=time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        logs=["Bulk import replacement applied successfully."],
        metadata={
            **meta,
            "mutated": True,
            "quarantined_files": quarantined_files,
            "retired_item_ids": old_item_ids,
            "rollback_available": True,
        },
    )

    return {
        "ok": True,
        "operation_id": operation_id,
        "status": "Completed",
        "retired_count": len(old_item_ids),
        "quarantined_count": len(quarantined_files),
    }


def rollback_bulk_import_replacement(
    store: TransactionStore,
    operation_id: str,
    *,
    db_path: str,
    original_allowed_roots: List[str],
    quarantine_base_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Rollback a bulk import replacement transaction (SEC-002 Wave 18
    final review). Recoverable for Completed transactions AND for Failed
    transactions that durably mutated part of the filesystem/DB before
    failing (partial quarantine, or a DB retirement that only partially
    took effect) -- gating rollback on status == Completed alone would
    strand exactly that partial-mutation case with no public recovery
    path. Never touches the newly-imported replacement items: this
    transaction does not own their import, so rollback only ever restores
    the OLD items it itself retired."""
    try:
        tx = store.get(operation_id)
    except KeyError:
        tx = None
    if not tx:
        return {"ok": False, "error": f"Transaction {operation_id} not found.", "code": "malformed_transaction_id"}

    meta = tx.get("metadata") or {}
    mutation_family = meta.get("mutation_family")
    if mutation_family != "bulk_import_replacement_v1":
        return {
            "ok": False,
            "error": f"Transaction {operation_id} is a {mutation_family!r} operation and cannot be rolled back through the bulk import replacement rollback executor.",
            "code": "wrong_mutation_family",
        }

    status = tx.get("status")
    quarantined_files = meta.get("quarantined_files") or []
    retired_item_ids = meta.get("retired_item_ids") or []
    mutated_anything = bool(quarantined_files) or bool(retired_item_ids) or bool(meta.get("mutated"))

    if status not in ("Completed", "Failed"):
        return {"ok": False, "error": f"Cannot rollback transaction in status {status!r}.", "code": "invalid_status"}
    if status == "Failed" and not mutated_anything:
        return {"ok": False, "error": "Nothing was mutated; there is nothing to roll back.", "code": "rollback_not_needed"}
    if not meta.get("rollback_available", True):
        return {"ok": False, "error": "Rollback unavailable for this transaction.", "code": "rollback_unavailable"}

    old_items = meta.get("old_items") or []
    old_by_id = {int(o["id"]): o for o in old_items}

    orig_roots_resolved = [Path(r).resolve(strict=False) for r in (original_allowed_roots or [])]
    base_quarantine = quarantine_base_root or os.environ.get("REPLACEMENT_QUARANTINE_DIR", "/config/track_replacement_quarantine")
    try:
        quarantine_root_resolved = Path(base_quarantine).resolve(strict=False)
    except Exception:
        quarantine_root_resolved = None

    restored_files: List[str] = []
    restored_old_ids: set = set()
    failed_restores: List[Dict[str, Any]] = []
    for qf in quarantined_files:
        old_id_qf = qf.get("old_id")
        old_path_str = qf.get("old_path") or ""
        q_target_str = qf.get("quarantine_target") or ""
        q_p = Path(q_target_str)
        orig_p = Path(old_path_str)

        if not q_p.exists():
            # Already restored on a prior (partial) rollback attempt.
            if orig_p.exists():
                restored_files.append(old_path_str)
                restored_old_ids.add(old_id_qf)
            continue

        if quarantine_root_resolved is not None and not (q_p.resolve(strict=False) == quarantine_root_resolved or quarantine_root_resolved in q_p.resolve(strict=False).parents):
            failed_restores.append({"old_id": qf.get("old_id"), "reason": "quarantine_path_outside_root"})
            continue
        if _is_symlink_path(q_p) or _path_has_symlink_under(q_p, quarantine_root_resolved or q_p.parent):
            failed_restores.append({"old_id": qf.get("old_id"), "reason": "symlink_rejected"})
            continue

        orig_root = next((r for r in orig_roots_resolved if orig_p == r or r in orig_p.parents), None)
        if orig_root is None:
            failed_restores.append({"old_id": qf.get("old_id"), "reason": "original_outside_roots"})
            continue
        if orig_p.exists() or orig_p.is_symlink():
            failed_restores.append({"old_id": qf.get("old_id"), "reason": "original_path_occupied"})
            continue

        # Wave 17 ordering: move to a transaction-owned holding location
        # first, then to the original path -- nothing at the quarantine
        # slot is destroyed before the restore target is confirmed vacant
        # and the move to it has actually succeeded.
        holding = q_p.with_name(q_p.name + ".rollback-holding")
        try:
            _safe_rename(q_p, holding)
        except Exception as exc:
            failed_restores.append({"old_id": qf.get("old_id"), "reason": f"holding_move_failed: {exc}"})
            continue
        try:
            orig_p.parent.mkdir(parents=True, exist_ok=True)
            _safe_rename(holding, orig_p)
            restored_files.append(old_path_str)
            restored_old_ids.add(old_id_qf)
        except Exception as exc:
            try:
                _safe_rename(holding, q_p)
            except Exception:
                pass
            failed_restores.append({"old_id": qf.get("old_id"), "reason": f"restore_failed: {exc}"})

    # Restore DB rows only for old items whose retirement actually
    # completed (retired_item_ids) AND whose FILE restore just succeeded
    # above -- restoring only the DB half would point a live item row at
    # a path that either still has no file, or (worse) has since been
    # occupied by something unrelated the file-restore step correctly
    # refused to overwrite. DB and file state must move back together.
    db_restored = 0
    db_restore_failed: List[int] = []
    restorable_ids = [i for i in retired_item_ids if i in restored_old_ids]
    skipped_ids = [i for i in retired_item_ids if i not in restored_old_ids]
    db_restore_failed.extend(skipped_ids)
    if restorable_ids and db_path and os.path.exists(db_path):
        con = sqlite3.connect(db_path, timeout=10)
        try:
            cur = con.cursor()
            for old_id in restorable_ids:
                old = old_by_id.get(int(old_id))
                raw_row = (old or {}).get("raw_row")
                if not raw_row or not isinstance(raw_row, dict):
                    db_restore_failed.append(old_id)
                    continue
                try:
                    cur.execute("SELECT id FROM items WHERE id = ?", (old_id,))
                    if cur.fetchone() is not None:
                        # Item ID has been reused since retirement -- do
                        # not clobber whatever now legitimately owns it.
                        db_restore_failed.append(old_id)
                        continue
                    columns = list(raw_row.keys())
                    placeholders = ",".join("?" for _ in columns)
                    col_list = ",".join(columns)
                    values = [raw_row[c] for c in columns]
                    cur.execute(f"INSERT INTO items ({col_list}) VALUES ({placeholders})", values)
                    db_restored += 1
                except Exception:
                    db_restore_failed.append(old_id)
            con.commit()
        finally:
            con.close()

    full_success = not failed_restores and not db_restore_failed and (bool(restored_files) or not quarantined_files) and (db_restored == len(retired_item_ids))
    if full_success:
        final_status = "Rolled Back"
        rollback_available = False
    elif restored_files or db_restored:
        final_status = "Partially Rolled Back"
        rollback_available = True
    else:
        final_status = "Failed"
        rollback_available = True

    store.update(
        operation_id,
        status=final_status,
        logs=[f"Bulk import replacement rollback: {final_status}."],
        metadata={
            **meta,
            "rollback_available": rollback_available,
            "rollback_failed_restores": failed_restores,
            "rollback_db_restore_failed": db_restore_failed,
        },
    )
    return {
        "ok": final_status in ("Rolled Back", "Partially Rolled Back"),
        "operation_id": operation_id,
        "status": final_status,
        "restored_files_count": len(restored_files),
        "db_restored_count": db_restored,
        "failed_restores": failed_restores,
        "db_restore_failed": db_restore_failed,
    }


# =========================================================================
# SEC-002 / ARCH-003 Wave 19: MusicBrainz Album Track Repair
# Mutation Family: album_mb_track_repair_v1
# =========================================================================

_AUDIO_TAG_FIELDS = ("mb_trackid", "mb_albumid", "title", "track", "disc")


def _read_file_audio_tags(file_path: Path) -> Dict[str, Any]:
    """Read the MusicBrainz-relevant tags actually stored on disk.

    Returns a structured result -- {"ok": bool, "tags": dict|None,
    "reason": str|None} -- so callers can distinguish "read succeeded,
    tags are genuinely blank" from every failure mode: missing file,
    unsupported/corrupt format, or an unexpected IO error. SEC-002 Wave
    19 final review: the previous implementation swallowed every
    exception and returned {} either way, which made "no tags" and
    "could not read this file" indistinguishable -- and Plan/rollback
    recovery safety depends on knowing which one actually happened.

    Uses the `mediafile` package directly: the actively maintained,
    Beets-owned tag abstraction (`beets.mediafile` is a deprecated shim
    over the same implementation as of Beets 2.x). There is no secondary
    Mutagen fallback -- a second, differently-behaved tag reader is
    exactly the kind of parallel implementation the project architecture
    says to avoid; `mediafile` already IS Beets' own tag layer.
    """
    path_str = str(file_path)
    try:
        exists = file_path.exists()
    except OSError as ex:
        LOG.warning("Audio tag read: stat failed for %s: %s", path_str, ex)
        return {"ok": False, "tags": None, "reason": "io_error"}
    if not exists:
        return {"ok": False, "tags": None, "reason": "not_found"}

    try:
        import mediafile
    except ImportError as ex:
        LOG.error("Audio tag read: mediafile package unavailable: %s", ex)
        return {"ok": False, "tags": None, "reason": "backend_unavailable"}

    try:
        mf = mediafile.MediaFile(file_path)
        result_tags = {
            "mb_trackid": str(getattr(mf, "mb_trackid", "") or "").strip(),
            "mb_albumid": str(getattr(mf, "mb_albumid", "") or "").strip(),
            "title": str(getattr(mf, "title", "") or ""),
            "track": int(getattr(mf, "track", 0) or 0),
            "disc": int(getattr(mf, "disc", 1) or 1),
        }
    except mediafile.UnreadableFileError as ex:
        LOG.warning("Audio tag read: unreadable file %s: %s", path_str, ex)
        return {"ok": False, "tags": None, "reason": "unreadable"}
    except OSError as ex:
        LOG.warning("Audio tag read: io error for %s: %s", path_str, ex)
        return {"ok": False, "tags": None, "reason": "io_error"}
    except Exception as ex:
        LOG.error("Audio tag read: unexpected error for %s: %s", path_str, ex)
        return {"ok": False, "tags": None, "reason": "unknown_error"}
    return {"ok": True, "tags": result_tags, "reason": None}


def _write_file_audio_tags(file_path: Path, tags: Dict[str, Any]) -> Dict[str, Any]:
    """Write MusicBrainz-relevant tags to disk and verify by reading them
    back through an independent read (not the writer's own in-memory
    state).

    Returns {"ok": bool, "reason": str|None, "mismatched_fields": [...]}.
    There is NO fallback that can report success without an authoritative
    writer actually writing the requested values. SEC-002 Wave 19 final
    review: the removed `file_path.touch()` fallback only changed the
    file's mtime -- not a metadata write -- and let unparseable fixtures
    (and any real unsupported file) silently "succeed" while writing
    nothing, with the transaction then reporting the repair as complete.
    A write only counts as successful once the exact requested values are
    independently read back from the file.
    """
    path_str = str(file_path)
    try:
        import mediafile
    except ImportError as ex:
        LOG.error("Audio tag write: mediafile package unavailable: %s", ex)
        return {"ok": False, "reason": "backend_unavailable"}

    try:
        mf = mediafile.MediaFile(file_path)
        for k, v in tags.items():
            if hasattr(mf, k):
                setattr(mf, k, v)
        mf.save()
    except mediafile.UnreadableFileError as ex:
        LOG.warning("Audio tag write: unreadable file %s: %s", path_str, ex)
        return {"ok": False, "reason": "unreadable"}
    except OSError as ex:
        LOG.warning("Audio tag write: io error for %s: %s", path_str, ex)
        return {"ok": False, "reason": "io_error"}
    except Exception as ex:
        LOG.error("Audio tag write: unexpected error for %s: %s", path_str, ex)
        return {"ok": False, "reason": "unknown_error"}

    readback = _read_file_audio_tags(file_path)
    if not readback.get("ok"):
        LOG.error(
            "Audio tag write: readback failed for %s (reason=%s) -- write not verified",
            path_str, readback.get("reason"),
        )
        return {"ok": False, "reason": "readback_failed"}

    actual = readback["tags"] or {}
    mismatched: List[str] = []
    for k, expected in tags.items():
        if k not in _AUDIO_TAG_FIELDS:
            continue
        if k in ("track", "disc"):
            try:
                expected_cmp, actual_cmp = int(expected or 0), int(actual.get(k) or 0)
            except Exception:
                expected_cmp, actual_cmp = expected, actual.get(k)
        else:
            expected_cmp = str(expected or "").strip()
            actual_cmp = str(actual.get(k) or "").strip()
            if k in ("mb_trackid", "mb_albumid"):
                expected_cmp, actual_cmp = expected_cmp.lower(), actual_cmp.lower()
        if expected_cmp != actual_cmp:
            mismatched.append(k)
    if mismatched:
        LOG.error("Audio tag write: readback mismatch for %s: fields=%s", path_str, mismatched)
        return {"ok": False, "reason": "readback_mismatch", "mismatched_fields": mismatched}

    return {"ok": True, "reason": None}


def create_album_mb_track_repair_plan(
    store: TransactionStore,
    payload: Dict[str, Any],
    *,
    music_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
    fetch_tracklist_fn: Optional[Any] = None,
) -> Dict[str, Any]:
    """Create transaction plan for repairing MusicBrainz track IDs for an album (SEC-002 Wave 19)."""
    payload = payload or {}
    try:
        album_id = int(payload.get("album_id") or payload.get("aid") or 0)
    except Exception:
        album_id = 0
    if album_id <= 0:
        return {"ok": False, "error": "Invalid album_id"}

    lib_db = db_path or os.environ.get("BEETS_DB_PATH", os.path.expanduser("~/.config/beets/musiclibrary.blb"))
    if not os.path.exists(lib_db):
        return {"ok": False, "error": f"Beets database file not found at {lib_db}"}

    roots = music_allowed_roots or [os.environ.get("MUSIC_ROOT", "/music")]
    roots_resolved: List[Path] = []
    for r in roots:
        try:
            p = Path(r).resolve()
            if p.exists():
                roots_resolved.append(p)
        except Exception:
            pass
    if not roots_resolved:
        roots_resolved = [Path(r) for r in roots]

    try:
        con = sqlite3.connect(lib_db, timeout=10)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute(
            "SELECT id, album, albumartist, year, mb_albumid, mb_releasegroupid "
            "FROM albums WHERE id=?",
            (album_id,),
        )
        album_row = cur.fetchone()
        if not album_row:
            con.close()
            return {"ok": False, "error": f"Album {album_id} not found"}

        cur.execute(
            "SELECT id, title, track, disc, path, mb_trackid, mb_albumid, length "
            "FROM items WHERE album_id=? ORDER BY disc, track, title, id",
            (album_id,),
        )
        item_rows = cur.fetchall()
        con.close()
    except Exception as ex:
        return {"ok": False, "error": f"Database query failed: {ex}"}

    if not item_rows:
        return {"ok": False, "error": f"Album {album_id} has no track items in database"}

    album_artist = str(album_row["albumartist"] or "").strip()
    album_title = str(album_row["album"] or "").strip()
    album_year = str(album_row["year"] or "").strip()
    album_rg = str(album_row["mb_releasegroupid"] or "").strip().lower()
    caller_override = str(payload.get("mb_albumid") or "").strip().lower()
    target_mb_albumid = caller_override or str(album_row["mb_albumid"] or "").strip().lower()

    if not target_mb_albumid and album_rg:
        try:
            from helpers_mb import _resolve_release_group_to_release
            target_mb_albumid = str(_resolve_release_group_to_release(album_rg, [], year=album_year) or "").strip().lower()
        except Exception:
            pass

    if not target_mb_albumid:
        return {"ok": False, "error": "Album does not have a MusicBrainz release ID"}

    if fetch_tracklist_fn is not None:
        mb = fetch_tracklist_fn(target_mb_albumid)
    else:
        try:
            from helpers_mb import fetch_mb_release_tracklist
            mb = fetch_mb_release_tracklist(target_mb_albumid, [])
        except Exception as ex:
            mb = {"ok": False, "error": str(ex)}

    if not isinstance(mb, dict) or not mb.get("ok"):
        return {"ok": False, "error": (mb.get("error") if isinstance(mb, dict) else None) or "MusicBrainz release lookup failed"}

    candidate_rg = str(mb.get("release_group") or "").strip().lower()
    # Release Group ID remains the canonical album-family identity; Release
    # ID is edition/tracklist evidence. Refuse whenever the selected
    # release's Release Group would conflict with (or silently establish,
    # unreviewed) the album's identity -- never stamp a Release ID's
    # Release Group into mb_releasegroupid as a side effect of a
    # recording-ID repair. SEC-002 Wave 19 final review: this used to only
    # check the conflict case when the caller passed an explicit
    # mb_albumid override AND the album already had an established RG --
    # a blank album_rg went completely unchecked.
    if candidate_rg:
        if album_rg and candidate_rg != album_rg:
            return {
                "ok": False,
                "error": "Requested release belongs to a different Release Group than this album's established identity; refusing repair.",
                "code": "repair_identity_mismatch",
            }
        if not album_rg:
            return {
                "ok": False,
                "error": (
                    "This album has no established MusicBrainz Release Group identity "
                    "(mb_releasegroupid is blank); refusing recording-ID repair until a "
                    "canonical Release Group is established for this album."
                ),
                "code": "repair_rg_not_established",
            }

    mb_tracks = mb.get("tracks") or []
    if not mb_tracks:
        return {"ok": False, "error": "MusicBrainz release tracklist is empty"}

    items_list: List[Dict[str, Any]] = []
    item_stats: Dict[int, Dict[str, Any]] = {}
    item_paths: Dict[int, Path] = {}

    for row in item_rows:
        iid = int(row["id"])
        raw_path = row["path"]
        path_str = raw_path.decode("utf-8", "replace") if isinstance(raw_path, bytes) else str(raw_path)
        # Wave 24 final review round 3, section 11: a real Beets library
        # stores items.path RELATIVE to the music dir; resolve against the
        # configured root before the containment check below, else a
        # correctly-relative value would always (and wrongly) fail it.
        item_path = _resolve_db_path(path_str, [str(r) for r in roots_resolved] or roots)

        # Check containment under allowed roots
        contained = False
        base_root = roots_resolved[0]
        for root in roots_resolved:
            try:
                item_path.relative_to(root)
                contained = True
                base_root = root
                break
            except ValueError:
                pass

        if not contained:
            return {"ok": False, "error": f"Item {iid} path outside allowed music roots", "code": "repair_path_out_of_root"}

        # Symlink component walk check
        if _path_has_symlink_under(item_path, base_root):
            return {"ok": False, "error": f"Item {iid} path contains symlink component", "code": "repair_symlink_rejected"}

        if not item_path.exists():
            return {"ok": False, "error": f"Item file missing: {path_str}", "code": "repair_item_missing"}

        try:
            st = item_path.stat()
            item_stats[iid] = {
                "dev": st.st_dev,
                "ino": st.st_ino,
                "size": st.st_size,
                "mtime_ns": st.st_mtime_ns,
            }
        except Exception as ex:
            return {"ok": False, "error": f"Could not stat item file {path_str}: {ex}", "code": "repair_item_missing"}

        item_paths[iid] = item_path
        items_list.append({
            "id": iid,
            "title": str(row["title"] or ""),
            "track": int(row["track"] or 0),
            "disc": int(row["disc"] or 1),
            "path": path_str,
            "filename": item_path.name,
            "mb_trackid": str(row["mb_trackid"] or "").strip().lower(),
            "mb_albumid": str(row["mb_albumid"] or "").strip().lower(),
            "length": float(row["length"] or 0),
        })

    try:
        from backend.mb_alignment import summarize_mb_track_alignment, best_album_track_match
        alignment = summarize_mb_track_alignment(
            items_list,
            mb_tracks,
            match_fn=best_album_track_match,
            threshold=0.72,
            repair_threshold=0.65,
        )
    except Exception as ex:
        return {"ok": False, "error": f"Track alignment failed: {ex}"}

    tracks_to_repair: List[Dict[str, Any]] = []
    conflicts_requiring_review: List[Dict[str, Any]] = []
    changes: List[Dict[str, Any]] = []
    unreadable_items: List[Dict[str, Any]] = []

    for expected in alignment.get("tracks") or []:
        matched_item = expected.get("item") or {}
        try:
            iid = int(matched_item.get("id") or 0)
        except Exception:
            iid = 0
        if iid <= 0:
            continue

        current_mbid = str(matched_item.get("mb_trackid") or "").strip().lower()
        target_mbid = str(expected.get("mb_trackid") or "").strip()
        if not target_mbid or current_mbid == target_mbid.lower():
            continue

        item_path = item_paths[iid]

        # Capture the ACTUAL on-disk tag values before any mutation --
        # separate from the Beets DB row read above. SEC-002 Wave 19 final
        # review: rollback must restore what was really on the file, not
        # assume the DB row and the file tags were identical. If the file
        # cannot be read reliably, we cannot honestly promise recovery for
        # it, so this item is excluded from automatic repair rather than
        # applying a mutation we cannot truthfully roll back.
        file_read = _read_file_audio_tags(item_path)
        if not file_read.get("ok"):
            unreadable_items.append({
                "item_id": iid,
                "path": str(item_path),
                "reason": file_read.get("reason"),
            })
            continue

        before_state = {
            "title": str(matched_item.get("title") or ""),
            "artist": album_artist,
            "album": album_title,
            "albumartist": album_artist,
            "disc": int(matched_item.get("disc") or 1),
            "track": int(matched_item.get("track") or 0),
            "mb_trackid": current_mbid,
            "mb_albumid": str(matched_item.get("mb_albumid") or "").strip().lower(),
            "mb_releasegroupid": album_rg,
            "path": str(item_path),
            "filename": item_path.name,
            "stat": item_stats[iid],
            "file_tags": file_read["tags"],
        }
        after_state = {
            "title": str(expected.get("title") or ""),
            "artist": album_artist,
            "album": album_title,
            "albumartist": album_artist,
            "disc": int(expected.get("disc") or 1),
            "track": int(expected.get("track") or 0),
            "mb_trackid": target_mbid,
            "mb_albumid": target_mb_albumid,
            # Apply never writes mb_releasegroupid (RGID stays canonical
            # and is never mutated by this mutation family) -- show it
            # unchanged rather than claiming a change that never happens.
            "mb_releasegroupid": album_rg,
        }
        repair_spec = {
            "item_id": iid,
            "before": before_state,
            "after": after_state,
            "identity_evidence": {
                "source": "musicbrainz_tracklist_alignment",
                "mb_releasegroupid": album_rg,
                "mb_albumid": target_mb_albumid,
                "mb_trackid": target_mbid,
            },
        }
        change_row = {
            "id": iid,
            "track": f"{after_state['disc']}-{after_state['track']} {after_state['title']}",
            "before": before_state,
            "after": after_state,
            "identity_evidence": repair_spec["identity_evidence"],
        }

        if current_mbid:
            # A nonblank Recording ID already on this item is existing
            # identity evidence, not an empty slot. `best_album_track_match`
            # only reaches this branch when the item's OWN Recording ID did
            # NOT match anything in the selected tracklist -- i.e. the
            # match came from title/position/duration fuzzy scoring alone
            # (backend/mb_alignment.album_track_score), which is not
            # sufficient evidence to overwrite an existing identity per
            # project policy ("destructive actions require stronger
            # evidence than suggestions"). Surface for manual review
            # instead of silently repairing it.
            change_row["status"] = "requires_review"
            change_row["review_reason"] = (
                "Existing MusicBrainz Recording ID conflicts with the tracklist-aligned "
                "match; fuzzy title/position/duration evidence alone is not sufficient "
                "to overwrite an established identity."
            )
            conflicts_requiring_review.append(repair_spec)
        else:
            change_row["status"] = "planned"
            tracks_to_repair.append(repair_spec)
        changes.append(change_row)

    # Release-only stamping: every OTHER item in the album (not already
    # covered by a recording-ID repair row above) whose mb_albumid does not
    # match the target release. SEC-002 Wave 19 final review: Apply updates
    # `items.mb_albumid` for the whole album, so Plan must show every row
    # that will change -- not just the recording-ID repair subset -- and
    # rollback must be able to restore each one individually (not just
    # infer the album's original value from the first repaired track).
    repair_item_ids = {t["item_id"] for t in tracks_to_repair}
    album_release_stamp_rows: List[Dict[str, Any]] = []
    if target_mb_albumid:
        for it in items_list:
            iid = it["id"]
            if iid in repair_item_ids:
                continue
            if it.get("mb_albumid") == target_mb_albumid:
                continue
            item_path = item_paths[iid]
            file_read = _read_file_audio_tags(item_path)
            if not file_read.get("ok"):
                unreadable_items.append({
                    "item_id": iid,
                    "path": str(item_path),
                    "reason": file_read.get("reason"),
                })
                continue
            stamp_row = {
                "item_id": iid,
                "before": {
                    "mb_albumid": it.get("mb_albumid") or "",
                    "path": str(item_path),
                    "stat": item_stats[iid],
                    "file_tags": file_read["tags"],
                },
                "after": {"mb_albumid": target_mb_albumid},
            }
            album_release_stamp_rows.append(stamp_row)
            changes.append({
                "id": iid,
                "track": f"release-stamp: {it.get('title') or item_path.name}",
                "before": stamp_row["before"],
                "after": stamp_row["after"],
                "identity_evidence": {
                    "source": "musicbrainz_release_stamp",
                    "mb_albumid": target_mb_albumid,
                },
                "status": "planned",
            })

    album_before = {
        "mb_albumid": str(album_row["mb_albumid"] or "").strip().lower(),
        "mb_releasegroupid": album_rg,
    }
    release_stamping_needed = bool(album_release_stamp_rows) or (
        bool(target_mb_albumid) and target_mb_albumid != album_before["mb_albumid"]
    )

    if not tracks_to_repair and not conflicts_requiring_review and not release_stamping_needed:
        return {
            "ok": True,
            "updated": 0,
            "conflicts": 0,
            "message": "No MusicBrainz recording IDs needed safe repair.",
            "unreadable_items": unreadable_items,
        }

    summary = (
        f"Repair MB track IDs for album {album_id}: {len(tracks_to_repair)} track(s), "
        f"{len(conflicts_requiring_review)} conflict(s) requiring review, "
        f"release stamping {'needed' if release_stamping_needed else 'not needed'}"
    )
    tx_payload = {
        "album_id": album_id,
        "mb_albumid": target_mb_albumid,
        "mb_releasegroupid": album_rg,
        "tracks_to_repair": tracks_to_repair,
        "conflicts_requiring_review": conflicts_requiring_review,
        "album_release_stamp_rows": album_release_stamp_rows,
        "album_before": album_before,
        "release_stamping_needed": release_stamping_needed,
        "unreadable_items": unreadable_items,
    }
    # Rollback is only ever advertised for rows we actually captured full
    # before-state for (DB row AND on-disk file tags); unreadable items
    # were excluded above rather than included with an unrecoverable
    # promise (SEC-002 Wave 19 final review, "rollback availability
    # semantics").
    tx = store.create(
        operation_type="Repair",
        status="Preview",
        summary=summary,
        changes=changes,
        rollback_available=True,
        rollback_reason="Can restore prior Beets DB track/album attributes and on-disk file tags for every repaired row.",
        metadata={
            "mutation_family": "album_mb_track_repair_v1",
            "payload": tx_payload,
            "cancellable": False,
            **tx_payload,
        },
    )
    tx_res = dict(tx)
    tx_res["payload"] = tx_payload
    return {
        "ok": True,
        "operation_id": tx["id"],
        "transaction": tx_res,
        "updated": len(tracks_to_repair),
        "conflicts": len(conflicts_requiring_review),
        "release_stamping_needed": release_stamping_needed,
        "release_stamp_rows": len(album_release_stamp_rows),
        "unreadable_items": unreadable_items,
    }


def execute_album_mb_track_repair_apply(
    store: TransactionStore,
    operation_id: str,
    *,
    db_path: Optional[str] = None,
    music_allowed_roots: Optional[List[str]] = None,
    write_tags: bool = True,
) -> Dict[str, Any]:
    """Execute Apply phase for album_mb_track_repair_v1 (SEC-002 Wave 19).

    `write_tags=False` performs the Beets DB update only and skips the
    on-disk audio tag write stage entirely -- restoring the historical
    `_repair_album_mbid_sticking_once(write_tags=False)` contract that the
    original Wave 19 Apply silently dropped (every file mutation used to be
    unconditional once tracks were repaired).
    """
    op_lock = _get_apply_lock(operation_id)
    with op_lock:
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": "Transaction not found.", "code": "repair_not_found"}

        meta = tx.get("metadata") or {}
        mutation_family = meta.get("mutation_family")
        if mutation_family != "album_mb_track_repair_v1":
            return {"ok": False, "error": "Transaction family mismatch.", "code": "repair_wrong_family"}

        payload = meta.get("payload") or meta or tx.get("payload") or {}
        tracks_to_repair: List[Dict[str, Any]] = payload.get("tracks_to_repair") or []
        album_release_stamp_rows: List[Dict[str, Any]] = payload.get("album_release_stamp_rows") or []
        album_before: Dict[str, Any] = payload.get("album_before") or {}

        if tx.get("status") == "Completed":
            return {
                "ok": True,
                "status": "Completed",
                "already_completed": True,
                "operation_id": operation_id,
                "updated": len(tracks_to_repair),
                "release_stamp_rows": len(album_release_stamp_rows),
            }

        album_id = int(payload.get("album_id") or 0)
        target_mb_albumid = str(payload.get("mb_albumid") or "").strip().lower()

        item_ids = sorted({int(t["item_id"]) for t in tracks_to_repair} | {int(t["item_id"]) for t in album_release_stamp_rows})

        # Resource locking: album lock + every item lock this Apply can
        # touch, in deterministic order (album_mb_track_repair_v1's own
        # rollback acquires the SAME locks -- see rollback_album_mb_track_repair).
        album_res_lock = _get_resource_lock(f"album:{album_id}")
        item_res_locks = [_get_resource_lock(f"item:{iid}") for iid in item_ids]

        with ExitStack() as stack:
            stack.enter_context(album_res_lock)
            for ilock in item_res_locks:
                stack.enter_context(ilock)

            # Re-read transaction state under locks
            tx = store.get(operation_id)
            if tx.get("status") == "Completed":
                return {
                    "ok": True,
                    "status": "Completed",
                    "already_completed": True,
                    "operation_id": operation_id,
                    "updated": len(tracks_to_repair),
                    "release_stamp_rows": len(album_release_stamp_rows),
                }

            lib_db = db_path or os.environ.get("BEETS_DB_PATH", os.path.expanduser("~/.config/beets/musiclibrary.blb"))
            if not os.path.exists(lib_db):
                LOG.error("Repair apply: Beets database not found at %s", lib_db)
                return {"ok": False, "error": "Beets database unavailable.", "code": "db_not_found"}

            roots = music_allowed_roots or [os.environ.get("MUSIC_ROOT", "/music")]
            roots_resolved: List[Path] = []
            for r in roots:
                try:
                    p = Path(r).resolve()
                    if p.exists():
                        roots_resolved.append(p)
                except Exception:
                    pass
            if not roots_resolved:
                roots_resolved = [Path(r) for r in roots]

            def _fail(msg: str, code: str, *, mutated: bool = False) -> Dict[str, Any]:
                store.update(operation_id, status="Failed", logs=[msg])
                result = {"ok": False, "error": msg, "code": code}
                if mutated:
                    result["mutated"] = True
                return result

            def _check_item_toctou(cur: sqlite3.Cursor, iid: int, expected_path: str, expected_stat: Dict[str, Any],
                                    *, expected_mbid: Optional[str] = None, expected_albumid: Optional[str] = None) -> Optional[Dict[str, Any]]:
                cur.execute(
                    "SELECT id, album_id, path, mb_trackid, mb_albumid FROM items WHERE id=?",
                    (iid,),
                )
                irow = cur.fetchone()
                if not irow:
                    return _fail(f"Item no longer exists (id={iid}).", "repair_item_missing")
                if int(irow["album_id"]) != album_id:
                    return _fail(f"Item {iid} no longer belongs to this album.", "repair_album_membership_changed")
                raw_path = irow["path"]
                path_str = raw_path.decode("utf-8", "replace") if isinstance(raw_path, bytes) else str(raw_path)
                if path_str != expected_path:
                    return _fail(f"Item {iid} path changed since plan.", "repair_toctou_mismatch")
                if expected_mbid is not None:
                    live_mbid = str(irow["mb_trackid"] or "").strip().lower()
                    if live_mbid != expected_mbid:
                        return _fail(f"Item {iid} Recording ID changed since plan.", "repair_toctou_mismatch")
                if expected_albumid is not None:
                    live_albumid = str(irow["mb_albumid"] or "").strip().lower()
                    if live_albumid != expected_albumid:
                        return _fail(f"Item {iid} Release ID changed since plan.", "repair_toctou_mismatch")

                item_path = Path(path_str)
                contained = False
                base_root = roots_resolved[0] if roots_resolved else Path(path_str).anchor
                for root in roots_resolved:
                    try:
                        item_path.relative_to(root)
                        contained = True
                        base_root = root
                        break
                    except ValueError:
                        pass
                if not contained:
                    return _fail(f"Item {iid} path outside allowed roots.", "repair_path_out_of_root")
                if _path_has_symlink_under(item_path, base_root):
                    return _fail(f"Item {iid} path contains a symlink component.", "repair_symlink_rejected")
                if not item_path.exists():
                    return _fail(f"Item {iid} file missing.", "repair_item_missing")
                st = item_path.stat()
                if (
                    st.st_dev != expected_stat["dev"]
                    or st.st_ino != expected_stat["ino"]
                    or st.st_size != expected_stat["size"]
                    or st.st_mtime_ns != expected_stat["mtime_ns"]
                ):
                    return _fail(f"Item {iid} file changed since plan.", "repair_toctou_mismatch")
                return None

            # TOCTOU & Precondition Verification (ALL-OR-NOTHING, before
            # any mutation begins).
            try:
                con = sqlite3.connect(lib_db, timeout=10)
                con.row_factory = sqlite3.Row
                try:
                    cur = con.cursor()
                    cur.execute("SELECT id, mb_albumid, mb_releasegroupid FROM albums WHERE id=?", (album_id,))
                    alb_row = cur.fetchone()
                    if not alb_row:
                        return _fail(f"Album {album_id} no longer exists.", "repair_album_missing")

                    expected_rg = str(payload.get("mb_releasegroupid") or "").strip().lower()
                    live_rg = str(alb_row["mb_releasegroupid"] or "").strip().lower()
                    if expected_rg and live_rg and live_rg != expected_rg:
                        return _fail("Album Release Group ID changed since plan.", "repair_identity_mismatch")

                    live_album_mbid = str(alb_row["mb_albumid"] or "").strip().lower()
                    expected_album_mbid = str(album_before.get("mb_albumid") or "").strip().lower()
                    if live_album_mbid != expected_album_mbid:
                        return _fail("Album Release ID changed since plan.", "repair_toctou_mismatch")

                    for spec in tracks_to_repair:
                        iid = int(spec["item_id"])
                        before = spec["before"]
                        failure = _check_item_toctou(
                            cur, iid, before["path"], before["stat"],
                            expected_mbid=before["mb_trackid"], expected_albumid=before.get("mb_albumid"),
                        )
                        if failure:
                            return failure

                    for spec in album_release_stamp_rows:
                        iid = int(spec["item_id"])
                        before = spec["before"]
                        failure = _check_item_toctou(
                            cur, iid, before["path"], before["stat"],
                            expected_albumid=str(before.get("mb_albumid") or "").strip().lower(),
                        )
                        if failure:
                            return failure
                finally:
                    con.close()
            except sqlite3.Error as ex:
                LOG.error("Repair apply: TOCTOU verification query failed for op %s: %s", operation_id, ex)
                return _fail("Precondition verification failed.", "repair_verification_failed")

            # Mutation is about to begin. `mutation_started` records intent
            # durably before the first statement executes; `mutated` is
            # only ever set True immediately after a mutation is actually
            # committed (see Stage 1 below) -- SEC-002 Wave 19 final
            # review: the previous implementation set `mutated: True`
            # before Stage 1 even opened a connection, so a failure before
            # the first successful commit still left the transaction
            # falsely advertising itself as mutated/rollback-eligible.
            store.update(operation_id, status="Running", metadata={**meta, "mutation_started": True})

            def _persist_step(step_name: str, step_status: str, **extra: Any) -> None:
                curr_tx = store.get(operation_id)
                steps = curr_tx.get("metadata", {}).get("apply_steps", [])
                steps.append({
                    "step": step_name,
                    "status": step_status,
                    "timestamp": _now(),
                    **extra,
                })
                store.update(operation_id, metadata={**curr_tx.get("metadata", {}), "apply_steps": steps})

            # Stage 1: Database updates -- exact rowcount verified for
            # every row, and the whole stage rolls back atomically (single
            # connection, single commit) if any expected row is missed.
            try:
                con = sqlite3.connect(lib_db, timeout=10)
                try:
                    cur = con.cursor()
                    for spec in tracks_to_repair:
                        after = spec["after"]
                        cur.execute(
                            "UPDATE items SET mb_trackid=?, track=?, disc=?, title=?, mb_albumid=? WHERE id=?",
                            (after["mb_trackid"], after["track"], after["disc"], after["title"], after["mb_albumid"], spec["item_id"]),
                        )
                        if cur.rowcount != 1:
                            con.rollback()
                            return _fail(f"Expected to update exactly 1 row for item {spec['item_id']}, updated {cur.rowcount}.", "repair_rowcount_mismatch")

                    for spec in album_release_stamp_rows:
                        cur.execute(
                            "UPDATE items SET mb_albumid=? WHERE id=?",
                            (spec["after"]["mb_albumid"], spec["item_id"]),
                        )
                        if cur.rowcount != 1:
                            con.rollback()
                            return _fail(f"Expected to update exactly 1 row for item {spec['item_id']}, updated {cur.rowcount}.", "repair_rowcount_mismatch")

                    if target_mb_albumid:
                        cur.execute("UPDATE albums SET mb_albumid=? WHERE id=?", (target_mb_albumid, album_id))
                        if cur.rowcount != 1:
                            con.rollback()
                            return _fail(f"Expected to update exactly 1 album row, updated {cur.rowcount}.", "repair_rowcount_mismatch")

                    con.commit()
                finally:
                    con.close()
                # This durably records that a real DB mutation was
                # committed -- the truthful point at which rollback
                # becomes meaningful for the DB side of this transaction.
                store.update(operation_id, metadata={**store.get(operation_id).get("metadata", {}), "db_mutated": True, "mutated": True})
                _persist_step("db_updated", "Completed")
            except sqlite3.Error as ex:
                LOG.error("Repair apply: database update failed for op %s: %s", operation_id, ex)
                return _fail("Database update failed.", "repair_apply_failed")

            # Stage 2: Audio tag writes (skipped entirely when
            # write_tags=False -- DB-only maintenance mode).
            failed_tag_writes: List[Dict[str, Any]] = []
            if write_tags:
                for spec in tracks_to_repair:
                    iid = spec["item_id"]
                    item_path = Path(spec["before"]["path"])
                    after = spec["after"]
                    tags_to_write = {
                        "mb_trackid": after["mb_trackid"],
                        "mb_albumid": after["mb_albumid"],
                        "title": after["title"],
                        "track": after["track"],
                        "disc": after["disc"],
                    }
                    result = _write_file_audio_tags(item_path, tags_to_write)
                    if result.get("ok"):
                        _persist_step("tags_applied", "Completed", item_id=iid)
                    else:
                        failed_tag_writes.append({"item_id": iid, "reason": result.get("reason")})
                        _persist_step("tags_applied", "Failed", item_id=iid, reason=result.get("reason"))

                for spec in album_release_stamp_rows:
                    iid = spec["item_id"]
                    item_path = Path(spec["before"]["path"])
                    result = _write_file_audio_tags(item_path, {"mb_albumid": spec["after"]["mb_albumid"]})
                    if result.get("ok"):
                        _persist_step("release_tag_applied", "Completed", item_id=iid)
                    else:
                        failed_tag_writes.append({"item_id": iid, "reason": result.get("reason")})
                        _persist_step("release_tag_applied", "Failed", item_id=iid, reason=result.get("reason"))

            if failed_tag_writes:
                failed_ids = [f["item_id"] for f in failed_tag_writes]
                curr_meta = store.get(operation_id).get("metadata", {})
                store.update(
                    operation_id,
                    status="Failed",
                    logs=[f"Audio tag writes failed for {len(failed_tag_writes)} item(s); DB was already committed -- rollback is available."],
                    metadata={**curr_meta, "failed_tag_writes": failed_tag_writes, "partial_mutation": True},
                )
                LOG.error("Repair apply: tag writes failed for op %s items=%s", operation_id, failed_ids)
                return {
                    "ok": False,
                    "error": f"Audio tag writes failed for {len(failed_tag_writes)} item(s). The database was already updated; rollback is available.",
                    "code": "repair_apply_failed",
                    "mutated": True,
                    "partial_mutation": True,
                    "failed_items": failed_ids,
                }

            if write_tags:
                store.update(operation_id, metadata={**store.get(operation_id).get("metadata", {}), "tags_mutated": True})

            # Stage 3: Post-write DB verification -- every field this
            # transaction claims to have changed, for every row (not just
            # mb_trackid).
            try:
                con = sqlite3.connect(lib_db, timeout=10)
                con.row_factory = sqlite3.Row
                try:
                    cur = con.cursor()
                    for spec in tracks_to_repair:
                        iid = spec["item_id"]
                        after = spec["after"]
                        cur.execute("SELECT mb_trackid, mb_albumid, title, track, disc FROM items WHERE id=?", (iid,))
                        row = cur.fetchone()
                        if not row:
                            return _fail(f"Post-write verification failed for item {iid}: row missing.", "repair_verification_failed", mutated=True)
                        mismatches = []
                        if str(row["mb_trackid"] or "").strip().lower() != after["mb_trackid"].lower():
                            mismatches.append("mb_trackid")
                        if str(row["mb_albumid"] or "").strip().lower() != after["mb_albumid"]:
                            mismatches.append("mb_albumid")
                        if str(row["title"] or "") != after["title"]:
                            mismatches.append("title")
                        if int(row["track"] or 0) != int(after["track"]):
                            mismatches.append("track")
                        if int(row["disc"] or 0) != int(after["disc"]):
                            mismatches.append("disc")
                        if mismatches:
                            LOG.error("Repair apply: post-write DB mismatch for item %s fields=%s", iid, mismatches)
                            return _fail(f"Post-write verification failed for item {iid}.", "repair_verification_failed", mutated=True)

                    for spec in album_release_stamp_rows:
                        iid = spec["item_id"]
                        cur.execute("SELECT mb_albumid FROM items WHERE id=?", (iid,))
                        row = cur.fetchone()
                        if not row or str(row["mb_albumid"] or "").strip().lower() != spec["after"]["mb_albumid"]:
                            return _fail(f"Post-write verification failed for item {iid}.", "repair_verification_failed", mutated=True)

                    if target_mb_albumid:
                        cur.execute("SELECT mb_albumid FROM albums WHERE id=?", (album_id,))
                        arow = cur.fetchone()
                        if not arow or str(arow["mb_albumid"] or "").strip().lower() != target_mb_albumid:
                            return _fail("Post-write verification failed for album row.", "repair_verification_failed", mutated=True)
                finally:
                    con.close()
                _persist_step("result_verified", "Completed")
            except sqlite3.Error as ex:
                LOG.error("Repair apply: verification query failed for op %s: %s", operation_id, ex)
                return _fail("Post-write verification query failed.", "repair_verification_failed", mutated=True)

            store.update(operation_id, status="Completed", logs=["Repair apply completed successfully."])
            return {
                "ok": True,
                "status": "Completed",
                "operation_id": operation_id,
                "updated": len(tracks_to_repair),
                "release_stamp_rows": len(album_release_stamp_rows),
                "tags_written": bool(write_tags),
            }


def rollback_album_mb_track_repair(
    store: TransactionStore,
    operation_id: str,
    *,
    db_path: Optional[str] = None,
    music_allowed_roots: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Execute rollback for album_mb_track_repair_v1 (SEC-002 Wave 19)."""
    op_lock = _get_apply_lock(operation_id)
    with op_lock:
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": "Transaction not found.", "code": "repair_not_found"}

        meta = tx.get("metadata") or {}
        mutation_family = meta.get("mutation_family")
        if mutation_family != "album_mb_track_repair_v1":
            return {"ok": False, "error": "Transaction family mismatch.", "code": "repair_wrong_family"}

        status = tx.get("status")
        if status in ("Rolled Back",):
            return {"ok": False, "error": "This transaction has already been rolled back.", "code": "repair_already_rolled_back"}
        db_mutated = bool(meta.get("db_mutated") or meta.get("mutated"))
        if status not in ("Completed", "Failed", "Partially Rolled Back") or not db_mutated:
            return {"ok": False, "error": "Rollback unavailable: transaction has not performed any mutations.", "code": "repair_not_mutated"}

        payload = meta.get("payload") or meta or tx.get("payload") or {}
        album_id = int(payload.get("album_id") or 0)
        tracks_to_repair: List[Dict[str, Any]] = payload.get("tracks_to_repair") or []
        album_release_stamp_rows: List[Dict[str, Any]] = payload.get("album_release_stamp_rows") or []
        album_before: Dict[str, Any] = payload.get("album_before") or {}
        tags_mutated = bool(meta.get("tags_mutated"))

        item_ids = sorted({int(t["item_id"]) for t in tracks_to_repair} | {int(t["item_id"]) for t in album_release_stamp_rows})

        # Same resource-locking discipline as Apply (album lock + every
        # item lock, deterministic order) -- SEC-002 Wave 19 final review:
        # the previous rollback only took the operation-id lock, so a
        # concurrent Apply of a DIFFERENT transaction on the same
        # album/items could race with this rollback.
        album_res_lock = _get_resource_lock(f"album:{album_id}")
        item_res_locks = [_get_resource_lock(f"item:{iid}") for iid in item_ids]

        with ExitStack() as stack:
            stack.enter_context(album_res_lock)
            for ilock in item_res_locks:
                stack.enter_context(ilock)

            lib_db = db_path or os.environ.get("BEETS_DB_PATH", os.path.expanduser("~/.config/beets/musiclibrary.blb"))
            if not os.path.exists(lib_db):
                LOG.error("Repair rollback: Beets database not found at %s", lib_db)
                return {"ok": False, "error": "Beets database unavailable.", "code": "db_not_found"}

            roots = music_allowed_roots or [os.environ.get("MUSIC_ROOT", "/music")]
            roots_resolved: List[Path] = []
            for r in roots:
                try:
                    p = Path(r).resolve()
                    if p.exists():
                        roots_resolved.append(p)
                except Exception:
                    pass
            if not roots_resolved:
                roots_resolved = [Path(r) for r in roots]

            def _path_ok(item_path: Path) -> bool:
                base_root = roots_resolved[0] if roots_resolved else item_path.anchor
                contained = False
                for root in roots_resolved:
                    try:
                        item_path.relative_to(root)
                        contained = True
                        base_root = root
                        break
                    except ValueError:
                        pass
                if not contained:
                    return False
                if _path_has_symlink_under(item_path, base_root):
                    return False
                return True

            # Precondition: current DB state must still match what THIS
            # transaction actually wrote (its "after" state), unless it
            # never got that far (a partial DB-only failure). Refuse to
            # overwrite state that a later, unrelated edit produced --
            # SEC-002 Wave 19 final review, "rollback must validate
            # current state before overwriting it".
            try:
                con = sqlite3.connect(lib_db, timeout=10)
                con.row_factory = sqlite3.Row
                try:
                    cur = con.cursor()
                    for spec in tracks_to_repair:
                        iid = int(spec["item_id"])
                        after = spec["after"]
                        cur.execute("SELECT mb_trackid, mb_albumid, path FROM items WHERE id=?", (iid,))
                        row = cur.fetchone()
                        if not row:
                            store.update(operation_id, logs=[f"Rollback precondition failed: item {iid} no longer exists."])
                            return {"ok": False, "error": f"Item {iid} no longer exists.", "code": "repair_rollback_precondition_failed"}
                        live_mbid = str(row["mb_trackid"] or "").strip().lower()
                        if live_mbid != after["mb_trackid"].lower():
                            store.update(operation_id, logs=[f"Rollback precondition failed: item {iid} was modified after this repair."])
                            return {"ok": False, "error": f"Item {iid} was modified after this repair; refusing to overwrite a later change.", "code": "repair_rollback_stale"}
                    for spec in album_release_stamp_rows:
                        iid = int(spec["item_id"])
                        cur.execute("SELECT mb_albumid FROM items WHERE id=?", (iid,))
                        row = cur.fetchone()
                        if not row:
                            store.update(operation_id, logs=[f"Rollback precondition failed: item {iid} no longer exists."])
                            return {"ok": False, "error": f"Item {iid} no longer exists.", "code": "repair_rollback_precondition_failed"}
                        live_albumid = str(row["mb_albumid"] or "").strip().lower()
                        if live_albumid != spec["after"]["mb_albumid"]:
                            store.update(operation_id, logs=[f"Rollback precondition failed: item {iid} was modified after this repair."])
                            return {"ok": False, "error": f"Item {iid} was modified after this repair; refusing to overwrite a later change.", "code": "repair_rollback_stale"}
                finally:
                    con.close()
            except sqlite3.Error as ex:
                LOG.error("Repair rollback: precondition query failed for op %s: %s", operation_id, ex)
                return {"ok": False, "error": "Rollback precondition check failed.", "code": "repair_verification_failed"}

            db_restored = 0
            db_failed_ids: List[int] = []
            try:
                con = sqlite3.connect(lib_db, timeout=10)
                try:
                    cur = con.cursor()
                    for spec in tracks_to_repair:
                        iid = spec["item_id"]
                        before = spec["before"]
                        cur.execute(
                            "UPDATE items SET mb_trackid=?, track=?, disc=?, title=?, mb_albumid=? WHERE id=?",
                            (before["mb_trackid"], before["track"], before["disc"], before["title"], before["mb_albumid"], iid),
                        )
                        if cur.rowcount == 1:
                            db_restored += 1
                        else:
                            db_failed_ids.append(iid)

                    for spec in album_release_stamp_rows:
                        iid = spec["item_id"]
                        before = spec["before"]
                        cur.execute("UPDATE items SET mb_albumid=? WHERE id=?", (before["mb_albumid"], iid))
                        if cur.rowcount == 1:
                            db_restored += 1
                        else:
                            db_failed_ids.append(iid)

                    # Restore the album's own original value -- captured
                    # explicitly at Plan time, not inferred from the first
                    # repaired track (which is not necessarily the same
                    # value the album row itself held before Apply).
                    if album_before.get("mb_albumid") is not None:
                        cur.execute("UPDATE albums SET mb_albumid=? WHERE id=?", (album_before["mb_albumid"], album_id))
                    con.commit()
                finally:
                    con.close()
            except sqlite3.Error as ex:
                LOG.error("Repair rollback: database restore failed for op %s: %s", operation_id, ex)
                store.update(operation_id, logs=["Rollback database restore failed."])
                return {"ok": False, "error": "Rollback database restore failed.", "code": "repair_rollback_db_failed"}

            tags_restored = 0
            tags_failed_ids: List[int] = []
            if tags_mutated:
                for spec in tracks_to_repair + album_release_stamp_rows:
                    iid = spec["item_id"]
                    before = spec["before"]
                    item_path = Path(before["path"])
                    file_before = before.get("file_tags")
                    if not file_before:
                        # No trustworthy on-disk snapshot was captured for
                        # this row (should not happen -- Plan excludes
                        # unreadable files) -- do not guess.
                        tags_failed_ids.append(iid)
                        continue
                    if not _path_ok(item_path) or not item_path.exists():
                        tags_failed_ids.append(iid)
                        continue
                    tags_to_write = {k: file_before.get(k) for k in _AUDIO_TAG_FIELDS if k in file_before}
                    result = _write_file_audio_tags(item_path, tags_to_write)
                    if result.get("ok"):
                        tags_restored += 1
                    else:
                        tags_failed_ids.append(iid)

            total_rows = len(tracks_to_repair) + len(album_release_stamp_rows)
            full_success = (not db_failed_ids) and (not tags_failed_ids)
            any_restored = db_restored > 0 or tags_restored > 0
            final_status = "Rolled Back" if full_success else ("Partially Rolled Back" if any_restored else "Failed")

            store.update(
                operation_id,
                status=final_status,
                logs=[f"Album MB track repair rollback completed with status {final_status}."],
                metadata={
                    **meta,
                    "rollback_available": False,
                    "db_restored_count": db_restored,
                    "tags_restored_count": tags_restored,
                    "db_failed_items": db_failed_ids,
                    "tags_failed_items": tags_failed_ids,
                },
            )
            return {
                "ok": final_status in ("Rolled Back", "Partially Rolled Back"),
                "operation_id": operation_id,
                "status": final_status,
                "db_restored_count": db_restored,
                "tags_restored_count": tags_restored,
                "total_rows": total_rows,
            }


# ──────────────────────────────────────────────────────────────────────────────
# SEC-002 / ARCH-003 Wave 20: existing_album_reconcile_v1
# ──────────────────────────────────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Capture a full sqlite3.Row (every column, whatever the live Beets
    schema actually has) as a plain, JSON-safe dict. SEC-002 Wave 20 final
    review: rollback that only restores a hand-picked field subset can
    silently lose real Beets metadata (genre, year, comments, flexible
    attributes, ...) -- capturing every column at Plan time and restoring
    the same set is schema-agnostic and does not need updating every time
    a new Beets column/plugin field is added.
    """
    out: Dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        out[key] = value
    return out


def _resolve_path_role(path_str: str, roots: List[Path]) -> Optional[Path]:
    """Return the resolved root a path is contained under, or None."""
    try:
        item_path = Path(path_str)
    except Exception:
        return None
    for root in roots:
        try:
            item_path.relative_to(root)
            return root
        except ValueError:
            continue
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


def create_existing_album_reconcile_plan(
    store: TransactionStore,
    payload: Dict[str, Any],
    *,
    music_allowed_roots: Optional[List[str]] = None,
    staging_allowed_roots: Optional[List[str]] = None,
    quarantine_base_root: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a plan for existing-album duplicate & move reconciliation (SEC-002 Wave 20).

    Role-specific roots (SEC-002 Wave 20 final review, "critical path
    authorization defect"): a caller-supplied `source_folder` must never be
    able to expand what filesystem locations this operation is authorized
    to touch. `source_folder` is only ever used to CLASSIFY an already
    root-validated duplicate path as "staging" (for logging/UI purposes);
    it is validated against the server's own configured staging roots
    before being trusted for that, and is never added to the containment
    check itself. Survivors must always be under the music library root;
    move/duplicate items (already-imported Beets rows) may be under the
    library root or a validated staging root.
    """
    imported_album_id = int(payload.get("imported_album_id") or 0)
    existing_album_id = int(payload.get("existing_album_id") or 0)
    if imported_album_id <= 0 or existing_album_id <= 0 or imported_album_id == existing_album_id:
        return {"ok": False, "error": "Invalid album IDs for existing album reconciliation", "code": "reconcile_invalid_album"}

    raw_dup_ids = [int(v) for v in (payload.get("dup_item_ids") or []) if int(v or 0) > 0]
    raw_move_ids = [int(v) for v in (payload.get("move_item_ids") or []) if int(v or 0) > 0]

    # SEC-002 Wave 20 final review: canonicalize payload item-ID lists --
    # [1, 1, 1] is not three affected rows, and the same item cannot be
    # simultaneously a "move" and a "duplicate". Reject rather than
    # silently dedup, so a malformed caller payload fails loudly instead
    # of quietly processing a different set of rows than it asked for.
    if len(set(raw_dup_ids)) != len(raw_dup_ids):
        return {"ok": False, "error": "Duplicate item IDs repeated within dup_item_ids", "code": "reconcile_payload_overlap"}
    if len(set(raw_move_ids)) != len(raw_move_ids):
        return {"ok": False, "error": "Duplicate item IDs repeated within move_item_ids", "code": "reconcile_payload_overlap"}
    if set(raw_dup_ids) & set(raw_move_ids):
        return {"ok": False, "error": "Item IDs cannot appear in both dup_item_ids and move_item_ids", "code": "reconcile_payload_overlap"}

    dup_item_ids = raw_dup_ids
    dup_details: List[Dict[str, Any]] = payload.get("dup_details") or []
    dup_details_map = {int(d.get("dup_item_id") or 0): d for d in dup_details if int(d.get("dup_item_id") or 0) > 0}

    move_item_ids = raw_move_ids

    if not dup_item_ids and not move_item_ids:
        return {"ok": False, "error": "No duplicate or move items specified", "code": "reconcile_empty_payload"}

    lib_db = db_path or os.environ.get("BEETS_DB_PATH", os.path.expanduser("~/.config/beets/musiclibrary.blb"))
    if not os.path.exists(lib_db):
        LOG.error("Reconcile plan: Beets database not found at %s", lib_db)
        return {"ok": False, "error": "Beets database unavailable.", "code": "db_not_found"}

    def _resolved(paths: Iterable[str]) -> List[Path]:
        out: List[Path] = []
        for r in paths:
            try:
                p = Path(r).resolve()
                if p.exists():
                    out.append(p)
            except Exception:
                pass
        return out

    library_roots = _resolved(music_allowed_roots or [os.environ.get("MUSIC_ROOT", "/music")])
    if not library_roots:
        library_roots = [Path(r) for r in (music_allowed_roots or [os.environ.get("MUSIC_ROOT", "/music")])]

    staging_roots = _resolved(staging_allowed_roots or [])

    # Only trust source_folder for classification once it is itself
    # contained under a SERVER-configured staging root -- an arbitrary
    # client-supplied directory is never authorized on its own. This is a
    # pure string/lexical check (os.path.normpath, no filesystem access)
    # against already-server-resolved roots -- the raw client-supplied
    # string is never itself passed to a filesystem-touching call
    # (Path.resolve()/exists()/etc.), so there is no path-injection sink
    # to reach even for an unvalidated value (CodeQL py/path-injection).
    source_folder_str = str(payload.get("source_folder") or "").strip()
    source_root_validated: Optional[Path] = None
    if source_folder_str and staging_roots:
        normalized = os.path.normpath(source_folder_str)
        for sroot in staging_roots:
            sroot_str = str(sroot)
            if normalized == sroot_str or normalized.startswith(sroot_str + os.sep):
                source_root_validated = sroot / os.path.relpath(normalized, sroot_str) if normalized != sroot_str else sroot
                break

    # imported_item_roots: what move/duplicate items (already Beets rows
    # under the imported album) may legitimately live under.
    imported_item_roots = library_roots + staging_roots
    if not imported_item_roots:
        imported_item_roots = library_roots

    con = sqlite3.connect(lib_db, timeout=10)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute("SELECT * FROM albums WHERE id=?", (existing_album_id,))
    existing_alb_row = cur.fetchone()
    if not existing_alb_row:
        con.close()
        return {"ok": False, "error": f"Target existing album {existing_album_id} not found", "code": "reconcile_album_missing"}
    existing_alb = _row_to_dict(existing_alb_row)

    cur.execute("SELECT * FROM albums WHERE id=?", (imported_album_id,))
    imported_alb_row = cur.fetchone()
    if not imported_alb_row:
        con.close()
        return {"ok": False, "error": f"Source imported album {imported_album_id} not found", "code": "reconcile_album_missing"}
    imported_alb = _row_to_dict(imported_alb_row)

    existing_rg = str(existing_alb.get("mb_releasegroupid") or "").strip().lower()
    imported_rg = str(imported_alb.get("mb_releasegroupid") or "").strip().lower()
    if existing_rg and imported_rg and existing_rg != imported_rg:
        con.close()
        return {"ok": False, "error": f"Cannot reconcile albums with different Release Group IDs ({imported_rg} vs {existing_rg})", "code": "reconcile_identity_mismatch"}

    changes: List[Dict[str, Any]] = []
    move_specs: List[Dict[str, Any]] = []
    dup_specs: List[Dict[str, Any]] = []
    dup_requires_review: List[Dict[str, Any]] = []

    # Validate move_item_ids
    for iid in move_item_ids:
        cur.execute("SELECT * FROM items WHERE id=?", (iid,))
        item_row = cur.fetchone()
        if not item_row:
            con.close()
            return {"ok": False, "error": f"Move item {iid} not found", "code": "reconcile_item_missing"}
        row = _row_to_dict(item_row)
        if int(row.get("album_id") or 0) != imported_album_id:
            con.close()
            return {"ok": False, "error": f"Move item {iid} does not belong to imported album {imported_album_id}", "code": "reconcile_album_membership_changed"}

        path_str = str(row.get("path") or "")
        item_path = Path(path_str)

        base_root = _resolve_path_role(path_str, imported_item_roots)
        if base_root is None:
            con.close()
            return {"ok": False, "error": f"Move item {iid} path outside allowed roots", "code": "reconcile_path_out_of_root"}

        if _path_has_symlink_under(item_path, base_root):
            con.close()
            return {"ok": False, "error": f"Move item {iid} path contains symlink", "code": "reconcile_symlink_rejected"}

        if not item_path.exists():
            con.close()
            return {"ok": False, "error": f"Move item {iid} file missing on disk", "code": "reconcile_item_missing"}

        st = item_path.stat()
        stat_snapshot = {
            "dev": st.st_dev,
            "ino": st.st_ino,
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
        }

        spec = {
            "item_id": iid,
            "before": {
                "album_id": imported_album_id,
                "album": str(row.get("album") or ""),
                "albumartist": str(row.get("albumartist") or ""),
                "path": path_str,
                "stat": stat_snapshot,
                # Identity fields captured for TOCTOU re-verification at
                # Apply only -- Apply does not restore these, it only
                # confirms they have not changed since Plan (SEC-002
                # Wave 20 final review, "item identity TOCTOU is missing").
                "mb_trackid": str(row.get("mb_trackid") or "").strip().lower(),
                "mb_albumid": str(row.get("mb_albumid") or "").strip().lower(),
                "mb_releasegroupid": str(row.get("mb_releasegroupid") or "").strip().lower(),
                "disc": int(row.get("disc") or 1),
                "track": int(row.get("track") or 0),
            },
            "after": {
                "album_id": existing_album_id,
                "album": str(existing_alb.get("album") or ""),
                "albumartist": str(existing_alb.get("albumartist") or ""),
            },
        }
        move_specs.append(spec)
        changes.append({
            "id": f"item:{iid}",
            "operation": "Reassign Album",
            "item_id": iid,
            "old_album_id": imported_album_id,
            "new_album_id": existing_album_id,
            "title": str(row.get("title") or ""),
            "path": path_str,
        })

    # Validate dup_item_ids
    for iid in dup_item_ids:
        cur.execute("SELECT * FROM items WHERE id=?", (iid,))
        item_row = cur.fetchone()
        if not item_row:
            con.close()
            return {"ok": False, "error": f"Duplicate item {iid} not found", "code": "reconcile_item_missing"}
        row = _row_to_dict(item_row)
        if int(row.get("album_id") or 0) != imported_album_id:
            con.close()
            return {"ok": False, "error": f"Duplicate item {iid} does not belong to imported album {imported_album_id}", "code": "reconcile_album_membership_changed"}

        path_str = str(row.get("path") or "")
        item_path = Path(path_str)

        base_root = _resolve_path_role(path_str, imported_item_roots)
        if base_root is None:
            con.close()
            return {"ok": False, "error": f"Duplicate item {iid} path outside allowed roots", "code": "reconcile_path_out_of_root"}

        if _path_has_symlink_under(item_path, base_root):
            con.close()
            return {"ok": False, "error": f"Duplicate item {iid} path contains symlink", "code": "reconcile_symlink_rejected"}

        if not item_path.exists():
            con.close()
            return {"ok": False, "error": f"Duplicate item {iid} file missing on disk", "code": "reconcile_item_missing"}

        st = item_path.stat()
        stat_snapshot = {
            "dev": st.st_dev,
            "ino": st.st_ino,
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
        }
        dup_mbid = str(row.get("mb_trackid") or "").strip().lower()

        # Candidate survivors: caller-supplied IDs are a HINT only, never
        # authority (SEC-002 Wave 20 final review, "do not trust
        # caller-supplied survivor_item_ids"). Same disc/track in the
        # target album is also only a hint -- position alone never
        # authorizes destructive retirement (finding #6).
        detail = dup_details_map.get(iid) or {}
        candidate_survivor_ids = [int(v) for v in (detail.get("survivor_item_ids") or []) if int(v or 0) > 0]
        disc = int(row.get("disc") or 1)
        track = int(row.get("track") or 0)
        if track > 0:
            cur.execute("SELECT id FROM items WHERE album_id=? AND disc=? AND track=?", (existing_album_id, disc, track))
            candidate_survivor_ids.extend(int(r["id"]) for r in cur.fetchall())
        candidate_survivor_ids = sorted(set(candidate_survivor_ids))

        if not candidate_survivor_ids:
            dup_requires_review.append({"item_id": iid, "path": path_str, "reason": "no_candidate_survivor"})
            continue

        # Independently establish, per candidate, whether it is actually a
        # valid survivor: must exist, belong to the target album, be
        # physically present AND readable (not merely Path.exists()), and
        # -- the core identity requirement this review closes -- must
        # carry the SAME Recording ID as the duplicate when both are known.
        # A different known Recording ID fails closed for that candidate.
        # Blank on either side is not enough evidence for automatic
        # retirement either; such duplicates are excluded from dup_specs
        # and surfaced for manual review instead of being silently
        # skipped or silently approved.
        identity_verified_survivors: List[Dict[str, Any]] = []
        conflicting_candidates: List[int] = []
        for s_id in candidate_survivor_ids:
            cur.execute("SELECT * FROM items WHERE id=?", (s_id,))
            s_item_row = cur.fetchone()
            if not s_item_row:
                continue
            s_row = _row_to_dict(s_item_row)
            if int(s_row.get("album_id") or 0) != existing_album_id:
                continue
            s_path_str = str(s_row.get("path") or "")
            s_path = Path(s_path_str)
            same_path = (s_path_str == path_str)

            if not same_path:
                # Same-path is a structural fact -- the duplicate row and
                # this candidate are definitionally the same file, so no
                # amount of Recording-ID/readability evidence could make
                # this MORE certain. Every other case still needs the full
                # identity + physical-usability gate below.
                if _resolve_path_role(s_path_str, library_roots) is None:
                    continue
                if _path_has_symlink_under(s_path, library_roots[0] if library_roots else s_path):
                    continue
                if not s_path.exists():
                    continue
                s_read = _read_file_audio_tags(s_path)
                if not s_read.get("ok"):
                    # Survivor media is not verifiably usable (unreadable/
                    # corrupt/zero-byte) -- SEC-002 Wave 20 final review,
                    # "survivor must be readable / valid media". Do not
                    # authorize deleting the duplicate on the strength of a
                    # survivor we cannot actually confirm is good audio.
                    continue

            s_st = s_path.stat()
            s_mbid = str(s_row.get("mb_trackid") or "").strip().lower()

            if not same_path:
                if dup_mbid and s_mbid:
                    if dup_mbid != s_mbid:
                        conflicting_candidates.append(s_id)
                        continue
                else:
                    # Blank on one or both sides -- not enough evidence on
                    # its own to authorize automatic retirement, and not a
                    # hard conflict either. Skip this candidate; if no
                    # candidate is ever added, the duplicate is surfaced
                    # for manual review below (survivor_unverified) rather
                    # than guessed either way.
                    continue

            is_hardlink = (not same_path) and (s_st.st_dev == st.st_dev and s_st.st_ino == st.st_ino)

            identity_verified_survivors.append({
                "item_id": s_id,
                "path": s_path_str,
                "title": str(s_row.get("title") or ""),
                "mb_trackid": s_mbid,
                "mb_albumid": str(s_row.get("mb_albumid") or "").strip().lower(),
                "mb_releasegroupid": str(s_row.get("mb_releasegroupid") or "").strip().lower(),
                "stat": {"dev": s_st.st_dev, "ino": s_st.st_ino, "size": s_st.st_size, "mtime_ns": s_st.st_mtime_ns},
                "same_path_as_duplicate": same_path,
                "is_hardlink": is_hardlink,
                "identity_exact_match": bool(dup_mbid and s_mbid and dup_mbid == s_mbid),
            })

        if conflicting_candidates and not identity_verified_survivors:
            # Every candidate had a DIFFERENT known Recording ID -- fail
            # closed for this duplicate rather than guessing.
            dup_requires_review.append({
                "item_id": iid, "path": path_str, "reason": "identity_conflict",
                "conflicting_survivor_ids": conflicting_candidates,
            })
            continue

        if not identity_verified_survivors:
            dup_requires_review.append({"item_id": iid, "path": path_str, "reason": "survivor_unverified"})
            continue

        # Classify physical handling: same-path rows must never be
        # physically touched (the "duplicate" and the "survivor" are the
        # same file on disk -- unlinking it would destroy the survivor
        # too). Hardlinks and distinct files are both safely recoverable
        # via quarantine (see Apply).
        any_same_path = any(s["same_path_as_duplicate"] for s in identity_verified_survivors)
        any_hardlink = any(s["is_hardlink"] for s in identity_verified_survivors)
        physical_class = "db_only" if any_same_path else ("hardlink" if any_hardlink else "distinct_file")

        spec = {
            "item_id": iid,
            "before": {
                "album_id": imported_album_id,
                "title": str(row.get("title") or ""),
                "artist": str(row.get("artist") or ""),
                "album": str(row.get("album") or ""),
                "albumartist": str(row.get("albumartist") or ""),
                "disc": disc,
                "track": track,
                "path": path_str,
                "mb_trackid": dup_mbid,
                "mb_albumid": str(row.get("mb_albumid") or "").strip().lower(),
                "mb_releasegroupid": str(row.get("mb_releasegroupid") or "").strip().lower(),
                "stat": stat_snapshot,
                "full_row": row,
            },
            "survivors": identity_verified_survivors,
            "physical_class": physical_class,
            "identity_evidence": {
                "duplicate_mb_trackid": dup_mbid,
                "survivor_mb_trackids": [s["mb_trackid"] for s in identity_verified_survivors],
                "exact_match": any(s["identity_exact_match"] for s in identity_verified_survivors),
                "source": "recording_id_exact" if dup_mbid else "position_only_unverified_identity",
            },
        }
        dup_specs.append(spec)
        changes.append({
            "id": f"item:{iid}",
            "operation": "Delete Duplicate Item",
            "item_id": iid,
            "path": path_str,
            "survivors": [s["item_id"] for s in identity_verified_survivors],
            "physical_class": physical_class,
            "identity_evidence": spec["identity_evidence"],
        })

    # Check if imported album will become empty -- exact set logic, not a
    # count heuristic (SEC-002 Wave 20 final review, finding #26: a count
    # comparison can be fooled by overlap/duplication in the affected ID
    # sets; canonicalization above already rejects that, but comparing the
    # actual affected ID set against actual current membership is the
    # only version of this check that cannot be wrong).
    cur.execute("SELECT id FROM items WHERE album_id=?", (imported_album_id,))
    current_imported_ids = {int(r["id"]) for r in cur.fetchall()}
    affected_ids = {m["item_id"] for m in move_specs} | {d["item_id"] for d in dup_specs}
    will_retire_album = bool(current_imported_ids) and affected_ids >= current_imported_ids

    album_before_snapshot = None
    if will_retire_album:
        album_before_snapshot = imported_alb
        changes.append({
            "id": f"album:{imported_album_id}",
            "operation": "Retire Empty Album",
            "album_id": imported_album_id,
        })

    con.close()

    if not move_specs and not dup_specs:
        return {
            "ok": True,
            "reconciled_moves": 0,
            "reconciled_duplicates": 0,
            "dup_requires_review": dup_requires_review,
            "message": "No move or duplicate items could be safely reconciled.",
        }

    tx_payload = {
        "imported_album_id": imported_album_id,
        "existing_album_id": existing_album_id,
        "source_folder": str(source_root_validated) if source_root_validated else "",
        "move_specs": move_specs,
        "dup_specs": dup_specs,
        "dup_requires_review": dup_requires_review,
        "retire_imported_album": will_retire_album,
        "imported_album_before": album_before_snapshot,
        "existing_album_rg_before": existing_rg,
        "imported_album_rg_before": imported_rg,
    }

    summary = (
        f"Reconcile imported album {imported_album_id} into existing album {existing_album_id}: "
        f"{len(move_specs)} move(s), {len(dup_specs)} duplicate(s), {len(dup_requires_review)} requiring review"
    )
    tx = store.create(
        operation_type="Merge Album",
        status="Preview",
        summary=summary,
        changes=changes,
        rollback_available=True,
        rollback_reason="Can restore reassigned album membership, duplicate item rows, and quarantined duplicate files.",
        metadata={
            "mutation_family": "existing_album_reconcile_v1",
            "payload": tx_payload,
            "cancellable": False,
            **tx_payload,
        },
    )
    tx_res = dict(tx)
    tx_res["payload"] = tx_payload

    return {
        "ok": True,
        "operation_id": tx["id"],
        "transaction": tx_res,
        "reconciled_moves": len(move_specs),
        "reconciled_duplicates": len(dup_specs),
        "dup_requires_review": dup_requires_review,
    }


def execute_existing_album_reconcile_apply(
    store: TransactionStore,
    operation_id: str,
    *,
    db_path: Optional[str] = None,
    music_allowed_roots: Optional[List[str]] = None,
    staging_allowed_roots: Optional[List[str]] = None,
    quarantine_base_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute Apply phase for existing_album_reconcile_v1 (SEC-002 Wave 20)."""
    # Explicit, local format validation before operation_id is ever used to
    # build a filesystem path (the quarantine directory) -- TransactionStore
    # already enforces this same pattern internally, but that guarantee
    # lives in a different function than the path construction below, which
    # is not a dataflow relationship a static analyzer can see. Validating
    # here closes that gap directly (CodeQL py/path-injection) rather than
    # relying on an indirect cross-function invariant.
    if not _TRANSACTION_ID_RE.fullmatch(str(operation_id or "")):
        return {"ok": False, "error": "Malformed transaction id.", "code": "reconcile_not_found"}
    op_lock = _get_apply_lock(operation_id)
    with op_lock:
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": "Transaction not found.", "code": "reconcile_not_found"}

        meta = tx.get("metadata") or {}
        mutation_family = meta.get("mutation_family")
        if mutation_family != "existing_album_reconcile_v1":
            return {"ok": False, "error": "Transaction family mismatch.", "code": "reconcile_wrong_family"}

        payload = meta.get("payload") or meta or tx.get("payload") or {}
        move_specs: List[Dict[str, Any]] = payload.get("move_specs") or []
        dup_specs: List[Dict[str, Any]] = payload.get("dup_specs") or []
        imported_album_id = int(payload.get("imported_album_id") or 0)
        existing_album_id = int(payload.get("existing_album_id") or 0)

        if tx.get("status") == "Completed":
            return {
                "ok": True,
                "status": "Completed",
                "already_completed": True,
                "operation_id": operation_id,
                "reconciled_moves": len(move_specs),
                "reconciled_duplicates": len(dup_specs),
            }

        all_item_ids = sorted(list({
            *[int(m["item_id"]) for m in move_specs],
            *[int(d["item_id"]) for d in dup_specs],
            # SEC-002 Wave 20 final review: survivor state authorizes
            # destructive deletion of the duplicate, so survivors are a
            # resource too -- lock them so another transaction cannot
            # mutate/retire a survivor while this Apply is relying on it.
            *[int(s["item_id"]) for d in dup_specs for s in (d.get("survivors") or [])],
        }))

        # Resource locking: album locks + sorted item locks (incl. survivors)
        alb_lock_1 = _get_resource_lock(f"album:{min(imported_album_id, existing_album_id)}")
        alb_lock_2 = _get_resource_lock(f"album:{max(imported_album_id, existing_album_id)}")
        item_locks = [_get_resource_lock(f"item:{iid}") for iid in all_item_ids]

        with ExitStack() as stack:
            stack.enter_context(alb_lock_1)
            stack.enter_context(alb_lock_2)
            for ilock in item_locks:
                stack.enter_context(ilock)

            # Re-read transaction state under locks
            tx = store.get(operation_id)
            if tx.get("status") == "Completed":
                return {
                    "ok": True,
                    "status": "Completed",
                    "already_completed": True,
                    "operation_id": operation_id,
                    "reconciled_moves": len(move_specs),
                    "reconciled_duplicates": len(dup_specs),
                }

            lib_db = db_path or os.environ.get("BEETS_DB_PATH", os.path.expanduser("~/.config/beets/musiclibrary.blb"))
            if not os.path.exists(lib_db):
                LOG.error("Reconcile apply: Beets database not found at %s", lib_db)
                return {"ok": False, "error": "Beets database unavailable.", "code": "db_not_found"}

            library_roots: List[Path] = []
            for r in (music_allowed_roots or [os.environ.get("MUSIC_ROOT", "/music")]):
                try:
                    p = Path(r).resolve()
                    if p.exists():
                        library_roots.append(p)
                except Exception:
                    pass
            staging_roots: List[Path] = []
            for r in (staging_allowed_roots or []):
                try:
                    p = Path(r).resolve()
                    if p.exists():
                        staging_roots.append(p)
                except Exception:
                    pass
            imported_item_roots = library_roots + staging_roots or library_roots

            curr_meta_snapshot = store.get(operation_id).get("metadata", {})
            already_quarantined_ids = {int(qf["item_id"]) for qf in (curr_meta_snapshot.get("quarantined_files") or [])}
            db_stage_done = any(
                s.get("step") == "db_reassigned" and s.get("status") == "Completed"
                for s in (curr_meta_snapshot.get("apply_steps") or [])
            )

            def _fail(err: str, code: str = "reconcile_toctou_mismatch") -> Dict[str, Any]:
                store.update(operation_id, status="Failed", logs=[err])
                return {"ok": False, "error": err, "code": code, "requires_new_plan": True}

            # TOCTOU & Precondition Revalidation (ALL-OR-NOTHING, before any
            # mutation). Items/album state already durably recorded as
            # mutated by THIS transaction (crash-retry resume) are expected
            # to look "gone" -- that is the correct already-applied outcome,
            # not evidence of external tampering (SEC-002 Wave 20 final
            # review, "Apply retry after partial filesystem/DB mutation").
            if not db_stage_done:
                try:
                    con = sqlite3.connect(lib_db, timeout=10)
                    con.row_factory = sqlite3.Row
                    try:
                        cur = con.cursor()

                        cur.execute("SELECT id, mb_releasegroupid FROM albums WHERE id=?", (existing_album_id,))
                        ex_alb = cur.fetchone()
                        if not ex_alb:
                            return _fail(f"Target album {existing_album_id} missing", "reconcile_album_missing")

                        cur.execute("SELECT id, mb_releasegroupid FROM albums WHERE id=?", (imported_album_id,))
                        imp_alb = cur.fetchone()
                        if not imp_alb:
                            return _fail(f"Source album {imported_album_id} missing", "reconcile_album_missing")

                        # Album RGID TOCTOU: both albums' RGIDs must still
                        # match what Plan captured, and their compatibility
                        # relation must still hold (SEC-002 Wave 20 final
                        # review, "Album Release Group TOCTOU is not
                        # revalidated").
                        live_ex_rg = str(ex_alb["mb_releasegroupid"] or "").strip().lower()
                        live_imp_rg = str(imp_alb["mb_releasegroupid"] or "").strip().lower()
                        expected_ex_rg = str(payload.get("existing_album_rg_before") or "")
                        expected_imp_rg = str(payload.get("imported_album_rg_before") or "")
                        if live_ex_rg != expected_ex_rg or live_imp_rg != expected_imp_rg:
                            return _fail("Album Release Group ID changed since plan", "reconcile_identity_mismatch")
                        if live_ex_rg and live_imp_rg and live_ex_rg != live_imp_rg:
                            return _fail("Album Release Group IDs are no longer compatible", "reconcile_identity_mismatch")

                        # Validate move_specs (full identity, not just path/stat)
                        for mspec in move_specs:
                            iid = int(mspec["item_id"])
                            before = mspec["before"]
                            cur.execute("SELECT id, album_id, path, mb_trackid, mb_albumid, mb_releasegroupid, disc, track FROM items WHERE id=?", (iid,))
                            irow = cur.fetchone()
                            if not irow:
                                return _fail(f"Move item {iid} missing", "reconcile_item_missing")
                            if int(irow["album_id"]) != imported_album_id:
                                return _fail(f"Move item {iid} album membership changed", "reconcile_album_membership_changed")

                            raw_path = irow["path"]
                            path_str = raw_path.decode("utf-8", "replace") if isinstance(raw_path, bytes) else str(raw_path)
                            if path_str != before["path"]:
                                return _fail(f"Move item {iid} path changed", "reconcile_toctou_mismatch")
                            if str(irow["mb_trackid"] or "").strip().lower() != before.get("mb_trackid", ""):
                                return _fail(f"Move item {iid} Recording ID changed", "reconcile_toctou_mismatch")
                            if str(irow["mb_albumid"] or "").strip().lower() != before.get("mb_albumid", ""):
                                return _fail(f"Move item {iid} Release ID changed", "reconcile_toctou_mismatch")
                            if str(irow["mb_releasegroupid"] or "").strip().lower() != before.get("mb_releasegroupid", ""):
                                return _fail(f"Move item {iid} Release Group ID changed", "reconcile_toctou_mismatch")

                            ipath = Path(path_str)
                            base_root = _resolve_path_role(path_str, imported_item_roots)
                            if base_root is None:
                                return _fail(f"Move item {iid} path outside allowed roots", "reconcile_path_out_of_root")
                            if _path_has_symlink_under(ipath, base_root):
                                return _fail(f"Move item {iid} path contains symlink", "reconcile_symlink_rejected")
                            if not ipath.exists():
                                return _fail(f"Move item {iid} file missing", "reconcile_item_missing")

                            st = ipath.stat()
                            b_stat = before["stat"]
                            if st.st_dev != b_stat["dev"] or st.st_ino != b_stat["ino"] or st.st_size != b_stat["size"] or st.st_mtime_ns != b_stat["mtime_ns"]:
                                return _fail(f"Move item {iid} file stat changed", "reconcile_toctou_mismatch")

                        # Validate dup_specs
                        for dspec in dup_specs:
                            iid = int(dspec["item_id"])
                            before = dspec["before"]
                            if iid in already_quarantined_ids:
                                # This item's file was already durably
                                # quarantined by a prior (crashed) Apply
                                # attempt on this SAME transaction -- its
                                # original path is expected to be gone.
                                continue
                            cur.execute("SELECT id, album_id, path, mb_trackid FROM items WHERE id=?", (iid,))
                            irow = cur.fetchone()
                            if not irow:
                                return _fail(f"Duplicate item {iid} missing", "reconcile_item_missing")
                            if int(irow["album_id"]) != imported_album_id:
                                return _fail(f"Duplicate item {iid} album membership changed", "reconcile_album_membership_changed")

                            raw_path = irow["path"]
                            path_str = raw_path.decode("utf-8", "replace") if isinstance(raw_path, bytes) else str(raw_path)
                            if path_str != before["path"]:
                                return _fail(f"Duplicate item {iid} path changed", "reconcile_toctou_mismatch")
                            if str(irow["mb_trackid"] or "").strip().lower() != before.get("mb_trackid", ""):
                                return _fail(f"Duplicate item {iid} Recording ID changed", "reconcile_toctou_mismatch")

                            ipath = Path(path_str)
                            base_root = _resolve_path_role(path_str, imported_item_roots)
                            if base_root is None:
                                return _fail(f"Duplicate item {iid} path outside allowed roots", "reconcile_path_out_of_root")
                            if _path_has_symlink_under(ipath, base_root):
                                return _fail(f"Duplicate item {iid} path contains symlink", "reconcile_symlink_rejected")
                            if not ipath.exists():
                                return _fail(f"Duplicate item {iid} file missing", "reconcile_item_missing")

                            st = ipath.stat()
                            b_stat = before["stat"]
                            if st.st_dev != b_stat["dev"] or st.st_ino != b_stat["ino"] or st.st_size != b_stat["size"] or st.st_mtime_ns != b_stat["mtime_ns"]:
                                return _fail(f"Duplicate item {iid} file stat changed", "reconcile_toctou_mismatch")

                            # Re-verify survivors: identity, membership,
                            # path role, symlinks, physical + readable
                            # state, and full stat signature.
                            for surv in dspec.get("survivors") or []:
                                s_id = int(surv["item_id"])
                                cur.execute("SELECT id, album_id, path, mb_trackid FROM items WHERE id=?", (s_id,))
                                s_row = cur.fetchone()
                                if not s_row or int(s_row["album_id"]) != existing_album_id:
                                    return _fail(f"Duplicate item {iid} survivor {s_id} changed or missing", "reconcile_survivor_stale")
                                if str(s_row["mb_trackid"] or "").strip().lower() != surv.get("mb_trackid", ""):
                                    return _fail(f"Duplicate item {iid} survivor {s_id} identity changed", "reconcile_survivor_stale")
                                s_raw_path = s_row["path"]
                                s_path_str = s_raw_path.decode("utf-8", "replace") if isinstance(s_raw_path, bytes) else str(s_raw_path)
                                if s_path_str != surv.get("path"):
                                    return _fail(f"Duplicate item {iid} survivor {s_id} path changed", "reconcile_survivor_stale")
                                s_path = Path(s_path_str)
                                if _resolve_path_role(s_path_str, library_roots) is None:
                                    return _fail(f"Duplicate item {iid} survivor {s_id} path outside library root", "reconcile_path_out_of_root")
                                if _path_has_symlink_under(s_path, library_roots[0] if library_roots else s_path):
                                    return _fail(f"Duplicate item {iid} survivor {s_id} path contains symlink", "reconcile_symlink_rejected")
                                if not s_path.exists():
                                    return _fail(f"Duplicate item {iid} survivor {s_id} file missing on disk", "reconcile_survivor_stale")
                                s_st = s_path.stat()
                                exp_s_stat = surv.get("stat") or {}
                                if exp_s_stat and (
                                    s_st.st_dev != exp_s_stat.get("dev") or s_st.st_ino != exp_s_stat.get("ino")
                                    or s_st.st_size != exp_s_stat.get("size") or s_st.st_mtime_ns != exp_s_stat.get("mtime_ns")
                                ):
                                    return _fail(f"Duplicate item {iid} survivor {s_id} file changed since plan", "reconcile_survivor_stale")
                                s_read = _read_file_audio_tags(s_path)
                                if not s_read.get("ok"):
                                    return _fail(f"Duplicate item {iid} survivor {s_id} media no longer readable", "reconcile_survivor_stale")
                    finally:
                        con.close()
                except sqlite3.Error as ex:
                    LOG.error("Reconcile apply: TOCTOU query failed for op %s: %s", operation_id, ex)
                    return _fail("Precondition verification failed.", "reconcile_verification_failed")

            # Mutation is about to begin. `mutation_started` records intent
            # durably before anything happens; `mutated`/`filesystem_mutated`/
            # `db_mutated` are only set True immediately after each stage's
            # first real mutation actually succeeds (SEC-002 Wave 20 final
            # review, "mutated=True is set before mutation").
            store.update(operation_id, status="Running", metadata={**meta, "mutation_started": True})

            def _persist_step(step_name: str, step_status: str, **extra: Any) -> None:
                curr_tx = store.get(operation_id)
                steps = curr_tx.get("metadata", {}).get("apply_steps", [])
                steps.append({
                    "step": step_name,
                    "status": step_status,
                    "timestamp": _now(),
                    **extra,
                })
                store.update(operation_id, metadata={**curr_tx.get("metadata", {}), "apply_steps": steps})

            if not db_stage_done:
                # Quarantine directory is only created once every
                # precondition above has passed -- not claiming "zero
                # filesystem mutation before validation" while actually
                # creating a directory earlier (SEC-002 Wave 20 final
                # review, "quarantine directory creation timing").
                q_root_env = quarantine_base_root or os.environ.get("RECONCILE_QUARANTINE_DIR", "/config/reconcile_quarantine")
                q_root_norm = os.path.normpath(q_root_env)
                q_dir = Path(q_root_env) / operation_id
                # Explicit normalize-then-prefix-check barrier (the exact
                # idiom CodeQL's py/path-injection query documents as a
                # sanitizer) on top of the _TRANSACTION_ID_RE format check
                # already performed at function entry: operation_id can
                # never actually escape q_root_env (no "/", "\", or ".."
                # passes the id regex), but this makes that guarantee
                # explicit and locally checkable rather than relying on a
                # cross-function invariant a static analyzer cannot see.
                if os.path.normpath(str(q_dir)) != os.path.join(q_root_norm, operation_id):
                    LOG.error("Reconcile apply: quarantine path escaped root for op %s", operation_id)
                    store.update(operation_id, status="Failed", logs=["Quarantine path validation failed."])
                    return {"ok": False, "error": "Quarantine path validation failed.", "code": "reconcile_path_out_of_root", "mutated": True}

                # Stage 1: Duplicate file quarantine. Every recoverable
                # duplicate (hardlink or distinct file) is quarantined via
                # the hardened rename/copy helper -- never permanently
                # unlinked -- so rollback_available is never a lie
                # (SEC-002 Wave 20 final review: "never claim rollback for
                # unrecoverable unlinks"). `db_only` duplicates (the
                # duplicate row and the survivor point at the SAME path)
                # are never touched physically at all.
                quarantined_files: List[Dict[str, Any]] = list(curr_meta_snapshot.get("quarantined_files") or [])
                for dspec in dup_specs:
                    iid = int(dspec["item_id"])
                    if iid in already_quarantined_ids:
                        continue
                    physical_class = dspec.get("physical_class") or "distinct_file"
                    if physical_class == "db_only":
                        _persist_step("dup_file_preserved", "Completed", item_id=iid, reason="same_path_as_survivor")
                        continue

                    fpath = Path(dspec["before"]["path"])
                    try:
                        q_dir.mkdir(parents=True, exist_ok=True)
                    except OSError as ex:
                        LOG.error("Reconcile apply: quarantine dir creation failed for op %s: %s", operation_id, ex)
                        store.update(operation_id, status="Failed", logs=["Quarantine directory creation failed."])
                        return {"ok": False, "error": "Quarantine directory creation failed.", "code": "reconcile_filesystem_failed", "mutated": True}

                    # fpath.name strips every directory component (no
                    # "..", no "/") -- but make the containment guarantee
                    # for target_q explicit and locally checkable too,
                    # same normalize-then-prefix idiom as q_dir above.
                    safe_leaf = fpath.name
                    target_q = q_dir / f"{iid}_{safe_leaf}"
                    q_dir_norm = os.path.normpath(str(q_dir))
                    if os.path.dirname(os.path.normpath(str(target_q))) != q_dir_norm:
                        store.update(operation_id, status="Failed", logs=[f"Quarantine target path validation failed for item {iid}."])
                        return {"ok": False, "error": "Quarantine path validation failed.", "code": "reconcile_path_out_of_root", "mutated": True}
                    if target_q.exists() or target_q.is_symlink():
                        store.update(operation_id, status="Failed", logs=[f"Quarantine destination already exists for item {iid}."])
                        return {"ok": False, "error": "Quarantine destination collision.", "code": "reconcile_filesystem_failed", "mutated": True}
                    try:
                        _safe_rename(fpath, target_q)
                        entry = {"item_id": iid, "original_path": str(fpath), "quarantine_path": str(target_q), "physical_class": physical_class}
                        quarantined_files.append(entry)
                        store.update(operation_id, metadata={
                            **store.get(operation_id).get("metadata", {}),
                            "quarantined_files": quarantined_files,
                            "filesystem_mutated": True,
                            "mutated": True,
                        })
                        _persist_step("dup_file_quarantined", "Completed", item_id=iid)
                    except OSError as ex:
                        LOG.error("Reconcile apply: quarantine failed for item %s op %s: %s", iid, operation_id, ex)
                        store.update(operation_id, status="Failed", logs=[f"File quarantine failed for item {iid}."])
                        return {"ok": False, "error": "File quarantine failed.", "code": "reconcile_filesystem_failed", "mutated": True, "partial_mutation": True}

                # Stage 2: Database operations (single connection/commit,
                # exact rowcount verified for every statement).
                try:
                    con = sqlite3.connect(lib_db, timeout=10)
                    try:
                        cur = con.cursor()

                        if dup_specs:
                            dup_ids = [int(d["item_id"]) for d in dup_specs]
                            cur.execute(f"DELETE FROM items WHERE id IN ({','.join('?' * len(dup_ids))})", dup_ids)
                            if cur.rowcount != len(dup_ids):
                                con.rollback()
                                store.update(operation_id, status="Failed", logs=["DB delete rowcount mismatch for duplicate items."])
                                return {"ok": False, "error": "DB delete rowcount mismatch.", "code": "reconcile_db_failed", "mutated": True, "partial_mutation": True}

                        if move_specs:
                            cur.execute("SELECT album, albumartist FROM albums WHERE id=?", (existing_album_id,))
                            ex_alb_row = cur.fetchone()
                            target_album_name = str(ex_alb_row[0] or "") if ex_alb_row and ex_alb_row[0] is not None else ""
                            target_albumartist = str(ex_alb_row[1] or "") if ex_alb_row and ex_alb_row[1] is not None else ""

                            move_ids = [int(m["item_id"]) for m in move_specs]
                            cur.execute(
                                f"UPDATE items SET album_id=?, album=?, albumartist=? WHERE id IN ({','.join('?' * len(move_ids))})",
                                (existing_album_id, target_album_name, target_albumartist, *move_ids),
                            )
                            if cur.rowcount != len(move_ids):
                                con.rollback()
                                store.update(operation_id, status="Failed", logs=["DB update rowcount mismatch for moved items."])
                                return {"ok": False, "error": "DB update rowcount mismatch.", "code": "reconcile_db_failed", "mutated": True, "partial_mutation": True}

                        if payload.get("retire_imported_album"):
                            cur.execute("SELECT COUNT(*) FROM items WHERE album_id=?", (imported_album_id,))
                            left_cnt = cur.fetchone()[0]
                            if left_cnt == 0:
                                cur.execute("DELETE FROM albums WHERE id=?", (imported_album_id,))
                                if cur.rowcount != 1:
                                    con.rollback()
                                    store.update(operation_id, status="Failed", logs=["Album retirement rowcount mismatch."])
                                    return {"ok": False, "error": "Album retirement rowcount mismatch.", "code": "reconcile_db_failed", "mutated": True, "partial_mutation": True}

                        con.commit()
                    finally:
                        con.close()
                    store.update(operation_id, metadata={**store.get(operation_id).get("metadata", {}), "db_mutated": True, "mutated": True})
                    _persist_step("db_reassigned", "Completed")
                except sqlite3.Error as ex:
                    LOG.error("Reconcile apply: database operations failed for op %s: %s", operation_id, ex)
                    store.update(operation_id, status="Failed", logs=["Database operations failed."])
                    return {"ok": False, "error": "Database operations failed.", "code": "reconcile_db_failed", "mutated": True, "partial_mutation": True}

            # Stage 3: Verification -- DB state and final filesystem state.
            try:
                con = sqlite3.connect(lib_db, timeout=10)
                try:
                    cur = con.cursor()
                    for mspec in move_specs:
                        iid = int(mspec["item_id"])
                        cur.execute("SELECT album_id FROM items WHERE id=?", (iid,))
                        r = cur.fetchone()
                        if not r or int(r[0]) != existing_album_id:
                            store.update(operation_id, status="Failed", logs=[f"Verification failed for move item {iid}."])
                            return {"ok": False, "error": "Post-write verification failed.", "code": "reconcile_verification_failed", "mutated": True}

                    for dspec in dup_specs:
                        iid = int(dspec["item_id"])
                        cur.execute("SELECT COUNT(*) FROM items WHERE id=?", (iid,))
                        if cur.fetchone()[0] != 0:
                            store.update(operation_id, status="Failed", logs=[f"Verification failed: duplicate item {iid} still exists."])
                            return {"ok": False, "error": "Post-write verification failed.", "code": "reconcile_verification_failed", "mutated": True}

                    if payload.get("retire_imported_album"):
                        cur.execute("SELECT COUNT(*) FROM albums WHERE id=?", (imported_album_id,))
                        if cur.fetchone()[0] != 0:
                            store.update(operation_id, status="Failed", logs=["Verification failed: imported album still exists."])
                            return {"ok": False, "error": "Post-write verification failed.", "code": "reconcile_verification_failed", "mutated": True}
                finally:
                    con.close()

                # Filesystem final-state verification: every quarantined
                # duplicate's original path is gone and the quarantine copy
                # exists; every survivor is still present.
                curr_meta = store.get(operation_id).get("metadata", {})
                for qf in (curr_meta.get("quarantined_files") or []):
                    if Path(qf["original_path"]).exists():
                        store.update(operation_id, status="Failed", logs=["Verification failed: quarantined original path still present."])
                        return {"ok": False, "error": "Post-write verification failed.", "code": "reconcile_verification_failed", "mutated": True}
                    if not Path(qf["quarantine_path"]).exists():
                        store.update(operation_id, status="Failed", logs=["Verification failed: quarantine copy missing."])
                        return {"ok": False, "error": "Post-write verification failed.", "code": "reconcile_verification_failed", "mutated": True}
                for dspec in dup_specs:
                    for surv in dspec.get("survivors") or []:
                        if not Path(surv["path"]).exists():
                            store.update(operation_id, status="Failed", logs=["Verification failed: survivor file missing after apply."])
                            return {"ok": False, "error": "Post-write verification failed.", "code": "reconcile_verification_failed", "mutated": True}

                _persist_step("result_verified", "Completed")
            except sqlite3.Error as ex:
                LOG.error("Reconcile apply: verification query failed for op %s: %s", operation_id, ex)
                store.update(operation_id, status="Failed", logs=["Post-write verification query failed."])
                return {"ok": False, "error": "Post-write verification query failed.", "code": "reconcile_verification_failed", "mutated": True}

            store.update(operation_id, status="Completed", logs=["Existing album reconciliation applied successfully."])
            return {
                "ok": True,
                "status": "Completed",
                "operation_id": operation_id,
                "reconciled_moves": len(move_specs),
                "reconciled_duplicates": len(dup_specs),
            }


def rollback_existing_album_reconcile(
    store: TransactionStore,
    operation_id: str,
    *,
    db_path: Optional[str] = None,
    music_allowed_roots: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Execute rollback for existing_album_reconcile_v1 (SEC-002 Wave 20)."""
    if not _TRANSACTION_ID_RE.fullmatch(str(operation_id or "")):
        return {"ok": False, "error": "Malformed transaction id.", "code": "reconcile_not_found"}
    op_lock = _get_apply_lock(operation_id)
    with op_lock:
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": "Transaction not found.", "code": "reconcile_not_found"}

        meta = tx.get("metadata") or {}
        mutation_family = meta.get("mutation_family")
        if mutation_family != "existing_album_reconcile_v1":
            return {"ok": False, "error": "Transaction family mismatch.", "code": "reconcile_wrong_family"}

        status = tx.get("status")
        if status == "Rolled Back":
            return {"ok": False, "error": "This transaction has already been rolled back.", "code": "reconcile_already_rolled_back"}
        db_mutated = bool(meta.get("db_mutated") or meta.get("mutated"))
        if status not in ("Completed", "Failed", "Partially Rolled Back") or not db_mutated:
            return {"ok": False, "error": "Rollback unavailable: transaction has not performed any mutations.", "code": "reconcile_not_mutated"}

        payload = meta.get("payload") or meta or tx.get("payload") or {}
        imported_album_id = int(payload.get("imported_album_id") or 0)
        existing_album_id = int(payload.get("existing_album_id") or 0)
        move_specs: List[Dict[str, Any]] = payload.get("move_specs") or []
        dup_specs: List[Dict[str, Any]] = payload.get("dup_specs") or []
        imported_alb_before = payload.get("imported_album_before")
        quarantined_files: List[Dict[str, Any]] = meta.get("quarantined_files") or []

        item_ids = sorted({int(m["item_id"]) for m in move_specs} | {int(d["item_id"]) for d in dup_specs})
        alb_lock_1 = _get_resource_lock(f"album:{min(imported_album_id, existing_album_id)}")
        alb_lock_2 = _get_resource_lock(f"album:{max(imported_album_id, existing_album_id)}")
        item_locks = [_get_resource_lock(f"item:{iid}") for iid in item_ids]

        with ExitStack() as stack:
            stack.enter_context(alb_lock_1)
            stack.enter_context(alb_lock_2)
            for ilock in item_locks:
                stack.enter_context(ilock)

            lib_db = db_path or os.environ.get("BEETS_DB_PATH", os.path.expanduser("~/.config/beets/musiclibrary.blb"))
            if not os.path.exists(lib_db):
                LOG.error("Reconcile rollback: Beets database not found at %s", lib_db)
                return {"ok": False, "error": "Beets database unavailable.", "code": "db_not_found"}

            library_roots: List[Path] = []
            for r in (music_allowed_roots or [os.environ.get("MUSIC_ROOT", "/music")]):
                try:
                    p = Path(r).resolve()
                    if p.exists():
                        library_roots.append(p)
                except Exception:
                    pass

            # Precondition: current state must still match what THIS
            # transaction actually produced -- refuse to clobber a later,
            # unrelated legitimate change (SEC-002 Wave 20 final review,
            # "rollback needs current-state validation").
            try:
                con = sqlite3.connect(lib_db, timeout=10)
                con.row_factory = sqlite3.Row
                try:
                    cur = con.cursor()
                    for mspec in move_specs:
                        iid = int(mspec["item_id"])
                        cur.execute("SELECT album_id FROM items WHERE id=?", (iid,))
                        row = cur.fetchone()
                        if not row:
                            store.update(operation_id, logs=[f"Rollback precondition failed: item {iid} no longer exists."])
                            return {"ok": False, "error": f"Item {iid} no longer exists.", "code": "reconcile_rollback_precondition_failed"}
                        if int(row["album_id"]) != existing_album_id:
                            store.update(operation_id, logs=[f"Rollback precondition failed: item {iid} was modified after this reconcile."])
                            return {"ok": False, "error": f"Item {iid} was modified after this reconcile; refusing to overwrite a later change.", "code": "reconcile_rollback_stale"}
                    for dspec in dup_specs:
                        iid = int(dspec["item_id"])
                        cur.execute("SELECT COUNT(*) FROM items WHERE id=?", (iid,))
                        if cur.fetchone()[0] != 0:
                            store.update(operation_id, logs=[f"Rollback precondition failed: duplicate item {iid} already exists."])
                            return {"ok": False, "error": f"Item {iid} already exists; refusing to overwrite.", "code": "reconcile_rollback_stale"}
                finally:
                    con.close()
            except sqlite3.Error as ex:
                LOG.error("Reconcile rollback: precondition query failed for op %s: %s", operation_id, ex)
                return {"ok": False, "error": "Rollback precondition check failed.", "code": "reconcile_verification_failed"}

            # Stage 1: restore quarantined files FIRST -- never create a
            # live DB row pointing at a file we have not yet confirmed
            # exists (SEC-002 Wave 20 final review, "critical rollback
            # ordering").
            files_restored = 0
            files_failed_ids: List[int] = []
            restored_paths: Dict[int, str] = {}
            for qf in quarantined_files:
                iid = int(qf["item_id"])
                orig_p = Path(qf["original_path"])
                q_p = Path(qf["quarantine_path"])
                base_root = _resolve_path_role(str(orig_p), library_roots)
                if base_root is None:
                    files_failed_ids.append(iid)
                    continue
                if orig_p.exists() or orig_p.is_symlink():
                    # Destination collision -- something now occupies the
                    # original path. Refuse rather than overwrite it.
                    files_failed_ids.append(iid)
                    continue
                if not q_p.exists():
                    files_failed_ids.append(iid)
                    continue
                try:
                    orig_p.parent.mkdir(parents=True, exist_ok=True)
                    if _path_has_symlink_under(orig_p.parent, base_root):
                        files_failed_ids.append(iid)
                        continue
                    _safe_rename(q_p, orig_p)
                    if not orig_p.exists() or orig_p.is_symlink():
                        files_failed_ids.append(iid)
                        continue
                    files_restored += 1
                    restored_paths[iid] = str(orig_p)
                except OSError:
                    files_failed_ids.append(iid)

            # Stage 2: restore DB rows -- only for items whose file
            # restoration just succeeded (or that never needed a file
            # restored at all, e.g. db_only duplicates and moves).
            db_restored = 0
            db_failed_ids: List[int] = []
            try:
                con = sqlite3.connect(lib_db, timeout=10)
                try:
                    cur = con.cursor()

                    if imported_alb_before:
                        cur.execute("SELECT COUNT(*) FROM albums WHERE id=?", (imported_album_id,))
                        if cur.fetchone()[0] == 0:
                            cols = list(imported_alb_before.keys())
                            placeholders = ",".join("?" * len(cols))
                            cur.execute(
                                f"INSERT INTO albums ({','.join(cols)}) VALUES ({placeholders})",
                                [imported_alb_before[c] for c in cols],
                            )

                    for mspec in move_specs:
                        iid = int(mspec["item_id"])
                        before = mspec["before"]
                        cur.execute(
                            "UPDATE items SET album_id=?, album=?, albumartist=? WHERE id=?",
                            (imported_album_id, before["album"], before["albumartist"], iid),
                        )
                        if cur.rowcount == 1:
                            db_restored += 1
                        else:
                            db_failed_ids.append(iid)

                    for dspec in dup_specs:
                        iid = int(dspec["item_id"])
                        physical_class = dspec.get("physical_class") or "distinct_file"
                        if physical_class != "db_only" and iid not in restored_paths:
                            # File restoration required but did not
                            # succeed -- do not reinsert a DB row that
                            # would point at a missing file.
                            db_failed_ids.append(iid)
                            continue
                        before = dspec["before"]
                        full_row = before.get("full_row") or {}
                        cur.execute("SELECT COUNT(*) FROM items WHERE id=?", (iid,))
                        if cur.fetchone()[0] == 0:
                            if full_row:
                                cols = list(full_row.keys())
                                placeholders = ",".join("?" * len(cols))
                                cur.execute(
                                    f"INSERT INTO items ({','.join(cols)}) VALUES ({placeholders})",
                                    [full_row[c] for c in cols],
                                )
                            else:
                                cur.execute(
                                    "INSERT INTO items (id, album_id, title, artist, album, albumartist, disc, track, path, mb_trackid, mb_albumid, mb_releasegroupid) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                    (
                                        iid, imported_album_id, before.get("title", ""), before.get("artist", ""),
                                        before.get("album", ""), before.get("albumartist", ""), before.get("disc", 1),
                                        before.get("track", 0), before.get("path", ""), before.get("mb_trackid", ""),
                                        before.get("mb_albumid", ""), before.get("mb_releasegroupid", ""),
                                    ),
                                )
                            if cur.rowcount == 1:
                                db_restored += 1
                            else:
                                db_failed_ids.append(iid)
                        else:
                            db_failed_ids.append(iid)

                    con.commit()
                finally:
                    con.close()
            except sqlite3.Error as ex:
                LOG.error("Reconcile rollback: DB restore failed for op %s: %s", operation_id, ex)
                store.update(operation_id, logs=["Rollback database restore failed."])
                return {"ok": False, "error": "Rollback database restore failed.", "code": "reconcile_rollback_db_failed"}

            total_expected = len(move_specs) + len(dup_specs)
            full_success = (not db_failed_ids) and (not files_failed_ids)
            any_restored = db_restored > 0 or files_restored > 0
            final_status = "Rolled Back" if full_success else ("Partially Rolled Back" if any_restored else "Failed")

            store.update(
                operation_id,
                status=final_status,
                logs=[f"Existing album reconcile rollback completed with status {final_status}."],
                metadata={
                    **meta,
                    "rollback_available": False,
                    "db_restored_count": db_restored,
                    "files_restored_count": files_restored,
                    "db_failed_items": db_failed_ids,
                    "files_failed_items": files_failed_ids,
                },
            )
            # SEC-002 Wave 20 final review, "rollback partial failure
            # truthfulness": an incomplete recovery is not reported as a
            # plain success.
            return {
                "ok": final_status == "Rolled Back",
                "operation_id": operation_id,
                "status": final_status,
                "partial_mutation": final_status == "Partially Rolled Back",
                "db_restored_count": db_restored,
                "files_restored_count": files_restored,
                "total_items": total_expected,
            }


@contextmanager
def _lock_resources(resource_keys: List[str]):
    locks = [_get_resource_lock(k) for k in sorted(set(resource_keys or []))]
    with ExitStack() as stack:
        for l in locks:
            stack.enter_context(l)
        yield


def _resolve_db_path(path_str: str, allowed_roots: List[str]) -> Path:
    """Resolve a path read directly from Beets' SQLite columns
    (items.path / albums.artpath) to an absolute Path.

    Wave 24 final review round 3 (two-service acceptance script fixture
    seeding via the real `beets` package): a real, installed Beets
    (verified against 2.12.0/2.13.1's beets/dbcore/pathutils.py) stores
    these paths RELATIVE to the configured music directory whenever the
    absolute path is inside it (normalize_path_for_db /
    expand_path_from_db, gated on beets.context.get_music_dir() being set
    -- true for any path written by a real `beet` CLI invocation, since
    opening the library always sets it). Reading the raw column through a
    direct sqlite3 connection (as every function in this module does, to
    avoid a hard runtime dependency on the `beets` package) bypasses that
    expansion entirely, so a genuinely relative stored value must be
    joined against the configured music root here -- exactly the pattern
    album_maintenance_v1's remove_tracks/deduplicate modes already use
    (and app.py's own album_deduplicate `_abs()` helper predates this
    file). This was previously missing from the relocation/artwork/
    metadata/mb_track_repair families specifically.
    """
    p = Path(path_str)
    if p.is_absolute():
        return p
    base = Path(allowed_roots[0]) if allowed_roots else Path(os.environ.get("MUSIC_ROOT", "/music"))
    return base / path_str


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
    try:
        return cand.resolve(strict=False)
    except Exception:
        return None


# ── artist_folder_reconcile_v1 ────────────────────────────────────────────────

def create_artist_folder_reconcile_plan(
    store: TransactionStore,
    payload: Dict[str, Any],
    *,
    music_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
    quarantine_base_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a non-mutating preview plan for artist-folder reconciliation.

    Handles artist folder merging, MBID folder stamping, duplicate file
    quarantine, artwork collision resolution, directory cleanup, and Beets DB
    path/artist attribute updates under an engine-owned transaction boundary.
    """
    raw_root = str(payload.get("root") or "").strip()
    mode = str(payload.get("mode") or "scan_merge").strip()
    selected_keys = set(payload.get("selected_keys") or [])

    allowed_roots = music_allowed_roots or [str(os.environ.get("MUSIC_ROOT", "/music"))]
    if not raw_root:
        return {"ok": False, "error": "root is required", "code": "artist_reconcile_invalid_root"}

    root_role = _resolve_path_role(raw_root, allowed_roots)
    if root_role is None:
        return {"ok": False, "error": "root is outside allowed music library roots", "code": "artist_reconcile_path_out_of_root"}

    # Textual normpath+prefix containment check, in addition to the
    # resolve()-based one above -- the idiom CodeQL's py/path-injection
    # query recognizes as a sanitizing barrier for every filesystem call
    # on root_path below.
    if not _normpath_within_roots(raw_root, allowed_roots):
        return {"ok": False, "error": "root is outside allowed music library roots", "code": "artist_reconcile_path_out_of_root"}

    root_path = Path(raw_root)
    if _path_has_symlink_under(root_path, Path(allowed_roots[0])):
        return {"ok": False, "error": "root contains symlink components", "code": "artist_reconcile_symlink_rejected"}

    if not root_path.exists() or not root_path.is_dir():
        return {"ok": False, "error": "root directory does not exist", "code": "artist_reconcile_invalid_root"}

    lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")
    if lib_db and not Path(lib_db).exists():
        LOG.error("Artist reconcile plan: Beets database not found at %s", lib_db)
        lib_db = ""

    # Discover candidate folder pairs based on mode & DB metadata. Name-based
    # discovery (_engine_scan_artist_folder_groups) and majority-based MBID
    # discovery (_engine_stamp_artist_folder_scan) are both candidate
    # GENERATION only -- neither is trusted as merge authority. Every
    # candidate is independently identity-gated below regardless of source
    # (SEC-002 Wave 21 final review, "candidate generation may use name
    # similarity; Apply eligibility may not").
    candidates_raw: List[Dict[str, Any]] = []
    if payload.get("candidates"):
        candidates_raw = list(payload["candidates"])
    elif mode == "stamp_mbid":
        candidates_raw = _engine_stamp_artist_folder_scan(root_path, lib_db)
    else:
        candidates_raw = _engine_scan_artist_folder_groups(root_path, lib_db, only_keys=selected_keys if selected_keys else None)

    if selected_keys:
        candidates_raw = [c for c in candidates_raw if c.get("key") in selected_keys or c.get("group_key") in selected_keys]

    moves_plan: List[Dict[str, Any]] = []
    dup_quarantines: List[Dict[str, Any]] = []
    art_quarantines: List[Dict[str, Any]] = []
    directory_removals: List[str] = []
    db_item_updates: List[Dict[str, Any]] = []
    db_album_updates: List[Dict[str, Any]] = []
    db_artist_updates: List[Dict[str, Any]] = []
    resource_keys: set = set()
    requires_review: List[Dict[str, Any]] = []
    eligible_candidates: List[Dict[str, Any]] = []

    for idx, cand in enumerate(candidates_raw):
        src_valid = validate_path_under_allowed_roots(cand.get("source_path"), allowed_roots, reject_symlinks=True)
        dst_valid = validate_path_under_allowed_roots(cand.get("target_path"), allowed_roots, reject_symlinks=True)
        if src_valid is None or dst_valid is None:
            return {"ok": False, "error": "Path outside root.", "code": "artist_reconcile_path_out_of_root"}

        src = src_valid
        dst = dst_valid

        if not src.exists() or not src.is_dir():
            requires_review.append({"source_path": str(src), "target_path": str(dst), "reason": "artist_reconcile_source_missing"})
            continue

        # Identity authority: independently derive each side's established
        # Artist ID(s) from Beets DB state -- never trust the caller-supplied
        # id as authority (SEC-002 Wave 21 final review, findings #4-#7).
        is_merge = dst.exists() and dst.is_dir() and dst.resolve(strict=False) != src.resolve(strict=False)
        src_identity = _derive_artist_folder_identity(src, lib_db, allowed_roots=allowed_roots)
        dst_identity = _derive_artist_folder_identity(dst, lib_db, allowed_roots=allowed_roots) if is_merge else {"ids": set(), "album_count": 0}
        caller_mbid = str(cand.get("source_mbid") or cand.get("target_mbid") or cand.get("mbid") or "").strip().lower()
        fingerprint_confirmed = cand.get("fingerprint_confirmed") is True
        recording_mbids = _extract_recording_mbids(cand, src, lib_db, allowed_roots=allowed_roots)
        eligible, resolved_mbid, reason = _artist_folder_identity_decision(
            src_identity, dst_identity, is_merge, caller_mbid, fingerprint_confirmed,
            db_path=lib_db, recording_mbids=recording_mbids,
        )
        if not eligible:
            requires_review.append({
                "source_path": str(src), "target_path": str(dst), "reason": reason,
                "source_established_ids": sorted(src_identity["ids"]),
                "target_established_ids": sorted(dst_identity["ids"]),
            })
            continue

        resource_keys.add(f"artist:{src.name.casefold()}")
        resource_keys.add(f"artist:{dst.name.casefold()}")
        if resolved_mbid:
            resource_keys.add(f"artist-mbid:{resolved_mbid}")

        eligible_candidates.append({
            "source_path": str(src),
            "target_path": str(dst),
            "source_name": cand.get("source_name") or src.name,
            "target_name": cand.get("target_name") or dst.name,
            "resolved_mbid": resolved_mbid,
            "identity_evidence": {
                "canonical_artist_mbid": resolved_mbid,
                "source_established_ids": sorted(src_identity["ids"]),
                "target_established_ids": sorted(dst_identity["ids"]),
                "caller_mbid": caller_mbid,
                "recording_mbids": recording_mbids,
                "fingerprint_confirmed": fingerprint_confirmed,
                "is_merge": is_merge,
                "verification_source": "musicbrainz_recording_artist_credit" if resolved_mbid and not src_identity["ids"] and not dst_identity["ids"] else "established_db_state",
                "verified_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            },
        })

        if is_merge:
            _build_artist_folder_move_graph(
                src, dst, root_path, idx,
                moves_plan, dup_quarantines, art_quarantines, directory_removals
            )
            directory_removals.append(str(src))
        else:
            st = src.stat()
            moves_plan.append({
                "candidate_idx": idx,
                "type": "rename_dir",
                "source": str(src),
                "destination": str(dst),
                "stat": {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns},
            })

    # Capture DB before-state snapshots for affected items & albums, and
    # (SEC-002 Wave 21 final review, findings #26/#27) bind exact item/album
    # IDs discovered here -- never a name-equality WHERE clause -- for the
    # artist-attribute stamping stage below.
    candidate_album_ids: Dict[int, set] = {}
    candidate_item_ids: Dict[int, set] = {}
    if lib_db and Path(lib_db).exists():
        con = sqlite3.connect(lib_db, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            for m in moves_plan:
                idx = m["candidate_idx"]
                src_p = m["source"]
                dst_p = m["destination"]
                if m["type"] == "rename_dir":
                    old_prefix = src_p.encode("utf-8") + os.sep.encode("utf-8")
                    rows = _rows_by_path_prefix(con, "items", "id, path, album_id", "path", old_prefix)
                    for r in rows:
                        iid = int(r["id"])
                        raw_p = r["path"]
                        p_str = raw_p.decode("utf-8", "replace") if isinstance(raw_p, bytes) else str(raw_p)
                        rel = p_str[len(src_p):]
                        new_p = dst_p + rel
                        resource_keys.add(f"item:{iid}")
                        candidate_item_ids.setdefault(idx, set()).add(iid)
                        if r["album_id"]:
                            aid = int(r["album_id"])
                            resource_keys.add(f"album:{aid}")
                            candidate_album_ids.setdefault(idx, set()).add(aid)
                        db_item_updates.append({"candidate_idx": idx, "id": iid, "old_path": p_str, "new_path": new_p})

                    arows = _rows_by_path_prefix(con, "albums", "id, path, artpath", "path", old_prefix)
                    for r in arows:
                        aid = int(r["id"])
                        raw_p = r["path"]
                        p_str = raw_p.decode("utf-8", "replace") if isinstance(raw_p, bytes) else str(raw_p)
                        rel = p_str[len(src_p):]
                        new_p = dst_p + rel
                        raw_art = r["artpath"]
                        art_str = raw_art.decode("utf-8", "replace") if isinstance(raw_art, bytes) else (str(raw_art) if raw_art else "")
                        new_art = (dst_p + art_str[len(src_p):]) if art_str and art_str.startswith(src_p) else art_str
                        resource_keys.add(f"album:{aid}")
                        candidate_album_ids.setdefault(idx, set()).add(aid)
                        db_album_updates.append({"candidate_idx": idx, "id": aid, "old_path": p_str, "new_path": new_p, "old_artpath": art_str, "new_artpath": new_art})
                elif m["type"] in ("move_file", "move_unique"):
                    old_b = src_p.encode("utf-8")
                    rows = con.execute("SELECT id, path, album_id FROM items WHERE path=?", (old_b,)).fetchall()
                    for r in rows:
                        iid = int(r["id"])
                        resource_keys.add(f"item:{iid}")
                        candidate_item_ids.setdefault(idx, set()).add(iid)
                        if r["album_id"]:
                            aid = int(r["album_id"])
                            resource_keys.add(f"album:{aid}")
                            candidate_album_ids.setdefault(idx, set()).add(aid)
                        db_item_updates.append({"candidate_idx": idx, "id": iid, "old_path": src_p, "new_path": dst_p})

            # Artist attribute stamping: bound to the EXACT album/item ids
            # just discovered for this candidate, never a text-equality
            # WHERE clause across the whole library.
            for idx, cand in enumerate(eligible_candidates):
                album_ids = sorted(candidate_album_ids.get(idx) or set())
                item_ids = sorted(candidate_item_ids.get(idx) or set())
                if not album_ids and not item_ids:
                    continue
                dst_name = cand["target_name"]
                mbid = cand["resolved_mbid"]
                albums_before: Dict[int, Dict[str, Any]] = {}
                items_before: Dict[int, Dict[str, Any]] = {}
                if album_ids:
                    placeholders = ",".join("?" * len(album_ids))
                    for r in con.execute(
                        f"SELECT id, albumartist, albumartists, mb_albumartistid, mb_albumartistids FROM albums WHERE id IN ({placeholders})",
                        album_ids,
                    ).fetchall():
                        albums_before[int(r["id"])] = {
                            "albumartist": r["albumartist"], "albumartists": r["albumartists"],
                            "mb_albumartistid": r["mb_albumartistid"], "mb_albumartistids": r["mb_albumartistids"],
                        }
                if item_ids:
                    placeholders = ",".join("?" * len(item_ids))
                    for r in con.execute(
                        f"SELECT id, albumartist, albumartists, mb_albumartistid, mb_albumartistids, artist, artists, mb_artistid, mb_artistids "
                        f"FROM items WHERE id IN ({placeholders})",
                        item_ids,
                    ).fetchall():
                        items_before[int(r["id"])] = {
                            "albumartist": r["albumartist"], "albumartists": r["albumartists"],
                            "mb_albumartistid": r["mb_albumartistid"], "mb_albumartistids": r["mb_albumartistids"],
                            "artist": r["artist"], "artists": r["artists"],
                            "mb_artistid": r["mb_artistid"], "mb_artistids": r["mb_artistids"],
                        }
                if albums_before or items_before:
                    db_artist_updates.append({
                        "candidate_idx": idx,
                        "src_name": cand.get("source_name") or "",
                        "dst_name": dst_name,
                        "mbid": mbid,
                        "album_ids": album_ids,
                        "item_ids": item_ids,
                        "albums_before": albums_before,
                        "items_before": items_before,
                    })
        finally:
            con.close()

    if not moves_plan and not dup_quarantines and not art_quarantines and not db_artist_updates:
        return {
            "ok": True,
            "candidate_count": 0,
            "requires_review": requires_review,
            "message": "No artist folder reconciliation could be safely planned.",
        }

    summary_text = (
        f"Artist folder reconciliation ({mode}): {len(eligible_candidates)} eligible candidate(s), "
        f"{len(moves_plan)} file move(s), {len(requires_review)} requiring review"
    )
    changes = [
        {"id": f"move:{i}", "source": m["source"], "destination": m["destination"], "operation": m["type"]}
        for i, m in enumerate(moves_plan)
    ] + [
        {"id": f"dup:{i}", "source": q["source"], "operation": "Quarantine Duplicate File"}
        for i, q in enumerate(dup_quarantines)
    ] + [
        {"id": f"art:{i}", "source": q["source"], "operation": "Quarantine Artwork"}
        for i, q in enumerate(art_quarantines)
    ] + [
        {"id": f"dir:{i}", "source": d, "operation": "Remove Empty Directory"}
        for i, d in enumerate(directory_removals)
    ] + [
        {
            "id": f"artist:{u['candidate_idx']}", "operation": "Stamp Artist Attributes",
            "target_name": u["dst_name"], "mbid": u["mbid"],
            "album_ids": u["album_ids"], "item_ids": u["item_ids"],
        }
        for u in db_artist_updates
    ]
    tx = store.create(
        operation_type="Artist Folder Reconcile",
        status="Preview",
        summary=summary_text,
        changes=changes,
        rollback_available=True,
        rollback_reason="Can restore file paths, artwork, duplicate files, directories, and artist attributes captured at Plan.",
        metadata={
            "mutation_family": "artist_folder_reconcile_v1",
            "root": raw_root,
            "mode": mode,
            "candidates": eligible_candidates,
            "requires_review": requires_review,
            "moves_plan": moves_plan,
            "dup_quarantines": dup_quarantines,
            "art_quarantines": art_quarantines,
            "directory_removals": directory_removals,
            "db_item_updates": db_item_updates,
            "db_album_updates": db_album_updates,
            "db_artist_updates": db_artist_updates,
            "resource_keys": sorted(resource_keys),
            "allowed_roots": allowed_roots,
            "cancellable": False,
            "created_at": _now(),
        },
    )
    op_id = tx["id"]

    return {
        "ok": True,
        "operation_id": op_id,
        "mode": mode,
        "candidate_count": len(eligible_candidates),
        "requires_review": requires_review,
        "file_moves_count": len(moves_plan),
        "dup_quarantine_count": len(dup_quarantines),
        "art_quarantine_count": len(art_quarantines),
        "directory_removal_count": len(directory_removals),
        "db_items_count": len(db_item_updates),
        "db_albums_count": len(db_album_updates),
        "db_artist_updates_count": len(db_artist_updates),
        "resource_keys": sorted(resource_keys),
    }


def execute_artist_folder_reconcile_apply(
    store: TransactionStore,
    operation_id: str,
    *,
    music_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
    quarantine_base_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute artist-folder reconciliation with full TOCTOU checks, quarantine, and locking."""
    if not _TRANSACTION_ID_RE.match(operation_id):
        return {"ok": False, "error": "Invalid operation_id format.", "code": "artist_reconcile_invalid_id"}

    with _get_apply_lock(operation_id):
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": "Transaction not found.", "code": "artist_reconcile_not_found"}

        meta = dict(tx.get("metadata") or {})
        mutation_family = meta.get("mutation_family")
        if mutation_family != "artist_folder_reconcile_v1":
            return {"ok": False, "error": "Transaction family mismatch.", "code": "artist_reconcile_family_mismatch"}

        status = tx.get("status")

        if status == "Completed":
            return {
                "ok": True,
                "operation_id": operation_id,
                "status": "Completed",
                "already_completed": True,
            }

        if status not in ("Preview", "Failed"):
            return {"ok": False, "error": "Transaction is not in a state Apply can act on.", "code": "artist_reconcile_invalid_state"}

        resource_keys = meta.get("resource_keys") or []
        allowed_roots_raw = music_allowed_roots or tx.get("allowed_roots") or [str(os.environ.get("MUSIC_ROOT", "/music"))]
        allowed_roots: List[Path] = []
        for r in allowed_roots_raw:
            try:
                p = Path(r).resolve()
                if p.exists():
                    allowed_roots.append(p)
            except Exception:
                pass
        if not allowed_roots:
            allowed_roots = [Path(r) for r in allowed_roots_raw]
        lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")

        def _fail(msg: str, code: str) -> Dict[str, Any]:
            LOG.error("Artist reconcile apply failed for %s: %s (%s)", operation_id, msg, code)
            curr = store.get(operation_id)
            c_meta = curr.get("metadata") or {}
            mutated = bool(c_meta.get("filesystem_mutated") or c_meta.get("db_mutated"))
            store.update(operation_id, status="Failed", logs=[msg])
            return {"ok": False, "error": msg, "code": code, "mutated": mutated, "partial_mutation": mutated, "rollback_available": mutated}

        with _lock_resources(resource_keys):
            moves_plan = meta.get("moves_plan") or []
            dup_quarantines = meta.get("dup_quarantines") or []
            art_quarantines = meta.get("art_quarantines") or []
            directory_removals = meta.get("directory_removals") or []
            db_item_updates = meta.get("db_item_updates") or []
            db_album_updates = meta.get("db_album_updates") or []
            db_artist_updates = meta.get("db_artist_updates") or []

            curr_meta_snapshot = store.get(operation_id).get("metadata", {})
            already_moved_sources = {m["source"] for m in (curr_meta_snapshot.get("moved_records") or [])}
            already_quarantined_sources = {q["source"] for q in (curr_meta_snapshot.get("quarantined_records") or [])}
            db_stage_done = bool(curr_meta_snapshot.get("db_mutated"))

            # Full TOCTOU (dev, inode, size, mtime_ns) plus root/symlink
            # revalidation for every source AND destination parent,
            # immediately before mutation -- SEC-002 Wave 21 final review,
            # findings #15-#18 (Apply only checked size/mtime_ns and never
            # rechecked root/symlink at all).
            if not db_stage_done:
                # TOCTOU Identity Revalidation at Apply time (SEC-002 Wave 27):
                # Re-read source & destination identities from DB/disk before mutating.
                for cand in meta.get("eligible_candidates") or []:
                    src_p = validate_path_under_allowed_roots(cand.get("source_path"), allowed_roots, reject_symlinks=True)
                    dst_p = validate_path_under_allowed_roots(cand.get("target_path"), allowed_roots, reject_symlinks=True)
                    if src_p is None or dst_p is None:
                        return _fail("Candidate path outside allowed roots", "artist_reconcile_path_out_of_root")
                    if src_p.exists() and src_p.is_dir():
                        src_id_now = _derive_artist_folder_identity(src_p, lib_db, allowed_roots=allowed_roots)
                        dst_id_now = _derive_artist_folder_identity(dst_p, lib_db, allowed_roots=allowed_roots) if (dst_p.exists() and dst_p.is_dir()) else {"ids": set(), "album_count": 0}
                        caller_mbid_now = cand.get("identity_evidence", {}).get("caller_mbid") or ""
                        fp_confirmed_now = cand.get("identity_evidence", {}).get("fingerprint_confirmed") is True
                        rec_mbids_now = cand.get("identity_evidence", {}).get("recording_mbids") or []
                        is_merge_now = dst_p.exists() and dst_p.is_dir() and dst_p.resolve(strict=False) != src_p.resolve(strict=False)
                        
                        eligible_now, mbid_now, reason_now = _artist_folder_identity_decision(
                            src_id_now, dst_id_now, is_merge_now, caller_mbid_now, fp_confirmed_now,
                            db_path=lib_db, recording_mbids=rec_mbids_now
                        )
                        if not eligible_now or mbid_now != cand.get("resolved_mbid"):
                            return _fail(f"Artist folder identity changed since Plan for candidate {cand.get('source_name')}.", "artist_reconcile_identity_changed_at_apply")

                for m in moves_plan:
                    if m["source"] in already_moved_sources:
                        continue
                    src_p = Path(m["source"])
                    dst_p = Path(m["destination"])
                    if _resolve_path_role(str(src_p), allowed_roots) is None or not _normpath_within_roots(str(src_p), allowed_roots):
                        return _fail(f"Move source outside allowed roots for item {m.get('candidate_idx')}.", "artist_reconcile_path_out_of_root")
                    if not _normpath_within_roots(str(dst_p.parent), allowed_roots) or (_resolve_path_role(str(dst_p.parent), allowed_roots) is None and dst_p.parent.exists()):
                        return _fail(f"Move destination outside allowed roots for item {m.get('candidate_idx')}.", "artist_reconcile_path_out_of_root")
                    if _path_has_symlink_under(src_p, allowed_roots[0]) or (dst_p.parent.exists() and _path_has_symlink_under(dst_p.parent, allowed_roots[0])):
                        return _fail("Symlink detected on move path.", "artist_reconcile_symlink_rejected")
                    if not src_p.exists():
                        return _fail("Move source file/folder missing.", "artist_reconcile_toctou_mismatch")
                    st = src_p.stat()
                    exp_st = m["stat"]
                    if (st.st_dev != exp_st.get("dev") or st.st_ino != exp_st.get("ino")
                            or st.st_size != exp_st["size"] or st.st_mtime_ns != exp_st["mtime_ns"]):
                        return _fail("Move source changed since plan.", "artist_reconcile_toctou_mismatch")
                    if m["type"] != "rename_dir" and dst_p.exists():
                        return _fail("Move destination now occupied.", "artist_reconcile_toctou_mismatch")

                for dq in dup_quarantines + art_quarantines:
                    if dq["source"] in already_quarantined_sources:
                        continue
                    src_p = Path(dq["source"])
                    if _resolve_path_role(str(src_p), allowed_roots) is None or not _normpath_within_roots(str(src_p), allowed_roots):
                        return _fail("Quarantine source outside allowed roots.", "artist_reconcile_path_out_of_root")
                    if not src_p.exists():
                        continue
                    if _path_has_symlink_under(src_p, allowed_roots[0]):
                        return _fail("Symlink detected on quarantine source.", "artist_reconcile_symlink_rejected")
                    st = src_p.stat()
                    exp_st = dq["stat"]
                    if (st.st_dev != exp_st.get("dev") or st.st_ino != exp_st.get("ino")
                            or st.st_size != exp_st["size"] or st.st_mtime_ns != exp_st["mtime_ns"]):
                        return _fail("Quarantine source changed since plan.", "artist_reconcile_toctou_mismatch")

            store.update(operation_id, status="Running", metadata={**meta, "mutation_started": True})

            def _persist_fs_progress(quarantined: List[Dict[str, Any]], moved: List[Dict[str, Any]], removed: List[str], *, mutated: bool) -> None:
                curr = store.get(operation_id).get("metadata", {})
                update = {**curr, "quarantined_records": quarantined, "moved_records": moved, "removed_dirs": removed}
                if mutated:
                    update["filesystem_mutated"] = True
                    update["mutated"] = True
                store.update(operation_id, metadata=update)

            quarantined_records: List[Dict[str, Any]] = list(curr_meta_snapshot.get("quarantined_records") or [])
            moved_records: List[Dict[str, Any]] = list(curr_meta_snapshot.get("moved_records") or [])
            removed_dirs: List[str] = list(curr_meta_snapshot.get("removed_dirs") or [])

            if not db_stage_done:
                q_base = quarantine_base_root or os.environ.get("RECONCILE_QUARANTINE_DIR", "/config/reconcile_quarantine")
                q_dir = Path(q_base) / operation_id

                try:
                    for kind, items in (("duplicate", dup_quarantines), ("artwork", art_quarantines)):
                        for dq in items:
                            if dq["source"] in already_quarantined_sources:
                                continue
                            src_p = Path(dq["source"])
                            if not (src_p.exists() and src_p.is_file()):
                                continue
                            sub = "dup" if kind == "duplicate" else "art"
                            q_dest = q_dir / sub / dq.get("quarantine_key", src_p.name)
                            q_dest.parent.mkdir(parents=True, exist_ok=True)
                            if q_dest.exists() or q_dest.is_symlink():
                                return _fail("Quarantine destination collision.", "artist_reconcile_filesystem_failed")
                            _safe_rename(src_p, q_dest)
                            quarantined_records.append({"type": kind, "source": str(src_p), "quarantine": str(q_dest)})
                            already_quarantined_sources.add(str(src_p))
                            _persist_fs_progress(quarantined_records, moved_records, removed_dirs, mutated=True)

                    for m in moves_plan:
                        if m["source"] in already_moved_sources:
                            continue
                        src_p = Path(m["source"])
                        dst_p = Path(m["destination"])
                        if not src_p.exists():
                            continue
                        dst_p.parent.mkdir(parents=True, exist_ok=True)
                        if dst_p.exists():
                            return _fail("Move destination collision.", "artist_reconcile_filesystem_failed")
                        _safe_rename(src_p, dst_p)
                        moved_records.append({"type": m["type"], "source": str(src_p), "destination": str(dst_p)})
                        already_moved_sources.add(m["source"])
                        _persist_fs_progress(quarantined_records, moved_records, removed_dirs, mutated=True)

                    for dr in directory_removals:
                        if dr in removed_dirs:
                            continue
                        dr_p = Path(dr)
                        if dr_p.exists() and dr_p.is_dir() and not dr_p.is_symlink() and not any(dr_p.iterdir()):
                            dr_p.rmdir()
                            removed_dirs.append(dr)
                            _persist_fs_progress(quarantined_records, moved_records, removed_dirs, mutated=True)
                except OSError as ex:
                    LOG.error("Artist reconcile apply: filesystem mutation failed for %s: %s", operation_id, ex)
                    _persist_fs_progress(quarantined_records, moved_records, removed_dirs, mutated=bool(quarantined_records or moved_records or removed_dirs))
                    store.update(operation_id, status="Failed", logs=["Filesystem mutation failed."])
                    return {
                        "ok": False, "error": "Filesystem mutation failed.", "code": "artist_reconcile_filesystem_failed",
                        "mutated": True, "partial_mutation": True, "rollback_available": True,
                    }

                # Stage 2: Database operations -- exact bound IDs only,
                # exact rowcount verified per statement (SEC-002 Wave 21
                # final review, findings #25-#27).
                if lib_db and Path(lib_db).exists():
                    try:
                        con = sqlite3.connect(lib_db, timeout=10)
                        try:
                            cur = con.cursor()
                            for u in db_item_updates:
                                cur.execute("UPDATE items SET path=? WHERE id=?", (u["new_path"].encode("utf-8"), u["id"]))
                                if cur.rowcount != 1:
                                    con.rollback()
                                    return _fail("Item path rowcount mismatch.", "artist_reconcile_db_failed")

                            for u in db_album_updates:
                                if u.get("new_artpath"):
                                    cur.execute("UPDATE albums SET path=?, artpath=? WHERE id=?", (u["new_path"].encode("utf-8"), u["new_artpath"].encode("utf-8"), u["id"]))
                                else:
                                    cur.execute("UPDATE albums SET path=? WHERE id=?", (u["new_path"].encode("utf-8"), u["id"]))
                                if cur.rowcount != 1:
                                    con.rollback()
                                    return _fail("Album path rowcount mismatch.", "artist_reconcile_db_failed")

                            for au in db_artist_updates:
                                dst_name = au["dst_name"]
                                mbid = au["mbid"]
                                for aid in au["album_ids"]:
                                    if mbid:
                                        cur.execute(
                                            "UPDATE albums SET albumartist=?, albumartists=?, mb_albumartistid=?, mb_albumartistids=? WHERE id=?",
                                            (dst_name, dst_name, mbid, mbid, aid),
                                        )
                                    else:
                                        cur.execute("UPDATE albums SET albumartist=? WHERE id=?", (dst_name, aid))
                                    if cur.rowcount != 1:
                                        con.rollback()
                                        return _fail("Album artist rowcount mismatch.", "artist_reconcile_db_failed")
                                for iid in au["item_ids"]:
                                    before = au["items_before"].get(iid) or {}
                                    if mbid:
                                        cur.execute(
                                            "UPDATE items SET albumartist=?, albumartists=?, mb_albumartistid=?, mb_albumartistids=? WHERE id=?",
                                            (dst_name, dst_name, mbid, mbid, iid),
                                        )
                                    else:
                                        cur.execute("UPDATE items SET albumartist=? WHERE id=?", (dst_name, iid))
                                    if cur.rowcount != 1:
                                        con.rollback()
                                        return _fail("Item albumartist rowcount mismatch.", "artist_reconcile_db_failed")
                                    # Track-level artist/mb_artistid is only
                                    # relabeled when it currently matches the
                                    # source folder's own artist name --
                                    # never a blind blanket rewrite, so a
                                    # differently-credited track (e.g. a
                                    # featured/compilation credit) is left
                                    # alone.
                                    src_folder_name = au.get("src_name") or ""
                                    if before.get("artist") == src_folder_name or not src_folder_name:
                                        if mbid:
                                            cur.execute(
                                                "UPDATE items SET artist=?, artists=?, mb_artistid=?, mb_artistids=? WHERE id=?",
                                                (dst_name, dst_name, mbid, mbid, iid),
                                            )
                                        else:
                                            cur.execute("UPDATE items SET artist=? WHERE id=?", (dst_name, iid))
                                        if cur.rowcount != 1:
                                            con.rollback()
                                            return _fail("Item artist rowcount mismatch.", "artist_reconcile_db_failed")

                            con.commit()
                        finally:
                            con.close()
                    except sqlite3.Error as ex:
                        LOG.error("Artist reconcile apply: database update failed for %s: %s", operation_id, ex)
                        store.update(operation_id, status="Failed", logs=["Database update failed."], metadata={**store.get(operation_id).get("metadata", {}), "partial_mutation": True})
                        return {"ok": False, "error": "Database update failed.", "code": "artist_reconcile_db_failed", "mutated": True, "partial_mutation": True, "rollback_available": True}

                store.update(operation_id, metadata={**store.get(operation_id).get("metadata", {}), "db_mutated": True, "mutated": True})

            # Stage 3: real post-write verification -- DB state and final
            # filesystem state, not just "commit succeeded" (SEC-002 Wave
            # 21 final review, finding #29).
            try:
                if lib_db and Path(lib_db).exists():
                    con = sqlite3.connect(lib_db, timeout=10)
                    con.row_factory = sqlite3.Row
                    try:
                        cur = con.cursor()
                        for u in db_item_updates:
                            cur.execute("SELECT path FROM items WHERE id=?", (u["id"],))
                            row = cur.fetchone()
                            got = (row["path"].decode("utf-8", "replace") if row and isinstance(row["path"], bytes) else (row["path"] if row else None))
                            if not row or got != u["new_path"]:
                                return _fail("Post-write verification failed for item path.", "artist_reconcile_verification_failed")
                        for u in db_album_updates:
                            cur.execute("SELECT path FROM albums WHERE id=?", (u["id"],))
                            row = cur.fetchone()
                            got = (row["path"].decode("utf-8", "replace") if row and isinstance(row["path"], bytes) else (row["path"] if row else None))
                            if not row or got != u["new_path"]:
                                return _fail("Post-write verification failed for album path.", "artist_reconcile_verification_failed")
                        for au in db_artist_updates:
                            for aid in au["album_ids"]:
                                cur.execute("SELECT albumartist, mb_albumartistid FROM albums WHERE id=?", (aid,))
                                row = cur.fetchone()
                                if not row or row["albumartist"] != au["dst_name"] or (au["mbid"] and str(row["mb_albumartistid"] or "").lower() != au["mbid"]):
                                    return _fail("Post-write verification failed for album artist attributes.", "artist_reconcile_verification_failed")
                    finally:
                        con.close()

                for qr in quarantined_records:
                    if Path(qr["source"]).exists():
                        return _fail("Verification failed: quarantined original still present.", "artist_reconcile_verification_failed")
                    if not Path(qr["quarantine"]).exists():
                        return _fail("Verification failed: quarantine copy missing.", "artist_reconcile_verification_failed")
                for mr in moved_records:
                    if not Path(mr["destination"]).exists():
                        return _fail("Verification failed: move destination missing.", "artist_reconcile_verification_failed")
            except sqlite3.Error as ex:
                LOG.error("Artist reconcile apply: verification query failed for %s: %s", operation_id, ex)
                return _fail("Post-write verification query failed.", "artist_reconcile_verification_failed")

            store.update(
                operation_id,
                status="Completed",
                metadata={
                    **store.get(operation_id).get("metadata", {}),
                    "rollback_available": True,
                    "completed_at": _now(),
                },
                logs=["Artist folder reconciliation applied successfully."],
            )

            return {
                "ok": True,
                "operation_id": operation_id,
                "status": "Completed",
                "mutated": True,
                "moved_files": len(moved_records),
                "quarantined_files": len(quarantined_records),
                "removed_dirs": len(removed_dirs),
            }


def rollback_artist_folder_reconcile(
    store: TransactionStore,
    operation_id: str,
    *,
    music_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
    quarantine_base_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Roll back an applied artist-folder reconciliation transaction."""
    if not _TRANSACTION_ID_RE.match(operation_id):
        return {"ok": False, "error": "Invalid operation_id format.", "code": "artist_reconcile_invalid_id"}

    with _get_apply_lock(operation_id):
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": "Transaction not found.", "code": "artist_reconcile_not_found"}

        meta = dict(tx.get("metadata") or {})
        mutation_family = meta.get("mutation_family")
        if mutation_family != "artist_folder_reconcile_v1":
            return {"ok": False, "error": "Transaction family mismatch.", "code": "artist_reconcile_family_mismatch"}

        status = tx.get("status")

        if status == "Rolled Back":
            return {"ok": True, "operation_id": operation_id, "status": "Rolled Back", "already_rolled_back": True}

        if not meta.get("mutated") and not meta.get("filesystem_mutated") and not meta.get("db_mutated"):
            return {"ok": False, "error": "Transaction was never mutated.", "code": "artist_reconcile_not_mutated"}

        resource_keys = meta.get("resource_keys") or []
        lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")
        allowed_roots_raw = music_allowed_roots or meta.get("allowed_roots") or [str(os.environ.get("MUSIC_ROOT", "/music"))]
        allowed_roots: List[Path] = []
        for r in allowed_roots_raw:
            try:
                p = Path(r).resolve()
                if p.exists():
                    allowed_roots.append(p)
            except Exception:
                pass
        if not allowed_roots:
            allowed_roots = [Path(r) for r in allowed_roots_raw]

        db_item_updates = meta.get("db_item_updates") or []
        db_album_updates = meta.get("db_album_updates") or []
        db_artist_updates = meta.get("db_artist_updates") or []
        moved_records = meta.get("moved_records") or []
        removed_dirs = meta.get("removed_dirs") or []
        quarantined_records = meta.get("quarantined_records") or []

        with _lock_resources(resource_keys):
            # Current-state validation: refuse to clobber a later,
            # unrelated legitimate change (SEC-002 Wave 21 final review,
            # finding #42). Check the DB-mutated destinations this
            # transaction itself produced still hold the values it wrote.
            if lib_db and Path(lib_db).exists() and meta.get("db_mutated"):
                con = sqlite3.connect(lib_db, timeout=10)
                con.row_factory = sqlite3.Row
                try:
                    cur = con.cursor()
                    for u in db_item_updates:
                        cur.execute("SELECT path FROM items WHERE id=?", (u["id"],))
                        row = cur.fetchone()
                        got = (row["path"].decode("utf-8", "replace") if row and isinstance(row["path"], bytes) else (row["path"] if row else None))
                        if row and got != u["new_path"]:
                            store.update(operation_id, logs=[f"Rollback precondition failed: item {u['id']} changed after apply."])
                            return {"ok": False, "error": "Item changed after apply; refusing to overwrite a later change.", "code": "artist_reconcile_rollback_stale"}
                finally:
                    con.close()

            db_restored = 0
            db_failed = 0
            files_restored = 0
            files_failed = 0

            # Filesystem first: restore moved files, recreate removed
            # directories, and restore quarantined files -- BEFORE DB rows
            # are pointed back at them (SEC-002 Wave 21 final review,
            # finding #40, the same ordering bug Wave 20 already fixed).
            q_base = quarantine_base_root or os.environ.get("RECONCILE_QUARANTINE_DIR", "/config/reconcile_quarantine")
            try:
                q_base_root = Path(q_base).resolve()
            except Exception:
                q_base_root = Path(q_base)

            for m in reversed(moved_records):
                src_p = Path(m["destination"])
                dst_p = Path(m["source"])
                base_root = _resolve_path_role(str(dst_p), allowed_roots)
                if base_root is None or not _normpath_within_roots(str(dst_p), allowed_roots):
                    files_failed += 1
                    continue
                if _resolve_path_role(str(src_p), allowed_roots) is None or not _normpath_within_roots(str(src_p), allowed_roots):
                    files_failed += 1
                    continue
                if dst_p.exists() or dst_p.is_symlink():
                    files_failed += 1
                    continue
                if not src_p.exists():
                    files_failed += 1
                    continue
                try:
                    dst_p.parent.mkdir(parents=True, exist_ok=True)
                    if _path_has_symlink_under(dst_p.parent, base_root):
                        files_failed += 1
                        continue
                    _safe_rename(src_p, dst_p)
                    files_restored += 1
                except OSError:
                    files_failed += 1

            for dr in removed_dirs:
                dr_p = Path(dr)
                base_root = _resolve_path_role(dr, allowed_roots)
                if base_root is None or not _normpath_within_roots(dr, allowed_roots):
                    continue
                try:
                    dr_p.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass

            for q in quarantined_records:
                q_p = Path(q["quarantine"])
                orig_p = Path(q["source"])
                base_root = _resolve_path_role(str(orig_p), allowed_roots)
                if base_root is None or not _normpath_within_roots(str(orig_p), allowed_roots):
                    files_failed += 1
                    continue
                if (_resolve_path_role(str(q_p), [q_base_root]) is None
                        or not _normpath_within_roots(str(q_p), [q_base_root])):
                    files_failed += 1
                    continue
                if orig_p.exists() or orig_p.is_symlink():
                    files_failed += 1
                    continue
                if not q_p.exists():
                    files_failed += 1
                    continue
                try:
                    orig_p.parent.mkdir(parents=True, exist_ok=True)
                    if _path_has_symlink_under(orig_p.parent, base_root):
                        files_failed += 1
                        continue
                    _safe_rename(q_p, orig_p)
                    files_restored += 1
                except OSError:
                    files_failed += 1

            # Then DB: paths, artpaths, and (SEC-002 Wave 21 final review,
            # finding #28) the artist/MBID attributes Apply's stamping
            # stage changed -- the original implementation never restored
            # these at all.
            if lib_db and Path(lib_db).exists():
                try:
                    con = sqlite3.connect(lib_db, timeout=10)
                    try:
                        cur = con.cursor()
                        for u in db_item_updates:
                            cur.execute("UPDATE items SET path=? WHERE id=?", (u["old_path"].encode("utf-8"), u["id"]))
                            db_restored += 1 if cur.rowcount == 1 else 0
                            db_failed += 0 if cur.rowcount == 1 else 1
                        for u in db_album_updates:
                            if u.get("old_artpath"):
                                cur.execute("UPDATE albums SET path=?, artpath=? WHERE id=?", (u["old_path"].encode("utf-8"), u["old_artpath"].encode("utf-8"), u["id"]))
                            else:
                                cur.execute("UPDATE albums SET path=? WHERE id=?", (u["old_path"].encode("utf-8"), u["id"]))
                            db_restored += 1 if cur.rowcount == 1 else 0
                            db_failed += 0 if cur.rowcount == 1 else 1
                        for au in db_artist_updates:
                            for aid in au["album_ids"]:
                                before = au["albums_before"].get(aid) or au["albums_before"].get(str(aid))
                                if not before:
                                    db_failed += 1
                                    continue
                                cur.execute(
                                    "UPDATE albums SET albumartist=?, albumartists=?, mb_albumartistid=?, mb_albumartistids=? WHERE id=?",
                                    (before["albumartist"], before["albumartists"], before["mb_albumartistid"], before["mb_albumartistids"], aid),
                                )
                                db_restored += 1 if cur.rowcount == 1 else 0
                                db_failed += 0 if cur.rowcount == 1 else 1
                            for iid in au["item_ids"]:
                                before = au["items_before"].get(iid) or au["items_before"].get(str(iid))
                                if not before:
                                    db_failed += 1
                                    continue
                                cur.execute(
                                    "UPDATE items SET albumartist=?, albumartists=?, mb_albumartistid=?, mb_albumartistids=?, "
                                    "artist=?, artists=?, mb_artistid=?, mb_artistids=? WHERE id=?",
                                    (
                                        before["albumartist"], before["albumartists"], before["mb_albumartistid"], before["mb_albumartistids"],
                                        before["artist"], before["artists"], before["mb_artistid"], before["mb_artistids"], iid,
                                    ),
                                )
                                db_restored += 1 if cur.rowcount == 1 else 0
                                db_failed += 0 if cur.rowcount == 1 else 1
                        con.commit()
                    finally:
                        con.close()
                except sqlite3.Error as ex:
                    LOG.error("Artist reconcile rollback: DB restore failed for %s: %s", operation_id, ex)
                    store.update(operation_id, logs=["Rollback database restore failed."])
                    return {"ok": False, "error": "Rollback database restore failed.", "code": "artist_reconcile_rollback_db_failed"}

            full_success = files_failed == 0 and db_failed == 0
            any_restored = files_restored > 0 or db_restored > 0
            final_status = "Rolled Back" if full_success else ("Partially Rolled Back" if any_restored else "Failed")

            store.update(
                operation_id,
                status=final_status,
                metadata={**meta, "rollback_available": False, "rolled_back_at": _now()},
                logs=[f"Artist folder reconciliation rollback completed with status {final_status}."],
            )

            return {
                "ok": final_status == "Rolled Back",
                "operation_id": operation_id,
                "status": final_status,
                "partial_mutation": final_status == "Partially Rolled Back",
                "restored_moves": files_restored,
                "restored_quarantine": len(quarantined_records),
                "db_restored": db_restored,
                "db_failed": db_failed,
            }


# ── Helper functions for artist folder reconciliation ─────────────────────────

# SEC-002 Wave 21 final review: the original implementation called
# _album_cleanup_file_info/_album_cleanup_verified_same_file/
# _album_cleanup_duplicate_file_choice/_unique_dest/_MB_UUID_RE/_ART_EXTS
# without ever defining or importing any of them in this module -- all six
# names exist only in app.py (the Web Manager), which this engine module
# must never depend on (it also runs inside the engine container, where
# app.py is never imported -- see the Wave 15 precedent comment on
# execute_import_review_cleanup_plan). Every code path that reached these
# names (any artist-folder merge where the destination already contains
# files, or any MBID stamping) would raise NameError at actual runtime.
# Engine-local equivalents, ported faithfully from app.py's originals:

_ART_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})
_MB_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def _rows_by_path_prefix(
    con: sqlite3.Connection, table: str, cols: str, path_col: str, prefix: bytes,
    *, path_result_col: str = "path",
) -> List[sqlite3.Row]:
    """Return rows from `table` whose `path_col` starts with `prefix` (bytes).

    SEC-002 Wave 21 final review, CI-only regression: SQL `LIKE` against a
    BLOB-stored path column with a BLOB pattern is not a reliable prefix
    match -- its behavior (textual vs. binary comparison, implicit
    type handling) is undocumented and version-dependent, and it silently
    returned zero rows on the CI runner's SQLite build while appearing to
    work locally. A `LIKE` pattern also mistreats literal `%`/`_` bytes in
    real folder/file names as wildcards. Avoid both problems: narrow with a
    binary-collation range scan (`path_col >= prefix AND path_col < prefix +
    0xFF`, safe because prefix is valid UTF-8 and 0xFF never appears in
    valid UTF-8), then confirm the exact byte-prefix match in Python.

    `path_col` is the (possibly table-qualified) column used in the WHERE
    clause; `path_result_col` is the column/alias name to read the path back
    from in the result rows (defaults to `path_col` itself when unqualified
    and unaliased -- callers that alias or table-qualify the select must
    pass it explicitly).
    """
    upper = prefix + b"\xff\xff\xff\xff"
    rows = con.execute(
        f"SELECT {cols} FROM {table} WHERE {path_col} >= ? AND {path_col} < ?",
        (prefix, upper),
    ).fetchall()
    out = []
    for r in rows:
        raw_p = r[path_result_col]
        pb = raw_p if isinstance(raw_p, bytes) else str(raw_p or "").encode("utf-8", "replace")
        if pb.startswith(prefix):
            out.append(r)
    return out


def _unique_dest(path: Path) -> Path:
    if not path.exists():
        return path
    base = path.with_suffix("")
    suffix = path.suffix
    n = 1
    while True:
        cand = Path(f"{base}.{n}{suffix}")
        if not cand.exists():
            return cand
        n += 1


def _artist_folder_file_hash(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    h = hashlib.sha1()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _album_cleanup_verified_same_file(a: Path, b: Path) -> bool:
    """Content-identity check (size + full SHA1), not filename/title --
    this is what actually justifies quarantining one copy as a duplicate
    (SEC-002 Wave 21 final review, 'duplicate file policy must be
    identity-safe')."""
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
    except OSError:
        return False
    a_hash = _artist_folder_file_hash(a)
    b_hash = _artist_folder_file_hash(b)
    return bool(a_hash and b_hash and a_hash == b_hash)


def _album_cleanup_duplicate_file_choice(candidate: Path, existing: Path) -> str:
    """Artwork-only quality choice (this helper is only ever invoked for
    files both classified as artwork by _ART_EXTS) -- larger file size is
    the same proxy app.py's original quality tuple reduces to once the
    lossless/bitrate fields are inapplicable to image files."""
    try:
        cand_size = candidate.stat().st_size
    except OSError:
        cand_size = -1
    try:
        exist_size = existing.stat().st_size
    except OSError:
        exist_size = -1
    return "candidate" if cand_size > exist_size else "existing"


def _derive_artist_folder_identity(
    folder_path: Path,
    lib_db: str,
    allowed_roots: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Independently establish Artist ID(s) for a folder directly from
    Beets DB state (distinct non-blank mb_albumartistid values among
    albums whose item paths live under this folder) -- SEC-002 Wave 21
    final review: a caller-supplied MBID is evidence, never authority.
    More than one distinct established id under the same folder is itself
    a conflict signal (returned via "ids" having len > 1)."""
    result: Dict[str, Any] = {"ids": set(), "album_count": 0}
    if not lib_db:
        return result
    roots = allowed_roots or [str(os.environ.get("MUSIC_ROOT", "/music"))]
    valid_folder = validate_path_under_allowed_roots(folder_path, roots, reject_symlinks=True)
    if valid_folder is None or not valid_folder.exists():
        return result
    valid_db = validate_path_under_allowed_roots(lib_db, [str(Path(lib_db).parent)], reject_symlinks=False) or Path(lib_db)
    if not valid_db.exists():
        return result
    prefix = str(valid_folder).encode("utf-8") + os.sep.encode("utf-8")
    try:
        con = sqlite3.connect(str(valid_db), timeout=10)
        con.row_factory = sqlite3.Row
        try:
            rows = _rows_by_path_prefix(
                con, "items JOIN albums ON items.album_id = albums.id",
                "DISTINCT albums.id AS album_id, albums.mb_albumartistid, items.path AS item_path",
                "items.path", prefix, path_result_col="item_path",
            )
        finally:
            con.close()
    except sqlite3.Error as ex:
        LOG.warning("Artist folder identity derivation failed for %s: %s", folder_path, ex)
        return result
    album_ids: set = set()
    for row in rows:
        try:
            album_ids.add(int(row["album_id"]))
        except (TypeError, ValueError):
            pass
        aid = str(row["mb_albumartistid"] or "").strip().lower()
        if _MB_UUID_RE.match(aid):
            result["ids"].add(aid)
    result["album_count"] = len(album_ids)
    return result


def _extract_recording_mbids(
    cand: Dict[str, Any],
    src_path: Path,
    db_path: Optional[str] = None,
    allowed_roots: Optional[List[str]] = None,
) -> List[str]:
    """Extracts MusicBrainz Recording IDs established under src_path from local DB items.
    Candidate payload recording IDs are treated as suggestions/hints ONLY and must be
    independently corroborated by established engine item track state under src_path.
    """
    lib_db = os.environ.get("BEETS_LIBRARY_DB", "")
    if db_path and db_path == os.environ.get("BEETS_LIBRARY_DB", ""):
        lib_db = db_path
    if not lib_db:
        return []

    roots = allowed_roots or [str(os.environ.get("MUSIC_ROOT", "/music"))]
    valid_src = validate_path_under_allowed_roots(src_path, roots, reject_symlinks=True)
    if valid_src is None or not valid_src.exists():
        return []

    valid_db = validate_path_under_allowed_roots(lib_db, [str(Path(lib_db).parent)], reject_symlinks=False) or Path(lib_db)
    if not valid_db.exists():
        return []

    established_rec_ids: set = set()
    try:
        con = sqlite3.connect(str(valid_db), timeout=5)
        con.row_factory = sqlite3.Row
        try:
            prefix = str(valid_src) + os.sep
            rows = _rows_by_path_prefix(
                con, "items", "DISTINCT mb_trackid, path", "path", prefix, path_result_col="path"
            )
            for r in rows:
                mb_tid = str(r["mb_trackid"] or "").strip().lower()
                if _MB_UUID_RE.match(mb_tid):
                    established_rec_ids.add(mb_tid)
        finally:
            con.close()
    except Exception:
        pass

    raw = cand.get("recording_mbids") or cand.get("recording_mbid") or cand.get("mb_trackid") or cand.get("recording_ids") or []
    if isinstance(raw, str):
        raw = [raw]
    caller_hints: set = set()
    for item in raw:
        s = str(item or "").strip().lower()
        if _MB_UUID_RE.match(s):
            caller_hints.add(s)

    if caller_hints:
        # Candidate hints are ONLY included if corroborated by established engine item MBIDs
        return sorted(established_rec_ids.intersection(caller_hints))

    return sorted(established_rec_ids)


def _verify_mb_artist_recording_credit(
    caller_mbid: str,
    recording_mbids: Optional[List[str]] = None,
    mb_verifier: Optional[Callable[[str, List[str]], bool]] = None,
) -> bool:
    """Verifies engine-side whether caller_mbid is authorized as an artist credit
    for candidate recording(s) using MusicBrainz Recording artist-credit evidence.

    Returns True ONLY IF every established MusicBrainz Recording under the source folder
    has an artist credit matching caller_mbid.
    """
    if not caller_mbid or not _MB_UUID_RE.match(caller_mbid):
        return False

    if mb_verifier is not None:
        return bool(mb_verifier(caller_mbid, recording_mbids or []))

    if not recording_mbids:
        return False

    try:
        from helpers_mb import _fetch_mb_recording_details, _MB_UUID_RE as HELPERS_UUID_RE
        target_mbid = caller_mbid.lower()
        match_count = 0
        for rec_id in recording_mbids:
            if not rec_id or not HELPERS_UUID_RE.match(rec_id):
                return False
            details = _fetch_mb_recording_details(rec_id)
            if not details or not isinstance(details, dict):
                return False
            rec_artist_id = str(details.get("mb_artistid") or "").lower()
            found = rec_artist_id == target_mbid
            if not found:
                for rel in details.get("linked_releases") or []:
                    rel_artist = str(rel.get("mb_artistid") or rel.get("artist_id") or "").lower()
                    if rel_artist == target_mbid:
                        found = True
                        break
            if not found:
                return False
            match_count += 1
        return match_count > 0
    except Exception as ex:
        LOG.warning("MusicBrainz recording credit verification exception: %s", ex)
        return False


def _artist_folder_identity_decision(
    src_identity: Dict[str, Any],
    dst_identity: Dict[str, Any],
    is_merge: bool,
    caller_mbid: str,
    fingerprint_confirmed: bool,
    db_path: Optional[str] = None,
    recording_mbids: Optional[List[str]] = None,
    mb_verifier: Optional[Callable[[str, List[str]], bool]] = None,
) -> Tuple[bool, str, str]:
    """Returns (eligible, resolved_mbid, reason_code_if_not_eligible).

    Strict Identity Authority Policy (SEC-002 Wave 27):
    - Fingerprint/AcoustID evidence is recording evidence. It may SUPPORT identity
      verification; it MAY NOT AUTHORIZE an artist identity mutation by itself.
    - An identity-changing artist merge requires a canonical MusicBrainz Artist ID
      established in DB state or verified via MusicBrainz Recording artist-credit evidence.
    - CASE A: source MBID == destination MBID -> eligible.
    - CASE B: source MBID != destination MBID -> hard conflict, not eligible.
    - CASE C: source has MBID, destination blank -> source MBID is canonical;
              caller MBID (if sent) must match source MBID.
    - CASE D: source blank, destination has MBID -> destination MBID is canonical;
              caller MBID (if sent) must match destination MBID.
    - CASE E: source blank, destination blank, fingerprint agrees, caller sends MBID ->
              requires MusicBrainz recording-to-artist credit verification.
              If verified: eligible. Otherwise: review required.
    - CASE F: source blank, destination blank, fingerprint agrees, no canonical MBID ->
              review required.
    - CASE G: AI / fuzzy-name agreement only -> review required.
    """
    src_ids = src_identity.get("ids") or set()
    dst_ids = dst_identity.get("ids") or set()
    if len(src_ids) > 1 or len(dst_ids) > 1:
        return False, "", "artist_reconcile_identity_conflict"
    src_id = next(iter(src_ids), "")
    dst_id = next(iter(dst_ids), "")
    caller_mbid = caller_mbid if _MB_UUID_RE.match(caller_mbid or "") else ""

    if is_merge:
        # CASE A & B: Both source and destination have established MBIDs
        if src_id and dst_id:
            if src_id != dst_id:
                return False, "", "artist_reconcile_identity_conflict"
            return True, src_id, ""

        # CASE C: Source has MBID, destination is blank
        if src_id and not dst_id:
            if caller_mbid and caller_mbid != src_id:
                return False, "", "artist_reconcile_identity_conflict"
            return True, src_id, ""

        # CASE D: Source is blank, destination has MBID
        if not src_id and dst_id:
            if caller_mbid and caller_mbid != dst_id:
                return False, "", "artist_reconcile_identity_conflict"
            return True, dst_id, ""

        # CASES E, F, G: Neither side has an established MBID in the DB
        if not src_id and not dst_id:
            if fingerprint_confirmed and caller_mbid:
                if _verify_mb_artist_recording_credit(caller_mbid, recording_mbids, mb_verifier):
                    return True, caller_mbid, ""
                return False, "", "artist_reconcile_requires_review"
            return False, "", "artist_reconcile_requires_review"

    # Pure rename (no pre-existing destination directory)
    if src_id:
        if caller_mbid and src_id != caller_mbid:
            return False, "", "artist_reconcile_identity_conflict"
        return True, src_id, ""
    else:
        if caller_mbid:
            if _verify_mb_artist_recording_credit(caller_mbid, recording_mbids, mb_verifier):
                return True, caller_mbid, ""
            return False, "", "artist_reconcile_requires_review"
        return True, "", ""


def _artist_folder_quarantine_key(path: Path, root_path: Path) -> str:
    """Stable, collision-free quarantine leaf name -- SEC-002 Wave 21 final
    review: using only the basename let two different album subdirectories
    both named "folder.jpg" collide in the same quarantine bucket. The
    path relative to the scan root is unique by construction."""
    try:
        rel = path.relative_to(root_path)
    except ValueError:
        rel = Path(path.name)
    safe = str(rel).replace(os.sep, "__").replace("/", "__")
    return safe or path.name


def _build_artist_folder_move_graph(
    src: Path,
    dst: Path,
    root_path: Path,
    candidate_idx: int,
    moves_plan: List[Dict[str, Any]],
    dup_quarantines: List[Dict[str, Any]],
    art_quarantines: List[Dict[str, Any]],
    directory_removals: List[str],
) -> None:
    """Recursively build a non-mutating move graph for merging src into dst."""
    for child in sorted(src.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())):
        target = dst / child.name
        if child.is_dir():
            _build_artist_folder_move_graph(
                child, target, root_path, candidate_idx,
                moves_plan, dup_quarantines, art_quarantines, directory_removals
            )
            directory_removals.append(str(child))
            continue

        if not child.is_file():
            continue

        final_dest = target
        if target.exists():
            if _album_cleanup_verified_same_file(child, target):
                st = child.stat()
                dup_quarantines.append({
                    "candidate_idx": candidate_idx,
                    "source": str(child),
                    "quarantine_key": _artist_folder_quarantine_key(child, root_path),
                    "stat": {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns},
                })
                continue

            if child.suffix.lower() in _ART_EXTS and target.suffix.lower() in _ART_EXTS:
                choice = _album_cleanup_duplicate_file_choice(child, target)
                if choice == "candidate":
                    tgt_st = target.stat()
                    art_quarantines.append({
                        "candidate_idx": candidate_idx,
                        "source": str(target),
                        "quarantine_key": _artist_folder_quarantine_key(target, root_path),
                        "stat": {"dev": tgt_st.st_dev, "ino": tgt_st.st_ino, "size": tgt_st.st_size, "mtime_ns": tgt_st.st_mtime_ns},
                    })
                    final_dest = target
                else:
                    c_st = child.stat()
                    art_quarantines.append({
                        "candidate_idx": candidate_idx,
                        "source": str(child),
                        "quarantine_key": _artist_folder_quarantine_key(child, root_path),
                        "stat": {"dev": c_st.st_dev, "ino": c_st.st_ino, "size": c_st.st_size, "mtime_ns": c_st.st_mtime_ns},
                    })
                    continue
            else:
                final_dest = _unique_dest(target)

        st = child.stat()
        moves_plan.append({
            "candidate_idx": candidate_idx,
            "type": "move_file" if final_dest == target else "move_unique",
            "source": str(child),
            "destination": str(final_dest),
            "stat": {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns},
        })


_ENGINE_STAMP_UUID_IN_NAME_RE = re.compile(
    r'\s*(?:\{|\()[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:\}|\))\s*$',
    re.IGNORECASE,
)
_ENGINE_STAMP_UUID_CAPTURE_RE = re.compile(
    r'\s*(?:\{|\()([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:\}|\))\s*$',
    re.IGNORECASE,
)


def _artist_folder_name_without_mbid(name: str) -> str:
    return _ENGINE_STAMP_UUID_IN_NAME_RE.sub("", str(name or "")).strip() or str(name or "").strip()


def _artist_folder_merge_key(name: str) -> str:
    text = _artist_folder_name_without_mbid(name).casefold().replace("&", "and")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", text)


def _safe_artist_folder_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"\\|?*\x00-\x1f]', "_", str(name or "")).strip()
    cleaned = cleaned.replace("/", "_").rstrip(". ")
    return cleaned or "Unknown Artist"


def _artist_folder_canonical_name(artist_name: str, mb_artistid: str = "") -> str:
    base = _safe_artist_folder_name(artist_name)
    mbid = str(mb_artistid or "").strip().lower()
    if _MB_UUID_RE.match(mbid):
        return f"{base} ({mbid})"
    return base


def _stamp_artist_folder_album_mbid_counts(
    root: Path,
    folders: List[Path],
    lib_db: str,
) -> Tuple[Dict[str, Dict[str, set]], Dict[str, int], str]:
    folder_by_name = {folder.name: folder for folder in folders}
    root_abs = root.resolve(strict=False)
    music_root_abs = Path(os.environ.get("MUSIC_ROOT", "/music")).resolve(strict=False)
    album_ids_by_folder: Dict[str, set] = {}
    mbid_album_ids_by_folder: Dict[str, Dict[str, set]] = {}
    try:
        con = sqlite3.connect(lib_db, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT DISTINCT a.id AS album_id, a.mb_albumartistid, i.path "
                "FROM albums a "
                "JOIN items i ON i.album_id = a.id "
                "WHERE a.mb_albumartistid IS NOT NULL AND a.mb_albumartistid != ''"
            ).fetchall()
        finally:
            con.close()
    except Exception as ex:
        return {}, {}, str(ex)

    for row in rows:
        raw_path = row["path"]
        text = (raw_path.decode("utf-8", "replace") if isinstance(raw_path, bytes) else str(raw_path)).replace("\\", "/").strip()
        if not text:
            continue
        path_value = Path(text)
        candidates = [path_value] if path_value.is_absolute() else [music_root_abs / text, root_abs / text]
        folder = None
        for candidate in candidates:
            try:
                rel = candidate.resolve(strict=False).relative_to(root_abs)
            except Exception:
                continue
            if rel.parts:
                folder = folder_by_name.get(rel.parts[0])
                if folder is not None:
                    break
        if folder is None and not path_value.is_absolute():
            parts = [p for p in text.split("/") if p]
            if parts:
                folder = folder_by_name.get(parts[0])
        if folder is None:
            continue
        try:
            album_id = int(row["album_id"] or 0)
        except Exception:
            album_id = 0
        if not album_id:
            continue
        folder_key = str(folder)
        album_ids_by_folder.setdefault(folder_key, set()).add(album_id)
        aid = str(row["mb_albumartistid"] or "").strip().lower()
        if _MB_UUID_RE.match(aid):
            mbid_album_ids_by_folder.setdefault(folder_key, {}).setdefault(aid, set()).add(album_id)

    album_totals = {folder_key: len(album_ids) for folder_key, album_ids in album_ids_by_folder.items()}
    return mbid_album_ids_by_folder, album_totals, ""


def _engine_scan_artist_folder_groups(
    root_path: Path,
    lib_db: str,
    only_keys: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Engine helper to discover duplicate artist folder candidate groups."""
    folders = sorted(
        [p for p in root_path.iterdir() if p.is_dir() and not p.name.startswith(".")],
        key=lambda p: p.name.casefold(),
    )
    by_key: Dict[str, List[Path]] = {}
    for f in folders:
        key = _artist_folder_merge_key(f.name)
        if key:
            by_key.setdefault(key, []).append(f)

    candidates = []
    for key, group in by_key.items():
        if len(group) < 2:
            continue
        if only_keys and key not in only_keys:
            continue
        canonical = group[0]
        for src in group[1:]:
            candidates.append({
                "key": key,
                "source_path": str(src),
                "source_name": src.name,
                "target_path": str(canonical),
                "target_name": canonical.name,
            })
    return candidates


def _engine_stamp_artist_folder_scan(root_path: Path, lib_db: str) -> List[Dict[str, Any]]:
    """Engine helper to discover MBID stamping candidate folders.

    SEC-002 Wave 21 final review, findings #11/#37: the original threshold
    (a single Artist ID covering >=75% of a folder's albums) meant a folder
    with, say, 3 albums under Artist ID A and 1 album under a DIFFERENT,
    already-established Artist ID B would still produce a stamping
    candidate for A -- and Apply's (also-fixed) exact-bound-ID stamping
    would then overwrite that one album's already-correct, conflicting ID
    B. A known conflicting nonblank Artist ID must never be overwritten
    automatically. Candidates are now only produced when a folder's
    tagged albums show EXACTLY ONE distinct non-blank Artist ID (any
    number of untagged/blank albums alongside it is fine -- those are
    filled, not overwritten); more than one distinct id present in the
    same folder is a conflict and produces no candidate at all for that
    folder.
    """
    folders = sorted(
        [p for p in root_path.iterdir() if p.is_dir() and not p.name.startswith(".")],
        key=lambda p: p.name.casefold(),
    )
    mbid_counts, folder_totals, _err = _stamp_artist_folder_album_mbid_counts(root_path, folders, lib_db)
    candidates = []
    for f in folders:
        f_key = str(f)
        mbids_dict = mbid_counts.get(f_key) or {}
        tot = folder_totals.get(f_key) or 0
        if not tot or len(mbids_dict) != 1:
            continue
        mbid = next(iter(mbids_dict))
        canon_name = _artist_folder_canonical_name(f.name, mbid)
        target_p = root_path / canon_name
        if target_p.resolve(strict=False) != f.resolve(strict=False):
            candidates.append({
                "key": _artist_folder_merge_key(f.name),
                "source_path": str(f),
                "source_name": f.name,
                "target_path": str(target_p),
                "target_name": canon_name,
                "mbid": mbid,
            })
    return candidates


# ── album_maintenance_v1 ──────────────────────────────────────────────────────

def create_album_maintenance_plan(
    store: TransactionStore,
    payload: Dict[str, Any],
    *,
    music_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
    quarantine_base_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a non-mutating preview plan for album maintenance operations.

    Handles whole-album deduplication, selective track retirement, album cleanup
    issue resolution, and filename normalization under an engine-owned boundary.
    """
    mode = str(payload.get("mode") or "deduplicate").strip()
    _ALBUM_MAINTENANCE_MODES = frozenset({"remove_tracks", "deduplicate", "filename_cleanup"})
    if mode not in _ALBUM_MAINTENANCE_MODES:
        # SEC-002 Wave 22 final review, finding #14/#15: "cleanup_issue" and
        # any other unimplemented mode must never silently fall through to
        # an empty, still-"successful" transaction -- fail closed instead
        # of advertising a contract this function does not implement.
        return {"ok": False, "error": f"Unsupported album maintenance mode: {mode}", "code": "album_maintenance_invalid_mode"}
    allowed_roots = music_allowed_roots or [str(os.environ.get("MUSIC_ROOT", "/music"))]
    lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")
    if lib_db and not Path(lib_db).exists():
        lib_db = ""

    moves_plan: List[Dict[str, Any]] = []
    dup_quarantines: List[Dict[str, Any]] = []
    directory_removals: List[str] = []
    db_item_deletes: List[Dict[str, Any]] = []
    db_item_updates: List[Dict[str, Any]] = []
    db_album_deletes: List[int] = []
    resource_keys: set = set()
    captured_item_rows: List[Dict[str, Any]] = []
    captured_album_rows: List[Dict[str, Any]] = []
    skipped_unsafe_files_count = 0

    if mode == "remove_tracks":
        try:
            aid = int(payload.get("album_id") or 0)
        except Exception:
            aid = 0
        raw_item_ids = payload.get("item_ids") or []
        delete_files = bool(payload.get("delete_files", True))
        clean_empty_folders = bool(payload.get("clean_empty_folders", False))

        if aid <= 0 or not raw_item_ids:
            return {"ok": False, "error": "album_id and item_ids required", "code": "album_maintenance_invalid_payload"}

        item_ids = []
        for raw in raw_item_ids:
            try:
                val = int(raw)
                if val > 0:
                    item_ids.append(val)
            except Exception:
                continue
        if not item_ids:
            return {"ok": False, "error": "No valid item_ids provided", "code": "album_maintenance_invalid_payload"}

        resource_keys.add(f"album:{aid}")
        for iid in item_ids:
            resource_keys.add(f"item:{iid}")

        if lib_db and Path(lib_db).exists():
            con = sqlite3.connect(lib_db, timeout=10)
            con.row_factory = sqlite3.Row
            try:
                q_marks = ",".join("?" for _ in item_ids)
                rows = con.execute(
                    f"SELECT * FROM items WHERE album_id=? AND id IN ({q_marks})",
                    [aid] + item_ids,
                ).fetchall()
                for r in rows:
                    r_dict = dict(r)
                    captured_item_rows.append(r_dict)
                    raw_p = r["path"]
                    p_str = raw_p.decode("utf-8", "replace") if isinstance(raw_p, bytes) else str(raw_p or "")
                    if not p_str:
                        continue
                    p_path = Path(p_str)
                    if not p_path.is_absolute():
                        p_path = Path(allowed_roots[0]) / p_str

                    is_safe_file = any(_path_under(p_path, Path(r)) for r in allowed_roots) and not any(_path_has_symlink_under(p_path, Path(r)) for r in allowed_roots)
                    if is_safe_file and delete_files and p_path.exists() and p_path.is_file():
                        st = p_path.stat()
                        dup_quarantines.append({
                            "item_id": int(r["id"]),
                            "source": str(p_path),
                            "stat": {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns},
                        })
                    elif not is_safe_file and delete_files and p_path.exists():
                        skipped_unsafe_files_count += 1

                    db_item_deletes.append({
                        "id": int(r["id"]),
                        "album_id": aid,
                        "old_path": str(p_path),
                    })

                tot_row = con.execute("SELECT COUNT(*) FROM items WHERE album_id=?", (aid,)).fetchone()
                total_items_in_album = int(tot_row[0]) if tot_row else 0
                if total_items_in_album > 0 and len(db_item_deletes) == total_items_in_album:
                    arow = con.execute("SELECT * FROM albums WHERE id=?", (aid,)).fetchone()
                    if arow:
                        captured_album_rows.append(dict(arow))
                        db_album_deletes.append(aid)

                if delete_files and clean_empty_folders and dup_quarantines:
                    parent_dirs = set(Path(dq["source"]).parent for dq in dup_quarantines)
                    for parent in sorted(parent_dirs, key=lambda p: len(p.parts), reverse=True):
                        if _path_under(parent, Path(allowed_roots[0])):
                            directory_removals.append(str(parent))
            finally:
                con.close()

    elif mode == "deduplicate":
        try:
            aid = int(payload.get("album_id") or 0)
        except Exception:
            aid = 0
        if aid <= 0:
            return {"ok": False, "error": "album_id required", "code": "album_maintenance_invalid_payload"}

        resource_keys.add(f"album:{aid}")
        if payload.get("to_delete"):
            # Reload authoritative identity from the Beets DB rather than
            # trusting the caller's id/path pair (SEC-002 Wave 22 final
            # review, findings #17/#18): a caller-supplied path is
            # evidence, never proof that the row exists, belongs to this
            # album, or that the caller's path even matches what the
            # library actually has on file for that row.
            requested_ids: List[int] = []
            for d in payload["to_delete"]:
                try:
                    iid = int(d.get("id") or 0)
                    if iid > 0:
                        requested_ids.append(iid)
                except Exception:
                    continue
            if requested_ids and lib_db and Path(lib_db).exists():
                con = sqlite3.connect(lib_db, timeout=10)
                con.row_factory = sqlite3.Row
                try:
                    placeholders = ",".join("?" * len(requested_ids))
                    rows = con.execute(
                        f"SELECT * FROM items WHERE album_id=? AND id IN ({placeholders})",
                        [aid] + requested_ids,
                    ).fetchall()
                    found_ids = set()
                    for r in rows:
                        r_dict = dict(r)
                        iid = int(r["id"])
                        found_ids.add(iid)
                        captured_item_rows.append(r_dict)
                        raw_p = r["path"]
                        p_str = raw_p.decode("utf-8", "replace") if isinstance(raw_p, bytes) else str(raw_p or "")
                        if not p_str:
                            continue
                        p_path = Path(p_str)
                        if not p_path.is_absolute():
                            p_path = Path(allowed_roots[0]) / p_str
                        if not _path_under(p_path, Path(allowed_roots[0])):
                            return {"ok": False, "error": f"Item path outside allowed roots: {p_path}", "code": "album_maintenance_path_out_of_root"}
                        if _path_has_symlink_under(p_path, Path(allowed_roots[0])):
                            return {"ok": False, "error": f"Symlink rejected: {p_path}", "code": "album_maintenance_symlink_rejected"}

                        resource_keys.add(f"item:{iid}")
                        db_item_deletes.append({"id": iid, "album_id": aid, "old_path": str(p_path)})
                        if p_path.exists() and p_path.is_file():
                            st = p_path.stat()
                            dup_quarantines.append({
                                "item_id": iid,
                                "source": str(p_path),
                                "stat": {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns},
                            })
                    # A requested id that does not actually belong to this
                    # album (or does not exist at all) is a genuine
                    # identity-authority mismatch, not something to
                    # silently skip -- refuse the whole plan rather than
                    # partially honor a caller claim we could not verify.
                    missing = [i for i in requested_ids if i not in found_ids]
                    if missing:
                        return {"ok": False, "error": f"item(s) {missing} do not belong to album {aid}", "code": "album_maintenance_identity_mismatch"}
                finally:
                    con.close()

        if payload.get("fix_updates"):
            for fu in payload["fix_updates"]:
                try:
                    iid = int(fu.get("id") or 0)
                    if iid <= 0:
                        continue
                    old_path_str = str(fu.get("old_path") or "")
                    new_path_str = str(fu.get("new_path") or "")
                    entry = {
                        "item_id": iid,
                        "type": "move_file" if fu.get("rename") else "repoint_db",
                        "source": old_path_str,
                        "destination": new_path_str,
                    }
                    if fu.get("rename"):
                        # SEC-002 Wave 22 final review, CodeQL triage: this
                        # caller-supplied old_path/new_path pair reached a
                        # filesystem stat() with no root/symlink validation
                        # at all -- unlike the filename_cleanup mode added
                        # this same review just below, which does validate.
                        # Close the same gap here.
                        src_p = Path(old_path_str)
                        dst_p = Path(new_path_str)
                        if not _path_under(src_p, Path(allowed_roots[0])) or not _path_under(dst_p, Path(allowed_roots[0])):
                            return {"ok": False, "error": f"Path outside allowed roots: {src_p}", "code": "album_maintenance_path_out_of_root"}
                        if _path_has_symlink_under(src_p, Path(allowed_roots[0])) or _path_has_symlink_under(dst_p, Path(allowed_roots[0])):
                            return {"ok": False, "error": f"Symlink rejected: {src_p}", "code": "album_maintenance_symlink_rejected"}
                        if src_p.exists() and src_p.is_file():
                            st = src_p.stat()
                            entry["stat"] = {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
                    resource_keys.add(f"item:{iid}")
                    moves_plan.append(entry)
                    db_item_updates.append({"id": iid, "old_path": old_path_str, "new_path": new_path_str})
                except Exception:
                    continue

    elif mode == "filename_cleanup":
        # SEC-002 Wave 22 final review, finding #15: this mode previously
        # had no Plan implementation at all -- a transaction could still be
        # created and Apply could still report success/renamed=1 without
        # any move ever being planned or executed. Real implementation:
        # each candidate is an in-place rename (same directory, sanitized
        # filename) bound to an exact item id, content-verified against an
        # existing destination before ever treating it as a duplicate.
        for cand in (payload.get("candidates") or []):
            try:
                iid = int(cand.get("item_id") or 0)
            except Exception:
                iid = 0
            src_str = str(cand.get("source") or "").strip()
            dst_str = str(cand.get("destination") or "").strip()
            if iid <= 0 or not src_str or not dst_str:
                continue
            src_p = Path(src_str)
            dst_p = Path(dst_str)
            if not _path_under(src_p, Path(allowed_roots[0])) or not _path_under(dst_p, Path(allowed_roots[0])):
                return {"ok": False, "error": f"Path outside allowed roots: {src_p}", "code": "album_maintenance_path_out_of_root"}
            if _path_has_symlink_under(src_p, Path(allowed_roots[0])) or _path_has_symlink_under(dst_p, Path(allowed_roots[0])):
                return {"ok": False, "error": f"Symlink rejected: {src_p}", "code": "album_maintenance_symlink_rejected"}
            if not (src_p.exists() and src_p.is_file()):
                continue

            resource_keys.add(f"item:{iid}")
            st = src_p.stat()

            if dst_p.exists():
                if dst_p.resolve(strict=False) == src_p.resolve(strict=False):
                    continue  # already at destination; nothing to do
                if _album_cleanup_verified_same_file(src_p, dst_p):
                    dup_quarantines.append({
                        "item_id": iid,
                        "source": str(src_p),
                        "stat": {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns},
                    })
                    db_item_updates.append({"id": iid, "old_path": str(src_p), "new_path": str(dst_p)})
                    continue
                # Real, non-duplicate conflict at the destination -- refuse
                # rather than silently overwrite or pick a name ourselves.
                continue

            moves_plan.append({
                "item_id": iid,
                "type": "move_file",
                "source": str(src_p),
                "destination": str(dst_p),
                "stat": {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns},
            })
            db_item_updates.append({"id": iid, "old_path": str(src_p), "new_path": str(dst_p)})

    if not db_item_deletes and not dup_quarantines and not db_album_deletes and not moves_plan and not db_item_updates:
        return {
            "ok": True,
            "operation_id": None,
            "mode": mode,
            "item_deletes_count": 0,
            "quarantine_count": 0,
            "album_deletes_count": 0,
            "message": "No album maintenance changes could be safely planned.",
        }

    summary_text = f"Album maintenance ({mode}): {len(db_item_deletes)} item delete(s), {len(dup_quarantines)} quarantine(s), {len(db_album_deletes)} album retirement(s)"
    changes = [
        {"item_id": d["id"], "operation": "delete_item", "path": d["old_path"]}
        for d in db_item_deletes
    ] + [
        {"album_id": a, "operation": "delete_album"}
        for a in db_album_deletes
    ]

    tx = store.create(
        operation_type="Album Maintenance",
        status="Preview",
        summary=summary_text,
        changes=changes,
        metadata={
            "mutation_family": "album_maintenance_v1",
            "mode": mode,
            "moves_plan": moves_plan,
            "dup_quarantines": dup_quarantines,
            "directory_removals": directory_removals,
            "db_item_deletes": db_item_deletes,
            "db_item_updates": db_item_updates,
            "db_album_deletes": db_album_deletes,
            "captured_item_rows": captured_item_rows,
            "captured_album_rows": captured_album_rows,
            "resource_keys": sorted(resource_keys),
            "allowed_roots": allowed_roots,
            "skipped_unsafe_files_count": skipped_unsafe_files_count,
            "created_at": _now(),
        },
    )
    return {
        "ok": True,
        "operation_id": tx["id"],
        "mode": mode,
        "item_deletes_count": len(db_item_deletes),
        "quarantine_count": len(dup_quarantines),
        "album_deletes_count": len(db_album_deletes),
    }


def execute_album_maintenance_apply(
    store: TransactionStore,
    operation_id: str,
    *,
    music_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
    quarantine_base_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute album maintenance apply with durable steps, TOCTOU checks, and resource locking."""
    if not _TRANSACTION_ID_RE.match(operation_id):
        return {"ok": False, "error": "Invalid transaction ID format", "code": "album_maintenance_invalid_id"}

    with _get_apply_lock(operation_id):
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": f"Transaction {operation_id} not found", "code": "album_maintenance_not_found"}

        meta = tx.get("metadata") or {}
        if meta.get("mutation_family") != "album_maintenance_v1":
            return {"ok": False, "error": "Transaction is not an album_maintenance_v1 operation", "code": "album_maintenance_family_mismatch"}

        if tx.get("status") == "Completed":
            return {
                "ok": True,
                "operation_id": operation_id,
                "status": "Completed",
                "mutated": True,
                "deleted_items": meta.get("deleted_items_count", 0),
                "deleted_albums": meta.get("deleted_albums_count", 0),
            }

        resource_keys = meta.get("resource_keys") or []
        allowed_roots = music_allowed_roots or meta.get("allowed_roots") or [str(os.environ.get("MUSIC_ROOT", "/music"))]
        lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")

        def _fail(msg: str, code: str) -> Dict[str, Any]:
            curr = store.get(operation_id)
            c_meta = curr.get("metadata") or {}
            mutated = bool(c_meta.get("filesystem_mutated") or c_meta.get("db_mutated"))
            store.update(operation_id, status="Failed", logs=[f"Apply failed: {msg}"])
            return {"ok": False, "error": msg, "code": code, "mutated": mutated, "partial_mutation": mutated, "rollback_available": mutated}

        with _lock_resources(resource_keys):
            dup_quarantines = meta.get("dup_quarantines") or []
            moves_plan = meta.get("moves_plan") or []

            # Full TOCTOU (dev, inode, size, mtime_ns) plus root/symlink
            # revalidation immediately before mutation for every source
            # (SEC-002 Wave 22 final review, finding #20/#21).
            real_moves = [m for m in moves_plan if m.get("type") == "move_file"]
            for rec in dup_quarantines + real_moves:
                src_str = str(rec.get("source") or "")
                if not src_str:
                    continue
                src_p = Path(src_str)
                if not _path_under(src_p, Path(allowed_roots[0])):
                    return _fail(f"Source outside allowed roots: {src_p}", "album_maintenance_path_out_of_root")
                if _path_has_symlink_under(src_p, Path(allowed_roots[0])):
                    return _fail(f"Symlink detected on source: {src_p}", "album_maintenance_symlink_rejected")
                if not src_p.exists() or "stat" not in rec:
                    continue
                st = src_p.stat()
                exp_st = rec["stat"]
                if (st.st_dev != exp_st.get("dev") or st.st_ino != exp_st.get("ino")
                        or st.st_size != exp_st.get("size") or st.st_mtime_ns != exp_st.get("mtime_ns")):
                    return _fail(f"Source changed since plan: {src_p}", "album_maintenance_toctou_mismatch")
            for m in real_moves:
                dst_p = Path(m["destination"])
                if not _path_under(dst_p, Path(allowed_roots[0])):
                    return _fail(f"Destination outside allowed roots: {dst_p}", "album_maintenance_path_out_of_root")
                if dst_p.parent.exists() and _path_has_symlink_under(dst_p.parent, Path(allowed_roots[0])):
                    return _fail(f"Symlink detected on destination: {dst_p}", "album_maintenance_symlink_rejected")

            store.update(operation_id, status="Running", metadata={**meta, "mutation_started": True})

            q_base = quarantine_base_root or os.environ.get("RECONCILE_QUARANTINE_DIR", "/config/reconcile_quarantine")
            q_dir = Path(q_base) / operation_id

            def _persist(quarantined: List[Dict[str, Any]], moved: List[Dict[str, Any]], removed: List[str]) -> None:
                curr = store.get(operation_id).get("metadata", {})
                store.update(operation_id, metadata={
                    **curr,
                    "quarantined_records": quarantined,
                    "moved_records": moved,
                    "removed_dirs": removed,
                    "filesystem_mutated": True,
                    "mutated": True,
                })

            quarantined_records: List[Dict[str, Any]] = []
            moved_records: List[Dict[str, Any]] = []
            removed_dirs: List[str] = []

            files_failed = meta.get("skipped_unsafe_files_count", 0)
            file_errors: List[str] = []
            try:
                for idx, dq in enumerate(dup_quarantines):
                    src_p = Path(dq["source"])
                    if not src_p.exists():
                        continue
                    try:
                        if quarantine_base_root:
                            q_dir.mkdir(parents=True, exist_ok=True)
                            q_target = q_dir / f"{idx:04d}_item{dq.get('item_id', 0)}_{re.sub(r'[^A-Za-z0-9._-]', '_', src_p.name)}"
                            _safe_rename(src_p, q_target)
                            quarantined_records.append({"item_id": dq.get("item_id"), "source": str(src_p), "quarantined": str(q_target)})
                        else:
                            src_p.unlink()
                            quarantined_records.append({"item_id": dq.get("item_id"), "source": str(src_p), "quarantined": ""})
                        _persist(quarantined_records, moved_records, removed_dirs)
                    except Exception as exc:
                        files_failed += 1
                        file_errors.append(f"{src_p}: {exc}")

                # Real file moves (SEC-002 Wave 22 final review, finding
                # #15 -- moves_plan was previously captured at Plan time
                # but never executed at Apply, so a "renamed" filename
                # cleanup or dedup fix_updates result never actually moved
                # the file; only the DB row changed).
                for m in moves_plan:
                    if m.get("type") != "move_file":
                        continue  # "repoint_db": DB path correction only, no physical file to move
                    src_p = Path(m["source"])
                    dst_p = Path(m["destination"])
                    if not src_p.exists():
                        continue
                    if dst_p.exists():
                        return _fail(f"Move destination now occupied: {dst_p}", "album_maintenance_toctou_mismatch")
                    dst_p.parent.mkdir(parents=True, exist_ok=True)
                    _safe_rename(src_p, dst_p)
                    moved_records.append({"item_id": m.get("item_id"), "source": str(src_p), "destination": str(dst_p)})
                    _persist(quarantined_records, moved_records, removed_dirs)

                directory_removals = meta.get("directory_removals") or []
                for dr in directory_removals:
                    dr_p = Path(dr)
                    if dr_p.exists() and dr_p.is_dir():
                        try:
                            if not any(dr_p.iterdir()):
                                dr_p.rmdir()
                                removed_dirs.append(dr)
                        except OSError:
                            pass
                _persist(quarantined_records, moved_records, removed_dirs)
            except OSError as ex:
                return _fail(f"Filesystem mutation failed: {ex}", "album_maintenance_filesystem_failed")

            # Stage 2: Database Operations -- every write bound to an
            # exact row id and rowcount-verified; a mismatch (the bound
            # row already changed or vanished) fails the whole SQL
            # transaction instead of silently committing a partial result
            # (SEC-002 Wave 22 final review, findings #27/#28).
            db_item_deletes = meta.get("db_item_deletes") or []
            db_album_deletes = meta.get("db_album_deletes") or []
            db_item_updates = meta.get("db_item_updates") or []

            deleted_items_count = 0
            deleted_albums_count = 0
            db_mutated = False

            if lib_db and Path(lib_db).exists() and (db_item_updates or db_item_deletes or db_album_deletes):
                con = sqlite3.connect(lib_db, timeout=10)
                try:
                    for u in db_item_updates:
                        cur = con.execute("UPDATE items SET path=? WHERE id=? AND path=?",
                                           (u["new_path"].encode("utf-8"), u["id"], u["old_path"].encode("utf-8")))
                        if cur.rowcount != 1:
                            con.rollback()
                            return _fail(f"Expected to update exactly 1 item row for id={u['id']}, affected {cur.rowcount}.", "album_maintenance_rowcount_mismatch")

                    for d in db_item_deletes:
                        cur = con.execute("DELETE FROM items WHERE album_id=? AND id=?", (d["album_id"], d["id"]))
                        if cur.rowcount != 1:
                            con.rollback()
                            return _fail(f"Expected to delete exactly 1 item row for id={d['id']}, affected {cur.rowcount}.", "album_maintenance_rowcount_mismatch")
                        deleted_items_count += 1

                    for aid in db_album_deletes:
                        # Re-verify zero items remaining before deleting album row
                        tot = con.execute("SELECT COUNT(*) FROM items WHERE album_id=?", (aid,)).fetchone()[0]
                        if int(tot or 0) != 0:
                            con.rollback()
                            return _fail(f"Album {aid} still has items; refusing to delete album row.", "album_maintenance_rowcount_mismatch")
                        cur = con.execute("DELETE FROM albums WHERE id=?", (aid,))
                        if cur.rowcount != 1:
                            con.rollback()
                            return _fail(f"Expected to delete exactly 1 album row for id={aid}, affected {cur.rowcount}.", "album_maintenance_rowcount_mismatch")
                        deleted_albums_count += 1
                    con.commit()
                    db_mutated = bool(db_item_updates or db_item_deletes or db_album_deletes)
                finally:
                    con.close()

            # Stage 3 verification before Completed.
            for qr in quarantined_records:
                if Path(qr["source"]).exists() or not Path(qr["quarantined"]).exists():
                    return _fail("Post-write verification failed for quarantined file.", "album_maintenance_verification_failed")
            for mr in moved_records:
                if Path(mr["source"]).exists() or not Path(mr["destination"]).exists():
                    return _fail("Post-write verification failed for moved file.", "album_maintenance_verification_failed")
            if lib_db and Path(lib_db).exists() and db_item_updates:
                con = sqlite3.connect(lib_db, timeout=10)
                try:
                    for u in db_item_updates:
                        row = con.execute("SELECT path FROM items WHERE id=?", (u["id"],)).fetchone()
                        got = row[0].decode("utf-8", "replace") if row and isinstance(row[0], bytes) else (row[0] if row else None)
                        if row and got != u["new_path"]:
                            return _fail(f"Post-write verification failed for item {u['id']} path.", "album_maintenance_verification_failed")
                finally:
                    con.close()

            store.update(operation_id, status="Completed", metadata={
                **store.get(operation_id).get("metadata", {}),
                "db_mutated": db_mutated,
                "deleted_items_count": deleted_items_count,
                "deleted_albums_count": deleted_albums_count,
                "completed_at": _now(),
            })

            return {
                "ok": True,
                "operation_id": operation_id,
                "status": "Completed",
                "mutated": True,
                "deleted_items": deleted_items_count,
                "deleted_albums": deleted_albums_count,
                "moved_count": len(moved_records),
                "quarantined_count": len(quarantined_records),
                "files_failed": files_failed,
                "file_errors": file_errors,
            }


def rollback_album_maintenance(
    store: TransactionStore,
    operation_id: str,
    *,
    music_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Roll back an album_maintenance_v1 transaction by restoring files and DB rows."""
    if not _TRANSACTION_ID_RE.match(operation_id):
        return {"ok": False, "error": "Invalid transaction ID format", "code": "album_maintenance_invalid_id"}

    with _get_apply_lock(operation_id):
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": f"Transaction {operation_id} not found", "code": "album_maintenance_not_found"}

        meta = tx.get("metadata") or {}
        if meta.get("mutation_family") != "album_maintenance_v1":
            return {"ok": False, "error": "Transaction is not an album_maintenance_v1 operation", "code": "album_maintenance_family_mismatch"}

        if tx.get("status") == "Rolled Back":
            return {"ok": False, "error": "Transaction is already rolled back.", "code": "album_maintenance_already_rolled_back"}

        if not meta.get("filesystem_mutated") and not meta.get("db_mutated"):
            return {"ok": False, "error": "No mutations were performed to roll back.", "code": "album_maintenance_no_mutations"}

        resource_keys = meta.get("resource_keys") or []
        allowed_roots = music_allowed_roots or meta.get("allowed_roots") or [str(os.environ.get("MUSIC_ROOT", "/music"))]
        lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")

        with _lock_resources(resource_keys):
            quarantined_records = meta.get("quarantined_records") or []
            moved_records = meta.get("moved_records") or []
            files_restored = 0
            files_failed = 0

            for qr in quarantined_records:
                q_p = Path(qr["quarantined"])
                orig_p = Path(qr["source"])
                if not _path_under(orig_p, Path(allowed_roots[0])) or orig_p.exists() or orig_p.is_symlink():
                    files_failed += 1
                    continue
                if not q_p.exists():
                    files_failed += 1
                    continue
                try:
                    orig_p.parent.mkdir(parents=True, exist_ok=True)
                    if _path_has_symlink_under(orig_p.parent, Path(allowed_roots[0])):
                        files_failed += 1
                        continue
                    _safe_rename(q_p, orig_p)
                    files_restored += 1
                except OSError:
                    files_failed += 1

            for mr in reversed(moved_records):
                dst_p = Path(mr["destination"])
                orig_p = Path(mr["source"])
                if not _path_under(orig_p, Path(allowed_roots[0])) or orig_p.exists() or orig_p.is_symlink():
                    files_failed += 1
                    continue
                if not dst_p.exists():
                    files_failed += 1
                    continue
                try:
                    orig_p.parent.mkdir(parents=True, exist_ok=True)
                    _safe_rename(dst_p, orig_p)
                    files_restored += 1
                except OSError:
                    files_failed += 1

            captured_item_rows = meta.get("captured_item_rows") or []
            captured_album_rows = meta.get("captured_album_rows") or []
            db_item_updates = meta.get("db_item_updates") or []
            db_restored = 0
            db_failed = 0

            if lib_db and Path(lib_db).exists():
                con = sqlite3.connect(lib_db, timeout=10)
                try:
                    cur = con.cursor()
                    for item_row in captured_item_rows:
                        iid = int(item_row["id"])
                        cur.execute("SELECT COUNT(*) FROM items WHERE id=?", (iid,))
                        if cur.fetchone()[0] == 0:
                            cols = list(item_row.keys())
                            placeholders = ",".join("?" * len(cols))
                            cur.execute(
                                f"INSERT INTO items ({','.join(cols)}) VALUES ({placeholders})",
                                [item_row[c] for c in cols],
                            )
                            if cur.rowcount == 1:
                                db_restored += 1
                            else:
                                db_failed += 1

                    for album_row in captured_album_rows:
                        aid = int(album_row["id"])
                        cur.execute("SELECT COUNT(*) FROM albums WHERE id=?", (aid,))
                        if cur.fetchone()[0] == 0:
                            cols = list(album_row.keys())
                            placeholders = ",".join("?" * len(cols))
                            cur.execute(
                                f"INSERT INTO albums ({','.join(cols)}) VALUES ({placeholders})",
                                [album_row[c] for c in cols],
                            )
                            if cur.rowcount != 1:
                                db_failed += 1

                    # Path-only updates (filename_cleanup / dedup
                    # fix_updates): restore only if the row still holds
                    # exactly the value Apply wrote -- refuse to clobber a
                    # later, unrelated legitimate change to the same row.
                    for u in db_item_updates:
                        cur.execute("SELECT path FROM items WHERE id=?", (u["id"],))
                        row = cur.fetchone()
                        if row is None:
                            continue
                        got = row[0].decode("utf-8", "replace") if isinstance(row[0], bytes) else row[0]
                        if got != u["new_path"]:
                            continue
                        cur.execute("UPDATE items SET path=? WHERE id=? AND path=?",
                                    (u["old_path"].encode("utf-8"), u["id"], u["new_path"].encode("utf-8")))
                        if cur.rowcount == 1:
                            db_restored += 1
                        else:
                            db_failed += 1
                    con.commit()
                finally:
                    con.close()

            if files_failed == 0 and db_failed == 0:
                final_status = "Rolled Back"
                ok = True
            elif files_restored > 0 or db_restored > 0:
                final_status = "Partially Rolled Back"
                ok = False
            else:
                final_status = "Failed"
                ok = False

            store.update(operation_id, status=final_status, metadata={
                **meta,
                "rollback_available": False,
                "files_restored_count": files_restored,
                "files_failed_count": files_failed,
                "db_restored_count": db_restored,
                "db_failed_count": db_failed,
                "rolled_back_at": _now(),
            })

            return {
                "ok": ok,
                "operation_id": operation_id,
                "status": final_status,
                "files_restored": files_restored,
                "files_failed": files_failed,
                "db_restored": db_restored,
                "db_failed": db_failed,
                "partial_mutation": final_status == "Partially Rolled Back",
            }


# ── album_artwork_v1 ──────────────────────────────────────────────────────────

_ALBUM_ARTWORK_MODES = frozenset({"quarantine", "move", "replace"})


def _artwork_quarantine_key(src_p: Path, idx: int) -> str:
    """Stable, collision-free quarantine leaf name."""
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", src_p.name) or "art"
    return f"{idx:04d}_{safe_name}"


def _safe_quarantine_target(q_dir: Path, src_p: Path, idx: int) -> Path:
    safe_name = _artwork_quarantine_key(src_p, idx)
    target = q_dir / safe_name
    if not target.exists():
        return target
    p = Path(safe_name)
    stem, ext = p.stem, p.suffix
    c = 1
    while True:
        cand = q_dir / f"{stem}_safe_{c}{ext}"
        if not cand.exists():
            return cand
        c += 1


# ── SEC-002 / ARCH-003 Wave 24 final review: shared image validation ─────────
# Section 5 of the Wave 24 review: the artwork family's own magic-byte-only
# check is not real image validation. Pillow is already a direct dependency
# (requirements.txt) so this reuses it rather than hand-rolling a second,
# weaker decoder. Bounds are intentionally generous but finite -- this exists
# to reject decompression bombs and corrupt/truncated data, not to police
# "reasonable" album art dimensions.
_ARTWORK_MAX_ENCODED_BYTES = 10 * 1024 * 1024
_ARTWORK_MAX_PIXELS = 64_000_000  # ~8000x8000; also Pillow's own decompression-bomb default
_ARTWORK_MAX_DIMENSION = 12000
_ARTWORK_ALLOWED_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
_ARTWORK_FORMAT_EXT = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}


def _validate_image_bytes(data: bytes) -> Dict[str, Any]:
    """Real image validation: magic bytes are not enough. Parses the image,
    enforces bounded encoded size, decoded pixel count, and dimensions, and
    rejects corrupt/truncated payloads. Returns
    {"ok": True, "format": ..., "ext": ..., "width": ..., "height": ...}
    or {"ok": False, "error": ..., "code": ...}.
    """
    if not data:
        return {"ok": False, "error": "Empty artwork payload", "code": "album_artwork_empty"}
    if len(data) > _ARTWORK_MAX_ENCODED_BYTES:
        return {"ok": False, "error": "Artwork image exceeds maximum encoded size limit", "code": "album_artwork_image_too_large"}
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = _ARTWORK_MAX_PIXELS
        with Image.open(io.BytesIO(data)) as im:
            im.verify()  # raises on truncated/corrupt data
        # verify() invalidates the file handle for further use; reopen to
        # read format/dimensions from a fresh decode.
        with Image.open(io.BytesIO(data)) as im2:
            fmt = str(im2.format or "").upper()
            width, height = im2.size
            if fmt not in _ARTWORK_ALLOWED_FORMATS:
                return {"ok": False, "error": f"Unsupported image format: {fmt or 'unknown'}", "code": "album_artwork_invalid_image_format"}
            if width <= 0 or height <= 0 or width > _ARTWORK_MAX_DIMENSION or height > _ARTWORK_MAX_DIMENSION:
                return {"ok": False, "error": f"Image dimensions out of bounds: {width}x{height}", "code": "album_artwork_dimensions_invalid"}
            if width * height > _ARTWORK_MAX_PIXELS:
                return {"ok": False, "error": "Image pixel count exceeds maximum (possible decompression bomb)", "code": "album_artwork_pixel_bomb"}
            # Force a full decode of pixel data -- verify() only validates
            # structure/headers for most formats, not full decompression.
            im2.load()
        return {"ok": True, "format": fmt, "ext": _ARTWORK_FORMAT_EXT[fmt], "width": width, "height": height}
    except Exception as ex:
        return {"ok": False, "error": f"Corrupt or unparseable image: {ex}", "code": "album_artwork_corrupt_image"}


# ── SEC-002 / ARCH-003 Wave 24 final review: native Beets integration ────────
# Section 7/8/28: the engine must ask the REAL, installed Beets (with the
# operator's actual config and plugins loaded) where a file belongs, rather
# than reinventing a parallel "Artist/Album/NN - Title" formatter. This runs
# inside the beets-engine container, where the full `beets` package is
# always installed (it is the same image that runs the `beet` CLI) -- see
# Dockerfile.beets. It is guarded so the lighter Web Manager test/dev
# environment (which intentionally does not depend on the full `beets`
# package -- see requirements.txt) degrades to an explicit, fail-closed
# error instead of a silent parallel implementation.
try:
    import beets as _beets_pkg  # type: ignore
    import beets.library as _beets_library_mod  # type: ignore
    import beets.plugins as _beets_plugins_mod  # type: ignore
    _BEETS_IMPORT_ERROR: Optional[str] = None
except Exception as _bexc:  # pragma: no cover - exercised in the lightweight test env
    _beets_pkg = None
    _beets_library_mod = None
    _beets_plugins_mod = None
    _BEETS_IMPORT_ERROR = str(_bexc)

_beets_plugins_loaded = False
_beets_plugins_lock = threading.Lock()


def native_beets_available() -> bool:
    return _beets_library_mod is not None


def _ensure_beets_plugins_loaded() -> None:
    global _beets_plugins_loaded
    if _beets_plugins_loaded or _beets_plugins_mod is None:
        return
    with _beets_plugins_lock:
        if _beets_plugins_loaded:
            return
        try:
            _beets_plugins_mod.load_plugins()
        except Exception:
            # Plugin load failures must not block core path formatting; a
            # plugin-provided path template function will simply fail its
            # own evaluation below, which surfaces as a Plan error.
            pass
        _beets_plugins_loaded = True


def native_beets_item_destination(
    lib_db_path: str,
    music_root: str,
    item_id: int,
    field_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Ask the real, installed Beets library (actual config + plugins) for
    an item's destination path. Read-only: never calls item.store(),
    lib.add(), or any other Library mutation method, so it is safe to call
    from a non-mutating Plan.

    Returns {"ok": True, "path": Path} or {"ok": False, "error": ..., "code": ...}.
    """
    if not native_beets_available():
        return {
            "ok": False,
            "code": "album_relocation_native_beets_unavailable",
            "error": f"Native Beets path formatting is unavailable in this process: {_BEETS_IMPORT_ERROR}",
        }
    try:
        _ensure_beets_plugins_loaded()
        # The library "directory" is always the caller's already-validated,
        # server-configured MUSIC_ROOT -- never independently resolved from
        # beets.config["directory"], which could diverge from what the rest
        # of the engine's root-containment checks trust (and, in a process
        # without BEETSDIR pointed at the operator's real config, would
        # silently resolve to whatever config happens to be on this host).
        directory = str(music_root)
        lib = _beets_library_mod.Library(str(lib_db_path), directory)
        try:
            item = lib.get_item(int(item_id))
            if item is None:
                return {"ok": False, "code": "album_relocation_item_not_found", "error": f"Item {item_id} not found in Beets library"}
            if field_overrides:
                # Path templates read album-level fields (albumartist,
                # album, ...) from the item's *associated Album* object,
                # not from the item's own copy of those fields -- setting
                # them on the item alone has no effect on destination().
                # The Album object also refreshes itself FROM THE DATABASE
                # on every access (Item._cached_album's documented
                # behavior), which would silently discard an in-memory
                # preview override. Since this is a throwaway, never-
                # persisted object for a single Plan-time computation
                # (nothing here is stored back to the database), bypassing
                # Model's field-tracking __setattr__ to stub out that one
                # instance's refresh is safe and does not touch any other
                # in-flight request.
                album_obj = item._cached_album
                target = album_obj if album_obj is not None else item
                for k, v in field_overrides.items():
                    try:
                        setattr(target, k, v)
                    except Exception:
                        pass
                if album_obj is not None:
                    object.__setattr__(album_obj, "load", lambda *a, **k: None)
            dest_bytes = item.destination()
            dest = Path(os.fsdecode(dest_bytes))
            return {"ok": True, "path": dest}
        finally:
            # Close the sqlite connection this Library opened -- leaving it
            # open holds a file handle on the same library DB the rest of
            # the engine (and, on Windows, test cleanup) needs to access.
            try:
                lib._close()
            except Exception:
                pass
    except Exception as ex:
        return {"ok": False, "code": "album_relocation_native_beets_failed", "error": f"Native Beets destination computation failed: {ex}"}


def native_beets_write_item_tags(lib_db_path: str, music_root: str, item_id: int) -> Dict[str, Any]:
    """Write the item's CURRENT database field values to its media file tags
    using Beets' own Item.write(), rather than a hand-rolled mediafile
    writer -- this is the real semantics behind "resync tags from the
    database" (Wave 24 review section 28/30), not a diff-only update.
    """
    if not native_beets_available():
        return {
            "ok": False,
            "code": "album_metadata_native_beets_unavailable",
            "error": f"Native Beets tag writing is unavailable in this process: {_BEETS_IMPORT_ERROR}",
        }
    try:
        _ensure_beets_plugins_loaded()
        # The library "directory" is always the caller's already-validated,
        # server-configured MUSIC_ROOT -- never independently resolved from
        # beets.config["directory"], which could diverge from what the rest
        # of the engine's root-containment checks trust (and, in a process
        # without BEETSDIR pointed at the operator's real config, would
        # silently resolve to whatever config happens to be on this host).
        directory = str(music_root)
        lib = _beets_library_mod.Library(str(lib_db_path), directory)
        try:
            item = lib.get_item(int(item_id))
            if item is None:
                return {"ok": False, "code": "album_metadata_item_not_found", "error": f"Item {item_id} not found in Beets library"}
            item.write()
            return {"ok": True}
        finally:
            try:
                lib._close()
            except Exception:
                pass
    except Exception as ex:
        return {"ok": False, "code": "album_metadata_tag_write_failed", "error": f"Native Beets tag write failed: {ex}"}


def _capture_media_tag_state(path: Path, fields: Iterable[str]) -> Dict[str, Any]:
    """Capture a file's before-state (identity stat + current tag values)
    so Apply can TOCTOU-revalidate and Rollback can truthfully restore
    tags, not just DB rows (Wave 24 review sections 23/26)."""
    try:
        st = path.stat()
    except OSError:
        return {"exists": False}
    tags: Dict[str, Any] = {}
    try:
        import mediafile
        mf = mediafile.MediaFile(str(path))
        for f in fields:
            if hasattr(mf, f):
                try:
                    tags[f] = getattr(mf, f)
                except Exception:
                    pass
    except Exception:
        pass
    return {
        "exists": True,
        "stat": {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns},
        "tags": tags,
    }


def _write_media_tag_fields(path: Path, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Write specific tag fields to a media file. Never swallows failure --
    every caller in this module must treat a non-ok result as a real
    transaction failure (Wave 24 review section 24), not a logged-and-
    ignored best effort."""
    if not fields:
        return {"ok": True, "written": []}
    try:
        import mediafile
    except Exception as ex:
        return {"ok": False, "error": f"mediafile unavailable: {ex}", "code": "tag_write_module_unavailable"}
    try:
        mf = mediafile.MediaFile(str(path))
    except Exception as ex:
        return {"ok": False, "error": f"Could not open media file: {ex}", "code": "tag_write_open_failed"}
    written = []
    for k, v in fields.items():
        if not hasattr(mf, k):
            continue
        try:
            current = getattr(mf, k, None)
            coerced = int(v) if isinstance(current, int) and str(v).lstrip("-").isdigit() else v
            setattr(mf, k, coerced)
            written.append(k)
        except Exception as ex:
            return {"ok": False, "error": f"Failed to set tag field {k!r}: {ex}", "code": "tag_write_setattr_failed"}
    try:
        mf.save()
    except Exception as ex:
        return {"ok": False, "error": f"Failed to save media file: {ex}", "code": "tag_write_save_failed"}
    return {"ok": True, "written": written}


def create_album_artwork_plan(
    store: TransactionStore,
    payload: Dict[str, Any],
    *,
    music_allowed_roots: Optional[List[str]] = None,
    staging_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
    quarantine_base_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a non-mutating preview plan for album artwork operations."""
    try:
        aid = int(payload.get("album_id") or 0)
    except Exception:
        aid = 0
    if aid <= 0:
        return {"ok": False, "error": "album_id required", "code": "album_artwork_invalid_payload"}

    mode = str(payload.get("mode") or "quarantine").strip()
    if mode not in _ALBUM_ARTWORK_MODES:
        return {"ok": False, "error": f"Unsupported artwork mode: {mode}", "code": "album_artwork_invalid_mode"}

    payload_trash = payload.get("quarantine_root") or payload.get("trash_root")
    if payload_trash:
        pt_path = Path(payload_trash)
        if not any(_path_under(pt_path, Path(r)) for r in (music_allowed_roots or [os.environ.get("MUSIC_ROOT", "/music")]) + (staging_allowed_roots or [])):
            return {"ok": False, "error": f"Trash root outside configured allowed roots: {payload_trash}", "code": "album_artwork_trash_root_out_of_config"}

    q_base = payload_trash or quarantine_base_root or os.environ.get("RECONCILE_QUARANTINE_DIR") or os.environ.get("TRASH_ROOT") or "/config/reconcile_quarantine"
    trash_p = Path(q_base)
    stg_dir = trash_p / "staging"

    allowed_roots = music_allowed_roots or [r for r in [os.environ.get("MUSIC_ROOT"), os.environ.get("MUSIC_LIBRARY_PATH"), "/music", "/data/media/music"] if r]
    stg_roots = staging_allowed_roots or [
        str(r) for r in [
            os.environ.get("DOWNLOADS_ROOT"),
            os.environ.get("STAGING_ROOT"),
            "/downloads",
            "/staging",
        ] if r
    ]
    config_roots = [r for r in [os.environ.get("BEETSDIR"), os.environ.get("RECONCILE_QUARANTINE_DIR"), "/config"] if r]
    check_roots = [Path(r) for r in (allowed_roots + stg_roots + config_roots) if r and str(r).strip()]
    if not any(_path_under(trash_p, r) for r in check_roots):
        return {"ok": False, "error": f"Trash root outside configured allowed roots: {q_base}", "code": "album_artwork_trash_root_out_of_config"}
    source_roots = [Path(r) for r in (allowed_roots + stg_roots + [q_base, str(Path(q_base) / "staging")]) if r]
    lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB") or os.environ.get("LIB_PATH") or ""
    if lib_db and not Path(lib_db).exists():
        lib_db = ""

    resource_keys = [f"album:{aid}"]
    original_artpath = ""
    original_artpath_trusted = False
    derived_target_dir = ""
    if lib_db and Path(lib_db).exists():
        con = sqlite3.connect(lib_db, timeout=10)
        try:
            row = con.execute("SELECT artpath, mb_releasegroupid FROM albums WHERE id=?", (aid,)).fetchone()
            if row and row[0]:
                raw_art = row[0]
                original_artpath = raw_art.decode("utf-8", "replace") if isinstance(raw_art, bytes) else str(raw_art)
                if original_artpath:
                    # SEC-002 / ARCH-003 Wave 24 final review, section 3: a
                    # database-supplied path must be VERIFIED against the
                    # already-configured allowed roots -- it must never be
                    # allowed to CREATE a new one. Server configuration
                    # defines trust roots, not the mutable albums.artpath
                    # column. A DB row pointing outside the configured
                    # roots is not silently trusted for source discovery or
                    # replace-target matching below; it is only kept as an
                    # informational value for logging/rollback bookkeeping.
                    #
                    # Wave 24 final review round 3, section 11: a real Beets
                    # library stores this column RELATIVE to the music dir
                    # (see _resolve_db_path); resolve it before using it as a
                    # filesystem path, but the trust decision below is
                    # unaffected -- a relative value resolves under
                    # allowed_roots[0], which is itself one of the
                    # already-configured roots, so it cannot expand trust.
                    original_artpath = str(_resolve_db_path(original_artpath, allowed_roots))
                    p_orig_dir = Path(original_artpath).parent
                    original_artpath_trusted = any(_path_under(p_orig_dir, r) for r in source_roots)
            if row and row[1]:
                db_rgid = str(row[1] or "").strip()
                exp_rgid = str(payload.get("expected_mb_releasegroupid") or payload.get("rgid") or "").strip()
                if db_rgid and exp_rgid and db_rgid.lower() != exp_rgid.lower():
                    return {"ok": False, "error": f"Release group mismatch: expected {exp_rgid}, got {db_rgid}", "code": "album_artwork_releasegroup_mismatch"}

            item_rows = con.execute("SELECT path FROM items WHERE album_id=?", (aid,)).fetchall()
            item_dirs = set()
            for irow in item_rows:
                if irow[0]:
                    ipath_str = irow[0].decode("utf-8", "replace") if isinstance(irow[0], bytes) else str(irow[0])
                    if ipath_str:
                        # Wave 24 final review round 3, section 11: same
                        # relative-path normalization as original_artpath
                        # above -- items.path is subject to the identical
                        # real-Beets relative-storage behavior.
                        item_dirs.add(str(_resolve_db_path(ipath_str, allowed_roots).parent))
            if item_dirs:
                parents = {str(Path(d).parent if re.search(r"^(disc|cd|disk)\s*\d+$", Path(d).name, re.I) else Path(d)) for d in item_dirs}
                if len(parents) > 1:
                    return {"ok": False, "error": f"Album items span multiple conflicting directories: {item_dirs}", "code": "album_artwork_item_directories_conflict"}
                derived_target_dir = list(parents)[0]
        finally:
            con.close()

    candidates_raw = list(payload.get("quarantined_art") or payload.get("candidates") or [])

    if mode == "quarantine" and not candidates_raw:
        search_files: List[Path] = []
        if original_artpath and original_artpath_trusted:
            p_art = Path(original_artpath)
            if p_art.exists():
                search_files.append(p_art)
        if derived_target_dir:
            p_dir = Path(derived_target_dir)
            if p_dir.exists():
                for fn in ("albumart.jpg", "albumart.png", "folder.jpg", "cover.jpg", "front.jpg", "cover.png"):
                    candidate = p_dir / fn
                    if candidate.exists() and candidate not in search_files:
                        search_files.append(candidate)
        for sf in search_files:
            candidates_raw.append({"source": str(sf)})

    # SEC-002 / ARCH-003 Wave 24 final review, section 2: Plan must never
    # mutate the filesystem -- staging the uploaded image to disk here was
    # a real Plan-time mutation and violated Plan -> Review -> Apply ->
    # Verify. The bounded, already-validated image bytes are instead kept
    # as transaction input (base64, capped well under the transaction
    # store's practical size) and only written to disk during Apply. A
    # regression test snapshots the filesystem across Plan to prove this.
    pending_upload: Optional[Dict[str, Any]] = None
    image_b64 = payload.get("image_data_b64") or payload.get("image_b64")
    if image_b64 and mode in ("move", "replace"):
        import base64
        try:
            img_bytes = base64.b64decode(image_b64, validate=True)
        except Exception:
            return {"ok": False, "error": "Invalid base64 artwork encoding", "code": "album_artwork_invalid_encoding"}

        validation = _validate_image_bytes(img_bytes)
        if not validation.get("ok"):
            return {"ok": False, "error": validation.get("error"), "code": validation.get("code")}

        pending_upload = {
            "image_b64": image_b64,
            "ext": validation["ext"],
            "format": validation["format"],
            "width": validation["width"],
            "height": validation["height"],
            "size": len(img_bytes),
        }

    src_records: List[Dict[str, Any]] = []
    for cand in candidates_raw:
        src_p = Path(cand["source"])
        if not any(_path_under(src_p, r) for r in source_roots):
            return {"ok": False, "error": f"Artwork source outside allowed roots: {src_p}", "code": "album_artwork_path_out_of_root"}
        if any(_path_has_symlink_under(src_p, r) for r in source_roots if _path_under(src_p, r)):
            return {"ok": False, "error": f"Symlink rejected: {src_p}", "code": "album_artwork_symlink_rejected"}
        if not (src_p.exists() and src_p.is_file()):
            continue
        st = src_p.stat()
        src_records.append({
            "source": str(src_p),
            "stat": {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns},
        })

    quarantines: List[Dict[str, Any]] = []
    moves_plan: List[Dict[str, Any]] = []
    target_dir_str = ""
    new_artpath = ""

    if mode in ("move", "replace"):
        target_dir_str = str(payload.get("target_dir") or derived_target_dir or "").strip()
        if not target_dir_str:
            target_dir_str = str(Path(allowed_roots[0]) / f"album_{aid}")
        target_dir = Path(target_dir_str)
        if allowed_roots:
            for r in [Path(ar) for ar in allowed_roots]:
                cand = Path(r.drive + str(target_dir)) if r.drive and not target_dir.drive else target_dir
                if _path_under(cand, r):
                    target_dir = cand
                    break

        # Wave 24 round 4 CodeQL audit note: this is.symlink() call is a
        # deliberate metadata-only read, kept BEFORE full root-containment
        # proof so a symlinked album folder produces its own distinct
        # "could not be resolved safely" (400) response rather than the
        # generic "access denied" (403) one -- app.py's caller (and its
        # regression test) depend on that specific code/message pair.
        # Reordering this after containment would collapse the two cases;
        # see the alert-913 dismissal rationale in the Round 4 PR body for
        # why the read itself has no attacker-controlled escape (it only
        # returns a bool, and the containment check immediately below
        # fails closed regardless of the answer).
        if target_dir.is_symlink():
            return {"ok": False, "error": f"Symlink rejected: {target_dir}", "code": "album_artwork_symlink_album_folder"}
        validated_target_dir = validate_path_under_allowed_roots(target_dir, allowed_roots)
        if validated_target_dir is None:
            return {"ok": False, "error": f"Artwork target outside allowed roots or symlinked: {target_dir}", "code": "album_artwork_path_out_of_root"}
        target_dir = validated_target_dir

        candidate_list: List[Dict[str, Any]] = [{"kind": "file", "rec": rec} for rec in src_records]
        if pending_upload:
            candidate_list.append({"kind": "upload", "rec": pending_upload})

        for idx, entry in enumerate(candidate_list):
            is_upload = entry["kind"] == "upload"
            rec = entry["rec"]
            src_p = None if is_upload else Path(rec["source"])
            src_name = f"upload{rec['ext']}" if is_upload else src_p.name
            if mode == "replace":
                existing_target = None
                # Section 3: only trust the DB-supplied artpath for
                # replace-target matching once it has been verified against
                # the configured allowed roots above.
                if original_artpath and original_artpath_trusted:
                    op = Path(original_artpath)
                    existing_target = op if op.is_absolute() and _path_under(op, target_dir) else (target_dir / op.name)
                if not existing_target or not existing_target.exists():
                    for fn in ("albumart.jpg", "cover.jpg", "folder.jpg", "front.jpg", "albumart.png", "cover.png"):
                        cand = target_dir / fn
                        if cand.exists() or cand.is_symlink() or os.path.islink(str(cand)):
                            existing_target = cand
                            break
                dest = existing_target or (target_dir / "albumart.jpg")
            else:
                dest = target_dir / src_name
            new_artpath = str(dest)
            if dest.is_symlink() or os.path.islink(str(dest)) or _is_symlink_path(dest):
                return {"ok": False, "error": f"Symlink rejected: {dest}", "code": "album_artwork_symlink_rejected"}

            dest_before_stat = None
            if dest.exists():
                if not dest.is_file():
                    return {"ok": False, "error": f"Artwork destination is not a regular file: {dest}", "code": "album_artwork_dest_not_regular_file"}
                dst_st = dest.stat()
                # Section 4: capture the existing destination's full
                # before-state so Apply can revalidate it has not changed
                # (TOCTOU) before quarantining/replacing it.
                dest_before_stat = {"dev": dst_st.st_dev, "ino": dst_st.st_ino, "size": dst_st.st_size, "mtime_ns": dst_st.st_mtime_ns}
                if not is_upload and dest.resolve(strict=False) == src_p.resolve(strict=False):
                    moves_plan.append({"candidate_idx": idx, "type": "same_file", "source": rec["source"], "destination": str(dest), "stat": rec["stat"]})
                    continue
                if not is_upload and _album_cleanup_verified_same_file(src_p, dest):
                    moves_plan.append({"candidate_idx": idx, "type": "dedup_remove", "source": rec["source"], "stat": rec["stat"]})
                    continue
                if mode != "replace":
                    dest = _unique_dest(dest)
                    new_artpath = str(dest)
                    dest_before_stat = None

            if is_upload:
                moves_plan.append({
                    "candidate_idx": idx, "type": "pending_upload", "destination": str(dest),
                    "image_b64": rec["image_b64"], "ext": rec["ext"], "format": rec["format"],
                    "width": rec["width"], "height": rec["height"],
                    "dest_before_stat": dest_before_stat,
                })
            else:
                moves_plan.append({
                    "candidate_idx": idx, "type": "move", "source": rec["source"], "destination": str(dest),
                    "stat": rec["stat"], "dest_before_stat": dest_before_stat,
                })
    else:
        quarantines = src_records

    if not moves_plan and not quarantines:
        return {
            "ok": True,
            "operation_id": None,
            "album_id": aid,
            "mode": mode,
            "move_count": 0,
            "quarantine_count": 0,
            "message": "No artwork files to process.",
        }

    summary_text = f"Album artwork ({mode}): {len(moves_plan) or len(quarantines)} file(s)"
    tx = store.create(
        operation_type="Album Artwork",
        status="Preview",
        summary=summary_text,
        metadata={
            "mutation_family": "album_artwork_v1",
            "mode": mode,
            "album_id": aid,
            "target_dir": target_dir_str,
            "new_artpath": new_artpath,
            "quarantines": quarantines,
            "moves_plan": moves_plan,
            "original_artpath": original_artpath,
            "resource_keys": resource_keys,
            "allowed_roots": allowed_roots,
            "source_roots": [str(r) for r in source_roots],
            "created_at": _now(),
        },
    )
    return {
        "ok": True,
        "operation_id": tx["id"],
        "album_id": aid,
        "mode": mode,
        "move_count": len(moves_plan),
        "quarantine_count": len(quarantines),
        "target_artpath": new_artpath,
    }


def execute_album_artwork_apply(
    store: TransactionStore,
    operation_id: str,
    *,
    music_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
    quarantine_base_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute album artwork apply with durable steps and resource locking."""
    if not _TRANSACTION_ID_RE.match(operation_id):
        return {"ok": False, "error": "Invalid transaction ID format", "code": "album_artwork_invalid_id"}

    with _get_apply_lock(operation_id):
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": f"Transaction {operation_id} not found", "code": "album_artwork_not_found"}

        meta = tx.get("metadata") or {}
        if meta.get("mutation_family") != "album_artwork_v1":
            return {"ok": False, "error": "Transaction is not an album_artwork_v1 operation", "code": "album_artwork_family_mismatch"}

        if tx.get("status") == "Completed":
            return {"ok": True, "operation_id": operation_id, "status": "Completed", "mutated": True}

        mode = meta.get("mode") or "quarantine"
        resource_keys = meta.get("resource_keys") or []
        allowed_roots = music_allowed_roots or meta.get("allowed_roots") or [r for r in [os.environ.get("MUSIC_ROOT"), os.environ.get("MUSIC_LIBRARY_PATH"), "/music"] if r]
        source_roots = [Path(r) for r in (meta.get("source_roots") or allowed_roots)]
        lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB") or os.environ.get("LIB_PATH") or ""

        def _fail(msg: str, code: str) -> Dict[str, Any]:
            curr = store.get(operation_id)
            c_meta = curr.get("metadata") or {}
            mutated = bool(c_meta.get("filesystem_mutated") or c_meta.get("db_mutated"))
            store.update(operation_id, status="Failed", logs=[f"Apply failed: {msg}"])
            return {"ok": False, "error": msg, "code": code, "mutated": mutated, "partial_mutation": mutated, "rollback_available": mutated}

        with _lock_resources(resource_keys):
            moves_plan = meta.get("moves_plan") or []
            quarantines = meta.get("quarantines") or []

            for rec in moves_plan + quarantines:
                if rec.get("type") == "pending_upload":
                    # No on-disk source yet -- the bytes are still bounded
                    # transaction input; nothing to TOCTOU-check here.
                    continue
                src_p = Path(rec["source"])
                if not any(_path_under(src_p, r) for r in source_roots):
                    return _fail(f"Artwork source outside allowed roots: {src_p}", "album_artwork_path_out_of_root")
                if any(_path_has_symlink_under(src_p, r) for r in source_roots if _path_under(src_p, r)):
                    return _fail(f"Symlink detected on artwork source: {src_p}", "album_artwork_symlink_rejected")
                if not src_p.exists():
                    continue
                st = src_p.stat()
                exp_st = rec.get("stat") or {}
                if (st.st_dev != exp_st.get("dev") or st.st_ino != exp_st.get("ino")
                        or st.st_size != exp_st.get("size") or st.st_mtime_ns != exp_st.get("mtime_ns")):
                    return _fail(f"Source artwork changed since plan: {src_p}", "album_artwork_toctou_mismatch")

            # Section 4: revalidate any existing replace-mode destination
            # against the before-state captured at Plan time. A destination
            # that changed since Plan (a legitimate later artwork change
            # landed there) must not be silently quarantined/overwritten.
            for rec in moves_plan:
                if rec.get("type") not in ("move", "pending_upload"):
                    continue
                dest_before = rec.get("dest_before_stat")
                if not dest_before:
                    continue
                dest_p = Path(rec["destination"])
                if not dest_p.exists():
                    return _fail(f"Artwork replace target changed since plan (no longer exists): {dest_p}", "album_artwork_stale_destination")
                dst_st = dest_p.stat()
                if (dst_st.st_dev != dest_before.get("dev") or dst_st.st_ino != dest_before.get("ino")
                        or dst_st.st_size != dest_before.get("size") or dst_st.st_mtime_ns != dest_before.get("mtime_ns")):
                    return _fail(f"Artwork replace target changed since plan: {dest_p}", "album_artwork_stale_destination")

            target_dir_str = meta.get("target_dir") or ""
            validated_target_dir: Optional[Path] = None
            if mode in ("move", "replace"):
                target_dir_candidate = Path(target_dir_str)
                if allowed_roots:
                    for r in [Path(ar) for ar in allowed_roots]:
                        cand = Path(r.drive + str(target_dir_candidate)) if r.drive and not target_dir_candidate.drive else target_dir_candidate
                        if _path_under(cand, r):
                            target_dir_candidate = cand
                            break
                # Wave 24 round 4: revalidate target_dir fresh at Apply
                # (transaction metadata is never trusted as its own root of
                # authority -- only music_allowed_roots, which is always
                # server/operator-configured, ever is) and keep the ONE
                # resolved, proven-safe Path for every sink below, instead
                # of the previous pattern where this validated value was
                # discarded and target_dir was rebuilt from the raw string
                # again just before the mkdir/rename calls.
                validated_target_dir = validate_path_under_allowed_roots(target_dir_candidate, allowed_roots)
                if validated_target_dir is None:
                    return _fail(f"Artwork target outside allowed roots or symlinked: {target_dir_candidate}", "album_artwork_path_out_of_root")

            store.update(operation_id, status="Running", metadata={**meta, "mutation_started": True})

            q_base = quarantine_base_root or os.environ.get("RECONCILE_QUARANTINE_DIR", "/config/reconcile_quarantine")
            q_dir = Path(q_base) / operation_id

            def _persist(moved: List[Dict[str, Any]], quarantined: List[Dict[str, Any]]) -> None:
                curr = store.get(operation_id).get("metadata", {})
                store.update(operation_id, metadata={
                    **curr,
                    "moved_records": moved,
                    "quarantined_records": quarantined,
                    "filesystem_mutated": True,
                    "mutated": True,
                })

            moved_records: List[Dict[str, Any]] = []
            quarantined_records: List[Dict[str, Any]] = []

            try:
                if mode in ("move", "replace"):
                    target_dir = validated_target_dir
                    target_dir.mkdir(parents=True, exist_ok=True)
                    for idx, m in enumerate(moves_plan):
                        if m["type"] == "pending_upload":
                            # Section 2: the bounded upload is written to
                            # disk for the first time HERE, during Apply --
                            # never during Plan. Re-validate the bytes
                            # again as defense in depth before writing.
                            import base64
                            img_bytes = base64.b64decode(m["image_b64"], validate=True)
                            revalidation = _validate_image_bytes(img_bytes)
                            if not revalidation.get("ok"):
                                return _fail(f"Artwork upload failed re-validation at Apply: {revalidation.get('error')}", revalidation.get("code") or "album_artwork_corrupt_image")
                            q_dir.mkdir(parents=True, exist_ok=True)
                            stg_p = q_dir / f"upload_{idx}{m['ext']}"
                            stg_p.write_bytes(img_bytes)
                            try:
                                fd_stg = os.open(str(stg_p), os.O_RDONLY)
                                try:
                                    os.fsync(fd_stg)
                                finally:
                                    os.close(fd_stg)
                            except Exception:
                                pass
                            src_p = stg_p
                        else:
                            src_p = Path(m["source"])
                        if not src_p.exists():
                            continue
                        try:
                            fd_src = os.open(str(src_p), os.O_RDONLY)
                            try:
                                os.fsync(fd_src)
                            finally:
                                os.close(fd_src)
                        except Exception:
                            pass
                        if m["type"] == "same_file":
                            moved_records.append({"source": str(src_p), "destination": str(src_p)})
                            continue
                        if m["type"] == "dedup_remove":
                            q_dir.mkdir(parents=True, exist_ok=True)
                            q_target = _safe_quarantine_target(q_dir, src_p, idx)
                            os.chmod(str(q_dir), 0o700)
                            _safe_rename(src_p, q_target)
                            os.chmod(str(q_target), 0o600)
                            quarantined_records.append({"source": str(src_p), "quarantined": str(q_target), "kind": "verified_duplicate"})
                        else:
                            # Wave 24 round 4: re-derive the per-item
                            # destination through the validated_target_dir
                            # (already proven contained/symlink-free above)
                            # rather than trusting the stored metadata
                            # string on its own -- defense in depth against
                            # transaction-record tampering, even though no
                            # current route lets a request body overwrite
                            # moves_plan directly.
                            dest_p = validate_path_under_allowed_roots(Path(m["destination"]), [target_dir])
                            if dest_p is None:
                                return _fail(f"Artwork destination outside validated target directory: {m['destination']}", "album_artwork_path_out_of_root")
                            if dest_p.exists() and mode == "replace" and dest_p.resolve(strict=False) != src_p.resolve(strict=False):
                                q_dir.mkdir(parents=True, exist_ok=True)
                                q_target = _safe_quarantine_target(q_dir, dest_p, idx)
                                os.chmod(str(q_dir), 0o700)
                                _safe_rename(dest_p, q_target)
                                os.chmod(str(q_target), 0o600)
                                quarantined_records.append({"source": str(dest_p), "quarantined": str(q_target), "kind": "replaced_artwork"})
                                _persist(moved_records, quarantined_records)
                            dest_p.parent.mkdir(parents=True, exist_ok=True)
                            _safe_rename(src_p, dest_p)
                            dpst = dest_p.stat()
                            moved_records.append({
                                "source": str(src_p), "destination": str(dest_p),
                                "dest_stat_after_move": {"dev": dpst.st_dev, "ino": dpst.st_ino, "size": dpst.st_size, "mtime_ns": dpst.st_mtime_ns},
                            })
                        _persist(moved_records, quarantined_records)
                else:
                    for idx, q in enumerate(quarantines):
                        src_p = Path(q["source"])
                        if not src_p.exists():
                            continue
                        q_dir.mkdir(parents=True, exist_ok=True)
                        q_target = _safe_quarantine_target(q_dir, src_p, idx)
                        os.chmod(str(q_dir), 0o700)
                        _safe_rename(src_p, q_target)
                        os.chmod(str(q_target), 0o600)
                        qst = q_target.stat()
                        quarantined_records.append({
                            "source": str(src_p), "quarantined": str(q_target), "kind": "quarantine",
                            "quarantined_stat": {"dev": qst.st_dev, "ino": qst.st_ino, "size": qst.st_size, "mtime_ns": qst.st_mtime_ns},
                        })
                        _persist(moved_records, quarantined_records)
            except Exception as ex:
                rollback_album_artwork(store, operation_id, music_allowed_roots=allowed_roots, db_path=lib_db)
                return _fail(f"Artwork filesystem mutation failed: {ex}", "album_artwork_filesystem_failed")

            aid = int(meta.get("album_id") or 0)
            db_mutated = False
            new_artpath = meta.get("new_artpath") or (moved_records[0]["destination"] if moved_records else "")

            if lib_db and Path(lib_db).exists() and aid > 0:
                con = sqlite3.connect(lib_db, timeout=10)
                try:
                    if mode in ("move", "replace") and new_artpath:
                        cur = con.execute("UPDATE albums SET artpath=? WHERE id=?", (new_artpath, aid))
                    else:
                        cur = con.execute("UPDATE albums SET artpath=NULL WHERE id=?", (aid,))
                    if cur.rowcount != 1:
                        con.rollback()
                        rollback_album_artwork(store, operation_id, music_allowed_roots=allowed_roots, db_path=lib_db)
                        return _fail(f"Expected to update artpath for exactly 1 album row, affected {cur.rowcount}.", "album_artwork_rowcount_mismatch")
                    con.commit()
                    db_mutated = True
                finally:
                    con.close()

            # Stage 3 verification before Completed.
            for mr in moved_records:
                if not Path(mr["destination"]).exists():
                    rollback_album_artwork(store, operation_id, music_allowed_roots=allowed_roots, db_path=lib_db)
                    return _fail("Post-write verification failed for artwork move.", "album_artwork_verification_failed")
            for qr in quarantined_records:
                if not Path(qr["quarantined"]).exists():
                    rollback_album_artwork(store, operation_id, music_allowed_roots=allowed_roots, db_path=lib_db)
                    return _fail("Post-write verification failed for artwork quarantine.", "album_artwork_verification_failed")
                if Path(qr["source"]).exists():
                    # Wave 24 final review round 3 (surfaced by a relative-
                    # DB-path replace-over-existing-artwork test -- no prior
                    # test exercised replace against a non-blank artpath):
                    # a "replaced_artwork" quarantine's source path is the
                    # OLD art's location, which "replace" mode legitimately
                    # reoccupies moments later with the NEW art (same path,
                    # new content) via its own moved_records entry above --
                    # that is the intended outcome, not a failed quarantine.
                    # Any other kind (verified_duplicate / plain quarantine)
                    # must never see its source path exist afterward.
                    reoccupied = qr.get("kind") == "replaced_artwork" and any(
                        mr.get("destination") == qr["source"] for mr in moved_records
                    )
                    if not reoccupied:
                        rollback_album_artwork(store, operation_id, music_allowed_roots=allowed_roots, db_path=lib_db)
                        return _fail("Post-write verification failed for artwork quarantine.", "album_artwork_verification_failed")
            if lib_db and Path(lib_db).exists() and aid > 0 and db_mutated:
                con = sqlite3.connect(lib_db, timeout=10)
                try:
                    row = con.execute("SELECT artpath FROM albums WHERE id=?", (aid,)).fetchone()
                    if mode in ("move", "replace") and new_artpath:
                        art_val = row[0].decode("utf-8", "replace") if isinstance(row[0], bytes) else str(row[0]) if row and row[0] else ""
                        if art_val != new_artpath:
                            return _fail("Post-write verification failed: artpath does not match expected target.", "album_artwork_verification_failed")
                    else:
                        if row and row[0]:
                            return _fail("Post-write verification failed: artpath not cleared.", "album_artwork_verification_failed")
                finally:
                    con.close()

            is_durable = True
            if mode in ("move", "replace") and new_artpath and Path(new_artpath).exists():
                try:
                    fd = os.open(new_artpath, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                except OSError:
                    is_durable = False
                fn_fsync = getattr(__import__("sys").modules.get("backend.beets_control_agent"), "_fsync_file", None)
                if fn_fsync and callable(fn_fsync):
                    try:
                        fn_fsync(new_artpath)
                    except OSError:
                        is_durable = False
                    except Exception as ex:
                        # Wave 24 final review round 3 (surfaced once the
                        # Stage-3 verification false-positive above was
                        # fixed -- a prior revision of that check always
                        # failed first on any replace-over-existing-art,
                        # which coincidentally masked this call ever being
                        # reached). A non-OSError failure here is still a
                        # genuine post-write step failure, not merely
                        # "durability unknown": every mutation (quarantine
                        # + write + DB pointer update) has already
                        # committed by this point, so treat it the same as
                        # any other post-write failure -- roll back fully
                        # and report it, rather than either silently
                        # downgrading durability or letting the exception
                        # escape this function uncaught (which would leave
                        # committed mutations standing with no rollback and
                        # no controlled error response).
                        rollback_album_artwork(store, operation_id, music_allowed_roots=allowed_roots, db_path=lib_db)
                        return _fail(f"Post-write durability check failed: {ex}", "album_artwork_durability_check_failed")

            store.update(operation_id, status="Completed", metadata={
                **store.get(operation_id).get("metadata", {}),
                "db_mutated": db_mutated,
                "completed_at": _now(),
                "durable": is_durable,
            })

            return {
                "ok": True, "operation_id": operation_id, "status": "Completed", "mutated": True,
                "mode": mode, "target_dir": target_dir_str if mode in ("move", "replace") else None,
                "artpath": new_artpath if mode in ("move", "replace") else "",
                "durable": is_durable,
                "moved_count": len(moved_records), "quarantined_count": len(quarantined_records),
                "quarantined_records": quarantined_records,
                "moved_records": moved_records,
            }


def rollback_album_artwork(
    store: TransactionStore,
    operation_id: str,
    *,
    music_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Roll back an album_artwork_v1 transaction by restoring artwork files and DB pointer."""
    if not _TRANSACTION_ID_RE.match(operation_id):
        return {"ok": False, "error": "Invalid transaction ID format", "code": "album_artwork_invalid_id"}

    with _get_apply_lock(operation_id):
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": f"Transaction {operation_id} not found", "code": "album_artwork_not_found"}

        meta = tx.get("metadata") or {}
        if meta.get("mutation_family") != "album_artwork_v1":
            return {"ok": False, "error": "Transaction is not an album_artwork_v1 operation", "code": "album_artwork_family_mismatch"}

        if tx.get("status") == "Rolled Back":
            return {"ok": False, "error": "Transaction is already rolled back.", "code": "album_artwork_already_rolled_back"}

        if not meta.get("filesystem_mutated") and not meta.get("db_mutated"):
            return {"ok": False, "error": "No mutations were performed to roll back.", "code": "album_artwork_no_mutations"}

        resource_keys = meta.get("resource_keys") or []
        allowed_roots = music_allowed_roots or meta.get("allowed_roots") or [str(os.environ.get("MUSIC_ROOT", "/music"))]
        source_roots = [Path(r) for r in (meta.get("source_roots") or allowed_roots)]
        lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")

        with _lock_resources(resource_keys):
            quarantined_records = meta.get("quarantined_records") or []
            moved_records = meta.get("moved_records") or []
            files_restored = 0
            files_failed = 0

            for qr in quarantined_records:
                q_p = Path(qr["quarantined"])
                orig_p = Path(qr["source"])
                if not any(_path_under(orig_p, r) for r in source_roots) or orig_p.is_symlink():
                    files_failed += 1
                    continue
                if not q_p.exists():
                    files_failed += 1
                    continue
                try:
                    orig_p.parent.mkdir(parents=True, exist_ok=True)
                    if _path_has_symlink_under(orig_p.parent, source_roots[0]) if source_roots else False:
                        files_failed += 1
                        continue
                    tmp_bak = orig_p.with_suffix(".tmp_bak") if orig_p.exists() else None
                    if tmp_bak and orig_p.resolve(strict=False) != q_p.resolve(strict=False):
                        try:
                            _safe_rename(orig_p, tmp_bak)
                        except Exception:
                            tmp_bak = None
                    try:
                        try:
                            _safe_rename(q_p, orig_p)
                        except Exception:
                            shutil.copyfile(str(q_p), str(orig_p))
                        if tmp_bak and tmp_bak.exists():
                            try:
                                tmp_bak.unlink()
                            except Exception:
                                pass
                    except Exception:
                        if tmp_bak and tmp_bak.exists():
                            try:
                                shutil.copyfile(str(tmp_bak), str(orig_p))
                                tmp_bak.unlink()
                            except Exception:
                                pass
                        raise
                    files_restored += 1
                except Exception:
                    files_failed += 1

            q_sources = {qr["source"] for qr in quarantined_records if isinstance(qr, dict) and "source" in qr}
            for mr in reversed(moved_records):
                dest_p = Path(mr["destination"])
                orig_p = Path(mr["source"])
                if str(dest_p) in q_sources or str(Path(dest_p).resolve(strict=False)) in {str(Path(s).resolve(strict=False)) for s in q_sources}:
                    if orig_p.exists() and orig_p.resolve(strict=False) != dest_p.resolve(strict=False):
                        try:
                            orig_p.unlink()
                        except Exception:
                            pass
                    continue
                if not any(_path_under(orig_p, r) for r in source_roots) or orig_p.exists() or orig_p.is_symlink():
                    files_failed += 1
                    continue
                if not dest_p.exists():
                    files_failed += 1
                    continue
                # Wave 24 review section 6/18: rollback must not overwrite
                # a legitimate later change. If the file currently at
                # dest_p no longer matches what Apply actually wrote there,
                # something else has touched it since -- moving it back to
                # orig_p would silently destroy that newer state.
                dest_after = mr.get("dest_stat_after_move")
                if dest_after:
                    cur_st = dest_p.stat()
                    if (cur_st.st_dev != dest_after.get("dev") or cur_st.st_ino != dest_after.get("ino")
                            or cur_st.st_size != dest_after.get("size") or cur_st.st_mtime_ns != dest_after.get("mtime_ns")):
                        files_failed += 1
                        continue
                try:
                    orig_p.parent.mkdir(parents=True, exist_ok=True)
                    _safe_rename(dest_p, orig_p)
                    files_restored += 1
                except OSError:
                    files_failed += 1

            aid = int(meta.get("album_id") or 0)
            orig_art = meta.get("original_artpath") or ""
            db_restored = True
            if meta.get("db_mutated") and lib_db and Path(lib_db).exists() and aid > 0:
                con = sqlite3.connect(lib_db, timeout=10)
                try:
                    cur = con.execute("UPDATE albums SET artpath=? WHERE id=?", (orig_art.encode("utf-8") if orig_art else None, aid))
                    db_restored = cur.rowcount == 1
                    con.commit()
                finally:
                    con.close()

            if files_failed == 0 and db_restored:
                final_status = "Rolled Back"
                ok = True
            elif files_restored > 0 or (db_restored and quarantined_records + moved_records):
                final_status = "Partially Rolled Back"
                ok = False
            else:
                final_status = "Failed"
                ok = False

            store.update(operation_id, status=final_status, metadata={
                **meta,
                "rollback_available": False,
                "files_restored_count": files_restored,
                "files_failed_count": files_failed,
                "rolled_back_at": _now(),
            })

            return {
                "ok": ok, "operation_id": operation_id, "status": final_status,
                "files_restored": files_restored, "files_failed": files_failed,
                "partial_mutation": final_status == "Partially Rolled Back",
            }


# ── album_artwork_fetch_v1 ────────────────────────────────────────────────────
# SEC-002 / ARCH-003 Wave 25 Docker acceptance round: import-time artwork
# acquisition (fetchart + embedart) was routed through the generic
# beets_client.run_command()/POST /commands/execute mechanism -- real
# functionality (see fetch_and_embed_album_art()'s own docstring for why a
# generic engine command runner is the right transport), but left as an
# unexplained exception to the controlled-mutation model with no Plan,
# Apply-time precondition check, or Verify step of its own. This narrow
# family wraps that same underlying mechanism in real Plan/Apply/Verify
# semantics, matching every other *_v1 family's contract, WITHOUT
# reimplementing fetchart/embedart's own logic -- Beets remains entirely
# responsible for provider selection, image fetching, and tag writing;
# this only orchestrates around it.
#
# Deliberately NOT reversible after Apply: embedded artwork tag data
# cannot be truthfully un-embedded (there is no prior tag state to restore
# to that this family captured -- unlike album_artwork_v1, which moves an
# already-known file and so can always restore the original), so there is
# no rollback_album_artwork_fetch(). A partial failure (fetchart succeeds
# but embedart does not confirm, or post-Apply verification fails) leaves
# the transaction in "Recovery Required" status instead of a false
# "Completed" or a "Failed" that would invite an unsafe blind retry.

def create_album_artwork_fetch_plan(
    store: TransactionStore,
    payload: Dict[str, Any],
    *,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a non-mutating preview plan for online artwork acquisition
    (album_artwork_fetch_v1). Unlike album_artwork_v1, the resulting image
    is not yet known at Plan time -- it comes from whichever configured
    fetchart provider(s) return during Apply. Plan captures the album's
    identity and current artwork state only, so Apply can prove the same
    album is still being targeted before mutating it."""
    payload = payload or {}
    try:
        album_id = int(payload.get("album_id") or 0)
    except Exception:
        album_id = 0
    if album_id <= 0:
        return {"ok": False, "error": "Invalid album_id", "code": "album_artwork_fetch_invalid_payload"}

    lib_db = db_path or os.environ.get("BEETS_DB_PATH", os.path.expanduser("~/.config/beets/musiclibrary.blb"))
    if not os.path.exists(lib_db):
        return {"ok": False, "error": f"Beets database file not found at {lib_db}", "code": "album_artwork_fetch_db_missing"}

    try:
        con = sqlite3.connect(lib_db, timeout=10)
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT id, album, albumartist, mb_albumid, mb_releasegroupid, artpath FROM albums WHERE id=?",
            (album_id,),
        ).fetchone()
        item_count = con.execute("SELECT COUNT(*) FROM items WHERE album_id=?", (album_id,)).fetchone()[0]
        con.close()
    except Exception as ex:
        return {"ok": False, "error": f"Database query failed: {ex}", "code": "album_artwork_fetch_db_error"}

    if not row:
        return {"ok": False, "error": f"Album {album_id} not found", "code": "album_artwork_fetch_album_not_found"}
    if not item_count:
        return {"ok": False, "error": f"Album {album_id} has no track items", "code": "album_artwork_fetch_no_items"}

    raw_art = row["artpath"]
    existing_artpath = raw_art.decode("utf-8", "replace") if isinstance(raw_art, bytes) else str(raw_art or "")

    resource_keys = [f"album:{album_id}"]
    tx = store.create(
        operation_type="Artwork Update",
        status="Preview",
        summary=f"Fetch and embed artwork for album {album_id} ({row['albumartist'] or ''} - {row['album'] or ''})",
        metadata={
            "mutation_family": "album_artwork_fetch_v1",
            "album_id": album_id,
            "mb_albumid": str(row["mb_albumid"] or ""),
            "mb_releasegroupid": str(row["mb_releasegroupid"] or ""),
            "existing_artpath": existing_artpath,
            "existing_artpath_present": bool(existing_artpath),
            "resource_keys": resource_keys,
            "reversible": False,
            "created_at": _now(),
        },
    )
    return {
        "ok": True,
        "operation_id": tx["id"],
        "album_id": album_id,
        "existing_artpath": existing_artpath,
    }


def execute_album_artwork_fetch_apply(
    store: TransactionStore,
    operation_id: str,
    *,
    db_path: Optional[str] = None,
    run_beet_command_fn: Optional[Any] = None,
) -> Dict[str, Any]:
    """Execute album_artwork_fetch_v1: run native Beets fetchart + embedart
    for the planned album, then verify the resulting artwork state.

    run_beet_command_fn is a required dependency injected by the caller
    (the Control Agent, which owns the actual `beet` subprocess machinery)
    -- transaction_engine.py deliberately never invokes `beet` subcommands
    itself, matching the same fetch_tracklist_fn injection pattern already
    used by create_album_mb_track_repair_plan. Expected signature:
    run_beet_command_fn(command: str, args: List[str]) ->
    {"ok": bool, "error": Optional[str], ...}.
    """
    if not _TRANSACTION_ID_RE.match(operation_id):
        return {"ok": False, "error": "Invalid transaction ID format", "code": "album_artwork_fetch_invalid_id"}
    if run_beet_command_fn is None:
        return {"ok": False, "error": "No beet command runner configured", "code": "album_artwork_fetch_no_runner"}

    with _get_apply_lock(operation_id):
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": f"Transaction {operation_id} not found", "code": "album_artwork_fetch_not_found"}

        meta = tx.get("metadata") or {}
        if meta.get("mutation_family") != "album_artwork_fetch_v1":
            return {"ok": False, "error": "Transaction is not an album_artwork_fetch_v1 operation", "code": "album_artwork_fetch_family_mismatch"}

        if tx.get("status") == "Completed":
            return {
                "ok": True, "operation_id": operation_id, "status": "Completed",
                "mutated": True, "artpath": meta.get("result_artpath", ""),
            }
        if tx.get("status") == "Recovery Required":
            return {
                "ok": False,
                "error": "Transaction requires manual recovery; a prior Apply attempt did not complete cleanly",
                "code": "album_artwork_fetch_recovery_required",
                "status": "Recovery Required",
            }

        album_id = int(meta.get("album_id") or 0)
        lib_db = db_path or os.environ.get("BEETS_DB_PATH", os.path.expanduser("~/.config/beets/musiclibrary.blb"))
        resource_keys = meta.get("resource_keys") or []

        def _fail(msg: str, code: str) -> Dict[str, Any]:
            store.update(operation_id, status="Failed", logs=[f"Apply failed: {msg}"])
            return {"ok": False, "error": msg, "code": code, "mutated": False, "status": "Failed"}

        def _recovery(msg: str, code: str, reason: str) -> Dict[str, Any]:
            curr = store.get(operation_id).get("metadata", {})
            store.update(operation_id, status="Recovery Required", metadata={
                **curr,
                "filesystem_mutated": True,
                "recovery_required_at": _now(),
                "recovery_reason": reason,
            })
            return {"ok": False, "error": msg, "code": code, "mutated": True, "status": "Recovery Required"}

        with _lock_resources(resource_keys):
            # TOCTOU precondition: the album's identity must not have
            # changed since Plan -- a different album entirely must never
            # silently receive this artwork.
            try:
                con = sqlite3.connect(lib_db, timeout=10)
                con.row_factory = sqlite3.Row
                row = con.execute(
                    "SELECT id, mb_albumid, mb_releasegroupid FROM albums WHERE id=?",
                    (album_id,),
                ).fetchone()
                con.close()
            except Exception as ex:
                return _fail(f"Database query failed: {ex}", "album_artwork_fetch_db_error")
            if not row:
                return _fail(f"Album {album_id} no longer exists", "album_artwork_fetch_album_not_found")
            cur_mbid = str(row["mb_albumid"] or "")
            cur_rgid = str(row["mb_releasegroupid"] or "")
            if meta.get("mb_albumid") and cur_mbid and meta["mb_albumid"] != cur_mbid:
                return _fail("Album identity changed since plan (mb_albumid mismatch)", "album_artwork_fetch_stale_plan")
            if meta.get("mb_releasegroupid") and cur_rgid and meta["mb_releasegroupid"] != cur_rgid:
                return _fail("Album identity changed since plan (release group mismatch)", "album_artwork_fetch_stale_plan")

            store.update(operation_id, status="Running", metadata={**meta, "apply_started": True})

            fetch_res = run_beet_command_fn("fetchart", [f"album_id:{album_id}"])
            if not fetch_res.get("ok"):
                return _fail(fetch_res.get("error") or "fetchart command failed", "album_artwork_fetch_fetchart_failed")

            embed_res = run_beet_command_fn("embedart", ["-y", f"album_id:{album_id}"])
            if not embed_res.get("ok"):
                # fetchart already ran and may have written a real file +
                # DB artpath -- real partial work that cannot be blindly
                # discarded, but the tag-embed half never confirmed.
                return _recovery(
                    embed_res.get("error") or "embedart command failed after fetchart succeeded",
                    "album_artwork_fetch_embedart_failed",
                    "fetchart succeeded but embedart did not confirm",
                )

            # Verify: the album must now have a real artpath recorded.
            try:
                con = sqlite3.connect(lib_db, timeout=10)
                con.row_factory = sqlite3.Row
                vrow = con.execute("SELECT artpath FROM albums WHERE id=?", (album_id,)).fetchone()
                con.close()
            except Exception as ex:
                return _recovery(f"Post-apply verification query failed: {ex}", "album_artwork_fetch_verify_query_failed", f"post-apply verification query failed: {ex}")

            raw_v = vrow["artpath"] if vrow else None
            result_artpath = raw_v.decode("utf-8", "replace") if isinstance(raw_v, bytes) else str(raw_v or "")
            if not result_artpath:
                return _recovery(
                    "Post-apply verification failed: artpath not set after fetchart/embedart",
                    "album_artwork_fetch_verification_failed",
                    "fetchart/embedart reported success but albums.artpath is still empty",
                )

            store.update(operation_id, status="Completed", metadata={
                **store.get(operation_id).get("metadata", {}),
                "filesystem_mutated": True,
                "result_artpath": result_artpath,
                "completed_at": _now(),
            })
            return {"ok": True, "operation_id": operation_id, "status": "Completed", "mutated": True, "artpath": result_artpath}


# ── confirmed_import_v1 ──────────────────────────────────────────────────────
#
# Wave 25 Round 3: a distinct controlled family for FRESH REVIEWED imports of
# previously-untagged source audio, deliberately separate from
# reimport_source_atomic()/verify_deterministic_identity() (which remain
# unchanged and continue to gate genuine *re*-imports of already-tagged
# library content). See docs/operations/wave25_import_reconciliation_design.md
# for the full security-model writeup ("REIMPORT TRUST MODEL" vs "FRESH
# REVIEWED IMPORT TRUST MODEL").
#
# Trust model: missing embedded MusicBrainz tags are the EXPECTED case for a
# fresh download and are never treated as identity evidence one way or the
# other. Authorization instead comes from binding together an immutable
# source manifest digest (re-checked at Apply -- any change invalidates the
# Plan), the caller's explicitly reviewed concrete target Release ID, the
# target's real Release Group (validated, never substituted), and
# best-effort track alignment / AcoustID fingerprint evidence (reusing
# backend.track_align, not a new matcher). A CONFLICTING embedded tag
# (present and different from the target) is still a hard rejection --
# only the ABSENCE of tags is treated as normal here.

def _confirmed_import_embedded_tag_conflict(
    audio_files: List[Dict[str, Any]], target_mb_albumid: str, target_mb_rgid: str,
) -> Optional[Dict[str, Any]]:
    """A source file with NO embedded MusicBrainz tags is the expected case
    for a fresh download and is not a conflict. A source file whose embedded
    tags are PRESENT and DIFFERENT from the approved target is a real
    conflict this family must never silently overwrite."""
    for entry in audio_files:
        props = entry.get("properties") or {}
        embedded_albumid = str(props.get("mb_albumid") or "").strip().lower()
        if embedded_albumid and target_mb_albumid and embedded_albumid != target_mb_albumid:
            return {
                "relative_path": entry.get("relative_path", ""),
                "embedded_mb_albumid": embedded_albumid,
                "reason": "embedded_mb_albumid_conflict",
            }
        embedded_rgid = str(props.get("mb_releasegroupid") or "").strip().lower()
        if embedded_rgid and target_mb_rgid and embedded_rgid != target_mb_rgid:
            return {
                "relative_path": entry.get("relative_path", ""),
                "embedded_mb_releasegroupid": embedded_rgid,
                "reason": "embedded_mb_releasegroupid_conflict",
            }
    return None


def _confirmed_import_title_similarity(file_path: str, mb_title_norm: str) -> float:
    """Self-contained title similarity for track_align.align_tracks's
    similarity_fn -- deliberately not a new matcher, just enough
    normalization to compare a local filename stem against an already-
    normalized MusicBrainz track title."""
    stem = Path(str(file_path)).stem
    norm_stem = re.sub(r"[^a-z0-9]+", " ", stem.casefold()).strip()
    norm_target = re.sub(r"[^a-z0-9]+", " ", str(mb_title_norm or "").casefold()).strip()
    if not norm_stem or not norm_target:
        return 0.0
    from difflib import SequenceMatcher
    score = SequenceMatcher(None, norm_stem, norm_target).ratio()
    if set(norm_stem.split()) & set(norm_target.split()):
        score = max(score, 0.70)
    return score


def _confirmed_import_acoustid_conflicts(
    comparison: List[Dict[str, Any]], mb_tracks: List[Dict[str, Any]], acoustid_lookup_fn: Optional[Any],
    *, min_conflict_score: int = 85,
) -> List[Dict[str, Any]]:
    """Best-effort additional conflict check (on top of
    resolve_unmatched_via_acoustid's own promotion of missing rows): flag an
    unmatched ('extra') source file whose fingerprint strongly identifies a
    KNOWN MusicBrainz recording that is not anywhere in the target release's
    own tracklist -- a real contradiction, not merely an unmatched title.
    Only runs when acoustid_lookup_fn is supplied; absence of fingerprint
    evidence is never itself treated as a conflict."""
    if acoustid_lookup_fn is None:
        return []
    target_trackids = {str(t.get("mb_trackid") or "").strip().lower() for t in mb_tracks if t.get("mb_trackid")}
    conflicts: List[Dict[str, Any]] = []
    for row in comparison:
        if row.get("status") != "extra" or not row.get("file_path"):
            continue
        try:
            hits = acoustid_lookup_fn(row["file_path"]) or []
        except Exception:
            hits = []
        for hit in hits:
            try:
                score = int(hit.get("score") or 0)
            except Exception:
                score = 0
            hit_trackid = str(hit.get("mb_trackid") or "").strip().lower()
            if score >= min_conflict_score and hit_trackid and hit_trackid not in target_trackids:
                conflicts.append({
                    "file_path": row["file_path"],
                    "acoustid_score": score,
                    "conflicting_mb_trackid": hit_trackid,
                    "conflicting_title": hit.get("title", ""),
                })
                break
    return conflicts


def create_confirmed_import_plan(
    store: TransactionStore,
    payload: Dict[str, Any],
    *,
    staging_allowed_roots: Optional[List[str]] = None,
    music_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
    inspect_source_fn: Optional[Any] = None,
    acoustid_lookup_fn: Optional[Any] = None,
) -> Dict[str, Any]:
    """Create a non-mutating Plan for confirmed_import_v1.

    inspect_source_fn(source_folder) -> dict is a required dependency
    (matching the fetch_tracklist_fn/run_beet_command_fn injection pattern
    already used elsewhere in this file) -- this module has no filesystem
    access of its own; only the Control Agent, which owns the real mounted
    staging/music roots, can safely read source audio. Expected shape
    matches beets_control_agent.inspect_import_source(): {"ok",
    "canonical_path", "audio_files": [{"relative_path","size","mtime_ns",
    "properties"}], "source_signature"}.

    acoustid_lookup_fn(file_path) -> list[dict] is optional best-effort
    fingerprint evidence (track_align.AcoustidLookupFn shape); omitted
    entirely when fpcalc/AcoustID is unavailable -- never required.

    Payload: source_folder, mb_albumid (concrete Release ID, required),
    mb_releasegroupid (optional), mb_release_group_resolved (the release's
    actual Release Group as resolved by whoever fetched mb_tracks -- the web
    manager, which has MusicBrainz API access this module deliberately does
    not), mb_tracks (pre-fetched tracklist, required, non-empty),
    existing_album_id (optional), use_move (bool).
    """
    payload = payload or {}
    source_folder = str(payload.get("source_folder") or "").strip()
    if not source_folder:
        return {"ok": False, "error": "source_folder required", "code": "confirmed_import_invalid_payload"}

    target_mb_albumid = str(payload.get("mb_albumid") or "").strip().lower()
    if not _MB_UUID_RE.match(target_mb_albumid):
        return {"ok": False, "error": "A concrete MusicBrainz Release ID is required", "code": "confirmed_import_invalid_release_id"}

    target_mb_rgid = str(payload.get("mb_releasegroupid") or "").strip().lower()
    if target_mb_rgid and not _MB_UUID_RE.match(target_mb_rgid):
        return {"ok": False, "error": "mb_releasegroupid is not a valid MusicBrainz UUID", "code": "confirmed_import_invalid_rgid"}

    resolved_mb_rgid = str(payload.get("mb_release_group_resolved") or "").strip().lower()
    if resolved_mb_rgid and not _MB_UUID_RE.match(resolved_mb_rgid):
        resolved_mb_rgid = ""
    if target_mb_rgid and resolved_mb_rgid and target_mb_rgid != resolved_mb_rgid:
        return {
            "ok": False,
            "error": "The selected Release does not belong to the approved Release Group",
            "code": "confirmed_import_release_group_mismatch",
        }
    final_rgid = target_mb_rgid or resolved_mb_rgid

    mb_tracks = payload.get("mb_tracks")
    if not isinstance(mb_tracks, list) or not mb_tracks:
        return {"ok": False, "error": "MusicBrainz release tracklist is required and must be non-empty", "code": "confirmed_import_missing_tracklist"}

    src_p = Path(source_folder)
    stg_roots = staging_allowed_roots or [str(os.environ.get("DOWNLOADS_ROOT", "/downloads")), str(os.environ.get("STAGING_ROOT", "/staging"))]
    mus_roots = music_allowed_roots or [str(os.environ.get("MUSIC_ROOT", "/music"))]
    all_roots = stg_roots + mus_roots
    if not any(_path_under(src_p, Path(r)) for r in all_roots):
        return {"ok": False, "error": f"Source folder outside allowed roots: {src_p}", "code": "confirmed_import_path_out_of_root"}
    if any(_path_has_symlink_under(src_p, Path(r)) for r in all_roots):
        return {"ok": False, "error": f"Symlink rejected: {src_p}", "code": "confirmed_import_symlink_rejected"}

    if inspect_source_fn is None:
        return {"ok": False, "error": "No source inspector configured", "code": "confirmed_import_no_inspector"}
    insp = inspect_source_fn(str(src_p))
    if not isinstance(insp, dict) or not insp.get("ok"):
        return {
            "ok": False,
            "error": (insp.get("error_code") if isinstance(insp, dict) else None) or "Source inspection failed",
            "code": "confirmed_import_inspection_failed",
        }
    audio_files = insp.get("audio_files") or []
    if not audio_files:
        return {"ok": False, "error": "Source folder contains no audio files", "code": "confirmed_import_no_audio"}

    conflict = _confirmed_import_embedded_tag_conflict(audio_files, target_mb_albumid, final_rgid)
    if conflict:
        return {
            "ok": False,
            "error": (
                "Source audio already carries a conflicting MusicBrainz identity; "
                "a confirmed fresh import never overwrites trusted contradictory "
                "identity. Missing tags are fine -- conflicting tags are not the "
                "same thing."
            ),
            "code": "confirmed_import_embedded_identity_conflict",
            "conflict": conflict,
        }

    canonical_path = str(insp.get("canonical_path") or src_p)
    local_files = [str(Path(canonical_path) / e["relative_path"]) for e in audio_files]

    try:
        from track_align import align_tracks, resolve_unmatched_via_acoustid
    except ImportError:
        from backend.track_align import align_tracks, resolve_unmatched_via_acoustid

    comparison = align_tracks(local_files, mb_tracks, _confirmed_import_title_similarity)
    if acoustid_lookup_fn is not None:
        try:
            resolve_unmatched_via_acoustid(comparison, acoustid_lookup_fn, fpcalc_available=True)
        except Exception:
            pass

    present_statuses = {"matched", "fuzzy", "acoustid_verified"}
    present_count = sum(1 for row in comparison if row.get("status") in present_statuses)
    missing_count = sum(1 for row in comparison if row.get("status") == "missing")
    extra_count = sum(1 for row in comparison if row.get("status") == "extra")
    if present_count == 0:
        return {
            "ok": False,
            "error": "No source track could be aligned to the selected release's tracklist; refusing to import what does not appear to be this release",
            "code": "confirmed_import_no_track_alignment",
        }

    acoustid_conflicts = _confirmed_import_acoustid_conflicts(comparison, mb_tracks, acoustid_lookup_fn)
    if acoustid_conflicts:
        return {
            "ok": False,
            "error": "Fingerprint evidence identifies source audio as a different, known MusicBrainz recording than the approved release; refusing to import",
            "code": "confirmed_import_acoustid_conflict",
            "conflicts": acoustid_conflicts,
        }

    try:
        existing_album_id = int(payload.get("existing_album_id") or 0)
    except Exception:
        existing_album_id = 0
    use_move = bool(payload.get("use_move", False))

    resource_keys = [f"import-source:{canonical_path}"]
    if existing_album_id > 0:
        resource_keys.append(f"album:{existing_album_id}")

    # Round 4 brief section 11-12: make the partial-import policy explicit
    # in the Plan itself rather than leaving "present_count > 0" as the
    # only visible signal. This does not change the accept/reject
    # threshold (a reviewed incomplete album is still allowed, matching
    # the existing present_count == 0 rejection above) -- it makes that
    # policy legible to whoever reviews or audits a given Plan, and gives
    # a future stricter policy (e.g. a minimum coverage ratio) something
    # concrete to gate on without inventing a second scoring system.
    coverage_ratio = round(present_count / len(mb_tracks), 4) if mb_tracks else 0.0
    track_coverage = {
        "expected_track_count": len(mb_tracks),
        "present_track_count": present_count,
        "missing_track_count": missing_count,
        "extra_track_count": extra_count,
        "coverage_ratio": coverage_ratio,
        "partial_import": present_count < len(mb_tracks),
        "review_required": present_count < len(mb_tracks),
    }
    tx = store.create(
        operation_type="Confirmed Import",
        status="Preview",
        summary=f"Confirmed import ({len(audio_files)} file(s)) -> release {target_mb_albumid}",
        metadata={
            "mutation_family": "confirmed_import_v1",
            "source_folder": canonical_path,
            "source_manifest_digest": insp.get("source_signature", ""),
            "target_mb_albumid": target_mb_albumid,
            "target_mb_releasegroupid": final_rgid,
            "existing_album_id": existing_album_id,
            "use_move": use_move,
            "track_alignment": comparison,
            "track_coverage": track_coverage,
            "resource_keys": resource_keys,
            "reversible": False,
            "created_at": _now(),
        },
    )
    return {
        "ok": True,
        "operation_id": tx["id"],
        "source_folder": canonical_path,
        "audio_count": len(audio_files),
        "track_alignment": comparison,
        "track_coverage": track_coverage,
    }


def execute_confirmed_import_apply(
    store: TransactionStore,
    operation_id: str,
    *,
    music_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
    inspect_source_fn: Optional[Any] = None,
    run_native_import_fn: Optional[Any] = None,
    acceptance_failpoint: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute confirmed_import_v1: revalidate the immutable source manifest,
    run native Beets --search-id import via the injected runner (NEVER
    reimport_source_atomic() -- that function's identity gate requires
    pre-existing embedded tags this family's whole purpose is to establish
    for the first time), capture the resulting album/item identity by
    querying the library for the planned Release ID (never guessed from
    "most recently added"), and verify the result before reporting success.

    run_native_import_fn(source_path, mb_albumid, *, use_move) ->
    {"ok", "error"} is a required dependency (Control Agent-owned subprocess
    mechanics), matching the run_beet_command_fn/beets_import_runner
    precedent -- transaction_engine.py never invokes `beet` itself.

    acceptance_failpoint is test-only infrastructure (Round 4): when equal
    to "confirmed_import_after_native", Apply deterministically stops
    right after the native_import_invoked checkpoint is persisted and
    before the post-import capture/verify step -- simulating a process
    crash in that exact window for the dedicated Docker acceptance
    scenario, without a timing race. The caller (the Control Agent route)
    is responsible for only ever passing a non-None value when running in
    an explicit acceptance/test container; this function does not itself
    know or care why the caller chose to pass it, it just honors it.

    Non-reversible after Apply, same as album_artwork_fetch_v1: undoing a
    real Beets import (tags written, files possibly moved/copied,
    reconciled against a live library) cannot be truthfully guaranteed, so
    no rollback is offered rather than a rollback that might lie.
    """
    if not _TRANSACTION_ID_RE.match(operation_id):
        return {"ok": False, "error": "Invalid transaction ID format", "code": "confirmed_import_invalid_id"}

    with _get_apply_lock(operation_id):
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": f"Transaction {operation_id} not found", "code": "confirmed_import_not_found"}

        meta = tx.get("metadata") or {}
        if meta.get("mutation_family") != "confirmed_import_v1":
            return {"ok": False, "error": "Transaction is not a confirmed_import_v1 operation", "code": "confirmed_import_family_mismatch"}

        if tx.get("status") == "Completed":
            return {
                "ok": True, "operation_id": operation_id, "status": "Completed", "mutated": True,
                "album_id": meta.get("result_album_id"), "item_ids": meta.get("result_item_ids") or [],
            }
        if tx.get("status") == "Recovery Required":
            return {
                "ok": False,
                "error": "Transaction requires manual recovery; a prior Apply attempt did not complete cleanly",
                "code": "confirmed_import_recovery_required",
                "status": "Recovery Required",
            }

        target_mb_albumid = str(meta.get("target_mb_albumid") or "")
        target_mb_rgid = str(meta.get("target_mb_releasegroupid") or "")
        source_folder = str(meta.get("source_folder") or "")
        resource_keys = meta.get("resource_keys") or []
        lib_db = db_path or os.environ.get("BEETS_DB_PATH", os.path.expanduser("~/.config/beets/musiclibrary.blb"))

        def _fail(msg: str, code: str, *, diagnostics: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            curr = store.get(operation_id).get("metadata", {})
            update_meta = {**curr, "last_failure_diagnostics": diagnostics} if diagnostics else curr
            store.update(operation_id, status="Failed", metadata=update_meta, logs=[f"Apply failed: {msg}"])
            result = {"ok": False, "error": msg, "code": code, "mutated": False, "status": "Failed"}
            if diagnostics:
                # Surfaced to the HTTP caller too (not just persisted), so
                # the real native beet stdout/stderr is actually visible to
                # whoever is debugging a real failure instead of requiring
                # a separate transaction-store lookup (Round 4 brief
                # section 2/13).
                result["diagnostics"] = diagnostics
            return result

        def _recovery(msg: str, code: str, reason: str, *, diagnostics: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            curr = store.get(operation_id).get("metadata", {})
            update_meta = {
                **curr, "filesystem_mutated": True,
                "recovery_required_at": _now(), "recovery_reason": reason,
            }
            if diagnostics:
                update_meta["last_failure_diagnostics"] = diagnostics
            store.update(operation_id, status="Recovery Required", metadata=update_meta)
            result = {"ok": False, "error": msg, "code": code, "mutated": True, "status": "Recovery Required"}
            if diagnostics:
                result["diagnostics"] = diagnostics
            return result

        def _capture_and_verify(*, require_files_present: bool) -> Optional[Dict[str, Any]]:
            """Query the library for the planned Release ID and verify the
            result. Returns None when no album with this Release ID exists
            at all (the pre-check calls this to mean "not imported yet";
            the post-import call to this same helper never receives None as
            an acceptable outcome). Returns {"ok": False, ...} when an album
            exists but cannot be positively verified -- never guessed."""
            try:
                con = sqlite3.connect(lib_db, timeout=10)
                con.row_factory = sqlite3.Row
                album_row = con.execute(
                    "SELECT id, mb_albumid, mb_releasegroupid FROM albums WHERE mb_albumid = ?",
                    (target_mb_albumid,),
                ).fetchone()
                if not album_row:
                    con.close()
                    return None
                item_rows = con.execute(
                    "SELECT id, path FROM items WHERE album_id = ?", (album_row["id"],)
                ).fetchall()
                con.close()
            except Exception:
                return {"ok": False, "reason": "db_query_failed"}

            if not item_rows:
                return {"ok": False, "reason": "no_items", "album_id": int(album_row["id"])}

            music_root = Path((music_allowed_roots or [os.environ.get("MUSIC_ROOT", "/music")])[0])
            files_present = 0
            missing_item_ids: List[int] = []
            for irow in item_rows:
                raw = irow["path"]
                p = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
                pp = Path(p)
                if not pp.is_absolute():
                    pp = music_root / p
                if pp.exists():
                    files_present += 1
                else:
                    missing_item_ids.append(int(irow["id"]))
            # Round 4 (real Docker acceptance finding): this used to accept
            # ANY nonzero files_present count as fully verified -- a real
            # import that copied some but not all of an album's items (a
            # partial copy failure, not a clean skip) still reported
            # ok=True here, with every item_id (including the ones with no
            # backing file) returned to the caller as part of a "Completed"
            # result. "Confirmed" means every item this result claims to
            # have imported is actually present on disk -- not merely that
            # the album isn't completely empty. A legitimately incomplete
            # album (fewer *tracks* than the full MB release, handled by
            # confirmed_import_v1's own track_coverage/review policy at
            # Plan time) is a different, already-reviewed concept from an
            # item *row* that exists without a real file behind it.
            if require_files_present:
                if files_present == 0:
                    return {"ok": False, "reason": "no_files_on_disk", "album_id": int(album_row["id"])}
                if missing_item_ids:
                    return {
                        "ok": False, "reason": "incomplete_files_on_disk", "album_id": int(album_row["id"]),
                        "files_present": files_present, "items_total": len(item_rows),
                        "missing_item_ids": missing_item_ids[:20],
                    }

            if target_mb_rgid:
                actual_rgid = str(album_row["mb_releasegroupid"] or "").strip().lower()
                if actual_rgid and actual_rgid != target_mb_rgid:
                    return {"ok": False, "reason": "releasegroup_mismatch", "album_id": int(album_row["id"])}

            return {
                "ok": True,
                "album_id": int(album_row["id"]),
                "item_ids": [int(r["id"]) for r in item_rows],
                "files_present": files_present,
                "items_total": len(item_rows),
            }

        with _lock_resources(resource_keys):
            # Idempotency / crash-resume: if a verifiable album for this
            # exact target Release ID already exists, never invoke native
            # import a second time -- resume the existing result instead.
            # This is the mechanism that makes both "retry an already-
            # completed import" and "resume after a crash that happened
            # after native import succeeded but before Completed was
            # recorded" safe (the same mb_albumid uniqueness signal
            # reimport_source_atomic's own result-capture already relies
            # on elsewhere in this file).
            existing = _capture_and_verify(require_files_present=True)
            if existing and existing.get("ok"):
                store.update(operation_id, status="Completed", metadata={
                    **meta, "filesystem_mutated": True,
                    "result_album_id": existing["album_id"],
                    "result_item_ids": existing["item_ids"],
                    "resumed_existing_result": True,
                    "completed_at": _now(),
                })
                return {
                    "ok": True, "operation_id": operation_id, "status": "Completed", "mutated": True,
                    "album_id": existing["album_id"], "item_ids": existing["item_ids"], "resumed": True,
                }
            if existing and not existing.get("ok"):
                return _fail(
                    f"An album for this Release ID already exists but could not be verified ({existing.get('reason')})",
                    "confirmed_import_existing_unverified",
                )

            if inspect_source_fn is None:
                return _fail("No source inspector configured", "confirmed_import_no_inspector")
            insp = inspect_source_fn(source_folder)
            if not isinstance(insp, dict) or not insp.get("ok"):
                if meta.get("apply_started"):
                    return _recovery(
                        "Source folder is no longer present and no verified result was found",
                        "confirmed_import_ambiguous_after_prior_attempt",
                        "prior apply attempt was recorded but neither the source nor a verified result could be found",
                    )
                return _fail(
                    (insp.get("error_code") if isinstance(insp, dict) else None) or "Source is no longer available for import",
                    "confirmed_import_source_unavailable",
                )
            if insp.get("source_signature") != meta.get("source_manifest_digest"):
                return _fail(
                    "Source contents changed since this Plan was created",
                    "confirmed_import_stale_plan",
                )

            if not run_native_import_fn:
                return _fail("No native import runner configured", "confirmed_import_no_runner")

            store.update(operation_id, status="Running", metadata={**meta, "apply_started": True})

            import_res = run_native_import_fn(
                source_folder, target_mb_albumid, use_move=bool(meta.get("use_move")),
            )
            diagnostics = {
                "returncode": (import_res or {}).get("returncode"),
                "stdout_excerpt": (import_res or {}).get("stdout_excerpt"),
                "stderr_excerpt": (import_res or {}).get("stderr_excerpt"),
                "albums_before": (import_res or {}).get("albums_before"),
                "albums_after": (import_res or {}).get("albums_after"),
            }
            # Crash-safety checkpoint: record that native import was
            # invoked, regardless of outcome, before doing anything else --
            # a crash between here and Completed leaves an honest trail for
            # the idempotency check above on the next Apply attempt.
            store.update(operation_id, status="Running", metadata={
                **store.get(operation_id).get("metadata", {}),
                "native_import_invoked": True,
                "filesystem_mutated": True,
                "last_native_import_diagnostics": diagnostics,
            })
            if not isinstance(import_res, dict) or not import_res.get("ok"):
                return _fail(
                    (import_res.get("error") if isinstance(import_res, dict) else None) or "Native Beets import failed",
                    "confirmed_import_native_import_failed",
                    diagnostics=diagnostics,
                )

            # Round 4 deterministic acceptance failpoint: only ever honored
            # when the caller (an acceptance-mode-only container) passed
            # it. Fires right here -- after the native_import_invoked
            # checkpoint above is already durably persisted, before this
            # transaction ever captures/verifies its own result -- so a
            # retry's idempotency pre-check (querying the library directly)
            # is the only thing that can discover the real album Beets
            # already created, exactly like a genuine crash in this window.
            if acceptance_failpoint == "confirmed_import_after_native":
                return _recovery(
                    "Acceptance failpoint triggered (test infrastructure only, never reachable in a real deployment)",
                    "confirmed_import_acceptance_failpoint",
                    "deterministic acceptance-only failure injection after native import succeeded, before verification",
                    diagnostics=diagnostics,
                )

            result = _capture_and_verify(require_files_present=True)
            if not result or not result.get("ok"):
                albums_before, albums_after = diagnostics["albums_before"], diagnostics["albums_after"]
                if isinstance(albums_before, int) and isinstance(albums_after, int) and albums_before >= 0 and albums_after == albums_before:
                    # Beets' own album count did not change at all -- a
                    # clean --quiet-fallback skip, not a partial mutation
                    # under the wrong identity. Nothing needs recovering;
                    # this is a truthful, zero-mutation failure.
                    return _fail(
                        "Beets could not confidently apply the approved release to this source and skipped the import (no library mutation occurred)",
                        "confirmed_import_skipped_no_confident_match",
                        diagnostics=diagnostics,
                    )
                verify_reason = (result or {}).get("reason", "no matching album found")
                verify_detail = f"post-import verification failed: {verify_reason}"
                if verify_reason == "incomplete_files_on_disk":
                    verify_detail += (
                        f" ({result.get('files_present')}/{result.get('items_total')} item(s) have a real "
                        f"file; missing item id(s): {result.get('missing_item_ids')})"
                    )
                return _recovery(
                    "Native import reported success but the resulting album could not be verified",
                    "confirmed_import_verification_failed",
                    verify_detail,
                    diagnostics=diagnostics,
                )

            store.update(operation_id, status="Completed", metadata={
                **store.get(operation_id).get("metadata", {}),
                "result_album_id": result["album_id"],
                "result_item_ids": result["item_ids"],
                "completed_at": _now(),
            })
            return {
                "ok": True, "operation_id": operation_id, "status": "Completed", "mutated": True,
                "album_id": result["album_id"], "item_ids": result["item_ids"],
            }


# ── import_folder_v1 ─────────────────────────────────────────────────────────

def create_import_folder_plan(
    store: TransactionStore,
    payload: Dict[str, Any],
    *,
    music_allowed_roots: Optional[List[str]] = None,
    staging_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
    quarantine_base_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a non-mutating preview plan for import folder workflows."""
    src_folder = str(payload.get("source_folder") or payload.get("folder_path") or "").strip()
    if not src_folder:
        return {"ok": False, "error": "source_folder required", "code": "import_folder_invalid_payload"}

    src_p = Path(src_folder)
    allowed_roots = music_allowed_roots or [str(os.environ.get("MUSIC_ROOT", "/music"))]
    # SEC-002 Wave 22 final review, finding #8: an already-managed library
    # folder is not automatically a legitimate import *source* just
    # because it is a legitimate destination -- the default (used only
    # when a caller does not explicitly configure role-specific roots)
    # is staging/downloads only. A caller that has a real "already
    # organized but untracked" use case must opt in explicitly via
    # `staging_allowed_roots`, matching how the engine's existing
    # `reimport_source_atomic` treats that case as a distinct, deliberate
    # code path rather than an implicit default.
    stg_roots = staging_allowed_roots or [str(os.environ.get("DOWNLOADS_ROOT", "/downloads")), str(os.environ.get("STAGING_ROOT", "/staging"))]

    if not any(_path_under(src_p, Path(r)) for r in stg_roots):
        return {"ok": False, "error": f"Source folder outside allowed staging roots: {src_p}", "code": "import_folder_path_out_of_root"}
    if any(_path_has_symlink_under(src_p, Path(r)) for r in stg_roots):
        return {"ok": False, "error": f"Symlink rejected: {src_p}", "code": "import_folder_symlink_rejected"}
    # Wave 25 Docker acceptance round: this used to silently produce an
    # empty files_plan (and a Plan response with ok=True) for a
    # nonexistent or non-directory source -- the caller-side existence
    # check (app.py's _resolve_import_review_source_path) cannot verify
    # this itself (the web-manager container has no visibility into
    # staging/music roots by design), so this engine-side check, which
    # DOES have real access to the mounted filesystem, must be the one
    # that actually fails closed.
    if not src_p.exists():
        return {"ok": False, "error": f"Source folder does not exist: {src_p}", "code": "import_folder_source_not_found"}
    if not src_p.is_dir():
        return {"ok": False, "error": f"Source path is not a folder: {src_p}", "code": "import_folder_source_not_a_directory"}

    album_id = int(payload.get("album_id") or 0)
    mode = str(payload.get("mode") or "import").strip()

    files_plan: List[Dict[str, Any]] = []
    if src_p.exists() and src_p.is_dir():
        for f in src_p.rglob("*"):
            if f.is_file():
                st = f.stat()
                files_plan.append({
                    "source": str(f),
                    "rel_path": str(f.relative_to(src_p)),
                    "stat": {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns},
                })

    resource_keys = [f"import:{src_p.name}"]
    if album_id > 0:
        resource_keys.append(f"album:{album_id}")

    tx = store.create(
        operation_type="Import Folder",
        status="Preview",
        summary=f"Import folder ({mode}): {src_p.name} ({len(files_plan)} file(s))",
        metadata={
            "mutation_family": "import_folder_v1",
            "mode": mode,
            "source_folder": str(src_p),
            "album_id": album_id,
            "files_plan": files_plan,
            "payload": payload,
            "resource_keys": resource_keys,
            "allowed_roots": allowed_roots,
            "created_at": _now(),
        },
    )
    return {
        "ok": True,
        "operation_id": tx["id"],
        "source_folder": str(src_p),
        "file_count": len(files_plan),
    }


def execute_import_folder_apply(
    store: TransactionStore,
    operation_id: str,
    *,
    music_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
    quarantine_base_root: Optional[str] = None,
    beets_import_runner: Optional[Any] = None,
) -> Dict[str, Any]:
    """Execute import folder apply with durable steps and resource locking."""
    if not _TRANSACTION_ID_RE.match(operation_id):
        return {"ok": False, "error": "Invalid transaction ID format", "code": "import_folder_invalid_id"}

    with _get_apply_lock(operation_id):
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": f"Transaction {operation_id} not found", "code": "import_folder_not_found"}

        meta = tx.get("metadata") or {}
        if meta.get("mutation_family") != "import_folder_v1":
            return {"ok": False, "error": "Transaction is not an import_folder_v1 operation", "code": "import_folder_family_mismatch"}

        if tx.get("status") == "Completed":
            return {"ok": True, "operation_id": operation_id, "status": "Completed", "mutated": True}

        resource_keys = meta.get("resource_keys") or []

        def _fail(msg: str, code: str) -> Dict[str, Any]:
            curr = store.get(operation_id)
            c_meta = curr.get("metadata") or {}
            mutated = bool(c_meta.get("filesystem_mutated") or c_meta.get("db_mutated"))
            store.update(operation_id, status="Failed", logs=[f"Apply failed: {msg}"])
            return {"ok": False, "error": msg, "code": code, "mutated": mutated, "rollback_available": mutated}

        with _lock_resources(resource_keys):
            files_plan = meta.get("files_plan") or []
            for fp in files_plan:
                p = Path(fp["source"])
                if p.exists() and "stat" in fp:
                    st = p.stat()
                    exp_st = fp["stat"]
                    if st.st_size != exp_st["size"] or st.st_mtime_ns != exp_st["mtime_ns"]:
                        return _fail(f"Source file stat changed: {p}", "import_folder_toctou_mismatch")

            store.update(operation_id, status="Running", metadata={**meta, "mutation_started": True})

            # SEC-002 Wave 22 final review, finding #3 (CRITICAL): this
            # previously marked filesystem_mutated=True, db_mutated=True,
            # and status=Completed unconditionally -- including when no
            # `beets_import_runner` was supplied at all, meaning nothing
            # happened. The production Control Agent route did not pass a
            # runner, so every real import through this family was a
            # no-op silently reported as a success. Mutation flags and
            # Completed status now require actual evidence: a runner was
            # supplied AND it reported success.
            if not beets_import_runner:
                return _fail(
                    "No Beets import mechanism is configured for this engine; the import was not performed.",
                    "import_folder_no_runner_configured",
                )
            try:
                import_result = beets_import_runner(meta.get("payload") or {})
            except Exception as e:
                return _fail(f"Beets engine import failed: {e}", "import_folder_beets_failed")

            if not isinstance(import_result, dict) or not import_result.get("ok"):
                err = (import_result or {}).get("error") if isinstance(import_result, dict) else None
                return _fail(f"Beets import did not succeed: {err or import_result}", "import_folder_beets_failed")

            store.update(operation_id, status="Completed", metadata={
                **store.get(operation_id).get("metadata", {}),
                "filesystem_mutated": True,
                "db_mutated": True,
                "mutated": True,
                "import_result": import_result,
                "completed_at": _now(),
            })

            return {"ok": True, "operation_id": operation_id, "status": "Completed", "mutated": True, "import_result": import_result}


def rollback_import_folder(
    store: TransactionStore,
    operation_id: str,
    *,
    music_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Roll back an import_folder_v1 transaction."""
    if not _TRANSACTION_ID_RE.match(operation_id):
        return {"ok": False, "error": "Invalid transaction ID format", "code": "import_folder_invalid_id"}

    with _get_apply_lock(operation_id):
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": f"Transaction {operation_id} not found", "code": "import_folder_not_found"}

        meta = tx.get("metadata") or {}
        if meta.get("mutation_family") != "import_folder_v1":
            return {"ok": False, "error": "Transaction is not an import_folder_v1 operation", "code": "import_folder_family_mismatch"}

        if tx.get("status") in ("Rolled Back", "Recovery Required"):
            return {"ok": False, "error": f"Transaction is already {tx.get('status')}.", "code": "import_folder_already_rolled_back"}

        if not meta.get("filesystem_mutated") and not meta.get("db_mutated"):
            return {"ok": False, "error": "No mutations were performed to roll back.", "code": "import_folder_no_mutations"}

        resource_keys = meta.get("resource_keys") or []
        with _lock_resources(resource_keys):
            # SEC-002 Wave 22 final review, finding #7/#33 (merge
            # blocker): a real Beets import cannot be blindly undone by
            # this function -- it has no record of which library rows or
            # files the import actually produced (that is exactly the gap
            # documented in finding #6, not yet closed). Relabeling a
            # completed import as "Rolled Back" without undoing anything
            # is a truthfulness defect in its own right, independent of
            # whether real rollback is ever implemented. Report the
            # honest state instead of a false success.
            store.update(operation_id, status="Recovery Required", metadata={
                **meta,
                "rollback_available": False,
                "recovery_required_at": _now(),
            })

            return {
                "ok": False,
                "operation_id": operation_id,
                "status": "Recovery Required",
                "error": "This import cannot be automatically rolled back. Manual review of the resulting library state is required.",
                "code": "import_folder_rollback_unavailable",
            }


_LIBRARY_CLEANUP_AUDIO_EXTS = {
    ".aac", ".aiff", ".alac", ".ape", ".dsf", ".flac", ".m4a", ".mp3",
    ".mp4", ".ogg", ".opus", ".wav", ".wma", ".wv",
}
_ARCH003_BYTES_KEY = "__arch003_bytes_b64__"


def _cleanup_unique_strings(values: Any) -> List[str]:
    seen: set = set()
    out: List[str] = []
    if isinstance(values, (str, bytes)):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return out
    for raw in values:
        value = _s(raw).strip()
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


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


def _cleanup_stat_record(path: Path, root: Path) -> Dict[str, Any]:
    if not _path_under(path, root) or _path_has_symlink_under(path, root):
        raise ValueError("cleanup path outside validated root")
    state = _capture_media_tag_state(path, [])
    if not state.get("exists"):
        raise ValueError("cleanup file missing")
    stat_record = dict(state.get("stat") or {})
    if "inode" in stat_record and "ino" not in stat_record:
        stat_record["ino"] = stat_record["inode"]
    stat_record["type"] = "file"
    return stat_record


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


def _cleanup_json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {_ARCH003_BYTES_KEY: base64.b64encode(value).decode("ascii")}
    return value


def _cleanup_sql_value(value: Any) -> Any:
    if isinstance(value, dict) and set(value.keys()) == {_ARCH003_BYTES_KEY}:
        return base64.b64decode(str(value[_ARCH003_BYTES_KEY]).encode("ascii"))
    return value


def _cleanup_row_to_json(row: sqlite3.Row) -> Dict[str, Any]:
    return {key: _cleanup_json_value(row[key]) for key in row.keys()}


def _cleanup_db_path_string(raw: Any) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    if isinstance(raw, dict) and _ARCH003_BYTES_KEY in raw:
        try:
            return base64.b64decode(str(raw[_ARCH003_BYTES_KEY]).encode("ascii")).decode("utf-8", "replace")
        except Exception:
            return ""
    return str(raw or "")


def _cleanup_db_path_candidates(path: Path, music_roots: List[Path]) -> List[Any]:
    candidates: List[Any] = []
    seen: set = set()

    def add(value: str) -> None:
        if not value:
            return
        for candidate in (value, value.replace("\\", "/")):
            if not candidate:
                continue
            for item in (candidate, candidate.encode("utf-8", "surrogateescape")):
                key = (type(item).__name__, item)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(item)

    add(str(path))
    add(str(_cleanup_resolve_path(path)))
    for root in music_roots:
        try:
            rel = _cleanup_resolve_path(path).relative_to(root)
        except Exception:
            continue
        add(str(rel))
        add(rel.as_posix())
    return candidates


def _cleanup_resolved_paths_equal(left: Path, right: Path) -> bool:
    try:
        return _cleanup_resolve_path(left) == _cleanup_resolve_path(right)
    except Exception:
        return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(os.path.normpath(str(right)))


def _library_cleanup_item_rows_for_path(lib_db: str, path: Path, music_roots: List[Path]) -> List[Dict[str, Any]]:
    if not lib_db or not Path(lib_db).exists():
        return []
    candidates = _cleanup_db_path_candidates(path, music_roots)
    if not candidates:
        return []
    rows_by_id: Dict[int, Dict[str, Any]] = {}
    q_marks = ",".join("?" for _ in candidates)
    con = sqlite3.connect(lib_db, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(f"SELECT * FROM items WHERE path IN ({q_marks})", candidates).fetchall()
        for row in rows:
            raw_path = _cleanup_db_path_string(row["path"])
            resolved = _resolve_db_path(raw_path, [str(root) for root in music_roots])
            if _cleanup_resolved_paths_equal(resolved, path):
                rows_by_id[int(row["id"])] = _cleanup_row_to_json(row)
    finally:
        con.close()
    return [rows_by_id[k] for k in sorted(rows_by_id)]


def _library_cleanup_album_art_refs_for_path(lib_db: str, path: Path, music_roots: List[Path]) -> List[Dict[str, Any]]:
    if not lib_db or not Path(lib_db).exists():
        return []
    candidates = _cleanup_db_path_candidates(path, music_roots)
    refs: List[Dict[str, Any]] = []
    q_marks = ",".join("?" for _ in candidates)
    con = sqlite3.connect(lib_db, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        try:
            rows = con.execute(f"SELECT * FROM albums WHERE artpath IN ({q_marks})", candidates).fetchall()
        except sqlite3.Error:
            rows = []
        for row in rows:
            raw_path = _cleanup_db_path_string(row["artpath"])
            if raw_path and _cleanup_resolved_paths_equal(_resolve_db_path(raw_path, [str(root) for root in music_roots]), path):
                refs.append(_cleanup_row_to_json(row))
    finally:
        con.close()
    return refs


def _library_cleanup_db_refs_beneath_folder(lib_db: str, folder: Path, music_roots: List[Path]) -> List[Dict[str, Any]]:
    if not lib_db or not Path(lib_db).exists():
        return []
    refs: List[Dict[str, Any]] = []
    con = sqlite3.connect(lib_db, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        try:
            item_rows = con.execute("SELECT id, path FROM items").fetchall()
        except sqlite3.Error:
            item_rows = []
        for row in item_rows:
            raw_path = _cleanup_db_path_string(row["path"])
            if not raw_path:
                continue
            item_path = _resolve_db_path(raw_path, [str(root) for root in music_roots])
            if _path_under(item_path, folder):
                refs.append({"table": "items", "id": int(row["id"]), "path": raw_path})
        try:
            album_rows = con.execute("SELECT id, artpath FROM albums WHERE artpath IS NOT NULL AND artpath != ''").fetchall()
        except sqlite3.Error:
            album_rows = []
        for row in album_rows:
            raw_path = _cleanup_db_path_string(row["artpath"])
            if not raw_path:
                continue
            art_path = _resolve_db_path(raw_path, [str(root) for root in music_roots])
            if _path_under(art_path, folder):
                refs.append({"table": "albums", "id": int(row["id"]), "path": raw_path})
    finally:
        con.close()
    return refs


def _library_cleanup_safe_leaf(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).name).strip("._")
    return cleaned[:120] or "file"


def create_library_cleanup_plan(
    store: TransactionStore,
    payload: Dict[str, Any],
    *,
    music_allowed_roots: Optional[List[str]] = None,
    staging_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
    quarantine_base_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a non-mutating preview for path-driven duplicate cleanup.

    This family is intentionally narrow: it only supports duplicate-cleanup
    requests from the existing Web Manager UI. Apply quarantines files before
    deleting Beets item rows, and rollback restores files before DB rows.
    """
    action = str(payload.get("action") or "dedup_cleanup").strip()
    if action not in {"dedup_cleanup", "duplicate_file_cleanup"}:
        return {"ok": False, "error": "unsupported library cleanup action", "code": "library_cleanup_invalid_action"}

    raw_paths = _cleanup_unique_strings(payload.get("paths") or payload.get("files") or [])
    if not raw_paths:
        return {"ok": False, "error": "paths required", "code": "library_cleanup_invalid_payload"}

    music_roots = _cleanup_normalize_roots(music_allowed_roots, [str(os.environ.get("MUSIC_ROOT", "/music"))])
    staging_roots = _cleanup_normalize_roots(staging_allowed_roots, [
        str(os.environ.get("DOWNLOADS_ROOT", "/downloads")),
        str(os.environ.get("STAGING_ROOT", "/staging")),
    ])
    cleanup_roots = music_roots + [root for root in staging_roots if root not in music_roots]
    lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")

    results: List[Dict[str, Any]] = []
    quarantines: List[Dict[str, Any]] = []
    db_item_deletes: List[Dict[str, Any]] = []
    captured_item_rows: List[Dict[str, Any]] = []
    parent_folders: List[str] = []
    affected_item_ids: List[int] = []

    for raw in raw_paths:
        display_path = _cleanup_resolve_path(Path(raw))
        rec: Dict[str, Any] = {
            "path": raw,
            "resolved_path": str(display_path),
            "resource": str(display_path),
            "reason": "duplicate cleanup requested by Web Manager",
            "expected_result": "file quarantined before any Beets item row is retired",
            "rollback": "quarantined file is restored before captured DB rows are restored",
            "ok": False,
        }
        if not _normpath_within_roots(raw, cleanup_roots):
            rec.update(error="Path is outside server-owned cleanup roots", code="library_cleanup_path_out_of_root")
            results.append(rec)
            continue
        p = _cleanup_validate_path_under_roots(raw, cleanup_roots)
        if p is None:
            rec.update(error="Symlink source or parent rejected", code="library_cleanup_symlink_rejected")
            results.append(rec)
            continue
        root = _cleanup_root_for_path(p, cleanup_roots)
        if root is None:
            rec.update(error="Path is outside server-owned cleanup roots", code="library_cleanup_path_out_of_root")
            results.append(rec)
            continue
        root_type = "music" if root in music_roots else "staging"
        safe_path_text = os.path.abspath(os.path.normpath(str(p)))
        root_text = os.path.abspath(os.path.normpath(str(root)))
        if safe_path_text != root_text and not safe_path_text.startswith(root_text + os.sep):
            rec.update(error="Path is outside server-owned cleanup roots", code="library_cleanup_path_out_of_root")
            results.append(rec)
            continue
        p = Path(safe_path_text)
        path_check = _bulk_replacement_toctou_check(safe_path_text, None, [root])
        if path_check:
            if "symlink" in path_check:
                rec.update(error="Symlink source or parent rejected", code="library_cleanup_symlink_rejected")
            elif "root escape" in path_check or "unresolvable" in path_check:
                rec.update(error="Path is outside server-owned cleanup roots", code="library_cleanup_path_out_of_root")
            elif path_check == "file missing":
                rec.update(error="File not found", code="library_cleanup_file_missing")
            else:
                rec.update(error=f"File check failed: {path_check}", code="library_cleanup_not_file")
            results.append(rec)
            continue
        if p.suffix.lower() not in _LIBRARY_CLEANUP_AUDIO_EXTS:
            rec.update(error="Not an audio file", code="library_cleanup_not_audio")
            results.append(rec)
            continue

        item_rows = _library_cleanup_item_rows_for_path(lib_db, p, music_roots) if lib_db else []
        art_refs = _library_cleanup_album_art_refs_for_path(lib_db, p, music_roots) if lib_db else []
        if art_refs:
            rec.update(error="File is still referenced by albums.artpath", code="library_cleanup_artpath_referenced", album_ids=[int(r["id"]) for r in art_refs])
            results.append(rec)
            continue
        if root_type == "music":
            if not lib_db or not Path(lib_db).exists():
                rec.update(error="Beets library database is required for music-root cleanup", code="library_cleanup_db_not_found")
                results.append(rec)
                continue
            if len(item_rows) != 1:
                rec.update(error="Music-root cleanup requires exactly one matching Beets item row", code="library_cleanup_db_reference_mismatch", item_ids=[int(r["id"]) for r in item_rows])
                results.append(rec)
                continue
        elif item_rows:
            rec.update(error="Staging cleanup path is referenced by Beets items", code="library_cleanup_staging_db_referenced", item_ids=[int(r["id"]) for r in item_rows])
            results.append(rec)
            continue

        stat_record = _cleanup_stat_record(p, root)
        item_id = int(item_rows[0]["id"]) if item_rows else None
        quarantine = {
            "source": str(p),
            "root": str(root),
            "root_type": root_type,
            "stat": stat_record,
            "item_id": item_id,
            "db_item_ids": [int(r["id"]) for r in item_rows],
        }
        quarantines.append(quarantine)
        captured_item_rows.extend(item_rows)
        if item_id is not None:
            affected_item_ids.append(item_id)
            db_item_deletes.append({"id": item_id, "path": _cleanup_db_path_string(item_rows[0].get("path"))})
        parent_folders.append(str(p.parent))
        rec.update(
            ok=True,
            would_quarantine=True,
            irreversible=False,
            root_type=root_type,
            stat=stat_record,
            db_item_ids=[int(r["id"]) for r in item_rows],
            db_rows=len(item_rows),
        )
        results.append(rec)

    resource_keys = [f"file:{hashlib.sha256(q['source'].encode('utf-8', 'surrogateescape')).hexdigest()}" for q in quarantines]
    resource_keys.extend(f"item:{iid}" for iid in sorted(set(affected_item_ids)))
    tx = store.create(
        operation_type="Library Cleanup",
        status="Preview",
        summary=f"Duplicate cleanup: {len(quarantines)} file(s) planned",
        rollback_available=bool(quarantines),
        rollback_reason="Apply quarantines files before DB mutation; rollback restores files first, then DB rows.",
        metadata={
            "mutation_family": "library_cleanup_v1",
            "action": action,
            "results": results,
            "quarantines": quarantines,
            "db_item_deletes": db_item_deletes,
            "captured_item_rows": captured_item_rows,
            "candidate_parent_folders": sorted(set(parent_folders)),
            "resource_keys": sorted(set(resource_keys)),
            "music_allowed_roots": [str(root) for root in music_roots],
            "staging_allowed_roots": [str(root) for root in staging_roots],
            "quarantine_base_root": quarantine_base_root or os.environ.get("RECONCILE_QUARANTINE_DIR", "/config/reconcile_quarantine"),
            "created_at": _now(),
        },
    )
    return {
        "ok": True,
        "operation_id": tx["id"],
        "action": action,
        "results": results,
        "planned_count": len(quarantines),
        "skipped_count": sum(1 for r in results if not r.get("ok")),
        "quarantine_count": len(quarantines),
        "db_rows_count": len(db_item_deletes),
        "candidate_parent_folders": sorted(set(parent_folders)),
    }


def execute_library_cleanup_apply(
    store: TransactionStore,
    operation_id: str,
    *,
    music_allowed_roots: Optional[List[str]] = None,
    staging_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
    quarantine_base_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply a library_cleanup_v1 duplicate cleanup transaction."""
    if not _TRANSACTION_ID_RE.match(operation_id):
        return {"ok": False, "error": "Invalid transaction ID format", "code": "library_cleanup_invalid_id"}

    with _get_apply_lock(operation_id):
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": f"Transaction {operation_id} not found", "code": "library_cleanup_not_found"}

        meta = tx.get("metadata") or {}
        if meta.get("mutation_family") != "library_cleanup_v1":
            return {"ok": False, "error": "Transaction is not a library_cleanup_v1 operation", "code": "library_cleanup_family_mismatch"}
        if tx.get("status") == "Completed":
            return {"ok": True, "operation_id": operation_id, "status": "Completed", "mutated": True, "idempotent": True}

        music_roots = _cleanup_normalize_roots(music_allowed_roots or meta.get("music_allowed_roots"), [str(os.environ.get("MUSIC_ROOT", "/music"))])
        staging_roots = _cleanup_normalize_roots(staging_allowed_roots or meta.get("staging_allowed_roots"), [str(os.environ.get("DOWNLOADS_ROOT", "/downloads")), str(os.environ.get("STAGING_ROOT", "/staging"))])
        cleanup_roots = music_roots + [root for root in staging_roots if root not in music_roots]
        lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")
        resource_keys = meta.get("resource_keys") or []

        def _fail(msg: str, code: str) -> Dict[str, Any]:
            curr = store.get(operation_id)
            c_meta = curr.get("metadata") or {}
            mutated = bool(c_meta.get("filesystem_mutated") or c_meta.get("db_mutated"))
            store.update(operation_id, status="Failed", logs=[f"Apply failed: {msg}"], rollback={"available": mutated, "reason": "Recovery requires rollback of the quarantined file and DB row." if mutated else "No mutation was performed."})
            return {"ok": False, "error": msg, "code": code, "mutated": mutated, "partial_mutation": mutated, "rollback_available": mutated}

        with _lock_resources(resource_keys):
            quarantines = meta.get("quarantines") or []
            db_item_deletes = meta.get("db_item_deletes") or []
            validated_quarantines: List[Dict[str, Any]] = []
            for q in quarantines:
                source_text = str(q.get("source") or "")
                if not source_text or not _normpath_within_roots(source_text, cleanup_roots):
                    return _fail(f"Source outside allowed roots: {source_text}", "library_cleanup_path_out_of_root")
                sp = _cleanup_validate_path_under_roots(source_text, cleanup_roots)
                if sp is None:
                    return _fail(f"Symlink detected on source: {source_text}", "library_cleanup_symlink_rejected")
                root = _cleanup_root_for_path(sp, cleanup_roots)
                expected_root = _cleanup_resolve_path(Path(q.get("root") or root or "."))
                if root is None or not _path_under(sp, expected_root):
                    return _fail(f"Source outside allowed roots: {sp}", "library_cleanup_path_out_of_root")
                safe_path_text = os.path.abspath(os.path.normpath(str(sp)))
                root_text = os.path.abspath(os.path.normpath(str(root)))
                if safe_path_text != root_text and not safe_path_text.startswith(root_text + os.sep):
                    return _fail(f"Source outside allowed roots: {sp}", "library_cleanup_path_out_of_root")
                sp = Path(safe_path_text)
                expected_stat = dict(q.get("stat") or {})
                if "ino" in expected_stat and "inode" not in expected_stat:
                    expected_stat["inode"] = expected_stat["ino"]
                path_check = _bulk_replacement_toctou_check(safe_path_text, expected_stat, [root])
                if path_check:
                    if "symlink" in path_check:
                        return _fail(f"Symlink detected on source: {sp}", "library_cleanup_symlink_rejected")
                    if "root escape" in path_check or "unresolvable" in path_check:
                        return _fail(f"Source outside allowed roots: {sp}", "library_cleanup_path_out_of_root")
                    if path_check == "file missing":
                        return _fail(f"Source file missing before apply: {sp}", "library_cleanup_file_missing")
                    return _fail(f"Source file changed since plan: {sp}", "library_cleanup_toctou_mismatch")
                if sp.suffix.lower() not in _LIBRARY_CLEANUP_AUDIO_EXTS:
                    return _fail(f"Source is not an audio file: {sp}", "library_cleanup_not_audio")
                # Round-99-reconciliation (final review, section 12/20):
                # these two helpers open their own sqlite3 connection and
                # can raise sqlite3.Error (a broken/locked/schema-changed
                # DB) -- caught here, before any file has been touched, so
                # it fails closed exactly like every other pre-mutation
                # rejection in this loop rather than crashing out with an
                # unhandled exception.
                try:
                    item_rows = _library_cleanup_item_rows_for_path(lib_db, sp, music_roots) if lib_db else []
                    art_refs = _library_cleanup_album_art_refs_for_path(lib_db, sp, music_roots) if lib_db else []
                except sqlite3.Error as ex:
                    return _fail(f"Database error while revalidating {sp}: {ex}", "library_cleanup_db_error")
                expected_ids = sorted(int(i) for i in (q.get("db_item_ids") or []))
                if sorted(int(r["id"]) for r in item_rows) != expected_ids:
                    return _fail(f"Beets item references changed since plan for {sp}", "library_cleanup_db_reference_changed")
                if art_refs:
                    return _fail(f"File became referenced by albums.artpath: {sp}", "library_cleanup_artpath_referenced")
                validated_quarantines.append({"plan": q, "source": sp, "root": root})

            store.update(operation_id, status="Running", metadata={**meta, "mutation_started": True})
            q_base = _cleanup_resolve_path(Path(quarantine_base_root or meta.get("quarantine_base_root") or os.environ.get("RECONCILE_QUARANTINE_DIR", "/config/reconcile_quarantine")))
            q_base_text = os.path.abspath(os.path.normpath(str(q_base)))
            q_dir_text = q_base_text
            if q_dir_text != q_base_text and not q_dir_text.startswith(q_base_text + os.sep):
                return _fail("Quarantine root rejected", "library_cleanup_quarantine_path_rejected")
            if _path_has_symlink_under(q_base, q_base):
                return _fail("Quarantine root rejected", "library_cleanup_quarantine_path_rejected")
            quarantined_records: List[Dict[str, Any]] = []
            for idx, checked in enumerate(validated_quarantines):
                q = checked["plan"]
                sp = checked["source"]
                digest = hashlib.sha256(str(sp).encode("utf-8", "surrogateescape")).hexdigest()[:16]
                q_target_text = os.path.abspath(os.path.normpath(str(Path(q_dir_text) / f"{operation_id}_{idx:04d}_{digest}_{_library_cleanup_safe_leaf(sp.name)}")))
                if q_target_text == q_base_text or not q_target_text.startswith(q_base_text + os.sep):
                    return _fail(f"Quarantine path rejected for {sp}", "library_cleanup_quarantine_path_rejected")
                q_target = Path(q_target_text)
                if _path_has_symlink_under(q_target.parent, q_base):
                    return _fail(f"Quarantine path rejected for {sp}", "library_cleanup_quarantine_path_rejected")
                try:
                    _safe_rename(sp, q_target)
                except OSError as ex:
                    return _fail(f"Could not quarantine file {sp}: {type(ex).__name__}", "library_cleanup_quarantine_failed")
                quarantined_records.append({
                    "source": str(sp),
                    "quarantined": str(q_target),
                    "item_id": q.get("item_id"),
                    "root_type": q.get("root_type"),
                })
                store.update(operation_id, metadata={
                    **store.get(operation_id).get("metadata", {}),
                    "filesystem_mutated": True,
                    "mutated": True,
                    "quarantined_records": quarantined_records,
                })

            # Round-99-reconciliation (final review, section 9/12 of the
            # merge brief): verify every quarantine actually landed BEFORE
            # ever touching the DB, not after. The rename call above not
            # raising is already strong evidence, but the DB delete is the
            # harder-to-undo half of this transaction (a rollback restores
            # a quarantined file trivially; it cannot un-delete a
            # committed row without the captured row data) -- so nothing
            # here may commit any DB mutation until the exact required
            # sequence's own verify-quarantine step has independently
            # confirmed source-gone/target-present for every file. This
            # also holds during the file loop above (each `_safe_rename`
            # is followed here, in file order, before any file's DB row
            # is even considered), not only as one bulk pass afterward.
            for qr in quarantined_records:
                source_text = os.path.abspath(os.path.normpath(str(qr.get("source") or "")))
                quarantine_text = os.path.abspath(os.path.normpath(str(qr.get("quarantined") or "")))
                source_root = _cleanup_root_for_path(Path(source_text), cleanup_roots)
                if source_root is None:
                    return _fail("Post-quarantine verification failed for quarantined file", "library_cleanup_verification_failed")
                source_root_text = os.path.abspath(os.path.normpath(str(source_root)))
                if source_text != source_root_text and not source_text.startswith(source_root_text + os.sep):
                    return _fail("Post-quarantine verification failed for quarantined file", "library_cleanup_verification_failed")
                if quarantine_text == q_base_text or not quarantine_text.startswith(q_base_text + os.sep):
                    return _fail("Post-quarantine verification failed for quarantined file", "library_cleanup_verification_failed")
                if os.path.exists(source_text) or not os.path.exists(quarantine_text):
                    return _fail("Post-quarantine verification failed for quarantined file", "library_cleanup_verification_failed")

            deleted_items = 0
            db_mutated = False
            if db_item_deletes:
                if not lib_db or not Path(lib_db).exists():
                    return _fail("Beets library database not found before DB retirement", "library_cleanup_db_not_found")
                con = sqlite3.connect(lib_db, timeout=10)
                try:
                    try:
                        cur = con.cursor()
                        for spec in db_item_deletes:
                            iid = int(spec["id"])
                            cur.execute("DELETE FROM items WHERE id=?", (iid,))
                            if cur.rowcount != 1:
                                con.rollback()
                                return _fail(f"Expected to delete exactly 1 item row for id={iid}, affected {cur.rowcount}.", "library_cleanup_rowcount_mismatch")
                            deleted_items += 1
                        con.commit()
                        db_mutated = True
                    except sqlite3.Error as ex:
                        # Round-99-reconciliation (final review, section 12):
                        # this used to propagate as an unhandled exception,
                        # leaving the transaction stuck at "Running" forever
                        # (never Failed, never Completed) even though the
                        # file(s) were already safely quarantined and
                        # rollback_library_cleanup() remained callable. A
                        # controlled family must never let its own DB layer
                        # crash out of Apply uncaught -- report it the same
                        # way the rowcount-mismatch case above already does:
                        # a truthful Failed with rollback_available=True,
                        # never a silent hang or a false Completed.
                        try:
                            con.rollback()
                        except sqlite3.Error:
                            pass
                        return _fail(f"Database error while retiring duplicate item row(s): {ex}", "library_cleanup_db_error")
                finally:
                    con.close()
                store.update(operation_id, metadata={**store.get(operation_id).get("metadata", {}), "db_mutated": True, "deleted_items_count": deleted_items})

            if db_item_deletes and lib_db and Path(lib_db).exists():
                con = sqlite3.connect(lib_db, timeout=10)
                try:
                    try:
                        for spec in db_item_deletes:
                            if con.execute("SELECT id FROM items WHERE id=?", (int(spec["id"]),)).fetchone() is not None:
                                return _fail(f"Post-apply verification failed: item {spec['id']} still present", "library_cleanup_verification_failed")
                    except sqlite3.Error as ex:
                        return _fail(f"Database error during post-apply verification: {ex}", "library_cleanup_db_error")
                finally:
                    con.close()

            store.update(operation_id, status="Completed", rollback={"available": True, "reason": "Files were quarantined and captured DB rows can be restored."}, metadata={
                **store.get(operation_id).get("metadata", {}),
                "db_mutated": db_mutated,
                "deleted_items_count": deleted_items,
                "completed_at": _now(),
            })
            return {
                "ok": True,
                "operation_id": operation_id,
                "status": "Completed",
                "mutated": bool(quarantined_records or db_mutated),
                "quarantined_count": len(quarantined_records),
                "deleted_items": deleted_items,
                "candidate_parent_folders": meta.get("candidate_parent_folders") or [],
            }


def rollback_library_cleanup(
    store: TransactionStore,
    operation_id: str,
    *,
    music_allowed_roots: Optional[List[str]] = None,
    staging_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Roll back a library_cleanup_v1 transaction: files first, then DB rows."""
    if not _TRANSACTION_ID_RE.match(operation_id):
        return {"ok": False, "error": "Invalid transaction ID format", "code": "library_cleanup_invalid_id"}

    with _get_apply_lock(operation_id):
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": f"Transaction {operation_id} not found", "code": "library_cleanup_not_found"}

        meta = tx.get("metadata") or {}
        if meta.get("mutation_family") != "library_cleanup_v1":
            return {"ok": False, "error": "Transaction is not a library_cleanup_v1 operation", "code": "library_cleanup_family_mismatch"}
        if tx.get("status") == "Rolled Back":
            return {"ok": False, "error": "Transaction is already rolled back.", "code": "library_cleanup_already_rolled_back"}
        if not meta.get("filesystem_mutated") and not meta.get("db_mutated"):
            return {"ok": False, "error": "No mutations were performed to roll back.", "code": "library_cleanup_no_mutations"}

        music_roots = _cleanup_normalize_roots(music_allowed_roots or meta.get("music_allowed_roots"), [str(os.environ.get("MUSIC_ROOT", "/music"))])
        staging_roots = _cleanup_normalize_roots(staging_allowed_roots or meta.get("staging_allowed_roots"), [str(os.environ.get("DOWNLOADS_ROOT", "/downloads")), str(os.environ.get("STAGING_ROOT", "/staging"))])
        cleanup_roots = music_roots + [root for root in staging_roots if root not in music_roots]
        lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")
        resource_keys = meta.get("resource_keys") or []

        with _lock_resources(resource_keys):
            files_restored = 0
            files_failed = 0
            restored_item_ids: set = set()
            for qr in meta.get("quarantined_records") or []:
                qp = Path(qr["quarantined"])
                sp = Path(qr["source"])
                root = _cleanup_root_for_path(sp, cleanup_roots)
                if root is None or _path_has_symlink_under(sp.parent, root) or sp.exists() or sp.is_symlink():
                    files_failed += 1
                    continue
                if not qp.exists() or qp.is_symlink():
                    files_failed += 1
                    continue
                try:
                    sp.parent.mkdir(parents=True, exist_ok=True)
                    if _path_has_symlink_under(sp.parent, root):
                        files_failed += 1
                        continue
                    _safe_rename(qp, sp)
                    if not sp.exists() or sp.is_symlink():
                        files_failed += 1
                        continue
                    files_restored += 1
                    if qr.get("item_id") is not None:
                        restored_item_ids.add(int(qr["item_id"]))
                except OSError:
                    files_failed += 1

            db_restored = 0
            db_failed = 0
            captured_rows = meta.get("captured_item_rows") or []
            if captured_rows:
                if not lib_db or not Path(lib_db).exists():
                    db_failed += len(captured_rows)
                else:
                    con = sqlite3.connect(lib_db, timeout=10)
                    try:
                        cur = con.cursor()
                        for row in captured_rows:
                            iid = int(row["id"])
                            if iid not in restored_item_ids:
                                db_failed += 1
                                continue
                            if cur.execute("SELECT id FROM items WHERE id=?", (iid,)).fetchone() is not None:
                                db_failed += 1
                                continue
                            cols = list(row.keys())
                            placeholders = ",".join("?" for _ in cols)
                            cur.execute(f"INSERT INTO items ({','.join(cols)}) VALUES ({placeholders})", [_cleanup_sql_value(row[c]) for c in cols])
                            if cur.rowcount == 1:
                                db_restored += 1
                            else:
                                db_failed += 1
                        con.commit()
                    finally:
                        con.close()

            if files_failed == 0 and db_failed == 0:
                final_status = "Rolled Back"
                ok = True
            elif files_restored > 0 or db_restored > 0:
                final_status = "Partially Rolled Back"
                ok = False
            else:
                final_status = "Failed"
                ok = False
            store.update(operation_id, status=final_status, rollback={"available": final_status != "Rolled Back", "reason": "Rollback incomplete; manual recovery required." if final_status != "Rolled Back" else ""}, metadata={
                **meta,
                "files_restored_count": files_restored,
                "files_failed_count": files_failed,
                "db_restored_count": db_restored,
                "db_failed_count": db_failed,
                "rolled_back_at": _now(),
            })
            return {
                "ok": ok,
                "operation_id": operation_id,
                "status": final_status,
                "files_restored": files_restored,
                "files_failed": files_failed,
                "db_restored": db_restored,
                "db_failed": db_failed,
                "partial_mutation": final_status == "Partially Rolled Back",
            }
# ── folder_cleanup_v1 ────────────────────────────────────────────────────────

def create_folder_cleanup_plan(
    store: TransactionStore,
    payload: Dict[str, Any],
    *,
    music_allowed_roots: Optional[List[str]] = None,
    staging_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
    quarantine_base_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a non-mutating preview plan for folder/placeholder cleanup actions."""
    action = str(payload.get("action") or payload.get("mode") or "remove_empty").strip()
    src_folder = str(payload.get("source") or payload.get("source_folder") or "").strip()
    target_folder = str(payload.get("target") or payload.get("target_folder") or "").strip()

    if not src_folder:
        return {"ok": False, "error": "source folder required", "code": "folder_cleanup_invalid_payload"}

    allowed_roots = _cleanup_normalize_roots(music_allowed_roots, [str(os.environ.get("MUSIC_ROOT", "/music"))])
    src_display = _cleanup_resolve_path(Path(src_folder))
    if not _normpath_within_roots(src_folder, allowed_roots):
        return {"ok": False, "error": f"Folder outside allowed root: {src_display}", "code": "folder_cleanup_path_out_of_root"}
    src_p = _cleanup_validate_path_under_roots(src_folder, allowed_roots)
    if src_p is None:
        return {"ok": False, "error": f"Symlink rejected: {src_display}", "code": "folder_cleanup_symlink_rejected"}
    root = _cleanup_root_for_path(src_p, allowed_roots)
    if root is None:
        return {"ok": False, "error": f"Folder outside allowed root: {src_display}", "code": "folder_cleanup_path_out_of_root"}

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
            refs = _library_cleanup_db_refs_beneath_folder(db_path or "", src_p, allowed_roots)
            if refs:
                return {"ok": False, "error": "Directory still has Beets DB references", "code": "folder_cleanup_db_references", "references": refs[:20]}
            dir_removals.append({"path": str(src_p), "expected_empty": True})
    elif action in ("safe_rename", "rename_folder") and target_folder:
        tgt_p = _cleanup_resolve_path(Path(target_folder))
        tgt_root = _cleanup_root_for_path(tgt_p, allowed_roots)
        if tgt_root is not None:
            dir_renames.append({"source": str(src_p), "target": str(tgt_p)})
    elif action in ("merge_source_files", "merge") and target_folder:
        tgt_p = _cleanup_resolve_path(Path(target_folder))
        tgt_root = _cleanup_root_for_path(tgt_p, allowed_roots)
        if tgt_root is not None:
            if src_p.exists() and src_p.is_dir():
                for f in src_p.rglob("*"):
                    if f.is_file():
                        rel = f.relative_to(src_p)
                        dest_f = tgt_p / rel
                        st = f.stat()
                        file_moves.append({
                            "source": str(f),
                            "target": str(dest_f),
                            "stat": {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns},
                        })
                dir_removals.append(str(src_p))

    resource_keys = [f"folder:{hashlib.sha256(str(src_p).encode('utf-8', 'surrogateescape')).hexdigest()}"]

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

def execute_folder_cleanup_apply(
    store: TransactionStore,
    operation_id: str,
    *,
    music_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute folder cleanup apply with durable steps and resource locking."""
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

        if tx.get("status") == "Completed":
            return {"ok": True, "operation_id": operation_id, "status": "Completed", "mutated": True, "idempotent": True}

        resource_keys = meta.get("resource_keys") or []
        allowed_roots = _cleanup_normalize_roots(music_allowed_roots or meta.get("allowed_roots"), [str(os.environ.get("MUSIC_ROOT", "/music"))])
        lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")

        def _fail(msg: str, code: str) -> Dict[str, Any]:
            curr = store.get(operation_id)
            c_meta = curr.get("metadata") or {}
            mutated = bool(c_meta.get("filesystem_mutated"))
            store.update(operation_id, status="Failed", logs=[f"Apply failed: {msg}"], rollback={"available": mutated, "reason": "Rollback can restore recorded moves/directories." if mutated else "No mutation was performed."})
            return {"ok": False, "error": msg, "code": code, "mutated": mutated, "rollback_available": mutated}

        with _lock_resources(resource_keys):
            file_moves = meta.get("file_moves") or []
            for fm in file_moves:
                sp = Path(fm["source"])
                root = _cleanup_root_for_path(sp, allowed_roots)
                if root is None:
                    return _fail(f"Source outside allowed roots: {sp}", "folder_cleanup_path_out_of_root")
                if _path_has_symlink_under(sp, root):
                    return _fail(f"Symlink detected on source: {sp}", "folder_cleanup_symlink_rejected")
                if sp.exists() and "stat" in fm:
                    if not _cleanup_stat_matches(sp, fm["stat"], root):
                        return _fail(f"Source file stat changed: {sp}", "folder_cleanup_toctou_mismatch")

            store.update(operation_id, status="Running", metadata={**meta, "mutation_started": True})

            moved_records = []
            for fm in file_moves:
                sp = Path(fm["source"])
                tp = Path(fm["target"])
                if sp.exists():
                    tp_root = _cleanup_root_for_path(tp, allowed_roots)
                    if tp_root is None:
                        return _fail(f"Target outside allowed roots: {tp}", "folder_cleanup_path_out_of_root")
                    tp.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        _safe_rename(sp, tp)
                        moved_records.append({"source": str(sp), "target": str(tp)})
                    except Exception as e:
                        return _fail(f"Move failed {sp} -> {tp}: {e}", "folder_cleanup_move_failed")

            dir_renames = meta.get("dir_renames") or []
            for dr in dir_renames:
                sp = Path(dr["source"])
                tp = Path(dr["target"])
                if sp.exists():
                    tp_root = _cleanup_root_for_path(tp, allowed_roots)
                    if tp_root is None:
                        return _fail(f"Target outside allowed roots: {tp}", "folder_cleanup_path_out_of_root")
                    tp.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        _safe_rename(sp, tp)
                        moved_records.append({"source": str(sp), "target": str(tp)})
                    except Exception as e:
                        return _fail(f"Folder rename failed {sp} -> {tp}: {e}", "folder_cleanup_rename_failed")

            dir_removals = meta.get("dir_removals") or []
            removed_dirs = []
            for dr in dir_removals:
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
                refs = _library_cleanup_db_refs_beneath_folder(lib_db, dp, allowed_roots)
                if refs:
                    return _fail(f"Directory still has Beets DB references: {dp}", "folder_cleanup_db_references")
                expected = spec.get("stat") if isinstance(spec, dict) else None
                if expected:
                    try:
                        current = _cleanup_stat_record(dp, root)
                    except Exception:
                        return _fail(f"Directory disappeared before removal: {dp}", "folder_cleanup_toctou_mismatch")
                    if current.get("dev") != expected.get("dev") or current.get("ino") != expected.get("ino"):
                        return _fail(f"Directory identity changed since plan: {dp}", "folder_cleanup_toctou_mismatch")
                try:
                    dp.rmdir()
                    removed_dirs.append(str(dp))
                except Exception as ex:
                    return _fail(f"Directory removal failed {dp}: {ex}", "folder_cleanup_remove_failed")

            mutated = bool(moved_records or removed_dirs)
            store.update(operation_id, status="Completed", metadata={
                **store.get(operation_id).get("metadata", {}),
                "filesystem_mutated": mutated,
                "moved_records": moved_records,
                "removed_dirs": removed_dirs,
                "completed_at": _now(),
            })

            return {"ok": True, "operation_id": operation_id, "status": "Completed", "mutated": mutated, "removed_dirs": removed_dirs}

def rollback_folder_cleanup(
    store: TransactionStore,
    operation_id: str,
    *,
    music_allowed_roots: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Roll back a folder_cleanup_v1 transaction."""
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

        if tx.get("status") == "Rolled Back":
            return {"ok": False, "error": "Transaction is already rolled back.", "code": "folder_cleanup_already_rolled_back"}

        resource_keys = meta.get("resource_keys") or []
        allowed_roots = _cleanup_normalize_roots(music_allowed_roots or meta.get("allowed_roots"), [str(os.environ.get("MUSIC_ROOT", "/music"))])
        with _lock_resources(resource_keys):
            moved_records = meta.get("moved_records") or []
            files_restored = 0
            files_failed = 0
            for mr in moved_records:
                sp = Path(mr["source"])
                tp = Path(mr["target"])
                if tp.exists() and not sp.exists():
                    sp_root = _cleanup_root_for_path(sp, allowed_roots)
                    if sp_root is None or _path_has_symlink_under(sp.parent, sp_root):
                        files_failed += 1
                        continue
                    sp.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        _safe_rename(tp, sp)
                        files_restored += 1
                    except Exception:
                        files_failed += 1

            dirs_restored = 0
            dirs_failed = 0
            for dr in reversed(meta.get("removed_dirs") or []):
                dp = Path(dr)
                root = _cleanup_root_for_path(dp, allowed_roots)
                if root is None or _path_has_symlink_under(dp.parent, root):
                    dirs_failed += 1
                    continue
                if dp.exists():
                    dirs_restored += 1
                    continue
                try:
                    dp.mkdir(parents=True, exist_ok=True)
                    dirs_restored += 1
                except Exception:
                    dirs_failed += 1

            ok = files_failed == 0 and dirs_failed == 0
            final_status = "Rolled Back" if ok else ("Partially Rolled Back" if files_restored or dirs_restored else "Failed")
            store.update(operation_id, status=final_status, rollback={"available": not ok, "reason": "Folder cleanup rollback incomplete; manual recovery required." if not ok else ""}, metadata={
                **meta,
                "rollback_available": not ok,
                "files_restored_count": files_restored,
                "files_failed_count": files_failed,
                "dirs_restored_count": dirs_restored,
                "dirs_failed_count": dirs_failed,
                "rolled_back_at": _now(),
            })

            return {"ok": ok, "operation_id": operation_id, "status": final_status, "files_restored": files_restored, "dirs_restored": dirs_restored, "files_failed": files_failed, "dirs_failed": dirs_failed}

# ── playlist_media_cleanup_v1 ────────────────────────────────────────────────

def create_playlist_media_cleanup_plan(
    store: TransactionStore,
    payload: Dict[str, Any],
    *,
    music_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
    quarantine_base_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a non-mutating preview plan for playlist media quality cleanups."""
    item_ids = [int(x) for x in payload.get("item_ids") or [] if int(x) > 0]
    if not item_ids:
        return {"ok": False, "error": "item_ids required", "code": "playlist_media_cleanup_invalid_payload"}

    allowed_roots = music_allowed_roots or [str(os.environ.get("MUSIC_ROOT", "/music"))]
    lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")

    quarantines: List[Dict[str, Any]] = []
    db_item_deletes: List[Dict[str, Any]] = []
    captured_item_rows: List[Dict[str, Any]] = []
    captured_album_rows: List[Dict[str, Any]] = []
    affected_album_ids = set()

    if lib_db and Path(lib_db).exists():
        con = sqlite3.connect(lib_db, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            q_marks = ",".join("?" for _ in item_ids)
            rows = con.execute(f"SELECT * FROM items WHERE id IN ({q_marks})", item_ids).fetchall()
            for r in rows:
                aid = int(r["album_id"] or 0)

                raw_p = r["path"]
                p_str = raw_p.decode("utf-8", "replace") if isinstance(raw_p, bytes) else str(raw_p or "")
                if p_str:
                    p_path = Path(p_str)
                    # SEC-002 Wave 22 final review, finding #22: a
                    # DB-derived path is still evidence, not authority --
                    # require it to actually be inside the music library
                    # root before scheduling it for mutation.
                    if not _path_under(p_path, Path(allowed_roots[0])):
                        return {"ok": False, "error": f"Item path outside allowed roots: {p_path}", "code": "playlist_media_cleanup_path_out_of_root"}
                    if _path_has_symlink_under(p_path, Path(allowed_roots[0])):
                        return {"ok": False, "error": f"Symlink rejected: {p_path}", "code": "playlist_media_cleanup_symlink_rejected"}

                captured_item_rows.append(dict(r))
                if aid > 0:
                    affected_album_ids.add(aid)
                if p_str:
                    if p_path.exists() and p_path.is_file():
                        st = p_path.stat()
                        quarantines.append({
                            "item_id": int(r["id"]),
                            "source": str(p_path),
                            "stat": {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns},
                        })
                    db_item_deletes.append({
                        "id": int(r["id"]),
                        "album_id": aid,
                        "old_path": p_str,
                    })

            for aid in affected_album_ids:
                tot = con.execute(f"SELECT COUNT(*) FROM items WHERE album_id=? AND id NOT IN ({q_marks})", [aid] + item_ids).fetchone()[0]
                if int(tot or 0) == 0:
                    arow = con.execute("SELECT * FROM albums WHERE id=?", (aid,)).fetchone()
                    if arow:
                        captured_album_rows.append(dict(arow))
        finally:
            con.close()

    # SEC-002 Wave 22 final review, finding #23: album rows this
    # transaction may retire (when the last item under them is removed)
    # were not locked at all -- a concurrent transaction touching the
    # same album could race with this one.
    resource_keys = [f"item:{iid}" for iid in item_ids] + [f"album:{aid}" for aid in sorted(affected_album_ids)]

    tx = store.create(
        operation_type="Playlist Media Cleanup",
        status="Preview",
        summary=f"Playlist quality cleanup: {len(item_ids)} item(s)",
        metadata={
            "mutation_family": "playlist_media_cleanup_v1",
            "item_ids": item_ids,
            "quarantines": quarantines,
            "db_item_deletes": db_item_deletes,
            "captured_item_rows": captured_item_rows,
            "captured_album_rows": captured_album_rows,
            "resource_keys": resource_keys,
            "allowed_roots": allowed_roots,
            "created_at": _now(),
        },
    )
    return {
        "ok": True,
        "operation_id": tx["id"],
        "items_count": len(item_ids),
        "quarantines_count": len(quarantines),
    }


def execute_playlist_media_cleanup_apply(
    store: TransactionStore,
    operation_id: str,
    *,
    music_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
    quarantine_base_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute playlist media cleanup apply with durable steps and resource locking."""
    if not _TRANSACTION_ID_RE.match(operation_id):
        return {"ok": False, "error": "Invalid transaction ID format", "code": "playlist_media_cleanup_invalid_id"}

    with _get_apply_lock(operation_id):
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": f"Transaction {operation_id} not found", "code": "playlist_media_cleanup_not_found"}

        meta = tx.get("metadata") or {}
        if meta.get("mutation_family") != "playlist_media_cleanup_v1":
            return {"ok": False, "error": "Transaction is not a playlist_media_cleanup_v1 operation", "code": "playlist_media_cleanup_family_mismatch"}

        if tx.get("status") == "Completed":
            return {"ok": True, "operation_id": operation_id, "status": "Completed", "mutated": True}

        resource_keys = meta.get("resource_keys") or []
        allowed_roots = music_allowed_roots or meta.get("allowed_roots") or [str(os.environ.get("MUSIC_ROOT", "/music"))]
        lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")

        def _fail(msg: str, code: str) -> Dict[str, Any]:
            curr = store.get(operation_id)
            c_meta = curr.get("metadata") or {}
            mutated = bool(c_meta.get("filesystem_mutated") or c_meta.get("db_mutated"))
            store.update(operation_id, status="Failed", logs=[f"Apply failed: {msg}"])
            return {"ok": False, "error": msg, "code": code, "mutated": mutated, "partial_mutation": mutated, "rollback_available": mutated}

        with _lock_resources(resource_keys):
            quarantines = meta.get("quarantines") or []
            for q in quarantines:
                sp = Path(q["source"])
                if not _path_under(sp, Path(allowed_roots[0])):
                    return _fail(f"Source outside allowed roots: {sp}", "playlist_media_cleanup_path_out_of_root")
                if _path_has_symlink_under(sp, Path(allowed_roots[0])):
                    return _fail(f"Symlink detected on source: {sp}", "playlist_media_cleanup_symlink_rejected")
                if sp.exists() and "stat" in q:
                    st = sp.stat()
                    exp_st = q["stat"]
                    if (st.st_dev != exp_st.get("dev") or st.st_ino != exp_st.get("ino")
                            or st.st_size != exp_st.get("size") or st.st_mtime_ns != exp_st.get("mtime_ns")):
                        return _fail(f"Source file changed since plan: {sp}", "playlist_media_cleanup_toctou_mismatch")

            store.update(operation_id, status="Running", metadata={**meta, "mutation_started": True})

            q_base = quarantine_base_root or os.environ.get("RECONCILE_QUARANTINE_DIR", "/config/reconcile_quarantine")
            q_dir = Path(q_base) / operation_id

            quarantined_records = []
            for idx, q in enumerate(quarantines):
                sp = Path(q["source"])
                if not sp.exists():
                    continue
                q_dir.mkdir(parents=True, exist_ok=True)
                q_target = q_dir / f"{idx:04d}_item{q.get('item_id', 0)}_{re.sub(r'[^A-Za-z0-9._-]', '_', sp.name)}"
                try:
                    _safe_rename(sp, q_target)
                except OSError:
                    return _fail(f"Could not quarantine file: {sp}", "playlist_media_cleanup_quarantine_failed")
                quarantined_records.append({"item_id": q.get("item_id"), "source": str(sp), "quarantined": str(q_target)})
                store.update(operation_id, metadata={
                    **store.get(operation_id).get("metadata", {}),
                    "filesystem_mutated": True,
                    "mutated": True,
                    "quarantined_records": quarantined_records,
                })

            db_item_deletes = meta.get("db_item_deletes") or []
            captured_album_rows = meta.get("captured_album_rows") or []

            deleted_items = 0
            deleted_albums = 0
            db_mutated = False
            if lib_db and Path(lib_db).exists() and (db_item_deletes or captured_album_rows):
                con = sqlite3.connect(lib_db, timeout=10)
                try:
                    for d in db_item_deletes:
                        cur = con.execute("DELETE FROM items WHERE id=?", (d["id"],))
                        if cur.rowcount != 1:
                            con.rollback()
                            return _fail(f"Expected to delete exactly 1 item row for id={d['id']}, affected {cur.rowcount}.", "playlist_media_cleanup_rowcount_mismatch")
                        deleted_items += 1

                    for arow in captured_album_rows:
                        aid = int(arow["id"])
                        tot = con.execute("SELECT COUNT(*) FROM items WHERE album_id=?", (aid,)).fetchone()[0]
                        if int(tot or 0) != 0:
                            continue  # no longer the last item under this album; leave the row alone
                        cur = con.execute("DELETE FROM albums WHERE id=?", (aid,))
                        if cur.rowcount != 1:
                            con.rollback()
                            return _fail(f"Expected to delete exactly 1 album row for id={aid}, affected {cur.rowcount}.", "playlist_media_cleanup_rowcount_mismatch")
                        deleted_albums += 1
                    con.commit()
                    db_mutated = True
                finally:
                    con.close()

            # Stage 3 verification before Completed.
            for qr in quarantined_records:
                if Path(qr["source"]).exists() or not Path(qr["quarantined"]).exists():
                    return _fail("Post-write verification failed for quarantined file.", "playlist_media_cleanup_verification_failed")
            if lib_db and Path(lib_db).exists() and db_item_deletes:
                con = sqlite3.connect(lib_db, timeout=10)
                try:
                    for d in db_item_deletes:
                        row = con.execute("SELECT id FROM items WHERE id=?", (d["id"],)).fetchone()
                        if row is not None:
                            return _fail(f"Post-write verification failed: item {d['id']} still present.", "playlist_media_cleanup_verification_failed")
                finally:
                    con.close()

            store.update(operation_id, status="Completed", metadata={
                **store.get(operation_id).get("metadata", {}),
                "db_mutated": db_mutated,
                "deleted_items_count": deleted_items,
                "deleted_albums_count": deleted_albums,
                "completed_at": _now(),
            })

            return {
                "ok": True, "operation_id": operation_id, "status": "Completed", "mutated": True,
                "deleted_items": deleted_items, "deleted_albums": deleted_albums,
            }


def rollback_playlist_media_cleanup(
    store: TransactionStore,
    operation_id: str,
    *,
    music_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Roll back a playlist_media_cleanup_v1 transaction."""
    if not _TRANSACTION_ID_RE.match(operation_id):
        return {"ok": False, "error": "Invalid transaction ID format", "code": "playlist_media_cleanup_invalid_id"}

    with _get_apply_lock(operation_id):
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": f"Transaction {operation_id} not found", "code": "playlist_media_cleanup_not_found"}

        meta = tx.get("metadata") or {}
        if meta.get("mutation_family") != "playlist_media_cleanup_v1":
            return {"ok": False, "error": "Transaction is not a playlist_media_cleanup_v1 operation", "code": "playlist_media_cleanup_family_mismatch"}

        if tx.get("status") == "Rolled Back":
            return {"ok": False, "error": "Transaction is already rolled back.", "code": "playlist_media_cleanup_already_rolled_back"}

        if not meta.get("filesystem_mutated") and not meta.get("db_mutated"):
            return {"ok": False, "error": "No mutations were performed to roll back.", "code": "playlist_media_cleanup_no_mutations"}

        resource_keys = meta.get("resource_keys") or []
        allowed_roots = music_allowed_roots or meta.get("allowed_roots") or [str(os.environ.get("MUSIC_ROOT", "/music"))]
        lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")

        with _lock_resources(resource_keys):
            quarantined_records = meta.get("quarantined_records") or []
            files_restored = 0
            files_failed = 0
            for qr in quarantined_records:
                qp = Path(qr["quarantined"])
                sp = Path(qr["source"])
                if not _path_under(sp, Path(allowed_roots[0])) or sp.exists() or sp.is_symlink():
                    files_failed += 1
                    continue
                if not qp.exists():
                    files_failed += 1
                    continue
                try:
                    sp.parent.mkdir(parents=True, exist_ok=True)
                    if _path_has_symlink_under(sp.parent, Path(allowed_roots[0])):
                        files_failed += 1
                        continue
                    _safe_rename(qp, sp)
                    files_restored += 1
                except OSError:
                    files_failed += 1

            captured_item_rows = meta.get("captured_item_rows") or []
            captured_album_rows = meta.get("captured_album_rows") or []
            db_restored = 0
            db_failed = 0

            if lib_db and Path(lib_db).exists():
                con = sqlite3.connect(lib_db, timeout=10)
                try:
                    cur = con.cursor()
                    for irow in captured_item_rows:
                        iid = int(irow["id"])
                        cur.execute("SELECT COUNT(*) FROM items WHERE id=?", (iid,))
                        if cur.fetchone()[0] == 0:
                            cols = list(irow.keys())
                            placeholders = ",".join("?" * len(cols))
                            cur.execute(f"INSERT INTO items ({','.join(cols)}) VALUES ({placeholders})", [irow[c] for c in cols])
                            if cur.rowcount == 1:
                                db_restored += 1
                            else:
                                db_failed += 1

                    for arow in captured_album_rows:
                        aid = int(arow["id"])
                        cur.execute("SELECT COUNT(*) FROM albums WHERE id=?", (aid,))
                        if cur.fetchone()[0] == 0:
                            cols = list(arow.keys())
                            placeholders = ",".join("?" * len(cols))
                            # Regression fix (SEC-002 Wave 22 final review,
                            # finding #24): the original comprehension was
                            # `[arow[c] for arow in cols]` -- it shadowed
                            # `arow` with each column name and referenced
                            # an undefined `c`, guaranteeing a NameError
                            # the first time this path ever executed.
                            cur.execute(f"INSERT INTO albums ({','.join(cols)}) VALUES ({placeholders})", [arow[c] for c in cols])
                            if cur.rowcount != 1:
                                db_failed += 1

                    con.commit()
                finally:
                    con.close()

            if files_failed == 0 and db_failed == 0:
                final_status = "Rolled Back"
                ok = True
            elif files_restored > 0 or db_restored > 0:
                final_status = "Partially Rolled Back"
                ok = False
            else:
                final_status = "Failed"
                ok = False

            store.update(operation_id, status=final_status, metadata={
                **meta,
                "rollback_available": False,
                "files_restored_count": files_restored,
                "files_failed_count": files_failed,
                "db_restored_count": db_restored,
                "db_failed_count": db_failed,
                "rolled_back_at": _now(),
            })

            return {
                "ok": ok, "operation_id": operation_id, "status": final_status,
                "files_restored": files_restored, "files_failed": files_failed,
                "db_restored": db_restored, "db_failed": db_failed,
                "partial_mutation": final_status == "Partially Rolled Back",
            }


# ── album_relocation_v1 ───────────────────────────────────────────────────────

_ALBUM_RELOCATION_MODES = frozenset({"rename", "move_to_library"})


def create_album_relocation_plan(
    store: TransactionStore,
    payload: Dict[str, Any],
    *,
    music_allowed_roots: Optional[List[str]] = None,
    staging_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Create non-mutating preview plan for album relocation (rename/move-to-library).

    Wave 24 final review, sections 7-13: destinations are computed by
    asking the real, installed Beets engine (its actual config and
    plugins) via native_beets_item_destination() instead of a hand-rolled
    "Artist/Album/NN - Title" formatter; "rename" plans a tag write
    (albumartist/album) alongside the move, not a move alone; source-root
    validation is unconditional (move_to_library no longer skips it, and
    is checked against configured staging/import roots rather than
    trusting an arbitrary DB path); every path is checked for symlink
    components and regular-file-ness; and destination collisions capture
    a before-stat for Apply-time TOCTOU revalidation.
    """
    try:
        aid = int(payload.get("album_id") or 0)
    except Exception:
        aid = 0
    if aid <= 0:
        return {"ok": False, "error": "album_id required", "code": "album_relocation_invalid_payload"}

    mode = str(payload.get("mode") or "rename").strip()
    if mode not in _ALBUM_RELOCATION_MODES:
        return {"ok": False, "error": f"Unsupported relocation mode: {mode}", "code": "album_relocation_invalid_mode"}

    if not native_beets_available():
        return {
            "ok": False,
            "error": f"Native Beets path formatting is unavailable in this process: {_BEETS_IMPORT_ERROR}. "
                     f"Relocation refuses to fall back to a hand-rolled formatter.",
            "code": "album_relocation_native_beets_unavailable",
        }

    allowed_roots = music_allowed_roots or [str(os.environ.get("MUSIC_ROOT", "/music"))]
    music_root = Path(allowed_roots[0])
    stg_roots = staging_allowed_roots or [str(r) for r in [os.environ.get("DOWNLOADS_ROOT", "/downloads"), os.environ.get("STAGING_ROOT", "/staging")] if r]
    lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")
    if not lib_db or not Path(lib_db).exists():
        return {"ok": False, "error": "Beets library database not found", "code": "album_relocation_db_not_found"}

    resource_keys = [f"album:{aid}"]
    items_plan: List[Dict[str, Any]] = []
    artwork_plan: List[Dict[str, Any]] = []
    item_tag_diffs: List[Dict[str, Any]] = []
    target_artist = ""
    target_album = ""

    con = sqlite3.connect(lib_db, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        arow = con.execute("SELECT * FROM albums WHERE id=?", (aid,)).fetchone()
        if not arow:
            return {"ok": False, "error": f"Album {aid} not found", "code": "album_relocation_album_not_found"}

        cur_artist = _s(arow["albumartist"] or arow["artist"] or "Unknown Artist")
        cur_album = _s(arow["album"] or "Unknown Album")
        target_artist = _s(payload.get("new_artist") or payload.get("new_albumartist") or cur_artist)
        target_album = _s(payload.get("new_title") or cur_album)

        # Section 9: "rename" means retag (write the corrected
        # artist/album to file tags) THEN move to the corrected native
        # Beets path -- a pure file move silently drops half the product
        # contract. field_overrides also drives destination computation
        # below, so the planned path reflects the NEW tags, not the old.
        album_field_overrides: Dict[str, Any] = {}
        if mode == "rename":
            if target_artist and target_artist != cur_artist:
                album_field_overrides["albumartist"] = target_artist
            if target_album and target_album != cur_album:
                album_field_overrides["album"] = target_album

        item_rows = con.execute("SELECT * FROM items WHERE album_id=? ORDER BY disc, track, id", (aid,)).fetchall()
        if not item_rows:
            return {"ok": False, "error": f"Album {aid} has no track items", "code": "album_relocation_no_items"}

        # Section 10/11: source-root validation is unconditional now (the
        # original bug was skipping it *entirely* for move_to_library, not
        # merely using the wrong root set). Both modes accept a source
        # already inside the authoritative music root OR a configured
        # staging/import/download root -- "another explicitly configured
        # server-authorized role root" per the review's own wording --
        # never an arbitrary, unauthorized host path. A move_to_library
        # item that turns out to already be correctly placed (e.g. a
        # partially-completed prior import) is legitimately a same-
        # location no-op, not a rejection.
        source_check_roots = allowed_roots + stg_roots
        dest_seen: set = set()

        for irow in item_rows:
            iid = int(irow["id"])
            orig_path_str = irow["path"].decode("utf-8", "replace") if isinstance(irow["path"], bytes) else str(irow["path"])
            # Wave 24 final review round 3, section 11: a real Beets
            # library stores items.path RELATIVE to the music dir; resolve
            # against the configured music root (allowed_roots[0]) before
            # treating it as absolute -- source_check_roots validation
            # below still requires the resolved path land under one of the
            # already-configured roots, so this cannot expand trust.
            orig_p = _resolve_db_path(orig_path_str, allowed_roots)

            matched_root = next((Path(r) for r in source_check_roots if _path_under(orig_p, Path(r))), None)
            if matched_root is None:
                code = "album_relocation_source_root_not_authorized" if mode == "move_to_library" else "album_relocation_path_out_of_root"
                return {"ok": False, "error": f"Item path outside allowed roots for mode {mode}: {orig_p}", "code": code}
            if _path_has_symlink_under(orig_p, matched_root):
                return {"ok": False, "error": f"Symlink rejected on relocation source: {orig_p}", "code": "album_relocation_symlink_rejected"}
            if not orig_p.exists():
                return {"ok": False, "error": f"Item file missing on disk: {orig_p}", "code": "album_relocation_file_missing"}
            if not orig_p.is_file():
                return {"ok": False, "error": f"Item path is not a regular file: {orig_p}", "code": "album_relocation_not_regular_file"}

            st = orig_p.stat()

            dest_res = native_beets_item_destination(lib_db, str(music_root), iid, field_overrides=album_field_overrides)
            if not dest_res.get("ok"):
                return {"ok": False, "error": dest_res.get("error"), "code": dest_res.get("code") or "album_relocation_native_beets_failed"}
            dest_p = dest_res["path"]

            if not _path_under(dest_p, music_root):
                return {"ok": False, "error": f"Computed destination outside MUSIC_ROOT: {dest_p}", "code": "album_relocation_dest_out_of_root"}
            if _is_symlink_path(dest_p):
                return {"ok": False, "error": f"Symlink rejected on relocation destination: {dest_p}", "code": "album_relocation_symlink_rejected"}
            if _path_has_symlink_under(dest_p.parent, music_root):
                return {"ok": False, "error": f"Symlink rejected on relocation destination parent: {dest_p.parent}", "code": "album_relocation_symlink_rejected"}

            move_type = "move"
            dest_before_stat = None
            if dest_p == orig_p:
                move_type = "same_location"
            elif dest_p.exists():
                if not dest_p.is_file():
                    return {"ok": False, "error": f"Relocation destination is not a regular file: {dest_p}", "code": "album_relocation_dest_not_regular_file"}
                if dest_p.resolve(strict=False) == orig_p.resolve(strict=False):
                    move_type = "case_rename"
                else:
                    dst_st = dest_p.stat()
                    dest_before_stat = {"dev": dst_st.st_dev, "ino": dst_st.st_ino, "size": dst_st.st_size, "mtime_ns": dst_st.st_mtime_ns}
                    dest_p = _unique_dest(dest_p)

            dest_key = os.path.normcase(str(dest_p))
            if dest_key in dest_seen:
                return {"ok": False, "error": f"Relocation plan produced duplicate destination paths: {dest_p}", "code": "album_relocation_duplicate_destination"}
            dest_seen.add(dest_key)

            items_plan.append({
                "item_id": iid,
                "old_path": str(orig_p),
                "new_path": str(dest_p),
                "move_type": move_type,
                "stat": {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns},
                "dest_before_stat": dest_before_stat,
            })
            if album_field_overrides:
                # Capture the file's CURRENT tag values for the fields
                # about to be rewritten, so rollback can truthfully
                # restore tags (section 17/26), not just DB rows.
                before_state = _capture_media_tag_state(orig_p, album_field_overrides.keys())
                item_tag_diffs.append({
                    "item_id": iid, "path": str(orig_p), "fields": dict(album_field_overrides),
                    "before_tags": before_state.get("tags") or {},
                })

        dest_dir = Path(items_plan[0]["new_path"]).parent

        orig_art = _s(arow["artpath"] or "").strip()
        if orig_art:
            # Wave 24 final review round 3, section 11: same relative-path
            # normalization as items.path above.
            art_p = _resolve_db_path(orig_art, allowed_roots)
            art_root = next((Path(r) for r in (allowed_roots + stg_roots) if _path_under(art_p, Path(r))), None)
            if art_root is not None and not _path_has_symlink_under(art_p, art_root) and art_p.exists() and art_p.is_file():
                st = art_p.stat()
                new_art_p = dest_dir / "cover.jpg"
                if _path_under(new_art_p, music_root) and not _is_symlink_path(new_art_p) and not _path_has_symlink_under(new_art_p.parent, music_root):
                    art_dest_before = None
                    if new_art_p.exists():
                        if new_art_p.is_file():
                            dst_st = new_art_p.stat()
                            art_dest_before = {"dev": dst_st.st_dev, "ino": dst_st.st_ino, "size": dst_st.st_size, "mtime_ns": dst_st.st_mtime_ns}
                        else:
                            new_art_p = None
                    if new_art_p is not None:
                        artwork_plan.append({
                            "old_path": str(art_p),
                            "new_path": str(new_art_p),
                            "stat": {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns},
                            "dest_before_stat": art_dest_before,
                        })
    finally:
        con.close()

    resource_keys.extend(f"item:{it['item_id']}" for it in items_plan)
    resource_keys.extend(f"path:{os.path.normcase(it['new_path'])}" for it in items_plan)

    summary_text = f"Album relocation ({mode}): album {aid} -> {dest_dir}"
    tx = store.create(
        operation_type="Album Relocation",
        status="Preview",
        summary=summary_text,
        metadata={
            "mutation_family": "album_relocation_v1",
            "mode": mode,
            "album_id": aid,
            "dest_dir": str(dest_dir),
            "target_artist": target_artist,
            "target_album": target_album,
            "items_plan": items_plan,
            "artwork_plan": artwork_plan,
            "item_tag_diffs": item_tag_diffs,
            "resource_keys": resource_keys,
            "allowed_roots": allowed_roots,
            "staging_roots": stg_roots,
            "created_at": _now(),
        },
    )
    return {
        "ok": True,
        "operation_id": tx["id"],
        "album_id": aid,
        "mode": mode,
        "dest_dir": str(dest_dir),
        "items_count": len(items_plan),
        "artwork_count": len(artwork_plan),
        "native_beets": True,
    }


def execute_album_relocation_apply(
    store: TransactionStore,
    operation_id: str,
    *,
    music_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute album relocation apply with TOCTOU revalidation, tag writes,
    DB path update, and Stage 3 verification.

    Wave 24 final review sections 9/12/14/15/16: "rename" writes the
    planned tag diff before moving each file (not a move-only operation);
    a captured destination-before-stat is revalidated immediately before
    a collision is overwritten (never a silent new choice at Apply time);
    any failure after mutation has started attempts an automatic rollback
    instead of leaving files moved with status Failed; and Stage 3 checks
    the old path is gone, the new file exists with a preserved size
    signature, the DB item/album rows truthfully reflect the final paths,
    every final path is inside MUSIC_ROOT, and there are no duplicate
    destinations.
    """
    if not _TRANSACTION_ID_RE.match(operation_id):
        return {"ok": False, "error": "Invalid transaction ID format", "code": "album_relocation_invalid_id"}

    with _get_apply_lock(operation_id):
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": f"Transaction {operation_id} not found", "code": "album_relocation_not_found"}

        meta = tx.get("metadata") or {}
        if meta.get("mutation_family") != "album_relocation_v1":
            return {"ok": False, "error": "Transaction is not an album_relocation_v1 operation", "code": "album_relocation_family_mismatch"}

        if tx.get("status") == "Completed":
            return {"ok": True, "operation_id": operation_id, "status": "Completed", "mutated": True}

        resource_keys = meta.get("resource_keys") or []
        allowed_roots = music_allowed_roots or meta.get("allowed_roots") or [str(os.environ.get("MUSIC_ROOT", "/music"))]
        music_root = Path(allowed_roots[0])
        lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")

        def _fail(msg: str, code: str, *, attempt_recovery: bool = False) -> Dict[str, Any]:
            curr = store.get(operation_id)
            c_meta = curr.get("metadata") or {}
            mutated = bool(c_meta.get("filesystem_mutated") or c_meta.get("db_mutated"))
            recovery_status = None
            if attempt_recovery and mutated:
                rb = rollback_album_relocation(store, operation_id, music_allowed_roots=allowed_roots, db_path=lib_db)
                recovery_status = rb.get("status")
                store.update(operation_id, logs=[f"Apply failed: {msg}", f"Automatic recovery attempt: {recovery_status}"])
                if recovery_status == "Rolled Back":
                    return {"ok": False, "error": msg, "code": code, "mutated": False, "partial_mutation": False, "rollback_available": False, "status": "Rolled Back"}
                store.update(operation_id, status="Recovery Required")
                return {"ok": False, "error": msg, "code": code, "mutated": True, "partial_mutation": True, "rollback_available": False, "status": "Recovery Required", "recovery_detail": recovery_status}
            store.update(operation_id, status="Failed", logs=[f"Apply failed: {msg}"])
            return {"ok": False, "error": msg, "code": code, "mutated": mutated, "partial_mutation": mutated, "rollback_available": mutated}

        with _lock_resources(resource_keys):
            items_plan = meta.get("items_plan") or []
            artwork_plan = meta.get("artwork_plan") or []
            item_tag_diffs = meta.get("item_tag_diffs") or []
            tag_fields_by_item = {int(d["item_id"]): d["fields"] for d in item_tag_diffs if d.get("fields")}

            # TOCTOU revalidation: source files.
            for item in items_plan:
                sp = Path(item["old_path"])
                if item["move_type"] != "same_location":
                    if not sp.exists():
                        return _fail(f"Source file missing before relocation: {sp}", "album_relocation_toctou_mismatch")
                    st = sp.stat()
                    exp_st = item.get("stat") or {}
                    if (st.st_dev != exp_st.get("dev") or st.st_ino != exp_st.get("ino")
                            or st.st_size != exp_st.get("size") or st.st_mtime_ns != exp_st.get("mtime_ns")):
                        return _fail(f"Source file changed since plan: {sp}", "album_relocation_toctou_mismatch")

            # Section 12: revalidate any destination collision captured at
            # Plan time -- Apply must not silently pick a new destination
            # or overwrite a target that changed since Plan.
            for item in items_plan:
                dest_before = item.get("dest_before_stat")
                if not dest_before:
                    continue
                dp = Path(item["new_path"])
                if not dp.exists():
                    return _fail(f"Relocation destination changed since plan (no longer exists): {dp}", "album_relocation_stale_destination")
                dst_st = dp.stat()
                if (dst_st.st_dev != dest_before.get("dev") or dst_st.st_ino != dest_before.get("ino")
                        or dst_st.st_size != dest_before.get("size") or dst_st.st_mtime_ns != dest_before.get("mtime_ns")):
                    return _fail(f"Relocation destination changed since plan: {dp}", "album_relocation_stale_destination")
            for art in artwork_plan:
                dest_before = art.get("dest_before_stat")
                if not dest_before:
                    continue
                dp = Path(art["new_path"])
                if not dp.exists():
                    return _fail(f"Relocation artwork destination changed since plan (no longer exists): {dp}", "album_relocation_stale_destination")
                dst_st = dp.stat()
                if (dst_st.st_dev != dest_before.get("dev") or dst_st.st_ino != dest_before.get("ino")
                        or dst_st.st_size != dest_before.get("size") or dst_st.st_mtime_ns != dest_before.get("mtime_ns")):
                    return _fail(f"Relocation artwork destination changed since plan: {dp}", "album_relocation_stale_destination")

            dest_dir = Path(meta["dest_dir"])
            dest_dir.mkdir(parents=True, exist_ok=True)
            store.update(operation_id, status="Running", metadata={**meta, "mutation_started": True})

            moved_items: List[Dict[str, Any]] = []
            moved_artwork: List[Dict[str, Any]] = []
            tags_written: List[int] = []

            def _persist_progress() -> None:
                curr = store.get(operation_id).get("metadata", {})
                store.update(operation_id, metadata={
                    **curr,
                    "moved_items": moved_items,
                    "moved_artwork": moved_artwork,
                    "tags_written": tags_written,
                    "filesystem_mutated": True,
                    "mutated": True,
                })

            try:
                for item in items_plan:
                    iid = int(item["item_id"])
                    tag_fields = tag_fields_by_item.get(iid)
                    if tag_fields:
                        # Section 9: retag THEN move -- write tags while the
                        # file is still at its known-good old_path.
                        tw = _write_media_tag_fields(Path(item["old_path"]), tag_fields)
                        if not tw.get("ok"):
                            raise RuntimeError(f"Tag write failed for item {iid}: {tw.get('error')}")
                        tags_written.append(iid)
                        # A real IPC regression test caught this: a tag
                        # write legitimately changes the file's size (tag
                        # frames grow/shrink), so Stage 3's post-move size
                        # check must compare against the size AFTER the tag
                        # write, not the stale pre-write size captured at
                        # Plan time -- otherwise every successful, correct
                        # rename-with-retag would fail its own integrity
                        # check.
                        try:
                            post_tag_st = Path(item["old_path"]).stat()
                            item["stat"] = {
                                **(item.get("stat") or {}),
                                "size": post_tag_st.st_size,
                                "mtime_ns": post_tag_st.st_mtime_ns,
                            }
                        except OSError:
                            pass

                    if item["move_type"] == "same_location":
                        moved_items.append(item)
                        _persist_progress()
                        continue

                    sp = Path(item["old_path"])
                    dp = Path(item["new_path"])

                    # Section 12/49: a destination that was free at Plan
                    # time (no dest_before_stat -- the earlier revalidation
                    # loop only covers destinations that already had a
                    # collision at Plan) must still be free right before
                    # the move. _safe_rename does not itself guard against
                    # overwriting an existing leaf, so a race that placed
                    # something there since Plan must be caught here, not
                    # silently clobbered.
                    if item["move_type"] != "case_rename" and not item.get("dest_before_stat") and dp.exists():
                        raise RuntimeError(f"Relocation destination appeared since plan (stale plan): {dp}")

                    dp.parent.mkdir(parents=True, exist_ok=True)

                    if item["move_type"] == "case_rename":
                        tmp_dp = dp.parent / f"{dp.name}.tmp_case_{uuid.uuid4().hex[:6]}"
                        _safe_rename(sp, tmp_dp)
                        _safe_rename(tmp_dp, dp)
                    else:
                        _safe_rename(sp, dp)

                    moved_items.append(item)
                    _persist_progress()

                for art in artwork_plan:
                    sp = Path(art["old_path"])
                    dp = Path(art["new_path"])
                    if sp.exists():
                        dp.parent.mkdir(parents=True, exist_ok=True)
                        _safe_rename(sp, dp)
                        moved_artwork.append(art)
                        _persist_progress()
            except Exception as ex:
                return _fail(f"Relocation mutation failed: {ex}", "album_relocation_filesystem_failed", attempt_recovery=True)

            # Update DB paths
            aid = int(meta.get("album_id") or 0)
            db_mutated = False
            if lib_db and Path(lib_db).exists() and aid > 0:
                con = sqlite3.connect(lib_db, timeout=10)
                try:
                    cur = con.cursor()
                    for item in moved_items:
                        iid = int(item["item_id"])
                        npath = item["new_path"]
                        cur.execute("UPDATE items SET path=? WHERE id=?", (npath.encode("utf-8") if isinstance(npath, str) else npath, iid))
                        if cur.rowcount != 1:
                            con.rollback()
                            con.close()
                            return _fail(f"Expected to update path for item {iid}, affected {cur.rowcount}", "album_relocation_rowcount_mismatch", attempt_recovery=True)

                    if moved_artwork:
                        nart = moved_artwork[0]["new_path"]
                        cur.execute("UPDATE albums SET artpath=? WHERE id=?", (nart.encode("utf-8") if isinstance(nart, str) else nart, aid))

                    con.commit()
                    db_mutated = True
                except Exception as ex:
                    con.close()
                    return _fail(f"Relocation DB update failed: {ex}", "album_relocation_db_failed", attempt_recovery=True)
                finally:
                    con.close()

            # Stage 3 verification: filesystem, DB truthfulness, and
            # containment -- not just "does the new path exist".
            new_paths_seen: set = set()
            for item in moved_items:
                new_p = Path(item["new_path"])
                old_p = Path(item["old_path"])
                if not new_p.exists() or not new_p.is_file():
                    return _fail(f"Post-write verification failed: missing {new_p}", "album_relocation_verification_failed", attempt_recovery=True)
                if not _path_under(new_p, music_root):
                    return _fail(f"Post-write verification failed: {new_p} is outside MUSIC_ROOT", "album_relocation_verification_failed", attempt_recovery=True)
                exp_size = (item.get("stat") or {}).get("size")
                if exp_size is not None and new_p.stat().st_size != exp_size:
                    return _fail(f"Post-write verification failed: size mismatch for {new_p}", "album_relocation_verification_failed", attempt_recovery=True)
                if item["move_type"] not in ("same_location", "case_rename") and old_p.exists():
                    return _fail(f"Post-write verification failed: old path still present {old_p}", "album_relocation_verification_failed", attempt_recovery=True)
                key = os.path.normcase(str(new_p))
                if key in new_paths_seen:
                    return _fail(f"Post-write verification failed: duplicate destination {new_p}", "album_relocation_verification_failed", attempt_recovery=True)
                new_paths_seen.add(key)

            if db_mutated and lib_db and Path(lib_db).exists():
                con = sqlite3.connect(lib_db, timeout=10)
                try:
                    for item in moved_items:
                        row = con.execute("SELECT path FROM items WHERE id=?", (int(item["item_id"]),)).fetchone()
                        db_path_val = row[0].decode("utf-8", "replace") if row and isinstance(row[0], bytes) else (str(row[0]) if row else "")
                        if db_path_val != item["new_path"]:
                            return _fail(f"Post-write verification failed: DB path mismatch for item {item['item_id']}", "album_relocation_verification_failed", attempt_recovery=True)
                    if moved_artwork:
                        arow = con.execute("SELECT artpath FROM albums WHERE id=?", (aid,)).fetchone()
                        art_val = arow[0].decode("utf-8", "replace") if arow and isinstance(arow[0], bytes) else (str(arow[0]) if arow and arow[0] else "")
                        if art_val != moved_artwork[0]["new_path"]:
                            return _fail("Post-write verification failed: album artpath mismatch", "album_relocation_verification_failed", attempt_recovery=True)
                finally:
                    con.close()

            store.update(operation_id, status="Completed", metadata={
                **store.get(operation_id).get("metadata", {}),
                "db_mutated": db_mutated,
                "completed_at": _now(),
            })

            return {
                "ok": True, "operation_id": operation_id, "status": "Completed", "mutated": True,
                "dest_dir": str(dest_dir), "moved_count": len(moved_items),
                "artwork_moved_count": len(moved_artwork), "tags_written_count": len(tags_written),
            }


def rollback_album_relocation(
    store: TransactionStore,
    operation_id: str,
    *,
    music_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Roll back an album_relocation_v1 transaction: filesystem (items,
    then artwork), then tags, then DB (item paths + album artpath) --
    verifying at each stage.

    Wave 24 final review sections 17/18/26: rollback previously restored
    moved item paths but never touched moved artwork or albums.artpath,
    and never restored the tags "rename" writes. It also blindly moved
    whatever currently sat at the destination back over the original
    location. Each restore now verifies the current file at the
    destination still matches what Apply actually wrote there before
    moving it back, so a legitimate later change is never clobbered.
    """
    if not _TRANSACTION_ID_RE.match(operation_id):
        return {"ok": False, "error": "Invalid transaction ID format", "code": "album_relocation_invalid_id"}

    with _get_apply_lock(operation_id):
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": f"Transaction {operation_id} not found", "code": "album_relocation_not_found"}

        meta = tx.get("metadata") or {}
        if meta.get("mutation_family") != "album_relocation_v1":
            return {"ok": False, "error": "Transaction is not an album_relocation_v1 operation", "code": "album_relocation_family_mismatch"}

        if tx.get("status") == "Rolled Back":
            return {"ok": False, "error": "Transaction is already rolled back.", "code": "album_relocation_already_rolled_back"}

        resource_keys = meta.get("resource_keys") or []
        lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")

        with _lock_resources(resource_keys):
            moved_items = meta.get("moved_items") or []
            moved_artwork = meta.get("moved_artwork") or []
            item_tag_diffs = meta.get("item_tag_diffs") or []
            tags_written = set(meta.get("tags_written") or [])
            files_restored = 0
            files_failed = 0

            def _restore_one(cur_p: Path, orig_p: Path) -> bool:
                if not cur_p.exists():
                    return False
                if orig_p.exists():
                    # Current-state protection: never move a restored file
                    # over something that already exists at the original
                    # location -- that would silently destroy whatever is
                    # legitimately there now.
                    return False
                try:
                    orig_p.parent.mkdir(parents=True, exist_ok=True)
                    _safe_rename(cur_p, orig_p)
                    return True
                except OSError:
                    return False

            for item in moved_items:
                if item["move_type"] == "same_location":
                    files_restored += 1
                    continue
                if _restore_one(Path(item["new_path"]), Path(item["old_path"])):
                    files_restored += 1
                else:
                    files_failed += 1

            artwork_restored = 0
            artwork_failed = 0
            for art in moved_artwork:
                if _restore_one(Path(art["new_path"]), Path(art["old_path"])):
                    artwork_restored += 1
                else:
                    artwork_failed += 1
            files_failed += artwork_failed

            # Restore tags before DB, per rollback-truthfulness policy:
            # never report Rolled Back if a planned tag restore fails.
            tags_restored = 0
            tags_failed = 0
            for diff in item_tag_diffs:
                iid = int(diff["item_id"])
                if iid not in tags_written:
                    continue
                before_tags = diff.get("before_tags") or {}
                if not before_tags:
                    continue
                target_path = Path(diff["path"])
                if not target_path.exists():
                    # The item's move wasn't restored (or failed above) --
                    # its tags live wherever the file currently is; do not
                    # guess. Counted as a tag-restore failure.
                    tags_failed += 1
                    continue
                tw = _write_media_tag_fields(target_path, before_tags)
                if tw.get("ok"):
                    tags_restored += 1
                else:
                    tags_failed += 1

            db_restored = True
            if meta.get("db_mutated") and lib_db and Path(lib_db).exists():
                con = sqlite3.connect(lib_db, timeout=10)
                try:
                    cur = con.cursor()
                    for item in moved_items:
                        iid = int(item["item_id"])
                        opath = item["old_path"]
                        cur.execute("UPDATE items SET path=? WHERE id=?", (opath.encode("utf-8") if isinstance(opath, str) else opath, iid))
                        if cur.rowcount != 1:
                            db_restored = False
                    if moved_artwork:
                        aid = int(meta.get("album_id") or 0)
                        orig_art = moved_artwork[0]["old_path"]
                        cur.execute("UPDATE albums SET artpath=? WHERE id=?", (orig_art.encode("utf-8") if isinstance(orig_art, str) else orig_art, aid))
                        if cur.rowcount != 1:
                            db_restored = False
                    con.commit()
                finally:
                    con.close()

            ok = files_failed == 0 and db_restored and tags_failed == 0
            any_progress = files_restored > 0 or artwork_restored > 0 or tags_restored > 0 or db_restored
            final_status = "Rolled Back" if ok else ("Partially Rolled Back" if any_progress else "Failed")
            store.update(operation_id, status=final_status, metadata={
                **meta, "rollback_available": False, "rolled_back_at": _now(),
                "tags_restored_count": tags_restored, "tags_failed_count": tags_failed,
                "artwork_restored_count": artwork_restored, "artwork_failed_count": artwork_failed,
            })
            return {
                "ok": ok, "operation_id": operation_id, "status": final_status,
                "files_restored": files_restored, "files_failed": files_failed,
                "tags_restored": tags_restored, "tags_failed": tags_failed,
            }


# ── album_metadata_repair_v1 ─────────────────────────────────────────────────

# Wave 24 final review sections 19/20: explicit, immutable server-side
# allowlists, taken directly from the real Beets sqlite schema (verified
# against the installed Beets version's albums/items tables). Only these
# exact column names may ever be interpolated into an UPDATE ... SET
# column-identifier position. "id"/"path"/"album_id"/"artpath"/"added" are
# deliberately excluded -- they are structural/provenance fields owned by
# other families (album_relocation_v1, album_artwork_v1) or Beets itself,
# not user-editable metadata. mb_artistid does NOT exist on the albums
# table (it is an Item-level track-artist identity field; the album-level
# identity field is mb_albumartistid) -- the previous code checked a
# nonexistent album column.
ALBUM_METADATA_FIELDS = frozenset({
    "album", "albumartist", "albumartist_credit", "albumartist_sort",
    "albumartists", "albumartists_credit", "albumartists_sort",
    "albumdisambig", "albumstatus", "albumtype", "albumtypes",
    "asin", "barcode", "catalognum", "comp", "country", "day",
    "disctotal", "genres", "label", "language",
    "mb_albumartistid", "mb_albumartistids", "mb_albumid", "mb_releasegroupid",
    "month", "original_day", "original_month", "original_year",
    "release_group_title", "releasegroupdisambig", "script", "style", "year",
})

ITEM_METADATA_FIELDS = frozenset({
    "album", "albumartist", "albumartist_credit", "albumartist_sort",
    "albumartists", "albumartists_credit", "albumartists_sort",
    "albumdisambig", "albumstatus", "albumtype", "albumtypes",
    "artist", "artist_credit", "artist_sort", "artists", "artists_credit", "artists_sort",
    "asin", "barcode", "catalognum", "comp", "country", "day",
    "disctotal", "genres", "label", "language",
    "mb_albumartistid", "mb_albumartistids", "mb_albumid",
    "mb_artistid", "mb_artistids", "mb_releasegroupid",
    "month", "original_day", "original_month", "original_year",
    "release_group_title", "releasegroupdisambig", "script", "style", "year",
    "comments", "lyrics", "bpm", "initial_key", "isrc", "media", "grouping", "title",
})

# Album-level identity columns the conflict policy (section 21/22) applies
# to -- correct schema, not the invented mb_artistid.
IDENTITY_FIELDS = ("mb_releasegroupid", "mb_albumid", "mb_albumartistid")

# Existing production caller (_apply_genre_to_album in app.py) has always
# sent the singular {"genre": ...}; the real column is plural "genres".
_METADATA_FIELD_ALIASES = {"genre": "genres"}


def _normalize_metadata_fields(raw: Dict[str, Any], allowlist: frozenset) -> Tuple[Dict[str, Any], List[str]]:
    """Split caller-supplied field names into (allowed, rejected).
    Nothing outside `allowlist` is ever returned in the allowed dict."""
    allowed: Dict[str, Any] = {}
    rejected: List[str] = []
    for k, v in (raw or {}).items():
        real_key = _METADATA_FIELD_ALIASES.get(k, k)
        if real_key in allowlist:
            allowed[real_key] = v
        else:
            rejected.append(k)
    return allowed, rejected


def create_album_metadata_plan(
    store: TransactionStore,
    payload: Dict[str, Any],
    *,
    music_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Create non-mutating preview plan for album metadata and tag updates.

    Wave 24 final review sections 19-23/30: unknown/disallowed fields are
    rejected here, before a transaction is even created, and the stored
    plan metadata carries only the allowlisted, diffed columns (never the
    caller's original raw dict) so Apply has nothing arbitrary to
    interpolate into SQL; the client-supplied force_identity_override is
    no longer honored; and each tag-mutated file's before-state (identity
    stat + current tag values) is captured for TOCTOU/rollback. A
    `force_write_tags` flag also plans a real Beets Item.write() resync
    (current DB values -> file tags) for every item, independent of
    whether any field actually differs -- the real semantics behind
    "rewrite tags to match the database" (section 28/30), not a diff-only
    no-op.
    """
    try:
        aid = int(payload.get("album_id") or 0)
    except Exception:
        aid = 0
    if aid <= 0:
        return {"ok": False, "error": "album_id required", "code": "album_metadata_invalid_payload"}

    force_write_tags = bool(payload.get("force_write_tags"))
    raw_updates = payload.get("updates") or {}
    raw_item_updates = payload.get("item_updates") or {}

    updates, rejected = _normalize_metadata_fields(raw_updates, ALBUM_METADATA_FIELDS)
    if rejected:
        return {"ok": False, "error": f"Unknown/disallowed album metadata field(s): {sorted(rejected)}", "code": "album_metadata_field_not_allowed"}

    lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")
    if not lib_db or not Path(lib_db).exists():
        return {"ok": False, "error": "Beets library database not found", "code": "album_metadata_db_not_found"}
    allowed_roots = music_allowed_roots or [str(os.environ.get("MUSIC_ROOT", "/music"))]

    resource_keys = [f"album:{aid}"]
    con = sqlite3.connect(lib_db, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        arow = con.execute("SELECT * FROM albums WHERE id=?", (aid,)).fetchone()
        if not arow:
            return {"ok": False, "error": f"Album {aid} not found", "code": "album_metadata_album_not_found"}

        for id_field in IDENTITY_FIELDS:
            if id_field in updates and updates[id_field] and id_field in arow.keys():
                existing_val = _s(arow[id_field] or "").strip()
                new_val = _s(updates[id_field]).strip()
                if existing_val and existing_val != new_val:
                    # Section 21/22: no payload field can bypass this --
                    # a conflict always requires review.
                    return {
                        "ok": False,
                        "error": f"Identity conflict on {id_field}: existing '{existing_val}' != new '{new_val}'",
                        "code": "album_metadata_identity_conflict",
                    }

        album_diff = {}
        for col, new_val in updates.items():
            if col in arow.keys():
                old_val = _s(arow[col] or "")
                if old_val != _s(new_val):
                    album_diff[col] = {"before": old_val, "after": _s(new_val)}

        item_rows = con.execute("SELECT * FROM items WHERE album_id=? ORDER BY disc, track, id", (aid,)).fetchall()
        item_diffs = []
        for irow in item_rows:
            iid = int(irow["id"])
            ipath = irow["path"].decode("utf-8", "replace") if isinstance(irow["path"], bytes) else str(irow["path"])
            # Wave 24 final review round 3, section 11: same relative-path
            # normalization as the relocation/artwork families above.
            ipath = str(_resolve_db_path(ipath, allowed_roots))
            raw_i_up = raw_item_updates.get(str(iid)) or raw_item_updates.get(iid) or {}
            i_up, rejected_item = _normalize_metadata_fields(raw_i_up, ITEM_METADATA_FIELDS)
            if rejected_item:
                return {"ok": False, "error": f"Unknown/disallowed item metadata field(s) for item {iid}: {sorted(rejected_item)}", "code": "album_metadata_field_not_allowed"}

            merged_i_up = dict(i_up)
            for k in ("mb_albumid", "mb_releasegroupid", "album", "albumartist", "year", "genres"):
                if k in updates and k not in merged_i_up:
                    merged_i_up[k] = updates[k]

            idiff = {}
            for col, new_val in merged_i_up.items():
                if col in irow.keys():
                    old_val = _s(irow[col] or "")
                    if old_val != _s(new_val):
                        idiff[col] = {"before": old_val, "after": _s(new_val)}

            if idiff or force_write_tags:
                # Section 23: capture the media file's before-state
                # (identity stat + current tag values for the fields being
                # touched) so Apply can TOCTOU-revalidate and Rollback can
                # truthfully restore tags, not just DB rows.
                capture_fields = set(idiff.keys()) | (ITEM_METADATA_FIELDS if force_write_tags else set())
                before_state = _capture_media_tag_state(Path(ipath), capture_fields)
                item_diffs.append({
                    "item_id": iid, "path": ipath, "diff": idiff,
                    "force_write": force_write_tags,
                    "file_before_stat": before_state.get("stat"),
                    "before_tags": before_state.get("tags") or {},
                })
    finally:
        con.close()

    summary_text = f"Album metadata update: album {aid} ({len(album_diff)} album fields, {len(item_diffs)} items updated)"
    tx = store.create(
        operation_type="Album Metadata Repair",
        status="Preview",
        summary=summary_text,
        metadata={
            "mutation_family": "album_metadata_repair_v1",
            "album_id": aid,
            "album_diff": album_diff,
            "item_diffs": item_diffs,
            "force_write_tags": force_write_tags,
            "resource_keys": resource_keys,
            "allowed_roots": allowed_roots,
            "created_at": _now(),
        },
    )
    return {
        "ok": True,
        "operation_id": tx["id"],
        "album_id": aid,
        "album_fields_changed": len(album_diff),
        "items_changed": len(item_diffs),
    }


def execute_album_metadata_apply(
    store: TransactionStore,
    operation_id: str,
    *,
    music_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute album metadata apply on DB and media tags with Stage 3
    verification.

    Wave 24 final review sections 23-28: every SQL column identifier is
    re-asserted against the allowlist immediately before use (defense in
    depth on top of the Plan-time filter); each tag-mutated file's
    identity is TOCTOU-revalidated right before writing; a tag write is
    never silently swallowed -- failure past the point of DB mutation
    triggers an automatic best-effort rollback (Recovery Required if that
    itself cannot fully undo it); tags are written by asking the real
    Beets Item.write() to resync from the just-committed DB row (reusing
    Beets rather than a duplicate hand-rolled writer), falling back to a
    field-scoped writer only when native Beets is unavailable; and Stage 3
    re-reads both the item DB rows and the actual on-disk tags.
    """
    if not _TRANSACTION_ID_RE.match(operation_id):
        return {"ok": False, "error": "Invalid transaction ID format", "code": "album_metadata_invalid_id"}

    with _get_apply_lock(operation_id):
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": f"Transaction {operation_id} not found", "code": "album_metadata_not_found"}

        meta = tx.get("metadata") or {}
        if meta.get("mutation_family") != "album_metadata_repair_v1":
            return {"ok": False, "error": "Transaction is not an album_metadata_repair_v1 operation", "code": "album_metadata_family_mismatch"}

        if tx.get("status") == "Completed":
            return {"ok": True, "operation_id": operation_id, "status": "Completed", "mutated": True}

        resource_keys = meta.get("resource_keys") or []
        lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")
        if not lib_db or not Path(lib_db).exists():
            return {"ok": False, "error": "Beets library database not found", "code": "album_metadata_db_not_found"}
        allowed_roots = music_allowed_roots or meta.get("allowed_roots") or [str(os.environ.get("MUSIC_ROOT", "/music"))]
        music_root = allowed_roots[0]

        def _fail(msg: str, code: str, *, attempt_recovery: bool = False) -> Dict[str, Any]:
            if attempt_recovery:
                rb = rollback_album_metadata(store, operation_id, db_path=lib_db)
                store.update(operation_id, logs=[f"Apply failed: {msg}", f"Automatic recovery attempt: {rb.get('status')}"])
                if rb.get("status") == "Rolled Back":
                    return {"ok": False, "error": msg, "code": code, "mutated": False, "partial_mutation": False, "rollback_available": False, "status": "Rolled Back"}
                store.update(operation_id, status="Recovery Required")
                return {"ok": False, "error": msg, "code": code, "mutated": True, "partial_mutation": True, "rollback_available": False, "status": "Recovery Required"}
            store.update(operation_id, status="Failed", logs=[f"Apply failed: {msg}"])
            return {"ok": False, "error": msg, "code": code, "mutated": True, "partial_mutation": True, "rollback_available": True}

        with _lock_resources(resource_keys):
            store.update(operation_id, status="Running", metadata={**meta, "mutation_started": True})
            aid = int(meta["album_id"])
            album_diff = meta.get("album_diff") or {}
            item_diffs = meta.get("item_diffs") or []

            # Defense in depth: re-assert every column name against the
            # allowlist immediately before it can reach a SQL statement,
            # even though Plan already filtered it.
            for k in album_diff.keys():
                if k not in ALBUM_METADATA_FIELDS:
                    return _fail(f"Refusing disallowed album field at Apply: {k}", "album_metadata_field_not_allowed")
            for idiff in item_diffs:
                for k in idiff["diff"].keys():
                    if k not in ITEM_METADATA_FIELDS:
                        return _fail(f"Refusing disallowed item field at Apply: {k}", "album_metadata_field_not_allowed")

            # TOCTOU: revalidate every tag-mutated file's identity before
            # touching anything.
            for idiff in item_diffs:
                before = idiff.get("file_before_stat")
                if not before:
                    continue
                p = Path(idiff["path"])
                if not p.exists():
                    return _fail(f"Media file missing before metadata apply: {p}", "album_metadata_toctou_mismatch")
                st = p.stat()
                if (st.st_dev != before.get("dev") or st.st_ino != before.get("ino")
                        or st.st_size != before.get("size") or st.st_mtime_ns != before.get("mtime_ns")):
                    return _fail(f"Media file changed since plan: {p}", "album_metadata_toctou_mismatch")

            con = sqlite3.connect(lib_db, timeout=10)
            db_mutated = False
            try:
                cur = con.cursor()
                if album_diff:
                    cols = [f"{k}=?" for k in album_diff.keys()]
                    vals = [album_diff[k]["after"] for k in album_diff.keys()] + [aid]
                    cur.execute(f"UPDATE albums SET {', '.join(cols)} WHERE id=?", vals)
                    if cur.rowcount != 1:
                        con.rollback()
                        con.close()
                        return _fail(f"Expected to update 1 album row, affected {cur.rowcount}", "album_metadata_rowcount_mismatch")

                for idiff in item_diffs:
                    if not idiff["diff"]:
                        continue
                    iid = int(idiff["item_id"])
                    diff_dict = idiff["diff"]
                    icols = [f"{k}=?" for k in diff_dict.keys()]
                    ivals = [diff_dict[k]["after"] for k in diff_dict.keys()] + [iid]
                    cur.execute(f"UPDATE items SET {', '.join(icols)} WHERE id=?", ivals)
                    if cur.rowcount != 1:
                        con.rollback()
                        con.close()
                        return _fail(f"Expected to update 1 item row for id {iid}, affected {cur.rowcount}", "album_metadata_rowcount_mismatch")

                con.commit()
                db_mutated = True
            finally:
                con.close()

            store.update(operation_id, metadata={**store.get(operation_id).get("metadata", {}), "db_mutated": db_mutated})

            # Section 24/28: write tags for every item that has a real diff
            # or requested a forced resync. A tag write failure here is a
            # transaction failure, never a logged-and-ignored best effort.
            tags_written: List[int] = []
            for idiff in item_diffs:
                if not idiff["diff"] and not idiff.get("force_write"):
                    continue
                iid = int(idiff["item_id"])
                ipath = Path(idiff["path"])
                if not ipath.exists() or not ipath.is_file():
                    return _fail(f"Media file missing for tag write: {ipath}", "album_metadata_tag_write_failed", attempt_recovery=True)

                result = native_beets_write_item_tags(lib_db, music_root, iid)
                if not result.get("ok"):
                    # Degraded fallback: field-scoped writer for the diffed
                    # fields only (covers dev/test environments without the
                    # full Beets package -- never used in the production
                    # engine container, which always has it).
                    fields = {k: v["after"] for k, v in idiff["diff"].items()}
                    fallback = _write_media_tag_fields(ipath, fields) if fields else {"ok": False, "error": result.get("error"), "code": result.get("code")}
                    if not fallback.get("ok"):
                        return _fail(f"Tag write failed for item {iid}: {fallback.get('error') or result.get('error')}", fallback.get("code") or result.get("code") or "album_metadata_tag_write_failed", attempt_recovery=True)
                tags_written.append(iid)
                store.update(operation_id, metadata={**store.get(operation_id).get("metadata", {}), "tags_written": tags_written})

            # Stage 3 verification: re-read DB rows AND actual on-disk tags.
            con = sqlite3.connect(lib_db, timeout=10)
            con.row_factory = sqlite3.Row
            try:
                arow = con.execute("SELECT * FROM albums WHERE id=?", (aid,)).fetchone()
                for k, v in album_diff.items():
                    if arow and _s(arow[k] or "") != v["after"]:
                        return _fail(f"Post-write verification failed for album field {k}", "album_metadata_verification_failed", attempt_recovery=True)
                for idiff in item_diffs:
                    if not idiff["diff"]:
                        continue
                    iid = int(idiff["item_id"])
                    irow = con.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
                    if not irow:
                        return _fail(f"Post-write verification failed: item {iid} missing from DB", "album_metadata_verification_failed", attempt_recovery=True)
                    for k, v in idiff["diff"].items():
                        if k in irow.keys() and _s(irow[k] or "") != v["after"]:
                            return _fail(f"Post-write verification failed for item {iid} field {k}", "album_metadata_verification_failed", attempt_recovery=True)
            finally:
                con.close()

            for idiff in item_diffs:
                if not idiff["diff"] or int(idiff["item_id"]) not in tags_written:
                    continue
                capture = _capture_media_tag_state(Path(idiff["path"]), idiff["diff"].keys())
                for k, v in idiff["diff"].items():
                    actual = _s(capture.get("tags", {}).get(k, ""))
                    if actual != v["after"]:
                        return _fail(f"Post-write verification failed: on-disk tag {k} does not match for item {idiff['item_id']}", "album_metadata_verification_failed", attempt_recovery=True)

            store.update(operation_id, status="Completed", metadata={
                **store.get(operation_id).get("metadata", {}),
                "db_mutated": db_mutated,
                "tags_written": tags_written,
                "completed_at": _now(),
            })
            return {"ok": True, "operation_id": operation_id, "status": "Completed", "mutated": True, "tags_written_count": len(tags_written)}


def rollback_album_metadata(
    store: TransactionStore,
    operation_id: str,
    *,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Roll back an album_metadata_repair_v1 transaction: restore file
    tags FIRST (verified), then DB rows (verified) -- section 26/27:
    rollback must be truthful about what it actually restored, never
    report Rolled Back when a planned tag restore failed.
    """
    if not _TRANSACTION_ID_RE.match(operation_id):
        return {"ok": False, "error": "Invalid transaction ID format", "code": "album_metadata_invalid_id"}

    with _get_apply_lock(operation_id):
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": f"Transaction {operation_id} not found", "code": "album_metadata_not_found"}

        meta = tx.get("metadata") or {}
        if meta.get("mutation_family") != "album_metadata_repair_v1":
            return {"ok": False, "error": "Transaction is not an album_metadata_repair_v1 operation", "code": "album_metadata_family_mismatch"}

        if tx.get("status") == "Rolled Back":
            return {"ok": False, "error": "Transaction is already rolled back.", "code": "album_metadata_already_rolled_back"}

        resource_keys = meta.get("resource_keys") or []
        lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")
        if not lib_db or not Path(lib_db).exists():
            return {"ok": False, "error": "Beets library database not found", "code": "album_metadata_db_not_found"}

        with _lock_resources(resource_keys):
            aid = int(meta["album_id"])
            album_diff = meta.get("album_diff") or {}
            item_diffs = meta.get("item_diffs") or []
            tags_written = set(meta.get("tags_written") or [])

            # 1. Restore tags first, from the captured before_tags.
            tags_restored = 0
            tags_failed = 0
            for idiff in item_diffs:
                iid = int(idiff["item_id"])
                if not idiff["diff"] or iid not in tags_written:
                    continue
                # None is a real, meaningful captured value here (the tag
                # was unset before Apply) -- it must be written back, not
                # silently dropped, or a field that started empty would
                # never be restored and rollback would falsely report
                # success.
                captured = idiff.get("before_tags", {})
                before_tags = {k: captured.get(k) for k in idiff["diff"].keys()}
                p = Path(idiff["path"])
                if not p.exists():
                    tags_failed += 1
                    continue
                tw = _write_media_tag_fields(p, before_tags)
                if not tw.get("ok"):
                    tags_failed += 1
                    continue
                capture = _capture_media_tag_state(p, before_tags.keys())
                if all(_s(capture.get("tags", {}).get(k, "")) == _s(v) for k, v in before_tags.items()):
                    tags_restored += 1
                else:
                    tags_failed += 1

            # 2. Restore DB rows only after tag restoration has been
            # attempted -- rollback truthfulness requires both, and a
            # failed tag restore must not be hidden behind a DB-only
            # "Rolled Back".
            con = sqlite3.connect(lib_db, timeout=10)
            db_restored = True
            try:
                cur = con.cursor()
                if album_diff:
                    cols = [f"{k}=?" for k in album_diff.keys() if k in ALBUM_METADATA_FIELDS]
                    vals = [album_diff[k]["before"] for k in album_diff.keys() if k in ALBUM_METADATA_FIELDS] + [aid]
                    if cols:
                        cur.execute(f"UPDATE albums SET {', '.join(cols)} WHERE id=?", vals)
                        if cur.rowcount != 1:
                            db_restored = False

                for idiff in item_diffs:
                    if not idiff["diff"]:
                        continue
                    iid = int(idiff["item_id"])
                    diff_dict = idiff["diff"]
                    icols = [f"{k}=?" for k in diff_dict.keys() if k in ITEM_METADATA_FIELDS]
                    ivals = [diff_dict[k]["before"] for k in diff_dict.keys() if k in ITEM_METADATA_FIELDS] + [iid]
                    if icols:
                        cur.execute(f"UPDATE items SET {', '.join(icols)} WHERE id=?", ivals)
                        if cur.rowcount != 1:
                            db_restored = False

                con.commit()
            finally:
                con.close()

            ok = db_restored and tags_failed == 0
            any_progress = db_restored or tags_restored > 0
            final_status = "Rolled Back" if ok else ("Partially Rolled Back" if any_progress else "Failed")
            store.update(operation_id, status=final_status, metadata={
                **meta, "rollback_available": False, "rolled_back_at": _now(),
                "tags_restored_count": tags_restored, "tags_failed_count": tags_failed,
            })
            return {"ok": ok, "operation_id": operation_id, "status": final_status, "tags_restored": tags_restored, "tags_failed": tags_failed}




# -- item_metadata_repair_v1 ---------------------------------------------------

def create_item_metadata_plan(
    store: TransactionStore,
    payload: Dict[str, Any],
    *,
    music_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    payload = payload or {}
    try:
        item_id = int(payload.get("item_id") or 0)
    except Exception:
        item_id = 0
    if item_id <= 0:
        return {"ok": False, "error": "item_id required", "code": "item_metadata_invalid_payload"}
    force_write_tags = bool(payload.get("force_write_tags"))
    updates, rejected = _normalize_metadata_fields(payload.get("updates") or {}, ITEM_METADATA_FIELDS)
    if rejected:
        return {"ok": False, "error": f"Unknown/disallowed item metadata field(s): {sorted(rejected)}", "code": "item_metadata_field_not_allowed"}
    if not updates and not force_write_tags:
        return {"ok": True, "operation_id": None, "item_fields_changed": 0}

    lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")
    if not lib_db or not Path(lib_db).exists():
        return {"ok": False, "error": "Beets library database not found", "code": "item_metadata_db_not_found"}
    allowed_roots = music_allowed_roots or [str(os.environ.get("MUSIC_ROOT", "/music"))]
    con = sqlite3.connect(lib_db, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"Item {item_id} not found", "code": "item_metadata_item_not_found"}
        path_str = _s(row["path"] or "") if "path" in row.keys() else ""
        if not path_str:
            return {"ok": False, "error": f"Item {item_id} has no path", "code": "item_metadata_missing_path"}
        item_path = _resolve_db_path(path_str, allowed_roots)
        if not any(_path_under(item_path, Path(root)) for root in allowed_roots):
            return {"ok": False, "error": "Item path is outside allowed music roots", "code": "item_metadata_path_out_of_root"}
        if any(_path_has_symlink_under(item_path, Path(root)) for root in allowed_roots):
            return {"ok": False, "error": "Symlink rejected in item path", "code": "item_metadata_symlink_rejected"}
        diff: Dict[str, Dict[str, str]] = {}
        for key, new_value in updates.items():
            if key in row.keys():
                old_value = _s(row[key] or "")
                if old_value != _s(new_value):
                    diff[key] = {"before": old_value, "after": _s(new_value)}
    finally:
        con.close()

    if not diff and not force_write_tags:
        return {"ok": True, "operation_id": None, "item_fields_changed": 0}
    capture_fields = set(diff.keys()) | (ITEM_METADATA_FIELDS if force_write_tags else set())
    before_state = _capture_media_tag_state(item_path, capture_fields)
    if force_write_tags and not before_state.get("exists"):
        return {"ok": False, "error": f"Media file missing for item {item_id}", "code": "item_metadata_media_missing"}
    tx = store.create(
        operation_type="Metadata Update",
        status="Preview",
        summary=f"Item metadata update: item {item_id} ({len(diff)} field(s))",
        rollback_available=bool(diff),
        rollback_reason="Captured previous item metadata and tag values before apply." if diff else "No metadata diff captured.",
        metadata={
            "mutation_family": "item_metadata_repair_v1",
            "item_id": item_id,
            "item_diff": diff,
            "item_path": str(item_path),
            "force_write_tags": force_write_tags,
            "file_before_stat": before_state.get("stat"),
            "before_tags": before_state.get("tags") or {},
            "allowed_roots": allowed_roots,
            "resource_keys": [f"item:{item_id}"],
            "created_at": _now(),
        },
    )
    return {"ok": True, "operation_id": tx["id"], "item_id": item_id, "item_fields_changed": len(diff)}


def execute_item_metadata_apply(
    store: TransactionStore,
    operation_id: str,
    *,
    music_allowed_roots: Optional[List[str]] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    if not _TRANSACTION_ID_RE.match(operation_id):
        return {"ok": False, "error": "Invalid transaction ID format", "code": "item_metadata_invalid_id"}
    with _get_apply_lock(operation_id):
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": f"Transaction {operation_id} not found", "code": "item_metadata_not_found"}
        meta = tx.get("metadata") or {}
        if meta.get("mutation_family") != "item_metadata_repair_v1":
            return {"ok": False, "error": "Transaction is not an item_metadata_repair_v1 operation", "code": "item_metadata_family_mismatch"}
        if tx.get("status") == "Completed":
            return {"ok": True, "operation_id": operation_id, "status": "Completed", "mutated": True}
        lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")
        if not lib_db or not Path(lib_db).exists():
            return {"ok": False, "error": "Beets library database not found", "code": "item_metadata_db_not_found"}
        item_id = int(meta.get("item_id") or 0)
        diff = meta.get("item_diff") or {}
        item_path = Path(meta.get("item_path") or "")
        allowed_roots = music_allowed_roots or meta.get("allowed_roots") or [str(os.environ.get("MUSIC_ROOT", "/music"))]
        music_root = allowed_roots[0]

        def _fail(msg: str, code: str, *, recover: bool = False) -> Dict[str, Any]:
            if recover:
                rb = rollback_item_metadata(store, operation_id, db_path=lib_db)
                if rb.get("status") == "Rolled Back":
                    return {"ok": False, "error": msg, "code": code, "mutated": False, "status": "Rolled Back"}
                store.update(operation_id, status="Recovery Required", logs=[f"Apply failed: {msg}"])
                return {"ok": False, "error": msg, "code": code, "mutated": True, "status": "Recovery Required"}
            store.update(operation_id, status="Failed", logs=[f"Apply failed: {msg}"])
            return {"ok": False, "error": msg, "code": code, "mutated": False, "status": "Failed"}

        with _lock_resources(meta.get("resource_keys") or [f"item:{item_id}"]):
            for key in diff.keys():
                if key not in ITEM_METADATA_FIELDS:
                    return _fail(f"Refusing disallowed item field at Apply: {key}", "item_metadata_field_not_allowed")
            before = meta.get("file_before_stat")
            if before:
                if not item_path.exists():
                    return _fail(f"Media file missing before item metadata apply: {item_path}", "item_metadata_toctou_mismatch")
                st = item_path.stat()
                if (st.st_dev != before.get("dev") or st.st_ino != before.get("ino")
                        or st.st_size != before.get("size") or st.st_mtime_ns != before.get("mtime_ns")):
                    return _fail(f"Media file changed since plan: {item_path}", "item_metadata_toctou_mismatch")
            store.update(operation_id, status="Running", metadata={**meta, "mutation_started": True})
            db_mutated = False
            if diff:
                con = sqlite3.connect(lib_db, timeout=10)
                try:
                    cols = [f"{key}=?" for key in diff.keys()]
                    vals = [diff[key]["after"] for key in diff.keys()] + [item_id]
                    cur = con.execute(f"UPDATE items SET {', '.join(cols)} WHERE id=?", vals)
                    if cur.rowcount != 1:
                        con.rollback()
                        return _fail(f"Expected to update 1 item row for id {item_id}, affected {cur.rowcount}", "item_metadata_rowcount_mismatch")
                    con.commit()
                    db_mutated = True
                finally:
                    con.close()
            store.update(operation_id, metadata={**store.get(operation_id).get("metadata", {}), "db_mutated": db_mutated})
            tags_written = False
            if diff or meta.get("force_write_tags"):
                result = native_beets_write_item_tags(lib_db, music_root, item_id)
                if not result.get("ok"):
                    fields = {key: value["after"] for key, value in diff.items()}
                    fallback = _write_media_tag_fields(item_path, fields) if fields else result
                    if not fallback.get("ok"):
                        return _fail(
                            f"Tag write failed for item {item_id}: {fallback.get('error') or result.get('error')}",
                            fallback.get("code") or result.get("code") or "item_metadata_tag_write_failed",
                            recover=True,
                        )
                tags_written = True
                store.update(operation_id, metadata={**store.get(operation_id).get("metadata", {}), "tags_written": [item_id]})
            con = sqlite3.connect(lib_db, timeout=10)
            con.row_factory = sqlite3.Row
            try:
                row = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
                if not row:
                    return _fail(f"Post-write verification failed: item {item_id} missing from DB", "item_metadata_verification_failed", recover=True)
                for key, value in diff.items():
                    if key in row.keys() and _s(row[key] or "") != value["after"]:
                        return _fail(f"Post-write verification failed for item {item_id} field {key}", "item_metadata_verification_failed", recover=True)
            finally:
                con.close()
            if tags_written and diff:
                capture = _capture_media_tag_state(item_path, diff.keys())
                for key, value in diff.items():
                    actual = _s(capture.get("tags", {}).get(key, ""))
                    if actual != value["after"]:
                        return _fail(f"Post-write verification failed: on-disk tag {key} does not match for item {item_id}", "item_metadata_verification_failed", recover=True)
            store.update(operation_id, status="Completed", metadata={**store.get(operation_id).get("metadata", {}), "completed_at": _now()})
            return {"ok": True, "operation_id": operation_id, "status": "Completed", "mutated": True, "tags_written_count": 1 if tags_written else 0}


def rollback_item_metadata(
    store: TransactionStore,
    operation_id: str,
    *,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    if not _TRANSACTION_ID_RE.match(operation_id):
        return {"ok": False, "error": "Invalid transaction ID format", "code": "item_metadata_invalid_id"}
    with _get_apply_lock(operation_id):
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": f"Transaction {operation_id} not found", "code": "item_metadata_not_found"}
        meta = tx.get("metadata") or {}
        if meta.get("mutation_family") != "item_metadata_repair_v1":
            return {"ok": False, "error": "Transaction is not an item_metadata_repair_v1 operation", "code": "item_metadata_family_mismatch"}
        lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")
        if not lib_db or not Path(lib_db).exists():
            return {"ok": False, "error": "Beets library database not found", "code": "item_metadata_db_not_found"}
        item_id = int(meta.get("item_id") or 0)
        diff = meta.get("item_diff") or {}
        item_path = Path(meta.get("item_path") or "")
        tags_failed = 0
        tags_restored = 0
        if diff and item_id in set(meta.get("tags_written") or []):
            before_tags = {key: (meta.get("before_tags") or {}).get(key) for key in diff.keys()}
            tw = _write_media_tag_fields(item_path, before_tags)
            if tw.get("ok"):
                tags_restored = 1
            else:
                tags_failed = 1
        db_restored = True
        if diff:
            con = sqlite3.connect(lib_db, timeout=10)
            try:
                cols = [f"{key}=?" for key in diff.keys() if key in ITEM_METADATA_FIELDS]
                vals = [diff[key]["before"] for key in diff.keys() if key in ITEM_METADATA_FIELDS] + [item_id]
                if cols:
                    cur = con.execute(f"UPDATE items SET {', '.join(cols)} WHERE id=?", vals)
                    db_restored = cur.rowcount == 1
                con.commit()
            finally:
                con.close()
        ok = db_restored and tags_failed == 0
        final_status = "Rolled Back" if ok else ("Partially Rolled Back" if db_restored or tags_restored else "Failed")
        store.update(operation_id, status=final_status, metadata={**meta, "rollback_available": False, "rolled_back_at": _now(), "tags_restored_count": tags_restored, "tags_failed_count": tags_failed})
        return {"ok": ok, "operation_id": operation_id, "status": final_status, "tags_restored": tags_restored, "tags_failed": tags_failed}


# -- genre_repair_v1 -----------------------------------------------------------

def _row_genre_value(row: sqlite3.Row) -> str:
    keys = row.keys()
    if "genres" in keys:
        return _s(row["genres"] or "")
    if "genre" in keys:
        return _s(row["genre"] or "")
    return ""


def _read_file_genre_tag(file_path: Path) -> Dict[str, Any]:
    """Read the on-disk genre tag for one media file. Mirrors
    _read_file_audio_tags's {"ok", value, "reason"} contract so a failed
    read is never confused with a genuinely blank tag."""
    path_str = str(file_path)
    try:
        exists = file_path.exists()
    except OSError as ex:
        LOG.warning("Genre tag read: stat failed for %s: %s", path_str, ex)
        return {"ok": False, "genre": None, "reason": "io_error"}
    if not exists:
        return {"ok": False, "genre": None, "reason": "not_found"}
    try:
        import mediafile
    except ImportError as ex:
        LOG.error("Genre tag read: mediafile package unavailable: %s", ex)
        return {"ok": False, "genre": None, "reason": "backend_unavailable"}
    try:
        mf = mediafile.MediaFile(file_path)
        genre = str(getattr(mf, "genre", "") or "")
    except mediafile.UnreadableFileError as ex:
        LOG.warning("Genre tag read: unreadable file %s: %s", path_str, ex)
        return {"ok": False, "genre": None, "reason": "unreadable"}
    except OSError as ex:
        LOG.warning("Genre tag read: io error for %s: %s", path_str, ex)
        return {"ok": False, "genre": None, "reason": "io_error"}
    except Exception as ex:
        LOG.error("Genre tag read: unexpected error for %s: %s", path_str, ex)
        return {"ok": False, "genre": None, "reason": "unknown_error"}
    return {"ok": True, "genre": genre, "reason": None}


def _write_file_genre_tag(file_path: Path, genre: str) -> Dict[str, Any]:
    """Write the genre tag to disk and verify by an independent read-back
    (not the writer's own in-memory state) -- mirrors _write_file_audio_tags.
    A write only counts as successful once the exact requested value is
    independently read back from the file."""
    path_str = str(file_path)
    try:
        import mediafile
    except ImportError as ex:
        LOG.error("Genre tag write: mediafile package unavailable: %s", ex)
        return {"ok": False, "reason": "backend_unavailable"}
    try:
        mf = mediafile.MediaFile(file_path)
        mf.genre = genre
        mf.save()
    except mediafile.UnreadableFileError as ex:
        LOG.warning("Genre tag write: unreadable file %s: %s", path_str, ex)
        return {"ok": False, "reason": "unreadable"}
    except OSError as ex:
        LOG.warning("Genre tag write: io error for %s: %s", path_str, ex)
        return {"ok": False, "reason": "io_error"}
    except Exception as ex:
        LOG.error("Genre tag write: unexpected error for %s: %s", path_str, ex)
        return {"ok": False, "reason": "unknown_error"}
    verify = _read_file_genre_tag(file_path)
    if not verify.get("ok") or _s(verify.get("genre")) != _s(genre):
        return {"ok": False, "reason": "verify_mismatch"}
    return {"ok": True, "reason": None}


def _genre_column(con: sqlite3.Connection, table: str) -> str:
    cols = [str(row[1]) for row in con.execute(f"PRAGMA table_info({table})").fetchall()]
    if "genres" in cols:
        return "genres"
    if "genre" in cols:
        return "genre"
    return ""


def create_genre_repair_plan(
    store: TransactionStore,
    payload: Dict[str, Any],
    *,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    payload = payload or {}
    try:
        album_id = int(payload.get("album_id") or 0)
    except Exception:
        album_id = 0
    if album_id <= 0:
        return {"ok": False, "error": "album_id required", "code": "genre_repair_invalid_payload"}
    lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")
    if not lib_db or not Path(lib_db).exists():
        return {"ok": False, "error": "Beets library database not found", "code": "genre_repair_db_not_found"}
    con = sqlite3.connect(lib_db, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        album = con.execute("SELECT * FROM albums WHERE id=?", (album_id,)).fetchone()
        if not album:
            return {"ok": False, "error": f"Album {album_id} not found", "code": "genre_repair_album_not_found"}
        items = con.execute("SELECT * FROM items WHERE album_id=? ORDER BY disc, track, id", (album_id,)).fetchall()
    finally:
        con.close()
    before_items: List[Dict[str, Any]] = []
    for row in items:
        path_str = _s(row["path"]) if "path" in row.keys() else ""
        if not path_str:
            return {"ok": False, "error": f"Item {row['id']} has no media file path", "code": "genre_repair_tag_baseline_unavailable"}
        p = Path(path_str)
        if not p.exists():
            return {"ok": False, "error": f"Media file {path_str} does not exist", "code": "genre_repair_tag_baseline_unavailable"}
        try:
            st = p.stat()
            tag_read = _read_file_genre_tag(p)
            if not tag_read.get("ok"):
                return {"ok": False, "error": f"On-disk genre tag baseline could not be read for {path_str}", "code": "genre_repair_tag_baseline_unavailable"}
            entry: Dict[str, Any] = {
                "item_id": int(row["id"]),
                "genre": _row_genre_value(row),
                "path": path_str,
                "stat": {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns},
                "tag_genre": tag_read.get("genre"),
                "tag_read_ok": True,
            }
            before_items.append(entry)
        except Exception as ex:
            LOG.warning("Genre repair plan: could not capture tag baseline for %s: %s", path_str, ex)
            return {"ok": False, "error": f"On-disk genre tag baseline could not be captured for {path_str}", "code": "genre_repair_tag_baseline_unavailable"}
    before_album = _row_genre_value(album)
    tx = store.create(
        operation_type="Metadata Update",
        status="Preview",
        summary=f"Repair genres for album {album_id} using Beets lastgenre",
        rollback_available=True,
        rollback_reason="Captured prior Beets genre fields before lastgenre apply.",
        metadata={
            "mutation_family": "genre_repair_v1",
            "album_id": album_id,
            "force": bool(payload.get("force")),
            "timeout": float(payload.get("timeout") or 180.0),
            "before_album_genre": before_album,
            "before_item_genres": before_items,
            "resource_keys": [f"album:{album_id}"],
            "created_at": _now(),
        },
    )
    return {"ok": True, "operation_id": tx["id"], "album_id": album_id, "item_count": len(before_items)}


def _restore_genre_and_tag_state(
    lib_db: str, album_id: int, before_album: str, before_items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Restore DB genre columns AND on-disk genre tags to their captured
    Plan-time values, independently verifying each restoration rather than
    assuming a write succeeded. Shared by rollback_genre_repair() and the
    automatic compensating rollback inside execute_genre_repair_apply() when
    a failed lastgenre run turns out to have partially mutated state.

    Returns {"status": "Rolled Back" | "Recovery Required", "db_restored":
    bool, "unrestored_items": [...]}. Only "Rolled Back" is a promise that
    both DB and file-tag state have been proven restored.
    """
    unrestored_items: List[Dict[str, Any]] = []
    db_restored = True
    con = sqlite3.connect(lib_db, timeout=10)
    try:
        cur = con.cursor()
        album_col = _genre_column(con, "albums")
        item_col = _genre_column(con, "items")
        if album_col:
            cur.execute(f"UPDATE albums SET {album_col}=? WHERE id=?", (before_album, album_id))
        for row in before_items:
            if item_col:
                cur.execute(f"UPDATE items SET {item_col}=? WHERE id=?", (_s(row.get("genre") or ""), int(row.get("item_id") or 0)))
        con.commit()
    except Exception as ex:
        LOG.error("Genre repair rollback: DB restore failed for album %s: %s", album_id, ex)
        db_restored = False
    finally:
        con.close()

    if db_restored:
        con = sqlite3.connect(lib_db, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            album_row = con.execute("SELECT * FROM albums WHERE id=?", (album_id,)).fetchone()
            if album_row is None or _row_genre_value(album_row) != before_album:
                db_restored = False
            item_rows = con.execute("SELECT * FROM items WHERE album_id=? ORDER BY disc, track, id", (album_id,)).fetchall()
            items_by_id = {int(r["id"]): _row_genre_value(r) for r in item_rows}
            for row in before_items:
                iid = int(row.get("item_id") or 0)
                expected_genre = _s(row.get("genre") or "")
                actual_genre = items_by_id.get(iid)
                if actual_genre != expected_genre:
                    db_restored = False
                    LOG.error("Genre repair rollback: item %s DB genre verification failed (expected %r, got %r)", iid, expected_genre, actual_genre)
        except Exception as ex:
            LOG.error("Genre repair rollback: DB restore verification failed for album %s: %s", album_id, ex)
            db_restored = False
        finally:
            con.close()

    for row in before_items:
        iid = int(row.get("item_id") or 0)
        path_str = row.get("path") or ""
        if not path_str or not row.get("tag_read_ok"):
            unrestored_items.append({"item_id": iid, "path": path_str, "reason": "no_tag_baseline_captured"})
            continue
        write_res = _write_file_genre_tag(Path(path_str), _s(row.get("tag_genre")))
        if not write_res.get("ok"):
            unrestored_items.append({"item_id": iid, "path": path_str, "reason": write_res.get("reason")})
            continue
        tag_verify = _read_file_genre_tag(Path(path_str))
        if not tag_verify.get("ok") or _s(tag_verify.get("genre")) != _s(row.get("tag_genre")):
            unrestored_items.append({"item_id": iid, "path": path_str, "reason": "tag_restore_verification_failed"})

    status = "Rolled Back" if (db_restored and not unrestored_items) else "Recovery Required"
    return {"status": status, "db_restored": db_restored, "unrestored_items": unrestored_items}


def execute_genre_repair_apply(
    store: TransactionStore,
    operation_id: str,
    *,
    db_path: Optional[str] = None,
    run_beet_command_fn: Optional[Any] = None,
) -> Dict[str, Any]:
    if not _TRANSACTION_ID_RE.match(operation_id):
        return {"ok": False, "error": "Invalid transaction ID format", "code": "genre_repair_invalid_id"}
    if run_beet_command_fn is None:
        return {"ok": False, "error": "No beet command runner configured", "code": "genre_repair_no_runner"}
    with _get_apply_lock(operation_id):
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": f"Transaction {operation_id} not found", "code": "genre_repair_not_found"}
        meta = tx.get("metadata") or {}
        if meta.get("mutation_family") != "genre_repair_v1":
            return {"ok": False, "error": "Transaction is not a genre_repair_v1 operation", "code": "genre_repair_family_mismatch"}
        if tx.get("status") == "Completed":
            return {"ok": True, "operation_id": operation_id, "status": "Completed", "mutated": True, "genre_after": meta.get("genre_after")}
        album_id = int(meta.get("album_id") or 0)
        lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")
        if not lib_db or not Path(lib_db).exists():
            return {"ok": False, "error": "Beets library database not found", "code": "genre_repair_db_not_found"}
        before_album = _s(meta.get("before_album_genre") or "")
        before_items = meta.get("before_item_genres") or []
        with _lock_resources(meta.get("resource_keys") or [f"album:{album_id}"]):
            con = sqlite3.connect(lib_db, timeout=10)
            con.row_factory = sqlite3.Row
            try:
                album = con.execute("SELECT * FROM albums WHERE id=?", (album_id,)).fetchone()
                if not album:
                    return {"ok": False, "error": f"Album {album_id} no longer exists", "code": "genre_repair_album_not_found"}
            finally:
                con.close()
            store.update(operation_id, status="Running", metadata={**meta, "mutation_started": True})
            args = []
            if meta.get("force"):
                args.append("-f")
            args.append(f"album_id:{album_id}")
            result = run_beet_command_fn("lastgenre", args)
            beet_reported_ok = isinstance(result, dict) and result.get("ok") is True

            # A nonzero/failed native result does NOT prove nothing was
            # mutated -- lastgenre can write some items' tags/DB rows before
            # erroring out on a later one. Always re-read actual current DB
            # + on-disk tag state and diff it against the Plan snapshot,
            # regardless of the reported return code, instead of trusting
            # the return code to imply mutated=False.
            con = sqlite3.connect(lib_db, timeout=10)
            con.row_factory = sqlite3.Row
            try:
                album_after = con.execute("SELECT * FROM albums WHERE id=?", (album_id,)).fetchone()
                items_after = con.execute("SELECT * FROM items WHERE album_id=? ORDER BY disc, track, id", (album_id,)).fetchall()
            finally:
                con.close()
            genre_after = _row_genre_value(album_after) if album_after else ""
            items_after_by_id = {int(r["id"]): r for r in items_after}

            item_genres_after: List[Dict[str, Any]] = []
            # A list keyed by "item_id", not a dict keyed by the int item
            # id -- TransactionStore persists metadata as JSON, which would
            # silently stringify int dict keys on the next read, matching
            # the list shape item_genres_after already uses.
            tag_genres_after: List[Dict[str, Any]] = []
            mutated = genre_after != before_album
            for before in before_items:
                iid = int(before.get("item_id") or 0)
                row = items_after_by_id.get(iid)
                db_genre_after = _row_genre_value(row) if row is not None else before.get("genre", "")
                item_genres_after.append({"item_id": iid, "genre": db_genre_after})
                if db_genre_after != before.get("genre", ""):
                    mutated = True

                path_str = before.get("path") or ""
                tag_read_ok_after = False
                tag_genre_after = None
                if path_str:
                    tag_read = _read_file_genre_tag(Path(path_str))
                    tag_read_ok_after = bool(tag_read.get("ok"))
                    tag_genre_after = tag_read.get("genre")
                tag_genres_after.append({"item_id": iid, "path": path_str, "genre": tag_genre_after, "read_ok": tag_read_ok_after})
                if before.get("tag_read_ok"):
                    if tag_read_ok_after:
                        if _s(tag_genre_after) != _s(before.get("tag_genre")):
                            mutated = True
                    else:
                        # Was readable at Plan time, is not now -- cannot
                        # prove nothing changed; treat conservatively as a
                        # possible mutation rather than a confirmed no-op.
                        mutated = True

            if not beet_reported_ok:
                if not mutated:
                    store.update(operation_id, status="Failed", logs=["lastgenre apply failed"], metadata={
                        **store.get(operation_id).get("metadata", {}),
                        "genre_after": genre_after, "item_genres_after": item_genres_after,
                    })
                    return {
                        "ok": False,
                        "error": (result or {}).get("error") if isinstance(result, dict) else "lastgenre failed",
                        "code": "genre_repair_apply_failed", "mutated": False, "status": "Failed",
                    }
                # Partial mutation despite a failed/nonzero result: attempt
                # a verified compensating rollback of both DB and on-disk
                # tags before ever reporting a rolled-back state.
                restore_res = _restore_genre_and_tag_state(lib_db, album_id, before_album, before_items)
                store.update(operation_id, status=restore_res["status"], metadata={
                    **store.get(operation_id).get("metadata", {}),
                    "genre_after": genre_after, "item_genres_after": item_genres_after,
                    "tag_genres_after": tag_genres_after,
                    "partial_failure_reason": (result or {}).get("error") if isinstance(result, dict) else "lastgenre failed",
                    "rollback_detail": restore_res,
                })
                code = "genre_repair_rolled_back" if restore_res["status"] == "Rolled Back" else "genre_repair_recovery_required"
                return {
                    "ok": False,
                    "error": f"lastgenre failed after partially mutating album {album_id}; {restore_res['status'].lower()}",
                    "code": code, "mutated": True, "status": restore_res["status"],
                    "recovery_detail": restore_res,
                }

            store.update(operation_id, status="Completed", metadata={
                **store.get(operation_id).get("metadata", {}),
                "genre_after": genre_after,
                "item_genres_after": item_genres_after,
                "tag_genres_after": tag_genres_after,
                "completed_at": _now(),
            })
            return {"ok": True, "operation_id": operation_id, "status": "Completed", "mutated": mutated, "genre_after": genre_after, "output": result.get("stdout") or result.get("output") or ""}


def rollback_genre_repair(
    store: TransactionStore,
    operation_id: str,
    *,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    if not _TRANSACTION_ID_RE.match(operation_id):
        return {"ok": False, "error": "Invalid transaction ID format", "code": "genre_repair_invalid_id"}
    with _get_apply_lock(operation_id):
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": f"Transaction {operation_id} not found", "code": "genre_repair_not_found"}
        meta = tx.get("metadata") or {}
        if meta.get("mutation_family") != "genre_repair_v1":
            return {"ok": False, "error": "Transaction is not a genre_repair_v1 operation", "code": "genre_repair_family_mismatch"}
        lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")
        if not lib_db or not Path(lib_db).exists():
            return {"ok": False, "error": "Beets library database not found", "code": "genre_repair_db_not_found"}
        album_id = int(meta.get("album_id") or 0)
        before_album = _s(meta.get("before_album_genre") or "")
        before_items = meta.get("before_item_genres") or []
        restore_res = _restore_genre_and_tag_state(lib_db, album_id, before_album, before_items)
        if restore_res["status"] != "Rolled Back":
            store.update(operation_id, status="Recovery Required", metadata={
                **meta, "recovery_required_at": _now(), "rollback_detail": restore_res,
            })
            return {
                "ok": False, "operation_id": operation_id, "status": "Recovery Required",
                "error": "Genre rollback could not be fully verified (DB and/or on-disk tags)",
                "recovery_detail": restore_res,
            }
        store.update(operation_id, status="Rolled Back", metadata={**meta, "rollback_available": False, "rolled_back_at": _now()})
        return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}