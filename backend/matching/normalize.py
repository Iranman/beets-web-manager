from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, List
import unicodedata


_BRACKET_PAIRS = {
    "(": ")",
    "[": "]",
    "{": "}",
}
_CLOSERS = set(_BRACKET_PAIRS.values())
_FEATURE_TOKENS = {"feat", "featuring", "ft"}
_VERSION_TOKENS = {
    "remaster",
    "remastered",
    "version",
    "edit",
    "mix",
    "mono",
    "stereo",
    "live",
    "demo",
}


def _s(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _s(value)).casefold()
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _tokens(value: Any) -> List[str]:
    folded = _fold(value).replace("&", " and ")
    out: List[str] = []
    buf: List[str] = []
    for ch in folded:
        cat = unicodedata.category(ch)
        if ch.isalnum():
            buf.append(ch)
        elif ch in {"'", "`", "\u2019"}:
            continue
        elif ch in {"-", "_", ".", "/", "\\", ":", ";", ",", "+", "|"} or cat.startswith(("P", "S", "Z")):
            if buf:
                out.append("".join(buf))
                buf = []
        else:
            if buf:
                out.append("".join(buf))
                buf = []
    if buf:
        out.append("".join(buf))
    return out


def _strip_leading_track_number(tokens: Iterable[str]) -> List[str]:
    values = list(tokens)
    if len(values) >= 2 and values[0].isdigit() and 0 < int(values[0]) < 300:
        return values[1:]
    return values


def _strip_feature_suffix(tokens: Iterable[str]) -> List[str]:
    values = list(tokens)
    for index, token in enumerate(values):
        if token in _FEATURE_TOKENS and index > 0:
            return values[:index]
    return values


def _strip_known_version_suffix(tokens: Iterable[str]) -> List[str]:
    values = list(tokens)
    for index, token in enumerate(values):
        if token in _VERSION_TOKENS and index > 0:
            return values[:index]
    return values


def _without_balanced_parenthetical_segments(value: Any) -> str:
    """Remove balanced bracketed spans in one pass.

    This is used only to create a secondary title variant. The primary
    normalization keeps parenthetical content so distinct identities like
    "Intro" and "Intro (Live at Wembley)" do not collapse by default.
    """

    text = _s(value)
    out: List[str] = []
    stack: List[str] = []
    pending: List[str] = []
    for ch in text:
        if stack:
            pending.append(ch)
            if ch == stack[-1]:
                stack.pop()
                if not stack:
                    pending = []
            elif ch in _BRACKET_PAIRS:
                stack.append(_BRACKET_PAIRS[ch])
            continue
        if ch in _BRACKET_PAIRS:
            stack.append(_BRACKET_PAIRS[ch])
            pending = [ch]
            continue
        out.append(ch)
    if stack:
        out.extend(pending)
    return "".join(out)


def normalize_title(value: Any, *, strip_track_number: bool = False) -> str:
    tokens = _tokens(value)
    if strip_track_number:
        tokens = _strip_leading_track_number(tokens)
    return " ".join(tokens)


def normalize_artist(value: Any) -> str:
    return " ".join(_strip_feature_suffix(_tokens(value)))


def title_variants(title: Any, path: Any = "") -> List[str]:
    variants: List[str] = []
    raw_values = [_s(title)]
    if path:
        raw_values.append(Path(_s(path)).stem)
    for raw in raw_values:
        for candidate in (
            raw,
            _without_balanced_parenthetical_segments(raw),
        ):
            tokens = _tokens(candidate)
            for token_list in (
                tokens,
                _strip_leading_track_number(tokens),
                _strip_feature_suffix(tokens),
                _strip_known_version_suffix(tokens),
                _strip_known_version_suffix(_strip_feature_suffix(tokens)),
            ):
                normalized = " ".join(token_list)
                if normalized and normalized not in variants:
                    variants.append(normalized)
    if not variants:
        normalized = normalize_title(title)
        if normalized:
            variants.append(normalized)
    return variants


def similarity(left: Any, right: Any) -> float:
    left_norm = normalize_title(left)
    right_norm = normalize_title(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    ratio = SequenceMatcher(None, left_norm, right_norm).ratio()
    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    if left_tokens and right_tokens:
        overlap = len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens))
        if overlap >= 0.75:
            ratio = max(ratio, 0.88)
        elif overlap >= 0.5:
            ratio = max(ratio, 0.72)
    return max(0.0, min(1.0, ratio))
