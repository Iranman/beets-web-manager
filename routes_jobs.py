"""Job management routes — registered on app after app.py initializes."""
import time

from flask import jsonify, request

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
