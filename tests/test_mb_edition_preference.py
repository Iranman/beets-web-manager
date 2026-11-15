"""MB_EDITION_PREFERENCE: configurable edition (release) preference applied in
the MusicBrainz release-search ranker (helpers_mb._mb_release_search). It is
SECONDARY to tracklist fit and only ever reorders editions WITHIN a Release
Group. Default (unset) reproduces the long-standing non-vinyl/US ranking.
Synthetic data only; the provider boundary is mocked."""
import io
import json
import unittest
from unittest import mock

import helpers_mb

RG = "44444444-4444-4444-4444-444444444444"
REL_CD = "c0000000-0000-0000-0000-0000000000cd"
REL_VINYL = "11110000-0000-0000-0000-00000000v1nl"
REL_US = "c0000000-0000-0000-0000-0000000000us"
REL_GB = "c0000000-0000-0000-0000-0000000000gb"


def _raw(rid, country="US", fmt="CD", tracks=10, rg=RG, score=90, primary="Album"):
    return {"id": rid, "score": score, "title": "Album", "date": "2012-01-01",
            "country": country, "artist-credit": [{"artist": {"name": "Artist"}}],
            "release-group": {"id": rg, "primary-type": primary},
            "media": [{"format": fmt, "track-count": tracks, "position": 1}]}


def _opened(releases):
    return mock.patch.object(helpers_mb.provider_boundary, "opened",
                             side_effect=lambda *a, **k: io.BytesIO(
                                 json.dumps({"releases": releases}).encode()))


def _winner(releases, pref, **kwargs):
    """The release id _mb_release_search returns as a Release Group's best
    edition, under a given parsed preference."""
    with mock.patch.object(helpers_mb, "_EDITION_PREFERENCE", pref), _opened(releases):
        found = helpers_mb._mb_release_search("Album", "Artist", **kwargs)
    return found[0]["mb_albumid"]


class EditionPreferenceParseTests(unittest.TestCase):
    def test_unset_is_empty_and_default_module_value_is_empty(self):
        self.assertEqual(helpers_mb._parse_edition_preference(""), {})
        self.assertEqual(helpers_mb._parse_edition_preference("   "), {})
        self.assertEqual(helpers_mb._EDITION_PREFERENCE, {})  # nothing set in the test env

    def test_parses_three_ordered_fields_case_insensitively(self):
        pref = helpers_mb._parse_edition_preference("Format=Vinyl,CD; country=US , GB ;type=album")
        self.assertEqual(pref, {"format": ("vinyl", "cd"), "country": ("us", "gb"), "type": ("album",)})

    def test_unknown_or_empty_fields_are_ignored(self):
        self.assertEqual(helpers_mb._parse_edition_preference("bogus=x; format=; =vinyl"), {})

    def test_default_key_reproduces_non_vinyl_us_ranking(self):
        key = helpers_mb._edition_preference_key
        cd_us = {"is_vinyl": False, "country": "US", "release_group_primary_type": "Album"}
        vinyl_us = {"is_vinyl": True, "country": "US", "release_group_primary_type": "Album"}
        cd_gb = {"is_vinyl": False, "country": "GB", "release_group_primary_type": "Album"}
        cd_jp = {"is_vinyl": False, "country": "JP", "release_group_primary_type": "Album"}
        self.assertLess(key(cd_us, {}), key(vinyl_us, {}))   # non-vinyl before vinyl
        self.assertLess(key(cd_us, {}), key(cd_gb, {}))      # US before GB
        self.assertLess(key(cd_gb, {}), key(cd_jp, {}))      # known country before unknown
        self.assertEqual(key(cd_us, {})[2], key(vinyl_us, {})[2])  # type term is a no-op by default


class EditionPreferenceRankingTests(unittest.TestCase):
    def test_default_prefers_non_vinyl(self):
        rels = [_raw(REL_VINYL, fmt='12" Vinyl'), _raw(REL_CD, fmt="CD")]
        self.assertEqual(_winner(rels, {}), REL_CD)

    def test_default_prefers_us_country(self):
        rels = [_raw(REL_GB, country="GB"), _raw(REL_US, country="US")]
        self.assertEqual(_winner(rels, {}), REL_US)

    def test_format_preference_reorders_editions(self):
        rels = [_raw(REL_VINYL, fmt='12" Vinyl'), _raw(REL_CD, fmt="CD")]
        self.assertEqual(_winner(rels, {"format": ("vinyl", "cd")}), REL_VINYL)

    def test_country_preference_reorders_editions(self):
        rels = [_raw(REL_GB, country="GB"), _raw(REL_US, country="US")]
        self.assertEqual(_winner(rels, {"country": ("gb", "us")}), REL_GB)

    def test_fit_outranks_preference(self):
        # Mutation guard: tracklist fit is PRIMARY. The preferred-format edition
        # has the WRONG track count; the non-preferred edition matches the folder.
        # Fit must win, so the matching edition is chosen despite the preference.
        # FAILS if the preference were ranked above track-count fit.
        wrong_count_vinyl = _raw(REL_VINYL, fmt='12" Vinyl', tracks=7)
        right_count_cd = _raw(REL_CD, fmt="CD", tracks=10)
        rels = [wrong_count_vinyl, right_count_cd]
        self.assertEqual(_winner(rels, {"format": ("vinyl", "cd")}, track_count=10), REL_CD)


if __name__ == "__main__":
    unittest.main()
