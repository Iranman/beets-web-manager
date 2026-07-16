import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app.py").read_text(encoding="utf-8")
INTAKE = (ROOT / "frontend" / "src" / "features" / "intake" / "IntakePanel.tsx").read_text(encoding="utf-8")
CLIENT = (ROOT / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
TYPES = (ROOT / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")


def section(source: str, start: str, end: str) -> str:
    start_idx = source.index(start)
    end_idx = source.index(end, start_idx)
    return source[start_idx:end_idx]


class AiBatchImportReliabilityTests(unittest.TestCase):
    def test_batch_scan_creates_durable_batch_job(self):
        self.assertIn('_AI_BATCH_STATE_DIR = Path(os.environ.get("AI_BATCH_STATE_DIR", "/config/ai_batch_jobs"))', APP)
        self.assertIn('def _ai_batch_write_state', APP)
        self.assertIn('def _ai_batch_initial_state', APP)
        self.assertIn('"batch_job_id": batch_job_id', APP)
        self.assertIn('"source_path": source_path', APP)
        self.assertIn('"heartbeat_at": now', APP)

    def test_discovered_folders_create_per_folder_work_items(self):
        self.assertIn('def _ai_batch_folder_state', APP)
        for field in [
            '"folder_id": _ai_batch_folder_id(source_folder)',
            '"source_folder": source_folder',
            '"ai_suggest_started_at": None',
            '"ai_suggest_completed_at": None',
            '"review_item_id": ""',
            '"suggested_release_group_id": ""',
            '"failure_reason": ""',
        ]:
            self.assertIn(field, APP)
        self.assertIn('folder_states[fid] = _ai_batch_folder_state(batch_job_id, folder)', APP)

    def test_max_three_ai_suggestions_run_in_parallel_without_as_completed_wait(self):
        route = section(APP, '@app.post("/api/ai-batch-import")', '# ── Library scan')
        self.assertIn('_AI_BATCH_MAX_AI_WORKERS =', APP)
        self.assertIn('len(active) < _AI_BATCH_MAX_AI_WORKERS', APP)
        self.assertNotIn('ThreadPoolExecutor', route)
        self.assertNotIn('as_completed', route)

    def test_one_ai_suggestion_timeout_does_not_block_batch(self):
        self.assertIn('_AI_BATCH_AI_TIMEOUT =', APP)
        self.assertIn('AI suggestion timed out after', APP)
        self.assertIn('status="timed_out"', APP)
        self.assertIn('active.pop(fid, None)', APP)
        self.assertIn('while pending or active:', APP)
        self.assertIn('return "done"', APP)

    def test_failed_folder_is_marked_failed_and_batch_continues(self):
        self.assertIn('status="failed"', APP)
        self.assertIn('folder failed with reason:', APP)
        self.assertIn('AI failed; queued for review', APP)
        self.assertIn('import failed; queued for review', APP)
        self.assertIn('continue', section(APP, 'def _ai_batch_process_decisions', 'def _run_ai_batch_import'))

    def test_refresh_reconnects_to_running_batch_state(self):
        self.assertIn('@app.get("/api/ai-batch-import/status")', APP)
        self.assertIn('def _ai_batch_find_state', APP)
        self.assertIn('getAiBatchStatus(jobId)', INTAKE)
        self.assertIn('localStorage.getItem(AI_BATCH_JOB_STORAGE_KEY)', INTAKE)
        self.assertIn('localStorage.setItem(AI_BATCH_JOB_STORAGE_KEY, nextJobId)', INTAKE)

    def test_stale_heartbeat_marks_batch_recoverable(self):
        self.assertIn('_AI_BATCH_STALE_SECONDS =', APP)
        self.assertIn('state["status"] = "stale"', APP)
        self.assertIn('Batch appears stuck; worker heartbeat is stale.', APP)
        self.assertIn('def _ai_batch_mark_stale_ai_running_folders', APP)
        self.assertIn('current_step="stale AI suggestion timed out"', APP)
        self.assertIn('_ai_batch_commit(state, heartbeat=False)', APP)
        self.assertIn('Batch appears stuck', INTAKE)
        self.assertIn('Recover batch', INTAKE)

    def test_status_poll_does_not_refresh_worker_heartbeat(self):
        route = section(APP, '@app.get("/api/ai-batch-import/status")', '@app.post("/api/ai-batch-import/recover")')
        self.assertIn('state = _ai_batch_reconcile_state(state)', route)
        self.assertNotIn('_ai_batch_commit(state, None)', route)
        self.assertIn('_ai_batch_commit(latest, None, heartbeat=False)', APP)

    def test_recover_batch_requeues_only_unfinished_folders(self):
        self.assertIn('@app.post("/api/ai-batch-import/recover")', APP)
        self.assertIn('loaded existing batch with', APP)
        self.assertIn('review item already exists', APP)
        self.assertIn('requeued by recovery', APP)
        self.assertIn('Recover batch', INTAKE)

    def test_duplicate_review_items_are_not_created_on_recovery(self):
        self.assertIn('_pending_review_path_key(src) in pending_review_paths', APP)
        self.assertIn('not _pending_review_has_path(folder)', APP)
        self.assertIn('def _ai_batch_queue_pending_review', APP)
        self.assertIn('_add_to_pending(folder, suggestion, evidence=evidence, origin=origin)', APP)

    def test_start_action_is_hidden_while_batch_is_running(self):
        self.assertIn('{!jobId ? (', INTAKE)
        self.assertIn('Import All', INTAKE)
        self.assertIn('Batch Import', INTAKE)
        self.assertIn('Pause', INTAKE)
        self.assertIn('Stop', INTAKE)

    def test_pause_stops_new_folder_claims(self):
        self.assertIn('@app.post("/api/ai-batch-pause")', APP)
        self.assertIn('control["pause"] = True', APP)
        self.assertIn('while pending and not paused and len(active) < _AI_BATCH_MAX_AI_WORKERS', APP)
        self.assertIn('Pause requested. Active folder work can finish or time out.', INTAKE)

    def test_stop_cancels_batch_safely(self):
        self.assertIn('cancel_event and cancel_event.is_set()', APP)
        self.assertIn('state["status"] = "canceled"', APP)
        self.assertIn('batch canceled', APP)
        self.assertIn('@app.post("/api/ai-batch-stop")', APP)
        self.assertIn('stopAiBatch(jobId)', INTAKE)

    def test_skip_current_marks_active_folder_skipped_and_continues(self):
        self.assertIn('control["skip_current"] = True', APP)
        self.assertIn('status="skipped"', APP)
        self.assertIn('skipped by user', APP)
        self.assertIn('Skip current', INTAKE)
        self.assertIn('skipAiBatch(jobId, folderId)', INTAKE)

    def test_worker_thread_does_not_clobber_job_id_back_to_empty(self):
        # jobs.start_python() spawns its thread before returning job_id, so the
        # worker must wait for and stamp the real job_id onto the state object
        # it repeatedly commits — otherwise a later worker commit overwrites the
        # job_id the route handler set on its own separate copy of the state,
        # permanently orphaning the batch under an empty job_id (unreachable by
        # the id the frontend polls with).
        run_fn = section(APP, "def _run_ai_batch_import(", "def _start_ai_batch_job(")
        self.assertIn("job_id: str = \"\"", run_fn)
        self.assertIn('if job_id:', run_fn)
        self.assertIn('state["job_id"] = job_id', run_fn)

        start_fn = section(APP, "def _start_ai_batch_job(", "@app.post(\"/api/ai-batch-skip\")")
        self.assertIn("job_id_ready = threading.Event()", start_fn)
        self.assertIn("job_id_ready.wait(timeout=", start_fn)
        self.assertIn("job_id_holder.get(\"job_id\", \"\")", start_fn)
        self.assertIn("job_id_holder[\"job_id\"] = job.job_id", start_fn)
        self.assertIn("job_id_ready.set()", start_fn)

    def test_status_endpoint_returns_404_and_distinguishes_missing_state(self):
        # A missing AI batch state must never come back as a plain 200 ok:false
        # (the frontend would treat that ambiguously); it needs an explicit
        # not-found signal so the UI can stop rendering a false "Running" state.
        route = section(APP, '@app.get("/api/ai-batch-import/status")', '@app.post("/api/ai-batch-import/recover")')
        self.assertIn('jobs.get(ident) if ident else None', route)
        self.assertIn('"reason": "child_missing"', route)
        self.assertIn('"reason": "not_found"', route)
        self.assertIn('"Batch job no longer exists."', route)
        self.assertIn('}), 404', route)

        self.assertIn('const missing = statusLoaded && !batchState && Boolean(statusError);', INTAKE)
        self.assertIn('const done = batchDone || missing || jobEndedWithoutState;', INTAKE)
        self.assertIn("Batch job no longer exists for this ID.", INTAKE)

    def test_completed_ai_suggestions_are_cached(self):
        self.assertIn('and not f.get("ai_result")', APP)
        self.assertIn('ai_result=result', APP)
        self.assertIn('item.pop("ai_result", None)', APP)
        self.assertIn('state?: AiBatchState', TYPES)
        self.assertIn('getAiBatchStatus', CLIENT)



    def test_batch_finalizer_completes_all_terminal_folder_states(self):
        self.assertIn('def _ai_batch_recalculate_batch_state', APP)
        self.assertIn('processed < total', APP)
        self.assertIn('unfinished > 0', APP)
        self.assertIn('state["status"] = next_status', APP)
        self.assertIn('"completed_with_warnings" if attention else "completed"', APP)
        self.assertIn('state["recovery_state"] = ""', APP)
        self.assertIn('Batch finalized:', APP)

    def test_ai_completed_is_decision_ready_not_processed_terminal(self):
        self.assertIn('_AI_BATCH_DECISION_READY_STATUSES = {"ai_completed"}', APP)
        self.assertIn('_AI_BATCH_UNFINISHED_FOLDER_STATUSES = (', APP)
        self.assertIn('_AI_BATCH_IMPORTED_FOLDER_STATUSES = {"completed", "imported"}', APP)
        self.assertIn('status="imported", current_step="imported"', APP)

    def test_policy_rejected_maps_to_warning_outcome(self):
        self.assertIn('_AI_BATCH_FORMAT_POLICY_HANDLED_MESSAGE = _MUSIC_FORMAT_POLICY_HANDLED_MESSAGE', APP)
        self.assertIn('_is_music_format_policy_handled_error(reason)', APP)
        self.assertIn('_music_format_policy_review_note(reason)', APP)
        self.assertIn('_music_format_policy_review_note(err)', APP)
        self.assertIn('"status": "policy_rejected"', APP)
        self.assertIn('folders_warning', APP)

    def test_recovery_finalizes_already_terminal_batch_without_requeue(self):
        self.assertIn('No unfinished folder work remains; recovery finalized existing batch.', APP)
        self.assertIn('if state.get("status") in _AI_BATCH_TERMINAL_STATUSES and not retry_failed:', APP)
        self.assertIn('return jsonify({"ok": True, "job_id": state.get("job_id") or batch_job_id', APP)

    def test_recovery_requeues_only_retryable_outcomes(self):
        self.assertIn('_AI_BATCH_RETRYABLE_FOLDER_STATUSES = {"failed", "import_failed", "ai_failed", "timed_out", "replacement_unavailable"}', APP)
        self.assertIn('retry_failed and status in _AI_BATCH_RETRYABLE_FOLDER_STATUSES', APP)
        self.assertNotIn('retry_failed and status in _AI_BATCH_POLICY_WARNING_STATUSES', APP)
        self.assertIn('retryable failure requeued', APP)
        self.assertIn('if status in (_AI_BATCH_IMPORTED_FOLDER_STATUSES | _AI_BATCH_SKIPPED_FOLDER_STATUSES', APP)

    def test_stale_cached_ai_completed_finalizes_as_review_required(self):
        self.assertIn('def _ai_batch_mark_orphaned_ai_completed_for_review', APP)
        self.assertIn('status="review_required"', APP)
        reconcile_fn = section(APP, 'def _ai_batch_reconcile_state', 'def _ai_batch_queue_pending_review')
        self.assertIn('no_active_folder_work', reconcile_fn)
        self.assertIn('counts.get("ai_completed")', reconcile_fn)
        self.assertIn('_ai_batch_mark_orphaned_ai_completed_for_review(state)', reconcile_fn)
    def test_recovery_uses_cached_pending_review_paths(self):
        self.assertIn('def _pending_review_path_set', APP)
        run_fn = section(APP, 'def _run_ai_batch_import(', 'def _start_ai_batch_job(')
        self.assertIn('pending_review_paths = _pending_review_path_set()', run_fn)
        self.assertIn('_pending_review_path_key(src) in pending_review_paths', run_fn)
        self.assertNotIn('_pending_review_has_path(src)', run_fn)
        self.assertIn('cached decision(s) ready', run_fn)

    def test_cached_decision_processing_throttles_large_state_commits(self):
        process_fn = section(APP, 'def _ai_batch_process_decisions', 'def _run_ai_batch_import')
        self.assertIn('def _commit(force: bool = False)', process_fn)
        self.assertIn('now - last_commit_at >= 5', process_fn)
        self.assertIn('_commit(True)', process_fn)
    def test_terminal_ai_batch_job_rows_do_not_count_as_running(self):
        routes = (ROOT / "routes_jobs.py").read_text(encoding="utf-8")
        self.assertIn('def _present_job_row', routes)
        self.assertIn('metadata.get("type") == "ai-batch-import"', routes)
        self.assertIn('row["status"] = "success"', routes)
        self.assertIn('_ai_batch_reconcile_state(state)', routes)

    def test_terminal_batch_ui_hides_active_controls_and_shows_summary(self):
        self.assertIn('Review {attentionCount} issue', INTAKE)
        self.assertIn('Retry eligible failures', INTAKE)
        self.assertIn('View report', INTAKE)
        self.assertIn('Activity log', INTAKE)
        self.assertIn('folderStatusLabel(folder.status)', INTAKE)
        self.assertIn('value={done && total > 0 ? 100 : progressValue}', INTAKE)
if __name__ == "__main__":
    unittest.main()




