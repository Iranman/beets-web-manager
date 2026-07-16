import unittest

from backend.mb_alignment import summarize_mb_track_alignment


def _track(num, title, mbid=None):
    return {
        "disc": 1,
        "track": num,
        "title": title,
        "mb_trackid": mbid or f"target-{num}",
        "duration_ms": 180000,
    }


GET_RICH_MB_TRACKS = [
    _track(1, "Intro"),
    _track(2, "What Up Gangsta"),
    _track(3, "Patiently Waiting"),
    _track(4, "Many Men (Wish Death)"),
    _track(5, "In da Club"),
    _track(6, "High All the Time"),
    _track(7, "Heat"),
    _track(8, "If I Can't"),
    _track(9, "Blood Hound"),
    _track(10, "Back Down"),
    _track(11, "P.I.M.P."),
    _track(12, "Like My Style"),
    _track(13, "Poor Lil Rich"),
    _track(14, "21 Questions"),
    _track(15, "Don't Push Me"),
    _track(16, "Gotta Make It to Heaven"),
    _track(17, "Wanksta"),
    _track(18, "U Not Like Me"),
    _track(19, "Life's on the Line"),
]


def _item(item_id, track, title, mbid="old-release-id"):
    return {
        "id": item_id,
        "disc": 1,
        "track": track,
        "title": title,
        "path": f"50 Cent/Get Rich or Die Tryin'/{track:02d}-{title}.flac",
        "filename": f"{track:02d}-{title}.flac",
        "mb_trackid": mbid,
        "length": 180.0,
    }


def _fake_match(item, mb_tracks):
    title = item.get("title", "")
    for idx, track in enumerate(mb_tracks):
        if track["title"] == title:
            return {"idx": idx, "score": 1.0, "title_score": 1.0, "exact_mbid": False}
    return {"idx": -1, "score": 0.0, "title_score": 0.0, "exact_mbid": False}


class MbAlignmentTests(unittest.TestCase):
    def test_get_rich_style_alignment_classifies_missing_extra_and_repairable(self):
        items = [
            _item(3840, 2, "What Up Gangsta"),
            _item(3841, 3, "Patiently Waiting"),
            _item(3842, 4, "Many Men (Wish Death)"),
            _item(3843, 5, "In da Club", "duplicate-old-id"),
            _item(3857, 5, "In da Club", "duplicate-old-id"),
            _item(3844, 6, "High All the Time"),
            _item(3845, 0, "Heat", ""),
            _item(3846, 7, "If I Can't"),
            _item(3847, 8, "Blood Hound"),
            _item(3848, 9, "Back Down"),
            _item(3849, 10, "P.I.M.P."),
            _item(3850, 11, "Like My Style"),
            _item(3851, 12, "Poor Lil Rich"),
            _item(3852, 13, "21 Questions"),
            _item(3853, 15, "Gotta Make It to Heaven"),
            _item(3854, 16, "Wanksta"),
            _item(3855, 17, "U Not Like Me"),
            _item(3856, 18, "Life's on the Line"),
        ]

        result = summarize_mb_track_alignment(
            items,
            GET_RICH_MB_TRACKS,
            match_fn=_fake_match,
            threshold=0.82,
            repair_threshold=0.72,
        )

        self.assertEqual(result["expected_count"], 19)
        self.assertEqual(result["actual_count"], 18)
        self.assertEqual(result["missing_count"], 2)
        self.assertEqual(
            [(row["track"], row["title"]) for row in result["missing"]],
            [(1, "Intro"), (15, "Don't Push Me")],
        )
        self.assertEqual(result["extra_count"], 1)
        self.assertEqual(result["extra_items"][0]["id"], 3857)
        self.assertEqual(result["tracks"][4]["item"]["id"], 3843)
        self.assertEqual(result["mb_trackid_missing_count"], 1)
        self.assertEqual(result["mb_trackid_mismatch_count"], 16)
        self.assertEqual(result["mb_repairable_count"], 17)
        self.assertEqual(result["mb_duplicate_recording_id_count"], 15)
        groups = {
            group["mb_trackid"]: group
            for group in result["duplicate_recording_groups"]
        }
        self.assertEqual(groups["duplicate-old-id"]["duplicate_count"], 1)
        self.assertEqual(
            [row["id"] for row in groups["duplicate-old-id"]["items"]],
            [3843, 3857],
        )
        self.assertEqual(groups["old-release-id"]["duplicate_count"], 14)

    def test_expected_repeated_recording_id_is_not_reported_as_duplicate(self):
        mb_tracks = [
            _track(1, "Radio Edit", "same-recording"),
            _track(2, "Album Edit", "same-recording"),
        ]
        items = [
            _item(10, 1, "Radio Edit", "same-recording"),
            _item(11, 2, "Album Edit", "same-recording"),
        ]

        result = summarize_mb_track_alignment(
            items,
            mb_tracks,
            match_fn=_fake_match,
            threshold=0.82,
            repair_threshold=0.72,
        )

        self.assertEqual(result["missing_count"], 0)
        self.assertEqual(result["mb_duplicate_recording_id_count"], 0)
        self.assertEqual(result["duplicate_recording_groups"], [])


if __name__ == "__main__":
    unittest.main()
