import unittest
from pathlib import Path



class ImportReviewQualityFilterTests(unittest.TestCase):
    def test_review_page_exposes_music_matching_buckets(self):
        root = Path(__file__).resolve().parents[1]
        review_source = (
            root / "frontend" / "src" / "features" / "importReview" / "ImportReviewPage.tsx"
        ).read_text(encoding="utf-8")
        types_source = (root / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")
        app_source = (root / "app.py").read_text(encoding="utf-8")

        self.assertIn("export type QueueFilter =", review_source)
        self.assertIn("| 'ready'", review_source)
        self.assertIn("| 'blocked'", review_source)
        self.assertIn("| 'audio_mismatch'", review_source)
        self.assertIn("| 'failed'", review_source)
        self.assertIn("| 'no_candidate'", review_source)
        self.assertIn("type MatchBucket = 'ready' | 'blocked' | 'audio_mismatch' | 'failed' | 'no_candidate' | 'needs_id'", review_source)
        self.assertIn("const filters", review_source)
        self.assertIn("{ id: 'blocked', label: 'Blocked' }", review_source)
        self.assertIn("{ id: 'audio_mismatch', label: 'Audio Mismatch' }", review_source)
        self.assertIn("{ id: 'failed', label: 'Failed' }", review_source)
        self.assertIn("function itemMatchBucket(item: ReviewItem)", review_source)
        self.assertIn("function hasAudioMismatchEvidence", review_source)
        self.assertIn("function shouldShowBlockedBucket", review_source)
        self.assertIn("function shouldShowReadyBucket", review_source)
        self.assertIn("function storedBlockedNextAction", review_source)
        self.assertIn("function blockedActionHint", review_source)
        self.assertIn("if (item.blocked_reason) return item.blocked_reason", review_source)
        self.assertIn("storedBlockedNextAction(item) ||", review_source)
        self.assertIn("status_key?: string", types_source)
        self.assertIn("blocked_reason?: string", types_source)
        self.assertIn("blocked_next_action?: string", types_source)
        self.assertIn("def _review_blocked_metadata", app_source)
        self.assertIn("\"blocked_reason\": blocked.get(\"reason\", \"\")", app_source)
        self.assertIn("\"blocked_next_action\": blocked.get(\"next_action\", \"\")", app_source)
        self.assertIn("if (hasAudioMismatchEvidence(item)) return 'audio_mismatch'", review_source)
        self.assertIn("const explicitBlocked = new Set", review_source)
        self.assertIn("'not_importable'", review_source)
        self.assertIn("'no_verified_tracks'", review_source)
        self.assertIn("if (item.blocked_reason || explicitBlocked.has(statusKey)) return 'blocked'", review_source)
        self.assertIn("if (preflight && preflight.ok === false) return 'failed'", review_source)
        self.assertIn("function partialFullAlbumMismatch(item: ReviewItem)", review_source)
        self.assertIn("sourceRatio >= 0.8 && matchRatio < 0.25", review_source)
        self.assertIn("function matchReviewNote(item: ReviewItem)", review_source)
        self.assertIn("AcoustID fingerprint mismatch: selected release", review_source)
        self.assertIn("Delete Wrong Audio", review_source)
        self.assertIn("folderEvidence?.guessed_artist", review_source)
        self.assertIn("if (!shouldShowReadyBucket(item, candidateMbid, selectedMatch, previewState)) return false", review_source)
        self.assertIn("if (!shouldShowBlockedBucket(item, candidateMbid, selectedMatch, previewState)) return false", review_source)
        self.assertIn("if (itemMatchBucket(item) !== filter) return false", review_source)
        self.assertIn("const filterCounts = useMemo", review_source)
        self.assertIn("return shouldShowReadyBucket(i, mbids[i.id] ?? initialMbid(i), selectedMatch, previewState)", review_source)
        self.assertIn("blocked,", review_source)
        self.assertIn("audio_mismatch: audioMismatch", review_source)
        self.assertIn("filters.map((entry)", review_source)


    def test_visible_candidates_feed_selected_match_state(self):
        root = Path(__file__).resolve().parents[1]
        review_source = (
            root / "frontend" / "src" / "features" / "importReview" / "ImportReviewPage.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("preflight_status: 'passed' | 'failed' | 'stale' | 'not_run'", review_source)
        self.assertIn("track_mapping: TrackRow[]", review_source)
        self.assertIn("is_importable: boolean", review_source)
        self.assertIn("confidence_score: number | null", review_source)
        self.assertIn("confidence_level: MatchConfidenceLevel", review_source)
        self.assertIn("auto_fix_eligible: boolean", review_source)
        self.assertIn("missing_track_count: number", review_source)
        self.assertIn("function buildCandidateSelectedMatch(", review_source)
        self.assertIn("onSelectMatchRef.current?.(pendingMatch)", review_source)
        self.assertIn("onSelectMatchRef.current?.(selected)", review_source)
        self.assertIn("onUseCandidateRef.current(selected.release_group_id)", review_source)
        self.assertIn("function applyBlockReason(", review_source)
        self.assertIn("Import blocked because the visible match and Release Group ID field are out of sync.", review_source)
        self.assertIn("Once a visible candidate is selected, show that candidate's state instead of stale stored evidence.", review_source)
        self.assertIn("function shouldShowReadyBucket(", review_source)
        self.assertIn("if (shouldShowBlockedBucket(item, mbid, selectedMatch, targetPreviewState)) return false", review_source)
        self.assertIn("if (!selectedMatch || selectedMatch.source === 'manual') return false", review_source)
        self.assertIn("if (!selectedMatch.is_importable) return false", review_source)
        self.assertIn("if (selectedMatch.preflight_status !== 'passed') return false", review_source)
        self.assertIn("if (!preview || !preview.safe) return false", review_source)
        self.assertIn("return selectedCount > 0 && previewCount > 0 && selectedCount === previewCount", review_source)

    def test_selected_match_confidence_controls_auto_fix_eligibility(self):
        root = Path(__file__).resolve().parents[1]
        review_source = (
            root / "frontend" / "src" / "features" / "importReview" / "ImportReviewPage.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("function normalizedScoreValue(value: number | string | undefined): number | null", review_source)
        self.assertIn("if (score >= 0.90) return 'high'", review_source)
        self.assertIn("const AUTO_IMPORT_CONFIDENCE_THRESHOLD = 0.60", review_source)
        self.assertIn("const REVIEW_QUEUE_LIMIT = 5000", review_source)
        self.assertIn("if (score >= AUTO_IMPORT_CONFIDENCE_THRESHOLD) return 'medium'", review_source)
        self.assertIn("const autoFixEligible = isImportable && confidenceScore !== null && confidenceScore >= AUTO_IMPORT_CONFIDENCE_THRESHOLD", review_source)
        self.assertIn("Review required because this match is below the 60% auto-import threshold.", review_source)
        self.assertIn("Auto-import eligible — ${formatPercent(confidenceScore)} confidence.", review_source)
        self.assertIn("const autoFixRequiresReview = false", review_source)
        self.assertIn("const selectedConfidence = selectedMatch && selectedMatch.source !== 'manual'", review_source)
        self.assertIn("selectedConfidenceText || (item.confidence ? `${item.confidence} confidence` : '')", review_source)
        self.assertIn("const selectedMatchActive = Boolean(selectedMatch && selectedMatch.source !== 'manual')", review_source)
        self.assertIn("const blockedActive = shouldShowBlockedBucket(item, mbid, selectedMatch, targetPreviewState)", review_source)
        self.assertIn("const visibleMatchBucket: MatchBucket = audioMismatchActive", review_source)
        self.assertIn(": blockedActive", review_source)
        self.assertIn("const staleStoredReason = selectedMatchActive && /preflight failed|failed tracklist preflight|rejected selected musicbrainz release/i.test(item.reason || '')", review_source)
        self.assertIn("const visibleMatchNote = selectedMatchActive && !audioMismatchActive ? '' : matchNote", review_source)
        self.assertIn("actionLabel(item, selectedMatch, targetPreviewState?.preview)", review_source)
        self.assertIn("Next: {visibleBlockHint}", review_source)

    def test_import_review_target_preview_gates_apply(self):
        root = Path(__file__).resolve().parents[1]
        review_source = (
            root / "frontend" / "src" / "features" / "importReview" / "ImportReviewPage.tsx"
        ).read_text(encoding="utf-8")
        client_source = (root / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
        types_source = (root / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")

        self.assertIn("type TargetPreviewState", review_source)
        self.assertIn("function targetPreviewKey(item: ReviewItem, selectedMatch?: SelectedMatch): string", review_source)
        self.assertIn("function TargetPathPreviewPanel({ state }: { state?: TargetPreviewState })", review_source)
        self.assertIn("function targetPreviewBlockReason(", review_source)
        self.assertIn("Import blocked until the target path preview finishes.", review_source)
        self.assertIn("Import blocked by target path preview:", review_source)
        self.assertIn("previewImportTarget({", review_source)
        self.assertIn("delete next[item.id]", review_source)
        self.assertIn("Target path preview passed.", review_source)
        self.assertIn("Target preview", review_source)
        self.assertIn("Show target details", review_source)
        self.assertIn("Track matches ({trackData.matched_count}/{trackData.mb_track_count})", review_source)
        self.assertIn("Save ID only", review_source)
        self.assertNotIn("Save Release Group ID", review_source)
        self.assertIn("getReviewQueue({ limit: REVIEW_QUEUE_LIMIT, origin_type: sourceFilter })", review_source)

        self.assertIn("export function previewImportTarget", client_source)
        self.assertIn("'/api/folders/import-target-preview'", client_source)
        self.assertIn("export interface ImportTargetPreviewResponse", types_source)
        self.assertIn("next_action?: 'import' | 'verify_or_cleanup_unmatched' | 'resolve_conflict' | 'blocked'", types_source)
        self.assertIn("cleanup_required_count?: number", types_source)
        self.assertIn("rejected_cleanup_count?: number", types_source)
        self.assertIn("album_folder_uses_release_group_id: boolean", types_source)
        self.assertIn("preview.next_action === 'verify_or_cleanup_unmatched'", review_source)
        self.assertIn("Needs cleanup", review_source)
        self.assertIn("Cleanup candidates:", review_source)
        self.assertIn("Rejected cleanup:", review_source)
        self.assertIn("automatic verification", review_source)

    def test_import_review_reconcile_job_endpoint_and_status_unknown(self):
        root = Path(__file__).resolve().parents[1]
        review_source = (
            root / "frontend" / "src" / "features" / "importReview" / "ImportReviewPage.tsx"
        ).read_text(encoding="utf-8")
        client_source = (root / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
        app_source = (root / "app.py").read_text(encoding="utf-8")

        self.assertIn('@app.post("/api/import/reconcile-job")', app_source)
        self.assertIn('def import_reconcile_job():', app_source)
        self.assertIn('"status": "import_job_missing"', app_source)
        self.assertIn('"status": "returned_to_review"', app_source)
        self.assertIn('"status": "likely_completed"', app_source)
        self.assertIn('retryable?: boolean', client_source)
        self.assertIn('handled?: boolean', client_source)
        self.assertIn('"step": "format_policy_handled"', app_source)
        self.assertIn('"handled": True', app_source)
        self.assertIn('_is_music_format_policy_handled_error(last)', app_source)
        self.assertIn('markPolicyHandled', review_source)
        self.assertIn('if (rec.handled)', review_source)
        self.assertIn('if (rec?.handled)', review_source)
        self.assertIn("rec.status === 'returned_to_review' || rec.status === 'not_found' || rec.status === 'import_job_missing' || rec.retryable", review_source)
        self.assertIn("Connection lost — retrying ${consecutiveErrors}/3", review_source)
        self.assertIn("Status unknown — reconciliation needed.", review_source)
        self.assertIn("Job missing — returned to review.", review_source)
        self.assertIn("returned to review", review_source)
        self.assertIn("status unknown", review_source)
        self.assertNotIn("Lost contact with backend — check the Jobs page for live status.", review_source)

    def test_review_queue_uses_lightweight_status_loaders(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "app.py").read_text(encoding="utf-8")

        self.assertIn("def _load_pending_reviews(*, prune_resolved: bool = True) -> list:", app_source)
        self.assertIn("pending_reviews = _load_pending_reviews(prune_resolved=False)", app_source)
        self.assertIn("if prune_resolved:", app_source)
        self.assertIn("def _import_skipped_items(limit: int = 500, *, deep_scan: bool = True,", app_source)
        self.assertIn('skipped_deep_scan = status_filter == "skipped"', app_source)
        self.assertIn("_import_skipped_items(skipped_limit, deep_scan=skipped_deep_scan, max_log_lines=skipped_max_log_lines)", app_source)
    def test_backend_import_target_preview_is_read_only_and_release_group_based(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "app.py").read_text(encoding="utf-8")

        self.assertIn('@app.post("/api/folders/import-target-preview")', app_source)
        self.assertIn("def _build_import_target_preview(payload: Dict[str, Any])", app_source)
        self.assertIn("_DEFAULT_ALBUM_PATH_TEMPLATE", app_source)
        self.assertIn('"release_group_id"', app_source)
        self.assertIn('"album_folder_uses_release_group_id"', app_source)
        self.assertIn('"next_action": next_action', app_source)
        self.assertIn('"verify_or_cleanup_unmatched"', app_source)
        self.assertIn('"cleanup_required_count": cleanup_required_count', app_source)
        self.assertIn('"rejected_cleanup_count": rejected_cleanup_count', app_source)
        self.assertIn('"target_folder_conflict"', app_source)
        self.assertIn('"placeholder_warning_count"', app_source)
        self.assertIn('"release_id_path_warning_count"', app_source)
        preview_block = app_source[
            app_source.index("def _build_import_target_preview"):
            app_source.index("@app.post(\"/api/folders/import-target-preview\")")
        ]
        self.assertNotIn("jobs.start_python", preview_block)
        self.assertNotIn("_beet_run", preview_block)
        self.assertNotIn(".mkdir(", preview_block)
        self.assertNotIn(".rename(", preview_block)
        self.assertNotIn(".unlink(", preview_block)


    def test_blocked_review_items_offer_guarded_file_cleanup(self):
        root = Path(__file__).resolve().parents[1]
        review_source = (
            root / "frontend" / "src" / "features" / "importReview" / "ImportReviewPage.tsx"
        ).read_text(encoding="utf-8")
        client_source = (root / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
        types_source = (root / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")
        app_source = (root / "app.py").read_text(encoding="utf-8")

        self.assertIn("function selectedCleanupSourceFiles", review_source)
        self.assertIn("const CLEANUP_TRACK_STATUSES = new Set<TrackRow['status']>([", review_source)
        self.assertIn("'unmatched_extra'", review_source)
        self.assertIn("'ignored_for_this_import'", review_source)
        self.assertIn("'conflicting'", review_source)
        self.assertIn("'acoustid_verified'", review_source)
        self.assertIn("'verified_match'", review_source)
        self.assertIn("selectedCleanupSourceFiles(selectedMatch, targetPreviewState?.preview)", review_source)
        self.assertIn("onCleanupFiles", review_source)
        self.assertNotIn("Verify with fingerprint", review_source)
        self.assertIn("cleanupPurgeReady", review_source)
        self.assertIn("{cleanupPurgeReady ? 'Purge' : 'Quarantine'} {cleanupFiles.length", review_source)
        self.assertIn("const runCleanupFiles = useCallback", review_source)
        self.assertIn("cleanupReviewFiles({", review_source)
        self.assertIn("action: cleanupAction", review_source)
        self.assertIn("pending_review_removed || result.remaining_audio_count === 0", review_source)
        self.assertIn("shown · {filterCounts[filter]} in filter · {items.length} loaded of {filterCounts.all} total", review_source)

        self.assertIn("export function cleanupReviewFiles", client_source)
        self.assertIn("'/api/import/review-files/cleanup'", client_source)
        self.assertIn("ImportReviewFileCleanupPayload", client_source)
        self.assertIn("export interface ImportReviewFileCleanupPayload", types_source)
        self.assertIn("export interface ImportReviewFileCleanupResponse", types_source)
        self.assertIn("quarantine_rejected", types_source)
        self.assertIn("pending_review_removed: boolean", types_source)

        self.assertIn('@app.post("/api/import/review-files/cleanup")', app_source)
        self.assertIn("def cleanup_import_review_files():", app_source)
        self.assertIn("_pending_review_matches(folder_path, review_item_id)", app_source)
        self.assertIn("outside_review_folder", app_source)
        self.assertIn("resolved.suffix.lower() not in AUDIO_EXT", app_source)
        self.assertIn("IMPORT_REVIEW_QUARANTINE_DIR", app_source)
        self.assertIn("Permanent delete requires allow_delete=true", app_source)
        self.assertIn("_remove_pending_review_for_path(folder_path, log)", app_source)
        self.assertIn("_mark_pending_review_status(", app_source)
        self.assertIn("_record_ai_review_decision(", app_source)

    def test_small_rejected_cleanup_batches_auto_quarantine(self):
        root = Path(__file__).resolve().parents[1]
        review_source = (
            root / "frontend" / "src" / "features" / "importReview" / "ImportReviewPage.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("const AUTO_QUARANTINE_REJECTED_MAX_FILES = 5", review_source)
        self.assertIn("const autoCleanupKeysRef = useRef<Set<string>>(new Set())", review_source)
        self.assertIn("function persistSubmittedItemIds", review_source)
        self.assertIn("restoredStaleSubmittedIds", review_source)
        self.assertIn("hiddenByActiveJob", review_source)
        self.assertIn("activeJobHiddenCount", review_source)
        self.assertIn("files.length < 1 || files.length >= AUTO_QUARANTINE_REJECTED_MAX_FILES", review_source)
        self.assertIn("const preview = previewState?.status === 'ready' ? previewState.preview : undefined", review_source)
        self.assertIn("selectedCleanupSourceFiles(selectedMatch, preview)", review_source)
        self.assertIn("autoCleanupKeysRef.current.has(key)", review_source)
        self.assertIn("Auto-quarantining", review_source)
        auto_cleanup_pos = review_source.index("Auto-quarantining")
        auto_cleanup_region = review_source[auto_cleanup_pos - 900:auto_cleanup_pos + 1800]
        self.assertNotIn("submittedItemIdsRef.current.has(item.id)", auto_cleanup_region)
        self.assertIn("hasDestructiveCleanupMismatch", review_source)
        self.assertIn("delete_rejected", review_source)
        self.assertIn("allow_delete: destructive", review_source)
        self.assertIn("Auto-purging", review_source)
        self.assertIn("Auto-quarantined", review_source)
        self.assertIn("action: cleanupAction", review_source)
        self.assertIn("pending_review_removed || result.remaining_audio_count === 0", review_source)
        self.assertIn("autoCleanupKeysRef.current.delete(key)", review_source)

    def test_saved_revalidated_track_mapping_hydrates_selected_match(self):
        root = Path(__file__).resolve().parents[1]
        review_source = (
            root / "frontend" / "src" / "features" / "importReview" / "ImportReviewPage.tsx"
        ).read_text(encoding="utf-8")
        types_source = (root / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")

        self.assertIn("track_mapping?: ImportWithIdPayload['track_mapping']", types_source)
        self.assertIn("track_match_count?: number | null", types_source)
        self.assertIn("function savedSelectedMatch(item: ReviewItem): SelectedMatch | null", review_source)
        self.assertIn("if (!suggestion?.track_mapping?.length) return null", review_source)
        self.assertIn("const saved = savedSelectedMatch(item)", review_source)
        self.assertIn("if (saved) next[item.id] = saved", review_source)
        self.assertIn("const mapping = ((suggestion.track_mapping ?? []) as TrackRow[]).filter(Boolean)", review_source)
        self.assertIn("const isImportable = identityValidated && isReleaseGroupUsable && hasRepresentativeRelease && importableTrackCount > 0 && status === 'passed'", review_source)
    def test_background_import_strip_uses_clear_queue(self):
        root = Path(__file__).resolve().parents[1]
        review_source = (
            root / "frontend" / "src" / "features" / "importReview" / "ImportReviewPage.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("onClearQueue", review_source)
        self.assertIn("Clear queue", review_source)
        self.assertIn("Reconcile all", review_source)
        self.assertIn("onReconcileAll", review_source)
        self.assertNotIn("Refresh queue", review_source)
        self.assertIn("const clearable = list.filter((j) => !isActiveBgJob(j)).length", review_source)
        self.assertIn("if (isActiveBgJob(job)) next[itemId] = job", review_source)
        self.assertIn("else submittedItemIdsRef.current.delete(itemId)", review_source)
        self.assertIn("status: 'status_unknown'", review_source)
        self.assertIn("'import_job_missing'", review_source)
        self.assertIn("'returned_to_review'", review_source)
        self.assertIn("job.status === 'returned_to_review' && liveIds.has(id)", review_source)

    def test_auto_enqueue_failed_is_failed_not_ready(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "app.py").read_text(encoding="utf-8")
        body = app_source[app_source.index('def _review_queue_status_matches'):app_source.index('def _review_row_has_evidence')]
        self.assertIn('"auto_enqueue_failed"', body)
        self.assertIn('"format_policy_rejected"', body)
        self.assertIn('not_ready_statuses', body)
        self.assertIn('status_key not in not_ready_statuses', body)

    def test_frontend_auto_enqueue_failed_bucket_is_failed(self):
        root = Path(__file__).resolve().parents[1]
        review_source = (
            root / "frontend" / "src" / "features" / "importReview" / "ImportReviewPage.tsx"
        ).read_text(encoding="utf-8")
        body = review_source[review_source.index('function itemMatchBucket'):review_source.index('function partialFullAlbumMismatch')]
        self.assertIn('const explicitFailed = new Set', body)
        self.assertIn("'auto_enqueue_failed'", body)
        self.assertIn("if (explicitFailed.has(statusKey)) return 'failed';", body)
        failed_block = body[body.index('const explicitFailed'):body.index('const explicitBlocked')]
        blocked_block = body[body.index('const explicitBlocked'):body.index("if (item.blocked_reason")]
        self.assertNotIn("'format_policy_rejected'", failed_block)
        self.assertIn("'format_policy_rejected'", blocked_block)
if __name__ == "__main__":
    unittest.main()





