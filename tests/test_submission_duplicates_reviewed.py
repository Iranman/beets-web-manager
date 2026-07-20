"""Tests for making "Possible existing releases reviewed" resolvable.

Since the readiness redesign, this check was hardcoded to `ok=False` --
always showing as an open recommendation with no way to actually resolve
it. Follows the same draft-flag pattern as artist_dismissed: an explicit
user action (picking a candidate, using a Discogs fallback as reference, or
an explicit "Mark as reviewed") persists to the draft and the check reads
that back instead of a constant.
"""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTES_SOURCE = (ROOT / "routes_submissions.py").read_text(encoding="utf-8")
SUBMISSIONS_SOURCE = (ROOT / "frontend" / "src" / "views" / "Submissions.tsx").read_text(encoding="utf-8")


def _function_source(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


class BackendDuplicatesReviewedTests(unittest.TestCase):
    def test_check_reads_the_flag_instead_of_a_hardcoded_constant(self):
        preflight_fn = _function_source(ROUTES_SOURCE, "def _submission_preflight(", "def _submission_stage_id(")
        block_start = preflight_fn.index('_check("duplicates_reviewed"')
        block = preflight_fn[block_start:block_start + 340]
        self.assertIn("duplicates_reviewed,", block)
        self.assertNotIn("False, \"musicbrainz_prep\"", block)

    def test_preflight_accepts_duplicates_reviewed_param(self):
        self.assertIn(
            "duplicates_reviewed: bool = False) -> Dict[str, Any]:",
            ROUTES_SOURCE,
        )

    def test_route_reads_flag_from_draft_and_threads_it_through(self):
        route = _function_source(ROUTES_SOURCE, "def submission_target():", "def _draft_target_ref(")
        self.assertIn('duplicates_reviewed = bool(draft.get("duplicates_reviewed"))', route)
        self.assertIn("duplicates_reviewed=duplicates_reviewed,", route)

    def test_still_non_blocking(self):
        # A user who hasn't reviewed candidates yet must not be blocked from
        # preparing the MusicBrainz submission -- it's a recommendation.
        preflight_fn = _function_source(ROUTES_SOURCE, "def _submission_preflight(", "def _submission_stage_id(")
        block_start = preflight_fn.index('_check("duplicates_reviewed"')
        block = preflight_fn[block_start:block_start + 340]
        self.assertIn("blocking=False", block)


class FrontendDuplicatesReviewedTests(unittest.TestCase):
    def test_mark_reviewed_handler_present(self):
        self.assertIn("function markDuplicatesReviewed()", SUBMISSIONS_SOURCE)

    def test_selecting_a_candidate_marks_reviewed(self):
        fn_start = SUBMISSIONS_SOURCE.index("(selectedItem?.evidence?.top_candidates || [])")
        block = SUBMISSIONS_SOURCE[fn_start:fn_start + 500]
        self.assertIn("markDuplicatesReviewed();", block)

    def test_discogs_fallback_reference_also_marks_reviewed(self):
        block_start = SUBMISSIONS_SOURCE.index("Use as reference")
        block = SUBMISSIONS_SOURCE[block_start - 200:block_start + 100]
        self.assertIn("markDuplicatesReviewed();", block)

    def test_explicit_mark_reviewed_button_and_reviewed_indicator(self):
        self.assertIn("Mark as reviewed", SUBMISSIONS_SOURCE)
        self.assertIn('color="success" label="Reviewed"', SUBMISSIONS_SOURCE)

    def test_reviewed_flag_persisted_through_draft_reload(self):
        self.assertIn("duplicates_reviewed: remoteDraft.duplicates_reviewed,", SUBMISSIONS_SOURCE)


if __name__ == "__main__":
    unittest.main()
