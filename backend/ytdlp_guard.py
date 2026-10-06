"""Outbound policy for yt-dlp (SEC-2).

yt-dlp makes its own HTTP connections in-process, so neither the global
``secure_urlopen`` policy nor ``open_public_url`` applies to it. Its
*generic* extractor will fetch any URL it is given -- including loopback,
link-local cloud metadata and the operator's internal services -- and hand
back page titles. Every ``yt_dlp.YoutubeDL(...)`` construction in this
application therefore goes through :func:`ytdlp_guarded_options`, which

* disables the generic extractor (``allowed_extractors``), and
* refuses any target that is neither a yt-dlp search query
  (``ytsearchN:``/``scsearchN:``) nor an http(s) URL on an allowlisted media
  host whose DNS answers are all public addresses.

A structural test (tests/test_sec_ytdlp_guard.py) requires every
``YoutubeDL(`` call site to pass its options through this function.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any, Dict, Iterable

from backend.security import OutboundPolicyError, resolve_public_target

# Media hosts yt-dlp is used for. Subdomains match (music.youtube.com,
# m.soundcloud.com, <artist>.bandcamp.com). No suffix matching beyond a
# real dot boundary.
YTDLP_ALLOWED_HOSTS = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "soundcloud.com",
    "bandcamp.com",
    "mixcloud.com",
    "vimeo.com",
    "deezer.com",
)

YTDLP_ALLOWED_EXTRACTORS = ["default", "-generic"]

_SEARCH_QUERY_RE = re.compile(r"^(?:ytsearch|ytsearchdate|scsearch)(?:\d{1,3}|all)?:", re.IGNORECASE)


class YtdlpTargetRejected(ValueError):
    """A yt-dlp target is outside the allowed hosts or not public."""


def ytdlp_host_allowed(host: str) -> bool:
    clean = (host or "").strip().rstrip(".").lower()
    return bool(clean) and any(clean == d or clean.endswith("." + d) for d in YTDLP_ALLOWED_HOSTS)


def ytdlp_target_allowed(target: Any) -> bool:
    """True for a yt-dlp search query, or an http(s) URL on an allowlisted
    media host that resolves only to public addresses. Never raises."""
    try:
        check_ytdlp_target(target)
        return True
    except YtdlpTargetRejected:
        return False


def check_ytdlp_target(target: Any) -> None:
    text = str(target or "").strip()
    if not text:
        raise YtdlpTargetRejected("empty yt-dlp target")
    if _SEARCH_QUERY_RE.match(text):
        return
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError as exc:
        raise YtdlpTargetRejected("unparseable yt-dlp target") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise YtdlpTargetRejected("yt-dlp target must be an http(s) URL or a search query")
    if not ytdlp_host_allowed(parsed.hostname or ""):
        raise YtdlpTargetRejected("yt-dlp target host is not a supported media site")
    try:
        resolve_public_target(text)
    except OutboundPolicyError as exc:
        raise YtdlpTargetRejected(f"yt-dlp target is not allowed: {exc}") from exc


def ytdlp_guarded_options(opts: Dict[str, Any], targets: Iterable[Any]) -> Dict[str, Any]:
    """Return a copy of ``opts`` with the generic extractor disabled, after
    checking every target. Raises YtdlpTargetRejected for a bad target."""
    for target in targets:
        check_ytdlp_target(target)
    guarded = dict(opts)
    guarded["allowed_extractors"] = list(YTDLP_ALLOWED_EXTRACTORS)
    return guarded
