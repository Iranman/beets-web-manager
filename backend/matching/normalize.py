from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path
import re
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

_HEX_CHARS = set("0123456789abcdefABCDEF")

_ANNOTATION_KEYWORDS = (
    "ft.", "feat.", "with", "prod.", "prod", "produced by", "remix", "re-mix",
    "edit", "remaster", "remastered", "radio", "live", "acoustic",
    "album version", "single version", "original version", "explicit album version",
    "clean version", "version", "bonus", "instrumental", "deluxe",
    "explicit", "clean", "official", "lyrics", "lyric", "letra oficial",
    "hq audio", "hq", "hd", "audio", "video", "mv",
)

_ALBUM_TRACK_VERSION_MARKER_RE = re.compile(
    r"\b(?:remix|re-?mix|edit|remaster(?:ed)?|radio|live|acoustic|acappella|"
    r"a\s*cappella|a\s*pella|instrumental|karaoke|dub|extended|club|vip|"
    r"rework|reprise|demo|sketch|outtake|version|mix|mono|stereo|explicit|"
    r"clean|bonus|deluxe|single|album\s+edit)\b",
    re.IGNORECASE,
)

_BARE_FEATURE_PREFIX_RE = re.compile(
    r'\b(?:featuring|feat|ft)\.?\s+',
    re.IGNORECASE,
)

_VARIANTS_FEATURE_PREFIX_RE = re.compile(
    r'\b(?:featuring|feat|ft|with)\.?\s+',
    re.IGNORECASE,
)

_GLUED_FEATURE_RE = re.compile(
    r'(?<=[a-zA-Z0-9]{4})(?:featuring|feat|ft)(?:\.|\s)',
    re.IGNORECASE,
)

_TRACK_FILENAME_SOURCE_ID_SUFFIX_RE = re.compile(
    r'''(?ix)
    (?:[\s._-]+|\s*[\(\[]\s*)
    (?:
        [0-9]{10,}
        | [0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}
        | (?:lidarr|soularr|soulseek|slskd|source|import|staging|playlist)[\s._-]*[a-z0-9]{6,}
        | (?=[a-f0-9]*[a-f])[a-f0-9]{6,9}
    )
    \s*[\)\]]?\s*$
    ''',
    re.IGNORECASE,
)

_TRACK_FILENAME_SHORT_SOURCE_ID_SUFFIX_RE = re.compile(
    r'''(?ix)
    (?:[\s._-]+|\s*[\(\[]\s*)
    (?=[a-f0-9]*[a-f])[a-f0-9]{4,5}
    \s*[\)\]]?\s*$
    ''',
    re.IGNORECASE,
)


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


def _is_hex_uuid(val: str) -> bool:
    if len(val) != 36:
        return False
    if val[8] != "-" or val[13] != "-" or val[18] != "-" or val[23] != "-":
        return False
    for idx, ch in enumerate(val):
        if idx in (8, 13, 18, 23):
            continue
        if ch not in _HEX_CHARS:
            return False
    return True


def _split_dashed_segments(s: str) -> List[str]:
    """Split string on dash/en-dash/em-dash boundaries that have space on either side."""
    if not s:
        return []
    parts: List[str] = []
    current: List[str] = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch in "-–—":
            prev_space = (i > 0 and s[i - 1].isspace())
            next_space = (i + 1 < n and s[i + 1].isspace())
            if prev_space or next_space:
                part = "".join(current).strip()
                if part:
                    parts.append(part)
                current = []
                i += 1
                while i < n and s[i].isspace():
                    i += 1
                continue
        current.append(ch)
        i += 1
    if current:
        part = "".join(current).strip()
        if part:
            parts.append(part)
    return parts


def _is_annotation_bracket_content(content: str) -> bool:
    trimmed = content.strip().lower()
    if not trimmed:
        return False
    for kw in _ANNOTATION_KEYWORDS:
        if trimmed == kw:
            return True
        if trimmed.startswith(kw):
            rest = trimmed[len(kw):]
            if not rest or rest[0] in " \t:;.,-_–—)]/\\|":
                return True
    return False


def _strip_annotations_and_unclosed(text: str) -> str:
    """Strip balanced annotation bracket spans and unclosed trailing brackets in linear time."""
    if not text:
        return ""
    n = len(text)
    out: List[str] = []
    i = 0
    while i < n:
        ch = text[i]
        if ch in "([":
            closer = ")" if ch == "(" else "]"
            depth = 1
            j = i + 1
            while j < n:
                if text[j] == ch:
                    depth += 1
                elif text[j] == closer:
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if depth == 0:
                content = text[i + 1:j]
                if _is_annotation_bracket_content(content):
                    i = j + 1
                    while i < n and text[i].isspace():
                        i += 1
                    continue
                else:
                    out.append(text[i:j + 1])
                    i = j + 1
                    continue
            else:
                break
        else:
            out.append(ch)
            i += 1
    return "".join(out).strip()


def _strip_leading_track_num(s: str) -> str:
    """Strip leading numeric or format track token prefix such as '01 - ' or '%track{01} - '."""
    trimmed = s.lstrip()
    if trimmed.startswith("%"):
        brace_close = trimmed.find("}")
        if brace_close > 1:
            rest = trimmed[brace_close + 1:].lstrip(" -–—.")
            return rest.strip()
    i = 0
    while i < len(trimmed) and trimmed[i].isdigit():
        i += 1
    if i > 0 and i < len(trimmed):
        delims = trimmed[i:]
        m = re.match(r'^\s*[-–—\.]\s*', delims)
        if m:
            return delims[m.end():].strip()
    return s


def normalize_title(value: Any, *, strip_track_number: bool = False) -> str:
    tokens = _tokens(value)
    if strip_track_number:
        tokens = _strip_leading_track_number(tokens)
    return " ".join(tokens)


def normalize_artist(value: Any) -> str:
    return " ".join(_strip_feature_suffix(_tokens(value)))


def strip_track_filename_id_suffix(value: Any) -> str:
    """Strip trailing Lidarr/Soulseek/hash ID suffixes from track filenames/titles."""
    text = _s(value).strip()
    for _ in range(4):
        if not text:
            break
        search_start = max(0, len(text) - 120)
        m = _TRACK_FILENAME_SOURCE_ID_SUFFIX_RE.search(text, search_start)
        if m and m.end() == len(text):
            cleaned = text[:m.start()].strip(" -_.")
        else:
            cleaned = text
        if cleaned == text:
            m_short = _TRACK_FILENAME_SHORT_SOURCE_ID_SUFFIX_RE.search(text, search_start)
            if m_short and m_short.end() == len(text):
                short_cleaned = text[:m_short.start()].strip(" -_.")
                dirty_prefix_hint = bool(
                    re.search(r"[_\(\)\[\]]", short_cleaned)
                    or re.match(r"^\s*\d{1,3}[\s._-]+", short_cleaned)
                )
                if short_cleaned != text and dirty_prefix_hint:
                    cleaned = short_cleaned
        if cleaned == text or not cleaned:
            break
        text = cleaned
    return text


def track_filename_has_source_id_suffix(value: Any) -> bool:
    """Return True if the text ends in a stripped source-ID suffix."""
    text = _s(value).strip()
    return bool(text and strip_track_filename_id_suffix(text) != text)


def normalize_track_title_for_matching(value: Any) -> str:
    """Aggressively normalizes messy real-world file/candidate track titles for matching.

    Strips source ID suffixes (Lidarr/Soularr/short hashes), annotation brackets,
    @handle mentions, feature suffixes (feat./ft.), non-alphanumeric chars,
    and leading 'bonus track' prefixes in linear time without catastrophic backtracking.
    """
    text = strip_track_filename_id_suffix(value).casefold()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("&", " and ")
    text = _strip_annotations_and_unclosed(text)
    text = re.sub(r"@\w+", " ", text)
    m_feat = _BARE_FEATURE_PREFIX_RE.search(text)
    if m_feat:
        text = text[:m_feat.start()]
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = text.strip()
    while text.startswith("bonus track"):
        text = text[11:].strip()
    return " ".join(text.split())


def track_feature_variants(value: Any) -> List[str]:
    """Return title candidates with normal and glued feature suffixes removed."""
    text = _s(value).strip()
    if not text:
        return []
    variants = [text]

    m = _VARIANTS_FEATURE_PREFIX_RE.search(text)
    if m and m.start() > 0:
        stripped = text[:m.start()].strip(" -_–—:;,.")
        if stripped and stripped != text:
            variants.append(stripped)

    m_glue = _GLUED_FEATURE_RE.search(text)
    if m_glue and m_glue.start() >= 4:
        glued = text[:m_glue.start()].strip(" -_–—:;,.")
        if glued and glued != text:
            variants.append(glued)
        spaced = text[:m_glue.start()] + " " + text[m_glue.start():]
        if spaced and spaced != text:
            variants.append(spaced)
            m_sp = _VARIANTS_FEATURE_PREFIX_RE.search(spaced)
            if m_sp and m_sp.start() > 0:
                spaced_stripped = spaced[:m_sp.start()].strip(" -_–—:;,.")
                if spaced_stripped and spaced_stripped != spaced:
                    variants.append(spaced_stripped)

    out: List[str] = []
    seen: set[str] = set()
    for val in variants:
        key = val.casefold()
        if val and key not in seen:
            seen.add(key)
            out.append(val)
    return out


def track_parenthetical_alias_variants(value: Any) -> List[str]:
    """Return conservative title aliases such as 'Money (That\\'s What I Want)' -> 'Money'."""
    text = _s(value).strip()
    if not text or len(text) < 5:
        return []
    if not (text.endswith(")") or text.endswith("]")):
        return []
    closer = text[-1]
    opener = "(" if closer == ")" else "["
    depth = 0
    open_idx = -1
    for idx in range(len(text) - 1, -1, -1):
        if text[idx] == closer:
            depth += 1
        elif text[idx] == opener:
            depth -= 1
            if depth == 0:
                open_idx = idx
                break
    if open_idx <= 0:
        return []
    inside = text[open_idx + 1:-1].strip()
    if len(inside) < 2:
        return []
    if _ALBUM_TRACK_VERSION_MARKER_RE.search(inside):
        return []
    base = text[:open_idx].strip(" -_–—:;,.")
    if len(normalize_track_title_for_matching(base)) >= 3:
        return [base]
    return []


def track_path_prefixes(path: Any) -> List[str]:
    """Normalized artist/album prefixes that may be embedded in track titles."""
    raw_path = _s(path).replace("\\", "/")
    if not raw_path:
        return []
    for marker in (
        "/data/media/music/",
        "/data/torrents/music/",
        "/data/downloads/music/",
        "/downloads/music/",
        "/download/music/",
    ):
        if marker in raw_path:
            raw_path = raw_path.split(marker, 1)[1]
            break
    parts = [p for p in raw_path.split("/") if p]
    prefixes: List[str] = []

    def _prefix_candidates(value: str) -> List[str]:
        text = _s(value).strip()
        if not text:
            return []
        candidates = [text]

        # 1. Trailing 4-digit year in parens/brackets
        s = text.rstrip()
        if len(s) >= 6 and s[-1] in ")]" and s[-6] in "([" and s[-5:-1].isdigit():
            no_year = s[:-6].rstrip()
            if no_year and no_year != text:
                candidates.append(no_year)
        else:
            no_year = ""

        # 2. Trailing MBID in brackets/braces/parens
        if len(s) >= 38 and s[-1] in ")}]" and s[-38] in "({[":
            cand_uuid = s[-37:-1]
            if _is_hex_uuid(cand_uuid):
                no_mbid = s[:-38].rstrip()
                if no_mbid and no_mbid != text:
                    candidates.append(no_mbid)
            else:
                no_mbid = ""
        else:
            no_mbid = ""

        # 3. Leading track/index number
        numeric_alias_base = no_mbid or no_year or text
        sn = numeric_alias_base.lstrip()
        i = 0
        while i < len(sn) and sn[i].isdigit():
            i += 1
        if i > 0 and i < len(sn) and sn[i] in " ._-\t":
            while i < len(sn) and sn[i] in " ._-\t":
                i += 1
            no_leading_number = sn[i:].strip()
            if no_leading_number and no_leading_number != numeric_alias_base:
                candidates.append(no_leading_number)

        # 4. Bare artist without UUID stamp
        if len(s) >= 38 and s[-1] in ")}]" and s[-38] in "({[":
            cand_uuid = s[-37:-1]
            if _is_hex_uuid(cand_uuid):
                bare_artist = s[:-38].strip()
                if bare_artist and bare_artist != text:
                    candidates.append(bare_artist)
        return candidates

    if len(parts) >= 2:
        prefixes.extend(_prefix_candidates(parts[0]))
        prefixes.extend(_prefix_candidates(parts[1]))
    if len(parts) >= 1:
        stem = Path(parts[-1]).stem
        file_parts = _split_dashed_segments(stem)
        if file_parts:
            prefixes.extend(_prefix_candidates(file_parts[0]))
    out: List[str] = []
    seen: set[str] = set()
    for prefix in prefixes:
        norm = normalize_track_title_for_matching(prefix)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def track_title_variants_for_matching(title: Any, path: Any = "") -> List[str]:
    """Return all plausible normalized matching title variants for a track title/path."""
    raw_values = [_s(title).strip()]
    if path:
        raw_values.append(Path(_s(path)).stem)
    for raw in list(raw_values):
        cleaned = strip_track_filename_id_suffix(raw)
        if cleaned and cleaned not in raw_values:
            raw_values.append(cleaned)
    variants: List[str] = []
    seen: set[str] = set()
    path_prefixes = track_path_prefixes(path)
    for raw in raw_values:
        if not raw:
            continue
        candidates = [raw]
        parts = _split_dashed_segments(raw)
        if len(parts) > 1:
            for idx in range(1, min(len(parts), 8)):
                candidates.append(" - ".join(parts[idx:]))
            candidates.append(parts[-1])
        stripped = raw
        for _ in range(3):
            nxt = _strip_leading_track_num(stripped)
            if nxt == stripped:
                break
            stripped = nxt
            candidates.append(stripped)
        expanded_candidates: List[str] = []
        for cand in candidates:
            expanded_candidates.append(cand)
            stripped_cand = cand
            for _ in range(3):
                nxt = _strip_leading_track_num(stripped_cand)
                if nxt == stripped_cand:
                    break
                stripped_cand = nxt
                expanded_candidates.append(stripped_cand)
        feature_candidates: List[str] = []
        for cand in expanded_candidates:
            feature_candidates.extend(track_feature_variants(cand) or [cand])
        alias_candidates: List[str] = []
        for cand in feature_candidates:
            alias_candidates.append(cand)
            alias_candidates.extend(track_parenthetical_alias_variants(cand))
        for cand in alias_candidates:
            bare = _strip_annotations_and_unclosed(cand)
            for val in (cand, bare):
                norm = normalize_track_title_for_matching(val)
                norm_options = [norm] if norm else []
                for prefix in path_prefixes:
                    if norm == prefix:
                        continue
                    if norm.startswith(prefix + " "):
                        norm_options.append(norm[len(prefix):].strip())
                for opt in norm_options:
                    if opt and opt not in seen:
                        seen.add(opt)
                        variants.append(opt)
    return variants


def title_variants(title: Any, path: Any = "") -> List[str]:
    """Extract canonical title variants, incorporating both token-based and track-matching variants."""
    variants: List[str] = []
    seen: set[str] = set()

    for v in track_title_variants_for_matching(title, path):
        if v and v not in seen:
            seen.add(v)
            variants.append(v)

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
                if normalized and normalized not in seen:
                    seen.add(normalized)
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
        left_match_norm = normalize_track_title_for_matching(left)
        right_match_norm = normalize_track_title_for_matching(right)
        if not left_match_norm or not right_match_norm:
            return 0.0
        left_norm = left_match_norm
        right_norm = right_match_norm
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

