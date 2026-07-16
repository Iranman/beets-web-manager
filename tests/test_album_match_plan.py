import unittest

from backend.album_match import build_album_match_plan


MB_TRACKS = [
    {"disc": 1, "track": 1, "title": "Right Song", "mb_trackid": "mb-1"},
    {"disc": 1, "track": 2, "title": "Second Song", "mb_trackid": "mb-2"},
]


def _fake_match(item, _mb_tracks):
    title = item.get("title", "")
    if title == "Right Song":
        return {"idx": 0, "score": 0.98, "exact_mbid": False}
    if title == "Right Song Duplicate":
        return {"idx": 0, "score": 0.88, "exact_mbid": False}
    if title == "Second Song":
        return {"idx": 1, "score": 0.98, "exact_mbid": False}
    if title == "Exact ID Different Title":
        return {"idx": 1, "score": 0.2, "exact_mbid": True}
    return {"idx": 0, "score": 0.31, "exact_mbid": False}


class AlbumMatchPlanTests(unittest.TestCase):
    def _plan(self, items, *, exists=True):
        return build_album_match_plan(
            album_id=12,
            mb_albumid="ABCDEFAB-1234-4321-ABCD-ABCDEFABCDEF",
            release_title="Selected Release",
            items=items,
            mb_tracks=MB_TRACKS,
            match_fn=_fake_match,
            file_exists_fn=lambda _path: exists,
            threshold=0.82,
        )

    def test_wrong_manual_release_marks_all_items_unmatched_before_tagging(self):
        plan = self._plan([
            {"id": 1, "title": "Wrong Album Song", "path": "Artist/Album/01.flac"},
            {"id": 2, "title": "Another Wrong Song", "path": "Artist/Album/02.flac"},
        ])

        self.assertEqual(plan["matched_count"], 0)
        self.assertEqual(plan["unmatched_count"], 2)
        self.assertEqual([item["id"] for item in plan["unmatched_items"]], [1, 2])

    def test_partial_manual_release_keeps_only_matching_items(self):
        plan = self._plan([
            {"id": 1, "title": "Right Song", "path": "Artist/Album/01.flac"},
            {"id": 2, "title": "Wrong Album Song", "path": "Artist/Album/02.flac"},
        ])

        self.assertEqual([item["id"] for item in plan["matched_items"]], [1])
        self.assertEqual([item["id"] for item in plan["unmatched_items"]], [2])

    def test_duplicate_musicbrainz_track_keeps_best_scored_item_only(self):
        plan = self._plan([
            {"id": 1, "title": "Right Song Duplicate", "path": "Artist/Album/01 copy.flac"},
            {"id": 2, "title": "Right Song", "path": "Artist/Album/01.flac"},
        ])

        self.assertEqual([item["id"] for item in plan["matched_items"]], [2])
        self.assertEqual([item["id"] for item in plan["unmatched_items"]], [1])

    def test_exact_musicbrainz_recording_id_can_override_low_title_score(self):
        plan = self._plan([
            {"id": 1, "title": "Exact ID Different Title", "path": "Artist/Album/02.flac"},
        ])

        self.assertEqual(plan["matched_count"], 1)
        self.assertEqual(plan["unmatched_count"], 0)

    def test_missing_file_does_not_count_as_matched(self):
        plan = self._plan([
            {"id": 1, "title": "Right Song", "path": "Artist/Album/missing.flac"},
        ], exists=False)

        self.assertEqual(plan["matched_count"], 0)
        self.assertEqual(plan["unmatched_count"], 1)


if __name__ == "__main__":
    unittest.main()
