"""Maintenance runner, library health and root-folder repair (ARCH-001).
"""

from __future__ import annotations

import json, re, threading, time
from pathlib import Path
from typing import Any, Dict, List, Optional
from backend.app_runtime import _app_logger, MAINTENANCE_RUNNER_LAST_FILE, MUSIC_ROOT, _s
from backend.library_service import _append_stamp_skipped_log, _record_scan, _refresh_library_cache, _root_folder_repair_apply_safe, _root_folder_repair_save_report, _run_normalize_artists_if_needed, _scan_folder_name_placeholders, _stamp_artist_folder_scan
from backend.pending_review_store import _load_pending_reviews
from backend.import_reconciliation_service import _ARTIST_RECONCILE_TERMINAL_FAILURE_STATUSES, _ARTIST_RECONCILE_TERMINAL_SUCCESS_STATUSES, _apply_artist_folder_reconcile_resilient
from backend.cleanup_service import _album_cleanup_save_report, _album_folder_cleanup_apply_safe, _clean_remove_orphaned_items, _load_rgid_resolutions
from backend.app_runtime import _path_under, _safe_inventory_error_message, _safe_operation_status_error_message
from backend.beets_adapter import BeetsError, BeetsUnavailableError
import backend.composite_workflows as composite_workflows
import backend.duplicate_identity as _duplicate_identity
from backend.matching_service import _invalidate_lib_cache
from backend.app_runtime import jobs
from backend.job_service import _root_folder_repair_running_job, _running_job_of_type
from backend.serializers import _json_from_flask_response

# ── ARCH-001 extracted code ──


_FULL_SCAN_INTERVAL  = 1800   # 30 min — full DB reconciliation + cleanup


_QUICK_SCAN_INTERVAL = 120    # 2 min  — invalidate lib cache so next /api/library is fresh


# Mutated in place (never rebound) so the scan service and the status route share it.
_SCAN_STATE: Dict[str, Optional[str]] = {"last_job_id": None}


def _do_scan_job() -> str:
    """Start a Python-native library scan. Returns job_id."""

    def _scan(log, cancel_event=None):
        def _check_cancelled() -> None:
            if cancel_event is not None and cancel_event.is_set():
                log.append("[cancelled]")
                raise RuntimeError("cancelled")

        started = time.time()
        log.append("phase:read-db")
        _check_cancelled()
        try:
            stats_res = composite_workflows.get_library_stats()
            total_tracks = int(stats_res.get("tracks", 0))
            total_albums = int(stats_res.get("albums", 0))
            log.append(f"phase:read-db rows:{total_tracks}")

            log.append("phase:scan-disk")
            _check_cancelled()

            log.append("phase:check-missing")
            _check_cancelled()
            sync_res = composite_workflows.sync_deleted_files(dry_run=False, limit=50000)
            missing_count = int(sync_res.get("missing_count", 0))
            removed_items = int(sync_res.get("removed_from_db", 0))

            empty_res = composite_workflows.clean_empty_albums(dry_run=False)
            removed_empty = int(empty_res.get("removed_count", 0))
            removed_count = removed_items + removed_empty

            if removed_count:
                log.append(f"cleaned:{removed_count} stale DB entr{'y' if removed_count==1 else 'ies'} removed")

            elapsed = int(time.time() - started)
            status = "ok"
            log.append(f"status:{status}")
            log.append(f"tracks:{max(0, total_tracks - removed_items)}")
            log.append(f"albums:{max(0, total_albums - removed_empty)}")
            log.append(f"missing:{missing_count}")
            log.append(f"removed:{removed_count}")
            log.append("unimported:0")
            log.append(f"elapsed_seconds:{elapsed}")
        except (BeetsUnavailableError, BeetsError) as ex:
            log.append(f"ERROR: Library scan failed: engine unavailable ({ex})")
            raise RuntimeError(f"Library scan failed: {ex}") from ex

    job = jobs.start_python(_scan, label="Library scan", metadata={"type": "library-scan", "mutating": False})
    _SCAN_STATE["last_job_id"] = job.job_id

    def _watch():
        for _ in range(1800):
            time.sleep(2)
            j = jobs.get(job.job_id)
            if j and j.status in ("success", "failed"):
                if j.status == "success":
                    _record_scan()
                break
    threading.Thread(target=_watch, daemon=True).start()
    return job.job_id


def _auto_scan_loop():
    """Two-tier auto-scan (no button needed):
    - Every 2 min  → proactively rebuild the lib cache in the background
    - Every 30 min → full DB reconciliation (remove stale entries) + auto-normalize names
    """
    time.sleep(30)   # let Flask finish starting
    _run_normalize_artists_if_needed()   # fix any Unicode names immediately on startup
    last_full  = 0.0
    last_quick = 0.0
    while True:
        try:
            now = time.time()
            # ── Full scan (every 30 min) ──────────────────────────────────────
            if now - last_full >= _FULL_SCAN_INTERVAL:
                jid = _do_scan_job()
                last_full  = now
                last_quick = now   # full scan counts as a quick scan too
                # Cap memory growth without wiping recent operator-facing job
                # results/logs from Clean, Import, Playlists, and repair flows.
                jobs.prune_finished()
                _captured_jid = jid
                def _post_scan():
                    for _ in range(900):
                        time.sleep(2)
                        j = jobs.get(_captured_jid)
                        if j and j.status in ("success", "failed"):
                            if j.status == "success":
                                _run_normalize_artists_if_needed()
                            break
                threading.Thread(target=_post_scan, daemon=True).start()
            # ── Quick tick (every 2 min) ──────────────────────────────────────
            # Rebuild in-place rather than just invalidating: this used to
            # only invalidate, which forced whichever visitor loaded /library
            # next to pay for the full rebuild synchronously (confirmed the
            # dominant cause of "library takes forever to load every time" --
            # the cache was rarely surviving more than ~2 minutes). Doing the
            # rebuild here keeps the page fast without giving up the "stay
            # in sync with external disk changes" behavior this loop exists
            # for.
            elif now - last_quick >= _QUICK_SCAN_INTERVAL:
                _refresh_library_cache()
                last_quick = now
        except Exception as _e:
            import logging as _lg
            _lg.getLogger(__name__).warning("_auto_scan_loop error: %s", _e)
        time.sleep(20)   # heartbeat every 20s


def _library_health_payload(orphan_sample_limit: int = 100,
                            duplicate_limit: int = 100,
                            empty_limit: int = 100,
                            progress: Optional[Any] = None) -> Dict[str, Any]:
    if progress:
        progress({
            "category": "Cleanup",
            "current_task": "Reading Beets database health",
            "current_result": "Fetching health report from Beets engine",
        })
    res = composite_workflows.get_library_health(
        orphan_sample_limit=orphan_sample_limit,
        duplicate_limit=duplicate_limit,
        empty_limit=empty_limit,
    )
    rgid_resolutions = _load_rgid_resolutions()
    # The engine deliberately returns rgid_duplicate_groups UNTRUNCATED (see
    # its own comment in _get_library_health_report) precisely so this split
    # -- and the counts/truncation below -- operate on the true total, not a
    # window the engine already cut before resolution state (Web-Manager-
    # owned, unknown to the engine) could be applied. Splitting first and
    # truncating each resulting list afterward, unconditionally (not only
    # when rgid_resolutions is non-empty), matches the pre-existing
    # behavior this endpoint replaced: a "keep separate" group must never be
    # silently dropped just because it fell outside a pre-split window, and
    # the reported counts must always reflect the true totals.
    full_rgid_groups = res.get("rgid_duplicate_groups", [])
    rgid_resolved_groups = list(res.get("rgid_resolved_groups", []))
    unresolved_groups = []
    for group in full_rgid_groups:
        rgid = group.get("mb_releasegroupid", "")
        resolution = rgid_resolutions.get(rgid)
        if resolution and resolution.get("decision") == "keep_separate":
            group["resolution"] = resolution
            rgid_resolved_groups.append(group)
        else:
            unresolved_groups.append(group)
    res["rgid_duplicate_group_count"] = len(unresolved_groups)
    res["rgid_resolved_group_count"] = len(rgid_resolved_groups)
    res["rgid_duplicate_groups"] = unresolved_groups[:duplicate_limit]
    res["rgid_resolved_groups"] = rgid_resolved_groups[:duplicate_limit]
    if "final_summary" in res:
        res["final_summary"]["same_release_group_id_groups"] = len(unresolved_groups)

    if progress:
        summary = res.get("final_summary", {})
        progress({
            "category": "Cleanup",
            "current_task": "Library database health scan completed",
            "scanned_count": summary.get("database_rows_scanned", 0),
            "total_count": summary.get("database_rows_scanned", 0),
            "albums_count": summary.get("albums_count", 0),
            "tracks_count": summary.get("tracks_count", 0),
            "duplicate_album_groups": summary.get("duplicate_album_groups", 0),
            "same_release_group_id_groups": summary.get("same_release_group_id_groups", 0),
            "empty_albums": summary.get("empty_albums", 0),
            "orphaned_items": summary.get("orphaned_items", 0),
            "missing_files": summary.get("missing_files", 0),
            "current_result": "Health scan completed",
        })
    return res


def _maintenance_safe_folder_renames(rows: List[Dict[str, Any]], log: List[str],
                                     cancel_event=None) -> Dict[str, Any]:
    """Apply only safe_rename folder placeholder rows.

    This intentionally excludes target_exists, DB-tracked, merge/delete, and
    conflict cases. It never overwrites an existing folder and never deletes
    media files.
    """
    renamed = 0
    skipped = 0
    errors = 0
    examples: List[Dict[str, str]] = []
    for row in rows:
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("cancelled")
        source_raw = _s(row.get("folder")).strip()
        target_raw = _s(row.get("proposed_folder")).strip()
        safe = bool(row.get("safe"))
        if (
            not safe
            or row.get("target_exists")
            or int(row.get("db_item_count") or 0) > 0
            or not source_raw
            or not target_raw
        ):
            skipped += 1
            continue
        try:
            source = Path(source_raw).resolve(strict=False)
            target = Path(target_raw).resolve(strict=False)
            if not _path_under(source, MUSIC_ROOT) or not _path_under(target, MUSIC_ROOT):
                skipped += 1
                log.append(f"  [folder-safe-rename] skipped outside library root: {source_raw}")
                continue
            if source == target:
                skipped += 1
                continue
            if not source.exists() or not source.is_dir():
                skipped += 1
                log.append(f"  [folder-safe-rename] skipped missing source: {source}")
                continue
            if target.exists():
                skipped += 1
                log.append(f"  [folder-safe-rename] skipped existing target: {target}")
                continue
            res = composite_workflows.move_file(str(source), str(target))
            if not res.get("ok"):
                raise RuntimeError(res.get("error") or "move failed")
            renamed += 1
            if len(examples) < 20:
                examples.append({"source": str(source), "target": str(target)})
            log.append(f"  [folder-safe-rename] renamed: {source.name!r} -> {target.name!r}")
        except Exception as exc:
            errors += 1
            log.append(f"  [folder-safe-rename] error for {source_raw}: {exc}")
    if renamed:
        _invalidate_lib_cache()
    return {
        "renamed": renamed,
        "skipped": skipped,
        "errors": errors,
        "examples": examples,
    }


MAINTENANCE_RUNNER_TASKS = [
    {"id": "library_health", "label": "Library DB Health Check"},
    {"id": "missing_files", "label": "Missing Files Scan"},
    {"id": "root_folder_repair", "label": "Root Folder Repair"},
    {"id": "artist_alias", "label": "Artist Alias / MBID Variant Check"},
    {"id": "artist_folder_merge", "label": "Artist Folder Merge"},
    {"id": "release_group_merge", "label": "Release Group Merge"},
    {"id": "duplicates", "label": "Duplicate Track Scan"},
    {"id": "folder_scan", "label": "Folder Name Scan"},
    {"id": "folder_safe_renames", "label": "Folder Safe Renames"},
    {"id": "artwork", "label": "Artwork Fetch"},
    {"id": "genres", "label": "Genre Tagging"},
    {"id": "final_verification", "label": "Final Verification"},
    {"id": "stale_jobs", "label": "Stale Job Cleanup"},
    {"id": "playlist_refs", "label": "Playlist Reference Check"},
]


def _maintenance_initial_task_state() -> List[Dict[str, Any]]:
    return [
        {"id": task["id"], "label": task["label"], "status": "pending"}
        for task in MAINTENANCE_RUNNER_TASKS
    ]


def _maintenance_load_last_report() -> Dict[str, Any]:
    try:
        if MAINTENANCE_RUNNER_LAST_FILE.exists():
            loaded = json.loads(MAINTENANCE_RUNNER_LAST_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded
    except Exception:
        pass
    return {}


def _maintenance_checkpoint_results(report: Dict[str, Any], last_run: Dict[str, Any]) -> Dict[str, Any]:
    results: Dict[str, Any] = {}
    raw_results = last_run.get("results")
    if isinstance(raw_results, dict):
        results.update(raw_results)
    for task in MAINTENANCE_RUNNER_TASKS:
        task_id = _s(task.get("id"))
        if task_id and task_id not in results and isinstance(report.get(task_id), dict):
            results[task_id] = report.get(task_id)
    return results


def _maintenance_resume_from_report(report: Dict[str, Any]) -> Dict[str, Any]:
    last_run = report.get("last_run")
    if not isinstance(last_run, dict):
        return {"resumable": False}
    previous_status = _s(last_run.get("status")).strip().lower()
    if previous_status in {"complete", "completed", "success"}:
        return {"resumable": False}
    raw_tasks = last_run.get("tasks")
    if not isinstance(raw_tasks, list):
        return {"resumable": False}

    previous_by_id: Dict[str, Dict[str, Any]] = {}
    for item in raw_tasks:
        if not isinstance(item, dict):
            continue
        task_id = _s(item.get("id")).strip()
        if task_id:
            previous_by_id[task_id] = item

    results = _maintenance_checkpoint_results(report, last_run)
    tasks = _maintenance_initial_task_state()
    completed = 0
    for task in tasks:
        task_id = _s(task.get("id")).strip()
        previous = previous_by_id.get(task_id) or {}
        status = _s(previous.get("status")).strip().lower()
        saved_operation_id = _s(previous.get("operation_id")).strip()
        if status == "running":
            if task_id in {"library_health", "missing_files", "artist_alias"} and task_id in results:
                status = "complete"
            elif task_id == "artist_folder_merge" and saved_operation_id:
                # A saved operation_id means real engine-side work (a
                # Beets Engine artist_folder_reconcile_v1 transaction) may
                # still be in flight -- or may already have completed --
                # even though THIS process was interrupted mid-run. Stay
                # "running" and carry the operation_id forward so the
                # resumed run checks that operation's own authoritative
                # transaction status before creating any new Plan, instead
                # of discarding it and starting a redundant duplicate
                # operation for the same folders.
                task["operation_id"] = saved_operation_id
            else:
                status = "pending"
        if status == "running":
            task["status"] = "running"
            if _s(previous.get("detail")).strip():
                task["detail"] = _s(previous.get("detail")).strip()
            continue
        if status not in {"complete", "skipped"}:
            task["status"] = "pending"
            task.pop("detail", None)
            continue
        if status == "complete" and task_id not in results:
            task["status"] = "pending"
            task.pop("detail", None)
            continue
        task["status"] = status
        if _s(previous.get("detail")).strip():
            task["detail"] = _s(previous.get("detail")).strip()
        completed += 1

    explicit_next_task = _s(last_run.get("next_task")).strip()
    if explicit_next_task:
        seen_next = False
        for task in tasks:
            task_id = _s(task.get("id")).strip()
            if task_id == explicit_next_task:
                seen_next = True
            if seen_next and task.get("status") == "complete" and task_id not in results:
                task["status"] = "pending"
                task.pop("detail", None)

    next_task = next((task for task in tasks if task.get("status") not in {"complete", "skipped"}), None)
    if completed <= 0 or not next_task:
        return {"resumable": False}
    return {
        "resumable": True,
        "previous_status": previous_status,
        "completed_count": completed,
        "remaining_count": max(0, len(tasks) - completed),
        "next_task": next_task.get("id"),
        "next_task_label": next_task.get("label"),
        "updated_at": last_run.get("updated_at") or report.get("updated_at"),
        "tasks": tasks,
        "results": results,
        "result_task_ids": sorted(str(key) for key in results.keys()),
    }


def _maintenance_resume_summary(report: Dict[str, Any]) -> Dict[str, Any]:
    resume = _maintenance_resume_from_report(report)
    if not resume.get("resumable"):
        return {"resumable": False}
    return {
        "resumable": True,
        "previous_status": resume.get("previous_status"),
        "completed_count": resume.get("completed_count"),
        "remaining_count": resume.get("remaining_count"),
        "next_task": resume.get("next_task"),
        "next_task_label": resume.get("next_task_label"),
        "updated_at": resume.get("updated_at"),
    }


def _maintenance_artist_folder_merge_step(
    log: List[str],
    cancel_event: Any,
    root: str,
    *,
    resume_operation_id: str = "",
    on_operation_planned: Optional[Any] = None,
) -> Dict[str, Any]:
    """Run (or resume) the artist-folder MBID-stamping/merge step, shared by
    the /api/clean/artist-folders/stamp-mbid route and Clean All's
    "Artist Folder Merge" task.

    Clean All resume (hotfix v0.1.17 follow-up): if resume_operation_id is
    given, its transaction status is checked FIRST, before any new Plan is
    created. This is what lets Clean All survive a process restart mid-Apply
    without blindly re-Applying or creating a second, redundant operation
    for the same folders -- the exact gap the previous implementation had:
    only an *in-process* running job was checked (_running_job_of_type),
    which finds nothing at all after a real process restart, so a resumed
    run used to always start a brand new Plan+Apply regardless of whether
    the interrupted operation was still running or had already completed on
    the engine.

    on_operation_planned(op_id), when given, is invoked the moment a NEW
    Plan succeeds (before Apply is ever called) so the caller can persist
    that operation_id to its own checkpoint immediately -- surviving a
    crash between Plan and Apply, not just during Apply.
    """
    if resume_operation_id:
        try:
            tx_res = composite_workflows.get_transaction(resume_operation_id)
            tx = tx_res.get("transaction") or {}
            status = str(tx.get("status") or "")
        except Exception as ex:
            # Independent review finding: a lookup failure (timeout,
            # connection reset, malformed response, transient 503, ...)
            # does NOT prove the saved operation is gone -- it means its
            # status is currently unknown. Falling through to a fresh scan
            # here would recreate the exact duplicate-operation risk this
            # hotfix exists to eliminate (a second Plan/Apply for the same
            # folders while the original operation may still be genuinely
            # in progress on the engine). Preserve the operation_id and
            # report "still unresolved" instead: the caller keeps the task
            # "running" with this same operation_id in the checkpoint, and
            # the next resume checks it again -- never re-Applies, never
            # re-Plans, until the engine's own authoritative status
            # conclusively says otherwise.
            # CodeQL: information exposure through an exception -- {ex} can
            # carry internal URLs, paths, or transport details, and this
            # message flows into a job-visible log line and an "error"
            # field. Log the real exception server-side only.
            _app_logger.error(
                "Artist folder merge: status check for saved operation %s failed: %s",
                resume_operation_id, ex, exc_info=True,
            )
            safe_reason = _safe_operation_status_error_message(ex)
            log.append(
                f"Artist folder merge: could not check saved operation {resume_operation_id} ({safe_reason}); "
                f"its status is unknown -- not creating a new plan or re-applying, will retry on the next run."
            )
            return {
                "ok": False, "operation_id": resume_operation_id,
                "renamed": 0, "merged": 0, "skipped": 0,
                "error": safe_reason,
                "still_running": True,
            }

        if tx is not None and status in _ARTIST_RECONCILE_TERMINAL_SUCCESS_STATUSES:
            log.append(f"Artist folder merge: resumed operation {resume_operation_id} was already Completed.")
            return {
                "ok": True, "operation_id": resume_operation_id,
                "renamed": int(tx.get("moved_files") or 0), "merged": int(tx.get("quarantined_files") or 0),
                "skipped": 0,
            }
        if tx is not None and status in _ARTIST_RECONCILE_TERMINAL_FAILURE_STATUSES:
            log.append(
                f"Artist folder merge: resumed operation {resume_operation_id} ended {status}; it will not "
                f"be retried. Re-scanning for anything still remaining."
            )
            # Falls through to the fresh-scan path below -- a terminally
            # failed/rolled-back/cancelled operation is conclusively not
            # recoverable, so this is the one case where starting a new
            # Plan is correct rather than a violation of "never re-Apply".
        elif tx is not None and status:
            # Preview/Pending/Approved/Running: genuinely still in progress
            # on the engine. skip_initial_apply=True is only safe -- and
            # only used -- for "Running", where Apply is already known (via
            # this authoritative status check, not an assumption) to have
            # been accepted by the engine; Preview/Pending/Approved mean
            # Apply itself was never confirmed sent, so it still needs to
            # be called exactly once, same as a fresh operation.
            apply_res = _apply_artist_folder_reconcile_resilient(
                resume_operation_id, log, cancel_event=cancel_event, log_prefix="Artist folder merge",
                skip_initial_apply=(status == "Running"),
            )
            if not apply_res.get("ok"):
                return {
                    "ok": False, "operation_id": resume_operation_id,
                    "renamed": 0, "merged": 0, "skipped": 0, "error": apply_res.get("error"),
                    "still_running": bool(apply_res.get("still_running")),
                }
            return {
                "ok": True, "operation_id": resume_operation_id,
                "renamed": int(apply_res.get("moved_files") or 0), "merged": int(apply_res.get("quarantined_files") or 0),
                "skipped": 0,
            }

    root_path = Path(root)
    scan = _stamp_artist_folder_scan(root_path)
    if not scan.get("ok", True):
        # Independent review finding: an engine inventory failure must never
        # be reported as "no work to do" -- that would let Clean All mark
        # this phase complete while the engine was never actually reached.
        # No operation_id exists yet at this point (no Plan was ever
        # created), so this is a genuine "failed" outcome -- not
        # "still_running" -- meaning the next resume correctly retries the
        # scan from scratch rather than treating a nonexistent operation as
        # still in flight.
        log.append(f"Artist folder merge: engine inventory scan failed ({scan.get('error')}); MBID stamping was not performed.")
        return {
            "ok": False, "renamed": 0, "merged": 0, "skipped": 0,
            "error": scan.get("error"), "error_code": scan.get("error_code", ""),
        }
    candidates = scan["candidates"]
    skipped = scan["skipped"]
    if not candidates:
        log.append("No artist folders need MB ID stamping.")
        _append_stamp_skipped_log(log, skipped, include_examples=False)
        return {"ok": True, "renamed": 0, "merged": 0, "skipped": 0}

    log.append(f"Stamping MB IDs on {len(candidates)} artist folder(s)…")
    payload = {"root": str(root_path), "mode": "stamp_mbid"}
    # SEC-002 Wave 21 final review: no local in-process fallback.
    try:
        plan_res = composite_workflows.plan_artist_folder_reconcile(payload)
    except (BeetsUnavailableError, BeetsError) as ex:
        # CodeQL: information exposure through an exception -- str(ex) must
        # not flow into this "error" field (job-visible result). Log the
        # real exception server-side only.
        log.append("Engine unavailable; MBID stamping was not performed.")
        _app_logger.error("MBID stamping: engine unavailable: %s", ex, exc_info=True)
        return {"ok": False, "renamed": 0, "merged": 0, "skipped": len(skipped), "error": _safe_inventory_error_message(ex)}
    except Exception as ex:
        log.append("Engine communication failed; MBID stamping was not performed.")
        _app_logger.error("MBID stamping: unexpected engine communication failure: %s", ex, exc_info=True)
        return {"ok": False, "renamed": 0, "merged": 0, "skipped": len(skipped), "error": _safe_inventory_error_message(ex)}

    if not plan_res.get("ok"):
        log.append(f"Refusing to operate: {plan_res.get('error')}")
        return {"ok": False, "renamed": 0, "merged": 0, "skipped": len(skipped), "error": plan_res.get("error")}

    op_id = plan_res.get("operation_id")
    if not op_id:
        log.append(_s(plan_res.get("message")) or "No artist folder move was required.")
        return {"ok": True, "renamed": 0, "merged": 0, "skipped": len(skipped)}

    if on_operation_planned is not None:
        on_operation_planned(op_id)

    apply_res = _apply_artist_folder_reconcile_resilient(op_id, log, cancel_event=cancel_event, log_prefix="MBID stamping")
    if not apply_res.get("ok"):
        log.append(f"Engine MBID stamping failed: {apply_res.get('error')}")
        return {
            "ok": False, "renamed": 0, "merged": 0, "skipped": len(skipped), "error": apply_res.get("error"),
            "operation_id": op_id, "still_running": bool(apply_res.get("still_running")),
        }

    _invalidate_lib_cache()
    log.append(f"  [stamp] Delegated MBID stamping to engine (op_id={op_id})")
    return {
        "ok": True,
        "operation_id": op_id,
        "renamed": apply_res.get("moved_files", 0),
        "merged": apply_res.get("quarantined_files", 0),
        "skipped": len(skipped),
    }


def _maintenance_running_job() -> Optional[Any]:
    for job in jobs.all():
        metadata = getattr(job, "metadata", {}) or {}
        if metadata.get("type") == "maintenance-runner" and job.status == "running":
            return job
    return None


def _maintenance_extract_child_job_id(response: Any) -> str:
    data = _json_from_flask_response(response)
    if not data.get("ok") or not data.get("job_id"):
        raise RuntimeError(data.get("error") or "maintenance child job did not start")
    return _s(data.get("job_id")).strip()


def _maintenance_task_result_summary(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    final = result.get("final_summary")
    if isinstance(final, dict):
        result = final
    if "renamed" in result or "merged" in result:
        return (
            f"renamed: {int(result.get('renamed') or 0)}, "
            f"merged: {int(result.get('merged') or 0)}, "
            f"skipped: {int(result.get('skipped') or 0)}"
        )
    if any(key in result for key in ("files_moved", "folders_deleted", "duplicate_files_quarantined")):
        return (
            f"moved: {int(result.get('files_moved') or 0)}, "
            f"duplicates: {int(result.get('duplicate_files_quarantined') or result.get('duplicate_files_removed') or 0)}, "
            f"folders removed: {int(result.get('folders_deleted') or result.get('folders_removed') or 0)}"
        )
    for key in (
        "duplicate_tracks_found",
        "deleted_files",
        "saved",
        "placeholder_folders_found",
        "safe_to_fix",
        "duplicate_album_groups",
        "same_release_group_id_groups",
        "duplicate_artist_mbid_groups_remaining",
        "duplicate_release_group_id_groups_remaining",
        "duplicate_recording_mbid_groups_remaining",
        "checked",
        "verified",
        "metadata_mismatch",
        "replacement_required",
        "needs_replacement",
        "complete",
        "missing_files",
        "orphaned_items",
        "count",
        "candidates",
        "changed_count",
    ):
        value = result.get(key)
        if isinstance(value, (int, float)):
            return f"{key.replace('_', ' ')}: {int(value)}"
    return ""


def _maintenance_clean_all_counts(tasks: List[Dict[str, Any]], results: Dict[str, Any]) -> Dict[str, int]:
    failed = sum(1 for task in tasks if task.get("status") == "failed")
    fixed = 0
    removed = 0
    needs_review = 0
    needs_submission = 0

    rename_result = results.get("folder_safe_renames")
    if isinstance(rename_result, dict):
        fixed += int(rename_result.get("renamed") or 0)
        needs_review += int(rename_result.get("skipped_unsafe") or 0)

    genres = results.get("genres")
    if isinstance(genres, dict):
        summary = genres.get("final_summary") if isinstance(genres.get("final_summary"), dict) else genres
        fixed += int((summary or {}).get("changed_count") or (summary or {}).get("updated") or 0)

    artwork = results.get("artwork")
    if isinstance(artwork, dict):
        summary = artwork.get("final_summary") if isinstance(artwork.get("final_summary"), dict) else artwork
        fixed += int((summary or {}).get("saved") or (summary or {}).get("updated") or 0)

    duplicates = results.get("duplicates")
    if isinstance(duplicates, dict):
        summary = duplicates.get("final_summary") if isinstance(duplicates.get("final_summary"), dict) else duplicates
        removed += int((summary or {}).get("deleted_files") or 0)
        needs_review += int((summary or {}).get("skipped_candidates") or 0)

    audio_identity = results.get("audio_identity")
    if isinstance(audio_identity, dict):
        summary = audio_identity.get("final_summary") if isinstance(audio_identity.get("final_summary"), dict) else audio_identity
        fixed += int((summary or {}).get("metadata_mismatch") or 0)
        needs_review += int((summary or {}).get("review_required") or 0)
        needs_submission += int((summary or {}).get("missing_musicbrainz_identity") or 0)
        needs_submission += int((summary or {}).get("missing_acoustid_match") or 0)

    metadata_repair = results.get("metadata_repair")
    if isinstance(metadata_repair, dict):
        summary = metadata_repair.get("final_summary") if isinstance(metadata_repair.get("final_summary"), dict) else metadata_repair
        fixed += int((summary or {}).get("track_rows") or 0)
        fixed += int((summary or {}).get("release_item_rows") or 0)
        fixed += int((summary or {}).get("resolved_album_rows") or 0)
        needs_review += int((summary or {}).get("unresolved_count") or 0)

    music_format = results.get("music_format_scan")
    if isinstance(music_format, dict):
        summary = music_format.get("final_summary") if isinstance(music_format.get("final_summary"), dict) else music_format
        needs_review += int((summary or {}).get("needs_replacement") or 0)

    replacement = results.get("verified_replacement")
    replaced = 0
    if isinstance(replacement, dict):
        summary = replacement.get("final_summary") if isinstance(replacement.get("final_summary"), dict) else replacement
        replaced = int((summary or {}).get("complete") or 0)
        fixed += replaced
        needs_review += int((summary or {}).get("failed") or 0)

    health = results.get("library_health")
    if isinstance(health, dict):
        needs_review += int(health.get("orphaned_item_count") or 0)
        needs_review += int(health.get("empty_album_count") or 0)

    artist_merge = results.get("artist_folder_merge")
    if isinstance(artist_merge, dict):
        fixed += int(artist_merge.get("renamed") or 0)
        fixed += int(artist_merge.get("merged") or 0)

    release_group_merge = results.get("release_group_merge")
    if isinstance(release_group_merge, dict):
        summary = release_group_merge.get("final_summary") if isinstance(release_group_merge.get("final_summary"), dict) else release_group_merge.get("summary", release_group_merge)
        fixed += int((summary or {}).get("files_moved") or 0)
        fixed += int((summary or {}).get("folders_deleted") or 0)
        removed += int((summary or {}).get("duplicate_files_quarantined") or 0)
        needs_review += int((summary or {}).get("needs_review") or 0)

    verification = results.get("final_verification")
    if isinstance(verification, dict):
        summary = verification.get("final_summary") if isinstance(verification.get("final_summary"), dict) else verification
        needs_review += int((summary or {}).get("manual_review_items") or 0)
        needs_submission += int((summary or {}).get("submission_queue_items") or 0)

    try:
        needs_submission += len(_load_pending_reviews() or [])
    except Exception:
        pass

    return {
        "scanned": sum(1 for task in tasks if task.get("status") in {"complete", "failed", "skipped"}),
        "verified": sum(1 for task in tasks if task.get("status") == "complete"),
        "fixed": fixed,
        "replaced": replaced,
        "removed": removed,
        "needs_submission": needs_submission,
        "needs_review": needs_review,
        "failed": failed,
    }


def _maintenance_save_last_report(update: Dict[str, Any], log: Optional[List[str]] = None) -> None:
    report: Dict[str, Any] = _maintenance_load_last_report()
    report.update(update)
    report["updated_at"] = time.time()
    try:
        MAINTENANCE_RUNNER_LAST_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = MAINTENANCE_RUNNER_LAST_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
        tmp.replace(MAINTENANCE_RUNNER_LAST_FILE)
    except Exception as exc:
        if log is not None:
            log.append(f"[Maintenance] WARN: could not save latest report: {exc}")


def _maintenance_same_file_hash(left: Path, right: Path) -> bool:
    return _duplicate_identity.same_file_hash(left, right)


def _maintenance_remove_missing_file_rows(health: Dict[str, Any], log: List[str]) -> Dict[str, Any]:
    missing = int(health.get("orphaned_item_count") or 0)
    ids = [int(i) for i in (health.get("orphaned_item_ids") or []) if str(i).isdigit() and int(i) > 0]
    total_items = int(health.get("item_row_count") or (health.get("final_summary") or {}).get("tracks_count") or 0)
    result = {
        "ok": True,
        "missing_files": missing,
        "orphaned_item_count": missing,
        "removed_db_rows": 0,
        "empty_albums_removed": 0,
        "skipped": 0,
        "final_summary": {"missing_files": missing, "removed_db_rows": 0},
    }
    if missing <= 0:
        return result
    if not ids:
        result["skipped"] = missing
        result["skip_reason"] = "missing item IDs were not available"
        result["final_summary"]["skipped"] = missing
        log.append("[Missing Files Scan] skipped DB removal: missing item IDs were not available.")
        return result
    if not MUSIC_ROOT.exists():
        result["skipped"] = missing
        result["skip_reason"] = "music root is not accessible"
        result["final_summary"]["skipped"] = missing
        log.append(f"[Missing Files Scan] skipped DB removal: music root is not accessible: {MUSIC_ROOT}")
        return result
    if total_items > 0 and (missing / max(total_items, 1)) >= 0.5:
        result["skipped"] = missing
        result["skip_reason"] = "too many DB rows appear missing; possible mount issue"
        result["final_summary"]["skipped"] = missing
        log.append(
            "[Missing Files Scan] skipped DB removal: "
            f"{missing}/{total_items} item row(s) appear missing; possible mount issue."
        )
        return result

    cleanup = _clean_remove_orphaned_items(ids, dry_run=False, log=log, trigger_plex=False)
    removed = int(cleanup.get("removed") or 0)
    empty_albums_removed = int(cleanup.get("empty_albums_removed") or 0)
    skipped = int(cleanup.get("skipped") or 0)
    result.update({
        "cleanup": cleanup,
        "removed_db_rows": removed,
        "empty_albums_removed": empty_albums_removed,
        "skipped": skipped,
        "final_summary": {
            "missing_files": missing,
            "removed_db_rows": removed,
            "empty_albums_removed": empty_albums_removed,
            "skipped": skipped,
        },
    })
    return result


def _maintenance_release_group_merge(log: List[str], cancel_event: Optional[Any] = None,
                                     progress: Optional[Any] = None) -> Dict[str, Any]:
    running = _running_job_of_type({
        "album-folder-cleanup-scan",
        "album-folder-cleanup-apply-safe",
        "album-folder-cleanup-apply-issue",
    })
    if running:
        result = {
            "ok": True,
            "skipped": True,
            "reason": "Album folder cleanup already running",
            "final_summary": {"skipped": 1},
        }
        log.append("[release-group-merge] skipped: album folder cleanup already running.")
        return result
    if not _ALBUM_FOLDER_CLEANUP_LOCK.acquire(blocking=False):
        result = {
            "ok": True,
            "skipped": True,
            "reason": "Album folder cleanup lock is busy",
            "final_summary": {"skipped": 1},
        }
        log.append("[release-group-merge] skipped: album folder cleanup lock is busy.")
        return result
    try:
        log.append("[release-group-merge] Scanning Release Group duplicate album folders")
        report = _album_folder_cleanup_apply_safe(
            MUSIC_ROOT,
            log,
            cancel_event=cancel_event,
            progress=progress,
            verbose_files=False,
        )
        summary = report.get("summary") or {}
        log.append(
            "[release-group-merge] Done: "
            f"{summary.get('safe_issues_selected', 0)} group(s) processed in {summary.get('batches', 0)} batch(es), "
            f"{summary.get('files_moved', 0)} file(s) moved, "
            f"{summary.get('duplicate_files_quarantined', 0)} duplicate(s) quarantined, "
            f"{summary.get('folders_deleted', 0)} folder(s) removed, "
            f"{summary.get('errors', 0)} error(s); "
            f"{summary.get('needs_review_remaining', 0)} group(s) still need manual review."
        )
        _album_cleanup_save_report(report, log)
        _maintenance_save_last_report({"release_group_merge": report}, log)
        return report
    finally:
        _ALBUM_FOLDER_CLEANUP_LOCK.release()


def _maintenance_root_folder_repair(log: List[str], cancel_event: Optional[Any] = None,
                                    progress: Optional[Any] = None) -> Dict[str, Any]:
    running = _root_folder_repair_running_job()
    if running:
        result = {
            "ok": True,
            "skipped": True,
            "reason": "Root folder repair already running",
            "final_summary": {"skipped": 1},
        }
        log.append("[root-folder-repair] skipped: already running.")
        return result
    if not _ROOT_FOLDER_REPAIR_LOCK.acquire(blocking=False):
        result = {
            "ok": True,
            "skipped": True,
            "reason": "Root folder repair lock is busy",
            "final_summary": {"skipped": 1},
        }
        log.append("[root-folder-repair] skipped: lock is busy.")
        return result
    try:
        log.append("[root-folder-repair] Scanning for misplaced root-level album/singleton folders")
        report = _root_folder_repair_apply_safe(log, cancel_event=cancel_event, progress=progress)
        summary = report.get("summary") or {}
        log.append(
            "[root-folder-repair] Done: "
            f"{summary.get('empty_folders_removed', 0)} empty folder(s) removed, "
            f"{summary.get('items_moved', 0)} item(s) re-homed, "
            f"{summary.get('items_failed', 0)} item(s) failed, "
            f"{summary.get('orphaned_queued', 0)} folder(s) queued for review."
        )
        _root_folder_repair_save_report(report, log)
        _maintenance_save_last_report({"root_folder_repair": report}, log)
        return report
    finally:
        _ROOT_FOLDER_REPAIR_LOCK.release()


def _maintenance_artwork_collision_leftovers(root: Path, limit: int = 10000) -> Dict[str, Any]:
    pattern = re.compile(r"(?i)^(?:albumart|cover|folder)\.\d+\.(?:jpe?g|png|webp|gif|bmp)$")
    count = 0
    examples: List[str] = []
    checked = 0
    try:
        iterator = root.rglob("*")
    except Exception:
        iterator = iter(())
    for path in iterator:
        checked += 1
        if checked > limit:
            break
        try:
            if not path.is_file() or not pattern.match(path.name):
                continue
        except Exception:
            continue
        count += 1
        if len(examples) < 20:
            examples.append(str(path))
    return {"count": count, "examples": examples, "truncated": checked > limit}


def _maintenance_duplicate_recording_mbid_groups() -> int:
    try:
        res = composite_workflows.scan_library_integrity()
        return int(res.get("duplicate_recording_mbids_count") or res.get("duplicate_recording_groups") or 0)
    except Exception:
        return 0


def _maintenance_final_verification(log: List[str], cancel_event: Optional[Any] = None,
                                    progress: Optional[Any] = None) -> Dict[str, Any]:
    if cancel_event and cancel_event.is_set():
        raise RuntimeError("cancelled")
    health = _library_health_payload(progress=progress)
    if cancel_event and cancel_event.is_set():
        raise RuntimeError("cancelled")
    artist_scan = _stamp_artist_folder_scan(MUSIC_ROOT)
    placeholder_meta: Dict[str, Any] = {}
    placeholders = _scan_folder_name_placeholders(cancel_event=cancel_event, scan_meta=placeholder_meta)
    artwork_leftovers = _maintenance_artwork_collision_leftovers(MUSIC_ROOT)
    duplicate_recording_groups = _maintenance_duplicate_recording_mbid_groups()

    final_summary = {
        "duplicate_artist_mbid_groups_remaining": len(artist_scan.get("candidates") or []),
        "duplicate_release_group_id_groups_remaining": int(health.get("rgid_duplicate_group_count") or 0),
        "duplicate_recording_mbid_groups_remaining": duplicate_recording_groups,
        "duplicate_album_groups_remaining": int(health.get("duplicate_album_count") or 0),
        "broken_db_paths": int(health.get("orphaned_item_count") or 0),
        "missing_file_db_rows": int(health.get("orphaned_item_count") or 0),
        "placeholder_identity_folders_remaining": len(placeholders),
        "artwork_collision_leftovers_remaining": int(artwork_leftovers.get("count") or 0),
        "empty_stale_folders_remaining": int(health.get("empty_album_count") or 0),
        "empty_album_rows": int(health.get("empty_album_count") or 0),
        "submission_queue_items": 0,
        "manual_review_items": (
            len(artist_scan.get("candidates") or [])
            + int(health.get("rgid_duplicate_group_count") or 0)
            + duplicate_recording_groups
            + int(health.get("orphaned_item_count") or 0)
            + len(placeholders)
            + int(artwork_leftovers.get("count") or 0)
            + int(health.get("empty_album_count") or 0)
        ),
    }
    log.append("[verification] Duplicate Artist MBID groups remaining: "
               f"{final_summary['duplicate_artist_mbid_groups_remaining']}")
    log.append("[verification] Duplicate Release Group ID groups remaining: "
               f"{final_summary['duplicate_release_group_id_groups_remaining']}")
    log.append("[verification] Duplicate Recording MBID groups remaining: "
               f"{final_summary['duplicate_recording_mbid_groups_remaining']}")
    log.append("[verification] Broken DB paths: "
               f"{final_summary['broken_db_paths']}")
    log.append("[verification] Placeholder identity folders remaining: "
               f"{final_summary['placeholder_identity_folders_remaining']}")
    log.append("[verification] Artwork collision leftovers remaining: "
               f"{final_summary['artwork_collision_leftovers_remaining']}")
    result = {
        "ok": True,
        "health": health,
        "artist_folder_candidates": artist_scan.get("candidates") or [],
        "placeholder_folders": placeholders[:50],
        "artwork_collision_leftovers": artwork_leftovers,
        "final_summary": final_summary,
    }
    _maintenance_save_last_report({"final_verification": result}, log)
    return result


_ALBUM_FOLDER_CLEANUP_LOCK = threading.Lock()


_ROOT_FOLDER_REPAIR_LOCK = threading.Lock()


_AUTO_SCAN_INTERVAL  = _FULL_SCAN_INTERVAL   # kept for API compat
