"""Durable transaction records for library-changing jobs.

The transaction store is intentionally small and file-backed so it can sit
beside the existing JobStore without changing the job architecture.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
import urllib.parse


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

        if tx.get("status") == "Completed":
            return True, None

        meta = tx.get("metadata") or {}
        payload = meta.get("payload") or {}
        target_path = meta.get("target_path")
        expected_states = meta.get("expected_states") or {}

        # 1. Check symlink / existence / size / mtime for recorded paths
        for path_str, state in expected_states.items():
            p = Path(path_str)
            if os.path.islink(p):
                return False, f"Path {path_str} became a symlink."
            if not p.exists():
                return False, f"Path {path_str} no longer exists."
            st = p.stat()
            expected_size = state.get("size")
            expected_mtime = state.get("mtime")
            if expected_size is not None and st.st_size != expected_size:
                return False, f"Path {path_str} size changed (expected {expected_size}, got {st.st_size})."
            if expected_mtime is not None and abs(st.st_mtime - expected_mtime) > 0.001:
                return False, f"Path {path_str} mtime changed."

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
) -> Dict[str, Any]:
    """Execute plan creation for import review folder/files with strict validation."""
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

    # Check music library refusal if not confirmed wrong library folder
    import app as flask_app_mod
    music_root = Path(getattr(flask_app_mod, "MUSIC_ROOT", "/music")).resolve(strict=False)
    if (resolved_target == music_root or music_root in resolved_target.parents) and not confirmed_wrong_library_folder and album_id <= 0:
        return {"ok": False, "error": f"Review folder path {resolved_target} is inside music library."}

    expected_states: Dict[str, Any] = {}
    sources: List[str] = []
    skipped: List[Dict[str, str]] = []

    if raw_files:
        for f in raw_files:
            try:
                f_raw = str(f).strip()
                if not f_raw:
                    skipped.append({"file": f_raw, "reason": "invalid_path"})
                    continue

                if "\x00" in f_raw or "%00" in f_raw:
                    skipped.append({"file": f_raw, "reason": "invalid_path"})
                    continue

                norm_raw = f_raw.replace("\\", "/")
                parts_raw = [p for p in norm_raw.split("/") if p]
                if any(p in {".", ".."} for p in parts_raw) or f_raw.startswith(("/", "\\")):
                    skipped.append({"file": f_raw, "reason": "outside_review_folder"})
                    continue

                # 1. Try literal path under resolved_target first (preserves literal percent signs in filenames)
                literal_fp = Path(f_raw)
                if not literal_fp.is_absolute():
                    literal_fp = resolved_target / literal_fp
                
                try:
                    literal_resolved = literal_fp.resolve(strict=False)
                except Exception:
                    literal_resolved = None

                if literal_resolved and literal_resolved.exists() and literal_resolved.is_file() and not literal_fp.is_symlink() and not literal_resolved.is_symlink():
                    if literal_resolved != resolved_target and resolved_target in literal_resolved.parents:
                        st = literal_resolved.stat()
                        expected_states[str(literal_resolved)] = {"size": st.st_size, "mtime": st.st_mtime, "is_file": True}
                        sources.append(str(literal_resolved))
                        continue

                # 2. Decode URL encoding if literal path was not found
                f_decode = f_raw
                for _ in range(3):
                    dec = urllib.parse.unquote(f_decode)
                    if dec == f_decode:
                        break
                    f_decode = dec

                if "\x00" in f_decode:
                    skipped.append({"file": f_raw, "reason": "invalid_path"})
                    continue

                # Traversal check on raw and decoded text
                norm_f = f_decode.replace("\\", "/")
                parts = [p for p in norm_f.split("/") if p]
                if any(p in {".", ".."} for p in parts) or f_raw.startswith(("/", "\\")) or Path(f_decode).is_absolute():
                    skipped.append({"file": f_raw, "reason": "outside_review_folder"})
                    continue

                fp = Path(f_decode)
                if not fp.is_absolute():
                    fp = resolved_target / fp
                fp_resolved = fp.resolve(strict=False)

                # Check if fp_resolved is contained under resolved_target
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
                    st = fp_resolved.stat()
                    expected_states[str(fp_resolved)] = {"size": st.st_size, "mtime": st.st_mtime, "is_file": True}
                    sources.append(str(fp_resolved))
                else:
                    skipped.append({"file": f_raw, "reason": "not_found"})
            except Exception:
                skipped.append({"file": str(f), "reason": "invalid_path"})
    else:
        if resolved_target.is_dir():
            for fp in resolved_target.rglob("*"):
                if fp.is_file() and not os.path.islink(fp) and not fp.is_symlink():
                    st = fp.stat()
                    expected_states[str(fp.resolve())] = {"size": st.st_size, "mtime": st.st_mtime, "is_file": True}
                    sources.append(str(fp.resolve()))

    payload_with_skipped = {**payload, "skipped": skipped}

    try:
        op_id, tx = store.create_import_review_cleanup_plan(
            target_path=str(resolved_target),
            allowed_roots=allowed_roots,
            source_paths=sources,
            expected_states=expected_states,
            reversibility="RECOVERABLE" if action != "delete_folder" else "IRREVERSIBLE",
            payload=payload_with_skipped,
        )
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
    """Execute plan application for import review folder/files after revalidating preconditions."""
    tx = store.get(operation_id)
    if not tx:
        return {"ok": False, "error": f"Transaction {operation_id} not found."}

    meta = tx.get("metadata") or {}
    payload = meta.get("payload") or {}
    skipped = payload.get("skipped", [])

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

    log: List[str] = []
    deleted: List[str] = []
    moved: List[Dict[str, str]] = []

    target_path = Path(meta.get("target_path", ""))
    q_base = Path(quarantine_root or os.environ.get("IMPORT_REVIEW_QUARANTINE_DIR", "/config/import_review_quarantine"))

    import shutil
    import stat

    def _is_symlink(p: Path) -> bool:
        try:
            return p.is_symlink() or os.path.islink(str(p)) or stat.S_ISLNK(os.lstat(str(p)).st_mode)
        except Exception:
            return False

    def _path_has_symlink_under(path: Path, base: Path) -> bool:
        try:
            curr = path
            while curr != base and base in curr.parents:
                if _is_symlink(curr):
                    return True
                curr = curr.parent
            if curr == base and _is_symlink(curr):
                return True
        except Exception:
            return True
        return False

    if action in {"quarantine_rejected", "quarantine_duplicate"}:
        date_folder = q_base / time.strftime("%Y%m%d")
        for src_str in expected_states.keys():
            src = Path(src_str)
            if not src.exists() or not src.is_file() or src.is_symlink() or os.path.islink(str(src)):
                skipped.append({"file": str(src), "reason": "symlink"})
                continue

            rel = src.relative_to(target_path) if target_path in src.parents else Path(src.name)
            dest = date_folder / rel

            # Validate destination directory safety & no symlink components under q_base
            try:
                dest_parent = dest.parent
                if _path_has_symlink_under(dest_parent, q_base):
                    skipped.append({"file": str(src), "reason": "symlink"})
                    continue

                dest_parent.mkdir(parents=True, exist_ok=True)

                if _path_has_symlink_under(dest_parent, q_base) or dest.is_symlink() or os.path.islink(str(dest)):
                    skipped.append({"file": str(src), "reason": "symlink"})
                    continue

                q_resolved = q_base.resolve(strict=False)
                dest_resolved = dest_parent.resolve(strict=False)
                if dest_resolved == q_resolved or q_resolved not in dest_resolved.parents:
                    skipped.append({"file": str(src), "reason": "unsafe_destination"})
                    continue
            except Exception:
                skipped.append({"file": str(src), "reason": "unsafe_destination"})
                continue

            # Pre-move check: src must still not be a symlink and dest must not exist as a symlink/dir
            if src.is_symlink() or os.path.islink(str(src)) or dest.is_symlink() or os.path.islink(str(dest)) or (dest.exists() and dest.is_dir()):
                skipped.append({"file": str(src), "reason": "symlink"})
                continue

            try:
                shutil.move(str(src), str(dest))
                # Post-move verification: ensure no file escaped q_base or was written through a symlink
                q_resolved = q_base.resolve(strict=False)
                dest_resolved = dest.resolve(strict=False)

                is_invalid = dest.is_symlink() or os.path.islink(str(dest)) or dest_resolved == q_resolved or q_resolved not in dest_resolved.parents
                written_file = (dest / src.name) if (dest.exists() and dest.is_dir() and dest != dest_parent) else dest
                written_resolved = written_file.resolve(strict=False)
                if written_resolved != q_resolved and q_resolved not in written_resolved.parents:
                    is_invalid = True

                if is_invalid:
                    try:
                        # 1. If a file was written through a symlink to an escaped directory, restore it to src
                        if (dest.exists() and dest.is_dir() and dest != dest_parent) or (written_file != dest):
                            if written_resolved.exists() and written_resolved.is_file() and not src.exists() and q_resolved not in written_resolved.parents:
                                try:
                                    shutil.move(str(written_resolved), str(src))
                                except Exception:
                                    pass

                        # 2. Remove destination symlink itself (does not touch external files like sentinel)
                        if _is_symlink(dest) or dest.is_symlink():
                            try:
                                os.unlink(str(dest))
                            except Exception:
                                pass
                        else:
                            if written_file.exists() and q_resolved in written_file.resolve(strict=False).parents:
                                try:
                                    written_file.unlink()
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    skipped.append({"file": str(src), "reason": "symlink"})
                    continue
                moved.append({"source": str(src), "quarantined": str(dest)})
                log.append(f"Quarantined {src} -> {dest}")
            except Exception:
                skipped.append({"file": str(src), "reason": "failed_move"})

    elif action in {"delete_rejected", "delete_duplicate", "delete", "delete_folder"}:
        for src_str in expected_states.keys():
            src = Path(src_str)
            if src.exists():
                if src.is_file() or src.is_symlink():
                    src.unlink()
                    deleted.append(str(src))
                    log.append(f"Deleted {src}")

    # Remove empty parent subdirectories if target_path is a directory
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

    store.update(
        operation_id,
        status="Completed",
        applied_at=time.time(),
        metadata={"deleted": deleted, "moved": moved},
        logs=log,
    )

    return {
        "ok": True,
        "status": "Completed",
        "operation_id": operation_id,
        "deleted": deleted,
        "moved": moved,
        "skipped": skipped,
        "log": log,
    }
