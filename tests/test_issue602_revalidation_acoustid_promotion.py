"""#602: revalidation promotes an unresolved identity (None) to proven (True)
only on fresh, non-contradicted AcoustID recording proof inside the selected
Release Group. Synthetic MusicBrainz/AcoustID data only; no provider is called.
"""
import unittest

import backend.import_review_service as irs

REL = "083b9009-2222-2222-2222-222222222222"
RG = "37cb4a72-1111-1111-1111-111111111111"
PREFLIGHT = {"ok": True, "matches": 1, "expected": 2, "audio_count": 2,
             "match_ratio": 1.0, "source_match_ratio": 1.0}


def _comparison(*statuses):
    return {"identity_validated": True, "selected_release_group_id": RG,
            "representative_release_id": REL, "release_artist": "Synthetic Artist",
            "comparison": [{"status": s} for s in statuses], "mb_track_count": 2,
            "local_track_count": len(statuses), "matched_count": len(statuses)}


def _match(statuses, stored=None, preflight=None, suggestion_extra=None):
    suggestion = {"identity_validated": stored, **(suggestion_extra or {})}
    return irs._import_review_build_revalidated_match(
        {"suggestion": suggestion}, _comparison(*statuses), dict(PREFLIGHT, **(preflight or {})))


class FreshAcoustIDPromotesUnresolved(unittest.TestCase):
    def test_fresh_matching_proof_promotes_none_to_true(self):
        m = _match(["matched", "acoustid_verified"])
        self.assertIs(m["identity_validated"], True)
        self.assertTrue(m["is_importable"])
        self.assertEqual(m["release_group_id"], RG)

    def test_missing_proof_stays_unresolved(self):
        for statuses in (["matched"], ["matched", "fuzzy"], ["matched", "missing"]):
            with self.subTest(statuses=statuses):
                self.assertIsNone(_match(statuses)["identity_validated"])

    def test_stale_stored_proof_is_ignored(self):
        # An acoustid_verified row stored from an earlier run is not fresh proof.
        m = _match(["matched"], suggestion_extra={
            "track_mapping": [{"status": "acoustid_verified"}],
            "preflight": {"acoustid_target_hits": 5}})
        self.assertIsNone(m["identity_validated"])

    def test_mismatching_proof_does_not_promote(self):
        cases = (
            (["acoustid_verified", "conflicting"], None),
            (["acoustid_verified"], {"acoustid_mismatch": True}),
            (["acoustid_verified"], {"matching_decision": {"action_allowed": False}}),
        )
        for statuses, preflight in cases:
            with self.subTest(statuses=statuses, preflight=preflight):
                m = _match(statuses, preflight=preflight)
                self.assertIsNot(m["identity_validated"], True)
                self.assertFalse(m["auto_fix_eligible"])

    def test_false_is_never_promoted(self):
        # Non-artist stored False keeps the #597 behaviour (unresolved, not proof).
        m = _match(["acoustid_verified"], stored=False, suggestion_extra={
            "candidate_identity_error": "Release Group lookup returned no usable releases."})
        self.assertIsNone(m["identity_validated"])
        # Stored artist rejection stays a rejection.
        m = _match(["acoustid_verified"], stored=False, suggestion_extra={
            "candidate_identity_error": irs._AI_ARTIST_REJECTION_PREFIX + " source X vs Y"})
        self.assertIs(m["identity_validated"], False)
        self.assertFalse(m["is_importable"])

    def test_membership_failure_is_not_overridden(self):
        comparison = dict(_comparison("acoustid_verified"), identity_validated=False,
                          candidate_identity_error="Representative Release ID rejected")
        m = irs._import_review_build_revalidated_match(
            {"suggestion": {"identity_validated": None}}, comparison, dict(PREFLIGHT))
        self.assertIs(m["identity_validated"], False)


if __name__ == "__main__":
    unittest.main()
