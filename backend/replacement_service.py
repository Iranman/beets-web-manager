"""Format-policy replacement: preferences, rejection and upgrade decisions (ARCH-001).
"""

from __future__ import annotations

import os, re, time, unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from backend.app_runtime import AUDIO_EXT, MUSIC_ROOT, _MB_UUID_RE, _s
from backend.ai_evidence_service import _track_ai_similarity
from backend.acquisition_service import start_album_download
from backend.playlist_service import _music_format_preferences
from backend.audio_preferences import load_replacement_statuses as _load_music_format_replacement_statuses, mark_needs_replacement as _mark_music_format_needs_replacement, validate_audio_file as _validate_audio_file_preferences
from helpers_mb import _fetch_mb_recording_details, _mb_recording_search, _clean_for_mb, _resolve_release_group_to_release, _fetch_mb_release_candidate
from backend.beets_adapter import beets_adapter
import backend.composite_workflows as composite_workflows
import backend.recording_review as recording_review
from backend.acoustid_service import _acoustid_fingerprint_ids, _acoustid_fingerprint_match, _acoustid_lookup_cached, _playlist_artist_name_score, _playlist_title_score, _read_file_media_tags
from backend.job_service import _wait_for_child_job
from backend.serializers import _json_from_flask_response

# ── ARCH-001 extracted code ──


def _music_format_library_rows(limit: int = 0) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    cleanup_rows = beets_adapter.get_album_cleanup_index()

    cleanup_rows.sort(key=lambda r: (
        int(r.get("item_album_id") or r.get("album_id") or 0),
        int(r.get("item_track") or r.get("track") or 0),
        int(r.get("item_id") or r.get("id") or 0),
    ))

    for r in cleanup_rows:
        raw_path = _s(r.get("item_path") or r.get("path") or "")
        path = Path(raw_path)
        if not path.is_absolute():
            path = MUSIC_ROOT / raw_path
        if path.suffix.lower() not in AUDIO_EXT:
            continue
        year_val = _s(r.get("album_year") or r.get("year") or "").strip()
        rows.append({
            "item_id": int(r.get("item_id") or r.get("id") or 0),
            "album_id": int(r.get("item_album_id") or r.get("album_id") or 0),
            "path": str(path),
            "title": _s(r.get("item_title") or r.get("title") or ""),
            "artist": _s(r.get("item_artist") or r.get("artist") or ""),
            "album": _s(r.get("item_album") or r.get("album") or ""),
            "albumartist": _s(r.get("album_albumartist") or r.get("albumartist") or ""),
            "disc": int(r.get("item_disc") or r.get("disc") or 1),
            "track": int(r.get("item_track") or r.get("track") or 0),
            "year": int(year_val) if year_val.isdigit() else 0,
            "mb_trackid": _s(r.get("item_mb_trackid") or r.get("mb_trackid") or ""),
            "mb_albumid": _s(r.get("item_mb_albumid") or r.get("mb_albumid") or r.get("album_mb_albumid") or ""),
            "mb_releasegroupid": _s(r.get("album_mb_releasegroupid") or r.get("mb_releasegroupid") or ""),
        })
        if limit and limit > 0 and len(rows) >= limit:
            break
    return rows


def _music_format_scan_library(log: list, cancel_event=None, update_state=None):
    prefs = _music_format_preferences()
    limit = int(getattr(_music_format_scan_library, "limit", 0) or 0)
    rows = _music_format_library_rows(limit)
    total = len(rows)
    non_compliant: List[Dict[str, Any]] = []
    log.append(f"Scanning {total} library track(s) against Music Format Preferences")
    for index, row in enumerate(rows, start=1):
        if cancel_event is not None and cancel_event.is_set():
            log.append("Scan cancelled; no files were removed")
            break
        path = Path(row["path"])
        if not path.exists():
            continue
        result = _validate_audio_file_preferences(str(path), prefs)
        if result.get("ok"):
            if index <= 5:
                log.append(f"  [audio] {result.get('message')}")
        else:
            reason = "; ".join(result.get("reasons") or ["does not match Music Format Preferences"])
            status = "Replacement queued" if prefs.get("replacement_fallback", {}).get("queue_retry") else "Non-compliant but temporarily kept"
            record = {
                **row,
                "status": "Needs replacement",
                "replacement_status": status,
                "reason": reason,
                "audio": result.get("properties") or {},
                "queued_retry": bool(prefs.get("replacement_fallback", {}).get("queue_retry")),
            }
            non_compliant.append(record)
            log.append(f"  [audio] {result.get('message')}: {row.get('artist')} - {row.get('title')}")
            log.append("  No replacement found: keeping current file and marking Needs replacement")
            if record["queued_retry"]:
                log.append("  Queued retry: no compliant source available")
        if update_state and (index == total or index % 100 == 0):
            update_state({"phase": "Scanning", "processed": index, "total": total, "needs_replacement": len(non_compliant)})
    if non_compliant:
        _mark_music_format_needs_replacement(non_compliant)
    log.append(f"Music format scan complete: {len(non_compliant)} track(s) need replacement")
    return {"total": total, "needs_replacement": len(non_compliant), "tracks": non_compliant[:200]}


def _music_format_hydrate_status_row(row: Dict[str, Any], library_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    item_id = int(row.get("item_id") or 0)
    raw_path = _s(row.get("path") or "")
    for candidate in library_rows:
        if item_id and int(candidate.get("item_id") or 0) == item_id:
            merged = dict(row)
            merged.update(candidate)
            return merged
    if raw_path:
        raw_resolved = str((Path(raw_path) if Path(raw_path).is_absolute() else MUSIC_ROOT / raw_path).resolve(strict=False)).casefold()
        for candidate in library_rows:
            cand_path = _s(candidate.get("path") or "")
            if str(Path(cand_path).resolve(strict=False)).casefold() == raw_resolved:
                merged = dict(row)
                merged.update(candidate)
                return merged
    return dict(row)


_MUSIC_FORMAT_REPLACEMENT_MAX_ATTEMPTS = int(os.environ.get("MUSIC_FORMAT_REPLACEMENT_MAX_ATTEMPTS", "3") or "3")


_MUSIC_FORMAT_REPLACEMENT_BACKOFF_SECONDS = int(os.environ.get("MUSIC_FORMAT_REPLACEMENT_BACKOFF_SECONDS", "900") or "900")


def _music_format_replacement_norm(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _s(value)).casefold().replace("&", " and ")
    text = re.sub(r"[\u2010-\u2015]+", "-", text)
    text = re.sub(r"['`´‘’]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _music_format_replacement_key(row: Dict[str, Any]) -> str:
    for key in ("mb_trackid", "identity_mb_trackid", "resolved_mb_trackid", "acoustid_mb_trackid"):
        value = _s(row.get(key) or "").strip().lower()
        if value:
            return f"mb:{value}"
    artist = _music_format_replacement_norm(row.get("artist") or row.get("albumartist") or "")
    title = _music_format_replacement_norm(row.get("title") or "")
    if artist or title:
        return f"text:{artist}|{title}"
    fallback = _s(row.get("path") or row.get("item_id") or "").strip()
    return f"row:{fallback}" if fallback else ""


def _music_format_retry_delay(attempt_count: int) -> int:
    attempt = max(1, int(attempt_count or 1))
    return min(86400, _MUSIC_FORMAT_REPLACEMENT_BACKOFF_SECONDS * (2 ** max(0, attempt - 1)))


def _music_format_retry_allowed(row: Dict[str, Any], now: float, *, reset_retry_state: bool = False) -> Tuple[bool, str]:
    if reset_retry_state:
        return True, ""
    if row.get("retryable") is False or row.get("queued_retry") is False:
        return False, _s(row.get("failure_reason") or row.get("reason") or "not retryable")
    attempts = int(row.get("attempt_count") or row.get("retry_attempt_count") or 0)
    if attempts >= _MUSIC_FORMAT_REPLACEMENT_MAX_ATTEMPTS:
        return False, f"retry limit reached ({attempts}/{_MUSIC_FORMAT_REPLACEMENT_MAX_ATTEMPTS})"
    next_retry = float(row.get("next_retry_at") or 0)
    if next_retry and next_retry > now:
        return False, f"next retry after {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(next_retry))}"
    return True, ""


def _music_format_retry_state(row: Dict[str, Any], *, status: str, stage: str,
                              reason: str, retryable: bool,
                              attempt_count: Optional[int] = None) -> Dict[str, Any]:
    now = time.time()
    attempts = int(attempt_count if attempt_count is not None else (row.get("attempt_count") or row.get("retry_attempt_count") or 0))
    next_retry = 0 if not retryable else now + _music_format_retry_delay(max(1, attempts))
    return {
        **row,
        "status": status,
        "replacement_status": status,
        "reason": reason,
        "failure_stage": stage,
        "failure_reason": reason,
        "retryable": bool(retryable),
        "queued_retry": bool(retryable),
        "attempt_count": attempts,
        "last_attempt_at": now,
        "next_retry_at": next_retry,
        "replacement_identity_key": _music_format_replacement_key(row),
    }


def _music_format_read_embedded_identity(row: Dict[str, Any]) -> Dict[str, Any]:
    path_text = _s(row.get("path") or "")
    if not path_text:
        return {}
    path = Path(path_text)
    if not path.is_absolute():
        path = MUSIC_ROOT / path_text
    if not path.is_file():
        return {}
    try:
        tags = _read_file_media_tags(path)
        return {
            "artist": _s(tags.get("artist", "") or ""),
            "albumartist": _s(tags.get("albumartist", "") or ""),
            "title": _s(tags.get("title", "") or ""),
            "album": _s(tags.get("album", "") or ""),
            "mb_trackid": _s(tags.get("mb_trackid", "") or ""),
            "mb_albumid": _s(tags.get("mb_albumid", "") or ""),
        }
    except Exception:
        return {}


def _music_format_resolve_replacement_identity(row: Dict[str, Any], log: list) -> Dict[str, Any]:
    label = f"{row.get('artist') or 'Unknown artist'} - {row.get('title') or 'Unknown track'}"
    log.append(f"Resolving identity: {label}")
    resolved = dict(row)
    embedded = _music_format_read_embedded_identity(row)
    for key in ("artist", "albumartist", "title", "album", "mb_trackid", "mb_albumid"):
        if embedded.get(key) and not _s(resolved.get(key) or "").strip():
            resolved[key] = embedded[key]

    artist = _s(resolved.get("artist") or resolved.get("albumartist") or "").strip()
    title = _s(resolved.get("title") or "").strip()
    clean_title, clean_artist = _clean_for_mb(title, artist)
    if clean_title:
        resolved["title"] = clean_title
    if clean_artist:
        resolved["artist"] = clean_artist
    artist = _s(resolved.get("artist") or resolved.get("albumartist") or "").strip()
    title = _s(resolved.get("title") or "").strip()

    path_text = _s(resolved.get("path") or "")
    path = Path(path_text) if path_text else Path("")
    if path_text and not path.is_absolute():
        path = MUSIC_ROOT / path_text
    acoustid_hits: Optional[List[Dict[str, Any]]] = None
    if path_text and path.is_file():
        try:
            acoustid_hits = _acoustid_lookup_cached(str(path))
        except Exception as ex:
            log.append(f"  AcoustID lookup failed: {ex}")
        if acoustid_hits:
            log.append("  AcoustID match found")
    # Canonical recording identity: backend/recording_review.py.
    recording_review.apply_replacement_identity(
        resolved, title=title, artist=artist, filename=path.name if path_text else "",
        acoustid_hits=acoustid_hits,
        search_text=lambda t, a: _mb_recording_search(t, a, limit=5),
        similarity_fn=_track_ai_similarity, log=log,
    )

    mb_trackid = _s(resolved.get("mb_trackid") or "").strip().lower()
    if mb_trackid:
        details = _fetch_mb_recording_details(mb_trackid, _s(resolved.get("mb_albumid") or "").strip().lower())
        for key in ("artist", "albumartist", "album", "year", "track", "tracktotal", "disc", "disctotal", "label", "genre", "mb_albumid", "mb_artistid"):
            if details.get(key) and (key in {"artist", "album", "mb_albumid"} or not _s(resolved.get(key) or "").strip()):
                resolved[key] = details[key]
        log.append("  MusicBrainz recording resolved")

    release_id = _s(resolved.get("mb_albumid") or "").strip().lower()
    rgid = _s(resolved.get("mb_releasegroupid") or resolved.get("album_mb_releasegroupid") or "").strip().lower()
    if release_id and _MB_UUID_RE.match(release_id) and not rgid:
        release = _fetch_mb_release_candidate(release_id) or {}
        rgid = _s(release.get("mb_releasegroupid") or "").strip().lower()
        if release.get("album") and not _s(resolved.get("album") or "").strip():
            resolved["album"] = release.get("album")
    if rgid:
        resolved["mb_releasegroupid"] = rgid
        log.append("  Release group selected")
        if not release_id or not _MB_UUID_RE.match(release_id):
            release_id = _resolve_release_group_to_release(rgid, log, year=_s(resolved.get("year") or ""), track_count=0)
            if release_id:
                resolved["mb_albumid"] = release_id

    resolved["album_id"] = int(resolved.get("album_id") or 0)
    resolved["track"] = int(resolved.get("track") or 0) if _s(resolved.get("track") or "").strip().isdigit() else int(row.get("track") or 0)
    resolved["disc"] = int(resolved.get("disc") or 1) if _s(resolved.get("disc") or "").strip().isdigit() else int(row.get("disc") or 1)
    resolved["replacement_identity_key"] = _music_format_replacement_key(resolved)

    if not _s(resolved.get("mb_trackid") or "").strip():
        reason = _s(resolved.get("review_reason") or "unable to resolve MusicBrainz recording identity")
        return {**resolved, "ok": False, "retryable": False, "failure_stage": "identity_resolution", "reason": reason}
    if not _s(resolved.get("artist") or "").strip() or not _s(resolved.get("title") or "").strip():
        return {**resolved, "ok": False, "retryable": False, "failure_stage": "identity_resolution", "reason": "resolved recording is missing artist or title"}
    return {**resolved, "ok": True, "retryable": True, "failure_stage": "", "reason": "identity resolved"}


def _music_format_replacement_payload(row: Dict[str, Any], prefs: Dict[str, Any], method: str) -> Dict[str, Any]:
    rgid = _s(row.get("mb_releasegroupid") or "").strip().lower()
    mb_albumid = _s(row.get("mb_albumid") or "").strip().lower()
    mb_for_import = mb_albumid if _MB_UUID_RE.match(mb_albumid) else (f"https://musicbrainz.org/release-group/{rgid}" if rgid else "")
    wanted = [{
        "disc": int(row.get("disc") or 1),
        "track": int(row.get("track") or 0),
        "title": _s(row.get("title") or ""),
        "artist": _s(row.get("artist") or ""),
        "mb_trackid": _s(row.get("mb_trackid") or ""),
    }]
    payload = {
        "artist": row.get("albumartist") or row.get("artist") or "",
        "album": row.get("album") or "",
        "albumartist": row.get("albumartist") or row.get("artist") or "",
        "year": row.get("year") or "",
        "mb_albumid": mb_for_import,
        "wanted_tracks": wanted,
        "replace_existing": True,
        "method": method,
        "try_source_fallback": bool(prefs.get("replacement_fallback", {}).get("try_alternate_source")),
        "fallback_method": "spotiflac",
    }
    album_id = int(row.get("album_id") or 0)
    if album_id:
        payload["existing_album_id"] = album_id
    item_id = int(row.get("item_id") or 0)
    if item_id:
        payload["replace_existing_item_ids"] = [item_id]
    return payload


def _music_format_find_verified_replacement(row: Dict[str, Any], prefs: Dict[str, Any]) -> Dict[str, Any]:
    album_id = int(row.get("album_id") or 0)
    original_item_id = int(row.get("item_id") or 0)
    original_path = _s(row.get("path") or "")
    mb_trackid = _s(row.get("mb_trackid") or row.get("identity_mb_trackid") or row.get("resolved_mb_trackid") or "").strip().lower()
    disc = int(row.get("disc") or 1)
    track = int(row.get("track") or 0)
    artist = _s(row.get("artist") or "")
    title = _s(row.get("title") or "")
    candidates: List[Dict[str, Any]] = []
    seen_candidate_ids = set()

    def _add_candidates(rows) -> None:
        for candidate in rows:
            candidate_id = int(candidate.get("id") or 0)
            if candidate_id and candidate_id not in seen_candidate_ids:
                seen_candidate_ids.add(candidate_id)
                candidates.append(candidate)

    try:
        if mb_trackid:
            try:
                _add_candidates(composite_workflows.find_all_items_by_mbid(mb_trackid))
            except Exception:
                pass
        if album_id:
            try:
                _add_candidates(composite_workflows.find_all_items_by_album_id(album_id))
            except Exception:
                pass
        if not candidates:
            try:
                _add_candidates(composite_workflows.get_items_page(offset=0, limit=100).get("items", []))
            except Exception:
                pass
    except Exception:
        return {}
    for candidate in candidates:
        candidate_id = int(candidate["id"] or 0)
        if original_item_id and candidate_id == original_item_id:
            continue
        candidate_mbid = _s(candidate["mb_trackid"]).strip().lower()
        candidate_disc = int(candidate["disc"] or 1)
        candidate_track = int(candidate["track"] or 0)
        candidate_title = _s(candidate["title"] or "")
        candidate_artist = _s(candidate["artist"] or "")
        candidate_matches_recording = bool(mb_trackid and candidate_mbid == mb_trackid)
        if mb_trackid and candidate_mbid and candidate_mbid != mb_trackid:
            continue
        if track and album_id and not candidate_matches_recording and (candidate_disc, candidate_track) != (disc, track):
            continue
        if not candidate_matches_recording:
            if title and _playlist_title_score(title, candidate_title) < 0.90:
                continue
            if artist and _playlist_artist_name_score(artist, candidate_artist) < 0.75:
                continue
        raw_path = _s(candidate["path"])
        final_path = Path(raw_path)
        if not final_path.is_absolute():
            final_path = MUSIC_ROOT / raw_path
        if original_path and str(final_path.resolve(strict=False)).casefold() == str(Path(original_path).resolve(strict=False)).casefold():
            continue
        validation = _validate_audio_file_preferences(str(final_path), prefs)
        if validation.get("ok"):
            original_abs = Path(original_path) if original_path else Path("")
            if original_path and not original_abs.is_absolute():
                original_abs = MUSIC_ROOT / original_path
            fingerprint_validation: Dict[str, Any] = {}
            final_ids: List[str] = []
            if original_path and original_abs.exists():
                shared_id, source_ids, candidate_ids = _acoustid_fingerprint_match(str(original_abs), str(final_path))
                final_ids = candidate_ids
                if shared_id:
                    fingerprint_validation = {
                        "fingerprint_status": "matched",
                        "acoustid_status": "confirmed",
                        "identity_status": "verified",
                        "mb_recording_id_candidate": shared_id,
                        "decision_reason": f"Replacement AcoustID fingerprint matches original recording {shared_id}.",
                    }
                elif source_ids and candidate_ids:
                    continue
            if not fingerprint_validation:
                if not final_ids:
                    final_ids = _acoustid_fingerprint_ids(str(final_path))
                if mb_trackid and mb_trackid in final_ids:
                    fingerprint_validation = {
                        "fingerprint_status": "matched",
                        "acoustid_status": "confirmed",
                        "identity_status": "verified",
                        "mb_recording_id_candidate": mb_trackid,
                        "decision_reason": "Replacement AcoustID fingerprint matches the intended MusicBrainz recording.",
                    }
                else:
                    continue
            return {
                "item_id": candidate_id,
                "path": str(final_path),
                "validation": validation,
                "fingerprint_validation": fingerprint_validation,
            }
    return {}


def _music_format_replacement_matching_contract(resolved: Dict[str, Any], replacement: Dict[str, Any]) -> Dict[str, Any]:
    """Build the matching_contract the engine requires to authorize a track
    replacement, from evidence this caller already computed via
    _music_format_resolve_replacement_identity (the original's identity)
    and _music_format_find_verified_replacement (the candidate's AcoustID
    fingerprint verification against that identity) -- SEC-002 Wave 17
    final review.

    This is deliberately NOT a fresh AI suggestion or an unverified
    client claim: fingerprint_validation is only ever populated by
    _music_format_find_verified_replacement after an actual AcoustID
    fingerprint comparison (see _acoustid_fingerprint_match /
    _acoustid_fingerprint_ids), so identity_source is set to
    "acoustid_fingerprint" only when that real verification produced a
    result -- never a bare guess."""
    fp = (replacement or {}).get("fingerprint_validation") or {}
    replacement_recording_id = _s(fp.get("mb_recording_id_candidate") or "").strip().lower()
    identity_source = "acoustid_fingerprint" if (replacement_recording_id and fp.get("fingerprint_status") == "matched") else ""
    return {
        "identity_source": identity_source,
        "original_recording_id": _s(resolved.get("mb_trackid") or "").strip().lower(),
        "replacement_recording_id": replacement_recording_id,
        "original_release_group_id": _s(resolved.get("mb_releasegroupid") or "").strip().lower(),
        "replacement_release_group_id": _s(resolved.get("mb_releasegroupid") or "").strip().lower(),
        "decision_reason": _s(fp.get("decision_reason") or ""),
    }


def _music_format_remove_original_after_replacement(original_path: str, final_path: str, prefs: Dict[str, Any], log: list,
                                                    original_item_id: int = 0,
                                                    resolved: Optional[Dict[str, Any]] = None,
                                                    replacement: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Execute engine-owned track replacement transaction without direct local filesystem or DB mutations (SEC-002 Wave 17)."""
    result = {"removed": False, "quarantined_to": "", "reason": ""}
    if not original_path or not final_path:
        result["reason"] = "original or replacement path missing"
        return result

    matching_contract = _music_format_replacement_matching_contract(resolved or {}, replacement or {})

    try:
        plan_res = composite_workflows.plan_track_replacement({
            "original_item_id": int(original_item_id or 0),
            "original_path": original_path,
            "replacement_path": final_path,
            "reason": "Music format quality replacement",
            "matching_contract": matching_contract,
        })
        if not plan_res.get("ok"):
            error_msg = plan_res.get("error") or "track replacement plan failed"
            log.append(f"Track replacement plan failed: {error_msg}")
            result["reason"] = error_msg
            return result

        op_id = plan_res.get("operation_id")
        apply_res = composite_workflows.apply_track_replacement(op_id)
        if not apply_res.get("ok"):
            error_msg = apply_res.get("error") or "track replacement apply failed"
            log.append(f"Track replacement apply failed: {error_msg}")
            result["reason"] = error_msg
            return result

        result["removed"] = True
        result["quarantined_to"] = apply_res.get("quarantined_to") or ""
        log.append("Original removed after verified replacement via engine transaction")
        return result
    except Exception as ex:
        result["reason"] = str(ex)
        log.append(f"Track replacement engine IPC failed: {ex}")
        return result


def _music_format_replace_rows(log: list, cancel_event=None, update_state=None, *, limit: int = 0,
                               method: str = "slskd", reset_retry_state: bool = False) -> Dict[str, Any]:
    prefs = _music_format_preferences()
    statuses = _load_music_format_replacement_statuses().get("tracks") or []
    now = time.time()
    pending: List[Dict[str, Any]] = []
    seen_keys: set = set()
    skipped = 0
    for raw in statuses:
        status_text = _s(raw.get("status") or raw.get("replacement_status") or "").lower()
        if status_text in {"replacement complete", "complete"}:
            continue
        allowed, wait_reason = _music_format_retry_allowed(raw, now, reset_retry_state=reset_retry_state)
        if not allowed:
            skipped += 1
            continue
        key = _music_format_replacement_key(raw)
        if key and key in seen_keys:
            skipped += 1
            reason = "duplicate logical replacement request suppressed"
            _mark_music_format_needs_replacement([
                _music_format_retry_state(raw, status="Replacement duplicate suppressed",
                                          stage="dedupe", reason=reason,
                                          retryable=False,
                                          attempt_count=int(raw.get("attempt_count") or 0))
            ])
            continue
        if key:
            seen_keys.add(key)
        pending.append(raw)
    if limit > 0:
        pending = pending[:limit]
    library_rows = _music_format_library_rows(0)
    complete = 0
    failed = 0
    log.append(f"Starting replacement retry for {len(pending)} track(s)")
    for index, raw_row in enumerate(pending, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("music format replacement stopped by user")
        row = _music_format_hydrate_status_row(raw_row, library_rows)
        if reset_retry_state:
            row = {**row, "attempt_count": 0, "next_retry_at": 0, "retryable": True, "queued_retry": True}
        attempt_count = int(row.get("attempt_count") or row.get("retry_attempt_count") or 0) + 1
        label = f"{row.get('artist') or 'Unknown artist'} - {row.get('title') or 'Unknown track'}"
        if update_state:
            update_state({"phase": "Resolving identity", "processed": index - 1, "total": len(pending), "current": label})
        original_path = _s(row.get("path") or "")
        original_validation = _validate_audio_file_preferences(original_path, prefs) if original_path else {"ok": False, "reasons": ["missing path"]}
        if original_validation.get("ok"):
            skipped += 1
            log.append(f"Skipped replacement: current file now matches preferences: {label}")
            _mark_music_format_needs_replacement([{**row, "status": "Replacement complete", "replacement_status": "Replacement complete", "reason": "Current file now matches Music Format Preferences", "queued_retry": False, "retryable": False, "attempt_count": attempt_count}])
            continue

        _mark_music_format_needs_replacement([
            _music_format_retry_state(row, status="Resolving identity", stage="identity_resolution",
                                      reason="Resolving replacement target identity", retryable=True,
                                      attempt_count=attempt_count)
        ])
        resolved = _music_format_resolve_replacement_identity(row, log)
        if not resolved.get("ok"):
            failed += 1
            reason = _s(resolved.get("reason") or "unable to resolve replacement identity")
            log.append(f"Needs review: {label}: {reason}")
            _mark_music_format_needs_replacement([
                _music_format_retry_state(resolved, status="Needs review", stage=resolved.get("failure_stage") or "identity_resolution",
                                          reason=reason, retryable=False, attempt_count=attempt_count)
            ])
            continue

        # ARCH-009: the replacement's target album is identified by its
        # Release Group; a bare Release ID (edition evidence) is never
        # substituted when the release group could not be resolved.
        album_context = _s(resolved.get("mb_releasegroupid") or "").strip()
        if not album_context:
            failed += 1
            reason = "Could not confidently identify album context"
            log.append(reason)
            log.append(f"Needs review: {label}: multiple release groups remain plausible")
            _mark_music_format_needs_replacement([
                _music_format_retry_state(resolved, status="Needs review", stage="album_context_resolution",
                                          reason=reason, retryable=False, attempt_count=attempt_count)
            ])
            continue

        mode = "fully resolved track" if int(resolved.get("album_id") or 0) and int(resolved.get("track") or 0) and album_context else "verified recording identity"
        log.append(f"Searching replacement sources: {mode}")
        _mark_music_format_needs_replacement([
            _music_format_retry_state(resolved, status="Searching for replacement", stage="searching_replacement",
                                      reason="Searching replacement sources", retryable=True,
                                      attempt_count=attempt_count)
        ])
        try:
            payload = _music_format_replacement_payload(resolved, prefs, method)
            if not payload.get("artist") or not payload.get("album"):
                raise RuntimeError("resolved identity is missing searchable artist or album/title context")
            started = _json_from_flask_response(start_album_download(payload))
            if not started.get("ok") or not started.get("job_id"):
                raise RuntimeError(started.get("error") or "replacement download did not start")
            _wait_for_child_job(started["job_id"], log, cancel_event, prefix="replacement", timeout=2400)
            replacement = _music_format_find_verified_replacement(resolved, prefs)
            if not replacement:
                raise RuntimeError("replacement failed verification")
            log.append("Replacement found: imported " + _s((replacement.get("validation") or {}).get("message") or "compliant audio"))
            fp_reason = _s((replacement.get("fingerprint_validation") or {}).get("decision_reason") or "")
            if fp_reason:
                log.append("Replacement fingerprint: " + fp_reason)
            removal = _music_format_remove_original_after_replacement(
                original_path,
                replacement.get("path") or "",
                prefs,
                log,
                original_item_id=int(row.get("item_id") or 0),
                resolved=resolved,
                replacement=replacement,
            )
            if not removal.get("removed"):
                raise RuntimeError(removal.get("reason") or "original was not removed after replacement")
            complete += 1
            _mark_music_format_needs_replacement([{**resolved, "status": "Replacement complete", "replacement_status": "Replacement complete", "replacement_path": replacement.get("path"), "reason": "Replacement imported and verified", "queued_retry": False, "retryable": False, "attempt_count": attempt_count}])
        except Exception as ex:
            failed += 1
            reason = str(ex)
            retryable = attempt_count < _MUSIC_FORMAT_REPLACEMENT_MAX_ATTEMPTS
            log.append(f"Skipped removal: replacement failed verification: {label}: {reason}")
            log.append("No replacement currently available" if retryable else "Resolution failed: retry limit reached")
            _mark_music_format_needs_replacement([
                _music_format_retry_state(resolved if 'resolved' in locals() else row,
                                          status="No replacement currently available" if retryable else "Resolution failed",
                                          stage="verification" if "verification" in reason.lower() else "searching_replacement",
                                          reason=reason,
                                          retryable=retryable,
                                          attempt_count=attempt_count)
            ])
        if update_state:
            update_state({"phase": "Replacing", "processed": index, "total": len(pending), "complete": complete, "failed": failed, "skipped": skipped})
    log.append(f"Replacement retry complete: {complete} complete, {failed} failed, {skipped} skipped")
    return {"total": len(pending), "complete": complete, "failed": failed, "skipped": skipped}
