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

from backend.beets_client import beets_client, BeetsError, BeetsUnavailableError, BeetsAuthError, BeetsCommandError
import os, shlex
from typing import NamedTuple


class ParsedRemoteBeetCommand(NamedTuple):
    subcommand: str
    args: List[str]
    config_override: str = ""


# How long Job._run() will keep polling for a *confirmed* remote terminal status
# after cancellation is requested, before giving up and reporting cancel_failed
# rather than waiting forever. Overridable per-Job for tests.
REMOTE_CANCEL_CONFIRM_TIMEOUT = float(os.environ.get("BEETS_CANCEL_CONFIRM_TIMEOUT", "30.0"))


def _is_beet_executable_token(token: str) -> bool:
    if not token:
        return False
    normalized = token.replace("\\", "/")
    base = normalized.split("/")[-1].lower()
    return base in ("beet", "beet.exe")


def _parse_remote_beet_command(command: Any) -> ParsedRemoteBeetCommand:
    """Parse and normalize raw Beets command tokens or string for remote execution."""
    if command is None:
        raise BeetsCommandError("Command cannot be None")

    if isinstance(command, str):
        raw_str = command.strip()
        if not raw_str:
            raise BeetsCommandError("Command string cannot be empty")
        try:
            tokens = shlex.split(raw_str, posix=True)
        except ValueError as exc:
            raise BeetsCommandError(f"Invalid shell quoting in command string: {exc}") from exc
    elif isinstance(command, (list, tuple)):
        tokens = [str(t) for t in command]
    else:
        raise BeetsCommandError(f"Unsupported command type: {type(command)}")

    if not tokens:
        raise BeetsCommandError("Command tokens list cannot be empty")

    # Strip leading Beets executable token if present (e.g. beet, /lsiopy/bin/beet, C:\...\beet.exe)
    if tokens and _is_beet_executable_token(tokens[0]):
        tokens = tokens[1:]

    # Strip -c / --config parameters (the remote engine uses its own authoritative config)
    clean_tokens: List[str] = []
    skip_next = False
    for i, token in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        if token in ("-c", "--config"):
            if i + 1 < len(tokens):
                skip_next = True
            continue
        clean_tokens.append(token)

    if not clean_tokens:
        raise BeetsCommandError("No subcommand provided after parsing executable and flags")

    subcommand = clean_tokens[0]
    args = clean_tokens[1:]

    if not subcommand or subcommand.strip() == "":
        raise BeetsCommandError("Subcommand cannot be empty")

    # Reject if subcommand is another path or executable
    sub_norm = subcommand.replace("\\", "/")
    if "/" in sub_norm or sub_norm.endswith((".py", ".sh", ".exe", ".bin")):
        raise BeetsCommandError(f"Invalid subcommand name: '{subcommand}'")

    # Reject shell operators/chaining in subcommand and args
    dangerous_ops = (";", "&&", "||", "|", ">", "<", "`", "$(")
    for token_to_check in [subcommand] + args:
        for op in dangerous_ops:
            if op in token_to_check:
                raise BeetsCommandError(f"Dangerous shell operator '{op}' rejected in command token: '{token_to_check}'")

    return ParsedRemoteBeetCommand(subcommand=subcommand, args=args, config_override="")


def _beet_run(cmd, log, *, timeout=120, env=None, warn_msg=None, cancel=None, config_override=""):
    """Run a beet command via the external Beets Control Agent API.

    config_override, when given, is forwarded as-is to the control agent's
    own config_override handling (it writes the string to its own temp YAML
    file and passes "-c <that file>" to its local beet invocation) -- it is
    NOT the same as a "-c <path>" token inside cmd, which _parse_remote_beet_command
    strips and discards (a local path on the caller's side has no meaning on
    the remote engine). Callers whose command semantics depend on config
    content (e.g. import's copy/move/duplicate_action policy) must pass that
    content explicitly here rather than relying on a stripped "-c" token.
    """
    class _R:
        def __init__(self, rc=0, out="", err=""):
            self.returncode = rc
            self.stdout = out
            self.stderr = err

    try:
        parsed = _parse_remote_beet_command(cmd)
    except BeetsCommandError as exc:
        log.append(f"  ⚠ Invalid Beets command: {exc}")
        return _R(1, "", str(exc))
    except Exception as exc:
        log.append(f"  ⚠ _beet_run parsing error: {exc}")
        return _R(1, "", str(exc))

    try:
        res = beets_client.run_command(
            parsed.subcommand, args=parsed.args, timeout=float(timeout),
            config_override=config_override or parsed.config_override,
        )
        rc = res.get("returncode", 0)
        stdout = res.get("stdout", "")
        stderr = res.get("stderr", "")
        if stdout:
            for line in stdout.splitlines():
                log.append(line)
        if stderr:
            for line in stderr.splitlines():
                log.append(f"  ⚠ {line}")
        return _R(rc, stdout, stderr)
    except BeetsUnavailableError as exc:
        log.append(f"  ⚠ Beets service unavailable: {exc}")
        return _R(1, "", str(exc))
    except BeetsAuthError as exc:
        log.append(f"  ⚠ Beets authentication failed: {exc}")
        return _R(1, "", str(exc))
    except Exception as exc:
        log.append(f"  ⚠ _beet_run error: {exc}")
        return _R(1, "", str(exc))


class Job:
    def __init__(self, job_id: str, command: List[str], label: str = "", cancel_confirm_timeout: Optional[float] = None):
        self.job_id      = job_id
        self.command     = command
        self.label       = label or " ".join(command)
        self.created_at  = time.time()
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.returncode: Optional[int]    = None
        self.log: List[str]               = []
        self._remote_job_id: Optional[str] = None
        self._cancel_requested = False
        self._cancel_failed = False
        self._state = "created"
        self._cancel_confirm_timeout = (
            REMOTE_CANCEL_CONFIRM_TIMEOUT if cancel_confirm_timeout is None else cancel_confirm_timeout
        )
        self._lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    @property
    def status(self) -> str:
        with self._lock:
            return self._state

    def kill(self):
        remote_id_to_cancel = None
        with self._lock:
            if self.finished_at is not None or self._state in ("cancelled", "success", "failed", "cancel_failed", "timeout"):
                return
            self._cancel_requested = True
            if "[killed]" not in self.log:
                self.log.append("[killed]")
            if self._state in ("running", "dispatching"):
                self._state = "cancelling"
            remote_id_to_cancel = self._remote_job_id

        if remote_id_to_cancel:
            try:
                beets_client.cancel_job(remote_id_to_cancel)
            except Exception as exc:
                with self._lock:
                    self._cancel_failed = True
                    err_msg = f"  ⚠ Remote cancellation request error: {exc}"
                    if err_msg not in self.log:
                        self.log.append(err_msg)

    def _run(self):
        with self._lock:
            self.started_at = time.time()
            if self._cancel_requested:
                self._state = "cancelled"
                self.returncode = 130
                self.finished_at = time.time()
                return
            self._state = "dispatching"

        # 1. Parse command
        try:
            parsed = _parse_remote_beet_command(self.command)
        except Exception as exc:
            with self._lock:
                self.log.append(f"  ⚠ Invalid command for job: {exc}")
                self._state = "failed"
                self.returncode = 1
                self.finished_at = time.time()
            return

        # 2. Check cancel before dispatch
        with self._lock:
            if self._cancel_requested:
                self._state = "cancelled"
                self.returncode = 130
                self.finished_at = time.time()
                return

        # 3. Dispatch start_job
        try:
            remote_id = beets_client.start_job(
                parsed.subcommand,
                args=parsed.args,
                label=self.label,
                config_override=parsed.config_override
            )
        except Exception as exc:
            with self._lock:
                if self._cancel_requested:
                    self._state = "cancelled"
                    self.returncode = 130
                else:
                    self.log.append(f"ERROR: {exc}")
                    self._state = "failed"
                    self.returncode = 1
                self.finished_at = time.time()
            return

        if not remote_id:
            with self._lock:
                if self._cancel_requested:
                    self._state = "cancelled"
                    self.returncode = 130
                else:
                    self.log.append("ERROR: Failed to start remote job on Beets agent")
                    self._state = "failed"
                    self.returncode = 1
                self.finished_at = time.time()
            return

        # 4. Store remote_id
        with self._lock:
            self._remote_job_id = remote_id
            if self._cancel_requested:
                self._state = "cancelling"
            else:
                self._state = "running"

        # 5. Polling loop and cancellation confirmation
        seen_stdout = 0
        seen_stderr = 0
        cancel_deadline: Optional[float] = None

        while True:
            with self._lock:
                cancel_req = self._cancel_requested
                cancel_err = self._cancel_failed

            if cancel_req:
                if cancel_deadline is None:
                    # Deadline starts the moment we first notice cancellation was
                    # requested while a real remote job exists, using monotonic time
                    # so wall-clock adjustments can't extend or shorten the wait.
                    cancel_deadline = time.monotonic() + self._cancel_confirm_timeout
                try:
                    beets_client.cancel_job(remote_id)
                except Exception as exc:
                    with self._lock:
                        self._cancel_failed = True
                        err_msg = f"  ⚠ Remote cancellation request error: {exc}"
                        if err_msg not in self.log:
                            self.log.append(err_msg)

            try:
                job_data = beets_client.get_job(remote_id)
                r_status = job_data.get("status", "running")
                r_stdout = job_data.get("stdout", [])
                r_stderr = job_data.get("stderr", [])

                with self._lock:
                    if len(r_stdout) > seen_stdout:
                        for line in r_stdout[seen_stdout:]:
                            self.log.append(line)
                        seen_stdout = len(r_stdout)

                    if len(r_stderr) > seen_stderr:
                        for line in r_stderr[seen_stderr:]:
                            self.log.append(f"ERR: {line}")
                        seen_stderr = len(r_stderr)

                if r_status in ("success", "failed", "cancelled", "timeout"):
                    with self._lock:
                        if r_status == "cancelled":
                            self._state = "cancelled"
                            self.returncode = 130
                        elif r_status == "success":
                            if cancel_req:
                                self.log.append("  ⚠ Cancellation arrived after the remote job had already completed.")
                            self._state = "success"
                            self.returncode = job_data.get("returncode", 0)
                        elif r_status == "failed":
                            self._state = "failed"
                            self.returncode = job_data.get("returncode", 1)
                        else:
                            self._state = r_status
                            self.returncode = job_data.get("returncode", 1)
                        self.finished_at = time.time()
                    break

            except Exception as exc:
                with self._lock:
                    if cancel_req or cancel_err:
                        self.log.append(f"  ⚠ Remote state unconfirmed during cancellation: {exc}")
                        self._state = "cancel_failed"
                        self.returncode = 1
                        self.finished_at = time.time()
                        break
                    else:
                        self.log.append(f"ERROR: {exc}")
                        self._state = "failed"
                        self.returncode = 1
                        self.finished_at = time.time()
                        break

            if cancel_deadline is not None and time.monotonic() >= cancel_deadline:
                with self._lock:
                    err_msg = (
                        f"  ⚠ Remote cancellation was not confirmed within "
                        f"{self._cancel_confirm_timeout:.0f}s; giving up waiting."
                    )
                    if err_msg not in self.log:
                        self.log.append(err_msg)
                    self._state = "cancel_failed"
                    self.returncode = 1
                    self.finished_at = time.time()
                break

            time.sleep(0.2)

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
