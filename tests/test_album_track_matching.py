import ast
import re
import unittest
from pathlib import Path
from typing import Any, Dict, List


APP_SOURCE = Path(__file__).resolve().parents[1] / "app.py"


def _load_matcher_namespace():
    tree = ast.parse(APP_SOURCE.read_text(encoding="utf-8"))
    names = {
        "_ALBUM_TRACK_PREFIX_RE",
        "_ALBUM_TRACK_ANNOT_RE",
        "_ALBUM_TRACK_UNCLOSED_RE",
        "_ALBUM_TRACK_TRAILING_ALIAS_RE",
        "_ALBUM_TRACK_VERSION_MARKER_RE",
        "_ALBUM_TRACK_FEATURE_SUFFIX_RE",
        "_ALBUM_TRACK_GLUED_FEATURE_SUFFIX_RE",
        "_TRACK_FILENAME_SOURCE_ID_SUFFIX_RE",
        "_TRACK_FILENAME_SHORT_SOURCE_ID_SUFFIX_RE",
        "_strip_track_filename_id_suffix",
        "_track_filename_has_source_id_suffix",
        "_slskd_title_guess_from_name",
        "_album_track_norm",
        "_album_track_feature_variants",
        "_album_track_parenthetical_alias_variants",
        "_album_track_path_prefixes",
        "_album_track_title_variants",
        "_album_track_score",
        "_best_album_track_match",
    }
    ns = {
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "Path": Path,
        "re": re,
        "_s": lambda value: (
            value.decode("utf-8", errors="replace")
            if isinstance(value, bytes)
            else str(value or "")
        ),
        "_album_item_position_hints": lambda item: (
            int(item.get("disc") or 1),
            int(item.get("track") or 0),
        ),
    }
    for node in tree.body:
        node_name = ""
        if isinstance(node, ast.Assign):
            node_name = getattr(node.targets[0], "id", "")
        elif isinstance(node, ast.FunctionDef):
            node_name = node.name
        if node_name in names:
            mod = ast.Module(body=[node], type_ignores=[])
            ast.fix_missing_locations(mod)
            exec(compile(mod, str(APP_SOURCE), "exec"), ns)
    return ns


class AlbumTrackMatchingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matcher = _load_matcher_namespace()

    def test_parenthetical_alias_matches_short_musicbrainz_title(self):
        variants = self.matcher["_album_track_title_variants"](
            "Money (That's What I Want)",
            "",
        )

        self.assertIn("money", variants)

        score = self.matcher["_album_track_score"](
            {
                "title": "Money (That's What I Want)",
                "track": 14,
                "disc": 1,
                "length": 168.973,
            },
            {
                "title": "Money",
                "title_norm": "money",
                "track": 14,
                "disc": 1,
                "duration_ms": 168000,
            },
        )

        self.assertGreaterEqual(score, 0.98)

    def test_full_parenthetical_title_still_matches_full_musicbrainz_title(self):
        score = self.matcher["_album_track_score"](
            {
                "title": "Many Men (Wish Death)",
                "track": 4,
                "disc": 1,
                "length": 180.0,
            },
            {
                "title": "Many Men (Wish Death)",
                "title_norm": "many men wish death",
                "track": 4,
                "disc": 1,
                "duration_ms": 180000,
            },
        )

        self.assertGreaterEqual(score, 0.98)

    def test_lidarr_source_id_suffix_is_stripped_for_matching(self):
        strip = self.matcher["_strip_track_filename_id_suffix"]
        has_suffix = self.matcher["_track_filename_has_source_id_suffix"]

        self.assertEqual(strip("Syrup Damage_639189505313367522"), "Syrup Damage")
        self.assertEqual(strip("2 Feet-639189505752092846"), "2 Feet")
        self.assertEqual(strip("You Marvelous (639189505577102143)"), "You Marvelous")
        self.assertEqual(strip("Cheesestix Fast Break [639189505596742656]"), "Cheesestix Fast Break")
        self.assertTrue(has_suffix("Syrup Damage_639189505313367522"))

    def test_lidarr_dirty_titles_match_clean_musicbrainz_titles(self):
        norm = self.matcher["_album_track_norm"]
        score_fn = self.matcher["_album_track_score"]
        cases = [
            ("Syrup Damage_639189505313367522", "Syrup Damage"),
            ("2 Feet_639189505752092846", "2 Feet"),
        ]

        for dirty, clean in cases:
            with self.subTest(dirty=dirty):
                score = score_fn(
                    {"title": dirty, "track": 2, "disc": 1, "length": 180.0},
                    {
                        "title": clean,
                        "title_norm": norm(clean),
                        "track": 2,
                        "disc": 1,
                        "duration_ms": 180000,
                    },
                )
                self.assertGreaterEqual(score, 0.98)

    def test_short_hash_source_id_suffix_is_stripped_for_matching(self):
        strip = self.matcher["_strip_track_filename_id_suffix"]
        has_suffix = self.matcher["_track_filename_has_source_id_suffix"]

        self.assertEqual(strip("spesh-trust_life_(feat_benny)-b3e356"), "spesh-trust_life_(feat_benny)")
        self.assertEqual(strip("spesh-stay_up-e7a4fd"), "spesh-stay_up")
        self.assertEqual(strip("be_somebody-e0e6db"), "be_somebody")
        self.assertEqual(strip("spesh-rely_on_that_(feat_klass_murda)-0a35e0"), "spesh-rely_on_that_(feat_klass_murda)")
        self.assertEqual(strip("01-38_spesh-intro_(feat_uncle_black)-28bb"), "01-38_spesh-intro_(feat_uncle_black)")
        self.assertTrue(has_suffix("spesh-tony_toca_freestyle-604ebf"))
        self.assertTrue(has_suffix("01-38_spesh-intro_(feat_uncle_black)-28bb"))
    def test_slskd_title_guess_strips_track_artist_prefix_feature_and_short_hash(self):
        guess = self.matcher["_slskd_title_guess_from_name"]

        self.assertEqual(
            guess("01-38_spesh-intro_(feat_uncle_black)-28bb.mp3"),
            "intro_(feat_uncle_black)",
        )
        self.assertEqual(
            guess("03-38_spesh-my_gun_(feat_klass_murda_and_benny)-28bb.mp3"),
            "my_gun_(feat_klass_murda_and_benny)",
        )

    def test_dirty_spesh_titles_match_clean_musicbrainz_titles(self):
        norm = self.matcher["_album_track_norm"]
        score_fn = self.matcher["_album_track_score"]
        cases = [
            ("spesh-trust_life_(feat_benny)-b3e356", "Trust Life"),
            ("spesh-my_gun_(feat_klass_murda_and_benny)-e7a4fd", "My Gun"),
            ("spesh-stay_up-e7a4fd", "Stay Up"),
            ("be_somebody-e0e6db", "Be Somebody"),
            ("spesh-tony_toca_freestyle-604ebf", "Tony Toca Freestyle"),
            ("spesh-rely_on_that_(feat_klass_murda)-0a35e0", "Rely On That"),
            ("01-38_spesh-intro_(feat_uncle_black)-28bb", "Intro"),
            ("03-38_spesh-my_gun_(feat_klass_murda_and_benny)-28bb", "My Gun"),
            ("05-38_spesh-homicide_(feat_benny_and_klass_murda)-28bb", "Homicide"),
            ("12-38_spesh-about_me_(feat_klass_murda_and_benny)-28bb", "About Me"),
        ]

        for dirty, clean in cases:
            with self.subTest(dirty=dirty):
                score = score_fn(
                    {
                        "title": dirty,
                        "path": f"/data/torrents/music/38 Spesh/The Trust Tape/{dirty}.flac",
                        "track": 1,
                        "disc": 1,
                        "length": 180.0,
                    },
                    {
                        "title": clean,
                        "title_norm": norm(clean),
                        "track": 1,
                        "disc": 1,
                        "duration_ms": 180000,
                    },
                )
                self.assertGreaterEqual(score, 0.98)

    def test_best_match_maps_dirty_spesh_titles_to_release_tracks(self):
        norm = self.matcher["_album_track_norm"]
        best_match = self.matcher["_best_album_track_match"]
        mb_tracks = [
            {"title": "Intro", "title_norm": norm("Intro"), "track": 1, "disc": 1, "duration_ms": 0},
            {"title": "Trust Life", "title_norm": norm("Trust Life"), "track": 2, "disc": 1, "duration_ms": 0},
            {"title": "My Gun", "title_norm": norm("My Gun"), "track": 3, "disc": 1, "duration_ms": 0},
            {"title": "Stay Up", "title_norm": norm("Stay Up"), "track": 4, "disc": 1, "duration_ms": 0},
            {"title": "Homicide", "title_norm": norm("Homicide"), "track": 5, "disc": 1, "duration_ms": 0},
            {"title": "Be Somebody", "title_norm": norm("Be Somebody"), "track": 6, "disc": 1, "duration_ms": 0},
            {"title": "Trust Firm", "title_norm": norm("Trust Firm"), "track": 7, "disc": 1, "duration_ms": 0},
            {"title": "Tony Toca Freestyle", "title_norm": norm("Tony Toca Freestyle"), "track": 8, "disc": 1, "duration_ms": 0},
            {"title": "Rely On That", "title_norm": norm("Rely On That"), "track": 9, "disc": 1, "duration_ms": 0},
            {"title": "About Me", "title_norm": norm("About Me"), "track": 12, "disc": 1, "duration_ms": 0},
        ]
        cases = [
            ("01-38_spesh-intro_(feat_uncle_black)-28bb", "Intro"),
            ("03-38_spesh-my_gun_(feat_klass_murda_and_benny)-28bb", "My Gun"),
            ("04-38_spesh-stay_up-e7a4fd", "Stay Up"),
            ("05-38_spesh-homicide_(feat_benny_and_klass_murda)-28bb", "Homicide"),
            ("12-38_spesh-about_me_(feat_klass_murda_and_benny)-28bb", "About Me"),
            ("spesh-trust_life_(feat_benny)-b3e356", "Trust Life"),
            ("be_somebody-e0e6db", "Be Somebody"),
            ("spesh-trust_firm_(feat_niddi_villin)-e0e6db", "Trust Firm"),
            ("spesh-tony_toca_freestyle-604ebf", "Tony Toca Freestyle"),
            ("spesh-rely_on_that_(feat_klass_murda)-0a35e0", "Rely On That"),
        ]

        for dirty, clean in cases:
            with self.subTest(dirty=dirty):
                match = best_match(
                    {
                        "title": dirty,
                        "path": f"/data/torrents/music/38 Spesh/The Trust Tape/{dirty}.flac",
                        "track": 0,
                        "disc": 1,
                        "length": 0,
                    },
                    mb_tracks,
                )
                self.assertEqual(match["track"]["title"], clean)
                self.assertGreaterEqual(match["score"], 0.82)
    def test_feature_text_does_not_block_matching(self):
        norm = self.matcher["_album_track_norm"]
        score = self.matcher["_album_track_score"](
            {
                "title": "trust_life_(feat_benny)-b3e356",
                "path": "/data/torrents/music/38 Spesh/The Trust Tape/trust_life_(feat_benny)-b3e356.flac",
                "track": 1,
                "disc": 1,
                "length": 180.0,
            },
            {
                "title": "Trust Life",
                "title_norm": norm("Trust Life"),
                "track": 1,
                "disc": 1,
                "duration_ms": 180000,
            },
        )

        self.assertGreaterEqual(score, 0.98)

    def test_feature_text_still_matches_when_musicbrainz_title_includes_feature(self):
        norm = self.matcher["_album_track_norm"]
        score = self.matcher["_album_track_score"](
            {
                "title": "trust_life_(feat_benny)-b3e356",
                "path": "/data/torrents/music/38 Spesh/The Trust Tape/trust_life_(feat_benny)-b3e356.flac",
                "track": 1,
                "disc": 1,
                "length": 180.0,
            },
            {
                "title": "Trust Life (feat. Benny)",
                "title_norm": norm("Trust Life (feat. Benny)"),
                "track": 1,
                "disc": 1,
                "duration_ms": 180000,
            },
        )

        self.assertGreaterEqual(score, 0.98)

    def test_real_title_numbers_are_preserved(self):
        strip = self.matcher["_strip_track_filename_id_suffix"]
        has_suffix = self.matcher["_track_filename_has_source_id_suffix"]

        self.assertEqual(strip("99 Problems"), "99 Problems")
        self.assertEqual(strip("2 Phones"), "2 Phones")
        self.assertEqual(strip("6 Foot 7 Foot"), "6 Foot 7 Foot")
        self.assertEqual(strip("4 Da Gang"), "4 Da Gang")
        self.assertFalse(has_suffix("99 Problems"))
        self.assertFalse(has_suffix("2 Phones"))
        self.assertFalse(has_suffix("6 Foot 7 Foot"))
        self.assertFalse(has_suffix("4 Da Gang"))
        self.assertEqual(strip("love-dead"), "love-dead")


if __name__ == "__main__":
    unittest.main()



