"""yt-dlp / SpotiFLAC provider: runtime discovery, installs and downloads (ARCH-001).
"""

from __future__ import annotations

import copy, importlib, json, os, re, shlex, shutil, subprocess, sys, time
import logging
import urllib.error
from backend.security import OutboundPolicyError, validate_outbound_url
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from backend.app_runtime import AUDIO_EXT, YTDLP_ALLOW_BROWSER_COOKIES, YTDLP_COOKIES_FROM_BROWSER, YTDLP_COOKIES_FROM_BROWSER_FALLBACK, YTDLP_COOKIE_FALLBACKS, YTDLP_COOKIE_FILE, YTDLP_NETRC_FILE, YTDLP_PO_PROVIDER_URL, YTDLP_REQUIRE_YOUTUBE_AUTH, _YTDLP_AUTH_SMOKE_TTL, _YTDLP_BGUTIL_PIP_PACKAGE, _YTDLP_COOKIE_REJECTED_FILE, _YTDLP_PIP_FALLBACK_PACKAGE, _YTDLP_PIP_PACKAGE, _YTDLP_RUNTIME_BIN_DIR, _plugin_install_log, _s, _ur, _ytdlp_auth_smoke_cache, _ytdlp_auth_smoke_lock, _ytdlp_ready
from backend.setup_service import _binary_status
from backend.app_runtime import _redact_security_text
from backend.slskd_service import _download_method_list, _normalise_download_method, _normalise_wanted_tracks, _wanted_track_label
from backend.auth_service import _yt_auth_source_failed_message, _yt_bot_check_message
from backend.matching_service import _fetch_mb_release_tracklist

# ── ARCH-001 extracted code ──


def _prepend_ytdlp_runtime_path() -> None:
    runtime_dir = str(_YTDLP_RUNTIME_BIN_DIR)
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if runtime_dir not in parts:
        os.environ["PATH"] = os.pathsep.join([runtime_dir] + parts)


def _probe_js_runtime(binary: str) -> Dict[str, Any]:
    try:
        r = subprocess.run(
            [binary, "--version"],
            timeout=5,
            capture_output=True,
            text=True,
        )
        output = (r.stdout or r.stderr or "").strip()
        version = output.splitlines()[0].strip()[:120] if output else ""
        return {
            "ok": r.returncode == 0 and bool(version),
            "returncode": r.returncode,
            "version": version,
            "error": "" if r.returncode == 0 else output[:240],
        }
    except Exception as ex:
        logging.getLogger("app").warning("JS runtime probe failed for %r: %s", binary, type(ex).__name__)
        return {
            "ok": False,
            "returncode": None,
            "version": "",
            "error": f"Could not run {binary}.",
        }


def _js_runtime_binary_names(runtime_name: str) -> List[str]:
    name = runtime_name.strip().lower()
    if name == "quickjs":
        return ["qjs", "quickjs"]
    return [name] if name else []


def _js_runtime_candidate_paths(configured: str) -> List[Tuple[str, str]]:
    name, sep, configured_path = configured.partition(":")
    name = name.strip().lower()
    candidates: List[Tuple[str, str]] = []
    seen: set = set()

    def add(path_value: str) -> None:
        path_value = _s(path_value).strip()
        if not path_value:
            return
        key = path_value.casefold()
        if key in seen:
            return
        seen.add(key)
        candidates.append((name, path_value))

    if sep and configured_path.strip():
        add(configured_path.strip())
    for probe_name in _js_runtime_binary_names(name):
        managed = _YTDLP_RUNTIME_BIN_DIR / probe_name
        if managed.exists():
            add(str(managed))
        path = shutil.which(probe_name)
        if path:
            add(path)
    return candidates


def _ytdlp_js_runtime_status() -> Dict[str, Any]:
    _prepend_ytdlp_runtime_path()
    runtimes = []
    failed = []
    for configured in _ytdlp_js_runtime_names():
        name = configured.split(":", 1)[0].strip().lower()
        if not name:
            continue
        for runtime_name, path in _js_runtime_candidate_paths(configured):
            probe_name = Path(path).name
            probe = _probe_js_runtime(path)
            if not probe.get("ok"):
                failed.append({
                    "name": probe_name,
                    "runtime": runtime_name,
                    "path": path,
                    "returncode": probe.get("returncode"),
                    "error": probe.get("error"),
                })
                continue
            runtimes.append({
                "name": probe_name,
                "runtime": runtime_name,
                "path": path,
                "version": probe.get("version", ""),
            })
            break
        if runtimes:
            break
    return {
        "available": bool(runtimes),
        "runtimes": runtimes,
        "failed_runtimes": [] if runtimes else failed,
        "path_prefix": str(_YTDLP_RUNTIME_BIN_DIR),
    }


def _require_ytdlp_js_runtime() -> Dict[str, Any]:
    status = _ytdlp_js_runtime_status()
    if not status.get("available"):
        raise RuntimeError(
            "yt-dlp YouTube downloads need a supported JavaScript runtime "
            "for signature challenge solving; Deno/Node/QuickJS was not found. "
            "Check /api/plugins/install-log for install details."
        )
    return status


def _ytdlp_js_runtime_names() -> List[str]:
    raw = os.environ.get("YTDLP_JS_RUNTIMES", "deno,node,quickjs")
    names = [part.strip() for part in raw.split(",") if part.strip()]
    return names or ["deno"]


def _ytdlp_js_runtime_options() -> Dict[str, Dict[str, str]]:
    status = _ytdlp_js_runtime_status()
    if status.get("runtimes"):
        runtime = status["runtimes"][0]
        name = _s(runtime.get("runtime") or runtime.get("name") or "").strip().lower()
        path = _s(runtime.get("path") or "").strip()
        if name and path:
            return {name: {"path": path}}
    runtimes: Dict[str, Dict[str, str]] = {}
    for item in _ytdlp_js_runtime_names():
        name, sep, path = item.partition(":")
        name = name.strip().lower()
        if not name:
            continue
        runtimes[name] = {"path": path.strip()} if sep and path.strip() else {}
    return runtimes or {"deno": {}}


def _ytdlp_remote_components() -> List[str]:
    raw = os.environ.get("YTDLP_REMOTE_COMPONENTS", "")
    components = [part.strip() for part in raw.split(",") if part.strip()]
    return components


def _ytdlp_cookie_help() -> str:
    return (
        "YouTube uses anonymous yt-dlp by default. Cookies are optional and only "
        "used when YTDLP_REQUIRE_YOUTUBE_AUTH=1 or an explicit restricted-content "
        "diagnostic is run. Browser cookies are disabled unless "
        "YTDLP_ALLOW_BROWSER_COOKIES=1 is set."
    )


def _ytdlp_cookie_rejected_help(cookie_file: str = "") -> str:
    if cookie_file.startswith("browser:"):
        browser = cookie_file.split(":", 1)[1]
        return (
            f"YouTube rejected the optional yt-dlp browser cookies ({browser}). "
            "Anonymous YouTube remains available; refresh or disable that optional auth source."
        )
    path_note = f" ({cookie_file})" if cookie_file else ""
    return (
        f"YouTube rejected the optional yt-dlp cookies{path_note}. "
        "Anonymous YouTube remains available; refresh or disable that optional auth source."
    )


def _ytdlp_cookie_signature(cookie_file: str) -> Dict[str, Any]:
    try:
        path = Path(cookie_file)
        st = path.stat()
        return {
            "cookie_file": str(path),
            "size": int(st.st_size),
            "mtime_ns": int(st.st_mtime_ns),
        }
    except Exception:
        return {"cookie_file": cookie_file, "size": 0, "mtime_ns": 0}


def _mark_ytdlp_cookie_rejected(cookie_file: str, reason: str = "") -> None:
    if not cookie_file:
        return
    try:
        _YTDLP_COOKIE_REJECTED_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = _ytdlp_cookie_signature(cookie_file)
        data["rejected_at"] = time.time()
        data["reason"] = reason or "YouTube bot/cookie challenge"
        tmp = _YTDLP_COOKIE_REJECTED_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_YTDLP_COOKIE_REJECTED_FILE)
    except Exception:
        pass


def _ytdlp_cookie_rejection_state(cookie_file: str) -> Optional[Dict[str, Any]]:
    if not cookie_file:
        return None
    try:
        if not _YTDLP_COOKIE_REJECTED_FILE.exists():
            return None
        data = json.loads(_YTDLP_COOKIE_REJECTED_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        current = _ytdlp_cookie_signature(cookie_file)
        if (
            _s(data.get("cookie_file")) == _s(current.get("cookie_file"))
            and int(data.get("size") or 0) == int(current.get("size") or 0)
            and int(data.get("mtime_ns") or 0) == int(current.get("mtime_ns") or 0)
        ):
            return data
    except Exception:
        return None
    return None


def _ytdlp_cookie_rejected_error(cookie_file: str, reason: str = "") -> str:
    _mark_ytdlp_cookie_rejected(cookie_file, reason)
    return _ytdlp_cookie_rejected_help(cookie_file)


def _normalise_ytdlp_browser_cookie_spec(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if value.lower() in ("0", "false", "no", "off", "none", "disabled"):
        return ""
    if "--cookies-from-browser" in value:
        try:
            parts = shlex.split(value)
        except ValueError:
            parts = value.split()
        for idx, part in enumerate(parts):
            if part == "--cookies-from-browser" and idx + 1 < len(parts):
                return parts[idx + 1].strip()
            if part.startswith("--cookies-from-browser="):
                return part.split("=", 1)[1].strip()
        return ""
    return value


def _parse_ytdlp_browser_cookie_spec(spec: str) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    value = _normalise_ytdlp_browser_cookie_spec(spec)
    container: Optional[str] = None
    main = value
    if "::" in main:
        main, container = main.split("::", 1)
        container = container or None

    profile: Optional[str] = None
    browser_part = main
    if ":" in main:
        browser_part, profile = main.split(":", 1)
        profile = profile or None

    keyring: Optional[str] = None
    browser = browser_part
    if "+" in browser_part:
        browser, keyring = browser_part.split("+", 1)
        keyring = keyring or None

    return browser.strip().lower(), profile, keyring.strip().lower() if keyring else None, container


def _configured_ytdlp_browser_cookie_spec(log: Optional[list] = None) -> str:
    if not YTDLP_ALLOW_BROWSER_COOKIES:
        if (YTDLP_COOKIES_FROM_BROWSER or YTDLP_COOKIES_FROM_BROWSER_FALLBACK) and log is not None:
            log.append(
                "  [yt-dlp] Browser cookies disabled; using --cookies FILE only"
            )
        return ""
    spec = _normalise_ytdlp_browser_cookie_spec(YTDLP_COOKIES_FROM_BROWSER)
    if YTDLP_COOKIES_FROM_BROWSER and not spec and log is not None:
        log.append("  [yt-dlp] WARN: YTDLP_COOKIES_FROM_BROWSER was set but no browser spec was found")
    return spec


def _fallback_ytdlp_browser_cookie_spec() -> str:
    if not YTDLP_ALLOW_BROWSER_COOKIES:
        return ""
    return _normalise_ytdlp_browser_cookie_spec(YTDLP_COOKIES_FROM_BROWSER_FALLBACK)


def _configured_ytdlp_cookie_file(log: Optional[list] = None) -> str:
    candidates: list[tuple[Path, bool]] = []
    if YTDLP_COOKIE_FILE:
        candidates.append((Path(YTDLP_COOKIE_FILE), True))
    candidates.extend((path, False) for path in YTDLP_COOKIE_FALLBACKS)

    for path, explicit in candidates:
        try:
            if not path.exists():
                if explicit and log is not None:
                    log.append(f"  [yt-dlp] WARN: YTDLP_COOKIE_FILE does not exist: {path}")
                continue
            if not path.is_file():
                if log is not None:
                    log.append(f"  [yt-dlp] WARN: cookie path is not a file: {path}")
                continue
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                header = fh.readline().strip()
            if header not in ("# HTTP Cookie File", "# Netscape HTTP Cookie File"):
                if log is not None:
                    log.append(
                        f"  [yt-dlp] WARN: cookie file must be Netscape format; ignoring {path}"
                    )
                continue
            return str(path)
        except Exception as ex:
            if log is not None:
                log.append(f"  [yt-dlp] WARN: could not read cookie file {path}: {ex}")
    return ""


def _ytdlp_browser_cookie_auth(browser: str, source: str = "configured") -> Dict[str, Any]:
    return {
        "mode": "browser",
        "browser": browser,
        "browser_spec": _parse_ytdlp_browser_cookie_spec(browser),
        "label": f"browser cookies {browser}",
        "source": source,
    }


def _ytdlp_cookie_file_auth(cookie_file: str) -> Dict[str, Any]:
    return {
        "mode": "file",
        "cookie_file": cookie_file,
        "label": f"cookie file {cookie_file}",
        "source": "configured",
    }


def _configured_ytdlp_netrc_file(log: Optional[list] = None) -> str:
    if not YTDLP_NETRC_FILE:
        return ""
    path = Path(YTDLP_NETRC_FILE)
    try:
        if not path.exists():
            return ""
        if not path.is_file():
            if log is not None:
                log.append(f"  [yt-dlp] WARN: netrc path is not a file: {path}")
            return ""
        if path.stat().st_size <= 0:
            if log is not None:
                log.append(f"  [yt-dlp] WARN: netrc file is empty: {path}")
            return ""
        return str(path)
    except Exception as ex:
        if log is not None:
            log.append(f"  [yt-dlp] WARN: could not read netrc file {path}: {ex}")
        return ""


def _apply_ytdlp_netrc(ydl_opts: Dict[str, Any], log: Optional[list] = None) -> None:
    netrc_file = _configured_ytdlp_netrc_file(log)
    if not netrc_file:
        return
    ydl_opts["usenetrc"] = True
    ydl_opts["netrc_location"] = netrc_file
    if log is not None:
        log.append(f"  [yt-dlp] Using --netrc --netrc-location {netrc_file}")


def _configured_ytdlp_cookie_auths(log: Optional[list] = None) -> List[Dict[str, Any]]:
    auths: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def add(auth: Dict[str, Any]) -> None:
        key = _ytdlp_cookie_auth_rejection_key(auth)
        if not key or key in seen:
            return
        seen.add(key)
        auths.append(auth)

    cookie_file = _configured_ytdlp_cookie_file(log)
    if cookie_file:
        add(_ytdlp_cookie_file_auth(cookie_file))

    explicit_browser = _configured_ytdlp_browser_cookie_spec(log)
    if explicit_browser:
        add(_ytdlp_browser_cookie_auth(explicit_browser, "configured"))

    fallback_browser = _fallback_ytdlp_browser_cookie_spec()
    if fallback_browser:
        add(_ytdlp_browser_cookie_auth(fallback_browser, "fallback"))

    return auths


def _usable_ytdlp_cookie_auths(log: Optional[list] = None) -> List[Dict[str, Any]]:
    usable: List[Dict[str, Any]] = []
    for auth in _configured_ytdlp_cookie_auths(log):
        key = _ytdlp_cookie_auth_rejection_key(auth)
        if key and not _ytdlp_cookie_rejection_state(key):
            usable.append(auth)
    return usable


def _describe_ytdlp_cookie_auths(auths: List[Dict[str, Any]]) -> List[str]:
    labels: List[str] = []
    for auth in auths:
        label = _s(auth.get("label"))
        key = _ytdlp_cookie_auth_rejection_key(auth)
        if key and _ytdlp_cookie_rejection_state(key):
            label = f"{label} (rejected)"
        if label:
            labels.append(label)
    return labels


def _mark_ytdlp_auth_rejected(auth: Dict[str, Any], reason: str = "") -> str:
    key = _ytdlp_cookie_auth_rejection_key(auth)
    if not key:
        return _ytdlp_cookie_help()
    msg = _ytdlp_cookie_rejected_error(key, reason)
    if auth.get("source") == "fallback":
        return msg.replace("configured yt-dlp browser cookies", "yt-dlp browser-cookie fallback")
    return msg


def _ytdlp_cookie_auth_rejection_key(auth: Dict[str, Any]) -> str:
    if auth.get("mode") == "browser":
        return f"browser:{auth.get('browser') or ''}"
    if auth.get("mode") == "file":
        return str(auth.get("cookie_file") or "")
    return ""


def _apply_ytdlp_cookie_auth(
    ydl_opts: Dict[str, Any],
    auth: Dict[str, Any],
    log: Optional[list] = None,
) -> None:
    if auth.get("mode") == "browser":
        ydl_opts["cookiesfrombrowser"] = auth.get("browser_spec")
        if log is not None:
            log.append(f"  [yt-dlp] Using browser cookies: {auth.get('browser')}")
    elif auth.get("mode") == "file":
        ydl_opts["cookiefile"] = auth.get("cookie_file")
        if log is not None:
            log.append(f"  [yt-dlp] Using --cookies {auth.get('cookie_file')}")


def _ytdlp_youtube_client_profiles() -> List[tuple]:
    raw = (os.environ.get("YTDLP_YOUTUBE_CLIENTS") or "").strip()
    clients = [c.strip() for c in raw.split(",") if c.strip()] if raw else [
        "mweb",
        "web_embedded",
        "web_safari",
        "ios",
        "android",
        "tv",
        "default",
    ]
    profiles: List[tuple] = []
    seen: set = set()
    for client in clients:
        key = client.casefold()
        if key in seen:
            continue
        seen.add(key)
        if key in ("", "default"):
            profiles.append(("default", None))
        else:
            profiles.append((client, {"youtube": {"player_client": [client]}}))
    return profiles or [("default", None)]


_SPOTIFLAC_INPUT_SOURCES = os.environ.get("SPOTIFLAC_INPUT_SOURCES", "soundcloud")


_SPOTIFLAC_SERVICES = os.environ.get("SPOTIFLAC_SERVICES", "tidal,qobuz,deezer,amazon,soundcloud")


def _download_method_label(method: Any) -> str:
    method = _normalise_download_method(method)
    return {
        "slskd": "SLSKD",
        "ytdlp": "YouTube",
        "soundcloud": "SoundCloud",
        "spotiflac": "SpotiFLAC",
        "resume": "Resume",
    }.get(method, method)


def _ytdlp_source_needs_youtube_auth(source: str) -> bool:
    return _normalise_download_method(source) == "ytdlp" and YTDLP_REQUIRE_YOUTUBE_AUTH


def _ytdlp_source_requires_js(source: str) -> bool:
    return _normalise_download_method(source) == "ytdlp"


def _ytdlp_client_profiles_for_source(source: str) -> List[tuple]:
    if _normalise_download_method(source) == "soundcloud":
        return [("default", None)]
    return _ytdlp_youtube_client_profiles()


def _ytdlp_cookie_auths_for_source(source: str, log: Optional[list] = None) -> List[Optional[Dict[str, Any]]]:
    if not _ytdlp_source_needs_youtube_auth(source):
        return [None]
    all_cookie_auths = _configured_ytdlp_cookie_auths(log)
    cookie_auths = _usable_ytdlp_cookie_auths(log)
    if not all_cookie_auths:
        raise RuntimeError(_ytdlp_cookie_help())
    if not cookie_auths:
        raise RuntimeError(
            "yt-dlp auth sources are unavailable: "
            + ", ".join(_describe_ytdlp_cookie_auths(all_cookie_auths))
        )
    return cookie_auths


def _ytdlp_album_queries(source: str, artist: str, album: str, max_tracks: int) -> List[str]:
    source = _normalise_download_method(source, "ytdlp")
    if source == "soundcloud":
        return [
            f"scsearch{max_tracks}:{artist} {album}",
            f"scsearch{max_tracks}:{artist} {album} full album",
        ]
    return [
        f"ytsearch{max_tracks}:{artist} {album}",
        f"ytsearch:{artist} {album} full album",
    ]


def _ytdlp_track_queries(source: str, artist: str, album: str, title: str, year: str = "") -> List[str]:
    source = _normalise_download_method(source, "ytdlp")
    prefix = "scsearch1" if source == "soundcloud" else "ytsearch1"
    queries = [f"{prefix}:{artist} {album} {title}"]
    if year:
        queries.append(f"{prefix}:{artist} {album} {title} {year}")
    queries.append(f"{prefix}:{artist} {title}")
    return queries


def _ytdlp_apply_cookie_auth_if_needed(ydl_opts: Dict[str, Any],
                                       cookie_auth: Optional[Dict[str, Any]],
                                       log: Optional[list] = None) -> None:
    if cookie_auth:
        _apply_ytdlp_cookie_auth(ydl_opts, cookie_auth, log)


def _ytdlp_apply_source_auth(ydl_opts: Dict[str, Any], source: str,
                             cookie_auth: Optional[Dict[str, Any]],
                             log: Optional[list] = None) -> None:
    _ytdlp_apply_cookie_auth_if_needed(ydl_opts, cookie_auth, log)
    if _normalise_download_method(source) == "soundcloud":
        _apply_ytdlp_netrc(ydl_opts, log)


def _merge_ytdlp_extractor_args(*groups: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for group in groups:
        if not group:
            continue
        for extractor, values in group.items():
            current = merged.setdefault(extractor, {})
            if isinstance(values, dict):
                for key, value in values.items():
                    current[key] = value
    return merged


def _ytdlp_base_extractor_args_for_source(source: str) -> Dict[str, Any]:
    if _normalise_download_method(source) != "ytdlp" or not YTDLP_PO_PROVIDER_URL:
        return {}
    try:
        validate_outbound_url(YTDLP_PO_PROVIDER_URL)
    except OutboundPolicyError as ex:
        raise RuntimeError("Configured yt-dlp PO provider URL is blocked by outbound policy") from ex
    return {"youtubepot-bgutilhttp": {"base_url": [YTDLP_PO_PROVIDER_URL]}}


def _ytdlp_source_extractor_args(source: str,
                                 client_args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _merge_ytdlp_extractor_args(_ytdlp_base_extractor_args_for_source(source), client_args)


def _ytdlp_audio_format_for_source(source: str) -> str:
    if _normalise_download_method(source) == "ytdlp":
        return "bestaudio[acodec!=none][vcodec=none]/bestaudio[acodec!=none]/bestaudio/best"
    return "bestaudio[ext=m4a]/bestaudio/best"


def _ytdlp_audio_codec_for_source(source: str) -> str:
    return "flac" if _normalise_download_method(source) == "ytdlp" else "mp3"


def _ytdlp_postprocessors_for_source(source: str) -> List[Dict[str, Any]]:
    codec = _ytdlp_audio_codec_for_source(source)
    audio_pp: Dict[str, Any] = {"key": "FFmpegExtractAudio", "preferredcodec": codec}
    if codec == "mp3":
        audio_pp["preferredquality"] = "320"
    return [audio_pp, {"key": "FFmpegMetadata"}]


def _ytdlp_youtube_impersonate_target(log: Optional[list] = None):
    raw = os.environ.get("YTDLP_YOUTUBE_IMPERSONATE", "chrome").strip().lower()
    if raw in {"", "0", "false", "no", "off", "none"}:
        return None
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        return ImpersonateTarget.from_str(raw)
    except Exception as ex:
        if log is not None:
            log.append(f"  [yt-dlp] WARN: browser impersonation unavailable for {raw}: {ex}")
        return None


def _ytdlp_apply_source_network_options(ydl_opts: Dict[str, Any], source: str,
                                        log: Optional[list] = None) -> None:
    if _normalise_download_method(source) != "ytdlp":
        return
    target = _ytdlp_youtube_impersonate_target(log)
    if target is not None:
        ydl_opts["impersonate"] = target


def _spotiflac_list_from_env(value: str) -> List[str]:
    return [
        item.strip().lower()
        for item in re.split(r"[\s,]+", _s(value))
        if item.strip()
    ]


def _spotiflac_input_sources() -> List[str]:
    sources = []
    for item in _download_method_list(_SPOTIFLAC_INPUT_SOURCES):
        method = _normalise_download_method(item, "soundcloud")
        if method == "soundcloud" and method not in sources:
            sources.append(method)
    return sources or ["soundcloud"]


def _spotiflac_services() -> List[str]:
    return _spotiflac_list_from_env(_SPOTIFLAC_SERVICES) or [
        "tidal", "qobuz", "deezer", "amazon", "soundcloud"
    ]


def _spotiflac_command(log: Optional[list] = None) -> List[str]:
    configured = os.environ.get("SPOTIFLAC_CMD", "").strip()
    if configured:
        parts = shlex.split(configured)
        if parts:
            first = shutil.which(parts[0]) or parts[0]
            try:
                if Path(first).exists():
                    return [first] + parts[1:]
            except Exception:
                pass
    candidates = [configured] if configured else []
    found = shutil.which("spotiflac")
    if found:
        candidates.append(found)
    candidates.extend([
        str(Path(sys.executable).with_name("spotiflac")),
        "/usr/local/bin/spotiflac",
        "/lsiopy/bin/spotiflac",
    ])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            if Path(candidate).exists():
                return [candidate]
        except Exception:
            continue
    if log is not None:
        log.append("  [spotiflac] runtime CLI installation is disabled; preinstall a pinned SpotiFLAC CLI in the image build")
    raise RuntimeError(
        "SpotiFLAC is not available. Install the pinned SpotiFLAC CLI during the container image build."
    )


def _audio_files_in_dir(dest_dir: str) -> set:
    try:
        return {
            str(p)
            for p in Path(dest_dir).rglob("*")
            if p.is_file() and p.suffix.lower() in AUDIO_EXT
        }
    except Exception:
        return set()


def _ytdlp_find_first_url(source: str, queries: List[str], log: list) -> str:
    if not _ytdlp_ready.wait(timeout=30):
        raise RuntimeError("yt-dlp not ready — still installing")
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp not installed")

    js_runtime = None
    if _ytdlp_source_requires_js(source):
        js_runtime = _require_ytdlp_js_runtime()
    base_opts = {
        "quiet": True,
        "no_warnings": False,
        "skip_download": True,
        "ignoreerrors": False,
        "socket_timeout": 30,
        "noplaylist": True,
        "extract_flat": "in_playlist",
        "js_runtimes": _ytdlp_js_runtime_options(),
        "remote_components": _ytdlp_remote_components(),
    }
    _ytdlp_apply_source_network_options(base_opts, source, log)
    if js_runtime and js_runtime.get("runtimes"):
        runtime = js_runtime["runtimes"][0]
        log.append(
            f"  [yt-dlp] JS runtime: {runtime['name']} {runtime.get('version') or ''}".rstrip()
        )

    for cookie_auth in _ytdlp_cookie_auths_for_source(source, log):
        auth_opts = dict(base_opts)
        _ytdlp_apply_cookie_auth_if_needed(auth_opts, cookie_auth, log)
        _ytdlp_apply_source_auth(auth_opts, source, cookie_auth, log)
        for query in queries:
            for client_label, extractor_args in _ytdlp_client_profiles_for_source(source):
                opts = dict(auth_opts)
                merged_extractor_args = _ytdlp_source_extractor_args(source, extractor_args)
                if merged_extractor_args:
                    opts["extractor_args"] = merged_extractor_args
                log.append(f"  [yt-dlp] Resolving source URL: {query!r} (client {client_label})")
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(query, download=False)
                except Exception as ex:
                    log.append(f"  [yt-dlp] URL resolve error: {ex}")
                    continue
                entries = info.get("entries") if isinstance(info, dict) else None
                candidates = entries if entries else [info]
                for entry in candidates or []:
                    if not entry:
                        continue
                    url = _s(entry.get("webpage_url") or entry.get("original_url") or entry.get("url")).strip()
                    if not url:
                        continue
                    if not url.startswith(("http://", "https://")):
                        if _normalise_download_method(source) == "ytdlp":
                            url = f"https://www.youtube.com/watch?v={url}"
                        else:
                            continue
                    log.append(f"  [yt-dlp] Source URL: {url}")
                    return url
    return ""


def _spotiflac_download_url(url: str, dest_dir: str, log: list, label: str = "") -> int:
    os.makedirs(dest_dir, exist_ok=True)
    before = _audio_files_in_dir(dest_dir)
    cmd = _spotiflac_command(log) + [url, dest_dir]
    services = _spotiflac_services()
    if services:
        cmd += ["--service"] + services
    cmd += [
        "--filename-format", "{title} - {artist}",
        "--use-track-numbers",
        "--use-album-track-numbers",
    ]
    log.append(
        "  [spotiflac] Downloading"
        + (f" {label}" if label else "")
        + f" via {url}"
    )
    try:
        proc = subprocess.run(
            cmd,
            timeout=int(os.environ.get("SPOTIFLAC_TIMEOUT", "1200") or "1200"),
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as ex:
        raise RuntimeError(f"SpotiFLAC timed out: {label or url}") from ex
    output = "\n".join([proc.stdout or "", proc.stderr or ""]).strip()
    for line in output.splitlines()[-25:]:
        text = line.strip()
        if text:
            log.append(f"  [spotiflac] {text[:240]}")
    if proc.returncode != 0:
        raise RuntimeError(f"SpotiFLAC failed with exit code {proc.returncode}")
    after = _audio_files_in_dir(dest_dir)
    return max(0, len(after - before))


def _spotiflac_missing_tracks_download(artist: str, album: str, year: str,
                                       dest_dir: str, log: list,
                                       wanted_tracks: List[Dict[str, Any]]) -> int:
    wanted_tracks = _normalise_wanted_tracks(wanted_tracks)
    if not wanted_tracks:
        return _spotiflac_album_download(artist, album, year, dest_dir, log)
    downloaded = 0
    failed: List[str] = []
    log.append(
        f"  [spotiflac] Missing-track mode: resolving URLs for "
        f"{len(wanted_tracks)} requested track(s)"
    )
    for idx, track in enumerate(wanted_tracks, start=1):
        title = _s(track.get("title", "")).strip()
        label = _wanted_track_label(track)
        if not title:
            failed.append(label)
            continue
        log.append(f"  [spotiflac] [{idx}/{len(wanted_tracks)}] {label}")
        source_url = ""
        for source in _spotiflac_input_sources():
            source_url = _ytdlp_find_first_url(
                source,
                _ytdlp_track_queries(source, artist, album, title, year),
                log,
            )
            if source_url:
                break
        if not source_url:
            failed.append(label)
            log.append(f"  [spotiflac] No source URL found for {label}")
            continue
        try:
            n_new = _spotiflac_download_url(source_url, dest_dir, log, label)
        except Exception as ex:
            failed.append(label)
            log.append(f"  [spotiflac] Failed {label}: {ex}")
            continue
        if n_new > 0:
            downloaded += n_new
        else:
            failed.append(label)
            log.append(
                f"  [spotiflac] No output file produced for {label} "
                "(SpotiFLAC exited successfully but no audio was saved)"
            )
    if downloaded:
        log.append(f"  [spotiflac] Downloaded {downloaded} file(s) to {dest_dir}")
        if failed:
            log.append(
                "  [spotiflac] Still not downloaded: "
                + ", ".join(failed[:8])
                + ("..." if len(failed) > 8 else "")
            )
        return downloaded
    raise RuntimeError("SpotiFLAC: no files downloaded")


def _spotiflac_album_download(artist: str, album: str, year: str,
                              dest_dir: str, log: list,
                              track_count: int = 0,
                              mb_albumid: str = "") -> int:
    mb_tracks: List[Dict[str, Any]] = []
    if mb_albumid:
        mb = _fetch_mb_release_tracklist(mb_albumid, log)
        if mb.get("ok"):
            mb_tracks = _normalise_wanted_tracks(mb.get("tracks") or [])
    if mb_tracks:
        log.append(
            f"  [spotiflac] Album mode: downloading {len(mb_tracks)} "
            "MusicBrainz track URL(s)"
        )
        return _spotiflac_missing_tracks_download(artist, album, year, dest_dir, log, mb_tracks)

    max_tracks = max(track_count + 2, 3) if track_count else 25
    queries_by_source = [
        (source, _ytdlp_album_queries(source, artist, album, max_tracks))
        for source in _spotiflac_input_sources()
    ]
    for source, queries in queries_by_source:
        source_url = _ytdlp_find_first_url(source, queries, log)
        if not source_url:
            continue
        n_new = _spotiflac_download_url(source_url, dest_dir, log, f"{artist} - {album}")
        if n_new:
            return n_new
    raise RuntimeError("SpotiFLAC: no album source URL found")


def _ytdlp_album_download(artist: str, album: str, year: str,
                           dest_dir: str, log: list,
                           track_count: int = 0,
                           source: str = "ytdlp") -> int:
    """Download album tracks from a yt-dlp-backed source.
    Returns number of audio files downloaded to dest_dir."""
    source = _normalise_download_method(source, "ytdlp")
    if not _ytdlp_ready.wait(timeout=30):
        raise RuntimeError("yt-dlp not ready — still installing")
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp not installed")

    cookie_auths = _ytdlp_cookie_auths_for_source(source, log)
    js_runtime = _require_ytdlp_js_runtime() if _ytdlp_source_requires_js(source) else {}

    os.makedirs(dest_dir, exist_ok=True)
    if track_count:
        max_tracks = max(track_count + 2, 3)
    else:
        max_tracks = 25
    bot_check_seen = {"count": 0}

    # Custom logger so yt-dlp errors/warnings appear in the job log
    _ytlog = log
    class _YdlLogger:
        def debug(self, msg):
            if not msg.startswith('[debug]'):
                _ytlog.append(f"  [yt-dlp] {msg[:200]}")
        def warning(self, msg):
            _ytlog.append(f"  [yt-dlp] WARN: {msg[:200]}")
        def error(self, msg):
            _ytlog.append(f"  [yt-dlp] ERR: {msg[:200]}")
            if _yt_bot_check_message(msg):
                bot_check_seen["count"] += 1

    # outtmpl: %(title)s avoids the %(playlist_index)02d silent-failure bug
    # (playlist_index is None for non-playlist downloads, causing ignoreerrors
    # to silently discard the file when using the %02d format spec).
    ydl_opts = {
        'format': _ytdlp_audio_format_for_source(source),
        'postprocessors': _ytdlp_postprocessors_for_source(source),
        'outtmpl': os.path.join(dest_dir, '%(title)s.%(ext)s'),
        'logger': _YdlLogger(),
        'quiet': True,
        'no_warnings': False,
        'ignoreerrors': False,
        'socket_timeout': 30,
        'playlistend': max_tracks,
        'noplaylist': False,
        'js_runtimes': _ytdlp_js_runtime_options(),
        'remote_components': _ytdlp_remote_components(),
    }
    _ytdlp_apply_source_network_options(ydl_opts, source, log)

    if js_runtime.get("runtimes"):
        runtime = js_runtime["runtimes"][0]
        log.append(
            f"  [yt-dlp] JS runtime: {runtime['name']} {runtime.get('version') or ''}".rstrip()
        )
    if ydl_opts.get("remote_components"):
        log.append(
            "  [yt-dlp] Remote components: "
            + ", ".join(ydl_opts["remote_components"])
        )

    log.append(f"  [yt-dlp] Source: {_download_method_label(source)}")
    queries = _ytdlp_album_queries(source, artist, album, max_tracks)
    client_profiles = _ytdlp_client_profiles_for_source(source)
    last_auth_error = ""
    for cookie_auth in cookie_auths:
        cookie_auth_key = _ytdlp_cookie_auth_rejection_key(cookie_auth) if cookie_auth else ""
        bot_check_seen["count"] = 0
        auth_opts = dict(ydl_opts)
        _ytdlp_apply_cookie_auth_if_needed(auth_opts, cookie_auth, log)
        _ytdlp_apply_source_auth(auth_opts, source, cookie_auth, log)
        auth_failed = False
        for query in queries:
            for client_label, extractor_args in client_profiles:
                log.append(f"  [yt-dlp] Trying: {query!r} (client {client_label})")
                n_before = len([f for f in os.listdir(dest_dir)
                                if os.path.splitext(f)[1].lower() in AUDIO_EXT])
                opts = dict(auth_opts)
                merged_extractor_args = _ytdlp_source_extractor_args(source, extractor_args)
                if merged_extractor_args:
                    opts["extractor_args"] = merged_extractor_args
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        ydl.download([query])
                    if cookie_auth and bot_check_seen["count"]:
                        last_auth_error = _mark_ytdlp_auth_rejected(cookie_auth)
                        auth_failed = True
                        log.append(
                            f"  [yt-dlp] Auth source failed: {cookie_auth.get('label')}; "
                            "trying next auth source..."
                        )
                        break
                except Exception as ex:
                    if cookie_auth and (_yt_auth_source_failed_message(str(ex)) or bot_check_seen["count"]):
                        last_auth_error = _mark_ytdlp_auth_rejected(cookie_auth, str(ex))
                        auth_failed = True
                        log.append(
                            f"  [yt-dlp] Auth source failed: {cookie_auth.get('label')}; "
                            "trying next auth source..."
                        )
                        break
                    log.append(f"  [yt-dlp] Error: {ex}")
                    continue
                files = [f for f in os.listdir(dest_dir)
                         if os.path.splitext(f)[1].lower() in AUDIO_EXT]
                n_new = len(files) - n_before
                if n_new > 0:
                    log.append(f"  [yt-dlp] Downloaded {n_new} new file(s) to {dest_dir}")
                    if n_new >= max(track_count // 2, 1):
                        return len(files)
                    log.append(f"  [yt-dlp] Only {n_new}/{track_count or '?'} tracks, trying next...")
            if auth_failed:
                break

    files = [f for f in os.listdir(dest_dir)
             if os.path.splitext(f)[1].lower() in AUDIO_EXT]
    if files:
        log.append(f"  [yt-dlp] {len(files)} total file(s) in {dest_dir}")
        return len(files)
    if last_auth_error:
        raise RuntimeError(last_auth_error)
    raise RuntimeError("yt-dlp: no audio files downloaded")


def _ytdlp_missing_tracks_download(artist: str, album: str, year: str,
                                   dest_dir: str, log: list,
                                   wanted_tracks: List[Dict[str, Any]],
                                   source: str = "ytdlp") -> int:
    """Download one yt-dlp result per requested missing MusicBrainz track."""
    source = _normalise_download_method(source, "ytdlp")
    wanted_tracks = _normalise_wanted_tracks(wanted_tracks)
    if not wanted_tracks:
        return _ytdlp_album_download(artist, album, year, dest_dir, log, source=source)
    if not _ytdlp_ready.wait(timeout=30):
        raise RuntimeError("yt-dlp not ready — still installing")
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp not installed")

    cookie_auths = _ytdlp_cookie_auths_for_source(source, log)
    js_runtime = _require_ytdlp_js_runtime() if _ytdlp_source_requires_js(source) else {}

    os.makedirs(dest_dir, exist_ok=True)
    bot_check_seen = {"count": 0}
    _ytlog = log

    class _YdlLogger:
        def debug(self, msg):
            if not msg.startswith('[debug]'):
                _ytlog.append(f"  [yt-dlp] {msg[:200]}")

        def warning(self, msg):
            _ytlog.append(f"  [yt-dlp] WARN: {msg[:200]}")

        def error(self, msg):
            _ytlog.append(f"  [yt-dlp] ERR: {msg[:200]}")
            if _yt_bot_check_message(msg):
                bot_check_seen["count"] += 1

    def _audio_files() -> set[str]:
        try:
            return {
                str(p)
                for p in Path(dest_dir).iterdir()
                if p.is_file() and p.suffix.lower() in AUDIO_EXT
            }
        except Exception:
            return set()

    def _safe_name(value: str, fallback: str) -> str:
        cleaned = re.sub(r'[\\/:*?"<>|]+', "_", _s(value)).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        return (cleaned or fallback)[:90]

    base_opts = {
        'format': _ytdlp_audio_format_for_source(source),
        'postprocessors': _ytdlp_postprocessors_for_source(source),
        'logger': _YdlLogger(),
        'quiet': True,
        'no_warnings': False,
        'ignoreerrors': False,
        'socket_timeout': 30,
        'noplaylist': True,
        'js_runtimes': _ytdlp_js_runtime_options(),
        'remote_components': _ytdlp_remote_components(),
    }
    _ytdlp_apply_source_network_options(base_opts, source, log)
    if js_runtime.get("runtimes"):
        runtime = js_runtime["runtimes"][0]
        log.append(
            f"  [yt-dlp] JS runtime: {runtime['name']} {runtime.get('version') or ''}".rstrip()
        )
    if base_opts.get("remote_components"):
        log.append(
            "  [yt-dlp] Remote components: "
            + ", ".join(base_opts["remote_components"])
        )
    log.append(
        f"  [yt-dlp] Missing-track mode: searching {_download_method_label(source)} for "
        f"{len(wanted_tracks)} requested track(s)"
    )

    client_profiles = _ytdlp_client_profiles_for_source(source)
    downloaded = 0
    failed: List[str] = []
    last_auth_error = ""
    for idx, track in enumerate(wanted_tracks, start=1):
        title = _s(track.get("title", "")).strip()
        if not title:
            failed.append(_wanted_track_label(track))
            log.append(f"  [yt-dlp] Skipping track {idx}: missing title")
            continue
        disc = int(track.get("disc") or 1)
        track_num = int(track.get("track") or 0)
        label = _wanted_track_label(track)
        prefix = (
            f"{disc:02d}-{track_num:02d} {_safe_name(title, f'track-{idx}')}"
            if track_num else
            f"{idx:03d} {_safe_name(title, f'track-{idx}')}"
        )
        outtmpl = os.path.join(dest_dir, f"{prefix} - %(title).120s.%(ext)s")
        queries = _ytdlp_track_queries(source, artist, album, title, year)

        log.append(f"  [yt-dlp] [{idx}/{len(wanted_tracks)}] {label}")
        track_downloaded = False
        for cookie_auth in cookie_auths:
            cookie_auth_key = _ytdlp_cookie_auth_rejection_key(cookie_auth) if cookie_auth else ""
            if cookie_auth_key and _ytdlp_cookie_rejection_state(cookie_auth_key):
                continue
            bot_check_seen["count"] = 0
            auth_opts = dict(base_opts)
            _ytdlp_apply_cookie_auth_if_needed(auth_opts, cookie_auth, log)
            _ytdlp_apply_source_auth(auth_opts, source, cookie_auth, log)
            auth_failed = False
            for query in queries:
                for client_label, extractor_args in client_profiles:
                    before = _audio_files()
                    log.append(f"  [yt-dlp] Trying: {query!r} (client {client_label})")
                    ydl_opts = dict(auth_opts)
                    ydl_opts["outtmpl"] = outtmpl
                    merged_extractor_args = _ytdlp_source_extractor_args(source, extractor_args)
                    if merged_extractor_args:
                        ydl_opts["extractor_args"] = merged_extractor_args
                    try:
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            ydl.download([query])
                        if cookie_auth and bot_check_seen["count"]:
                            last_auth_error = _mark_ytdlp_auth_rejected(cookie_auth)
                            auth_failed = True
                            log.append(
                                f"  [yt-dlp] Auth source failed: {cookie_auth.get('label')}; "
                                "trying next auth source..."
                            )
                            break
                    except Exception as ex:
                        if cookie_auth and (_yt_auth_source_failed_message(str(ex)) or bot_check_seen["count"]):
                            last_auth_error = _mark_ytdlp_auth_rejected(cookie_auth, str(ex))
                            auth_failed = True
                            log.append(
                                f"  [yt-dlp] Auth source failed: {cookie_auth.get('label')}; "
                                "trying next auth source..."
                            )
                            break
                        log.append(f"  [yt-dlp] Error: {ex}")
                        continue
                    after = _audio_files()
                    new_files = sorted(after - before)
                    if new_files:
                        downloaded += len(new_files)
                        track_downloaded = True
                        log.append(
                            f"  [yt-dlp] Downloaded {Path(new_files[0]).name}"
                        )
                        break
                if track_downloaded or auth_failed:
                    break
            if track_downloaded:
                break
        if not track_downloaded:
            failed.append(label)
            log.append(f"  [yt-dlp] No {_download_method_label(source)} result downloaded for {label}")

    if downloaded:
        log.append(f"  [yt-dlp] Downloaded {downloaded} missing-track file(s) to {dest_dir}")
        if failed:
            log.append(
                "  [yt-dlp] Still not downloaded: "
                + ", ".join(failed[:8])
                + ("..." if len(failed) > 8 else "")
        )
        return downloaded
    if last_auth_error:
        raise RuntimeError(last_auth_error)
    raise RuntimeError("yt-dlp: no missing tracks downloaded")


def _run_version_probe(binary: str) -> str:
    return _probe_js_runtime(binary).get("version", "")


def _js_runtime_available(runtime_name: str) -> bool:
    _prepend_ytdlp_runtime_path()
    for _, path in _js_runtime_candidate_paths(runtime_name):
        if _probe_js_runtime(path).get("ok"):
            return True
    return False


def _cleanup_broken_managed_runtime(name: str) -> None:
    clean_name = Path(_s(name)).name
    if not clean_name or clean_name != _s(name) or clean_name in {".", ".."}:
        _plugin_install_log.append(f"[warn] refusing unsafe managed runtime cleanup target: {name!r}")
        return
    runtime_root = _YTDLP_RUNTIME_BIN_DIR.resolve(strict=False)
    target = (_YTDLP_RUNTIME_BIN_DIR / clean_name).resolve(strict=False)
    try:
        target.relative_to(runtime_root)
    except Exception:
        _plugin_install_log.append(f"[warn] refusing managed runtime cleanup outside runtime root: {name!r}")
        return
    if not target.exists():
        return
    if target.is_symlink() or target.is_dir():
        _plugin_install_log.append(f"[warn] refusing managed runtime cleanup for symlink/directory: {target}")
        return
    probe = _probe_js_runtime(str(target))
    if probe.get("ok"):
        return
    try:
        target.unlink()
        _plugin_install_log.append(
            f"[cleanup] removed non-working managed {clean_name} runtime: {probe.get('error') or 'probe failed'}"
        )
    except Exception as ex:
        _plugin_install_log.append(f"[warn] could not remove non-working managed {clean_name}: {ex}")


def _install_ytdlp():
    _prepend_ytdlp_runtime_path()
    _plugin_install_log.append(
        "[blocked] runtime package installation disabled; yt-dlp and helper runtimes must be installed in the image build"
    )
    try:
        import yt_dlp
        ytdlp_version = getattr(getattr(yt_dlp, "version", None), "__version__", "unknown")
        _plugin_install_log.append(f"[ok]  yt-dlp import {ytdlp_version}")
    except Exception as ex:
        _plugin_install_log.append(f"[err] yt-dlp import failed; install pinned yt-dlp in the image build: {ex}")
    if _ytdlp_js_runtime_status().get("available"):
        _plugin_install_log.append("[ok]  preinstalled JS runtime available")
    else:
        _plugin_install_log.append("[warn] no preinstalled JS runtime available; YouTube challenge solving may fail")
    _ytdlp_ready.set()


def _ytdlp_cookie_rejection_seen(text: str) -> bool:
    lowered = (text or "").lower()
    return (
        "youtube rejected the configured yt-dlp cookies" in lowered
        or _yt_bot_check_message(text)
    )


def _ytdlp_netrc_machines(path: str) -> List[str]:
    machines: List[str] = []
    try:
        tokens = Path(path).read_text(encoding="utf-8", errors="ignore").split()
        for idx, token in enumerate(tokens[:-1]):
            if token == "machine":
                name = tokens[idx + 1].strip()
                if name and name not in machines:
                    machines.append(name)
    except Exception:
        pass
    return machines


def _configured_ytdlp_cookie_auth(log: Optional[list] = None) -> Dict[str, Any]:
    auths = _configured_ytdlp_cookie_auths(log)
    for auth in auths:
        key = _ytdlp_cookie_auth_rejection_key(auth)
        if key and not _ytdlp_cookie_rejection_state(key):
            return auth
    if auths:
        return auths[0]
    return {"mode": "none", "label": ""}


def _ytdlp_auth_smoke_check(auth: Dict[str, Any], *, force: bool = False) -> Dict[str, Any]:
    """Validate that yt-dlp can load cookies from this auth source."""
    key = _ytdlp_cookie_auth_rejection_key(auth)
    label = _s(auth.get("label") or key or "none")
    result: Dict[str, Any] = {
        "key": key,
        "label": label,
        "mode": _s(auth.get("mode") or "none"),
        "ok": False,
        "cached": False,
    }
    if not key:
        result["error"] = "No yt-dlp cookie auth source is configured."
        return result

    rejection = _ytdlp_cookie_rejection_state(key)
    if rejection:
        result["rejected"] = True
        result["rejection"] = rejection
        result["error"] = "yt-dlp auth source is already rejected."
        return result

    now = time.time()
    ttl = max(0, _YTDLP_AUTH_SMOKE_TTL)
    with _ytdlp_auth_smoke_lock:
        cached = copy.deepcopy(_ytdlp_auth_smoke_cache.get(key) or {})
    if cached and not force and ttl and now - float(cached.get("checked_at") or 0) <= ttl:
        cached["cached"] = True
        return cached

    try:
        if auth.get("mode") == "file":
            cookie_path = Path(_s(auth.get("cookie_file") or ""))
            if not cookie_path.exists() or cookie_path.stat().st_size <= 0:
                raise RuntimeError(f"yt-dlp cookie file is missing or empty: {cookie_path}")

        import yt_dlp

        ydl_opts: Dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "simulate": True,
            "noplaylist": True,
            "js_runtimes": _ytdlp_js_runtime_options(),
            "remote_components": _ytdlp_remote_components(),
        }
        smoke_url = _s(
            os.environ.get("YTDLP_AUTH_SMOKE_URL")
            or "https://www.youtube.com/watch?v=jNQXAC9IVRw"
        ).strip()
        if smoke_url.lower() in {"0", "false", "none", "off"}:
            smoke_url = ""
        _apply_ytdlp_cookie_auth(ydl_opts, auth)
        _apply_ytdlp_netrc(ydl_opts)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            cookiejar = getattr(ydl, "cookiejar", None)
            cookie_count = len(cookiejar) if cookiejar is not None else 0
            if cookie_count <= 0:
                raise RuntimeError(f"yt-dlp auth source loaded no cookies: {label}")
            if smoke_url:
                smoke_err = ""
                for client_label, extractor_args in _ytdlp_youtube_client_profiles():
                    probe_opts = dict(ydl_opts)
                    if extractor_args:
                        probe_opts["extractor_args"] = extractor_args
                    try:
                        with yt_dlp.YoutubeDL(probe_opts) as probe_ydl:
                            info = probe_ydl.extract_info(smoke_url, download=False)
                        if isinstance(info, dict):
                            result["smoke_title"] = _s(info.get("title") or "")[:120]
                        result["smoke_client"] = client_label
                        smoke_err = ""
                        break
                    except Exception as probe_exc:
                        smoke_err = str(probe_exc)
                        if _yt_auth_source_failed_message(smoke_err):
                            raise
                if smoke_err:
                    result["smoke_warning"] = _redact_security_text(smoke_err)[:500]
        result.update({
            "ok": True,
            "cookie_count": cookie_count,
            "checked_at": now,
            "smoke_url": smoke_url,
        })
    except Exception as exc:
        err = str(exc)
        result.update({
            "ok": False,
            "error": err[:500],
            "checked_at": now,
        })
        if _yt_auth_source_failed_message(err) or "loaded no cookies" in err.lower() or "missing or empty" in err.lower():
            _mark_ytdlp_auth_rejected(auth, err)
            result["rejected"] = True
            result["rejection"] = _ytdlp_cookie_rejection_state(key)

    with _ytdlp_auth_smoke_lock:
        _ytdlp_auth_smoke_cache[key] = copy.deepcopy(result)
    return result


def _usable_ytdlp_cookie_auths_with_smoke(*, force: bool = False) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    usable: List[Dict[str, Any]] = []
    checks: List[Dict[str, Any]] = []
    checked_keys: set[str] = set()
    while True:
        auth = None
        for candidate in _usable_ytdlp_cookie_auths():
            candidate_key = _ytdlp_cookie_auth_rejection_key(candidate)
            if candidate_key and candidate_key not in checked_keys:
                auth = candidate
                break
        if not auth:
            break
        key = _ytdlp_cookie_auth_rejection_key(auth)
        checked_keys.add(key)
        check = _ytdlp_auth_smoke_check(auth, force=force)
        checks.append(check)
        if check.get("ok"):
            usable.append(auth)
            break
    return usable, checks


def _ytdlp_cookie_candidates() -> List[str]:
    paths: List[str] = []
    if YTDLP_COOKIE_FILE:
        paths.append(YTDLP_COOKIE_FILE)
    paths.extend(str(path) for path in YTDLP_COOKIE_FALLBACKS)
    seen: set[str] = set()
    unique: List[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _package_version(dist_name: str) -> str:
    try:
        return importlib.metadata.version(dist_name)
    except Exception:
        return ""


def _yt_dlp_install_status() -> Dict[str, Any]:
    ytdlp_version = ""
    try:
        import yt_dlp
        ytdlp_version = getattr(getattr(yt_dlp, "version", None), "__version__", "")
    except Exception:
        ytdlp_version = ""
    return {
        "python": sys.executable,
        "runtime_install_enabled": False,
        "package": _YTDLP_PIP_PACKAGE,
        "fallback_package": _YTDLP_PIP_FALLBACK_PACKAGE,
        "version": ytdlp_version or _package_version("yt-dlp"),
        "ejs_version": _package_version("yt-dlp-ejs"),
        "curl_cffi_version": _package_version("curl_cffi"),
        "note": "yt-dlp and helper runtimes must be installed during the image build",
    }


def _ffmpeg_status() -> Dict[str, Any]:
    return {
        "ffmpeg": _binary_status("ffmpeg"),
        "ffprobe": _binary_status("ffprobe"),
    }


def _ytdlp_netrc_status() -> Dict[str, Any]:
    netrc_file = _configured_ytdlp_netrc_file()
    return {
        "enabled": bool(netrc_file),
        "file": netrc_file,
        "machines": _ytdlp_netrc_machines(netrc_file) if netrc_file else [],
    }


def _spotiflac_status() -> Dict[str, Any]:
    configured = os.environ.get("SPOTIFLAC_CMD", "").strip()
    candidates = [
        configured,
        shutil.which("spotiflac"),
        str(Path(sys.executable).with_name("spotiflac")),
        "/usr/local/bin/spotiflac",
        "/lsiopy/bin/spotiflac",
    ]
    command = ""
    for candidate in candidates:
        if not candidate:
            continue
        try:
            if Path(candidate).exists():
                command = candidate
                break
        except Exception:
            continue
    return {
        "available": bool(command),
        "enabled": bool(command) or _SPOTIFLAC_AUTO_INSTALL,
        "command": command,
        "auto_install": _SPOTIFLAC_AUTO_INSTALL,
        "package": _SPOTIFLAC_PIP_PACKAGE,
        "version": _package_version("SpotiFLAC") or _package_version("spotiflac"),
        "input_sources": _spotiflac_input_sources(),
        "services": _spotiflac_services(),
    }


def _ytdlp_po_provider_status() -> Dict[str, Any]:
    url = YTDLP_PO_PROVIDER_URL
    result: Dict[str, Any] = {
        "configured": bool(url),
        "url": url,
        "reachable": False,
    }
    if not url:
        return result
    try:
        req = _ur.Request(url, method="GET")
        with _ur.urlopen(req, timeout=3) as resp:
            result.update({"reachable": True, "status": getattr(resp, "status", None)})
    except urllib.error.HTTPError as ex:
        result.update({"reachable": ex.code < 500, "status": ex.code, "error": f"HTTP {ex.code}"})
    except Exception as ex:
        logging.getLogger("app").warning("PO provider reachability check failed: %s", type(ex).__name__)
        result["error"] = "Could not reach the PO token provider."
    return result


def _ytdlp_bgutil_plugin_status() -> Dict[str, Any]:
    version = _package_version("bgutil-ytdlp-pot-provider")
    return {
        "available": bool(version),
        "package": _YTDLP_BGUTIL_PIP_PACKAGE,
        "version": version,
    }


def _ytdlp_youtube_status(js_runtime: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    js_runtime = js_runtime or _ytdlp_js_runtime_status()
    install = _yt_dlp_install_status()
    ffmpeg = _ffmpeg_status()
    bgutil = _ytdlp_bgutil_plugin_status()
    provider = _ytdlp_po_provider_status()
    return {
        "enabled": True,
        "priority": 3,
        "label": "YouTube",
        "description": "Audio via yt-dlp",
        "auth_required": YTDLP_REQUIRE_YOUTUBE_AUTH,
        "anonymous_default": not YTDLP_REQUIRE_YOUTUBE_AUTH,
        "yt_dlp_available": bool(install.get("version")),
        "ffmpeg_available": bool((ffmpeg.get("ffmpeg") or {}).get("available")),
        "ffprobe_available": bool((ffmpeg.get("ffprobe") or {}).get("available")),
        "js_runtime_available": bool(js_runtime.get("available")),
        "ejs_available": bool(install.get("ejs_version")),
        "curl_cffi_available": bool(install.get("curl_cffi_version")),
        "bgutil_plugin": bgutil,
        "po_provider": provider,
        "ready": bool(install.get("version") and js_runtime.get("available")),
    }


def _redacted_ytdlp_auth_label(auth: Dict[str, Any]) -> str:
    mode = _s((auth or {}).get("mode") or "none")
    if mode == "file":
        return "cookie file configured"
    if mode == "browser":
        return "browser cookies configured"
    return "none"


def _redacted_ytdlp_rejection(rejection: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(rejection, dict):
        return None
    return {
        "configured": True,
        "rejected_at": rejection.get("rejected_at"),
        "reason": _redact_security_text(rejection.get("reason") or "")[:240],
    }


def _redacted_ytdlp_candidate_labels(candidates: Iterable[Any]) -> List[str]:
    count = len(list(candidates or []))
    return ["configured cookie path"] if count else []


_SPOTIFLAC_PIP_PACKAGE = os.environ.get("SPOTIFLAC_PIP_PACKAGE", "SpotiFLAC").strip() or "SpotiFLAC"


_SPOTIFLAC_AUTO_INSTALL = False


_YTDLP_SOURCE_METHODS = {"ytdlp", "soundcloud"}
