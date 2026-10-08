"""AI-assisted identification: evidence, batch queue and decisions (ARCH-001).
"""

from __future__ import annotations

import backend.provider_boundary as provider_boundary
import hashlib, json, os, re, threading, time, uuid
import urllib.error
from backend.matching import similarity as _canonical_similarity
from pathlib import Path
from typing import Any, Dict, List, Optional
from backend.app_runtime import _app_logger, AUDIO_EXT, MUSIC_ROOT, WEB_MANAGER_DATA_DIR, _AI_BATCH_MEDIUM_RATIO, _AI_BATCH_MIN_CONF, _AI_CONF_ORDER, _AI_REPAIR_MIN_CONF, _AI_USE_CASE_THRESHOLDS, _s
from backend.library_service import _apply_artist_folder_groups, _build_folder_evidence, _preserve_torrent_source_path, _scan_artist_folder_groups
from backend.pending_review_store import _add_to_pending, _remove_pending_review_for_path
from backend.playlist_service import _music_format_preferences
from backend.ai_batch_state_service import _AI_BATCH_FAILED_FOLDER_STATUSES, _AI_BATCH_IMPORTED_FOLDER_STATUSES, _AI_BATCH_MAX_AI_WORKERS, _AI_BATCH_POLICY_WARNING_STATUSES, _AI_BATCH_REPLACEMENT_FOLDER_STATUSES, _AI_BATCH_RETRYABLE_FOLDER_STATUSES, _AI_BATCH_REVIEW_FOLDER_STATUSES, _AI_BATCH_SKIPPED_FOLDER_STATUSES, _AI_BATCH_TERMINAL_STATUSES, _AI_BATCH_UNFINISHED_FOLDER_STATUSES, _MUSIC_FORMAT_POLICY_HANDLED_MESSAGE, _ai_batch_active_workers, _ai_batch_commit, _ai_batch_effective_folder_status, _ai_batch_mark_folder, _ai_batch_public_state, _ai_batch_recalculate_batch_state, _ai_batch_state_lock, _ai_batch_terminal_summary, _ai_batch_worker_lock, _ai_batch_worker_registered, _ai_batch_write_state, _get_ai_batch_store, _is_music_format_policy_handled_error, _music_format_policy_review_note
from backend.import_reconciliation_service import _ai_batch_reconcile_state
from backend.app_runtime import _path_is_under
from backend.audio_preferences import mark_needs_replacement as _mark_music_format_needs_replacement, validate_audio_tree as _validate_audio_tree_preferences, handle_rejected_download as _handle_rejected_audio_download
from helpers_mb import _mb_release_search, _fetch_mb_release_candidate
from backend.beets_adapter import lib, BeetsError, BeetsUnavailableError, BeetsAuthError
import backend.composite_workflows as composite_workflows
from backend.acoustid_service import _acoustid_lookup_cached, _acoustid_lookup_cached_outcome, _acoustid_multi_file, acoustid_failure_status, _album_track_norm, _audio_identity_score, _playlist_artist_name_score, _playlist_title_score
from backend.artwork_service import _fetch_artwork_after_retag
from backend.slskd_service import _normalise_wanted_tracks, _slskd_file_wanted_match_score
from backend.matching_service import _ai_api_key, _ai_model_and_endpoint, _album_preflight_folder, _best_album_track_match, _compact_preflight, _fetch_mb_release_tracklist, _folder_release_preflight, _invalidate_lib_cache, _preflight_match_ratio, _preflight_oversized_subset_complete, _preflight_tracklist_gate_ok
from backend.app_runtime import jobs
from backend.musicbrainz_service import _artist_folder_key, _discogs_release_fallback_candidate, _mb_release_search_by_folder_tracks
from backend.serializers import _compact_mb_candidate, _resolve_import_review_source_path
from backend.pending_review_store import _import_review_folder_signature, _pending_review_has_path, _pending_review_path_key, _pending_review_path_set
from backend.plex_service import _trigger_plex_refresh

# ── ARCH-001 extracted code ──


def _ai_thresholds_for(use_case: str) -> Dict[str, Any]:
    """Return a copy of the threshold settings used for this decision type."""
    return dict(_AI_USE_CASE_THRESHOLDS.get(use_case) or _AI_USE_CASE_THRESHOLDS["fresh_import"])


def _music_format_policy_rejection_error(rejected_count: int,
                                         handled_results: Optional[List[Dict[str, Any]]] = None,
                                         prefs: Optional[Dict[str, Any]] = None) -> str:
    results = list(handled_results or [])
    try:
        count = int(rejected_count or len(results) or 0)
    except Exception:
        count = len(results)
    handling = _s((prefs or {}).get("rejected_download_handling") or "").strip().casefold()
    action = "deleted" if handling == "delete" else "quarantined"
    kept = sum(1 for result in results if (result or {}).get("handling") == "kept")
    failed = sum(1 for result in results if (result or {}).get("handling") != "kept"
                 and ((result or {}).get("error") or not (result or {}).get("removed")))
    if kept:
        handled = (f"{max(count - kept - failed, 0)}/{count} rejected file(s) were {action}; "
                   f"{kept} left in place (seeded torrent source or library file)")
        if failed:
            handled += f"; {failed} need manual cleanup"
    elif count <= 0:
        handled = f"Rejected files were {action}"
    elif failed:
        handled = f"{max(count - failed, 0)}/{count} rejected file(s) were {action}; {failed} need manual cleanup"
    elif count == 1:
        handled = f"1 rejected file was {action}"
    else:
        handled = f"{count} rejected files were {action}"
    return (
        f"{_MUSIC_FORMAT_POLICY_HANDLED_MESSAGE} {handled}. "
        "Import stopped; choose another source or update Music Format Preferences before retrying."
    )


def _validate_import_source_audio(path_value: str, log: list, *, reject_downloads: bool = True) -> Dict[str, Any]:
    prefs = _music_format_preferences()
    root = Path(path_value)
    try:
        root_is_library = _path_is_under(root.resolve(strict=False), MUSIC_ROOT.resolve(strict=False))
    except Exception:
        root_is_library = False
    report = _validate_audio_tree_preferences(path_value, prefs, AUDIO_EXT)
    for row in report.get("accepted") or []:
        msg = row.get("message") or "Accepted: audio matches Music Format Preferences"
        log.append(f"  [audio] {msg}")
    rejected = list(report.get("rejected") or [])
    handled_results: List[Dict[str, Any]] = []
    if not rejected:
        return report
    for row in rejected:
        msg = row.get("message") or "Rejected download: audio does not match Music Format Preferences"
        log.append(f"  [audio] {msg}: {Path(row.get('path') or '').name}")
        if reject_downloads and not root_is_library and row.get("path"):
            handled_results.append(_handle_rejected_audio_download(row["path"], prefs, log=log))
    if root_is_library:
        _mark_music_format_needs_replacement([
            {
                "path": row.get("path"),
                "status": "Needs replacement",
                "reason": "; ".join(row.get("reasons") or ["does not match Music Format Preferences"]),
                "audio": row.get("properties") or {},
                "queued_retry": True,
            }
            for row in rejected
        ])
        raise RuntimeError(
            "Existing library audio does not match Music Format Preferences. "
            "Current files were kept and marked Needs replacement."
        )
    raise RuntimeError(
        _music_format_policy_rejection_error(len(rejected), handled_results, prefs)
    )


def _ai_match_evidence_packet(use_case: str, *, folder_path: str = "",
                              suggestion: Optional[Dict[str, Any]] = None,
                              folder_evidence: Optional[Dict[str, Any]] = None,
                              selected_candidate: Optional[Dict[str, Any]] = None,
                              candidates: Optional[List[Dict[str, Any]]] = None,
                              preflight: Optional[Dict[str, Any]] = None,
                              wanted_tracks: Optional[List[Dict[str, Any]]] = None,
                              reason: str = "") -> Dict[str, Any]:
    """Compact, JSON-safe evidence for Review UI and decision history."""
    suggestion = suggestion or {}
    folder_evidence = folder_evidence or {}
    top_candidates = [_compact_mb_candidate(c) for c in (candidates or [])[:5]]
    try:
        candidate_index = int(suggestion.get("candidate_index", -1))
    except Exception:
        candidate_index = -1
    return {
        "use_case": use_case,
        "created_at": int(time.time()),
        "thresholds": _ai_thresholds_for(use_case),
        "confidence": suggestion.get("confidence", ""),
        "reason": reason or suggestion.get("reason", ""),
        "candidate_index": candidate_index,
        "mb_albumid": suggestion.get("mb_albumid", ""),
        "mb_valid": bool(suggestion.get("mb_valid")),
        "folder": {
            "path": folder_path or folder_evidence.get("folder_path", ""),
            "guessed_artist": folder_evidence.get("guessed_artist", ""),
            "guessed_album": folder_evidence.get("guessed_album", ""),
            "guessed_year": folder_evidence.get("guessed_year", ""),
            "track_count": int(folder_evidence.get("folder_track_count") or 0),
            "nested_audio_count": int(folder_evidence.get("nested_audio_count") or 0),
            "track_titles": (folder_evidence.get("track_titles") or [])[:12],
            "filenames": (folder_evidence.get("filenames") or [])[:12],
        },
        "selected_candidate": _compact_mb_candidate(selected_candidate) if selected_candidate else {},
        "top_candidates": top_candidates,
        "preflight": _compact_preflight(preflight),
        "fingerprint": {
            "status": "matched" if preflight and int(preflight.get("acoustid_target_hits") or 0) else "no_result",
            "acoustid_mismatch": bool((preflight or {}).get("acoustid_mismatch")),
            "target_hits": int((preflight or {}).get("acoustid_target_hits") or 0),
            "top_release": _s((preflight or {}).get("acoustid_top_release") or ""),
        },
        "wanted_tracks": _normalise_wanted_tracks(wanted_tracks)[:20],
    }


def _ai_conf_at_least(confidence: str, required: str) -> bool:
    """Return whether an AI confidence label satisfies a named threshold."""
    return _AI_CONF_ORDER.get(_s(confidence).lower(), -1) >= _AI_CONF_ORDER.get(_s(required).lower(), 99)


def _ai_auto_import_allowed(use_case: str, confidence: str,
                            preflight: Optional[Dict[str, Any]],
                            mb_valid: bool, mb_id: str) -> bool:
    """Central gate for automated import/repair decisions."""
    thresholds = _ai_thresholds_for(use_case)
    if thresholds.get("requires_mb_release", True) and (not mb_valid or not mb_id):
        return False
    if not preflight or not preflight.get("ok"):
        return False
    if _ai_conf_at_least(confidence, thresholds.get("auto_confidence", "high")):
        return True
    review_conf = thresholds.get("review_confidence")
    medium_ratio = float(thresholds.get("medium_preflight_ratio") or 0)
    if review_conf and _ai_conf_at_least(confidence, review_conf):
        return _preflight_match_ratio(preflight) >= medium_ratio
    return False


def _track_ai_parse_duration_seconds(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
        if number <= 0:
            return None
        return number / 1000.0 if number > 10000 else number
    except Exception:
        pass
    text = _s(value).strip()
    if not text:
        return None
    parts = text.split(":")
    try:
        if len(parts) == 2:
            return float(parts[0]) * 60.0 + float(parts[1])
        if len(parts) == 3:
            return float(parts[0]) * 3600.0 + float(parts[1]) * 60.0 + float(parts[2])
    except Exception:
        return None
    return None


def _track_ai_duration_seconds(current: Dict[str, Any]) -> Optional[float]:
    return _track_ai_parse_duration_seconds(
        current.get("duration_seconds")
        or current.get("duration")
        or current.get("length")
    )


def _track_ai_candidate_duration_seconds(candidate: Dict[str, Any], details: Dict[str, Any]) -> Optional[float]:
    release = candidate.get("selected_release") or details.get("selected_release") or {}
    return _track_ai_parse_duration_seconds(
        release.get("duration_ms")
        or candidate.get("duration_ms")
        or details.get("recording_length_ms")
        or candidate.get("duration")
    )


def _track_ai_match_status(score: float, strong: float = 0.82, fuzzy: float = 0.68) -> str:
    if score >= strong:
        return "yes"
    if score >= fuzzy:
        return "fuzzy"
    return "no"


def _compact_track_ai_candidate(candidate: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    c = candidate or {}
    # Use the sanitized score_breakdown written by the matching contract
    # (build_recording_matching_decision), never the raw candidate["_match_score"]
    # -- the raw dict is untrusted and must never reach browser-visible JSON.
    score = c.get("score_breakdown") or {}
    try:
        candidate_index = int(c.get("candidate_index", -1))
    except Exception:
        candidate_index = -1
    selected_release = c.get("selected_release") or {}
    decision = c.get("decision") if isinstance(c.get("decision"), dict) else {}
    return {
        "candidate_index": candidate_index,
        "candidate_type": _s(c.get("candidate_type") or "recording"),
        "mb_trackid": _s(c.get("mb_trackid", "")),
        "mb_url": _s(c.get("mb_url") or c.get("musicbrainz_url") or ""),
        "musicbrainz_url": _s(c.get("musicbrainz_url") or c.get("mb_url") or ""),
        "title": _s(c.get("title", "")),
        "artist": _s(c.get("artist", "")),
        "recording_title": _s(c.get("recording_title") or c.get("title") or ""),
        "recording_artist": _s(c.get("recording_artist") or c.get("artist") or ""),
        "album": _s(c.get("album", "")),
        "year": _s(c.get("year", "")),
        "release_title": _s(c.get("release_title") or c.get("album") or ""),
        "release_artist": _s(c.get("release_artist") or selected_release.get("artist") or ""),
        "release_date": _s(c.get("release_date") or selected_release.get("date") or ""),
        "release_year": _s(c.get("release_year") or c.get("year") or ""),
        "country": _s(c.get("country", "")),
        "medium_format": _s(c.get("medium_format") or c.get("media_format") or ""),
        "track_number": _s(c.get("track_number") or c.get("track") or ""),
        "medium_position": c.get("medium_position"),
        "duration": _s(c.get("duration", "")),
        "source": _s(c.get("source") or score.get("source") or "mb"),
        "match_method": _s(c.get("match_method") or c.get("source") or score.get("source") or "mb"),
        "score": c.get("score") or 0,
        "match_total": round(float(score.get("total") or c.get("confidence_score") or 0), 3),
        "confidence": _s(c.get("confidence") or ""),
        "confidence_score": c.get("confidence_score"),
        "score_breakdown": score,
        "decision": c.get("decision") or {},
        "conflicts": c.get("conflicts") or decision.get("conflicts") or [],
        "warnings": c.get("warnings") or decision.get("warnings") or [],
        "review_required": bool(c.get("review_required") or decision.get("review_required")),
        "action_eligibility": decision.get("action_eligibility") or c.get("action_eligibility") or {},
        "eligibility_reason": _s(decision.get("eligibility_reason") or c.get("eligibility_reason") or ""),
        "recommended_action": _s(c.get("recommended_action") or decision.get("recommended_action") or ""),
        "requires_confirmation": bool(c.get("requires_confirmation")),
        "safety_result": _s(c.get("safety_result") or ""),
        "safety_key": _s(c.get("safety_key") or ""),
        "reason": _s(c.get("reason") or ""),
        "acoustid_score": c.get("acoustid_score"),
        "mb_albumid": _s(c.get("mb_albumid", "")),
        "release_id": _s(c.get("release_id") or c.get("mb_albumid", "")),
        "mb_albumids": c.get("mb_albumids", []) or [],
        "mb_releasegroupid": _s(c.get("mb_releasegroupid", "")),
        "release_group_id": _s(c.get("release_group_id") or c.get("mb_releasegroupid", "")),
        "mb_releasegroupurl": _s(c.get("mb_releasegroupurl", "")),
        "selected_release": selected_release,
        "linked_releases": (c.get("linked_releases") or [])[:12],
        "same_recording_release_count": c.get("same_recording_release_count") or len(c.get("linked_releases") or []),
        "matching_local_release_found": bool(c.get("matching_local_release_found")),
        "matching_contract": c.get("matching_contract") or {},
        "decision_version": _s(c.get("decision_version") or ""),
    }


def _track_ai_evidence_packet(iid: int, *, filename: str,
                              current: Dict[str, Any],
                              search_title: str,
                              search_artist: str,
                              suggestions: Dict[str, Any],
                              selected_candidate: Optional[Dict[str, Any]],
                              candidates: List[Dict[str, Any]],
                              acoustid_candidates: List[Dict[str, Any]],
                              discogs_candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    mb_trackid = _s(suggestions.get("mb_trackid", "")).strip().lower()
    try:
        selected_index = int((selected_candidate or {}).get("candidate_index", -1))
    except Exception:
        selected_index = -1
    top_acoustid = acoustid_candidates[0] if acoustid_candidates else {}
    fingerprint_status = "matched" if acoustid_candidates else "no_result"
    return {
        "use_case": "track_retag",
        "created_at": int(time.time()),
        "item_id": int(iid),
        "filename": filename,
        "current": dict(current),
        "search": {
            "title": search_title,
            "artist": search_artist,
        },
        "confidence": suggestions.get("confidence", ""),
        "reason": suggestions.get("reason", ""),
        "mb_trackid": mb_trackid,
        "mb_albumid": _s(suggestions.get("mb_albumid", "")).strip().lower(),
        "mb_valid": bool(suggestions.get("mb_valid")),
        "mb_candidate_valid": bool(selected_candidate),
        "candidate_index": selected_index,
        "selected_candidate": (
            _compact_track_ai_candidate(selected_candidate) if selected_candidate else {}
        ),
        "selected_recording_candidate": (
            _compact_track_ai_candidate(selected_candidate) if selected_candidate else {}
        ),
        "top_candidates": [_compact_track_ai_candidate(c) for c in candidates[:8]],
        "recording_candidates": [_compact_track_ai_candidate(c) for c in candidates[:8]],
        "missing_id_type": "Recording ID",
        "source_counts": {
            "acoustid": len(acoustid_candidates),
            "musicbrainz": max(0, len(candidates) - len(acoustid_candidates)),
            "discogs": len(discogs_candidates),
        },
        "fingerprint": {
            "status": fingerprint_status,
            "acoustid_status": "candidate" if acoustid_candidates else "no_result",
            "score": _audio_identity_score(top_acoustid) if top_acoustid else 0.0,
            "acoustid_id": _s(top_acoustid.get("acoustid_id") or "") if top_acoustid else "",
            "mb_trackid": _s(top_acoustid.get("mb_trackid") or "") if top_acoustid else "",
            "mb_releasegroupid": _s(top_acoustid.get("mb_releasegroupid") or "") if top_acoustid else "",
        },
        "validation": {
            "returned_track_in_candidates": bool(selected_candidate) if mb_trackid else False,
            "acoustid_available": bool(acoustid_candidates),
            "fingerprint_status": fingerprint_status,
            "trusted_for_apply": bool(selected_candidate),
        },
    }


def _classify_openai_error(exc: Exception) -> str:
    """Classify an OpenAI request failure into a short, human-readable reason.

    AI is an optional enhancement everywhere it's used for matching: a missing
    key, invalid key, timeout, rate limit, or any other provider failure must
    degrade gracefully to MusicBrainz/AcoustID-only matching rather than abort
    the caller. Callers use this text as the "AI unavailable: ..." suffix.
    """
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return "the AI provider rejected the API key (invalid or unauthorized)"
        if exc.code == 429:
            return "the AI provider rate-limited this request"
        if exc.code == 404:
            return "the configured AI model was not found"
        return f"the AI provider returned HTTP {exc.code}"
    if isinstance(exc, TimeoutError):
        return "the AI provider request timed out"
    if isinstance(exc, urllib.error.URLError):
        return "the AI provider is unreachable"
    _app_logger.warning("Unclassified AI provider error: %s", type(exc).__name__)
    return f"the AI provider request failed unexpectedly ({type(exc).__name__})"


def _ai_suggest_album_internal(
    album,
    log: List[str],
    *,
    existing_album_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Run AI + MusicBrainz suggestion for a library album that lacks an mb_albumid.

    Returns the same shape as the /api/albums/<id>/ai-suggest JSON response
    but as a plain dict (not a Flask response). AI is optional here: a
    missing/invalid key or any provider failure falls back to the top-ranked
    MusicBrainz/AcoustID candidate instead of returning ok=False, so light-
    confirm repair keeps working without AI configured.
    """
    api_key = _ai_api_key()
    ai_available = bool(api_key)

    aid = existing_album_id or int(getattr(album, "id", 0) or 0)
    tracks = sorted(album.items(), key=lambda t: t.track or 0)
    track_count = len(tracks)
    album_year  = str(album.year or "")
    track_list = "\n".join(
        f"{t.track or i+1}. {t.title or '(unknown)'}"
        for i, t in enumerate(tracks)
    )
    album_dir = _album_preflight_folder(album, tracks)
    track_paths: List[str] = []
    for t in tracks:
        path_text = _s(getattr(t, "path", "")).strip()
        if not path_text:
            continue
        try:
            p = Path(path_text)
            if not p.is_absolute():
                p = MUSIC_ROOT / p
            if p.exists():
                track_paths.append(str(p))
        except Exception:
            continue
    album_ev = {
        "folder_path": album_dir,
        "audio_files": track_paths,
        "folder_track_count": track_count,
        "nested_audio_count": 0,
        "guessed_artist": album.albumartist or "",
        "guessed_album": album.album or "",
        "guessed_year": album_year,
        "track_titles": [_s(getattr(t, "title", "")).strip() for t in tracks if _s(getattr(t, "title", "")).strip()],
        "track_lines": [
            f"  {t.track or i+1}. {t.title or '(unknown)'}"
            for i, t in enumerate(tracks)
        ],
        "filenames": [Path(p).name for p in track_paths[:30]],
    }
    acoustid_release_hits: Dict[str, int] = {}
    try:
        acoustid_release_hits = _acoustid_multi_file(track_paths) if track_paths else {}
    except Exception:
        acoustid_release_hits = {}
    acoustid_cands: List[Dict[str, Any]] = []
    for path in track_paths:
        acoustid_cands = _acoustid_lookup_cached(path)
        if acoustid_cands:
            break

    mb_candidates = _mb_release_search(album.album or "", album.albumartist or "", limit=8,
                                       year=album_year, track_count=track_count)
    for c in mb_candidates:
        c["acoustid_release_hits"] = acoustid_release_hits.get(c.get("mb_albumid", ""), 0)
        c["_match_score"] = _score_mb_release_candidate(album_ev, c)
    mb_candidates.sort(key=lambda c: c["_match_score"]["total"], reverse=True)
    if not mb_candidates:
        sug = {
            "candidate_index": -1,
            "album": album.album or "",
            "albumartist": album.albumartist or "",
            "year": int(album.year or 0) if album.year else None,
            "label": "",
            "country": "",
            "confidence": "low",
            "reason": (
                "No MusicBrainz release candidates were found for this album. "
                "Manual review or adding the release to MusicBrainz is required."
            ),
            "mb_albumid": "",
            "mb_valid": False,
            "mb_url": "",
        }
        if album.albumartist:
            sug["mb_artist_search_url"] = (
                "https://musicbrainz.org/search?"
                + urllib.parse.urlencode({"query": album.albumartist, "type": "artist"}))
        evidence = _ai_match_evidence_packet(
            "light_confirm",
            folder_path=album_dir,
            suggestion=sug,
            folder_evidence=album_ev,
            candidates=[],
        )
        sug["review_evidence"] = evidence
        log.append(f"  No MB candidates found for {album.albumartist or '?'} — {album.album or '?'}")
        return {
            "ok": True,
            "suggestion": sug,
            "mb_candidates": [],
            "acoustid_candidates": acoustid_cands,
            "acoustid_release_hits": acoustid_release_hits,
            "evidence": evidence,
        }

    acoustid_section = ""
    if acoustid_cands:
        lines = ["AcoustID fingerprint candidates (score / title / artist / album / year / mb_trackid):"]
        for c in acoustid_cands:
            lines.append(
                f"  [{c['score']:3d}] {c['title']} — {c['artist']} / {c['album']} ({c['year']}) [{c['mb_trackid']}]"
            )
        acoustid_section = "\n\n" + "\n".join(lines)

    mb_section = ""
    if mb_candidates:
        lines = [
            "MusicBrainz release candidates (idx / match / mb_score / album / artist / date / tracks / "
            "country / format / label+catalog / barcode / cover art / alternate editions / mb_albumid):"
        ]
        for i, c in enumerate(mb_candidates):
            ms = c.get("_match_score", {})
            fmt_str = "+".join(c.get("formats", [])) or "?"
            vinyl_flag = " [VINYL]" if c.get("is_vinyl") else ""
            aid_str = f" [AcoustID:{c['acoustid_release_hits']}]" if c.get("acoustid_release_hits") else ""
            catalog = ",".join(c.get("catalog_numbers") or []) or "?"
            barcode = c.get("barcode") or "?"
            cover = (
                f"cover:{c.get('cover_art_count', 0)}"
                if c.get("cover_art") else "cover:none")
            alt_count = max(0, int(c.get("edition_count") or 0) - 1)
            alt_note = f" alt_editions:{alt_count}" if alt_count else ""
            lines.append(
                f"  [{i}] match={ms.get('total', 0):.2f} mb={c['score']:3d} "
                f"{c['album']} - {c['artist']} ({c.get('date') or c['year']}) "
                f"{c['tracks']}trk {c['country']} {fmt_str}{vinyl_flag}{aid_str} / "
                f"{c['label']} [{catalog}] / barcode:{barcode} / {cover}{alt_note} "
                f"[{c['mb_albumid']}]"
            )
        mb_section = "\n\n" + "\n".join(lines)

    prompt = (
        "You are a music metadata expert with comprehensive MusicBrainz and AcoustID knowledge.\n\n"
        + acoustid_section +
        mb_section +
        "\n\n"
        "An album was imported without MusicBrainz identification. Identify the correct release.\n\n"
        f"Album:   {album.album or '(unknown)'}\n"
        f"Artist:  {album.albumartist or '(unknown)'}\n"
        f"Year:    {album_year or '(unknown)'}\n"
        f"Tracks ({track_count}):\n{track_list}"
        + "\n\n"
        "MATCHING RULES — apply in order:\n"
        "1. ARTIST + ALBUM match is required. Reject any candidate with a different artist or album title.\n"
        f"2. TRACK COUNT: album has {track_count} tracks. Strongly prefer candidates with exactly {track_count} tracks.\n"
        + (f"3. YEAR: album year is {album_year}. Prefer candidates from {album_year} (±1 year acceptable).\n"
           if album_year else "3. YEAR: unknown — use earliest matching release.\n")
        + "4. COUNTRY: strongly prefer US (country=US). If no equally strong US candidate exists, prefer Worldwide (country=XW) before other countries.\n"
        "5. FORMAT: prefer CD or Digital Media. Reject vinyl (marked [VINYL]) unless no other option exists.\n"
        "6. CONFIDENCE rules:\n"
        "   - high:   artist, album, track titles, track count, year all match\n"
        "   - medium: artist + album match but year/tracks differ slightly\n"
        "   - low:    uncertain or only partial match\n\n"
        "Return a JSON object with these keys:\n"
        f"  candidate_index — 0-based index into the candidates list above (0 to {max(0, len(mb_candidates)-1)})\n"
        "                    Use -1 if no candidate matches.\n"
        "  album           — correct album title\n"
        "  albumartist     — correct artist name\n"
        "  year            — release year as integer, or null if unknown\n"
        "  label           — record label (empty string if unknown)\n"
        "  country         — release country code (US, GB, XW, etc.; empty string if unknown)\n"
        "  confidence      — high | medium | low\n"
        "  reason          — one sentence explaining your match and why you chose this pressing\n"
    )
    _album_match_schema = {
        "type": "object",
        "properties": {
            "candidate_index": {"type": "integer"},
            "album":           {"type": "string"},
            "albumartist":     {"type": "string"},
            "year":            {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "label":           {"type": "string"},
            "country":         {"type": "string"},
            "confidence":      {"type": "string", "enum": ["high", "medium", "low"]},
            "reason":          {"type": "string"},
        },
        "required": ["candidate_index", "album", "albumartist", "year",
                     "label", "country", "confidence", "reason"],
        "additionalProperties": False,
    }
    _ai_model, _ai_endpoint = _ai_model_and_endpoint("gpt-4o")
    oai_payload = json.dumps({
        "model": _ai_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "album_match", "strict": True, "schema": _album_match_schema},
        },
    }).encode()
    ai_unavailable_reason = "" if ai_available else "OPENAI_API_KEY not configured"
    sug: Optional[Dict[str, Any]] = None
    if ai_available:
        req = urllib.request.Request(
            _ai_endpoint,
            data=oai_payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with provider_boundary.opened("ai", req, timeout=30) as r:
                data = json.loads(r.read())
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            if msg.get("refusal"):
                ai_available = False
                ai_unavailable_reason = f"AI refusal: {msg['refusal'][:200]}"
            else:
                sug = json.loads(msg["content"])
        except Exception as exc:
            ai_available = False
            ai_unavailable_reason = _classify_openai_error(exc)

    if sug is None:
        # AI unavailable/failed -- fall back to the top-ranked MusicBrainz/
        # AcoustID candidate instead of returning ok=False. The MB search and
        # AcoustID work above already ran unconditionally.
        top = mb_candidates[0]
        top_score = float((top.get("_match_score") or {}).get("total", 0) or 0)
        sug = {
            "candidate_index": 0,
            "album": top.get("album", "") or (album.album or ""),
            "albumartist": top.get("artist", "") or (album.albumartist or ""),
            "year": int(top["year"]) if str(top.get("year", "")).isdigit() else None,
            "label": top.get("label", ""),
            "country": top.get("country", ""),
            "confidence": "medium" if top_score >= 0.75 else "low",
            "reason": f"Matched using MusicBrainz and AcoustID (AI unavailable: {ai_unavailable_reason}).",
        }

    try:
        cand_idx = int(sug.get("candidate_index", -1))
        selected_candidate = None
        if 0 <= cand_idx < len(mb_candidates):
            selected_candidate = mb_candidates[cand_idx]
            mb_id = _s(selected_candidate.get("mb_albumid", "")).strip()
        else:
            mb_id = ""
            if sug.get("confidence") == "high":
                sug["confidence"] = "low"
            sug["reason"] = sug.get("reason") or "No matching candidate selected"
            cand_idx = -1
        sug["candidate_index"] = cand_idx
        # The selected candidate's Release is surfaced as is: never swapped for
        # a Release in another Release Group (a single stays a single).
        sug["mb_albumid"] = mb_id
        sug["representative_mb_albumid"] = mb_id
        rg_id = _s((selected_candidate or {}).get("mb_releasegroupid", "")).strip().lower()
        rg_url = _s((selected_candidate or {}).get("mb_releasegroupurl", "")).strip()
        rg_type = _s((selected_candidate or {}).get("release_group_primary_type", "")).strip()
        if mb_id and (
            not rg_id
            or mb_id != _s((selected_candidate or {}).get("mb_albumid", "")).strip().lower()
        ):
            mb_meta = _fetch_mb_release_tracklist(mb_id, [])
            if mb_meta.get("ok"):
                rg_id = _s(mb_meta.get("release_group", "")).strip().lower()
                rg_type = _s(mb_meta.get("release_group_primary_type", "")).strip()
        sug["mb_releasegroupid"] = rg_id
        sug["mb_releasegroupurl"] = rg_url or (
            f"https://musicbrainz.org/release-group/{rg_id}" if rg_id else ""
        )
        sug["release_group_primary_type"] = rg_type
        _MB_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
        sug["mb_valid"] = bool(re.match(_MB_RE, mb_id, re.I))
        if sug["mb_valid"]:
            sug["mb_url"] = f"https://musicbrainz.org/release/{mb_id}"
        if not sug.get("confidence"):
            sug["confidence"] = "medium"
        if not sug.get("reason"):
            sug["reason"] = ""
        selected_preflight = None
        if sug["mb_valid"]:
            selected_preflight = _run_ai_release_preflight(
                album_dir,
                mb_id,
                existing_album_id=aid,
            )
            _apply_ai_preflight_to_suggestion(sug, selected_preflight)
        _aa = (sug.get("albumartist") or "").strip()
        if _aa:
            sug["mb_artist_search_url"] = (
                "https://musicbrainz.org/search?"
                + urllib.parse.urlencode({"query": _aa, "type": "artist"}))
        sug["ai_available"] = ai_available
        sug["ai_unavailable_reason"] = ai_unavailable_reason
        evidence = _ai_match_evidence_packet(
            "light_confirm",
            folder_path=album_dir,
            suggestion=sug,
            folder_evidence=album_ev,
            selected_candidate=selected_candidate,
            candidates=mb_candidates,
            preflight=selected_preflight,
        )
        sug["review_evidence"] = evidence
        return {
            "ok": True,
            "suggestion": sug,
            "mb_candidates": mb_candidates,
            "acoustid_candidates": acoustid_cands,
            "acoustid_release_hits": acoustid_release_hits,
            "evidence": evidence,
        }
    except Exception as exc:
        _app_logger.warning("AI album suggestion failed: %s", type(exc).__name__)
        return {"ok": False, "error": "Could not generate suggestions."}


# ── AI Match history ──────────────────────────────────────────────────────────

_AI_MATCH_HISTORY_FILE = Path("/config/ai_match_history.json")


_ai_match_history_lock = threading.Lock()


def _record_ai_match(original_path: str, suggestion: dict):
    """Persist a successful AI-matched import to the history file."""
    entry = {
        "matched_at":      int(time.time()),
        "original_path":   original_path,
        "original_folder": Path(original_path).name,
        "artist":          suggestion.get("albumartist", ""),
        "album":           suggestion.get("album", ""),
        "year":            suggestion.get("year", ""),
        "mb_albumid":      suggestion.get("mb_albumid", ""),
        "mb_url":          suggestion.get("mb_url", "") or (
            f"https://musicbrainz.org/release/{suggestion['mb_albumid']}"
            if suggestion.get("mb_albumid") else ""),
        "confidence":      suggestion.get("confidence", ""),
        "reason":          suggestion.get("reason", ""),
        "evidence":        suggestion.get("review_evidence") or {},
    }
    with _ai_match_history_lock:
        try:
            existing = json.loads(_AI_MATCH_HISTORY_FILE.read_text()) \
                       if _AI_MATCH_HISTORY_FILE.exists() else []
        except Exception:
            existing = []
        existing.insert(0, entry)
        _AI_MATCH_HISTORY_FILE.write_text(json.dumps(existing[:200], indent=2))


_CANDIDATE_COUNTRY_RANK = {"US": 0, "XW": 1, "GB": 2, "CA": 3, "AU": 4}


def _score_mb_release_candidate(
    folder_evidence: Dict[str, Any], candidate: Dict[str, Any]
) -> Dict[str, Any]:
    """Score a MusicBrainz release candidate against folder evidence. No network calls.

    Returns component scores and a combined total (higher = better match).
    Pass acoustid_release_hits (int) in candidate if available.
    """
    guessed_artist     = folder_evidence.get("guessed_artist", "")
    guessed_album      = folder_evidence.get("guessed_album", "")
    guessed_year       = folder_evidence.get("guessed_year", "")
    folder_track_count = int(folder_evidence.get("folder_track_count") or 0)

    cand_artist   = _s(candidate.get("artist", ""))
    cand_album    = _s(candidate.get("album", ""))
    cand_year     = _s(candidate.get("year", ""))
    cand_tracks   = int(candidate.get("tracks", 0) or 0)
    cand_country  = _s(candidate.get("country", "")).upper()
    mb_score      = int(candidate.get("score", 0) or 0)
    acoustid_hits = int(candidate.get("acoustid_release_hits", 0) or 0)

    if guessed_artist and cand_artist:
        artist_sim = _canonical_similarity(guessed_artist, cand_artist)
    else:
        artist_sim = 0.5

    if guessed_album and cand_album:
        album_sim = _canonical_similarity(guessed_album, cand_album)
    else:
        album_sim = 0.5

    year_delta = 999
    if guessed_year and cand_year and guessed_year.isdigit() and cand_year[:4].isdigit():
        year_delta = abs(int(guessed_year) - int(cand_year[:4]))
    if year_delta == 999:
        year_score = 0.5
    elif year_delta == 0:
        year_score = 1.0
    elif year_delta == 1:
        year_score = 0.85
    elif year_delta <= 3:
        year_score = 0.60
    else:
        year_score = max(0.0, 0.50 - (year_delta - 3) * 0.05)

    track_count_delta = 999
    if folder_track_count and cand_tracks:
        track_count_delta = abs(folder_track_count - cand_tracks)
    if track_count_delta == 999:
        track_count_score = 0.5
    elif track_count_delta == 0:
        track_count_score = 1.0
    elif track_count_delta == 1:
        track_count_score = 0.80
    elif track_count_delta <= 3:
        track_count_score = 0.50
    else:
        track_count_score = max(0.0, 0.40 - track_count_delta * 0.05)

    country_rank   = _CANDIDATE_COUNTRY_RANK.get(cand_country, 9)
    country_score  = max(0.0, 1.0 - country_rank * 0.1)
    acoustid_bonus = min(0.30, acoustid_hits * 0.10)
    vinyl_penalty  = -0.20 if candidate.get("is_vinyl") else 0.0

    total = (
        artist_sim          * 0.32
        + album_sim         * 0.28
        + year_score        * 0.10
        + track_count_score * 0.14
        + country_score     * 0.05
        + (mb_score / 100.0) * 0.05
        + acoustid_bonus    * 0.06
        + vinyl_penalty
    )

    return {
        "artist_sim":         round(artist_sim, 3),
        "album_sim":          round(album_sim, 3),
        "year_score":         round(year_score, 3),
        "year_delta":         year_delta,
        "track_count_score":  round(track_count_score, 3),
        "track_count_delta":  track_count_delta,
        "country_score":      round(country_score, 3),
        "country_rank":       country_rank,
        "mb_score":           mb_score,
        "acoustid_hits":      acoustid_hits,
        "vinyl_penalty":      vinyl_penalty,
        "total":              round(max(0.0, total), 4),
    }


def _ai_preflight_note(preflight: Optional[Dict[str, Any]]) -> str:
    if not preflight:
        return ""
    status = "passed" if preflight.get("ok") else "failed"
    matches = int(preflight.get("matches") or 0)
    expected = int(preflight.get("expected") or 0)
    audio_count = int(preflight.get("audio_count") or 0)
    min_required = int(preflight.get("min_required") or 0)
    release_title = _s(preflight.get("release_title")).strip()
    release_artist = _s(preflight.get("release_artist")).strip()
    release_name = " - ".join(v for v in (release_artist, release_title) if v)
    target = f" for {release_name}" if release_name else ""

    parts = [
        f"Preflight {status}{target}: {matches}/{expected or '?'} release track(s) matched"
    ]
    if audio_count:
        parts.append(f"{audio_count} audio file(s) checked")
    if min_required and not preflight.get("ok"):
        parts.append(f"needs at least {min_required}")
    if preflight.get("artist_ok") is False:
        parts.append("artist check failed")
    if preflight.get("acoustid_mismatch"):
        parts.append("AcoustID points to a different release")
    error = _s(preflight.get("error")).strip()
    if error:
        parts.append(error)
    return "; ".join(parts) + "."


def _run_ai_release_preflight(folder_path: str, mb_albumid: str,
                              existing_album_id: int = 0) -> Optional[Dict[str, Any]]:
    folder_path = _s(folder_path).strip()
    mb_albumid = _s(mb_albumid).strip()
    if not folder_path or not mb_albumid:
        return None
    try:
        return _folder_release_preflight(
            folder_path,
            mb_albumid,
            existing_album_id=existing_album_id,
            log=None,
        )
    except Exception as ex:
        _app_logger.warning("Preflight failed to run: %s", type(ex).__name__)
        return {
            "ok": False,
            "matches": 0,
            "expected": 0,
            "audio_count": 0,
            "min_required": 0,
            "match_ratio": 0,
            "source_match_ratio": 0,
            "artist_ok": True,
            "artist_score": 0,
            "too_many_extras": False,
            "oversized_subset_complete": False,
            "acoustid_mismatch": False,
            "acoustid_target_hits": 0,
            "acoustid_top_release": "",
            "acoustid_top_hits": 0,
            "acoustid_release_hits": {},
            "release_title": "",
            "release_artist": "",
            "release_group": "",
            "error": "Preflight failed to run.",
            "examples": [],
        }


def _apply_ai_preflight_to_suggestion(suggestion: Dict[str, Any],
                                      preflight: Optional[Dict[str, Any]]) -> None:
    if not preflight:
        return

    suggestion["preflight"] = _compact_preflight(preflight)
    note = _ai_preflight_note(preflight)
    if not note:
        return

    if preflight.get("ok"):
        suggestion["preflight_note"] = note
        return

    matches = int(preflight.get("matches") or 0)
    min_required = int(preflight.get("min_required") or 0)
    severe = (
        bool(preflight.get("acoustid_mismatch"))
        or matches == 0
        or (preflight.get("artist_ok") is False and matches < max(1, min_required))
    )
    current_conf = _s(suggestion.get("confidence")).strip().lower()
    if severe:
        suggestion["confidence"] = "low"
    elif current_conf == "high":
        suggestion["confidence"] = "medium"

    reason = _s(suggestion.get("reason")).strip()
    if note not in reason:
        suggestion["reason"] = f"{note} {reason}".strip()


def _ai_suggest_folder_internal(folder_path: str) -> dict:
    """Identify an unimported folder using AI + MusicBrainz.
    Returns: { ok, suggestion: { mb_albumid, album, albumartist, year, label, country, confidence, reason, mb_valid, mb_url }, mb_candidates }

    AI is an enhancement, not a requirement: a missing/invalid OpenAI key or
    any provider failure (401/403/timeout/rate limit/refusal/etc.) must not
    block MusicBrainz/AcoustID matching. `ai_available` below gates only the
    actual OpenAI call further down -- folder evidence gathering, AcoustID
    fingerprinting, and MusicBrainz search all run unconditionally.
    """
    if not folder_path:
        return {"ok": False, "error": "path required"}
    api_key = _ai_api_key()
    ai_available = bool(api_key)

    # ── gather folder evidence ────────────────────────────────────────────────
    ev = _build_folder_evidence(folder_path)
    audio_files        = ev["audio_files"]
    folder_track_count = ev["folder_track_count"]
    nested_audio_count = ev["nested_audio_count"]
    guessed_artist     = ev["guessed_artist"]
    guessed_artist_mbid = ev.get("guessed_artist_mbid", "")
    guessed_album      = ev["guessed_album"]
    guessed_year       = ev["guessed_year"]
    track_lines        = ev["track_lines"]
    filenames          = ev["filenames"]

    # Detect whether direct (root-level) audio was found for the nested-file note.
    folder_obj, err = _resolve_import_review_source_path(folder_path, allow_music=True, expected_type="dir")
    if err or not folder_obj:
        return {"ok": False, "error": err or "Invalid folder path."}

    direct_audio_count = sum(
        1 for p in (folder_obj.iterdir() if folder_obj.is_dir() else [])
        if not p.is_symlink() and p.is_file() and p.suffix.lower() in AUDIO_EXT
    )

    # ── AcoustID fingerprinting (multi-file, cached) ──────────────────────────
    acoustid_release_hits = _acoustid_multi_file(audio_files)
    # #252 NF-2: an AcoustID that was not asked (no key, rejected key,
    # outage) is reported as such, never as "no fingerprint candidates".
    acoustid_lookup = _acoustid_lookup_cached_outcome(audio_files[0]) if audio_files else None
    acoustid_status = acoustid_failure_status(acoustid_lookup) if acoustid_lookup else ""
    acoustid_cands: List[Dict[str, Any]] = list(acoustid_lookup.data or []) if acoustid_lookup else []

    # ── MusicBrainz release search ─────────────────────────────────────────────
    # When the artist folder is MBID-stamped, prefer arid: lookup over artist name
    # search — more reliable for non-ASCII or unusual artist names (e.g. ¥$).
    # mb_search_log captures real lookup failures (network/API errors) so they
    # surface as "MusicBrainz lookup failed: ..." instead of being silently
    # indistinguishable from a genuine no-candidates result.
    mb_search_log: List[str] = []
    mb_unavailable = False
    try:
        mb_candidates = _mb_release_search(guessed_album, guessed_artist, limit=8,
                                           year=guessed_year, track_count=folder_track_count,
                                           artist_mbid=guessed_artist_mbid, log=mb_search_log)
        if not mb_candidates and guessed_artist:
            mb_candidates = _mb_release_search(guessed_album, "", limit=8,
                                               year=guessed_year, track_count=folder_track_count,
                                               artist_mbid=guessed_artist_mbid, log=mb_search_log)
        if not mb_candidates and guessed_album:
            short = " ".join(guessed_album.split()[:3])
            mb_candidates = _mb_release_search(short, guessed_artist, limit=8,
                                               year=guessed_year, track_count=folder_track_count,
                                               artist_mbid=guessed_artist_mbid, log=mb_search_log)
        if not mb_candidates:
            # Last resort: search MB by track titles extracted from the audio files.
            # Useful when folder/tag names are badly mangled but track metadata is intact.
            mb_candidates = _mb_release_search_by_folder_tracks(
                folder_path, artist=guessed_artist, log=mb_search_log, limit=8)
    except provider_boundary.ProviderError as exc:
        # An outage is not "no candidates": keep going with AcoustID/Discogs
        # evidence and report the lookup failure below.
        mb_unavailable, mb_candidates = True, []
        mb_search_log.append(f"WARN: MusicBrainz lookup failed: {exc}")
    discogs_fallback: Dict[str, Any] = {}
    if not mb_candidates:
        # MusicBrainz text search exhausted every variant it knows; Discogs'
        # larger catalog and cleaner "Artist - Album" parsing can sometimes
        # succeed where the raw folder-name guess didn't.
        discogs_fallback = _discogs_release_fallback_candidate(guessed_artist, guessed_album)
        if discogs_fallback:
            mb_search_log.append(
                f"Discogs match found: {discogs_fallback.get('artist')} - {discogs_fallback.get('album')}"
            )
            retry_artist = _s(discogs_fallback.get("artist")) or guessed_artist
            retry_album = _s(discogs_fallback.get("album")) or guessed_album
            if not mb_unavailable and (retry_artist != guessed_artist or retry_album != guessed_album):
                mb_candidates = _mb_release_search(retry_album, retry_artist, limit=8,
                                                   year=guessed_year, track_count=folder_track_count,
                                                   artist_mbid=guessed_artist_mbid, log=mb_search_log)
    mb_search_failed = any(
        "failed" in line.lower() or "warn:" in line.lower() for line in mb_search_log
    )

    # ── AcoustID winner injection ─────────────────────────────────────────────
    # If 3+ independently fingerprinted tracks agree on a release not yet in
    # mb_candidates (text search missed it), fetch it from MB and prepend it
    # so the shortcut and the AI prompt can both see it.
    if acoustid_release_hits:
        _top_aid_id, _top_aid_hits = max(
            acoustid_release_hits.items(), key=lambda kv: kv[1]
        )
        _already_present = any(
            _s(c.get("mb_albumid", "")).strip().lower() == _top_aid_id.lower()
            for c in mb_candidates
        )
        if _top_aid_hits >= 3 and not _already_present:
            _fetched = _fetch_mb_release_candidate(_top_aid_id)
            if _fetched:
                mb_candidates.insert(0, _fetched)

    # ── score and rank candidates ─────────────────────────────────────────────
    for c in mb_candidates:
        c["acoustid_release_hits"] = acoustid_release_hits.get(c.get("mb_albumid", ""), 0)
        c["_match_score"] = _score_mb_release_candidate(ev, c)
    mb_candidates.sort(key=lambda c: c["_match_score"]["total"], reverse=True)

    # ── AcoustID shortcut (skip AI when fingerprints strongly agree) ──────────
    # 2+ tracks fingerprinted to the same release that's already in candidates
    # → use it directly without paying for an OpenAI call.
    if acoustid_release_hits and mb_candidates:
        _top_aid_id, _top_aid_hits = max(
            acoustid_release_hits.items(), key=lambda kv: kv[1]
        )
        if _top_aid_hits >= 2:
            _aid_cand = next(
                (c for c in mb_candidates
                 if _s(c.get("mb_albumid", "")).strip().lower() == _top_aid_id.lower()),
                None,
            )
            if _aid_cand is not None:
                _aid_conf = "high" if _top_aid_hits >= 3 else "medium"
                _aid_id = _s(_aid_cand.get("mb_albumid", "")).strip()
                sug = {
                    "candidate_index": mb_candidates.index(_aid_cand),
                    "album":       _aid_cand.get("album", ""),
                    "albumartist": _aid_cand.get("artist", ""),
                    "year": (int(_aid_cand["year"])
                             if str(_aid_cand.get("year", "")).isdigit() else None),
                    "label":       _aid_cand.get("label", ""),
                    "country":     _aid_cand.get("country", ""),
                    "confidence":  _aid_conf,
                    "reason": (
                        f"AcoustID fingerprint: {_top_aid_hits} track(s) "
                        "independently matched this release — skipped AI."
                    ),
                    "mb_albumid": _aid_id,
                    "mb_valid":   bool(re.match(
                        r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
                        _aid_id, re.I,
                    )),
                }
                if sug["mb_valid"]:
                    sug["mb_url"] = f"https://musicbrainz.org/release/{_aid_id}"
                    _pf = _run_ai_release_preflight(folder_path, _aid_id)
                    _apply_ai_preflight_to_suggestion(sug, _pf)
                else:
                    _pf = None
                if _aa := (sug.get("albumartist") or "").strip():
                    sug["mb_artist_search_url"] = (
                        "https://musicbrainz.org/search?"
                        + urllib.parse.urlencode({"query": _aa, "type": "artist"})
                    )
                evidence = _ai_match_evidence_packet(
                    "fresh_import",
                    folder_path=folder_path,
                    suggestion=sug,
                    folder_evidence=ev,
                    selected_candidate=_aid_cand,
                    candidates=mb_candidates,
                    preflight=_pf,
                )
                sug["folder_signature"] = _import_review_folder_signature(folder_path)
                sug["review_evidence"] = evidence
                return {
                    "ok": True,
                    "suggestion": sug,
                    "mb_candidates": mb_candidates,
                    "acoustid_candidates": acoustid_cands,
                    "acoustid_release_hits": acoustid_release_hits,
                    "evidence": evidence,
                    "acoustid_unavailable": bool(acoustid_status),
                    "acoustid_status": acoustid_status,
                }

    if not mb_candidates:
        if mb_search_failed:
            no_candidates_reason = (
                "MusicBrainz lookup failed: " + "; ".join(mb_search_log[-2:])
            )
        elif not guessed_artist and not guessed_album:
            no_candidates_reason = (
                "No artist/album could be parsed from the folder name/tags; "
                "manual review is required."
            )
        elif discogs_fallback:
            no_candidates_reason = (
                "No MusicBrainz release candidates were found, but a possible match exists on Discogs: "
                f"{discogs_fallback.get('artist')} - {discogs_fallback.get('album')} "
                f"({discogs_fallback.get('discogs_url') or 'no URL'}). Consider submitting it to MusicBrainz."
            )
        else:
            no_candidates_reason = "No MusicBrainz release candidates were found; manual review is required."
        sug = {
            "candidate_index": -1,
            "album": guessed_album,
            "albumartist": guessed_artist,
            "year": int(guessed_year) if str(guessed_year).isdigit() else None,
            "label": "",
            "country": "",
            "confidence": "low",
            "reason": no_candidates_reason,
            "mb_albumid": "",
            "mb_valid": False,
            "discogs_candidate": discogs_fallback or None,
        }
        evidence = _ai_match_evidence_packet(
            "fresh_import",
            folder_path=folder_path,
            suggestion=sug,
            folder_evidence=ev,
            candidates=[],
        )
        sug["folder_signature"] = _import_review_folder_signature(folder_path)
        sug["review_evidence"] = evidence
        return {
            "ok": True,
            "suggestion": sug,
            "mb_candidates": [],
            "acoustid_candidates": acoustid_cands,
            "acoustid_release_hits": acoustid_release_hits,
            "evidence": evidence,
            "musicbrainz_unavailable": mb_unavailable,
            "acoustid_unavailable": bool(acoustid_status),
            "acoustid_status": acoustid_status,
        }

    # ── build prompt sections ─────────────────────────────────────────────────
    acoustid_section = ""
    if acoustid_release_hits:
        top_hits = sorted(acoustid_release_hits.items(), key=lambda x: -x[1])[:6]
        lines = ["AcoustID fingerprint hit counts by MB release (sampled tracks that matched each release):"]
        for mb_id, hits in top_hits:
            lines.append(f"  {hits} hit(s): [{mb_id}]")
        acoustid_section = "\n\n" + "\n".join(lines)
    elif acoustid_cands:
        lines = ["AcoustID fingerprint candidates (score / title / artist / album / year / mb_trackid):"]
        for c in acoustid_cands:
            lines.append(
                f"  [{c['score']:3d}] {c['title']} — {c['artist']} / {c['album']} ({c['year']}) [{c['mb_trackid']}]"
            )
        acoustid_section = "\n\n" + "\n".join(lines)

    mb_section = ""
    if mb_candidates:
        lines = [
            "MusicBrainz release candidates (idx / match / mb_score / album / artist / year / "
            "tracks / country / format / label+catalog / barcode / cover art / alternate editions / mb_albumid):"
        ]
        for i, c in enumerate(mb_candidates):
            ms     = c.get("_match_score", {})
            fmt_str = "+".join(c.get("formats", [])) or "?"
            vinyl_flag = " [VINYL]" if c.get("is_vinyl") else ""
            aid_str = f" [AcoustID:{c['acoustid_release_hits']}]" if c.get("acoustid_release_hits") else ""
            catalog = ",".join(c.get("catalog_numbers") or []) or "?"
            barcode = c.get("barcode") or "?"
            cover = (
                f"cover:{c.get('cover_art_count', 0)}"
                if c.get("cover_art") else "cover:none")
            alt_count = max(0, int(c.get("edition_count") or 0) - 1)
            alt_note = f" alt_editions:{alt_count}" if alt_count else ""
            lines.append(
                f"  [{i}] match={ms.get('total', 0):.2f} mb={c['score']:3d} {c['album']} — {c['artist']} "
                f"({c['year']}) {c['tracks']}trk {c['country']} {fmt_str}{vinyl_flag}{aid_str} "
                f"/ {c['label']} [{catalog}] / barcode:{barcode} / {cover}{alt_note} [{c['mb_albumid']}]"
            )
        mb_section = "\n\n" + "\n".join(lines)

    track_section = ""
    if track_lines:
        track_section = f"\n\nTracks in folder ({folder_track_count} audio files):\n" + "\n".join(track_lines)
    elif filenames:
        track_section = f"\n\nAudio filenames ({folder_track_count} files):\n" + "\n".join(f"  {fn}" for fn in filenames)

    nested_note = (
        f" used for matching ({nested_audio_count} nested duplicate/candidate file(s) ignored)"
        if direct_audio_count > 0 and nested_audio_count > 0 else ""
    )

    prompt = (
        "You are a music metadata expert with comprehensive MusicBrainz and AcoustID knowledge.\n\n"
        "If AcoustID fingerprint candidates exist, prefer them and use them as the primary basis for matching.\n"
        "A folder of music files was skipped during import — beets could not auto-match it.\n"
        "Identify the correct MusicBrainz release so it can be imported.\n\n"
        f"Folder path:    {folder_path}\n"
        f"Guessed artist: {guessed_artist or '(unknown)'}\n"
        f"Guessed album:  {guessed_album or '(unknown)'}\n"
        f"Guessed year:   {guessed_year or '(unknown)'}\n"
        f"Folder has {folder_track_count} audio file(s){nested_note}"
        + track_section
        + acoustid_section
        + mb_section
        + "\n\n"
        "MATCHING RULES — apply in order:\n"
        "1. ARTIST + ALBUM match is required. Reject any candidate with a different artist or album title.\n"
        f"2. TRACK COUNT: folder has {folder_track_count} tracks used for matching. "
        "High confidence requires the candidate track count to match exactly; "
        "if track counts differ, confidence must be medium or low.\n"
        + (f"3. YEAR: folder year is {guessed_year}. Prefer candidates from {guessed_year} (±1 year acceptable).\n"
           if guessed_year else "3. YEAR: unknown — use earliest matching release.\n")
        + "4. COUNTRY: strongly prefer US (country=US). If no equally strong US candidate exists, prefer Worldwide (country=XW) before other countries.\n"
        "5. FORMAT: prefer CD or Digital Media. Reject vinyl (marked [VINYL]) unless no other option exists.\n"
        "6. CONFIDENCE rules:\n"
        "   - high:   artist, album, exact track count, year, country all match\n"
        "   - medium: artist + album match but year/country differ slightly; track count may differ only if explained\n"
        "   - low:    uncertain match or only partial name match\n\n"
        "Return a JSON object with these keys:\n"
        f"  candidate_index — 0-based index into the candidates list above (0 to {max(0, len(mb_candidates)-1)})\n"
        "                    Use -1 if no candidate matches. DO NOT invent an index.\n"
        "  album           — correct album title\n"
        "  albumartist     — correct artist name\n"
        "  year            — release year as integer, or null if unknown\n"
        "  label           — record label (empty string if unknown)\n"
        "  country         — release country code (US, GB, XW, etc.; empty string if unknown)\n"
        "  confidence      — high | medium | low\n"
        "  reason          — one sentence explaining your match and why you chose this pressing\n"
    )
    _folder_match_schema = {
        "type": "object",
        "properties": {
            "candidate_index": {"type": "integer"},
            "album":           {"type": "string"},
            "albumartist":     {"type": "string"},
            "year":            {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "label":           {"type": "string"},
            "country":         {"type": "string"},
            "confidence":      {"type": "string", "enum": ["high", "medium", "low"]},
            "reason":          {"type": "string"},
        },
        "required": ["candidate_index", "album", "albumartist", "year",
                     "label", "country", "confidence", "reason"],
        "additionalProperties": False,
    }
    _ai_model, _ai_endpoint = _ai_model_and_endpoint("gpt-4o")
    req_payload = json.dumps({
        "model": _ai_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name":   "folder_match",
                "strict": True,
                "schema": _folder_match_schema,
            },
        },
    }).encode()
    ai_unavailable_reason = "" if ai_available else "OPENAI_API_KEY not configured"
    sug: Optional[Dict[str, Any]] = None
    if ai_available:
        req = urllib.request.Request(
            _ai_endpoint,
            data=req_payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with provider_boundary.opened("ai", req, timeout=30) as r:
                data = json.loads(r.read())
            # Check for refusal
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            if msg.get("refusal"):
                ai_available = False
                ai_unavailable_reason = f"AI refusal: {msg['refusal'][:200]}"
            else:
                sug = json.loads(msg["content"])
        except Exception as exc:
            ai_available = False
            ai_unavailable_reason = _classify_openai_error(exc)

    if sug is None:
        # AI unavailable/failed -- fall back to the top-ranked MusicBrainz/
        # AcoustID candidate instead of returning ok=False. Folder evidence,
        # AcoustID fingerprinting, and MusicBrainz search above already ran
        # unconditionally, so this still completes the match.
        top = mb_candidates[0]
        top_score = float((top.get("_match_score") or {}).get("total", 0) or 0)
        sug = {
            "candidate_index": 0,
            "album": top.get("album", "") or guessed_album,
            "albumartist": top.get("artist", "") or guessed_artist,
            "year": int(top["year"]) if str(top.get("year", "")).isdigit() else None,
            "label": top.get("label", ""),
            "country": top.get("country", ""),
            "confidence": "medium" if top_score >= 0.75 else "low",
            "reason": f"Matched using MusicBrainz and AcoustID (AI unavailable: {ai_unavailable_reason}).",
        }

    try:
        # Map candidate_index → mb_albumid
        cand_idx = int(sug.get("candidate_index", -1))
        selected_candidate = None
        if 0 <= cand_idx < len(mb_candidates):
            selected_candidate = mb_candidates[cand_idx]
            mb_id = _s(selected_candidate.get("mb_albumid", "")).strip()
        else:
            mb_id = ""
            if sug.get("confidence") == "high":
                sug["confidence"] = "low"
            sug["reason"] = sug.get("reason") or "No matching candidate selected"
            cand_idx = -1
        sug["candidate_index"] = cand_idx
        sug["mb_albumid"] = mb_id
        sug["representative_mb_albumid"] = mb_id
        rg_id = _s((selected_candidate or {}).get("mb_releasegroupid", "")).strip().lower()
        rg_url = _s((selected_candidate or {}).get("mb_releasegroupurl", "")).strip()
        rg_type = _s((selected_candidate or {}).get("release_group_primary_type", "")).strip()
        if mb_id and (
            not rg_id
            or mb_id != _s((selected_candidate or {}).get("mb_albumid", "")).strip().lower()
        ):
            mb_meta = _fetch_mb_release_tracklist(mb_id, [])
            if mb_meta.get("ok"):
                rg_id = _s(mb_meta.get("release_group", "")).strip().lower()
                rg_type = _s(mb_meta.get("release_group_primary_type", "")).strip()
        sug["mb_releasegroupid"] = rg_id
        sug["mb_releasegroupurl"] = rg_url or (
            f"https://musicbrainz.org/release-group/{rg_id}" if rg_id else ""
        )
        sug["release_group_primary_type"] = rg_type
        _MB_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
        sug["mb_valid"] = bool(re.match(_MB_RE, mb_id, re.I))
        if sug["mb_valid"]:
            sug["mb_url"] = f"https://musicbrainz.org/release/{mb_id}"
        if not sug.get("confidence"):
            sug["confidence"] = "medium"
        if not sug.get("reason"):
            sug["reason"] = ""
        selected_preflight = None
        if sug["mb_valid"]:
            selected_preflight = _run_ai_release_preflight(folder_path, mb_id)
            _apply_ai_preflight_to_suggestion(sug, selected_preflight)
        _aa = (sug.get("albumartist") or "").strip()
        if _aa:
            sug["mb_artist_search_url"] = (
                "https://musicbrainz.org/search?"
                + urllib.parse.urlencode({"query": _aa, "type": "artist"}))
        # Backend identity gate: reject a candidate whose artist doesn't
        # match the folder's own evidence, independent of album title —
        # a matching album title must never rescue a wrong-artist candidate
        # (2026-07-16, Voyager/Vitalic fix: "same title, different artist"
        # was slipping through an earlier AND-based version of this check).
        identity_rejection_reason = ""
        if selected_candidate and guessed_artist:
            cand_artist = _s(selected_candidate.get("artist", "")).strip()
            cand_album = _s(selected_candidate.get("album", "")).strip()
            artist_score = _playlist_artist_name_score(guessed_artist, cand_artist)
            album_score = _playlist_title_score(guessed_album, cand_album)
            if cand_artist and artist_score < 0.45:
                identity_rejection_reason = (
                    "Rejected: artist mismatch. Album title matched, but "
                    f"candidate artist is {cand_artist} and source artist is {guessed_artist}."
                )
        sug["identity_validated"] = not bool(identity_rejection_reason)
        sug["candidate_identity_error"] = identity_rejection_reason
        sug["ai_available"] = ai_available
        sug["ai_unavailable_reason"] = ai_unavailable_reason
        evidence = _ai_match_evidence_packet(
            "fresh_import",
            folder_path=folder_path,
            suggestion=sug,
            folder_evidence=ev,
            selected_candidate=selected_candidate,
            candidates=mb_candidates,
            preflight=selected_preflight,
        )
        sug["folder_signature"] = _import_review_folder_signature(folder_path)
        sug["review_evidence"] = evidence
        return {
            "ok": True,
            "suggestion": sug,
            "mb_candidates": mb_candidates,
            "acoustid_candidates": acoustid_cands,
            "acoustid_release_hits": acoustid_release_hits,
            "evidence": evidence,
            "acoustid_unavailable": bool(acoustid_status),
            "acoustid_status": acoustid_status,
        }
    except Exception as exc:
        _app_logger.warning("AI folder suggestion failed: %s", type(exc).__name__)
        return {"ok": False, "error": "Could not generate suggestions."}


def _run_ai_matching_regressions() -> Dict[str, Any]:
    """Read-only regression checks for import matching heuristics."""
    cases: List[Dict[str, Any]] = []

    def _case(name: str, ok: bool, details: Optional[Dict[str, Any]] = None) -> None:
        cases.append({"name": name, "ok": bool(ok), "details": details or {}})

    wanted = [
        {"disc": 1, "track": 1, "title": "Life of the Party", "mb_trackid": "trk-life"},
        {"disc": 1, "track": 2, "title": "Come to Life", "mb_trackid": "trk-come"},
    ]
    exact = _slskd_file_wanted_match_score("01 - Life of the Party.flac", wanted)
    fuzzy = _slskd_file_wanted_match_score("Kanye West - Come To Life (Explicit).mp3", wanted)
    miss = _slskd_file_wanted_match_score("03 - We Don't Care.flac", wanted)
    _case("missing-track exact number/title match", exact.get("ok") and exact.get("score", 0) >= 0.98, exact)
    _case("missing-track fuzzy title match", fuzzy.get("ok") and fuzzy.get("score", 0) >= 0.86, fuzzy)
    _case("missing-track rejects unrelated file", not miss.get("ok"), miss)

    glued_ft = _best_album_track_match(
        {"title": "Light It Upft Pop Smoke", "path": ""},
        [{"disc": 1, "track": 1, "title": "Light It Up", "title_norm": _album_track_norm("Light It Up")}],
    )
    glued_feat = _best_album_track_match(
        {"title": "Malibufeat Polo G", "path": ""},
        [{"disc": 1, "track": 2, "title": "Malibu", "title_norm": _album_track_norm("Malibu")}],
    )
    _case("album matcher strips glued ft suffix", glued_ft.get("score", 0) >= 0.98, glued_ft)
    _case("album matcher strips glued feat suffix", glued_feat.get("score", 0) >= 0.98, glued_feat)

    oversized_pre = {
        "audio_count": 10,
        "expected": 77,
        "matches": 10,
        "artist_ok": True,
    }
    weak_pre = {
        "audio_count": 10,
        "expected": 77,
        "matches": 3,
        "artist_ok": True,
    }
    _case("oversized-release subset accepts complete source", _preflight_oversized_subset_complete(oversized_pre), oversized_pre)
    _case("oversized-release subset rejects weak source", not _preflight_oversized_subset_complete(weak_pre), weak_pre)
    _case(
        "preflight gate accepts clean partial oversized release",
        _preflight_tracklist_gate_ok(
            matches=8,
            min_required=12,
            artist_gate=True,
            too_many_extras=False,
            oversized_subset=_preflight_oversized_subset_complete(oversized_pre),
        ),
        oversized_pre,
    )
    _case(
        "preflight gate rejects weak oversized release",
        not _preflight_tracklist_gate_ok(
            matches=3,
            min_required=12,
            artist_gate=True,
            too_many_extras=False,
            oversized_subset=_preflight_oversized_subset_complete(weak_pre),
        ),
        weak_pre,
    )

    folder_ev = {
        "guessed_artist": "Prince",
        "guessed_album": "20Ten",
        "guessed_year": "2010",
        "folder_track_count": 10,
    }
    sized = _score_mb_release_candidate(folder_ev, {
        "artist": "Prince", "album": "20Ten", "year": "2010",
        "tracks": 10, "country": "US", "score": 95,
    })
    oversized = _score_mb_release_candidate(folder_ev, {
        "artist": "Prince", "album": "20Ten", "year": "2010",
        "tracks": 77, "country": "US", "score": 95,
    })
    _case(
        "candidate scorer prefers source-sized release",
        sized.get("total", 0) > oversized.get("total", 0),
        {"source_sized": sized, "oversized": oversized},
    )

    strong_pf = {"ok": True, "matches": 8, "expected": 10}
    weak_pf = {"ok": True, "matches": 7, "expected": 10}
    _case(
        "fresh-import medium requires configured preflight ratio",
        _ai_auto_import_allowed("fresh_import", "medium", strong_pf, True, "x")
        and not _ai_auto_import_allowed("fresh_import", "medium", weak_pf, True, "x"),
        {
            "strong_ratio": _preflight_match_ratio(strong_pf),
            "weak_ratio": _preflight_match_ratio(weak_pf),
            "thresholds": _ai_thresholds_for("fresh_import"),
        },
    )
    _case(
        "light-confirm threshold is separate from fresh import",
        _ai_thresholds_for("light_confirm")["auto_confidence"] == _AI_REPAIR_MIN_CONF,
        {"thresholds": _ai_thresholds_for("light_confirm")},
    )

    ok = all(c["ok"] for c in cases)
    return {"ok": ok, "case_count": len(cases), "cases": cases}


# Wave 26 correction: this is Web Manager application state (which batches
# are running, AI review decisions, suggestion cache) -- never authoritative
# Beets library/media state -- so it belongs under WEB_MANAGER_DATA_DIR
# (/web-manager-data), the root this container actually owns and creates
# (see Dockerfile: only /web-manager-data is mkdir'd/chowned/declared as a
# VOLUME; the image runs as non-root USER beets). The previous default,
# /config/ai_batch_jobs, is the Beets ENGINE's own config mount -- it does
# not exist in the beets-web-manager container in either documented
# two-service compose topology, so AiBatchStateStore's constructor-time
# `self.db_path.parent.mkdir(parents=True, exist_ok=True)` raised
# PermissionError creating /config itself at MODULE IMPORT time, before
# Waitress/Flask ever bound a port -- the container never became healthy.
_AI_BATCH_STATE_DIR = Path(os.environ.get("AI_BATCH_STATE_DIR", str(WEB_MANAGER_DATA_DIR / "ai_batch_jobs")))


_AI_BATCH_AI_TIMEOUT = max(30, int(os.environ.get("AI_BATCH_AI_TIMEOUT", "180") or "180"))


_ai_batch_control_lock = threading.Lock()


_ai_batch_controls: Dict[str, Dict[str, Any]] = {}


_AI_BATCH_WORKER_MISSING = object()


def _ai_batch_reserve_worker(batch_job_id: str) -> bool:
    """Atomically claim the right to start a worker for batch_job_id.

    Returns True if the caller may proceed to call jobs.start_python(); False
    if another worker is already reserved-starting or active for this batch.
    On True, the caller owns the entry and must eventually call either
    _ai_batch_promote_worker (handing ownership to the worker thread, which
    then owns the eventual _ai_batch_release_worker call) or
    _ai_batch_release_worker(batch_job_id, expected=None) itself if startup
    fails before a worker thread can take ownership."""
    with _ai_batch_worker_lock:
        if batch_job_id in _ai_batch_active_workers:
            return False
        _ai_batch_active_workers[batch_job_id] = None
        return True


def _ai_batch_promote_worker(batch_job_id: str, job_id: str) -> bool:
    """Atomically move a reservation from "startup reserved" to "active with
    job_id", once jobs.start_python() has returned and the job_id
    association has been durably persisted. Must happen before the worker
    thread is unblocked to do real work. Returns False if the entry is no
    longer present (defensive; should not happen in normal operation since
    only the reserving caller promotes its own reservation)."""
    with _ai_batch_worker_lock:
        if batch_job_id not in _ai_batch_active_workers:
            return False
        _ai_batch_active_workers[batch_job_id] = job_id
        return True


def _ai_batch_release_worker(batch_job_id: str, expected: Optional[str]) -> None:
    """Release batch_job_id's registry entry, but only if it still equals
    `expected` -- an ownership check so a stale worker's cleanup (e.g. an old
    job_id from a previous, already-superseded reservation) can never remove
    a newer worker's registration. Pass expected=None to release an
    unpromoted startup reservation (a failed start); pass the real job_id to
    release a promoted, completed/failed worker."""
    with _ai_batch_worker_lock:
        if _ai_batch_active_workers.get(batch_job_id, _AI_BATCH_WORKER_MISSING) == expected:
            _ai_batch_active_workers.pop(batch_job_id, None)


def _ai_batch_active_worker_job_id(batch_job_id: str) -> str:
    """Return the promoted job_id for batch_job_id, or "" if no worker is
    registered at all, or if a worker is startup-reserved but not yet
    promoted (job_id not allocated yet)."""
    if not batch_job_id:
        return ""
    with _ai_batch_worker_lock:
        return _ai_batch_active_workers.get(batch_job_id) or ""


def _ai_batch_release_worker_any(batch_job_id: str, owned_job_id: str) -> None:
    """Release batch_job_id's registry entry regardless of which startup
    outcome actually applies to this worker: tries the promoted-token
    release first (the common case, once _ai_batch_promote_worker
    succeeded), then the unpromoted-reservation release (startup aborted,
    timed out, or failed before promotion). Both calls are the same
    ownership-safe compare-and-pop as _ai_batch_release_worker -- at most
    one of the two expected values can ever match the live registry entry
    for this batch_job_id at a given moment (reservation is exclusive, so
    no other caller's entry can be sitting in between), so trying both in
    sequence is safe and idempotent regardless of outcome, without needing
    a separate mutable "was this promoted" flag shared between threads."""
    if owned_job_id:
        _ai_batch_release_worker(batch_job_id, expected=owned_job_id)
    _ai_batch_release_worker(batch_job_id, expected=None)


class _AiBatchStartupAbortedError(Exception):
    """Raised inside the AI batch worker wrapper (_do, in
    _start_ai_batch_job) when the startup handoff was never validly
    completed -- the handoff wait timed out, _start_ai_batch_job explicitly
    aborted after the worker thread was already spawned, no job_id was
    ever assigned, or the active-worker registry does not (yet, or no
    longer) show this worker's own job_id as promoted. In every such case
    the worker must exit without calling _run_ai_batch_import: there was
    no durable, registry-confirmed authorization to do real batch work.
    Caught by the wrapper's own try/finally; PythonJob._run's existing
    exception handling records this as a normal job failure (a concise
    message in the job log, not a traceback) -- never as successful batch
    processing."""


def _ai_batch_validate_worker_handoff(
    batch_job_id: str, owned_job_id: str, *, signaled: bool, aborted: bool,
) -> None:
    """Raise _AiBatchStartupAbortedError unless every part of the startup
    handoff was validly completed. Extracted from the worker wrapper (_do,
    in _start_ai_batch_job) as a small, directly unit-testable function --
    exercising the handoff-timeout and startup-abort paths behaviorally
    would otherwise require either a real 10-second wait on
    threading.Event.wait's timeout, or patching internals of a closure that
    isn't otherwise reachable from outside _start_ai_batch_job. Not pure:
    it reads live process-local active-worker registry state
    (_ai_batch_active_worker_job_id) to confirm ownership.

    Does not use timing or the mere presence of owned_job_id as proof that
    startup succeeded: `signaled` must be the actual return value of
    handoff_ready.wait(...) (True only when _start_ai_batch_job explicitly
    signaled a decision, never inferred from a timeout), and ownership is
    confirmed by re-reading the live active-worker registry rather than
    trusting owned_job_id's mere presence (which is set well before
    promotion is attempted, and stays set even if promotion later fails)."""
    if not signaled:
        # _start_ai_batch_job never completed a handoff decision within the
        # bound (e.g. its own thread died unexpectedly mid-startup) -- do
        # not assume success from silence.
        raise _AiBatchStartupAbortedError("AI batch startup handoff timed out")
    if aborted:
        raise _AiBatchStartupAbortedError("AI batch startup was aborted")
    if not owned_job_id:
        raise _AiBatchStartupAbortedError("AI batch startup has no job ID")
    if _ai_batch_active_worker_job_id(batch_job_id) != owned_job_id:
        raise _AiBatchStartupAbortedError("AI batch worker ownership was not promoted")


_AI_BATCH_MAX_FOLDER_RETRIES = 3


def _ai_batch_folder_id(source_folder: str) -> str:
    raw = _s(source_folder).strip().replace("\\", "/").rstrip("/")
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _ai_batch_state_file(batch_job_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", _s(batch_job_id).strip()) or "unknown"
    return _AI_BATCH_STATE_DIR / f"{safe}.json"


def _ai_batch_json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _ai_batch_json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_ai_batch_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_ai_batch_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return _s(value)


def _ai_batch_persist_job_association(batch_job_id: str, job_id: str) -> None:
    with _ai_batch_state_lock:
        state = _ai_batch_load_state(batch_job_id) or _ai_batch_initial_state(batch_job_id, "")
        state["job_id"] = job_id
        state["batch_job_id"] = batch_job_id
        _ai_batch_write_state(state)


def _ai_batch_load_state(identifier: str) -> Optional[Dict[str, Any]]:
    ident = _s(identifier).strip()
    if not ident:
        return None
    with _ai_batch_state_lock:
        return _get_ai_batch_store().get_batch_state(ident)


def _ai_batch_latest_state() -> Optional[Dict[str, Any]]:
    states = _get_ai_batch_store().list_batch_states()
    return states[0] if states else None


def _ai_batch_find_state(ident: str) -> Optional[Dict[str, Any]]:
    ident = _s(ident).strip()
    if not ident:
        return None
    return _get_ai_batch_store().get_batch_state(ident)


def _ai_batch_control(batch_job_id: str) -> Dict[str, Any]:
    with _ai_batch_control_lock:
        control = _ai_batch_controls.setdefault(batch_job_id, {})
        control.setdefault("pause", False)
        control.setdefault("skip_current", False)
        control.setdefault("skip_folder_ids", set())
        return control


def _ai_batch_initial_state(batch_job_id: str, source_path: str, job_id: str = "") -> Dict[str, Any]:
    now = time.time()
    return {
        "batch_job_id": batch_job_id,
        "job_id": job_id,
        "source_path": source_path,
        "status": "queued",
        "current_step": "queued",
        "total_folders_found": 0,
        "folders_processed": 0,
        "folders_queued": 0,
        "folders_running": 0,
        "folders_completed": 0,
        "folders_failed": 0,
        "folders_skipped": 0,
        "heartbeat_at": now,
        "started_at": now,
        "updated_at": now,
        "completed_at": None,
        "current_folder_names": [],
        "last_completed_folder": "",
        "last_failed_folder": "",
        "last_failed_reason": "",
        "last_error": "",
        "retry_count": 0,
        "ai_max_parallel": _AI_BATCH_MAX_AI_WORKERS,
        "ai_timeout_seconds": _AI_BATCH_AI_TIMEOUT,
        "folder_states": {},
    }


def _ai_batch_find_audio_dirs(root: str) -> List[str]:
    result: List[str] = []
    cursor = None
    try:
        while True:
            res = composite_workflows.discover_import_sources(root, operation="ai_batch_discovery", cursor=cursor)
            if not res.get("ok"):
                raise RuntimeError(f"Engine discovery failed for {root}: {res.get('error_code', 'unknown_error')}")
            candidates = res.get("candidates") or []
            for cand in candidates:
                cpath = cand.get("canonical_path")
                if cpath and cpath not in result:
                    result.append(cpath)
            if res.get("complete") or not res.get("continuation"):
                break
            cursor = res.get("continuation")
        return sorted(result)
    except (BeetsError, BeetsUnavailableError, BeetsAuthError) as exc:
        raise RuntimeError(f"Engine discovery failed for {root}: {exc}") from exc


def _ai_batch_already_in_library(folder_path: str) -> bool:
    try:
        res = composite_workflows.resolve_folder_to_albums(folder_path)
        return bool(int(res.get("track_count") or 0) > 0 or res.get("item_ids"))
    except BeetsUnavailableError:
        raise
    except Exception:
        return False


def _ai_batch_folder_state(batch_job_id: str, source_folder: str) -> Dict[str, Any]:
    return {
        "folder_id": _ai_batch_folder_id(source_folder),
        "batch_job_id": batch_job_id,
        "source_folder": source_folder,
        "status": "queued",
        "current_step": "queued",
        "ai_suggest_status": "not_started",
        "ai_suggest_started_at": None,
        "ai_suggest_completed_at": None,
        "ai_suggest_error": "",
        "review_item_id": "",
        "detected_artist": "",
        "detected_album": "",
        "suggested_release_group_id": "",
        "failure_reason": "",
        "retry_count": 0,
    }


def _ai_batch_queue_pending_review(folder: str, suggestion: Dict[str, Any], evidence: Optional[Dict[str, Any]] = None, batch_job_id: str = "") -> str:
    batch_id = _s(batch_job_id or (evidence or {}).get("batch_job_id") or suggestion.get("source_batch_id") or suggestion.get("batch_job_id") or "")
    origin = {
        "origin_type": "batch_import",
        "origin_label": "Batch",
        "source_batch_id": batch_id,
        "source_folder": folder,
        "created_by_workflow": "ai_batch_import",
    }
    suggestion = dict(suggestion or {})
    suggestion.update({k: v for k, v in origin.items() if v})
    _add_to_pending(folder, suggestion, evidence=evidence, origin=origin)
    return _ai_batch_folder_id(folder)


def _ai_batch_build_evidence(folder: str, sug_result: Dict[str, Any], suggestion: Dict[str, Any], pf_result=None) -> Dict[str, Any]:
    evidence = dict(sug_result.get("evidence") or suggestion.get("review_evidence") or {})
    if not evidence:
        cands = sug_result.get("mb_candidates") or []
        selected = None
        try:
            cand_idx = int(suggestion.get("candidate_index", -1))
        except Exception:
            cand_idx = -1
        if 0 <= cand_idx < len(cands):
            selected = cands[cand_idx]
        evidence = _ai_match_evidence_packet(
            "fresh_import",
            folder_path=folder,
            suggestion=suggestion,
            selected_candidate=selected,
            candidates=cands,
        )
    if pf_result:
        evidence["preflight"] = _compact_preflight(pf_result)
    evidence["thresholds"] = _ai_thresholds_for("fresh_import")
    suggestion["review_evidence"] = evidence
    return evidence


def _ai_batch_run_suggestions(state: Dict[str, Any], log: list, cancel_event=None, update_state=None) -> str:
    folders = state.get("folder_states") or {}
    pending = [fid for fid, f in folders.items() if f.get("status") in {"queued", "ai_queued"} and not f.get("ai_result")]
    active: Dict[str, Dict[str, Any]] = {}
    last_heartbeat_log = 0.0

    def _launch(fid: str) -> None:
        folder = folders[fid]
        source = folder.get("source_folder", "")
        holder: Dict[str, Any] = {"done": False, "started_at": time.time(), "source_folder": source}

        def _target() -> None:
            try:
                if _ai_batch_already_in_library(source):
                    holder["result"] = {"ok": True, "already_in_library": True}
                else:
                    holder["result"] = _ai_suggest_folder_internal(source)
            except Exception as ex:
                holder["result"] = {"ok": False, "error": str(ex)}
            finally:
                holder["done"] = True

        _ai_batch_mark_folder(
            state,
            fid,
            status="ai_running",
            current_step="AI suggestion running",
            ai_suggest_status="running",
            ai_suggest_started_at=holder["started_at"],
            ai_suggest_error="",
        )
        log.append(f"AI suggestion started for folder: {source}")
        thread = threading.Thread(target=_target, daemon=True)
        holder["thread"] = thread
        active[fid] = holder
        thread.start()

    while pending or active:
        control = _ai_batch_control(state["batch_job_id"])
        if cancel_event and cancel_event.is_set():
            for fid in list(active.keys()) + pending:
                folder = folders.get(fid) or {}
                _ai_batch_mark_folder(state, fid, status="skipped", current_step="canceled", failure_reason="batch canceled")
                log.append(f"folder skipped: {folder.get('source_folder', fid)} — batch canceled")
            state["status"] = "canceled"
            state["current_step"] = "canceled by user"
            state["completed_at"] = time.time()
            _ai_batch_commit(state, update_state)
            return "canceled"

        skip_ids = set(control.get("skip_folder_ids") or set())
        if control.get("skip_current"):
            skip_ids.update(active.keys())
            control["skip_current"] = False
        if skip_ids:
            for fid in list(skip_ids):
                if fid in active:
                    folder = folders.get(fid) or {}
                    _ai_batch_mark_folder(state, fid, status="skipped", current_step="skipped by user", failure_reason="skipped by user")
                    active.pop(fid, None)
                    log.append(f"folder skipped: {folder.get('source_folder', fid)}")
                elif fid in pending:
                    folder = folders.get(fid) or {}
                    pending.remove(fid)
                    _ai_batch_mark_folder(state, fid, status="skipped", current_step="skipped by user", failure_reason="skipped by user")
                    log.append(f"folder skipped: {folder.get('source_folder', fid)}")
            control["skip_folder_ids"] = set()
            _ai_batch_commit(state, update_state)

        paused = bool(control.get("pause"))
        while pending and not paused and len(active) < _AI_BATCH_MAX_AI_WORKERS:
            fid = pending.pop(0)
            _ai_batch_mark_folder(state, fid, status="ai_queued", current_step="AI suggestion queued", ai_suggest_status="queued")
            _launch(fid)
            _ai_batch_commit(state, update_state)

        now = time.time()
        for fid, holder in list(active.items()):
            folder = folders.get(fid) or {}
            source = folder.get("source_folder", holder.get("source_folder", fid))
            if holder.get("done"):
                result = holder.get("result") or {"ok": False, "error": "no result"}
                suggestion = result.get("suggestion") or {}
                _ai_batch_mark_folder(
                    state,
                    fid,
                    status="ai_completed" if result.get("ok") else "ai_failed",
                    current_step="AI suggestion completed" if result.get("ok") else "AI suggestion failed",
                    ai_suggest_status="completed" if result.get("ok") else "failed",
                    ai_suggest_completed_at=now,
                    ai_suggest_error="" if result.get("ok") else _s(result.get("error") or "AI suggestion failed"),
                    detected_artist=_s(suggestion.get("albumartist") or suggestion.get("artist")),
                    detected_album=_s(suggestion.get("album")),
                    suggested_release_group_id=_s(suggestion.get("mb_releasegroupid")),
                    failure_reason="" if result.get("ok") else _s(result.get("error") or "AI suggestion failed"),
                    ai_result=result,
                )
                if result.get("ok"):
                    log.append(f"AI suggestion completed for folder: {source}")
                else:
                    log.append(f"folder failed with reason: {source} — {_s(result.get('error') or 'AI suggestion failed')}")
                active.pop(fid, None)
                _ai_batch_commit(state, update_state)
            elif now - float(holder.get("started_at") or now) > _AI_BATCH_AI_TIMEOUT:
                reason = f"AI suggestion timed out after {_AI_BATCH_AI_TIMEOUT}s"
                _ai_batch_mark_folder(
                    state,
                    fid,
                    status="timed_out",
                    current_step="AI suggestion timed out",
                    ai_suggest_status="timed_out",
                    ai_suggest_completed_at=now,
                    ai_suggest_error=reason,
                    failure_reason=reason,
                )
                log.append(f"AI suggestion timed out for folder: {source} — {reason}")
                active.pop(fid, None)
                _ai_batch_commit(state, update_state)

        if paused and not active:
            state["status"] = "paused"
            state["current_step"] = "paused"
            _ai_batch_commit(state, update_state)
            return "paused"

        if now - last_heartbeat_log >= 30:
            log.append("batch heartbeat updated")
            last_heartbeat_log = now
        state["current_step"] = "Gathering AI suggestions"
        _ai_batch_commit(state, update_state)
        time.sleep(0.5)

    return "done"


def _ai_batch_import_failure_outcome(error_text: str) -> Dict[str, str]:
    err = _s(error_text)
    if _is_music_format_policy_handled_error(err):
        return {
            "status": "policy_rejected",
            "current_step": "audio policy handled",
            "reason": _music_format_policy_review_note(err),
        }
    return {
        "status": "import_failed",
        "current_step": "import failed; queued for review",
        "reason": err or "Import failed",
    }


def _ai_batch_process_decisions(state: Dict[str, Any], log: list, cancel_event=None, update_state=None) -> Dict[str, int]:
    imported = already_cnt = queued = errors = skipped = warnings = replacements = 0
    folders = state.get("folder_states") or {}
    total = len(folders)
    last_commit_at = 0.0

    def _commit(force: bool = False) -> None:
        nonlocal last_commit_at
        now = time.time()
        if force or now - last_commit_at >= 5:
            _ai_batch_commit(state, update_state)
            last_commit_at = now

    for idx, fid in enumerate(sorted(folders, key=lambda key: _s(folders[key].get("source_folder"))), start=1):
        folder_state = folders[fid]
        folder = folder_state.get("source_folder", "")
        fname = Path(folder).name
        if cancel_event and cancel_event.is_set():
            state["status"] = "canceled"
            state["current_step"] = "canceled by user"
            state["completed_at"] = time.time()
            _commit(True)
            break
        status = _ai_batch_effective_folder_status(folder_state)
        if status in (_AI_BATCH_IMPORTED_FOLDER_STATUSES | _AI_BATCH_SKIPPED_FOLDER_STATUSES | _AI_BATCH_FAILED_FOLDER_STATUSES | _AI_BATCH_REVIEW_FOLDER_STATUSES | _AI_BATCH_POLICY_WARNING_STATUSES | _AI_BATCH_REPLACEMENT_FOLDER_STATUSES):
            if status in _AI_BATCH_SKIPPED_FOLDER_STATUSES:
                skipped += 1
                reason = folder_state.get("failure_reason") or "skipped by user"
                if reason != "batch canceled" and not folder_state.get("review_item_id") and not _pending_review_has_path(folder):
                    suggestion = {
                        "artist": folder_state.get("detected_artist") or Path(folder).name,
                        "album": folder_state.get("detected_album") or Path(folder).name,
                        "albumartist": folder_state.get("detected_artist") or Path(folder).name,
                        "confidence": "low",
                        "reason": reason,
                    }
                    review_id = _ai_batch_queue_pending_review(folder, suggestion, evidence={"ai_batch_skip": reason, "status": status, "batch_job_id": state.get("batch_job_id", "")}, batch_job_id=state.get("batch_job_id", ""))
                    folder_state["review_item_id"] = review_id
                    log.append(f"review item created: {folder}")
                    _commit()
            elif status in _AI_BATCH_FAILED_FOLDER_STATUSES:
                errors += 1
                if not folder_state.get("review_item_id") and not _pending_review_has_path(folder):
                    reason = folder_state.get("failure_reason") or folder_state.get("ai_suggest_error") or "AI suggestion failed"
                    suggestion = {
                        "artist": folder_state.get("detected_artist") or Path(folder).name,
                        "album": folder_state.get("detected_album") or Path(folder).name,
                        "albumartist": folder_state.get("detected_artist") or Path(folder).name,
                        "confidence": "low",
                        "reason": reason,
                    }
                    review_id = _ai_batch_queue_pending_review(folder, suggestion, evidence={"ai_batch_failure": reason, "status": status, "batch_job_id": state.get("batch_job_id", "")}, batch_job_id=state.get("batch_job_id", ""))
                    folder_state["review_item_id"] = review_id
                    log.append(f"review item created: {folder}")
                    _commit()
            elif status in _AI_BATCH_IMPORTED_FOLDER_STATUSES:
                imported += 1
            elif status in _AI_BATCH_REVIEW_FOLDER_STATUSES:
                queued += 1
            elif status in _AI_BATCH_REPLACEMENT_FOLDER_STATUSES:
                replacements += 1
            else:
                warnings += 1
            continue
        result = folder_state.get("ai_result") or {}
        if not result:
            continue
        state["current_step"] = f"Processing {fname}"
        _ai_batch_mark_folder(state, fid, current_step="processing decision")
        _commit()
        log.append(f"\n[{idx}/{total}] {fname}")

        if result.get("already_in_library"):
            log.append("  Already in library - skipped; no source folder deleted")
            _ai_batch_mark_folder(state, fid, status="skipped", current_step="already in library", failure_reason="already in library")
            already_cnt += 1
            _commit()
            continue

        if not result.get("ok"):
            err = _s(result.get("error") or "AI suggestion failed")
            log.append(f"  AI failed: {err}")
            review_id = _ai_batch_queue_pending_review(folder, {"confidence": "low", "reason": f"AI failed: {err}"}, evidence=result.get("evidence") or None, batch_job_id=state.get("batch_job_id", ""))
            _ai_batch_mark_folder(state, fid, status="ai_failed", current_step="AI failed; queued for review", review_item_id=review_id, failure_reason=err)
            queued += 1
            errors += 1
            _commit()
            continue

        suggestion = result.get("suggestion") or {}
        conf = _s(suggestion.get("confidence", "low")).lower() or "low"
        mb_id = _s(suggestion.get("mb_albumid") or "").strip()
        mb_valid = suggestion.get("mb_valid", False)
        log.append(f"  {suggestion.get('albumartist','')} - {suggestion.get('album','')} ({suggestion.get('year','')})")
        log.append(f"  Confidence: {conf}" + (f"  MB: {mb_id[:8]}..." if mb_id else ""))
        if suggestion.get("mb_url"):
            log.append(f"  {suggestion['mb_url']}")

        if _ai_conf_at_least(conf, "medium") and mb_valid and mb_id:
            pf = _folder_release_preflight(folder, mb_id)
            match_ratio = _preflight_match_ratio(pf)
            auto_ok = _ai_auto_import_allowed("fresh_import", conf, pf, mb_valid, mb_id)
            if auto_ok:
                log.append(
                    f"  Preflight ok ({pf.get('matches',0)}/{pf.get('expected',0)} tracks, conf={conf}) - importing..."
                )
                try:
                    import_result = _ai_import_folder(folder, mb_id, suggestion, log, cancel_event)
                    if not import_result:
                        # Every real return path from _ai_import_folder returns
                        # a populated dict; a falsy result here means it
                        # returned without raising but without confirming
                        # anything, which is itself a failure -- raise so the
                        # existing except-branch below handles it (queued for
                        # review), instead of defaulting to false/unknown
                        # fields while still marking this folder "imported".
                        raise RuntimeError("Import completed without a verifiable result")
                    imported += 1
                    _ai_batch_mark_folder(
                        state, fid, status="imported", current_step="imported",
                        metadata_imported=bool(import_result.get("metadata_imported", False)),
                        identity_verified=bool(import_result.get("identity_verified", False)),
                        artwork_status=_s(import_result.get("artwork_status") or "unknown"),
                        artwork_retryable=bool(import_result.get("artwork_retryable", False)),
                        album_id=import_result.get("album_id"),
                    )
                except Exception as ex:
                    outcome = _ai_batch_import_failure_outcome(str(ex))
                    log.append(f"  Import outcome: {outcome['reason']}")
                    if outcome["status"] == "policy_rejected":
                        warnings += 1
                        _ai_batch_mark_folder(
                            state,
                            fid,
                            status=outcome["status"],
                            current_step=outcome["current_step"],
                            failure_reason=outcome["reason"],
                        )
                    else:
                        kept_ids = getattr(ex, "kept_album_ids", None)
                        review_sug = {**suggestion, "kept_album_ids": kept_ids} if kept_ids else suggestion
                        review_id = _ai_batch_queue_pending_review(folder, review_sug, evidence=_ai_batch_build_evidence(folder, result, suggestion, pf), batch_job_id=state.get("batch_job_id", ""))
                        _ai_batch_mark_folder(
                            state,
                            fid,
                            status=outcome["status"],
                            current_step=outcome["current_step"],
                            review_item_id=review_id,
                            failure_reason=outcome["reason"],
                        )
                        queued += 1
                        errors += 1
            elif pf.get("scan_unavailable"):
                # Wave 26 Docker acceptance round: this is NOT "the AI's
                # evidence is ambiguous, a human should decide" -- it is
                # "we could not even reach the engine to gather evidence",
                # an infrastructure/connectivity failure. Routing it
                # through the same terminal "review_created" status as a
                # genuine low-confidence match permanently stranded the
                # folder with no automated retry path (found live: after
                # the engine came back online, /api/ai-batch-import/recover
                # with retry_failed=true never re-attempted this folder at
                # all, because "review_created" is not in
                # _AI_BATCH_RETRYABLE_FOLDER_STATUSES -- confirmed via a
                # real folder_states dump on 4 consecutive real CI runs,
                # not a guess). Marked retryable instead, matching the
                # existing _ai_batch_import_failure_outcome default-branch
                # contract used elsewhere in this same function.
                pf_note = pf.get("error") or "Could not scan source folder."
                log.append(f"  Scan unavailable, marked retryable: {pf_note}")
                _ai_batch_mark_folder(
                    state, fid, status="import_failed",
                    current_step="scan unavailable; queued for retry",
                    failure_reason=pf_note,
                )
                queued += 1
                errors += 1
            else:
                if pf.get("ok"):
                    pf_note = (
                        f"{conf} confidence with {pf.get('matches',0)}/{pf.get('expected',0)} tracks matched "
                        f"({match_ratio:.0%}; {_AI_BATCH_MEDIUM_RATIO:.0%} required for medium auto-import)"
                    )
                else:
                    pf_note = pf.get("error") or f"preflight: {pf.get('matches',0)}/{pf.get('expected',0)} tracks matched"
                    if conf == _AI_BATCH_MIN_CONF:
                        suggestion["confidence"] = "medium"
                log.append(f"  Queued for review: {pf_note}")
                suggestion["reason"] = f"[{pf_note}] " + (suggestion.get("reason") or "")
                review_id = _ai_batch_queue_pending_review(folder, suggestion, evidence=_ai_batch_build_evidence(folder, result, suggestion, pf), batch_job_id=state.get("batch_job_id", ""))
                _ai_batch_mark_folder(state, fid, status="review_created", current_step="review item created", review_item_id=review_id)
                log.append(f"review item created: {folder}")
                queued += 1
        else:
            log.append(f"  {conf.capitalize()} confidence - queued for review")
            review_id = _ai_batch_queue_pending_review(folder, suggestion, evidence=_ai_batch_build_evidence(folder, result, suggestion), batch_job_id=state.get("batch_job_id", ""))
            _ai_batch_mark_folder(state, fid, status="review_created", current_step="review item created", review_item_id=review_id)
            log.append(f"review item created: {folder}")
            queued += 1
        _commit()
    _commit(True)
    return {"imported": imported, "already": already_cnt, "queued": queued, "errors": errors, "skipped": skipped, "warnings": warnings, "replacements": replacements}


def _run_ai_batch_import(batch_job_id: str, scan_path: str, log: list, cancel_event=None, update_state=None, *, recover: bool = False, retry_failed: bool = False, job_id: str = "") -> Dict[str, Any]:
    state = _ai_batch_load_state(batch_job_id) if recover else None
    if not state:
        state = _ai_batch_initial_state(batch_job_id, scan_path, job_id=job_id)
    state["batch_job_id"] = batch_job_id
    state["source_path"] = scan_path
    if job_id:
        # The JobStore job_id is only known to the caller after jobs.start_python()
        # returns; stamp it here (on the same state object this thread commits
        # repeatedly) so later commits from this worker never clobber it back to "".
        state["job_id"] = job_id
    if recover and not retry_failed:
        if _ai_batch_recalculate_batch_state(state, log) or state.get("status") in _AI_BATCH_TERMINAL_STATUSES:
            log.append("No unfinished folder work remains; recovery finalized existing batch.")
            _ai_batch_commit(state, update_state, heartbeat=False)
            return _ai_batch_public_state(state)
    state["status"] = "running"
    state["current_step"] = "source scan started"
    state["last_error"] = ""
    if recover:
        state["retry_count"] = int(state.get("retry_count") or 0) + 1
    log.append("batch created" if not recover else ("batch retry started" if retry_failed else "batch recovery started"))
    log.append(f"source scan started: {scan_path}")

    folder_states = state.setdefault("folder_states", {})
    if not recover or not folder_states:
        # Commit "running" before the (potentially slow) disk walk below so a
        # poller sees progress immediately. Deliberately NOT done for the
        # recover/retry branch: _ai_batch_commit unconditionally recalculates
        # batch status from folder_states, and pre-reconciliation folder
        # data would make it finalize a terminal status before reconciliation
        # runs. The single commit after the if/else below always sees
        # fully-reconciled folder_states first.
        _ai_batch_commit(state, update_state)
        folders = _ai_batch_find_audio_dirs(scan_path)
        log.append(f"folders discovered count: {len(folders)}")
        for folder in folders:
            fid = _ai_batch_folder_id(folder)
            folder_states[fid] = _ai_batch_folder_state(batch_job_id, folder)
            log.append(f"folder queued: {folder}")
    else:
        log.append(f"loaded existing batch with {len(folder_states)} folder state(s)")
        pending_review_paths = _pending_review_path_set()
        requeued = 0
        terminal = 0
        decision_ready = 0
        for fid, folder in list(folder_states.items()):
            src = folder.get("source_folder", "")
            status = _ai_batch_effective_folder_status(folder)
            if retry_failed and status in _AI_BATCH_RETRYABLE_FOLDER_STATUSES:
                prior_retries = int(folder.get("retry_count") or 0)
                if prior_retries >= _AI_BATCH_MAX_FOLDER_RETRIES:
                    # Status/failure_reason are deliberately left as-is
                    # ("failed"/"timed_out"/etc) rather than switched to a
                    # new status value: the existing operator UI already
                    # renders that status as an attention-needing failure,
                    # and switching to an unrecognized status risks the
                    # folder silently disappearing from the attention list.
                    # retry_exhausted/manual_review_required are the signal
                    # that further automatic retry_failed=true calls must
                    # not touch this folder (see _ai_batch_recompute_counts'
                    # folders_retryable exclusion below).
                    _ai_batch_mark_folder(
                        state, fid,
                        retry_exhausted=True,
                        max_retries=_AI_BATCH_MAX_FOLDER_RETRIES,
                        manual_review_required=True,
                        current_step=f"retry limit ({_AI_BATCH_MAX_FOLDER_RETRIES}) reached; needs manual review",
                    )
                    terminal += 1
                    continue
                folder.update({
                    "status": "ai_queued",
                    "current_step": "retryable failure requeued",
                    "ai_suggest_status": "queued",
                    "failure_reason": "",
                    "ai_suggest_error": "",
                    "retry_count": prior_retries + 1,
                })
                requeued += 1
                continue
            if status in _AI_BATCH_UNFINISHED_FOLDER_STATUSES and _pending_review_path_key(src) in pending_review_paths:
                _ai_batch_mark_folder(state, fid, status="review_created", current_step="review item already exists", review_item_id=fid)
                terminal += 1
                continue
            if status == "ai_completed" and folder.get("ai_result"):
                decision_ready += 1
                continue
            if status == "ai_running":
                reason = "stale AI-running folder recovered"
                _ai_batch_mark_folder(state, fid, status="timed_out", current_step="stale AI suggestion timed out", ai_suggest_status="timed_out", ai_suggest_error=reason, failure_reason=reason)
                terminal += 1
            elif status in {"scanning", "queued", "ai_queued"}:
                folder.update({"status": "ai_queued", "current_step": "requeued by recovery", "ai_suggest_status": "queued"})
                requeued += 1
            else:
                terminal += 1
        log.append(f"Reconciled state: {terminal} terminal, {requeued} requeued, {decision_ready} cached decision(s) ready")
    _ai_batch_commit(state, update_state)
    if state.get("status") in _AI_BATCH_TERMINAL_STATUSES:
        log.append("No unfinished folder work remains; batch is terminal.")
        return _ai_batch_public_state(state)

    ai_status = _ai_batch_run_suggestions(state, log, cancel_event, update_state)
    if ai_status in {"canceled", "paused"}:
        return _ai_batch_public_state(state)

    state["current_step"] = "processing AI decisions"
    _ai_batch_commit(state, update_state)
    summary = _ai_batch_process_decisions(state, log, cancel_event, update_state)
    _invalidate_lib_cache()

    if state.get("status") not in {"canceled", "paused"}:
        _ai_batch_recalculate_batch_state(state, log)
        if state.get("status") not in _AI_BATCH_TERMINAL_STATUSES:
            state["completed_at"] = time.time()
            state["status"] = "completed_with_warnings" if state.get("folders_attention") else "completed"
            state["current_step"] = "completed with warnings" if state.get("folders_attention") else "completed"
            state["batch_summary"] = _ai_batch_terminal_summary(state)
        log.append("batch completed with warnings" if state.get("status") == "completed_with_warnings" else "batch completed")
        log.append(
            f"Done: {summary.get('imported', 0)} imported, {summary.get('already', 0)} skipped already in library, "
            f"{summary.get('queued', 0)} queued for review, {summary.get('warnings', 0)} warning(s), "
            f"{summary.get('replacements', 0)} replacement handoff(s), {summary.get('errors', 0)} true failure(s), "
            f"{summary.get('skipped', 0)} skipped"
        )
        _ai_batch_commit(state, update_state)
    return _ai_batch_public_state(state)


def _ai_import_folder(folder_path: str, mb_albumid: str, suggestion: dict,
                      log: list, cancel_event=None):
    """Import a folder as a specific MB release through Beets' own importer
    (confirmed import), which tags, writes and places the files. Web Manager
    does not retag the result (ARCH-024). A preserved torrent source is
    copied, never moved. Runs directly -- safe for background threads."""
    preserve_torrent_source = _preserve_torrent_source_path(folder_path)
    if preserve_torrent_source:
        log.append("  [torrent] Protected source detected; Beets copies it, "
                   "leaving the qBittorrent source in place.")

    # ── Step 1: confirmed_import_v1 (Plan -> Apply) with the AI-reviewed,
    # human/AI-approved release ──────────────────────────────────────────
    # Wave 26 correction: this previously called composite_workflows.reimport_source
    # (POST /imports/reimport, reimport_source_atomic's
    # verify_deterministic_identity() gate) -- the exact same trust-model
    # mismatch Wave 25 already fixed for import_folder_with_id.
    # verify_deterministic_identity() requires the source audio's OWN
    # embedded MusicBrainz tags to already match the target; AI-suggested
    # candidates are by construction reviewed matches for PREVIOUSLY-
    # UNTAGGED source audio, which never has that by construction. Fixed
    # to compose confirmed_import_v1 (create_confirmed_import_plan /
    # execute_confirmed_import_apply via plan_confirmed_import /
    # apply_confirmed_import), which binds authorization to an immutable
    # source-manifest digest + this already-resolved concrete Release ID
    # + best-effort track/fingerprint alignment instead of pre-existing
    # embedded tags -- exactly import_folder_with_id's own composition.
    # reimport_source_atomic()/verify_deterministic_identity() themselves
    # remain untouched and still correctly gate genuine reimports of
    # already-tagged library content elsewhere.
    #
    # This also eliminates the dead code this replaced: a temp Beets
    # config (`/tmp/beets_ai_batch.yaml`, including the ENGINE's own
    # /config/config.yaml) was written here and assigned to a `base`
    # command-array variable that was never actually passed to any
    # subprocess call in this function -- the real import always ran
    # through the (wrong-trust-model) reimport_source() HTTP call, not a
    # locally-executed `beet` invocation. The Web Manager has no local
    # Beets binary/engine config to invoke in the two-service topology in
    # the first place.
    # The AI-chosen candidate is imported as chosen: never swapped for another
    # Release (or Release Group) here.
    _validate_import_source_audio(folder_path, log, reject_downloads=True)
    mb_identity = _fetch_mb_release_tracklist(mb_albumid, log)
    if not mb_identity.get("ok"):
        raise RuntimeError(
            "The selected MusicBrainz release could not be loaded. Import was not started."
        )
    resolved_releasegroupid = _s(mb_identity.get("release_group") or "").strip().lower()
    if not resolved_releasegroupid:
        raise RuntimeError("The Release Group of the selected MusicBrainz release is unknown, so the "
                           "import could not be verified. Import was not started.")
    log.append(f"[import] Canonical MusicBrainz release-group ID: {resolved_releasegroupid}")

    plan_res = composite_workflows.plan_confirmed_import({
        "source_folder": folder_path,
        "mb_albumid": mb_albumid,
        "mb_releasegroupid": resolved_releasegroupid,
        "mb_release_group_resolved": resolved_releasegroupid,
        "mb_tracks": mb_identity.get("tracks") or [],
        "use_move": not preserve_torrent_source,
    })
    if not plan_res.get("ok"):
        raise RuntimeError(f"Import planning failed: {plan_res.get('error') or 'unknown error'}")
    try:
        atomic_res = composite_workflows.apply_confirmed_import(plan_res["operation_id"])
    except (BeetsError, BeetsUnavailableError) as ex:
        diag = getattr(ex, "diagnostics", None) or {}
        if diag.get("returncode") is not None:
            log.append(f"  Native Beets exit code: {diag['returncode']}")
        if diag.get("stdout_excerpt"):
            log.append(f"  Native Beets stdout: {diag['stdout_excerpt']}")
        if diag.get("stderr_excerpt"):
            log.append(f"  Native Beets stderr: {diag['stderr_excerpt']}")
        raise RuntimeError(f"Beets import failed: {ex}")
    if not atomic_res.get("ok"):
        kept = atomic_res.get("album_ids") or []
        msg = f"Beets import failed: {atomic_res.get('error', 'confirmed import apply failed')}"
        if kept:  # verification failed after Beets imported: the rows stay
            msg += f" Album_id {', '.join(str(i) for i in kept)} was left in the library for review."
        err = RuntimeError(msg)
        err.kept_album_ids = kept  # type: ignore[attr-defined]  # read by the batch review queue
        raise err

    # Beets applied the release; apply_confirmed_import verified exactly one
    # new album with that Release ID and Release Group.
    aid = int(atomic_res["album_id"])
    log.append(f"  Beets imported album_id={aid} as release {mb_albumid} (verified).")
    if cancel_event and cancel_event.is_set():
        raise RuntimeError("cancelled")

    # ── Step 4: verify persisted identity, then fetch artwork ────────────────
    art_outcome = _fetch_artwork_after_retag(int(aid), mb_albumid, log, cancel_event=cancel_event)
    identity_verified = art_outcome["identity_verified"]
    artwork_status = art_outcome["artwork_status"]
    artwork_retryable = art_outcome["artwork_retryable"]

    # Record AI match in history
    if suggestion:
        try:
            _record_ai_match(folder_path, suggestion)
        except Exception:
            pass

    _remove_pending_review_for_path(folder_path, log)
    try:
        imported_album = lib.get_album(int(aid))
        if imported_album:
            _auto_merge_case_duplicate_artist_folder(
                str(MUSIC_ROOT), _s(getattr(imported_album, "albumartist", "") or ""), log,
            )
    except Exception as ex:
        log.append(f"  [auto-dedup] artist-folder check skipped: {ex}")
    _invalidate_lib_cache()
    _trigger_plex_refresh(log, workflow="batch")
    if artwork_status in ("fetched", "already_present"):
        log.append("  ✓ Done")
    else:
        log.append("  ✓ Done (metadata); artwork needs retry")
    return {
        "album_id": int(aid),
        "metadata_imported": True,
        "identity_verified": identity_verified,
        "artwork_status": artwork_status,
        "artwork_retryable": artwork_retryable,
    }


def _auto_merge_case_duplicate_artist_folder(root: str, albumartist: str,
                                             log: Optional[List[str]] = None) -> bool:
    """Call right after an import lands a new album folder. If the artist
    folder that import just used is a case/punctuation-only duplicate of an
    existing one (e.g. "aaliyah" vs "Aaliyah"), merge it into the existing
    folder immediately instead of leaving a new duplicate for someone to
    notice later. Skips the MusicBrainz canonical-name lookup so it stays
    cheap enough to run after every import without a network round trip;
    the manual Clean-page merge (which does use MusicBrainz) still runs
    the occasional slower pass for names it doesn't catch this way."""
    name = _s(albumartist).strip()
    if not name:
        return False
    key = _artist_folder_key(name)
    if not key:
        return False
    try:
        groups = _scan_artist_folder_groups(root, use_musicbrainz=False, only_keys={key})
    except Exception as ex:
        if log is not None:
            log.append(f"  [auto-dedup] artist-folder check skipped: {ex}")
        return False
    if not groups:
        return False
    merge_log: List[str] = []
    try:
        summary = _apply_artist_folder_groups(root, [key], False, merge_log, use_musicbrainz=False)
    except Exception as ex:
        if log is not None:
            log.append(f"  [auto-dedup] artist-folder merge failed: {ex}")
        return False
    if log is not None and summary.get("folders"):
        log.append(f"  [auto-dedup] Merged {summary['folders']} duplicate artist folder(s) for '{name}' "
                    f"found on disk (case/punctuation variant of an existing folder).")
    return bool(summary.get("folders"))


_AI_REVIEW_DECISIONS_FILE = Path("/config/ai_review_decisions.json")


_AI_BATCH_AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aiff", ".wv", ".ape"}


_AI_BATCH_FORMAT_POLICY_HANDLED_MESSAGE = _MUSIC_FORMAT_POLICY_HANDLED_MESSAGE
