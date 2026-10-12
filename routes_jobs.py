"""Job management routes — registered on app after app.py initializes."""
import hashlib
import os
import re
import time

from flask import g, jsonify, request

import backend.provider_boundary as provider_boundary
from job_engine import DuplicateJobError, IdempotencyKeyReuseError, enter_start_scope, exit_start_scope

# Imported after app.py has already defined app and jobs (circular-but-OK pattern)
from app import app, jobs, _ai_batch_find_state, _ai_batch_reconcile_state, _AI_BATCH_TERMINAL_STATUSES  # noqa: E402


def _truthy_arg(name: str, default: bool = False) -> bool:
    raw = request.args.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _present_job_row(job, *, include_log=False, include_result=True):
    row = job.to_dict(include_log=include_log, include_result=include_result)
    metadata = row.get("metadata") or {}
    if metadata.get("type") == "ai-batch-import":
        ident = metadata.get("batch_job_id") or row.get("job_id")
        state = _ai_batch_find_state(ident) if ident else None
        if state:
            state = _ai_batch_reconcile_state(state)
            if state.get("status") in _AI_BATCH_TERMINAL_STATUSES and row.get("status") == "running":
                row["status"] = "success"
                row["returncode"] = 0
                row["finished_at"] = state.get("completed_at") or row.get("finished_at")
            row.setdefault("state", {})
            row["state"].update({
                "status": state.get("status"),
                "current_step": state.get("current_step"),
                "folders_processed": state.get("folders_processed"),
                "total_folders_found": state.get("total_folders_found"),
                "folders_attention": state.get("folders_attention"),
            })
    return row


_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:\-]{8,128}$")


def _request_fingerprint() -> str:
    """What makes two requests the same operation: method, path, query and
    body (JSON/raw bytes, form fields, uploaded file names)."""
    digest = hashlib.sha256()
    for part in (request.method, request.path, request.query_string.decode("latin-1")):
        digest.update(part.encode("utf-8", "replace") + b"\0")
    for key, value in sorted(request.form.items(multi=True)):
        digest.update(f"form:{key}={value}\0".encode("utf-8", "replace"))
    for key, storage in sorted(request.files.items(multi=True), key=lambda kv: kv[0]):
        digest.update(f"file:{key}={storage.filename}\0".encode("utf-8", "replace"))
        try:  # the upload's bytes, without disturbing the view's read position
            position = storage.stream.tell()
            storage.stream.seek(0)
            for chunk in iter(lambda: storage.stream.read(1 << 16), b""):
                digest.update(chunk)
            storage.stream.seek(position)
        except (OSError, ValueError, AttributeError):
            digest.update(os.urandom(16))  # unknown contents: never call two uploads the same
    digest.update(request.get_data(cache=True))
    return digest.hexdigest()


@app.before_request
def _open_job_start_scope():
    """Duplicate-start protection for every route that starts a job (see
    job_engine's module docstring): the first job a mutating request starts
    is tied to the request's fingerprint and optional Idempotency-Key."""
    if request.method not in _MUTATING_METHODS:
        return None
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if key and not _IDEMPOTENCY_KEY_RE.match(key):
        return jsonify({"ok": False, "code": "invalid_idempotency_key",
                        "error": "Idempotency-Key must be 8-128 characters of A-Z, a-z, 0-9, '.', '_', ':' or '-'."}), 400
    g._job_start_scope = enter_start_scope(_request_fingerprint, key)
    return None


@app.teardown_request
def _close_job_start_scope(_exc=None):
    token = g.pop("_job_start_scope", None)
    if token is not None:
        exit_start_scope(token)


@app.errorhandler(DuplicateJobError)
def _duplicate_job(exc):
    """Any route that starts a job: nothing was started. Either an identical
    mutating job is still running (BA-6), or -- ``job_already_started`` --
    this request's Idempotency-Key already started ``job_id``. A client that
    retried a start follows ``job_id`` either way."""
    job = exc.job
    code = "job_already_started" if exc.replay and job.status != "running" else "job_already_running"
    return jsonify({"ok": False, "code": code, "error": str(exc), "job_id": job.job_id,
                    "status": job.status, "replay": bool(exc.replay)}), 409


@app.errorhandler(IdempotencyKeyReuseError)
def _idempotency_key_reused(exc):
    return jsonify({"ok": False, "code": "idempotency_key_reused", "error": str(exc),
                    "job_id": exc.job.job_id}), 422


@app.get("/api/jobs")
def list_jobs():
    started = time.perf_counter()
    include_result = _truthy_arg("include_result", False)
    rows = [_present_job_row(j, include_result=include_result) for j in jobs.all()]
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    if duration_ms > 250:
        app.logger.info("/api/jobs listed %s job(s) in %.1f ms", len(rows), duration_ms)
    return jsonify({"jobs": rows, "count": len(rows), "duration_ms": duration_ms})


@app.get("/api/jobs/<jid>")
def get_job(jid):
    job = jobs.get(jid)
    if not job:
        return jsonify({"ok": False, "error": "Not found"}), 404
    return jsonify(_present_job_row(job, include_log=True))


def _job_log_level(line: str) -> str:
    text = str(line or "")
    lower = text.lower()
    if "[debug]" in lower or lower.startswith("debug"):
        return "debug"
    if "error" in lower or "traceback" in lower or "failed" in lower:
        return "error"
    if "warn" in lower or "skipped" in lower:
        return "warn"
    return "info"


@app.get("/api/jobs/feed")
def jobs_feed():
    try:
        limit = max(1, min(1000, int(request.args.get("limit") or 250)))
    except Exception:
        limit = 250
    level_filter = str(request.args.get("level") or "all").strip().lower()
    started = time.perf_counter()
    entries = []
    total = 0
    for job in sorted(jobs.all(), key=lambda item: (item.created_at or 0, item.job_id), reverse=True):
        for idx in range(len(job.log) - 1, -1, -1):
            line = job.log[idx]
            level = _job_log_level(line)
            if level_filter == "debug" and level != "debug":
                continue
            if level_filter == "warn" and level not in {"warn", "error"}:
                continue
            total += 1
            if len(entries) >= limit:
                continue
            entries.append({
                "job_id": job.job_id,
                "label": job.label,
                "status": job.status,
                "level": level,
                "line": idx + 1,
                "message": line,
                "created_at": job.created_at,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
            })
    entries.reverse()
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    if duration_ms > 250:
        app.logger.info("/api/jobs/feed scanned %s log line(s) in %.1f ms", total, duration_ms)
    return jsonify({"ok": True, "entries": entries, "total": total, "duration_ms": duration_ms})


@app.post("/api/jobs/<jid>/kill")
def kill_job(jid):
    job = jobs.get(jid)
    if not job:
        return jsonify({"ok": False, "error": "Not found"}), 404
    job.kill()
    return jsonify({"ok": True})


@app.delete("/api/jobs")
def clear_jobs():
    jobs.clear_finished()
    return jsonify({"ok": True})


@app.get("/api/providers/health")
def providers_health():
    """Read-only: the last classified outcome per external provider since this
    process started, and each provider's retry policy (ARCH-006). Redacted:
    no URLs, no keys."""
    policies = {name: {"max_attempts": provider_boundary.policy_for(name).max_attempts,
                       "base_backoff": provider_boundary.policy_for(name).base_backoff}
                for name in sorted(provider_boundary.POLICIES)}
    return jsonify({"ok": True, "providers": provider_boundary.provider_health(), "policies": policies})
