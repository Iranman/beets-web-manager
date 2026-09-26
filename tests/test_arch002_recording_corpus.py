"""ARCH-002 canonical single-recording evaluator corpus.

Every scenario states its expected safety outcome explicitly. Scenarios run
through the production serializer (build_recording_matching_decision) with a
real AcoustID hit set where relevant, and a subset also runs directly against
backend.matching.evaluate_recording_candidate to prove the serializer has no
decision logic of its own.
"""

import unittest

from backend.matching import (
    AcoustIDStatus,
    ConfidenceState,
    RecordingIdentityProof,
    acoustid_evidence_from_hits,
    best_recording_candidates,
    evaluate_recording_candidate,
)
from backend.matching_contract import AiState, build_recording_matching_decision

REC = "33333333-3333-3333-3333-333333333333"
OTHER = "66666666-6666-6666-6666-666666666666"
THIRD = "77777777-7777-7777-7777-777777777777"
RGID = "11111111-1111-1111-1111-111111111111"
RELEASE_ID = "22222222-2222-2222-2222-222222222222"


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
        "mb_trackid": "",
        "mb_albumid": "",
        "mb_releasegroupid": "",
    }
    data.update(overrides)
    return data


def _candidate(**overrides):
    data = {
        "candidate_index": 0,
        "source": "mb",
        "score": 92,
        "mb_trackid": REC,
        "title": "Correct Title",
        "artist": "Example Artist",
        "album": "Correct Album",
        "year": "1988",
        "mb_albumid": RELEASE_ID,
        "mb_releasegroupid": RGID,
        "_match_score": {"total": 0.9, "source": "mb"},
    }
    data.update(overrides)
    return data


def _release(**overrides):
    data = {
        "mb_albumid": RELEASE_ID,
        "mb_releasegroupid": RGID,
        "album": "Correct Album",
        "artist": "Example Artist",
        "year": "1988",
        "track_number": "3",
        "track_position": 3,
        "duration_ms": 181000,
    }
    data.update(overrides)
    return data


def _hit(recording_id, score):
    return {"mb_trackid": recording_id, "score": score}


def _decide(local=None, candidate=None, release=None, hits=None, ai_state=None):
    return build_recording_matching_decision(
        current=local or _local(),
        candidate=candidate or _candidate(),
        selected_release=release or _release(),
        acoustid_hits=hits,
        ai_state=ai_state,
    ).to_dict()


class RecordingCorpusAssertions(unittest.TestCase):
    def assertSafe(self, d, proof):
        self.assertEqual(d["decision"]["safety_key"], "safe", d["decision"]["eligibility_reason"])
        self.assertTrue(d["action_allowed"])
        self.assertTrue(d["decision"]["action_eligibility"]["attach_without_review"])
        self.assertFalse(d["decision"]["requires_confirmation"])
        self.assertEqual(d["decision"]["identity_proof"], proof)
        self.assertEqual(d["decision"]["conflicts"], [])

    def assertReview(self, d, reason=None):
        self.assertEqual(d["decision"]["safety_key"], "review", d["decision"]["conflicts"])
        self.assertFalse(d["action_allowed"])
        self.assertFalse(d["decision"]["action_eligibility"]["attach_without_review"])
        self.assertTrue(d["decision"]["requires_confirmation"])
        self.assertEqual(d["decision"]["hard_conflicts"], [])
        if reason:
            self.assertIn(reason, d["decision"]["review_reasons"])

    def assertConflict(self, d, code):
        self.assertEqual(d["decision"]["safety_key"], "conflict")
        self.assertFalse(d["action_allowed"])
        self.assertFalse(d["decision"]["action_eligibility"]["attach_without_review"])
        self.assertIn(code, d["decision"]["hard_conflicts"])
        self.assertEqual(d["decision"]["confidence_state"], "conflict")


class TestArch002RecordingCorpus(RecordingCorpusAssertions):
    # 1
    def test_01_exact_existing_recording_id_match_is_safe(self):
        d = _decide(local=_local(mb_trackid=REC))
        self.assertSafe(d, "embedded_recording_id")
        self.assertEqual(d["decision"]["confidence_state"], "confirmed")

    # 2
    def test_02_existing_recording_id_conflict_is_hard_conflict(self):
        d = _decide(local=_local(mb_trackid=OTHER))
        self.assertConflict(d, "recording_id_conflict")

    # 3
    def test_03_no_existing_id_confirmed_acoustid_is_safe(self):
        d = _decide(hits=[_hit(REC, 96)])
        self.assertSafe(d, "acoustid_recording_id")
        self.assertEqual(d["evidence"]["acoustid"]["canonical_status"], "confirmed")
        self.assertGreaterEqual(d["decision"]["acoustid_score"], 0.8)

    # 4
    def test_04_no_existing_id_acoustid_conflict_is_hard_conflict(self):
        d = _decide(hits=[_hit(OTHER, 96)])
        self.assertConflict(d, "fingerprint_conflict")
        self.assertIn("fingerprint_recording_id_conflict", d["decision"]["hard_conflicts"])
        self.assertEqual(d["evidence"]["acoustid"]["evidence_recording_id"], OTHER)

    def test_04b_text_candidate_contradicted_by_fingerprint_is_not_no_result(self):
        """Regression for the Import Review merge loop: an MB text-search
        candidate used to be labelled fingerprint_status="no_result" even
        when AcoustID confidently identified a different recording."""
        candidate = _candidate(
            fingerprint_attempted=True, fingerprint_matched=False, fingerprint_status="no_result",
        )
        d = _decide(candidate=candidate, hits=[_hit(OTHER, 95)])
        self.assertConflict(d, "fingerprint_conflict")

    # 5
    def test_05_acoustid_unavailable_strong_text_requires_confirmation(self):
        d = _decide(hits=None)
        self.assertReview(d, "no_deterministic_recording_proof")
        self.assertEqual(d["decision"]["identity_proof"], "textual_support")
        self.assertEqual(d["decision"]["confidence_state"], "strong_match")
        self.assertEqual(d["decision"]["conflicts"], [])

    # 6
    def test_06_acoustid_no_result_is_not_a_conflict(self):
        d = _decide(hits=[])
        self.assertReview(d, "no_deterministic_recording_proof")
        self.assertEqual(d["decision"]["conflicts"], [])
        self.assertEqual(d["evidence"]["acoustid"]["canonical_status"], "no_result")

    def test_06b_no_result_with_embedded_id_match_is_safe(self):
        d = _decide(local=_local(mb_trackid=REC), hits=[])
        self.assertSafe(d, "embedded_recording_id")

    # 7
    def test_07_ambiguous_acoustid_requires_review(self):
        d = _decide(hits=[_hit(REC, 95), _hit(OTHER, 94)])
        self.assertReview(d, "acoustid_ambiguous")
        self.assertEqual(d["evidence"]["acoustid"]["canonical_status"], "ambiguous")

    def test_07b_ambiguous_acoustid_resolved_by_embedded_id(self):
        d = _decide(local=_local(mb_trackid=REC), hits=[_hit(REC, 95), _hit(OTHER, 94)])
        self.assertSafe(d, "embedded_recording_id")

    def test_07c_low_score_hits_only_are_ambiguous_not_conflict(self):
        d = _decide(hits=[_hit(OTHER, 55)])
        self.assertReview(d, "acoustid_ambiguous")

    # 8
    def test_08_same_title_wrong_artist_is_conflict(self):
        d = _decide(candidate=_candidate(artist="Someone Else Entirely"),
                    release=_release(artist="Someone Else Entirely"))
        self.assertConflict(d, "artist_conflict")

    def test_08b_wrong_artist_blocks_even_with_acoustid(self):
        d = _decide(candidate=_candidate(artist="Someone Else Entirely"),
                    release=_release(artist="Someone Else Entirely"), hits=[_hit(REC, 97)])
        self.assertConflict(d, "artist_conflict")

    # 9
    def test_09_same_title_different_recording_is_conflict(self):
        # Text is identical, but the fingerprint identifies another recording.
        d = _decide(candidate=_candidate(mb_trackid=OTHER), hits=[_hit(REC, 97)])
        self.assertConflict(d, "fingerprint_recording_id_conflict")

    # 10
    def test_10_live_version_of_studio_file_is_not_safe(self):
        d = _decide(
            candidate=_candidate(title="Correct Title (Live)"),
            release=_release(duration_ms=240000),
        )
        self.assertEqual(d["decision"]["safety_key"], "review")
        self.assertIn("version_qualifier_mismatch", d["warnings"])
        self.assertIn("duration_conflict", d["decision"]["conflicts"])
        self.assertFalse(d["action_allowed"])

    def test_10b_live_candidate_contradicted_by_fingerprint_is_conflict(self):
        d = _decide(candidate=_candidate(mb_trackid=OTHER, title="Correct Title (Live)"), hits=[_hit(REC, 96)])
        self.assertConflict(d, "fingerprint_conflict")

    def test_10c_remaster_qualifier_is_not_a_version_mismatch(self):
        d = _decide(candidate=_candidate(title="Correct Title - 2011 Remaster"), hits=[_hit(REC, 96)])
        self.assertNotIn("version_qualifier_mismatch", d["warnings"])

    # 11
    def test_11_duration_near_match_is_tolerated(self):
        d = _decide(release=_release(duration_ms=187000), hits=[_hit(REC, 96)])
        self.assertSafe(d, "acoustid_recording_id")
        self.assertEqual(d["evidence"]["duration"]["status"], "tolerance")

    # 12
    def test_12_duration_hard_mismatch_blocks_auto_attach(self):
        d = _decide(release=_release(duration_ms=260000), hits=[_hit(REC, 96)])
        self.assertReview(d, "duration_conflict")
        self.assertIn("duration_conflict", d["decision"]["conflicts"])

    # 13
    def test_13_filename_only_evidence_is_never_sufficient(self):
        d = _decide(local=_local(title="", filename="03 - Correct Title.flac"))
        self.assertReview(d, "no_deterministic_recording_proof")
        self.assertIn("filename_only_title_evidence", d["warnings"])
        self.assertGreaterEqual(d["evidence"]["filename_and_tags"]["filename_title_score"], 0.82)

    def test_13b_filename_corroborates_acoustid_when_title_tag_missing(self):
        d = _decide(local=_local(title="", filename="03 - Correct Title.flac"), hits=[_hit(REC, 96)])
        self.assertSafe(d, "acoustid_recording_id")

    # 14
    def test_14_unicode_and_punctuation_variation_is_not_a_conflict(self):
        local = _local(title="Déjà Vu!", artist="Beyoncé", albumartist="Beyoncé")
        cand = _candidate(title="Deja Vu", artist="Beyonce")
        rel = _release(artist="Beyonce")
        text_only = _decide(local=local, candidate=cand, release=rel)
        self.assertReview(text_only, "no_deterministic_recording_proof")
        self.assertEqual(text_only["decision"]["title_match"]["status"], "yes")
        self.assertEqual(text_only["decision"]["artist_match"]["status"], "yes")
        proven = _decide(local=local, candidate=cand, release=rel, hits=[_hit(REC, 96)])
        self.assertSafe(proven, "acoustid_recording_id")

    # 15
    def test_15_existing_wrong_mbid_beats_strong_fuzzy_text(self):
        cand = _candidate(_match_score={"total": 1.0, "source": "mb"}, score=100)
        d = _decide(local=_local(mb_trackid=OTHER), candidate=cand)
        self.assertConflict(d, "recording_id_conflict")

    def test_15b_existing_wrong_mbid_not_overridden_by_acoustid_or_ai(self):
        ai = AiState(state_known=True, configured=True, attempted=True, available=True,
                     contribution={"mb_trackid": REC, "confidence": "high"})
        d = _decide(local=_local(mb_trackid=OTHER), hits=[_hit(REC, 99)], ai_state=ai)
        self.assertConflict(d, "recording_id_conflict")

    # 16
    def test_16_multiple_plausible_recordings_are_never_auto_attached(self):
        first = _decide(candidate=_candidate(mb_trackid=REC))
        second = _decide(candidate=_candidate(mb_trackid=OTHER))
        self.assertReview(first)
        self.assertReview(second)
        hits = [_hit(REC, 93), _hit(OTHER, 93)]
        self.assertReview(_decide(candidate=_candidate(mb_trackid=REC), hits=hits), "acoustid_ambiguous")
        self.assertReview(_decide(candidate=_candidate(mb_trackid=OTHER), hits=hits), "acoustid_ambiguous")

    # 17
    def test_17_exact_recording_id_with_title_mismatch_needs_corroboration(self):
        corroborated = _decide(local=_local(mb_trackid=REC, title="Totally Different Words"))
        self.assertSafe(corroborated, "embedded_recording_id")
        self.assertIn("title_mismatch_with_strong_recording_evidence", corroborated["warnings"])
        uncorroborated = _decide(
            local=_local(mb_trackid=REC, title="Totally Different Words", track=9, duration_seconds=0),
        )
        self.assertConflict(uncorroborated, "title_conflict")

    # 18
    def test_18_acoustid_confirmation_with_title_mismatch_needs_corroboration(self):
        corroborated = _decide(local=_local(title="Totally Different Words"), hits=[_hit(REC, 96)])
        self.assertSafe(corroborated, "acoustid_recording_id")
        self.assertIn("title_mismatch_with_strong_recording_evidence", corroborated["warnings"])
        uncorroborated = _decide(
            local=_local(title="Totally Different Words", track=9, duration_seconds=0),
            hits=[_hit(REC, 96)],
        )
        self.assertConflict(uncorroborated, "title_conflict")


class TestCanonicalRecordingEvaluatorDirect(unittest.TestCase):
    """The same rules exercised directly on the canonical evaluator."""

    def _local(self, **o):
        data = {"title": "Song", "artist": "Band", "duration_seconds": 200.0, "track": 1}
        data.update(o)
        return data

    def _cand(self, **o):
        data = {"recording_id": REC, "title": "Song", "artist": "Band", "duration_seconds": 201.0,
                "track_position": 1, "release_group_id": RGID}
        data.update(o)
        return data

    def test_multi_source_deterministic(self):
        r = evaluate_recording_candidate(
            self._local(recording_id=REC), self._cand(),
            acoustid=acoustid_evidence_from_hits([_hit(REC, 97)], REC),
        )
        self.assertEqual(r.identity_proof, RecordingIdentityProof.MULTI_SOURCE_DETERMINISTIC)
        self.assertEqual(r.state, ConfidenceState.CONFIRMED)
        self.assertTrue(r.can_auto_attach())

    def test_unavailable_vs_no_result_vs_ambiguous(self):
        self.assertEqual(acoustid_evidence_from_hits(None, REC).status, AcoustIDStatus.UNAVAILABLE)
        self.assertEqual(acoustid_evidence_from_hits([], REC).status, AcoustIDStatus.NO_RESULT)
        self.assertEqual(
            acoustid_evidence_from_hits([_hit(REC, 90), _hit(OTHER, 89)], REC).status, AcoustIDStatus.AMBIGUOUS
        )
        for hits in (None, []):
            r = evaluate_recording_candidate(self._local(), self._cand(),
                                             acoustid=acoustid_evidence_from_hits(hits, REC))
            self.assertEqual(r.conflicts, [])
            self.assertFalse(r.can_auto_attach())

    def test_text_only_never_auto_attaches_regardless_of_heuristic(self):
        r = evaluate_recording_candidate(self._local(), self._cand(), heuristic_score=1.0)
        self.assertEqual(r.identity_proof, RecordingIdentityProof.TEXTUAL_SUPPORT)
        self.assertEqual(r.state, ConfidenceState.STRONG_MATCH)
        self.assertFalse(r.can_auto_attach())

    def test_missing_release_group_blocks_auto_attach(self):
        r = evaluate_recording_candidate(
            self._local(recording_id=REC), self._cand(release_group_id=""),
        )
        self.assertEqual(r.safety_key, "review")
        self.assertIn("release_group_id_missing", r.review_reasons)

    def test_ai_disagreement_is_warning_only(self):
        r = evaluate_recording_candidate(
            self._local(recording_id=REC), self._cand(), ai_recording_id=THIRD,
        )
        self.assertIn("ai_recording_conflict", r.warnings)
        self.assertTrue(r.can_auto_attach())

    def test_display_ordering_does_not_change_eligibility(self):
        safe = evaluate_recording_candidate(self._local(recording_id=REC), self._cand())
        text = evaluate_recording_candidate(self._local(), self._cand(recording_id=OTHER))
        conflict = evaluate_recording_candidate(self._local(recording_id=OTHER), self._cand())
        ordered = best_recording_candidates([conflict, text, safe])
        self.assertEqual([r.safety_key for r in ordered], ["safe", "review", "conflict"])
        self.assertFalse(text.can_auto_attach())

    def test_result_to_dict_exposes_canonical_fields(self):
        payload = evaluate_recording_candidate(self._local(), self._cand()).to_dict()
        for key in ("identity_proof", "confidence_state", "hard_conflicts", "warnings",
                    "review_reasons", "action_eligibility", "acoustid"):
            self.assertIn(key, payload)


class TestVerifyAudioAgainstRequest(unittest.TestCase):
    """Playlist/download verification: which audio may be imported."""

    def _v(self, hits, **kw):
        from backend.matching import verify_audio_against_request
        return verify_audio_against_request(hits, **kw)

    def _h(self, rid, score, title="Song", artist="Band"):
        return {"mb_trackid": rid, "score": score, "title": title, "artist": artist}

    def test_expected_id_confirmed_accepts(self):
        self.assertEqual(self._v([self._h(REC, 92)], expected_title="Song", expected_recording_id=REC)["decision"], "accept")

    def test_expected_id_tied_with_duplicate_recording_of_same_song_accepts(self):
        hits = [self._h(REC, 92), self._h(OTHER, 92)]
        self.assertEqual(self._v(hits, expected_title="Song", expected_artist="Band",
                                 expected_recording_id=REC)["decision"], "accept")

    def test_expected_id_tied_with_different_song_needs_review(self):
        hits = [self._h(REC, 92), self._h(OTHER, 92, title="Different Song Entirely")]
        self.assertEqual(self._v(hits, expected_title="Song", expected_recording_id=REC)["decision"], "review")

    def test_fingerprint_names_other_recording_of_same_song_is_review_not_accept(self):
        out = self._v([self._h(OTHER, 95)], expected_title="Song", expected_artist="Band", expected_recording_id=REC)
        self.assertEqual(out["decision"], "review")

    def test_fingerprint_names_different_song_rejects_despite_text(self):
        out = self._v([self._h(OTHER, 95, title="Other Song", artist="Other")],
                      expected_title="Song", expected_artist="Band", expected_recording_id=REC)
        self.assertEqual(out["decision"], "reject")

    def test_live_version_is_not_the_studio_request(self):
        out = self._v([self._h(OTHER, 95, title="Song (Live)")], expected_title="Song", expected_artist="Band")
        self.assertEqual(out["decision"], "reject")

    def test_low_score_hits_never_accept(self):
        self.assertEqual(self._v([self._h(REC, 60)], expected_title="Song", expected_recording_id=REC)["decision"], "review")

    def test_no_result_and_unavailable_are_review(self):
        self.assertEqual(self._v([], expected_title="Song")["decision"], "review")
        self.assertEqual(self._v(None, expected_title="Song")["decision"], "review")


if __name__ == "__main__":
    unittest.main()
