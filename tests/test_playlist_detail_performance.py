import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
PAGE_SOURCE = (ROOT / "frontend" / "src" / "views" / "Playlists.tsx").read_text(encoding="utf-8")
CLIENT_SOURCE = (ROOT / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
TYPES_SOURCE = (ROOT / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")
HOOKS_SOURCE = (ROOT / "frontend" / "src" / "lib" / "hooks.ts").read_text(encoding="utf-8")


class PlaylistDetailPerformanceTests(unittest.TestCase):
    def test_playlist_tracks_endpoint_has_summary_and_rows_mode(self):
        route = APP_SOURCE[
            APP_SOURCE.index('@app.get("/api/playlists/<path:name>/tracks")'):
            APP_SOURCE.index('def _playlist_record_pipeline(')
        ]
        self.assertIn('mode = _s(request.args.get("mode") or "full")', route)
        self.assertIn('_playlist_detail_summary_payload(clean_name)', route)
        self.assertIn('_playlist_rows_page_payload(clean_name)', route)
        self.assertIn('_playlist_detail_payload(clean_name)', route)
        self.assertIn('"duration_ms"', route)

    def test_summary_payload_does_not_build_library_index(self):
        region = APP_SOURCE[
            APP_SOURCE.index('def _playlist_detail_summary_payload('):
            APP_SOURCE.index('def _playlist_m3u_reference_track_rows(')
        ]
        self.assertNotIn('_playlist_library_index()', region)
        self.assertIn('_playlist_manifest_list_summary(clean_name, manifest)', region)
        self.assertIn('"tracks_loaded": False', region)
        self.assertIn('"detail_mode": "summary"', region)

    def test_rows_payload_is_paged_and_partial(self):
        region = APP_SOURCE[
            APP_SOURCE.index('def _playlist_rows_page_payload('):
            APP_SOURCE.index('def _playlist_detail_payload(')
        ]
        self.assertIn('limit = _playlist_int_request_arg("limit", 100, low=1, high=250)', region)
        self.assertIn('_playlist_matched_rows_page(clean_name, group, offset, limit)', region)
        self.assertIn('"detail_mode": "rows"', region)
        self.assertIn('"partial_tracks_loaded": True', region)
        self.assertIn('"has_more": has_more', region)
        self.assertIn('"rows": rows', region)

    def test_saved_playlist_view_loads_summary_then_paged_rows(self):
        self.assertIn("options?: { mode?: 'summary' | 'full' }", CLIENT_SOURCE)
        self.assertIn("?mode=${encodeURIComponent(options.mode)}", CLIENT_SOURCE)
        self.assertIn('export function getPlaylistRows(', CLIENT_SOURCE)
        self.assertIn("const qs = new URLSearchParams({ mode: 'rows' });", CLIENT_SOURCE)
        self.assertIn("getPlaylistDetails(playlist.name, { mode: 'summary' })", PAGE_SOURCE)
        self.assertIn('handleLoadPlaylistRows', PAGE_SOURCE)
        self.assertIn('getPlaylistRows(savedPlaylistName, {', PAGE_SOURCE)
        self.assertNotIn('handleLoadFullPlaylistDetails', PAGE_SOURCE)
        self.assertNotIn("getPlaylistDetails(savedPlaylistName, { mode: 'full' })", PAGE_SOURCE)
        self.assertIn('tracks_loaded?: boolean', TYPES_SOURCE)
        self.assertIn('partial_tracks_loaded?: boolean', TYPES_SOURCE)
        self.assertIn('const partialDetailRows = Boolean(savedDetail?.partial_tracks_loaded)', PAGE_SOURCE)
        self.assertIn('const loadedRowsAreComplete = savedDetailRowsLoaded && !partialDetailRows', PAGE_SOURCE)
        self.assertIn("detail_mode?: 'summary' | 'full' | 'rows' | string", TYPES_SOURCE)
        self.assertIn('export interface PlaylistRowsResponse', TYPES_SOURCE)

    def test_playlist_polling_uses_bounded_intervals_and_summary_refreshes(self):
        self.assertIn('PLAYLIST_SYNC_STATUS_POLL_MS = 60_000', PAGE_SOURCE)
        self.assertIn('PLAYLIST_DOWNLOAD_POLL_MS = 3_000', PAGE_SOURCE)
        self.assertIn('PLAYLIST_PIPELINE_JOB_POLL_MS = 5_000', PAGE_SOURCE)
        self.assertIn('PLAYLIST_HIDDEN_POLL_MS = 15_000', PAGE_SOURCE)
        self.assertIn('playlistPollDelay(PLAYLIST_PIPELINE_JOB_POLL_MS)', PAGE_SOURCE)
        self.assertNotIn('window.setTimeout(poll, 1500)', PAGE_SOURCE)
        self.assertNotIn('window.setTimeout(poll, 2000)', PAGE_SOURCE)
        self.assertNotIn('getPlaylistDetails(playlist.name);', PAGE_SOURCE)
        self.assertIn("getPlaylistDetails(playlist.name, { mode: 'summary' })", PAGE_SOURCE)
        self.assertIn('JOB_POLL_MS = 3_000', HOOKS_SOURCE)
        self.assertIn('GLOBAL_JOBS_RUNNING_POLL_MS = 15_000', HOOKS_SOURCE)
        self.assertIn('GLOBAL_JOBS_FAILED_POLL_MS = 60_000', HOOKS_SOURCE)
        self.assertIn("refetchInterval: (q) => (q.state.data?.status === 'running' ? JOB_POLL_MS : false)", HOOKS_SOURCE)
        self.assertNotIn('setTimeout(resolve, 1500)', HOOKS_SOURCE)
        self.assertNotIn('running > 0 ? 5_000', HOOKS_SOURCE)


if __name__ == "__main__":
    unittest.main()