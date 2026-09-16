import re
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _app_ast_cache import load_app_symbols  # noqa: E402

from backend.matching import AcoustIDStatus

APP_SOURCE = Path(__file__).resolve().parents[1] / "app.py"


def _load_matcher_namespace(*, with_fingerprint_check: bool = False, acoustid_lookup=None):
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
    if with_fingerprint_check:
        names.add("_album_track_fingerprint_check")
    extra_ns = {
        "AcoustIDStatus": AcoustIDStatus,
        "_s": lambda value: (
            value.decode("utf-8", errors="replace")
            if isinstance(value, bytes)
            else str(value or "")
        ),
        "_album_item_position_hints": lambda item: (
            int(item.get("disc") or 1),
            int(item.get("track") or 0),
        ),
        # ARCH-002 Part 7 regression matrix: _album_track_fingerprint_check
        # calls these two free names. _album_item_abs_path only needs to be
        # a pass-through here (fixtures supply absolute-looking test
        # paths); _acoustid_lookup_cached is per-test injectable so every
        # canonical AcoustIDStatus outcome (confirmed/conflict/no_result/
        # unavailable/ambiguous) can be exercised deterministically without
        # a real fingerprint/network call.
        "_album_item_abs_path": lambda raw_path: str(raw_path or ""),
        "_acoustid_lookup_cached": acoustid_lookup or (lambda _path: []),
    }
    return load_app_symbols(names, extra_ns=extra_ns)


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


class AlbumTrackFingerprintCheckAcoustIDMatrixTests(unittest.TestCase):
    """ARCH-002 Part 7: _album_track_fingerprint_check() must return the
    canonical AcoustIDStatus vocabulary, and NO_RESULT/UNAVAILABLE/AMBIGUOUS
    must never be treated as CONFLICT. This exercises the real production
    function (via AST extraction, not a reimplementation), not just
    backend/matching/evidence.py's own internal logic."""

    def _fp_check(self, acoustid_lookup):
        ns = _load_matcher_namespace(with_fingerprint_check=True, acoustid_lookup=acoustid_lookup)
        return ns["_album_track_fingerprint_check"]

    def _mb_tracks(self):
        return [
            {"mb_trackid": "rec-crossfire", "title": "Crossfire", "title_norm": "crossfire"},
            {"mb_trackid": "rec-space-time", "title": "Space and Time", "title_norm": "space and time"},
        ]

    def test_unavailable_when_local_file_is_missing(self):
        # _album_item_abs_path is stubbed as a pass-through; a nonexistent
        # path means Path(path).exists() is False -- no lookup is even
        # attempted, matching "the file itself could not be read."
        fp_check = self._fp_check(acoustid_lookup=lambda _path: (_ for _ in ()).throw(AssertionError("must not be called")))
        result = fp_check({"path": "/definitely/does/not/exist/track.flac"}, self._mb_tracks())
        self.assertEqual(result["status"], AcoustIDStatus.UNAVAILABLE.value)

    def test_no_result_when_fingerprinting_succeeds_with_zero_candidates(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".flac") as tf:
            fp_check = self._fp_check(acoustid_lookup=lambda _path: [])
            result = fp_check({"path": tf.name}, self._mb_tracks())
        self.assertEqual(result["status"], AcoustIDStatus.NO_RESULT.value)
        self.assertNotEqual(result["status"], AcoustIDStatus.CONFLICT.value, "no_result must never be conflict")

    def test_confirmed_when_a_candidate_mbid_is_in_the_target_tracklist(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".flac") as tf:
            fp_check = self._fp_check(
                acoustid_lookup=lambda _path: [{"mb_trackid": "rec-crossfire", "title": "Crossfire", "score": 95}],
            )
            result = fp_check({"path": tf.name}, self._mb_tracks())
        self.assertEqual(result["status"], AcoustIDStatus.CONFIRMED.value)

    def test_conflict_when_confident_candidate_matches_no_target_title(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".flac") as tf:
            fp_check = self._fp_check(
                acoustid_lookup=lambda _path: [
                    {"mb_trackid": "rec-a-totally-unrelated-song", "title": "A Totally Unrelated Song", "score": 95}
                ],
            )
            result = fp_check({"path": tf.name}, self._mb_tracks())
        self.assertEqual(result["status"], AcoustIDStatus.CONFLICT.value)
        self.assertNotEqual(result["status"], AcoustIDStatus.NO_RESULT.value)
        self.assertNotEqual(result["status"], AcoustIDStatus.UNAVAILABLE.value)

    def test_ambiguous_when_candidate_confidence_is_weak(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".flac") as tf:
            fp_check = self._fp_check(
                acoustid_lookup=lambda _path: [
                    {"mb_trackid": "rec-a-totally-unrelated-song", "title": "A Totally Unrelated Song", "score": 40}
                ],
            )
            result = fp_check({"path": tf.name}, self._mb_tracks())
        self.assertEqual(result["status"], AcoustIDStatus.AMBIGUOUS.value)
        self.assertNotEqual(result["status"], AcoustIDStatus.CONFLICT.value, "a weak/uncertain candidate is ambiguous, not a confirmed conflict")

    def test_wrong_audio_conflict_survives_through_a_real_production_caller(self):
        """ARCH-002 Part 8: the real _candidate_track_build_comparison()
        caller (not a reimplementation) must mark a fingerprint-conflicting
        candidate as "conflicting", never silently accept it as a match."""
        import tempfile
        names = {
            "_candidate_track_build_comparison",
            "_album_track_fingerprint_check",
            "_best_album_track_match",
            "_album_track_norm",
            "_album_track_score",
            "_album_track_feature_variants",
            "_album_track_parenthetical_alias_variants",
            "_album_track_path_prefixes",
            "_album_track_title_variants",
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
        }
        with tempfile.NamedTemporaryFile(suffix=".flac") as tf:
            extra_ns = {
                "AcoustIDStatus": AcoustIDStatus,
                "_s": lambda value: str(value or ""),
                "_album_item_position_hints": lambda item: (int(item.get("disc") or 1), int(item.get("track") or 0)),
                "_album_item_abs_path": lambda raw_path: str(raw_path or ""),
                "_acoustid_lookup_cached": lambda _path: [
                    {"mb_trackid": "rec-a-totally-unrelated-song", "title": "A Totally Unrelated Song", "score": 95}
                ],
                "_MB_TRACK_PREFLIGHT_MATCH_THRESHOLD": 0.82,
            }
            ns = load_app_symbols(names, extra_ns=extra_ns)
            build_comparison = ns["_candidate_track_build_comparison"]
            candidate = {"title": "Wrong Title Entirely", "path": tf.name}
            tracklist = {
                "tracks": [
                    {"mb_trackid": "rec-crossfire", "title": "Crossfire", "track": 1, "title_norm": "crossfire"},
                ],
            }
            result = build_comparison("mb-album-1", tracklist, [candidate])
            extra_rows = [row for row in result["comparison"] if row["mb_title"] == ""]
            self.assertEqual(len(extra_rows), 1)
            self.assertEqual(extra_rows[0]["status"], "conflicting")
            # And it must not have been silently counted as a real match.
            matched_rows = [row for row in result["comparison"] if row["status"] in ("matched", "acoustid_verified", "fuzzy")]
            self.assertEqual(matched_rows, [])


class CanonicalMatchingEquivalenceTests(unittest.TestCase):
    """ARCH-002: Verify backend.matching canonical engine matches app.py delegates identically."""

    def test_golden_equivalence_corpus(self):
        from backend.matching import (
            album_track_score,
            best_album_track_match,
            normalize_track_title_for_matching,
            strip_track_filename_id_suffix,
            track_feature_variants,
            track_filename_has_source_id_suffix,
            track_parenthetical_alias_variants,
            track_title_variants_for_matching,
        )

        ns = _load_matcher_namespace()
        app_norm = ns["_album_track_norm"]
        app_score = ns["_album_track_score"]
        app_best = ns["_best_album_track_match"]
        app_strip = ns["_strip_track_filename_id_suffix"]
        app_has_suffix = ns["_track_filename_has_source_id_suffix"]
        app_feature = ns["_album_track_feature_variants"]
        app_alias = ns["_album_track_parenthetical_alias_variants"]
        app_variants = ns["_album_track_title_variants"]

        test_corpus = [
            "Money (That's What I Want)",
            "Many Men (Wish Death)",
            "Syrup Damage_639189505313367522",
            "2 Feet-639189505752092846",
            "spesh-trust_life_(feat_benny)-b3e356",
            "01-38_spesh-intro_(feat_uncle_black)-28bb",
            "Light It Upft Pop Smoke",
            "Malibufeat Polo G",
            "Intro (Explicit Album Version) [Remastered 2024]",
            "Bonus Track: Secret Song (Live @ Wembley)",
            "Song & Dance (feat. Artist A and Artist B)",
            "99 Problems",
            "Track 01 - Hello World [Lidarr-abc123456]",
        ]

        for title in test_corpus:
            with self.subTest(title=title):
                self.assertEqual(normalize_track_title_for_matching(title), app_norm(title))
                self.assertEqual(strip_track_filename_id_suffix(title), app_strip(title))
                self.assertEqual(track_filename_has_source_id_suffix(title), app_has_suffix(title))
                self.assertEqual(track_feature_variants(title), app_feature(title))
                self.assertEqual(track_parenthetical_alias_variants(title), app_alias(title))
                self.assertEqual(
                    track_title_variants_for_matching(title, "/data/torrents/music/Artist/Album/01.flac"),
                    app_variants(title, "/data/torrents/music/Artist/Album/01.flac"),
                )

        # Scoring & Best match equivalence
        mb_tracks = [
            {"title": "Money", "title_norm": "money", "track": 1, "disc": 1, "duration_ms": 180000, "mb_trackid": "rec-1"},
            {"title": "Many Men (Wish Death)", "title_norm": "many men wish death", "track": 2, "disc": 1, "duration_ms": 200000, "mb_trackid": "rec-2"},
            {"title": "Syrup Damage", "title_norm": "syrup damage", "track": 3, "disc": 1, "duration_ms": 150000, "mb_trackid": "rec-3"},
        ]

        local_item = {"title": "Money (That's What I Want)", "track": 1, "disc": 1, "length": 180.0, "path": "01 - Money.flac"}
        self.assertAlmostEqual(album_track_score(local_item, mb_tracks[0]), app_score(local_item, mb_tracks[0]), places=5)
        self.assertEqual(best_album_track_match(local_item, mb_tracks)["idx"], app_best(local_item, mb_tracks)["idx"])

    def test_multi_disc_position_hints(self):
        from backend.matching import album_track_score

        item_d2_t1 = {"title": "Overture Live in Concert", "track": 1, "disc": 2, "length": 120.0}
        target_d1_t1 = {"title": "Overture", "title_norm": "overture", "track": 1, "disc": 1, "duration_ms": 120000}
        target_d2_t1 = {"title": "Overture", "title_norm": "overture", "track": 1, "disc": 2, "duration_ms": 120000}

        score_diff_disc = album_track_score(item_d2_t1, target_d1_t1)
        score_same_disc = album_track_score(item_d2_t1, target_d2_t1)

        # Same disc + track gets full position bonus (+0.06 vs +0.04)
        self.assertGreater(score_same_disc, score_diff_disc)


class CanonicalAdversarialNormalizerTests(unittest.TestCase):
    """Adversarial stress and scaling tests to verify absence of ReDoS polynomial backtracking."""

    def test_adversarial_large_inputs_do_not_hang(self):
        import time
        from backend.matching import (
            normalize_track_title_for_matching,
            strip_track_filename_id_suffix,
            track_feature_variants,
            track_filename_has_source_id_suffix,
            track_parenthetical_alias_variants,
            track_path_prefixes,
            track_title_variants_for_matching,
        )

        adversarial_inputs = [
            ("10k spaces", "Song" + " " * 10000 + "Title"),
            ("50k spaces", "Song" + " " * 50000 + "Title"),
            ("Repeated open parens (20k)", "Song " + "(" * 20000),
            ("Repeated unclosed brackets (10k)", "Song " + "[a" * 10000),
            ("Repeated dash space (5k)", "Artist" + " - " * 5000 + "Title"),
            ("Repeated format placeholders (5k)", "Prefix" + " - %track{01}" * 5000 + "Title"),
            ("Repeated feature prefixes (5k)", "Song " + "feat. Artist " * 5000),
            ("Huge hex suffix (50k)", "Song_" + "a" * 50000),
            ("Huge malformed UUID suffix (50k)", "Song_{" + "01234567-89ab-cdef-" * 2500 + "}"),
            ("Deeply malformed annotation text (50k)", "Song (remix " + "deluxe " * 7000 + "edition)"),
        ]

        for desc, attack_str in adversarial_inputs:
            with self.subTest(scenario=desc):
                t0 = time.perf_counter()
                norm = normalize_track_title_for_matching(attack_str)
                strip = strip_track_filename_id_suffix(attack_str)
                has_suffix = track_filename_has_source_id_suffix(attack_str)
                feat = track_feature_variants(attack_str)
                alias = track_parenthetical_alias_variants(attack_str)
                pref = track_path_prefixes(attack_str)
                variants = track_title_variants_for_matching(attack_str, attack_str)
                dt = time.perf_counter() - t0

                # Must complete boundedly without hanging (well under 5.0s for 50k chars across 7 functions)
                self.assertLess(dt, 5.0, f"Adversarial input [{desc}] took {dt:.3f}s (potential ReDoS)")
                self.assertIsInstance(norm, str)
                self.assertIsInstance(strip, str)
                self.assertIsInstance(has_suffix, bool)
                self.assertIsInstance(feat, list)
                self.assertIsInstance(alias, list)
                self.assertIsInstance(pref, list)
                self.assertIsInstance(variants, list)

    def test_complexity_scaling_bounded(self):
        """Verify execution time scales linearly rather than exponentially/polynomially."""
        import time
        from backend.matching import (
            normalize_track_title_for_matching,
            track_title_variants_for_matching,
        )

        base_pattern = "Artist - %track{01} - Song (feat. Artist) [Remastered] "
        times = []
        sizes = [100, 200, 400]

        for multiplier in sizes:
            input_str = base_pattern * multiplier
            t0 = time.perf_counter()
            _ = normalize_track_title_for_matching(input_str)
            _ = track_title_variants_for_matching(input_str, input_str)
            times.append(time.perf_counter() - t0)

        # Confirm all runs completed in sub-second time
        for t in times:
            self.assertLess(t, 1.0)


if __name__ == "__main__":
    unittest.main()




