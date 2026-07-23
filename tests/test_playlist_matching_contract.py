"""Tests for the playlist-specific additions to the shared matching
contract (backend/matching_contract.py): `library_identity_verified`,
`extra_conflicts`, and the `playlist_resolve_without_review` action
eligibility field added for the playlist missing-track-suggestions
consumer (migrated after PR #19's Import Review attach enforcement).

These are pure contract-level tests -- no Flask app, no Beets library --
covering exactly the same kind of decision logic Import Review's own
tests/test_matching_contract.py exercises, plus the playlist-only
behavior layered on top. Route-level behavior (candidate generation,
staleness, batching, concurrency, rollback) lives in
tests/test_playlist_safe_suggestions.py and
tests/test_playlist_suggestion_integrity.py.
"""
import unittest

from backend.matching_contract import (
    build_recording_matching_decision,
    compute_decision_version,
)

RECORDING_ID = "11111111-1111-1111-1111-111111111111"
RELEASE_ID = "22222222-2222-2222-2222-222222222222"
RGID = "33333333-3333-3333-3333-333333333333"
OTHER_RECORDING_ID = "44444444-4444-4444-4444-444444444444"


def _current(**overrides):
    data = {"title": "Midnight City", "artist": "M83"}
    data.update(overrides)
    return data


def _library_candidate(**overrides):
    data = {"artist": "M83", "title": "Midnight City", "source": "beets", "item_id": 7}
    data.update(overrides)
    return data


def _mb_candidate(**overrides):
    data = {
        "artist": "M83", "title": "Midnight City", "source": "musicbrainz",
        "mb_trackid": RECORDING_ID, "mb_albumid": RELEASE_ID, "mb_releasegroupid": RGID,
        "_match_score": {"total": 0.90, "source": "mb"},
    }
    data.update(overrides)
    return data


class LibraryIdentityBypassTests(unittest.TestCase):
    """Section 6 "Existing Beets library identity": a deterministic
    artist+title match to an existing library item is safe for playlist
    resolution even with no MusicBrainz Recording ID at all -- but this
    must never leak into attach_without_review (PR #19's own field)."""

    def test_exact_match_with_no_recording_id_is_playlist_safe(self):
        decision = build_recording_matching_decision(
            current=_current(), candidate=_library_candidate(),
            library_identity_verified=True,
        ).to_dict()
        d = decision["decision"]
        self.assertEqual(d["safety_key"], "safe")
        self.assertFalse(d["review_required"])
        self.assertFalse(d["requires_confirmation"])
        self.assertTrue(d["action_eligibility"]["playlist_resolve_without_review"])

    def test_exact_match_never_grants_attach_without_review(self):
        """A library-identity-only match has no Recording ID to attach --
        attach_without_review must stay scoped to the recording-ID path
        Import Review actually uses, even though safety_key reads "safe"."""
        decision = build_recording_matching_decision(
            current=_current(), candidate=_library_candidate(),
            library_identity_verified=True,
        ).to_dict()
        self.assertFalse(decision["decision"]["action_eligibility"]["attach_without_review"])

    def test_not_verified_stays_unsafe_with_no_recording_id(self):
        decision = build_recording_matching_decision(
            current=_current(), candidate=_library_candidate(),
            library_identity_verified=False,
        ).to_dict()
        d = decision["decision"]
        self.assertEqual(d["safety_key"], "none")
        self.assertFalse(d["action_eligibility"]["playlist_resolve_without_review"])

    def test_fuzzy_title_mismatch_requires_review_even_when_verified(self):
        decision = build_recording_matching_decision(
            current=_current(title="Midnight City"),
            candidate=_library_candidate(title="Completely Different Song"),
            library_identity_verified=True,
        ).to_dict()
        d = decision["decision"]
        self.assertNotEqual(d["safety_key"], "safe")
        self.assertFalse(d["action_eligibility"]["playlist_resolve_without_review"])

    def test_fuzzy_artist_mismatch_requires_review_even_when_verified(self):
        decision = build_recording_matching_decision(
            current=_current(artist="M83"),
            candidate=_library_candidate(artist="Some Other Band"),
            library_identity_verified=True,
        ).to_dict()
        self.assertFalse(
            decision["decision"]["action_eligibility"]["playlist_resolve_without_review"]
        )

    def test_existing_recording_conflict_blocks_library_bypass(self):
        """current already carries a different Recording ID than the
        candidate resolves to -- a genuine identity conflict must still
        block the playlist bypass, not just the attach path."""
        decision = build_recording_matching_decision(
            current=_current(mb_trackid=OTHER_RECORDING_ID),
            candidate=_library_candidate(mb_trackid=RECORDING_ID),
            library_identity_verified=True,
        ).to_dict()
        d = decision["decision"]
        self.assertIn("recording_id_conflict", d["conflicts"])
        self.assertFalse(d["action_eligibility"]["playlist_resolve_without_review"])


class RecordingIdPlaylistEligibilityTests(unittest.TestCase):
    """A MusicBrainz-only recording candidate is playlist-safe exactly
    when it's already attach_without_review-safe -- no separate, looser
    bar for MB text-search candidates (conservative per spec)."""

    def test_safe_recording_candidate_is_playlist_safe(self):
        decision = build_recording_matching_decision(
            current=_current(), candidate=_mb_candidate(),
            selected_release={"release_id": RELEASE_ID, "release_group_id": RGID,
                              "title": "Hurry Up, We're Dreaming", "artist": "M83"},
            linked_releases=[{"release_id": RELEASE_ID, "release_group_id": RGID}],
        ).to_dict()
        d = decision["decision"]
        self.assertTrue(d["action_eligibility"]["attach_without_review"])
        self.assertTrue(d["action_eligibility"]["playlist_resolve_without_review"])

    def test_fuzzy_recording_without_release_group_requires_review(self):
        """No release-group resolution (plain MB text search) -- must not
        be auto-applied on text score alone."""
        decision = build_recording_matching_decision(
            current=_current(), candidate=_mb_candidate(mb_releasegroupid=""),
        ).to_dict()
        self.assertFalse(
            decision["decision"]["action_eligibility"]["playlist_resolve_without_review"]
        )


class ExtraConflictsTests(unittest.TestCase):
    """extra_conflicts is the mechanism the playlist adapter uses to fold
    a competing-candidate tie into the decision *before* decision_version
    is computed, so the hash and the displayed safety state can never
    diverge (see app.py's _playlist_decisions_for_track)."""

    def test_extra_conflict_forces_review_and_blocks_playlist_bypass(self):
        decision = build_recording_matching_decision(
            current=_current(), candidate=_library_candidate(),
            library_identity_verified=True,
            extra_conflicts=("competing_candidates_tie",),
        ).to_dict()
        d = decision["decision"]
        self.assertIn("competing_candidates_tie", d["conflicts"])
        self.assertNotEqual(d["safety_key"], "safe")
        self.assertFalse(d["action_eligibility"]["playlist_resolve_without_review"])

    def test_extra_conflict_changes_decision_version(self):
        base = build_recording_matching_decision(
            current=_current(), candidate=_library_candidate(),
            library_identity_verified=True,
        )
        tied = build_recording_matching_decision(
            current=_current(), candidate=_library_candidate(),
            library_identity_verified=True,
            extra_conflicts=("competing_candidates_tie",),
        )
        self.assertNotEqual(
            compute_decision_version("track-key", base),
            compute_decision_version("track-key", tied),
        )

    def test_no_extra_conflicts_is_a_no_op(self):
        a = build_recording_matching_decision(
            current=_current(), candidate=_library_candidate(), library_identity_verified=True,
        ).to_dict()
        b = build_recording_matching_decision(
            current=_current(), candidate=_library_candidate(),
            library_identity_verified=True, extra_conflicts=(),
        ).to_dict()
        self.assertEqual(a["decision"], b["decision"])

    def test_duplicate_extra_conflict_not_added_twice(self):
        decision = build_recording_matching_decision(
            current=_current(artist="Nobody", title="Nothing"),
            candidate=_library_candidate(artist="Someone Else", title="Something Else"),
            library_identity_verified=True,
            extra_conflicts=("title_conflict", "title_conflict"),
        ).to_dict()
        self.assertEqual(decision["decision"]["conflicts"].count("title_conflict"), 1)


class PlaylistDecisionVersionTests(unittest.TestCase):
    def test_decision_version_keyed_by_track_key_not_item_id(self):
        decision = build_recording_matching_decision(
            current=_current(), candidate=_library_candidate(), library_identity_verified=True,
        )
        v1 = compute_decision_version("ptk_aaaa", decision)
        v2 = compute_decision_version("ptk_bbbb", decision)
        self.assertNotEqual(v1, v2)
        self.assertTrue(v1.startswith("drv2:"))

    def test_decision_version_changes_when_identity_changes(self):
        d1 = build_recording_matching_decision(
            current=_current(), candidate=_mb_candidate(mb_trackid=RECORDING_ID),
            selected_release={"release_id": RELEASE_ID, "release_group_id": RGID},
            linked_releases=[{"release_id": RELEASE_ID, "release_group_id": RGID}],
        )
        d2 = build_recording_matching_decision(
            current=_current(), candidate=_mb_candidate(mb_trackid=OTHER_RECORDING_ID),
            selected_release={"release_id": RELEASE_ID, "release_group_id": RGID},
            linked_releases=[{"release_id": RELEASE_ID, "release_group_id": RGID}],
        )
        self.assertNotEqual(
            compute_decision_version("ptk_x", d1), compute_decision_version("ptk_x", d2)
        )


if __name__ == "__main__":
    unittest.main()
