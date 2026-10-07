"""Response payload shaping and display formatting (ARCH-001).
"""

from __future__ import annotations

import os, re
import urllib.error, urllib.parse, urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from backend.app_runtime import DOWNLOADS_ALLOWED_ROOTS, DOWNLOADS_ROOT, MUSIC_ROOT, PLAYLIST_DOWNLOAD_ROOT, TORRENT_SOURCE_ROOTS, _s
from backend.config_layers import safe_roots
from backend.app_runtime import _path_has_symlink_component_under, _path_is_under, _path_lexically_under

from flask import jsonify
from backend.beets_adapter import BeetsError

# ── ARCH-001 extracted code ──


def _format_duration(seconds: Any) -> str:
    try:
        total = int(round(float(seconds or 0)))
    except Exception:
        total = 0
    if total <= 0:
        return ""
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _json_from_flask_response(resp) -> Dict[str, Any]:
    """Extract JSON from a Flask Response, a (Response, status) tuple, or a
    service's (json_body, status) result (ARCH-001 request-free services)."""
    if isinstance(resp, tuple):
        resp = resp[0]
    if isinstance(resp, dict):
        return resp
    try:
        return resp.get_json(silent=True) or {}
    except Exception:
        return {}


# Never "/tmp": it holds lock directories and other services' files.
_DOWNLOADS_ROOTS = [str(root) for root in DOWNLOADS_ALLOWED_ROOTS]


def _import_review_path_text_error(raw: Any, *, allow_relative: bool = False) -> Optional[str]:
    text = _s(raw).strip()
    if not text:
        return "Path is required."
    raw_path = Path(text)
    raw_is_absolute = raw_path.is_absolute()
    if not allow_relative and not raw_is_absolute:
        return "Path must be an absolute container path."

    values = [text]
    current = text
    for _ in range(3):
        decoded = urllib.parse.unquote(current)
        if decoded == current:
            break
        values.append(decoded)
        current = decoded

    for value in values:
        if "\x00" in value:
            return "Path contains unsafe encoded characters."
        value_path = Path(value)
        value_is_native_absolute = os.name == "nt" and value_path.is_absolute()
        if value.startswith("\\\\") or value.startswith("//"):
            return "Path must be a POSIX container path."
        if "\\" in value and not value_is_native_absolute:
            return "Path must be a POSIX container path."
        if re.match(r"^[A-Za-z]:", value) and not value_is_native_absolute:
            return "Path must be a POSIX container path."
        if "//" in value:
            return "Path must be a POSIX container path."
        if re.search(r"%(?:2f|5c|00)", value, re.I):
            return "Path contains unsafe encoded characters."
        normalized = value.replace("\\", "/")
        parts = [part for part in normalized.split("/") if part]
        if any(part in {".", ".."} for part in parts):
            return "Path contains unsafe traversal segments."
        if allow_relative and not raw_is_absolute and value_path.is_absolute():
            return "Path contains unsafe encoded characters."
    return None


def _import_review_cleanup_roots(*, allow_music: bool = False) -> List[Path]:
    # Deliberately does NOT include QBIT_REPAIR_ALLOWED_ROOTS: that list is an
    # independently-configurable authorization boundary for a different
    # feature (qBittorrent hardlink-repair destinations, SEC-002 Wave 6).
    # Reusing it here would couple two unrelated features' authorization --
    # reconfiguring one would silently widen or narrow the other. Import
    # review's own TORRENT_SOURCE_ROOTS already covers the same download-area
    # territory for this feature's own purposes, independently configurable.
    roots = [DOWNLOADS_ROOT, PLAYLIST_DOWNLOAD_ROOT] + [Path(root) for root in _DOWNLOADS_ROOTS] + list(TORRENT_SOURCE_ROOTS)
    # Fail closed: "/" or a root overlapping the library would let a cleanup
    # plan delete library files (security F2). The library is added only
    # explicitly, below.
    roots = list(safe_roots("import review cleanup root", roots, MUSIC_ROOT))
    if allow_music:
        roots.append(MUSIC_ROOT)
    # Not "trusted": CodeQL reads that name as a secret (#1387).
    contained_roots: List[Path] = []
    seen: set = set()
    for root in roots:
        try:
            resolved = root.resolve(strict=False)
        except Exception:
            resolved = root
        key = str(resolved).replace("\\", "/").rstrip("/").casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        contained_roots.append(resolved)
    return contained_roots


def _resolve_import_review_source_path(
    raw: Any,
    *,
    allow_music: bool = True,
    expected_type: Optional[str] = None,
    require_exists: bool = True,
) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve an Import Review source file/folder under approved roots."""
    error = _import_review_path_text_error(raw, allow_relative=False)
    if error:
        return None, error
    candidate = Path(_s(raw).strip())
    matched_root: Optional[Path] = None
    for root in _import_review_cleanup_roots(allow_music=allow_music):
        if candidate == root:
            return None, "Refusing to operate on an approved root."
        if _path_lexically_under(candidate, root):
            matched_root = root
            break
    if matched_root is None:
        return None, "Source path is outside the allowed roots."
    if _path_has_symlink_component_under(candidate, matched_root):
        return None, "Source path cannot contain symlink components."
    if candidate.is_symlink():
        return None, "Source path cannot be a symlink."
    if require_exists and not candidate.exists():
        return None, "Source path does not exist."
    if expected_type == "file" and candidate.exists() and not candidate.is_file():
        return None, "Source path is not an audio file."
    if expected_type == "dir" and candidate.exists() and not candidate.is_dir():
        return None, "Source path is not a folder."
    if expected_type is None and candidate.exists() and not (candidate.is_file() or candidate.is_dir()):
        return None, "Source path is not a file or folder."
    try:
        resolved = candidate.resolve(strict=False)
        root_resolved = matched_root.resolve(strict=False)
    except Exception:
        return None, "Invalid source path."
    if resolved == root_resolved:
        return None, "Refusing to operate on an approved root."
    if not _path_is_under(resolved, root_resolved):
        return None, "Source path is outside the allowed roots."
    if expected_type == "file" and resolved.exists() and not resolved.is_file():
        return None, "Source path is not an audio file."
    if expected_type == "dir" and resolved.exists() and not resolved.is_dir():
        return None, "Source path is not a folder."
    return resolved, None


REVIEW_ORIGIN_TYPES = {
    "playlist",
    "batch_import",
    "manual_import",
    "downloads",
    "missing_track_acquisition",
    "cleanup_leftover",
    "unknown",
}


REVIEW_ORIGIN_LABELS = {
    "playlist": "Playlist",
    "batch_import": "Batch",
    "manual_import": "Manual",
    "downloads": "Downloads",
    "missing_track_acquisition": "Missing Tracks",
    "cleanup_leftover": "Cleanup Leftovers",
    "unknown": "Unknown source",
}


REVIEW_ORIGIN_FIELDS = (
    "origin_type",
    "origin_label",
    "origin_id",
    "source_playlist_id",
    "source_playlist_name",
    "source_batch_id",
    "source_folder",
    "created_by_workflow",
)


def _normalize_review_origin_type(value: Any, allow_all: bool = False) -> str:
    raw = _s(value).strip().casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "": "all" if allow_all else "unknown",
        "all": "all",
        "all_sources": "all",
        "playlist_import": "playlist",
        "playlists": "playlist",
        "batch": "batch_import",
        "ai_batch": "batch_import",
        "ai_batch_import": "batch_import",
        "manual": "manual_import",
        "download": "downloads",
        "staging": "downloads",
        "missing_tracks": "missing_track_acquisition",
        "missing_track": "missing_track_acquisition",
        "remaining_files": "cleanup_leftover",
        "remaining_files_review": "cleanup_leftover",
        "cleanup": "cleanup_leftover",
    }
    normalized = aliases.get(raw, raw)
    if allow_all and normalized == "all":
        return "all"
    if normalized in REVIEW_ORIGIN_TYPES:
        return normalized
    return "all" if allow_all and not normalized else "unknown"


def _path_origin_hint(folder_path: str) -> Dict[str, Any]:
    raw = _s(folder_path).strip()
    if not raw or "\x00" in raw or "\\" in raw:
        return {}
    try:
        resolved, err = _resolve_import_review_source_path(raw, allow_music=True, require_exists=False)
        if err or not resolved:
            return {"source_folder": raw}
        playlist_root = PLAYLIST_DOWNLOAD_ROOT.resolve(strict=False)
        if _path_is_under(resolved, playlist_root):
            rel_name = ""
            try:
                parts = resolved.relative_to(playlist_root).parts
                rel_name = _s(parts[0] if parts else "").strip()
            except Exception:
                rel_name = ""
            return {
                "origin_type": "playlist",
                "origin_label": f"Playlist: {rel_name}" if rel_name else "Playlist",
                "source_playlist_name": rel_name,
                "source_folder": raw,
                "created_by_workflow": "playlist_import",
            }
        downloads_root = DOWNLOADS_ROOT.resolve(strict=False)
        if _path_is_under(resolved, downloads_root):
            return {
                "origin_type": "downloads",
                "origin_label": "Downloads",
                "source_folder": raw,
                "created_by_workflow": "downloads",
            }
    except Exception:
        pass
    return {"source_folder": raw}


def _review_origin_payload(folder_path: str = "", suggestion: Optional[Dict[str, Any]] = None,
                           item: Optional[Dict[str, Any]] = None,
                           origin: Optional[Any] = None,
                           evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}

    def absorb(src: Optional[Any]) -> None:
        if not src:
            return
        if isinstance(src, str):
            payload["origin_type"] = src
            return
        if not isinstance(src, dict):
            return
        if isinstance(src.get("origin"), dict):
            absorb(src.get("origin"))
        for field in REVIEW_ORIGIN_FIELDS:
            value = src.get(field)
            if value not in (None, ""):
                payload[field] = value
        if src.get("playlist_id") and not payload.get("source_playlist_id"):
            payload["source_playlist_id"] = src.get("playlist_id")
        if src.get("playlist_name") and not payload.get("source_playlist_name"):
            payload["source_playlist_name"] = src.get("playlist_name")
        if src.get("batch_job_id") and not payload.get("source_batch_id"):
            payload["source_batch_id"] = src.get("batch_job_id")
        workflow = src.get("workflow") or src.get("pipeline_source") or src.get("source")
        if workflow and not payload.get("created_by_workflow"):
            payload["created_by_workflow"] = workflow

    absorb(_path_origin_hint(folder_path))
    absorb(item)
    absorb(suggestion)
    absorb(evidence)
    absorb(origin)

    origin_type = _normalize_review_origin_type(payload.get("origin_type"))
    workflow_type = _normalize_review_origin_type(payload.get("created_by_workflow"))
    if origin_type == "unknown" and workflow_type != "unknown":
        origin_type = workflow_type
    if origin_type == "unknown":
        if payload.get("source_playlist_id") or payload.get("source_playlist_name"):
            origin_type = "playlist"
        elif payload.get("source_batch_id"):
            origin_type = "batch_import"
    payload["origin_type"] = origin_type
    if not payload.get("origin_label"):
        label = REVIEW_ORIGIN_LABELS.get(origin_type, "Unknown source")
        if origin_type == "playlist" and payload.get("source_playlist_name"):
            label = f"Playlist: {payload.get('source_playlist_name')}"
        payload["origin_label"] = label
    if not payload.get("origin_id"):
        payload["origin_id"] = (
            payload.get("source_playlist_id")
            or payload.get("source_playlist_name")
            or payload.get("source_batch_id")
            or ""
        )
    if folder_path and not payload.get("source_folder"):
        payload["source_folder"] = folder_path
    return {field: payload.get(field, "") for field in REVIEW_ORIGIN_FIELDS if payload.get(field, "") != ""}


def _compact_mb_candidate(candidate: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    c = candidate or {}
    score = c.get("_match_score") or {}
    return {
        "mb_albumid":    c.get("mb_albumid", ""),
        "album":         c.get("album", ""),
        "artist":        c.get("artist", ""),
        "year":          c.get("year", ""),
        "date":          c.get("date", ""),
        "country":       c.get("country", ""),
        "tracks":        int(c.get("tracks") or 0),
        "label":         c.get("label", ""),
        "labels":        c.get("labels", []) or [],
        "catalog_numbers": c.get("catalog_numbers", []) or [],
        "label_entries": c.get("label_entries", []) or [],
        "barcode":       c.get("barcode", ""),
        "formats":       c.get("formats", []) or [],
        "mediums":       c.get("mediums", []) or [],
        "format_summary": c.get("format_summary", ""),
        "is_vinyl":      bool(c.get("is_vinyl")),
        "status":        c.get("status", ""),
        "packaging":     c.get("packaging", ""),
        "cover_art":     c.get("cover_art"),
        "front_art":     c.get("front_art"),
        "cover_art_count": int(c.get("cover_art_count") or 0),
        "edition_count": int(c.get("edition_count") or 0),
        "edition_alternates": (c.get("edition_alternates") or [])[:5],
        "mb_score":      int(c.get("score") or score.get("mb_score") or 0),
        "match_total":   round(float(score.get("total") or 0), 3),
        "acoustid_hits": int(c.get("acoustid_release_hits") or score.get("acoustid_hits") or 0),
        "score_breakdown": score,
    }


def _leaked_db_paths_summary(rows: List[Dict[str, Any]],
                             *,
                             total_scanned: Optional[int] = None) -> Dict[str, Any]:
    safe = [r for r in rows if r.get("safe")]
    unsafe = [r for r in rows if not r.get("safe")]
    source_missing = [
        r for r in rows
        if not r.get("file_exists_at_db_path") and not r.get("file_exists_at_resolved")
    ]
    target_exists = [r for r in rows if r.get("file_exists_at_resolved")]
    return {
        "total_db_rows_scanned": int(total_scanned if total_scanned is not None else len(rows)),
        "leaked_paths_found": len(rows),
        "safe_repair_candidates": len(safe),
        "needs_review": len(unsafe),
        "source_missing": len(source_missing),
        "target_exists": len(target_exists),
        "skipped_unsafe": len(unsafe),
    }


def json_route_result(body: Any, status: int = 200):
    """Route return value for a request-free service result (ARCH-001).

    Keeps each route's historical return shape: a bare Response for 200, and
    (Response, status) otherwise, so direct in-process callers are unchanged."""
    response = jsonify(body)
    return response if status == 200 else (response, status)


# Stable (error_code, http_status, message) mapping shared by all three
# routes below -- keeps status codes/messages consistent without
# interpolating raw exception text (which may embed up to 200 characters of
# an unrecognized upstream error body; see composite_workflows._request()) into
# any browser-facing response.
_CONFIG_ERROR_RESPONSES = {
    "config_not_found":             (404, "Beets config.yaml not found on the engine."),
    "config_permission_denied":     (403, "Beets config.yaml is not accessible (permission denied)."),
    "config_backup_not_found":      (404, "No config.yaml backup found."),
    "config_empty":                 (400, "Empty config rejected."),
    "config_invalid_json":          (400, "Invalid config request body."),
    "config_invalid_content":       (400, "Config content must be a string."),
    "config_too_large":             (413, "Config content is too large."),
    "config_missing_revision":      (428, "Config revision is required before saving."),
    "config_revision_conflict":     (409, "Config was changed by another writer; reload before saving."),
    "config_invalid_yaml":          (400, "Invalid Beets configuration YAML."),
    "config_invalid_structure":     (400, "Invalid Beets configuration structure."),
    "config_beets_validation_failed": (400, "Beets rejected the candidate configuration."),
    "config_post_write_validation_failed": (500, "Beets rejected the committed configuration; the previous config was restored."),
    "config_read_failed":           (502, "Could not read config.yaml from the Beets engine."),
    "config_write_failed":          (502, "Could not save config.yaml on the Beets engine."),
    "config_revert_failed":         (502, "Could not revert config.yaml on the Beets engine."),
}


_CONFIG_ERROR_DEFAULT = (502, "Beets engine returned an unexpected error.")


def _config_error_response(exc: "BeetsError"):
    status, message = _CONFIG_ERROR_RESPONSES.get(exc.error_code, _CONFIG_ERROR_DEFAULT)
    return jsonify({"ok": False, "error": message, "code": exc.error_code or "config_engine_error"}), status
