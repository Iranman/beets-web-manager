"""SEC-5 regression tests: polynomial ReDoS in title/artist normalizers
(CodeQL py/polynomial-redos #1357-#1363).

Every flagged pattern had the shape ``\\s*CORE`` / ``\\s+CORE``: a leading
whitespace quantifier the engine re-scans from each start position, so a long
whitespace run costs O(n^2). The fixes in ``backend/title_normalize.py`` search
only CORE (or scan by hand) and extend matches back over whitespace in one
pass. Two kinds of checks guard that:

* timing: the helpers are called directly on uncapped 100k-character
  adversarial inputs (no 1024-char cap in front of them) and must finish
  well under 1 s, plus a growth check against a quarter-size input;
* equivalence: each helper is compared with the original (pre-SEC-5, deb4ec3)
  regex as an oracle, on a realistic corpus and on seeded random strings drawn
  from an alphabet that includes Unicode whitespace, newlines and the
  case-folding characters (dotless i, dotted I, long s) that ``re.I`` treats
  specially.
"""
import random
import re
import time
import unittest

import app as app_module  # noqa: F401  (boots the app family)
from backend import acoustid_service, ai_evidence_service, playlist_service, slskd_service
from backend.title_normalize import (
    dash_suffix_group,
    split_ws_led,
    strip_bracket_credits,
    strip_in_mono_suffix,
    trailing_bracket_group,
)

_N = 100_000
_CEILING_SECONDS = 1.0


def _elapsed(fn, arg):
    start = time.perf_counter()
    fn(arg)
    return time.perf_counter() - start


class _ReDoSCase(unittest.TestCase):
    def assert_linear_and_fast(self, fn, make):
        big = _elapsed(fn, make(_N))
        self.assertLess(big, _CEILING_SECONDS, f"{getattr(fn, '__name__', fn)} took {big:.2f}s on {_N} chars")
        small = min(_elapsed(fn, make(_N // 4)) for _ in range(3))
        # Linear growth is ~4x; quadratic is ~16x. Allow 10x plus a fixed
        # allowance for timer noise on sub-millisecond runs.
        self.assertLess(big, small * 10 + 0.05, f"{getattr(fn, '__name__', fn)} grows super-linearly")


# ── oracles: the original deb4ec3 patterns ──────────────────────────────────
_O_FEAT = re.compile(r"\s*[\(\[]?(?:feat(?:uring)?\.?|ft\.?|with)\b.*", re.I)
_O_SPLIT = re.compile(r"\s*(?:/|,|\+|\b(?:ft\.?|feat\.?|featuring|with|x|and)\b|&)\s+", re.I)
_O_MONO = re.compile(r"\s+\bin\s+mono\b$", re.I)
_O_SCENE = re.compile(r"\s*-\s*")
_O_BRACKET = re.compile(r"\s*[\(\[]([^()\[\]]+)[\)\]]\s*$")
_O_DASH = re.compile(r"\s+[-–—]\s+(.+)$")
_O_SLSKD_SPLIT = re.compile(r"\s+-\s+")
_O_SLSKD_FEAT_TAIL = re.compile(r"\b(?:feat|ft)\.?\s+.*$", re.I)  # CodeQL #1369
_O_SLSKD_BRACKET = re.compile(
    r"\s*[\(\[]\s*(?:feat\.?|ft\.?|with|prod\.?|produced\s+by|remix|edit|version|bonus|clean|explicit).*?[\)\]]\s*",
    re.I,
)


# #186: playlist_service._playlist_primary_artist_name before the rewrite.
_O_PL_SLASH = re.compile(r"\s*/\s*")
_O_PL_FEAT = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+", re.I)


def _o_primary_artist(artist):
    text = str(artist or "").strip()
    if not text:
        return ""
    text = _O_PL_SLASH.split(text, maxsplit=1)[0].strip()
    text = _O_PL_FEAT.split(text, maxsplit=1)[0].strip()
    return text or str(artist or "").strip()


def _o_group(rx, s):
    # Callers strip the prefix, so the prefix is compared stripped; the group exactly.
    m = rx.search(s)
    return None if m is None else (s[:m.start()].strip(), m.group(1))


def _h_group(fn, s):
    found = fn(s)
    return None if found is None else (found[0].strip(), found[1])


def _stripped_parts(parts):
    return [p.strip() for p in parts if p.strip()]


# (name, oracle(s), helper(s))
_PAIRS = [
    ("feat", lambda s: _O_FEAT.sub("", s), lambda s: "".join(split_ws_led(s, acoustid_service._FEAT_RE, to_eol=True))),
    ("artist_split", _O_SPLIT.split, lambda s: split_ws_led(s, acoustid_service._ARTIST_SPLIT_CORE_RE)),
    ("in_mono", lambda s: _O_MONO.sub("", s).strip(), strip_in_mono_suffix),
    ("scene_split", lambda s: _stripped_parts(_O_SCENE.split(s)), lambda s: _stripped_parts(s.split("-"))),
    ("trailing_bracket", lambda s: _o_group(_O_BRACKET, s), lambda s: _h_group(trailing_bracket_group, s)),
    ("dash_suffix", lambda s: _o_group(_O_DASH, s), lambda s: _h_group(dash_suffix_group, s)),
    ("slskd_split", _O_SLSKD_SPLIT.split,
     lambda s: split_ws_led(s, slskd_service._DASH_SEP_CORE_RE, need_ws=True)),
    ("slskd_bracket", lambda s: _O_SLSKD_BRACKET.sub(" ", s), strip_bracket_credits),
    ("slskd_feat_tail", lambda s: _O_SLSKD_FEAT_TAIL.sub("", s), slskd_service._strip_feat_tail),
    ("playlist_primary_artist", _o_primary_artist, playlist_service._playlist_primary_artist_name),
]

_CORPUS = [
    "", " ", "\n", "a", "-", "()", "(a)", "a  feat b", "A (feat. B)", "Artist [ft. Other]", "Wiz Khalifa feat. Snoop Dogg",
    "Earth, Wind & Fire", "Wiz Khalifa, Juicy J", "a / b, c & d with e", "x and y + z", "Rx", "Simon & Garfunkel",
    "AC/DC", "Florence + the Machine", "Jay-Z featuring Rihanna", "Artist FEAT Other", "Artist (Featuring X)",
    "Band in Mono", "The Beatles in Mono", "The Beatles IN MONO\n", "in mono", " in mono", "Twin Mono", "a in  mono x",
    "Artist - Album (2001) [FLAC]", "x - y - z", "  -  ", "a\t-\tb", "a-b", "a -b", "a- b", "a - b\nc", "a - b\n",
    "a -\nb", "a - \n", "Song Title (Explicit)", "Song Title (Live)", "Title  (Live)  ", "Title (Live)\n", "Title [a]  ",
    "Song (Remastered 2011) (Explicit)", "Song (a (b))", "Song (a]", "Song ()", "Song - Radio Edit", "Song – Live",
    "Song — 2011 Remaster", "Song -  ", "Song - x - y", "Song (feat. Someone) [Remix]", "Song (feat. Someone",
    "Song ( Produced  By X ) tail", "Song [Clean Version]", "Song (with\nX)", "Song (with X)\n(edit)", "(ft.(ft.)",
    "01 - Artist - Title.flac", "Artist - Title", "  ft. x", "[with y]", "Ⅰn mono", "İn mono", "ın mono", "a ſ ft. b",
    "a (produced\n by x) b", "a (produced\n\n by x\n) b", "a (prod\n by x) b", "Song (PRODUCED\tBY X)",
    "Jay-Z / Kanye West", "A featuring B / C", "A Feat. B", "A feat.B", "afeat b", "A  FT  B", "/x", "A /", "A\nfeat\nB",
    "feat  \n\nx", "x feat y\n", "feat x\n\n", "feat x\ny", "a ft. \n", "feat. ft x\nfeat y", "feat\t", "ft..x ft x",
]

_ALPHABET = " \t\n 　 -–—()[]/,+&.aAfeatwithxndmoiıİſprodcubyg"


def _fuzz(seed, count=4000, max_len=24):
    rng = random.Random(seed)
    for _ in range(count):
        yield "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, max_len)))


class OracleEquivalenceTests(unittest.TestCase):
    """Each linear helper must agree with the original quadratic regex."""

    def _check(self, inputs):
        for name, oracle, helper in _PAIRS:
            for s in inputs:
                expected, got = oracle(s), helper(s)
                if expected != got:
                    self.fail(f"{name} differs on {s!r}: oracle={expected!r} helper={got!r}")

    def test_corpus(self):
        self._check(_CORPUS)

    def test_seeded_fuzz(self):
        self._check(list(_fuzz(20261006)))

    def test_seeded_fuzz_word_heavy(self):
        # Fewer separators, more keyword fragments: exercises the \b and re.I paths.
        rng = random.Random(1357)
        words = ["feat", "feat.", "ft", "ft.", "featuring", "FEATURING", "produced by", "with", "x", "and", "in", "mono", "prod.", "produced",
                 "by", "remix", "edit", "-", "–", "(", ")", "[", "]", "/", ",", "&", "+", "a", "İn", "ſ"]
        seps = ["", " ", "  ", "\t", "\n", " "]
        inputs = ["".join(rng.choice(words) + rng.choice(seps) for _ in range(rng.randint(0, 8))) for _ in range(4000)]
        self._check(inputs)

    def test_qa_harness_inputs(self):
        self._check([
            "a" + " " * 1022 + "!",
            "(ft." * 256,
            (" - " + "  " * 500)[:1024],
            "a " * 512,
        ])


class HelperTimingTests(_ReDoSCase):
    """Uncapped adversarial inputs straight into the helpers."""

    def test_long_whitespace_run(self):
        make = lambda n: "a" + " " * n + "!"  # noqa: E731
        for _name, _oracle, helper in _PAIRS:
            with self.subTest(helper=_name):
                self.assert_linear_and_fast(helper, make)

    def test_repeated_open_feat(self):
        self.assert_linear_and_fast(strip_bracket_credits, lambda n: "(ft." * (n // 4))
        self.assert_linear_and_fast(strip_bracket_credits, lambda n: "(" + " " * n + "feat")

    def test_feat_tail_whitespace_then_newline(self):
        # CodeQL #1369: "feat" + a long space run + an embedded newline was O(n^2).
        self.assert_linear_and_fast(slskd_service._strip_feat_tail, lambda n: "feat" + " " * n + "\nx\ny")
        self.assert_linear_and_fast(slskd_service._strip_feat_tail, lambda n: "feat " * (n // 5) + "\nx\ny")

    def test_dash_then_whitespace(self):
        make = lambda n: " - " + "  " * (n // 2)  # noqa: E731
        self.assert_linear_and_fast(lambda s: dash_suffix_group(s), make)
        self.assert_linear_and_fast(lambda s: split_ws_led(s, slskd_service._DASH_SEP_CORE_RE, need_ws=True), make)
        self.assert_linear_and_fast(lambda s: split_ws_led(s, acoustid_service._ARTIST_SPLIT_CORE_RE), make)

    def test_many_short_separators(self):
        self.assert_linear_and_fast(lambda s: dash_suffix_group(s), lambda n: "a - " * (n // 4) + "\nx")
        self.assert_linear_and_fast(strip_in_mono_suffix, lambda n: " in" * (n // 3) + " mono")


class CallerTests(_ReDoSCase):
    """The wired call sites: still fast and behaviour-preserving."""

    def test_acoustid(self):
        self.assert_linear_and_fast(acoustid_service._normalize_albumartist, lambda n: " " * n + "x")
        self.assert_linear_and_fast(acoustid_service._playlist_artist_name_variants, lambda n: "a" + " " * n + "q")
        f = acoustid_service._normalize_albumartist
        self.assertEqual(f("Wiz Khalifa feat. Snoop Dogg"), "Wiz Khalifa")
        self.assertEqual(f("Artist (feat. Other)"), "Artist")
        self.assertEqual(f("Artist [ft. Other]"), "Artist")
        self.assertEqual(f("Earth, Wind & Fire"), "Earth, Wind & Fire")
        self.assertEqual(f("Wiz Khalifa, Juicy J"), "Wiz Khalifa")

    def test_ai_evidence(self):
        self.assert_linear_and_fast(ai_evidence_service._ai_evidence_clean_artist_guess, lambda n: "a" + " " * n + "x")
        self.assert_linear_and_fast(ai_evidence_service._ai_evidence_scene_guess, lambda n: "a" + " " * n + "b")
        self.assertEqual(ai_evidence_service._ai_evidence_clean_artist_guess("The Beatles in Mono"), "The Beatles")

    def test_playlist(self):
        f = playlist_service._playlist_clean_variant_title
        self.assert_linear_and_fast(f, lambda n: "a" + " " * n + "(a)x")
        self.assert_linear_and_fast(f, lambda n: "a" + " " * n + "-")
        self.assertEqual(f("Song Title (Explicit)"), ("Song Title", True))
        self.assertEqual(f("Song - Radio Edit"), ("Song", True))
        self.assertEqual(f("Song Title (Live)"), ("Song Title (Live)", False))

    def test_playlist_primary_artist(self):
        f = playlist_service._playlist_primary_artist_name
        self.assert_linear_and_fast(f, lambda n: "a" + " " * n + "x")
        self.assert_linear_and_fast(f, lambda n: "a" + " " * n + "feat")
        self.assert_linear_and_fast(f, lambda n: "a feat" * (n // 6))
        self.assertEqual(f("Jay-Z / Kanye West"), "Jay-Z")
        self.assertEqual(f("Drake featuring Rihanna"), "Drake")
        self.assertEqual(f("Afeat B"), "Afeat B")

    def test_playlist_download_text_candidates(self):
        f = playlist_service._playlist_download_text_candidates
        self.assert_linear_and_fast(f, lambda n: "a" + " " * n + "x.mp3")
        self.assert_linear_and_fast(f, lambda n: "001 a" + " " * n + "- x.mp3")
        got = f("/nonexistent/001 Requested - Artist - Song.mp3")
        self.assertIn("Song", got["titles"])
        self.assertIn("Artist", got["artists"])
        self.assertNotIn("Requested", got["artists"])

    def test_strip_bracket_credits_produced_by_across_newline(self):
        # #186 N1: "prod" matched first and had no closer before the newline;
        # the original regex backtracked into "produced\s+by".
        self.assertEqual(strip_bracket_credits("a (produced\n by x) b"), "a b")
        self.assertEqual(strip_bracket_credits("a (prod\n by x) b"), "a (prod\n by x) b")

    def test_slskd(self):
        self.assert_linear_and_fast(slskd_service._slskd_title_norm, lambda n: "(feat" * (n // 5))
        self.assert_linear_and_fast(slskd_service._slskd_title_norm, lambda n: "a" + " " * n + "(x")
        self.assert_linear_and_fast(slskd_service._slskd_title_guess_from_name, lambda n: "a" + " " * n + "x.flac")
        self.assertEqual(slskd_service._slskd_title_norm("Song (feat. Someone) [Remix]"), "song")


if __name__ == "__main__":
    unittest.main()
