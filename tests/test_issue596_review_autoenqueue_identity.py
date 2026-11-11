"""#596: automated Import Review enqueue needs a proven artist identity
(``identity_validated is True``), the same rule as Import All (#584).
An unresolved identity (``None``) stays importable by hand only.

Synthetic MusicBrainz data only; no provider is called.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.import_review_service as irs
import backend.import_service as isvc

REL = "083b9009-2222-2222-2222-222222222222"
RG = "37cb4a72-1111-1111-1111-111111111111"


class EligibilityRequiresProvenIdentity(unittest.TestCase):
    def _eligibility(self, **identity):
        with tempfile.TemporaryDirectory() as tmp:
            track = Path(tmp) / "01.flac"
            track.write_bytes(b"")
            payload = {"path": tmp, "mb_albumid": REL, "mb_releasegroupid": RG,
                       "confidence_score": 0.95,
                       "selected_match": {"preflight_status": "passed", "is_importable": True,
                                          **identity.pop("selected_match", {})},
                       **identity}
            with mock.patch.object(isvc, "_pending_review_is_quality_upgrade", return_value=False), \
                    mock.patch.object(isvc, "_import_review_selected_source_files", return_value=[track]), \
                    mock.patch.object(isvc, "_cached_import_target_preview",
                                      return_value={"safe": True, "tracks_to_import_count": 1}):
                return isvc.evaluate_import_eligibility(payload)

    def test_unresolved_identity_is_not_auto_enqueued(self):
        for case in ({}, {"identity_validated": None},
                     {"selected_match": {"identity_validated": None}}):
            with self.subTest(case=case):
                result = self._eligibility(**case)
                self.assertFalse(result["eligible"])
                self.assertIn("Artist identity unresolved", "; ".join(result["blocking_reasons"]))

    def test_proven_identity_is_eligible(self):
        self.assertTrue(self._eligibility(selected_match={"identity_validated": True})["eligible"])
        self.assertTrue(self._eligibility(identity_validated=True)["eligible"])

    def test_rejected_identity_stays_blocked(self):
        result = self._eligibility(selected_match={"identity_validated": False})
        self.assertFalse(result["eligible"])
        self.assertNotIn("Artist identity unresolved", "; ".join(result["blocking_reasons"]))


class RevalidatedAutoFixRequiresProvenIdentity(unittest.TestCase):
    COMPARISON = {"identity_validated": True, "selected_release_group_id": RG,
                  "representative_release_id": REL, "release_artist": "Synthetic Artist",
                  "comparison": [{"status": "matched"}], "mb_track_count": 1,
                  "local_track_count": 1, "matched_count": 1}
    PREFLIGHT = {"ok": True, "matches": 1, "expected": 1, "audio_count": 1,
                 "match_ratio": 1.0, "source_match_ratio": 1.0}

    def _match(self, suggestion):
        return irs._import_review_build_revalidated_match(
            {"suggestion": suggestion}, dict(self.COMPARISON), dict(self.PREFLIGHT))

    def test_unresolved_is_importable_by_hand_but_not_auto_fixable(self):
        m = self._match({"identity_validated": None})
        self.assertIsNone(m["identity_validated"])
        self.assertTrue(m["is_importable"])
        self.assertFalse(m["auto_fix_eligible"])

    def test_proven_identity_is_auto_fixable(self):
        self.assertTrue(self._match({"identity_validated": True})["auto_fix_eligible"])

    def test_stored_non_artist_rejection_does_not_become_proof(self):
        # QA (#597): a stored False from a failed lookup, revalidated with no
        # folder artist to compare, must stay unresolved, not turn True.
        m = self._match({"identity_validated": False,
                         "candidate_identity_error": "Release Group lookup returned no usable releases."})
        self.assertIsNone(m["identity_validated"])
        self.assertTrue(m["is_importable"])
        self.assertFalse(m["auto_fix_eligible"])
        eligibility = EligibilityRequiresProvenIdentity._eligibility(
            self, selected_match={"identity_validated": m["identity_validated"]})
        self.assertFalse(eligibility["eligible"])
        self.assertIn("Artist identity unresolved", "; ".join(eligibility["blocking_reasons"]))

    def test_truthy_non_bool_is_not_proof(self):
        result = EligibilityRequiresProvenIdentity._eligibility(self, selected_match={"identity_validated": 1})
        self.assertFalse(result["eligible"])


if __name__ == "__main__":
    unittest.main()
