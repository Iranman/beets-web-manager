"""Maintenance runner and transaction routes (ARCH-001): HTTP handlers over the maintenance/transaction services.
"""

from __future__ import annotations

import functools, time
from typing import Any, Dict, Optional
from flask import Response, jsonify, request
from backend.beets_adapter import BeetsError, BeetsUnavailableError
import backend.composite_workflows as composite_workflows
import backend.duplicate_cleanup as duplicate_cleanup
import backend.album_row_merge as album_row_merge
import backend.untracked_recovery_service as untracked_recovery
from backend.app_runtime import MAINTENANCE_RUNNER_LAST_FILE, MUSIC_ROOT, _app_logger, _s, jobs, registered_flask_app, transactions
from backend.dedup_service import _maintenance_full_duplicate_scan
import backend.job_contract as job_contract
from backend.job_service import _running_job_of_type, _wait_for_child_job
from backend.library_service import _artist_id_alias_groups, _folder_placeholder_summary, _run_item_metadata_restore, _run_item_recording_id_restore, _scan_folder_name_placeholders, start_fetch_missing_art, start_library_fix_genres
from backend.maintenance_service import _library_health_payload, _maintenance_artist_folder_merge_step, _maintenance_clean_all_counts, _maintenance_extract_child_job_id, _maintenance_final_verification, _maintenance_initial_task_state, _maintenance_load_last_report, _maintenance_release_group_merge, _maintenance_remove_missing_file_rows, _maintenance_resume_from_report, _maintenance_resume_summary, _maintenance_root_folder_repair, _maintenance_running_job, _maintenance_save_last_report, _maintenance_task_result_summary
from backend.transaction_service import _start_metadata_apply_transaction, _sync_transactions_from_jobs
from backend.cleanup_service import album_cleanup_apply_response, controlled_apply_error, controlled_rollback_error
from backend.playlist_service import playlist_sync_status_payload
from app import app  # noqa: E402  (route modules load after app.py defines app)

# ── ARCH-001 extracted code ──


@app.post("/api/restart")
def restart_app():
    """Restart the app process by re-executing the current command."""
    import threading, os, sys
    def _restart():
        import time
        time.sleep(0.3)
        argv = list(sys.argv or [])
        if argv:
            try:
                if argv[0].endswith(".py"):
                    os.execv(sys.executable, [sys.executable] + argv)
                else:
                    os.execvp(argv[0], argv)
            except Exception:
                pass
        os._exit(1)
    threading.Thread(target=_restart, daemon=True).start()
    return jsonify({"ok": True, "message": "Restarting…"})


CLEAN_ALL_PIPELINE_STEPS = [
    "Scanning",
    "Fingerprinting",
    "Matching",
    "Verifying",
    "Repairing",
    "Replacing",
    "Organizing",
    "Syncing",
]


CLEAN_ALL_TASK_PHASES = {
    "duplicates": "Fingerprinting",
    "genres": "Repairing",
    "artwork": "Repairing",
    "folder_scan": "Scanning",
    "folder_safe_renames": "Organizing",
    "library_health": "Verifying",
    "missing_files": "Verifying",
    "root_folder_repair": "Organizing",
    "artist_alias": "Matching",
    "artist_folder_merge": "Organizing",
    "release_group_merge": "Organizing",
    "final_verification": "Verifying",
    "stale_jobs": "Organizing",
    "playlist_refs": "Syncing",
}


@app.get("/api/jobs/maintenance-runner/report")
def maintenance_runner_report():
    try:
        exists = MAINTENANCE_RUNNER_LAST_FILE.exists()
        report = _maintenance_load_last_report() if exists else {}
    except Exception as exc:
        _app_logger.warning("Could not read maintenance report: %s", type(exc).__name__)
        return jsonify({"ok": False, "error": "Could not read maintenance report."}), 500
    last_run = report.get("last_run") if isinstance(report.get("last_run"), dict) else None
    if last_run is not None and _s(last_run.get("status")).strip().lower() == "running" and not _maintenance_running_job():
        # jobs.JobStore is in-memory only, so a "running" checkpoint left
        # behind by an app restart mid-run has no live job backing it
        # anymore -- same stale-state class already handled for playlist
        # download checkpoints (_playlist_job_is_live). Relabel here rather
        # than leaving the UI showing "running" forever for a job that
        # cannot possibly still be progressing.
        last_run["status"] = "interrupted"
        report["last_run"] = last_run
    resume = _maintenance_resume_summary(report)
    summary_only = _s(request.args.get("summary")).strip().lower() in {"1", "true", "yes"}
    report_payload = {} if summary_only else report
    return jsonify({
        "ok": True,
        "exists": exists,
        "report": report_payload,
        "resumable": bool(resume.get("resumable")),
        "resume": resume,
    })


def _run_with_app_context(fn):
    @functools.wraps(fn)
    def _wrapped(log, cancel_event=None, update_state=None):
        with registered_flask_app().app_context():
            return fn(log, cancel_event, update_state)
    return _wrapped


@app.post("/api/jobs/maintenance-runner")
def start_maintenance_runner():
    """Start the Clean All parent job using the existing maintenance framework.

    This entry point coordinates the current safe cleanup/report steps and emits
    Clean All progress fields without creating a second cleanup job system.
    """
    running = _maintenance_running_job()
    if running:
        return jsonify({"ok": True, "job_id": running.job_id, "already_running": True})

    # A checkpoint can be resumable but not worth resuming -- e.g. left
    # behind by an app restart mid-run (JobStore is in-memory only and
    # doesn't survive one) with a task already marked "skipped" that will
    # never be retried by resuming. Only a real fresh run re-attempts it,
    # so the UI needs an explicit way to bypass resume instead of always
    # being forced into it whenever a checkpoint happens to exist.
    force_fresh = bool((request.get_json(silent=True) or {}).get("force_fresh"))
    resume_snapshot = {} if force_fresh else _maintenance_resume_from_report(_maintenance_load_last_report())
    resume_requested = bool(resume_snapshot.get("resumable"))

    def _run(
        log,
        cancel_event=None,
        update_state=None,
        resume_snapshot=resume_snapshot,
        resume_requested=resume_requested,
    ):
        tasks = [dict(task) for task in (resume_snapshot.get("tasks") or _maintenance_initial_task_state())]
        task_index = {task["id"]: idx for idx, task in enumerate(tasks)}
        raw_results = resume_snapshot.get("results") if resume_requested else {}
        results: Dict[str, Any] = dict(raw_results) if isinstance(raw_results, dict) else {}
        total = len(tasks)
        run_started_at = time.time()
        def emit(current_id: str = "", message: str = ""):
            completed = sum(1 for task in tasks if task["status"] in {"complete", "failed", "skipped"})
            current = next((task for task in tasks if task["id"] == current_id), None)
            current_task = current["label"] if current else (message or "Clean All")
            current_phase = CLEAN_ALL_TASK_PHASES.get(current_id, "Scanning" if not completed else "Syncing")
            payload = {
                "category": "Cleanup",
                "workflow": "clean-all",
                "current_task": current_task,
                "current_result": message,
                "current_phase": current_phase,
                "last_completed_action": message,
                "last_heartbeat_at": time.time(),
                "maintenance_status": "running",
                "maintenance_tasks": tasks,
                "clean_all_pipeline": CLEAN_ALL_PIPELINE_STEPS,
                "clean_all_counts": _maintenance_clean_all_counts(tasks, results),
                "progress_percent": round((completed / total) * 100),
                "scanned_count": completed,
                "total_count": total,
                "remaining_count": max(0, total - completed),
            }
            if update_state:
                update_state(payload)

        def next_resume_task() -> Optional[Dict[str, Any]]:
            return next((task for task in tasks if task.get("status") not in {"complete", "skipped"}), None)

        def persist_checkpoint(status: str = "running", error: str = ""):
            next_task = next_resume_task()
            last_run = {
                "status": status,
                "workflow": "clean-all",
                "started_at": run_started_at,
                "updated_at": time.time(),
                "resumed": resume_requested,
                "completed_task_ids": [task.get("id") for task in tasks if task.get("status") in {"complete", "skipped"}],
                "next_task": next_task.get("id") if next_task else "",
                "next_task_label": next_task.get("label") if next_task else "",
                "tasks": tasks,
                "result_task_ids": sorted(str(key) for key in results.keys()),
            }
            if status == "complete":
                last_run["completed_at"] = time.time()
            if status in {"failed", "cancelled", "partial"}:
                last_run["failed_at"] = time.time()
            if error:
                last_run["error"] = error
            _maintenance_save_last_report({"last_run": last_run}, log)
            if update_state:
                # The resumable position, also in the durable job record.
                update_state({"checkpoint": {
                    "workflow": "maintenance-runner", "stage": status,
                    "completed_task_ids": last_run["completed_task_ids"], "next_task": last_run["next_task"],
                    "at": last_run["updated_at"]}})

        def task_is_complete(task_id: str) -> bool:
            task = tasks[task_index[task_id]]
            return task.get("status") in {"complete", "skipped"}

        def skip_completed_task(task_id: str) -> bool:
            if not task_is_complete(task_id):
                return False
            task = tasks[task_index[task_id]]
            message = f"Resuming from checkpoint; {task['label']} already {task['status']}"
            log.append(f"[Clean All Resume] {message}.")
            emit(task_id, message)
            return True
        def set_task(task_id: str, status: str, detail: str = "", result: Any = None):
            idx = task_index[task_id]
            tasks[idx] = {**tasks[idx], "status": status}
            if detail:
                tasks[idx]["detail"] = detail
            if result is not None:
                summary = _maintenance_task_result_summary(result)
                if summary:
                    tasks[idx]["detail"] = summary
                results[task_id] = result
                if status in {"complete", "skipped"}:
                    _maintenance_save_last_report({task_id: result}, log)
            emit(task_id, detail)
            persist_checkpoint('running')

        def ensure_not_cancelled():
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("cancelled")

        def finish_partial(failed_message: str) -> Dict[str, Any]:
            clean_counts = _maintenance_clean_all_counts(tasks, results)
            progress_percent = round(
                (sum(1 for task in tasks if task["status"] in {"complete", "failed", "skipped"}) / total) * 100
            )
            next_task = next_resume_task()
            if update_state:
                update_state({
                    "category": "Cleanup",
                    "workflow": "clean-all",
                    "maintenance_status": "partial",
                    "current_task": next_task["label"] if next_task else "Clean All paused",
                    "current_result": failed_message,
                    "current_phase": CLEAN_ALL_TASK_PHASES.get((next_task or {}).get("id", ""), "Verifying"),
                    "last_completed_action": failed_message,
                    "last_heartbeat_at": time.time(),
                    "error_summary": failed_message,
                    "maintenance_tasks": tasks,
                    "clean_all_pipeline": CLEAN_ALL_PIPELINE_STEPS,
                    "clean_all_counts": clean_counts,
                    "progress_percent": progress_percent,
                    "final_summary": {
                        "status": "partial",
                        "tasks_complete": sum(1 for task in tasks if task["status"] == "complete"),
                        "tasks_skipped": sum(1 for task in tasks if task["status"] == "skipped"),
                        "tasks_failed": sum(1 for task in tasks if task["status"] == "failed"),
                        "clean_all_counts": clean_counts,
                    },
                })
            persist_checkpoint("partial", failed_message)
            log.append(f"Clean All partially completed: {failed_message}")
            return {"ok": False, "partial": True, "status": "partial", "error": failed_message, "tasks": tasks, "results": results}

        log.append("Clean All started.")
        if resume_requested:
            log.append(
                "[Clean All Resume] Resuming from checkpoint: "
                f"{resume_snapshot.get('completed_count', 0)} completed task(s); "
                f"next: {resume_snapshot.get('next_task_label') or 'next pending task'}."
            )
        emit("", "Resuming Clean All" if resume_requested else "Starting Clean All")
        persist_checkpoint("running")
        try:
            # 1. Library DB health establishes current duplicate and path state.
            ensure_not_cancelled()
            if skip_completed_task("library_health"):
                health = results.get("library_health") if isinstance(results.get("library_health"), dict) else {}
            else:
                set_task("library_health", "running", "Running DB health check")
                health = _library_health_payload(progress=lambda updates: emit("library_health", _s(updates.get("current_result") or "")))
                log.append(
                    "[Library DB Health] "
                    f"{health.get('duplicate_album_count', 0)} duplicate album group(s), "
                    f"{health.get('rgid_duplicate_group_count', 0)} same Release Group ID group(s), "
                    f"{health.get('orphaned_item_count', 0)} missing file row(s), "
                    f"{health.get('empty_album_count', 0)} empty album row(s)."
                )
                set_task("library_health", "complete", "DB health report refreshed", health)


            # 2. Missing files are reported before mutation; DB rows are not blindly removed.
            ensure_not_cancelled()
            if skip_completed_task("missing_files"):
                missing_summary = results.get("missing_files") if isinstance(results.get("missing_files"), dict) else {}
                if int(missing_summary.get("removed_db_rows") or 0) > 0:
                    health = _library_health_payload(progress=lambda updates: emit("library_health", _s(updates.get("current_result") or "")))
            else:
                set_task("missing_files", "running", "Reconciling missing file DB rows")
                missing_summary = _maintenance_remove_missing_file_rows(health, log)
                if int(missing_summary.get("removed_db_rows") or 0) > 0:
                    health = _library_health_payload(progress=lambda updates: emit("library_health", _s(updates.get("current_result") or "")))
                skip_reason = _s(missing_summary.get("skip_reason") or "")
                log.append(
                    f"[Missing Files Scan] {missing_summary['missing_files']} missing file row(s); "
                    f"removed {missing_summary['removed_db_rows']} DB row(s)."
                    + (f" skipped: {skip_reason}." if skip_reason else "")
                )
                set_task("missing_files", "complete", "Missing files reconciled", missing_summary)

            # 3. Re-home root-level album/singleton folders missing their
            #    artist-folder wrapper before anything downstream assumes a
            #    clean two-level artist/album layout.
            ensure_not_cancelled()
            if skip_completed_task("root_folder_repair"):
                pass
            else:
                set_task("root_folder_repair", "running", "Repairing misplaced root-level folders")
                root_repair_result = _maintenance_root_folder_repair(
                    log,
                    cancel_event=cancel_event,
                    progress=lambda updates: emit("root_folder_repair", _s(updates.get("current_result") or "")),
                )
                set_task(
                    "root_folder_repair",
                    "complete" if not root_repair_result.get("skipped") else "skipped",
                    "Root folder repair complete",
                    root_repair_result,
                )

            # 4. Resolve artist identity variants before album-level moves.
            ensure_not_cancelled()
            if skip_completed_task("artist_alias"):
                pass
            else:
                set_task("artist_alias", "running", "Checking MusicBrainz artist alias groups")
                try:
                    alias_groups = _artist_id_alias_groups()
                    alias_result = {
                        "ok": True,
                        "count": len(alias_groups),
                        "groups": alias_groups[:50],
                        "truncated": len(alias_groups) > 50,
                        "final_summary": {"count": len(alias_groups)},
                    }
                    log.append(f"[Artist Alias Check] {len(alias_groups)} same-MusicBrainz artist alias group(s).")
                    _maintenance_save_last_report({"artist_alias": alias_result}, log)
                    set_task("artist_alias", "complete", "Artist alias report refreshed", alias_result)
                except Exception as ex:
                    alias_result = {"ok": False, "count": 0, "error": str(ex)}
                    log.append(f"[Artist Alias Check] skipped: {ex}")
                    set_task("artist_alias", "skipped", "Artist alias check skipped", alias_result)


            ensure_not_cancelled()
            if skip_completed_task("artist_folder_merge"):
                pass
            else:
                resume_op_id = _s(tasks[task_index["artist_folder_merge"]].get("operation_id")).strip()
                set_task("artist_folder_merge", "running", "Merging safe MusicBrainz artist folder variants")
                if not resume_op_id and _running_job_of_type({"artist-folder-merge", "stamp-mbid-folders"}):
                    set_task("artist_folder_merge", "skipped", "Artist folder merge already running")
                else:
                    if resume_op_id:
                        log.append(
                            f"[Clean All Resume] Artist folder merge: found a saved engine operation "
                            f"({resume_op_id}) from before this run was interrupted; checking its status "
                            f"before creating any new plan."
                        )

                    def _persist_artist_folder_merge_operation_id(op_id: str) -> None:
                        idx = task_index["artist_folder_merge"]
                        tasks[idx] = {**tasks[idx], "operation_id": op_id}
                        persist_checkpoint("running")

                    result = _maintenance_artist_folder_merge_step(
                        log, cancel_event, str(MUSIC_ROOT),
                        resume_operation_id=resume_op_id,
                        on_operation_planned=_persist_artist_folder_merge_operation_id,
                    )
                    renamed = int((result or {}).get("renamed") or 0) if isinstance(result, dict) else 0
                    merged = int((result or {}).get("merged") or 0) if isinstance(result, dict) else 0
                    _maintenance_save_last_report({"artist_folder_merge": result}, log)
                    if (result or {}).get("ok", True):
                        tasks[task_index["artist_folder_merge"]].pop("operation_id", None)
                        set_task(
                            "artist_folder_merge", "complete",
                            f"Artist folder merge complete: {renamed} renamed, {merged} merged", result,
                        )
                    elif (result or {}).get("still_running"):
                        # The engine operation's outcome is still genuinely
                        # unresolved (Apply's response was lost and the poll
                        # deadline was reached while status was still
                        # "Running") -- this is not a failure of the merge
                        # itself, just of Web Manager's ability to keep
                        # watching it in this run. Raise into the existing
                        # partial-run handling below rather than marking it
                        # "failed": the task stays "running" with its
                        # operation_id intact (see the except block's
                        # special-case guard for this task id), so a later
                        # resume checks that same operation's authoritative
                        # status instead of discarding it and creating a
                        # duplicate one.
                        raise RuntimeError(
                            f"Artist folder merge: engine operation "
                            f"{(result or {}).get('operation_id', '')} is still in progress; will be "
                            f"reconciled on the next run."
                        )
                    else:
                        tasks[task_index["artist_folder_merge"]].pop("operation_id", None)
                        set_task(
                            "artist_folder_merge", "failed",
                            f"Artist folder merge failed: {(result or {}).get('error', '')}", result,
                        )

            # 5. Release Group ID drives album-folder consolidation.
            ensure_not_cancelled()
            if skip_completed_task("release_group_merge"):
                pass
            else:
                set_task("release_group_merge", "running", "Merging duplicate Release Group album folders")
                result = _maintenance_release_group_merge(
                    log,
                    cancel_event=cancel_event,
                    progress=lambda updates: emit("release_group_merge", _s(updates.get("current_result") or "")),
                )
                set_task("release_group_merge", "complete" if not result.get("skipped") else "skipped", "Release Group merge complete", result)

            # 6. Full duplicate scan and verified duplicate cleanup.
            ensure_not_cancelled()
            if skip_completed_task("duplicates"):
                pass
            else:
                set_task("duplicates", "running", "Running full duplicate track scan")
                result = _maintenance_full_duplicate_scan(
                    log,
                    cancel_event=cancel_event,
                    progress=lambda updates: emit("duplicates", _s(updates.get("current_result") or "")),
                )
                set_task("duplicates", "complete" if not result.get("skipped") else "skipped", "Duplicate track scan complete", result)

            # 7. Placeholder scan stays diagnostic after RGID repair; no blind rename.
            ensure_not_cancelled()
            set_task("folder_scan", "running", "Scanning remaining folder placeholder names")
            scan_meta: Dict[str, Any] = {}
            rows = _scan_folder_name_placeholders(
                progress=lambda updates: emit("folder_scan", _s(updates.get("current_result") or "")),
                cancel_event=cancel_event,
                scan_meta=scan_meta,
            )
            folder_scan_result = {
                "total": len(rows),
                "safe_rename": 0,
                "skipped_unsafe": len(rows),
                "final_summary": _folder_placeholder_summary(
                    rows,
                    total_scanned=scan_meta.get("total_folders_scanned"),
                ),
            }
            log.append(
                f"[Folder Name Scan] {len(rows)} placeholder folder(s) remain after identity repair; "
                "unresolved folders were left for review/submission instead of blind rename."
            )
            set_task("folder_scan", "complete", "Folder placeholder scan complete", folder_scan_result)

            ensure_not_cancelled()
            set_task("folder_safe_renames", "skipped", "Placeholder-only renames disabled; Release Group merge handles resolvable IDs", {
                "renamed": 0,
                "skipped_unsafe": len(rows),
                "final_summary": {"renamed": 0, "skipped_unsafe": len(rows)},
            })

            # 8. Artwork and genres run after filesystem consolidation.
            ensure_not_cancelled()
            if skip_completed_task("artwork"):
                pass
            else:
                if _running_job_of_type({"fetch-missing-art", "album-art-rebuild"}):
                    set_task("artwork", "skipped", "Artwork job already running")
                else:
                    set_task("artwork", "running", "Fetching missing artwork only")
                    child_id = _maintenance_extract_child_job_id(start_fetch_missing_art({}))
                    result = _wait_for_child_job(child_id, log, cancel_event, prefix="artwork", timeout=3600)
                    set_task("artwork", "complete", "Missing artwork fetch complete", result)

            ensure_not_cancelled()
            if skip_completed_task("genres"):
                pass
            else:
                if _running_job_of_type({"fix-genres"}):
                    set_task("genres", "skipped", "Genre tagging already running")
                else:
                    set_task("genres", "running", "Tagging missing genres only")
                    child_id = _maintenance_extract_child_job_id(start_library_fix_genres({"force": False, "use_ai": False}))
                    result = _wait_for_child_job(child_id, log, cancel_event, prefix="genres", timeout=3600)
                    set_task("genres", "complete", "Missing genre tagging complete", result)

            # 9. Final verification reports remaining identity and path issues.
            ensure_not_cancelled()
            if skip_completed_task("final_verification"):
                pass
            else:
                set_task("final_verification", "running", "Verifying final cleanup state")
                result = _maintenance_final_verification(
                    log,
                    cancel_event=cancel_event,
                    progress=lambda updates: emit("final_verification", _s(updates.get("current_result") or "")),
                )
                set_task("final_verification", "complete", "Final verification complete", result)

            ensure_not_cancelled()
            set_task("stale_jobs", "running", "Pruning stale completed job history")
            jobs.prune_finished(max_age_seconds=21600, metadata_max_age_seconds=604800, max_finished=250)
            stale_result = {"pruned": True, "removed_running_jobs": 0}
            log.append("[Stale Job Cleanup] pruned old finished jobs within retention limits.")
            set_task("stale_jobs", "complete", "Old successful job history pruned", stale_result)

            ensure_not_cancelled()
            set_task("playlist_refs", "running", "Checking playlist references")
            playlist_report = playlist_sync_status_payload()
            log.append(
                "[Playlist Reference Check] "
                f"enabled={bool(playlist_report.get('enabled'))}, "
                f"running={bool(playlist_report.get('running'))}."
            )
            set_task("playlist_refs", "complete", "Playlist reference status checked", playlist_report)

            if update_state:
                clean_counts = _maintenance_clean_all_counts(tasks, results)
                update_state({
                    "category": "Cleanup",
                    "workflow": "clean-all",
                    "maintenance_status": "complete",
                    "current_task": "Clean All complete",
                    "current_result": "All current cleanup steps finished",
                    "current_phase": "Syncing",
                    "last_completed_action": "All current cleanup steps finished",
                    "last_heartbeat_at": time.time(),
                    "maintenance_tasks": tasks,
                    "clean_all_pipeline": CLEAN_ALL_PIPELINE_STEPS,
                    "clean_all_counts": clean_counts,
                    "progress_percent": 100,
                    "scanned_count": total,
                    "total_count": total,
                    "remaining_count": 0,
                    "final_summary": {
                        "tasks_complete": sum(1 for task in tasks if task["status"] == "complete"),
                        "tasks_skipped": sum(1 for task in tasks if task["status"] == "skipped"),
                        "tasks_failed": sum(1 for task in tasks if task["status"] == "failed"),
                        "clean_all_counts": clean_counts,
                    },
                })
            persist_checkpoint("complete")
            log.append("Clean All complete.")
            return {"ok": True, "tasks": tasks, "results": results}
        except Exception as exc:
            failed_message = _s(exc) or "maintenance failed"
            completed_before_failure = sum(1 for task in tasks if task["status"] in {"complete", "skipped"})
            running_task = next((task for task in tasks if task["status"] == "running"), None)
            # Clean All resume reattachment: a running_task carrying a saved
            # artist-folder-merge operation_id is not a crash -- it is a
            # real engine operation whose outcome is still genuinely
            # unresolved (see the "still_running" raise above). Forcing it
            # to "failed" here would make the next resume treat a
            # possibly-still-active or already-completed engine operation
            # as conclusively dead and start a duplicate one; leave it
            # "running" (with operation_id intact) so resume checks its
            # authoritative transaction status first.
            if running_task and not (running_task.get("id") == "artist_folder_merge" and running_task.get("operation_id")):
                set_task(running_task["id"], "failed", failed_message)
            if completed_before_failure > 0:
                return finish_partial(failed_message)
            if update_state:
                update_state({
                    "category": "Cleanup",
                    "workflow": "clean-all",
                    "maintenance_status": "failed",
                    "current_task": running_task["label"] if running_task else "Clean All failed",
                    "current_result": failed_message,
                    "current_phase": CLEAN_ALL_TASK_PHASES.get((running_task or {}).get("id", ""), "Verifying"),
                    "last_completed_action": failed_message,
                    "last_heartbeat_at": time.time(),
                    "error_summary": failed_message,
                    "maintenance_tasks": tasks,
                    "clean_all_pipeline": CLEAN_ALL_PIPELINE_STEPS,
                    "clean_all_counts": _maintenance_clean_all_counts(tasks, results),
                    "progress_percent": round(
                        (sum(1 for task in tasks if task["status"] in {"complete", "failed", "skipped"}) / total) * 100
                    ),
                })
            persist_checkpoint("failed", failed_message)
            log.append(f"Clean All failed: {failed_message}")
            raise
    job = jobs.start_python(
        job_contract.guarded(_run_with_app_context(_run), workflow="maintenance-runner"),
        label="Clean All",
        metadata={"type": "maintenance-runner", "workflow": "clean-all", "category": "Cleanup", "resumed": resume_requested,
                  **job_contract.contract_metadata("maintenance-runner")},
    )
    resume_payload = _maintenance_resume_summary({
        "last_run": {
            "status": "failed",
            "tasks": resume_snapshot.get("tasks") or [],
            "results": resume_snapshot.get("results") or {},
        }
    }) if resume_requested else {"resumable": False}
    return jsonify({
        "ok": True,
        "job_id": job.job_id,
        "resumed": resume_requested,
        "resume": resume_payload,
    })


@app.get("/api/transactions/settings")
def api_transaction_settings():
    return jsonify({"ok": True, "settings": transactions.settings()})


@app.post("/api/transactions/settings")
def api_transaction_settings_save():
    payload = request.get_json(silent=True) or {}
    try:
        settings = transactions.save_settings(payload)
    except Exception as ex:
        _app_logger.warning("Could not save transaction settings: %s", type(ex).__name__)
        return jsonify({"ok": False, "error": "Could not save transaction settings."}), 400
    return jsonify({"ok": True, "settings": settings})


@app.get("/api/transactions")
def api_transactions_list():
    # SEC-002 Wave 16 final review: this previously fell back to
    # composite_workflows.list_transactions() (browsing the ENTIRE engine
    # TransactionStore, across every mutation family and every
    # operation ever run, not just this session's own) whenever the local
    # list was empty. Nothing in the frontend calls this fallback --
    # AlbumCleanupModal only ever reads the transaction embedded directly
    # in its own Plan/Apply responses -- so it was unused, unjustified
    # attack surface (unbounded server-side path/metadata exposure for no
    # product benefit). Kept local-only until a real feature needs it.
    _sync_transactions_from_jobs()
    rows, total = transactions.list(
        offset=_transaction_int_arg("offset", 0, 0, 1_000_000),
        limit=_transaction_int_arg("limit", 50, 1, 500),
        status=str(request.args.get("status") or ""),
        operation=str(request.args.get("operation") or ""),
        query=str(request.args.get("q") or ""),
        job=str(request.args.get("job") or ""),
    )
    return jsonify({"ok": True, "transactions": rows, "total": total})


@app.get("/api/transactions/<transaction_id>")
def api_transaction_detail(transaction_id):
    # SEC-002 Wave 16 final review: same rationale as api_transactions_list
    # above -- the engine get_transaction() fallback here was unused dead
    # surface (AlbumCleanupModal reads plan.transaction /
    # applyResult directly from the Plan/Apply responses, never a separate
    # GET). Kept local-only; add a properly scoped fallback if a real
    # feature needs to poll a specific known engine transaction by ID.
    _sync_transactions_from_jobs()
    try:
        tx = transactions.get(
            transaction_id,
            offset=_transaction_int_arg("offset", 0, 0, 1_000_000),
            limit=_transaction_int_arg("limit", 100, 1, 1000),
        )
        return jsonify({"ok": True, "transaction": tx})
    except KeyError:
        return jsonify({"ok": False, "error": "Transaction not found"}), 404


@app.post("/api/transactions/<transaction_id>/approve")
def api_transaction_approve(transaction_id):
    """Approve a Preview transaction (compare-and-set, LT-16). Any other
    status -- Completed, Failed, Rolled Back, Cancelled, Running, Recovery
    Required, or already Approved -- is refused, so a finished or failed
    mutation can never be re-opened and applied again.

    An album cleanup plan that deletes the album's files additionally needs
    the explicit phrase in the body (confirm_delete_files="DELETE ALBUM
    FILES"); approving it here is otherwise refused, never implied (F5)."""
    payload = request.get_json(silent=True) or {}
    try:
        current_tx = transactions.get(transaction_id)
    except KeyError:
        return jsonify({"ok": False, "error": "Transaction not found"}), 404
    meta = current_tx.get("metadata") or {}
    if (meta.get("mutation_family") == composite_workflows.ALBUM_CLEANUP_FAMILY and meta.get("delete_files")
            and payload.get("confirm_delete_files") != composite_workflows.DELETE_ALBUM_FILES_CONFIRMATION):
        return jsonify({"ok": False, "code": "confirmation_required",
                        "error": "This album cleanup deletes files; approving it needs confirm_delete_files="
                                 f"\"{composite_workflows.DELETE_ALBUM_FILES_CONFIRMATION}\"."}), 400
    try:
        tx = transactions.transition(transaction_id, "Preview", "Approved",
                                     metadata={"approved_by": "operator (transactions approve route)"})
        if tx is None:
            current = transactions.get(transaction_id).get("status")
            return jsonify({"ok": False, "code": "not_preview",
                            "error": f"Only a Preview transaction can be approved (this one is {current})."}), 409
    except KeyError:
        return jsonify({"ok": False, "error": "Transaction not found"}), 404
    return jsonify({"ok": True, "transaction": tx})


@app.post("/api/transactions/<transaction_id>/cancel")
def api_transaction_cancel(transaction_id):
    # Compare-and-set (#187 F-6): only a not-yet-started transaction can be
    # cancelled, so a cancel racing an apply never overwrites its outcome.
    try:
        for expected in ("Pending", "Preview", "Approved"):
            tx = transactions.transition(transaction_id, expected, "Cancelled")
            if tx is not None:
                return jsonify({"ok": True, "transaction": tx})
        current = transactions.get(transaction_id).get("status")
    except KeyError:
        return jsonify({"ok": False, "error": "Transaction not found"}), 404
    return jsonify({"ok": False, "code": "not_cancellable",
                    "error": f"Only a Pending, Preview or Approved transaction can be cancelled "
                             f"(this one is {current})."}), 409


def _item_file_replacement_response(fn, transaction_id, *, rollback_family=None):
    """Run an engine-backed apply/rollback (item-file replacement, reviewed
    duplicate cleanup) with the same error mapping as the item replacement
    routes (no raw engine text leaks): every raised exception gets the
    controlled contract of cleanup_service.controlled_apply_error (#220), or
    of controlled_rollback_error when ``rollback_family`` is given (#227)."""
    try:
        res = fn(transaction_id)
    except Exception as exc:
        if rollback_family is None:
            body, status = controlled_apply_error(exc, transaction_id)
        else:
            body, status = controlled_rollback_error(
                exc, transaction_id, lock_before_write=rollback_family in _ROLLBACK_LOCKS_BEFORE_WRITE)
        return jsonify(body), status
    status_code = 200 if res.get("ok") else (409 if res.get("code") in ("not_approved", "already_applied") else 400)
    return jsonify(res), status_code


#: Engine-backed transaction families: (apply, rollback). Each apply needs an
#: Approved transaction, holds its durable resource locks, records the engine
#: request first and verifies afterwards; see backend/transaction_recovery.py.
_ENGINE_FAMILIES = {
    composite_workflows.ITEM_FILE_REPLACEMENT_FAMILY: (composite_workflows.apply_track_replacement,
                                                       composite_workflows.rollback_track_replacement),
    composite_workflows.TRACK_QUARANTINE_FAMILY: (composite_workflows.apply_track_quarantine,
                                                  composite_workflows.rollback_track_quarantine),
    duplicate_cleanup.REVIEWED_CLEANUP_FAMILY: (duplicate_cleanup.apply_reviewed_cleanup,
                                                duplicate_cleanup.rollback_reviewed_cleanup),
    album_row_merge.ALBUM_ROW_MERGE_FAMILY: (album_row_merge.apply_album_row_merge,
                                             album_row_merge.rollback_album_row_merge),
    untracked_recovery.ATTACH_FAMILY: (untracked_recovery.apply_recovery, untracked_recovery.rollback_recovery),
    untracked_recovery.QUARANTINE_FAMILY: (untracked_recovery.apply_recovery, untracked_recovery.rollback_recovery),
    untracked_recovery.ATTACH_ALBUM_FAMILY: (untracked_recovery.apply_recovery, untracked_recovery.rollback_recovery),
    composite_workflows.ALBUM_CLEANUP_FAMILY: (composite_workflows.apply_album_cleanup,
                                               composite_workflows.rollback_album_cleanup),
}

#: Families whose rollback executor acquires its resource locks before any
#: engine call or store write, so a ResourceLockConflictError from it proves
#: nothing changed (#227). The item replacement, track quarantine and reviewed
#: cleanup rollbacks take no lock, so a conflict from them proves nothing.
_ROLLBACK_LOCKS_BEFORE_WRITE = frozenset({
    album_row_merge.ALBUM_ROW_MERGE_FAMILY, untracked_recovery.ATTACH_FAMILY,
    untracked_recovery.QUARANTINE_FAMILY, untracked_recovery.ATTACH_ALBUM_FAMILY,
})


@app.post("/api/transactions/<transaction_id>/apply")
def api_transaction_apply(transaction_id):
    try:
        tx = transactions.get(transaction_id)
        family = (tx.get("metadata") or {}).get("mutation_family")
        if family == composite_workflows.ALBUM_CLEANUP_FAMILY:
            # Same classification and controlled errors as
            # /api/albums/cleanup/apply (PR #204 QA F-B).
            body, status = album_cleanup_apply_response(transaction_id)
            return jsonify(body), status
        engine_family = _ENGINE_FAMILIES.get(family)
        if engine_family:
            return _item_file_replacement_response(engine_family[0], transaction_id)
        if tx.get("operation_type") == "Metadata Update":
            job = _start_metadata_apply_transaction(transaction_id)
        else:
            return jsonify({"ok": False, "error": "Apply is not implemented for this transaction type yet."}), 409
    except KeyError:
        return jsonify({"ok": False, "error": "Transaction not found"}), 404
    except ValueError as ex:
        return jsonify({"ok": False, "error": str(ex)}), 409
    if job is None:
        return jsonify({"ok": True, "transaction": transactions.get(transaction_id)})
    return jsonify({"ok": True, "job_id": job.job_id, "transaction": transactions.get(transaction_id)})


@app.post("/api/transactions/<transaction_id>/rollback")
def api_transaction_rollback(transaction_id):
    _sync_transactions_from_jobs()
    try:
        tx = transactions.get(transaction_id)
    except KeyError:
        # SEC-002 Wave 17 final review: dispatch to the correct
        # family-specific rollback executor rather than always trying the
        # Import Review one -- that was exactly the "generic rollback
        # route silently routes everything through an unrelated family's
        # executor" anti-pattern flagged in Wave 16 review, just not yet
        # closed for a THIRD mutation family. Fetch the transaction's own
        # record first (a narrow, justified use of the engine
        # get_transaction lookup -- not the general browsing surface
        # removed in Wave 16) to learn its mutation_family, then dispatch;
        # each executor also independently enforces its own family match,
        # so this dispatch is a UX/correctness improvement, not the sole
        # security boundary.
        try:
            detail = composite_workflows.get_transaction(transaction_id)
        except BeetsUnavailableError as exc:
            # Never interpolate the raw exception text: BeetsClient._request()
            # falls back to embedding up to 200 raw response-body characters
            # for any non-JSON error response it doesn't recognize, which
            # could carry stack-trace-shaped text. error_code is a short,
            # fixed identifier string, never free text.
            return jsonify({"ok": False, "error": "Beets engine is unavailable.", "code": exc.error_code or "beets_unavailable"}), 503
        except BeetsError as exc:
            return jsonify({"ok": False, "error": "Transaction not found", "code": exc.error_code or "beets_error"}), 404
        except Exception:
            return jsonify({"ok": False, "error": "Transaction not found"}), 404

        engine_tx = (detail or {}).get("transaction") or {}
        mutation_family = (engine_tx.get("metadata") or {}).get("mutation_family")

        try:
            if mutation_family == "import_review_cleanup_v1":
                res = composite_workflows.rollback_import_review_cleanup(transaction_id)
            elif mutation_family == "album_mb_track_repair_v1":
                res = composite_workflows.rollback_album_mb_track_repair(transaction_id)
            elif mutation_family == "existing_album_reconcile_v1":
                res = composite_workflows.rollback_existing_album_reconcile(transaction_id)
            elif mutation_family == "artist_folder_reconcile_v1":
                res = composite_workflows.rollback_artist_folder_reconcile(transaction_id)
            elif mutation_family == "album_maintenance_v1":
                res = composite_workflows.rollback_album_maintenance(transaction_id)
            elif mutation_family == "album_artwork_v1":
                res = composite_workflows.rollback_album_artwork(transaction_id)
            elif mutation_family == "import_folder_v1":
                res = composite_workflows.rollback_import_folder(transaction_id)
            elif mutation_family == "folder_cleanup_v1":
                res = composite_workflows.rollback_folder_cleanup(transaction_id)
            elif mutation_family == "playlist_media_cleanup_v1":
                res = composite_workflows.rollback_playlist_media_cleanup(transaction_id)
            elif mutation_family:
                return jsonify({
                    "ok": False,
                    "error": f"Transactions of type {mutation_family!r} do not support rollback through this endpoint.",
                }), 400
            else:
                return jsonify({"ok": False, "error": "Transaction not found"}), 404
            status_code = 200 if res.get("ok") else 400
            return jsonify(res), status_code
        except BeetsUnavailableError as exc:
            return jsonify({"ok": False, "error": "Beets engine is unavailable.", "code": exc.error_code or "beets_unavailable"}), 503
        except BeetsError as exc:
            return jsonify({"ok": False, "error": "Rollback failed.", "code": exc.error_code or "beets_error"}), 400
        except Exception:
            return jsonify({"ok": False, "error": "Transaction not found"}), 404
    family = (tx.get("metadata") or {}).get("mutation_family")
    engine_family = _ENGINE_FAMILIES.get(family)
    if engine_family:
        return _item_file_replacement_response(engine_family[1], transaction_id, rollback_family=family)
    rollback = tx.get("rollback") or {}
    operations = rollback.get("operations") or []
    if not rollback.get("available") or not operations:
        return jsonify({
            "ok": False,
            "error": rollback.get("reason") or "Rollback unavailable.",
            "rollback_available": False,
        }), 409

    unsupported = [
        op for op in operations
        if op.get("type") != "metadata_restore" and op.get("type") != "recording_id_restore"
    ]
    if unsupported:
        return jsonify({
            "ok": False,
            "error": "Rollback unavailable for one or more recorded operation types.",
            "rollback_available": False,
        }), 409

    # Claim Completed -> Running (CAS) before the job starts (#219, ARCH-019).
    # Only an applied transaction may be rolled back: a Preview, Approved or
    # Cancelled one never wrote anything, so "restoring" its captured values
    # would itself be a library write. Failed qualifies only with an engine
    # apply record (metadata.engine_result, the same proof claim_approved
    # uses); these local families record none, so a Failed one is refused.
    status = tx.get("status")
    source = "Failed" if status == "Failed" and (tx.get("metadata") or {}).get("engine_result") else "Completed"
    if transactions.transition(transaction_id, source, "Running") is None:
        status = transactions.get(transaction_id).get("status")
        return jsonify({
            "ok": False,
            "error": f"Only a completed transaction can be rolled back (status is {status}).",
            "mutated": False,
            "status": status,
        }), 409

    def _do(log, cancel_event=None):
        ok_count = 0
        failed_count = 0
        try:
            for op in operations:
                if cancel_event is not None and cancel_event.is_set():
                    log.append("  [rollback] Cancel requested.")
                    break
                fields = op.get("fields") or {}
                item_id = int(op.get("item_id") or 0)
                if not item_id:
                    failed_count += 1
                    log.append("  [rollback] Missing item id; skipped operation.")
                    continue
                if op.get("type") == "recording_id_restore":
                    restored = _run_item_recording_id_restore(item_id, fields, log, cancel_event=cancel_event)
                else:
                    restored = _run_item_metadata_restore(item_id, fields, log, cancel_event=cancel_event)
                if restored:
                    ok_count += 1
                else:
                    failed_count += 1
            status = "Rolled Back" if failed_count == 0 else "Partially Rolled Back"
            transactions.update(
                transaction_id,
                status=status,
                logs=list(log)[-500:],
                counts={"rollback_ok": ok_count, "rollback_failed": failed_count},
            )
            return {"ok": failed_count == 0, "transaction_id": transaction_id, "rollback_ok": ok_count, "rollback_failed": failed_count}
        except Exception as ex:
            transactions.update(transaction_id, status="Failed", logs=list(log)[-500:])
            transactions.append_log(transaction_id, f"ERROR: rollback failed: {ex}")
            raise

    try:
        job = jobs.start_python(
            _do,
            label=f"Rollback transaction {transaction_id}",
            metadata={"transaction": False, "transaction_id": transaction_id, "type": "transaction-rollback"},
        )
    except Exception as ex:
        # Nothing ran: hand the claim back so the rollback can be retried
        # (SEC-223-2). The global handler returns a fixed 500.
        transactions.transition(transaction_id, "Running", source)
        transactions.append_log(transaction_id, "The rollback job could not be started; nothing was restored.")
        raise RuntimeError("The rollback job could not be started.") from ex
    transactions.update(transaction_id, metadata={"rollback_job_id": job.job_id})
    return jsonify({"ok": True, "job_id": job.job_id, "transaction": transactions.get(transaction_id)})


@app.get("/api/transactions/<transaction_id>/export")
def api_transaction_export(transaction_id):
    fmt = str(request.args.get("format") or "json").strip().lower()
    try:
        payload, mimetype = transactions.export(transaction_id, fmt)
    except KeyError:
        return jsonify({"ok": False, "error": "Transaction not found"}), 404
    ext = "md" if fmt in {"markdown", "md"} else ("csv" if fmt == "csv" else "json")
    response = Response(payload, mimetype=mimetype)
    response.headers["Content-Disposition"] = f"attachment; filename={transaction_id}.{ext}"
    return response


def _transaction_int_arg(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(request.args.get(name) or default)
    except Exception:
        value = default
    return max(minimum, min(maximum, value))
