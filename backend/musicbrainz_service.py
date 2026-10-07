"""MusicBrainz / Discogs provider: lookups, release tracklists and caching (ARCH-001).
"""

from __future__ import annotations

import backend.provider_boundary as provider_boundary
import json, mimetypes, re, time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from backend.app_runtime import AUDIO_EXT, DOWNLOADS_ROOT, MUSIC_ROOT, RELEASE_ART_CACHE_DIR, _MALFORMED_RELEASE_GROUP_STAMP_RE, _MB_UUID_RE, _extract_mb_uuid, _is_valid_mb_uuid, _s, _up, _ur
from backend.app_runtime import _normalize_name, _path_is_under, _split_mbid_values
from helpers_mb import _fetch_mb_recording_details, _fetch_mb_release_candidate, _mb_unavailable
from backend.beets_adapter import lib, BeetsUnavailableError
from backend.security import OutboundPolicyError
import backend.composite_workflows as composite_workflows
from backend.acoustid_service import _album_track_norm, _read_file_media_tags
from backend.artwork_service import DISCOGS_TOKEN, _ARTIST_IMAGE_MAX_BYTES, _RELEASE_ART_MBID_RE, _artist_image_ext, _IMAGE_FETCH_HEADERS, _artist_name_key, _release_art_cache_info, _release_art_save_miss
from backend.slskd_service import _slskd_title_guess_from_name
from backend.matching_service import _album_key, _album_title_match, _album_track_title_variants, _fetch_mb_release_tracklist, _normalize_album

# ── ARCH-001 extracted code ──


def _mb_release_group_for_release(release_id: str) -> str:
    """Authoritative Release Group of a Release, from MusicBrainz ("" if unknown)."""
    return _s((_fetch_mb_release_candidate(release_id) or {}).get("mb_releasegroupid") or "").strip().lower()


def _discogs_track_search(title: str, artist: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Search Discogs for track/release candidates. Returns list of candidate dicts."""

    if not DISCOGS_TOKEN or not (title or artist):
        return []
    params = _up.urlencode({
        "q":      f"{artist} {title}".strip(),
        "type":   "release",
        "per_page": limit,
        "page":   1,
        "token":  DISCOGS_TOKEN,
    })
    headers = {
        "User-Agent": "BeetsWebControl/1.0",
        "Authorization": f"Discogs token={DISCOGS_TOKEN}",
    }
    try:
        req = _ur.Request(f"https://api.discogs.com/database/search?{params}", headers=headers)
        with provider_boundary.opened("discogs", req, timeout=12) as r:
            data = json.loads(r.read())
    except Exception:
        return []
    out = []
    for rel in (data.get("results") or [])[:limit]:
        rel_title = rel.get("title", "")          # "Artist - Album" format
        year      = str(rel.get("year") or "")
        country   = rel.get("country") or ""
        format_   = ", ".join(rel.get("format") or [])
        genres    = ", ".join(rel.get("genre") or [])
        discogs_id = rel.get("id")
        discogs_url = f"https://www.discogs.com/release/{discogs_id}" if discogs_id else ""
        # title is "Artist - Album"; extract if possible
        if " - " in rel_title:
            parts = rel_title.split(" - ", 1)
            d_artist, d_album = parts[0].strip(), parts[1].strip()
        else:
            d_artist, d_album = "", rel_title
        out.append({
            "score":       85,   # Discogs doesn't give a relevance score; use fixed
            "discogs_id":  discogs_id,
            "discogs_url": discogs_url,
            "artist":      d_artist,
            "album":       d_album,
            "year":        year,
            "country":     country,
            "format":      format_,
            "genre":       genres,
            "source":      "discogs",
        })
    return out


def _discogs_release_fallback_candidate(artist: str, album: str) -> Dict[str, Any]:
    """Best-effort Discogs release match for when MusicBrainz search finds
    nothing at all. Discogs' catalog is far larger than MusicBrainz's for a
    lot of regional/independent/reissue releases, so a Discogs hit here is
    useful two ways: its cleaner artist/album text can succeed at a retry
    MusicBrainz search that the raw folder-name guess couldn't, and even
    when MusicBrainz still has nothing, surfacing the Discogs match gives a
    reviewer something concrete to submit to MusicBrainz instead of a bare
    "no candidates found". Returns {} if Discogs isn't configured, nothing
    was found, or the top result's artist looks unrelated to the query.
    """
    if not DISCOGS_TOKEN or not (artist or album):
        return {}
    results = _discogs_track_search(album, artist, limit=5)
    if not results:
        return {}
    best = results[0]
    if artist and best.get("artist"):
        from difflib import SequenceMatcher
        similarity = SequenceMatcher(None, _s(best.get("artist")).lower(), _s(artist).lower()).ratio()
        if similarity < 0.55:
            return {}
    return best


def _discogs_artist_discography(artist_name: str) -> Dict[str, Any]:
    """Fetch artist releases from Discogs and compare against disk. Returns same shape as _fetch_discography."""

    if not DISCOGS_TOKEN:
        return {"ok": False, "error": "DISCOGS_TOKEN not set"}
    # Step 1: find artist
    q = _up.urlencode({"q": artist_name, "type": "artist", "per_page": 5, "page": 1, "token": DISCOGS_TOKEN})
    headers = {"User-Agent": "BeetsWebControl/1.0", "Authorization": f"Discogs token={DISCOGS_TOKEN}"}
    try:
        req = _ur.Request(f"https://api.discogs.com/database/search?{q}", headers=headers)
        with provider_boundary.opened("discogs", req, timeout=15) as r:
            results = json.loads(r.read()).get("results") or []
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if not results:
        return {"ok": False, "error": f"Artist '{artist_name}' not found on Discogs"}
    artist_hit = max(results, key=lambda r: _discogs_artist_score(r, artist_name))
    artist_id   = artist_hit["id"]
    artist_name_d = re.sub(r'\s+\(\d+\)\s*$', '', artist_hit.get("title") or artist_name)
    # Step 2: fetch releases (master releases = albums)
    releases: List[Dict] = []
    page = 1
    while page <= 5:
        time.sleep(1.1)   # Discogs: 60 req/min
        q2 = _up.urlencode({"sort": "year", "sort_order": "asc", "per_page": 100, "page": page, "token": DISCOGS_TOKEN})
        try:
            req2 = _ur.Request(f"https://api.discogs.com/artists/{artist_id}/releases?{q2}", headers=headers)
            with provider_boundary.opened("discogs", req2, timeout=15) as r2:
                page_data = json.loads(r2.read())
        except Exception:
            break
        items = page_data.get("releases") or []
        if not items:
            break
        # Keep only albums and masters (skip singles, compilations)
        for rel in items:
            rtype = (rel.get("type") or "").lower()
            role  = (rel.get("role")  or "").lower()
            # Include all master releases (canonical album entries on Discogs)
            # AND all releases where the artist has a main/primary role
            if rtype == "master" or role == "main":
                releases.append(rel)
        pagination = page_data.get("pagination") or {}
        if page >= (pagination.get("pages") or 1):
            break
        page += 1
    # Step 3: compare against disk
    # (MUSIC_ROOT is a module-level constant)
    album_index = _artist_album_index(artist_name)
    have: List[Dict]    = []
    missing: List[Dict] = []
    seen_titles: set    = set()
    for rel in releases:
        title = (rel.get("title") or "").strip()
        year  = str(rel.get("year") or "")
        if not title:
            continue
        norm = _normalize_album(title)
        if norm in seen_titles:
            continue
        seen_titles.add(norm)
        discogs_id  = rel.get("main_release") or rel.get("id")
        discogs_url = f"https://www.discogs.com/release/{discogs_id}" if discogs_id else f"https://www.discogs.com/artist/{artist_id}"
        matched, match_reason = _album_title_match(title, album_index)
        entry = {
            "album": title, "year": year, "discogs_url": discogs_url,
            "discogs_id": discogs_id, "role": rel.get("role", ""),
            "release_type": rel.get("type", ""), "match_reason": match_reason,
        }
        if matched:
            have.append(entry)
        else:
            missing.append(entry)
    missing.sort(key=lambda r: r["year"] or "9999")
    have.sort(key=lambda r: r["year"] or "9999")
    return {"ok": True, "source": "discogs", "artist": artist_name_d,
            "discogs_artist_id": artist_id, "have": have, "missing": missing,
            "total": len(have) + len(missing)}


def _artist_album_index(artist_name: str, mb_artistid: str = "") -> Dict[str, set]:
    """Return title/release-group keys known locally for an artist."""
    title_norms: set = set()
    title_keys: set = set()
    rgids: set = set()

    def add_title(title: str):
        norm = _normalize_album(title)
        key = _album_key(title)
        if norm:
            title_norms.add(norm)
        if key:
            title_keys.add(key)

    target_key = _artist_name_key(artist_name)
    candidate_dirs: List[Path] = []
    try:
        if MUSIC_ROOT.exists():
            exact = MUSIC_ROOT / artist_name
            if exact.exists() and exact.is_dir():
                candidate_dirs.append(exact)
            for d in MUSIC_ROOT.iterdir():
                if d.is_dir() and _artist_name_key(d.name) == target_key and d not in candidate_dirs:
                    candidate_dirs.append(d)
    except Exception:
        pass

    for adir in candidate_dirs:
        try:
            for d in adir.iterdir():
                if d.is_dir():
                    add_title(re.sub(r'\s*[\(\[]\d{4,8}[\)\]]\s*$', '', d.name).strip())
        except Exception:
            pass

    try:
        for ba in lib.albums([]):
            names = [
                _s(getattr(ba, "albumartist", "") or ""),
                _s(getattr(ba, "albumartist_credit", "") or ""),
            ]
            ids = (_split_mbid_values(_s(getattr(ba, "mb_albumartistids", "") or ""))
                   or _split_mbid_values(_s(getattr(ba, "mb_albumartistid", "") or "")))
            name_match = target_key in {_artist_name_key(n) for n in names if n}
            id_match = bool(mb_artistid and mb_artistid.lower() in ids)
            if not (name_match or id_match):
                continue
            add_title(_s(getattr(ba, "album", "") or ""))
            rgid = _s(getattr(ba, "mb_releasegroupid", "") or "").strip().lower()
            if _MB_UUID_RE.match(rgid):
                rgids.add(rgid)
    except Exception:
        pass

    return {"norms": title_norms, "keys": title_keys, "rgids": rgids}


def _discogs_artist_score(result: Dict[str, Any], artist_name: str) -> int:
    """Score Discogs artist search results so aliases do not always pick the first hit."""
    raw = (result.get("title") or "").strip()
    title = re.sub(r'\s+\(\d+\)\s*$', '', raw)
    want = _artist_name_key(artist_name)
    got = _artist_name_key(title)
    if got == want:
        return 100
    if want and (want in got or got in want):
        return 75
    return 0


def _mb_release_track_count(mb_releaseid: str, log: list = None) -> int:
    """Return MusicBrainz release track count, or 0 if unavailable."""
    if not _MB_UUID_RE.match((mb_releaseid or "").strip()):
        return 0
    mb = _fetch_mb_release_tracklist(mb_releaseid, log)
    if mb.get("ok"):
        return len(mb.get("tracks") or [])
    if log is not None:
        ex = mb.get("error") or "unknown MusicBrainz lookup error"
        log.append(f"  WARN: release track-count lookup failed: {ex}")
    return 0


def _mb_release_has_tracks(mb_releaseid: str) -> bool:
    """Return True when a UUID resolves as a MusicBrainz release with media."""
    return _mb_release_track_count(mb_releaseid) > 0


def _folder_track_search_titles(source_folder: str, existing_album_id: int = 0,
                                limit: int = 12) -> List[str]:
    """Return cleaned track titles from DB/files for tracklist-based MB lookup."""
    raw_entries: List[tuple] = []
    if existing_album_id:
        try:
            items = composite_workflows.find_all_items_by_album_id(int(existing_album_id))
            sorted_items = sorted(
                items,
                key=lambda r: (
                    int(r.get("disc") or 1),
                    int(r.get("track") or 0),
                    _s(r.get("title") or ""),
                    int(r.get("id") or 0),
                ),
            )
            raw_entries.extend((_s(r.get("title")), _s(r.get("path"))) for r in sorted_items)
        except BeetsUnavailableError:
            raise
        except Exception:
            pass
    try:
        source = Path(source_folder)
        # SEC-002 CodeQL repository-wide closure finding: same missing
        # containment check as _folder_release_preflight() above -- and the
        # exact pattern _folder_import_track_count() (a few lines above
        # this function) was already fixed for in Wave 1.
        if (
            _path_is_under(source, MUSIC_ROOT) or _path_is_under(source, DOWNLOADS_ROOT)
        ) and source.is_dir():
            files = sorted(
                [p for p in source.rglob("*")
                 if p.is_file() and p.suffix.lower() in AUDIO_EXT],
                key=lambda p: str(p).lower(),
            )
            raw_entries.extend(
                (_slskd_title_guess_from_name(p.name) or p.stem, str(p))
                for p in files
            )
    except Exception:
        pass

    titles: List[str] = []
    seen: set = set()
    for raw, path in raw_entries:
        # Strip MBID stamp artifacts from track names before MB search
        raw = re.sub(r"\{[^{}]*\}", "", raw).strip(" -_.")
        variants = _album_track_title_variants(raw, path)
        if variants:
            term = sorted(
                variants,
                key=lambda v: (
                    1 if re.match(r"^\d+\b", v) else 0,
                    len(v.split()),
                    len(v),
                ),
            )[0]
        else:
            term = _album_track_norm(raw)
        term = re.sub(r"^(?:bonus\s+track\s*)+", "", term, flags=re.IGNORECASE).strip()
        if not term or len(term) < 3 or term in seen:
            continue
        seen.add(term)
        titles.append(term)
        if len(titles) >= limit:
            break
    return titles


def _mb_release_search_by_folder_tracks(source_folder: str,
                                        existing_album_id: int = 0,
                                        artist: str = "",
                                        log: Optional[list] = None,
                                        limit: int = 8) -> List[Dict[str, Any]]:
    """Find release candidates by aggregating MB recording search release hits."""
    titles = _folder_track_search_titles(source_folder, existing_album_id, limit=12)
    if not titles:
        return []
    if log is not None:
        log.append(
            "  Searching MusicBrainz by folder track titles: "
            + ", ".join(titles[:5])
            + ("…" if len(titles) > 5 else "")
            + (f" (artist scoped to {artist})" if artist else "")
        )

    release_hits: Dict[str, Dict[str, Any]] = {}
    used_title_only_fallback = False
    for pos, title in enumerate(titles, start=1):
        data: Dict[str, Any] = {}
        query_terms: List[tuple] = []
        if artist:
            query_terms.append(("artist", f'artist:"{artist}" AND recording:"{title}"'))
        query_terms.append(("title", f'recording:"{title}"'))
        for query_scope, query in query_terms:
            params = _up.urlencode({
                "query": query,
                "limit": 50,
                "fmt": "json",
            })
            req = _ur.Request(
                f"https://musicbrainz.org/ws/2/recording?{params}",
                headers={"User-Agent": "BeetsWebControl/1.0 (beets-webcontrol)"},
            )
            data = {}
            # BA-5: provider_boundary.opened already retries; an outage
            # raises (never "no candidates" for the rest of the folder).
            try:
                with provider_boundary.opened("musicbrainz", req, timeout=25) as resp:
                    data = json.loads(resp.read())
            except Exception as ex:
                unavailable = _mb_unavailable(ex)
                if log is not None:
                    log.append(f"  WARN: MB recording search failed for {title!r}: {unavailable or ex}")
                if unavailable is not None:
                    raise unavailable from ex
            if data.get("recordings") or query_scope == "title":
                if query_scope == "title" and artist:
                    used_title_only_fallback = True
                break
        for rec in data.get("recordings", []) or []:
            artist_credit = " / ".join(
                x.get("artist", {}).get("name", "")
                for x in rec.get("artist-credit", [])
                if isinstance(x, dict)
            )
            for rel in rec.get("releases") or []:
                rid = _s(rel.get("id", "")).strip().lower()
                if not rid:
                    continue
                entry = release_hits.setdefault(rid, {
                    "score": 0,
                    "mb_albumid": rid,
                    "mb_url": f"https://musicbrainz.org/release/{rid}",
                    "album": _s(rel.get("title", "")),
                    "artist": artist_credit,
                    "year": _s(rel.get("date", ""))[:4],
                    "label": "",
                    "country": _s(rel.get("country", "")),
                    "tracks": 0,
                    "formats": [],
                    "is_vinyl": False,
                    "_hit_titles": set(),
                })
                entry["_hit_titles"].add(title)
                entry["score"] = len(entry["_hit_titles"])
        if pos < len(titles):
            time.sleep(1.0)

    if used_title_only_fallback and log is not None:
        log.append("  Artist-scoped recording search had gaps; used title-only fallback for some tracks.")

    cands = list(release_hits.values())
    for cand in cands:
        cand["score"] = int(cand.get("score") or 0) * 10
        cand.pop("_hit_titles", None)
    cands.sort(key=lambda c: c.get("score", 0), reverse=True)
    return cands[:limit]


class ReleaseArtUnavailable(Exception):
    """The art source could not be asked (timeout, 429, 5xx, DNS, policy):
    not an answer, so never cached as "no art" (IA-01)."""


def _release_art_download(mbid: str, url: str, source: str) -> str:
    """Download and cache one image. "" means the source answered without
    usable art (404, not an image, too large). Raises ReleaseArtUnavailable
    when the source could not be asked."""
    try:
        RELEASE_ART_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # Cover Art Archive / Discogs image URLs come from provider responses:
        # fetched with the public-only, address-pinned policy (opened_public).
        with provider_boundary.opened_public("artwork", url, timeout=15, headers=_IMAGE_FETCH_HEADERS) as r:
            content_type = r.headers.get("Content-Type", "")
            ext = _artist_image_ext(url, content_type)
            if not ext:
                return ""
            blob = r.read(_ARTIST_IMAGE_MAX_BYTES + 1)
    except OutboundPolicyError:
        return ""  # the provider pointed at a non-public address: unusable art
    except Exception as exc:
        unavailable = _mb_unavailable(exc)
        if unavailable is None:
            return ""  # the source answered (4xx): no such image
        raise ReleaseArtUnavailable(str(unavailable)) from exc
    try:
        if not blob or len(blob) > _ARTIST_IMAGE_MAX_BYTES:
            return ""
        if ext == ".jpeg":
            ext = ".jpg"
        image_name = f"{mbid}{ext}"
        image_path = RELEASE_ART_CACHE_DIR / image_name
        tmp_path = RELEASE_ART_CACHE_DIR / f"{image_name}.tmp"
        tmp_path.write_bytes(blob)
        tmp_path.replace(image_path)
        for stale in RELEASE_ART_CACHE_DIR.glob(f"{mbid}.*"):
            if stale.name not in {image_name, f"{mbid}.json"} and stale.is_file():
                try:
                    stale.unlink()
                except Exception:
                    pass
        meta = {
            "mbid": mbid,
            "source_url": url,
            "source": source,
            "image_file": image_name,
            "mime": (content_type or mimetypes.guess_type(image_name)[0] or "image/jpeg").split(";", 1)[0],
            "cached_at": time.time(),
        }
        (RELEASE_ART_CACHE_DIR / f"{mbid}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return f"/api/release-art-cache/{mbid}?v={int(image_path.stat().st_mtime)}"
    except Exception:
        return ""


def _fetch_release_group_art_discogs(artist_name: str, album_title: str) -> str:
    """Fallback source when Cover Art Archive has no art for a release group."""
    if not DISCOGS_TOKEN or not (artist_name or album_title):
        return ""
    q = _up.urlencode({
        "q": f"{artist_name} {album_title}".strip(),
        "type": "release",
        "per_page": 3,
        "page": 1,
        "token": DISCOGS_TOKEN,
    })
    headers = {"User-Agent": "BeetsWebControl/1.0", "Authorization": f"Discogs token={DISCOGS_TOKEN}"}
    try:
        req = _ur.Request(f"https://api.discogs.com/database/search?{q}", headers=headers)
        with provider_boundary.opened("discogs", req, timeout=10) as r:
            results = json.loads(r.read()).get("results") or []
        if results:
            return results[0].get("cover_image") or results[0].get("thumb") or ""
    except Exception:
        pass
    return ""


def _ensure_release_group_art(mbid: str, artist_name: str = "", album_title: str = "") -> Dict[str, Any]:
    """Serve a locally-stored release-group cover, downloading+caching on first request.
    Tries Cover Art Archive first, then Discogs (if configured) so a release with no
    MusicBrainz-registered art can still get a thumbnail. Misses are cached too, so a
    release with genuinely no art anywhere isn't re-fetched on every page load."""
    if not _RELEASE_ART_MBID_RE.match(mbid or ""):
        return {"ok": False, "error": "invalid mbid"}
    cached = _release_art_cache_info(mbid)
    if cached.get("url"):
        return {"ok": True, "url": cached["url"]}
    if cached.get("miss"):
        return {"ok": False, "error": "no art found"}

    unavailable = False
    try:
        url = _release_art_download(
            mbid, f"https://coverartarchive.org/release-group/{mbid}/front-250", "coverartarchive")
    except ReleaseArtUnavailable:
        url, unavailable = "", True
    if not url and (artist_name or album_title):
        discogs_url = _fetch_release_group_art_discogs(artist_name, album_title)
        if discogs_url:
            try:
                url = _release_art_download(mbid, discogs_url, "discogs")
            except ReleaseArtUnavailable:
                unavailable = True
    if not url:
        if unavailable:
            # IA-01: an outage is not "no art"; do not cache a 7-day miss.
            return {"ok": False, "error": "artwork source unavailable", "unavailable": True}
        _release_art_save_miss(mbid)
        return {"ok": False, "error": "no art found"}
    return {"ok": True, "url": url}


def _library_album_ids_for_musicbrainz(mb_albumid: str = "", mb_releasegroupid: str = "") -> List[int]:
    release_id = _extract_mb_uuid(_s(mb_albumid).strip().lower())
    release_group_id = _extract_mb_uuid(_s(mb_releasegroupid).strip().lower())
    if not release_id and not release_group_id:
        return []
    album_ids: List[int] = []
    try:
        if release_id:
            albums = composite_workflows.find_all_albums_by_mb_albumid(release_id)
            album_ids.extend(int(a["id"]) for a in albums if a.get("id"))
        if release_group_id:
            rg_albums = composite_workflows.find_all_albums_by_releasegroupid(release_group_id)
            for a in rg_albums[:25]:
                aid = int(a.get("id") or 0)
                if aid and aid not in album_ids:
                    album_ids.append(aid)
    except BeetsUnavailableError:
        raise
    except Exception:
        return []
    return album_ids


_MB_ENTITY_PATH_RE = re.compile(
    r"(?:^|/)("
    r"release-group|release|recording"
    r")/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:$|[/?#])",
    re.I,
)


def _parse_manual_musicbrainz_identifier(raw_value: Any) -> Dict[str, str]:
    text = _s(raw_value).strip()
    if not text:
        return {"ok": False, "error": "Enter a MusicBrainz UUID or URL."}
    match = _MB_ENTITY_PATH_RE.search(text)
    if match:
        entity = match.group(1).lower()
        return {"ok": True, "entity_type": entity, "mbid": match.group(2).lower()}
    uuid_match = re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        text,
        re.I,
    )
    if not uuid_match:
        return {"ok": False, "error": "This is not a valid MusicBrainz UUID or URL."}
    return {"ok": True, "entity_type": "unknown", "mbid": uuid_match.group(0).lower()}


def _prefer_album_mb_release(mb_albumid: str, log: list) -> str:
    """If mb_albumid points to a single/EP release, look up the recording on
    MusicBrainz and return the release ID of the best album that contains it.
    Returns the original mb_albumid unchanged if it is already an album or if
    no better album release can be found."""
    if not mb_albumid:
        return mb_albumid
    tracklist = _fetch_mb_release_tracklist(mb_albumid, log)
    if not tracklist.get("ok"):
        return mb_albumid
    primary_type = tracklist.get("release_group_primary_type", "").casefold()
    if primary_type not in ("single", "ep", ""):
        return mb_albumid  # Already an album — keep it
    # Find the first recording MBID on this release to look up its appearances
    mb_trackid = next(
        (t["mb_trackid"] for t in (tracklist.get("tracks") or []) if t.get("mb_trackid")),
        "",
    )
    if not mb_trackid:
        return mb_albumid
    details = _fetch_mb_recording_details(mb_trackid)
    album_release_id = details.get("mb_albumid", "")
    if not album_release_id or album_release_id == mb_albumid:
        return mb_albumid
    # Verify the found release is actually an album type
    album_data = _fetch_mb_release_tracklist(album_release_id, log)
    found_type = album_data.get("release_group_primary_type", "").casefold()
    if found_type not in ("single", "ep"):
        log.append(
            f"  [album-prefer] Upgrading {primary_type or 'unknown'} release "
            f"{mb_albumid!r} → album release {album_release_id!r} "
            f"({album_data.get('release_title', '')})"
        )
        return album_release_id
    return mb_albumid


# ── Clean: artist folder merge ────────────────────────────────────────────────

def _artist_folder_key(name: str) -> str:
    """Case/punctuation-insensitive key for duplicate artist folder detection."""
    text = _normalize_name(name).casefold()
    text = (
        text.replace("$", "s")
            .replace("@", "a")
            .replace("!", "i")
    )
    return re.sub(r"[^a-z0-9]+", "", text)


_mb_artist_cache: Dict[str, Dict[str, Any]] = {}


def _mb_artist_search_one(name: str) -> Dict[str, Any]:
    """Return the best MusicBrainz artist match for a name or {}.

    Used by cleanup to choose canonical spellings like Joey Bada$$.
    """
    raw = _normalize_name(name).strip()
    if not raw:
        return {}
    cache_key = raw.casefold()
    if cache_key in _mb_artist_cache:
        return _mb_artist_cache[cache_key]

    queries = [
        f'artist:"{raw}"',
        raw,
    ]
    best: Dict[str, Any] = {}
    key = _artist_folder_key(raw)
    for q in queries:
        try:
            params = _up.urlencode({"query": q, "limit": 8, "fmt": "json"})
            req = _ur.Request(
                f"https://musicbrainz.org/ws/2/artist?{params}",
                headers={"User-Agent": "BeetsWebControl/1.0 (beets-webcontrol)"}
            )
            with provider_boundary.opened("musicbrainz", req, timeout=8) as resp:
                data = json.loads(resp.read())
        except Exception:
            continue
        for artist in data.get("artists", []):
            mb_name = _s(artist.get("name", "")).strip()
            mb_id = _s(artist.get("id", "")).strip()
            if not mb_name or not _MB_UUID_RE.match(mb_id):
                continue
            score = int(artist.get("score", 0) or 0)
            mb_key = _artist_folder_key(mb_name)
            alias_match = any(
                _artist_folder_key((a or {}).get("name", "")) == key
                for a in artist.get("aliases", []) or []
                if isinstance(a, dict)
            )
            key_match = mb_key == key or alias_match
            rank = (
                0 if key_match else 1,
                -score,
                len(mb_name),
                mb_name.casefold(),
            )
            cand = {
                "id": mb_id,
                "name": mb_name,
                "score": score,
                "disambiguation": _s(artist.get("disambiguation", "")),
                "_rank": rank,
                "matched_query": q,
            }
            if not best or rank < best.get("_rank", (9, 0, 0, "")):
                best = cand
    if best:
        best.pop("_rank", None)
    _mb_artist_cache[cache_key] = best
    return best


def _mb_artist_lookup_by_id(mb_artistid: str) -> Dict[str, Any]:
    """Return MusicBrainz canonical artist details for an artist UUID."""
    mbid = _s(mb_artistid).strip().lower()
    if not _MB_UUID_RE.match(mbid):
        return {}
    cache_key = f"id:{mbid}"
    if cache_key in _mb_artist_cache:
        return _mb_artist_cache[cache_key]
    best: Dict[str, Any] = {}
    try:
        req = _ur.Request(
            f"https://musicbrainz.org/ws/2/artist/{mbid}?fmt=json",
            headers={"User-Agent": "BeetsWebControl/1.0 (beets-webcontrol)"}
        )
        with provider_boundary.opened("musicbrainz", req, timeout=8) as resp:
            data = json.loads(resp.read())
        mb_name = _s(data.get("name", "")).strip()
        if mb_name:
            best = {
                "id": mbid,
                "name": mb_name,
                "disambiguation": _s(data.get("disambiguation", "")),
                "matched_query": mbid,
            }
    except Exception:
        best = {}
    _mb_artist_cache[cache_key] = best
    return best


def _mb_canonical_for_artist_entries(entries: List[Dict[str, Any]], key: str) -> Dict[str, Any]:
    matches = []
    for e in entries:
        m = _mb_artist_search_one(e["name"])
        if not m:
            continue
        if _artist_folder_key(m.get("name", "")) != key:
            # Keep alias hits only when the search score is strong enough.
            if int(m.get("score") or 0) < 90:
                continue
        matches.append({"entry": e, "match": m})
    if not matches:
        return {}

    by_id: Dict[str, Dict[str, Any]] = {}
    for item in matches:
        m = item["match"]
        rec = by_id.setdefault(m["id"], {
            "id": m["id"],
            "name": m["name"],
            "score_total": 0,
            "count": 0,
            "entries": [],
            "disambiguation": m.get("disambiguation", ""),
        })
        rec["score_total"] += int(m.get("score") or 0)
        rec["count"] += 1
        rec["entries"].append(item["entry"]["name"])
        # Prefer the MB display spelling from the best scoring hit.
        if int(m.get("score") or 0) >= rec.get("best_score", -1):
            rec["name"] = m["name"]
            rec["best_score"] = int(m.get("score") or 0)
    best = sorted(
        by_id.values(),
        key=lambda r: (-r["count"], -r["score_total"], r["name"].casefold())
    )[0]
    return best


def _clean_malformed_release_group_stamps(value: str) -> str:
    return _MALFORMED_RELEASE_GROUP_STAMP_RE.sub(r"{\1}", _s(value))


def _folder_cleanup_known_release_group_id(album_ids: Iterable[int]) -> str:
    ids = sorted({int(album_id) for album_id in album_ids if int(album_id or 0) > 0})
    if not ids:
        return ""
    for aid in ids:
        try:
            alb = composite_workflows.get_album(aid)
            if alb:
                rgid = _s(alb.get("mb_releasegroupid")).strip()
                if _is_valid_mb_uuid(rgid):
                    return rgid
        except Exception:
            continue
    return ""


def _folder_cleanup_release_group_from_name(name: str) -> str:
    for match in re.finditer(r"\{([0-9a-fA-F-]{36})\}", _s(name)):
        candidate = match.group(1)
        if _is_valid_mb_uuid(candidate):
            return candidate
    return ""


def _album_cleanup_valid_rgid(value: Any) -> str:
    text = _s(value).strip().lower()
    return text if _MB_UUID_RE.match(text) else ""


def _album_cleanup_embedded_musicbrainz_tags(folder: Path, inventory: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    rgids: List[str] = []
    release_ids: List[str] = []
    albums: List[str] = []
    years: List[str] = []
    inspected = 0
    audio_items = [
        (_s(rel), info)
        for rel, info in sorted((inventory or {}).items(), key=lambda item: _s(item[0]).casefold())
        if info.get("is_audio")
    ]
    for rel, info in audio_items[:24]:
        raw_path = _s(info.get("path") or str(folder / rel)).strip()
        if not raw_path:
            continue
        try:
            tags = _read_file_media_tags(raw_path)
            if not tags:
                continue
        except Exception:
            continue
        inspected += 1
        rgid = _album_cleanup_valid_rgid(tags.get("mb_releasegroupid", ""))
        if rgid:
            rgids.append(rgid)
        release_id = _album_cleanup_valid_rgid(tags.get("mb_albumid", ""))
        if release_id:
            release_ids.append(release_id)
        album = _s(tags.get("album", "")).strip()
        if album:
            albums.append(album)
        year = _s(tags.get("year", "")).strip()[:4]
        if year and year.isdigit():
            years.append(year)

    return {
        "mb_releasegroupids": sorted(set(rgids)),
        "mb_albumids": sorted(set(release_ids)),
        "albums": albums,
        "years": years,
        "inspected": inspected,
    }
