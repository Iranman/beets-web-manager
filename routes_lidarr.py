"""Lidarr integration routes.

Registered after app.py initializes. Keeps Lidarr HTTP glue out of the large app
module while reusing the existing acquisition helpers and outbound URL policy.
"""
import json
import urllib.error
import urllib.parse
from typing import Any, Dict, List, Tuple

from flask import jsonify, request

from app import (  # noqa: E402
    LIDARR_KEY,
    LIDARR_URL,
    _acq_fetch_lidarr_wanted,
    _s,
    _ur,
    app,
)


def _lidarr_config_error() -> str:
    if not LIDARR_KEY:
        return "LIDARR_API_KEY not configured"
    if not LIDARR_URL:
        return "LIDARR_URL not configured"
    return ""


def _lidarr_headers() -> Dict[str, str]:
    return {"Accept": "application/json", "Content-Type": "application/json", "X-Api-Key": LIDARR_KEY}


def _lidarr_url(path: str, params: Dict[str, Any] | None = None) -> str:
    base = LIDARR_URL.rstrip("/")
    suffix = path if path.startswith("/") else f"/{path}"
    query = urllib.parse.urlencode(params or {}, doseq=True)
    return f"{base}{suffix}{'?' + query if query else ''}"


def _lidarr_request_json(path: str, *, params: Dict[str, Any] | None = None,
                         method: str = "GET", body: Dict[str, Any] | None = None) -> Any:
    error = _lidarr_config_error()
    if error:
        raise RuntimeError(error)
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = _ur.Request(_lidarr_url(path, params), data=data, headers=_lidarr_headers(), method=method)
    with _ur.urlopen(req, timeout=20) as resp:
        raw = resp.read()
    return json.loads(raw or b"{}")


def _http_error_message(exc: Exception) -> Tuple[str, int]:
    if isinstance(exc, urllib.error.HTTPError):
        return f"Lidarr returned HTTP {exc.code}", 502
    if isinstance(exc, RuntimeError):
        return str(exc), 503
    return "Could not reach Lidarr", 502


def _norm(value: str) -> str:
    return "".join(ch.lower() for ch in _s(value) if ch.isalnum())


def _artist_name(artist: Dict[str, Any]) -> str:
    return _s(artist.get("artistName") or artist.get("name") or artist.get("title") or "")


def _album_year(album: Dict[str, Any]) -> str:
    for key in ("releaseDate", "release_date", "firstReleaseDate"):
        value = _s(album.get(key) or "")
        if len(value) >= 4:
            return value[:4]
    year = album.get("year")
    return _s(year or "")


def _cover_url(album: Dict[str, Any]) -> str:
    for image in album.get("images") or []:
        if not isinstance(image, dict):
            continue
        if _s(image.get("coverType") or "").lower() in {"cover", "poster"}:
            return _s(image.get("remoteUrl") or image.get("url") or "")
    return ""


def _album_path(album: Dict[str, Any], artist_path: str) -> str:
    return _s(album.get("path") or album.get("folder") or album.get("albumFolder") or artist_path)


def _album_payload(album: Dict[str, Any], artist_path: str) -> Dict[str, Any]:
    stats = album.get("statistics") or {}
    track_count = int(stats.get("trackCount") or album.get("trackCount") or 0)
    track_file_count = int(stats.get("trackFileCount") or album.get("trackFileCount") or 0)
    percent = stats.get("percentOfTracks", album.get("percent", 0))
    try:
        percent_value = float(percent or 0)
    except (TypeError, ValueError):
        percent_value = 0.0
    disk_path = _album_path(album, artist_path)
    return {
        "lidarr_id": int(album.get("id") or 0),
        "title": _s(album.get("title") or album.get("albumTitle") or ""),
        "year": _album_year(album),
        "album_type": _s(album.get("albumType") or album.get("type") or ""),
        "monitored": bool(album.get("monitored", True)),
        "track_file_count": track_file_count,
        "track_count": track_count,
        "percent": round(max(0.0, min(100.0, percent_value)), 1),
        "mb_albumid": _s(album.get("foreignAlbumId") or album.get("mbAlbumId") or ""),
        "cover_url": _cover_url(album),
        "disk_path": disk_path,
        "aldir": disk_path,
    }


def _find_artist(artists: List[Dict[str, Any]], name: str) -> Dict[str, Any] | None:
    wanted = _norm(name)
    if not wanted:
        return None
    exact = [artist for artist in artists if _norm(_artist_name(artist)) == wanted]
    if exact:
        return exact[0]
    contains = [artist for artist in artists if wanted in _norm(_artist_name(artist))]
    return contains[0] if contains else None


@app.get("/api/wanted/lidarr")
def wanted_lidarr():
    rows, error = _acq_fetch_lidarr_wanted()
    if error:
        status = 503 if "not configured" in error.lower() else 502
        return jsonify({"ok": False, "error": error, "missing": [], "total": 0}), status
    return jsonify({"ok": True, "missing": rows, "total": len(rows)})


@app.get("/api/lidarr/artist-albums-by-name")
def lidarr_artist_albums_by_name():
    name = _s(request.args.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name is required", "albums": []}), 400
    try:
        artists = _lidarr_request_json("/api/v1/artist")
        if not isinstance(artists, list):
            artists = []
        artist = _find_artist(artists, name)
        if not artist:
            return jsonify({"ok": True, "found": False, "albums": [], "lidarr_artist": "", "artist_path": ""})
        artist_path = _s(artist.get("path") or "")
        albums = artist.get("albums") or []
        if not albums and artist.get("id"):
            album_data = _lidarr_request_json("/api/v1/album", params={"artistId": artist.get("id")})
            albums = album_data if isinstance(album_data, list) else []
        payload = [_album_payload(album, artist_path) for album in albums if isinstance(album, dict)]
        return jsonify({
            "ok": True,
            "found": True,
            "albums": payload,
            "lidarr_artist": _artist_name(artist),
            "artist_path": artist_path,
        })
    except Exception as exc:  # network/config boundary; response is redacted
        message, status = _http_error_message(exc)
        return jsonify({"ok": False, "error": message, "albums": []}), status


@app.post("/api/lidarr/command")
def lidarr_command():
    payload = request.get_json(silent=True) or {}
    name = _s(payload.get("name") or "")
    if name != "AlbumSearch":
        return jsonify({"ok": False, "error": "unsupported Lidarr command"}), 400
    album_ids = []
    for raw in payload.get("albumIds") or []:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            album_ids.append(value)
    if not album_ids:
        return jsonify({"ok": False, "error": "albumIds must include at least one positive ID"}), 400
    try:
        result = _lidarr_request_json("/api/v1/command", method="POST", body={"name": name, "albumIds": album_ids})
        return jsonify({
            "ok": True,
            "command_id": result.get("id") if isinstance(result, dict) else None,
            "status": result.get("status") if isinstance(result, dict) else "queued",
        })
    except Exception as exc:  # network/config boundary; response is redacted
        message, status = _http_error_message(exc)
        return jsonify({"ok": False, "error": message}), status
