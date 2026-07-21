import json
import unittest

from backend.matching_contract import (
    AiState,
    build_recording_matching_decision,
)


RGID = "11111111-1111-1111-1111-111111111111"
RELEASE_ID = "22222222-2222-2222-2222-222222222222"
RECORDING_ID = "33333333-3333-3333-3333-333333333333"


def _local(**overrides):
    data = {
        "title": "Local Mistag",
        "artist": "Example Artist",
        "albumartist": "Example Artist",
        "album": "Correct Album",
        "year": "1988",
        "track": 3,
        "disc": 1,
        "duration_seconds": 180,
        "filename": "03 - Local Mistag.flac",
        "source_path": "/data/media/music/Example Artist/Correct Album/03 - Local Mistag.flac",
        "mb_trackid": "",
        "mb_albumid": "",
        "mb_releasegroupid": "",
    }
    data.update(overrides)
    return data


def _candidate(**overrides):
    data = {
        "candidate_index": 0,
        "source": "acoustid",
        "score": 97,
        "mb_trackid": RECORDING_ID,
        "title": "Correct Title",
        "artist": "Example Artist",
        "album": "Correct Album",
        "year": "1988",
        "mb_albumid": RELEASE_ID,
        "mb_releasegroupid": RGID,
        "_match_score": {"total": 0.94, "source": "acoustid"},
    }
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


class MatchingContractIdentityTests(unittest.TestCase):
    def test_release_group_id_remains_canonical_when_release_id_is_present(self):
        decision = build_recording_matching_decision(
            current=_local(title="Correct Title"),
            candidate=_candidate(),
            selected_release=_release(),
        )

        payload = decision.to_dict()
        self.assertEqual(payload["identity"]["release_group_id"], RGID)
        self.assertEqual(payload["identity"]["release_id"], RELEASE_ID)
        self.assertNotEqual(payload["identity"]["release_group_id"], RELEASE_ID)

    def test_release_id_is_not_substituted_into_missing_release_group_field(self):
        decision = build_recording_matching_decision(
            current=_local(title="Correct Title"),
            candidate=_candidate(mb_releasegroupid=""),
            selected_release=_release(mb_releasegroupid=""),
        )

        payload = decision.to_dict()
        self.assertEqual(payload["identity"]["release_id"], RELEASE_ID)
        self.assertEqual(payload["identity"]["release_group_id"], "")
        self.assertIn("release_group_id_missing", payload["evidence"]["missing"])

    def test_missing_identifiers_are_represented_honestly(self):
        decision = build_recording_matching_decision(
            current=_local(title="Correct Title"),
            candidate=_candidate(mb_trackid="", mb_albumid="", mb_releasegroupid=""),
            selected_release={},
        )

        payload = decision.to_dict()
        self.assertEqual(payload["identity"]["recording_ids"], [])
        self.assertEqual(payload["identity"]["release_id"], "")
        self.assertEqual(payload["identity"]["release_group_id"], "")
        self.assertEqual(payload["decision"]["safety_result"], "No verified match")


class MatchingContractAiTests(unittest.TestCase):
    def test_ai_unavailable_no_key_is_explicit_and_does_not_erase_candidate(self):
        decision = build_recording_matching_decision(
            current=_local(title="Correct Title"),
            candidate=_candidate(),
            selected_release=_release(),
            ai_state=AiState(
                configured=False,
                attempted=False,
                available=False,
                unavailability_reason="OPENAI_API_KEY not configured",
            ),
        )

        payload = decision.to_dict()
        self.assertFalse(payload["ai"]["configured"])
        self.assertFalse(payload["ai"]["available"])
        self.assertIn("OPENAI_API_KEY not configured", payload["ai"]["unavailability_reason"])
        self.assertEqual(payload["identity"]["recording_ids"], [RECORDING_ID])

    def test_ai_401_or_403_is_explicit_and_keeps_deterministic_identity(self):
        decision = build_recording_matching_decision(
            current=_local(title="Correct Title"),
            candidate=_candidate(),
            selected_release=_release(),
            ai_state=AiState(
                configured=True,
                attempted=True,
                available=False,
                unavailability_reason="the AI provider rejected the API key (invalid or unauthorized)",
            ),
        )

        payload = decision.to_dict()
        self.assertFalse(payload["ai"]["available"])
        self.assertIn("rejected the API key", payload["ai"]["unavailability_reason"])
        self.assertEqual(payload["identity"]["recording_ids"], [RECORDING_ID])

    def test_ai_timeout_or_rate_limit_is_explicit_and_keeps_deterministic_identity(self):
        for reason in (
            "the AI provider request timed out",
            "the AI provider rate-limited this request",
        ):
            with self.subTest(reason=reason):
                decision = build_recording_matching_decision(
                    current=_local(title="Correct Title"),
                    candidate=_candidate(),
                    selected_release=_release(),
                    ai_state=AiState(
                        configured=True,
                        attempted=True,
                        available=False,
                        unavailability_reason=reason,
                    ),
                )
                payload = decision.to_dict()
                self.assertIn(reason, payload["ai"]["unavailability_reason"])
                self.assertEqual(payload["identity"]["recording_ids"], [RECORDING_ID])

    def test_incorrect_ai_preference_cannot_replace_deterministic_identity(self):
        other_recording = "44444444-4444-4444-4444-444444444444"
        decision = build_recording_matching_decision(
            current=_local(title="Correct Title"),
            candidate=_candidate(),
            selected_release=_release(),
            ai_state=AiState(
                configured=True,
                attempted=True,
                available=True,
                contribution={"mb_trackid": other_recording, "confidence": "high"},
            ),
        )

        payload = decision.to_dict()
        self.assertEqual(payload["identity"]["recording_ids"], [RECORDING_ID])
        self.assertIn("ai_recording_conflict", payload["warnings"])


class MatchingContractDecisionTests(unittest.TestCase):
    def test_acoustid_confirmed_recording_with_different_title_remains_strong_with_warning(self):
        decision = build_recording_matching_decision(
            current=_local(title="Bad Radio Title", track=3),
            candidate=_candidate(title="Correct Title", source="acoustid"),
            selected_release=_release(track_number="3", duration_ms=181000),
        )

        payload = decision.to_dict()
        self.assertNotIn("title_conflict", payload["decision"]["conflicts"])
        self.assertIn("title_mismatch_with_strong_recording_evidence", payload["warnings"])
        self.assertNotEqual(payload["decision"]["safety_result"], "Conflict")

    def test_track_count_agreement_does_not_override_fingerprint_conflict(self):
        decision = build_recording_matching_decision(
            current=_local(title="Correct Title"),
            candidate=_candidate(fingerprint_status="mismatch", track_count_agreement=True),
            selected_release=_release(track_count=10),
        )

        payload = decision.to_dict()
        self.assertIn("fingerprint_conflict", payload["decision"]["conflicts"])
        self.assertTrue(payload["decision"]["review_required"])
        self.assertFalse(payload["decision"]["action_eligibility"]["attach_recording_id"])

    def test_conflicting_evidence_sets_review_required(self):
        decision = build_recording_matching_decision(
            current=_local(title="Correct Title", year="1988"),
            candidate=_candidate(),
            selected_release=_release(year="1993", date="1993-01-01"),
        )

        payload = decision.to_dict()
        self.assertIn("year_conflict", payload["decision"]["conflicts"])
        self.assertTrue(payload["decision"]["review_required"])

    def test_eligibility_and_ineligibility_include_reasons(self):
        eligible = build_recording_matching_decision(
            current=_local(title="Correct Title"),
            candidate=_candidate(),
            selected_release=_release(),
        ).to_dict()
        ineligible = build_recording_matching_decision(
            current=_local(title="Other", artist="Other Artist", albumartist="Other Artist"),
            candidate=_candidate(),
            selected_release=_release(),
        ).to_dict()

        self.assertTrue(eligible["decision"]["action_eligibility"]["attach_recording_id"])
        self.assertTrue(eligible["decision"]["eligibility_reason"])
        self.assertFalse(ineligible["decision"]["action_eligibility"]["attach_recording_id"])
        self.assertTrue(ineligible["decision"]["eligibility_reason"])

    def test_serialization_is_stable_and_does_not_include_provider_secrets(self):
        decision = build_recording_matching_decision(
            current=_local(title="Correct Title"),
            candidate=_candidate(
                provider_payload={"Authorization": "Bearer sk-secret"},
                token="sk-secret",
            ),
            selected_release=_release(),
        )

        first = json.dumps(decision.to_dict(), sort_keys=True)
        second = json.dumps(decision.to_dict(), sort_keys=True)
        self.assertEqual(first, second)
        self.assertNotIn("sk-secret", first)
        self.assertNotIn("Authorization", first)

    def test_import_review_compatibility_serializer_keeps_existing_candidate_keys(self):
        decision = build_recording_matching_decision(
            current=_local(title="Correct Title"),
            candidate=_candidate(candidate_index=2),
            selected_release=_release(),
        )

        payload = decision.to_review_recording_candidate()
        for key in (
            "candidate_index",
            "candidate_type",
            "mb_trackid",
            "mb_url",
            "musicbrainz_url",
            "title",
            "artist",
            "album",
            "year",
            "mb_albumid",
            "mb_releasegroupid",
            "decision",
            "conflicts",
            "recommended_action",
            "requires_confirmation",
            "safety_result",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["mb_releasegroupid"], RGID)
        self.assertEqual(payload["mb_albumid"], RELEASE_ID)


class MatchingContractRegressionFixtureTests(unittest.TestCase):
    def test_known_release_group_overrides_wrong_candidate_album_or_artist_label(self):
        decision = build_recording_matching_decision(
            current=_local(title="Correct Title", artist="Correct Artist", album="Correct Album"),
            candidate=_candidate(
                artist="Wrong Artist Label",
                album="Wrong Album Label",
                mb_releasegroupid=RGID,
            ),
            selected_release=_release(artist="Correct Artist", album="Correct Album"),
        )

        payload = decision.to_review_recording_candidate()
        self.assertEqual(payload["mb_releasegroupid"], RGID)
        self.assertEqual(payload["artist"], "Correct Artist")
        self.assertEqual(payload["album"], "Correct Album")
        self.assertNotIn("artist_conflict", payload["conflicts"])
        self.assertNotIn("album_conflict", payload["conflicts"])


if __name__ == "__main__":
    unittest.main()
