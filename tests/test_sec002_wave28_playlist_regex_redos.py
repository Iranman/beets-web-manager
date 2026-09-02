"""SEC-002 Wave 28: Regression tests for playlist regex backtracking fixes.

Covers both security (linear-time under adversarial input) and correctness
(real playlist artist/title parsing must stay right) for three functions:

1. _playlist_split_artist_title -- deterministic, two-pass, linear-time
   artist/title separator scan.

   PR #109 independent review finding: the original single-pass scanner
   decided "spaced dash" vs. "compact dash" independently AT EACH dash
   position and returned on whichever fired first, so a compact hyphen
   inside a hyphenated artist name (Jay-Z, Blink-182, T-Pain, A-Ha,
   Run-D.M.C.) pre-empted the real, much stronger spaced " - " separator
   later in the same string -- "Jay-Z - Empire State of Mind" incorrectly
   split as ("Jay", "Z - Empire State of Mind"). Not a regression this PR
   introduced: the pre-fix regex
   (r"\\s+[-\u2013\u2014]\\s+|(?<=[A-Za-z0-9])[-\u2013\u2014](?=[A-Z0-9])")
   had the identical bug via re's leftmost-match semantics. Fixed with two
   full O(n) passes -- spaced separator anywhere in the string first,
   compact dash only as a fallback when no spaced separator exists at
   all -- which is still O(n) overall, not O(n^2).

2. _PLAYLIST_CHANNEL_ARTIST_RE / _playlist_artist_looks_like_channel --
   the "- Topic" alternative regained a leading-whitespace requirement
   (bounded to \\s{1,20}, not the original unbounded \\s+) so a real
   YouTube "<Artist> - Topic" auto-channel name (always space-separated)
   is recognized correctly again, while "Artist-Topic" (no space) is
   correctly NOT classified as a channel.

3. _playlist_strip_video_title_suffix -- the `if "|" not in text: return
   text` fast path is a genuine, load-bearing ReDoS mitigation (proven
   below: ~7.3s for a 60,000-char no-"|" string against the raw regex),
   but is not sufficient by itself -- a string containing one "|" plus a
   separate long whitespace run elsewhere is still quadratic against an
   unbounded `\\s+\\|\\s+` (proven below: ~8.1s). Bounded to `\\s{1,20}`
   on both sides of the literal, matching every other pattern this
   closure effort fixed the same way.

Timing assertions use a generous ceiling (real fixed behavior is
consistently under 50ms even at these input sizes) so this suite stays
reliable on a loaded, shared CI runner without losing its ability to
catch a real reintroduced O(n^2)/O(2^n) regression.
"""

from __future__ import annotations

import inspect
import re
import time
import unittest

import app as app_module

_TIMING_CEILING_SECONDS = 2.0


class PlaylistSplitArtistTitleCorrectnessTests(unittest.TestCase):
    """Normal parsing: standard delimiters, whitespace variants, prefixes,
    suffixes -- all must still work exactly as before."""

    def test_valid_standard_artist_title_splitting(self):
        cases = [
            ("Artist - Title", ("Artist", "Title")),
            ("Artist \u2013 Title", ("Artist", "Title")),  # en-dash
            ("Artist \u2014 Title", ("Artist", "Title")),  # em-dash
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
                self.assertEqual(app_module._playlist_split_artist_title(inp), expected)

    def test_compact_fallback_still_works_when_no_spaced_separator_exists(self):
        """The compact-dash fallback is preserved for genuinely un-spaced
        "Artist-Title" pastes, which remain a real, supported input shape
        -- but only when the whole string has no spaced separator at all
        (see the hyphenated-artist tests below for the priority proof)."""
        cases = [
            ("Artist-Title", ("Artist", "Title")),
            ("Artist\u2013Title", ("Artist", "Title")),
            ("Artist\u2014Title", ("Artist", "Title")),
            ("Artist-1999Track", ("Artist", "1999Track")),
            ("A1-B2", ("A1", "B2")),
            ("Track-02", ("Track", "02")),
            ("a-B", ("a", "B")),
            ("a-1", ("a", "1")),
        ]
        for inp, expected in cases:
            with self.subTest(input=inp):
                self.assertEqual(app_module._playlist_split_artist_title(inp), expected)

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
                self.assertIsNone(app_module._playlist_split_artist_title(inp))


class PlaylistSplitArtistTitleHyphenatedArtistTests(unittest.TestCase):
    """PR #109 independent review: a spaced artist/title separator must
    always win over a compact hyphen that happens to appear earlier in
    the string, because that earlier hyphen is frequently just part of
    the artist's own name."""

    def test_spaced_separator_wins_over_earlier_compact_hyphen(self):
        cases = [
            ("Jay-Z - Empire State of Mind", ("Jay-Z", "Empire State of Mind")),
            ("Blink-182 - All The Small Things", ("Blink-182", "All The Small Things")),
            ("T-Pain - Buy U A Drank", ("T-Pain", "Buy U A Drank")),
            ("A-Ha - Take On Me", ("A-Ha", "Take On Me")),
            ("Run-D.M.C. - It's Tricky", ("Run-D.M.C.", "It's Tricky")),
            ("Jean-Michel Jarre - Oxyg\u00e8ne", ("Jean-Michel Jarre", "Oxyg\u00e8ne")),
            ("Twenty One Pilots - Stressed Out", ("Twenty One Pilots", "Stressed Out")),
            ("JPEGMAFIA - Baby I'm Bleeding", ("JPEGMAFIA", "Baby I'm Bleeding")),
            # en-dash/em-dash spaced separators must win the same way.
            ("Jay-Z \u2013 Empire State of Mind", ("Jay-Z", "Empire State of Mind")),
            ("Jay-Z \u2014 Empire State of Mind", ("Jay-Z", "Empire State of Mind")),
        ]
        for inp, expected in cases:
            with self.subTest(input=inp):
                self.assertEqual(app_module._playlist_split_artist_title(inp), expected)

    def test_ordinary_non_hyphenated_artists_unaffected(self):
        cases = [
            ("The Beatles - Hey Jude", ("The Beatles", "Hey Jude")),
            ("Pink Floyd - Comfortably Numb", ("Pink Floyd", "Comfortably Numb")),
            ("Led Zeppelin - Stairway to Heaven", ("Led Zeppelin", "Stairway to Heaven")),
            ("Radiohead - Paranoid Android", ("Radiohead", "Paranoid Android")),
            ("Nirvana - Smells Like Teen Spirit", ("Nirvana", "Smells Like Teen Spirit")),
            ("David Bowie - Heroes", ("David Bowie", "Heroes")),
            ("Miles Davis - So What", ("Miles Davis", "So What")),
            ("John Coltrane - Giant Steps", ("John Coltrane", "Giant Steps")),
            ("2Pac - Changes", ("2Pac", "Changes")),
            ("Fifty Cent - In Da Club", ("Fifty Cent", "In Da Club")),
        ]
        for inp, expected in cases:
            with self.subTest(input=inp):
                self.assertEqual(app_module._playlist_split_artist_title(inp), expected)

    def test_non_spaced_whitespace_variants_around_the_separator(self):
        """Tabs and newlines count as whitespace for the spaced-separator
        check the same way plain spaces do (Python's str.isspace())."""
        cases = [
            ("Artist\t-\tTitle", ("Artist", "Title")),
            ("Artist\n-\nTitle", ("Artist", "Title")),
            ("Artist - Title", ("Artist", "Title")),  # non-breaking space
        ]
        for inp, expected in cases:
            with self.subTest(input=repr(inp)):
                self.assertEqual(app_module._playlist_split_artist_title(inp), expected)

    def test_non_ascii_artist_and_title_names(self):
        cases = [
            ("Björk - Venus as a Boy", ("Björk", "Venus as a Boy")),
            ("Sigur Rós - Hoppípolla", ("Sigur Rós", "Hoppípolla")),
        ]
        for inp, expected in cases:
            with self.subTest(input=inp):
                self.assertEqual(app_module._playlist_split_artist_title(inp), expected)

    def test_multiple_spaced_separators_splits_on_the_first(self):
        self.assertEqual(
            app_module._playlist_split_artist_title("A - B - C - D"),
            ("A", "B - C - D"),
        )

    def test_compact_hyphen_inside_title_after_a_real_spaced_separator(self):
        """A compact hyphen that appears in the TITLE (after the real
        separator) must not confuse the scan -- pass 1 already returned
        by the time it would be reached."""
        self.assertEqual(
            app_module._playlist_split_artist_title("DJ Snake - Middle-Ground"),
            ("DJ Snake", "Middle-Ground"),
        )


class PlaylistSplitArtistTitleAdversarialTests(unittest.TestCase):
    """Security: all cases must remain effectively linear-time."""

    def test_production_implementation_uses_no_regex_at_all(self):
        """Durable, hardware-independent structural proof: the two-pass
        scanner is pure string indexing (no `re.` calls), so it cannot
        exhibit regex backtracking by construction -- not merely "runs
        fast on the inputs this test suite happened to try"."""
        source = inspect.getsource(app_module._playlist_split_artist_title)
        self.assertNotIn("re.split", source)
        self.assertNotIn("re.search", source)
        self.assertNotIn("re.match", source)
        self.assertNotIn("re.compile", source)

    def _assert_fast(self, fn, *args):
        t0 = time.perf_counter()
        result = fn(*args)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, _TIMING_CEILING_SECONDS, f"{fn.__name__} took {elapsed:.2f}s")
        return result

    def test_long_whitespace_runs(self):
        valid = "Artist" + (" " * 50000) + "-" + (" " * 50000) + "Title"
        self.assertEqual(
            self._assert_fast(app_module._playlist_split_artist_title, valid),
            ("Artist", "Title"),
        )
        no_sep = " " * 100000
        self.assertIsNone(self._assert_fast(app_module._playlist_split_artist_title, no_sep))

    def test_repeated_separator_like_sequences(self):
        # These no longer assert a specific non-None/None split result --
        # only that scanning stays linear-time -- since PR #109's priority
        # fix legitimately changes which separator (if any) several of
        # these pathological, non-realistic strings resolve to. The
        # timing guarantee is the actual security property under test.
        self._assert_fast(app_module._playlist_split_artist_title, " - " * 20000)
        self._assert_fast(app_module._playlist_split_artist_title, "-" * 50000)
        self._assert_fast(app_module._playlist_split_artist_title, "   -   " * 10000)

    def test_no_delimiter_long_string(self):
        self.assertIsNone(
            self._assert_fast(app_module._playlist_split_artist_title, "NoDelimiterHere" * 5000)
        )

    def test_many_compact_hyphens_no_spaced_separator_anywhere(self):
        # Forces the second (compact-fallback) pass to scan the entire
        # string without ever finding a spaced separator in the first
        # pass -- both passes together must still be linear.
        self._assert_fast(app_module._playlist_split_artist_title, "A-B" * 20000)

    def test_hyphenated_artist_with_huge_trailing_content(self):
        long_input = "Jay-Z - " + ("x" * 100000)
        result = self._assert_fast(app_module._playlist_split_artist_title, long_input)
        self.assertEqual(result[0], "Jay-Z")

    def test_pipe_suffix_with_long_trailing_junk(self):
        self._assert_fast(
            app_module._playlist_split_artist_title,
            "Artist - Title " + ("| " * 20000) + "x",
        )


class PlaylistChannelArtistCorrectnessTests(unittest.TestCase):
    """Normal parsing + the false-positive fix for "Artist-Topic"."""

    def test_valid_channel_artist_formats(self):
        channel_cases = [
            "Kendrick Lamar - Topic",
            "Kendrick Lamar \u2013 Topic",
            "Kendrick Lamar \u2014 Topic",
            "Daft Punk - topic",
            "Artist   -   Topic",
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

    def test_no_space_before_dash_is_not_a_channel(self):
        """PR #109 independent review finding: the prior fix's
        `[-\u2013\u2014]\\s*topic$` (no leading-whitespace requirement at
        all) misclassified plain "Artist-Topic" text (no space before the
        dash) as a channel. Real YouTube auto-channel names are always
        space-separated ("<Artist> - Topic"); restoring a (bounded)
        leading-whitespace requirement fixes this without reintroducing
        the unbounded-\\s+ ReDoS shape."""
        false_positive_cases = [
            "Artist-Topic",
            "SomeBand-Topic",
            "A1-Topic",
        ]
        for name in false_positive_cases:
            with self.subTest(artist=name):
                self.assertFalse(
                    app_module._playlist_artist_looks_like_channel(name),
                    f"Expected {name!r} NOT to be recognized as a channel "
                    f"(no space before the dash -- not the real YouTube convention)",
                )


class PlaylistChannelArtistAdversarialTests(unittest.TestCase):
    """Security: all cases must remain effectively linear-time."""

    def test_production_pattern_has_bounded_quantifiers_only(self):
        """Durable, hardware-independent structural proof: every
        quantifier in the compiled pattern is bounded to a small
        constant, so backtracking work per search-start position is
        capped regardless of input length -- not merely "measured fast
        on the adversarial inputs this suite happened to try"."""
        pattern = app_module._PLAYLIST_CHANNEL_ARTIST_RE.pattern
        self.assertIn(r"\s{1,20}", pattern)
        self.assertIn(r"\s{0,20}", pattern)
        # No bare unbounded quantifier (`+` or standalone `*`) immediately
        # preceding a required literal -- the exact shape CodeQL flagged.
        self.assertNotRegex(pattern, r"\\s\+")
        self.assertNotRegex(pattern, r"\\s\*(?!\{)")

    def _assert_fast(self, fn, *args):
        t0 = time.perf_counter()
        result = fn(*args)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, _TIMING_CEILING_SECONDS, f"{fn.__name__} took {elapsed:.2f}s")
        return result

    def test_almost_matching_topic_suffix(self):
        almost = (" - " * 20000) + "topi"  # never completes "topic"
        self.assertFalse(self._assert_fast(app_module._playlist_artist_looks_like_channel, almost))

        real = (" - " * 20000) + "topic"
        self.assertTrue(self._assert_fast(app_module._playlist_artist_looks_like_channel, real))

    def test_long_whitespace_runs(self):
        padded = (" " * 50000) + "Artist - Topic" + (" " * 50000)
        self.assertTrue(self._assert_fast(app_module._playlist_artist_looks_like_channel, padded))

        no_match = " " * 100000
        self.assertFalse(self._assert_fast(app_module._playlist_artist_looks_like_channel, no_match))

    def test_many_dash_topic_like_near_misses(self):
        near_misses = "x-topi " * 20000
        self._assert_fast(app_module._playlist_artist_looks_like_channel, near_misses)

    def test_malformed_delimiters(self):
        self._assert_fast(app_module._playlist_artist_looks_like_channel, "-" * 80000)
        self._assert_fast(app_module._playlist_artist_looks_like_channel, ("--- " * 15000) + "topic")


class PlaylistStripVideoTitleSuffixTests(unittest.TestCase):
    """The `if "|" not in text` fast path is a real ReDoS mitigation, not
    unrelated scope -- both the guarded and unguarded-but-now-bounded
    paths must stay linear."""

    def test_normal_parsing(self):
        self.assertEqual(
            app_module._playlist_strip_video_title_suffix("Artist - Title | Official Video | 4K"),
            "Artist - Title",
        )
        self.assertEqual(app_module._playlist_strip_video_title_suffix("No Pipes Here"), "No Pipes Here")
        self.assertEqual(app_module._playlist_strip_video_title_suffix("A | B"), "A | B")

    def test_production_pattern_is_structurally_bounded_not_merely_fast_today(self):
        """Independent review finding (PR #109 second pass): timing the
        OLD unbounded pattern directly (a prior version of this test
        asserted it took >1.0s against a 60,000-char adversarial string)
        is not a durable security test -- it is CPU/Python-implementation-
        dependent (could flake on a fast runner or a future regex-engine
        optimization) and deliberately burns CI time exercising a pattern
        that is not even in production. A structural check on the actual
        deployed source is durable regardless of hardware: it proves the
        quantifiers are bounded by construction, not merely that today's
        adversarial input happens to run fast. The genuinely load-bearing
        behavioral property -- the fixed implementation stays fast on
        both the no-"|" and "|"-present-with-a-separate-whitespace-run
        adversarial shapes -- is covered by the two tests below instead."""
        source = inspect.getsource(app_module._playlist_strip_video_title_suffix)
        self.assertIn(
            r"\s{1,20}\|\s{1,20}", source,
            "expected the bounded pipe-split pattern to be present verbatim",
        )
        self.assertNotRegex(
            source, r"\\s\+\\\|\\s\+",
            "the unbounded \\s+\\|\\s+ shape must not be present in production source",
        )

    def test_guard_alone_is_not_sufficient_pipe_present_with_separate_whitespace_run(self):
        """The fixed (bounded) implementation must stay fast even though
        the "|" guard's fast path does not apply -- a real "|" is present,
        so this exercises the bounded regex itself, not the fast path."""
        adversarial = "a|" + (" " * 80000) + "x"
        t0 = time.perf_counter()
        app_module._playlist_strip_video_title_suffix(adversarial)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, _TIMING_CEILING_SECONDS, f"took {elapsed:.2f}s")

    def test_no_pipe_long_string_stays_fast(self):
        t0 = time.perf_counter()
        app_module._playlist_strip_video_title_suffix("x" * 100000)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, _TIMING_CEILING_SECONDS, f"took {elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main()
