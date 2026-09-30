"""The shared contract for long-running mutating jobs (ARCH-004).

A workflow that adopts the contract gets, without changing its own logic or
its HTTP behaviour:

* **Operation identity and duplicate-start prevention across processes and
  restarts.** The job holds the durable lock ``workflow:<name>``
  (backend/resource_locks.py) for as long as it runs. A second process
  starting the same workflow waits for it; a lock left by a process that died
  is reclaimed once its heartbeat expires, so a restart never runs two.
* **A heartbeat** kept fresh while the job runs.
* **A persisted checkpoint** in the durable job record: which workflow, which
  locks, which stage -- and, when the workflow passes ``progress``, its own
  resumable position, republished whenever it changes. After a restart the
  job is ``recovery_required`` (job_engine) and this checkpoint is what the
  operator and the workflow's own resume logic read.
* **Cancellation** honoured while waiting for the lock.

In-process guards a workflow already has (an import slot, a per-playlist
lock, a reservation registry) stay; the contract is the layer that makes them
hold across processes and restarts.
"""

from __future__ import annotations

import hashlib
import inspect
import re
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterable, List, Optional

from backend.resource_locks import HOST, PID, ResourceLockConflictError, locks as resource_locks

#: Longer than a lock's ttl, so a lock left by a dead process is reclaimed
#: (and the job proceeds) before the wait gives up.
DEFAULT_WAIT_SECONDS = 180.0
LOCK_TTL_SECONDS = 120.0
PROGRESS_INTERVAL_SECONDS = 15.0

JOB_CLASSIFICATION_RESUMABLE_COMPUTATION = "resumable_computation"
JOB_CLASSIFICATION_ENGINE_BACKED_MUTATION = "engine_backed_mutation"
JOB_CLASSIFICATION_NON_RESUMABLE_SIDE_EFFECT = "non_resumable_side_effect"
VALID_JOB_CLASSIFICATIONS = (
    JOB_CLASSIFICATION_RESUMABLE_COMPUTATION,
    JOB_CLASSIFICATION_ENGINE_BACKED_MUTATION,
    JOB_CLASSIFICATION_NON_RESUMABLE_SIDE_EFFECT,
)


class WorkflowBusyError(RuntimeError):
    """The workflow is running in another process and did not finish in time."""


class PermanentJobError(Exception):
    """An unrecoverable job failure that must not be retried."""


def slug(text: Any) -> str:
    """A stable lock-safe id for a free-text workflow subject (a playlist
    name, a folder path)."""
    return hashlib.sha256(str(text or "").strip().lower().encode("utf-8")).hexdigest()[:16]


def workflow_key(workflow: str) -> str:
    return f"workflow:{workflow}"


def normalize_lock_key(key: str) -> str:
    key = str(key or "").strip()
    from backend.resource_locks import _KEY_RE
    if _KEY_RE.match(key):
        return key
    prefix = key.split(":", 1)[0].lower() if ":" in key else "res"
    safe_prefix = re.sub(r"[^a-z0-9]", "", prefix)[:10] or "res"
    return f"workflow:{safe_prefix}-{slug(key)}"


def make_checkpoint(
    *,
    workflow: str,
    workflow_version: str = "1.0",
    job_id: Optional[str] = None,
    operation_id: Optional[str] = None,
    resource_keys: Optional[Iterable[str]] = None,
    stage: Optional[str] = None,
    phase: Optional[str] = None,
    unit_index: int = 0,
    unit_identity: Optional[str] = None,
    completed_units: int = 0,
    pending_units: int = 0,
    total_units: Optional[int] = None,
    last_safe_checkpoint: Optional[Dict[str, Any]] = None,
    engine_operation_id: Optional[str] = None,
    retry_count: int = 0,
    provider_retry_state: Optional[Dict[str, Any]] = None,
    classification: str = JOB_CLASSIFICATION_NON_RESUMABLE_SIDE_EFFECT,
    cancel_requested: bool = False,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a standardized ARCH-004 checkpoint dictionary."""
    now = time.time()
    st = stage or phase or "initialized"
    keys = list(resource_keys or [workflow_key(workflow)])
    return {
        "workflow": workflow,
        "workflow_version": str(workflow_version),
        "stage": st,
        "phase": st,
        "job_id": job_id,
        "operation_id": operation_id,
        "lock_keys": keys,
        "resource_keys": keys,
        "unit_index": int(unit_index),
        "unit_identity": unit_identity,
        "completed_units": int(completed_units),
        "pending_units": int(pending_units),
        "total_units": total_units,
        "last_safe_checkpoint": last_safe_checkpoint,
        "engine_operation_id": engine_operation_id,
        "retry_count": int(retry_count),
        "provider_retry_state": provider_retry_state,
        "classification": classification,
        "created_at": now,
        "updated_at": now,
        "heartbeat": now,
        "cancel_requested": bool(cancel_requested),
        "extra": dict(extra or {}),
    }


class JobCheckpointController:
    """Manages publishing and updating standardized checkpoint state during execution."""

    def __init__(
        self,
        workflow: str,
        *,
        update_state: Optional[Callable[..., None]] = None,
        classification: str = JOB_CLASSIFICATION_NON_RESUMABLE_SIDE_EFFECT,
        workflow_version: str = "1.0",
        resource_keys: Optional[Iterable[str]] = None,
        job_id: Optional[str] = None,
        operation_id: Optional[str] = None,
    ):
        self.workflow = workflow
        self.workflow_version = workflow_version
        self.resource_keys = list(resource_keys or [workflow_key(workflow)])
        self.classification = classification
        self.job_id = job_id
        self.operation_id = operation_id
        self.update_state = update_state
        self.state: Dict[str, Any] = make_checkpoint(
            workflow=workflow,
            workflow_version=workflow_version,
            job_id=job_id,
            operation_id=operation_id,
            resource_keys=self.resource_keys,
            classification=classification,
            stage="running",
            phase="running",
        )
        self._publish()

    def update(
        self,
        *,
        stage: Optional[str] = None,
        phase: Optional[str] = None,
        unit_index: Optional[int] = None,
        unit_identity: Optional[str] = None,
        completed_units: Optional[int] = None,
        pending_units: Optional[int] = None,
        total_units: Optional[int] = None,
        engine_operation_id: Optional[str] = None,
        retry_count: Optional[int] = None,
        provider_retry_state: Optional[Dict[str, Any]] = None,
        cancel_requested: Optional[bool] = None,
        extra: Optional[Dict[str, Any]] = None,
        is_safe_checkpoint: bool = False,
    ) -> Dict[str, Any]:
        now = time.time()
        st = stage or phase
        if st is not None:
            self.state["stage"] = st
            self.state["phase"] = st
        if unit_index is not None:
            self.state["unit_index"] = unit_index
        if unit_identity is not None:
            self.state["unit_identity"] = unit_identity
        if completed_units is not None:
            self.state["completed_units"] = completed_units
        if pending_units is not None:
            self.state["pending_units"] = pending_units
        if total_units is not None:
            self.state["total_units"] = total_units
        if engine_operation_id is not None:
            self.state["engine_operation_id"] = engine_operation_id
        if retry_count is not None:
            self.state["retry_count"] = retry_count
        if provider_retry_state is not None:
            self.state["provider_retry_state"] = provider_retry_state
        if cancel_requested is not None:
            self.state["cancel_requested"] = cancel_requested
        if extra:
            self.state.setdefault("extra", {}).update(extra)
        self.state["updated_at"] = now
        self.state["heartbeat"] = now
        if is_safe_checkpoint:
            self.state["last_safe_checkpoint"] = {
                k: v for k, v in self.state.items() if k != "last_safe_checkpoint"
            }
        self._publish()
        return self.state

    def record_engine_operation_before_request(self, engine_operation_id: str) -> None:
        """Record engine operation ID BEFORE dispatching request."""
        self.update(engine_operation_id=engine_operation_id, phase="dispatching_engine_operation")

    def _publish(self) -> None:
        if self.update_state:
            try:
                self.update_state({"checkpoint": dict(self.state)})
            except Exception:
                pass


def bounded_retry(
    fn: Callable[[], Any],
    *,
    max_attempts: int = 3,
    initial_backoff: float = 1.0,
    backoff_factor: float = 2.0,
    max_backoff: float = 30.0,
    retryable_exceptions: tuple = (Exception,),
    is_retryable: Optional[Callable[[Exception], bool]] = None,
    cancel_event: Optional[Any] = None,
    on_retry: Optional[Callable[[int, Exception, float], None]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Any:
    """Run `fn` with bounded retries, exponential backoff, and Retry-After support.

    Does not retry PermanentJobError or if `is_retryable` returns False.
    Checks `cancel_event` before and during backoff sleeps.
    """
    attempt = 0
    while True:
        attempt += 1
        if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
            raise RuntimeError("cancelled")
        try:
            return fn()
        except PermanentJobError:
            raise
        except retryable_exceptions as exc:
            if attempt >= max_attempts:
                raise
            if is_retryable is not None and not is_retryable(exc):
                raise
            # Check Retry-After header/attribute
            retry_after = getattr(exc, "retry_after", None)
            if retry_after is not None:
                try:
                    delay = min(float(retry_after), max_backoff)
                except (ValueError, TypeError):
                    delay = min(initial_backoff * (backoff_factor ** (attempt - 1)), max_backoff)
            else:
                delay = min(initial_backoff * (backoff_factor ** (attempt - 1)), max_backoff)

            if on_retry is not None:
                try:
                    on_retry(attempt, exc, delay)
                except Exception:
                    pass

            if sleep_fn is not time.sleep:
                sleep_fn(delay)
            else:
                start_sleep = time.time()
                while time.time() - start_sleep < delay:
                    if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                        raise RuntimeError("cancelled")
                    time.sleep(min(0.1, max(0.0, delay - (time.time() - start_sleep))))


def evaluate_restart_recovery(
    checkpoint: Dict[str, Any],
    *,
    adapter: Optional[Any] = None,
) -> Dict[str, Any]:
    """Evaluate whether an interrupted job can resume, is completed from evidence, or requires recovery.

    Returns a dict with:
      - 'action': 'resume' | 'finalize_completed' | 'recovery_required' | 'failed'
      - 'reason': explanation string
      - 'safe_checkpoint': safe state to resume from (if action == 'resume')
      - 'engine_result': result payload if finalized from engine
    """
    classification = checkpoint.get("classification", JOB_CLASSIFICATION_NON_RESUMABLE_SIDE_EFFECT)

    if classification == JOB_CLASSIFICATION_RESUMABLE_COMPUTATION:
        safe = checkpoint.get("last_safe_checkpoint") or checkpoint
        return {
            "action": "resume",
            "reason": "resumable computation: safe to resume from last deterministic checkpoint",
            "safe_checkpoint": safe,
        }

    if classification == JOB_CLASSIFICATION_ENGINE_BACKED_MUTATION:
        engine_op_id = checkpoint.get("engine_operation_id") or checkpoint.get("operation_id")
        if not engine_op_id:
            safe = checkpoint.get("last_safe_checkpoint")
            if safe:
                return {
                    "action": "resume",
                    "reason": "engine request was never dispatched: safe to resume from pre-apply checkpoint",
                    "safe_checkpoint": safe,
                }
            return {
                "action": "recovery_required",
                "reason": "interrupted before engine dispatch with no pre-apply checkpoint",
            }

        if adapter is not None:
            try:
                op_status = adapter.get_operation(engine_op_id)
                if op_status and op_status.get("status") in ("succeeded", "applied"):
                    return {
                        "action": "finalize_completed",
                        "reason": f"engine operation {engine_op_id} completed successfully; finalize local transaction",
                        "engine_result": op_status.get("result"),
                    }
                elif op_status and op_status.get("status") in ("failed", "compensated"):
                    return {
                        "action": "failed",
                        "reason": f"engine operation {engine_op_id} failed / compensated",
                    }
                elif op_status and op_status.get("status") in ("running", "applying"):
                    return {
                        "action": "recovery_required",
                        "reason": f"engine operation {engine_op_id} is still running on engine",
                    }
            except Exception:
                pass

        return {
            "action": "recovery_required",
            "reason": f"engine-backed mutation {engine_op_id} outcome is ambiguous; operator review required",
        }

    return {
        "action": "recovery_required",
        "reason": "interrupted non-resumable mutating job; operator review required",
    }


def contract_metadata(workflow: str, metadata: Optional[Dict[str, Any]] = None,
                      keys: Optional[Iterable[str]] = None,
                      resource_keys: Optional[Iterable[str]] = None,
                      classification: str = JOB_CLASSIFICATION_NON_RESUMABLE_SIDE_EFFECT) -> Dict[str, Any]:
    """Job metadata for a contract job: never treated as read-only unless resumable computation,
    and the durable record names its workflow, locks, and classification."""
    out = dict(metadata or {})
    if classification == JOB_CLASSIFICATION_RESUMABLE_COMPUTATION:
        out.setdefault("mutating", False)
    else:
        out.setdefault("mutating", True)
    actual_keys = [normalize_lock_key(k) for k in (resource_keys or keys or [workflow_key(workflow)])]
    out["workflow_contract"] = {
        "workflow": workflow,
        "lock_keys": actual_keys,
        "resource_keys": actual_keys,
        "classification": classification,
    }
    return out


def _publish(update_state: Optional[Callable[..., None]], checkpoint: Dict[str, Any]) -> None:
    if update_state is None:
        return
    try:
        update_state({"checkpoint": checkpoint})
    except Exception:
        pass


@contextmanager
def held(workflow: str, *, log: Optional[List[str]] = None, cancel_event: Any = None,
         update_state: Optional[Callable[..., None]] = None, keys: Optional[Iterable[str]] = None,
         resource_keys: Optional[Iterable[str]] = None,
         wait_seconds: float = DEFAULT_WAIT_SECONDS, progress: Optional[Callable[[], Dict[str, Any]]] = None,
         fail_fast_in_process: bool = False,
         classification: str = JOB_CLASSIFICATION_NON_RESUMABLE_SIDE_EFFECT):
    """Hold the workflow's durable lock(s) for the duration of the block.

    Waits (checking ``cancel_event``) while another process holds them;
    raises WorkflowBusyError after ``wait_seconds``. Publishes the contract
    checkpoint, and ``progress()`` whenever it changes, to ``update_state``.

    ``fail_fast_in_process``: a holder in THIS process is a live duplicate,
    not a stale lock -- raise WorkflowBusyError at once instead of waiting
    (for workflows with no in-process guard of their own)."""
    wanted = [normalize_lock_key(k) for k in (resource_keys or keys or [workflow_key(workflow)])]
    owner = f"job:{workflow}:{uuid.uuid4().hex[:12]}"
    registry = resource_locks()
    base = make_checkpoint(
        workflow=workflow,
        resource_keys=wanted,
        stage="running",
        phase="running",
        classification=classification,
    )
    base["owner"] = owner
    deadline = time.time() + max(0.0, wait_seconds)
    announced = False
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("cancelled")
        try:
            registry.acquire(wanted, owner, timeout=0.0, ttl=LOCK_TTL_SECONDS)
            break
        except ResourceLockConflictError as ex:
            same_process = ex.holder.get("host") == HOST and ex.holder.get("pid") == PID
            if fail_fast_in_process and same_process:
                raise WorkflowBusyError(f"{workflow} is already running (lock {ex.key})") from ex
            if time.time() >= deadline:
                raise WorkflowBusyError(
                    f"{workflow} is already running in another process (lock {ex.key} held by "
                    f"{ex.holder.get('owner')})") from ex
            if not announced:
                announced = True
                if log is not None:
                    log.append(f"[{workflow}] Waiting: the same workflow holds its lock in another process.")
                waiting_cp = dict(base)
                waiting_cp["stage"] = "waiting_for_lock"
                waiting_cp["phase"] = "waiting_for_lock"
                waiting_cp["at"] = time.time()
                waiting_cp["updated_at"] = time.time()
                _publish(update_state, waiting_cp)
            time.sleep(1.0)

    stop = threading.Event()
    state = {"progress": None}

    def beat():
        last_progress, last_lock = 0.0, time.time()
        while not stop.wait(1.0 if progress else LOCK_TTL_SECONDS / 3.0):
            now = time.time()
            try:
                if now - last_lock >= LOCK_TTL_SECONDS / 3.0 - 1.0:
                    last_lock = now
                    registry.heartbeat(owner)
                if progress and now - last_progress >= PROGRESS_INTERVAL_SECONDS:
                    last_progress = now
                    current = progress()
                    if current != state["progress"]:
                        state["progress"] = current
                        running_cp = dict(base)
                        running_cp["stage"] = "running"
                        running_cp["phase"] = "running"
                        running_cp["at"] = now
                        running_cp["updated_at"] = now
                        running_cp["progress"] = current
                        _publish(update_state, running_cp)
            except Exception:
                pass

    running_init = dict(base)
    running_init["stage"] = "running"
    running_init["phase"] = "running"
    running_init["at"] = time.time()
    _publish(update_state, running_init)
    thread = threading.Thread(target=beat, daemon=True, name=f"job-contract-{workflow}")
    thread.start()
    try:
        yield owner
    finally:
        stop.set()
        thread.join(timeout=5)
        try:
            registry.release(wanted, owner)
        except Exception:
            pass


class Handle:
    """An entered ``held`` block for code that cannot use ``with`` (a long
    function with its own try/finally): ``close()`` it in that finally."""

    def __init__(self, manager):
        self._manager = manager
        self.owner = manager.__enter__()
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._manager.__exit__(None, None, None)


def enter(workflow: str, **kwargs: Any) -> Handle:
    """Enter the contract now; the caller must ``close()`` the handle."""
    return Handle(held(workflow, **kwargs))


def guarded(fn: Callable[..., Any], *, workflow: str, keys: Optional[Iterable[str]] = None,
            resource_keys: Optional[Iterable[str]] = None,
            wait_seconds: float = DEFAULT_WAIT_SECONDS,
            progress: Optional[Callable[[], Dict[str, Any]]] = None,
            fail_fast_in_process: bool = False,
            classification: str = JOB_CLASSIFICATION_NON_RESUMABLE_SIDE_EFFECT) -> Callable[..., Any]:
    """Wrap a job function (``fn(log[, cancel_event[, update_state]])``) so it
    runs under the contract. The result is what ``fn`` returns."""
    arity = len(inspect.signature(fn).parameters)
    wanted_keys = list(resource_keys or keys or [workflow_key(workflow)])

    def _contract_job(log, cancel_event=None, update_state=None):
        with held(workflow, log=log, cancel_event=cancel_event, update_state=update_state, keys=wanted_keys,
                  wait_seconds=wait_seconds, progress=progress, fail_fast_in_process=fail_fast_in_process,
                  classification=classification):
            if arity >= 3:
                return fn(log, cancel_event, update_state)
            if arity >= 2:
                return fn(log, cancel_event)
            return fn(log)

    # No __wrapped__: the job engine picks the call shape from this wrapper's
    # own three-parameter signature.
    return _contract_job
