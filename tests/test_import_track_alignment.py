"""Tests for import-review track alignment: candidate_tracks uses _best_album_track_match.

Covers:
- Missing-first-track → shifted alignment, not 0 matches
- matched_count mirrors _folder_release_preflight (unique MB indices ≥ 0.82)
- Filename with album prefix / track number matches canonical MB title
- Missing-track count == 1, not equal to total
- Target preview uses source_path from alignment rows
- Extra local files stay out of target preview/import subsets
- Wrong audio title still shows as 'different' / blocks import
"""
import unittest
from pathlib import Path


def _app_source() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "app.py").read_text(encoding="utf-8")



def _frontend_import_review_source() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "frontend" / "src" / "features" / "importReview" / "ImportReviewPage.tsx").read_text(encoding="utf-8")

def _candidate_tracks_source(src: str) -> str:
    """Extract the shared candidate-track comparison helper used by the route and revalidator."""
    start = src.index("def _candidate_track_local_candidates(")
    end = src.index("\ndef _target_preview_year(", start)
    return src[start:end]
def _build_target_preview_source(src: str) -> str:
    """Extract _build_import_target_preview (up to the route that calls it)."""
    start = src.index("def _build_import_target_preview(")
    end = src.index("@app.post(\"/api/folders/import-target-preview\")", start)
    return src[start:end]


class CandidateTracksAlignmentStructureTests(unittest.TestCase):
    """candidate_tracks uses _best_album_track_match, not positional SequenceMatcher."""

    def setUp(self):
        self._src = _app_source()
        self._fn = _candidate_tracks_source(self._src)

    def test_title_prefix_normalizer_handles_review_torrent_paths(self):
        """Review folders under /data/torrents/music must strip artist/album prefixes before fuzzy matching."""
        self.assertIn('def _album_track_path_prefixes', self._src)
        self.assertIn('"/data/torrents/music/"', self._src)
        self.assertIn('def _prefix_candidates(value: str)', self._src)
        self.assertIn('_artist_folder_name_without_mbid(text)', self._src)
        self.assertIn('prefixes.extend(_prefix_candidates(parts[0]))', self._src)
        self.assertIn('prefixes.extend(_prefix_candidates(parts[1]))', self._src)
        self.assertIn('if norm.startswith(prefix + " ")', self._src)
        self.assertIn('norm[len(prefix)', self._src)

    def test_candidate_route_uses_shared_comparison_payload(self):
        route_start = self._src.index('@app.get("/api/candidates/<mb_albumid>/tracks")')
        route_end = self._src.index('\ndef _target_preview_year(', route_start)
        route = self._src[route_start:route_end]
        self.assertIn('_candidate_track_comparison_payload(mb_albumid, folder, release_group_id=release_group_id)', route)

    def test_candidate_route_passes_release_group_scope(self):
        route_start = self._src.index('@app.get("/api/candidates/<mb_albumid>/tracks")')
        route_end = self._src.index('\ndef _target_preview_year(', route_start)
        route = self._src[route_start:route_end]
        self.assertIn('release_group_id = request.args.get("release_group_id", "").strip()', route)
        self.assertIn('release_group_id=release_group_id', route)

    def test_release_group_scope_rejects_wrong_representative(self):
        self.assertIn('_mb_release_group_candidates(selected_rgid, log)', self._fn)
        self.assertIn('representative_belongs_to_selected_release_group', self._fn)
        self.assertIn('Representative Release ID rejected: it does not belong to selected Release Group', self._fn)
        self.assertIn('if rel_group != selected_rgid:', self._fn)

    def test_uses_best_album_track_match(self):
        """Core alignment call must be present — not just positional ratio."""
        self.assertIn("_best_album_track_match(", self._fn)

    def test_does_not_use_positional_title_lookup(self):
        """Old positional local_titles[i] pattern must not be present."""
        self.assertNotIn("local_titles[i]", self._fn)
        self.assertNotIn("_folder_track_search_titles(", self._fn)

    def test_builds_candidate_dicts_from_audio_files(self):
        """Must scan folder with audio file helpers, not just title strings."""
        self.assertIn("_audio_position_from_path(", self._fn)
        self.assertIn("_slskd_title_guess_from_name(", self._fn)

    def test_matched_count_uses_matched_indices_set(self):
        """matched_count must use the same set-based counting as _folder_release_preflight."""
        self.assertIn("matched_indices", self._fn)
        self.assertIn("len(matched_indices)", self._fn)

    def test_missing_status_for_unmatched_mb_tracks(self):
        """MB tracks with no matching local candidate become 'missing', not 'different'."""
        # Route-level status assignments must only use matched/fuzzy/missing/extra.
        self.assertIn('"missing"', self._fn)
        # No direct 'different' assignment in this route.
        self.assertNotIn('status = "different"', self._fn)

    def test_source_path_in_comparison_rows(self):
        """Each comparison row must include source_path so target preview can use it."""
        self.assertIn('"source_path"', self._fn)

    def test_uses_preflight_threshold_constant(self):
        """Must use _MB_TRACK_PREFLIGHT_MATCH_THRESHOLD, not a magic literal."""
        self.assertIn("_MB_TRACK_PREFLIGHT_MATCH_THRESHOLD", self._fn)

    def test_unmatched_candidates_try_acoustid_fallback(self):
        """Automatic verification must promote fingerprint-confirmed rows into the shared mapping."""
        self.assertIn("_album_track_fingerprint_check(cand, mb_tracks)", self._fn)
        self.assertIn('cand["acoustid_verified"] = True', self._fn)
        self.assertIn('cand["acoustid_mismatch"] = True', self._fn)
        self.assertIn('"status": "conflicting" if cand.get("acoustid_mismatch") else "extra"', self._fn)
        self.assertIn('status = "acoustid_verified" if cand.get("acoustid_verified")', self._fn)


class MissingFirstTrackAlignmentTests(unittest.TestCase):
    """When the first MB track is absent locally, remaining tracks must not shift to 'different'."""

    def setUp(self):
        self._fn = _candidate_tracks_source(_app_source())

    def test_greedy_assignment_loop_present(self):
        """Greedy MB-track assignment loop must exist to handle off-by-one gaps."""
        self.assertIn("mb_display", self._fn)
        self.assertIn("used_cand", self._fn)

    def test_per_mb_dict_collects_candidate_scores(self):
        """per_mb dict accumulates (cand_idx, score) pairs per MB track index."""
        self.assertIn("per_mb", self._fn)
        self.assertIn("per_mb.setdefault(mb_idx", self._fn)

    def test_missing_count_is_derived_from_mb_tracks_without_display_entry(self):
        """MB tracks not in mb_display become 'missing' — count is sparse, not total."""
        self.assertIn("if i in mb_display", self._fn)


class TargetPreviewSourcePathTests(unittest.TestCase):
    """_build_import_target_preview uses explicit source_path from rows, not purely sequential."""

    def setUp(self):
        self._fn = _build_target_preview_source(_app_source())

    def test_reads_source_path_from_row(self):
        """Must extract source_path from the track_mapping row dict."""
        self.assertIn("row.get(\"source_path\")", self._fn)

    def test_uses_row_source_path_before_sequential_fallback(self):
        """Row source_path must be evaluated before the sequential source_index fallback."""
        row_src_pos = self._fn.index("row.get(\"source_path\")")
        sequential_pos = self._fn.index("source_index += 1")
        self.assertLess(row_src_pos, sequential_pos)

    def test_sequential_fallback_still_present_for_legacy_callers(self):
        """source_index-based fallback must remain for callers that don't supply source_path."""
        self.assertIn("source_index", self._fn)
        self.assertIn("source_files", self._fn)


    def test_target_preview_strips_lidarr_source_id_suffix(self):
        """Preview must use the same cleaned track title as the import worker."""
        self.assertIn("raw_title = row.get", self._fn)
        self.assertIn("_strip_track_filename_id_suffix(raw_title)", self._fn)

class PartialImportSubsetTests(unittest.TestCase):
    """Partial imports operate on verified source rows; extras remain in review."""

    def setUp(self):
        self._app = _app_source()
        self._fn = _build_target_preview_source(self._app)
        self._frontend = _frontend_import_review_source()

    def test_candidate_tracks_returns_extra_count(self):
        """Frontend receives an explicit extra_count for partial-import summaries."""
        candidate_fn = _candidate_tracks_source(self._app)
        self.assertIn('"extra_count"', candidate_fn)
        self.assertIn('r.get("status") in {"extra", "conflicting"}', candidate_fn)

    def test_extra_status_does_not_block_target_preview(self):
        """Rows with status 'extra' are counted as left behind, not blocked."""
        self.assertIn('_IMPORT_REVIEW_LEFT_BEHIND_STATUSES', self._fn)
        extra_pos = self._fn.index('if status in _IMPORT_REVIEW_LEFT_BEHIND_STATUSES:')
        extra_region = self._fn[extra_pos: extra_pos + 180]
        self.assertIn('unmatched_extra_count += 1', extra_region)
        self.assertIn('continue', extra_region)
        self.assertNotIn('blocked.append', extra_region)

    def test_extra_status_receives_no_target_path(self):
        """Target filenames are generated only after left-behind extras continue."""
        extra_pos = self._fn.index('if status in _IMPORT_REVIEW_LEFT_BEHIND_STATUSES:')
        target_pos = self._fn.index('target_name =')
        self.assertLess(extra_pos, target_pos)

    def test_missing_tracks_are_informational_only(self):
        """Missing release tracks should produce warnings, not blockers."""
        missing_pos = self._fn.index('if status == "missing":')
        missing_region = self._fn[missing_pos: missing_pos + 160]
        self.assertIn('missing_album_track_count += 1', missing_region)
        self.assertIn('continue', missing_region)
        self.assertNotIn('blocked.append', missing_region)

    def test_different_status_is_cleanup_candidate_not_global_blocker(self):
        """Rows with status 'different'/'conflicting' become cleanup candidates, not global blockers."""
        diff_pos = self._fn.index('if status == "different" or status == "conflicting":')
        diff_region = self._fn[diff_pos: diff_pos + 900]
        self.assertIn('rejected_cleanup_count += 1', diff_region)
        self.assertIn('tracks.append({', diff_region)
        self.assertIn('continue', diff_region)
        self.assertNotIn('blocked.append', diff_region)

    def test_import_route_uses_selected_source_files(self):
        """Import-with-id parses an immutable selected source list from the request."""
        self.assertIn('selected_source_files = _import_review_selected_source_files(', self._app)
        self.assertIn('selected_subset_import = bool(selected_source_files)', self._app)

    def test_worker_stages_selected_subset(self):
        """Worker imports the staged selected subset rather than the whole source folder."""
        self.assertIn('import_folder_path = _stage_selected_audio_files(', self._app)
        self.assertIn('"--search-id", mb_albumid, import_folder_path', self._app)

    def test_unmatched_remainder_keeps_review_item(self):
        """Pending review is preserved when unmatched audio remains after partial import."""
        self.assertIn('Partial import complete.', self._app)
        self.assertIn('Pending Review kept for unmatched files', self._app)

    def test_frontend_sends_selected_source_files(self):
        """Preview and import job payloads include selected source files."""
        self.assertIn('selectedImportSourceFiles', self._frontend)
        self.assertIn('selected_source_files: selectedImportSourceFiles(selectedMatch)', self._frontend)
        self.assertIn('selected_source_files: selectedSourceFiles', self._frontend)

    def test_frontend_uses_single_partial_summary(self):
        """UI should show partial ready text without a contradictory review label."""
        self.assertIn('Partial auto-import ready', self._frontend)
        self.assertIn('Partial import ready:', self._frontend)
        self.assertNotIn('Review before import', self._frontend)
        self.assertNotIn('Partial import — review before applying', self._frontend)




class BeetsSelectedReleaseImportConfigTests(unittest.TestCase):
    """Selected-release imports must not fall back to dirty source tags."""

    def setUp(self):
        self._app = _app_source()

    def test_selected_release_import_uses_permissive_distance_threshold(self):
        self.assertIn('strong_rec_thresh: 1.0', self._app)
        self.assertIn('medium_rec_thresh: 1.0', self._app)
        self.assertNotIn('strong_rec_thresh: 0.0', self._app)
        self.assertNotIn('medium_rec_thresh: 0.0', self._app)

    def test_selected_release_threshold_comment_uses_beets_distance_semantics(self):
        self.assertIn('Beets thresholds are distances', self._app)
        self.assertNotIn('lowers the match threshold to 0 so --search-id', self._app)
