"""AI evidence helpers: folder-name evidence parsing and track-level AI candidate scoring (ARCH-001).

Candidate generation and display only; final decisions stay in backend.matching.
"""

from __future__ import annotations

import backend.provider_boundary as provider_boundary
import json, re
import urllib.error
from backend.matching import normalize_track_title_for_matching, similarity as _canonical_similarity
from pathlib import Path
from typing import Any, Dict, List, Optional
from backend.app_runtime import MUSIC_ROOT, _s
from backend.matching_contract import AiState, build_recording_matching_decision, compute_decision_version
from backend.title_normalize import restore_time_colon_title as _restore_time_colon_title
from helpers_mb import _fetch_mb_recording_details
from backend.matching_service import _ai_model_and_endpoint, _album_track_title_variants

# ── ARCH-001 extracted code ──


def _track_ai_similarity(left: str, right: str) -> float:
    """Canonical string similarity wrapper for AI suggestions."""
    return _canonical_similarity(left, right)


_AI_EVIDENCE_FMT_SEG_RE = re.compile(
    r'^(?:WEB|WEBRIP|FLAC|ALAC|MP3|AAC|OGG|OPUS|CD|SACD|DSD|MQA|HDTracks|'
    r'\d{1,2}BIT|\d{1,2}CD|\d{2,4}[Kk][Hh][Zz]|\d{3,4})$', re.I
)


_AI_EVIDENCE_DISC_FOLDER_RE = re.compile(r'^(?:disc|cd|disk)\s*0*\d{1,2}$', re.I)


_AI_EVIDENCE_SCENE_DROP_SEG_RE = re.compile(
    r'^(?:READNFO|NFOFIX|PROPER|REPACK|RERIP|REMASTER|REMASTERED|BONUS|'
    r'WEBRIP|SCENE|ALBUM|RELEASE|TITLE)$',
    re.I,
)


def _ai_evidence_clean_segment(value: str) -> str:
    text = _s(value).replace("_", " ").strip()
    # Strip MB stamp artifacts: {uuid}, {Album MbId}, {Track ArtistMbId}, (uuid)
    text = re.sub(r"\([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\)", "", text, flags=re.I)
    text = re.sub(r"\{[^{}]*\}", "", text)
    text = re.sub(
        r"[\[\(][^\]\)]*(?:WEB|FLAC|MP3|ALAC|AAC|OGG|OPUS|CD|SACD|DSD|MQA|"
        r"HDTracks|\d{1,2}BIT|\d{1,2}CD|(?:19|20)\d{2})[^\]\)]*[\]\)]",
        " ",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\b(?:READNFO|NFOFIX|PROPER|REPACK|RERIP|WEBRIP|"
        r"\d{1,2}BIT|\d{1,2}CD|WEB|FLAC|ALAC|MP3|AAC|OGG|OPUS|SACD|DSD|MQA|HDTracks)\b",
        " ",
        text,
        flags=re.I,
    )
    text = " ".join(text.split()).strip(" -_.")
    if text.isupper() and len(text) > 3:
        text = text.title()
    return text


def _ai_evidence_extract_year(value: str) -> tuple[str, str]:
    text = _s(value).strip()
    year = ""
    m = re.search(r"[\(\[]((?:19|20)\d{2})[\)\]]", text)
    if m:
        year = m.group(1)
        text = re.sub(r"\s*[\(\[]" + year + r"[\)\]]", "", text).strip()
    m = re.match(r"^((?:19|20)\d{2})\s*[-–]\s*(.+)$", text)
    if m:
        year = year or m.group(1)
        text = m.group(2).strip()
    m = re.search(r"(?:^|\s)((?:19|20)\d{2})$", text)
    if m:
        year = year or m.group(1)
        text = text[:m.start()].strip(" -_")
    return _restore_time_colon_title(" ".join(text.split()).strip()), year


def _ai_evidence_clean_artist_guess(value: str) -> str:
    # SEC-5 (ReDoS): cap free text (1024 chars) before the regexes below.
    text = _ai_evidence_clean_segment(_s(value)[:1024])
    text = re.sub(r"(?<!\s)\s+\bin\s+mono\b$", "", text, flags=re.I).strip()
    text, _ = _ai_evidence_extract_year(text)
    return text


def _ai_evidence_weak_artist_guess(value: str) -> bool:
    text = _s(value)
    if not text:
        return True
    low = text.casefold()
    if _AI_EVIDENCE_DISC_FOLDER_RE.match(text):
        return True
    if re.search(r"(?:19|20)\d{2}", text):
        return True
    if any(token in low for token in ("flac", "mp3", "web", "mono [", "lossless", "disc ")):
        return True
    return False


def _ai_evidence_weak_album_guess(value: str, *, disc_context: bool = False) -> bool:
    text = _s(value).strip()
    if not text or disc_context or _AI_EVIDENCE_DISC_FOLDER_RE.match(text):
        return True
    low = text.casefold()
    if any(token in low for token in ("readnfo", "nfofix", "proper", "repack", "flac", " web ", "16bit", "24bit")):
        return True
    return bool(" " not in text and re.match(r"^[a-z0-9_.-]+$", text))


def _ai_evidence_scene_guess(folder_name: str) -> tuple[str, str, str]:
    raw = _s(folder_name)[:1024].replace("_", " ").strip()
    if not raw:
        return "", "", ""
    raw, year = _ai_evidence_extract_year(raw)
    parts = [p.strip() for p in re.split(r"(?:(?<!\s)\s+)?-\s*", raw) if p.strip()]
    cleaned: List[str] = []
    for idx, part in enumerate(parts):
        seg = _ai_evidence_clean_segment(part)
        if not seg:
            continue
        if re.fullmatch(r"(?:19|20)\d{2}", seg):
            year = year or seg
            continue
        if idx == 0 and re.fullmatch(r"\d{2,4}", seg):
            # A purely-numeric first segment is almost always the artist
            # position (e.g. "311", "112"), never a bitrate/quality marker —
            # the FMT drop rule below would otherwise eat it.
            cleaned.append(seg)
            continue
        if _AI_EVIDENCE_FMT_SEG_RE.match(seg) or _AI_EVIDENCE_SCENE_DROP_SEG_RE.match(seg):
            continue
        if len(seg) <= 9 and " " not in seg and re.search(r"[A-Z]{2,}", seg):
            continue
        seg, seg_year = _ai_evidence_extract_year(seg)
        year = year or seg_year
        if seg:
            cleaned.append(seg)
    if len(cleaned) >= 2:
        return _ai_evidence_clean_artist_guess(cleaned[0]), " ".join(cleaned[1:]).strip(), year
    if cleaned:
        return "", cleaned[0], year
    return "", "", year


def _item_ai_abs_path(item) -> str:
    path_text = _s(getattr(item, "path", "")).strip()
    if path_text and not Path(path_text).is_absolute():
        path_text = str(MUSIC_ROOT / path_text)
    return path_text


def _track_ai_norm(value: str) -> str:
    """Canonical track title normalization wrapper for AI suggestions."""
    return normalize_track_title_for_matching(value)


def _score_track_ai_candidate(current: Dict[str, Any], search_title: str,
                              search_artist: str, filename: str,
                              candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Rank AI track candidates for candidate generation / prompt ordering only.

    This function computes an initial heuristic score for ordering candidates.
    It is CANDIDATE_GENERATION_ONLY and does NOT authorize identity or actions.
    Final identity decisions and safety gates are governed exclusively by
    build_recording_matching_decision.
    """
    try:
        title_variants = _album_track_title_variants(
            current.get("title") or search_title or filename,
            filename,
        )
    except Exception:
        title_variants = [_track_ai_norm(current.get("title") or search_title or filename)]
    cand_title = _s(candidate.get("title", ""))
    cand_artist = _s(candidate.get("artist", ""))
    cand_album = _s(candidate.get("album", ""))

    title_score = max(
        (_track_ai_similarity(v, cand_title) for v in title_variants if v),
        default=_track_ai_similarity(search_title, cand_title),
    )
    artist_score = _track_ai_similarity(search_artist or current.get("artist", ""), cand_artist)
    album_score = _track_ai_similarity(current.get("album", ""), cand_album)

    year_score = 0.5
    cur_year = _s(current.get("year", ""))
    cand_year = _s(candidate.get("year", ""))
    if cur_year[:4].isdigit() and cand_year[:4].isdigit():
        delta = abs(int(cur_year[:4]) - int(cand_year[:4]))
        year_score = 1.0 if delta == 0 else (0.80 if delta == 1 else (0.55 if delta <= 3 else 0.0))

    mb_score = max(0.0, min(1.0, float(candidate.get("score") or 0) / 100.0))
    source = _s(candidate.get("source") or "mb").lower()
    acoustid_bonus = 0.22 if source == "acoustid" else 0.0
    total = (
        title_score * 0.38
        + artist_score * 0.27
        + album_score * 0.10
        + year_score * 0.05
        + mb_score * 0.10
        + acoustid_bonus
    )
    return {
        "title_score": round(title_score, 3),
        "artist_score": round(artist_score, 3),
        "album_score": round(album_score, 3),
        "year_score": round(year_score, 3),
        "mb_score": round(mb_score, 3),
        "source": source,
        "acoustid_bonus": acoustid_bonus,
        "total": round(min(1.0, max(0.0, total)), 4),
    }


def _track_ai_year(value: Any) -> str:
    text = _s(value).strip()
    m = re.match(r"^((?:19|20)\d{2})", text)
    return m.group(1) if m else ""


def _track_ai_release_match_score(current: Dict[str, Any], release: Dict[str, Any]) -> Dict[str, Any]:
    local_album = _s(current.get("album") or "")
    local_artist = _s(current.get("albumartist") or current.get("artist") or "")
    local_year = _track_ai_year(current.get("year"))
    release_album = _s(release.get("album") or "")
    release_artist = _s(release.get("artist") or "")
    release_year = _track_ai_year(release.get("year") or release.get("date"))
    album_score = _track_ai_similarity(local_album, release_album) if local_album and release_album else 0.0
    artist_score = _track_ai_similarity(local_artist, release_artist) if local_artist and release_artist else 0.0
    year_match = bool(local_year and release_year and local_year == release_year)
    year_delta = 99
    if local_year and release_year:
        try:
            year_delta = abs(int(local_year) - int(release_year))
        except Exception:
            year_delta = 99
    year_score = 1.0 if year_match else (0.75 if year_delta == 1 else (0.45 if year_delta <= 3 else 0.0))
    total = album_score * 0.48 + artist_score * 0.24 + year_score * 0.28
    return {
        "album_score": round(album_score, 3),
        "artist_score": round(artist_score, 3),
        "year_score": round(year_score, 3),
        "year_match": year_match,
        "year_delta": year_delta if year_delta != 99 else None,
        "total": round(total, 4),
    }


def _track_ai_best_linked_release(current: Dict[str, Any], candidate: Dict[str, Any], details: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    linked = details.get("linked_releases") or candidate.get("linked_releases") or []
    if not linked:
        selected = details.get("selected_release") or candidate.get("selected_release") or {}
        return selected if isinstance(selected, dict) and selected else None
    preferred_albumid = _s(candidate.get("mb_albumid") or details.get("mb_albumid") or "").strip().lower()

    def _sort_key(release: Dict[str, Any]):
        match = _track_ai_release_match_score(current, release)
        preferred = 1 if preferred_albumid and _s(release.get("mb_albumid")).strip().lower() == preferred_albumid else 0
        primary = _s(release.get("release_group_primary_type")).casefold()
        albumish = 1 if primary == "album" else (0.5 if primary == "ep" else 0)
        country = _s(release.get("country")).upper()
        country_rank = 1 if country in {"US", "XW"} else 0
        return (match["total"], match["album_score"], 1 if match["year_match"] else 0, preferred, albumish, country_rank)

    ranked = sorted((r for r in linked if isinstance(r, dict)), key=_sort_key, reverse=True)
    candidate["linked_releases"] = ranked
    for release in ranked:
        release["local_match"] = _track_ai_release_match_score(current, release)
    return ranked[0] if ranked else None


def _enrich_track_ai_candidate(current: Dict[str, Any], candidate: Dict[str, Any], details: Optional[Dict[str, Any]] = None,
                               *, ai_state: Optional[AiState] = None, item_id: Optional[int] = None,
                               acoustid_hits: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    details = details or {}
    mb_trackid = _s(candidate.get("mb_trackid") or details.get("recording_id") or "").strip().lower()
    if mb_trackid and not details:
        preferred_albumid = _s(candidate.get("mb_albumid") or next(iter(candidate.get("mb_albumids") or []), "")).strip()
        details = _fetch_mb_recording_details(mb_trackid, preferred_albumid)

    selected_release = _track_ai_best_linked_release(current, candidate, details) or {}
    linked_releases = candidate.get("linked_releases") or details.get("linked_releases") or []
    matching_result = build_recording_matching_decision(
        current=current,
        candidate=candidate,
        details=details,
        selected_release=selected_release,
        linked_releases=linked_releases,
        ai_state=ai_state,
        similarity_fn=_track_ai_similarity,
        acoustid_hits=acoustid_hits,
    )
    candidate.update(matching_result.to_review_recording_candidate())
    if item_id is not None:
        # decision_version proves later that a submitted attach request saw
        # this exact decision -- it never grants authority by itself.
        candidate["decision_version"] = compute_decision_version(item_id, matching_result)
    return candidate


def _ai_suggest_genre(albumartist: str, album: str, year, api_key: str, log: list) -> str:
    """Ask OpenAI for the most accurate genre for an album. Returns '' on failure."""
    prompt = (
        f"What is the single most accurate music genre tag for this album?\n"
        f"Artist: {albumartist}\nAlbum: {album}\nYear: {year or 'unknown'}\n\n"
        "Reply with ONLY the genre name — e.g. 'Hip-Hop', 'Jazz', 'Electronic', "
        "'Alternative Rock', 'R&B'. Use standard MusicBrainz/Last.fm genre names. "
        "No explanation, no punctuation beyond the genre name itself."
    )
    _ai_model, _ai_endpoint = _ai_model_and_endpoint("gpt-4o-mini")
    body = json.dumps({
        "model": _ai_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 16,
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        _ai_endpoint,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with provider_boundary.opened("ai", req, timeout=15) as r:
            data = json.loads(r.read())
        genre = data["choices"][0]["message"]["content"].strip().strip('"').strip("'")
        return genre
    except Exception as ex:
        log.append(f"  AI genre error ({albumartist} – {album}): {ex}")
        return ""
