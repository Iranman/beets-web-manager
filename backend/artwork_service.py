"""Album and artist artwork: fetch, validate, cache and repair (ARCH-001).
"""

from __future__ import annotations

import backend.provider_boundary as provider_boundary
import base64, hashlib, io, json, mimetypes, os, re, threading, time
import urllib.error
from backend.security import OutboundPolicyError, resolve_public_target
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from backend.app_runtime import _app_logger, ARTIST_IMAGE_CACHE_DIR, ART_REPAIR_LAST_FILE, METADATA_CACHE_ROOT, MUSIC_ROOT, RELEASE_ART_CACHE_DIR, _s, _up, _ur
from backend.app_runtime import _path_has_symlink_component_under, _path_is_under, _redact_security_text
from backend.beets_adapter import lib, BeetsError, BeetsUnavailableError
import backend.composite_workflows as composite_workflows
from backend.transaction_engine import _sniff_unsupported_image_format

# ── ARCH-001 extracted code ──


def _beets_config_discogs_token() -> str:
    cfg_path = Path(os.environ.get("BEETS_CONFIG", "/config/config.yaml"))
    try:
        in_discogs = False
        discogs_indent = 0
        for raw in cfg_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            if indent == 0 and stripped.endswith(":"):
                in_discogs = stripped[:-1].strip() == "discogs"
                discogs_indent = indent
                continue
            if in_discogs and indent <= discogs_indent:
                in_discogs = False
            if in_discogs and stripped.startswith("user_token:"):
                return stripped.split(":", 1)[1].strip().strip("\"'")
    except Exception:
        pass
    return ""


DISCOGS_TOKEN = (
    os.environ.get("DISCOGS_TOKEN", "").strip()
    or os.environ.get("DISCOGS_USER_TOKEN", "").strip()
    or _beets_config_discogs_token()
)


def _artist_name_key(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', (s or "").lower().replace("&", "and"))


_ARTIST_IMAGE_MAX_BYTES = 8 * 1024 * 1024


_ARTIST_IMAGE_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _artist_image_cache_key(artist_name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (artist_name or "").lower()).strip("-")[:80]
    digest = hashlib.sha1((artist_name or "").strip().lower().encode("utf-8")).hexdigest()[:12]
    return f"{base or 'artist'}-{digest}"


def _artist_image_cache_info(artist_name: str) -> Dict[str, Any]:
    key = _artist_image_cache_key(artist_name)
    meta_path = ARTIST_IMAGE_CACHE_DIR / f"{key}.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        image_name = _s(meta.get("image_file", "") or "")
        if not image_name or Path(image_name).name != image_name:
            return {}
        image_path = ARTIST_IMAGE_CACHE_DIR / image_name
        if image_path.exists() and image_path.is_file():
            return {
                "url": f"/api/artist-image-cache/{key}?v={int(image_path.stat().st_mtime)}",
                "key": key,
                "image_file": image_name,
                "cached_at": meta.get("cached_at") or 0,
                "source_url": _s(meta.get("source_url", "") or ""),
            }
    except Exception:
        pass
    return {}


def _artist_image_cache_url(artist_name: str) -> str:
    return _s(_artist_image_cache_info(artist_name).get("url", "") or "")


def _attach_artist_image_cache_urls(artists: List[Dict[str, Any]]) -> None:
    for artist in artists:
        if not isinstance(artist, dict):
            continue
        url = _artist_image_cache_url(_s(artist.get("name", "") or ""))
        artist["image_url"] = url
        artist["artist_image_url"] = url
        artist["image_source"] = "artist_cache" if url else ""


def _artist_image_ext(url: str, content_type: str) -> str:
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if ctype in _ARTIST_IMAGE_EXT_BY_MIME:
        return _ARTIST_IMAGE_EXT_BY_MIME[ctype]
    ext = Path(_up.urlparse(url or "").path).suffix.lower()
    return ext if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else ""


# Image URLs come from users or provider responses, so they are fetched with
# provider_boundary.opened_public(): public internet only, the outbound
# allowlist ignored, the socket pinned to the validated address, and every
# redirect hop re-validated (CodeQL #1350).
_IMAGE_FETCH_HEADERS = {"User-Agent": "BeetsWebControl/1.0"}


def _cache_artist_image(artist_name: str, url: str) -> str:
    parsed = _up.urlparse(url or "")
    if parsed.scheme not in {"http", "https"}:
        return ""
    try:
        ARTIST_IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with provider_boundary.opened_public("artwork", url, timeout=15, headers=_IMAGE_FETCH_HEADERS) as r:
            content_type = r.headers.get("Content-Type", "")
            ext = _artist_image_ext(url, content_type)
            if not ext:
                return ""
            blob = r.read(_ARTIST_IMAGE_MAX_BYTES + 1)
        if not blob or len(blob) > _ARTIST_IMAGE_MAX_BYTES:
            return ""
        key = _artist_image_cache_key(artist_name)
        if ext == ".jpeg":
            ext = ".jpg"
        image_name = f"{key}{ext}"
        image_path = ARTIST_IMAGE_CACHE_DIR / image_name
        tmp_path = ARTIST_IMAGE_CACHE_DIR / f"{image_name}.tmp"
        tmp_path.write_bytes(blob)
        tmp_path.replace(image_path)
        for stale in ARTIST_IMAGE_CACHE_DIR.glob(f"{key}.*"):
            if stale.name not in {image_name, f"{key}.json"} and stale.is_file():
                try:
                    stale.unlink()
                except Exception:
                    pass
        meta = {
            "artist": artist_name,
            "source_url": url,
            "image_file": image_name,
            "mime": (content_type or mimetypes.guess_type(image_name)[0] or "image/jpeg").split(";", 1)[0],
            "cached_at": time.time(),
        }
        (ARTIST_IMAGE_CACHE_DIR / f"{key}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return f"/api/artist-image-cache/{key}?v={int(image_path.stat().st_mtime)}"
    except Exception:
        return ""


def _fetch_artist_image(artist_name: str) -> str:
    """Fetch artist image URL from Discogs search. Returns empty string if not found."""

    if not DISCOGS_TOKEN:
        return ""
    q = _up.urlencode({"q": artist_name, "type": "artist", "per_page": 3, "page": 1})
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


def _artist_local_art_url(artist_name: str) -> str:
    """Use a local album cover as an artist-card fallback."""
    want = _artist_name_key(artist_name)
    fallback = ""
    try:
        for album in lib.albums():
            album_artist = _s(getattr(album, "albumartist", "") or getattr(album, "artist", ""))
            if _artist_name_key(album_artist) != want:
                continue
            aid = int(getattr(album, "id", 0) or 0)
            if not aid:
                continue
            artpath = _s(getattr(album, "artpath", "") or "")
            mbid = (_s(getattr(album, "mb_releasegroupid", "") or "")
                    or _s(getattr(album, "mb_albumid", "") or ""))
            if artpath:
                return f"/api/albums/{aid}/art"
            if not fallback or mbid:
                fallback = f"/api/albums/{aid}/art"
    except Exception:
        return fallback
    return fallback


_RELEASE_ART_MBID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


_RELEASE_ART_NEGATIVE_TTL = 7 * 24 * 3600  # re-check a "no art found" release weekly, not every page load


def _release_art_cache_info(mbid: str) -> Dict[str, Any]:
    if not _RELEASE_ART_MBID_RE.match(mbid or ""):
        return {}
    meta_path = RELEASE_ART_CACHE_DIR / f"{mbid}.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if meta.get("miss"):
        if time.time() - float(meta.get("cached_at") or 0) < _RELEASE_ART_NEGATIVE_TTL:
            return {"miss": True}
        return {}
    image_name = _s(meta.get("image_file", "") or "")
    if not image_name or Path(image_name).name != image_name:
        return {}
    image_path = RELEASE_ART_CACHE_DIR / image_name
    if not image_path.exists() or not image_path.is_file():
        return {}
    return {
        "url": f"/api/release-art-cache/{mbid}?v={int(image_path.stat().st_mtime)}",
        "image_file": image_name,
    }


def _release_art_save_miss(mbid: str) -> None:
    try:
        RELEASE_ART_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (RELEASE_ART_CACHE_DIR / f"{mbid}.json").write_text(
            json.dumps({"miss": True, "cached_at": time.time()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


_ALBUM_ART_NAMES = (
    "albumart.jpg", "albumart.jpeg", "albumart.png", "albumart.webp",
    "folder.jpg", "folder.jpeg", "folder.png", "folder.webp",
    "cover.jpg", "cover.jpeg", "cover.png", "cover.webp",
    "front.jpg", "front.jpeg", "front.png", "front.webp",
)


_ART_SUBDIR_NAMES = frozenset({"artwork", "covers", "scans", "art", "images"})


_ART_EXTS = frozenset({'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'})


def _move_artwork_to_target(src_dir: Path, album_ids: list, log: list) -> Optional[Path]:
    """Move artwork files from src_dir into the canonical beets album folder.

    Queries the DB to find the target folder, then moves each image file with
    safe conflict naming (cover-1.jpg, cover-2.jpg …).  Identical files that
    are already present at the target are removed from the source without
    copying.  Unknown/non-image files are never touched.

    Returns the resolved target Path if found, otherwise None.
    """
    if not src_dir.is_dir():
        return None

    target_dir: Optional[Path] = None
    for aid in album_ids:
        try:
            items = composite_workflows.find_all_items_by_album_id(int(aid))
            for item in items:
                raw = item.get("path")
                if raw:
                    p = Path(os.fsdecode(raw) if isinstance(raw, bytes) else raw)
                    if p.parent.is_dir():
                        target_dir = p.parent
                        break
            if target_dir:
                break
        except BeetsUnavailableError as ex:
            log.append(f"  [artwork] Engine unavailable for album {aid}: {ex}")
            raise
        except Exception as ex:
            log.append(f"  [artwork] Lookup for album {aid}: {ex}")

    if not target_dir:
        log.append("  [artwork] Cannot find canonical album folder — artwork left in source.")
        return None

    try:
        if target_dir.resolve() == src_dir.resolve():
            return target_dir  # source IS the library folder; nothing to move
    except Exception:
        pass

    # Engine-controlled relocation only (SEC-002 Wave 22 final review,
    # findings #9/#11): the previous local `shutil.move`-based fallback
    # ran whenever the engine call raised or reported non-ok, which meant
    # the engine path (which, before this review, always silently
    # quarantined instead of moving while still reporting success) could
    # mask real artwork loss behind a truthful-looking log line, AND kept
    # a second, parallel, unmigrated local mutation path alive in
    # production. Engine unavailable or Plan/Apply failure now fails
    # closed -- artwork is left in place in src_dir and the caller is
    # told nothing was moved, never silently mutated locally.
    top_level_files = [f for f in src_dir.iterdir() if f.is_file() and f.suffix.lower() in _ART_EXTS]
    subdirs = [d for d in src_dir.iterdir() if d.is_dir() and d.name.lower() in _ART_SUBDIR_NAMES]
    candidates = [{"source": str(f)} for f in top_level_files]
    for d in subdirs:
        candidates.extend({"source": str(f)} for f in sorted(d.rglob("*")) if f.is_file() and f.suffix.lower() in _ART_EXTS)

    if not candidates:
        return target_dir

    try:
        aid = album_ids[0] if album_ids else 0
        plan_res = composite_workflows.plan_album_artwork({
            "mode": "move",
            "album_id": aid,
            "target_dir": str(target_dir),
            "candidates": candidates,
        })
        if not plan_res.get("ok"):
            log.append(f"  [artwork] Engine relocation plan rejected: {plan_res.get('error')} — artwork left in source.")
            return target_dir
        op_id = plan_res.get("operation_id")
        if not op_id:
            return target_dir  # nothing eligible to move
        apply_res = composite_workflows.apply_album_artwork(op_id)
        if not apply_res.get("ok"):
            log.append(f"  [artwork] Engine relocation apply failed: {apply_res.get('error')} — artwork left in source.")
            return target_dir
        log.append(
            f"  [artwork] Engine controlled artwork relocation applied: {target_dir} "
            f"(moved={apply_res.get('moved_count')}, deduped={apply_res.get('quarantined_count')})"
        )
    except (BeetsUnavailableError, BeetsError) as ex:
        log.append(f"  [artwork] Engine unreachable — artwork relocation not performed: {ex}")
        return target_dir
    except Exception as ex:
        _app_logger.error("Artwork relocation: unexpected engine communication failure: %s", ex)
        log.append("  [artwork] Unexpected engine communication failure — artwork relocation not performed.")
        return target_dir

    # Empty subdirectories the engine emptied out are cosmetic cleanup
    # only (not a Beets-library mutation); safe to remove locally.
    for d in subdirs:
        for sub in sorted(d.rglob("*"), reverse=True):
            if sub.is_dir():
                try:
                    sub.rmdir()
                except OSError:
                    pass
        try:
            d.rmdir()
        except OSError:
            pass

    return target_dir


def _album_dir_for_art(album) -> Optional[Path]:
    """Return the album directory under MUSIC_ROOT, if it can be resolved."""
    raw = _get_album_item_dir(album)
    if raw:
        aldir = Path(raw)
        if not aldir.is_absolute():
            aldir = MUSIC_ROOT / raw
        if _path_is_under(aldir, MUSIC_ROOT):
            return aldir

    disc_re = re.compile(r'^(?:disc|cd|disk)\s*\d+$', re.I)
    try:
        for item in album.items():
            raw_path = _s(item.path)
            if not raw_path:
                continue
            p = Path(raw_path)
            if not p.is_absolute():
                p = MUSIC_ROOT / raw_path
            aldir = p.parent
            if disc_re.match(aldir.name):
                aldir = aldir.parent
            if _path_is_under(aldir, MUSIC_ROOT):
                return aldir
    except Exception:
        return None
    return None


def _album_stored_art_path(album) -> Optional[Path]:
    raw = _s(getattr(album, "artpath", "") or "").replace("\x00", "").strip()
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = MUSIC_ROOT / raw
    return p if _path_is_under(p, MUSIC_ROOT) else None


def _album_path_repair_summary(album) -> Dict[str, Any]:
    total = 0
    outside = 0
    missing = 0
    first_path = ""
    first_outside = ""
    try:
        items = list(album.items()) if album else []
    except Exception:
        items = []
    for item in items:
        raw = _s(getattr(item, "path", "") or "").strip()
        if not raw:
            continue
        p = Path(raw)
        if not p.is_absolute():
            p = MUSIC_ROOT / raw
        total += 1
        if not first_path:
            first_path = str(p)
        if not _path_is_under(p, MUSIC_ROOT):
            outside += 1
            if not first_outside:
                first_outside = str(p)
        try:
            if not p.exists():
                missing += 1
        except Exception:
            missing += 1
    return {
        "track_count": total,
        "outside_library_count": outside,
        "missing_file_count": missing,
        "first_track_path": first_outside or first_path,
        "path_issue": "outside_library_root" if outside else ("missing_files" if missing else ""),
        "can_move_to_library": bool(outside),
    }


def _album_art_candidates(album) -> List[Path]:
    seen = set()
    candidates: List[Path] = []
    current = _album_stored_art_path(album)
    aldir = _album_dir_for_art(album)
    if current and _path_is_under(current, MUSIC_ROOT) and not current.is_symlink() and not _path_has_symlink_component_under(current, MUSIC_ROOT) and current.exists() and current.is_file():
        seen.add(str(current.resolve(strict=False)))
        candidates.append(current)
    if aldir:
        for name in _ALBUM_ART_NAMES:
            p = aldir / name
            key = str(p.resolve(strict=False))
            if key in seen:
                continue
            if _path_is_under(p, MUSIC_ROOT) and not p.is_symlink() and not _path_has_symlink_component_under(p, MUSIC_ROOT) and p.exists() and p.is_file():
                seen.add(key)
                candidates.append(p)
    return candidates


def _usable_album_art_file(path: Optional[Path]) -> bool:
    try:
        return bool(
            path
            and _path_is_under(path, MUSIC_ROOT)
            and not path.is_symlink()
            and not _path_has_symlink_component_under(path, MUSIC_ROOT)
            and path.exists()
            and path.is_file()
            and path.stat().st_size >= 1000
        )
    except Exception:
        return False


def _album_art_status(aid: int) -> Optional[Dict[str, Any]]:
    album = lib.get_album(aid)
    if not album:
        return None
    aldir = _album_dir_for_art(album)
    current = _album_stored_art_path(album)
    candidates = _album_art_candidates(album)
    usable = [p for p in candidates if _usable_album_art_file(p)]
    local = current if _usable_album_art_file(current) else (usable[0] if usable else None)
    return {
        "ok": True,
        "album_id": aid,
        "album": _s(getattr(album, "album", "") or ""),
        "albumartist": _s(getattr(album, "albumartist", "") or getattr(album, "artist", "") or ""),
        "album_dir": str(aldir) if aldir else "",
        "art_url": f"/api/albums/{aid}/art",
        "artpath": str(current) if current else "",
        "art_exists": bool(current and current.exists()),
        "local_art_path": str(local) if local else "",
        "has_local_art": bool(local),
        "has_removable_art": bool(candidates),
        "broken_art_count": len(candidates) - len(usable),
        "candidates": [
            {
                "name": p.name,
                "path": str(p),
                "size": p.stat().st_size if p.exists() else 0,
                "usable": _usable_album_art_file(p),
            }
            for p in candidates
        ],
    }


def _album_art_repair_entry(album, status: Optional[Dict[str, Any]] = None,
                            issue: str = "", reason: str = "") -> Dict[str, Any]:
    try:
        aid = int(getattr(album, "id", 0) or 0)
    except Exception:
        aid = 0
    status = status or (_album_art_status(aid) if aid else None) or {}
    aldir = _album_dir_for_art(album) if album else None
    path_summary = _album_path_repair_summary(album)
    has_art = bool(status.get("has_local_art"))
    broken_count = int(status.get("broken_art_count") or 0)
    has_candidates = bool(status.get("has_removable_art") or status.get("candidates"))
    aldir_exists = bool(aldir and aldir.exists())
    if not issue:
        if path_summary.get("outside_library_count"):
            issue = "unresolved"
            reason = reason or f"Track files are outside {MUSIC_ROOT}; move the album into the library before art repair"
        elif not aldir:
            issue = "unresolved"
            reason = reason or "Album folder could not be resolved"
        elif not aldir_exists:
            issue = "unresolved"
            reason = reason or "Album folder no longer exists on disk"
        elif has_candidates and not has_art:
            issue = "broken"
            reason = reason or "Local art candidates exist but none are usable"
        else:
            issue = "missing"
            reason = reason or "No usable local album art found"
    return {
        "album_id": aid,
        "albumartist": _s(getattr(album, "albumartist", "") or getattr(album, "artist", "") or status.get("albumartist", "") or ""),
        "album": _s(getattr(album, "album", "") or status.get("album", "") or ""),
        "year": int(getattr(album, "year", 0) or 0),
        "mb_albumid": _s(getattr(album, "mb_albumid", "") or ""),
        "mb_releasegroupid": _s(getattr(album, "mb_releasegroupid", "") or ""),
        "album_dir": str(aldir) if aldir else _s(status.get("album_dir", "") or ""),
        "artpath": _s(status.get("artpath", "") or getattr(album, "artpath", "") or ""),
        "local_art_path": _s(status.get("local_art_path", "") or ""),
        "has_local_art": has_art,
        "has_removable_art": bool(status.get("has_removable_art")),
        "broken_art_count": broken_count,
        "candidate_count": len(status.get("candidates") or []),
        "track_count": int(path_summary.get("track_count") or 0),
        "outside_library_count": int(path_summary.get("outside_library_count") or 0),
        "missing_file_count": int(path_summary.get("missing_file_count") or 0),
        "first_track_path": _s(path_summary.get("first_track_path") or ""),
        "path_issue": _s(path_summary.get("path_issue") or ""),
        "can_move_to_library": bool(path_summary.get("can_move_to_library")),
        "repair_action": "move_to_library" if path_summary.get("can_move_to_library") else "",
        "issue": issue,
        "reason": reason,
        "actionable": bool(aid and aldir_exists),
        "last_status": "",
        "last_error": "",
        "last_source": "",
    }


def _art_repair_load_last() -> Dict[str, Any]:
    try:
        data = json.loads(ART_REPAIR_LAST_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _art_repair_save_last(report: Dict[str, Any]) -> None:
    try:
        ART_REPAIR_LAST_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = ART_REPAIR_LAST_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, default=str), encoding="utf-8")
        tmp.replace(ART_REPAIR_LAST_FILE)
    except Exception:
        pass


def _art_repair_build_report() -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    counts = {"missing": 0, "broken": 0, "unresolved": 0, "total": 0}
    total_albums = 0
    for album in lib.albums([]):
        total_albums += 1
        try:
            aid = int(getattr(album, "id", 0) or 0)
        except Exception:
            aid = 0
        status = _album_art_status(aid) if aid else None
        if status and status.get("has_local_art"):
            continue
        entry = _album_art_repair_entry(album, status)
        issue = entry.get("issue") or "missing"
        counts[issue] = int(counts.get(issue, 0)) + 1
        counts["total"] += 1
        items.append(entry)
    items.sort(key=lambda item: (
        {"unresolved": 0, "broken": 1, "missing": 2}.get(_s(item.get("issue")), 9),
        _s(item.get("albumartist")).lower(),
        _s(item.get("album")).lower(),
    ))
    return {
        "ok": True,
        "generated_at": time.time(),
        "total_albums": total_albums,
        "counts": counts,
        "items": items,
    }


def _art_repair_attach_last_run(report: Dict[str, Any],
                                last_run: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = dict(report)
    last = last_run if isinstance(last_run, dict) else _art_repair_load_last()
    by_id: Dict[int, Dict[str, Any]] = {}
    for group_name in ("failed_items", "saved_items", "unresolved_items"):
        for row in last.get(group_name) or []:
            try:
                aid = int(row.get("album_id") or 0)
            except Exception:
                aid = 0
            if aid:
                by_id[aid] = row
    items = []
    for item in out.get("items") or []:
        next_item = dict(item)
        try:
            aid = int(next_item.get("album_id") or 0)
        except Exception:
            aid = 0
        last_item = by_id.get(aid)
        if last_item:
            next_item["last_status"] = _s(last_item.get("status") or "")
            next_item["last_error"] = _s(last_item.get("error") or "")
            next_item["last_source"] = _s(last_item.get("source") or "")
        items.append(next_item)
    out["items"] = items
    out["last_run"] = last
    return out


def _album_art_clear_pointer(aid: int, album, log: List[str]) -> None:
    try:
        composite_workflows.clear_album_artpath(aid)
    except Exception as ex:
        log.append(f"  artpath clear warning: {ex}")


def _album_art_set_pointer(aid: int, path: str, log: List[str]) -> None:
    try:
        composite_workflows.set_album_artpath(aid, _s(path))
    except Exception as ex:
        log.append(f"  artpath restore warning: {ex}")


def _album_art_quarantine_current(aid: int, album, trash_root: Path, log: List[str]) -> Dict[str, Any]:
    aldir = _album_dir_for_art(album)
    current = _album_stored_art_path(album)
    candidates = _album_art_candidates(album)
    if current and _path_is_under(current, MUSIC_ROOT) and not current.is_symlink() and not _path_has_symlink_component_under(current, MUSIC_ROOT) and current.exists() and current.is_file():
        candidates.append(current)
    seen: set = set()
    cand_list: List[Dict[str, str]] = []
    for src in candidates:
        key = str(src.resolve(strict=False))
        if key not in seen and _path_is_under(src, MUSIC_ROOT) and src.exists() and src.is_file() and not src.is_symlink():
            seen.add(key)
            cand_list.append({"source": str(src)})

    if not cand_list:
        return {
            "original_artpath": str(current) if current else "",
            "quarantined_art": [],
            "quarantined_count": 0,
        }

    payload = {
        "mode": "quarantine",
        "album_id": aid,
        "candidates": cand_list,
    }
    try:
        plan_res = composite_workflows.plan_album_artwork(payload)
        if not plan_res.get("ok"):
            log.append(f"  art quarantine engine plan warning: {plan_res.get('error')}")
            return {"original_artpath": str(current) if current else "", "quarantined_art": [], "quarantined_count": 0}
        op_id = plan_res["operation_id"]
        apply_res = composite_workflows.apply_album_artwork(op_id)
        if not apply_res.get("ok"):
            log.append(f"  art quarantine engine apply warning: {apply_res.get('error')}")
            return {"original_artpath": str(current) if current else "", "quarantined_art": [], "quarantined_count": 0}
        log.append(f"  quarantined current art: {len(cand_list)} file(s)")
        return {
            "operation_id": op_id,
            "original_artpath": str(current) if current else "",
            "quarantined_art": cand_list,
            "quarantined_count": len(cand_list),
        }
    except Exception as ex:
        log.append(f"  art quarantine warning: {ex}")
        return {
            "original_artpath": str(current) if current else "",
            "quarantined_art": [],
            "quarantined_count": 0,
        }


def _album_art_restore_quarantine(aid: int, quarantine: Dict[str, Any], log: List[str]) -> int:
    op_id = quarantine.get("operation_id")
    if op_id:
        try:
            res = composite_workflows.rollback_album_artwork(op_id)
            if res.get("ok"):
                restored = int(res.get("files_restored") or 0)
                log.append(f"  restored previous art: {restored} file(s)")
                return restored
        except Exception as ex:
            log.append(f"  art restore warning: {ex}")
    return 0


def _repair_album_art(aid: int, log: List[str], cancel_event=None,
                      cfg: str = "", env: Optional[Dict[str, str]] = None,
                      force: bool = False,
                      trash_root: Optional[Path] = None) -> Dict[str, Any]:
    album = lib.get_album(aid)
    if not album:
        return {
            "album_id": aid,
            "albumartist": "",
            "album": "",
            "status": "failed",
            "source": "",
            "error": "Album not found",
        }
    entry = _album_art_repair_entry(album)
    artist_name = _s(entry.get("albumartist") or "")
    album_name = _s(entry.get("album") or "")
    quarantine: Dict[str, Any] = {}
    if entry.get("has_local_art") and not force:
        return {**entry, "status": "skipped", "source": "local", "error": ""}
    if not entry.get("actionable"):
        return {**entry, "status": "unresolved", "source": "", "error": entry.get("reason") or "Album folder could not be resolved"}
    if force:
        quarantine = _album_art_quarantine_current(
            aid,
            album,
            trash_root or (METADATA_CACHE_ROOT / "album-art-rebuild-trash" / time.strftime("%Y%m%d-%H%M%S")),
            log,
        )
        if quarantine.get("quarantined_count"):
            log.append(f"  removed current art before fresh fetch ({quarantine.get('quarantined_count')} file(s))")

    key = f"{artist_name.lower()}::{album_name.lower()}"
    with _album_art_cache_lock:
        cached = _album_art_cache.get(key)
    url = cached or _fetch_album_art(artist_name, album_name)
    if url:
        with _album_art_cache_lock:
            _album_art_cache[key] = url
    expected_rgid = _album_art_expected_release_group(album)
    saved_path = _save_art_to_disk(
        url,
        aid,
        expected_mb_releasegroupid=expected_rgid,
        source="discogs",
        log=log,
    ) if url else ""
    if not saved_path:
        error = "Discogs fallback download failed" if url else "No art found by fetchart or Discogs fallback"
        restored = _album_art_restore_quarantine(aid, quarantine, log) if quarantine else 0
        return {
            **entry,
            "status": "failed",
            "source": "discogs" if url else "",
            "error": error,
            **quarantine,
            "restored_current_art": restored,
        }

    return {
        **_album_art_repair_entry(album, _album_art_status(aid)),
        "status": "saved",
        "source": "discogs",
        "error": "",
        "saved_path": saved_path,
        **quarantine,
    }


# ── Album art cache (Discogs) ─────────────────────────────────────────────────
_album_art_cache: Dict[str, str] = {}


_album_art_cache_lock = threading.Lock()


def _fetch_album_art(artist: str, album: str) -> str:
    """Fetch album cover URL from Discogs. Returns empty string if not found."""

    if not DISCOGS_TOKEN:
        return ""
    q = _up.urlencode({
        "q":       f"{artist} {album}".strip(),
        "type":    "release",
        "per_page": 3, "page": 1,
    })
    headers = {"User-Agent": "BeetsWebControl/1.0",
               "Authorization": f"Discogs token={DISCOGS_TOKEN}"}
    try:
        req = _ur.Request(f"https://api.discogs.com/database/search?{q}", headers=headers)
        with provider_boundary.opened("discogs", req, timeout=10) as r:
            results = json.loads(r.read()).get("results") or []
        if results:
            return results[0].get("cover_image") or results[0].get("thumb") or ""
    except Exception:
        pass
    return ""


def _album_art_ext_for_bytes(data: bytes, content_type: str = "") -> str:
    """Return a supported cover-art extension for validated bytes."""
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }.get(ctype, "")


_ALBUM_ART_UPLOAD_MAX_BYTES = 15 * 1024 * 1024
_ALBUM_ART_PILLOW_FORMATS = ("JPEG", "PNG", "WEBP")


_ALBUM_ART_MAX_PIXELS = 50_000_000


_ALBUM_ART_MAX_DIMENSION = 12_000


class AlbumArtRequestError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _validate_album_art_bytes(data: bytes) -> Dict[str, Any]:
    if not data or len(data) < 32:
        raise AlbumArtRequestError("Cover image is empty or too small", 400)
    if len(data) > _ALBUM_ART_UPLOAD_MAX_BYTES:
        raise AlbumArtRequestError("Cover image must be 15 MB or smaller", 400)
    try:
        from PIL import Image, UnidentifiedImageError
        Image.MAX_IMAGE_PIXELS = _ALBUM_ART_MAX_PIXELS
    except Exception as exc:
        raise AlbumArtRequestError("Image validation is unavailable", 500) from exc
    try:
        # formats= restricts Pillow to the decoders for the accepted types, so
        # an attacker-supplied PSD/FITS/GD/McIdas/... payload never reaches
        # its (historically memory-unsafe) parser.
        try:
            image_cm = Image.open(io.BytesIO(data), formats=_ALBUM_ART_PILLOW_FORMATS)
        except UnidentifiedImageError as exc:
            if _sniff_unsupported_image_format(data, ("JPEG", "PNG", "WEBP")):
                raise AlbumArtRequestError("Unsupported image type; use JPEG, PNG, or WebP", 400) from exc
            raise AlbumArtRequestError("Cover image could not be safely decoded", 400) from exc
        with image_cm as image:
            image_format = (image.format or "").upper()
            width, height = image.size
            frames = int(getattr(image, "n_frames", 1) or 1)
            if image_format not in {"JPEG", "PNG", "WEBP"}:
                raise AlbumArtRequestError("Unsupported image type; use JPEG, PNG, or WebP", 400)
            if frames != 1 or bool(getattr(image, "is_animated", False)):
                raise AlbumArtRequestError("Animated artwork is not supported", 400)
            if width < 1 or height < 1:
                raise AlbumArtRequestError("Image dimensions are invalid", 400)
            if width > _ALBUM_ART_MAX_DIMENSION or height > _ALBUM_ART_MAX_DIMENSION:
                raise AlbumArtRequestError("Image dimensions exceed the limit", 400)
            if width * height > _ALBUM_ART_MAX_PIXELS:
                raise AlbumArtRequestError("Image pixel count exceeds the limit", 400)
            image.verify()
    except AlbumArtRequestError:
        raise
    except Exception as exc:
        raise AlbumArtRequestError("Cover image could not be safely decoded", 400) from exc
    return {"format": image_format, "width": width, "height": height, "bytes": len(data)}


def _album_art_download_error(exc: BaseException) -> "AlbumArtRequestError":
    """Say why the image could not be fetched (IA-20): the image host's own
    refusal is the caller's to fix (4xx); an unreachable or failing host is
    a gateway problem (502/504), and a throttling host asks to retry (503)."""
    err = provider_boundary.classify_exception(exc)
    outcome = err.outcome
    if outcome == provider_boundary.ProviderOutcome.RATE_LIMITED:
        return AlbumArtRequestError("The image host is rate limiting requests; try again later", 503)
    if outcome == provider_boundary.ProviderOutcome.AUTHENTICATION_ERROR:
        return AlbumArtRequestError(f"The image host refused access (HTTP {err.status_code})", 400)
    if err.status_code == 404:
        return AlbumArtRequestError("No image at that URL (HTTP 404)", 400)
    if outcome == provider_boundary.ProviderOutcome.REJECTED and err.status_code is not None:
        return AlbumArtRequestError(f"The image host refused the request (HTTP {err.status_code})", 400)
    if outcome == provider_boundary.ProviderOutcome.TRANSIENT_ERROR and "timed out" in str(err):
        return AlbumArtRequestError("The image host did not respond in time", 504)
    if outcome == provider_boundary.ProviderOutcome.UNAVAILABLE and err.status_code is not None:
        return AlbumArtRequestError(f"The image host is unavailable (HTTP {err.status_code})", 502)
    return AlbumArtRequestError("Could not reach the image host", 502)


def _download_album_art_bytes(image_url: str) -> Tuple[bytes, Dict[str, Any]]:
    parsed = urllib.parse.urlparse(image_url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AlbumArtRequestError("Valid http(s) image URL required", 400)
    try:
        resolve_public_target(image_url)
    except OutboundPolicyError as exc:
        raise AlbumArtRequestError("Image URL is not allowed", 400) from exc
    try:
        with provider_boundary.opened_public("artwork", image_url, timeout=15, headers=_IMAGE_FETCH_HEADERS) as resp:
            data = resp.read(_ALBUM_ART_UPLOAD_MAX_BYTES + 1)
    except OutboundPolicyError as exc:
        raise AlbumArtRequestError("Image URL is not allowed", 400) from exc
    except Exception as exc:
        raise _album_art_download_error(exc) from exc
    info = _validate_album_art_bytes(data)
    return data, info


def _album_art_path_text(value: Any) -> str:
    return _s(value).strip().replace("\\", "/").rstrip("/")


def _album_art_album_id(album) -> int:
    try:
        return int(getattr(album, "id", 0) or 0)
    except Exception:
        return 0


def _resolve_album_art_request_album(body: Dict[str, Any]):
    raw_id = body.get("album_id") or body.get("albumId") or body.get("aid")
    if raw_id not in (None, ""):
        try:
            aid = int(raw_id)
        except Exception as exc:
            raise AlbumArtRequestError("Invalid album ID", 400) from exc
        album = lib.get_album(aid)
        if not album:
            raise AlbumArtRequestError("Album not found", 404)
        return aid, album

    artist = _s(body.get("artist") or "").strip().casefold()
    album_name = _s(body.get("album") or "").strip().casefold()
    supplied_dir = _album_art_path_text(body.get("aldir") or "")
    if not (artist or album_name):
        raise AlbumArtRequestError("album_id or artist/album is required", 400)
    matches = []
    for candidate in lib.albums([]):
        cand_artist = _s(getattr(candidate, "albumartist", "") or getattr(candidate, "artist", "")).strip().casefold()
        cand_album = _s(getattr(candidate, "album", "") or "").strip().casefold()
        if artist and cand_artist != artist:
            continue
        if album_name and cand_album != album_name:
            continue
        if supplied_dir:
            trusted_dir = _album_dir_for_art(candidate)
            if not trusted_dir or _album_art_path_text(str(trusted_dir)) != supplied_dir:
                continue
        aid = _album_art_album_id(candidate)
        if aid:
            matches.append((aid, candidate))
    if len(matches) != 1:
        raise AlbumArtRequestError("A unique album_id is required for artwork updates", 400)
    return matches[0]


def _album_art_expected_release_group(album) -> str:
    return _s(getattr(album, "mb_releasegroupid", "") or "").strip().lower()


def _replace_album_art_bytes(album_id: int, data: bytes, *, source: str,
                             expected_mb_releasegroupid: str = "",
                             log: Optional[List[str]] = None) -> Dict[str, Any]:
    _validate_album_art_bytes(data)
    encoded = base64.b64encode(data).decode("ascii")
    try:
        result = composite_workflows.replace_album_art(
            album_id,
            encoded,
            source=source,
            expected_mb_releasegroupid=expected_mb_releasegroupid,
        )
    except Exception as exc:
        raise RuntimeError("Could not update album artwork") from exc
    if not result.get("ok"):
        raise RuntimeError("Could not update album artwork")
    if log is not None:
        log.append(f"Saved cover art: {Path(_s(result.get('artpath') or '')).name or 'albumart.jpg'}")
    return result


def _replace_album_art_from_url(album_id: int, image_url: str, *, source: str,
                                expected_mb_releasegroupid: str = "",
                                log: Optional[List[str]] = None) -> Dict[str, Any]:
    data, _info = _download_album_art_bytes(image_url)
    return _replace_album_art_bytes(
        album_id,
        data,
        source=source,
        expected_mb_releasegroupid=expected_mb_releasegroupid,
        log=log,
    )


def _save_art_to_disk(image_url: str, album_id: int, *, expected_mb_releasegroupid: str = "",
                      source: str = "discogs", log: Optional[List[str]] = None) -> str:
    """Compatibility wrapper: download art and delegate the write to the Beets engine."""
    try:
        result = _replace_album_art_from_url(
            int(album_id),
            image_url,
            source=source,
            expected_mb_releasegroupid=expected_mb_releasegroupid,
            log=log,
        )
        return _s(result.get("artpath") or "")
    except Exception:
        return ""


def _fetch_artwork_after_retag(aid: int, mb_albumid: str, log: List[str],
                               cancel_event=None) -> Dict[str, Any]:
    """Verify the persisted MusicBrainz identity, then fetch artwork.

    An as-is import can't be relied on for artwork -- it initially lacks the
    finalized MusicBrainz identity fetchart's sources search against. Only
    run it once mbsync/write/move/recording-ID repair have all completed
    and the album's persisted mb_albumid actually matches what this job
    just imported; reuses the same single-album repair path as the manual
    Album Art Repair retry action (POST /api/albums/<aid>/fetch-art), which
    already verifies actual on-disk art after the fetch, not just rc==0.

    Never raises for an artwork failure -- artwork is optional and must not
    undo a successfully imported and verified album -- except to propagate
    a genuine job cancellation.
    """
    identity_verified = False
    try:
        verify_album = lib.get_album(int(aid))
        identity_verified = bool(
            verify_album
            and _s(getattr(verify_album, "mb_albumid", "")).strip().lower() == mb_albumid.strip().lower()
        )
    except Exception as ex:
        log.append(f"  [identity] verification warning: {_redact_security_text(ex)}")

    artwork_status = "skipped_identity_unverified"
    artwork_retryable = False
    if identity_verified:
        try:
            art_result = _repair_album_art(int(aid), log, cancel_event=cancel_event)
        except Exception as ex:
            art_result = {"status": "failed", "error": _s(ex)}
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("cancelled")
        art_status = art_result.get("status")
        if art_status == "saved":
            artwork_status = "fetched"
            saved_path = _s(art_result.get("saved_path") or "")
            log.append(f"  Artwork: fetched{f' ({Path(saved_path).name})' if saved_path else ''}.")
        elif art_status == "skipped":
            artwork_status = "already_present"
            log.append("  Artwork: already present.")
        else:
            artwork_status = "failed"
            artwork_retryable = True
            reason = _redact_security_text(art_result.get("error") or "no art found")
            log.append(f"  Artwork: failed — {reason}. Retry artwork from Album Art Repair.")
    else:
        log.append("  Artwork: skipped — MusicBrainz identity was not verified as persisted.")

    return {
        "identity_verified": identity_verified,
        "artwork_status": artwork_status,
        "artwork_retryable": artwork_retryable,
    }


def _get_album_item_dir(album: Any) -> str:
    """Safely extract directory path from native Beets Album, RemoteAlbum, or dict."""
    if album is None:
        return ""
    val = getattr(album, "item_dir", None) if not isinstance(album, dict) else album.get("item_dir")
    if callable(val):
        try:
            text = _s(val())
        except ValueError:
            text = ""
        if text:
            return text
    elif val is not None:
        text = _s(val)
        if text:
            return text
    if isinstance(album, dict):
        return _s(album.get("path", ""))
    return _s(getattr(album, "path", ""))
