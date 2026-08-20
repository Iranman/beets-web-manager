"""Durable transaction records for library-changing jobs.

The transaction store is intentionally small and file-backed so it can sit
beside the existing JobStore without changing the job architecture.
"""
from __future__ import annotations

import csv
import errno
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
from contextlib import ExitStack
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

def create_existing_album_reconcile_plan(
    store: TransactionStore,
    payload: Dict[str, Any],
    *,
    music_allowed_roots: Optional[List[str]] = None,
    quarantine_base_root: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a plan for existing-album duplicate & move reconciliation (SEC-002 Wave 20)."""
    imported_album_id = int(payload.get("imported_album_id") or 0)
    existing_album_id = int(payload.get("existing_album_id") or 0)
    if imported_album_id <= 0 or existing_album_id <= 0 or imported_album_id == existing_album_id:
        return {"ok": False, "error": "Invalid album IDs for existing album reconciliation", "code": "reconcile_invalid_album"}

    dup_item_ids = [int(v) for v in (payload.get("dup_item_ids") or []) if int(v or 0) > 0]
    dup_details: List[Dict[str, Any]] = payload.get("dup_details") or []
    dup_details_map = {int(d.get("dup_item_id") or 0): d for d in dup_details if int(d.get("dup_item_id") or 0) > 0}

    move_item_ids = [int(v) for v in (payload.get("move_item_ids") or []) if int(v or 0) > 0]

    if not dup_item_ids and not move_item_ids:
        return {"ok": False, "error": "No duplicate or move items specified", "code": "reconcile_empty_payload"}

    lib_db = db_path or os.environ.get("BEETS_DB_PATH", os.path.expanduser("~/.config/beets/musiclibrary.blb"))
    if not os.path.exists(lib_db):
        return {"ok": False, "error": f"Beets database file not found at {lib_db}"}

    source_folder_str = str(payload.get("source_folder") or "").strip()
    roots = list(music_allowed_roots or [os.environ.get("MUSIC_ROOT", "/music")])
    if source_folder_str:
        roots.append(source_folder_str)

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

    con = sqlite3.connect(lib_db, timeout=10)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute("SELECT id, album, albumartist, mb_albumid, mb_releasegroupid FROM albums WHERE id=?", (existing_album_id,))
    existing_alb = cur.fetchone()
    if not existing_alb:
        con.close()
        return {"ok": False, "error": f"Target existing album {existing_album_id} not found", "code": "reconcile_album_missing"}

    cur.execute("SELECT id, album, albumartist, mb_albumid, mb_releasegroupid FROM albums WHERE id=?", (imported_album_id,))
    imported_alb = cur.fetchone()
    if not imported_alb:
        con.close()
        return {"ok": False, "error": f"Source imported album {imported_album_id} not found", "code": "reconcile_album_missing"}

    existing_rg = str(existing_alb["mb_releasegroupid"] or "").strip().lower()
    imported_rg = str(imported_alb["mb_releasegroupid"] or "").strip().lower()
    if existing_rg and imported_rg and existing_rg != imported_rg:
        con.close()
        return {"ok": False, "error": f"Cannot reconcile albums with different Release Group IDs ({imported_rg} vs {existing_rg})", "code": "reconcile_identity_mismatch"}

    changes: List[Dict[str, Any]] = []
    move_specs: List[Dict[str, Any]] = []
    dup_specs: List[Dict[str, Any]] = []

    # Validate move_item_ids
    for iid in move_item_ids:
        cur.execute(
            "SELECT id, album_id, title, artist, album, albumartist, disc, track, path, mb_trackid, mb_albumid, mb_releasegroupid, length "
            "FROM items WHERE id=?",
            (iid,),
        )
        row = cur.fetchone()
        if not row:
            con.close()
            return {"ok": False, "error": f"Move item {iid} not found", "code": "reconcile_item_missing"}
        if int(row["album_id"]) != imported_album_id:
            con.close()
            return {"ok": False, "error": f"Move item {iid} does not belong to imported album {imported_album_id}", "code": "reconcile_album_membership_changed"}

        raw_path = row["path"]
        path_str = raw_path.decode("utf-8", "replace") if isinstance(raw_path, bytes) else str(raw_path)
        item_path = Path(path_str)

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
            con.close()
            return {"ok": False, "error": f"Move item {iid} path outside allowed roots: {path_str}", "code": "reconcile_path_out_of_root"}

        if _path_has_symlink_under(item_path, base_root):
            con.close()
            return {"ok": False, "error": f"Move item {iid} path contains symlink: {path_str}", "code": "reconcile_symlink_rejected"}

        if not item_path.exists():
            con.close()
            return {"ok": False, "error": f"Move item {iid} file missing on disk: {path_str}", "code": "reconcile_item_missing"}

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
                "album": str(row["album"] or ""),
                "albumartist": str(row["albumartist"] or ""),
                "path": path_str,
                "stat": stat_snapshot,
            },
            "after": {
                "album_id": existing_album_id,
                "album": str(existing_alb["album"] or ""),
                "albumartist": str(existing_alb["albumartist"] or ""),
            },
        }
        move_specs.append(spec)
        changes.append({
            "id": f"item:{iid}",
            "operation": "Reassign Album",
            "item_id": iid,
            "old_album_id": imported_album_id,
            "new_album_id": existing_album_id,
            "title": str(row["title"] or ""),
            "path": path_str,
        })

    # Validate dup_item_ids
    for iid in dup_item_ids:
        cur.execute(
            "SELECT id, album_id, title, artist, album, albumartist, disc, track, path, mb_trackid, mb_albumid, mb_releasegroupid, length "
            "FROM items WHERE id=?",
            (iid,),
        )
        row = cur.fetchone()
        if not row:
            con.close()
            return {"ok": False, "error": f"Duplicate item {iid} not found", "code": "reconcile_item_missing"}
        if int(row["album_id"]) != imported_album_id:
            con.close()
            return {"ok": False, "error": f"Duplicate item {iid} does not belong to imported album {imported_album_id}", "code": "reconcile_album_membership_changed"}

        raw_path = row["path"]
        path_str = raw_path.decode("utf-8", "replace") if isinstance(raw_path, bytes) else str(raw_path)
        item_path = Path(path_str)

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
            con.close()
            return {"ok": False, "error": f"Duplicate item {iid} path outside allowed roots: {path_str}", "code": "reconcile_path_out_of_root"}

        if _path_has_symlink_under(item_path, base_root):
            con.close()
            return {"ok": False, "error": f"Duplicate item {iid} path contains symlink: {path_str}", "code": "reconcile_symlink_rejected"}

        if not item_path.exists():
            con.close()
            return {"ok": False, "error": f"Duplicate item {iid} file missing on disk: {path_str}", "code": "reconcile_item_missing"}

        st = item_path.stat()
        stat_snapshot = {
            "dev": st.st_dev,
            "ino": st.st_ino,
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
        }

        # Check survivor requirement
        detail = dup_details_map.get(iid) or {}
        survivor_ids = [int(v) for v in (detail.get("survivor_item_ids") or []) if int(v or 0) > 0]
        if not survivor_ids:
            # Fallback: check existing_album_id for item at same disc/track
            disc = int(row["disc"] or 1)
            track = int(row["track"] or 0)
            if track > 0:
                cur.execute(
                    "SELECT id FROM items WHERE album_id=? AND disc=? AND track=?",
                    (existing_album_id, disc, track),
                )
                survivor_ids = [int(r["id"]) for r in cur.fetchall()]

        if not survivor_ids:
            con.close()
            return {"ok": False, "error": f"Duplicate item {iid} has no verified survivor track in album {existing_album_id}", "code": "reconcile_survivor_missing"}

        # Validate survivors
        valid_survivors: List[Dict[str, Any]] = []
        is_hardlink = False
        for s_id in survivor_ids:
            cur.execute(
                "SELECT id, album_id, title, path, mb_trackid FROM items WHERE id=?",
                (s_id,),
            )
            s_row = cur.fetchone()
            if not s_row or int(s_row["album_id"]) != existing_album_id:
                continue
            s_raw_path = s_row["path"]
            s_path_str = s_raw_path.decode("utf-8", "replace") if isinstance(s_raw_path, bytes) else str(s_raw_path)
            s_path = Path(s_path_str)
            if not s_path.exists():
                continue
            s_st = s_path.stat()
            if s_st.st_dev == st.st_dev and s_st.st_ino == st.st_ino:
                is_hardlink = True

            valid_survivors.append({
                "item_id": s_id,
                "path": s_path_str,
                "title": str(s_row["title"] or ""),
                "mb_trackid": str(s_row["mb_trackid"] or ""),
            })

        if not valid_survivors:
            con.close()
            return {"ok": False, "error": f"Duplicate item {iid} survivor item(s) invalid or missing on disk", "code": "reconcile_survivor_missing"}

        spec = {
            "item_id": iid,
            "before": {
                "album_id": imported_album_id,
                "title": str(row["title"] or ""),
                "artist": str(row["artist"] or ""),
                "album": str(row["album"] or ""),
                "albumartist": str(row["albumartist"] or ""),
                "disc": int(row["disc"] or 1),
                "track": int(row["track"] or 0),
                "path": path_str,
                "mb_trackid": str(row["mb_trackid"] or ""),
                "mb_albumid": str(row["mb_albumid"] or ""),
                "mb_releasegroupid": str(row["mb_releasegroupid"] or ""),
                "length": float(row["length"] or 0),
                "stat": stat_snapshot,
            },
            "survivors": valid_survivors,
            "is_hardlink": is_hardlink,
        }
        dup_specs.append(spec)
        changes.append({
            "id": f"item:{iid}",
            "operation": "Delete Duplicate Item",
            "item_id": iid,
            "path": path_str,
            "survivors": [s["item_id"] for s in valid_survivors],
            "is_hardlink": is_hardlink,
        })

    # Check if imported album will become empty
    cur.execute("SELECT COUNT(*) FROM items WHERE album_id=?", (imported_album_id,))
    total_imported_items = cur.fetchone()[0]
    total_processed = len(move_specs) + len(dup_specs)
    will_retire_album = (total_processed >= total_imported_items)

    album_before_snapshot = None
    if will_retire_album:
        album_before_snapshot = {
            "id": imported_album_id,
            "album": str(imported_alb["album"] or ""),
            "albumartist": str(imported_alb["albumartist"] or ""),
            "mb_albumid": str(imported_alb["mb_albumid"] or ""),
            "mb_releasegroupid": str(imported_alb["mb_releasegroupid"] or ""),
        }
        changes.append({
            "id": f"album:{imported_album_id}",
            "operation": "Retire Empty Album",
            "album_id": imported_album_id,
        })

    con.close()

    tx_payload = {
        "imported_album_id": imported_album_id,
        "existing_album_id": existing_album_id,
        "source_folder": source_folder_str,
        "move_specs": move_specs,
        "dup_specs": dup_specs,
        "retire_imported_album": will_retire_album,
        "imported_album_before": album_before_snapshot,
    }

    summary = f"Reconcile imported album {imported_album_id} into existing album {existing_album_id}: {len(move_specs)} move(s), {len(dup_specs)} duplicate(s)"
    tx = store.create(
        operation_type="Merge Album",
        status="Preview",
        summary=summary,
        changes=changes,
        rollback_available=True,
        rollback_reason="Can restore reassigned album membership, duplicate rows, and unlinked/quarantined duplicate files",
        metadata={
            "mutation_family": "existing_album_reconcile_v1",
            "payload": tx_payload,
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
    }


def execute_existing_album_reconcile_apply(
    store: TransactionStore,
    operation_id: str,
    *,
    db_path: Optional[str] = None,
    music_allowed_roots: Optional[List[str]] = None,
    quarantine_base_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute Apply phase for existing_album_reconcile_v1 (SEC-002 Wave 20)."""
    op_lock = _get_apply_lock(operation_id)
    with op_lock:
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": f"Transaction {operation_id} not found."}

        meta = tx.get("metadata") or {}
        mutation_family = meta.get("mutation_family")
        if mutation_family != "existing_album_reconcile_v1":
            return {"ok": False, "error": f"Transaction family mismatch: expected 'existing_album_reconcile_v1', got {mutation_family!r}"}

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
        }))

        # Resource locking: album locks + sorted item locks
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
                return {"ok": False, "error": f"Beets database file not found at {lib_db}"}

            source_folder_str = str(payload.get("source_folder") or "").strip()
            roots = list(music_allowed_roots or [os.environ.get("MUSIC_ROOT", "/music")])
            if source_folder_str:
                roots.append(source_folder_str)

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

            q_root_env = quarantine_base_root or os.environ.get("RECONCILE_QUARANTINE_DIR", "/config/reconcile_quarantine")
            q_dir = Path(q_root_env) / operation_id
            q_dir.mkdir(parents=True, exist_ok=True)

            def _fail(err: str, code: str = "reconcile_toctou_mismatch") -> Dict[str, Any]:
                store.update(operation_id, status="Failed", logs=[f"TOCTOU failed: {err}"])
                return {"ok": False, "error": err, "code": code, "requires_new_plan": True}

            # TOCTOU & Precondition Revalidation (ALL-OR-NOTHING)
            try:
                con = sqlite3.connect(lib_db, timeout=10)
                con.row_factory = sqlite3.Row
                cur = con.cursor()

                cur.execute("SELECT id, mb_releasegroupid FROM albums WHERE id=?", (existing_album_id,))
                ex_alb = cur.fetchone()
                if not ex_alb:
                    con.close()
                    return _fail(f"Target album {existing_album_id} missing", "reconcile_album_missing")

                cur.execute("SELECT id, mb_releasegroupid FROM albums WHERE id=?", (imported_album_id,))
                imp_alb = cur.fetchone()
                if not imp_alb:
                    con.close()
                    return _fail(f"Source album {imported_album_id} missing", "reconcile_album_missing")

                # Validate move_specs
                for mspec in move_specs:
                    iid = int(mspec["item_id"])
                    before = mspec["before"]
                    cur.execute("SELECT id, album_id, path FROM items WHERE id=?", (iid,))
                    irow = cur.fetchone()
                    if not irow:
                        con.close()
                        return _fail(f"Move item {iid} missing", "reconcile_item_missing")
                    if int(irow["album_id"]) != imported_album_id:
                        con.close()
                        return _fail(f"Move item {iid} album membership changed", "reconcile_album_membership_changed")

                    raw_path = irow["path"]
                    path_str = raw_path.decode("utf-8", "replace") if isinstance(raw_path, bytes) else str(raw_path)
                    if path_str != before["path"]:
                        con.close()
                        return _fail(f"Move item {iid} path changed", "reconcile_toctou_mismatch")

                    ipath = Path(path_str)
                    if not ipath.exists():
                        con.close()
                        return _fail(f"Move item {iid} file missing: {path_str}", "reconcile_item_missing")

                    st = ipath.stat()
                    b_stat = before["stat"]
                    if st.st_dev != b_stat["dev"] or st.st_ino != b_stat["ino"] or st.st_size != b_stat["size"] or st.st_mtime_ns != b_stat["mtime_ns"]:
                        con.close()
                        return _fail(f"Move item {iid} file stat changed", "reconcile_toctou_mismatch")

                # Validate dup_specs
                for dspec in dup_specs:
                    iid = int(dspec["item_id"])
                    before = dspec["before"]
                    cur.execute("SELECT id, album_id, path FROM items WHERE id=?", (iid,))
                    irow = cur.fetchone()
                    if not irow:
                        con.close()
                        return _fail(f"Duplicate item {iid} missing", "reconcile_item_missing")
                    if int(irow["album_id"]) != imported_album_id:
                        con.close()
                        return _fail(f"Duplicate item {iid} album membership changed", "reconcile_album_membership_changed")

                    raw_path = irow["path"]
                    path_str = raw_path.decode("utf-8", "replace") if isinstance(raw_path, bytes) else str(raw_path)
                    if path_str != before["path"]:
                        con.close()
                        return _fail(f"Duplicate item {iid} path changed", "reconcile_toctou_mismatch")

                    ipath = Path(path_str)
                    if not ipath.exists():
                        con.close()
                        return _fail(f"Duplicate item {iid} file missing: {path_str}", "reconcile_item_missing")

                    st = ipath.stat()
                    b_stat = before["stat"]
                    if st.st_dev != b_stat["dev"] or st.st_ino != b_stat["ino"] or st.st_size != b_stat["size"] or st.st_mtime_ns != b_stat["mtime_ns"]:
                        con.close()
                        return _fail(f"Duplicate item {iid} file stat changed", "reconcile_toctou_mismatch")

                    # Re-verify survivors
                    for surv in dspec.get("survivors") or []:
                        s_id = int(surv["item_id"])
                        cur.execute("SELECT id, album_id, path FROM items WHERE id=?", (s_id,))
                        s_row = cur.fetchone()
                        if not s_row or int(s_row["album_id"]) != existing_album_id:
                            con.close()
                            return _fail(f"Duplicate item {iid} survivor {s_id} changed or missing", "reconcile_survivor_missing")
                        s_raw_path = s_row["path"]
                        s_path_str = s_raw_path.decode("utf-8", "replace") if isinstance(s_raw_path, bytes) else str(s_raw_path)
                        s_path = Path(s_path_str)
                        if not s_path.exists():
                            con.close()
                            return _fail(f"Duplicate item {iid} survivor {s_id} file missing on disk", "reconcile_survivor_missing")

                con.close()
            except Exception as ex:
                return _fail(f"Precondition check exception: {ex}", "reconcile_verification_failed")

            # Mark Running & Mutated
            store.update(operation_id, status="Running", metadata={**meta, "mutated": True})

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

            # Stage 1: Duplicate File Quarantine / Removal
            quarantined_files: List[Dict[str, Any]] = []
            unlinked_files: List[Dict[str, Any]] = []
            source_root_path = Path(source_folder_str).resolve(strict=False) if source_folder_str else None

            for dspec in dup_specs:
                iid = int(dspec["item_id"])
                fpath = Path(dspec["before"]["path"])
                is_hardlink = bool(dspec.get("is_hardlink"))

                # Check if path is under source_folder (staging)
                under_source = False
                if source_root_path:
                    try:
                        fpath.resolve(strict=False).relative_to(source_root_path)
                        under_source = True
                    except ValueError:
                        pass

                if under_source or is_hardlink:
                    try:
                        fpath.unlink(missing_ok=True)
                        unlinked_files.append({"item_id": iid, "path": str(fpath)})
                        _persist_step("dup_file_unlinked", "Completed", item_id=iid)
                    except Exception as ex:
                        store.update(operation_id, status="Failed", logs=[f"File unlink failed for dup item {iid}: {ex}"])
                        return {"ok": False, "error": f"File unlink failed for item {iid}: {ex}", "code": "reconcile_filesystem_failed", "mutated": True}
                else:
                    # Move to quarantine
                    target_q = q_dir / f"{iid}_{fpath.name}"
                    try:
                        shutil.move(str(fpath), str(target_q))
                        quarantined_files.append({"item_id": iid, "original_path": str(fpath), "quarantine_path": str(target_q)})
                        _persist_step("dup_file_quarantined", "Completed", item_id=iid)
                    except Exception as ex:
                        store.update(operation_id, status="Failed", logs=[f"File quarantine failed for dup item {iid}: {ex}"])
                        return {"ok": False, "error": f"File quarantine failed for item {iid}: {ex}", "code": "reconcile_filesystem_failed", "mutated": True}

            store.update(operation_id, metadata={
                **store.get(operation_id).get("metadata", {}),
                "quarantined_files": quarantined_files,
                "unlinked_files": unlinked_files,
            })

            # Stage 2: Database Operations
            try:
                con = sqlite3.connect(lib_db, timeout=10)
                cur = con.cursor()

                # 1. Delete dup rows
                if dup_specs:
                    dup_ids = [int(d["item_id"]) for d in dup_specs]
                    cur.execute(f"DELETE FROM items WHERE id IN ({','.join('?' * len(dup_ids))})", dup_ids)
                    if cur.rowcount != len(dup_ids):
                        con.rollback()
                        con.close()
                        store.update(operation_id, status="Failed", logs=["DB delete rowcount mismatch for duplicate items"])
                        return {"ok": False, "error": "DB delete rowcount mismatch for duplicate items", "code": "reconcile_db_failed", "mutated": True}

                # 2. Reassign move rows
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
                        con.close()
                        store.update(operation_id, status="Failed", logs=["DB update rowcount mismatch for moved items"])
                        return {"ok": False, "error": "DB update rowcount mismatch for moved items", "code": "reconcile_db_failed", "mutated": True}

                # 3. Retire imported album if empty
                if payload.get("retire_imported_album"):
                    cur.execute("SELECT COUNT(*) FROM items WHERE album_id=?", (imported_album_id,))
                    left_cnt = cur.fetchone()[0]
                    if left_cnt == 0:
                        cur.execute("DELETE FROM albums WHERE id=?", (imported_album_id,))

                con.commit()
                con.close()
                _persist_step("db_reassigned", "Completed")
            except Exception as ex:
                store.update(operation_id, status="Failed", logs=[f"Database operations failed: {ex}"])
                return {"ok": False, "error": f"Database operations failed: {ex}", "code": "reconcile_db_failed", "mutated": True}

            # Stage 3: Verification
            try:
                con = sqlite3.connect(lib_db, timeout=10)
                cur = con.cursor()
                for mspec in move_specs:
                    iid = int(mspec["item_id"])
                    cur.execute("SELECT album_id FROM items WHERE id=?", (iid,))
                    r = cur.fetchone()
                    if not r or int(r[0]) != existing_album_id:
                        con.close()
                        store.update(operation_id, status="Failed", logs=[f"Verification failed: move item {iid} album_id != {existing_album_id}"])
                        return {"ok": False, "error": f"Verification failed for move item {iid}", "code": "reconcile_verification_failed", "mutated": True}

                for dspec in dup_specs:
                    iid = int(dspec["item_id"])
                    cur.execute("SELECT COUNT(*) FROM items WHERE id=?", (iid,))
                    cnt = cur.fetchone()[0]
                    if cnt != 0:
                        con.close()
                        store.update(operation_id, status="Failed", logs=[f"Verification failed: duplicate item {iid} still exists in DB"])
                        return {"ok": False, "error": f"Verification failed for duplicate item {iid}", "code": "reconcile_verification_failed", "mutated": True}

                con.close()
                _persist_step("result_verified", "Completed")
            except Exception as ex:
                store.update(operation_id, status="Failed", logs=[f"Verification exception: {ex}"])
                return {"ok": False, "error": f"Verification exception: {ex}", "code": "reconcile_verification_failed", "mutated": True}

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
    op_lock = _get_apply_lock(operation_id)
    with op_lock:
        try:
            tx = store.get(operation_id)
        except KeyError:
            return {"ok": False, "error": f"Transaction {operation_id} not found."}

        meta = tx.get("metadata") or {}
        mutation_family = meta.get("mutation_family")
        if mutation_family != "existing_album_reconcile_v1":
            return {"ok": False, "error": f"Transaction family mismatch: expected 'existing_album_reconcile_v1', got {mutation_family!r}"}

        status = tx.get("status")
        mutated = bool(meta.get("mutated"))
        if status not in ("Completed", "Failed") or not mutated:
            return {"ok": False, "error": "Rollback unavailable: transaction has not performed any mutations"}

        payload = meta.get("payload") or meta or tx.get("payload") or {}
        imported_album_id = int(payload.get("imported_album_id") or 0)
        move_specs: List[Dict[str, Any]] = payload.get("move_specs") or []
        dup_specs: List[Dict[str, Any]] = payload.get("dup_specs") or []
        imported_alb_before = payload.get("imported_album_before")
        quarantined_files: List[Dict[str, Any]] = meta.get("quarantined_files") or []

        lib_db = db_path or os.environ.get("BEETS_DB_PATH", os.path.expanduser("~/.config/beets/musiclibrary.blb"))
        if not os.path.exists(lib_db):
            return {"ok": False, "error": f"Beets database file not found at {lib_db}"}

        db_restored = 0
        db_failed = 0

        # Stage 1: Restore DB
        try:
            con = sqlite3.connect(lib_db, timeout=10)
            cur = con.cursor()

            # Re-create imported album if it was retired
            if imported_alb_before:
                cur.execute("SELECT COUNT(*) FROM albums WHERE id=?", (imported_album_id,))
                if cur.fetchone()[0] == 0:
                    cur.execute(
                        "INSERT INTO albums (id, album, albumartist, mb_albumid, mb_releasegroupid) VALUES (?, ?, ?, ?, ?)",
                        (
                            imported_album_id,
                            imported_alb_before.get("album", ""),
                            imported_alb_before.get("albumartist", ""),
                            imported_alb_before.get("mb_albumid", ""),
                            imported_alb_before.get("mb_releasegroupid", ""),
                        ),
                    )

            # Reassign move_specs back to imported_album_id
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
                    db_failed += 1

            # Restore duplicate DB rows
            for dspec in dup_specs:
                iid = int(dspec["item_id"])
                before = dspec["before"]
                cur.execute("SELECT COUNT(*) FROM items WHERE id=?", (iid,))
                if cur.fetchone()[0] == 0:
                    cur.execute(
                        "INSERT INTO items (id, album_id, title, artist, album, albumartist, disc, track, path, mb_trackid, mb_albumid, mb_releasegroupid, length) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            iid,
                            imported_album_id,
                            before["title"],
                            before["artist"],
                            before["album"],
                            before["albumartist"],
                            before["disc"],
                            before["track"],
                            before["path"],
                            before["mb_trackid"],
                            before["mb_albumid"],
                            before["mb_releasegroupid"],
                            before["length"],
                        ),
                    )
                    if cur.rowcount == 1:
                        db_restored += 1
                    else:
                        db_failed += 1

            con.commit()
            con.close()
        except Exception as ex:
            store.update(operation_id, logs=[f"Rollback DB restore failed: {ex}"])
            return {"ok": False, "error": f"Rollback DB restore failed: {ex}"}

        # Stage 2: Restore Quarantined Files
        files_restored = 0
        files_failed = 0
        for qf in quarantined_files:
            orig_p = Path(qf["original_path"])
            q_p = Path(qf["quarantine_path"])
            if q_p.exists():
                try:
                    orig_p.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(q_p), str(orig_p))
                    files_restored += 1
                except Exception:
                    files_failed += 1

        total_expected = len(move_specs) + len(dup_specs)
        full_success = (db_failed == 0) and (files_failed == 0)
        final_status = "Rolled Back" if full_success else ("Partially Rolled Back" if (db_restored > 0 or files_restored > 0) else "Failed")

        store.update(
            operation_id,
            status=final_status,
            logs=[f"Existing album reconcile rollback completed with status {final_status}."],
            metadata={
                **meta,
                "rollback_available": not full_success,
                "db_restored_count": db_restored,
                "files_restored_count": files_restored,
            },
        )
        return {
            "ok": final_status in ("Rolled Back", "Partially Rolled Back"),
            "operation_id": operation_id,
            "status": final_status,
            "db_restored_count": db_restored,
            "files_restored_count": files_restored,
            "total_items": total_expected,
        }

