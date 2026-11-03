"""Characterization of Clean All (modernization Phase 4, #505).

Each scenario drives the real POST /api/jobs/maintenance-runner route and the
real job body, with every collaborator faked, a real on-disk checkpoint and the
real workflow guard. The ordered trace -- collaborator calls and arguments, every
job state update, every report save, every log line, the job's result or its
full exception chain, the response and the final report -- is compared with
``tests/fixtures/clean_all_golden.json``, which was recorded from the code BEFORE
the job was split into steps.

The golden holds a sha256 of each whole trace plus a readable outcome, and the
full trace of a few representative runs (FULL_TRACES). To see a difference:
    CLEAN_ALL_TRACE_DUMP=/some/dir python -m unittest tests.test_clean_all_characterization
writes every actual trace there. Clocks are fixed or scrubbed and nothing lists
a directory, so a trace depends on neither.

Regenerate only for a deliberate behavior change:
    UPDATE_CLEAN_ALL_GOLDEN=1 python -m unittest tests.test_clean_all_characterization
"""
import hashlib
import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest import mock

import app as app_module
import backend.maintenance_service as maintenance_service
import backend.resource_locks as resource_locks
import routes_maintenance as rm
from job_engine import DuplicateJobError

try:  # ARCH-001: patch app.py and the modules extracted from it
    from _app_family import patch_app_family  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_family import patch_app_family  # noqa: E402

GOLDEN = Path(__file__).parent / "fixtures" / "clean_all_golden.json"
FULL_TRACES = {
    "fresh_success", "artist_merge_still_running_then_resume_completes", "root_repair_fails",
    "first_task_fails", "cancel_during_missing_files", "resume_after_alias",
}
PIPELINE = ["Scanning", "Fingerprinting", "Matching", "Verifying", "Repairing", "Replacing", "Organizing", "Syncing"]
TASK_IDS = [t["id"] for t in maintenance_service._maintenance_initial_task_state()]
LABELS = {t["id"]: t["label"] for t in maintenance_service._maintenance_initial_task_state()}
NOW = 1000.0


def _defaults():
    return {
        "library_health": {"ok": True, "duplicate_album_count": 1, "rgid_duplicate_group_count": 2,
                           "orphaned_item_count": 3, "empty_album_count": 4, "final_summary": {"tracks_count": 9}},
        "missing_files": {"ok": True, "missing_files": 3, "removed_db_rows": 0,
                          "final_summary": {"missing_files": 3, "removed_db_rows": 0}},
        "root_folder_repair": {"ok": True, "final_summary": {"files_moved": 2, "folders_deleted": 1}},
        "artist_alias": [],
        "artist_folder_merge": {"ok": True, "renamed": 2, "merged": 1},
        "release_group_merge": {"ok": True, "skipped": False, "final_summary": {"files_moved": 1}},
        "duplicates": {"ok": True, "skipped": False, "final_summary": {"deleted_files": 2, "skipped_candidates": 1}},
        "folder_scan": [{"path": "/music/a", "folder_name": "a"}, {"path": "/music/b", "folder_name": "b"}],
        "start_art": ({"ok": True, "job_id": "child-art"}, 200),
        "start_genres": ({"ok": True, "job_id": "child-genres"}, 200),
        "wait_artwork": {"saved": 3},
        "wait_genres": {"changed_count": 4},
        "final_verification": {"ok": True, "final_summary": {"manual_review_items": 1, "submission_queue_items": 2}},
        "prune": None,
        "playlist_refs": {"enabled": True, "running": False},
    }


COLLABORATORS = tuple(_defaults())


def _chain(exc, seen=None):
    """The whole exception chain: __cause__, __context__ and the suppress flag."""
    seen = seen if seen is not None else set()
    if exc is None or id(exc) in seen:
        return None
    seen.add(id(exc))
    return {"type": type(exc).__name__, "message": str(exc), "suppress": exc.__suppress_context__,
            "cause": _chain(exc.__cause__, seen), "context": _chain(exc.__context__, seen)}


class _Log(list):
    def __init__(self, world):
        super().__init__()
        self._world = world

    def append(self, line):
        self._world.event("log", line)
        super().append(line)


class _Job:
    def __init__(self, world, job_id, fn, label, metadata):
        self.world, self.job_id, self.fn, self.label, self.metadata = world, job_id, fn, label, metadata
        self.log = _Log(world)
        self.cancel = threading.Event()

    def run(self, *, bare=False, label="run"):
        world = self.world
        world.event("job_begin", label)
        self.cancel.clear()
        if world.opts.get("precancel"):
            self.cancel.set()
        try:
            result = self.fn(self.log) if bare else self.fn(self.log, self.cancel, world.update_state)
        except Exception as exc:  # the job engine's boundary
            world.event("job_raised", _chain(exc))
            world.outcome_error = _chain(exc)
        else:
            world.event("job_returned", world.clean(result))
            world.outcome_result = result
        return self


class _Store:
    def __init__(self, world):
        self.world = world
        self.started = []

    def start_python(self, fn, label="", metadata=None):
        dup = self.world.opts.get("duplicate")
        self.world.event("start_python", label, metadata)
        if dup is not None:
            raise DuplicateJobError(SimpleNamespace(job_id="dup-job", label=label, status="success"), replay=dup == "replay")
        job = _Job(self.world, f"job-{len(self.started) + 1}", fn, label, metadata)
        self.started.append(job)
        return job.run()

    def prune_finished(self, **kwargs):
        return self.world.collab("prune", (), kwargs)


class _World:
    def __init__(self, tmp, opts):
        self.tmp = str(tmp)
        self.opts = opts
        self.results = {**_defaults(), **opts.get("results", {})}
        self.raises = dict(opts.get("raises", {}))
        self.events = []
        self.tasks_seen = {}
        self.updates = 0
        self.saves = 0
        self.outcome_error = None
        self.outcome_result = None
        self.response = None
        self.store = _Store(self)
        self.checkpoint = Path(tmp) / "maintenance-runner-last.json"

    def active_cancel(self):
        return self.store.started[-1].cancel if self.store.started else None

    def _scrub(self, text):
        text = text.replace(self.tmp, "<TMP>")
        return re.sub(r"(job:maintenance-runner:)[0-9a-f]{12}", r"\1<owner>", text)

    def clean(self, value, key=""):
        """JSON-safe copy, taken at call time, with task lists shown as changes."""
        if key.endswith("_at") or key == "at":
            return value if value in (NOW, None) or not isinstance(value, float) else "<wall-clock>"
        if isinstance(value, dict):
            return {str(k): self.clean(v, str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            if value == PIPELINE:
                return "<pipeline>"
            if value and all(isinstance(row, dict) and "id" in row and "status" in row for row in value):
                return self._task_delta(value)
            return [self.clean(v) for v in value]
        if isinstance(value, _Log):
            return "<log>"
        if isinstance(value, threading.Event):
            return "<cancel>"
        if callable(value):
            return "<callable>"
        if isinstance(value, str):
            return self._scrub(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return self._scrub(str(value))

    def _task_delta(self, rows):
        changed = {}
        for row in rows:
            rest = {k: self.clean(v) for k, v in row.items() if k != "id"}
            if rest.get("label") == LABELS.get(row["id"]):
                rest.pop("label")
            text = json.dumps(rest, sort_keys=True)
            if self.tasks_seen.get(row["id"]) != text:
                self.tasks_seen[row["id"]] = text
                changed[row["id"]] = rest
        return {"order": hashlib.sha256(",".join(r["id"] for r in rows).encode()).hexdigest()[:8], "changed": changed}

    def event(self, kind, *payload):
        self.events.append([kind, *[self.clean(p) for p in payload]])

    def _maybe_fault(self, count, spec, what):
        if not spec:
            return
        at, sticky = spec
        if count == at or (sticky and count > at):
            raise RuntimeError(f"{what} fault {at}")

    def update_state(self, updates=None, **kwargs):
        self.updates += 1
        self.event("update", {**(updates or {}), **kwargs})
        if self.opts.get("cancel_on_update") == self.updates:
            self.active_cancel().set()
        self._maybe_fault(self.updates, self.opts.get("update_fault"), "update_state")

    def save_report(self, update, log=None):
        self.saves += 1
        self.event("save", update, log)
        self._maybe_fault(self.saves, self.opts.get("save_fault"), "save")
        return _REAL_SAVE(update, log)

    def collab(self, name, args, kwargs):
        self.event("call", name, list(args), kwargs)
        for key in ("progress",):
            cb = kwargs.get(key)
            if cb:
                cb({"current_result": f"{name} working"})
                cb({})
        if name == "folder_scan" and isinstance(kwargs.get("scan_meta"), dict):
            kwargs["scan_meta"]["total_folders_scanned"] = 7
        if name == "artist_folder_merge" and self.opts.get("plan_op"):
            kwargs["on_operation_planned"](self.opts["plan_op"])
        if self.opts.get("cancel_at") == name:
            self.active_cancel().set()
        if name in self.raises:
            raise self.raises[name]
        return self.results[name]

    def fake(self, name):
        def _fake(*args, **kwargs):
            return self.collab(name, args, kwargs)
        return _fake

    def wait_child(self, child_id, log, cancel_event, prefix="", timeout=0):
        return self.collab(f"wait_{prefix}", (child_id, log, cancel_event), {"prefix": prefix, "timeout": timeout})

    def running_of_type(self, types):
        self.event("running_of_type", sorted(types))
        return bool(set(types) & set(self.opts.get("running", ())))

    def guarded(self, fn, **kwargs):
        # Scheduled ticks in this process also call guarded(); they are not this run.
        if kwargs.get("workflow") == "maintenance-runner":
            self.event("guarded", kwargs)
        return _REAL_GUARDED(fn, **kwargs)

    def post(self, body=None, raw=False):
        client = app_module.app.test_client()
        headers = {"X-Beets-CSRF": "1"}
        if raw:
            resp = client.post("/api/jobs/maintenance-runner", headers=headers, data="not json", content_type="text/plain")
        else:
            resp = client.post("/api/jobs/maintenance-runner", headers=headers, json=body if body is not None else {})
        self.response = {"status": resp.status_code, "json": self.clean(resp.get_json(silent=True))}
        self.event("response", self.response)
        return self.response

    def report(self):
        if not self.checkpoint.exists():
            return None
        data = json.loads(self.checkpoint.read_text(encoding="utf-8"))
        return self.clean(data)

    def write_checkpoint(self, status="partial", tasks=None, results=None, extra=None, omit_tasks=False):
        rows = []
        for tid in TASK_IDS:
            row = {"id": tid, "label": LABELS[tid], "status": "pending"}
            spec = (tasks or {}).get(tid)
            row.update({"status": spec} if isinstance(spec, str) else (spec or {}))
            rows.append(row)
        last = {"status": status, "workflow": "clean-all", "started_at": 500.0, "updated_at": 600.0, "results": results or {}}
        if not omit_tasks:
            last["tasks"] = rows
        last.update(extra or {})
        self.checkpoint.write_text(json.dumps({"last_run": last}), encoding="utf-8")

    def _patches(self):
        fakes = {
            "_library_health_payload": "library_health", "_maintenance_remove_missing_file_rows": "missing_files",
            "_maintenance_root_folder_repair": "root_folder_repair", "_artist_id_alias_groups": "artist_alias",
            "_maintenance_artist_folder_merge_step": "artist_folder_merge",
            "_maintenance_release_group_merge": "release_group_merge", "_maintenance_full_duplicate_scan": "duplicates",
            "_scan_folder_name_placeholders": "folder_scan", "start_fetch_missing_art": "start_art",
            "start_library_fix_genres": "start_genres", "_maintenance_final_verification": "final_verification",
            "playlist_sync_status_payload": "playlist_refs",
        }
        out = [mock.patch.object(rm, attr, self.fake(name)) for attr, name in fakes.items()]
        out += [
            mock.patch.object(rm, "_wait_for_child_job", self.wait_child),
            mock.patch.object(rm, "_running_job_of_type", self.running_of_type),
            mock.patch.object(rm, "_maintenance_running_job", lambda: self.opts.get("already_running")),
            mock.patch.object(rm, "_maintenance_save_last_report", self.save_report),
            mock.patch.object(rm, "jobs", self.store),
            mock.patch.object(rm, "time", SimpleNamespace(time=lambda: NOW)),
            mock.patch.object(rm, "MUSIC_ROOT", PurePosixPath("/music-root")),
            mock.patch.object(rm.job_contract, "guarded", self.guarded),
            patch_app_family(app_module, "MAINTENANCE_RUNNER_LAST_FILE", self.checkpoint),
            patch_app_family(app_module, "_security_auth_disabled", return_value=True),
        ]
        if self.opts.get("holder"):
            out.append(mock.patch.object(rm.job_contract, "live_holder", lambda _w: {"pid": 1}))
        if self.opts.get("write_fails"):
            out.append(mock.patch.object(maintenance_service, "atomic_write_json", side_effect=OSError("disk full")))
        if "resume_snapshot" in self.opts:
            out.append(mock.patch.object(rm, "_maintenance_resume_from_report", lambda _r: self.opts["resume_snapshot"]))
        return out

    def play(self, script):
        patches = self._patches()
        for p in patches:
            p.start()
        registry = resource_locks.ResourceLocks(Path(self.tmp) / "locks")
        resource_locks.set_locks(registry)
        try:
            script(self)
        finally:
            resource_locks.set_locks(None)
            for p in reversed(patches):
                p.stop()
        return {"events": self.events, "report": self.report()}


_REAL_SAVE = maintenance_service._maintenance_save_last_report
_REAL_GUARDED = rm.job_contract.guarded


def _post(body=None):
    return lambda w: w.post(body)


def _ckpt(then=None, **kwargs):
    def script(w):
        w.write_checkpoint(**kwargs)
        (then or (lambda x: x.post()))(w)
    return script


def _done_results(ids):
    results = {tid: {"ok": True, "final_summary": {"checked": 1}} for tid in ids}
    return results


def _prefix(k, status="complete"):
    ids = TASK_IDS[:k]
    return {tid: status for tid in ids}, _done_results(ids)


def _twice(w):
    w.post({"force_fresh": True})
    w.post()


def _reinvoke(first_opts_cleared=False):
    def script(w):
        w.post()
        job = w.store.started[-1]
        if first_opts_cleared:
            w.raises.clear()
        job.run(label="reinvoke")
    return script


def _bare(w):
    w.post()
    w.store.started[-1].run(bare=True, label="bare")


def _still_running_then_completes(w):
    w.post({"force_fresh": True})
    w.results["artist_folder_merge"] = {"ok": True, "renamed": 1, "merged": 0}
    w.opts.pop("plan_op", None)
    w.post()


def _failure_then_retry(failing):
    def script(w):
        w.post({"force_fresh": True})
        w.raises.pop(failing, None)
        w.post()
    return script


def _scenarios():
    s = {}

    def add(name, script=None, **opts):
        s[name] = (script or _post(), opts)

    add("fresh_success")
    add("fresh_force_flag", _post({"force_fresh": True}))
    add("non_json_body", lambda w: w.post(raw=True))
    add("already_running", already_running=SimpleNamespace(job_id="live"))
    add("held_by_other_process", holder=True)
    add("duplicate_race", duplicate="plain")
    add("duplicate_replay", duplicate="replay")
    add("report_write_fails", write_fails=True)
    add("cancelled_before_start", precancel=True)
    add("no_cancel_or_state_channel", _bare)
    add("run_twice_back_to_back", _twice)

    # Every collaborator: an ordinary failure, a cancel-worded failure, an empty
    # message, and a cancel raised while it runs.
    for name in COLLABORATORS:
        add(f"raise_{name}", raises={name: RuntimeError(f"{name} boom")})
        add(f"raise_cancelled_{name}", raises={name: RuntimeError(" Cancelled ")})
        add(f"raise_empty_{name}", raises={name: RuntimeError("")})
        add(f"cancel_during_{name}", cancel_at=name)
        add(f"raise_then_retry_{name}", _failure_then_retry(name), raises={name: RuntimeError(f"{name} boom")})
        add(f"reinvoke_after_raise_{name}", _reinvoke(True), raises={name: RuntimeError(f"{name} boom")})
    s["first_task_fails"] = s["raise_library_health"]
    s["root_repair_fails"] = s["raise_root_folder_repair"]
    add("root_repair_cancel_raised_with_event_set", raises={"root_folder_repair": RuntimeError("stuck")},
        cancel_at="root_folder_repair")
    add("root_repair_skipped", results={"root_folder_repair": {"skipped": True, "final_summary": {"files_moved": 0}}})

    # Thresholds at their boundaries.
    for removed in (0, 1, 2):
        add(f"missing_removed_{removed}", results={"missing_files": {
            "ok": True, "missing_files": 3, "removed_db_rows": removed, "skip_reason": "", "final_summary": {}}})
    add("missing_skip_reason", results={"missing_files": {
        "ok": True, "missing_files": 3, "removed_db_rows": 0, "skip_reason": "mount issue", "final_summary": {}}})
    for count in (0, 1, 49, 50, 51):
        add(f"alias_groups_{count}", results={"artist_alias": [{"mbid": f"m{i}"} for i in range(count)]})
    add("no_placeholder_rows", results={"folder_scan": []})
    add("merge_results_skipped", results={
        "release_group_merge": {"skipped": True}, "duplicates": {"skipped": True}})

    # artist_folder_merge: every result branch, and the PR #517 skip.
    unsupported = {"ok": False, "error_code": "artist_folder_mode_unsupported", "error": "needs split folders"}
    add("artist_merge_unsupported_mode_skips", results={"artist_folder_merge": unsupported})
    add("artist_merge_unsupported_with_planned_op", results={"artist_folder_merge": unsupported}, plan_op="op-1")
    add("artist_merge_unsupported_finishes_then_rerun", _twice, results={"artist_folder_merge": unsupported})
    add("artist_merge_failed_result", results={"artist_folder_merge": {"ok": False, "error": "nope"}}, plan_op="op-1")
    add("artist_merge_failed_no_error", results={"artist_folder_merge": {"ok": False}})
    add("artist_merge_none_result", results={"artist_folder_merge": None})
    add("artist_merge_no_ok_key", results={"artist_folder_merge": {"renamed": 1}})
    add("artist_merge_planned_then_ok", plan_op="op-1")
    still = {"ok": False, "still_running": True, "operation_id": "op-1"}
    add("artist_merge_still_running", results={"artist_folder_merge": still}, plan_op="op-1")
    add("artist_merge_still_running_no_id", results={"artist_folder_merge": {"ok": False, "still_running": True}})
    add("artist_merge_still_running_then_resume_completes", _still_running_then_completes,
        results={"artist_folder_merge": still}, plan_op="op-1")
    add("artist_merge_raises_with_planned_op", raises={"artist_folder_merge": RuntimeError("apply lost")}, plan_op="op-1")
    add("artist_merge_other_job_running", running={"artist-folder-merge"})
    add("artist_merge_stamp_job_running", running={"stamp-mbid-folders"})
    tasks, results = _prefix(TASK_IDS.index("artist_folder_merge"))
    op_tasks = {**tasks, "artist_folder_merge": {"status": "running", "operation_id": "op-9", "detail": "was running"}}
    add("resume_saved_op", _ckpt(tasks=op_tasks, results=results))
    add("resume_saved_op_other_job_running", _ckpt(tasks=op_tasks, results=results), running={"artist-folder-merge"})
    add("resume_saved_op_still_running", _ckpt(tasks=op_tasks, results=results), results={"artist_folder_merge": still})
    add("resume_saved_op_raises", _ckpt(tasks=op_tasks, results=results), raises={"artist_folder_merge": RuntimeError("lookup")})
    add("resume_saved_op_unsupported", _ckpt(tasks=op_tasks, results=results), results={"artist_folder_merge": unsupported})
    add("resume_saved_op_failed", _ckpt(tasks=op_tasks, results=results), results={"artist_folder_merge": {"ok": False, "error": "x"}})
    add("resume_saved_op_cancel", _ckpt(tasks=op_tasks, results=results), cancel_at="artist_folder_merge")
    add("reinvoke_resume_saved_op", _ckpt(_reinvoke(), tasks=op_tasks, results=results))

    # Other jobs already running.
    add("artwork_job_running", running={"album-art-rebuild"})
    add("genres_job_running", running={"fix-genres"})
    add("art_child_did_not_start", results={"start_art": ({"ok": False, "error": "busy"}, 409)})
    add("genre_child_did_not_start", results={"start_genres": ({"ok": True}, 200)})

    # Resume from each position, in each saved status.
    for k in range(len(TASK_IDS) + 1):
        tasks, results = _prefix(k)
        add(f"resume_prefix_{k}", _ckpt(tasks=tasks, results=results))
        tasks, results = _prefix(k, "skipped")
        add(f"resume_prefix_skipped_{k}", _ckpt(tasks=tasks, results=results))
    s["resume_after_alias"] = (_ckpt(tasks={**_prefix(TASK_IDS.index("artist_alias") + 1)[0]},
                                     results=_prefix(TASK_IDS.index("artist_alias") + 1)[1]), {})
    for status in ("partial", "failed", "cancelled", "running", "interrupted", "complete", "completed", "success", ""):
        tasks, results = _prefix(3)
        add(f"previous_status_{status or 'blank'}", _ckpt(status=status, tasks=tasks, results=results))
    add("resume_force_fresh_discards", _ckpt(lambda w: w.post({"force_fresh": True}), tasks=_prefix(3)[0], results=_prefix(3)[1]))
    add("resume_no_tasks_list", _ckpt(omit_tasks=True))
    add("resume_garbage_report", lambda w: (w.checkpoint.write_text('{"last_run": 3}', encoding="utf-8"), w.post()))
    add("reinvoke_resumed_run", _ckpt(_reinvoke(), tasks=_prefix(5)[0], results=_prefix(5)[1]))
    add("reinvoke_fresh_run", _reinvoke())
    for tid in ("library_health", "missing_files", "artist_alias", "root_folder_repair", "release_group_merge", "duplicates"):
        tasks = {**_prefix(TASK_IDS.index(tid))[0], tid: "running"}
        add(f"resume_interrupted_running_{tid}", _ckpt(tasks=tasks, results=_prefix(TASK_IDS.index(tid))[1]))
        add(f"resume_interrupted_running_with_result_{tid}", _ckpt(tasks=tasks, results=_prefix(TASK_IDS.index(tid) + 1)[1]))
        tasks = {**_prefix(TASK_IDS.index(tid))[0], tid: "failed"}
        add(f"resume_failed_{tid}", _ckpt(tasks=tasks, results=_prefix(TASK_IDS.index(tid))[1]))

    # Saved results of unusual shapes.
    health_done = {"library_health": "complete"}
    add("resume_health_result_not_dict", _ckpt(tasks=health_done, results={"library_health": "odd"}))
    add("resume_health_result_absent", _ckpt(tasks=health_done, results={}))
    missing_done = {"library_health": "complete", "missing_files": "complete"}
    for removed in (0, 1):
        add(f"resume_missing_removed_{removed}", _ckpt(tasks=missing_done, results={
            "library_health": {"ok": True}, "missing_files": {"removed_db_rows": removed}}))
    add("resume_missing_result_not_dict", _ckpt(tasks=missing_done, results={"library_health": {}, "missing_files": "odd"}))
    add("resume_snapshot_results_not_dict", resume_snapshot={"resumable": True, "tasks": [
        {"id": t, "label": LABELS[t], "status": "pending"} for t in TASK_IDS], "results": "odd", "previous_status": "partial"})
    add("snapshot_not_resumable_keeps_its_tasks", resume_snapshot={"resumable": False, "tasks": [
        {"id": t, "label": LABELS[t], "status": "complete" if t == "library_health" else "pending"} for t in TASK_IDS],
        "results": {"library_health": {"ok": True}}})
    add("snapshot_without_status_or_counts", resume_snapshot={"resumable": True, "tasks": [
        {"id": t, "label": LABELS[t], "status": "pending"} for t in TASK_IDS], "results": {}})

    # Fault at every job-state update and report save, once and from there on,
    # and a cancel arriving at every update.
    for n in range(1, UPDATE_SWEEP + 1):
        add(f"sweep_update_fault_{n}", update_fault=(n, False))
        add(f"sweep_update_fault_sticky_{n}", update_fault=(n, True))
        add(f"sweep_cancel_at_update_{n}", cancel_on_update=n)
    for n in range(1, SAVE_SWEEP + 1):
        add(f"sweep_save_fault_{n}", save_fault=(n, False))
    tasks, results = _prefix(TASK_IDS.index("artist_folder_merge"))
    for n in range(1, 25):
        add(f"sweep_resume_op_cancel_at_update_{n}", _ckpt(tasks=op_tasks, results=results), cancel_on_update=n)
    return s


UPDATE_SWEEP = 80
SAVE_SWEEP = 47


def _outcome(world, trace):
    result = world.outcome_result if isinstance(world.outcome_result, dict) else {}
    last_run = (trace["report"] or {}).get("last_run") if isinstance(trace["report"], dict) else None
    return {
        "http": (world.response or {}).get("status"),
        "ok": result.get("ok"),
        "status": result.get("status"),
        "error": world.outcome_error["message"] if world.outcome_error else result.get("error"),
        "last_run_status": last_run.get("status") if isinstance(last_run, dict) else None,
        "tasks": ",".join(f"{t['id']}={t['status']}" for t in (result.get("tasks") or [])),
        "events": len(trace["events"]),
    }


def _run_scenario(name, script, opts):
    with tempfile.TemporaryDirectory() as tmp:
        world = _World(tmp, dict(opts))
        trace = world.play(script)
    text = json.dumps(trace, sort_keys=True, separators=(",", ":"))
    dump = os.environ.get("CLEAN_ALL_TRACE_DUMP")
    if dump:
        Path(dump).mkdir(parents=True, exist_ok=True)
        (Path(dump) / f"{name}.json").write_text(json.dumps(trace, sort_keys=True, indent=1), encoding="utf-8")
    entry = {"sha256": hashlib.sha256(text.encode()).hexdigest(), "outcome": _outcome(world, trace)}
    if name in FULL_TRACES:
        entry["trace"] = trace
    return entry


class CleanAllCharacterization(unittest.TestCase):
    maxDiff = None

    def test_traces_match_golden(self):
        actual = {name: _run_scenario(name, script, opts) for name, (script, opts) in sorted(_scenarios().items())}
        if os.environ.get("UPDATE_CLEAN_ALL_GOLDEN"):
            GOLDEN.write_text(json.dumps(actual, sort_keys=True, indent=1) + "\n", encoding="utf-8")
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(sorted(golden), sorted(actual))
        different = [name for name in actual if golden[name]["sha256"] != actual[name]["sha256"]]
        for name in different[:5]:
            with self.subTest(scenario=name):
                self.assertEqual(golden[name].get("outcome"), actual[name]["outcome"])
                self.assertEqual(golden[name].get("trace"), actual[name].get("trace"))
        self.assertEqual(different, [], f"{len(different)} of {len(actual)} traces differ")

    def test_sweeps_reach_the_end_of_a_run(self):
        """Grow UPDATE_SWEEP/SAVE_SWEEP if a run ever needs more fault points."""
        with tempfile.TemporaryDirectory() as tmp:
            world = _World(tmp, {})
            world.play(_post())
        self.assertLess(world.updates, UPDATE_SWEEP)
        self.assertLess(world.saves, SAVE_SWEEP)
        self.assertEqual(TASK_IDS[0], "library_health")


if __name__ == "__main__":
    unittest.main()
