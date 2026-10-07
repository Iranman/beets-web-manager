"""Acquisition: wanted tracks, download methods and album downloads (ARCH-001).
"""

from __future__ import annotations

import backend.provider_boundary as provider_boundary
import copy, difflib, json, re, time
import urllib.error
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import backend.job_contract as job_contract
from backend.app_runtime import _app_logger, AUDIO_EXT, DOWNLOADS_ROOT, LIDARR_KEY, LIDARR_URL, MUSIC_ROOT, QBIT_CATEGORY, QBIT_FILTER, QBIT_PASSWORD, QBIT_PATH_ALIASES, QBIT_REPAIR_ALLOWED_ROOTS, QBIT_URL, QBIT_USERNAME, TORRENT_SOURCE_ROOTS, _MB_UUID_RE, _s, _ur
from backend.ytdlp_service import _audio_files_in_dir, _download_method_label, _spotiflac_album_download, _spotiflac_missing_tracks_download, _ytdlp_album_download, _ytdlp_missing_tracks_download
from backend.ai_service import _ai_match_evidence_packet, _validate_import_source_audio
from backend.import_service import _delete_staged_import_folder, _stage_selected_audio_files, _start_reimport_disk_job_internal, _wanted_tracks_not_in_album
from backend.library_service import _album_folder_for_album_id, _build_folder_evidence, _representative_tracktotal, _resolve_album_release_for_import, get_library_payload
from backend.pending_review_store import _queue_folder_for_manual_review
from backend.playlist_service import _playlist_download_audio_allowed, _playlist_download_match, _playlist_filter_preview_downloads, _playlist_identity_log, _playlist_stamp_download_tags
from backend.app_runtime import _path_under
from helpers_mb import _resolve_release_group_to_release
import backend.composite_workflows as composite_workflows
from backend.acoustid_service import _album_track_norm
from backend.slskd_service import _find_slskd_downloaded_files, _normalise_download_method, _normalise_wanted_tracks, _slskd_cancel_queued_downloads, _slskd_cleanup_failed_candidate_files, _slskd_fallback_methods, _slskd_file_wanted_match_score, _slskd_search_and_queue, _slskd_wait_downloads, _wanted_track_key, _wanted_track_label
from backend.matching_service import _album_key, _album_mb_completeness, _artist_folder_name_without_mbid, _fetch_mb_release_tracklist, _folder_release_preflight, _normalize_album
from backend.app_runtime import jobs
from backend.job_service import _wait_for_child_job
from backend.serializers import _json_from_flask_response

# ── ARCH-001 extracted code ──


def _wanted_tracks_satisfied_by_names(names: List[Any],
                                     wanted_tracks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    wanted_tracks = _normalise_wanted_tracks(wanted_tracks)
    if not names or not wanted_tracks:
        return []
    best_by_key: Dict[tuple, Dict[str, Any]] = {}
    for raw in names:
        match = _slskd_file_wanted_match_score(_s(raw), wanted_tracks)
        if not match.get("ok"):
            continue
        track = match.get("track") or {}
        key = match.get("key") or _wanted_track_key(track)
        current = best_by_key.get(key)
        if current and float(current.get("score") or 0) >= float(match.get("score") or 0):
            continue
        best_by_key[key] = {
            "track": track,
            "score": float(match.get("score") or 0),
        }
    wanted_keys = {_wanted_track_key(t): t for t in wanted_tracks}
    return [
        wanted_keys[key]
        for key in wanted_keys
        if key in best_by_key
    ]


_DOWNLOAD_METHODS = {"slskd", "spotiflac", "ytdlp", "soundcloud"}


def _download_method_job_label(method: Any) -> str:
    method = _normalise_download_method(method)
    return {
        "slskd": "Soulseek",
        "ytdlp": "YouTube",
        "soundcloud": "SoundCloud",
        "spotiflac": "SpotiFLAC",
    }.get(method, method)


# Service behind POST /api/download/album (ARCH-001): request-free,
# returns (json_body, http_status); the route and in-process callers share it.
def start_album_download(payload_in: Dict[str, Any]) -> Tuple[Any, int]:
    """Start a background job to download an album via slskd (primary)
    or yt-dlp (fallback), then import/tag/move it unless auto_import=false."""
    data        = payload_in
    artist      = (data.get("artist")      or "").strip()
    album       = (data.get("album")       or "").strip()
    year        = str(data.get("year")     or "").strip()
    track_count = int(data.get("track_count") or 0)
    # A release group (e.g. a Lidarr wanted row) is passed as such and
    # resolved to a concrete release below -- never used as a release ID.
    mb_albumid  = ((data.get("mb_albumid") or "").strip()
                   or _acq_release_group_url(data.get("mb_releasegroupid")))
    method     = _normalise_download_method(data.get("method") or "slskd")
    auto_import = bool(data.get("auto_import", True))
    forced_albumartist = _artist_folder_name_without_mbid(data.get("albumartist") or artist).strip()
    raw_fallback_method = data.get("fallback_method")
    fallback_method = _normalise_download_method(raw_fallback_method or "spotiflac", "spotiflac")
    source_fallback_methods = _slskd_fallback_methods(fallback_method)
    try_source_fallback = (
        (raw_fallback_method is not None and fallback_method in {"soundcloud", "spotiflac"})
        or bool(data.get("try_ytdlp_fallback"))
        or bool(data.get("ytdlp_fallback"))
        or bool(data.get("try_source_fallback"))
    )
    wanted_tracks = _normalise_wanted_tracks(
        data.get("missing_tracks") or data.get("wanted_tracks") or [])
    replace_existing_item_ids = []
    for raw_id in (data.get("replace_existing_item_ids") or []):
        try:
            item_id = int(raw_id or 0)
            if item_id > 0:
                replace_existing_item_ids.append(item_id)
        except Exception:
            pass
    replace_existing = bool(data.get("replace_existing") or replace_existing_item_ids)
    try:
        existing_album_id = int(data.get("existing_album_id") or data.get("album_id") or 0)
    except Exception:
        existing_album_id = 0
    # method: "slskd" | "soundcloud" | "spotiflac";
    # slskd can optionally fall back through non-YouTube direct sources.

    if not artist or not album:
        return {"ok": False, "error": "artist and album required"}, 200
    if method not in _DOWNLOAD_METHODS:
        return {
            "ok": False,
            "error": (
                f"Unknown method {method!r}; use 'slskd', 'spotiflac', "
                "'ytdlp', or 'soundcloud'"
            ),
        }, 200

    def _do(log, cancel_event=None):
        _safe = lambda s: re.sub(r'[\\/:*?"<>|]', '_', str(s)).strip()
        yr_sfx   = f" ({year})" if year else ""
        dest_dir = str(DOWNLOADS_ROOT / _safe(artist) / (_safe(album) + yr_sfx))
        download_result: Dict[str, Any]
        resolved_mbid = mb_albumid
        effective_track_count = track_count
        job_wanted_tracks = list(wanted_tracks)
        supplied_mbid_match = re.search(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            mb_albumid,
            re.I,
        )
        supplied_mbid = supplied_mbid_match.group(0).lower() if supplied_mbid_match else ""
        release_group_requested = "release-group" in _s(mb_albumid).casefold()
        resolved_release_group_id = ""
        if auto_import or mb_albumid:
            log.append("[import] Resolving MusicBrainz release before download…")
            resolved_mbid = _resolve_album_release_for_import(
                mb_albumid, artist, album, year, track_count, log)
            if resolved_mbid:
                if supplied_mbid and supplied_mbid != _s(resolved_mbid).strip().lower():
                    release_group_requested = True
                    resolved_release_group_id = supplied_mbid
                mb_release = _fetch_mb_release_tracklist(resolved_mbid, log)
                mb_tracks = len(mb_release.get("tracks") or []) if mb_release.get("ok") else 0
                if mb_release.get("release_group"):
                    resolved_release_group_id = _s(mb_release.get("release_group")).strip().lower()
                if mb_tracks and (not effective_track_count or effective_track_count != mb_tracks):
                    log.append(f"  MusicBrainz track count: {mb_tracks}")
                    effective_track_count = mb_tracks
            elif auto_import:
                raise RuntimeError("Could not resolve a MusicBrainz release ID before download")

        if existing_album_id and resolved_mbid:
            if replace_existing and job_wanted_tracks:
                log.append(
                    f"[import] Replacement mode: {len(job_wanted_tracks)} track(s) "
                    f"will be downloaded for existing album_id {existing_album_id}"
                )
            else:
                job_wanted_tracks = _wanted_tracks_not_in_album(
                    existing_album_id, resolved_mbid, job_wanted_tracks, log)
            if job_wanted_tracks:
                log.append(
                    f"[import] Missing-track mode: {len(job_wanted_tracks)} track(s) "
                    f"will be downloaded for existing album_id {existing_album_id}"
                )
            elif existing_album_id:
                review_reason = (
                    "Requested missing track(s) are not missing for the selected "
                    "MusicBrainz release. Verify the release edition before downloading."
                )
                log.append(
                    "[import] No requested tracks are still missing for this selected "
                    "MusicBrainz release; skipping download"
                )
                album_folder = _album_folder_for_album_id(existing_album_id)
                if album_folder:
                    try:
                        comp = _album_mb_completeness(existing_album_id, resolved_mbid, log)
                    except Exception:
                        comp = {}
                    requested = _normalise_wanted_tracks(wanted_tracks)
                    currently_missing = _normalise_wanted_tracks((comp or {}).get("missing") or [])
                    suggestion = {
                        "albumartist": artist,
                        "album": album,
                        "year": year,
                        "mb_albumid": resolved_mbid,
                        "mb_url": f"https://musicbrainz.org/release/{resolved_mbid}",
                        "mb_valid": bool(_MB_UUID_RE.match(_s(resolved_mbid).strip().lower())),
                        "confidence": "low",
                        "reason": review_reason,
                    }
                    evidence = _ai_match_evidence_packet(
                        "missing_track",
                        folder_path=album_folder,
                        suggestion=suggestion,
                        folder_evidence=_build_folder_evidence(album_folder),
                        preflight=_folder_release_preflight(
                            album_folder,
                            resolved_mbid,
                            existing_album_id=existing_album_id,
                            log=None,
                        ),
                        wanted_tracks=requested,
                        reason=review_reason,
                    )
                    evidence["requested_tracks"] = requested
                    evidence["currently_missing_tracks"] = currently_missing
                    if _queue_folder_for_manual_review(
                        album_folder,
                        suggestion,
                        review_reason,
                        log,
                        allow_existing=True,
                        evidence=evidence,
                    ):
                        log.append(
                            "[import] Queued existing album for Review with requested-track evidence."
                        )
                return {
                    "aldir": "",
                    "method": method,
                    "files": 0,
                    "mb_albumid": resolved_mbid,
                    "imported": True,
                    "already_complete": True,
                    "existing_album_id": existing_album_id,
                }

        expected_download_count = len(job_wanted_tracks) if job_wanted_tracks else effective_track_count

        def _maybe_switch_release_for_slskd_candidate(candidate_dir: str,
                                                      candidate_count: int) -> int:
            nonlocal resolved_mbid, effective_track_count, expected_download_count
            if (
                job_wanted_tracks
                or not release_group_requested
                or not resolved_release_group_id
                or not candidate_count
                or candidate_count == effective_track_count
            ):
                return 0
            if effective_track_count:
                min_viable = max(1, min(effective_track_count, int(effective_track_count * 0.70)))
                if candidate_count < min_viable:
                    return 0
            replacement = _resolve_release_group_to_release(
                resolved_release_group_id,
                log,
                year=year,
                track_count=candidate_count,
            )
            replacement = _s(replacement).strip().lower()
            if not replacement or replacement == _s(resolved_mbid).strip().lower():
                return 0
            replacement_mb = _fetch_mb_release_tracklist(replacement, log)
            replacement_count = len(replacement_mb.get("tracks") or []) if replacement_mb.get("ok") else 0
            if replacement_count != candidate_count:
                log.append(
                    "  [slskd] Same-release-group candidate did not match downloaded "
                    f"file count: MB has {replacement_count or '?'} track(s), "
                    f"download has {candidate_count}."
                )
                return 0
            preflight = _folder_release_preflight(
                candidate_dir,
                replacement,
                existing_album_id=existing_album_id,
                log=None,
            )
            if not preflight.get("ok"):
                log.append(
                    "  [slskd] Same-release-group candidate rejected by downloaded "
                    f"folder tracklist: {preflight.get('matches', 0)}/"
                    f"{preflight.get('expected', 0)} track(s) matched."
                )
                return 0
            old_mbid = _s(resolved_mbid).strip().lower()
            old_count = int(effective_track_count or 0)
            resolved_mbid = replacement
            effective_track_count = replacement_count
            expected_download_count = replacement_count
            log.append(
                "  [slskd] Downloaded folder has "
                f"{candidate_count} file(s); switched MusicBrainz release within "
                f"release-group {resolved_release_group_id}: "
                f"{old_mbid or '?'} ({old_count or '?'} tracks) -> "
                f"{replacement} ({replacement_count} tracks)."
            )
            return replacement_count

        def _import_downloaded_album(
            import_dir: str,
            import_wanted_tracks: Optional[List[Dict[str, Any]]] = None,
        ) -> Dict[str, Any]:
            if not resolved_mbid:
                raise RuntimeError("Could not resolve a MusicBrainz release ID for import")
            active_wanted_tracks = (
                _normalise_wanted_tracks(import_wanted_tracks)
                if import_wanted_tracks is not None
                else job_wanted_tracks
            )
            if active_wanted_tracks:
                _validate_wanted_download_identity_before_import(
                    import_dir,
                    active_wanted_tracks,
                    log,
                    fallback_artist=forced_albumartist or artist,
                )
            _validate_import_source_audio(import_dir, log, reject_downloads=True)
            log.append(f"[import] Starting Beets import/tag/move for {import_dir}")
            import_job_id = _start_reimport_disk_job_internal(
                import_dir, resolved_mbid, albumartist=forced_albumartist,
                existing_album_id=existing_album_id,
                wanted_tracks=active_wanted_tracks,
                replace_existing_item_ids=replace_existing_item_ids)
            log.append(f"[import] Job {import_job_id} started; waiting for completion...")
            import_result = _wait_for_child_job(import_job_id, log, cancel_event,
                                                prefix="import", timeout=1200)
            log.append("[import] Complete — files imported, tagged, and moved into the music folder")
            return {
                "imported": True,
                "import_job_id": import_job_id,
                "import_result": import_result,
                "existing_album_id": existing_album_id,
                "missing_track_count": len(active_wanted_tracks),
            }

        def _direct_dest_dir(selection_tracks: Optional[List[Dict[str, Any]]] = None,
                             direct_method: Optional[str] = None) -> str:
            source_method = _normalise_download_method(direct_method or method, "ytdlp")
            active_tracks = (
                _normalise_wanted_tracks(selection_tracks)
                if selection_tracks is not None
                else job_wanted_tracks
            )
            if active_tracks:
                suffix = str(int(time.time() * 1000))
                source_slug = re.sub(r"[^a-z0-9]+", "", source_method) or "download"
                return str(
                    DOWNLOADS_ROOT
                    / _safe(artist)
                    / f"{_safe(album)}{yr_sfx} - {source_slug} missing {suffix}"
                )
            return dest_dir

        def _download_ytdlp_selection(
            selection_tracks: Optional[List[Dict[str, Any]]] = None,
            direct_method: Optional[str] = None,
        ) -> Dict[str, Any]:
            source_method = _normalise_download_method(direct_method or method, "ytdlp")
            active_tracks = (
                _normalise_wanted_tracks(selection_tracks)
                if selection_tracks is not None
                else job_wanted_tracks
            )
            yt_dir = _direct_dest_dir(active_tracks, source_method)
            log.append(f"  Downloading via {_download_method_label(source_method)} → {yt_dir}")
            if active_tracks:
                if source_method == "spotiflac":
                    n = _spotiflac_missing_tracks_download(
                        artist, album, year, yt_dir, log, active_tracks)
                else:
                    n = _ytdlp_missing_tracks_download(
                        artist, album, year, yt_dir, log, active_tracks,
                        source=source_method)
            else:
                if source_method == "spotiflac":
                    n = _spotiflac_album_download(
                        artist, album, year, yt_dir, log,
                        track_count=effective_track_count,
                        mb_albumid=resolved_mbid)
                else:
                    n = _ytdlp_album_download(
                        artist, album, year, yt_dir, log,
                        track_count=effective_track_count,
                        source=source_method)
            return {
                "aldir": yt_dir,
                "method": source_method,
                "files": n,
                "mb_albumid": resolved_mbid,
            }

        def _try_direct_sources_after_slskd(slskd_error: Exception,
                                            selection_tracks: Optional[List[Dict[str, Any]]] = None,
                                            fallback_from: str = "slskd") -> Dict[str, Any]:
            active_tracks = (
                _normalise_wanted_tracks(selection_tracks)
                if selection_tracks is not None
                else job_wanted_tracks
            )
            if not try_source_fallback:
                raise slskd_error
            fallback_tracks = active_tracks or None
            fallback_scope = (
                f"{len(active_tracks)} missing track(s)"
                if active_tracks
                else "the full album"
            )
            methods = list(source_fallback_methods)
            failures: List[str] = []
            log.append(
                f"[fallback] SLSKD could not provide a valid candidate for "
                f"{fallback_scope}; trying direct sources: "
                + ", ".join(_download_method_label(method) for method in methods)
            )
            for fallback_source in methods:
                fallback_result = None
                try:
                    log.append(
                        f"[fallback] Trying {_download_method_label(fallback_source)} "
                        f"for {fallback_scope}."
                    )
                    fallback_result = _download_ytdlp_selection(
                        fallback_tracks, direct_method=fallback_source)
                    fallback_result["fallback_from"] = fallback_from
                    fallback_result["slskd_error"] = str(slskd_error)
                    if auto_import:
                        fallback_result.update(
                            _import_downloaded_album(
                                fallback_result["aldir"], fallback_tracks))
                    return fallback_result
                except Exception as direct_ex:
                    failures.append(
                        f"{_download_method_label(fallback_source)}: {direct_ex}"
                    )
                    log.append(
                        f"[fallback] {_download_method_label(fallback_source)} failed: "
                        f"{direct_ex}"
                    )
                    if fallback_result:
                        _delete_staged_import_folder(
                            fallback_result.get("aldir", ""), log
                        )
            raise RuntimeError(
                "SLSKD download failed; direct-source fallback also failed: "
                + "; ".join(failures)
            ) from slskd_error

        if method == "slskd":
            # ── Soulseek via slskd ───────────────────────────────────────────
            max_slskd_attempts = 12

            def _skip_key(username: str, remote_dir: str) -> tuple:
                return (
                    _s(username).strip().lower(),
                    _s(remote_dir).replace("\\", "/").strip().lower(),
                )

            def _download_slskd_selection(
                selection_tracks: List[Dict[str, Any]],
                expected_count: int,
                skip_candidates: Optional[set] = None,
            ) -> Dict[str, Any]:
                slskd_skip_candidates = skip_candidates if skip_candidates is not None else set()
                last_slskd_error: Optional[Exception] = None
                for attempt in range(1, max_slskd_attempts + 1):
                    username = ""
                    remote_dir = ""
                    queued: List[str] = []
                    try:
                        if attempt > 1:
                            log.append(
                                f"  [slskd] Trying another Soulseek candidate "
                                f"({attempt}/{max_slskd_attempts})..."
                            )
                        username, queued, expected, remote_dir = _slskd_search_and_queue(
                            artist, album, year, log, track_count=effective_track_count,
                            wanted_tracks=selection_tracks,
                            skip_candidates=slskd_skip_candidates)
                        matched_tracks = _wanted_tracks_satisfied_by_names(
                            queued, selection_tracks)
                        download_target_tracks = (
                            matched_tracks if selection_tracks and matched_tracks
                            else _normalise_wanted_tracks(selection_tracks)
                        )
                        download_expected_count = (
                            len(download_target_tracks)
                            if selection_tracks and download_target_tracks
                            else expected_count
                        )
                        aldir, transfer_hints = _slskd_wait_downloads(username, queued, log, timeout=600)
                        time.sleep(3)
                        aldir, afiles = _find_slskd_downloaded_files(
                            username, queued, expected or aldir, log,
                            artist=artist, album=album, track_count=download_expected_count,
                            transfer_hints=transfer_hints,
                            wanted_tracks=download_target_tracks)
                        if not afiles:
                            raise RuntimeError(
                                f"No queued Soulseek files found at {aldir} after download; "
                                "refusing unrelated fallback folders"
                            )
                        if (
                            not selection_tracks
                            and download_expected_count
                            and len(afiles) != download_expected_count
                        ):
                            switched_count = _maybe_switch_release_for_slskd_candidate(
                                aldir,
                                len(afiles),
                            )
                            if switched_count:
                                download_expected_count = switched_count
                        if download_expected_count:
                            required_files = max(1, download_expected_count)
                            if len(afiles) < required_files:
                                raise RuntimeError(
                                    f"Downloaded candidate has only {len(afiles)}/{download_expected_count} "
                                    "expected files; refusing incomplete import"
                                )
                        log.append(f"  [slskd] OK {len(afiles)} file(s) in {aldir}")
                        log.append(
                            "  [slskd] Download phase complete for this candidate; "
                            "no additional Soulseek sources will be queued unless "
                            "import validation fails."
                        )
                        return {
                            "aldir": aldir,
                            "files": afiles,
                            "queued": queued,
                            "username": username,
                            "remote_dir": remote_dir,
                            "wanted_tracks": download_target_tracks,
                        }
                    except Exception as ex:
                        last_slskd_error = ex
                        if username or remote_dir:
                            if queued:
                                log.append("  [slskd] Candidate failed after queueing; cancelling it before retrying.")
                                _slskd_cancel_queued_downloads(username, queued, log)
                                _slskd_cleanup_failed_candidate_files(username, queued, log)
                            slskd_skip_candidates.add(_skip_key(username, remote_dir))
                            if attempt < max_slskd_attempts:
                                log.append(f"  [slskd] Candidate failed: {ex}")
                                log.append(
                                    "  [slskd] Skipping that peer/folder and continuing automatically."
                                )
                                continue
                        if slskd_skip_candidates:
                            raise RuntimeError(
                                f"SLSKD could not find a usable candidate after "
                                f"{len(slskd_skip_candidates)} failed candidate(s): {ex}"
                            ) from ex
                        raise
                raise RuntimeError(
                    f"SLSKD could not find a usable candidate after {max_slskd_attempts} attempts"
                    + (f": {last_slskd_error}" if last_slskd_error else "")
                )

            if job_wanted_tracks:
                log.append(
                    f"[import] SLSKD batch mode: {len(job_wanted_tracks)} "
                    "requested missing track(s) will be queued together"
                )
            else:
                log.append("[import] SLSKD batch mode: album tracks will be queued together")
            slskd_skip_candidates: set = set()
            if auto_import:
                last_import_error: Optional[Exception] = None
                for import_attempt in range(1, max_slskd_attempts + 1):
                    try:
                        dl = _download_slskd_selection(
                            job_wanted_tracks,
                            expected_download_count,
                            skip_candidates=slskd_skip_candidates,
                        )
                    except Exception as ex:
                        return _try_direct_sources_after_slskd(ex)
                    aldir = dl["aldir"]
                    afiles = dl["files"]
                    dl_wanted_tracks = _normalise_wanted_tracks(
                        dl.get("wanted_tracks") or job_wanted_tracks)
                    import_dir = _stage_selected_audio_files(
                        aldir, afiles, artist, album, log,
                        target_tracks=dl_wanted_tracks or None,
                    )
                    try:
                        download_result = {
                            "aldir": import_dir,
                            "method": "slskd",
                            "files": len(afiles),
                            "mb_albumid": resolved_mbid,
                            "username": dl.get("username", ""),
                            "remote_dir": dl.get("remote_dir", ""),
                        }
                        download_result.update(
                            _import_downloaded_album(import_dir, dl_wanted_tracks))
                    except Exception as ex:
                        last_import_error = ex
                        username = _s(dl.get("username", ""))
                        queued = list(dl.get("queued") or [])
                        remote_dir = _s(dl.get("remote_dir", ""))
                        log.append(
                            "  [slskd] Downloaded candidate failed import validation: "
                            f"{ex}"
                        )
                        if queued:
                            log.append(
                                "  [slskd] Cleaning failed candidate before trying another source."
                            )
                            _slskd_cancel_queued_downloads(username, queued, log)
                            _slskd_cleanup_failed_candidate_files(username, queued, log)
                        _delete_staged_import_folder(import_dir, log)
                        if username or remote_dir:
                            slskd_skip_candidates.add(_skip_key(username, remote_dir))
                        if import_attempt < max_slskd_attempts:
                            log.append(
                                "  [slskd] Trying the next peer/folder because the "
                                "downloaded files did not validate."
                            )
                            continue
                        break
                    if job_wanted_tracks and dl_wanted_tracks and len(dl_wanted_tracks) < len(job_wanted_tracks):
                        remaining_after_slskd = _wanted_tracks_not_in_album(
                            existing_album_id, resolved_mbid, job_wanted_tracks, log)
                        if remaining_after_slskd:
                            remaining_desc = ", ".join(
                                _wanted_track_label(t) for t in remaining_after_slskd[:8]
                            )
                            if not try_source_fallback:
                                raise RuntimeError(
                                    "SLSKD imported "
                                    f"{len(dl_wanted_tracks)}/{len(job_wanted_tracks)} "
                                    "requested missing track(s), but these remain: "
                                    f"{remaining_desc}"
                                )
                            log.append(
                                "[fallback] SLSKD satisfied "
                                f"{len(dl_wanted_tracks)}/{len(job_wanted_tracks)} "
                                "requested missing track(s); trying direct sources for remaining: "
                                f"{remaining_desc}"
                            )
                            try:
                                fallback_result = _try_direct_sources_after_slskd(
                                    RuntimeError(
                                        "partial SLSKD candidate left unresolved track(s): "
                                        f"{remaining_desc}"
                                    ),
                                    remaining_after_slskd,
                                    fallback_from="partial_slskd",
                                )
                            except Exception as direct_ex:
                                raise RuntimeError(
                                    "SLSKD imported "
                                    f"{len(dl_wanted_tracks)}/{len(job_wanted_tracks)} "
                                    "requested missing track(s), but direct-source fallback for the "
                                    f"remaining track(s) failed: {direct_ex}"
                                ) from direct_ex
                            combined = dict(download_result)
                            combined["method"] = f"slskd+{fallback_result.get('method') or 'fallback'}"
                            combined["files"] = int(download_result.get("files") or 0) + int(fallback_result.get("files") or 0)
                            combined["fallback_result"] = fallback_result
                            return combined
                    return download_result
                slskd_error = RuntimeError(
                    f"SLSKD could not import a valid candidate after "
                    f"{len(slskd_skip_candidates)} failed candidate(s)"
                    + (f": {last_import_error}" if last_import_error else "")
                )
                return _try_direct_sources_after_slskd(slskd_error)

            try:
                dl = _download_slskd_selection(
                    job_wanted_tracks,
                    expected_download_count,
                    skip_candidates=slskd_skip_candidates,
                )
            except Exception as ex:
                return _try_direct_sources_after_slskd(ex)
            aldir = dl["aldir"]
            afiles = dl["files"]
            download_result = {"aldir": aldir, "method": "slskd",
                               "files": len(afiles), "mb_albumid": resolved_mbid}

        else:
            # ── Direct non-SLSKD source ──────────────────────────────────────
            download_result = _download_ytdlp_selection(direct_method=method)

        if not auto_import:
            return download_result

        aldir = download_result["aldir"]
        download_result["mb_albumid"] = resolved_mbid
        download_result.update(_import_downloaded_album(aldir))
        return download_result

    label = f"{_download_method_job_label(method)} Import: {artist} – {album}"
    # One download+import per album at a time, across processes and restarts.
    workflow = "download-import-" + job_contract.slug(f"{artist}|{album}|{mb_albumid}")
    job = jobs.start_python(job_contract.guarded(_do, workflow=workflow, fail_fast_in_process=True), label=label,
                            metadata={"type": "download-import", "mutating": True, "artist": artist,
                                      "album": album, "year": year,
                                      "track_count": track_count,
                                      "mb_albumid": mb_albumid, "method": method,
                                      "albumartist": forced_albumartist,
                                      "existing_album_id": existing_album_id,
                                      "missing_track_count": len(wanted_tracks),
                                      "try_source_fallback": try_source_fallback,
                                      "try_ytdlp_fallback": try_source_fallback,
                                      "fallback_methods": source_fallback_methods})
    return {"ok": True, "job_id": job.job_id}, 200


def _validate_wanted_download_identity_before_import(import_dir: str,
                                                     wanted_tracks: List[Dict[str, Any]],
                                                     log: list,
                                                     fallback_artist: str = "") -> List[str]:
    """Require downloaded wanted-track files to fingerprint-match before import."""
    wanted = _normalise_wanted_tracks(wanted_tracks)
    if not wanted:
        return []
    audio_files = sorted(_audio_files_in_dir(import_dir))
    if not audio_files:
        raise RuntimeError(
            "No downloaded audio files found for requested track verification"
        )

    accepted: List[str] = []
    failures: List[str] = []
    remaining = list(wanted)

    def _identity_log(message: str) -> None:
        line = f"  [identity] {message}"
        if hasattr(log, "append"):
            log.append(line)
        elif callable(log):
            log(line)

    for path_value in _playlist_filter_preview_downloads(audio_files, _identity_log):
        best_idx = -1
        best_ok_score = -1.0
        best_any_match: Optional[Dict[str, Any]] = None
        best_any_score = -1.0
        for idx, track in enumerate(remaining):
            title = _s(track.get("title") or "").strip()
            if not title:
                continue
            track_artist = _s(track.get("artist") or fallback_artist or "").strip()
            match = _playlist_download_match(
                path_value,
                track_artist,
                title,
                _s(track.get("mb_trackid") or ""),
            )
            identity = match.get("identity") if isinstance(match.get("identity"), dict) else {}
            score = (
                float(match.get("title_score") or 0)
                + float(match.get("artist_score") or 0)
                + float(identity.get("acoustid_match_score") or 0)
            )
            if score > best_any_score:
                best_any_score = score
                best_any_match = match
            if match.get("ok") and score > best_ok_score:
                best_ok_score = score
                best_idx = idx

        if best_idx < 0:
            identity = (
                best_any_match.get("identity")
                if isinstance(best_any_match, dict) and isinstance(best_any_match.get("identity"), dict)
                else {}
            )
            if _s(identity.get("final_action") or "review") == "reject":
                try:
                    composite_workflows.delete_file(path_value)
                except Exception:
                    pass
            detail = _playlist_identity_log(best_any_match) if best_any_match else "no identity evidence"
            failures.append(f"{Path(path_value).name} ({detail})")
            _identity_log(
                "rejected unverified requested-track download "
                f"({detail}): {Path(path_value).name}"
            )
            continue

        track = remaining.pop(best_idx)
        title = _s(track.get("title") or "").strip()
        track_artist = _s(track.get("artist") or fallback_artist or "").strip()
        if not _playlist_download_audio_allowed(path_value, _identity_log):
            failures.append(f"{Path(path_value).name} (format rejected)")
            continue
        _playlist_stamp_download_tags(path_value, track_artist, title, _identity_log)
        match = _playlist_download_match(
            path_value,
            track_artist,
            title,
            _s(track.get("mb_trackid") or ""),
        )
        accepted.append(path_value)
        _identity_log(
            "accepted fingerprint-verified requested-track download "
            f"({_playlist_identity_log(match)}): {Path(path_value).name}"
        )

    if failures or remaining:
        missing = ", ".join(_wanted_track_label(track) for track in remaining[:5])
        details = "; ".join(failures[:5])
        raise RuntimeError(
            "Downloaded audio did not fingerprint-verify as requested MusicBrainz track(s)"
            + (f": {missing}" if missing else "")
            + (f" ({details})" if details else "")
        )
    return accepted


def _deno_download_url() -> str:
    return ""


# ── Acquisition queue ─────────────────────────────────────────────────────────

def _acq_text_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _s(value).lower())


def _acq_display_year(value: Any) -> str:
    text = _s(value).strip()
    return text[:4] if text else ""


def _acq_release_text_key(artist: Any, album: Any, year: Any = "") -> str:
    return "|".join([
        _acq_text_key(artist),
        _acq_text_key(album),
        _acq_display_year(year),
    ])


def _acq_release_identity(artist: Any, album: Any, year: Any, mbid: Any, rgid: Any = "") -> str:
    """Queue identity: the release group (canonical) when known, else the
    release, else text. A Lidarr wanted row only ever has a release group."""
    clean_rgid = _s(rgid).strip().lower()
    if clean_rgid:
        return f"rg:{clean_rgid}"
    clean_mbid = _s(mbid).strip().lower()
    if clean_mbid:
        return f"mb:{clean_mbid}"
    return f"title:{_acq_release_text_key(artist, album, year)}"


def _acq_release_group_url(rgid: Any) -> str:
    """A release-group reference the download path resolves to a concrete
    release (``_resolve_album_release_for_import``); never a release ID."""
    clean = _s(rgid).strip().lower()
    return f"https://musicbrainz.org/release-group/{clean}" if clean else ""


def _acq_album_has_no_track_rows(album: Dict[str, Any]) -> bool:
    if album.get("tracks_deferred"):
        return False
    tracks = album.get("tracks")
    if tracks is None:
        return False
    return int(album.get("track_count") or 0) > 0 and not tracks


def _acq_expected_track_count(album: Dict[str, Any]) -> int:
    explicit = int(album.get("expected_track_count") or 0)
    if explicit > 0:
        return explicit
    per_disc: Dict[int, List[int]] = {}
    for track in album.get("tracks") or []:
        try:
            total = int(track.get("tracktotal") or 0)
            disc = int(track.get("disc") or 1)
        except Exception:
            continue
        if total > 0 and total < 300:
            per_disc.setdefault(disc, []).append(total)
    return sum(_representative_tracktotal(values) for values in per_disc.values()) if per_disc else 0


def _acq_imported_count(album: Dict[str, Any]) -> int:
    if _acq_album_has_no_track_rows(album):
        return 0
    tracks = album.get("tracks") or []
    if tracks:
        return sum(1 for track in tracks if track.get("ok") and not track.get("missing"))
    return max(
        0,
        int(album.get("track_count") or 0)
        - int(album.get("missing") or 0)
        - int(album.get("not_imported") or 0),
    )


def _acq_track_missing_count(album: Dict[str, Any]) -> int:
    if album.get("tracks_deferred"):
        return 0
    return sum(1 for track in album.get("tracks") or [] if track.get("missing"))


def _acq_is_extra_only_complete(album: Dict[str, Any]) -> bool:
    expected = _acq_expected_track_count(album)
    imported = _acq_imported_count(album)
    return (
        int(album.get("extra_track_count") or 0) > 0
        and expected > 0
        and imported >= expected
        and int(album.get("missing") or 0) <= 0
        and _acq_track_missing_count(album) <= 0
        and int(album.get("not_imported") or 0) <= 0
    )


def _acq_is_leftover_only_complete(album: Dict[str, Any]) -> bool:
    expected = _acq_expected_track_count(album)
    imported = _acq_imported_count(album)
    return (
        int(album.get("not_imported") or 0) > 0
        and expected > 0
        and imported >= expected
        and int(album.get("missing") or 0) <= 0
        and _acq_track_missing_count(album) <= 0
    )


def _acq_missing_count(album: Dict[str, Any]) -> int:
    if _acq_album_has_no_track_rows(album):
        return int(album.get("track_count") or 0)
    flagged = int(album.get("missing") or 0)
    track_missing = _acq_track_missing_count(album)
    if _acq_is_extra_only_complete(album) or _acq_is_leftover_only_complete(album):
        return 0
    mb_missing_raw = album.get("mb_missing_count", -1)
    try:
        mb_missing = int(mb_missing_raw)
    except Exception:
        mb_missing = -1
    expected = _acq_expected_track_count(album)
    derived = max(0, expected - _acq_imported_count(album)) if expected > 0 else 0
    return max(
        flagged,
        mb_missing if mb_missing >= 0 else 0,
        track_missing,
        derived,
    )


def _acq_health(album: Dict[str, Any]) -> Dict[str, Any]:
    imported = _acq_imported_count(album)
    missing = _acq_missing_count(album)
    not_imported = int(album.get("not_imported") or 0)
    expected = _acq_expected_track_count(album)
    total = max(expected, int(album.get("track_count") or 0), imported + missing + not_imported, 1)
    track_mb_missing = int(album.get("mb_trackid_missing_count") or 0)
    track_mb_mismatch = int(album.get("mb_trackid_mismatch_count") or 0)
    duplicate_recordings = int(album.get("mb_duplicate_recording_id_count") or 0)
    mb_repairable = int(album.get("mb_repairable_count") or 0)
    extra_tracks = int(album.get("extra_track_count") or 0)
    extra_only_complete = _acq_is_extra_only_complete(album)
    leftover_only_complete = _acq_is_leftover_only_complete(album)
    release_mb_missing = not bool(_s(album.get("mb_albumid") or "").strip())
    label = "Complete"
    color = "success"
    if missing > 0:
        label, color = "Missing files", "error"
    elif leftover_only_complete:
        label, color = "Leftover files", "warning"
    elif not_imported > 0:
        label, color = "Partial import", "warning"
    elif extra_only_complete:
        label, color = "Extra tracks", "warning"
    elif (
        release_mb_missing
        or track_mb_missing > 0
        or track_mb_mismatch > 0
        or duplicate_recordings > 0
        or mb_repairable > 0
    ):
        label, color = "Needs MB review", "secondary"
    return {
        "imported": imported,
        "missing": missing,
        "not_imported": not_imported,
        "expected": expected,
        "percent": max(0, min(100, round(imported / total * 100))),
        "label": label,
        "color": color,
        "release_mb_missing": release_mb_missing,
        "track_mb_missing": track_mb_missing,
        "track_mb_mismatch": track_mb_mismatch,
        "duplicate_recording_ids": duplicate_recordings,
        "mb_repairable": mb_repairable,
        "extra_tracks": extra_tracks,
        "extra_only_complete": extra_only_complete,
        "leftover_only_complete": leftover_only_complete,
    }


def _acq_needs_acquisition(album: Dict[str, Any],
                           health: Optional[Dict[str, Any]] = None) -> bool:
    health = health or _acq_health(album)
    if health.get("extra_only_complete") or health.get("leftover_only_complete"):
        return False
    return health["missing"] > 0 or health["not_imported"] > 0 or _acq_album_has_no_track_rows(album)


def _acq_locally_satisfies_wanted(album: Dict[str, Any],
                                  health: Optional[Dict[str, Any]] = None) -> bool:
    health = health or _acq_health(album)
    if _acq_album_has_no_track_rows(album):
        return False
    if int(health.get("missing") or 0) > 0:
        return False
    if int(health.get("not_imported") or 0) > 0 and not health.get("leftover_only_complete"):
        return False
    imported = int(health.get("imported") or _acq_imported_count(album))
    expected = int(health.get("expected") or _acq_expected_track_count(album))
    return imported > 0 and (expected <= 0 or imported >= expected)


def _acq_local_issue(album: Dict[str, Any], health: Dict[str, Any]) -> str:
    if _acq_album_has_no_track_rows(album):
        return f"{int(album.get('track_count') or 0) or 'Unknown'} track row(s) need import"
    if health["missing"] > 0:
        return f"{health['missing']} missing track(s)"
    if health["not_imported"] > 0:
        return f"{health['not_imported']} file(s) on disk but not imported"
    return _s(health.get("label") or "Needs acquisition")


_LIDARR_WANTED_MAX_PAGES = 200  # 20,000 wanted albums; a paging bug must not loop forever


def _lidarr_wanted_error(exc: BaseException) -> str:
    """A fixed, user-facing reason (IA-08); never the provider's body."""
    err = provider_boundary.classify_exception(exc)
    if err.outcome == provider_boundary.ProviderOutcome.AUTHENTICATION_ERROR:
        return "Lidarr rejected the API key"
    if err.outcome == provider_boundary.ProviderOutcome.RATE_LIMITED:
        return "Lidarr is rate limiting requests"
    if isinstance(exc, (ValueError, TypeError, KeyError, AttributeError)):
        return "Lidarr returned an unexpected response"
    if err.status_code is not None and err.status_code < 500:
        return f"Lidarr refused the request (HTTP {err.status_code})"
    return "Could not reach Lidarr"


def _acq_fetch_lidarr_wanted() -> Tuple[List[Dict[str, Any]], str]:
    """Every wanted/missing album, or ([], reason) -- never a partial list
    presented as complete. ``foreignAlbumId`` is a MusicBrainz release
    group, so it is reported as ``mb_releasegroupid`` (IA-07)."""
    if not LIDARR_KEY:
        return [], "LIDARR_API_KEY not configured"
    results: List[Dict[str, Any]] = []
    page = 1
    try:
        while True:
            url = (
                f"{LIDARR_URL}/api/v1/wanted/missing?page={page}"
                "&pageSize=100&sortKey=releaseDate&sortDirection=descending"
            )
            req = _ur.Request(url, headers={"X-Api-Key": LIDARR_KEY})
            with provider_boundary.opened("lidarr", req, timeout=10) as r:
                data = json.loads(r.read())
            if not isinstance(data, dict) or not isinstance(data.get("records", []), list):
                raise ValueError("unexpected wanted/missing payload")
            records = data.get("records", [])
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                artist_obj = rec.get("artist") if isinstance(rec.get("artist"), dict) else {}
                rgid = _s(rec.get("foreignAlbumId") or "").strip().lower()
                if rgid and not _MB_UUID_RE.match(rgid):
                    rgid = ""
                try:
                    lidarr_id = int(rec.get("id") or 0)
                except (TypeError, ValueError):
                    lidarr_id = 0
                results.append({
                    "artist": _s(artist_obj.get("artistName") or ""),
                    "album": _s(rec.get("title") or ""),
                    "year": _s(rec.get("releaseDate") or "")[:4],
                    "type": _s(rec.get("albumType") or ""),
                    "lidarr_id": lidarr_id,
                    "mb_albumid": "",
                    "mb_releasegroupid": rgid,
                    "mb_url": _acq_release_group_url(rgid),
                    "monitored": rec.get("monitored", True) is not False,
                })
            if len(records) < 100:
                break
            page += 1
            if page > _LIDARR_WANTED_MAX_PAGES:
                raise ValueError("wanted/missing paging did not end")
        return results, ""
    except Exception as exc:
        reason = _lidarr_wanted_error(exc)
        _app_logger.warning("Lidarr wanted-list fetch failed: %s (%s)", reason, type(exc).__name__)
        return [], reason


def _acq_item_mbid(item: Dict[str, Any]) -> str:
    local = item.get("local") or {}
    wanted = item.get("wanted") or {}
    return _s(item.get("mbid") or local.get("mb_albumid")
              or _acq_release_group_url(wanted.get("mb_releasegroupid")) or "")


def _acq_can_import_disk(item: Dict[str, Any]) -> bool:
    local = item.get("local") or {}
    health = local.get("health") or {}
    if health.get("leftover_only_complete"):
        return False
    not_imported = int(health.get("not_imported") or 0)
    return bool(local.get("aldir") and local.get("album_id") and _acq_item_mbid(item) and not_imported)


def _acq_can_download(item: Dict[str, Any]) -> bool:
    mbid = _acq_item_mbid(item)
    if not mbid:
        return False
    local = item.get("local") or {}
    if not local:
        return True
    health = local.get("health") or {}
    missing = int(health.get("missing") or 0)
    not_imported = int(health.get("not_imported") or 0)
    if missing > 0:
        return True
    if not_imported > 0:
        return False
    return True


def _acq_action_flags(item: Dict[str, Any]) -> Dict[str, Any]:
    local = item.get("local") or {}
    wanted = item.get("wanted") or {}
    mbid = _acq_item_mbid(item)
    # Acquire is a download surface. Disk import/metadata repair rows belong in Review or Library cleanup.
    can_import_disk = False
    can_download = _acq_can_download(item)
    health = local.get("health") or {}
    not_imported = int(health.get("not_imported") or 0)
    missing = int(health.get("missing") or 0)
    if can_download:
        recommended = "slskd"
    elif local and not_imported > 0 and missing <= 0:
        recommended = "review"
    else:
        recommended = "review"
    return {
        "can_download": can_download,
        "can_ytdlp": can_download,
        "can_import_disk": can_import_disk,
        "can_search_lidarr": bool(wanted.get("lidarr_id")),
        "recommended": recommended,
    }


def _build_acquisition_queue_payload(force: bool = False) -> Dict[str, Any]:
    # Acquisition always includes disk-only folders (as GET /api/library did
    # for /api/acquisition requests); `force` is the caller's ?refresh=1.
    library_payload = get_library_payload(force=force, include_disk_only=True)
    library_error = "" if library_payload.get("ok") else _s(library_payload.get("error") or "")
    wanted_rows, wanted_error = _acq_fetch_lidarr_wanted()
    by_identity: Dict[str, Dict[str, Any]] = {}
    by_text: Dict[str, Dict[str, Any]] = {}
    complete_local_by_identity: Dict[str, Dict[str, Any]] = {}
    complete_local_by_text: Dict[str, Dict[str, Any]] = {}

    def _text_key(artist: Any, album: Any) -> str:
        # IA-07: no year. Lidarr's year is the release group's first release;
        # the local year is the edition's. Only used when either side has no
        # release group ID.
        return _acq_release_text_key(artist, album)

    def _text_fallback(table: Dict[str, Dict[str, Any]], key: str, rgid: str) -> Optional[Dict[str, Any]]:
        found = table.get(key)
        if not found:
            return None
        found_rgid = _s(found.get("mb_releasegroupid")
                        or (found.get("local") or {}).get("mb_releasegroupid") or "").strip().lower()
        # Two different release groups that share a title are different albums.
        return None if (rgid and found_rgid and found_rgid != rgid) else found

    def _add(item: Dict[str, Any]) -> None:
        by_identity[item["key"]] = item
        by_text[_text_key(item.get("artist"), item.get("album"))] = item

    for artist in library_payload.get("artists") or []:
        for album in artist.get("albums") or []:
            artist_name = _s(album.get("albumartist") or artist.get("name") or "Unknown artist")
            album_name = _s(album.get("album") or "Untitled album")
            year = _acq_display_year(album.get("year"))
            health = _acq_health(album)
            identity_key = _acq_release_identity(artist_name, album_name, year, album.get("mb_albumid") or "",
                                                 album.get("mb_releasegroupid") or "")
            text_key = _text_key(artist_name, album_name)
            if not _acq_needs_acquisition(album, health):
                if _acq_locally_satisfies_wanted(album, health):
                    complete_local_by_identity[identity_key] = album
                    complete_local_by_text[text_key] = album
                continue
            local = {
                "album_id": int(album.get("album_id") or 0),
                "aldir": _s(album.get("aldir") or ""),
                "track_count": int(album.get("track_count") or 0),
                "expected_track_count": _acq_expected_track_count(album),
                "albumtype": _s(album.get("albumtype") or ""),
                "mb_albumid": _s(album.get("mb_albumid") or ""),
                "mb_releasegroupid": _s(album.get("mb_releasegroupid") or "").strip().lower(),
                "health": health,
            }
            item = {
                "key": identity_key,
                "sort_key": f"{_acq_text_key(artist_name)}|{year}|{_acq_text_key(album_name)}",
                "artist": artist_name,
                "album": album_name,
                "year": year,
                "mbid": _s(album.get("mb_albumid") or ""),
                "issue": _acq_local_issue(album, health),
                "local": local,
                "wanted": None,
                "sources": ["beets"],
            }
            item["actions"] = _acq_action_flags(item)
            _add(item)

    for wanted in wanted_rows:
        artist_name = _s(wanted.get("artist") or "Unknown artist")
        album_name = _s(wanted.get("album") or "Untitled album")
        year = _acq_display_year(wanted.get("year"))
        rgid = _s(wanted.get("mb_releasegroupid") or "").strip().lower()
        identity = _acq_release_identity(artist_name, album_name, year, "", rgid)
        fallback = _text_key(artist_name, album_name)
        existing = by_identity.get(identity) or _text_fallback(by_text, fallback, rgid)
        if existing:
            existing["wanted"] = wanted
            existing["mbid"] = existing.get("mbid") or _acq_release_group_url(rgid)
            if "lidarr" not in existing["sources"]:
                existing["sources"].append("lidarr")
            existing["actions"] = _acq_action_flags(existing)
            continue
        if complete_local_by_identity.get(identity) or _text_fallback(complete_local_by_text, fallback, rgid):
            continue
        item = {
            "key": identity,
            "sort_key": f"{_acq_text_key(artist_name)}|{year}|{_acq_text_key(album_name)}",
            "artist": artist_name,
            "album": album_name,
            "year": year,
            "mbid": _acq_release_group_url(rgid),
            "issue": "Wanted in Lidarr" if wanted.get("monitored", True) else "Unmonitored in Lidarr",
            "local": None,
            "wanted": wanted,
            "sources": ["lidarr"],
        }
        item["actions"] = _acq_action_flags(item)
        _add(item)

    items = sorted(
        [row for row in by_identity.values() if (row.get("actions") or {}).get("can_download")],
        key=lambda row: row.get("sort_key") or "",
    )
    return {
        "ok": True,
        "items": items,
        "total": len(items),
        "counts": {
            "beets": sum(1 for row in items if "beets" in row.get("sources", [])),
            "lidarr": sum(
                1 for row in items
                if "lidarr" in row.get("sources", []) and (row.get("wanted") or {}).get("monitored", True)
            ),
            "merged": sum(1 for row in items if len(row.get("sources", [])) > 1),
            "unmonitored": sum(
                1 for row in items
                if "lidarr" in row.get("sources", []) and not (row.get("wanted") or {}).get("monitored", True)
            ),
        },
        "library_error": library_error,
        "wanted_error": wanted_error,
        "library_version": library_payload.get("library_version"),
    }


_ACQ_DOWNLOAD_ALL_LAST_FILE = Path("/config/acquisition_download_all_last.json")


def _acq_download_payload(item: Dict[str, Any], method: str,
                          try_source_fallback: bool) -> Dict[str, Any]:
    local = item.get("local") or {}
    mbid = _s(item.get("mbid") or local.get("mb_albumid") or "")
    existing_album_id = int(local.get("album_id") or 0)
    payload: Dict[str, Any] = {
        "artist": _s(item.get("artist") or ""),
        "albumartist": _s(item.get("artist") or ""),
        "album": _s(item.get("album") or ""),
        "year": _s(item.get("year") or ""),
        "mb_albumid": mbid,
        "existing_album_id": existing_album_id,
        "method": method,
        "auto_import": True,
    }
    track_count = int(local.get("expected_track_count") or local.get("track_count") or 0)
    if track_count:
        payload["track_count"] = track_count
    if method == "slskd" and try_source_fallback:
        payload["fallback_method"] = "spotiflac"
        payload["try_ytdlp_fallback"] = True
        payload["try_source_fallback"] = True
    return payload


def _acq_start_download_job(payload: Dict[str, Any]) -> str:
    resp = start_album_download(payload)
    data = _json_from_flask_response(resp)
    if not data.get("ok") or not data.get("job_id"):
        raise RuntimeError(data.get("error") or "failed to start download job")
    return _s(data["job_id"])


def _acq_start_import_disk_job(item: Dict[str, Any]) -> str:
    if not _acq_can_import_disk(item):
        raise RuntimeError("Acquire row cannot be imported from disk")
    local = item.get("local") or {}
    aldir = _s(local.get("aldir") or "")
    mbid = _acq_item_mbid(item)
    existing_album_id = int(local.get("album_id") or 0)
    return _start_reimport_disk_job_internal(
        aldir,
        mbid,
        albumartist=_s(item.get("artist") or ""),
        existing_album_id=existing_album_id,
    )


def _acq_trim_batch_log(log: List[str], keep: int = 1200) -> None:
    if len(log) <= keep:
        return
    marker = f"[older acquisition batch log trimmed: kept last {keep} line(s)]"
    del log[:-keep]
    if not log or log[0] != marker:
        log.insert(0, marker)


def _acq_download_all_record(label: str,
                             metadata: Dict[str, Any],
                             result: Dict[str, Any],
                             log: List[str],
                             *,
                             status: str,
                             job_id: str = "",
                             error: str = "") -> Dict[str, Any]:
    record = {
        "job_id": job_id,
        "label": label,
        "status": status,
        "created_at": time.time(),
        "finished_at": time.time(),
        "metadata": dict(metadata or {}),
        "result": copy.deepcopy(result or {}),
        "log": list(log[-200:]),
        "log_lines": len(log),
        "returncode": 0 if status == "success" else 1,
    }
    if error:
        record["error"] = error
    return record


def _save_acq_download_all_last(record: Dict[str, Any]) -> None:
    try:
        _ACQ_DOWNLOAD_ALL_LAST_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _ACQ_DOWNLOAD_ALL_LAST_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_ACQ_DOWNLOAD_ALL_LAST_FILE)
    except Exception:
        pass


def _load_acq_download_all_last() -> Optional[Dict[str, Any]]:
    try:
        if not _ACQ_DOWNLOAD_ALL_LAST_FILE.exists():
            return None
        data = json.loads(_ACQ_DOWNLOAD_ALL_LAST_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _qbit_status_payload() -> Dict[str, Any]:
    return {
        "ok": True,
        "configured": bool(QBIT_URL),
        "url": QBIT_URL,
        "username_configured": bool(QBIT_USERNAME),
        "password_configured": bool(QBIT_PASSWORD),
        "category": QBIT_CATEGORY,
        "filter": QBIT_FILTER,
        "path_aliases": QBIT_PATH_ALIASES,
        "repair_allowed_roots": [str(p) for p in QBIT_REPAIR_ALLOWED_ROOTS],
        "torrent_source_roots": [str(p) for p in TORRENT_SOURCE_ROOTS],
    }


def _qbit_login_cookie() -> str:
    if not QBIT_URL:
        raise RuntimeError(
            "qBittorrent is not configured. Set QBITTORRENT_URL or QBIT_URL "
            "in the Beets container."
        )
    if not QBIT_URL.startswith(("http://", "https://")):
        raise RuntimeError(f"qBittorrent URL must include http:// or https://: {QBIT_URL}")
    if not (QBIT_USERNAME and QBIT_PASSWORD):
        return ""
    data = urllib.parse.urlencode({
        "username": QBIT_USERNAME,
        "password": QBIT_PASSWORD,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{QBIT_URL}/api/v2/auth/login",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with provider_boundary.opened("qbittorrent", req, timeout=20) as resp:
        status = int(getattr(resp, "status", 200) or 200)
        body = resp.read().decode("utf-8", errors="replace").strip()
        cookie = resp.headers.get("Set-Cookie", "")
        if body != "Ok." and not (status == 204 and cookie):
            raise RuntimeError(f"qBittorrent login failed: {body or 'empty response'}")
    return cookie.split(";", 1)[0].strip()


def _qbit_request_json(api_path: str, params: Optional[Dict[str, Any]] = None,
                       cookie: str = "") -> Any:
    query = urllib.parse.urlencode(params or {})
    url = f"{QBIT_URL}{api_path}" + (f"?{query}" if query else "")
    headers = {"Cookie": cookie} if cookie else {}
    req = urllib.request.Request(url, headers=headers)
    with provider_boundary.opened("qbittorrent", req, timeout=30) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return json.loads(body) if body.strip() else None


def _qbit_post(api_path: str, data: Dict[str, Any], cookie: str = "") -> str:
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(f"{QBIT_URL}{api_path}", data=encoded, headers=headers)
    with provider_boundary.opened("qbittorrent", req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _qbit_path_alias_pairs() -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    for token in QBIT_PATH_ALIASES.split(","):
        if "=" not in token:
            continue
        src, dst = token.split("=", 1)
        src = src.strip().replace("\\", "/").rstrip("/")
        dst = dst.strip().replace("\\", "/").rstrip("/")
        if src and dst:
            pairs.append((src, dst))
    return sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)


def _qbit_map_path(path_value: str) -> Optional[Path]:
    """Map a qBittorrent-reported path through configured aliases.

    Returns None for blank, NUL-containing, or otherwise unmappable input --
    never Path(""), which stringifies to "." and would silently resolve to
    the process's current working directory in any downstream containment
    check or filesystem call.
    """
    text = _s(path_value).strip().replace("\\", "/")
    if not text or "\x00" in text:
        return None
    for src, dst in _qbit_path_alias_pairs():
        if text == src or text.startswith(src + "/"):
            text = dst + text[len(src):]
            break
    return Path(text) if text else None


def _qbit_allowed_repair_path(path: Path) -> bool:
    if not QBIT_REPAIR_ALLOWED_ROOTS:
        return False
    # Uses _path_under() (not the older _path_is_under()) deliberately: this
    # gate authorizes where a qBittorrent-derived path may be written, so it
    # must use the hardened containment check, not the pattern CodeQL flags
    # as an unrecognized path-injection sanitizer (see #253/#254).
    return any(_path_under(path, root) for root in QBIT_REPAIR_ALLOWED_ROOTS)


def _qbit_candidate_target_paths(torrent: Dict[str, Any],
                                 file_row: Dict[str, Any]) -> List[Path]:
    rel_name = _s(file_row.get("name")).strip().replace("\\", "/").lstrip("/")
    if not rel_name:
        return []
    rel = Path(rel_name)
    content_raw = _s(torrent.get("content_path")).strip()
    save_raw = _s(torrent.get("save_path")).strip()
    content = _qbit_map_path(content_raw) if content_raw else None
    save = _qbit_map_path(save_raw) if save_raw else None
    candidates: List[Path] = []

    if content:
        if content.suffix.lower() in AUDIO_EXT:
            candidates.append(content)
        elif rel.parts and content.name.casefold() == rel.parts[0].casefold():
            if save:
                candidates.append(save / rel)
            candidates.append(content.parent / rel)
        else:
            candidates.append(content / rel)
    if save:
        candidates.append(save / rel)
    if content and content.suffix.lower() in AUDIO_EXT:
        candidates.append(content)

    out: List[Path] = []
    seen: set = set()
    for candidate in candidates:
        text = str(candidate)
        if text in seen:
            continue
        seen.add(text)
        out.append(candidate)
    return out


def _library_file_candidates_for_qbit(file_name: str, size: int) -> List[Dict[str, Any]]:
    basename = Path(file_name).name
    if not basename or size <= 0:
        return []
    rows: List[Dict[str, Any]] = []
    try:
        rows = composite_workflows.find_files_for_hardlink(filename=basename, limit=100)
    except Exception:
        return []

    matches: List[Dict[str, Any]] = []
    seen_paths: set = set()
    for row in rows:
        raw = _s(row.get("path") if isinstance(row, dict) else row["path"]).strip()
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = MUSIC_ROOT / raw
        try:
            # The qBittorrent hardlink-repair source becomes the file the
            # engine links from; a DB row is normally library-authored, but
            # this must not be assumed -- require the resolved path to
            # actually be inside MUSIC_ROOT before it is ever eligible as a
            # hardlink source.
            if not _path_under(path, MUSIC_ROOT):
                continue
            if not path.exists() or not path.is_file():
                continue
            if path.stat().st_size != size:
                continue
            resolved = str(path.resolve(strict=False))
        except Exception:
            continue
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        matches.append({
            "id": int(row["id"]),
            "path": str(path),
            "title": _s(row["title"]),
            "artist": _s(row["artist"]),
            "album": _s(row["album"]),
            "size": size,
            "match_strategy": "filename_size",
        })
    return matches


_QBIT_SCENE_META_TOKENS = {
    "16BIT", "24BIT", "320", "CD", "CDS", "DIGITAL", "FLAC", "MP3", "M4A",
    "PROPER", "REPACK", "REMASTERED", "RETAIL", "VINYL", "WEB",
}


def _qbit_scene_text(value: str) -> str:
    text = _s(value).strip()
    text = text.replace("_", " ").replace(".", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _qbit_torrent_scene_artist_slug(torrent_name: str) -> str:
    first = _s(torrent_name).split("-", 1)[0]
    return re.sub(r"[^a-z0-9]+", "_", first.casefold()).strip("_")


def _qbit_torrent_album_guess(torrent_name: str) -> str:
    parts = [_s(p).strip() for p in _s(torrent_name).split("-") if _s(p).strip()]
    if len(parts) < 2:
        return ""
    album_parts: List[str] = []
    for part in parts[1:]:
        token = re.sub(r"[^A-Za-z0-9]+", "", part).upper()
        if token in _QBIT_SCENE_META_TOKENS or re.fullmatch(r"(?:19|20)\d{2}", token or ""):
            break
        album_parts.append(part)
    return _qbit_scene_text(" ".join(album_parts))


def _qbit_track_guess_from_scene_file(file_name: str, torrent_name: str) -> Dict[str, Any]:
    stem = Path(_s(file_name).replace("\\", "/")).stem
    match = re.match(r"^\s*(?:cd\d+[-_. ]*)?0*(\d{1,3})[-_. ]+(.+)$", stem, re.I)
    if not match:
        return {"track": 0, "title": ""}
    try:
        track_no = int(match.group(1))
    except Exception:
        track_no = 0
    remainder = match.group(2).strip("-_ .")
    artist_slug = _qbit_torrent_scene_artist_slug(torrent_name)
    rem_slug = re.sub(r"[^a-z0-9]+", "_", remainder.casefold()).strip("_")
    if artist_slug and rem_slug.startswith(artist_slug + "_"):
        remainder = remainder[len(artist_slug):].lstrip("-_ .")
    elif "-" in remainder:
        # Scene files commonly use NN-artist-title.ext. If the artist slug did
        # not match exactly, the first hyphen chunk is still usually artist text.
        first, rest = remainder.split("-", 1)
        first_key = re.sub(r"[^a-z0-9]+", "", first.casefold())
        artist_key = re.sub(r"[^a-z0-9]+", "", artist_slug)
        if first_key and artist_key and (first_key in artist_key or artist_key in first_key):
            remainder = rest
    return {"track": track_no, "title": _qbit_scene_text(remainder)}


def _library_file_candidates_for_qbit_metadata(file_name: str, size: int,
                                               torrent: Dict[str, Any], *,
                                               require_size: bool = True) -> List[Dict[str, Any]]:
    if size <= 0:
        return []
    torrent_name = _s(torrent.get("name"))
    album_guess = _qbit_torrent_album_guess(torrent_name)
    track_guess = _qbit_track_guess_from_scene_file(file_name, torrent_name)
    track_no = int(track_guess.get("track") or 0)
    title_guess = _s(track_guess.get("title"))
    album_norm = _normalize_album(album_guess)
    album_key = _album_key(album_guess)
    title_norm = _album_track_norm(title_guess)
    if not album_norm and not album_key:
        return []
    if not track_no and not title_norm:
        return []

    meta: Dict[str, Any] = {}
    if album_guess:
        meta["album"] = album_guess
    if title_guess:
        meta["title"] = title_guess
    if track_no:
        meta["track"] = track_no
    rows: List[Dict[str, Any]] = []
    try:
        rows = composite_workflows.find_files_for_hardlink(metadata=meta, limit=100)
    except Exception:
        return []

    matches: List[Dict[str, Any]] = []
    seen_paths: set = set()
    for row in rows:
        raw = _s(row.get("path") if isinstance(row, dict) else row["path"]).strip()
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = MUSIC_ROOT / raw
        try:
            if not _path_under(path, MUSIC_ROOT):
                continue
            if not path.exists() or not path.is_file():
                continue
            actual_size = int(path.stat().st_size)
            if require_size and actual_size != size:
                continue
            resolved = str(path.resolve(strict=False))
        except Exception:
            continue

        row_album = _s(row["album"])
        row_album_norm = _normalize_album(row_album)
        row_album_key = _album_key(row_album)
        album_match = bool(
            (album_norm and album_norm == row_album_norm)
            or (album_key and album_key == row_album_key)
        )
        if not album_match:
            continue

        row_title_norm = _album_track_norm(_s(row["title"]))
        title_match = bool(title_norm and row_title_norm and (
            title_norm == row_title_norm
            or difflib.SequenceMatcher(None, title_norm, row_title_norm).ratio() >= 0.92
        ))
        if not track_no and not title_match:
            continue

        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        matches.append({
            "id": int(row["id"]),
            "path": str(path),
            "title": _s(row["title"]),
            "artist": _s(row["artist"]),
            "album": row_album,
            "albumartist": _s(row["albumartist"]),
            "track": int(row["track"] or 0),
            "disc": int(row["disc"] or 0),
            "size": size,
            "actual_size": actual_size,
            "size_delta": actual_size - size,
            "match_strategy": "album_track_size" if not title_match else "album_track_title_size",
            "torrent_album_guess": album_guess,
            "torrent_title_guess": title_guess,
        })
    return matches


_QBIT_HARDLINK_SAFE_ENGINE_ERRORS = frozenset({
    "Access denied for path outside allowed roots",
    "Source path must be a regular non-symlink file",
    "Source file size does not match expected size",
    "Target path exists and is a symlink",
    "Target path exists and is a different file",
    "Target path exists",
    "Cross-device hardlink not supported",
    "Failed to create hardlink",
    "Post-link verification failed",
    "Post-link identity mismatch",
    "Invalid expected_size value",
})


def _qbit_hardlink_public_error(res: Any) -> str:
    """Return a message safe to surface in job logs/status.

    The control agent's error strings are all fixed, enumerable, non-secret
    messages today, but this boundary must not assume every future engine
    error is equally safe to forward verbatim -- only a known-safe string
    passes through; anything else (or a malformed response) collapses to
    the generic fallback.
    """
    if not isinstance(res, dict):
        return "Could not create this hardlink."
    err = res.get("error")
    if isinstance(err, str) and err in _QBIT_HARDLINK_SAFE_ENGINE_ERRORS:
        return err
    return "Could not create this hardlink."


def _qbit_hardlink_missing_impl(*, dry_run: bool, category: str,
                                qbit_filter: str,
                                search: str,
                                hashes: List[str], limit: int,
                                recheck: bool, log: list,
                                cancel_event=None) -> Dict[str, Any]:
    cookie = _qbit_login_cookie()
    params: Dict[str, Any] = {}
    category = _s(category).strip()
    if category and category.lower() not in {"*", "all", "any"}:
        params["category"] = category
    qbit_filter = _s(qbit_filter).strip()
    if qbit_filter and qbit_filter.lower() not in {"*", "all", "any"}:
        params["filter"] = qbit_filter
    torrents = _qbit_request_json("/api/v2/torrents/info", params, cookie) or []
    wanted_hashes = {h.strip().lower() for h in hashes if h and h.strip()}
    if wanted_hashes:
        torrents = [
            t for t in torrents
            if _s(t.get("hash")).strip().lower() in wanted_hashes
        ]
    search_norm = _s(search).strip().casefold()
    if search_norm:
        torrents = [
            t for t in torrents
            if search_norm in _s(t.get("name")).casefold()
        ]
    log.append(
        f"qBittorrent scan: {len(torrents)} torrent(s), "
        f"category={category or 'all'}, filter={qbit_filter or 'all'}, "
        f"search={search or 'all'}, dry_run={dry_run}"
    )

    summary = {
        "dry_run": dry_run,
        "torrents": len(torrents),
        "category": category or "",
        "filter": qbit_filter or "",
        "search": search or "",
        "checked": 0,
        "already_present": 0,
        "linked": 0,
        "would_link": 0,
        "size_mismatch": 0,
        "skipped": 0,
        "errors": 0,
        "rechecked_hashes": 0,
        "actions": [],
    }
    touched_hashes: set = set()
    max_actions = 200

    def add_action(action: Dict[str, Any]) -> None:
        if len(summary["actions"]) < max_actions:
            summary["actions"].append(action)

    for torrent in torrents:
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("cancelled")
        thash = _s(torrent.get("hash")).strip()
        if not thash:
            continue
        try:
            files = _qbit_request_json(
                "/api/v2/torrents/files",
                {"hash": thash},
                cookie,
            ) or []
        except Exception as ex:
            summary["errors"] += 1
            log.append(f"  ERROR reading files for {torrent.get('name')}: {ex}")
            continue
        for file_row in files:
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("cancelled")
            if limit and summary["checked"] >= limit:
                break
            name = _s(file_row.get("name")).strip()
            if Path(name).suffix.lower() not in AUDIO_EXT:
                continue
            progress = float(file_row.get("progress") or 0)
            size = int(file_row.get("size") or 0)
            if size <= 0:
                summary["skipped"] += 1
                add_action({
                    "status": "skipped",
                    "reason": "missing file size",
                    "torrent": _s(torrent.get("name")),
                    "file": name,
                    "progress": round(progress, 3),
                    "size": size,
                })
                continue
            summary["checked"] += 1
            target_candidates = _qbit_candidate_target_paths(torrent, file_row)
            existing_target = next((p for p in target_candidates if p.exists() and p.is_file()), None)
            if existing_target:
                summary["already_present"] += 1
                add_action({
                    "status": "already_present",
                    "reason": "qBittorrent target file exists",
                    "torrent": _s(torrent.get("name")),
                    "file": name,
                    "target": str(existing_target),
                    "progress": round(progress, 3),
                    "size": size,
                })
                continue
            target = next((p for p in target_candidates if _qbit_allowed_repair_path(p)), None)
            if not target:
                summary["skipped"] += 1
                add_action({
                    "status": "skipped",
                    "reason": "no allowed qBittorrent target path",
                    "torrent": _s(torrent.get("name")),
                    "file": name,
                    "progress": round(progress, 3),
                    "size": size,
                })
                continue
            lib_matches = _library_file_candidates_for_qbit(name, size)
            if len(lib_matches) != 1:
                meta_matches = _library_file_candidates_for_qbit_metadata(name, size, torrent)
                if len(meta_matches) == 1 or not lib_matches:
                    lib_matches = meta_matches
            if len(lib_matches) != 1:
                size_mismatch_matches: List[Dict[str, Any]] = []
                if not lib_matches:
                    size_mismatch_matches = [
                        m for m in _library_file_candidates_for_qbit_metadata(
                            name, size, torrent, require_size=False)
                        if int(m.get("actual_size") or 0) != size
                    ]
                if len(size_mismatch_matches) == 1:
                    src = Path(size_mismatch_matches[0]["path"])
                    summary["skipped"] += 1
                    summary["size_mismatch"] += 1
                    add_action({
                        "status": "size_mismatch",
                        "reason": (
                            "library track matched by metadata, but size differs; "
                            "qBittorrent recheck would fail"
                        ),
                        "torrent": _s(torrent.get("name")),
                        "file": name,
                        "source": str(src),
                        "target": str(target),
                        "progress": round(progress, 3),
                        "expected_size": size,
                        "actual_size": int(size_mismatch_matches[0].get("actual_size") or 0),
                        "size_delta": int(size_mismatch_matches[0].get("size_delta") or 0),
                        "match_strategy": size_mismatch_matches[0].get("match_strategy", ""),
                    })
                    continue
                summary["skipped"] += 1
                add_action({
                    "status": "skipped",
                    "reason": "no unique library file match" if not lib_matches else "ambiguous library matches",
                    "torrent": _s(torrent.get("name")),
                    "file": name,
                    "target": str(target),
                    "progress": round(progress, 3),
                    "size": size,
                    "match_count": len(lib_matches),
                })
                continue
            src = Path(lib_matches[0]["path"])
            action = {
                "status": "would_link" if dry_run else "linked",
                "torrent": _s(torrent.get("name")),
                "file": name,
                "source": str(src),
                "target": str(target),
                "progress": round(progress, 3),
                "reason": (
                    "unique library file matched by filename and size"
                    if lib_matches[0].get("match_strategy") == "filename_size"
                    else "unique library file matched by album/track metadata and size"
                ),
                "size": size,
                "match_strategy": lib_matches[0].get("match_strategy", ""),
            }
            if dry_run:
                summary["would_link"] += 1
                add_action(action)
                log.append(f"  would link: {target.name}")
                continue
            try:
                res = composite_workflows.create_hardlink(str(src), str(target), expected_size=size)
                if res and res.get("ok"):
                    touched_hashes.add(thash)
                    if res.get("already_present"):
                        # Report truthfully: this call did not create a new
                        # hardlink, so it must not inflate the "linked" count.
                        summary["already_present"] += 1
                        action["status"] = "already_present"
                        action["reason"] = "qBittorrent target file is already hardlinked"
                        log.append(f"  already present: {target}")
                    else:
                        summary["linked"] += 1
                        log.append(f"  linked: {target}")
                    add_action(action)
                else:
                    summary["errors"] += 1
                    action["status"] = "error"
                    action["error"] = _qbit_hardlink_public_error(res)
                    add_action(action)
                    log.append(f"  ERROR linking {target}: {action['error']}")
            except Exception as ex:
                summary["errors"] += 1
                action["status"] = "error"
                action["error"] = "Could not create this hardlink."
                add_action(action)
                log.append(f"  ERROR linking {target}: {type(ex).__name__}")
        if limit and summary["checked"] >= limit:
            break

    if touched_hashes and recheck:
        try:
            _qbit_post(
                "/api/v2/torrents/recheck",
                {"hashes": "|".join(sorted(touched_hashes))},
                cookie,
            )
            summary["rechecked_hashes"] = len(touched_hashes)
            log.append(f"Triggered qBittorrent recheck for {len(touched_hashes)} torrent(s).")
        except Exception as ex:
            summary["errors"] += 1
            log.append(f"  WARN: qBittorrent recheck failed: {ex}")

    log.append(
        "qBittorrent hardlink repair complete: "
        f"{summary['linked']} linked, {summary['would_link']} would link, "
        f"{summary['already_present']} already present, {summary['skipped']} skipped, "
        f"{summary['errors']} error(s)."
    )
    return summary


def _audio_files_under(path_value: str) -> List[Path]:
    root = Path(path_value)
    try:
        if root.is_file():
            return [root] if root.suffix.lower() in AUDIO_EXT else []
        if root.exists():
            return [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXT]
    except Exception:
        return []
    return []
