"""Job engine — PythonJob and JobStore, with optional durable persistence.

Statuses: ``running`` while a job runs; terminal ``success``, ``failed``,
``cancelled`` and ``recovery_required``.

Durability (ARCH-004), enabled when the store is given a persistence dir:

* Each job is written atomically to ``<dir>/<job_id>.json`` on every
  lifecycle transition (start, cancel request, finish) and, throttled, on
  progress: at most every ``PERSIST_INTERVAL_SECONDS``. The persisted record
  carries metadata, structured state (including any ``checkpoint`` a job
  publishes through ``update_state``), the log tail, the result and a
  ``heartbeat_at`` refreshed every ``HEARTBEAT_SECONDS`` while it runs.
* After a restart, a job persisted as running did not finish in this
  process. A read-only job (``READ_ONLY_JOB_TYPES``, or metadata
  ``mutating: False``) becomes ``failed`` -- re-running it is safe. Anything
  else becomes ``recovery_required``: completion cannot be proven, so it is
  never re-run automatically. Its last checkpoint is kept for the operator
  and for workflows that resume from their own persisted records.
* ``JobStore.close()`` stops the heartbeat and waits for every job thread,
  including its final write. A job's in-memory status turns terminal a
  moment before that write lands, so close the store before removing its
  directory.

Engine-backed mutations are additionally protected by their transaction's
idempotency key; see backend/transaction_recovery.py for how a transaction
left Running by a restart is finished from engine evidence, never replayed.

Duplicate starts (lost HTTP responses, retries, double clicks): the web layer
opens a ``start scope`` per mutating request (``enter_start_scope``). The
first job that request starts carries the request's fingerprint (method,
path, query, body) and, when the client sent one, its ``Idempotency-Key``:

* the same Idempotency-Key again returns the job it already started, in any
  status (also after a restart, while the record is retained), and never
  starts a second one -- ``DuplicateJobError`` with ``replay=True``;
* without a key, an identical request while that job is still running is
  refused with ``DuplicateJobError`` (read-only jobs are exempt).

Recurring work: ``JobStore.every`` runs a tick on a store-owned schedule
(stopped by ``close()``); the tick starts tracked jobs with ``start_python``.
``PythonJob.add_done_callback`` replaces "poll until finished" watcher threads.

Persisted ``log_tail`` and ``result`` are redacted centrally (#272).
"""
import contextlib
import contextvars
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

TERMINAL_STATUSES = ("success", "failed", "cancelled", "recovery_required")
PERSIST_INTERVAL_SECONDS = 2.0
HEARTBEAT_SECONDS = 15.0
LOG_TAIL_PERSISTED = 500
RESULT_PERSIST_LIMIT_BYTES = 1_000_000

#: Job types that never mutate the library or files: after an interrupted
#: run they are simply failed (safe to start again).
READ_ONLY_JOB_TYPES = frozenset({
    "dedup-scan", "dedup-ai-review", "untracked-inventory", "library-health-scan", "leaked-db-path-scan",
    "folder-placeholder-scan", "artist-folder-scan", "root-folder-repair-scan", "album-folder-cleanup-scan",
    "music-format-scan", "musicbrainz-match", "batch-ai-suggest", "ytdlp-youtube-test",
})

_log = logging.getLogger("app.job_engine")

# "scheme://user:pass@" -- provider_boundary.redact covers key=value, Bearer
# and sensitive dict keys, but not credentials embedded in a URL.
_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s/@:]+:[^\s/@]+@")


def redact_for_persistence(value: Any) -> Any:
    """Scrub secret-looking text from a job's log/result before it is written
    to disk (#272). Producers still redact their own lines; this is the
    backstop for one that forgets."""
    from backend.provider_boundary import redact  # stdlib-only module; lazy keeps job_engine import-light

    def userinfo(v):
        if isinstance(v, str):
            return _URL_USERINFO_RE.sub(r"\1[REDACTED]@", v)
        if isinstance(v, dict):
            return {k: userinfo(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [userinfo(x) for x in v]
        return v

    return redact(userinfo(value))


class StartScope:
    """What one HTTP request contributes to the first job it starts."""

    def __init__(self, fingerprint_fn, idempotency_key: str = ""):
        self._fingerprint_fn = fingerprint_fn
        self._fingerprint: Optional[str] = None
        self.idempotency_key = idempotency_key
        self.used = False

    @property
    def fingerprint(self) -> str:
        if self._fingerprint is None:  # lazy: the view has parsed its body by now
            try:
                self._fingerprint = str(self._fingerprint_fn() or "")
            except Exception:
                self._fingerprint = ""
        return self._fingerprint


_start_scope: "contextvars.ContextVar[Optional[StartScope]]" = contextvars.ContextVar("job_start_scope", default=None)


def enter_start_scope(fingerprint_fn, idempotency_key: str = ""):
    """Open the start scope for the current request; returns a token for
    ``exit_start_scope``. The web layer calls this, never a job."""
    return _start_scope.set(StartScope(fingerprint_fn, idempotency_key))


def exit_start_scope(token) -> None:
    try:
        _start_scope.reset(token)
    except (ValueError, RuntimeError):
        _start_scope.set(None)


def _active_scope() -> Optional[StartScope]:
    scope = _start_scope.get()
    return scope if scope is not None and not scope.used else None


class IdempotencyKeyReuseError(ValueError):
    """An Idempotency-Key was sent again with a different request."""

    def __init__(self, job: "PythonJob"):
        super().__init__("This Idempotency-Key was already used for a different request; "
                         "send a new key for a new operation.")
        self.job = job


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


class CancelSignal(threading.Event):
    """A job's cancel event that remembers whether the job saw the request.

    ``is_set()``/``wait()`` returning True mark the request ``observed``: the
    job had the chance to stop. ``requested`` reads the flag without marking.
    """

    observed = False

    @property
    def requested(self) -> bool:
        return super().is_set()

    def is_set(self) -> bool:
        if super().is_set():
            self.observed = True
            return True
        return False

    def wait(self, timeout: Optional[float] = None) -> bool:
        if super().wait(timeout):
            self.observed = True
            return True
        return False


def cancel_honoured(cancel: Any, exc: Optional[BaseException] = None) -> bool:
    """Did a finished job function stop because of a cancel request?

    True when it raised ``"cancelled"``, or when a cancel was requested and
    the function saw it (it had the chance to stop). A request the function
    never saw arrived after its work was done, or while a step it does not
    interrupt ran: the real outcome stands. Job status and the hook-created
    transaction both use this rule.
    """
    if exc is not None and str(exc).strip().lower() == "cancelled":
        return True
    observed = getattr(cancel, "observed", None)
    if observed is None:  # a plain Event (not started by a JobStore)
        return bool(cancel is not None and cancel.is_set())
    return bool(observed)


def cancel_requested(cancel: Any) -> bool:
    """Whether a cancel was requested, without marking it observed."""
    requested = getattr(cancel, "requested", None)
    return bool(requested) if requested is not None else bool(cancel is not None and cancel.is_set())


class PythonJob:
    """Runs a Python callable in a background thread.
    The callable receives (log, cancel_event) and should periodically check
    cancel_event.is_set() to exit early.  Callables may also accept a third
    update_state callback for structured progress.  If the callable returns a
    dict, the result is stored in self.result and included in to_dict()."""
    def __init__(self, job_id: str, fn, label: str = "", *, persist=None, start: bool = True):
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
        self.heartbeat_at: Optional[float] = None
        self.recovery: Optional[Dict[str, Any]] = None
        self._terminal: Optional[str]     = None  # set only for recovered records
        # {"fingerprint", "idempotency_key"} of the request that started it.
        self.start_keys: Dict[str, str]   = {}
        self._done_callbacks: List[Any]   = []
        self._lock        = threading.Lock()
        self._fn          = fn
        self._cancel      = CancelSignal()
        self._persist     = persist
        self._last_persist = 0.0
        self._thread: Optional[threading.Thread] = None
        if start and fn is not None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    @property
    def status(self):
        if self._terminal:
            return self._terminal
        if self.finished_at is not None:
            if self.returncode == -1:  # a late cancel never rewrites the outcome
                return "cancelled"
            return "success" if self.returncode == 0 else "failed"
        return "running"

    def kill(self):
        """Request cancellation.  The job must co-operatively check _cancel."""
        self._cancel.set()
        self.log.append("[cancel requested]")
        self.save(force=True)

    def update_state(self, updates: Optional[Dict[str, Any]] = None, **kwargs):
        """Merge structured progress fields for API consumers.

        This is intentionally additive and optional so older jobs that only
        produce readable/raw log output continue to behave exactly as before.
        A job may publish a ``checkpoint`` here; it is persisted with the job.
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
        self.save(force="checkpoint" in payload)

    def save(self, force: bool = False) -> None:
        """Persist this job (throttled unless ``force``); no-op when the store
        has no persistence directory."""
        if self._persist is None:
            return
        now = time.time()
        if not force and now - self._last_persist < PERSIST_INTERVAL_SECONDS:
            return
        self._last_persist = now
        try:
            self._persist(self)
        except Exception:
            pass

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
        self.heartbeat_at = self.started_at
        self.save(force=True)
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
            if self._cancel.requested and cancel_honoured(self._cancel):
                self.returncode = -1
            elif reports_failure(ret):
                # BA-3: a job that returns {"ok": False, ...} failed, even
                # though it did not raise.
                self.returncode = 1
                self.log.append(f"ERROR: {str(ret.get('error') or 'the job reported a failed result')[:300]}")
            else:
                self.returncode = 0
            if self.returncode != -1 and self._cancel.requested:
                self.log.append("[cancel requested, but the job had already done its work: "
                                f"recorded as {'failed' if self.returncode else 'success'}]")
        except Exception as exc:
            if cancel_honoured(self._cancel, exc):
                self.returncode = -1
                if not any("cancel" in str(line).lower() for line in self.log[-3:]):
                    self.log.append("Job cancelled by user.")
            else:
                self.log.append(f"ERROR: {exc}")
                self.returncode = 1
        finally:
            self.finished_at = time.time()
            self.save(force=True)
            self._run_done_callbacks()

    def add_done_callback(self, fn) -> None:
        """Call ``fn(job)`` once the job is terminal (at once if it already is).
        Use it instead of a thread that polls the job until it finishes."""
        with self._lock:
            if self.status == "running":
                self._done_callbacks.append(fn)
                return
        self._call_done(fn)

    def _run_done_callbacks(self) -> None:
        with self._lock:
            callbacks, self._done_callbacks = self._done_callbacks, []
        for fn in callbacks:
            self._call_done(fn)

    def _call_done(self, fn) -> None:
        try:
            fn(self)
        except Exception:
            _log.exception("job %s: done callback failed", self.job_id)

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
        if self.recovery:
            d["recovery"] = self.recovery
            # The process restarted while this job ran (see _recover).
            d["interrupted"] = True
        with self._lock:
            has_structured_state = bool(self.state)
        if has_structured_state or self.metadata:
            d["state"] = self._state_snapshot()
        return d

    # -- durable record ---------------------------------------------------
    def to_record(self) -> Dict[str, Any]:
        with self._lock:
            state = dict(self.state)
        result = self.result
        try:
            if len(json.dumps(result, default=str)) > RESULT_PERSIST_LIMIT_BYTES:
                result = {"truncated": True, "summary": _summarize_result(result)}
        except Exception:
            result = {"truncated": True, "summary": _summarize_result(result)}
        return {
            "version": 1,
            "job_id": self.job_id, "label": self.label, "status": self.status,
            "created_at": self.created_at, "started_at": self.started_at, "finished_at": self.finished_at,
            "returncode": self.returncode, "heartbeat_at": self.heartbeat_at,
            "metadata": self.metadata, "state": state,
            "log_tail": redact_for_persistence(list(self.log[-LOG_TAIL_PERSISTED:])),
            "log_lines": len(self.log), "result": redact_for_persistence(result), "recovery": self.recovery,
            "start_keys": self.start_keys,
        }

    @classmethod
    def from_record(cls, rec: Dict[str, Any]) -> "PythonJob":
        job = cls(rec.get("job_id") or uuid.uuid4().hex, None, rec.get("label") or "", start=False)
        job.created_at = rec.get("created_at") or time.time()
        job.started_at = rec.get("started_at")
        job.finished_at = rec.get("finished_at")
        job.returncode = rec.get("returncode")
        job.heartbeat_at = rec.get("heartbeat_at")
        job.metadata = dict(rec.get("metadata") or {})
        job.state = dict(rec.get("state") or {})
        job.log = list(rec.get("log_tail") or [])
        job.result = rec.get("result")
        job.recovery = rec.get("recovery")
        job.start_keys = dict(rec.get("start_keys") or {})
        status = rec.get("status")
        if status in TERMINAL_STATUSES:
            job._terminal = status
        return job


def reports_failure(result: Any) -> bool:
    """True for the failure shape jobs and services return: ``{"ok": False}``."""
    return isinstance(result, dict) and result.get("ok") is False


class DuplicateJobError(RuntimeError):
    """A job with the same ``dedupe_key`` or request fingerprint is already
    running (BA-6, ARCH-004), or -- ``replay`` -- this request's
    Idempotency-Key already started ``job``. Nothing new was started."""

    def __init__(self, job: "PythonJob", replay: bool = False):
        if replay:
            message = (f"This request already started a job ({job.label or job.job_id}, {job.status}); "
                       "it was not started again.")
        else:
            message = (f"This job is already running ({job.label or job.job_id}); "
                       "wait for it to finish before starting it again.")
        super().__init__(message)
        self.job = job
        self.replay = replay


def dedupe_key(metadata: Optional[Dict[str, Any]]) -> str:
    """The caller's explicit ``metadata["dedupe_key"]``, or "" for no guard.

    Opt-in on purpose: a label or metadata often leaves out the input that
    makes two starts different (a folder path, an album set, dry run), so
    an implicit key refused legitimate jobs. A caller opts in only when its
    key names the whole input, e.g. a library-wide job with no parameters."""
    return str((metadata or {}).get("dedupe_key") or "")


def is_read_only_job(metadata: Optional[Dict[str, Any]]) -> bool:
    metadata = metadata or {}
    if metadata.get("mutating") is False:
        return True
    if metadata.get("mutating") is True:
        return False
    return str(metadata.get("type") or "") in READ_ONLY_JOB_TYPES


class JobStore:
    """Web Manager's user-facing workflow job store.

    Only PythonJob (a local Python callable run in a background thread) is
    supported -- there is no generic remote "beet command job" abstraction.
    Long-running stock-Beets mutations use the webmanager integration
    plugin's own operation-id polling (see backend/beets_adapter.py and
    beetsplug/webmanager/operations.py), orchestrated from inside a
    PythonJob when Web Manager needs to show it in the Jobs page.

    With ``persistence_dir`` every job is also a durable record there (see
    the module docstring); without it the store is purely in memory.
    """

    def __init__(self, persistence_dir: Optional[Union[str, Path]] = None):
        self._jobs: Dict[str, "PythonJob"] = {}
        self._lock = threading.RLock()  # re-entered via start_guard()
        self._write_lock = threading.Lock()
        self.root: Optional[Path] = Path(persistence_dir) if persistence_dir else None
        self.recovered: List[Dict[str, Any]] = []
        self._closed = threading.Event()
        self._heartbeat: Optional[threading.Thread] = None
        # Every started job thread, also after clear/prune drops its job:
        # close() must join a thread whose final write is still in flight.
        self._threads: List[threading.Thread] = []
        self._schedules: Dict[str, threading.Thread] = {}
        if self.root is not None:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.root = None
        if self.root is not None:
            self._recover()
            self._heartbeat = threading.Thread(target=self._heartbeat_loop, daemon=True, name="job-heartbeat")
            self._heartbeat.start()

    # -- persistence ------------------------------------------------------
    def _write(self, job: "PythonJob") -> None:
        if self.root is None:
            return
        target = self.root / f"{job.job_id}.json"
        tmp = self.root / f".{job.job_id}.{threading.get_ident()}.tmp"
        with self._write_lock:
            # A cleared or pruned job is never written back: its thread's
            # final save can land after the record was deleted (#286).
            if self._jobs.get(job.job_id) is not job:
                return
            # Snapshot inside the lock: the last writer always persists the
            # current state (a stale "running" can never land after a finish).
            record = job.to_record()
            tmp.write_text(json.dumps(record, default=str), encoding="utf-8")
            for attempt in range(50):
                try:
                    os.replace(tmp, target)
                    break
                except PermissionError:  # Windows: a reader holds the target open
                    if attempt == 49:
                        raise
                    time.sleep(0.01)

    def _delete(self, jid: str) -> None:
        if self.root is None:
            return
        with self._write_lock:  # never between a write's check and its replace
            try:
                (self.root / f"{jid}.json").unlink()
            except OSError:
                pass

    def _recover(self) -> None:
        """Load persisted jobs; resolve those this process never finished."""
        now = time.time()
        for path in sorted(self.root.glob("*.json")):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
                job = PythonJob.from_record(rec)
            except Exception:
                continue
            job._persist = self._write
            self._jobs[job.job_id] = job
            if rec.get("status") == "running":
                read_only = is_read_only_job(job.metadata)
                job._terminal = "failed" if read_only else "recovery_required"
                job.finished_at = job.finished_at or now
                job.returncode = 1
                job.recovery = {
                    "interrupted_at": now,
                    "last_heartbeat_at": rec.get("heartbeat_at"),
                    "checkpoint": (rec.get("state") or {}).get("checkpoint"),
                    "resolution": ("failed: read-only job, safe to start again" if read_only else
                                   "recovery_required: the process stopped before this job finished; "
                                   "it is not re-run automatically"),
                }
                job.log.append(f"[{job._terminal}: the Web Manager process restarted while this job ran]")
                self._write(job)
                self.recovered.append({"job_id": job.job_id, "status": job._terminal,
                                       "type": job.metadata.get("type")})

    def _heartbeat_loop(self) -> None:
        while not self._closed.wait(HEARTBEAT_SECONDS):
            for job in self.all():
                if job.status == "running":
                    job.heartbeat_at = time.time()
                    job.save(force=True)

    # -- API ------------------------------------------------------------------
    @staticmethod
    def _running_key(metadata) -> str:
        """The key a new start is compared on: the explicit ``dedupe_key``,
        else the starting request's fingerprint (mutating jobs only)."""
        key = dedupe_key(metadata)
        scope = _active_scope()
        if key or scope is None or is_read_only_job(metadata):
            return key
        return "request:" + scope.fingerprint if scope.fingerprint else ""

    @staticmethod
    def _job_key(job: "PythonJob") -> str:
        key = dedupe_key(job.metadata)
        fingerprint = job.start_keys.get("fingerprint")
        return key or ("request:" + fingerprint if fingerprint else "")

    def find_duplicate(self, metadata=None) -> Optional["PythonJob"]:
        """The running job a new start with ``metadata`` would duplicate.

        Raises DuplicateJobError(replay=True) when the current request's
        Idempotency-Key already started a job (any status), and
        IdempotencyKeyReuseError when that key came with a different request."""
        scope = _active_scope()
        if scope is not None and scope.idempotency_key:
            for job in list(self._jobs.values()):
                if job.start_keys.get("idempotency_key") == scope.idempotency_key:
                    if job.start_keys.get("fingerprint") != scope.fingerprint:
                        raise IdempotencyKeyReuseError(job)
                    raise DuplicateJobError(job, replay=True)
        key = self._running_key(metadata)
        if not key:
            return None
        for job in list(self._jobs.values()):
            if job.status == "running" and self._job_key(job) == key:
                return job
        return None

    def start_guard(self, metadata=None):
        """Hold across a caller's own duplicate check and the start it guards,
        so two racing starts cannot both pass the check (QA F3)."""
        scope = _active_scope()
        guarded = self._running_key(metadata) or (scope is not None and scope.idempotency_key)
        return self._lock if guarded else contextlib.nullcontext()

    def start_python(self, fn, label="", metadata=None) -> PythonJob:
        """Start ``fn`` as a job. Raises DuplicateJobError when a job with the
        same explicit ``metadata["dedupe_key"]``, or started by an identical
        request, is still running, or when this request's Idempotency-Key
        already started a job (see the module docstring)."""
        with self._lock:
            existing = self.find_duplicate(metadata)
            if existing is not None:
                raise DuplicateJobError(existing)
            jid  = uuid.uuid4().hex
            job  = PythonJob(jid, fn, label, persist=self._write if self.root else None, start=False)
            if metadata:
                job.metadata = metadata
            scope = _active_scope()
            if scope is not None:
                # Only the first job a request starts carries its keys: a
                # request that starts several jobs is guarded by the first.
                scope.used = True
                job.start_keys = {k: v for k, v in (("fingerprint", scope.fingerprint),
                                                    ("idempotency_key", scope.idempotency_key)) if v}
            self._jobs[jid] = job
        try:
            thread = threading.Thread(target=job._run, daemon=True)
            thread.start()
            job._thread = thread  # only a started thread can be joined by close()
            with self._lock:
                self._threads = [t for t in self._threads if t.is_alive()] + [thread]
        except BaseException as exc:
            # #229: a job whose thread never started must not show as running.
            job.returncode = 1
            job.finished_at = time.time()
            job.log.append(f"ERROR: the job could not be started: {exc}")
            job.save(force=True)
            raise
        return job

    def every(self, name: str, interval_seconds: float, tick, *, initial_delay: float = 0.0) -> threading.Thread:
        """Call ``tick()`` every ``interval_seconds`` until ``close()``.

        The scheduler hook for recurring work: ``tick`` decides whether work
        is due and starts it as a tracked job with ``start_python`` (it may
        return that job). While the job a tick returned is still running,
        later ticks are skipped, so a slow run is never overlapped. A tick
        that raises is logged and the schedule continues. One schedule per
        ``name``; a second ``every`` with the same name raises ValueError."""
        def loop():
            last: Optional[PythonJob] = None
            if self._closed.wait(max(0.0, initial_delay)):
                return
            while True:
                if last is None or last.status != "running":
                    try:
                        returned = tick()
                        last = returned if isinstance(returned, PythonJob) else None
                    except Exception:
                        _log.exception("scheduled job tick %r failed", name)
                if self._closed.wait(max(0.01, interval_seconds)):
                    return

        with self._lock:
            if name in self._schedules:
                raise ValueError(f"schedule {name!r} already exists")
            thread = threading.Thread(target=loop, daemon=True, name=f"job-schedule-{name}")
            self._schedules[name] = thread
        thread.start()
        return thread

    def close(self, timeout: float = 10.0) -> bool:
        """Stop the heartbeat and schedules and wait (up to ``timeout`` in
        all) for every job thread, including its final persisted write, to
        finish. Jobs are not cancelled. True when no store thread is still
        alive."""
        self._closed.set()
        deadline = time.monotonic() + timeout
        with self._lock:
            threads = [self._heartbeat] + list(self._schedules.values()) + list(self._threads)
        for thread in threads:
            if thread is not None and thread is not threading.current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))
        return not any(t is not None and t.is_alive() for t in threads)

    def get(self, jid) -> Optional["PythonJob"]:
        return self._jobs.get(jid)

    def all(self) -> List["PythonJob"]:
        return sorted(list(self._jobs.values()), key=lambda j: j.created_at, reverse=True)

    def clear_finished(self):
        """Manual "clear done". Keeps running jobs and ``recovery_required``
        records, which the operator still has to resolve (BA-10)."""
        keep = ("running", "recovery_required")
        with self._lock:
            removed = [k for k, v in self._jobs.items() if v.status not in keep]
            self._jobs = {k: v for k, v in self._jobs.items() if v.status in keep}
        for jid in removed:
            self._delete(jid)

    def prune_finished(self, *, max_age_seconds=21600,
                       metadata_max_age_seconds=604800,
                       max_finished=250):
        """Prune old finished jobs without wiping recent operator-visible history.

        Manual "clear done" still uses clear_finished(). This is for automatic
        maintenance paths that need to cap memory growth while keeping recent
        Jobs rows, logs, metadata, and PythonJob result payloads available.
        ``recovery_required`` records are never pruned automatically.
        """
        now = time.time()
        max_age = max(0.0, float(max_age_seconds))
        metadata_max_age = max(max_age, float(metadata_max_age_seconds))
        max_finished = max(0, int(max_finished or 0))
        with self._lock:
            before = set(self._jobs)
            running = {
                jid: job for jid, job in self._jobs.items()
                if job.status in ("running", "recovery_required")
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
            removed = before - set(self._jobs)
        for jid in removed:
            self._delete(jid)
