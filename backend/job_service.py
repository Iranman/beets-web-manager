"""Job orchestration helpers over the shared JobStore (ARCH-001).
"""

from __future__ import annotations

import contextvars, re, time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from backend.app_runtime import _s
from backend.auth_service import _yt_bot_check_message
from backend.app_runtime import jobs
from job_engine import is_read_only_job

# ── ARCH-001 extracted code ──

#: How long a parent waits, after asking a mutating child job to stop, for the
#: child's real outcome (#317). A Beets operation the child is waiting on does
#: not stop at once.
# ponytail: fixed window; per-caller values if a child type needs longer.
CHILD_SETTLE_SECONDS = 300
# An album download (Soulseek peers, then fallback sources, then the import) may
# legitimately run past 40 minutes; give up only once it stops making progress.
# The idle limit stays above the silent inner phases (import-slot wait and the
# SpotiFLAC subprocess, each capped at 1200s), so it never cuts them short.
ALBUM_DOWNLOAD_WAIT = {"timeout": 2400, "idle_timeout": 1500}

#: Set by the transaction job wrapper (transaction_service) for the job running
#: on this thread: ``note_job_progress`` reports into that job's audit.
_job_progress: "contextvars.ContextVar[Optional[Callable[..., None]]]" = contextvars.ContextVar(
    "job_progress", default=None)


def note_job_progress(**flags: Any) -> None:
    """Tell the current job's transaction audit what happened (#317):
    ``mutation_started=True`` (the library may have changed) or
    ``outcome_unknown=True`` (a child operation was still running when this
    job stopped waiting for it). No-op outside a transaction-wrapped job."""
    sink = _job_progress.get()
    if sink is not None:
        sink(**flags)


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

    def _mirror(child: Any) -> List[Any]:
        nonlocal seen
        new_lines = child.log[seen:]
        for line in new_lines:
            if _should_mirror_child_line(line):
                log.append(f"  [{prefix}] {line}")
        mirrored_lines.extend(new_lines)
        seen += len(new_lines)
        return new_lines

    def _settle(child: Any) -> Optional[Any]:
        """Ask the child to stop. A mutating child may be in the middle of a
        Beets operation that keeps running (#317), so wait for its real
        outcome: the terminal child, or None when it is still running after
        CHILD_SETTLE_SECONDS (or its record vanished): outcome unknown."""
        if child.status != "running":
            return child
        child.kill()
        log.append(f"  [{prefix}] Asked job {job_id} to stop; waiting up to {CHILD_SETTLE_SECONDS}s "
                   "for its Beets operation to finish or stop.")
        end = time.time() + CHILD_SETTLE_SECONDS
        while True:
            current = jobs.get(job_id)
            if current is None:
                return None
            _mirror(current)
            if current.status != "running":
                return current
            if time.time() >= end:
                return None
            time.sleep(2)

    def _changed_library(done: Any) -> bool:
        state = getattr(done, "state", None) or {}
        result = getattr(done, "result", None)
        return (done.status == "success" or bool(state.get("mutation_started"))
                or (isinstance(result, dict) and bool(result.get("mutation_started"))))

    def _note_unknown() -> None:
        note_job_progress(outcome_unknown=True, mutation_started=True)
        log.append(f"  [{prefix}] Job {job_id} is still running; whether it changed the library is unknown. "
                   "Check it in Jobs.")

    def _cancel(child: Optional[Any]) -> RuntimeError:
        if child is None or is_read_only_job(getattr(child, "metadata", None)):
            if child is not None:
                child.kill()
            return RuntimeError("cancelled")
        done = _settle(child)
        if done is None:
            _note_unknown()
        elif _changed_library(done):
            note_job_progress(mutation_started=True)
            log.append(f"  [{prefix}] Job {job_id} ended {done.status} after the cancel request; "
                       "it may have changed the library before it stopped.")
        else:
            log.append(f"  [{prefix}] Job {job_id} stopped ({done.status}) before reporting a library change.")
        return RuntimeError("cancelled")

    def _give_up(child: Any, reason: str) -> Any:
        """Timeout or stall: a read-only child is simply stopped; a mutating
        one's real outcome is waited for and reported (#317)."""
        if is_read_only_job(getattr(child, "metadata", None)):
            child.kill()
            raise RuntimeError(reason)
        done = _settle(child)
        if done is None:
            _note_unknown()
            raise RuntimeError(f"{reason}; job {job_id} is still running, so its outcome is unknown")
        if done.status == "success":
            log.append(f"  [{prefix}] Job {job_id} finished its work after all ({reason}); continuing with its result.")
            return getattr(done, "result", None)
        if _changed_library(done):
            note_job_progress(mutation_started=True)
        raise RuntimeError(reason)

    while True:
        now = time.time()
        if cancel_event is not None and cancel_event.is_set():
            raise _cancel(jobs.get(job_id))
        child = jobs.get(job_id)
        if not child:
            if _mirrored_log_indicates_success():
                log.append(
                    f"  [{prefix}] Child job record was already cleaned up after a successful import; continuing."
                )
                return {"job_id": job_id, "status": "success", "result_inferred_from_log": True}
            raise RuntimeError(f"child job not found: {job_id}")
        new_lines = _mirror(child)
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
                return _give_up(child, f"{prefix} job timed out")
        if idle_timeout and (now - last_activity) >= idle_timeout:
            return _give_up(child, f"{prefix} job stalled with no progress for {idle_timeout}s")
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
