"""Identity suggestion for one library item (POST /api/items/<iid>/ai-suggest).

Pipeline: read the item's tags, gather AcoustID/MusicBrainz/Discogs evidence,
optionally let the AI rank it, then validate what came back against that
evidence and shape the response. AI is optional and untrusted: only a recording
already in the candidate list is ever marked trusted; everything else is
downgraded and left for the user to review.
"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import backend.provider_boundary as provider_boundary
from backend import recording_review
from backend.acoustid_service import _acoustid_lookup_cached_outcome, acoustid_failure_status
from backend.ai_evidence_service import _enrich_track_ai_candidate, _item_ai_abs_path, _score_track_ai_candidate
from backend.ai_service import _classify_openai_error, _compact_track_ai_candidate, _track_ai_evidence_packet
from backend.app_runtime import _app_logger, _s
from backend.matching_contract import AiState
from backend.matching_service import _ai_api_key, _ai_model_and_endpoint
from backend.musicbrainz_service import _discogs_track_search
from backend.serializers import _format_duration
from helpers_mb import _MB_UUID_RE, _clean_for_mb, _fetch_mb_recording_details, _mb_recording_search

_NOISE_ARTIST_RE = re.compile(
    r'radio|station|channel|network|records|music|media|official|vevo|'
    r'entertainment|group|label|^various',
    re.I)
_YEAR_RE = re.compile(r'^((?:19|20)\d{2})')
# Fields MusicBrainz fills authoritatively (override AI guesses).
_MB_AUTHORITATIVE = {"artist", "mb_albumid", "mb_artistid", "track", "tracktotal", "disc", "disctotal",
                     "label", "genre", "album", "year"}


class _Evidence(NamedTuple):
    search_title: str
    search_artist: str
    acoustid_cands: List[Dict[str, Any]]
    acoustid_status: str
    acoustid_hits: Any
    mb_candidates: List[Dict[str, Any]]
    musicbrainz_unavailable: bool
    discogs_cands: List[Dict[str, Any]]


def _current_tags(item) -> Dict[str, Any]:
    item_path = _item_ai_abs_path(item)
    raw_year = str(item.year or "")
    # Normalise year: "20120923" -> "2012", "2012-09-23" -> "2012"
    clean_year = _YEAR_RE.match(raw_year)
    length = float(getattr(item, "length", 0) or 0)
    return {
        "title":       item.title       or "",
        "artist":      item.artist      or "",
        "album":       item.album       or "",
        "albumartist": item.albumartist or "",
        "year":        clean_year.group(1) if clean_year else raw_year,
        "genre":       _s(getattr(item, "genre", "")) or "",
        "track":       item.track       or "",
        "label":       _s(getattr(item, "label", "")) or "",
        "mb_trackid":  _s(getattr(item, "mb_trackid", "")) or "",
        "mb_albumid":  _s(getattr(item, "mb_albumid", "")) or "",
        "mb_releasegroupid": _s(getattr(item, "mb_releasegroupid", "")) or "",
        "filename": Path(item_path or _s(item.path)).name,
        "source_path": item_path,
        "duration_seconds": length,
        "duration": _format_duration(length),
    }


def _search_terms(current: Dict[str, Any]) -> Tuple[str, str]:
    """Title/artist to search with; splits "Artist - Title" when the artist tag is noise or absent."""
    # Strip leading track numbers: "01 - Title" -> "Title"
    stem_clean = re.sub(r'^\d+\s*[-_.]\s*', '', Path(current["filename"]).stem).strip()
    search_title = current["title"] or stem_clean
    search_artist = current["artist"] or current["albumartist"] or ""
    if " - " in search_title:
        candidate_artist, candidate_title = (p.strip() for p in search_title.split(" - ", 1))
        if not search_artist or _NOISE_ARTIST_RE.search(search_artist) or len(search_artist) > 30:
            search_title, search_artist = candidate_title, candidate_artist
    return _clean_for_mb(search_title, search_artist)


def _gather_evidence(iid: int, current: Dict[str, Any]) -> _Evidence:
    """Deterministic evidence; runs whether or not AI is configured."""
    item_path = current["source_path"]
    title, artist = _search_terms(current)

    # 1. AcoustID fingerprint (most reliable).
    # #252 NF-2: not asked (no key, rejected key, outage) is not "no match".
    acoustid_lookup = _acoustid_lookup_cached_outcome(item_path) if item_path else None
    acoustid_status = acoustid_failure_status(acoustid_lookup) if acoustid_lookup else ""
    acoustid_cands = list(acoustid_lookup.data or []) if acoustid_lookup else []

    # 2. MusicBrainz text search. An outage keeps the AcoustID evidence; the MB
    # part is reported unavailable, never as "no candidates".
    musicbrainz_unavailable = False
    try:
        mb_text_cands = _mb_recording_search(title, artist, limit=6)
        if not mb_text_cands and artist:  # broaden if nothing found
            mb_text_cands = _mb_recording_search(title, "", limit=6)
    except provider_boundary.ProviderError:
        musicbrainz_unavailable, mb_text_cands = True, []

    # 3. Discogs (supplemental genre / label info).
    discogs_cands = _discogs_track_search(title, artist, limit=3)

    # Merge AcoustID + MB, deduplicate by mb_trackid (candidate generation
    # only -- the canonical evaluator decides, with the full hit set).
    acoustid_hits = None if acoustid_status else recording_review.acoustid_hits_for(item_path, acoustid_cands)
    mb_candidates = recording_review.merge_recording_candidates(
        acoustid_cands, mb_text_cands, item_path=item_path,
        score_fn=lambda c: _score_track_ai_candidate(current, title, artist, current["filename"], c),
    )
    recording_review.enrich_and_index(
        mb_candidates,
        lambda c, hits: _enrich_track_ai_candidate(current, c, item_id=iid, acoustid_hits=hits),
        acoustid_hits,
        swallow_errors=True,
    )
    return _Evidence(title, artist, acoustid_cands, acoustid_status, acoustid_hits, mb_candidates,
                     musicbrainz_unavailable, discogs_cands)


def _build_prompt(current: Dict[str, Any], evidence: _Evidence) -> str:
    mb_section = ""
    if evidence.mb_candidates:
        lines = ["MusicBrainz/AcoustID candidates "
                 "(idx / match / score / title / artist / album / year / country / mb_trackid / source):"]
        for c in evidence.mb_candidates:
            ms = c.get("_match_score", {})
            lines.append(
                f"  [{c.get('candidate_index', -1)}] match={ms.get('total', 0):.2f} "
                f"mb={int(c.get('score') or 0):3d} {c['title']} — {c['artist']} / "
                f"{c['album']} ({c['year']}) {c.get('country','')} "
                f"[{c['mb_trackid']}] via {c.get('source','mb')}")
        mb_section = "\n\n" + "\n".join(lines)

    discogs_section = ""
    if evidence.discogs_cands:
        dlines = ["Discogs candidates (artist / album / year / genre / format):"]
        for d in evidence.discogs_cands:
            dlines.append(f"  {d['artist']} — {d['album']} ({d['year']}) "
                          f"[{d['genre']}] {d['format']}")
        discogs_section = "\n\n" + "\n".join(dlines)

    return (
        "You are a music metadata expert. A file was imported with incorrect or incomplete tags.\n\n"
        f"Filename: {current['filename']}\n"
        "Current (possibly wrong) tags:\n" +
        "\n".join(f"  {k}: {v or '(empty)'}" for k, v in current.items()) +
        mb_section + discogs_section +
        "\n\n"
        "RULES — apply strictly:\n"
        "1. If AcoustID fingerprint candidates are available, prefer them over text-only matches and only use MB recordings from that candidate list.\n"
        "   Do not silently override strong contradictory fingerprint evidence; return low confidence and explain the conflict.\n"
        "2. If no AcoustID candidates are found, fall back to MusicBrainz text search results.\n"
        "   If choosing a MusicBrainz candidate, copy its mb_trackid exactly; do not invent a recording ID.\n"
        "3. FILENAME is the next most reliable clue. Filenames often follow 'Artist - Title' or "
        "'NN - Artist - Title' patterns — extract the correct title and artist from it.\n"
        "4. YEAR: output only a 4-digit year (e.g. 2012, not 20120923 or 2012-09-23).\n"
        "5. ARTIST: use the correct performing artist, not a radio station, label, or 'Various Artists'.\n"
        "6. ALBUM: if the file belongs to a known album, fill it in. Otherwise leave empty.\n"
        "7. RELEASE PREFERENCE: prefer US releases (country=US). If no equally strong US match exists, use Worldwide (country=XW) before other countries.\n"
        "8. FORMAT: prefer CD or Digital Media recordings. Avoid vinyl if a digital version exists.\n"
        "9. CONFIDENCE — set one of:\n"
        "   high:   certain of artist, title, year, and album\n"
        "   medium: reasonably sure but one field is uncertain\n"
        "   low:    guessing based on limited info\n\n"
        "Return ONLY a valid JSON object with these keys "
        "(include only what you are confident about):\n"
        "  title — clean track title only (no artist prefix)\n"
        "  artist — performing artist using feat. notation: e.g. 'Young Money feat. Lil Wayne & Curren$y'\n"
        "  album, albumartist, year (4-digit integer), genre, track (integer), tracktotal (integer),\n"
        "  disc (integer), disctotal (integer), label,\n"
        "  mb_trackid (recording UUID), mb_albumid (release UUID), mb_artistid (artist UUID),\n"
        "  confidence (high|medium|low), reason (one sentence)\n\n"
        'Example: {"title":"Poppin\' Them Bottles","artist":"Young Money feat. Lil Wayne, Curren$y & Mack Maine",'
        '"album":"Dedication 2","albumartist":"Lil Wayne","year":2006,"genre":"Hip-Hop","track":4,'
        '"mb_trackid":"b5316c12-b617-4086-a107-312eccfd12e7",'
        '"confidence":"high","reason":"AcoustID fingerprint matches Lil Wayne Dedication 2 mixtape"}'
    )


def _ask_ai(prompt: str, api_key: str) -> Any:
    """The AI's parsed reply. Raises on any provider or parse failure."""
    model, endpoint = _ai_model_and_endpoint("gpt-4o")
    req = urllib.request.Request(
        endpoint,
        data=json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    with provider_boundary.opened("ai", req, timeout=30) as resp:
        data = json.loads(resp.read())
    return json.loads(data["choices"][0]["message"]["content"])


def _fallback_suggestion(current: Dict[str, Any], mb_candidates: List[Dict[str, Any]],
                         ai_unavailable_reason: str) -> Dict[str, Any]:
    """AI unavailable/failed: the top-ranked evidence candidate, instead of ok=False."""
    if mb_candidates:
        top = mb_candidates[0]
        top_score = float((top.get("_match_score") or {}).get("total", 0) or 0)
        return {
            "title": top.get("title", "") or current["title"],
            "artist": top.get("artist", "") or current["artist"],
            "album": top.get("album", "") or current["album"],
            "year": str(top.get("year") or current["year"] or ""),
            "genre": top.get("genre", "") or current["genre"],
            "label": top.get("label", "") or current["label"],
            "mb_trackid": top.get("mb_trackid", ""),
            "mb_albumid": top.get("mb_albumid", ""),
            "confidence": "medium" if top_score >= 0.75 else "low",
            "reason": f"Matched using MusicBrainz and AcoustID (AI unavailable: {ai_unavailable_reason}).",
        }
    return {
        "confidence": "low",
        "reason": f"No MusicBrainz/AcoustID candidates found, and AI is unavailable ({ai_unavailable_reason}).",
    }


def _normalize_fields(suggestions: Dict[str, Any]) -> None:
    for f in ("year", "track", "tracktotal", "disc", "disctotal"):
        if f in suggestions:
            suggestions[f] = str(suggestions[f])
    if suggestions.get("year"):
        year = _YEAR_RE.match(str(suggestions["year"]))
        if year:
            suggestions["year"] = year.group(1)
    if not suggestions.get("confidence"):
        suggestions["confidence"] = "medium"
    if not suggestions.get("reason"):
        suggestions["reason"] = ""


def _validate_identity(suggestions: Dict[str, Any], mb_candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Check the suggested recording against the evidence; returns the candidate it names.

    Only a recording in the candidate list is trusted. An ID outside it, or a
    malformed one, is downgraded and carries a warning for the reviewer.
    """
    mb_trackid = (suggestions.get("mb_trackid") or "").strip()
    selected_candidate = None
    candidate_index = -1
    if mb_trackid:
        target_trackid = mb_trackid.strip().lower()
        for c in mb_candidates:
            if _s(c.get("mb_trackid", "")).strip().lower() == target_trackid:
                selected_candidate = c
                try:
                    candidate_index = int(c.get("candidate_index", -1))
                except Exception:
                    candidate_index = -1
                break
    suggestions["candidate_index"] = candidate_index
    suggestions["mb_candidate_valid"] = bool(selected_candidate)
    suggestions["mb_trusted"] = bool(selected_candidate)
    if mb_trackid and mb_candidates and not selected_candidate:
        if _s(suggestions.get("confidence")).lower() == "high":
            suggestions["confidence"] = "low"
        reason = _s(suggestions.get("reason")).strip()
        guard = "AI returned a MusicBrainz recording ID that was not in the AcoustID/MusicBrainz candidate list; review before applying."
        suggestions["reason"] = f"{guard} {reason}".strip()
    suggestions["mb_valid"] = bool(_MB_UUID_RE.match(mb_trackid))
    if not suggestions["mb_valid"]:
        suggestions["mb_trusted"] = False
        suggestions["mb_apply_warning"] = (
            "No validated MusicBrainz recording ID was returned; review text tags manually before applying."
        )
        if _s(suggestions.get("confidence")).lower() == "high":
            suggestions["confidence"] = "medium"
        reason = _s(suggestions.get("reason")).strip()
        guard = suggestions["mb_apply_warning"]
        if guard not in reason:
            suggestions["reason"] = f"{guard} {reason}".strip()
    return selected_candidate


def _fill_from_musicbrainz(suggestions: Dict[str, Any], current: Dict[str, Any], selected_candidate: Dict[str, Any],
                           evidence: _Evidence, ai_state: AiState) -> None:
    """Fill remaining fields from the selected recording; MusicBrainz overrides AI guesses."""
    mb_trackid = (suggestions.get("mb_trackid") or "").strip()
    try:
        preferred_albumid = (
            (suggestions.get("mb_albumid") or "").strip()
            or _s(selected_candidate.get("mb_albumid", "")).strip()
            or next(iter(selected_candidate.get("mb_albumids") or []), "")
        )
        details = _fetch_mb_recording_details(mb_trackid, preferred_albumid)
        _enrich_track_ai_candidate(current, selected_candidate, details,
                                   ai_state=ai_state, acoustid_hits=evidence.acoustid_hits)
        for k, v in details.items():
            if v and (k in _MB_AUTHORITATIVE or not suggestions.get(k)):
                suggestions[k] = v
    except Exception:
        pass


def _attach_candidate_evidence(suggestions: Dict[str, Any], selected_candidate: Optional[Dict[str, Any]],
                               mb_candidates: List[Dict[str, Any]]) -> None:
    if selected_candidate:
        packet = _compact_track_ai_candidate(selected_candidate)
        suggestions["candidate_type"] = "recording"
        suggestions["candidate_evidence"] = packet
        suggestions["selected_recording_candidate"] = packet
        suggestions["selected_release"] = packet.get("selected_release") or {}
        suggestions["linked_releases"] = packet.get("linked_releases") or []
        suggestions["conflicts"] = packet.get("conflicts") or []
        suggestions["recommended_action"] = packet.get("recommended_action") or ""
        suggestions["requires_confirmation"] = bool(packet.get("requires_confirmation"))
        suggestions["safety_result"] = packet.get("safety_result") or ""
        suggestions["confidence_score"] = packet.get("confidence_score")
        suggestions["match_method"] = packet.get("match_method") or packet.get("source")
    suggestions["recording_candidates"] = [_compact_track_ai_candidate(c) for c in mb_candidates[:8]]
    suggestions["missing_id_type"] = "Recording ID"
    if _MB_UUID_RE.match(suggestions.get("mb_albumid") or ""):
        suggestions["mb_album_url"] = f"https://musicbrainz.org/release/{suggestions['mb_albumid']}"


def _stamp_ai_status(suggestions: Dict[str, Any], ai_available: bool, ai_configured: bool, ai_contributed: bool,
                     ai_unavailable_reason: str, mb_candidates: List[Dict[str, Any]]) -> None:
    suggestions["ai_available"] = ai_available
    suggestions["ai_unavailable_reason"] = ai_unavailable_reason
    # When AI did not actually contribute (not configured, or its key was
    # rejected/unreachable -- classified by _classify_openai_error), the
    # confidence is a MusicBrainz/AcoustID (or absent) verdict, NOT an AI one.
    # A STRUCTURED ai_status (configured vs validly reachable, with the provider
    # boundary's last classified outcome, which knows a 401-rejected key) plus
    # an explicit confidence_source lets the UI render a provider-status chip
    # instead of a misleading "AI low". matching_decision.ai is left untouched
    # (its boundary default is correct by design).
    health = (provider_boundary.provider_health() or {}).get("ai") or {}
    suggestions["ai_status"] = {
        "configured": bool(ai_configured),
        "attempted": bool(ai_configured),
        "contributed": bool(ai_contributed),
        "available": bool(ai_contributed),
        "unavailable_reason": "" if ai_contributed else ai_unavailable_reason,
        "last_outcome": health.get("last_outcome"),
        "last_status_code": health.get("last_status_code"),
    }
    suggestions["confidence_source"] = "ai" if ai_contributed else ("musicbrainz" if mb_candidates else "none")


def suggest_for_item(item, iid: int) -> Dict[str, Any]:
    """The JSON body of POST /api/items/<iid>/ai-suggest."""
    # AI is optional: the evidence below is gathered unconditionally, so a
    # missing/invalid key still yields a match.
    api_key = _ai_api_key()
    ai_available = bool(api_key)
    ai_configured = ai_available
    current = _current_tags(item)
    evidence = _gather_evidence(iid, current)
    mb_candidates = evidence.mb_candidates
    prompt = _build_prompt(current, evidence)

    ai_unavailable_reason = "" if ai_available else "OPENAI_API_KEY not configured"
    suggestions: Any = None
    if ai_available:
        try:
            suggestions = _ask_ai(prompt, api_key)
        except Exception as exc:
            ai_available = False
            ai_unavailable_reason = _classify_openai_error(exc)

    # Captured before the fallback overwrites `suggestions` with a mechanically
    # derived (non-AI) guess, so the matching contract never claims AI
    # contributed when it did not actually respond.
    ai_contributed = suggestions is not None
    if suggestions is None:
        suggestions = _fallback_suggestion(current, mb_candidates, ai_unavailable_reason)

    try:
        _normalize_fields(suggestions)
        selected_candidate = _validate_identity(suggestions, mb_candidates)
        if suggestions["mb_valid"]:
            suggestions["mb_url"] = f"https://musicbrainz.org/recording/{(suggestions.get('mb_trackid') or '').strip()}"
            if selected_candidate:
                # AI has been attempted and the fallback has run, so this is the
                # one place a real AiState can be reported.
                ai_state = AiState(
                    state_known=True,
                    configured=ai_configured,
                    attempted=ai_configured,
                    available=ai_contributed,
                    unavailability_reason=ai_unavailable_reason,
                    contribution=(
                        {
                            "mb_trackid": suggestions.get("mb_trackid", ""),
                            "confidence": suggestions.get("confidence", ""),
                            "reason": suggestions.get("reason", ""),
                        }
                        if ai_contributed else {}
                    ),
                )
                _fill_from_musicbrainz(suggestions, current, selected_candidate, evidence, ai_state)
        _attach_candidate_evidence(suggestions, selected_candidate, mb_candidates)
        packet = _track_ai_evidence_packet(
            iid,
            filename=current["filename"],
            current=current,
            search_title=evidence.search_title,
            search_artist=evidence.search_artist,
            suggestions=suggestions,
            selected_candidate=selected_candidate,
            candidates=mb_candidates,
            acoustid_candidates=evidence.acoustid_cands,
            discogs_candidates=evidence.discogs_cands,
            acoustid_status=evidence.acoustid_status,
        )
        _stamp_ai_status(suggestions, ai_available, ai_configured, ai_contributed, ai_unavailable_reason, mb_candidates)
        suggestions["evidence"] = packet
        return {"ok": True, "suggestions": suggestions,
                "mb_candidates": mb_candidates,
                "selected_candidate": _compact_track_ai_candidate(selected_candidate) if selected_candidate else {},
                "evidence": packet,
                "acoustid_candidates": evidence.acoustid_cands,
                "discogs_candidates": evidence.discogs_cands,
                "musicbrainz_unavailable": evidence.musicbrainz_unavailable,
                "acoustid_unavailable": bool(evidence.acoustid_status),
                "acoustid_status": evidence.acoustid_status}
    except Exception as exc:
        _app_logger.warning("AI track suggestion failed: %s", type(exc).__name__)
        return {"ok": False, "error": "Could not generate suggestions."}
