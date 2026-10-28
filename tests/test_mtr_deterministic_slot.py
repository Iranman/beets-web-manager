"""Regression coverage for the DETERMINISTIC_METADATA_SLOT identity tier and
the candidate relatedness veto (STEP 1 of the missing-track recovery redesign;
docs/adr/0007-deterministic-metadata-slot-gate.md).

Scenario anchor: 112 "Forever" (RG 37cb4a72), 9 tracks, missing #1
"Forever (Intro)" and #9 "For Us (Outro)"; the recovered files fingerprint to
AcoustID NO_RESULT (genuinely absent, not an outage). The title-collision foil
is FLEUR "FOREVER FOREVER" (RG 2da91738), an unrelated real release.
"""

import unittest

from backend.matching import (
    AcoustIDStatus,
    ActionScope,
    CandidateRelatedness,
    ConfidenceState,
    DEFAULT_MATCH_POLICY,
    DETERMINISTIC_SLOT_DURATION_TOLERANCE_S,
    IdentityProof,
    MatchPolicy,
    candidate_is_unrelated,
    classify_candidate_relatedness,
    evaluate_release_group_candidate,
)
from backend.matching_contract import build_album_matching_decision

RG_112 = "37cb4a72-1111-1111-1111-111111111111"
REL_112 = "083b9009-2222-2222-2222-222222222222"
ART_112 = "a1b2c3d4-4444-4444-4444-444444444444"
RG_FLEUR = "2da91738-3333-3333-3333-333333333333"

SUBSET_POLICY = MatchPolicy(scope=ActionScope.VERIFIED_SUBSET)

# 112 "Forever" tracklist (titles + per-track seconds for duration proof).
_112_TRACKS = [
    ("Forever (Intro)", 42),
    ("Only You", 255),
    ("Cupid", 240),
    ("Come See Me", 233),
    ("Anywhere", 262),
    ("Love Me", 228),
    ("Now That We're Done", 219),
    ("It's Over Now", 250),
    ("For Us (Outro)", 55),
]


def _mb_112():
    return [
        {
            "title": title,
            "artist": "112",
            "disc": 1,
            "track": i + 1,
            "duration_ms": secs * 1000,
            "recording_id": f"rec-112-{i + 1}",
            "track_id": f"trk-112-{i + 1}",
        }
        for i, (title, secs) in enumerate(_112_TRACKS)
    ]


_UNSET = object()


def _file(title, track, secs, *, disc=1, acoustid_hits=None, recording_id="",
          original_name=_UNSET, embedded_title=_UNSET,
          embedded_rg=RG_112, embedded_artist=ART_112):
    """A staged recovery file. Option C (USER-chosen gate): a genuine recovery
    is corroborated by TWO independent sources -- the ORIGINAL pre-rename
    download name and the file's EMBEDDED MB tags -- so both default to the
    genuine identity here. A test models a non-genuine file by overriding
    ``embedded_title``/``original_name``/``embedded_*`` to disagree, or passing
    ``None`` to drop a source entirely (absent => fail closed to review)."""
    if original_name is _UNSET:
        original_name = f"{track:02d} {title}.flac"
    if embedded_title is _UNSET:
        embedded_title = title
    row = {
        "path": f"/staged/{track:02d}-{title}.flac",
        "title": title,
        "artist": "112",
        "disc": disc,
        "track": track,
        "length": float(secs),
        "recording_id": recording_id,
    }
    if acoustid_hits is not None:
        row["acoustid_hits"] = acoustid_hits
    if original_name is not None:
        row["original_name"] = original_name
    if embedded_title is not None:
        row["embedded_title"] = embedded_title
    if embedded_rg is not None:
        row["embedded_release_group_id"] = embedded_rg
    if embedded_artist is not None:
        row["embedded_artist_id"] = embedded_artist
    return row


def _recovered_intro_outro(acoustid_hits):
    """The two recovered files: #1 intro + #9 outro, exact title/position/dur."""
    return [
        _file("Forever (Intro)", 1, 42, acoustid_hits=acoustid_hits),
        _file("For Us (Outro)", 9, 55, acoustid_hits=acoustid_hits),
    ]


def _112_current():
    return {"album": "Forever", "artist": "112", "mb_releasegroupid": RG_112,
            "mb_albumartistid": ART_112}


def _112_candidate():
    return {
        "mb_releasegroupid": RG_112,
        "mb_albumid": REL_112,
        "release_group_id": RG_112,
        "release_id": REL_112,
        "album": "Forever",
        "artist": "112",
        "mb_albumartistid": ART_112,
        "tracks": _mb_112(),
    }


def _evaluate(items):
    return evaluate_release_group_candidate(
        _112_current(),
        _112_candidate(),
        local_tracks=items,
        trust_model="existing_library",
    )


class DeterministicMetadataSlotTests(unittest.TestCase):
    def test_112_no_result_recovery_is_slot_proven_and_subset_auto_eligible(self):
        res = _evaluate(_recovered_intro_outro(acoustid_hits=[]))
        self.assertEqual(res.identity_proof, IdentityProof.DETERMINISTIC_METADATA_SLOT)
        self.assertTrue(res.local_coverage_complete)
        self.assertEqual(res.local_tracks_verified, 2)
        # Each present track read AcoustID NO_RESULT, not UNAVAILABLE.
        self.assertTrue(all(a.acoustid_status == AcoustIDStatus.NO_RESULT for a in res.track_alignment.assignments))
        # VERIFIED_SUBSET: auto-eligible. FULL_RELEASE: never (partial album).
        self.assertTrue(res.can_auto_accept(SUBSET_POLICY))
        self.assertFalse(res.can_auto_accept(DEFAULT_MATCH_POLICY))
        self.assertFalse(res.action_allowed)  # action_allowed stays FULL_RELEASE-grade

    def test_112_contract_authorizes_subset_with_no_result(self):
        decision = build_album_matching_decision(
            current=_112_current(),
            candidate=_112_candidate(),
            items=_recovered_intro_outro(acoustid_hits=[]),
            mb_tracks=_mb_112(),
        ).to_dict()
        self.assertEqual(decision["contract_version"], 3)
        self.assertTrue(decision["action_allowed"])
        self.assertTrue(decision["identity_verified"])
        self.assertFalse(decision["review_required"])
        self.assertEqual(decision["identity_proof"], IdentityProof.DETERMINISTIC_METADATA_SLOT.value)
        dims = decision["dimensions"]
        self.assertEqual(dims["downloaded_recording_verification"]["status"], "deterministic_metadata_slot")
        self.assertTrue(dims["import_eligibility"]["ok"])
        self.assertTrue(dims["identifier_syntax"]["ok"])

    def test_unavailable_never_satisfies_the_gate(self):
        # No acoustid_hits key at all => UNAVAILABLE (no key / outage).
        res = _evaluate(_recovered_intro_outro(acoustid_hits=None))
        self.assertTrue(all(a.acoustid_status == AcoustIDStatus.UNAVAILABLE for a in res.track_alignment.assignments))
        self.assertFalse(res.local_coverage_complete)
        self.assertNotEqual(res.identity_proof, IdentityProof.DETERMINISTIC_METADATA_SLOT)
        self.assertFalse(res.can_auto_accept(SUBSET_POLICY))
        decision = build_album_matching_decision(
            current=_112_current(), candidate=_112_candidate(),
            items=_recovered_intro_outro(acoustid_hits=None), mb_tracks=_mb_112(),
        ).to_dict()
        self.assertFalse(decision["action_allowed"])
        self.assertTrue(decision["review_required"])

    def test_no_result_message_distinct_from_unavailable(self):
        # One present track, answered-empty => no_result wording, never
        # "not configured or not reachable".
        decision = build_album_matching_decision(
            current=_112_current(), candidate=_112_candidate(),
            items=[_file("Only You", 2, 255, acoustid_hits=[])], mb_tracks=_mb_112(),
        ).to_dict()
        warns = decision["warnings"]
        self.assertIn("acoustid_no_result", warns)
        self.assertNotIn("acoustid_unavailable", warns)
        self.assertNotIn("not configured or not reachable", decision["explanation"])

    def test_wrong_duration_blocks_slot(self):
        bad = [_file("Forever (Intro)", 1, 42 + DETERMINISTIC_SLOT_DURATION_TOLERANCE_S + 5, acoustid_hits=[])]
        res = _evaluate(bad)
        self.assertFalse(res.local_coverage_complete)
        self.assertNotEqual(res.identity_proof, IdentityProof.DETERMINISTIC_METADATA_SLOT)

    def test_wrong_position_blocks_slot(self):
        # Correct title + duration but wrong track number: not the intro slot.
        bad = [_file("Forever (Intro)", 7, 42, acoustid_hits=[])]
        res = _evaluate(bad)
        slot_rows = [a for a in res.track_alignment.assignments if a.is_deterministic_metadata_slot()]
        self.assertEqual(slot_rows, [])

    def test_non_exact_title_blocks_slot(self):
        # A genuinely different title in slot #1: the normalized title does not
        # match the intro track, so the slot cannot be proven on metadata.
        bad = [_file("Completely Different Tune", 1, 42, acoustid_hits=[])]
        res = _evaluate(bad)
        slot_rows = [a for a in res.track_alignment.assignments if a.is_deterministic_metadata_slot()]
        self.assertEqual(slot_rows, [])
        self.assertNotEqual(res.identity_proof, IdentityProof.DETERMINISTIC_METADATA_SLOT)


class VersionStrippedTitleCannotAutoPassTests(unittest.TestCase):
    """F4 (orchestrator-verified): the slot gate must use a STRICT, version-
    PRESERVING canonical-title comparison, not the variant-maximized
    ``_title_similarity`` ranking score. A live take / remaster / alt-take /
    edit whose version marker is ABSENT on the target normalizes to the bare
    title under the fuzzy ranker and would otherwise score 1.0 >= 0.999; with
    exact position + duration within tolerance + NO_RESULT that silently
    unlocked an UNATTENDED import of the WRONG recording. It must fail closed.
    """

    def _slot_rows(self, items):
        res = _evaluate(items)
        return [a for a in res.track_alignment.assignments if a.is_deterministic_metadata_slot()], res

    def test_live_variant_in_bare_slot_cannot_slot_or_subset_accept(self):
        # Target #2 is bare "Only You"; the file is "Only You (Live 2019)" at
        # the same position, duration within tolerance, genuine NO_RESULT.
        bad = [_file("Only You (Live 2019)", 2, 255, acoustid_hits=[])]
        slot_rows, res = self._slot_rows(bad)
        self.assertEqual(slot_rows, [], "a live variant must not satisfy the slot")
        self.assertFalse(res.local_coverage_complete)
        self.assertNotEqual(res.identity_proof, IdentityProof.DETERMINISTIC_METADATA_SLOT)
        self.assertFalse(res.can_auto_accept(SUBSET_POLICY))
        self.assertFalse(res.action_allowed)

    def test_remaster_variant_in_bare_slot_cannot_slot(self):
        bad = [_file("Only You (Remastered)", 2, 255, acoustid_hits=[])]
        slot_rows, res = self._slot_rows(bad)
        self.assertEqual(slot_rows, [])
        self.assertFalse(res.can_auto_accept(SUBSET_POLICY))

    def test_alt_take_variant_in_bare_slot_cannot_slot(self):
        bad = [_file("Only You (Take 2)", 2, 255, acoustid_hits=[])]
        slot_rows, res = self._slot_rows(bad)
        self.assertEqual(slot_rows, [])
        self.assertFalse(res.can_auto_accept(SUBSET_POLICY))

    def test_edit_variant_in_bare_slot_cannot_slot(self):
        bad = [_file("Only You (Radio Edit)", 2, 255, acoustid_hits=[])]
        slot_rows, res = self._slot_rows(bad)
        self.assertEqual(slot_rows, [])
        self.assertFalse(res.can_auto_accept(SUBSET_POLICY))

    def test_full_version_variant_recovery_blocked_but_genuine_exact_passes(self):
        # Two files in the intro/outro slots: the intro is a genuine exact
        # title (passes), the outro is a live variant of the bare... here both
        # target titles carry their own parenthetical, so a MISMATCHED version
        # marker on the local file must still block that slot.
        mixed = [
            _file("Forever (Intro)", 1, 42, acoustid_hits=[]),          # genuine exact
            _file("For Us (Outro) (Live)", 9, 55, acoustid_hits=[]),    # extra version marker
        ]
        res = _evaluate(mixed)
        slot_titles = {a.target_index for a in res.track_alignment.assignments
                       if a.is_deterministic_metadata_slot()}
        self.assertIn(0, slot_titles, "genuine exact intro must still slot")
        self.assertNotIn(8, slot_titles, "a version-marked outro must not slot")
        self.assertFalse(res.local_coverage_complete)
        self.assertFalse(res.can_auto_accept(SUBSET_POLICY))

    def test_genuine_exact_intro_outro_still_slot_and_subset_accept(self):
        # Regression guard: the strict gate must NOT break the real recovery.
        good = _recovered_intro_outro(acoustid_hits=[])
        res = _evaluate(good)
        self.assertEqual(res.identity_proof, IdentityProof.DETERMINISTIC_METADATA_SLOT)
        self.assertTrue(res.local_coverage_complete)
        self.assertTrue(res.can_auto_accept(SUBSET_POLICY))


class FleurTitleCollisionTests(unittest.TestCase):
    def _fleur_candidate(self):
        return {
            "mb_releasegroupid": RG_FLEUR,
            "release_group_id": RG_FLEUR,
            "album": "FOREVER FOREVER",
            "artist": "FLEUR",
            "tracks": [
                {"title": "Forever (Intro)", "artist": "FLEUR", "disc": 1, "track": 1,
                 "duration_ms": 42000, "recording_id": "rec-fleur-1"},
            ],
        }

    def test_fleur_against_112_album_is_hard_conflict_never_auto(self):
        # Local album has the 112 RG; FLEUR's RG contradicts it.
        res = evaluate_release_group_candidate(
            _112_current(), self._fleur_candidate(),
            local_tracks=[_file("Forever (Intro)", 1, 42, acoustid_hits=[])],
            trust_model="existing_library",
        )
        self.assertEqual(res.state, ConfidenceState.CONFLICT)
        self.assertEqual(res.identity_proof, IdentityProof.INSUFFICIENT)
        self.assertFalse(res.can_auto_accept(SUBSET_POLICY))
        self.assertIn("wrong_release_group", res.conflicts)

    def test_fleur_orphan_no_local_rg_stays_unverified(self):
        # Orphaned folder lost its RG: FLEUR must not auto-verify on title alone.
        res = evaluate_release_group_candidate(
            {"album": "112 - Forever", "artist": ""}, self._fleur_candidate(),
            local_tracks=[_file("Forever (Intro)", 1, 42, acoustid_hits=[])],
            trust_model="existing_library",
        )
        self.assertNotEqual(res.release_group_status, "validated")
        self.assertNotEqual(res.identity_proof, IdentityProof.DETERMINISTIC_METADATA_SLOT)
        self.assertFalse(res.can_auto_accept(SUBSET_POLICY))


class EmbeddedRecordingVsFingerprintTests(unittest.TestCase):
    def test_embedded_recording_id_contradicting_fingerprint_is_conflict(self):
        # File carries a recording ID for slot #2 but fingerprints to slot #1.
        hits = [{"recording_id": "rec-112-1", "score": 0.97}]
        item = _file("Only You", 2, 255, acoustid_hits=hits, recording_id="rec-112-9")
        res = _evaluate([item])
        self.assertTrue(res.conflicts)
        self.assertNotEqual(res.identity_proof, IdentityProof.DETERMINISTIC_METADATA_SLOT)
        self.assertFalse(res.can_auto_accept(SUBSET_POLICY))


class CandidateRelatednessTests(unittest.TestCase):
    def test_same_release_group_is_related(self):
        self.assertEqual(
            classify_candidate_relatedness(
                {"release_group_id": RG_112, "artist": "112"},
                target_release_group_id=RG_112, target_artist="112",
            ),
            CandidateRelatedness.RELATED_RELEASE_GROUP,
        )

    def test_fleur_is_unrelated_despite_title_collision(self):
        fleur = {"release_group_id": RG_FLEUR, "album": "FOREVER FOREVER", "artist": "FLEUR"}
        self.assertEqual(
            classify_candidate_relatedness(fleur, target_release_group_id=RG_112, target_artist="112"),
            CandidateRelatedness.UNRELATED,
        )
        self.assertTrue(candidate_is_unrelated(fleur, target_release_group_id=RG_112, target_artist="112"))

    def test_same_artist_id_is_related_without_rg(self):
        self.assertEqual(
            classify_candidate_relatedness(
                {"artist_id": "mbid-112", "artist": "112"},
                target_release_group_id=RG_112, target_artist_id="mbid-112",
            ),
            CandidateRelatedness.RELATED_ARTIST,
        )

    def test_different_artist_id_vetoes_even_with_text_floor(self):
        self.assertEqual(
            classify_candidate_relatedness(
                {"artist_id": "mbid-fleur", "artist": "112"},
                target_release_group_id=RG_112, target_artist_id="mbid-112", target_artist="112",
            ),
            CandidateRelatedness.UNRELATED,
        )


class MultiReleaseGroupCandidateTests(unittest.TestCase):
    def test_veto_filters_unrelated_rg_from_pool(self):
        pool = [
            {"release_group_id": RG_FLEUR, "album": "FOREVER FOREVER", "artist": "FLEUR"},
            {"release_group_id": RG_112, "album": "Forever", "artist": "112"},
        ]
        related = [c for c in pool
                   if not candidate_is_unrelated(c, target_release_group_id=RG_112, target_artist="112")]
        self.assertEqual(len(related), 1)
        self.assertEqual(related[0]["release_group_id"], RG_112)


if __name__ == "__main__":
    unittest.main()
