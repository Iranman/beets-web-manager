"""I-7: library rows missing canonical identity, keyed on mb_releasegroupid.
Synthetic rows only."""
import unittest

from backend.matching.library_gaps import (
    ALBUM_GROUP_WITHOUT_RELEASE, ALBUM_RELEASE_WITHOUT_GROUP, ALBUM_TRACK_WITHOUT_RECORDING,
    ALBUM_WITHOUT_IDS, GAP_KINDS, IDENTITY_MISSING_KINDS, SINGLETON_WITHOUT_RECORDING,
    library_identity_gaps,
)

REL = "11111111-1111-1111-1111-111111111111"
RG = "22222222-2222-2222-2222-222222222222"
REC = "33333333-3333-3333-3333-333333333333"


def _ids(rows):
    return [r["id"] for r in rows]


class LibraryIdentityGapsTests(unittest.TestCase):
    def setUp(self):
        self.albums = [
            {"id": 1, "album": "Complete", "mb_albumid": REL, "mb_releasegroupid": RG, "added": 1},
            {"id": 2, "album": "Release only", "mb_albumid": REL, "mb_releasegroupid": "", "added": 2},  # (a)
            {"id": 3, "album": "Neither", "mb_albumid": "", "mb_releasegroupid": "", "added": 3},  # (b)
            {"id": 4, "album": "Group only", "mb_albumid": "", "mb_releasegroupid": RG, "added": 4},  # (b2)
            {"id": 5, "album": "Malformed", "mb_albumid": "not-a-uuid", "mb_releasegroupid": "  ", "added": 5},  # (b)
        ]
        self.items = [
            {"id": 10, "album_id": 1, "track": 1, "mb_trackid": REC, "path": "/m/a/1.flac"},
            {"id": 11, "album_id": 1, "track": 2, "mb_trackid": "", "path": "/m/a/2.flac"},  # (c)
            {"id": 12, "album_id": 2, "track": 2, "mb_trackid": REC, "path": "/m/b/2.flac"},
            {"id": 13, "album_id": 2, "track": 1, "mb_trackid": REC, "path": "/m/b/1.flac"},
            {"id": 20, "album_id": None, "mb_trackid": "", "added": 9},  # (d)
            {"id": 21, "album_id": 0, "mb_trackid": REC},  # complete singleton
            {"id": 22, "mb_trackid": "  ", "added": 1},  # (d), no album_id key
        ]
        self.gaps = library_identity_gaps(self.albums, self.items)

    def test_every_kind_is_present(self):
        self.assertEqual(set(self.gaps), set(GAP_KINDS))
        self.assertNotIn(ALBUM_GROUP_WITHOUT_RELEASE, IDENTITY_MISSING_KINDS)

    def test_release_id_alone_is_a_gap(self):
        # Never keyed on mb_albumid alone: a Release without its Release Group is (a).
        self.assertEqual(_ids(self.gaps[ALBUM_RELEASE_WITHOUT_GROUP]), [2])
        row = self.gaps[ALBUM_RELEASE_WITHOUT_GROUP][0]
        self.assertEqual((row["first_item_id"], row["first_item_path"], row["tracks"]), (13, "/m/b/1.flac", 2))

    def test_albums_without_ids_and_malformed_ids(self):
        self.assertEqual(_ids(self.gaps[ALBUM_WITHOUT_IDS]), [3, 5])

    def test_group_without_release_is_informational(self):
        self.assertEqual(_ids(self.gaps[ALBUM_GROUP_WITHOUT_RELEASE]), [4])

    def test_album_tracks_without_recording(self):
        rows = self.gaps[ALBUM_TRACK_WITHOUT_RECORDING]
        self.assertEqual(_ids(rows), [11])
        self.assertEqual((rows[0]["album_mb_albumid"], rows[0]["album_mb_releasegroupid"]), (REL, RG))

    def test_singletons_without_recording(self):
        self.assertEqual(_ids(self.gaps[SINGLETON_WITHOUT_RECORDING]), [22, 20])

    def test_complete_rows_are_not_gaps(self):
        listed = {(k, r["id"]) for k, rows in self.gaps.items() for r in rows}
        self.assertNotIn((ALBUM_RELEASE_WITHOUT_GROUP, 1), listed)
        self.assertFalse(any(r["id"] in (10, 12, 13, 21) for k in (ALBUM_TRACK_WITHOUT_RECORDING, SINGLETON_WITHOUT_RECORDING)
                             for r in self.gaps[k]))

    def test_inputs_are_not_mutated(self):
        self.assertNotIn("first_item_id", self.albums[1])
        self.assertNotIn("album_mb_releasegroupid", self.items[1])

    def test_empty_library(self):
        self.assertEqual(library_identity_gaps([], []), {k: [] for k in GAP_KINDS})


if __name__ == "__main__":
    unittest.main()
