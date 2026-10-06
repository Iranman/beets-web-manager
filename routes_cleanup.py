"""Cleanup and duplicate routes (ARCH-001): HTTP handlers over backend.cleanup_service / backend.dedup_service.
"""

from __future__ import annotations

import backend.provider_boundary as provider_boundary
import json, os, re, time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional
from flask import jsonify, request
from helpers_mb import _mb_release_search, _resolve_release_group_to_release, _mb_release_group_candidates
from backend.beets_adapter import lib, BeetsError, BeetsUnavailableError, BeetsAuthError
import backend.composite_workflows as composite_workflows
from backend.identity_contract import verify_album_identity as _verify_album_identity
from backend.acoustid_service import AUDIO_EXTS, _acoustid_fingerprint_match, _album_track_norm, _read_file_media_tags
from backend.app_runtime import ALBUM_FOLDER_CLEANUP_LAST_FILE, METADATA_CACHE_ROOT, MUSIC_ROOT, ROOT_FOLDER_REPAIR_LAST_FILE, _LITERAL_PLACEHOLDER_RE, _MB_UUID_RE, _UNRESOLVED_TEMPLATE_TOKEN_RE, _app_logger, _path_under, _s, _ur, jobs
from backend.cleanup_service import _album_cleanup_apply_issue, _album_cleanup_running_job, _album_cleanup_save_report, _album_folder_cleanup_apply_safe, _album_folder_cleanup_plan, _clean_remove_empty_albums, _clean_remove_orphaned_items, _clear_rgid_resolution, _delete_no_audio_folders, _folder_clean_root, _folder_cleanup_db_items, _folder_cleanup_is_approved_root, _folder_cleanup_merge_preview, _folder_cleanup_path, _folder_cleanup_review_for, _load_rgid_resolutions, _scan_no_audio_folder_candidates, _set_rgid_resolution
from backend.dedup_service import _dedup_final_summary, _dedup_job_result, _dedup_raise_if_cancelled, _dedup_resolve_source_scan, _dedup_scans, _dedup_state_response, _dedup_structured_state, _library_duplicate_merge_safety, run_dedup_cleanup, start_dedup_scan
from backend.import_reconciliation_service import _apply_artist_folder_reconcile_resilient
from backend.job_service import _root_folder_repair_running_job
from backend.library_service import _album_source_folder, _append_stamp_candidate_log, _append_stamp_skipped_log, _apply_artist_folder_groups, _artist_folder_repair_root, _resolve_album_release_for_import, _rgid_group_albums, _root_folder_repair_apply_safe, _root_folder_repair_save_report, _root_folder_repair_scan, _scan_artist_folder_groups, _scan_folder_name_placeholders, _stamp_artist_folder_scan
from backend.maintenance_service import _ALBUM_FOLDER_CLEANUP_LOCK, _ROOT_FOLDER_REPAIR_LOCK, _library_health_payload
from backend.matching_service import _ai_api_key, _ai_model_and_endpoint, _invalidate_lib_cache, _remove_album_track_items, _repair_album_mbid_sticking_once, _scan_album_track_integrity
from backend.musicbrainz_service import _mb_release_group_for_release
from backend.playlist_service import _PLAYLIST_DUPLICATE_JOB_MESSAGE
from backend.serializers import json_route_result
from app import app  # noqa: E402  (route modules load after app.py defines app)

import backend.dedup_authorization as _dedup_authorization
from backend.app_runtime import WEB_MANAGER_DATA_DIR
from backend.auth_service import _transaction_user_label
from backend.dedup_service import _maintenance_full_duplicate_scan
from backend.job_service import _running_job_of_type
from backend.maintenance_service import _maintenance_load_last_report
import backend.duplicate_cleanup as _duplicate_cleanup
import backend.library_integrity_service as _library_integrity
import backend.album_row_merge as _album_row_merge
import backend.untracked_recovery_service as _untracked_recovery

# ── ARCH-001 extracted code ──


@app.post("/api/dedup/scan")
def dedup_scan():
    """Start a background dedup scan; returns job_id immediately."""
    body, status = start_dedup_scan(request.get_json(silent=True) or {})
    return json_route_result(body, status)


@app.get("/api/dedup/scan/<jid>")
def dedup_scan_status(jid):
    """Poll the status of a running dedup scan."""
    state = _dedup_scans.get(jid)
    job = jobs.get(jid)
    if not state and job and isinstance(getattr(job, "result", None), dict):
        result = job.result or {}
        if result.get("kind") in {"scan", "ai_review"}:
            state = {
                "kind": result.get("kind", "scan"),
                "created_at": getattr(job, "created_at", time.time()),
                "status": "done",
                "duplicates": result.get("duplicates", []),
                "folders": result.get("folders", []),
                "scanned": result.get("scanned", 0),
                "found": result.get("found", 0),
                "total": result.get("total", 0),
                "scan_path": result.get("scan_path", ""),
            }
            _dedup_scans[jid] = state
    if not state:
        return jsonify({"ok": False, "error": "Scan job not found"}), 404
    return jsonify(_dedup_state_response(jid, state, job))


@app.post("/api/dedup/ai-review")
def dedup_ai_review():
    """Second-pass duplicate detection using GPT-4o-mini for files the standard scan missed.
    Body: { "scan_jid": "..." }  — must reference a completed /api/dedup/scan job.
    Returns: { ok, job_id } — poll /api/dedup/scan/<job_id> for results.
    """
    payload  = request.get_json(silent=True) or {}
    scan_jid = payload.get("scan_jid", "").strip()
    scan_path_hint = (payload.get("scan_path") or payload.get("path") or "").strip()
    scan_jid, scan_state, resolve_error = _dedup_resolve_source_scan(scan_jid, scan_path_hint)
    if not scan_state:
        return jsonify({"ok": False, "error": resolve_error, "needs_scan": True})
    api_key = _ai_api_key()
    if not api_key:
        return jsonify({"ok": False, "error": "OPENAI_API_KEY not configured"})

    if scan_state.get("status") != "done":
        return jsonify({"ok": False, "error": "Original scan must complete before AI review"})

    already_matched = {d["source_path"] for d in scan_state.get("duplicates", [])}
    scan_path = Path(scan_state.get("scan_path", "/data/torrents/music"))

    state: Dict[str, Any] = {
        "kind": "ai_review",
        "source_scan_jid": scan_jid,
        "created_at": time.time(),
        "status": "running", "log": [], "duplicates": [],
        "folders": [], "scanned": 0, "found": 0, "total": 0,
        "scan_path": str(scan_path),
    }

    def _run(log, cancel, update_state=None):
        from difflib import SequenceMatcher as _SM

        state["log"] = log
        if update_state:
            update_state(_dedup_structured_state(
                state,
                current_task="Listing unmatched files for AI duplicate review",
                current_result="Scanning source path",
            ))
        state["log"].append(f"🤖 AI Review: scanning {scan_path} for unmatched files…")
        try:
            all_files = sorted(
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
                    current_task="Listing unmatched files for AI duplicate review",
                    current_result="Failed to list source files",
                    error_count=1,
                    error_summary=f"Could not list source files: {exc}",
                    final_summary=state["final_summary"],
                ))
            return

        unmatched = [f for f in all_files if str(f) not in already_matched]
        state["total"] = len(unmatched)
        if update_state:
            update_state(_dedup_structured_state(
                state,
                current_task="Preparing AI duplicate review batches",
                current_result=f"{len(unmatched)} unmatched file(s) to review",
            ))
        state["log"].append(
            f"{len(all_files)} total file(s) · "
            f"{len(already_matched)} already matched · "
            f"{len(unmatched)} to review with AI"
        )
        if not unmatched:
            state["log"].append("Nothing left to review — done.")
            state["status"] = "done"
            state["final_summary"] = _dedup_final_summary(state, source_files=all_files)
            if update_state:
                update_state(_dedup_structured_state(
                    state,
                    current_task="AI duplicate review complete",
                    current_item=None,
                    current_path=None,
                    current_result="No unmatched files needed AI review",
                    final_summary=state["final_summary"],
                ))
            result = _dedup_job_result("ai_review", state)
            result["source_scan_jid"] = scan_jid
            return result

        # ── Build compact library index ────────────────────────────────────
        lib_index: List[Dict] = []
        _dedup_raise_if_cancelled(cancel, state)
        if update_state:
            update_state(_dedup_structured_state(
                state,
                current_task="Building library comparison index",
                current_result="Reading library tracks",
            ))
        for item in lib.items([]):
            _dedup_raise_if_cancelled(cancel, state)
            lib_index.append({
                "id":     item.id,
                "artist": (_s(getattr(item, "artist", "") or "")).strip(),
                "title":  (_s(getattr(item, "title",  "") or "")).strip(),
                "album":  (_s(getattr(item, "album",  "") or "")).strip(),
            })
        state["log"].append(f"Library has {len(lib_index)} track(s) to compare against")

        def _norm_words(s: str):
            return set(re.sub(r'[^a-z0-9]', ' ', s.lower()).split())

        BATCH = 20
        new_dupes: List[Dict] = []
        folder_totals: Dict[str, int] = defaultdict(int)
        for f in unmatched:
            folder_totals[str(f.parent)] += 1

        for batch_start in range(0, len(unmatched), BATCH):
            _dedup_raise_if_cancelled(cancel, state)
            batch = unmatched[batch_start: batch_start + BATCH]
            state["scanned"] = batch_start + len(batch)
            state["current_task"] = "Reviewing unmatched files with AI"
            state["current_item"] = (
                f"Batch {batch_start // BATCH + 1} of "
                f"{(len(unmatched) + BATCH - 1) // BATCH}"
            )
            state["current_path"] = str(batch[0]) if batch else ""
            state["current_result"] = f"{state.get('found', 0)} duplicate file(s) found so far"
            if update_state:
                update_state(_dedup_structured_state(state))

            # ── Read embedded tags for each file in batch ──────────────────
            file_meta: List[Dict] = []
            for f in batch:
                fm: Dict = {"path": str(f), "filename": f.name,
                            "artist": "", "title": "", "album": ""}
                tags = _read_file_media_tags(str(f))
                fm["artist"] = (_s(tags.get("artist") or "")).strip()
                fm["title"]  = (_s(tags.get("title") or "")).strip()
                fm["album"]  = (_s(tags.get("album") or "")).strip()
                # Fallback: parse artist from filename  "Artist - Title.mp3"
                if not fm["artist"]:
                    stem = re.sub(r'^\d+\s*[-\.]\s*', '', f.stem)
                    for sep in (' - ', ' – '):
                        if sep in stem:
                            fm["artist"] = stem.split(sep)[0].strip()
                            break
                file_meta.append(fm)

            # ── Find relevant library tracks for this batch ────────────────
            # Build artist-word set AND title-word set for filtering
            batch_artist_words: set = set()
            batch_title_words:  set = set()
            for fm in file_meta:
                if fm["artist"]:
                    batch_artist_words.update(_norm_words(fm["artist"]))
                if fm["album"]:
                    batch_artist_words.update(_norm_words(fm["album"]))
                # Extract title from tag or filename (strip leading track number)
                fm_title = fm["title"] or re.sub(r'^\d+\s*[-_.]\s*', '', Path(fm["path"]).stem)
                fm["_derived_title"] = fm_title   # store for post-validation
                if fm_title:
                    batch_title_words.update(_norm_words(fm_title))

            # Per-file top-3 library candidates by title fuzzy similarity.
            # First filter by artist-word overlap, then rank by SequenceMatcher title sim.
            # This prevents sending 150-track batches where most are irrelevant.
            per_file_top3: Dict[str, List] = {}
            for fm in file_meta:
                fm_title = fm.get("_derived_title") or fm.get("title") or ""
                fm_artist_words = _norm_words(fm.get("artist") or fm.get("album") or "")
                if fm_artist_words:
                    artist_pool = [
                        it for it in lib_index
                        if _norm_words(it["artist"]) & fm_artist_words
                    ]
                else:
                    artist_pool = lib_index
                if fm_title:
                    scored = sorted(
                        ((
                            _SM(None,
                                re.sub(r'[^a-z0-9 ]', '', fm_title.lower()),
                                re.sub(r'[^a-z0-9 ]', '', it["title"].lower())
                            ).ratio(),
                            it,
                        ) for it in artist_pool),
                        key=lambda x: -x[0],
                    )
                    per_file_top3[fm["path"]] = [it for _, it in scored[:3] if _ > 0]
                else:
                    per_file_top3[fm["path"]] = artist_pool[:3]

            seen_ids: set = set()
            relevant = []
            for cands in per_file_top3.values():
                for it in cands:
                    if it["id"] not in seen_ids:
                        seen_ids.add(it["id"])
                        relevant.append(it)

            # ── Build prompt ───────────────────────────────────────────────
            files_str = "\n".join(
                f'{i+1}. filename={fm["filename"]!r}  '
                f'artist={fm["artist"]!r}  title={fm["_derived_title"]!r}  album={fm["album"]!r}'
                for i, fm in enumerate(file_meta)
            )
            lib_str = "\n".join(
                f'  id={it["id"]}  artist={it["artist"]!r}  '
                f'title={it["title"]!r}  album={it["album"]!r}'
                for it in relevant
            )

            prompt = (
                "You are a music duplicate-detection expert.\n\n"
                "The files below are in a DOWNLOADS folder and may already exist in the library.\n"
                "Decide which download files are the SAME SONG as a library track.\n\n"
                f"DOWNLOAD FILES:\n{files_str}\n\n"
                f"LIBRARY TRACKS:\n{lib_str}\n\n"
                "Rules (READ CAREFULLY):\n"
                "• TITLE must match — 'Young Girls' cannot match 'That's What I Like'. "
                "Different song titles = different songs. Never match on artist alone.\n"
                "• Allow minor title variations: punctuation, 'feat.' tags, 'Remaster' suffixes.\n"
                "• Do NOT match remixes, live versions, or acoustic versions to studio originals.\n"
                "• Only include matches you are CERTAIN about (title genuinely the same song).\n"
                '• Return JSON: {"matches": [{"file_idx": <1-based int>, '
                '"lib_id": <int>, "confidence": "high"|"medium", '
                '"reason": "one sentence"}]}\n'
                '• If no file has a genuine title match, return {"matches": []}.'
            )

            _dedup_schema = {
                "type": "object",
                "properties": {
                    "matches": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file_idx":   {"type": "integer"},
                                "lib_id":     {"type": "integer"},
                                "confidence": {"type": "string", "enum": ["high", "medium"]},
                                "reason":     {"type": "string"},
                            },
                            "required": ["file_idx", "lib_id", "confidence", "reason"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["matches"],
                "additionalProperties": False,
            }
            _ai_model, _ai_endpoint = _ai_model_and_endpoint("gpt-4o-mini")
            req_body = json.dumps({
                "model": _ai_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "dedup_matches", "strict": True, "schema": _dedup_schema},
                },
            }).encode()

            try:
                req = _ur.Request(
                    _ai_endpoint,
                    data=req_body,
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                )
                with provider_boundary.opened("ai", req, timeout=45) as r:
                    resp_data = json.loads(r.read())
                msg = (resp_data.get("choices") or [{}])[0].get("message") or {}
                if msg.get("refusal"):
                    state["log"].append(f"  AI refusal: {msg['refusal'][:100]}")
                    continue
                parsed = json.loads(msg["content"])
                matches = parsed.get("matches", []) if isinstance(parsed, dict) else []

                for m in matches:
                    fidx    = int(m.get("file_idx", 0)) - 1
                    lib_id  = int(m.get("lib_id", 0))
                    conf    = str(m.get("confidence", "medium")).lower()
                    reason  = str(m.get("reason", ""))
                    if fidx < 0 or fidx >= len(file_meta): continue
                    if conf not in ("high", "medium"): continue

                    # Look up the library item to get its real path
                    lib_item_obj = lib.get_item(lib_id)
                    if not lib_item_obj: continue
                    lib_path = _s(getattr(lib_item_obj, "path", "") or "")
                    if lib_path and not lib_path.startswith("/"):
                        lib_path = str(MUSIC_ROOT / lib_path)
                    if not Path(lib_path).exists(): continue

                    fm  = file_meta[fidx]
                    src = Path(fm["path"])
                    if str(src) == lib_path: continue  # same file

                    # ── Hard title-similarity guard ────────────────────────
                    # Reject AI matches where titles don't actually align —
                    # this catches hallucinations like "Young Girls" → "24K Magic"
                    def _tsim(a: str, b: str) -> float:
                        a2 = re.sub(r'[^a-z0-9 ]', '', a.lower())
                        b2 = re.sub(r'[^a-z0-9 ]', '', b.lower())
                        if not a2 or not b2: return 0.0
                        return _SM(None, a2, b2).ratio()

                    lib_meta_tmp = next((x for x in lib_index if x["id"] == lib_id), {})
                    file_title_chk = fm.get("_derived_title") or fm.get("title") or src.stem
                    lib_title_chk  = lib_meta_tmp.get("title", "")
                    sim = _tsim(file_title_chk, lib_title_chk)
                    if sim < 0.50:
                        state["log"].append(
                            f"  REJECT hallucination: {src.name!r} → {lib_title_chk!r} "
                            f"(title sim={sim:.2f}, reason={reason!r})"
                        )
                        continue

                    # ── AcoustID fingerprint verification ───────────────────
                    # GPT can hallucinate matches on title similarity alone; a
                    # confirmed fingerprint mismatch rejects the AI candidate
                    # outright, and a confirmed match upgrades confidence.
                    shared_id, src_fp_ids, lib_fp_ids = _acoustid_fingerprint_match(str(src), lib_path)
                    fingerprint_verified = bool(shared_id)
                    if src_fp_ids and lib_fp_ids and not shared_id:
                        state["log"].append(
                            f"  ✗ REJECTED AI match: {src.name!r} → {lib_title_chk!r} "
                            f"— AcoustID fingerprint disagrees (reason={reason!r})"
                        )
                        continue
                    if fingerprint_verified:
                        conf = "high"

                    # Find matching lib_index entry for display metadata
                    lib_meta = next((x for x in lib_index if x["id"] == lib_id), {})
                    dup = {
                        "source_path":          str(src),
                        "source_filename":      src.name,
                        "source_artist":        fm.get("artist", ""),
                        "source_title":         file_title_chk,
                        "source_size":          src.stat().st_size if src.exists() else 0,
                        "lib_path":             lib_path,
                        "lib_title":            lib_meta.get("title", ""),
                        "lib_artist":           lib_meta.get("artist", ""),
                        "lib_album":            lib_meta.get("album", ""),
                        "lib_id":               lib_id,
                        "match_type":           f"AI duplicate{' + fingerprint-verified' if fingerprint_verified else ''}",
                        "confidence":           conf,
                        "reason":               f"AI: {reason}" + (
                            f" Confirmed by AcoustID audio fingerprint (recording {shared_id})."
                            if fingerprint_verified else ""
                        ),
                        "fingerprint_verified": fingerprint_verified,
                    }
                    new_dupes.append(dup)
                    state["found"] += 1
                    state["duplicate_type"] = "AI duplicate"
                    state["current_result"] = "AI duplicate candidate found"
                    if update_state:
                        update_state(_dedup_structured_state(state))
                    state["log"].append(
                        f"  🤖 [{conf}]{' [fingerprint-verified]' if fingerprint_verified else ''} {src.name}"
                        f"  →  {lib_meta.get('artist','')} – {lib_meta.get('title','')}"
                        f"  ({reason})"
                    )

            except Exception as exc:
                state["log"].append(
                    f"  ⚠ Batch {batch_start//BATCH + 1} error: {exc}")

            state["log"].append(
                f"  Batch {batch_start//BATCH + 1}/{(len(unmatched)+BATCH-1)//BATCH} done")
            # Update incrementally so the client can show partial results
            state["duplicates"] = list(new_dupes)
            if update_state:
                update_state(_dedup_structured_state(
                    state,
                    current_result=f"{len(new_dupes)} duplicate file(s) found so far",
                ))

        # ── Group by source folder (same structure as regular scan) ───────
        folder_dups: Dict[str, list] = defaultdict(list)
        for dup in new_dupes:
            folder_dups[str(Path(dup["source_path"]).parent)].append(dup)
        folders_out = []
        for fpath in sorted(folder_dups.keys()):
            fdups  = folder_dups[fpath]
            ftotal = folder_totals.get(fpath, 0)
            folders_out.append({
                "path":        fpath,
                "name":        Path(fpath).name,
                "total_files": ftotal,
                "dup_count":   len(fdups),
                "all_dupes":   len(fdups) >= ftotal > 0,
                "files":       fdups,
            })
        state["folders"] = folders_out
        state["status"]  = "done"
        state["final_summary"] = _dedup_final_summary(state, source_files=unmatched)
        if update_state:
            update_state(_dedup_structured_state(
                state,
                current_task="AI duplicate review complete",
                current_item=None,
                current_path=None,
                current_result=f"{len(new_dupes)} additional duplicate file(s) found",
                final_summary=state["final_summary"],
            ))
        state["log"].append(
            f"🤖 AI review complete — {len(new_dupes)} additional "
            f"duplicate{'s' if len(new_dupes)!=1 else ''} found"
        )
        result = _dedup_job_result("ai_review", state)
        result["source_scan_jid"] = scan_jid
        return result

    job = jobs.start_python(
        _run,
        label=f"Duplicate AI review: {scan_path}",
        metadata={
            "type": "dedup-ai-review",
            "path": str(scan_path),
            "source_scan_jid": scan_jid,
        },
    )
    _dedup_scans[job.job_id] = state
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/dedup/cleanup")
def dedup_cleanup():
    body, status = run_dedup_cleanup(request.get_json(silent=True) or {})
    return json_route_result(body, status)


@app.post("/api/clean/no-audio-folders/scan")
def clean_no_audio_folders_scan():
    payload = request.get_json(silent=True) or {}
    root = (payload.get("root") or str(MUSIC_ROOT)).strip()
    try:
        root_path = _folder_clean_root(root)
    except Exception as ex:
        status_code = 409 if str(ex) == _PLAYLIST_DUPLICATE_JOB_MESSAGE else 400
        return jsonify({"ok": False, "error": str(ex)}), status_code

    def _do(log, cancel_event=None):
        log.append(f"Scanning for folders with no audio files under {root_path}")
        return _scan_no_audio_folder_candidates(root_path, log)

    job = jobs.start_python(_do, label=f"Clean empty folders scan: {root_path.name or root_path}")
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/clean/no-audio-folders/delete")
def clean_no_audio_folders_delete():
    payload = request.get_json(silent=True) or {}
    root = (payload.get("root") or str(MUSIC_ROOT)).strip()
    paths = payload.get("paths") or []
    dry_run = payload.get("dry_run", True) is not False
    if not isinstance(paths, list) or not paths:
        return jsonify({"ok": False, "error": "paths required"}), 400
    try:
        _folder_clean_root(root)
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 400
    if dry_run:
        log: List[str] = []
        result = _delete_no_audio_folders(root, paths, dry_run=True, log=log)
        return jsonify(result), (200 if result.get("ok") else 400)
    if payload.get("confirm") is not True:
        return jsonify({"ok": False, "code": "confirmation_required",
                        "error": "Deleting folders needs explicit confirmation (confirm: true) after a dry run."}), 400

    def _do(log, cancel_event=None):
        result = _delete_no_audio_folders(root, paths, dry_run=False, log=log)
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "Folder deletion failed.")
        return result

    job = jobs.start_python(_do, label="Clean empty/no-audio folders")
    return jsonify({"ok": True, "job_id": job.job_id})


@app.get("/api/clean/library-health")
def clean_library_health():
    # request.args.get(..., 100) returns a raw query-string TEXT value
    # whenever the parameter is actually present (only the "not present"
    # fallback is the literal int 100) -- _library_health_payload() below
    # now does its own local Python-level list slicing with duplicate_limit
    # (unlike before this fix, where it only ever passed the value through
    # to composite_workflows.get_library_health(), which just stringifies it into
    # a URL query string and lets the engine's own _parse_bounded_int_param
    # validate/cast it). A raw string reaching a `list[:duplicate_limit]`
    # slice raises TypeError, so these must be real ints before being used
    # locally at all, not just by the time they reach the engine.
    try:
        orphan_sample_limit = int(request.args.get("orphan_sample_limit", 100))
        duplicate_limit = int(request.args.get("duplicate_limit", 100))
        empty_limit = int(request.args.get("empty_limit", 100))
    except (TypeError, ValueError):
        return jsonify({
            "ok": False,
            "error": "Invalid library health query parameters.",
            "error_code": "INVALID_QUERY_PARAMETER",
        }), 400
    try:
        return jsonify(_library_health_payload(
            orphan_sample_limit=orphan_sample_limit,
            duplicate_limit=duplicate_limit,
            empty_limit=empty_limit,
        ))
    except BeetsUnavailableError as ex:
        _app_logger.warning("Library health scan failed: Beets engine unavailable")
        return jsonify({
            "ok": False,
            "error": "Beets engine is unavailable.",
            "error_code": "ENGINE_OFFLINE",
        }), 503
    except BeetsAuthError as ex:
        _app_logger.error("Library health scan failed: Control agent authentication failed (%s)", type(ex).__name__)
        return jsonify({
            "ok": False,
            "error": "Beets engine authentication failed.",
            "error_code": "ENGINE_AUTH_FAILED",
        }), 503
    except BeetsError as ex:
        status_code = getattr(ex, "status_code", 0) or 500
        error_code = getattr(ex, "error_code", "")
        if not error_code or error_code in ("BEETS_ADAPTER_ERROR", "BEETS_ERROR"):
            error_code = "LIBRARY_HEALTH_FAILED"
        _app_logger.error("Library health scan failed: %s (code=%s, status=%s)", type(ex).__name__, error_code, status_code)
        if error_code == "INVALID_QUERY_PARAMETER":
            client_msg = "Invalid library health query parameters."
        else:
            client_msg = "Could not load library health."
        return jsonify({
            "ok": False,
            "error": client_msg,
            "error_code": error_code,
        }), status_code
    except Exception as ex:
        _app_logger.exception("Library health scan unexpected failure")
        return jsonify({
            "ok": False,
            "error": "Could not load library health.",
            "error_code": "LIBRARY_HEALTH_FAILED",
        }), 500


@app.post("/api/clean/library-health/scan")
def clean_library_health_scan():
    def _do(log, cancel_event=None, update_state=None):
        log.append("Scanning library database health")
        if update_state:
            update_state({
                "category": "Cleanup",
                "current_task": "Scanning library database health",
                "current_result": "Starting database health scan",
            })
        if cancel_event and cancel_event.is_set():
            log.append("Cancelled before scan started.")
            raise RuntimeError("cancelled")
        result = _library_health_payload(progress=update_state)
        if cancel_event and cancel_event.is_set():
            log.append("Cancelled after scan completed.")
            raise RuntimeError("cancelled")
        final_summary = result.get("final_summary") or {}
        if update_state:
            update_state({
                "category": "Cleanup",
                "current_task": "Library DB Health scan complete",
                "current_item": None,
                "current_path": None,
                "scanned_count": result.get("database_rows_scanned"),
                "duplicate_album_groups": result.get("duplicate_album_count", 0),
                "same_release_group_id_groups": result.get("rgid_duplicate_group_count", 0),
                "orphaned_items": result.get("orphaned_item_count", 0),
                "empty_albums": result.get("empty_album_count", 0),
                "missing_files": result.get("orphaned_item_count", 0),
                "current_result": (
                    f"{result.get('duplicate_album_count', 0)} duplicate album group(s), "
                    f"{result.get('rgid_duplicate_group_count', 0)} same Release Group ID group(s)"
                ),
                "final_summary": final_summary,
            })
        log.append(
            "Done: "
            f"{result.get('duplicate_album_count', 0)} duplicate album group(s), "
            f"{result.get('rgid_duplicate_group_count', 0)} same Release Group ID group(s), "
            f"{result.get('orphaned_item_count', 0)} orphaned item(s), "
            f"{result.get('empty_album_count', 0)} empty album row(s)."
        )
        return result

    job = jobs.start_python(
        _do,
        label="Scan library database health",
        metadata={"type": "library-health-scan"},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/clean/merge-duplicate-album")
def clean_merge_duplicate_album():
    """Merge source album rows into target album (DB only, audio untouched).

    Body: { target_album_id: int, source_album_id: int }
    The source album's items are re-assigned to the target; the source album row
    is deleted. Target album inherits any metadata the source has that target lacks
    (mb_albumid, year, label, etc.).
    """
    payload = request.get_json(silent=True) or {}
    target_id = int(payload.get("target_album_id") or 0)
    source_id = int(payload.get("source_album_id") or 0)
    if not target_id or not source_id:
        return jsonify({"ok": False, "error": "target_album_id and source_album_id are required"}), 400
    if target_id == source_id:
        return jsonify({"ok": False, "error": "target and source must be different album IDs"}), 400

    target_album = composite_workflows.get_album(target_id)
    source_album = composite_workflows.get_album(source_id)
    target_items = composite_workflows.find_all_items_by_album_id(target_id) if target_album else []
    source_items = composite_workflows.find_all_items_by_album_id(source_id) if source_album else []

    safety_rows = []
    if target_album:
        safety_rows.append({
            "id": target_id,
            "albumartist": target_album.get("albumartist", ""),
            "album": target_album.get("album", ""),
            "year": target_album.get("year", 0),
            "mb_albumid": target_album.get("mb_albumid", ""),
            "mb_releasegroupid": target_album.get("mb_releasegroupid", "") or "",
            "track_count": len(target_items),
        })
    if source_album:
        safety_rows.append({
            "id": source_id,
            "albumartist": source_album.get("albumartist", ""),
            "album": source_album.get("album", ""),
            "year": source_album.get("year", 0),
            "mb_albumid": source_album.get("mb_albumid", ""),
            "mb_releasegroupid": source_album.get("mb_releasegroupid", "") or "",
            "track_count": len(source_items),
        })
    safety_items = sorted(
        target_items + source_items,
        key=lambda it: (int(it.get("album_id") or 0), int(it.get("disc") or 1), int(it.get("track") or 0), int(it.get("id") or 0))
    )

    if len(safety_rows) != 2:
        found_ids = {int(r["id"]) for r in safety_rows}
        missing_ids = [aid for aid in (target_id, source_id) if aid not in found_ids]
        return jsonify({"ok": False, "error": f"album_id not found: {', '.join(map(str, missing_ids))}"}), 404

    pair_key = {
        (_album_track_norm(_s(r["albumartist"])), _album_track_norm(_s(r["album"])))
        for r in safety_rows
    }
    if len(pair_key) != 1:
        return jsonify({"ok": False, "error": "target and source are not the same normalized album group"}), 409

    safety_items_by_album: Dict[int, List[Any]] = {}
    for row in safety_items:
        safety_items_by_album.setdefault(int(row["album_id"]), []).append(row)
    safety = _library_duplicate_merge_safety(safety_rows, safety_items_by_album)
    if not safety.get("merge_safe"):
        return jsonify({
            "ok": False,
            "error": safety.get("merge_reason") or "Duplicate album merge is not safe.",
            "merge_safe": False,
            "merge_blockers": safety.get("merge_blockers", []),
        }), 409
    if target_id != int(safety.get("merge_target_album_id") or 0):
        return jsonify({
            "ok": False,
            "error": f"target album_id must be {safety.get('merge_target_album_id')} for this safe merge",
            "merge_safe": False,
        }), 409

    def _do(log, cancel_event=None):
        target_row = composite_workflows.get_album(target_id)
        source_row = composite_workflows.get_album(source_id)
        if not target_row:
            raise RuntimeError(f"Target album_id {target_id} not found")
        if not source_row:
            raise RuntimeError(f"Source album_id {source_id} not found")

        t_label = f"{_s(target_row['albumartist'])} — {_s(target_row['album'])} (id={target_id})"
        s_label = f"{_s(source_row['albumartist'])} — {_s(source_row['album'])} (id={source_id})"
        log.append(f"Merging: {s_label}  →  {t_label}")

        merge_res = composite_workflows.merge_duplicate_albums(target_id, source_id)
        if not merge_res.get("ok"):
            raise RuntimeError(merge_res.get("error") or "Engine rejected duplicate album merge")
        moved = int(merge_res.get("moved") or 0)
        log.append(f"  Moved {moved} item(s) to target album")
        for col, val in (merge_res.get("inherit_fields") or {}).items():
            log.append(f"  Inherited {col}={_s(val)!r} from source")
        log.append(f"  Deleted source album row {source_id}")

        _invalidate_lib_cache()
        log.append("Done.")
        return {"ok": True, "moved": moved, "target_album_id": target_id, "source_album_id": source_id}

    job = jobs.start_python(
        _do,
        label=f"Merge duplicate album {source_id}→{target_id}",
        metadata={"type": "merge-duplicate-album",
                  "target_album_id": target_id, "source_album_id": source_id},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.get("/api/clean/rgid-group/<rgid>")
def clean_rgid_group_detail(rgid):
    """Fetch full cluster details for one release-group-ID cluster (fetch-cluster-details)."""
    rgid = _s(rgid).strip().lower()
    if not _MB_UUID_RE.match(rgid):
        return jsonify({"ok": False, "error": "invalid MusicBrainz release-group id"}), 400
    rows = _rgid_group_albums(rgid)
    if not rows:
        return jsonify({"ok": False, "error": "no albums found for this release-group id"}), 404

    items_by_album: Dict[int, List[Any]] = {}
    for r in rows:
        aid = int(r["id"])
        items_by_album[aid] = r.get("tracks") or composite_workflows.find_all_items_by_album_id(aid)

    def _folder_for(aid: int) -> str:
        dirs = [os.path.dirname(_s(it["path"])) for it in items_by_album.get(aid, []) if _s(it["path"])]
        dirs = [d for d in dirs if d]
        return Counter(dirs).most_common(1)[0][0] if dirs else ""

    safety = _library_duplicate_merge_safety(rows, items_by_album)
    albums = [
        {
            "album_id": int(r["id"]),
            "albumartist": _s(r["albumartist"]),
            "album": _s(r["album"]),
            "year": int(r["year"] or 0),
            "track_count": int(r["track_count"] or 0),
            "mb_albumid": _s(r["mb_albumid"]),
            "aldir": _folder_for(int(r["id"])),
        }
        for r in rows
    ]
    try:
        candidate_releases = _mb_release_group_candidates(rgid)
    except Exception:
        candidate_releases = []

    return jsonify({
        "ok": True,
        "mb_releasegroupid": rgid,
        "albums": albums,
        "merge_safe": safety.get("merge_safe"),
        "merge_target_album_id": safety.get("merge_target_album_id"),
        "merge_source_album_ids": safety.get("merge_source_album_ids"),
        "merge_reason": safety.get("merge_reason"),
        "merge_blockers": safety.get("merge_blockers", []),
        "resolution": _load_rgid_resolutions().get(rgid),
        "candidate_releases": candidate_releases,
    })


@app.post("/api/clean/rgid-group/merge")
def clean_rgid_group_merge():
    """Merge two album rows that share a release-group id (regardless of title text)."""
    payload = request.get_json(silent=True) or {}
    rgid = _s(payload.get("mb_releasegroupid") or "").strip().lower()
    target_id = int(payload.get("target_album_id") or 0)
    source_id = int(payload.get("source_album_id") or 0)
    if not _MB_UUID_RE.match(rgid):
        return jsonify({"ok": False, "error": "mb_releasegroupid is required"}), 400
    if not target_id or not source_id or target_id == source_id:
        return jsonify({"ok": False, "error": "target_album_id and source_album_id are required and must differ"}), 400

    rows = _rgid_group_albums(rgid)
    ids_in_group = {int(r["id"]) for r in rows}
    if target_id not in ids_in_group or source_id not in ids_in_group:
        return jsonify({"ok": False, "error": "target/source album must belong to this release-group id"}), 409

    items_by_album: Dict[int, List[Any]] = {}
    items_by_album[target_id] = composite_workflows.find_all_items_by_album_id(target_id)
    items_by_album[source_id] = composite_workflows.find_all_items_by_album_id(source_id)
    pair_rows = [r for r in rows if int(r["id"]) in (target_id, source_id)]
    safety = _library_duplicate_merge_safety(pair_rows, items_by_album)
    if not safety.get("merge_safe"):
        return jsonify({
            "ok": False,
            "error": safety.get("merge_reason") or "Merge is not safe.",
            "merge_blockers": safety.get("merge_blockers", []),
        }), 409
    if target_id != int(safety.get("merge_target_album_id") or 0):
        return jsonify({
            "ok": False,
            "error": f"target album_id must be {safety.get('merge_target_album_id')} for this safe merge",
            "merge_safe": False,
        }), 409

    def _do(log, cancel_event=None):
        target_row = composite_workflows.get_album(target_id)
        source_row = composite_workflows.get_album(source_id)
        if not target_row or not source_row:
            raise RuntimeError("album row(s) not found")

        log.append(f"Merging release-group {rgid} cluster: album {source_id} → {target_id}")
        merge_res = composite_workflows.merge_duplicate_albums(target_id, source_id)
        if not merge_res.get("ok"):
            raise RuntimeError(merge_res.get("error") or "Engine rejected release-group duplicate merge")
        moved = int(merge_res.get("moved") or 0)
        log.append(f"  Moved {moved} item(s) to target album")
        for col, val in (merge_res.get("inherit_fields") or {}).items():
            log.append(f"  Inherited {col}={_s(val)!r} from source")
        log.append(f"  Deleted source album row {source_id}")

        _clear_rgid_resolution(rgid)
        _invalidate_lib_cache()
        log.append("Done.")
        return {
            "ok": True, "moved": moved, "target_album_id": target_id,
            "source_album_id": source_id, "mb_releasegroupid": rgid,
        }

    job = jobs.start_python(
        _do,
        label=f"Merge release-group duplicate {source_id}→{target_id}",
        metadata={"type": "merge-rgid-duplicate", "mb_releasegroupid": rgid,
                  "target_album_id": target_id, "source_album_id": source_id},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/clean/rgid-group/keep-separate")
def clean_rgid_group_keep_separate():
    """Persist a 'these are valid separate editions' decision so the warning doesn't return."""
    payload = request.get_json(silent=True) or {}
    rgid = _s(payload.get("mb_releasegroupid") or "").strip().lower()
    reason = _s(payload.get("reason") or "").strip() or "Marked as separate editions under the same release group"
    if not _MB_UUID_RE.match(rgid):
        return jsonify({"ok": False, "error": "mb_releasegroupid is required"}), 400
    rows = _rgid_group_albums(rgid)
    if not rows:
        return jsonify({"ok": False, "error": "no albums found for this release-group id"}), 404
    album_ids = [int(r["id"]) for r in rows]
    _set_rgid_resolution(rgid, "keep_separate", reason, album_ids)
    return jsonify({"ok": True, "mb_releasegroupid": rgid, "decision": "keep_separate", "reason": reason})


@app.post("/api/clean/rgid-group/undo-resolution")
def clean_rgid_group_undo_resolution():
    """Clear a previously persisted keep-separate/resolved decision for a release-group id."""
    payload = request.get_json(silent=True) or {}
    rgid = _s(payload.get("mb_releasegroupid") or "").strip().lower()
    if not _MB_UUID_RE.match(rgid):
        return jsonify({"ok": False, "error": "mb_releasegroupid is required"}), 400
    _clear_rgid_resolution(rgid)
    return jsonify({"ok": True, "mb_releasegroupid": rgid})


@app.post("/api/clean/rgid-group/assign-representative-release")
def clean_rgid_group_assign_release():
    """Assign a specific candidate MusicBrainz release as an album row's representative release."""
    payload = request.get_json(silent=True) or {}
    album_id = int(payload.get("album_id") or 0)
    mb_albumid = _s(payload.get("mb_albumid") or "").strip().lower()
    if not album_id or not _MB_UUID_RE.match(mb_albumid):
        return jsonify({"ok": False, "error": "album_id and a valid mb_albumid are required"}), 400
    alb = composite_workflows.get_album(album_id)
    if not alb:
        return jsonify({"ok": False, "error": f"album_id {album_id} not found"}), 404
    rgid = _s(alb.get("mb_releasegroupid") or "").strip().lower()

    def _do(log, cancel_event=None):
        # ARCH-009: fail closed -- an unverifiable release is never assigned.
        identity = _verify_album_identity(rgid, mb_albumid, resolve_release_group=_mb_release_group_for_release,
                                          require_release_group=False)
        if not identity.ok:
            raise RuntimeError(f"{identity.error} Refusing to assign this release.")
        result = _repair_album_mbid_sticking_once(
            album_id, mb_albumid, log, repair_tracks=True, write_tags=True, cancel_event=cancel_event,
        )
        _invalidate_lib_cache()
        log.append(f"Assigned representative release {mb_albumid} to album_id {album_id}.")
        return result

    job = jobs.start_python(
        _do,
        label=f"Assign representative release to album {album_id}",
        metadata={"type": "rgid-assign-representative-release", "album_id": album_id, "mb_albumid": mb_albumid},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/clean/rgid-group/relink")
def clean_rgid_group_relink():
    """Search MusicBrainz and relink a wrongly-grouped row to a different release/release-group."""
    payload = request.get_json(silent=True) or {}
    album_id = int(payload.get("album_id") or 0)
    mb_albumid = _s(payload.get("mb_albumid") or "").strip().lower()
    mb_releasegroupid = _s(payload.get("mb_releasegroupid") or "").strip().lower()
    if not album_id:
        return jsonify({"ok": False, "error": "album_id is required"}), 400
    row = composite_workflows.get_album(album_id)
    if not row:
        return jsonify({"ok": False, "error": f"album_id {album_id} not found"}), 404

    def _do(log, cancel_event=None):
        target_mbid = mb_albumid
        target_rgid = mb_releasegroupid
        if target_mbid and not _MB_UUID_RE.match(target_mbid):
            raise RuntimeError(f"'{target_mbid}' is not a valid MusicBrainz release id")
        if not target_mbid:
            if target_rgid and _MB_UUID_RE.match(target_rgid):
                target_mbid = _resolve_release_group_to_release(target_rgid, log)
            else:
                candidates = _mb_release_search(
                    _s(row["album"]), _s(row["albumartist"]), limit=1,
                    year=_s(row["year"] or ""), log=log,
                )
                if not candidates:
                    raise RuntimeError("No MusicBrainz release found for this artist/album")
                target_mbid = _s(candidates[0].get("mb_albumid") or "").strip().lower()
                target_rgid = _s(candidates[0].get("mb_releasegroupid") or "").strip().lower()
        if not target_mbid or not _MB_UUID_RE.match(target_mbid):
            raise RuntimeError("Could not resolve a valid MusicBrainz release for relink")
        # ARCH-009: the Release Group written is always the release's
        # authoritative one; a supplied RGID that disagrees is refused.
        identity = _verify_album_identity(target_rgid, target_mbid,
                                          resolve_release_group=_mb_release_group_for_release)
        if not identity.ok:
            raise RuntimeError(identity.error)
        target_rgid = identity.release_group_id

        relink_updates: Dict[str, Any] = {"mb_albumid": target_mbid}
        if target_rgid:
            relink_updates["mb_releasegroupid"] = target_rgid
        relink_res = composite_workflows.update_album_metadata(album_id, relink_updates)
        if not relink_res.get("ok"):
            raise RuntimeError(relink_res.get("error") or "Engine rejected release relink")
        log.append(
            f"Relinked album_id {album_id} to release {target_mbid}"
            + (f" (release group {target_rgid})" if target_rgid else "")
        )
        result = _repair_album_mbid_sticking_once(
            album_id, target_mbid, log, repair_tracks=True, write_tags=True, cancel_event=cancel_event,
        )
        _invalidate_lib_cache()
        return {**result, "mb_albumid": target_mbid, "mb_releasegroupid": target_rgid}

    job = jobs.start_python(
        _do,
        label=f"Relink album {album_id}",
        metadata={"type": "rgid-relink", "album_id": album_id},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/clean/rgid-group/send-to-repair")
def clean_rgid_group_send_to_repair():
    """Route a 1-2-track partial-import row into the existing MBID repair machinery."""
    payload = request.get_json(silent=True) or {}
    album_id = int(payload.get("album_id") or 0)
    if not album_id:
        return jsonify({"ok": False, "error": "album_id is required"}), 400
    alb = composite_workflows.get_album(album_id)
    if not alb:
        return jsonify({"ok": False, "error": f"album_id {album_id} not found"}), 404
    items = composite_workflows.find_all_items_by_album_id(album_id)
    row = {
        "id": alb["id"],
        "albumartist": alb.get("albumartist", ""),
        "album": alb.get("album", ""),
        "year": alb.get("year", 0),
        "mb_albumid": alb.get("mb_albumid", ""),
        "mb_releasegroupid": alb.get("mb_releasegroupid", "") or "",
        "track_count": len(items),
    }
    if not row:
        return jsonify({"ok": False, "error": f"album_id {album_id} not found"}), 404

    def _do(log, cancel_event=None):
        mbid = _s(row["mb_albumid"] or "").strip().lower()
        rgid = _s(row["mb_releasegroupid"] or "").strip().lower()
        label = f"{_s(row['albumartist'])} - {_s(row['album'])}".strip(" -")
        if not mbid:
            log.append(f"album_id {album_id} ({label}) has no representative release id — resolving one first.")
            mb_input = f"https://musicbrainz.org/release-group/{rgid}" if rgid else ""
            aldir = _album_source_folder(album_id)
            mbid = _resolve_album_release_for_import(
                mb_input, _s(row["albumartist"] or ""), _s(row["album"] or ""),
                _s(row["year"] or ""), int(row["track_count"] or 0), log,
                source_folder=aldir, existing_album_id=album_id,
            )
            if not mbid:
                raise RuntimeError(
                    "No confident MusicBrainz match found for this partial import — needs manual review."
                )
        result = _repair_album_mbid_sticking_once(
            album_id, mbid, log, repair_tracks=True, write_tags=True, cancel_event=cancel_event,
        )
        _invalidate_lib_cache()
        log.append(f"Repaired partial import for album_id {album_id} ({label}).")
        return {**result, "mb_albumid": mbid}

    job = jobs.start_python(
        _do,
        label=f"Repair partial import album {album_id}",
        metadata={"type": "rgid-repair-partial-import", "album_id": album_id},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/clean/remove-orphaned-items")
def clean_remove_orphaned_items():
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get("item_ids") or []
    dry_run = payload.get("dry_run", True) is not False
    if not isinstance(raw_ids, list):
        return jsonify({"ok": False, "error": "item_ids must be a list"}), 400
    item_ids = [int(i) for i in raw_ids if str(i).isdigit()]
    if not item_ids:
        return jsonify({"ok": False, "code": "empty_selection",
                        "error": "item_ids is empty; nothing to remove."}), 400

    def _do(log, cancel_event=None):
        log.append(f"{'Dry run: ' if dry_run else ''}Removing DB rows of items whose files are missing "
                   "(files are never deleted)")
        result = _clean_remove_orphaned_items(item_ids, dry_run=dry_run, log=log)
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "Orphaned-item cleanup was refused.")
        return result

    job = jobs.start_python(_do, label="Clean orphaned library items")
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/clean/remove-empty-albums")
def clean_remove_empty_albums():
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get("album_ids") or []
    dry_run = payload.get("dry_run", True) is not False
    if not isinstance(raw_ids, list):
        return jsonify({"ok": False, "error": "album_ids must be a list"}), 400
    album_ids = [int(i) for i in raw_ids if str(i).isdigit()]

    def _do(log, cancel_event=None):
        log.append(f"{'Dry run: ' if dry_run else ''}Removing empty album rows")
        return _clean_remove_empty_albums(album_ids, dry_run=dry_run, log=log)

    job = jobs.start_python(_do, label="Clean empty album rows")
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/clean/album-tracks/scan")
def clean_album_tracks_scan():
    payload = request.get_json(silent=True) or {}
    album_id = int(payload.get("album_id") or 0)
    limit = max(1, min(int(payload.get("limit") or 75), 500))
    use_ai = bool(payload.get("use_ai", True))
    use_fingerprint = bool(payload.get("use_fingerprint", True))
    fingerprint_limit = max(1, min(int(payload.get("fingerprint_limit") or 80), 200))

    try:
        if album_id:
            try:
                alb = composite_workflows.get_album(album_id)
                rows = [alb] if alb else []
            except Exception:
                rows = []
        else:
            rows = composite_workflows.find_albums_with_mbid(limit=limit, sort="desc")
    except Exception as ex:
        _app_logger.warning("Could not read MusicBrainz-tagged albums: %s", type(ex).__name__)
        return jsonify({"ok": False, "error": "Could not read albums."}), 500
    if album_id and not rows:
        return jsonify({"ok": False, "error": f"Album {album_id} not found"}), 404
    if not rows:
        return jsonify({"ok": False, "error": "No MusicBrainz-tagged albums found"}), 404

    album_rows = [dict(r) for r in rows]

    def _do(log, cancel_event=None):
        results = []
        remove_count = 0
        review_count = 0
        log.append(f"Scanning {len(album_rows)} MusicBrainz-tagged album(s)")
        log.append(f"AI review: {'on' if use_ai else 'off'}; audio fingerprint: {'on' if use_fingerprint else 'off'}")
        for idx, row in enumerate(album_rows, start=1):
            if cancel_event is not None and cancel_event.is_set():
                log.append("Cancelled")
                break
            log.append(f"[{idx}/{len(album_rows)}] {row.get('albumartist','')} - {row.get('album','')} (album_id {row.get('id')})")
            res = _scan_album_track_integrity(
                row,
                use_ai=use_ai,
                use_fingerprint=use_fingerprint,
                fingerprint_limit=fingerprint_limit,
                log=log,
            )
            if not res:
                log.append("  OK")
                continue
            rmc = len(res.get("remove_candidates") or [])
            rvc = len(res.get("review_candidates") or [])
            log.append(f"  Found {rmc} remove candidate(s), {rvc} review candidate(s)")
            remove_count += rmc
            review_count += rvc
            results.append(res)
        return {
            "ok": True,
            "albums_scanned": len(album_rows),
            "problem_albums": results,
            "problem_count": len(results),
            "remove_count": remove_count,
            "review_count": review_count,
        }

    label = f"Clean album tracks: album {album_id}" if album_id else "Clean album tracks scan"
    job = jobs.start_python(_do, label=label)
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/clean/album-tracks/remove")
def clean_album_tracks_remove():
    payload = request.get_json(silent=True) or {}
    album_id = int(payload.get("album_id") or 0)
    item_ids = payload.get("item_ids") or []
    dry_run = payload.get("dry_run", True) is not False
    # Files are never deleted by this route: selected tracks go to the engine
    # quarantine (restorable). delete_files is accepted for compatibility only.
    delete_files = False
    clean_empty_folders = False
    if not album_id:
        return jsonify({"ok": False, "error": "album_id required"}), 400
    if not isinstance(item_ids, list) or not item_ids:
        return jsonify({"ok": False, "error": "item_ids required"}), 400
    if not dry_run and payload.get("confirm") is not True:
        return jsonify({"ok": False, "code": "confirmation_required",
                        "error": "Removing tracks needs explicit confirmation (confirm: true) after a dry run."}), 400

    if dry_run:
        log: List[str] = []
        try:
            summary = _remove_album_track_items(
                album_id,
                item_ids,
                dry_run=True,
                delete_files=delete_files,
                clean_empty_folders=clean_empty_folders,
                log=log,
            )
        except RuntimeError as exc:
            _app_logger.warning("Album track removal preview failed for album %s: %s", album_id, exc)
            return jsonify({"ok": False, "dry_run": True, "code": "preview_failed",
                            "error": "Could not preview the track removal; see server logs.", "log": log}), 400
        return jsonify({"ok": True, "dry_run": True, "summary": summary, "log": log})

    def _do(log, cancel_event=None):
        return _remove_album_track_items(
            album_id,
            item_ids,
            dry_run=False,
            delete_files=delete_files,
            clean_empty_folders=clean_empty_folders,
            log=log,
            approved_by="operator confirmed track removal (/api/clean/album-tracks/remove)",
        )

    job = jobs.start_python(_do, label=f"Remove bad tracks: album {album_id}")
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/clean/album-tracks/remove-batch")
def clean_album_tracks_remove_batch():
    payload = request.get_json(silent=True) or {}
    groups = payload.get("groups") or []
    dry_run = payload.get("dry_run", True) is not False
    delete_files = False  # never deletes: tracks go to the engine quarantine
    clean_empty_folders = False
    if not isinstance(groups, list) or not groups:
        return jsonify({"ok": False, "error": "groups required"}), 400
    if not dry_run and payload.get("confirm") is not True:
        return jsonify({"ok": False, "code": "confirmation_required",
                        "error": "Removing tracks needs explicit confirmation (confirm: true) after a dry run."}), 400

    clean_groups: List[Dict[str, Any]] = []
    for group in groups:
        try:
            album_id = int((group or {}).get("album_id") or 0)
        except Exception:
            album_id = 0
        item_ids = (group or {}).get("item_ids") or []
        if album_id <= 0 or not isinstance(item_ids, list):
            continue
        ids = []
        for raw in item_ids:
            try:
                iid = int(raw)
            except Exception:
                continue
            if iid > 0:
                ids.append(iid)
        if ids:
            clean_groups.append({"album_id": album_id, "item_ids": sorted(set(ids))})
    if not clean_groups:
        return jsonify({"ok": False, "error": "No valid album/item groups"}), 400

    def _do(log, cancel_event=None):
        summaries = []
        totals = {
            "albums": 0,
            "removed_db": 0,
            "deleted_files": 0,
            "folders_removed": 0,
            "album_deleted": 0,
        }
        log.append(
            f"{'Dry run: ' if dry_run else ''}Removing bad tracks from "
            f"{len(clean_groups)} album(s); folders will be preserved."
        )
        for idx, group in enumerate(clean_groups, start=1):
            if cancel_event is not None and cancel_event.is_set():
                log.append("Cancelled")
                break
            album_id = int(group["album_id"])
            item_ids = group["item_ids"]
            log.append(f"[{idx}/{len(clean_groups)}] album_id {album_id}: {len(item_ids)} track(s)")
            try:
                summary = _remove_album_track_items(
                    album_id,
                    item_ids,
                    dry_run=dry_run,
                    delete_files=delete_files,
                    clean_empty_folders=clean_empty_folders,
                    log=log,
                    approved_by="" if dry_run else "operator confirmed batch track removal",
                )
            except RuntimeError as exc:
                log.append(f"  album_id {album_id}: {exc}")
                summaries.append({"album_id": album_id, "ok": False, "error": str(exc)})
                totals["failed"] = totals.get("failed", 0) + 1
                continue
            summaries.append({"album_id": album_id, **summary})
            totals["quarantined_files"] = totals.get("quarantined_files", 0) + int(summary.get("quarantined_files") or 0)
            totals["albums"] += 1
            totals["removed_db"] += int(summary.get("removed_db") or 0)
            totals["deleted_files"] += int(summary.get("deleted_files") or 0)
            totals["folders_removed"] += int(summary.get("folders_removed") or 0)
            if summary.get("album_deleted"):
                totals["album_deleted"] += 1
        log.append(
            "Done: "
            f"{totals.get('quarantined_files', 0)} file(s) {'would be ' if dry_run else ''}quarantined (kept, restorable), "
            f"{totals['removed_db']} DB row(s), "
            f"{totals.get('failed', 0)} album(s) refused."
        )
        return {"ok": not totals.get("failed"), "dry_run": dry_run, "totals": totals, "albums": summaries}

    job = jobs.start_python(_do, label="Remove bad tracks: batch")
    return jsonify({"ok": True, "job_id": job.job_id})


@app.get("/api/clean/folder-placeholder/review")
def review_folder_placeholder_api():
    source, error = _folder_cleanup_path(request.args.get("source_path"))
    if error or source is None:
        return jsonify({"ok": False, "error": error or "Invalid source path"}), 400
    proposed_raw = request.args.get("target_path") or request.args.get("proposed_path")
    proposed: Optional[Path] = None
    if proposed_raw:
        proposed, proposed_error = _folder_cleanup_path(proposed_raw)
        if proposed_error:
            return jsonify({"ok": False, "error": proposed_error}), 400
    return jsonify(_folder_cleanup_review_for(source, proposed))


@app.post("/api/clean/folder-placeholder/preview-merge")
def preview_folder_placeholder_merge_api():
    payload = request.get_json(silent=True) or {}
    source, source_error = _folder_cleanup_path(payload.get("source_path"))
    target, target_error = _folder_cleanup_path(payload.get("target_path"))
    if source_error or source is None:
        return jsonify({"ok": False, "error": source_error or "Invalid source path"}), 400
    if target_error or target is None:
        return jsonify({"ok": False, "error": target_error or "Invalid target path"}), 400
    return jsonify(_folder_cleanup_merge_preview(source, target))


@app.post("/api/clean/folder-placeholder/apply")
def apply_folder_placeholder_action_api():
    payload = request.get_json(silent=True) or {}
    action = _s(payload.get("action")).strip()
    if payload.get("confirmed") is not True:
        return jsonify({"ok": False, "error": "Confirmation is required before applying cleanup"}), 400
    source, source_error = _folder_cleanup_path(payload.get("source_path"))
    if source_error or source is None:
        return jsonify({"ok": False, "error": source_error or "Invalid source path"}), 400
    if action != "mark_reviewed" and action != "skip" and _folder_cleanup_is_approved_root(source):
        return jsonify({"ok": False, "error": "Refusing to modify the music library root itself"}), 400

    class _PlanApplyOk:
        __slots__ = ("plan_res", "apply_res", "op_id")

        def __init__(self, plan_res, apply_res, op_id):
            self.plan_res = plan_res
            self.apply_res = apply_res
            self.op_id = op_id

    def _engine_reject(result: Dict[str, Any], *, default_error: str, target: Optional[Path] = None,
                       status: int = 400, blocking_reasons: Optional[List[str]] = None):
        code = _s(result.get("code") or result.get("error_code")).strip()
        refs = result.get("references") if isinstance(result, dict) else None
        ref_count = len(refs) if isinstance(refs, list) else 0
        message = default_error
        if code == "folder_cleanup_db_references":
            if action in {"remove_empty_source", "remove_empty"}:
                message = f"{ref_count} DB item(s) tracked under source; cannot remove"
            elif action in {"safe_rename", "rename_folder"}:
                message = f"{ref_count} DB item(s) tracked under source - use beet move instead of plain rename"
            else:
                message = "Merge is not safe to apply"
        elif code == "folder_cleanup_not_empty":
            message = "Folder is not empty; cannot remove"
        elif code in {"folder_cleanup_not_directory", "folder_cleanup_target_missing"}:
            message = "Source folder does not exist" if "source" in default_error.lower() else "Target folder does not exist"
        elif code == "folder_cleanup_target_parent_missing":
            message = "Target subfolder does not exist; cleanup apply will not create folders"
        elif code == "folder_cleanup_target_exists":
            message = f"Target folder already exists: {target.name}" if target is not None else "Target folder already exists"
            status = 409
        elif code in {"folder_cleanup_toctou_mismatch", "folder_cleanup_noop"}:
            status = 409
            if code == "folder_cleanup_noop":
                message = "No source-only files are available to merge"
            else:
                message = "Stale preview; rerun preview before applying"
        elif code in {"folder_cleanup_path_out_of_root", "folder_cleanup_symlink_rejected", "folder_cleanup_root_refused"}:
            message = "Access denied for folder cleanup path"
            status = 403 if code != "folder_cleanup_root_refused" else 400
        body: Dict[str, Any] = {"ok": False, "error": message, "code": code or "folder_cleanup_rejected"}
        if blocking_reasons:
            body["blocking_reasons"] = blocking_reasons
        return jsonify(body), status

    def _engine_exception(exc: Exception, *, phase: str, operation_id: str = ""):
        if isinstance(exc, BeetsUnavailableError):
            _app_logger.warning("Folder cleanup %s failed because engine is unavailable: %s", phase, type(exc).__name__)
            return jsonify({
                "ok": False,
                "error": "Beets engine is unavailable; folder cleanup was not performed.",
                "error_code": "ENGINE_OFFLINE",
                "code": "ENGINE_OFFLINE",
                "operation_id": operation_id,
            }), 503
        _app_logger.warning("Folder cleanup %s rejected by engine: %s", phase, getattr(exc, "error_code", "") or type(exc).__name__)
        return jsonify({
            "ok": False,
            "error": "Beets engine rejected folder cleanup.",
            "code": getattr(exc, "error_code", "") or "beets_error",
            "operation_id": operation_id,
        }), getattr(exc, "status_code", 409) or 409

    def _plan_and_apply(engine_payload: Dict[str, Any], *, target: Optional[Path] = None,
                        default_error: str = "Folder cleanup was rejected"):
        # Bug fix: error responses from _engine_reject()/_engine_exception()
        # are themselves plain (Response, status_code) tuples, which is
        # indistinguishable from a bare `isinstance(result, tuple)` check
        # against the 3-item success tuple below -- every rejected/failed
        # plan or apply used to unpack a 2-tuple into 3 names and raise an
        # uncaught ValueError instead of returning the intended structured
        # error JSON. Wrap the success case in a distinct, unambiguous type
        # so callers can tell success from a Flask error response reliably.
        try:
            plan_res = composite_workflows.plan_folder_cleanup(engine_payload)
        except (BeetsUnavailableError, BeetsError) as exc:
            return _engine_exception(exc, phase="plan")
        if not plan_res.get("ok"):
            return _engine_reject(plan_res, default_error=default_error, target=target)
        op_id = _s(plan_res.get("operation_id")).strip()
        if not op_id:
            return jsonify({"ok": True, "action": engine_payload.get("action"), "changed_count": 0})
        try:
            apply_res = composite_workflows.apply_folder_cleanup(op_id)
        except (BeetsUnavailableError, BeetsError) as exc:
            return _engine_exception(exc, phase="apply", operation_id=op_id)
        if not apply_res.get("ok"):
            return _engine_reject(apply_res, default_error="Folder cleanup apply failed", target=target,
                                  status=409 if apply_res.get("mutated") else 400)
        return _PlanApplyOk(plan_res, apply_res, op_id)

    if action in {"remove_empty_source", "remove_empty"}:
        preview_token = _s(payload.get("preview_token")).strip()
        if not preview_token:
            return jsonify({"ok": False, "error": "Preview token is required; rerun preview before applying"}), 400
        result = _plan_and_apply({"action": "remove_empty", "source": str(source)}, default_error="Source folder does not exist")
        if not isinstance(result, _PlanApplyOk):
            return result
        _plan_res, apply_res, op_id = result.plan_res, result.apply_res, result.op_id
        removed_folders = [_s(p) for p in apply_res.get("removed_dirs") or ([] if not apply_res.get("mutated") else [str(source)]) if _s(p)]
        return jsonify({
            "ok": True,
            "action": "remove_empty_source",
            "removed": removed_folders[0] if removed_folders else str(source),
            "removed_folders": removed_folders,
            "changed_count": len(removed_folders),
            "operation_id": op_id,
        })

    if action in {"safe_rename", "rename_folder"}:
        target_raw = _s(payload.get("target_path") or payload.get("proposed_path")).strip()
        if not target_raw:
            return jsonify({"ok": False, "error": "target_path is required for safe_rename"}), 400
        target, target_error = _folder_cleanup_path(target_raw)
        if target_error or target is None:
            return jsonify({"ok": False, "error": target_error or "Invalid target path"}), 400
        result = _plan_and_apply(
            {"action": "safe_rename", "source": str(source), "target": str(target)},
            target=target,
            default_error="Source folder does not exist",
        )
        if not isinstance(result, _PlanApplyOk):
            return result
        _plan_res, apply_res, op_id = result.plan_res, result.apply_res, result.op_id
        moved = apply_res.get("moved_records") or [{"source": str(source), "target": str(target)}]
        return jsonify({
            "ok": True,
            "action": "safe_rename",
            "renamed_from": str(source),
            "renamed_to": str(target),
            "moved": moved,
            "changed_count": len(moved),
            "operation_id": op_id,
        })

    if action in {"merge_source_files", "merge"}:
        preview_token = _s(payload.get("preview_token")).strip()
        if not preview_token:
            return jsonify({"ok": False, "error": "Preview token is required; rerun preview before applying"}), 400
        target, target_error = _folder_cleanup_path(payload.get("target_path"))
        if target_error or target is None:
            return jsonify({"ok": False, "error": target_error or "Invalid target path"}), 400
        preview = _folder_cleanup_merge_preview(source, target)
        if preview_token != preview.get("preview_token"):
            return jsonify({"ok": False, "error": "Stale preview; rerun preview before applying"}), 409
        if not preview.get("safe"):
            return jsonify({
                "ok": False,
                "error": "Merge is not safe to apply",
                "blocking_reasons": preview.get("blocking_reasons", []),
            }), 400
        result = _plan_and_apply(
            {"action": "merge_source_files", "source": str(source), "target": str(target)},
            target=target,
            default_error="Merge is not safe to apply",
        )
        if not isinstance(result, _PlanApplyOk):
            return result
        _plan_res, apply_res, op_id = result.plan_res, result.apply_res, result.op_id
        moved = apply_res.get("moved_records") or []
        removed_folders = [_s(p) for p in apply_res.get("removed_dirs") or [] if _s(p)]
        return jsonify({
            "ok": True,
            "action": "merge_source_files",
            "moved": moved,
            "removed_folders": removed_folders,
            "changed_count": len(moved) + len(removed_folders),
            "operation_id": op_id,
        })

    if action in {"mark_reviewed", "skip"}:
        return jsonify({"ok": True, "action": action, "changed_count": 0})
    return jsonify({"ok": False, "error": f"Unsupported cleanup action: {action}"}), 400


@app.post("/api/clean/folder-placeholder/apply-safe-renames")
def apply_safe_folder_placeholder_renames_job():
    """Start a background job to rename all safe folder placeholder rows."""
    payload = request.get_json(silent=True) or {}
    source_paths: Optional[List[str]] = payload.get("source_paths")

    def _do(log, cancel_event=None, update_state=None):
        rows_to_rename: List[Dict[str, Any]] = []

        if source_paths is not None:
            log.append(f"Validating {len(source_paths)} selected folder(s) for safe rename...")
            for src_str in source_paths:
                src, err = _folder_cleanup_path(src_str)
                if err or src is None:
                    log.append(f"  SKIP (invalid path): {src_str}")
                    continue
                if _folder_cleanup_is_approved_root(src):
                    log.append(f"  SKIP (refusing to rename the music library root): {src_str}")
                    continue
                name = src.name
                clean_name = _LITERAL_PLACEHOLDER_RE.sub("", name)
                clean_name = _UNRESOLVED_TEMPLATE_TOKEN_RE.sub("", clean_name)
                clean_name = re.sub(r"\s+", " ", clean_name).strip().strip(" -_")
                if not clean_name or clean_name == name:
                    log.append(f"  SKIP (no clean name): {src.name}")
                    continue
                rows_to_rename.append({"folder": str(src), "proposed_folder": str(src.parent / clean_name)})
        else:
            log.append(f"Scanning {MUSIC_ROOT} for safe folder rename candidates...")
            scan_meta: Dict[str, Any] = {}
            all_rows = _scan_folder_name_placeholders(
                progress=update_state, cancel_event=cancel_event, scan_meta=scan_meta
            )
            rows_to_rename = [
                r for r in all_rows
                if r.get("safe") and not r.get("target_exists")
                and int(r.get("db_item_count") or 0) == 0
                and r.get("proposed_folder")
            ]
            log.append(f"Scan complete. {len(rows_to_rename)} safe rename candidate(s) found.")

        renamed = 0
        skipped = 0
        failed = 0
        total = len(rows_to_rename)

        for i, row in enumerate(rows_to_rename):
            if cancel_event and cancel_event.is_set():
                log.append("Job cancelled.")
                break

            src = Path(row["folder"])
            dst = Path(row["proposed_folder"])

            if not _path_under(src, MUSIC_ROOT) or not _path_under(dst, MUSIC_ROOT):
                log.append(f"  [{i + 1}/{total}] SKIP (path outside music library): {src.name}")
                skipped += 1
                continue
            if _folder_cleanup_is_approved_root(src):
                log.append(f"  [{i + 1}/{total}] SKIP (refusing to rename the music library root): {src.name}")
                skipped += 1
                continue
            if not src.exists() or not src.is_dir():
                log.append(f"  [{i + 1}/{total}] SKIP (source gone): {src.name}")
                skipped += 1
                continue
            if dst.exists():
                log.append(f"  [{i + 1}/{total}] SKIP (target exists): {dst.name}")
                skipped += 1
                continue
            db_items_check = _folder_cleanup_db_items(src)
            if db_items_check:
                log.append(f"  [{i + 1}/{total}] SKIP (DB tracked, {len(db_items_check)} items): {src.name}")
                skipped += 1
                continue

            try:
                plan_res = composite_workflows.plan_folder_cleanup({
                    "action": "safe_rename",
                    "source": str(src),
                    "target": str(dst),
                })
                if not plan_res.get("ok"):
                    raise RuntimeError(plan_res.get("error") or "Folder cleanup planning failed")
                apply_res = composite_workflows.apply_folder_cleanup(plan_res["operation_id"])
                if not apply_res.get("ok"):
                    raise RuntimeError(apply_res.get("error") or "Folder cleanup execution failed")

                log.append(f"  [{i + 1}/{total}] RENAMED: {src.name} (engine controlled)")
                log.append(f"           → {dst.name}")
                renamed += 1
            except Exception as exc:
                log.append(f"  [{i + 1}/{total}] FAILED: {src.name}: {exc}")
                failed += 1

            if update_state:
                update_state({
                    "category": "Cleanup",
                    "current_task": "Applying safe folder renames",
                    "current_path": str(src),
                    "scanned_count": i + 1,
                    "total_count": total,
                    "renamed": renamed,
                    "skipped": skipped,
                    "failed": failed,
                })

        summary = f"Safe rename complete: {renamed} renamed, {skipped} skipped, {failed} failed."
        log.append(summary)
        return {
            "ok": True,
            "renamed": renamed,
            "skipped": skipped,
            "failed": failed,
            "total": total,
            "summary": summary,
        }

    job = jobs.start_python(
        _do,
        label="Apply safe folder placeholder renames",
        metadata={"type": "folder-placeholder-apply-safe"},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.get("/api/clean/album-folders/report")
def clean_album_folders_report():
    report: Dict[str, Any] = {}
    exists = False
    try:
        if ALBUM_FOLDER_CLEANUP_LAST_FILE.exists():
            loaded = json.loads(ALBUM_FOLDER_CLEANUP_LAST_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                report = loaded
                exists = True
    except Exception as exc:
        _app_logger.warning("Could not read album-folder cleanup report: %s", type(exc).__name__)
        return jsonify({"ok": False, "error": "Could not read album-folder cleanup report."}), 500
    return jsonify({"ok": True, "exists": exists, "report": report})


@app.post("/api/clean/album-folders/scan")
def clean_album_folders_scan():
    running = _album_cleanup_running_job()
    if running:
        return jsonify({"ok": True, "job_id": running.job_id, "already_running": True})

    def _do(log, cancel_event=None, update_state=None):
        if not _ALBUM_FOLDER_CLEANUP_LOCK.acquire(blocking=False):
            raise RuntimeError("Album folder cleanup is already running")
        try:
            log.append(f"Scanning album folders under {MUSIC_ROOT}")
            report = _album_folder_cleanup_plan(MUSIC_ROOT, progress=update_state, cancel_event=cancel_event)
            summary = report.get("summary") or {}
            log.append(
                "Album folder scan complete: "
                f"{summary.get('issues_found', 0)} issue(s), "
                f"{summary.get('safe_fixes', 0)} safe, "
                f"{summary.get('needs_review', 0)} need review, "
                f"{summary.get('blocked', 0)} blocked."
            )
            for issue in report.get("issues", [])[:40]:
                log.append(
                    f"  [{issue.get('safety')}] {issue.get('artist')} - {issue.get('album')} "
                    f"({', '.join(issue.get('issue_types') or [])})"
                )
                if issue.get("release_group_id"):
                    log.append(f"    Release Group ID: {issue.get('release_group_id')}")
                if issue.get("canonical_folder"):
                    log.append(f"    Canonical: {issue.get('canonical_folder')}")
            _album_cleanup_save_report(report, log)
            return report
        finally:
            _ALBUM_FOLDER_CLEANUP_LOCK.release()

    job = jobs.start_python(
        _do,
        label="Scan album folders",
        metadata={"type": "album-folder-cleanup-scan", "category": "Cleanup"},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/clean/album-folders/apply-safe")
def clean_album_folders_apply_safe():
    running = _album_cleanup_running_job()
    if running:
        return jsonify({"ok": True, "job_id": running.job_id, "already_running": True})

    def _do(log, cancel_event=None, update_state=None):
        if not _ALBUM_FOLDER_CLEANUP_LOCK.acquire(blocking=False):
            raise RuntimeError("Album folder cleanup is already running")
        try:
            log.append(f"Applying safe album-folder cleanup under {MUSIC_ROOT}")
            report = _album_folder_cleanup_apply_safe(MUSIC_ROOT, log, cancel_event=cancel_event, progress=update_state)
            summary = report.get("summary") or {}
            log.append(
                "Safe album-folder cleanup complete: "
                f"{summary.get('files_moved', 0)} file(s) moved, "
                f"{summary.get('artwork_moved', 0)} artwork file(s) moved, "
                f"{summary.get('duplicate_files_quarantined', 0)} duplicate(s) quarantined, "
                f"{summary.get('folders_deleted', 0)} folder(s) removed, "
                f"{summary.get('errors', 0)} error(s)."
            )
            _album_cleanup_save_report(report, log)
            return report
        finally:
            _ALBUM_FOLDER_CLEANUP_LOCK.release()

    job = jobs.start_python(
        _do,
        label="Apply safe album folder cleanup",
        metadata={"type": "album-folder-cleanup-apply-safe", "category": "Cleanup"},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.get("/api/clean/root-folders/report")
def clean_root_folders_report():
    report: Dict[str, Any] = {}
    exists = False
    try:
        if ROOT_FOLDER_REPAIR_LAST_FILE.exists():
            loaded = json.loads(ROOT_FOLDER_REPAIR_LAST_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                report = loaded
                exists = True
    except Exception as exc:
        _app_logger.warning("Could not read root-folder repair report: %s", type(exc).__name__)
        return jsonify({"ok": False, "error": "Could not read root-folder repair report."}), 500
    return jsonify({"ok": True, "exists": exists, "report": report})


@app.post("/api/clean/root-folders/scan")
def clean_root_folders_scan():
    running = _root_folder_repair_running_job()
    if running:
        return jsonify({"ok": True, "job_id": running.job_id, "already_running": True})

    def _do(log, cancel_event=None, update_state=None):
        if not _ROOT_FOLDER_REPAIR_LOCK.acquire(blocking=False):
            raise RuntimeError("Root folder repair is already running")
        try:
            log.append(f"Scanning {MUSIC_ROOT} for misplaced root-level album/singleton folders")
            report = _root_folder_repair_scan(MUSIC_ROOT)
            summary = report.get("summary") or {}
            log.append(
                "Root folder scan complete: "
                f"{summary.get('empty_count', 0)} empty folder(s), "
                f"{summary.get('shallow_folder_count', 0)} shallow folder(s) "
                f"({summary.get('shallow_item_count', 0)} tracked item(s)), "
                f"{summary.get('orphaned_count', 0)} untracked folder(s) needing review."
            )
            for name in report.get("empty_folders", [])[:40]:
                log.append(f"  [empty] {name}")
            for entry in report.get("shallow_folders", [])[:40]:
                log.append(f"  [shallow] {entry.get('name')} ({entry.get('item_count')} item(s))")
            for entry in report.get("orphaned_folders", [])[:40]:
                log.append(f"  [needs review] {entry.get('name')} ({entry.get('file_count')} file(s))")
            _root_folder_repair_save_report(report, log)
            return report
        finally:
            _ROOT_FOLDER_REPAIR_LOCK.release()

    job = jobs.start_python(
        _do,
        label="Scan root-level folders",
        metadata={"type": "root-folder-repair-scan", "category": "Cleanup"},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/clean/root-folders/apply-safe")
def clean_root_folders_apply_safe():
    running = _root_folder_repair_running_job()
    if running:
        return jsonify({"ok": True, "job_id": running.job_id, "already_running": True})

    def _do(log, cancel_event=None, update_state=None):
        if not _ROOT_FOLDER_REPAIR_LOCK.acquire(blocking=False):
            raise RuntimeError("Root folder repair is already running")
        try:
            log.append(f"Repairing misplaced root-level folders under {MUSIC_ROOT}")
            report = _root_folder_repair_apply_safe(log, cancel_event=cancel_event, progress=update_state)
            summary = report.get("summary") or {}
            log.append(
                "Root folder repair complete: "
                f"{summary.get('empty_folders_removed', 0)} empty folder(s) removed, "
                f"{summary.get('items_moved', 0)} item(s) re-homed, "
                f"{summary.get('items_failed', 0)} item(s) failed, "
                f"{summary.get('orphaned_queued', 0)} folder(s) queued for review."
            )
            _root_folder_repair_save_report(report, log)
            return report
        finally:
            _ROOT_FOLDER_REPAIR_LOCK.release()

    job = jobs.start_python(
        _do,
        label="Repair root-level folders",
        metadata={"type": "root-folder-repair-apply-safe", "category": "Cleanup"},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/clean/album-folders/apply-issue")
def clean_album_folders_apply_issue():
    payload = request.get_json(silent=True) or {}
    issue_id = _s(payload.get("issue_id")).strip()
    if not issue_id:
        return jsonify({"ok": False, "error": "issue_id is required"}), 400
    if payload.get("confirmed") is not True:
        return jsonify({"ok": False, "error": "Confirmation is required before applying cleanup"}), 400
    running = _album_cleanup_running_job()
    if running:
        return jsonify({"ok": True, "job_id": running.job_id, "already_running": True})

    def _do(log, cancel_event=None, update_state=None):
        if not _ALBUM_FOLDER_CLEANUP_LOCK.acquire(blocking=False):
            raise RuntimeError("Album folder cleanup is already running")
        try:
            log.append(f"Applying album-folder cleanup issue {issue_id}")
            plan = _album_folder_cleanup_plan(MUSIC_ROOT, progress=update_state, cancel_event=cancel_event)
            scan_root = Path(plan.get("root") or MUSIC_ROOT).resolve(strict=False)
            issue = next((row for row in plan.get("issues", []) if _s(row.get("id")) == issue_id), None)
            if not issue:
                raise RuntimeError("Cleanup issue was not found; rerun the scan")
            summary = dict(plan.get("summary") or {})
            summary.update({
                "dry_run": False,
                "manual_issue_selected": issue_id,
                "files_moved": 0,
                "artwork_moved": 0,
                "duplicate_files_quarantined": 0,
                "folders_deleted": 0,
                "db_paths_updated": 0,
                "errors": 0,
                "completed": 0,
            })
            operations: List[Dict[str, Any]] = []
            trash_root = METADATA_CACHE_ROOT / "album-folder-cleanup-trash" / time.strftime("%Y%m%d-%H%M%S")
            if update_state:
                update_state({
                    "category": "Cleanup",
                    "current_task": "Applying selected album-folder cleanup",
                    "current_item": f"{issue.get('artist')} - {issue.get('album')}",
                    "total_count": 1,
                    "scanned_count": 1,
                })
            applied_issue = _album_cleanup_apply_issue(issue, scan_root, trash_root, log, summary, operations)
            errors = list(plan.get("errors") or [])
            if applied_issue.get("status") == "Blocked":
                errors.extend(_s(reason) for reason in applied_issue.get("blocking_reasons") or [])
            if operations:
                _invalidate_lib_cache()
            report = {
                "ok": True,
                "dry_run": False,
                "root": str(scan_root),
                "summary": summary,
                "final_summary": summary,
                "issues": [applied_issue],
                "operations": operations,
                "errors": errors,
                "rollback": {
                    "available": False,
                    "trash_root": str(trash_root) if trash_root.exists() else "",
                    "note": "Duplicate/rejected files were moved to cleanup trash instead of being permanently deleted.",
                },
            }
            _album_cleanup_save_report(report, log)
            return report
        finally:
            _ALBUM_FOLDER_CLEANUP_LOCK.release()

    job = jobs.start_python(
        _do,
        label="Apply selected album folder cleanup",
        metadata={"type": "album-folder-cleanup-apply-issue", "category": "Cleanup", "issue_id": issue_id},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/clean/artist-folders/scan")
def clean_artist_folders_scan():
    payload = request.get_json(silent=True) or {}
    root_path, root_error = _artist_folder_repair_root(payload.get("root") or str(MUSIC_ROOT))
    if root_path is None:
        return jsonify({"ok": False, "error": root_error}), 400
    root = str(root_path)

    def _do(log, cancel_event=None):
        log.append(f"Scanning artist folders: {root}")
        if cancel_event and cancel_event.is_set():
            log.append("Cancelled before scan started.")
            raise RuntimeError("cancelled")
        groups = _scan_artist_folder_groups(root, use_musicbrainz=True)
        if cancel_event and cancel_event.is_set():
            log.append("Cancelled after scan completed.")
            raise RuntimeError("cancelled")
        name_count = sum(1 for g in groups if g.get("match_type") != "mb_artist_id")
        mbid_count = sum(1 for g in groups if g.get("match_type") == "mb_artist_id")
        log.append(
            f"Done: found {len(groups)} artist-folder group(s), "
            f"{name_count} name variant group(s), {mbid_count} MB ID group(s)."
        )
        return {
            "ok": True,
            "root": root,
            "groups": groups,
            "count": len(groups),
            "name_group_count": name_count,
            "mbid_group_count": mbid_count,
        }

    job = jobs.start_python(
        _do,
        label=f"Scan artist folders: {root_path.name or root}",
        metadata={"type": "artist-folder-scan", "path": root},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/clean/artist-folders/merge")
def clean_artist_folders_merge():
    payload = request.get_json(silent=True) or {}
    root_path, root_error = _artist_folder_repair_root(payload.get("root") or str(MUSIC_ROOT))
    if root_path is None:
        return jsonify({"ok": False, "error": root_error}), 400
    root = str(root_path)
    keys = payload.get("keys") or []
    dry_run = bool(payload.get("dry_run", True))
    if dry_run:
        log: List[str] = []
        summary = _apply_artist_folder_groups(root, keys, True, log)
        return jsonify({"ok": True, "dry_run": True, "summary": summary, "log": log})

    def _do(log, cancel_event=None):
        _apply_artist_folder_groups(root, keys, False, log)

    job = jobs.start_python(
        _do,
        label=f"Clean artist folders: {Path(root).name or root}",
        metadata={"type": "artist-folder-merge", "path": root},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/clean/artist-folders/stamp-mbid")
def clean_artist_folders_stamp_mbid():
    """Rename or merge artist folders to the canonical MB artist UUID name.

    Example: 'Celia Cruz' -> 'Celia Cruz (7b8e1188-...)'.
    Same-UUID folders merge into the canonical MusicBrainz artist folder.
    Only acts on folders where >=75% of albums share the same mb_albumartistid.
    Body: { root: str, dry_run: bool }
    Returns: { ok, renamed, skipped, log } for dry_run - or { ok, job_id } for apply.
    """
    payload = request.get_json(silent=True) or {}
    root_path, root_error = _artist_folder_repair_root(payload.get("root") or str(MUSIC_ROOT))
    if root_path is None:
        return jsonify({"ok": False, "error": root_error}), 400
    root_str = str(root_path)
    dry_run = bool(payload.get("dry_run", True))
    compact_log = bool(payload.get("compact_log", False))
    # Test-only acceptance failpoint passthrough (hotfix v0.1.17 pattern,
    # see composite_workflows.apply_artist_folder_reconcile()'s docstring): a
    # no-op in real deployments, since the engine only honors it when that
    # container was booted with BEETS_ACCEPTANCE_MODE=1.
    acceptance_failpoint = _s(payload.get("_acceptance_failpoint")) or None

    if dry_run:
        log: List[str] = []
        scan = _stamp_artist_folder_scan(root_path)
        if not scan.get("ok", True):
            # Independent review finding: an engine inventory failure must
            # never be reported as a successful dry run with zero
            # candidates -- fail closed with the real error instead.
            status_code = int(scan.get("status_code") or 0) or 502
            return jsonify({
                "ok": False, "error": scan.get("error"), "error_code": scan.get("error_code", ""),
            }), status_code
        candidates = scan["candidates"]
        skipped = scan["skipped"]
        _append_stamp_candidate_log(log, candidates)
        _append_stamp_skipped_log(log, skipped)
        log.append(f"Dry run: {len(candidates)} folder(s) would be renamed or merged; {len(skipped)} skipped.")
        return jsonify({"ok": True, "dry_run": True, "renamed": 0, "skipped": 0,
                        "candidates": len(candidates), "skipped_total": len(skipped), "log": log})

    def _do(log, cancel_event=None):
        scan = _stamp_artist_folder_scan(root_path)
        if not scan.get("ok", True):
            # Independent review finding: an engine inventory failure must
            # never be reported as "No artist folders need MB ID stamping" --
            # that message asserts a genuine, successful zero-candidate scan.
            # Raise so the job is reported failed (fail closed, resumable on
            # retry) instead of silently succeeding with nothing done.
            log.append(f"Artist folder MBID stamping: engine inventory scan failed ({scan.get('error')}); nothing was performed.")
            raise RuntimeError(f"Engine inventory scan failed: {scan.get('error')}")
        candidates = scan["candidates"]
        skipped = scan["skipped"]
        if not candidates:
            log.append("No artist folders need MB ID stamping.")
            _append_stamp_skipped_log(log, skipped, include_examples=False)
            return {"renamed": 0, "merged": 0, "skipped": 0}
        # Formerly performed direct disk merge via _merge_artist_dir_contents(; now delegated to composite_workflows.plan_artist_folder_reconcile
        log.append(f"Stamping MB IDs on {len(candidates)} artist folder(s)…")
        payload = {
            "root": str(root_path),
            "mode": "stamp_mbid",
        }
        # SEC-002 Wave 21 final review: no local in-process fallback -- see
        # the identical correction and rationale in _apply_artist_folder_groups.
        try:
            plan_res = composite_workflows.plan_artist_folder_reconcile(payload)
        except (BeetsUnavailableError, BeetsError) as ex:
            log.append("Engine unavailable; MBID stamping was not performed.")
            _app_logger.error("MBID stamping: engine unavailable: %s", ex)
            return {"renamed": 0, "merged": 0, "skipped": len(skipped)}
        except Exception as ex:
            log.append("Engine communication failed; MBID stamping was not performed.")
            _app_logger.error("MBID stamping: unexpected engine communication failure: %s", ex)
            return {"renamed": 0, "merged": 0, "skipped": len(skipped)}

        if not plan_res.get("ok"):
            log.append(f"Refusing to operate: {plan_res.get('error')}")
            return {"renamed": 0, "merged": 0, "skipped": len(skipped)}

        op_id = plan_res["operation_id"]
        apply_res = _apply_artist_folder_reconcile_resilient(
            op_id, log, cancel_event=cancel_event, log_prefix="MBID stamping",
            _acceptance_failpoint=acceptance_failpoint,
        )

        if not apply_res.get("ok"):
            log.append(f"Engine MBID stamping failed: {apply_res.get('error')}")
            return {"renamed": 0, "merged": 0, "skipped": len(skipped)}

        _invalidate_lib_cache()
        log.append(f"  [stamp] Delegated MBID stamping to engine (op_id={op_id})")
        summary = {
            "renamed": apply_res.get("moved_files", 0),
            "merged": apply_res.get("quarantined_files", 0),
            "skipped": len(skipped),
            "files_moved": apply_res.get("moved_files", 0),
            "duplicate_files_removed": apply_res.get("quarantined_files", 0),
            "artwork_collisions_resolved": 0,
            "filename_conflicts_preserved": 0,
            "folders_removed": apply_res.get("removed_dirs", 0),
        }
        log.append(
            f"Done: {summary['renamed']} renamed, {summary['merged']} merged, {summary['skipped']} skipped; "
            f"{summary['files_moved']} file(s) moved, {summary['duplicate_files_removed']} duplicate file(s) removed."
        )
        return summary

    job = jobs.start_python(
        _do,
        label=f"Stamp MB IDs on artist folders: {root_path.name or root_str}",
        metadata={"type": "stamp-mbid-folders", "path": root_str},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


# ── Unattended duplicate deletion: authorization and review ──────────────────


def _unattended_cleanup_status() -> Dict[str, Any]:
    report = _maintenance_load_last_report()
    duplicates = report.get("duplicates") if isinstance(report.get("duplicates"), dict) else {}
    return {
        "ok": True,
        "authorization": _dedup_authorization.load_authorization(WEB_MANAGER_DATA_DIR),
        "music_root": str(MUSIC_ROOT),
        "last_run_summary": duplicates.get("final_summary") or {},
        "proposal": duplicates.get("proposal") or [],
    }


@app.get("/api/dedup/unattended-cleanup")
def dedup_unattended_cleanup_status():
    """Authorization state plus the last scheduled run's review proposal."""
    return jsonify(_unattended_cleanup_status())


@app.post("/api/dedup/unattended-cleanup")
def dedup_unattended_cleanup_set():
    """Turn unattended duplicate deletion on or off. Enabling requires the
    exact confirmation phrase; disabling never does."""
    payload = request.get_json(silent=True) or {}
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"ok": False, "error": "enabled must be true or false"}), 400
    if enabled and payload.get("confirm") != _dedup_authorization.ENABLE_CONFIRMATION:
        return jsonify({
            "ok": False,
            "error": f'Enabling unattended deletion requires confirm="{_dedup_authorization.ENABLE_CONFIRMATION}".',
        }), 400
    _dedup_authorization.set_unattended_delete(
        WEB_MANAGER_DATA_DIR, enabled, actor=_transaction_user_label(), reason=_s(payload.get("reason")),
    )
    _app_logger.warning("Unattended duplicate deletion %s", "ENABLED" if enabled else "disabled")
    return jsonify(_unattended_cleanup_status())


@app.post("/api/dedup/reviewed-cleanup/plan")
def dedup_reviewed_cleanup_plan():
    """Preview removing reviewed duplicate copies (Plan step; nothing changes).

    Body: {"pairs": [{"delete_item_id", "keep_item_id"}, ...]}; without
    "pairs", every "delete" row of the last scan proposal. Each pair is
    re-verified against live Beets; drifted pairs are returned as skipped.
    Approve and apply through /api/transactions/<id>/approve and /apply."""
    payload = request.get_json(silent=True) or {}
    status = _unattended_cleanup_status()
    raw = payload.get("pairs")
    if raw is not None and not isinstance(raw, list):
        return jsonify({"ok": False, "error": "pairs must be a list"}), 400
    try:
        wanted = [(int(p["delete_item_id"]), int(p["keep_item_id"])) for p in raw] if raw else None
    except (TypeError, ValueError, KeyError):
        return jsonify({"ok": False, "error": "each pair needs integer delete_item_id and keep_item_id"}), 400
    pairs = _duplicate_cleanup.pairs_from_proposal(status["proposal"], wanted)
    known = {(p["delete_item_id"], p["keep_item_id"]) for p in pairs}
    pairs += [{"delete_item_id": d, "keep_item_id": k} for d, k in (wanted or []) if (d, k) not in known]
    if not pairs:
        return jsonify({"ok": False, "error": "no reviewed pairs to plan"}), 400
    try:
        # Operator-reviewed pairs only: a copy that is the sole item of a
        # duplicate row of the keeper's release may retire that row with it.
        res = _duplicate_cleanup.plan_reviewed_cleanup(pairs, reason=_s(payload.get("reason") or "Reviewed duplicate cleanup"),
                                                       allow_sibling_row_retire=True)
    except BeetsUnavailableError as exc:
        return jsonify({"ok": False, "error": "Beets engine is unavailable.", "code": exc.error_code or "beets_unavailable"}), 503
    return jsonify(res), (200 if res.get("ok") else 409)


@app.get("/api/library/album-duplicate-analysis")
def library_album_duplicate_analysis_last():
    """The last read-only duplicate-album analysis (see POST)."""
    report = _library_integrity.load_album_duplicate_analysis()
    return jsonify({"ok": report is not None, "report": report}), (200 if report is not None else 404)


@app.post("/api/library/album-duplicate-analysis")
def library_album_duplicate_analysis_run():
    """Read-only: group album rows by Release Group and propose a merge plan
    per group (retained row, item moves, overlapping slots, blockers).
    Nothing is merged; the report is saved for review."""
    try:
        return jsonify({"ok": True, "report": _library_integrity.run_album_duplicate_analysis()})
    except BeetsUnavailableError as exc:
        return jsonify({"ok": False, "error": "Beets engine is unavailable.", "code": exc.error_code or "beets_unavailable"}), 503


@app.get("/api/library/untracked-inventory")
def library_untracked_inventory_last():
    """Summary of the last read-only untracked-file inventory (see POST)."""
    summary = _library_integrity.load_untracked_inventory_summary()
    return jsonify({"ok": summary is not None, "summary": summary}), (200 if summary is not None else 404)


@app.post("/api/library/untracked-inventory")
def library_untracked_inventory_run():
    """Start the read-only inventory of audio files Beets does not track:
    one walk of the music root, AcoustID cache only (no API calls), evidence
    saved under the data directory. Nothing is deleted, imported or moved."""
    if _running_job_of_type({_library_integrity.INVENTORY_JOB_TYPE}):
        return jsonify({"ok": False, "error": "An untracked-file inventory is already running"}), 409
    job = _library_integrity.start_untracked_inventory_job()
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/library/album-duplicate-analysis/plan-merge")
def library_album_row_merge_plan():
    """Plan step (no mutation): merge the duplicate album rows of one
    Release Group -- only when the live analysis proves it deterministic.
    Approve/apply/rollback through /api/transactions/<id>/..."""
    payload = request.get_json(silent=True) or {}
    rg = _s(payload.get("release_group_id")).strip().lower()
    if not _MB_UUID_RE.match(rg):
        return jsonify({"ok": False, "error": "release_group_id must be a MusicBrainz id"}), 400
    try:
        res = _album_row_merge.plan_album_row_merge(rg)
    except BeetsUnavailableError as exc:
        return jsonify({"ok": False, "error": "Beets engine is unavailable.", "code": exc.error_code or "beets_unavailable"}), 503
    return jsonify(res), (200 if res.get("ok") else 409)


@app.get("/api/library/untracked-recovery/candidates")
def library_untracked_recovery_candidates():
    """Read-only page of the persisted inventory with the backend-owned
    action, eligibility, safety result and reason for every file."""
    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "limit/offset must be integers"}), 400
    res = _untracked_recovery.candidates(_s(request.args.get("category")).strip() or None, limit=limit, offset=offset)
    return jsonify(res), (200 if res.get("ok") else 404)


@app.get("/api/library/untracked-recovery/album-candidates")
def library_untracked_recovery_album_candidates():
    """Read-only: folders of untracked album files from the persisted
    inventory (no disk or tag read), largest first."""
    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "limit/offset must be integers"}), 400
    res = _untracked_recovery.album_candidates(limit=limit, offset=offset)
    return jsonify(res), (200 if res.get("ok") else 404)


@app.post("/api/library/untracked-recovery/plan")
def library_untracked_recovery_plan():
    """Plan step (no mutation) for one recovery action -- attach,
    track_for_replacement, quarantine or attach_album -- after identity is
    re-proven. Approve/apply/rollback through /api/transactions/<id>/...

    attach_album (paths = one album folder) proves every file against
    MusicBrainz and AcoustID, so it runs as a job whose result is the plan."""
    payload = request.get_json(silent=True) or {}
    paths = payload.get("paths")
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return jsonify({"ok": False, "error": "paths must be a list of strings"}), 400
    if _s(payload.get("action")) == "attach_album":
        if len(paths) != 1:
            return jsonify({"ok": False, "error": "Plan one album folder at a time."}), 400
        folder = paths[0]

        def _plan(log, cancel_event=None, update_state=None):
            res = _untracked_recovery.plan_album_attach(
                folder, progress=(lambda info: update_state(**info)) if update_state else None)
            log.append(f"Album attach plan: {'ok, tx ' + _s(res.get('operation_id')) if res.get('ok') else res.get('code')}")
            return res

        job = jobs.start_python(_plan, label="Plan new album row from untracked files",
                                metadata={"type": "untracked-album-plan", "mutating": False})
        return jsonify({"ok": True, "job_id": job.job_id}), 202
    try:
        res = _untracked_recovery.plan_recovery(_s(payload.get("action")), paths)
    except BeetsUnavailableError as exc:
        return jsonify({"ok": False, "error": "Beets engine is unavailable.", "code": exc.error_code or "beets_unavailable"}), 503
    return jsonify(res), (200 if res.get("ok") else 409)


@app.post("/api/dedup/maintenance-run")
def dedup_maintenance_run():
    """Run the scheduled duplicate step on its own (scan, AcoustID
    verification, proposal). It deletes only when unattended deletion is
    authorized; otherwise it records the proposal for review."""
    if _running_job_of_type({"dedup-scan", "dedup-ai-review", "dedup-cleanup", "maintenance-duplicates"}):
        return jsonify({"ok": False, "error": "A duplicate scan or cleanup is already running"}), 409

    def _do(log, cancel_event=None, update_state=None):
        return _maintenance_full_duplicate_scan(log, cancel_event, progress=update_state)

    job = jobs.start_python(_do, label="Duplicate maintenance run", metadata={"type": "maintenance-duplicates"})
    return jsonify({"ok": True, "job_id": job.job_id})
