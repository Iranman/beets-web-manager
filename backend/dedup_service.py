"""Duplicate scan and cleanup orchestration (ARCH-001).

Unattended deletion requires fingerprint or byte identity plus the
release-slot safeguards (backend.duplicate_identity).
"""

from __future__ import annotations

import difflib, re, time, uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from backend.app_runtime import _app_logger, DOWNLOADS_ROOT, MUSIC_ROOT, _MB_TRACK_PREFLIGHT_MATCH_THRESHOLD, _s
from backend.library_service import _scan_scope_label
from backend.playlist_service import (
    _match_track,
    _norm,
    _playlist_item_text_variants,
    _playlist_library_match_candidates,
    _playlist_match_payload_from_candidates,
    _playlist_quality_for_item,
)
from backend.cleanup_service import _album_cleanup_merge_plan
from backend.app_runtime import _path_is_under, _path_under
from backend.title_normalize import restore_time_colon_title as _restore_time_colon_title
from backend.beets_adapter import lib, BeetsError, BeetsUnavailableError
import backend.composite_workflows as composite_workflows
import backend.duplicate_identity as _duplicate_identity
from backend.acoustid_service import AUDIO_EXTS, _acoustid_fingerprint_ids, _acoustid_fingerprint_match, _album_track_norm, _read_file_media_tags
from backend.matching_service import _album_mb_completeness
from backend.app_runtime import jobs
from backend.job_service import _running_job_of_type, _wait_for_child_job
from backend.import_review_service import _import_review_cleanup_destination, _unique_import_review_cleanup_path
from backend.serializers import _json_from_flask_response
from backend.maintenance_service import _library_health_payload, _maintenance_extract_child_job_id, _maintenance_same_file_hash, _maintenance_save_last_report

import backend.dedup_authorization as _dedup_authorization
from backend.app_runtime import WEB_MANAGER_DATA_DIR

# ── ARCH-001 extracted code ──


_BROWSE_ALLOWED_ROOTS = (MUSIC_ROOT, DOWNLOADS_ROOT)


_dedup_scans: Dict[str, Any] = {}   # jid → {status, log, duplicates, scanned, found, total}


def _dedup_norm_path(path_value: Any) -> str:
    try:
        return str(Path(str(path_value or "")).resolve(strict=False))
    except Exception:
        return str(path_value or "")


def _dedup_resolve_source_scan(scan_jid: str, scan_path: str = ""):
    """Resolve the standard duplicate scan used as the source for AI review."""
    scan_jid = (scan_jid or "").strip()
    if scan_jid:
        state = _dedup_scans.get(scan_jid)
        if state and state.get("kind", "scan") == "scan":
            return scan_jid, state, None
        if state:
            return None, None, "AI review needs a duplicate scan job, not an AI review job"

    wanted_path = _dedup_norm_path(scan_path)
    candidates = []
    for jid, state in _dedup_scans.items():
        if state.get("kind", "scan") != "scan":
            continue
        if wanted_path and _dedup_norm_path(state.get("scan_path")) != wanted_path:
            continue
        candidates.append((float(state.get("created_at") or 0), jid, state))
    if candidates:
        _, jid, state = sorted(candidates, reverse=True)[0]
        return jid, state, None

    if scan_jid:
        return None, None, "Duplicate scan expired or was lost after an app restart. Run Scan again, then AI Review."
    return None, None, "Run a duplicate scan before AI Review."


def _dedup_cancel_requested(cancel) -> bool:
    try:
        return bool(cancel and cancel.is_set())
    except Exception:
        return False


def _dedup_raise_if_cancelled(cancel, state: Dict[str, Any]) -> None:
    if not _dedup_cancel_requested(cancel):
        return
    state["status"] = "cancelled"
    state.setdefault("log", []).append("Cancelled by user from Jobs.")
    raise RuntimeError("cancelled")


def _dedup_structured_state(state: Dict[str, Any], **updates: Any) -> Dict[str, Any]:
    if updates:
        state.update(updates)
    scanned = int(state.get("scanned") or 0)
    total = int(state.get("total") or 0)
    found = int(state.get("found") or 0)
    current_path = _s(state.get("current_path") or "")
    current_file = Path(current_path).name if current_path else ""
    progress_pct = round((scanned / total) * 100) if total else 0
    payload: Dict[str, Any] = {
        "category": "Duplicates",
        "current_task": state.get("current_task") or "Scanning files for duplicates",
        "current_result": state.get("current_result") or (
            f"Scanning {scanned}/{total} ({progress_pct}%) · {found} duplicate candidate(s) found" if total else ""
        ),
        "scan_scope": state.get("scan_scope"),
        "scan_path": state.get("scan_path"),
        "scanned_count": scanned,
        "found_count": found,
        "progress_percent": progress_pct,
    }
    if current_file:
        payload["current_file"] = current_file
    if total:
        payload["total_count"] = total
        payload["remaining_count"] = max(0, total - scanned)
    for key in ("current_path", "current_item", "duplicate_type", "error_summary", "error_count", "final_summary"):
        if state.get(key) is not None:
            payload[key] = state.get(key)
    return payload


def _dedup_final_summary(state: Dict[str, Any],
                         *,
                         source_files: Optional[List[Path]] = None) -> Dict[str, Any]:
    duplicates = state.get("duplicates") or []
    folders = state.get("folders") or []
    type_counts = Counter(_s(d.get("match_type") or "duplicate") for d in duplicates)
    summary: Dict[str, Any] = {
        "scanned_files": int(state.get("total") or state.get("scanned") or 0),
        "duplicate_tracks_found": len(duplicates),
        "duplicate_folders_found": len(folders),
    }
    if source_files is not None:
        summary["scanned_folders"] = len({str(p.parent) for p in source_files})
    if type_counts:
        summary["duplicate_type_counts"] = dict(type_counts)
    return summary


def _dedup_state_status(job: Any, state: Dict[str, Any]) -> str:
    if job:
        return "running" if getattr(job, "status", "") == "running" else "done"
    if state.get("status") == "running":
        return "running"
    return "done"


def _dedup_state_response(jid: str, state: Dict[str, Any], job: Any = None) -> Dict[str, Any]:
    if job is None:
        job = jobs.get(jid)
    status = _dedup_state_status(job, state)
    log = getattr(job, "log", None) if job else None
    if log is None:
        log = state.get("log", [])
    response = {
        "ok":         True,
        "job_id":     jid,
        "job_status": getattr(job, "status", None) if job else state.get("job_status", status),
        "kind":       state.get("kind", "scan"),
        "scan_path":  state.get("scan_path", ""),
        "status":     status,
        "log":        log,
        "scanned":    state.get("scanned", 0),
        "total":      state.get("total", 0),
        "found":      state.get("found", 0),
        "duplicates": state.get("duplicates", []) if status == "done" else [],
        "folders":    state.get("folders", []) if status == "done" else [],
    }
    job_state = getattr(job, "state", None) if job else None
    if isinstance(job_state, dict) and job_state:
        response["state"] = dict(job_state)
    return response


def _dedup_job_result(kind: str, state: Dict[str, Any]) -> Dict[str, Any]:
    result = {
        "kind": kind,
        "scan_path": state.get("scan_path", ""),
        "scanned": state.get("scanned", 0),
        "total": state.get("total", 0),
        "found": state.get("found", 0),
        "duplicates": state.get("duplicates", []),
        "folders": state.get("folders", []),
    }
    if state.get("final_summary"):
        result["final_summary"] = state.get("final_summary")
    return result


def _resolve_album_title_duplicate_candidate(
    library: Any,
    folder_raw: str,
    track_title: str,
    threshold: float = _MB_TRACK_PREFLIGHT_MATCH_THRESHOLD,
    logger_instance: Optional[Any] = None,
    album_index: Optional[Dict[str, List[Any]]] = None,
) -> Tuple[Optional[Any], str]:
    """Find duplicate library track candidate by extracting folder album name and comparing track title.

    Used by dedup_scan to match files where the source folder represents the album name.
    Returns (candidate_item, match_type_str). Returns (None, "") on no match or query failure.
    """
    log = logger_instance or _app_logger
    if not track_title or not folder_raw:
        return None, ""

    folder_album = re.sub(r'\s*[\(\[]\d{4}[\)\]]\s*$', '', folder_raw).strip()
    folder_album = re.sub(r'^\d{4}\s*[-–—]\s*', '', folder_album).strip()
    folder_album = _restore_time_colon_title(folder_album)
    if not folder_album:
        return None, ""

    try:
        from difflib import SequenceMatcher
        if album_index is not None:
            folder_album_lower = folder_album.lower()
            candidates = list(album_index.get(folder_album_lower, []))
            if not candidates:
                for alb_key, alb_items in album_index.items():
                    if alb_key and SequenceMatcher(None, alb_key, folder_album_lower).ratio() >= 0.8:
                        candidates.extend(alb_items)
        else:
            candidates = list(library.items(f"album:{folder_album}"))
        for cand in candidates:
            cand_album = (getattr(cand, "album", None) or getattr(cand, "get", lambda k, d="": "")("album") or "").lower()
            if cand_album and SequenceMatcher(None, cand_album, folder_album.lower()).ratio() < 0.8:
                continue
            cand_title = (getattr(cand, "title", None) or getattr(cand, "get", lambda k, d="": "")("title") or "").lower()
            score = SequenceMatcher(None, cand_title, track_title.lower()).ratio()
            if score >= threshold:
                return cand, f"album+title ({min(1.0, score):.0%})"
    except BeetsError as ex:
        log.warning("Album duplicate candidate search failed for '%s': %s", folder_album, ex)
    except Exception as ex:
        log.error("Unexpected error during album+title duplicate candidate search for '%s': %s", folder_album, ex, exc_info=True)
        raise

    return None, ""


def _resolve_dedup_scan_path(raw: Any) -> Tuple[Optional[Path], Optional[str]]:
    """Validate the dedup-scan source path against the same allowlist as
    /api/browse (library + downloads roots) before any filesystem
    operation touches it.

    SEC-002 CodeQL repository-wide closure finding: /api/dedup/scan
    previously called Path(path).exists() and scan_path.rglob("*") against
    the raw request body value with no containment check at all, allowing
    an authenticated caller to enumerate audio-file names (and, via the
    later per-file stat() calls in dedup_scan's _run(), file sizes) under
    any path the container process can read -- not just the intended
    library/downloads scope.
    """
    text = _s(raw).strip()
    if not text:
        return None, "Path is required"
    try:
        candidate = Path(text)
        if not candidate.is_absolute():
            return None, "Path must be absolute"
        resolved = candidate.resolve(strict=False)
    except Exception:
        return None, "Invalid path."
    if not any(
        _path_is_under(resolved, root) or resolved == root.resolve(strict=False)
        for root in _BROWSE_ALLOWED_ROOTS
    ):
        return None, "Path is outside the allowed scan roots"
    return resolved, None


# Service behind POST /api/dedup/scan (ARCH-001): request-free,
# returns (json_body, http_status); the route and in-process callers share it.
def start_dedup_scan(payload_in: Dict[str, Any]) -> Tuple[Any, int]:
    """Start a background dedup scan; returns job_id immediately."""
    payload   = payload_in
    path_raw  = payload.get("path", "/data/torrents/music")
    # tracked_only: check only files Beets tracks (the scheduled cleanup can
    # only ever act on tracked pairs, so untracked files are out of scope).
    tracked_only = payload.get("tracked_only") is True
    scan_path, path_error = _resolve_dedup_scan_path(path_raw)
    if path_error:
        return {"ok": False, "error": path_error}, 400
    if not scan_path.exists():
        return {"ok": False, "error": f"Path not found: {scan_path}"}, 200

    state: Dict[str, Any] = {
        "kind":       "scan",
        "created_at": time.time(),
        "status":     "running",
        "log":        [],
        "duplicates": [],
        "folders":    [],   # folder-level groupings added at end of scan
        "scanned":    0,
        "found":      0,
        "total":      0,
        "scan_path":  str(scan_path),  # stored so AI review can re-enumerate
        "scan_scope": _scan_scope_label(scan_path),
    }

    # ── YouTube / generic filename parser ──────────────────────────────────────
    _YT_STRIP = re.compile(
        r'\s*[\(\[](?:official\s*(?:video|music\s*video|audio|lyric\s*video|clip)|'
        r'lyric(?:s|\s*video)?|audio|hd|hq|4k|explicit|clean|remaster(?:ed)?|'
        r'live|acoustic|instrumental|extended|radio\s*edit|feat\.?[^\)\]]*|'
        r'ft\.?[^\)\]]*|\d{4})[^\)\]]*[\)\]]\s*',
        re.I
    )

    def _parse_filename_tags(stem: str):
        """Extract (artist, title) from a filename when embedded tags are absent.

        Handles beets-style names:  Artist - Album - NN - Title
        Also handles:               Artist - NN - Title
                                    Artist - Title
                                    NN - Title  (after stripping leading number)
        """
        # strip leading track numbers  "01 - " or "01. "
        clean = re.sub(r'^\d+\s*[\-\.]\s*', '', stem).strip()
        # strip YouTube decorators
        clean = _YT_STRIP.sub('', clean).strip()
        for sep in (' - ', ' – ', ' — '):
            if sep not in clean:
                continue
            parts = [p.strip() for p in clean.split(sep)]
            # Artist - Album - NN - Title  (4+ parts, parts[2] is a bare number)
            if len(parts) >= 4 and re.match(r'^\d{1,3}$', parts[2]):
                return parts[0], sep.join(parts[3:])
            # Artist - NN - Title  (3 parts, parts[1] is a bare number)
            if len(parts) == 3 and re.match(r'^\d{1,3}$', parts[1]):
                return parts[0], parts[2]
            # Artist - Title  or  Artist - Something - Title
            return parts[0], sep.join(parts[1:])
        return '', clean

    def _item_library_path(item) -> str:
        raw = _s(getattr(item, "path", "") or "")
        if raw and not Path(raw).is_absolute():
            raw = str(MUSIC_ROOT / raw)
        return raw

    def _path_key(path_value: Any) -> str:
        try:
            return str(Path(_s(path_value)).resolve(strict=False)).casefold()
        except Exception:
            return _s(path_value).casefold()

    def _run(log, cancel, update_state=None):
        state["log"] = log
        if update_state:
            update_state(_dedup_structured_state(
                state,
                current_task="Listing source audio files",
                current_result="Scanning source path",
            ))
        if tracked_only:
            state["log"].append(f"Checking Beets-tracked library files under: {scan_path}")
        else:
            state["log"].append(f"Scanning downloads folder: {scan_path}")
        state["log"].append(f"Comparing against library items in beets database …")
        try:
            _dedup_raise_if_cancelled(cancel, state)
            all_items = list(lib.items([]))
            if tracked_only:
                scan_root = scan_path.resolve(strict=False)
                tracked = {Path(_item_library_path(it)) for it in all_items}
                source_files = sorted(
                    p for p in tracked
                    if p.resolve(strict=False).is_relative_to(scan_root)
                    and p.suffix.lower() in AUDIO_EXTS and p.is_file()
                )
            else:
                source_files = sorted(
                    p for p in scan_path.rglob("*")
                    if p.is_file() and p.suffix.lower() in AUDIO_EXTS
                )
        except Exception as exc:
            state["log"].append(f"ERROR listing files: {exc}")
            state["status"] = "done"
            state["final_summary"] = {"scanned_files": 0, "duplicate_tracks_found": 0, "error": str(exc)}
            if update_state:
                update_state(_dedup_structured_state(
                    state,
                    current_task="Listing source audio files",
                    current_result="Failed to list source files",
                    error_count=1,
                    error_summary=f"Could not list source files: {exc}",
                    final_summary=state["final_summary"],
                ))
            return
        total = len(source_files)
        state["total"] = total
        state["log"].append(f"Found {total} audio file{'s' if total != 1 else ''} to check")
        if update_state:
            update_state(_dedup_structured_state(
                state,
                current_task="Building library comparison index",
                current_result=f"Found {total} source audio file(s)",
            ))

        # Build fast library indexes for duplicate identity checks ONCE.
        size_index: Dict[int, list] = {}
        mb_trackid_index: Dict[str, list] = {}
        path_to_item: Dict[str, Any] = {}
        album_index: Dict[str, list] = {}
        items_by_id: Dict[int, Any] = {}
        fuzzy_candidates_all: List[Dict[str, Any]] = []
        fuzzy_candidates_by_title: Dict[str, List[Dict[str, Any]]] = {}
        _dedup_raise_if_cancelled(cancel, state)
        for item in all_items:
            _dedup_raise_if_cancelled(cancel, state)
            lp = _item_library_path(item)
            mbid = _s(getattr(item, "mb_trackid", "") or "").strip()
            if mbid:
                mb_trackid_index.setdefault(mbid, []).append(item)
            try:
                items_by_id[int(getattr(item, "id", 0) or 0)] = item
            except (TypeError, ValueError):
                pass
            alb = _s(getattr(item, "album", "") or "").strip().lower()
            if alb:
                album_index.setdefault(alb, []).append(item)
            try:
                sz = Path(lp).stat().st_size
                size_index.setdefault(sz, []).append(item)
                path_to_item[_path_key(lp)] = (item, sz)
            except Exception:
                pass

            # Build fuzzy candidate rows
            try:
                path_text = _s(getattr(item, "path", ""))
                quality = _playlist_quality_for_item(item, path_text)
                payload = {
                    "id": getattr(item, "id", 0),
                    "title": getattr(item, "title", ""),
                    "artist": getattr(item, "artist", ""),
                    "album": getattr(item, "album", ""),
                    "path": path_text,
                }
                payload.update(quality)
                seen: set = set()
                for cand_artist, cand_title in _playlist_item_text_variants(item):
                    key = (_norm(cand_artist), _norm(cand_title))
                    if not key[1] or key in seen:
                        continue
                    seen.add(key)
                    row = {
                        "artist": cand_artist,
                        "title": cand_title,
                        "quality": quality,
                        "payload": payload,
                    }
                    fuzzy_candidates_all.append(row)
                    fuzzy_candidates_by_title.setdefault(key[1], []).append(row)
            except Exception:
                pass

        fuzzy_candidates = {"all": fuzzy_candidates_all, "by_title": fuzzy_candidates_by_title}

        def _prepare_source(src: Path) -> Dict[str, Any]:
            """I/O step for one source file: size + mb_trackid/artist/title.
            Reuses already-known DB tags for files that are already library
            items (no stat()/MediaFile() re-read needed); otherwise reads the
            file directly. Safe to run concurrently across files — touches no
            shared state."""
            known = path_to_item.get(_path_key(src))
            if known is not None:
                known_item, known_size = known
                return {
                    "ok": True,
                    "size": known_size,
                    "mb_trackid": _s(getattr(known_item, "mb_trackid", "") or "").strip(),
                    "artist": _s(getattr(known_item, "artist", "") or "").strip(),
                    "title": _s(getattr(known_item, "title", "") or "").strip(),
                }
            try:
                size = src.stat().st_size
            except FileNotFoundError:
                return {"ok": False, "error": "stale path"}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
            mb_trackid = artist = title = ""
            try:
                tags = _read_file_media_tags(src)
                mb_trackid = (tags.get("mb_trackid", "") or "").strip()
                artist     = (tags.get("artist",     "") or "").strip()
                title      = (tags.get("title",      "") or "").strip()
            except Exception:
                pass
            if not title:
                fn_artist, fn_title = _parse_filename_tags(src.stem)
                if not artist:
                    artist = fn_artist
                title = fn_title
            return {"ok": True, "size": size, "mb_trackid": mb_trackid, "artist": artist, "title": title}

        duplicates = []
        _DEDUP_BATCH_SIZE = 64
        _DEDUP_IO_WORKERS = 8
        with ThreadPoolExecutor(max_workers=_DEDUP_IO_WORKERS) as pool:
            for batch_start in range(0, total, _DEDUP_BATCH_SIZE):
                _dedup_raise_if_cancelled(cancel, state)
                batch = source_files[batch_start:batch_start + _DEDUP_BATCH_SIZE]
                # Skip the thread pool entirely for files we already know from
                # the DB (a plain dict lookup) - only files that need a real
                # stat()/MediaFile() read benefit from concurrency.
                prepared = list(pool.map(_prepare_source, batch))

                for offset, (src, info) in enumerate(zip(batch, prepared)):
                    i = batch_start + offset
                    _dedup_raise_if_cancelled(cancel, state)
                    state["scanned"] = i + 1
                    state["current_path"] = str(src)
                    state["current_task"] = "Scanning"
                    state["current_result"] = f"Scanning {i+1}/{total} ({round((i+1)/total*100) if total else 0}%) · {len(duplicates)} duplicate candidate(s) found"
                    if update_state and (i == 0 or i % 10 == 0 or i == total - 1):
                        update_state(_dedup_structured_state(state))
                    if i % 10 == 0 or i == total - 1:
                        state["log"].append(f"  [{i+1}/{total}] {src.name}")

                    if not info["ok"]:
                        state["log"].append(f"  skipped {info['error']}: {src}")
                        continue

                    source_size = info["size"]
                    mb_trackid = info["mb_trackid"]
                    artist = info["artist"]
                    title = info["title"]

                    lib_item   = None
                    match_type = ""

                    source_key = _path_key(src)

                    # 1. Exact MusicBrainz Track ID match (most reliable)
                    if mb_trackid:
                        results = mb_trackid_index.get(mb_trackid) or []
                        for canonical in results:
                            canonical_path = _item_library_path(canonical)
                            if canonical_path and _path_key(canonical_path) != source_key:
                                lib_item   = canonical
                                match_type = "MB Track ID"
                                break

                    # 2. Exact file size match (catches perfect copies with same bytes)
                    if not lib_item:
                        try:
                            src_size = source_size
                            if src_size > 0 and src_size in size_index:
                                for canonical in size_index[src_size]:
                                    canonical_path = _item_library_path(canonical)
                                    if canonical_path and _path_key(canonical_path) != source_key:
                                        lib_item   = canonical
                                        match_type = "identical file size"
                                        break
                        except Exception:
                            pass

                    # 3. Fuzzy artist + title match (threshold 0.88)
                    if not lib_item and title:
                        cand_payload = _playlist_match_payload_from_candidates(artist, title, fuzzy_candidates)
                        if cand_payload:
                            score = float(cand_payload.get("score") or 0)
                            if score >= 0.88:
                                # Resolve to the real Beets item so release-slot
                                # evidence (album, disc, track, IDs) stays intact.
                                try:
                                    real_item = items_by_id.get(int(cand_payload.get("id") or 0))
                                except (TypeError, ValueError):
                                    real_item = None
                                if real_item is not None:
                                    lib_item = real_item
                                else:
                                    cand_item = type("PlaylistMatchedItem", (), {})()
                                    for k, v in cand_payload.items():
                                        setattr(cand_item, k, v)
                                    lib_item = cand_item
                                match_type = f"fuzzy match {min(1.0, score):.0%}"

                    # 4. Album-folder + title match — catches beets-renamed files where
                    #    the source folder is "WILLOW (2019)" or "1999 - Californication"
                    if not lib_item and title:
                        cand_item, cand_match_type = _resolve_album_title_duplicate_candidate(
                            lib, src.parent.name, title, logger_instance=_app_logger, album_index=album_index
                        )
                        if cand_item:
                            lib_item = cand_item
                            match_type = cand_match_type

                    # 5. AcoustID fingerprint fallback — when tags/size/text heuristics
                    #    found nothing, fingerprint the source file and look for a
                    #    library item that resolves to the same MusicBrainz recording.
                    fingerprint_verified = False
                    fingerprint_mbid = ""
                    if not lib_item:
                        for fid in _acoustid_fingerprint_ids(str(src)):
                            results = mb_trackid_index.get(fid) or []
                            if results:
                                canonical = results[0]
                                canonical_path = _item_library_path(canonical)
                                if canonical_path and _path_key(canonical_path) == source_key:
                                    continue
                                lib_item   = canonical
                                match_type = "AcoustID fingerprint"
                                fingerprint_verified = True
                                fingerprint_mbid = fid
                                break

                    if not lib_item:
                        continue

                    lib_path = _s(lib_item.path)
                    # Normalise relative paths stored without the music root prefix
                    if lib_path and not lib_path.startswith("/"):
                        lib_path = str(MUSIC_ROOT / lib_path)
                    if str(src) == lib_path:          # same file — skip
                        continue
                    if not Path(lib_path).exists():   # library file gone — skip
                        continue

                    # 6. Fingerprint-verify every non-byte-identical match before
                    #    trusting it as a duplicate. AcoustID-fingerprint matches are
                    #    already audio-proven; identical file size is byte-grade. An
                    #    embedded MB Track ID is NOT audio proof: live data showed
                    #    duplicate groups whose shared embedded Recording ID the
                    #    fingerprint contradicts. A confirmed shared recording
                    #    marks the pair fingerprint-verified; a confirmed mismatch
                    #    rejects the candidate instead of risking a wrong-file
                    #    deletion.
                    src_fp_ids: List[str] = [fingerprint_mbid] if fingerprint_mbid else []
                    lib_fp_ids: List[str] = [fingerprint_mbid] if fingerprint_mbid else []
                    if (match_type == "MB Track ID" or match_type.startswith("fuzzy match")
                            or match_type.startswith("album+title")):
                        shared_id, src_fp_ids, lib_fp_ids = _acoustid_fingerprint_match(str(src), lib_path)
                        if shared_id:
                            fingerprint_verified = True
                            fingerprint_mbid = shared_id
                        elif src_fp_ids and lib_fp_ids:
                            state["log"].append(
                                f"  [REJECTED] [{match_type}] {src.name}"
                                f"  — AcoustID fingerprint disagrees with library candidate"
                            )
                            continue

                    if match_type in ("MB Track ID", "identical file size", "AcoustID fingerprint"):
                        confidence = "high"
                    elif fingerprint_verified:
                        confidence = "high"
                    else:
                        confidence = "medium"

                    reason_map = {
                        "MB Track ID":         "Embedded MusicBrainz track ID matches the library copy exactly.",
                        "identical file size": "File size is byte-identical to the library copy.",
                        "AcoustID fingerprint": f"AcoustID audio fingerprint matches library recording {fingerprint_mbid}.",
                    }
                    reason = reason_map.get(match_type, f"Matched by {match_type}.")
                    if fingerprint_verified and match_type not in reason_map:
                        reason += f" Confirmed by AcoustID audio fingerprint (recording {fingerprint_mbid})."

                    # ARCH-009: a shared Recording ID (or fingerprint) proves the
                    # same *recording*, not the same album. The same recording
                    # on a studio album and a compilation is two legitimate
                    # library entries, never a duplicate file to delete.
                    known_source = path_to_item.get(source_key)
                    source_item = known_source[0] if known_source else None
                    release_relation = _duplicate_identity.release_relation(source_item, lib_item)
                    if release_relation in ("different_release", "different_position"):
                        confidence = "medium"
                        reason += (
                            " Both files are library tracks in different release slots -- same recording, "
                            "not a duplicate file; review before removing either."
                        )

                    dup = {
                        "source_path":          str(src),
                        "source_filename":      src.name,
                        "source_artist":        artist,
                        "source_title":         title,
                        "source_size":          source_size,
                        "lib_path":             lib_path,
                        "lib_title":            lib_item.title  or "",
                        "lib_artist":           lib_item.artist or "",
                        "lib_album":            lib_item.album  or "",
                        "lib_id":               lib_item.id,
                        "lib_album_id":         _duplicate_identity.item_album_id(lib_item),
                        "source_item_id":       getattr(source_item, "id", None) if source_item is not None else None,
                        "source_album_id":      _duplicate_identity.item_album_id(source_item),
                        "release_relation":     release_relation,
                        "match_type":           match_type,
                        "confidence":           confidence,
                        "reason":               reason,
                        "fingerprint_verified": fingerprint_verified,
                        # Review evidence (unattended-cleanup proposal): the
                        # embedded IDs, what AcoustID actually heard, and the
                        # release slot of each copy.
                        "fingerprint_mbid":         fingerprint_mbid,
                        "source_fingerprint_ids":   list(src_fp_ids or []),
                        "lib_fingerprint_ids":      list(lib_fp_ids or []),
                        "source_recording_id":      _s(getattr(source_item, "mb_trackid", "") or "") if source_item is not None else "",
                        "lib_recording_id":         _s(getattr(lib_item, "mb_trackid", "") or ""),
                        "source_disc":              getattr(source_item, "disc", None) if source_item is not None else None,
                        "source_track":             getattr(source_item, "track", None) if source_item is not None else None,
                        "lib_disc":                 getattr(lib_item, "disc", None),
                        "lib_track":                getattr(lib_item, "track", None),
                        # Keeper-ranking facts (album row, IDs, format/quality).
                        "source_meta":              _duplicate_identity.copy_meta(source_item),
                        "lib_meta":                 _duplicate_identity.copy_meta(lib_item),
                    }
                    duplicates.append(dup)
                    state["found"] = len(duplicates)
                    state["duplicate_type"] = match_type
                    state["current_result"] = f"Scanning {i+1}/{total} ({round((i+1)/total*100) if total else 0}%) · {len(duplicates)} duplicate candidate(s) found"
                    if update_state:
                        update_state(_dedup_structured_state(state))

                    if release_relation in ("different_release", "different_position"):
                        evidence_tag = "REVIEW REQUIRED"
                    elif fingerprint_verified:
                        evidence_tag = "FINGERPRINT VERIFIED"
                    elif match_type == "identical file size":
                        evidence_tag = "BYTE VERIFIED"
                    else:
                        evidence_tag = "CANDIDATE"

                    state["log"].append(
                        f"  [{evidence_tag}] [{match_type}] {src.name}"
                        f"  →  {lib_item.artist or ''} – {lib_item.title or lib_path}"
                    )

        state["duplicates"] = duplicates

        # ── Group duplicates by source folder ──────────────────────────────
        folder_dups: dict  = defaultdict(list)
        folder_totals: dict = defaultdict(int)
        for sf in source_files:
            folder_totals[str(sf.parent)] += 1
        for dup in duplicates:
            folder_dups[str(Path(dup["source_path"]).parent)].append(dup)
        folders_out = []
        for fpath in sorted(folder_dups.keys()):
            fdups   = folder_dups[fpath]
            ftotal  = folder_totals.get(fpath, 0)
            folders_out.append({
                "path":        fpath,
                "name":        Path(fpath).name,
                "total_files": ftotal,
                "dup_count":   len(fdups),
                "all_dupes":   len(fdups) >= ftotal > 0,
                "files":       fdups,
            })
        state["folders"] = folders_out

        state["status"] = "done"
        state["final_summary"] = _dedup_final_summary(state, source_files=source_files)
        if update_state:
            update_state(_dedup_structured_state(
                state,
                current_task="Duplicate scan complete",
                current_item=None,
                current_path=None,
                current_result=(
                    f"{len(duplicates)} duplicate file(s) found across "
                    f"{len(folders_out)} folder(s)"
                ),
                final_summary=state["final_summary"],
            ))
        state["log"].append(
            f"Done — {total} file{'s' if total!=1 else ''} scanned, "
            f"{len(duplicates)} duplicate{'s' if len(duplicates)!=1 else ''} found "
            f"across {len(folders_out)} folder{'s' if len(folders_out)!=1 else ''}"
        )
        return _dedup_job_result("scan", state)

    job = jobs.start_python(
        _run,
        label=f"Duplicate scan: {scan_path}",
        metadata={"type": "dedup-scan", "path": str(scan_path)},
    )
    _dedup_scans[job.job_id] = state
    return {"ok": True, "job_id": job.job_id}, 200


# Service behind POST /api/dedup/cleanup (ARCH-001): request-free,
# returns (json_body, http_status); the route and in-process callers share it.
def run_dedup_cleanup(payload_in: Dict[str, Any]) -> Tuple[Any, int]:
    payload = payload_in
    paths = payload.get("paths", [])
    dry_run_raw = payload.get("dry_run", True)
    dry_run = str(dry_run_raw).strip().lower() not in {"0", "false", "no", "off"} if isinstance(dry_run_raw, str) else bool(dry_run_raw)
    root = str(payload.get("root") or payload.get("scan_path") or "").strip()
    if not isinstance(paths, list):
        return {"ok": False, "error": "paths must be a list"}, 400

    plan_payload = {
        "action": "dedup_cleanup",
        "paths": [_s(p) for p in paths],
        "requested_root": root,
    }
    try:
        plan_res = composite_workflows.plan_library_cleanup(plan_payload)
    except BeetsUnavailableError as ex:
        return {
            "ok": False,
            "error": "Beets engine is unavailable; duplicate cleanup was not performed.",
            "code": getattr(ex, "error_code", "") or "beets_unavailable",
            "dry_run": dry_run,
        }, 503
    except BeetsError as ex:
        return {
            "ok": False,
            "error": "Beets engine rejected duplicate cleanup planning.",
            "code": getattr(ex, "error_code", "") or "beets_error",
            "dry_run": dry_run,
        }, getattr(ex, "status_code", 400) or 400

    results = []
    for rec in plan_res.get("results") or []:
        out = dict(rec)
        out.setdefault("folders_removed", [])
        out["dry_run"] = dry_run
        out["deleted"] = bool(out.get("ok")) if dry_run else False
        results.append(out)

    planned = int(plan_res.get("planned_count") or 0)
    skipped = int(plan_res.get("skipped_count") or sum(1 for r in results if not r.get("ok")))
    if dry_run or planned <= 0:
        return {
            "ok": True,
            "results": results,
            "deleted": planned if dry_run else 0,
            "skipped": skipped,
            "folders_removed": 0,
            "dry_run": dry_run,
            "operation_id": plan_res.get("operation_id"),
            "plan": plan_res,
        }, 200

    op_id = _s(plan_res.get("operation_id")).strip()
    if not op_id:
        return {"ok": False, "error": "Engine did not return a cleanup operation_id", "results": results}, 502

    try:
        apply_res = composite_workflows.apply_library_cleanup(op_id)
    except BeetsUnavailableError as ex:
        return {
            "ok": False,
            "error": "Beets engine is unavailable; duplicate cleanup was not performed.",
            "code": getattr(ex, "error_code", "") or "beets_unavailable",
            "operation_id": op_id,
            "results": results,
            "dry_run": dry_run,
        }, 503
    except BeetsError as ex:
        return {
            "ok": False,
            "error": "Beets engine rejected duplicate cleanup apply.",
            "code": getattr(ex, "error_code", "") or "beets_error",
            "operation_id": op_id,
            "results": results,
            "dry_run": dry_run,
        }, getattr(ex, "status_code", 409) or 409

    def _cleanup_empty_parents(parent_folders: List[str]) -> List[str]:
        removed: List[str] = []
        seen: set = set()
        for folder_str in sorted({_s(p).strip() for p in parent_folders if _s(p).strip()}, key=len, reverse=True):
            current = Path(folder_str)
            for _ in range(12):
                key = _s(current)
                if not key or key in seen:
                    break
                seen.add(key)
                try:
                    folder_plan = composite_workflows.plan_folder_cleanup({"action": "remove_empty", "source": key})
                    if not folder_plan.get("ok") or int(folder_plan.get("removals_count") or 0) <= 0:
                        break
                    folder_op = _s(folder_plan.get("operation_id")).strip()
                    if not folder_op:
                        break
                    folder_apply = composite_workflows.apply_folder_cleanup(folder_op)
                    if not folder_apply.get("ok") or not folder_apply.get("mutated"):
                        break
                    removed_dirs = [_s(p) for p in folder_apply.get("removed_dirs") or [key] if _s(p)]
                    removed.extend(removed_dirs)
                    current = current.parent
                except (BeetsUnavailableError, BeetsError) as ex:
                    _app_logger.info("Engine empty-parent cleanup stopped at %s: %s", key, ex)
                    break
        return removed

    removed_folders = _cleanup_empty_parents(apply_res.get("candidate_parent_folders") or plan_res.get("candidate_parent_folders") or [])
    for rec in results:
        if rec.get("ok"):
            rec["deleted"] = True
            rec["quarantined"] = True
            rec["folders_removed"] = list(removed_folders)

    deleted = int(apply_res.get("quarantined_count") or apply_res.get("deleted_items") or planned)
    return {
        "ok": True,
        "results": results,
        "deleted": deleted,
        "skipped": skipped,
        "folders_removed": len(removed_folders),
        "dry_run": dry_run,
        "operation_id": op_id,
        "apply": apply_res,
    }, 200


# ── Clean: library database health ────────────────────────────────────────────

def _library_duplicate_merge_safety(rows: List[Any],
                                    items_by_album: Dict[int, List[Any]]) -> Dict[str, Any]:
    sorted_rows = sorted(
        rows,
        key=lambda r: (-int(r["track_count"] or 0), int(r["id"] or 0)),
    )
    target_id = int(sorted_rows[0]["id"]) if sorted_rows else 0
    source_ids = [int(r["id"]) for r in sorted_rows[1:]]
    mbids = [_s(r["mb_albumid"]).strip() for r in sorted_rows]
    def _safe_rgid(r: Any) -> str:
        try:
            return _s(r["mb_releasegroupid"]).strip()
        except (IndexError, KeyError):
            return ""
    rgids = [_safe_rgid(r) for r in sorted_rows]
    nonblank_mbids = {mbid for mbid in mbids if mbid}
    nonblank_rgids = {rgid for rgid in rgids if rgid}
    blockers: List[str] = []

    if not sorted_rows or not source_ids:
        blockers.append("not enough album rows to merge")
    # ARCH-009: Release Group ID is canonical album identity for a merge.
    # Every row sharing one non-blank RGID is the same album (different
    # release IDs are just editions). When any row lacks an RGID, release ID
    # may stand in only if every row carries the same concrete release --
    # one release belongs to exactly one release group. A row with an
    # unknown RGID never inherits another row's RGID.
    all_rgids_known = bool(rgids) and all(rgids)
    if len(nonblank_rgids) > 1:
        blockers.append(
            f"different MusicBrainz release-group IDs — these may be separate albums "
            f"({', '.join(sorted(nonblank_rgids)[:3])})"
        )
    elif all_rgids_known:
        pass  # one release group: canonical album identity established
    elif any(not mbid for mbid in mbids):
        blockers.append("missing MusicBrainz release ID")
    elif len(nonblank_mbids) > 1:
        blockers.append(
            "different MusicBrainz release IDs without a release-group ID on every row"
            if nonblank_rgids else "different MusicBrainz release IDs"
        )

    positions: Dict[Tuple[int, int], int] = {}
    unknown_position_albums: set[int] = set()
    duplicate_positions: set[Tuple[int, int, int]] = set()
    overlapping_positions: set[Tuple[int, int]] = set()

    for row in sorted_rows:
        aid = int(row["id"])
        seen_for_album: set[Tuple[int, int]] = set()
        for item in items_by_album.get(aid, []):
            disc = int(item["disc"] or 1)
            track = int(item["track"] or 0)
            if track <= 0:
                unknown_position_albums.add(aid)
                continue
            key = (disc, track)
            if key in seen_for_album:
                duplicate_positions.add((aid, disc, track))
            seen_for_album.add(key)
            previous_album = positions.get(key)
            if previous_album is not None and previous_album != aid:
                overlapping_positions.add(key)
            else:
                positions[key] = aid

    if unknown_position_albums:
        blockers.append("missing or zero track numbers")
    if duplicate_positions:
        blockers.append("duplicate track numbers within an album row")
    if overlapping_positions:
        blockers.append("overlapping disc/track numbers across album rows")

    merge_safe = not blockers
    # Check if same RGID but different release IDs (different editions — merge may be ok)
    different_release_ids_same_rgid = (
        len(nonblank_mbids) > 1 and len(nonblank_rgids) == 1
    )
    if merge_safe and different_release_ids_same_rgid:
        reason = (
            "Same MusicBrainz release group with complementary disc/track positions. "
            "Different release IDs suggest different editions — verify before merging."
        )
    elif merge_safe:
        reason = "Same MusicBrainz release and complementary disc/track positions."
    elif any("different MusicBrainz release-group IDs" in b for b in blockers):
        rgid_list = ", ".join(sorted(nonblank_rgids)[:3])
        reason = (
            f"Different MusicBrainz release-group IDs ({rgid_list}) — "
            "these are likely separate albums, not duplicates. Use 'Keep separate' to dismiss."
        )
    elif "different MusicBrainz release IDs" in blockers:
        reason = "Different MusicBrainz release IDs with no release-group to confirm they are the same album."
    elif "missing MusicBrainz release ID" in blockers:
        reason = "Missing MusicBrainz release IDs; manual review is required."
    elif "overlapping disc/track numbers across album rows" in blockers:
        reason = "Overlapping disc/track positions would create duplicate rows if merged."
    elif "duplicate track numbers within an album row" in blockers:
        reason = "Duplicate track positions already exist inside an album row."
    elif "missing or zero track numbers" in blockers:
        reason = "Track numbers are missing, so a safe DB-only merge cannot be proven."
    else:
        reason = "; ".join(blockers) or "Manual review is required."

    return {
        "merge_safe": merge_safe,
        "merge_target_album_id": target_id,
        "merge_source_album_ids": source_ids,
        "merge_reason": reason,
        "merge_blockers": blockers,
    }


def _resolver_rel_path(raw_path: str) -> str:
    text = _s(raw_path).strip().replace("\\", "/")
    root = str(MUSIC_ROOT).replace("\\", "/").rstrip("/")
    if text.startswith(root + "/"):
        text = text[len(root) + 1:]
    return text.strip("/")


def _resolver_parent(raw_path: str) -> str:
    rel = _resolver_rel_path(raw_path)
    if "/" not in rel:
        return ""
    return rel.rsplit("/", 1)[0]


def _resolver_sql_like(value: str) -> str:
    return (
        value
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _resolver_compact_track(track: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "disc": int(track.get("disc") or 1),
        "track": int(track.get("track") or 0),
        "title": _s(track.get("title") or ""),
        "mb_trackid": _s(track.get("mb_trackid") or "").strip().lower(),
    }


def _resolver_compact_item(item: Dict[str, Any], *,
                           selected_album_id: int,
                           selected_mbid: str,
                           matched_ids: set[int]) -> Dict[str, Any]:
    path = _s(item.get("path") or "")
    return {
        "id": int(item.get("id") or 0),
        "album_id": int(item.get("album_id") or 0),
        "album": _s(item.get("album") or ""),
        "albumartist": _s(item.get("albumartist") or ""),
        "disc": int(item.get("disc") or 1),
        "track": int(item.get("track") or 0),
        "title": _s(item.get("title") or ""),
        "path": path,
        "filename": Path(path).name,
        "mb_trackid": _s(item.get("mb_trackid") or "").strip().lower(),
        "mb_albumid": _s(item.get("mb_albumid") or "").strip().lower(),
        "length": float(item.get("length") or 0),
        "in_selected_album": int(item.get("album_id") or 0) == int(selected_album_id),
        "selected_release": _s(item.get("mb_albumid") or "").strip().lower() == selected_mbid,
        "matched_to_selected_release": int(item.get("id") or 0) in matched_ids,
    }


def _resolver_title_base(value: str) -> str:
    text = re.sub(r"\s*[\(\[].*?[\)\]]", " ", _s(value))
    return _album_track_norm(text)


def _resolver_title_score(source_title: str, target_title: str) -> float:
    source_full = _album_track_norm(source_title)
    target_full = _album_track_norm(target_title)
    source_base = _resolver_title_base(source_title)
    target_base = _resolver_title_base(target_title)
    scores = [
        difflib.SequenceMatcher(None, source_full, target_full).ratio()
        if source_full and target_full else 0.0,
        difflib.SequenceMatcher(None, source_base, target_base).ratio()
        if source_base and target_base else 0.0,
    ]
    if source_base and target_full and (
        source_base == target_full or target_full.startswith(source_base + " ")
    ):
        scores.append(0.94)
    if target_base and source_full and (
        target_base == source_full or source_full.startswith(target_base + " ")
    ):
        scores.append(0.90)
    return max(scores or [0.0])


def _resolver_retag_candidates(item: Dict[str, Any],
                               missing_tracks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for track in missing_tracks:
        score = _resolver_title_score(item.get("title", ""), track.get("title", ""))
        if score < 0.55:
            continue
        candidates.append({
            **_resolver_compact_track(track),
            "score": round(score, 3),
        })
    candidates.sort(key=lambda c: (-float(c.get("score") or 0), int(c.get("disc") or 1), int(c.get("track") or 0)))
    return candidates[:5]


def _album_duplicate_resolver_plan(album_id: int, mb_override: str = "",
                                   log: Optional[List[str]] = None) -> Dict[str, Any]:
    data = _album_mb_completeness(album_id, mb_override, log)
    selected_mbid = _s(data.get("mb_albumid") or mb_override).strip().lower()
    if not selected_mbid:
        raise RuntimeError("Album does not have a selected MusicBrainz release ID")

    matched_ids = {
        int((row.get("item") or {}).get("id") or 0)
        for row in data.get("tracks") or []
        if int((row.get("item") or {}).get("id") or 0) > 0
    }
    expected_by_mbid: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in data.get("tracks") or []:
        mbid = _s(row.get("mb_trackid") or "").strip().lower()
        if mbid:
            expected_by_mbid[mbid].append(row)
    missing_tracks = [
        _resolver_compact_track(row)
        for row in data.get("missing") or []
        if _s(row.get("mb_trackid") or "").strip()
    ]

    try:
        raw_items = composite_workflows.find_all_items_by_album_id(int(album_id))
        selected_rows = sorted(
            raw_items,
            key=lambda it: (
                int(it.get("disc") or 1),
                int(it.get("track") or 0),
                int(it.get("id") or 0),
            )
        )
    except Exception as ex:
        raise RuntimeError(f"Could not read selected album paths: {ex}") from ex

    prefixes = sorted({
        _resolver_parent(row["path"])
        for row in selected_rows
        if _resolver_parent(row["path"])
    })
    if not prefixes:
        return {
            "ok": True,
            "album_id": int(album_id),
            "mb_albumid": selected_mbid,
            "groups": [],
            "missing_tracks": missing_tracks,
            "message": "No album folder path was available for duplicate resolution.",
        }

    try:
        rows = composite_workflows.get_folder_items(prefixes[:30])
    except Exception as ex:
        raise RuntimeError(f"Could not read album-folder items: {ex}") from ex

    folder_items: List[Dict[str, Any]] = []
    for row in rows:
        folder_items.append({
            "id": int(row["id"]),
            "album_id": int(row["album_id"] or 0),
            "title": _s(row["title"]),
            "track": int(row["track"] or 0),
            "disc": int(row["disc"] or 1),
            "path": _s(row["path"]),
            "mb_trackid": _s(row["mb_trackid"]).strip().lower(),
            "mb_albumid": _s(row["mb_albumid"]).strip().lower(),
            "length": float(row["length"] or 0),
            "album": _s(row["album"]),
            "albumartist": _s(row["albumartist"]),
        })

    by_mbid: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in folder_items:
        mbid = _s(item.get("mb_trackid") or "").strip().lower()
        if mbid:
            by_mbid[mbid].append(item)

    groups: List[Dict[str, Any]] = []
    used_missing_targets: set[str] = set()
    for mbid, items in sorted(by_mbid.items()):
        expected_rows = expected_by_mbid.get(mbid, [])
        allowed_count = max(1, len(expected_rows))
        if len(items) <= allowed_count:
            continue
        sorted_items = sorted(
            items,
            key=lambda item: (
                0 if int(item.get("id") or 0) in matched_ids else 1,
                0 if _s(item.get("mb_albumid") or "").strip().lower() == selected_mbid else 1,
                int(item.get("album_id") or 0) != int(album_id),
                int(item.get("disc") or 1),
                int(item.get("track") or 0),
                int(item.get("id") or 0),
            ),
        )
        keep_ids = {
            int(item.get("id") or 0)
            for item in sorted_items
            if int(item.get("id") or 0) in matched_ids
        }
        if not keep_ids:
            selected_release_items = [
                item for item in sorted_items
                if _s(item.get("mb_albumid") or "").strip().lower() == selected_mbid
            ]
            keep_ids.add(int((selected_release_items or sorted_items)[0].get("id") or 0))

        action_items: List[Dict[str, Any]] = []
        for item in sorted_items:
            item_id = int(item.get("id") or 0)
            if item_id in keep_ids:
                continue
            compact = _resolver_compact_item(
                item,
                selected_album_id=int(album_id),
                selected_mbid=selected_mbid,
                matched_ids=matched_ids,
            )
            candidates = _resolver_retag_candidates(item, data.get("missing") or [])
            available_candidates = [
                cand for cand in candidates
                if _s(cand.get("mb_trackid") or "").strip().lower() not in used_missing_targets
            ]
            default_action = "delete"
            default_target = None
            if available_candidates and float(available_candidates[0].get("score") or 0) >= 0.78:
                default_action = "retag"
                default_target = available_candidates[0]
                used_missing_targets.add(_s(default_target.get("mb_trackid")).strip().lower())
            compact.update({
                "default_action": default_action,
                "default_target": default_target,
                "retag_candidates": candidates,
            })
            action_items.append(compact)

        groups.append({
            "key": mbid,
            "mb_trackid": mbid,
            "count": len(sorted_items),
            "duplicate_count": max(0, len(sorted_items) - allowed_count),
            "expected_count": len(expected_rows),
            "expected_tracks": [_resolver_compact_track(row) for row in expected_rows],
            "keep_items": [
                _resolver_compact_item(
                    item,
                    selected_album_id=int(album_id),
                    selected_mbid=selected_mbid,
                    matched_ids=matched_ids,
                )
                for item in sorted_items
                if int(item.get("id") or 0) in keep_ids
            ],
            "action_items": action_items,
        })

    return {
        "ok": True,
        "album_id": int(album_id),
        "mb_albumid": selected_mbid,
        "album": data.get("album", ""),
        "artist": data.get("artist", ""),
        "expected_count": data.get("expected_count", 0),
        "actual_count": len(folder_items),
        "missing_count": len(missing_tracks),
        "missing_tracks": missing_tracks,
        "groups": groups,
        "group_count": len(groups),
        "action_item_count": sum(len(group.get("action_items") or []) for group in groups),
    }


def _filename_cleanup_duplicate_quarantine_path(old_path: Path) -> Path:
    return _unique_import_review_cleanup_path(
        _import_review_cleanup_destination(
            Path("filename_cleanup") / uuid.uuid4().hex[:8] / old_path.name
        )
    )


def _maintenance_duplicate_report(log: List[str], progress: Optional[Any] = None) -> Dict[str, Any]:
    """Lightweight duplicate report for automatic maintenance.

    The manual Duplicate Check UI still owns the full file-by-file duplicate
    scan. The runner uses the DB-backed duplicate report so a recurring
    background pass cannot monopolize filesystem walking or duplicate cleanup.
    """
    health = _library_health_payload(progress=progress)
    duplicate_album_groups = int(health.get("duplicate_album_count") or 0)
    release_group_groups = int(health.get("rgid_duplicate_group_count") or 0)
    duplicate_candidates = duplicate_album_groups + release_group_groups
    result = {
        "kind": "maintenance_duplicate_report",
        "duplicate_candidates": duplicate_candidates,
        "duplicate_album_groups": duplicate_album_groups,
        "same_release_group_id_groups": release_group_groups,
        "file_duplicate_scan_started": False,
        "deleted_files": 0,
        "final_summary": {
            "duplicate_candidates": duplicate_candidates,
            "duplicate_album_groups": duplicate_album_groups,
            "same_release_group_id_groups": release_group_groups,
            "file_duplicate_scan_started": False,
            "deleted_files": 0,
        },
    }
    log.append(
        "[Duplicate Finder] lightweight report refreshed: "
        f"{duplicate_album_groups} duplicate album group(s), "
        f"{release_group_groups} same Release Group ID group(s); "
        "full file duplicate scan was not started."
    )
    _maintenance_save_last_report({"duplicates": result}, log)
    return result


def _maintenance_duplicate_plan(scan_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    # Rules live in backend/duplicate_identity.py (ARCH-009): audio proof
    # (fingerprint or identical bytes), same release slot, the best copy kept
    # by the keeper policy, and never an album slot left without a tracked item.
    return _duplicate_identity.plan_unattended_cleanup(
        scan_result, MUSIC_ROOT, _path_under, same_file=_maintenance_same_file_hash,
    )


def _maintenance_duplicate_cleanup_paths(scan_result: Dict[str, Any]) -> List[str]:
    return [d["delete"]["path"] for d in _maintenance_duplicate_plan(scan_result)]


def _file_size(path: str) -> Optional[int]:
    try:
        return Path(path).stat().st_size
    except OSError:
        return None


def _maintenance_duplicate_proposal(plan: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Review rows for the unattended plan: what would be deleted, what is
    kept and why, and the evidence (embedded IDs, fingerprint, release slot)."""

    def _copy(side: Dict[str, Any]) -> Dict[str, Any]:
        meta = side.get("meta") or {}
        return {
            "path": side.get("path"), "size": side.get("size") or _file_size(_s(side.get("path"))),
            "item_id": side.get("item_id"), "album_id": meta.get("album_id"),
            "recording_id": _s(meta.get("recording_id")), "disc": meta.get("disc"), "track": meta.get("track"),
            "format": _s(meta.get("format")), "bitrate": meta.get("bitrate"),
        }

    rows: List[Dict[str, Any]] = []
    for decision in plan:
        drop, keep = decision["delete"], decision["keep"]
        shared = _s(decision.get("shared_recording_id"))
        embedded = {_s((side.get("meta") or {}).get("recording_id")).lower() for side in (drop, keep)} - {""}
        rows.append({
            "delete": _copy(drop),
            "keep": _copy(keep),
            "keep_reason": decision.get("keep_reason") or "",
            "match_type": _s(decision.get("match_type")),
            "release_relation": _s(decision.get("release_relation")),
            "fingerprint": {
                "verified": bool(decision.get("fingerprint_verified")),
                "shared_recording_id": shared,
                "delete_copy_recording_ids": list(drop.get("fingerprint_ids") or []),
                "keep_copy_recording_ids": list(keep.get("fingerprint_ids") or []),
            },
            "byte_identical": bool(decision.get("byte_identical")),
            # True when an embedded Recording ID is not what AcoustID heard.
            "embedded_id_contradicts_fingerprint": bool(shared) and bool(embedded) and embedded != {shared.lower()},
        })
    return rows


def _maintenance_duplicate_proposal_line(row: Dict[str, Any]) -> str:
    d, k, fp = row["delete"], row["keep"], row["fingerprint"]
    return (
        f"[duplicates] PROPOSED delete {d['path']} ({d['size']} bytes, item {d['item_id']}, "
        f"disc {d['disc']} track {d['track']}, embedded {d['recording_id'] or '-'}) "
        f"-- keep {k['path']} ({k['size']} bytes, item {k['item_id']}, embedded {k['recording_id'] or '-'}); "
        f"slot {row['release_relation']}; fingerprint shared {fp['shared_recording_id'] or '-'}; "
        f"kept because: {row['keep_reason']}"
        f"{' (embedded ID contradicts fingerprint)' if row['embedded_id_contradicts_fingerprint'] else ''}"
    )


def _maintenance_full_duplicate_scan(log: List[str], cancel_event: Optional[Any] = None,
                                     progress: Optional[Any] = None) -> Dict[str, Any]:
    if _running_job_of_type({"dedup-scan", "dedup-ai-review", "dedup-cleanup"}):
        result = {
            "ok": True,
            "skipped": True,
            "reason": "Duplicate scan already running",
            "final_summary": {
                "file_duplicate_scan_started": False,
                "duplicate_tracks_found": 0,
                "deleted_files": 0,
                "skipped_candidates": 0,
            },
        }
        log.append("[duplicates] skipped: duplicate scan already running.")
        return result

    log.append(f"[duplicates] Starting full duplicate scan under {MUSIC_ROOT}")
    # Only Beets-tracked pairs can ever be selected for unattended deletion,
    # so the scheduled step checks tracked library files, not every file on disk.
    child_id = _maintenance_extract_child_job_id(start_dedup_scan({"path": str(MUSIC_ROOT), "tracked_only": True}))
    scan_result = _wait_for_child_job(
        child_id,
        log,
        cancel_event,
        prefix="duplicates",
        timeout=0,
        idle_timeout=1800,
        progress=progress,
    )
    if not isinstance(scan_result, dict):
        scan_result = {}
    duplicates = scan_result.get("duplicates") or []
    plan = _maintenance_duplicate_plan(scan_result)
    cleanup_paths = [d["delete"]["path"] for d in plan]
    skipped_candidates = max(0, len(duplicates) - len(cleanup_paths))
    proposal = _maintenance_duplicate_proposal(plan)
    # Deleting without review needs an explicit operator authorization that
    # is independent of MUSIC_ROOT or any other configuration.
    authorized = _dedup_authorization.unattended_delete_enabled(WEB_MANAGER_DATA_DIR)
    for row in proposal:
        log.append(_maintenance_duplicate_proposal_line(row))
    cleanup_result: Dict[str, Any] = {
        "ok": True,
        "deleted": 0,
        "skipped": 0,
        "folders_removed": 0,
        "results": [],
    }
    if cleanup_paths and not authorized:
        log.append(
            f"[duplicates] Unattended deletion is disabled: {len(cleanup_paths)} audio-proven duplicate(s) "
            f"proposed for review, nothing deleted; {skipped_candidates} other candidate(s) left for review."
        )
    elif cleanup_paths:
        log.append(
            f"[duplicates] Resolving {len(cleanup_paths)} verified duplicate recording(s); "
            f"{skipped_candidates} candidate(s) left for review."
        )
        cleanup_response = run_dedup_cleanup({"paths": cleanup_paths, "dry_run": False, "root": str(MUSIC_ROOT)},
        )
        cleanup_result = _json_from_flask_response(cleanup_response)
    else:
        log.append(
            f"[duplicates] Found {len(duplicates)} duplicate candidate(s); "
            "none met automatic deletion rules."
        )

    deleted = int(cleanup_result.get("deleted") or 0)
    db_rows_removed = sum(
        int((row or {}).get("db_rows_removed") or 0)
        for row in cleanup_result.get("results") or []
        if isinstance(row, dict)
    )
    final_summary = {
        "file_duplicate_scan_started": True,
        "scanned_files": int(scan_result.get("scanned") or scan_result.get("total") or 0),
        "duplicate_tracks_found": len(duplicates),
        "auto_selected": len(cleanup_paths),
        "unattended_delete_enabled": authorized,
        "proposed_deletions": len(cleanup_paths),
        "deleted_files": deleted,
        "db_rows_removed": db_rows_removed,
        "folders_removed": int(cleanup_result.get("folders_removed") or 0),
        "skipped_candidates": skipped_candidates + int(cleanup_result.get("skipped") or 0),
    }
    result = {
        "ok": True,
        "kind": "maintenance_full_duplicate_scan",
        "scan_job_id": child_id,
        "scan": scan_result,
        "cleanup": cleanup_result,
        "proposal": proposal,
        "final_summary": final_summary,
    }
    log.append(
        "[duplicates] Done: "
        f"{final_summary['duplicate_tracks_found']} candidate(s), "
        f"{final_summary['deleted_files']} duplicate file(s) removed, "
        f"{final_summary['skipped_candidates']} left for review."
    )
    _maintenance_save_last_report({"duplicates": result}, log)
    return result


def _album_cleanup_count_duplicate_files(records: List[Dict[str, Any]], canonical_path: str) -> int:
    return len(_album_cleanup_merge_plan(records, canonical_path).get("duplicate_files_to_quarantine") or [])
