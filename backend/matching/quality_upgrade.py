"""Is a proven duplicate (``duplicate_is_already_in_library``) a quality upgrade?

Only asked after the B1 identity proof holds; it never proves identity. The
ranking is the user's Music Format Preferences (``preferred_formats`` order,
``audio_preferences.format_rank``) and nothing else: those rules order
formats only, so within one format (bit depth, sample rate, bitrate) there is
no upgrade claim. Conservative: the incoming copy's worst file must rank
strictly above the library's best file of the same Release Group. Any file
on either side whose format is unknown means "no upgrade". Pure: no I/O.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

from backend.audio_preferences import _friendly_format, format_rank

# Beets' ``format`` field (mediafile type names) -> preference format keys,
# mapped the way audio_preferences maps ffprobe codecs (vorbis -> opus).
_BEETS_FORMAT_KEYS = {"mp3": "mp3", "flac": "flac", "aac": "aac", "alac": "alac", "opus": "opus",
                      "ogg": "opus", "wave": "wav", "wav": "wav"}
_LOSSLESS = {"flac", "alac", "wav", "truehd"}


def _num(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _describe(rows: List[Dict[str, Any]]) -> str:
    """"MP3 320 kbps", "MP3 245-320 kbps", "FLAC 16-bit/44.1 kHz"; one part per format."""
    parts = []
    for fmt in sorted({r["format"] for r in rows}):
        group = [r for r in rows if r["format"] == fmt]
        label = _friendly_format(fmt)
        depths = {(r["bit_depth"], r["sample_rate"]) for r in group}
        rates = sorted({round(r["bitrate"] / 1000) for r in group if r["bitrate"]})
        if fmt in _LOSSLESS and all(d and s for d, s in depths):
            label += " " + ", ".join(f"{d}-bit/{s / 1000:g} kHz" for d, s in sorted(depths))
        elif rates and all(r["bitrate"] for r in group):
            label += f" {rates[0]} kbps" if len(rates) == 1 else f" {rates[0]}-{rates[-1]} kbps"
        parts.append(label)
    return ", ".join(parts)


def _library_rows(items: Iterable[Any]) -> Optional[List[Dict[str, Any]]]:
    rows = []
    for it in items:
        if not isinstance(it, Mapping):
            return None
        fmt = _BEETS_FORMAT_KEYS.get(str(it.get("format") or "").strip().lower())
        if not fmt:
            return None
        rows.append({"format": fmt, "bitrate": _num(it.get("bitrate")),
                     "sample_rate": _num(it.get("samplerate")), "bit_depth": _num(it.get("bitdepth"))})
    return rows or None


def _source_rows(props: Iterable[Any]) -> Optional[List[Dict[str, Any]]]:
    rows = []
    for p in props:
        if not isinstance(p, Mapping) or not p.get("ok"):
            return None
        fmt = str(p.get("format") or "").strip().lower()
        if not fmt or fmt == "unknown":
            return None
        rows.append({"format": fmt, "bitrate": _num(p.get("bitrate")),
                     "sample_rate": _num(p.get("sample_rate")), "bit_depth": _num(p.get("bit_depth"))})
    return rows or None


def quality_upgrade(source_properties: Iterable[Any], library_items: Iterable[Any],
                    prefs: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """``{"library_format", "source_format"}`` when the incoming files are a
    format upgrade over the library album(s), else None.

    ``source_properties``: ``inspect_audio_file`` results for every incoming
    file. ``library_items``: Beets' stored ``format``/``bitrate``/
    ``samplerate``/``bitdepth`` for every item of the matched library album(s)."""
    src, lib = _source_rows(source_properties), _library_rows(library_items)
    if not src or not lib:
        return None
    worst_src = max(format_rank(r["format"], prefs) for r in src)
    best_lib = min(format_rank(r["format"], prefs) for r in lib)
    if worst_src >= best_lib:
        return None
    return {"library_format": _describe(lib), "source_format": _describe(src)}
