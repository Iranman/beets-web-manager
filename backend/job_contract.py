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


class WorkflowBusyError(RuntimeError):
    """The workflow is running in another process and did not finish in time."""


def slug(text: Any) -> str:
    """A stable lock-safe id for a free-text workflow subject (a playlist
    name, a folder path)."""
    return hashlib.sha256(str(text or "").strip().lower().encode("utf-8")).hexdigest()[:16]


def workflow_key(workflow: str) -> str:
    return f"workflow:{workflow}"


def contract_metadata(workflow: str, metadata: Optional[Dict[str, Any]] = None,
                      keys: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Job metadata for a contract job: never treated as read-only, and the
    durable record names its workflow and locks."""
    out = dict(metadata or {})
    out.setdefault("mutating", True)
    out["workflow_contract"] = {"workflow": workflow, "lock_keys": list(keys or [workflow_key(workflow)])}
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
         wait_seconds: float = DEFAULT_WAIT_SECONDS, progress: Optional[Callable[[], Dict[str, Any]]] = None,
         fail_fast_in_process: bool = False):
    """Hold the workflow's durable lock(s) for the duration of the block.

    Waits (checking ``cancel_event``) while another process holds them;
    raises WorkflowBusyError after ``wait_seconds``. Publishes the contract
    checkpoint, and ``progress()`` whenever it changes, to ``update_state``.

    ``fail_fast_in_process``: a holder in THIS process is a live duplicate,
    not a stale lock -- raise WorkflowBusyError at once instead of waiting
    (for workflows with no in-process guard of their own)."""
    wanted = list(keys or [workflow_key(workflow)])
    owner = f"job:{workflow}:{uuid.uuid4().hex[:12]}"
    registry = resource_locks()
    base = {"workflow": workflow, "lock_keys": wanted, "owner": owner}
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
                _publish(update_state, {**base, "stage": "waiting_for_lock", "at": time.time()})
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
                        _publish(update_state, {**base, "stage": "running", "at": now, "progress": current})
            except Exception:
                pass

    _publish(update_state, {**base, "stage": "running", "at": time.time()})
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
            wait_seconds: float = DEFAULT_WAIT_SECONDS,
            progress: Optional[Callable[[], Dict[str, Any]]] = None,
            fail_fast_in_process: bool = False) -> Callable[..., Any]:
    """Wrap a job function (``fn(log[, cancel_event[, update_state]])``) so it
    runs under the contract. The result is what ``fn`` returns."""
    arity = len(inspect.signature(fn).parameters)

    def _contract_job(log, cancel_event=None, update_state=None):
        with held(workflow, log=log, cancel_event=cancel_event, update_state=update_state, keys=keys,
                  wait_seconds=wait_seconds, progress=progress, fail_fast_in_process=fail_fast_in_process):
            if arity >= 3:
                return fn(log, cancel_event, update_state)
            if arity >= 2:
                return fn(log, cancel_event)
            return fn(log)

    # No __wrapped__: the job engine picks the call shape from this wrapper's
    # own three-parameter signature.
    return _contract_job
