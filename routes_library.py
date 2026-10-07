"""Library, artist, album and artwork routes (ARCH-001): HTTP handlers over the library/artwork services.
"""

from __future__ import annotations

import backend.provider_boundary as provider_boundary
import json, mimetypes, os, re, threading, time
import urllib.error
from backend.security import OutboundPolicyError, resolve_public_target
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional
from flask import jsonify, request, send_file, redirect
from backend.matching_contract import AiState
from backend.transaction_engine import metadata_diff
from helpers_mb import _fetch_mb_recording_details, _mb_recording_search, _mb_release_search, _clean_for_mb, _resolve_mb_release_id
from backend.beets_adapter import beets_adapter, lib, BeetsError, BeetsUnavailableError, BeetsAuthError, BeetsAdapterNotFoundError, BeetsAdapterConnectionError, BeetsAdapterTimeoutError
import backend.composite_workflows as composite_workflows
import backend.recording_review as recording_review
from backend.identity_contract import verify_album_identity as _verify_album_identity
from backend.acoustid_service import _acoustid_fingerprint_match, _acoustid_lookup_cached, _normalize_albumartist, same_recording_proof
import backend.duplicate_cleanup as _duplicate_cleanup
from backend.ai_batch_state_service import _get_ai_batch_store
from backend.ai_evidence_service import _ai_suggest_genre, _enrich_track_ai_candidate, _item_ai_abs_path, _score_track_ai_candidate
from backend.ai_service import _ai_suggest_album_internal, _ai_suggest_folder_internal, _classify_openai_error, _compact_track_ai_candidate, _track_ai_evidence_packet
from backend.app_runtime import ARTIST_IMAGE_CACHE_DIR, AUDIO_EXT, DOWNLOADS_ROOT, EDITABLE_FIELDS, METADATA_CACHE_ROOT, MUSIC_ROOT, RELEASE_ART_CACHE_DIR, UNMATCHED_DRAFT_ROOT, _ANSI_RE, _DISC_CACHE_TTL, _MB_UUID_RE, _UNRESOLVED_TEMPLATE_TOKEN_RE, _app_logger, _path_is_under, _redact_security_text, _s, _safe_path_component, jobs, transactions
from backend.artwork_service import AlbumArtRequestError, _ALBUM_ART_UPLOAD_MAX_BYTES, _RELEASE_ART_MBID_RE, _album_art_cache, _album_art_cache_lock, _album_art_expected_release_group, _album_art_repair_entry, _album_art_status, _album_dir_for_art, _art_repair_attach_last_run, _art_repair_build_report, _art_repair_save_last, _artist_image_cache_url, _artist_local_art_url, _cache_artist_image, _fetch_album_art, _fetch_artist_image, _repair_album_art, _replace_album_art_bytes, _replace_album_art_from_url, _resolve_album_art_request_album, _save_art_to_disk, _usable_album_art_file, _validate_album_art_bytes
from backend.auth_service import _sanitize_confirmation_reason
from backend.cleanup_service import album_cleanup_apply_response, _cleanup_artist_alias_source_dirs, _cleanup_template_token_files, _cleanup_template_tokens_for_album
from backend.dedup_service import _BROWSE_ALLOWED_ROOTS, _album_duplicate_resolver_plan
from backend.import_reconciliation_service import _run_artist_folder_reconcile_for_alias_merge
from backend.import_review_service import _metadata_transaction_pending_fields, _redact_unmatched_draft_tracks
from backend.import_service import _coerce_library_import_all_album, _library_import_all_read_last, _library_import_all_record, _library_import_all_write_last, _start_reimport_disk_job_internal, start_reimport_disk
from backend.job_service import _wait_for_child_job
from backend.library_service import AttachRecordingCancelled, _album_db_folder_from_item_paths, _album_genre_value, _apply_genre_to_album, _artist_id_alias_groups, _fetch_discography, _folder_placeholder_summary, _get_last_scan, _lastgenre_cmd, _legacy_local_scan_enabled, _library_track_dict, _reconstruct_track_recording_candidates, _release_attach_recording_item, _require_attach_stage_success, _require_beet_ok, _reserve_attach_recording_item, _resolve_artist_alias_mbid, _scan_folder_name_placeholders, _scan_leaked_db_paths, _strip_year_from_album_name, album_dict, get_library_payload, item_dict, item_dict_full, start_fetch_missing_art, start_library_fix_genres
from backend.maintenance_service import _SCAN_STATE, _do_scan_job
from backend.matching_service import _ai_api_key, _ai_model_and_endpoint, _album_mb_completeness, _album_mb_match_plan, _disc_cache, _disc_cache_discogs, _disc_cache_discogs_lock, _disc_cache_lock, _invalidate_lib_cache, _load_album_mb_suggestions, _match_tracks_from_mb, _remove_album_track_items
from backend.musicbrainz_service import _discogs_artist_discography, _discogs_track_search, _ensure_release_group_art, _mb_release_group_for_release
from backend.pending_review_store import _is_music_root_path, _queue_folder_for_manual_review
from backend.plex_service import _trigger_plex_refresh
import backend.item_replacement as _item_replacement
from backend.serializers import _format_duration, _leaked_db_paths_summary, _resolve_import_review_source_path, json_route_result
from backend.transaction_service import _item_metadata_transaction_payload, _start_metadata_apply_transaction
from app import app  # noqa: E402  (route modules load after app.py defines app)
from helpers_mb import _fetch_mb_release_candidate
from backend.library_service import _album_source_folder, _resolve_album_release_for_import, _stamp_album_release_id
from backend.auth_service import _transaction_user_label

# ── ARCH-001 extracted code ──


@app.get("/api/stats")
def stats():
    try:
        s = beets_adapter.get_stats()
        tracks = s.get("items", 0)
        albums_count = s.get("albums", 0)
        artists = len(beets_adapter.get_artists())
        return jsonify({"tracks": tracks, "albums": albums_count, "artists": artists})
    except Exception as ex:
        _app_logger.warning("Beets /stats unavailable: %s", ex)
        return jsonify({
            "ok": False,
            "error": "Beets library unavailable",
            "error_code": "ENGINE_OFFLINE",
            "status": "unavailable",
        }), 503


def _compensate_committed_metadata_or_raise(
    aid: int, meta_op_id: Optional[str], failed_stage: str, downstream_exc: Exception, log,
) -> None:
    """A stage that runs after a committed album_metadata_repair_v1 mutation
    has failed. If meta_op_id identifies that committed operation, attempt a
    compensating rollback_album_metadata() so the failure doesn't leave a
    silent partial mutation; otherwise (no operation_id -- nothing was
    actually mutated, or the caller has no way to identify it) just
    surface the downstream failure as-is. Always raises: either a
    "rolled back cleanly" RuntimeError or a "Recovery Required" one when the
    compensating rollback itself also fails."""
    if not meta_op_id:
        raise downstream_exc
    log.append(f"{failed_stage} failed after a committed metadata update; rolling back metadata (operation_id={meta_op_id})...")
    try:
        rollback_result = composite_workflows.rollback_album_metadata(meta_op_id)
        _require_attach_stage_success(rollback_result, "album metadata rollback")
    except Exception as rollback_exc:
        # Compensating rollback itself failed: the album is left with
        # committed metadata but the downstream stage never completed, and
        # no automatic path back to the prior state. Do not report a
        # generic failure that hides this -- surface it explicitly as
        # needing manual review.
        log.append(
            f"RECOVERY REQUIRED: metadata operation {meta_op_id} committed and could not be "
            f"rolled back after {failed_stage} failed ({downstream_exc}); rollback error: {rollback_exc}. "
            f"Album {aid} has metadata applied without the {failed_stage} completing -- manual review needed."
        )
        raise RuntimeError(
            f"Recovery Required: album {aid} metadata was committed (operation_id={meta_op_id}) but "
            f"{failed_stage} failed and the compensating rollback also failed. Manual recovery needed."
        ) from rollback_exc
    log.append(f"Metadata rolled back cleanly; no partial mutation remains for album {aid}.")
    raise RuntimeError(f"{failed_stage} failed and metadata was rolled back: {downstream_exc}") from downstream_exc


def _run_attach_relocation_stage(aid: int, meta_op_id: Optional[str], stage: str, log) -> None:
    """Run the relocation stage of a metadata-then-relocate sequence
    (album_add_mbids, match_album) and, if it fails after a metadata
    mutation already committed, attempt a compensating rollback of that
    metadata operation rather than leaving a silent partial mutation.

    meta_op_id is the operation_id the prior update_album_metadata() call
    returned -- None means either nothing was mutated (a no-op plan) or the
    caller has no way to identify what to compensate, so no rollback is
    attempted in that case; a relocation failure is simply surfaced as-is,
    matching pre-existing behavior for that path."""
    try:
        relocate_result = composite_workflows.relocate_album(aid)
        _require_attach_stage_success(relocate_result, stage)
    except Exception as relocate_exc:
        _compensate_committed_metadata_or_raise(aid, meta_op_id, stage, relocate_exc, log)


# ── Items / Albums ────────────────────────────────────────────────────────────

@app.get("/api/items")
def items():
    q = request.args.get("q", "").strip()
    limit = min(int(request.args.get("limit", 100)), 500)
    rows = []
    try:
        for item in lib.items(q.split() if q else []):
            rows.append(item_dict(item))
            if len(rows) >= limit:
                break
        return jsonify({"count": len(rows), "items": rows})
    except (BeetsAdapterConnectionError, BeetsAdapterTimeoutError, BeetsUnavailableError) as ex:
        _app_logger.warning("Beets items query unavailable: %s", ex)
        return jsonify({
            "ok": False,
            "error": "Beets library unavailable",
            "error_code": "ENGINE_OFFLINE",
        }), 503


@app.get("/api/items/<int:iid>")
def get_item(iid):
    try:
        item = lib.get_item(iid)
    except (BeetsAdapterConnectionError, BeetsAdapterTimeoutError, BeetsUnavailableError) as ex:
        _app_logger.warning("Beets get_item unavailable: %s", ex)
        return jsonify({
            "ok": False,
            "error": "Beets library unavailable",
            "error_code": "ENGINE_OFFLINE",
        }), 503
    if not item:
        return jsonify({"ok": False, "error": "Not found"}), 404
    return jsonify({"ok": True, "item": item_dict_full(item)})


@app.get("/api/artists")
def get_artists_list():
    """Fetch all artist names directly from Stock Beets Web API."""
    try:
        artists = beets_adapter.get_artists()
        return jsonify({"ok": True, "count": len(artists), "artists": artists})
    except (BeetsAdapterConnectionError, BeetsAdapterTimeoutError, BeetsUnavailableError) as ex:
        _app_logger.warning("Beets get_artists unavailable: %s", ex)
        return jsonify({
            "ok": False,
            "error": "Beets library unavailable",
            "error_code": "ENGINE_OFFLINE",
        }), 503


@app.get("/api/search")
def api_search():
    """Search items and albums across stock Beets library."""
    q = request.args.get("q", "").strip()
    limit = min(int(request.args.get("limit", 100)), 500)
    try:
        items_rows = [item_dict(item) for item in lib.items(q.split() if q else [])[:limit]]
        albums_rows = [album_dict(album) for album in lib.albums(q.split() if q else [])[:limit]]
        return jsonify({
            "ok": True,
            "query": q,
            "items": items_rows,
            "albums": albums_rows,
            "items_count": len(items_rows),
            "albums_count": len(albums_rows),
        })
    except (BeetsAdapterConnectionError, BeetsAdapterTimeoutError, BeetsUnavailableError) as ex:
        _app_logger.warning("Beets search unavailable: %s", ex)
        return jsonify({
            "ok": False,
            "error": "Beets library unavailable",
            "error_code": "ENGINE_OFFLINE",
        }), 503


@app.get("/api/items/<int:iid>/file")
@app.get("/api/items/<int:iid>/audio")
@app.get("/api/item/<int:iid>/file")
def item_audio_file(iid: int):
    """Proxy audio file stream from stock Beets to browser without buffering in memory."""
    try:
        upstream_resp = beets_adapter.open_item_file(iid)
    except BeetsAdapterNotFoundError:
        return jsonify({"ok": False, "error": "Item audio file not found"}), 404
    except (BeetsAdapterConnectionError, BeetsAdapterTimeoutError) as ex:
        _app_logger.warning("Beets audio stream connection error for item %d: %s", iid, ex)
        return jsonify({
            "ok": False,
            "error": "Beets audio streaming unavailable",
            "error_code": "ENGINE_OFFLINE",
        }), 503
    except Exception as ex:
        _app_logger.warning("Beets audio stream error for item %d: %s", iid, ex)
        return jsonify({"ok": False, "error": "Audio stream failed"}), 500

    def generate_stream():
        try:
            while True:
                chunk = upstream_resp.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            upstream_resp.close()

    headers = {}
    content_type = upstream_resp.headers.get("Content-Type", "audio/mpeg")
    if "Content-Length" in upstream_resp.headers:
        headers["Content-Length"] = upstream_resp.headers["Content-Length"]
    if "Accept-Ranges" in upstream_resp.headers:
        headers["Accept-Ranges"] = upstream_resp.headers["Accept-Ranges"]
    if "Content-Disposition" in upstream_resp.headers:
        headers["Content-Disposition"] = upstream_resp.headers["Content-Disposition"]

    from flask import Response as _FlaskResponse
    return _FlaskResponse(generate_stream(), status=upstream_resp.status, mimetype=content_type, headers=headers)


@app.post("/api/items/<int:iid>/modify")
def modify_item(iid):
    payload = request.get_json(silent=True) or {}
    fields = payload.get("fields", {})
    if not isinstance(fields, dict) or not fields:
        return jsonify({"ok": False, "error": "No fields"}), 400
    editable = {field for field, _label in EDITABLE_FIELDS}
    fields = {str(k): v for k, v in fields.items() if str(k) in editable}
    if not fields:
        return jsonify({"ok": False, "error": "No editable fields"}), 400

    approved_tx_id = _s(payload.get("apply_transaction_id") or payload.get("approved_transaction_id") or "").strip()
    if approved_tx_id:
        try:
            tx = transactions.get(approved_tx_id)
            meta = tx.get("metadata") or {}
            if int(meta.get("item_id") or 0) != int(iid):
                return jsonify({"ok": False, "error": "Approved transaction belongs to a different item."}), 409
            pending_fields = _metadata_transaction_pending_fields(tx)
            if {str(k): _s(v) for k, v in pending_fields.items()} != {str(k): _s(v) for k, v in fields.items()}:
                return jsonify({"ok": False, "error": "Approved transaction fields do not match this apply request."}), 409
            job = _start_metadata_apply_transaction(approved_tx_id)
        except KeyError:
            return jsonify({"ok": False, "error": "Transaction not found"}), 404
        except ValueError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 409
        if job is None:
            return jsonify({"ok": True, "transaction_id": approved_tx_id, "applied": True})
        return jsonify({"ok": True, "job_id": job.job_id, "transaction_id": approved_tx_id})

    try:
        _item, _current, _proposed, tx_payload = _item_metadata_transaction_payload(iid, fields)
    except KeyError:
        return jsonify({"ok": False, "error": "Not found"}), 404

    changed_fields = [row["field"] for row in tx_payload["diff_rows"] if row.get("changed")]
    summary = f"Metadata edit for item {iid}: {', '.join(changed_fields) if changed_fields else 'no field changes'}"
    tx = transactions.create(
        operation_type="Metadata Update",
        initiating_user=_transaction_user_label(),
        status="Preview",
        dry_run=True,
        summary=summary,
        reason="Manual operator metadata edit.",
        source="User supplied fields",
        confidence={"overall": 1.0},
        changes=[tx_payload["change"]],
        rollback_available=bool(changed_fields),
        rollback_reason="Captured previous metadata values before apply." if changed_fields else "No changed fields were captured.",
        metadata={
            "item_id": iid,
            "changed_fields": changed_fields,
            "pending_fields": fields,
            "apply_endpoint": f"/api/transactions/{{id}}/apply",
            "requires_approval": True,
        },
    )
    if changed_fields:
        transactions.update(tx["id"], rollback={
            "available": True,
            "reason": "Captured previous metadata values before apply.",
            "operations": [tx_payload["rollback_op"]],
        }, counts={"items": 1, "changes": len(changed_fields)})

    return jsonify({
        "ok": True,
        "requires_approval": True,
        "dry_run": True,
        "transaction_id": tx["id"],
        "transaction": transactions.get(tx["id"]),
    })


@app.post("/api/items/<int:iid>/retag")
def retag_item(iid):
    """Sync metadata from MusicBrainz (if item has mb_trackid/mb_albumid),
    write tags to the audio file, then move it into the library structure."""
    def _do(log, cancel_event=None):
        try:
            item = lib.get_item(iid)
            has_mb = item and bool(getattr(item, "mb_albumid", "") or getattr(item, "mb_trackid", ""))
            aid = int(getattr(item, "album_id", 0) or 0) if item else 0
        except Exception:
            has_mb = False
            aid = 0

        if has_mb and aid > 0:
            log.append("[1/3] Syncing metadata from MusicBrainz via engine transaction…")
            try:
                p_res = composite_workflows.plan_album_mb_track_repair({"album_id": aid})
                if p_res.get("ok") and p_res.get("operation_id"):
                    composite_workflows.apply_album_mb_track_repair(p_res["operation_id"], write_tags=True)
            except Exception as ex:
                log.append(f"  WARN: mb_track_repair failed: {ex} — continuing")
        else:
            log.append("[1/3] No MusicBrainz ID — skipping mbsync")

        log.append("[2/3] Writing tags to file via engine transaction…")
        if aid > 0:
            try:
                # SEC-002 / ARCH-003 Wave 24 final review section 30: an
                # empty updates dict is a no-op for the diff-based
                # metadata family -- it planned zero item diffs and wrote
                # nothing, while this step claimed to have written tags.
                # force_write_tags requests a real Beets Item.write()
                # resync (current DB values -> file tags) regardless of
                # whether any field differs, which is the actual "write
                # current metadata to tags" semantics retag intends.
                res = composite_workflows.update_album_metadata(aid, {}, force_write_tags=True)
                if not res.get("ok"):
                    log.append(f"  WARN: tag write failed: {res.get('error')}")
            except Exception as ex:
                log.append(f"  WARN: update_album_metadata failed: {ex}")

        log.append("[3/3] Moving file into library structure via engine transaction…")
        if aid > 0:
            try:
                res = composite_workflows.relocate_album(aid, mode="rename")
                if res.get("ok"):
                    log.append(f"✓ Relocated album {aid}")
            except Exception as ex:
                log.append(f"  WARN: relocate_album failed: {ex}")

        try:
            updated = lib.get_item(iid)
            if updated:
                log.append(f"✓ Final path: {_s(updated.path)}")
        except Exception:
            pass
        _invalidate_lib_cache()
        _trigger_plex_refresh(log)

    job = jobs.start_python(_do, label=f"Retag+Move: item {iid}")
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/items/<int:iid>/mbsubmit")
def item_mbsubmit(iid: int):
    """Run beet mbsubmit for the album containing this item; return submission text."""
    def _do(log, cancel_event=None):
        item = lib.get_item(iid)
        if not item:
            raise RuntimeError(f"Item {iid} not found in library")
        query = f"album_id:{item.album_id}" if item.album_id else f"id:{iid}"
        res = composite_workflows.run_command("mbsubmit", [query], timeout=60.0)
        _require_attach_stage_success(res, "mbsubmit")
        output = _ANSI_RE.sub("", str(res.get("stdout") or res.get("output") or "")).strip()
        for line in output.splitlines():
            if line.strip():
                log.append(line)
        return {"output": output}
    job = jobs.start_python(_do, label=f"mbsubmit: item {iid}")
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/albums/<int:aid>/mbsubmit")
def album_mbsubmit(aid: int):
    """Run beet mbsubmit for an album; return submission text."""
    def _do(log, cancel_event=None):
        res = composite_workflows.run_command("mbsubmit", [f"album_id:{aid}"], timeout=60.0)
        _require_attach_stage_success(res, "mbsubmit")
        output = _ANSI_RE.sub("", str(res.get("stdout") or res.get("output") or "")).strip()
        for line in output.splitlines():
            if line.strip():
                log.append(line)
        return {"output": output}
    job = jobs.start_python(_do, label=f"mbsubmit: album {aid}")
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/albums/<int:aid>/add-mbids")
def album_add_mbids(aid: int):
    """Attach real MusicBrainz IDs to an album and move it to the MBID-stamped path."""
    payload = request.get_json(silent=True) or {}
    mb_albumartistid = _s(payload.get("mb_albumartistid") or "").strip().lower()
    mb_releasegroupid = _s(payload.get("mb_releasegroupid") or "").strip().lower()
    mb_albumid = _s(payload.get("mb_albumid") or "").strip().lower()
    if not mb_albumartistid or not mb_releasegroupid:
        return jsonify({"ok": False, "error": "mb_albumartistid and mb_releasegroupid are required"}), 400
    if not _MB_UUID_RE.match(mb_albumartistid) or not _MB_UUID_RE.match(mb_releasegroupid):
        return jsonify({"ok": False, "error": "Invalid MusicBrainz UUID format"}), 400
    # ARCH-009: an optional Release ID is edition evidence and must belong to
    # the supplied Release Group (verified against MusicBrainz, fail closed).
    identity = _verify_album_identity(mb_releasegroupid, mb_albumid,
                                      resolve_release_group=_mb_release_group_for_release)
    if not identity.ok:
        return jsonify({"ok": False, "error": identity.error, "code": identity.code}), 409

    def _do(log, cancel_event=None):
        fields = {
            "mb_albumartistid": mb_albumartistid,
            "mb_releasegroupid": identity.release_group_id,
        }
        if identity.release_id:
            fields["mb_albumid"] = identity.release_id
        meta_result = composite_workflows.update_album_metadata(aid, fields)
        _require_attach_stage_success(meta_result, "album MBID metadata update")
        # update_album_metadata() commits through its own rollback-capable
        # album_metadata_repair_v1 transaction and returns that operation_id
        # (when a mutation actually happened) precisely so a later stage
        # failing here has something to compensate against instead of
        # leaving a silently-partial mutation.
        meta_op_id = meta_result.get("operation_id") if isinstance(meta_result, dict) else None
        _run_attach_relocation_stage(aid, meta_op_id, "album MBID relocation", log)
        _invalidate_lib_cache()
        _trigger_plex_refresh(log)
        log.append(f"MBIDs applied and album moved to MBID-stamped path.")

    job = jobs.start_python(_do, label=f"Add MBIDs: album {aid}")
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/items/<int:iid>/attach-recording")
def item_attach_recording(iid: int):
    """Attach a MusicBrainz recording ID to a singleton item (no album_id)
    and sync/move it, mirroring album_add_mbids's direct modify+write+move
    pattern at item level.

    The backend -- never the browser -- decides whether the requested
    Recording ID is safe to attach without review, requires explicit
    confirmed review, or must be rejected outright: every client-supplied
    safety/confidence/eligibility field is ignored, and the matching
    decision is rebuilt from the current trusted MusicBrainz/AcoustID
    candidate set for this item at request time.

    Concurrency: a per-item reservation (_reserve_attach_recording_item) is
    held from just before the final idempotency check through the async
    mutation job, so two concurrent requests for the SAME item can never
    race -- the second gets 409 attachment_in_progress immediately. The
    item is re-read after the reservation is acquired and every check below
    (candidates, decision, existing identity) runs against that fresh read,
    never a snapshot taken before the reservation. After the beet stages
    all report success, the item is re-read again and the actual persisted
    mb_trackid/mb_albumid/mb_releasegroupid are what get audited as
    "Completed" -- never the pre-mutation candidate-expected identity."""
    payload = request.get_json(silent=True) or {}
    mb_trackid = _s(payload.get("mb_trackid") or "").strip().lower()
    if not mb_trackid:
        return jsonify({"ok": False, "error": "mb_trackid is required.", "code": "recording_id_required"}), 400
    if not _MB_UUID_RE.match(mb_trackid):
        return jsonify({"ok": False, "error": "Invalid MusicBrainz Recording ID format.", "code": "invalid_recording_id"}), 400

    mode = _s(payload.get("mode") or "safe").strip().lower()
    if mode not in ("safe", "confirmed_review"):
        return jsonify({"ok": False, "error": "Unknown attachment mode.", "code": "invalid_mode"}), 400

    if lib.get_item(iid) is None:
        return jsonify({"ok": False, "error": "Item not found.", "code": "review_item_not_found"}), 404

    if not _reserve_attach_recording_item(iid):
        return jsonify({
            "ok": False,
            "error": "A Recording ID attachment is already running for this item.",
            "code": "attachment_in_progress",
        }), 409

    job_started = False
    try:
        # Re-read after acquiring the reservation -- every check from here
        # on runs against this fresh item, not the pre-reservation snapshot,
        # so a concurrent mutation between the two reads can't be missed.
        item = lib.get_item(iid)
        if item is None:
            return jsonify({"ok": False, "error": "Item not found.", "code": "review_item_not_found"}), 404

        try:
            current, candidates, item_path, filename = _reconstruct_track_recording_candidates(item, iid)
        except Exception as exc:
            _app_logger.error(
                "attach-recording candidate reconstruction failed for item %s: %s: %s",
                iid, type(exc).__name__, _redact_security_text(str(exc))[:300],
            )
            return jsonify({
                "ok": False,
                "error": "Unable to rebuild trusted MusicBrainz/AcoustID evidence for this item.",
                "code": "matching_evidence_unavailable",
            }), 503

        candidate = next(
            (c for c in candidates if _s(c.get("mb_trackid", "")).strip().lower() == mb_trackid),
            None,
        )
        if candidate is None:
            return jsonify({
                "ok": False,
                "error": "This Recording ID is not part of the current trusted candidate set for this item.",
                "code": "candidate_not_in_trusted_set",
            }), 400

        decision = candidate.get("decision") or {}
        action_eligibility = decision.get("action_eligibility") or {}
        review_required = bool(decision.get("review_required"))
        requires_confirmation = bool(decision.get("requires_confirmation"))
        conflicts = list(decision.get("conflicts") or [])
        warnings = list(decision.get("warnings") or [])
        identity = (candidate.get("matching_contract") or {}).get("identity") or {}
        resolved_recording_id = _s(identity.get("resolved_recording_id"))
        computed_version = _s(candidate.get("decision_version") or "")
        submitted_version = _s(payload.get("decision_version") or "").strip()
        sanitized_candidate = _compact_track_ai_candidate(candidate)

        def _stale_response():
            return jsonify({
                "ok": False,
                "error": "The matching decision has changed since it was displayed; refresh the review item.",
                "code": "matching_decision_stale",
                "candidate": sanitized_candidate,
            }), 409

        confirmation_reason = ""
        if mode == "safe":
            if submitted_version and submitted_version != computed_version:
                return _stale_response()
            if not resolved_recording_id or resolved_recording_id != mb_trackid:
                return jsonify({
                    "ok": False,
                    "error": "Requested Recording ID does not match the backend-resolved Recording ID.",
                    "code": "recording_id_mismatch",
                    "candidate": sanitized_candidate,
                }), 409
            if (not action_eligibility.get("attach_without_review") or review_required
                    or requires_confirmation or conflicts):
                return jsonify({
                    "ok": False,
                    "error": "This candidate requires explicit confirmed review before it can be attached.",
                    "code": "review_confirmation_required",
                    "candidate": sanitized_candidate,
                }), 409
        else:
            if payload.get("confirm") is not True:
                return jsonify({
                    "ok": False,
                    "error": "Explicit confirmation is required for confirmed-review attachment.",
                    "code": "review_confirmation_required",
                }), 400
            confirmation_reason = _sanitize_confirmation_reason(payload.get("confirmation_reason"))
            if not confirmation_reason:
                return jsonify({
                    "ok": False,
                    "error": "A confirmation reason is required for confirmed-review attachment.",
                    "code": "confirmation_reason_required",
                }), 400
            if not submitted_version:
                return jsonify({
                    "ok": False,
                    "error": "decision_version is required for confirmed-review attachment.",
                    "code": "matching_decision_stale",
                }), 400
            if submitted_version != computed_version:
                return _stale_response()
            # Confirmation only ever authorizes attaching this Recording ID --
            # it never elevates the candidate's own safety/eligibility fields,
            # and never authorizes any destructive follow-on action.

        existing_mb_trackid = _s(getattr(item, "mb_trackid", "")).strip().lower()
        existing_mb_albumid = _s(getattr(item, "mb_albumid", "")).strip().lower()
        existing_rgid = _s(getattr(item, "mb_releasegroupid", "")).strip().lower()
        release_id = _s(candidate.get("release_id") or candidate.get("mb_albumid") or "").strip().lower()
        release_group_id = _s(candidate.get("release_group_id") or candidate.get("mb_releasegroupid") or "").strip().lower()

        if (existing_mb_trackid == mb_trackid
                and (not release_id or existing_mb_albumid == release_id)
                and (not release_group_id or existing_rgid == release_group_id)):
            return jsonify({
                "ok": True,
                "changed": False,
                "reason": "already_attached",
                "recording_id": mb_trackid,
            })

        before_snapshot = {
            "mb_trackid": existing_mb_trackid,
            "mb_albumid": existing_mb_albumid,
            "mb_releasegroupid": existing_rgid,
        }
        # Candidate-expected identity -- what we intend to persist. Kept
        # distinct from the actually-persisted identity verified after the
        # mutation (see _do below); never presented as persisted state
        # until that verification succeeds.
        candidate_identity = {
            "mb_trackid": mb_trackid,
            "mb_albumid": release_id,
            "mb_releasegroupid": release_group_id,
        }
        change_entry = {
            "id": f"item:{iid}",
            "operation": "MusicBrainz Match",
            "track": current.get("title") or filename,
            "artist": current.get("artist") or "",
            "album": current.get("album") or "",
            "current_metadata": before_snapshot,
            "new_metadata": candidate_identity,
            "metadata_diff": metadata_diff(before_snapshot, candidate_identity),
            "confidence": {"overall": decision.get("confidence_score")},
            "reason": confirmation_reason or _s(decision.get("eligibility_reason") or ""),
            "source": "Import Review attach-recording",
            "metadata": {"candidate_identity": dict(candidate_identity)},
        }
        tx = transactions.create(
            operation_type="MusicBrainz Match",
            initiating_user=_transaction_user_label(),
            status="Running",
            dry_run=False,
            summary=f"Attach recording ID for item {iid} ({'confirmed review' if mode == 'confirmed_review' else 'safe attach'})",
            reason=confirmation_reason or _s(decision.get("eligibility_reason") or ""),
            source="Import Review attach-recording",
            confidence={"overall": decision.get("confidence_score")},
            changes=[change_entry],
            rollback_available=True,
            rollback_reason="Restores the previous Recording/Release/Release-Group IDs and resyncs tags from MusicBrainz.",
            metadata={
                "item_id": iid,
                "mode": mode,
                "decision_version": computed_version,
                "resolved_recording_id": resolved_recording_id,
                "conflicts": conflicts,
                "warnings": warnings,
                "review_required": review_required,
                "requires_confirmation": requires_confirmation,
                "confirmation_reason": confirmation_reason,
                "candidate_identity": dict(candidate_identity),
            },
        )
        transactions.update(tx["id"], rollback={
            "available": True,
            "reason": "Restores the previous Recording/Release/Release-Group IDs and resyncs tags from MusicBrainz.",
            "operations": [{
                "type": "recording_id_restore",
                "item_id": iid,
                "fields": before_snapshot,
                "reason": "Restore recording identity captured before attach-recording.",
            }],
        }, counts={"items": 1, "changes": 1})
        audit_id = tx["id"]

        def _do(log, cancel_event=None):
            transactions.update(audit_id, status="Running")
            try:
                item_obj = lib.get_item(iid)
                aid = int(getattr(item_obj, "album_id", 0) or 0) if item_obj else 0

                # SEC-002 / ARCH-003 Wave 24 final review section 31: this
                # used to fall back to local `_beet_run modify/mbsync/
                # write/move` mutation whenever the engine transaction
                # didn't report `applied`. Test-harness compatibility must
                # never create a production mutation fallback -- the Web
                # Manager process is not supposed to be able to mutate
                # media/tags/DB directly at all (that authority belongs to
                # the engine, reached only through the transaction
                # boundary). Engine failure now fails closed: no local
                # Beets mutation, full stop. Tests requiring coverage here
                # must mock/inject the engine, not exercise a real
                # fallback mutation path in this process.
                applied = False
                engine_error = "engine track repair transaction unavailable"
                try:
                    repair_payload = {
                        "album_id": aid,
                        "track_mbids": {str(iid): mb_trackid},
                    }
                    acceptance_failpoint = _s(payload.get("_acceptance_failpoint") or "").strip()
                    if acceptance_failpoint:
                        repair_payload["_acceptance_failpoint"] = acceptance_failpoint
                    p_res = composite_workflows.plan_album_mb_track_repair(repair_payload)
                    if p_res.get("ok"):
                        op_id = p_res.get("operation_id")
                        if op_id:
                            a_res = composite_workflows.apply_album_mb_track_repair(op_id, write_tags=True)
                            if a_res.get("ok"):
                                applied = True
                            else:
                                engine_error = a_res.get("error") or "apply_album_mb_track_repair failed"
                        else:
                            # No operation_id with ok=True means there was
                            # nothing to change -- not a failure.
                            applied = True
                    else:
                        engine_error = p_res.get("error") or "plan_album_mb_track_repair rejected"
                except AttachRecordingCancelled:
                    # Preserve the distinct Cancelled status -- do not fold
                    # a real cancellation into a generic engine-failure
                    # RuntimeError.
                    raise
                except Exception as _be:
                    engine_error = str(_be)

                if not applied:
                    safe_engine_error = _redact_security_text(engine_error)
                    log.append(f"  ERROR: engine track repair transaction failed: {safe_engine_error}")
                    raise RuntimeError(f"Engine track repair transaction failed, refusing local fallback mutation: {safe_engine_error}")

                if aid > 0:
                    relocate_result = composite_workflows.relocate_album(aid, mode="rename")
                    _require_attach_stage_success(relocate_result, "attach recording relocation")

                # Truthfulness: never claim the candidate-expected identity
                # was persisted without checking. Invalidate the cache and
                # re-read the item so the audit reflects reality.
                _invalidate_lib_cache()
                persisted_item = lib.get_item(iid)
                actual_mb_trackid = _s(getattr(persisted_item, "mb_trackid", "")).strip().lower() if persisted_item else ""
                actual_mb_albumid = _s(getattr(persisted_item, "mb_albumid", "")).strip().lower() if persisted_item else ""
                actual_rgid = _s(getattr(persisted_item, "mb_releasegroupid", "")).strip().lower() if persisted_item else ""

                if actual_mb_trackid != mb_trackid:
                    transactions.update(audit_id, status="Failed", logs=list(log)[-500:])
                    transactions.append_log(
                        audit_id,
                        "ERROR: Requested Recording ID was not verified on the re-read item after mutation.",
                    )
                    raise RuntimeError("Recording ID did not verify after mutation")

                persisted_identity = {
                    "mb_trackid": actual_mb_trackid,
                    "mb_albumid": actual_mb_albumid,
                    "mb_releasegroupid": actual_rgid,
                }
                # Recording ID matched; a differing but legitimate release
                # context is allowed -- record it truthfully rather than
                # silently claiming the candidate's release context landed.
                release_mismatch = bool(release_id) and actual_mb_albumid != release_id
                rgid_mismatch = bool(release_group_id) and actual_rgid != release_group_id
                if release_mismatch or rgid_mismatch:
                    log.append(
                        "Persisted release/release-group identity differs from the evaluated "
                        "candidate; actual values recorded for audit, candidate expectation kept "
                        "separate. Review recommended."
                    )

                log.append("Revalidating MusicBrainz recording details after attach.")
                details = _fetch_mb_recording_details(mb_trackid)
                linked_count = len(details.get("linked_releases") or []) if isinstance(details, dict) else 0
                selected_release = (details.get("selected_release") or {}) if isinstance(details, dict) else {}
                if mode == "confirmed_review":
                    log.append(f"User confirmed this candidate before attaching the Recording ID: {confirmation_reason}")
                if linked_count:
                    rel_title = _s(selected_release.get("album") or details.get("album") or "")
                    rel_year = _s(selected_release.get("year") or details.get("year") or "")
                    rgid = _s(selected_release.get("mb_releasegroupid") or details.get("mb_releasegroupid") or "")
                    log.append(f"MusicBrainz recording lookup returned {linked_count} linked release(s); selected release context: {rel_title or '(unknown release)'} {rel_year or ''} {rgid or ''}".strip())
                    if not rgid:
                        log.append("Album/release identity remains under review; no Release Group ID was attached from the recording candidate.")
                else:
                    log.append("MusicBrainz recording lookup returned no linked releases; album identity remains under review.")
                log.append("Review eligibility will be recalculated from the refreshed library state.")
                _trigger_plex_refresh(log)
                log.append("Recording ID attached and item synced/moved.")

                updated_change = dict(change_entry)
                updated_change["current_metadata"] = before_snapshot
                updated_change["new_metadata"] = persisted_identity
                updated_change["metadata_diff"] = metadata_diff(before_snapshot, persisted_identity)
                updated_change["metadata"] = {
                    "candidate_identity": dict(candidate_identity),
                    "persisted_identity": persisted_identity,
                }
                transactions.update(
                    audit_id,
                    status="Completed",
                    logs=list(log)[-500:],
                    changes=[updated_change],
                    metadata={
                        "candidate_identity": dict(candidate_identity),
                        "persisted_identity": persisted_identity,
                        "release_identity_mismatch": release_mismatch,
                        "release_group_identity_mismatch": rgid_mismatch,
                    },
                )
                return {
                    "ok": True, "changed": True, "mode": mode, "recording_id": mb_trackid,
                    "decision_version": computed_version, "audit_id": audit_id,
                    "persisted_identity": persisted_identity,
                }
            except AttachRecordingCancelled as ex:
                transactions.update(audit_id, status="Cancelled", logs=list(log)[-500:])
                transactions.append_log(audit_id, f"Cancelled: {_redact_security_text(str(ex))[:200]}")
                raise
            except Exception as ex:
                transactions.update(audit_id, status="Failed", logs=list(log)[-500:])
                transactions.append_log(audit_id, f"ERROR: {_redact_security_text(str(ex))[:300]}")
                raise
            finally:
                _release_attach_recording_item(iid)

        try:
            job = jobs.start_python(_do, label=f"Attach recording ID: item {iid}")
        except Exception:
            transactions.update(audit_id, status="Failed", logs=["ERROR: failed to start attachment job"])
            return jsonify({
                "ok": False,
                "error": "Unable to start the attachment job.",
                "code": "attachment_job_start_failed",
            }), 500
        # Ownership of the reservation transfers to the job from this point
        # on (released in its own finally above) -- do not release here.
        job_started = True
        transactions.attach_job(audit_id, job.job_id)
        return jsonify({
            "ok": True,
            "changed": True,
            "mode": mode,
            "recording_id": mb_trackid,
            "decision_version": computed_version,
            "audit_id": audit_id,
            "job_id": job.job_id,
        })
    finally:
        if not job_started:
            _release_attach_recording_item(iid)


@app.post("/api/items/<int:iid>/ai-suggest")
def ai_suggest(iid):
    item = lib.get_item(iid)
    if not item:
        return jsonify({"ok": False, "error": "Not found"})
    # AI is optional: gathering AcoustID/MusicBrainz/Discogs candidates below
    # runs unconditionally, so a missing/invalid key still yields a match.
    api_key = _ai_api_key()
    ai_available = bool(api_key)
    ai_configured = ai_available  # preserved for later boundary; ai_available itself may be mutated below

    item_path = _item_ai_abs_path(item)
    filename = Path(item_path or _s(item.path)).name
    raw_year  = str(item.year or "")
    # Normalise year: "20120923" → "2012", "2012-09-23" → "2012"
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

    # ── Pre-process: extract artist/title from "Artist - Title" filenames ──────
    # When the title tag contains "Artist - Title" or the filename does, split it.
    stem = Path(filename).stem
    # Strip leading track numbers: "01 - Title" → "Title"
    stem_clean = re.sub(r'^\d+\s*[-_.]\s*', '', stem).strip()

    search_title  = current["title"] or stem_clean
    search_artist = current["artist"] or current["albumartist"] or ""

    # If title looks like "Artist - Title" (and artist seems wrong/missing), split it
    _SPLIT_SCORE_THRESH = 0.4
    if " - " in search_title:
        parts = search_title.split(" - ", 1)
        candidate_artist, candidate_title = parts[0].strip(), parts[1].strip()
        # Use the split if the existing artist looks like station/label noise or is absent
        _junk_patterns = re.compile(
            r'radio|station|channel|network|records|music|media|official|vevo|'
            r'entertainment|group|label|^various',
            re.I)
        if (not search_artist or _junk_patterns.search(search_artist)
                or len(search_artist) > 30):
            search_title  = candidate_title
            search_artist = candidate_artist

    _mb_t, _mb_a = _clean_for_mb(search_title, search_artist)

    # ── 1. AcoustID fingerprint (most reliable) ───────────────────────────────
    acoustid_cands = _acoustid_lookup_cached(item_path) if item_path else []

    # ── 2. MusicBrainz text search ────────────────────────────────────────────
    # A MusicBrainz outage keeps the AcoustID evidence; the MB part is
    # reported unavailable, never as "no candidates".
    musicbrainz_unavailable = False
    try:
        mb_text_cands = _mb_recording_search(_mb_t, _mb_a, limit=6)
        # Broaden if nothing found
        if not mb_text_cands and _mb_a:
            mb_text_cands = _mb_recording_search(_mb_t, "", limit=6)
    except provider_boundary.ProviderError:
        musicbrainz_unavailable, mb_text_cands = True, []

    # ── 3. Discogs (supplemental genre / label info) ──────────────────────────
    discogs_cands = _discogs_track_search(_mb_t, _mb_a, limit=3)

    # Merge AcoustID + MB, deduplicate by mb_trackid (candidate generation
    # only -- the canonical evaluator decides, with the full hit set).
    acoustid_hits = recording_review.acoustid_hits_for(item_path, acoustid_cands)
    mb_candidates = recording_review.merge_recording_candidates(
        acoustid_cands, mb_text_cands, item_path=item_path,
        score_fn=lambda c: _score_track_ai_candidate(current, _mb_t, _mb_a, filename, c),
    )
    recording_review.enrich_and_index(
        mb_candidates,
        lambda c, hits: _enrich_track_ai_candidate(current, c, item_id=iid, acoustid_hits=hits),
        acoustid_hits,
        swallow_errors=True,
    )

    mb_section = ""
    if mb_candidates:
        lines = ["MusicBrainz/AcoustID candidates "
                 "(idx / match / score / title / artist / album / year / country / mb_trackid / source):"]
        for c in mb_candidates:
            ms = c.get("_match_score", {})
            lines.append(
                f"  [{c.get('candidate_index', -1)}] match={ms.get('total', 0):.2f} "
                f"mb={int(c.get('score') or 0):3d} {c['title']} — {c['artist']} / "
                f"{c['album']} ({c['year']}) {c.get('country','')} "
                f"[{c['mb_trackid']}] via {c.get('source','mb')}")
        mb_section = "\n\n" + "\n".join(lines)

    discogs_section = ""
    if discogs_cands:
        dlines = ["Discogs candidates (artist / album / year / genre / format):"]
        for d in discogs_cands:
            dlines.append(f"  {d['artist']} — {d['album']} ({d['year']}) "
                          f"[{d['genre']}] {d['format']}")
        discogs_section = "\n\n" + "\n".join(dlines)

    prompt = (
        "You are a music metadata expert. A file was imported with incorrect or incomplete tags.\n\n"
        f"Filename: {filename}\n"
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
    _ai_model, _ai_endpoint = _ai_model_and_endpoint("gpt-4o")
    payload = json.dumps({
        "model": _ai_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }).encode()
    ai_unavailable_reason = "" if ai_available else "OPENAI_API_KEY not configured"
    suggestions: Optional[Dict[str, Any]] = None
    if ai_available:
        req = urllib.request.Request(
            _ai_endpoint,
            data=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with provider_boundary.opened("ai", req, timeout=30) as resp:
                data = json.loads(resp.read())
            suggestions = json.loads(data["choices"][0]["message"]["content"])
        except Exception as exc:
            ai_available = False
            ai_unavailable_reason = _classify_openai_error(exc)

    # Captured before the fallback below overwrites `suggestions` with a
    # mechanically-derived (non-AI) guess, so the matching contract never
    # claims AI contributed when it did not actually respond.
    ai_contributed = suggestions is not None

    if suggestions is None:
        # AI unavailable/failed -- fall back to the top-ranked AcoustID/
        # MusicBrainz candidate (already gathered above) instead of
        # returning ok=False.
        if mb_candidates:
            top = mb_candidates[0]
            top_score = float((top.get("_match_score") or {}).get("total", 0) or 0)
            suggestions = {
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
        else:
            suggestions = {
                "confidence": "low",
                "reason": f"No MusicBrainz/AcoustID candidates found, and AI is unavailable ({ai_unavailable_reason}).",
            }

    try:
        # Coerce numeric fields
        for f in ("year", "track", "tracktotal", "disc", "disctotal"):
            if f in suggestions:
                suggestions[f] = str(suggestions[f])
        # Clamp year to 4 digits
        if suggestions.get("year"):
            _ym = re.match(r'^((?:19|20)\d{2})', str(suggestions["year"]))
            if _ym:
                suggestions["year"] = _ym.group(1)
        # Ensure confidence and reason are always present
        if not suggestions.get("confidence"):
            suggestions["confidence"] = "medium"
        if not suggestions.get("reason"):
            suggestions["reason"] = ""
        # Validate mb_trackid
        _MB_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
        mb_trackid  = (suggestions.get("mb_trackid")  or "").strip()
        mb_albumid  = (suggestions.get("mb_albumid")  or "").strip()
        mb_artistid = (suggestions.get("mb_artistid") or "").strip()
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
        suggestions["mb_valid"] = bool(re.match(_MB_RE, mb_trackid, re.I))
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
        if suggestions["mb_valid"]:
            suggestions["mb_url"] = f"https://musicbrainz.org/recording/{mb_trackid}"
            # Fetch full recording details from MB to fill remaining fields
            if selected_candidate:
                try:
                    preferred_albumid = (
                        mb_albumid
                        or _s(selected_candidate.get("mb_albumid", "")).strip()
                        or next(iter(selected_candidate.get("mb_albumids") or []), "")
                    )
                    details = _fetch_mb_recording_details(mb_trackid, preferred_albumid)
                    # All facts below are genuinely known at this point in
                    # the request (AI has already been attempted, and the
                    # fallback path -- if AI failed -- has already run), so
                    # this is the one call site where a real AiState can be
                    # reported instead of "not evaluated at this boundary".
                    ai_state_for_selected = AiState(
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
                    _enrich_track_ai_candidate(
                        current, selected_candidate, details,
                        ai_state=ai_state_for_selected, acoustid_hits=acoustid_hits,
                    )
                    # Fields MB fills authoritatively (override AI guesses)
                    _mb_authoritative = {"artist", "mb_albumid", "mb_artistid",
                                         "track", "tracktotal", "disc", "disctotal",
                                         "label", "genre", "album", "year"}
                    for k, v in details.items():
                        if v and (k in _mb_authoritative or not suggestions.get(k)):
                            suggestions[k] = v
                    # Re-read possibly-updated mb_albumid
                    mb_albumid = (suggestions.get("mb_albumid") or "").strip()
                except Exception:
                    pass
        if selected_candidate:
            selected_packet = _compact_track_ai_candidate(selected_candidate)
            suggestions["candidate_type"] = "recording"
            suggestions["candidate_evidence"] = selected_packet
            suggestions["selected_recording_candidate"] = selected_packet
            suggestions["selected_release"] = selected_packet.get("selected_release") or {}
            suggestions["linked_releases"] = selected_packet.get("linked_releases") or []
            suggestions["conflicts"] = selected_packet.get("conflicts") or []
            suggestions["recommended_action"] = selected_packet.get("recommended_action") or ""
            suggestions["requires_confirmation"] = bool(selected_packet.get("requires_confirmation"))
            suggestions["safety_result"] = selected_packet.get("safety_result") or ""
            suggestions["confidence_score"] = selected_packet.get("confidence_score")
            suggestions["match_method"] = selected_packet.get("match_method") or selected_packet.get("source")
        suggestions["recording_candidates"] = [_compact_track_ai_candidate(c) for c in mb_candidates[:8]]
        suggestions["missing_id_type"] = "Recording ID"
        if re.match(_MB_RE, (suggestions.get("mb_albumid") or ""), re.I):
            suggestions["mb_album_url"] = (
                f"https://musicbrainz.org/release/{suggestions['mb_albumid']}")
        evidence = _track_ai_evidence_packet(
            iid,
            filename=filename,
            current=current,
            search_title=_mb_t,
            search_artist=_mb_a,
            suggestions=suggestions,
            selected_candidate=selected_candidate,
            candidates=mb_candidates,
            acoustid_candidates=acoustid_cands,
            discogs_candidates=discogs_cands,
        )
        suggestions["ai_available"] = ai_available
        suggestions["ai_unavailable_reason"] = ai_unavailable_reason
        suggestions["evidence"] = evidence
        return jsonify({"ok": True, "suggestions": suggestions,
                        "mb_candidates": mb_candidates,
                        "selected_candidate": _compact_track_ai_candidate(selected_candidate) if selected_candidate else {},
                        "evidence": evidence,
                        "acoustid_candidates": acoustid_cands,
                        "discogs_candidates": discogs_cands,
                        "musicbrainz_unavailable": musicbrainz_unavailable})
    except Exception as exc:
        _app_logger.warning("AI track suggestion failed: %s", type(exc).__name__)
        return jsonify({"ok": False, "error": "Could not generate suggestions."})


@app.get("/api/items/<int:iid>/mb-candidates")
def item_mb_candidates(iid):
    """Search MusicBrainz for recording candidates for a library track."""
    item = lib.get_item(iid)
    if not item:
        return jsonify({"ok": False, "error": "Not found"})
    raw_title  = item.title or ""
    raw_artist = item.artist or item.albumartist or ""
    title, artist = _clean_for_mb(raw_title, raw_artist)
    candidates = _mb_recording_search(title, artist)
    return jsonify({"ok": True, "candidates": candidates, "query": {"title": title, "artist": artist}})


@app.get("/api/albums/<int:aid>/mb-candidates")
def album_mb_candidates(aid):
    """Search MusicBrainz for release candidates for a library album."""
    album = lib.get_album(aid)
    if not album:
        return jsonify({"ok": False, "error": "Not found"})
    candidates = _mb_release_search(album.album or "", album.albumartist or "")
    current_mbid = _s(getattr(album, "mb_albumid", "") or "").strip().lower()
    if current_mbid:
        for candidate in candidates:
            candidate["is_current"] = (
                _s(candidate.get("mb_albumid", "")).strip().lower() == current_mbid)
            for alt in candidate.get("edition_alternates") or []:
                if isinstance(alt, dict):
                    alt["is_current"] = (
                        _s(alt.get("mb_albumid", "")).strip().lower() == current_mbid)
    return jsonify({"ok": True, "candidates": candidates})


@app.get("/api/artist-discography")
def artist_discography():
    """Start (or return cached) discography check. Returns job_id while running, result when done."""
    artist_name = request.args.get("artist", "").strip()
    if not artist_name:
        return jsonify({"ok": False, "error": "artist parameter required"})

    # Return cached result if fresh (same session)
    with _disc_cache_lock:
        if artist_name in _disc_cache:
            return jsonify(_disc_cache[artist_name])

    # Start background job
    result_holder: Dict[str, Any] = {}

    def _run(log):
        log.append(f"Searching MusicBrainz for '{artist_name}'…")
        try:
            res = _fetch_discography(artist_name)
            result_holder.update(res)
            log.append(f"done:{len(res.get('missing', []))} missing")
            with _disc_cache_lock:
                _disc_cache[artist_name] = res
        except Exception as exc:
            result_holder["error"] = str(exc)
            log.append(f"error:{exc}")

    job = jobs.start_python(_run, label=f"Discography: {artist_name}")
    return jsonify({"ok": True, "status": "running", "job_id": job.job_id,
                    "artist": artist_name})


@app.get("/api/artist-discography/discogs")
def artist_discography_discogs():
    """Fetch artist discography from Discogs and compare against library."""
    artist_name = request.args.get("artist", "").strip()
    if not artist_name:
        return jsonify({"ok": False, "error": "artist parameter required"})
    with _disc_cache_discogs_lock:
        cached = _disc_cache_discogs.get(artist_name)
        if cached:
            age   = time.time() - cached.get("_cached_at", 0)
            total = cached.get("total", 0) or (
                len(cached.get("have") or []) + len(cached.get("missing") or []))
            # Serve cache if: within TTL, non-empty, or a known-error result
            if age < _DISC_CACHE_TTL and (total > 0 or not cached.get("ok")):
                return jsonify(cached)
            # Expired or empty-success → evict and re-fetch
            del _disc_cache_discogs[artist_name]

    def _run(log):
        log.append(f"Searching Discogs for '{artist_name}'…")
        try:
            res = _discogs_artist_discography(artist_name)
            if res.get("ok") and (res.get("total") or 0) == 0:
                log.append("warn:0 releases matched — check Discogs filter or artist name")
            else:
                res["_cached_at"] = time.time()
                with _disc_cache_discogs_lock:
                    _disc_cache_discogs[artist_name] = res
            log.append(f"done:{len(res.get('missing', []))} missing / {len(res.get('have', []))} have")
        except Exception as exc:
            log.append(f"error:{exc}")

    job = jobs.start_python(_run, label=f"Discogs discography: {artist_name}")
    return jsonify({"ok": True, "status": "running", "job_id": job.job_id,
                    "artist": artist_name, "source": "discogs"})


_artist_img_cache: Dict[str, str] = {}


_artist_img_cache_lock = threading.Lock()


@app.get("/api/artist-image-cache/<key>")
def artist_image_cache(key):
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,120}-[0-9a-f]{12}$", key or ""):
        return ("", 404)
    meta_path = ARTIST_IMAGE_CACHE_DIR / f"{key}.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        image_name = _s(meta.get("image_file", "") or "")
        if not image_name or Path(image_name).name != image_name:
            return ("", 404)
        image_path = ARTIST_IMAGE_CACHE_DIR / image_name
        if not image_path.exists() or not image_path.is_file():
            return ("", 404)
        mime = _s(meta.get("mime", "") or "") or mimetypes.guess_type(image_name)[0] or "image/jpeg"
        return send_file(str(image_path), mimetype=mime)
    except Exception:
        return ("", 404)


@app.get("/api/release-art-cache/<mbid>")
def release_art_cache(mbid):
    if not _RELEASE_ART_MBID_RE.match(mbid or ""):
        return ("", 404)
    meta_path = RELEASE_ART_CACHE_DIR / f"{mbid}.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        image_name = _s(meta.get("image_file", "") or "")
        if not image_name or Path(image_name).name != image_name:
            return ("", 404)
        image_path = RELEASE_ART_CACHE_DIR / image_name
        if not image_path.exists() or not image_path.is_file():
            return ("", 404)
        mime = _s(meta.get("mime", "") or "") or mimetypes.guess_type(image_name)[0] or "image/jpeg"
        return send_file(str(image_path), mimetype=mime)
    except Exception:
        return ("", 404)


@app.get("/api/release-art")
def release_art_api():
    """Return a locally cached release-group cover art URL, fetching+storing it first if needed."""
    mbid = request.args.get("mbid", "").strip()
    artist_name = request.args.get("artist", "").strip()
    album_title = request.args.get("album", "").strip()
    if not mbid:
        return jsonify({"ok": False, "error": "mbid required"})
    return jsonify(_ensure_release_group_art(mbid, artist_name, album_title))


@app.get("/api/artist-image-url")
def artist_image_url_api():
    """Return a local cached artist image URL, downloading it if needed."""
    artist = request.args.get("artist", "").strip()
    if not artist:
        return jsonify({"ok": False, "error": "artist required"})
    with _artist_img_cache_lock:
        if artist in _artist_img_cache:
            cached_url = _artist_img_cache[artist]
            return jsonify({
                "ok": True,
                "url": cached_url,
                "cached": cached_url.startswith("/api/artist-image-cache/"),
                "source": "artist_cache" if cached_url.startswith("/api/artist-image-cache/") else "album_fallback",
                "downloaded": False,
            })
    url = _artist_image_cache_url(artist)
    source = "artist_cache" if url else ""
    downloaded = False
    if not url:
        remote_url = _fetch_artist_image(artist)
        url = _cache_artist_image(artist, remote_url) if remote_url else ""
        if url:
            source = "artist_cache"
            downloaded = True
            # Do NOT _invalidate_lib_cache() here: with the Library page now
            # fetching+swapping in artist photos client-side on mount (see
            # useArtistArtUrl), a burst of first-time downloads for many
            # never-cached artists would otherwise force a full, expensive
            # /api/library rebuild after nearly every single one of them —
            # a self-inflicted cache-invalidation storm. /api/library's own
            # _LIB_CACHE_TTL (90s) picks up the fresh image_url soon enough
            # for any other consumer that reads it from there instead.
        else:
            url = _artist_local_art_url(artist)
            source = "album_fallback" if url else ""
    with _artist_img_cache_lock:
        if url:
            _artist_img_cache[artist] = url
    return jsonify({"ok": True, "url": url, "cached": source == "artist_cache",
                    "source": source, "downloaded": downloaded})


@app.get("/api/library/art-repair")
def library_art_repair_report():
    try:
        report = _art_repair_build_report()
        return jsonify(_art_repair_attach_last_run(report))
    except BeetsUnavailableError as ex:
        _app_logger.warning("Artwork repair report failed: Beets engine unavailable")
        return jsonify({
            "ok": False,
            "error": "Beets engine is unavailable.",
            "error_code": "ENGINE_OFFLINE",
        }), 503
    except BeetsAuthError as ex:
        _app_logger.error("Artwork repair report failed: Control agent authentication failed (%s)", type(ex).__name__)
        return jsonify({
            "ok": False,
            "error": "Beets engine authentication failed.",
            "error_code": "ENGINE_AUTH_FAILED",
        }), 503
    except BeetsError as ex:
        _app_logger.error("Artwork repair report failed: %s", ex)
        return jsonify({
            "ok": False,
            "error": "Could not load artwork repair status.",
            "error_code": "ART_REPAIR_FAILED",
        }), 500
    except Exception as ex:
        _app_logger.exception("Artwork repair report unexpected failure")
        return jsonify({
            "ok": False,
            "error": "Could not load artwork repair status.",
            "error_code": "ART_REPAIR_FAILED",
        }), 500


@app.get("/api/albums/<int:aid>/art")
def album_art(aid):
    """Serve album art via stock Beets proxy, local cache, or Cover Art Archive fallback."""
    # 1. Try stock Beets web endpoint proxy first
    try:
        upstream_resp = beets_adapter.open_album_art(aid)

        def generate_art():
            try:
                while True:
                    chunk = upstream_resp.read(32 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                upstream_resp.close()

        headers = {"Cache-Control": "public, max-age=86400"}
        content_type = upstream_resp.headers.get("Content-Type", "image/jpeg")
        if "Content-Length" in upstream_resp.headers:
            headers["Content-Length"] = upstream_resp.headers["Content-Length"]
        from flask import Response as _Resp
        return _Resp(generate_art(), status=upstream_resp.status, mimetype=content_type, headers=headers)
    except (BeetsAdapterNotFoundError, Exception):
        pass

    # 2. Local filesystem / artpath fallback
    _MROOT_ART = str(MUSIC_ROOT)
    album = lib.get_album(aid)
    if not album:
        return ("", 404)
    artpath = _s(getattr(album, "artpath", "") or "")
    if artpath and not artpath.startswith("/"):
        artpath = _MROOT_ART + "/" + artpath
    artpath = artpath.replace("\x00", "").strip()
    if artpath and _usable_album_art_file(Path(artpath)):
        mime = "image/jpeg"
        low = artpath.lower()
        if low.endswith(".png"):
            mime = "image/png"
        elif low.endswith(".gif"):
            mime = "image/gif"
        return send_file(artpath, mimetype=mime)

    # 3. Scan track items for folder art
    _DISC_RE = re.compile(r'^(?:disc|cd|disk)\s*\d+$', re.I)
    _ART_NAMES = ("albumart.jpg", "albumart.png", "folder.jpg",
                  "cover.jpg", "front.jpg", "cover.png")
    try:
        for item in lib.items(f"album_id:{aid}"):
            p = _s(item.path)
            if p and not p.startswith("/"):
                p = _MROOT_ART + "/" + p
            item_dir = Path(p).parent
            dirs_to_check = [item_dir]
            if _DISC_RE.match(item_dir.name):
                dirs_to_check.append(item_dir.parent)
            if item_dir.parent.parent != item_dir.parent:
                dirs_to_check.append(item_dir.parent)
            for check_dir in dirs_to_check:
                for aname in _ART_NAMES:
                    candidate = check_dir / aname
                    if _usable_album_art_file(candidate):
                        mime = "image/png" if aname.endswith(".png") else "image/jpeg"
                        return send_file(str(candidate), mimetype=mime)
            break
    except Exception:
        pass

    # 4. Fallback: Cover Art Archive via MB release group or release ID
    mbid = (_s(getattr(album, "mb_releasegroupid", "") or "")
            or _s(getattr(album, "mb_albumid", "") or ""))
    if mbid:
        art_result = _ensure_release_group_art(
            mbid,
            _s(getattr(album, "albumartist", "") or getattr(album, "artist", "") or ""),
            _s(getattr(album, "album", "") or ""),
        )
        if art_result.get("ok") and art_result.get("url"):
            return redirect(art_result["url"])

    # 5. Transparent placeholder
    _SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>')
    from flask import Response as _Resp
    return _Resp(_SVG, status=200, mimetype="image/svg+xml")


@app.get("/api/albums/<int:aid>/art/status")
def album_art_status(aid):
    status = _album_art_status(aid)
    if not status:
        return jsonify({"ok": False, "error": "Album not found"}), 404
    return jsonify(status)


@app.post("/api/albums/<int:aid>/fetch-art")
def album_fetch_art(aid):
    """Run beet fetchart for a specific album, then fallback if needed."""
    def _do(log, cancel_event=None, update_state=None):
        result = _repair_album_art(aid, log, cancel_event)
        error = _s(result.get("error") or "")
        if error == "cancelled":
            terminal_outcome = "cancelled"
        elif "timed out" in error.lower():
            terminal_outcome = "timed_out"
        elif result.get("status") == "saved":
            terminal_outcome = "success"
        else:
            terminal_outcome = "failed"
        if update_state:
            update_state(terminal_outcome=terminal_outcome)
        if result.get("status") != "saved":
            raise RuntimeError(error or "album art repair failed")
        saved_path = _s(result.get("saved_path") or result.get("local_art_path") or "")
        if saved_path:
            log.append(f"  Art saved: {Path(saved_path).name}")
        _invalidate_lib_cache()
        return result
    job = jobs.start_python(
        _do,
        label=f"FetchArt: album {aid}",
        metadata={"type": "album_art_repair", "album_id": aid},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/albums/<int:aid>/fetch-embed-artwork")
def album_fetch_embed_artwork(aid):
    """Fetch and embed cover art for an album through the controlled
    album_artwork_fetch_v1 engine transaction (Plan -> Apply -> Verify).

    SEC-002 / ARCH-003 Wave 25 Docker acceptance round: distinct from the
    pre-existing /api/albums/<id>/fetch-art above, which predates the
    two-service architecture and calls a local `lib.get_album(aid)` /
    Discogs-fallback pipeline of its own -- migrating that route is
    real, standalone engineering not attempted in this pass (see
    docs/TECHNICAL_DEBT.md). This route is the actual, directly reachable
    production entry point for the new controlled family
    (composite_workflows.fetch_and_embed_album_art -> POST
    /albums/artwork/fetch/plan + /apply on the engine), used by
    reimport_disk's post-import artwork step and independently callable
    here so it has its own real HTTP surface, not just an internal
    function call."""
    def _do(log, cancel_event=None):
        res = composite_workflows.fetch_and_embed_album_art(aid)
        if not res.get("ok"):
            raise RuntimeError(res.get("error") or "artwork fetch/embed failed")
        log.append(f"  Artwork saved: {res.get('artpath')}")
        _invalidate_lib_cache()
        return res
    job = jobs.start_python(
        _do,
        label=f"FetchEmbedArtwork: album {aid}",
        metadata={"type": "album_artwork_fetch_v1", "album_id": aid},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/albums/<int:aid>/art/url")
def album_replace_art_from_url(aid):
    """Download a user-provided cover image URL and set it as this album's art."""
    album = lib.get_album(aid)
    if not album:
        return jsonify({"ok": False, "error": "Album not found"}), 404
    payload = request.get_json(silent=True) or {}
    image_url = _s(payload.get("url") or "").strip()
    parsed = urllib.parse.urlparse(image_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return jsonify({"ok": False, "error": "Valid http(s) image URL required"}), 400
    try:
        # Pasted URL: public internet only, never the operator allowlist. The
        # download itself re-validates and pins (_download_album_art_bytes).
        resolve_public_target(image_url)
    except OutboundPolicyError:
        return jsonify({"ok": False, "error": "Image URL is not allowed"}), 400
    expected_rgid = _album_art_expected_release_group(album)

    def _do(log, cancel_event=None):
        album_obj = lib.get_album(aid)
        if not album_obj:
            raise RuntimeError("Album not found")
        try:
            result = _replace_album_art_from_url(
                aid,
                image_url,
                source="user_url",
                expected_mb_releasegroupid=expected_rgid,
                log=log,
            )
        except AlbumArtRequestError as ex:
            raise RuntimeError(ex.message) from ex
        except BeetsError as ex:
            _app_logger.warning("Could not replace album artwork for album %s: %s", aid, type(ex).__name__)
            raise RuntimeError("Could not update album artwork") from ex
        except Exception as ex:
            raise RuntimeError("Could not update album artwork") from ex
        saved = _s(result.get("artpath") or "")
        _invalidate_lib_cache()
        return {"path": saved, "image": result.get("image") or {}}

    job = jobs.start_python(
        _do,
        label=f"Replace art: album {aid}",
        metadata={"type": "album-art-replace", "album_id": aid},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/albums/<int:aid>/art/upload")
def album_upload_art(aid):
    """Accept a user-uploaded cover image and set it as this album's art."""
    album = lib.get_album(aid)
    if not album:
        return jsonify({"ok": False, "error": "Album not found"}), 404
    upload = request.files.get("file") or request.files.get("art")
    if not upload:
        return jsonify({"ok": False, "error": "Cover image file required"}), 400
    data = upload.read(_ALBUM_ART_UPLOAD_MAX_BYTES + 1)
    try:
        _validate_album_art_bytes(data)
    except AlbumArtRequestError as ex:
        return jsonify({"ok": False, "error": ex.message}), ex.status
    expected_rgid = _album_art_expected_release_group(album)

    def _do(log, cancel_event=None):
        album_obj = lib.get_album(aid)
        if not album_obj:
            raise RuntimeError("Album not found")
        try:
            result = _replace_album_art_bytes(
                aid,
                data,
                source="user_upload",
                expected_mb_releasegroupid=expected_rgid,
                log=log,
            )
        except AlbumArtRequestError as ex:
            raise RuntimeError(ex.message) from ex
        except BeetsError as ex:
            _app_logger.warning("Could not upload album artwork for album %s: %s", aid, type(ex).__name__)
            raise RuntimeError("Could not update album artwork") from ex
        except Exception as ex:
            raise RuntimeError("Could not update album artwork") from ex
        saved = _s(result.get("artpath") or "")
        _invalidate_lib_cache()
        return {"path": saved, "image": result.get("image") or {}}

    job = jobs.start_python(
        _do,
        label=f"Upload art: album {aid}",
        metadata={"type": "album-art-upload", "album_id": aid},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.delete("/api/albums/<int:aid>/art")
def album_delete_art(aid):
    """Quarantine local art files through the Beets engine transaction boundary."""
    album = lib.get_album(aid)
    if not album:
        return jsonify({"ok": False, "error": "Album not found"}), 404
    try:
        result = composite_workflows.delete_album_art(aid)
    except BeetsError as ex:
        _app_logger.warning("Could not delete album artwork for album %s: %s", aid, type(ex).__name__)
        return jsonify({"ok": False, "error": "Could not delete album artwork."}), 400
    except Exception as ex:
        _app_logger.warning("Could not delete album artwork for album %s: %s", aid, type(ex).__name__)
        return jsonify({"ok": False, "error": "Could not delete album artwork."}), 500
    if not result.get("ok"):
        return jsonify({"ok": False, "error": result.get("error") or "Could not delete album artwork."}), 400
    _invalidate_lib_cache()
    return jsonify({
        "ok": True,
        "removed": result.get("removed") or [],
        "removed_count": int(result.get("removed_count") or 0),
        "quarantined_art": result.get("quarantined_art") or [],
    })


@app.post("/api/albums/<int:aid>/remove")
def album_remove(aid):
    """Remove an album from the beets library and storage via engine-controlled transaction."""
    delete_files = bool((request.json or {}).get("delete_files", False))

    album_obj = lib.get_album(aid)
    if not album_obj:
        return jsonify({"ok": False, "error": f"Album {aid} not found in library"}), 404

    item_ids = [
        int(i) for i in (getattr(it, "id", None) for it in lib.items(f"album_id:{aid}"))
        if i is not None and str(i).isdigit()
    ]

    # LT-3/LT-4: this route used to plan "remove_tracks" through album
    # maintenance, which never removed anything while reporting success. It now
    # only PLANS an album cleanup (row-only unless the files are explicitly
    # confirmed); apply it through /api/albums/cleanup/apply.
    if delete_files and (request.json or {}).get("confirm_delete_files") !=             composite_workflows.DELETE_ALBUM_FILES_CONFIRMATION:
        return jsonify({"ok": False, "code": "confirmation_required",
                        "error": "Deleting the album's files needs confirm_delete_files="
                                 f"\"{composite_workflows.DELETE_ALBUM_FILES_CONFIRMATION}\"."}), 400
    try:
        plan = composite_workflows.plan_album_cleanup(aid, delete_files=delete_files, reason="album remove request")
    except (BeetsUnavailableError, BeetsError) as ex:
        _app_logger.warning("Album remove plan failed: Beets engine unavailable (%s)", type(ex).__name__)
        return jsonify({"ok": False, "error": "Beets engine is unavailable.", "error_code": "ENGINE_OFFLINE"}), 503
    status_code = 200 if plan.get("ok") else 400
    return jsonify({**plan, "item_count": len(item_ids),
                    "next_step": "POST /api/albums/cleanup/apply with this operation_id"}), status_code


@app.post("/api/albums/<int:aid>/rename")
def album_rename(aid):
    """Rename album files and relocate under library via album_relocation_v1 family."""
    album = lib.get_album(aid)
    if not album:
        return jsonify({"ok": False, "error": "Album not found"})
    label = f"Rename: {_s(getattr(album,'albumartist',''))} — {_s(getattr(album,'album',''))}"

    def _do(log, cancel_event=None):
        log.append(f"Executing album rename for album {aid} via engine transaction…")
        res = composite_workflows.relocate_album(aid, mode="rename")
        if not res.get("ok"):
            raise RuntimeError(res.get("error") or "Album rename failed")
        log.append(f"Album {aid} renamed and relocated to: {res.get('dest_dir')}")

    job = jobs.start_python(_do, label=label)
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/albums/<int:aid>/move-to-library")
def album_move_to_library(aid):
    """Move an imported album into authoritative MUSIC_ROOT via album_relocation_v1 family."""
    album = lib.get_album(aid)
    if not album:
        return jsonify({"ok": False, "error": "Album not found"}), 404
    label = f"Move to library: {_s(getattr(album,'albumartist',''))} - {_s(getattr(album,'album',''))}"

    def _do(log, cancel_event=None):
        log.append(f"Moving album {aid} into library via engine transaction…")
        res = composite_workflows.move_album_to_library(aid)
        if not res.get("ok"):
            raise RuntimeError(res.get("error") or "Move to library failed")
        log.append(f"Album {aid} relocated to: {res.get('dest_dir')}")

    job = jobs.start_python(
        _do,
        label=label,
        metadata={"type": "album-move-to-library", "album_id": aid},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/albums/<int:aid>/fix-metadata")
def album_fix_metadata(aid):
    """Patch album metadata in engine DB and sync MusicBrainz track repair via engine transaction."""
    payload = request.get_json(silent=True) or {}
    new_album = (payload.get("album") or "").strip()
    new_aa = (payload.get("albumartist") or "").strip()
    new_year = payload.get("year")
    new_mbid = (payload.get("mb_albumid") or "").strip()

    def _do(log, cancel_event=None):
        # SEC-002 / ARCH-003 Wave 24 final review: mb_albumid is deliberately
        # NOT included in this dict. ALBUM_EDITABLE_FIELDS
        # (backend/beets_control_agent.py) had MusicBrainz identity fields
        # removed from the generic PATCH allowlist in Wave 23 -- canonical
        # identity mutation must go through a controlled transaction, never
        # this untransacted endpoint. The PATCH handler rejects the *entire*
        # request (HTTP 403) if any field in the payload is not in the
        # allowlist, so bundling mb_albumid in here silently broke the
        # album/albumartist/year updates too, every time a caller supplied a
        # new mb_albumid alongside them. mb_albumid is instead handled below,
        # on its own, via the dedicated album_mb_track_repair_v1 transaction.
        updates = {}
        if new_album: updates["album"] = new_album
        if new_aa: updates["albumartist"] = new_aa
        if new_year:
            try: updates["year"] = int(new_year)
            except Exception: pass

        # Wave 24 final review round 3, section 21-24 (found only by
        # actually exercising this route over real HTTP against a real
        # two-service stack with the engine genuinely unreachable -- no
        # prior test drove this route's own engine call to fail): both
        # blocks below used to catch every exception (including
        # BeetsUnavailableError -- the engine being unreachable) and only
        # append a log line, letting `_do()` return normally either way.
        # jobs.PythonJob._run() marks the job "success" whenever the
        # callable returns without raising -- so a caller polling
        # GET /api/jobs/<id> saw status="success" for a metadata update
        # that never reached the engine at all, with the only trace of
        # the real failure buried in a free-text log line nobody parses.
        # Collect failures from both independent updates and raise once,
        # after attempting both, so a truthful "failed" status is the only
        # way this job ever completes when a requested change did not
        # actually happen.
        failures: List[str] = []

        if updates:
            try:
                # SEC-002 / ARCH-003 Wave 24 final review section 29: this
                # was still calling the generic, untransacted
                # update_album_fields (PATCH /albums/<id>) bypass instead
                # of the controlled album_metadata_repair_v1 family the
                # rest of this route already uses for mb_albumid. Album/
                # albumartist/year now go through the same controlled
                # boundary -- allowlisted fields, Plan/Apply/Verify, and a
                # real rollback path.
                res = composite_workflows.update_album_metadata(aid, updates)
                if res.get("ok"):
                    log.append(f"  Engine album fields updated: {updates}")
                else:
                    log.append(f"  Engine album update failed: {res.get('error')}")
                    failures.append(f"album fields: {res.get('error')}")
            except Exception as ex:
                log.append(f"  Engine album update failed: {ex}")
                failures.append(f"album fields: {ex}")

        if new_mbid:
            log.append("Syncing metadata from MusicBrainz via engine transaction…")
            try:
                plan_res = composite_workflows.plan_album_mb_track_repair({
                    "album_id": aid,
                    "mb_albumid": new_mbid,
                })
                if plan_res.get("ok") and plan_res.get("operation_id"):
                    apply_res = composite_workflows.apply_album_mb_track_repair(plan_res["operation_id"])
                    if not apply_res.get("ok"):
                        log.append(f"  MB track repair failed: {apply_res.get('error')}")
                        failures.append(f"mb track repair: {apply_res.get('error')}")
                elif not plan_res.get("ok"):
                    log.append(f"  MB track repair plan failed: {plan_res.get('error')}")
                    failures.append(f"mb track repair: {plan_res.get('error')}")
            except Exception as ex:
                log.append(f"  MB track repair failed: {ex}")
                failures.append(f"mb track repair: {ex}")

        _invalidate_lib_cache()

        if failures:
            raise RuntimeError("; ".join(failures))

    label = f"Fix metadata: album_id={aid}"
    job = jobs.start_python(_do, label=label)
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/albums/<int:aid>/deduplicate")
def album_deduplicate(aid):
    """Deduplicate an album's tracks against its MusicBrainz release.

    Duplicate removal (Step 4 below) runs through the reviewed duplicate
    cleanup (backend.duplicate_cleanup: re-verified pair, engine
    quarantine, rollback). What remains local is track MATCHING/renumbering
    (Step 1, `_match_tracks_from_mb` -> `_match_tracks_from_mb_shared`),
    which still issues `UPDATE items SET mb_trackid=..., track=...`
    directly against `_db()`. That function is shared by several other
    callers beyond this route (album_fix_metadata's manual-ID retag flow
    among them) and migrating it onto a controlled transaction family is
    real, standalone engineering, not attempted in this pass -- see
    docs/TECHNICAL_DEBT.md. It is correctly inventoried as an unresolved
    ARCH003_BLOCKER (domain "other"), not hidden inside the album-lifecycle
    domains this wave closes.

    For every (disc, track) slot with more than one file, a copy is treated
    as a duplicate only with positive same-recording proof: both files
    fingerprint (AcoustID) as CONFIRMED for one recording (MI-3). Proven
    copies go to a reviewed duplicate cleanup (engine quarantine, rollback);
    it is applied in this job only with confirm=true, otherwise left in
    Preview. Unproven copies and unmatched (track 0) items are kept and
    reported. Nothing is ever deleted outright.

    Body (optional): { mb_albumid: "uuid", keep_extras: true, confirm: false }
    """
    payload = request.get_json(silent=True) or {}
    mb_override = payload.get("mb_albumid", "").strip()
    keep_extras = payload.get("keep_extras", True) is not False
    # Duplicates are only ever quarantined through a reviewed cleanup; it is
    # applied in this job only with an explicit confirm, else left in Preview.
    confirm_apply = payload.get("confirm") is True

    album = lib.get_album(aid)
    if not album:
        return jsonify({"ok": False, "error": "Album not found"}), 404
    override_rgid = ""
    if mb_override:
        # ARCH-009: an override release is persisted as this album's edition
        # only if it belongs to the album's Release Group (fail closed).
        identity = _verify_album_identity(
            _s(getattr(album, "mb_releasegroupid", "") or ""), mb_override.lower(),
            resolve_release_group=_mb_release_group_for_release, require_release_group=False,
        )
        if not identity.ok:
            return jsonify({"ok": False, "error": identity.error, "code": identity.code}), 409
        mb_override, override_rgid = identity.release_id, identity.release_group_id

    label = f"Dedup: {_s(getattr(album,'albumartist',''))} — {_s(getattr(album,'album',''))}"

    def _do(log, cancel_event=None):
        _MROOT   = str(MUSIC_ROOT)

        # Resolve which MB album ID to use
        mb_albumid = mb_override
        if not mb_albumid:
            try:
                album_data = composite_workflows.get_album(int(aid))
                mb_albumid = (album_data.get("mb_albumid") or "").strip() if album_data else ""
            except BeetsUnavailableError as ex:
                log.append(f"ERROR: Engine unavailable reading album {aid}: {ex}")
                return
            except Exception:
                mb_albumid = ""
        log.append(f"MB release ID: {mb_albumid or '(none)'}")

        # If caller supplied (or resolved) a real UUID, persist it now so that
        # `beet mbsync` (Step 6) can read it from the DB.
        if mb_albumid and _MB_UUID_RE.match(mb_albumid):
            try:
                identity_fields = {"mb_albumid": mb_albumid}
                if override_rgid:
                    identity_fields["mb_releasegroupid"] = override_rgid
                composite_workflows.update_album_metadata(aid, identity_fields)
                log.append(f"  Stored mb_albumid in DB")
            except Exception as ex:
                log.append(f"  WARN storing mb_albumid: {ex}")

        # ── Step 1: Re-match items to MB to assign proper track numbers ────────
        if mb_albumid:
            matched = _match_tracks_from_mb(mb_albumid, aid, log,
                                            zero_unmatched=not keep_extras)
            log.append(f"  Re-matched {matched} track(s) from MusicBrainz")
        else:
            log.append("  No MB ID — skipping MB re-match (track numbers may be 0)")

        # ── Step 2: Load all items and group by track number ──────────────────
        def _abs(p):
            if isinstance(p, bytes):
                p = p.decode("utf-8", errors="replace")
            return (_MROOT + "/" + p) if (p and not p.startswith("/")) else (p or "")

        def _pstr(raw) -> str:
            """Decode bytes path to str."""
            if isinstance(raw, bytes):
                return raw.decode("utf-8", errors="replace")
            return str(raw or "")

        def _collision_rank(raw_path) -> int:
            """0 = primary (no .N suffix), 1+ = collision copy number."""
            stem = Path(_pstr(raw_path)).stem
            m = re.search(r'\.(\d+)$', stem)
            return int(m.group(1)) if m else 0

        try:
            raw_items = composite_workflows.find_all_items_by_album_id(int(aid))
            sorted_items = sorted(
                raw_items,
                key=lambda it: (int(it.get("track") or 0), int(it.get("disc") or 1)),
            )
        except BeetsUnavailableError as ex:
            log.append(f"ERROR: Engine unavailable loading items for album {aid}: {ex}")
            return
        except Exception as ex:
            log.append(f"ERROR loading items: {ex}")
            return

        # Normalise rows to plain dicts with string paths
        all_items = []
        for it in sorted_items:
            all_items.append({
                "id":    int(it["id"]),
                "track": int(it.get("track") or 0),
                "disc":  int(it.get("disc") or 1),
                "title": _pstr(it.get("title")),
                "path":  _pstr(it.get("path")),
                "mb_trackid": _pstr(it.get("mb_trackid")).strip().lower(),
            })

        log.append(f"Items in DB: {len(all_items)}")

        # MI-3: a slot is (disc, track) -- track 3 of disc 1 and of disc 2
        # are different songs.
        by_slot = defaultdict(list)
        for it in all_items:
            by_slot[(it["disc"], it["track"])].append(it)

        # ── Step 3: Decide what to remove -- only with positive proof ─────────
        # A copy is a duplicate only when BOTH files fingerprint (AcoustID) as
        # CONFIRMED for one recording. Unknown/unavailable/conflicting
        # evidence spares the copy and reports it; unmatched (track 0) items
        # are never removed here (keep_extras is ignored for removal).
        pairs = []
        spared = []
        summary = []
        for (disc, trk), group in sorted(by_slot.items()):
            if trk == 0:
                summary.append(f"  [00] Kept {len(group)} unmatched item(s) for review")
                spared.extend({"item_id": it["id"], "reason": "unmatched_track_number"} for it in group)
                continue
            if len(group) == 1:
                summary.append(f"  [{disc}-{trk:02d}] OK — {Path(group[0]['path']).name[:60]}")
                continue
            group.sort(key=lambda x: (_collision_rank(x["path"]), x["id"]))
            kept = group[0]
            kept_abs = _abs(kept["path"])
            for d in group[1:]:
                proof = same_recording_proof(_abs(d["path"]), kept_abs, kept.get("mb_trackid") or "")
                if proof["proven"]:
                    pairs.append({"delete_item_id": d["id"], "keep_item_id": kept["id"]})
                    summary.append(f"  [{disc}-{trk:02d}] Duplicate proven ({proof['recording_id']}): "
                                   f"{Path(d['path']).name[:40]} (keep {Path(kept['path']).name[:40]})")
                else:
                    spared.append({"item_id": d["id"], "reason": proof["reason"],
                                   "drop_status": proof["drop_status"], "keep_status": proof["keep_status"]})
                    summary.append(f"  [{disc}-{trk:02d}] SPARED (no same-recording proof: {proof['reason']}): "
                                   f"{Path(d['path']).name[:40]}")

        for line in summary:
            log.append(line)

        # ── Step 4: Reviewed duplicate cleanup (quarantine, rollbackable) ─────
        dedup_result = {"proven_pairs": len(pairs), "spared": spared, "operation_id": None,
                        "applied": False, "quarantined": 0}
        if not pairs:
            log.append("Nothing proven duplicate — nothing removed.")
        else:
            try:
                plan = _duplicate_cleanup.plan_reviewed_cleanup(pairs, reason=f"Album {aid} deduplicate")
            except (BeetsUnavailableError, BeetsError) as ex:
                log.append(f"  Engine unavailable planning duplicate cleanup: {ex}")
                raise RuntimeError(f"Duplicate cleanup planning failed: {ex}")
            for skipped in plan.get("skipped") or []:
                log.append(f"  Kept item {skipped.get('delete_item_id')}: "
                           f"{', '.join(skipped.get('reasons') or [])}")
            if plan.get("ok"):
                dedup_result["operation_id"] = plan["operation_id"]
                if confirm_apply:
                    store = composite_workflows.get_default_store()
                    if store.transition(plan["operation_id"], "Preview", "Approved",
                                        metadata={"approved_by": "operator confirmed album deduplicate"}) is None:
                        raise RuntimeError(f"Duplicate cleanup {plan['operation_id']} changed state before it "
                                           "could be approved; nothing was quarantined.")
                    applied = _duplicate_cleanup.apply_reviewed_cleanup(plan["operation_id"])
                    if not applied.get("ok"):
                        raise RuntimeError(f"Duplicate cleanup {plan['operation_id']} did not apply "
                                           f"({applied.get('status') or applied.get('code')}): "
                                           f"{applied.get('error') or 'see transaction log'}")
                    dedup_result.update(applied=bool(applied.get("ok")),
                                        quarantined=len(applied.get("removed") or []))
                    log.append(f"  Quarantined {dedup_result['quarantined']} proven duplicate(s) "
                               f"({applied.get('status')}); rollback via transaction {plan['operation_id']}.")
                else:
                    log.append(f"  Preview transaction {plan['operation_id']} created; approve and apply it "
                               "to quarantine the proven duplicates (nothing removed yet).")
            else:
                log.append("  No pair passed duplicate re-verification; nothing removed.")
        if spared:
            log.append(f"  {len(spared)} item(s) kept for review (no positive same-recording proof).")

        # ── Step 5: Strip year suffix from album name (anti-double-year) ──────
        _strip_year_from_album_name(aid, log)

        # ── Step 6: Relocate album via engine ──────────────────────────────────
        try:
            rel_res = composite_workflows.relocate_album(aid, mode="rename")
            if rel_res.get("ok"):
                log.append(f"  Album relocated to: {rel_res.get('dest_dir')}")
            else:
                log.append(f"  Relocation warning: {rel_res.get('error')}")
        except Exception as ex:
            log.append(f"  Relocation warning: {ex}")

        # ── Step 7: Final track listing ───────────────────────────────────────
        _invalidate_lib_cache()
        try:
            final_items = composite_workflows.find_all_items_by_album_id(int(aid))
            sorted_final = sorted(final_items, key=lambda it: int(it.get("track") or 0))
            log.append(f"Final: {len(sorted_final)} track(s)")
            for it in sorted_final:
                pth = it.get("path")
                fname = Path(
                    pth.decode("utf-8", errors="replace") if isinstance(pth, bytes) else str(pth or "")
                ).name
                trk = int(it.get("track") or 0)
                log.append(f"  [{trk:02d}] {fname[:70]}")
        except Exception as ex:
            log.append(f"Final listing warning: {ex}")
        return dedup_result

    job = jobs.start_python(_do, label=label)
    return jsonify({"ok": True, "job_id": job.job_id})


_DISK_ART_MIME_BY_EXT = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png", "gif": "image/gif",
    "webp": "image/webp", "bmp": "image/bmp",
}


@app.get("/api/disk-art")
def disk_art_serve():
    """Serve an album art image from the music library or downloads root
    (security: path must be under one of those roots)."""
    path = request.args.get("path", "").strip()
    if not path:
        return ("", 404)
    p = Path(os.path.realpath(path))
    if not any(_path_is_under(p, root) for root in _BROWSE_ALLOWED_ROOTS):
        return ("", 403)
    if not p.exists() or not p.is_file():
        return ("", 404)
    sfx = p.suffix.lower().lstrip(".")
    mime = _DISK_ART_MIME_BY_EXT.get(sfx)
    if not mime:
        # Unrecognized extension: don't guess "image/jpeg" and serve
        # arbitrary file content under a misleading content-type.
        return ("", 404)
    return send_file(str(p), mimetype=mime)


@app.post("/api/albums/<int:aid>/match")
def match_album(aid):
    """Set mb_albumid, sync full metadata from MusicBrainz, write tags, then move files
    into the correct Artist/Album/Track - Title structure.
    Accepts a release URL, release UUID, or release-group URL (auto-resolved)."""
    payload  = request.get_json(silent=True) or {}
    mb_input = payload.get("mb_id", "").strip()
    if not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                     mb_input, re.I):
        return jsonify({"ok": False, "error": "Paste a MusicBrainz release URL or UUID"})
    album = lib.get_album(aid)
    if not album:
        return jsonify({"ok": False, "error": "Album not found"})
    label = f"Match: {album.albumartist or album.album or f'album {aid}'}"

    def _do(log, cancel_event=None):
        # 0 ── resolve release-group → release if needed
        mb_albumid = _resolve_mb_release_id(mb_input, log)
        if not mb_albumid:
            raise RuntimeError("Could not extract a MusicBrainz UUID from the input")

        # 1 ── validate before touching tags/paths. MI-8: a track that does not
        # match the selected release is NOT deleted (a wrong manual match, an
        # incomplete tracklist or a bad fingerprint lookup must never cost the
        # user a file). The match stops before any write and the nonmatching
        # tracks are reported for review; nothing is stamped with the release.
        log.append("[1/6] Validating current album against selected MusicBrainz release ...")
        plan = _album_mb_match_plan(aid, mb_albumid, log)
        matched_count = int(plan.get("matched_count") or 0)
        actual_count = int(plan.get("actual_count") or 0)
        expected_count = int(plan.get("expected_count") or 0)
        log.append(
            f"  Selected release: {plan.get('release_title') or mb_albumid}"
        )
        log.append(
            f"  Current album matches {matched_count}/{actual_count} local track(s) "
            f"against {expected_count} MusicBrainz track(s)."
        )
        unmatched_items = list(plan.get("unmatched_items") or [])
        if unmatched_items or matched_count <= 0:
            log.append(
                f"  {len(unmatched_items)} track(s) do not match the selected release; "
                "nothing was changed and no file was removed. Review them (or pick another "
                "release) before matching."
            )
            for item in unmatched_items[:50]:
                log.append(f"    needs review: {item.get('filename') or item.get('title')} (item {item.get('id')})")
            # Raised (not returned) so the job reads as not-done, never as a
            # successful match.
            raise RuntimeError(
                f"requires_review: {len(unmatched_items)} track(s) do not match the selected release "
                f"({matched_count}/{actual_count} matched); nothing was changed."
            )

        # 2 ── set mb_albumid on both items AND the album record
        log.append(f"[2/6] Setting mb_albumid={mb_albumid} on matched items + album record ...")
        metadata_result = composite_workflows.update_album_metadata(aid, {"mb_albumid": mb_albumid})
        _require_attach_stage_success(metadata_result, "match album metadata update")
        meta_op_id = metadata_result.get("operation_id") if isinstance(metadata_result, dict) else None
        log.append(f"  albums.mb_albumid set to {mb_albumid}")

        # 3 ── match & number tracks from MB release data. This local DB pass
        # is best-effort evidence enrichment; the required identity sync below
        # still owns the authoritative Beets repair/verification stage.
        log.append("[3/6] Numbering tracks from MusicBrainz release ...")
        try:
            matched = _match_tracks_from_mb(mb_albumid, aid, log)
            log.append(f"  -> {matched} track(s) matched and numbered.")
        except Exception as ex:
            log.append(f"  WARN: track numbering failed: {ex}")

        # 4 ── sync all metadata (titles, track numbers, artist, year...) from MusicBrainz
        log.append("[4/6] Syncing metadata from MusicBrainz (mbsync) ...")
        try:
            repair_plan = composite_workflows.plan_album_mb_track_repair({"album_id": aid, "mb_albumid": mb_albumid})
            _require_attach_stage_success(repair_plan, "match album MB track repair plan")
            operation_id = repair_plan.get("operation_id")
            if operation_id:
                repair_apply = composite_workflows.apply_album_mb_track_repair(operation_id, write_tags=True)
                _require_attach_stage_success(repair_apply, "match album MB track repair apply")
        except Exception as repair_exc:
            _compensate_committed_metadata_or_raise(aid, meta_op_id, "match album MB track repair", repair_exc, log)

        # Strip trailing year from album name before move (avoid "Album (2025) (2025)")
        _strip_year_from_album_name(aid, log)

        # 5 ── write tags to audio files
        log.append("[5/6] Writing tags to audio files ...")
        write_result = composite_workflows.update_album_metadata(aid, {}, force_write_tags=True)
        _require_attach_stage_success(write_result, "match album tag write")
        # A relocation failure after this point should compensate the most
        # recently committed metadata operation (this tag write), not the
        # earlier mb_albumid-only one from step 2 -- rolling back a
        # superseded operation against the album's now-current state would
        # be incorrect.
        if isinstance(write_result, dict) and write_result.get("operation_id"):
            meta_op_id = write_result.get("operation_id")

        # 6 ── move / rename files into Artist/Album/Track - Title structure
        log.append("[6/6] Moving files into library structure ...")
        _run_attach_relocation_stage(aid, meta_op_id, "match album relocation", log)

        _invalidate_lib_cache()

        # report final paths
        try:
            updated = lib.get_album(aid)
            if updated:
                items = list(updated.items())
                log.append(f"✓ {len(items)} track(s) organised:")
                for it in sorted(items, key=lambda i: i.track or 0):
                    log.append(f"  {_s(it.path)}")
        except Exception:
            pass

    job = jobs.start_python(_do, label=label)
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/albums/<int:album_id>/cleanup/plan")
@app.post("/api/albums/cleanup/plan")
def plan_album_cleanup_route(album_id: int = 0):
    payload = request.get_json(silent=True) or {}
    target_album_id = album_id or int(payload.get("album_id") or 0)
    if not target_album_id:
        return jsonify({"ok": False, "error": "album_id required"}), 400

    # LT-4: row-only by default. Deleting the audio files too needs the
    # explicit confirmation phrase on the PLAN; Apply never widens it.
    delete_files = payload.get("delete_files") is True
    if delete_files and payload.get("confirm_delete_files") != composite_workflows.DELETE_ALBUM_FILES_CONFIRMATION:
        return jsonify({"ok": False, "code": "confirmation_required",
                        "error": "Deleting the album's files needs confirm_delete_files="
                                 f"\"{composite_workflows.DELETE_ALBUM_FILES_CONFIRMATION}\"."}), 400
    try:
        res = composite_workflows.plan_album_cleanup(target_album_id, delete_files=delete_files,
                                                     reason=_s(payload.get("reason")))
        status_code = 200 if res.get("ok") else 400
        return jsonify(res), status_code
    except BeetsUnavailableError as ex:
        return jsonify({"ok": False, "error": f"Beets engine unavailable: {ex}"}), 503
    except BeetsError as ex:
        return jsonify({"ok": False, "error": str(ex)}), 400
    except Exception as ex:
        _app_logger.exception("plan_album_cleanup_route failed for album_id=%s", target_album_id)
        return jsonify({"ok": False, "error": "Album cleanup planning failed"}), 500


@app.post("/api/albums/cleanup/apply")
def apply_album_cleanup_route():
    payload = request.get_json(silent=True) or {}
    op_id = _s(payload.get("operation_id")).strip()
    if not op_id:
        return jsonify({"ok": False, "error": "operation_id required"}), 400

    # LT-4: Apply needs an Approved plan. The operator's Apply request on a
    # reviewed ROW-ONLY Preview plan is that approval; a plan that deletes
    # files must be approved explicitly (transactions approve route, or
    # confirm_delete_files here) -- never implied.
    store = composite_workflows.get_default_store()
    try:
        tx = store.get(op_id)
    except KeyError:
        tx = {}  # unknown/malformed id: apply_album_cleanup reports it below
    meta = tx.get("metadata") or {}
    if tx.get("status") == "Preview" and meta.get("mutation_family") == composite_workflows.ALBUM_CLEANUP_FAMILY:
        if meta.get("delete_files") and                 payload.get("confirm_delete_files") != composite_workflows.DELETE_ALBUM_FILES_CONFIRMATION:
            return jsonify({"ok": False, "code": "confirmation_required", "error_kind": "other", "mutated": False,
                            "error": "This plan deletes files; confirm_delete_files is required."}), 400
        if store.transition(op_id, "Preview", "Approved",
                            metadata={"approved_by": "operator apply (album cleanup)"}) is None:
            return jsonify({"ok": False, "code": "not_preview", "error_kind": "other", "mutated": False,
                            "error": "The cleanup plan changed state before it could be approved; "
                                     "nothing was changed."}), 409

    # error_kind is the authoritative UI signal -- the frontend must not
    # re-derive it from the "error" text. Shared with the generic
    # /api/transactions/<id>/apply route.
    body, status = album_cleanup_apply_response(op_id)
    return jsonify(body), status


@app.get("/api/album-art-url")
def album_art_url_api():
    """Return Discogs cover art URL for an artist+album (cached)."""
    artist = request.args.get("artist", "").strip()
    album  = request.args.get("album",  "").strip()
    if not (artist or album):
        return jsonify({"ok": False, "error": "artist or album required"})
    key = f"{artist.lower()}::{album.lower()}"
    with _album_art_cache_lock:
        if key in _album_art_cache:
            return jsonify({"ok": True, "url": _album_art_cache[key]})
    url = _fetch_album_art(artist, album)
    with _album_art_cache_lock:
        _album_art_cache[key] = url
    return jsonify({"ok": True, "url": url})


@app.post("/api/save-album-art")
def save_album_art():
    """Fetch art from Discogs and save it through the Beets engine for one album."""
    body = request.get_json(silent=True) or {}
    try:
        aid, album_obj = _resolve_album_art_request_album(body)
    except AlbumArtRequestError as ex:
        return jsonify({"ok": False, "error": ex.message}), ex.status

    artist = (_s(body.get("artist") or "").strip()
              or _s(getattr(album_obj, "albumartist", "") or getattr(album_obj, "artist", "") or "").strip())
    album_name = (_s(body.get("album") or "").strip()
                  or _s(getattr(album_obj, "album", "") or "").strip())
    if not (artist or album_name):
        return jsonify({"ok": False, "error": "artist and album required"}), 400

    status = _album_art_status(aid)
    if status and status.get("has_local_art"):
        return jsonify({
            "ok": True,
            "album_id": aid,
            "path": _s(status.get("local_art_path") or ""),
            "cached": True,
        })

    key = f"{artist.lower()}::{album_name.lower()}"
    with _album_art_cache_lock:
        url = _album_art_cache.get(key)
    url = url or _fetch_album_art(artist, album_name)
    if not url:
        return jsonify({"ok": False, "error": "No art found on Discogs"}), 404
    with _album_art_cache_lock:
        _album_art_cache[key] = url
    saved = _save_art_to_disk(
        url,
        aid,
        expected_mb_releasegroupid=_album_art_expected_release_group(album_obj),
        source="discogs",
    )
    if not saved:
        return jsonify({"ok": False, "error": "Download failed"}), 400
    _invalidate_lib_cache()
    return jsonify({"ok": True, "album_id": aid, "path": saved})


@app.post("/api/fetch-missing-art")
def fetch_missing_art():
    """Background job: find albums missing usable local art and repair them."""
    body, status = start_fetch_missing_art(request.get_json(silent=True) or {})
    return json_route_result(body, status)


@app.post("/api/rebuild-album-art")
def rebuild_album_art():
    """Background job: rebuild album art for every actionable album."""
    payload = request.get_json(silent=True) or {}
    if payload.get("confirmed") is not True:
        return jsonify({"ok": False, "error": "Confirmation is required before rebuilding album art"}), 400

    def _do(log, cancel_event=None):
        started_at = time.time()
        albums = list(lib.albums([]))
        trash_root = METADATA_CACHE_ROOT / "album-art-rebuild-trash" / time.strftime("%Y%m%d-%H%M%S")
        rebuilt_items: List[Dict[str, Any]] = []
        restored_items: List[Dict[str, Any]] = []
        failed_items: List[Dict[str, Any]] = []
        unresolved_items: List[Dict[str, Any]] = []
        skipped = 0
        removed_existing = 0

        log.append(f"Full album art rebuild: checking {len(albums)} album(s).")
        log.append("Current art is quarantined first and restored if no fresh cover is confirmed.")

        for idx, album in enumerate(albums, start=1):
            if cancel_event and cancel_event.is_set():
                log.append("[cancelled]")
                break
            try:
                aid = int(getattr(album, "id", 0) or 0)
            except Exception:
                aid = 0
            if not aid:
                skipped += 1
                continue
            status = _album_art_status(aid)
            entry = _album_art_repair_entry(album, status)
            artist_name = _s(entry.get("albumartist") or "")
            album_name = _s(entry.get("album") or "")
            log.append(f"[{idx}/{len(albums)}] Rebuilding: {artist_name} - {album_name}")
            if not entry.get("actionable"):
                log.append(f"  unresolved: {entry.get('reason') or 'Album folder could not be resolved'}")
                unresolved_items.append({**entry, "status": "unresolved"})
                continue
            try:
                result = _repair_album_art(aid, log, cancel_event, force=True, trash_root=trash_root)
                removed_existing += int(result.get("quarantined_count") or 0)
                if result.get("status") == "saved" and result.get("saved_path"):
                    log.append(f"  confirmed fresh art: {Path(_s(result.get('saved_path'))).name}")
                    rebuilt_items.append(result)
                elif int(result.get("restored_current_art") or 0) > 0:
                    log.append("  no fresh replacement confirmed; restored previous art")
                    restored_items.append(result)
                    failed_items.append(result)
                else:
                    log.append(f"  failed: {result.get('error') or 'unknown error'}")
                    failed_items.append(result)
                time.sleep(0.42)
            except Exception as ex:
                log.append(f"  warning: {ex}")
                failed_items.append({**entry, "status": "failed", "source": "", "error": str(ex)})

        _invalidate_lib_cache()
        refreshed = _art_repair_build_report()
        summary = {
            "ok": True,
            "mode": "full_rebuild",
            "started_at": started_at,
            "finished_at": time.time(),
            "total_albums": len(albums),
            "attempted": len(rebuilt_items) + len(failed_items) + len(unresolved_items),
            "rebuilt": len(rebuilt_items),
            "saved": len(rebuilt_items),
            "fetchart_saved": sum(1 for item in rebuilt_items if item.get("source") == "fetchart"),
            "fallback_saved": sum(1 for item in rebuilt_items if item.get("source") == "discogs"),
            "failed": len(failed_items),
            "restored": len(restored_items),
            "unresolved": len(unresolved_items),
            "skipped": skipped,
            "removed_existing": removed_existing,
            "trash_root": str(trash_root),
            "saved_items": rebuilt_items,
            "failed_items": failed_items,
            "restored_items": restored_items,
            "unresolved_items": unresolved_items,
            "remaining_items": refreshed.get("items") or [],
            "counts": refreshed.get("counts") or {},
        }
        _art_repair_save_last(summary)
        log.append(
            f"Done - {len(rebuilt_items)} rebuilt, {len(restored_items)} restored, "
            f"{len(failed_items)} failed, {len(unresolved_items)} unresolved, "
            f"{removed_existing} old art file(s) quarantined"
        )
        return summary

    job = jobs.start_python(
        _do,
        label="Rebuild Album Art",
        metadata={"type": "album-art-rebuild", "mode": "full_rebuild"},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.get("/api/library")
def library_full():
    """Walk /data/media/music on disk + inject library items whose files are missing (shown in red).
    Results are cached for _LIB_CACHE_TTL seconds; pass ?refresh=1 to force a rebuild.
    Supports ?limit=N&offset=M for fast paginated queries directly from the engine.
    """
    limit_arg = request.args.get("limit")
    if limit_arg is not None:
        try:
            limit = min(max(1, int(limit_arg)), 500)
        except Exception:
            limit = 50
        try:
            offset = max(0, int(request.args.get("offset", 0)))
        except Exception:
            offset = 0

        try:
            res = beets_adapter.get_items_page(offset=offset, limit=limit)
            raw_items = res.get("items", [])
            items = [_library_track_dict(r) for r in raw_items]
            return jsonify({
                "items": items,
                "pagination": {
                    "limit": limit,
                    "offset": offset,
                    "returned": len(items),
                    "total": int(res.get("total", len(items)))
                }
            })
        except Exception as ex:
            if isinstance(ex, (BeetsAdapterConnectionError, BeetsAdapterTimeoutError, BeetsUnavailableError, TimeoutError)):
                _app_logger.warning("get_items_page unavailable: %s: %s", type(ex).__name__, ex)
                return jsonify({
                    "error": "Beets library is unavailable.",
                    "error_code": "ENGINE_OFFLINE",
                    "status": "unavailable"
                }), 503
            raise

    return jsonify(get_library_payload(
        force=request.args.get("refresh", "0") == "1",
        include_tracks=request.args.get("include_tracks", "0") == "1",
        include_disk_only=(
            request.args.get("include_disk_only", "0") == "1"
            or request.path.startswith("/api/acquisition")
        ),
    ))


@app.post("/api/albums/<int:aid>/ai-suggest")
def ai_suggest_album(aid):
    album = lib.get_album(aid)
    if not album:
        return jsonify({"ok": False, "error": "Album not found"})
    log: List[str] = []
    result = _ai_suggest_album_internal(album, log, existing_album_id=aid)
    return jsonify(result)


@app.post("/api/albums/batch-ai-suggest")
def batch_ai_suggest():
    payload = request.get_json(silent=True) or {}
    limit = min(int(payload.get("limit") or 500), 2000)
    if not _ai_api_key():
        return jsonify({"ok": False, "error": "AI is not configured (set OPENAI_API_KEY, OPENROUTER_API_KEY, or AI_API_KEY)"}), 400

    def _do(log, cancel_event=None):
        try:
            unmatched_res = composite_workflows.get_unmatched_review_items(limit=min(limit, 1000), include_singletons=False)
            album_ids = [int(r["id"]) for r in unmatched_res.get("albums", [])]
        except BeetsUnavailableError as ex:
            raise RuntimeError(f"Could not load unlinked albums: Beets engine unavailable: {ex}")
        except Exception as ex:
            raise RuntimeError(f"Could not load unlinked albums: {ex}")
        log.append(f"Batch AI suggest: {len(album_ids)} unlinked album(s)")

        existing = _load_album_mb_suggestions()
        results: Dict[str, Any] = dict(existing)

        success = skipped = failed = 0
        for idx, cur_aid in enumerate(album_ids, start=1):
            if cancel_event and cancel_event.is_set():
                log.append("Cancelled.")
                break
            str_aid = str(cur_aid)
            album = lib.get_album(cur_aid)
            if not album:
                failed += 1
                continue
            label = f"{album.albumartist or '?'} — {album.album or '?'}"
            prev = existing.get(str_aid, {})
            if prev.get("confidence") in ("high", "medium") and prev.get("mb_valid"):
                skipped += 1
                log.append(f"[{idx}/{len(album_ids)}] Skip (already suggested): {label}")
                continue
            log.append(f"[{idx}/{len(album_ids)}] Suggest: {label}")
            try:
                res = _ai_suggest_album_internal(album, log, existing_album_id=cur_aid)
                sug = res.get("suggestion") or {}
                ev  = res.get("evidence") or {}
                results[str_aid] = {
                    "album_id":       cur_aid,
                    "album":          album.album or "",
                    "albumartist":    album.albumartist or "",
                    "mb_albumid":     sug.get("mb_albumid", ""),
                    "confidence":     sug.get("confidence", "low"),
                    "reason":         sug.get("reason", ""),
                    "mb_valid":       bool(sug.get("mb_valid")),
                    "mb_url":         sug.get("mb_url", ""),
                    "top_candidates": (ev.get("top_candidates") or [])[:3],
                    "preflight":      ev.get("preflight"),
                    "updated_at":     time.time(),
                }
                success += 1
            except Exception as ex:
                failed += 1
                log.append(f"  ERROR: {ex}")

            try:
                _get_ai_batch_store().save_suggestions_cache(results)
            except Exception:
                pass

        log.append(f"Done: {success} suggested, {skipped} skipped, {failed} failed")
        return {"success": success, "skipped": skipped, "failed": failed,
                "total": len(album_ids)}

    job = jobs.start_python(
        _do,
        label=f"AI suggest: up to {limit} unlinked album(s)",
        metadata={"type": "batch-ai-suggest", "limit": limit},
    )
    return jsonify({"ok": True, "job_id": job.job_id, "limit": limit})


@app.get("/api/albums/<int:aid>/mb-format")
def album_mb_format(aid):
    """Return album tracks formatted for MusicBrainz track parser + submission URL."""
    album = lib.get_album(aid)
    if not album:
        return jsonify({"ok": False, "error": "Not found"}), 404
    tracks = sorted(album.items(), key=lambda t: (t.disc or 0, t.track or 0))
    lines = []
    for t in tracks:
        length = getattr(t, "length", 0) or 0
        mins, secs = int(length // 60), int(length % 60)
        dur = f"{mins}:{secs:02d}" if length else ""
        artist_part = f" - {t.artist}" if t.artist and t.artist != album.albumartist else ""
        lines.append(f"{t.track or ''}. {t.title or '(unknown)'}{artist_part} ({dur})" if dur
                     else f"{t.track or ''}. {t.title or '(unknown)'}{artist_part}")
    track_text = "\n".join(lines)
    # MusicBrainz add-release URL with pre-filled fields
    params = {}
    if album.albumartist:  params["artist"]      = album.albumartist
    if album.album:        params["title"]        = album.album
    if album.year:         params["year"]         = str(album.year)
    mb_url = "https://musicbrainz.org/release/add?" + urllib.parse.urlencode(params) if params else "https://musicbrainz.org/release/add"
    return jsonify({
        "ok":          True,
        "track_text":  track_text,
        "mb_url":      mb_url,
        "album":       album.album       or "",
        "albumartist": album.albumartist or "",
        "year":        album.year        or "",
        "track_count": len(tracks),
    })


@app.get("/api/albums/<int:aid>/tracks")
def album_tracks(aid):
    album = lib.get_album(aid)
    if not album:
        return jsonify({"ok": False, "error": "Not found"}), 404
    items = sorted(album.items(), key=lambda i: (i.disc or 0, i.track or 0))
    tracks = [_library_track_dict(i) for i in items]
    known_paths = {
        str(Path(t.get("path") or "").resolve(strict=False)).lower()
        for t in tracks
        if t.get("path")
    }
    all_path_items: Dict[str, Any] = {}
    try:
        for other in lib.items([]):
            raw_path = _s(getattr(other, "path", "") or "")
            if not raw_path:
                continue
            abs_path = raw_path if Path(raw_path).is_absolute() else str(MUSIC_ROOT / raw_path)
            all_path_items[str(Path(abs_path).resolve(strict=False)).lower()] = other
    except Exception:
        all_path_items = {}
    album_dir = _album_dir_for_art(album)
    if album_dir and album_dir.exists():
        disc_sub_re = re.compile(r'^(?:cd|disc|disk)\s*0*(\d+)$', re.IGNORECASE)
        disk_files: List[Path] = []
        try:
            disk_files.extend(
                sorted(
                    [p for p in album_dir.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXT],
                    key=lambda p: p.name.lower(),
                )
            )
            for sub in sorted([p for p in album_dir.iterdir() if p.is_dir()], key=lambda p: p.name.lower()):
                if not disc_sub_re.match(sub.name):
                    continue
                disk_files.extend(
                    sorted(
                        [p for p in sub.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXT],
                        key=lambda p: p.name.lower(),
                    )
                )
        except Exception:
            disk_files = []

        for fpath in disk_files:
            key = str(fpath.resolve(strict=False)).lower()
            if key in known_paths:
                continue
            other_item = all_path_items.get(key)
            if other_item:
                other_album_id = int(getattr(other_item, "album_id", 0) or 0)
                if other_album_id == aid:
                    continue
                if other_album_id:
                    row = _library_track_dict(other_item)
                    row["status"] = "other_album"
                    row["other_album_id"] = other_album_id
                    row["imported"] = True
                    row["disk_only"] = False
                    row["missing"] = False
                    tracks.append(row)
                    continue
            disc = 1
            dm = disc_sub_re.match(fpath.parent.name)
            if dm:
                disc = int(dm.group(1))
            tracks.append({
                "id": 0,
                "album_id": aid,
                "path": str(fpath),
                "title": fpath.stem,
                "track": 0,
                "disc": disc,
                "tracktotal": 0,
                "ok": False,
                "missing": False,
                "imported": False,
                "disk_only": True,
                "status": "not_imported",
                "mb_trackid": "",
            })
    tracks.sort(key=lambda t: (
        int(t.get("disc") or 1),
        int(t.get("track") or 999),
        _s(t.get("title") or "").lower(),
    ))
    return jsonify({
        "ok": True,
        "album_id": aid,
        "tracks": tracks,
    })


@app.post("/api/albums/<int:target_aid>/merge-split-album")
def album_merge_split_album(target_aid):
    """Merge visible split-album item rows into the selected album without touching audio files."""
    payload = request.get_json(silent=True) or {}
    try:
        target_id = int(target_aid or 0)
        source_id = int(payload.get("source_album_id") or 0)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid album id"}), 400
    item_ids = sorted({
        int(value)
        for value in (payload.get("item_ids") or [])
        if str(value).isdigit() and int(value) > 0
    })
    dry_run = bool(payload.get("dry_run", False))
    if target_id <= 0 or source_id <= 0 or target_id == source_id:
        return jsonify({"ok": False, "error": "Choose different source and target album IDs"}), 400
    if not item_ids:
        return jsonify({"ok": False, "error": "No item rows were selected for split-album merge"}), 400

    def _do(log):
        log.append(
            f"Split-album merge: album_id {source_id} → album_id {target_id} "
            f"({len(item_ids)} selected row(s))"
        )
        try:
            target = composite_workflows.get_album(target_id)
            source = composite_workflows.get_album(source_id)
            if not target:
                raise RuntimeError(f"Target album_id {target_id} was not found")
            if not source:
                raise RuntimeError(f"Source album_id {source_id} was not found")

            target_rows = composite_workflows.find_all_items_by_album_id(target_id)
            target_dir = _album_db_folder_from_item_paths(target_rows)
            if not target_dir:
                raise RuntimeError(f"Could not resolve the target folder for album_id {target_id}")

            source_items = composite_workflows.find_all_items_by_album_id(source_id)
            source_items_by_id = {int(it["id"]): it for it in source_items if it.get("id")}
            selected = [source_items_by_id[iid] for iid in item_ids if iid in source_items_by_id]
            found_ids = {int(row["id"]) for row in selected}
            missing_ids = [iid for iid in item_ids if iid not in found_ids]
            if missing_ids:
                raise RuntimeError(f"Selected item row(s) not found: {missing_ids}")
        except BeetsUnavailableError as ex:
            raise RuntimeError(f"Engine unavailable during split album merge: {ex}")

        move_ids: List[int] = []
        skipped: List[str] = []
        for row in selected:
            row_album_id = int(row.get("album_id") or 0)
            if row_album_id != source_id:
                skipped.append(f"id:{int(row['id'])} belongs to album_id {row_album_id}")
                continue
            raw_path = _s(row.get("path"))
            fpath = Path(raw_path)
            if not fpath.is_absolute():
                fpath = MUSIC_ROOT / raw_path
            if not _path_is_under(fpath, target_dir):
                skipped.append(f"id:{int(row['id'])} is outside {target_dir}")
                continue
            move_ids.append(int(row["id"]))

        for line in skipped[:8]:
            log.append(f"  Skipped {line}")
        if not move_ids:
            raise RuntimeError("No selected rows were safe to merge")

        if dry_run:
            log.append(f"Dry run: would merge {len(move_ids)} item row(s); no DB changes made")
            return {
                "dry_run": True,
                "source_album_id": source_id,
                "target_album_id": target_id,
                "item_count": len(move_ids),
                "source_album_deleted": False,
            }

        # An album-row merge (ARCH-020): ownership only, one Release Group
        # and Release ID, free slots; the source row is retired only if the
        # move empties it. Rollback: /api/transactions/<operation_id>/rollback.
        merge_res = composite_workflows.merge_split_album_items(target_id, source_id, move_ids)
        if not merge_res.get("ok"):
            raise RuntimeError(merge_res.get("error") or "Engine rejected split-album merge")
        source_album_deleted = bool(merge_res.get("source_album_deleted"))

        _invalidate_lib_cache()
        log.append(
            f"Merged {len(move_ids)} item row(s) into album_id {target_id}; "
            + (f"removed empty album_id {source_id}" if source_album_deleted else f"album_id {source_id} still has rows")
        )
        return {
            "dry_run": False,
            "source_album_id": source_id,
            "target_album_id": target_id,
            "item_count": len(move_ids),
            "source_album_deleted": source_album_deleted,
            "operation_id": merge_res.get("operation_id"),
        }

    job = jobs.start_python(
        _do,
        label=f"Merge split album {source_id} → {target_id}",
        metadata={
            "type": "split-album-merge",
            "source_album_id": source_id,
            "target_album_id": target_id,
            "item_count": len(item_ids),
        },
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.get("/api/albums")
def albums():
    q     = request.args.get("q", "").strip()
    limit = min(int(request.args.get("limit", 100)), 500)
    rows  = []
    for album in lib.albums(q.split() if q else []):
        rows.append(album_dict(album))
        if len(rows) >= limit:
            break
    return jsonify({"count": len(rows), "albums": rows})


# ── Recent ────────────────────────────────────────────────────────────────────

@app.get("/api/recent")
def recent():
    limit = min(int(request.args.get("limit", 50)), 200)
    # Filter: items whose file exists on disk AND whose path is under the music library root.
    # Fall back to all items if none qualify (e.g., beets DB has legacy paths).
    music_root = str(MUSIC_ROOT)
    all_sorted = sorted(lib.items([]), key=lambda x: getattr(x, "added", 0), reverse=True)
    library_items = [i for i in all_sorted if _s(i.path).startswith(music_root)]
    if not library_items:
        # Fall back: any item whose file actually exists on disk
        library_items = [i for i in all_sorted if Path(_s(i.path)).exists()]
    if not library_items:
        library_items = all_sorted
    return jsonify({"items": [item_dict(i) for i in library_items[:limit]]})


@app.get("/api/library/mbid-status")
def library_mbid_status():
    """MusicBrainz coverage for albums/tracks already under /data/media/music."""
    total_albums = missing_album_mb = total_tracks = missing_track_mb = 0
    item_release_gap_rows = 0
    albums_with_item_release_gaps = 0
    track_recording_gap_rows = 0
    albums_with_track_recording_gaps = 0
    inferred_album_mbid_rows = 0
    albums_with_template_tokens = 0
    template_token_rows = 0
    examples: List[Dict[str, Any]] = []
    release_gap_examples: List[Dict[str, Any]] = []
    track_gap_examples: List[Dict[str, Any]] = []
    template_token_examples: List[Dict[str, Any]] = []
    try:
        for album in lib.albums([]):
            items = list(album.items())
            music_items = [i for i in items if _is_music_root_path(getattr(i, "path", ""))]
            if not music_items:
                continue
            total_albums += 1
            total_tracks += len(music_items)
            album_mb = _s(getattr(album, "mb_albumid", "") or "").strip().lower()
            if not album_mb:
                missing_album_mb += 1
                item_release_ids = {
                    _s(getattr(i, "mb_albumid", "") or "").strip().lower()
                    for i in music_items
                    if _s(getattr(i, "mb_albumid", "") or "").strip()
                }
                if len(item_release_ids) == 1:
                    inferred_album_mbid_rows += 1
                if len(examples) < 12:
                    examples.append({
                        "album_id": album.id,
                        "artist": _s(getattr(album, "albumartist", "") or ""),
                        "album": _s(getattr(album, "album", "") or ""),
                        "tracks": len(music_items),
                        "year": int(getattr(album, "year", 0) or 0),
                    })
            album_release_gaps = 0
            album_track_gaps = 0
            album_template_tokens = 0
            for item in music_items:
                item_mb = _s(getattr(item, "mb_albumid", "") or "").strip().lower()
                if album_mb and (not item_mb or item_mb != album_mb):
                    item_release_gap_rows += 1
                    album_release_gaps += 1
                    if len(release_gap_examples) < 8:
                        release_gap_examples.append({
                            "album_id": album.id,
                            "item_id": getattr(item, "id", 0),
                            "artist": _s(getattr(album, "albumartist", "") or ""),
                            "album": _s(getattr(album, "album", "") or ""),
                            "title": _s(getattr(item, "title", "") or ""),
                            "album_mb_albumid": album_mb,
                            "item_mb_albumid": item_mb,
                        })
                if not _s(getattr(item, "mb_trackid", "") or "").strip():
                    missing_track_mb += 1
                    track_recording_gap_rows += 1
                    album_track_gaps += 1
                    if len(track_gap_examples) < 8:
                        track_gap_examples.append({
                            "album_id": album.id,
                            "item_id": getattr(item, "id", 0),
                            "artist": _s(getattr(album, "albumartist", "") or ""),
                            "album": _s(getattr(album, "album", "") or ""),
                            "title": _s(getattr(item, "title", "") or ""),
                            "track": int(getattr(item, "track", 0) or 0),
                            "disc": int(getattr(item, "disc", 1) or 1),
                        })
                item_path = _s(getattr(item, "path", "") or "")
                if item_path and _UNRESOLVED_TEMPLATE_TOKEN_RE.search(item_path):
                    template_token_rows += 1
                    album_template_tokens += 1
                    if len(template_token_examples) < 8:
                        template_token_examples.append({
                            "album_id": album.id,
                            "item_id": getattr(item, "id", 0),
                            "artist": _s(getattr(album, "albumartist", "") or ""),
                            "album": _s(getattr(album, "album", "") or ""),
                            "title": _s(getattr(item, "title", "") or ""),
                            "path": item_path,
                        })
            if album_release_gaps:
                albums_with_item_release_gaps += 1
            if album_track_gaps:
                albums_with_track_recording_gaps += 1
            if album_template_tokens:
                albums_with_template_tokens += 1
    except Exception as exc:
        _app_logger.warning("Library health scan failed: %s", type(exc).__name__)
        return jsonify({"ok": False, "error": "Could not scan library health."})
    return jsonify({
        "ok": True,
        "root": str(MUSIC_ROOT),
        "total_albums": total_albums,
        "missing_album_mb": missing_album_mb,
        "total_tracks": total_tracks,
        "missing_track_mb": missing_track_mb,
        "item_release_gap_rows": item_release_gap_rows,
        "albums_with_item_release_gaps": albums_with_item_release_gaps,
        "track_recording_gap_rows": track_recording_gap_rows,
        "albums_with_track_recording_gaps": albums_with_track_recording_gaps,
        "inferred_album_mbid_rows": inferred_album_mbid_rows,
        "template_token_rows": template_token_rows,
        "albums_with_template_tokens": albums_with_template_tokens,
        "examples": examples,
        "release_gap_examples": release_gap_examples,
        "track_gap_examples": track_gap_examples,
        "template_token_examples": template_token_examples,
    })


@app.post("/api/albums/reimport-disk")
def reimport_disk():
    """Tag & import audio files that are already in the music folder but not in the beets DB."""
    body, status = start_reimport_disk(request.get_json(silent=True) or {})
    return json_route_result(body, status)


@app.post("/api/library/import-all")
def library_import_all():
    """Queue a single parent job that repairs/imports selected library albums.

    Albums with a mb_albumid go directly to reimport-disk.
    Albums without a mb_albumid first run AcoustID + MusicBrainz + AI discovery;
    confident matches are imported, uncertain ones are queued for Import Review.
    """
    payload = request.get_json(silent=True) or {}
    raw_albums = payload.get("albums") or []
    if not isinstance(raw_albums, list) or not raw_albums:
        return jsonify({"ok": False, "error": "albums required"}), 400

    albums: List[Dict[str, Any]] = []
    for idx, raw in enumerate(raw_albums, start=1):
        coerced = _coerce_library_import_all_album(raw, idx)
        if coerced:
            albums.append(coerced)

    # Deduplicate by resolved aldir so the same folder isn't processed twice
    _seen_aldirs: set = set()
    _unique: List[Dict[str, Any]] = []
    for _a in albums:
        try:
            _akey = str(Path(_a["aldir"]).resolve()) if _a.get("aldir") else _a.get("aldir", "")
        except Exception:
            _akey = _a.get("aldir", "")
        if _akey not in _seen_aldirs:
            _seen_aldirs.add(_akey)
            _unique.append(_a)
    albums = _unique

    if not albums:
        return jsonify({"ok": False, "error": "albums required"}), 400

    def _do(log, cancel_event=None):
        failures: List[Dict[str, Any]] = []
        queued: List[Dict[str, Any]] = []
        repaired: List[Dict[str, Any]] = []
        need_discover = [a for a in albums if not a["mb_albumid"]]
        have_mbid     = [a for a in albums if a["mb_albumid"]]
        log.append(
            f"Import All: {len(have_mbid)} album(s) to repair, "
            f"{len(need_discover)} album(s) need MB ID discovery."
        )
        for idx, album in enumerate(albums, start=1):
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("cancelled")
            label = album.get("label") or Path(album["aldir"]).name
            mb_albumid = album["mb_albumid"]

            # ── Discovery phase for albums without a MB ID ────────────────────
            if not mb_albumid:
                log.append(f"[{idx}/{len(albums)}] Discovering: {label}")
                try:
                    result = _ai_suggest_folder_internal(album["aldir"])
                    sug    = result.get("suggestion") or {}
                    conf   = (sug.get("confidence") or "low").lower()
                    found  = (sug.get("mb_albumid") or "").strip()
                    if result.get("ok") and found and conf in ("high", "medium") \
                            and sug.get("mb_valid"):
                        mb_albumid = found
                        album["mb_albumid"] = mb_albumid
                        log.append(
                            f"  Identified as {sug.get('albumartist')} — "
                            f"{sug.get('album')} [{conf}]"
                        )
                    else:
                        reason = (sug.get("reason") or result.get("error")
                                  or "Could not identify album")
                        log.append(f"  Queuing for Import Review: {reason}")
                        _queue_folder_for_manual_review(
                            album["aldir"], sug or None, reason, log,
                            allow_existing=True,
                            evidence=result.get("evidence") or None,
                        )
                        queued.append(_library_import_all_record(album, reason))
                        continue
                except Exception as exc:
                    reason = f"Discovery error: {exc}"
                    log.append(f"  {reason} — queuing for Import Review")
                    try:
                        _queue_folder_for_manual_review(
                            album["aldir"], None, reason, log,
                            allow_existing=True,
                        )
                    except Exception:
                        pass
                    queued.append(_library_import_all_record(album, reason))
                    continue

            # ── Repair / import phase ─────────────────────────────────────────
            log.append(f"[{idx}/{len(albums)}] Repairing: {label}")
            child_job_id = ""
            try:
                child_job_id = _start_reimport_disk_job_internal(
                    album["aldir"],
                    mb_albumid,
                    albumartist=album.get("albumartist") or "",
                    existing_album_id=int(album.get("existing_album_id") or 0),
                    wanted_tracks=album.get("wanted_tracks") or [],
                    strict_edition_guard=True,
                )
                child_result = _wait_for_child_job(
                    child_job_id, log, cancel_event=cancel_event,
                    prefix=f"album {idx}", timeout=1200,
                )
                result_status = (child_result or {}).get("status", "")
                if result_status == "no_useful_missing_tracks":
                    record = _library_import_all_record(
                        album,
                        "No source files found for missing tracks",
                        child_job_id,
                    )
                    queued.append(record)
                    log.append(
                        f"[{idx}/{len(albums)}] No progress: {label}: "
                        "missing tracks remain, no source files found — queued for Review"
                    )
                else:
                    album["mb_albumid"] = mb_albumid
                    repaired.append(_library_import_all_record(album, "repaired", child_job_id))
                    log.append(f"[{idx}/{len(albums)}] Done: {label}")
            except Exception as ex:
                detail = str(ex)
                record = _library_import_all_record(album, detail, child_job_id)
                if "queued for Review without changing library files" in detail:
                    queued.append(record)
                    log.append(f"[{idx}/{len(albums)}] Queued for Review: {label}: {ex}")
                elif "cancelled" in detail.lower():
                    failures.append(record)
                    log.append(f"[{idx}/{len(albums)}] Failed: {label}: {ex}")
                else:
                    failures.append(record)
                    log.append(f"[{idx}/{len(albums)}] Failed: {label}: {ex}")
                    try:
                        _queue_folder_for_manual_review(
                            album["aldir"],
                            {"mb_albumid": mb_albumid,
                             "album": album.get("label") or "",
                             "albumartist": album.get("albumartist") or "",
                             "confidence": "low"},
                            f"Import All failed: {detail[:300]}",
                            log,
                            allow_existing=True,
                        )
                    except Exception:
                        pass

        _invalidate_lib_cache()
        summary = (
            f"Import All complete: "
            f"{len(repaired)}/{len(albums)} repaired"
            + (f", {len(queued)} queued for review" if queued else "")
            + (f", {len(failures)} failed" if failures else "")
            + "."
        )
        log.append(summary)
        if queued:
            log.append("Queued for Review:")
            for rec in queued[:10]:
                log.append(f"  - {rec.get('label')}: {rec.get('message')}")
        if failures:
            log.append("Failed albums eligible for Retry Failed:")
            for rec in failures[:10]:
                log.append(f"  - {rec.get('label')}: {rec.get('message')}")
        _library_import_all_write_last({
            "ok": True,
            "updated_at": int(time.time()),
            "album_count": len(albums),
            "repaired_count": len(repaired),
            "queued_count": len(queued),
            "failed_count": len(failures),
            "repaired": repaired,
            "queued": queued,
            "failures": failures,
            "failed_albums": [rec.get("album") for rec in failures if rec.get("album")],
        })
        if failures:
            raise RuntimeError(summary)
        return {
            "album_count": len(albums),
            "repaired": len(repaired),
            "queued": len(queued),
            "failed": len(failures),
        }

    job = jobs.start_python(
        _do,
        label=f"Import All: {len(albums)} album(s)",
        metadata={"type": "library-import-all", "album_count": len(albums)},
    )
    return jsonify({"ok": True, "job_id": job.job_id, "album_count": len(albums)})


@app.get("/api/library/import-all/last")
def library_import_all_last():
    data = _library_import_all_read_last()
    data.setdefault("ok", True)
    data.setdefault("failed_count", 0)
    data.setdefault("failures", [])
    data.setdefault("failed_albums", [])
    return jsonify(data)


@app.post("/api/library/import-all/retry-failed")
def library_import_all_retry_failed():
    data = _library_import_all_read_last()
    failed_albums = data.get("failed_albums") or []
    if not isinstance(failed_albums, list) or not failed_albums:
        return jsonify({"ok": False, "error": "No failed Import All albums to retry"}), 400
    albums = []
    for idx, raw in enumerate(failed_albums, start=1):
        coerced = _coerce_library_import_all_album(raw, idx)
        if coerced:
            albums.append(coerced)
    if not albums:
        return jsonify({"ok": False, "error": "No retryable Import All album payloads found"}), 400
    with app.test_request_context(
        "/api/library/import-all",
        method="POST",
        json={"albums": albums, "retry_failed": True},
    ):
        return library_import_all()


@app.post("/api/library/scan")
def library_scan():
    if not _legacy_local_scan_enabled():
        return jsonify({
            "ok": False,
            "error": "Legacy local library scan is disabled for the external Beets engine architecture.",
        }), 409
    jid = _do_scan_job()
    return jsonify({"ok": True, "job_id": jid, "last_scan": _get_last_scan()})


@app.get("/api/library/scan/status")
def library_scan_status():
    summary = {}
    last_job_id = _SCAN_STATE.get("last_job_id")
    if last_job_id:
        j = jobs.get(last_job_id)
        if j:
            for line in (j.log or []):
                if ":" in line:
                    k, _, v = line.partition(":")
                    k = k.strip()
                    if k in ("tracks", "albums", "missing", "unimported", "removed", "status"):
                        summary[k] = v.strip()
    return jsonify({"ok": True, "last_scan": _get_last_scan(),
                    "last_job_id": last_job_id, "summary": summary})


@app.post("/api/library/merge-artist")
def library_merge_artist():
    """Rename an artist (albumartist) across all albums + items, then move files.
    If to_artist matches an existing artist, this effectively merges the two."""
    payload = request.get_json(silent=True) or {}
    from_artist = (payload.get("from_artist") or "").strip()
    to_artist   = (payload.get("to_artist")   or "").strip()
    if not from_artist or not to_artist:
        return jsonify({"ok": False, "error": "from_artist and to_artist required"})
    if from_artist == to_artist:
        return jsonify({"ok": False, "error": "from_artist and to_artist are the same"})

    def _do(log, cancel_event=None):
        # ARCH-007 (Wave 34): structured engine read, not raw _db() SQL --
        # see backend.composite_workflows.find_all_albums_by_albumartist()'s own
        # docstring for why this is an exact-match query, not a substring
        # LIKE match.
        try:
            album_rows = composite_workflows.find_all_albums_by_albumartist(from_artist)
        except (BeetsUnavailableError, BeetsError) as ex:
            log.append(f"ERROR: Engine unavailable looking up artist '{from_artist}': {ex}")
            return
        if not album_rows:
            log.append(f"No albums found for artist '{from_artist}'")
            return
        album_ids = [int(r["id"]) for r in album_rows]
        log.append(f"Renaming {len(album_ids)} album(s): {from_artist!r} → {to_artist!r}")

        # Same album_metadata_repair_v1 migration already applied to
        # library_normalize_artists()/_run_normalize_artists_if_needed()
        # (ARCH-003 Wave 30/32): updates={"albumartist": to_artist}
        # propagates to every item row of each album too, and
        # force_write_tags=True performs the real on-disk tag write --
        # no local subprocess execution needed for either the DB rename
        # or the write step.
        renamed_ids: List[int] = []
        for aid in album_ids:
            try:
                res = composite_workflows.update_album_metadata(aid, {"albumartist": to_artist}, force_write_tags=True)
            except (BeetsUnavailableError, BeetsError) as ex:
                log.append(f"  Engine unavailable renaming album_id {aid}: {ex}")
                continue
            if not res.get("ok"):
                log.append(f"  Engine rejected rename for album_id {aid}: {res.get('error') or 'unknown error'}")
                continue
            renamed_ids.append(aid)
        log.append(f"DB updated: {len(renamed_ids)}/{len(album_ids)} album(s).")

        for i, aid in enumerate(renamed_ids, 1):
            log.append(f"[{i}/{len(renamed_ids)}] Moving files for album_id={aid}…")
            try:
                rel_res = composite_workflows.relocate_album(aid, mode="rename")
                if rel_res.get("ok"):
                    log.append(f"  ✓ Relocated album {aid} to: {rel_res.get('dest_dir')}")
                else:
                    log.append(f"  relocate warning: {rel_res.get('error')}")
            except Exception as _ex:
                log.append(f"  relocate warning: {_ex}")
        _invalidate_lib_cache()
        log.append(f"Done — {len(renamed_ids)} album(s) now under '{to_artist}'.")

    job = jobs.start_python(_do, label=f"Merge artist: {from_artist!r} → {to_artist!r}")
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/library/sync-deleted")
def library_sync_deleted():
    """Remove beets DB entries for albums/tracks whose files no longer exist on disk.
    Does NOT delete any files — only cleans the DB to match what is actually on disk."""
    payload = request.get_json(silent=True) or {}
    dry_run = payload.get("dry_run", True) is not False
    if not dry_run and payload.get("confirmed") is not True:
        return jsonify({"ok": False, "error": "Confirmation is required before syncing deleted files"}), 400
    raw_ids = payload.get("item_ids") or []
    if not isinstance(raw_ids, list):
        return jsonify({"ok": False, "error": "item_ids must be a list"}), 400
    planned_ids = [int(i) for i in raw_ids if str(i).isdigit() and int(i) > 0]
    if not dry_run and not planned_ids:
        # LT-2: apply removes only the rows a preview listed (missing_item_ids),
        # each re-verified as still missing.
        return jsonify({"ok": False, "code": "planned_ids_required",
                        "error": "Apply needs item_ids from a preview (missing_item_ids); nothing was removed."}), 400

    def _do(log, cancel_event=None, update_state=None):
        mode = "Previewing" if dry_run else "Applying"
        log.append(f"{mode} missing-file DB sync...")
        if cancel_event and cancel_event.is_set():
            log.append("[cancelled]")
            return

        try:
            sync_res = composite_workflows.sync_deleted_files(dry_run=dry_run, limit=50000,
                                                              item_ids=planned_ids or None)
        except (BeetsUnavailableError, BeetsError) as ex:
            log.append(f"ERROR: Sync deleted failed: {ex}")
            raise RuntimeError(f"Sync deleted failed: {ex}") from ex
        if sync_res.get("ok") is False:
            log.append(f"ERROR: {sync_res.get('error')}")
            raise RuntimeError(sync_res.get("error") or "Sync deleted was refused; nothing was removed.")
        if dry_run and sync_res.get("missing_item_ids"):
            log.append("missing_item_ids: " + ",".join(str(i) for i in sync_res["missing_item_ids"][:5000]))

        scanned = int(sync_res.get("scanned_items", 0))
        missing_count = int(sync_res.get("missing_count", 0))
        removed_items = int(sync_res.get("removed_from_db", 0))
        removed_albums = int(sync_res.get("missing_albums_count", 0))

        if missing_count == 0:
            log.append("Nothing to clean up - all files are present on disk.")
            if update_state:
                update_state({
                    "current_task": "Missing-file DB sync complete",
                    "affected_count": 0,
                    "safe_count": 0,
                    "changed_count": 0,
                    "final_summary": {
                        "DB rows scanned": scanned,
                        "Missing item rows": 0,
                        "Albums with all files missing": 0,
                        "Mode": "Preview only" if dry_run else "Applied",
                    },
                })
            return

        if dry_run:
            log.append(
                f"Preview only - would remove {removed_albums} album row(s) and "
                f"{missing_count} item row(s)."
            )
            if update_state:
                update_state({
                    "current_task": "Missing-file DB sync preview complete",
                    "affected_count": missing_count,
                    "safe_count": missing_count,
                    "changed_count": 0,
                    "final_summary": {
                        "DB rows scanned": scanned,
                        "Missing item rows": missing_count,
                        "Albums with all files missing": removed_albums,
                        "Mode": "Preview only",
                    },
                })
            return {"ok": True, "dry_run": True, "missing_count": missing_count,
                    "missing_item_ids": sync_res.get("missing_item_ids") or []}

        _invalidate_lib_cache()
        log.append(f"Done - removed {removed_albums} album(s), "
                   f"{removed_items} track(s) from DB (files already gone from disk)")
        if update_state:
            update_state({
                "current_task": "Missing-file DB sync applied",
                "affected_count": removed_items,
                "changed_count": removed_albums + removed_items,
                "final_summary": {
                    "DB rows scanned": scanned,
                    "Missing item rows removed": removed_items,
                    "Album rows removed": removed_albums,
                    "Mode": "Applied after confirmation",
                },
            })

    job = jobs.start_python(
        _do,
        label="Preview Missing File DB Sync" if dry_run else "Apply Missing File DB Sync",
        metadata={"type": "sync-deleted", "dry_run": dry_run},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/library/unmatched-draft")
def create_unmatched_draft():
    """Create a local review draft for releases not found in MusicBrainz/Discogs.

    Writes a musicbrainz_submission.txt and draft.json into a clean
    Artist/Album (Year) [unmatched-<id>]/ folder under UNMATCHED_DRAFT_ROOT.

    This is web-manager orchestration/review state, not authoritative media:
    no audio file is placed or referenced here, and nothing downstream reads
    these files back to drive an import. Per SEC-002 Wave 8's architecture
    review, it must not be written under MUSIC_ROOT -- the web manager does
    not own that root and, in the shipped Compose topology, does not even
    have it mounted. If a draft is later actually imported into the
    library, that import goes through the normal reimport_disk()/Beets
    engine path, which owns MUSIC_ROOT and validates its own identity
    evidence at that time; this route only ever prepares review metadata.
    """
    payload = request.get_json(silent=True) or {}
    artist = _s(payload.get("artist") or "").strip()
    album = _s(payload.get("album") or "").strip()
    year = _s(payload.get("year") or "").strip()
    source_url = _redact_security_text(payload.get("source_url") or "").strip()
    tracks = _redact_unmatched_draft_tracks(payload.get("tracks") or [])

    if not artist:
        return jsonify({"ok": False, "error": "artist is required"}), 400
    if not album:
        return jsonify({"ok": False, "error": "album is required — do not use a year-only folder"}), 400
    if re.match(r'^\d{4}$', album.strip()):
        return jsonify({"ok": False, "error": "album must be a title, not a year"}), 400

    slug_artist = re.sub(r"[\s_]+", "-", re.sub(r"[^\w\s-]", "", artist.lower())).strip("-")
    slug_album = re.sub(r"[\s_]+", "-", re.sub(r"[^\w\s-]", "", album.lower())).strip("-")
    local_id = f"{slug_artist}-{slug_album}"[:60].strip("-") or "unmatched"

    # artist/album/year are client-supplied free text with no validation
    # above other than non-empty; sanitize each into a single safe path
    # component BEFORE building the folder name, so an embedded "/" or a
    # pure ".."/"." value can never smuggle a path separator or traversal
    # segment into draft_path (Path's / operator re-parses embedded
    # separators as additional components -- concatenating first and
    # sanitizing the combined string after the fact would not catch that).
    safe_artist = _safe_path_component(artist, "Unknown Artist")
    safe_album = _safe_path_component(album, "Unknown Album")
    safe_year = _safe_path_component(year, "") if year else ""

    album_folder = f"{safe_album} ({safe_year}) [unmatched-{local_id}]" if safe_year else f"{safe_album} [unmatched-{local_id}]"
    draft_path = UNMATCHED_DRAFT_ROOT / safe_artist / album_folder

    try:
        draft_root_resolved = UNMATCHED_DRAFT_ROOT.resolve(strict=False)
        draft_path_resolved = draft_path.resolve(strict=False)
    except Exception:
        return jsonify({"ok": False, "error": "Could not resolve draft folder path."}), 400
    if not _path_is_under(draft_path_resolved, draft_root_resolved):
        # Defense-in-depth: sanitization above should already make this
        # unreachable, but the destructive mkdir/write below must never
        # run against an unverified path regardless.
        return jsonify({"ok": False, "error": "Draft folder is outside the allowed draft root."}), 400
    draft_path = draft_path_resolved

    # Collision policy: two different raw (artist, album) pairs can sanitize
    # to the same folder (e.g. "A/B" and "A\B" both become "A_B"). Silently
    # overwriting a previous, unrelated draft would lose it without warning.
    # Re-submitting the SAME (artist, album) is treated as an idempotent
    # update; a different pair mapping to the same sanitized folder is a
    # genuine collision and must not silently overwrite the existing draft.
    existing_draft_json = draft_path / "draft.json"
    if existing_draft_json.exists():
        try:
            existing_data = json.loads(existing_draft_json.read_text(encoding="utf-8"))
        except Exception:
            existing_data = {}
        if existing_data.get("artist") != artist or existing_data.get("album") != album:
            return jsonify({
                "ok": False,
                "error": "A different draft already maps to this folder name.",
                "local_id": local_id,
            }), 409

    try:
        draft_path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        _app_logger.warning("Could not create draft folder: %s", type(exc).__name__)
        return jsonify({"ok": False, "error": "Could not create folder."}), 500

    tracklist_lines = "\n".join(
        f"  {i+1}. {_s(t.get('title') or '')}"
        + (f" ({_s(t.get('duration') or '')})" if t.get("duration") else "")
        for i, t in enumerate(tracks)
    )
    submission_text = (
        f"Artist: {artist}\n"
        f"Album: {album}\n"
        f"Year: {year}\n"
        f"Source URL: {source_url}\n"
        f"\nTracklist:\n{tracklist_lines}\n"
        f"\nLocal draft path: {draft_path}\n"
        f"Status: unmatched — not found in MusicBrainz/Discogs\n"
        f"Notes: Local release. Submit at https://musicbrainz.org/release/add\n"
    )
    try:
        (draft_path / "musicbrainz_submission.txt").write_text(submission_text, encoding="utf-8")
        (draft_path / "draft.json").write_text(
            json.dumps({
                "artist": artist, "album": album, "year": year,
                "source_url": source_url, "local_id": local_id,
                "status": "unmatched_ready_for_review",
                "tracks": tracks, "created_at": time.time(),
            }, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        _app_logger.warning("Could not write draft files: %s", type(exc).__name__)
        return jsonify({"ok": False, "error": "Could not write draft files."}), 500

    return jsonify({
        "ok": True,
        "local_id": local_id,
        "path": str(draft_path),
        "album_folder": album_folder,
        "submission_file": str(draft_path / "musicbrainz_submission.txt"),
    })


@app.post("/api/library/move-all")
def library_move_all():
    """Rename/move all library files to match the current path template (beet move)."""
    def _do(log, cancel_event=None):
        candidate_dirs: set = set()
        try:
            path_values = composite_workflows.list_distinct_item_paths()
            for p in path_values:
                if not p:
                    continue
                abs_p = p if p.startswith("/") else f"{MUSIC_ROOT}/{p}"
                parent = Path(abs_p).parent
                root = Path(str(MUSIC_ROOT))
                while parent != root and root in parent.parents:
                    candidate_dirs.add(str(parent))
                    parent = parent.parent
            log.append(
                f"Pre-move scan: {len(path_values)} distinct item path(s) read via engine IPC, "
                f"{len(candidate_dirs)} candidate empty-folder director{'y' if len(candidate_dirs) == 1 else 'ies'}."
            )
        except Exception as ex:
            log.append(f"  [warn] Could not enumerate pre-move directories for empty-folder cleanup: {ex}")

        # Steps 1 & 2: rescan disk (beet update) and move files (beet move) via engine IPC under OS lock
        log.append("Rescanning and moving library files via engine IPC…")
        deadline = time.time() + 5400.0  # 1.5-hour hard cap
        try:
            move_res = composite_workflows.move_library(query="", rescan_first=True, async_job=True, timeout=5400.0)
        except Exception as ex:
            raise RuntimeError(f"Failed to execute move_library on engine: {ex}") from ex
        if isinstance(move_res, dict) and move_res.get("ok") is False:
            # LT-18: refused (not_supported) -- fail the job, change nothing.
            raise RuntimeError(move_res.get("error") or "Moving the whole library was refused; nothing was moved.")

        remote_job_id = move_res.get("job_id") if isinstance(move_res, dict) else None
        rc = 0
        if remote_job_id:
            seen_stdout = 0
            seen_stderr = 0
            while True:
                if cancel_event and cancel_event.is_set():
                    try:
                        composite_workflows.cancel_job(remote_job_id)
                    except Exception:
                        pass
                    log.append("[cancelled]")
                    return

                if time.time() > deadline:
                    try:
                        composite_workflows.cancel_job(remote_job_id)
                    except Exception:
                        pass
                    log.append("⚠ move_library timed out.")
                    return

                try:
                    job_info = composite_workflows.get_job(remote_job_id)
                except Exception:
                    time.sleep(0.5)
                    continue

                r_stdout = job_info.get("stdout") or []
                if len(r_stdout) > seen_stdout:
                    for line in r_stdout[seen_stdout:]:
                        stripped = line.rstrip()
                        if stripped:
                            log.append(stripped)
                    seen_stdout = len(r_stdout)

                r_stderr = job_info.get("stderr") or []
                if len(r_stderr) > seen_stderr:
                    for line in r_stderr[seen_stderr:]:
                        stripped = line.rstrip()
                        if stripped:
                            log.append(f"  ⚠ {stripped}")
                    seen_stderr = len(r_stderr)

                status = job_info.get("status")
                if status in ("success", "failed", "cancelled", "timeout"):
                    rc = job_info.get("returncode", 0 if status == "success" else 1)
                    if status == "cancelled":
                        log.append("[cancelled]")
                        return
                    if status == "timeout":
                        log.append("⚠ move_library timed out.")
                        return
                    break

                time.sleep(0.5)
        elif isinstance(move_res, dict):
            # Sync response / unit test mock fallback
            stdout = move_res.get("stdout") or ""
            stderr = move_res.get("stderr") or ""
            for line in stdout.splitlines():
                if line.strip():
                    log.append(line.rstrip())
            for line in stderr.splitlines():
                if line.strip():
                    log.append(f"  ⚠ {line.rstrip()}")
            rc = move_res.get("returncode", 0 if move_res.get("ok", True) else 1)

        if (isinstance(move_res, dict) and not move_res.get("ok", True)) or rc not in (0, 1):
            err = move_res.get("error") if isinstance(move_res, dict) else None
            raise RuntimeError(err or f"beet move exited with rc={rc}")

        if cancel_event and cancel_event.is_set():
            log.append("[cancelled]"); return

        # Step 3: ask the engine to remove empty candidate directories
        removed_dirs = 0
        for cdir in sorted(candidate_dirs, key=len, reverse=True):
            if cancel_event and cancel_event.is_set():
                log.append("[cancelled]"); return
            try:
                plan_res = composite_workflows.plan_folder_cleanup({"source": cdir, "action": "remove_empty"})
            except Exception as ex:
                log.append(f"  [warn] Folder cleanup plan failed for {cdir}: {ex}")
                continue
            if not plan_res.get("ok"):
                code = plan_res.get("code") or ""
                if code not in ("folder_cleanup_not_empty", "folder_cleanup_db_references"):
                    log.append(f"  [warn] Folder cleanup plan rejected for {cdir}: {plan_res.get('error')}")
                continue
            op_id = plan_res.get("operation_id")
            if not op_id:
                continue
            try:
                apply_res = composite_workflows.apply_folder_cleanup(op_id)
            except Exception as ex:
                log.append(f"  [warn] Folder cleanup apply failed for {cdir}: {ex}")
                continue
            if apply_res.get("ok"):
                removed_dirs += 1
                log.append(f"  Removed empty folder: {cdir}")
            else:
                log.append(f"  [warn] Folder cleanup apply rejected for {cdir}: {apply_res.get('error')}")
        if removed_dirs:
            log.append(f"Cleaned up {removed_dirs} empty folder(s).")

        _invalidate_lib_cache()
        _trigger_plex_refresh(log)
        log.append("✓ Done." if rc == 0 else "⚠ Finished with some errors (see above).")

    job = jobs.start_python(_do, label="Move all library files", metadata={"type": "move-all", "dedupe_key": "library:move-all"})
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/library/mbsync-all")
def library_mbsync_all():
    """Sync all library tracks against MusicBrainz metadata (beet mbsync)."""
    def _do(log, cancel_event=None):
        # LT-18: ask the engine first; when the library-wide sync is refused
        # (not_supported today) nothing else -- not even the orphan-row prune --
        # runs, and the job fails instead of reporting success.
        try:
            mbsync_res = composite_workflows.mbsync(query="", async_job=True)
        except Exception as ex:
            raise RuntimeError(f"Failed to start beet mbsync on engine: {ex}") from ex
        if isinstance(mbsync_res, dict) and mbsync_res.get("ok") is False:
            raise RuntimeError(mbsync_res.get("error") or "Library-wide mbsync was refused; nothing was changed.")
        try:
            orphan_rows = composite_workflows.find_all_orphan_albums()
            orphan_ids = [int(r["id"]) for r in orphan_rows]
        except Exception as ex:
            log.append(f"  [warn] Orphan lookup failed (non-fatal): {ex}")
            orphan_ids = []

        pruned = 0
        for oid in orphan_ids:
            if cancel_event and cancel_event.is_set():
                log.append("[cancelled]"); return
            try:
                # Empty orphan rows only; delete_album never deletes files.
                res = composite_workflows.delete_album(oid, delete_files=False)
                if res.get("ok"):
                    pruned += 1
                else:
                    log.append(f"  [warn] Could not prune orphaned album {oid}: {res.get('error')}")
            except Exception as ex:
                log.append(f"  [warn] Could not prune orphaned album {oid}: {ex}")
        if orphan_ids:
            log.append(f"Pruned {pruned}/{len(orphan_ids)} orphaned album record(s) with no tracks.")

        log.append("Running beet mbsync on full library via engine IPC — this may take several minutes…")
        deadline = time.time() + 7200.0  # 2-hour hard cap

        remote_job_id = mbsync_res.get("job_id") if isinstance(mbsync_res, dict) else None
        rc = 0
        if remote_job_id:
            seen_stdout = 0
            seen_stderr = 0
            while True:
                if cancel_event and cancel_event.is_set():
                    try:
                        composite_workflows.cancel_job(remote_job_id)
                    except Exception:
                        pass
                    log.append("[cancelled]")
                    return

                if time.time() > deadline:
                    try:
                        composite_workflows.cancel_job(remote_job_id)
                    except Exception:
                        pass
                    log.append("⚠ mbsync timed out after 2 hours.")
                    return

                try:
                    job_info = composite_workflows.get_job(remote_job_id)
                except Exception:
                    time.sleep(0.5)
                    continue

                r_stdout = job_info.get("stdout") or []
                if len(r_stdout) > seen_stdout:
                    for line in r_stdout[seen_stdout:]:
                        stripped = line.rstrip()
                        if stripped:
                            log.append(stripped)
                    seen_stdout = len(r_stdout)

                r_stderr = job_info.get("stderr") or []
                if len(r_stderr) > seen_stderr:
                    for line in r_stderr[seen_stderr:]:
                        stripped = line.rstrip()
                        if stripped:
                            log.append(f"  ⚠ {stripped}")
                    seen_stderr = len(r_stderr)

                status = job_info.get("status")
                if status in ("success", "failed", "cancelled", "timeout"):
                    rc = job_info.get("returncode", 0 if status == "success" else 1)
                    if status == "cancelled":
                        log.append("[cancelled]")
                        return
                    if status == "timeout":
                        log.append("⚠ mbsync timed out after 2 hours.")
                        return
                    break

                time.sleep(0.5)
        elif isinstance(mbsync_res, dict):
            # Sync response / unit test mock fallback
            stdout = mbsync_res.get("stdout") or ""
            stderr = mbsync_res.get("stderr") or ""
            for line in stdout.splitlines():
                if line.strip():
                    log.append(line.rstrip())
            for line in stderr.splitlines():
                if line.strip():
                    log.append(f"  ⚠ {line.rstrip()}")
            rc = mbsync_res.get("returncode", 0 if mbsync_res.get("ok", True) else 1)

        if rc not in (0, 1):
            raise RuntimeError(f"beet mbsync exited with rc={rc}")
        _invalidate_lib_cache()
        log.append("✓ beet mbsync complete." if rc == 0 else "⚠ mbsync finished with some errors (see above).")

    job = jobs.start_python(_do, label="MBSync all library tracks", metadata={"type": "mbsync-all", "dedupe_key": "library:mbsync-all"})
    return jsonify({"ok": True, "job_id": job.job_id})


@app.get("/api/library/genre-stats")
def library_genre_stats():
    """Genre coverage: how many albums have / are missing a genre tag."""
    albums = list(lib.albums([]))
    with_genre    = [a for a in albums if _album_genre_value(a)]
    without_genre = [a for a in albums if not _album_genre_value(a)]
    return jsonify({
        "ok":            True,
        "total":         len(albums),
        "with_genre":    len(with_genre),
        "without_genre": len(without_genre),
        "missing": [
            {"id": a.id, "album": a.album or "", "albumartist": a.albumartist or "",
             "year": a.year or ""}
            for a in sorted(without_genre, key=lambda a: (a.albumartist or "").lower())[:200]
        ],
    })


@app.post("/api/library/fix-genres")
def library_fix_genres():
    """Background job: run beet lastgenre for albums missing genre, then optionally"""
    body, status = start_library_fix_genres(request.get_json(silent=True) or {})
    return json_route_result(body, status)


@app.post("/api/albums/<int:aid>/fix-genre")
def album_fix_genre(aid):
    """Fix genre for a single album: run lastgenre, fall back to AI if nothing found."""
    album = lib.get_album(aid)
    if not album:
        return jsonify({"ok": False, "error": "Album not found"})

    def _do(log, cancel_event=None):
        log.append(f"[1/2] Running lastgenre for {album.albumartist or '?'} — {album.album or '?'}…")
        r = _lastgenre_cmd(False, f"album_id:{aid}", log, cancel_event=cancel_event)
        _require_beet_ok(r, "lastgenre", log)

        _invalidate_lib_cache()
        updated = lib.get_album(aid)
        if updated and _album_genre_value(updated):
            log.append(f"  ✓ Genre: {_album_genre_value(updated)}")
            return

        api_key = _ai_api_key()
        if not api_key:
            log.append("  Last.fm returned no genre and OPENAI_API_KEY is not set")
            return

        log.append("[2/2] Last.fm had no tag — asking AI…")
        a = updated or album
        genre = _ai_suggest_genre(a.albumartist or "", a.album or "", a.year, api_key, log)
        if not genre:
            log.append("  AI could not determine genre")
            return
        _apply_genre_to_album(aid, genre, log, cancel_event=cancel_event)
        _invalidate_lib_cache()

    label = f"Fix genre: {album.albumartist or '?'} — {album.album or '?'}"
    job = jobs.start_python(_do, label=label)
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/library/normalize-artists")
def library_normalize_artists():
    """Normalize Unicode punctuation (fancy hyphens, smart quotes, etc.) in all
    albumartist and artist fields in the DB, then move files for affected albums."""
    def _do(log, cancel_event=None):
        # ARCH-007 (Wave 34): structured engine read, not raw _db() SQL.
        try:
            aa_values = composite_workflows.list_distinct_albumartists()
        except (BeetsUnavailableError, BeetsError) as ex:
            log.append(f"ERROR: Engine unavailable listing artist names: {ex}")
            return
        to_fix = []
        for aa in aa_values:
            clean = _normalize_albumartist(aa)
            if clean != aa:
                to_fix.append((aa, clean))
                log.append(f"  Renamed: {aa!r} → {clean!r}")

        if not to_fix:
            log.append("No artist names needed normalization.")
            return

        # Selection (which album rows currently hold the un-normalized
        # value) stays a non-mutating structured read; the rename itself is
        # one album_metadata_repair_v1 call per affected album --
        # updates={"albumartist": new_aa} already propagates to every
        # item row of that album too (create_album_metadata_plan merges
        # album-level identity fields into each item's diff when the
        # item doesn't already set its own), so no separate
        # UPDATE items SET albumartist=... step is needed. Same
        # migration already applied to the sibling auto-triggered
        # function _run_normalize_artists_if_needed() (ARCH-003 Wave 30/34).
        affected_ids: List[int] = []
        for old_aa, new_aa in to_fix:
            try:
                rows = composite_workflows.find_all_albums_by_albumartist(old_aa)
            except (BeetsUnavailableError, BeetsError) as ex:
                log.append(f"  Engine unavailable looking up albums for {old_aa!r}: {ex}")
                continue
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
        log.append(f"DB updated: {len(to_fix)} artist name(s) normalized across {len(affected_ids)} album(s).")

        for i, aid in enumerate(affected_ids, 1):
            log.append(f"[{i}/{len(affected_ids)}] Moving album_id={aid}…")
            try:
                rel_res = composite_workflows.relocate_album(aid, mode="rename")
                if rel_res.get("ok"):
                    log.append(f"  ✓ Relocated album {aid} to: {rel_res.get('dest_dir')}")
                else:
                    log.append(f"  relocate warning: {rel_res.get('error')}")
            except Exception as _ex:
                log.append(f"  relocate warning: {_ex}")

        _invalidate_lib_cache()
        log.append("Done.")

    job = jobs.start_python(_do, label="Normalize artist names (Unicode→ASCII)")
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/library/merge-artist-id")
def library_merge_artist_id():
    payload = request.get_json(silent=True) or {}
    mb_artistid = (payload.get("mb_artistid") or "").strip().lower()
    canonical = (payload.get("canonical") or "").strip()
    if not _MB_UUID_RE.match(mb_artistid):
        return jsonify({"ok": False, "error": "Valid MusicBrainz artist ID required"}), 400
    if not canonical:
        return jsonify({"ok": False, "error": "Canonical artist name required"}), 400

    group = next((g for g in _artist_id_alias_groups() if g["mb_artistid"] == mb_artistid), None)
    if not group:
        return jsonify({"ok": False, "error": "No alias group found for that MB artist ID"}), 404
    source_names = [n["name"] for n in group["names"] if n["name"] != canonical]
    if not source_names:
        return jsonify({"ok": False, "error": "Nothing to merge"}), 400

    def _do(log, cancel_event=None):
        log.append(f"MusicBrainz artist ID: {mb_artistid}")
        log.append(f"Canonical artist: {canonical}")
        log.append("Merging aliases: " + ", ".join(source_names))
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("cancelled")
        log.append(f"Executing artist reconciliation via artist_folder_reconcile_v1 ...")
        _run_artist_folder_reconcile_for_alias_merge(source_names, canonical, mb_artistid, log)

        _cleanup_artist_alias_source_dirs(source_names, canonical, log)
        _invalidate_lib_cache()
        log.append(f"Done. Updated artist aliases to {canonical!r}.")

    job = jobs.start_python(_do, label=f"Merge artist aliases: {canonical}")
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/library/confirm-artist-alias")
def library_confirm_artist_alias():
    payload = request.get_json(silent=True) or {}
    source = (payload.get("source_artist") or "").strip()
    canonical = (payload.get("canonical_artist") or "").strip()
    mb_artistid = (payload.get("mb_artistid") or "").strip().lower()
    if not source or not canonical:
        return jsonify({"ok": False, "error": "source_artist and canonical_artist required"}), 400
    resolved_mbid = _resolve_artist_alias_mbid(source, canonical, mb_artistid)
    if not resolved_mbid:
        return jsonify({
            "ok": False,
            "error": (
                "Could not resolve a MusicBrainz artist ID. Paste the artist UUID "
                "or make sure the canonical artist already has one in the library."
            )
        }), 400

    def _do(log, cancel_event=None):
        mbid = _resolve_artist_alias_mbid(source, canonical, resolved_mbid, log)
        if not mbid:
            raise RuntimeError("Could not resolve a MusicBrainz artist ID")
        log.append(f"Confirmed alias: {source!r} -> {canonical!r}")
        log.append(f"MusicBrainz artist ID: {mbid}")
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("cancelled")
        log.append(f"Executing artist reconciliation via artist_folder_reconcile_v1 ...")
        _run_artist_folder_reconcile_for_alias_merge([source], canonical, mbid, log)

        _cleanup_artist_alias_source_dirs([source], canonical, log)
        _invalidate_lib_cache()
        log.append(f"Done. Updated artist alias {source!r} -> {canonical!r}.")

    job = jobs.start_python(_do, label=f"Confirm artist alias: {source} -> {canonical}")
    return jsonify({"ok": True, "job_id": job.job_id})


@app.get("/api/browse")
def browse():
    """List subdirectories of a path for the path picker. Restricted to the
    library/downloads roots — this is an authenticated endpoint, but callers
    should never be able to enumerate arbitrary container filesystem paths."""
    raw = request.args.get("path", str(DOWNLOADS_ROOT))
    p = Path(raw)
    if not any(_path_is_under(p, root) or p.resolve(strict=False) == root.resolve(strict=False)
               for root in _BROWSE_ALLOWED_ROOTS):
        return jsonify({"ok": False, "error": "path is outside the allowed browse roots"}), 400
    try:
        entries = sorted(
            [d.name for d in p.iterdir() if d.is_dir()],
            key=str.lower
        )
        return jsonify({"ok": True, "path": str(p), "dirs": entries})
    except Exception:
        return jsonify({"ok": False, "error": "could not list directory"})


@app.get("/api/albums/<int:aid>/duplicate-resolver")
def album_duplicate_resolver(aid):
    mbid = (request.args.get("mb_albumid") or "").strip()
    try:
        return jsonify(_album_duplicate_resolver_plan(aid, mbid))
    except Exception as ex:
        _app_logger.warning("album_duplicate_resolver failed for album %s: %s", aid, type(ex).__name__)
        return jsonify({"ok": False, "error": "Could not build duplicate-resolver plan."}), 400


@app.post("/api/albums/<int:aid>/duplicate-resolver/apply")
def apply_album_duplicate_resolver(aid):
    payload = request.get_json(silent=True) or {}
    mbid = _s(payload.get("mb_albumid") or "").strip()
    actions = payload.get("actions") or []
    dry_run = bool(payload.get("dry_run", False))
    # Resolver deletes are track quarantines (files kept, restorable); never
    # a permanent delete.
    delete_files = False
    write_tags = payload.get("write_tags", True) is not False
    if not isinstance(actions, list) or not actions:
        return jsonify({"ok": False, "error": "actions required"}), 400
    has_delete = any(isinstance(a, dict) and _s(a.get("action")).strip().lower() == "delete" for a in actions)
    if has_delete and not dry_run and payload.get("confirm") is not True:
        return jsonify({"ok": False, "code": "confirmation_required",
                        "error": "Delete actions need explicit confirmation (confirm: true) after a dry run."}), 400
    if mbid:
        # ARCH-009: an override release is stamped onto the album only if it
        # belongs to the album's Release Group (verified, fail closed).
        album = lib.get_album(aid)
        identity = _verify_album_identity(
            _s(getattr(album, "mb_releasegroupid", "") or "") if album else "", mbid.lower(),
            resolve_release_group=_mb_release_group_for_release, require_release_group=False,
        )
        if not identity.ok:
            return jsonify({"ok": False, "error": identity.error, "code": identity.code}), 409
        mbid = identity.release_id

    def _do(log, cancel_event=None):
        plan = _album_duplicate_resolver_plan(aid, mbid, log)
        allowed_items: Dict[int, Dict[str, Any]] = {}
        missing_by_mbid = {
            _s(track.get("mb_trackid") or "").strip().lower(): track
            for track in plan.get("missing_tracks") or []
            if _s(track.get("mb_trackid") or "").strip()
        }
        for group in plan.get("groups") or []:
            for item in group.get("action_items") or []:
                allowed_items[int(item.get("id") or 0)] = item

        delete_by_album: Dict[int, List[int]] = defaultdict(list)
        retags: List[Dict[str, Any]] = []
        for raw in actions:
            if not isinstance(raw, dict):
                continue
            try:
                item_id = int(raw.get("item_id") or 0)
            except Exception:
                item_id = 0
            action = _s(raw.get("action") or "").strip().lower()
            if item_id <= 0 or action == "skip":
                continue
            item = allowed_items.get(item_id)
            if not item:
                raise RuntimeError(f"Item {item_id} is not available in the duplicate resolver plan")
            if action == "delete":
                delete_by_album[int(item.get("album_id") or 0)].append(item_id)
            elif action == "retag":
                target_mbid = _s(raw.get("target_mb_trackid") or "").strip().lower()
                target = missing_by_mbid.get(target_mbid)
                if not target:
                    raise RuntimeError(
                        f"Retag target for item {item_id} is not currently missing from the selected release"
                    )
                retags.append({"item": item, "target": target})
            else:
                raise RuntimeError(f"Unsupported resolver action: {action}")

        if not delete_by_album and not retags:
            log.append("No resolver actions were selected.")
            return {"ok": True, "deleted": 0, "retagged": 0, "dry_run": dry_run}

        log.append(
            f"{'Dry run: ' if dry_run else ''}Applying duplicate resolver: "
            f"{sum(len(v) for v in delete_by_album.values())} delete(s), {len(retags)} retag(s)"
        )

        deleted = 0
        delete_summaries = []
        for item_album_id, item_ids in sorted(delete_by_album.items()):
            if item_album_id <= 0:
                raise RuntimeError("A selected delete item does not have a valid album_id")
            summary = _remove_album_track_items(
                item_album_id,
                item_ids,
                dry_run=dry_run,
                delete_files=delete_files,
                clean_empty_folders=False,
                log=log,
                approved_by="" if dry_run else "operator confirmed duplicate resolver delete",
            )
            deleted += int(summary.get("removed_db") or 0)
            delete_summaries.append({"album_id": item_album_id, **summary})

        retagged = 0
        retagged_ids: List[int] = []
        retag_failures: List[Dict[str, Any]] = []
        if retags:
            if dry_run:
                for entry in retags:
                    item = entry["item"]
                    target = entry["target"]
                    log.append(
                        "  Would retag item {id}: {old} -> {disc}.{track:02d} {title}".format(
                            id=int(item.get("id") or 0),
                            old=_s(item.get("filename") or item.get("title") or ""),
                            disc=int(target.get("disc") or 1),
                            track=int(target.get("track") or 0),
                            title=_s(target.get("title") or ""),
                        )
                    )
                retagged = len(retags)
            else:
                # ARCH-020 (v0.1.44): the album-row merge is an ownership
                # change only and refuses this payload
                # (identity_rewrite_not_supported) -- it rewrote Recording
                # ID and disc/track on the moved items without audio proof.
                # Each source is therefore reported in retag_failures until
                # retag is rebuilt on the recording-attach workflow (see
                # docs/TECHNICAL_DEBT.md ARCH-020).
                #
                # ARCH-003 Wave 33 continuation: decomposed into N single-
                # source-album album_duplicate_merge_v1 calls (one per
                # distinct source album among the selected retag items)
                # rather than extending that shared family's Plan/Apply/
                # Rollback control flow to support multiple source albums
                # in one call -- the family has other real callers, and
                # touching its core flow for this one edge case risked
                # regressing them. adopt_target_fields=True makes each
                # moved item inherit the target album's own current
                # album/albumartist/year/mb_albumid; item_field_overrides
                # gives each moved item its own specific
                # mb_trackid/disc/track/title (the actual retag: each
                # duplicate item assigned to a different missing-track
                # slot). album_duplicate_merge_v1's own Apply already
                # retires a source album row that becomes empty -- no
                # separate cleanup step is needed here the way the old
                # raw-SQL code needed one.
                #
                # DELIBERATE, ACCEPTED TRADE-OFF (see docs/TECHNICAL_DEBT.md):
                # this sacrifices the old code's whole-batch atomicity
                # (one SQL transaction covering every retagged item
                # across every source album at once) for N independent
                # atomic operations, one per source album. A later
                # source's merge failing after an earlier one already
                # committed is a real, possible partial-completion
                # outcome now -- reported truthfully below (per-source
                # success/failure in retag_failures, never silently
                # folded into an overall "ok": true), and each
                # succeeded source's merge remains individually
                # rollback-able through the family's own existing
                # rollback endpoint exactly as any other
                # album_duplicate_merge_v1 operation would be.
                by_source: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
                for entry in retags:
                    by_source[int(entry["item"].get("album_id") or 0)].append(entry)

                for src_aid, entries in sorted(by_source.items()):
                    item_ids = [int(e["item"].get("id") or 0) for e in entries]
                    if src_aid <= 0:
                        retag_failures.append({
                            "source_album_id": src_aid, "item_ids": item_ids,
                            "error": "Retag item does not have a valid source album_id",
                        })
                        continue
                    if cancel_event is not None and cancel_event.is_set():
                        raise RuntimeError("cancelled")

                    item_field_overrides = {
                        str(int(e["item"].get("id") or 0)): {
                            "mb_trackid": _s(e["target"].get("mb_trackid") or ""),
                            "disc": int(e["target"].get("disc") or 1),
                            "track": int(e["target"].get("track") or 0),
                            "title": _s(e["target"].get("title") or ""),
                        }
                        for e in entries
                    }
                    merge_payload = {
                        "target_album_id": int(aid),
                        "source_album_id": src_aid,
                        "item_ids": item_ids,
                        "adopt_target_fields": True,
                        "item_field_overrides": item_field_overrides,
                    }
                    try:
                        plan_res = composite_workflows.plan_album_duplicate_merge(merge_payload)
                    except Exception as ex:
                        retag_failures.append({"source_album_id": src_aid, "item_ids": item_ids, "error": str(ex)})
                        log.append(f"  Retag from album {src_aid} failed (plan): {ex}")
                        continue
                    if not plan_res.get("ok") or not plan_res.get("operation_id"):
                        err = plan_res.get("error") or "plan rejected"
                        retag_failures.append({"source_album_id": src_aid, "item_ids": item_ids, "error": err})
                        log.append(f"  Retag from album {src_aid} failed (plan): {err}")
                        continue
                    try:
                        apply_res = composite_workflows.apply_album_duplicate_merge(plan_res["operation_id"])
                    except Exception as ex:
                        retag_failures.append({"source_album_id": src_aid, "item_ids": item_ids, "error": str(ex)})
                        log.append(f"  Retag from album {src_aid} failed (apply): {ex}")
                        continue
                    if not apply_res.get("ok"):
                        err = apply_res.get("error") or "apply failed"
                        retag_failures.append({"source_album_id": src_aid, "item_ids": item_ids, "error": err})
                        log.append(f"  Retag from album {src_aid} failed (apply): {err}")
                        continue

                    retagged += len(item_ids)
                    retagged_ids.extend(item_ids)
                    for e in entries:
                        item = e["item"]
                        target = e["target"]
                        item_id = int(item.get("id") or 0)
                        label = f"{int(target.get('disc') or 1)}.{int(target.get('track') or 0):02d} {_s(target.get('title') or '')}"
                        log.append(f"  Retagged item {item_id}: {label}")

                if retag_failures:
                    log.append(
                        f"  {len(retag_failures)} source album(s) failed to retag "
                        f"(see above); {retagged} item(s) succeeded across the rest."
                    )

                if retagged:
                    # album_duplicate_merge_v1 never touches the target
                    # album's own row -- adopt_target_fields only ever
                    # copies FROM it into moved items. Stamp the target's
                    # own mb_albumid to the selected release separately,
                    # through the same controlled per-album metadata
                    # update every other album-level field write in this
                    # route already uses.
                    stamp_mbid = _s(plan.get("mb_albumid") or "")
                    if stamp_mbid:
                        stamp_res = composite_workflows.update_album_metadata(int(aid), {"mb_albumid": stamp_mbid})
                        if not stamp_res.get("ok"):
                            log.append(f"  WARN: could not stamp target album mb_albumid: {stamp_res.get('error')}")

                    if write_tags:
                        if cancel_event is not None and cancel_event.is_set():
                            raise RuntimeError("cancelled")
                        tag_result = composite_workflows.update_album_metadata(int(aid), {}, force_write_tags=True)
                        _require_attach_stage_success(tag_result, "duplicate resolver tag write")
                        relocate_result = composite_workflows.relocate_album(int(aid), mode="rename")
                        _require_attach_stage_success(relocate_result, "duplicate resolver relocation")

        if not dry_run and retagged:
            _invalidate_lib_cache()
            _trigger_plex_refresh(log)
        log.append(
            f"Done - deleted {deleted} row(s), retagged {retagged} row(s)"
            + (f", {len(retag_failures)} source album(s) failed" if retag_failures else "")
            + "."
        )
        return {
            # Truthful even when some (not all) source albums failed to
            # retag: the job itself ran to completion and did not crash,
            # but a caller must inspect retag_failures -- never assume
            # every requested retag succeeded just because "ok" is true.
            "ok": True,
            "deleted": deleted,
            "retagged": retagged,
            "retagged_ids": retagged_ids,
            "retag_failures": retag_failures,
            "delete_summaries": delete_summaries,
            "dry_run": dry_run,
        }

    job = jobs.start_python(
        _do,
        label=f"Resolve duplicate tracks: album {aid}",
        metadata={"type": "duplicate-track-resolver", "album_id": aid, "mb_albumid": mbid},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.get("/api/albums/<int:aid>/mb-completeness")
def album_mb_completeness(aid):
    mbid = (request.args.get("mb_albumid") or "").strip()
    try:
        data = _album_mb_completeness(aid, mbid)
        return jsonify(data)
    except Exception as ex:
        _app_logger.warning("album_mb_completeness failed for album %s: %s", aid, type(ex).__name__)
        return jsonify({"ok": False, "error": "Could not check MusicBrainz completeness."}), 400


@app.post("/api/albums/<int:aid>/repair-mb-tracks")
def repair_album_mb_tracks(aid):
    payload = request.get_json(silent=True) or {}
    mbid = _s(payload.get("mb_albumid") or "").strip()

    album = lib.get_album(aid)
    if not album:
        return jsonify({"ok": False, "error": f"Album {aid} not found"}), 404

    def _do(log, cancel_event=None):
        log.append(f"[1/3] Planning MusicBrainz track repair for album_id {aid}...")
        try:
            plan_res = composite_workflows.plan_album_mb_track_repair({"album_id": aid, "mb_albumid": mbid})
        except Exception as ex:
            log.append(f"  ERROR: Plan request failed: {ex}")
            return {"ok": False, "error": f"Repair plan failed: {ex}"}

        if not plan_res.get("ok"):
            err_msg = plan_res.get("error") or "Repair planning failed."
            log.append(f"  ERROR: {err_msg}")
            return {"ok": False, "error": err_msg, "code": plan_res.get("code")}

        op_id = plan_res.get("operation_id")
        updated_count = int(plan_res.get("updated") or 0)
        conflicts = int(plan_res.get("conflicts") or 0)
        # SEC-002 Wave 19 final review: the engine's Plan response never
        # actually returns "release_rows_updated" -- checking for it here
        # meant release-only stamping (updated_count == 0 but
        # release_stamping_needed == True) silently produced an unapplied
        # Preview transaction and reported "nothing to do". Use the real
        # signal (release_stamping_needed / release_stamp_rows) instead.
        needs_apply = bool(op_id) and (
            updated_count > 0
            or bool(plan_res.get("release_stamping_needed"))
            or int(plan_res.get("release_stamp_rows") or 0) > 0
        )

        if not needs_apply:
            if conflicts:
                log.append(
                    f"No MusicBrainz recording IDs needed safe repair; {conflicts} "
                    "conflicting recording ID(s) require manual review."
                )
            else:
                log.append("No MusicBrainz recording IDs needed safe repair.")
            return {"ok": True, "updated": 0, "conflicts": conflicts, "operation_id": op_id}

        log.append(f"[2/3] Applying controlled repair transaction {op_id} for {updated_count} track(s)...")
        try:
            apply_res = composite_workflows.apply_album_mb_track_repair(op_id)
        except Exception as ex:
            log.append(f"  ERROR: Apply request failed: {ex}")
            return {"ok": False, "error": f"Repair apply failed: {ex}", "operation_id": op_id}

        if not apply_res.get("ok"):
            err_msg = apply_res.get("error") or "Repair apply failed."
            log.append(f"  ERROR: {err_msg}")
            return {"ok": False, "error": err_msg, "code": apply_res.get("code"), "operation_id": op_id}

        release_stamp_rows = int(apply_res.get("release_stamp_rows") or 0)
        log.append("[3/3] Finalizing repair and refreshing library cache...")
        _invalidate_lib_cache()
        _trigger_plex_refresh(log)
        log.append(
            f"Done — repaired {updated_count} MusicBrainz recording ID(s), "
            f"stamped release ID on {release_stamp_rows} row(s)."
            + (f" {conflicts} conflicting recording ID(s) require manual review." if conflicts else "")
        )
        return {
            "ok": True,
            "updated": updated_count,
            "release_stamp_rows": release_stamp_rows,
            "conflicts": conflicts,
            "operation_id": op_id,
        }

    job = jobs.start_python(
        _do,
        label=f"Repair MB track IDs: {album.albumartist or album.album or aid}",
        metadata={"type": "repair-mb-tracks", "album_id": aid, "mb_albumid": mbid},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/library/mbid-sticking-repair")
def library_mbid_sticking_repair():
    return _start_library_mbid_sticking_repair(
        request.get_json(silent=True) or {},
        default_limit=50,
        max_limit=500,
        job_label="Library MBID sticking repair",
        job_type="mbid-sticking-repair",
    )


@app.post("/api/library/mb-full-sync")
def library_mb_full_sync():
    return _start_library_mbid_sticking_repair(
        request.get_json(silent=True) or {},
        default_limit=1000,
        max_limit=5000,
        job_label="Library full MB sync",
        job_type="mb-full-sync",
    )


def _db_path_value(path: Path) -> str:
    raw = str(path)
    mroot = str(MUSIC_ROOT)
    return raw[len(mroot) + 1:] if raw.startswith(mroot + "/") else raw


@app.post("/api/library/template-token-cleanup")
def library_template_token_cleanup():
    payload = request.get_json(silent=True) or {}
    dry_run = payload.get("dry_run", True) is not False
    album_id = int(payload.get("album_id") or 0)

    def _do(log, cancel_event=None):
        if album_id:
            log.append(f"Scanning album_id {album_id} for unresolved Beets path-template tokens...")
            result = _cleanup_template_tokens_for_album(album_id, log, dry_run=dry_run)
        else:
            roots = [MUSIC_ROOT]
            log.append("Scanning full music library for unresolved Beets path-template tokens...")
            result = _cleanup_template_token_files(
                roots,
                recursive=True,
                dry_run=dry_run,
                log=log,
            )
        action = "would rename" if dry_run else "renamed"
        log.append(
            f"Done - {action} {result['renamed'] if not dry_run else result['candidates']} "
            f"file(s); quarantined: {result.get('quarantined', 0)}; DB path updates: {result['db_updates']}; skipped: {result['skipped']}."
        )
        if not dry_run and (result.get("renamed") or result.get("quarantined")):
            _invalidate_lib_cache()
            _trigger_plex_refresh(log)
        return result

    job = jobs.start_python(
        _do,
        label="Library template-token cleanup",
        metadata={
            "type": "template-token-cleanup",
            "dry_run": dry_run,
            "album_id": album_id,
        },
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/library/leaked-db-paths/scan")
def scan_leaked_db_paths_job():
    """Start a background scan for DB rows with leaked template-token paths.

    Returns job_id. Poll /api/jobs/<job_id> for status; result has rows.
    """
    def _do(log, cancel_event=None, update_state=None):
        log.append("Scanning DB for items with unresolved path-template tokens...")
        scan_meta: Dict[str, Any] = {}
        rows = _scan_leaked_db_paths(progress=update_state, cancel_event=cancel_event, scan_meta=scan_meta)
        safe = [r for r in rows if r.get("safe")]
        unsafe = [r for r in rows if not r.get("safe")]
        final_summary = _leaked_db_paths_summary(
            rows,
            total_scanned=scan_meta.get("total_db_rows_scanned"),
        )
        if update_state:
            update_state({
                "category": "Cleanup",
                "current_task": "Leaked DB Paths scan complete",
                "current_item": None,
                "current_path": None,
                "affected_count": len(rows),
                "safe_count": len(safe),
                "needs_review_count": len(unsafe),
                "skipped_count": len(unsafe),
                "current_result": (
                    f"{len(rows)} leaked row(s): "
                    f"{len(safe)} safe, {len(unsafe)} need review"
                ),
                "final_summary": final_summary,
            })
        log.append(
            f"Found {len(rows)} leaked-path row(s): "
            f"{len(safe)} safe to fix, {len(unsafe)} need manual review."
        )
        for r in unsafe[:20]:
            log.append(f"  Skipped item {r['item_id']}: {r.get('skip_reason','?')} — {r['db_path'][:80]}")
        return {
            "ok": True,
            "total": len(rows),
            "safe_count": len(safe),
            "unsafe_count": len(unsafe),
            "rows": rows,
            "final_summary": final_summary,
        }
    job = jobs.start_python(
        _do,
        label="Leaked DB path scan",
        metadata={"type": "leaked-db-path-scan"},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/library/leaked-db-paths/fix")
def fix_leaked_db_paths():
    """Fix leaked path DB rows (DB-only update — no files are moved).

    Body: { "dry_run": bool (default true), "item_ids": [int] (optional — all safe if omitted) }
    """
    payload = request.get_json(silent=True) or {}
    dry_run = payload.get("dry_run", True) is not False
    if not dry_run and payload.get("confirmed") is not True:
        return jsonify({"ok": False, "error": "Confirmation is required before repairing DB paths"}), 400
    requested_ids: set = set()
    for x in (payload.get("item_ids") or []):
        try:
            requested_ids.add(int(x))
        except Exception:
            pass

    def _do(log, cancel_event=None, update_state=None):
        log.append("Scanning DB for leaked path-template rows...")
        scan_meta: Dict[str, Any] = {}
        all_rows = _scan_leaked_db_paths(progress=update_state, cancel_event=cancel_event, scan_meta=scan_meta)
        safe_rows = [r for r in all_rows if r.get("safe")]
        target_rows = [r for r in safe_rows if r["item_id"] in requested_ids] if requested_ids else safe_rows

        unsafe_rows = [r for r in all_rows if not r.get("safe")]
        if requested_ids:
            unsafe_rows = [r for r in unsafe_rows if r["item_id"] in requested_ids]

        log.append(
            f"Found {len(all_rows)} leaked-path row(s) total; "
            f"{len(safe_rows)} safe to fix; "
            f"{len(all_rows) - len(safe_rows)} skipped."
        )

        fixed = 0
        errors = 0

        for r in target_rows:
            if cancel_event and cancel_event.is_set():
                log.append("Cancelled.")
                break
            item_id = r["item_id"]
            old_path = r["db_path"]
            new_path_str = r["resolved_path"]
            if not new_path_str:
                errors += 1
                continue
            new_path = Path(new_path_str)
            db_val = _db_path_value(new_path)
            if dry_run:
                log.append(
                    f"  [dry-run] item {item_id} (album {r['album_id']}):\n"
                    f"    old: {old_path}\n"
                    f"    new: {new_path_str}"
                )
                fixed += 1
            else:
                album_id = int(r.get("album_id") or 0)
                if album_id <= 0:
                    errors += 1
                    log.append(f"  ERROR item {item_id}: has no album_id; cannot repair through album_maintenance_v1")
                    continue
                try:
                    res = composite_workflows.repoint_item_db_path(item_id, album_id, old_path, db_val)
                except (BeetsUnavailableError, BeetsError) as ex:
                    errors += 1
                    log.append(f"  ERROR item {item_id}: engine unavailable: {ex}")
                    continue
                if res.get("ok") and res.get("repointed"):
                    fixed += 1
                    log.append(
                        f"  Fixed item {item_id}: {Path(old_path).name!r} -> {Path(new_path_str).name!r}"
                    )
                elif res.get("ok"):
                    # Plan found nothing to change (e.g. row already
                    # matched -- a legitimate no-op, not a failure).
                    log.append(f"  No change needed for item {item_id}")
                else:
                    errors += 1
                    log.append(f"  ERROR item {item_id}: {res.get('error') or 'engine rejected repair'}")
            if update_state:
                update_state({
                    "category": "Cleanup",
                    "current_task": "Previewing leaked DB path repairs" if dry_run else "Repairing leaked DB path rows",
                    "current_item": f"item {item_id}",
                    "changed_count": fixed,
                    "error_count": errors,
                    "skipped_count": len(unsafe_rows),
                    "current_result": f"{fixed} row(s) {'would be fixed' if dry_run else 'fixed'}",
                })

        for r in unsafe_rows:
            reason = r.get("skip_reason") or "unsafe"
            log.append(
                f"  Skipped item {r['item_id']} (album {r['album_id']}): {reason}\n"
                f"    path: {r['db_path']}"
            )

        action = "would fix" if dry_run else "fixed"
        log.append(
            f"Done — {action} {fixed} row(s); "
            f"skipped {len(unsafe_rows)} unsafe; "
            f"errors {errors}."
        )
        if not dry_run and fixed:
            _invalidate_lib_cache()

        final_summary = {
            "total_db_rows_scanned": int(scan_meta.get("total_db_rows_scanned") or len(all_rows)),
            "safe_repair_candidates": len(safe_rows),
            "selected_rows": len(target_rows),
            "changed_count": fixed,
            "skipped_unsafe": len(unsafe_rows),
            "errors": errors,
            "dry_run": dry_run,
        }
        if update_state:
            update_state({
                "category": "Cleanup",
                "current_task": "Leaked DB path repair preview complete" if dry_run else "Leaked DB path repair complete",
                "current_item": None,
                "current_path": None,
                "changed_count": fixed,
                "skipped_count": len(unsafe_rows),
                "error_count": errors,
                "current_result": (
                    f"{fixed} row(s) {'would be fixed' if dry_run else 'fixed'}; "
                    f"{len(unsafe_rows)} skipped"
                ),
                "final_summary": final_summary,
            })
        return {
            "fixed": fixed,
            "skipped": len(unsafe_rows),
            "errors": errors,
            "dry_run": dry_run,
            "total_scanned": len(all_rows),
            "safe_found": len(safe_rows),
            "rows": all_rows,
            "final_summary": final_summary,
        }

    job = jobs.start_python(
        _do,
        label=f"Leaked DB path fix ({'dry-run' if dry_run else 'apply'})",
        metadata={
            "type": "leaked-db-path-fix",
            "dry_run": dry_run,
        },
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/library/folder-placeholders/scan")
def scan_folder_placeholders_job():
    """Start a background scan for album folders with unresolved placeholder names."""
    def _do(log, cancel_event=None, update_state=None):
        log.append(f"Scanning {MUSIC_ROOT} for folder names with unresolved placeholders...")
        scan_meta: Dict[str, Any] = {}
        rows = _scan_folder_name_placeholders(progress=update_state, cancel_event=cancel_event, scan_meta=scan_meta)
        safe = [r for r in rows if r.get("safe")]
        unsafe = [r for r in rows if not r.get("safe")]
        final_summary = _folder_placeholder_summary(
            rows,
            total_scanned=scan_meta.get("total_folders_scanned"),
        )
        if update_state:
            update_state({
                "category": "Cleanup",
                "current_task": "Folder Names scan complete",
                "current_item": None,
                "current_path": None,
                "placeholder_count": len(rows),
                "safe_count": len(safe),
                "needs_review_count": len(unsafe),
                "target_exists_count": sum(1 for r in rows if r.get("target_exists")),
                "db_tracked_count": sum(1 for r in rows if int(r.get("db_item_count") or 0) > 0),
                "empty_folder_count": sum(1 for r in rows if r.get("is_empty")),
                "skipped_count": len(unsafe),
                "current_result": (
                    f"{len(rows)} placeholder folder(s): "
                    f"{len(safe)} safe, {len(unsafe)} need review"
                ),
                "final_summary": final_summary,
            })
        log.append(
            f"Found {len(rows)} folder(s) with placeholders: "
            f"{len(safe)} safe to rename, {len(unsafe)} need review."
        )
        for r in rows[:40]:
            tag = "[safe]" if r["safe"] else "[review]"
            log.append(f"  {tag} {r['folder']}")
            if r.get("proposed_folder"):
                log.append(f"    → {r['proposed_folder']}")
            if r.get("skip_reason"):
                log.append(f"    skip: {r['skip_reason']}")
        return {
            "ok": True,
            "total": len(rows),
            "safe_count": len(safe),
            "unsafe_count": len(unsafe),
            "rows": rows,
            "final_summary": final_summary,
        }

    job = jobs.start_python(
        _do,
        label="Scan folder placeholder names",
        metadata={"type": "folder-placeholder-scan"},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/items/<int:iid>/replacement/plan")
def item_replacement_plan(iid: int):
    """Plan replacing this album item's file with another tracked item's
    file (e.g. a proven lossless duplicate). Planning never mutates; the
    one replacement authority (backend.item_replacement) re-proves the pair
    by AcoustID and checks the canonical destination. Approve and apply the
    returned transaction separately. Untracked/staged candidate files are
    not supported: that would need a local write into the read-only /music."""
    payload = request.get_json(silent=True) or {}
    try:
        candidate_item_id = int(payload.get("candidate_item_id") or payload.get("replacement_item_id") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "candidate_item_id must be an integer."}), 400
    if not candidate_item_id:
        if _s(payload.get("candidate_path") or payload.get("replacement_path") or "").strip():
            return jsonify({"ok": False, "code": "staged_replacement_unsupported",
                            "error": "Replacement must be a tracked library item (candidate_item_id); "
                                     "replacing from an untracked staged file is not supported."}), 400
        return jsonify({"ok": False, "error": "candidate_item_id is required."}), 400
    try:
        res = _item_replacement.plan_verified_replacement(
            iid, candidate_item_id, reason=_s(payload.get("reason") or "Manual track replacement"))
    except BeetsUnavailableError as exc:
        # error_code is a short fixed identifier, never raw engine text.
        err_code = "beets_unavailable" if not exc.error_code or exc.error_code in ("BEETS_UNREACHABLE", "BEETS_TIMEOUT", "BEETS_ADAPTER_ERROR") else exc.error_code
        return jsonify({"ok": False, "error": "Beets engine is unavailable.", "code": err_code}), 503
    except BeetsError as exc:
        return jsonify({"ok": False, "error": "Track replacement planning failed.", "code": exc.error_code or "beets_error"}), 400
    except Exception:
        _app_logger.exception("item_replacement_plan failed for iid=%s", iid)
        return jsonify({"ok": False, "error": "Track replacement planning failed."}), 500
    if res.get("ok"):
        return jsonify(res), 200
    status = {"item_not_found": 404, "destination_occupied": 409}.get(_s(res.get("code")), 400)
    if res.get("code") in ("fingerprint_disagreement", "fingerprint_unavailable"):
        res = {**res, "code": "candidate_not_verified", "reason_code": res.get("code")}
    return jsonify(res), status


@app.post("/api/items/<int:iid>/replacement/apply")
def item_replacement_apply(iid: int):
    """Apply a previously-Planned track replacement. Requires the
    operation_id returned by item_replacement_plan -- there is deliberately
    no way to Apply without having gone through Plan first, and Plan
    itself performs no mutation, so this is the only place a destructive
    change can actually occur for this workflow."""
    payload = request.get_json(silent=True) or {}
    op_id = _s(payload.get("operation_id")).strip()
    if not op_id:
        return jsonify({"ok": False, "error": "operation_id required"}), 400
    try:
        res = composite_workflows.apply_track_replacement(op_id)
        status_code = 200 if res.get("ok") else 400
        return jsonify(res), status_code
    except BeetsUnavailableError as exc:
        err_code = "beets_unavailable" if not exc.error_code or exc.error_code in ("BEETS_UNREACHABLE", "BEETS_TIMEOUT", "BEETS_ADAPTER_ERROR") else exc.error_code
        return jsonify({"ok": False, "error": "Beets engine is unavailable.", "code": err_code}), 503
    except BeetsError as exc:
        return jsonify({"ok": False, "error": "Track replacement apply failed.", "code": exc.error_code or "beets_error"}), 400
    except Exception:
        _app_logger.exception("item_replacement_apply failed for iid=%s op_id=%s", iid, op_id)
        return jsonify({"ok": False, "error": "Track replacement apply failed."}), 500


def _start_library_mbid_sticking_repair(
    payload: Dict[str, Any],
    *,
    default_limit: int,
    max_limit: int,
    job_label: str,
    job_type: str,
):
    dry_run = bool(payload.get("dry_run", False))
    repair_tracks = payload.get("repair_tracks", True) is not False
    write_tags = payload.get("write_tags", True) is not False
    trigger_plex = payload.get("trigger_plex", True) is not False
    try:
        limit = int(payload.get("limit") or default_limit)
    except Exception:
        limit = default_limit
    limit = max(1, min(limit, max_limit))

    def _do(log, cancel_event=None):
        summary = {
            "albums_checked": 0,
            "albums_changed": 0,
            "release_item_rows": 0,
            "track_rows": 0,
            "inferred_album_rows": 0,
            "unlinked_track_gap_albums": 0,
            "resolved_album_rows": 0,
            "unresolved_albums": [],
            "unresolved_count": 0,
            "skipped_already_fixed": 0,
            "failed_count": 0,
            "dry_run": dry_run,
        }

        log.append(
            "Scanning for albums whose MusicBrainz IDs are not sticking "
            f"(limit {limit}, repair_tracks={repair_tracks}, write_tags={write_tags}, dry_run={dry_run})..."
        )

        # If item rows have a single release ID but the album row is blank,
        # restore the album-level release ID first.
        try:
            cand_res = composite_workflows.get_mbid_sticking_candidates(mode="inferred", limit=limit)
            inferred = cand_res.get("inferred") or cand_res.get("candidates") or []
            for row in inferred:
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("cancelled")
                aid = int(row.get("album_id") or row.get("id") or 0)
                mbid = _s(row.get("mb_albumid") or row.get("inferred_mb_albumid")).strip().lower()
                label = f"{_s(row.get('albumartist'))} - {_s(row.get('album'))}".strip(" -")
                if dry_run:
                    log.append(f"  Would restore album mb_albumid for album_id {aid}: {label} -> {mbid}")
                    summary["inferred_album_rows"] += 1
                else:
                    composite_workflows.update_album_metadata(aid, {"mb_albumid": mbid})
                    log.append(f"  Restored album mb_albumid for album_id {aid}: {label}")
                    summary["inferred_album_rows"] += 1
                    summary["albums_changed"] += 1
        except Exception as ex:
            log.append(f"  WARN inferring album release IDs from item rows: {ex}")

        # Albums with a blank album-level mb_albumid and no unanimous item-level
        # evidence to infer one from (the sticky-restore pass above already
        # handled that case). These used to be silently skipped with a log
        # message telling the user to link the release manually. Instead,
        # actually attempt MusicBrainz discovery: release-group -> representative
        # release when a release-group ID is already known (from folder stamping
        # or prior partial matching), otherwise search MB by artist+album+year and
        # validate against the album's own folder tracklist before accepting.
        try:
            cand_res = composite_workflows.get_mbid_sticking_candidates(mode="blank", limit=limit)
            blank_rows = cand_res.get("blank") or cand_res.get("candidates") or []
        except Exception as ex:
            blank_rows = []
            log.append(f"  WARN scanning albums with blank mb_albumid: {ex}")

        summary["unlinked_track_gap_albums"] = len(blank_rows)
        if blank_rows:
            log.append(
                f"  Attempting MusicBrainz discovery for {len(blank_rows)} album(s) "
                "with blank album mb_albumid..."
            )
        for row in blank_rows:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("cancelled")
            aid = int(row.get("id") or row.get("album_id") or 0)
            label = f"{_s(row.get('albumartist'))} - {_s(row.get('album'))}".strip(" -")
            rgid = _s(row["mb_releasegroupid"] or "").strip().lower()
            year = _s(row["year"] or "")
            track_count = int(row["track_count"] or 0)
            aldir = _album_source_folder(aid)
            log.append(
                f"  [album_id {aid}] missing album mb_albumid — {label} "
                f"(release_group={rgid or 'none'}, tracks={track_count}, "
                f"folder={aldir or 'unknown'})"
            )

            mb_input = f"https://musicbrainz.org/release-group/{rgid}" if rgid else ""
            resolved_mbid = ""
            reason = ""
            try:
                resolved_mbid = _resolve_album_release_for_import(
                    mb_input,
                    _s(row["albumartist"] or ""),
                    _s(row["album"] or ""),
                    year,
                    track_count,
                    log,
                    source_folder=aldir,
                    existing_album_id=aid,
                )
            except Exception as ex:
                reason = f"MusicBrainz lookup error: {ex}"
                log.append(f"  [album_id {aid}] WARN resolving release: {ex}")

            if resolved_mbid and _MB_UUID_RE.match(resolved_mbid):
                resolved_rgid = rgid
                if not resolved_rgid:
                    try:
                        cand = _fetch_mb_release_candidate(resolved_mbid) or {}
                        resolved_rgid = _s(cand.get("mb_releasegroupid") or "").strip().lower()
                    except Exception:
                        resolved_rgid = ""
                if dry_run:
                    log.append(
                        f"  [album_id {aid}] would link to release {resolved_mbid}"
                        + (f" (release group {resolved_rgid})" if resolved_rgid and not rgid else "")
                    )
                    summary["resolved_album_rows"] += 1
                else:
                    meta_opts = {"mb_albumid": resolved_mbid}
                    if resolved_rgid and not rgid:
                        meta_opts["mb_releasegroupid"] = resolved_rgid
                    composite_workflows.update_album_metadata(aid, meta_opts)
                    log.append(
                        f"  [album_id {aid}] linked to release {resolved_mbid} "
                        f"(written via IPC: albums.mb_albumid"
                        + (", albums.mb_releasegroupid" if resolved_rgid and not rgid else "")
                        + ") — item-level release/track rows will be repaired below"
                    )
                    summary["resolved_album_rows"] += 1
            else:
                if not reason:
                    reason = (
                        "No MusicBrainz match found" if not aldir
                        else "Multiple low-confidence matches — folder tracklist did not "
                             "confidently confirm a MusicBrainz release"
                    )
                summary["unresolved_albums"].append({
                    "album_id": aid,
                    "label": label,
                    "reason": reason,
                })
                log.append(f"  [album_id {aid}] SKIPPED — {reason}. Needs manual review.")
        summary["unresolved_count"] = len(summary["unresolved_albums"])

        try:
            cand_res = composite_workflows.get_mbid_sticking_candidates(mode="track_gaps", limit=limit)
            rows = cand_res.get("track_gaps") or cand_res.get("candidates") or []
        except Exception as ex:
            raise RuntimeError(f"Could not scan Beets DB for stuck MBIDs: {ex}") from ex

        for row in rows:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("cancelled")
            aid = int(row.get("id") or row.get("album_id") or 0)
            mbid = _s(row.get("mb_albumid")).strip().lower()
            label = f"{_s(row.get('albumartist'))} - {_s(row.get('album'))}".strip(" -")
            release_gaps = int(row.get("release_gaps") or 0)
            track_gaps = int(row.get("track_gaps") or 0)
            summary["albums_checked"] += 1
            log.append(
                f"  [album_id {aid}] repairing {label} — "
                f"missing: {release_gaps} release-id row(s), {track_gaps} recording-id row(s)"
            )

            album_changed = False
            if release_gaps:
                if dry_run:
                    log.append(
                        f"  Would stamp release ID on {release_gaps} item row(s): "
                        f"album_id {aid} {label}"
                    )
                    summary["release_item_rows"] += release_gaps
                else:
                    changed = _stamp_album_release_id(aid, mbid, log)
                    summary["release_item_rows"] += changed
                    album_changed = album_changed or changed > 0
                    if changed:
                        log.append(f"  [album_id {aid}] wrote {changed} row(s) via IPC: items.mb_albumid")

            track_updates: List[tuple] = []
            if repair_tracks:
                try:
                    data = _album_mb_completeness(aid, mbid, [])
                    for expected in data.get("tracks") or []:
                        item = expected.get("item") or {}
                        item_id = int(item.get("id") or 0)
                        if item_id <= 0:
                            continue
                        current_mbid = _s(item.get("mb_trackid") or "").strip().lower()
                        target_mbid = _s(expected.get("mb_trackid") or "").strip()
                        if not target_mbid or current_mbid == target_mbid.lower():
                            continue
                        track_updates.append((
                            target_mbid,
                            int(expected.get("track") or 0),
                            int(expected.get("disc") or 1),
                            _s(expected.get("title") or ""),
                            mbid,
                            item_id,
                        ))
                except Exception as ex:
                    log.append(f"  WARN MB track repair skipped for album_id {aid}: {ex}")

            if track_updates:
                if dry_run:
                    log.append(
                        f"  Would repair {len(track_updates)} recording ID(s): "
                        f"album_id {aid} {label}"
                    )
                    summary["track_rows"] += len(track_updates)
                else:
                    plan_res = composite_workflows.plan_album_mb_track_repair({"album_id": aid, "mb_albumid": mbid})
                    if plan_res.get("ok"):
                        op_id = plan_res.get("operation_id") or ""
                        apply_res = composite_workflows.apply_album_mb_track_repair(op_id, write_tags=write_tags)
                        if apply_res.get("ok"):
                            log.append(f"  Repaired {len(track_updates)} recording ID(s) via IPC: album_id {aid} {label}")
                            summary["track_rows"] += len(track_updates)
                            album_changed = True
                        else:
                            log.append(f"  WARN: album_mb_track_repair apply failed for album_id {aid}: {apply_res.get('error')}")
                            summary["failed_count"] += 1
                    else:
                        log.append(f"  WARN: album_mb_track_repair plan failed for album_id {aid}: {plan_res.get('error')}")
                        summary["failed_count"] += 1

            if album_changed:
                summary["albums_changed"] += 1
            elif not dry_run:
                summary["skipped_already_fixed"] += 1
                log.append(f"  [album_id {aid}] no changes needed — already fixed")

        if not dry_run and (
            summary["albums_changed"]
            or summary["release_item_rows"]
            or summary["track_rows"]
            or summary["inferred_album_rows"]
        ):
            _invalidate_lib_cache()
            if trigger_plex:
                _trigger_plex_refresh(log)
            else:
                log.append("Plex refresh deferred until Clean All final sync.")
        log.append(
            "Done - checked {albums_checked}, changed {albums_changed}, "
            "release item rows {release_item_rows}, recording rows {track_rows}, "
            "inferred album rows {inferred_album_rows}, "
            "newly-linked albums {resolved_album_rows}, "
            "already fixed {skipped_already_fixed}, "
            "unresolved (needs review) {unresolved_count}, "
            "failed {failed_count}.".format(**summary)
        )
        return summary

    job = jobs.start_python(
        _do,
        label=job_label,
        metadata={
            "type": job_type,
            "dry_run": dry_run,
            "repair_tracks": repair_tracks,
            "write_tags": write_tags,
            "trigger_plex": trigger_plex,
            "limit": limit,
        },
    )
    return jsonify({"ok": True, "job_id": job.job_id})
