"""Music format preferences and audio validation helpers."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_LAYOUT_KEYS = ("mono", "stereo", "2.1", "5.1", "7.1", "atmos")
_FORMAT_KEYS = ("flac", "mp3", "aac", "alac", "opus", "wav", "eac3", "truehd")
_AUDIO_EXT_TO_FORMAT = {
    ".flac": "flac",
    ".mp3": "mp3",
    ".m4a": "aac",
    ".aac": "aac",
    ".alac": "alac",
    ".opus": "opus",
    ".ogg": "opus",
    ".wav": "wav",
    ".wave": "wav",
    ".eac3": "eac3",
    ".ec3": "eac3",
    ".thd": "truehd",
    ".truehd": "truehd",
}
_CODEC_TO_FORMAT = {
    "flac": "flac",
    "mp3": "mp3",
    "aac": "aac",
    "alac": "alac",
    "opus": "opus",
    "vorbis": "opus",
    "wav": "wav",
    "pcm_s16le": "wav",
    "pcm_s24le": "wav",
    "pcm_s32le": "wav",
    "eac3": "eac3",
    "e-ac-3": "eac3",
    "truehd": "truehd",
    "mlp": "truehd",
}

DEFAULT_MUSIC_FORMAT_PREFERENCES: Dict[str, Any] = {
    "allowed_layouts": {
        "mono": False,
        "stereo": True,
        "2.1": True,
        "5.1": False,
        "7.1": False,
        "atmos": True,
    },
    "allow_atmos": True,
    "custom_max_channels": None,
    "preferred_formats": ["flac", "mp3", "aac", "eac3", "truehd"],
    "rejected_download_handling": "quarantine",
    "replacement_fallback": {
        "keep_current": True,
        "mark_needs_replacement": True,
        "queue_retry": True,
        "try_lower_ranked": True,
        "try_alternate_source": True,
        "allow_temporary_exception": False,
    },
}


def _settings_path(path: Optional[str] = None) -> Path:
    return Path(path or os.environ.get("MUSIC_FORMAT_PREFS_FILE", "/config/music_format_preferences.json"))


def _replacement_status_path(path: Optional[str] = None) -> Path:
    return Path(path or os.environ.get("MUSIC_FORMAT_REPLACEMENT_STATUS_FILE", "/config/music_format_replacements.json"))


def _quarantine_root(path: Optional[str] = None) -> Path:
    return Path(path or os.environ.get("MUSIC_FORMAT_QUARANTINE_DIR", "/config/music_format_quarantine"))


def _copy_defaults() -> Dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_MUSIC_FORMAT_PREFERENCES))


def normalize_music_format_preferences(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    prefs = _copy_defaults()
    if not isinstance(raw, dict):
        return prefs

    allowed = raw.get("allowed_layouts") if isinstance(raw.get("allowed_layouts"), dict) else {}
    for key in _LAYOUT_KEYS:
        if key in allowed:
            prefs["allowed_layouts"][key] = bool(allowed[key])

    if "allow_atmos" in raw:
        prefs["allow_atmos"] = bool(raw.get("allow_atmos"))
        prefs["allowed_layouts"]["atmos"] = bool(raw.get("allow_atmos")) and bool(
            prefs["allowed_layouts"].get("atmos", True)
        )

    custom = raw.get("custom_max_channels")
    try:
        custom_int = int(custom) if custom not in (None, "") else None
    except (TypeError, ValueError):
        custom_int = None
    prefs["custom_max_channels"] = custom_int if custom_int and 1 <= custom_int <= 32 else None

    formats = raw.get("preferred_formats")
    if isinstance(formats, list):
        seen: List[str] = []
        for item in formats:
            key = str(item or "").strip().lower().replace("-", "")
            if key in {"eac3", "eac", "eac 3"}:
                key = "eac3"
            if key in _FORMAT_KEYS and key not in seen:
                seen.append(key)
        if seen:
            prefs["preferred_formats"] = seen

    handling = str(raw.get("rejected_download_handling") or "").strip().lower()
    if handling in {"quarantine", "delete"}:
        prefs["rejected_download_handling"] = handling

    fallback = raw.get("replacement_fallback") if isinstance(raw.get("replacement_fallback"), dict) else {}
    for key, value in fallback.items():
        if key in prefs["replacement_fallback"]:
            prefs["replacement_fallback"][key] = bool(value)
    if not prefs["replacement_fallback"].get("keep_current"):
        prefs["replacement_fallback"]["keep_current"] = True
    if not prefs["replacement_fallback"].get("mark_needs_replacement"):
        prefs["replacement_fallback"]["mark_needs_replacement"] = True
    return prefs


def load_music_format_preferences(path: Optional[str] = None) -> Dict[str, Any]:
    prefs_path = _settings_path(path)
    try:
        return normalize_music_format_preferences(json.loads(prefs_path.read_text(encoding="utf-8")))
    except Exception:
        return _copy_defaults()


def save_music_format_preferences(payload: Dict[str, Any], path: Optional[str] = None) -> Dict[str, Any]:
    prefs = normalize_music_format_preferences(payload)
    prefs_path = _settings_path(path)
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = prefs_path.with_suffix(prefs_path.suffix + ".tmp")
    tmp.write_text(json.dumps(prefs, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(prefs_path)
    return prefs


def _first_audio_stream(probe: Dict[str, Any]) -> Dict[str, Any]:
    for stream in probe.get("streams") or []:
        if str(stream.get("codec_type") or "").lower() == "audio":
            return stream
    return {}


def _text_blob(*parts: Any) -> str:
    chunks: List[str] = []
    for part in parts:
        if isinstance(part, dict):
            chunks.extend(str(v) for v in part.values())
        elif isinstance(part, list):
            chunks.extend(_text_blob(v) for v in part)
        elif part is not None:
            chunks.append(str(part))
    return " ".join(chunks).lower()


def _codec_format(stream: Dict[str, Any], path: Path) -> str:
    codec = str(stream.get("codec_name") or "").strip().lower().replace("-", "")
    if codec in _CODEC_TO_FORMAT:
        return _CODEC_TO_FORMAT[codec]
    ext_format = _AUDIO_EXT_TO_FORMAT.get(path.suffix.lower(), "")
    if ext_format:
        return ext_format
    return codec or "unknown"


def _has_reliable_atmos_indicator(stream: Dict[str, Any], probe: Dict[str, Any]) -> bool:
    blob = _text_blob(
        stream.get("codec_name"),
        stream.get("codec_long_name"),
        stream.get("profile"),
        stream.get("channel_layout"),
        stream.get("side_data_list"),
        stream.get("tags"),
        (probe.get("format") or {}).get("tags"),
    )
    patterns = (
        "e-ac-3 joc",
        "eac3 joc",
        "joint object coding",
        "dolby digital plus atmos",
        "dd+ atmos",
        "dolby truehd atmos",
        "truehd atmos",
        "dolby atmos",
    )
    return any(pattern in blob for pattern in patterns)


def _layout_key(channels: Optional[int], channel_layout: str, is_atmos: bool) -> str:
    layout = (channel_layout or "").strip().lower()
    if is_atmos:
        return "atmos"
    if layout in {"mono", "1.0"} or channels == 1:
        return "mono"
    if layout in {"stereo", "2.0"} or (channels == 2 and not layout):
        return "stereo"
    if "2.1" in layout or (channels == 3 and ("lfe" in layout or not layout)):
        return "2.1"
    if "5.1" in layout or channels == 6:
        return "5.1"
    if "7.1" in layout or channels == 8:
        return "7.1"
    return "custom" if channels else "unknown"


def _audio_probe_timeout(name: str, default: int) -> int:
    try:
        return max(5, int(os.environ.get(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


def inspect_audio_file(path_value: str, *, ffprobe_bin: Optional[str] = None) -> Dict[str, Any]:
    path = Path(path_value)
    ffprobe = ffprobe_bin or shutil.which("ffprobe") or "/usr/bin/ffprobe"
    result: Dict[str, Any] = {
        "path": str(path),
        "ok": False,
        "error": "",
        "codec": "unknown",
        "format": _AUDIO_EXT_TO_FORMAT.get(path.suffix.lower(), "unknown"),
        "channels": None,
        "channel_layout": "",
        "layout": "unknown",
        "is_atmos": False,
        "atmos_filename_hint": bool(re.search(r"\batmos\b", path.name, re.I)),
        "sample_rate": None,
        "bitrate": None,
    }
    full_timeout = _audio_probe_timeout("AUDIO_FFPROBE_TIMEOUT", 20)
    fast_timeout = _audio_probe_timeout("AUDIO_FFPROBE_FAST_TIMEOUT", 20)
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-show_streams",
                "-show_format",
                "-of", "json",
                str(path),
            ],
            timeout=full_timeout,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        try:
            proc = subprocess.run(
                [
                    ffprobe,
                    "-v", "error",
                    "-read_intervals", "0%+30",
                    "-select_streams", "a:0",
                    "-show_entries",
                    "stream=codec_name,codec_long_name,profile,channels,channel_layout,sample_rate,bit_rate,side_data_list,tags:format=duration,bit_rate,tags",
                    "-of", "json",
                    str(path),
                ],
                timeout=fast_timeout,
                capture_output=True,
                text=True,
            )
        except Exception as fallback_exc:
            result["error"] = f"ffprobe failed: {exc}; fast probe failed: {fallback_exc}"
            return result
    except Exception as exc:
        result["error"] = f"ffprobe failed: {exc}"
        return result
    if proc.returncode != 0:
        result["error"] = (proc.stderr or proc.stdout or "ffprobe failed").strip()[:400]
        return result
    try:
        probe = json.loads(proc.stdout or "{}")
    except Exception as exc:
        result["error"] = f"ffprobe JSON failed: {exc}"
        return result
    stream = _first_audio_stream(probe)
    if not stream:
        result["error"] = "No audio stream found"
        return result
    channels = stream.get("channels")
    try:
        channels_int = int(channels) if channels not in (None, "") else None
    except (TypeError, ValueError):
        channels_int = None
    is_atmos = _has_reliable_atmos_indicator(stream, probe)
    layout = str(stream.get("channel_layout") or "").strip()
    sample_rate = stream.get("sample_rate")
    bitrate = stream.get("bit_rate") or (probe.get("format") or {}).get("bit_rate")
    result.update({
        "ok": True,
        "codec": str(stream.get("codec_name") or "unknown").lower(),
        "format": _codec_format(stream, path),
        "channels": channels_int,
        "channel_layout": layout,
        "layout": _layout_key(channels_int, layout, is_atmos),
        "is_atmos": is_atmos,
        "sample_rate": int(sample_rate) if str(sample_rate or "").isdigit() else None,
        "bitrate": int(bitrate) if str(bitrate or "").isdigit() else None,
    })
    return result


def format_rank(format_key: str, prefs: Optional[Dict[str, Any]] = None) -> int:
    preferences = normalize_music_format_preferences(prefs)
    ordered = preferences.get("preferred_formats") or []
    try:
        return ordered.index(format_key)
    except ValueError:
        return len(ordered) + 100


def _friendly_layout(layout: str, channels: Optional[int]) -> str:
    if layout == "stereo":
        return "stereo 2-channel"
    if layout == "atmos":
        return "Dolby Atmos"
    if layout in {"mono", "2.1", "5.1", "7.1"}:
        return f"{layout} audio"
    if channels:
        return f"{channels}-channel audio"
    return "unknown channel layout"


def _friendly_format(format_key: str) -> str:
    return {
        "flac": "FLAC",
        "mp3": "MP3",
        "aac": "AAC",
        "alac": "ALAC",
        "opus": "Opus",
        "wav": "WAV",
        "eac3": "E-AC-3",
        "truehd": "TrueHD",
    }.get(format_key, format_key.upper() if format_key else "unknown format")


def validate_audio_properties(properties: Dict[str, Any], prefs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    preferences = normalize_music_format_preferences(prefs)
    reasons: List[str] = []
    if not properties.get("ok"):
        reasons.append(properties.get("error") or "audio could not be inspected")
    layout = str(properties.get("layout") or "unknown")
    channels = properties.get("channels")
    format_key = str(properties.get("format") or "unknown").lower()
    is_atmos = bool(properties.get("is_atmos"))

    if properties.get("ok"):
        if is_atmos and not preferences.get("allow_atmos"):
            reasons.append("Atmos audio is disabled in settings")
        if layout == "unknown":
            reasons.append("channel count/layout is unknown")
        elif layout == "custom":
            max_channels = preferences.get("custom_max_channels")
            if not channels or not max_channels or int(channels) > int(max_channels):
                reasons.append("this channel layout is not enabled in settings")
        elif not preferences.get("allowed_layouts", {}).get(layout, False):
            reasons.append(f"{_friendly_layout(layout, channels)} is disabled in settings")
        if format_key not in (preferences.get("preferred_formats") or []):
            reasons.append(f"{_friendly_format(format_key)} is not enabled in preferred formats")

    accepted = not reasons
    detail = f"{_friendly_layout(layout, channels)} {_friendly_format(format_key)}".strip()
    if accepted:
        if layout == "atmos":
            message = f"Accepted: Dolby Atmos {_friendly_format(format_key)}"
        elif layout == "2.1":
            message = "Accepted: 2.1 audio"
        else:
            message = f"Accepted: {detail}"
    else:
        message = "Rejected download: " + (reasons[0] if reasons else "audio does not match preferences")
    return {
        "ok": accepted,
        "accepted": accepted,
        "reasons": reasons,
        "message": message,
        "properties": properties,
        "format_rank": format_rank(format_key, preferences),
    }


def validate_audio_file(path_value: str, prefs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return validate_audio_properties(inspect_audio_file(path_value), prefs)


def validate_audio_tree(root: str, prefs: Optional[Dict[str, Any]], audio_exts: Iterable[str]) -> Dict[str, Any]:
    root_path = Path(root)
    files = [p for p in root_path.rglob("*") if p.is_file() and p.suffix.lower() in set(audio_exts)] if root_path.exists() else []
    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for path in files:
        result = validate_audio_file(str(path), prefs)
        row = {"path": str(path), **result}
        if result.get("ok"):
            accepted.append(row)
        else:
            rejected.append(row)
    return {"ok": not rejected, "accepted": accepted, "rejected": rejected, "total": len(files)}


def handle_rejected_download(path_value: str, prefs: Optional[Dict[str, Any]] = None, *, log: Optional[List[str]] = None) -> Dict[str, Any]:
    preferences = normalize_music_format_preferences(prefs)
    path = Path(path_value)
    handling = preferences.get("rejected_download_handling") or "quarantine"
    result = {"path": str(path), "handling": handling, "removed": False, "quarantined_to": ""}
    try:
        if handling == "delete":
            path.unlink(missing_ok=True)
            result["removed"] = True
            if log is not None:
                log.append(f"Rejected download removed: {path.name}")
        else:
            root = _quarantine_root()
            target_dir = root / time.strftime("%Y%m%d")
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / path.name
            if target.exists():
                target = target_dir / f"{path.stem}-{int(time.time())}{path.suffix}"
            shutil.move(str(path), str(target))
            result["quarantined_to"] = str(target)
            result["removed"] = True
            if log is not None:
                log.append(f"Rejected download quarantined: {target}")
    except Exception as exc:
        result["error"] = str(exc)
        if log is not None:
            log.append(f"Rejected download could not be moved: {exc}")
    return result


def sort_candidates_by_preferences(candidates: Iterable[Dict[str, Any]], prefs: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    preferences = normalize_music_format_preferences(prefs)
    def key(candidate: Dict[str, Any]) -> Tuple[int, int]:
        fmt = str(candidate.get("format") or candidate.get("codec") or "").lower()
        clear = 0 if (candidate.get("channels") or candidate.get("channel_layout") or candidate.get("is_atmos")) else 1
        return (format_rank(fmt, preferences), clear)
    return sorted(list(candidates), key=key)


def load_replacement_statuses(path: Optional[str] = None) -> Dict[str, Any]:
    status_path = _replacement_status_path(path)
    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("tracks"), list):
            return data
    except Exception:
        pass
    return {"tracks": []}


def save_replacement_statuses(data: Dict[str, Any], path: Optional[str] = None) -> Dict[str, Any]:
    status_path = _replacement_status_path(path)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"tracks": list(data.get("tracks") or []), "updated_at": time.time()}
    tmp = status_path.with_suffix(status_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(status_path)
    return payload



def _replacement_identity_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("&", " and ")
    text = re.sub(r"[\u2010-\u2015]+", "-", text)
    text = re.sub(r"['`´‘’]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def replacement_identity_key(row: Dict[str, Any]) -> str:
    for key in ("mb_trackid", "identity_mb_trackid", "resolved_mb_trackid", "acoustid_mb_trackid"):
        value = str(row.get(key) or "").strip().lower()
        if value:
            return f"mb:{value}"
    artist = _replacement_identity_text(row.get("artist") or row.get("albumartist") or "")
    title = _replacement_identity_text(row.get("title") or "")
    if artist or title:
        return f"text:{artist}|{title}"
    fallback = str(row.get("path") or row.get("item_id") or "").strip()
    return f"row:{fallback}" if fallback else ""
def mark_needs_replacement(rows: Iterable[Dict[str, Any]], path: Optional[str] = None) -> Dict[str, Any]:
    existing = load_replacement_statuses(path)
    by_key: Dict[str, Dict[str, Any]] = {}
    for idx, row in enumerate(existing.get("tracks") or []):
        key = replacement_identity_key(row) or str(row.get("path") or row.get("item_id") or idx)
        by_key[key] = row
    now = time.time()
    for row in rows:
        key = replacement_identity_key(row) or str(row.get("path") or row.get("item_id") or len(by_key))
        merged = dict(by_key.get(key) or {})
        merged.update(row)
        merged["replacement_identity_key"] = key
        merged.setdefault("first_seen_at", now)
        merged["updated_at"] = now
        merged.setdefault("status", "Needs replacement")
        merged.setdefault("queued_retry", True)
        by_key[key] = merged
    return save_replacement_statuses({"tracks": list(by_key.values())}, path)


def can_remove_original_after_replacement(replacement_validation: Dict[str, Any], final_path: str) -> bool:
    if not replacement_validation.get("ok"):
        return False
    if not final_path:
        return False
    try:
        return Path(final_path).exists()
    except Exception:
        return False