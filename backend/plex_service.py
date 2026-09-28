"""Plex provider: settings, requests, library refresh and playlist sync (ARCH-001).
"""

from __future__ import annotations

import json, math, os, re, socket, threading, time, unicodedata, uuid
import urllib.error, urllib.parse, urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from backend.app_runtime import _app_logger, CONFIG_FILE, DOWNLOADS_ROOT, MUSIC_ROOT, PLAYLIST_DOWNLOAD_ROOT, PLAYLIST_PATH_ROOT_ALIASES, PLEX_API_TIMEOUT, PLEX_INDEX_CACHE_TTL, PLEX_INDEX_PAGE_SIZE, PLEX_INDEX_TIMEOUT, PLEX_SCAN_TIMEOUT, PLEX_SYNC_MAX_FALLBACK_SEARCHES, PLEX_TOKEN, PLEX_URL, WEB_MANAGER_DATA_DIR, _MB_UUID_RE, _s
from backend.app_runtime import _norm, _path_is_under

# ── ARCH-001 extracted code ──


# Deliberately absolute, deliberately never a real filesystem location in
# any supported deployment: the safe "could not resolve to an authorized
# path" return value for _playlist_resolve_item_path(). Every call site
# treats it as any other Path (SEC-002 Wave 9 final review found 11 call
# sites; changing the return type to Optional[Path] would ripple across
# all of them), but since it is guaranteed to never .exists() and is
# guaranteed to fail containment under MUSIC_ROOT/any allowed root, every
# caller's own existence/containment check already fails closed on it --
# without inventing a plausible-looking library path from attacker input
# the way the previous MUSIC_ROOT / path.name fallback did.
_PLAYLIST_UNRESOLVED_PATH = Path("/nonexistent/beets-web-manager-unresolved-playlist-path")


def _playlist_resolve_item_path(path_value: Any) -> Path:
    raw = _s(path_value).strip()
    if not raw:
        return _PLAYLIST_UNRESOLVED_PATH
    path = Path(raw)
    allowed_roots = [
        MUSIC_ROOT.resolve(strict=False),
        PLAYLIST_DOWNLOAD_ROOT.resolve(strict=False),
        DOWNLOADS_ROOT.resolve(strict=False),
    ]
    for alias in PLAYLIST_PATH_ROOT_ALIASES:
        try:
            allowed_roots.append(Path(alias).resolve(strict=False))
        except Exception:
            pass
    if path.is_absolute():
        try:
            resolved = path.resolve(strict=False)
            for root in allowed_roots:
                try:
                    resolved.relative_to(root)
                    return path
                except ValueError:
                    pass
        except Exception:
            pass
        # Unauthorized absolute input (e.g. /etc/passwd, or a real file
        # under an unrelated root such as /data/media/music2) must not be
        # silently reinterpreted as MUSIC_ROOT / path.name -- that invents
        # a new, different, plausible-looking library path from attacker
        # input instead of rejecting it (SEC-002 Wave 9 final review).
        return _PLAYLIST_UNRESOLVED_PATH
    # Relative input: join under MUSIC_ROOT (the historical, intended
    # behavior for a library-relative path), but verify the *resolved*
    # result still lands under MUSIC_ROOT before returning it -- a
    # relative traversal string such as "../../../etc/passwd" would
    # otherwise resolve outside MUSIC_ROOT the moment any caller calls
    # .resolve()/.exists() on the returned path.
    candidate = MUSIC_ROOT / raw
    try:
        candidate.resolve(strict=False).relative_to(MUSIC_ROOT.resolve(strict=False))
    except Exception:
        return _PLAYLIST_UNRESOLVED_PATH
    return candidate


_PLEX_SECTION_CACHE: Dict[str, Any] = {"at": 0.0, "key": None, "value": None}


_PLEX_TRACK_INDEX_CACHE: Dict[str, Any] = {"at": 0.0, "key": None, "value": None}


def _plex_config_from_beets() -> Dict[str, str]:
    """Read the small Plex stanza from config.yaml without requiring PyYAML."""
    vals: Dict[str, str] = {}
    try:
        lines = Path(CONFIG_FILE).read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return vals

    in_plex = False
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if raw and not raw[0].isspace():
            if line == "plex:":
                in_plex = True
                continue
            if in_plex:
                break
        if not in_plex or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            vals[key] = value

    host = vals.get("url") or vals.get("base_url") or vals.get("host") or ""
    port = vals.get("port") or "32400"
    secure = vals.get("secure", "").lower() in {"1", "true", "yes", "on"}
    if host and not re.match(r"^https?://", host, re.I):
        host = f"{'https' if secure else 'http'}://{host}"
    if host and ":" not in urllib.parse.urlparse(host).netloc and port:
        host = f"{host}:{port}"
    if host:
        vals["url"] = host.rstrip("/")
    return vals


def _plex_settings() -> Dict[str, str]:
    cfg = _plex_config_from_beets()
    url = (os.environ.get("PLEX_URL") or cfg.get("plex_url") or cfg.get("url") or PLEX_URL or "").rstrip("/")
    token = os.environ.get("PLEX_TOKEN") or cfg.get("plex_token") or cfg.get("token") or PLEX_TOKEN or ""
    section = (
        os.environ.get("PLEX_MUSIC_SECTION")
        or os.environ.get("PLEX_SECTION_KEY")
        or cfg.get("plex_music_section")
        or cfg.get("section_key")
        or cfg.get("section")
        or cfg.get("library_name")
        or cfg.get("library")
        or ""
    )
    plex_roots = (
        os.environ.get("PLEX_MUSIC_ROOTS")
        or os.environ.get("PLEX_MUSIC_ROOT")
        or cfg.get("plex_music_roots")
        or cfg.get("plex_music_root")
        or ""
    )
    beets_root = (
        os.environ.get("PLEX_BEETS_MUSIC_ROOT")
        or os.environ.get("BEETS_MUSIC_ROOT")
        or cfg.get("beets_music_root")
        or str(MUSIC_ROOT)
    )
    scan_timeout = (
        os.environ.get("PLEX_SCAN_TIMEOUT")
        or cfg.get("plex_scan_timeout")
        or str(PLEX_SCAN_TIMEOUT)
    )
    index_timeout = (
        os.environ.get("PLEX_INDEX_TIMEOUT")
        or cfg.get("plex_index_timeout")
        or str(PLEX_INDEX_TIMEOUT)
    )
    return {"url": url, "token": token, "section": section,
            "plex_music_roots": plex_roots, "beets_music_root": beets_root,
            "plex_scan_timeout": scan_timeout, "plex_index_timeout": index_timeout}


def _plex_split_roots(value: str) -> List[str]:
    out: List[str] = []
    for raw in _s(value).split(","):
        root = raw.strip().replace("\\", "/").rstrip("/")
        if root and root not in out:
            out.append(root)
    return out


def _plex_music_roots(settings: Optional[Dict[str, str]] = None) -> List[str]:
    settings = settings or _plex_settings()
    roots = _plex_split_roots(settings.get("plex_music_roots") or "")
    for root in list(PLAYLIST_PATH_ROOT_ALIASES) + [str(MUSIC_ROOT), "/data/media/music", "/music"]:
        normalized = _s(root).strip().replace("\\", "/").rstrip("/")
        if normalized and normalized not in roots:
            roots.append(normalized)
    return roots


def _plex_beets_music_root(settings: Optional[Dict[str, str]] = None) -> str:
    settings = settings or _plex_settings()
    return _s(settings.get("beets_music_root") or str(MUSIC_ROOT)).replace("\\", "/").rstrip("/")


def _plex_norm_path(value: Any) -> str:
    raw = urllib.parse.unquote(_s(value).strip()).replace("\\", "/")
    if not raw:
        return ""
    raw = unicodedata.normalize("NFC", raw)
    normalized = re.sub(r"/+", "/", raw)
    prefix = "/" if normalized.startswith("/") else ""
    parts: List[str] = []
    for part in normalized.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    normalized = prefix + "/".join(parts)
    if not normalized and prefix:
        normalized = prefix
    return normalized.rstrip("/") if len(normalized) > 1 else normalized


def _plex_path_case_key(value: Any) -> str:
    return _plex_norm_path(value).casefold()


def _plex_path_is_under(path_value: Any, root_value: Any) -> bool:
    path = _plex_norm_path(path_value)
    root = _plex_norm_path(root_value)
    return bool(root and (path.casefold() == root.casefold()
                          or path.casefold().startswith(root.casefold() + "/")))


def _plex_relative_path(path_value: Any, roots: Iterable[str]) -> str:
    path = _plex_norm_path(path_value)
    if not path:
        return ""
    for root in roots or []:
        root_norm = _plex_norm_path(root)
        if _plex_path_is_under(path, root_norm):
            return path[len(root_norm):].lstrip("/")
    if not Path(path).is_absolute():
        return path.lstrip("/")
    return ""


def _plex_effective_music_roots(settings: Optional[Dict[str, str]] = None,
                                section_locations: Optional[Iterable[str]] = None) -> List[str]:
    settings = settings or _plex_settings()
    roots: List[str] = []

    def add(value: Any) -> None:
        normalized = _plex_norm_path(value)
        if normalized and normalized not in roots:
            roots.append(normalized)

    for root in section_locations or []:
        add(root)
    for root in _plex_music_roots(settings):
        add(root)
    return roots


def _plex_selected_path_map(settings: Optional[Dict[str, str]] = None,
                            section_locations: Optional[Iterable[str]] = None) -> Dict[str, str]:
    settings = settings or _plex_settings()
    beets_root = _plex_beets_music_root(settings)
    roots = _plex_effective_music_roots(settings, section_locations)
    target = ""
    location_roots = [_plex_norm_path(root) for root in (section_locations or []) if _plex_norm_path(root)]
    target = next((root for root in location_roots
                   if _plex_path_case_key(root) != _plex_path_case_key(beets_root)), "")
    if not target:
        target = next((root for root in roots
                       if _plex_path_case_key(root) != _plex_path_case_key(beets_root)), "")
    return {"beets_root": beets_root, "plex_root": target or beets_root}


def _plex_translate_beets_path(path_value: Any,
                               settings: Optional[Dict[str, str]] = None,
                               plex_roots: Optional[Iterable[str]] = None,
                               section_locations: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    raw = _s(path_value).strip()
    if not raw or not _plex_is_final_library_path(raw):
        return {
            "source_path": raw,
            "relative_path": "",
            "translated_path": "",
            "candidates": [],
            "local_exists": False,
        }
    settings = settings or _plex_settings()
    candidates: List[str] = []

    def add(value: Any) -> None:
        normalized = _plex_norm_path(value)
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    add(raw)
    resolved_path = None
    try:
        resolved_path = _playlist_resolve_item_path(raw).resolve(strict=False)
        add(str(resolved_path))
    except Exception:
        resolved_path = None

    beets_roots = [_plex_beets_music_root(settings), str(MUSIC_ROOT), "/data/media/music"]
    rel = ""
    for candidate in list(candidates):
        rel = _plex_relative_path(candidate, beets_roots)
        if rel:
            break
    if not rel and not Path(_plex_norm_path(raw)).is_absolute():
        rel = _plex_norm_path(raw).lstrip("/")
    if rel:
        add(rel)
        for root in _plex_effective_music_roots(settings, section_locations or plex_roots or []):
            add(f"{root.rstrip('/')}/{rel.lstrip('/')}")

    translated = ""
    path_map = _plex_selected_path_map(settings, section_locations or plex_roots or [])
    if rel and path_map.get("plex_root"):
        translated = _plex_norm_path(f"{path_map['plex_root'].rstrip('/')}/{rel.lstrip('/')}")

    local_exists = False
    try:
        local_exists = bool(resolved_path and resolved_path.exists())
    except Exception:
        local_exists = False
    return {
        "source_path": raw,
        "relative_path": rel,
        "translated_path": translated,
        "candidates": candidates,
        "local_exists": local_exists,
    }


def _plex_suffix_keys_for_path(path_value: Any, roots: Iterable[str]) -> set:
    normalized = _plex_norm_path(path_value)
    keys = set()
    if not normalized:
        return keys
    rel = _plex_relative_path(normalized, roots)
    if rel:
        keys.add(rel.casefold())
    parts = [part for part in normalized.strip("/").split("/") if part]
    for length in (4, 3, 2):
        if len(parts) >= length:
            keys.add("/".join(parts[-length:]).casefold())
    return keys


def _plex_mapped_beets_paths(path_value: Any,
                             settings: Optional[Dict[str, str]] = None,
                             plex_roots: Optional[Iterable[str]] = None,
                             section_locations: Optional[Iterable[str]] = None) -> List[str]:
    return list(_plex_translate_beets_path(
        path_value,
        settings=settings,
        plex_roots=plex_roots,
        section_locations=section_locations,
    ).get("candidates") or [])


def _plex_timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return True
    text = _s(exc).lower()
    return "timed out" in text or "timeout" in text


_plex_client_identifier_cache: str = ""


def _plex_client_identifier_file() -> Path:
    if os.environ.get("PLEX_CLIENT_IDENTIFIER_FILE", "").strip():
        return Path(os.environ["PLEX_CLIENT_IDENTIFIER_FILE"])
    return WEB_MANAGER_DATA_DIR / ".plex_client_identifier"


def _plex_client_identifier() -> str:
    """Stable, installation-specific Plex client identifier.

    Not a secret -- an opaque, per-installation UUID Plex uses to tell
    this integration's authorized-device entry apart from every other
    client on the account. Generated once and persisted so it survives
    container recreation; a fresh value would otherwise register a new
    "device" on every restart and never let a future rotation be
    isolated the way this one couldn't be.
    """
    global _plex_client_identifier_cache
    if _plex_client_identifier_cache:
        return _plex_client_identifier_cache
    id_file = _plex_client_identifier_file()
    try:
        existing = id_file.read_text(encoding="utf-8").strip()
        if existing:
            _plex_client_identifier_cache = existing
            return existing
    except Exception:
        pass
    generated = uuid.uuid4().hex
    try:
        id_file.parent.mkdir(parents=True, exist_ok=True)
        id_file.write_text(generated, encoding="utf-8")
    except Exception:
        pass  # Best-effort persistence; an in-memory-only id for this process is still correct.
    _plex_client_identifier_cache = generated
    return generated


def _plex_client_headers() -> Dict[str, str]:
    return {
        "X-Plex-Client-Identifier": _plex_client_identifier(),
        "X-Plex-Product": "Beets Web Manager",
        "X-Plex-Device-Name": "beets-web-manager",
        "X-Plex-Version": "0.1.0",
        "X-Plex-Platform": "Docker",
    }


def _plex_request(path: str, params: Optional[Dict[str, Any]] = None,
                  *, method: str = "GET", timeout: Optional[int] = None,
                  attempts: int = 2) -> Dict[str, Any]:
    settings = _plex_settings()
    if not settings["url"]:
        raise RuntimeError("Plex URL is not configured")
    if not settings["token"]:
        raise RuntimeError("Plex token is not configured")
    query = dict(params or {})
    url = f"{settings['url']}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    headers = {"Accept": "application/json", "X-Plex-Token": settings["token"]}
    headers.update(_plex_client_headers())
    req = urllib.request.Request(url, headers=headers, method=method)
    timeout = int(timeout or PLEX_API_TIMEOUT)
    last_exc: Optional[BaseException] = None
    for attempt in range(max(1, int(attempts or 1))):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            break
        except urllib.error.HTTPError:
            raise
        except Exception as exc:
            last_exc = exc
            if not _plex_timeout_error(exc) or attempt >= max(1, int(attempts or 1)) - 1:
                raise
            time.sleep(1.0 + attempt)
    else:
        raise RuntimeError(last_exc or "Plex request failed")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw.decode("utf-8", errors="replace")}


def _plex_machine_identifier() -> str:
    try:
        d = _plex_request("/identity", timeout=8)
        return _s(d.get("MediaContainer", {}).get("machineIdentifier") or "").strip()
    except Exception:
        return ""


def _plex_find_music_section(force: bool = False):
    """Return (machineIdentifier, sectionKey, sectionTitle) for the Plex music library."""
    settings = _plex_settings()
    cache_key = (settings.get("url"), settings.get("section"))
    now = time.time()
    if (not force and _PLEX_SECTION_CACHE.get("key") == cache_key
            and (now - float(_PLEX_SECTION_CACHE.get("at") or 0)) < 300):
        return _PLEX_SECTION_CACHE.get("value")

    d = _plex_request("/library/sections", timeout=8)
    mc = d.get("MediaContainer", {})
    machine_id = _s(mc.get("machineIdentifier") or "").strip() or _plex_machine_identifier()
    preferred = (settings.get("section") or "").strip().lower()
    music_sections = []
    selected = None
    for sec in mc.get("Directory", []):
        if sec.get("type") != "artist":
            continue
        key = str(sec.get("key") or "")
        title = str(sec.get("title") or sec.get("title1") or "")
        music_sections.append((key, title))
        if preferred and preferred in {key.lower(), title.lower()}:
            selected = (key, title)
            break
    if selected is None and music_sections:
        selected = music_sections[0]

    result = (machine_id, selected[0], selected[1]) if selected else (machine_id, None, None)
    _PLEX_SECTION_CACHE.update({"at": now, "key": cache_key, "value": result})
    return result


def _plex_section_locations(section_key: Any) -> List[str]:
    locations: List[str] = []

    def add(value: Any) -> None:
        normalized = _plex_norm_path(value)
        if normalized and normalized not in locations:
            locations.append(normalized)

    try:
        d = _plex_request(f"/library/sections/{section_key}", timeout=10)
    except Exception:
        return locations
    mc = d.get("MediaContainer", {}) if isinstance(d, dict) else {}
    containers = []
    if isinstance(mc, dict):
        containers.append(mc)
        containers.extend([row for row in mc.get("Directory", []) if isinstance(row, dict)])
    for container in containers:
        for loc in container.get("Location") or []:
            if isinstance(loc, dict):
                add(loc.get("path") or loc.get("title"))
            else:
                add(loc)
    if locations:
        return locations
    try:
        d = _plex_request("/library/sections", timeout=10)
    except Exception:
        return locations
    mc = d.get("MediaContainer", {}) if isinstance(d, dict) else {}
    for sec in mc.get("Directory", []) if isinstance(mc, dict) else []:
        if not isinstance(sec, dict) or _s(sec.get("key")) != _s(section_key):
            continue
        for loc in sec.get("Location") or []:
            if isinstance(loc, dict):
                add(loc.get("path") or loc.get("title"))
            else:
                add(loc)
    return locations


_ALLOWED_PLEX_REFRESH_WORKFLOWS = {"batch", "playlist", "manual"}


def _trigger_plex_refresh(log: list, *, workflow: str = "") -> bool:
    """Directly call the Plex library refresh API for approved workflows."""
    workflow_key = _s(workflow).strip().casefold()
    if workflow_key not in _ALLOWED_PLEX_REFRESH_WORKFLOWS:
        if log is not None:
            log.append("  [plex] Refresh skipped; automatic scans run only for playlist and batch jobs.")
        return False
    settings = _plex_settings()
    if not settings.get("url") or not settings.get("token"):
        return False
    result: Dict[str, Any] = {"done": False, "ok": False, "message": ""}

    def _worker() -> None:
        try:
            _, section_key, section_title = _plex_find_music_section()
            if not section_key:
                result.update({
                    "ok": False,
                    "message": "  [plex] No Plex music library section found",
                })
                return
            _plex_request(f"/library/sections/{section_key}/refresh", timeout=10)
            label = section_title or section_key
            result.update({"ok": True, "message": f"  [plex] Refresh triggered ({label})"})
        except Exception as ex:
            result.update({"ok": False, "message": f"  [plex] {ex}"})
        finally:
            result["done"] = True

    thread = threading.Thread(target=_worker, name="plex-refresh", daemon=True)
    thread.start()
    thread.join(12)
    if not result.get("done"):
        log.append("  [plex] Refresh request timed out; continuing without blocking job")
        return False
    if result.get("message"):
        log.append(result["message"])
    return bool(result.get("ok"))


def _plex_is_final_library_path(path_value: Any) -> bool:
    raw = _s(path_value).strip()
    if not raw:
        return False
    normalized = raw.replace("\\", "/").casefold()
    staging_roots = [
        str(PLAYLIST_DOWNLOAD_ROOT),
        str(DOWNLOADS_ROOT),
        "/data/torrents",
        "/data/downloads",
    ]
    for root in staging_roots:
        root_norm = _s(root).replace("\\", "/").rstrip("/").casefold()
        if root_norm and (normalized == root_norm or normalized.startswith(root_norm + "/")):
            return False
    try:
        path = _playlist_resolve_item_path(raw).resolve(strict=False)
        if _path_is_under(path, MUSIC_ROOT.resolve(strict=False)):
            return True
    except Exception:
        pass
    return not Path(raw).is_absolute()


def _plex_beets_path_candidates(path_value: Any,
                                settings: Optional[Dict[str, str]] = None) -> List[str]:
    raw = _s(path_value).strip()
    if not raw or not _plex_is_final_library_path(raw):
        return []
    settings = settings or _plex_settings()
    normalized = raw.replace("\\", "/")
    candidates: List[str] = []

    def add(value: str) -> None:
        value = _s(value).strip().replace("\\", "/")
        if value and value not in candidates:
            candidates.append(value)

    add(normalized)
    try:
        resolved = _playlist_resolve_item_path(raw).resolve(strict=False)
        add(str(resolved))
    except Exception:
        resolved = None

    rel = ""
    roots = [_plex_beets_music_root(settings), str(MUSIC_ROOT), "/data/media/music"]
    for root in roots:
        root_norm = _s(root).replace("\\", "/").rstrip("/")
        for value in list(candidates):
            value_norm = value.replace("\\", "/")
            if root_norm and value_norm.casefold().startswith(root_norm.casefold() + "/"):
                rel = value_norm[len(root_norm) + 1:]
                break
        if rel:
            break
    if not rel and not Path(normalized).is_absolute():
        rel = normalized.lstrip("/")
    if rel:
        add(rel)
        for plex_root in _plex_music_roots(settings):
            add(f"{plex_root.rstrip('/')}/{rel.lstrip('/')}")
    return candidates


def _plex_path_keys_for_beets_item(item: Dict[str, Any],
                                   settings: Optional[Dict[str, str]] = None) -> set:
    keys = set()
    for candidate in _plex_beets_path_candidates(item.get("path", ""), settings):
        keys.update(_playlist_path_keys(candidate))
    return keys


def _plex_path_keys_for_plex_file(path_value: Any,
                                  settings: Optional[Dict[str, str]] = None) -> set:
    settings = settings or _plex_settings()
    keys = set(_playlist_path_keys(_s(path_value)))
    value = _s(path_value).replace("\\", "/")
    for root in _plex_music_roots(settings):
        root_norm = root.rstrip("/")
        if root_norm and value.casefold().startswith(root_norm.casefold() + "/"):
            rel = value[len(root_norm) + 1:]
            keys.update(_playlist_path_keys(rel))
            for candidate_root in _plex_music_roots(settings):
                keys.update(_playlist_path_keys(f"{candidate_root.rstrip('/')}/{rel}"))
    return keys


def _plex_track_part_paths(track: Dict[str, Any]) -> List[str]:
    paths: List[str] = []
    for media in track.get("Media") or []:
        for part in media.get("Part") or []:
            file_path = _plex_norm_path(part.get("file") or "")
            if file_path and file_path not in paths:
                paths.append(file_path)
    return paths


def _plex_duration_seconds(value: Any, *, plex_ms: bool = False) -> int:
    try:
        numeric = float(value or 0)
    except Exception:
        return 0
    if numeric <= 0:
        return 0
    if plex_ms or numeric > 10000:
        numeric = numeric / 1000.0
    return int(round(numeric))


def _plex_track_duration(track: Dict[str, Any]) -> int:
    duration = _plex_duration_seconds(track.get("duration"), plex_ms=True)
    if duration:
        return duration
    for media in track.get("Media") or []:
        duration = _plex_duration_seconds(media.get("duration"), plex_ms=True)
        if duration:
            return duration
    return 0


def _plex_item_duration(item: Dict[str, Any]) -> int:
    return _plex_duration_seconds(item.get("length") or item.get("duration"))


def _plex_duration_values(seconds: int) -> List[int]:
    seconds = int(seconds or 0)
    return [seconds + delta for delta in (-1, 0, 1) if seconds + delta > 0]


def _plex_track_mbids(track: Dict[str, Any]) -> set:
    out = set()
    for raw in [track.get("guid")] + [g.get("id") for g in track.get("Guid") or [] if isinstance(g, dict)]:
        for match in re.findall(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", _s(raw)):
            out.add(match.lower())
    return out


def _plex_item_lookup_keys(item: Dict[str, Any]) -> List[Tuple[Any, ...]]:
    keys: List[Tuple[Any, ...]] = []
    duration = _plex_item_duration(item)
    file_name = Path(_s(item.get("path") or "")).name.casefold()
    if file_name and duration:
        for value in _plex_duration_values(duration):
            keys.append(("filename_duration", file_name, value))
    mbid = _s(item.get("mb_trackid") or "").strip().lower()
    if mbid and _MB_UUID_RE.match(mbid):
        keys.append(("mbid", mbid))
    artist_key = _norm(item.get("artist", ""))
    title_key = _norm(item.get("title", ""))
    if title_key and duration:
        for value in _plex_duration_values(duration):
            keys.append(("text_duration", artist_key, title_key, value))
    albumartist_key = _norm(item.get("albumartist", "") or item.get("artist", ""))
    album_key = _norm(item.get("album", ""))
    if albumartist_key and album_key and title_key:
        keys.append(("album_title", albumartist_key, album_key, title_key))
    return keys


def _plex_track_lookup_rows(track: Dict[str, Any]) -> List[Tuple[Tuple[Any, ...], str]]:
    rating_key = _s(track.get("ratingKey") or "").strip()
    if not rating_key:
        return []
    rows: List[Tuple[Tuple[Any, ...], str]] = []
    duration = _plex_track_duration(track)
    for file_path in _plex_track_part_paths(track):
        file_name = Path(file_path).name.casefold()
        if file_name and duration:
            for value in _plex_duration_values(duration):
                rows.append((("filename_duration", file_name, value), rating_key))
    for mbid in _plex_track_mbids(track):
        rows.append((("mbid", mbid), rating_key))
    artist_key = _norm(track.get("grandparentTitle") or "")
    title_key = _norm(track.get("title") or "")
    if title_key and duration:
        for value in _plex_duration_values(duration):
            rows.append((("text_duration", artist_key, title_key, value), rating_key))
    albumartist_key = _norm(track.get("grandparentTitle") or "")
    album_key = _norm(track.get("parentTitle") or "")
    if albumartist_key and album_key and title_key:
        rows.append((("album_title", albumartist_key, album_key, title_key), rating_key))
    return rows


def _plex_find_track(section_key, artist, title):
    """Return ratingKey of first Plex track matching artist+title, or None."""
    d = _plex_request(
        f"/library/sections/{section_key}/all",
        {"type": 10, "track.title": title},
        timeout=PLEX_API_TIMEOUT,
        attempts=1,
    )
    for t in d.get("MediaContainer", {}).get("Metadata", []):
        if _norm(t.get("grandparentTitle","")) == _norm(artist) or not artist:
            return t.get("ratingKey")
    return None


def _plex_section_track_index(section_key, log=None, force: bool = False) -> Dict[str, Any]:
    """Build one reusable Plex music index for a sync run."""
    settings = _plex_settings()
    section_locations = _plex_section_locations(section_key)
    roots = tuple(_plex_effective_music_roots(settings, section_locations))
    path_map = _plex_selected_path_map(settings, section_locations)
    index_version = 2
    cache_key = (
        settings.get("url"),
        section_key,
        roots,
        path_map.get("beets_root"),
        path_map.get("plex_root"),
        index_version,
    )
    now = time.time()
    cached = _PLEX_TRACK_INDEX_CACHE.get("value")
    if (not force and cached and _PLEX_TRACK_INDEX_CACHE.get("key") == cache_key
            and cached.get("path_index_version") == index_version
            and isinstance(cached.get("path_exact"), dict)
            and PLEX_INDEX_CACHE_TTL > 0
            and (now - float(_PLEX_TRACK_INDEX_CACHE.get("at") or 0)) < PLEX_INDEX_CACHE_TTL):
        if log is not None:
            log.append(
                f"  [plex] Reused cached Plex path index "
                f"({cached.get('fetched', 0)} tracks)"
            )
            cached_map = cached.get("path_map") if isinstance(cached.get("path_map"), dict) else {}
            log.append(f"  [plex] Plex locations: {', '.join(cached.get('section_locations') or []) or '(none)'}")
            log.append(
                f"  [plex] Using path map: {cached_map.get('beets_root') or _plex_beets_music_root(settings)} -> "
                f"{cached_map.get('plex_root') or '(none)'}"
            )
        return dict(cached)

    lookup: Dict[tuple, str] = {}
    tracks_by_key: Dict[str, Dict[str, Any]] = {}
    path_exact: Dict[str, str] = {}
    path_case: Dict[str, str] = {}
    suffix_paths: Dict[str, set] = {}
    filename_duration: Dict[tuple, set] = {}
    text_duration: Dict[tuple, set] = {}
    start = 0
    size = PLEX_INDEX_PAGE_SIZE
    fetched = 0
    total = 0
    loops = 0
    path_count = 0
    text_count = 0
    started = time.time()
    try:
        index_timeout = int(settings.get("plex_index_timeout") or PLEX_INDEX_TIMEOUT)
    except Exception:
        index_timeout = PLEX_INDEX_TIMEOUT
    while True:
        loops += 1
        d = _plex_request_with_wall_timeout(
            f"/library/sections/{section_key}/all",
            {
                "type": 10,
                "X-Plex-Container-Start": start,
                "X-Plex-Container-Size": size,
            },
            timeout=PLEX_API_TIMEOUT,
            wall_timeout=index_timeout,
        )
        mc = d.get("MediaContainer", {})
        tracks = mc.get("Metadata", []) or []
        if not total:
            try:
                total = int(mc.get("totalSize") or mc.get("TotalSize") or 0)
            except Exception:
                total = 0
        for track in tracks:
            rating_key = _s(track.get("ratingKey") or "").strip()
            if not rating_key:
                continue
            part_paths = _plex_track_part_paths(track)
            tracks_by_key.setdefault(rating_key, {
                "ratingKey": rating_key,
                "title": _s(track.get("title") or ""),
                "artist": _s(track.get("grandparentTitle") or ""),
                "album": _s(track.get("parentTitle") or ""),
                "duration": _plex_track_duration(track),
                "paths": part_paths,
            })
            duration = _plex_track_duration(track)
            for file_path in part_paths:
                normalized_path = _plex_norm_path(file_path)
                if normalized_path:
                    path_exact.setdefault(normalized_path, rating_key)
                    path_case.setdefault(normalized_path.casefold(), rating_key)
                    for suffix in _plex_suffix_keys_for_path(normalized_path, roots):
                        suffix_paths.setdefault(suffix, set()).add(rating_key)
                file_name = Path(normalized_path).name.casefold() if normalized_path else ""
                if file_name and duration:
                    for value in _plex_duration_values(duration):
                        filename_duration.setdefault((file_name, value), set()).add(rating_key)
                for path_key in _plex_path_keys_for_plex_file(file_path, settings):
                    lookup.setdefault(("path", path_key), rating_key)
                    path_count += 1
            for index_key, value in _plex_track_lookup_rows(track):
                lookup.setdefault(index_key, value)
                if index_key and index_key[0] == "filename_duration":
                    filename_duration.setdefault(index_key[1:], set()).add(value)
                elif index_key and index_key[0] == "text_duration":
                    text_duration.setdefault(index_key[1:], set()).add(value)
                text_count += 1
            artist_key = _norm(track.get("grandparentTitle") or "")
            title_key = _norm(track.get("title") or "")
            if title_key:
                lookup.setdefault(("text", artist_key, title_key), rating_key)
                text_count += 1
            if title_key and duration:
                for value in _plex_duration_values(duration):
                    text_duration.setdefault((artist_key, title_key, value), set()).add(rating_key)
        fetched += len(tracks)
        if not tracks:
            break
        start += len(tracks)
        if total and start >= total:
            break
        if not total and len(tracks) < size:
            break
        if loops >= 100:
            break
    index = {
        "lookup": lookup,
        "status": "ready",
        "fetched": fetched,
        "total": total,
        "path_keys": path_count,
        "fallback_keys": text_count,
        "duration": round(time.time() - started, 2),
        "section_key": section_key,
        "path_index_version": index_version,
        "beets_music_root": _plex_beets_music_root(settings),
        "path_map": path_map,
        "plex_music_roots": list(roots),
        "section_locations": list(section_locations),
        "tracks_by_key": tracks_by_key,
        "path_exact": path_exact,
        "path_case": path_case,
        "suffix_paths": suffix_paths,
        "filename_duration": filename_duration,
        "text_duration": text_duration,
        "sample_plex_paths": [
            path for row in tracks_by_key.values()
            for path in (row.get("paths") or [])[:1]
        ][:5],
    }
    _PLEX_TRACK_INDEX_CACHE.update({"at": time.time(), "key": cache_key, "value": dict(index)})
    if log is not None:
        log.append(f"  [plex] Beets root: {_plex_beets_music_root(settings)}")
        log.append(f"  [plex] Plex section: {section_key}")
        log.append(f"  [plex] Plex locations: {', '.join(section_locations) or '(none)'}")
        log.append(
            f"  [plex] Using path map: {path_map.get('beets_root') or '(none)'} -> "
            f"{path_map.get('plex_root') or '(none)'}"
        )
        log.append(
            f"  [plex] Indexed {fetched} Plex track(s) for playlist matching "
            f"({path_count} path keys, {text_count} fallback keys)"
        )
        log.append(
            f"  [plex] Path mapping: {_plex_beets_music_root(settings)} -> "
            f"{', '.join(roots) or '(none)'}"
        )
    return index


def _plex_section_track_lookup(section_key, log=None) -> Dict[tuple, str]:
    """Compatibility wrapper: return the cached Plex lookup map."""
    return _plex_section_track_index(section_key, log=log).get("lookup", {})


def _plex_request_with_wall_timeout(path: str,
                                    params: Optional[Dict[str, Any]] = None,
                                    *,
                                    timeout: int,
                                    wall_timeout: int) -> Dict[str, Any]:
    box: Dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["result"] = _plex_request(path, params, timeout=timeout, attempts=1)
        except Exception as exc:
            box["error"] = exc

    thread = threading.Thread(target=_worker, name="plex-request-timeout", daemon=True)
    thread.start()
    thread.join(max(1, int(wall_timeout or timeout)))
    if thread.is_alive():
        raise TimeoutError(f"Plex request timed out after {wall_timeout}s")
    if box.get("error"):
        raise box["error"]
    return box.get("result") or {}


def _plex_unique_index_value(mapping: Dict[Any, set], key: Any) -> str:
    values = mapping.get(key) or set()
    if len(values) == 1:
        return next(iter(values))
    return ""


def _plex_pending_match_reason(item: Dict[str, Any],
                               translation: Dict[str, Any],
                               index_info: Dict[str, Any],
                               preset: str = "") -> str:
    if preset:
        return preset
    raw_path = _s(item.get("path") or "").strip()
    if not raw_path:
        return "local path missing"
    if not _plex_is_final_library_path(raw_path):
        return "local file is not in the final music library"
    if not translation.get("local_exists"):
        return "local file no longer exists"
    if not translation.get("relative_path") or not translation.get("translated_path"):
        return "path could not be translated"
    path_exact = index_info.get("path_exact", {}) or {}
    path_case = index_info.get("path_case", {}) or {}
    candidates = translation.get("candidates") or []
    if not any(_plex_norm_path(path) in path_exact or _plex_path_case_key(path) in path_case for path in candidates):
        if _s(index_info.get("status") or "") in {"failed", "timeout"}:
            return "Plex database/API returned no matching media item"
        return "file exists but Plex has not indexed it"
    mbid = _s(item.get("mb_trackid") or "").strip()
    if mbid:
        return "MusicBrainz recording ID mismatch"
    if _s(item.get("artist") or "").strip() or _s(item.get("title") or "").strip():
        return "artist/title metadata mismatch"
    return "Plex database/API returned no matching media item"


def _plex_pending_match_row(item: Dict[str, Any],
                            translation: Dict[str, Any],
                            index_info: Dict[str, Any],
                            reason: str = "") -> Dict[str, Any]:
    return {
        "local_track_id": _playlist_status_id(item),
        "artist": _s(item.get("artist") or item.get("query_artist") or ""),
        "title": _s(item.get("title") or item.get("query_title") or ""),
        "album": _s(item.get("album") or ""),
        "local_path": _s(item.get("path") or ""),
        "translated_plex_path": _s(translation.get("translated_path") or ""),
        "mb_trackid": _s(item.get("mb_trackid") or item.get("identity_mb_trackid") or ""),
        "mb_releasegroupid": _s(item.get("mb_releasegroupid") or item.get("identity_mb_releasegroupid") or ""),
        "acoustid": _s(item.get("acoustid") or item.get("acoustid_id") or item.get("acoustid_status") or ""),
        "reason": _plex_pending_match_reason(item, translation, index_info, reason),
        "retry_action": "retry_pending_plex_match",
    }


def _plex_track_keys_for_items(section_key, items, log=None, wait_seconds=0, *,
                               return_details: bool = False):
    """Resolve Beets playlist items to Plex ratingKeys, optionally waiting for a scan."""
    keys_by_index: Dict[int, str] = {}
    methods_by_index: Dict[int, str] = {}
    translations_by_index: Dict[int, Dict[str, Any]] = {}
    pending_reason_by_index: Dict[int, str] = {}
    missing_examples: List[Dict[str, str]] = []
    pending_tracks: List[Dict[str, Any]] = []
    final_items = [it for it in (items or []) if _plex_is_final_library_path(it.get("path", ""))]
    deadline = time.time() + max(int(wait_seconds or 0), 0)
    attempt = 0
    index_error = ""
    index_status = "not_run"
    index_info: Dict[str, Any] = {}
    fallback_searches = 0
    max_fallback = PLEX_SYNC_MAX_FALLBACK_SEARCHES
    while True:
        attempt += 1
        lookup: Dict[tuple, str] = {}
        path_exact: Dict[str, str] = {}
        path_case: Dict[str, str] = {}
        suffix_paths: Dict[str, set] = {}
        filename_duration: Dict[tuple, set] = {}
        text_duration: Dict[tuple, set] = {}
        try:
            index_info = _plex_section_track_index(
                section_key, log=log if attempt == 1 else None, force=attempt > 1)
            lookup = index_info.get("lookup", {}) or {}
            path_exact = index_info.get("path_exact", {}) or {}
            path_case = index_info.get("path_case", {}) or {}
            suffix_paths = index_info.get("suffix_paths", {}) or {}
            filename_duration = index_info.get("filename_duration", {}) or {}
            text_duration = index_info.get("text_duration", {}) or {}
            index_status = _s(index_info.get("status") or "ready")
            index_error = ""
        except Exception as exc:
            index_status = "failed"
            index_error = str(exc)
            if log is not None:
                log.append(
                    f"  [plex] Path index unavailable; bounded title search only: {exc}"
                )
        for idx, it in enumerate(items):
            if idx in keys_by_index:
                continue
            if not _plex_is_final_library_path(it.get("path", "")):
                continue
            key = ""
            translation = _plex_translate_beets_path(
                it.get("path", ""),
                plex_roots=index_info.get("plex_music_roots") or [],
                section_locations=index_info.get("section_locations") or [],
            )
            translations_by_index[idx] = translation
            mapped_paths = list(translation.get("candidates") or [])
            for path_value in mapped_paths:
                key = path_exact.get(_plex_norm_path(path_value), "")
                if key:
                    methods_by_index[idx] = "exact_path"
                    break
            if not key:
                for path_value in mapped_paths:
                    key = path_case.get(_plex_path_case_key(path_value), "")
                    if key:
                        methods_by_index[idx] = "case_path"
                        break
            if not key:
                roots = index_info.get("plex_music_roots") or []
                for path_value in mapped_paths:
                    for suffix in _plex_suffix_keys_for_path(path_value, roots):
                        suffix_values = suffix_paths.get(suffix) or set()
                        if len(suffix_values) > 1:
                            pending_reason_by_index.setdefault(idx, "duplicate or ambiguous Plex results")
                        key = _plex_unique_index_value(suffix_paths, suffix)
                        if key:
                            methods_by_index[idx] = "suffix_path"
                            break
                    if key:
                        break
            if not key:
                for path_key in _plex_path_keys_for_beets_item(it):
                    key = lookup.get(("path", path_key), "")
                    if key:
                        methods_by_index[idx] = "path"
                        break
            if not key:
                duration = _plex_item_duration(it)
                file_name = Path(_s(it.get("path") or "")).name.casefold()
                if file_name and duration:
                    for value in _plex_duration_values(duration):
                        duration_values = filename_duration.get((file_name, value)) or set()
                        if len(duration_values) > 1:
                            pending_reason_by_index.setdefault(idx, "duplicate or ambiguous Plex results")
                        key = _plex_unique_index_value(filename_duration, (file_name, value))
                        if key:
                            methods_by_index[idx] = "filename_duration"
                            break
            if not key:
                duration = _plex_item_duration(it)
                artist_key = _norm(it.get("artist", ""))
                title_key = _norm(it.get("title", ""))
                if title_key and duration:
                    for value in _plex_duration_values(duration):
                        duration_values = text_duration.get((artist_key, title_key, value)) or set()
                        if len(duration_values) > 1:
                            pending_reason_by_index.setdefault(idx, "duplicate or ambiguous Plex results")
                        key = _plex_unique_index_value(text_duration, (artist_key, title_key, value))
                        if key:
                            methods_by_index[idx] = "text_duration"
                            break
            if not key:
                for lookup_key in _plex_item_lookup_keys(it):
                    key = lookup.get(lookup_key, "")
                    if key:
                        methods_by_index[idx] = _s(lookup_key[0])
                        break
            if not key:
                artist_key = _norm(it.get("artist", ""))
                title_key = _norm(it.get("title", ""))
                key = lookup.get(("text", artist_key, title_key), "")
                if key:
                    methods_by_index[idx] = "text"
            if not key and fallback_searches < max_fallback:
                fallback_searches += 1
                try:
                    key = _plex_find_track(section_key, it.get("artist", ""), it.get("title", ""))
                    if key:
                        methods_by_index[idx] = "bounded_search"
                except Exception as exc:
                    if _plex_timeout_error(exc):
                        index_status = "timeout"
                        max_fallback = fallback_searches
                    if log is not None and fallback_searches == 1:
                        log.append(f"  [plex] Bounded title search failed: {exc}")
            if key:
                keys_by_index[idx] = str(key)
        if len(keys_by_index) >= len(final_items) or time.time() >= deadline:
            break
        if log is not None:
            log.append(f"  [plex] Waiting for library scan ({len(keys_by_index)}/{len(final_items)} tracks visible)")
        time.sleep(8 if attempt < 4 else 12)
    keys: List[str] = []
    seen_keys = set()
    duplicate_key_count = 0
    for i in range(len(items)):
        key = keys_by_index.get(i)
        if not key:
            continue
        key = str(key)
        if key in seen_keys:
            duplicate_key_count += 1
        else:
            seen_keys.add(key)
        keys.append(key)
    missing_indexes = [
        i for i in range(len(items))
        if i not in keys_by_index and _plex_is_final_library_path(items[i].get("path", ""))
    ]
    for idx in missing_indexes:
        item = items[idx]
        translation = translations_by_index.get(idx) or _plex_translate_beets_path(
            item.get("path", ""),
            plex_roots=index_info.get("plex_music_roots") or [],
            section_locations=index_info.get("section_locations") or [],
        )
        pending_row = _plex_pending_match_row(
            item, translation, index_info, pending_reason_by_index.get(idx, ""))
        pending_tracks.append(pending_row)
        if len(missing_examples) < 25:
            missing_examples.append({
                "artist": _s(item.get("artist") or ""),
                "title": _s(item.get("title") or ""),
                "path": _s(item.get("path") or ""),
                "translated_plex_path": _s(pending_row.get("translated_plex_path") or ""),
                "reason": _s(pending_row.get("reason") or ""),
            })
    method_counts = Counter(methods_by_index.values())
    path_methods = {"exact_path", "case_path", "suffix_path", "path"}
    matched_by_path = sum(method_counts.get(method, 0) for method in path_methods)
    sample_item = next((it for it in final_items if _s(it.get("path") or "").strip()), {})
    sample_beets_path = _s(sample_item.get("path") or "")
    sample_mapped_paths = _plex_mapped_beets_paths(
        sample_beets_path,
        plex_roots=index_info.get("plex_music_roots") or [],
        section_locations=index_info.get("section_locations") or [],
    ) if sample_beets_path else []
    path_map = index_info.get("path_map") if isinstance(index_info.get("path_map"), dict) else {}
    sample_mapped_path = ""
    if sample_mapped_paths:
        plex_root = _s(path_map.get("plex_root") or "")
        sample_mapped_path = next(
            (path for path in sample_mapped_paths if plex_root and _plex_path_is_under(path, plex_root)),
            sample_mapped_paths[0],
        )
    sample_mapped_exists = bool(
        sample_mapped_path
        and _plex_norm_path(sample_mapped_path) in (index_info.get("path_exact", {}) or {})
    )
    mapped_path_hits = 0
    probe_items = final_items[:50]
    for it in probe_items:
        for mapped_path in _plex_mapped_beets_paths(
                it.get("path", ""),
                plex_roots=index_info.get("plex_music_roots") or [],
                section_locations=index_info.get("section_locations") or []):
            if _plex_norm_path(mapped_path) in (index_info.get("path_exact", {}) or {}):
                mapped_path_hits += 1
                break
    probe_required = max(3, min(10, int(math.ceil(len(probe_items) * 0.2)))) if probe_items else 1
    path_match_ratio = (matched_by_path / len(final_items)) if final_items else 0.0
    path_mapping_verified = bool(
        sample_mapped_exists
        or mapped_path_hits >= probe_required
        or path_match_ratio >= 0.5
    )
    details = {
        "index_status": index_status,
        "index_error": index_error,
        "index_tracks": int(index_info.get("fetched") or 0),
        "index_total": int(index_info.get("total") or 0),
        "index_duration": float(index_info.get("duration") or 0),
        "beets_music_root": _s(index_info.get("beets_music_root") or _plex_beets_music_root()),
        "plex_music_roots": list(index_info.get("plex_music_roots") or _plex_music_roots()),
        "plex_library_locations": list(index_info.get("section_locations") or []),
        "path_mapping_used": (
            f"{path_map.get('beets_root') or _plex_beets_music_root()} -> "
            f"{path_map.get('plex_root') or '(none)'}"
        ),
        "path_mapping_verified": path_mapping_verified,
        "sample_beets_path": sample_beets_path,
        "sample_mapped_plex_path": sample_mapped_path,
        "sample_mapped_exists": sample_mapped_exists,
        "mapped_path_probe_hits": mapped_path_hits,
        "playlist_track_count": len(items),
        "visible_in_plex_count": len(keys_by_index),
        "missing_in_plex_count": len(missing_indexes),
        "pending_plex_count": len(pending_tracks),
        "pending_tracks": pending_tracks,
        "matched_track_ids": [_playlist_status_id(items[i]) for i in sorted(keys_by_index.keys())],
        "unique_rating_keys": len(seen_keys),
        "matched_by_path": int(matched_by_path),
        "matched_by_fallback": int(
            sum(count for method, count in method_counts.items()
                if method not in path_methods | {"duplicate"})
        ),
        "matched_by_search": int(method_counts.get("bounded_search", 0)),
        "duplicate_keys": int(duplicate_key_count),
        "fallback_searches": fallback_searches,
        "max_fallback_searches": max_fallback,
        "missing_examples": missing_examples,
        "timed_out": 1 if index_status == "timeout" or _plex_timeout_error(Exception(index_error)) else 0,
    }
    if log is not None:
        log.append(
            f"  [plex] Sample Beets path: {sample_beets_path or '(none)'}"
        )
        log.append(
            f"  [plex] Sample mapped Plex path: {sample_mapped_path or '(none)'} "
            f"({'found' if sample_mapped_exists else 'not found'} in Plex index)"
        )
        log.append(
            f"  [plex] Path probe: {mapped_path_hits}/{len(probe_items)} mapped sample paths found"
        )
        log.append(
            f"  [plex] Playlist track count: {len(items)}; Plex indexed track count: "
            f"{details['index_tracks']}"
        )
        log.append(
            f"  [plex] Matched {len(keys)}/{len(items)} "
            f"({details['matched_by_path']} path, {details['matched_by_fallback']} fallback, "
            f"{len(missing_indexes)} missing)"
        )
        if duplicate_key_count:
            log.append(
                f"  [plex] Preserved {duplicate_key_count} duplicate playlist "
                "entries for repeated tracks"
            )
        if missing_indexes:
            log.append(
                f"  [plex] Missing in Plex: {len(missing_indexes)}; "
                f"fallback searches used {fallback_searches}/{max_fallback}"
            )
            for row in missing_examples[:5]:
                log.append(
                    f"  [plex] Pending Plex match: {row.get('path') or row.get('title') or '(unknown)'} "
                    f"({row.get('reason') or 'unresolved'})"
                )
    if return_details:
        return keys, details
    return keys


def _playlist_path_keys(path_value: str) -> set:
    raw = _s(path_value).strip()
    if not raw:
        return set()
    keys = set()

    def add(value: str) -> None:
        value = _s(value).strip().replace("\\", "/")
        if not value:
            return
        value = re.sub(r"/+", "/", value).strip()
        keys.add(value.casefold())
        keys.add(value.lstrip("/").casefold())

    add(raw)
    try:
        clean_raw = os.path.normpath(raw).replace("\\", "/")
        if os.path.isabs(clean_raw):
            add(clean_raw)
        else:
            joined = os.path.normpath(os.path.join(str(MUSIC_ROOT), clean_raw.lstrip("/"))).replace("\\", "/")
            add(joined)
    except Exception:
        pass

    root_candidates = {
        str(MUSIC_ROOT).replace("\\", "/").rstrip("/").casefold(),
        "/data/media/music",
    }
    for alias in PLAYLIST_PATH_ROOT_ALIASES:
        root_candidates.add(alias.casefold())
    for key in list(keys):
        for root in root_candidates:
            root = root.strip().rstrip("/")
            if root and key.startswith(root + "/"):
                keys.add(key[len(root) + 1:])
        if key.startswith("music/"):
            keys.add(key[len("music/"):])
    return keys


def _plex_track_file(track: Dict[str, Any]) -> str:
    paths = _plex_track_part_paths(track)
    return paths[0] if paths else ""


def _plex_status_payload(force: bool = False) -> Dict[str, Any]:
    settings = _plex_settings()
    payload = {
        "ok": True,
        "configured": bool(settings.get("url") and settings.get("token")),
        "connected": False,
        "url": settings.get("url") or "",
        "section_preference": settings.get("section") or "",
        "beets_music_root": settings.get("beets_music_root") or "",
        "plex_music_root": settings.get("plex_music_roots") or "",
        "plex_scan_timeout": settings.get("plex_scan_timeout") or str(PLEX_SCAN_TIMEOUT),
        "plex_index_timeout": settings.get("plex_index_timeout") or str(PLEX_INDEX_TIMEOUT),
        "machine_id": "",
        "section_key": None,
        "section_title": None,
        "section_locations": [],
        "error": None,
    }
    if not payload["configured"]:
        payload["error"] = "Plex URL/token is not configured"
        return payload
    try:
        machine_id, section_key, section_title = _plex_find_music_section(force=force)
        payload.update({
            "machine_id": machine_id or "",
            "section_key": section_key,
            "section_title": section_title,
            "section_locations": _plex_section_locations(section_key) if section_key else [],
            "connected": bool(section_key),
        })
        if not section_key:
            payload["error"] = "No Plex music library section found"
    except urllib.error.HTTPError as ex:
        payload["error"] = "Plex token is invalid or expired." if ex.code in (401, 403) else f"Plex returned HTTP {ex.code}."
    except Exception as ex:
        _app_logger.warning("Plex status check failed: %s", type(ex).__name__)
        payload["error"] = "Could not reach Plex."
    return payload


def _playlist_status_id(track: Dict[str, Any]) -> str:
    artist = _norm(track.get("artist") or track.get("query_artist") or "")
    title = _norm(track.get("title") or track.get("query_title") or "")
    return f"{artist}|{title}"
