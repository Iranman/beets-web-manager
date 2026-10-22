"""Resolve an on-disk album folder to its MusicBrainz release-group MBID via Lidarr.

Read-only. This module *plans* a rename; it performs no filesystem or library
mutation. The folder-rename workflow (library-transaction-engineer) consumes
``resolve(...)`` and renames album folders to ``Album (Year) {release-group-mbid}``.

Lidarr's ``album.foreignAlbumId`` IS the release-group MBID and
``artist.foreignArtistId`` IS the artist MBID (confirmed against the official
Lidarr v1 API docs). One bulk ``GET /api/v1/album`` snapshot per run carries
both, so a single call indexes every album by its artist MBID.

Contract (``resolve`` / ``LidarrRgidResolver.resolve``):
    {"status": "resolved", "release_group_mbid", "matched_tier", "lidarr_album_id"}
  | {"status": "unresolved", "reason"}
A provider/transport failure (timeout, 401/403, 5xx, rate limit, malformed
body, outbound-policy block, not configured) is raised as a
``provider_boundary.ProviderError`` from the snapshot fetch and MUST abort the
run -- it is never downgraded to an "unresolved" result. ``resolve`` itself
operates on the already-fetched in-memory snapshot and never touches the
network, so it never raises a provider error.

Only a syntactically valid MBID taken verbatim from Lidarr ``foreignAlbumId``
is ever returned; nothing is invented, guessed or partially filled.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

import backend.app_runtime as app_runtime
import backend.provider_boundary as provider_boundary
from backend.app_runtime import _is_valid_mb_uuid, _s, _ur

# Unresolved reasons (the only non-provider-failure outcomes).
ARTIST_NOT_IN_LIDARR = "artist_not_in_lidarr"
ALBUM_NOT_IN_LIDARR = "album_not_in_lidarr"
AMBIGUOUS_MULTIPLE_ALBUMS = "ambiguous_multiple_albums"
LIDARR_MISSING_RGID = "lidarr_missing_rgid"

# albumType preference for a tie-break: Album > EP > Single > Compilation/Other.
_TYPE_RANK = {"album": 0, "ep": 1, "single": 2, "compilation": 3}
_OTHER_TYPE_RANK = 4


def _norm(value: Any) -> str:
    """Lowercased alphanumerics only -- matches routes_lidarr._norm."""
    return "".join(ch for ch in _s(value).lower() if ch.isalnum())


def _artist_mbid(album: Dict[str, Any]) -> str:
    artist = album.get("artist")
    if not isinstance(artist, dict):
        return ""
    return _s(artist.get("foreignArtistId") or "").strip().lower()


def _album_year(album: Dict[str, Any]) -> str:
    for key in ("releaseDate", "release_date", "firstReleaseDate"):
        value = _s(album.get(key) or "")
        if len(value) >= 4:
            return value[:4]
    return _s(album.get("year") or "")


def _album_basename(album: Dict[str, Any]) -> str:
    path = _s(album.get("path") or album.get("folder") or album.get("albumFolder") or "")
    # Lidarr paths use the host OS separator; split on both so a basename
    # comparison works regardless of where the snapshot came from. (Pure
    # string work on a value from the snapshot -- never a filesystem call.)
    parts = [segment for segment in re.split(r"[\\/]+", path) if segment]
    return parts[-1] if parts else ""


def _type_rank(album: Dict[str, Any]) -> int:
    return _TYPE_RANK.get(_s(album.get("albumType") or album.get("type") or "").lower(), _OTHER_TYPE_RANK)


def _track_file_count(album: Dict[str, Any]) -> int:
    stats = album.get("statistics") or {}
    try:
        return int(stats.get("trackFileCount") or album.get("trackFileCount") or 0)
    except (TypeError, ValueError):
        return 0


def _release_group_mbid(album: Dict[str, Any]) -> str:
    """The album's release-group MBID straight from Lidarr, or "" if it is
    missing or not a syntactically valid MBID (never coerced or guessed)."""
    value = _s(album.get("foreignAlbumId") or "").strip()
    return value if _is_valid_mb_uuid(value) else ""


# -- the tiered matcher ---------------------------------------------------------

def _tie_break(candidates: List[Dict[str, Any]], want_year: str) -> Optional[Dict[str, Any]]:
    """Narrow >1 candidates to exactly one, or None if still ambiguous.

    Applied in order: exact year == folder year; then albumType rank; then
    monitored; then trackFileCount > 0. Each step keeps the best subset; a
    step that would empty the set is skipped (it could not decide). Never
    picks arbitrarily -- an undecided tie returns None."""
    remaining = list(candidates)

    def narrow(subset: List[Dict[str, Any]]) -> bool:
        nonlocal remaining
        if subset and len(subset) < len(remaining):
            remaining = subset
        return len(remaining) == 1

    if want_year and narrow([a for a in remaining if _album_year(a) == want_year]):
        return remaining[0]
    best = min(_type_rank(a) for a in remaining)
    if narrow([a for a in remaining if _type_rank(a) == best]):
        return remaining[0]
    if narrow([a for a in remaining if bool(a.get("monitored", False))]):
        return remaining[0]
    if narrow([a for a in remaining if _track_file_count(a) > 0]):
        return remaining[0]
    return remaining[0] if len(remaining) == 1 else None


def _resolved(album: Dict[str, Any], tier: str) -> Dict[str, Any]:
    rgid = _release_group_mbid(album)
    if not rgid:
        return {"status": "unresolved", "reason": LIDARR_MISSING_RGID}
    return {
        "status": "resolved",
        "release_group_mbid": rgid,
        "matched_tier": tier,
        "lidarr_album_id": int(album.get("id") or 0),
    }


def resolve(index: Dict[str, List[Dict[str, Any]]], *, artist_mbid: str, album_title: str,
            album_year: str = "", album_basename: str = "") -> Dict[str, Any]:
    """Resolve one on-disk album folder against a prebuilt snapshot index.

    ``index`` comes from ``index_by_artist(snapshot)``. Pure and
    network-free: returns a resolved/unresolved dict, never raises a provider
    error. See the module docstring for the contract."""
    candidates = index.get(_s(artist_mbid).strip().lower()) or []
    if not candidates:
        return {"status": "unresolved", "reason": ARTIST_NOT_IN_LIDARR}

    want_title = _norm(album_title)
    want_year = _s(album_year).strip()[:4]
    want_base = _norm(album_basename)

    # Tier A: exact folder-basename match. Lidarr paths usually differ from
    # the on-disk layout, so this rarely hits -- kept, not relied upon. A
    # non-unique basename match is not terminal; fall through to the stronger
    # title tiers.  ponytail: linear scan per artist (tens of albums), fine.
    if want_base:
        tier_a = [a for a in candidates if _norm(_album_basename(a)) == want_base]
        if len(tier_a) == 1:
            return _resolved(tier_a[0], "path_exact")
        if len(tier_a) > 1:
            chosen = _tie_break(tier_a, want_year)
            if chosen is not None:
                return _resolved(chosen, "path_exact")

    if not want_title:
        # No title to match on and Tier A did not uniquely resolve.
        return {"status": "unresolved", "reason": ALBUM_NOT_IN_LIDARR}

    # Tier B: normalized title + year. Tier C: normalized title only. A
    # multi-match that tie-breakers cannot settle is terminal -- the broader
    # title-only tier can only widen it, never shrink it.
    for tier, matches in (
        ("title_year", [a for a in candidates if _norm(a.get("title") or a.get("albumTitle")) == want_title
                         and want_year and _album_year(a) == want_year]),
        ("title_only", [a for a in candidates if _norm(a.get("title") or a.get("albumTitle")) == want_title]),
    ):
        if not matches:
            continue
        if len(matches) == 1:
            return _resolved(matches[0], tier)
        chosen = _tie_break(matches, want_year)
        if chosen is not None:
            return _resolved(chosen, tier)
        return {"status": "unresolved", "reason": AMBIGUOUS_MULTIPLE_ALBUMS}

    return {"status": "unresolved", "reason": ALBUM_NOT_IN_LIDARR}


# -- the snapshot (one bulk call per run) ---------------------------------------

def index_by_artist(snapshot: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Group a ``GET /api/v1/album`` snapshot by artist MBID (foreignArtistId).

    Albums without a valid artist MBID are dropped: they cannot be scoped to
    an on-disk ``Artist (artist-mbid)`` folder."""
    index: Dict[str, List[Dict[str, Any]]] = {}
    for album in snapshot:
        if not isinstance(album, dict):
            continue
        mbid = _artist_mbid(album)
        if mbid:
            index.setdefault(mbid, []).append(album)
    return index


def fetch_album_snapshot(*, timeout: float = 30.0) -> List[Dict[str, Any]]:
    """Fetch the full Lidarr album list via the provider boundary ("lidarr").

    One bulk ``GET /api/v1/album`` (37k+ albums). Reads LIDARR_URL/LIDARR_KEY
    the same way the rest of the app does (backend.app_runtime, honouring the
    *_FILE fallbacks resolved at boot). Raises ``provider_boundary.ProviderError``
    -- classified (not configured / auth / rate limited / unavailable /
    transient / rejected) -- on any failure, so the caller aborts the run."""
    url = _s(getattr(app_runtime, "LIDARR_URL", "")).strip()
    key = _s(getattr(app_runtime, "LIDARR_KEY", "")).strip()
    if not key:
        raise provider_boundary.ProviderError(
            provider_boundary.ProviderOutcome.NOT_CONFIGURED, "LIDARR_API_KEY not configured", provider="lidarr")
    if not url:
        raise provider_boundary.ProviderError(
            provider_boundary.ProviderOutcome.NOT_CONFIGURED, "LIDARR_URL not configured", provider="lidarr")

    req = _ur.Request(
        f"{url.rstrip('/')}/api/v1/album",
        headers={"Accept": "application/json", "X-Api-Key": key},
        method="GET",
    )
    try:
        with provider_boundary.opened("lidarr", req, timeout=timeout) as resp:
            data = json.loads(resp.read() or b"[]")
    except provider_boundary.ProviderError:
        raise
    except Exception as exc:  # classify timeout/HTTP/URL/malformed/policy -> ProviderError
        err = provider_boundary.classify_exception(exc)
        err.provider = err.provider or "lidarr"
        raise err from exc
    if not isinstance(data, list):
        raise provider_boundary.ProviderError(
            provider_boundary.ProviderOutcome.TRANSIENT_ERROR, "malformed response", provider="lidarr")
    return data


class LidarrRgidResolver:
    """One snapshot, many resolves.

    ``LidarrRgidResolver.from_lidarr()`` makes the single bulk call (and
    raises ``provider_boundary.ProviderError`` on a provider failure, aborting
    the run); ``.resolve(...)`` is then called once per on-disk album folder
    against the in-memory index and never touches the network."""

    def __init__(self, snapshot: List[Dict[str, Any]]):
        self.index = index_by_artist(snapshot)

    @classmethod
    def from_lidarr(cls, *, timeout: float = 30.0) -> "LidarrRgidResolver":
        return cls(fetch_album_snapshot(timeout=timeout))

    def resolve(self, *, artist_mbid: str, album_title: str, album_year: str = "",
                album_basename: str = "") -> Dict[str, Any]:
        return resolve(self.index, artist_mbid=artist_mbid, album_title=album_title,
                       album_year=album_year, album_basename=album_basename)
