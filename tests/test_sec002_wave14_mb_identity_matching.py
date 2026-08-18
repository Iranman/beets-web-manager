import unittest
import re
from typing import Dict, Any, List

from backend.matching_contract import (
    build_recording_matching_decision,
    build_album_matching_decision,
    MatchingDecision,
    AiState,
    _uuid,
)


class TestSEC002Wave14MbIdentityMatching(unittest.TestCase):
    """Test suite for SEC-002 Wave 14 MusicBrainz Identity & Matching Consolidation."""

    def test_release_group_id_is_canonical_album_identity(self):
        """RGID is canonical album-family identity; Release ID is edition evidence."""
        rgid = "511eea39-083a-4741-ae35-5a4d686ca2a6"
        release_id_us = "11111111-1111-1111-1111-111111111111"
        release_id_eu = "22222222-2222-2222-2222-222222222222"

        candidate_us = {
            "mb_releasegroupid": rgid,
            "mb_albumid": release_id_us,
            "album": "Chxtape 5",
            "artist": "Tory Lanez",
        }
        candidate_eu = {
            "mb_releasegroupid": rgid,
            "mb_albumid": release_id_eu,
            "album": "Chxtape 5 (EU Edition)",
            "artist": "Tory Lanez",
        }

        decision_us = build_album_matching_decision(current={}, candidate=candidate_us)
        decision_eu = build_album_matching_decision(current={}, candidate=candidate_eu)

        d_us = decision_us.to_dict()
        d_eu = decision_eu.to_dict()

        # Both releases share the same canonical Release Group ID
        self.assertEqual(d_us["release_group_id"], rgid)
        self.assertEqual(d_eu["release_group_id"], rgid)
        # But have distinct Release IDs
        self.assertEqual(d_us["release_id"], release_id_us)
        self.assertEqual(d_eu["release_id"], release_id_eu)

    def test_same_title_different_release_groups_do_not_merge(self):
        """Same artist & album title with different RGIDs must remain distinct."""
        rgid_1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        rgid_2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

        current_album = {"mb_releasegroupid": rgid_1, "album": "Greatest Hits", "artist": "Queen"}
        candidate_album = {"mb_releasegroupid": rgid_2, "album": "Greatest Hits", "artist": "Queen"}

        decision = build_album_matching_decision(current=current_album, candidate=candidate_album)
        d = decision.to_dict()

        self.assertIn("release_group_conflict", d["conflicts"])
        self.assertFalse(d["action_allowed"])
        self.assertEqual(d["reason_code"], "release_group_conflict")

    def test_wrong_release_group_good_text_blocks_automatch(self):
        """Release from wrong Release Group with close text must block auto-match."""
        current = {"mb_releasegroupid": "11111111-1111-1111-1111-111111111111", "title": "Track One", "artist": "Artist A"}
        candidate = {
            "mb_releasegroupid": "99999999-9999-9999-9999-999999999999",
            "mb_trackid": "77777777-7777-7777-7777-777777777777",
            "title": "Track One",
            "artist": "Artist A",
        }

        decision = build_recording_matching_decision(current=current, candidate=candidate)
        d = decision.to_dict()

        self.assertIn("release_group_conflict", d["conflicts"])
        self.assertFalse(d["action_allowed"])

    def test_acoustid_conflict_overrides_high_ai_text_confidence(self):
        """AcoustID conflict blocks auto-match even if AI or text score is high."""
        current = {"title": "Song Title", "artist": "Artist Name"}
        candidate = {
            "mb_trackid": "11111111-1111-1111-1111-111111111111",
            "title": "Song Title",
            "artist": "Artist Name",
            "fingerprint_attempted": True,
            "fingerprint_matched": True,
            "fingerprint_status": "mismatch",
        }
        ai_state = AiState(state_known=True, configured=True, attempted=True, available=True, contribution={"confidence": "0.99"})

        decision = build_recording_matching_decision(current=current, candidate=candidate, ai_state=ai_state)
        d = decision.to_dict()

        self.assertIn("fingerprint_conflict", d["conflicts"])
        self.assertFalse(d["action_allowed"])

    def test_correct_acoustid_with_text_variation_is_allowed(self):
        """Correct AcoustID/Recording ID match with punctuation/suffix variance is allowed."""
        current = {"title": "Song Title (Remastered 2020) feat. Bob", "artist": "Artist Name", "duration_seconds": 180}
        candidate = {
            "mb_trackid": "11111111-1111-1111-1111-111111111111",
            "title": "Song Title",
            "artist": "Artist Name",
            "duration": 180,
            "mapped_recording_id": "11111111-1111-1111-1111-111111111111",
            "fingerprint_attempted": True,
            "fingerprint_matched": True,
            "fingerprint_status": "matched",
            "score": 95,
        }
        details = {
            "recording_id": "11111111-1111-1111-1111-111111111111",
            "recording_title": "Song Title",
            "recording_artist": "Artist Name",
            "mb_releasegroupid": "55555555-5555-5555-5555-555555555555",
            "mb_albumid": "66666666-6666-6666-6666-666666666666",
        }

        decision = build_recording_matching_decision(current=current, candidate=candidate, details=details)
        d = decision.to_dict()

        self.assertTrue(d["action_allowed"])
        self.assertNotIn("title_conflict", d["conflicts"])

    def test_ai_cannot_independently_authorize_action(self):
        """AI state alone cannot authorize action without deterministic identity."""
        current = {"title": "Unknown Track"}
        candidate = {"score": 50}
        ai_state = AiState(
            state_known=True,
            configured=True,
            attempted=True,
            available=True,
            contribution={"confidence": "1.0", "mb_trackid": "11111111-1111-1111-1111-111111111111"},
        )

        decision = build_recording_matching_decision(current=current, candidate=candidate, ai_state=ai_state)
        d = decision.to_dict()

        # AI cannot override missing deterministic match
        self.assertFalse(d["action_allowed"])

    def test_release_id_only_resolves_rgid_or_reviews(self):
        """Release ID without RGID requires RGID resolution or marks review_required."""
        rel_id = "11111111-1111-1111-1111-111111111111"
        candidate = {"mb_albumid": rel_id, "album": "Some Release"}
        decision = build_album_matching_decision(current={}, candidate=candidate)
        d = decision.to_dict()

        # Without RGID, release_group_id_missing warning is present
        self.assertIn("release_group_id_missing", d["warnings"])
        self.assertEqual(d["release_id"], rel_id)

    def test_rgid_only_retains_rgid_as_canonical_identity(self):
        """RGID specified retains RGID as canonical identity."""
        rgid = "511eea39-083a-4741-ae35-5a4d686ca2a6"
        candidate = {"mb_releasegroupid": rgid, "album": "Chxtape 5"}
        decision = build_album_matching_decision(current={}, candidate=candidate)
        d = decision.to_dict()

        self.assertEqual(d["release_group_id"], rgid)
        self.assertTrue(d["identity_verified"])

    def test_duplicate_cleanup_different_rgid_never_merges(self):
        """Albums with different Release Group IDs must never be merged automatically."""
        rgid_1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        rgid_2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

        album1 = {"mb_releasegroupid": rgid_1, "album": "Album 1"}
        album2 = {"mb_releasegroupid": rgid_2, "album": "Album 2"}

        decision = build_album_matching_decision(current=album1, candidate=album2)
        d = decision.to_dict()

        self.assertIn("release_group_conflict", d["conflicts"])
        self.assertFalse(d["action_allowed"])


if __name__ == "__main__":
    unittest.main()
