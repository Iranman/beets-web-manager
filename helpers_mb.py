"""MusicBrainz / AcoustID API helpers — no app.py dependencies."""
import json, os, re, shutil, subprocess, threading, time
import urllib.error, urllib.parse, urllib.request
from backend.security import install_secure_urllib
install_secure_urllib()
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.title_normalize import restore_time_colon_title

_ur = urllib.request
_up = urllib.parse
_ACOUSTID_LOOKUP_LOCK = threading.Lock()
_ACOUSTID_NEXT_LOOKUP_AT = 0.0
try:
    _ACOUSTID_MIN_INTERVAL_SECONDS = max(0.0, float(os.environ.get("ACOUSTID_MIN_INTERVAL_SECONDS", "0.35") or "0.35"))
except Exception:
    _ACOUSTID_MIN_INTERVAL_SECONDS = 0.35

_MB_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)

_JUNK_TITLE_RE = re.compile(
    r'\s*[\(\[【][^\)\]】]*(official|video|audio|lyric|hd|hq|mv|music|explicit|clean|remaster|live)[^\)\]】]*[\)\]】]',
    re.I
)


def _mb_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace").strip()
        except Exception:
            return ""
    return str(value).strip()


def _mb_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except Exception:
        return default


def _mb_credit_text(credits: Any) -> str:
    parts: List[str] = []
    for credit in credits or []:
        if not isinstance(credit, dict):
            continue
        name = _mb_text(credit.get("name") or (credit.get("artist") or {}).get("name"))
        if not name:
            continue
        join = _mb_text(credit.get("joinphrase"))
        parts.append(f"{name}{join}")
    return "".join(parts).strip()


def _mb_first_artist_id(credits: Any) -> str:
    for credit in credits or []:
        if isinstance(credit, dict):
            artist_id = _mb_text((credit.get("artist") or {}).get("id"))
            if artist_id:
                return artist_id
    return ""


def _mb_release_track_count(release: Dict[str, Any]) -> int:
    total = 0
    for medium in release.get("media") or []:
        total += _mb_int(medium.get("track-count"), 0)
    return total


def _mb_release_track_for_recording(release: Dict[str, Any], mb_trackid: str) -> tuple:
    target = _mb_text(mb_trackid).lower()
    for medium in release.get("media") or []:
        for track in medium.get("tracks") or []:
            recording = track.get("recording") or {}
            if _mb_text(recording.get("id")).lower() == target:
                return medium, track
    return {}, {}


def _mb_first_label(release: Dict[str, Any]) -> str:
    for entry in release.get("label-info") or []:
        if not isinstance(entry, dict):
            continue
        label = _mb_text((entry.get("label") or {}).get("name"))
        if label:
            return label
    return ""


def _compact_mb_recording_release(release: Dict[str, Any], mb_trackid: str) -> Dict[str, Any]:
    release_id = _mb_text(release.get("id"))
    release_group = release.get("release-group") or {}
    release_group_id = _mb_text(release_group.get("id"))
    medium, track = _mb_release_track_for_recording(release, mb_trackid)
    track_recording = track.get("recording") or {}
    release_artist = _mb_credit_text(release.get("artist-credit"))
    date = _mb_text(release.get("date"))
    medium_position = _mb_int(medium.get("position"), 0)
    track_position = _mb_int(track.get("position"), 0)
    track_number = _mb_text(track.get("number") or track.get("position"))
    medium_format = _mb_text(medium.get("format"))
    duration_ms = _mb_int(track.get("length") or track_recording.get("length"), 0)
    track_count = _mb_release_track_count(release)
    return {
        "mb_albumid": release_id,
        "mb_url": f"https://musicbrainz.org/release/{release_id}" if release_id else "",
        "album": _mb_text(release.get("title")),
        "artist": release_artist,
        "date": date,
        "year": date[:4] if date[:4].isdigit() else "",
        "country": _mb_text(release.get("country")),
        "status": _mb_text(release.get("status")),
        "label": _mb_first_label(release),
        "mb_releasegroupid": release_group_id,
        "mb_releasegroupurl": f"https://musicbrainz.org/release-group/{release_group_id}" if release_group_id else "",
        "release_group_primary_type": _mb_text(release_group.get("primary-type")),
        "release_group_secondary_types": release_group.get("secondary-types") or [],
        "disc": str(medium_position) if medium_position else "",
        "medium_position": medium_position or None,
        "medium_format": medium_format,
        "media_format": medium_format,
        "track": track_number,
        "track_number": track_number,
        "track_position": track_position or None,
        "tracktotal": str(_mb_int(medium.get("track-count"), 0)) if medium else "",
        "tracks": track_count,
        "track_count": track_count,
        "duration_ms": duration_ms or None,
    }


def _fetch_mb_recording_details(mb_trackid: str, preferred_albumid: str = "") -> dict:
    """Fetch full recording details from MusicBrainz.

    The response keeps all linked releases so callers can compare a recording
    against local album/year tags instead of treating the first release as the
    only valid album context.
    """
    url = (f"https://musicbrainz.org/ws/2/recording/{mb_trackid}"
           "?inc=releases+release-groups+artist-credits+media+genres+label-info&fmt=json")
    req = _ur.Request(url, headers={"User-Agent": "BeetsWebControl/1.0"})
    try:
        with _ur.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
    except Exception:
        return {}

    result: dict = {
        "recording_id": _mb_text(data.get("id") or mb_trackid),
        "recording_title": _mb_text(data.get("title")),
        "recording_first_release_date": _mb_text(data.get("first-release-date")),
        "recording_length_ms": _mb_int(data.get("length"), 0) or None,
    }

    credits = data.get("artist-credit", [])
    artist_str = _mb_credit_text(credits)
    if artist_str:
        result["artist"] = artist_str
        result["recording_artist"] = artist_str
    artist_id = _mb_first_artist_id(credits)
    if artist_id:
        result["mb_artistid"] = artist_id

    genres = sorted(data.get("genres", []), key=lambda g: g.get("count", 0), reverse=True)
    if genres:
        result["genre"] = _mb_text(genres[0].get("name"))

    releases = data.get("releases", [])
    linked_releases = [
        _compact_mb_recording_release(r, mb_trackid)
        for r in releases
        if isinstance(r, dict)
    ]
    result["linked_releases"] = linked_releases
    result["same_recording_release_count"] = len(linked_releases)
    if not linked_releases:
        return result

    _CR = {"US": 0, "USA": 0, "XW": 1, "WORLDWIDE": 1, "GB": 2, "CA": 3, "AU": 4}
    preferred = _mb_text(preferred_albumid).lower()
    best = next((r for r in linked_releases if preferred and _mb_text(r.get("mb_albumid")).lower() == preferred), None)
    if not best:
        rec_title_norm = re.sub(r"[^a-z0-9]+", " ", _mb_text(data.get("title")).casefold()).strip()
        rec_artist_norm = re.sub(r"[^a-z0-9]+", " ", artist_str.casefold()).strip()

        def _albumish_penalty(release: Dict[str, Any]) -> int:
            title_norm = re.sub(r"[^a-z0-9]+", " ", _mb_text(release.get("album")).casefold()).strip()
            track_count = _mb_int(release.get("track_count") or release.get("tracks"), 0)
            primary_type = _mb_text(release.get("release_group_primary_type")).casefold()
            single_like = primary_type == "single" or (rec_title_norm and title_norm == rec_title_norm and 0 < track_count <= 2)
            if single_like:
                return 3
            if primary_type == "album":
                return 0
            if primary_type == "ep":
                return 1
            return 2

        def _release_artist_penalty(release: Dict[str, Any]) -> int:
            rel_artist_norm = re.sub(r"[^a-z0-9]+", " ", _mb_text(release.get("artist")).casefold()).strip()
            if not rec_artist_norm or not rel_artist_norm:
                return 1
            if rel_artist_norm == rec_artist_norm or rel_artist_norm in rec_artist_norm or rec_artist_norm in rel_artist_norm:
                return 0
            if rel_artist_norm in {"various artists", "various"}:
                return 3
            return 2

        def _rrank(release: Dict[str, Any]):
            cc = _CR.get(_mb_text(release.get("country")).upper(), 99)
            yr = _mb_text(release.get("year") or "9999")[:4] or "9999"
            return (_albumish_penalty(release), _release_artist_penalty(release), cc, yr)

        best = min(linked_releases, key=_rrank)

    result["selected_release"] = best
    result["mb_albumid"] = best.get("mb_albumid", "")
    result["album"] = best.get("album", "")
    result["albumartist"] = best.get("artist", "")
    result["year"] = best.get("year", "")
    result["date"] = best.get("date", "")
    result["country"] = best.get("country", "")
    result["label"] = best.get("label", "") or result.get("label", "")
    result["mb_releasegroupid"] = best.get("mb_releasegroupid", "")
    result["mb_releasegroupurl"] = best.get("mb_releasegroupurl", "")
    result["release_group_primary_type"] = best.get("release_group_primary_type", "")
    result["medium_format"] = best.get("medium_format", "")
    result["media_format"] = best.get("media_format", "")
    result["track"] = best.get("track", "")
    result["tracktotal"] = best.get("tracktotal", "")
    result["disc"] = best.get("disc", "")
    result["disctotal"] = str(max(1, len({r.get("medium_position") for r in linked_releases if r.get("medium_position")}))) if linked_releases else ""
    return result


def _mb_recording_search(title: str, artist: str, limit: int = 8):
    """Query MusicBrainz for recording candidates. Returns list of candidate dicts."""
    parts = []
    if title:  parts.append(f'recording:"{title}"')
    if artist: parts.append(f'artist:"{artist}"')
    if not parts:
        return []
    params = _up.urlencode({"query": " AND ".join(parts), "limit": limit, "fmt": "json"})
    url = f"https://musicbrainz.org/ws/2/recording?{params}"
    req = _ur.Request(url, headers={"User-Agent": "BeetsWebControl/1.0 (beets-webcontrol)"})
    try:
        with _ur.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        out = []
        for rec in data.get("recordings", []):
            mb_id   = rec.get("id", "")
            score   = int(rec.get("score", 0))
            t_title = rec.get("title", "")
            ac      = rec.get("artist-credit", [])
            artists = " / ".join(x.get("artist", {}).get("name", "")
                                 for x in ac if isinstance(x, dict))
            releases = rec.get("releases", [])
            rel = releases[0] if releases else {}
            rel_title = rel.get("title", "")
            rel_year  = (rel.get("date") or "")[:4]
            dur_ms = rec.get("length") or 0
            dur_s  = dur_ms // 1000
            dur    = f"{dur_s//60}:{dur_s%60:02d}" if dur_s else ""
            out.append({
                "score":      score,
                "mb_trackid": mb_id,
                "mb_url":     f"https://musicbrainz.org/recording/{mb_id}",
                "title":      t_title,
                "artist":     artists,
                "album":      rel_title,
                "mb_albumid": rel.get("id", ""),
                "mb_albumids": [r.get("id", "") for r in releases if r.get("id")],
                "year":       rel_year,
                "duration":   dur,
            })
        return out
    except Exception:
        return []


def _mb_release_search(album: str, artist: str, limit: int = 8,
                       year: str = "", track_count: int = 0,
                       log: Optional[list] = None,
                       artist_mbid: str = ""):
    """Query MusicBrainz for release candidates.

    Returns up to `limit` entries — de-duplicated across pressings of the same
    album.  When choosing among pressings, priority is:
      1. Non-vinyl before vinyl (CD / Digital Media preferred)
      2. US release first, Worldwide/XW fallback, then other countries (GB, CA, AU, …)
      3. Year match (if caller supplies a guessed year)
      4. Track-count match (if caller supplies expected track count)
      5. MusicBrainz relevance score

    Pass artist_mbid (UUID) when known — uses arid: scoped search which is more
    reliable than artist name search for non-ASCII or unusual artist names.
    """
    _MB_UUID_RE = re.compile(
        r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I
    )
    parts = []
    if album:  parts.append(f'release:"{album}"')
    if artist_mbid and _MB_UUID_RE.match(artist_mbid.strip()):
        parts.append(f'arid:{artist_mbid.strip().lower()}')
    elif artist:
        parts.append(f'artist:"{artist}"')
    if not parts:
        return []
    fetch_limit = min(limit * 8, 100)
    params = _up.urlencode({"query": " AND ".join(parts),
                            "limit": fetch_limit, "fmt": "json"})
    url = f"https://musicbrainz.org/ws/2/release?{params}"
    req = _ur.Request(url, headers={"User-Agent": "BeetsWebControl/1.0 (beets-webcontrol)"})
    data: Dict[str, Any] = {}
    transient_codes = {429, 500, 502, 503, 504}
    for attempt in range(1, 4):
        try:
            with _ur.urlopen(req, timeout=25) as r:
                data = json.loads(r.read())
            break
        except Exception as ex:
            code = getattr(ex, "code", None)
            reason = str(getattr(ex, "reason", "") or "").lower()
            transient = code in transient_codes or "timed out" in reason or "temporarily" in reason
            if transient and attempt < 3:
                if log is not None:
                    log.append(
                        f"  MB release search transient error ({code or reason or ex}); "
                        f"retrying {attempt + 1}/3"
                    )
                time.sleep(1.5 * attempt)
                continue
            if log is not None:
                log.append(f"  WARN: MusicBrainz release search failed: {ex}")
            return []

    _COUNTRY_RANK = {"US": 0, "USA": 0, "XW": 1, "WORLDWIDE": 1, "GB": 2, "CA": 3, "AU": 4}

    def _country_rank(c: str) -> int:
        return _COUNTRY_RANK.get((c or "").upper(), 99)

    _VINYL_KEYWORDS = {"vinyl", '7"', '10"', '12"', 'shellac', '78'}

    def _is_vinyl(media_list) -> bool:
        for m in media_list:
            fmt = (m.get("format") or "").lower()
            if any(k in fmt for k in _VINYL_KEYWORDS):
                return True
        return False

    def _label_details(label_info) -> tuple[str, List[str], List[str], List[Dict[str, str]]]:
        labels: List[str] = []
        catalog_numbers: List[str] = []
        entries: List[Dict[str, str]] = []
        if isinstance(label_info, list):
            for li in label_info:
                if not isinstance(li, dict):
                    continue
                label_name = ((li.get("label") or {}).get("name") or "").strip()
                catalog = (li.get("catalog-number") or "").strip()
                if label_name:
                    labels.append(label_name)
                if catalog:
                    catalog_numbers.append(catalog)
                if label_name or catalog:
                    entries.append({"label": label_name, "catalog_number": catalog})
        primary = labels[0] if labels else ""
        return primary, labels, catalog_numbers, entries

    def _media_summary(media_list) -> tuple[int, List[str], List[Dict[str, Any]], str]:
        total = 0
        formats: List[str] = []
        mediums: List[Dict[str, Any]] = []
        for medium in media_list or []:
            if not isinstance(medium, dict):
                continue
            fmt = (medium.get("format") or "").strip()
            count = int(medium.get("track-count") or 0)
            total += count
            if fmt and fmt not in formats:
                formats.append(fmt)
            mediums.append({
                "position": int(medium.get("position") or len(mediums) + 1),
                "format": fmt,
                "tracks": count,
            })
        summary = " + ".join(
            f"{m['format'] or 'Medium'} ({m['tracks']})" for m in mediums
        )
        return total, formats, mediums, summary

    def _edition_summary(rel: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "score": rel.get("score", 0),
            "mb_albumid": rel.get("mb_albumid", ""),
            "mb_url": rel.get("mb_url", ""),
            "mb_releasegroupid": rel.get("mb_releasegroupid", ""),
            "mb_releasegroupurl": rel.get("mb_releasegroupurl", ""),
            "release_group_primary_type": rel.get("release_group_primary_type", ""),
            "album": rel.get("album", ""),
            "artist": rel.get("artist", ""),
            "year": rel.get("year", ""),
            "date": rel.get("date", ""),
            "country": rel.get("country", ""),
            "label": rel.get("label", ""),
            "labels": rel.get("labels", []) or [],
            "catalog_numbers": rel.get("catalog_numbers", []) or [],
            "label_entries": rel.get("label_entries", []) or [],
            "barcode": rel.get("barcode", ""),
            "tracks": rel.get("tracks", 0),
            "formats": rel.get("formats", []) or [],
            "mediums": rel.get("mediums", []) or [],
            "format_summary": rel.get("format_summary", ""),
            "is_vinyl": bool(rel.get("is_vinyl")),
            "status": rel.get("status", ""),
            "packaging": rel.get("packaging", ""),
            "cover_art": rel.get("cover_art"),
            "front_art": rel.get("front_art"),
            "cover_art_count": int(rel.get("cover_art_count") or 0),
        }

    all_releases = []
    for rel in data.get("releases", []):
        mb_id     = rel.get("id", "")
        score     = int(rel.get("score", 0))
        r_title   = rel.get("title", "")
        full_date = rel.get("date") or ""
        r_date    = full_date[:4]
        r_country = rel.get("country", "")
        li = rel.get("label-info", [])
        r_label, labels, catalog_numbers, label_entries = _label_details(li)
        ac = rel.get("artist-credit", [])
        artists = " / ".join(x.get("artist", {}).get("name", "")
                             for x in ac if isinstance(x, dict))
        media    = rel.get("media", [])
        r_tracks, r_formats, mediums, format_summary = _media_summary(media)
        r_vinyl   = _is_vinyl(media)
        caa = rel.get("cover-art-archive")
        if isinstance(caa, dict):
            cover_art = bool(caa.get("artwork"))
            front_art = bool(caa.get("front"))
            cover_art_count = int(caa.get("count") or 0)
        else:
            cover_art = None
            front_art = None
            cover_art_count = 0
        rg = rel.get("release-group") or {}
        rg_id = str(rg.get("id") or "").strip().lower()
        rg_primary_type = str(rg.get("primary-type") or "").strip()
        all_releases.append({
            "score":      score,
            "mb_albumid": mb_id,
            "mb_url":     f"https://musicbrainz.org/release/{mb_id}",
            "mb_releasegroupid": rg_id,
            "mb_releasegroupurl": f"https://musicbrainz.org/release-group/{rg_id}" if rg_id else "",
            "release_group_primary_type": rg_primary_type,
            "album":      r_title,
            "artist":     artists,
            "year":       r_date,
            "date":       full_date,
            "label":      r_label,
            "labels":     labels,
            "catalog_numbers": catalog_numbers,
            "label_entries": label_entries,
            "country":    r_country,
            "barcode":    rel.get("barcode", "") or "",
            "tracks":     r_tracks,
            "formats":    r_formats,
            "mediums":    mediums,
            "format_summary": format_summary,
            "is_vinyl":   r_vinyl,
            "status":     rel.get("status", "") or "",
            "packaging":  rel.get("packaging", "") or "",
            "cover_art":  cover_art,
            "front_art":  front_art,
            "cover_art_count": cover_art_count,
        })

    def _rank(rel) -> tuple:
        vinyl_pen    = 10 if rel["is_vinyl"] else 0
        country_pts  = _country_rank(rel["country"])
        year_miss    = 0 if (not year or rel["year"][:4] == str(year)[:4]) else 2
        track_delta  = abs(int(rel["tracks"] or 0) - int(track_count or 0)) if track_count else 0
        track_miss   = 0 if (not track_count or rel["tracks"] == track_count) else 1
        score_pts    = -rel["score"]
        if track_count:
            return (track_miss, track_delta, vinyl_pen, country_pts, year_miss, score_pts)
        return (vinyl_pen, country_pts, year_miss, track_miss, score_pts)

    groups: dict = OrderedDict()
    for rel in all_releases:
        rg_key = rel.get("mb_releasegroupid", "")
        key = rg_key if rg_key else (rel["artist"].lower().strip(), rel["album"].lower().strip())
        groups.setdefault(key, []).append(rel)

    result = []
    for releases in groups.values():
        ranked = sorted(releases, key=_rank)
        best = dict(ranked[0])
        best["edition_count"] = len(ranked)
        best["edition_alternates"] = [
            _edition_summary(rel) for rel in ranked[1:8]
        ]
        result.append(best)

    return result[:limit]


def _fetch_mb_release_candidate(mb_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single MB release by UUID and return a candidate dict.

    Returns the same shape as _mb_release_search entries so callers can mix them.
    Returns None on any failure.
    """
    mb_id = (mb_id or "").strip().lower()
    if not _MB_UUID_RE.match(mb_id):
        return None
    try:
        url = (f"https://musicbrainz.org/ws/2/release/{mb_id}"
               "?inc=artist-credits+media+label-info+release-groups&fmt=json")
        req = _ur.Request(url, headers={"User-Agent": "BeetsWebControl/1.0"})
        with _ur.urlopen(req, timeout=15) as r:
            rel = json.loads(r.read())
    except Exception:
        return None

    artists = " / ".join(
        str((ac.get("artist") or {}).get("name") or ac.get("name") or "")
        for ac in (rel.get("artist-credit") or [])
        if isinstance(ac, dict)
    ).strip()

    labels: List[str] = []
    catalog_numbers: List[str] = []
    label_entries: List[Dict[str, str]] = []
    for li in (rel.get("label-info") or []):
        if not isinstance(li, dict):
            continue
        ln = str((li.get("label") or {}).get("name") or "").strip()
        cat = str(li.get("catalog-number") or "").strip()
        if ln:
            labels.append(ln)
        if cat:
            catalog_numbers.append(cat)
        if ln or cat:
            label_entries.append({"label": ln, "catalog_number": cat})

    media = rel.get("media") or []
    total_tracks = 0
    formats: List[str] = []
    mediums: List[Dict[str, Any]] = []
    _vinyl_kw = {"vinyl", '7"', '10"', '12"', "shellac", "78"}
    for medium in media:
        if not isinstance(medium, dict):
            continue
        fmt = str(medium.get("format") or "").strip()
        cnt = int(medium.get("track-count") or 0)
        total_tracks += cnt
        if fmt and fmt not in formats:
            formats.append(fmt)
        mediums.append({"position": int(medium.get("position") or len(mediums) + 1),
                        "format": fmt, "tracks": cnt})
    is_vinyl = any(any(k in (m.get("format") or "").lower() for k in _vinyl_kw) for m in mediums)
    format_summary = " + ".join(
        f"{m['format'] or 'Medium'} ({m['tracks']})" for m in mediums
    )

    full_date = str(rel.get("date") or "")
    caa = rel.get("cover-art-archive")
    if isinstance(caa, dict):
        cover_art = bool(caa.get("artwork"))
        front_art = bool(caa.get("front"))
        cover_art_count = int(caa.get("count") or 0)
    else:
        cover_art = None
        front_art = None
        cover_art_count = 0

    rg = rel.get("release-group") or {}
    rg_id = str(rg.get("id") or "").strip().lower()
    return {
        "score":             80,
        "mb_albumid":        mb_id,
        "mb_url":            f"https://musicbrainz.org/release/{mb_id}",
        "mb_releasegroupid": rg_id,
        "mb_releasegroupurl": f"https://musicbrainz.org/release-group/{rg_id}" if rg_id else "",
        "release_group_primary_type": str(rg.get("primary-type") or "").strip(),
        "album":             str(rel.get("title") or "").strip(),
        "artist":            artists,
        "year":              full_date[:4],
        "date":              full_date,
        "label":             labels[0] if labels else "",
        "labels":            labels,
        "catalog_numbers":   catalog_numbers,
        "label_entries":     label_entries,
        "country":           str(rel.get("country") or "").strip(),
        "barcode":           str(rel.get("barcode") or "").strip(),
        "tracks":            total_tracks,
        "formats":           formats,
        "mediums":           mediums,
        "format_summary":    format_summary,
        "is_vinyl":          is_vinyl,
        "status":            str(rel.get("status") or "").strip(),
        "packaging":         str(rel.get("packaging") or "").strip(),
        "cover_art":         cover_art,
        "front_art":         front_art,
        "cover_art_count":   cover_art_count,
        "edition_count":     1,
        "edition_alternates": [],
    }


def _clean_for_mb(title: str, artist: str):
    """Strip YouTube-style junk and split 'Artist - Title' when artist is missing."""
    if not artist and " - " in title:
        parts = title.split(" - ", 1)
        artist, title = parts[0].strip(), parts[1].strip()
    title = restore_time_colon_title(title)
    title = _JUNK_TITLE_RE.sub("", title).strip()
    title = re.sub(r'\s+(ft\.|feat\.)\s+.+$', '', title, flags=re.I).strip()
    return title, artist


def _acoustid_lookup(file_path: str) -> List[Dict[str, Any]]:
    """Run fpcalc + AcoustID lookup. Returns list of MB recording candidate dicts."""
    fpcalc = shutil.which("fpcalc") or "/usr/bin/fpcalc"
    if not Path(fpcalc).exists():
        return []
    try:
        r = subprocess.run([fpcalc, "-json", file_path], capture_output=True, text=True, timeout=30)
        if r.returncode != 0 or not r.stdout.strip():
            return []
        fp_data = json.loads(r.stdout)
        duration = int(fp_data.get("duration") or 0)
        fingerprint = (fp_data.get("fingerprint") or "").strip()
        if not fingerprint or duration < 5:
            return []
    except Exception:
        return []
    aid_key = os.environ.get("ACOUSTID_API_KEY") or "8XaBELgH"  # env var or test fallback
    params = _up.urlencode({
        "client":      aid_key,
        "meta":        "recordings releases releasegroups",
        "duration":    duration,
        "fingerprint": fingerprint,
        "format":      "json",
    })
    data = {}
    req = _ur.Request(
        f"https://api.acoustid.org/v2/lookup?{params}",
        headers={"User-Agent": "BeetsWebControl/1.0"}
    )
    for attempt in range(2):
        try:
            global _ACOUSTID_NEXT_LOOKUP_AT
            with _ACOUSTID_LOOKUP_LOCK:
                now = time.monotonic()
                if _ACOUSTID_NEXT_LOOKUP_AT > now:
                    time.sleep(_ACOUSTID_NEXT_LOOKUP_AT - now)
                _ACOUSTID_NEXT_LOOKUP_AT = time.monotonic() + _ACOUSTID_MIN_INTERVAL_SECONDS
            with _ur.urlopen(req, timeout=15) as r2:
                data = json.loads(r2.read())
            break
        except Exception:
            if attempt >= 1:
                return []
            time.sleep(1.0)
    if data.get("status") != "ok":
        return []
    out = []
    seen_mbids: set = set()
    for result in (data.get("results") or [])[:5]:
        confidence = int(round((result.get("score") or 0) * 100))
        acoustid_id = str(result.get("id") or "")
        for rec in (result.get("recordings") or [])[:3]:
            mb_id = rec.get("id", "")
            if not mb_id or mb_id in seen_mbids:
                continue
            seen_mbids.add(mb_id)
            t_title  = rec.get("title", "")
            artists  = " / ".join(a.get("name", "") for a in (rec.get("artists") or []))
            releases = rec.get("releases") or []
            rel = releases[0] if releases else {}
            rel_title = rel.get("title", "")
            rel_year  = (rel.get("date", {}).get("year") if isinstance(rel.get("date"), dict) else "")
            mb_albumids = [r.get("id", "") for r in releases if r.get("id")]
            releasegroups = rec.get("releasegroups") or rec.get("release-groups") or []
            rg = releasegroups[0] if releasegroups else {}
            rg_id = rg.get("id", "") if isinstance(rg, dict) else ""
            rg_title = rg.get("title", "") if isinstance(rg, dict) else ""
            if not rg_id:
                for rel_item in releases:
                    rel_rg = (
                        rel_item.get("releasegroup")
                        or rel_item.get("release-group")
                        or rel_item.get("release_group")
                        or {}
                    )
                    if isinstance(rel_rg, dict) and rel_rg.get("id"):
                        rg_id = rel_rg.get("id", "")
                        rg_title = rel_rg.get("title", "") or rg_title
                        break
            out.append({
                "score":       confidence,
                "acoustid_id": acoustid_id,
                "mb_trackid":  mb_id,
                "mb_url":      f"https://musicbrainz.org/recording/{mb_id}",
                "title":       t_title,
                "artist":      artists,
                "album":       rel_title or rg_title,
                "year":        str(rel_year) if rel_year else "",
                "duration":    "",
                "source":      "acoustid",
                "mb_albumids": mb_albumids,
                "mb_releasegroupid": rg_id,
                "release_group": rg_title,
            })
    return out


def _resolve_release_group_to_release(rg_mbid: str, log: list,
                                      year: str = "", track_count: int = 0) -> str:
    """Resolve a MusicBrainz release-group UUID to a concrete release UUID."""
    rg_mbid = (rg_mbid or "").strip().lower()
    if not _MB_UUID_RE.match(rg_mbid):
        return ""
    try:
        api_url = f"https://musicbrainz.org/ws/2/release-group/{rg_mbid}?inc=releases&fmt=json"
        req = _ur.Request(api_url, headers={"User-Agent": "BeetsWebControl/1.0"})
        with _ur.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        releases = data.get("releases", []) or []
    except Exception as ex:
        log.append(f"  WARN: release-group lookup failed for {rg_mbid}: {ex}")
        return ""
    if not releases:
        log.append(f"  WARN: release-group {rg_mbid} has no releases")
        return ""

    country_rank = {"US": 0, "XW": 1, "GB": 2, "CA": 3, "AU": 4}

    def _rank(rel):
        status_miss = 0 if rel.get("status") == "Official" else 1
        country = country_rank.get((rel.get("country") or "").upper(), 9)
        date = (rel.get("date") or "")[:4]
        year_miss = 0 if (not year or date == str(year)[:4]) else 2
        media = rel.get("media") or []
        tracks = sum(int(m.get("track-count") or 0) for m in media)
        if track_count:
            track_miss = 0 if tracks == track_count else 1
            track_delta = abs(tracks - track_count) if tracks else 999
            return (status_miss, track_miss, track_delta, country, year_miss, date or "9999")
        return (status_miss, country, year_miss, date or "9999")

    chosen = sorted(releases, key=_rank)[0]
    rel_id = (chosen.get("id") or "").lower()
    if rel_id:
        log.append(f"  Resolved release-group {rg_mbid} → release {rel_id}"
                   f" ({chosen.get('title','?')} {chosen.get('date','')})")
    return rel_id


def _mb_release_group_candidates(rg_mbid: str, log: Optional[list] = None) -> List[Dict[str, Any]]:
    """List candidate releases under a MusicBrainz release-group, for manual selection."""
    rg_mbid = (rg_mbid or "").strip().lower()
    if not _MB_UUID_RE.match(rg_mbid):
        return []
    try:
        api_url = f"https://musicbrainz.org/ws/2/release-group/{rg_mbid}?inc=releases+media&fmt=json"
        req = _ur.Request(api_url, headers={"User-Agent": "BeetsWebControl/1.0"})
        with _ur.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        releases = data.get("releases", []) or []
    except Exception as ex:
        if log is not None:
            log.append(f"  WARN: release-group lookup failed for {rg_mbid}: {ex}")
        return []

    out: List[Dict[str, Any]] = []
    for rel in releases:
        media = rel.get("media") or []
        tracks = sum(int(m.get("track-count") or 0) for m in media)
        out.append({
            "mb_albumid": (rel.get("id") or "").lower(),
            "title": rel.get("title") or "",
            "date": rel.get("date") or "",
            "country": rel.get("country") or "",
            "status": rel.get("status") or "",
            "track_count": tracks,
        })
    out.sort(key=lambda r: (0 if r["status"] == "Official" else 1, r["date"] or "9999"))
    return out


def _resolve_mb_release_id(mb_input: str, log: list) -> str:
    """Extract a MusicBrainz release UUID from a URL or raw UUID.
    If the input is a release-group URL/UUID, auto-resolves to the first
    Official release in that group via the MB API."""
    UUID_RE = re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
    m = UUID_RE.search(mb_input)
    if not m:
        return ""
    mb_uuid = m.group(0).lower()

    if "release-group" in mb_input.lower():
        try:
            api_url = (f"https://musicbrainz.org/ws/2/release-group/{mb_uuid}"
                       f"?inc=releases&fmt=json")
            req = urllib.request.Request(api_url, headers={
                "User-Agent": "BeetsWebControl/1.0 (beets-web@localhost)"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            releases = data.get("releases", [])
            official = [r for r in releases if r.get("status") == "Official"]
            chosen = (official or releases or [None])[0]
            if chosen:
                log.append(f"  Resolved release-group {mb_uuid} → release {chosen['id']}"
                           f" ({chosen.get('title','?')} {chosen.get('date','')})")
                return chosen["id"].lower()
            log.append(f"  WARN: release-group {mb_uuid} has no releases")
        except Exception as ex:
            log.append(f"  WARN: release-group lookup failed ({ex}), using raw UUID")
    return mb_uuid
