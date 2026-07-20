"""Music metadata submission routes.

Registered after app.py initializes. Keeps submission-only Beets commands out of
the main app module while reusing the existing JobStore and Beets config helpers.
"""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import threading
import time
import urllib.request
import uuid
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

from flask import jsonify, request

from app import (  # noqa: E402
    AUDIO_EXT,
    BEET_BIN,
    DISCOGS_TOKEN,
    DOWNLOADS_ROOT,
    MUSIC_ROOT,
    _ANSI_RE,
    _MB_UUID_RE,
    _artist_folder_key,
    _beet_env,
    _beet_run,
    _build_folder_evidence,
    _extract_mb_uuid,
    _fetch_mb_release_tracklist,
    _invalidate_lib_cache,
    _mb_artist_search_one,
    _path_is_under,
    _read_beets_plugin_list,
    _s,
    _write_job_beets_config,
    _ytdlp_js_runtime_options,
    _ytdlp_ready,
    _ytdlp_remote_components,
    app,
    jobs,
    lib,
)
from backend.security import OutboundPolicyError, validate_outbound_url

_SUBMISSION_ALLOWED_ROOTS = (MUSIC_ROOT, DOWNLOADS_ROOT)
_REFERENCE_URL_TIMEOUT = 20
_REFERENCE_MAX_BYTES = 2_000_000


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


def _submission_key(target_type: str, target_ref: Any) -> str:
    if target_type == "folder":
        digest = hashlib.sha1(_s(target_ref).encode("utf-8")).hexdigest()[:24]
        return f"folder:{digest}"
    return f"{target_type}:{int(target_ref)}"


def _submission_drafts() -> Dict[str, Any]:
    payload = _submission_json_load(_SUBMISSION_DRAFTS_FILE, {})
    return payload if isinstance(payload, dict) else {}


def _submission_draft(target_type: str, target_ref: Any) -> Dict[str, Any]:
    draft = _submission_drafts().get(_submission_key(target_type, target_ref), {})
    return draft if isinstance(draft, dict) else {}


def _save_submission_draft(target_type: str, target_ref: Any, draft: Dict[str, Any]) -> Dict[str, Any]:
    drafts = _submission_drafts()
    clean = draft if isinstance(draft, dict) else {}
    clean["target_type"] = target_type
    clean["target_id"] = target_ref if target_type == "folder" else int(target_ref)
    clean["updated_at"] = time.time()
    drafts[_submission_key(target_type, target_ref)] = clean
    _submission_json_save(_SUBMISSION_DRAFTS_FILE, drafts)
    return clean


def _delete_submission_draft(target_type: str, target_ref: Any) -> bool:
    drafts = _submission_drafts()
    key = _submission_key(target_type, target_ref)
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
        "resolved_state": "imported_album",
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
        "resolved_state": "imported_singleton",
    }


# -- Folder resolution (unimported/loose-track review items) --------------------

def _abs_resolved(path_str: str) -> Path:
    return Path(path_str).expanduser().resolve(strict=False)


def _find_beets_album_for_folder(folder: Path):
    target = str(folder)
    for album in lib.albums():
        try:
            album_dir = str(Path(album.item_dir()).resolve(strict=False))
        except Exception:
            continue
        if album_dir == target:
            return album
    return None


def _find_beets_items_for_folder(folder: Path) -> List[Any]:
    target = str(folder)
    matches = []
    for item in lib.items():
        try:
            item_path = _item_abs_path(item)
            if not item_path:
                continue
            if str(Path(item_path).parent.resolve(strict=False)) == target:
                matches.append(item)
        except Exception:
            continue
    return matches


def _media_tag_track_payload(file_path: Path, index: int) -> Dict[str, Any]:
    exists = file_path.exists()
    title = artist = album = albumartist = fmt = ""
    track = disc = year = 0
    duration = 0.0
    mb_trackid = ""
    if exists:
        try:
            from beets.mediafile import MediaFile
            mf = MediaFile(str(file_path))
            title = _s(mf.title or "").strip()
            artist = _s(mf.artist or "").strip()
            album = _s(mf.album or "").strip()
            albumartist = _s(getattr(mf, "albumartist", "") or artist).strip()
            track = int(mf.track or 0)
            disc = int(mf.disc or 0)
            year = int(getattr(mf, "year", 0) or getattr(mf, "original_year", 0) or 0)
            duration = float(mf.length or 0)
            mb_trackid = _s(getattr(mf, "mb_trackid", "") or "").strip().lower()
            fmt = file_path.suffix.lstrip(".").upper()
        except Exception:
            pass
    if not title:
        title = file_path.stem
    if not exists:
        validation = "File unavailable"
    elif not title.strip():
        validation = "Missing track title"
    else:
        validation = "Not imported to Beets yet"
    return {
        "index": index,
        "item_id": 0,
        "album_id": 0,
        "disc": disc or 1,
        "track": track or index,
        "title": title,
        "artist": artist,
        "album": album,
        "albumartist": albumartist,
        "duration": duration,
        "duration_display": _duration_label(duration),
        "file_name": file_path.name,
        "file_path": str(file_path),
        "file_available": exists,
        "format": fmt,
        "year": year,
        "mb_trackid": mb_trackid,
        "mb_albumid": "",
        "fingerprint_status": "File unavailable" if not exists else "Not imported to Beets yet",
        "validation_status": validation,
    }


def _folder_cover_art_url(folder: Path) -> str:
    if not folder.is_dir():
        return ""
    try:
        candidates = sorted(p for p in folder.glob("*") if p.is_file() and p.suffix.lower().lstrip(".") in {"jpg", "jpeg", "png"})
    except Exception:
        return ""
    for art_path in candidates:
        if any(_path_is_under(art_path, root) for root in _SUBMISSION_ALLOWED_ROOTS):
            from urllib.parse import quote
            return f"/api/disk-art?path={quote(str(art_path))}"
    return ""


def _empty_folder_summary(folder: Path, resolved_state: str = "empty") -> Dict[str, Any]:
    return {
        "target_type": "folder", "album_id": 0, "item_id": 0,
        "title": folder.name, "albumartist": "", "release_type": "", "secondary_type": "",
        "release_status": "", "release_date": "", "country": "", "label": "",
        "catalog_number": "", "barcode": "", "format": "", "disc_count": 0,
        "track_count": 0, "runtime": 0, "runtime_display": "", "source_path": str(folder),
        "mb_albumartistid": "", "mb_albumartistids": "", "mb_releasegroupid": "", "mb_albumid": "",
        "cover_art_url": "", "resolved_state": resolved_state,
    }


def _summary_for_folder_tracks(folder: Path, tracks: List[Dict[str, Any]], resolved_state: str, evidence: Dict[str, Any] = None) -> Dict[str, Any]:
    evidence = evidence or {}
    runtime = sum(float(t.get("duration") or 0) for t in tracks)
    discs = sorted({int(t.get("disc") or 1) for t in tracks}) or [1]
    tag_albums = [t.get("album", "").strip() for t in tracks if t.get("album", "").strip()]
    tag_artists = [(t.get("albumartist") or t.get("artist") or "").strip() for t in tracks if (t.get("albumartist") or t.get("artist"))]
    tag_years = [str(int(t.get("year"))) for t in tracks if t.get("year")]
    title = (max(set(tag_albums), key=tag_albums.count) if tag_albums else "") or evidence.get("guessed_album") or folder.name
    albumartist = (max(set(tag_artists), key=tag_artists.count) if tag_artists else "") or evidence.get("guessed_artist") or ""
    release_date = (max(set(tag_years), key=tag_years.count) if tag_years else "") or evidence.get("guessed_year", "")
    return {
        "target_type": "folder", "album_id": 0, "item_id": 0,
        "title": title, "albumartist": albumartist,
        "release_type": "Album" if resolved_state in ("unimported_album", "imported_singletons") else "Track",
        "secondary_type": "", "release_status": "",
        "release_date": release_date, "country": "", "label": "",
        "catalog_number": "", "barcode": "", "format": (tracks[0].get("format") if tracks else "") or "",
        "disc_count": len(discs), "track_count": len(tracks), "runtime": runtime,
        "runtime_display": _duration_label(runtime), "source_path": str(folder),
        "mb_albumartistid": "", "mb_albumartistids": "", "mb_releasegroupid": "", "mb_albumid": "",
        "cover_art_url": _folder_cover_art_url(folder), "resolved_state": resolved_state,
    }


def _folder_audio_file_listing(folder: Path) -> List[Path]:
    """Just the audio file paths under a folder -- same direct-children-else-
    recursive rule _build_folder_evidence uses, but without also opening and
    tag-parsing every file. Listing directory entries is cheap; MediaFile()
    parsing each one over networked storage is not, so callers that only
    need "which files are here" (not their tags) should use this instead."""
    direct = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXT)
    if direct:
        return direct
    return sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXT)


def _resolve_folder_submission_target(path: str) -> Tuple[str, Any, Dict[str, Any], List[Dict[str, Any]]]:
    """Resolve a review-item source path into tracks without requiring a prior
    Beets import. Detects: imported album/singletons (delegates to the normal
    Beets-backed resolution), an unimported album folder, a loose-track folder,
    or an empty/inaccessible path. The detected state is stamped on
    summary['resolved_state']."""
    raw_path = _s(path).strip()
    if not raw_path:
        raise ValueError("This review item has no source path to resolve.")
    folder = _abs_resolved(raw_path)

    if not folder.exists():
        return "folder", str(folder), _empty_folder_summary(folder, "inaccessible"), []

    if folder.is_file():
        if folder.suffix.lower() not in AUDIO_EXT:
            return "folder", str(folder), _empty_folder_summary(folder, "inaccessible"), []
        track = _media_tag_track_payload(folder, 1)
        summary = _summary_for_folder_tracks(folder.parent, [track], "loose_tracks")
        return "folder", str(folder), summary, [track]

    beets_album = _find_beets_album_for_folder(folder)
    if beets_album is not None:
        tracks = _album_track_rows(beets_album)
        summary = _summary_for_album(beets_album, tracks)
        return "album", int(beets_album.id), summary, tracks

    # _media_tag_track_payload (below) already reads every file's tags once
    # for the actual track payloads; _build_folder_evidence does its own
    # separate MediaFile() pass over the same files purely to guess an
    # artist/album/year for cases where tags are missing. Listing files
    # doesn't need that second tag-reading pass at all, and the
    # imported_singletons branch below doesn't either (it builds its
    # summary from Beets item rows, not from evidence) -- so only pay for
    # the full evidence scan lazily, once we know tags alone weren't enough.
    audio_paths = _folder_audio_file_listing(folder)
    beets_items = _find_beets_items_for_folder(folder)

    if beets_items and len(beets_items) >= max(1, len(audio_paths)):
        ordered = sorted(beets_items, key=lambda i: (int(getattr(i, "disc", 0) or 0), int(getattr(i, "track", 0) or 0), int(getattr(i, "id", 0) or 0)))
        tracks = [_track_payload(item, idx + 1) for idx, item in enumerate(ordered)]
        summary = _summary_for_folder_tracks(folder, tracks, "imported_singletons")
        return "folder", str(folder), summary, tracks

    if not audio_paths:
        return "folder", str(folder), _empty_folder_summary(folder, "empty"), []

    tracks = [_media_tag_track_payload(p, idx + 1) for idx, p in enumerate(audio_paths)]
    albums_seen = {t.get("album", "").strip().lower() for t in tracks if t.get("album", "").strip()}
    resolved_state = "unimported_album" if len(albums_seen) <= 1 else "loose_tracks"
    # _summary_for_folder_tracks falls back to evidence's guessed_album/
    # guessed_artist/guessed_year only when tags didn't supply them; skip
    # the (separate, redundant) full evidence scan only when tags already
    # cover all three, not just album/artist -- year has no other fallback.
    has_usable_tags = (
        any(t.get("album") or t.get("albumartist") for t in tracks)
        and any(t.get("year") for t in tracks)
    )
    evidence = {} if has_usable_tags else _build_folder_evidence(str(folder))
    summary = _summary_for_folder_tracks(folder, tracks, resolved_state, evidence=evidence)
    return "folder", str(folder), summary, tracks


# Stage taxonomy for the readiness UI. Order matters: a check's stage index
# vs. the target's current-stage index decides whether it is shown as
# "current stage" (index <= current) or hidden as future-stage noise.
_STAGE_ORDER = ["artist", "identify", "musicbrainz_prep", "attach_ids", "acoustid", "complete"]
_STAGE_LABELS = {
    "artist": "Artist identification",
    "identify": "Identify and review",
    "musicbrainz_prep": "MusicBrainz preparation",
    "attach_ids": "Attach published IDs",
    "acoustid": "AcoustID submission",
    "complete": "Complete",
}
_ARTIST_MATCH_MIN_SCORE = 95


def _submission_artist_match(albumartist: str) -> Dict[str, Any]:
    """Best-effort MusicBrainz artist lookup for the album artist credit.

    Reuses _mb_artist_search_one (already used by artist-folder cleanup to
    pick canonical spellings) instead of adding a second MusicBrainz artist
    search. Only returns a match confident enough to auto-attach without
    review: an exact name/alias match, or a very high search score.
    """
    name = _s(albumartist).strip()
    if not name:
        return {}
    match = _mb_artist_search_one(name)
    if not match or not _MB_UUID_RE.match(_s(match.get("id")).strip()):
        return {}
    confident = (
        _artist_folder_key(_s(match.get("name"))) == _artist_folder_key(name)
        or int(match.get("score") or 0) >= _ARTIST_MATCH_MIN_SCORE
    )
    if not confident:
        return {}
    return {
        "id": _s(match.get("id")).strip().lower(),
        "name": _s(match.get("name")),
        "score": int(match.get("score") or 0),
        "disambiguation": _s(match.get("disambiguation")),
    }


def _check(check_id: str, label: str, ok: bool, stage: str, group: str, explanation: str,
           action: str = "", action_type: str = "", action_target: str = "",
           affected: List[str] = None, blocking: bool = True) -> Dict[str, Any]:
    """Build one normalized preflight check.

    A passed check always carries empty explanation/action/action_type —
    the previous version always kept whatever `action` text the caller
    passed in, so a check that reported "pass" could still render its
    corrective text (the actual cause of "check X passed but says to fix
    X" everywhere in the old UI). Severity (blocked/needs_attention/ready)
    is the one field the frontend should key off of; status is kept only
    for backward compatibility with older stored drafts/log text.
    """
    if ok:
        status, severity = "pass", "ready"
        explanation, action, action_type, action_target = "", "", "", ""
    elif blocking:
        status, severity = "fail", "blocked"
    else:
        status, severity = "warning", "needs_attention"
    return {
        "id": check_id, "label": label, "status": status, "severity": severity,
        "stage": stage, "group": group,
        "explanation": explanation, "action": action,
        "action_type": action_type, "action_target": action_target,
        "affected": affected or [], "blocking": blocking,
    }


def _submission_preflight(summary: Dict[str, Any], tracks: List[Dict[str, Any]], readiness: Dict[str, Any],
                           artist_match: Dict[str, Any] = None, artist_dismissed: bool = False,
                           duplicates_reviewed: bool = False) -> Dict[str, Any]:
    missing_files = [t.get("file_name") or f"item {t.get('item_id')}" for t in tracks if not t.get("file_available")]
    missing_titles = [f"track {t.get('track') or t.get('index')}" for t in tracks if not _s(t.get("title")).strip()]
    bad_tracks = [f"item {t.get('item_id')}" for t in tracks if int(t.get("track") or 0) <= 0]
    bad_discs = [f"item {t.get('item_id')}" for t in tracks if int(t.get("disc") or 0) <= 0]
    no_duration = [f"track {t.get('track') or t.get('index')}" for t in tracks if float(t.get("duration") or 0) <= 0]
    missing_recordings = [f"track {t.get('track') or t.get('index')}" for t in tracks if not _MB_UUID_RE.match(_s(t.get("mb_trackid")).strip())]
    plugins = readiness.get("plugins") or {}
    resolved_state = _s(summary.get("resolved_state") or "").strip()
    is_beets_target = summary.get("target_type") in ("album", "item")
    # There's nothing to identify an artist for yet if the folder itself
    # couldn't be read or has no audio files -- that's an identify-stage
    # problem (see local_files_found below), not an unresolved artist. Left
    # unguarded, an empty/inaccessible folder has no albumartist to search
    # for, so artist_resolved would always be False and the "artist" stage
    # would incorrectly outrank "identify" and show "artist not found"
    # instead of the real "folder not found"/"no audio files" message.
    artist_resolved = (
        bool(_s(summary.get("mb_albumartistid")).strip())
        or bool((artist_match or {}).get("id"))
        or resolved_state in ("inaccessible", "empty")
    )
    state_explanations = {
        "inaccessible": "The source path could not be found or read on disk.",
        "empty": "No supported audio files were found in the source folder.",
        "unimported_album": "This folder has not been imported into the Beets library yet.",
        "loose_tracks": "These files were found on disk but do not share consistent album metadata.",
        "imported_singletons": "These files are already Beets library items, but not grouped as an album.",
    }
    checks = [
        _check("artist_resolved", "MusicBrainz artist found",
               artist_resolved or artist_dismissed, "artist", "musicbrainz",
               "No confident MusicBrainz match was found for this artist name.",
               "Create this artist on MusicBrainz, or paste an artist ID/URL you already have.",
               "resolve_artist", blocking=True),
        _check("local_files_found", "Local audio files found",
               bool(tracks) and resolved_state not in ("inaccessible", "empty"), "identify", "local_files",
               state_explanations.get(resolved_state, "No audio files were found for this review item."),
               "Scan the folder for audio files again." if resolved_state in ("inaccessible", "empty") else "",
               "rescan", blocking=True),
        _check("beets_imported", "Album imported into Beets",
               is_beets_target, "musicbrainz_prep", "musicbrainz",
               "This folder has not been imported into the Beets library yet.",
               "Import this album from Import Review.", "open_import_review"),
        _check("files_accessible", "Local files accessible",
               not missing_files, "identify", "local_files",
               "Some files are missing or unreadable.",
               "Restore the missing files or remove them from the album.", "edit_tracks", "submission-tracks",
               affected=missing_files),
        _check("artist_credit", "Artist credit present",
               bool(_s(summary.get("albumartist")).strip()), "identify", "metadata",
               "Album artist is empty.", "Enter a release artist credit.", "edit_metadata", "submission-metadata"),
        _check("album_title", "Album title present",
               bool(_s(summary.get("title")).strip()), "identify", "metadata",
               "Release title is empty.", "Enter a release title.", "edit_metadata", "submission-metadata"),
        _check("track_titles", "Track titles present",
               not missing_titles, "identify", "metadata",
               "One or more tracks have no title.", "Fill in the missing track titles.", "edit_tracks", "submission-tracks",
               affected=missing_titles),
        _check("track_positions", "Track positions valid",
               not bad_tracks, "identify", "local_files",
               "One or more tracks have an invalid track number.", "Fix track numbers.", "edit_tracks", "submission-tracks",
               affected=bad_tracks),
        _check("disc_positions", "Disc positions valid",
               not bad_discs, "identify", "local_files",
               "One or more tracks have an invalid disc number.", "Fix disc numbers.", "edit_tracks", "submission-tracks",
               affected=bad_discs),
        _check("track_durations", "Track durations available",
               not no_duration, "identify", "local_files",
               "Some tracks have no duration. MusicBrainz can still be prepared, but duplicate checks are weaker.",
               "Refresh Beets metadata for the affected files.", "edit_tracks", "submission-tracks",
               affected=no_duration, blocking=False),
        _check("release_date", "Release date set",
               bool(_s(summary.get("release_date")).strip()), "musicbrainz_prep", "metadata",
               "Release date is missing. Optional, but recommended before publishing.",
               "Add a year or full release date.", "edit_metadata", "submission-metadata", blocking=False),
        _check("release_format", "Media format selected",
               bool(_s(summary.get("format")).strip()), "musicbrainz_prep", "metadata",
               "Media format is unknown. Optional, but recommended before publishing.",
               "Choose the media format.", "edit_metadata", "submission-metadata", blocking=False),
        _check("duplicates_reviewed", "Possible existing releases reviewed",
               duplicates_reviewed, "musicbrainz_prep", "musicbrainz",
               "Review likely existing releases before creating a new one.",
               "Review possible duplicate releases.", "review_duplicates", "submission-duplicates", blocking=False),
        _check("mbsubmit_plugin", "MusicBrainz submission tool enabled",
               bool(plugins.get("mbsubmit")), "musicbrainz_prep", "musicbrainz",
               "The Beets mbsubmit plugin is not enabled in config.yaml.",
               "Enable the mbsubmit plugin in config.yaml.", "view_setup_details"),
        _check("recording_mbids", "MusicBrainz recording IDs attached",
               not missing_recordings, "acoustid", "acoustid",
               "Some tracks do not have MusicBrainz recording MBIDs.",
               "Attach recording IDs before submitting fingerprints.", "open_mb_handoff", "submission-mb-handoff",
               affected=missing_recordings),
        _check("acoustid_api_key", "AcoustID connection available",
               bool(readiness.get("acoustid_key_configured")), "acoustid", "acoustid",
               "The AcoustID API key is not configured.",
               "Add your AcoustID API key in Settings.", "open_settings"),
        _check("fpcalc_available", "Fingerprinting tools available",
               bool(readiness.get("fpcalc_available")), "acoustid", "system",
               "fpcalc was not found in the application container.",
               "Install chromaprint/fpcalc in the runtime.", "view_setup_details"),
        _check("pyacoustid_available", "AcoustID library available",
               bool(readiness.get("pyacoustid_available")), "acoustid", "system",
               "The Python acoustid module is not available.",
               "Install pyacoustid in the runtime.", "view_setup_details"),
        _check("chroma_plugin", "Chroma integration enabled",
               bool(plugins.get("chroma")), "acoustid", "acoustid",
               "The Beets chroma plugin is not enabled.",
               "Enable the chroma plugin.", "view_setup_details"),
    ]
    mb_blocked = any(c["blocking"] and c["status"] == "fail" and c["stage"] in ("artist", "identify", "musicbrainz_prep") for c in checks)
    acoustid_blocked = mb_blocked or any(c["blocking"] and c["status"] == "fail" and c["stage"] == "acoustid" for c in checks)
    return {
        "checks": checks,
        "missing_count": sum(1 for c in checks if c["status"] == "fail"),
        "warning_count": sum(1 for c in checks if c["status"] == "warning"),
        "musicbrainz_ready": not mb_blocked,
        "acoustid_ready": not acoustid_blocked,
    }


def _submission_stage_id(workflow_stage: str) -> str:
    """Map the existing free-text workflow_stage to one of _STAGE_ORDER.

    Mirrors the same substring precedence the frontend stepper already uses
    (stepIndex() in Submissions.tsx) so both stay in agreement without the
    checklist re-deriving progress through a second, divergent code path.
    """
    text = (workflow_stage or "").lower()
    if "complete" in text:
        return "complete"
    if "acoustid" in text:
        return "acoustid"
    if "waiting" in text or "ids" in text or "prepared" in text:
        return "attach_ids"
    if "ready for musicbrainz" in text:
        return "musicbrainz_prep"
    return "identify"


def _submission_current_stage(preflight: Dict[str, Any], workflow_stage: str) -> str:
    """More precise stage id than the free-text workflow_stage alone.

    `musicbrainz_ready` blocks on both "identify" and "musicbrainz_prep"
    checks together (e.g. missing album title AND not-yet-imported both
    keep it False), so workflow_stage can stay "Needs metadata" purely
    because of a musicbrainz_prep-only check like "not imported into
    Beets" even after every identify-stage check has passed. Left alone,
    that would hide the real blocker from the checklist (it only shows
    current-or-earlier-stage checks) while the primary action stays
    disabled for a reason the compact card can't see — exactly the kind
    of card/footer disagreement this rework is meant to prevent. Advance
    past "identify" as soon as identify-stage checks are clean, and only
    defer to the workflow_stage text for the stages after that.
    """
    artist_blocked = any(
        c["blocking"] and c["status"] == "fail" and c["stage"] == "artist"
        for c in preflight["checks"]
    )
    if artist_blocked:
        return "artist"
    identify_blocked = any(
        c["blocking"] and c["status"] == "fail" and c["stage"] == "identify"
        for c in preflight["checks"]
    )
    if identify_blocked:
        return "identify"
    return _submission_stage_id(workflow_stage)


def _annotate_current_stage(preflight: Dict[str, Any], workflow_stage: str) -> Dict[str, Any]:
    """Tag each check with current_stage_relevant and the target's stage id/label.

    A check is relevant once its own stage is reached (current or earlier);
    checks that belong only to a later stage (e.g. AcoustID checks while
    still preparing MusicBrainz metadata) are marked not relevant so the
    default UI can hide them instead of overwhelming the current step.
    """
    stage_id = _submission_current_stage(preflight, workflow_stage)
    current_index = _STAGE_ORDER.index(stage_id)
    for check in preflight["checks"]:
        check_index = _STAGE_ORDER.index(check["stage"]) if check["stage"] in _STAGE_ORDER else 0
        check["current_stage_relevant"] = check_index <= current_index
    preflight["current_stage"] = stage_id
    preflight["current_stage_label"] = _STAGE_LABELS[stage_id]
    return preflight


def _resolve_submission_target(album_id: int = 0, item_id: int = 0, path: str = "", singleton: bool = False) -> Tuple[str, Any, Dict[str, Any], List[Dict[str, Any]]]:
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
    if path:
        return _resolve_folder_submission_target(path)
    raise ValueError("Select a review item with a resolvable path, album, or item first.")


@app.get("/api/submissions/target")
def submission_target():
    try:
        album_id = int(request.args.get("album_id") or 0)
        item_id = int(request.args.get("item_id") or 0)
        path = _s(request.args.get("path") or "").strip()
        singleton = str(request.args.get("singleton") or "").strip().lower() in {"1", "true", "yes"}
        target_type, target_id, summary, tracks = _resolve_submission_target(album_id=album_id, item_id=item_id, path=path, singleton=singleton)
    except KeyError as ex:
        return jsonify({"ok": False, "error": str(ex)}), 404
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 400
    readiness = _submission_readiness()
    draft = _submission_draft(target_type, target_id)
    artist_dismissed = bool(draft.get("artist_dismissed"))
    duplicates_reviewed = bool(draft.get("duplicates_reviewed"))
    manual_artist_id = _s((draft.get("published") or {}).get("artistId") or "").strip().lower()
    if _s(summary.get("mb_albumartistid")).strip():
        artist_match: Dict[str, Any] = {}
    elif manual_artist_id and _MB_UUID_RE.match(manual_artist_id):
        # User pasted an artist ID/URL directly instead of relying on search.
        artist_match = {"id": manual_artist_id, "name": "", "score": 100, "disambiguation": ""}
    else:
        artist_match = _submission_artist_match(summary.get("albumartist"))
    preflight = _submission_preflight(
        summary, tracks, readiness, artist_match=artist_match,
        artist_dismissed=artist_dismissed, duplicates_reviewed=duplicates_reviewed,
    )
    summary["workflow_stage"] = _s(draft.get("stage") or ("Ready for AcoustID" if summary.get("mb_albumid") and summary.get("mb_releasegroupid") and preflight.get("acoustid_ready") else "Waiting for published MBIDs" if draft.get("mbsubmit_output") else "Ready for MusicBrainz" if preflight.get("musicbrainz_ready") else "Needs metadata"))
    preflight = _annotate_current_stage(preflight, summary["workflow_stage"])
    return jsonify({
        "ok": True, "target_type": target_type, "target_id": target_id,
        "summary": summary, "tracks": tracks, "preflight": preflight,
        "readiness": readiness, "draft": draft, "artist_match": artist_match,
    })


def _draft_target_ref(target_type: str, payload_or_args) -> Any:
    if target_type == "folder":
        path = _s(payload_or_args.get("target_path") or payload_or_args.get("target_id") or "").strip()
        if not path:
            raise ValueError("target_path is required for a folder draft.")
        return str(_abs_resolved(path))
    target_id = int(payload_or_args.get("target_id") or 0)
    if target_id <= 0:
        raise ValueError("A positive target_id is required.")
    return target_id


@app.post("/api/submissions/draft")
def save_submission_draft():
    payload = request.get_json(silent=True) or {}
    target_type = _s(payload.get("target_type") or "").strip().lower()
    if target_type not in {"album", "item", "folder"}:
        return jsonify({"ok": False, "error": "Valid target_type and target_id are required."}), 400
    try:
        target_ref = _draft_target_ref(target_type, payload)
    except ValueError as ex:
        return jsonify({"ok": False, "error": str(ex)}), 400
    draft = payload.get("draft") or {}
    if len(json.dumps(draft)) > 1_500_000:
        return jsonify({"ok": False, "error": "Submission draft is too large."}), 413
    return jsonify({"ok": True, "draft": _save_submission_draft(target_type, target_ref, draft)})


@app.delete("/api/submissions/draft")
def reset_submission_draft():
    target_type = _s(request.args.get("target_type") or "").strip().lower()
    if target_type not in {"album", "item", "folder"}:
        return jsonify({"ok": False, "error": "Valid target_type and target_id are required."}), 400
    try:
        target_ref = _draft_target_ref(target_type, request.args)
    except ValueError as ex:
        return jsonify({"ok": False, "error": str(ex)}), 400
    return jsonify({"ok": True, "removed": _delete_submission_draft(target_type, target_ref)})


# -- Reference URLs (YouTube metadata extraction + generic OpenGraph fallback) ---

_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
_MB_HOSTS = {"musicbrainz.org", "www.musicbrainz.org"}
_DISCOGS_HOSTS = {"discogs.com", "www.discogs.com"}
_SOUNDCLOUD_HOSTS = {"soundcloud.com", "www.soundcloud.com"}

_YT_BRACKET_NOISE_RE = re.compile(
    r'\s*[\(\[]\s*(?:official\s+(?:music\s+)?video|official\s+audio|lyric\s+video|lyrics?|'
    r'visualizer|full\s+album|hd|4k)\s*[\)\]]',
    re.IGNORECASE,
)
_YT_BARE_REMASTER_RE = re.compile(r'\bremastered\b(?!\s*\d{4})', re.IGNORECASE)
_YT_TOPIC_SUFFIX_RE = re.compile(r'\s*-\s*Topic\s*$', re.IGNORECASE)
_YT_PROVIDED_TO_RE = re.compile(r'^\s*provided\s+to\s+youtube\s+by\s+', re.IGNORECASE)
_YT_LABEL_CHANNEL_RE = re.compile(r'\b(records?|music|label|entertainment)\b\s*$', re.IGNORECASE)
_OG_META_RE = re.compile(r'<meta[^>]+property=["\']og:([a-zA-Z:]+)["\'][^>]+content=["\']([^"\']*)["\']', re.IGNORECASE)
_TITLE_TAG_RE = re.compile(r'<title[^>]*>([^<]*)</title>', re.IGNORECASE)


def _reference_url_source(host: str) -> str:
    host = (host or "").lower()
    if host in _YT_HOSTS:
        return "youtube"
    if host in _MB_HOSTS:
        return "musicbrainz"
    if host in _DISCOGS_HOSTS:
        return "discogs"
    if host.endswith(".bandcamp.com"):
        return "bandcamp"
    if host in _SOUNDCLOUD_HOSTS:
        return "soundcloud"
    return "web"


def _validate_reference_url(raw: str) -> str:
    text = _s(raw).strip()
    if not text:
        raise ValueError("Paste a URL first.")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http:// and https:// URLs are supported.")
    if not parsed.hostname:
        raise ValueError("URL is missing a host.")
    try:
        validate_outbound_url(text)
    except OutboundPolicyError as ex:
        raise ValueError(f"This URL cannot be fetched: {ex}") from ex
    return text


def _yt_normalize_title(raw_title: str) -> str:
    text = _s(raw_title).strip()
    text = _YT_BRACKET_NOISE_RE.sub('', text)
    text = _YT_BARE_REMASTER_RE.sub('', text)
    return re.sub(r'\s{2,}', ' ', text).strip(' -–—')


def _yt_channel_is_topic(channel: str) -> bool:
    return bool(_YT_TOPIC_SUFFIX_RE.search(_s(channel)))


def _yt_channel_looks_like_label(channel: str) -> bool:
    text = _s(channel).strip()
    if not text:
        return False
    if _YT_PROVIDED_TO_RE.search(text):
        return True
    return bool(_YT_LABEL_CHANNEL_RE.search(text)) and 'topic' not in text.lower()


def _yt_split_artist_title(raw_title: str) -> Tuple[str, str]:
    normalized = _yt_normalize_title(raw_title)
    for sep in (' - ', ' – ', ' — '):
        if sep in normalized:
            artist, title = normalized.split(sep, 1)
            return artist.strip(), title.strip()
    return '', normalized


def _yt_thumbnails(info: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = info.get("thumbnails") or []
    out = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        url = _s(t.get("url"))
        if not url:
            continue
        out.append({"url": url, "width": int(t.get("width") or 0), "height": int(t.get("height") or 0)})
    out.sort(key=lambda t: (t.get("width") or 0) * (t.get("height") or 0), reverse=True)
    return out[:8]


class _YtdlpUnsupportedUrlError(Exception):
    """Raised when yt-dlp has no extractor for a given URL, so the caller can
    fall back to a generic scrape instead of surfacing a hard error."""


def _extract_ytdlp_info(url: str) -> Dict[str, Any]:
    """Metadata-only extraction via yt-dlp. Works for YouTube and the many
    other sites yt-dlp has native extractors for (SoundCloud, Bandcamp,
    Vimeo, Mixcloud, etc.) - it auto-detects the right extractor from the URL."""
    if not _ytdlp_ready.wait(timeout=30):
        raise RuntimeError("yt-dlp is still installing; try again in about 30 seconds.")
    import yt_dlp
    ydl_opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "simulate": True,
        "socket_timeout": _REFERENCE_URL_TIMEOUT,
        "extract_flat": "in_playlist",
        "js_runtimes": _ytdlp_js_runtime_options(),
        "remote_components": _ytdlp_remote_components(),
    }
    result: Dict[str, Any] = {}
    errors: Dict[str, Any] = {}

    def _run():
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # process=False skips format-selection entirely (we only want
                # metadata, never a downloadable stream), so a site requiring a
                # JS/PO-token challenge for format resolution doesn't block
                # metadata extraction with "Requested format is not available".
                info = ydl.extract_info(url, download=False, process=False)
                if isinstance(info, dict) and info.get("_type") not in ("playlist", "multi_video"):
                    info = ydl.sanitize_info(info)
                result["info"] = info
        except Exception as ex:  # noqa: BLE001 - surfaced to the caller as a plain message
            errors["error"] = str(ex)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout=_REFERENCE_URL_TIMEOUT + 10)
    if worker.is_alive():
        raise TimeoutError("Metadata extraction timed out.")
    if errors.get("error"):
        message = errors["error"]
        if "unsupported url" in message.lower():
            raise _YtdlpUnsupportedUrlError(message)
        raise RuntimeError(message)
    info = result.get("info")
    if not isinstance(info, dict):
        raise RuntimeError("yt-dlp returned no data for that URL.")
    return info


def _describe_ytdlp_entry(info: Dict[str, Any]) -> Dict[str, Any]:
    is_playlist = info.get("_type") == "playlist"
    entries = [e for e in (info.get("entries") or []) if isinstance(e, dict)] if is_playlist else []
    raw_title = _s(info.get("title"))
    channel = _s(info.get("channel") or info.get("uploader") or "")
    uploader = _s(info.get("uploader") or "")
    description = _s(info.get("description"))[:4000]
    duration = float(info.get("duration") or 0)
    upload_date = _s(info.get("upload_date"))
    year = upload_date[:4] if len(upload_date) >= 4 and upload_date[:4].isdigit() else _s(info.get("release_year") or "")

    yt_artist_tag = _s(info.get("artist") or "")
    yt_track_tag = _s(info.get("track") or "")
    yt_album_tag = _s(info.get("album") or "")

    parsed_artist, parsed_title = _yt_split_artist_title(raw_title)
    is_topic = _yt_channel_is_topic(channel)
    label_like = _yt_channel_looks_like_label(channel)

    proposed_artist = yt_artist_tag or (_YT_TOPIC_SUFFIX_RE.sub('', channel).strip() if is_topic else parsed_artist)
    proposed_title = yt_track_tag or parsed_title or _yt_normalize_title(raw_title)
    proposed_album = yt_album_tag or (_yt_normalize_title(raw_title) if is_playlist else "")

    if yt_artist_tag:
        artist_confidence = "high"
    elif is_topic:
        artist_confidence = "medium"
    elif label_like:
        artist_confidence = "low"
    elif parsed_artist:
        artist_confidence = "medium"
    else:
        artist_confidence = "low"

    fields = []
    if proposed_artist:
        source = "youtube_metadata" if yt_artist_tag else ("youtube_channel" if is_topic else "youtube_title")
        fields.append({"field": "artist", "value": proposed_artist, "source": source, "confidence": artist_confidence})
    if proposed_title:
        fields.append({"field": "title", "value": proposed_title, "source": "youtube_metadata" if yt_track_tag else "youtube_title", "confidence": "high" if yt_track_tag else "medium"})
    if proposed_album:
        fields.append({"field": "album", "value": proposed_album, "source": "youtube_metadata" if yt_album_tag else "youtube_playlist", "confidence": "high" if yt_album_tag else "medium"})
    if year:
        fields.append({"field": "year", "value": year, "source": "youtube_upload_date", "confidence": "low"})

    mb_links = re.findall(r'https?://(?:www\.)?musicbrainz\.org/release[a-zA-Z0-9\-/]*', description)
    discogs_links = re.findall(r'https?://(?:www\.)?discogs\.com/release/[a-zA-Z0-9\-/]*', description)

    return {
        "raw": {
            "title": raw_title, "channel": channel, "uploader": uploader, "description": description,
            "duration": duration, "duration_display": _duration_label(duration),
            "upload_date": upload_date, "is_playlist": is_playlist, "entry_count": len(entries),
        },
        "normalized": {
            "artist": proposed_artist, "title": proposed_title, "album": proposed_album, "year": year,
            "is_topic_channel": is_topic, "likely_label_channel": label_like,
        },
        "fields": fields,
        "artwork_candidates": _yt_thumbnails(info),
        "playlist_entries": [
            {"title": _s(e.get("title")), "duration": float(e.get("duration") or 0), "url": _s(e.get("url") or e.get("webpage_url") or "")}
            for e in entries[:200]
        ] if is_playlist else [],
        "mb_links": mb_links[:5],
        "discogs_links": discogs_links[:5],
    }


_DISCOGS_RELEASE_ID_RE = re.compile(r'/release/(\d+)')
_DISCOGS_MASTER_ID_RE = re.compile(r'/master/(\d+)')


def _discogs_release_id_from_url(url: str) -> Tuple[str, str]:
    m = _DISCOGS_RELEASE_ID_RE.search(urlparse(url).path)
    if m:
        return "release", m.group(1)
    m = _DISCOGS_MASTER_ID_RE.search(urlparse(url).path)
    if m:
        return "master", m.group(1)
    return "", ""


def _fetch_discogs_release(entity_type: str, entity_id: str) -> Dict[str, Any]:
    endpoint = "masters" if entity_type == "master" else "releases"
    headers = {"User-Agent": "BeetsWebControl/1.0 (reference-url fetcher)"}
    if DISCOGS_TOKEN:
        headers["Authorization"] = f"Discogs token={DISCOGS_TOKEN}"
    req = urllib.request.Request(f"https://api.discogs.com/{endpoint}/{entity_id}", headers=headers)
    with urllib.request.urlopen(req, timeout=_REFERENCE_URL_TIMEOUT) as resp:
        data = json.loads(resp.read(_REFERENCE_MAX_BYTES))

    artists = data.get("artists") or []
    artist = ", ".join(_s(a.get("name")).replace(" (2)", "").strip() for a in artists if a.get("name")) if isinstance(artists, list) else ""
    title = _s(data.get("title") or "")
    year = _s(data.get("year") or "")
    country = _s(data.get("country") or "")
    labels = data.get("labels") or []
    label = _s((labels[0] or {}).get("name") or "") if labels else ""
    catalog_number = _s((labels[0] or {}).get("catno") or "") if labels else ""
    formats = data.get("formats") or []
    fmt = ", ".join(_s(f.get("name")) for f in formats if isinstance(f, dict) and f.get("name"))

    tracklist = []
    for track in (data.get("tracklist") or []):
        if not isinstance(track, dict) or _s(track.get("type_") or "track") != "track":
            continue
        tracklist.append({"position": _s(track.get("position")), "title": _s(track.get("title")), "duration": _s(track.get("duration"))})

    images = data.get("images") or []
    artwork = []
    for img in images:
        if not isinstance(img, dict) or not img.get("uri"):
            continue
        artwork.append({"url": img["uri"], "width": int(img.get("width") or 0), "height": int(img.get("height") or 0)})
    artwork.sort(key=lambda a: 0 if any(i.get("uri") == a["url"] and i.get("type") == "primary" for i in images) else 1)

    fields = []
    if artist:
        fields.append({"field": "artist", "value": artist, "source": "discogs_release", "confidence": "high"})
    if title:
        fields.append({"field": "title", "value": title, "source": "discogs_release", "confidence": "high"})
    if year:
        fields.append({"field": "year", "value": year, "source": "discogs_release", "confidence": "high"})

    return {
        "raw": {"title": title, "artist": artist, "year": year, "country": country, "label": label, "catalog_number": catalog_number, "format": fmt, "tracklist_count": len(tracklist)},
        "normalized": {"artist": artist, "title": title, "album": title, "year": year},
        "fields": fields,
        "artwork_candidates": artwork[:8],
        "playlist_entries": [{"title": t["title"], "duration": 0.0, "url": ""} for t in tracklist[:200]],
        "discogs_release_id": entity_id,
        "discogs_entity_type": entity_type,
    }


def _fetch_open_graph_metadata(url: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "beets-web-manager reference-url fetcher"})
    with urllib.request.urlopen(req, timeout=_REFERENCE_URL_TIMEOUT) as resp:
        raw = resp.read(_REFERENCE_MAX_BYTES)
    html = raw.decode("utf-8", errors="replace")
    og: Dict[str, str] = {}
    for m in _OG_META_RE.finditer(html):
        og[m.group(1).lower()] = unescape(m.group(2))
    title = og.get("title") or ""
    if not title:
        tm = _TITLE_TAG_RE.search(html)
        title = unescape(tm.group(1)).strip() if tm else ""
    return {
        "raw": {"title": title, "og_site_name": og.get("site_name", ""), "og_description": og.get("description", "")},
        "normalized": {"artist": "", "title": title, "album": "", "year": ""},
        "fields": ([{"field": "title", "value": title, "source": "web_page_title", "confidence": "low"}] if title else []),
        "artwork_candidates": ([{"url": og["image"], "width": 0, "height": 0}] if og.get("image") else []),
        "playlist_entries": [],
        "mb_links": [],
        "discogs_links": [],
    }


@app.post("/api/submissions/reference-url")
def submission_reference_url():
    payload = request.get_json(silent=True) or {}
    try:
        url = _validate_reference_url(_s(payload.get("url")))
    except ValueError as ex:
        return jsonify({"ok": False, "error": str(ex)}), 400

    album_id = int(payload.get("album_id") or 0)
    item_id = int(payload.get("item_id") or 0)
    path = _s(payload.get("path") or "").strip()
    try:
        if album_id > 0:
            target_type, target_ref = "album", album_id
        elif item_id > 0:
            resolved_type, resolved_id, _summary, _tracks = _resolve_submission_target(item_id=item_id)
            target_type, target_ref = resolved_type, resolved_id
        elif path:
            resolved_type, resolved_id, _summary, _tracks = _resolve_folder_submission_target(path)
            target_type, target_ref = resolved_type, resolved_id
        else:
            raise ValueError("Select a review item before adding a reference URL.")
    except (KeyError, ValueError) as ex:
        return jsonify({"ok": False, "error": str(ex)}), 400

    host = (urlparse(url).hostname or "").lower()
    source = _reference_url_source(host)
    entry: Dict[str, Any] = {"id": uuid.uuid4().hex, "url": url, "source": source, "added_at": time.time()}
    try:
        if source == "musicbrainz":
            entity_type, mbid = _extract_release_input(url)
            entry.update({
                "status": "ok",
                "raw": {"entity_type": entity_type, "mbid": mbid},
                "normalized": {}, "fields": [], "artwork_candidates": [], "playlist_entries": [],
                "mb_entity_type": entity_type, "mb_mbid": mbid,
            })
        elif source == "discogs" and _discogs_release_id_from_url(url)[1]:
            discogs_entity_type, discogs_id = _discogs_release_id_from_url(url)
            entry.update(_fetch_discogs_release(discogs_entity_type, discogs_id))
            entry["status"] = "ok"
        elif source == "youtube":
            # No fallback: yt-dlp is the only sane way to read a YouTube page
            # (heavily JS-rendered, so OpenGraph tags are minimal/unreliable).
            info = _extract_ytdlp_info(url)
            entry.update(_describe_ytdlp_entry(info))
            entry["status"] = "ok"
        elif source in ("soundcloud", "bandcamp"):
            # yt-dlp has dedicated, reliable extractors for these two.
            entry.update(_describe_ytdlp_entry(_extract_ytdlp_info(url)))
            entry["status"] = "ok"
        else:
            # Any other site ("web"): go straight to an OpenGraph scrape, not
            # yt-dlp. yt-dlp's "generic" extractor treats *any* URL as a
            # possible video-embed page and can "succeed" with an empty
            # result instead of raising - which silently threw away real
            # title/description data a plain OpenGraph read would have found.
            entry.update(_fetch_open_graph_metadata(url))
            entry["status"] = "ok"
    except TimeoutError as ex:
        entry["status"] = "error"
        entry["error"] = str(ex) or "Metadata extraction timed out."
    except Exception as ex:  # noqa: BLE001 - surfaced to the caller as a plain message
        entry["status"] = "error"
        entry["error"] = str(ex)

    draft = _submission_draft(target_type, target_ref)
    references = [r for r in (draft.get("reference_urls") or []) if isinstance(r, dict)]
    references.append(entry)
    if len(references) > 20:
        references = references[-20:]
    draft["reference_urls"] = references
    saved = _save_submission_draft(target_type, target_ref, draft)
    return jsonify({"ok": True, "reference": entry, "draft": saved})


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

