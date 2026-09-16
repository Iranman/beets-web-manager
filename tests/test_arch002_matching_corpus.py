import time
import unittest

from backend.matching import (
    AcoustIDStatus,
    ConfidenceState,
    DEFAULT_MATCH_POLICY,
    MatchPolicy,
    align_tracks_global,
    evaluate_release_group_candidate,
    normalize_title,
)


RG_311_VOYAGER = "11111111-1111-1111-1111-111111111111"
RG_OTHER = "99999999-9999-9999-9999-999999999999"
REL_STANDARD = "22222222-2222-2222-2222-222222222222"
REL_DELUXE = "33333333-3333-3333-3333-333333333333"


def _local(title, track, *, disc=1, artist="311", duration=180, recording_id="", acoustid_hits=None):
    return {
        "path": f"/source/{disc:02d}-{track:02d}-{title}.flac",
        "title": title,
        "artist": artist,
        "disc": disc,
        "track": track,
        "duration_seconds": duration,
        "recording_id": recording_id,
        "acoustid_hits": acoustid_hits,
    }


def _mb(title, track, rec, *, disc=1, artist="311", duration=180, track_id=None):
    return {
        "title": title,
        "artist": artist,
        "disc": disc,
        "track": track,
        "duration_ms": duration * 1000,
        "recording_id": rec,
        "track_id": track_id or f"track-{disc}-{track}-{rec}",
    }


def _candidate(*, rgid=RG_311_VOYAGER, title="Voyager", artist="311", release_id=REL_STANDARD, tracks=None, **extra):
    data = {
        "artist": artist,
        "artist_id": "artist-311",
        "release_group_id": rgid,
        "release_group_title": title,
        "release_id": release_id,
        "release_title": title,
        "date": "2019-07-12",
        "tracks": tracks if tracks is not None else [
            _mb("Crossfire", 1, "rec-crossfire"),
            _mb("Space and Time", 2, "rec-space-time"),
            _mb("Dodging Raindrops", 3, "rec-raindrops"),
        ],
    }
    data.update(extra)
    return data


class TestArch002KnownFailures(unittest.TestCase):
    def test_correct_release_group_cannot_display_unrelated_album_metadata(self):
        local_album = {"artist": "311", "album": "Voyager"}
        candidate = _candidate(
            display_artist="Vitalic",
            display_album="OK Cowboy",
        )

        result = evaluate_release_group_candidate(local_album, candidate)

        self.assertEqual(result.suggested_identity["release_group_id"], RG_311_VOYAGER)
        self.assertEqual(result.suggested_identity["artist"], "311")
        self.assertEqual(result.suggested_identity["release_group_title"], "Voyager")
        self.assertIn("candidate_metadata_mixed", result.review_reasons)
        self.assertEqual(result.state, ConfidenceState.CONFLICT)

    def test_correct_release_group_with_plausible_tracks_does_not_become_no_tracks_match(self):
        local_tracks = [
            _local("Crossfire", 1),
            _local("Space & Time", 2),
            _local("Dodging Raindrops", 3),
        ]
        result = evaluate_release_group_candidate(
            {"artist": "311", "album": "Voyager", "embedded_release_group_id": RG_311_VOYAGER},
            _candidate(),
            local_tracks=local_tracks,
        )

        self.assertEqual(result.release_group_status, "validated")
        self.assertEqual(result.track_alignment.matched_count, 3)
        self.assertNotIn("no_tracks_matched", result.review_reasons)

    def test_title_differs_but_acoustid_confirms_recording(self):
        local_tracks = [
            _local(
                "Wrong Local Title",
                1,
                acoustid_hits=[{"recording_id": "rec-crossfire", "score": 96, "acoustid": "aid-1"}],
            )
        ]
        result = evaluate_release_group_candidate(
            {"artist": "311", "album": "Voyager"},
            _candidate(tracks=[_mb("Crossfire", 1, "rec-crossfire")]),
            local_tracks=local_tracks,
            trust_model="fresh_reviewed_import",
        )

        assignment = result.track_alignment.assignments[0]
        self.assertEqual(assignment.acoustid_status, AcoustIDStatus.CONFIRMED)
        self.assertIn("title_differs", assignment.warnings)
        self.assertEqual(assignment.status, "matched")
        self.assertNotIn("acoustid_conflict", result.conflicts)

    def test_acoustid_conflict_blocks_text_match(self):
        local_tracks = [
            _local(
                "Crossfire",
                1,
                acoustid_hits=[{"recording_id": "rec-space-time", "score": 97, "acoustid": "aid-2"}],
            )
        ]
        result = evaluate_release_group_candidate(
            {"artist": "311", "album": "Voyager"},
            _candidate(tracks=[_mb("Crossfire", 1, "rec-crossfire"), _mb("Space and Time", 2, "rec-space-time")]),
            local_tracks=local_tracks,
        )

        self.assertEqual(result.state, ConfidenceState.CONFLICT)
        self.assertIn("acoustid_conflict", result.conflicts)
        self.assertEqual(result.track_alignment.assignments[0].acoustid_status, AcoustIDStatus.CONFLICT)

    def test_missing_acoustid_is_not_a_conflict(self):
        local_tracks = [_local("Crossfire", 1, acoustid_hits=[])]
        result = evaluate_release_group_candidate(
            {"artist": "311", "album": "Voyager"},
            _candidate(tracks=[_mb("Crossfire", 1, "rec-crossfire")]),
            local_tracks=local_tracks,
        )

        self.assertEqual(result.track_alignment.assignments[0].acoustid_status, AcoustIDStatus.NO_RESULT)
        self.assertNotIn("acoustid_conflict", result.conflicts)
        self.assertIn("acoustid_no_result", result.missing_evidence)

    def test_two_files_cannot_claim_one_musicbrainz_track(self):
        local_tracks = [
            _local("Intro", 1, duration=60),
            _local("Intro", 2, duration=62),
        ]
        mb_tracks = [
            _mb("Intro", 1, "rec-intro-a", duration=60, track_id="mb-track-a"),
            _mb("Intro", 2, "rec-intro-b", duration=62, track_id="mb-track-b"),
        ]

        alignment = align_tracks_global(local_tracks, mb_tracks)

        target_ids = [a.target_track_id for a in alignment.assignments]
        self.assertEqual(len(target_ids), len(set(target_ids)))
        self.assertEqual(alignment.matched_count, 2)

    def test_bonus_track_preserves_release_group_and_marks_exact_release_uncertain(self):
        local_tracks = [
            _local("Crossfire", 1),
            _local("Space and Time", 2),
            _local("Voyager Bonus", 3),
        ]
        standard_tracks = [_mb("Crossfire", 1, "rec-crossfire"), _mb("Space and Time", 2, "rec-space-time")]
        deluxe_tracks = standard_tracks + [_mb("Voyager Bonus", 3, "rec-bonus")]
        result = evaluate_release_group_candidate(
            {"artist": "311", "album": "Voyager", "embedded_release_group_id": RG_311_VOYAGER},
            _candidate(tracks=standard_tracks),
            local_tracks=local_tracks,
            release_candidates=[
                {"release_id": REL_STANDARD, "release_group_id": RG_311_VOYAGER, "tracks": standard_tracks, "release_title": "Voyager"},
                {"release_id": REL_DELUXE, "release_group_id": RG_311_VOYAGER, "tracks": deluxe_tracks, "release_title": "Voyager (Deluxe)"},
            ],
        )

        self.assertEqual(result.release_group_status, "validated")
        self.assertEqual(result.release_match.release_id, REL_DELUXE)
        self.assertIn("exact_release_ambiguous", result.review_reasons)
        self.assertNotIn("wrong_release_group", result.conflicts)

    def test_missing_track_keeps_correct_release_group_with_explicit_reason(self):
        local_tracks = [_local("Crossfire", 1), _local("Space and Time", 2)]
        result = evaluate_release_group_candidate(
            {"artist": "311", "album": "Voyager", "embedded_release_group_id": RG_311_VOYAGER},
            _candidate(),
            local_tracks=local_tracks,
        )

        self.assertEqual(result.release_group_status, "validated")
        self.assertIn("missing_canonical_tracks", result.review_reasons)
        self.assertEqual(result.track_alignment.missing_count, 1)

    def test_extra_unrelated_track_is_not_forced_onto_nearest_recording(self):
        local_tracks = [_local("Crossfire", 1), _local("Unrelated Song", 99, artist="Other Artist")]
        result = evaluate_release_group_candidate(
            {"artist": "311", "album": "Voyager", "embedded_release_group_id": RG_311_VOYAGER},
            _candidate(tracks=[_mb("Crossfire", 1, "rec-crossfire")]),
            local_tracks=local_tracks,
        )

        self.assertEqual(result.track_alignment.matched_count, 1)
        self.assertEqual(result.track_alignment.unmatched_local_count, 1)
        self.assertIn("extra_local_tracks", result.review_reasons)
        unmatched_titles = [row.local_title for row in result.track_alignment.unmatched_local]
        self.assertEqual(unmatched_titles, ["Unrelated Song"])

    def test_wrong_but_textually_similar_release_group_loses_to_correct_one(self):
        """ARCH-002 Part 5: local files clearly belong to album A (311 -
        Voyager). A search/AI step returns two candidates: the correct
        album A, and a textually very similar but wrong album B (same
        artist, a near-identical title) whose actual tracklist does not
        correspond to the local audio at all. The canonical evaluator must
        rank/resolve based on the complete evidence model -- hard track
        alignment coverage -- not on album-title text similarity alone, and
        must not need a special-cased score bonus to get there.
        """
        local_tracks = [
            _local("Crossfire", 1),
            _local("Space and Time", 2),
            _local("Dodging Raindrops", 3),
        ]
        # Embedded RGID simulates a reimport/already-tagged case -- without
        # any embedded or fingerprint identity evidence, text-only matching
        # correctly cannot reach CONFIRMED for *either* candidate (that is
        # itself part of the point: no amount of text similarity alone ever
        # auto-authorizes anything). See test_absent_embedded_ids_do_not_...
        # below for the fresh-untagged-import path.
        local_album = {"artist": "311", "album": "Voyager", "embedded_release_group_id": RG_311_VOYAGER}

        candidate_correct = _candidate()  # RG_311_VOYAGER, "Voyager", real Voyager tracklist
        candidate_wrong = _candidate(
            rgid=RG_OTHER,
            title="Voyager - Live From Boston",  # near-identical title text
            release_id="44444444-4444-4444-4444-444444444444",
            tracks=[
                _mb("Intro Tape", 1, "rec-live-intro"),
                _mb("Beautiful Disaster (Live)", 2, "rec-live-bd"),
                _mb("Down (Live)", 3, "rec-live-down"),
            ],
        )

        # Confirm this is a genuine trap, not a strawman: the wrong
        # candidate's title really is textually close to the local album.
        title_similarity = normalize_title(candidate_wrong["release_group_title"])
        self.assertIn("voyager", title_similarity)

        result_correct = evaluate_release_group_candidate(
            local_album, candidate_correct, local_tracks=local_tracks,
        )
        result_wrong = evaluate_release_group_candidate(
            local_album, candidate_wrong, local_tracks=local_tracks,
        )

        self.assertEqual(result_correct.state, ConfidenceState.CONFIRMED)
        self.assertTrue(result_correct.can_auto_accept())
        self.assertEqual(result_correct.track_alignment.matched_count, 3)

        # The wrong candidate must not reach an auto-acceptable state despite
        # its deceptively similar title -- its tracklist simply does not
        # align with the local audio.
        self.assertIn(
            result_wrong.state,
            (ConfidenceState.INSUFFICIENT_EVIDENCE, ConfidenceState.REVIEW_RECOMMENDED, ConfidenceState.CONFLICT),
        )
        self.assertFalse(result_wrong.can_auto_accept())
        self.assertEqual(result_wrong.track_alignment.matched_count, 0)
        self.assertLess(result_wrong.score, result_correct.score)

    def test_wrong_audio_identity_is_not_silently_retitled(self):
        """ARCH-002 Part 34: local filename/title says the expected track
        ("Crossfire"), but fingerprint/recording evidence unambiguously
        identifies a *different* recording ("Space and Time"). The engine
        must not let Track B's audio be assigned under Track A's identity:
        no match for that local file may be produced under the wrong
        target, the album-level result must be a hard CONFLICT (never
        auto-acceptable), and the conflict must be explained rather than
        silently discarded.
        """
        local_tracks = [
            _local(
                "Crossfire",
                1,
                acoustid_hits=[{"recording_id": "rec-space-time", "score": 98, "acoustid": "aid-wrong-audio"}],
            )
        ]
        result = evaluate_release_group_candidate(
            {"artist": "311", "album": "Voyager", "embedded_release_group_id": RG_311_VOYAGER},
            _candidate(tracks=[_mb("Crossfire", 1, "rec-crossfire"), _mb("Space and Time", 2, "rec-space-time")]),
            local_tracks=local_tracks,
        )

        # No assignment may pair this local file with the "Crossfire" MB
        # track under its own filename/title identity -- the fingerprint
        # conflict must win, not be silently ignored in favor of the label.
        crossfire_assignments = [
            row for row in result.track_alignment.assignments
            if row.target_recording_id == "rec-crossfire" and row.status == "matched"
        ]
        self.assertEqual(crossfire_assignments, [])
        self.assertEqual(result.state, ConfidenceState.CONFLICT)
        self.assertFalse(result.action_allowed)
        self.assertFalse(result.can_auto_accept())
        self.assertFalse(result.can_auto_accept(MatchPolicy(allow_strong_match=True, allowed_trust_models=None)))
        self.assertIn("acoustid_conflict", result.conflicts)
        # The conflict must be explained, not just silently withheld.
        self.assertTrue(
            any("acoustid_conflict" in row.conflicts for row in result.track_alignment.assignments)
        )

    def test_same_title_tracks_use_position_duration_and_one_to_one_assignment(self):
        local_tracks = [
            _local("Interlude", 1, duration=41),
            _local("Interlude", 7, duration=73),
        ]
        mb_tracks = [
            _mb("Interlude", 1, "rec-interlude-short", duration=41, track_id="short-track"),
            _mb("Interlude", 7, "rec-interlude-long", duration=73, track_id="long-track"),
        ]

        alignment = align_tracks_global(local_tracks, mb_tracks)
        by_recording = {a.target_recording_id: a.local_track for a in alignment.assignments}

        self.assertEqual(by_recording["rec-interlude-short"]["track"], 1)
        self.assertEqual(by_recording["rec-interlude-long"]["track"], 7)


class TestArch002EvidenceInvariants(unittest.TestCase):
    def test_embedded_recording_conflict_cannot_be_confirmed(self):
        local_tracks = [_local("Crossfire", 1, recording_id="rec-other")]
        result = evaluate_release_group_candidate(
            {"artist": "311", "album": "Voyager"},
            _candidate(tracks=[_mb("Crossfire", 1, "rec-crossfire")]),
            local_tracks=local_tracks,
            trust_model="existing_library",
        )

        self.assertEqual(result.state, ConfidenceState.CONFLICT)
        self.assertIn("recording_id_conflict", result.conflicts)

    def test_absent_embedded_ids_do_not_penalize_fresh_import(self):
        local_tracks = [_local("Crossfire", 1, recording_id="")]
        result = evaluate_release_group_candidate(
            {"artist": "311", "album": "Voyager"},
            _candidate(tracks=[_mb("Crossfire", 1, "rec-crossfire")]),
            local_tracks=local_tracks,
            trust_model="fresh_reviewed_import",
            source_manifest_digest="sha256:test",
            reviewed=True,
        )

        self.assertNotIn("missing_embedded_recording_id", result.conflicts)
        self.assertNotEqual(result.state, ConfidenceState.INSUFFICIENT_EVIDENCE)

    def test_adding_unrelated_candidate_release_does_not_change_confirmed_rgid(self):
        local_tracks = [_local("Crossfire", 1, acoustid_hits=[{"recording_id": "rec-crossfire", "score": 99}])]
        base = evaluate_release_group_candidate(
            {"artist": "311", "album": "Voyager", "embedded_release_group_id": RG_311_VOYAGER},
            _candidate(tracks=[_mb("Crossfire", 1, "rec-crossfire")]),
            local_tracks=local_tracks,
        )
        with_unrelated = evaluate_release_group_candidate(
            {"artist": "311", "album": "Voyager", "embedded_release_group_id": RG_311_VOYAGER},
            _candidate(tracks=[_mb("Crossfire", 1, "rec-crossfire")]),
            local_tracks=local_tracks,
            release_candidates=[
                {"release_id": REL_STANDARD, "release_group_id": RG_311_VOYAGER, "tracks": [_mb("Crossfire", 1, "rec-crossfire")]},
                {"release_id": "unrelated-release", "release_group_id": RG_OTHER, "tracks": [_mb("Other", 1, "rec-other")]},
            ],
        )

        self.assertEqual(base.suggested_identity["release_group_id"], with_unrelated.suggested_identity["release_group_id"])
        self.assertNotIn("wrong_release_group", with_unrelated.conflicts)

    def test_track_input_order_does_not_change_assignment(self):
        local_tracks = [_local("Crossfire", 1), _local("Space and Time", 2), _local("Dodging Raindrops", 3)]
        mb_tracks = [_mb("Crossfire", 1, "rec-crossfire"), _mb("Space and Time", 2, "rec-space-time"), _mb("Dodging Raindrops", 3, "rec-raindrops")]

        first = align_tracks_global(local_tracks, mb_tracks).as_pairs()
        second = align_tracks_global(list(reversed(local_tracks)), mb_tracks).as_pairs()

        self.assertEqual(first, second)

    def test_normalization_is_linear_for_adversarial_text(self):
        text = "(" * 25000 + "Intro feat. Someone" + "]" * 25000
        started = time.perf_counter()
        normalized = normalize_title(text)
        elapsed = time.perf_counter() - started

        self.assertIn("intro", normalized)
        self.assertLess(elapsed, 0.25)


if __name__ == "__main__":
    unittest.main()
