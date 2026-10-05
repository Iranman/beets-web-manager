"""SEC-5 regression tests: polynomial ReDoS in title/artist normalizers
(CodeQL #1291, #1292, #1294, #1295, #1298, #1299, #1301).

The measured pre-fix cost on a 20k-character adversarial string (a long
whitespace run, the shape a hostile tag or filename can carry) was 3-12 s
per call and grew quadratically. The checks below use a 20k input and a
deliberately generous 1 s ceiling: the fixed code takes milliseconds, the
old code takes many seconds, so the margin is two orders of magnitude and
the test cannot flake on a slow CI runner. A second check compares 5k vs
20k timings to catch quadratic growth even if a future runner is very fast.
"""
import re
import time
import unittest

import app as app_module  # noqa: F401  (boots the app family)
from backend import acoustid_service, ai_evidence_service, playlist_service, slskd_service

_N = 20_000
_CEILING_SECONDS = 1.0


def _elapsed(fn, arg):
    start = time.perf_counter()
    fn(arg)
    return time.perf_counter() - start


class _ReDoSCase(unittest.TestCase):
    def assert_linear_and_fast(self, fn, make):
        big = _elapsed(fn, make(_N))
        self.assertLess(big, _CEILING_SECONDS, f"{fn.__name__} took {big:.2f}s on {_N} chars")
        small = min(_elapsed(fn, make(_N // 4)) for _ in range(3))
        # Linear growth is ~4x; quadratic is ~16x. Allow 10x plus a fixed
        # allowance for timer noise on sub-millisecond runs.
        self.assertLess(big, small * 10 + 0.05, f"{fn.__name__} grows super-linearly")


class AcoustidNormalizerTests(_ReDoSCase):
    def test_1291_feat_suffix_regex(self):
        self.assert_linear_and_fast(acoustid_service._normalize_albumartist, lambda n: " " * n + "x")
        self.assert_linear_and_fast(lambda s: acoustid_service._FEAT_RE.sub("", s), lambda n: " " * n + "x")

    def test_1292_artist_variant_split(self):
        self.assert_linear_and_fast(acoustid_service._playlist_artist_name_variants, lambda n: "a" + " " * n + "q")

    def test_feat_behaviour_unchanged(self):
        f = acoustid_service._normalize_albumartist
        self.assertEqual(f("Wiz Khalifa feat. Snoop Dogg"), "Wiz Khalifa")
        self.assertEqual(f("Artist (feat. Other)"), "Artist")
        self.assertEqual(f("Artist [ft. Other]"), "Artist")
        self.assertEqual(f("Earth, Wind & Fire"), "Earth, Wind & Fire")
        self.assertEqual(f("Wiz Khalifa, Juicy J"), "Wiz Khalifa")


class AiEvidenceTests(_ReDoSCase):
    def test_1294_in_mono_suffix(self):
        self.assert_linear_and_fast(ai_evidence_service._ai_evidence_clean_artist_guess, lambda n: "a" + " " * n + "x")
        self.assertEqual(ai_evidence_service._ai_evidence_clean_artist_guess("The Beatles in Mono"), "The Beatles")

    def test_1295_scene_split(self):
        self.assert_linear_and_fast(ai_evidence_service._ai_evidence_scene_guess, lambda n: "a" + " " * n + "b")


class PlaylistTitleTests(_ReDoSCase):
    def test_1298_trailing_bracket(self):
        self.assert_linear_and_fast(playlist_service._playlist_clean_variant_title, lambda n: "a" + " " * n + "(a)x")

    def test_1299_dash_suffix(self):
        self.assert_linear_and_fast(playlist_service._playlist_clean_variant_title, lambda n: "a" + " " * n + "-")
        self.assert_linear_and_fast(playlist_service._playlist_clean_variant_title, lambda n: "a - " * (n // 4) + "\nx")

    def test_behaviour_unchanged(self):
        f = playlist_service._playlist_clean_variant_title
        self.assertEqual(f("Song Title (Explicit)"), ("Song Title", True))
        self.assertEqual(f("Song - Radio Edit"), ("Song", True))
        self.assertEqual(f("Song Title (Live)"), ("Song Title (Live)", False))


class SlskdTitleTests(_ReDoSCase):
    def test_1301_bracket_feature_strip(self):
        self.assert_linear_and_fast(slskd_service._slskd_title_norm, lambda n: "(feat" * (n // 5))
        self.assert_linear_and_fast(slskd_service._slskd_title_norm, lambda n: "a" + " " * n + "(x")
        self.assertEqual(slskd_service._slskd_title_norm("Song (feat. Someone) [Remix]"), "song")

    def test_1302_scene_split(self):
        self.assert_linear_and_fast(slskd_service._slskd_title_guess_from_name, lambda n: "a" + " " * n + "x.flac")


class RewrittenPatternEquivalenceTests(unittest.TestCase):
    """The leading-whitespace rewrite must not change what the patterns match."""

    PAIRS = [
        (r"\s*[\(\[]?(?:feat(?:uring)?\.?|ft\.?|with)\b.*",
         r"(?:(?<!\s)\s+)?[\(\[]?(?:feat(?:uring)?\.?|ft\.?|with)\b.*", re.I),
        (r"\s*(?:/|,|\+|\b(?:ft\.?|feat\.?|featuring|with|x|and)\b|&)\s+",
         r"(?:(?<!\s)\s+)?(?:/|,|\+|\b(?:ft\.?|feat\.?|featuring|with|x|and)\b|&)\s+", re.I),
        (r"\s+\bin\s+mono\b$", r"(?<!\s)\s+\bin\s+mono\b$", re.I),
        (r"\s*-\s*", r"(?:(?<!\s)\s+)?-\s*", 0),
        (r"\s*[\(\[]([^()\[\]]+)[\)\]]\s*$", r"(?:(?<!\s)\s+)?[\(\[]([^()\[\]]+)[\)\]]\s*$", 0),
        (r"\s+[-–—]\s+(.+)$", r"(?<!\s)\s+[-–—]\s+(.+)$", 0),
        (r"\s+-\s+", r"(?<!\s)\s+-\s+", 0),
    ]
    SAMPLES = ["", " ", "a  feat b", "A (feat. B)", "x - y - z", "  -  ", "Title  (Live)  ", "a / b, c & d with e",
               "Band in Mono", "a\t-\tb", "(", "a - b\nc", "  ft. x", "[with y]", "x and y + z"]

    def test_equivalent_on_samples(self):
        for old, new, flags in self.PAIRS:
            o, n = re.compile(old, flags), re.compile(new, flags)
            for s in self.SAMPLES:
                with self.subTest(pattern=old, sample=s):
                    self.assertEqual(o.sub("|", s), n.sub("|", s))
                    self.assertEqual(o.split(s), n.split(s))


if __name__ == "__main__":
    unittest.main()
