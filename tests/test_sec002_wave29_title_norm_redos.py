"""SEC-002 / ARCH-003 Wave 29: regression tests for two CodeQL findings on
PR #112 that were real, not false positives.

1. py/polynomial-redos -- backend/transaction_engine.py's
   _mb_track_repair_title_norm() (and the deliberately duplicated
   backend/mb_alignment.py album_track_norm()) stripped bracketed
   annotations with:

       re.sub(r"[\\(\\[\\{].*?[\\)\\]\\}]", "", t)

   That is quadratic, not linear. The lazy `.*?` stops at the first closing
   bracket, so an opening bracket with no reachable closer fails only after
   scanning to the newline or end of string -- and re.sub then retries at
   the next offset, rescanning the same tail. An input of N opening brackets
   therefore costs O(N^2). Measured against the pre-fix regex: 244ms at 8k
   chars, 958ms at 16k, 3.9s at 32k, and 11.6s for a 64k-char
   `"(" * n + "a" * n` payload -- a single request body, since these titles
   arrive as album_mb_track_repair_v1's caller-supplied `target_tracks`.

   Replaced by _strip_bracketed_spans(), one forward pass that precomputes
   the next reachable closing bracket per offset, so an unmatched opener is
   emitted literally with no rescan. Byte-identical output to the regex --
   asserted below over fixed edge cases plus randomized inputs -- and O(n).

2. py/clear-text-storage-sensitive-data -- the four `beet -c` override
   files in backend/beets_control_agent.py were created with
   `open(path, "w")` and only narrowed to 0600 by a following os.chmod().
   A config_override can carry a credential (/fingerprint builds one
   containing the chroma/acoustid `apikey`), so between those two calls the
   secret sat in a shared /tmp at the process umask (0644), and the plain
   open() would also have followed a pre-planted symlink and written the key
   wherever it pointed. Consolidated into _write_private_config_file(),
   which uses O_CREAT|O_EXCL|O_NOFOLLOW with mode 0600.

   The plaintext-on-disk itself stays -- beets only accepts a config as a
   real file -- which is the architecture decision already recorded for
   app.py's generated config in docs/TECHNICAL_DEBT.md SEC-002.

Timing assertions use a generous ceiling (the fixed implementations run in
single-digit milliseconds at these sizes) so the suite stays reliable on a
loaded shared CI runner while still catching a reintroduced O(n^2).
"""

from __future__ import annotations

import inspect
import os
import random
import re
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

from backend.beets_control_agent import _write_private_config_file
from backend.mb_alignment import album_track_norm
from backend.transaction_engine import (
    _mb_track_repair_title_norm,
    _strip_bracketed_spans,
)

_TIMING_CEILING_SECONDS = 2.0

# The exact pattern that was removed. Kept here only as the behavioural
# oracle the linear implementation must reproduce; never run on untrusted
# input, and never at adversarial sizes.
_LEGACY_BRACKET_RE = re.compile(r"[\(\[\{].*?[\)\]\}]")

_FIXED_CASES = [
    "",
    "(",
    ")",
    "()",
    "(a)",
    "((a))",
    ")(",
    "(a",
    "a)",
    "a(b)c",
    "(a)(b)",
    "[x] (y) {z}",
    "(a\nb)",
    "(\n)",
    "a (feat. b) c",
    "({[a]})",
    "(((",
    ")))",
    "(]",
    "[)",
    "{a)",
    "(a]b[c)",
    "song (live) [remix] {2020}",
    "no brackets here",
    "(unclosed (also unclosed",
    "trail (",
    "( lead",
    "\n(a)\n",
    "Track Title (Remastered 2011)",
    "Title [Explicit] (feat. Someone) - Live",
]


class StripBracketedSpansEquivalenceTests(unittest.TestCase):
    """The linear replacement must be byte-identical to the regex it
    replaced -- this is a pure performance fix, not a behaviour change."""

    def test_fixed_edge_cases_match_the_legacy_regex(self):
        for text in _FIXED_CASES:
            with self.subTest(text=text):
                self.assertEqual(
                    _strip_bracketed_spans(text),
                    _LEGACY_BRACKET_RE.sub("", text),
                )

    def test_randomized_inputs_match_the_legacy_regex(self):
        alphabet = "([{)]}ab \n\t-'é"
        rng = random.Random(20260915)
        for _ in range(20000):
            text = "".join(
                rng.choice(alphabet) for _ in range(rng.randint(0, 40))
            )
            expected = _LEGACY_BRACKET_RE.sub("", text)
            if _strip_bracketed_spans(text) != expected:
                self.fail(
                    f"divergence on {text!r}: "
                    f"linear={_strip_bracketed_spans(text)!r} "
                    f"regex={expected!r}"
                )

    def test_unmatched_opener_is_preserved_like_the_regex(self):
        # The regex cannot match an opener with no closer after it, so the
        # opener survives into the output. Easy thing for a "cleanup" to get
        # wrong by stripping it.
        self.assertEqual(_strip_bracketed_spans("abc (def"), "abc (def")
        self.assertEqual(_strip_bracketed_spans("a(b)c(d"), "ac(d")

    def test_newline_blocks_a_match_like_dot_does(self):
        # `.` does not match a newline, so an opener cannot pair with a
        # closer on a later line.
        self.assertEqual(_strip_bracketed_spans("(a\nb)"), "(a\nb)")
        self.assertEqual(
            _strip_bracketed_spans("(a\nb)"), _LEGACY_BRACKET_RE.sub("", "(a\nb)")
        )


class TitleNormalizerEquivalenceTests(unittest.TestCase):
    """Both normalizers -- the transaction_engine copy and the
    mb_alignment copy that is duplicated rather than imported -- must keep
    their pre-fix output exactly."""

    @staticmethod
    def _legacy_norm(text) -> str:
        t = str(text or "").lower()
        t = _LEGACY_BRACKET_RE.sub("", t)
        t = re.sub(r"[^\w\s]", "", t)
        return " ".join(t.split())

    def test_fixed_cases_unchanged_in_both_copies(self):
        for text in _FIXED_CASES:
            with self.subTest(text=text):
                expected = self._legacy_norm(text)
                self.assertEqual(_mb_track_repair_title_norm(text), expected)
                self.assertEqual(album_track_norm(text), expected)

    def test_randomized_cases_unchanged_in_both_copies(self):
        alphabet = "([{)]}ab \n\t-'é"
        rng = random.Random(987654321)
        for _ in range(20000):
            text = "".join(
                rng.choice(alphabet) for _ in range(rng.randint(0, 40))
            )
            expected = self._legacy_norm(text)
            if _mb_track_repair_title_norm(text) != expected:
                self.fail(f"transaction_engine divergence on {text!r}")
            if album_track_norm(text) != expected:
                self.fail(f"mb_alignment divergence on {text!r}")

    def test_realistic_titles_still_normalize_as_expected(self):
        self.assertEqual(
            _mb_track_repair_title_norm("Bohemian Rhapsody (Remastered 2011)"),
            "bohemian rhapsody",
        )
        self.assertEqual(
            _mb_track_repair_title_norm("Song [Explicit] (feat. X)"),
            "song",
        )
        self.assertEqual(album_track_norm("  Mixed   CASE  Title "), "mixed case title")


class TitleNormalizerAdversarialTimingTests(unittest.TestCase):
    """The payload that made the pre-fix regex take 11.6 seconds must now
    complete in milliseconds."""

    def _assert_fast(self, fn, text):
        t0 = time.perf_counter()
        fn(text)
        elapsed = time.perf_counter() - t0
        self.assertLess(
            elapsed,
            _TIMING_CEILING_SECONDS,
            f"{fn.__name__} took {elapsed:.2f}s on {len(text)} chars",
        )

    def test_many_unmatched_openers(self):
        payload = "(" * 32000 + "a" * 32000
        self._assert_fast(_strip_bracketed_spans, payload)
        self._assert_fast(_mb_track_repair_title_norm, payload)
        self._assert_fast(album_track_norm, payload)

    def test_all_openers_no_closer_anywhere(self):
        payload = "(" * 64000
        self._assert_fast(_strip_bracketed_spans, payload)
        self._assert_fast(_mb_track_repair_title_norm, payload)

    def test_single_trailing_closer_after_long_opener_run(self):
        # One reachable closer makes the first match consume everything,
        # then the remaining tail has none -- the shape that kept the regex
        # quadratic even when a closer was present.
        payload = "(" * 32000 + ")" + "(" * 32000
        self._assert_fast(_strip_bracketed_spans, payload)
        self._assert_fast(_mb_track_repair_title_norm, payload)

    def test_mixed_bracket_types(self):
        payload = ("([{" * 16000) + "a" * 16000
        self._assert_fast(_strip_bracketed_spans, payload)
        self._assert_fast(album_track_norm, payload)

    def test_scaling_is_not_quadratic(self):
        # Doubling the input must not quadruple the cost. Compared against a
        # deliberately loose factor so runner noise cannot fail this, while
        # a genuine O(n^2) reintroduction (factor ~4) still does.
        def timed(n: int) -> float:
            # Best-of-3: a shared runner can stall any single sample, and a
            # slow `base` or a stalled `doubled` would both skew the ratio.
            # The minimum is the sample least polluted by descheduling.
            payload = "(" * n + "a" * n
            best = float("inf")
            for _ in range(3):
                t0 = time.perf_counter()
                _strip_bracketed_spans(payload)
                best = min(best, time.perf_counter() - t0)
            return best

        base = max(timed(40000), 1e-4)
        doubled = timed(80000)
        self.assertLess(
            doubled / base,
            3.0,
            f"scaling looks quadratic: {base:.4f}s -> {doubled:.4f}s",
        )


class TitleNormalizerSourceTests(unittest.TestCase):
    """Guard against the quadratic pattern being pasted back in."""

    def test_quadratic_bracket_regex_is_gone_from_production_sources(self):
        repo_root = Path(__file__).resolve().parents[1]
        for rel in ("backend/transaction_engine.py", "backend/mb_alignment.py"):
            source = (repo_root / rel).read_text(encoding="utf-8")
            # Strip docstrings/comments' escaped rendition by checking for
            # the live call form only.
            self.assertNotIn(
                'sub(r"[\\(\\[\\{].*?[\\)\\]\\}]"',
                source,
                f"{rel} reintroduced the quadratic bracket regex",
            )


class PrivateConfigFileTests(unittest.TestCase):
    """_write_private_config_file must never expose a credential file, even
    briefly, and must refuse a path that already exists."""

    def setUp(self):
        self._dir = tempfile.mkdtemp()

    def test_content_is_written_verbatim(self):
        path = os.path.join(self._dir, "verbatim.yaml")
        payload = 'chroma:\n  auto: no\n  apikey: "abc123"\nacoustid:\n  apikey: "abc123"\n'
        _write_private_config_file(path, payload)
        self.assertEqual(Path(path).read_text(encoding="utf-8"), payload)

    @unittest.skipIf(sys.platform == "win32", "POSIX mode bits are not modelled on Windows")
    def test_file_is_created_owner_only_not_chmodded_afterwards(self):
        path = os.path.join(self._dir, "mode.yaml")
        _write_private_config_file(path, "acoustid:\n  apikey: \"s3cret\"\n")
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(
            mode,
            0o600,
            f"config file mode {oct(mode)} -- group/other must never have access",
        )

    def test_creation_flags_are_exclusive_and_owner_only(self):
        # Asserted by inspection rather than by flipping the process umask:
        # os.umask() is global and this suite runs threaded HTTP servers, so
        # temporarily widening it could change the mode of files another
        # test creates concurrently.
        source = inspect.getsource(_write_private_config_file)
        # Inspect executable lines only -- the docstring deliberately quotes
        # the old open()+os.chmod() pair it replaced.
        body = source.split('"""')[-1]
        self.assertIn("os.O_CREAT", body)
        self.assertIn("os.O_EXCL", body)
        self.assertIn("O_NOFOLLOW", body)
        # The mode must be passed to os.open() itself, not applied after.
        self.assertIn("os.open(path, flags, 0o600)", body)
        self.assertNotIn("os.chmod", body)

    def test_refuses_a_preexisting_path(self):
        path = os.path.join(self._dir, "exists.yaml")
        Path(path).write_text("planted\n", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            _write_private_config_file(path, "apikey: secret\n")
        # The planted file must be left untouched, not truncated.
        self.assertEqual(Path(path).read_text(encoding="utf-8"), "planted\n")

    @unittest.skipIf(sys.platform == "win32", "symlink creation needs privileges on Windows")
    def test_refuses_to_follow_a_planted_symlink(self):
        victim = os.path.join(self._dir, "victim.txt")
        Path(victim).write_text("important\n", encoding="utf-8")
        link = os.path.join(self._dir, "link.yaml")
        os.symlink(victim, link)
        with self.assertRaises(OSError):
            _write_private_config_file(link, "apikey: secret\n")
        self.assertEqual(Path(victim).read_text(encoding="utf-8"), "important\n")

    def test_agent_no_longer_uses_bare_open_plus_chmod_for_config_overrides(self):
        repo_root = Path(__file__).resolve().parents[1]
        source = (repo_root / "backend" / "beets_control_agent.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("os.chmod(tmp_cfg_path, 0o600)", source)
        # All four override sites must go through the shared writer.
        self.assertEqual(source.count("_write_private_config_file(tmp_cfg_path"), 4)


if __name__ == "__main__":
    unittest.main()
