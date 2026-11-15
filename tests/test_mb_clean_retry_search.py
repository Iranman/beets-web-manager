"""Clean-then-retry MusicBrainz text search (matching-roadmap).

When the raw-tag release search cascade comes back unusable, ``_text_search``
builds ONE cleaned query (bracketed junk / ``feat.`` / version words stripped)
and retries once to widen the candidate net. These tests prove the retry is
bounded and conditional, and -- the load-bearing part -- that a candidate found
only by the cleaned retry is still just a tier-5 candidate: it passes the same
acceptance gate as any other and can never change the Release Group identity or
auto-import on its own.

Synthetic data only; no provider is called."""
import os
import unittest
from unittest import mock

import backend.import_identity as ii
from backend.matching.folder_identity import decide_auto_import
from tests.test_import_folder_identity import _FolderFixture, REL_B, RG_B

CLEAN_REL = "e0000000-0000-0000-0000-00000000000e"


def _ev(album, artist):
    return {"guessed_album": album, "guessed_artist": artist, "guessed_year": "",
            "folder_track_count": 3, "guessed_artist_mbid": ""}


class _SearchCounter:
    """Records each (album, artist) passed to the patched release search and
    returns candidates keyed off the album argument."""

    def __init__(self, by_album):
        self.by_album = by_album      # {album_string: [release_uuid, ...]}
        self.calls = []               # [(album, artist), ...]

    def __call__(self, album, artist, **kwargs):
        self.calls.append((album, artist))
        return [{"mb_albumid": r} for r in self.by_album.get(album, [])]


class TextSearchCleanRetryTests(unittest.TestCase):
    def _run(self, counter, album, artist):
        with mock.patch.object(ii, "_mb_release_search", side_effect=counter), \
             mock.patch.object(ii, "_mb_release_search_by_folder_tracks", return_value=[]):
            return ii._text_search(_ev(album, artist), "/folder", [])

    def test_usable_first_search_does_not_retry(self):
        # Raw query already yields a candidate -> cascade short-circuits and no
        # cleaned retry runs. Exactly one MB search call.
        counter = _SearchCounter({"Album (Deluxe Edition)": [REL_B]})
        out = self._run(counter, "Album (Deluxe Edition)", "Artist")
        self.assertEqual(out, [REL_B])
        self.assertEqual(len(counter.calls), 1)

    def test_clean_query_equal_to_original_makes_no_extra_call(self):
        # Already-clean tags: the cleaned query matches a plain fold, so the
        # retry is skipped. Only the raw cascade's three calls happen, none of
        # them a cleaned retry.
        counter = _SearchCounter({})  # every search is empty -> unusable
        out = self._run(counter, "Clean Album", "Clean Artist")
        self.assertEqual(out, [])
        self.assertEqual(len(counter.calls), 3)  # raw cascade only, no 4th call

    def test_clean_retry_fires_when_raw_unusable_and_query_differs(self):
        # Raw cascade empty; the cleaned album "album" finds a candidate.
        counter = _SearchCounter({"album": [CLEAN_REL]})
        out = self._run(counter, "Album (Deluxe Edition)", "Artist")
        self.assertEqual(out, [CLEAN_REL])
        cleaned_calls = [c for c in counter.calls if c == ("album", "artist")]
        self.assertEqual(len(cleaned_calls), 1)  # capped at exactly one extra call

    def test_clean_retry_adds_at_most_one_call(self):
        # Even when the cleaned retry also returns nothing, it is tried once and
        # only once (then the track-search fallback, a different function, runs).
        counter = _SearchCounter({})
        self._run(counter, "Album [Explicit]", "Artist feat. Guest")
        cleaned_calls = [c for c in counter.calls if c == ("album", "artist")]
        self.assertEqual(len(cleaned_calls), 1)
        self.assertEqual(len(counter.calls), 4)  # 3 raw + 1 cleaned retry

    def test_score_floor_retries_when_all_hits_weak(self):
        # With MB_TEXT_USABLE_SCORE set, a raw hit below the floor still counts
        # as unusable and triggers the cleaned retry.
        counter = _SearchCounter({})

        def weak_then_clean(album, artist, **kwargs):
            counter.calls.append((album, artist))
            if album == "Album (Deluxe Edition)":
                return [{"mb_albumid": REL_B, "score": 40}]  # below the floor
            if album == "album":
                return [{"mb_albumid": CLEAN_REL, "score": 95}]
            return []

        with mock.patch.dict(os.environ, {"MB_TEXT_USABLE_SCORE": "70"}), \
             mock.patch.object(ii, "_MB_TEXT_USABLE_SCORE", 70), \
             mock.patch.object(ii, "_mb_release_search", side_effect=weak_then_clean), \
             mock.patch.object(ii, "_mb_release_search_by_folder_tracks", return_value=[]):
            out = ii._text_search(_ev("Album (Deluxe Edition)", "Artist"), "/folder", [])
        # Both the weak raw hit and the stronger cleaned hit are kept (widened).
        self.assertIn(CLEAN_REL, out)
        self.assertIn(REL_B, out)


class CleanRetryCannotChangeIdentityTests(_FolderFixture):
    """A candidate that exists ONLY because of the cleaned retry is tier-5
    text evidence like any other: it reaches the candidate pool but cannot
    auto-import without independent per-file proof."""

    def setUp(self):
        super().setUp()
        self.file_hits = {}  # no fingerprint proof for any file

    def _evidence(self, folder):
        # Junk tags so the raw query is unusable and the cleaned query differs.
        return {"audio_files": list(self.paths), "folder_track_count": len(self.paths),
                "guessed_artist": "Synthetic Artist", "guessed_album": "Some Album (Deluxe Edition)",
                "guessed_year": ""}

    def _patches(self):
        patches = super()._patches()
        # Replace the args-insensitive search mock with one that returns RG_B's
        # release ONLY for the cleaned query -- so RG_B is in the pool solely
        # because of the clean-retry.
        def search(album, artist, **kwargs):
            return [{"mb_albumid": REL_B}] if album == "some album" else []
        for i, p in enumerate(patches):
            if getattr(p, "attribute", None) == "_mb_release_search":
                patches[i] = mock.patch.object(ii, "_mb_release_search", side_effect=search)
        return patches

    def test_clean_retry_candidate_reaches_pool_but_never_auto_imports(self):
        result = self.identify()
        groups = {c["release_group_id"] for c in result["candidates"]}
        self.assertIn(RG_B, groups)  # clean-retry widened the pool
        rg_b = next(c for c in result["candidates"] if c["release_group_id"] == RG_B)
        self.assertEqual(rg_b["evidence"]["sources"], ["musicbrainz_search"])  # tier-5 only
        self.assertFalse(rg_b["evidence"]["action_allowed"], rg_b)
        self.assertNotEligible(result, "no_release_group_verified")

    def test_gate_is_load_bearing_mutation_check(self):
        # The same text-only candidate the clean-retry produced. decide_auto_import
        # must reject it; flipping the acceptance gate to allowed (a simulated
        # bypass) would let it through -- proving the gate, not a missing
        # candidate, is what blocks the clean-retry hit.
        def candidate(allowed):
            return {"release_group_id": RG_B, "representative_release_id": REL_B,
                    "evidence": {"action_allowed": allowed, "conflicts": [], "review_reasons": [],
                                 "releases_evaluated": [{"release_id": REL_B, "local_tracks_verified": 3}],
                                 "local_tracks_verified": 3, "local_tracks_total": 3}}

        blocked = decide_auto_import([candidate(False)], skip_reason="no_strong_match", audio_count=3)
        self.assertFalse(blocked["eligible"])
        self.assertIn("no_release_group_verified", blocked["reasons"])

        bypassed = decide_auto_import([candidate(True)], skip_reason="no_strong_match", audio_count=3)
        self.assertTrue(bypassed["eligible"], bypassed)  # mutation: gate removed -> would pass


if __name__ == "__main__":
    unittest.main()
