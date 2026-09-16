import unittest

from backend.import_guard import (
    existing_track_can_block_downloaded_replacement,
    existing_track_matches_target,
    filter_wanted_tracks_against_missing,
    missing_wanted_tracks_block_retag,
    release_track_matches_missing_target,
)
from backend.matching import AcoustIDStatus


class ImportGuardTests(unittest.TestCase):
    def test_existing_duplicate_requires_strong_title_match(self):
        self.assertFalse(existing_track_matches_target(title_score=0.82))
        self.assertTrue(existing_track_matches_target(title_score=0.94))

    def test_fingerprint_mismatch_overrides_title_match(self):
        """ARCH-002: `fingerprint_status` is the canonical `AcoustIDStatus`
        vocabulary (`_album_track_fingerprint_check()`'s `status` field
        returns these values directly), not an independent string set."""
        self.assertFalse(
            existing_track_matches_target(
                fingerprint_status=AcoustIDStatus.CONFLICT.value,
                title_score=1.0,
            )
        )
        # The enum member itself (not just its .value string) must work
        # identically -- AcoustIDStatus is a str subclass.
        self.assertFalse(
            existing_track_matches_target(
                fingerprint_status=AcoustIDStatus.CONFLICT,
                title_score=1.0,
            )
        )

    def test_fingerprint_match_accepts_existing_track(self):
        self.assertTrue(
            existing_track_matches_target(
                fingerprint_status=AcoustIDStatus.CONFIRMED.value,
                title_score=0.2,
            )
        )
        self.assertTrue(
            existing_track_matches_target(
                fingerprint_status=AcoustIDStatus.CONFIRMED,
                title_score=0.2,
            )
        )

    def test_no_result_and_unavailable_are_not_conflict(self):
        """ARCH-002 Part 4/7: NO_RESULT and UNAVAILABLE must never be
        treated as CONFLICT -- both fall through to the text-score
        fallback exactly like an empty/unrecognized status does, they do
        not themselves block or confirm anything."""
        for status in (AcoustIDStatus.NO_RESULT, AcoustIDStatus.UNAVAILABLE, AcoustIDStatus.AMBIGUOUS):
            with self.subTest(status=status):
                self.assertFalse(existing_track_matches_target(fingerprint_status=status, title_score=0.5))
                self.assertTrue(existing_track_matches_target(fingerprint_status=status, title_score=0.94))
                self.assertTrue(
                    existing_track_matches_target(
                        fingerprint_status=status, exact_mbid=True, title_score=0.8,
                    )
                )

    def test_exact_mbid_still_needs_reasonable_title(self):
        self.assertFalse(existing_track_matches_target(exact_mbid=True, title_score=0.4))
        self.assertTrue(existing_track_matches_target(exact_mbid=True, title_score=0.8))

    def test_missing_existing_file_does_not_block_downloaded_replacement(self):
        self.assertFalse(
            existing_track_can_block_downloaded_replacement(
                file_exists=False,
                exact_mbid=True,
                title_score=1.0,
            )
        )
        self.assertFalse(
            existing_track_can_block_downloaded_replacement(
                file_exists=False,
                fingerprint_status=AcoustIDStatus.CONFIRMED.value,
                title_score=1.0,
            )
        )
        self.assertTrue(
            existing_track_can_block_downloaded_replacement(
                file_exists=True,
                exact_mbid=True,
                title_score=1.0,
            )
        )

    def test_missing_requested_track_blocks_existing_album_retag(self):
        self.assertFalse(missing_wanted_tracks_block_retag([]))
        self.assertTrue(missing_wanted_tracks_block_retag([
            {"disc": 1, "track": 12, "title": "I Won't"},
        ]))

    def test_duplicate_title_release_track_must_match_missing_position(self):
        missing = [{
            "disc": 2,
            "track": 2,
            "title": "Skydive",
            "mb_trackid": "disc-2-skydive",
        }]
        title_counts = {"skydive": 2}

        self.assertFalse(
            release_track_matches_missing_target(
                {
                    "disc": 1,
                    "track": 2,
                    "title": "Skydive",
                    "mb_trackid": "disc-1-skydive",
                },
                missing,
                release_title_counts=title_counts,
            )
        )
        self.assertTrue(
            release_track_matches_missing_target(
                {
                    "disc": 2,
                    "track": 2,
                    "title": "Skydive",
                    "mb_trackid": "disc-2-skydive",
                },
                missing,
                release_title_counts=title_counts,
            )
        )

    def test_wanted_filter_does_not_keep_wrong_disc_by_title(self):
        missing = [{
            "disc": 2,
            "track": 2,
            "title": "Skydive",
            "mb_trackid": "disc-2-skydive",
        }]
        wanted = [{
            "disc": 1,
            "track": 2,
            "title": "Skydive",
            "mb_trackid": "disc-1-skydive",
        }]

        self.assertEqual(filter_wanted_tracks_against_missing(wanted, missing), [])

    def test_wanted_filter_maps_unpositioned_unique_title_to_missing_track(self):
        missing = [{
            "disc": 2,
            "track": 2,
            "title": "Skydive",
            "mb_trackid": "disc-2-skydive",
        }]
        wanted = [{"title": "Skydive"}]

        self.assertEqual(filter_wanted_tracks_against_missing(wanted, missing), missing)


if __name__ == "__main__":
    unittest.main()
