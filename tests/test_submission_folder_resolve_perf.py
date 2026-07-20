"""Tests for a real performance bug found via live testing: loading the
Submissions page for a folder-based review item took 19-46 seconds. Traced
to _resolve_folder_submission_target reading every file's tags twice --
once in _build_folder_evidence (identity guessing) and again in
_media_tag_track_payload (the actual track payload) -- and for the
imported_singletons branch, the first (evidence) read's result was never
even used. Fixed by listing files without tag-parsing them
(_folder_audio_file_listing), and only paying for the full evidence scan
lazily when tags didn't already cover album/artist/year.
"""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTES_SOURCE = (ROOT / "routes_submissions.py").read_text(encoding="utf-8")


def _function_source(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


class FolderAudioListingHelperTests(unittest.TestCase):
    def setUp(self):
        self._fn = _function_source(
            ROUTES_SOURCE, "def _folder_audio_file_listing(", "def _resolve_folder_submission_target("
        )

    def test_lists_without_opening_mediafile(self):
        # The docstring mentions MediaFile() by name to explain what this
        # helper deliberately avoids doing; the function body itself must
        # not actually call it.
        self.assertNotIn("MediaFile(str(", self._fn)

    def test_prefers_direct_children_falls_back_to_recursive(self):
        self.assertIn("direct = sorted(", self._fn)
        self.assertIn("if direct:", self._fn)
        self.assertIn("folder.rglob(", self._fn)


class ResolveFolderTargetPerfTests(unittest.TestCase):
    def setUp(self):
        self._fn = _function_source(
            ROUTES_SOURCE, "def _resolve_folder_submission_target(", "_STAGE_ORDER = "
        )

    def test_uses_cheap_listing_not_full_evidence_scan_up_front(self):
        listing_pos = self._fn.index("audio_paths = _folder_audio_file_listing(folder)")
        beets_items_pos = self._fn.index("beets_items = _find_beets_items_for_folder(folder)")
        self.assertLess(listing_pos, beets_items_pos)

    def test_imported_singletons_branch_does_not_touch_evidence(self):
        # This branch builds its summary entirely from Beets item rows; the
        # discarded-result bug was calling _build_folder_evidence before
        # reaching here even though nothing downstream used it.
        block_start = self._fn.index('"imported_singletons"')
        block = self._fn[max(0, block_start - 400):block_start + 50]
        self.assertNotIn("_build_folder_evidence", block)

    def test_evidence_scan_is_lazy_and_gated_on_missing_tags(self):
        self.assertIn("has_usable_tags = (", self._fn)
        self.assertIn('evidence = {} if has_usable_tags else _build_folder_evidence(str(folder))', self._fn)

    def test_usable_tags_requires_year_not_just_album_or_artist(self):
        # Regression: track payloads have no other source for release_date:
        # skipping evidence just because album/artist tags exist would
        # silently blank the release year for well-tagged folders.
        block_start = self._fn.index("has_usable_tags = (")
        block = self._fn[block_start:block_start + 220]
        self.assertIn('t.get("year")', block)
        self.assertIn('t.get("album") or t.get("albumartist")', block)


class TrackYearCapturedOnceTests(unittest.TestCase):
    def test_media_tag_track_payload_captures_year(self):
        fn = _function_source(ROUTES_SOURCE, "def _media_tag_track_payload(", "def _folder_cover_art_url(")
        self.assertIn('year = int(getattr(mf, "year", 0)', fn)
        self.assertIn('"year": year,', fn)

    def test_summary_prefers_tag_derived_year_over_evidence_guess(self):
        fn = _function_source(ROUTES_SOURCE, "def _summary_for_folder_tracks(", "def _folder_audio_file_listing(")
        tag_years_pos = fn.index('tag_years = [str(int(t.get("year")))')
        release_date_pos = fn.index("release_date = (max(set(tag_years)")
        self.assertLess(tag_years_pos, release_date_pos)
        self.assertIn('or evidence.get("guessed_year", "")', fn)


if __name__ == "__main__":
    unittest.main()
