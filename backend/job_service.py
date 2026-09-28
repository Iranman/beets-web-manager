"""Job orchestration helpers over the shared JobStore (ARCH-001).
"""

from __future__ import annotations

import re, time
from typing import Any, Iterable, List, Optional, Tuple
from backend.app_runtime import _s
from backend.auth_service import _yt_bot_check_message
from backend.app_runtime import jobs

# ── ARCH-001 extracted code ──


def _call_job_fn(fn, log, cancel=None, update_state=None):
    import inspect as _inspect
    sig = _inspect.signature(fn)
    if len(sig.parameters) >= 3:
        return fn(log, cancel, update_state)
    if len(sig.parameters) >= 2:
        return fn(log, cancel)
    return fn(log)


def _wait_for_child_job(job_id: str, log: list, cancel_event=None,
                        prefix: str = "import", timeout: int = 900,
                        idle_timeout: int = 0, progress: Optional[Any] = None) -> Any:
    """Mirror a child job and fail on failure, wall timeout, or idle stall."""
    started = time.time()
    deadline = started + timeout if timeout and timeout > 0 else None
    last_activity = started
    last_state_key: Optional[Tuple[Any, ...]] = None
    timeout_notice_logged = False
    seen = 0
    mirrored_lines: List[str] = []

    def _mirrored_log_indicates_success() -> bool:
        text = "\n".join(_s(line) for line in mirrored_lines[-120:])
        if re.search(r'(^|\n)\s*ERROR:|Traceback|job failed', text, re.IGNORECASE):
            return False
        success_markers = (
            "Updated ",
            "track(s) matched and numbered from MB",
            "Final file names",
            "Existing album already matches",
            "Existing album repair completed",
        )
        return any(marker in text for marker in success_markers)

    def _child_failure_detail() -> str:
        for line in reversed(mirrored_lines[-120:]):
            text = _s(line).strip()
            if not text:
                continue
            if (
                "youtube rejected the configured yt-dlp cookies" in text.lower()
                or text.startswith("ERROR:")
                or "Traceback" in text
                or _yt_bot_check_message(text)
            ):
                return text[:500]
        return ""

    def _should_mirror_child_line(line: Any) -> bool:
        if prefix == "metadata-repair":
            text = _s(line).strip()
            if not text:
                return False
            lower = text.lower()
            if "error" in lower or "traceback" in lower or "failed" in lower or "warn" in lower:
                return True
            if text.startswith(("Scanning for albums", "Done -")):
                return True
            return False
        if prefix != "duplicates":
            return True
        text = _s(line).strip()
        if not text:
            return False
        lower = text.lower()
        if "error" in lower or "traceback" in lower or "failed" in lower:
            return True
        if text.startswith("Found ") and "audio file" in lower:
            return True
        if text.startswith(("Scanning ", "Comparing ", "Done")):
            return True
        if re.match(r"^\[\d+/\d+\]", text):
            return False
        if "DUPLICATE [" in text or "REJECTED [" in text or any(marker in text for marker in (
            "CANDIDATE [", "VERIFIED [", "REVIEW REQUIRED [",
            "[CANDIDATE]", "[FINGERPRINT VERIFIED]", "[BYTE VERIFIED]", "[REVIEW REQUIRED]", "[REJECTED]"
        )):
            return False
        if text.startswith(("skipped stale path:", "skipped unreadable path:")):
            return False
        return True

    while True:
        now = time.time()
        if cancel_event is not None and cancel_event.is_set():
            child = jobs.get(job_id)
            if child:
                child.kill()
            raise RuntimeError("cancelled")
        child = jobs.get(job_id)
        if not child:
            if _mirrored_log_indicates_success():
                log.append(
                    f"  [{prefix}] Child job record was already cleaned up after a successful import; continuing."
                )
                return {"job_id": job_id, "status": "success", "result_inferred_from_log": True}
            raise RuntimeError(f"child job not found: {job_id}")
        new_lines = child.log[seen:]
        for line in new_lines:
            if _should_mirror_child_line(line):
                log.append(f"  [{prefix}] {line}")
        mirrored_lines.extend(new_lines)
        seen += len(new_lines)
        child_state = dict(getattr(child, "state", {}) or {})
        state_key = (
            child.status,
            len(child.log),
            child_state.get("scanned_count"),
            child_state.get("found_count"),
            child_state.get("total_count"),
            child_state.get("remaining_count"),
            child_state.get("current_path"),
            child_state.get("current_task"),
            child_state.get("current_result"),
            child_state.get("duplicate_type"),
            child_state.get("progress_percent"),
        )
        if new_lines or state_key != last_state_key:
            last_activity = now
            last_state_key = state_key
            if progress and child_state:
                progress(child_state)
        if child.status != "running":
            if child.status != "success":
                detail = _child_failure_detail()
                raise RuntimeError(
                    f"{prefix} job failed: {detail}" if detail else f"{prefix} job failed"
                )
            return getattr(child, "result", None)
        if deadline is not None and now >= deadline:
            if idle_timeout and (now - last_activity) < idle_timeout:
                if not timeout_notice_logged:
                    log.append(
                        f"  [{prefix}] Still making progress after {timeout}s; "
                        f"continuing unless idle for {idle_timeout}s."
                    )
                    timeout_notice_logged = True
            else:
                child.kill()
                raise RuntimeError(f"{prefix} job timed out")
        if idle_timeout and (now - last_activity) >= idle_timeout:
            child.kill()
            raise RuntimeError(f"{prefix} job stalled with no progress for {idle_timeout}s")
        time.sleep(2)


def _running_job_of_type(job_types: Iterable[str]) -> Optional[Any]:
    wanted = {str(value) for value in job_types}
    for job in jobs.all():
        if job.status != "running":
            continue
        metadata = getattr(job, "metadata", {}) or {}
        if str(metadata.get("type") or "") in wanted:
            return job
    return None


def _root_folder_repair_running_job() -> Optional[Any]:
    for job in jobs.all():
        if job.status != "running":
            continue
        metadata = getattr(job, "metadata", {}) or {}
        if str(metadata.get("type") or "") in {"root-folder-repair-scan", "root-folder-repair-apply-safe"}:
            return job
    return None
