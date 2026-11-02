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
    "media": [{"format": "CD", "track-count": 10, "position": 1},
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

    def test_invalid_count_makes_the_release_size_unknown(self):
        for bad in ("x", None, "", -1, 1.5, True, [], "junk-medium"):
            medium = bad if bad == "junk-medium" else {"format": "CD", "track-count": bad}
            with self.subTest(bad=bad):
                media = [{"format": "CD", "track-count": 10}, medium]
                self.assertEqual(helpers_mb._mb_release_track_count({"media": media}), 0)
                self.assertEqual(helpers_mb._media_summary(media)[0], 0)
        self.assertEqual(helpers_mb._mb_release_track_count({"media": [{"track-count": 10}, {"track-count": "2"}]}), 12)
        self.assertEqual(helpers_mb._mb_release_track_count({}), 0)

    def test_invalid_count_never_beats_a_valid_edition(self):
        bad = {"id": "bad", "status": "Official", "country": "US", "media": [{"track-count": 10}, {"track-count": "x"}]}
        good = {"id": "good", "status": "Official", "country": "JP", "media": [{"track-count": 10}]}
        for releases in ([bad, good], [good, bad]):
            with _opened({"releases": releases}):
                self.assertEqual(helpers_mb._resolve_release_group_to_release(RG, [], track_count=10), "good")

        def raw(rel_id, country, counts):
            return {**RAW, "id": rel_id, "country": country, "media": [{"format": "CD", "track-count": c} for c in counts]}
        with _opened({"releases": [raw(REL, "US", [10, "x"]), raw(RG, "JP", [10])]}):
            found = helpers_mb._mb_release_search("Album", "Artist", track_count=10)
        self.assertEqual(found[0]["mb_albumid"], RG)

    def test_unknown_count_ranks_below_a_valid_non_exact_edition(self):
        def raw(rel_id, counts):
            return {**RAW, "id": rel_id, "media": [{"format": "CD", "track-count": c} for c in counts]}
        for releases in ([raw(REL, [10, None]), raw(RG, [7])], [raw(RG, [7]), raw(REL, [10, None])]):
            with _opened({"releases": releases}):
                found = helpers_mb._mb_release_search("Album", "Artist", track_count=3)
            self.assertEqual(found[0]["mb_albumid"], RG)

    def test_unknown_count_ranks_below_a_valid_edition_at_any_distance(self):
        def raw(rel_id, count):
            return {**RAW, "id": rel_id, "media": [{"format": "CD", "track-count": count}]}
        for wanted, valid in ((10, 1500), (1200, 10), (10, 1009)):
            for releases in ([raw(REL, None), raw(RG, valid)], [raw(RG, valid), raw(REL, None)]):
                with self.subTest(wanted=wanted, valid=valid), _opened({"releases": releases}):
                    found = helpers_mb._mb_release_search("Album", "Artist", track_count=wanted)
                    self.assertEqual(found[0]["mb_albumid"], RG)

    def test_resolver_ranks_unknown_count_below_a_valid_edition(self):
        def rel(rel_id, count, status="Official"):
            return {"id": rel_id, "status": status, "country": "US", "media": [{"track-count": count}]}
        for wanted, valid in ((10, 1500), (1200, 10), (10, 1009)):
            with self.subTest(wanted=wanted, valid=valid), _opened({"releases": [rel(REL, None), rel(RG, valid)]}):
                self.assertEqual(helpers_mb._resolve_release_group_to_release(RG, [], track_count=wanted), RG)
        for media in ([], [{"track-count": None}], [{"track-count": 0}]):
            releases = [{"id": REL, "status": "Official", "country": "US", "media": media},
                        rel(RG, 10, status="Promotion")]
            with self.subTest(media=media), _opened({"releases": releases}):
                self.assertEqual(helpers_mb._resolve_release_group_to_release(RG, [], track_count=10), RG)

    def test_resolver_still_prefers_official_among_valid_counts(self):
        releases = [{"id": REL, "status": "Promotion", "country": "US", "media": [{"track-count": 10}]},
                    {"id": RG, "status": "Official", "country": "US", "media": [{"track-count": 12}]}]
        with _opened({"releases": releases}):
            self.assertEqual(helpers_mb._resolve_release_group_to_release(RG, [], track_count=10), RG)

    def test_scorer_never_rates_an_unknown_count_above_a_valid_edition(self):
        base = {"artist": "A", "album": "B", "year": "2012", "country": "US", "score": 90}
        folder = {"guessed_artist": "A", "guessed_album": "B", "guessed_year": "2012", "folder_track_count": 10}
        score = ai_service._score_mb_release_candidate
        unknown = score(folder, {**base, "tracks": 0})
        for tracks in (14, 18, 1009, 1500):
            valid = score(folder, {**base, "tracks": tracks})
            self.assertGreaterEqual(valid["total"], unknown["total"], tracks)
        self.assertEqual(score(folder, {**base, "tracks": 1009})["track_count_score"], 0.0)
        # Without a local count, nothing can be compared: neutral, as before.
        self.assertEqual(score({**folder, "folder_track_count": 0}, {**base, "tracks": 10})["track_count_score"], 0.5)

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
