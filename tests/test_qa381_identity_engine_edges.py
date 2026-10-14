"""QA (#381) adversarial edges for the Stage 2b gate, I-8 and the I-7 gap rule.
Synthetic data only; no provider is called."""
import unittest
from pathlib import Path

import backend.import_identity as ii
import backend.matching_service as ms
from backend.matching.library_gaps import (
    ALBUM_RELEASE_WITHOUT_GROUP, ALBUM_TRACK_WITHOUT_RECORDING, ALBUM_WITHOUT_IDS,
    SINGLETON_WITHOUT_RECORDING, library_identity_gaps,
)
from backend.provider_boundary import ProviderOutcome, ProviderResult
from tests.test_import_folder_identity import (
    OTHER_REC, RECS, REL_A, REL_B, RG_A, RG_B, TITLES, _FolderFixture, _hit, _tracks,
)

REL_Z, RG_Z = "e0000000-0000-0000-0000-00000000000e", "e1000000-0000-0000-0000-00000000000e"


class Stage2bGateEdges(_FolderFixture):
    def _add_nested(self, hits):
        nested = self.folder / "Bonus"
        nested.mkdir()
        p = nested / "04 Four.flac"
        p.write_bytes(b"synthetic")
        self.file_hits[str(p)] = hits
        return str(p)

    def test_nested_unproven_file_blocks(self):
        # _build_folder_evidence returns only the 3 direct files; the preflight
        # scans recursively and must count the nested file as unproven.
        self.releases[REL_A] = (RG_A, _tracks(4))
        self._add_nested([])
        result = self.identify()
        self.assertNotEligible(result, "no_release_group_verified")

    def test_nested_file_embedded_recording_conflict_is_surfaced(self):
        # Tier-1 (embedded Recording ID) vs tier-3 (fingerprint) disagreement on
        # a nested file must block exactly like on a direct file.
        self.releases[REL_A] = (RG_A, _tracks(4))
        nested = self._add_nested([_hit(RECS[3])])
        self.embedded = {nested: {"mb_trackid": OTHER_REC}}
        result = self.identify()
        self.assertFalse(result["auto_import"]["eligible"], result["auto_import"])

    def test_embedded_release_in_other_group_blocks(self):
        # Files carry an embedded Release ID from RG_Z but no Release Group tag;
        # fingerprints prove RG_A. Tier-1 evidence disagrees with the winner.
        self.releases[REL_Z] = (RG_Z, _tracks(3, [OTHER_REC] * 3))
        self.embedded = {p: {"mb_albumid": REL_Z} for p in self.paths}
        result = self.identify()
        self.assertFalse(result["auto_import"]["eligible"], result["auto_import"])

    def test_one_transient_failure_in_preflight_blocks(self):
        bad = self.paths[1]
        base = self._lookup

        def lookup(path):
            if str(path) == bad:
                return ProviderResult("acoustid", ProviderOutcome.TRANSIENT_ERROR, data=[])
            return base(path)
        self._lookup = lookup
        result = self.identify()
        self.assertNotEligible(result, "no_release_group_verified")
        self.assertEqual(result["evidence_capabilities"]["acoustid"], "partial")

    def test_lookup_exception_blocks(self):
        base = self._lookup

        def lookup(path):
            if str(path) == self.paths[0]:
                raise RuntimeError("boom")
            return base(path)
        self._lookup = lookup
        result = self.identify()
        self.assertNotEligible(result, "no_release_group_verified")

    def test_one_ambiguous_file_blocks(self):
        self.file_hits[self.paths[0]] = [_hit(RECS[0], 90), _hit(OTHER_REC, 88, releases=(REL_B,), rg=RG_B)]
        result = self.identify()
        self.assertNotEligible(result, "no_release_group_verified")

    def test_low_score_hit_does_not_prove(self):
        self.file_hits[self.paths[0]] = [_hit(RECS[0], 79)]
        self.assertNotEligible(self.identify(), "no_release_group_verified")

    def test_competitor_group_with_partial_proof_blocks(self):
        # RG_B proves two of the three files: it could be the folder's identity,
        # so RG_A is not unique. (Rule changed in #381 re-QA: a group sharing
        # only one track no longer blocks; see test_import_folder_identity.)
        self.releases[REL_B] = (RG_B, _tracks(2))
        self.search = [REL_A, REL_B]
        result = self.identify()
        self.assertNotEligible(result, "other_release_group_has_track_proof")

    def test_two_embedded_groups_block(self):
        self.embedded = {self.paths[0]: {"mb_releasegroupid": RG_A}, self.paths[1]: {"mb_releasegroupid": RG_B}}
        self.assertNotEligible(self.identify(), "folder_conflict:")

    def test_embedded_group_matching_candidate_does_not_conflict(self):
        self.embedded = {p: {"mb_releasegroupid": RG_A} for p in self.paths}
        self.assertTrue(self.identify()["auto_import"]["eligible"])

    def test_auth_failed_blocks(self):
        self.outcome = ProviderResult("acoustid", ProviderOutcome.AUTHENTICATION_ERROR, data=[])
        result = self.identify()
        self.assertEqual(result["evidence_capabilities"]["acoustid"], "auth_failed")
        self.assertNotEligible(result, "no_release_group_verified")


    def _two_groups_share_recordings(self):
        self.releases[REL_B] = (RG_B, _tracks(3))
        self.file_hits = {p: [_hit(RECS[i], releases=(REL_A, REL_B), rg=RG_A) | {"mb_releasegroupids": [RG_A, RG_B]}]
                          for i, p in enumerate(self.paths)}

    def test_competitor_group_release_fetch_outage_blocks(self):
        # Fingerprints say the recordings also belong to RG_B, but the
        # MusicBrainz fetch of RG_B's release fails (outage/rate limit). An
        # unanswered lookup must not read as "RG_B has no proof".
        self._two_groups_share_recordings()
        fetch = self._fetch
        self._fetch = lambda rid, log=None: {"ok": False, "error": "MusicBrainz 503"} if rid == REL_B else fetch(rid, log)
        result = self.identify()
        self.assertFalse(result["auto_import"]["eligible"], result["auto_import"])

    def test_competitor_group_unresolved_by_outage_blocks(self):
        # RG_B (from the fingerprint hits) cannot be resolved to a release
        # because MusicBrainz is down: its evidence was never evaluated.
        # Realistic AcoustID answer: several releases per group, so the
        # top-2 release seeds are both RG_A editions and RG_B is reachable
        # only through its Release Group.
        self.releases[REL_B] = (RG_B, _tracks(3))
        extra = ["a3000000-0000-0000-0000-00000000000a", "a4000000-0000-0000-0000-00000000000a"]
        for rid in extra:
            self.releases[rid] = (RG_A, _tracks(3))
        self.file_hits = {p: [_hit(RECS[i], releases=(REL_A, *extra, REL_B), rg=RG_A) | {"mb_releasegroupids": [RG_A, RG_B]}]
                          for i, p in enumerate(self.paths)}
        self.rg_to_release = {RG_A: REL_A, RG_B: ""}
        result = self.identify()
        self.assertNotIn(RG_B, {c["release_group_id"] for c in result["candidates"]})
        self.assertFalse(result["auto_import"]["eligible"], result["auto_import"])


class AIBatchGateAfterI8(_FolderFixture):
    def test_ai_choice_between_two_proven_groups_cannot_auto_import(self):
        # I-8 makes per-file fingerprints prove tracks in every caller of the
        # preflight. When two Release Groups share the recordings, an AI-picked
        # release must not pass the AI batch auto-import gate (AI would be the
        # identity source). On the base commit neither release passes.
        import backend.ai_service as ai
        self.releases[REL_B] = (RG_B, _tracks(3))
        self.file_hits = {p: [_hit(RECS[i], releases=(REL_A, REL_B), rg=RG_A) | {"mb_releasegroupids": [RG_A, RG_B]}]
                          for i, p in enumerate(self.paths)}
        patches = self._patches()
        for p in patches:
            p.start()
        try:
            pf = ms._folder_release_preflight(str(self.folder), REL_B)
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertFalse(ai._ai_auto_import_allowed("fresh_import", "high", pf, True, REL_B))


class PreflightI8Edges(_FolderFixture):
    def _preflight(self, **kw):
        patches = self._patches()
        for p in patches:
            p.start()
        try:
            return ms._folder_release_preflight(str(self.folder), REL_A, **kw)
        finally:
            for p in reversed(patches):
                p.stop()

    def _statuses(self, pre):
        return [a["acoustid_status"] for a in
                pre["matching_decision"]["evidence"]["canonical_match"]["track_alignment"]["assignments"]]

    def test_not_configured_never_counts_as_no_result(self):
        for outcome in (ProviderOutcome.NOT_CONFIGURED, ProviderOutcome.AUTHENTICATION_ERROR,
                        ProviderOutcome.UNAVAILABLE, ProviderOutcome.RATE_LIMITED):
            with self.subTest(outcome):
                self.outcome = ProviderResult("acoustid", outcome, data=[])
                pre = self._preflight()
                self.assertNotIn("no_result", self._statuses(pre))
                self.assertFalse(pre["ok"])

    def test_lookup_exception_is_unavailable(self):
        def boom(path):
            raise RuntimeError("x")
        self._lookup = boom
        pre = self._preflight(log=[])
        self.assertEqual(set(self._statuses(pre)), {"unavailable"})

    def test_fingerprint_contradicting_release_is_conflict_not_ok(self):
        # Repair/AI callers: a confirmed fingerprint for a recording outside the
        # target release is now visible and must keep ok False.
        self.file_hits[self.paths[0]] = [_hit(OTHER_REC, 95, releases=(REL_B,), rg=RG_B)]
        pre = self._preflight()
        self.assertFalse(pre["ok"])
        self.assertFalse(pre["matching_decision"]["action_allowed"])


class GapRuleEdges(unittest.TestCase):
    REL = "11111111-1111-1111-1111-111111111111"
    RG = "22222222-2222-2222-2222-222222222222"
    REC = "33333333-3333-3333-3333-333333333333"

    def test_malformed_group_with_valid_release_is_subtype_a(self):
        gaps = library_identity_gaps([{"id": 1, "mb_albumid": self.REL, "mb_releasegroupid": "abc"}], [])
        self.assertEqual([r["id"] for r in gaps[ALBUM_RELEASE_WITHOUT_GROUP]], [1])

    def test_uppercase_and_padded_ids_count_as_present(self):
        gaps = library_identity_gaps(
            [{"id": 1, "mb_albumid": " " + self.REL.upper(), "mb_releasegroupid": self.RG.upper() + " "}],
            [{"id": 5, "album_id": 1, "mb_trackid": self.REC.upper()}])
        self.assertEqual(sum(len(v) for v in gaps.values()), 0)

    def test_malformed_ids_are_missing(self):
        for bad in (None, "", "  ", "0", "not-a-uuid", "{%s}" % REC_PLAIN, REC_PLAIN[:-1], REC_PLAIN.replace("-", "")):
            with self.subTest(bad=bad):
                gaps = library_identity_gaps(
                    [{"id": 1, "mb_albumid": bad, "mb_releasegroupid": bad}],
                    [{"id": 5, "album_id": 1, "mb_trackid": bad}, {"id": 6, "album_id": 0, "mb_trackid": bad}])
                self.assertEqual([r["id"] for r in gaps[ALBUM_WITHOUT_IDS]], [1])
                self.assertEqual([r["id"] for r in gaps[ALBUM_TRACK_WITHOUT_RECORDING]], [5])
                self.assertEqual([r["id"] for r in gaps[SINGLETON_WITHOUT_RECORDING]], [6])

    def test_string_album_id_links_item_to_album(self):
        gaps = library_identity_gaps([{"id": "7", "mb_albumid": "", "mb_releasegroupid": ""}],
                                     [{"id": 8, "album_id": "7", "mb_trackid": "", "track": 1}])
        self.assertEqual(gaps[ALBUM_WITHOUT_IDS][0]["tracks"], 1)
        self.assertEqual([r["id"] for r in gaps[ALBUM_TRACK_WITHOUT_RECORDING]], [8])

    def test_track_in_album_missing_group_is_listed_in_both_a_and_c(self):
        gaps = library_identity_gaps([{"id": 1, "mb_albumid": self.REL, "mb_releasegroupid": ""}],
                                     [{"id": 2, "album_id": 1, "mb_trackid": ""}])
        self.assertEqual(len(gaps[ALBUM_RELEASE_WITHOUT_GROUP]), 1)
        self.assertEqual(gaps[ALBUM_TRACK_WITHOUT_RECORDING][0]["album_mb_albumid"], self.REL)


REC_PLAIN = "33333333-3333-3333-3333-333333333333"


if __name__ == "__main__":
    unittest.main()
