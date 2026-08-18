import unittest
import re
from typing import Dict, Any, List
from pathlib import Path

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

        current = {"mb_releasegroupid": rgid, "album": "Chxtape 5", "artist": "Tory Lanez"}
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

        decision_us = build_album_matching_decision(current=current, candidate=candidate_us)
        decision_eu = build_album_matching_decision(current=current, candidate=candidate_eu)

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
        """Release ID without RGID requires RGID resolution or marks review_required (FAILS CLOSED)."""
        rel_id = "11111111-1111-1111-1111-111111111111"
        candidate = {"mb_albumid": rel_id, "album": "Some Release"}
        decision = build_album_matching_decision(current={}, candidate=candidate)
        d = decision.to_dict()

        self.assertFalse(d["action_allowed"])
        self.assertFalse(d["identity_verified"])
        self.assertTrue(d["review_required"])
        self.assertEqual(d["reason_code"], "release_group_missing")
        self.assertIn("release_group_missing", d["conflicts"])

    def test_candidate_rgid_with_no_local_proof_requires_review(self):
        """Candidate supplying an RGID without local RGID or track proof is NOT identity_verified."""
        rgid = "511eea39-083a-4741-ae35-5a4d686ca2a6"
        current_album = {"album": "Unlabeled Folder", "artist": "Unknown Artist"}
        candidate_album = {"mb_releasegroupid": rgid, "album": "Specific Album", "artist": "Known Artist"}

        decision = build_album_matching_decision(current=current_album, candidate=candidate_album)
        d = decision.to_dict()

        self.assertFalse(d["identity_verified"])
        self.assertFalse(d["action_allowed"])
        self.assertTrue(d["review_required"])
        self.assertEqual(d["reason_code"], "review_required")

    def test_artist_ids_conflict_same_text_is_hard_conflict(self):
        """Differing MB Artist IDs with matching display text must produce a hard artist_id_conflict."""
        art1 = "11111111-1111-1111-1111-111111111111"
        art2 = "22222222-2222-2222-2222-222222222222"
        current = {"artist": "John Smith", "artist_id": art1}
        candidate = {"artist": "John Smith", "artist_id": art2, "mb_releasegroupid": "33333333-3333-3333-3333-333333333333"}

        decision = build_album_matching_decision(current=current, candidate=candidate)
        d = decision.to_dict()

        self.assertIn("artist_id_conflict", d["conflicts"])
        self.assertFalse(d["action_allowed"])

    def test_missing_metadata_does_not_score_as_perfect(self):
        """Missing album/artist title does not produce a perfect score or hide missing evidence."""
        current = {"artist": "Artist Name"}
        candidate = {"mb_releasegroupid": "33333333-3333-3333-3333-333333333333", "artist": "Artist Name"}

        decision = build_album_matching_decision(current=current, candidate=candidate)
        d = decision.to_dict()

        self.assertIn("album_title_missing", d["warnings"])
        self.assertLess(d["confidence"], 1.0)

    def test_duplicate_titles_in_tracklist_one_to_one(self):
        """One-to-one tracklist alignment prevents one MB track from satisfying multiple local tracks."""
        items = [
            {"title": "Track One", "track": 1},
            {"title": "Track One", "track": 2},
        ]
        mb_tracks = [
            {"title": "Track One", "track": 1},
        ]
        candidate = {"mb_releasegroupid": "33333333-3333-3333-3333-333333333333", "album": "EP", "artist": "Artist"}
        current = {"album": "EP", "artist": "Artist"}

        decision = build_album_matching_decision(current=current, candidate=candidate, items=items, mb_tracks=mb_tracks)
        d = decision.to_dict()

        # Matched count is 1, expected is 1, but items has 2 tracks -> tracklist_mismatch warning
        self.assertEqual(d["evidence"]["matched_count"], 1)

    def test_large_tracklist_mismatch_blocks_auto_action(self):
        """Severe tracklist mismatch (e.g. 12 local tracks vs 2 matched) blocks auto action."""
        items = [{"title": f"Song {i}", "track": i} for i in range(1, 13)]
        mb_tracks = [{"title": "Song 1", "track": 1}, {"title": "Song 2", "track": 2}]
        candidate = {"mb_releasegroupid": "33333333-3333-3333-3333-333333333333", "album": "Album", "artist": "Artist"}
        current = {"album": "Album", "artist": "Artist"}

        decision = build_album_matching_decision(current=current, candidate=candidate, items=items, mb_tracks=mb_tracks)
        d = decision.to_dict()

        self.assertIn("large_tracklist_mismatch", d["conflicts"])
        self.assertFalse(d["action_allowed"])

    def test_matching_decision_to_dict_fail_closed(self):
        """MatchingDecision.to_dict() fails closed if decision security keys are missing."""
        incomplete_decision = MatchingDecision(
            input={},
            identity={},
            evidence={},
            ai={},
            decision={},
            candidate={},
        )
        d = incomplete_decision.to_dict()

        self.assertFalse(d["action_allowed"])
        self.assertFalse(d["identity_verified"])
        self.assertTrue(d["review_required"])

    def test_rgid_only_retains_rgid_as_canonical_identity(self):
        """RGID specified retains RGID as canonical identity."""
        rgid = "511eea39-083a-4741-ae35-5a4d686ca2a6"
        current = {"mb_releasegroupid": rgid, "album": "Chxtape 5", "artist": "Artist"}
        candidate = {"mb_releasegroupid": rgid, "album": "Chxtape 5", "artist": "Artist"}
        decision = build_album_matching_decision(current=current, candidate=candidate)
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

    def test_text_only_12_of_12_not_identity_verified(self):
        """12/12 text/position track alignment without deterministic proof yields high confidence but action_allowed=false."""
        items = [{"title": f"Song {i}", "track": i} for i in range(1, 13)]
        mb_tracks = [{"title": f"Song {i}", "track": i} for i in range(1, 13)]
        candidate = {"mb_releasegroupid": "33333333-3333-3333-3333-333333333333", "album": "Famous Album", "artist": "Famous Artist"}
        current = {"album": "Famous Album", "artist": "Famous Artist"}  # No local_rg_id and no recording MBIDs

        decision = build_album_matching_decision(current=current, candidate=candidate, items=items, mb_tracks=mb_tracks)
        d = decision.to_dict()

        self.assertFalse(d["identity_verified"])
        self.assertTrue(d["review_required"])
        self.assertFalse(d["action_allowed"])
        self.assertEqual(d["safety_key"], "review")
        self.assertGreaterEqual(d["confidence"], 0.85)

    def test_deterministic_track_proof_positive(self):
        """Aligned tracks containing verified Recording IDs establish album-family identity."""
        rec_ids = [f"{i}{i}{i}{i}{i}{i}{i}{i}-1111-1111-1111-111111111111" for i in range(1, 4)]
        items = [{"title": f"Song {i}", "track": i, "mb_trackid": rec_ids[i-1]} for i in range(1, 4)]
        mb_tracks = [{"title": f"Song {i}", "track": i, "mb_trackid": rec_ids[i-1]} for i in range(1, 4)]
        candidate = {"mb_releasegroupid": "33333333-3333-3333-3333-333333333333", "album": "Verified Album", "artist": "Artist"}
        current = {"album": "Verified Album", "artist": "Artist"}

        decision = build_album_matching_decision(current=current, candidate=candidate, items=items, mb_tracks=mb_tracks)
        d = decision.to_dict()

        self.assertTrue(d["identity_verified"])
        self.assertFalse(d["review_required"])
        self.assertTrue(d["action_allowed"])
        self.assertEqual(d["safety_key"], "safe")

    def test_review_candidate_remains_visible(self):
        """Candidate requiring manual review is marked safety_key='review' and NOT suppressed or marked as conflict."""
        rgid = "511eea39-083a-4741-ae35-5a4d686ca2a6"
        current_album = {"album": "Unlabeled Folder", "artist": "Unknown Artist"}
        candidate_album = {"mb_releasegroupid": rgid, "album": "Specific Album", "artist": "Known Artist"}

        decision = build_album_matching_decision(current=current_album, candidate=candidate_album)
        d = decision.to_dict()

        self.assertFalse(d["action_allowed"])
        self.assertTrue(d["review_required"])
        self.assertEqual(d["safety_key"], "review")
        self.assertEqual(d["safety_result"], "Needs review")
        self.assertNotIn("release_group_conflict", d["conflicts"])

    def test_preflight_anti_circularity(self):
        """Preflight must never copy candidate values (release_title, RGID) onto the local/current side."""
        from app import _folder_release_preflight
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmpdir:
            artist_dir = Path(tmpdir) / "Local Artist"
            album_dir = artist_dir / "Unlabeled Folder"
            album_dir.mkdir(parents=True)
            dummy_file = album_dir / "track01.mp3"
            dummy_file.write_bytes(b"dummy mp3 data")

            mb_mock_data = {
                "ok": True,
                "release_title": "Candidate MB Release Title",
                "release_artist": "Candidate MB Artist",
                "release_group": "88888888-8888-8888-8888-888888888888",
                "tracks": [{"title": "Track 1", "track": 1}],
            }

            with patch("app._fetch_mb_release_tracklist", return_value=mb_mock_data):
                res = _folder_release_preflight(str(album_dir), "11111111-1111-1111-1111-111111111111")

            m_dec = res.get("matching_decision") or {}
            curr = m_dec.get("input", {}).get("current", {})

            # Assert current album title is local folder name, NOT candidate MB title
            self.assertNotEqual(curr.get("album"), "Candidate MB Release Title")
            self.assertEqual(curr.get("album"), "Unlabeled Folder")
            # Assert local RGID is empty, NOT copied from candidate release_group
            self.assertEqual(curr.get("mb_releasegroupid"), "")


if __name__ == "__main__":
    unittest.main()


