"""Regression coverage for Import Review auto-import enqueue behavior."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app.py").read_text(encoding="utf-8")
REVIEW_PAGE = (ROOT / "frontend" / "src" / "features" / "importReview" / "ImportReviewPage.tsx").read_text(encoding="utf-8")
CLIENT = (ROOT / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
TYPES = (ROOT / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")


def section(source: str, start: str, end: str) -> str:
    start_idx = source.index(start)
    end_idx = source.index(end, start_idx + len(start))
    return source[start_idx:end_idx]


class ImportAutoEnqueueTests(unittest.TestCase):
    def test_backend_endpoint_and_evaluator_exist(self):
        self.assertIn('def evaluate_import_eligibility(payload: Dict[str, Any])', APP)
        self.assertIn('@app.post("/api/import-review/auto-enqueue")', APP)
        self.assertIn('def import_review_auto_enqueue():', APP)

    def test_backend_uses_sixty_percent_threshold_and_identity_gates(self):
        body = section(APP, 'def evaluate_import_eligibility', 'def _import_review_start_auto_import')
        self.assertIn('IMPORT_REVIEW_AUTO_IMPORT_CONFIDENCE_THRESHOLD = 0.60', APP)
        self.assertIn('confidence below 60% auto-import threshold', body)
        self.assertIn('Release Group ID missing or invalid', body)
        self.assertIn('representative release ID missing or invalid', body)
        self.assertIn('track preflight is not passed', body)

    def test_backend_validates_selected_subset_and_target_preview(self):
        body = section(APP, 'def evaluate_import_eligibility', 'def _import_review_start_auto_import')
        self.assertIn('_import_review_selected_source_files(', body)
        self.assertIn('_cached_import_target_preview(preview_payload)', body)
        self.assertIn('real target conflicts exist', body)
        self.assertIn('no safe verified tracks selected for import', body)

    def test_idempotency_key_uses_review_release_and_sorted_selected_files(self):
        body = section(APP, 'def _import_review_auto_key', 'def evaluate_import_eligibility')
        self.assertIn('"review_item_id"', body)
        self.assertIn('"release_group_id"', body)
        self.assertIn('"representative_release_id"', body)
        self.assertIn('"selected_files": sorted(', body)
        self.assertIn('hashlib.sha256', body)

    def test_backend_can_enqueue_ready_pending_rows_durably(self):
        self.assertIn('@app.post("/api/import-review/auto-enqueue-ready")', APP)
        body = section(APP, 'def _run_import_review_auto_enqueue_ready_batch', '@app.post("/api/import-review/auto-enqueue")')
        self.assertIn('_load_pending_reviews(prune_resolved=False)', body)
        self.assertIn('_pending_item_is_ready_for_backend_auto_import(item)', body)
        self.assertIn('eligibility = evaluate_import_eligibility(item_payload)', body)
        self.assertIn('_import_review_start_auto_import(item_payload, eligibility)', body)
        self.assertIn('limit = max(1, min(int(limit or 5), 25))', body)

    def test_backend_ready_enqueue_has_controlled_worker_endpoint(self):
        self.assertIn('@app.post("/api/import-review/auto-enqueue-ready/job")', APP)
        body = section(APP, 'def import_review_auto_enqueue_ready_job', '@app.post("/api/import-review/auto-enqueue")')
        self.assertIn('jobs.start_python(_do, label="Auto-import ready review items")', body)
        self.assertIn('_run_import_review_auto_enqueue_ready_batch(limit, log, cancel_event)', body)
    def test_backend_ready_enqueue_skips_failed_or_running_statuses(self):
        body = section(APP, 'def _pending_item_is_ready_for_backend_auto_import', '@app.post("/api/import-review/auto-enqueue-ready")')
        self.assertIn('"auto_enqueue_failed"', body)
        self.assertIn('"format_policy_rejected"', body)
        self.assertIn('"import_enqueueing"', body)
        self.assertIn('"import_queued"', body)
        self.assertIn('if not suggestion.get("mb_valid")', body)
        self.assertIn('preflight.get("ok") is False', body)
    def test_target_preview_uses_short_lived_backend_plan_cache(self):
        self.assertIn("_IMPORT_TARGET_PREVIEW_CACHE_TTL", APP)
        self.assertIn("def _import_target_preview_cache_key(", APP)
        self.assertIn("def _cached_import_target_preview(", APP)
        preview_route = section(APP, '@app.post("/api/folders/import-target-preview")', 'def _import_review_confidence_score')
        self.assertIn('_cached_import_target_preview(payload)', preview_route)
        body = section(APP, 'def evaluate_import_eligibility', 'def _import_review_start_auto_import')
        self.assertIn('_cached_import_target_preview(preview_payload)', body)

    def test_auto_enqueue_persists_computed_import_plan(self):
        body = section(APP, 'def _import_review_start_auto_import', '@app.post("/api/import-review/auto-enqueue")')
        self.assertIn('import_plan={', body)
        self.assertIn('"plan_cache_key": (eligibility.get("target_preview") or {}).get("plan_cache_key", "")', body)
        self.assertIn('"target_preview": eligibility.get("target_preview") or {}', body)
    def test_auto_enqueue_reuses_existing_job_and_starts_import_with_subset(self):
        body = section(APP, 'def _import_review_start_auto_import', '@app.post("/api/import-review/auto-enqueue")')
        self.assertIn('_import_review_auto_job_for_submission(payload, key)', body)
        self.assertIn('"existing_job": True', body)
        self.assertIn('"selected_source_files": eligibility["selected_file_paths"]', body)
        self.assertIn('"auto_import_idempotency_key": key', body)
        self.assertIn('with app.test_request_context("/api/folders/import-with-id"', body)

    def test_auto_import_reuses_verified_track_mapping_before_broad_rematch(self):
        body = section(APP, '@app.post("/api/folders/import-with-id")', 'def _match_tracks_from_mb')
        self.assertIn('def _apply_verified_review_track_mapping(album_db_id: int) -> int:', body)
        self.assertIn('auto_import and selected_subset_import and importable_rows', body)
        self.assertIn('Applied verified Import Review track mapping', body)
        self.assertIn('skipped broad MB title rematch', body)
        self.assertIn('matched = _match_tracks_from_mb(mb_albumid, album_db_id, log)', body)
        self.assertIn('log.append("[3/4] Syncing album metadata from MusicBrainz…")', body)
        self.assertIn('log.append("[3/4] Writing tags to audio files…")', body)
        self.assertIn('log.append("[4/4] Renaming files to match library path template…")', body)
    def test_import_job_metadata_contains_immutable_selection_and_skips_plex_by_default(self):
        body = section(APP, '@app.post("/api/folders/import-with-id")', 'def _match_tracks_from_mb')
        self.assertIn('"selected_source_files": [str(p) for p in selected_source_files]', body)
        self.assertIn('"import_review_auto_idempotency_key": auto_import_idempotency_key', body)
        self.assertIn('trigger_plex_refresh_after = bool(payload.get("trigger_plex"))', body)
        self.assertIn('trigger_plex_context = _s(payload.get("trigger_plex_context")', body)
        self.assertIn('if trigger_plex_refresh_after:', body)
        self.assertIn('_trigger_plex_refresh(log, workflow=trigger_plex_context)', body)
        self.assertIn('Refresh skipped for Import Review; playlist/batch jobs trigger Plex separately.', body)
        self.assertIn('_ALLOWED_PLEX_REFRESH_WORKFLOWS = {"batch", "playlist", "manual"}', APP)
        self.assertIn('workflow_key not in _ALLOWED_PLEX_REFRESH_WORKFLOWS', APP)
        self.assertIn('automatic scans run only for playlist and batch jobs', APP)

    def test_pending_review_state_moves_queued_items_out_and_remaining_files_back(self):
        self.assertIn('def _mark_pending_review_status(', APP)
        self.assertIn('item_status_key = _review_status_key(item_status)', APP)
        self.assertIn('if item_status_key in {"import_enqueueing", "import_queued"}:', APP)
        self.assertIn('"remaining_files_review"', APP)
        self.assertIn('"auto_enqueue_failed"', APP)
        self.assertIn('_MUSIC_FORMAT_POLICY_REVIEW_STATUS = "format_policy_rejected"', APP)
        self.assertIn('def _finalize_pending_review_format_policy_rejection', APP)
        self.assertIn('pending_review_removed', APP)
        self.assertIn('handled audio-policy rejection', APP)
        self.assertIn('decision_note=status_note', APP)

    def test_already_in_library_auto_import_resolves_pending_review(self):
        self.assertIn('def _import_job_resolved_as_already_in_library(job) -> bool:', APP)
        self.assertIn('"album already in library" in text and "source cleaned up" in text', APP)
        body = section(APP, 'def _reconcile_pending_review_enqueue_item', 'def _import_review_auto_key')
        self.assertIn('if _import_job_resolved_as_already_in_library(job):', body)
        self.assertIn('return None, True', body)
        import_body = section(APP, '@app.post("/api/folders/import-with-id")', 'def _match_tracks_from_mb')
        self.assertIn('_remove_pending_review_for_path(folder_path, log)', import_body)
        self.assertIn('return {"status": "already_in_library"}', import_body)

    def test_stale_review_item_with_verified_tracks_resumes_import(self):
        self.assertIn('def _stale_review_item_can_resume_import(item: Dict[str, Any]) -> bool:', APP)
        self.assertIn('revalidation.get("importable_track_count")', APP)
        self.assertIn('"auto_enqueue_failed", "import_job_missing", "import_status_unknown"', APP)
        body = section(APP, 'def _reconcile_pending_review_enqueue_item', 'def _import_review_auto_key')
        self.assertIn('if _stale_review_item_can_resume_import(item):', body)
        self.assertIn('item["status"] = "ready_to_import"', body)
        self.assertIn('Verified tracks are ready to import; unmatched files stay in review after import.', body)

    def test_nested_missing_track_import_bypasses_parent_import_lock(self):
        self.assertIn('skip_import_lock: bool = False', APP)
        self.assertIn('payload["skip_import_lock"] = True', APP)
        self.assertIn('skip_import_lock=True', APP)
        body = section(APP, '@app.post("/api/albums/reimport-disk")', '_LIBRARY_IMPORT_ALL_LAST_FILE')
        self.assertIn('skip_import_lock = bool(payload.get("skip_import_lock"))', body)
        self.assertIn('[reimport] Running inside parent import slot', body)
        self.assertIn('"skip_import_lock": skip_import_lock', body)
    def test_import_album_lookup_prefers_musicbrainz_before_artist_scan(self):
        self.assertIn('def _library_album_ids_for_musicbrainz(mb_albumid: str = "", mb_releasegroupid: str = "") -> List[int]:', APP)
        body = section(APP, '# ── Step 2: find the album', 'if not album_ids and not item_ids:')
        lookup = section(APP, '# ── Step 2: find the album', '# E: search by album TEXT field')
        self.assertIn('# C: exact MusicBrainz identity lookup before broad artist-folder scans.', lookup)
        self.assertIn('_library_album_ids_for_musicbrainz(mb_albumid, selected_releasegroupid)', lookup)
        self.assertLess(lookup.index('_library_album_ids_for_musicbrainz'), lookup.index('f"{music_root}/{artist_name}"'))
    def test_frontend_calls_backend_auto_enqueue_after_safe_preview(self):
        body = section(REVIEW_PAGE, 'Backend-owned auto-enqueue', 'Keep ref in sync')
        self.assertIn('autoEnqueueImport(payload)', body)
        self.assertIn('!preview.safe', body)
        self.assertIn('selectedImportSourceFiles(sm, preview)', body)
        self.assertIn('removeLocalItem(item)', body)
        self.assertIn('pollJobBackground(jobId, item, label, keepReviewAfterSuccess)', body)

    def test_frontend_auto_enqueue_is_not_manual_click_simulation(self):
        body = section(REVIEW_PAGE, 'Backend-owned auto-enqueue', 'Keep ref in sync')
        self.assertNotIn('runApply(', body)
        self.assertNotIn('onApply(', body)
        self.assertNotIn('.click(', body)

    def test_api_types_and_client_are_registered(self):
        self.assertIn('export interface AutoEnqueueImportPayload extends ImportWithIdPayload', TYPES)
        self.assertIn('export interface AutoEnqueueImportResponse extends JobStartResponse', TYPES)
        self.assertIn('handled?: boolean', TYPES)
        self.assertIn('pending_review_exists?: boolean', TYPES)
        self.assertIn('note?: string', TYPES)
        self.assertIn('export function autoEnqueueImport(payload: AutoEnqueueImportPayload)', CLIENT)
        self.assertIn('export function reconcileAutoEnqueueImport(payload: AutoEnqueueImportPayload)', CLIENT)
        self.assertIn("'/api/import-review/auto-enqueue'", CLIENT)
        self.assertIn("'/api/import-review/auto-enqueue/reconcile'", CLIENT)

    def test_backend_rejects_stale_review_item_and_count_mismatch(self):
        body = section(APP, 'def evaluate_import_eligibility', 'def _import_review_start_auto_import')
        self.assertIn('_pending_review_matches(folder_path, review_item_id)', body)
        self.assertIn('review item does not match source folder', body)
        self.assertIn('selected file count does not match target preview', body)

    def test_backend_reconciles_stale_enqueueing_items(self):
        self.assertIn('def _reconcile_pending_review_enqueue_item(', APP)
        self.assertIn('resume_statuses = {"remaining_files_review", "auto_enqueue_failed", "import_job_missing", "import_status_unknown"}', APP)
        self.assertIn('active_statuses = {"import_enqueueing", "import_queued"}', APP)
        self.assertIn('status_key not in (active_statuses | resume_statuses)', APP)
        self.assertIn('Import enqueue did not create an active job; retry enqueue.', APP)
        self.assertIn('@app.post("/api/import-review/auto-enqueue/reconcile")', APP)

    def test_frontend_resets_per_item_state_when_review_item_changes(self):
        body = section(REVIEW_PAGE, 'const loadQueue = useCallback', 'useEffect(() => {')
        self.assertIn('reviewItemStateKey(item)', body)
        self.assertIn('changedIds.add(item.id)', body)
        self.assertIn('setSelectedMatches((current)', body)
        self.assertIn('setActions((current)', body)

    def test_frontend_enqueue_timeout_reconciles_instead_of_resubmitting(self):
        body = section(REVIEW_PAGE, 'Backend-owned auto-enqueue', 'Keep ref in sync')
        self.assertIn('withTimeout(autoEnqueueImport(payload)', body)
        self.assertIn('reconcileAutoEnqueueImport(payload)', body)
        self.assertIn('Retry enqueue', body)

    def test_frontend_preview_button_payload_counts_share_selection(self):
        body = section(REVIEW_PAGE, 'Backend-owned auto-enqueue', 'Keep ref in sync')
        self.assertIn('const previewCount = preview.tracks_to_import_count ?? selectedSourceFiles.length', body)
        self.assertIn('selectedSourceFiles.length !== previewCount', body)
        self.assertIn('selected_source_files: selectedSourceFiles', body)
        self.assertIn('function actionLabel(item: ReviewItem, selectedMatch?: SelectedMatch, preview?: ImportTargetPreviewResponse)', REVIEW_PAGE)


    def test_revalidate_endpoint_uses_shared_mapping_and_auto_enqueue_gate(self):
        self.assertIn('@app.post("/api/import-review/revalidate")', APP)
        body = section(APP, 'def import_review_revalidate', '@app.post("/api/folders/ai-suggest")')
        self.assertIn('release_group_id=release_group_id', body)
        self.assertIn('_import_review_build_revalidated_match(item, comparison, preflight)', body)
        self.assertIn('_update_pending_review_revalidation(path, selected_match, preflight, note)', body)
        self.assertIn('eligibility = evaluate_import_eligibility(auto_payload)', body)
        self.assertIn('_import_review_start_auto_import(auto_payload, eligibility)', body)
        self.assertIn('auto_enqueue and eligibility.get("eligible")', body)
        self.assertIn('if isinstance(raw_ids, str):', body)
        self.assertIn('explicit_all = payload.get("all") is True or payload.get("review_all") is True', body)
        self.assertIn('auto_enqueue = payload.get("auto_enqueue") is True', body)
        self.assertIn('if not review_ids and not explicit_all:', body)
        self.assertIn('review_item_ids required unless all=true', body)

    def test_revalidate_persists_track_mapping_for_existing_review_items(self):
        body = section(APP, 'def _update_pending_review_revalidation', '@app.post("/api/import-review/revalidate")')
        self.assertIn('suggestion["track_mapping"] = selected_match.get("track_mapping") or []', body)
        self.assertIn('evidence["preflight"] = preflight', body)
        self.assertIn('item["status"] = "ready_to_import" if selected_match.get("is_importable") else "no_verified_tracks"', body)

    def test_candidate_route_and_revalidator_share_mapping_source(self):
        route_start = APP.index('@app.get("/api/candidates/<mb_albumid>/tracks")')
        route_end = APP.index('\ndef _target_preview_year(', route_start)
        route = APP[route_start:route_end]
        revalidator = section(APP, 'def import_review_revalidate', '@app.post("/api/folders/ai-suggest")')
        self.assertIn('_candidate_track_comparison_payload(mb_albumid, folder, release_group_id=release_group_id)', route)
        self.assertIn('release_group_id=release_group_id', revalidator)

    def test_revalidate_client_types_and_wrapper_are_registered(self):
        self.assertIn('export interface ImportReviewRevalidatePayload', TYPES)
        self.assertIn('export interface ImportReviewRevalidateResponse', TYPES)
        self.assertIn('export function revalidateImportReview(payload: ImportReviewRevalidatePayload)', CLIENT)
        self.assertIn("'/api/import-review/revalidate'", CLIENT)

    def test_frontend_revalidate_button_hydrates_matches_and_refreshes_queue(self):
        self.assertIn('revalidateImportReview,', REVIEW_PAGE)
        self.assertIn('const handleRevalidateQueue = useCallback', REVIEW_PAGE)
        self.assertIn('review_item_ids: ids, auto_enqueue: true', REVIEW_PAGE)
        self.assertIn('setSelectedMatches((current)', REVIEW_PAGE)
        self.assertIn('next[row.review_item_id] = row.selected_match as unknown as SelectedMatch', REVIEW_PAGE)
        self.assertIn("{revalidateBusy ? 'Revalidating…' : 'Revalidate'}", REVIEW_PAGE)

    def test_auto_enqueue_does_not_reuse_stale_persisted_job_id(self):
        body = section(APP, 'def _import_review_start_auto_import', '@app.post("/api/import-review/auto-enqueue")')
        self.assertIn('state_job = jobs.get(existing_state_job_id) if existing_state_job_id else None', body)
        self.assertIn('reusable_job = existing_job if existing_job and existing_job.status == "running" else None', body)
        self.assertIn('status="stale_job_missing"', body)
        self.assertIn('job_id=""', body)
        self.assertNotIn('if existing_job or existing_state_job_id:', body)

    def test_auto_enqueue_reconcile_only_queues_running_jobs(self):
        body = section(APP, '@app.post("/api/import-review/auto-enqueue/reconcile")', 'def _import_review_reconcile_job_lookup')
        self.assertIn('if job and job.status == "running":', body)
        self.assertIn('"queued": True', body)
        self.assertIn('if job and job.status in {"failed", "success"}:', body)
        self.assertIn('"queued": False', body)
        self.assertIn('status="stale_job_missing"', body)
        self.assertNotIn('job.status in {"running", "success"}', body)

    def test_frontend_does_not_auto_enqueue_failed_enqueue_rows(self):
        body = section(REVIEW_PAGE, 'Backend-owned auto-enqueue', 'Keep ref in sync')
        self.assertIn("const statusKey = (item.status_key || item.status || '').trim().toLowerCase().replace", body)
        self.assertIn("['auto_enqueue_failed', 'format_policy_rejected', 'import_failed', 'failed'].includes(statusKey)", body)
        self.assertIn('if (started.handled)', body)
        self.assertIn('if (result.handled)', body)
        self.assertIn("status: 'warning'", body)
        self.assertIn('if (started.pending_review_exists === false) removeLocalItem(item)', body)
if __name__ == "__main__":
    unittest.main()











