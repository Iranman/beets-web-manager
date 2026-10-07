"""Conservative title normalization helpers for import/search text."""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

_TIME_LIKE_HYPHEN_RE = re.compile(r"(?<!\d)(\d{1,2})[-‐‑‒–—](\d{2})(?!\d)")


def restore_time_colon_title(value: str) -> str:
    """Restore album/title punctuation commonly flattened by filesystems.

    Some folders cannot use ":" and arrive as names like "14-59". Treat only
    1-2 digit hour/minute-shaped values as time-like titles so catalog numbers,
    years, and artist names such as "blink-182" are not changed.
    """
    text = str(value or "")

    def repl(match: re.Match[str]) -> str:
        hour = int(match.group(1))
        minute = int(match.group(2))
        if 0 <= hour <= 24 and 0 <= minute <= 59:
            return f"{match.group(1)}:{match.group(2)}"
        return match.group(0)

    return _TIME_LIKE_HYPHEN_RE.sub(repl, text)


# ── SEC-5: linear-time replacements for whitespace-led regexes ──────────────
# Patterns of the shape ``\s*CORE`` / ``\s+CORE`` backtrack quadratically on
# long whitespace runs (CodeQL py/polynomial-redos). The helpers below search
# only for CORE and extend each match back over whitespace by hand, which is
# exactly what the leading quantifier did, in a single left-to-right pass.
# ``str.isspace()`` is the same character class as ``\s`` for str patterns.


def split_ws_led(text: str, core: re.Pattern, *, need_ws: bool = False, to_eol: bool = False) -> List[str]:
    r"""``re.split(r"\s*" + core)`` (``\s+`` when *need_ws*); with *to_eol*
    each match also runs to the end of its line (``core + ".*"``)."""
    pieces: List[str] = []
    pos = search_at = 0
    while True:
        m = core.search(text, search_at)
        if not m:
            break
        start = m.start()
        while start > pos and text[start - 1].isspace():
            start -= 1
        if need_ws and start == m.start():
            search_at = m.start() + 1
            continue
        end = m.end()
        if to_eol:
            nl = text.find("\n", end)
            end = len(text) if nl < 0 else nl
        pieces.append(text[pos:start])
        pos = search_at = end
    pieces.append(text[pos:])
    return pieces


def _dollar_end(text: str) -> int:
    r"""Where a non-MULTILINE ``$`` can close a match that does not consume ``\n``."""
    return len(text) - 1 if text.endswith("\n") else len(text)


_MONO_RE = re.compile(r"mono", re.I)
_IN_RE = re.compile(r"in", re.I)


def strip_in_mono_suffix(text: str) -> str:
    r"""``re.sub(r"\s+\bin\s+mono\b$", "", text, flags=re.I).strip()``."""
    e = _dollar_end(text)
    if e >= 4 and _MONO_RE.fullmatch(text, e - 4, e):
        r = e - 4
        while r > 0 and text[r - 1].isspace():
            r -= 1
        if r < e - 4 and r >= 3 and _IN_RE.fullmatch(text, r - 2, r) and text[r - 3].isspace():
            return text[:r - 2].strip()
    return text.strip()


def trailing_bracket_group(text: str) -> Optional[Tuple[str, str]]:
    r"""``re.search(r"\s*[\(\[]([^()\[\]]+)[\)\]]\s*$", text)`` as
    ``(text_before_opener, group)`` or ``None``."""
    t = text.rstrip()
    j = len(t) - 1
    if j < 2 or t[j] not in ")]":
        return None
    i = max(t.rfind(c, 0, j) for c in "()[]")
    if i < 0 or t[i] not in "([" or j - i < 2:
        return None
    return text[:i], t[i + 1:j]


def dash_suffix_group(text: str) -> Optional[Tuple[str, str]]:
    r"""``re.search(r"\s+[-–—]\s+(.+)$", text)`` as
    ``(text_before_dash, group)`` or ``None``."""
    e = _dollar_end(text)
    line_start = text.rfind("\n", 0, e) + 1
    n = len(text)
    for d in range(1, n):
        if text[d] not in "-–—" or not text[d - 1].isspace():
            continue
        run_end = d + 1
        while run_end < n and text[run_end].isspace():
            run_end += 1
        g = min(run_end, e - 1)
        if g >= d + 2 and g >= line_start:
            return text[:d], text[g:e]
    return None


_SLSKD_KW_RE = re.compile(r"feat\.?|ft\.?|with|prod\.?|produced\s+by|remix|edit|version|bonus|clean|explicit", re.I)
_PRODUCED_BY_RE = re.compile(r"produced\s+by", re.I)


def strip_bracket_credits(text: str) -> str:
    r"""``re.sub(r"\s*[\(\[]\s*(?:feat\.?|ft\.?|with|prod\.?|produced\s+by|remix|edit|version|bonus|clean|explicit).*?[\)\]]\s*",
    " ", text, flags=re.I)``."""
    n = len(text)
    # next_closer[k] / next_nl[k]: first ")"/"]" and first "\n" at index >= k (n if none).
    next_closer = [n] * (n + 1)
    next_nl = [n] * (n + 1)
    for k in range(n - 1, -1, -1):
        next_closer[k] = k if text[k] in ")]" else next_closer[k + 1]
        next_nl[k] = k if text[k] == "\n" else next_nl[k + 1]
    out: List[str] = []
    pos = 0
    o = pos
    while o < n:
        if text[o] not in "([":
            o += 1
            continue
        q = o + 1
        while q < n and text[q].isspace():
            q += 1
        # ".*?[)\]]" succeeds iff a closer precedes the next newline after the
        # keyword. Only "produced\s+by" can consume a newline, so when the first
        # alternative ("prod") finds no closer the regex backtracks into it (#186 N1).
        closer = -1
        for m in (_SLSKD_KW_RE.match(text, q), _PRODUCED_BY_RE.match(text, q)):
            if m and next_closer[m.end()] < next_nl[m.end()]:
                closer = next_closer[m.end()]
                break
        if closer < 0:
            o += 1
            continue
        start = o
        while start > pos and text[start - 1].isspace():
            start -= 1
        end = closer + 1
        while end < n and text[end].isspace():
            end += 1
        out.append(text[pos:start])
        out.append(" ")
        pos = o = end
    out.append(text[pos:])
    return "".join(out)
