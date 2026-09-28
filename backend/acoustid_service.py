"""AcoustID provider: fingerprint lookups, caching and identity evidence (ARCH-001).
"""

from __future__ import annotations

import difflib, hashlib, json, os, re, sys
from backend.matching import AcoustIDStatus, normalize_track_title_for_matching, similarity as _canonical_similarity
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from backend.app_runtime import METADATA_CACHE_ROOT, MUSIC_ROOT, _s
from backend.app_runtime import _norm, _normalize_name
from backend.matching import verify_audio_against_request
from helpers_mb import _acoustid_lookup
import backend.recording_review as recording_review

# ── ARCH-001 extracted code ──


def _read_file_media_tags(path: Any) -> Dict[str, Any]:
    """Read media tags directly from an audio file (mutagen; beets.mediafile
    if genuinely present, e.g. under unit tests) -- never a remote call.
    Web Manager reads staged/unimported files' tags locally; only stock
    Beets ever owns library tag state."""
    p_str = str(path)
    sys_mods = sys.modules if "sys" in globals() else __import__("sys").modules
    if "beets.mediafile" in sys_mods and hasattr(sys_mods["beets.mediafile"], "MediaFile"):
        try:
            mf = sys_mods["beets.mediafile"].MediaFile(p_str)
            return {
                "title": getattr(mf, "title", "") or "",
                "artist": getattr(mf, "artist", "") or "",
                "album": getattr(mf, "album", "") or "",
                "albumartist": getattr(mf, "albumartist", "") or "",
                "year": str(getattr(mf, "year", "") or ""),
                "track": getattr(mf, "track", "") or "",
                "mb_trackid": getattr(mf, "mb_trackid", "") or "",
                "mb_albumid": getattr(mf, "mb_albumid", "") or "",
                "mb_releasegroupid": getattr(mf, "mb_releasegroupid", "") or "",
                "genre": getattr(mf, "genre", "") or "",
            }
        except Exception:
            pass
    try:
        import mutagen
        f = mutagen.File(p_str, easy=True)
        if f is not None:
            return {
                "title": (f.get("title") or [""])[0],
                "artist": (f.get("artist") or [""])[0],
                "album": (f.get("album") or [""])[0],
                "albumartist": (f.get("albumartist") or [""])[0],
                "year": (f.get("date") or [""])[0],
                "track": (f.get("tracknumber") or [""])[0],
                "genre": (f.get("genre") or [""])[0],
            }
    except Exception:
        pass
    return {}


_ACOUSTID_FILE_CACHE_DIR = METADATA_CACHE_ROOT / "acoustid"


def _audio_cache_file_identity(file_path: str) -> Tuple[Optional[Path], Optional[str]]:
    """Return stable cache path/key parts for an unchanged readable audio file."""
    try:
        path = Path(file_path).resolve(strict=False)
        st = path.stat()
        cache_key = hashlib.sha1(
            f"v2|{path}|{st.st_size}|{getattr(st, 'st_mtime_ns', int(st.st_mtime * 1_000_000_000))}".encode("utf-8")
        ).hexdigest()
        return path, cache_key
    except OSError:
        return None, None


def _acoustid_lookup_cached(file_path: str) -> List[Dict[str, Any]]:
    """AcoustID lookup with file-level disk cache keyed by resolved path + size + mtime_ns.

    Cache entries never expire — a changed file produces a new cache key.
    Returns the same format as _acoustid_lookup (list of recording candidate dicts).
    """
    path, cache_key = _audio_cache_file_identity(file_path)
    if not path or not cache_key:
        return []
    cache_path = _ACOUSTID_FILE_CACHE_DIR / cache_key[:2] / f"{cache_key}.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            return cached if isinstance(cached, list) else []
        except Exception:
            pass

    result = _acoustid_lookup(str(path))
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(result), encoding="utf-8")
    except Exception:
        pass
    return result


def _audio_identity_score(candidate: Dict[str, Any]) -> float:
    try:
        score = float(candidate.get("score") or 0)
    except Exception:
        return 0.0
    return round(score / 100.0 if score > 1 else score, 3)


def _audio_identity_compact_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "score": _audio_identity_score(candidate),
        "acoustid_id": _s(candidate.get("acoustid_id") or ""),
        "mb_trackid": _s(candidate.get("mb_trackid") or ""),
        "mb_releasegroupid": _s(candidate.get("mb_releasegroupid") or ""),
        "title": _s(candidate.get("title") or ""),
        "artist": _s(candidate.get("artist") or ""),
        "album": _s(candidate.get("album") or ""),
        "year": _s(candidate.get("year") or ""),
        "source": _s(candidate.get("source") or "acoustid"),
    }


def _audio_identity_decision(file_path: str, *, expected_artist: str = "",
                             expected_title: str = "",
                             expected_mb_trackid: str = "",
                             text_match: Optional[Dict[str, Any]] = None,
                             acoustid_candidates: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Shared AcoustID-first identity decision for file-level workflow gates."""
    path_text = _s(file_path).strip()
    text_match = text_match or {}
    result: Dict[str, Any] = {
        "fingerprint_status": "not_attempted",
        "acoustid_status": "not_attempted",
        "acoustid_match_score": 0.0,
        "acoustid_id": "",
        "mb_recording_id_candidate": "",
        "mb_release_group_id_candidate": "",
        "metadata_agreement": "unchecked",
        "ai_assessment": "Fingerprint evidence has not been evaluated.",
        "final_confidence": "low",
        "decision_reason": "Audio identity was not checked.",
        "conflicts": [],
        "final_action": "review",
        "identity_status": "review_required",
        "candidates": [],
    }
    if not path_text:
        result.update({
            "fingerprint_status": "failed",
            "acoustid_status": "failed",
            "decision_reason": "No audio file path was available for fingerprinting.",
            "ai_assessment": "Review required because no readable audio path was available.",
        })
        return result
    path = Path(path_text)
    if not path.is_file():
        result.update({
            "fingerprint_status": "failed",
            "acoustid_status": "failed",
            "decision_reason": "Audio file is missing; fingerprinting could not run.",
            "ai_assessment": "Review required because the audio file is not readable.",
        })
        return result
    if not os.access(str(path), os.R_OK):
        result.update({
            "fingerprint_status": "failed",
            "acoustid_status": "failed",
            "decision_reason": "Audio file is not readable; fingerprinting could not run.",
            "ai_assessment": "Review required because the audio file is not readable.",
        })
        return result

    try:
        candidates = acoustid_candidates if acoustid_candidates is not None else _acoustid_lookup_cached(str(path))
    except Exception as ex:
        result.update({
            "fingerprint_status": "failed",
            "acoustid_status": "failed",
            "decision_reason": f"AcoustID lookup failed: {ex}",
            "ai_assessment": "Review required because fingerprint lookup failed.",
            "conflicts": ["acoustid_lookup_failed"],
        })
        return result

    candidates = list(candidates or [])
    result["candidates"] = [_audio_identity_compact_candidate(c) for c in candidates[:5]]
    if not candidates:
        result.update({
            "fingerprint_status": "no_result",
            "acoustid_status": "no_result",
            "metadata_agreement": "degraded",
            "decision_reason": "No AcoustID candidate was returned for the readable audio file.",
            "ai_assessment": "Review required; text metadata alone is not enough to verify this audio.",
            "conflicts": ["no_acoustid_result"],
        })
        return result

    top = candidates[0]
    result.update({
        "fingerprint_status": "matched",
        "acoustid_status": "candidate",
        "acoustid_match_score": _audio_identity_score(top),
        "acoustid_id": _s(top.get("acoustid_id") or ""),
        "mb_recording_id_candidate": _s(top.get("mb_trackid") or ""),
        "mb_release_group_id_candidate": _s(top.get("mb_releasegroupid") or ""),
    })

    # ARCH-002: the accept/review/reject verdict is canonical
    # (backend.matching.verify_audio_against_request): canonical AcoustID
    # score floor and ambiguity window, and an expected Recording ID that the
    # fingerprint contradicts is never accepted on title/artist text.
    verdict = verify_audio_against_request(
        candidates,
        expected_title=_s(expected_title).strip(),
        expected_artist=_s(expected_artist).strip(),
        expected_recording_id=_s(expected_mb_trackid).strip().lower(),
        similarity_fn=_canonical_similarity,
    )
    chosen = next(
        (c for c in candidates if _s(c.get("mb_trackid") or "").strip().lower() == verdict.get("recording_id")),
        top,
    )
    result.update(recording_review.audio_identity_fields(
        verdict, chosen, text_match, acoustid_score=_audio_identity_score(chosen),
    ))
    return result


def _acoustid_verify_match(file_path: str, artist: str, title: str) -> str:
    """Verify an audio file matches the expected artist/title via AcoustID fingerprint.

    Returns:
      "confirmed"  – fingerprint agrees with the expected artist/title
      "mismatch"   – fingerprint found but disagrees (different song)
      "unverified" – no fingerprint available (fpcalc absent, API error, no result)
    """
    if not file_path:
        return "unverified"
    try:
        cands = _acoustid_lookup_cached(file_path)
    except Exception:
        return "unverified"
    if not cands:
        return "unverified"
    for c in cands[:5]:
        c_title = _s(c.get("title") or "").strip()
        c_artist = _s(c.get("artist") or "").strip()
        if not c_title:
            continue
        title_ok = _playlist_title_score(title, c_title) >= 0.78
        artist_ok = (not artist) or _playlist_artist_name_score(artist, c_artist) >= 0.72
        if title_ok and artist_ok:
            return "confirmed"
    return "mismatch"


def _acoustid_fingerprint_ids(file_path: str, limit: int = 5) -> List[str]:
    """Return the top AcoustID-resolved MusicBrainz recording IDs for a file.

    Ordered by AcoustID match confidence, most confident first. Empty when
    fpcalc is unavailable, the lookup fails, or the file has no fingerprint.
    """
    if not file_path:
        return []
    try:
        cands = _acoustid_lookup_cached(file_path)
    except Exception:
        return []
    ids: List[str] = []
    for c in cands[:limit]:
        rid = _s(c.get("mb_trackid") or "").strip().lower()
        if rid and rid not in ids:
            ids.append(rid)
    return ids


def _acoustid_fingerprint_match(source_path: str, lib_path: str) -> Tuple[str, List[str], List[str]]:
    """Fingerprint-verify that two audio files are the same recording via AcoustID.

    Returns (shared_mb_trackid, source_recording_ids, lib_recording_ids). The
    shared ID is empty either when the source has no usable fingerprint, or
    when both files fingerprinted successfully but resolve to different
    recordings (a genuine mismatch — callers should treat that as "verified
    not a duplicate", not "unknown").
    """
    src_ids = _acoustid_fingerprint_ids(source_path)
    if not src_ids:
        return "", [], []
    lib_ids = _acoustid_fingerprint_ids(lib_path)
    for rid in src_ids:
        if rid in lib_ids:
            return rid, src_ids, lib_ids
    return "", src_ids, lib_ids


def _acoustid_multi_file(
    audio_files: List[str], max_files: int = 5
) -> Dict[str, int]:
    """Fingerprint up to max_files representative tracks and aggregate release hit counts.

    Selects: first, middle, last, plus any files with weak/empty embedded tags.
    Returns {mb_albumid: hit_count} — how many fingerprinted tracks point to each
    MB release, suitable for populating acoustid_release_hits on candidates.
    """
    if not audio_files:
        return {}

    paths = audio_files
    n = len(paths)
    candidates_set: List[str] = []
    seen: set = set()

    def _add(p: str) -> None:
        if p not in seen:
            seen.add(p)
            candidates_set.append(p)

    _add(paths[0])
    if n > 1:
        _add(paths[n // 2])
    if n > 2:
        _add(paths[-1])

    if len(candidates_set) < max_files:
        for p in paths:
            if p in seen:
                continue
            try:
                tags = _read_file_media_tags(p)
                if not (tags.get("title", "") or "").strip() or not (tags.get("artist", "") or "").strip():
                    _add(p)
                    if len(candidates_set) >= max_files:
                        break
            except Exception:
                pass

    release_hits: Dict[str, int] = {}
    for p in candidates_set[:max_files]:
        for cand in _acoustid_lookup_cached(p):
            for mb_albumid in (cand.get("mb_albumids") or []):
                if mb_albumid:
                    release_hits[mb_albumid] = release_hits.get(mb_albumid, 0) + 1
    return release_hits


# Patterns that should never appear in albumartist
_FEAT_RE = re.compile(
    r'\s*[\(\[]?(?:feat(?:uring)?\.?|ft\.?|with)\b.*',
    re.IGNORECASE
)


def _normalize_albumartist(s: str) -> str:
    """Normalize an albumartist field:
    1. Unicode punctuation → ASCII
    2. Strip 'feat. X' / 'ft. X' / 'featuring X' suffixes
    3. Strip comma-separated collaborators when there is no '&' in the name
       (e.g. 'Wiz Khalifa, Juicy J' → 'Wiz Khalifa')
       but keep legitimate band names like 'Earth, Wind & Fire',
       'Bob Marley & The Wailers', 'Pete Rock & C.L. Smooth'.
    """
    s = _normalize_name(s)
    # Strip feat./ft./featuring suffix
    s = _FEAT_RE.sub('', s).strip().rstrip(',').strip()
    # Strip comma-listed collaborators (only when no '&' present — avoids
    # breaking "Earth, Wind & Fire" or "Crosby, Stills, Nash & Young")
    if ',' in s and '&' not in s:
        s = s.split(',')[0].strip()
    return s


AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".ogg", ".wav", ".aac", ".opus", ".wma", ".ape", ".alac"}


def _album_track_norm(value: str) -> str:
    try:
        return normalize_track_title_for_matching(value)
    except NameError:
        from backend.matching import normalize_track_title_for_matching as _fallback_norm
        return _fallback_norm(value)


def _album_item_abs_path(raw_path: str) -> str:
    p = _s(raw_path).strip()
    if p and not p.startswith("/"):
        p = str(MUSIC_ROOT / p)
    return p


def _album_track_fingerprint_check(item: Dict[str, Any],
                                   mb_tracks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Fingerprint-check one library item against a candidate MB tracklist.

    ARCH-002 Part 7: `status` is a canonical `AcoustIDStatus` value, not an
    independent vocabulary -- callers that used to compare against the
    legacy strings ("missing"/"none"/"match"/"mismatch"/"unclear") now
    compare against `AcoustIDStatus` members (a `str` subclass, so either
    the enum member or its plain `.value` string works). The decision logic
    itself is unchanged from before this migration -- only the returned
    vocabulary changed, verified against every one of this function's five
    production callers before the rename:
      "missing"  (no readable local file)         -> UNAVAILABLE
      "none"     (fingerprinted, zero candidates)  -> NO_RESULT
      "match"    (a candidate's MBID is in mb_tracks) -> CONFIRMED
      "mismatch" (confident candidate, no title match) -> CONFLICT
      "unclear"  (weak/uncertain candidate)        -> AMBIGUOUS
    """
    path = _album_item_abs_path(item.get("path", ""))
    if not path or not Path(path).exists():
        return {"status": AcoustIDStatus.UNAVAILABLE.value, "path": path}
    cands = _acoustid_lookup_cached(path)
    if not cands:
        return {"status": AcoustIDStatus.NO_RESULT.value}

    mb_ids = {t.get("mb_trackid") for t in mb_tracks if t.get("mb_trackid")}
    for cand in cands:
        cand_id = _s(cand.get("mb_trackid", "")).strip().lower()
        if cand_id and cand_id in mb_ids:
            return {"status": AcoustIDStatus.CONFIRMED.value, "candidate": cand}

    from difflib import SequenceMatcher
    best_cand = cands[0]
    cand_title = _album_track_norm(best_cand.get("title", ""))
    best_title_score = max(
        (SequenceMatcher(None, cand_title, t.get("title_norm", "")).ratio()
         for t in mb_tracks if cand_title and t.get("title_norm")),
        default=0.0,
    )
    if int(best_cand.get("score") or 0) >= 70 and best_title_score < 0.72:
        return {
            "status": AcoustIDStatus.CONFLICT.value,
            "candidate": best_cand,
            "best_title_score": round(best_title_score, 3),
        }
    return {
        "status": AcoustIDStatus.AMBIGUOUS.value,
        "candidate": best_cand,
        "best_title_score": round(best_title_score, 3),
    }


def _artist_folder_fingerprint_confirms(folder: Path, canonical_name: str, sample_limit: int = 3) -> Optional[bool]:
    """Sample audio files under `folder` and AcoustID-verify they belong to `canonical_name`.

    Returns True when at least one sampled file's fingerprint resolves to a
    matching artist, False when every fingerprinted file resolves to a
    disagreeing artist (confirmed mismatch — merge would commingle two
    different artists' catalogs), or None when no sampled file produced
    usable fingerprint data (unverified — caller should not block on this).
    """
    if not canonical_name:
        return None
    sample_paths: List[Path] = []
    try:
        for p in folder.rglob("*"):
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                sample_paths.append(p)
                if len(sample_paths) >= sample_limit:
                    break
    except Exception:
        return None
    if not sample_paths:
        return None

    saw_fingerprint_data = False
    for p in sample_paths:
        try:
            cands = _acoustid_lookup_cached(str(p))
        except Exception:
            continue
        if not cands:
            continue
        for c in cands[:5]:
            c_artist = _s(c.get("artist") or "").strip()
            if not c_artist:
                continue
            saw_fingerprint_data = True
            if _playlist_artist_name_score(canonical_name, c_artist) >= 0.72:
                return True
    return False if saw_fingerprint_data else None


def _playlist_title_variants(value):
    raw = _normalize_name(_s(value))
    variants = {raw}
    # SEC-002 CodeQL repository-wide closure finding (py/polynomial-redos):
    # an unbounded [^)\]]+ between two literal delimiters, scanned via an
    # unanchored re.sub(), is quadratic in input length for adversarial
    # text with no closing bracket (empirically confirmed: ~4.5s at a
    # 16,000-character input). Bounded to 100 chars -- no legitimate
    # parenthetical annotation in a track/album title is remotely that
    # long -- which measured linear-time for the same adversarial input.
    # SEC-002 CodeQL PR-scoped re-check (post-Wave-27 rebase, GitHub alert
    # #1029): the {1,100} content bound alone was insufficient -- the
    # *leading* \s* was still unbounded, and an unanchored re.sub() retried
    # at every offset of a long whitespace run (before a single unclosed
    # bracket) is quadratic regardless of the content bound (empirically
    # confirmed: ~5.4s at a 32,000-character adversarial "long whitespace
    # run + one bracket" input -- a materially different adversarial shape
    # than the "many small brackets" one originally tested, which this
    # bound alone did not cover). Bounding the leading whitespace too
    # (realistic titles never have more than a handful of separator
    # spaces) measured linear time against both adversarial shapes.
    variants.add(re.sub(r"\s{0,20}[\(\[][^\)\]]{1,100}[\)\]]\s{0,20}", " ", raw))
    variants.add(re.sub(r"\b(?:ft\.?|feat\.?|featuring)\b.+$", " ", raw, flags=re.I))
    variants.add(re.sub(r"\b(?:unreleased)\b", " ", raw, flags=re.I))
    variants.add(re.sub(
        r"\b(?:album|single|radio|main|clean|explicit|remaster(?:ed)?|version|edit)\b",
        " ",
        raw,
        flags=re.I,
    ))
    variants.add(re.sub(r"\s+", " ", raw).strip())
    return [_norm(v) for v in variants if _norm(v)]


def _playlist_token_score(left, right):
    left_words = set((left or "").split())
    right_words = set((right or "").split())
    if not left_words or not right_words:
        return 0.0
    overlap = len(left_words & right_words)
    return (2.0 * overlap) / (len(left_words) + len(right_words))


def _playlist_title_score(query_title, item_title):
    query_variants = _playlist_title_variants(query_title)
    item_variants = _playlist_title_variants(item_title)
    best = 0.0
    for qv in query_variants:
        for iv in item_variants:
            seq = difflib.SequenceMatcher(None, qv, iv).ratio()
            tok = _playlist_token_score(qv, iv)
            if qv == iv:
                score = 1.0
            elif len(qv) <= 4 or len(iv) <= 4:
                # Short titles like "B.E.D." must be exact after normalization.
                score = seq if seq >= 0.96 else min(seq, tok)
            elif seq >= 0.88:
                score = seq
            else:
                score = min(seq, tok)
            best = max(best, score)
    return best


_PLAYLIST_ARTIST_CHANNEL_NOISE_RE = re.compile(
    r"\b(?:canal\s+oficial|official\s+channel|oficial|official)\b",
    re.IGNORECASE,
)


def _playlist_strip_artist_channel_noise(value: str) -> str:
    """Strip YouTube-channel-branding noise ("[Canal Oficial]", "Oficial",
    trailing brackets) that commonly rides along with an artist name scraped
    from a video/channel title but isn't part of the actual artist name —
    left in, it silently drags down artist-match scores against a real
    downloaded file's tags/filename, which don't carry that branding."""
    text = _s(value)
    # SEC-002 CodeQL repository-wide closure finding (py/polynomial-redos):
    # same unbounded-content-between-delimiters shape as
    # _playlist_title_variants() above -- bounded for the same reason.
    # SEC-002 CodeQL PR-scoped re-check (post-Wave-27 rebase, GitHub alert
    # #1030): see the identical note on _playlist_title_variants() above --
    # the leading \s* was still unbounded and still quadratic for a long
    # whitespace run before a single unclosed bracket. Bounded it too.
    text = re.sub(r"\s{0,20}\[[^\]]{0,100}\]\s{0,20}$", "", text).strip()
    text = _PLAYLIST_ARTIST_CHANNEL_NOISE_RE.sub("", text)
    return " ".join(text.split()).strip(" -_/")


def _playlist_artist_name_variants(value):
    raw = _s(value).strip()
    if not raw:
        return []
    variants: List[str] = []

    def add(text):
        text = _s(text).strip(" -_/")
        if text and _norm(text) not in {_norm(v) for v in variants}:
            variants.append(text)

    # SEC-002 CodeQL repository-wide closure finding (py/polynomial-redos):
    # same unbounded-content-between-delimiters shape as
    # _playlist_title_variants() above -- bounded for the same reason.
    # SEC-002 CodeQL PR-scoped re-check (post-Wave-27 rebase, GitHub alert
    # #1031): see the identical note on _playlist_title_variants() above --
    # the leading \s* was still unbounded and still quadratic for a long
    # whitespace run before a single unclosed bracket. Bounded it too.
    cleaned = re.sub(r"\s{0,20}\([^)]{0,100}\)\s{0,20}$", "", raw).strip()
    add(raw)
    add(cleaned)
    add(_playlist_strip_artist_channel_noise(raw))
    add(_playlist_strip_artist_channel_noise(cleaned))
    for part in re.split(
        r"\s*(?:/|,|\+|\b(?:ft\.?|feat\.?|featuring|with|x|and)\b|&)\s+",
        cleaned,
        flags=re.IGNORECASE,
    ):
        add(part)
        add(_playlist_strip_artist_channel_noise(part))
    return variants


def _playlist_artist_name_pair_score(query_artist, candidate_artist):
    query_norm = _norm(_normalize_albumartist(_s(query_artist)))
    cand_norm = _norm(_normalize_albumartist(_s(candidate_artist)))
    if not query_norm or not cand_norm:
        return 0.0
    query_words = set(query_norm.split())
    cand_words = set(cand_norm.split())
    seq = difflib.SequenceMatcher(None, query_norm, cand_norm).ratio()
    tok = _playlist_token_score(query_norm, cand_norm)
    subset = 0.0
    if query_words and cand_words and (query_words <= cand_words or cand_words <= query_words):
        subset = 0.92
    return max(seq, tok, subset)


def _playlist_artist_name_score(query_artist, candidate_artist):
    query_variants = _playlist_artist_name_variants(query_artist) or [_s(query_artist).strip()]
    candidate_variants = _playlist_artist_name_variants(candidate_artist) or [_s(candidate_artist).strip()]
    best = 0.0
    for query_variant in query_variants:
        for candidate_variant in candidate_variants:
            best = max(
                best,
                _playlist_artist_name_pair_score(query_variant, candidate_variant),
            )
    return best
