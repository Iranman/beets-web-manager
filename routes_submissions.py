"""Music metadata submission routes.

Registered after app.py initializes. Keeps submission-only Beets commands out of
the main app module while reusing the existing JobStore and Beets config helpers.
"""
import importlib.util
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

from flask import jsonify, request

from app import (  # noqa: E402
    BEET_BIN,
    _ANSI_RE,
    _MB_UUID_RE,
    _beet_env,
    _beet_run,
    _extract_mb_uuid,
    _fetch_mb_release_tracklist,
    _invalidate_lib_cache,
    _read_beets_plugin_list,
    _s,
    _write_job_beets_config,
    app,
    jobs,
    lib,
)


def _acoustid_key() -> str:
    return (
        os.environ.get("ACOUSTID_API_KEY", "").strip()
        or os.environ.get("ACOUSTID_KEY", "").strip()
    )


def _acoustid_submit_config_extra() -> str:
    key = _acoustid_key()
    if not key:
        return "chroma:\n  auto: no\n"
    safe_key = key.replace("\\", "\\\\").replace('"', '\\"')
    return (
        "chroma:\n"
        "  auto: no\n"
        f'  apikey: "{safe_key}"\n'
        "acoustid:\n"
        f'  apikey: "{safe_key}"\n'
    )


def _append_clean_output(log, stdout: str = "", stderr: str = "") -> str:
    output = _ANSI_RE.sub("", ((stdout or "") + (stderr or "")).strip())
    for line in output.splitlines():
        if line.strip():
            log.append(line)
    return output


def _start_acoustid_submit_job(query: str, label: str):
    def _do(log, cancel_event=None):
        cfg = _write_job_beets_config(
            f"/tmp/beets_acoustid_submit_{uuid.uuid4().hex}.yaml",
            _acoustid_submit_config_extra(),
        )
        if not _acoustid_key():
            log.append("ACOUSTID_API_KEY/ACOUSTID_KEY is not set in the environment; using Beets config if present.")
        log.append(f"Running beet submit {query}")
        result = _beet_run(
            [BEET_BIN, "-c", cfg, "submit", query],
            log,
            timeout=300,
            env=_beet_env(),
            cancel=cancel_event,
        )
        output = _append_clean_output(log, result.stdout, result.stderr)
        if result.returncode != 0:
            raise RuntimeError(f"beet submit failed with exit code {result.returncode}")
        return {"output": output, "query": query}

    job = jobs.start_python(_do, label=label)
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/albums/<int:aid>/acoustid-submit")
def album_acoustid_submit(aid: int):
    album = lib.get_album(aid)
    if not album:
        return jsonify({"ok": False, "error": "Album not found"}), 404
    readiness = _submission_readiness()
    plugins = readiness.get("plugins") or {}
    if not plugins.get("chroma"):
        return jsonify({"ok": False, "error": "The Beets chroma plugin is not enabled."}), 400
    if not readiness.get("fpcalc_available"):
        return jsonify({"ok": False, "error": "fpcalc was not found in the application container."}), 400
    if not readiness.get("pyacoustid_available"):
        return jsonify({"ok": False, "error": "The Python acoustid module is not available."}), 400
    if not readiness.get("acoustid_key_configured"):
        return jsonify({"ok": False, "error": "The AcoustID API key is not configured."}), 400
    missing = [int(getattr(item, "id", 0) or 0) for item in album.items() if not _MB_UUID_RE.match(_s(getattr(item, "mb_trackid", "") or "").strip())]
    if missing:
        return jsonify({"ok": False, "error": f"{len(missing)} track(s) are missing MusicBrainz recording MBIDs before AcoustID submission.", "missing_item_ids": missing}), 400
    title = " - ".join(part for part in (_s(album.albumartist), _s(album.album)) if part)
    label = f"AcoustID submit: {title or f'album {aid}'}"
    return _start_acoustid_submit_job(f"album_id:{aid}", label)


@app.post("/api/items/<int:iid>/acoustid-submit")
def item_acoustid_submit(iid: int):
    item = lib.get_item(iid)
    if not item:
        return jsonify({"ok": False, "error": "Item not found"}), 404
    readiness = _submission_readiness()
    plugins = readiness.get("plugins") or {}
    if not plugins.get("chroma"):
        return jsonify({"ok": False, "error": "The Beets chroma plugin is not enabled."}), 400
    if not readiness.get("fpcalc_available"):
        return jsonify({"ok": False, "error": "fpcalc was not found in the application container."}), 400
    if not readiness.get("pyacoustid_available"):
        return jsonify({"ok": False, "error": "The Python acoustid module is not available."}), 400
    if not readiness.get("acoustid_key_configured"):
        return jsonify({"ok": False, "error": "The AcoustID API key is not configured."}), 400
    if getattr(item, "album_id", None):
        album = lib.get_album(int(getattr(item, "album_id", 0) or 0))
        missing = [int(getattr(row, "id", 0) or 0) for row in (album.items() if album else []) if not _MB_UUID_RE.match(_s(getattr(row, "mb_trackid", "") or "").strip())]
        if missing:
            return jsonify({"ok": False, "error": f"{len(missing)} track(s) in this album are missing MusicBrainz recording MBIDs before AcoustID submission.", "missing_item_ids": missing}), 400
    elif not _MB_UUID_RE.match(_s(getattr(item, "mb_trackid", "") or "").strip()):
        return jsonify({"ok": False, "error": f"Item {iid} has no MusicBrainz recording MBID, so its fingerprint cannot be submitted yet."}), 400
    query = f"album_id:{item.album_id}" if getattr(item, "album_id", None) else f"id:{iid}"
    title = " - ".join(part for part in (_s(item.artist), _s(item.title)) if part)
    label = f"AcoustID submit: {title or f'item {iid}'}"
    return _start_acoustid_submit_job(query, label)


# -- Submission workspace helpers -------------------------------------------------

_SUBMISSION_DRAFTS_FILE = Path(os.environ.get("BEETS_SUBMISSION_DRAFTS", "/config/submission_drafts.json"))


def _submission_json_load(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def _submission_json_save(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _submission_key(target_type: str, target_id: int) -> str:
    return f"{target_type}:{int(target_id)}"


def _submission_drafts() -> Dict[str, Any]:
    payload = _submission_json_load(_SUBMISSION_DRAFTS_FILE, {})
    return payload if isinstance(payload, dict) else {}


def _submission_draft(target_type: str, target_id: int) -> Dict[str, Any]:
    draft = _submission_drafts().get(_submission_key(target_type, target_id), {})
    return draft if isinstance(draft, dict) else {}


def _save_submission_draft(target_type: str, target_id: int, draft: Dict[str, Any]) -> Dict[str, Any]:
    drafts = _submission_drafts()
    clean = draft if isinstance(draft, dict) else {}
    clean["target_type"] = target_type
    clean["target_id"] = int(target_id)
    clean["updated_at"] = time.time()
    drafts[_submission_key(target_type, target_id)] = clean
    _submission_json_save(_SUBMISSION_DRAFTS_FILE, drafts)
    return clean


def _delete_submission_draft(target_type: str, target_id: int) -> bool:
    drafts = _submission_drafts()
    key = _submission_key(target_type, target_id)
    existed = key in drafts
    if existed:
        drafts.pop(key, None)
        _submission_json_save(_SUBMISSION_DRAFTS_FILE, drafts)
    return existed


def _config_has_acoustid_key(config_path: str = "/config/config.yaml") -> bool:
    try:
        lines = Path(config_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return False
    in_block = False
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(("acoustid:", "chroma:")):
            in_block = True
            continue
        if in_block and raw[:1] and not raw[:1].isspace():
            in_block = False
        if in_block and stripped.lower().startswith(("apikey:", "api_key:")):
            return bool(stripped.split(":", 1)[1].strip().strip("'\""))
    return False


def _submission_readiness() -> Dict[str, Any]:
    configured = set(_read_beets_plugin_list())
    fpcalc_path = shutil.which("fpcalc") or ""
    return {
        "plugins": {
            "mbsubmit": "mbsubmit" in configured,
            "musicbrainz": "musicbrainz" in configured,
            "chroma": "chroma" in configured,
            "mbsync": "mbsync" in configured,
        },
        "fpcalc_available": bool(fpcalc_path),
        "fpcalc_path": fpcalc_path,
        "pyacoustid_available": importlib.util.find_spec("acoustid") is not None,
        "acoustid_key_configured": bool(_acoustid_key() or _config_has_acoustid_key()),
        "beet_available": bool(BEET_BIN and Path(BEET_BIN).exists()),
    }


def _item_abs_path(item) -> str:
    raw = _s(getattr(item, "path", "") or "")
    if raw and not Path(raw).is_absolute():
        return str(Path("/data/media/music") / raw)
    return raw


def _duration_label(seconds: Any) -> str:
    try:
        value = int(float(seconds or 0))
    except Exception:
        value = 0
    return f"{value // 60}:{value % 60:02d}" if value > 0 else ""


def _track_payload(item, index: int) -> Dict[str, Any]:
    path = _item_abs_path(item)
    exists = bool(path and Path(path).exists())
    mb_trackid = _s(getattr(item, "mb_trackid", "") or "").strip().lower()
    title = _s(getattr(item, "title", "") or "").strip()
    track = int(getattr(item, "track", 0) or 0)
    disc = int(getattr(item, "disc", 0) or 0)
    if not exists:
        validation = "File unavailable"
    elif not title:
        validation = "Missing track title"
    elif track <= 0:
        validation = "Invalid track number"
    elif disc <= 0:
        validation = "Invalid disc number"
    else:
        validation = "Ready"
    return {
        "index": index,
        "item_id": int(getattr(item, "id", 0) or 0),
        "album_id": int(getattr(item, "album_id", 0) or 0),
        "disc": disc or 1,
        "track": track or index,
        "title": title,
        "artist": _s(getattr(item, "artist", "") or "").strip(),
        "album": _s(getattr(item, "album", "") or "").strip(),
        "albumartist": _s(getattr(item, "albumartist", "") or "").strip(),
        "duration": float(getattr(item, "length", 0) or 0),
        "duration_display": _duration_label(getattr(item, "length", 0) or 0),
        "file_name": Path(path).name if path else "",
        "file_path": path,
        "file_available": exists,
        "format": _s(getattr(item, "format", "") or "").strip(),
        "mb_trackid": mb_trackid,
        "mb_albumid": _s(getattr(item, "mb_albumid", "") or "").strip().lower(),
        "fingerprint_status": "File unavailable" if not exists else ("Ready for AcoustID" if mb_trackid else "Missing recording MBID"),
        "validation_status": validation,
    }

def _album_track_rows(album) -> List[Dict[str, Any]]:
    items = sorted(list(album.items()), key=lambda i: (int(getattr(i, "disc", 0) or 0), int(getattr(i, "track", 0) or 0), int(getattr(i, "id", 0) or 0)))
    return [_track_payload(item, idx + 1) for idx, item in enumerate(items)]


def _summary_for_album(album, tracks: List[Dict[str, Any]]) -> Dict[str, Any]:
    first = tracks[0] if tracks else {}
    runtime = sum(float(t.get("duration") or 0) for t in tracks)
    discs = sorted({int(t.get("disc") or 1) for t in tracks}) or [1]
    album_id = int(getattr(album, "id", 0) or 0)
    return {
        "target_type": "album",
        "album_id": album_id,
        "item_id": int(first.get("item_id") or 0),
        "title": _s(getattr(album, "album", "") or "").strip(),
        "albumartist": _s(getattr(album, "albumartist", "") or "").strip(),
        "release_type": _s(getattr(album, "albumtype", "") or "").strip() or "Album",
        "secondary_type": "",
        "release_status": _s(getattr(album, "status", "") or "").strip(),
        "release_date": _s(getattr(album, "original_date", "") or getattr(album, "date", "") or getattr(album, "year", "") or "").strip(),
        "country": _s(getattr(album, "country", "") or "").strip(),
        "label": _s(getattr(album, "label", "") or "").strip(),
        "catalog_number": _s(getattr(album, "catalognum", "") or "").strip(),
        "barcode": _s(getattr(album, "barcode", "") or "").strip(),
        "format": _s(first.get("format") or getattr(album, "format", "") or "").strip(),
        "disc_count": len(discs),
        "track_count": len(tracks),
        "runtime": runtime,
        "runtime_display": _duration_label(runtime),
        "source_path": _s(album.item_dir()) if hasattr(album, "item_dir") else _s(first.get("file_path") or ""),
        "mb_albumartistid": _s(getattr(album, "mb_albumartistid", "") or "").strip().lower(),
        "mb_albumartistids": _s(getattr(album, "mb_albumartistids", "") or "").strip(),
        "mb_releasegroupid": _s(getattr(album, "mb_releasegroupid", "") or "").strip().lower(),
        "mb_albumid": _s(getattr(album, "mb_albumid", "") or "").strip().lower(),
        "cover_art_url": f"/api/albums/{album_id}/art" if album_id else "",
    }


def _summary_for_item(item, tracks: List[Dict[str, Any]]) -> Dict[str, Any]:
    row = tracks[0] if tracks else _track_payload(item, 1)
    return {
        "target_type": "item",
        "album_id": int(getattr(item, "album_id", 0) or 0),
        "item_id": int(getattr(item, "id", 0) or 0),
        "title": row.get("album") or row.get("title") or "",
        "albumartist": row.get("albumartist") or row.get("artist") or "",
        "release_type": "Singleton",
        "secondary_type": "",
        "release_status": "",
        "release_date": _s(getattr(item, "year", "") or "").strip(),
        "country": _s(getattr(item, "country", "") or "").strip(),
        "label": _s(getattr(item, "label", "") or "").strip(),
        "catalog_number": _s(getattr(item, "catalognum", "") or "").strip(),
        "barcode": _s(getattr(item, "barcode", "") or "").strip(),
        "format": row.get("format") or "",
        "disc_count": 1,
        "track_count": len(tracks),
        "runtime": float(row.get("duration") or 0),
        "runtime_display": row.get("duration_display") or "",
        "source_path": row.get("file_path") or "",
        "mb_albumartistid": _s(getattr(item, "mb_albumartistid", "") or "").strip().lower(),
        "mb_albumartistids": _s(getattr(item, "mb_albumartistids", "") or "").strip(),
        "mb_releasegroupid": _s(getattr(item, "mb_releasegroupid", "") or "").strip().lower(),
        "mb_albumid": _s(getattr(item, "mb_albumid", "") or "").strip().lower(),
        "cover_art_url": "",
    }


def _check(label: str, ok: bool, stage: str, explanation: str, action: str = "", affected: List[str] = None, blocking: bool = True) -> Dict[str, Any]:
    return {"label": label, "status": "pass" if ok else ("fail" if blocking else "warning"), "stage": stage, "explanation": "" if ok else explanation, "action": action, "affected": affected or [], "blocking": blocking}


def _submission_preflight(summary: Dict[str, Any], tracks: List[Dict[str, Any]], readiness: Dict[str, Any]) -> Dict[str, Any]:
    missing_files = [t.get("file_name") or f"item {t.get('item_id')}" for t in tracks if not t.get("file_available")]
    missing_titles = [f"track {t.get('track') or t.get('index')}" for t in tracks if not _s(t.get("title")).strip()]
    bad_tracks = [f"item {t.get('item_id')}" for t in tracks if int(t.get("track") or 0) <= 0]
    bad_discs = [f"item {t.get('item_id')}" for t in tracks if int(t.get("disc") or 0) <= 0]
    no_duration = [f"track {t.get('track') or t.get('index')}" for t in tracks if float(t.get("duration") or 0) <= 0]
    missing_recordings = [f"track {t.get('track') or t.get('index')}" for t in tracks if not _MB_UUID_RE.match(_s(t.get("mb_trackid")).strip())]
    plugins = readiness.get("plugins") or {}
    checks = [
        _check("Album is imported into the Beets library", bool(summary.get("album_id") or summary.get("item_id")), "MusicBrainz", "The selected folder has not been imported into the Beets library.", "Import it before preparing a MusicBrainz submission."),
        _check("All files are accessible", not missing_files, "MusicBrainz", "Some files are missing or unreadable.", "Restore the missing files or remove them from the album before submitting.", missing_files),
        _check("Artist metadata is present", bool(_s(summary.get("albumartist")).strip()), "MusicBrainz", "Album artist is empty.", "Enter a release artist credit before preparing the submission."),
        _check("Album title is present", bool(_s(summary.get("title")).strip()), "MusicBrainz", "Release title is empty.", "Enter a release title before preparing the submission."),
        _check("Track titles are present", not missing_titles, "MusicBrainz", "One or more tracks have no title.", "Fill in the missing track titles.", missing_titles),
        _check("Track positions are valid", not bad_tracks, "MusicBrainz", "One or more tracks have an invalid track number.", "Fix track numbers before preparing the submission.", bad_tracks),
        _check("Disc positions are valid", not bad_discs, "MusicBrainz", "One or more tracks have an invalid disc number.", "Fix disc numbers before preparing the submission.", bad_discs),
        _check("Track durations are available", not no_duration, "MusicBrainz", "Some tracks have no duration. MusicBrainz can still be prepared, but duplicate checks are weaker.", "Refresh Beets metadata for the affected files.", no_duration, False),
        _check("Release date is valid", bool(_s(summary.get("release_date")).strip()), "MusicBrainz", "Release date is missing. Complete it during the MusicBrainz handoff.", "Add a year or full release date.", blocking=False),
        _check("Release format is selected", bool(_s(summary.get("format")).strip()), "MusicBrainz", "Media format is unknown.", "Choose the media format during the MusicBrainz handoff.", blocking=False),
        _check("Existing MusicBrainz duplicates have been checked", False, "MusicBrainz", "Review likely existing releases before creating a new release.", "Use the duplicate candidates section.", blocking=False),
        _check("Artist MusicBrainz entity has been selected or will be created", bool(_s(summary.get("mb_albumartistid")).strip()), "MusicBrainz", "Album artist MBID is not attached yet.", "Select or create the artist on MusicBrainz.", blocking=False),
        _check("Beets mbsubmit plugin is enabled", bool(plugins.get("mbsubmit")), "MusicBrainz", "The Beets mbsubmit plugin is not enabled in config.yaml.", "Enable mbsubmit before generating track-parser text."),
        _check("Tracks have recording IDs before AcoustID submission", not missing_recordings, "AcoustID", "Some tracks do not have MusicBrainz recording MBIDs.", "Attach recording MBIDs before submitting fingerprints.", missing_recordings),
        _check("AcoustID API key is configured", bool(readiness.get("acoustid_key_configured")), "AcoustID", "The AcoustID API key is not configured.", "Add it in Settings -> Integrations."),
        _check("Chromaprint/fpcalc is available", bool(readiness.get("fpcalc_available")), "AcoustID", "fpcalc was not found in the application container.", "Install chromaprint/fpcalc in the runtime."),
        _check("pyacoustid is available", bool(readiness.get("pyacoustid_available")), "AcoustID", "The Python acoustid module is not available.", "Install pyacoustid in the runtime."),
        _check("Beets chroma plugin is enabled", bool(plugins.get("chroma")), "AcoustID", "The Beets chroma plugin is not enabled.", "Enable chroma before submitting fingerprints."),
    ]
    mb_blocked = any(c["blocking"] and c["status"] == "fail" and c["stage"] == "MusicBrainz" for c in checks)
    acoustid_blocked = mb_blocked or any(c["blocking"] and c["status"] == "fail" and c["stage"] == "AcoustID" for c in checks)
    return {"checks": checks, "missing_count": sum(1 for c in checks if c["status"] == "fail"), "warning_count": sum(1 for c in checks if c["status"] == "warning"), "musicbrainz_ready": not mb_blocked, "acoustid_ready": not acoustid_blocked}


def _resolve_submission_target(album_id: int = 0, item_id: int = 0, singleton: bool = False) -> Tuple[str, int, Dict[str, Any], List[Dict[str, Any]]]:
    if album_id > 0:
        album = lib.get_album(album_id)
        if not album:
            raise KeyError(f"Album {album_id} was not found in the Beets library.")
        tracks = _album_track_rows(album)
        return "album", album_id, _summary_for_album(album, tracks), tracks
    if item_id > 0:
        item = lib.get_item(item_id)
        if not item:
            raise KeyError(f"Item {item_id} was not found in the Beets library.")
        item_album_id = int(getattr(item, "album_id", 0) or 0)
        if item_album_id > 0 and not singleton:
            return _resolve_submission_target(album_id=item_album_id)
        tracks = [_track_payload(item, 1)]
        return "item", item_id, _summary_for_item(item, tracks), tracks
    raise ValueError("Select an imported album or item before opening the submission workspace.")


@app.get("/api/submissions/target")
def submission_target():
    try:
        album_id = int(request.args.get("album_id") or 0)
        item_id = int(request.args.get("item_id") or 0)
        singleton = str(request.args.get("singleton") or "").strip().lower() in {"1", "true", "yes"}
        target_type, target_id, summary, tracks = _resolve_submission_target(album_id=album_id, item_id=item_id, singleton=singleton)
    except KeyError as ex:
        return jsonify({"ok": False, "error": str(ex)}), 404
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 400
    readiness = _submission_readiness()
    preflight = _submission_preflight(summary, tracks, readiness)
    draft = _submission_draft(target_type, target_id)
    summary["workflow_stage"] = _s(draft.get("stage") or ("Ready for AcoustID" if summary.get("mb_albumid") and summary.get("mb_releasegroupid") and preflight.get("acoustid_ready") else "Waiting for published MBIDs" if draft.get("mbsubmit_output") else "Ready for MusicBrainz" if preflight.get("musicbrainz_ready") else "Needs metadata"))
    return jsonify({"ok": True, "target_type": target_type, "target_id": target_id, "summary": summary, "tracks": tracks, "preflight": preflight, "readiness": readiness, "draft": draft})


@app.post("/api/submissions/draft")
def save_submission_draft():
    payload = request.get_json(silent=True) or {}
    target_type = _s(payload.get("target_type") or "").strip().lower()
    target_id = int(payload.get("target_id") or 0)
    draft = payload.get("draft") or {}
    if target_type not in {"album", "item"} or target_id <= 0:
        return jsonify({"ok": False, "error": "Valid target_type and target_id are required."}), 400
    if len(json.dumps(draft)) > 1_500_000:
        return jsonify({"ok": False, "error": "Submission draft is too large."}), 413
    return jsonify({"ok": True, "draft": _save_submission_draft(target_type, target_id, draft)})


@app.delete("/api/submissions/draft")
def reset_submission_draft():
    target_type = _s(request.args.get("target_type") or "").strip().lower()
    target_id = int(request.args.get("target_id") or 0)
    if target_type not in {"album", "item"} or target_id <= 0:
        return jsonify({"ok": False, "error": "Valid target_type and target_id are required."}), 400
    return jsonify({"ok": True, "removed": _delete_submission_draft(target_type, target_id)})

def _extract_release_input(value: str) -> Tuple[str, str]:
    text = _s(value).strip()
    if not text:
        raise ValueError("Paste a MusicBrainz release URL or release MBID.")
    parsed = urlparse(text)
    if parsed.scheme or parsed.netloc:
        host = parsed.netloc.lower()
        if host not in {"musicbrainz.org", "www.musicbrainz.org"}:
            raise ValueError("MusicBrainz URLs must use musicbrainz.org.")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2 or parts[0] not in {"release", "release-group"}:
            raise ValueError("Paste a MusicBrainz release or release-group URL.")
        mbid = parts[1].lower()
        if not _MB_UUID_RE.match(mbid):
            raise ValueError("The MusicBrainz URL does not contain a valid MBID.")
        return parts[0], mbid
    mbid = _extract_mb_uuid(text)
    if not mbid or not _MB_UUID_RE.match(mbid):
        raise ValueError("Paste a valid MusicBrainz MBID.")
    return "release", mbid


def _compare_release_tracks(local_tracks: List[Dict[str, Any]], mb_tracks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_position = {(int(t.get("disc") or 1), int(t.get("track") or 0)): t for t in mb_tracks}
    mapping = []
    for local in local_tracks:
        key = (int(local.get("disc") or 1), int(local.get("track") or 0))
        mb_track = by_position.get(key) or {}
        local_title = _s(local.get("title")).strip()
        mb_title = _s(mb_track.get("title")).strip()
        local_ms = int(float(local.get("duration") or 0) * 1000)
        mb_ms = int(mb_track.get("duration_ms") or 0)
        delta = abs(local_ms - mb_ms) if local_ms and mb_ms else 0
        issues = []
        if not mb_track:
            issues.append("No MusicBrainz track at this disc/track position.")
        elif local_title and mb_title and local_title.casefold() != mb_title.casefold():
            issues.append("Track title differs.")
        if delta > 8000:
            issues.append("Track duration differs by more than 8 seconds.")
        mapping.append({"item_id": local.get("item_id"), "disc": key[0], "track": key[1], "local_title": local_title, "musicbrainz_title": mb_title, "recording_mbid": _s(mb_track.get("mb_trackid")).strip().lower(), "duration_delta_ms": delta, "status": "mismatch" if issues else "match", "issues": issues})
    return mapping


@app.post("/api/submissions/musicbrainz-release/validate")
def validate_musicbrainz_release():
    payload = request.get_json(silent=True) or {}
    try:
        entity_type, mbid = _extract_release_input(_s(payload.get("input") or payload.get("mbid") or ""))
        if entity_type != "release":
            return jsonify({"ok": True, "entity_type": entity_type, "release_group_mbid": mbid, "requires_release_mbid": True, "message": "A release-group MBID was provided. Select the exact MusicBrainz release before attaching recording IDs."})
        album_id = int(payload.get("album_id") or 0)
        item_id = int(payload.get("item_id") or 0)
        if album_id or item_id:
            _target_type, _target_id, summary, local_tracks = _resolve_submission_target(album_id=album_id, item_id=item_id)
        else:
            summary, local_tracks = {}, []
        mb = _fetch_mb_release_tracklist(mbid, [])
        if not mb.get("ok"):
            return jsonify({"ok": False, "error": mb.get("error") or "MusicBrainz release lookup failed."}), 400
        mapping = _compare_release_tracks(local_tracks, mb.get("tracks") or []) if local_tracks else []
        mismatches = [row for row in mapping if row.get("status") != "match"]
        if local_tracks and len(local_tracks) != len(mb.get("tracks") or []):
            mismatches.append({"status": "mismatch", "issues": [f"MusicBrainz release contains {len(mb.get('tracks') or [])} tracks, but the local target contains {len(local_tracks)}."]})
        return jsonify({"ok": True, "entity_type": "release", "release": {"mb_albumid": mbid, "title": mb.get("release_title", ""), "albumartist": mb.get("release_artist", ""), "mb_albumartistid": mb.get("release_artist_id", ""), "mb_albumartistids": mb.get("release_artistids", ""), "mb_releasegroupid": mb.get("release_group", ""), "date": mb.get("date", ""), "country": mb.get("country", ""), "track_count": len(mb.get("tracks") or [])}, "local": summary, "mapping": mapping, "mismatches": mismatches, "needs_confirmation": bool(mismatches)})
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 400


@app.post("/api/submissions/albums/<int:aid>/attach-mbids")
def attach_album_mbids(aid: int):
    album = lib.get_album(aid)
    if not album:
        return jsonify({"ok": False, "error": f"Album {aid} was not found in the Beets library."}), 404
    payload = request.get_json(silent=True) or {}
    mb_albumartistid = _s(payload.get("mb_albumartistid") or "").strip().lower()
    mb_releasegroupid = _s(payload.get("mb_releasegroupid") or "").strip().lower()
    mb_albumid = _s(payload.get("mb_albumid") or "").strip().lower()
    recordings = payload.get("recordings") or []
    if not _MB_UUID_RE.match(mb_albumartistid):
        return jsonify({"ok": False, "error": "Album artist MBID must be a valid MusicBrainz UUID."}), 400
    if not _MB_UUID_RE.match(mb_releasegroupid):
        return jsonify({"ok": False, "error": "Release-group MBID must be a valid MusicBrainz UUID."}), 400
    if mb_albumid and not _MB_UUID_RE.match(mb_albumid):
        return jsonify({"ok": False, "error": "Release MBID must be a valid MusicBrainz UUID."}), 400
    album_item_ids = {int(getattr(item, "id", 0) or 0) for item in album.items()}
    clean_recordings = []
    for row in recordings if isinstance(recordings, list) else []:
        item_id = int((row or {}).get("item_id") or 0)
        mb_trackid = _s((row or {}).get("mb_trackid") or (row or {}).get("recording_mbid") or "").strip().lower()
        if item_id not in album_item_ids:
            return jsonify({"ok": False, "error": f"Item {item_id} does not belong to album {aid}."}), 400
        if mb_trackid and not _MB_UUID_RE.match(mb_trackid):
            return jsonify({"ok": False, "error": f"Recording MBID for item {item_id} is not a valid UUID."}), 400
        if mb_trackid:
            clean_recordings.append({"item_id": item_id, "mb_trackid": mb_trackid})

    def _do(log, cancel_event=None, update_state=None):
        cfg = _write_job_beets_config(f"/tmp/beets_attach_mbids_{uuid.uuid4().hex}.yaml")
        query = f"album_id:{aid}"
        modify_args = [f"mb_albumartistid={mb_albumartistid}", f"mb_releasegroupid={mb_releasegroupid}"]
        if mb_albumid:
            modify_args.append(f"mb_albumid={mb_albumid}")
        log.append(f"Attaching MusicBrainz album IDs to album {aid}.")
        if update_state:
            update_state(stage="album_ids", completed=0, total=len(clean_recordings) + 2)
        result = _beet_run([BEET_BIN, "-c", cfg, "modify", "--yes", "--nowrite", query] + modify_args, log, timeout=120, env=_beet_env(), cancel=cancel_event)
        output = _append_clean_output(log, result.stdout, result.stderr)
        if result.returncode not in (0, -9, 124):
            raise RuntimeError(f"beet modify failed with exit code {result.returncode}")
        for idx, row in enumerate(clean_recordings, start=1):
            if cancel_event is not None and cancel_event.is_set():
                return {"cancelled": True}
            if update_state:
                update_state(stage="recording_ids", current_track=idx, completed=idx, total=len(clean_recordings) + 2)
            log.append(f"Attaching recording MBID to item {row['item_id']}.")
            result = _beet_run([BEET_BIN, "-c", cfg, "modify", "--yes", "--nowrite", f"id:{row['item_id']}", f"mb_trackid={row['mb_trackid']}"], log, timeout=60, env=_beet_env(), cancel=cancel_event)
            chunk = _append_clean_output(log, result.stdout, result.stderr)
            output = (output + "\n" + chunk).strip()
            if result.returncode not in (0, -9, 124):
                raise RuntimeError(f"beet modify failed for item {row['item_id']} with exit code {result.returncode}")
        if update_state:
            update_state(stage="write_tags", completed=len(clean_recordings) + 1, total=len(clean_recordings) + 2)
        log.append("Writing updated MusicBrainz IDs to file tags.")
        result = _beet_run([BEET_BIN, "-c", cfg, "write", "--yes", query], log, timeout=240, env=_beet_env(), cancel=cancel_event)
        chunk = _append_clean_output(log, result.stdout, result.stderr)
        output = (output + "\n" + chunk).strip()
        if result.returncode not in (0, -9, 124):
            raise RuntimeError(f"beet write failed with exit code {result.returncode}")
        updated_album = lib.get_album(aid)
        verify_albumartist = _s(getattr(updated_album, "mb_albumartistid", "") or getattr(updated_album, "mb_albumartistids", "") or "").lower() if updated_album else ""
        verify_releasegroup = _s(getattr(updated_album, "mb_releasegroupid", "") or "").lower() if updated_album else ""
        verify_release = _s(getattr(updated_album, "mb_albumid", "") or "").lower() if updated_album else ""
        if not updated_album or mb_albumartistid not in verify_albumartist or verify_releasegroup != mb_releasegroupid or (mb_albumid and verify_release != mb_albumid):
            raise RuntimeError("MusicBrainz album IDs were not verified after write.")
        verified_recordings = 0
        for row in clean_recordings:
            item = lib.get_item(row["item_id"])
            if not item or _s(getattr(item, "mb_trackid", "") or "").strip().lower() != row["mb_trackid"]:
                raise RuntimeError(f"Recording MBID was not verified for item {row['item_id']}.")
            verified_recordings += 1
        _invalidate_lib_cache()
        if update_state:
            update_state(stage="verified", completed=len(clean_recordings) + 2, total=len(clean_recordings) + 2)
        log.append(f"Verified MusicBrainz IDs. Updated {verified_recordings} recording ID(s). Files were not moved or renamed.")
        return {"output": output.strip(), "album_id": aid, "recording_mbids_attached": verified_recordings, "files_moved": 0, "verified": True}

    job = jobs.start_python(_do, label=f"Attach MusicBrainz IDs: album {aid}", metadata={"type": "musicbrainz-match", "album_id": aid, "transaction_operation": "MusicBrainz Match"})
    return jsonify({"ok": True, "job_id": job.job_id})

