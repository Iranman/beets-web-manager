"""Tests for surfacing singleton items (already-imported tracks with no
album row, album_id NULL) in the Import Review "Needs MB ID" queue.

Root cause of the reported symptom ("library shows more items for review
than the actual review page"): /api/library counted 620 albums as
"Missing MusicBrainz ID", but every single one turned out (verified live)
to be a singleton whose one track was already fully imported -- not an
unimported disk folder. The existing library_no_mb query in
import_review_queue() only scans the `albums` table, which structurally
can never contain a singleton (no album row exists), so all 620 were
permanently invisible in Import Review. Fixed by adding a second query for
singleton items, reusing the same "library_no_mb" type (so existing
counts/filters pick them up automatically) with a target_kind="item"
marker for correct labeling/actions, plus a new
/api/items/<iid>/attach-recording endpoint (mirroring album_add_mbids's
direct modify+write+move pattern) to actually resolve them, wired to the
already-existing but previously-unused /api/items/<iid>/ai-suggest.
"""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
IMPORT_REVIEW_SOURCE = (ROOT / "frontend" / "src" / "features" / "importReview" / "ImportReviewPage.tsx").read_text(encoding="utf-8")
CLIENT_SOURCE = (ROOT / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
TYPES_SOURCE = (ROOT / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")


def _function_source(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


class SingletonReviewQueryTests(unittest.TestCase):
    def setUp(self):
        self._fn = _function_source(
            APP_SOURCE, "def import_review_queue():", "@app.post(\"/api/import/cleanup-stale\")"
        )

    def test_queries_items_with_no_album_row(self):
        self.assertIn("WHERE (items.album_id IS NULL OR items.album_id = 0)", self._fn)
        self.assertIn("AND COALESCE(items.mb_trackid, '') = ''", self._fn)

    def test_reuses_library_no_mb_type_so_existing_filters_pick_it_up(self):
        # Window sized generously above the current block length rather than
        # tightly, since this block has already grown once (extra evidence
        # fields added after the initial singleton-review-queue feature) and
        # a too-tight window silently breaks again on the next addition.
        block_start = self._fn.index("Singleton items (already imported")
        block = self._fn[block_start:block_start + 4000]
        self.assertIn('"type": "library_no_mb"', block)
        self.assertIn('"target_kind": "item"', block)

    def test_uses_text_factory_bytes(self):
        block_start = self._fn.index("Singleton items (already imported")
        block = self._fn[block_start:block_start + 1200]
        self.assertIn("with _db(text_factory=bytes, row_factory=sqlite3.Row) as con:", block)

    def test_still_respects_music_root_path_guard(self):
        block_start = self._fn.index("Singleton items (already imported")
        block = self._fn[block_start:block_start + 2200]
        self.assertIn("_is_music_root_path(item_path)", block)

    def test_query_scoped_by_the_same_limit_param_as_the_album_query(self):
        block_start = self._fn.index("Singleton items (already imported")
        block = self._fn[block_start:block_start + 1400]
        self.assertIn("(limit,)", block)


class AttachRecordingEndpointTests(unittest.TestCase):
    def setUp(self):
        self._fn = _function_source(
            APP_SOURCE, 'def item_attach_recording(iid: int):', "def _item_ai_abs_path("
        )

    def test_route_registered(self):
        self.assertIn('@app.post("/api/items/<int:iid>/attach-recording")', APP_SOURCE)

    def test_validates_mb_trackid_is_a_real_uuid(self):
        self.assertIn("if not _MB_UUID_RE.match(mb_trackid):", self._fn)

    def test_mirrors_album_add_mbids_direct_modify_write_move_pattern(self):
        self.assertIn('"modify", "--yes", "--nowrite", f"mb_trackid={mb_trackid}"', self._fn)
        self.assertIn('["mbsync", query]', self._fn)
        self.assertIn('["write", "--yes", query]', self._fn)
        self.assertIn('["move", query]', self._fn)

    def test_runs_as_a_background_job_not_inline(self):
        self.assertIn("jobs.start_python(_do,", self._fn)

    def test_invalidates_library_cache_after_success(self):
        self.assertIn("_invalidate_lib_cache()", self._fn)


class FrontendSingletonWiringTests(unittest.TestCase):
    def test_client_wrappers_present(self):
        self.assertIn("export function suggestItem(itemId: number)", CLIENT_SOURCE)
        # attachRecording grew an options param (confirmed_conflicts,
        # candidate) to support the recording-candidate-evidence feature --
        # check the signature start rather than the full original one-liner.
        self.assertIn("export function attachRecording(", CLIENT_SOURCE)
        self.assertIn("itemId: number,", CLIENT_SOURCE)
        self.assertIn("mbTrackId: string,", CLIENT_SOURCE)
        self.assertIn("/api/items/${itemId}/attach-recording", CLIENT_SOURCE)

    def test_review_item_type_carries_target_kind(self):
        self.assertIn("target_kind?: 'album' | 'item';", TYPES_SOURCE)

    def test_suggest_handler_routes_item_rows_to_item_level_endpoint(self):
        fn = _function_source(IMPORT_REVIEW_SOURCE, "const handleSuggest = useCallback(", "const startApply = useCallback(")
        self.assertIn("await suggestItem(item.item_id)", fn)

    def test_apply_handler_routes_item_rows_to_attach_recording(self):
        fn = _function_source(IMPORT_REVIEW_SOURCE, "const runApply = useCallback(", "const requestDismiss = useCallback(")
        self.assertIn("started = await attachRecording(item.item_id, representativeId);", fn)
        # Must be checked before the plain album branch, not after, so a
        # target_kind="item" row can't fall through to matchAlbum(0, ...).
        item_branch = fn.index("target_kind === 'item'")
        album_branch = fn.index("await matchAlbum(")
        self.assertLess(item_branch, album_branch)

    def test_action_label_reflects_recording_not_album(self):
        self.assertIn("item.target_kind === 'item' ? 'Attach recording ID' : 'Match album'", IMPORT_REVIEW_SOURCE)

    def test_mbid_field_label_reflects_recording_for_item_rows(self):
        self.assertIn("item.target_kind === 'item' ? 'MusicBrainz Recording ID or URL' : 'MusicBrainz Release or Release Group ID/URL'", IMPORT_REVIEW_SOURCE)

    def test_album_only_actions_stay_hidden_for_item_rows(self):
        # isLibraryNoMb (gates "Prepare MB Submission" / "Add MBIDs", both
        # album-only concepts) must still require album_id, so it stays
        # false for target_kind="item" rows without needing its own check.
        self.assertIn("const isLibraryNoMb = item.type === 'library_no_mb' && Boolean(item.album_id);", IMPORT_REVIEW_SOURCE)


if __name__ == "__main__":
    unittest.main()
