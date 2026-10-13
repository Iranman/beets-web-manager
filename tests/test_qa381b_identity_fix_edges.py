"""QA re-check (#381, after 2683f1e): adversarial edges for the F1-F5 fixes.
Synthetic data only; no provider is called."""
import unittest
from unittest import mock

import backend.matching_service as ms
from backend.provider_boundary import ProviderOutcome, ProviderResult
from tests.test_import_folder_identity import (
    OTHER_REC, RECS, REL_A, REL_B, RG_A, RG_B, _FolderFixture, _hit, _tracks,
)

REL_A2 = "a3000000-0000-0000-0000-00000000000a"
REL_Z, RG_Z = "e0000000-0000-0000-0000-00000000000e", "e1000000-0000-0000-0000-00000000000e"
RG_F = ["f%d000000-0000-0000-0000-00000000000f" % i for i in range(1, 4)]


class FixEdges(_FolderFixture):
    # F4: every embedded Release ID must be checked, not only the top two.
    def test_third_embedded_release_in_other_group_blocks(self):
        self.releases[REL_A2] = (RG_A, _tracks(3))
        self.releases[REL_Z] = (RG_Z, _tracks(3, [OTHER_REC] * 3))
        self.embedded = {self.paths[0]: {"mb_albumid": REL_A},
                         self.paths[1]: {"mb_albumid": REL_A2},
                         self.paths[2]: {"mb_albumid": REL_Z}}
        result = self.identify()
        self.assertFalse(result["auto_import"]["eligible"], result["auto_import"])

    # F4 + F5: embedded IDs on a nested file.
    def _nested(self, tags):
        nested = self.folder / "CD2"
        nested.mkdir()
        p = nested / "04 Four.flac"
        p.write_bytes(b"synthetic")
        self.releases[REL_A] = (RG_A, _tracks(4))
        self.file_hits[str(p)] = [_hit(RECS[3])]
        self.embedded = {str(p): tags}
        return str(p)

    def test_nested_embedded_release_in_other_group_blocks(self):
        self.releases[REL_Z] = (RG_Z, _tracks(3, [OTHER_REC] * 3))
        self._nested({"mb_albumid": REL_Z})
        result = self.identify()
        self.assertFalse(result["auto_import"]["eligible"], result["auto_import"])
        top = {c["release_group_id"]: c for c in result["candidates"]}[RG_A]
        self.assertIn("embedded_release_group_conflict", top["evidence"]["conflicts"])

    def test_nested_embedded_group_tag_in_other_group_blocks(self):
        self._nested({"mb_releasegroupid": RG_Z})
        self.assertFalse(self.identify()["auto_import"]["eligible"])

    def test_nested_proven_file_with_matching_tags_stays_eligible(self):
        self._nested({"mb_albumid": REL_A, "mb_releasegroupid": RG_A})
        auto = self.identify()["auto_import"]
        self.assertTrue(auto["eligible"], auto)

    # F2: any release lookup failure, including a raised exception.
    def test_competitor_preflight_exception_blocks(self):
        self.search = [REL_A, REL_B]
        fetch = self._fetch

        def boom(rid, log=None):
            if rid == REL_B:
                raise RuntimeError("MusicBrainz exploded")
            return fetch(rid, log)
        self._fetch = boom
        self.assertNotEligible(self.identify(), "release_lookup_failed:1")

    def test_embedded_release_fetch_failure_blocks(self):
        self.embedded = {self.paths[0]: {"mb_albumid": REL_Z}}  # unknown -> fetch fails
        self.assertNotEligible(self.identify(), "release_lookup_failed:")

    # F3: fingerprint-indicated groups past the top-3 seed cap.
    def test_fingerprint_group_past_seed_cap_blocks(self):
        groups = [RG_A, *RG_F]
        self.file_hits = {p: [_hit(RECS[i]) | {"mb_releasegroupids": groups}] for i, p in enumerate(self.paths)}
        self.assertNotEligible(self.identify(), "fingerprint_group_not_evaluated")

    def test_unconfirmed_hit_group_does_not_block(self):
        # A low-score hit for another recording in RG_B is not proof of RG_B.
        self.file_hits[self.paths[0]] = [_hit(RECS[0], 95), _hit(OTHER_REC, 40, releases=(REL_B,), rg=RG_B)]
        auto = self.identify()["auto_import"]
        self.assertTrue(auto["eligible"], auto)

    def test_text_search_crash_is_unavailable_not_fatal(self):
        with mock.patch("backend.import_identity._text_search", side_effect=ValueError("bad json")):
            result = self.identify()
        self.assertEqual(result["evidence_capabilities"]["musicbrainz"], "unavailable")
        self.assertIn("musicbrainz_unavailable", result["warnings"])


class PreflightDefaultIsBase(_FolderFixture):
    """F1: without fingerprint_evidence the preflight must not even ask AcoustID
    per file, and must hand the evaluator no acoustid_hits."""

    def _run(self, **kw):
        lookup = mock.Mock(side_effect=self._lookup)
        built = {}
        real = ms.build_album_matching_decision

        def spy(**k):
            built["items"] = k["items"]
            return real(**k)
        patches = self._patches() + [
            mock.patch.object(ms, "_acoustid_lookup_cached_outcome", lookup),
            mock.patch.object(ms, "build_album_matching_decision", side_effect=spy),
        ]
        for p in patches:
            p.start()
        try:
            pre = ms._folder_release_preflight(str(self.folder), REL_A, **kw)
        finally:
            for p in reversed(patches):
                p.stop()
        return pre, lookup, built["items"]

    def test_default_never_calls_lookup_and_attaches_no_hits(self):
        pre, lookup, items = self._run()
        lookup.assert_not_called()
        self.assertTrue(items)
        self.assertFalse(any("acoustid_hits" in row for row in items))
        self.assertFalse(pre["matching_decision"]["action_allowed"])

    def test_opt_in_attaches_hits(self):
        pre, lookup, items = self._run(fingerprint_evidence=True)
        self.assertEqual(lookup.call_count, 3)
        self.assertTrue(all("acoustid_hits" in row for row in items))
        self.assertTrue(pre["matching_decision"]["action_allowed"])


if __name__ == "__main__":
    unittest.main()
