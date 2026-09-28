"""Import Review queue: pending reviews, origins, revalidation and auto-import (ARCH-001).
"""

from __future__ import annotations

import json, os, time, uuid
from backend.matching import AcoustIDStatus
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from backend.app_runtime import _app_logger, AUDIO_EXT, EDITABLE_FIELDS, MUSIC_ROOT, _MB_TRACK_PREFLIGHT_MATCH_THRESHOLD, _MB_UUID_RE, _extract_mb_uuid, _s
from backend.ai_service import _compact_track_ai_candidate, _run_ai_release_preflight
from backend.import_service import IMPORT_REVIEW_AUTO_IMPORT_CONFIDENCE_THRESHOLD, _IMPORT_REVIEW_IMPORTABLE_STATUSES, _import_review_auto_lock, _import_review_auto_state, _import_review_auto_update, _import_review_confidence_score, evaluate_import_eligibility, start_folder_import_with_id
from backend.library_service import _album_folder_for_album_id, _folder_import_track_count, _target_preview_year
from backend.ai_evidence_service import _enrich_track_ai_candidate
from backend.pending_review_store import _AI_PENDING_FILE, _ai_pending_lock, _load_pending_reviews
from backend.ai_batch_state_service import _is_music_format_policy_handled_error
from backend.import_reconciliation_service import _import_review_auto_job_for_key, _resolve_import_review_selected_audio_file, _review_paths_equal, _review_status_key
from backend.app_runtime import _path_has_symlink_component_under, _path_is_under, _path_lexically_under, _redact_security_text
from helpers_mb import _fetch_mb_recording_details, _resolve_release_group_to_release, _mb_release_group_candidates
from backend.beets_adapter import lib
from backend.acoustid_service import _album_track_fingerprint_check, _album_track_norm
from backend.slskd_service import _slskd_title_guess_from_name
from backend.matching_service import _audio_position_from_path, _best_album_track_match, _compact_preflight, _fetch_mb_release_tracklist, _preflight_match_ratio
from backend.app_runtime import jobs
from backend.serializers import _import_review_cleanup_roots, _import_review_path_text_error, _json_from_flask_response, _normalize_review_origin_type, _resolve_import_review_source_path
from backend.pending_review_store import _import_review_folder_signature
from backend.pending_review_store import _finalize_pending_review_format_policy_rejection, _mark_pending_review_status

# ── ARCH-001 extracted code ──


def _metadata_transaction_pending_fields(tx: Dict[str, Any]) -> Dict[str, Any]:
    metadata = tx.get("metadata") or {}
    fields = metadata.get("pending_fields") if isinstance(metadata.get("pending_fields"), dict) else {}
    editable = {field for field, _label in EDITABLE_FIELDS}
    cleaned = {str(k): v for k, v in fields.items() if str(k) in editable}
    if cleaned:
        return cleaned
    changes = tx.get("changes") or []
    if not changes:
        return {}
    diff_rows = changes[0].get("metadata_diff") or []
    return {
        str(row.get("field")): row.get("new")
        for row in diff_rows
        if row.get("changed") and str(row.get("field")) in editable
    }


def _resolve_import_review_folder_path(raw: Any, *, allow_music: bool = False) -> Tuple[Optional[Path], Optional[str]]:
    error = _import_review_path_text_error(raw, allow_relative=False)
    if error:
        return None, error
    folder = Path(_s(raw).strip())
    try:
        music_root = MUSIC_ROOT.resolve(strict=False)
    except Exception:
        music_root = MUSIC_ROOT
    if folder == music_root or _path_lexically_under(folder, music_root):
        if not allow_music:
            return None, "Review file cleanup cannot modify the music library."

    matched_root: Optional[Path] = None
    for root in _import_review_cleanup_roots(allow_music=allow_music):
        if folder == root:
            return None, "Refusing to operate on an approved root."
        if _path_lexically_under(folder, root):
            matched_root = root
            break
    if matched_root is None:
        return None, "Review folder is outside the allowed cleanup roots."

    if _path_has_symlink_component_under(folder, matched_root):
        return None, "Review folder cannot contain symlink components."
    if folder.is_symlink():
        return None, "Review folder cannot be a symlink."
    if not folder.exists() or not folder.is_dir():
        return None, "Review folder does not exist."
    try:
        resolved = folder.resolve(strict=False)
    except Exception:
        return None, "Invalid review folder path."
    if resolved == matched_root:
        return None, "Refusing to operate on an approved root."
    if not _path_is_under(resolved, matched_root):
        return None, "Review folder is outside the allowed cleanup roots."
    return resolved, None


def _trusted_import_review_cleanup_destination(path: Path) -> Tuple[Optional[Path], Optional[str]]:
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None, "Cleanup destination is unsafe."
    try:
        root = Path(os.environ.get("IMPORT_REVIEW_QUARANTINE_DIR", "/config/import_review_quarantine"))
        dest = _import_review_cleanup_destination(path)
    except Exception:
        return None, "Cleanup destination is unsafe."
    if dest == root or not _path_lexically_under(dest, root):
        return None, "Cleanup destination is unsafe."
    if _path_has_symlink_component_under(dest, root, include_leaf=False):
        return None, "Cleanup destination is unsafe."
    try:
        root_resolved = root.resolve(strict=False)
        dest_resolved = dest.resolve(strict=False)
    except Exception:
        return None, "Cleanup destination is unsafe."
    if dest_resolved == root_resolved or not _path_is_under(dest_resolved, root_resolved):
        return None, "Cleanup destination is unsafe."
    return dest_resolved, None


def _import_review_cleanup_destination(path: Path) -> Path:
    root = Path(os.environ.get("IMPORT_REVIEW_QUARANTINE_DIR", "/config/import_review_quarantine"))
    return root / time.strftime("%Y%m%d") / path


def _unique_import_review_cleanup_path(path: Path) -> Path:
    if not path.exists():
        return path
    for idx in range(1, 1000):
        candidate = path.with_name(f"{path.stem}.{idx}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem}.{uuid.uuid4().hex[:8]}{path.suffix}")


def _review_blocked_metadata(status: Any,
                             reason: Any = "",
                             suggestion: Optional[Dict[str, Any]] = None,
                             evidence: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    status_text = _s(status).strip()
    status_key = _review_status_key(status_text)
    reason_text = _s(reason).strip()
    suggestion = suggestion if isinstance(suggestion, dict) else {}
    evidence = evidence if isinstance(evidence, dict) else {}
    preflight = evidence.get("preflight") or suggestion.get("preflight") or {}
    preflight = preflight if isinstance(preflight, dict) else {}

    blocked_statuses = {
        "blocked",
        "not_importable",
        "policy_rejected",
        "format_policy_rejected",
        "review_required",
        "target_blocked",
    }
    blocked = status_key in blocked_statuses or "blocked" in status_key
    if not blocked:
        return {"reason": "", "next_action": ""}

    block_reason = reason_text or _s(preflight.get("reason") or preflight.get("error") or "")
    if not block_reason:
        if preflight.get("acoustid_mismatch"):
            block_reason = "AcoustID fingerprint mismatch requires review before import."
        elif preflight and preflight.get("ok") is False:
            block_reason = "Tracklist preflight failed; review is required before import."
        elif status_text:
            block_reason = status_text
        else:
            block_reason = "Review required before import."

    lower = block_reason.casefold()
    if "music format preferences" in lower or "format policy" in lower:
        next_action = "Choose another source or update Music Format Preferences before retrying."
    elif "acoustid" in lower or "fingerprint" in lower or "audio" in lower:
        next_action = "Verify the audio evidence; delete the source folder if the audio is wrong."
    elif "release group" in lower or "musicbrainz" in lower or "mbid" in lower:
        next_action = "Select or paste a valid MusicBrainz Release Group ID."
    elif "preflight" in lower or "tracklist" in lower or "track" in lower:
        next_action = "Choose a release whose tracklist matches the files."
    else:
        next_action = "Resolve this review item before importing."
    return {"reason": block_reason, "next_action": next_action}


def _review_item_origin_type(row: Dict[str, Any]) -> str:
    return _normalize_review_origin_type(row.get("origin_type"))


def _review_queue_status_matches(row: Dict[str, Any], status_filter: str) -> bool:
    status = _review_status_key(status_filter)
    if not status or status == "all":
        return True
    row_type = _s(row.get("type"))
    if status in {"pending_ai", "skipped", "library_no_mb"}:
        return row_type == status
    preflight = ((row.get("evidence") or {}).get("preflight") or {}) if isinstance(row.get("evidence"), dict) else {}
    status_key = _review_status_key(row.get("status_key") or row.get("status"))
    if status == "audio_mismatch":
        return bool(preflight.get("acoustid_mismatch"))
    if status == "failed":
        return status_key in {"failed", "preflight_failed", "import_failed", "auto_enqueue_failed"} or preflight.get("ok") is False
    if status == "blocked":
        return bool(row.get("blocked_reason")) or status_key in {
            "blocked", "not_importable", "target_conflict", "purge_required",
            "duplicate_only", "duplicate_cleanup", "no_verified_tracks", "format_policy_rejected",
        }
    if status == "no_candidate":
        return row_type == "pending_ai" and not row.get("mb_valid") and not row.get("mb_albumid")
    if status == "ready":
        not_ready_statuses = {
            "auto_enqueue_failed", "import_enqueueing", "import_queued", "no_verified_tracks",
            "format_policy_rejected", "failed", "preflight_failed", "import_failed", "blocked", "not_importable",
        }
        return (
            row_type == "pending_ai"
            and status_key not in not_ready_statuses
            and bool(row.get("mb_valid"))
            and not row.get("blocked_reason")
            and preflight.get("ok") is not False
        )
    return True


def _import_review_suggestion_is_stale(item: Dict[str, Any]) -> bool:
    """True if a stored AI Suggest result's folder_signature no longer
    matches the folder's current contents. An empty stored signature is
    inconclusive (older items predate this field), not stale — don't
    false-positive on those."""
    stored_sig = _s((item or {}).get("folder_signature") or "")
    path = _s((item or {}).get("path") or "")
    if not stored_sig or not path:
        return False
    return _import_review_folder_signature(path) != stored_sig


def _candidate_track_local_candidates(folder: str) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    if not folder:
        return candidates

    source, err = _resolve_import_review_source_path(folder, allow_music=True)
    if err or not source:
        return candidates

    paths: List[Path] = []
    if not source.is_symlink() and source.is_file() and source.suffix.lower() in AUDIO_EXT:
        paths = [source]
    elif source.is_dir():
        for path in sorted(
            [p for p in source.rglob("*") if not p.is_symlink() and p.is_file() and p.suffix.lower() in AUDIO_EXT],
            key=lambda p: str(p).lower(),
        ):
            selected = _resolve_import_review_selected_audio_file(str(path), source)
            if selected:
                paths.append(selected)
        seen_keys: set = set()
        for fpath in paths:
            disc, track = _audio_position_from_path(str(fpath))
            title = _slskd_title_guess_from_name(fpath.name) or fpath.stem
            key = (_album_track_norm(title), str(fpath))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            candidates.append({
                "title": title,
                "path": str(fpath),
                "track": int(track or 0),
                "disc": int(disc or 1),
            })
    return candidates


def _candidate_track_build_comparison(
    mb_albumid: str,
    tracklist: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    *,
    selected_release_group_id: str = "",
    diagnostics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a track comparison for one MusicBrainz release using shared matching."""
    mb_tracks = tracklist.get("tracks") or []
    work_candidates = [dict(cand) for cand in candidates]
    matched_indices: set = set()
    per_mb: Dict[int, List[Tuple[int, float]]] = {}
    fingerprint_status_counts: Dict[str, int] = {}
    if work_candidates and mb_tracks:
        for cand_idx, cand in enumerate(work_candidates):
            best = _best_album_track_match(cand, mb_tracks)
            mb_idx = int(best.get("idx", -1))
            score = float(best.get("score") or 0.0)
            if mb_idx >= 0 and score >= _MB_TRACK_PREFLIGHT_MATCH_THRESHOLD:
                matched_indices.add(mb_idx)
                per_mb.setdefault(mb_idx, []).append((cand_idx, score))
        matched_candidate_indices = {cand_idx for pairs in per_mb.values() for cand_idx, _score in pairs}
        for cand_idx, cand in enumerate(work_candidates):
            if cand_idx in matched_candidate_indices:
                continue
            fp = _album_track_fingerprint_check(cand, mb_tracks)
            fp_status = _s(fp.get("status") or "unknown")
            fingerprint_status_counts[fp_status] = fingerprint_status_counts.get(fp_status, 0) + 1
            cand["fingerprint_status"] = fp_status
            if fp.get("status") == AcoustIDStatus.CONFLICT:
                cand["acoustid_mismatch"] = True
                continue
            if fp.get("status") != AcoustIDStatus.CONFIRMED:
                continue
            fp_candidate = fp.get("candidate") if isinstance(fp.get("candidate"), dict) else {}
            fp_mbid = _s(fp_candidate.get("mb_trackid", "")).strip().lower()
            if not fp_mbid:
                continue
            for mb_idx, mb_track in enumerate(mb_tracks):
                if fp_mbid and fp_mbid == _s(mb_track.get("mb_trackid", "")).strip().lower():
                    cand["acoustid_verified"] = True
                    matched_indices.add(mb_idx)
                    per_mb.setdefault(mb_idx, []).append((cand_idx, 1.0))
                    break

    mb_display: Dict[int, Tuple[int, float]] = {}
    used_cand: set = set()
    for mb_idx in sorted(per_mb):
        for cand_idx, score in sorted(per_mb[mb_idx], key=lambda p: -p[1]):
            if cand_idx not in used_cand:
                mb_display[mb_idx] = (cand_idx, score)
                used_cand.add(cand_idx)
                break

    comparison = []
    for i, mb_track in enumerate(mb_tracks):
        mb_title = mb_track.get("title", "")
        num = mb_track.get("track", i + 1)
        if i in mb_display:
            cand_idx, score = mb_display[i]
            cand = work_candidates[cand_idx]
            local_title = cand.get("title", "")
            source_path = cand.get("path", "")
            status = "acoustid_verified" if cand.get("acoustid_verified") else ("matched" if score >= 0.82 else "fuzzy")
        else:
            local_title = ""
            source_path = ""
            status = "missing"
        comparison.append({
            "num": num,
            "local_title": local_title,
            "mb_title": mb_title,
            "mb_trackid": mb_track.get("mb_trackid", ""),
            "status": status,
            "source_path": source_path,
        })

    for j, cand in enumerate(work_candidates):
        if j not in used_cand:
            comparison.append({
                "num": len(mb_tracks) + 1,
                "local_title": cand.get("title", ""),
                "mb_title": "",
                "mb_trackid": "",
                "status": "conflicting" if cand.get("acoustid_mismatch") else "extra",
                "source_path": cand.get("path", ""),
            })

    matched_count = len(matched_indices)
    fuzzy_count = sum(1 for r in comparison if r["status"] == "fuzzy")
    local_track_count = len(candidates)
    mb_track_count = len(mb_tracks)
    source_ratio = matched_count / max(1, local_track_count)
    release_ratio = matched_count / max(1, mb_track_count)
    preflight_error = ""
    if not matched_count:
        if fingerprint_status_counts.get(AcoustIDStatus.CONFLICT.value):
            preflight_error = "Fuzzy title matching failed; AcoustID matched a recording outside this Release Group."
        elif fingerprint_status_counts.get(AcoustIDStatus.NO_RESULT.value):
            preflight_error = "Fuzzy title matching failed; AcoustID lookup returned no recording."
        elif fingerprint_status_counts.get(AcoustIDStatus.UNAVAILABLE.value):
            preflight_error = "Fingerprint unavailable: source file missing."
        elif local_track_count:
            preflight_error = "No track in selected Release Group matches cleaned local title."
        else:
            preflight_error = "No local audio files were available for track matching."
    return {
        "ok": True,
        "mb_albumid": mb_albumid,
        "representative_release_id": mb_albumid,
        "mb_releasegroupid": tracklist.get("release_group", ""),
        "selected_release_group_id": selected_release_group_id or tracklist.get("release_group", ""),
        "representative_release_group_id": tracklist.get("release_group", ""),
        "identity_validated": True,
        "candidate_identity_error": "",
        "release_group_diagnostics": diagnostics or {},
        "release_title": tracklist.get("release_title", ""),
        "release_artist": tracklist.get("release_artist", ""),
        "date": tracklist.get("date", ""),
        "comparison": comparison,
        "mb_track_count": mb_track_count,
        "local_track_count": local_track_count,
        "matched_count": matched_count,
        "fuzzy_count": fuzzy_count,
        "extra_count": sum(1 for r in comparison if r.get("status") in {"extra", "conflicting"}),
        "fingerprint_status_counts": fingerprint_status_counts,
        "preflight": {
            "ok": bool(matched_count),
            "matches": matched_count,
            "expected": mb_track_count,
            "audio_count": local_track_count,
            "min_required": 1 if local_track_count else 0,
            "match_ratio": round(release_ratio, 3),
            "source_match_ratio": round(source_ratio, 3),
            "artist_ok": True,
            "artist_score": 1,
            "release_title": tracklist.get("release_title", ""),
            "release_artist": tracklist.get("release_artist", ""),
            "release_group": tracklist.get("release_group", ""),
            "error": preflight_error,
            "examples": [],
            "fingerprint_status_counts": fingerprint_status_counts,
        },
    }


def _candidate_track_release_rank(payload: Dict[str, Any]) -> Tuple[float, int, float, int, int]:
    matched = int(payload.get("matched_count") or 0)
    local_count = int(payload.get("local_track_count") or 0)
    mb_count = int(payload.get("mb_track_count") or 0)
    source_ratio = matched / max(1, local_count)
    release_ratio = matched / max(1, mb_count)
    track_delta = abs(mb_count - local_count) if local_count else 999
    return (source_ratio, matched, release_ratio, -track_delta, -mb_count)


def _candidate_track_comparison_payload(
    mb_albumid: str,
    folder: str = "",
    release_group_id: str = "",
) -> Dict[str, Any]:
    """Build Import Review track mapping with Release Group-scoped validation."""
    log: list = []
    trusted_folder = _s(folder).strip()
    if trusted_folder:
        resolved_folder, folder_error = _resolve_import_review_source_path(
            trusted_folder,
            allow_music=True,
            expected_type=None,
            require_exists=True,
        )
        if folder_error or resolved_folder is None:
            return {"ok": False, "error": folder_error or "Source path is not allowed."}
        trusted_folder = str(resolved_folder)
    try:
        candidates = _candidate_track_local_candidates(trusted_folder)
    except Exception as ex:
        _app_logger.error("Import Review local folder scan failed: %s", type(ex).__name__)
        return {"ok": False, "error": "Track comparison could not be completed."}

    selected_rgid = _extract_mb_uuid(release_group_id)
    rep_id = _extract_mb_uuid(mb_albumid)
    if selected_rgid:
        diagnostics: Dict[str, Any] = {
            "selected_release_group_id": selected_rgid,
            "local_file_count": len(candidates),
            "representative_release_id": rep_id,
            "representative_belongs_to_selected_release_group": False,
        }
        release_ids: List[str] = []
        rejected_rep = ""
        rep_group = ""
        if rep_id:
            rep_tracklist = _fetch_mb_release_tracklist(rep_id, log)
            rep_group = _extract_mb_uuid(rep_tracklist.get("release_group", "")) if rep_tracklist.get("ok") else ""
            diagnostics["representative_release_group_id"] = rep_group
            if rep_tracklist.get("ok") and rep_group == selected_rgid:
                diagnostics["representative_belongs_to_selected_release_group"] = True
                release_ids.append(rep_id)
            elif rep_tracklist.get("ok") and rep_group and rep_group != selected_rgid:
                rejected_rep = rep_id
                diagnostics["representative_rejected_reason"] = (
                    "Representative Release ID rejected: it does not belong to selected Release Group"
                )

        for rel in _mb_release_group_candidates(selected_rgid, log):
            rel_id = _extract_mb_uuid(rel.get("mb_albumid", ""))
            if rel_id and rel_id not in release_ids:
                release_ids.append(rel_id)
        if not release_ids:
            resolved = _resolve_release_group_to_release(selected_rgid, log, track_count=len(candidates))
            resolved = _extract_mb_uuid(resolved)
            if resolved:
                release_ids.append(resolved)
        if not release_ids:
            error = "Release Group lookup returned no usable releases."
            return {
                "ok": False,
                "error": error,
                "selected_release_group_id": selected_rgid,
                "representative_release_id": rep_id,
                "representative_release_group_id": rep_group,
                "identity_validated": False,
                "candidate_identity_error": error,
                "release_group_diagnostics": diagnostics,
            }

        best_payload: Optional[Dict[str, Any]] = None
        valid_release_count = 0
        invalid_release_count = 0
        for rel_id in release_ids[:25]:
            tracklist = _fetch_mb_release_tracklist(rel_id, log)
            if not tracklist.get("ok"):
                continue
            rel_group = _extract_mb_uuid(tracklist.get("release_group", ""))
            if rel_group != selected_rgid:
                invalid_release_count += 1
                continue
            valid_release_count += 1
            payload = _candidate_track_build_comparison(
                rel_id,
                tracklist,
                candidates,
                selected_release_group_id=selected_rgid,
                diagnostics=diagnostics,
            )
            if best_payload is None or _candidate_track_release_rank(payload) > _candidate_track_release_rank(best_payload):
                best_payload = payload
        diagnostics["valid_release_count"] = valid_release_count
        diagnostics["invalid_release_count"] = invalid_release_count
        if best_payload is None:
            error = "No representative release under the selected Release Group could be loaded."
            return {
                "ok": False,
                "error": error,
                "selected_release_group_id": selected_rgid,
                "representative_release_id": rep_id,
                "representative_release_group_id": rep_group,
                "identity_validated": False,
                "candidate_identity_error": error,
                "release_group_diagnostics": diagnostics,
            }
        diagnostics["chosen_representative_release_id"] = best_payload.get("mb_albumid", "")
        diagnostics["fuzzy_matches"] = best_payload.get("matched_count", 0)
        diagnostics["fingerprint_matches"] = sum(
            1 for row in best_payload.get("comparison", [])
            if isinstance(row, dict) and row.get("status") == "acoustid_verified"
        )
        best_payload["release_group_diagnostics"] = diagnostics
        best_payload["selected_release_group_id"] = selected_rgid
        best_payload["mb_releasegroupid"] = selected_rgid
        best_payload["identity_validated"] = True
        best_payload["candidate_identity_error"] = ""
        if rejected_rep:
            best_payload["rejected_representative_release_id"] = rejected_rep
            best_payload["representative_rejection_reason"] = diagnostics.get("representative_rejected_reason", "")
        return best_payload

    tracklist = _fetch_mb_release_tracklist(mb_albumid, log)
    if not tracklist.get("ok"):
        return {
            "ok": False,
            "error": "MusicBrainz lookup failed.",
        }
    return _candidate_track_build_comparison(
        mb_albumid,
        tracklist,
        candidates,
        selected_release_group_id=tracklist.get("release_group", ""),
    )


def _manual_review_album_folder(payload: Dict[str, Any]) -> str:
    folder = _s(payload.get("path") or "").strip()
    if folder:
        return folder
    existing_album_id = int(payload.get("existing_album_id") or payload.get("album_id") or 0)
    return _album_folder_for_album_id(existing_album_id) if existing_album_id else ""


def _manual_review_wrong_type_response(target_kind: str, entity_type: str) -> Tuple[Dict[str, Any], int]:
    required = "Recording ID" if target_kind == "item" else "album Release or Release Group ID"
    article = "a" if entity_type.lower().startswith(("release", "recording")) else "an"
    return {
        "ok": False,
        "entity_type": entity_type,
        "error": f"This ID is {article} {entity_type.replace('-', ' ').title()} ID, but this review item requires {required}.",
    }, 400


def _manual_review_public_comparison_error(error: Any) -> str:
    text = _s(error or "").strip()
    lower = text.lower()
    if not text:
        return "Track comparison could not be completed."
    if "none of the local tracks match" in lower:
        return "The ID is valid, but none of the local tracks match this release."
    if "release group lookup returned no usable releases" in lower:
        return "MusicBrainz release group lookup returned no usable releases."
    if "representative release id rejected" in lower or "does not belong to selected release group" in lower:
        return "The selected Release does not belong to the expected Release Group."
    if "no representative release" in lower:
        return "No representative release under the selected Release Group could be loaded."
    return "Track comparison could not be completed."


def _manual_review_validate_album_identifier(parsed: Dict[str, str], payload: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    entity_type = parsed.get("entity_type") or "unknown"
    mbid = parsed.get("mbid") or ""
    folder = _manual_review_album_folder(payload)
    log: List[str] = []
    release_group_candidates: List[Dict[str, Any]] = []
    if entity_type == "recording":
        return _manual_review_wrong_type_response("album", "Recording")

    resolved_release_id = ""
    release_group_id = ""
    if entity_type in {"release", "unknown"}:
        tracklist = _fetch_mb_release_tracklist(mbid, log)
        if tracklist.get("ok"):
            resolved_release_id = mbid
            release_group_id = _extract_mb_uuid(tracklist.get("release_group", ""))
            entity_type = "release"
    if not resolved_release_id and entity_type in {"release-group", "unknown"}:
        release_group_id = mbid
        release_group_candidates = _mb_release_group_candidates(release_group_id, log)
        if release_group_candidates:
            resolved_release_id = _s(release_group_candidates[0].get("mb_albumid") or "").strip().lower()
        if not resolved_release_id:
            resolved_release_id = _resolve_release_group_to_release(
                release_group_id,
                log,
                track_count=_folder_import_track_count(folder),
            )
        if resolved_release_id:
            entity_type = "release-group"

    if not resolved_release_id:
        if entity_type == "release":
            return {"ok": False, "entity_type": entity_type, "error": "MusicBrainz release lookup failed."}, 404
        if entity_type == "release-group":
            return {"ok": False, "entity_type": entity_type, "error": "MusicBrainz release group lookup returned no usable releases."}, 404
        details = _fetch_mb_recording_details(mbid)
        if details.get("recording_id"):
            return _manual_review_wrong_type_response("album", "Recording")
        return {"ok": False, "entity_type": "unknown", "error": "MusicBrainz did not return a release or release group for this ID."}, 404

    comparison = _candidate_track_comparison_payload(
        resolved_release_id,
        folder,
        release_group_id=release_group_id if entity_type == "release-group" else "",
    )
    if not comparison.get("ok"):
        return {
            "ok": False,
            "entity_type": entity_type,
            "mbid": mbid,
            "representative_release_id": resolved_release_id,
            "release_group_id": release_group_id,
            "error": _manual_review_public_comparison_error(comparison.get("error")),
        }, 400
    representative_release_id = _extract_mb_uuid(
        _s(comparison.get("representative_release_id") or comparison.get("mb_albumid") or resolved_release_id)
    )
    acoustic_preflight = _run_ai_release_preflight(
        folder,
        representative_release_id,
        existing_album_id=int(payload.get("existing_album_id") or payload.get("album_id") or 0),
    ) if representative_release_id else None
    preflight = _import_review_revalidation_preflight(comparison, acoustic_preflight)
    selected_match = _import_review_build_revalidated_match(
        {
            "suggestion": {
                "mb_releasegroupid": release_group_id,
                "representative_mb_albumid": representative_release_id,
                "mb_albumid": representative_release_id,
            },
            "artist": _s(payload.get("artist") or ""),
            "album": _s(payload.get("album") or ""),
            "folder_name": Path(folder).name if folder else "",
            "year": _s(payload.get("year") or ""),
        },
        comparison,
        preflight,
    )
    selected_match["source"] = "manual"
    release_group_id = selected_match.get("release_group_id") or release_group_id
    representative_release_id = selected_match.get("representative_release_id") or representative_release_id or resolved_release_id
    matched = int(selected_match.get("track_match_count") or 0)
    local_count = int(selected_match.get("local_track_count") or 0)
    total = int(selected_match.get("total_tracks") or 0)
    status_lines = [
        "Checking MusicBrainz ID...",
        "Valid release" if entity_type == "release" else "Valid release group",
        f"Resolved release group {release_group_id}" if release_group_id else "Release group could not be resolved",
        f"Comparing {local_count} local file{'' if local_count == 1 else 's'} with {total} MusicBrainz track{'' if total == 1 else 's'}...",
        f"{matched} matched, {max(0, total - matched)} need review",
    ]
    return {
        "ok": True,
        "entity_type": entity_type,
        "input_mbid": mbid,
        "release_group_id": release_group_id,
        "representative_release_id": representative_release_id,
        "musicbrainz_url": (
            f"https://musicbrainz.org/release-group/{release_group_id}"
            if entity_type == "release-group"
            else f"https://musicbrainz.org/release/{representative_release_id}"
        ),
        "selected_match": selected_match,
        "track_comparison": comparison,
        "release_group_candidates": release_group_candidates[:12],
        "status_lines": status_lines,
        "message": selected_match.get("preflight_reason") or "Manual MusicBrainz ID validated.",
    }, 200


def _manual_review_validate_recording_identifier(parsed: Dict[str, str], payload: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    entity_type = parsed.get("entity_type") or "unknown"
    mbid = parsed.get("mbid") or ""
    if entity_type in {"release", "release-group"}:
        return _manual_review_wrong_type_response("item", entity_type)
    details = _fetch_mb_recording_details(mbid)
    if not details.get("recording_id"):
        if entity_type == "recording":
            return {"ok": False, "entity_type": "recording", "error": "MusicBrainz recording lookup failed."}, 404
        tracklist = _fetch_mb_release_tracklist(mbid, [])
        if tracklist.get("ok"):
            return _manual_review_wrong_type_response("item", "Release")
        if _mb_release_group_candidates(mbid, []):
            return _manual_review_wrong_type_response("item", "Release Group")
        return {"ok": False, "entity_type": "unknown", "error": "MusicBrainz did not return a recording for this ID."}, 404

    item_id = int(payload.get("item_id") or 0)
    item = lib.get_item(item_id) if item_id else None
    current = {
        "title": _s(getattr(item, "title", "") or ""),
        "artist": _s(getattr(item, "artist", "") or ""),
        "album": _s(getattr(item, "album", "") or ""),
        "albumartist": _s(getattr(item, "albumartist", "") or ""),
        "year": _s(getattr(item, "year", "") or ""),
        "duration_seconds": float(getattr(item, "length", 0) or 0) if item else 0,
        "mb_releasegroupid": _s(getattr(item, "mb_releasegroupid", "") or ""),
    }
    candidate = {
        "mb_trackid": mbid,
        "source": "manual",
        "score": 100,
        "title": details.get("recording_title", ""),
        "artist": details.get("recording_artist") or details.get("artist", ""),
        "album": details.get("album", ""),
        "year": details.get("year", ""),
    }
    _enrich_track_ai_candidate(current, candidate, details)
    packet = _compact_track_ai_candidate(candidate)
    linked_count = len(packet.get("linked_releases") or [])
    return {
        "ok": True,
        "entity_type": "recording",
        "input_mbid": mbid,
        "mb_trackid": mbid,
        "musicbrainz_url": f"https://musicbrainz.org/recording/{mbid}",
        "selected_recording_candidate": packet,
        "recording_candidates": [packet],
        "status_lines": [
            "Checking MusicBrainz ID...",
            "Valid recording",
            f"Found {linked_count} linked release{'' if linked_count == 1 else 's'}",
            packet.get("safety_result") or "Recording evidence ready",
        ],
        "message": packet.get("reason") or "Manual MusicBrainz Recording ID validated.",
    }, 200


def _import_review_job_last_line(job) -> str:
    try:
        return next((_s(line).strip() for line in reversed(job.log[-20:]) if _s(line).strip()), "")
    except Exception:
        return ""


def _normalised_submission_files(raw_files: Iterable[Any]) -> List[str]:
    files: List[str] = []
    seen: set = set()
    for raw in raw_files or []:
        text = _s(raw).strip()
        if not text:
            continue
        resolved, error = _resolve_import_review_source_path(
            text,
            allow_music=True,
            expected_type=None,
            require_exists=False,
        )
        if error or resolved is None:
            continue
        text = str(resolved)
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        files.append(text)
    return sorted(files, key=str.casefold)


def _import_review_auto_job_for_submission(payload: Dict[str, Any], key: str = ""):
    direct = _import_review_auto_job_for_key(key)
    if direct:
        return direct
    review_item_id = _s(payload.get("review_item_id")).strip()
    folder_path = _s(payload.get("path")).strip()
    selected = _normalised_submission_files(payload.get("selected_source_files") or [])
    for job in jobs.all():
        meta = getattr(job, "metadata", {}) or {}
        if meta.get("type") != "import-folder" or not meta.get("auto_import"):
            continue
        if review_item_id and _s(meta.get("review_item_id")).strip() == review_item_id:
            return job
        if folder_path and _review_paths_equal(_s(meta.get("path")), folder_path):
            job_files = _normalised_submission_files(meta.get("selected_source_files") or [])
            if selected and job_files == selected:
                return job
    return None


def _import_review_start_auto_import(payload: Dict[str, Any], eligibility: Dict[str, Any]) -> Dict[str, Any]:
    key = _s(eligibility.get("idempotency_key")).strip()
    folder_path = _s(payload.get("path")).strip()
    review_item_id = _s(payload.get("review_item_id")).strip()
    existing_job = _import_review_auto_job_for_submission(payload, key)
    state = _import_review_auto_state()
    existing_state = state.get(key) or {}
    existing_state_job_id = _s(existing_state.get("job_id")).strip()
    state_job = jobs.get(existing_state_job_id) if existing_state_job_id else None
    reusable_job = existing_job if existing_job and existing_job.status == "running" else None
    if not reusable_job and state_job and state_job.status == "running":
        reusable_job = state_job
    if reusable_job:
        _mark_pending_review_status(
            folder_path,
            "import_queued",
            f"Auto-import queued: {eligibility['selected_track_count']} verified track(s).",
            job_id=reusable_job.job_id,
            idempotency_key=key,
        )
        return {
            "ok": True,
            "queued": True,
            "existing_job": True,
            "job_id": reusable_job.job_id,
            "job_status": reusable_job.status,
            "eligibility": eligibility,
        }
    if existing_job and existing_job.status in {"failed", "success"}:
        _import_review_auto_update(
            key,
            status="previous_job_terminal",
            job_id="",
            stale_job_id=existing_job.job_id,
            previous_job_status=existing_job.status,
            error=_import_review_job_last_line(existing_job) or f"Previous import job {existing_job.status}.",
        )
    if existing_state_job_id and not state_job:
        _import_review_auto_update(
            key,
            status="stale_job_missing",
            job_id="",
            stale_job_id=existing_state_job_id,
            error="Previous auto-import job is no longer in the job store; creating a fresh job.",
        )
    _import_review_auto_update(
        key,
        status="enqueueing",
        job_id="",
        error="",
        stale_job_id="",
        path=folder_path,
        review_item_id=review_item_id,
        selected_source_files=eligibility["selected_file_paths"],
        release_group_id=eligibility["release_group_id"],
        representative_release_id=eligibility["representative_release_id"],
        import_plan={
            "plan_cache_key": (eligibility.get("target_preview") or {}).get("plan_cache_key", ""),
            "selected_track_count": eligibility.get("selected_track_count", 0),
            "target_preview": eligibility.get("target_preview") or {},
        },
    )
    import_payload = {
        "path": folder_path,
        "mb_albumid": eligibility["representative_release_id"],
        "mb_releasegroupid": eligibility["release_group_id"],
        "selected_source_files": eligibility["selected_file_paths"],
        "track_mapping": payload.get("track_mapping") if isinstance(payload.get("track_mapping"), list) else [],
        "ai_suggestion": payload.get("ai_suggestion") if isinstance(payload.get("ai_suggestion"), dict) else {},
        "auto_import": True,
        "review_item_id": review_item_id,
        "auto_import_idempotency_key": key,
        "trigger_plex": bool(payload.get("trigger_plex")),
    }
    _mark_pending_review_status(
        folder_path,
        "import_enqueueing",
        f"Queueing auto-import for {eligibility['selected_track_count']} verified track(s).",
        idempotency_key=key,
    )
    response = start_folder_import_with_id(import_payload)
    data = _json_from_flask_response(response)
    if not data.get("ok") or not data.get("job_id"):
        raise RuntimeError(data.get("error") or "import job was not created")
    job_id = _s(data.get("job_id")).strip()
    _import_review_auto_update(key, status="queued", job_id=job_id)
    _mark_pending_review_status(
        folder_path,
        "import_queued",
        f"Auto-import queued: {eligibility['selected_track_count']} verified track(s).",
        job_id=job_id,
        idempotency_key=key,
    )
    return {"ok": True, "queued": True, "job_id": job_id, "eligibility": eligibility}


def _import_review_payload_from_pending_item(item: Dict[str, Any]) -> Dict[str, Any]:
    item = item or {}
    suggestion = item.get("suggestion") if isinstance(item.get("suggestion"), dict) else {}
    path = _s(item.get("path")).strip()
    existing_ids = suggestion.get("existing_album_ids") or []
    try:
        existing_album_id = int(existing_ids[0] or 0) if existing_ids else 0
    except Exception:
        existing_album_id = 0
    return {
        "path": path,
        "review_item_id": "pending:" + path,
        "mb_albumid": suggestion.get("representative_release_id") or suggestion.get("mb_albumid") or "",
        "mb_releasegroupid": suggestion.get("release_group_id") or suggestion.get("mb_releasegroupid") or "",
        "artist": suggestion.get("artist") or suggestion.get("albumartist") or "",
        "album": suggestion.get("album") or "",
        "year": suggestion.get("year") or "",
        "existing_album_id": existing_album_id,
        "track_mapping": suggestion.get("track_mapping") if isinstance(suggestion.get("track_mapping"), list) else [],
        "selected_match": suggestion,
        "confidence_score": suggestion.get("confidence_score", suggestion.get("confidence")),
        "ai_suggestion": suggestion,
    }


def _pending_item_is_ready_for_backend_auto_import(item: Dict[str, Any]) -> bool:
    status_key = _review_status_key((item or {}).get("status") or "ready_to_import")
    if status_key in {
        "auto_enqueue_failed", "import_enqueueing", "import_queued", "no_verified_tracks",
        "format_policy_rejected", "failed", "preflight_failed", "import_failed", "blocked", "not_importable",
    }:
        return False
    suggestion = (item or {}).get("suggestion") if isinstance((item or {}).get("suggestion"), dict) else {}
    if not suggestion.get("mb_valid"):
        return False
    evidence = (item or {}).get("evidence") if isinstance((item or {}).get("evidence"), dict) else {}
    preflight = evidence.get("preflight") if isinstance(evidence.get("preflight"), dict) else {}
    if preflight.get("ok") is False:
        return False
    return bool((item or {}).get("path"))


def _run_import_review_auto_enqueue_ready_batch(limit: int = 5,
                                                log: Optional[List[str]] = None,
                                                cancel_event=None) -> Dict[str, Any]:
    limit = max(1, min(int(limit or 5), 25))
    queued = 0
    skipped = 0
    failed = 0
    results: List[Dict[str, Any]] = []
    pending = _load_pending_reviews(prune_resolved=False)
    with _import_review_auto_lock:
        for item in pending:
            if cancel_event and cancel_event.is_set():
                if log is not None:
                    log.append("[cancelled]")
                break
            if queued >= limit:
                break
            path = _s((item or {}).get("path")).strip()
            if not _pending_item_is_ready_for_backend_auto_import(item):
                skipped += 1
                continue
            item_payload = _import_review_payload_from_pending_item(item)
            eligibility = evaluate_import_eligibility(item_payload)
            if not eligibility.get("eligible"):
                skipped += 1
                reason = "; ".join(eligibility.get("blocking_reasons") or []) or "not eligible"
                results.append({"path": path, "queued": False, "reason": reason})
                if log is not None:
                    log.append(f"Skipped {Path(path).name or path}: {reason}")
                continue
            try:
                result = _import_review_start_auto_import(item_payload, eligibility)
                queued += 1 if result.get("queued") else 0
                row = {
                    "path": path,
                    "queued": bool(result.get("queued")),
                    "job_id": result.get("job_id", ""),
                    "existing_job": bool(result.get("existing_job")),
                    "selected_track_count": eligibility.get("selected_track_count", 0),
                }
                results.append(row)
                if log is not None:
                    log.append(
                        f"Queued {row['selected_track_count']} track(s) for {Path(path).name or path}"
                    )
            except Exception as ex:
                reason = str(ex)
                if _is_music_format_policy_handled_error(reason):
                    skipped += 1
                    outcome = _finalize_pending_review_format_policy_rejection(path, reason, log=log)
                    results.append({
                        "path": path,
                        "queued": False,
                        "handled": True,
                        "status": outcome.get("status"),
                        "note": outcome.get("note"),
                    })
                    if log is not None:
                        log.append(f"Handled format-policy rejection for {Path(path).name or path}: {outcome.get('note')}")
                    continue
                failed += 1
                _app_logger.warning("Auto-enqueue failed for %r: %s", path, type(ex).__name__)
                _mark_pending_review_status(path, "auto_enqueue_failed", reason)
                results.append({"path": path, "queued": False, "error": "Could not queue this item."})
                if log is not None:
                    log.append(f"Failed {Path(path).name or path}: {ex}")
    summary = {
        "ok": failed == 0,
        "queued_count": queued,
        "skipped_count": skipped,
        "failed_count": failed,
        "limit": limit,
        "items": results,
    }
    if log is not None:
        log.append(
            f"Ready auto-import batch complete: {queued} queued, {skipped} skipped, {failed} failed."
        )
    return summary


def _import_review_revalidation_confidence_score(
    suggestion: Dict[str, Any],
    comparison: Dict[str, Any],
    preflight: Optional[Dict[str, Any]],
) -> float:
    explicit = _import_review_confidence_score(
        suggestion.get("confidence_score") or suggestion.get("match_total") or suggestion.get("score")
    )
    label = _s(suggestion.get("confidence")).strip().casefold()
    label_score = {"high": 0.91, "medium": 0.75, "low": 0.40}.get(label)
    preflight_ratio = _preflight_match_ratio(preflight) if preflight else 0.0
    source_ratio = float((comparison.get("preflight") or {}).get("source_match_ratio") or 0.0)
    release_ratio = float((comparison.get("preflight") or {}).get("match_ratio") or 0.0)
    return max(v for v in (explicit, label_score, preflight_ratio, source_ratio, release_ratio, 0.0) if v is not None)


def _import_review_confidence_level(score: float, importable: bool, preflight_status: str) -> str:
    if not importable:
        return "not_importable"
    if preflight_status != "passed":
        return "not_importable"
    if score >= 0.85:
        return "high"
    if score >= IMPORT_REVIEW_AUTO_IMPORT_CONFIDENCE_THRESHOLD:
        return "medium"
    return "low"


def _import_review_revalidation_preflight(
    comparison: Dict[str, Any],
    acoustic_preflight: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    base = dict(comparison.get("preflight") or {})
    compact = _compact_preflight(acoustic_preflight) if acoustic_preflight else None
    if compact:
        for key in (
            "acoustid_mismatch", "acoustid_target_hits", "acoustid_top_release",
            "acoustid_top_hits", "acoustid_release_hits", "artist_ok", "artist_score",
            "release_title", "release_artist", "release_group",
            # SEC-002 Wave 14: the shared MatchingDecision authority must
            # survive this compaction step to reach the real Import Review
            # auto-import gate in _import_review_build_revalidated_match().
            "release_group_id", "matching_decision",
        ):
            if key in compact:
                base[key] = compact.get(key)
        if compact.get("acoustid_mismatch"):
            base["ok"] = False
            base["error"] = "AcoustID points to a different release."
        elif int(base.get("matches") or 0) > 0:
            base["ok"] = True
            base["error"] = ""
    return base


def _import_review_build_revalidated_match(
    item: Dict[str, Any],
    comparison: Dict[str, Any],
    preflight: Dict[str, Any],
) -> Dict[str, Any]:
    suggestion = item.get("suggestion") if isinstance(item.get("suggestion"), dict) else {}
    release_group_id = _extract_mb_uuid(
        _s(comparison.get("selected_release_group_id") or comparison.get("mb_releasegroupid") or suggestion.get("mb_releasegroupid"))
    )
    representative_release_id = _extract_mb_uuid(
        _s(comparison.get("representative_release_id") or comparison.get("mb_albumid") or suggestion.get("representative_mb_albumid") or suggestion.get("mb_albumid"))
    )
    identity_validated = comparison.get("identity_validated") is not False
    candidate_identity_error = _s(comparison.get("candidate_identity_error")).strip()
    mapping = comparison.get("comparison") if isinstance(comparison.get("comparison"), list) else []
    importable_count = sum(
        1 for row in mapping
        if _s((row if isinstance(row, dict) else {}).get("status")).strip().lower()
        in _IMPORT_REVIEW_IMPORTABLE_STATUSES
    )
    total_tracks = int(comparison.get("mb_track_count") or 0)
    local_tracks = int(comparison.get("local_track_count") or 0)
    matched = int(comparison.get("matched_count") or 0)
    missing = max(0, total_tracks - importable_count)
    preflight_status = "passed" if identity_validated and importable_count > 0 and not preflight.get("acoustid_mismatch") else "failed"
    is_release_group_usable = bool(release_group_id and _MB_UUID_RE.match(release_group_id))
    has_representative = bool(representative_release_id and _MB_UUID_RE.match(representative_release_id))
    # SEC-002 Wave 14: the shared MatchingDecision (build_album_matching_decision,
    # via _folder_release_preflight -> _compact_preflight ->
    # _import_review_revalidation_preflight) must be authoritative here, not
    # merely informational -- a candidate it marks action_allowed=False (e.g.
    # unresolved Release Group ID, or a track-identity conflict) must not be
    # importable no matter what this function's own independent heuristic
    # concludes. An absent/empty matching_decision (no album preflight ran)
    # does not itself block import -- the pre-existing heuristic below still
    # applies in that case -- but an explicit action_allowed=False always does.
    matching_decision = preflight.get("matching_decision") if isinstance(preflight.get("matching_decision"), dict) else {}
    matching_decision_blocks = bool(matching_decision) and not matching_decision.get("action_allowed", True)
    is_importable = bool(
        identity_validated
        and is_release_group_usable
        and has_representative
        and importable_count > 0
        and preflight_status == "passed"
        and not matching_decision_blocks
    )
    confidence_score = _import_review_revalidation_confidence_score(suggestion, comparison, preflight)
    confidence_level = _import_review_confidence_level(confidence_score, is_importable, preflight_status)
    extra_count = int(comparison.get("extra_count") or 0)
    if not identity_validated:
        reason = candidate_identity_error or "Representative Release ID rejected: it does not belong to selected Release Group."
    elif matching_decision_blocks:
        reason = _s(matching_decision.get("explanation") or matching_decision.get("reason")) or "Candidate requires review before import (deterministic identity not verified)."
    elif importable_count:
        reason = (
            f"Revalidated: {importable_count} verified track(s) can import."
            + (f" {extra_count} unmatched file(s) stay in review." if extra_count else "")
            + (f" {missing} album track(s) can be acquired later." if missing else "")
        )
    else:
        reason = preflight.get("error") or "Revalidated: no verified local tracks matched this Release Group."
    return {
        "release_group_id": release_group_id,
        "representative_release_id": representative_release_id,
        "artist": comparison.get("release_artist") or suggestion.get("albumartist") or item.get("artist") or "",
        "album": comparison.get("release_title") or suggestion.get("album") or item.get("album") or item.get("folder_name") or "",
        "year": str(_target_preview_year(comparison.get("date")) or suggestion.get("year") or item.get("year") or ""),
        "track_match_count": matched,
        "total_tracks": total_tracks,
        "local_track_count": local_tracks,
        "track_mapping": mapping,
        "preflight_status": preflight_status,
        "preflight_reason": reason,
        "is_release_group_usable": is_release_group_usable,
        "is_importable": is_importable,
        "is_partial_import": bool(importable_count > 0 and total_tracks and importable_count < total_tracks),
        "confidence_score": round(float(confidence_score), 3),
        "confidence_level": confidence_level,
        "auto_fix_eligible": bool(is_importable and confidence_score >= IMPORT_REVIEW_AUTO_IMPORT_CONFIDENCE_THRESHOLD),
        "auto_fix_requires_review": False,
        "auto_fix_reason": reason,
        "missing_track_count": missing,
        "match_count": importable_count or matched,
        "preflight_ok": preflight_status == "passed",
        "identity_validated": identity_validated,
        "candidate_identity_error": candidate_identity_error,
        "representative_release_group_id": comparison.get("representative_release_group_id", ""),
        "rejected_representative_release_id": comparison.get("rejected_representative_release_id", ""),
        "release_group_diagnostics": comparison.get("release_group_diagnostics") or {},
        "source": "candidate",
        "matching_decision": matching_decision,
        "matching_decision_blocks_import": matching_decision_blocks,
    }


def _update_pending_review_revalidation(
    folder_path: str,
    selected_match: Dict[str, Any],
    preflight: Dict[str, Any],
    note: str,
) -> bool:
    raw = _s(folder_path).strip()
    if not raw:
        return False
    try:
        with _ai_pending_lock:
            if not _AI_PENDING_FILE.exists():
                return False
            items = json.loads(_AI_PENDING_FILE.read_text())
            if not isinstance(items, list):
                return False
            changed = False
            for item in items:
                item = item if isinstance(item, dict) else {}
                if not _review_paths_equal(_s(item.get("path")), raw):
                    continue
                suggestion = item.get("suggestion") if isinstance(item.get("suggestion"), dict) else {}
                suggestion = dict(suggestion)
                suggestion["mb_releasegroupid"] = selected_match.get("release_group_id", "")
                suggestion["representative_mb_albumid"] = selected_match.get("representative_release_id", "")
                suggestion.setdefault("mb_albumid", selected_match.get("representative_release_id", ""))
                suggestion["albumartist"] = selected_match.get("artist", "")
                suggestion["album"] = selected_match.get("album", "")
                suggestion["year"] = selected_match.get("year", "")
                suggestion["preflight"] = preflight
                suggestion["preflight_note"] = note
                suggestion["reason"] = note
                suggestion["track_mapping"] = selected_match.get("track_mapping") or []
                suggestion["track_match_count"] = selected_match.get("track_match_count")
                suggestion["local_track_count"] = selected_match.get("local_track_count")
                suggestion["mb_track_count"] = selected_match.get("total_tracks")
                suggestion["identity_validated"] = selected_match.get("identity_validated")
                suggestion["candidate_identity_error"] = selected_match.get("candidate_identity_error", "")
                suggestion["representative_release_group_id"] = selected_match.get("representative_release_group_id", "")
                suggestion["rejected_representative_release_id"] = selected_match.get("rejected_representative_release_id", "")
                suggestion["confidence_score"] = selected_match.get("confidence_score")
                item["suggestion"] = suggestion
                evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
                evidence = dict(evidence)
                evidence["preflight"] = preflight
                evidence["revalidation"] = {
                    "updated_at": int(time.time()),
                    "track_match_count": selected_match.get("track_match_count"),
                    "local_track_count": selected_match.get("local_track_count"),
                    "total_tracks": selected_match.get("total_tracks"),
                    "importable_track_count": selected_match.get("match_count"),
                    "confidence_score": selected_match.get("confidence_score"),
                }
                item["evidence"] = evidence
                item["status"] = "ready_to_import" if selected_match.get("is_importable") else "no_verified_tracks"
                item["status_note"] = note
                item["updated_at"] = int(time.time())
                changed = True
                break
            if changed:
                _AI_PENDING_FILE.write_text(json.dumps(items, indent=2))
            return changed
    except Exception:
        return False


_UNMATCHED_DRAFT_TRACK_FIELDS = ("title", "duration")


def _redact_unmatched_draft_tracks(tracks: Any) -> List[Dict[str, str]]:
    """Project client-supplied track entries down to a fixed, known-safe
    field set before they can reach persisted draft.json/submission text.

    tracks is client-controlled free-form JSON; without this, an extra
    field (or a title string itself) could carry a stray
    Authorization/api_key/password/URL-credential value straight into a
    file. Only title/duration are ever meaningful for a submission draft,
    so this also drops anything else outright rather than merely
    redacting it in place.
    """
    if not isinstance(tracks, list):
        return []
    result: List[Dict[str, str]] = []
    for entry in tracks:
        if not isinstance(entry, dict):
            continue
        result.append({
            field: _redact_security_text(entry.get(field) or "")
            for field in _UNMATCHED_DRAFT_TRACK_FIELDS
        })
    return result
