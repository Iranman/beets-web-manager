import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
CLIENT_SOURCE = (ROOT / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
CONFIG_SOURCE = (ROOT / "frontend" / "src" / "views" / "Config.tsx").read_text(encoding="utf-8")


class MusicFormatPreferenceIntegrationTests(unittest.TestCase):
    def test_backend_exposes_settings_scan_and_replace_routes(self):
        self.assertIn('@app.get("/api/settings/music-format")', APP_SOURCE)
        self.assertIn('@app.post("/api/settings/music-format")', APP_SOURCE)
        self.assertIn('@app.post("/api/library/music-format/scan")', APP_SOURCE)
        self.assertIn('@app.post("/api/library/music-format/replace")', APP_SOURCE)
        self.assertIn('metadata={"type": "music-format-scan"', APP_SOURCE)
        self.assertIn('metadata={"type": "music-format-replace"', APP_SOURCE)
        self.assertIn('Music format preference scan', APP_SOURCE)
        self.assertIn('Music format replacement retry', APP_SOURCE)

    def test_import_paths_call_central_audio_validation(self):
        self.assertIn('from backend.audio_preferences import', APP_SOURCE)
        self.assertIn('def _validate_import_source_audio(', APP_SOURCE)
        self.assertGreaterEqual(APP_SOURCE.count('_validate_import_source_audio('), 7)
        self.assertIn('_validate_import_source_audio(import_dir, log, reject_downloads=True)', APP_SOURCE)
        self.assertIn('_validate_import_source_audio(aldir, log, reject_downloads=True)', APP_SOURCE)
        self.assertIn('_validate_import_source_audio(folder_path, log, reject_downloads=True)', APP_SOURCE)
        self.assertIn('_validate_import_source_audio(str(batch_dir), log, reject_downloads=True)', APP_SOURCE)
        self.assertIn('_playlist_run_import_downloaded(name, state["log"], cancel_event=cancel_event)', APP_SOURCE)
        self.assertIn('batch_dir = _playlist_imports_dir(clean_name)', APP_SOURCE)
        self.assertNotIn('_validate_import_source_audio(str(round_dl_dir), state["log"], reject_downloads=True)', APP_SOURCE)

    def test_partial_import_subset_validates_selected_files_before_staging(self):
        route = APP_SOURCE[
            APP_SOURCE.index('@app.post("/api/folders/import-with-id")'):
            APP_SOURCE.index('def _match_tracks_from_mb')
        ]
        validate_pos = route.index('active_selected_source_files = _filter_import_review_selected_audio_files(')
        stage_pos = route.index('import_folder_path = _stage_selected_audio_files(')
        self.assertLess(validate_pos, stage_pos)
        self.assertIn('active_selected_source_files,', route[stage_pos:stage_pos + 300])
        self.assertIn('for src in active_selected_source_files:', route)
        self.assertIn('Selected partial-import files passed pre-stage audio validation.', route)

    def test_partial_import_subset_deferred_inspection_keeps_source_for_review(self):
        helper = APP_SOURCE[
            APP_SOURCE.index('def _filter_import_review_selected_audio_files'):
            APP_SOURCE.index('def _validate_import_source_audio')
        ]
        self.assertIn('def _audio_validation_inspection_failed(', APP_SOURCE)
        self.assertIn('_audio_validation_inspection_failed(row)', helper)
        self.assertIn('Inspection deferred; source kept in review', helper)
        self.assertIn('deferred.append(path)', helper)
        self.assertIn('Files were kept for retry/review.', helper)
        self.assertIn('Continuing partial import with', helper)
        defer_pos = helper.index('_audio_validation_inspection_failed(row)')
        reject_pos = helper.index('_handle_rejected_audio_download')
        self.assertLess(defer_pos, reject_pos)

    def test_rejected_downloads_use_saved_handling_but_library_rows_are_kept(self):
        self.assertIn('_handle_rejected_audio_download(row["path"], prefs, log=log)', APP_SOURCE)
        self.assertIn('_MUSIC_FORMAT_POLICY_HANDLED_MESSAGE', APP_SOURCE)
        self.assertIn('_music_format_policy_rejection_error', APP_SOURCE)
        self.assertIn('Downloaded audio does not match Music Format Preferences. ', APP_SOURCE)
        self.assertIn('Import stopped; choose another source or update Music Format Preferences before retrying.', APP_SOURCE)
        self.assertIn('handled_results.append(_handle_rejected_audio_download', APP_SOURCE)
        self.assertIn('_is_music_format_policy_handled_error', APP_SOURCE)
        self.assertIn('def _finalize_pending_review_format_policy_rejection', APP_SOURCE)
        self.assertIn('_MUSIC_FORMAT_POLICY_REVIEW_STATUS = "format_policy_rejected"', APP_SOURCE)
        self.assertIn('Existing library audio does not match Music Format Preferences', APP_SOURCE)
        self.assertIn('Current files were kept and marked Needs replacement', APP_SOURCE)
        self.assertIn('No replacement found: keeping current file and marking Needs replacement', APP_SOURCE)
        self.assertIn('Queued retry: no compliant source available', APP_SOURCE)
        scan_source = APP_SOURCE[APP_SOURCE.index('def _music_format_scan_library('):APP_SOURCE.index('@app.post("/api/library/music-format/scan")')]
        self.assertNotIn('unlink(', scan_source)
        self.assertNotIn('remove(', scan_source)
        self.assertNotIn('rmtree(', scan_source)

    def test_replacement_retry_verifies_before_original_removal(self):
        self.assertIn('replace_existing_item_ids', APP_SOURCE)
        self.assertIn('Replacement mode', APP_SOURCE)
        self.assertIn('forced_replace_ids', APP_SOURCE)
        self.assertNotIn('replace_rows.extend(forced_replace_rows)', APP_SOURCE)
        self.assertIn('replacement failed verification', APP_SOURCE)
        self.assertIn('Replacement found: imported', APP_SOURCE)
        self.assertIn('Original removed after verified replacement', APP_SOURCE)
        self.assertIn('DELETE FROM items WHERE id=?', APP_SOURCE)
        replace_source = APP_SOURCE[APP_SOURCE.index('def _music_format_replace_rows('):APP_SOURCE.index('@app.post("/api/library/music-format/replace")')]
        verify_index = replace_source.index('replacement = _music_format_find_verified_replacement(resolved, prefs)')
        remove_index = replace_source.index('_music_format_remove_original_after_replacement(')
        self.assertLess(verify_index, remove_index)

    def test_frontend_settings_controls_are_present(self):
        self.assertIn('getMusicFormatPreferences', CLIENT_SOURCE)
        self.assertIn('saveMusicFormatPreferences', CLIENT_SOURCE)
        self.assertIn('startMusicFormatScan', CLIENT_SOURCE)
        self.assertIn('startMusicFormatReplacement', CLIENT_SOURCE)
        self.assertIn('Music Format Preferences', CONFIG_SOURCE)
        self.assertIn('Downloaded tracks are inspected before import.', CONFIG_SOURCE)
        self.assertIn('Allow Atmos audio', CONFIG_SOURCE)
        self.assertIn('Reject Atmos audio', CONFIG_SOURCE)
        self.assertIn('Replace queued', CONFIG_SOURCE)
        self.assertIn('Needs replacement', CONFIG_SOURCE)


if __name__ == "__main__":
    unittest.main()

