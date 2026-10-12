"""QA 0.4.0: a track removed in Web Manager must leave the Plex playlist even
when Plex's title text differs from the Web Manager row's title.

User decision: "Web Manager list, synced in place" -- removals happen only
through Web Manager and must reach Plex. Reproduced end to end on the 0.4.0
integration branch: a legacy path-only m3u entry gets a filename-derived
title ("Album One - 03 - Song 3 of Album One") while Plex reports the tag
title ("Song 3 of Album One"); after the Web Manager remove, sync kept the
entry in Plex forever (moved to the end as an "extra"). The tombstone carries
the file path, but _plex_track_removed_in_web_manager only compares
artist/title text.

expectedFailure until the owner fixes it; remove the decorator then.
"""
import unittest

import app as app_module  # noqa: F401  -- loads the module family in order
import backend.playlist_service as ps

PATH = "/music/QA Artist A/Album One (2020)/QA Artist A - Album One - 03 - Song 3 of Album One.mp3"


def _plex_track(title):
    return {
        "ratingKey": "1003",
        "playlistItemID": "7",
        "title": title,
        "grandparentTitle": "QA Artist A",
        "Media": [{"Part": [{"file": PATH}]}],
    }


def _manifest(tombstone_title):
    return {"removed_tracks": [{"artist": "QA Artist A", "title": tombstone_title, "path": PATH}]}


class PlexRemovalPropagationTests(unittest.TestCase):
    def test_text_match_is_removable(self):
        # Control: same title text on both sides already works.
        self.assertTrue(ps._plex_track_removed_in_web_manager(
            _plex_track("Song 3 of Album One"), _manifest("Song 3 of Album One")))

    @unittest.expectedFailure
    def test_same_file_with_different_title_text_is_removable(self):
        self.assertTrue(ps._plex_track_removed_in_web_manager(
            _plex_track("Song 3 of Album One"), _manifest("Album One - 03 - Song 3 of Album One")))

    def test_unrelated_track_is_not_removable(self):
        other = _plex_track("Song 3 of Album One")
        other["Media"] = [{"Part": [{"file": "/music/Other/Other.mp3"}]}]
        self.assertFalse(ps._plex_track_removed_in_web_manager(
            other, {"removed_tracks": [{"artist": "QA Artist A", "title": "Something Else", "path": PATH}]}))


if __name__ == "__main__":
    unittest.main()
