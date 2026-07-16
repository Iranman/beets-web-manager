import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
PAGE_SOURCE = (ROOT / "frontend" / "src" / "views" / "Playlists.tsx").read_text(encoding="utf-8")
TYPES_SOURCE = (ROOT / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")


class PlaylistSavedDiscoveryTests(unittest.TestCase):
    def test_manifest_only_playlists_are_discovered(self):
        self.assertIn('def _playlist_saved_playlist_records', APP_SOURCE)
        self.assertIn('PLAYLIST_DIR.glob("*.playlist.json")', APP_SOURCE)
        self.assertIn('PLAYLIST_DIR.glob("*.m3u")', APP_SOURCE)
        self.assertIn('"has_manifest": False', APP_SOURCE)
        self.assertIn('"has_manifest"', APP_SOURCE)
        self.assertIn("'Manifest only'", PAGE_SOURCE)
        self.assertIn('has_manifest?: boolean', TYPES_SOURCE)

    def test_m3u_and_manifest_merge_into_one_saved_row(self):
        self.assertIn('records: Dict[str, Dict[str, Any]] = {}', APP_SOURCE)
        self.assertIn('key = _norm(clean_name)', APP_SOURCE)
        self.assertIn('duplicate merged', APP_SOURCE)
        self.assertIn('has_m3u?: boolean', TYPES_SOURCE)
        self.assertIn('playlistFileBadge(playlist)', PAGE_SOURCE)

    def test_checkpoint_only_rows_are_visible_and_resumable(self):
        self.assertIn('def _playlist_job_state_name', APP_SOURCE)
        self.assertIn('state.get("playlist_name")', APP_SOURCE)
        self.assertIn('"has_checkpoint"', APP_SOURCE)
        self.assertIn('checkpoint orphaned', APP_SOURCE)
        self.assertIn('Checkpoint only', PAGE_SOURCE)
        self.assertIn('fallbackResumablePlaylist', PAGE_SOURCE)
        self.assertIn("handleSavedPlaylistPipelineAction(activePlaylist, 'resume')", PAGE_SOURCE)

    def test_manifest_or_checkpoint_is_valid_for_details_and_pipeline(self):
        self.assertIn('def _playlist_saved_playlist_exists', APP_SOURCE)
        for marker in (
            'def playlist_tracks_detail',
            'def playlist_pipeline_action',
            'def playlist_suggestions',
            'def playlist_apply_safe_suggestions',
            'def playlist_track_action',
        ):
            start = APP_SOURCE.index(marker)
            region = APP_SOURCE[start:start + 500]
            self.assertIn('_playlist_saved_playlist_exists(clean_name)', region)

    def test_checkpoint_only_summary_avoids_library_index(self):
        self.assertIn('def _playlist_checkpoint_list_summary', APP_SOURCE)
        region = APP_SOURCE[
            APP_SOURCE.index('def _playlist_m3u_summary('):
            APP_SOURCE.index('def _playlist_manifest_name_from_file(')
        ]
        self.assertIn('checkpoint_summary = _playlist_checkpoint_list_summary', region)
        self.assertLess(
            region.index('checkpoint_summary = _playlist_checkpoint_list_summary'),
            region.index('index = index or _playlist_library_index()'),
        )
        self.assertIn('"desired_source": "checkpoint"', APP_SOURCE)
        self.assertIn('"duration_ms": duration_ms', APP_SOURCE)
    def test_resume_pipeline_is_primary_for_interrupted_rows(self):
        row_actions = PAGE_SOURCE[PAGE_SOURCE.index('const savedPlaylistRowActions'):PAGE_SOURCE.index('return (', PAGE_SOURCE.index('const savedPlaylistRowActions'))]
        self.assertLess(row_actions.index("label: 'Resume Pipeline'"), row_actions.index("label: 'Run Pipeline'"))
        self.assertIn("label: 'Clear Checkpoint'", row_actions)
        self.assertIn('Resume Pipeline', PAGE_SOURCE)
        self.assertNotIn('>\n                          Resume\n                        </Button>', PAGE_SOURCE)

    def test_saved_playlist_filters_and_metrics_are_visible(self):
        self.assertIn('type SavedPlaylistFilter', PAGE_SOURCE)
        self.assertIn('SAVED_PLAYLIST_FILTERS', PAGE_SOURCE)
        self.assertIn('savedPlaylistMetrics', PAGE_SOURCE)
        self.assertIn('filteredSavedPlaylistRows', PAGE_SOURCE)
        self.assertIn('Find playlist', PAGE_SOURCE)
        self.assertIn('No saved playlists match the current filters.', PAGE_SOURCE)

    def test_saved_playlist_rows_show_next_step_and_activity_context(self):
        self.assertIn('function SavedPlaylistRowContext', PAGE_SOURCE)
        self.assertIn('Next step:', PAGE_SOURCE)
        self.assertIn('Last activity:', PAGE_SOURCE)
        self.assertIn('Issue: {summary.lastError}', PAGE_SOURCE)
        self.assertIn('<SavedPlaylistRowContext summary={summary} />', PAGE_SOURCE)


if __name__ == "__main__":
    unittest.main()

