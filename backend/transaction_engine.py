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

_TRANSACTION_ID_RE = re.compile(r"^txn_\d+_[0-9a-f]{12}$")

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


def _get_apply_lock(operation_id: str) -> threading.Lock:
    with _apply_locks_guard:
        lock = _apply_locks.get(operation_id)
        if lock is None:
            lock = threading.Lock()
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


def _get_resource_lock(key: str) -> threading.Lock:
    with _resource_locks_guard:
        lock = _resource_locks.get(key)
        if lock is None:
            lock = threading.Lock()
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
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


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
    return f"txn_{int(_now())}_{uuid.uuid4().hex[:12]}"


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
        item_path = Path(path_str)

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


def _path_under(child: Path, parent: Path) -> bool:
    """True iff `child` is contained within `parent`.

    Two independent checks, both required: a textual `os.path.normpath` +
    prefix check (the idiom CodeQL's py/path-injection query recognizes as
    a sanitizing barrier), and the pre-existing `.resolve(strict=False)` +
    `.relative_to()` check (the one that actually matters at runtime,
    since it also collapses symlinks -- a purely textual check would still
    let a symlink inside an already-validated root point outside it)."""
    try:
        child_norm = os.path.normpath(str(child))
        parent_norm = os.path.normpath(str(parent))
        if child_norm != parent_norm and not child_norm.startswith(parent_norm + os.sep):
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
        src = Path(cand["source_path"])
        dst = Path(cand["target_path"])

        if not _path_under(src, root_path) or not _path_under(dst, root_path):
            return {"ok": False, "error": "Path outside root.", "code": "artist_reconcile_path_out_of_root"}

        if _path_has_symlink_under(src, root_path) or _path_has_symlink_under(dst, root_path):
            return {"ok": False, "error": "Symlink rejected.", "code": "artist_reconcile_symlink_rejected"}

        if not src.exists() or not src.is_dir():
            requires_review.append({"source_path": str(src), "target_path": str(dst), "reason": "artist_reconcile_source_missing"})
            continue

        # Identity authority: independently derive each side's established
        # Artist ID(s) from Beets DB state -- never trust the caller-supplied
        # id as authority (SEC-002 Wave 21 final review, findings #4-#7).
        is_merge = dst.exists() and dst.is_dir() and dst.resolve(strict=False) != src.resolve(strict=False)
        src_identity = _derive_artist_folder_identity(src, lib_db)
        dst_identity = _derive_artist_folder_identity(dst, lib_db) if is_merge else {"ids": set(), "album_count": 0}
        caller_mbid = str(cand.get("source_mbid") or cand.get("target_mbid") or cand.get("mbid") or "").strip().lower()
        fingerprint_confirmed = cand.get("fingerprint_confirmed") is True

        eligible, resolved_mbid, reason = _artist_folder_identity_decision(
            src_identity, dst_identity, is_merge, caller_mbid, fingerprint_confirmed,
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
                "source_established_ids": sorted(src_identity["ids"]),
                "target_established_ids": sorted(dst_identity["ids"]),
                "caller_mbid": caller_mbid,
                "fingerprint_confirmed": fingerprint_confirmed,
                "is_merge": is_merge,
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


def _derive_artist_folder_identity(folder_path: Path, lib_db: str) -> Dict[str, Any]:
    """Independently establish Artist ID(s) for a folder directly from
    Beets DB state (distinct non-blank mb_albumartistid values among
    albums whose item paths live under this folder) -- SEC-002 Wave 21
    final review: a caller-supplied MBID is evidence, never authority.
    More than one distinct established id under the same folder is itself
    a conflict signal (returned via "ids" having len > 1)."""
    result: Dict[str, Any] = {"ids": set(), "album_count": 0}
    if not lib_db or not Path(lib_db).exists():
        return result
    prefix = str(folder_path).encode("utf-8") + os.sep.encode("utf-8")
    try:
        con = sqlite3.connect(lib_db, timeout=10)
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


def _artist_folder_identity_decision(
    src_identity: Dict[str, Any],
    dst_identity: Dict[str, Any],
    is_merge: bool,
    caller_mbid: str,
    fingerprint_confirmed: bool,
) -> Tuple[bool, str, str]:
    """Returns (eligible, resolved_mbid, reason_code_if_not_eligible).

    Policy (SEC-002 Wave 21 final review, exact rules requested):
    - Same established Artist ID on both sides (or established on one side
      matching a well-formed caller-supplied id when the other is a pure
      rename with no pre-existing destination): eligible.
    - Different established Artist IDs, or more than one established id
      under a single folder: hard conflict, never eligible.
    - One side established / one side blank: requires explicit caller
      fingerprint-confirmation evidence.
    - Both sides blank: requires explicit caller fingerprint-confirmation
      evidence; name equality alone is never sufficient.
    """
    src_ids = src_identity.get("ids") or set()
    dst_ids = dst_identity.get("ids") or set()
    if len(src_ids) > 1 or len(dst_ids) > 1:
        return False, "", "artist_reconcile_identity_conflict"
    src_id = next(iter(src_ids), "")
    dst_id = next(iter(dst_ids), "")
    caller_mbid = caller_mbid if _MB_UUID_RE.match(caller_mbid or "") else ""

    if is_merge:
        if src_id and dst_id:
            if src_id != dst_id:
                return False, "", "artist_reconcile_identity_conflict"
            return True, src_id, ""
        if src_id or dst_id:
            if not fingerprint_confirmed:
                return False, "", "artist_reconcile_requires_review"
            return True, (src_id or dst_id), ""
        if not fingerprint_confirmed:
            return False, "", "artist_reconcile_requires_review"
        return True, caller_mbid, ""

    # Pure rename (no pre-existing destination directory): the only
    # established evidence available is the source folder's own DB state.
    if src_id and caller_mbid and src_id != caller_mbid:
        return False, "", "artist_reconcile_identity_conflict"
    return True, (src_id or caller_mbid), ""


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

                    if not _path_under(p_path, Path(allowed_roots[0])):
                        return {"ok": False, "error": f"Item path outside allowed roots: {p_path}", "code": "album_maintenance_path_out_of_root"}
                    if _path_has_symlink_under(p_path, Path(allowed_roots[0])):
                        return {"ok": False, "error": f"Symlink rejected: {p_path}", "code": "album_maintenance_symlink_rejected"}

                    if delete_files and p_path.exists() and p_path.is_file():
                        st = p_path.stat()
                        dup_quarantines.append({
                            "item_id": int(r["id"]),
                            "source": str(p_path),
                            "stat": {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns},
                        })

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
            for d in payload["to_delete"]:
                try:
                    iid = int(d.get("id") or 0)
                    if iid > 0:
                        resource_keys.add(f"item:{iid}")
                        db_item_deletes.append({
                            "id": iid,
                            "album_id": aid,
                            "old_path": str(d.get("path") or ""),
                        })
                        p_path = Path(d.get("path") or "")
                        if p_path.exists() and p_path.is_file():
                            st = p_path.stat()
                            dup_quarantines.append({
                                "item_id": iid,
                                "source": str(p_path),
                                "stat": {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns},
                            })
                except Exception:
                    continue

        if payload.get("fix_updates"):
            for fu in payload["fix_updates"]:
                try:
                    iid = int(fu.get("id") or 0)
                    if iid > 0:
                        resource_keys.add(f"item:{iid}")
                        moves_plan.append({
                            "type": "move_file" if fu.get("rename") else "repoint_db",
                            "source": str(fu.get("old_path") or ""),
                            "destination": str(fu.get("new_path") or ""),
                        })
                        db_item_updates.append({
                            "id": iid,
                            "old_path": str(fu.get("old_path") or ""),
                            "new_path": str(fu.get("new_path") or ""),
                        })
                except Exception:
                    continue

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
            return {"ok": False, "error": msg, "code": code, "mutated": mutated, "rollback_available": mutated}

        with _lock_resources(resource_keys):
            dup_quarantines = meta.get("dup_quarantines") or []
            for dq in dup_quarantines:
                src_p = Path(dq["source"])
                if src_p.exists():
                    st = src_p.stat()
                    exp_st = dq["stat"]
                    if st.st_size != exp_st["size"] or st.st_mtime_ns != exp_st["mtime_ns"]:
                        return _fail(f"Source stat changed: {src_p}", "album_maintenance_toctou_mismatch")

            store.update(operation_id, status="Running", metadata={**meta, "mutation_started": True})

            q_base = quarantine_base_root or os.environ.get("RECONCILE_QUARANTINE_DIR", "/config/reconcile_quarantine")
            q_dir = Path(q_base) / operation_id
            q_dir.mkdir(parents=True, exist_ok=True)

            quarantined_records: List[Dict[str, Any]] = []
            for dq in dup_quarantines:
                src_p = Path(dq["source"])
                if not src_p.exists():
                    continue
                q_target = q_dir / f"item_{dq.get('item_id', 0)}_{src_p.name}"
                try:
                    _safe_rename(src_p, q_target)
                except Exception:
                    return _fail(f"Could not quarantine duplicate file: {src_p}", "album_maintenance_quarantine_failed")
                quarantined_records.append({
                    "item_id": dq.get("item_id"),
                    "source": str(src_p),
                    "quarantined": str(q_target),
                })

            directory_removals = meta.get("directory_removals") or []
            removed_dirs = []
            for dr in directory_removals:
                dr_p = Path(dr)
                if dr_p.exists() and dr_p.is_dir():
                    try:
                        if not any(dr_p.iterdir()):
                            dr_p.rmdir()
                            removed_dirs.append(dr)
                    except Exception:
                        pass

            store.update(operation_id, metadata={
                **store.get(operation_id).get("metadata", {}),
                "filesystem_mutated": True,
                "quarantined_records": quarantined_records,
                "removed_dirs": removed_dirs,
            })

            # Stage 2: Database Operations
            db_item_deletes = meta.get("db_item_deletes") or []
            db_album_deletes = meta.get("db_album_deletes") or []
            db_item_updates = meta.get("db_item_updates") or []

            deleted_items_count = 0
            deleted_albums_count = 0

            if lib_db and Path(lib_db).exists():
                con = sqlite3.connect(lib_db, timeout=10)
                try:
                    for u in db_item_updates:
                        con.execute("UPDATE items SET path=? WHERE id=?", (u["new_path"].encode("utf-8"), u["id"]))

                    for d in db_item_deletes:
                        cur = con.execute("DELETE FROM items WHERE album_id=? AND id=?", (d["album_id"], d["id"]))
                        if cur.rowcount == 1:
                            deleted_items_count += 1

                    for aid in db_album_deletes:
                        # Re-verify zero items remaining before deleting album row
                        tot = con.execute("SELECT COUNT(*) FROM items WHERE album_id=?", (aid,)).fetchone()[0]
                        if int(tot or 0) == 0:
                            cur = con.execute("DELETE FROM albums WHERE id=?", (aid,))
                            if cur.rowcount == 1:
                                deleted_albums_count += 1
                    con.commit()
                finally:
                    con.close()

            store.update(operation_id, status="Completed", metadata={
                **store.get(operation_id).get("metadata", {}),
                "db_mutated": True,
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
            files_restored = 0
            for qr in quarantined_records:
                q_p = Path(qr["quarantined"])
                orig_p = Path(qr["source"])
                if q_p.exists() and not orig_p.exists():
                    orig_p.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        _safe_rename(q_p, orig_p)
                        files_restored += 1
                    except Exception:
                        pass

            captured_item_rows = meta.get("captured_item_rows") or []
            captured_album_rows = meta.get("captured_album_rows") or []
            db_restored = 0

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
                    con.commit()
                finally:
                    con.close()

            store.update(operation_id, status="Rolled Back", metadata={
                **meta,
                "rollback_available": False,
                "files_restored_count": files_restored,
                "db_restored_count": db_restored,
                "rolled_back_at": _now(),
            })

            return {
                "ok": True,
                "operation_id": operation_id,
                "status": "Rolled Back",
                "files_restored": files_restored,
                "db_restored": db_restored,
            }


# ── album_artwork_v1 ──────────────────────────────────────────────────────────

def create_album_artwork_plan(
    store: TransactionStore,
    payload: Dict[str, Any],
    *,
    music_allowed_roots: Optional[List[str]] = None,
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
    allowed_roots = music_allowed_roots or [str(os.environ.get("MUSIC_ROOT", "/music"))]
    lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")
    if lib_db and not Path(lib_db).exists():
        lib_db = ""

    quarantines: List[Dict[str, Any]] = []
    moves_plan: List[Dict[str, Any]] = []
    resource_keys = [f"album:{aid}"]
    original_artpath = ""

    if payload.get("quarantined_art"):
        quarantines = list(payload["quarantined_art"])
    elif payload.get("candidates"):
        for cand in payload["candidates"]:
            src_p = Path(cand["source"])
            if src_p.exists() and src_p.is_file():
                st = src_p.stat()
                quarantines.append({
                    "source": str(src_p),
                    "stat": {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns},
                })

    if lib_db and Path(lib_db).exists():
        con = sqlite3.connect(lib_db, timeout=10)
        try:
            row = con.execute("SELECT artpath FROM albums WHERE id=?", (aid,)).fetchone()
            if row and row[0]:
                raw_art = row[0]
                original_artpath = raw_art.decode("utf-8", "replace") if isinstance(raw_art, bytes) else str(raw_art)
        finally:
            con.close()

    summary_text = f"Album artwork ({mode}): {len(quarantines)} file(s) quarantined/moved"
    tx = store.create(
        operation_type="Album Artwork",
        status="Preview",
        summary=summary_text,
        metadata={
            "mutation_family": "album_artwork_v1",
            "mode": mode,
            "album_id": aid,
            "quarantines": quarantines,
            "moves_plan": moves_plan,
            "original_artpath": original_artpath,
            "resource_keys": resource_keys,
            "allowed_roots": allowed_roots,
            "created_at": _now(),
        },
    )
    return {
        "ok": True,
        "operation_id": tx["id"],
        "album_id": aid,
        "quarantine_count": len(quarantines),
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

        resource_keys = meta.get("resource_keys") or []
        allowed_roots = music_allowed_roots or meta.get("allowed_roots") or [str(os.environ.get("MUSIC_ROOT", "/music"))]
        lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")

        def _fail(msg: str, code: str) -> Dict[str, Any]:
            curr = store.get(operation_id)
            c_meta = curr.get("metadata") or {}
            mutated = bool(c_meta.get("filesystem_mutated") or c_meta.get("db_mutated"))
            store.update(operation_id, status="Failed", logs=[f"Apply failed: {msg}"])
            return {"ok": False, "error": msg, "code": code, "mutated": mutated, "rollback_available": mutated}

        with _lock_resources(resource_keys):
            quarantines = meta.get("quarantines") or []
            for q in quarantines:
                src_p = Path(q["source"])
                if src_p.exists() and "stat" in q:
                    st = src_p.stat()
                    exp_st = q["stat"]
                    if st.st_size != exp_st["size"] or st.st_mtime_ns != exp_st["mtime_ns"]:
                        return _fail(f"Source artwork stat changed: {src_p}", "album_artwork_toctou_mismatch")

            store.update(operation_id, status="Running", metadata={**meta, "mutation_started": True})

            q_base = quarantine_base_root or os.environ.get("RECONCILE_QUARANTINE_DIR", "/config/reconcile_quarantine")
            q_dir = Path(q_base) / operation_id
            q_dir.mkdir(parents=True, exist_ok=True)

            quarantined_records: List[Dict[str, Any]] = []
            for q in quarantines:
                src_p = Path(q["source"])
                if not src_p.exists():
                    continue
                q_target = q_dir / f"art_{src_p.name}"
                try:
                    _safe_rename(src_p, q_target)
                except Exception:
                    return _fail(f"Could not quarantine artwork file: {src_p}", "album_artwork_quarantine_failed")
                quarantined_records.append({
                    "source": str(src_p),
                    "quarantined": str(q_target),
                })

            store.update(operation_id, metadata={
                **store.get(operation_id).get("metadata", {}),
                "filesystem_mutated": True,
                "quarantined_records": quarantined_records,
            })

            aid = int(meta.get("album_id") or 0)
            if lib_db and Path(lib_db).exists() and aid > 0:
                con = sqlite3.connect(lib_db, timeout=10)
                try:
                    con.execute("UPDATE albums SET artpath=NULL WHERE id=?", (aid,))
                    con.commit()
                finally:
                    con.close()

            store.update(operation_id, status="Completed", metadata={
                **store.get(operation_id).get("metadata", {}),
                "db_mutated": True,
                "completed_at": _now(),
            })

            return {"ok": True, "operation_id": operation_id, "status": "Completed", "mutated": True}


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
        lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")

        with _lock_resources(resource_keys):
            quarantined_records = meta.get("quarantined_records") or []
            files_restored = 0
            for qr in quarantined_records:
                q_p = Path(qr["quarantined"])
                orig_p = Path(qr["source"])
                if q_p.exists() and not orig_p.exists():
                    orig_p.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        _safe_rename(q_p, orig_p)
                        files_restored += 1
                    except Exception:
                        pass

            aid = int(meta.get("album_id") or 0)
            orig_art = meta.get("original_artpath") or ""
            if lib_db and Path(lib_db).exists() and aid > 0 and orig_art:
                con = sqlite3.connect(lib_db, timeout=10)
                try:
                    con.execute("UPDATE albums SET artpath=? WHERE id=?", (orig_art.encode("utf-8"), aid))
                    con.commit()
                finally:
                    con.close()

            store.update(operation_id, status="Rolled Back", metadata={
                **meta,
                "rollback_available": False,
                "files_restored_count": files_restored,
                "rolled_back_at": _now(),
            })

            return {"ok": True, "operation_id": operation_id, "status": "Rolled Back", "files_restored": files_restored}


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
    stg_roots = staging_allowed_roots or [str(os.environ.get("DOWNLOADS_ROOT", "/downloads")), str(os.environ.get("STAGING_ROOT", "/staging")), str(os.environ.get("MUSIC_ROOT", "/music"))]

    if not any(_path_under(src_p, Path(r)) for r in stg_roots):
        return {"ok": False, "error": f"Source folder outside allowed staging roots: {src_p}", "code": "import_folder_path_out_of_root"}
    if any(_path_has_symlink_under(src_p, Path(r)) for r in stg_roots):
        return {"ok": False, "error": f"Symlink rejected: {src_p}", "code": "import_folder_symlink_rejected"}

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

            import_result = None
            if beets_import_runner:
                try:
                    import_result = beets_import_runner(meta.get("payload") or {})
                except Exception as e:
                    return _fail(f"Beets engine import failed: {e}", "import_folder_beets_failed")

            store.update(operation_id, status="Completed", metadata={
                **store.get(operation_id).get("metadata", {}),
                "filesystem_mutated": True,
                "db_mutated": True,
                "import_result": import_result,
                "completed_at": _now(),
            })

            return {"ok": True, "operation_id": operation_id, "status": "Completed", "mutated": True}


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

        if tx.get("status") == "Rolled Back":
            return {"ok": False, "error": "Transaction is already rolled back.", "code": "import_folder_already_rolled_back"}

        resource_keys = meta.get("resource_keys") or []
        with _lock_resources(resource_keys):
            store.update(operation_id, status="Rolled Back", metadata={
                **meta,
                "rollback_available": False,
                "rolled_back_at": _now(),
            })

            return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}


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

    src_p = Path(src_folder)
    allowed_roots = music_allowed_roots or [str(os.environ.get("MUSIC_ROOT", "/music"))]

    if not _path_under(src_p, Path(allowed_roots[0])):
        return {"ok": False, "error": f"Folder outside allowed root: {src_p}", "code": "folder_cleanup_path_out_of_root"}
    if _path_has_symlink_under(src_p, Path(allowed_roots[0])):
        return {"ok": False, "error": f"Symlink rejected: {src_p}", "code": "folder_cleanup_symlink_rejected"}

    file_moves: List[Dict[str, Any]] = []
    dir_removals: List[str] = []
    dir_renames: List[Dict[str, Any]] = []

    if action in ("remove_empty_source", "remove_empty"):
        if src_p.exists() and src_p.is_dir():
            dir_removals.append(str(src_p))
    elif action in ("safe_rename", "rename_folder") and target_folder:
        tgt_p = Path(target_folder)
        if _path_under(tgt_p, Path(allowed_roots[0])):
            dir_renames.append({"source": str(src_p), "target": str(tgt_p)})
    elif action in ("merge_source_files", "merge") and target_folder:
        tgt_p = Path(target_folder)
        if _path_under(tgt_p, Path(allowed_roots[0])):
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

    resource_keys = [f"folder:{src_p.name}"]

    tx = store.create(
        operation_type="Folder Cleanup",
        status="Preview",
        summary=f"Folder cleanup ({action}): {src_p.name}",
        metadata={
            "mutation_family": "folder_cleanup_v1",
            "action": action,
            "source": str(src_p),
            "target": target_folder,
            "file_moves": file_moves,
            "dir_removals": dir_removals,
            "dir_renames": dir_renames,
            "resource_keys": resource_keys,
            "allowed_roots": allowed_roots,
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
            return {"ok": True, "operation_id": operation_id, "status": "Completed", "mutated": True}

        resource_keys = meta.get("resource_keys") or []

        def _fail(msg: str, code: str) -> Dict[str, Any]:
            curr = store.get(operation_id)
            c_meta = curr.get("metadata") or {}
            mutated = bool(c_meta.get("filesystem_mutated"))
            store.update(operation_id, status="Failed", logs=[f"Apply failed: {msg}"])
            return {"ok": False, "error": msg, "code": code, "mutated": mutated, "rollback_available": mutated}

        with _lock_resources(resource_keys):
            file_moves = meta.get("file_moves") or []
            for fm in file_moves:
                sp = Path(fm["source"])
                if sp.exists() and "stat" in fm:
                    st = sp.stat()
                    exp_st = fm["stat"]
                    if st.st_size != exp_st["size"] or st.st_mtime_ns != exp_st["mtime_ns"]:
                        return _fail(f"Source file stat changed: {sp}", "folder_cleanup_toctou_mismatch")

            store.update(operation_id, status="Running", metadata={**meta, "mutation_started": True})

            moved_records = []
            for fm in file_moves:
                sp = Path(fm["source"])
                tp = Path(fm["target"])
                if sp.exists():
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
                    tp.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        _safe_rename(sp, tp)
                        moved_records.append({"source": str(sp), "target": str(tp)})
                    except Exception as e:
                        return _fail(f"Folder rename failed {sp} -> {tp}: {e}", "folder_cleanup_rename_failed")

            dir_removals = meta.get("dir_removals") or []
            removed_dirs = []
            for dr in dir_removals:
                dp = Path(dr)
                if dp.exists() and dp.is_dir():
                    try:
                        if not any(dp.iterdir()):
                            dp.rmdir()
                            removed_dirs.append(dr)
                    except Exception:
                        pass

            store.update(operation_id, status="Completed", metadata={
                **store.get(operation_id).get("metadata", {}),
                "filesystem_mutated": True,
                "moved_records": moved_records,
                "removed_dirs": removed_dirs,
                "completed_at": _now(),
            })

            return {"ok": True, "operation_id": operation_id, "status": "Completed", "mutated": True}


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
        with _lock_resources(resource_keys):
            moved_records = meta.get("moved_records") or []
            files_restored = 0
            for mr in moved_records:
                sp = Path(mr["source"])
                tp = Path(mr["target"])
                if tp.exists() and not sp.exists():
                    sp.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        _safe_rename(tp, sp)
                        files_restored += 1
                    except Exception:
                        pass

            store.update(operation_id, status="Rolled Back", metadata={
                **meta,
                "rollback_available": False,
                "files_restored_count": files_restored,
                "rolled_back_at": _now(),
            })

            return {"ok": True, "operation_id": operation_id, "status": "Rolled Back", "files_restored": files_restored}


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
                captured_item_rows.append(dict(r))
                aid = int(r["album_id"] or 0)
                if aid > 0:
                    affected_album_ids.add(aid)

                raw_p = r["path"]
                p_str = raw_p.decode("utf-8", "replace") if isinstance(raw_p, bytes) else str(raw_p or "")
                if p_str:
                    p_path = Path(p_str)
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

    resource_keys = [f"item:{iid}" for iid in item_ids]

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
        lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")

        def _fail(msg: str, code: str) -> Dict[str, Any]:
            curr = store.get(operation_id)
            c_meta = curr.get("metadata") or {}
            mutated = bool(c_meta.get("filesystem_mutated") or c_meta.get("db_mutated"))
            store.update(operation_id, status="Failed", logs=[f"Apply failed: {msg}"])
            return {"ok": False, "error": msg, "code": code, "mutated": mutated, "rollback_available": mutated}

        with _lock_resources(resource_keys):
            quarantines = meta.get("quarantines") or []
            for q in quarantines:
                sp = Path(q["source"])
                if sp.exists() and "stat" in q:
                    st = sp.stat()
                    exp_st = q["stat"]
                    if st.st_size != exp_st["size"] or st.st_mtime_ns != exp_st["mtime_ns"]:
                        return _fail(f"Source file stat changed: {sp}", "playlist_media_cleanup_toctou_mismatch")

            store.update(operation_id, status="Running", metadata={**meta, "mutation_started": True})

            q_base = quarantine_base_root or os.environ.get("RECONCILE_QUARANTINE_DIR", "/config/reconcile_quarantine")
            q_dir = Path(q_base) / operation_id
            q_dir.mkdir(parents=True, exist_ok=True)

            quarantined_records = []
            for q in quarantines:
                sp = Path(q["source"])
                if not sp.exists():
                    continue
                q_target = q_dir / f"item_{q.get('item_id', 0)}_{sp.name}"
                try:
                    _safe_rename(sp, q_target)
                except Exception:
                    return _fail(f"Could not quarantine file: {sp}", "playlist_media_cleanup_quarantine_failed")
                quarantined_records.append({"item_id": q.get("item_id"), "source": str(sp), "quarantined": str(q_target)})

            store.update(operation_id, metadata={
                **store.get(operation_id).get("metadata", {}),
                "filesystem_mutated": True,
                "quarantined_records": quarantined_records,
            })

            db_item_deletes = meta.get("db_item_deletes") or []
            captured_album_rows = meta.get("captured_album_rows") or []

            deleted_items = 0
            deleted_albums = 0
            if lib_db and Path(lib_db).exists():
                con = sqlite3.connect(lib_db, timeout=10)
                try:
                    for d in db_item_deletes:
                        cur = con.execute("DELETE FROM items WHERE id=?", (d["id"],))
                        if cur.rowcount == 1:
                            deleted_items += 1

                    for arow in captured_album_rows:
                        aid = int(arow["id"])
                        tot = con.execute("SELECT COUNT(*) FROM items WHERE album_id=?", (aid,)).fetchone()[0]
                        if int(tot or 0) == 0:
                            cur = con.execute("DELETE FROM albums WHERE id=?", (aid,))
                            if cur.rowcount == 1:
                                deleted_albums += 1
                    con.commit()
                finally:
                    con.close()

            store.update(operation_id, status="Completed", metadata={
                **store.get(operation_id).get("metadata", {}),
                "db_mutated": True,
                "deleted_items_count": deleted_items,
                "deleted_albums_count": deleted_albums,
                "completed_at": _now(),
            })

            return {"ok": True, "operation_id": operation_id, "status": "Completed", "mutated": True}


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

        resource_keys = meta.get("resource_keys") or []
        lib_db = db_path or os.environ.get("BEETS_LIBRARY_DB", "")

        with _lock_resources(resource_keys):
            quarantined_records = meta.get("quarantined_records") or []
            files_restored = 0
            for qr in quarantined_records:
                qp = Path(qr["quarantined"])
                sp = Path(qr["source"])
                if qp.exists() and not sp.exists():
                    sp.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        _safe_rename(qp, sp)
                        files_restored += 1
                    except Exception:
                        pass

            captured_item_rows = meta.get("captured_item_rows") or []
            captured_album_rows = meta.get("captured_album_rows") or []
            db_restored = 0

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

                    for arow in captured_album_rows:
                        aid = int(arow["id"])
                        cur.execute("SELECT COUNT(*) FROM albums WHERE id=?", (aid,))
                        if cur.fetchone()[0] == 0:
                            cols = list(arow.keys())
                            placeholders = ",".join("?" * len(cols))
                            cur.execute(f"INSERT INTO albums ({','.join(cols)}) VALUES ({placeholders})", [arow[c] for arow in cols])

                    con.commit()
                finally:
                    con.close()

            store.update(operation_id, status="Rolled Back", metadata={
                **meta,
                "rollback_available": False,
                "files_restored_count": files_restored,
                "db_restored_count": db_restored,
                "rolled_back_at": _now(),
            })

            return {"ok": True, "operation_id": operation_id, "status": "Rolled Back", "files_restored": files_restored, "db_restored": db_restored}


