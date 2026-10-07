"""Import, Import Review, reconciliation and AI routes (ARCH-001): HTTP handlers over the import services.
"""

from __future__ import annotations

import json, os, re, threading, time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List
from flask import jsonify, request
import backend.job_contract as job_contract
from backend.beets_adapter import BeetsError, BeetsUnavailableError, BeetsAuthError
import backend.composite_workflows as composite_workflows
import backend.import_reconciliation as _import_reconciliation
import backend.import_review_decision as _import_review_decision
from backend.ai_batch_state_service import _AI_BATCH_TERMINAL_STATUSES, _AI_BATCH_UNFINISHED_FOLDER_STATUSES, _MUSIC_FORMAT_POLICY_REVIEW_NOTE, _ai_batch_commit, _ai_batch_mark_folder, _ai_batch_public_state, _ai_batch_worker_registered, _ai_batch_write_state, _is_music_format_policy_handled_error
from backend.ai_service import _AI_BATCH_AI_TIMEOUT, _AI_MATCH_HISTORY_FILE, _AI_REVIEW_DECISIONS_FILE, _ai_batch_active_worker_job_id, _ai_batch_control, _ai_batch_find_state, _ai_batch_latest_state, _ai_suggest_folder_internal, _run_ai_release_preflight, _validate_import_source_audio
from backend.app_runtime import AUDIO_EXT, DOWNLOADS_ROOT, LOG_FILE, MUSIC_ROOT, _app_logger, _extract_mb_uuid, _redact_security_text, _s, jobs
from backend.artwork_service import _ART_EXTS, _album_art_status
from backend.import_reconciliation_service import _ai_batch_reconcile_state, _import_review_reconcile_job_lookup, _review_status_key
from backend.import_review_service import _candidate_track_comparison_payload, _import_review_auto_job_for_submission, _import_review_build_revalidated_match, _import_review_job_last_line, _import_review_revalidation_preflight, _import_review_start_auto_import, _manual_review_validate_album_identifier, _manual_review_validate_recording_identifier, _resolve_import_review_folder_path, _review_blocked_metadata, _review_item_origin_type, _review_queue_status_matches, _run_import_review_auto_enqueue_ready_batch, _update_pending_review_revalidation
from backend.import_service import _RECENT_IMPORTS_FILE, _cached_import_target_preview, _import_review_auto_lock, _import_review_auto_state, _import_review_auto_update, _import_skipped_items, _resolve_import_source_path, evaluate_import_eligibility, start_folder_import_with_id
from backend.library_service import _album_folder_for_album_id, _delete_if_already_in_library, _delete_review_source_folder, _library_no_mb_album_matches_folder, _preserve_torrent_source_path, _scan_artist_folder_groups
from backend.matching_service import _ai_api_key, _invalidate_lib_cache, _load_album_mb_suggestions
from backend.musicbrainz_service import _parse_manual_musicbrainz_identifier
from backend.pending_review_store import _AI_PENDING_FILE, _add_to_pending, _ai_pending_lock, _apply_review_origin, _finalize_pending_review_format_policy_rejection, _is_music_root_path, _load_pending_reviews, _mark_pending_review_status, _pending_review_item_is_resolved, _pending_review_matches, _record_ai_review_decision, _remove_pending_review_for_path, _review_path_is_resolved
from backend.serializers import _format_duration, _normalize_review_origin_type, _resolve_import_review_source_path, _review_origin_payload, json_route_result
from app import app  # noqa: E402  (route modules load after app.py defines app)
import uuid
from backend.ai_service import _ai_batch_control_lock, _ai_batch_controls, _ai_batch_initial_state, _ai_batch_load_state, _ai_batch_persist_job_association, _ai_batch_promote_worker, _ai_batch_release_worker, _ai_batch_release_worker_any, _ai_batch_reserve_worker, _ai_batch_validate_worker_handoff, _run_ai_batch_import

# ── ARCH-001 extracted code ──


@app.get("/api/unmatched-tracks")
def unmatched_tracks():
    """Tracks with no mb_trackid whose album also has no mb_albumid.
    Albums that have already been matched to MusicBrainz (mb_albumid set) are excluded
    even if individual track IDs weren't stored."""
    limit = min(int(request.args.get("limit", 300)), 2000)

    try:
        review_data = composite_workflows.get_unmatched_review_items(limit=min(limit, 1000), include_singletons=True)
    except BeetsUnavailableError as ex:
        _app_logger.warning("unmatched_tracks: Beets engine unavailable: %s", ex)
        return jsonify({
            "ok": False,
            "error": "Beets engine unavailable",
            "error_code": "ENGINE_UNAVAILABLE",
        }), 503

    tracks = []
    for a in review_data.get("albums", []):
        aid = int(a["id"])
        items = composite_workflows.find_all_items_by_album_id(aid)
        for item in items:
            if (item.get("mb_trackid") or "").strip():
                continue
            d = item.copy()
            d["mb_trackid"] = ""
            tracks.append(d)
    for s in review_data.get("singletons", []):
        d = s.copy()
        d["mb_trackid"] = ""
        tracks.append(d)

    tracks.sort(key=lambda t: t.get("added", 0), reverse=True)
    return jsonify({"tracks": tracks[:limit], "total": len(tracks)})


@app.get("/api/import/folder-stats")
def import_folder_stats():
    """Quick file-type breakdown of a review-item folder, shown in the
    delete-confirmation dialog so the user can see what they're about to
    remove before confirming a destructive action."""
    raw_path = _s(request.args.get("path") or "").strip()
    if not raw_path:
        return jsonify({"ok": False, "error": "path required"})
    folder, error = _resolve_import_review_folder_path(raw_path, allow_music=True)
    if error or folder is None:
        return jsonify({"ok": False, "error": error or "Invalid review folder path."}), 400
    audio_count = art_count = other_count = 0
    try:
        for entry in folder.rglob("*"):
            if not entry.is_file():
                continue
            ext = entry.suffix.lower()
            if ext in AUDIO_EXT:
                audio_count += 1
            elif ext in _ART_EXTS:
                art_count += 1
            else:
                other_count += 1
    except Exception as ex:
        _app_logger.warning("import_folder_stats failed for %r: %s", raw_path, type(ex).__name__)
        return jsonify({"ok": False, "error": "Could not scan this folder."})
    return jsonify({
        "ok": True, "path": raw_path, "exists": True,
        "audio_count": audio_count, "art_count": art_count, "other_count": other_count,
        "total_count": audio_count + art_count + other_count,
    })


@app.post("/api/import/review-folder/delete")
def delete_import_review_folder():
    payload = request.json or {}
    src_path = _s(payload.get("path")).strip()
    confirmed_wrong_library_folder = bool(
        payload.get("confirmed_wrong_library_folder")
        or payload.get("allow_library_delete")
    )
    try:
        album_id = int(payload.get("album_id") or 0)
    except Exception:
        album_id = 0
    log: List[str] = []

    resolved = Path(src_path).resolve(strict=False)
    missing_mbid_album_match = _library_no_mb_album_matches_folder(album_id, str(resolved))

    if not confirmed_wrong_library_folder and not missing_mbid_album_match:
        if _AI_PENDING_FILE.exists() and not _pending_review_matches(src_path, ""):
            return jsonify({"ok": False, "error": "Folder is not in Pending Review, or a Needs MB ID album row match.", "log": log}), 400

    try:
        result = _delete_review_source_folder(
            src_path,
            log,
            confirmed_wrong_library_folder=confirmed_wrong_library_folder or missing_mbid_album_match,
            album_id=album_id if missing_mbid_album_match else 0,
        )
        return jsonify({"ok": True, **result, "log": log})
    except ValueError as ex:
        return jsonify({"ok": False, "error": str(ex), "log": log}), 400
    except Exception as ex:
        _app_logger.warning("delete_import_review_folder failed for %r: %s", src_path, type(ex).__name__)
        return jsonify({"ok": False, "error": "Could not delete source folder.", "log": log}), 500


# CodeQL #1365: the planner's error can be raw ValueError text with resolved
# paths. Answer one fixed message per refusal kind; the original text is
# logged server-side. The planner's structured ``code`` wins; refusals that
# carry none are told apart by their engine-fixed wording, in this order.
_IMPORT_REVIEW_PLAN_CODE_ERRORS = {
    "import_review_target_contains_library":
        "The cleanup folder contains the music library; choose a folder below the downloads root instead.",
    "import_review_library_delete_refused":
        "A selected file is inside the music library; deleting it needs the explicit library-delete confirmation.",
    "import_review_unsafe_root": "An allowed cleanup root is unsafe; check the downloads root configuration.",
    "invalid_request": "Invalid cleanup request.",
}
# (prefix, suffix, message): matched against the engine's fixed wording at
# both ends, so a user path embedded in the middle never changes the kind.
_IMPORT_REVIEW_PLAN_ERRORS = (
    ("Review path is required.", "", "Review path is required."),
    ("A folder or file path is required.", "", "Review path is required."),
    ("Path contains unsafe encoded characters.", "", "Path contains unsafe encoded characters."),
    ("Symlinks are not permitted: ", "", "Symlinks are not permitted."),
    ("Cannot delete approved root ", " itself.", "Cannot clean up an approved root folder itself."),
    ("Review folder path ", " is inside music library.", "Review folder is inside the music library."),
    ("Target path ", " is outside allowed root boundaries.", "Review folder or file is outside the allowed cleanup roots."),
    ("Source path ", " is outside allowed root boundaries.", "Review folder or file is outside the allowed cleanup roots."),
    ("Invalid review folder path.", "", "Invalid cleanup request."),
)


def _import_review_cleanup_plan_error(plan_res: Dict[str, Any]) -> str:
    by_code = _IMPORT_REVIEW_PLAN_CODE_ERRORS.get(str(plan_res.get("code") or ""))
    if by_code:
        return by_code
    text = str(plan_res.get("error") or "")
    for prefix, suffix, message in _IMPORT_REVIEW_PLAN_ERRORS:
        if text.startswith(prefix) and text.endswith(suffix):
            return message
    return "Failed to create file cleanup plan."


@app.post("/api/import/review-files/cleanup")
def cleanup_import_review_files():
    payload = request.get_json(silent=True) or {}
    folder_path = _s(payload.get("path")).strip()
    review_item_id = _s(payload.get("review_item_id")).strip()
    action = _s(payload.get("action") or "quarantine_rejected").strip().lower()
    raw_files = payload.get("files") if isinstance(payload.get("files"), list) else []
    log: List[str] = []

    if action not in {"quarantine_rejected", "quarantine_duplicate", "delete_rejected", "delete_duplicate"}:
        return jsonify({"ok": False, "error": "Unsupported cleanup action.", "log": log}), 400
    if not folder_path:
        return jsonify({"ok": False, "error": "Review folder path is required.", "log": log}), 400
    if not raw_files:
        return jsonify({"ok": False, "error": "At least one cleanup file is required.", "log": log}), 400

    if not _pending_review_matches(folder_path, review_item_id):
        return jsonify({"ok": False, "error": "Review item does not match the source folder.", "log": log}), 400

    allow_delete = bool(payload.get("allow_delete"))
    # Permanent delete requires allow_delete=true
    plan_req = {
        "path": folder_path,
        "files": raw_files,
        "action": action,
        "allow_delete": allow_delete,
    }
    try:
        plan_res = composite_workflows.plan_import_review_cleanup(plan_req)
        if not plan_res.get("ok"):
            _app_logger.warning("cleanup_import_review_files plan refused for %r: %s", folder_path, plan_res.get("error"))
            return jsonify({"ok": False, "error": _import_review_cleanup_plan_error(plan_res),
                            "code": plan_res.get("code"),
                            "log": log}), 400

        op_id = plan_res.get("operation_id")
        # The operator's explicit cleanup request is the approval; apply
        # records it through a CAS so a concurrent caller cannot apply twice.
        apply_res = composite_workflows.apply_import_review_cleanup(
            op_id, approved_by="operator request (import review file cleanup)")
        if not apply_res.get("ok"):
            return jsonify({"ok": False, "operation_id": op_id, "status": apply_res.get("status"),
                            "code": apply_res.get("code"),
                            "error": apply_res.get("error", "Failed to apply cleanup plan."),
                            "deleted": apply_res.get("deleted", []), "quarantined": apply_res.get("moved", []),
                            "failures": apply_res.get("failures", []),
                            "log": apply_res.get("log", log)}), 400

        deleted = apply_res.get("deleted", [])
        moved = apply_res.get("moved", [])
        skipped = apply_res.get("skipped", [])
        for l in apply_res.get("log", []):
            log.append(f"  {l}")

        # Update pending review item state & audit log
        folder_p = Path(folder_path)
        remaining_audio = 0
        if folder_p.exists() and folder_p.is_dir():
            try:
                for f in folder_p.rglob("*"):
                    if f.is_file() and f.suffix.lower() in AUDIO_EXT:
                        remaining_audio += 1
            except Exception:
                pass

        if remaining_audio == 0:
            try:
                _remove_pending_review_for_path(folder_path, log)
            except Exception as ex:
                log.append(f"  Pending review removal warning: {ex}")
        else:
            try:
                _mark_pending_review_status(folder_path, "files_cleaned_up", note=f"Remaining audio files: {remaining_audio}")
            except Exception:
                pass

        try:
            _record_ai_review_decision(action, folder_path, note=f"Cleaned up files: {len(deleted)} deleted, {len(moved)} quarantined")
        except Exception:
            pass

        return jsonify({
            "ok": True,
            "operation_id": op_id,
            "action": action,
            "quarantined": moved,
            "deleted": deleted,
            "skipped": skipped,
            "quarantined_count": len(moved),
            "deleted_count": len(deleted),
            "skipped_count": len(skipped),
            "log": log,
        })
    except BeetsError as ex:
        # SEC-002 CodeQL repository-wide closure finding: BeetsClient._request()
        # falls back to embedding up to 200 raw response-body characters in
        # this exception's message for any non-JSON/unrecognized engine
        # error response (e.g. an unexpected proxy/framework error page),
        # which could carry stack-trace-shaped text -- never interpolate it
        # directly into a client-facing response (see reimport_disk()'s
        # identical, already-hardened handling of this exact exception type).
        _app_logger.warning("cleanup_import_review_files BeetsError for %r: %s", folder_path, ex)
        return jsonify({"ok": False, "error": "Could not cleanup review files.", "log": log}), 400
    except Exception as ex:
        _app_logger.exception("cleanup_import_review_files failed for %r", folder_path)
        return jsonify({"ok": False, "error": "Could not cleanup review files.", "log": log}), 500


@app.get("/api/recent-imports")
def get_recent_imports():
    """Return the list of recently imported albums (from reimport-disk)."""
    try:
        data = json.loads(_RECENT_IMPORTS_FILE.read_text()) if _RECENT_IMPORTS_FILE.exists() else []
    except Exception:
        data = []
    return jsonify({"imports": data})


@app.get("/api/ai-match-history")
def get_ai_match_history():
    limit = min(int(request.args.get("limit", 50)), 200)
    try:
        data = json.loads(_AI_MATCH_HISTORY_FILE.read_text()) \
               if _AI_MATCH_HISTORY_FILE.exists() else []
    except Exception:
        data = []
    return jsonify({"ok": True, "history": data[:limit]})


@app.post("/api/recent-imports/clear")
def clear_recent_imports():
    """Clear the recent imports log."""
    _RECENT_IMPORTS_FILE.write_text("[]")
    return jsonify({"ok": True})


# ── Import log ────────────────────────────────────────────────────────────────

@app.get("/api/import-log")
def import_log():
    limit = min(int(request.args.get("limit", 300)), 1000)
    try:
        lines = Path(LOG_FILE).read_text(errors="replace").splitlines()
        return jsonify({"ok": True, "lines": lines[-limit:], "total": len(lines)})
    except FileNotFoundError:
        return jsonify({"ok": True, "lines": [], "total": 0})


@app.get("/api/import-skipped")
def import_skipped():
    """Return folder paths that were skipped (no MusicBrainz match) from the beet import log.
    The beet log format for skips is:  skip /path/to/folder
    Multiple folders can appear on one line separated by '; '.
    """
    limit = min(int(request.args.get("limit", 500)), 2000)
    skipped = _import_skipped_items(limit)
    return jsonify({"ok": True, "skipped": skipped, "total": len(skipped)})


def _review_row_search_text(row: Dict[str, Any]) -> str:
    values = [
        row.get("title"), row.get("artist"), row.get("album"), row.get("path"),
        row.get("folder"), row.get("folder_name"), row.get("reason"), row.get("status"),
        row.get("status_key"), row.get("origin_label"), row.get("source_playlist_name"),
        row.get("source_batch_id"), row.get("created_by_workflow"), row.get("mb_albumid"),
        row.get("mb_releasegroupid"),
    ]
    return " ".join(_s(v) for v in values if v).casefold()


def _review_row_has_evidence(row: Dict[str, Any]) -> bool:
    evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
    return bool(evidence.get("top_candidates") or evidence.get("preflight"))


@app.get("/api/import/review-queue")
def import_review_queue():
    limit = min(int(request.args.get("limit", 500)), 5000)
    origin_filter = _normalize_review_origin_type(request.args.get("origin_type"), allow_all=True)
    status_filter = _review_status_key(request.args.get("status") or "all")
    search_filter = _s(request.args.get("search") or "").strip().casefold()
    evidence_only = _s(request.args.get("evidence_only") or "").strip().casefold() in {"1", "true", "yes"}
    rows: List[Dict[str, Any]] = []

    # Collect album IDs already claimed by pending items so library_no_mb rows
    # don't duplicate an album that is also shown as pending_ai.
    pending_reviews = _load_pending_reviews(prune_resolved=False)
    _pending_existing_ids: set = set()
    for _pitem in pending_reviews:
        for _aid in ((_pitem.get("suggestion") or {}).get("existing_album_ids") or []):
            try:
                _pending_existing_ids.add(int(_aid))
            except (TypeError, ValueError):
                pass

    try:
        unmatched_data = composite_workflows.get_unmatched_review_items(limit=limit, offset=0, include_singletons=True)
    except BeetsUnavailableError as ex:
        _app_logger.warning("import_review_queue: Beets engine unavailable: %s", ex)
        return jsonify({
            "ok": False,
            "error": "Beets engine unavailable",
            "error_code": "ENGINE_UNAVAILABLE",
        }), 503
    except BeetsAuthError as ex:
        _app_logger.warning("import_review_queue: Beets engine auth failed: %s", ex)
        return jsonify({
            "ok": False,
            "error": "Beets engine auth failed",
            "error_code": "ENGINE_AUTH_ERROR",
        }), 502
    except BeetsError as ex:
        _app_logger.warning("import_review_queue: Beets engine error: %s", ex)
        return jsonify({
            "ok": False,
            "error": "Beets engine error",
            "error_code": "ENGINE_ERROR",
        }), 502
    except Exception as ex:
        _app_logger.error("Unexpected error in import_review_queue: %s", ex)
        return jsonify({
            "ok": False,
            "error": "Internal error loading review queue",
            "error_code": "INTERNAL_ERROR",
        }), 500

    album_rows = unmatched_data.get("albums", [])
    for r in album_rows:
        if not _is_music_root_path(r.get("first_item_path") or ""):
            continue
        if int(r["id"]) in _pending_existing_ids:
            continue  # already shown as a pending_ai row
        album_folder = _album_folder_for_album_id(int(r["id"] or 0))
        artist = _s(r.get("albumartist"))
        album = _s(r.get("album"))
        rows.append({
            "id": f"album:{r['id']}",
            "type": "library_no_mb",
            "status": "Needs MB ID",
            "status_key": "needs_mb_id",
            "title": album or "(unknown album)",
            "artist": artist,
            "album": album,
            "year": int(r.get("year") or 0),
            "album_id": int(r["id"]),
            "first_item_id": int(r.get("first_item_id") or 0),
            "tracks": int(r.get("tracks") or 0),
            "path": album_folder,
            "folder": str(Path(album_folder).parent) if album_folder else "",
            "folder_name": Path(album_folder).name if album_folder else "",
            "sort_ts": float(r.get("added") or 0),
        })

    # Inject stored batch-AI suggestions into library_no_mb rows
    _suggestions = _load_album_mb_suggestions()
    if _suggestions:
        for row in rows:
            if row.get("type") != "library_no_mb":
                continue
            stored = _suggestions.get(str(row.get("album_id") or ""))
            if not stored:
                continue
            row["mb_albumid"]        = stored.get("mb_albumid", "")
            row["mb_releasegroupid"] = stored.get("mb_releasegroupid", "")
            row["mb_releasegroupurl"] = stored.get("mb_releasegroupurl", "")
            row["confidence"]     = stored.get("confidence", "")
            row["reason"]         = stored.get("reason", "")
            row["mb_valid"]       = bool(stored.get("mb_valid"))
            row["mb_url"]         = stored.get("mb_url", "")
            row["top_candidates"] = stored.get("top_candidates") or []
            row["preflight"]      = stored.get("preflight")

    # Singleton items (already imported, but never grouped into an albums
    # row -- album_id is NULL) lacking a MusicBrainz recording ID. /api/library
    # surfaces these as synthetic one-track "albums" via disk-folder grouping
    # and flags them "Missing MusicBrainz ID", but the album-level query
    # above only scans the `albums` table, which structurally can never
    # contain a singleton -- leaving them permanently invisible here even
    # though Library correctly identifies them as needing review. Reuses
    # the same "library_no_mb" type so existing counts/filters/UI already
    # pick these up; target_kind="item" distinguishes the identification
    # target (a recording, not a release) for correct labeling/actions.
    singleton_rows = unmatched_data.get("singletons", [])
    for r in singleton_rows:
        item_path = _s(r.get("path"))
        if not _is_music_root_path(item_path):
            continue
        abs_path = item_path if item_path.startswith("/") else str(MUSIC_ROOT / item_path)
        title = _s(r.get("title"))
        artist = _s(r.get("artist"))
        albumartist = _s(r.get("albumartist"))
        album = _s(r.get("album"))
        year = int(r.get("year") or 0)
        track_number = int(r.get("track") or 0)
        duration_seconds = float(r.get("length") or 0)
        current_ids = {
            "mb_trackid": _s(r.get("mb_trackid")),
            "mb_albumid": _s(r.get("mb_albumid")),
        }
        local_current = {
            "filename": Path(abs_path).name if abs_path else "",
            "source_path": abs_path,
            "title": title,
            "artist": artist,
            "album": album,
            "albumartist": albumartist,
            "year": str(year) if year else "",
            "track": track_number or "",
            "disc": int(r.get("disc") or 0) or "",
            "duration_seconds": duration_seconds,
            "duration": _format_duration(duration_seconds),
            "mb_trackid": current_ids["mb_trackid"],
            "mb_albumid": current_ids["mb_albumid"],
        }
        rows.append({
            "id": f"item:{int(r['id'])}",
            "type": "library_no_mb",
            "target_kind": "item",
            "status": "Needs recording ID",
            "status_key": "needs_mb_id",
            "missing_id_type": "Recording ID",
            "title": title or Path(abs_path).stem or "(unknown title)",
            "artist": artist,
            "album": album,
            "albumartist": albumartist,
            "year": year,
            "track": track_number or "",
            "duration": _format_duration(duration_seconds),
            "duration_seconds": duration_seconds,
            "mb_trackid": current_ids["mb_trackid"],
            "mb_albumid": current_ids["mb_albumid"],
            "album_id": 0,
            "item_id": int(r["id"]),
            "first_item_id": int(r["id"]),
            "tracks": 1,
            "path": abs_path,
            "folder": str(Path(abs_path).parent) if abs_path else "",
            "folder_name": Path(abs_path).parent.name if abs_path else "",
            "sort_ts": float(r.get("added") or 0),
            "evidence": {
                "missing_id_type": "Recording ID",
                "current": local_current,
                "fingerprint": {"status": "not_checked", "acoustid_status": "not_checked"},
                "recording_candidates": [],
            },
        })

    try:
        skipped_deep_scan = status_filter == "skipped"
        skipped_limit = limit if skipped_deep_scan else min(limit, 100)
        skipped_max_log_lines = 0 if skipped_deep_scan else 2000
        for s in _import_skipped_items(skipped_limit, deep_scan=skipped_deep_scan, max_log_lines=skipped_max_log_lines):
            if skipped_deep_scan and _review_path_is_resolved(s.get("path", "")):
                continue
            rows.append({
                "id": "skipped:" + s["path"],
                "type": "skipped",
                "status": "Skipped",
                "status_key": "skipped",
                "title": s.get("filename") or Path(s.get("path", "")).name,
                "artist": "",
                "album": "",
                "year": 0,
                "path": s.get("path", ""),
                "folder": s.get("folder", ""),
                "tracks": 0,
                "sort_ts": 0,
            })
    except Exception:
        pass

    try:
        for item in pending_reviews:
            item_status = _s(item.get("status") or "Pending AI").strip() or "Pending AI"
            item_status_key = _review_status_key(item_status)
            if item_status_key in {"import_enqueueing", "import_queued"}:
                continue
            s = item.get("suggestion") or {}
            origin_info = _review_origin_payload(
                item.get("path", ""),
                s,
                item=item,
                evidence=item.get("evidence") or {},
            )
            blocked = _review_blocked_metadata(item_status, s.get("reason", ""), s, item.get("evidence") or {})
            rows.append({
                "id": "pending:" + item.get("path", ""),
                "type": "pending_ai",
                "status": item_status,
                "status_key": item_status_key,
                "blocked_reason": blocked.get("reason", ""),
                "blocked_next_action": blocked.get("next_action", ""),
                "title": s.get("album") or item.get("folder_name") or Path(item.get("path", "")).name,
                "artist": s.get("albumartist", ""),
                "album": s.get("album", ""),
                "year": s.get("year") or 0,
                "path": item.get("path", ""),
                "folder": str(Path(item.get("path", "")).parent) if item.get("path") else "",
                "folder_name": item.get("folder_name", ""),
                "confidence": s.get("confidence", ""),
                "reason": s.get("reason", ""),
                "mb_albumid": s.get("mb_albumid", ""),
                "mb_releasegroupid": s.get("mb_releasegroupid", ""),
                "mb_releasegroupurl": s.get("mb_releasegroupurl", ""),
                "mb_url": s.get("mb_url", ""),
                "mb_valid": bool(s.get("mb_valid")),
                "existing_album_ids": s.get("existing_album_ids") or [],
                "existing_album_id": int((s.get("existing_album_ids") or [0])[0] or 0),
                "tracks": 0,
                "sort_ts": float(item.get("added_at") or 0),
                "evidence": item.get("evidence") or {},
                **origin_info,
            })
    except Exception:
        pass

    for row in rows:
        if not row.get("origin_type"):
            _apply_review_origin(
                row,
                _review_origin_payload(
                    row.get("path", ""),
                    row,
                    item=row,
                    evidence=row.get("evidence") or {},
                ),
            )

    rows.sort(key=lambda r: (
        {"pending_ai": 0, "skipped": 1, "library_no_mb": 2}.get(r["type"], 9),
        -float(r.get("sort_ts") or 0),
        str(r.get("title") or "").casefold(),
    ))

    status_rows = [r for r in rows if _review_queue_status_matches(r, status_filter)]
    if evidence_only:
        status_rows = [r for r in status_rows if _review_row_has_evidence(r)]
    if search_filter:
        status_rows = [r for r in status_rows if search_filter in _review_row_search_text(r)]

    origin_counts = Counter(_review_item_origin_type(r) for r in status_rows)
    origin_counts["all"] = len(status_rows)
    filtered_rows = status_rows
    if origin_filter != "all":
        filtered_rows = [r for r in status_rows if _review_item_origin_type(r) == origin_filter]

    counts = Counter(r["type"] for r in filtered_rows)
    counts["all"] = len(filtered_rows)
    return jsonify({
        "ok": True,
        "items": filtered_rows[:limit],
        "total": len(filtered_rows),
        "counts": dict(counts),
        "origin_counts": dict(origin_counts),
    })


@app.post("/api/import/cleanup-stale")
def import_cleanup_stale():
    removed_folder_gone = 0
    removed_resolved = 0
    audit: List[tuple] = []
    kept = []
    try:
        with _ai_pending_lock:
            items = _load_pending_reviews()
            for item in items:
                path = _s((item or {}).get("path", "")).strip()
                if path and not Path(path).exists():
                    audit.append(("cleanup_stale", path, item,
                                  "removed by cleanup-stale: folder missing from disk"))
                    removed_folder_gone += 1
                    continue
                if _pending_review_item_is_resolved(item or {}):
                    audit.append(("cleanup_stale", path, item,
                                  "removed by cleanup-stale: album already resolved in Beets"))
                    removed_resolved += 1
                    continue
                kept.append(item)
            if removed_folder_gone or removed_resolved:
                _AI_PENDING_FILE.write_text(json.dumps(kept, indent=2))
    except Exception as ex:
        _app_logger.warning("Pending-review cleanup-stale failed: %s", type(ex).__name__)
        return jsonify({"ok": False, "error": "Could not clean up pending review."}), 500
    for decision, path, item, note in audit:
        try:
            _record_ai_review_decision(
                decision,
                path,
                item.get("suggestion") or {},
                item.get("evidence") or {},
                note=note,
            )
        except Exception:
            pass
    return jsonify({
        "ok": True,
        "removed_total": removed_folder_gone + removed_resolved,
        "removed_folder_gone": removed_folder_gone,
        "removed_resolved": removed_resolved,
        "remaining": len(kept),
    })


@app.get("/api/candidates/<mb_albumid>/tracks")
def candidate_tracks(mb_albumid: str):
    """Return per-track comparison between a MusicBrainz release and a local folder."""
    folder = request.args.get("folder", "").strip()
    release_group_id = request.args.get("release_group_id", "").strip()
    payload = _candidate_track_comparison_payload(mb_albumid, folder, release_group_id=release_group_id)
    status = 200 if payload.get("ok") else 400
    return jsonify(payload), status


@app.post("/api/import-review/manual-id/validate")
def import_review_manual_id_validate():
    """Resolve a user-entered MusicBrainz UUID/URL through backend-owned validation."""
    payload = request.get_json(silent=True) or {}
    parsed = _parse_manual_musicbrainz_identifier(payload.get("musicbrainz_id") or payload.get("mbid") or "")
    if not parsed.get("ok"):
        return jsonify(parsed), 400
    target_kind = _s(payload.get("target_kind") or "").strip().lower()
    try:
        if target_kind == "item":
            body, status = _manual_review_validate_recording_identifier(parsed, payload)
        else:
            body, status = _manual_review_validate_album_identifier(parsed, payload)
    except Exception as ex:
        _app_logger.error("Manual MusicBrainz validation failed: %s", type(ex).__name__)
        return jsonify({"ok": False, "error": "MusicBrainz validation could not be completed."}), 500
    return jsonify(body), status


@app.post("/api/folders/import-target-preview")
def import_target_preview():
    """Read-only target path preview for Import Review selected matches."""
    payload = request.get_json(silent=True) or {}
    return jsonify(_cached_import_target_preview(payload))


@app.post("/api/import-review/decision")
def import_review_decision():
    """Read-only: the authoritative Import Review action decision (bucket,
    blocked/ready, block reason, next action, action label, source files)
    for each entry ``{"item", "mbid", "selected_match", "target_preview_state"}``
    (ARCH-005; see backend/import_review_decision.py)."""
    payload = request.get_json(silent=True) or {}
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries or len(entries) > 200:
        return jsonify({"ok": False, "error": "entries must be a list of 1-200 decision inputs"}), 400
    if not all(isinstance(entry, dict) and isinstance(entry.get("item"), dict) for entry in entries):
        return jsonify({"ok": False, "error": "each entry needs an item object"}), 400
    return jsonify({"ok": True, "decisions": [_import_review_decision.decide(entry) for entry in entries]})


@app.post("/api/import-review/auto-enqueue-ready")
def import_review_auto_enqueue_ready():
    payload = request.get_json(silent=True) or {}
    try:
        limit = int(payload.get("limit") or 5)
    except Exception:
        limit = 5
    return jsonify(_run_import_review_auto_enqueue_ready_batch(limit))


@app.post("/api/import-review/auto-enqueue-ready/job")
def import_review_auto_enqueue_ready_job():
    payload = request.get_json(silent=True) or {}
    try:
        limit = int(payload.get("limit") or 5)
    except Exception:
        limit = 5

    def _do(log, cancel_event=None):
        _run_import_review_auto_enqueue_ready_batch(limit, log, cancel_event)

    job = jobs.start_python(_do, label="Auto-import ready review items")
    return jsonify({"ok": True, "job_id": job.job_id, "limit": max(1, min(limit, 25))})


@app.get("/api/import-reconciliation/reviews")
def import_reconciliation_reviews():
    """Reconciliation decisions held for review (both files kept)."""
    status = _s(request.args.get("status") or "open").strip().lower()
    records = _import_reconciliation.load_reviews()
    if status != "all":
        records = [r for r in records if _s(r.get("status")) == status]
    return jsonify({"ok": True, "reviews": records[-500:], "count": len(records)})


@app.post("/api/import-reconciliation/reviews/<review_id>/resolve")
def import_reconciliation_resolve(review_id: str):
    """Apply a reviewer's choice through the engine's controlled transactions."""
    payload = request.get_json(silent=True) or {}
    review_id = _s(review_id).strip()
    if not re.fullmatch(r"[0-9a-f]{32}", review_id):
        return jsonify({"ok": False, "error": "invalid review id"}), 400
    try:
        result = _import_reconciliation.resolve_review(review_id, payload.get("choice"), composite_workflows)
    except (BeetsUnavailableError, BeetsError):
        return jsonify({"ok": False, "error": "Beets engine is unavailable.", "code": "beets_unavailable"}), 503
    if result.get("ok"):
        _invalidate_lib_cache()
        return jsonify(result)
    status = {"not_found": 404, "already_resolved": 409, "album_identity_unproven": 409}.get(result.get("code"), 400)
    return jsonify(result), status


@app.post("/api/import-review/auto-enqueue")
def import_review_auto_enqueue():
    payload = request.get_json(silent=True) or {}
    eligibility = evaluate_import_eligibility(payload)
    if not eligibility.get("eligible"):
        reason = "; ".join(eligibility.get("blocking_reasons") or []) or "Import is not eligible to queue."
        return jsonify({"ok": False, "queued": False, "error": reason, "eligibility": eligibility}), 400
    key = _s(eligibility.get("idempotency_key")).strip()
    try:
        with _import_review_auto_lock:
            result = _import_review_start_auto_import(payload, eligibility)
        return jsonify(result)
    except Exception as ex:
        reason = str(ex)
        if _is_music_format_policy_handled_error(reason):
            folder_path = _s(payload.get("path")).strip()
            outcome = _finalize_pending_review_format_policy_rejection(
                folder_path,
                reason,
                idempotency_key=key,
            )
            _import_review_auto_update(key, status=outcome.get("status"), error=outcome.get("note"))
            handled_eligibility = dict(eligibility)
            handled_eligibility["blocking_reasons"] = [outcome.get("note") or _MUSIC_FORMAT_POLICY_REVIEW_NOTE]
            return jsonify({
                "ok": True,
                "queued": False,
                "handled": True,
                "retryable": False,
                "status": outcome.get("status"),
                "pending_review_exists": outcome.get("pending_review_exists"),
                "note": outcome.get("note"),
                "eligibility": handled_eligibility,
            })
        _app_logger.warning("import_review_auto_enqueue failed: %s", type(ex).__name__)
        _import_review_auto_update(key, status="failed", error=reason)
        _mark_pending_review_status(
            _s(payload.get("path")).strip(),
            "auto_enqueue_failed",
            reason,
            idempotency_key=key,
        )
        return jsonify({"ok": False, "error": "Could not queue this import.", "eligibility": eligibility}), 500


@app.post("/api/import-review/auto-enqueue/reconcile")
def import_review_auto_enqueue_reconcile():
    payload = request.get_json(silent=True) or {}
    eligibility = evaluate_import_eligibility(payload)
    key = _s(eligibility.get("idempotency_key") or payload.get("idempotency_key")).strip()
    job = _import_review_auto_job_for_submission(payload, key)
    folder_path = _s(payload.get("path")).strip()
    state = _import_review_auto_state().get(key) if key else {}
    state_job_id = _s((state or {}).get("job_id")).strip() if isinstance(state, dict) else ""
    state_job = jobs.get(state_job_id) if state_job_id else None
    if not job and state_job:
        job = state_job
    if job and job.status == "running":
        _mark_pending_review_status(
            folder_path,
            "import_queued",
            "Import job running.",
            job_id=job.job_id,
            idempotency_key=key,
        )
        return jsonify({
            "ok": True,
            "queued": True,
            "existing_job": True,
            "reconciled": True,
            "job_id": job.job_id,
            "job_status": job.status,
            "eligibility": eligibility,
        })
    if job and job.status in {"failed", "success"}:
        note = _import_review_job_last_line(job) or f"Import job {job.status}."
        if job.status == "failed" and _is_music_format_policy_handled_error(note):
            outcome = _finalize_pending_review_format_policy_rejection(
                folder_path,
                note,
                job_id=job.job_id,
                idempotency_key=key,
            )
            _import_review_auto_update(key, status=outcome.get("status"), job_id=job.job_id, error=outcome.get("note"))
            handled_eligibility = dict(eligibility)
            handled_eligibility["blocking_reasons"] = [outcome.get("note") or _MUSIC_FORMAT_POLICY_REVIEW_NOTE]
            return jsonify({
                "ok": True,
                "queued": False,
                "existing_job": True,
                "reconciled": True,
                "handled": True,
                "job_id": job.job_id,
                "job_status": job.status,
                "retryable": False,
                "status": outcome.get("status"),
                "pending_review_exists": outcome.get("pending_review_exists"),
                "note": outcome.get("note"),
                "eligibility": handled_eligibility,
            })
        status = "remaining_files_review" if job.status == "success" else "auto_enqueue_failed"
        _mark_pending_review_status(
            folder_path,
            status,
            note,
            job_id=job.job_id,
            idempotency_key=key,
        )
        _import_review_auto_update(key, status=job.status, job_id=job.job_id, error=note)
        return jsonify({
            "ok": True,
            "queued": False,
            "existing_job": True,
            "reconciled": True,
            "job_id": job.job_id,
            "job_status": job.status,
            "retryable": job.status in ("failed", "cancelled"),
            "note": note,
            "eligibility": eligibility,
        })
    if state_job_id and not state_job:
        _import_review_auto_update(
            key,
            status="stale_job_missing",
            job_id="",
            stale_job_id=state_job_id,
            error="Previous auto-import job is no longer in the job store; retry enqueue.",
        )
    _mark_pending_review_status(
        folder_path,
        "auto_enqueue_failed",
        "Import enqueue acknowledgement was lost and no active job was found; retry enqueue.",
        idempotency_key=key,
    )
    return jsonify({
        "ok": True,
        "queued": False,
        "reconciled": True,
        "retryable": True,
        "note": "Import enqueue acknowledgement was lost and no active job was found; retry enqueue.",
        "eligibility": eligibility,
    })


@app.post("/api/import/reconcile-job")
def import_reconcile_job():
    payload = request.get_json(silent=True) or {}
    job_id = _s(payload.get("job_id")).strip()
    source_path = _s(payload.get("source_path")).strip()
    review_item_id = _s(payload.get("review_item_id")).strip()
    idempotency_key = _s(payload.get("idempotency_key") or payload.get("auto_import_idempotency_key")).strip()
    if not source_path and review_item_id.startswith("pending:"):
        source_path = review_item_id[len("pending:"):]
    if source_path:
        trusted_source, source_error = _resolve_import_review_source_path(
            source_path,
            allow_music=True,
            expected_type=None,
            require_exists=False,
        )
        if source_error or trusted_source is None:
            return jsonify({"ok": False, "error": source_error or "Source path is not allowed."}), 400
        source_path = str(trusted_source)
    job = _import_review_reconcile_job_lookup(job_id, source_path, review_item_id, idempotency_key)
    if job:
        job_log = list(getattr(job, "log", []) or [])
        last = next((_s(line).strip() for line in reversed(job_log[-20:]) if _s(line).strip()), "")
        meta = getattr(job, "metadata", {}) or {}
        job_source_path = source_path or _s(meta.get("path")).strip()
        if job.status == "failed" and job_source_path and _is_music_format_policy_handled_error(last):
            outcome = _finalize_pending_review_format_policy_rejection(
                job_source_path,
                last,
                job_id=job.job_id,
                idempotency_key=idempotency_key,
            )
            return jsonify({
                "ok": True,
                "job_id": job.job_id,
                "review_item_id": review_item_id,
                "status": outcome.get("status"),
                "handled": True,
                "reconciled": True,
                "retryable": False,
                "step": "format_policy_handled",
                "note": outcome.get("note"),
                "pending_review_exists": outcome.get("pending_review_exists"),
                "log": job_log[-20:],
            })
        return jsonify({
            "ok": True,
            "job_id": job.job_id,
            "review_item_id": review_item_id,
            "status": job.status,
            "reconciled": True,
            "step": "job_store",
            "note": f"Import job {job.status}.",
            "log": job_log[-20:],
        })
    pending = _pending_review_matches(source_path, review_item_id) if source_path else False
    source_exists = bool(source_path and Path(source_path).exists())
    if pending:
        return jsonify({
            "ok": True,
            "job_id": job_id,
            "review_item_id": review_item_id,
            "status": "returned_to_review",
            "reconciled": True,
            "retryable": True,
            "step": "pending_review_exists",
            "note": "Job missing — returned to review.",
            "pending_review_exists": True,
            "source_exists": source_exists,
        })
    if source_path and not source_exists:
        return jsonify({
            "ok": True,
            "job_id": job_id,
            "review_item_id": review_item_id,
            "status": "likely_completed",
            "reconciled": True,
            "step": "source_missing",
            "note": "Source folder is gone and no pending review remains; import likely completed.",
            "pending_review_exists": False,
            "source_exists": False,
        })
    return jsonify({
        "ok": True,
        "job_id": job_id,
        "review_item_id": review_item_id,
        "status": "import_job_missing",
        "reconciled": True,
        "retryable": True,
        "step": "job_not_found",
        "note": "No active import job was found; item can return to the review queue.",
        "pending_review_exists": False,
        "source_exists": source_exists,
    })


@app.post("/api/import-review/revalidate")
def import_review_revalidate():
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get("review_item_ids") or ([] if not payload.get("review_item_id") else [payload.get("review_item_id")])
    if isinstance(raw_ids, str):
        raw_ids = [raw_ids]
    review_ids = {_s(value).strip() for value in raw_ids if _s(value).strip()}
    explicit_all = payload.get("all") is True or payload.get("review_all") is True
    auto_enqueue = payload.get("auto_enqueue") is True
    limit = min(int(payload.get("limit") or 2000), 2000)
    if not review_ids and not explicit_all:
        return jsonify({
            "ok": False,
            "error": "review_item_ids required unless all=true",
            "reviewed_count": 0,
            "updated_count": 0,
            "queued_count": 0,
            "failed_count": 0,
            "items": [],
        }), 400
    pending = _load_pending_reviews()
    results: List[Dict[str, Any]] = []
    reviewed = updated = queued = failed = 0

    for item in pending:
        if reviewed >= limit:
            break
        item = item if isinstance(item, dict) else {}
        path = _s(item.get("path")).strip()
        review_item_id = f"pending:{path}" if path else ""
        if review_ids and review_item_id not in review_ids and path not in review_ids:
            continue
        reviewed += 1
        suggestion = item.get("suggestion") if isinstance(item.get("suggestion"), dict) else {}
        release_group_id = _extract_mb_uuid(
            _s(suggestion.get("mb_releasegroupid") or item.get("mb_releasegroupid"))
        )
        representative_release_id = _extract_mb_uuid(
            _s(suggestion.get("representative_mb_albumid") or suggestion.get("mb_albumid"))
        )
        if not path or (not representative_release_id and not release_group_id):
            failed += 1
            results.append({
                "ok": False,
                "review_item_id": review_item_id,
                "path": path,
                "error": "Review item has no source path or MusicBrainz Release Group ID.",
            })
            continue
        comparison = _candidate_track_comparison_payload(
            representative_release_id,
            path,
            release_group_id=release_group_id,
        )
        if not comparison.get("ok"):
            failed += 1
            results.append({
                "ok": False,
                "review_item_id": review_item_id,
                "path": path,
                "error": comparison.get("error") or "Track comparison failed.",
            })
            continue
        representative_release_id = _extract_mb_uuid(
            _s(comparison.get("representative_release_id") or comparison.get("mb_albumid") or representative_release_id)
        )
        acoustic_preflight = _run_ai_release_preflight(
            path,
            representative_release_id,
            existing_album_id=int((suggestion.get("existing_album_ids") or [0])[0] or 0),
        ) if representative_release_id else None
        preflight = _import_review_revalidation_preflight(comparison, acoustic_preflight)
        selected_match = _import_review_build_revalidated_match(item, comparison, preflight)
        note = selected_match.get("preflight_reason", "")
        if _update_pending_review_revalidation(path, selected_match, preflight, note):
            updated += 1
        auto_payload = {
            "path": path,
            "review_item_id": review_item_id,
            "mb_albumid": selected_match.get("representative_release_id", ""),
            "mb_releasegroupid": selected_match.get("release_group_id", ""),
            "artist": selected_match.get("artist", ""),
            "album": selected_match.get("album", ""),
            "year": selected_match.get("year", ""),
            "existing_album_id": int((suggestion.get("existing_album_ids") or [0])[0] or 0),
            "track_mapping": selected_match.get("track_mapping") or [],
            "selected_match": selected_match,
            "confidence_score": selected_match.get("confidence_score"),
            "ai_suggestion": suggestion,
        }
        eligibility = evaluate_import_eligibility(auto_payload)
        result = {
            "ok": True,
            "review_item_id": review_item_id,
            "path": path,
            "selected_match": selected_match,
            "eligibility": eligibility,
            "queued": False,
        }
        if auto_enqueue and eligibility.get("eligible"):
            try:
                with _import_review_auto_lock:
                    queue_result = _import_review_start_auto_import(auto_payload, eligibility)
                result.update({
                    "queued": bool(queue_result.get("queued")),
                    "job_id": queue_result.get("job_id", ""),
                    "existing_job": bool(queue_result.get("existing_job")),
                })
                queued += 1 if result.get("queued") else 0
            except Exception as ex:
                failed += 1
                result.update({"ok": False, "error": str(ex), "queued": False})
        results.append(result)

    return jsonify({
        "ok": True,
        "reviewed_count": reviewed,
        "updated_count": updated,
        "queued_count": queued,
        "failed_count": failed,
        "items": results,
    })


@app.post("/api/folders/ai-suggest")
def ai_suggest_folder():
    """Identify an unimported folder using AI + MusicBrainz.
    Body: { "path": "/downloads/Artist/Album (Year)" }
    Returns: { ok, suggestion: { mb_albumid, album, albumartist, year, label, country, confidence, reason, mb_valid, mb_url } }
    """
    payload = request.get_json(silent=True) or {}
    folder_path = payload.get("path", "").strip()
    return jsonify(_ai_suggest_folder_internal(folder_path))


@app.post("/api/folders/import-with-id")
def import_folder_with_id():
    """Two-step import for a skipped folder:"""
    body, status = start_folder_import_with_id(request.get_json(silent=True) or {})
    return json_route_result(body, status)


_ai_batch_skip_event = threading.Event()


@app.get("/api/ai-pending-review")
def get_ai_pending_review():
    return jsonify({"ok": True, "items": _load_pending_reviews()})


@app.get("/api/ai-review-decisions")
def get_ai_review_decisions():
    limit = min(int(request.args.get("limit", 100)), 500)
    try:
        data = (
            json.loads(_AI_REVIEW_DECISIONS_FILE.read_text())
            if _AI_REVIEW_DECISIONS_FILE.exists() else []
        )
        if not isinstance(data, list):
            data = []
    except Exception:
        data = []
    return jsonify({"ok": True, "items": data[:limit]})


@app.post("/api/ai-pending-review")
def add_ai_pending_review():
    payload = request.get_json(silent=True) or {}
    folder_path = payload.get("path", "").strip()
    if not folder_path:
        return jsonify({"ok": False, "error": "path required"}), 400
    suggestion = payload.get("suggestion") or {}
    allow_existing = bool(payload.get("allow_existing") or suggestion.get("allow_existing_review"))
    origin = _review_origin_payload(
        folder_path,
        suggestion,
        origin=payload or {"origin_type": "manual_import"},
        evidence=payload.get("evidence") or payload.get("review_evidence") or None,
    )
    if _normalize_review_origin_type(origin.get("origin_type")) == "unknown":
        origin = {**origin, "origin_type": "manual_import", "origin_label": "Manual", "created_by_workflow": "manual_import"}
    added = _add_to_pending(folder_path, suggestion, allow_existing=allow_existing, origin=origin)
    return jsonify({"ok": True, "added": bool(added), "skipped_existing": not bool(added)})


@app.delete("/api/ai-pending-review")
def clear_ai_pending_review():
    payload = request.get_json(silent=True) or {}
    folder_path = payload.get("path", "").strip()
    removed_items = []
    with _ai_pending_lock:
        items = _load_pending_reviews()
        if folder_path:
            removed_items = [i for i in items if i.get("path") == folder_path]
            items = [i for i in items if i.get("path") != folder_path]
        else:
            removed_items = list(items)
            items = []
        _AI_PENDING_FILE.write_text(json.dumps(items, indent=2))
    for item in removed_items:
        _record_ai_review_decision(
            "cleared",
            item.get("path", ""),
            item.get("suggestion") or {},
            item.get("evidence") or {},
            note="manual pending review clear",
        )
    return jsonify({"ok": True})


@app.post("/api/ai-batch/reconcile-artwork")
def ai_batch_reconcile_artwork():
    """Reconcile a persisted AI-batch folder's artwork_status/artwork_retryable
    against the album's actual current artwork state. Called by the Intake
    UI after the manual artwork-retry job (POST /api/albums/<aid>/fetch-art,
    started from a folder with artwork_retryable=true) reaches a terminal
    state -- job creation alone never proves success, so this re-reads the
    album and verifies real on-disk art via the same _album_art_status()
    check the rest of the art-repair system already uses, rather than
    trusting a client-claimed outcome.

    batch_job_id, folder_id, and artwork_job_id are all required -- there is
    no "latest batch" fallback, since guessing the wrong batch or the wrong
    retry job would silently reconcile the wrong folder. The artwork job is
    validated against the job store (must exist, must be this folder's
    album, must be terminal) rather than trusted from the client, and the
    album's real on-disk artwork always wins over what the job claims: a job
    that reports failure/cancellation/timeout while art is verifiably
    present still reconciles to "fetched"."""
    payload = request.get_json(silent=True) or {}
    batch_job_id = _s(payload.get("batch_job_id")).strip()
    folder_id = _s(payload.get("folder_id")).strip()
    artwork_job_id = _s(payload.get("artwork_job_id")).strip()
    if not batch_job_id:
        return jsonify({"ok": False, "error": "batch_job_id is required", "code": "batch_job_id_required"}), 400
    if not folder_id:
        return jsonify({"ok": False, "error": "folder_id is required", "code": "folder_id_required"}), 400
    if not artwork_job_id:
        return jsonify({"ok": False, "error": "artwork_job_id is required", "code": "artwork_job_id_required"}), 400

    state = _ai_batch_find_state(batch_job_id)
    if not state:
        return jsonify({"ok": False, "error": "AI batch state not found"}), 404
    folder = (state.get("folder_states") or {}).get(folder_id)
    if not folder:
        return jsonify({"ok": False, "error": "Folder not found in this batch"}), 404

    try:
        album_id = int(folder.get("album_id") or 0)
    except Exception:
        album_id = 0

    if not album_id:
        _ai_batch_mark_folder(
            state, folder_id,
            artwork_status="skipped_no_album",
            artwork_retryable=False,
        )
        _ai_batch_write_state(state)
        return jsonify({"ok": True, "state": _ai_batch_public_state(state)})

    job = jobs.get(artwork_job_id)
    if job is None:
        return jsonify({"ok": False, "error": "Artwork job not found", "code": "artwork_job_not_found"}), 404
    job_metadata = getattr(job, "metadata", {}) or {}
    try:
        job_album_id = int(job_metadata.get("album_id") or 0)
    except Exception:
        job_album_id = 0
    if _s(job_metadata.get("type")) != "album_art_repair" or job_album_id != album_id:
        return jsonify({"ok": False, "error": "Artwork job does not match this folder's album", "code": "artwork_job_mismatch"}), 400
    if job.status == "running":
        return jsonify({"ok": False, "error": "Artwork job has not finished yet", "code": "artwork_job_not_terminal"}), 409

    try:
        art_status = _album_art_status(album_id)
        has_art = bool(art_status and art_status.get("has_local_art"))
    except Exception as ex:
        # Fail closed: verification itself failing is never treated as
        # success, and the exception text is never returned or persisted
        # raw -- only ever through the shared redaction helper.
        log_note = _redact_security_text(ex)
        has_art = False
        _ai_batch_mark_folder(state, folder_id, last_error=log_note)

    if has_art:
        artwork_status = "fetched"
        artwork_retryable = False
    else:
        job_state = getattr(job, "state", {}) or {}
        terminal_outcome = _s(job_state.get("terminal_outcome") or "")
        if terminal_outcome == "cancelled":
            artwork_status = "cancelled"
        elif terminal_outcome == "timed_out":
            artwork_status = "timed_out"
        else:
            artwork_status = "failed"
        artwork_retryable = True

    _ai_batch_mark_folder(
        state, folder_id,
        artwork_status=artwork_status,
        artwork_retryable=artwork_retryable,
    )
    _ai_batch_write_state(state)
    return jsonify({"ok": True, "state": _ai_batch_public_state(state)})


@app.post("/api/ai-batch-skip")
def ai_batch_skip():
    payload = request.get_json(silent=True) or {}
    ident = _s(payload.get("batch_job_id") or payload.get("job_id")).strip()
    state = _ai_batch_find_state(ident) if ident else _ai_batch_latest_state()
    if not state:
        _ai_batch_skip_event.set()
        return jsonify({"ok": True, "legacy": True})

    batch_job_id = state.get("batch_job_id") or ident
    control = _ai_batch_control(batch_job_id)
    folder_id = _s(payload.get("folder_id")).strip()
    if folder_id:
        control["skip_folder_ids"].add(folder_id)
    elif payload.get("skip_stale"):
        now = time.time()
        for fid, folder in (state.get("folder_states") or {}).items():
            started = float(folder.get("ai_suggest_started_at") or 0)
            if folder.get("status") == "ai_running" and started and now - started > _AI_BATCH_AI_TIMEOUT:
                control["skip_folder_ids"].add(fid)
    else:
        control["skip_current"] = True
    _ai_batch_skip_event.set()
    return jsonify({"ok": True, "batch_job_id": batch_job_id})


@app.post("/api/ai-batch-pause")
def ai_batch_pause():
    payload = request.get_json(silent=True) or {}
    ident = _s(payload.get("batch_job_id") or payload.get("job_id")).strip()
    state = _ai_batch_find_state(ident) if ident else _ai_batch_latest_state()
    if not state:
        return jsonify({"ok": False, "error": "No active AI batch found"}), 404
    control = _ai_batch_control(state.get("batch_job_id") or ident)
    control["pause"] = True
    state["status"] = "pausing"
    state["current_step"] = "pause requested"
    _ai_batch_commit(state, None)
    return jsonify({"ok": True, "batch_job_id": state.get("batch_job_id"), "state": _ai_batch_public_state(state)})


@app.post("/api/ai-batch-stop")
def ai_batch_stop():
    payload = request.get_json(silent=True) or {}
    ident = _s(payload.get("batch_job_id") or payload.get("job_id")).strip()
    state = _ai_batch_find_state(ident) if ident else _ai_batch_latest_state()
    if not state:
        return jsonify({"ok": False, "error": "No active AI batch found"}), 404
    batch_job_id = state.get("batch_job_id") or ident
    control = _ai_batch_control(batch_job_id)
    control["pause"] = True
    job_id = _s(state.get("job_id") or ident)
    job = jobs.get(job_id) if job_id else None
    if job and job.status == "running":
        try:
            job.kill()
        except Exception:
            pass
    for fid, folder in (state.get("folder_states") or {}).items():
        if folder.get("status") in _AI_BATCH_UNFINISHED_FOLDER_STATUSES:
            _ai_batch_mark_folder(state, fid, status="skipped", current_step="canceled by user", failure_reason="batch canceled")
    state["status"] = "canceled"
    state["current_step"] = "canceled by user"
    state["completed_at"] = time.time()
    state["last_error"] = ""
    _ai_batch_commit(state, getattr(job, "update_state", None) if job else None)
    return jsonify({"ok": True, "batch_job_id": batch_job_id, "state": _ai_batch_public_state(state)})


@app.get("/api/ai-batch-import/status")
def ai_batch_import_status():
    ident = _s(request.args.get("batch_job_id") or request.args.get("job_id")).strip()
    state = _ai_batch_find_state(ident) if ident else _ai_batch_latest_state()
    if not state:
        # Distinguish "the worker job is still known but its durable state is
        # gone" from "there is no trace of this batch at all" so the frontend
        # can stop showing a false Running state instead of freezing on it.
        job = jobs.get(ident) if ident else None
        if job is not None:
            return jsonify({
                "ok": False,
                "error": "Batch state incomplete. Recovery available.",
                "recoverable": False,
                "reason": "child_missing",
            }), 404
        return jsonify({
            "ok": False,
            "error": "Batch job no longer exists.",
            "recoverable": False,
            "reason": "not_found",
        }), 404
    state = _ai_batch_reconcile_state(state)
    return jsonify({"ok": True, "state": _ai_batch_public_state(state)})


@app.post("/api/ai-batch-import/recover")
def ai_batch_import_recover():
    payload = request.get_json(silent=True) or {}
    ident = _s(payload.get("batch_job_id") or payload.get("job_id")).strip()
    retry_failed = bool(payload.get("retry_failed"))
    state = _ai_batch_find_state(ident) if ident else _ai_batch_latest_state()
    if not state:
        return jsonify({"ok": False, "error": "No recoverable AI batch found"}), 404
    state = _ai_batch_reconcile_state(state)
    source_path = _s(state.get("source_path") or payload.get("path") or str(DOWNLOADS_ROOT)).strip()
    batch_job_id = state.get("batch_job_id") or ident
    # Route-level optimization, not the correctness guarantee: this early
    # return avoids the retryable/terminal-status checks and a redundant
    # _start_ai_batch_job call when a worker is already known (via the
    # process-local active-worker registry -- see _ai_batch_active_workers)
    # to be running for this batch_job_id. The actual duplicate-start
    # protection lives in _start_ai_batch_job's own _ai_batch_reserve_worker
    # call below, so it holds even if this check is ever skipped, stale, or
    # bypassed by a future caller.
    #
    # worker_alive alone is not enough to claim reconnected=True: it is
    # True for a startup-reserved-but-unpromoted entry too (see
    # _ai_batch_reconcile_state), which has no real job_id yet. Require an
    # actually-promoted job_id; an unpromoted-but-registered batch defers to
    # _ai_batch_reconnect_response's own truthful, bounded-poll response
    # instead of reporting success with batch_job_id standing in for a
    # job_id that doesn't exist.
    active_job_id = _ai_batch_active_worker_job_id(batch_job_id)
    if active_job_id:
        return jsonify({"ok": True, "job_id": active_job_id, "batch_job_id": batch_job_id, "state": _ai_batch_public_state(state), "reconnected": True})
    if _ai_batch_worker_registered(batch_job_id):
        return _ai_batch_reconnect_response(batch_job_id)
    retryable = int(state.get("folders_retryable") or 0)
    if retry_failed and retryable <= 0:
        return jsonify({"ok": True, "job_id": state.get("job_id") or batch_job_id, "batch_job_id": batch_job_id, "state": _ai_batch_public_state(state), "reconnected": True})
    if state.get("status") in _AI_BATCH_TERMINAL_STATUSES and not retry_failed:
        return jsonify({"ok": True, "job_id": state.get("job_id") or batch_job_id, "batch_job_id": batch_job_id, "state": _ai_batch_public_state(state), "reconnected": True})
    return _start_ai_batch_job(source_path, recover_batch_job_id=batch_job_id, retry_failed=retry_failed)


@app.post("/api/ai-batch-import")
def start_ai_batch_import():
    """Start or reconnect the durable AI batch import worker."""
    payload = request.get_json(silent=True) or {}
    recover_batch_job_id = _s(payload.get("recover_batch_job_id") or payload.get("batch_job_id")).strip()
    if recover_batch_job_id:
        state = _ai_batch_find_state(recover_batch_job_id)
        if not state:
            return jsonify({"ok": False, "error": "AI batch state not found"}), 404
        # Stored job state is untrusted at execution time (it can be old,
        # or a recover/retry call can race a state file edited between
        # requests) -- revalidate exactly like a fresh caller-supplied path,
        # not "already validated when queued".
        scan_path = _s(state.get("source_path") or payload.get("path") or str(DOWNLOADS_ROOT)).strip()
    else:
        scan_path = _s(payload.get("path") or str(DOWNLOADS_ROOT)).strip()

    if not scan_path:
        return jsonify({"ok": False, "error": "Import source path is required"}), 400
    # scan_path feeds _ai_batch_find_audio_dirs(), which now discovers
    # candidate folders via the real engine (composite_workflows.discover_import_sources()),
    # not a local os.walk() -- it must still be confined to the same
    # trusted import-source roots as the rest of the import-review/AI-suggest
    # flow (a fast, local, pure-string root-containment check), but
    # require_exists=False for the same reason import_folder_with_id() was
    # already fixed to pass it (Wave 25 Docker acceptance round, found by
    # actually exercising the real two-service deployment): this process
    # (beets-web-manager) has no filesystem mount for the downloads root or
    # the library at all -- only the engine container does. A local
    # existence check here made every real /api/ai-batch-import call fail
    # with "Source path does not exist" regardless of whether the folder
    # was genuinely there. The engine's own discover_import_sources() call
    # (inside _ai_batch_find_audio_dirs) is the real, disk-backed
    # existence check.
    trusted_scan_path, scan_path_error = _resolve_import_review_source_path(
        scan_path, allow_music=True, expected_type="dir", require_exists=False,
    )
    if scan_path_error or trusted_scan_path is None:
        return jsonify({"ok": False, "error": scan_path_error or f"Path not found: {scan_path}"}), 400
    scan_path = str(trusted_scan_path)
    if not _ai_api_key():
        return jsonify({"ok": False, "error": "AI is not configured (set OPENAI_API_KEY, OPENROUTER_API_KEY, or AI_API_KEY)"}), 400

    if not recover_batch_job_id:
        latest = _ai_batch_latest_state()
        if latest and _s(latest.get("source_path")) == scan_path:
            latest = _ai_batch_reconcile_state(latest)
            status = _s(latest.get("status"))
            if status not in _AI_BATCH_TERMINAL_STATUSES:
                _ai_batch_commit(latest, None, heartbeat=False)
                return jsonify({
                    "ok": True,
                    "job_id": latest.get("job_id") or latest.get("batch_job_id"),
                    "batch_job_id": latest.get("batch_job_id"),
                    "state": _ai_batch_public_state(latest),
                    "reconnected": True,
                })

    return _start_ai_batch_job(scan_path, recover_batch_job_id=recover_batch_job_id)


@app.post("/api/import")
def start_import():
    payload  = request.get_json(silent=True) or {}
    path_raw = payload.get("path", str(DOWNLOADS_ROOT))
    validated_path, path_error = _resolve_import_source_path(path_raw)
    if path_error:
        return jsonify({"ok": False, "error": path_error}), 400
    path     = str(validated_path)
    fallback = payload.get("fallback", "asis")   # asis | skip
    write    = payload.get("write", True)
    move     = payload.get("move", False)
    noincremental = payload.get("reimport", False)   # "reimport" key kept for JS compat
    search_id     = payload.get("search_id", "").strip()
    # Auto-create directory so import always starts
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    if not Path(path).exists():
        return jsonify({"ok": False, "error": f"Path not found: {path}"})
    preserve_torrent_source = _preserve_torrent_source_path(path)
    if preserve_torrent_source and move:
        return jsonify({
            "ok": False,
            "error": (
                "Refusing move import from torrent source. "
                "Use copy import or set ALLOW_TORRENT_SOURCE_MOVE=1 only if you "
                "intend to remove qBittorrent source files."
            ),
        }), 400
    label = f"Import: {Path(path).name or path}"
    if search_id:
        label += f" [{search_id[:8]}…]"

    def _do(log, cancel_event=None):
        _validate_import_source_audio(path, log, reject_downloads=True)
        beets_options = {"quiet_fallback": fallback, "copy": preserve_torrent_source}
        if search_id:
            beets_options["search_id"] = search_id
        res = composite_workflows.reimport_source(path, beets_options=beets_options, timeout=300.0)
        if not res.get("ok"):
            raise RuntimeError(f"Engine import failed: {res.get('error', 'reimport_source failed')}")
        combined = ""
        _delete_if_already_in_library(path, combined, log)
        _invalidate_lib_cache()

    job = jobs.start_python(_do, label=label)
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/import/preflight")
def import_preflight():
    payload = request.get_json(silent=True) or {}
    path_raw = (payload.get("path") or str(DOWNLOADS_ROOT)).strip()
    scan_path, path_error = _resolve_import_source_path(path_raw)
    if path_error:
        return jsonify({"ok": False, "error": path_error}), 400
    if not scan_path.exists() or not scan_path.is_dir():
        return jsonify({"ok": False, "error": f"Path not found: {scan_path}"})

    root_res = scan_path
    folder_rows: List[Dict[str, Any]] = []
    total_audio = 0
    unsupported = 0
    empty_dirs = 0

    try:
        for dirpath, dirnames, filenames in os.walk(scan_path):
            audio_here = sum(
                1 for f in filenames
                if Path(f).suffix.lower() in AUDIO_EXT
            )
            unsupported += sum(
                1 for f in filenames
                if f and not f.startswith(".") and Path(f).suffix.lower() not in AUDIO_EXT
            )
            if not filenames and not dirnames:
                empty_dirs += 1
            if audio_here:
                p = Path(dirpath)
                total_audio += audio_here
                folder_rows.append({
                    "path": str(p),
                    "name": p.name,
                    "audio_files": audio_here,
                })
    except Exception as ex:
        _app_logger.warning("Could not scan path: %s", type(ex).__name__)
        return jsonify({"ok": False, "error": "Could not scan path."})

    tracked_dirs: set = set()
    try:
        paths = composite_workflows.list_distinct_item_paths()
        root_s = str(root_res)
        for raw_path in paths:
            p = _s(raw_path)
            if p and not p.startswith("/"):
                p = str(MUSIC_ROOT / p)
            try:
                item_path = Path(p).resolve(strict=False)
            except Exception:
                continue
            try:
                item_path.relative_to(root_res)
            except Exception:
                continue
            cur = item_path.parent
            while True:
                tracked_dirs.add(str(cur))
                if str(cur) == root_s or cur == cur.parent:
                    break
                cur = cur.parent
    except Exception:
        tracked_dirs = set()

    already = 0
    for row in folder_rows:
        if str(Path(row["path"]).resolve(strict=False)) in tracked_dirs:
            row["already_in_library"] = True
            already += 1
        else:
            row["already_in_library"] = False

    pending = 0
    try:
        for item in _load_pending_reviews():
            p = Path(item.get("path") or "").resolve(strict=False)
            try:
                p.relative_to(root_res)
                pending += 1
            except Exception:
                pass
    except Exception:
        pass

    artist_groups = []
    try:
        artist_groups = _scan_artist_folder_groups(str(scan_path))
    except Exception:
        artist_groups = []

    folder_rows = sorted(
        folder_rows,
        key=lambda r: (not r.get("already_in_library", False), r["path"].casefold())
    )
    return jsonify({
        "ok": True,
        "path": str(scan_path),
        "audio_files": total_audio,
        "audio_folders": len(folder_rows),
        "already_in_library_folders": already,
        "pending_review": pending,
        "unsupported_files": unsupported,
        "empty_dirs": empty_dirs,
        "artist_folder_groups": len(artist_groups),
        "folders": folder_rows[:100],
    })


def _ai_batch_reconnect_response(batch_job_id: str):
    """Build a response for a caller that lost the race to
    _ai_batch_reserve_worker. The winner may not have been promoted to a
    real job_id yet (a brief window between winning the reservation and
    _ai_batch_persist_job_association/_ai_batch_promote_worker). Poll the
    active-worker registry -- not stale batch JSON -- briefly, so a losing
    caller's response carries the real job_id whenever this call directly
    observes a promoted registry entry.

    Successful reconnection requires directly observing a promoted, live
    registry entry for this batch during the bounded poll below.
    Persisted state["job_id"], JobStore job existence, or a JobStore
    status of "success" do NOT prove a given JobStore job is *this*
    startup attempt: any of those could be a stale association left by an
    older successful run of the same batch_job_id, or a job that belongs
    to a different batch entirely if state ever got crossed. Proving
    current-attempt identity would need a durable per-start token --
    a larger persistence/CAS change tracked as future debt (see
    ARCH-010), not implemented here. So this function never infers success
    from anything persisted; it only trusts what it directly observes in
    the live registry.

    Three distinct outcomes:
      - job_id observed within the bound: ok=True, reconnected=True.
      - still startup-reserved (unpromoted) when the bound expires: a
        truthful, retryable "still starting" response.
      - the registry entry disappeared before this call ever observed a
        promoted job_id: this attempt's outcome cannot be proven from
        here, so a truthful, retryable "unconfirmed" response -- the
        caller should refresh normal batch status to resolve it, rather
        than being told either a false success or a false failure."""
    job_id = ""
    startup_disappeared = False
    deadline = time.time() + 2.0
    while time.time() < deadline:
        job_id = _ai_batch_active_worker_job_id(batch_job_id)
        if job_id:
            break
        if not _ai_batch_worker_registered(batch_job_id):
            startup_disappeared = True
            break
        time.sleep(0.02)

    if job_id:
        existing = _ai_batch_find_state(batch_job_id)
        if existing:
            existing = _ai_batch_reconcile_state(existing)
        return jsonify({
            "ok": True,
            "job_id": job_id,
            "batch_job_id": batch_job_id,
            "state": _ai_batch_public_state(existing) if existing else {},
            "reconnected": True,
        })
    if startup_disappeared:
        startup_message = (
            "The startup reservation ended before an active worker could be confirmed. "
            "Refresh batch status and retry if needed."
        )
        return jsonify({
            "ok": False,
            "reconnected": False,
            "retryable": True,
            "startup_unconfirmed": True,
            "batch_job_id": batch_job_id,
            "message": startup_message,
            "error": startup_message,
        }), 409
    return jsonify({
        "ok": False,
        "reconnected": False,
        "retryable": True,
        "startup_in_progress": True,
        "batch_job_id": batch_job_id,
        "error": "AI batch worker is still starting; retry shortly.",
    }), 409


def _start_ai_batch_job(scan_path: str, recover_batch_job_id: str = "", *, retry_failed: bool = False):
    # Authoritative gate, not merely a route-level convenience: scan_path
    # reaches _ai_batch_find_audio_dirs(), which queues every folder the
    # engine discovers for AI-driven import review. Both callers of this
    # function (a fresh /api/ai-batch-import request and
    # /api/ai-batch-import/recover, which can source scan_path from a
    # stored job-state file rather than the current request) must go
    # through this check -- stored state is untrusted at execution time
    # just like a fresh request body. require_exists=False for the same
    # reason as the sibling check in start_ai_batch_import() above: this
    # process has no local mount for the downloads root or the library,
    # so a local existence check here always fails regardless of whether
    # the path is genuinely valid -- the engine-side discovery call is the
    # real, disk-backed check.
    trusted_scan_path, scan_path_error = _resolve_import_review_source_path(
        scan_path, allow_music=True, expected_type="dir", require_exists=False,
    )
    if scan_path_error or trusted_scan_path is None:
        return jsonify({"ok": False, "error": scan_path_error or f"Path not found: {scan_path}"}), 400
    scan_path = str(trusted_scan_path)

    batch_job_id = recover_batch_job_id or uuid.uuid4().hex

    # Atomic check-then-start protection: _ai_batch_reserve_worker is the
    # sole arbiter of "is a worker already starting/running for this batch,"
    # independent of any (possibly stale) state-file read, and remains
    # authoritative for the worker's entire lifetime (see
    # _ai_batch_active_workers' docstring). Two threads calling this
    # function concurrently for the same batch_job_id -- e.g. a
    # double-clicked retry, a retried frontend request, or a second recover
    # arriving after this call already returned but before the worker it
    # started has reconciled/committed anything -- must result in exactly
    # one jobs.start_python() call; every loser reconnects instead.
    if not _ai_batch_reserve_worker(batch_job_id):
        return _ai_batch_reconnect_response(batch_job_id)

    # Explicit startup handoff state, owned by this one _start_ai_batch_job
    # invocation and closed over by its worker thread below. Two separate
    # events rather than a single flag: handoff_ready alone can't
    # distinguish "the wait timed out" from "the wait was signaled because
    # startup succeeded" from "the wait was signaled because startup was
    # explicitly aborted" -- the worker must tell all three apart before it
    # is ever allowed to call _run_ai_batch_import. job_id_holder carries
    # the allocated job_id (set once, immediately after jobs.start_python()
    # returns) -- its mere presence is NOT proof that startup succeeded
    # (startup can still fail after this point); the worker separately
    # re-checks the registry itself (_ai_batch_active_worker_job_id) as the
    # actual proof that this job_id was durably promoted.
    job_id_holder: Dict[str, str] = {}
    handoff_ready = threading.Event()
    startup_aborted = threading.Event()

    def _do(log, cancel_event=None, update_state=None):
        signaled = handoff_ready.wait(timeout=10)
        owned_job_id = job_id_holder.get("job_id", "")
        try:
            _ai_batch_validate_worker_handoff(
                batch_job_id, owned_job_id,
                signaled=signaled, aborted=startup_aborted.is_set(),
            )
            # After the in-process reservation: one worker per batch across
            # processes and restarts too.
            with job_contract.held("ai-batch-" + job_contract.slug(batch_job_id), log=log,
                                   cancel_event=cancel_event, update_state=update_state):
                return _run_ai_batch_import(
                    batch_job_id, scan_path, log, cancel_event, update_state,
                    recover=bool(recover_batch_job_id), retry_failed=retry_failed, job_id=owned_job_id,
                )
        finally:
            # Ownership-safe regardless of which startup outcome applies:
            # releases whichever token (the promoted job_id, or the
            # unpromoted None reservation) this worker actually holds. See
            # _ai_batch_release_worker_any.
            _ai_batch_release_worker_any(batch_job_id, owned_job_id)

    worker_spawned = False
    startup_committed = False
    try:
        with _ai_batch_control_lock:
            _ai_batch_controls[batch_job_id] = {"pause": False, "skip_current": False, "skip_folder_ids": set()}
        if not recover_batch_job_id:
            state = _ai_batch_initial_state(batch_job_id, scan_path)
            _ai_batch_commit(state, None)

        # jobs.start_python() spawns its worker thread before returning, and
        # the JobStore job_id doesn't exist until that call returns — so the
        # worker waits here rather than racing the main thread for it. The
        # worker must not be unblocked (and allowed to write batch state)
        # until the job_id association below is durably persisted and the
        # registry has been promoted to the real job_id, or an immediate
        # status poll (by job_id) could miss the batch entirely, and a
        # concurrent reconnecting caller could observe a reservation with no
        # job_id for longer than necessary.
        job = jobs.start_python(
            _do,
            label=f"AI Batch Import: {Path(scan_path).name}" + (" retry" if retry_failed else ""),
            metadata={"type": "ai-batch-import", "batch_job_id": batch_job_id, "source_path": scan_path},
        )
        # From this point on, a worker thread exists and is blocked waiting
        # for handoff_ready. Cleanup responsibility transfers to it
        # immediately: any failure below must abort+signal so the worker
        # exits right away, instead of this function popping the registry
        # itself and leaving the worker to block for up to 10s and then run
        # unregistered, unvalidated batch work (see _AiBatchStartupAbortedError).
        worker_spawned = True
        job_id_holder["job_id"] = job.job_id
        if recover_batch_job_id:
            # Targeted job_id association only -- not a full _ai_batch_commit.
            # The worker thread (still blocked below) owns the reconcile ->
            # commit cycle inside _run_ai_batch_import; a second independent
            # commit here would race it with a stale pre-reconciliation
            # snapshot (see _ai_batch_persist_job_association's docstring).
            _ai_batch_persist_job_association(batch_job_id, job.job_id)
            state = _ai_batch_load_state(batch_job_id) or _ai_batch_initial_state(batch_job_id, scan_path)
        else:
            state = _ai_batch_load_state(batch_job_id) or _ai_batch_initial_state(batch_job_id, scan_path)
            state["job_id"] = job.job_id
            state["status"] = "running"
            _ai_batch_commit(state, job.update_state)
        if not _ai_batch_promote_worker(batch_job_id, job.job_id):
            # Should not happen in normal operation (we are the sole owner
            # of our own reservation); treat as a startup failure so the
            # except block below aborts the already-spawned worker cleanly.
            raise RuntimeError(f"failed to promote AI batch worker registration for {batch_job_id}")
        # The worker must not be unblocked before the registry reflects its
        # real job_id, so a concurrent reconnect never observes a
        # startup-reserved-but-unpromoted entry for longer than necessary.
        #
        # Build the complete response *before* signaling handoff_ready:
        # once the worker is authorized it may immediately start real batch
        # work concurrently with the rest of this function, so nothing
        # failure-prone (state serialization, jsonify, etc.) may remain
        # after that point -- otherwise a failure here would report a
        # startup failure to the caller while the worker keeps running.
        response = jsonify({"ok": True, "job_id": job.job_id, "batch_job_id": batch_job_id, "state": _ai_batch_public_state(state)})
        # Once startup_committed is True the handoff is irrevocable: the
        # worker is authorized, the caller will receive success, and the
        # except block below must not abort a worker that may already be
        # running.
        startup_committed = True
        handoff_ready.set()
        return response
    except Exception:
        if not worker_spawned:
            # No worker thread was ever created (jobs.start_python() itself
            # raised, or something before it did) -- nothing to hand off
            # to, so this function retains sole ownership of releasing the
            # still-unpromoted reservation.
            _ai_batch_release_worker(batch_job_id, expected=None)
        elif not startup_committed:
            # A worker thread already exists and startup has not yet been
            # committed: abort it immediately rather than leaving it to
            # block on handoff_ready for up to 10s and then proceed
            # regardless. The worker's own finally releases whichever token
            # applies (see _do above) -- this function must not also pop
            # the registry here, or a third request could reserve and
            # start a second worker before the aborted one actually exits
            # and releases.
            startup_aborted.set()
            handoff_ready.set()
        # else: startup_committed is True -- the handoff is irrevocable, the
        # worker is authorized and may already be running; do not abort it.
        raise
