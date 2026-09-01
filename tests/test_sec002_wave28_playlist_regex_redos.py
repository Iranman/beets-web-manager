"""SEC-002 Wave 28: Regression tests for playlist regex backtracking fixes.

Covers:
1. _playlist_split_artist_title deterministic linear-time parser
   - Valid playlist inputs across all standard delimiter formats
   - Leading/trailing and repeated whitespace
   - Dash variants (hyphen-minus, en-dash, em-dash)
   - Compact [A-Za-z0-9][-–—][A-Z0-9] splitting
   - Long whitespace sequences and adversarial repeated separator-like strings
   - Inputs without valid separators
   - Semantic equivalence for legitimate inputs
2. _PLAYLIST_CHANNEL_ARTIST_RE and _playlist_artist_looks_like_channel
   - Channel / topic suffixes (- Topic, - topic, en/em-dashes)
   - Record label and media suffixes (Records, Music, Entertainment, Media, Films, Productions, TV)
   - VEVO and Official branding
   - Adversarial malformed inputs stressing unanchored regex search
   - Semantic equivalence for legitimate inputs
"""

from __future__ import annotations

import time
import unittest

import app as app_module


class PlaylistSplitArtistTitleTests(unittest.TestCase):
    """Focused regression tests for _playlist_split_artist_title."""

    def test_valid_standard_artist_title_splitting(self):
        cases = [
            ("Artist - Title", ("Artist", "Title")),
            ("Artist – Title", ("Artist", "Title")),  # en-dash \u2013
            ("Artist — Title", ("Artist", "Title")),  # em-dash \u2014
            ("Queen - Bohemian Rhapsody", ("Queen", "Bohemian Rhapsody")),
            ("AC/DC - Thunderstruck", ("AC/DC", "Thunderstruck")),
            ("Artist   -   Title", ("Artist", "Title")),
            ("Artist \t - \t Title", ("Artist", "Title")),
            ("  Artist - Title  ", ("Artist", "Title")),
            ("Artist - Title (Official Video)", ("Artist", "Title (Official Video)")),
            ("Artist - Title - Subtitle", ("Artist", "Title - Subtitle")),
            ("01 - Artist - Title", ("Artist", "Title")),
            ("01. Artist - Title", ("Artist", "Title")),
            ("1 - 01 - Artist - Title", ("Artist", "Title")),
            ("Artist - Title | Official Video | 4K", ("Artist", "Title")),
        ]
        for inp, expected in cases:
            with self.subTest(input=inp):
                result = app_module._playlist_split_artist_title(inp)
                self.assertEqual(result, expected)

    def test_compact_artist_title_splitting(self):
        cases = [
            ("Artist-Title", ("Artist", "Title")),
            ("Artist–Title", ("Artist", "Title")),
            ("Artist—Title", ("Artist", "Title")),
            ("Artist-1999Track", ("Artist", "1999Track")),
            ("A1-B2", ("A1", "B2")),
            ("Track-02", ("Track", "02")),
            ("a-B", ("a", "B")),
            ("a-1", ("a", "1")),
        ]
        for inp, expected in cases:
            with self.subTest(input=inp):
                result = app_module._playlist_split_artist_title(inp)
                self.assertEqual(result, expected)

    def test_invalid_or_missing_separator_returns_none(self):
        cases = [
            "",
            "   ",
            "SingleTitleWithoutSeparator",
            "- Title",
            "Artist -",
            "---",
            " - ",
            "   -   ",
            "Artist-title",  # lowercase right side does not match compact dash
            "Just Some Song Name (Original Mix)",
        ]
        for inp in cases:
            with self.subTest(input=inp):
                result = app_module._playlist_split_artist_title(inp)
                self.assertIsNone(result)

    def test_adversarial_long_whitespace_runs_in_linear_time(self):
        # Stresses whitespace handling with large non-matching and matching spans
        long_space_valid = "Artist" + (" " * 50000) + "-" + (" " * 50000) + "Title"
        t0 = time.perf_counter()
        res = app_module._playlist_split_artist_title(long_space_valid)
        elapsed = time.perf_counter() - t0
        self.assertEqual(res, ("Artist", "Title"))
        self.assertLess(elapsed, 0.2, f"Execution took too long: {elapsed:.4f}s")

        long_space_no_sep = " " * 100000
        t0 = time.perf_counter()
        res = app_module._playlist_split_artist_title(long_space_no_sep)
        elapsed = time.perf_counter() - t0
        self.assertIsNone(res)
        self.assertLess(elapsed, 0.2, f"Execution took too long: {elapsed:.4f}s")

    def test_adversarial_repeated_separator_sequences_in_linear_time(self):
        # Stresses separator matching across many separator-like candidates
        repeated_seps = " - " * 20000
        t0 = time.perf_counter()
        res = app_module._playlist_split_artist_title(repeated_seps)
        elapsed = time.perf_counter() - t0
        self.assertIsNotNone(res)
        self.assertEqual(res[0], "-")
        self.assertLess(elapsed, 0.2, f"Execution took too long: {elapsed:.4f}s")

        malformed_dashes = "-" * 50000
        t0 = time.perf_counter()
        res = app_module._playlist_split_artist_title(malformed_dashes)
        elapsed = time.perf_counter() - t0
        self.assertIsNone(res)
        self.assertLess(elapsed, 0.2, f"Execution took too long: {elapsed:.4f}s")

        no_left_seps = "   -   " * 10000
        t0 = time.perf_counter()
        res = app_module._playlist_split_artist_title(no_left_seps)
        elapsed = time.perf_counter() - t0
        self.assertIsNotNone(res)
        self.assertEqual(res[0], "-")
        self.assertLess(elapsed, 0.2, f"Execution took too long: {elapsed:.4f}s")

    def test_semantic_equivalence_on_representative_dataset(self):
        # Suite of diverse inputs verifying expected artist/title splitting
        test_data = [
            ("The Beatles - Hey Jude", ("The Beatles", "Hey Jude")),
            ("Pink Floyd - Comfortably Numb", ("Pink Floyd", "Comfortably Numb")),
            ("Led Zeppelin - Stairway to Heaven", ("Led Zeppelin", "Stairway to Heaven")),
            ("Radiohead - Paranoid Android", ("Radiohead", "Paranoid Android")),
            ("Nirvana - Smells Like Teen Spirit", ("Nirvana", "Smells Like Teen Spirit")),
            ("David Bowie - Heroes", ("David Bowie", "Heroes")),
            ("Miles Davis - So What", ("Miles Davis", "So What")),
            ("John Coltrane - Giant Steps", ("John Coltrane", "Giant Steps")),
            ("Jay-Z - Empire State of Mind", ("Jay", "Z - Empire State of Mind")),
            ("Blink-182 - All The Small Things", ("Blink", "182 - All The Small Things")),
            ("2Pac - Changes", ("2Pac", "Changes")),
            ("Fifty Cent - In Da Club", ("Fifty Cent", "In Da Club")),
            ("T-Pain - Buy U A Drank", ("T", "Pain - Buy U A Drank")),
            ("A-Ha - Take On Me", ("A", "Ha - Take On Me")),
        ]
        for inp, expected in test_data:
            with self.subTest(input=inp):
                self.assertEqual(app_module._playlist_split_artist_title(inp), expected)


class PlaylistChannelArtistTests(unittest.TestCase):
    """Focused regression tests for _PLAYLIST_CHANNEL_ARTIST_RE and channel recognition."""

    def test_valid_channel_artist_formats(self):
        channel_cases = [
            "Kendrick Lamar - Topic",
            "Kendrick Lamar – Topic",
            "Kendrick Lamar — Topic",
            "Daft Punk - topic",
            "Artist   -   Topic",
            "Artist-Topic",
            "Taylor Swift VEVO",
            "Taylor Swift - VEVO",
            "Artist Official",
            "Official Artist Channel",
            "Sony Music",
            "Atlantic Records",
            "Universal Entertainment",
            "Universal Media",
            "Warner TV",
            "Paramount Films",
            "Production Company Productions",
        ]
        for name in channel_cases:
            with self.subTest(channel=name):
                self.assertTrue(
                    app_module._playlist_artist_looks_like_channel(name),
                    f"Expected {name!r} to be recognized as channel",
                )

    def test_non_channel_artist_names(self):
        non_channel_cases = [
            "Topic",  # Artist named Topic without dash
            "Topical Island",
            "Just An Artist",
            "Music Man",  # 'music' is not at end
            "The Records Band",  # 'records' not at end
            "Mediaeval Baebes",
            "",
            "   ",
        ]
        for name in non_channel_cases:
            with self.subTest(artist=name):
                self.assertFalse(
                    app_module._playlist_artist_looks_like_channel(name),
                    f"Expected {name!r} NOT to be recognized as channel",
                )

    def test_adversarial_topic_separator_runs_in_linear_time(self):
        # Stresses regex backtracking on separator-like prefix before topic
        adversarial_input = (" - " * 20000) + "topi"  # Almost matches topic, fails at end
        t0 = time.perf_counter()
        res = app_module._playlist_artist_looks_like_channel(adversarial_input)
        elapsed = time.perf_counter() - t0
        self.assertFalse(res)
        self.assertLess(elapsed, 0.2, f"Execution took too long: {elapsed:.4f}s")

        adversarial_topic = (" - " * 20000) + "topic"
        t0 = time.perf_counter()
        res = app_module._playlist_artist_looks_like_channel(adversarial_topic)
        elapsed = time.perf_counter() - t0
        self.assertTrue(res)
        self.assertLess(elapsed, 0.2, f"Execution took too long: {elapsed:.4f}s")

    def test_adversarial_long_whitespace_runs_in_linear_time(self):
        adversarial_whitespace = (" " * 50000) + "Artist - Topic" + (" " * 50000)
        t0 = time.perf_counter()
        res = app_module._playlist_artist_looks_like_channel(adversarial_whitespace)
        elapsed = time.perf_counter() - t0
        self.assertTrue(res)
        self.assertLess(elapsed, 0.2, f"Execution took too long: {elapsed:.4f}s")


if __name__ == "__main__":
    unittest.main()
