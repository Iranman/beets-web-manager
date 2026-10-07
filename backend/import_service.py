"""Folder import into Beets: eligibility, targeting and attach (ARCH-001).
"""

from __future__ import annotations

import copy, hashlib, json, math, os, re, sqlite3, threading, time
import backend.job_contract as job_contract
from backend.matching import AcoustIDStatus, verify_audio_against_request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from backend.app_runtime import AUDIO_EXT, DOWNLOADS_ROOT, LOG_FILE, MUSIC_ROOT, TORRENT_SOURCE_ROOTS, _DEFAULT_ALBUM_PATH_TEMPLATE, _MB_TRACK_PREFLIGHT_MATCH_THRESHOLD, _MB_TRACK_REPAIR_MATCH_THRESHOLD, _MB_UUID_RE, _UNRESOLVED_TEMPLATE_TOKEN_RE, _YEAR_SFXRE, _env_int, _extract_mb_uuid, _s
from backend.ai_service import _ai_match_evidence_packet, _auto_merge_case_duplicate_artist_folder, _music_format_policy_rejection_error, _record_ai_match, _validate_import_source_audio
from backend.library_service import _app_managed_download_path, _build_folder_evidence, _delete_album_ids_from_db, _delete_album_items_under_folder, _delete_if_already_in_library, _preserve_torrent_source_path, _resolve_album_release_for_import, _source_audio_missing_track_scan, _strip_year_from_album_name, _target_preview_artist_folder, _target_preview_year
from backend.ai_evidence_service import _track_ai_similarity
from backend.pending_review_store import _library_album_ids_for_folder, _queue_folder_for_manual_review, _remove_pending_review_for_path
from backend.playlist_service import _music_format_preferences
from backend.ai_batch_state_service import _is_music_format_policy_handled_error
from backend.import_reconciliation_service import _remaining_audio_files, _resolve_import_review_cleanup_file, _resolve_import_review_selected_audio_file
from backend.cleanup_service import _cleanup_template_tokens_for_album
from backend.app_runtime import _path_is_under, _safe_path_component
from backend.slskd import stage_selected_audio_files as _stage_selected_audio_files_impl
from backend.import_guard import filter_wanted_tracks_against_missing as _guard_filter_wanted_tracks_against_missing, missing_wanted_tracks_block_retag as _guard_missing_wanted_tracks_block_retag
from backend.audio_preferences import mark_needs_replacement as _mark_music_format_needs_replacement, validate_audio_file as _validate_audio_file_preferences, validate_audio_properties as _validate_audio_properties, handle_rejected_download as _handle_rejected_audio_download
from backend.title_normalize import restore_time_colon_title as _restore_time_colon_title
from backend.beets_adapter import lib, BeetsError, BeetsUnavailableError, BeetsAuthError
import backend.composite_workflows as composite_workflows
import backend.import_reconciliation as _import_reconciliation
import backend.item_replacement as _item_replacement
from backend.acoustid_service import _acoustid_lookup_cached, _acoustid_multi_file, _album_item_abs_path, _album_track_fingerprint_check
from backend.artwork_service import _ART_EXTS, _move_artwork_to_target
from backend.slskd_service import _normalise_wanted_tracks, _slskd_title_norm, _strip_track_filename_id_suffix, _wanted_track_label
from backend.matching_service import _album_mb_completeness, _album_track_score, _artist_folder_name_without_mbid, _best_album_track_match, _fetch_mb_release_tracklist, _folder_release_preflight, _invalidate_lib_cache, _match_tracks_from_mb, _preflight_review_reason, _repair_album_mbid_sticking_once
from backend.app_runtime import jobs
from backend.job_service import _wait_for_child_job
from backend.musicbrainz_service import _library_album_ids_for_musicbrainz, _mb_release_track_count, _prefer_album_mb_release
from backend.serializers import _import_review_path_text_error, _json_from_flask_response, _resolve_import_review_source_path
from backend.pending_review_store import _finalize_pending_review_format_policy_rejection, _mark_pending_review_status, _pending_review_matches
from backend.plex_service import _trigger_plex_refresh

# ── ARCH-001 extracted code ──


def _beet_import_timeout_for_count(count: int, minimum: int = 300, maximum: int = 1200) -> int:
    """Scale beet import timeout with a known audio-file count while
    keeping a hard cap. Shared formula for _beet_import_timeout() (local
    scan) and reimport_disk() (engine-supplied audio_count -- SEC-002
    Wave 8 ARCH-003, since this route's source is not locally scannable)."""
    return max(minimum, min(maximum, 180 + (max(0, int(count or 0)) * 20)))


def _wanted_tracks_not_in_album(existing_album_id: int, mb_albumid: str,
                                wanted_tracks: List[Dict[str, Any]],
                                log: list) -> List[Dict[str, Any]]:
    """Filter a requested track list against the current library state."""
    if not existing_album_id or not mb_albumid:
        return wanted_tracks
    try:
        comp = _album_mb_completeness(existing_album_id, mb_albumid, log)
    except Exception as ex:
        log.append(f"  [import] WARN: could not refresh missing-track list: {ex}")
        return wanted_tracks

    missing_now = _normalise_wanted_tracks(comp.get("missing") or [])
    extra_count = int(comp.get("extra_count") or 0)
    if extra_count and not missing_now:
        log.append(
            f"  [import] Selected MusicBrainz release is complete, but album has "
            f"{extra_count} extra track(s) not on that release. Verify the edition or "
            "run Album Track Cleanup."
        )
    if not wanted_tracks:
        return missing_now
    filtered = _guard_filter_wanted_tracks_against_missing(
        wanted_tracks,
        missing_now,
        title_norm_fn=_slskd_title_norm,
    )
    skipped = len(wanted_tracks) - len(filtered)
    if skipped:
        log.append(f"  [import] Skipping {skipped} requested track(s) that are already in the library")
    return filtered


def _stage_selected_audio_files(aldir: str, audio_files: List[Path],
                                artist: str, album: str, log: list,
                                force_stage: bool = False,
                                target_tracks: Optional[List[Dict[str, Any]]] = None) -> str:
    """Copy a selected subset to a clean import folder when the source has extras."""
    return _stage_selected_audio_files_impl(
        DOWNLOADS_ROOT, AUDIO_EXT, aldir, audio_files, artist, album, log,
        force_stage=force_stage,
        target_tracks=target_tracks,
    )


def _prune_stale_wanted_rows_before_import(existing_album_id: int, mb_albumid: str,
                                           wanted_tracks: List[Dict[str, Any]],
                                           log: list) -> int:
    """Remove missing-file rows that would make Beets reject a downloaded replacement."""
    wanted = _normalise_wanted_tracks(wanted_tracks)
    if not existing_album_id or not wanted:
        return 0
    wanted_pairs = {
        (int(t.get("disc") or 1), int(t.get("track") or 0))
        for t in wanted if int(t.get("track") or 0)
    }
    wanted_mbids = {
        _s(t.get("mb_trackid", "")).strip().lower()
        for t in wanted if t.get("mb_trackid")
    }
    if not wanted_pairs and not wanted_mbids:
        return 0

    def _row_matches_wanted(row: sqlite3.Row) -> bool:
        pair = (int(row["disc"] or 1), int(row["track"] or 0))
        row_mbid = _s(row["mb_trackid"]) if "mb_trackid" in row.keys() else ""
        row_mbid = row_mbid.strip().lower()
        if pair[1] and pair in wanted_pairs:
            return True
        if row_mbid and row_mbid in wanted_mbids:
            return True
        item = {
            "title": _s(row["title"]) if "title" in row.keys() else "",
            "path": "",
            "disc": pair[0],
            "track": pair[1],
            "mb_trackid": row_mbid,
            "length": float(row["length"] or 0) if "length" in row.keys() else 0.0,
        }
        return any(
            _album_track_score(item, target) >= _MB_TRACK_REPAIR_MATCH_THRESHOLD
            for target in wanted
        )

    try:
        items = composite_workflows.find_all_items_by_album_id(int(existing_album_id))
        rows = sorted(
            items,
            key=lambda r: (int(r.get("disc") or 1), int(r.get("track") or 0), int(r.get("id") or 0)),
        )
        delete_ids: List[int] = []
        labels: List[str] = []
        for row in rows:
            raw_path = _s(row.get("path") or "")
            iid = int(row.get("id") or 0)
            if not iid:
                continue
            if raw_path:
                try:
                    if composite_workflows.find_item_by_path(raw_path):
                        continue
                except (BeetsUnavailableError, BeetsError):
                    raise
                except Exception:
                    pass
            if not _row_matches_wanted(row):
                continue
            delete_ids.append(iid)
            labels.append(
                f"{int(row.get('disc') or 1)}.{int(row.get('track') or 0):02d} "
                f"{_s(row.get('title') or '')}"
            )
        if not delete_ids:
            return 0
        # Rows whose files are already missing: rows-only removal, files are
        # never touched (S1). Authorised by the operator's import request.
        app_res = composite_workflows.remove_item_rows_keep_files(
            delete_ids, reason="stale wanted rows before re-import",
            approved_by="operator import request (stale missing rows)")
        removed = list(app_res.get("deleted_items") or [])
        if not app_res.get("ok"):
            raise RuntimeError(
                (app_res.get("error") or "stale wanted-row removal did not complete")
                + f" (removed {len(removed)} of {len(delete_ids)}; operation {app_res.get('operation_id')})")
        label_text = ", ".join(labels[:5])
        log.append(
            "  [import] Removed "
            f"{len(removed)} stale missing DB row(s) before duplicate check (files untouched)"
            + (f": {label_text}" if label_text else "")
        )
        return len(removed)
    except Exception as ex:
        log.append(f"  [import] WARN stale wanted-row cleanup skipped: {ex}")
        return 0


def _merge_imported_album_into_existing(imported_album_id: int, existing_album_id: int,
                                        source_folder: str, log: list,
                                        mb_albumid: str = "",
                                        replace_existing_item_ids: Optional[Iterable[int]] = None) -> int:
    """Move newly imported items onto an existing album row.

    ARCH-002/009: album identity (Release Group) and every contested
    disc/track slot are decided by backend/import_reconciliation.py from
    canonical evidence. Text similarity never discards either file: an
    unproven slot keeps both files and both rows and is recorded for review.
    This function only gathers inputs and applies the planned engine
    transactions.
    """
    if not imported_album_id or not existing_album_id or imported_album_id == existing_album_id:
        return imported_album_id
    try:
        target_by_slot: Dict[tuple, Dict[str, Any]] = {}
        target_rgid = ""
        if mb_albumid:
            mb = _fetch_mb_release_tracklist(mb_albumid, log)
            if mb.get("ok"):
                target_rgid = _s(mb.get("release_group") or "")
                target_by_slot = {
                    (int(t.get("disc") or 1), int(t.get("track") or 0)): t
                    for t in (mb.get("tracks") or [])
                    if int(t.get("track") or 0)
                }
        try:
            existing = composite_workflows.get_album(int(existing_album_id))
            imported = composite_workflows.get_album(int(imported_album_id)) or {}
        except BeetsUnavailableError as ex:
            log.append(f"  [merge] Engine unavailable fetching albums: {ex}")
            raise
        if not existing:
            log.append(f"  [merge] Existing album_id {existing_album_id} not found; keeping imported album_id {imported_album_id}")
            return imported_album_id
        try:
            existing_items = composite_workflows.find_all_items_by_album_id(int(existing_album_id))
            imported_items = composite_workflows.find_all_items_by_album_id(int(imported_album_id))
        except BeetsUnavailableError as ex:
            log.append(f"  [merge] Engine unavailable fetching items: {ex}")
            raise

        def _hits(row: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
            path = _album_item_abs_path(_s(row.get("path") or ""))
            return _acoustid_lookup_cached(path) if path and Path(path).is_file() else None

        plan = _import_reconciliation.plan_reconciliation(
            existing_items, imported_items, target_by_slot,
            _import_reconciliation.album_identity(existing, imported, target_rgid),
            forced_replace_ids=replace_existing_item_ids or (),
            hits_fn=_hits,
            exists_fn=lambda row: bool(_album_item_abs_path(_s(row.get("path") or ""))
                                       and Path(_album_item_abs_path(_s(row.get("path") or ""))).is_file()),
            similarity_fn=_track_ai_similarity,
        )
        log.append(f"  [merge] Reconciliation plan: {plan.counts()} (album identity: {plan.album.reason})")
        reviews = _import_reconciliation.record_reviews(
            plan, existing_album_id=existing_album_id, imported_album_id=imported_album_id,
            release_id=mb_albumid, source_folder=source_folder,
        )
        if reviews:
            log.append(f"  [merge] {len(reviews)} item(s) kept on both sides and queued for reconciliation review.")
        if not plan.album.same_album:
            return imported_album_id

        replace_ok = True
        reconcile_ok = True
        for pair in plan.mapping_pairs:
            # One canonical item-file replacement per slot, left in Preview:
            # both files and rows stay until an operator approves it.
            try:
                plan_res = _item_replacement.plan_verified_replacement(
                    int(pair["old_item_id"]), int(pair["new_item_id"]),
                    reason="Import album merge: verified replacement",
                    expected_recording_id=_s(pair.get("expected_recording_id")))
            except Exception as ex:
                plan_res = {"ok": False, "error": str(ex)}
            if plan_res.get("ok"):
                log.append(f"  [merge] Replacement of item {pair['old_item_id']} by imported item "
                           f"{pair['new_item_id']} planned (tx {plan_res.get('operation_id')}); awaiting approval.")
            else:
                log.append(f"  [merge] Replacement of item {pair['old_item_id']} not planned, both kept for review: "
                           f"{plan_res.get('error') or plan_res.get('code')}")

        if plan.duplicate_rows or plan.move_ids:
            dup_item_ids = [int(r["id"]) for r in plan.duplicate_rows]
            dup_details = [
                {"dup_item_id": int(r["id"]),
                 "survivor_item_ids": plan.survivors_by_slot.get(_import_reconciliation.slot_key(r), [])}
                for r in plan.duplicate_rows
            ]
            try:
                plan_res = composite_workflows.plan_existing_album_reconcile({
                    "imported_album_id": imported_album_id,
                    "existing_album_id": existing_album_id,
                    "dup_item_ids": dup_item_ids,
                    "dup_details": dup_details,
                    "move_item_ids": plan.move_ids,
                    "source_folder": source_folder,
                    "reason": "Existing album reconciliation",
                })
                if plan_res.get("ok"):
                    op_id = plan_res.get("operation_id")
                    apply_res = composite_workflows.apply_existing_album_reconcile(op_id)
                    if apply_res.get("ok"):
                        log.append(f"  [merge] Moved {len(plan.move_ids)} imported item(s) into the existing "
                                   f"album row (tx {op_id}).")
                        if dup_item_ids:
                            cleanup_id = apply_res.get("cleanup_operation_id")
                            log.append(
                                f"  [merge] {len(dup_item_ids)} imported duplicate(s) kept; reviewed cleanup "
                                + (f"tx {cleanup_id} awaits approval." if cleanup_id
                                   else "was not planned (the copies are not proven identical)."))
                    else:
                        reconcile_ok = False
                        log.append(f"  [merge] WARN existing album reconcile apply failed: {apply_res.get('error')}")
                else:
                    reconcile_ok = False
                    log.append(f"  [merge] WARN existing album reconcile plan failed: {plan_res.get('error')}")
            except Exception as ex:
                reconcile_ok = False
                log.append(f"  [merge] WARN exception delegating existing album reconcile to engine: {ex}")

        # Report existing_album_id only when what was attempted succeeded.
        # Rows held for review stay untouched in the imported album row; the
        # caller then validates the existing album and never the held rows.
        attempted = bool(plan.move_ids or plan.duplicate_rows)
        succeeded = replace_ok and reconcile_ok
        return existing_album_id if (attempted and succeeded) else imported_album_id
    except Exception as ex:
        log.append(f"  [merge] Warning: {ex}")
        return imported_album_id


def _start_reimport_disk_job_internal(aldir: str, mb_albumid: str,
                                      albumartist: str = "",
                                      existing_album_id: int = 0,
                                      wanted_tracks: Optional[List[Dict[str, Any]]] = None,
                                      strict_edition_guard: bool = False,
                                      replace_existing_item_ids: Optional[List[int]] = None,
                                      skip_import_lock: bool = False) -> str:
    """Start the existing reimport-disk job without making an HTTP request."""
    payload = {"aldir": aldir, "mb_albumid": mb_albumid}
    if albumartist:
        payload["albumartist"] = albumartist
    if existing_album_id:
        payload["existing_album_id"] = existing_album_id
    if wanted_tracks:
        payload["wanted_tracks"] = _normalise_wanted_tracks(wanted_tracks)
    if replace_existing_item_ids:
        payload["replace_existing_item_ids"] = [int(v) for v in replace_existing_item_ids if int(v or 0)]
        payload["replace_existing"] = True
    if strict_edition_guard:
        payload["strict_edition_guard"] = True
    if skip_import_lock:
        payload["skip_import_lock"] = True
    resp = start_reimport_disk(payload)
    data = _json_from_flask_response(resp)
    if not data.get("ok") or not data.get("job_id"):
        raise RuntimeError(data.get("error") or "failed to start import job")
    return data["job_id"]


def _validate_import_source_evidence(evidence: Dict[str, Any], log: list, *, reject_downloads: bool = True) -> Dict[str, Any]:
    """Same policy as _validate_import_source_audio(), but evaluated against
    already-computed engine-side audio evidence instead of walking the
    source locally (SEC-002 Wave 8 ARCH-003: reimport_disk()'s source lives
    on the Beets engine, not the web manager, so audio properties must come
    from composite_workflows.inspect_import_source(), not a local ffprobe/rglob
    pass this container cannot perform).

    Uses validate_audio_properties() directly -- the pure, already-existing
    evidence-in/decision-out half of the same code _validate_audio_tree_preferences()
    calls -- so the accept/reject policy itself is identical, not
    reimplemented.

    Only _validate_import_source_audio()'s other six call sites (unrelated
    to Wave 8) still use the local-filesystem path; this function is used
    by reimport_disk() only. Rejected-download handling
    (_handle_rejected_audio_download, which deletes/quarantines the file)
    still assumes local access and is not migrated here -- rejections are
    the uncommon case for an already-organized reimport source, and
    building an engine-side quarantine/delete capability for that edge case
    is deferred, not silently dropped (see docs/TECHNICAL_DEBT.md)."""
    prefs = _music_format_preferences()
    canonical_path = _s(evidence.get("canonical_path"))
    try:
        root_is_library = _path_is_under(Path(canonical_path).resolve(strict=False), MUSIC_ROOT.resolve(strict=False))
    except Exception:
        root_is_library = False

    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for entry in evidence.get("audio_files") or []:
        rel = _s(entry.get("relative_path"))
        abs_path = str(Path(canonical_path) / rel) if rel else canonical_path
        result = _validate_audio_properties(entry.get("properties") or {}, prefs)
        row = {"path": abs_path, **result}
        (accepted if result.get("ok") else rejected).append(row)

    for row in accepted:
        msg = row.get("message") or "Accepted: audio matches Music Format Preferences"
        log.append(f"  [audio] {msg}")
    if not rejected:
        return {"ok": True, "accepted": accepted, "rejected": rejected, "total": len(accepted)}

    handled_results: List[Dict[str, Any]] = []
    for row in rejected:
        msg = row.get("message") or "Rejected download: audio does not match Music Format Preferences"
        log.append(f"  [audio] {msg}: {Path(row.get('path') or '').name}")
        if reject_downloads and not root_is_library and row.get("path"):
            log.append("  [audio] Rejected-file quarantine/delete requires local access this route no longer has; leaving file in place for manual review.")
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


def _delete_staged_import_folder(folder_path: str, log: list) -> bool:
    """Remove a temporary download folder after a failed candidate.

    Accepts any path that _app_managed_download_path considers app-owned,
    provided it is not inside MUSIC_ROOT. This covers both _beets_missing_import
    staging folders (SLSKD) and fallback temp dirs created by SpotiFLAC/SoundCloud
    (e.g. 'Album - spotiflac missing <id>').
    """
    if not folder_path:
        return False
    try:
        folder = Path(folder_path).resolve(strict=False)
        music_root = MUSIC_ROOT.resolve(strict=False)
        if _path_is_under(folder, music_root):
            raise ValueError(f"Refusing to delete a folder inside the music library: {folder}")
        if not _app_managed_download_path(folder):
            raise ValueError(f"'{folder}' is not a recognised app-managed download path")
        downloads_root = DOWNLOADS_ROOT.resolve(strict=False)
        if folder == downloads_root:
            return False
        if folder.exists():
            composite_workflows.delete_file(str(folder))
            log.append(f"  [import] Removed failed staging folder: {folder}")
            return True
    except Exception as ex:
        log.append(f"  [import] WARN staging cleanup skipped: {ex}")
    return False


# ── Recent imports tracking ───────────────────────────────────────────────────

_RECENT_IMPORTS_FILE = Path("/config/recent_imports.json")


_recent_imports_lock = threading.Lock()


def _record_recent_import(artist: str, album: str, year: int,
                          track_count: int, mb_albumid: str, aldir: str):
    """Append an album-level entry to the recent imports log."""
    entry = {
        "artist": artist,
        "album": album,
        "year": year,
        "tracks": track_count,
        "mb_albumid": mb_albumid,
        "aldir": aldir,
        "imported_at": int(time.time()),
    }
    with _recent_imports_lock:
        try:
            existing = json.loads(_RECENT_IMPORTS_FILE.read_text()) if _RECENT_IMPORTS_FILE.exists() else []
        except Exception:
            existing = []
        existing.insert(0, entry)
        _RECENT_IMPORTS_FILE.write_text(json.dumps(existing[:100], indent=2))


_IMPORT_JOB_LOCK = threading.Lock()


# Service behind POST /api/albums/reimport-disk (ARCH-001): request-free,
# returns (json_body, http_status); the route and in-process callers share it.
def start_reimport_disk(payload_in: Dict[str, Any]) -> Tuple[Any, int]:
    """Tag & import audio files that are already in the music folder but not in the beets DB.
    Body: {aldir, mb_albumid}
    Uses a temp config with copy/move disabled + zero threshold so beets matches in-place.

    ARCH-003 Wave 33 bugfix: every plan_album_mb_track_repair() call in this
    function passes allow_establish_release_group=True. Without it, this
    function is a real, confirmed-live case of album_mb_track_repair_v1's
    repair_rg_not_established guard: a fresh reimport genuinely can be the
    FIRST time this specific album row's mb_releasegroupid gets set (a
    brand-new album, or an existing album row that predates this project's
    RG tracking), and the engine's default (correct for a plain repair
    caller) refuses to silently establish that identity as a side effect.
    Before this fix, that refusal was surfaced here as a raised
    RuntimeError, silently failing the entire reimport rather than
    completing it -- this function's whole premise is that mb_albumid was
    already independently confirmed (a real beets import against a chosen
    MB release, or explicit user/caller selection), so establishing the
    corresponding Release Group here is the correct, evidence-backed
    behavior, not a silent side effect. A real identity CONFLICT
    (repair_identity_mismatch -- the album already has a DIFFERENT
    established Release Group) is never bypassed by this flag and still
    fails the reimport exactly as before.
    """
    payload    = payload_in
    aldir      = payload.get("aldir", "").strip()
    mb_albumid = payload.get("mb_albumid", "").strip()
    manual_mbid_override = bool(
        payload.get("manual_mbid_override")
        or payload.get("force_mbid")
        or payload.get("force_release")
        or payload.get("manual_mbid")
    )
    # Optional: override the albumartist that will be pinned after mbsync.
    # Useful when files are in a mis-named artist folder and need to land under a different artist.
    forced_albumartist = _artist_folder_name_without_mbid(payload.get("albumartist") or "").strip()
    try:
        existing_album_id = int(payload.get("existing_album_id") or payload.get("album_id") or 0)
    except Exception:
        existing_album_id = 0
    wanted_tracks = _normalise_wanted_tracks(
        payload.get("wanted_tracks") or payload.get("missing_tracks") or [])
    replace_existing_item_ids = []
    for raw_id in (payload.get("replace_existing_item_ids") or []):
        try:
            item_id = int(raw_id or 0)
            if item_id > 0:
                replace_existing_item_ids.append(item_id)
        except Exception:
            pass
    replace_existing = bool(payload.get("replace_existing") or replace_existing_item_ids)
    queue_review_on_uncertain = payload.get("queue_review", True) is not False
    light_confirm = bool(payload.get("light_confirm"))
    strict_edition_guard = bool(
        payload.get("strict_edition_guard") or payload.get("library_import_all")
    )
    skip_import_lock = bool(payload.get("skip_import_lock"))
    if not aldir:
        return {"ok": False, "error": "aldir required"}, 400
    if not mb_albumid:
        return {"ok": False, "error": "mb_albumid required"}, 400
    # SEC-002 Wave 8 ARCH-003: _resolve_import_review_source_path() validated
    # existence/containment/symlink-safety against THIS process's own
    # filesystem -- which has no view of MUSIC_ROOT/DOWNLOADS_ROOT in the
    # shipped web-manager topology, so it could never succeed for a real
    # path (confirmed by the mandatory two-service runtime test). The Beets
    # engine owns those mounts, so it -- not the web manager -- must be the
    # one to validate and inspect the source. inspect_import_source() is
    # engine-authoritative: it reuses the engine's own resolve_safe_path()
    # (full realpath resolution + root containment, closing symlink escapes
    # the same way) and returns only the canonical path plus bounded audio
    # evidence, never letting the caller pick its own trusted root.
    try:
        import_source_evidence = composite_workflows.inspect_import_source(aldir, operation="reimport")
    except BeetsAuthError:
        return {"ok": False, "error": "Beets engine authentication failed."}, 502
    except BeetsUnavailableError:
        return {"ok": False, "error": "Beets engine is unavailable."}, 502
    except BeetsError as exc:
        # The engine's /imports/source/inspect only ever returns one of a
        # fixed set of stable error_code strings (see inspect_import_source()
        # in backend/beets_control_agent.py) -- BeetsClient._request() wraps
        # that string as this exception's message. Still, never interpolate
        # the exception text directly into a response: _request() falls back
        # to embedding up to 200 raw response-body characters for any
        # non-JSON error response it doesn't recognize (e.g. an unexpected
        # proxy/framework error page), which could carry stack-trace-shaped
        # text. Map only the known codes to a safe message; anything else
        # collapses to one generic string.
        _known_source_errors = {
            "invalid_path": "Import source is outside the allowed roots or does not exist.",
            "root_self_rejected": "Import source cannot be an entire trusted root.",
            "invalid_operation": "Import source inspection request was malformed.",
            "inspection_failed": "Could not inspect the import source.",
        }
        _error_text = next(
            (msg for code, msg in _known_source_errors.items() if code in str(exc)),
            "Import source rejected.",
        )
        return {"ok": False, "error": _error_text}, 400
    if not import_source_evidence.get("ok"):
        return {"ok": False, "error": "Import source rejected."}, 400
    # Only the engine's own canonical path is ever used from here on --
    # the raw client-supplied aldir is never reused after this point.
    aldir = str(import_source_evidence["canonical_path"])
    if not existing_album_id:
        try:
            existing_ids = _library_album_ids_for_folder(aldir)
            if len(existing_ids) == 1:
                existing_album_id = int(existing_ids[0])
        except Exception:
            existing_album_id = 0

    def _do(log, cancel_event=None):
        nonlocal aldir, mb_albumid, existing_album_id, wanted_tracks

        # Accept a release UUID, release URL, release-group UUID/URL, or a blank
        # value that can be resolved from the folder/artist context.  The rest
        # of this job expects a concrete MusicBrainz release ID.
        _guess_artist = forced_albumartist or _artist_folder_name_without_mbid(Path(aldir).parent.name)
        _guess_album = _restore_time_colon_title(
            re.sub(r'\s*[\(\[]\d{4}[\)\]]\s*$', '', Path(aldir).name).strip()
        )
        source_is_music_library = str(aldir).rstrip("/").startswith(str(MUSIC_ROOT).rstrip("/") + "/")
        # SEC-002 Wave 8 final mutation binding: torrent-staged sources are no
        # longer preserved here as a separate up-front step. reimport_source_atomic()
        # (called at the actual import mutation below) detects a torrent-staged
        # source itself and performs the protective copy internally, immediately
        # before the same Beets import it protects -- preserving here AND letting
        # the atomic endpoint preserve again would silently double-copy. aldir
        # stays the original canonical torrent source throughout preflight; only
        # the final mutation call operates on the engine's own preserved copy.
        def _maybe_queue_review(folder_path: str, suggestion: Optional[dict],
                                reason: str, *, allow_existing: bool = False,
                                evidence: Optional[Dict[str, Any]] = None) -> bool:
            if queue_review_on_uncertain or light_confirm:
                return _queue_folder_for_manual_review(
                    folder_path, suggestion, reason, log,
                    allow_existing=allow_existing, evidence=evidence)
            mode = "light-confirm" if light_confirm else "no-review"
            log.append(
                f"  Skipped Pending Review ({mode}): {reason} "
                "No library files were changed."
            )
            return False

        def _repair_existing_album_in_place(aid: int, *, partial: bool = False,
                                            manual_override: bool = False) -> None:
            repair_desc = (
                "Manual MusicBrainz release override; syncing album metadata "
                "in-place even though track titles did not pass automatic preflight."
                if manual_override else
                "Existing album is a high-confidence partial match; repairing "
                "matched tags in-place without re-importing."
                if partial else
                "Existing album already matches the selected MusicBrainz "
                "release; repairing tags in-place without re-importing."
            )
            log.append(f"[1/3] {repair_desc}")
            expected_tracks = _mb_release_track_count(mb_albumid, log)
            if not expected_tracks:
                raise RuntimeError(
                    "MusicBrainz release lookup failed during existing album repair; "
                    "album mb_albumid was not changed."
                )
            log.append("[2/3] Matching tracks in existing album...")
            matched = _match_tracks_from_mb(
                mb_albumid,
                aid,
                log,
                zero_unmatched=(not partial and not manual_override),
            )
            if matched < 0:
                raise RuntimeError(
                    "MusicBrainz release lookup failed during existing album repair; "
                    "album mb_albumid was not changed."
                )
            min_ratio = 0.80 if partial else 0.60
            if partial:
                min_matched = max(
                    1,
                    min(expected_tracks, int((expected_tracks * min_ratio) + 0.999)),
                )
            else:
                min_matched = max(1, min(expected_tracks, int(expected_tracks * min_ratio)))
            if matched < min_matched:
                if manual_override:
                    log.append(
                        "  Manual override: only "
                        f"{matched}/{expected_tracks} track(s) matched by title. "
                        "Continuing with album-level MusicBrainz sync; unmatched "
                        "track titles/numbers are left in place."
                    )
                else:
                    raise RuntimeError(
                        f"Existing album repair did not match enough tracks: "
                        f"{matched}/{expected_tracks}"
                    )
            p_res = composite_workflows.plan_album_mb_track_repair({"album_id": aid, "mb_albumid": mb_albumid, "allow_establish_release_group": True})
            if not p_res.get("ok") or not p_res.get("operation_id"):
                raise RuntimeError(f"Engine plan_album_mb_track_repair failed for album {aid}")
            app_res = composite_workflows.apply_album_mb_track_repair(p_res["operation_id"], write_tags=True)
            if not app_res.get("ok"):
                raise RuntimeError(f"Engine apply_album_mb_track_repair failed for album {aid}")

            log.append("[3/3] Writing tags and moving existing album...")
            _strip_year_from_album_name(aid, log)
            up_res = composite_workflows.update_album_metadata(aid, {}, force_write_tags=True)
            if not up_res.get("ok"):
                raise RuntimeError(f"Engine update_album_metadata failed for album {aid}")
            rel_res = composite_workflows.relocate_album(aid, mode="rename")
            if not rel_res.get("ok"):
                raise RuntimeError(f"Engine relocate_album failed for album {aid}")
            _repair_album_mbid_sticking_once(
                aid,
                mb_albumid,
                log,
                write_tags=True,
                cancel_event=cancel_event,
            )
            _remove_pending_review_for_path(aldir, log)
            _invalidate_lib_cache()
            _trigger_plex_refresh(log)
            log.append(f"✓ Done — existing album_id {aid} repaired without re-import.")

        if existing_album_id:
            try:
                exists = composite_workflows.get_album(existing_album_id)
                if not exists:
                    replacement_id = None
                    if mb_albumid:
                        mb_matches = composite_workflows.find_all_albums_by_mb_albumid(mb_albumid)
                        if mb_matches:
                            replacement_id = int(mb_matches[0]["id"])
                    if not replacement_id and _guess_album:
                        q_albums = composite_workflows.find_albums_by_query(f"album:{_guess_album}")
                        if q_albums:
                            replacement_id = int(q_albums[0]["id"])
                    if replacement_id:
                        old_existing = existing_album_id
                        existing_album_id = replacement_id
                        log.append(
                            f"  Existing album_id {old_existing} no longer exists; "
                            f"using current album_id {existing_album_id}"
                        )
                    else:
                        log.append(
                            f"  Existing album_id {existing_album_id} no longer exists; "
                            "continuing as a new folder import"
                        )
                        existing_album_id = 0
            except Exception as ex:
                log.append(f"  Existing album lookup warning: {ex}")
        _resolved_mbid = _resolve_album_release_for_import(
            mb_albumid,
            _guess_artist,
            _guess_album,
            "",
            0,
            log,
            source_folder=aldir,
            existing_album_id=existing_album_id,
            allow_provided_release_override=manual_mbid_override,
            allow_oversized_partial=not strict_edition_guard,
        )
        if not _resolved_mbid:
            if existing_album_id or source_is_music_library:
                review_reason = (
                    "No MusicBrainz release candidate matched this folder's "
                    "tracklist. Verify the correct artist and release before retagging."
                )
                review_suggestion = {
                    "artist": _guess_artist,
                    "albumartist": _guess_artist,
                    "album": _guess_album,
                    "confidence": "low",
                    "reason": review_reason,
                }
                rejected_preflight = None
                rejected_mbid = _s(mb_albumid).strip().lower()
                if _MB_UUID_RE.match(rejected_mbid):
                    review_suggestion["mb_albumid"] = rejected_mbid
                    review_suggestion["mb_url"] = f"https://musicbrainz.org/release/{rejected_mbid}"
                    review_suggestion["mb_valid"] = True
                    try:
                        rejected_preflight = _folder_release_preflight(
                            aldir,
                            rejected_mbid,
                            existing_album_id=existing_album_id,
                            log=None,
                        )
                    except Exception:
                        rejected_preflight = None
                    review_reason = _preflight_review_reason(
                        rejected_preflight,
                        review_reason,
                    )
                    review_suggestion["reason"] = review_reason
                review_evidence = _ai_match_evidence_packet(
                    "missing_track" if wanted_tracks else "light_confirm",
                    folder_path=aldir,
                    suggestion=review_suggestion,
                    folder_evidence=_build_folder_evidence(aldir),
                    preflight=rejected_preflight,
                    wanted_tracks=wanted_tracks,
                    reason=review_reason,
                )
                _maybe_queue_review(
                    aldir,
                    review_suggestion,
                    review_reason,
                    allow_existing=bool(existing_album_id),
                    evidence=review_evidence,
                )
            raise RuntimeError(
                "Could not resolve a MusicBrainz release ID for import; "
                "queued for Review without changing library files."
                if (existing_album_id or source_is_music_library)
                else "Could not resolve a MusicBrainz release ID for import"
            )
        if _resolved_mbid != mb_albumid:
            log.append(f"  Using MusicBrainz release ID: {_resolved_mbid}")
            mb_albumid = _resolved_mbid

        if existing_album_id or source_is_music_library:
            preflight = _folder_release_preflight(
                aldir, mb_albumid, existing_album_id=existing_album_id, log=log)
            target_desc = (
                f"Existing album_id {existing_album_id}"
                if existing_album_id else "Library folder"
            )
            log.append(
                f"  [preflight] {target_desc}: "
                f"{preflight.get('matches', 0)}/{preflight.get('expected', 0)} "
                f"track(s) match selected MusicBrainz release"
                + (f" ({preflight.get('release_title')})" if preflight.get("release_title") else "")
            )
            if preflight.get("release_artist") or preflight.get("folder_artist"):
                log.append(
                    "  [preflight] Artist check: "
                    f"folder={preflight.get('folder_artist')!r}, "
                    f"MusicBrainz={preflight.get('release_artist')!r}, "
                    f"score={preflight.get('artist_score', 0):.0%}"
                )
            if preflight.get("acoustid_top_release"):
                status = "mismatch" if preflight.get("acoustid_mismatch") else "checked"
                log.append(
                    f"  [preflight] AcoustID {status}: selected release "
                    f"{preflight.get('acoustid_target_hits', 0)} hit(s), "
                    f"top release {preflight.get('acoustid_top_release')} "
                    f"{preflight.get('acoustid_top_hits', 0)} hit(s)"
                )
            for line in preflight.get("examples") or []:
                log.append(line)
            staged_missing_import_ok = False
            if replace_existing and existing_album_id and wanted_tracks and not source_is_music_library:
                log.append(
                    "  [import] Replacement mode: importing verified staged file "
                    "instead of treating the requested track as already present."
                )
            if existing_album_id and wanted_tracks and not source_is_music_library and not replace_existing:
                scan = _source_audio_missing_track_scan(aldir, existing_album_id, mb_albumid, log)
                if scan.get("ok"):
                    useful_files = [Path(p) for p in (scan.get("useful_files") or [])]
                    duplicate_count = len(scan.get("duplicate_files") or [])
                    unknown_count = len(scan.get("unknown_files") or [])
                    log.append(
                        "  [import] Staged missing-track scan: "
                        f"{scan.get('missing_count', 0)} still-missing, "
                        f"{len(useful_files)} importable, "
                        f"{duplicate_count} already-present, "
                        f"{unknown_count} unknown"
                    )
                    if useful_files:
                        wanted_tracks = _normalise_wanted_tracks(
                            scan.get("wanted_tracks") or wanted_tracks)
                        log.append(
                            "  [import] Targeting still-missing track(s): "
                            + ", ".join(_wanted_track_label(t) for t in wanted_tracks[:5])
                        )
                        if unknown_count:
                            log.append(
                                "  [import] Ignoring "
                                f"{unknown_count} downloaded file(s) that did not "
                                "safely match requested missing MusicBrainz tracks."
                            )
                        staged_dir = _stage_selected_audio_files(
                            aldir,
                            useful_files,
                            scan.get("artist") or _guess_artist,
                            scan.get("album") or _guess_album,
                            log,
                            target_tracks=_normalise_wanted_tracks(
                                scan.get("wanted_tracks") or wanted_tracks
                            ),
                        )
                        if staged_dir != aldir:
                            aldir = staged_dir
                            source_is_music_library = False
                        staged_missing_import_ok = True
                        log.append(
                            "[import] Staged source matches still-missing "
                            "MusicBrainz track(s); continuing import."
                        )
            if not preflight.get("ok") and not staged_missing_import_ok:
                if manual_mbid_override:
                    log.append(
                        "  Manual MusicBrainz release override accepted. "
                        "Automatic tracklist preflight failed, but the user-provided "
                        "release ID will be used."
                    )
                    if existing_album_id:
                        _repair_existing_album_in_place(
                            existing_album_id,
                            manual_override=True,
                        )
                        return
                    log.append(
                        "  Continuing import for this library folder with the "
                        "provided release ID."
                    )
                else:
                    reason = (
                        "Selected MusicBrainz release did not match this existing "
                        f"library folder ({preflight.get('matches', 0)}/"
                        f"{preflight.get('expected', 0)} track(s) matched"
                        + (
                            f", artist {preflight.get('release_artist')!r} did not match "
                            f"folder artist {preflight.get('folder_artist')!r}"
                            if not preflight.get("artist_ok", True) else ""
                        )
                        + "). "
                        "Verify the release before retagging."
                    )
                    _maybe_queue_review(
                        aldir,
                        {
                            "mb_albumid": mb_albumid,
                            "mb_url": f"https://musicbrainz.org/release/{mb_albumid}",
                            "mb_valid": True,
                            "confidence": "low",
                            "albumartist": _guess_artist,
                            "album": _guess_album,
                            "reason": reason,
                        },
                        reason,
                        allow_existing=bool(existing_album_id),
                        evidence=_ai_match_evidence_packet(
                            "missing_track" if wanted_tracks else "light_confirm",
                            folder_path=aldir,
                            suggestion={
                                "mb_albumid": mb_albumid,
                                "mb_url": f"https://musicbrainz.org/release/{mb_albumid}",
                                "mb_valid": True,
                                "confidence": "low",
                                "albumartist": _guess_artist,
                                "album": _guess_album,
                                "reason": reason,
                            },
                            folder_evidence=_build_folder_evidence(aldir),
                            preflight=preflight,
                            wanted_tracks=wanted_tracks,
                            reason=reason,
                        ),
                    )
                    raise RuntimeError(
                        "Selected MusicBrainz release does not match this library folder; "
                        "queued for Review without changing DB rows."
                    )
            if existing_album_id and not replace_existing and int(preflight.get("matches") or 0) >= int(preflight.get("expected") or 0):
                try:
                    comp = _album_mb_completeness(existing_album_id, mb_albumid, log)
                    complete_existing = (
                        int(comp.get("missing_count") or 0) == 0
                        and int(comp.get("in_library") or 0) >= int(preflight.get("expected") or 0)
                    )
                    if complete_existing:
                        _repair_existing_album_in_place(existing_album_id)
                        # After repair, check for audio files in the folder that
                        # are still not in Beets.  These won't be imported by the
                        # repair path (mbsync/write/move only touches existing DB
                        # items), so they'd cause the album to re-appear in Import
                        # All on every run.  Route them to Import Review instead.
                        try:
                            _audio_exts = {
                                '.flac', '.mp3', '.m4a', '.aac', '.ogg', '.opus',
                                '.wav', '.aiff', '.wv', '.ape', '.dsf', '.dsd',
                            }
                            _disk_audio = [
                                f for f in Path(aldir).iterdir()
                                if f.is_file() and f.suffix.lower() in _audio_exts
                            ]
                            if _disk_audio:
                                _db_abs: set = set()
                                try:
                                    _existing_items = composite_workflows.find_all_items_by_album_id(existing_album_id)
                                    for _it in _existing_items:
                                        _p = _s(_it.get("path"))
                                        _abs = (
                                            str(MUSIC_ROOT / _p.lstrip("/"))
                                            if _p and not _p.startswith("/")
                                            else _p
                                        )
                                        _db_abs.add(_abs)
                                except Exception:
                                    pass
                                _unimported_files = [
                                    f for f in _disk_audio if str(f) not in _db_abs
                                ]
                                if _unimported_files:
                                    _reason = (
                                        f"Album repair complete "
                                        f"({comp.get('in_library', 0)} Beets track(s)), "
                                        f"but {len(_unimported_files)} audio file(s) on "
                                        f"disk are not in Beets"
                                        + (
                                            f" ({', '.join(f.name for f in _unimported_files[:3])}"
                                            + ("..." if len(_unimported_files) > 3 else "")
                                            + ")"
                                        )
                                        + ". Review in Album Track Cleanup."
                                    )
                                    log.append(f"  [import] {_reason}")
                                    _maybe_queue_review(
                                        aldir, None, _reason, allow_existing=True,
                                    )
                        except Exception as _scan_ex:
                            log.append(
                                f"  [import] Post-repair scan warning: {_scan_ex}"
                            )
                        return
                    log.append(
                        "  [import] Folder tracklist matches the selected release, "
                        f"but existing album_id {existing_album_id} is incomplete "
                        f"({comp.get('in_library', 0)}/{comp.get('expected_count', 0)} present). "
                        "Continuing source scan/import to complete it."
                    )
                except Exception as ex:
                    log.append(
                        "  [import] Existing album completeness check warning: "
                        f"{ex}; continuing source scan/import."
                    )
            if existing_album_id and source_is_music_library:
                scan = _source_audio_missing_track_scan(aldir, existing_album_id, mb_albumid, log)
                if scan.get("ok"):
                    useful_files = [Path(p) for p in (scan.get("useful_files") or [])]
                    duplicate_count = len(scan.get("duplicate_files") or [])
                    unknown_count = len(scan.get("unknown_files") or [])
                    log.append(
                        "  [import] Existing album source scan: "
                        f"{scan.get('missing_count', 0)} still-missing, "
                        f"{len(useful_files)} importable, "
                        f"{duplicate_count} already-present, "
                        f"{unknown_count} unknown"
                    )
                    if useful_files:
                        wanted_tracks = _normalise_wanted_tracks(
                            scan.get("wanted_tracks") or wanted_tracks)
                        log.append(
                            "  [import] Targeting still-missing track(s): "
                            + ", ".join(_wanted_track_label(t) for t in wanted_tracks[:5])
                        )
                        staged_dir = _stage_selected_audio_files(
                            aldir,
                            useful_files,
                            scan.get("artist") or _guess_artist,
                            scan.get("album") or _guess_album,
                            log,
                            force_stage=True,
                            target_tracks=_normalise_wanted_tracks(
                                scan.get("wanted_tracks") or wanted_tracks
                            ),
                        )
                        if staged_dir != aldir:
                            log.append(
                                "[import] Existing album detected; importing only "
                                f"{len(useful_files)} file(s) that match still-missing "
                                "MusicBrainz tracks."
                            )
                            aldir = staged_dir
                            source_is_music_library = False
                        else:
                            raise RuntimeError(
                                "Could not stage the still-missing track subset; "
                                "refusing to re-import the existing library folder."
                            )
                    elif int(scan.get("missing_count") or 0) > 0:
                        preflight_matches = int(preflight.get("matches") or 0)
                        preflight_expected = int(preflight.get("expected") or 0)
                        high_confidence_partial = (
                            preflight_expected > 0
                            and preflight_matches < preflight_expected
                            and (preflight_matches / max(1, preflight_expected)) >= 0.80
                        )
                        if high_confidence_partial:
                            log.append(
                                "[import] Existing album is missing "
                                f"{int(scan.get('missing_count') or 0)} MusicBrainz "
                                "track(s), but no safe source file was found for the "
                                "gap. Repairing the confidently matched existing "
                                "tracks instead."
                            )
                            if unknown_count:
                                log.append(
                                    "  [import] Warning: "
                                    f"{unknown_count} file(s) did not map cleanly; "
                                    "leaving them in place for later review."
                            )
                            _repair_existing_album_in_place(existing_album_id, partial=True)
                            _partial_missing = int(scan.get("missing_count") or 0)
                            _partial_in_library = int(scan.get("in_library") or 0)
                            _partial_expected = int(
                                scan.get("expected_count") or preflight_expected or 0
                            )
                            _partial_reason = (
                                f"Repaired {_partial_in_library}/"
                                f"{_partial_expected} track(s) in place. "
                                f"{_partial_missing} track(s) still missing — "
                                "no source files found. Download missing tracks to complete."
                            )
                            _maybe_queue_review(aldir, None, _partial_reason,
                                                allow_existing=True)
                            _invalidate_lib_cache()
                            return
                        if unknown_count:
                            reason_unknown = (
                                "Existing album is missing tracks, but this folder "
                                "contains audio that does not safely match the "
                                "missing MusicBrainz tracks."
                            )
                            unknown_suggestion = {
                                "mb_albumid": mb_albumid,
                                "mb_url": f"https://musicbrainz.org/release/{mb_albumid}",
                                "mb_valid": True,
                                "confidence": "low",
                                "albumartist": _guess_artist,
                                "album": _guess_album,
                                "reason": reason_unknown,
                            }
                            _maybe_queue_review(
                                aldir,
                                unknown_suggestion,
                                reason_unknown,
                                allow_existing=True,
                                evidence=_ai_match_evidence_packet(
                                    "missing_track",
                                    folder_path=aldir,
                                    suggestion=unknown_suggestion,
                                    folder_evidence=_build_folder_evidence(aldir),
                                    preflight=preflight,
                                    wanted_tracks=scan.get("wanted_tracks") or wanted_tracks,
                                    reason=reason_unknown,
                                ),
                            )
                        log.append(
                            "[import] No source files matched the still-missing "
                            "MusicBrainz tracks. Existing album was left unchanged "
                            "and still needs completion from another source."
                        )
                        _invalidate_lib_cache()
                        return {
                            "status": "no_useful_missing_tracks",
                            "existing_album_id": existing_album_id,
                            "mb_albumid": mb_albumid,
                            "missing_count": int(scan.get("missing_count") or 0),
                            "duplicate_count": duplicate_count,
                            "unknown_count": unknown_count,
                            "message": (
                                "Source contained no files that safely match the "
                                "missing MusicBrainz tracks."
                            ),
                        }
                else:
                    raise RuntimeError(
                        "Could not safely scan this existing album for missing tracks; "
                        "refusing full library folder re-import."
                    )

        # ── Step 0a: Template-token file names (report only) ──────────────────
        # Files like "Artist - Album - %02i{$track} - Title.flac" are left over
        # from a previous failed rename. This step used to rename them in place
        # with a raw filesystem move; since S1 (LT-13) raw moves are refused
        # inside the music library and no engine family renames files ahead of
        # an import, so the rename is NOT performed. beet import matches by
        # tags/fingerprint, not by file name, and the import's own move/rename
        # gives the files their final template names.
        try:
            token_files = [
                f.name for f in sorted(Path(aldir).iterdir())
                if f.is_file() and f.suffix.lower() in AUDIO_EXT
                and _UNRESOLVED_TEMPLATE_TOKEN_RE.search(f.stem)
            ]
        except Exception as ex:
            token_files = []
            log.append(f"  Template-token scan warning: {ex}")
        if token_files:
            log.append(
                f"  Pre-rename skipped (not_supported): {len(token_files)} file(s) carry unresolved "
                "template tokens; raw renames are disabled for library safety -- beet import will "
                "rename them to the configured template.")

        # ── Step 0b: Existing DB rows for this folder ─────────────────────────
        # Existing albums are preserved until after the selected MB release has
        # passed preflight; deleting the current rows first can lose a valid
        # library album when the MBID is wrong.
        if existing_album_id:
            log.append(
                f"  Existing album repair: preserving current DB rows for album_id {existing_album_id}"
            )
            if wanted_tracks:
                _prune_stale_wanted_rows_before_import(
                    existing_album_id, mb_albumid, wanted_tracks, log)
        else:
            # S1: the former "clear every orphan row in this folder" block
            # never worked (resolve_folder_to_albums returns a list of album
            # ids, so it always raised and logged a warning) and its comment
            # wrongly claimed it quarantined files. It is not performed;
            # rows are only ever removed through an approved rows-only
            # transaction.
            log.append("  Orphan DB-row pre-cleanup for unowned folders: not performed (not_supported)")

        # Uses the engine-supplied evidence captured at request time (see
        # import_source_evidence above), not a local scan -- this route's
        # source lives on the Beets engine, not the web manager.
        _validate_import_source_evidence(import_source_evidence, log, reject_downloads=True)
        log.append(f"[1/3] Importing & tagging '{Path(aldir).name}' in-place with MB {mb_albumid}…")
        import_timed_out = False
        import_timeout = _beet_import_timeout_for_count(import_source_evidence.get("audio_count", 0))
        # SEC-002 Wave 8 final mutation binding: the actual Beets import now
        # runs through the engine's reviewed reimport_source_atomic() (POST
        # /imports/reimport), not a bare `_beet_run(... "import" ...)`. This
        # binds the final mutation itself -- not just the earlier inspect
        # step -- to a fresh, engine-side re-verification of the source
        # signature and, where real deterministic evidence exists, album
        # identity, immediately before any file is touched. See
        # docs/TECHNICAL_DEBT.md ("SEC-002 Wave 8: production reimport
        # binding") for the evidence-selection rationale and known limits.
        expected_identity: Dict[str, Any] = {}
        if existing_album_id:
            # Strong, DB-backed evidence: the engine looks this row up
            # itself and only objects if the source audio's own embedded
            # tags actively conflict with it -- absence of embedded tags
            # (the common case for a folder being repaired in place) is not
            # itself treated as a conflict. See verify_deterministic_identity().
            expected_identity["existing_album_id"] = existing_album_id
        else:
            # No prior DB row exists. mb_albumid (already resolved, and for
            # existing-library/known folders already preflight-matched
            # above) is the only concrete deterministic claim available for
            # a brand-new folder. If the source has no embedded MusicBrainz
            # tags to confirm it against -- the common case for freshly
            # downloaded, not-yet-tagged audio -- the engine correctly
            # returns review_required rather than importing on trust alone.
            expected_identity["mb_albumid"] = mb_albumid

        if cancel_event and cancel_event.is_set():
            raise RuntimeError("cancelled")
        try:
            atomic_res = composite_workflows.reimport_source(
                aldir,
                expected_source_signature=import_source_evidence.get("source_signature"),
                expected_deterministic_identity=expected_identity,
                beets_options={
                    "mb_albumid": mb_albumid,
                    "duplicate_action": "keep" if existing_album_id else "remove",
                },
                timeout=import_timeout + 15.0,
            )
        except BeetsAuthError:
            raise RuntimeError("Beets engine authentication failed.")
        except BeetsUnavailableError:
            raise RuntimeError("Beets engine is unavailable.")
        except BeetsError:
            raise RuntimeError("Beets import request failed.")

        if not atomic_res.get("ok"):
            err_code = atomic_res.get("error_code", "import_failed")
            err_msg = str(atomic_res.get("message") or err_code)
            if err_code == "import_failed" and "timed out" in err_msg.lower():
                # Soft-timeout recovery, matching the previous behavior: the
                # Beets CLI process may exceed its timeout while having
                # still completed the actual mutation. The web manager has
                # its own authoritative read access to the same Beets
                # library DB, so check directly rather than treating this
                # as a hard failure immediately.
                import_timed_out = True
                log.append(f"  ⚠ Beets import timed out — checking if files were processed")
            elif err_code in ("review_required", "identity_mismatch", "stale_source",
                               "identity_verification_failed"):
                _review_reasons = {
                    "review_required": "Could not verify this source's MusicBrainz identity with enough confidence to import automatically.",
                    "identity_mismatch": "This source's embedded MusicBrainz identity conflicts with the requested target; refusing to import automatically.",
                    "stale_source": "Source files changed after they were inspected; refusing to import a possibly different set of files.",
                    "identity_verification_failed": "Could not verify this source's identity against the library database.",
                }
                review_reason = _review_reasons[err_code]
                _maybe_queue_review(
                    aldir,
                    {
                        "mb_albumid": mb_albumid,
                        "mb_url": f"https://musicbrainz.org/release/{mb_albumid}",
                        "mb_valid": True,
                        "confidence": "low",
                        "albumartist": _guess_artist,
                        "album": _guess_album,
                        "reason": review_reason,
                    },
                    review_reason,
                    allow_existing=bool(existing_album_id),
                )
                raise RuntimeError(f"{review_reason} Queued for Review without changing library files.")
            else:
                raise RuntimeError(f"Beets import failed ({err_code}).")

        # atomic_res never carries raw stdout/stderr (SEC-002 Wave 8
        # sanitization). The previous "already in the library" phrase-
        # detection cleanup relied on that text and, in the shipped
        # topology, already had no local view of engine-owned download
        # paths to act on regardless -- calling it with an empty string
        # preserves its existing (already inert here) behavior.
        _delete_if_already_in_library(aldir, "", log)

        # ── Find album in DB ───────────────────────────────────────────────────
        log.append("[2/3] Locating album in library…")
        album_ids: list = []
        item_ids: list = []
        strategy = ""

        if not import_timed_out and atomic_res.get("ok"):
            aid = atomic_res.get("album_id")
            if aid and atomic_res.get("album_id_verified") and not atomic_res.get("album_lookup_failed"):
                album_ids = [int(aid)]
                strategy = "engine-verified mb_albumid"
            else:
                raise RuntimeError(
                    "Beets import completed but the engine could not deterministically "
                    "verify the resulting album; refusing to guess which library row it "
                    "created."
                )
        else:
            # Soft-timeout recovery: the mutation call itself reported a
            # timeout, so fall back to a direct, deterministic lookup by
            # mb_albumid via composite_workflows (the same authoritative key the
            # engine's own atomic endpoint uses), never a heuristic path/name guess.
            time.sleep(1)
            try:
                mb_albums = composite_workflows.find_all_albums_by_mb_albumid(mb_albumid)
                for malb in mb_albums:
                    m_aid = int(malb.get("id") or 0)
                    if m_aid:
                        items = composite_workflows.find_all_items_by_album_id(m_aid)
                        if items:
                            album_ids = [m_aid]
                            strategy = "post-timeout mb_albumid lookup"
                            break
            except Exception as ex:
                log.append(f"  DB warning (post-timeout lookup): {ex}")
            if not album_ids:
                raise RuntimeError("import timed out and album was not found in the Beets DB")

        log.append(f"Found {len(album_ids)} album(s) via {strategy}: {album_ids}")

        # ── Retag + rename ────────────────────────────────────────────────────
        log.append("[3/3] Matching tracks, writing tags, renaming files…")
        final_album_ids: List[int] = []
        skipped_album_errors: List[str] = []
        for aid in album_ids:
            if existing_album_id and aid == existing_album_id and wanted_tracks:
                still_missing_before_retag = _wanted_tracks_not_in_album(
                    existing_album_id, mb_albumid, wanted_tracks, log)
                if _guard_missing_wanted_tracks_block_retag(still_missing_before_retag):
                    labels = ", ".join(
                        _wanted_track_label(t) for t in still_missing_before_retag[:5]
                    )
                    _delete_album_items_under_folder(aid, aldir, log)
                    _delete_staged_import_folder(aldir, log)
                    raise RuntimeError(
                        "Downloaded file(s) did not satisfy requested missing "
                        "MusicBrainz track(s) before retagging; existing "
                        f"album_id {aid} was left untouched: "
                        f"{labels or len(still_missing_before_retag)}"
                    )

            p_res = composite_workflows.plan_album_mb_track_repair({"album_id": aid, "mb_albumid": mb_albumid, "allow_establish_release_group": True})
            if not p_res.get("ok") or not p_res.get("operation_id"):
                raise RuntimeError(f"Engine plan_album_mb_track_repair failed for album {aid}")
            app_res = composite_workflows.apply_album_mb_track_repair(p_res["operation_id"], write_tags=True)
            if not app_res.get("ok"):
                raise RuntimeError(f"Engine apply_album_mb_track_repair failed for album {aid}")
            log.append(f"  Set albums.mb_albumid (id={aid})")

            # Match each track by title similarity against MB release data
            # (handles filenames like "Artist - Album - %02i{$track} - Title")
            match_targets = wanted_tracks if (existing_album_id and aid != existing_album_id) else None
            matched = _match_tracks_from_mb(
                mb_albumid,
                aid,
                log,
                zero_unmatched=True,
                target_tracks=match_targets,
            )
            if matched < 0:
                raise RuntimeError(
                    "MusicBrainz release lookup failed during track matching; "
                    "refusing to delete imported DB rows."
                )
            log.append(f"  → {matched} track(s) matched and numbered from MB.")

            if existing_album_id and aid != existing_album_id and wanted_tracks and matched > 0:
                fp_validation = _validate_wanted_album_items_with_acoustid(
                    aid, mb_albumid, wanted_tracks, log)
                if not fp_validation.get("ok", True):
                    labels = []
                    for mismatch in (fp_validation.get("mismatches") or [])[:5]:
                        target = mismatch.get("target") or {}
                        labels.append(
                            _wanted_track_label({
                                "disc": target.get("disc", 1),
                                "track": target.get("track", 0),
                                "title": target.get("title", ""),
                            })
                        )
                    _delete_album_items_under_folder(aid, aldir, log)
                    raise RuntimeError(
                        "AcoustID rejected downloaded file(s) for requested "
                        "missing MusicBrainz track(s): "
                        f"{', '.join(labels) if labels else len(fp_validation.get('mismatches') or [])}"
                    )

            if existing_album_id and aid != existing_album_id and matched > 0:
                merged_aid = _merge_imported_album_into_existing(
                    aid, existing_album_id, aldir, log, mb_albumid=mb_albumid,
                    replace_existing_item_ids=replace_existing_item_ids)
                if merged_aid != aid:
                    aid = merged_aid
                    p_res = composite_workflows.plan_album_mb_track_repair({"album_id": aid, "mb_albumid": mb_albumid, "allow_establish_release_group": True})
                    if not p_res.get("ok") or not p_res.get("operation_id"):
                        raise RuntimeError(f"Engine plan_album_mb_track_repair failed for album {aid} after merge")
                    app_res = composite_workflows.apply_album_mb_track_repair(p_res["operation_id"], write_tags=True)
                    if not app_res.get("ok"):
                        raise RuntimeError(f"Engine apply_album_mb_track_repair failed for album {aid} after merge")
                    log.append(f"  Set albums.mb_albumid (id={aid}) after merge")
                    matched = _match_tracks_from_mb(
                        mb_albumid, aid, log, zero_unmatched=True)
                    if matched < 0:
                        raise RuntimeError(
                            "MusicBrainz release lookup failed during track matching; "
                            "refusing to delete imported DB rows."
                        )
                    log.append(
                        f"  → {matched} track(s) matched and numbered from MB after merge."
                    )

            expected_tracks = _mb_release_track_count(mb_albumid, log)
            if not expected_tracks:
                raise RuntimeError(
                    "MusicBrainz release lookup failed during validation; "
                    "refusing to delete imported DB rows."
                )
            actual_tracks = 0
            try:
                actual_tracks = len(composite_workflows.find_all_items_by_album_id(aid))
            except Exception:
                pass
            if existing_album_id and aid == existing_album_id and wanted_tracks:
                still_missing_requested = _wanted_tracks_not_in_album(
                    existing_album_id, mb_albumid, wanted_tracks, log)
                if still_missing_requested:
                    labels = ", ".join(
                        _wanted_track_label(t) for t in still_missing_requested[:5]
                    )
                    _delete_album_items_under_folder(aid, aldir, log)
                    raise RuntimeError(
                        "Downloaded file(s) did not satisfy requested missing "
                        f"MusicBrainz track(s): {labels or len(still_missing_requested)}"
                    )
                validation_expected = max(1, min(actual_tracks, expected_tracks))
                max_allowed = expected_tracks + max(1, expected_tracks // 4)
            elif existing_album_id and aid == existing_album_id:
                validation_expected = expected_tracks
                max_allowed = validation_expected + max(2, validation_expected // 4)
            else:
                validation_expected = len(wanted_tracks) if wanted_tracks else expected_tracks
                max_allowed = validation_expected + max(1 if wanted_tracks else 2, validation_expected // 4)
                clean_partial_import = (
                    not wanted_tracks
                    and actual_tracks < expected_tracks
                    and (actual_tracks >= min(6, expected_tracks) or (actual_tracks <= 3 and matched >= actual_tracks))
                    and matched >= max(1, int(math.ceil(actual_tracks * 0.90)))
                )
                if clean_partial_import:
                    validation_expected = max(1, actual_tracks)
                    max_allowed = expected_tracks + max(1, expected_tracks // 4)
                    log.append(
                        "  Clean partial import accepted: "
                        f"{matched}/{actual_tracks} imported source track(s) "
                        f"matched the {expected_tracks}-track MusicBrainz release. "
                        "Unimported release tracks will remain missing."
                    )
            min_matched = max(1, min(validation_expected, int(validation_expected * 0.60)))
            if actual_tracks > max_allowed or matched < min_matched:
                mismatch_msg = (
                    f"{actual_tracks} file(s), {matched}/{validation_expected} "
                    "expected MB track match(es)"
                )
                if existing_album_id and aid == existing_album_id:
                    scan = _source_audio_missing_track_scan(aldir, existing_album_id, mb_albumid, log)
                    if (scan.get("ok") and scan.get("audio_count")
                            and not scan.get("useful_files")
                            and not scan.get("unknown_files")):
                        log.append(
                            "[import] Source folder contained no tracks still missing "
                            "from the existing album; existing library album left untouched."
                        )
                        _delete_if_already_in_library(aldir, "already in library", log)
                        _invalidate_lib_cache()
                        return
                    raise RuntimeError(
                        f"Downloaded files do not match MusicBrainz release and "
                        f"existing album_id {aid} was left untouched: "
                        f"{mismatch_msg}"
                    )
                _delete_album_ids_from_db([aid], log, delete_files=not source_is_music_library)
                if len(album_ids) > 1:
                    skipped_album_errors.append(f"album_id {aid}: {mismatch_msg}")
                    log.append(
                        f"  Skipped non-matching sidecar album_id {aid}: {mismatch_msg}"
                    )
                    continue
                raise RuntimeError(
                    f"Downloaded files do not match MusicBrainz release: {mismatch_msg}"
                )

            if aid not in final_album_ids:
                final_album_ids.append(aid)

            # ── Infer intended albumartist from folder structure ──────────────────
            # mbsync may change "Wiz Khalifa" → "Wiz Khalifa & Curren$y", moving
            # files out from under the artist folder they belong to in Lidarr.
            # Priority: explicit override → folder path (if under MUSIC_ROOT).
            _intended_albumartist = forced_albumartist  # may be "" if not provided
            if not _intended_albumartist:
                try:
                    _music_root_pfx = str(MUSIC_ROOT) + "/"
                    if aldir.startswith(_music_root_pfx):
                        _rel_parts = Path(aldir).relative_to(MUSIC_ROOT).parts
                        if len(_rel_parts) >= 2:   # Artist/Album/...
                            _intended_albumartist = _artist_folder_name_without_mbid(_rel_parts[0])
                except Exception:
                    pass

            p_res = composite_workflows.plan_album_mb_track_repair({"album_id": aid, "mb_albumid": mb_albumid, "allow_establish_release_group": True})
            if not p_res.get("ok") or not p_res.get("operation_id"):
                raise RuntimeError(f"Engine plan_album_mb_track_repair failed for album {aid}")
            app_res = composite_workflows.apply_album_mb_track_repair(p_res["operation_id"], write_tags=True)
            if not app_res.get("ok"):
                raise RuntimeError(f"Engine apply_album_mb_track_repair failed for album {aid}")

            # ── Restore albumartist if mbsync changed it ──────────────────────
            if _intended_albumartist:
                try:
                    _cur_album = composite_workflows.get_album(aid)
                    _cur_aa = (_cur_album.get("albumartist") if _cur_album else "") or ""
                except Exception:
                    _cur_aa = ""
                if _cur_aa != _intended_albumartist:
                    up_aa = composite_workflows.update_album_metadata(aid, {"albumartist": _intended_albumartist}, force_write_tags=True)
                    if not up_aa.get("ok"):
                        raise RuntimeError(f"Engine update albumartist failed for album {aid}")
                    log.append(
                        f"  [albumartist] Pinned: '{_cur_aa}' → '{_intended_albumartist}'")

            # Strip any trailing year suffix from album name BEFORE rename so the
            # path template $album (%left{$year,4}) doesn't produce "Album (2022) (2022)"
            _strip_year_from_album_name(aid, log)

            up_res = composite_workflows.update_album_metadata(aid, {}, force_write_tags=True)
            if not up_res.get("ok"):
                raise RuntimeError(f"Engine update_album_metadata failed for album {aid}")
            rel_res = composite_workflows.relocate_album(aid, mode="rename")
            if not rel_res.get("ok"):
                raise RuntimeError(f"Engine relocate_album failed for album {aid}")
            log.append(f"  ✓ Relocated album {aid} to: {rel_res.get('dest_dir')}")

            # ── Post-move fallback: only inspect this album's current item dirs ──
            # If a future Beets template/plugin issue leaves unresolved tokens in
            # filenames, do not sweep the entire artist folder from a single import.
            try:
                _cleanup_template_tokens_for_album(aid, log)
            except Exception as pf_ex:
                log.append(f"  Post-fix current-album sweep warning: {pf_ex}")

            # Report final filenames
            try:
                items3 = composite_workflows.find_all_items_by_album_id(aid)
                rows3 = sorted(items3, key=lambda it: int(it.get("track") or 0))
                log.append(f"  ✓ Final file names ({len(rows3)} tracks):")
                for it in rows3:
                    pth = it.get("path")
                    fname = Path(
                        pth.decode("utf-8", errors="replace") if isinstance(pth, bytes) else str(pth or "")
                    ).name
                    trk = int(it.get("track") or 0)
                    log.append(f"    [{trk:02d}] {fname}")
            except Exception:
                pass

        if skipped_album_errors:
            if final_album_ids:
                log.append(
                    "  Ignored non-matching sidecar album(s): "
                    + "; ".join(skipped_album_errors[:3])
                )
            else:
                raise RuntimeError(
                    "Downloaded files do not match MusicBrainz release: "
                    + "; ".join(skipped_album_errors)
                )

        # Wave 25 Round (independent review): item_ids holds items
        # _find_ids_in_db found WITHOUT an album_id -- i.e. standalone
        # tracks Beets did not group into an album row. The previous
        # version of this loop resolved each item's real album_id (below,
        # for _strip_year_from_album_name) but then discarded it, passing
        # the item's own row id to update_album_metadata()/relocate_album()
        # /plan_album_mb_track_repair() as if it WERE an album_id. Those
        # functions take an album_id; an item id can numerically collide
        # with an unrelated album's id, causing wrong-album mutation. Fix:
        # always use the freshly re-resolved real album_id for every
        # album-scoped call, never the item id; skip items that genuinely
        # have no album (there is nothing album-level to repair); and
        # dedupe so two items sharing the same album are only processed
        # once.
        _item_repaired_album_ids: set = set()
        for iid in item_ids:
            try:
                _item_data = composite_workflows.get_item(iid)
                _real_aid = int(_item_data.get("album_id") or 0) if _item_data else 0
            except Exception:
                _real_aid = 0
            if _real_aid <= 0:
                # Genuinely standalone track: no album row to repair,
                # relocate, or MB-track-repair -- album-level operations
                # do not apply.
                continue
            if _real_aid in _item_repaired_album_ids:
                continue
            _item_repaired_album_ids.add(_real_aid)

            _strip_year_from_album_name(_real_aid, log)

            p_res = composite_workflows.plan_album_mb_track_repair({"album_id": _real_aid, "mb_albumid": mb_albumid, "allow_establish_release_group": True})
            if not p_res.get("ok") or not p_res.get("operation_id"):
                raise RuntimeError(f"Engine plan_album_mb_track_repair failed for album {_real_aid}")
            app_res = composite_workflows.apply_album_mb_track_repair(p_res["operation_id"], write_tags=True)
            if not app_res.get("ok"):
                raise RuntimeError(f"Engine apply_album_mb_track_repair failed for album {_real_aid}")
            up_res = composite_workflows.update_album_metadata(_real_aid, {}, force_write_tags=True)
            if not up_res.get("ok"):
                raise RuntimeError(f"Engine update_album_metadata failed for album {_real_aid}")
            rel_res = composite_workflows.relocate_album(_real_aid, mode="rename")
            if not rel_res.get("ok"):
                raise RuntimeError(f"Engine relocate_album failed for album {_real_aid}")
            if _real_aid not in final_album_ids:
                final_album_ids.append(_real_aid)

        if final_album_ids:
            album_ids = final_album_ids

        for _mbid_aid in album_ids:
            _repair_album_mbid_sticking_once(
                int(_mbid_aid),
                mb_albumid,
                log,
                write_tags=True,
                cancel_event=cancel_event,
            )

        # ── Fetch and embed album art ─────────────────────────────────────────
        # Wave 25 round (independent review): BeetsClient had no
        # repair_album_artwork method at all -- every call here raised
        # AttributeError, silently swallowed by this try/except, so
        # artwork was never actually fetched for any import. Best-effort
        # by design (matches the pre-Wave-25 local `beet fetchart`/`beet
        # embedart` behavior this replaces): missing cover art must not
        # fail an otherwise-successful import, but the failure is now a
        # real, checked result rather than a guaranteed no-op.
        for _art_aid in album_ids:
            try:
                _art_res = composite_workflows.fetch_and_embed_album_art(int(_art_aid))
                if not _art_res.get("ok"):
                    log.append(f"  [artwork] Warning: {_art_res.get('error')}")
            except Exception as _ae:
                log.append(f"  [artwork] Warning: {_ae}")

        # ── Record recent import ───────────────────────────────────────────────
        try:
            _ri_artist, _ri_album, _ri_year, _ri_tracks = "", Path(aldir).name, 0, 0
            _aid_for_rec = album_ids[0] if album_ids else None
            if _aid_for_rec:
                try:
                    _rrow = composite_workflows.get_album(_aid_for_rec)
                    _ri_items = composite_workflows.find_all_items_by_album_id(_aid_for_rec)
                    _ri_tracks = len(_ri_items)
                except Exception:
                    _rrow = None
                    _ri_tracks = 0
                if _rrow:
                    _ri_artist = _rrow.get("albumartist") or ""
                    _ri_album  = _rrow.get("album") or Path(aldir).name
                    _ri_year   = int(_rrow.get("year") or 0)
            _record_recent_import(_ri_artist, _ri_album, _ri_year,
                                  _ri_tracks, mb_albumid, aldir)
        except Exception as _rce:
            log.append(f"  [recent] Warning: {_rce}")

        _remove_pending_review_for_path(aldir, log)
        _invalidate_lib_cache()
        _trigger_plex_refresh(log)
        log.append(f"✓ Done — '{Path(aldir).name}' tagged and renamed to library structure.")
        return {
            "album_ids": [int(aid) for aid in album_ids if str(aid).isdigit()],
            "item_ids": [int(iid) for iid in item_ids if str(iid).isdigit()],
            "aldir": aldir,
            "mb_albumid": mb_albumid,
            "existing_album_id": int(existing_album_id or 0),
        }

    def _do_locked(log, cancel_event=None):
        if skip_import_lock:
            log.append("[reimport] Running inside parent import slot…")
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("cancelled")
            return _do(log, cancel_event)
        log.append("[reimport] Queued — waiting for import worker slot…")
        with _IMPORT_JOB_LOCK:
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("cancelled")
            log.append("[reimport] Import slot acquired — starting…")
            # The slot, held durably: one import at a time across processes.
            with job_contract.held("import-slot", log=log, cancel_event=cancel_event):
                return _do(log, cancel_event)

    job = jobs.start_python(_do_locked, label=f"Tag+Import disk: {Path(aldir).name}",
                            metadata={"aldir": aldir, "mb_albumid": mb_albumid,
                                      "existing_album_id": existing_album_id,
                                      "skip_import_lock": skip_import_lock,
                                      "missing_track_count": len(wanted_tracks),
                                      "type": "reimport-disk"})
    return {"ok": True, "job_id": job.job_id}, 200


def _validate_wanted_album_items_with_acoustid(album_id: int, mb_albumid: str,
                                               wanted_tracks: List[Dict[str, Any]],
                                               log: list) -> Dict[str, Any]:
    """Reject downloaded missing-track files when AcoustID proves they are wrong."""
    wanted = _normalise_wanted_tracks(wanted_tracks)
    if not album_id or not mb_albumid or not wanted:
        return {"ok": True, "checked": 0, "mismatches": []}
    mb = _fetch_mb_release_tracklist(mb_albumid, log)
    if not mb.get("ok"):
        return {"ok": True, "checked": 0, "mismatches": [], "warning": mb.get("error", "")}
    mb_tracks = mb.get("tracks") or []
    wanted_pairs = {
        (int(t.get("disc") or 1), int(t.get("track") or 0))
        for t in wanted
        if int(t.get("track") or 0)
    }
    wanted_mbids = {
        _s(t.get("mb_trackid", "")).strip().lower()
        for t in wanted
        if t.get("mb_trackid")
    }
    target_tracks = [
        t for t in mb_tracks
        if (
            (int(t.get("disc") or 1), int(t.get("track") or 0)) in wanted_pairs
            or _s(t.get("mb_trackid", "")).strip().lower() in wanted_mbids
        )
    ]
    target_by_pair = {
        (int(t.get("disc") or 1), int(t.get("track") or 0)): t
        for t in target_tracks
        if int(t.get("track") or 0)
    }
    target_by_mbid = {
        _s(t.get("mb_trackid", "")).strip().lower(): t
        for t in target_tracks
        if t.get("mb_trackid")
    }
    if not target_tracks:
        return {"ok": True, "checked": 0, "mismatches": []}

    try:
        raw_items = composite_workflows.find_all_items_by_album_id(int(album_id))
        rows = sorted(
            raw_items,
            key=lambda it: (
                int(it.get("disc") or 1),
                int(it.get("track") or 0),
                int(it.get("id") or 0),
            )
        )
    except Exception as ex:
        log.append(f"  AcoustID validation warning: {ex}")
        return {"ok": True, "checked": 0, "mismatches": [], "warning": str(ex)}

    checked = 0
    no_result = 0
    confirmed = 0
    unverified: List[Dict[str, Any]] = []
    mismatches: List[Dict[str, Any]] = []
    for row in rows:
        item = {
            "title": _s(row["title"]),
            "path": _s(row["path"]),
            "disc": int(row["disc"] or 1),
            "track": int(row["track"] or 0),
            "mb_trackid": _s(row["mb_trackid"]),
            "length": float(row["length"] or 0),
        }
        item_mbid = _s(item.get("mb_trackid", "")).strip().lower()
        pair = (int(item.get("disc") or 1), int(item.get("track") or 0))
        target = target_by_mbid.get(item_mbid) or target_by_pair.get(pair)
        if not target:
            best = _best_album_track_match(item, target_tracks)
            if float(best.get("score") or 0) >= _MB_TRACK_PREFLIGHT_MATCH_THRESHOLD:
                target = best.get("track") or {}
        if not target:
            continue

        path = _album_item_abs_path(item.get("path", ""))
        cands = _acoustid_lookup_cached(path) if path else []
        if not cands:
            no_result += 1
            continue
        checked += 1
        target_trackid = _s(target.get("mb_trackid", "")).strip().lower()
        # MI-7: the canonical requested-audio check decides. Only the
        # target Recording ID counts -- membership of the target *release*
        # in a hit's releases proves nothing about which track this is --
        # and only hits at/above the canonical floor, with the ambiguity
        # window, are evidence. A reject (a different song) blocks the
        # merge; a review outcome is recorded as unverified, never as
        # confirmed and never as grounds to delete the download.
        verdict = verify_audio_against_request(
            cands,
            expected_title=_s(target.get("title") or ""),
            expected_recording_id=target_trackid,
        ) if target_trackid else {"decision": "review", "recording_id": ""}
        if verdict.get("decision") == "accept":
            confirmed += 1
            continue
        if verdict.get("decision") != "reject":
            unverified.append({"item_id": int(row["id"]), "title": item.get("title", ""),
                               "reason": _s(verdict.get("reason") or "no target recording ID")})
            continue
        rejected_id = _s(verdict.get("recording_id") or "")
        top = next((c for c in cands if _s(c.get("mb_trackid", "")).strip().lower() == rejected_id), cands[0])
        mismatches.append({
            "item_id": int(row["id"]),
            "title": item.get("title", ""),
            "path": path,
            "target": target,
            "candidate": top,
        })

    if checked or no_result:
        log.append(
            "  AcoustID missing-track validation: "
            f"{checked} checked, {confirmed} confirmed, {len(unverified)} unverified, "
            f"{no_result} without fingerprint result, {len(mismatches)} mismatch(es)."
        )
    for mismatch in mismatches[:5]:
        cand = mismatch.get("candidate") or {}
        target = mismatch.get("target") or {}
        log.append(
            "  AcoustID mismatch: "
            f"{mismatch.get('title')!r} should be {target.get('title')!r}, "
            f"fingerprint points to {cand.get('artist', '')} - "
            f"{cand.get('title', '')} ({cand.get('album', '')})"
        )
    return {
        "ok": not mismatches,
        "checked": checked,
        "confirmed": confirmed,
        "unverified": unverified,
        "no_result": no_result,
        "mismatches": mismatches,
    }


def _beet_import_timeout(source_path: str, minimum: int = 300, maximum: int = 1200) -> int:
    """Scale beet import timeout with album size while keeping a hard cap."""
    count = 0
    try:
        root = Path(source_path)
        if root.is_file():
            count = 1 if root.suffix.lower() in AUDIO_EXT else 0
        elif root.exists():
            count = sum(1 for p in root.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXT)
    except Exception:
        count = 0
    return _beet_import_timeout_for_count(count, minimum, maximum)


def _audio_validation_inspection_failed(row: Dict[str, Any]) -> bool:
    props = row.get("properties") if isinstance(row.get("properties"), dict) else {}
    text = " ".join(
        _s(value) for value in (
            row.get("message"),
            " ".join(row.get("reasons") or []),
            props.get("error"),
        )
    ).casefold()
    return bool(
        text
        and not props.get("ok")
        and (
            "ffprobe failed" in text
            or "ffprobe json failed" in text
            or "timed out" in text
            or "timeout" in text
        )
    )


def _filter_import_review_selected_audio_files(audio_files: List[Path], log: list) -> List[Path]:
    """Keep partial imports moving while leaving uninspectable selected files in review."""
    prefs = _music_format_preferences()
    accepted: List[Path] = []
    deferred: List[Path] = []
    rejected: List[Path] = []
    handled_results: List[Dict[str, Any]] = []
    try:
        music_root_resolved = MUSIC_ROOT.resolve(strict=False)
    except Exception:
        music_root_resolved = MUSIC_ROOT
    for path in audio_files:
        row = {"path": str(path), **_validate_audio_file_preferences(str(path), prefs)}
        if row.get("ok"):
            msg = row.get("message") or "Accepted: audio matches Music Format Preferences"
            log.append(f"  [audio] {msg}: {path.name}")
            accepted.append(path)
            continue
        msg = row.get("message") or "Rejected download: audio does not match Music Format Preferences"
        if _audio_validation_inspection_failed(row):
            log.append(f"  [audio] Inspection deferred; source kept in review: {path.name} ({msg})")
            deferred.append(path)
            continue
        log.append(f"  [audio] {msg}: {path.name}")
        rejected.append(path)
        try:
            source_is_library = _path_is_under(path.resolve(strict=False), music_root_resolved)
        except Exception:
            source_is_library = False
        if not source_is_library:
            handled_results.append(_handle_rejected_audio_download(str(path), prefs, log=log))
    if not accepted:
        if deferred and not rejected:
            raise RuntimeError(
                "Audio inspection did not complete for selected files. "
                "Files were kept for retry/review."
            )
        raise RuntimeError(
            _music_format_policy_rejection_error(len(rejected), handled_results, prefs)
        )
    if deferred:
        log.append(
            f"  [audio] Continuing partial import with {len(accepted)} inspected file(s); "
            f"{len(deferred)} file(s) remain in review."
        )
    if rejected:
        log.append(
            f"  [audio] Continuing partial import with {len(accepted)} accepted file(s); "
            f"{len(rejected)} rejected file(s) handled according to settings."
        )
    return accepted


def _resolve_import_review_db_music_path(
    raw: Any,
    *,
    expected_type: Optional[str] = None,
    require_exists: bool = True,
) -> Tuple[Optional[Path], Optional[str]]:
    text = os.fsdecode(raw) if isinstance(raw, (bytes, bytearray)) else _s(raw)
    text = text.strip()
    error = _import_review_path_text_error(text, allow_relative=True)
    if error:
        return None, error
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = MUSIC_ROOT / candidate
    return _resolve_import_review_source_path(
        str(candidate),
        allow_music=True,
        expected_type=expected_type,
        require_exists=require_exists,
    )


def _import_skipped_items(limit: int = 500, *, deep_scan: bool = True,
                          max_log_lines: int = 0) -> List[Dict[str, Any]]:
    try:
        lines = Path(LOG_FILE).read_text(errors="replace").splitlines()
    except FileNotFoundError:
        return []
    if max_log_lines > 0:
        lines = lines[-max_log_lines:]

    skipped = []
    seen: set = set()
    for line in reversed(lines):
        stripped = line.strip()
        # Match lines like: "skip /path/to/folder" or "skip /a/b; /c/d"
        if not stripped.lower().startswith("skip "):
            continue
        rest = stripped[5:].strip()
        # Multiple folders can be separated by "; "; read newest entries first.
        parts = [p.strip() for p in reversed(rest.split(";"))]
        for part in parts:
            if not part or part in seen:
                continue
            seen.add(part)
            # Filter out stale log entries, but stop as soon as the caller's
            # requested page is filled instead of scanning the entire import log.
            p = Path(part)
            if p.exists():
                if p.is_file():
                    has_audio = p.suffix.lower() in AUDIO_EXT
                elif deep_scan:
                    has_audio = any(
                        f.suffix.lower() in AUDIO_EXT
                        for f in p.rglob("*") if f.is_file()
                    )
                else:
                    has_audio = True
                if not has_audio:
                    continue
            else:
                continue  # folder gone entirely; already imported/moved
            skipped.append({
                "path":     part,
                "filename": p.name,
                "folder":   str(p.parent),
            })
            if len(skipped) >= limit:
                return skipped

    return skipped


def _target_preview_source_files(folder_path: str, existing_album_id: int = 0) -> List[Path]:
    files: List[Path] = []
    seen: set[str] = set()

    def _add(path: Path) -> None:
        key = str(path).casefold()
        if key not in seen:
            files.append(path)
            seen.add(key)

    if existing_album_id:
        try:
            items = composite_workflows.find_all_items_by_album_id(int(existing_album_id))
            sorted_items = sorted(
                items,
                key=lambda it: (
                    int(it.get("disc") or 1),
                    int(it.get("track") or 0),
                    _s(it.get("title") or ""),
                    int(it.get("id") or 0),
                ),
            )
            for item in sorted_items:
                raw_path = item.get("path")
                if raw_path:
                    db_path, _error = _resolve_import_review_db_music_path(
                        raw_path,
                        expected_type="file",
                        require_exists=True,
                    )
                    if db_path and db_path.suffix.lower() in AUDIO_EXT:
                        _add(db_path)
        except BeetsUnavailableError:
            raise
        except Exception:
            pass

    source, source_error = _resolve_import_review_source_path(
        folder_path,
        allow_music=True,
        expected_type=None,
        require_exists=True,
    ) if _s(folder_path).strip() else (None, "source path missing")
    if source_error or source is None:
        return files
    try:
        if source.is_file() and source.suffix.lower() in AUDIO_EXT:
            _add(source)
        elif source.is_dir():
            for path in sorted(
                [p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXT],
                key=lambda p: str(p).lower(),
            ):
                selected = _resolve_import_review_selected_audio_file(str(path), source)
                if selected:
                    _add(selected)
    except Exception:
        pass
    return files


_IMPORT_REVIEW_IMPORTABLE_STATUSES = {"matched", "fuzzy", "verified_match", "acoustid_verified"}


_IMPORT_REVIEW_LEFT_BEHIND_STATUSES = {"extra", "unmatched_extra", "ignored_for_this_import"}


IMPORT_REVIEW_AUTO_IMPORT_CONFIDENCE_THRESHOLD = 0.60


_IMPORT_REVIEW_AUTO_IMPORTS_FILE = Path(
    os.environ.get("IMPORT_REVIEW_AUTO_IMPORTS_FILE", "/config/import_review_auto_imports.json")
)


_import_review_auto_lock = threading.RLock()


_IMPORT_TARGET_PREVIEW_CACHE_TTL = _env_int("IMPORT_TARGET_PREVIEW_CACHE_TTL", 20, minimum=0)


_IMPORT_TARGET_PREVIEW_CACHE: Dict[str, Dict[str, Any]] = {}


_IMPORT_TARGET_PREVIEW_CACHE_LOCK = threading.Lock()


def _import_review_selected_source_files(
    folder_path: str,
    selected_source_files: Optional[List[Any]] = None,
    track_mapping: Optional[List[Any]] = None,
) -> List[Path]:
    """Return verified source files selected by Import Review, never extras."""
    source_root, source_error = _resolve_import_review_source_path(
        folder_path,
        allow_music=True,
        expected_type=None,
        require_exists=True,
    ) if _s(folder_path).strip() else (None, "source path missing")
    if source_error or source_root is None:
        return []

    raw_files: List[Any] = list(selected_source_files or [])
    if not raw_files and track_mapping:
        for raw_row in track_mapping:
            row = raw_row if isinstance(raw_row, dict) else {}
            status = _s(row.get("status")).strip().lower()
            if status in _IMPORT_REVIEW_IMPORTABLE_STATUSES:
                raw_source = _s(row.get("source_path")).strip()
                if raw_source:
                    raw_files.append(raw_source)

    selected: List[Path] = []
    seen: set = set()
    for raw in raw_files:
        resolved = _resolve_import_review_selected_audio_file(raw, source_root)
        if not resolved:
            continue
        key = str(resolved).casefold()
        if key in seen:
            continue
        seen.add(key)
        selected.append(resolved)
    return selected


def _import_target_preview_cache_key(payload: Dict[str, Any]) -> str:
    tracks: List[Dict[str, Any]] = []
    raw_mapping = payload.get("track_mapping") or payload.get("tracks") or []
    if isinstance(raw_mapping, list):
        for row in raw_mapping:
            if not isinstance(row, dict):
                continue
            tracks.append({
                "status": _s(row.get("status")).strip().lower(),
                "source_path": _s(row.get("source_path")).strip(),
                "title": _s(row.get("title") or row.get("mb_title") or row.get("local_title")).strip(),
                "num": _s(row.get("num") or row.get("track")).strip(),
            })
    source_path = _s(payload.get("path")).strip()
    source_marker: Dict[str, Any] = {"path": source_path}
    source_for_stat, _source_error = _resolve_import_review_source_path(
        source_path,
        allow_music=True,
        expected_type=None,
        require_exists=True,
    ) if source_path else (None, "source path missing")
    if source_for_stat is not None:
        source_marker["canonical_path"] = str(source_for_stat)
        # Containment validation alone does not invalidate the cache when
        # the folder's own contents change (a file added/replaced/removed)
        # without the path itself changing -- restore the same freshness
        # signal the pre-security-fix implementation had, now computed from
        # the already-validated canonical path rather than raw request text.
        try:
            st = source_for_stat.stat()
            source_marker["size"] = int(st.st_size)
            source_marker["mtime_ns"] = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))
        except Exception:
            pass
    material = {
        "version": 1,
        "source": source_marker,
        "release_group_id": _s(payload.get("release_group_id") or payload.get("mb_releasegroupid")).strip().lower(),
        "representative_release_id": _s(payload.get("representative_release_id") or payload.get("mb_albumid")).strip().lower(),
        "artist": _s(payload.get("artist") or payload.get("albumartist")).strip(),
        "album": _s(payload.get("album")).strip(),
        "year": _s(payload.get("year")).strip(),
        "existing_album_id": _s(payload.get("existing_album_id") or payload.get("album_id")).strip(),
        "identity_validated": payload.get("identity_validated"),
        "candidate_identity_error": _s(payload.get("candidate_identity_error")).strip(),
        "tracks": tracks,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _cached_import_target_preview(payload: Dict[str, Any]) -> Dict[str, Any]:
    if _IMPORT_TARGET_PREVIEW_CACHE_TTL <= 0:
        preview = _build_import_target_preview(payload)
        preview["plan_cache_key"] = _import_target_preview_cache_key(payload)
        return preview
    key = _import_target_preview_cache_key(payload)
    now = time.time()
    with _IMPORT_TARGET_PREVIEW_CACHE_LOCK:
        cached = _IMPORT_TARGET_PREVIEW_CACHE.get(key)
        if cached and (now - float(cached.get("ts") or 0)) < _IMPORT_TARGET_PREVIEW_CACHE_TTL:
            preview = copy.deepcopy(cached.get("payload") or {})
            preview["cache_hit"] = True
            preview["plan_cache_key"] = key
            return preview
    preview = _build_import_target_preview(payload)
    preview["cache_hit"] = False
    preview["plan_cache_key"] = key
    with _IMPORT_TARGET_PREVIEW_CACHE_LOCK:
        _IMPORT_TARGET_PREVIEW_CACHE[key] = {"ts": now, "payload": copy.deepcopy(preview)}
    return preview


def _build_import_target_preview(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a read-only target path preview for Import Review.

    This mirrors the job path standard from _DEFAULT_ALBUM_PATH_TEMPLATE:
    Artist Folder/Album (Year) {mb_releasegroupid}/Artist - Album - 00 - Title.ext
    It performs no filesystem writes and never calls beets.
    """
    folder_path = _s(payload.get("path")).strip()
    release_group_id = _extract_mb_uuid(
        _s(payload.get("release_group_id") or payload.get("mb_releasegroupid"))
    )
    representative_release_id = _extract_mb_uuid(
        _s(payload.get("representative_release_id") or payload.get("mb_albumid"))
    )
    artist = _s(payload.get("artist") or payload.get("albumartist")).strip()
    album = _s(payload.get("album")).strip()
    year = _target_preview_year(payload.get("year"))
    try:
        existing_album_id = int(payload.get("existing_album_id") or payload.get("album_id") or 0)
    except Exception:
        existing_album_id = 0
    raw_mapping = payload.get("track_mapping") or payload.get("tracks") or []
    track_mapping = raw_mapping if isinstance(raw_mapping, list) else []
    identity_validated = payload.get("identity_validated")
    candidate_identity_error = _s(payload.get("candidate_identity_error")).strip()
    identity_invalid = bool(identity_validated is False or candidate_identity_error)
    if identity_invalid:
        track_mapping = []

    blocked: List[str] = []
    warnings: List[str] = []
    trusted_folder_path = folder_path
    trusted_source_path: Optional[Path] = None
    if not folder_path:
        blocked.append("source path missing")
    else:
        trusted_source_path, source_path_error = _resolve_import_review_source_path(
            folder_path,
            allow_music=True,
            expected_type=None,
            require_exists=True,
        )
        if source_path_error or trusted_source_path is None:
            blocked.append(source_path_error or "source path is not allowed")
            trusted_folder_path = ""
        else:
            trusted_folder_path = str(trusted_source_path)
    if "$mb_releasegroupid" in _DEFAULT_ALBUM_PATH_TEMPLATE and not _MB_UUID_RE.match(release_group_id):
        blocked.append("Release Group ID missing or invalid")
    if representative_release_id and not _MB_UUID_RE.match(representative_release_id):
        blocked.append("representative release ID invalid")
    if not track_mapping:
        blocked.append("track mapping incomplete")
    if identity_invalid:
        blocked.append(
            candidate_identity_error
            or "Target preview unavailable until Release Group identity is verified."
        )

    if identity_invalid:
        artist_folder = ""
        album_title = ""
        album_folder = ""
        album_path = MUSIC_ROOT
    else:
        artist_folder = _target_preview_artist_folder(trusted_folder_path, artist)
        album_title = _safe_path_component(_YEAR_SFXRE.sub("", album).strip() or album, "Unknown Album")
        year_suffix = f" ({year})" if year else ""
        rgid_suffix = f" {{{release_group_id}}}" if release_group_id else ""
        album_folder = f"{album_title}{year_suffix}{rgid_suffix}"
        album_path = MUSIC_ROOT / artist_folder / album_folder
    source_path_obj = trusted_source_path if trusted_source_path is not None else Path()

    source_files = _target_preview_source_files(trusted_folder_path, existing_album_id)
    target_folder_exists = False
    target_folder_conflict = False
    try:
        target_folder_exists = bool(not identity_invalid and album_path.exists())
        target_folder_conflict = bool(
            not identity_invalid
            and target_folder_exists
            and source_path_obj
            and album_path.resolve(strict=False) != source_path_obj.resolve(strict=False)
        )
    except Exception:
        target_folder_exists = False
        target_folder_conflict = False
    if target_folder_conflict:
        blocked.append("target album folder already exists")

    unresolved_paths = 0
    release_id_path_warnings = 0
    conflict_count = 0
    already_imported_count = 0
    tracks_to_import_count = 0
    unmatched_extra_count = 0
    rejected_cleanup_count = 0
    missing_album_track_count = 0
    source_index = 0
    tracks: List[Dict[str, Any]] = []
    albumartist_for_filename = _safe_path_component(
        _artist_folder_name_without_mbid(artist or artist_folder),
        "Unknown Artist",
    )
    for idx, raw_row in enumerate(track_mapping):
        row = raw_row if isinstance(raw_row, dict) else {}
        status = _s(row.get("status")).strip().lower()
        track_num_raw = row.get("num") or row.get("track") or idx + 1
        try:
            track_num = int(track_num_raw or idx + 1)
        except Exception:
            track_num = idx + 1

        if status == "missing":
            missing_album_track_count += 1
            continue
        if status in _IMPORT_REVIEW_LEFT_BEHIND_STATUSES:
            unmatched_extra_count += 1
            continue
        if status == "different" or status == "conflicting":
            rejected_cleanup_count += 1
            tracks.append({
                "track": track_num,
                "status": status,
                "source_path": _s(row.get("source_path")).strip(),
                "target_filename": "",
                "target_path": "",
                "target_exists": False,
                "target_conflict": False,
                "already_imported": False,
                "same_as_source": False,
                "unresolved_placeholder": False,
                "uses_release_id_in_path": False,
            })
            continue
        if status not in _IMPORT_REVIEW_IMPORTABLE_STATUSES:
            continue

        source_file: Optional[Path] = None
        row_source_str = _s(row.get("source_path")).strip()
        if row_source_str and trusted_source_path is not None:
            # SEC-002 CodeQL repository-wide closure finding: this preview
            # endpoint took row["source_path"] straight from the request
            # body with no containment check, unlike every sibling
            # Import Review helper (_resolve_import_review_selected_audio_file
            # / _remaining_audio_files / _target_preview_source_files), which
            # all route through _resolve_import_review_source_path() first.
            # Low real impact here (this function performs no filesystem
            # writes -- see its docstring -- so the worst case was an
            # unvalidated .resolve()/.suffix comparison), but tightened to
            # match the established validated-source-file pattern rather
            # than leaving an inconsistent, CodeQL-flagged exception to it.
            resolved_row_source, _row_source_error = _resolve_import_review_cleanup_file(
                row_source_str, trusted_source_path
            )
            source_file = resolved_row_source
        if source_file is None:
            if source_index < len(source_files):
                source_file = source_files[source_index]
            source_index += 1
        if not source_file:
            blocked.append(f"track {track_num} source file missing")
            continue

        raw_title = row.get("mb_title") or row.get("title") or row.get("local_title") or f"Track {track_num}"
        title = _safe_path_component(
            _strip_track_filename_id_suffix(raw_title),
            f"Track {track_num}",
        )
        suffix = source_file.suffix if source_file else ".flac"
        target_name = f"{albumartist_for_filename} - {album_title} - {track_num:02d} - {title}{suffix}"
        target_path = album_path / target_name
        target_exists = False
        same_as_source = False
        try:
            target_exists = target_path.exists()
            same_as_source = target_path.resolve(strict=False) == source_file.resolve(strict=False)
        except Exception:
            target_exists = False
            same_as_source = False
        target_conflict = bool(target_exists and not same_as_source)
        if target_conflict:
            conflict_count += 1
        else:
            tracks_to_import_count += 1
        if same_as_source:
            already_imported_count += 1
        target_text = str(target_path)
        if _UNRESOLVED_TEMPLATE_TOKEN_RE.search(target_text):
            unresolved_paths += 1
        if (
            representative_release_id
            and release_group_id
            and representative_release_id != release_group_id
            and representative_release_id in target_text
        ):
            release_id_path_warnings += 1
        tracks.append({
            "track": track_num,
            "status": status or "unknown",
            "source_path": str(source_file) if source_file else "",
            "target_filename": target_name,
            "target_path": target_text,
            "target_exists": target_exists,
            "target_conflict": target_conflict,
            "already_imported": same_as_source,
            "same_as_source": same_as_source,
            "unresolved_placeholder": bool(_UNRESOLVED_TEMPLATE_TOKEN_RE.search(target_text)),
            "uses_release_id_in_path": bool(
                representative_release_id
                and release_group_id
                and representative_release_id != release_group_id
                and representative_release_id in target_text
            ),
        })

    if track_mapping and not tracks_to_import_count:
        blocked.append("no verified local tracks selected for import")
    if conflict_count:
        if tracks_to_import_count > 0:
            warnings.append(f"{conflict_count} target file conflict(s) will stay in review")
        else:
            blocked.append(f"{conflict_count} target file(s) already exist")
    if unresolved_paths:
        blocked.append(f"{unresolved_paths} target path(s) contain unresolved placeholders")
    if release_id_path_warnings:
        blocked.append("target path uses representative release ID instead of Release Group ID")
    if not source_files and trusted_folder_path:
        warnings.append("No source audio files found for preview")
    if unmatched_extra_count:
        warnings.append(
            f"{unmatched_extra_count} unmatched local file(s) will stay in review"
        )
    if rejected_cleanup_count:
        warnings.append(
            f"{rejected_cleanup_count} rejected local file(s) are ready for cleanup"
        )
    if missing_album_track_count:
        warnings.append(
            f"{missing_album_track_count} album track(s) are missing and can be acquired later"
        )

    # Keep reasons stable and compact.
    blocked_unique = list(dict.fromkeys(reason for reason in blocked if reason))
    warnings_unique = list(dict.fromkeys(reason for reason in warnings if reason))
    safe = not blocked_unique
    cleanup_required_count = (unmatched_extra_count + rejected_cleanup_count) if track_mapping and not tracks_to_import_count else 0
    if safe and tracks_to_import_count > 0:
        next_action = "import"
    elif cleanup_required_count and not conflict_count and not target_folder_conflict:
        next_action = "verify_or_cleanup_unmatched"
    elif conflict_count or target_folder_conflict:
        next_action = "resolve_conflict"
    else:
        next_action = "blocked"
    return {
        "ok": True,
        "safe": safe,
        "status": "safe" if safe else "blocked",
        "next_action": next_action,
        "cleanup_required_count": cleanup_required_count,
        "blocked_reasons": blocked_unique,
        "warnings": warnings_unique,
        "path_template": _DEFAULT_ALBUM_PATH_TEMPLATE,
        "release_group_id": release_group_id,
        "representative_release_id": representative_release_id,
        "artist_folder": artist_folder,
        "album_folder": album_folder,
        "album_path": "" if identity_invalid else str(album_path),
        "album_folder_uses_release_group_id": bool(
            release_group_id and release_group_id in album_folder
        ),
        "target_folder_exists": target_folder_exists,
        "target_folder_conflict": target_folder_conflict,
        "existing_folder_reuse": bool(target_folder_exists and not target_folder_conflict),
        "already_imported_count": already_imported_count,
        "conflict_count": conflict_count,
        "real_conflict_count": conflict_count,
        "placeholder_warning_count": unresolved_paths,
        "release_id_path_warning_count": release_id_path_warnings,
        "source_file_count": len(source_files),
        "track_count": tracks_to_import_count,
        "tracks_to_import_count": tracks_to_import_count,
        "unmatched_extra_count": unmatched_extra_count,
        "rejected_cleanup_count": rejected_cleanup_count,
        "missing_album_track_count": missing_album_track_count,
        "tracks": tracks,
    }


def _import_review_confidence_score(value: Any) -> Optional[float]:
    if isinstance(value, str):
        text = value.strip().rstrip("%")
        if not text:
            return None
        try:
            score = float(text)
        except Exception:
            return None
    else:
        try:
            score = float(value)
        except Exception:
            return None
    if not math.isfinite(score) or score < 0:
        return None
    return score / 100.0 if score > 1 else score


def _import_review_auto_state() -> Dict[str, Any]:
    try:
        if _IMPORT_REVIEW_AUTO_IMPORTS_FILE.exists():
            data = json.loads(_IMPORT_REVIEW_AUTO_IMPORTS_FILE.read_text())
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _write_import_review_auto_state(state: Dict[str, Any]) -> None:
    try:
        _IMPORT_REVIEW_AUTO_IMPORTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _IMPORT_REVIEW_AUTO_IMPORTS_FILE.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def _import_review_auto_update(key: str, **updates) -> None:
    if not key:
        return
    with _import_review_auto_lock:
        state = _import_review_auto_state()
        entry = dict(state.get(key) or {})
        entry.update(updates)
        entry["updated_at"] = time.time()
        state[key] = entry
        _write_import_review_auto_state(state)


def _import_review_auto_key(payload: Dict[str, Any], selected_files: List[Path],
                            release_group_id: str, representative_release_id: str) -> str:
    basis = {
        "version": 1,
        "review_item_id": _s(payload.get("review_item_id")).strip(),
        "path": _s(payload.get("path")).strip(),
        "release_group_id": release_group_id,
        "representative_release_id": representative_release_id,
        "selected_files": sorted(str(path.resolve(strict=False)) for path in selected_files),
    }
    raw = json.dumps(basis, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def evaluate_import_eligibility(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Single backend gate for Import Review auto-enqueue decisions."""
    payload = payload or {}
    selected_match = payload.get("selected_match") if isinstance(payload.get("selected_match"), dict) else {}
    folder_path = _s(payload.get("path")).strip()
    review_item_id = _s(payload.get("review_item_id")).strip()
    release_group_id = _extract_mb_uuid(
        _s(payload.get("mb_releasegroupid") or payload.get("release_group_id") or selected_match.get("release_group_id"))
    )
    representative_release_id = _extract_mb_uuid(
        _s(payload.get("mb_albumid") or payload.get("representative_release_id") or selected_match.get("representative_release_id"))
    )
    track_mapping = payload.get("track_mapping") if isinstance(payload.get("track_mapping"), list) else []
    confidence = _import_review_confidence_score(
        payload.get("confidence_score", selected_match.get("confidence_score"))
    )
    selected_files = _import_review_selected_source_files(
        folder_path,
        payload.get("selected_source_files") if isinstance(payload.get("selected_source_files"), list) else [],
        track_mapping,
    )
    identity_invalid = bool(
        payload.get("identity_validated") is False
        or selected_match.get("identity_validated") is False
    )
    candidate_identity_error = _s(
        payload.get("candidate_identity_error") or selected_match.get("candidate_identity_error")
    ).strip()

    blockers: List[str] = []
    warnings: List[str] = []
    if not folder_path:
        blockers.append("source path missing")
    elif not Path(folder_path).exists():
        blockers.append("source path not found")
    if review_item_id and not _pending_review_matches(folder_path, review_item_id):
        blockers.append("review item does not match source folder")
    if not release_group_id or not _MB_UUID_RE.match(release_group_id):
        blockers.append("Release Group ID missing or invalid")
    if not representative_release_id or not _MB_UUID_RE.match(representative_release_id):
        blockers.append("representative release ID missing or invalid")
    if identity_invalid:
        blockers.append(
            candidate_identity_error
            or "Representative Release ID rejected: it does not belong to selected Release Group"
        )
    if confidence is None or confidence < IMPORT_REVIEW_AUTO_IMPORT_CONFIDENCE_THRESHOLD:
        blockers.append("confidence below 60% auto-import threshold")
    if selected_match.get("preflight_status") and selected_match.get("preflight_status") != "passed":
        blockers.append("track preflight is not passed")
    # ARCH-002: the canonical album decision is authoritative. The confidence
    # threshold above may only narrow it -- a candidate the canonical
    # evaluator refused (matching_decision.action_allowed=False, surfaced by
    # _import_review_build_revalidated_match as is_importable=False) must never
    # be auto-enqueued because a separate confidence number cleared 60%.
    canonical_decision = (
        selected_match.get("matching_decision") if isinstance(selected_match.get("matching_decision"), dict) else {}
    )
    if (
        selected_match.get("matching_decision_blocks_import")
        or (canonical_decision and not canonical_decision.get("action_allowed", False))
    ):
        blockers.append("canonical matching decision requires review")
    elif selected_match.get("is_importable") is False:
        blockers.append("revalidated match is not importable")
    if not selected_files:
        blockers.append("no verified local tracks selected for import")

    preview_payload = {
        "path": folder_path,
        "release_group_id": release_group_id,
        "representative_release_id": representative_release_id,
        "artist": payload.get("artist") or selected_match.get("artist") or "",
        "album": payload.get("album") or selected_match.get("album") or "",
        "year": payload.get("year") or selected_match.get("year") or "",
        "existing_album_id": payload.get("existing_album_id") or payload.get("album_id") or 0,
        "track_mapping": track_mapping,
        "selected_source_files": [str(path) for path in selected_files],
        "identity_validated": False if identity_invalid else True,
        "candidate_identity_error": candidate_identity_error,
    }
    preview = _cached_import_target_preview(preview_payload)
    if not preview.get("safe"):
        blockers.extend(_s(reason) for reason in (preview.get("blocked_reasons") or []))
    preview_import_count = int(preview.get("tracks_to_import_count") or 0)
    if int(preview.get("real_conflict_count") or 0) > 0:
        blockers.append("real target conflicts exist")
    if preview_import_count < 1:
        blockers.append("no safe verified tracks selected for import")
    if selected_files and preview_import_count != len(selected_files):
        blockers.append("selected file count does not match target preview")
    warnings.extend(_s(reason) for reason in (preview.get("warnings") or []))

    blockers = list(dict.fromkeys(reason for reason in blockers if reason))
    warnings = list(dict.fromkeys(reason for reason in warnings if reason))
    eligible = not blockers
    idempotency_key = _import_review_auto_key(
        payload, selected_files, release_group_id, representative_release_id
    ) if eligible else ""
    return {
        "eligible": eligible,
        "review_item_id": review_item_id,
        "confidence": confidence,
        "identity_verified": bool(release_group_id and representative_release_id),
        "release_group_verified": bool(release_group_id),
        "release_group_id": release_group_id,
        "representative_release_id": representative_release_id,
        "selected_file_paths": [str(path) for path in selected_files],
        "selected_track_count": len(selected_files),
        "unmatched_file_count": int(preview.get("unmatched_extra_count") or 0),
        "missing_release_track_count": int(preview.get("missing_album_track_count") or 0),
        "real_conflict_count": int(preview.get("real_conflict_count") or 0),
        "blocking_reasons": blockers,
        "informational_warnings": warnings,
        "target_preview": preview,
        "idempotency_key": idempotency_key,
    }


# Service behind POST /api/folders/import-with-id (ARCH-001): request-free,
# returns (json_body, http_status); the route and in-process callers share it.
def start_folder_import_with_id(payload_in: Dict[str, Any]) -> Tuple[Any, int]:
    """Two-step import for a skipped folder:
      1. beet import --quiet-fallback asis  (always succeeds, gets files into library)
      2. beet modify + mbsync + write + move  (applies the confirmed MB album ID)
    This bypasses the similarity threshold that causes --search-id to skip in quiet mode.
    Body: {
      "path": "/data/torrents/music/...",
      "mb_albumid": "representative-release-uuid",
      "mb_releasegroupid": "canonical-release-group-uuid"
    }
    """
    payload    = payload_in
    folder_path   = payload.get("path",  "").strip()
    raw_mb_albumid = _s(payload.get("mb_albumid") or payload.get("mbid")).strip()
    raw_mb_releasegroupid = _s(
        payload.get("mb_releasegroupid")
        or payload.get("release_group")
        or payload.get("release_group_id")
    ).strip()
    raw_mb_input = raw_mb_albumid or raw_mb_releasegroupid
    mb_albumid = _extract_mb_uuid(raw_mb_input)
    selected_releasegroupid = _extract_mb_uuid(raw_mb_releasegroupid)
    if not selected_releasegroupid and "release-group" in raw_mb_input.casefold():
        selected_releasegroupid = mb_albumid
    # Wave 25 Docker acceptance round: mb_albumid currently holds a
    # release-group UUID rather than a genuine, directly-importable release
    # UUID exactly when selected_releasegroupid ended up equal to it -- either
    # because the caller supplied mb_releasegroupid explicitly with no
    # separate release id (mb_albumid fell back to the release-group value
    # above), or because a release-group URL/ID was pasted into the generic
    # release-id field and detected by the check just above. In both cases
    # the value must be resolved to a representative release before beet
    # import can use it. This flag was referenced below but never assigned,
    # so every real fresh-import call raised NameError -- only surfaced once
    # the real two-service Docker acceptance run exercised this production
    # route end to end.
    input_looks_like_release_group = (
        bool(selected_releasegroupid) and selected_releasegroupid == mb_albumid
    )
    ai_suggestion = payload.get("ai_suggestion")   # optional — records in AI match history
    use_move      = bool(payload.get("move", False))
    # Wave 25 Round 4: test-only pass-through for the dedicated Docker
    # acceptance crash/resume scenario. Inert in every real deployment --
    # the engine only honors it when its OWN container was booted with
    # BEETS_ACCEPTANCE_MODE=1 (see beets_control_agent.ACCEPTANCE_MODE),
    # which no production compose file ever sets, regardless of what any
    # caller of this route sends.
    acceptance_failpoint = _s(payload.get("_acceptance_failpoint")).strip() or None
    try:
        existing_album_id = int(payload.get("existing_album_id") or payload.get("album_id") or 0)
    except Exception:
        existing_album_id = 0
    queue_review_on_uncertain = payload.get("queue_review", True) is not False
    light_confirm = bool(payload.get("light_confirm"))
    auto_import = bool(payload.get("auto_import"))
    review_item_id = _s(payload.get("review_item_id")).strip()
    auto_import_idempotency_key = _s(payload.get("auto_import_idempotency_key")).strip()
    trigger_plex_refresh_after = bool(payload.get("trigger_plex"))
    trigger_plex_context = _s(payload.get("trigger_plex_context") or payload.get("plex_context") or payload.get("workflow") or "").strip().casefold()
    wanted_tracks = _normalise_wanted_tracks(
        payload.get("wanted_tracks") or payload.get("missing_tracks") or [])
    replace_existing_item_ids = []
    for raw_id in (payload.get("replace_existing_item_ids") or []):
        try:
            item_id = int(raw_id or 0)
            if item_id > 0:
                replace_existing_item_ids.append(item_id)
        except Exception:
            pass
    if not folder_path:
        return {"ok": False, "error": "path required"}, 400
    # Wave 25 Docker acceptance round (found only by actually exercising
    # this real production route against the documented two-service
    # deployment): requiring local existence made _resolve_import_review_source_path
    # call candidate.exists()/is_symlink() against a path this process (the
    # web-manager container) has no filesystem visibility into at all --
    # docker-compose.yml/docker-compose.full.yml mount ONLY
    # /web-manager-data into beets-web-manager, by design (the engine
    # container owns /data/media/music and /data/torrents). Every real
    # import-with-id call was therefore guaranteed to fail with "Source
    # path does not exist", 404, regardless of whether the folder was
    # genuinely there -- the frontend calls this route directly
    # (api/client.ts), so this broke the confirm-and-import workflow
    # entirely in the real, security-hardened deployment topology, not
    # just some rare edge case. Root-containment (a pure string
    # comparison, no filesystem access) is still checked here and remains
    # a real, fast, meaningful rejection of an out-of-bounds path.
    # Existence and symlink-safety are the engine's job -- it has the
    # actual mount -- and create_import_folder_plan() below already does
    # real, disk-backed root-containment + symlink-component checks with
    # a real fail-closed existence requirement of its own.
    trusted_folder, folder_error = _resolve_import_review_source_path(
        folder_path,
        allow_music=True,
        expected_type="dir",
        require_exists=False,
    )
    if folder_error or trusted_folder is None:
        return {"ok": False, "error": folder_error or "Source path is not allowed."}, 400
    folder_path = str(trusted_folder)
    if not raw_mb_input:
        return {
            "ok": False,
            "error": "MusicBrainz release-group ID or representative release ID required",
        }, 400
    if not mb_albumid:
        return {
            "ok": False,
            "error": "Enter a valid MusicBrainz release-group ID or release ID.",
        }, 400

    raw_track_mapping = payload.get("track_mapping") if isinstance(payload.get("track_mapping"), list) else []
    selected_source_files = _import_review_selected_source_files(
        folder_path,
        payload.get("selected_source_files") if isinstance(payload.get("selected_source_files"), list) else [],
        raw_track_mapping,
    )
    if raw_track_mapping and any(
        _s((row if isinstance(row, dict) else {}).get("status")).strip().lower()
        in _IMPORT_REVIEW_IMPORTABLE_STATUSES
        for row in raw_track_mapping
    ) and not selected_source_files:
        return {"ok": False, "error": "No verified selected source files were found for import."}, 400
    selected_subset_import = bool(selected_source_files)
    preserve_torrent_source = _preserve_torrent_source_path(folder_path)
    if not existing_album_id:
        try:
            existing_ids = _library_album_ids_for_folder(folder_path)
            if len(existing_ids) == 1:
                existing_album_id = int(existing_ids[0])
        except Exception:
            existing_album_id = 0

    def _do(log, cancel_event=None):
        nonlocal mb_albumid, selected_releasegroupid
        music_root = str(MUSIC_ROOT)
        source_folder_path = folder_path
        import_folder_path = folder_path
        # Wave 25 Docker acceptance round (second NameError found by the same
        # real fresh-import scenario, after fixing input_looks_like_release_group
        # above): source_is_library is referenced extensively below (library-
        # source validation, rollback, and messaging branches) but was never
        # assigned anywhere in this function either. True when the caller is
        # re-tagging/re-validating a folder that is already inside the music
        # library, as opposed to importing from staging/downloads.
        source_is_library = _path_is_under(Path(folder_path), Path(music_root))
        # Wave 25 Docker acceptance round (third NameError found by the same
        # real fresh-import scenario): already_present/combined are
        # referenced much further below by a legacy "beet reported nothing
        # to import" phrase-matching fallback (_delete_if_already_in_library
        # searches raw beet CLI stdout for phrases like "already in the
        # library"). That mechanism predates the import_folder_v1 engine
        # migration -- the controlled composite_workflows.plan_import_folder /
        # apply_import_folder path no longer exposes raw beet stdout to
        # app.py at all, so there is no text left to phrase-match, and
        # fabricating a signal here would be dishonest. Defaulting both
        # False/"" keeps that specific legacy fallback inert rather than
        # crashing; a genuinely already-imported source is still found by
        # the real library lookups (strategies A-G below, which query by MB
        # identity/path/name), and the one real remaining gap -- a source
        # where none of those find a match because nothing was ever
        # imported -- is now an honest error instead of a NameError.
        already_present = False
        combined = ""

        if input_looks_like_release_group:
            resolved_release = _resolve_album_release_for_import(
                mb_albumid,
                Path(folder_path).parent.name,
                Path(folder_path).name,
                "",
                len(wanted_tracks),
                log,
                source_folder=folder_path,
                existing_album_id=existing_album_id,
            )
            if not resolved_release:
                raise RuntimeError(
                    "The selected MusicBrainz release-group ID did not resolve "
                    "to an importable representative release. Import was not started."
                )
            log.append(
                "[import] Using representative MusicBrainz release "
                f"{resolved_release} for selected release group {selected_releasegroupid}."
            )
            mb_albumid = resolved_release

        def _find_ids_in_db(path_prefix: str, since: float = 0.0):
            """Return (album_ids, item_ids) from beets SQLite via composite_workflows."""
            try:
                res = composite_workflows.resolve_folder_to_albums(path_prefix, since=since if since else None)
                return res.get("album_ids", []), res.get("item_ids", [])
            except Exception as ex:
                log.append(f"  DB query warning: {ex}")
                return [], []

        def _album_match_summary(album_db_id: int) -> Dict[str, Any]:
            """Compare imported item titles to the requested MB release before retagging."""
            mb = _fetch_mb_release_tracklist(mb_albumid, log)
            if not mb.get("ok"):
                raise RuntimeError(mb.get("error") or "MusicBrainz release lookup failed")
            mb_tracks = mb.get("tracks") or []
            try:
                raw_items = composite_workflows.find_all_items_by_album_id(album_db_id)
                rows = sorted(
                    raw_items,
                    key=lambda it: (
                        int(it.get("disc") or 1),
                        int(it.get("track") or 0),
                        _s(it.get("title") or ""),
                        int(it.get("id") or 0),
                    ),
                )
                album_row = composite_workflows.get_album(album_db_id)
            except Exception as ex:
                raise RuntimeError(f"Could not validate imported album: {ex}")

            matched_indices: set = set()
            unmatched = 0
            duplicate_matches = 0
            best_lines: List[str] = []
            imported_audio_paths: List[str] = []
            for row in rows:
                raw_path = _s(row.get("path"))
                file_name = Path(raw_path).name if raw_path else ""
                if raw_path:
                    fpath_for_fp = Path(raw_path)
                    if not fpath_for_fp.is_absolute():
                        fpath_for_fp = Path(music_root) / raw_path
                    if fpath_for_fp.exists():
                        imported_audio_paths.append(str(fpath_for_fp))
                item = {
                    "id": int(row.get("id") or 0),
                    "title": _s(row.get("title")),
                    "track": int(row.get("track") or 0),
                    "disc": int(row.get("disc") or 1),
                    "path": raw_path,
                    "mb_trackid": _s(row.get("mb_trackid")).strip().lower(),
                    "length": float(row.get("length") or 0),
                }
                best = _best_album_track_match(item, mb_tracks)
                idx = int(best.get("idx", -1))
                score = float(best.get("score") or 0.0)
                title_score = float(best.get("title_score") or 0.0)
                fp = _album_track_fingerprint_check(item, mb_tracks)
                if fp.get("status") == AcoustIDStatus.CONFLICT:
                    unmatched += 1
                elif (
                    (best.get("exact_mbid") and title_score >= _MB_TRACK_REPAIR_MATCH_THRESHOLD)
                    or (not best.get("exact_mbid") and score >= _MB_TRACK_PREFLIGHT_MATCH_THRESHOLD)
                ):
                    if idx >= 0 and idx not in matched_indices:
                        matched_indices.add(idx)
                    else:
                        duplicate_matches += 1
                        unmatched += 1
                else:
                    unmatched += 1
                if len(best_lines) < 4:
                    mbt = best.get("track") or {}
                    mb_disc = int(mbt.get("disc") or 1)
                    mb_track = int(mbt.get("track") or 0)
                    mb_pos = f"d{mb_disc}t{mb_track:02d}" if mb_disc > 1 else f"{mb_track:02d}"
                    file_part = f"file={file_name!r} " if file_name else ""
                    best_lines.append(
                        f"    {file_part}current={item['title']!r} -> "
                        f"selected MB #{mb_pos} {mbt.get('title','?')!r} ({score:.0%})"
                    )
            expected = len(mb_tracks)
            actual = len(rows)
            matches = len(matched_indices)
            acoustid_release_hits: Dict[str, int] = {}
            if imported_audio_paths:
                try:
                    acoustid_release_hits = _acoustid_multi_file(imported_audio_paths)
                except Exception as ex:
                    log.append(f"  Validation AcoustID warning: {ex}")
            target_mbid = _s(mb_albumid).strip().lower()
            acoustid_target_hits = 0
            acoustid_top_release = ""
            acoustid_top_hits = 0
            if acoustid_release_hits:
                acoustid_target_hits = sum(
                    int(hits or 0)
                    for rid, hits in acoustid_release_hits.items()
                    if _s(rid).strip().lower() == target_mbid
                )
                acoustid_top_release, acoustid_top_hits = max(
                    acoustid_release_hits.items(),
                    key=lambda item: int(item[1] or 0),
            )
            min_required = max(1, min(expected or actual or 1, int((expected or actual or 1) * 0.60)))
            max_allowed = (expected + max(1, expected // 4)) if expected else actual
            extra_unmatched = max(0, unmatched, actual - expected)
            clean_partial_import = bool(
                expected
                and actual < expected
                and (actual >= min(6, expected) or (actual <= 3 and matches >= actual))
                and matches >= max(1, int(math.ceil(actual * 0.90)))
                and unmatched == 0
                and duplicate_matches == 0
            )
            selected_subset_ok = bool(
                selected_subset_import
                and actual > 0
                and matches >= max(1, int(math.ceil(actual * 0.90)))
                and unmatched == 0
                and duplicate_matches == 0
            )
            acoustid_mismatch = bool(
                target_mbid
                and acoustid_top_release
                and _s(acoustid_top_release).strip().lower() != target_mbid
                and int(acoustid_target_hits or 0) == 0
            )
            ok = matches >= min_required or clean_partial_import or selected_subset_ok
            if expected and actual > expected and (unmatched or duplicate_matches):
                ok = False
            if expected and actual > max_allowed:
                ok = False
            if acoustid_mismatch:
                ok = False
            album_name = _s(album_row["album"]) if album_row else ""
            albumartist = _s(album_row["albumartist"]) if album_row else ""
            return {
                "ok": ok,
                "matches": matches,
                "unmatched": unmatched,
                "duplicate_matches": duplicate_matches,
                "extra_unmatched": extra_unmatched,
                "expected": expected,
                "actual": actual,
                "min_required": min_required,
                "max_allowed": max_allowed,
                "clean_partial_import": clean_partial_import,
                "selected_subset_import": selected_subset_ok,
                "missing_expected_tracks": max(0, expected - actual),
                "album": album_name,
                "albumartist": albumartist,
                "release_title": mb.get("release_title", ""),
                "release_artist": mb.get("release_artist", ""),
                "acoustid_release_hits": acoustid_release_hits,
                "acoustid_target_hits": int(acoustid_target_hits or 0),
                "acoustid_top_release": acoustid_top_release,
                "acoustid_top_hits": int(acoustid_top_hits or 0),
                "acoustid_mismatch": acoustid_mismatch,
                "examples": best_lines,
            }

        def _cleanup_failed_import_copy(album_db_id: int) -> None:
            """Remove a failed copy-mode import only when the original source still exists.

            Wave 25 round (independent review): this previously deleted each
            item's file via a generic, DB-unaware engine delete-file call
            BEFORE calling plan_album_cleanup/apply_album_cleanup -- but
            album_cleanup_v1's own Apply already deletes both the DB rows
            AND the on-disk files itself (symlink-checked, TOCTOU-
            precondition-rechecked, step-tracked), per its own contract
            ("Create transaction plan for deleting an album from Beets DB
            and disk"). The manual pre-delete loop was therefore redundant
            AND bypassed that transaction's own safety checks for the file
            half of the deletion (the files were gone before Apply's own
            symlink/TOCTOU checks ever ran on them). Let the existing
            transaction do the whole job instead of duplicating it unsafely.
            """
            if source_is_library:
                log.append("  Failed import cleanup skipped: source is already under the music library")
                return
            if use_move or not Path(folder_path).exists():
                log.append("  Failed import cleanup skipped: source folder is not safely preserved")
                return
            try:
                rows = composite_workflows.find_all_items_by_album_id(album_db_id)
                if not rows:
                    return
                stale = [
                    r for r in rows
                    if not r.get("added") or float(r.get("added") or 0) < t_before
                ]
                if stale:
                    log.append(
                        "  Failed import cleanup skipped: album rows were not created by this job")
                    return
                # LT-17: row-only. The imported files are never deleted here;
                # they stay where Beets put them (untracked) for review.
                app_res = composite_workflows.remove_album_rows_after_failed_import(
                    album_db_id, reason="failed import validation (copied import)")
                if not app_res.get("ok"):
                    raise RuntimeError(app_res.get("error") or f"Row removal failed for album {album_db_id}")
                log.append(f"  Removed failed import's Beets rows: album_id {album_db_id}; "
                           "its files were kept on disk (untracked) for review")
            except Exception as ex:
                log.append(f"  Failed import cleanup warning: {ex}")

        def _rollback_failed_library_source_import(album_db_id: int) -> bool:
            """Undo DB rows created by a failed validation of a library-source folder."""
            if not source_is_library or not album_db_id:
                return False
            source_root = Path(folder_path).resolve(strict=False)
            try:
                rows = composite_workflows.find_all_items_by_album_id(album_db_id)
                if not rows:
                    return False
                for row in rows:
                    try:
                        added = float(row.get("added") or 0)
                    except Exception:
                        added = 0.0
                    if added < t_before:
                        log.append(
                            "  Failed library import DB rollback skipped: "
                            f"album_id {album_db_id} has older item rows"
                        )
                        return False
                    raw_path = _s(row.get("path"))
                    fpath = Path(raw_path)
                    if not fpath.is_absolute():
                        fpath = Path(music_root) / raw_path
                    try:
                        fpath.resolve(strict=False).relative_to(source_root)
                    except Exception:
                        log.append(
                            "  Failed library import DB rollback skipped: "
                            f"item path is outside source folder ({fpath})"
                        )
                        return False
                # LT-17: these are the operator's own library files -- remove
                # the rows this failed validation created, never the files.
                app_res = composite_workflows.remove_album_rows_after_failed_import(
                    album_db_id, reason="failed library-source import validation")
                if not app_res.get("ok"):
                    raise RuntimeError(f"Row removal failed for album {album_db_id}: {app_res.get('error')}")
                log.append(
                    "  Rolled back failed library-source import DB rows for "
                    f"album_id {album_db_id}; source files were kept on disk"
                )
                return True
            except Exception as ex:
                log.append(f"  Failed library import DB rollback warning: {ex}")
            return False

        def _queue_failed_library_source_review(summary: Dict[str, Any]) -> bool:
            release_name = " - ".join(
                v for v in (
                    _s(summary.get("release_artist", "")).strip(),
                    _s(summary.get("release_title", "")).strip(),
                ) if v
            )
            reason = (
                "Selected MusicBrainz release did not match this library folder "
                f"({int(summary.get('matches') or 0)}/"
                f"{int(summary.get('expected') or summary.get('actual') or 0)} "
                f"track(s) matched, {int(summary.get('actual') or 0)} file(s) present). "
                "The failed Beets DB rows were rolled back; choose the correct release "
                "before importing or deleting the source folder."
            )
            if release_name:
                reason = f"Selected MusicBrainz release ({release_name}) did not match this library folder " + reason.split("this library folder ", 1)[1]
            preflight = {
                "ok": False,
                "matches": int(summary.get("matches") or 0),
                "expected": int(summary.get("expected") or 0),
                "audio_count": int(summary.get("actual") or 0),
                "min_required": int(summary.get("min_required") or 0),
                "release_title": summary.get("release_title", ""),
                "release_artist": summary.get("release_artist", ""),
                "acoustid_mismatch": bool(summary.get("acoustid_mismatch")),
                "acoustid_target_hits": int(summary.get("acoustid_target_hits") or 0),
                "acoustid_top_release": summary.get("acoustid_top_release", ""),
                "acoustid_top_hits": int(summary.get("acoustid_top_hits") or 0),
                "examples": summary.get("examples") or [],
            }
            sug = dict(ai_suggestion or {})
            sug.update({
                "mb_albumid": mb_albumid,
                "mb_url": f"https://musicbrainz.org/release/{mb_albumid}",
                "mb_releasegroupid": selected_releasegroupid,
                "mb_releasegroup_url": (
                    f"https://musicbrainz.org/release-group/{selected_releasegroupid}"
                    if selected_releasegroupid else ""
                ),
                "mb_valid": bool(_MB_UUID_RE.match(_s(mb_albumid).strip().lower())),
                "confidence": "low",
                "albumartist": summary.get("albumartist") or Path(folder_path).parent.name,
                "album": summary.get("album") or Path(folder_path).name,
                "reason": reason,
            })
            evidence = _ai_match_evidence_packet(
                "light_confirm",
                folder_path=folder_path,
                suggestion=sug,
                folder_evidence=_build_folder_evidence(folder_path),
                preflight=preflight,
                wanted_tracks=wanted_tracks,
                reason=reason,
            )
            return _queue_folder_for_manual_review(
                folder_path, sug, reason, log,
                allow_existing=True, evidence=evidence,
            )

        def _handoff_existing_album_import(candidate_album_ids: List[int]) -> bool:
            """Handle Beets album-duplicate skips as an existing-album missing-track import."""
            target_album_id = existing_album_id or (int(candidate_album_ids[0]) if candidate_album_ids else 0)
            if not target_album_id:
                return False

            def _review_payload(reason: str, scan_result: Optional[Dict[str, Any]] = None) -> tuple:
                sug = dict(ai_suggestion or {})
                sug.update({
                    "mb_albumid": mb_albumid,
                    "mb_url": f"https://musicbrainz.org/release/{mb_albumid}",
                    "mb_releasegroupid": selected_releasegroupid,
                    "mb_releasegroup_url": (
                        f"https://musicbrainz.org/release-group/{selected_releasegroupid}"
                        if selected_releasegroupid else ""
                    ),
                    "mb_valid": bool(_MB_UUID_RE.match(_s(mb_albumid).strip().lower())),
                    "confidence": "low",
                    "albumartist": sug.get("albumartist") or Path(folder_path).parent.name,
                    "album": sug.get("album") or Path(folder_path).name,
                    "reason": reason,
                })
                pre = None
                try:
                    pre = _folder_release_preflight(
                        folder_path,
                        mb_albumid,
                        existing_album_id=target_album_id,
                        log=None,
                    )
                except Exception:
                    pass
                evidence = _ai_match_evidence_packet(
                    "missing_track" if (scan_result or {}).get("wanted_tracks") or wanted_tracks else "light_confirm",
                    folder_path=folder_path,
                    suggestion=sug,
                    folder_evidence=_build_folder_evidence(folder_path),
                    preflight=pre,
                    wanted_tracks=(scan_result or {}).get("wanted_tracks") or wanted_tracks,
                    reason=reason,
                )
                return sug, evidence

            scan = _source_audio_missing_track_scan(folder_path, target_album_id, mb_albumid, log)
            if not scan.get("ok"):
                return False
            log.append(
                "  Existing album source scan: "
                f"{len(scan.get('useful_files') or [])} still-missing, "
                f"{len(scan.get('duplicate_files') or [])} already-present, "
                f"{len(scan.get('unknown_files') or [])} unknown"
            )
            if source_is_library and scan.get("unknown_files"):
                reason = (
                    f"Selected MusicBrainz release did not match this library folder "
                    f"({len(scan.get('unknown_files') or [])} file(s) did not map cleanly). "
                    "Choose the correct edition/release before tagging."
                )
                review_sug, review_evidence = _review_payload(reason, scan)
                if _queue_folder_for_manual_review(
                        folder_path, review_sug, reason, log,
                        allow_existing=True, evidence=review_evidence):
                    log.append("[import] Library folder needs manual release review; no files were changed.")
                else:
                    log.append("[import] Existing library album skipped; no pending item was added.")
                return True
            useful_files = [Path(p) for p in (scan.get("useful_files") or [])]
            if useful_files:
                staged_dir = _stage_selected_audio_files(
                    folder_path,
                    useful_files,
                    scan.get("artist") or Path(folder_path).parent.name,
                    scan.get("album") or Path(folder_path).name,
                    log,
                    force_stage=source_is_library,
                    target_tracks=_normalise_wanted_tracks(
                        scan.get("wanted_tracks") or wanted_tracks
                    ),
                )
                if source_is_library and staged_dir == folder_path:
                    raise RuntimeError(
                        "Could not stage the still-missing track subset; "
                        "refusing to import the existing library folder."
                    )
                handoff_tracks = _normalise_wanted_tracks(scan.get("wanted_tracks") or wanted_tracks)
                log.append(
                    f"[import] Existing album detected; importing {len(useful_files)} "
                    f"still-missing track(s) into album_id {target_album_id}"
                )
                child_job_id = _start_reimport_disk_job_internal(
                    staged_dir,
                    mb_albumid,
                    albumartist=scan.get("artist") or "",
                    existing_album_id=target_album_id,
                    wanted_tracks=handoff_tracks,
                    skip_import_lock=True,
                )
                log.append(f"[import] Missing-track import job {child_job_id} started; waiting for completion…")
                _wait_for_child_job(child_job_id, log, cancel_event, prefix="import", timeout=1200)
                if not scan.get("unknown_files"):
                    _delete_if_already_in_library(folder_path, "already in library", log)
                log.append("[import] Existing album merge complete.")
                return True

            if scan.get("audio_count") and not scan.get("unknown_files"):
                if int(scan.get("missing_count") or 0):
                    log.append(
                        "[import] Source folder did not contain any tracks that are still "
                        "missing from this MusicBrainz release."
                    )
                else:
                    log.append("[import] Existing album is already complete for this MusicBrainz release.")
                _delete_if_already_in_library(folder_path, "already in library", log)
                return True

            if selected_subset_import and not useful_files and scan.get("duplicate_files"):
                # unknown_files counts ANY file in the source folder that
                # wasn't selected for this job — completely unrelated to what
                # was actually requested. Don't let unrelated files block
                # recognizing that the selected file(s) are already present
                # (they showed up as duplicate_files, not useful_files).
                # Unlike the "fully complete" branch above, do not delete the
                # folder here: the unrelated files may still need review.
                log.append(
                    "[import] Selected file(s) are already present in the library; "
                    "unrelated files remain in the source folder for review."
                )
                return True

            if source_is_library:
                reason = (
                    "Selected MusicBrainz release did not match the current library track list. "
                    "Choose the correct edition/release before tagging."
                )
                review_sug, review_evidence = _review_payload(reason, scan)
                if _queue_folder_for_manual_review(
                        folder_path, review_sug, reason, log,
                        allow_existing=True, evidence=review_evidence):
                    log.append("[import] Library folder needs manual release review; no files were changed.")
                else:
                    log.append("[import] Existing library album skipped; no pending item was added.")
                return True

            return False

        if source_is_library:
            candidate_album_ids = _library_album_ids_for_folder(folder_path)
            if candidate_album_ids:
                try:
                    pre = _folder_release_preflight(
                        folder_path,
                        mb_albumid,
                        existing_album_id=int(candidate_album_ids[0]),
                        log=log,
                    )
                except Exception as ex:
                    pre = {"ok": False, "error": str(ex), "matches": 0, "expected": 0}
                log.append(
                    "[import] Library source preflight before Beets import: "
                    f"{pre.get('matches', 0)}/{pre.get('expected', 0)} "
                    "track(s) matched selected MusicBrainz release"
                )
                if not pre.get("ok"):
                    if _handoff_existing_album_import([int(aid) for aid in candidate_album_ids]):
                        _invalidate_lib_cache()
                        return
                    raise RuntimeError(
                        "Selected MusicBrainz release does not match this library folder; "
                        "refusing to run beet import against existing library files."
                    )

        # ── Step 1: import with specific MB release ID + permissive threshold ──────────

        if preserve_torrent_source and use_move:
            log.append(
                "[torrent] Move requested, but source is under a protected "
                "torrent root; using --copy so qBittorrent source files remain."
            )
        import_mode = "--move" if selected_subset_import else ("--copy" if preserve_torrent_source or not use_move else "--move")
        mb_albumid = _prefer_album_mb_release(mb_albumid, log)
        mb_identity = _fetch_mb_release_tracklist(mb_albumid, log)
        if not mb_identity.get("ok"):
            raise RuntimeError(
                "The selected MusicBrainz release could not be loaded. "
                "Import was not started."
            )
        resolved_releasegroupid = _s(mb_identity.get("release_group") or "").strip().lower()
        if selected_releasegroupid and resolved_releasegroupid and selected_releasegroupid != resolved_releasegroupid:
            raise RuntimeError(
                "The representative MusicBrainz release is not in the selected "
                "release group. Import was blocked before files were moved or tagged."
            )
        if resolved_releasegroupid and not selected_releasegroupid:
            selected_releasegroupid = resolved_releasegroupid
        if selected_releasegroupid:
            log.append(f"[import] Canonical MusicBrainz release-group ID: {selected_releasegroupid}")
        if selected_subset_import:
            active_selected_source_files = _filter_import_review_selected_audio_files(selected_source_files, log)
            log.append("  [audio] Selected partial-import files passed pre-stage audio validation.")
            import_folder_path = _stage_selected_audio_files(
                folder_path,
                active_selected_source_files,
                _s(mb_identity.get("artist")),
                _s(mb_identity.get("title")),
                log,
                target_tracks=wanted_tracks,
            )
        else:
            _validate_import_source_audio(folder_path, log, reject_downloads=True)
        log.append(f"[1/4] Importing '{Path(folder_path).name}' with MB ID {mb_albumid}…")
        t_before = time.time() - 5
        import_timeout = _beet_import_timeout(import_folder_path)

        # Wave 25 Round 3: routed through confirmed_import_v1, NOT
        # import_folder_v1/reimport_source_atomic. This workflow's whole
        # premise is a human reviewing and confirming an identity for
        # PREVIOUSLY-UNTAGGED source audio -- reimport_source_atomic's
        # verify_deterministic_identity() gate requires the source's OWN
        # embedded MusicBrainz tags to already match the target, which
        # fresh untagged downloads never have by construction (that gate
        # remains untouched and still correctly protects genuine reimports
        # of already-tagged library content via BeetsClient.reimport_source
        # / POST /imports/reimport). confirmed_import_v1 instead binds
        # authorization to an immutable source manifest digest (re-checked
        # at Apply), this already-resolved concrete Release ID + Release
        # Group, and best-effort track/fingerprint alignment.
        plan_res = composite_workflows.plan_confirmed_import({
            "source_folder": import_folder_path,
            "existing_album_id": existing_album_id,
            "mb_albumid": mb_albumid,
            "mb_releasegroupid": selected_releasegroupid,
            "mb_release_group_resolved": resolved_releasegroupid,
            "mb_tracks": mb_identity.get("tracks") or [],
            "use_move": use_move,
        })
        if not plan_res.get("ok"):
            raise RuntimeError(f"Import planning failed: {plan_res.get('error') or 'unknown error'}")
        # Round 4 (found while adding this very diagnostics feature):
        # BeetsClient._request() raises BeetsError/BeetsUnavailableError
        # for every non-2xx response (standard urllib behavior) -- it
        # never returns a plain {"ok": False, ...} dict for those, so a
        # `if not apply_res.get("ok")` check here would be dead code for
        # every real rejection this route can produce (Plan/Apply both
        # respond 400/409 on failure, never 200 with ok:false). The
        # exception itself is the only thing that ever reaches this line,
        # and its own .diagnostics attribute (populated by _request() from
        # the agent's error JSON) is what carries the real native Beets
        # stdout/stderr -- surfaced into this job's own log here so it is
        # visible to whoever is debugging a real failure, including the
        # Docker acceptance script's own [FAIL] report.
        try:
            apply_res = composite_workflows.apply_confirmed_import(
                plan_res["operation_id"], acceptance_failpoint=acceptance_failpoint,
            )
        except (BeetsError, BeetsUnavailableError) as ex:
            diag = getattr(ex, "diagnostics", None) or {}
            if diag.get("returncode") is not None:
                log.append(f"[import] Native Beets exit code: {diag['returncode']}")
            if diag.get("stdout_excerpt"):
                log.append(f"[import] Native Beets stdout: {diag['stdout_excerpt']}")
            if diag.get("stderr_excerpt"):
                log.append(f"[import] Native Beets stderr: {diag['stderr_excerpt']}")
            raise
        log.append(f"[import] Engine controlled import completed: {import_folder_path}")
        if apply_res.get("resumed"):
            log.append("[import] Resumed an already-verified prior result for this release (native import was not re-invoked).")
        confirmed_import_album_id = int(apply_res.get("album_id") or 0)
        confirmed_import_item_ids = [int(i) for i in (apply_res.get("item_ids") or [])]

        # ── Step 2: find the album ─────────────────────────────────────────────
        log.append("[2/4] Locating album in library…")
        time.sleep(1)

        # confirmed_import_v1's Apply already performed authoritative,
        # verified result capture (queried the library by the exact planned
        # Release ID, confirmed items and files exist on disk) -- trust
        # that directly rather than re-discovering it through the broad
        # heuristic strategies below, which exist to cover cases this
        # deterministic result does not (kept as a defensive fallback, not
        # the primary path, now that a real verified result is available).
        if confirmed_import_album_id > 0:
            album_ids, item_ids = [confirmed_import_album_id], list(confirmed_import_item_ids)
            strategy = "confirmed_import_v1 verified result"
        else:
            album_ids, item_ids = [], []
            strategy = ""

        # A: items still in the import source folder
        if not album_ids and not item_ids:
            album_ids, item_ids = _find_ids_in_db(import_folder_path)
            strategy = (
                "source path (library)" if source_is_library
                else "source path (selected subset)" if selected_subset_import
                else "source path (downloads)"
            )

        # B: items recently copied to the music library
        if not album_ids and not item_ids:
            album_ids, item_ids = _find_ids_in_db(music_root, since=t_before)
            strategy = "recently added to music library"

        # C: exact MusicBrainz identity lookup before broad artist-folder scans.
        if not album_ids and not item_ids:
            album_ids = _library_album_ids_for_musicbrainz(mb_albumid, selected_releasegroupid)
            if album_ids:
                strategy = "existing MusicBrainz release/release-group ID"
        # D: music root path candidates — try artist/album subfolders
        if not album_ids and not item_ids:
            folder_name = Path(folder_path).name
            artist_name  = Path(folder_path).parent.name
            for candidate in [
                f"{music_root}/{artist_name}/{folder_name}",
                f"{music_root}/{folder_name}",
                f"{music_root}/{artist_name}",
            ]:
                album_ids, item_ids = _find_ids_in_db(candidate)
                if album_ids or item_ids:
                    strategy = f"music path ({candidate})"
                    break

        # E: search by album TEXT field in SQLite (no path / time constraints)
        #    Handles "already in library" where beets renamed the folder
        album_guess  = re.sub(r'\s*[\(\[]\d{4}[\)\]]\s*$', '',
                              Path(folder_path).name).strip()
        album_guess = _restore_time_colon_title(album_guess)
        artist_guess = Path(folder_path).parent.name
        if not album_ids and not item_ids:
            try:
                found_items = composite_workflows.find_items_by_query(f"album:{album_guess}", limit=200)
                if not found_items and artist_guess.lower() not in {
                        "music","torrents","downloads","data","failed_imports"}:
                    found_items = composite_workflows.find_items_by_query(f"album:{album_guess} artist:{artist_guess}", limit=200)
                for row in found_items:
                    row_aid = row.get("album_id")
                    row_id = row.get("id")
                    if row_aid and row_aid not in album_ids:
                        album_ids.append(row_aid)
                    elif not row_aid and row_id not in item_ids:
                        item_ids.append(row_id)
                if album_ids or item_ids:
                    strategy = f"album name ({album_guess!r})"
            except Exception as ex:
                log.append(f"  Strategy H warning: {ex}")

        # F: search by the target mb_albumid itself (album was already correctly tagged)
        if not album_ids and not item_ids:
            try:
                _e_rows = composite_workflows.find_all_albums_by_mb_albumid(mb_albumid)
                for _row in _e_rows:
                    _aid = int(_row.get("id") or 0)
                    if _aid and _aid not in album_ids:
                        album_ids.append(_aid)
                if album_ids:
                    strategy = f"existing mb_albumid={mb_albumid[:8]}…"
            except Exception as ex:
                log.append(f"  Strategy F warning: {ex}")

        # G: LIKE fuzzy on album name (handles "(Taped Over)" suffix mismatches)
        if not album_ids and not item_ids:
            try:
                found_items = composite_workflows.find_items_by_query(f"album:{album_guess}", limit=200)
                if found_items and artist_guess.lower() not in {
                        "music","torrents","downloads","data","failed_imports","ye","kanye"}:
                    found_items = [r for r in found_items if r.get("album_id") is not None]
                for row in found_items:
                    row_aid = row.get("album_id")
                    row_id = row.get("id")
                    if row_aid and row_aid not in album_ids:
                        album_ids.append(row_aid)
                    elif not row_aid and row_id not in item_ids:
                        item_ids.append(row_id)
                if album_ids or item_ids:
                    strategy = f"fuzzy album name ({album_guess!r})"
            except Exception as ex:
                log.append(f"  Strategy G warning: {ex}")

        # H: albumartist search — last resort when album title was renamed by beets
        if not album_ids and not item_ids and already_present and \
                artist_guess.lower() not in {"music","torrents","downloads","data","failed_imports"}:
            try:
                found_items = composite_workflows.find_items_by_query(f"albumartist:{artist_guess}", limit=50)
                if not found_items:
                    found_items = composite_workflows.find_items_by_query(f"artist:{artist_guess}", limit=50)
                for row in found_items:
                    row_aid = row.get("album_id")
                    if row_aid and row_aid not in album_ids:
                        album_ids.append(row_aid)
                if album_ids:
                    strategy = f"albumartist ({artist_guess!r}) — pick correct album below"
                    # Narrow to most likely match if multiple albums exist
                    if len(album_ids) > 1:
                        try:
                            best, best_score = album_ids[0], 0
                            for aid in album_ids:
                                _ag_row = composite_workflows.get_album(aid)
                                _ag_name = (_ag_row.get("album") if _ag_row else "") or ""
                                from difflib import SequenceMatcher as _SM2
                                sc = _SM2(None, album_guess.lower(),
                                          _ag_name.lower()).ratio()
                                if sc > best_score:
                                    best_score, best = sc, aid
                            album_ids = [best]
                            strategy = f"albumartist+fuzzy ({artist_guess!r}/{album_guess!r})"
                        except Exception:
                            album_ids = album_ids[:1]
            except Exception as ex:
                log.append(f"  Strategy H warning: {ex}")

        if not album_ids and not item_ids:
            # Strategy I: when beet said "no files imported" / "already in library",
            # try locating the existing library album directly by the provided MB albumid.
            # This handles the case where the folder name doesn't match the stored album
            # name (e.g. "1999 - Californication" vs "Californication") or the files were
            # already moved to the library by a previous import.
            if already_present and mb_albumid:
                try:
                    _h_rows = composite_workflows.find_all_albums_by_mb_albumid(mb_albumid)
                    for _row in _h_rows:
                        _aid = int(_row.get("id") or 0)
                        if _aid and _aid not in album_ids:
                            album_ids.append(_aid)
                    if album_ids:
                        strategy = f"existing mb_albumid in library (Strategy I)"
                        log.append(f"  Strategy I: found album by mb_albumid in library")
                except Exception as _hex:
                    log.append(f"  Strategy I warning: {_hex}")

        if not album_ids and not item_ids:
            if already_present:
                # Nothing found anywhere, but beet confirmed the selected files are already present.
                _delete_if_already_in_library(folder_path, combined, log)
                _remove_pending_review_for_path(folder_path, log)
                log.append("Album already in library — source cleaned up.")
                return {"status": "already_in_library"}
            log.append("ERROR: could not find album in library after import. "
                       "The import may have silently failed. Check beet.log for details.")
            raise RuntimeError("could not find album in library after import")

        if album_ids:
            log.append(f"Found {len(album_ids)} album(s) via {strategy}: {album_ids}")
        else:
            log.append(f"Found {len(item_ids)} item(s) (no album grouping) via {strategy}")

        if album_ids:
            candidate_album_ids = [int(aid) for aid in album_ids]
            validated_album_ids: List[int] = []
            validation_errors: List[str] = []
            failed_library_source_summaries: List[Dict[str, Any]] = []
            rolled_back_failed_library_source = False
            for aid in album_ids:
                if strategy == "confirmed_import_v1 verified result":
                    # Already authoritatively verified server-side inside
                    # execute_confirmed_import_apply()'s post-import
                    # capture (exact planned Release ID + RGID match,
                    # items/files confirmed on disk) via structured
                    # library queries. Re-deriving that here through
                    # _album_match_summary() would be redundant at best --
                    # and structurally broken at worst, since it depends
                    # on raw SQL through _db(), which is intentionally
                    # disabled in the two-service topology
                    # (BeetsClient.raw_sqlite_query always raises "Raw
                    # SQLite queries are not permitted"). Trust the
                    # already-verified result directly instead of
                    # re-deriving it through a path that can never
                    # succeed here.
                    log.append(
                        f"  Album {aid} already verified by confirmed_import_v1 "
                        "(exact planned release match) — skipping redundant re-validation."
                    )
                    validated_album_ids.append(aid)
                    continue
                summary = _album_match_summary(int(aid))
                log.append(
                    f"  Validation album_id {aid}: {summary['matches']}/"
                    f"{summary['expected'] or summary['actual']} track(s) match requested MB release"
                    f" ({summary['actual']} file(s) in album)"
                )
                if summary.get("ok"):
                    if summary.get("clean_partial_import"):
                        log.append(
                            "  Clean partial import accepted: "
                            f"{summary['matches']}/{summary['actual']} imported source track(s) "
                            f"matched the {summary['expected']}-track MusicBrainz release. "
                            f"{summary.get('missing_expected_tracks', 0)} release track(s) remain missing."
                        )
                    validated_album_ids.append(aid)
                    continue
                if summary.get("extra_unmatched"):
                    log.append(
                        f"  Release mismatch: {summary['extra_unmatched']} extra file(s) "
                        "did not map to the selected MusicBrainz release"
                    )
                release_name = " - ".join(
                    v for v in (
                        _s(summary.get("release_artist", "")).strip(),
                        _s(summary.get("release_title", "")).strip(),
                    ) if v
                )
                if release_name:
                    log.append(f"  Selected MB release: {release_name}")
                log.append(
                    f"  Imported as: {summary.get('albumartist','')} - {summary.get('album','')}")
                if summary.get("acoustid_top_release"):
                    status = "mismatch" if summary.get("acoustid_mismatch") else "checked"
                    log.append(
                        f"  AcoustID {status}: selected release "
                        f"{summary.get('acoustid_target_hits', 0)} hit(s), "
                        f"top release {summary.get('acoustid_top_release')} "
                        f"{summary.get('acoustid_top_hits', 0)} hit(s)"
                    )
                if summary.get("examples"):
                    log.append("  Track comparison (current file/tag -> selected MB track):")
                for line in summary.get("examples") or []:
                    log.append(line)
                if source_is_library:
                    failed_library_source_summaries.append(summary)
                    rolled_back_failed_library_source = (
                        _rollback_failed_library_source_import(int(aid))
                        or rolled_back_failed_library_source
                    )
                else:
                    _cleanup_failed_import_copy(int(aid))
                validation_errors.append(
                    f"album_id {aid} matched {summary['matches']}/"
                    f"{summary['expected'] or summary['actual']} track(s), "
                    f"{summary['actual']} file(s) present"
                )
            album_ids = validated_album_ids
            if not album_ids and not item_ids:
                if source_is_library and rolled_back_failed_library_source and failed_library_source_summaries:
                    if _queue_failed_library_source_review(failed_library_source_summaries[0]):
                        _invalidate_lib_cache()
                        return {
                            "status": "queued_for_review",
                            "reason": "library_source_validation_failed",
                        }
                if (already_present or source_is_library) and _handoff_existing_album_import(candidate_album_ids):
                    _invalidate_lib_cache()
                    return
                raise RuntimeError(
                    "Imported files do not match the selected MusicBrainz release "
                    f"({'; '.join(validation_errors)})"
                )

        def _apply_verified_review_track_mapping(album_db_id: int) -> int:
            importable_rows = [
                row for row in raw_track_mapping
                if isinstance(row, dict)
                and _s(row.get("status")).strip().lower() in _IMPORT_REVIEW_IMPORTABLE_STATUSES
            ]
            if not (auto_import and selected_subset_import and importable_rows):
                return 0
            mapped_by_name: Dict[str, Dict[str, Any]] = {}
            for row in importable_rows:
                source_name = Path(_s(row.get("source_path")).strip()).name.casefold()
                if source_name:
                    mapped_by_name[source_name] = row
            if not mapped_by_name:
                return 0
            updated = 0
            try:
                item_rows = composite_workflows.find_all_items_by_album_id(album_db_id)
            except Exception as ex:
                log.append(f"  Verified review mapping warning: {ex}")
                return 0
            for item_row in item_rows:
                try:
                    item_name = Path(_s(item_row.get("path"))).name.casefold()
                    mapping = mapped_by_name.get(item_name)
                    if not mapping:
                        continue
                    track_num = int(mapping.get("num") or mapping.get("track") or 0)
                    disc_num = int(mapping.get("disc") or 1)
                    mb_trackid = _s(mapping.get("mb_trackid") or mapping.get("recording_id")).strip().lower()
                    title = _s(mapping.get("mb_title") or mapping.get("title") or mapping.get("local_title")).strip()
                    updates: Dict[str, Any] = {}
                    if track_num > 0:
                        updates["track"] = track_num
                    if disc_num > 0:
                        updates["disc"] = disc_num
                    if mb_trackid:
                        updates["mb_trackid"] = mb_trackid
                    if title:
                        updates["title"] = title
                    clean = {k: v for k, v in updates.items() if k in {"track", "disc", "mb_trackid", "title"}}
                    if not clean:
                        continue
                    try:
                        res = composite_workflows.update_item_metadata(
                            int(item_row["id"]),
                            clean,
                            force_write_tags=False,
                            write_tags=False,
                        )
                    except (BeetsUnavailableError, BeetsError) as ex:
                        log.append(f"  Verified review mapping engine warning: {ex}")
                        return updated
                    if not res.get("ok"):
                        log.append(f"  Verified review mapping engine warning: {res.get('error') or 'item metadata update rejected'}")
                        return updated
                    updated += int(res.get("item_fields_changed") or 0) or 1
                except Exception as ex:
                    # Regression guard: this loop used to run inside one
                    # broad try/except (raw SQL UPDATE path). Migrating to
                    # per-item engine calls must not let one malformed
                    # mapping row (bad int(), missing key, etc.) raise
                    # uncaught out of this helper and abort the whole
                    # import job -- skip the row and keep going.
                    log.append(f"  Verified review mapping warning for item {item_row['id']}: {ex}")
                    continue
            if updated:
                log.append(f"  Applied verified Import Review track mapping to {updated} item(s).")
            return updated

        def _retag(query_flag: str, label: str, album_db_id=None):
            """Stamp mb_albumid → fetch MB data → match tracks by title → write + move via engine transaction."""
            log.append(f"[3/4] Setting mb_albumid on {label} via engine transaction…")
            if album_db_id is not None:
                aid = int(album_db_id)
                try:
                    composite_workflows.update_album_metadata(aid, {"mb_albumid": mb_albumid}, release_selected_by_operator=True)
                except Exception as _mbe:
                    log.append(f"  update_album_metadata warning: {_mbe}")

                verified_mapping_count = _apply_verified_review_track_mapping(aid)
                importable_mapping_count = sum(
                    1 for row in raw_track_mapping
                    if isinstance(row, dict)
                    and _s(row.get("status")).strip().lower() in _IMPORT_REVIEW_IMPORTABLE_STATUSES
                )
                if verified_mapping_count and verified_mapping_count >= importable_mapping_count:
                    log.append("[3/4] Reused verified Import Review track mapping; skipped broad MB title rematch.")
                else:
                    log.append("[3/4] Matching tracks from MusicBrainz release data…")
                    matched = _match_tracks_from_mb(mb_albumid, aid, log)
                    log.append(f"  → {matched} track(s) matched and updated.")

                log.append("[3/4] Syncing album metadata from MusicBrainz via engine transaction…")
                try:
                    p_res = composite_workflows.plan_album_mb_track_repair({"album_id": aid})
                    if p_res.get("ok") and p_res.get("operation_id"):
                        composite_workflows.apply_album_mb_track_repair(p_res["operation_id"], write_tags=True)
                except Exception as _se:
                    log.append(f"  mbsync transaction warning: {_se}")

                cur_album = _strip_year_from_album_name(aid, log)

                log.append("[3/4] Writing tags to audio files via engine transaction…")
                try:
                    # Wave 24 final review section 30: force_write_tags
                    # requests a real Beets Item.write() resync -- an
                    # empty diff-based update here was a silent no-op.
                    res = composite_workflows.update_album_metadata(aid, {}, force_write_tags=True)
                    if not res.get("ok"):
                        log.append(f"  write tags warning: {res.get('error')}")
                except Exception as _we:
                    log.append(f"  write tags warning: {_we}")

                log.append("[4/4] Renaming files to match library path template via engine transaction…")
                try:
                    rel_res = composite_workflows.relocate_album(aid, mode="rename")
                    if rel_res.get("ok"):
                        log.append(f"  ✓ Relocated album {aid} to: {rel_res.get('dest_dir')}")
                except Exception as _me:
                    log.append(f"  relocate warning: {_me}")
                _cleanup_template_tokens_for_album(aid, log)

        # ── Steps 3 & 4: tag + move ────────────────────────────────────────────
        for aid in album_ids:
            _retag(f"album_id:{aid}", f"album {aid}", album_db_id=aid)

        for iid in item_ids:
            _retag(f"id:{iid}", f"item {iid}", album_db_id=None)

        partial_remaining_audio: List[Path] = []
        if selected_subset_import:
            removed_selected = 0
            music_root_path = Path(music_root).resolve(strict=False)
            for src in active_selected_source_files:
                try:
                    resolved_src = Path(src).resolve(strict=False)
                    try:
                        resolved_src.relative_to(music_root_path)
                        log.append(f"  [cleanup] Kept selected library source file: {resolved_src.name}")
                        continue
                    except Exception:
                        pass
                    composite_workflows.delete_file(str(resolved_src))
                    removed_selected += 1
                except Exception as ex:
                    log.append(f"  [cleanup] WARN could not remove imported source file {src}: {ex}")
            partial_remaining_audio = _remaining_audio_files(source_folder_path)
            if partial_remaining_audio:
                log.append(
                    f"Partial import complete. {len(partial_remaining_audio)} unmatched file(s) remain in review."
                )
            else:
                log.append(f"Partial import complete. Removed {removed_selected} imported source file(s).")
        # ── Move orphaned artwork to canonical album folder, then remove source ──
        # After beet move, audio files are at the library path.  Any image files
        # (cover.jpg, folder.jpg, Artwork/ …) left in the source folder are moved
        # to the same canonical folder.  The source is then removed only when it
        # contains no unknown (non-image) files.
        src_dir = Path(source_folder_path)
        try:
            if src_dir.is_dir():
                if selected_subset_import and partial_remaining_audio:
                    log.append(
                        f"  [cleanup] Source folder kept: {len(partial_remaining_audio)} unmatched audio file(s) awaiting review."
                    )
                elif already_present:
                    # Route through the safe helper — never deletes music-library paths
                    _delete_if_already_in_library(str(src_dir), combined, log)
                else:
                    # Move artwork to the canonical album folder before cleanup
                    _move_artwork_to_target(src_dir, list(album_ids), log)
                    # Walk remaining files; skip directories (already handled above)
                    remaining_files = [f for f in src_dir.rglob("*") if f.is_file()]
                    unknown = [f for f in remaining_files if f.suffix.lower() not in _ART_EXTS]
                    if unknown:
                        log.append(
                            f"  [cleanup] {len(unknown)} unknown file(s) in source — not removing: "
                            + ", ".join(f.name for f in unknown[:5])
                        )
                    else:
                        # Only residual art files (already moved) or empty — safe to remove.
                        # Wave 25 round (independent review): folder_cleanup_v1's Plan
                        # reads payload["source"]/["source_folder"] and
                        # action in {"remove_empty_source","remove_empty",
                        # "safe_rename","rename_folder","merge_source_files","merge"}
                        # -- the previous {"path": ..., "action": "delete"}
                        # payload matched none of that, so Plan always
                        # returned {"ok": False, "error": "source folder
                        # required"} and this source folder was never
                        # actually removed (silently caught by the outer
                        # except below, logged as "cleanup skipped").
                        # remove_empty is the correct action: Apply only
                        # rmdir()s if the directory is verified truly empty,
                        # failing closed (not raising) otherwise.
                        p_cl = composite_workflows.plan_folder_cleanup({"source": str(src_dir), "action": "remove_empty"})
                        if not p_cl.get("ok") or not p_cl.get("operation_id"):
                            raise RuntimeError(p_cl.get("error") or f"Engine plan_folder_cleanup failed for {src_dir}")
                        app_cl = composite_workflows.apply_folder_cleanup(p_cl["operation_id"])
                        if not app_cl.get("ok"):
                            raise RuntimeError(app_cl.get("error") or f"Engine apply_folder_cleanup failed for {src_dir}")
                        log.append(f"  [cleanup] Source folder removed: {src_dir.name}")
        except Exception as ex:
            log.append(f"  (source folder cleanup skipped: {ex})")

        if selected_subset_import and import_folder_path != source_folder_path:
            try:
                composite_workflows.delete_file(import_folder_path)
            except Exception as ex:
                log.append(f"  [cleanup] WARN staging cleanup skipped: {ex}")
        for aid in album_ids:
            _repair_album_mbid_sticking_once(
                int(aid),
                mb_albumid,
                log,
                write_tags=True,
                cancel_event=cancel_event,
            )

        if selected_subset_import and partial_remaining_audio:
            log.append("  Pending Review kept for unmatched files left in the source folder.")
            if auto_import_idempotency_key:
                _mark_pending_review_status(
                    folder_path,
                    "remaining_files_review",
                    f"Partial import complete. {len(partial_remaining_audio)} unmatched file(s) remain in review.",
                    idempotency_key=auto_import_idempotency_key,
                )
        else:
            _remove_pending_review_for_path(folder_path, log)
        try:
            for aid in album_ids:
                album_obj = lib.get_album(int(aid))
                if album_obj:
                    _auto_merge_case_duplicate_artist_folder(
                        music_root, _s(getattr(album_obj, "albumartist", "") or ""), log,
                    )
        except Exception as ex:
            log.append(f"  [auto-dedup] artist-folder check skipped: {ex}")
        _invalidate_lib_cache()
        if trigger_plex_refresh_after:
            _trigger_plex_refresh(log, workflow=trigger_plex_context)
        else:
            log.append("  [plex] Refresh skipped for Import Review; playlist/batch jobs trigger Plex separately.")
        log.append(f"Done — '{Path(folder_path).name}' imported and tagged.")
        # Record AI suggestion in history if this came from an AI-suggested match
        if ai_suggestion and isinstance(ai_suggestion, dict):
            try:
                _record_ai_match(folder_path, ai_suggestion)
            except Exception:
                pass

    def _do_locked(log, cancel_event=None):
        log.append("[import] Queued — waiting for import worker slot…")
        with _IMPORT_JOB_LOCK:
            try:
                if cancel_event and cancel_event.is_set():
                    raise RuntimeError("cancelled")
                if auto_import_idempotency_key:
                    _import_review_auto_update(
                        auto_import_idempotency_key,
                        status="running",
                        review_item_id=review_item_id,
                        path=folder_path,
                    )
                log.append(f"[import] Import slot acquired — starting… selected_files={len(selected_source_files)}")
                # The slot, held durably: one import at a time across processes.
                with job_contract.held("import-slot", log=log, cancel_event=cancel_event):
                    result = _do(log, cancel_event)
                if auto_import_idempotency_key:
                    _import_review_auto_update(
                        auto_import_idempotency_key,
                        status="completed",
                        completed_at=time.time(),
                    )
                return result
            except Exception as ex:
                reason = str(ex)
                if auto_import_idempotency_key:
                    if _is_music_format_policy_handled_error(reason):
                        outcome = _finalize_pending_review_format_policy_rejection(
                            folder_path,
                            reason,
                            idempotency_key=auto_import_idempotency_key,
                            log=log,
                        )
                        _import_review_auto_update(
                            auto_import_idempotency_key,
                            status=outcome.get("status"),
                            error=outcome.get("note"),
                        )
                    else:
                        _import_review_auto_update(auto_import_idempotency_key, status="failed", error=reason)
                        _mark_pending_review_status(
                            folder_path,
                            "auto_enqueue_failed",
                            reason,
                            idempotency_key=auto_import_idempotency_key,
                        )
                raise

    job = jobs.start_python(_do_locked, label=f"Import+Tag: {Path(folder_path).name}",
                            metadata={"path": folder_path, "mb_albumid": mb_albumid,
                                      "mb_releasegroupid": selected_releasegroupid,
                                      "existing_album_id": existing_album_id,
                                      "missing_track_count": len(wanted_tracks),
                                      "selected_source_file_count": len(selected_source_files),
                                      "selected_source_files": [str(p) for p in selected_source_files],
                                      "partial_subset_import": selected_subset_import,
                                      "trigger_plex_refresh": trigger_plex_refresh_after,
                                      "auto_import": auto_import,
                                      "review_item_id": review_item_id,
                                      "import_review_auto_idempotency_key": auto_import_idempotency_key,
                                      "type": "import-folder"})
    return {"ok": True, "job_id": job.job_id}, 200


_LIBRARY_IMPORT_ALL_LAST_FILE = Path("/config/library_import_all_last.json")


_library_import_all_last_lock = threading.Lock()


def _coerce_library_import_all_album(raw: Dict[str, Any], idx: int) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    aldir = _s(raw.get("aldir") or raw.get("path")).strip()
    if not aldir:
        return None
    mb_albumid = _s(raw.get("mb_albumid") or raw.get("mbid")).strip()
    albumartist = _artist_folder_name_without_mbid(_s(raw.get("albumartist") or raw.get("artist")).strip())
    try:
        existing_album_id = int(raw.get("existing_album_id") or raw.get("album_id") or 0)
    except Exception:
        existing_album_id = 0
    wanted_tracks = _normalise_wanted_tracks(
        raw.get("wanted_tracks") or raw.get("missing_tracks") or [])
    album_title = _s(raw.get("album") or Path(aldir).name).strip()
    if albumartist and album_title and albumartist.lower() not in album_title.lower():
        label = f"{albumartist} — {album_title}"
    else:
        label = album_title or f"album {idx}"
    return {
        "aldir": aldir,
        "mb_albumid": mb_albumid,
        "albumartist": albumartist,
        "existing_album_id": existing_album_id,
        "wanted_tracks": wanted_tracks,
        "album": album_title,
        "label": label,
    }


def _library_import_all_retry_payload(album: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "aldir": _s(album.get("aldir")).strip(),
        "mb_albumid": _s(album.get("mb_albumid")).strip(),
        "existing_album_id": int(album.get("existing_album_id") or 0),
        "albumartist": _s(album.get("albumartist")).strip(),
        "album": _s(album.get("album") or album.get("label")).strip(),
        "wanted_tracks": _normalise_wanted_tracks(album.get("wanted_tracks") or []),
    }


def _library_import_all_record(album: Dict[str, Any], message: str = "",
                               child_job_id: str = "") -> Dict[str, Any]:
    return {
        "label": _s(album.get("label") or album.get("album") or Path(_s(album.get("aldir"))).name),
        "aldir": _s(album.get("aldir")),
        "mb_albumid": _s(album.get("mb_albumid")),
        "existing_album_id": int(album.get("existing_album_id") or 0),
        "wanted_track_count": len(_normalise_wanted_tracks(album.get("wanted_tracks") or [])),
        "message": _s(message),
        "child_job_id": _s(child_job_id),
        "album": _library_import_all_retry_payload(album),
    }


def _library_import_all_read_last() -> Dict[str, Any]:
    try:
        with _library_import_all_last_lock:
            if not _LIBRARY_IMPORT_ALL_LAST_FILE.exists():
                return {}
            data = json.loads(_LIBRARY_IMPORT_ALL_LAST_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _library_import_all_write_last(data: Dict[str, Any]) -> None:
    try:
        with _library_import_all_last_lock:
            _LIBRARY_IMPORT_ALL_LAST_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _LIBRARY_IMPORT_ALL_LAST_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(_LIBRARY_IMPORT_ALL_LAST_FILE)
    except Exception:
        pass


# ── Import ────────────────────────────────────────────────────────────────────

_IMPORT_SOURCE_ALLOWED_ROOTS = tuple(TORRENT_SOURCE_ROOTS) + (MUSIC_ROOT,)


def _resolve_import_source_path(raw: Any) -> Tuple[Optional[Path], Optional[str]]:
    """Validate a caller-supplied import source path against the configured
    torrent-source roots (or the music library root, for the "already
    organized, just untracked" reimport case) before any filesystem
    operation touches it. Mirrors the _folder_cleanup_path()/_path_is_under()
    allowlist pattern already used elsewhere in this file.

    SEC-002 CodeQL repository-wide closure finding: /api/import and
    /api/import/preflight previously called Path(path).mkdir(), .exists()
    and os.walk(path) against the raw request body value with no
    containment check at all -- an authenticated caller could point `path`
    at any absolute filesystem path the container process can reach,
    causing arbitrary-directory creation (mkdir) and filesystem-layout
    enumeration (os.walk) outside the intended torrent-source/library
    roots. This validator must run before any of those operations, not
    just before the eventual composite_workflows hand-off (the engine-side
    reimport_source_atomic() validation is a separate, later boundary and
    does not protect the web-manager-side probes made before it runs).
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
        for root in _IMPORT_SOURCE_ALLOWED_ROOTS
    ):
        return None, "Path is outside the allowed import source roots"
    return resolved, None
