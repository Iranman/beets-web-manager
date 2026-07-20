"""Regression tests for a follow-up review pass over the artist-resolution
gate: a dead sticky-footer button when the artist stage blocks, a stale
readiness/artist-card flash while switching review items, and leftover dead
code in SubmissionReadinessCard.
"""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUBMISSIONS_SOURCE = (ROOT / "frontend" / "src" / "views" / "Submissions.tsx").read_text(encoding="utf-8")
CARD_SOURCE = (ROOT / "frontend" / "src" / "components" / "SubmissionReadinessCard.tsx").read_text(encoding="utf-8")


def _function_source(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


class ArtistStagePrimaryButtonTests(unittest.TestCase):
    def test_primary_action_does_not_fall_through_to_generic_no_op_blocker(self):
        # Regression: firstBlocker.action_type is 'resolve_artist' while the
        # artist stage is active, which ACTION_LABEL doesn't map and
        # handleCheckAction treats as a no-op -- the sticky footer button
        # showed generic text and did nothing when clicked. The artist stage
        # must be special-cased before the generic firstBlocker branch.
        primary_fn = _function_source(SUBMISSIONS_SOURCE, "const primary = (() => {", "if (activeStep <= 1)")
        artist_check_pos = primary_fn.index("if (artistStageActive)")
        blocker_check_pos = primary_fn.index("const { firstBlocker }")
        self.assertLess(artist_check_pos, blocker_check_pos)

    def test_resolve_artist_action_type_has_a_real_label_as_a_fallback(self):
        # Belt-and-suspenders: even if a resolve_artist blocker ever reaches
        # the generic ACTION_LABEL lookup, it should read sensibly.
        self.assertIn("resolve_artist: 'Resolve artist'", CARD_SOURCE)


class ArtistStageLoadingFlashTests(unittest.TestCase):
    def test_loading_state_checked_before_artist_stage_gate(self):
        # Regression: target isn't cleared while a new fetch is in flight,
        # so switching review items could briefly show the previous item's
        # ArtistResolutionCard (or full page) based on stale data. Loading
        # must be checked first so neither stale layout flashes.
        block = _function_source(SUBMISSIONS_SOURCE, "{targetLoading ? (", "{/* D. Submission readiness */}")
        self.assertIn("artistStageActive ? (", block)
        self.assertIn("<ArtistResolutionCard", block)

    def test_header_and_footer_stage_label_share_one_source(self):
        # Regression: workflow_stage's free text predates the artist stage
        # and always reads "Needs metadata" while it's active; header chip
        # and footer text must both use the artist-aware label instead of
        # disagreeing with the readiness card's own stage name.
        self.assertIn("const stageLabel = artistStageActive ? (target?.preflight.current_stage_label", SUBMISSIONS_SOURCE)
        self.assertIn("<Chip label={stageLabel}", SUBMISSIONS_SOURCE)
        self.assertIn("${target.summary.title || 'Untitled'} / ${stageLabel}`", SUBMISSIONS_SOURCE)


class ReadinessCardDeadCodeTests(unittest.TestCase):
    def test_unused_stage_order_export_removed(self):
        self.assertNotIn("export { STAGE_ORDER }", CARD_SOURCE)
        self.assertNotIn("const STAGE_ORDER = ", CARD_SOURCE)


if __name__ == "__main__":
    unittest.main()
