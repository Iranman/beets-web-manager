import unittest

from backend.import_guard import (
    existing_track_can_block_downloaded_replacement,
    existing_track_matches_target,
    filter_wanted_tracks_against_missing,
    missing_wanted_tracks_block_retag,
    release_track_matches_missing_target,
)


class ImportGuardTests(unittest.TestCase):
    def test_existing_duplicate_requires_strong_title_match(self):
        self.assertFalse(existing_track_matches_target(title_score=0.82))
        self.assertTrue(existing_track_matches_target(title_score=0.94))

    def test_fingerprint_mismatch_overrides_title_match(self):
        self.assertFalse(
            existing_track_matches_target(
                fingerprint_status="mismatch",
                title_score=1.0,
            )
        )

    def test_fingerprint_match_accepts_existing_track(self):
        self.assertTrue(
            existing_track_matches_target(
                fingerprint_status="match",
                title_score=0.2,
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
                fingerprint_status="match",
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
