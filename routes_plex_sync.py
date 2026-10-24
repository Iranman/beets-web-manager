"""Plex library-sync routes (ARCH-001): HTTP handlers over backend.plex_sync_service.

- POST /api/plex/sync/audit — the read-only whole-library audit as a job:
  folder naming, Plex-group readability (POSIX ACLs included), beets join by
  album directory and Plex index presence.
- POST /api/plex/sync/heal — the individually opt-in repairs as a job
  (chmod / touch / refresh). Flags come from the request body with the
  PLEX_SYNC_AUTO_* environment values as defaults, so a run only ever does
  what the operator asked for in this request. Nothing here deletes, moves
  or rewrites content.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from flask import jsonify, request

from app import app  # noqa: E402  (route modules load after app.py defines app)
from backend.app_runtime import jobs  # noqa: E402  (app_runtime is a leaf)
import backend.plex_sync_service as _svc


def _plex_sync_running_job(wanted_types: set) -> Optional[Any]:
    for job in jobs.all():
        if job.status != "running":
            continue
        metadata = getattr(job, "metadata", {}) or {}
        if str(metadata.get("type") or "") in wanted_types:
            return job
    return None


def _heal_flags(body: Dict[str, Any]) -> Dict[str, Any]:
    """Per-run actions. The request value wins when present; the module's
    environment-backed flags are only the fallback defaults."""
    try:
        max_paths = int(body.get("max_paths", _svc.PLEX_SYNC_MAX_REPAIR_PATHS))
    except (TypeError, ValueError):
        max_paths = _svc.PLEX_SYNC_MAX_REPAIR_PATHS
    return {
        "allow_chmod": bool(body.get("allow_chmod", _svc.PLEX_SYNC_AUTO_CHMOD)),
        "allow_touch": bool(body.get("allow_touch", _svc.PLEX_SYNC_AUTO_TOUCH)),
        "allow_refresh": bool(body.get("allow_refresh", _svc.PLEX_SYNC_AUTO_REFRESH)),
        "max_paths": max(100, max_paths),
    }


@app.post("/api/plex/sync/audit")
def plex_sync_audit():
    running = _plex_sync_running_job({"plex-sync-audit"})
    if running is not None:
        return jsonify({"ok": True, "job_id": running.job_id, "already_running": True})
    job = jobs.start_python(
        _svc.plex_sync_audit_job,
        label="Plex sync audit",
        metadata={"type": "plex-sync-audit"},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/plex/sync/heal")
def plex_sync_heal():
    running = _plex_sync_running_job({"plex-sync-heal"})
    if running is not None:
        return jsonify({"ok": True, "job_id": running.job_id, "already_running": True})
    flags = _heal_flags(request.get_json(silent=True) or {})
    actions = [name for name in ("allow_chmod", "allow_touch", "allow_refresh")
               if flags[name]]
    if not actions:
        return jsonify({
            "ok": False,
            "error": ("heal requested with every action disabled; pass "
                      "allow_chmod / allow_touch / allow_refresh in the body or "
                      "enable the PLEX_SYNC_AUTO_* environment flags"),
        }), 400

    def _run(run_log: List[str], cancel: Any = None) -> Dict[str, Any]:
        run_log.append("[plex-sync] heal requested: %s" % ", ".join(actions))
        return {"heal": _svc.apply_plex_sync_heal(run_log, **flags)}

    job = jobs.start_python(
        _run,
        label="Plex sync heal (%s)" % ", ".join(
            action.removeprefix("allow_") for action in actions),
        metadata={"type": "plex-sync-heal", **flags},
    )
    return jsonify({"ok": True, "job_id": job.job_id})
