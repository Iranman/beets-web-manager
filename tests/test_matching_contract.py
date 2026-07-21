import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

from backend.matching_contract import (
    AiState,
    build_recording_matching_decision,
)


RGID = "11111111-1111-1111-1111-111111111111"
RELEASE_ID = "22222222-2222-2222-2222-222222222222"
RECORDING_ID = "33333333-3333-3333-3333-333333333333"
AI_RECORDING_ID = "44444444-4444-4444-4444-444444444444"
DETAILS_RECORDING_ID = "55555555-5555-5555-5555-555555555555"
EXISTING_RECORDING_ID = "66666666-6666-6666-6666-666666666666"
ACOUSTID_MAPPED_MISMATCH_ID = "77777777-7777-7777-7777-777777777777"
OTHER_RGID = "88888888-8888-8888-8888-888888888888"
OTHER_RELEASE_ID = "99999999-9999-9999-9999-999999999999"


def _local(**overrides):
    data = {
        "title": "Correct Title",
        "artist": "Example Artist",
        "albumartist": "Example Artist",
        "album": "Correct Album",
        "year": "1988",
        "track": 3,
        "disc": 1,
        "duration_seconds": 180,
        "filename": "03 - Correct Title.flac",
        "source_path": "/data/media/music/Example Artist/Correct Album/03 - Correct Title.flac",
        "mb_trackid": "",
        "mb_albumid": "",
        "mb_releasegroupid": "",
    }
    data.update(overrides)
    return data


def _match_score(**overrides):
    data = {
        "title_score": 0.95,
        "artist_score": 0.95,
        "album_score": 0.9,
        "year_score": 1.0,
        "mb_score": 0.9,
        "acoustid_bonus": 0.0,
        "source": "mb",
        "total": 0.94,
    }
    data.update(overrides)
    return data


def _candidate(**overrides):
    data = {
        "candidate_index": 0,
        "source": "mb",
        "score": 92,
        "mb_trackid": RECORDING_ID,
        "title": "Correct Title",
        "artist": "Example Artist",
        "album": "Correct Album",
        "year": "1988",
        "mb_albumid": RELEASE_ID,
        "mb_releasegroupid": RGID,
        "_match_score": _match_score(total=0.90, source="mb"),
    }
    data.update(overrides)
    return data


def _acoustid_candidate(**overrides):
    data = _candidate(
        source="acoustid",
        score=97,
        fingerprint_attempted=True,
        fingerprint_matched=True,
        fingerprint_status="matched",
        mapped_recording_id=RECORDING_ID,
        acoustid_id="ACOUSTID-XYZ",
    )
    data["_match_score"] = _match_score(total=0.94, source="acoustid", acoustid_bonus=0.22)
    data.update(overrides)
    return data


def _release(**overrides):
    data = {
        "mb_albumid": RELEASE_ID,
        "mb_releasegroupid": RGID,
        "album": "Correct Album",
        "artist": "Example Artist",
        "date": "1988-05-01",
        "year": "1988",
        "country": "PA",
        "medium_format": "CD",
        "medium_position": 1,
        "track_number": "3",
        "track_count": 10,
        "duration_ms": 180000,
    }
    data.update(overrides)
    return data


class MatchingContractAiProvenanceTests(unittest.TestCase):
    """Section 1: production must never assert false AI booleans."""

    def test_default_ai_state_is_not_evaluated_not_false(self):
        decision = build_recording_matching_decision(
            current=_local(), candidate=_candidate(), selected_release=_release()
        ).to_dict()
        ai = decision["ai"]
        self.assertFalse(ai["state_known"])
        self.assertEqual(ai["status"], "not_evaluated_at_this_boundary")
        self.assertIsNone(ai["configured"])
        self.assertIsNone(ai["attempted"])
        self.assertIsNone(ai["available"])
        self.assertEqual(decision["identity"]["resolved_recording_id"], RECORDING_ID)

    def test_ai_unavailable_no_key_is_explicit_and_does_not_erase_candidate(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(),
            selected_release=_release(),
            ai_state=AiState(
                state_known=True,
                configured=False,
                attempted=False,
                available=False,
                unavailability_reason="OPENAI_API_KEY not configured",
            ),
        ).to_dict()
        ai = decision["ai"]
        self.assertTrue(ai["state_known"])
        self.assertFalse(ai["configured"])
        self.assertFalse(ai["available"])
        self.assertIn("OPENAI_API_KEY not configured", ai["unavailability_reason"])
        self.assertEqual(decision["identity"]["resolved_recording_id"], RECORDING_ID)

    def test_ai_401_or_403_is_explicit_and_keeps_deterministic_identity(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(),
            selected_release=_release(),
            ai_state=AiState(
                state_known=True,
                configured=True,
                attempted=True,
                available=False,
                unavailability_reason="the AI provider rejected the API key (invalid or unauthorized)",
            ),
        ).to_dict()
        self.assertFalse(decision["ai"]["available"])
        self.assertIn("rejected the API key", decision["ai"]["unavailability_reason"])
        self.assertEqual(decision["identity"]["resolved_recording_id"], RECORDING_ID)

    def test_ai_timeout_or_rate_limit_is_explicit_and_keeps_deterministic_identity(self):
        for reason in (
            "the AI provider request timed out",
            "the AI provider rate-limited this request",
        ):
            with self.subTest(reason=reason):
                decision = build_recording_matching_decision(
                    current=_local(),
                    candidate=_candidate(),
                    selected_release=_release(),
                    ai_state=AiState(
                        state_known=True,
                        configured=True,
                        attempted=True,
                        available=False,
                        unavailability_reason=reason,
                    ),
                ).to_dict()
                self.assertIn(reason, decision["ai"]["unavailability_reason"])
                self.assertEqual(decision["identity"]["resolved_recording_id"], RECORDING_ID)

    def test_incorrect_ai_preference_cannot_replace_deterministic_identity(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(),
            selected_release=_release(),
            ai_state=AiState(
                state_known=True,
                configured=True,
                attempted=True,
                available=True,
                contribution={"mb_trackid": AI_RECORDING_ID, "confidence": "high"},
            ),
        ).to_dict()
        self.assertEqual(decision["identity"]["resolved_recording_id"], RECORDING_ID)
        self.assertIn("ai_recording_conflict", decision["warnings"])
        self.assertNotIn("ai_recording_conflict", decision["decision"]["conflicts"])


class MatchingContractRecordingIdentityTests(unittest.TestCase):
    """Section 2: every Recording ID source is preserved and conflicts are detected."""

    def test_candidate_and_details_recording_id_agree_resolves_identity(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(mb_trackid=RECORDING_ID),
            details={"recording_id": RECORDING_ID},
            selected_release=_release(),
        ).to_dict()
        self.assertEqual(decision["identity"]["resolved_recording_id"], RECORDING_ID)
        self.assertFalse(decision["identity"]["recording_id_source_conflict"])

    def test_candidate_and_details_recording_id_disagree_blocks_safe_attach(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(mb_trackid=RECORDING_ID),
            details={"recording_id": DETAILS_RECORDING_ID},
            selected_release=_release(),
        ).to_dict()
        self.assertTrue(decision["identity"]["recording_id_source_conflict"])
        self.assertEqual(decision["identity"]["resolved_recording_id"], "")
        self.assertIn("recording_id_source_conflict", decision["decision"]["conflicts"])
        self.assertFalse(decision["decision"]["action_eligibility"]["attach_without_review"])
        self.assertNotEqual(decision["decision"]["safety_key"], "safe")

    def test_conflicting_ids_still_display_evaluated_candidate_id_for_continuity(self):
        compat = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(mb_trackid=RECORDING_ID),
            details={"recording_id": DETAILS_RECORDING_ID},
            selected_release=_release(),
        ).to_review_recording_candidate()
        self.assertEqual(compat["mb_trackid"], RECORDING_ID)
        self.assertFalse(compat["action_eligibility"]["attach_without_review"])
        self.assertIn("recording_id_source_conflict", compat["conflicts"])

    def test_candidate_id_missing_details_id_present_resolves_to_details(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(mb_trackid=""),
            details={"recording_id": RECORDING_ID},
            selected_release=_release(),
        ).to_dict()
        self.assertEqual(decision["identity"]["resolved_recording_id"], RECORDING_ID)
        self.assertFalse(decision["identity"]["recording_id_source_conflict"])

    def test_candidate_id_present_details_id_missing_resolves_to_candidate(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(mb_trackid=RECORDING_ID),
            details={},
            selected_release=_release(),
        ).to_dict()
        self.assertEqual(decision["identity"]["resolved_recording_id"], RECORDING_ID)

    def test_existing_local_recording_id_disagreement_is_hard_conflict(self):
        decision = build_recording_matching_decision(
            current=_local(mb_trackid=EXISTING_RECORDING_ID),
            candidate=_candidate(mb_trackid=RECORDING_ID),
            selected_release=_release(),
        ).to_dict()
        self.assertIn("recording_id_conflict", decision["decision"]["conflicts"])
        self.assertEqual(decision["decision"]["safety_key"], "conflict")

    def test_ai_recording_disagreement_is_warning_not_conflict(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(mb_trackid=RECORDING_ID),
            selected_release=_release(),
            ai_state=AiState(
                state_known=True,
                configured=True,
                attempted=True,
                available=True,
                contribution={"mb_trackid": AI_RECORDING_ID, "confidence": "high"},
            ),
        ).to_dict()
        self.assertIn("ai_recording_conflict", decision["warnings"])
        self.assertNotIn("ai_recording_conflict", decision["decision"]["conflicts"])
        self.assertEqual(decision["identity"]["resolved_recording_id"], RECORDING_ID)

    def test_multiple_deterministic_ids_disagree(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(mb_trackid=RECORDING_ID),
            details={"recording_id": DETAILS_RECORDING_ID},
            selected_release=_release(),
        ).to_dict()
        sources = decision["identity"]["recording_id_sources"]
        self.assertEqual(sources["candidate"], RECORDING_ID)
        self.assertEqual(sources["musicbrainz_details"], DETAILS_RECORDING_ID)
        self.assertNotEqual(sources["candidate"], sources["musicbrainz_details"])
        self.assertEqual(decision["identity"]["resolved_recording_id"], "")

    def test_invalid_recording_ids_never_become_resolved_identity(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(mb_trackid="not-a-uuid"),
            details={"recording_id": "also-not-a-uuid"},
            selected_release=_release(),
        ).to_dict()
        self.assertEqual(decision["identity"]["resolved_recording_id"], "")
        self.assertEqual(decision["decision"]["safety_key"], "none")


class MatchingContractReleaseIdentityTests(unittest.TestCase):
    """Section 5: release / release-group provenance is preserved, not flattened."""

    def test_all_release_group_sources_agree(self):
        decision = build_recording_matching_decision(
            current=_local(), candidate=_candidate(), selected_release=_release()
        ).to_dict()
        self.assertEqual(decision["identity"]["release_group_id"], RGID)
        self.assertEqual(decision["identity"]["release_id"], RELEASE_ID)
        self.assertNotEqual(decision["identity"]["release_group_id"], RELEASE_ID)
        self.assertFalse(decision["identity"]["release_group_source_conflict"])

    def test_missing_release_group_id_is_not_substituted_from_release_id(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(mb_releasegroupid=""),
            selected_release=_release(mb_releasegroupid=""),
        ).to_dict()
        self.assertEqual(decision["identity"]["release_id"], RELEASE_ID)
        self.assertEqual(decision["identity"]["release_group_id"], "")
        self.assertIn("release_group_id_missing", decision["evidence"]["missing"])
        self.assertIn("release_group_id_missing", decision["warnings"])
        self.assertTrue(decision["decision"]["review_required"])
        self.assertFalse(decision["decision"]["action_eligibility"]["attach_without_review"])

    def test_candidate_and_details_rgid_disagree_forces_review(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(mb_releasegroupid=RGID),
            details={"mb_releasegroupid": OTHER_RGID},
            selected_release=_release(mb_releasegroupid=RGID),
        ).to_dict()
        self.assertTrue(decision["identity"]["release_group_source_conflict"])
        self.assertEqual(decision["identity"]["release_group_id"], "")
        self.assertIn("release_group_id_source_conflict", decision["decision"]["conflicts"])
        self.assertTrue(decision["decision"]["review_required"])
        self.assertNotEqual(decision["decision"]["safety_key"], "safe")

    def test_selected_release_rgid_disagrees_with_candidate_and_details(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(mb_releasegroupid=RGID),
            details={"mb_releasegroupid": RGID},
            selected_release=_release(mb_releasegroupid=OTHER_RGID),
        ).to_dict()
        self.assertTrue(decision["identity"]["release_group_source_conflict"])
        self.assertIn("release_group_id_source_conflict", decision["decision"]["conflicts"])

    def test_selected_release_not_linked_forces_review(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(),
            selected_release=_release(),
            linked_releases=[_release(mb_albumid=OTHER_RELEASE_ID)],
        ).to_dict()
        self.assertTrue(decision["identity"]["selected_release_not_linked"])
        self.assertIn("selected_release_not_linked", decision["decision"]["conflicts"])
        self.assertTrue(decision["decision"]["review_required"])

    def test_release_group_id_present_with_no_release_id(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(mb_albumid="", mb_releasegroupid=RGID),
            selected_release=_release(mb_albumid="", mb_releasegroupid=RGID),
        ).to_dict()
        self.assertEqual(decision["identity"]["release_group_id"], RGID)
        self.assertEqual(decision["identity"]["release_id"], "")

    def test_mb_albumids_conflicting_with_selected_release_is_visible_not_hidden(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(mb_albumid="", mb_albumids=[OTHER_RELEASE_ID]),
            selected_release=_release(),
        ).to_dict()
        sources = decision["identity"]["release_identity_sources"]
        self.assertEqual(sources["candidate_release_id"], OTHER_RELEASE_ID)
        self.assertEqual(sources["selected_release_id"], RELEASE_ID)
        self.assertNotEqual(sources["candidate_release_id"], sources["selected_release_id"])
        # A release-id difference alone (unlike release-group-id) is not a hard identity conflict.
        self.assertNotIn("recording_id_source_conflict", decision["decision"]["conflicts"])

    def test_multiple_valid_linked_releases_for_same_recording(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(),
            selected_release=_release(),
            linked_releases=[_release(), _release(country="GB")],
        ).to_dict()
        self.assertGreaterEqual(decision["evidence"]["musicbrainz"]["same_recording_release_count"], 2)
        self.assertIn("same_recording_on_multiple_releases", decision["warnings"])


class MatchingContractFingerprintProvenanceTests(unittest.TestCase):
    """Section 3: strong AcoustID evidence requires explicit fingerprint provenance."""

    def test_verified_acoustid_match_high_score_is_strong(self):
        decision = build_recording_matching_decision(
            current=_local(), candidate=_acoustid_candidate(), selected_release=_release()
        ).to_dict()
        self.assertGreaterEqual(decision["decision"]["acoustid_score"], 0.8)
        self.assertEqual(decision["decision"]["safety_key"], "safe")

    def test_acoustid_source_high_score_missing_verification_is_not_strong(self):
        candidate = _candidate(
            source="acoustid", score=97, _match_score=_match_score(total=0.94, source="acoustid")
        )
        decision = build_recording_matching_decision(
            current=_local(), candidate=candidate, selected_release=_release()
        ).to_dict()
        self.assertEqual(decision["decision"]["acoustid_score"], 0.0)
        self.assertNotIn("fingerprint_conflict", decision["decision"]["conflicts"])

    def test_acoustid_source_explicit_mismatch_is_hard_conflict(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_acoustid_candidate(fingerprint_status="mismatch"),
            selected_release=_release(),
        ).to_dict()
        self.assertIn("fingerprint_conflict", decision["decision"]["conflicts"])
        self.assertEqual(decision["decision"]["safety_key"], "conflict")
        self.assertFalse(decision["decision"]["action_eligibility"]["attach_without_review"])

    def test_acoustid_source_missing_recording_id_cannot_be_strong(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_acoustid_candidate(mb_trackid="", mapped_recording_id=""),
            selected_release=_release(),
        ).to_dict()
        self.assertEqual(decision["decision"]["safety_key"], "none")

    def test_musicbrainz_source_high_score_is_not_fingerprint_evidence(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(source="mb", score=95, _match_score=_match_score(total=0.5, source="mb")),
            selected_release=_release(),
        ).to_dict()
        self.assertEqual(decision["decision"]["acoustid_score"], 0.0)
        self.assertGreater(decision["decision"]["musicbrainz_search_score"], 0.9)

    def test_unknown_source_high_score_is_not_fingerprint_evidence(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(source="unknown", score=95, _match_score=_match_score(total=0.5, source="unknown")),
            selected_release=_release(),
        ).to_dict()
        self.assertEqual(decision["decision"]["acoustid_score"], 0.0)

    def test_acoustid_id_mapped_to_different_recording_id_is_not_strong(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_acoustid_candidate(mapped_recording_id=ACOUSTID_MAPPED_MISMATCH_ID),
            selected_release=_release(),
        ).to_dict()
        self.assertIn("fingerprint_recording_mismatch", decision["decision"]["conflicts"])
        self.assertEqual(decision["decision"]["safety_key"], "conflict")


class MatchingContractEvidenceAgreementTests(unittest.TestCase):
    """Section 4: missing duration/position must not count as agreement."""

    def test_title_mismatch_with_matching_duration_is_downgraded(self):
        decision = build_recording_matching_decision(
            current=_local(title="Wrong Radio Edit", track=None),
            candidate=_acoustid_candidate(title="Correct Title"),
            selected_release=_release(track_number="", duration_ms=180000),
        ).to_dict()
        self.assertNotIn("title_conflict", decision["decision"]["conflicts"])
        self.assertIn("title_mismatch_with_strong_recording_evidence", decision["warnings"])
        self.assertNotEqual(decision["decision"]["safety_result"], "Conflict")

    def test_title_mismatch_with_matching_position_is_downgraded(self):
        decision = build_recording_matching_decision(
            current=_local(title="Wrong Radio Edit", track=3, duration_seconds=None),
            candidate=_acoustid_candidate(title="Correct Title"),
            selected_release=_release(track_number="3", duration_ms=None),
        ).to_dict()
        self.assertNotIn("title_conflict", decision["decision"]["conflicts"])
        self.assertNotEqual(decision["decision"]["safety_result"], "Conflict")

    def test_title_mismatch_with_both_missing_stays_conflict(self):
        decision = build_recording_matching_decision(
            current=_local(title="Wrong Radio Edit", track=None, duration_seconds=None),
            candidate=_acoustid_candidate(title="Correct Title"),
            selected_release=_release(track_number="", duration_ms=None),
        ).to_dict()
        self.assertIn("title_conflict", decision["decision"]["conflicts"])
        self.assertTrue(decision["decision"]["review_required"])
        self.assertFalse(decision["decision"]["action_eligibility"]["attach_without_review"])

    def test_title_mismatch_with_unverified_acoustid_score_stays_conflict(self):
        decision = build_recording_matching_decision(
            current=_local(title="Wrong Radio Edit", track=3),
            candidate=_candidate(
                source="acoustid", score=97, title="Correct Title",
                _match_score=_match_score(total=0.94, source="acoustid"),
            ),
            selected_release=_release(track_number="3", duration_ms=180000),
        ).to_dict()
        self.assertIn("title_conflict", decision["decision"]["conflicts"])

    def test_title_mismatch_with_duration_conflict_stays_conflict(self):
        decision = build_recording_matching_decision(
            current=_local(title="Wrong Radio Edit", track=None, duration_seconds=60),
            candidate=_acoustid_candidate(title="Correct Title"),
            selected_release=_release(track_number="", duration_ms=180000),
        ).to_dict()
        self.assertIn("title_conflict", decision["decision"]["conflicts"])
        self.assertIn("duration_conflict", decision["decision"]["conflicts"])

    def test_title_mismatch_with_position_conflict_stays_conflict(self):
        decision = build_recording_matching_decision(
            current=_local(title="Wrong Radio Edit", track=5, duration_seconds=None),
            candidate=_acoustid_candidate(title="Correct Title"),
            selected_release=_release(track_number="3", duration_ms=None),
        ).to_dict()
        self.assertIn("title_conflict", decision["decision"]["conflicts"])


class MatchingContractEligibilityTests(unittest.TestCase):
    """Sections 6 & 7: no submission permission, honest safe-without-review semantics."""

    def test_action_eligibility_has_no_submit_metadata_field(self):
        decision = build_recording_matching_decision(
            current=_local(), candidate=_candidate(mb_trackid=""), selected_release=_release()
        ).to_dict()
        self.assertNotIn("submit_metadata", decision["decision"]["action_eligibility"])
        self.assertEqual(
            set(decision["decision"]["action_eligibility"].keys()),
            {"attach_without_review", "destructive_use"},
        )

    def test_safe_candidate_is_attach_without_review(self):
        decision = build_recording_matching_decision(
            current=_local(), candidate=_acoustid_candidate(), selected_release=_release()
        ).to_dict()
        self.assertTrue(decision["decision"]["action_eligibility"]["attach_without_review"])

    def test_conflict_candidate_is_not_attach_without_review(self):
        decision = build_recording_matching_decision(
            current=_local(mb_trackid=EXISTING_RECORDING_ID),
            candidate=_candidate(mb_trackid=RECORDING_ID),
            selected_release=_release(),
        ).to_dict()
        self.assertFalse(decision["decision"]["action_eligibility"]["attach_without_review"])

    def test_insufficient_evidence_is_not_attach_without_review(self):
        candidate = _candidate(source="mb", score=10, _match_score=_match_score(total=0.2, source="mb"))
        decision = build_recording_matching_decision(
            current=_local(), candidate=candidate, selected_release=_release()
        ).to_dict()
        self.assertFalse(decision["decision"]["action_eligibility"]["attach_without_review"])

    def test_missing_rgid_with_strong_recording_identity_is_not_attach_without_review(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_acoustid_candidate(mb_releasegroupid=""),
            selected_release=_release(mb_releasegroupid=""),
        ).to_dict()
        self.assertFalse(decision["decision"]["action_eligibility"]["attach_without_review"])
        self.assertEqual(decision["decision"]["safety_key"], "review")

    def test_ai_only_evidence_is_not_attach_without_review(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(mb_trackid=""),
            selected_release=_release(),
            ai_state=AiState(
                state_known=True, configured=True, attempted=True, available=True,
                contribution={"mb_trackid": AI_RECORDING_ID, "confidence": "high"},
            ),
        ).to_dict()
        self.assertFalse(decision["decision"]["action_eligibility"]["attach_without_review"])

    def test_fingerprint_mismatch_is_not_attach_without_review(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_acoustid_candidate(fingerprint_status="mismatch"),
            selected_release=_release(),
        ).to_dict()
        self.assertFalse(decision["decision"]["action_eligibility"]["attach_without_review"])

    def test_destructive_use_always_false(self):
        for candidate in (_acoustid_candidate(), _candidate(mb_trackid=""), _candidate(source="mb", score=10)):
            decision = build_recording_matching_decision(
                current=_local(), candidate=candidate, selected_release=_release()
            ).to_dict()
            self.assertFalse(decision["decision"]["action_eligibility"]["destructive_use"])


class MatchingContractReviewConfirmationParityTests(unittest.TestCase):
    """Section 8: requires_confirmation must mirror review_required."""

    def test_safe_has_no_confirmation_required(self):
        decision = build_recording_matching_decision(
            current=_local(), candidate=_acoustid_candidate(), selected_release=_release()
        ).to_dict()
        self.assertFalse(decision["decision"]["review_required"])
        self.assertFalse(decision["decision"]["requires_confirmation"])

    def test_conflict_requires_confirmation(self):
        decision = build_recording_matching_decision(
            current=_local(mb_trackid=EXISTING_RECORDING_ID),
            candidate=_candidate(mb_trackid=RECORDING_ID),
            selected_release=_release(),
        ).to_dict()
        self.assertTrue(decision["decision"]["review_required"])
        self.assertTrue(decision["decision"]["requires_confirmation"])

    def test_missing_evidence_requires_confirmation(self):
        decision = build_recording_matching_decision(
            current=_local(), candidate=_candidate(mb_trackid=""), selected_release=_release()
        ).to_dict()
        self.assertTrue(decision["decision"]["review_required"])
        self.assertTrue(decision["decision"]["requires_confirmation"])

    def test_missing_rgid_requires_confirmation(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_acoustid_candidate(mb_releasegroupid=""),
            selected_release=_release(mb_releasegroupid=""),
        ).to_dict()
        self.assertTrue(decision["decision"]["review_required"])
        self.assertTrue(decision["decision"]["requires_confirmation"])

    def test_ai_unavailable_but_deterministic_evidence_safe_needs_no_confirmation(self):
        decision = build_recording_matching_decision(
            current=_local(),
            candidate=_acoustid_candidate(),
            selected_release=_release(),
            ai_state=AiState(
                state_known=True, configured=False, attempted=False, available=False,
                unavailability_reason="OPENAI_API_KEY not configured",
            ),
        ).to_dict()
        self.assertFalse(decision["decision"]["review_required"])
        self.assertFalse(decision["decision"]["requires_confirmation"])

    def test_no_verified_recording_id_requires_confirmation(self):
        decision = build_recording_matching_decision(
            current=_local(), candidate=_candidate(mb_trackid="not-a-uuid"), selected_release=_release()
        ).to_dict()
        self.assertTrue(decision["decision"]["review_required"])
        self.assertTrue(decision["decision"]["requires_confirmation"])


class MatchingContractScoreSanitizationTests(unittest.TestCase):
    """Section 9: only allowlisted score fields may reach browser-visible JSON."""

    def _decision_with_score(self, match_score):
        return build_recording_matching_decision(
            current=_local(), candidate=_candidate(_match_score=match_score), selected_release=_release()
        )

    def test_authorization_header_is_dropped(self):
        decision = self._decision_with_score({"total": 0.9, "Authorization": "Bearer sk-secret"}).to_dict()
        rendered = json.dumps(decision)
        self.assertNotIn("Authorization", rendered)
        self.assertNotIn("sk-secret", rendered)

    def test_token_field_is_dropped(self):
        decision = self._decision_with_score({"total": 0.9, "token": "sk-secret-token"}).to_dict()
        self.assertNotIn("sk-secret-token", json.dumps(decision))

    def test_provider_payload_field_is_dropped(self):
        decision = self._decision_with_score(
            {"total": 0.9, "provider_payload": {"Authorization": "Bearer sk-secret"}}
        ).to_dict()
        self.assertNotIn("sk-secret", json.dumps(decision))

    def test_nested_mapping_is_dropped(self):
        decision = self._decision_with_score({"total": 0.9, "nested": {"a": {"b": "leak-value"}}}).to_dict()
        self.assertNotIn("leak-value", json.dumps(decision))

    def test_unexpected_list_is_dropped(self):
        decision = self._decision_with_score({"total": 0.9, "raw_responses": ["leak-1", "leak-2"]}).to_dict()
        self.assertNotIn("leak-1", json.dumps(decision))

    def test_nan_is_dropped(self):
        decision = self._decision_with_score({"total": float("nan"), "title_score": float("nan")}).to_dict()
        rendered = json.dumps(decision)
        self.assertNotIn("NaN", rendered)

    def test_infinity_is_dropped(self):
        decision = self._decision_with_score({"total": float("inf"), "mb_score": float("-inf")}).to_dict()
        rendered = json.dumps(decision)
        self.assertNotIn("Infinity", rendered)

    def test_score_breakdown_and_compatibility_response_agree(self):
        decision = self._decision_with_score({"total": 0.9, "secret_field": "leak-value"})
        payload = decision.to_dict()
        compat = decision.to_review_recording_candidate()
        self.assertNotIn("leak-value", json.dumps(payload))
        self.assertNotIn("leak-value", json.dumps(compat))
        self.assertNotIn("secret_field", payload["evidence"]["score_breakdown"])
        self.assertNotIn("secret_field", compat["score_breakdown"])


class MatchingContractCompatibilityRegressionTests(unittest.TestCase):
    """Section 10 & 12: realistic behavior/value regression coverage, not just key presence."""

    def test_safe_musicbrainz_candidate(self):
        compat = build_recording_matching_decision(
            current=_local(), candidate=_candidate(), selected_release=_release()
        ).to_review_recording_candidate()
        self.assertEqual(compat["mb_trackid"], RECORDING_ID)
        self.assertEqual(compat["mb_albumid"], RELEASE_ID)
        self.assertEqual(compat["mb_releasegroupid"], RGID)
        self.assertEqual(compat["source"], "mb")
        self.assertEqual(compat["match_method"], "mb")
        self.assertEqual(compat["safety_key"], "safe")
        self.assertEqual(compat["safety_result"], "Safe to attach")
        self.assertFalse(compat["requires_confirmation"])
        self.assertEqual(compat["conflicts"], [])
        self.assertEqual(compat["recommended_action"], "Attach Recording ID")

    def test_safe_verified_acoustid_candidate(self):
        compat = build_recording_matching_decision(
            current=_local(), candidate=_acoustid_candidate(), selected_release=_release()
        ).to_review_recording_candidate()
        self.assertEqual(compat["source"], "acoustid")
        self.assertGreaterEqual(compat["acoustid_score"], 0.8)
        self.assertEqual(compat["safety_key"], "safe")
        self.assertFalse(compat["requires_confirmation"])

    def test_missing_recording_id(self):
        compat = build_recording_matching_decision(
            current=_local(), candidate=_candidate(mb_trackid=""), selected_release=_release()
        ).to_review_recording_candidate()
        self.assertEqual(compat["mb_trackid"], "")
        self.assertEqual(compat["safety_key"], "none")
        self.assertEqual(compat["safety_result"], "No verified match")
        self.assertTrue(compat["requires_confirmation"])
        self.assertEqual(compat["recommended_action"], "Search MusicBrainz manually")

    def test_title_conflict(self):
        compat = build_recording_matching_decision(
            current=_local(title="Totally Unrelated Song Name"),
            candidate=_candidate(),
            selected_release=_release(),
        ).to_review_recording_candidate()
        self.assertIn("title_conflict", compat["conflicts"])
        self.assertEqual(compat["safety_key"], "conflict")
        self.assertTrue(compat["requires_confirmation"])

    def test_artist_conflict(self):
        compat = build_recording_matching_decision(
            current=_local(artist="Zzq Nonexistent Person", albumartist="Zzq Nonexistent Person"),
            candidate=_candidate(),
            selected_release=_release(),
        ).to_review_recording_candidate()
        self.assertIn("artist_conflict", compat["conflicts"])
        self.assertEqual(compat["safety_key"], "conflict")

    def test_album_conflict(self):
        compat = build_recording_matching_decision(
            current=_local(album="Zzq Nonexistent Record"), candidate=_candidate(), selected_release=_release()
        ).to_review_recording_candidate()
        self.assertIn("album_conflict", compat["conflicts"])
        self.assertEqual(compat["safety_key"], "review")
        self.assertTrue(compat["requires_confirmation"])

    def test_year_conflict(self):
        compat = build_recording_matching_decision(
            current=_local(year="1988"),
            candidate=_candidate(),
            selected_release=_release(year="1993", date="1993-01-01"),
        ).to_review_recording_candidate()
        self.assertIn("year_conflict", compat["conflicts"])
        self.assertTrue(compat["requires_confirmation"])

    def test_duration_conflict(self):
        compat = build_recording_matching_decision(
            current=_local(duration_seconds=60),
            candidate=_candidate(),
            selected_release=_release(duration_ms=180000),
        ).to_review_recording_candidate()
        self.assertIn("duration_conflict", compat["conflicts"])
        self.assertTrue(compat["requires_confirmation"])

    def test_release_group_id_conflict_local_vs_candidate(self):
        compat = build_recording_matching_decision(
            current=_local(mb_releasegroupid=OTHER_RGID), candidate=_candidate(), selected_release=_release()
        ).to_review_recording_candidate()
        self.assertIn("release_group_conflict", compat["conflicts"])
        self.assertEqual(compat["safety_key"], "conflict")

    def test_multiple_linked_releases(self):
        compat = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(),
            selected_release=_release(),
            linked_releases=[_release(), _release(country="GB")],
        ).to_review_recording_candidate()
        self.assertEqual(compat["same_recording_release_count"], 2)
        self.assertIn("same_recording_on_multiple_releases", compat["warnings"])

    def test_missing_linked_releases(self):
        compat = build_recording_matching_decision(
            current=_local(), candidate=_candidate(), selected_release=_release(), linked_releases=[]
        ).to_review_recording_candidate()
        self.assertEqual(compat["linked_releases"], [])
        self.assertEqual(compat["same_recording_release_count"], 0)

    def test_invalid_ids_are_not_treated_as_identity(self):
        compat = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(mb_trackid="xyz", mb_albumid="xyz", mb_releasegroupid="xyz"),
            selected_release=_release(mb_albumid="xyz", mb_releasegroupid="xyz"),
        ).to_review_recording_candidate()
        self.assertEqual(compat["mb_trackid"], "")
        self.assertEqual(compat["mb_albumid"], "")
        self.assertEqual(compat["mb_releasegroupid"], "")
        self.assertEqual(compat["safety_key"], "none")

    def test_no_selected_release(self):
        compat = build_recording_matching_decision(
            current=_local(), candidate=_candidate(mb_albumid="", mb_releasegroupid=""), selected_release={}
        ).to_review_recording_candidate()
        self.assertEqual(compat["selected_release"]["release_id"], "")
        self.assertEqual(compat["mb_albumid"], "")

    def test_review_required_with_no_named_conflict(self):
        candidate = _candidate(source="mb", score=10, _match_score=_match_score(total=0.2, source="mb"))
        compat = build_recording_matching_decision(
            current=_local(), candidate=candidate, selected_release=_release()
        ).to_review_recording_candidate()
        self.assertEqual(compat["conflicts"], [])
        self.assertEqual(compat["safety_key"], "review")
        self.assertTrue(compat["requires_confirmation"])
        self.assertTrue(compat["review_required"])

    def test_import_review_compatibility_serializer_keeps_existing_candidate_keys(self):
        payload = build_recording_matching_decision(
            current=_local(), candidate=_candidate(candidate_index=2), selected_release=_release()
        ).to_review_recording_candidate()
        for key in (
            "candidate_index", "candidate_type", "mb_trackid", "mb_url", "musicbrainz_url",
            "title", "artist", "album", "year", "mb_albumid", "mb_releasegroupid",
            "decision", "conflicts", "recommended_action", "requires_confirmation", "safety_result",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["mb_releasegroupid"], RGID)
        self.assertEqual(payload["mb_albumid"], RELEASE_ID)

    def test_authoritative_selected_release_details_override_untrusted_candidate_labels(self):
        # Renamed from the previous test_known_release_group_overrides_wrong_
        # candidate_album_or_artist_label: an RGID by itself does not
        # validate arbitrary labels -- it's the selected-release evidence
        # (sourced from MusicBrainz, not the untrusted candidate dict) that
        # is authoritative for display fields.
        decision = build_recording_matching_decision(
            current=_local(title="Correct Title", artist="Example Artist", album="Correct Album"),
            candidate=_candidate(artist="Wrong Artist Label", album="Wrong Album Label", mb_releasegroupid=RGID),
            selected_release=_release(artist="Example Artist", album="Correct Album"),
        )
        payload = decision.to_review_recording_candidate()
        self.assertEqual(payload["mb_releasegroupid"], RGID)
        self.assertEqual(payload["artist"], "Example Artist")
        self.assertEqual(payload["album"], "Correct Album")
        self.assertNotIn("artist_conflict", payload["conflicts"])
        self.assertNotIn("album_conflict", payload["conflicts"])


class MatchingContractMalformedInputTests(unittest.TestCase):
    """Section 13: the contract must fail closed, never invent identity."""

    def test_current_none_does_not_crash(self):
        payload = build_recording_matching_decision(
            current=None, candidate=_candidate(), selected_release=_release()
        ).to_dict()
        self.assertEqual(payload["input"]["local_title"], "")

    def test_candidate_none_does_not_crash(self):
        payload = build_recording_matching_decision(
            current=_local(), candidate=None, selected_release=_release()
        ).to_dict()
        self.assertEqual(payload["identity"]["resolved_recording_id"], "")

    def test_non_mapping_selected_release_does_not_crash(self):
        payload = build_recording_matching_decision(
            current=_local(), candidate=_candidate(), selected_release="not-a-mapping"
        ).to_dict()
        self.assertEqual(payload["identity"]["release_identity_sources"]["selected_release_id"], "")

    def test_linked_releases_as_string_does_not_crash(self):
        payload = build_recording_matching_decision(
            current=_local(), candidate=_candidate(), selected_release=_release(), linked_releases="abcdef"
        ).to_dict()
        self.assertEqual(payload["evidence"]["musicbrainz"]["linked_releases"], [])

    def test_malformed_linked_release_entries_are_skipped(self):
        payload = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(),
            selected_release=_release(),
            linked_releases=[_release(), "garbage", 42, None, ["nested"]],
        ).to_dict()
        self.assertEqual(len(payload["evidence"]["musicbrainz"]["linked_releases"]), 1)

    def test_bytes_values_do_not_crash(self):
        payload = build_recording_matching_decision(
            current=_local(title=b"Correct Title", artist=b"Example Artist"),
            candidate=_candidate(),
            selected_release=_release(),
        ).to_dict()
        self.assertEqual(payload["input"]["local_title"], "Correct Title")

    def test_negative_score_does_not_crash(self):
        payload = build_recording_matching_decision(
            current=_local(), candidate=_candidate(score=-50), selected_release=_release()
        ).to_dict()
        self.assertEqual(payload["decision"]["musicbrainz_search_score"], 0.0)

    def test_huge_score_does_not_crash(self):
        payload = build_recording_matching_decision(
            current=_local(), candidate=_candidate(score=10 ** 12), selected_release=_release()
        ).to_dict()
        self.assertEqual(payload["decision"]["musicbrainz_search_score"], 1.0)

    def test_nan_and_infinity_score_do_not_crash(self):
        for bad_score in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad_score=bad_score):
                payload = build_recording_matching_decision(
                    current=_local(), candidate=_candidate(score=bad_score), selected_release=_release()
                ).to_dict()
                self.assertEqual(payload["decision"]["musicbrainz_search_score"], 0.0)

    def test_invalid_uuids_do_not_crash(self):
        payload = build_recording_matching_decision(
            current=_local(mb_trackid="not-a-uuid", mb_albumid="also-bad", mb_releasegroupid="still-bad"),
            candidate=_candidate(mb_trackid="nope", mb_albumid="nope", mb_releasegroupid="nope"),
            selected_release=_release(mb_albumid="nope", mb_releasegroupid="nope"),
        ).to_dict()
        self.assertEqual(payload["identity"]["release_group_id"], "")

    def test_invalid_years_do_not_crash(self):
        payload = build_recording_matching_decision(
            current=_local(year="not-a-year"),
            candidate=_candidate(year="also-bad"),
            selected_release=_release(year="nope", date="nope"),
        ).to_dict()
        self.assertEqual(payload["decision"]["year_match"]["status"], "unknown")

    def test_invalid_durations_do_not_crash(self):
        payload = build_recording_matching_decision(
            current=_local(duration_seconds="not-a-duration"),
            candidate=_candidate(),
            selected_release=_release(duration_ms="also-not-a-duration"),
        ).to_dict()
        self.assertEqual(payload["decision"]["duration_match"]["status"], "unknown")

    def test_very_short_track_does_not_crash(self):
        payload = build_recording_matching_decision(
            current=_local(duration_seconds=0.01), candidate=_candidate(), selected_release=_release(duration_ms=1)
        ).to_dict()
        self.assertIn(payload["decision"]["duration_match"]["status"], {"yes", "tolerance", "conflict", "unknown"})

    def test_track_value_with_slash_does_not_crash(self):
        payload = build_recording_matching_decision(
            current=_local(track="3/10"), candidate=_candidate(), selected_release=_release(track_number="3")
        ).to_dict()
        self.assertIsNone(payload["decision"]["position_match"]["local"])

    def test_track_value_with_letter_does_not_crash(self):
        payload = build_recording_matching_decision(
            current=_local(track="A3"), candidate=_candidate(), selected_release=_release(track_number="3")
        ).to_dict()
        self.assertIsNone(payload["decision"]["position_match"]["local"])

    def test_duplicate_linked_releases_do_not_crash(self):
        payload = build_recording_matching_decision(
            current=_local(),
            candidate=_candidate(),
            selected_release=_release(),
            linked_releases=[_release(), _release(), _release()],
        ).to_dict()
        self.assertEqual(len(payload["evidence"]["musicbrainz"]["linked_releases"]), 3)


# ---------------------------------------------------------------------------
# Section 11: app-boundary integration tests. These import the real app.py,
# with BEETSDIR/LIB_PATH/etc. pointed at a throwaway temp directory (same
# isolation pattern as tests/test_ai_batch_retry_race.py) so module-level
# side effects never touch the real library, then call the actual
# _enrich_track_ai_candidate() / _compact_track_ai_candidate() functions --
# direct builder tests alone do not prove the production wiring is correct.
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[1]

_APP_TMP_ROOT = Path(tempfile.mkdtemp(prefix="beets_matching_contract_app_"))
unittest.addModuleCleanup(shutil.rmtree, str(_APP_TMP_ROOT), ignore_errors=True)

_APP_ENV_OVERRIDES = {
    "BEETSDIR": str(_APP_TMP_ROOT / "config"),
    "LIB_PATH": str(_APP_TMP_ROOT / "config" / "musiclibrary.blb"),
    "AI_BATCH_STATE_DIR": str(_APP_TMP_ROOT / "ai_batch_jobs"),
    "METADATA_CACHE_DIR": str(_APP_TMP_ROOT / "cache"),
    "BEETS_TRANSACTION_DIR": str(_APP_TMP_ROOT / "transactions"),
    "BEETS_WEB_AUTH_DISABLED": "1",
}
(_APP_TMP_ROOT / "config").mkdir(parents=True, exist_ok=True)
_app_env_patcher = mock.patch.dict(os.environ, _APP_ENV_OVERRIDES, clear=False)
_app_env_patcher.start()
unittest.addModuleCleanup(_app_env_patcher.stop)


def _import_app_for_boundary_tests():
    sys.path.insert(0, str(ROOT))
    import app as app_module
    return app_module


try:
    APP = _import_app_for_boundary_tests()
    _APP_IMPORT_ERROR = None
except Exception as _exc:  # pragma: no cover - environment-dependent
    APP = None
    _APP_IMPORT_ERROR = _exc


def _enrich(current, candidate, details=None, ai_state=None):
    # _fetch_mb_recording_details would otherwise make a real network call
    # whenever the candidate carries a valid mb_trackid and no details were
    # supplied -- exactly the call-site-1 production shape being tested.
    with mock.patch.object(APP, "_fetch_mb_recording_details", return_value={}):
        return APP._enrich_track_ai_candidate(dict(current), dict(candidate), details, ai_state=ai_state)


@unittest.skipIf(APP is None, f"app.py could not be imported for boundary tests: {_APP_IMPORT_ERROR}")
class AppBoundaryIntegrationTests(unittest.TestCase):
    def test_enrichment_and_compaction_round_trip_preserves_contract(self):
        enriched = _enrich(_local(), _acoustid_candidate())
        self.assertIn("matching_contract", enriched)
        compacted = APP._compact_track_ai_candidate(enriched)
        self.assertIn("matching_contract", compacted)
        self.assertEqual(compacted["mb_releasegroupid"], RGID)
        self.assertNotEqual(compacted["mb_releasegroupid"], RELEASE_ID)
        self.assertIn("review_required", compacted)
        self.assertIn("action_eligibility", compacted)
        self.assertIn("attach_without_review", compacted["action_eligibility"])
        self.assertNotIn("submit_metadata", compacted["action_eligibility"])

    def test_no_ai_state_at_first_boundary_is_truthful(self):
        enriched = _enrich(_local(), _candidate())
        ai_block = enriched["matching_contract"]["ai"]
        self.assertFalse(ai_block["state_known"])
        self.assertEqual(ai_block["status"], "not_evaluated_at_this_boundary")
        self.assertIsNone(ai_block["configured"])

    def test_real_ai_state_at_selected_boundary_is_truthful(self):
        ai_state = APP.AiState(
            state_known=True, configured=True, attempted=True, available=True,
            contribution={"mb_trackid": RECORDING_ID, "confidence": "high", "reason": "test"},
        )
        enriched = _enrich(_local(), _candidate(), ai_state=ai_state)
        ai_block = enriched["matching_contract"]["ai"]
        self.assertTrue(ai_block["state_known"])
        self.assertTrue(ai_block["configured"])
        self.assertTrue(ai_block["available"])

    def test_release_and_release_group_remain_distinct_through_compaction(self):
        enriched = _enrich(_local(), _candidate())
        compacted = APP._compact_track_ai_candidate(enriched)
        self.assertEqual(compacted["mb_albumid"], RELEASE_ID)
        self.assertEqual(compacted["mb_releasegroupid"], RGID)
        self.assertNotEqual(compacted["mb_albumid"], compacted["mb_releasegroupid"])

    def test_recording_id_provenance_survives_compaction(self):
        enriched = _enrich(_local(), _candidate(mb_trackid=RECORDING_ID), details={"recording_id": DETAILS_RECORDING_ID})
        compacted = APP._compact_track_ai_candidate(enriched)
        contract = compacted["matching_contract"]
        self.assertEqual(contract["identity"]["recording_id_sources"]["candidate"], RECORDING_ID)
        self.assertEqual(contract["identity"]["recording_id_sources"]["musicbrainz_details"], DETAILS_RECORDING_ID)
        self.assertTrue(contract["identity"]["recording_id_source_conflict"])
        self.assertFalse(compacted["action_eligibility"]["attach_without_review"])

    def test_conflicts_and_review_fields_survive_compaction(self):
        enriched = _enrich(_local(mb_trackid=EXISTING_RECORDING_ID), _candidate(mb_trackid=RECORDING_ID))
        compacted = APP._compact_track_ai_candidate(enriched)
        self.assertIn("recording_id_conflict", compacted["conflicts"])
        self.assertTrue(compacted["review_required"])
        self.assertTrue(compacted["requires_confirmation"])
        self.assertFalse(compacted["action_eligibility"]["attach_without_review"])

    def test_safe_without_review_field_survives_compaction(self):
        enriched = _enrich(_local(), _acoustid_candidate())
        compacted = APP._compact_track_ai_candidate(enriched)
        self.assertTrue(compacted["action_eligibility"]["attach_without_review"])
        self.assertFalse(compacted["review_required"])

    def test_unknown_score_fields_and_secrets_do_not_survive_compaction(self):
        candidate = _candidate(
            _match_score=_match_score(total=0.9, secret_field="leak-value", Authorization="Bearer sk-x")
        )
        enriched = _enrich(_local(), candidate)
        compacted = APP._compact_track_ai_candidate(enriched)
        rendered = json.dumps(compacted)
        self.assertNotIn("leak-value", rendered)
        self.assertNotIn("sk-x", rendered)

    def test_existing_frontend_required_fields_remain_available(self):
        enriched = _enrich(_local(), _acoustid_candidate())
        compacted = APP._compact_track_ai_candidate(enriched)
        for key in (
            "mb_trackid", "mb_albumid", "mb_releasegroupid", "selected_release",
            "linked_releases", "source", "match_method", "score", "match_total",
            "confidence", "confidence_score", "acoustid_score", "conflicts",
            "requires_confirmation", "safety_result", "safety_key", "recommended_action", "reason",
        ):
            self.assertIn(key, compacted)


if __name__ == "__main__":
    unittest.main()
