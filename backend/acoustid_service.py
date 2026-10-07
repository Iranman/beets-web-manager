"""AcoustID provider: fingerprint lookups, caching and identity evidence (ARCH-001).
"""

from __future__ import annotations

import difflib, hashlib, json, os, re, sys
from backend.matching import AcoustIDStatus, normalize_track_title_for_matching, similarity as _canonical_similarity
from backend.matching import acoustid_evidence_from_hits
from backend.matching.recording import ACOUSTID_MIN_SCORE
from backend.matching.track_alignment import _hit_score
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from backend.app_runtime import METADATA_CACHE_ROOT, MUSIC_ROOT, _s
from backend.app_runtime import _norm, _normalize_name
from backend.matching import verify_audio_against_request
from helpers_mb import acoustid_lookup_outcome
from backend.provider_boundary import ProviderOutcome, ProviderResult
import backend.recording_review as recording_review
from backend.title_normalize import split_ws_led

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


def _acoustid_lookup_cached_outcome(file_path: str) -> ProviderResult:
    """AcoustID lookup through the file-level disk cache, as a typed result.

    The cache is keyed by resolved path + size + mtime_ns and never expires
    (a changed file gets a new key). Only real answers -- confirmed or
    no_result -- are cached; an outage, throttle, rejected key or timeout is
    returned as such and NOT cached, so it can never become a permanent
    "no match"."""
    path, cache_key = _audio_cache_file_identity(file_path)
    if not path or not cache_key:
        return ProviderResult("acoustid", ProviderOutcome.NO_RESULT, data=[], message="file not readable")
    cache_path = _ACOUSTID_FILE_CACHE_DIR / cache_key[:2] / f"{cache_key}.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, list):
                return ProviderResult("acoustid", ProviderOutcome.CONFIRMED if cached else ProviderOutcome.NO_RESULT,
                                      data=cached, from_cache=True, attempts=0)
        except Exception:
            pass

    result = acoustid_lookup_outcome(str(path))
    if result.outcome in (ProviderOutcome.CONFIRMED, ProviderOutcome.NO_RESULT):
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(result.data or []), encoding="utf-8")
        except Exception:
            pass
    return result


#: Fingerprint statuses for a lookup that never got an answer (D5/#252 NF-2).
#: "Not checked" is not "no recording": these are never NO_RESULT, never
#: cached, and never negative identity evidence.
ACOUSTID_FAILURE_MESSAGES = {
    "not_configured": "AcoustID not configured (set ACOUSTID_API_KEY); fingerprints were not checked.",
    "auth_failed": "AcoustID rejected the API key (check ACOUSTID_API_KEY); fingerprints were not checked.",
    "lookup_failed": "AcoustID lookup failed (service unavailable or rate limited); fingerprints were not checked.",
}


def acoustid_failure_status(result: ProviderResult) -> str:
    """"" for a real answer, else why no answer was obtained."""
    if result.answered:
        return ""
    if result.outcome == ProviderOutcome.NOT_CONFIGURED:
        return "not_configured"
    if result.outcome == ProviderOutcome.AUTHENTICATION_ERROR:
        return "auth_failed"
    return "lookup_failed"


def _acoustid_lookup_cached(file_path: str) -> List[Dict[str, Any]]:
    """AcoustID candidates for a file (cached answers only; see
    _acoustid_lookup_cached_outcome). Returns [] when there is no match AND
    when the provider could not be asked -- callers that must distinguish
    those use the _outcome variant."""
    return list(_acoustid_lookup_cached_outcome(file_path).data or [])


def _audio_identity_score(candidate: Dict[str, Any]) -> float:
    """0..1 display score; integer scores are percents (canonical rule)."""
    from backend.matching.track_alignment import acoustid_score_percent
    return round(acoustid_score_percent(candidate.get("score")) / 100.0, 3)


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
        if acoustid_candidates is not None:
            candidates = acoustid_candidates
        else:
            from backend.acoustid_service import ACOUSTID_FAILURE_MESSAGES, acoustid_failure_status  # self-import: tests AST-extract this function
            lookup = _acoustid_lookup_cached_outcome(str(path))
            failure = acoustid_failure_status(lookup)
            if failure:
                result.update({
                    "fingerprint_status": failure,
                    "acoustid_status": failure,
                    "decision_reason": ACOUSTID_FAILURE_MESSAGES[failure],
                    "ai_assessment": "Review required because the fingerprint could not be checked.",
                    "conflicts": [f"acoustid_{failure}"],
                })
                return result
            candidates = lookup.data
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
      "unverified" – no fingerprint available (fpcalc absent, API error, no
                     result), only hits below the canonical score floor, or
                     a top tier that is only partly the expected song

    MI-13: only the confident top tier counts (score >= ACOUSTID_MIN_SCORE,
    within the shared 3-point window of the best hit); "confirmed" needs
    every recording in that tier to be the expected song.
    """
    if not file_path:
        return "unverified"
    try:
        cands = _acoustid_lookup_cached(file_path)
    except Exception:
        return "unverified"
    from backend.acoustid_service import _acoustid_top_tier  # self-import: tests AST-extract this function
    tier = _acoustid_top_tier(cands)
    if not tier:
        return "unverified"

    def _agrees(c: Dict[str, Any]) -> bool:
        c_title = _s(c.get("title") or "").strip()
        c_artist = _s(c.get("artist") or "").strip()
        if not c_title:
            return False
        title_ok = _playlist_title_score(title, c_title) >= 0.78
        artist_ok = (not artist) or _playlist_artist_name_score(artist, c_artist) >= 0.72
        return title_ok and artist_ok

    agreeing = sum(1 for c in tier if _agrees(c))
    if agreeing == len(tier):
        return "confirmed"
    return "unverified" if agreeing else "mismatch"


def _acoustid_top_tier(cands: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """The hits that decide identity: recording-bearing hits at or above the
    canonical floor (ACOUSTID_MIN_SCORE) within 3 points of the best one --
    the same window backend.matching uses. [] when nothing clears the floor."""
    high = [c for c in (cands or []) if isinstance(c, dict) and _s(c.get("mb_trackid") or "").strip()
            and _hit_score(c) >= ACOUSTID_MIN_SCORE]
    if not high:
        return []
    top = max(_hit_score(c) for c in high)
    return [c for c in high if _hit_score(c) >= top - 3.0]


def _acoustid_confirmed_recording(cands: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """The single hit whose recording these candidates CONFIRM under the
    canonical rule (backend.matching.acoustid_evidence_from_hits: at or above
    the floor, no other recording within the ambiguity window), or None.
    MI-5/MI-6/MI-11: identity proofs use this, never "any top-5 hit"."""
    tier = _acoustid_top_tier(cands)
    if not tier:
        return None
    best = max(tier, key=_hit_score)
    rid = _s(best.get("mb_trackid") or "").strip().lower()
    if acoustid_evidence_from_hits(cands, rid).status != AcoustIDStatus.CONFIRMED:
        return None
    return best


def _confirmed_recording_ids(cands: Any) -> List[str]:
    best = _acoustid_confirmed_recording(cands if isinstance(cands, list) else [])
    return [_s(best.get("mb_trackid")).strip().lower()] if best else []


def _acoustid_fingerprint_ids(file_path: str, limit: int = 5) -> List[str]:
    """The MusicBrainz recording ID AcoustID CONFIRMS for a file, as a
    0- or 1-element list (MI-5).

    Empty when fpcalc is unavailable, the lookup fails, the file has no
    fingerprint, every hit is below the canonical score floor, or two
    recordings sit in the ambiguity window -- i.e. "no proof", never
    "proof of a different recording". ``limit`` is kept for call-site
    compatibility only.
    """
    if not file_path:
        return []
    try:
        cands = _acoustid_lookup_cached(file_path)
    except Exception:
        return []
    from backend.acoustid_service import _confirmed_recording_ids  # self-import: tests AST-extract this function
    return _confirmed_recording_ids(cands)


def _acoustid_cached_fingerprint_ids(file_path: str, limit: int = 5) -> Optional[List[str]]:
    """Recording IDs from the AcoustID file cache only -- never fingerprints
    or calls the AcoustID API. None when this exact file (path, size, mtime)
    has no cache entry, so read-only inventories can report "not cached"
    instead of spending API calls."""
    path, cache_key = _audio_cache_file_identity(file_path)
    if not path or not cache_key:
        return None
    cache_path = _ACOUSTID_FILE_CACHE_DIR / cache_key[:2] / f"{cache_key}.json"
    try:
        cands = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # MI-5: same confirmed-only rule as _acoustid_fingerprint_ids.
    from backend.acoustid_service import _confirmed_recording_ids  # self-import: tests AST-extract this function
    return _confirmed_recording_ids(cands)


def _acoustid_fingerprint_match(source_path: str, lib_path: str) -> Tuple[str, List[str], List[str]]:
    """Fingerprint-verify that two audio files are the same recording via AcoustID.

    Returns (shared_mb_trackid, source_recording_ids, lib_recording_ids).
    Each ID list holds only the recording AcoustID CONFIRMS for that file
    (_acoustid_fingerprint_ids, MI-5), so:
      shared set           -> both files confirm the same recording;
      both lists non-empty -> each file confirms a *different* recording
                              (callers treat that as "verified not a
                              duplicate", not "unknown");
      either list empty    -> no proof either way (weak, ambiguous or
                              unavailable fingerprint).
    """
    src_ids = _acoustid_fingerprint_ids(source_path)
    if not src_ids:
        return "", [], []
    lib_ids = _acoustid_fingerprint_ids(lib_path)
    for rid in src_ids:
        if rid in lib_ids:
            return rid, src_ids, lib_ids
    return "", src_ids, lib_ids


def _acoustid_hits_or_none(file_path: str) -> Optional[List[Dict[str, Any]]]:
    """AcoustID hits for a file, or None when no real answer was obtained
    (outage, throttle, missing file). [] means "looked up, no result"."""
    if not file_path or not Path(file_path).is_file():
        return None
    try:
        result = _acoustid_lookup_cached_outcome(file_path)
    except Exception:
        return None
    if result.outcome not in (ProviderOutcome.CONFIRMED, ProviderOutcome.NO_RESULT):
        return None
    return list(result.data or [])


def same_recording_proof(drop_path: str, keep_path: str, expected_recording_id: str = "") -> Dict[str, Any]:
    """Positive same-recording proof for two files (MI-3 containment).

    Both files must have a real AcoustID answer and BOTH must classify as
    CONFIRMED (acoustid_evidence_from_hits) for one recording: the keeper's
    embedded Recording ID when given, else the keeper's top hit. Anything
    else -- unavailable, no result, ambiguous, conflict -- is not proof.
    Returns {"proven": bool, "recording_id", "reason", "drop_status", "keep_status"}."""
    from backend.matching import acoustid_evidence_from_hits
    keep_hits = _acoustid_hits_or_none(keep_path)
    drop_hits = _acoustid_hits_or_none(drop_path)
    if keep_hits is None or drop_hits is None:
        return {"proven": False, "recording_id": "", "reason": "fingerprint_unavailable",
                "drop_status": "unavailable" if drop_hits is None else "", "keep_status":
                "unavailable" if keep_hits is None else ""}
    target = _s(expected_recording_id).strip().lower()
    if not target:
        scored = sorted(((_s(h.get("mb_trackid") or h.get("recording_id") or "").strip().lower(),
                          float(h.get("score") or 0)) for h in keep_hits), key=lambda r: -r[1])
        target = next((rid for rid, _ in scored if rid), "")
    if not target:
        return {"proven": False, "recording_id": "", "reason": "no_recording_for_keeper",
                "drop_status": "", "keep_status": "no_result"}
    keep_ev = acoustid_evidence_from_hits(keep_hits, target)
    drop_ev = acoustid_evidence_from_hits(drop_hits, target)
    proven = keep_ev.status == AcoustIDStatus.CONFIRMED and drop_ev.status == AcoustIDStatus.CONFIRMED
    return {"proven": proven, "recording_id": target if proven else "",
            "reason": "" if proven else "not_both_confirmed_for_one_recording",
            "drop_status": _s(getattr(drop_ev.status, "value", drop_ev.status)),
            "keep_status": _s(getattr(keep_ev.status, "value", keep_ev.status))}


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
# SEC-5 (ReDoS): only the whitespace-free core is a regex; the leading
# whitespace run and the trailing ``.*`` of the original
# ``\s*CORE.*`` pattern are applied by split_ws_led in linear time.
_FEAT_RE = re.compile(r'[\(\[]?(?:feat(?:uring)?\.?|ft\.?|with)\b', re.IGNORECASE)
_ARTIST_SPLIT_CORE_RE = re.compile(
    r'(?:/|,|\+|\b(?:ft\.?|feat\.?|featuring|with|x|and)\b|&)\s+',
    re.IGNORECASE,
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
    # SEC-5 (ReDoS): cap free text before the regexes; real names are far shorter.
    s = _normalize_name(s)[:1024]
    # Strip feat./ft./featuring suffix
    s = ''.join(split_ws_led(s, _FEAT_RE, to_eol=True)).strip().rstrip(',').strip()
    # Strip comma-listed collaborators (only when no '&' present — avoids
    # breaking "Earth, Wind & Fire" or "Crosby, Stills, Nash & Young")
    if ',' in s and '&' not in s:
        s = s.split(',')[0].strip()
    return s


# ── Dedup scan ────────────────────────────────────────────────────────────────

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
    A lookup with no answer returns acoustid_failure_status() instead
    (not_configured / auth_failed / lookup_failed), never NO_RESULT.
    """
    path = _album_item_abs_path(item.get("path", ""))
    if not path or not Path(path).exists():
        return {"status": AcoustIDStatus.UNAVAILABLE.value, "path": path}
    from backend.acoustid_service import acoustid_failure_status  # self-import: tests AST-extract this function
    lookup = _acoustid_lookup_cached_outcome(path)
    failure = acoustid_failure_status(lookup)
    if failure:
        # Not checked is not "no recording" (D5): never NO_RESULT.
        return {"status": failure, "path": path}
    cands = list(lookup.data or [])
    if not cands:
        return {"status": AcoustIDStatus.NO_RESULT.value}

    # MI-6: only a recording AcoustID CONFIRMS (canonical floor, no rival
    # recording in the ambiguity window) can confirm or contradict the
    # tracklist; weak or tied hits are AMBIGUOUS.
    mb_ids = {_s(t.get("mb_trackid")).strip().lower() for t in mb_tracks if t.get("mb_trackid")}
    from backend.acoustid_service import _acoustid_confirmed_recording  # self-import: tests AST-extract this function
    confirmed = _acoustid_confirmed_recording(cands)
    if confirmed is not None and _s(confirmed.get("mb_trackid")).strip().lower() in mb_ids:
        return {"status": AcoustIDStatus.CONFIRMED.value, "candidate": confirmed}

    from difflib import SequenceMatcher
    best_cand = confirmed or cands[0]
    cand_title = _album_track_norm(best_cand.get("title", ""))
    best_title_score = max(
        (SequenceMatcher(None, cand_title, t.get("title_norm", "")).ratio()
         for t in mb_tracks if cand_title and t.get("title_norm")),
        default=0.0,
    )
    if confirmed is not None and best_title_score < 0.72:
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

    Only a recording AcoustID CONFIRMS for a file counts (MI-11: canonical
    score floor, no rival recording in the ambiguity window). Returns True
    when at least one sampled file confirms a recording by a matching artist
    and none confirms a different artist; False when any sampled file
    confirms a different artist (merge would commingle two catalogs); None
    when no sampled file produced a confirmed recording (unverified --
    callers must not treat that as confirmation).
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

    from backend.acoustid_service import _acoustid_confirmed_recording  # self-import: tests AST-extract this function
    agreed = False
    for p in sample_paths:
        try:
            cands = _acoustid_lookup_cached(str(p))
        except Exception:
            continue
        confirmed = _acoustid_confirmed_recording(cands)
        c_artist = _s((confirmed or {}).get("artist") or "").strip()
        if not c_artist:
            continue
        if _playlist_artist_name_score(canonical_name, c_artist) < 0.72:
            return False
        agreed = True
    return True if agreed else None


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
    for part in split_ws_led(cleaned[:1024], _ARTIST_SPLIT_CORE_RE):
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
