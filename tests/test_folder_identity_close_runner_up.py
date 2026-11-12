"""Ambiguity margin: a text/slot-proven candidate with another Release Group
scoring within CLOSE_RUNNER_UP_MARGIN goes to review (``close_runner_up``);
a clear winner, or a candidate whose every track is hard-proven (fingerprint
or embedded Recording ID), is unaffected. Synthetic evaluated releases only."""
import unittest

from backend.matching.folder_identity import (
    CLOSE_RUNNER_UP_MARGIN, decide_auto_import, rank_folder_candidates,
)

RG_A, REL_A = "a1000000-0000-0000-0000-00000000000a", "a0000000-0000-0000-0000-00000000000a"
RG_B, REL_B = "b1000000-0000-0000-0000-00000000000b", "b0000000-0000-0000-0000-00000000000b"


def _release(rid, rgid, score, *, allowed, hard, verified=3, total=3):
    assignments = [{"status": "matched", "is_deterministic": hard} for _ in range(total)]
    canonical = {"score": score, "local_tracks_verified": verified, "local_tracks_total": total,
                 "track_alignment": {"assignments": assignments}}
    return {"release_id": rid, "release_group_id": rgid, "track_count": total, "sources": ["musicbrainz_search"],
            "preflight": {}, "error": "",
            "matching_decision": {"action_allowed": allowed, "conflicts": [],
                                  "evidence": {"canonical_match": canonical}}}


def _decide(releases):
    candidates = rank_folder_candidates(releases, 3)
    return candidates, decide_auto_import(candidates, skip_reason="no_strong_match", audio_count=3,
                                          unconfirmed_files=0)


class CloseRunnerUpTests(unittest.TestCase):
    def test_near_tie_with_text_evidence_goes_to_review(self):
        runner_up = 0.80 - CLOSE_RUNNER_UP_MARGIN / 2
        candidates, auto = _decide([
            _release(REL_A, RG_A, 0.80, allowed=True, hard=False),
            _release(REL_B, RG_B, runner_up, allowed=False, hard=False, verified=0),
        ])
        self.assertFalse(auto["eligible"], auto)
        self.assertIn("close_runner_up", auto["reasons"])
        top = next(c for c in candidates if c["release_group_id"] == RG_A)
        self.assertFalse(top["evidence"]["action_allowed"])
        self.assertEqual(top["evidence"]["review_reasons"], ["close_runner_up"])
        self.assertNotIn("_score", top)

    def test_runner_up_scoring_higher_also_counts(self):
        _, auto = _decide([
            _release(REL_A, RG_A, 0.80, allowed=True, hard=False),
            _release(REL_B, RG_B, 0.90, allowed=False, hard=False, verified=0),
        ])
        self.assertIn("close_runner_up", auto["reasons"])

    def test_clear_winner_still_auto_accepts(self):
        candidates, auto = _decide([
            _release(REL_A, RG_A, 0.90, allowed=True, hard=False),
            _release(REL_B, RG_B, 0.90 - 2 * CLOSE_RUNNER_UP_MARGIN, allowed=False, hard=False, verified=0),
        ])
        self.assertTrue(auto["eligible"], auto)
        self.assertEqual(auto["release_group_id"], RG_A)
        self.assertEqual(candidates[0]["evidence"]["review_reasons"], [])

    def test_single_candidate_unaffected(self):
        _, auto = _decide([_release(REL_A, RG_A, 0.80, allowed=True, hard=False)])
        self.assertTrue(auto["eligible"], auto)

    def test_fingerprint_verified_near_tie_still_auto_accepts(self):
        candidates, auto = _decide([
            _release(REL_A, RG_A, 0.80, allowed=True, hard=True),
            _release(REL_B, RG_B, 0.80, allowed=False, hard=False, verified=0),
        ])
        self.assertTrue(auto["eligible"], auto)
        self.assertEqual(auto["release_group_id"], RG_A)
        self.assertNotIn("close_runner_up", auto["reasons"])

    def test_partly_hard_proven_is_still_text_dependent(self):
        rel = _release(REL_A, RG_A, 0.80, allowed=True, hard=True)
        rel["matching_decision"]["evidence"]["canonical_match"]["track_alignment"]["assignments"][0][
            "is_deterministic"] = False
        _, auto = _decide([rel, _release(REL_B, RG_B, 0.79, allowed=False, hard=False, verified=0)])
        self.assertIn("close_runner_up", auto["reasons"])


if __name__ == "__main__":
    unittest.main()
