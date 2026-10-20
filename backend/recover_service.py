"""Recover My Uploads (increment 1): authorized FETCH + STAGE for a user's own
YouTube / SoundCloud uploads.

This reuses the guarded yt-dlp path (``ytdlp_guard.ytdlp_guarded_options`` with
its allowlist + public-IP/SSRF checks, unchanged) and adds a direct-URL /
creator entry point that the search-only album downloader does not have. It
stages authorized audio under ``DOWNLOADS_ROOT``; it does NOT import into the
library (a later increment) and performs NO DRM circumvention.

Key decisions (see the increment-1 spec):

* Authorized access = the operator's ``YTDLP_COOKIE_FILE`` (Netscape format),
  referenced only by variable name / signature. Cookie *contents* are never
  read, logged, echoed or stored here.
* YouTube audio is kept in its ORIGINAL lossy container UNCHANGED (no transcode
  to FLAC); provenance records it as ``quality="lossy"`` / not-lossless.
* SoundCloud fetches the ORIGINAL uploaded file when the owner enabled
  downloads for that track, otherwise the best available stream. No netrc / no
  stored SoundCloud credentials for this feature.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.app_runtime import AUDIO_EXT, _redact_security_text, _s, validated_downloads_root
from backend.audio_preferences import ffmpeg_file_input
from backend.auth_service import _yt_auth_source_failed_message, _yt_bot_check_message
from backend.acquisition_service import _download_dest_under, _download_folder_segment
from backend.ytdlp_guard import YtdlpTargetRejected, check_ytdlp_target, ytdlp_guarded_options
from backend.ytdlp_service import (
    _YtdlpJobLogger,
    _apply_ytdlp_cookie_auth,
    _configured_ytdlp_cookie_auths,
    _require_ytdlp_js_runtime,
    _usable_ytdlp_cookie_auths,
    _ytdlp_apply_source_network_options,
    _ytdlp_auth_smoke_check,
    _ytdlp_cookie_auth_rejection_key,
    _ytdlp_cookie_rejection_state,
    _ytdlp_cookie_signature,
    _ytdlp_js_runtime_options,
    _ytdlp_outtmpl,
    _ytdlp_remote_components,
)

# A staged file whose decoded duration is within this fraction of the probed
# metadata duration is accepted; outside it, it is treated as truncated/corrupt.
_RECOVER_DURATION_TOLERANCE = 0.15
_RECOVER_DURATION_FLOOR_SECONDS = 2.0


# ── yt-dlp module seam (so tests can inject a fake without a network) ──
def _yt_dlp_module(ydl_module: Any = None) -> Any:
    if ydl_module is not None:
        return ydl_module
    import yt_dlp  # lazy: not imported until a recovery call actually runs
    return yt_dlp


def _host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(_s(url)).hostname or "").lower()
    except ValueError:
        return ""


def _host_matches(host: str, *domains: str) -> bool:
    # Exact host or a real dot-boundary subdomain only (mirrors
    # ytdlp_guard.ytdlp_host_allowed). A bare ``endswith("youtube.com")`` would
    # also match ``evilyoutube.com`` (py/incomplete-url-substring-sanitization).
    clean = (host or "").strip().rstrip(".").lower()
    return bool(clean) and any(clean == d or clean.endswith("." + d) for d in domains)


def _is_youtube_host(host: str) -> bool:
    return _host_matches(host, "youtube.com", "youtu.be", "youtube-nocookie.com")


def _is_soundcloud_host(host: str) -> bool:
    return _host_matches(host, "soundcloud.com")


def _platform_for_host(host: str) -> str:
    if _is_youtube_host(host):
        return "youtube"
    if _is_soundcloud_host(host):
        return "soundcloud"
    return "other"


# ── format / quality plan per the increment-1 decisions ──
def _recover_format_plan(host: str, probe: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Pick the highest *authorized* audio format and record its quality.

    YouTube: keep the original lossy container unchanged (``quality="lossy"``,
    not lossless). SoundCloud: prefer the owner-enabled original download
    (format_id ``download``), else best stream. We never transcode here, so no
    audio postprocessor is attached and the file is staged as downloaded.
    """
    if _is_youtube_host(host):
        return {
            "format": "bestaudio[acodec!=none][vcodec=none]/bestaudio[acodec!=none]/bestaudio/best",
            "quality": "lossy",
            "lossless": False,
            "note": "YouTube audio kept in its original lossy container (not transcoded)",
        }
    if _is_soundcloud_host(host):
        original_available = False
        for fmt in (probe or {}).get("formats") or []:
            if _s(fmt.get("format_id")).lower() == "download" or "original" in _s(fmt.get("format_note")).lower():
                original_available = True
                break
        return {
            # "download" is SoundCloud's owner-enabled original file.
            "format": "download/bestaudio/best",
            "quality": "original" if original_available else "stream",
            "lossless": bool(original_available),
            "note": (
                "SoundCloud original upload"
                if original_available
                else "SoundCloud best available stream (owner download not enabled)"
            ),
        }
    return {
        "format": "bestaudio/best",
        "quality": "original",
        "lossless": False,
        "note": "best available audio",
    }


# ── cookie-state reporting (never exposes cookie contents) ──
def recover_cookie_state() -> Dict[str, Any]:
    """Report the recoverable cookie state for owner-only recovery, by variable
    name and signature only. One of: ``none`` / ``invalid`` / ``rejected`` /
    ``ok``. Cookie *contents* are never read here."""
    file_auth: Dict[str, Any] = {}
    for auth in _configured_ytdlp_cookie_auths():
        if auth.get("mode") == "file":
            file_auth = auth
            break

    cookie_file = _s(file_auth.get("cookie_file"))
    if not cookie_file:
        # Either YTDLP_COOKIE_FILE is unset, or the configured path is missing /
        # not a readable Netscape file (_configured_ytdlp_cookie_file rejected it).
        if _env_cookie_path():
            return {
                "state": "invalid",
                "configured": True,
                "signature": None,
                "message": "YTDLP_COOKIE_FILE is set but is missing or not a Netscape-format cookie file.",
                "action": "Re-export cookies in Netscape format to the path in YTDLP_COOKIE_FILE.",
            }
        return {
            "state": "none",
            "configured": False,
            "signature": None,
            "message": "No yt-dlp cookies are configured; private/unlisted own uploads will be skipped.",
            "action": "Place a Netscape cookies file and set YTDLP_COOKIE_FILE to recover owner-only content.",
        }

    key = _ytdlp_cookie_auth_rejection_key(file_auth)
    signature = _ytdlp_cookie_signature(cookie_file)  # path/size/mtime only — never contents
    rejection = _ytdlp_cookie_rejection_state(key) if key else None
    if rejection:
        return {
            "state": "rejected",
            "configured": True,
            "signature": signature,
            "message": "The configured yt-dlp cookies were rejected or have expired.",
            "action": "Refresh YTDLP_COOKIE_FILE with freshly exported cookies for the owning account.",
        }

    smoke = _ytdlp_auth_smoke_check(file_auth)
    if smoke.get("ok"):
        return {
            "state": "ok",
            "configured": True,
            "signature": signature,
            "message": "yt-dlp cookies are present and loaded.",
            "action": "",
        }
    if smoke.get("rejected"):
        return {
            "state": "rejected",
            "configured": True,
            "signature": signature,
            "message": "The configured yt-dlp cookies were rejected or have expired.",
            "action": "Refresh YTDLP_COOKIE_FILE with freshly exported cookies for the owning account.",
        }
    return {
        "state": "invalid",
        "configured": True,
        "signature": signature,
        "message": "The configured yt-dlp cookie file loaded no usable cookies.",
        "action": "Re-export a complete Netscape cookies file for the owning account into YTDLP_COOKIE_FILE.",
    }


def _env_cookie_path() -> str:
    # The raw configured path (not its contents), used only to tell "unset" from
    # "set but unusable" in cookie-state reporting.
    from backend.app_runtime import YTDLP_COOKIE_FILE
    return _s(YTDLP_COOKIE_FILE)


# ── error taxonomy ──
def classify_recover_error(error: Any) -> Dict[str, str]:
    """Map a yt-dlp error to a recoverable category, action and sanitized
    message. DRM is marked ONLY on real EME/DRM evidence — an auth, format or
    HLS-assembly failure is never labelled DRM."""
    raw = _s(error)
    text = raw.lower()
    sanitized = _redact_security_text(raw)[:500]

    def result(category: str, action: str) -> Dict[str, str]:
        return {"category": category, "action": action, "message": sanitized}

    # Auth first: owner-only content needs the operator's cookies.
    if (
        _yt_auth_source_failed_message(raw)
        or _yt_bot_check_message(raw)
        or "private video" in text
        or "login required" in text
        or "sign in" in text
        or ("members-only" in text or "members only" in text)
    ):
        return result("auth-needed",
                      "Configure/refresh YTDLP_COOKIE_FILE with cookies for the owning account.")

    if "429" in text or "too many requests" in text or "rate limit" in text or "rate-limit" in text:
        return result("rate-limited", "Wait and retry later; reduce concurrency.")

    if (
        "geo" in text and ("restrict" in text or "block" in text or "not available" in text)
    ) or "not available in your country" in text or "age-restricted" in text or "confirm your age" in text:
        return result("access-restricted",
                      "Content is access-restricted; owner cookies for the correct account may be required.")

    if (
        "no longer available" in text
        or "video unavailable" in text
        or "has been removed" in text
        or "removed by the uploader" in text
        or "url expired" in text
        or "expired" in text
        or ("403" in text and ("forbidden" in text or "googlevideo" in text))
    ):
        return result("expired-url", "Re-fetch a fresh URL for this item, or it no longer exists.")

    if "m3u8" in text or "hls" in text or "fragment" in text or "ffmpeg exited" in text:
        return result("hls-assembly", "Transient HLS/stream assembly failure; retry the fetch.")

    if "requested format is not available" in text or "no video formats" in text or "no suitable format" in text or "requested format" in text:
        return result("format", "Adjust the requested format or try again; the stream format changed.")

    if (
        "widevine" in text
        or "fairplay" in text
        or "playready" in text
        # "eme" as a standalone token only: a word-boundary match so that an
        # ordinary "Unsupported URL scheme" error (sch-EME) is never mislabeled
        # as DRM. DRM is marked only on real EME/CDM evidence.
        or re.search(r"\beme\b", text)
        or "drm protected" in text
        or "protected by drm" in text
        or ("drm" in text and "protect" in text)
    ):
        return result("genuinely-DRM",
                      "This item is DRM/EME protected; recover it from the account owner's own source, not here.")

    if (
        "not installed" in text
        or "not ready" in text
        or "javascript runtime" in text
        or "netscape" in text
        or "ffmpeg" in text
        or "ffprobe" in text
    ):
        return result("misconfig", "Fix the server setup named in the message (binary/runtime/cookies).")

    return result("unknown", "Review the sanitized message; retry or report it.")


# ── integrity gate ──
def _probe_file_duration(path: str) -> Optional[float]:
    """Decoded duration of ``path`` in seconds via ffprobe, or None if ffprobe
    cannot read it. Local-file-only input (SEC-13)."""
    import shutil
    import subprocess
    ffprobe = shutil.which("ffprobe") or "/usr/bin/ffprobe"
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "json", ffmpeg_file_input(path)],
            timeout=int(os.environ.get("AUDIO_FFPROBE_TIMEOUT", "20") or "20"),
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "{}")
        value = float((data.get("format") or {}).get("duration"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    return value if value > 0 else None


def _duration_within_tolerance(actual: Optional[float], expected: Optional[float],
                               tolerance: float = _RECOVER_DURATION_TOLERANCE) -> bool:
    """True when ``actual`` is within ``tolerance`` of ``expected`` (generalizes
    ``_soundcloud_duration_ok``). Unknown ``expected`` cannot be checked here, so
    the caller decides; this only answers the comparison."""
    try:
        a = float(actual)
        e = float(expected)
    except (TypeError, ValueError):
        return False
    if e <= 0:
        return False
    return abs(a - e) <= max(e * tolerance, _RECOVER_DURATION_FLOOR_SECONDS)


def recover_integrity_check(path: str, expected_duration: Optional[float]) -> Dict[str, Any]:
    """Staging gate: reject leftover ``.part``, zero-byte, undecodable or short
    files. Returns ``{ok, reason, duration, duration_verified}``."""
    p = Path(path)
    name = p.name.lower()
    if name.endswith(".part") or name.endswith(".ytdl"):
        return {"ok": False, "reason": "leftover partial download (.part)", "duration": None, "duration_verified": False}
    try:
        size = p.stat().st_size
    except OSError:
        return {"ok": False, "reason": "file is missing", "duration": None, "duration_verified": False}
    if size <= 0:
        return {"ok": False, "reason": "zero-byte file", "duration": None, "duration_verified": False}

    actual = _probe_file_duration(str(p))
    if actual is None:
        return {"ok": False, "reason": "ffprobe could not decode the file", "duration": None, "duration_verified": False}

    try:
        expected = float(expected_duration) if expected_duration is not None else 0.0
    except (TypeError, ValueError):
        expected = 0.0
    if expected > 0:
        if not _duration_within_tolerance(actual, expected):
            return {
                "ok": False,
                "reason": f"duration {actual:.1f}s is outside tolerance of expected {expected:.1f}s (truncated/corrupt)",
                "duration": actual,
                "duration_verified": True,
            }
        return {"ok": True, "reason": "", "duration": actual, "duration_verified": True}
    # No metadata duration to compare against: decodable and non-empty is the
    # best we can assert.
    return {"ok": True, "reason": "", "duration": actual, "duration_verified": False}


# ── probe / enumerate ──
def _recover_base_opts(host: str, *, log: Optional[list] = None) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": False,
        "socket_timeout": 30,
        "noplaylist": True,
        "js_runtimes": _ytdlp_js_runtime_options(),
        "remote_components": _ytdlp_remote_components(),
    }
    _ytdlp_apply_source_network_options(opts, "ytdlp" if _is_youtube_host(host) else "", log)
    return opts


def _recover_apply_owner_cookies(opts: Dict[str, Any], log: Optional[list] = None) -> Dict[str, Any]:
    """Apply the first usable configured cookie auth (owner-only content). No
    netrc. Returns the chosen auth dict (``{}`` when anonymous)."""
    usable = _usable_ytdlp_cookie_auths(log)
    for auth in usable:
        if auth.get("mode") == "file":
            _apply_ytdlp_cookie_auth(opts, auth, log)
            return auth
    if usable:
        _apply_ytdlp_cookie_auth(opts, usable[0], log)
        return usable[0]
    return {}


def recover_probe(url: str, *, ydl_module: Any = None) -> Dict[str, Any]:
    """Metadata/format probe (``extract_info(download=False)``) for one
    allowlisted URL, plus the selected authorized format plan. Never downloads."""
    url = _s(url).strip()
    try:
        check_ytdlp_target(url)
    except YtdlpTargetRejected as exc:
        return {"ok": False, "error": _s(exc), "category": "misconfig"}
    host = _host_of(url)
    opts = _recover_base_opts(host)
    auth = _recover_apply_owner_cookies(opts)
    try:
        yt_dlp = _yt_dlp_module(ydl_module)
        with yt_dlp.YoutubeDL(ytdlp_guarded_options(opts, [url])) as ydl:
            info = ydl.extract_info(url, download=False)
    except YtdlpTargetRejected as exc:
        return {"ok": False, "error": _s(exc), "category": "misconfig"}
    except Exception as exc:  # noqa: BLE001 — classified below
        return {"ok": False, **classify_recover_error(exc)}
    if not isinstance(info, dict):
        return {"ok": False, "error": "no metadata returned", "category": "unknown"}

    formats = [
        {
            "format_id": _s(f.get("format_id")),
            "ext": _s(f.get("ext")),
            "acodec": _s(f.get("acodec")),
            "vcodec": _s(f.get("vcodec")),
            "abr": f.get("abr"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "format_note": _s(f.get("format_note")),
        }
        for f in (info.get("formats") or []) if isinstance(f, dict)
    ]
    plan = _recover_format_plan(host, {"formats": formats})
    return {
        "ok": True,
        "platform": _platform_for_host(host),
        "title": _s(info.get("title"))[:300],
        "uploader": _s(info.get("uploader") or info.get("channel") or info.get("uploader_id"))[:200],
        "duration": info.get("duration"),
        "webpage_url": _s(info.get("webpage_url") or url),
        "formats": formats,
        "selected_format": plan["format"],
        "quality": plan["quality"],
        "lossless": plan["lossless"],
        "authorized_access": bool(auth),
    }


def recover_enumerate(url: str, *, limit: int = 500, ydl_module: Any = None) -> Dict[str, Any]:
    """Resolve a channel/user/playlist/set URL with ``extract_flat`` and list the
    owner's items. The user supplies the URL/handle; only allowlisted-host items
    are returned. Nothing is downloaded."""
    url = _s(url).strip()
    try:
        check_ytdlp_target(url)
    except YtdlpTargetRejected as exc:
        return {"ok": False, "error": _s(exc), "category": "misconfig"}
    host = _host_of(url)
    opts = _recover_base_opts(host)
    opts["extract_flat"] = "in_playlist"
    opts["noplaylist"] = False
    opts["playlistend"] = max(1, min(int(limit or 500), 2000))
    _recover_apply_owner_cookies(opts)
    try:
        yt_dlp = _yt_dlp_module(ydl_module)
        with yt_dlp.YoutubeDL(ytdlp_guarded_options(opts, [url])) as ydl:
            info = ydl.extract_info(url, download=False)
    except YtdlpTargetRejected as exc:
        return {"ok": False, "error": _s(exc), "category": "misconfig"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, **classify_recover_error(exc)}

    items: List[Dict[str, Any]] = []
    entries = info.get("entries") if isinstance(info, dict) else None
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        item_url = _s(entry.get("webpage_url") or entry.get("url")).strip()
        if not item_url.startswith(("http://", "https://")):
            continue
        # Only keep items that pass the same allowlist/SSRF guard.
        try:
            check_ytdlp_target(item_url)
        except YtdlpTargetRejected:
            continue
        items.append({
            "url": item_url,
            "title": _s(entry.get("title"))[:300],
            "uploader": _s(entry.get("uploader") or entry.get("channel"))[:200],
            "duration": entry.get("duration"),
        })
    return {
        "ok": True,
        "platform": _platform_for_host(host),
        "uploader": _s((info or {}).get("uploader") or (info or {}).get("channel") or (info or {}).get("title"))[:200],
        "count": len(items),
        "items": items,
    }


# ── provenance ──
def _write_provenance(staged_file: str, record: Dict[str, Any]) -> str:
    """Persist provenance next to the staged file for a later import to consume.
    Never contains cookie contents."""
    sidecar = str(Path(staged_file).with_suffix(Path(staged_file).suffix + ".recover.json"))
    try:
        Path(sidecar).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return ""
    return sidecar


# ── fetch + stage job ──
def _recover_stage_dir(platform: str) -> str:
    root = validated_downloads_root()
    segment = _download_folder_segment(platform) or "other"
    return str(_download_dest_under(root, "recovered", segment))


def _new_audio_files(dest_dir: str, before: set) -> List[str]:
    try:
        now = {
            str(p) for p in Path(dest_dir).iterdir()
            if p.is_file() and p.suffix.lower() in AUDIO_EXT
        }
    except OSError:
        return []
    return sorted(now - before)


def _recover_fetch_one(url: str, log: list, *, job_id: str = "",
                       ydl_module: Any = None) -> Dict[str, Any]:
    url = _s(url).strip()
    try:
        check_ytdlp_target(url)
    except YtdlpTargetRejected as exc:
        log.append(f"  [recover] Rejected target: {exc}")
        return {"url": url, "recovered": False, "category": "misconfig",
                "action": "Use an allowlisted public YouTube/SoundCloud URL.", "message": _s(exc)}

    host = _host_of(url)
    platform = _platform_for_host(host)
    if _is_youtube_host(host):
        try:
            _require_ytdlp_js_runtime()
        except RuntimeError as exc:
            return {"url": url, "recovered": False, **_fail_from(exc)}

    probe = recover_probe(url, ydl_module=ydl_module)
    if not probe.get("ok"):
        return {"url": url, "recovered": False,
                "category": probe.get("category", "unknown"),
                "action": classify_recover_error(probe.get("error") or probe.get("message") or "").get("action", ""),
                "message": _s(probe.get("error") or probe.get("message"))}
    plan = _recover_format_plan(host, probe)
    expected_duration = probe.get("duration")

    stage_dir = _recover_stage_dir(platform)
    os.makedirs(stage_dir, exist_ok=True)
    outtmpl = _ytdlp_outtmpl(stage_dir, "%(title).120s [%(id)s].%(ext)s")

    opts: Dict[str, Any] = {
        "format": plan["format"],
        "postprocessors": [],  # keep the original container UNCHANGED (no transcode)
        "outtmpl": outtmpl,
        "logger": _YtdlpJobLogger(log, {"count": 0}),
        "quiet": True,
        "no_warnings": False,
        "ignoreerrors": False,
        "socket_timeout": 30,
        "noplaylist": True,
        # Resume (#5): continue partial downloads and retry fragments.
        "continuedl": True,
        "nopart": False,
        "retries": 5,
        "fragment_retries": 10,
        "file_access_retries": 3,
        "js_runtimes": _ytdlp_js_runtime_options(),
        "remote_components": _ytdlp_remote_components(),
    }
    _ytdlp_apply_source_network_options(opts, "ytdlp" if _is_youtube_host(host) else "", log)
    auth = _recover_apply_owner_cookies(opts, log)

    before = set()
    try:
        before = {str(p) for p in Path(stage_dir).iterdir()
                  if p.is_file() and p.suffix.lower() in AUDIO_EXT}
    except OSError:
        pass

    log.append(f"  [recover] Fetching {url} ({plan['note']})")
    try:
        yt_dlp = _yt_dlp_module(ydl_module)
        with yt_dlp.YoutubeDL(ytdlp_guarded_options(opts, [url])) as ydl:
            ydl.download([url])
    except YtdlpTargetRejected as exc:
        return {"url": url, "recovered": False, "category": "misconfig",
                "action": "Use an allowlisted public URL.", "message": _s(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"url": url, "recovered": False, **_fail_from(exc)}

    new_files = _new_audio_files(stage_dir, before)
    if not new_files:
        return {"url": url, "recovered": False, "category": "format",
                "action": "No audio file was produced; check the requested format.",
                "message": "no audio file produced"}

    staged = new_files[-1]
    integrity = recover_integrity_check(staged, expected_duration)
    if not integrity.get("ok"):
        try:
            os.remove(staged)
        except OSError:
            pass
        log.append(f"  [recover] Integrity gate failed for {Path(staged).name}: {integrity.get('reason')}")
        return {"url": url, "recovered": False, "category": "integrity",
                "action": "Re-fetch; the staged file was truncated or corrupt.",
                "message": _s(integrity.get("reason"))}

    record = {
        "source_platform": platform,
        "source_url": probe.get("webpage_url") or url,
        "uploader": probe.get("uploader", ""),
        "title": probe.get("title", ""),
        "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "authorized_access": bool(auth),
        "quality": plan["quality"],
        "lossless": plan["lossless"],
        "duration": integrity.get("duration"),
        "job_id": job_id,
    }
    sidecar = _write_provenance(staged, record)
    log.append(f"  [recover] Staged {Path(staged).name} (quality={plan['quality']})")
    return {
        "url": url,
        "recovered": True,
        "staged_file": staged,
        "provenance": sidecar,
        "quality": plan["quality"],
        "lossless": plan["lossless"],
        "duration": integrity.get("duration"),
        "authorized_access": bool(auth),
    }


def _fail_from(exc: Any) -> Dict[str, str]:
    info = classify_recover_error(exc)
    return {"category": info["category"], "action": info["action"], "message": info["message"]}


def recover_fetch_job(urls: List[str], log: list, cancel_event: Any = None,
                      *, job_id: str = "", ydl_module: Any = None) -> Dict[str, Any]:
    """Fetch + stage each URL under DOWNLOADS_ROOT. Does NOT import. Per-URL
    result (recovered / failed-with-reason) goes to the job log."""
    recovered: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    cookie_state = recover_cookie_state()
    if cookie_state.get("state") != "ok":
        log.append(f"  [recover] Cookie state: {cookie_state.get('state')} — {cookie_state.get('message')}")
    for idx, url in enumerate(urls, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("cancelled")
        log.append(f"[recover] [{idx}/{len(urls)}] {url}")
        result = _recover_fetch_one(url, log, job_id=job_id, ydl_module=ydl_module)
        if result.get("recovered"):
            recovered.append(result)
        else:
            failed.append(result)
            log.append(
                f"  [recover] Failed ({result.get('category')}): {result.get('action')}"
            )
    return {
        "ok": True,
        "cookie_state": cookie_state.get("state"),
        "recovered_count": len(recovered),
        "failed_count": len(failed),
        "recovered": recovered,
        "failed": failed,
    }


def start_recover_fetch(payload_in: Dict[str, Any]) -> Any:
    """Service behind POST /api/recover/fetch (request-free). Starts a tracked
    job on the canonical job infrastructure; returns (json_body, http_status)."""
    import backend.job_contract as job_contract
    from backend.app_runtime import jobs

    data = payload_in or {}
    urls: List[str] = []
    raw = data.get("urls")
    if isinstance(raw, list):
        urls = [_s(u).strip() for u in raw if _s(u).strip()]
    single = _s(data.get("url")).strip()
    if single:
        urls.append(single)
    # de-dup, keep order
    seen: set = set()
    urls = [u for u in urls if not (u in seen or seen.add(u))]
    if not urls:
        return {"ok": False, "error": "one or more URLs are required"}, 200

    # Validate the allowlist/SSRF guard up front, before any job exists.
    for url in urls:
        try:
            check_ytdlp_target(url)
        except YtdlpTargetRejected as exc:
            return {"ok": False, "error": f"{url}: {exc}"}, 200

    try:
        validated_downloads_root()
    except RuntimeError as exc:
        return {"ok": False, "error": _s(exc)}, 200

    # The download runs long after the job starts, so reading job_id from this
    # holder inside the job (set just below) is safe for provenance labelling.
    holder: Dict[str, str] = {"job_id": ""}

    def _do(log, cancel_event=None):
        return recover_fetch_job(urls, log, cancel_event, job_id=holder["job_id"])

    label = f"Recover uploads: {len(urls)} item(s)"
    workflow = "recover-fetch-" + job_contract.slug("|".join(urls))
    job = jobs.start_python(
        job_contract.guarded(_do, workflow=workflow, fail_fast_in_process=True),
        label=label,
        metadata={"type": "recover-fetch", "mutating": False, "url_count": len(urls)},
    )
    holder["job_id"] = job.job_id
    return {"ok": True, "job_id": job.job_id}, 200
