"""Recover My Uploads (increments 1 & 2): authorized FETCH, STREAM DIAGNOSTICS,
STAGE, VERIFY, BEETS IMPORT, RETRY, and PLAYLIST COVERAGE REFRESH for a user's
own YouTube / SoundCloud uploads.

This reuses the guarded yt-dlp path (``ytdlp_guard.ytdlp_guarded_options`` with
its allowlist + public-IP/SSRF checks, unchanged) and adds a direct-URL /
creator entry point that the search-only album downloader does not have. It
stages authorized audio under ``DOWNLOADS_ROOT``, verifies audio integrity and
identity (AcoustID + MusicBrainz with safe singleton fallback for original
uploads), imports into Beets via ``beets_adapter.run_import`` without
overwriting existing library items, persists run state for retries, and updates
playlist coverage. It performs NO DRM circumvention and never treats ClearKey
laboratory observations as proof that Widevine streams can be decrypted.

Key decisions:

* Authorized access = the operator's ``YTDLP_COOKIE_FILE`` (Netscape format),
  referenced only by variable name / signature. Cookie *contents* are never
  read, logged, echoed or stored here.
* YouTube audio is kept in its ORIGINAL lossy container UNCHANGED (no transcode
  to FLAC); provenance records it as ``quality="lossy"`` / not-lossless.
* SoundCloud fetches the ORIGINAL uploaded file when the owner enabled
  downloads for that track, otherwise the best available stream. No netrc / no
  stored SoundCloud credentials for this feature.
* Diagnostic reports from external/lab stream probes are sanitized via
  ``sanitize_diagnostic_report`` so secret keys, cookies, and license payloads
  can never enter logs, sidecars, or persisted state.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from backend.app_runtime import (
    AUDIO_EXT,
    MUSIC_ROOT,
    PLAYLIST_MANIFESTS_DIR,
    _norm,
    _redact_security_text,
    _s,
    validated_downloads_root,
)
from backend.audio_preferences import ffmpeg_file_input, inspect_audio_file
from backend.auth_service import _yt_auth_source_failed_message, _yt_bot_check_message
from backend.acquisition_service import _download_dest_under, _download_folder_segment
from backend.beets_adapter import beets_adapter
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
# Small absolute slack for container-vs-stream rounding only. Kept well under a
# second so a sub-2s truncation of a short item cannot pass the gate (F9).
_RECOVER_DURATION_ABS_EPSILON = 0.5

# yt-dlp bestaudio / SoundCloud original can land in containers AUDIO_EXT does
# not list (notably bare .aac, .oga). Gate output discovery on this superset so
# a healthy original is never reported "no audio produced" (F10).
_RECOVER_AUDIO_EXT = frozenset(AUDIO_EXT) | {".aac", ".oga"}

# Codecs/containers that carry lossless audio. Anything else (mp3, aac, opus,
# vorbis, ...) is lossy even when it is the owner's "original" upload (F4).
_LOSSLESS_CODECS = frozenset({
    "flac", "alac", "wav", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le",
    "aiff", "ape", "wavpack", "wv", "tak", "tta", "dsd", "dsf", "dff",
})
_LOSSLESS_EXTS = frozenset({
    ".flac", ".alac", ".wav", ".aiff", ".aif", ".ape", ".wv", ".tak", ".tta",
    ".dsf", ".dff",
})

# Provenance status vocabulary shared across probe, fetch, import, and Phase 3
# diagnostic report sanitization.
RECOVER_PROVENANCE_STATES = frozenset({
    "accessible",
    "auth-needed",
    "unsupported-format",
    "genuinely-DRM",
    "unavailable",
    "recovered",
    "imported",
})

# Allowed EME MediaKeyStatus values (W3C EME spec); never includes key IDs or key bytes.
_ALLOWED_EME_KEY_STATUSES = frozenset({
    "usable",
    "expired",
    "released",
    "output-restricted",
    "output-downscaled",
    "status-pending",
    "internal-error",
})

# Forbidden secret / cryptographic / credential field names stripped by
# sanitize_diagnostic_report (Phase 3 research compatibility).
_FORBIDDEN_DIAGNOSTIC_KEYS = frozenset({
    "key",
    "keys",
    "k",
    "kid",
    "kids",
    "kid_key_pair",
    "kid_key_pairs",
    "content_key",
    "content_keys",
    "session_key",
    "session_keys",
    "private_key",
    "device_private_key",
    "client_id_blob",
    "license",
    "license_payload",
    "license_request",
    "license_response",
    "raw_license",
    "pssh",
    "pssh_raw",
    "pssh_box",
    "init_data",
    "initdata",
    "cookie",
    "cookies",
    "set_cookie",
    "set-cookie",
    "authorization",
    "bearer",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "password",
    "recovered_keys",
    "decrypted_keys",
    "clearkeys",
})


def _as_list(value):
    """F-9: tolerate malformed diagnostic fields — only real lists/tuples iterate."""
    return list(value) if isinstance(value, (list, tuple)) else []


def _codec_is_lossless(codec: Any, ext: Any) -> bool:
    c = _s(codec).strip().lower()
    e = _s(ext).strip().lower()
    if e and not e.startswith("."):
        e = "." + e
    return c in _LOSSLESS_CODECS or e in _LOSSLESS_EXTS


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


# ── streaming-format, HLS/DASH/SABR manifest, and DRM/EME diagnostics ──
def _format_has_drm(fmt: Dict[str, Any]) -> bool:
    if not isinstance(fmt, dict):
        return False
    drm_val = fmt.get("has_drm") or fmt.get("drm") or fmt.get("_has_drm")
    if drm_val is True:
        return True
    if isinstance(drm_val, str) and drm_val.strip().lower() not in ("", "no", "false", "none", "0"):
        return True
    note = _s(fmt.get("format_note") or "").lower()
    proto = _s(fmt.get("protocol") or "").lower()
    combined = f"{note} {proto}"
    return bool(
        "drm" in combined
        or "widevine" in combined
        or "playready" in combined
        or "fairplay" in combined
        or "cenc" in combined
        or re.search(r"\beme\b", combined)
    )


def _detect_key_systems_from_text(text: str) -> List[str]:
    low = _s(text).lower()
    found = set()
    if "widevine" in low or "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed" in low:
        found.add("widevine")
    if "playready" in low or "9a04f079-9840-4286-ab92-e65be0885f95" in low:
        found.add("playready")
    if "fairplay" in low or "94ce86fb-07ff-4f43-adb8-93d2fa968ca2" in low:
        found.add("fairplay")
    if "clearkey" in low or "org.w3.clearkey" in low or "1077efec-c0b2-4d02-ace3-3c1e52e2fb4b" in low:
        found.add("clearkey")
    if "cenc" in low or "cbcs" in low:
        found.add("cenc")
    return sorted(found)


def _format_has_audio(fmt: Dict[str, Any]) -> bool:
    if not isinstance(fmt, dict):
        return False
    acodec = _s(fmt.get("acodec") or "").strip().lower()
    if acodec == "none":
        return False
    if acodec:
        return True
    ext = _s(fmt.get("ext") or "").strip().lower()
    if ext and not ext.startswith("."):
        ext = "." + ext
    vcodec = _s(fmt.get("vcodec") or "").strip().lower()
    if ext in _RECOVER_AUDIO_EXT and vcodec in ("", "none"):
        return True
    if _s(fmt.get("format_id") or "").strip().lower() == "download":
        return True
    return False


def _transport_category(fmt: Dict[str, Any]) -> str:
    proto = _s(fmt.get("protocol") or "").strip().lower()
    manifest_url = _s(fmt.get("manifest_url") or "").strip().lower()
    note = _s(fmt.get("format_note") or "").strip().lower()
    if "sabr" in proto or "sabr" in note:
        return "sabr"
    if "m3u8" in proto or "hls" in proto or manifest_url.endswith(".m3u8") or ".m3u8?" in manifest_url:
        return "hls"
    if "dash" in proto or manifest_url.endswith(".mpd") or ".mpd?" in manifest_url or "dash" in note:
        return "dash"
    if proto in ("https", "http", ""):
        return "progressive"
    return proto


def select_best_audio_format(
    formats: Iterable[Dict[str, Any]],
    *,
    platform: str = "",
) -> Optional[Dict[str, Any]]:
    """Select the highest-quality accessible (non-DRM) audio format from a
    probed format list without transcoding."""
    candidates: List[Tuple[Tuple[Any, ...], Dict[str, Any]]] = []
    plat = _s(platform).strip().lower()
    for raw in formats or []:
        if not isinstance(raw, dict):
            continue
        if _format_has_drm(raw) or not _format_has_audio(raw):
            continue
        fmt_id = _s(raw.get("format_id"))
        ext = _s(raw.get("ext")).lower()
        acodec = _s(raw.get("acodec")).lower()
        vcodec = _s(raw.get("vcodec")).lower()
        proto = _s(raw.get("protocol")).lower()
        note = _s(raw.get("format_note"))
        transport = _transport_category(raw)
        lossless = _codec_is_lossless(acodec, ext)
        is_owner_orig = 1 if (plat == "soundcloud" and (fmt_id.lower() == "download" or "original" in note.lower())) else 0
        is_audio_only = 1 if vcodec in ("", "none") else 0
        transport_rank = {"progressive": 3, "hls": 2, "dash": 2, "sabr": 1}.get(transport, 0)
        if lossless:
            codec_rank = 4
        elif "opus" in acodec:
            codec_rank = 3
        elif any(k in acodec for k in ("aac", "mp4a", "vorbis")):
            codec_rank = 2
        elif "mp3" in acodec or ext == "mp3":
            codec_rank = 1
        else:
            codec_rank = 0
        try:
            abr = float(raw.get("abr") or raw.get("tbr") or 0.0)
        except (TypeError, ValueError):
            abr = 0.0
        try:
            asr = int(raw.get("asr") or 0)
        except (TypeError, ValueError):
            asr = 0
        # Rank quality (owner original, lossless, audio-only, codec, bitrate,
        # sample rate) ahead of transport preference so a low-bitrate
        # progressive stream never outranks a higher-quality DASH/HLS stream.
        rank_key = (
            is_owner_orig,
            1 if lossless else 0,
            is_audio_only,
            codec_rank,
            abr,
            asr,
            transport_rank,
        )
        summary = {
            "format_id": fmt_id,
            "ext": ext,
            "acodec": acodec,
            "vcodec": vcodec or "none",
            "protocol": proto or "https",
            "transport": transport,
            "abr": abr if abr > 0 else raw.get("abr"),
            "asr": asr if asr > 0 else raw.get("asr"),
            "lossless": lossless,
            "format_note": note,
        }
        candidates.append((rank_key, summary))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


def inspect_stream_diagnostics(
    info: Optional[Dict[str, Any]],
    *,
    platform: str = "",
) -> Dict[str, Any]:
    """Inspect a yt-dlp info dictionary for streaming protocols, HLS/DASH/SABR
    manifest usage, DRM/EME protection flags, and accessible audio formats.

    Strictly metadata-level: never extracts keys, license payloads, or PSSH
    binary blobs.
    """
    data = info if isinstance(info, dict) else {}
    raw_formats = [f for f in _as_list(data.get("formats")) if isinstance(f, dict)]
    plat = _s(platform or _platform_for_host(_host_of(_s(data.get("webpage_url") or "")))).lower()

    protocols = set()
    manifest_types = set()
    key_systems = set()
    fragmented_count = 0
    audio_count = 0
    accessible_audio_count = 0
    drm_format_count = 0
    lossless_available = False

    top_drm = data.get("drm") or data.get("_has_drm")
    top_drm_bool = bool(
        top_drm is True
        or (isinstance(top_drm, str) and top_drm.strip().lower() not in ("", "no", "false", "none", "0"))
    )
    if isinstance(top_drm, str):
        key_systems.update(_detect_key_systems_from_text(top_drm))
    for fam in _as_list(data.get("drm_families")):
        key_systems.update(_detect_key_systems_from_text(_s(fam)))

    for fmt in raw_formats:
        proto = _s(fmt.get("protocol") or "").strip().lower()
        if proto:
            protocols.add(proto)
        transport = _transport_category(fmt)
        if transport in ("hls", "dash", "sabr", "progressive"):
            manifest_types.add(transport)
        if fmt.get("fragments") or transport in ("hls", "dash", "sabr"):
            fragmented_count += 1
        has_drm = _format_has_drm(fmt)
        if has_drm:
            drm_format_count += 1
            key_systems.update(
                _detect_key_systems_from_text(
                    f"{_s(fmt.get('drm'))} {_s(fmt.get('format_note'))} {_s(fmt.get('protocol'))}"
                )
            )
        if _format_has_audio(fmt):
            audio_count += 1
            if not has_drm:
                accessible_audio_count += 1
                if _codec_is_lossless(fmt.get("acodec"), fmt.get("ext")):
                    lossless_available = True

    if (top_drm_bool or drm_format_count > 0) and not key_systems:
        key_systems.add("unknown_drm")

    if (top_drm_bool and accessible_audio_count == 0) or (drm_format_count > 0 and accessible_audio_count == 0):
        drm_status = "protected"
    elif drm_format_count > 0 and accessible_audio_count > 0:
        drm_status = "partial"
    else:
        drm_status = "none"

    best_audio = select_best_audio_format(raw_formats, platform=plat)
    return {
        "protocols": sorted(protocols),
        "manifest_types": sorted(manifest_types),
        "has_hls": "hls" in manifest_types,
        "has_dash": "dash" in manifest_types,
        "has_sabr": "sabr" in manifest_types,
        "has_progressive": "progressive" in manifest_types,
        "fragmented_format_count": fragmented_count,
        "total_format_count": len(raw_formats),
        "audio_format_count": audio_count,
        "accessible_audio_format_count": accessible_audio_count,
        "drm_format_count": drm_format_count,
        "lossless_available": lossless_available,
        "drm_status": drm_status,
        "eme_detected": bool(drm_status != "none" or key_systems),
        "key_systems": sorted(key_systems),
        "best_audio_format": best_audio,
    }


# ── format / quality plan per the increment-1 & increment-2 decisions ──
_SAFE_FORMAT_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _recover_format_plan(host: str, probe: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Pick the highest *authorized* audio format and record its quality.

    YouTube: keep the original lossy container unchanged (``quality="lossy"``,
    not lossless). SoundCloud: prefer the owner-enabled original download
    (format_id ``download``), else best stream. We never transcode here, so no
    audio postprocessor is attached and the file is staged as downloaded.
    """
    formats = (probe or {}).get("formats") or []
    platform = _platform_for_host(host)
    best_audio = select_best_audio_format(formats, platform=platform)
    best_id = _s((best_audio or {}).get("format_id")).strip()
    if best_id and not _SAFE_FORMAT_ID_RE.match(best_id):
        best_id = ""
    if _is_youtube_host(host):
        base_yt = "bestaudio[acodec!=none][vcodec=none]/bestaudio[acodec!=none]/bestaudio/best"
        fmt_sel = f"{best_id}/{base_yt}" if best_id else base_yt
        return {
            "format": fmt_sel,
            "quality": "lossy",
            "lossless": False,
            "recommended_format": best_audio,
            "note": "YouTube audio kept in its original lossy container (not transcoded)",
        }
    if _is_soundcloud_host(host):
        original_fmt: Optional[Dict[str, Any]] = None
        for fmt in formats:
            if _s(fmt.get("format_id")).lower() == "download" or "original" in _s(fmt.get("format_note")).lower():
                original_fmt = fmt
                break
        # An owner-enabled original is only lossless when its codec/ext is
        # lossless (an original MP3/AAC is still lossy) (F4). Best-effort from
        # the probe; the fetch re-derives from the actual staged file.
        lossless = bool(original_fmt) and _codec_is_lossless(
            (original_fmt or {}).get("acodec"), (original_fmt or {}).get("ext"))
        sc_sel = (
            f"download/{best_id}/bestaudio/best"
            if (best_id and best_id.lower() != "download")
            else "download/bestaudio/best"
        )
        return {
            # "download" is SoundCloud's owner-enabled original file.
            "format": sc_sel,
            "quality": "original" if original_fmt else "stream",
            "lossless": lossless,
            "recommended_format": best_audio,
            "note": (
                "SoundCloud original upload"
                if original_fmt
                else "SoundCloud best available stream (owner download not enabled)"
            ),
        }
    return {
        "format": f"{best_id}/bestaudio/best" if best_id else "bestaudio/best",
        "quality": "original",
        "lossless": bool(best_audio and best_audio.get("lossless")),
        "recommended_format": best_audio,
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


def recover_auth_diagnostics(
    info: Optional[Dict[str, Any]] = None,
    error_text: str = "",
    *,
    cookie_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Provider authentication and session diagnostics for YouTube/SoundCloud
    recovery. Never reads, echoes, or logs cookie contents."""
    cstate = dict(cookie_info) if isinstance(cookie_info, dict) else recover_cookie_state()
    state = _s(cstate.get("state") or "none")
    data = info if isinstance(info, dict) else {}
    availability = _s(data.get("availability") or "").strip().lower()
    err_cat = classify_recover_error(error_text).get("category", "") if error_text else ""
    auth_required = bool(
        err_cat in ("auth-needed", "access-restricted")
        or availability in ("private", "needs_auth", "subscriber_only", "premium_only", "unlisted")
    )
    session_status_map = {
        "ok": "ready",
        "rejected": "expired_or_rejected_cookie",
        "invalid": "invalid_cookie",
        "none": "missing_cookie",
    }
    session_status = session_status_map.get(state, "missing_cookie")
    if auth_required and state == "ok" and err_cat in ("auth-needed", "access-restricted"):
        session_status = "access_denied"
        remediation = (
            "Cookies are loaded, but the provider still denied access; verify the cookie file "
            "belongs to the account that owns this upload."
        )
    else:
        remediation = _s(cstate.get("action") or "")
    return {
        "cookie_state": state,
        "configured": bool(cstate.get("configured")),
        "signature": cstate.get("signature"),
        "authenticated_session_likely": state == "ok",
        "auth_required_by_target": auth_required,
        "availability": availability or "public",
        "session_status": session_status,
        "message": _s(cstate.get("message") or ""),
        "remediation": remediation,
    }


def classify_media_provenance(
    *,
    probe: Optional[Dict[str, Any]] = None,
    error_category: str = "",
    staged: bool = False,
    imported: bool = False,
) -> str:
    """Classify a media item's recovery provenance into a canonical state:
    ``accessible``, ``auth-needed``, ``unsupported-format``, ``genuinely-DRM``,
    ``unavailable``, ``recovered``, or ``imported``."""
    if imported:
        return "imported"
    if staged:
        return "recovered"

    cat = _s(error_category or (probe or {}).get("category") or "").strip()
    diag = (probe or {}).get("stream_diagnostics") if isinstance(probe, dict) else None
    if not isinstance(diag, dict) and isinstance(probe, dict) and isinstance(probe.get("formats"), list):
        diag = inspect_stream_diagnostics(probe)
    drm_status = _s((diag or {}).get("drm_status") or "none")

    if cat == "genuinely-DRM" or drm_status == "protected":
        return "genuinely-DRM"
    if cat == "auth-needed":
        return "auth-needed"
    if cat in ("expired-url", "access-restricted"):
        return "unavailable"
    if cat in ("format", "hls-assembly"):
        return "unsupported-format"

    if isinstance(probe, dict) and probe.get("ok"):
        if probe.get("is_collection"):
            return "accessible"
        total_formats = int((diag or {}).get("total_format_count") or 0)
        accessible_audio = int((diag or {}).get("accessible_audio_format_count") or 0)
        if total_formats > 0 and accessible_audio == 0:
            return "genuinely-DRM" if drm_status != "none" else "unsupported-format"
        return "accessible"

    if cat:
        return "unavailable"
    return "accessible"


# ── Phase 3: Research compatibility & sanitized diagnostic report format ──
_PEM_OR_SECRET_VALUE_RE = re.compile(
    r"(-----BEGIN [A-Z0-9 ]+-----|"
    r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"^\s*#?HttpOnly_)",
    re.IGNORECASE,
)


def _redaction_category(key_low: str) -> str:
    """F3a: a fixed label for redacted_fields so an attacker-chosen field
    NAME is never echoed back in the sanitized output."""
    if "cookie" in key_low:
        return "cookie"
    if "license" in key_low:
        return "license"
    if "token" in key_low or "bearer" in key_low or "authorization" in key_low or "password" in key_low:
        return "credential"
    if "secret" in key_low:
        return "secret"
    return "key_material"


def _strip_secret_fields_recursive(obj: Any, redacted: set, *, parent_key: str = "") -> Any:
    if isinstance(obj, dict):
        cleaned: Dict[str, Any] = {}
        for k, v in obj.items():
            key_str = _s(k).strip()
            key_low = key_str.lower()
            if (
                key_low in _FORBIDDEN_DIAGNOSTIC_KEYS
                or "secret" in key_low
                or "private_key" in key_low
                or "content_key" in key_low
                or "session_key" in key_low
                or ("cookie" in key_low and key_low != "cookie_state")
                or "license_payload" in key_low
                or "license_response" in key_low
                or "license_request" in key_low
            ):
                redacted.add(_redaction_category(key_low))
                continue
            cleaned[key_str] = _strip_secret_fields_recursive(v, redacted, parent_key=key_low)
        return cleaned
    if isinstance(obj, list):
        return [_strip_secret_fields_recursive(item, redacted, parent_key=parent_key) for item in obj[:100]]
    if isinstance(obj, str):
        # Bound the scan length before matching (a secret marker appears at the
        # start of a value); avoids pathological backtracking on huge inputs.
        if _PEM_OR_SECRET_VALUE_RE.search(obj[:4096]):
            redacted.add("inline_secret_value")
            return "[REDACTED]"
        return _redact_security_text(obj)[:500]
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return _redact_security_text(_s(obj))[:200]


def sanitize_diagnostic_report(raw_report: Any) -> Dict[str, Any]:
    """Consume an external or laboratory streaming/EME diagnostic observation
    and return a sanitized Beets diagnostic summary.

    Security invariants (Phase 3):
    * Strips all secret keys, KID/key pairs, raw license payloads, PSSH blobs,
      cookies, and credentials.
    * Never treats ClearKey laboratory interception as proof that commercial
      Widevine streams can be decrypted (``widevine_decryptable`` and
      ``decryption_supported`` are unconditionally ``False``).
    """
    if not isinstance(raw_report, dict):
        return {
            "ok": False,
            "schema": "beets.recover.diagnostic.v1",
            "error": "diagnostic report must be a JSON object",
            "decryption_supported": False,
            "widevine_decryptable": False,
        }

    redacted: set = set()
    cleaned = _strip_secret_fields_recursive(raw_report, redacted)

    source_url = _sanitize_persisted_url(
        _s(cleaned.get("source_url") or cleaned.get("webpage_url") or cleaned.get("url") or "").strip()
    )
    host = _host_of(source_url)
    platform = _s(cleaned.get("source_platform") or cleaned.get("platform") or _platform_for_host(host)).lower()

    # Only CANONICAL key-system ids are ever emitted (never the raw string), so
    # an attacker-controlled value in key_systems/keySystem cannot leak out.
    key_systems = set()
    for ks in _as_list(cleaned.get("key_systems") or cleaned.get("keySystems")):
        key_systems.update(_detect_key_systems_from_text(_s(ks)))
    key_systems.update(
        _detect_key_systems_from_text(_s(cleaned.get("key_system") or cleaned.get("keySystem") or "")))

    session_types = set()
    for st in _as_list(cleaned.get("session_types") or cleaned.get("sessionTypes")):
        st_text = _s(st).strip().lower()
        if st_text in ("temporary", "persistent-license", "persistent-usage-record"):
            session_types.add(st_text)

    message_types = set()
    for mt in _as_list(cleaned.get("message_types") or cleaned.get("messageTypes")):
        mt_text = _s(mt).strip().lower()
        if mt_text in ("license-request", "license-renewal", "license-release", "individualization-request"):
            message_types.add(mt_text)

    key_statuses = set()
    raw_statuses = raw_report.get("key_statuses") or raw_report.get("keyStatuses") or []
    if isinstance(raw_statuses, dict):
        # Map of kid -> status: keep ONLY the allowed status value, strip KID!
        redacted.add("key_statuses.kid")
        raw_statuses = list(raw_statuses.values())
    if isinstance(raw_statuses, list):
        for item in raw_statuses:
            if isinstance(item, dict):
                if any(k in item for k in ("kid", "key", "k")):
                    redacted.add("key_statuses.entry_key")
                status_val = _s(item.get("status") or "").strip().lower()
            else:
                status_val = _s(item).strip().lower()
            if status_val in _ALLOWED_EME_KEY_STATUSES:
                key_statuses.add(status_val)

    # EME lifecycle events are read from the CLEANED report (secrets already
    # stripped by field name); only canonical key-system ids and allowlisted
    # status/message tokens are kept, never raw event strings.
    for ev in _as_list(cleaned.get("events"))[:200]:
        if not isinstance(ev, dict):
            continue
        key_systems.update(
            _detect_key_systems_from_text(_s(ev.get("keySystem") or ev.get("key_system") or "")))
        msg_type = _s(ev.get("messageType") or ev.get("message_type") or "").strip().lower()
        if msg_type in ("license-request", "license-renewal", "license-release", "individualization-request"):
            message_types.add(msg_type)
        for st_val in _as_list(ev.get("statuses") or ev.get("keyStatuses")):
            if isinstance(st_val, dict):
                s_name = _s(st_val.get("status") or "").strip().lower()
            else:
                s_name = _s(st_val).strip().lower()
            if s_name in _ALLOWED_EME_KEY_STATUSES:
                key_statuses.add(s_name)

    stream_diag = inspect_stream_diagnostics(cleaned, platform=platform)
    for ks in stream_diag.get("key_systems") or []:
        key_systems.add(ks)

    eme_detected = bool(
        cleaned.get("eme_detected")
        or stream_diag.get("eme_detected")
        or key_systems
        or message_types
        or key_statuses
    )

    non_clearkey_drm = any(ks in ("widevine", "playready", "fairplay") for ks in key_systems)
    raw_prov = _s(cleaned.get("provenance_status") or "").strip()
    if raw_prov in RECOVER_PROVENANCE_STATES:
        provenance_status = raw_prov
    elif non_clearkey_drm and int(stream_diag.get("accessible_audio_format_count") or 0) == 0:
        provenance_status = "genuinely-DRM"
    else:
        provenance_status = classify_media_provenance(
            probe={"ok": True, "stream_diagnostics": stream_diag, "formats": cleaned.get("formats") or []},
            error_category=_s(cleaned.get("category") or ""),
        )

    return {
        "ok": True,
        "schema": "beets.recover.diagnostic.v1",
        "source_url": source_url,
        "source_platform": platform,
        "eme_detected": eme_detected,
        "key_systems": sorted(key_systems),
        "session_types": sorted(session_types),
        "message_types": sorted(message_types),
        "key_statuses": sorted(key_statuses),
        "manifest_types": stream_diag.get("manifest_types") or [],
        "protocols": stream_diag.get("protocols") or [],
        "provenance_status": provenance_status,
        "decryption_supported": False,
        "widevine_decryptable": False,
        "security_note": (
            "Diagnostic metadata only; secret keys, cookies, and license payloads are stripped. "
            "ClearKey laboratory observations do not imply commercial Widevine decryptability."
        ),
        "redacted_fields": sorted(redacted),
    }


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

    # Auth first: owner-only content needs the operator's cookies. 401 /
    # Unauthorized / "entitlement required" / "sign in" are authorization
    # failures, not DRM (F1) -- classify them positively here, ahead of the DRM
    # branch, so they can never fall through and be mislabeled.
    if (
        _yt_auth_source_failed_message(raw)
        or _yt_bot_check_message(raw)
        or "private video" in text
        or "login required" in text
        or "sign in" in text
        or "unauthorized" in text
        or "http error 401" in text
        or "error 401" in text
        or "entitlement required" in text
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

    if (
        "m3u8" in text or "hls" in text or "fragment" in text
        or "ffmpeg exited" in text or "manifest" in text
    ):
        return result("hls-assembly", "Transient HLS/stream assembly failure; retry the fetch.")

    if "requested format is not available" in text or "no video formats" in text or "no suitable format" in text or "requested format" in text:
        return result("format", "Adjust the requested format or try again; the stream format changed.")

    # DRM only on real EME/CDM evidence. "eme" is matched as a standalone token
    # only, so "sch-EME"/"placement"/"requirement"/"agreement"/"Element" never
    # trip it. Auth/format/HLS are already classified above and cannot reach
    # here (F1).
    if (
        "widevine" in text
        or "fairplay" in text
        or "playready" in text
        or "encrypted media" in text
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
    ``_soundcloud_duration_ok``). The band is proportional (``tolerance``) plus a
    sub-second absolute epsilon for container rounding only -- NOT a 2 s floor,
    so a sub-2s truncation of a short item is still rejected (F9). Unknown
    ``expected`` cannot be checked here; the caller decides."""
    try:
        a = float(actual)
        e = float(expected)
    except (TypeError, ValueError):
        return False
    if e <= 0:
        return False
    slack = e * tolerance + _RECOVER_DURATION_ABS_EPSILON
    return abs(a - e) <= slack


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


def recover_probe(url: str, *, ydl_module: Any = None,
                  diagnostic_report: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Metadata/format probe (``extract_info(download=False)``) for one
    allowlisted URL, plus stream/manifest/DRM diagnostics, auth diagnostics,
    and the selected authorized format plan. Never downloads."""
    url = _s(url).strip()
    try:
        check_ytdlp_target(url)
    except YtdlpTargetRejected as exc:
        return {
            "ok": False,
            "error": _s(exc),
            "category": "misconfig",
            "provenance_status": "unavailable",
        }
    host = _host_of(url)
    platform = _platform_for_host(host)
    opts = _recover_base_opts(host)
    auth = _recover_apply_owner_cookies(opts)
    try:
        yt_dlp = _yt_dlp_module(ydl_module)
        with yt_dlp.YoutubeDL(ytdlp_guarded_options(opts, [url])) as ydl:
            info = ydl.extract_info(url, download=False)
    except YtdlpTargetRejected as exc:
        return {
            "ok": False,
            "error": _s(exc),
            "category": "misconfig",
            "provenance_status": "unavailable",
        }
    except Exception as exc:  # noqa: BLE001 — classified below
        err_info = classify_recover_error(exc)
        prov = classify_media_provenance(error_category=err_info["category"])
        return {
            "ok": False,
            **err_info,
            "provenance_status": prov,
            "auth_diagnostics": recover_auth_diagnostics(error_text=_s(exc)),
        }
    if not isinstance(info, dict):
        return {
            "ok": False,
            "error": "no metadata returned",
            "category": "unknown",
            "provenance_status": "unavailable",
        }

    # A channel/user/playlist/set resolves to a collection, not one item. Flag
    # it so the single-item fetch path can refuse it and ask for enumerate
    # first; a single video with a &list= param stays single (noplaylist=True).
    if info.get("_type") == "playlist" or isinstance(info.get("entries"), list):
        return {
            "ok": True,
            "is_collection": True,
            "platform": platform,
            "entry_count": len(info.get("entries") or []),
            "webpage_url": _s(info.get("webpage_url") or url),
            "title": _s(info.get("title"))[:300],
            "uploader": _s(info.get("uploader") or info.get("channel"))[:200],
            "provenance_status": "accessible",
        }

    formats = [
        {
            "format_id": _s(f.get("format_id")),
            "ext": _s(f.get("ext")),
            "acodec": _s(f.get("acodec")),
            "vcodec": _s(f.get("vcodec")),
            "abr": f.get("abr"),
            "tbr": f.get("tbr"),
            "asr": f.get("asr"),
            "protocol": _s(f.get("protocol")),
            "manifest_url": _s(f.get("manifest_url"))[:300] if f.get("manifest_url") else "",
            "has_drm": _format_has_drm(f),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "format_note": _s(f.get("format_note")),
        }
        for f in (info.get("formats") or []) if isinstance(f, dict)
    ]
    stream_diag = inspect_stream_diagnostics(info, platform=platform)
    plan = _recover_format_plan(host, {"formats": formats})
    provenance_status = classify_media_provenance(
        probe={"ok": True, "is_collection": False, "formats": formats, "stream_diagnostics": stream_diag}
    )
    out: Dict[str, Any] = {
        "ok": True,
        "is_collection": False,
        "platform": platform,
        "title": _s(info.get("title"))[:300],
        "uploader": _s(info.get("uploader") or info.get("channel") or info.get("uploader_id"))[:200],
        "duration": info.get("duration"),
        "webpage_url": _s(info.get("webpage_url") or url),
        "formats": formats,
        "selected_format": plan["format"],
        "recommended_format": plan.get("recommended_format"),
        "quality": plan["quality"],
        "lossless": plan["lossless"],
        "authorized_access": bool(auth),
        "stream_diagnostics": stream_diag,
        "provenance_status": provenance_status,
        "auth_diagnostics": recover_auth_diagnostics(info=info),
    }
    if isinstance(diagnostic_report, dict):
        out["diagnostic_report"] = sanitize_diagnostic_report(diagnostic_report)
    return out


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
_SENSITIVE_URL_QUERY_KEYS = frozenset({
    "token", "access_token", "refresh_token", "id_token", "auth", "oauth_token",
    "sig", "signature", "key", "apikey", "api_key", "secret", "password",
    "pass", "credential", "credentials", "cookie", "cookies", "policy",
    "x-amz-signature", "x-amz-credential", "x-amz-security-token",
    "x-goog-signature", "hdnea", "hdnts", "session", "session_id",
})


def _sanitize_persisted_url(url: Any) -> str:
    """Strip userinfo and sensitive query parameters (tokens, signatures, keys)
    from a URL before persisting it to provenance sidecars or recover_runs.json."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    raw = _s(url).strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        if not parts.scheme or not parts.netloc:
            return _redact_security_text(raw)[:500]
        netloc = parts.netloc.rsplit("@", 1)[-1]
        clean_params = []
        for k, v in parse_qsl(parts.query, keep_blank_values=True):
            kl = k.strip().lower()
            if kl in _SENSITIVE_URL_QUERY_KEYS or any(
                sub in kl for sub in ("token", "secret", "sig", "auth", "key", "cookie", "pass", "cred")
            ):
                continue
            clean_params.append((k, v))
        new_query = urlencode(clean_params, doseq=True)
        return urlunsplit((parts.scheme, netloc, parts.path, new_query, ""))[:500]
    except Exception:
        return _redact_security_text(raw)[:500]


def _write_provenance(staged_file: str, record: Dict[str, Any]) -> str:
    """Persist provenance next to the staged file for a later import to consume.
    Never contains cookie contents, secret keys, or URL query tokens."""
    clean_rec = _strip_secret_fields_recursive(dict(record or {}), set())
    if isinstance(clean_rec, dict) and clean_rec.get("source_url"):
        clean_rec["source_url"] = _sanitize_persisted_url(clean_rec.get("source_url"))
    sidecar = str(Path(staged_file).with_suffix(Path(staged_file).suffix + ".recover.json"))
    data = json.dumps(clean_rec, ensure_ascii=False, indent=2).encode("utf-8")
    # O_NOFOLLOW: refuse to write THROUGH a symlink that may have been swapped in
    # for the sidecar (TOCTOU), so provenance can never clobber a file outside
    # staging. (No-op flag on platforms without it; the deploy target is Linux.)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(sidecar, flags, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except OSError:
        return ""
    return sidecar


# ── recovery-job persistence & retries (Phase 2 #7) ──
_RECOVER_RUNS_FILENAME = "recover_runs.json"
_RECOVER_RUNS_LOCK = threading.Lock()
_MAX_RECOVER_RUNS = 100
_ALLOWED_PERSISTED_RUN_KEYS = frozenset({
    "run_id",
    "job_id",
    "status",
    "cookie_state",
    "urls",
    "recovered_count",
    "failed_count",
    "imported_count",
    "skipped_existing_count",
    "import_failed_count",
    "recovered",
    "failed",
    "imported",
    "skipped_existing",
    "import_failed",
    "playlist_coverage",
    "retry_of",
    "retry_count",
    "created_at",
    "updated_at",
})


def _sanitize_persisted_run(run_record: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure persisted run records contain zero credentials, cookies, URL query
    tokens, or secret keys across all top-level and nested structures."""
    raw = _strip_secret_fields_recursive(dict(run_record or {}), set())
    if not isinstance(raw, dict):
        return {}
    rec: Dict[str, Any] = {
        k: v for k, v in raw.items() if k in _ALLOWED_PERSISTED_RUN_KEYS
    }
    if isinstance(rec.get("urls"), list):
        rec["urls"] = [
            _sanitize_persisted_url(u)
            for u in rec["urls"]
            if _sanitize_persisted_url(u)
        ]
    for list_key in ("recovered", "failed", "imported", "skipped_existing", "import_failed"):
        items = rec.get(list_key)
        if not isinstance(items, list):
            continue
        cleaned_items: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            c_item = dict(item)
            for url_field in ("url", "source_url", "webpage_url"):
                if url_field in c_item and c_item[url_field]:
                    c_item[url_field] = _sanitize_persisted_url(c_item[url_field])
            for text_field in ("message", "error", "action"):
                if text_field in c_item and c_item[text_field]:
                    c_item[text_field] = _redact_security_text(_s(c_item[text_field]))[:400]
            cleaned_items.append(c_item)
        rec[list_key] = cleaned_items
    return rec


def _upsert_recover_run(run_record: Dict[str, Any]) -> Dict[str, Any]:
    """Atomically persist or update a recovery run record in WEB_MANAGER_DATA_DIR
    using WebManagerConfigStore."""
    from backend.web_manager_config_store import WebManagerConfigStoreError, get_config_store

    clean_run = _sanitize_persisted_run(run_record)
    run_id = _s(clean_run.get("run_id")).strip()
    if not run_id:
        return clean_run

    with _RECOVER_RUNS_LOCK:
        try:
            store = get_config_store()
            for _attempt in range(3):
                existing_rec = store.read_text_record(_RECOVER_RUNS_FILENAME)
                rev = existing_rec.get("revision") if existing_rec.get("exists") else None
                raw_text = existing_rec.get("content")
                data = {"runs": []}
                if raw_text:
                    try:
                        parsed = json.loads(raw_text)
                        if isinstance(parsed, dict) and isinstance(parsed.get("runs"), list):
                            data = parsed
                    except Exception:
                        data = {"runs": []}
                runs: List[Dict[str, Any]] = [
                    r for r in data.get("runs") or [] if isinstance(r, dict) and _s(r.get("run_id"))
                ]
                replaced = False
                for idx, existing in enumerate(runs):
                    if _s(existing.get("run_id")) == run_id:
                        merged = {**existing, **clean_run, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
                        runs[idx] = _sanitize_persisted_run(merged)
                        clean_run = runs[idx]
                        replaced = True
                        break
                if not replaced:
                    now_str = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                    clean_run.setdefault("created_at", now_str)
                    clean_run["updated_at"] = now_str
                    runs.insert(0, clean_run)
                runs = runs[:_MAX_RECOVER_RUNS]
                try:
                    store.save_json(
                        _RECOVER_RUNS_FILENAME,
                        {"version": 1, "runs": runs},
                        is_secret=False,
                        expected_revision=rev,
                    )
                    break
                except WebManagerConfigStoreError:
                    continue
        except Exception:
            pass
    return clean_run


def recover_list_runs(*, limit: int = 50) -> Dict[str, Any]:
    """Return persisted recovery runs (most recent first)."""
    from backend.web_manager_config_store import get_config_store

    bounded_limit = max(1, min(int(limit or 50), _MAX_RECOVER_RUNS))
    with _RECOVER_RUNS_LOCK:
        try:
            data = get_config_store().load_json(_RECOVER_RUNS_FILENAME, default={"runs": []})
        except Exception:
            data = {"runs": []}
    runs = [
        _sanitize_persisted_run(r)
        for r in ((data or {}).get("runs") or [])
        if isinstance(r, dict) and _s(r.get("run_id"))
    ]
    return {
        "ok": True,
        "total": len(runs),
        "runs": runs[:bounded_limit],
    }


def recover_get_run(run_id: str) -> Optional[Dict[str, Any]]:
    """Lookup a single persisted recovery run by ``run_id``."""
    target = _s(run_id).strip()
    if not target:
        return None
    listing = recover_list_runs(limit=_MAX_RECOVER_RUNS)
    for run in listing.get("runs") or []:
        if _s(run.get("run_id")) == target:
            return run
    return None


# ── fetch + stage job ──
def _recover_stage_dir(platform: str, run_id: str) -> str:
    """Per-run staging dir: DOWNLOADS_ROOT/recovered/<platform>/<run_id>. The
    run_id segment isolates concurrent recover-fetch jobs so two jobs on the
    same platform can never select, delete or mislabel each other's files
    (F8 / staging race)."""
    root = validated_downloads_root()
    segment = _download_folder_segment(platform) or "other"
    safe_run = _download_folder_segment(run_id) or "run"
    return str(_download_dest_under(root, "recovered", segment, safe_run))


def _audio_files_in(dest_dir: str) -> List[str]:
    """Audio files directly in ``dest_dir`` (a per-run isolated dir), by the
    recover extension superset (F10)."""
    try:
        return sorted(
            str(p) for p in Path(dest_dir).iterdir()
            if p.is_file() and p.suffix.lower() in _RECOVER_AUDIO_EXT
        )
    except OSError:
        return []


def _has_partial_file(dest_dir: str) -> bool:
    try:
        return any(
            p.is_file() and (p.suffix.lower() in (".part", ".ytdl") or p.name.lower().endswith(".part"))
            for p in Path(dest_dir).iterdir()
        )
    except OSError:
        return False


def _recover_fetch_one(url: str, log: list, *, run_id: str = "",
                       ydl_module: Any = None) -> Dict[str, Any]:
    url = _s(url).strip()
    run_id = _s(run_id).strip() or uuid.uuid4().hex
    try:
        check_ytdlp_target(url)
    except YtdlpTargetRejected as exc:
        log.append(f"  [recover] Rejected target: {exc}")
        return {"url": url, "recovered": False, "category": "misconfig",
                "provenance_status": "unavailable",
                "action": "Use an allowlisted public YouTube/SoundCloud URL.", "message": _s(exc)}

    host = _host_of(url)
    platform = _platform_for_host(host)
    if _is_youtube_host(host):
        try:
            _require_ytdlp_js_runtime()
        except RuntimeError as exc:
            fail_info = _fail_from(exc)
            return {"url": url, "recovered": False,
                    "provenance_status": classify_media_provenance(error_category=fail_info["category"]),
                    **fail_info}

    probe = recover_probe(url, ydl_module=ydl_module)
    if not probe.get("ok"):
        cat = _s(probe.get("category") or "unknown")
        return {"url": url, "recovered": False,
                "category": cat,
                "provenance_status": _s(probe.get("provenance_status")) or classify_media_provenance(error_category=cat),
                "action": classify_recover_error(probe.get("error") or probe.get("message") or "").get("action", ""),
                "message": _s(probe.get("error") or probe.get("message"))}
    # A collection URL (channel/user/playlist/set) is refused in the single-item
    # path: enumerate it first, then fetch each item URL, so every child is
    # guard-checked and individually integrity/provenance-gated (F3).
    if probe.get("is_collection"):
        log.append(f"  [recover] Refusing collection URL in fetch: {_sanitize_persisted_url(url) or url}")
        return {"url": url, "recovered": False, "category": "collection",
                "provenance_status": "accessible",
                "action": "Enumerate this channel/playlist first, then fetch the individual item URLs.",
                "message": f"URL is a collection of {probe.get('entry_count', 0)} item(s), not a single upload"}

    stream_diag = probe.get("stream_diagnostics") if isinstance(probe.get("stream_diagnostics"), dict) else {}
    if stream_diag.get("drm_status") == "protected":
        log.append(f"  [recover] Refusing DRM-protected stream ({', '.join(stream_diag.get('key_systems') or ['DRM'])}): {_sanitize_persisted_url(url) or url}")
        return {
            "url": url,
            "recovered": False,
            "category": "genuinely-DRM",
            "provenance_status": "genuinely-DRM",
            "stream_diagnostics": stream_diag,
            "action": "This item is DRM/EME protected; recover it from the account owner's own source, not here.",
            "message": "all available streams are DRM/EME protected",
        }

    plan = _recover_format_plan(host, probe)
    expected_duration = probe.get("duration")

    stage_dir = _recover_stage_dir(platform, run_id)
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
        # A single-item URL only; a stray &list= never expands to a playlist.
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

    log.append(f"  [recover] Fetching {_sanitize_persisted_url(url) or url} ({plan['note']})")
    try:
        yt_dlp = _yt_dlp_module(ydl_module)
        with yt_dlp.YoutubeDL(ytdlp_guarded_options(opts, [url])) as ydl:
            ydl.download([url])
    except YtdlpTargetRejected as exc:
        return {"url": url, "recovered": False, "category": "misconfig",
                "provenance_status": "unavailable",
                "action": "Use an allowlisted public URL.", "message": _s(exc)}
    except Exception as exc:  # noqa: BLE001
        # A crash after a complete download but before provenance is retried
        # safely below: the isolated dir is re-scanned for a finished file.
        if not _audio_files_in(stage_dir):
            fail_info = _fail_from(exc)
            return {"url": url, "recovered": False,
                    "provenance_status": classify_media_provenance(error_category=fail_info["category"]),
                    **fail_info}
        log.append(f"  [recover] Download reported an error but a staged file exists; validating it: {exc}")

    # Every audio file in the isolated run dir belongs to THIS run, so a scan
    # (not a before/after diff) finds the finished file even on a crash-retry
    # where it already exists (F5/F13), and never picks up another job's file
    # (F8). With noplaylist + collection refusal this is normally exactly one.
    produced = _audio_files_in(stage_dir)
    if not produced:
        if _has_partial_file(stage_dir):
            return {"url": url, "recovered": False, "category": "integrity",
                    "provenance_status": "unavailable",
                    "action": "Re-fetch; only a partial (.part) download remains.",
                    "message": "only a leftover partial download (.part) is present"}
        return {"url": url, "recovered": False, "category": "format",
                "provenance_status": "unsupported-format",
                "action": "No audio file was produced; check the requested format.",
                "message": "no audio file produced"}

    staged_ok: List[Dict[str, Any]] = []
    last_fail = ""
    for staged in produced:
        integrity = recover_integrity_check(staged, expected_duration)
        if not integrity.get("ok"):
            last_fail = _s(integrity.get("reason"))
            try:
                os.remove(staged)
            except OSError:
                pass
            log.append(f"  [recover] Integrity gate failed for {Path(staged).name}: {last_fail}")
            continue
        # Losslessness from the ACTUAL file's codec/ext, not a blanket flag (F4).
        probe_media = inspect_audio_file(staged)
        lossless = _codec_is_lossless(probe_media.get("codec"), Path(staged).suffix)
        quality = "original" if (plan["quality"] in ("original", "stream") and lossless) else plan["quality"]
        record = {
            "source_platform": platform,
            "source_url": probe.get("webpage_url") or url,
            "uploader": probe.get("uploader", ""),
            "title": probe.get("title", ""),
            "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z") or time.strftime("%Y-%m-%dT%H:%M:%S"),
            "authorized_access": bool(auth),
            "quality": quality,
            "lossless": lossless,
            "codec": _s(probe_media.get("codec")),
            "duration": integrity.get("duration"),
            "run_id": run_id,
            "provenance_status": "recovered",
            "stream_diagnostics": {
                "protocols": stream_diag.get("protocols") or [],
                "manifest_types": stream_diag.get("manifest_types") or [],
                "drm_status": stream_diag.get("drm_status") or "none",
            },
        }
        sidecar = _write_provenance(staged, record)
        if not sidecar:
            # Provenance is required: without it a later import cannot trust the
            # file, so the item FAILS and the orphan is removed (F11).
            last_fail = "could not persist provenance sidecar"
            try:
                os.remove(staged)
            except OSError:
                pass
            log.append(f"  [recover] Provenance write failed for {Path(staged).name}; discarded")
            continue
        log.append(f"  [recover] Staged {Path(staged).name} (quality={quality}, lossless={lossless})")
        staged_ok.append({
            "staged_file": staged,
            "provenance": sidecar,
            "quality": quality,
            "lossless": lossless,
            "duration": integrity.get("duration"),
            "title": probe.get("title", ""),
            "uploader": probe.get("uploader", ""),
        })

    if not staged_ok:
        return {"url": url, "recovered": False, "category": "integrity",
                "provenance_status": "unavailable",
                "action": "Re-fetch; the staged file was truncated, corrupt, or could not be recorded.",
                "message": last_fail or "no valid audio file could be staged"}

    first = staged_ok[0]
    return {
        "url": url,
        "recovered": True,
        "provenance_status": "recovered",
        "staged_file": first["staged_file"],
        "staged_files": [s["staged_file"] for s in staged_ok],
        "provenance": first["provenance"],
        "quality": first["quality"],
        "lossless": first["lossless"],
        "duration": first["duration"],
        "title": first.get("title", ""),
        "uploader": first.get("uploader", ""),
        "authorized_access": bool(auth),
    }


def _fail_from(exc: Any) -> Dict[str, str]:
    info = classify_recover_error(exc)
    return {"category": info["category"], "action": info["action"], "message": info["message"]}


# ── Phase 2 #8, #9, #10: Audio verification, AcoustID/MB matching, Beets import & playlist coverage ──
def _has_symlink_in_chain(path: Path) -> bool:
    for candidate in (path, *path.parents):
        try:
            if candidate.exists() and (candidate.is_symlink() or os.path.islink(str(candidate))):
                return True
        except OSError:
            return True
    return False


def _validate_staged_recover_file(staged_path: str) -> Tuple[Optional[Path], Optional[Dict[str, Any]], str]:
    """Verify ``staged_path`` is a regular, non-symlink audio file inside
    ``DOWNLOADS_ROOT/recovered`` (never under ``MUSIC_ROOT``) and has a valid
    ``.recover.json`` provenance sidecar."""
    raw = _s(staged_path).strip()
    if not raw or "\x00" in raw:
        return None, None, "staged_file path is required"
    p = Path(raw)
    if _has_symlink_in_chain(p):
        return None, None, "refusing symlinked staged path"
    try:
        resolved = p.resolve(strict=True)
    except OSError:
        return None, None, "staged audio file does not exist"
    if not resolved.is_file() or resolved.suffix.lower() not in _RECOVER_AUDIO_EXT:
        return None, None, "staged path is not a supported audio file"

    # Reject hard links: a freshly staged recovery file has a single link. A
    # hard link under recovered/ could point at a MUSIC_ROOT file (same inode,
    # different path, so resolve()/MUSIC_ROOT checks miss it); stamping tags on
    # it would mutate the library file in place, outside the transaction engine.
    try:
        if resolved.stat().st_nlink > 1:
            return None, None, "refusing staged file with multiple hard links"
    except OSError:
        return None, None, "cannot stat staged audio file"

    try:
        music_root = Path(MUSIC_ROOT).resolve()
        if resolved == music_root or music_root in resolved.parents:
            return None, None, "refusing path inside MUSIC_ROOT"
    except Exception:
        pass

    try:
        dl_root = Path(validated_downloads_root()).resolve()
        recovered_root = (dl_root / "recovered").resolve()
        if resolved != recovered_root and recovered_root not in resolved.parents:
            return None, None, "staged path must reside under DOWNLOADS_ROOT/recovered"
    except Exception as exc:
        return None, None, f"staging root validation failed: {exc}"

    sidecar_path = resolved.with_suffix(resolved.suffix + ".recover.json")
    if _has_symlink_in_chain(sidecar_path) or not sidecar_path.is_file():
        return None, None, "missing .recover.json provenance sidecar"
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except Exception:
        return None, None, "corrupt .recover.json provenance sidecar"
    if not isinstance(sidecar, dict) or not _s(sidecar.get("source_url")).strip():
        return None, None, "invalid .recover.json provenance sidecar"
    return resolved, sidecar, ""


def verify_and_match_staged_upload(
    staged_path: str,
    *,
    artist_hint: str = "",
    title_hint: str = "",
    album_hint: str = "",
) -> Dict[str, Any]:
    """Verify a staged upload's provenance sidecar and audio integrity, then
    resolve identity via AcoustID fingerprinting + MusicBrainz search with a
    safe upload-metadata singleton fallback for original/unreleased tracks."""
    from backend import provider_boundary
    from backend.acoustid_service import _audio_identity_decision
    from backend.playlist_service import _playlist_canonicalize_track
    from helpers_mb import _mb_recording_search

    resolved, sidecar, err = _validate_staged_recover_file(staged_path)
    if resolved is None or sidecar is None:
        return {"ok": False, "error": err, "staged_file": _s(staged_path)}

    expected_duration = sidecar.get("duration")
    integrity = recover_integrity_check(str(resolved), expected_duration)
    if not integrity.get("ok"):
        return {
            "ok": False,
            "error": f"integrity check failed: {integrity.get('reason')}",
            "staged_file": str(resolved),
        }

    media_info = inspect_audio_file(str(resolved))
    if not media_info.get("ok", True):
        return {
            "ok": False,
            "error": _s(media_info.get("error") or "audio inspection failed"),
            "staged_file": str(resolved),
        }

    raw_uploader = _s(artist_hint or sidecar.get("uploader") or "").strip()
    raw_title = _s(title_hint or sidecar.get("title") or resolved.stem).strip()
    canonical = _playlist_canonicalize_track({
        "artist": raw_uploader,
        "title": raw_title,
        "uploader_artist": not bool(artist_hint),
    })
    artist = _s(canonical.get("artist") or raw_uploader or "Unknown Artist").strip()
    title = _s(canonical.get("title") or raw_title or resolved.stem).strip()
    album = _s(album_hint or sidecar.get("album") or title).strip()

    # 1. AcoustID audio fingerprinting + canonical identity decision gate
    identity = _audio_identity_decision(
        str(resolved),
        expected_artist=artist,
        expected_title=title,
    )
    acoustid_status = _s(identity.get("acoustid_status") or "not_attempted").strip().lower()
    identity_status = _s(identity.get("identity_status") or "").strip().lower()
    final_action = _s(identity.get("final_action") or "").strip().lower()
    conflicts = [str(c) for c in (identity.get("conflicts") or []) if c]
    acoustid_mbid = _s(identity.get("mb_recording_id_candidate") or "").strip()
    acoustid_rgid = _s(identity.get("mb_release_group_id_candidate") or "").strip()
    acoustid_score = float(identity.get("acoustid_match_score") or 0.0)
    top_candidates = identity.get("candidates") if isinstance(identity.get("candidates"), list) else []
    top_cand = top_candidates[0] if top_candidates and isinstance(top_candidates[0], dict) else {}

    # Conflict/status token vocabularies. _audio_identity_decision emits
    # acoustid_-PREFIXED tokens for provider/toolchain problems
    # (acoustid_lookup_failed / acoustid_no_fpcalc / acoustid_no_api_key /
    # acoustid_unavailable / acoustid_failed) but BARE tokens when no identity
    # simply exists (no_acoustid_result / no_result / no_match). Normalise the
    # prefix so both shapes classify the same way.
    norm_conflicts = [c.strip().lower().removeprefix("acoustid_") for c in conflicts]

    # "No fingerprint identity exists" -> genuinely original / owner upload.
    # Benign: must fall through to the singleton fallback (never reject, never
    # review).
    no_match_tokens = {"no_acoustid_result", "no_result", "no_match"}
    # "Could not verify" -> provider down / no fpcalc / no api key. Must route to
    # REVIEW: never auto-import unverified audio, and never hard-reject on an
    # outage (a provider outage is not evidence of a wrong identity).
    unverifiable_tokens = {"lookup_failed", "no_fpcalc", "no_api_key", "unavailable", "not_attempted", "failed"}

    # Only a genuine fingerprint-vs-metadata contradiction is a blocking conflict.
    blocking_conflicts = [
        c for c, n in zip(conflicts, norm_conflicts)
        if n not in no_match_tokens and n not in unverifiable_tokens
    ]
    could_not_verify = (
        acoustid_status in unverifiable_tokens
        or any(n in unverifiable_tokens for n in norm_conflicts)
    )

    # Gate 1 (REJECT): respect a canonical reject/conflict decision or a genuine
    # mismatch conflict token. A could-not-verify outage must NOT land here.
    if final_action == "reject" or identity_status == "conflict" or acoustid_status == "mismatch" or blocking_conflicts:
        reason = ", ".join(blocking_conflicts) if blocking_conflicts else (acoustid_status or "identity_conflict")
        return {
            "ok": False,
            "status": "identity_conflict",
            "final_action": "reject",
            "identity_status": identity_status or "conflict",
            "acoustid_status": acoustid_status,
            "conflicts": blocking_conflicts,
            "error": f"audio fingerprint conflicts with expected upload identity ({reason})",
            "staged_file": str(resolved),
        }

    # Gate 2 (REVIEW): could-not-verify, ambiguity, or a bare "review" verdict
    # that is not one of the benign no-match statuses. "no_result"/"no_match" are
    # benign here so a genuinely-original upload falls through to the singleton
    # fallback below instead of being parked in review.
    if (
        acoustid_status in ("unavailable", "ambiguous", "candidate")
        or could_not_verify
        or (final_action == "review" and acoustid_status not in ("", "no_result", "no_match", "no_acoustid_result"))
    ):
        return {
            "ok": False,
            "status": "review_required",
            "final_action": "review",
            "identity_status": identity_status or "unverified",
            "acoustid_status": acoustid_status,
            "conflicts": conflicts,
            "error": f"audio identity requires manual review (acoustid_status={acoustid_status})",
            "staged_file": str(resolved),
        }

    mb_trackid = ""
    mb_releasegroupid = ""
    match_source = "upload_metadata_singleton"

    acoustid_accepted = (
        bool(acoustid_mbid)
        and acoustid_score > 0.80
        and (
            final_action == "accept"
            or identity_status == "verified"
            or acoustid_status in ("confirmed", "matched")
        )
    )
    if acoustid_accepted:
        mb_trackid = acoustid_mbid
        mb_releasegroupid = acoustid_rgid
        artist = _s(top_cand.get("artist") or artist).strip()
        title = _s(top_cand.get("title") or title).strip()
        album = _s(album_hint or top_cand.get("album") or album).strip()
        match_source = "acoustid"
    else:
        # 2. MusicBrainz recording lookup fallback when AcoustID has no hit.
        # Note signature: _mb_recording_search(title: str, artist: str, limit: int = 8).
        try:
            mb_hits = _mb_recording_search(title, artist, limit=5) if (artist and title) else []
        except provider_boundary.ProviderError as exc:
            return {
                "ok": False,
                "status": "review_required",
                "final_action": "review",
                "identity_status": "unverified",
                "acoustid_status": acoustid_status,
                "error": f"MusicBrainz provider unavailable during verification: {_s(exc)}",
                "staged_file": str(resolved),
            }
        except Exception:
            mb_hits = []
        for hit in mb_hits or []:
            if not isinstance(hit, dict):
                continue
            hit_id = _s(hit.get("mb_trackid") or hit.get("id") or "").strip()
            hit_artist = _s(hit.get("artist") or "").strip()
            hit_title = _s(hit.get("title") or "").strip()
            if hit_id and _norm(hit_title) == _norm(title) and (not artist or _norm(hit_artist) == _norm(artist)):
                mb_trackid = hit_id
                mb_releasegroupid = _s(hit.get("mb_releasegroupid") or "").strip()
                artist = hit_artist or artist
                title = hit_title or title
                album = _s(album_hint or hit.get("album") or album).strip()
                match_source = "musicbrainz_search"
                break

    lossless = _codec_is_lossless(media_info.get("codec") or sidecar.get("codec"), resolved.suffix)
    return {
        "ok": True,
        "staged_file": str(resolved),
        "provenance_file": str(resolved.with_suffix(resolved.suffix + ".recover.json")),
        "source_url": _s(sidecar.get("source_url")),
        "source_platform": _s(sidecar.get("source_platform")),
        "authorized_access": bool(sidecar.get("authorized_access")),
        "duration": integrity.get("duration"),
        "codec": _s(media_info.get("codec") or sidecar.get("codec")),
        "lossless": lossless,
        "quality": _s(sidecar.get("quality") or ("original" if lossless else "lossy")),
        "artist": artist,
        "title": title,
        "album": album,
        "mb_trackid": mb_trackid,
        "mb_releasegroupid": mb_releasegroupid,
        "match_source": match_source,
        "acoustid_status": acoustid_status or "not_attempted",
        "acoustid_score": acoustid_score,
        "identity_status": identity_status or ("verified" if acoustid_accepted else "unverified"),
        "final_action": final_action or ("accept" if acoustid_accepted else "singleton"),
    }


def _find_existing_library_conflict(
    artist: str,
    title: str,
    mb_trackid: str,
    *,
    adapter: Any = None,
) -> Optional[Dict[str, Any]]:
    """Return an existing Beets library item dict if the track is already in the
    library (by ``mb_trackid`` or normalized ``(artist, title)``), so recovery
    never overwrites existing library files."""
    ad = adapter or beets_adapter
    mbid = _s(mb_trackid).strip()
    if mbid:
        try:
            by_mbid = ad.find_all_items_by_mbid(mbid) or []
            for row in by_mbid:
                if isinstance(row, dict) and row.get("id") is not None:
                    return row
        except Exception:
            pass
    norm_artist = _norm(artist)
    norm_title = _norm(title)
    if norm_artist and norm_title:
        try:
            candidates = ad.find_items_by_query(f"title:{title}") or []
            for row in candidates:
                if not isinstance(row, dict):
                    continue
                if _norm(row.get("title")) == norm_title and _norm(row.get("artist") or row.get("albumartist")) == norm_artist:
                    return row
        except Exception:
            pass
    return None


def _stamp_staged_identity_tags(
    resolved_file: str,
    *,
    artist: str,
    title: str,
    album: str,
    mb_trackid: str = "",
) -> None:
    """Stamp verified identity metadata onto the staged audio file prior to
    singleton Beets import so ``operations._refuse_blank_identity`` sees non-blank
    artist/title tags when ``autotag=False`` and ``quiet_fallback='skip'``."""
    try:
        from mediafile import MediaFile

        mf = MediaFile(resolved_file)
        mf.artist = artist
        mf.albumartist = artist
        mf.title = title
        mf.album = album
        if mb_trackid:
            mf.mb_trackid = mb_trackid
        mf.save()
    except Exception:
        pass


def _recover_import_one(
    staged_path: str,
    log: list,
    *,
    artist_hint: str = "",
    title_hint: str = "",
    album_hint: str = "",
    skip_existing: bool = True,
    adapter: Any = None,
) -> Dict[str, Any]:
    """Verify one staged recovery file and import it into Beets as a singleton
    via ``beets_adapter.run_import`` with ``autotag=False``,
    ``quiet_fallback='skip'``, ``duplicate_action='skip'``, and
    ``copy=True, move=False`` under a tracked ``TransactionStore`` record."""
    from backend.app_runtime import transactions

    verified = verify_and_match_staged_upload(
        staged_path,
        artist_hint=artist_hint,
        title_hint=title_hint,
        album_hint=album_hint,
    )
    if not verified.get("ok"):
        err = _s(verified.get("error") or "verification failed")
        ver_status = _s(verified.get("status") or "verification_failed")
        log.append(f"  [recover-import] Verification refused {Path(_s(staged_path)).name}: {err}")
        return {
            "staged_file": _s(staged_path),
            "imported": False,
            "status": ver_status,
            "final_action": _s(verified.get("final_action") or "reject"),
            "provenance_status": "recovered" if ver_status in ("identity_conflict", "review_required") else "unavailable",
            "error": err,
        }

    resolved_file = verified["staged_file"]
    artist = verified["artist"]
    title = verified["title"]
    album = verified["album"]
    mb_trackid = verified["mb_trackid"]
    match_source = verified["match_source"]

    # Always enforce existing-library conflict protection before importing.
    existing = _find_existing_library_conflict(artist, title, mb_trackid, adapter=adapter)
    if existing is not None:
        existing_id = existing.get("id")
        existing_path = _s(existing.get("path") or "")
        log.append(
            f"  [recover-import] Skipped {artist} - {title}: already in library (item #{existing_id})"
        )
        return {
            "staged_file": resolved_file,
            "imported": False,
            "status": "skipped_existing",
            "provenance_status": "recovered",
            "existing_item_id": existing_id,
            "existing_path": existing_path,
            "artist": artist,
            "title": title,
            "album": album,
            "mb_trackid": mb_trackid,
            "match_source": match_source,
        }

    _stamp_staged_identity_tags(
        resolved_file,
        artist=artist,
        title=title,
        album=album,
        mb_trackid=mb_trackid,
    )

    set_fields: Dict[str, Any] = {
        "artist": artist,
        "albumartist": artist,
        "title": title,
        "album": album,
        "source_platform": verified.get("source_platform") or "",
    }
    if mb_trackid:
        set_fields["mb_trackid"] = mb_trackid

    tx_id: Optional[str] = None
    try:
        tx = transactions.create(
            operation_type="Import",
            status="Running",
            source="recover_upload_singleton",
            summary=f"{artist} - {title}",
            metadata={
                "workflow": "recover_upload_singleton",
                "staged_file": resolved_file,
                "source_url": _sanitize_persisted_url(verified.get("source_url") or ""),
                "match_source": match_source,
            },
        )
        if isinstance(tx, dict):
            tx_id = _s(tx.get("id") or "") or None
    except Exception:
        tx_id = None

    try:
        if adapter is None:
            import_res = beets_adapter.run_import(
                paths=[resolved_file],
                autotag=False,
                duplicate_action="skip",
                copy=True,
                move=False,
                write=True,
                singletons=True,
                set_fields=set_fields,
                search_ids=None,
                quiet_fallback="skip",
            )
        else:
            import_res = adapter.run_import(
                paths=[resolved_file],
                autotag=False,
                duplicate_action="skip",
                copy=True,
                move=False,
                write=True,
                singletons=True,
                set_fields=set_fields,
                search_ids=None,
                quiet_fallback="skip",
            )
    except Exception as exc:
        err_msg = _redact_security_text(_s(exc))[:400]
        if tx_id:
            try:
                transactions.update(tx_id, status="Failed", metadata={"error": err_msg})
            except Exception:
                pass
        log.append(f"  [recover-import] Beets import failed for {Path(resolved_file).name}: {err_msg}")
        return {
            "staged_file": resolved_file,
            "imported": False,
            "status": "import_failed",
            "provenance_status": "recovered",
            "error": err_msg,
            "artist": artist,
            "title": title,
            "transaction_id": tx_id,
        }

    if isinstance(import_res, dict) and (
        import_res.get("ok") is False or import_res.get("success") is False
    ):
        err_msg = _redact_security_text(_s(import_res.get("error") or "beets import returned failure"))[:400]
        if tx_id:
            try:
                transactions.update(tx_id, status="Failed", metadata={"error": err_msg})
            except Exception:
                pass
        log.append(f"  [recover-import] Beets refused import for {Path(resolved_file).name}: {err_msg}")
        return {
            "staged_file": resolved_file,
            "imported": False,
            "status": "import_failed",
            "provenance_status": "recovered",
            "error": err_msg,
            "artist": artist,
            "title": title,
            "transaction_id": tx_id,
        }

    # Check if Beets skipped the file during import (e.g., duplicate or blank_identity).
    if isinstance(import_res, dict):
        skipped_entries = import_res.get("skipped") if isinstance(import_res.get("skipped"), list) else []
        skipped_paths = import_res.get("skipped_paths") if isinstance(import_res.get("skipped_paths"), list) else []
        if skipped_entries or skipped_paths:
            first_skip = skipped_entries[0] if (skipped_entries and isinstance(skipped_entries[0], dict)) else {}
            skip_reason = _s(first_skip.get("reason") or "skipped").strip().lower()
            if skip_reason == "duplicate":
                if tx_id:
                    try:
                        transactions.update(
                            tx_id,
                            status="Completed",
                            metadata={"engine_result": {"skipped_duplicate": True, "staged_file": resolved_file}},
                        )
                    except Exception:
                        pass
                log.append(f"  [recover-import] Beets skipped duplicate during import: {artist} - {title}")
                return {
                    "staged_file": resolved_file,
                    "imported": False,
                    "status": "skipped_existing",
                    "provenance_status": "recovered",
                    "skip_reason": "duplicate",
                    "artist": artist,
                    "title": title,
                    "album": album,
                    "mb_trackid": mb_trackid,
                    "match_source": match_source,
                    "transaction_id": tx_id,
                }
            err_msg = f"beets skipped file during import ({skip_reason})"
            if tx_id:
                try:
                    transactions.update(tx_id, status="Failed", metadata={"error": err_msg})
                except Exception:
                    pass
            log.append(f"  [recover-import] Beets skipped {Path(resolved_file).name}: {err_msg}")
            return {
                "staged_file": resolved_file,
                "imported": False,
                "status": "import_failed",
                "provenance_status": "recovered",
                "error": err_msg,
                "artist": artist,
                "title": title,
                "transaction_id": tx_id,
            }

    # Post-import library read-back: verify the imported item actually exists in Beets.
    imported_item = _find_existing_library_conflict(artist, title, mb_trackid, adapter=adapter)
    if imported_item is None:
        err_msg = "beets import completed without adding item to library"
        if tx_id:
            try:
                transactions.update(tx_id, status="Failed", metadata={"error": err_msg})
            except Exception:
                pass
        log.append(f"  [recover-import] Post-import library read-back failed for {artist} - {title}")
        return {
            "staged_file": resolved_file,
            "imported": False,
            "status": "import_failed",
            "provenance_status": "recovered",
            "error": err_msg,
            "artist": artist,
            "title": title,
            "transaction_id": tx_id,
        }

    imported_item_id = imported_item.get("id")
    if tx_id:
        try:
            transactions.update(
                tx_id,
                status="Completed",
                metadata={"engine_result": {
                    "item_ids": [imported_item_id] if imported_item_id is not None else [],
                    "staged_file": resolved_file,
                }},
            )
        except Exception:
            pass

    # Update the .recover.json sidecar with import provenance
    try:
        sidecar_data = json.loads(Path(verified["provenance_file"]).read_text(encoding="utf-8"))
    except Exception:
        sidecar_data = {}
    if isinstance(sidecar_data, dict):
        sidecar_data.update({
            "imported": True,
            "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "imported_item_id": imported_item_id,
            "provenance_status": "imported",
            "artist": artist,
            "title": title,
            "album": album,
            "mb_trackid": mb_trackid,
            "match_source": match_source,
        })
        _write_provenance(resolved_file, sidecar_data)

    log.append(
        f"  [recover-import] Imported {artist} - {title} ({match_source}"
        + (f", mb_trackid={mb_trackid}" if mb_trackid else "")
        + ")"
    )
    return {
        "staged_file": resolved_file,
        "imported": True,
        "imported_item_id": imported_item_id,
        "status": "imported",
        "provenance_status": "imported",
        "artist": artist,
        "title": title,
        "album": album,
        "mb_trackid": mb_trackid,
        "match_source": match_source,
        "source_url": _sanitize_persisted_url(verified.get("source_url") or ""),
        "source_platform": verified.get("source_platform") or "",
        "transaction_id": tx_id,
        "beets": import_res if isinstance(import_res, dict) else {},
    }


def refresh_playlists_for_recovered_tracks(
    imported_records: Iterable[Dict[str, Any]],
    log: Optional[list] = None,
) -> Dict[str, Any]:
    """Invalidate the library cache and reconcile any saved playlists whose
    desired tracks match the newly imported uploads (Phase 2 #10)."""
    from backend.matching_service import _invalidate_lib_cache
    from backend.playlist_service import (
        _playlist_ensure_state_dirs,
        _playlist_find_track_row,
        _playlist_manifest_name_from_file,
        _playlist_read_manifest,
        _playlist_store_track_state,
        _playlist_write_local_membership,
    )

    records = [r for r in (imported_records or []) if isinstance(r, dict) and r.get("imported")]
    _invalidate_lib_cache()
    if not records:
        return {"playlists_checked": 0, "playlists_updated": [], "tracks_covered": 0}

    _playlist_ensure_state_dirs()
    playlists_checked = 0
    playlists_updated: List[str] = []
    tracks_covered = 0

    if not PLAYLIST_MANIFESTS_DIR.exists():
        return {"playlists_checked": 0, "playlists_updated": [], "tracks_covered": 0}

    for manifest_path in sorted(PLAYLIST_MANIFESTS_DIR.glob("*.playlist.json"), key=lambda p: p.name.lower()):
        diagnostics: List[str] = []
        clean_name, manifest = _playlist_manifest_name_from_file(manifest_path, diagnostics)
        if not clean_name or not isinstance(manifest, dict):
            continue
        playlists_checked += 1
        desired = manifest.get("desired_tracks") or []
        if not isinstance(desired, list) or not desired:
            continue

        matched_in_playlist = 0
        for rec in records:
            target = {
                "artist": _s(rec.get("artist") or ""),
                "title": _s(rec.get("title") or ""),
                "source_url": _s(rec.get("source_url") or ""),
            }
            idx, row = _playlist_find_track_row(desired, target)
            if idx < 0 and rec.get("source_url"):
                want_url = _s(rec.get("source_url")).strip()
                for i, d_row in enumerate(desired):
                    if isinstance(d_row, dict) and _s(d_row.get("source_url")).strip() == want_url:
                        idx, row = i, dict(d_row)
                        break
            if idx >= 0 and isinstance(row, dict):
                matched_in_playlist += 1
                tracks_covered += 1
                try:
                    _playlist_store_track_state(
                        clean_name,
                        row,
                        "imported",
                        message="recovered upload imported into Beets library",
                    )
                except Exception:
                    pass

        if matched_in_playlist > 0:
            try:
                # Reload manifest after _playlist_store_track_state so newly stored
                # 'imported' states are preserved when writing local membership.
                fresh_manifest = _playlist_read_manifest(clean_name)
                if not isinstance(fresh_manifest, dict):
                    fresh_manifest = manifest
                _playlist_write_local_membership(clean_name, fresh_manifest)
                playlists_updated.append(clean_name)
                if log is not None:
                    log.append(
                        f"  [recover-import] Updated playlist coverage for '{clean_name}' "
                        f"(+{matched_in_playlist} recovered track(s))"
                    )
            except Exception:
                pass

    return {
        "playlists_checked": playlists_checked,
        "playlists_updated": playlists_updated,
        "tracks_covered": tracks_covered,
    }


def recover_import_job(
    staged_files: List[str],
    log: list,
    cancel_event: Any = None,
    *,
    run_id: str = "",
    metadata_by_file: Optional[Dict[str, Dict[str, Any]]] = None,
    skip_existing: bool = True,
    adapter: Any = None,
) -> Dict[str, Any]:
    """Verify and import staged recovery files into Beets, update persisted run
    records, and refresh playlist coverage."""
    meta_map = metadata_by_file or {}
    imported: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    for idx, staged_path in enumerate(staged_files, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("cancelled")
        hints = meta_map.get(staged_path) or {}
        log.append(f"[recover-import] [{idx}/{len(staged_files)}] {Path(staged_path).name}")
        res = _recover_import_one(
            staged_path,
            log,
            artist_hint=_s(hints.get("artist") or ""),
            title_hint=_s(hints.get("title") or ""),
            album_hint=_s(hints.get("album") or ""),
            skip_existing=True,
            adapter=adapter,
        )
        if res.get("imported"):
            imported.append(res)
        elif res.get("status") == "skipped_existing":
            skipped.append(res)
        else:
            failed.append(res)

    playlist_coverage = refresh_playlists_for_recovered_tracks(imported, log=log)
    all_failed = bool(failed) and not imported and not skipped
    summary: Dict[str, Any] = {
        "ok": not all_failed,
        "run_id": _s(run_id),
        "imported_count": len(imported),
        "skipped_existing_count": len(skipped),
        "failed_count": len(failed),
        "partial": bool(failed and (imported or skipped)),
        "imported": imported,
        "skipped_existing": skipped,
        "failed": failed,
        "playlist_coverage": playlist_coverage,
    }
    if all_failed:
        summary["error"] = f"all {len(failed)} staged file(s) failed verification or import"

    if run_id:
        existing_run = recover_get_run(run_id) or {"run_id": run_id}
        existing_run["imported"] = imported
        existing_run["imported_count"] = len(imported)
        existing_run["skipped_existing"] = skipped
        existing_run["skipped_existing_count"] = len(skipped)
        existing_run["import_failed"] = failed
        existing_run["import_failed_count"] = len(failed)
        existing_run["playlist_coverage"] = playlist_coverage
        if failed:
            existing_run["status"] = "failed" if (all_failed and not existing_run.get("recovered")) else "partial"
        _upsert_recover_run(existing_run)

    return summary


def recover_fetch_job(urls: List[str], log: list, cancel_event: Any = None,
                      *, run_id: str = "", ydl_module: Any = None,
                      auto_import: bool = False, retry_of: str = "",
                      retry_count: int = 0, adapter: Any = None) -> Dict[str, Any]:
    """Fetch + stage each URL under DOWNLOADS_ROOT. Optionally imports verified
    staged files when ``auto_import=True``. Persists run state in
    ``recover_runs.json`` for retry support."""
    run_id = _s(run_id).strip() or uuid.uuid4().hex
    recovered: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    cookie_state = recover_cookie_state()
    if cookie_state.get("state") != "ok":
        log.append(f"  [recover] Cookie state: {cookie_state.get('state')} — {cookie_state.get('message')}")

    _upsert_recover_run({
        "run_id": run_id,
        "status": "running",
        "cookie_state": cookie_state.get("state"),
        "urls": list(urls),
        "recovered": [],
        "failed": [],
        "retry_of": _s(retry_of),
        "retry_count": int(retry_count or 0),
    })

    for idx, url in enumerate(urls, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("cancelled")
        log.append(f"[recover] [{idx}/{len(urls)}] {_sanitize_persisted_url(url) or url}")
        # Per-item sub-run id keeps multiple items in one job isolated too.
        result = _recover_fetch_one(url, log, run_id=f"{run_id}-{idx}", ydl_module=ydl_module)
        if result.get("recovered"):
            recovered.append(result)
        else:
            failed.append(result)
            log.append(
                f"  [recover] Failed ({result.get('category')}): {result.get('action')}"
            )
    # BA-3: a job where every item failed must report ok=False so the job engine
    # records it as failed, not success (F12). A partial success stays ok=True
    # but carries partial=True and the per-item counts.
    all_failed = bool(failed) and not recovered
    summary: Dict[str, Any] = {
        "run_id": run_id,
        "cookie_state": cookie_state.get("state"),
        "recovered_count": len(recovered),
        "failed_count": len(failed),
        "partial": bool(recovered and failed),
        "recovered": recovered,
        "failed": failed,
        "retry_of": _s(retry_of),
        "retry_count": int(retry_count or 0),
    }
    if all_failed:
        summary["ok"] = False
        summary["error"] = (
            f"all {len(failed)} item(s) failed to recover: "
            + "; ".join(f"{f.get('category')}" for f in failed[:8])
        )
    else:
        summary["ok"] = True

    import_failed_items: List[Dict[str, Any]] = []
    skipped_existing_items: List[Dict[str, Any]] = []
    if auto_import and recovered:
        staged_paths = [
            _s(r.get("staged_file")) for r in recovered if _s(r.get("staged_file")).strip()
        ]
        if staged_paths:
            import_summary = recover_import_job(
                staged_paths,
                log,
                cancel_event,
                run_id=run_id,
                adapter=adapter,
            )
            summary["import_summary"] = import_summary
            import_failed_items = list(import_summary.get("failed") or [])
            skipped_existing_items = list(import_summary.get("skipped_existing") or [])
            summary["imported_count"] = int(import_summary.get("imported_count") or 0)
            summary["skipped_existing_count"] = len(skipped_existing_items)
            summary["import_failed_count"] = len(import_failed_items)
            summary["import_failed"] = import_failed_items
            if import_failed_items:
                summary["partial"] = True
                if not import_summary.get("imported") and not skipped_existing_items and not failed:
                    summary["ok"] = False
                    summary["error"] = _s(
                        import_summary.get("error")
                        or f"all {len(import_failed_items)} recovered item(s) failed import"
                    )

    run_status = (
        "failed"
        if (all_failed or not summary.get("ok"))
        else ("partial" if summary["partial"] else "done")
    )
    _upsert_recover_run({
        "run_id": run_id,
        "status": run_status,
        "cookie_state": cookie_state.get("state"),
        "urls": list(urls),
        "recovered_count": len(recovered),
        "failed_count": len(failed),
        "imported_count": len((summary.get("import_summary") or {}).get("imported") or []),
        "skipped_existing_count": len(skipped_existing_items),
        "import_failed_count": len(import_failed_items),
        "recovered": recovered,
        "failed": failed,
        "imported": (summary.get("import_summary") or {}).get("imported") or [],
        "skipped_existing": skipped_existing_items,
        "import_failed": import_failed_items,
        "playlist_coverage": (summary.get("import_summary") or {}).get("playlist_coverage") or {},
        "retry_of": _s(retry_of),
        "retry_count": int(retry_count or 0),
    })
    return summary


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

    auto_import = bool(data.get("auto_import", False))
    retry_of = _s(data.get("retry_of") or "").strip()
    try:
        retry_count = max(0, int(data.get("retry_count") or 0))
    except (TypeError, ValueError):
        retry_count = 0

    # Pre-generate the run id BEFORE starting the job and capture it in the
    # closure, so the worker never reads an unset id (F5/F13) and the staging
    # dir isolation (F8) is deterministic regardless of thread timing.
    run_id = uuid.uuid4().hex

    def _do(log, cancel_event=None):
        return recover_fetch_job(
            urls,
            log,
            cancel_event,
            run_id=run_id,
            auto_import=auto_import,
            retry_of=retry_of,
            retry_count=retry_count,
        )

    label = f"Recover uploads: {len(urls)} item(s)" + (" (retry)" if retry_of else "")
    workflow = "recover-fetch-" + job_contract.slug("|".join(urls))
    job_meta: Dict[str, Any] = {
        "type": "recover-fetch",
        "mutating": auto_import,
        "url_count": len(urls),
        "run_id": run_id,
        "retry_of": retry_of,
        "retry_count": retry_count,
    }
    if auto_import:
        job_meta["transaction_operation"] = "Import"
    job = jobs.start_python(
        job_contract.guarded(_do, workflow=workflow, fail_fast_in_process=True),
        label=label,
        metadata=job_meta,
    )
    return {"ok": True, "job_id": job.job_id, "run_id": run_id, "retry_of": retry_of, "retry_count": retry_count}, 200


def start_recover_retry(payload_in: Dict[str, Any]) -> Any:
    """Service behind POST /api/recover/retry. Looks up a persisted recovery run
    by ``run_id`` and starts a new recovery job for its failed/unrecovered URLs,
    or retries failed staged imports directly when all URLs were already staged."""
    data = payload_in or {}
    parent_run_id = _s(data.get("run_id") or "").strip()
    if not parent_run_id:
        return {"ok": False, "error": "run_id is required"}, 200

    parent = recover_get_run(parent_run_id)
    if not isinstance(parent, dict):
        return {"ok": False, "error": f"recovery run '{parent_run_id}' not found"}, 200

    include_drm = bool(data.get("include_drm", False))
    recovered_urls = {
        _s(r.get("url")).strip()
        for r in (parent.get("recovered") or [])
        if isinstance(r, dict) and r.get("recovered")
    }
    drm_skipped_urls = {
        _s(f.get("url")).strip()
        for f in (parent.get("failed") or [])
        if isinstance(f, dict) and not include_drm and _s(f.get("category")) == "genuinely-DRM"
    }
    failed_urls = [
        _s(f.get("url")).strip()
        for f in (parent.get("failed") or [])
        if isinstance(f, dict)
        and _s(f.get("url")).strip()
        and (include_drm or _s(f.get("category")) != "genuinely-DRM")
    ]
    for orig_url in (parent.get("urls") or []):
        u = _s(orig_url).strip()
        if u and u not in recovered_urls and u not in failed_urls and u not in drm_skipped_urls:
            failed_urls.append(u)

    requested_subset = data.get("urls")
    if isinstance(requested_subset, list) and requested_subset:
        wanted = {_s(u).strip() for u in requested_subset if _s(u).strip()}
        retry_urls = [u for u in failed_urls if u in wanted]
    else:
        retry_urls = failed_urls

    if not retry_urls:
        # Check if there are failed staged imports that can be retried from staging
        # without re-downloading from the remote platform.
        import_failed_files = [
            _s(item.get("staged_file")).strip()
            for item in (parent.get("import_failed") or [])
            if isinstance(item, dict) and _s(item.get("staged_file")).strip()
        ]
        if import_failed_files:
            imp_body, imp_status = start_recover_import({
                "run_id": parent_run_id,
                "staged_files": import_failed_files,
            })
            if isinstance(imp_body, dict) and imp_body.get("ok"):
                imp_body["retried_staged_files"] = list(import_failed_files)
                imp_body["retry_of"] = parent_run_id
            return imp_body, imp_status
        return {"ok": False, "error": "no failed or unrecovered URLs to retry for this run"}, 200

    next_retry_count = int(parent.get("retry_count") or 0) + 1
    body, status = start_recover_fetch({
        "urls": retry_urls,
        "auto_import": bool(data.get("auto_import", False)),
        "retry_of": parent_run_id,
        "retry_count": next_retry_count,
    })
    if isinstance(body, dict) and body.get("ok"):
        body["retried_urls"] = list(retry_urls)
    return body, status


def start_recover_import(payload_in: Dict[str, Any]) -> Any:
    """Service behind POST /api/recover/import (request-free). Verifies staged
    files up front and starts a tracked job to fingerprint, match, import into
    Beets (without overwriting existing library items), and refresh playlists."""
    import backend.job_contract as job_contract
    from backend.app_runtime import jobs

    data = payload_in or {}
    run_id = _s(data.get("run_id") or "").strip()
    staged_files: List[str] = []
    metadata_by_file: Dict[str, Dict[str, Any]] = {}

    raw_files = data.get("staged_files")
    if isinstance(raw_files, list):
        for f in raw_files:
            if isinstance(f, str) and f.strip():
                staged_files.append(f.strip())
            elif isinstance(f, dict) and _s(f.get("staged_file")).strip():
                sf = _s(f.get("staged_file")).strip()
                staged_files.append(sf)
                metadata_by_file[sf] = {
                    "artist": _s(f.get("artist") or ""),
                    "title": _s(f.get("title") or ""),
                    "album": _s(f.get("album") or ""),
                }
    single_file = _s(data.get("staged_file") or "").strip()
    if single_file:
        staged_files.append(single_file)
        metadata_by_file.setdefault(single_file, {
            "artist": _s(data.get("artist") or ""),
            "title": _s(data.get("title") or ""),
            "album": _s(data.get("album") or ""),
        })

    if not staged_files and run_id:
        run_rec = recover_get_run(run_id)
        if not isinstance(run_rec, dict):
            return {"ok": False, "error": f"recovery run '{run_id}' not found"}, 200
        for rec in (run_rec.get("recovered") or []):
            if isinstance(rec, dict) and _s(rec.get("staged_file")).strip():
                staged_files.append(_s(rec.get("staged_file")).strip())

    seen: set = set()
    staged_files = [f for f in staged_files if not (f in seen or seen.add(f))]
    if not staged_files:
        return {"ok": False, "error": "staged_files or a run_id with recovered files is required"}, 200

    # Validate every staged file and provenance sidecar before creating a job.
    validated_paths: List[str] = []
    for sf in staged_files:
        resolved, _sidecar, err = _validate_staged_recover_file(sf)
        if resolved is None:
            return {"ok": False, "error": f"{Path(sf).name or sf}: {err}"}, 200
        resolved_str = str(resolved)
        validated_paths.append(resolved_str)
        if sf in metadata_by_file and resolved_str not in metadata_by_file:
            metadata_by_file[resolved_str] = metadata_by_file[sf]

    # Always enforce existing-library overwrite protection (non-configurable).
    skip_existing = True

    def _do(log, cancel_event=None):
        return recover_import_job(
            validated_paths,
            log,
            cancel_event,
            run_id=run_id,
            metadata_by_file=metadata_by_file,
            skip_existing=skip_existing,
        )

    label = f"Import recovered uploads: {len(validated_paths)} item(s)"
    workflow = "recover-import-" + job_contract.slug(run_id or "|".join(validated_paths))
    job = jobs.start_python(
        job_contract.guarded(_do, workflow=workflow, fail_fast_in_process=True),
        label=label,
        metadata={
            "type": "recover-import",
            "transaction_operation": "Import",
            "mutating": True,
            "file_count": len(validated_paths),
            "run_id": run_id,
        },
    )
    return {"ok": True, "job_id": job.job_id, "run_id": run_id, "file_count": len(validated_paths)}, 200
