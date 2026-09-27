import unittest

from backend.import_guard import (
    filter_wanted_tracks_against_missing,
    missing_wanted_tracks_block_retag,
    release_track_matches_missing_target,
)


class ImportGuardTests(unittest.TestCase):
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
