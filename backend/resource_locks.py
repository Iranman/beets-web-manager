"""Durable, hierarchical resource locks shared by every Web Manager process.

Each held key is a JSON file ``<root>/<key>.lock`` created with
``O_CREAT | O_EXCL``, so ownership is visible to every process and survives
restarts. Conflict checks and creation run under a cross-process registry
mutex (``.registry.mutex``, also ``O_EXCL``), so two processes can never both
believe they hold conflicting keys.

Keys and hierarchy (lower level = coarser):

    level 0  library-global            conflicts with every other key
    level 1  untracked-inventory
             album-merge:<release group id>
             playlist:<id>
             workflow:<name>           one running instance of a long job
                                       (backend/job_contract.py)
    level 2  album:<id>
    level 3  item:<id>

Identical keys conflict; ``library-global`` conflicts with everything. The
hierarchy is enforced, not documented: one owner must acquire keys in
non-decreasing (level, key) order -- a single ``acquire`` sorts its keys, and
acquiring a key that sorts before one the owner already holds raises
``LockOrderError``. Every owner therefore takes locks in one global order,
which rules out deadlock cycles.

Liveness: the holder refreshes ``heartbeat_at`` (``hold`` runs a heartbeat
thread). A lock is reclaimed only when its heartbeat is older than its ttl
AND its owner process is provably gone: on the same host the recorded pid
must be dead (or reused -- the process start time is recorded on Linux). A
lock held by another host is reclaimed on heartbeat expiry alone. A live
long operation is therefore never stolen.
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_KEY_RE = re.compile(r"^(library-global|untracked-inventory|album-merge:[0-9a-f-]{8,64}|playlist:[A-Za-z0-9_.-]{1,80}"
                     r"|workflow:[a-z0-9][a-z0-9-]{0,79}|album:\d{1,12}|item:\d{1,12})$")
_LEVELS = {"library-global": 0, "untracked-inventory": 1, "album-merge": 1, "playlist": 1, "workflow": 1,
           "album": 2, "item": 3}
HOST = socket.gethostname()
PID = os.getpid()


class ResourceLockConflictError(Exception):
    def __init__(self, key: str, holder: Dict[str, Any], requested_owner: str):
        self.key, self.holder, self.requested_owner = key, holder, requested_owner
        super().__init__(f"resource {key!r} is held by {holder.get('owner')!r}")


class LockOrderError(Exception):
    """An owner tried to take a coarser/earlier lock after a finer/later one."""


def level_of(key: str) -> int:
    return _LEVELS[key.split(":", 1)[0]]


def _order(key: str) -> Tuple[int, str]:
    return (level_of(key), key)


def validate_key(key: str) -> str:
    key = str(key or "").strip()
    if not _KEY_RE.match(key):
        raise ValueError(f"invalid resource lock key: {key!r}")
    return key


def conflicts(a: str, b: str) -> bool:
    return a == b or a == "library-global" or b == "library-global"


def _proc_start_time(pid: int) -> Optional[str]:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            return fh.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _pid_alive(pid: int, start: Optional[str]) -> bool:
    if pid <= 0:
        return False
    if os.path.isdir("/proc"):
        now_start = _proc_start_time(pid)
        return now_start is not None and (start is None or now_start == start)
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


_SELF_START = _proc_start_time(PID)


class ResourceLocks:
    def __init__(self, root: Path, *, mutex_timeout: float = 30.0):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.mutex_timeout = mutex_timeout
        self._local = threading.Lock()
        self._held: Dict[Tuple[str, str], int] = {}  # (owner, key) -> re-entrant count in this process

    # -- registry mutex ------------------------------------------------------
    @contextmanager
    def _registry(self):
        path = self.root / ".registry.mutex"
        self.root.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.mutex_timeout
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, json.dumps({"pid": PID, "host": HOST, "at": time.time()}).encode())
                os.close(fd)
                break
            except FileExistsError:
                try:  # a mutex is held for milliseconds; one older than 30 s is from a dead process
                    if time.time() - path.stat().st_mtime > 30:
                        path.unlink()
                        continue
                except OSError:
                    continue
                if time.time() > deadline:
                    raise TimeoutError("resource lock registry is busy")
                time.sleep(0.005)
        try:
            yield
        finally:
            try:
                path.unlink()
            except OSError:
                pass

    def _file(self, key: str) -> Path:
        return self.root / (key.replace(":", "__") + ".lock")

    def _read_all(self) -> Dict[str, Dict[str, Any]]:
        out = {}
        for path in self.root.glob("*.lock"):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
                out[rec["key"]] = rec
            except (OSError, ValueError, KeyError):
                continue
        return out

    def is_stale(self, rec: Dict[str, Any], now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        if now - float(rec.get("heartbeat_at") or 0) <= float(rec.get("ttl") or 0):
            return False
        if rec.get("host") == HOST:
            return not _pid_alive(int(rec.get("pid") or 0), rec.get("pid_start"))
        return True

    # -- API -------------------------------------------------------------------
    def acquire(self, keys: Iterable[str], owner: str, *, timeout: float = 30.0, ttl: float = 120.0) -> List[str]:
        wanted = sorted({validate_key(k) for k in keys}, key=_order)
        owner = str(owner or "").strip()
        if not owner:
            raise ValueError("lock owner is required")
        with self._local:
            mine = [k for (o, k) in self._held if o == owner]
        if mine:
            highest = max(mine, key=_order)
            for key in wanted:
                if key not in mine and _order(key) < _order(highest):
                    raise LockOrderError(f"{owner!r} holds {highest!r}; {key!r} must be acquired before it")
        deadline = time.time() + max(0.0, timeout)
        while True:
            with self._registry():
                existing = self._read_all()
                now = time.time()
                blocker = None
                for key in wanted:
                    for other_key, rec in existing.items():
                        if rec.get("owner") == owner or not conflicts(key, other_key):
                            continue
                        if self.is_stale(rec, now):
                            self._file(other_key).unlink(missing_ok=True)
                            continue
                        blocker = (key, rec)
                        break
                    if blocker:
                        break
                if blocker is None:
                    for key in wanted:
                        if existing.get(key, {}).get("owner") != owner:
                            rec = {"key": key, "owner": owner, "host": HOST, "pid": PID, "pid_start": _SELF_START,
                                   "acquired_at": now, "heartbeat_at": now, "ttl": ttl}
                            fd = os.open(self._file(key), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                            os.write(fd, json.dumps(rec).encode())
                            os.close(fd)
                        with self._local:
                            self._held[(owner, key)] = self._held.get((owner, key), 0) + 1
                    return wanted
            if time.time() >= deadline:
                raise ResourceLockConflictError(blocker[0], blocker[1], owner)
            time.sleep(0.02)

    def release(self, keys: Iterable[str], owner: str) -> None:
        with self._registry():
            for key in {validate_key(k) for k in keys}:
                with self._local:
                    count = self._held.get((owner, key), 0) - 1
                    if count > 0:
                        self._held[(owner, key)] = count
                        continue
                    self._held.pop((owner, key), None)
                path = self._file(key)
                try:
                    rec = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if rec.get("owner") == owner:
                    path.unlink(missing_ok=True)

    def heartbeat(self, owner: str) -> int:
        updated = 0
        with self._registry():
            for key, rec in self._read_all().items():
                if rec.get("owner") == owner and rec.get("host") == HOST and rec.get("pid") == PID:
                    rec["heartbeat_at"] = time.time()
                    tmp = self.root / f".{uuid.uuid4().hex}.tmp"
                    tmp.write_text(json.dumps(rec), encoding="utf-8")
                    os.replace(tmp, self._file(key))
                    updated += 1
        return updated

    def held(self) -> List[Dict[str, Any]]:
        return sorted(self._read_all().values(), key=lambda r: _order(r["key"]))

    def reclaim_stale(self) -> List[str]:
        reclaimed = []
        with self._registry():
            now = time.time()
            for key, rec in self._read_all().items():
                if self.is_stale(rec, now):
                    self._file(key).unlink(missing_ok=True)
                    reclaimed.append(key)
        return reclaimed

    @contextmanager
    def hold(self, keys: Iterable[str], owner: str, *, timeout: float = 30.0, ttl: float = 120.0):
        """Acquire, keep the heartbeat fresh while held, always release."""
        keys = self.acquire(keys, owner, timeout=timeout, ttl=ttl)
        stop = threading.Event()

        def beat():
            while not stop.wait(max(0.05, ttl / 3.0)):
                try:
                    self.heartbeat(owner)
                except Exception:
                    pass

        thread = threading.Thread(target=beat, daemon=True)
        thread.start()
        try:
            yield keys
        finally:
            stop.set()
            thread.join(timeout=5)
            self.release(keys, owner)


def attempt_owner(operation_id: str) -> str:
    """A lock owner unique to one attempt. Locks are re-entrant per owner, so
    two concurrent attempts on the same transaction must not share one."""
    return f"{operation_id}:{uuid.uuid4().hex[:12]}"


def claim_approved(store, operation_id: str) -> Optional[Dict[str, Any]]:
    """Re-read the transaction INSIDE the held lock and move it Approved ->
    Running with a compare-and-set; None if another attempt already claimed
    or applied it, or a cancel won the race (#206 F3)."""
    tx = store.get(operation_id)
    if (tx.get("metadata") or {}).get("engine_result"):
        return None
    return store.transition(operation_id, "Approved", "Running")


def claim_refusal(store, operation_id: str) -> str:
    """The message for a failed claim_approved, read once after the CAS lost
    (QA-217-3): a cancel and a second apply look different to the caller."""
    try:
        status = store.get(operation_id).get("status")
    except KeyError:
        return "Transaction not found."
    if status == "Approved":
        return "This transaction was already applied."
    return f"This transaction is no longer Approved (now {status})."


def approve_preview(store, operation_id: str, approved_by: str) -> Optional[Dict[str, Any]]:
    """Compare-and-set Preview -> Approved (#206 F4). An already-Approved
    transaction passes unchanged; any other status (Cancelled, Failed,
    Completed, ...) returns None and is never resurrected. KeyError if the
    transaction does not exist."""
    tx = store.transition(operation_id, "Preview", "Approved", metadata={"approved_by": str(approved_by or "")})
    if tx is None:
        tx = store.get(operation_id)
        if tx.get("status") != "Approved":
            return None
    return tx


_override: Optional[ResourceLocks] = None
_by_root: Dict[str, ResourceLocks] = {}
_default_lock = threading.Lock()


def locks() -> ResourceLocks:
    """The lock registry under <WEB_MANAGER_DATA_DIR>/locks (one per data dir)."""
    with _default_lock:
        if _override is not None:
            return _override
        base = os.environ.get("WEB_MANAGER_DATA_DIR") or "/web-manager-data"
        registry = _by_root.get(base)
        if registry is None:
            try:
                registry = ResourceLocks(Path(base) / "locks")
            except OSError:
                # No writable data dir (a bare test/CI runner): keep locking
                # correct across this host's processes via a temp dir.
                import logging
                import tempfile
                fallback = Path(tempfile.gettempdir()) / f"bwm-locks-{os.getuid() if hasattr(os, 'getuid') else 'u'}"
                logging.getLogger("app.resource_locks").warning(
                    "resource lock dir %s/locks is not writable; using %s", base, fallback)
                registry = ResourceLocks(fallback)
            _by_root[base] = registry
        return registry


def set_locks(registry: Optional[ResourceLocks]) -> None:
    """Force a specific registry (tests); None restores the data-dir default."""
    global _override
    with _default_lock:
        _override = registry
