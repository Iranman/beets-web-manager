"""Playlist and Plex routes (ARCH-001): thin HTTP handlers over backend.playlist_service / backend.plex_service.
"""

from __future__ import annotations

import json, time
import urllib.error
from collections import defaultdict
from typing import Any, Dict, List, Optional
from flask import jsonify, request
from backend.beets_adapter import BeetsUnavailableError
import backend.composite_workflows as composite_workflows
import backend.provider_boundary as provider_boundary
from backend.app_runtime import _app_logger, _norm, _redact_security_text, _s, jobs
from backend.playlist_service import playlist_sync_status_payload, PlaylistQualityCandidatesUnavailableError, PlaylistStateError, _PLAYLIST_STATE_LOCK, _PLAYLIST_SYNC_STATE, _clean_playlist_name, _create_playlist_outputs, _pl_dl_jobs, _playlist_apply_manifest_replacements, _playlist_apply_track_action, _playlist_clean_track_list, _playlist_clean_video_text, _playlist_delete_job_state, _playlist_detail_payload, _playlist_detail_summary_payload, _playlist_download_methods, _playlist_ensure_stable_id, _playlist_ensure_state_dirs, _playlist_int, _playlist_interrupted_saved_job_state, _playlist_job_state_name, _playlist_key, _playlist_library_index, _playlist_load_index, _playlist_load_job_state, _playlist_m3u_summary, _playlist_manifest_path, _playlist_manual_placement_from_payload, _playlist_new_internal_id, _playlist_other_live_pids_with_name, _playlist_place_quality_candidate_job, _playlist_quality_cleanup_candidates, _playlist_read_manifest, _playlist_record_pipeline, _playlist_replace_manifest, _playlist_resolve_stable_id, _playlist_run_quality_cleanup_job, _playlist_save_index, _playlist_save_job_state, _playlist_saved_job_states_for_name, _playlist_saved_playlist_exists, _playlist_saved_playlist_records, _playlist_start_direct_action, _playlist_start_download_action, _playlist_state_error_payload, _playlist_state_error_status, _playlist_suggestions_for_track, _playlist_sync_all_locked, _playlist_valid_internal_id, _playlist_write_manifest, _plex_delete_playlist_by_rating_key, _plex_delete_playlist_by_title_unambiguous, parse_playlist_request, start_playlist_download
from backend.plex_service import _plex_settings, _plex_status_payload, _trigger_plex_refresh
from backend.serializers import json_route_result
from app import app  # noqa: E402  (route modules load after app.py defines app)
from backend.playlist_service import _playlist_group_known_total, _playlist_matched_rows_page, _playlist_state_rows_page

# ── ARCH-001 extracted code ──


@app.get("/api/plex/status")
def api_plex_status():
    return jsonify(_plex_status_payload(force=True))


@app.post("/api/plex/refresh")
def api_plex_refresh():
    def _do(log):
        status = _plex_status_payload(force=True)
        if not status.get("configured") or not status.get("connected"):
            raise RuntimeError(status.get("error") or "Plex is not connected")
        log.append(f"Plex server: {status.get('url')}")
        log.append(f"Music library: {status.get('section_title') or status.get('section_key')}")
        if not _trigger_plex_refresh(log, workflow="manual"):
            raise RuntimeError("Plex refresh failed")
        return status

    job = jobs.start_python(_do, label="Plex library refresh")
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/playlist/parse")
def playlist_parse():
    body, status = parse_playlist_request(request.get_json(silent=True) or {})
    return json_route_result(body, status)


@app.post("/api/playlist/create")
def playlist_create():
    payload = request.get_json(silent=True) or {}
    name    = _clean_playlist_name(payload.get("name") or "Untitled")
    items   = payload.get("items") or []  # [{id,path,title,artist}]
    desired_tracks = payload.get("desired_tracks")
    if not isinstance(desired_tracks, list) or not desired_tracks:
        desired_tracks = payload.get("tracks")
    if not isinstance(desired_tracks, list) or not desired_tracks:
        desired_tracks = items
    missing_tracks = payload.get("missing_tracks")
    if not isinstance(missing_tracks, list):
        missing_tracks = []
    requested_playlist_id = _s(payload.get("playlist_id") or "").strip()
    try:
        if items:
            result = _create_playlist_outputs(
                name,
                items,
                desired_tracks=desired_tracks,
                missing_tracks=missing_tracks,
                source=_s(payload.get("source") or "manual"),
                content=_s(payload.get("content") or ""),
                playlist_id=requested_playlist_id or _playlist_new_internal_id(),
            )
        elif desired_tracks:
            _playlist_ensure_state_dirs()
            pid = _playlist_ensure_stable_id(name, playlist_id=requested_playlist_id or _playlist_new_internal_id())
            key = _playlist_key(name, playlist_id=pid, allocate=False)
            export_result = composite_workflows.export_playlist_m3u(key, name, [])
            if not (isinstance(export_result, dict) and export_result.get("ok")):
                raise RuntimeError("m3u_export_failed")
            manifest = _playlist_write_manifest(
                name,
                desired_tracks,
                matched_tracks=[],
                missing_tracks=missing_tracks or desired_tracks,
                source=_s(payload.get("source") or "manual"),
                content=_s(payload.get("content") or ""),
                playlist_id=pid,
            )
            result = {
                "m3u": f"engine:{key}.m3u",
                "manifest": str(_playlist_manifest_path(name, manifest)),
                "playlist_id": pid,
                "playlist_key": key,
                "plex": {"created": False, "tracks_added": 0, "error": None},
                "tracks_in_m3u": 0,
                "desired_tracks": len(_playlist_clean_track_list(desired_tracks)),
                "missing_tracks": len(_playlist_clean_track_list(missing_tracks or desired_tracks)),
            }
        else:
            raise RuntimeError("No playlist tracks were provided")
    except Exception as exc:
        _app_logger.warning("Playlist M3U generation failed: %s", type(exc).__name__)
        cause = getattr(exc, "__cause__", None)
        if isinstance(exc, BeetsUnavailableError) or isinstance(cause, BeetsUnavailableError):
            return jsonify({
                "ok": False,
                "error": "Engine is unavailable; could not export authoritative M3U.",
                "error_code": "engine_unavailable",
            }), 503
        if isinstance(exc, PlaylistStateError):
            _app_logger.warning("Playlist creation state error: %s (%s)", exc.code, _redact_security_text(str(exc)))
            return jsonify(_playlist_state_error_payload(exc)), _playlist_state_error_status(exc)
        return jsonify({
            "ok": False,
            "error": "Could not generate playlist file.",
            "error_code": "m3u_export_failed",
        }), 502
    return jsonify({"ok": True, **result})


@app.post("/api/playlist/download")
def playlist_download():
    body, status = start_playlist_download(request.get_json(silent=True) or {})
    return json_route_result(body, status)


@app.get("/api/playlist/download/<jid>")
def playlist_download_status(jid):
    s = _pl_dl_jobs.get(jid)
    if not s:
        saved = _playlist_load_job_state(jid)
        if saved:
            if _s(saved.get("status") or "").lower() == "running":
                saved = _playlist_interrupted_saved_job_state(jid, saved)
            return jsonify({"ok": True, **saved})
    if not s:
        return jsonify({"ok": False, "error": "Job not found"})
    return jsonify({"ok": True, **s})


@app.post("/api/playlists/quality-cleanup")
def playlist_quality_cleanup():
    payload = request.get_json(silent=True) or {}
    dry_run = bool(payload.get("dry_run", True))
    limit = int(payload.get("limit") or 200)
    action = _s(payload.get("action") or "scan").strip().lower()
    filter_mode = _s(payload.get("filter") or payload.get("filter_mode") or "all").strip().lower()
    all_matching = bool(payload.get("all_matching") or payload.get("all"))
    if action in {"repair", "delete_preview", "move_singletons"} and filter_mode == "all":
        filter_mode = "preview" if action == "delete_preview" else "repair"
    raw_ids = payload.get("item_ids") or []
    item_ids = [int(v) for v in raw_ids if str(v).strip().isdigit()] if isinstance(raw_ids, list) else []
    try:
        candidates = _playlist_quality_cleanup_candidates(
            limit=limit, item_ids=item_ids or None, filter_mode=filter_mode)
    except PlaylistQualityCandidatesUnavailableError as ex:
        # Never report "0 candidates" for a scan that didn't actually run --
        # that would read as "library is clean" when it is really "engine
        # unreachable", hiding real quality issues from the operator. The
        # underlying exception (which may carry connection/URL detail from
        # the IPC layer) is logged server-side only, never in the response.
        _app_logger.warning("Playlist quality-cleanup scan failed: %s", ex)
        return jsonify({"ok": False, "error": "Could not reach the Beets engine to scan for quality issues"}), 502
    summary = {
        "candidates": len(candidates),
        "bad": len([c for c in candidates if c.get("quality") == "bad"]),
        "review": len([c for c in candidates if c.get("quality") == "review"]),
        "repair": len([c for c in candidates if c.get("recommended_action") == "repair"]),
        "delete_preview": len([c for c in candidates if c.get("recommended_action") == "delete_preview"]),
        "move_singletons": len([
            c for c in candidates
            if "bad_playlist_path" in set(c.get("quality_flags") or [])
            and "preview_risk" not in set(c.get("quality_flags") or [])
        ]),
    }
    if dry_run:
        return jsonify({
            "ok": True, "dry_run": True, "action": action, "filter": filter_mode,
            "summary": summary, "candidates": candidates,
        })
    if action not in {"repair", "delete_preview", "move_singletons"}:
        return jsonify({
            "ok": False,
            "error": "Choose action='repair', action='move_singletons', or action='delete_preview'.",
            "summary": summary,
            "candidates": candidates,
        }), 400
    if not item_ids and not all_matching:
        return jsonify({
            "ok": False,
            "error": "Pass item_ids, or all_matching=true, to queue playlist quality cleanup.",
            "summary": summary,
            "candidates": candidates,
        }), 400
    def _matches_requested_action(candidate: Dict[str, Any]) -> bool:
        if action == "move_singletons":
            flags = set(candidate.get("quality_flags") or [])
            return "bad_playlist_path" in flags and "preview_risk" not in flags
        return candidate.get("recommended_action") == action

    selected_candidates = [
        c for c in candidates
        if int(c.get("id") or 0) > 0 and _matches_requested_action(c)
    ]
    if not selected_candidates:
        return jsonify({
            "ok": True,
            "dry_run": False,
            "action": action,
            "filter": filter_mode,
            "summary": summary,
            "queued": False,
            "job_id": "",
            "backup": "",
            "rows_deleted": 0,
            "files_deleted": 0,
            "rows_repaired": 0,
            "rows_moved": 0,
            "repaired": [],
            "deleted": [],
            "moved": [],
        })

    label = (
        f"Playlist cleanup: place {len(selected_candidates)} track(s)"
        if action == "repair"
        else (
            f"Playlist cleanup: move {len(selected_candidates)} singleton track(s)"
            if action == "move_singletons"
            else f"Playlist cleanup: delete {len(selected_candidates)} preview row(s)"
        )
    )

    def _do(log, cancel_event=None):
        return _playlist_run_quality_cleanup_job(
            action, selected_candidates, summary, log=log, cancel_event=cancel_event)

    job = jobs.start_python(_do, label=label, metadata={
        "type": "playlist-quality-cleanup",
        "action": action,
        "filter": filter_mode,
        "item_count": len(selected_candidates),
        "all_matching": all_matching,
    })
    return jsonify({
        "ok": True,
        "dry_run": False,
        "action": action,
        "filter": filter_mode,
        "summary": summary,
        "queued": True,
        "job_id": job.job_id,
        "candidates": selected_candidates[:25],
    })


@app.post("/api/playlists/quality-place")
def playlist_quality_place():
    payload = request.get_json(silent=True) or {}
    item_id = _playlist_int(payload.get("item_id") or payload.get("id"), 0)
    if item_id <= 0:
        return jsonify({"ok": False, "error": "item_id is required"}), 400

    try:
        candidates = _playlist_quality_cleanup_candidates(
            limit=50,
            item_ids=[item_id],
            filter_mode="repair",
        )
    except PlaylistQualityCandidatesUnavailableError as ex:
        _app_logger.warning("Playlist quality-place candidate lookup failed: %s", ex)
        return jsonify({"ok": False, "error": "Could not reach the Beets engine to look up this item"}), 502
    candidate = next((c for c in candidates if int(c.get("id") or 0) == item_id), None)
    if not candidate:
        return jsonify({
            "ok": False,
            "error": f"Item {item_id} is not a playlist review/repair candidate",
        }), 404

    placement = _playlist_manual_placement_from_payload(candidate, payload)
    if not placement.get("ok"):
        return jsonify({"ok": False, "error": placement.get("reason") or "Invalid placement"}), 400

    sync_playlist = _clean_playlist_name(_s(payload.get("playlist") or payload.get("name") or ""))
    job_id = _playlist_place_quality_candidate_job(item_id, placement, sync_playlist=sync_playlist)
    return jsonify({
        "ok": True,
        "queued": True,
        "job_id": job_id,
        "candidate": candidate,
        "placement": placement,
    })


@app.get("/api/playlists")
def list_playlists():
    started = time.perf_counter()
    _playlist_ensure_state_dirs()
    out = []
    index: Optional[Dict[str, Any]] = None
    checkpoint_states = _playlist_saved_job_states_for_name("", mark_interrupted=True)
    checkpoint_states_by_name: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for state in checkpoint_states:
        state_name = _playlist_job_state_name(state)
        if state_name:
            checkpoint_states_by_name[_norm(state_name)].append(state)
    records, diagnostics = _playlist_saved_playlist_records(checkpoint_states)
    last_details = {}
    last_result = (_PLAYLIST_SYNC_STATE.get("last_result") or {})
    if isinstance(last_result, dict):
        for detail in last_result.get("details") or []:
            last_details[_norm(detail.get("name", ""))] = detail
    for record in records:
        name = _s(record.get("name") or "")
        if not name:
            continue
        manifest = record.get("manifest") if isinstance(record.get("manifest"), dict) else _playlist_read_manifest(name)
        summary = _playlist_m3u_summary(
            name,
            index,
            checkpoint_states_by_name.get(_norm(name), []),
            manifest,
        )
        sync_detail = last_details.get(_norm(name)) or {}
        plex_synced = bool(sync_detail.get("plex_synced")) if sync_detail else None
        plex_tracks = int(sync_detail.get("plex_tracks") or 0)
        if plex_synced:
            plex_tracks = int(
                sync_detail.get("plex_tracks_matched")
                or sync_detail.get("merged_tracks")
                or sync_detail.get("plex_tracks")
                or 0
            )
        last_plex = manifest.get("last_plex") if isinstance(manifest.get("last_plex"), dict) else {}
        if last_plex:
            plex_synced = bool(last_plex.get("complete"))
            verified_plex_tracks = int(
                last_plex.get("verified_count")
                or last_plex.get("existing_playlist_count")
                or 0
            )
            if verified_plex_tracks:
                plex_tracks = verified_plex_tracks
            elif plex_synced:
                plex_tracks = int(last_plex.get("tracks_added") or last_plex.get("tracks_matched") or 0)
            elif not plex_tracks:
                plex_tracks = int(last_plex.get("tracks_added") or 0)
        has_m3u = bool(record.get("has_m3u"))
        has_manifest = bool(record.get("has_manifest"))
        has_checkpoint = bool(record.get("has_checkpoint"))
        source = _s(manifest.get("source") or "").strip()
        if not source:
            source = "local_m3u" if has_m3u else "manifest"
        out.append({
            "name": name,
            **summary,
            "has_m3u": has_m3u,
            "has_manifest": has_manifest,
            "has_checkpoint": has_checkpoint,
            "playlist_id": _s(manifest.get("id") or manifest.get("playlist_id") or ""),
            "manifest_path": _s(record.get("manifest_path") or ""),
            "m3u_path": f"engine:{record.get('playlist_key') or record.get('key')}.m3u" if has_m3u else "",
            "plex_tracks": plex_tracks,
            "plex_synced": plex_synced,
            "last_plex": last_plex,
            "plex_tracks_matched": int(last_plex.get("tracks_matched") or 0),
            "plex_tracks_unmatched": int(last_plex.get("tracks_unmatched") or 0),
            "plex_pending_count": int(last_plex.get("pending_plex_count") or last_plex.get("tracks_unmatched") or 0),
            "last_sync_status": _s(last_plex.get("status") or ("not_run" if plex_synced is None else "synced")),
            "last_sync_error": _s(last_plex.get("error") or ""),
            "source": source,
            "last_pipeline": dict(manifest.get("last_pipeline") or {}),
            "sync_status": "synced" if summary["missing"] == 0 else "missing",
        })
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    if duration_ms > 500:
        _app_logger.info("/api/playlists listed %s playlist(s) in %.1f ms", len(out), duration_ms)
    return jsonify({
        "playlists": out,
        "diagnostics": diagnostics,
        "supported_sources": ["local_m3u", "url", "text"],
        "download_sources": _playlist_download_methods(),
        "duration_ms": duration_ms,
    })


@app.delete("/api/playlists/<path:name>")
def playlist_delete(name):
    clean_name = _clean_playlist_name(_s(name))
    payload = request.get_json(silent=True) or {}
    playlist_id = _s(payload.get("playlist_id") or "").strip()
    try:
        _playlist_ensure_state_dirs()
    except PlaylistStateError as exc:
        _app_logger.warning("Playlist delete state error: %s (%s)", exc.code, _redact_security_text(str(exc)))
        return jsonify(_playlist_state_error_payload(exc)), _playlist_state_error_status(exc)
    pid = ""
    try:
        pid = _playlist_resolve_stable_id(clean_name, playlist_id=playlist_id or None)
        key = _playlist_key(clean_name, playlist_id=pid or None, allocate=False)
    except PlaylistStateError as exc:
        _app_logger.warning("Playlist delete identity error: %s (%s)", exc.code, _redact_security_text(str(exc)))
        return jsonify(_playlist_state_error_payload(exc)), _playlist_state_error_status(exc)
    delete_plex = bool(payload.get("delete_plex", True))

    deleted_m3u = False
    deleted_manifest = False
    plex_deleted = 0
    plex_error = ""

    try:
        res = composite_workflows.delete_playlist_m3u(key, fallback_name=clean_name)
        if not (isinstance(res, dict) and res.get("ok")):
            return jsonify({
                "ok": False,
                "error": "Could not delete authoritative engine M3U.",
                "error_code": "m3u_delete_failed",
            }), 502
        deleted_m3u = bool(res.get("deleted"))
    except Exception as ex:
        _app_logger.warning("Engine M3U delete via IPC failed: %s", type(ex).__name__)
        return jsonify({
            "ok": False,
            "error": "Engine is unavailable; could not delete authoritative M3U.",
            "error_code": "engine_unavailable",
        }), 503

    manifest_seed = {"playlist_id": pid} if _playlist_valid_internal_id(pid) else None
    # Read manifest data (for its stored Plex ratingKey, if any) before
    # removing it -- the file is the only place that identity is recorded.
    manifest_data = _playlist_read_manifest(clean_name, playlist_id=pid or None)
    manifest_path = _playlist_manifest_path(clean_name, manifest_seed, allocate=False)
    try:
        if manifest_path.exists():
            manifest_path.unlink()
            deleted_manifest = True
    except Exception as ex:
        _app_logger.warning("Could not delete playlist manifest %r: %s", clean_name, type(ex).__name__)

    try:
        for ckpt in _playlist_saved_job_states_for_name(clean_name, playlist_id=pid, strict=True):
            ckpt_jid = _s(ckpt.get("job_id"))
            if ckpt_jid:
                _playlist_delete_job_state(ckpt_jid)
    except PlaylistStateError as exc:
        _app_logger.warning("Could not delete playlist checkpoint for %r: %s", clean_name, exc.code)

    # The engine M3U delete above is this route's one hard-required step
    # (a failure already returned before this point); once past it, the
    # playlist's identity record is retired from the index so a later
    # same-named create doesn't collide with a dead orphan entry that
    # would otherwise falsely trip ambiguous_playlist detection (SEC-002
    # Wave 10 second final review).
    if _playlist_valid_internal_id(pid):
        try:
            with _PLAYLIST_STATE_LOCK:
                index_data = _playlist_load_index()
                if pid in index_data:
                    del index_data[pid]
                    _playlist_save_index(index_data)
        except PlaylistStateError as exc:
            _app_logger.warning("Could not retire playlist index entry %r: %s", pid, exc.code)

    if delete_plex and _plex_settings().get("token"):
        stored_rating_key = _s((manifest_data.get("last_plex") or {}).get("rating_key") or "").strip()
        try:
            if stored_rating_key:
                plex_deleted = _plex_delete_playlist_by_rating_key(stored_rating_key)
            else:
                others = _playlist_other_live_pids_with_name(clean_name, exclude_pid=pid)
                if others:
                    plex_error = "Another playlist shares this name in Plex; delete it manually or sync first to establish a distinct Plex identity."
                else:
                    plex_deleted, plex_err = _plex_delete_playlist_by_title_unambiguous(clean_name)
                    if plex_err:
                        plex_error = "Plex title fallback is ambiguous; delete it manually or sync first to establish a distinct Plex identity."
        except urllib.error.HTTPError as ex:
            plex_error = "Plex token is invalid or expired." if ex.code in (401, 403) else f"Plex returned HTTP {ex.code}."
        except Exception as ex:
            _app_logger.warning("Plex playlist delete failed: %s", type(ex).__name__)
            plex_error = "Could not delete this playlist from Plex."
    return jsonify({
        "ok": True,
        "name": clean_name,
        "key": key,
        "m3u": f"engine:{key}.m3u",
        "deleted_m3u": deleted_m3u,
        "deleted_manifest": deleted_manifest,
        "plex_deleted": plex_deleted,
        "plex_error": plex_error,
        "library_tracks_deleted": 0,
    })


@app.post("/api/playlists/<path:name>/tracks/action")
def playlist_track_action(name):
    clean_name = _clean_playlist_name(_s(name))
    if not _playlist_saved_playlist_exists(clean_name):
        return jsonify({"ok": False, "error": f"Playlist not found: {clean_name}"}), 404
    payload = request.get_json(silent=True) or {}
    track = payload.get("track") if isinstance(payload.get("track"), dict) else {}
    if not _s(track.get("title") or track.get("path") or "").strip():
        return jsonify({"ok": False, "error": "A playlist track is required"}), 400
    try:
        result = _playlist_apply_track_action(
            clean_name,
            _s(payload.get("action") or ""),
            track,
            requested_path=_s(payload.get("path") or ""),
        )
    except Exception as ex:
        _app_logger.warning("Playlist track action failed: %s", type(ex).__name__)
        return jsonify({"ok": False, "error": "Could not apply this track action."}), 400
    normalized_action = _s(payload.get("action") or "").strip().lower().replace("-", "_")
    try:
        if normalized_action in {"retry", "retry_download"}:
            result["job"] = _playlist_start_download_action(
                clean_name, "download_missing", retry_tracks=[track])
        elif normalized_action == "retry_import":
            result["job"] = _playlist_start_direct_action(clean_name, "import_downloaded")
    except Exception as ex:
        _app_logger.warning("Playlist track retry failed: %s", type(ex).__name__)
        result["retry_error"] = "Could not retry this track."
    return jsonify(result)


@app.post("/api/playlists/<path:name>/resolve-track")
def playlist_resolve_track(name):
    clean_name = _clean_playlist_name(_s(name))
    if not _playlist_saved_playlist_exists(clean_name):
        return jsonify({"ok": False, "error": f"Playlist not found: {clean_name}"}), 404
    payload = request.get_json(silent=True) or {}
    original = payload.get("track") if isinstance(payload.get("track"), dict) else {}
    replacement = payload.get("replacement") if isinstance(payload.get("replacement"), dict) else {}
    if not _playlist_clean_video_text(replacement.get("title") or payload.get("title") or ""):
        return jsonify({"ok": False, "error": "A replacement title is required"}), 400
    result = _playlist_apply_manifest_replacements(
        clean_name,
        [{"track": original, "replacement": replacement}],
        source_label="manual",
    )
    if not result.get("resolved_count"):
        if result.get("errors"):
            return jsonify({"ok": False, "error": result["errors"][0].get("error") or "Resolve failed"}), 400
        return jsonify({"ok": False, "error": "Could not find that track in the playlist manifest"}), 404
    return jsonify(result)


def _suggestions_keeping_local(track, index, include_mb: bool, limit: int, state: Dict[str, bool]):
    """A MusicBrainz outage keeps the Beets-local suggestions and sets
    state["musicbrainz_unavailable"]; later tracks skip MusicBrainz."""
    if include_mb and not state.get("musicbrainz_unavailable"):
        try:
            return _playlist_suggestions_for_track(track, index, include_musicbrainz=True, limit=limit)
        except provider_boundary.ProviderError:
            state["musicbrainz_unavailable"] = True
    return _playlist_suggestions_for_track(track, index, include_musicbrainz=False, limit=limit)


@app.get("/api/playlists/<path:name>/suggestions")
def playlist_suggestions(name):
    clean_name = _clean_playlist_name(_s(name))
    if not _playlist_saved_playlist_exists(clean_name):
        return jsonify({"ok": False, "error": f"Playlist not found: {clean_name}"}), 404
    include_mb = request.args.get("musicbrainz", "1").strip().lower() not in {"0", "false", "no", "off"}
    try:
        limit = int(request.args.get("limit") or 5)
    except Exception:
        limit = 5
    index = _playlist_library_index()
    detail = _playlist_detail_payload(clean_name, index)
    rows = []
    safe_count = 0
    mb_state: Dict[str, bool] = {}
    for track in detail.get("missing") or []:
        suggestions = _suggestions_keeping_local(track, index, include_mb, limit, mb_state)
        best = suggestions[0] if suggestions else None
        if best and best.get("safe"):
            safe_count += 1
        rows.append({
            "track": track,
            "suggestions": suggestions,
            "best": best,
        })
    return jsonify({
        "ok": True,
        "name": clean_name,
        "total_missing": len(detail.get("missing") or []),
        "safe_count": safe_count,
        "rows": rows,
        "musicbrainz_unavailable": bool(mb_state.get("musicbrainz_unavailable")),
    })


@app.post("/api/playlists/<path:name>/apply-safe-suggestions")
def playlist_apply_safe_suggestions(name):
    clean_name = _clean_playlist_name(_s(name))
    if not _playlist_saved_playlist_exists(clean_name):
        return jsonify({"ok": False, "error": f"Playlist not found: {clean_name}"}), 404
    payload = request.get_json(silent=True) or {}
    include_mb = bool(payload.get("musicbrainz", True))
    index = _playlist_library_index()
    detail = _playlist_detail_payload(clean_name, index)
    replacements: List[Dict[str, Any]] = []
    suggestion_rows = []
    mb_state: Dict[str, bool] = {}
    for track in detail.get("missing") or []:
        suggestions = _suggestions_keeping_local(track, index, include_mb, 5, mb_state)
        best = suggestions[0] if suggestions else None
        suggestion_rows.append({"track": track, "best": best})
        if best and best.get("safe"):
            replacements.append({
                "track": track,
                "replacement": {
                    "artist": best.get("artist") or "",
                    "title": best.get("title") or "",
                },
            })
    result = _playlist_apply_manifest_replacements(
        clean_name,
        replacements,
        index=index,
        source_label="suggestion",
    ) if replacements else _playlist_detail_payload(clean_name, index)
    return jsonify({
        **result,
        "suggested": suggestion_rows,
        "safe_count": len(replacements),
        "musicbrainz_unavailable": bool(mb_state.get("musicbrainz_unavailable")),
    })


@app.get("/api/playlists/<path:name>/tracks")
def playlist_tracks_detail(name):
    started = time.perf_counter()
    clean_name = _clean_playlist_name(_s(name))
    if not _playlist_saved_playlist_exists(clean_name):
        return jsonify({"ok": False, "error": f"Playlist not found: {clean_name}"}), 404

    mode = _s(request.args.get("mode") or "full").strip().lower()
    if mode in {"summary", "counts"}:
        payload = _playlist_detail_summary_payload(clean_name)
    elif mode in {"rows", "page"}:
        payload = _playlist_rows_page_payload(clean_name)
    else:
        payload = _playlist_detail_payload(clean_name)
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    payload["duration_ms"] = duration_ms
    if duration_ms > 500:
        _app_logger.info("/api/playlists/%s/tracks mode=%s built in %.1f ms", clean_name, payload.get("detail_mode") or mode, duration_ms)
    return jsonify(payload)


@app.post("/api/playlists/<path:name>/pipeline/<action>")
def playlist_pipeline_action(name, action):
    clean_name = _clean_playlist_name(_s(name))
    if not _playlist_saved_playlist_exists(clean_name):
        return jsonify({"ok": False, "error": f"Playlist not found: {clean_name}"}), 404
    normalized = _s(action).strip().lower().replace("-", "_")
    try:
        if normalized in {"sync_sources", "import_downloaded", "sync_plex", "reconcile_state"}:
            return jsonify(_playlist_start_direct_action(clean_name, normalized))
        if normalized in {"download_missing", "run_full", "resume"}:
            return jsonify(_playlist_start_download_action(clean_name, normalized))
        if normalized in {"pause", "stop"}:
            manifest = _playlist_read_manifest(clean_name)
            pipeline = dict(manifest.get("last_pipeline") or {})
            jobs_job_id = _s(pipeline.get("jobs_job_id") or "")
            playlist_job_id = _s(pipeline.get("playlist_job_id") or "")
            job = jobs.get(jobs_job_id) if jobs_job_id else None
            if playlist_job_id:
                state = _pl_dl_jobs.get(playlist_job_id)
                if state:
                    state["pause_requested"] = normalized == "pause"
                    state["stop_requested"] = normalized == "stop"
                    _playlist_save_job_state(state)
            if job and job.status == "running":
                job.kill()
            _playlist_record_pipeline(clean_name, status="paused" if normalized == "pause" else "stopped")
            return jsonify({"ok": True, "action": normalized, "job_id": jobs_job_id})
        if normalized == "clear":
            manifest = _playlist_read_manifest(clean_name)
            pipeline = dict(manifest.get("last_pipeline") or {})
            jobs_job_id = _s(pipeline.get("jobs_job_id") or "")
            job = jobs.get(jobs_job_id) if jobs_job_id else None
            if job and job.status == "running":
                return jsonify({"ok": False, "error": "Stop the running playlist job before clearing it"}), 409
            playlist_job_id = _s(pipeline.get("playlist_job_id") or "")
            checkpoint_ids = {
                _s(state.get("job_id") or "")
                for state in _playlist_saved_job_states_for_name(clean_name, playlist_id=_s(manifest.get("playlist_id") or ""), strict=True)
            }
            if playlist_job_id:
                checkpoint_ids.add(playlist_job_id)
            manifest["last_pipeline"] = {}
            _playlist_replace_manifest(clean_name, manifest)
            for checkpoint_id in checkpoint_ids:
                if not checkpoint_id:
                    continue
                _playlist_delete_job_state(checkpoint_id)
                _pl_dl_jobs.pop(checkpoint_id, None)
            return jsonify({"ok": True, "action": normalized})
    except Exception as ex:
        _app_logger.warning("Playlist pipeline action %r failed: %s", normalized, type(ex).__name__)
        return jsonify({"ok": False, "error": "Could not apply this pipeline action."}), 400
    return jsonify({"ok": False, "error": f"Unsupported pipeline action: {normalized}"}), 400


@app.get("/api/playlists/sync/status")
def playlist_sync_status():
    return jsonify(playlist_sync_status_payload())


@app.post("/api/playlists/sync")
def playlist_sync_start():
    payload = request.get_json(silent=True) or {}
    names_raw = payload.get("names") or payload.get("name") or []
    if isinstance(names_raw, str):
        names = [names_raw]
    else:
        names = [str(n) for n in names_raw if str(n).strip()] if isinstance(names_raw, list) else []

    def _do(log, cancel_event=None):
        log.append("Starting playlist two-way sync")
        result = _playlist_sync_all_locked(log, names=names or None)
        log.append(json.dumps(result, sort_keys=True))

    job = jobs.start_python(_do, label="Playlist two-way sync")
    return jsonify({"ok": True, "job_id": job.job_id})


def _playlist_int_request_arg(name: str, default: int, *, low: int = 0, high: int = 5000) -> int:
    try:
        value = int(request.args.get(name) or default)
    except Exception:
        value = default
    return max(low, min(high, value))


def _playlist_rows_page_payload(clean_name: str) -> Dict[str, Any]:
    group = _s(request.args.get("group") or "available").strip().lower().replace("-", "_")
    if group not in {"available", "missing", "waiting", "failed", "removed", "pending_plex"}:
        group = "available"
    offset = _playlist_int_request_arg("offset", 0, low=0, high=100000)
    limit = _playlist_int_request_arg("limit", 100, low=1, high=250)
    if group in {"waiting", "failed", "removed", "pending_plex"}:
        rows, has_more, exact_total = _playlist_state_rows_page(clean_name, group, offset, limit)
        scanned = len(rows)
    else:
        rows, has_more, scanned = _playlist_matched_rows_page(clean_name, group, offset, limit)
        exact_total = None
    summary = _playlist_detail_summary_payload(clean_name)
    known_total = exact_total if exact_total is not None else _playlist_group_known_total(summary, group)
    return {
        "ok": True,
        "name": clean_name,
        "detail_mode": "rows",
        "tracks_loaded": False,
        "partial_tracks_loaded": True,
        "group": group,
        "rows": rows,
        "offset": offset,
        "limit": limit,
        "row_count": len(rows),
        "known_total": known_total,
        "has_more": has_more,
        "scanned": scanned,
        "summary": summary,
    }
