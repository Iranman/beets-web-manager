"""Defect (a): a failed MusicBrainz release lookup must not let a worse-scoring
surviving sibling pose as the answer.

Shape of the live bug (folder ``0117. Aretha Franklin - Lady Soul (1968)``):
the correct 10-track 1968 Release Group's release lookup fails, the 20-track
1995 reissue's lookup succeeds, and the reissue wrongly becomes the review
suggestion. An errored preflight carries NO Release Group (see
``_folder_release_preflight`` returning before it reads one), so the fix cannot
invent a candidate for the 1968 RG -- it must instead refuse to present the
sibling and route to review, keyed off the authoritative ``auto_import``
verdict. Synthetic evaluated releases only; no MusicBrainz/AcoustID I/O."""
import unittest
from pathlib import Path

import tests.test_import_batch_phase_b as pb
from backend.ai_service import _ai_batch_review_suggestion
from backend.matching.folder_identity import decide_auto_import, rank_folder_candidates


def setUpModule():
    pb.setUpModule()

# 1968 original (correct), 1995 combined reissue (surviving worse sibling).
RG_1968, REL_1968 = "05571372-5b11-39b7-89c5-1c02589a27c8", "10000000-0000-0000-0000-000000001968"
RG_REISSUE, REL_REISSUE = "ea3b697e-e0a6-4ac8-8ebd-e0e715fe9c5a", "20000000-0000-0000-0000-000000001995"
FOLDER = "/downloads/0117. Aretha Franklin - Lady Soul (1968)"


def _resolved(rid, rgid, score, *, allowed, verified, total, title="", artist=""):
    assignments = [{"status": "matched", "is_deterministic": False} for _ in range(total)]
    canonical = {"score": score, "local_tracks_verified": verified, "local_tracks_total": total,
                 "track_alignment": {"assignments": assignments}}
    return {"release_id": rid, "release_group_id": rgid, "track_count": total,
            "sources": ["musicbrainz_search"],
            "preflight": {"release_title": title, "release_artist": artist}, "error": "",
            "matching_decision": {"action_allowed": allowed, "conflicts": [],
                                  "evidence": {"canonical_match": canonical}}}


def _errored(rid):
    """What ``import_identity`` builds for a release whose MB lookup failed:
    no Release Group, no matching decision, an error string set."""
    return {"release_id": rid, "release_group_id": "", "track_count": 0,
            "sources": ["embedded_release_id"], "preflight": {}, "matching_decision": {},
            "error": "MusicBrainz release lookup failed"}


class ReleaseLookupFailedTests(unittest.TestCase):
    def test_failed_lookup_does_not_suggest_surviving_sibling(self):
        """(i) 1968 lookup FAILS, 20-track reissue SUCCEEDS -> suggestion is NOT
        the reissue, names no Release Group, and auto-import is refused."""
        reissue_title, reissue_artist = "lady soul (deluxe)", "aretha franklin"
        reissue = _resolved(REL_REISSUE, RG_REISSUE, 0.60, allowed=False, verified=0, total=20,
                             title=reissue_title, artist=reissue_artist)
        releases = [reissue, _errored(REL_1968)]

        candidates = rank_folder_candidates(releases, folder_track_count=10)
        failed = sum(1 for r in releases if r["error"])  # mirrors import_identity.identify_import_folder
        auto = decide_auto_import(candidates, skip_reason="no_strong_match", audio_count=10,
                                  unconfirmed_files=10, failed_lookups=failed)
        identity = {"candidates": candidates, "auto_import": auto,
                    "warnings": [f"release_lookup_failed:{failed}"]}
        suggestion = _ai_batch_review_suggestion(FOLDER, "no_strong_match", identity)

        self.assertEqual(failed, 1)
        self.assertFalse(auto["eligible"], auto)
        self.assertTrue(any(r.startswith("release_lookup_failed") for r in auto["reasons"]), auto)
        # The surviving worse sibling must never be presented as the answer -- in
        # the IDs or in the NAME shown as detected_album/detected_artist.
        self.assertEqual(suggestion["mb_releasegroupid"], "")
        self.assertEqual(suggestion["mb_albumid"], "")
        self.assertNotIn(RG_REISSUE, suggestion.values())
        self.assertNotIn(REL_REISSUE, suggestion.values())
        # The reissue's distinctive title/artist must not leak into any field
        # (these become the review row's detected_album / detected_artist).
        self.assertNotIn(reissue_title, suggestion.values())
        self.assertNotIn(reissue_artist, suggestion.values())
        self.assertNotEqual(suggestion["album"], reissue_title)
        self.assertNotEqual(suggestion["albumartist"], reissue_artist)
        self.assertNotEqual(suggestion["artist"], reissue_artist)
        # Falls back to neutral folder-derived text, like plain_suggestion.
        folder_name = FOLDER.rsplit("/", 1)[-1]
        self.assertEqual(suggestion["album"], folder_name)
        self.assertEqual(suggestion["albumartist"], folder_name)
        self.assertIn("could not be evaluated", suggestion["reason"])

    def test_errored_release_carrying_a_release_group_is_still_dropped(self):
        """Pins the load-bearing ``not release.get("error")`` guard in
        ``rank_folder_candidates``. The "release has no tracks" preflight path
        (``_fetch_mb_release_tracklist`` ok, zero tracks) sets ``release_group``
        and THEN errors, so an errored row CAN carry an rgid -- the guard is not
        redundant. Such a row must still be dropped (it is counted as a failed
        lookup and routed to review), never ranked, or a zero-track edition
        could pose as the folder's identity."""
        reissue = _resolved(REL_REISSUE, RG_REISSUE, 0.60, allowed=False, verified=0, total=20,
                            title="lady soul (deluxe)", artist="aretha franklin")
        errored_with_rgid = {"release_id": REL_1968, "release_group_id": RG_1968,
                             "track_count": 0, "sources": ["embedded_release_id"],
                             "preflight": {}, "matching_decision": {},
                             "error": "MusicBrainz release has no tracks"}
        candidates = rank_folder_candidates([reissue, errored_with_rgid], folder_track_count=10)
        groups = {c["release_group_id"] for c in candidates}
        self.assertNotIn(RG_1968, groups)   # dropped despite carrying an rgid
        self.assertEqual(groups, {RG_REISSUE})

    def test_both_release_groups_present_ranks_full_coverage_first(self):
        """(ii) Both RGs resolve, no fingerprints -> the 10-track/1968 RG ranks
        first (coverage weighting) and the fix is inert: the suggestion still
        names the correct RG, nothing is marked unevaluated."""
        releases = [
            _resolved(REL_1968, RG_1968, 0.90, allowed=True, verified=10, total=10,
                      title="lady soul", artist="aretha franklin"),
            _resolved(REL_REISSUE, RG_REISSUE, 0.60, allowed=False, verified=0, total=20,
                      title="lady soul (deluxe)", artist="aretha franklin"),
        ]
        candidates = rank_folder_candidates(releases, folder_track_count=10)
        auto = decide_auto_import(candidates, skip_reason="no_strong_match", audio_count=10,
                                  unconfirmed_files=10, failed_lookups=0)
        suggestion = _ai_batch_review_suggestion(FOLDER, "no_strong_match",
                                                 {"candidates": candidates, "auto_import": auto})

        self.assertEqual(candidates[0]["release_group_id"], RG_1968)
        self.assertEqual(candidates[0]["deterministic_rank"], 1)
        self.assertEqual(candidates[1]["release_group_id"], RG_REISSUE)
        self.assertFalse(any("release_lookup_failed" in r for r in auto["reasons"]), auto)
        self.assertEqual(suggestion["mb_releasegroupid"], RG_1968)
        self.assertNotIn("could not be evaluated", suggestion["reason"])


class FailedLookupReviewRowTests(pb._Batch):
    """End-to-end guard for the round-2 fix: drive a Beets-skipped folder whose
    release lookup failed through the REAL review-row path and assert the
    surviving sibling's name never reaches the suggestion OR the review row's
    human-facing ``detected_artist``/``detected_album`` (which a pure-function
    test of ``_ai_batch_review_suggestion`` alone cannot pin -- those fields are
    assigned in ``_ai_batch_process_folder`` and read by the fallback row, so a
    future change there could reintroduce the leak)."""

    def setUp(self):
        super().setUp()
        self.leak_title, self.leak_artist = "lady soul (deluxe)", "aretha franklin"
        # Beets skips folder A; identify returns the failed-lookup identity whose
        # only surviving candidate is the worse-scoring sibling (distinctive
        # preflight name that MUST NOT leak). Folder B imports normally.
        self.reimport[self.folders[0]] = pb.skipped(self.folders[0])
        self.ident = {
            "folder": "", "skip_reason": "no_strong_match",
            "candidates": [{"release_group_id": pb.OTHER_RG, "representative_release_id": "sibling-rel",
                            "deterministic_rank": 1, "ai_rank": None,
                            "evidence": {"preflight": {"release_title": self.leak_title,
                                                       "release_artist": self.leak_artist}}}],
            "auto_import": {"eligible": False, "release_group_id": "", "release_id": "",
                            "reasons": ["release_lookup_failed:1", "no_release_group_verified"],
                            "post_conditions": ["beets_strong_match", "single_new_album_with_release_group"]},
            "conflicts": [], "warnings": ["release_lookup_failed:1"], "ai_state": {"status": "not_run"},
        }

    def test_failed_lookup_review_row_never_shows_the_sibling_name(self):
        _, folders = self.run_batch()
        a = folders[self.folders[0]]
        folder_name = Path(self.folders[0]).name

        self.assertEqual(a["status"], "review_created")
        self.queued.assert_called_once()
        suggestion = self.queued.call_args.args[1]
        # The suggestion names no Release Group and no sibling-derived text.
        self.assertEqual(suggestion["mb_releasegroupid"], "")
        self.assertEqual(suggestion["mb_albumid"], "")
        self.assertNotIn(self.leak_title, suggestion.values())
        self.assertNotIn(self.leak_artist, suggestion.values())
        self.assertEqual(suggestion["album"], folder_name)
        self.assertEqual(suggestion["albumartist"], folder_name)
        self.assertIn("could not be evaluated", suggestion["reason"])
        # The review row's human-facing detected_* fields (the round-1 leak site).
        self.assertEqual(a["detected_artist"], folder_name)
        self.assertEqual(a["detected_album"], folder_name)
        self.assertEqual(a["suggested_release_group_id"], "")


if __name__ == "__main__":
    unittest.main()
