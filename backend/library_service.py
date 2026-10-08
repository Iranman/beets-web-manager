"""Library, artist and album services over Beets (ARCH-001).
"""

from __future__ import annotations

import backend.provider_boundary as provider_boundary
import json, os, re, sqlite3, threading, time, uuid
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Tuple
from backend.app_runtime import _app_logger, AUDIO_EXT, DOWNLOADS_ALLOWED_ROOTS, DOWNLOADS_ROOT, EDITABLE_FIELDS, MUSIC_ROOT, PLAYLIST_DOWNLOAD_ROOT, ROOT_FOLDER_REPAIR_LAST_FILE, TORRENT_SOURCE_MOVE_ALLOWED, TORRENT_SOURCE_ROOTS, WEB_MANAGER_DATA_DIR, _ANSI_RE, _LITERAL_PLACEHOLDER_RE, _MB_TRACK_REPAIR_MATCH_THRESHOLD, _MB_UUID_RE, _UNRESOLVED_TEMPLATE_TOKEN_RE, _YEAR_SFXRE, _s, _up, _ur
from backend.ai_evidence_service import _AI_EVIDENCE_DISC_FOLDER_RE, _ai_evidence_clean_artist_guess, _ai_evidence_clean_segment, _ai_evidence_extract_year, _ai_evidence_scene_guess, _ai_evidence_weak_album_guess, _ai_evidence_weak_artist_guess, _ai_suggest_genre, _enrich_track_ai_candidate, _item_ai_abs_path, _score_track_ai_candidate
from backend.pending_review_store import _load_pending_reviews, _queue_folder_for_manual_review, _remove_pending_review_for_path
from backend.playlist_service import _artist_folder_merge_key, _safe_artist_folder_name
from backend.import_reconciliation_service import _apply_artist_folder_reconcile_resilient
from backend.cleanup_service import _album_cleanup_file_info, _album_cleanup_quality_tuple, _album_cleanup_remove_empty_tree, _album_cleanup_verified_same_file, _artist_alias_key, _unique_dest
from backend.app_runtime import PLAYLIST_DOWNLOAD_ALLOWED_ROOTS, _path_is_under, _path_under, _redact_security_text, _safe_inventory_error_message, _safe_path_component, _split_beets_multi, _split_collab_credit, _split_mbid_values
from backend.import_guard import release_track_matches_missing_target as _guard_release_track_matches_missing_target
from backend.title_normalize import restore_time_colon_title as _restore_time_colon_title
from helpers_mb import _mb_recording_search, _mb_release_search, _clean_for_mb, _resolve_release_group_to_release, _resolve_mb_release_id, _fetch_mb_release_candidate, _mb_release_group_candidates
from backend.beets_adapter import beets_adapter, lib, BeetsError, BeetsUnavailableError, BeetsAuthError, BeetsBadRequestError, BeetsNotFoundError
import backend.composite_workflows as composite_workflows
from backend.library_cache import library_cache
import backend.recording_review as recording_review
from backend.acoustid_service import _acoustid_lookup_cached, _album_track_norm, _artist_folder_fingerprint_confirms, _normalize_albumartist, _read_file_media_tags
from backend.artwork_service import _ART_EXTS, _artist_name_key, _attach_artist_image_cache_urls, _get_album_item_dir
from backend.slskd_service import _normalise_wanted_tracks, _slskd_title_guess_from_name
from backend.matching_service import _ai_api_key, _album_mb_completeness, _album_title_match, _artist_folder_name_without_mbid, _audio_position_from_path, _best_album_track_match, _fast_album_mb_health_fields, _fetch_mb_release_tracklist, _folder_release_preflight, _invalidate_lib_cache, _preflight_oversized_subset_complete, _stamp_artist_folder_album_mbid_counts
from backend.app_runtime import jobs
from backend.musicbrainz_service import _artist_album_index, _artist_folder_key, _mb_artist_lookup_by_id, _mb_artist_search_one, _mb_canonical_for_artist_entries, _mb_release_has_tracks, _mb_release_search_by_folder_tracks
from backend.serializers import _DOWNLOADS_ROOTS, _format_duration, _import_review_path_text_error, _leaked_db_paths_summary, _resolve_import_review_source_path
from backend.plex_service import _trigger_plex_refresh

from backend.artwork_service import _art_repair_build_report, _art_repair_save_last, _repair_album_art

# ── ARCH-001 extracted code ──


class AttachRecordingCancelled(RuntimeError):
    """Raised when a beet subprocess stage of the attach-recording /
    recording-ID-rollback workflow was cancelled (rc=-9), so callers can
    tell an intentional cancellation apart from an ordinary failure."""


def _require_attach_stage_success(result, stage: str) -> None:
    """Fail closed for every supported attach/rollback IPC result shape."""
    if isinstance(result, dict):
        if result.get("ok") is True:
            return
        if result.get("ok") is False:
            detail = result.get("error") or result.get("code") or "remote stage returned ok=false"
            raise RuntimeError(f"{stage} failed: {detail}")
        raise RuntimeError(f"{stage} returned an ambiguous result")
    if hasattr(result, "returncode"):
        rc = int(getattr(result, "returncode"))
        if rc == 0:
            return
        if rc == -9:
            raise AttachRecordingCancelled(f"{stage} cancelled")
        if rc == 124:
            raise RuntimeError(f"{stage} timed out")
        raise RuntimeError(f"{stage} failed")
    raise RuntimeError(f"{stage} returned an unsupported result")


def _album_folder_for_album_id(album_id: int) -> str:
    """Return a best-effort library folder path for an album row."""
    if not album_id:
        return ""
    try:
        items = composite_workflows.find_all_items_by_album_id(int(album_id))
    except BeetsUnavailableError:
        raise
    except Exception:
        items = []
    if not items:
        return ""
    sorted_items = sorted(
        items,
        key=lambda it: (int(it.get("disc") or 1), int(it.get("track") or 0), int(it.get("id") or 0)),
    )
    raw = _s(sorted_items[0].get("path") or "")
    if not raw:
        return ""
    fpath = Path(raw)
    if not fpath.is_absolute():
        fpath = MUSIC_ROOT / raw
    folder = fpath.parent
    if re.match(r'^(?:disc|cd|disk)\s*\d+$', folder.name, re.I):
        folder = folder.parent
    return str(folder)


def _source_audio_missing_track_scan(folder_path: str, existing_album_id: int,
                                     mb_albumid: str, log: list) -> Dict[str, Any]:
    """Classify source audio files against the still-missing MB tracks."""
    result: Dict[str, Any] = {
        "ok": False,
        "existing_album_id": int(existing_album_id or 0),
        "artist": "",
        "album": "",
        "audio_count": 0,
        "expected_count": 0,
        "in_library": 0,
        "missing_count": 0,
        "useful_files": [],
        "duplicate_files": [],
        "unknown_files": [],
        "wanted_tracks": [],
    }
    if not existing_album_id or not mb_albumid:
        return result
    source = Path(folder_path)
    if not source.exists() or not source.is_dir():
        return result

    try:
        comp = _album_mb_completeness(existing_album_id, mb_albumid, log)
    except Exception as ex:
        log.append(f"  [import] Existing-album scan warning: {ex}")
        return result
    missing = _normalise_wanted_tracks(comp.get("missing") or [])
    result["artist"] = _s(comp.get("artist", ""))
    result["album"] = _s(comp.get("album", ""))
    result["expected_count"] = int(comp.get("expected_count") or 0)
    result["in_library"] = int(comp.get("in_library") or 0)
    result["missing_count"] = len(missing)

    audio_files: List[Path] = []
    inspect_evidence: Optional[Dict[str, Any]] = None
    try:
        if source.exists() and source.is_dir():
            audio_files = sorted(
                [p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXT],
                key=lambda p: str(p).lower(),
            )
    except Exception as ex:
        log.append(f"  [import] Source scan warning: {ex}")

    if not audio_files and folder_path:
        try:
            inspect_res = composite_workflows.inspect_import_source(folder_path, "reimport")
            if inspect_res.get("ok"):
                inspect_evidence = inspect_res
        except Exception as ex:
            log.append(f"  [import] Remote source inspection warning: {ex}")

    if inspect_evidence:
        audio_entries = inspect_evidence.get("audio_files") or []
        result["audio_count"] = len(audio_entries)
    else:
        result["audio_count"] = len(audio_files)

    if not audio_files and not inspect_evidence:
        result["ok"] = True
        return result

    mb = _fetch_mb_release_tracklist(mb_albumid, log)
    mb_tracks = mb.get("tracks") or []
    if not mb_tracks:
        return result

    release_title_counts: Dict[str, int] = {}
    for mbt in mb_tracks:
        title_norm = _album_track_norm(mbt.get("title", ""))
        if title_norm:
            release_title_counts[title_norm] = release_title_counts.get(title_norm, 0) + 1

    missing_mb_tracks = [
        t for t in mb_tracks
        if _guard_release_track_matches_missing_target(
            t,
            missing,
            title_norm_fn=_album_track_norm,
            release_title_counts=release_title_counts,
        )
    ]
    if not missing_mb_tracks:
        missing_mb_tracks = [
            {
                "disc": int(t.get("disc") or 1),
                "track": int(t.get("track") or 0),
                "title": _s(t.get("title", "")),
                "title_norm": _album_track_norm(t.get("title", "")),
                "mb_trackid": _s(t.get("mb_trackid", "")).strip().lower(),
                "duration_ms": int(t.get("duration_ms") or 0),
            }
            for t in missing
        ]

    scan_candidates: List[Tuple[Path, str, int, int]] = []
    if inspect_evidence:
        for entry in inspect_evidence.get("audio_files") or []:
            rel = _s(entry.get("relative_path"))
            fpath = Path(folder_path) / rel if rel else Path(folder_path)
            props = entry.get("properties") if isinstance(entry.get("properties"), dict) else {}
            disc = int(props.get("disc") or 0)
            track = int(props.get("track") or 0)
            if not track or not disc:
                path_disc, path_track = _audio_position_from_path(str(fpath))
                if not track:
                    track = path_track
                if not disc or disc == 1:
                    disc = path_disc or 1
            title = _s(props.get("title")) or _slskd_title_guess_from_name(fpath.name) or fpath.stem
            scan_candidates.append((fpath, title, disc, track))
    else:
        for fpath in audio_files:
            disc, track = _audio_position_from_path(str(fpath))
            title = _slskd_title_guess_from_name(fpath.name) or fpath.stem
            scan_candidates.append((fpath, title, disc, track))

    selected_by_key: Dict[tuple, Dict[str, Any]] = {}
    for fpath, title, disc, track in scan_candidates:
        item = {
            "title": title,
            "path": str(fpath),
            "track": int(track or 0),
            "disc": int(disc or 1),
            "mb_trackid": "",
            "length": 0,
        }
        best_missing = _best_album_track_match(item, missing_mb_tracks)
        missing_idx = int(best_missing.get("idx", -1))
        missing_score = float(best_missing.get("score") or 0.0)
        if missing_idx >= 0 and missing_score >= _MB_TRACK_REPAIR_MATCH_THRESHOLD:
            mbt = best_missing.get("track") or {}
            key = (
                int(mbt.get("disc") or 1),
                int(mbt.get("track") or 0),
                _s(mbt.get("mb_trackid", "")).strip().lower(),
            )
            if key in selected_by_key:
                result["duplicate_files"].append(str(fpath))
                continue
            selected_by_key[key] = {
                "path": str(fpath),
                "disc": int(mbt.get("disc") or 1),
                "track": int(mbt.get("track") or 0),
                "title": _s(mbt.get("title", "")),
                "mb_trackid": _s(mbt.get("mb_trackid", "")).strip().lower(),
                "score": round(missing_score, 3),
            }
            continue

        best = _best_album_track_match(item, mb_tracks)
        idx = int(best.get("idx", -1))
        score = float(best.get("score") or 0.0)
        if idx >= 0 and score >= _MB_TRACK_REPAIR_MATCH_THRESHOLD:
            result["duplicate_files"].append(str(fpath))
        else:
            result["unknown_files"].append(str(fpath))

    selected = list(selected_by_key.values())
    selected.sort(key=lambda t: (int(t.get("disc") or 1), int(t.get("track") or 0), t.get("title", "")))
    result["useful_files"] = [t["path"] for t in selected]
    result["wanted_tracks"] = [
        {
            "disc": t["disc"],
            "track": t["track"],
            "title": t["title"],
            "mb_trackid": t["mb_trackid"],
        }
        for t in selected
    ]
    result["ok"] = True
    return result


def _folder_import_track_count(source_folder: str, existing_album_id: int = 0) -> int:
    """Best known track count for a source folder before selecting an MB release."""
    if existing_album_id:
        try:
            items = composite_workflows.find_all_items_by_album_id(int(existing_album_id))
            if items:
                return len(items)
        except BeetsUnavailableError:
            raise
        except Exception:
            pass
    try:
        source = Path(source_folder)
        if not (_path_is_under(source, MUSIC_ROOT) or _path_is_under(source, DOWNLOADS_ROOT)):
            return 0
        if source.is_dir():
            return sum(
                1 for p in source.rglob("*")
                if p.is_file() and p.suffix.lower() in AUDIO_EXT
            )
    except Exception:
        pass
    return 0


def _resolve_album_release_for_import(mb_input: str, artist: str, album: str,
                                      year: str, track_count: int, log: list,
                                      source_folder: str = "",
                                      existing_album_id: int = 0,
                                      allow_provided_release_override: bool = False,
                                      allow_oversized_partial: bool = True) -> str:
    """Resolve or discover a concrete MB release UUID suitable for beet import."""
    mb_input = (mb_input or "").strip()
    source_track_count = (
        _folder_import_track_count(source_folder, existing_album_id)
        if source_folder else 0
    )
    rank_track_count = track_count or source_track_count

    def _oversized_release_subset_complete(pre: Dict[str, Any]) -> bool:
        return _preflight_oversized_subset_complete(pre)

    def _same_group_source_sized_release(pre: Dict[str, Any], label: str) -> str:
        if not _oversized_release_subset_complete(pre):
            return ""
        rgid = _s(pre.get("release_group", "") or "").strip().lower()
        if not rgid:
            return ""
        audio_count = int(pre.get("audio_count") or 0)
        log.append(
            f"  {label} matches {pre.get('matches', 0)}/{audio_count} source track(s) "
            f"but the MB release has {pre.get('expected', 0)} tracks; "
            f"checking release group for a {audio_count}-track release…"
        )
        replacement = _resolve_release_group_to_release(
            rgid, log, year=year, track_count=audio_count or rank_track_count)
        if not replacement:
            return ""
        replacement_pre = _folder_release_preflight(
            source_folder,
            replacement,
            existing_album_id=existing_album_id,
            log=None,
        )
        if replacement_pre.get("ok"):
            log.append(
                f"  {label} switched to same-release-group track-count match: "
                f"{replacement} ({replacement_pre.get('matches', 0)}/"
                f"{replacement_pre.get('expected', 0)} track(s))"
            )
            return replacement
        log.append(
            f"  Same-release-group candidate rejected by folder tracklist: "
            f"{replacement_pre.get('matches', 0)}/"
            f"{replacement_pre.get('expected', 0)} track(s) matched"
        )
        return ""

    def _source_accepts_release(rel_id: str, label: str) -> str:
        if not source_folder:
            return rel_id
        pre = _folder_release_preflight(
            source_folder,
            rel_id,
            existing_album_id=existing_album_id,
            log=None,
        )
        replacement = _same_group_source_sized_release(pre, label)
        if replacement:
            return replacement
        if _oversized_release_subset_complete(pre):
            audio_count = int(pre.get("audio_count") or 0)
            if not allow_oversized_partial:
                log.append(
                    f"  {label} rejected by strict edition guard: "
                    f"{pre.get('matches', 0)}/{audio_count} source track(s) match "
                    f"the {pre.get('expected', 0)}-track MusicBrainz release. "
                    "Queueing Review instead of auto-repairing an oversized edition."
                )
                return ""
            log.append(
                f"  {label} accepted as a clean partial match: "
                f"{pre.get('matches', 0)}/{audio_count} source track(s) match "
                f"the {pre.get('expected', 0)}-track MusicBrainz release. "
                "Remaining tracks will stay missing/downloadable."
            )
            return rel_id
        if pre.get("ok"):
            log.append(
                f"  {label} passed folder tracklist check: "
                f"{pre.get('matches', 0)}/{pre.get('expected', 0)} track(s)"
            )
            return rel_id
        log.append(
            f"  {label} rejected by folder tracklist: "
            f"{pre.get('matches', 0)}/{pre.get('expected', 0)} track(s) matched"
            + (f" ({pre.get('release_title')})" if pre.get("release_title") else "")
        )
        for line in (pre.get("examples") or [])[:3]:
            log.append(line)
        return ""

    def _accept_from_release_group(rgid: str, first: str) -> str:
        """Explicit release group (#260): return only a release of ``rgid``.

        Tries the ranked release, then every other release in the group.
        Never falls back to a free search, which could pick another group."""
        accepted = _source_accepts_release(first, "Resolved release-group candidate")
        if accepted:
            return accepted
        others = [
            c for c in _mb_release_group_candidates(rgid, log)
            if c.get("mb_albumid") and c["mb_albumid"] != first
        ]
        if rank_track_count:
            others.sort(key=lambda c: abs(int(c.get("track_count") or 0) - rank_track_count))
        # ponytail: one tracklist fetch per release; very large groups are slow (MB rate limit).
        for cand in others:
            accepted = _source_accepts_release(
                cand["mb_albumid"], "Release-group alternative release")
            if accepted:
                return accepted
        if allow_provided_release_override:
            log.append(
                "  Manual MusicBrainz release-group override requested; using "
                "the resolved release despite the folder tracklist mismatch."
            )
            return first
        log.append(
            f"  REFUSED: no release in the requested release-group {rgid} matches the "
            "folder tracklist; not searching other release groups. Manual Review is required."
        )
        return ""

    if mb_input:
        input_uuid_match = re.search(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            mb_input,
            re.I,
        )
        if input_uuid_match and "release-group" in mb_input.casefold():
            rg_resolved = _resolve_release_group_to_release(
                input_uuid_match.group(0).lower(),
                log,
                year=year,
                track_count=rank_track_count,
            )
            if rg_resolved:
                return _accept_from_release_group(
                    input_uuid_match.group(0).lower(), rg_resolved)
            # Fail closed: an explicit release group never falls back to a
            # free search that could return another group (#260).
            log.append(
                f"  WARN: release-group {input_uuid_match.group(0).lower()} "
                "did not resolve to a release with tracks"
            )
            return ""

        resolved = _resolve_mb_release_id(mb_input, log) or ""
        if resolved and _mb_release_has_tracks(resolved):
            accepted = _source_accepts_release(resolved, "Provided MusicBrainz release")
            if accepted:
                return accepted
            if allow_provided_release_override:
                log.append(
                    "  Manual MusicBrainz release override requested; using the "
                    "provided release ID despite the folder tracklist mismatch."
                )
                return resolved
            # A replacement stays inside the provided Release's own Release
            # Group; a cross-group replacement is never automatic (review).
            provided_rg = _s(_fetch_mb_release_tracklist(resolved, log).get("release_group")).strip().lower()
            if not provided_rg:
                log.append("  REFUSED: the provided release's release group is unknown; "
                           "Manual Review is required.")
                return ""
            log.append(f"  Looking for a replacement inside release group {provided_rg}…")
            return _accept_from_release_group(provided_rg, resolved)
        elif resolved and _MB_UUID_RE.match(resolved):
            rg_resolved = _resolve_release_group_to_release(
                resolved, log, year=year, track_count=track_count)
            if rg_resolved:
                return _accept_from_release_group(resolved, rg_resolved)
            log.append(f"  WARN: {resolved} did not resolve to a release with tracks")

    log.append("  Searching MusicBrainz for a release ID…")
    album_only_pool_loaded = False
    cands = _mb_release_search(album, artist, limit=6, year=year,
                               track_count=rank_track_count, log=log)
    if not cands and source_folder and artist:
        log.append(
            "  No artist-scoped MusicBrainz candidates found; "
            "trying album title without artist…"
        )
        cands = _mb_release_search(album, "", limit=12, year=year,
                                   track_count=rank_track_count, log=log)
        album_only_pool_loaded = True
    if not cands and not source_folder:
        return ""

    if source_folder:
        scored_cands: List[tuple] = []
        rejected: List[str] = []
        seen_mbids: set = set()

        def _score_candidate_pool(candidates: List[Dict[str, Any]]) -> None:
            for cand in candidates:
                cand_mbid = (cand.get("mb_albumid") or "").strip().lower()
                if not cand_mbid or cand_mbid in seen_mbids:
                    continue
                seen_mbids.add(cand_mbid)
                pre = _folder_release_preflight(
                    source_folder,
                    cand_mbid,
                    existing_album_id=existing_album_id,
                    log=None,
                )
                matches = int(pre.get("matches") or 0)
                expected = int(pre.get("expected") or cand.get("tracks") or 0)
                replacement = _same_group_source_sized_release(
                    pre,
                    f"MusicBrainz candidate {cand.get('artist','?')} – {cand.get('album','?')}",
                )
                if replacement:
                    replacement_cand = dict(cand)
                    replacement_cand["mb_albumid"] = replacement
                    if source_track_count:
                        replacement_cand["tracks"] = source_track_count
                    scored_cands.append((
                        matches,
                        -abs((replacement_cand.get("tracks") or expected) - rank_track_count)
                        if rank_track_count else 0,
                        replacement_cand,
                    ))
                    continue
                if _oversized_release_subset_complete(pre):
                    if not allow_oversized_partial:
                        rejected.append(
                            f"{cand.get('artist','?')} – {cand.get('album','?')} "
                            f"{cand.get('year','')} [{cand_mbid}] "
                            f"(oversized partial: {matches}/{int(pre.get('audio_count') or 0)} "
                            f"source track matches, release has {expected} tracks)"
                        )
                        continue
                    scored_cands.append((
                        matches,
                        -abs((cand.get("tracks") or expected) - rank_track_count)
                        if rank_track_count else 0,
                        cand,
                    ))
                    continue
                if pre.get("ok"):
                    scored_cands.append((
                        matches,
                        -abs((cand.get("tracks") or expected) - rank_track_count)
                        if rank_track_count else 0,
                        cand,
                    ))
                    continue
                rejected.append(
                    f"{cand.get('artist','?')} – {cand.get('album','?')} "
                    f"{cand.get('year','')} [{cand_mbid}] ({matches}/{expected} track matches)"
                )

        _score_candidate_pool(cands)
        if not scored_cands and artist and not album_only_pool_loaded:
            log.append(
                "  No artist-scoped MusicBrainz candidate matched the folder; "
                "trying album title without artist…"
            )
            _score_candidate_pool(
                _mb_release_search(album, "", limit=12, year=year,
                                   track_count=rank_track_count, log=log)
            )
        if not scored_cands:
            track_cands = _mb_release_search_by_folder_tracks(
                source_folder, existing_album_id=existing_album_id,
                artist=artist, log=log, limit=12)
            if track_cands:
                log.append(
                    f"  Found {len(track_cands)} MusicBrainz candidate(s) "
                    "from folder track titles."
                )
                _score_candidate_pool(track_cands)

        if scored_cands:
            scored_cands.sort(key=lambda item: (item[0], item[1]), reverse=True)
            best = scored_cands[0][2]
            mbid = (best.get("mb_albumid") or "").strip().lower()
            log.append(
                f"  MusicBrainz release selected after tracklist check: "
                f"{best.get('artist','?')} – {best.get('album','?')} "
                f"{best.get('year','')} [{mbid}]"
            )
            return mbid

        for line in rejected[:3]:
            log.append(f"  MusicBrainz candidate rejected: {line}")
        log.append(
            "  No MusicBrainz search candidate matched the folder tracklist; "
            "manual Review is required."
        )
        return ""

    best = cands[0]
    mbid = (best.get("mb_albumid") or "").strip().lower()
    if mbid:
        log.append(f"  MusicBrainz release selected: {best.get('artist','?')} – "
                   f"{best.get('album','?')} {best.get('year','')} [{mbid}]")
    return mbid


def _app_managed_download_path(path: Path) -> bool:
    # Validated roots only: an unsafe DOWNLOADS_ROOT authorizes nothing (#251 F-1).
    managed_roots = [root / "_beets_missing_import" for root in DOWNLOADS_ALLOWED_ROOTS]
    managed_roots.extend(PLAYLIST_DOWNLOAD_ALLOWED_ROOTS)  # #268 S-4
    try:
        path_res = path.resolve(strict=False)
    except Exception:
        path_res = path
    if any(_path_is_under(path_res, root) for root in managed_roots):
        return True
    name = path.name.casefold()
    return any(
        marker in name
        for marker in (
            " - yt missing ",
            " - soundcloud missing ",
            " - spotiflac missing ",
        )
    )


def _preserve_torrent_source_path(path_value: str | Path) -> bool:
    """True when Beets should not move/delete this source folder."""
    if TORRENT_SOURCE_MOVE_ALLOWED:
        return False
    path = Path(path_value)
    if _path_is_under(path, MUSIC_ROOT):
        return False
    if _app_managed_download_path(path):
        return False
    roots = TORRENT_SOURCE_ROOTS or DOWNLOADS_ALLOWED_ROOTS
    if not roots:
        # No safe download root (DOWNLOADS_ROOT unsafe): copy, never move (#251 N1).
        return True
    return any(_path_is_under(path, root) for root in roots)


_MUSIC_LIBRARY_ROOT = str(MUSIC_ROOT)


def _delete_if_already_in_library(src_path: str, beet_output: str, log: list) -> bool:
    """Delete source folder(s) that beet reported as already in the library.

    Safe only when the source is under a downloads/temp root — never touches
    the music library itself.

    Returns True if anything was deleted.
    """

    output_lower = beet_output.lower()
    _already_phrases = ("already in the library", "already in library",
                        "already imported", "no files imported", "nothing was imported")
    if not any(p in output_lower for p in _already_phrases):
        return False

    src = Path(src_path)
    # Refuse to touch anything inside the music library
    try:
        resolved = str(src.resolve())
    except Exception:
        resolved = str(src)
    if _path_is_under(Path(resolved), MUSIC_ROOT):
        return False
    if _preserve_torrent_source_path(src):
        log.append(
            "  [cleanup] Preserved torrent source already in library "
            f"(qBittorrent-safe): {src}"
        )
        return False
    is_safe = any(_path_is_under(Path(resolved), Path(r)) for r in _DOWNLOADS_ROOTS)
    if not is_safe:
        log.append(f"  [cleanup] Skipped (path not under downloads root): {src}")
        return False

    audio_exts = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav",
                  ".aiff", ".wv", ".ape", ".alac", ".aac"}

    # Strategy 1: parse specific folder paths from beet output lines
    # Beet outputs lines like:
    #   "Artist/Album (Year): Already in library"
    #   "Skipping: /path/to/folder"
    deleted_any = False
    folders_to_delete: set = set()

    # Look for paths explicitly mentioned before "already in the library"
    for line in beet_output.splitlines():
        line_lower = line.lower()
        if not any(p in line_lower for p in _already_phrases):
            continue
        # Try to extract a path from the beginning of the line
        # Pattern: "/some/path: Already in library" or "/some/path Already in library"
        candidate = line.split(":")[0].strip().strip('"').strip("'")
        if candidate and Path(candidate).exists() and Path(candidate).is_dir():
            sub = Path(candidate)
            try:
                sub_resolved = str(sub.resolve())
            except Exception:
                sub_resolved = str(sub)
            if (any(_path_is_under(Path(sub_resolved), Path(r)) for r in _DOWNLOADS_ROOTS)
                    and not _path_is_under(Path(sub_resolved), MUSIC_ROOT)):
                folders_to_delete.add(str(sub))

    def _delete_staging_folder(folder) -> bool:
        # Resolve once against the staging roots (never the library, a
        # staging root itself, protected data, or a symlink), then re-check
        # with lstat right before deleting (S1/F3). Failures are reported,
        # never swallowed.
        try:
            target = composite_workflows._validated_staging_target(folder, "delete")
            if _path_is_under(target, MUSIC_ROOT):
                raise ValueError(f"Refusing to delete inside the music library: {folder}")
            composite_workflows._remove_resolved(target)
            return True
        except (OSError, ValueError) as ex:
            log.append(f"  [cleanup] Not deleted {Path(str(folder)).name}: {ex}")
            return False

    for folder in folders_to_delete:
        if _delete_staging_folder(folder):
            log.append(f"  [cleanup] Deleted (already in library): {Path(folder).name}")
            deleted_any = True

    # Strategy 2: if no specific folders were found and src DIRECTLY contains
    # audio files (i.e. it IS a leaf album folder), delete it.
    # Intentionally NOT recursive — avoids nuking a parent folder that has
    # unimported albums in subdirectories alongside the already-present one.
    if not deleted_any and src.is_dir():
        try:
            direct_audio = [f for f in src.iterdir()
                            if f.is_file() and f.suffix.lower() in audio_exts]
            if direct_audio and _delete_staging_folder(src):
                log.append(f"  [cleanup] Deleted source (already in library): {src.name}")
                deleted_any = True
        except Exception as ex:
            log.append(f"  [cleanup] Warning during cleanup: {ex}")

    return deleted_any


def _delete_album_ids_from_db(album_ids: list, log: list, *,
                              delete_files: bool = False) -> int:
    """Remove failed imported album rows through the engine transaction boundary."""
    ids = [int(aid) for aid in album_ids if str(aid).isdigit()]
    if not ids:
        return 0
    removed_files = 0
    removed_albums = 0
    if delete_files:
        log.append("  Failed-import cleanup removes Beets rows only; audio files are kept on disk.")
    for aid in ids:
        try:
            # LT-17: row-only, whatever delete_files says.
            res = composite_workflows.remove_album_rows_after_failed_import(
                int(aid), reason="failed import cleanup")
        except BeetsUnavailableError as ex:
            log.append(f"  Engine unavailable during failed-import cleanup for album_id {aid}: {ex}")
            continue
        except BeetsError as ex:
            log.append(f"  Engine cleanup rejected album_id {aid}: {ex}")
            continue
        except Exception as ex:
            _app_logger.warning("Failed-import cleanup failed for album_id=%s: %s", aid, type(ex).__name__)
            log.append(f"  Engine cleanup failed for album_id {aid}")
            continue
        if not res.get("ok") and not res.get("success"):
            log.append(f"  Engine cleanup rejected album_id {aid}: {res.get('error') or 'unknown error'}")
            continue
        removed_albums += 1
        log.append(f"  Removed failed import album_id {aid} rows through an engine transaction (files kept)")
    if removed_albums:
        log.append(f"  Removed failed import DB rows for {removed_albums} album(s); no file was deleted")
    return removed_files


def _delete_album_items_under_folder(album_id: int, folder_path: str, log: list) -> int:
    """Remove the DB rows of one album's items whose files are still under a
    failed staging folder.

    Rows only: the files are NOT deleted or quarantined here (S1). The
    removal is a ``playlist_media_cleanup_v1`` rows-only transaction that is
    approved on behalf of the failed-import cleanup that called this, then
    claimed, locked and verified like every other apply. Returns the number
    of rows actually removed."""
    if not album_id or not folder_path:
        return 0
    folder = Path(folder_path).resolve(strict=False)
    try:
        rows = composite_workflows.find_all_items_by_album_id(int(album_id))
    except BeetsUnavailableError as ex:
        log.append(f"  Staged-file cleanup failed: engine unavailable: {ex}")
        raise
    except Exception as ex:
        log.append(f"  Staged-file cleanup warning: {ex}")
        return 0

    delete_ids: list = []
    for row in rows:
        raw_path = _s(row["path"])
        if not raw_path:
            continue
        abs_path = Path(raw_path)
        if not abs_path.is_absolute():
            abs_path = MUSIC_ROOT / raw_path
        try:
            abs_resolved = abs_path.resolve(strict=False)
            abs_resolved.relative_to(folder)
        except Exception:
            continue
        delete_ids.append(int(row["id"]))

    if not delete_ids:
        return 0
    try:
        app_res = composite_workflows.remove_item_rows_keep_files(
            delete_ids, reason=f"failed staged import rows (album {int(album_id)})",
            approved_by="failed-import cleanup")
    except Exception as ex:
        log.append(f"  Staged-file DB cleanup warning: {ex}")
        return 0
    removed = list(app_res.get("deleted_items") or [])
    if not app_res.get("ok"):
        log.append(
            f"  Staged-file DB cleanup incomplete: removed {len(removed)} of {len(delete_ids)} row(s): "
            f"{app_res.get('error') or app_res.get('status')}")
        return len(removed)
    log.append(f"  Removed {len(removed)} failed staged DB item(s) (files kept)")
    return len(removed)


def _strip_year_from_album_name(aid: int, log: list) -> str:
    """If the album name ends with a year suffix like ' (2022)' or ' [2022]',
    strip it so the beets path template doesn't produce double-year folder names
    (e.g. 'Multiverse (2022) (2022)' → 'Multiverse (2022)').

    Returns the (possibly cleaned) album name.
    Updates metadata via the engine-owned album_metadata_repair_v1 transaction family."""
    try:
        album_dict = composite_workflows.get_album(aid)
        if not album_dict:
            return ""
        raw_name = str(album_dict.get("album") or "")
        clean_name = _YEAR_SFXRE.sub("", raw_name).strip()
        if clean_name and clean_name != raw_name:
            update_res = composite_workflows.update_album_metadata(aid, {"album": clean_name})
            _require_attach_stage_success(update_res, "album year-strip metadata update")
            log.append(f"  ↳ Cleaned album name: {raw_name!r} → {clean_name!r}")
        elif not clean_name and raw_name:
            log.append(f"  ↳ Album name is a bare year ({raw_name!r}) — keeping as-is")
        return clean_name or raw_name
    except Exception as ex:
        log.append(f"  WARN: album name cleanup failed: {ex}")
        return ""


# ── AI matching: folder evidence builder and candidate scorer ──────────────────
_AI_EVIDENCE_ROOT_DIRS = frozenset({
    "music", "torrents", "downloads", "data", "media", "audio",
    "import", "failed_imports", "failed-imports", "failed imports",
    "flac", "mp3", "lossless", "incoming", "unsorted", "new",
})


def _build_folder_evidence(folder_path: str) -> Dict[str, Any]:
    """Extract matching evidence from a downloads folder without network calls.

    Returns: folder_path, audio_files (list[str]), folder_track_count,
    nested_audio_count, guessed_artist, guessed_album, guessed_year,
    track_titles (list[str]), track_lines (list[str]), filenames (list[str]).
    """
    folder, err = _resolve_import_review_source_path(folder_path, allow_music=True, expected_type="dir")
    if err or not folder:
        return {
            "folder_path": _s(folder_path),
            "audio_files": [],
            "folder_track_count": 0,
            "nested_audio_count": 0,
            "guessed_artist": "",
            "guessed_album": "",
            "guessed_year": "",
            "track_titles": [],
            "track_lines": [],
            "filenames": [],
        }

    direct_audio = sorted(
        p for p in folder.iterdir()
        if not p.is_symlink() and p.is_file() and p.suffix.lower() in AUDIO_EXT
    ) if folder.is_dir() else []
    all_audio = sorted(
        p for p in folder.rglob("*")
        if not p.is_symlink() and p.is_file() and p.suffix.lower() in AUDIO_EXT
    ) if folder.is_dir() else []
    audio_files = direct_audio or all_audio
    nested_audio_count = max(0, len(all_audio) - len(direct_audio))

    track_titles: List[str] = []
    track_lines: List[str] = []
    tag_artists: List[str] = []
    tag_albums: List[str] = []
    tag_years: List[str] = []
    def _strip_stamps(s: str) -> str:
        return re.sub(r"\{[^{}]*\}", "", s).strip(" -_.")

    for f in audio_files[:20]:
        try:
            tags = _read_file_media_tags(f)
            t = tags.get("track", "") or ""
            title = _strip_stamps((tags.get("title", "") or "").strip())
            if title:
                track_titles.append(title)
                track_lines.append(
                    f"  {t}. {title} — {tags.get('artist', '') or ''} [{tags.get('album', '') or ''}] ({tags.get('year', '') or ''})"
                )
            else:
                _stem = _strip_stamps(f.stem)
                track_titles.append(_stem)
                track_lines.append(f"  {_stem}")
            # Collect tag-derived metadata for artist/album/year guessing
            _ta = str(tags.get("albumartist") or tags.get("artist") or "").strip()
            _tb = str(tags.get("album") or "").strip()
            _ty = str(tags.get("year") or "").strip()[:4]
            if _ta:
                tag_artists.append(_ta)
            if _tb:
                tag_albums.append(_tb)
            if _ty and len(_ty) == 4 and _ty.isdigit():
                tag_years.append(_ty)
        except Exception:
            _stem = _strip_stamps(f.stem)
            track_titles.append(_stem)
            track_lines.append(f"  {_stem}")

    disc_context = bool(_AI_EVIDENCE_DISC_FOLDER_RE.match(folder.name))
    context_folder = folder.parent if disc_context and folder.parent != folder else folder

    guessed_album = context_folder.name
    _raw_artist_folder = (
        context_folder.parent.name
        if context_folder.parent.name.lower() not in _AI_EVIDENCE_ROOT_DIRS else ""
    )
    guessed_artist = _raw_artist_folder
    # Extract MB artist UUID stamped in parent folder name, e.g. "Artist (uuid)"
    _artist_mbid_m = re.search(
        r"\(([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\)",
        _raw_artist_folder, re.I,
    )
    guessed_artist_mbid = _artist_mbid_m.group(1).lower() if _artist_mbid_m else ""
    guessed_year = ""

    sceneish = (
        "_" in context_folder.name
        or bool(re.search(r"\b(?:READNFO|NFOFIX|PROPER|REPACK|RERIP|WEB|FLAC|MP3|\d{1,2}BIT|\d{1,2}CD)\b", context_folder.name, re.I))
    )
    if not guessed_artist or sceneish:
        scene_artist, scene_album, scene_year = _ai_evidence_scene_guess(context_folder.name)
        if scene_artist:
            guessed_artist = scene_artist
        if scene_album:
            guessed_album = scene_album
        if scene_year:
            guessed_year = scene_year

    guessed_artist = _ai_evidence_clean_artist_guess(guessed_artist)
    guessed_album = _ai_evidence_clean_segment(guessed_album)
    guessed_album, album_year = _ai_evidence_extract_year(guessed_album)
    if album_year and not guessed_year:
        guessed_year = album_year

    # Supplement folder-name guesses with audio tag data when tags are consistent.
    # If ≥50% of sampled files agree on a value, it's more reliable than folder names.
    def _tag_majority(values: List[str]) -> str:
        if not values:
            return ""
        counts: Dict[str, int] = {}
        for v in values:
            counts[v] = counts.get(v, 0) + 1
        best, best_n = max(counts.items(), key=lambda kv: kv[1])
        return best if best_n >= max(1, len(values) // 2) else ""

    _n = len(audio_files[:20])
    if _n:
        _tag_artist = _tag_majority(tag_artists)
        _tag_album  = _tag_majority(tag_albums)
        _tag_year   = _tag_majority(tag_years)
        # Prefer tag artist when folder guess is empty or a generic root dir name
        if _tag_artist and (
            not guessed_artist
            or guessed_artist.lower() in _AI_EVIDENCE_ROOT_DIRS
            or _ai_evidence_weak_artist_guess(guessed_artist)
        ):
            guessed_artist = _tag_artist
        # Prefer tag album when folder name is generic, scene-noisy, or a hash/ID.
        if _tag_album and (
            not guessed_album
            or _ai_evidence_weak_album_guess(guessed_album, disc_context=disc_context)
            or (not " " in guessed_album and re.match(r"^[a-z0-9_\-\.]+$", guessed_album))
        ):
            guessed_album = _tag_album
        # Prefer tag year when folder name didn't yield one
        if _tag_year and not guessed_year:
            guessed_year = _tag_year

    return {
        "folder_path":        str(folder_path),
        "audio_files":        [str(p) for p in audio_files],
        "folder_track_count": len(audio_files),
        "nested_audio_count": nested_audio_count,
        "guessed_artist":     guessed_artist,
        "guessed_artist_mbid": guessed_artist_mbid,
        "guessed_album":      guessed_album,
        "guessed_year":       guessed_year,
        "track_titles":       track_titles,
        "track_lines":        track_lines,
        "filenames":          [f.name for f in audio_files[:30]],
    }


def _stamp_album_release_id(album_id: int, mb_albumid: str,
                            log: Optional[List[str]] = None) -> int:
    """Persist the selected MB release ID on the album and all its item rows."""
    try:
        aid = int(album_id or 0)
    except Exception:
        aid = 0
    mbid = _s(mb_albumid).strip().lower()
    if aid <= 0 or not _MB_UUID_RE.match(mbid):
        return 0
    try:
        res = composite_workflows.update_album_metadata(aid, {"mb_albumid": mbid})
        changed = int(res.get("items_changed") or 0)
        if log is not None and changed:
            log.append(f"  Stamped mb_albumid on {changed} item row(s).")
        return changed
    except Exception as ex:
        if log is not None:
            log.append(f"  WARN stamping mb_albumid: {ex}")
        return 0


def _run_item_metadata_restore(item_id: int, fields: Dict[str, Any], log: List[str], cancel_event=None) -> bool:
    restore_fields = {str(k): v for k, v in (fields or {}).items()}
    if not restore_fields:
        log.append("  [rollback] No metadata fields to restore.")
        return True
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise AttachRecordingCancelled("rollback cancelled")
        result = composite_workflows.update_item_metadata(item_id, restore_fields, force_write_tags=False)
        _require_attach_stage_success(result, "rollback metadata restore")
        _invalidate_lib_cache()
        log.append(f"  [rollback] Restored metadata for item {item_id}.")
        return True
    except AttachRecordingCancelled:
        log.append(f"  [rollback] Metadata restore cancelled for item {item_id}.")
        return False
    except Exception as ex:
        log.append(f"  [rollback] Metadata restore failed for item {item_id}: {_redact_security_text(str(ex))[:200]}")
        return False


# ── attach-recording: per-item reservation + strict subprocess outcomes ───────
#
# Concurrency: keyed by Beets item id, process-local, and deliberately
# narrow to this one feature -- two concurrent attach-recording requests for
# the SAME item can never race each other into conflicting mutations, while
# unrelated items proceed fully in parallel. Do NOT reuse _IMPORT_JOB_LOCK
# (global/cross-feature) or add any global attachment lock.
_ATTACH_RECORDING_RESERVATIONS_LOCK = threading.Lock()


_ATTACH_RECORDING_RESERVED_ITEMS: set = set()


def _reserve_attach_recording_item(item_id: int) -> bool:
    with _ATTACH_RECORDING_RESERVATIONS_LOCK:
        if item_id in _ATTACH_RECORDING_RESERVED_ITEMS:
            return False
        _ATTACH_RECORDING_RESERVED_ITEMS.add(item_id)
        return True


def _release_attach_recording_item(item_id: int) -> None:
    with _ATTACH_RECORDING_RESERVATIONS_LOCK:
        _ATTACH_RECORDING_RESERVED_ITEMS.discard(item_id)


def _run_item_recording_id_restore(item_id: int, fields: Dict[str, Any], log: List[str], cancel_event=None) -> bool:
    """Undo executor for attach-recording through controlled engine IPC."""
    restore_fields = {
        "mb_trackid": _s((fields or {}).get("mb_trackid", "")).strip().lower(),
        "mb_albumid": _s((fields or {}).get("mb_albumid", "")).strip().lower(),
        "mb_releasegroupid": _s((fields or {}).get("mb_releasegroupid", "")).strip().lower(),
    }
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise AttachRecordingCancelled("rollback cancelled")
        result = composite_workflows.update_item_metadata(item_id, restore_fields, force_write_tags=True)
        _require_attach_stage_success(result, "rollback recording identity restore")
        album_id = 0
        try:
            item = lib.get_item(item_id)
            album_id = int(getattr(item, "album_id", 0) or 0) if item else 0
        except Exception:
            album_id = 0
        if album_id > 0:
            relocate_result = composite_workflows.relocate_album(album_id)
            _require_attach_stage_success(relocate_result, "rollback recording relocation")
        _invalidate_lib_cache()
        log.append(f"  [rollback] Restored recording identity for item {item_id}.")
        return True
    except AttachRecordingCancelled:
        log.append(f"  [rollback] Recording ID restore cancelled for item {item_id}.")
        return False
    except Exception as ex:
        log.append(f"  [rollback] Recording ID restore failed for item {item_id}: {_redact_security_text(str(ex))[:200]}")
        return False


def _reconstruct_track_recording_candidates(item, iid: int):
    """Rebuild the current-tag snapshot and enriched MB/AcoustID recording
    candidates for a singleton item -- the same trusted candidate-generation
    pipeline /api/items/<iid>/ai-suggest uses (AcoustID lookup + MusicBrainz
    text search + matching-contract enrichment). attach-recording calls this
    fresh at request time instead of trusting anything the browser sends, so
    the set of Recording IDs it will accept always reflects current backend
    evidence, not a snapshot the client could have held onto or edited."""
    item_path = _item_ai_abs_path(item)
    filename = Path(item_path or _s(item.path)).name
    raw_year = str(item.year or "")
    clean_year = re.match(r'^((?:19|20)\d{2})', raw_year)
    clean_year = clean_year.group(1) if clean_year else raw_year

    current = {
        "title":       item.title       or "",
        "artist":      item.artist      or "",
        "album":       item.album       or "",
        "albumartist": item.albumartist or "",
        "year":        clean_year,
        "genre":       _s(getattr(item, "genre", "")) or "",
        "track":       item.track       or "",
        "label":       _s(getattr(item, "label", "")) or "",
        "mb_trackid":  _s(getattr(item, "mb_trackid", "")) or "",
        "mb_albumid":  _s(getattr(item, "mb_albumid", "")) or "",
        "mb_releasegroupid": _s(getattr(item, "mb_releasegroupid", "")) or "",
        "filename": filename,
        "source_path": item_path,
        "duration_seconds": float(getattr(item, "length", 0) or 0),
        "duration": _format_duration(float(getattr(item, "length", 0) or 0)),
    }

    stem = Path(filename).stem
    stem_clean = re.sub(r'^\d+\s*[-_.]\s*', '', stem).strip()
    search_title  = current["title"] or stem_clean
    search_artist = current["artist"] or current["albumartist"] or ""
    if " - " in search_title:
        parts = search_title.split(" - ", 1)
        candidate_artist, candidate_title = parts[0].strip(), parts[1].strip()
        _junk_patterns = re.compile(
            r'radio|station|channel|network|records|music|media|official|vevo|'
            r'entertainment|group|label|^various',
            re.I)
        if (not search_artist or _junk_patterns.search(search_artist)
                or len(search_artist) > 30):
            search_title  = candidate_title
            search_artist = candidate_artist

    _mb_t, _mb_a = _clean_for_mb(search_title, search_artist)

    acoustid_cands = _acoustid_lookup_cached(item_path) if item_path else []
    mb_text_cands = _mb_recording_search(_mb_t, _mb_a, limit=6)
    if not mb_text_cands and _mb_a:
        mb_text_cands = _mb_recording_search(_mb_t, "", limit=6)

    mb_candidates = recording_review.merge_recording_candidates(
        acoustid_cands, mb_text_cands, item_path=item_path,
        score_fn=lambda c: _score_track_ai_candidate(current, _mb_t, _mb_a, filename, c),
    )
    recording_review.enrich_and_index(
        mb_candidates,
        lambda c, hits: _enrich_track_ai_candidate(current, c, item_id=iid, acoustid_hits=hits),
        recording_review.acoustid_hits_for(item_path, acoustid_cands),
    )
    return current, mb_candidates, item_path, filename


def _artist_library_mbid(artist_name: str) -> str:
    """Prefer the MusicBrainz artist ID already stamped in the Beets library."""
    target = _artist_name_key(artist_name)
    if not target:
        return ""
    counts: Counter = Counter()
    try:
        for ba in lib.albums([]):
            names = [
                _s(getattr(ba, "albumartist", "") or ""),
                _s(getattr(ba, "albumartist_credit", "") or ""),
            ]
            names += _split_beets_multi(_s(getattr(ba, "albumartists", "") or ""))
            names += _split_beets_multi(_s(getattr(ba, "albumartists_credit", "") or ""))
            if target not in {_artist_name_key(n) for n in names if n}:
                continue
            ids = (_split_mbid_values(_s(getattr(ba, "mb_albumartistids", "") or ""))
                   or _split_mbid_values(_s(getattr(ba, "mb_albumartistid", "") or "")))
            counts.update(ids)
    except Exception:
        pass
    return counts.most_common(1)[0][0] if counts else ""


def _album_item_position_hints(item: Dict[str, Any]) -> tuple:
    try:
        disc = int(item.get("disc") or 1)
    except Exception:
        disc = 1
    try:
        track = int(item.get("track") or 0)
    except Exception:
        track = 0
    path = _s(item.get("path", ""))
    if path:
        path_disc, path_track = _audio_position_from_path(path)
        if path_track and (not track or track == path_track):
            track = path_track
        if path_disc and (disc <= 1 or path_disc != 1):
            disc = path_disc
    return max(disc, 1), max(track, 0)


def _db_item_file_exists(raw_path: str) -> bool:
    """DEPRECATED (ARCH-007): Media filesystem checks must not run in Web Manager container."""
    return False


def _fetch_discography(artist_name: str) -> Dict[str, Any]:
    """Fetch + compare discography synchronously (runs in a thread). Returns result dict."""


    # --- Step 1: artist search ---
    mbid = _artist_library_mbid(artist_name)
    mb_artist = artist_name
    if mbid:
        req = _ur.Request(f"https://musicbrainz.org/ws/2/artist/{mbid}?fmt=json",
                          headers={"User-Agent": "BeetsWebControl/1.0"})
        try:
            with provider_boundary.opened("musicbrainz", req, timeout=20) as r:
                adata = json.loads(r.read())
            mb_artist = adata.get("name") or artist_name
        except Exception:
            mbid = ""
    if not mbid:
        q = _up.urlencode({"query": f'artist:"{artist_name}"', "limit": 8, "fmt": "json"})
        req = _ur.Request(f"https://musicbrainz.org/ws/2/artist?{q}",
                          headers={"User-Agent": "BeetsWebControl/1.0"})
        with provider_boundary.opened("musicbrainz", req, timeout=20) as r:
            adata = json.loads(r.read())

        artists = adata.get("artists", [])
        if not artists:
            raise ValueError(f"'{artist_name}' not found on MusicBrainz")

        def _artist_score(a):
            nm = _artist_name_key(a.get("name", ""))
            sn = _artist_name_key((a.get("sort-name") or "").split(",", 1)[0])
            want = _artist_name_key(artist_name)
            if nm == want or sn == want:
                return 100
            if want and (want in nm or nm in want):
                return 75
            return int(a.get("score") or 0)

        artist_hit = max(artists, key=_artist_score)
        mbid      = artist_hit["id"]
        mb_artist = artist_hit.get("name") or artist_name

    # --- Step 2: release-groups (page through, respecting 1 req/s) ---
    rgs: list = []
    offset = 0
    while True:
        time.sleep(1.1)
        p = _up.urlencode({"artist": mbid, "limit": 100, "offset": offset, "fmt": "json"})
        req = _ur.Request(f"https://musicbrainz.org/ws/2/release-group?{p}",
                          headers={"User-Agent": "BeetsWebControl/1.0"})
        try:
            with provider_boundary.opened("musicbrainz", req, timeout=20) as r:
                rdata = json.loads(r.read())
        except Exception:
            break
        batch = rdata.get("release-groups", [])
        rgs.extend(batch)
        if len(batch) < 100:
            break
        offset += 100

    # --- Step 3: compare against disk ---
    # (MUSIC_ROOT is a module-level constant)
    album_index = _artist_album_index(artist_name, mbid)

    have, missing = [], []
    for rg in rgs:
        title     = rg.get("title", "")
        year      = (rg.get("first-release-date") or "")[:4]
        rg_type   = rg.get("primary-type", "Album")
        sec_types = rg.get("secondary-types", [])
        rg_mbid   = rg.get("id", "")
        on_disk, match_reason = _album_title_match(title, album_index, rgid=rg_mbid)
        rec = {"album": title, "year": year, "type": rg_type,
               "subtypes": sec_types, "mbid": rg_mbid,
               "mb_url": f"https://musicbrainz.org/release-group/{rg_mbid}",
               "on_disk": on_disk, "match_reason": match_reason}
        (have if on_disk else missing).append(rec)

    have.sort(   key=lambda r: r["year"] or "9999")
    missing.sort(key=lambda r: r["year"] or "9999")
    return {"ok": True, "mb_artist": mb_artist, "mbid": mbid,
            "have": have, "missing": missing, "total": len(rgs)}


def _artist_names_for_album(album: Dict[str, Any], fallback_artist: str,
                            known_artists: Optional[set] = None) -> List[str]:
    names = _split_beets_multi(album.get("albumartists", ""))
    if len(names) >= 2:
        return names
    names = _split_beets_multi(album.get("albumartists_credit", ""))
    if len(names) >= 2:
        return names
    credit = (
        album.get("albumartist_credit")
        or album.get("albumartist")
        or fallback_artist
    )
    return _split_collab_credit(credit, known_artists)


def _recompute_artist_totals(artist: Dict[str, Any]) -> None:
    albums = artist.get("albums", [])
    real_albums = [a for a in albums if not a.get("virtual_appearance")]
    artist["total"] = sum(int(a.get("track_count") or len(a.get("tracks", []))) for a in albums)
    artist["imported"] = sum(
        int(a.get("track_count") or len(a.get("tracks", [])))
        - int(a.get("not_imported") or 0)
        - int(a.get("missing") or 0)
        for a in real_albums
    )
    artist["not_imported"] = sum(int(a.get("not_imported") or 0) for a in real_albums)
    artist["missing"] = sum(int(a.get("missing") or 0) for a in real_albums)


def _apply_collaboration_album_views(result: List[Dict[str, Any]]) -> None:
    """Show multi-artist albums under each credited artist without copying files.

    The first credited artist becomes the primary display location, mirroring
    Lidarr's practical behavior. Other credited artists get virtual appearances.
    """
    by_name: Dict[str, Dict[str, Any]] = {a["name"]: a for a in result}
    known = set(by_name)
    pending_adds: List[tuple] = []

    for artist in list(result):
        kept = []
        for album in artist.get("albums", []):
            names = _artist_names_for_album(album, artist["name"], known)
            if len(names) < 2:
                kept.append(album)
                continue

            primary = names[0]
            album["credited_artists"] = names
            album["primary_artist"] = primary
            album["collaboration"] = True

            source_name = artist["name"]
            source_is_credit_bucket = source_name.casefold() not in {n.casefold() for n in names}
            if source_is_credit_bucket:
                base = dict(album)
                base["virtual_appearance"] = False
                base["source_artist_bucket"] = source_name
                pending_adds.append((primary, base))
                for other in names[1:]:
                    clone = dict(album)
                    clone["virtual_appearance"] = True
                    clone["source_artist_bucket"] = source_name
                    pending_adds.append((other, clone))
                continue

            if source_name.casefold() == primary.casefold():
                kept.append(album)
                for other in names[1:]:
                    clone = dict(album)
                    clone["virtual_appearance"] = True
                    clone["source_artist_bucket"] = source_name
                    pending_adds.append((other, clone))
            else:
                album["virtual_appearance"] = True
                kept.append(album)
                base = dict(album)
                base["virtual_appearance"] = False
                base["source_artist_bucket"] = source_name
                pending_adds.append((primary, base))
        artist["albums"] = kept

    for target_name, album in pending_adds:
        target = by_name.get(target_name)
        if not target:
            target = {
                "name": target_name,
                "albums": [],
                "total": 0,
                "imported": 0,
                "not_imported": 0,
                "missing": 0,
                "virtual_artist": True,
            }
            by_name[target_name] = target
            result.append(target)
        album_key = album.get("album_id") or album.get("aldir") or album.get("album")
        if any((a.get("album_id") or a.get("aldir") or a.get("album")) == album_key
               for a in target.get("albums", [])):
            continue
        target["albums"].append(album)

    result[:] = [a for a in result if a.get("albums")]
    for artist in result:
        artist["albums"].sort(key=lambda a: (
            a.get("virtual_appearance", False),
            a.get("year") or 0,
            _s(a.get("album", "")).casefold(),
        ))
        _recompute_artist_totals(artist)


# ── Library cache ─────────────────────────────────────────────────────────────
_LIB_CACHE_TTL = 180.0  # comfortably longer than _auto_scan_loop's 2-min proactive rebuild tick, so a page visit essentially never has to pay for a synchronous rebuild


def _library_no_mb_album_matches_folder(album_id: int, folder_path: str) -> bool:
    """True when a missing-MBID album row maps wholly to the requested folder."""
    try:
        aid = int(album_id or 0)
    except Exception:
        return False
    raw_folder = _s(folder_path).strip()
    if aid <= 0 or not raw_folder:
        return False

    folder = Path(raw_folder).resolve(strict=False)
    if not _path_is_under(folder, MUSIC_ROOT):
        return False

    try:
        album_row = composite_workflows.get_album(aid)
        if not album_row or _s(album_row.get("mb_albumid")).strip():
            return False
        rows = composite_workflows.find_all_items_by_album_id(aid)
    except BeetsUnavailableError:
        raise
    except Exception:
        return False

    if not rows:
        return False

    matched = 0
    for row in rows:
        raw_path = _s(row["path"])
        if not raw_path:
            return False
        abs_path = Path(raw_path)
        if not abs_path.is_absolute():
            abs_path = MUSIC_ROOT / raw_path
        if not _path_is_under(abs_path, folder):
            return False
        matched += 1
    return matched > 0


def _delete_review_source_folder(src_path: str, log: list,
                                 confirmed_wrong_library_folder: bool = False,
                                 album_id: int = 0) -> Dict[str, Any]:
    """Delete a pending-review source folder via engine-owned transaction."""
    plan_req = {
        "path": src_path,
        "action": "delete_folder",
        "allow_delete": True,
        "album_id": album_id,
        "confirmed_wrong_library_folder": confirmed_wrong_library_folder,
    }
    plan_res = composite_workflows.plan_import_review_cleanup(plan_req)
    if not plan_res.get("ok"):
        raise ValueError(plan_res.get("error", "Failed to create folder deletion plan."))

    op_id = plan_res.get("operation_id")
    if plan_res.get("library_paths_quarantined"):
        log.append("  Folder is inside the music library: its files are quarantined, not deleted.")
    apply_res = composite_workflows.apply_import_review_cleanup(
        op_id, approved_by="operator request (review source folder delete)")
    for l in apply_res.get("log", []):
        log.append(f"  {l}")
    if not apply_res.get("ok"):
        raise ValueError(
            f"{apply_res.get('error', 'Failed to apply folder deletion plan.')} "
            f"(status {apply_res.get('status')}, operation {op_id})")

    try:
        _remove_pending_review_for_path(src_path, log)
    except Exception as ex:
        log.append(f"  Pending Review cleanup warning: {ex}")

    deleted_files = apply_res.get("deleted", [])
    moved_files = apply_res.get("moved", [])
    return {
        "operation_id": op_id,
        "deleted": deleted_files,
        "quarantined": moved_files,
        "files_removed": len(deleted_files) + len(moved_files),
        "status": apply_res.get("status") or "Completed",
    }


def _representative_tracktotal(values: List[int]) -> int:
    counts = Counter(v for v in values if v > 0 and v < 300)
    if not counts:
        return 0
    top_count = max(counts.values())
    return max(value for value, count in counts.items() if count == top_count)


def _expected_track_count_from_library_rows(tracks: List[Dict[str, Any]]) -> int:
    """Best-effort expected album track count from Beets per-disc track totals."""
    per_disc: Dict[int, List[int]] = {}
    for track in tracks:
        try:
            track_total = int(track.get("tracktotal") or 0)
            disc = int(track.get("disc") or 1)
        except Exception:
            continue
        if track_total > 0 and track_total < 300:
            per_disc.setdefault(disc, []).append(track_total)
    return sum(_representative_tracktotal(values) for values in per_disc.values()) if per_disc else 0


def _library_album_is_disk_only(album: Dict[str, Any]) -> bool:
    try:
        album_id = int(album.get("album_id") or 0)
    except Exception:
        album_id = 0
    return album_id <= 0 and int(album.get("not_imported") or 0) > 0


def _library_recompute_artist_counts(artist: Dict[str, Any]) -> Dict[str, Any]:
    next_artist = dict(artist)
    albums = list(next_artist.get("albums") or [])
    total = sum(int(album.get("track_count") or len(album.get("tracks") or [])) for album in albums)
    missing = sum(int(album.get("missing") or 0) for album in albums)
    not_imported = sum(int(album.get("not_imported") or 0) for album in albums)
    next_artist["total"] = total
    next_artist["missing"] = missing
    next_artist["not_imported"] = not_imported
    next_artist["imported"] = max(0, total - missing - not_imported)
    next_artist["empty_artist_folder"] = not albums
    return next_artist


def _library_payload_for_response(payload: Dict[str, Any], include_tracks: bool = False,
                                  include_disk_only: bool = False) -> Dict[str, Any]:
    """Return the library payload in summary form unless full track rows were requested."""
    if include_tracks:
        out = dict(payload)
        if not include_disk_only:
            artists = []
            for artist in out.get("artists") or []:
                next_artist = dict(artist)
                next_artist["albums"] = [
                    album for album in next_artist.get("albums") or []
                    if not _library_album_is_disk_only(album)
                ]
                if next_artist["albums"]:
                    artists.append(_library_recompute_artist_counts(next_artist))
            out["artists"] = artists
            out["stats"] = _library_stats_for_artists(artists)
        out["tracks_included"] = True
        return out

    slim = dict(payload)
    slim_artists: List[Dict[str, Any]] = []
    for artist in payload.get("artists") or []:
        next_artist = dict(artist)
        next_albums = []
        for album in artist.get("albums") or []:
            next_album = dict(album)
            try:
                album_id = int(next_album.get("album_id") or 0)
            except Exception:
                album_id = 0
            if album_id > 0 and "tracks" in next_album:
                next_album.pop("tracks", None)
                next_album["tracks_deferred"] = True
            if not include_disk_only and _library_album_is_disk_only(next_album):
                continue
            next_albums.append(next_album)
        if not next_albums:
            continue
        next_artist["albums"] = next_albums
        slim_artists.append(_library_recompute_artist_counts(next_artist))
    slim["artists"] = slim_artists
    slim["stats"] = _library_stats_for_artists(slim_artists)
    slim["tracks_included"] = False
    return slim


def _library_stats_for_artists(artists: List[Dict[str, Any]]) -> Dict[str, int]:
    """Summary counts for the artists/albums tree produced by
    _build_library_payload(). Each entry in an artist's "albums" list is a
    disk-folder-derived CARD, not necessarily a real Beets album:

    - A card with album_id > 0 is a real Beets album. The same album_id can
      legitimately appear on more than one card (its tracks split across
      more than one disk folder, e.g. a reissue alongside the original) --
      count it once, not once per card.
    - A card with album_id == 0 and not_imported == 0 has no Beets album
      row, but every track on it IS imported -- a genuine singleton (or a
      synthetic "(Singles)" bucket of singletons). This is not a fake
      extra album; its tracks count toward the track total, not the album
      total.
    - A card with album_id == 0 and not_imported > 0 is genuinely disk-only
      (no Beets album row, at least one file never imported). Reported
      separately, never folded into "albums" or "tracks".

    Prior behavior counted every non-virtual card as "1 album" and summed
    every card's track_count into "tracks" -- so singleton pseudo-albums
    inflated the album count (roughly 1 per singleton), the same album
    split across multiple disk folders was counted multiple times, and
    disk-only content leaked into "tracks" whenever it reached this
    function unfiltered. Fixed by keying album identity on album_id and
    routing singleton/disk-only cards to their own counts instead.
    """
    album_ids_seen: set = set()
    tracks_total = 0
    singleton_tracks = 0
    disk_only_albums = 0
    disk_only_tracks = 0

    for artist in artists:
        for album in artist.get("albums", []):
            if album.get("virtual_appearance"):
                continue
            try:
                album_id = int(album.get("album_id") or 0)
            except Exception:
                album_id = 0
            track_count = int(album.get("track_count") or len(album.get("tracks") or []))
            not_imported = int(album.get("not_imported") or 0)

            if album_id > 0:
                album_ids_seen.add(album_id)
                # track_count is the card's total file count -- imported +
                # not-yet-imported extras + missing-on-disk (see
                # _build_library_payload: "track_count": len(tracks), where
                # `tracks` includes not-imported files sitting alongside a
                # real album). Only the imported+missing portion represents
                # actual Beets item rows; extra not-imported files on disk
                # must not inflate the track total for a real album.
                tracks_total += track_count - not_imported
            elif not_imported > 0:
                disk_only_albums += 1
                disk_only_tracks += track_count
            else:
                singleton_tracks += track_count
                tracks_total += track_count

    return {
        "artists": len(artists),
        "albums": len(album_ids_seen),
        "tracks": tracks_total,
        "singleton_tracks": singleton_tracks,
        "disk_only_albums": disk_only_albums,
        "disk_only_tracks": disk_only_tracks,
    }


def _library_track_dict(item) -> Dict[str, Any]:
    get_val = (lambda k, d=None: item.get(k, d)) if isinstance(item, dict) else (lambda k, d=None: getattr(item, k, d))
    raw_path = _s(get_val("path", "") or "")
    abs_path = raw_path
    if raw_path and not Path(raw_path).is_absolute():
        abs_path = str(MUSIC_ROOT / raw_path)
    exists = bool(abs_path and Path(abs_path).exists())
    length = get_val("length", None)
    try:
        length_value = float(length or 0) or None
    except Exception:
        length_value = None
    return {
        "id": int(get_val("id", 0) or 0),
        "album_id": int(get_val("album_id", 0) or 0),
        "path": abs_path or raw_path,
        "title": _s(get_val("title", "") or (Path(raw_path).stem if raw_path else "")),
        "track": int(get_val("track", 0) or 0),
        "disc": int(get_val("disc", 0) or 1),
        "tracktotal": int(get_val("tracktotal", 0) or 0),
        "ok": exists,
        "missing": not exists,
        "imported": True,
        "disk_only": False,
        "status": "imported" if exists else "missing_file",
        "mb_trackid": _s(get_val("mb_trackid", "") or ""),
        "length": length_value,
    }


def get_library_payload(*, force: bool = False, include_tracks: bool = False,
                        include_disk_only: bool = False) -> Dict[str, Any]:
    """Library page payload from the shared cache, rebuilt when stale or forced.

    Request-free service behind GET /api/library (ARCH-001)."""
    now   = time.time()
    cached_payload, cached_ts = library_cache.snapshot()
    if not force and cached_payload and (now - cached_ts) < _LIB_CACHE_TTL:
        return _library_payload_for_response(
            cached_payload, include_tracks, include_disk_only)

    payload = _refresh_library_cache()
    return _library_payload_for_response(
        payload, include_tracks, include_disk_only)


def _refresh_library_cache() -> dict:
    """Build a fresh library payload and store it as the shared cache.

    Safe to call from a request handler or the background auto-scan loop
    below -- no Flask request context needed. Splitting this out of
    library_full() lets the background loop proactively rebuild the cache
    on its own schedule instead of just invalidating it, so a page visit
    almost always hits an already-warm cache instead of paying for a full
    rebuild synchronously (a large library's cold build can take ~25s+).
    """
    payload = _build_library_payload()
    library_cache.store(payload)
    return payload


def _build_library_payload() -> dict:
    """Walk /data/media/music on disk + inject library items whose files are
    missing (shown in red). Pure builder -- no caching or request handling,
    so both library_full() and the background cache-warmer can call it.
    Do not change this traversal logic without running the parity suite
    (tests/test_arch012_missing_album_library_parity.py).
    """
    # Build lookup: file path → beets item (for import-status annotation)
    # Beets stores paths relative to the music root (e.g. "Artist/Album/song.flac").
    # We register BOTH the relative form AND the absolute form so the disk-walk lookup works.
    _MROOT = str(MUSIC_ROOT)
    _TYPE_ORDER = {"album": 0, "ep": 1, "mixtape": 2, "single": 3,
                   "broadcast": 4, "other": 5, "": 6}
    path_to_id:   Dict[str, int] = {}
    path_to_item: Dict[str, Any] = {}
    all_lib_items = list(lib.items([]))
    for item in all_lib_items:
        p = _s(item.path)
        # Resolve relative beets path → absolute
        abs_p = (_MROOT + "/" + p) if (p and not p.startswith("/")) else p
        path_to_id[abs_p]   = item.id
        path_to_item[abs_p] = item
        if abs_p != p:          # also register the raw form for safety
            path_to_id[p]   = item.id
            path_to_item[p] = item

    # Build beets album lookup: (albumartist.lower(), album.lower()) → album metadata
    beets_album_lk: Dict[tuple, dict] = {}
    beets_album_lk_by_id: Dict[int, dict] = {}   # fallback: album DB id → metadata
    try:
        for ba in lib.albums([]):
            try:
                a_name = _s(getattr(ba, "albumartist", "") or getattr(ba, "artist", "") or "")
                a_alb  = _s(getattr(ba, "album", "") or "")
                key    = (a_name.lower(), a_alb.lower())
                entry  = {
                    "id":                ba.id,
                    "artpath":           _s(getattr(ba, "artpath", "") or ""),
                    "albumartist":       a_name,
                    "albumartist_credit": _s(getattr(ba, "albumartist_credit", "") or ""),
                    "albumartists":      _s(getattr(ba, "albumartists", "") or ""),
                    "albumartists_credit": _s(getattr(ba, "albumartists_credit", "") or ""),
                    "mb_albumartistid":  _s(getattr(ba, "mb_albumartistid", "") or ""),
                    "mb_albumartistids": _s(getattr(ba, "mb_albumartistids", "") or ""),
                    "mb_albumid":        _s(getattr(ba, "mb_albumid", "") or ""),
                    "mb_releasegroupid": _s(getattr(ba, "mb_releasegroupid", "") or ""),
                    "albumtype":         _s(getattr(ba, "albumtype",  "") or "").lower(),
                    "albumtypes":        _s(getattr(ba, "albumtypes", "") or "").lower(),
                }
                beets_album_lk[key] = entry
                beets_album_lk_by_id[ba.id] = entry
                # Also index by UUID-stripped artist name so disk-walk lookups
                # using artist_lc_bare can match even when the beets albumartist
                # field still contains a UUID stamp.
                bare_key = (_artist_folder_name_without_mbid(a_name).lower(), a_alb.lower())
                if bare_key != key:
                    beets_album_lk.setdefault(bare_key, entry)
            except Exception:
                pass
    except Exception:
        pass

    # Collect library items whose file is MISSING from disk
    # Group them by (artist, album) so we can inject them into the right tree bucket
    missing_by_bucket: Dict[tuple, list] = defaultdict(list)
    for item in all_lib_items:
        p = _s(item.path)
        # Resolve relative beets path to absolute for the exists() check
        abs_p = (_MROOT + "/" + p) if (p and not p.startswith("/")) else p
        if not Path(abs_p).exists():
            artist = item.albumartist or item.artist or "Unknown Artist"
            album  = item.album  or "Unknown Album"
            year   = item.year   or 0
            missing_by_bucket[(artist, album, year)].append({
                "id":         item.id,
                "album_id":   int(getattr(item, "album_id", 0) or 0),
                "path":       p,
                "title":      item.title or Path(p).stem,
                "track":      item.track or 0,
                "disc":       item.disc or 1,
                "tracktotal":  int(getattr(item, "tracktotal", 0) or 0),
                "ok":         False,   # imported but file gone
                "missing":    True,    # file doesn't exist on disk
                "imported":   True,
                "disk_only":  False,
                "status":     "missing_file",
                "mb_trackid": _s(getattr(item, "mb_trackid", "")),
            })

    # Build set of folder paths already queued for manual review (load once).
    try:
        _review_paths: set = {
            _s((item or {}).get("path", "")).strip().rstrip("/\\")
            for item in (_load_pending_reviews() or [])
        } - {""}
    except Exception:
        _review_paths = set()

    # --- Walk filesystem ---
    result = []
    seen_buckets: set = set()   # (artist_name, album_name) pairs we've added from disk
    seen_disk_album_ids: set = set()

    try:
        artist_dirs = sorted(
            [d for d in MUSIC_ROOT.iterdir() if d.is_dir()],
            key=lambda d: d.name.lower()
        ) if MUSIC_ROOT.exists() else []
    except PermissionError:
        artist_dirs = []

    for adir in artist_dirs:
        artist_name = adir.name
        artist_name_lc_bare = _artist_folder_name_without_mbid(artist_name).lower()
        albums: list = []
        total = imported_count = 0

        try:
            album_dirs = sorted(
                [d for d in adir.iterdir() if d.is_dir()],
                key=lambda d: d.name.lower()
            )
            loose_files = sorted(
                [f for f in adir.iterdir()
                 if f.is_file() and f.suffix.lower() in AUDIO_EXT],
                key=lambda f: f.name.lower()
            )
        except PermissionError:
            continue

        if loose_files:
            album_dirs = [None] + list(album_dirs)

        for aldir in (album_dirs if album_dirs else []):
            if aldir is None:
                album_name, year, files = "(Singles)", 0, loose_files
            else:
                album_name = aldir.name
                year = 0
                m2 = re.search(r'\((\d{4})\)\s*$', album_name)
                if m2:
                    year = int(m2.group(1))
                try:
                    files = sorted(
                        [f for f in aldir.iterdir()
                         if f.is_file() and f.suffix.lower() in AUDIO_EXT],
                        key=lambda f: f.name.lower()
                    )
                    # Multi-disc albums: Lidarr may store files in CD 01/, CD 02/,
                    # Disc 1/, Disc 2/ etc. sub-folders. Recurse one level if no
                    # audio files were found directly in the album folder.
                    if not files:
                        for sub in sorted(aldir.iterdir(), key=lambda d: d.name.lower()):
                            if sub.is_dir():
                                files += sorted(
                                    [f for f in sub.iterdir()
                                     if f.is_file() and f.suffix.lower() in AUDIO_EXT],
                                    key=lambda f: f.name.lower()
                                )
                except PermissionError:
                    files = []

            _disc_sub_re = re.compile(
                r'^(?:cd|disc|disk)\s*0*(\d+)$', re.IGNORECASE)

            tracks: list = []
            for f in files:
                fpath = str(f)
                item  = path_to_item.get(fpath)
                iid   = path_to_id.get(fpath)
                # Detect disc number: prefer beets DB value, fall back to
                # parsing the parent subfolder name (CD 01, Disc 2, etc.)
                disc_num = (item.disc if item and getattr(item, "disc", 0) else 0)
                if not disc_num:
                    parent_name = f.parent.name if aldir and f.parent != aldir else ""
                    dm = _disc_sub_re.match(parent_name)
                    if dm:
                        disc_num = int(dm.group(1))
                tracks.append({
                    "id":         iid or 0,
                    "album_id":   int(getattr(item, "album_id", 0) or 0) if item else 0,
                    "path":       fpath,
                    "title":      (item.title if item and item.title else f.stem),
                    "track":      (item.track if item else 0) or 0,
                    "disc":       disc_num or 1,
                    "tracktotal":  int(getattr(item, "tracktotal", 0) or 0) if item else 0,
                    "ok":         iid is not None,
                    "missing":    False,
                    "imported":   iid is not None,
                    "disk_only":  iid is None,
                    "status":     "imported" if iid is not None else "not_imported",
                    "mb_trackid": (_s(getattr(item, "mb_trackid", "")) if item else ""),
                })
                if iid:
                    imported_count += 1

            # Normalize disk folder name for matching against beets DB.
            # Strip UUID stamp FIRST so the year suffix lands at the end and can be stripped next.
            # e.g. "VULTURES 2 (2024){mbid}" → strip UUID → "VULTURES 2 (2024)" → strip year → "VULTURES 2"
            album_name_bare = re.sub(
                r'\s*\{[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\}\s*$',
                '', album_name, flags=re.IGNORECASE
            ).strip()
            album_name_bare = re.sub(r'\s*[\(\[]\d{4,8}[\)\]]\s*$', '', album_name_bare).strip()
            album_name_bare = _restore_time_colon_title(album_name_bare)
            album_name_bare_lc = album_name_bare.lower()

            # Consume ALL beets missing-track buckets that belong to this disk folder.
            # Beets may store the same album multiple times with different release dates
            # (e.g. year=20220729, year=20220802, year=20221020 for "Multiverse").
            # Without consuming all of them here, the leftovers appear as phantom cards.
            bucket_key = (artist_name, album_name, year)
            missing_here = missing_by_bucket.pop(bucket_key, [])

            # Collect every remaining key that matches artist + album (exact or year-stripped)
            artist_lc_bare = re.sub(
                r'\s*\([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\)\s*$',
                '', artist_name, flags=re.IGNORECASE
            ).strip().lower()
            album_lc  = album_name.lower()
            extra_keys = [
                k for k in list(missing_by_bucket.keys())
                if k[0].lower() == artist_lc_bare
                and (k[1].lower() == album_lc
                     or _restore_time_colon_title(
                         re.sub(r'\s*[\(\[]\d{4,8}[\)\]]\s*$', '', k[1]).strip()
                     ).lower() == album_name_bare_lc)
            ]
            for k in extra_keys:
                missing_here += missing_by_bucket.pop(k, [])

            # Deduplicate missing tracks by (title, track) so re-imports don't double-count
            seen_mt: set = set()
            deduped: list = []
            for t in missing_here:
                key_mt = (t.get("title", "").lower(), t.get("track", 0))
                if key_mt not in seen_mt:
                    seen_mt.add(key_mt)
                    deduped.append(t)
            missing_here = deduped

            tracks.extend(missing_here)
            seen_buckets.add(bucket_key)

            if not tracks:
                continue

            imported_album_ids = [
                int(t.get("album_id") or 0)
                for t in tracks
                if int(t.get("album_id") or 0) > 0
            ]
            dominant_album_id = (
                Counter(imported_album_ids).most_common(1)[0][0]
                if imported_album_ids else 0
            )
            seen_disk_album_ids.update(aid for aid in imported_album_ids if aid)
            ba_info = (
                beets_album_lk_by_id.get(dominant_album_id)
                or beets_album_lk.get((artist_lc_bare, album_name.lower()))
                or beets_album_lk.get((artist_lc_bare, album_name_bare_lc))
                or {}
            )
            # Fallback: album record has empty name/artist — look up by item's album_id
            if not ba_info:
                for _t in tracks:
                    _item = path_to_item.get(_t.get("path", ""))
                    if _item:
                        _aid = getattr(_item, "album_id", 0)
                        if _aid and _aid in beets_album_lk_by_id:
                            ba_info = beets_album_lk_by_id[_aid]
                            break
            album_db_id = int(ba_info.get("id") or 0)
            if album_db_id:
                for _t in tracks:
                    try:
                        track_album_id = int(_t.get("album_id") or 0)
                    except Exception:
                        track_album_id = 0
                    if track_album_id and track_album_id != album_db_id:
                        _t["status"] = "other_album"
                        _t["other_album_id"] = track_album_id
                        _t["imported"] = True
                        _t["disk_only"] = False
            not_imported = sum(1 for t in tracks if not t["ok"] and not t["missing"])
            missing_cnt  = sum(1 for t in tracks if t.get("missing"))
            imported_cnt = sum(1 for t in tracks if t.get("ok") and not t.get("missing"))
            expected_track_count = _expected_track_count_from_library_rows(tracks)
            # True when the album is complete in Beets relative to expected count
            # but has extra audio on disk.  These extras need Album Track Cleanup,
            # not re-import, so Import All should skip them.
            not_imported_is_extra = bool(
                album_db_id and ba_info.get("mb_albumid")
                and not_imported > 0 and missing_cnt == 0
                and expected_track_count > 0 and imported_cnt >= expected_track_count
            )
            total += len(tracks)
            # Detect cover art on disk even for albums not in beets DB
            disk_art = ""
            if aldir:
                for aname in ("albumart.jpg", "albumart.png", "folder.jpg",
                              "cover.jpg", "front.jpg", "cover.png"):
                    c = aldir / aname
                    if c.exists():
                        disk_art = str(c)
                        break
            mb_health = _fast_album_mb_health_fields(
                tracks, expected_track_count, missing_cnt)
            albums.append({
                "album":             album_name_bare if album_name_bare else album_name,
                "year":              year,
                "tracks":            sorted(tracks, key=lambda t: t["track"] or 999),
                "track_count":        len(tracks),
                "expected_track_count": expected_track_count,
                "not_imported":      not_imported,
                "not_imported_is_extra": not_imported_is_extra,
                "pending_review":    bool(
                    aldir and str(aldir).rstrip("/\\") in _review_paths
                ),
                "missing":           missing_cnt,
                "album_id":          ba_info.get("id", 0),
                "albumartist":       ba_info.get("albumartist", artist_name),
                "albumartist_credit": ba_info.get("albumartist_credit", ""),
                "albumartists":      ba_info.get("albumartists", ""),
                "albumartists_credit": ba_info.get("albumartists_credit", ""),
                "mb_albumartistid":  ba_info.get("mb_albumartistid", ""),
                "mb_albumartistids": ba_info.get("mb_albumartistids", ""),
                "artpath":           ba_info.get("artpath", "") or disk_art,
                "disk_art":          disk_art,
                "aldir":             str(aldir) if aldir else "",
                "mb_albumid":        ba_info.get("mb_albumid", ""),
                "mb_releasegroupid": ba_info.get("mb_releasegroupid", ""),
                "albumtype":         ba_info.get("albumtype", ""),
                "albumtypes":        ba_info.get("albumtypes", ""),
                **mb_health,
            })

        # Inject missing-only albums (library items with no matching disk folder).
        # Group multiple beets entries for the SAME album (different release dates) into ONE card.
        missing_album_groups: Dict[str, dict] = {}  # group_key → merged card data
        for (mart, malb, myr), mitems in list(missing_by_bucket.items()):
            if mart.lower() != artist_name_lc_bare:
                continue
            missing_by_bucket.pop((mart, malb, myr), None)
            m_album_ids = [
                int(t.get("album_id") or 0)
                for t in mitems
                if int(t.get("album_id") or 0) > 0
            ]
            m_dom_aid = (
                Counter(m_album_ids).most_common(1)[0][0]
                if m_album_ids else 0
            )

            malb_bare = re.sub(r'\s*[\(\[]\d{4,8}[\)\]]\s*$', '', malb).strip()
            malb_bare = _restore_time_colon_title(malb_bare)
            malb_key  = malb_bare.lower()
            ba_info2  = (
                beets_album_lk_by_id.get(m_dom_aid)
                or beets_album_lk.get((mart.lower(), malb.lower()))
                or beets_album_lk.get((_artist_folder_name_without_mbid(mart).lower(), malb_key))
                or {}
            )
            ba_info2_id = int(ba_info2.get("id") or m_dom_aid or 0)
            if ba_info2_id and ba_info2_id in seen_disk_album_ids:
                continue

            group_key = f"id:{ba_info2_id}" if ba_info2_id > 0 else f"name:{malb_key}"
            if group_key not in missing_album_groups:
                missing_album_groups[group_key] = {
                    "album":             malb_bare or malb,
                    "year":              myr,
                    "tracks":            [],
                    "track_count":        0,
                    "album_id":          ba_info2_id,
                    "albumartist":        ba_info2.get("albumartist", mart),
                    "albumartist_credit": ba_info2.get("albumartist_credit", ""),
                    "albumartists":       ba_info2.get("albumartists", ""),
                    "albumartists_credit": ba_info2.get("albumartists_credit", ""),
                    "mb_albumartistid":   ba_info2.get("mb_albumartistid", ""),
                    "mb_albumartistids":  ba_info2.get("mb_albumartistids", ""),
                    "artpath":           ba_info2.get("artpath", ""),
                    "mb_albumid":        ba_info2.get("mb_albumid", ""),
                    "mb_releasegroupid": ba_info2.get("mb_releasegroupid", ""),
                    "albumtype":         ba_info2.get("albumtype", ""),
                    "albumtypes":        ba_info2.get("albumtypes", ""),
                    "pending_review":    False,
                    "aldir":             "",
                    "disk_art":          "",
                }
            # Merge tracks, dedup by (title, track)
            existing_keys = {
                (t.get("title","").lower(), t.get("track",0))
                for t in missing_album_groups[group_key]["tracks"]
            }
            for t in mitems:
                tk = (t.get("title","").lower(), t.get("track",0))
                if tk not in existing_keys:
                    existing_keys.add(tk)
                    missing_album_groups[group_key]["tracks"].append(t)
            # Keep best year (prefer 4-digit year)
            if myr and str(myr)[:4].isdigit():
                cur_yr = missing_album_groups[group_key]["year"]
                if not cur_yr or len(str(cur_yr)) > 4:
                    missing_album_groups[group_key]["year"] = int(str(myr)[:4])
        for grp in missing_album_groups.values():
            grp["not_imported"] = 0
            grp["missing"]      = len(grp["tracks"])
            grp["track_count"]  = len(grp["tracks"])
            grp["expected_track_count"] = _expected_track_count_from_library_rows(grp["tracks"])
            grp["tracks"]       = sorted(grp["tracks"], key=lambda t: t["track"] or 999)
            grp.update(_fast_album_mb_health_fields(
                grp["tracks"], grp["expected_track_count"], grp["missing"]))
            albums.append(grp)
            total += grp["missing"]

        _TYPE_ORDER = {"album": 0, "ep": 1, "mixtape": 2, "single": 3,
                       "broadcast": 4, "other": 5, "": 6}
        albums.sort(key=lambda a: (
            _TYPE_ORDER.get(a.get("albumtype", ""), 6),
            a["year"] or 0,
            a["album"].lower()
        ))

        missing_total = sum(a["missing"] for a in albums)
        artist_name_display = _artist_folder_name_without_mbid(artist_name)
        result.append({
            "name":                artist_name_display,
            "albums":              albums,
            "total":               total,
            "imported":            imported_count,
            "not_imported":        total - imported_count - missing_total,
            "missing":             missing_total,
            "empty_artist_folder": not albums,
            "path":                str(adir),
        })

    # Any remaining missing items belong to artists not on disk at all
    artist_extras: Dict[str, dict] = {}
    for (mart, malb, myr), mitems in list(missing_by_bucket.items()):
        m_album_ids = [
            int(t.get("album_id") or 0)
            for t in mitems
            if int(t.get("album_id") or 0) > 0
        ]
        m_dom_aid = (
            Counter(m_album_ids).most_common(1)[0][0]
            if m_album_ids else 0
        )

        malb_bare = re.sub(r'\s*[\(\[]\d{4,8}[\)\]]\s*$', '', malb).strip()
        malb_bare = _restore_time_colon_title(malb_bare)
        malb_key  = malb_bare.lower()

        _ext_ba = (
            beets_album_lk_by_id.get(m_dom_aid)
            or beets_album_lk.get((mart.lower(), malb.lower()))
            or beets_album_lk.get((_artist_folder_name_without_mbid(mart).lower(), malb_key))
            or {}
        )
        ext_aid = int(_ext_ba.get("id") or m_dom_aid or 0)
        artist_display_name = _ext_ba.get("albumartist") or mart
        artist_key = artist_display_name.lower()

        if artist_key not in artist_extras:
            artist_extras[artist_key] = {
                "name": artist_display_name,
                "albums_by_key": {},
                "path": "",
            }

        album_key = f"id:{ext_aid}" if ext_aid > 0 else f"name:{malb_key}"
        if album_key not in artist_extras[artist_key]["albums_by_key"]:
            artist_extras[artist_key]["albums_by_key"][album_key] = {
                "album":               _restore_time_colon_title(malb_bare or malb),
                "year":                myr,
                "tracks":              [],
                "track_count":          0,
                "expected_track_count": 0,
                "albumartist":          artist_display_name,
                "not_imported":         0,
                "missing":              0,
                "pending_review":       False,
                "aldir":                "",
                "disk_art":             "",
                "album_id":             ext_aid,
                "artpath":              _ext_ba.get("artpath", ""),
                "mb_albumid":           _ext_ba.get("mb_albumid", ""),
                "mb_releasegroupid":    _ext_ba.get("mb_releasegroupid", ""),
                "albumtype":            _ext_ba.get("albumtype", ""),
                "albumtypes":           _ext_ba.get("albumtypes", ""),
                "albumartist_credit":   _ext_ba.get("albumartist_credit", ""),
                "albumartists":         _ext_ba.get("albumartists", ""),
                "albumartists_credit":  _ext_ba.get("albumartists_credit", ""),
                "mb_albumartistid":     _ext_ba.get("mb_albumartistid", ""),
                "mb_albumartistids":    _ext_ba.get("mb_albumartistids", ""),
                "not_imported_is_extra": False,
            }

        album_entry = artist_extras[artist_key]["albums_by_key"][album_key]
        existing_keys = {
            (t.get("title", "").lower(), t.get("track", 0))
            for t in album_entry["tracks"]
        }
        for t in mitems:
            tk = (t.get("title", "").lower(), t.get("track", 0))
            if tk not in existing_keys:
                existing_keys.add(tk)
                album_entry["tracks"].append(t)

        if myr and str(myr)[:4].isdigit():
            cur_yr = album_entry["year"]
            if not cur_yr or len(str(cur_yr)) > 4:
                album_entry["year"] = int(str(myr)[:4])

    final_extras = []
    for art_data in artist_extras.values():
        album_list = []
        art_total = 0
        for grp in art_data["albums_by_key"].values():
            grp["not_imported"] = 0
            grp["missing"] = len(grp["tracks"])
            grp["track_count"] = len(grp["tracks"])
            grp["expected_track_count"] = _expected_track_count_from_library_rows(grp["tracks"])
            grp["tracks"] = sorted(grp["tracks"], key=lambda t: t["track"] or 999)
            grp.update(_fast_album_mb_health_fields(
                grp["tracks"], grp["expected_track_count"], grp["missing"]))
            album_list.append(grp)
            art_total += grp["missing"]

        album_list.sort(key=lambda a: (
            _TYPE_ORDER.get(a.get("albumtype", ""), 6),
            a["year"] or 0,
            a["album"].lower()
        ))
        final_extras.append({
            "name": art_data["name"],
            "albums": album_list,
            "total": art_total,
            "imported": 0,
            "not_imported": 0,
            "missing": art_total,
            "empty_artist_folder": not album_list,
            "path": "",
        })

    result.extend(sorted(final_extras, key=lambda a: a["name"].lower()))
    _apply_collaboration_album_views(result)
    _attach_artist_image_cache_urls(result)
    result.sort(key=lambda a: a["name"].lower())

    total_artists = len(result)
    total_albums  = sum(1 for a in result for al in a.get("albums", [])
                        if not al.get("virtual_appearance"))
    total_tracks  = sum(int(al.get("track_count") or len(al.get("tracks", [])))
                        for a in result for al in a.get("albums", [])
                        if not al.get("virtual_appearance"))

    payload = {
        "ok":      True,
        "artists": result,
        "stats":   {"artists": total_artists, "albums": total_albums, "tracks": total_tracks},
        "library_version": library_cache.ts or time.time(),
    }
    return payload


def _album_db_folder_from_item_paths(rows: List[sqlite3.Row]) -> Optional[Path]:
    disc_sub_re = re.compile(r'^(?:cd|disc|disk)\s*0*(\d+)$', re.IGNORECASE)
    for row in rows:
        raw_path = _s(row["path"] if "path" in row.keys() else "").strip()
        if not raw_path:
            continue
        fpath = Path(raw_path)
        if not fpath.is_absolute():
            fpath = MUSIC_ROOT / raw_path
        parent = fpath.parent
        if disc_sub_re.match(parent.name):
            parent = parent.parent
        if _path_is_under(parent, MUSIC_ROOT):
            return parent
    return None


def _target_preview_year(value: Any) -> str:
    match = re.search(r"\d{4}", _s(value))
    return match.group(0) if match else ""


def _target_preview_artist_folder(folder_path: str, artist: str) -> str:
    source, error = _resolve_import_review_source_path(
        folder_path,
        allow_music=True,
        expected_type=None,
        require_exists=True,
    ) if _s(folder_path).strip() else (None, "source path missing")
    if source is not None and not error:
        try:
            music_resolved = MUSIC_ROOT.resolve(strict=False)
            source.relative_to(music_resolved)
            if source.parent and source.parent != MUSIC_ROOT:
                parent_name = _s(source.parent.name).strip()
                if parent_name:
                    return _safe_path_component(parent_name, "Unknown Artist")
        except Exception:
            pass
    return _safe_artist_folder_name(_normalize_albumartist(artist) or artist or "Unknown Artist")


# ── Library scan ──────────────────────────────────────────────────────────────

_SCAN_STATE_FILE     = Path(os.environ["BEETS_SCAN_STATE_FILE"]) if os.environ.get("BEETS_SCAN_STATE_FILE", "").strip() else (WEB_MANAGER_DATA_DIR / "last_scan.txt")


def _legacy_local_scan_enabled() -> bool:
    return os.environ.get("BEETS_ENABLE_LEGACY_LOCAL_SCAN", "0").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _get_last_scan() -> float:
    """Return Unix timestamp of the last successful scan, or 0."""
    try:
        return float(_SCAN_STATE_FILE.read_text().strip())
    except Exception:
        return 0.0


def _record_scan():
    try:
        _SCAN_STATE_FILE.write_text(str(time.time()))
    except Exception:
        pass
    # Rebuild here (in this background watcher thread) rather than just
    # invalidating -- otherwise the cache sits cold for up to
    # _QUICK_SCAN_INTERVAL until the next auto-scan tick, and the next
    # visitor pays for the rebuild synchronously.
    try:
        _refresh_library_cache()
    except Exception:
        _invalidate_lib_cache()


def _album_genre_value_by_id(album_id: int) -> str:
    """Return the dominant item-level genre for an album."""
    if not album_id:
        return ""
    try:
        alb = composite_workflows.get_album(int(album_id))
        if alb:
            g = _s(alb.get("genre") or alb.get("genres") or "").strip()
            if g:
                return g
        items = composite_workflows.find_all_items_by_album_id(int(album_id))
        counts: Counter = Counter()
        for it in items:
            ig = _s(it.get("genre") or it.get("genres") or "").strip()
            if ig:
                counts[ig] += 1
        if not counts:
            return ""
        return sorted(counts.keys(), key=lambda k: (-counts[k], k.lower()))[0]
    except Exception:
        return ""


def _album_genre_value(album) -> str:
    for attr in ("genre", "genres"):
        album_value = _s(getattr(album, attr, "") or "").strip()
        if album_value:
            return album_value
    return _album_genre_value_by_id(int(getattr(album, "id", 0) or 0))


def _beet_output(r) -> str:
    return _ANSI_RE.sub(
        "",
        ((getattr(r, "stdout", "") or "") + (getattr(r, "stderr", "") or "")).strip()
    )


def _require_beet_ok(r, label: str, log: list) -> str:
    out = _beet_output(r)
    if r.returncode == -9:
        raise RuntimeError(f"{label} cancelled")
    if r.returncode == 124:
        if out:
            log.append(out[:400])
        raise RuntimeError(f"{label} timed out")
    if r.returncode != 0:
        if out:
            log.append(out[:400])
        raise RuntimeError(f"{label} failed rc={r.returncode}")
    return out


def _log_beet_output_excerpt(log: list, label: str, out: str,
                             *, max_lines: int = 18, max_chars: int = 1600) -> None:
    """Append a compact subprocess output excerpt for jobs that otherwise look idle."""
    clean = _s(out).strip()
    if not clean:
        log.append(f"  {label} completed with no console output")
        return
    lines = [ln.strip() for ln in clean.splitlines() if ln.strip()]
    if not lines:
        return
    excerpt = lines[-max_lines:]
    total = 0
    log.append(f"  {label} output:")
    for line in excerpt:
        line = line[:240]
        total += len(line)
        if total > max_chars:
            log.append("    ...")
            break
        log.append(f"    {line}")


def _apply_genre_to_album(album_id: int, genre: str, log: list, env: Optional[dict] = None,
                           cancel_event=None) -> bool:
    """Write genre to every track via album_metadata_repair_v1 family."""
    try:
        res = composite_workflows.update_album_metadata(album_id, {"genre": genre})
        if not res.get("ok"):
            raise RuntimeError(res.get("error") or "genre update failed")
    except Exception as ex:
        raise RuntimeError(f"genre DB update failed: {ex}") from ex

    _invalidate_lib_cache()
    log.append(f"  ✓ Genre set: {genre}")
    return True


def _lastgenre_cmd(force: bool, query: str, log: list, env: Optional[dict] = None,
                   cancel_event=None, timeout: int = 180):
    """Run controlled album-scoped lastgenre repair via BeetsClient IPC."""
    raw_query = _s(query).strip()
    match = re.fullmatch(r"album_id:(\d+)", raw_query)
    if not match:
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="lastgenre repair requires an album_id query",
        )
    try:
        result = composite_workflows.repair_album_genre(int(match.group(1)), force=force, timeout=float(timeout))
    except Exception as ex:
        result = {"ok": False, "error": str(ex)}
    return SimpleNamespace(
        returncode=0 if result.get("ok") else 1,
        stdout=str(result.get("stdout") or result.get("output") or ""),
        stderr=str(result.get("stderr") or result.get("error") or ""),
    )


# Service behind POST /api/library/fix-genres (ARCH-001): request-free,
# returns (json_body, http_status); the route and in-process callers share it.
def start_library_fix_genres(payload_in: Dict[str, Any]) -> Tuple[Any, int]:
    """Background job: run beet lastgenre for albums missing genre, then optionally
    call OpenAI for those lastgenre still couldn't tag."""
    payload  = payload_in
    force    = bool(payload.get("force"))
    use_ai   = bool(payload.get("use_ai"))

    def _do(log, cancel_event=None):
        targets = list(lib.albums([]))
        if not force:
            targets = [a for a in targets if not _album_genre_value(a)]
        targets = sorted(
            targets,
            key=lambda a: ((_s(a.albumartist).lower()), (_s(a.album).lower()), int(a.id or 0)),
        )

        if force:
            log.append(f"[1/2] Running lastgenre force re-tag for {len(targets)} album(s)…")
        else:
            log.append(f"[1/2] Running lastgenre for {len(targets)} album(s) with no genre…")

        tagged_lastfm = failed_lastfm = 0
        for idx, album in enumerate(targets, 1):
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("cancelled")
            aid = int(album.id or 0)
            name = f"{album.albumartist or '?'} - {album.album or '?'}"
            log.append(f"  [{idx}/{len(targets)}] {name}")
            before = _album_genre_value(album)
            try:
                r = _lastgenre_cmd(force, f"album_id:{aid}", log, cancel_event=cancel_event)
                out = _require_beet_ok(r, "lastgenre", log)
                if idx == 1:
                    _log_beet_output_excerpt(log, "lastgenre", out)
            except Exception as ex:
                failed_lastfm += 1
                log.append(f"    WARN lastgenre failed for album_id:{aid}: {ex}")
                continue

            _invalidate_lib_cache()
            after = _album_genre_value_by_id(aid)
            if after and after != before:
                tagged_lastfm += 1
                log.append(f"    ✓ Genre: {after}")

        if cancel_event and cancel_event.is_set():
            raise RuntimeError("cancelled")

        if not use_ai:
            still = sum(1 for a in lib.albums([]) if not _album_genre_value(a))
            log.append(
                f"Done. Last.fm tagged {tagged_lastfm} album(s), "
                f"{failed_lastfm} failed, {still} still missing genre."
            )
            return

        api_key = _ai_api_key()
        if not api_key:
            log.append("[2/2] OPENAI_API_KEY not set — skipping AI fallback")
            return

        missing = [a for a in lib.albums([]) if not _album_genre_value(a)]
        log.append(f"[2/2] AI genre fill-in for {len(missing)} album(s)…")
        tagged = failed = 0
        for album in missing:
            if cancel_event and cancel_event.is_set():
                break
            genre = _ai_suggest_genre(
                album.albumartist or "", album.album or "", album.year, api_key, log)
            if genre:
                try:
                    _apply_genre_to_album(album.id, genre, log, cancel_event=cancel_event)
                    tagged += 1
                except Exception as ex:
                    failed += 1
                    log.append(f"  WARN album_id:{album.id}: {ex}")

        if cancel_event and cancel_event.is_set():
            raise RuntimeError("cancelled")
        if failed and not tagged:
            raise RuntimeError("AI genre writes failed")

        _invalidate_lib_cache()
        parts = [f"AI tagged {tagged} album(s)"]
        if failed:
            parts.append(f"{failed} failed/cancelled")
        log.append("Done. " + ", ".join(parts) + ".")

    if force:
        label = "Force genre re-tag"
    elif use_ai:
        label = "Fix missing genres: Last.fm + AI"
    else:
        label = "Fix missing genres: Last.fm"
    job = jobs.start_python(_do, label=label, metadata={"type": "fix-genres", "force": force, "use_ai": use_ai})
    return {"ok": True, "job_id": job.job_id}, 200


_ARTIST_ALIAS_REJECTED_FILE = Path("/config/artist_alias_rejected.json")


_artist_alias_rejected_lock = threading.Lock()


def _artist_alias_group_reject_key(mb_artistid: str, names: Iterable[Any]) -> str:
    clean_names: List[str] = []
    for value in names or []:
        if isinstance(value, dict):
            value = value.get("name", "")
        key = _artist_alias_key(_s(value))
        if key:
            clean_names.append(key)
    return f"{_s(mb_artistid).strip().lower()}::{'|'.join(sorted(set(clean_names)))}"


def _artist_alias_rejected_map() -> Dict[str, Dict[str, Any]]:
    try:
        with _artist_alias_rejected_lock:
            if not _ARTIST_ALIAS_REJECTED_FILE.exists():
                return {}
            data = json.loads(_ARTIST_ALIAS_REJECTED_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                rejected = data.get("rejected") if isinstance(data.get("rejected"), dict) else data
                return rejected if isinstance(rejected, dict) else {}
            if isinstance(data, list):
                return {str(key): {"rejected_at": 0} for key in data if str(key).strip()}
    except Exception:
        pass
    return {}


def _artist_alias_write_rejected_map(rejected: Dict[str, Dict[str, Any]]) -> None:
    try:
        with _artist_alias_rejected_lock:
            _ARTIST_ALIAS_REJECTED_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _ARTIST_ALIAS_REJECTED_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"rejected": rejected}, indent=2), encoding="utf-8")
            tmp.replace(_ARTIST_ALIAS_REJECTED_FILE)
    except Exception:
        pass


def _artist_id_alias_groups(include_rejected: bool = False) -> List[Dict[str, Any]]:
    try:
        res = composite_workflows.get_artist_alias_groups()
        alias_groups = res.get("alias_groups") or res.get("groups") or []
    except Exception:
        return []

    rejected = _artist_alias_rejected_map()
    out = []
    for rec in alias_groups:
        names = rec.get("names", [])
        if len(names) < 2:
            continue
        group = dict(rec)
        group["reject_key"] = _artist_alias_group_reject_key(group["mb_artistid"], group["names"])
        if not include_rejected and group["reject_key"] in rejected:
            continue
        out.append(group)
    return sorted(out, key=lambda g: g["canonical"].casefold())


def _resolve_artist_alias_mbid(source: str, canonical: str, mb_artistid: str,
                               log: Optional[list] = None) -> str:
    """Resolve the MB artist ID for a manual alias confirmation."""
    mb_artistid = (mb_artistid or "").strip().lower()
    if _MB_UUID_RE.match(mb_artistid):
        return mb_artistid

    names = [canonical, source]
    try:
        for name in names:
            if not name:
                continue
            albums = composite_workflows.find_all_albums_by_albumartist(name.strip())
            for alb in albums:
                ids = _split_beets_multi(alb.get("mb_albumartistids")) or _split_beets_multi(alb.get("mb_albumartistid"))
                for candidate in ids:
                    candidate = _s(candidate).strip().lower()
                    if _MB_UUID_RE.match(candidate):
                        if log is not None:
                            log.append(
                                f"Resolved MusicBrainz artist ID from existing "
                                f"artist {name!r}: {candidate}"
                            )
                        return candidate
    except Exception as ex:
        if log is not None:
            log.append(f"  WARN: existing artist ID lookup failed: {ex}")

    for name in names:
        hit = _mb_artist_search_one(name)
        candidate = _s(hit.get("id", "")).strip().lower()
        if _MB_UUID_RE.match(candidate):
            if log is not None:
                log.append(
                    f"Resolved MusicBrainz artist ID from MusicBrainz search "
                    f"for {name!r}: {candidate}"
                )
            return candidate
    return ""


def _artist_alias_values(row: sqlite3.Row) -> List[str]:
    values: List[str] = []
    for col in ("albumartist", "albumartist_credit"):
        try:
            values.append(_s(row[col]))
        except Exception:
            pass
    for col in ("albumartists", "albumartists_credit"):
        try:
            values.extend(_split_beets_multi(_s(row[col])))
        except Exception:
            pass
    return [v for v in values if _s(v).strip()]


def _artist_alias_ids(row: sqlite3.Row) -> List[str]:
    vals: List[str] = []
    for col in ("mb_albumartistids", "mb_albumartistid"):
        try:
            vals.extend(_split_beets_multi(_s(row[col])))
        except Exception:
            pass
    return [_s(v).strip().lower() for v in vals if _MB_UUID_RE.match(_s(v).strip())]


def _artist_alias_updates(canonical: str, mbid: str, *, album: bool) -> Dict[str, Any]:
    prefix = "album" if album else ""
    updates = {
        f"{prefix}artist": canonical,
        f"{prefix}artists": canonical,
        f"{prefix}artist_credit": canonical,
        f"{prefix}artists_credit": canonical,
        f"{prefix}artist_sort": canonical,
        f"{prefix}artists_sort": canonical,
    }
    if mbid:
        updates[f"mb_{prefix}artistid"] = mbid
        updates[f"mb_{prefix}artistids"] = mbid
        if not album:
            updates["artists_ids"] = mbid
    return updates


def _item_artist_matches_alias(row: sqlite3.Row, source_keys: set) -> bool:
    values: List[str] = []
    for col in ("artist", "artist_credit", "artists", "artists_credit"):
        try:
            values.append(_s(row[col]))
            values.extend(_split_beets_multi(_s(row[col])))
        except Exception:
            pass
    return any(_artist_alias_key(v) in source_keys for v in values if _s(v).strip())


def _run_normalize_artists_if_needed():
    """Normalize albumartist fields in the entire library:
    - Unicode punctuation → ASCII  (Wu‐Tang Clan → Wu-Tang Clan)
    - Strip feat./ft./featuring suffixes
    - Strip comma-listed collaborators when no '&' present
      (Wiz Khalifa, Juicy J → Wiz Khalifa)
    Runs silently as a background job when anything needs fixing.
    """
    try:
        # ARCH-007 (Wave 34): structured engine read, not raw _db() SQL --
        # this background function silently no-op'd via the outer
        # `except Exception: pass` on every run in the real two-service
        # topology before this fix (found tracing this function as the
        # "sibling" of library_normalize_artists(), per its own comment
        # above, while fixing that function's identical defect).
        aa_values = composite_workflows.list_distinct_albumartists()
        to_fix = [
            (aa, _normalize_albumartist(aa))
            for aa in aa_values
            if _normalize_albumartist(aa) != aa
        ]
        if not to_fix:
            return
        def _do(log, cancel_event=None):
            # Selection (which album rows currently hold the un-normalized
            # value) is a non-mutating structured read; the actual rename is
            # one album_metadata_repair_v1 call per affected album --
            # updates={"albumartist": new_aa} already propagates to every
            # item row of that album too (create_album_metadata_plan merges
            # album-level identity fields into each item's diff when the
            # item doesn't already set its own), so no separate
            # UPDATE items SET albumartist=... step is needed.
            affected_ids: List[int] = []
            for old_aa, new_aa in to_fix:
                try:
                    rows = composite_workflows.find_all_albums_by_albumartist(old_aa)
                except (BeetsUnavailableError, BeetsError) as ex:
                    log.append(f"  Engine unavailable looking up albums for {old_aa!r}: {ex}")
                    continue
                log.append(f"  Renamed: {old_aa!r} → {new_aa!r}")
                for row in rows:
                    aid = int(row["id"])
                    try:
                        res = composite_workflows.update_album_metadata(aid, {"albumartist": new_aa}, force_write_tags=True)
                    except (BeetsUnavailableError, BeetsError) as ex:
                        log.append(f"  Engine unavailable normalizing album_id {aid}: {ex}")
                        continue
                    if not res.get("ok"):
                        log.append(f"  Engine rejected normalize for album_id {aid}: {res.get('error') or 'unknown error'}")
                        continue
                    affected_ids.append(aid)
            for i, aid in enumerate(affected_ids, 1):
                log.append(f"[{i}/{len(affected_ids)}] Moving album_id={aid}…")
                try:
                    rel_res = composite_workflows.relocate_album(aid, mode="rename")
                    if rel_res.get("ok"):
                        log.append(f"  ✓ Relocated album {aid} to: {rel_res.get('dest_dir')}")
                except Exception as _ex:
                    log.append(f"  relocate warning: {_ex}")
            _invalidate_lib_cache()
            log.append(f"Auto-normalized {len(affected_ids)} album(s) across {len(to_fix)} artist name(s).")
        jobs.start_python(_do, label="Auto-normalize artist names")
    except Exception:
        pass


def _scan_scope_label(scan_path: Path) -> str:
    try:
        resolved = scan_path.resolve(strict=False)
        if _path_under(resolved, MUSIC_ROOT):
            return "Music Library"
        if _path_under(resolved, DOWNLOADS_ROOT):
            return "Downloads"
    except Exception:
        pass
    return "Custom path"


def _album_source_folder(aid: int) -> str:
    if not aid:
        return ""
    try:
        items = composite_workflows.find_all_items_by_album_id(int(aid))
    except Exception:
        return ""
    dirs = [os.path.dirname(_s(it.get("path"))) for it in items if _s(it.get("path"))]
    dirs = [d for d in dirs if d]
    if not dirs:
        return ""
    return Counter(dirs).most_common(1)[0][0]


# ── Same Release Group ID cluster resolution ───────────────────────────────
# "Review details" used to just redirect to the Library page with no way to
# actually resolve a cluster, so the warning was permanent. These endpoints
# give the card real actions: merge, keep-separate (persisted), assign a
# specific representative release, relink to a different release/group, or
# route a partial import into the existing MBID repair machinery.

def _rgid_group_albums(rgid: str) -> List[Any]:
    rgid = _s(rgid).strip().lower()
    if not rgid:
        return []
    try:
        res = composite_workflows.get_rgid_group_detail(rgid)
        if res.get("ok"):
            return res.get("albums") or []
    except Exception:
        pass
    return []


def _artist_folder_db_counts() -> Dict[str, Dict[str, int]]:
    try:
        return beets_adapter.get_artist_counts()
    except Exception:
        return {}


def _scan_artist_folder_groups(root: str, *, use_musicbrainz: bool = False,
                               only_keys: Optional[set] = None) -> List[Dict[str, Any]]:
    root_path = Path(root)
    db_counts = _artist_folder_db_counts()
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    wanted = set(only_keys or [])
    # ARCH-020: folder listing and audio-file counts are engine-side facts --
    # Web Manager has no local media mount in the supported two-service
    # deployment and must never walk MUSIC_ROOT itself.
    entries = sorted(
        composite_workflows.get_artist_folder_inventory(str(root_path)),
        key=lambda f: _s(f.get("name")).casefold(),
    )
    for entry in entries:
        name = _s(entry.get("name"))
        if not name or name.startswith("."):
            continue
        key = _artist_folder_key(name)
        if not key:
            continue
        if wanted and key not in wanted:
            continue
        dbc = db_counts.get(name, {})
        grouped[key].append({
            "name": name,
            "path": _s(entry.get("path")) or str(root_path / name),
            "audio_files": int(entry.get("audio_files") or 0),
            "subfolders": int(entry.get("subfolders") or 0),
            "db_albums": dbc.get("albums", 0),
            "db_tracks": dbc.get("tracks", 0),
        })

    out = []
    for key, entries in grouped.items():
        mb_canonical = _mb_canonical_for_artist_entries(entries, key) if use_musicbrainz else {}
        canonical_name_from_mb = _s(mb_canonical.get("name", "")).strip()
        needs_mb_rename = bool(
            canonical_name_from_mb
            and not any(e["name"] == canonical_name_from_mb for e in entries)
            and any(_artist_folder_key(e["name"]) == _artist_folder_key(canonical_name_from_mb) for e in entries)
        )
        if len(entries) < 2 and not needs_mb_rename:
            continue
        def _looks_bad_upper(name: str) -> bool:
            letters = re.sub(r"[^A-Za-z]+", "", name)
            return len(letters) > 4 and letters.isupper()

        has_preferred_case = any(not _looks_bad_upper(e["name"]) for e in entries)

        def _score(e):
            return (
                1 if has_preferred_case and _looks_bad_upper(e["name"]) else 0,
                -int(e["db_tracks"] or 0),
                -int(e["audio_files"] or 0),
                len(e["name"]),
                e["name"].casefold(),
            )

        canonical = sorted(entries, key=_score)[0]
        if canonical_name_from_mb:
            existing_mb = next((e for e in entries if e["name"] == canonical_name_from_mb), None)
            if existing_mb:
                canonical = existing_mb
            elif needs_mb_rename:
                canonical = {
                    "name": canonical_name_from_mb,
                    "path": str(root_path / canonical_name_from_mb),
                    "audio_files": 0,
                    "subfolders": 0,
                    "db_albums": 0,
                    "db_tracks": 0,
                    "musicbrainz_synthetic": True,
                }
        sources = [e for e in entries if e["path"] != canonical["path"]]
        out.append({
            "key": key,
            "canonical": canonical,
            "sources": sources,
            "variants": entries,
            "musicbrainz": {
                "id": mb_canonical.get("id", ""),
                "name": canonical_name_from_mb,
                "score": mb_canonical.get("best_score", 0),
                "matched_entries": mb_canonical.get("entries", []),
                "disambiguation": mb_canonical.get("disambiguation", ""),
            } if mb_canonical else {},
            "rename_to_musicbrainz": bool(canonical_name_from_mb),
            "source_audio_files": sum(int(e["audio_files"] or 0) for e in sources),
            "source_folders": len(sources),
        })
    return sorted(out, key=lambda g: g["canonical"]["name"].casefold())


def _try_resolve_leaked_path(current: Path) -> Optional[Path]:
    """Strip template-token-only directory components from a path that has leaked tokens.

    Handles the common '$disc_subfolder' case: removes any path component that is
    entirely an unresolved template token, returning the shortened path.
    Returns None if no stripping is possible or the result equals the input.
    """
    parts = current.parts
    if not parts:
        return None
    filename = parts[-1]
    dir_parts = list(parts[:-1])
    cleaned: list = []
    stripped_any = False
    for part in dir_parts:
        if not part:
            continue
        remainder = _UNRESOLVED_TEMPLATE_TOKEN_RE.sub("", part).strip()
        if remainder:
            cleaned.append(part)
        else:
            stripped_any = True
    if not stripped_any or not cleaned:
        return None
    try:
        candidate = Path(cleaned[0]).joinpath(*cleaned[1:]) / filename
    except Exception:
        return None
    if not candidate.is_absolute():
        return None
    return candidate if candidate != current else None


def _scan_leaked_db_paths(progress: Optional[Any] = None,
                          cancel_event: Optional[Any] = None,
                          scan_meta: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Scan the beets DB for item rows whose paths contain unresolved template tokens.

    Returns one dict per affected row with:
        item_id, album_id, db_path (as stored), abs_path (absolute),
        resolved_path (proposed fix), file_exists_at_db_path,
        file_exists_at_resolved, safe (bool), skip_reason (str).
    """
    results: List[Dict[str, Any]] = []
    try:
        rows = beets_adapter.list_item_paths(details=True)
    except Exception as ex:
        return [{"error": str(ex)}]

    _mroot = str(MUSIC_ROOT)
    total_rows = len(rows)
    if scan_meta is not None:
        scan_meta["total_db_rows_scanned"] = total_rows
    if progress:
        progress({
            "category": "Cleanup",
            "current_task": "Scanning DB rows for leaked path-template tokens",
            "scanned_count": 0,
            "total_count": total_rows,
            "affected_count": 0,
            "safe_count": 0,
            "needs_review_count": 0,
        })
    safe_count = 0
    needs_review_count = 0
    source_missing_count = 0
    target_exists_count = 0
    for idx, row in enumerate(rows, start=1):
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("cancelled")
        raw = row["path"]
        if isinstance(raw, bytes):
            try:
                raw_str = raw.decode("utf-8")
            except Exception:
                raw_str = raw.decode("latin-1", errors="replace")
        else:
            raw_str = _s(raw)
        if not raw_str:
            if progress and (idx == 1 or idx % 500 == 0 or idx == total_rows):
                progress({
                    "category": "Cleanup",
                    "current_task": "Scanning DB rows for leaked path-template tokens",
                    "scanned_count": idx,
                    "total_count": total_rows,
                    "remaining_count": max(0, total_rows - idx),
                    "affected_count": len(results),
                    "safe_count": safe_count,
                    "needs_review_count": needs_review_count,
                    "source_missing_count": source_missing_count,
                    "target_exists_count": target_exists_count,
                })
            continue
        if not _UNRESOLVED_TEMPLATE_TOKEN_RE.search(raw_str):
            if progress and (idx == 1 or idx % 500 == 0 or idx == total_rows):
                progress({
                    "category": "Cleanup",
                    "current_task": "Scanning DB rows for leaked path-template tokens",
                    "current_item": f"item {int(row['id'] or 0)}",
                    "scanned_count": idx,
                    "total_count": total_rows,
                    "remaining_count": max(0, total_rows - idx),
                    "affected_count": len(results),
                    "safe_count": safe_count,
                    "needs_review_count": needs_review_count,
                    "source_missing_count": source_missing_count,
                    "target_exists_count": target_exists_count,
                })
            continue

        # Resolve to absolute path
        if raw_str.startswith("/"):
            abs_path = Path(raw_str)
        else:
            abs_path = MUSIC_ROOT / raw_str

        try:
            file_at_db = abs_path.exists() and abs_path.is_file()
        except Exception:
            file_at_db = False

        resolved: Optional[Path] = _try_resolve_leaked_path(abs_path)
        file_at_resolved = False
        safe = False
        skip_reason = ""

        if file_at_db:
            # File literally exists at path with template token in it (e.g. dir named "$disc_subfolder")
            skip_reason = "File exists at leaked path — manual review needed"
        elif resolved is None:
            skip_reason = "Cannot determine target (complex or nested tokens)"
        else:
            try:
                file_at_resolved = resolved.exists() and resolved.is_file()
            except Exception:
                file_at_resolved = False
            if file_at_resolved:
                safe = True
            else:
                skip_reason = "File not found at either path"

        if safe:
            safe_count += 1
        else:
            needs_review_count += 1
        if file_at_resolved:
            target_exists_count += 1
        if not file_at_db and not file_at_resolved:
            source_missing_count += 1

        rec = {
            "item_id": int(row["id"] or 0),
            "album_id": int(row["album_id"] or 0),
            "db_path": raw_str,
            "abs_path": str(abs_path),
            "resolved_path": str(resolved) if resolved else None,
            "file_exists_at_db_path": file_at_db,
            "file_exists_at_resolved": file_at_resolved,
            "safe": safe,
            "skip_reason": skip_reason,
        }
        results.append(rec)
        if progress:
            progress({
                "category": "Cleanup",
                "current_task": "Scanning DB rows for leaked path-template tokens",
                "current_item": f"item {rec['item_id']}",
                "current_path": raw_str,
                "scanned_count": idx,
                "total_count": total_rows,
                "remaining_count": max(0, total_rows - idx),
                "affected_count": len(results),
                "safe_count": safe_count,
                "needs_review_count": needs_review_count,
                "source_missing_count": source_missing_count,
                "target_exists_count": target_exists_count,
                "skipped_count": needs_review_count,
                "current_result": (
                    "Safe repair candidate found" if safe
                    else "Leaked DB path needs review"
                ),
            })
    if progress:
        progress({
            "category": "Cleanup",
            "current_task": "Leaked DB Paths scan complete",
            "current_item": None,
            "current_path": None,
            "scanned_count": total_rows,
            "total_count": total_rows,
            "remaining_count": 0,
            "affected_count": len(results),
            "safe_count": safe_count,
            "needs_review_count": needs_review_count,
            "source_missing_count": source_missing_count,
            "target_exists_count": target_exists_count,
            "skipped_count": needs_review_count,
            "current_result": (
                f"{len(results)} leaked row(s): "
                f"{safe_count} safe, {needs_review_count} need review"
            ),
            "final_summary": _leaked_db_paths_summary(results, total_scanned=total_rows),
        })
    return results


def _folder_placeholder_summary(rows: List[Dict[str, Any]],
                                *,
                                total_scanned: Optional[int] = None) -> Dict[str, Any]:
    safe = [r for r in rows if r.get("safe")]
    unsafe = [r for r in rows if not r.get("safe")]
    return {
        "total_folders_scanned": int(total_scanned if total_scanned is not None else len(rows)),
        "placeholder_folders_found": len(rows),
        "safe_to_fix": len(safe),
        "needs_review": len(unsafe),
        "target_exists": sum(1 for r in rows if r.get("target_exists")),
        "db_tracked": sum(1 for r in rows if int(r.get("db_item_count") or 0) > 0),
        "empty_folders": sum(1 for r in rows if r.get("is_empty")),
        "skipped_unsafe": len(unsafe),
    }


def _scan_folder_name_placeholders(progress: Optional[Any] = None,
                                   cancel_event: Optional[Any] = None,
                                   scan_meta: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Scan MUSIC_ROOT album-level folders for unresolved template placeholders in their names.

    Detects patterns like {Album MbId}, {Album Mbid}, $disc_subfolder in folder names.
    Returns one dict per found folder with enough info for a safe preview/review workflow.
    Does NOT modify anything.
    """
    root = Path(MUSIC_ROOT)
    if not root.exists():
        return []

    # Build folder -> DB info map from items table
    folder_db: Dict[str, Dict[str, Any]] = {}
    try:
        rows = beets_adapter.list_item_paths(details=True)
        for row in rows:
            p = _s(row.get("path"))
            d = os.path.dirname(p) if p else ""
            if not d:
                continue
            if d not in folder_db:
                folder_db[d] = {"album_ids": set(), "item_count": 0}
            if row.get("album_id"):
                folder_db[d]["album_ids"].add(int(row["album_id"]))
            folder_db[d]["item_count"] += 1
    except Exception:
        pass

    results: List[Dict[str, Any]] = []
    seen: set = set()

    try:
        artist_dirs = [
            artist_dir for artist_dir in sorted(root.iterdir())
            if artist_dir.is_dir() and not artist_dir.name.startswith(".")
        ]
    except PermissionError:
        artist_dirs = []

    album_dirs: List[Path] = []
    skipped_count = 0
    for artist_dir in artist_dirs:
        try:
            album_dirs.extend([
                album_dir for album_dir in sorted(artist_dir.iterdir())
                if album_dir.is_dir() and not album_dir.name.startswith(".")
            ])
        except PermissionError:
            skipped_count += 1
            continue

    total_album_dirs = len(album_dirs)
    if scan_meta is not None:
        scan_meta["total_folders_scanned"] = total_album_dirs
    if progress:
        progress({
            "category": "Cleanup",
            "current_task": "Scanning folder names for unresolved placeholders",
            "scan_path": str(root),
            "scanned_count": 0,
            "total_count": total_album_dirs,
            "placeholder_count": 0,
            "safe_count": 0,
            "needs_review_count": 0,
            "skipped_count": skipped_count,
        })

    safe_count = 0
    needs_review_count = 0
    target_exists_count = 0
    db_tracked_count = 0
    empty_folder_count = 0

    for idx, album_dir in enumerate(album_dirs, start=1):
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("cancelled")
        name = album_dir.name
        folder_str = str(album_dir)

        if progress and (idx == 1 or idx % 50 == 0 or idx == total_album_dirs):
            progress({
                "category": "Cleanup",
                "current_task": "Scanning folder names for unresolved placeholders",
                "current_path": folder_str,
                "scanned_count": idx,
                "total_count": total_album_dirs,
                "remaining_count": max(0, total_album_dirs - idx),
                "placeholder_count": len(results),
                "safe_count": safe_count,
                "needs_review_count": needs_review_count,
                "target_exists_count": target_exists_count,
                "db_tracked_count": db_tracked_count,
                "empty_folder_count": empty_folder_count,
                "skipped_count": skipped_count,
            })

        # Check for any unresolved placeholder in folder name
        has_literal = bool(_LITERAL_PLACEHOLDER_RE.search(name))
        has_token = bool(_UNRESOLVED_TEMPLATE_TOKEN_RE.search(name))
        if not (has_literal or has_token):
            continue

        key = folder_str.lower()
        if key in seen:
            continue
        seen.add(key)

        if has_literal:
            placeholder_type = "literal_placeholder"
            placeholder_desc = "{Album MbId} or {Track ArtistMbId} text was never replaced"
        else:
            placeholder_type = "template_token"
            placeholder_desc = "$variable or %func{} was never expanded"

        # Propose clean name by stripping placeholders
        clean_name = _LITERAL_PLACEHOLDER_RE.sub("", name)
        clean_name = _UNRESOLVED_TEMPLATE_TOKEN_RE.sub("", clean_name)
        clean_name = re.sub(r"\s+", " ", clean_name).strip().strip(" -_")

        proposed_folder = str(album_dir.parent / clean_name) if clean_name and clean_name != name else None
        target_exists = bool(proposed_folder and Path(proposed_folder).exists())

        # Inspect contents
        try:
            contents = list(album_dir.iterdir())
            file_count = sum(1 for f in contents if f.is_file())
            audio_count = sum(1 for f in contents if f.is_file() and f.suffix.lower() in AUDIO_EXT)
            is_empty = len(contents) == 0
        except Exception:
            file_count = -1
            audio_count = -1
            is_empty = False

        db_info = folder_db.get(folder_str, {})
        db_album_ids = sorted(db_info.get("album_ids", set()))
        db_item_count = db_info.get("item_count", 0)

        # Safety classification
        skip_reason = ""
        safe = False

        if not clean_name:
            skip_reason = "Placeholder removal results in an empty folder name"
        elif target_exists:
            skip_reason = f"Target already exists: {proposed_folder}"
        elif db_item_count > 0:
            # DB-tracked files require beet move, not just a folder rename
            skip_reason = (
                f"{db_item_count} DB-tracked item(s) — rename requires beet move, "
                "not a simple folder rename. Run beet move after fixing DB paths."
            )
        elif audio_count > 0 and db_item_count == 0:
            # Untracked audio — folder rename is possible
            safe = True
        elif is_empty:
            safe = True
        else:
            # Only non-audio files, no DB items
            safe = True

        if safe:
            safe_count += 1
        else:
            needs_review_count += 1
        if target_exists:
            target_exists_count += 1
        if db_item_count > 0:
            db_tracked_count += 1
        if is_empty:
            empty_folder_count += 1

        results.append({
            "folder": folder_str,
            "artist": album_dir.parent.name,
            "name": name,
            "clean_name": clean_name or None,
            "proposed_folder": proposed_folder,
            "placeholder_type": placeholder_type,
            "placeholder_desc": placeholder_desc,
            "is_empty": is_empty,
            "file_count": file_count,
            "audio_count": audio_count,
            "db_album_ids": db_album_ids,
            "db_item_count": db_item_count,
            "target_exists": target_exists,
            "safe": safe,
            "skip_reason": skip_reason,
        })
        if progress:
            progress({
                "category": "Cleanup",
                "current_task": "Scanning folder names for unresolved placeholders",
                "current_path": folder_str,
                "scanned_count": idx,
                "total_count": total_album_dirs,
                "remaining_count": max(0, total_album_dirs - idx),
                "placeholder_count": len(results),
                "safe_count": safe_count,
                "needs_review_count": needs_review_count,
                "target_exists_count": target_exists_count,
                "db_tracked_count": db_tracked_count,
                "empty_folder_count": empty_folder_count,
                "skipped_count": skipped_count + needs_review_count,
                "current_result": (
                    "Safe placeholder folder candidate found" if safe
                    else "Placeholder folder needs review"
                ),
            })

    sorted_results = sorted(results, key=lambda r: (r["artist"].casefold(), r["name"].casefold()))
    if progress:
        progress({
            "category": "Cleanup",
            "current_task": "Folder Names scan complete",
            "current_item": None,
            "current_path": None,
            "scanned_count": total_album_dirs,
            "total_count": total_album_dirs,
            "remaining_count": 0,
            "placeholder_count": len(sorted_results),
            "safe_count": safe_count,
            "needs_review_count": needs_review_count,
            "target_exists_count": target_exists_count,
            "db_tracked_count": db_tracked_count,
            "empty_folder_count": empty_folder_count,
            "skipped_count": skipped_count + needs_review_count,
            "current_result": (
                f"{len(sorted_results)} placeholder folder(s): "
                f"{safe_count} safe, {needs_review_count} need review"
            ),
            "final_summary": _folder_placeholder_summary(sorted_results, total_scanned=total_album_dirs),
        })
    return sorted_results


def _root_folder_repair_scan(root: Optional[Path] = None) -> Dict[str, Any]:
    """Find MUSIC_ROOT top-level entries that are actually album/singleton
    folders sitting one level too shallow, e.g. "/data/media/music/Aaliyah
    (2001)" instead of "/data/media/music/Aaliyah (mbid)/Aaliyah (2001)
    {rgid}". A real artist folder in this library always has at least one
    album subfolder underneath it; anything with zero subfolders directly
    under root is either leftover empty junk or content that never got its
    artist-folder wrapper (both invisible to the normal artist/album scans,
    which only look one level down from root for albums)."""
    scan_root = (root or MUSIC_ROOT).resolve(strict=False)
    if not scan_root.exists() or not scan_root.is_dir():
        raise RuntimeError(f"Music library root is not accessible: {scan_root}")

    try:
        top_dirs = [
            p for p in sorted(scan_root.iterdir(), key=lambda x: x.name.casefold())
            if p.is_dir() and not p.name.startswith(".")
        ]
    except Exception as exc:
        raise RuntimeError(f"Could not list {scan_root}: {exc}")

    # Item id -> its top-level folder name, for items whose path is exactly
    # "<folder>/<file>" directly under MUSIC_ROOT (no artist-folder level).
    shallow_by_folder: Dict[str, List[int]] = {}
    try:
        for item in lib.items([]):
            path = _s(getattr(item, "path", "")).strip().replace("\\", "/")
            if not path or path.startswith("/"):
                continue
            parts = path.split("/")
            if len(parts) == 2:
                shallow_by_folder.setdefault(parts[0], []).append(int(item.id))
    except Exception:
        pass

    empty_folders: List[str] = []
    shallow_folders: List[Dict[str, Any]] = []
    orphaned_folders: List[Dict[str, Any]] = []

    for top in top_dirs:
        try:
            children = list(top.iterdir())
        except Exception:
            continue
        subdirs = [c for c in children if c.is_dir() and not c.name.startswith(".")]
        if subdirs:
            continue  # real artist folder; its albums live one level down

        item_ids = shallow_by_folder.get(top.name) or []
        if item_ids:
            shallow_folders.append({
                "folder": str(top),
                "name": top.name,
                "item_ids": sorted(item_ids),
                "item_count": len(item_ids),
            })
            continue

        files = [c for c in children if c.is_file()]
        if not files:
            empty_folders.append(str(top))
            continue

        has_audio = any(c.suffix.lower() in AUDIO_EXT for c in files)
        orphaned_folders.append({
            "folder": str(top),
            "name": top.name,
            "file_count": len(files),
            "has_audio": has_audio,
        })

    return {
        "ok": True,
        "root": str(scan_root),
        "empty_folders": empty_folders,
        "shallow_folders": shallow_folders,
        "orphaned_folders": orphaned_folders,
        "summary": {
            "empty_count": len(empty_folders),
            "shallow_folder_count": len(shallow_folders),
            "shallow_item_count": sum(f["item_count"] for f in shallow_folders),
            "orphaned_count": len(orphaned_folders),
        },
    }


def _root_folder_repair_apply_safe(log: List[str], cancel_event: Optional[Any] = None,
                                   progress: Optional[Any] = None) -> Dict[str, Any]:
    """Remove empty root-level junk folders and re-home shallow-path items
    (already-tracked Beets items missing their artist-folder wrapper) to the
    path the current template says they belong at, via the same per-item
    `beet write` + `beet move` used by /api/items/<iid>/retag. Folders with
    untracked audio are left alone and queued to Import Review instead of
    being touched, matching this app's "queue uncertain matches, don't
    guess" rule for anything under /data/media/music."""
    scan = _root_folder_repair_scan()
    summary = {
        "empty_folders_removed": 0,
        "items_moved": 0,
        "items_failed": 0,
        "orphaned_queued": 0,
        "errors": 0,
    }

    for folder_str in scan["empty_folders"]:
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("cancelled")
        folder = Path(folder_str)
        removed = _album_cleanup_remove_empty_tree(folder, log)
        if removed:
            summary["empty_folders_removed"] += 1

    shallow_folders = scan["shallow_folders"]
    total_items = sum(f["item_count"] for f in shallow_folders)
    done = 0

    for entry in shallow_folders:
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("cancelled")
        for iid in entry["item_ids"]:
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("cancelled")
            done += 1
            log.append(f"[{done}/{total_items}] Re-homing item {iid} from {entry['name']}")
            if progress:
                progress({
                    "category": "Cleanup",
                    "current_task": "Re-homing shallow root-level items",
                    "current_item": entry["name"],
                    "scanned_count": done,
                    "total_count": total_items,
                })
            try:
                item = lib.get_item(iid)
                has_mb = bool(item) and bool(
                    getattr(item, "mb_albumid", "") or getattr(item, "mb_trackid", ""))
            except Exception:
                has_mb = False
            aid = getattr(item, "album_id", None) if item else None
            if aid:
                # Wave 26 correction (section 18 of the review brief): every
                # required-or-informative child call's result is now
                # actually inspected. Relocation is the operation's real
                # purpose (see docstring) and remains fatal-on-failure,
                # unchanged. mbsync/tag-rewrite are best-effort refreshes
                # that must not block a successful relocation -- but a
                # silently-ignored failure there is exactly the "call(),
                # ignore response, continue" pattern the brief prohibits,
                # so both are now logged and counted, never discarded.
                if has_mb:
                    mbid = getattr(item, "mb_albumid", "")
                    plan_res = composite_workflows.plan_album_mb_track_repair({"album_id": int(aid), "mb_albumid": mbid})
                    if not plan_res.get("ok"):
                        summary["errors"] += 1
                        log.append(f"  WARN: mbsync plan failed for album_id {aid}: {plan_res.get('error')}")
                    else:
                        track_res = composite_workflows.apply_album_mb_track_repair(plan_res.get("operation_id"), write_tags=True)
                        if not track_res.get("ok"):
                            summary["errors"] += 1
                            log.append(f"  WARN: mbsync apply failed for album_id {aid}: {track_res.get('error')}")
                meta_res = composite_workflows.update_album_metadata(int(aid), {}, force_write_tags=True)
                if not meta_res.get("ok"):
                    summary["errors"] += 1
                    log.append(f"  WARN: tag rewrite failed for album_id {aid}: {meta_res.get('error')}")
                rel_res = composite_workflows.relocate_album(int(aid), mode="move")
                if not rel_res.get("ok"):
                    summary["items_failed"] += 1
                    summary["errors"] += 1
                    log.append(f"  ERROR: relocate_album failed for album_id {aid}: {rel_res.get('error')}")
                    continue
                summary["items_moved"] += 1

        # The move above should have emptied this folder; clean it up if so.
        # Wave 26 correction (sections 16-17): this used to check emptiness
        # via a LOCAL folder.iterdir() call and then delete via the generic
        # composite_workflows.delete_file() passthrough -- both the precondition
        # check and the mutation itself belong on the engine side, which
        # actually owns this mount; the Web Manager has no reliable local
        # view of it in the two-service topology. Reuses
        # _album_cleanup_remove_empty_tree(), the same real
        # folder_cleanup_v1 Plan/Apply composition already used a few lines
        # above for the "empty_folders" scan category -- the engine itself
        # verifies containment, symlink-safety, directory type, actual
        # emptiness, and DB-reference-freedom immediately before removing
        # anything, rather than trusting a stale local stat.
        folder = Path(entry["folder"])
        if _album_cleanup_remove_empty_tree(folder, log):
            summary["empty_folders_removed"] += 1

    for entry in scan["orphaned_folders"]:
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("cancelled")
        queued = _queue_folder_for_manual_review(
            entry["folder"], None,
            "Root-level folder with untracked audio files, missing its artist-folder level",
            log=log,
        )
        if queued:
            summary["orphaned_queued"] += 1

    if summary["items_moved"] or summary["empty_folders_removed"]:
        _invalidate_lib_cache()

    report = {
        "ok": True,
        "root": scan["root"],
        "summary": summary,
        "final_summary": summary,
        "scan_summary": scan["summary"],
    }
    if progress:
        progress({
            "category": "Cleanup",
            "current_task": "Root folder repair complete",
            "current_item": None,
            "scanned_count": total_items,
            "total_count": total_items,
            "final_summary": summary,
        })
    return report


def _album_cleanup_duplicate_file_choice(candidate: Path, existing: Path,
                                         candidate_info: Optional[Dict[str, Any]] = None,
                                         existing_info: Optional[Dict[str, Any]] = None) -> str:
    candidate_quality = _album_cleanup_quality_tuple(candidate, candidate_info)
    existing_quality = _album_cleanup_quality_tuple(existing, existing_info)
    if candidate_quality > existing_quality:
        return "candidate"
    return "existing"


def _root_folder_repair_save_report(report: Dict[str, Any], log: Optional[List[str]] = None) -> None:
    payload = dict(report)
    payload["updated_at"] = time.time()
    try:
        ROOT_FOLDER_REPAIR_LAST_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = ROOT_FOLDER_REPAIR_LAST_FILE.with_suffix(f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
        tmp.replace(ROOT_FOLDER_REPAIR_LAST_FILE)
    except Exception as exc:
        if log is not None:
            log.append(f"[root-folder-repair] WARN: could not save report: {exc}")


def _merge_artist_dir_contents(src: Path, dst: Path, *, dry_run: bool,
                               log: List[str], moves: List[tuple],
                               verbose_files: bool = True,
                               stats: Optional[Dict[str, int]] = None):
    """Scan and compute artist directory merge moves without local direct filesystem mutations.

    All real mutations are delegated to the Beets engine via
    composite_workflows.plan_artist_folder_reconcile and apply_artist_folder_reconcile (SEC-002 Wave 21).
    """
    stats = stats if stats is not None else {}
    for key in ("files_moved", "duplicate_files_removed", "artwork_collisions_resolved", "filename_conflicts_preserved", "folders_removed"):
        stats.setdefault(key, 0)

    def _v(message: str) -> None:
        if verbose_files:
            log.append(message)

    if not src.exists() or not src.is_dir():
        return

    for child in sorted(src.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())):
        target = dst / child.name
        if child.is_dir():
            _merge_artist_dir_contents(child, target, dry_run=dry_run,
                                       log=log, moves=moves,
                                       verbose_files=verbose_files,
                                       stats=stats)
            _v(f"  {'Would remove' if dry_run else 'Remove'} empty folder: {child}")
            stats["folders_removed"] += 1
            continue

        if not child.is_file():
            continue

        final = target
        if target.exists():
            source_info = _album_cleanup_file_info(child)
            target_info = _album_cleanup_file_info(target)
            if _album_cleanup_verified_same_file(source_info, target_info):
                _v(f"  {'Would remove' if dry_run else 'Remove'} duplicate file already present at target: {child}")
                stats["duplicate_files_removed"] += 1
                continue

            if child.suffix.lower() in _ART_EXTS and target.suffix.lower() in _ART_EXTS:
                stats["artwork_collisions_resolved"] += 1
                choice = _album_cleanup_duplicate_file_choice(child, target, source_info, target_info)
                if choice == "candidate":
                    _v(f"  {'Would replace' if dry_run else 'Replace'} lower-quality artwork: {target.name}")
                    moves.append((child, target))
                else:
                    _v(f"  {'Would remove' if dry_run else 'Remove'} lower-quality duplicate artwork: {child}")
                continue

            final = _unique_dest(target)
            stats["filename_conflicts_preserved"] += 1
            _v(f"  Collision: {target.name} exists; preserving different file as {final.name}")

        moves.append((child, final))
        _v(f"  {'Would move' if dry_run else 'Move'}: {child} -> {final}")
        stats["files_moved"] += 1


def _artist_folder_repair_root(raw: Any) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve and validate the artist-folder-repair `root` request value.

    The UI labels this field "Library root" and defaults it to MUSIC_ROOT;
    there is no supported concept of scanning an individual artist or album
    folder as a repair root. Accepting an arbitrary descendant would let
    _scan_artist_folder_groups() misinterpret album folders as "duplicate
    artist folders" and destructively merge two different albums together
    (and rewrite the Beets DB's albumartist column with the album folder's
    name). `root` must therefore resolve to exactly the configured music
    library root -- never a descendant, and never anything else. A failure
    to resolve the trusted root itself fails closed rather than silently
    falling back to an unresolved comparison basis.
    """
    text = _s(raw).strip()
    if not text:
        return None, "root is required"
    error = _import_review_path_text_error(text, allow_relative=False)
    if error:
        return None, error
    candidate = Path(text)
    try:
        music_root = MUSIC_ROOT.resolve(strict=False)
    except Exception:
        return None, "Music library root is not available"
    if candidate != music_root:
        return None, "root must be the configured music library"
    # ARCH-020: no local existence/is_dir() check here -- Web Manager has no
    # local media mount in the supported two-service deployment. Whether the
    # root actually exists is an engine-side fact; the engine's own
    # candidate-discovery and Plan endpoints fail closed with a clear error
    # when it does not, and callers already surface that error to the user.
    try:
        resolved = candidate.resolve(strict=False)
    except Exception:
        return None, "Invalid root path"
    if resolved != music_root:
        return None, "root must be the configured music library"
    return resolved, None


def _apply_artist_folder_groups(root: str, keys: Optional[List[str]],
                                dry_run: bool, log: List[str],
                                use_musicbrainz: bool = True) -> Dict[str, int]:
    root_path, root_error = _artist_folder_repair_root(root)
    if root_path is None:
        log.append(f"Refusing to operate: {root_error}")
        return {"groups": 0, "folders": 0, "files": 0, "db_paths": 0, "db_artists": 0, "db_tags": 0}
    wanted = set(keys or [])
    groups = _scan_artist_folder_groups(
        str(root_path),
        use_musicbrainz=use_musicbrainz,
        only_keys=wanted if wanted else None,
    )
    if wanted:
        groups = [g for g in groups if g["key"] in wanted]

    summary = {"groups": len(groups), "folders": 0, "files": 0, "db_paths": 0, "db_artists": 0, "db_tags": 0}
    if not groups:
        log.append("No duplicate artist folders found.")
        return summary

    all_moves: List[tuple] = []
    reconcile_candidates: List[Dict[str, Any]] = []
    for group in groups:
        canonical = Path(group["canonical"]["path"])
        canonical_name = group["canonical"]["name"]
        mb_artistid = _s((group.get("musicbrainz") or {}).get("id", "")).strip()
        if not _path_under(canonical, root_path):
            log.append(f"Skipping unsafe canonical path: {canonical}")
            continue
        log.append(f"\nArtist: {canonical_name}")
        if mb_artistid:
            log.append(f"  MusicBrainz canonical: {canonical_name} [{mb_artistid}]")
        for source in group["sources"]:
            src = Path(source["path"])
            if not _path_under(src, root_path) or src.parent != root_path:
                log.append(f"  Skipping unsafe source path: {src}")
                continue
            # NOTE (ARCH-020 investigation): group["musicbrainz"]["id"] here
            # comes from a MusicBrainz TEXT SEARCH on the folder NAME
            # (_mb_canonical_for_artist_entries -> _mb_artist_search_one),
            # not from per-album Beets DB mb_albumartistid evidence like the
            # engine's own CASE A/C/D identity authority uses -- two
            # different real artists with similar names can produce a
            # matching mb_artistid here. This fingerprint check is therefore
            # NOT redundant with the engine's DB-derived identity re-check
            # and must run unconditionally, regardless of whether mb_artistid
            # is set -- do not bypass it (see
            # ArtistFolderMergeIdentityTests.test_group_with_musicbrainz_artist_id_still_requires_fingerprint_confirmation).
            fp_result = _artist_folder_fingerprint_confirms(src, canonical_name)
            if fp_result is not True:
                reason = (
                    "AcoustID fingerprint of sampled tracks resolved to a different artist"
                    if fp_result is False
                    else "no AcoustID fingerprint evidence was available to confirm this match"
                )
                log.append(
                    f"  Skipping {src.name!r}: folder name matches {canonical.name!r} but {reason}"
                )
                continue
            log.append(f"  Merge folder: {src.name} -> {canonical.name}")
            _merge_artist_dir_contents(src, canonical, dry_run=dry_run, log=log, moves=all_moves)
            summary["folders"] += 1
            reconcile_candidates.append({
                "source_path": str(src),
                "target_path": str(canonical),
                # Evidence only, not authority -- the engine independently
                # re-derives each folder's established Artist ID(s) from
                # Beets DB state before treating any merge as eligible (SEC-002
                # Wave 21 final review, findings #4-#6). fingerprint_confirmed
                # is safe to assert here because this loop iteration only
                # reaches this point when fp_result was already checked True
                # a few lines above.
                "source_mbid": mb_artistid,
                "target_mbid": mb_artistid,
                "fingerprint_confirmed": True,
            })

    summary["files"] = len(all_moves)
    if dry_run:
        log.append(f"\nDry run: {summary['groups']} artist group(s), "
                   f"{summary['folders']} folder(s), {summary['files']} file move(s).")
        return summary

    if not reconcile_candidates:
        log.append("No duplicate artist folders passed validation.")
        return summary

    # Apply phase: delegate all real mutations to Beets Engine transaction boundary
    op_payload = {
        "root": str(root_path),
        "mode": "scan_merge",
        "selected_keys": list(wanted) if wanted else [],
        "candidates": reconcile_candidates,
    }
    # SEC-002 Wave 21 final review: the previous implementation fell back to
    # importing and directly executing backend.transaction_engine's Plan/
    # Apply functions IN-PROCESS inside the Web Manager whenever the
    # composite_workflows IPC call raised any exception -- meaning the Web Manager
    # silently became the mutation authority (bypassing every engine-side
    # TOCTOU/root/symlink/identity check) any time the engine was
    # unreachable, authentication failed, DNS failed, or the response
    # failed to parse. That is exactly the architecture violation SEC-002/
    # ARCH-003 exists to close. There is no local fallback: if the engine
    # cannot be reached, this fails closed and reports a stable error --
    # nothing is mutated locally, ever.
    try:
        plan_res = composite_workflows.plan_artist_folder_reconcile(op_payload)
    except (BeetsUnavailableError, BeetsError) as ex:
        log.append("Engine unavailable; artist folder merge was not performed.")
        _app_logger.error("Artist folder merge: engine unavailable: %s", ex)
        return summary
    except Exception as ex:
        log.append("Engine communication failed; artist folder merge was not performed.")
        _app_logger.error("Artist folder merge: unexpected engine communication failure: %s", ex)
        return summary

    if not plan_res.get("ok"):
        log.append(f"Refusing to operate: {plan_res.get('error')}")
        return summary

    op_id = plan_res["operation_id"]
    apply_res = _apply_artist_folder_reconcile_resilient(op_id, log, log_prefix="Artist folder merge")

    if not apply_res.get("ok"):
        log.append(f"Engine artist folder merge failed: {apply_res.get('error')}")
        return summary

    _invalidate_lib_cache()
    log.append(f"  [merge] Delegated artist folder merge to engine (op_id={op_id})")
    summary["files"] = apply_res.get("moved_files", 0)
    summary["folders"] = summary.get("groups", 0)

    log.append(f"\nDone: merged {summary['groups']} artist group(s), moved {summary['files']} file(s).")
    return summary


_STAMP_UUID_IN_NAME_CAPTURE_RE = re.compile(
    r'\s*(?:\{|\()([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:\}|\))\s*$',
    re.IGNORECASE,
)


def _artist_folder_mbid_from_name(name: str) -> str:
    match = _STAMP_UUID_IN_NAME_CAPTURE_RE.search(_s(name))
    return match.group(1).lower() if match else ""


def _artist_folder_canonical_name(artist_name: str, mb_artistid: str = "") -> str:
    """Return the configured artist-folder form used by imports."""
    base = _safe_artist_folder_name(artist_name)
    mbid = _s(mb_artistid).strip().lower()
    if _MB_UUID_RE.match(mbid):
        return f"{base} ({mbid})"
    return base


def _stamp_db_path_prefix_pairs(src: Path, dst: Path) -> List[Tuple[bytes, bytes]]:
    return [
        (str(src).encode() + b"/", str(dst).encode() + b"/"),
        (src.name.encode() + b"/", dst.name.encode() + b"/"),
    ]


def _stamp_artist_folder_scan(root: Path) -> Dict[str, Any]:
    """Scan artist folders under root for MB ID stamping eligibility.

    A folder is eligible when >=75% of its Beets albums share a single
    mb_albumartistid UUID and the folder is not already canonical.
    Already-stamped same-UUID duplicates are also candidates, even when they
    are disk-only and have no imported Beets albums yet.
    Returns both eligible candidates and skipped folders with blockers.
    """
    candidates = []
    skipped = []
    try:
        # ARCH-020: folder listing is an engine-side fact -- Web Manager has
        # no local media mount in the supported two-service deployment and
        # must never walk MUSIC_ROOT itself. The engine's raw path strings
        # are turned back into Path objects purely for string manipulation
        # (.name/.parent/.resolve(strict=False)) below; none of that requires
        # the path to actually exist locally.
        folders = sorted(
            (Path(_s(entry.get("path")) or str(root / _s(entry.get("name"))))
             for entry in composite_workflows.get_artist_folder_inventory(str(root))
             if _s(entry.get("name")) and not _s(entry.get("name")).startswith(".")),
            key=lambda p: p.name.casefold(),
        )
    except (BeetsUnavailableError, BeetsAuthError, BeetsBadRequestError, BeetsNotFoundError, BeetsError) as ex:
        # Independent review finding: an engine inventory failure (timeout,
        # auth, 4xx/5xx) must never be indistinguishable from a genuine
        # successful scan that found zero eligible folders -- callers used
        # to see the same empty {"candidates": [], "skipped": []} shape for
        # both and would report "No artist folders need MB ID stamping" /
        # mark the phase complete even though the engine was never actually
        # reached. ok=False plus the structured error fields let every
        # caller fail closed instead.
        #
        # Independent review follow-up (CodeQL: information exposure
        # through an exception): str(ex) can carry internal URLs, paths, or
        # transport details, and this "error" field flows into HTTP JSON
        # responses and job-visible logs. Log the real exception
        # server-side only; return a sanitized message plus the structured
        # error_code/status_code fields (which are agent-controlled,
        # stable, and safe to expose).
        _app_logger.error("Artist folder inventory scan failed: %s", ex, exc_info=True)
        return {
            "ok": False, "candidates": [], "skipped": [],
            "error": _safe_inventory_error_message(ex),
            "error_code": getattr(ex, "error_code", "") or "",
            "status_code": getattr(ex, "status_code", 0) or 0,
        }
    except Exception as ex:
        _app_logger.error("Artist folder inventory scan failed with an unexpected error: %s", ex, exc_info=True)
        return {"ok": False, "candidates": [], "skipped": [], "error": _safe_inventory_error_message(ex), "error_code": "", "status_code": 0}
    existing_names = {f.name for f in folders}

    folder_id_album_sets, folder_album_totals, scan_error = _stamp_artist_folder_album_mbid_counts(root, folders)
    if scan_error:
        return {
            "ok": False,
            "error": scan_error,
            "error_code": "",
            "status_code": 0,
            "candidates": [],
            "skipped": [
                {
                    "path": str(folder),
                    "name": folder.name,
                    "reason": f"scan error: {scan_error}",
                }
                for folder in folders
            ],
        }

    for folder in folders:
        folder_info = {
            "path": str(folder),
            "name": folder.name,
        }
        folder_key = str(folder)
        album_total = int(folder_album_totals.get(folder_key) or 0)
        if not album_total:
            skipped.append({
                **folder_info,
                "reason": "no albums with MB artist IDs were found under this folder",
            })
            continue
        id_counts = {
            aid: len(album_ids)
            for aid, album_ids in (folder_id_album_sets.get(folder_key) or {}).items()
        }
        if not id_counts:
            skipped.append({
                **folder_info,
                "reason": "albums were found, but none had a valid MB artist UUID",
            })
            continue
        best_id, best_count = max(id_counts.items(), key=lambda kv: kv[1])
        match_ratio = best_count / album_total if album_total else 0.0
        if album_total < 1 or match_ratio < 0.75:
            skipped.append({
                **folder_info,
                "reason": f"best MB artist UUID only matched {best_count}/{album_total} album(s) ({round(match_ratio * 100):.0f}%)",
                "match_ratio": round(match_ratio, 3),
                "album_count": album_total,
                "mb_albumartistid": best_id,
            })
            continue
        canonical_lookup = _mb_artist_lookup_by_id(best_id)
        canonical_name = _safe_artist_folder_name(
            canonical_lookup.get("name") or _artist_folder_name_without_mbid(folder.name)
        )
        new_name = _artist_folder_canonical_name(canonical_name, best_id)
        new_path = folder.parent / new_name
        if folder.resolve(strict=False) == new_path.resolve(strict=False):
            skipped.append({
                **folder_info,
                "reason": "already stamped with the canonical MB artist folder name",
                "mb_albumartistid": best_id,
            })
            continue
        candidates.append({
            **folder_info,
            "new_name": new_name,
            "new_path": str(new_path),
            "mb_albumartistid": best_id,
            "canonical_artist": canonical_name,
            "target_exists": new_path.name in existing_names,
            "album_count": album_total,
            "match_ratio": round(match_ratio, 3),
        })

    candidate_paths = {
        str(Path(c["path"]).resolve(strict=False))
        for c in candidates
    }
    stamped_folders_by_mbid: Dict[str, List[Path]] = {}
    for folder in folders:
        stamped_mbid = _artist_folder_mbid_from_name(folder.name)
        if stamped_mbid:
            stamped_folders_by_mbid.setdefault(stamped_mbid, []).append(folder)

    for stamped_mbid, same_id_folders in stamped_folders_by_mbid.items():
        if len(same_id_folders) < 2:
            continue
        canonical_lookup = _mb_artist_lookup_by_id(stamped_mbid)
        fallback_name = _artist_folder_name_without_mbid(
            sorted(same_id_folders, key=lambda p: p.name.casefold())[0].name
        )
        canonical_name = _safe_artist_folder_name(canonical_lookup.get("name") or fallback_name)
        new_name = _artist_folder_canonical_name(canonical_name, stamped_mbid)
        new_path = root / new_name
        new_resolved = new_path.resolve(strict=False)
        for folder in same_id_folders:
            folder_resolved = folder.resolve(strict=False)
            if folder_resolved == new_resolved:
                continue
            folder_key = str(folder_resolved)
            if folder_key in candidate_paths:
                continue
            album_total = int(folder_album_totals.get(str(folder)) or 0)
            candidates.append({
                "path": str(folder),
                "name": folder.name,
                "new_name": new_name,
                "new_path": str(new_path),
                "mb_albumartistid": stamped_mbid,
                "canonical_artist": canonical_name,
                "target_exists": new_path.name in existing_names,
                "album_count": album_total,
                "match_ratio": 1.0,
                "same_mbid_duplicate": True,
            })
            candidate_paths.add(folder_key)

    stamped_targets_by_key: Dict[str, List[Path]] = {}
    for folder in folders:
        stamped_mbid = _artist_folder_mbid_from_name(folder.name)
        if not stamped_mbid:
            continue
        merge_key = _artist_folder_merge_key(folder.name)
        if merge_key:
            stamped_targets_by_key.setdefault(merge_key, []).append(folder)

    for folder in folders:
        if _artist_folder_mbid_from_name(folder.name):
            continue
        folder_key = str(folder.resolve(strict=False))
        if folder_key in candidate_paths:
            continue
        merge_key = _artist_folder_merge_key(folder.name)
        if not merge_key:
            continue
        target_folders = stamped_targets_by_key.get(merge_key) or []
        target_mbids = {
            _artist_folder_mbid_from_name(target.name)
            for target in target_folders
            if _artist_folder_mbid_from_name(target.name)
        }
        if len(target_mbids) != 1:
            continue
        target = sorted(target_folders, key=lambda p: p.name.casefold())[0]
        target_mbid = next(iter(target_mbids))
        if folder.resolve(strict=False) == target.resolve(strict=False):
            continue

        # This merge is keyed on case-folded folder-name equality alone — no
        # per-album MB artist UUID evidence like the block above. Two
        # different artists that happen to share a folder name would
        # otherwise be silently commingled under one wrong MBID. Sample a
        # few audio files and AcoustID-verify before trusting the name match.
        # Only an explicit True confirmation authorizes it -- an absent or
        # unavailable fingerprint result (None) is not evidence of a safe
        # match and must not be treated as though it were confirmed.
        canonical_artist = _artist_folder_name_without_mbid(target.name)
        fp_result = _artist_folder_fingerprint_confirms(folder, canonical_artist)
        if fp_result is not True:
            reason = (
                f"folder name matches {target.name!r} but AcoustID fingerprint of sampled "
                f"tracks resolved to a different artist — not merging to avoid commingling "
                f"two different artists"
                if fp_result is False
                else (
                    f"folder name matches {target.name!r} but no AcoustID fingerprint evidence "
                    f"was available to confirm this match"
                )
            )
            skipped.append({
                "path": str(folder),
                "name": folder.name,
                "reason": reason,
                "mb_albumartistid": target_mbid,
                "canonical_artist": canonical_artist,
            })
            # Deliberately not added to candidate_paths: this folder was
            # skipped, not promoted to a candidate. candidate_paths is used
            # below to drop *stale* skip entries superseded by a real
            # candidacy elsewhere -- adding a skip-only path here would
            # cause the end-of-function filter to erroneously discard this
            # exact skip reason, silently hiding why the merge was blocked.
            continue

        album_total = int(folder_album_totals.get(str(folder)) or 0)
        candidates.append({
            "path": str(folder),
            "name": folder.name,
            "new_name": target.name,
            "new_path": str(target),
            "mb_albumartistid": target_mbid,
            "canonical_artist": canonical_artist,
            "target_exists": True,
            "album_count": album_total,
            "match_ratio": 1.0,
            "plain_stamped_duplicate": True,
            "fingerprint_verified": fp_result is True,
        })
        candidate_paths.add(folder_key)

    if candidate_paths:
        skipped = [
            entry for entry in skipped
            if str(Path(entry["path"]).resolve(strict=False)) not in candidate_paths
        ]
    return {"ok": True, "candidates": candidates, "skipped": skipped}


def _stamp_artist_folder_candidates(root: Path) -> List[Dict[str, Any]]:
    return _stamp_artist_folder_scan(root)["candidates"]


def _append_stamp_candidate_log(log: List[str], candidates: List[Dict[str, Any]], limit: int = 20) -> None:
    for c in candidates[:limit]:
        action = "merge into" if c.get("target_exists") else "rename to"
        detail = (
            "same MBID duplicate"
            if c.get("same_mbid_duplicate")
            else "plain folder matches stamped artist folder"
            if c.get("plain_stamped_duplicate")
            else f"{c['album_count']} album(s), {c['match_ratio']*100:.0f}% match"
        )
        log.append(f"  Would {action}: {c['name']!r} → {c['new_name']!r}  ({detail})")
    if len(candidates) > limit:
        log.append(f"  … and {len(candidates) - limit} more candidate folder(s)")


def _append_stamp_skipped_log(
    log: List[str],
    skipped: List[Dict[str, Any]],
    *,
    include_examples: bool = True,
    limit: int = 20,
) -> None:
    if not skipped:
        return
    reason_counts = Counter(s["reason"] for s in skipped)
    log.append("  Skipped folders:")
    for reason, count in reason_counts.most_common():
        log.append(f"    {count} folder(s): {reason}")
    if not include_examples:
        return
    for entry in skipped[:limit]:
        log.append(f"    {entry['name']!r}: {entry['reason']}")
    if len(skipped) > limit:
        log.append(f"    … and {len(skipped) - limit} more")


def item_dict(item) -> Dict[str, Any]:
    return {
        "id":          item.id,
        "title":       item.title,
        "artist":      item.artist,
        "album":       item.album,
        "albumartist": item.albumartist,
        "track":       item.track,
        "disc":        item.disc,
        "year":        item.year,
        "genre":       _s(getattr(item, "genre", "")),
        "path":        _s(item.path),
        "added":       getattr(item, "added", 0),
    }


def item_dict_full(item) -> Dict[str, Any]:
    d = item_dict(item)
    for field, _ in EDITABLE_FIELDS:
        if field not in d:
            d[field] = _s(getattr(item, field, "") or "")
    d["length"]  = round(float(getattr(item, "length",  0) or 0), 1)
    d["bitrate"] = getattr(item, "bitrate", 0)
    d["format"]  = _s(getattr(item, "format", ""))
    return d


def album_dict(album) -> Dict[str, Any]:
    return {
        "id":          getattr(album, "id", None) if not isinstance(album, dict) else album.get("id"),
        "album":       _s(getattr(album, "album", "") if not isinstance(album, dict) else album.get("album", "")),
        "albumartist": _s(getattr(album, "albumartist", "") if not isinstance(album, dict) else album.get("albumartist", "")),
        "year":        getattr(album, "year", 0) if not isinstance(album, dict) else album.get("year", 0),
        "genre":       _s(getattr(album, "genre", "") if not isinstance(album, dict) else album.get("genre", "")),
        "mb_albumid":  _s(getattr(album, "mb_albumid", "") if not isinstance(album, dict) else album.get("mb_albumid", "")),
        "mb_releasegroupid": _s(getattr(album, "mb_releasegroupid", "") if not isinstance(album, dict) else album.get("mb_releasegroupid", "")),
        "path":        _get_album_item_dir(album),
    }


# Service behind POST /api/fetch-missing-art (ARCH-001): request-free,
# returns (json_body, http_status); the route and in-process callers share it.
def start_fetch_missing_art(payload_in: Dict[str, Any]) -> Tuple[Any, int]:
    """Background job: find albums missing usable local art and repair them."""
    def _do(log, cancel_event=None):
        started_at = time.time()
        report = _art_repair_build_report()
        items = list(report.get("items") or [])
        skipped = max(0, int(report.get("total_albums") or 0) - len(items))
        unresolved_items = [item for item in items if item.get("issue") == "unresolved"]
        actionable = [item for item in items if item.get("actionable")]
        log.append(f"Checking {report.get('total_albums', 0)} albums for missing art...")
        log.append(
            f"Found {len(items)} album(s) needing art repair; "
            f"{len(actionable)} actionable, {len(unresolved_items)} unresolved, {skipped} already have art."
        )

        saved_items: List[Dict[str, Any]] = []
        failed_items: List[Dict[str, Any]] = []

        for idx, info in enumerate(actionable, start=1):
            if cancel_event and cancel_event.is_set():
                log.append("[cancelled]")
                break
            aid = int(info["album_id"])
            artist_name = info["albumartist"]
            album_name = info["album"]
            log.append(f"[{idx}/{len(actionable)}] Fetching: {artist_name} - {album_name}")
            try:
                result = _repair_album_art(aid, log, cancel_event)
                if result.get("status") == "saved":
                    log.append(f"  saved by {result.get('source') or 'repair'}")
                    saved_items.append(result)
                else:
                    log.append(f"  failed: {result.get('error') or 'unknown error'}")
                    failed_items.append(result)
                time.sleep(0.42)
            except Exception as ex:
                log.append(f"  warning: {ex}")
                failed_items.append({**info, "status": "failed", "source": "", "error": str(ex)})
                continue

        _invalidate_lib_cache()
        refreshed = _art_repair_build_report()
        summary = {
            "ok": True,
            "started_at": started_at,
            "finished_at": time.time(),
            "missing": len(items),
            "saved": len(saved_items),
            "fetchart_saved": sum(1 for item in saved_items if item.get("source") == "fetchart"),
            "fallback_saved": sum(1 for item in saved_items if item.get("source") == "discogs"),
            "failed": len(failed_items),
            "skipped": skipped,
            "unresolved": len(unresolved_items),
            "saved_items": saved_items,
            "failed_items": failed_items,
            "unresolved_items": unresolved_items,
            "remaining_items": refreshed.get("items") or [],
            "counts": refreshed.get("counts") or {},
        }
        _art_repair_save_last(summary)
        log.append(
            f"Done - {len(items)} missing, {len(saved_items)} saved "
            f"({summary['fetchart_saved']} fetchart, {summary['fallback_saved']} fallback), "
            f"{len(failed_items)} failed, {len(unresolved_items)} unresolved, {skipped} already had art"
        )
        return summary
    job = jobs.start_python(
        _do,
        label="Fetch Missing Album Art",
        metadata={"type": "fetch-missing-art"},
    )
    return {"ok": True, "job_id": job.job_id}, 200


_STAMP_DB_PATH_COLUMNS = {
    ("items", "path"),
    ("albums", "artpath"),
}
