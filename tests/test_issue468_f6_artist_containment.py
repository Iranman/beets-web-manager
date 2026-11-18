"""#468 F6: tighten artist-token-containment so a DISTINCT artist cannot
satisfy the playlist textual match gate.

Root cause (verified on origin/main): a strict-subset artist name
("Drake" ⊆ "Drake Bell") flat-scored 0.92 via the containment bonus, and
even with that removed the sequence/Dice overlap of the one shared token
(~0.667) still cleared the 0.55 gate. The fix caps an asymmetric-containment
match at ARTIST_CONTAINMENT_BASE * covered-token ratio in the single shared
scorer both the playlist and AcoustID paths route through.

Synthetic names only -- no live keys, no real library.
"""

import unittest

from backend.matching import artist_pair_similarity
from backend.matching.normalize import ARTIST_CONTAINMENT_BASE
from backend.acoustid_service import _playlist_artist_name_score
from backend.playlist_service import _playlist_artist_score

GATE = 0.55  # backend/playlist_service.py: artist_ok = artist_score >= 0.55


def _norm(value):
    import re
    return re.sub(r"\s+", " ", (value or "").strip().lower())


class _Item:
    def __init__(self, artist):
        self.artist = artist
        self.albumartist = artist
        self.artist_credit = artist


class ArtistContainmentPrecision(unittest.TestCase):
    # (a) regression: the reported false positive is now below the gate.
    def test_distinct_subset_artist_rejected(self):
        for query, cand in [
            ("Drake Bell", "Drake"),   # the reported case
            ("Drake", "Drake Bell"),   # symmetric direction
            ("John Williams", "John"), # extra distinct-artist case
            ("John", "John Williams"),
            ("Kanye West", "Kanye"),
        ]:
            score = _playlist_artist_score(query, _Item(cand))
            self.assertLess(
                score, GATE,
                f"{query!r} vs {cand!r} must be REJECTED (got {score:.3f})",
            )

    # (b) guards: legitimate variants still pass the gate.
    def test_legit_variants_still_pass(self):
        for query, cand in [
            ("The Beatles", "Beatles"),              # non-identifying article
            ("Beatles", "The Beatles"),
            ("Artist feat. Guest", "Artist"),        # feat. stripped upstream
            ("Radiohead", "Radiohead"),              # exact
            ("John Williams", "John Williams Orchestra"),  # near-complete containment
        ]:
            score = _playlist_artist_score(query, _Item(cand))
            self.assertGreaterEqual(
                score, GATE,
                f"{query!r} vs {cand!r} must still PASS (got {score:.3f})",
            )

    # The AcoustID-path scorer (reachable from the gate via
    # _playlist_artist_name_score) must agree -- no 0.92 leak.
    def test_acoustid_path_agrees(self):
        self.assertLess(_playlist_artist_name_score("Drake Bell", "Drake"), GATE)
        self.assertEqual(_playlist_artist_name_score("The Beatles", "Beatles"), 1.0)
        self.assertEqual(_playlist_artist_name_score("Artist feat. Guest", "Artist"), 1.0)

    # Shared helper: containment is scaled by covered-token ratio, exact is 1.0.
    def test_helper_containment_scaling(self):
        self.assertAlmostEqual(
            artist_pair_similarity(_norm("Drake Bell"), _norm("Drake")),
            ARTIST_CONTAINMENT_BASE * 0.5,
        )
        self.assertAlmostEqual(
            artist_pair_similarity(_norm("John Williams"), _norm("John Williams Orchestra")),
            ARTIST_CONTAINMENT_BASE * (2.0 / 3.0),
        )
        self.assertEqual(artist_pair_similarity("radiohead", "radiohead"), 1.0)
        self.assertEqual(artist_pair_similarity("", "drake"), 0.0)


if __name__ == "__main__":
    unittest.main()
