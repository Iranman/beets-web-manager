"""helpers_mb release parsing and ai_service's candidate binding share one implementation."""
import io
import json
import unittest
from unittest import mock

import helpers_mb
from backend import ai_service

REL = "33333333-3333-3333-3333-333333333333"
RG = "44444444-4444-4444-4444-444444444444"
RAW = {
    "id": REL, "score": 90, "title": "Album", "date": "2012-09-23", "country": "US",
    "artist-credit": [{"artist": {"name": "Artist"}}],
    "release-group": {"id": RG, "primary-type": "Album"},
    "label-info": [{"label": {"name": "L1"}, "catalog-number": "C1"}, {"label": None, "catalog-number": ""},
                   "junk", {"label": {"name": "L2"}}],
    "media": [{"format": "CD", "track-count": 10, "position": 1}, "junk",
              {"format": '12" Vinyl', "track-count": 2}, {"track-count": 1, "position": 9}],
    "cover-art-archive": {"artwork": True, "front": False, "count": 3},
}
SHARED = ("label", "labels", "catalog_numbers", "label_entries", "tracks", "formats", "mediums",
          "format_summary", "is_vinyl", "cover_art", "front_art", "cover_art_count",
          "mb_releasegroupid", "mb_albumid", "country")


def _opened(payload):
    return mock.patch.object(helpers_mb.provider_boundary, "opened",
                             side_effect=lambda *a, **k: io.BytesIO(json.dumps(payload).encode()))


class ReleaseParsingTests(unittest.TestCase):
    def test_lookup_and_search_agree_on_a_release(self):
        with _opened(RAW):
            looked_up = helpers_mb._fetch_mb_release_candidate(REL)
        with _opened({"releases": [RAW]}):
            searched = helpers_mb._mb_release_search("Album", "Artist")[0]
        for key in SHARED:
            with self.subTest(key):
                self.assertEqual(looked_up[key], searched[key])
        self.assertEqual(looked_up["labels"], ["L1", "L2"])
        self.assertEqual(looked_up["tracks"], 13)
        self.assertEqual(looked_up["format_summary"], 'CD (10) + 12" Vinyl (2) + Medium (1)')
        self.assertTrue(looked_up["is_vinyl"])
        self.assertEqual(looked_up["mediums"][1]["position"], 2)

    def test_no_cover_art_block(self):
        self.assertEqual(helpers_mb._cover_art_details(None), (None, None, 0))


class BindSelectedReleaseTests(unittest.TestCase):
    CAND = {"mb_albumid": REL, "mb_releasegroupid": RG, "mb_releasegroupurl": "u", "release_group_primary_type": "Album"}

    def test_index_binds_identity_from_the_candidate_not_the_ai(self):
        sug = {"candidate_index": 0, "mb_albumid": "ai-made-up", "mb_releasegroupid": "ai-made-up", "confidence": "high"}
        selected, mb_id = ai_service._ai_bind_selected_release(sug, [self.CAND])
        self.assertIs(selected, self.CAND)
        self.assertEqual((mb_id, sug["mb_albumid"], sug["mb_releasegroupid"]), (REL, REL, RG))
        self.assertEqual(sug["confidence"], "high")
        self.assertTrue(sug["mb_valid"])

    def test_out_of_range_index_selects_nothing_and_downgrades(self):
        sug = {"candidate_index": 5, "mb_albumid": REL, "mb_releasegroupid": RG, "confidence": "high"}
        selected, mb_id = ai_service._ai_bind_selected_release(sug, [self.CAND])
        self.assertIsNone(selected)
        self.assertEqual((mb_id, sug["mb_albumid"], sug["mb_releasegroupid"]), ("", "", ""))
        self.assertEqual((sug["confidence"], sug["candidate_index"], sug["mb_valid"]), ("low", -1, False))

    def test_missing_release_group_comes_from_musicbrainz_never_the_release_id(self):
        cand = {"mb_albumid": REL}
        with mock.patch.object(ai_service, "_fetch_mb_release_tracklist",
                               return_value={"ok": True, "release_group": RG, "release_group_primary_type": "EP"}):
            sug = {"candidate_index": 0}
            ai_service._ai_bind_selected_release(sug, [cand])
        self.assertEqual((sug["mb_releasegroupid"], sug["release_group_primary_type"]), (RG, "EP"))
        with mock.patch.object(ai_service, "_fetch_mb_release_tracklist", return_value={"ok": False}):
            sug = {"candidate_index": 0}
            ai_service._ai_bind_selected_release(sug, [cand])
        self.assertEqual(sug["mb_releasegroupid"], "")


if __name__ == "__main__":
    unittest.main()
