"""ListenBrainz MBID-mapping lookup: an extra source of recording CANDIDATES.

``GET https://api.listenbrainz.org/1/metadata/lookup/`` maps an artist and
recording name to a MusicBrainz recording MBID. That answer is a fuzzy text
mapping (tier-5 evidence), so here it is only a candidate:

* it counts only after a MusicBrainz recording lookup agrees with it on
  title, artist, tracklist (the recording is on the release ListenBrainz
  named) and duration; any disagreement drops it;
* a candidate that passes is still review-only (``review_required``). It
  carries no embedded-ID or fingerprint proof, so the canonical evaluator
  (``backend.matching.evaluate_recording_candidate``) never lets it attach
  identity on its own.

Every outbound call goes through ``provider_boundary`` ("listenbrainz" for
the mapping, "musicbrainz" for verification). The endpoint needs a user
token (``LISTENBRAINZ_TOKEN``); without one nothing is sent. The token is
never logged, and failures carry fixed strings only.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

import backend.provider_boundary as provider_boundary
from backend import config_layers
from backend.matching.normalize import (lookup_title, normalize_artist, similarity, strip_track_filename_id_suffix,
                                        track_feature_variants)
from backend.matching.recording import ARTIST_STRONG, DURATION_TOLERANCE_SECONDS, TITLE_STRONG
from backend.provider_boundary import ProviderOutcome, ProviderResult

LOG = logging.getLogger(__name__)

LOOKUP_URL = "https://api.listenbrainz.org/1/metadata/lookup/"
SOURCE = "listenbrainz"
TIMEOUT_SECONDS = 8
MAX_BODY_BYTES = 64 * 1024
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _uuid(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if _UUID_RE.match(text) else ""


def _token() -> str:
    try:
        return config_layers.legacy_env_value("LISTENBRAINZ_TOKEN").strip()
    except Exception:
        return ""


def _query_variants(title: str, artist: str) -> List[Tuple[str, str]]:
    """First a lightly cleaned query (source-ID suffix and ``feat.`` gone,
    case kept), then -- the one retry -- an aggressively cleaned one."""
    light = strip_track_filename_id_suffix(title).strip()
    light = (track_feature_variants(light)[1:2] or [light])[0]
    variants = [(light, str(artist or "").strip())]
    cleaned = (lookup_title(title), normalize_artist(artist))
    if cleaned[0] and (cleaned[0], cleaned[1]) != (variants[0][0].casefold(), variants[0][1].casefold()):
        variants.append(cleaned)
    return [(t, a) for t, a in variants if t and a]


def _lookup_once(title: str, artist: str, token: str) -> ProviderResult:
    params = urllib.parse.urlencode({"artist_name": artist, "recording_name": title})
    req = urllib.request.Request(f"{LOOKUP_URL}?{params}", headers={
        "Authorization": f"Token {token}", "User-Agent": "BeetsWebControl/1.0", "Accept": "application/json"})
    try:
        with provider_boundary.opened("listenbrainz", req, timeout=TIMEOUT_SECONDS) as response:
            body = json.loads(response.read(MAX_BODY_BYTES + 1)[:MAX_BODY_BYTES] or b"{}")
    except Exception as exc:  # classified; the message is a fixed string, never the exception text
        err = provider_boundary.classify_exception(exc)
        return ProviderResult(SOURCE, err.outcome, message="ListenBrainz lookup failed",
                              status_code=err.status_code)
    rid = _uuid(body.get("recording_mbid")) if isinstance(body, dict) else ""
    if not rid:
        return ProviderResult(SOURCE, ProviderOutcome.NO_RESULT)
    return ProviderResult(SOURCE, ProviderOutcome.CONFIRMED, data={
        "recording_mbid": rid,
        "release_mbid": _uuid(body.get("release_mbid")),
        "recording_name": str(body.get("recording_name") or ""),
        "artist_credit_name": str(body.get("artist_credit_name") or ""),
    })


def lookup_recording(title: str, artist: str) -> ProviderResult:
    """The ListenBrainz mapping for ``artist`` / ``title``: a CONFIRMED
    result's ``data`` is the raw mapping (unverified). A miss on the first
    query is retried once with the cleaned query; a failure is not retried
    here (the boundary already applied the provider policy)."""
    token = _token()
    if not token:
        return ProviderResult(SOURCE, ProviderOutcome.NOT_CONFIGURED, message="ListenBrainz token not configured")
    result = ProviderResult(SOURCE, ProviderOutcome.NO_RESULT)
    for q_title, q_artist in _query_variants(title, artist):
        result = _lookup_once(q_title, q_artist, token)
        if result.outcome != ProviderOutcome.NO_RESULT:
            return result
    return result


def _seconds(value: Any) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def verify_mapping(mapping: Dict[str, Any], *, title: str, artist: str, duration_seconds: Any,
                   fetch_recording: Callable[[str, str], Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], str]:
    """Check a ListenBrainz mapping against MusicBrainz. Returns
    ``(candidate, "")`` when MusicBrainz agrees on title, artist, tracklist
    and duration, else ``(None, reason)``. The candidate has the
    ``_mb_recording_search`` shape and is always ``review_required``."""
    rid = _uuid(mapping.get("recording_mbid"))
    if not rid:
        return None, "no_recording_id"
    details = fetch_recording(rid, _uuid(mapping.get("release_mbid"))) or {}
    mb_id = _uuid(details.get("recording_id"))
    if not mb_id:
        return None, "musicbrainz_lookup_failed"
    mb_title = str(details.get("recording_title") or "")
    mb_artist = str(details.get("recording_artist") or details.get("artist") or "")
    title_score = max(similarity(title, mb_title), similarity(lookup_title(title), lookup_title(mb_title)))
    if title_score < TITLE_STRONG:
        return None, "title_disagrees"
    artist_score = similarity(normalize_artist(artist), normalize_artist(mb_artist))
    if artist_score < ARTIST_STRONG:
        return None, "artist_disagrees"
    linked = [r for r in details.get("linked_releases") or [] if isinstance(r, dict)]
    release_ids = {_uuid(r.get("mb_albumid")) for r in linked} - {""}
    lb_release = _uuid(mapping.get("release_mbid"))
    if not release_ids or (lb_release and lb_release not in release_ids):
        return None, "tracklist_disagrees"
    local_seconds = _seconds(duration_seconds)
    mb_seconds = _seconds(details.get("recording_length_ms")) / 1000.0
    if not local_seconds or not mb_seconds:
        return None, "duration_unverifiable"
    delta = abs(local_seconds - mb_seconds)
    if delta > DURATION_TOLERANCE_SECONDS:
        return None, "duration_disagrees"
    selected = details.get("selected_release") or {}
    whole = int(mb_seconds)
    return {
        "score": int(round(100 * min(title_score, artist_score))),
        "mb_trackid": mb_id,
        "mb_url": f"https://musicbrainz.org/recording/{mb_id}",
        "title": mb_title,
        "artist": mb_artist,
        "album": str(details.get("album") or ""),
        "mb_albumid": _uuid(details.get("mb_albumid")),
        "mb_albumids": sorted(release_ids),
        # Edition context only; canonical album identity stays the release group.
        "mb_releasegroupid": _uuid(selected.get("mb_releasegroupid") or details.get("mb_releasegroupid")),
        "year": str(details.get("year") or ""),
        "duration": f"{whole // 60}:{whole % 60:02d}",
        "source": SOURCE,
        "listenbrainz_verified": True,
        "review_required": True,
        "duration_delta_seconds": round(delta, 1),
    }, ""


def _default_fetch(rid: str, release_id: str) -> Dict[str, Any]:
    from helpers_mb import _fetch_mb_recording_details  # lazy: helpers_mb imports the app runtime
    return _fetch_mb_recording_details(rid, release_id)


def listenbrainz_recording_candidates(title: str, artist: str, duration_seconds: Any, *,
                                      fetch_recording: Optional[Callable[[str, str], Dict[str, Any]]] = None
                                      ) -> Dict[str, Any]:
    """MusicBrainz-verified ListenBrainz candidates (0 or 1) for one track.

    Returns ``{"status", "candidates", "reason"}``; ``status`` is the
    ProviderOutcome value of the mapping lookup. Never raises: a provider
    that could not be asked yields no candidates, which callers must treat
    as "not asked", never as "no match"."""
    try:
        result = lookup_recording(str(title or ""), str(artist or ""))
    except Exception:  # defensive: candidate generation must never break the caller
        LOG.warning("ListenBrainz mapping lookup failed")
        return {"status": ProviderOutcome.TRANSIENT_ERROR.value, "candidates": [], "reason": "lookup_failed"}
    if result.outcome != ProviderOutcome.CONFIRMED:
        return {"status": result.outcome.value, "candidates": [], "reason": ""}
    try:
        candidate, reason = verify_mapping(result.data, title=str(title or ""), artist=str(artist or ""),
                                           duration_seconds=duration_seconds,
                                           fetch_recording=fetch_recording or _default_fetch)
    except Exception:
        LOG.warning("ListenBrainz candidate verification failed")
        candidate, reason = None, "musicbrainz_lookup_failed"
    return {"status": result.outcome.value, "candidates": [candidate] if candidate else [], "reason": reason}
