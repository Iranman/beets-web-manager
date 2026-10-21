"""Acquisition, yt-dlp and format-replacement routes (ARCH-001).
"""

from __future__ import annotations

import copy, os, re, threading
from typing import Any, Dict, List
from flask import jsonify, request
import backend.job_contract as job_contract
import backend.provider_boundary as provider_boundary
from backend.audio_preferences import load_replacement_statuses as _load_music_format_replacement_statuses
from backend.acquisition_service import _ACQ_QUEUE_SOURCES, _DOWNLOAD_METHODS, _acq_download_all_record, _acq_download_payload, _acq_item_mbid, _acq_start_download_job, _acq_trim_batch_log, _download_sources_status, _load_acq_download_all_last, acquisition_queue_mark_done, acquisition_queue_page, download_entered_library, acquisition_queue_snapshot, ensure_acquisition_queue_fresh, refresh_acquisition_queue, start_acquisition_queue_build, _qbit_hardlink_missing_impl, _qbit_status_payload, _save_acq_download_all_last, start_album_download
from backend.app_runtime import QBIT_CATEGORY, QBIT_FILTER, QBIT_URL, YTDLP_ALLOW_BROWSER_COOKIES, _app_logger, _s, _ytdlp_ready, jobs
from backend.job_service import ALBUM_DOWNLOAD_WAIT, _wait_for_child_job
from backend.matching_service import _invalidate_lib_cache
from backend.recover_service import (
    recover_cookie_state,
    recover_enumerate,
    recover_get_run,
    recover_list_runs,
    recover_probe,
    sanitize_diagnostic_report,
    start_recover_fetch,
    start_recover_import,
    start_recover_retry,
)
from backend.replacement_service import _music_format_replace_rows, _music_format_scan_library
from backend.serializers import json_route_result
from backend.slskd_service import _normalise_download_method
from backend.ytdlp_guard import ytdlp_guarded_options
from backend.ytdlp_service import _configured_ytdlp_cookie_auths, _ffmpeg_status, _package_version, _redacted_ytdlp_auth_label, _redacted_ytdlp_candidate_labels, _redacted_ytdlp_rejection, _require_ytdlp_js_runtime, _spotiflac_status, _usable_ytdlp_cookie_auths, _usable_ytdlp_cookie_auths_with_smoke, _yt_dlp_install_status, _ytdlp_apply_source_network_options, _ytdlp_client_profiles_for_source, _ytdlp_cookie_auth_rejection_key, _ytdlp_cookie_candidates, _ytdlp_cookie_rejection_state, _ytdlp_js_runtime_names, _ytdlp_js_runtime_options, _ytdlp_js_runtime_status, _ytdlp_netrc_status, _ytdlp_remote_components, _ytdlp_source_extractor_args, _ytdlp_youtube_status
from app import app  # noqa: E402  (route modules load after app.py defines app)

# ── ARCH-001 extracted code ──


@app.get("/api/ytdlp/status")
def ytdlp_status():
    smoke_force = request.args.get("refresh", "0") == "1" or request.args.get("smoke", "0") == "1"
    auths = _configured_ytdlp_cookie_auths()
    ready = _ytdlp_ready.is_set()
    if ready:
        usable_auths, smoke_checks = _usable_ytdlp_cookie_auths_with_smoke(force=smoke_force)
    else:
        usable_auths = _usable_ytdlp_cookie_auths()
        smoke_checks: List[Dict[str, Any]] = []
    auth = usable_auths[0] if usable_auths else (auths[0] if auths else {"mode": "none", "label": ""})
    auth_key = _ytdlp_cookie_auth_rejection_key(auth)
    file_auth = next((item for item in auths if item.get("mode") == "file"), {})
    browser_auth = next((item for item in auths if item.get("mode") == "browser"), {})
    cookie_file = str(file_auth.get("cookie_file") or "")
    browser_cookie_spec = str(browser_auth.get("browser") or "")
    js_runtime = _ytdlp_js_runtime_status()
    cookie_rejected = _ytdlp_cookie_rejection_state(auth_key) if auth_key else None
    rejected_auths = [
        {
            "label": _redacted_ytdlp_auth_label(auth_item),
            "rejection": _redacted_ytdlp_rejection(_ytdlp_cookie_rejection_state(key)),
        }
        for auth_item in auths
        for key in [_ytdlp_cookie_auth_rejection_key(auth_item)]
        if key and _ytdlp_cookie_rejection_state(key)
    ]
    enabled = bool(ready and js_runtime.get("available"))
    if enabled:
        runtime = js_runtime["runtimes"][0]
        skipped = [str(item.get("label") or item.get("key")) for item in rejected_auths]
        suffix = f"; optional auth source(s) skipped: {', '.join(skipped)}" if skipped else ""
        message = (
            f"yt-dlp ready; YouTube anonymous; "
            f"JS runtime {runtime['name']} {runtime.get('version') or ''}{suffix}"
        ).strip()
    elif ready:
        message = (
            "yt-dlp ready, but YouTube challenge solving needs a supported "
            "JavaScript runtime such as Deno."
        )
    else:
        message = "yt-dlp is still installing; YouTube will run anonymously when ready."
    return jsonify({
        "ok": True,
        "ready": ready,
        "enabled": enabled,
        "cookie_file": "",
        "cookies_from_browser": "",
        "browser_cookies_enabled": YTDLP_ALLOW_BROWSER_COOKIES,
        "cookie_auth_mode": auth.get("mode"),
        "cookie_auth_label": _redacted_ytdlp_auth_label(auth),
        "cookie_auth_candidates": [_redacted_ytdlp_auth_label(item) for item in auths],
        "cookie_auth_rejections": rejected_auths,
        "cookie_auth_smoke": smoke_checks,
        "cookie_candidates": _redacted_ytdlp_candidate_labels(_ytdlp_cookie_candidates()),
        "install": _yt_dlp_install_status(),
        "ffmpeg": _ffmpeg_status(),
        "netrc": _ytdlp_netrc_status(),
        "spotiflac": _spotiflac_status(),
        "youtube": _ytdlp_youtube_status(js_runtime),
        "js_runtime": js_runtime,
        "js_runtimes": _ytdlp_js_runtime_names(),
        "remote_components": _ytdlp_remote_components(),
        "cookie_rejected": bool(cookie_rejected),
        "cookie_rejection": _redacted_ytdlp_rejection(cookie_rejected),
        "message": message,
    })


@app.post("/api/ytdlp/test-youtube")
def ytdlp_test_youtube():
    test_url = _s(
        os.environ.get("YTDLP_YOUTUBE_TEST_URL")
        or "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    ).strip()

    def _do(log, cancel_event=None, update_state=None):
        log.append("Testing YouTube source anonymously")
        if update_state:
            update_state(phase="checking")
        if not _ytdlp_ready.wait(timeout=30):
            raise RuntimeError("yt-dlp unavailable")
        js_runtime = _require_ytdlp_js_runtime()
        if not _package_version("yt-dlp-ejs"):
            raise RuntimeError("EJS unavailable or incompatible")
        if update_state:
            update_state(phase="extracting")
        try:
            import yt_dlp
        except Exception as ex:
            raise RuntimeError(f"yt-dlp unavailable: {ex}") from ex
        ydl_opts: Dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "simulate": True,
            "noplaylist": True,
            "socket_timeout": 30,
            "js_runtimes": _ytdlp_js_runtime_options(),
            "remote_components": _ytdlp_remote_components(),
        }
        _ytdlp_apply_source_network_options(ydl_opts, "ytdlp", log)
        log.append("Inspecting public YouTube URL without cookies")
        last_error = ""
        selected_client = ""
        info: Dict[str, Any] = {}
        for client_label, extractor_args in _ytdlp_client_profiles_for_source("ytdlp"):
            probe_opts = dict(ydl_opts)
            merged_args = _ytdlp_source_extractor_args("ytdlp", extractor_args)
            if merged_args:
                probe_opts["extractor_args"] = merged_args
            log.append(f"Inspecting with YouTube client {client_label}")
            try:
                with yt_dlp.YoutubeDL(ytdlp_guarded_options(probe_opts, [test_url])) as ydl:
                    extracted = ydl.extract_info(test_url, download=False)
                if isinstance(extracted, dict):
                    info = extracted
                selected_client = client_label
                last_error = ""
                break
            except Exception as ex:
                last_error = str(ex)
                log.append(f"Client {client_label} failed: {last_error[:240]}")
                continue
        if last_error:
            raise RuntimeError(last_error)
        title = _s((info or {}).get("title") or "")[:120]
        log.append("YouTube source ready" + (f": {title}" if title else ""))
        if update_state:
            update_state(phase="verified", title=title, client=selected_client)
        return {"ok": True, "title": title, "client": selected_client, "url": test_url, "youtube": _ytdlp_youtube_status(js_runtime)}

    job = jobs.start_python(
        _do,
        label="Test YouTube Source",
        metadata={"type": "ytdlp-youtube-test", "category": "diagnostic"},
    )
    return jsonify({"ok": True, "job_id": job.job_id, "status": "queued"})


@app.post("/api/download/album")
def api_download_album():
    """Start a background job to download an album via slskd (primary)"""
    body, status = start_album_download(request.get_json(silent=True) or {})
    return json_route_result(body, status)


_ACQ_QUEUE_MAX_LIMIT = 500
_ACQ_QUEUE_SEARCH_MAX = 200


def _flag(value: Any) -> bool:
    return _s(value).strip().lower() in {"1", "true", "yes", "on"}


def _int_arg(name: str, default: int, hi: int):
    """``(value, error)`` for a bounded non-negative integer query arg."""
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default, ""
    try:
        value = int(raw)
    except ValueError:
        return default, f"{name} must be an integer"
    if value < 0 or value > hi:
        return default, f"{name} must be between 0 and {hi}"
    return value, ""


@app.get("/api/acquisition/queue")
def acquisition_queue():
    """One page of the cached Find Music list (F7). Never builds it: a missing
    or stale snapshot starts the acquisition-queue-build job and this answers
    at once with what it has."""
    include_unmonitored = _flag(request.args.get("include_unmonitored"))
    offset, err = _int_arg("offset", 0, 10_000_000)
    # An absent limit is one full page, never the whole list (#385 F3).
    limit, err2 = _int_arg("limit", _ACQ_QUEUE_MAX_LIMIT, _ACQ_QUEUE_MAX_LIMIT)
    search = _s(request.args.get("search") or "").strip()
    source = _s(request.args.get("source") or "all").strip().lower()
    sort = _s(request.args.get("sort") or "sort_key").strip().lower()
    album_types = {part.strip().lower() for part in _s(request.args.get("album_type") or "").split(",")
                   if part.strip()}
    error = err or err2
    if not error and len(search) > _ACQ_QUEUE_SEARCH_MAX:
        error = f"search must be at most {_ACQ_QUEUE_SEARCH_MAX} characters"
    if not error and source not in _ACQ_QUEUE_SOURCES:
        error = "source must be one of: " + ", ".join(_ACQ_QUEUE_SOURCES)
    if not error and sort not in ("sort_key", "year_desc"):
        error = "sort must be sort_key or year_desc"
    if not error and len(album_types) > 20:
        error = "album_type lists at most 20 types"
    if error:
        return jsonify({"ok": False, "error": error}), 400

    # Refresh is POST /api/acquisition/queue/refresh only; a GET never forces a build.
    ensure_acquisition_queue_fresh(include_unmonitored=include_unmonitored)
    snap, cache = acquisition_queue_snapshot()
    rows, type_counts = acquisition_queue_page(
        (snap or {}).get("items") or [], search=search, source=source, album_types=album_types,
        include_unmonitored=include_unmonitored, sort=sort)
    page = rows[offset:offset + limit]
    counts = dict((snap or {}).get("counts") or {})
    for key in ("all", "beets", "lidarr", "merged", "unmonitored"):
        counts.setdefault(key, 0)
    return jsonify({
        "ok": True,
        "items": page,
        "total": len(rows),
        "offset": offset,
        "limit": limit,
        "counts": counts,
        "album_types": type_counts,
        "cache": cache,
        "library_error": _s((snap or {}).get("library_error") or ""),
        "wanted_error": _s((snap or {}).get("wanted_error") or ""),
        "library_version": (snap or {}).get("library_version"),
        "download_sources": _download_sources_status(),
    })


@app.post("/api/acquisition/queue/refresh")
def acquisition_queue_refresh():
    """Start (or join) the Find Music list build; returns at once. Only the
    UI's automatic refresh after a finished download sends
    ``after_download: true``, which may reuse the list that download just
    patched (F2); a user's Refresh always builds or joins a build (QA #398 N5)."""
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "body must be a JSON object"}), 400
    job_id, running, reused = refresh_acquisition_queue(
        include_unmonitored=payload.get("include_unmonitored") is True,
        after_download=payload.get("after_download") is True)
    if reused:  # a download just patched the list; no rebuild (F2)
        return jsonify({"ok": True, "job_id": None, "already_running": False, "reused": True}), 200
    return jsonify({"ok": True, "job_id": job_id, "already_running": running, "reused": False}), 202


_acq_download_all_lock = threading.Lock()


@app.post("/api/acquisition/download-all")
def acquisition_download_all():
    payload = request.get_json(silent=True) or {}
    raw_keys = payload.get("keys") or []
    if raw_keys and not isinstance(raw_keys, list):
        return jsonify({"ok": False, "error": "keys must be a list"}), 400

    method = _normalise_download_method(payload.get("method") or "slskd")
    if method not in _DOWNLOAD_METHODS:
        return jsonify({
            "ok": False,
            "error": "method must be slskd, spotiflac, ytdlp, or soundcloud",
        }), 400

    include_unmonitored = bool(payload.get("include_unmonitored", False))
    try_source_fallback = payload.get(
        "try_source_fallback",
        payload.get("try_ytdlp_fallback", True),
    ) is not False
    prioritize = _s(payload.get("prioritize") or "beets_first").strip().lower()
    try:
        limit = int(payload.get("limit") or 0)
    except Exception:
        limit = 0
    limit = max(0, limit)

    if _acq_download_all_lock.locked():
        return jsonify({"ok": False, "error": "An Acquire Download All job is already running"}), 409

    selected_keys = {_s(key).strip() for key in raw_keys if _s(key).strip()}
    # F8: rows come from the cached Find Music snapshot, never a rebuild here.
    snap, cache = acquisition_queue_snapshot()
    if snap is None:
        if not cache["refreshing"]:
            start_acquisition_queue_build(include_unmonitored=include_unmonitored)
        _, cache = acquisition_queue_snapshot()
        return jsonify({"ok": False, "code": "queue_not_ready",
                        "error": "The Find Music list is still being built; try again when it is ready.",
                        "job_id": cache["job_id"]}), 409
    queue_items = snap.get("items") or []

    selected: List[Dict[str, Any]] = []
    skipped = 0
    for item in queue_items:
        if selected_keys and _s(item.get("key")) not in selected_keys:
            continue
        local = item.get("local") or {}
        wanted = item.get("wanted") or {}
        if wanted and not wanted.get("monitored", True) and not include_unmonitored and not local:
            skipped += 1
            continue
        actions = item.get("actions") or {}
        can_download = bool(actions.get("can_download") and _acq_item_mbid(item))
        if not can_download:
            skipped += 1
            continue
        selected.append(copy.deepcopy(item))

    if prioritize in {"beets", "beets_first", "repairs", "repairs_first"}:
        selected.sort(
            key=lambda row: (
                0 if "beets" in (row.get("sources") or []) else 1,
                row.get("sort_key") or "",
            )
        )

    if limit:
        selected = selected[:limit]

    if not selected:
        return jsonify({"ok": False, "error": "No downloadable Acquire rows matched the request"}), 400

    label = f"Acquire Download All: {len(selected)} item(s)"
    job_ref: Dict[str, str] = {}
    job_metadata = {
        "type": "acquisition-download-all",
        "method": method,
        "count": len(selected),
        "skipped": skipped,
        "try_source_fallback": try_source_fallback,
        "try_ytdlp_fallback": try_source_fallback,
        "import_disk_count": 0,
        "download_count": sum(1 for row in selected if (row.get("actions") or {}).get("can_download")),
    }

    def _persist(status: str,
                 result: Dict[str, Any],
                 log: List[str],
                 *,
                 error: str = "") -> None:
        _save_acq_download_all_last(
            _acq_download_all_record(
                label,
                job_metadata,
                result,
                log,
                status=status,
                job_id=job_ref.get("job_id", ""),
                error=error,
            )
        )

    def _do(log, cancel_event=None, update_state=None):
        if not _acq_download_all_lock.acquire(blocking=False):
            raise RuntimeError("another Acquire Download All job is already running")
        contract = None
        batch_source_fallback_enabled = bool(try_source_fallback)
        totals = {
            "total": len(selected),
            "success": 0,
            "failed": 0,
            "skipped": skipped,
            "ytdlp_fallback_disabled": False,
            "failures": [],
        }
        done_keys: List[str] = []
        try:
            contract = job_contract.enter(
                "acquisition-download-all", log=log, cancel_event=cancel_event, update_state=update_state,
                progress=lambda: {k: totals[k] for k in ("total", "success", "failed", "skipped")})
            try:
                log.append(
                    f"Acquire Download All starting: {len(selected)} item(s), "
                    f"method={method}, source_fallback={bool(try_source_fallback)}"
                )
                for idx, item in enumerate(selected, 1):
                    if cancel_event is not None and cancel_event.is_set():
                        raise RuntimeError("cancelled")
                    artist = _s(item.get("artist") or "")
                    album = _s(item.get("album") or "")
                    year = _s(item.get("year") or "")
                    key = _s(item.get("key") or "")
                    log.append(f"[{idx}/{len(selected)}] {artist} - {album} {year}".strip())
                    try:
                        actions = item.get("actions") or {}
                        if actions.get("can_download") and _acq_item_mbid(item):
                            child_payload = _acq_download_payload(
                                item, method, batch_source_fallback_enabled)
                            child_id = _acq_start_download_job(child_payload)
                            prefix = f"download {idx}/{len(selected)}"
                        else:
                            totals["skipped"] += 1
                            log.append("  SKIPPED: row is no longer processable")
                            continue
                        log.append(f"  child job: {child_id}")
                        result = _wait_for_child_job(
                            child_id, log, cancel_event,
                            prefix=prefix, **ALBUM_DOWNLOAD_WAIT,
                        )
                        totals["success"] += 1
                        if download_entered_library(result):
                            done_keys.append(key)
                        if isinstance(result, dict) and result.get("aldir"):
                            log.append(f"  imported from: {result.get('aldir')}")
                        log.append(f"  OK: {artist} - {album}")
                    except Exception as exc:
                        totals["failed"] += 1
                        err = str(exc)
                        totals["failures"].append({
                            "key": key,
                            "artist": artist,
                            "album": album,
                            "error": err,
                        })
                        log.append(f"  FAILED: {err}")
                    _acq_trim_batch_log(log)
                log.append(
                    f"Done: {totals['success']} succeeded, {totals['failed']} failed, "
                    f"{totals['skipped']} skipped."
                )
                _persist("success", totals, log)
                _invalidate_lib_cache()
                acquisition_queue_mark_done(keys=done_keys)
                return totals
            except Exception as exc:
                _persist("failed", totals, log, error=str(exc))
                raise
        finally:
            if contract is not None:
                contract.close()
            _acq_download_all_lock.release()

    job = jobs.start_python(
        _do,
        label=label,
        metadata=job_contract.contract_metadata("acquisition-download-all", job_metadata),
    )
    job_ref["job_id"] = job.job_id
    return jsonify({"ok": True, "job_id": job.job_id, "count": len(selected), "skipped": skipped})


@app.get("/api/acquisition/download-all/active")
def acquisition_download_all_active():
    batch_jobs = [
        job for job in jobs.all()
        if (getattr(job, "metadata", {}) or {}).get("type") == "acquisition-download-all"
    ]
    running = next((job for job in batch_jobs if job.status == "running"), None)
    latest = running or (batch_jobs[0] if batch_jobs else None)
    last_job = _load_acq_download_all_last()
    if not latest:
        return jsonify({"ok": True, "active": False, "job": None, "last_job": last_job})
    return jsonify({
        "ok": True,
        "active": latest.status == "running",
        "job": latest.to_dict(include_log=True),
        "last_job": last_job,
    })


@app.get("/api/qbittorrent/status")
def qbit_status():
    return jsonify(_qbit_status_payload())


@app.post("/api/qbittorrent/hardlink-missing")
def qbit_hardlink_missing():
    payload = request.get_json(silent=True) or {}
    dry_run = payload.get("dry_run", True) is not False
    category = _s(payload.get("category") or QBIT_CATEGORY).strip()
    qbit_filter = _s(payload.get("filter") or payload.get("qbit_filter") or QBIT_FILTER).strip()
    search = _s(payload.get("search") or payload.get("q") or "").strip()
    raw_hashes = payload.get("hashes") or []
    if isinstance(raw_hashes, str):
        hashes = [h.strip() for h in re.split(r"[\s,|]+", raw_hashes) if h.strip()]
    elif isinstance(raw_hashes, list):
        hashes = [_s(h).strip() for h in raw_hashes if _s(h).strip()]
    else:
        hashes = []
    limit = max(0, min(int(payload.get("limit") or 0), 10000))
    recheck = payload.get("recheck", True) is not False
    if not QBIT_URL:
        return jsonify({
            "ok": False,
            "error": (
                "qBittorrent is not configured. Set QBITTORRENT_URL or QBIT_URL "
                "for the Web Manager container."
            ),
            "status": _qbit_status_payload(),
        }), 400

    if dry_run:
        log: List[str] = []
        try:
            result = _qbit_hardlink_missing_impl(
                dry_run=True,
                category=category,
                qbit_filter=qbit_filter,
                search=search,
                hashes=hashes,
                limit=limit,
                recheck=False,
                log=log,
                cancel_event=None,
            )
        except Exception as ex:
            _app_logger.warning("qBittorrent hardlink-missing dry-run failed: %s", type(ex).__name__)
            block = provider_boundary.outbound_block(ex, "qbittorrent")
            if block:
                return jsonify({"ok": False, **block, "log": log}), 502
            return jsonify({"ok": False, "error": "Could not run hardlink-missing scan.", "log": log}), 500
        return jsonify({
            "ok": True,
            "dry_run": True,
            "result": result,
            "log": log,
        })

    def _do(log, cancel_event=None):
        return _qbit_hardlink_missing_impl(
            dry_run=False,
            category=category,
            qbit_filter=qbit_filter,
            search=search,
            hashes=hashes,
            limit=limit,
            recheck=recheck,
            log=log,
            cancel_event=cancel_event,
        )

    label = "qBittorrent hardlink dry run" if dry_run else "qBittorrent hardlink repair"
    job = jobs.start_python(
        _do,
        label=label,
        metadata={
            "type": "qbit-hardlink-repair",
            "dry_run": dry_run,
            "category": category,
            "filter": qbit_filter,
            "search": search,
            "hash_count": len(hashes),
        },
    )
    return jsonify({"ok": True, "job_id": job.job_id, "dry_run": dry_run})


@app.get("/api/music-format/replacements")
def music_format_replacement_statuses():
    data = _load_music_format_replacement_statuses()
    data.setdefault("ok", True)
    data.setdefault("tracks", [])
    return jsonify(data)


@app.post("/api/library/music-format/scan")
def start_music_format_library_scan():
    payload = request.get_json(silent=True) or {}
    try:
        limit = max(0, int(payload.get("limit") or 0))
    except Exception:
        limit = 0
    setattr(_music_format_scan_library, "limit", limit)
    job = jobs.start_python(
        _music_format_scan_library,
        label="Music format preference scan",
        metadata={"type": "music-format-scan", "category": "cleanup"},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/library/music-format/replace")
def start_music_format_replacement_retry():
    payload = request.get_json(silent=True) or {}
    try:
        limit = max(0, int(payload.get("limit") or 0))
    except Exception:
        limit = 0
    method = _normalise_download_method(payload.get("method") or "slskd")

    reset_retry_state = bool(payload.get("reset_retry_state") or payload.get("manual_retry"))

    def _do(log, cancel_event=None, update_state=None):
        return _music_format_replace_rows(
            log, cancel_event, update_state,
            limit=limit, method=method, reset_retry_state=reset_retry_state)

    job = jobs.start_python(
        job_contract.guarded(_do, workflow="music-format-replace", fail_fast_in_process=True),
        label="Music format replacement retry",
        metadata={"type": "music-format-replace", "category": "cleanup",
                  **job_contract.contract_metadata("music-format-replace")},
    )
    return jsonify({"ok": True, "job_id": job.job_id})


# ── Recover My Uploads (increments 1 & 2): authorized FETCH, STAGE, VERIFY, IMPORT & DIAGNOSTICS ──
# Added here (not a new routes_*.py) because app.py's ROUTE_MODULES is the only
# registration path and app.py is out of scope for this change.
@app.get("/api/recover/cookie-state")
def api_recover_cookie_state():
    """Recoverable yt-dlp cookie state (none/invalid/rejected/ok) for owner-only
    recovery, by variable name and signature only — never cookie contents."""
    return json_route_result(recover_cookie_state(), 200)


@app.post("/api/recover/probe")
def api_recover_probe():
    """Probe metadata + available formats + stream/DRM/auth diagnostics for one
    allowlisted URL. No download."""
    data = request.get_json(silent=True) or {}
    url = _s(data.get("url")).strip()
    if not url:
        return json_route_result({"ok": False, "error": "url is required"}, 200)
    diag = data.get("diagnostic_report") if isinstance(data.get("diagnostic_report"), dict) else None
    return json_route_result(recover_probe(url, diagnostic_report=diag), 200)


@app.post("/api/recover/enumerate")
def api_recover_enumerate():
    """List a channel/user/playlist/set owner's items (extract_flat). No download."""
    data = request.get_json(silent=True) or {}
    url = _s(data.get("url")).strip()
    if not url:
        return json_route_result({"ok": False, "error": "url is required"}, 200)
    try:
        limit = int(data.get("limit") or 500)
    except (TypeError, ValueError):
        limit = 500
    return json_route_result(recover_enumerate(url, limit=limit), 200)


@app.post("/api/recover/fetch")
def api_recover_fetch():
    """Start a background job to fetch + stage authorized uploads under
    DOWNLOADS_ROOT (with optional auto_import). Requires an ``authorization``
    object ``{basis, note}`` with a valid basis (original_creation / own_account
    / licensed_permission); own_account is independently verified against
    RECOVER_OWNED_ACCOUNTS, the others are asserted-only."""
    body, status = start_recover_fetch(request.get_json(silent=True) or {})
    return json_route_result(body, status)


@app.get("/api/recover/runs")
def api_recover_runs():
    """List persisted recovery runs (for status inspection and retries)."""
    run_id = _s(request.args.get("run_id") or "").strip()
    if run_id:
        run_rec = recover_get_run(run_id)
        if run_rec is None:
            return json_route_result({"ok": False, "error": f"recovery run '{run_id}' not found"}, 200)
        return json_route_result({"ok": True, "run": run_rec}, 200)
    try:
        limit = int(request.args.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    return json_route_result(recover_list_runs(limit=limit), 200)


@app.post("/api/recover/retry")
def api_recover_retry():
    """Start a recovery job retrying the failed/unrecovered URLs from a
    persisted recovery run."""
    body, status = start_recover_retry(request.get_json(silent=True) or {})
    return json_route_result(body, status)


@app.post("/api/recover/import")
def api_recover_import():
    """Verify staged recovery files, run AcoustID/MusicBrainz matching, safely
    import into Beets without overwriting existing library items, and refresh
    playlist coverage. Requires an ``authorization`` object ``{basis, note}``;
    an asserted-only (unverified) basis is forced to review_required and never
    auto-imported."""
    body, status = start_recover_import(request.get_json(silent=True) or {})
    return json_route_result(body, status)


@app.post("/api/recover/diagnostics")
def api_recover_diagnostics():
    """Consume an external or laboratory stream/EME observation report and
    return a sanitized Beets diagnostic summary with all secret keys, cookies,
    and license payloads stripped."""
    payload = request.get_json(silent=True)
    report_in = payload.get("report") if isinstance(payload, dict) and isinstance(payload.get("report"), dict) else payload
    return json_route_result(sanitize_diagnostic_report(report_in), 200)
