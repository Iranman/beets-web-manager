"""Regression test: an inaccessible/empty folder has no albumartist tag to
search MusicBrainz for at all, so without an exemption the "artist_resolved"
check would always fail for it -- and since _submission_current_stage checks
the "artist" stage before "identify", that would make a genuinely
blocked/missing folder show "MusicBrainz artist not found" (and the
ArtistResolutionCard collapsing the whole page to just that) instead of the
correct "folder not found"/"no audio files" messaging already handled by
the identify-stage local_files_found check and the frontend's isBlockedState
path.
"""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTES_SOURCE = (ROOT / "routes_submissions.py").read_text(encoding="utf-8")


def _function_source(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


class ArtistResolvedBlockedStateExemptionTests(unittest.TestCase):
    def setUp(self):
        self._fn = _function_source(
            ROUTES_SOURCE, "def _submission_preflight(", "def _submission_stage_id("
        )

    def test_resolved_state_computed_before_artist_resolved(self):
        resolved_state_pos = self._fn.index('resolved_state = _s(summary.get("resolved_state")')
        artist_resolved_pos = self._fn.index("artist_resolved = (")
        self.assertLess(resolved_state_pos, artist_resolved_pos)

    def test_inaccessible_and_empty_states_exempt_the_artist_gate(self):
        block_start = self._fn.index("artist_resolved = (")
        block = self._fn[block_start:block_start + 260]
        self.assertIn('resolved_state in ("inaccessible", "empty")', block)

    def test_unimported_and_loose_track_states_are_not_exempt(self):
        # These states have real tag-derived albumartist values and should
        # still go through normal artist resolution, not be waved through.
        block_start = self._fn.index("artist_resolved = (")
        block = self._fn[block_start:block_start + 260]
        self.assertNotIn("unimported_album", block)
        self.assertNotIn("loose_tracks", block)


if __name__ == "__main__":
    unittest.main()
