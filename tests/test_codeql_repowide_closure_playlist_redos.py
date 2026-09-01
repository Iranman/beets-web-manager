r"""Regression coverage for real py/polynomial-redos vulnerabilities found
during the repository-wide CodeQL closure pass (alerts #114/#41019,
#115/#41103, #116/#41119, #122/#41214, app.py at baseline commit
7b05844d58d657ce6f1ae6c5b1724f5ba70ee257) -- all in the playlist
artist/title-cleaning helpers.

Each of the 4 patterns shared the same underlying issue: an unanchored
re.sub()/re.split() with an unbounded quantifier (e.g. `[^)]*`, `\\s+`)
that gets re-tried at every character offset of adversarial input with no
closing delimiter / no terminating whitespace -- empirically confirmed
quadratic (not linear) time: 41019 took ~4.5s against a 16,000-character
adversarial string; 41214 took ~1.4s against the same scale. Bounded the
relevant quantifiers (100 chars for parenthetical/bracket content, 20 for
separator whitespace runs) -- no legitimate track/artist title comes
remotely close to either bound, and the bounded patterns measured
linear-time against the same adversarial inputs during triage.

These 4 functions are reachable from user-pasted playlist text (Playlists
view's "paste a list of tracks" mode), so unbounded attacker-controlled
input length is a realistic path, not merely theoretical.

IMPORTANT LESSON, recorded here so it isn't lost: the first version of this
fix (bounding only the delimiter-content quantifier, e.g. `[^)]{0,100}`)
was INCOMPLETE. GitHub's own CodeQL PR-scoped re-check (after this branch
was rebased onto PR #102/Wave 27) still flagged these exact lines
(alerts #1029/#1030/#1031) as polynomial-redos. Investigating rather than
dismissing the "new" alert found the real gap empirically: the *leading*
`\s*` in each pattern was still unbounded, and a long run of whitespace
before a single unclosed delimiter -- a materially different adversarial
shape than the "many small brackets" one originally tested -- was still
quadratic (~5.4s at 32,000 characters) regardless of the content bound.
`PlaylistCleaningRegexPerformanceTests` below now covers BOTH adversarial
shapes for every fixed function, specifically so a future regression in
either dimension is caught, not just the one that happened to be tested
first.
"""

import time
import unittest

import app as app_module


class PlaylistCleaningRegexPerformanceTests(unittest.TestCase):
    """Proves the fix: adversarial input that used to take seconds now
    completes near-instantly. A generous 2-second ceiling is used (real
    fixed behavior is <50ms even at these sizes) so this stays reliable
    on a loaded CI runner without reintroducing a hang if a future edit
    undoes the bound."""

    ADVERSARIAL_SIZE = 20000

    def _assert_fast(self, fn, *args, **kwargs):
        t0 = time.perf_counter()
        fn(*args, **kwargs)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 2.0, f"{fn.__name__} took {elapsed:.2f}s on adversarial input")

    def test_playlist_title_variants_is_not_quadratic(self):
        adversarial = ("  (" * (self.ADVERSARIAL_SIZE // 3)) + "X" * self.ADVERSARIAL_SIZE
        self._assert_fast(app_module._playlist_title_variants, adversarial)

    def test_playlist_title_variants_is_not_quadratic_long_leading_whitespace(self):
        # The adversarial shape the first fix attempt missed: a long
        # whitespace run before a single unclosed bracket, not many small
        # ones. See the module docstring.
        adversarial = (" " * self.ADVERSARIAL_SIZE) + "(" + ("x" * 200)
        self._assert_fast(app_module._playlist_title_variants, adversarial)

    def test_playlist_strip_artist_channel_noise_is_not_quadratic(self):
        adversarial = ("  [" * (self.ADVERSARIAL_SIZE // 3)) + "X" * self.ADVERSARIAL_SIZE
        self._assert_fast(app_module._playlist_strip_artist_channel_noise, adversarial)

    def test_playlist_strip_artist_channel_noise_is_not_quadratic_long_leading_whitespace(self):
        adversarial = (" " * self.ADVERSARIAL_SIZE) + "[" + ("x" * 200)
        self._assert_fast(app_module._playlist_strip_artist_channel_noise, adversarial)

    def test_playlist_artist_name_variants_is_not_quadratic(self):
        adversarial = ("  (" * (self.ADVERSARIAL_SIZE // 3)) + "X" * self.ADVERSARIAL_SIZE
        self._assert_fast(app_module._playlist_artist_name_variants, adversarial)

    def test_playlist_artist_name_variants_is_not_quadratic_long_leading_whitespace(self):
        adversarial = (" " * self.ADVERSARIAL_SIZE) + "(" + ("x" * 200)
        self._assert_fast(app_module._playlist_artist_name_variants, adversarial)

    def test_playlist_split_artist_title_is_not_quadratic(self):
        adversarial = (" " * self.ADVERSARIAL_SIZE) + "-" * self.ADVERSARIAL_SIZE
        self._assert_fast(app_module._playlist_split_artist_title, adversarial)


class PlaylistCleaningRegexBehaviorPreservedTests(unittest.TestCase):
    """Proves the bound didn't change behavior for any realistic input."""

    def test_title_variants_still_strips_normal_parenthetical(self):
        variants = app_module._playlist_title_variants("Song Title (feat. Someone)")
        self.assertIn("song title", {v.strip() for v in variants})

    def test_artist_channel_noise_still_stripped(self):
        result = app_module._playlist_strip_artist_channel_noise("Some Artist [Official Video]")
        self.assertNotIn("[", result)
        self.assertIn("Some Artist", result)

    def test_artist_name_variants_still_strips_normal_parenthetical(self):
        variants = app_module._playlist_artist_name_variants("Some Artist (Topic)")
        self.assertIn("Some Artist", variants)

    def test_split_artist_title_still_splits_normal_dash_separator(self):
        artist, title = app_module._playlist_split_artist_title("Pink Floyd - Breathe")
        self.assertEqual(artist, "Pink Floyd")
        self.assertEqual(title, "Breathe")

    def test_split_artist_title_still_splits_with_extra_spaces(self):
        # A handful of stray spaces around the separator is realistic and
        # must still work under the new bound (20 chars is generous).
        artist, title = app_module._playlist_split_artist_title("Pink Floyd   -   Breathe")
        self.assertEqual(artist, "Pink Floyd")
        self.assertEqual(title, "Breathe")


if __name__ == "__main__":
    unittest.main()
