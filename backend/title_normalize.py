"""Conservative title normalization helpers for import/search text."""

from __future__ import annotations

import re

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
