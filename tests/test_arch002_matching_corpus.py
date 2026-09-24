import time
import unittest

from backend.matching import (
    AcoustIDStatus,
    ActionScope,
    ConfidenceState,
    DEFAULT_MATCH_POLICY,
    IdentityProof,
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

    def test_three_way_same_title_collision_resolves_globally_not_greedily(self):
        """ARCH-002 Part 6: 3+ competing local files, not just 2. All three
        local tracks share the exact same title ("Silence") and only
        duration distinguishes which MusicBrainz track each one really is.
        Fed in scrambled (non-positional, non-duration-sorted) order, a
        first-match/greedy scan can grab a locally-plausible but globally
        wrong pairing for an early file and then have no correct option
        left for a later one. The real min-cost global assignment must
        still find the fully correct one-to-one pairing regardless of
        input order.
        """
        # Deliberately scrambled: local file order does not match either
        # the target disc/track order or the target duration order.
        local_tracks = [
            _local("Silence", 3, duration=45),  # really the 45s target
            _local("Silence", 1, duration=15),  # really the 15s target
            _local("Silence", 2, duration=30),  # really the 30s target
        ]
        mb_tracks = [
            _mb("Silence", 1, "rec-silence-15", duration=15, track_id="silence-15"),
            _mb("Silence", 2, "rec-silence-30", duration=30, track_id="silence-30"),
            _mb("Silence", 3, "rec-silence-45", duration=45, track_id="silence-45"),
        ]

        alignment = align_tracks_global(local_tracks, mb_tracks)

        self.assertEqual(alignment.matched_count, 3)
        target_ids = [a.target_track_id for a in alignment.assignments]
        self.assertEqual(len(target_ids), len(set(target_ids)), "each MB track claimed at most once")
        local_indices = [a.local_index for a in alignment.assignments]
        self.assertEqual(len(local_indices), len(set(local_indices)), "each local file used at most once")

        by_recording = {a.target_recording_id: a.local_track for a in alignment.assignments}
        self.assertEqual(by_recording["rec-silence-15"]["duration_seconds"], 15)
        self.assertEqual(by_recording["rec-silence-30"]["duration_seconds"], 30)
        self.assertEqual(by_recording["rec-silence-45"]["duration_seconds"], 45)

    def test_three_way_collision_is_order_invariant(self):
        """The same collision as above, permuted every possible way, must
        always converge on the identical correct global assignment -- input
        order must never change which local file lands on which target."""
        import itertools

        base = [
            ("Silence", 1, 15, "rec-silence-15"),
            ("Silence", 2, 30, "rec-silence-30"),
            ("Silence", 3, 45, "rec-silence-45"),
        ]
        mb_tracks = [_mb(title, track, rec, duration=dur) for title, track, rec, dur in
                     [(t, tr, r, d) for t, tr, d, r in base]]

        expected = None
        for perm in itertools.permutations(range(3)):
            local_tracks = [_local("Silence", base[i][1], duration=base[i][2]) for i in perm]
            alignment = align_tracks_global(local_tracks, mb_tracks)
            mapping = {
                a.target_recording_id: a.local_track["duration_seconds"]
                for a in alignment.assignments
            }
            if expected is None:
                expected = mapping
            self.assertEqual(mapping, expected, f"permutation {perm} produced a different assignment")
        self.assertEqual(expected, {"rec-silence-15": 15, "rec-silence-30": 30, "rec-silence-45": 45})

    def test_extra_same_title_file_is_left_unmatched_not_forced(self):
        """4 local files share one generic title but only 3 MB targets
        exist. The algorithm must leave exactly one local file unmatched
        rather than force it onto an already-claimed or wrong target."""
        local_tracks = [
            _local("Silence", 1, duration=15),
            _local("Silence", 2, duration=30),
            _local("Silence", 3, duration=45),
            _local("Silence", 4, duration=999),  # no corresponding MB track at all
        ]
        mb_tracks = [
            _mb("Silence", 1, "rec-silence-15", duration=15),
            _mb("Silence", 2, "rec-silence-30", duration=30),
            _mb("Silence", 3, "rec-silence-45", duration=45),
        ]

        alignment = align_tracks_global(local_tracks, mb_tracks)

        self.assertEqual(alignment.matched_count, 3)
        self.assertEqual(alignment.unmatched_local_count, 1)
        self.assertEqual(alignment.unmatched_local[0].local_track["duration_seconds"], 999)
        target_ids = [a.target_track_id for a in alignment.assignments]
        self.assertEqual(len(target_ids), len(set(target_ids)))

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


_VERIFIED_SUBSET = MatchPolicy(scope=ActionScope.VERIFIED_SUBSET)


def _eighteen_track_release(**overrides):
    tracks = [_mb(f"Track {i}", i, f"rec-track-{i}") for i in range(1, 19)]
    return _candidate(tracks=tracks, **overrides)


class TestArch002PartialAlbumIdentity(unittest.TestCase):
    """ARCH-002 Part 3 policy: deterministic per-track proof for a
    partial album is real identity for the tracks present -- distinct
    from, and not gated behind, whole-release completeness. See
    IdentityProof/ActionScope in backend/matching/models.py."""

    def test_two_of_eighteen_deterministic_is_verified_subset_allowed(self):
        """2 of 18 target tracks, both exact Recording ID matches: local
        coverage is complete, target/release coverage is not, no conflict,
        and VERIFIED_SUBSET authorizes action while FULL_RELEASE does not."""
        local_tracks = [
            _local("Track Three", 3, recording_id="rec-track-3"),
            _local("Track Nine", 9, recording_id="rec-track-9"),
        ]
        result = evaluate_release_group_candidate(
            {"artist": "311", "album": "Big Comp"},
            _eighteen_track_release(artist="Various", title="Big Comp"),
            local_tracks=local_tracks,
            trust_model="existing_library",
        )

        self.assertEqual(result.conflicts, [])
        self.assertEqual(result.identity_proof, IdentityProof.DETERMINISTIC_TRACK_RECORDING_ID)
        self.assertEqual(result.local_tracks_total, 2)
        self.assertEqual(result.local_tracks_verified, 2)
        self.assertTrue(result.local_coverage_complete)
        self.assertEqual(result.target_tracks_total, 18)
        self.assertEqual(result.target_tracks_matched, 2)
        self.assertFalse(result.target_coverage_complete)
        self.assertFalse(result.release_complete)
        self.assertTrue(result.can_auto_accept(_VERIFIED_SUBSET))
        self.assertFalse(result.can_auto_accept(DEFAULT_MATCH_POLICY))

    def test_rgid_match_alone_does_not_authorize_verified_subset(self):
        """Matching Release Group ID with zero local tracks supplied (no
        deterministic evidence at all) must not authorize a subset-scoped
        action -- string equality alone is not track-level proof."""
        result = evaluate_release_group_candidate(
            {"artist": "Various", "album": "Big Comp", "embedded_release_group_id": RG_311_VOYAGER},
            _candidate(rgid=RG_311_VOYAGER, artist="Various", title="Big Comp"),
            local_tracks=[],
            trust_model="existing_library",
        )

        self.assertEqual(result.conflicts, [])
        self.assertEqual(result.identity_proof, IdentityProof.RELEASE_GROUP_ID)
        self.assertFalse(result.local_coverage_complete)
        self.assertFalse(result.can_auto_accept(_VERIFIED_SUBSET))
        self.assertFalse(result.can_auto_accept(DEFAULT_MATCH_POLICY))

    def test_partial_album_one_track_recording_id_conflict_denies_action(self):
        """One of two local tracks carries an embedded Recording ID that
        conflicts with its aligned target: a hard conflict, blocking
        action under every scope regardless of the other track's proof."""
        local_tracks = [
            _local("Track Three", 3, recording_id="rec-track-3"),
            _local("Track Nine", 9, recording_id="rec-wrong-recording"),
        ]
        result = evaluate_release_group_candidate(
            {"artist": "311", "album": "Big Comp"},
            _eighteen_track_release(artist="Various", title="Big Comp"),
            local_tracks=local_tracks,
            trust_model="existing_library",
        )

        self.assertEqual(result.state, ConfidenceState.CONFLICT)
        self.assertIn("recording_id_conflict", result.conflicts)
        self.assertFalse(result.can_auto_accept(_VERIFIED_SUBSET))
        self.assertFalse(result.can_auto_accept(DEFAULT_MATCH_POLICY))

    def test_partial_album_mixed_deterministic_and_text_only_not_verified(self):
        """One of two local tracks has exact Recording ID proof; the other
        has only a plain title match with no embedded ID. Local coverage is
        NOT complete (only 1 of 2 tracks deterministically proven), so this
        must not reach DETERMINISTIC_TRACK_RECORDING_ID / VERIFIED_SUBSET
        auto-accept, even though both tracks matched their targets."""
        local_tracks = [
            _local("Track Three", 3, recording_id="rec-track-3"),
            _local("Track Nine", 9, recording_id=""),
        ]
        result = evaluate_release_group_candidate(
            {"artist": "311", "album": "Big Comp"},
            _eighteen_track_release(artist="Various", title="Big Comp"),
            local_tracks=local_tracks,
            trust_model="existing_library",
        )

        self.assertEqual(result.conflicts, [])
        self.assertEqual(result.local_tracks_total, 2)
        self.assertEqual(result.local_tracks_verified, 1)
        self.assertFalse(result.local_coverage_complete)
        self.assertNotEqual(result.identity_proof, IdentityProof.DETERMINISTIC_TRACK_RECORDING_ID)
        self.assertFalse(result.can_auto_accept(_VERIFIED_SUBSET))

    def test_partial_album_duplicate_local_claim_denies_verified_subset(self):
        """Two local files both carry the SAME Recording ID (both claim
        the one target track that has it). Only one can win the one-to-one
        assignment; the other is left unmatched, so local coverage is not
        complete and VERIFIED_SUBSET must not authorize action."""
        local_tracks = [
            _local("Track Three", 3, recording_id="rec-track-3"),
            _local("Track Three (dup)", 3, recording_id="rec-track-3"),
        ]
        result = evaluate_release_group_candidate(
            {"artist": "311", "album": "Big Comp"},
            _eighteen_track_release(artist="Various", title="Big Comp"),
            local_tracks=local_tracks,
            trust_model="existing_library",
        )

        self.assertEqual(result.track_alignment.unmatched_local_count, 1)
        self.assertFalse(result.local_coverage_complete)
        self.assertFalse(result.can_auto_accept(_VERIFIED_SUBSET))

    def test_partial_album_acoustid_conflict_denies_action(self):
        """A local track's text/position match points to one target, but
        its AcoustID fingerprint confidently points to a different
        recording in the same candidate release: a hard conflict that must
        block action under every scope."""
        local_tracks = [
            # Title/track exactly match target index 2 ("Track 3") by text,
            # but the fingerprint confirms target index 8 ("Track 9")
            # instead -- the disagreement the acoustid-conflict path exists
            # to catch.
            _local(
                "Track 3",
                3,
                recording_id="",
                acoustid_hits=[{"recording_id": "rec-track-9", "score": 95}],
            ),
        ]
        result = evaluate_release_group_candidate(
            {"artist": "311", "album": "Big Comp"},
            _eighteen_track_release(artist="Various", title="Big Comp"),
            local_tracks=local_tracks,
            trust_model="existing_library",
        )

        self.assertIn("acoustid_conflict", result.conflicts)
        self.assertFalse(result.can_auto_accept(_VERIFIED_SUBSET))
        self.assertFalse(result.can_auto_accept(DEFAULT_MATCH_POLICY))


if __name__ == "__main__":
    unittest.main()
