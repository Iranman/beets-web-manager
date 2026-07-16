"""Job engine — subprocess Job/PythonJob, JobStore, and _beet_run helper."""
import subprocess, threading, time, uuid
from typing import Any, Dict, List, Optional


def _summarize_result(value):
    """Compact PythonJob results for list views without shipping full reports."""
    if value is None:
        return None
    if isinstance(value, dict):
        keys = list(value.keys())
        scalars = {}
        sizes = {}
        for key, item in value.items():
            if isinstance(item, (str, int, float, bool)) or item is None:
                text = item if not isinstance(item, str) else item[:160]
                scalars[str(key)] = text
            elif isinstance(item, (list, tuple, set, dict)):
                sizes[str(key)] = len(item)
        return {
            "type": "dict",
            "key_count": len(keys),
            "keys": [str(key) for key in keys[:16]],
            "scalars": scalars,
            "sizes": sizes,
        }
    if isinstance(value, (list, tuple, set)):
        return {"type": "list", "count": len(value)}
    return {"type": type(value).__name__, "value": str(value)[:160]}

def _beet_run(cmd, log, *, timeout=120, env=None, warn_msg=None, cancel=None):
    """Run a beet subprocess.
    Polls every 250 ms so a cancel request kills the process immediately.
    On TimeoutExpired logs a warning and returns rc=124 so callers can treat
    it as a soft timeout rather than success or hard error."""
    _POLL = 0.25
    class _Killed:
        returncode = -9; stdout = ""; stderr = ""
    class _TimedOut:
        returncode = 124; stdout = ""; stderr = ""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env)
        deadline = time.time() + timeout
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=_POLL)
                class _R:
                    pass
                r = _R()
                r.returncode = proc.returncode
                r.stdout = stdout or ""
                r.stderr = stderr or ""
                return r
            except subprocess.TimeoutExpired:
                pass
            if cancel is not None and cancel.is_set():
                proc.kill()
                proc.communicate()
                log.append("  [killed by user]")
                return _Killed()
            if time.time() > deadline:
                proc.kill()
                proc.communicate()
                step = ' '.join(cmd[3:5]) if len(cmd) > 4 else ' '.join(cmd)
                log.append(warn_msg or
                           f"  ⚠ '{step}' timed out after {timeout}s —"
                           " files likely already in place, verify in library")
                return _TimedOut()
    except Exception as exc:
        log.append(f"  ⚠ _beet_run error: {exc}")
        class _R:
            returncode = 1; stdout = ""; stderr = ""
        return _R()


class Job:
    def __init__(self, job_id: str, command: List[str], label: str = ""):
        self.job_id      = job_id
        self.command     = command
        self.label       = label or " ".join(command)
        self.created_at  = time.time()
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.returncode: Optional[int]    = None
        self.log: List[str]               = []
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    @property
    def status(self):
        if self.finished_at is not None:
            return "success" if self.returncode == 0 else "failed"
        return "running"

    def kill(self):
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.kill()
                self.log.append("[killed]")

    def _run(self):
        self.started_at = time.time()
        try:
            with self._lock:
                self._proc = subprocess.Popen(
                    self.command, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                )
            for line in self._proc.stdout:
                self.log.append(line.rstrip("\n"))
                if len(self.log) > 5000:
                    self.log = self.log[-5000:]
            self.returncode = self._proc.wait()
        except Exception as exc:
            self.log.append(f"ERROR: {exc}")
            self.returncode = 1
        finally:
            self.finished_at = time.time()

    def to_dict(self, include_log=False, include_result=True):
        d = {
            "job_id":      self.job_id,
            "label":       self.label,
            "status":      self.status,
            "created_at":  self.created_at,
            "started_at":  self.started_at,
            "finished_at": self.finished_at,
            "returncode":  self.returncode,
            "log_lines":   len(self.log),
        }
        if include_log:
            d["log"] = self.log
        return d


class PythonJob:
    """Like Job but runs a Python callable instead of a subprocess.
    The callable receives (log, cancel_event) and should periodically check
    cancel_event.is_set() to exit early.  Callables may also accept a third
    update_state callback for structured progress.  If the callable returns a
    dict, the result is stored in self.result and included in to_dict()."""
    def __init__(self, job_id: str, fn, label: str = ""):
        self.job_id      = job_id
        self.label       = label
        self.created_at  = time.time()
        self.started_at: Optional[float]  = None
        self.finished_at: Optional[float] = None
        self.returncode: Optional[int]    = None
        self.log: List[str]               = []
        self.result: Optional[Any]        = None
        self.metadata: Dict[str, Any]     = {}
        self.state: Dict[str, Any]        = {}
        self._lock        = threading.Lock()
        self._fn          = fn
        self._cancel      = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    @property
    def status(self):
        if self.finished_at is not None:
            return "success" if self.returncode == 0 else "failed"
        return "running"

    def kill(self):
        """Request cancellation.  The job must co-operatively check _cancel."""
        self._cancel.set()
        self.log.append("[cancel requested]")

    def update_state(self, updates: Optional[Dict[str, Any]] = None, **kwargs):
        """Merge structured progress fields for API consumers.

        This is intentionally additive and optional so older jobs that only
        produce readable/raw log output continue to behave exactly as before.
        """
        payload: Dict[str, Any] = {}
        if updates:
            payload.update(updates)
        if kwargs:
            payload.update(kwargs)
        if not payload:
            return
        with self._lock:
            self.state.update(payload)

    def _state_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            state = dict(self.state)
        state.setdefault("job_id", self.job_id)
        state.setdefault("job_name", self.label)
        state.setdefault("status", self.status)
        if self.metadata.get("category") and "category" not in state:
            state["category"] = self.metadata.get("category")
        if self.started_at is not None:
            state.setdefault("started_at", self.started_at)
        if self.finished_at is not None:
            state.setdefault("finished_at", self.finished_at)
        if self.started_at is not None:
            end = self.finished_at if self.finished_at is not None else time.time()
            state.setdefault("duration_seconds", max(0.0, end - self.started_at))
        return state

    def _run(self):
        self.started_at = time.time()
        try:
            import inspect as _ins
            sig = _ins.signature(self._fn)
            if len(sig.parameters) >= 3:
                ret = self._fn(self.log, self._cancel, self.update_state)
            elif len(sig.parameters) >= 2:
                ret = self._fn(self.log, self._cancel)
            else:
                ret = self._fn(self.log)
            if ret is not None:
                self.result = ret
            self.returncode = 0
        except Exception as exc:
            self.log.append(f"ERROR: {exc}")
            self.returncode = 1
        finally:
            self.finished_at = time.time()

    def to_dict(self, include_log=False, include_result=True):
        d = {
            "job_id":      self.job_id,
            "label":       self.label,
            "status":      self.status,
            "created_at":  self.created_at,
            "started_at":  self.started_at,
            "finished_at": self.finished_at,
            "returncode":  self.returncode,
            "log_lines":   len(self.log),
        }
        if include_log:
            d["log"] = self.log
        if self.result is not None:
            if include_result:
                d["result"] = self.result
            else:
                d["result_summary"] = _summarize_result(self.result)
        if self.metadata:
            d["metadata"] = self.metadata
        with self._lock:
            has_structured_state = bool(self.state)
        if has_structured_state or self.metadata:
            d["state"] = self._state_snapshot()
        return d


class JobStore:
    def __init__(self):
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(self, command, label="") -> Job:
        with self._lock:
            jid  = uuid.uuid4().hex
            job  = Job(jid, command, label)
            self._jobs[jid] = job
            return job

    def start_python(self, fn, label="", metadata=None) -> PythonJob:
        with self._lock:
            jid  = uuid.uuid4().hex
            job  = PythonJob(jid, fn, label)
            if metadata:
                job.metadata = metadata
            self._jobs[jid] = job
            return job

    def get(self, jid) -> Optional[Job]:
        return self._jobs.get(jid)

    def all(self) -> List[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def clear_finished(self):
        with self._lock:
            self._jobs = {k: v for k, v in self._jobs.items() if v.status == "running"}

    def prune_finished(self, *, max_age_seconds=21600,
                       metadata_max_age_seconds=604800,
                       max_finished=250):
        """Prune old finished jobs without wiping recent operator-visible history.

        Manual "clear done" still uses clear_finished(). This is for automatic
        maintenance paths that need to cap memory growth while keeping recent
        Jobs rows, logs, metadata, and PythonJob result payloads available.
        """
        now = time.time()
        max_age = max(0.0, float(max_age_seconds))
        metadata_max_age = max(max_age, float(metadata_max_age_seconds))
        max_finished = max(0, int(max_finished or 0))
        with self._lock:
            running = {
                jid: job for jid, job in self._jobs.items()
                if job.status == "running"
            }
            keep_finished = []
            for jid, job in self._jobs.items():
                if jid in running:
                    continue
                finished_at = job.finished_at or job.created_at or now
                metadata = getattr(job, "metadata", {}) or {}
                has_type = bool(str(metadata.get("type") or "").strip())
                ttl = metadata_max_age if has_type else max_age
                if now - finished_at <= ttl:
                    keep_finished.append((jid, job, finished_at, has_type))

            if max_finished and len(keep_finished) > max_finished:
                keep_finished.sort(
                    key=lambda item: (item[3], item[2]),
                    reverse=True,
                )
                keep_finished = keep_finished[:max_finished]

            self._jobs = {
                **running,
                **{jid: job for jid, job, _finished_at, _has_type in keep_finished},
            }
