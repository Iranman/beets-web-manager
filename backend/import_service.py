"""Folder import into Beets: eligibility, targeting and attach (ARCH-001).
"""

from __future__ import annotations

import copy, hashlib, json, math, os, re, sqlite3, threading, time
import backend.job_contract as job_contract
from backend.matching import verify_audio_against_request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from backend.app_runtime import AUDIO_EXT, DOWNLOADS_ROOT, LOG_FILE, MUSIC_ROOT, TORRENT_SOURCE_ROOTS, _DEFAULT_ALBUM_PATH_TEMPLATE, _MB_TRACK_PREFLIGHT_MATCH_THRESHOLD, _MB_TRACK_REPAIR_MATCH_THRESHOLD, _MB_UUID_RE, _UNRESOLVED_TEMPLATE_TOKEN_RE, _YEAR_SFXRE, _env_int, _extract_mb_uuid, _s
from backend.ai_service import _ai_match_evidence_packet, _auto_merge_case_duplicate_artist_folder, _music_format_policy_rejection_error, _record_ai_match, _validate_import_source_audio
from backend.library_service import _app_managed_download_path, _build_folder_evidence, _delete_if_already_in_library, _preserve_torrent_source_path, _resolve_album_release_for_import, _source_audio_missing_track_scan, _strip_year_from_album_name, _target_preview_artist_folder, _target_preview_year
from backend.ai_evidence_service import _track_ai_similarity
from backend.pending_review_store import _library_album_ids_for_folder, _queue_folder_for_manual_review, _remove_pending_review_for_path
from backend.playlist_service import _music_format_preferences
from backend.ai_batch_state_service import _is_music_format_policy_handled_error
from backend.import_reconciliation_service import _remaining_audio_files, _resolve_import_review_cleanup_file, _resolve_import_review_selected_audio_file
from backend.app_runtime import _path_is_under, _safe_path_component, validated_downloads_root
from backend.slskd import stage_selected_audio_files as _stage_selected_audio_files_impl
from backend.import_guard import filter_wanted_tracks_against_missing as _guard_filter_wanted_tracks_against_missing
from backend.audio_preferences import mark_needs_replacement as _mark_music_format_needs_replacement, validate_audio_file as _validate_audio_file_preferences, validate_audio_properties as _validate_audio_properties, handle_rejected_download as _handle_rejected_audio_download
from backend.title_normalize import restore_time_colon_title as _restore_time_colon_title
from backend.beets_adapter import lib, BeetsError, BeetsUnavailableError, BeetsAuthError
import backend.composite_workflows as composite_workflows
import backend.import_reconciliation as _import_reconciliation
import backend.item_replacement as _item_replacement
from backend.acoustid_service import _acoustid_lookup_cached, _album_item_abs_path
from backend.artwork_service import _ART_EXTS, _move_artwork_to_target
from backend.slskd_service import _normalise_wanted_tracks, _slskd_title_norm, _strip_track_filename_id_suffix, _wanted_track_label
from backend.matching_service import _album_mb_completeness, _album_track_score, _artist_folder_name_without_mbid, _best_album_track_match, _fetch_mb_release_tracklist, _folder_release_preflight, _invalidate_lib_cache, _match_tracks_from_mb, _preflight_review_reason, _repair_album_mbid_sticking_once
from backend.app_runtime import jobs
from backend.job_service import _wait_for_child_job
from backend.musicbrainz_service import _mb_release_track_count
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
    # Validated root only; an unsafe DOWNLOADS_ROOT raises the setup-block
    # message instead of creating _beets_missing_import inside it (#268 S-2).
    return _stage_selected_audio_files_impl(
        validated_downloads_root(), AUDIO_EXT, aldir, audio_files, artist, album, log,
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
    aldir = str(import_source_evidence["path"])
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

        # Web Manager mounts the downloads root and (read-only) the library
        # in the documented compose, so the audio policy reads the files here.
        _validate_import_source_audio(aldir, log, reject_downloads=True)
        log.append(f"[1/3] Importing '{Path(aldir).name}' with Beets as MB release {mb_albumid}…")
        import_timeout = _beet_import_timeout_for_count(import_source_evidence.get("audio_file_count", 0))
        # Beets' own importer does the work (beet import -q --search-id
        # <mb_albumid>) through the confirmed-import family: Beets looks the
        # Release up, applies it and places the files; a source Beets cannot
        # confidently match is skipped by Beets and goes to review. A library
        # folder is imported in place; a download source is moved unless it
        # is a preserved torrent source, which is copied (D10).
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("cancelled")
        # The Release Group the import must land in; apply_confirmed_import
        # refuses an album Beets tagged into any other one.
        planned_rg = _s((_fetch_mb_release_tracklist(mb_albumid, log) or {}).get("release_group")).strip().lower()
        if not planned_rg:
            raise RuntimeError(
                "Could not look up the Release Group of the selected MusicBrainz release; "
                "nothing was imported.")
        def _keep_for_review(reason: str, kept_ids: List[int]) -> None:
            # Never removes the rows Beets just imported: a removal goes
            # through the transaction preview/approve flow, not this job.
            ids = ", ".join(str(i) for i in kept_ids)
            reason = f"{reason} Album_id {ids} was left in the library for review."
            _queue_folder_for_manual_review(
                aldir, {"mb_albumid": mb_albumid, "mb_valid": True, "confidence": "low",
                        "kept_album_ids": kept_ids, "reason": reason},
                reason, log, allow_existing=True)
            raise RuntimeError(reason)

        plan_res = composite_workflows.plan_confirmed_import({
            "source_folder": aldir,
            "existing_album_id": existing_album_id,
            "mb_albumid": mb_albumid,
            "mb_releasegroupid": planned_rg,
            "use_move": not source_is_music_library and not _preserve_torrent_source_path(aldir),
            "in_place": source_is_music_library,
            "duplicate_action": "keep" if existing_album_id else "skip",
        })
        atomic_res = composite_workflows.apply_confirmed_import(
            plan_res["operation_id"], timeout=import_timeout + 15.0)
        if not atomic_res.get("ok"):
            if atomic_res.get("code") == "not_imported":
                review_reason = ("Beets could not confidently match this source to the selected "
                                 "MusicBrainz release.")
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
            # A verification failure after Beets imported (release_group_mismatch,
            # import_ambiguous) keeps the album rows and sends the folder to review.
            if atomic_res.get("album_ids"):
                _keep_for_review(atomic_res.get("error") or "Beets import failed verification.",
                                 atomic_res["album_ids"])
            raise RuntimeError(atomic_res.get("error") or "Beets import failed.")

        # Beets applied the confirmed Release (tags, file placement, write) and
        # apply_confirmed_import verified exactly one new album with that
        # mb_albumid and the planned Release Group. Web Manager does not retag
        # it (ARCH-024).
        aid = int(atomic_res["album_id"])
        log.append(f"[2/3] Beets imported album_id {aid} as release {mb_albumid} "
                   f"(Release Group {planned_rg}, verified).")

        def _needs_review(reason: str) -> None:
            _keep_for_review(reason, [aid])

        if existing_album_id:
            # Missing-track fill: Beets kept the new files as their own album
            # (duplicate_action keep); merge them onto the existing album.
            if wanted_tracks:
                fp_validation = _validate_wanted_album_items_with_acoustid(
                    aid, mb_albumid, wanted_tracks, log)
                if not fp_validation.get("ok", True):
                    _needs_review("AcoustID rejected downloaded file(s) for the requested "
                                  "missing MusicBrainz track(s).")
            aid = _merge_imported_album_into_existing(
                aid, existing_album_id, aldir, log, mb_albumid=mb_albumid,
                replace_existing_item_ids=replace_existing_item_ids)
            merged = composite_workflows.get_album(aid) or {}
            if (_s(merged.get("mb_albumid")).strip().lower() != mb_albumid
                    or _s(merged.get("mb_releasegroupid")).strip().lower() != planned_rg):
                _needs_review(f"Album_id {aid} does not carry the selected release and "
                              "Release Group after the merge.")

        if forced_albumartist:
            cur = composite_workflows.get_album(aid) or {}
            if _s(cur.get("albumartist")) != forced_albumartist:
                up_aa = composite_workflows.update_album_metadata(
                    aid, {"albumartist": forced_albumartist}, force_write_tags=True)
                if not up_aa.get("ok"):
                    raise RuntimeError(f"Engine update albumartist failed for album {aid}")
                rel_res = composite_workflows.relocate_album(aid, mode="rename")
                if not rel_res.get("ok"):
                    raise RuntimeError(f"Engine relocate_album failed for album {aid}")
                log.append(f"  [albumartist] Set to the requested '{forced_albumartist}'.")
        log.append("[3/3] Tags and file placement were done by Beets' importer.")
        album_ids = [aid]

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
        log.append(f"✓ Done — '{Path(aldir).name}' imported by Beets"
                   + (" in place." if source_is_music_library else " into the library structure."))
        return {
            "album_ids": [int(aid) for aid in album_ids if str(aid).isdigit()],
            "item_ids": [],
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
    """Import a folder as a confirmed MusicBrainz Release with Beets' own
    importer (beet import -q --search-id). Beets applies the Release and
    places the files; a source Beets would not confidently match is skipped
    and goes to review. Web Manager does not retag afterwards (ARCH-024).
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
        # The confirmed Release is imported as confirmed: never swapped for
        # another Release (or Release Group) here.
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
        if not selected_releasegroupid:
            raise RuntimeError(
                "The Release Group of the selected MusicBrainz release is unknown, so the "
                "import could not be verified. Import was not started.")
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
        log.append(f"[1/2] Importing '{Path(folder_path).name}' with MB ID {mb_albumid}…")
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
            # The mode decided above: a preserved torrent source is copied.
            "use_move": import_mode == "--move",
            # A folder already inside the library is imported where it is.
            "in_place": source_is_library and not selected_subset_import,
            "duplicate_action": "keep" if existing_album_id else "skip",
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
                timeout=import_timeout + 15.0,
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
        if not apply_res.get("ok"):
            reason = apply_res.get("error") or "Beets import failed."
            kept_ids = apply_res.get("album_ids") or []
            if kept_ids:
                # Verification failed after Beets imported: keep the rows (a
                # removal goes through preview/approve) and send the folder to review.
                reason = (f"{reason} Album_id {', '.join(str(i) for i in kept_ids)} "
                          "was left in the library for review.")
                _queue_folder_for_manual_review(
                    folder_path, {"mb_albumid": mb_albumid, "mb_valid": True, "confidence": "low",
                                  "kept_album_ids": kept_ids, "reason": reason},
                    reason, log, allow_existing=True)
            raise RuntimeError(reason)
        log.append(f"[import] Beets import completed: {import_folder_path}")
        # Beets applied the confirmed Release (tags, placement, write) and
        # apply_confirmed_import verified exactly one new album with that
        # mb_albumid and the planned Release Group. Web Manager does not retag
        # it (ARCH-024); a verification failure above raised with the rows kept.
        album_ids = [int(apply_res["album_id"])]
        log.append(f"[2/2] Beets imported album_id {album_ids[0]} as release {mb_albumid} "
                   f"(Release Group {apply_res.get('mb_releasegroupid') or '?'}, verified).")

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
