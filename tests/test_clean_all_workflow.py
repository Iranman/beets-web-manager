import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
JOBS_SOURCE = (ROOT / "frontend" / "src" / "views" / "Jobs.tsx").read_text(encoding="utf-8")
CLIENT_SOURCE = (ROOT / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
IMPORT_REVIEW_SOURCE = (ROOT / "frontend" / "src" / "features" / "importReview" / "ImportReviewPage.tsx").read_text(encoding="utf-8")


class CleanAllWorkflowStaticTests(unittest.TestCase):
    def test_clean_all_reuses_existing_job_framework(self):
        self.assertIn('@app.post("/api/jobs/maintenance-runner")', APP_SOURCE)
        self.assertIn('metadata={"type": "maintenance-runner", "workflow": "clean-all"', APP_SOURCE)
        self.assertIn('label="Clean All"', APP_SOURCE)
        self.assertIn('export function startCleanAll(', CLIENT_SOURCE)
        self.assertIn('return startMaintenanceRunner(options);', CLIENT_SOURCE)
        self.assertIn('await startCleanAll(options)', JOBS_SOURCE)

    def test_clean_all_progress_has_pipeline_counts_and_heartbeat(self):
        for step in [
            'Scanning',
            'Fingerprinting',
            'Matching',
            'Verifying',
            'Repairing',
            'Replacing',
            'Organizing',
            'Syncing',
        ]:
            self.assertIn(step, APP_SOURCE)
            self.assertIn(step, JOBS_SOURCE)
        for key in [
            'scanned',
            'verified',
            'fixed',
            'replaced',
            'removed',
            'needs_submission',
            'needs_review',
            'failed',
        ]:
            self.assertIn(key, APP_SOURCE)
            self.assertIn(key, JOBS_SOURCE)
        self.assertIn('last_heartbeat_at', APP_SOURCE)
        self.assertIn('Last heartbeat', JOBS_SOURCE)

    def test_main_library_cleanup_surface_has_single_clean_all_action(self):
        self.assertIn('One coordinated cleanup workflow', JOBS_SOURCE)
        self.assertIn('Clean All', JOBS_SOURCE)
        self.assertIn('Submission Queue', JOBS_SOURCE)
        self.assertIn('Cleanup Report', JOBS_SOURCE)
        self.assertIn('View Progress', JOBS_SOURCE)
        self.assertNotIn('Scan Library Cleanup', JOBS_SOURCE)
        self.assertNotIn('Run Now', JOBS_SOURCE)

    def test_clean_all_runs_identity_merge_and_duplicate_phases(self):
        self.assertIn('{"id": "artist_folder_merge", "label": "Artist Folder Merge"}', APP_SOURCE)
        self.assertIn('{"id": "release_group_merge", "label": "Release Group Merge"}', APP_SOURCE)
        self.assertIn('{"id": "duplicates", "label": "Duplicate Track Scan"}', APP_SOURCE)
        self.assertIn('{"id": "final_verification", "label": "Final Verification"}', APP_SOURCE)
        self.assertIn('"artist_folder_merge": "Organizing"', APP_SOURCE)
        self.assertIn('"release_group_merge": "Organizing"', APP_SOURCE)
        self.assertIn('"duplicates": "Fingerprinting"', APP_SOURCE)
        self.assertIn('"final_verification": "Verifying"', APP_SOURCE)
        self.assertIn('clean_artist_folders_stamp_mbid()', APP_SOURCE)
        self.assertIn('json={"root": str(MUSIC_ROOT), "dry_run": False, "compact_log": True}', APP_SOURCE)
        self.assertIn('_maintenance_release_group_merge(', APP_SOURCE)
        self.assertIn('_maintenance_full_duplicate_scan(', APP_SOURCE)
        self.assertIn('_maintenance_final_verification(', APP_SOURCE)
        self.assertIn('"stamp-mbid-folders"', APP_SOURCE)
        self.assertIn("{ id: 'artist_folder_merge', label: 'Artist Folder Merge'", JOBS_SOURCE)
        self.assertIn("{ id: 'release_group_merge', label: 'Release Group Merge'", JOBS_SOURCE)
        self.assertIn("{ id: 'duplicates', label: 'Duplicate Track Scan'", JOBS_SOURCE)
        self.assertIn("{ id: 'final_verification', label: 'Final Verification'", JOBS_SOURCE)

    def test_clean_all_button_resumes_incomplete_checkpoint(self):
        self.assertIn('def _maintenance_resume_from_report', APP_SOURCE)
        self.assertIn('def _maintenance_resume_summary', APP_SOURCE)
        self.assertIn('persist_checkpoint("failed", failed_message)', APP_SOURCE)
        self.assertIn('skip_completed_task("artist_folder_merge")', APP_SOURCE)
        self.assertIn('skip_completed_task("release_group_merge")', APP_SOURCE)
        self.assertIn('skip_completed_task("duplicates")', APP_SOURCE)
        self.assertIn('"resumed": resume_requested', APP_SOURCE)
        self.assertIn('"results": results', APP_SOURCE)
        self.assertIn('Resume Clean All', JOBS_SOURCE)
        self.assertIn('Resume checkpoint ready', JOBS_SOURCE)
        self.assertIn('checkpointResumable', JOBS_SOURCE)
        self.assertIn("raw === 'partial'", JOBS_SOURCE)

    def test_clean_all_partial_failure_preserves_completed_work(self):
        self.assertIn('def finish_partial(failed_message: str)', APP_SOURCE)
        self.assertIn('persist_checkpoint("partial", failed_message)', APP_SOURCE)
        self.assertIn('return finish_partial(failed_message)', APP_SOURCE)
        self.assertIn('"maintenance_status": "partial"', APP_SOURCE)
        self.assertIn('"partial": True', APP_SOURCE)
        self.assertIn("status === 'Partial'", JOBS_SOURCE)

    def test_clean_all_missing_files_are_reconciled_before_duplicate_scan(self):
        self.assertIn('def _maintenance_remove_missing_file_rows', APP_SOURCE)
        self.assertIn('_maintenance_remove_missing_file_rows(health, log)', APP_SOURCE)
        self.assertIn('trigger_plex=False', APP_SOURCE)
        self.assertIn('"removed_db_rows": removed', APP_SOURCE)
        start = APP_SOURCE.index('@app.post("/api/jobs/maintenance-runner")')
        end = APP_SOURCE.index('def _folder_cleanup_path', start)
        runner_source = APP_SOURCE[start:end]
        self.assertLess(
            runner_source.index('_maintenance_remove_missing_file_rows(health, log)'),
            runner_source.index('_maintenance_full_duplicate_scan('),
        )

    def test_submission_queue_reuses_import_review_and_mbsubmit(self):
        self.assertIn('href="/import?tab=review"', JOBS_SOURCE)
        self.assertIn('@app.post("/api/albums/<int:aid>/mbsubmit")', APP_SOURCE)
        self.assertIn('@app.post("/api/items/<int:iid>/mbsubmit")', APP_SOURCE)
        self.assertIn('Generate MusicBrainz submission text for this album', IMPORT_REVIEW_SOURCE)
        self.assertIn('submittedItemIdsRef', IMPORT_REVIEW_SOURCE)

    def test_replacement_and_identity_guardrails_remain_fingerprint_based(self):
        self.assertIn('def _validate_wanted_download_identity_before_import', APP_SOURCE)
        self.assertIn('Downloaded audio did not fingerprint-verify', APP_SOURCE)
        self.assertIn('_acoustid_fingerprint_match(str(original_abs), str(final_path))', APP_SOURCE)
        self.assertIn('_acoustid_fingerprint_ids(str(final_path))', APP_SOURCE)
        self.assertIn('fingerprint_validation', APP_SOURCE)


if __name__ == "__main__":
    unittest.main()
