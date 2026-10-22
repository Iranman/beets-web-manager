"""Resolve an on-disk album folder to its MusicBrainz release-group MBID via Lidarr.

Read-only. This module *plans* a rename; it performs no filesystem or library
mutation. The folder-rename workflow (library-transaction-engineer) consumes
``resolve(...)`` and renames album folders to ``Album (Year) {release-group-mbid}``.

Lidarr's ``album.foreignAlbumId`` IS the release-group MBID and
``artist.foreignArtistId`` IS the artist MBID (confirmed against the official
Lidarr v1 API docs). The snapshot is SCOPED to the artists actually on disk:
one ``GET /api/v1/artist`` maps each needed artist MBID to its internal
``artist.id``, then one ``GET /api/v1/album?artistId=<id>`` per needed artist
pulls just that artist's albums (each still carries ``artist.foreignArtistId``,
so the in-memory index groups by artist MBID exactly as before). The full
``GET /api/v1/album`` (tens of thousands of albums, hundreds of MB -- it times
out on a large Lidarr) is NEVER fetched.

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


def _collapse_rgid(matches: List[Dict[str, Any]]) -> Optional[str]:
    """The single release-group MBID shared by *all* matches, or None.

    None means it is NOT safe to tie-break: either no match carries a valid
    RGID, or the matches span more than one distinct release group. Renaming a
    folder with an RGID establishes canonical identity, so choosing among
    *different* release groups would be a guess -- that is held for review, not
    tie-broken. Choosing among editions of ONE release group is safe: the
    emitted RGID is identical whichever edition wins."""
    rgids = {_release_group_mbid(a) for a in matches}
    if len(rgids) == 1:
        return next(iter(rgids)) or None  # "" (all missing) -> None
    return None


def _multi_unresolved_reason(matches: List[Dict[str, Any]]) -> str:
    """Reason for a multi-match set that _collapse_rgid refused to resolve:
    all missing a valid RGID -> missing; otherwise distinct RGIDs -> ambiguous."""
    if any(_release_group_mbid(a) for a in matches):
        return AMBIGUOUS_MULTIPLE_ALBUMS
    return LIDARR_MISSING_RGID


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
        if len(tier_a) > 1 and _collapse_rgid(tier_a):
            # Multiple basename matches but a single distinct RGID: editions of
            # one release group -- safe to resolve. Different RGIDs fall through
            # to the title tiers (Tier A is never terminal).
            chosen = _tie_break(tier_a, want_year) or tier_a[0]
            return _resolved(chosen, "path_exact")

    if not want_title:
        # No title to match on and Tier A did not uniquely resolve.
        return {"status": "unresolved", "reason": ALBUM_NOT_IN_LIDARR}

    # Tier B: normalized title + year. Tier C: normalized title only. A match
    # resolves only when every matched album shares ONE release-group MBID --
    # a single edition, or several editions of the same release group (a
    # tie-break then only picks which edition's id to report, never which
    # RGID). If matches span DISTINCT release groups, renaming the folder would
    # bake in a guessed identity, so it is held for review. A multi-match is
    # terminal: the broader title-only tier can only widen the set, never
    # shrink it to one release group.
    for tier, matches in (
        ("title_year", [a for a in candidates if _norm(a.get("title") or a.get("albumTitle")) == want_title
                         and want_year and _album_year(a) == want_year]),
        ("title_only", [a for a in candidates if _norm(a.get("title") or a.get("albumTitle")) == want_title]),
    ):
        if not matches:
            continue
        if len(matches) == 1:
            return _resolved(matches[0], tier)
        if _collapse_rgid(matches):
            chosen = _tie_break(matches, want_year) or matches[0]
            return _resolved(chosen, tier)
        return {"status": "unresolved", "reason": _multi_unresolved_reason(matches)}

    return {"status": "unresolved", "reason": ALBUM_NOT_IN_LIDARR}


# -- the snapshot (scoped per-artist fetch) -------------------------------------

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


def _lidarr_base():
    """(LIDARR_URL, LIDARR_KEY), read the same way the rest of the app does
    (backend.app_runtime, honouring the *_FILE fallbacks resolved at boot).
    Raises NOT_CONFIGURED if either is missing."""
    url = _s(getattr(app_runtime, "LIDARR_URL", "")).strip()
    key = _s(getattr(app_runtime, "LIDARR_KEY", "")).strip()
    if not key:
        raise provider_boundary.ProviderError(
            provider_boundary.ProviderOutcome.NOT_CONFIGURED, "LIDARR_API_KEY not configured", provider="lidarr")
    if not url:
        raise provider_boundary.ProviderError(
            provider_boundary.ProviderOutcome.NOT_CONFIGURED, "LIDARR_URL not configured", provider="lidarr")
    return url, key


# ── TEMPORARY boot-trust diagnostic (v0.13.3) ─────────────────────────────────
# Fires ONLY when the folder-MBID resolver's Lidarr call is REJECTED by the
# outbound URL policy, to capture (from the live serving process's own job
# thread) why the configured-URL trust was not active there while a fresh
# `import app` admits the same call. One WARNING line, once per process. Logs
# only non-secret base URLs (userinfo stripped), rule and module ids -- NEVER
# the Lidarr API key. Remove once the live cause is confirmed.
_rejection_diagnostic_logged = False


def _redact_origin(url: str) -> str:
    """scheme://host:port only (drop any userinfo/path/query), so a logged URL
    can never carry a credential even if one were ever embedded."""
    try:
        import urllib.parse as _up
        p = _up.urlsplit(_s(url or "").strip())
        if not p.scheme or not p.hostname:
            return "" if not url else "<unparseable>"
        host = f"[{p.hostname}]" if ":" in p.hostname else p.hostname
        return f"{p.scheme}://{host}" + (f":{p.port}" if p.port else "")
    except Exception:
        return "<redacted>"


def _log_rejection_diagnostic(err: Any) -> None:
    global _rejection_diagnostic_logged
    try:
        if getattr(err, "outcome", None) != provider_boundary.ProviderOutcome.REJECTED:
            return
        if _rejection_diagnostic_logged:
            return
        _rejection_diagnostic_logged = True
        import sys as _sys
        from backend import security as _security
        resolver = provider_boundary._CONFIGURED_URL_RESOLVERS.get("lidarr")
        resolver_value = None
        if resolver is not None:
            try:
                resolver_value = _redact_origin(resolver())
            except Exception as exc:  # pragma: no cover - diagnostic must never raise
                resolver_value = f"<resolver raised {type(exc).__name__}>"
        cfg_url = provider_boundary.configured_url("lidarr")
        # What opened() computes as the trust for this call. _TRUSTED_RULE itself
        # is already reset here (opened()'s finally ran before this except), so
        # recompute it: not-None here but a rejected call would mean the trust
        # was built yet not seen at urlopen (a duplicate backend.security);
        # None here means the configured-URL path yielded no trusting rule.
        would_trust = _security.implicit_rule_for_url(cfg_url)
        app_runtime._app_logger.warning(
            "LIDARR TRUST DIAGNOSTIC (temporary, v0.13.3): folder-MBID Lidarr call "
            "REJECTED by outbound policy. configured_url(lidarr)=%r resolver_registered=%s "
            "resolver()=%r app_runtime.LIDARR_URL=%r would_trust_rule=%r live_TRUSTED_RULE=%r "
            "ids{app_runtime=%s security=%s provider_boundary=%s}",
            _redact_origin(cfg_url),
            resolver is not None,
            resolver_value,
            _redact_origin(getattr(app_runtime, "LIDARR_URL", "")),
            repr(would_trust),            # OutboundAllowRule: scheme/host/ip/port only, no credential
            repr(_security._TRUSTED_RULE.get()),
            id(_sys.modules.get("backend.app_runtime")),
            id(_sys.modules.get("backend.security")),
            id(_sys.modules.get("backend.provider_boundary")),
        )
    except Exception:  # pragma: no cover - a diagnostic must never change behavior
        pass


def _lidarr_get_list(path: str, *, timeout: float) -> List[Dict[str, Any]]:
    """GET a Lidarr v1 JSON array via the provider boundary ("lidarr").

    ``path`` is an absolute API path, e.g. ``/api/v1/artist`` or
    ``/api/v1/album?artistId=3``. Raises a classified
    ``provider_boundary.ProviderError`` (not configured / auth / rate limited /
    unavailable / transient / rejected) on ANY failure, so the caller aborts the
    run -- a provider failure is never downgraded to an "unresolved" result."""
    url, key = _lidarr_base()
    req = _ur.Request(
        f"{url.rstrip('/')}{path}",
        headers={"Accept": "application/json", "X-Api-Key": key},
        method="GET",
    )
    try:
        with provider_boundary.opened("lidarr", req, timeout=timeout) as resp:
            data = json.loads(resp.read() or b"[]")
    except provider_boundary.ProviderError as err:
        _log_rejection_diagnostic(err)
        raise
    except Exception as exc:  # classify timeout/HTTP/URL/malformed/policy -> ProviderError
        err = provider_boundary.classify_exception(exc)
        err.provider = err.provider or "lidarr"
        _log_rejection_diagnostic(err)
        raise err from exc
    if not isinstance(data, list):
        raise provider_boundary.ProviderError(
            provider_boundary.ProviderOutcome.TRANSIENT_ERROR, "malformed response", provider="lidarr")
    return data


def fetch_artist_id_map(*, timeout: float = 30.0) -> Dict[str, int]:
    """One ``GET /api/v1/artist`` -> {artist MBID (foreignArtistId, lowercased):
    internal ``artist.id``}. The artist list is far smaller than the album list
    (one artist row vs all its albums), so a single call is cheap. Raises
    ``provider_boundary.ProviderError`` on any provider failure."""
    out: Dict[str, int] = {}
    for artist in _lidarr_get_list("/api/v1/artist", timeout=timeout):
        if not isinstance(artist, dict):
            continue
        mbid = _s(artist.get("foreignArtistId") or "").strip().lower()
        aid = artist.get("id")
        if mbid and isinstance(aid, int):
            out[mbid] = aid
    return out


def fetch_albums_for_artist(artist_id: int, *, timeout: float = 30.0) -> List[Dict[str, Any]]:
    """One ``GET /api/v1/album?artistId=<id>`` -- just this artist's albums (a
    small payload). Each album still embeds ``artist.foreignArtistId``, so
    ``index_by_artist`` groups it by artist MBID exactly as before. Raises
    ``provider_boundary.ProviderError`` on any provider failure."""
    return _lidarr_get_list(f"/api/v1/album?artistId={int(artist_id)}", timeout=timeout)


def fetch_album_snapshot(artist_mbids, *, timeout: float = 30.0) -> List[Dict[str, Any]]:
    """Scoped album snapshot for only the given artist MBIDs.

    One ``GET /api/v1/artist`` maps the DISTINCT needed MBIDs to internal ids,
    then one ``GET /api/v1/album?artistId=<id>`` per artist that Lidarr knows.
    The full ``GET /api/v1/album`` (tens of thousands of albums) is NEVER
    fetched. An artist MBID not in Lidarr is simply skipped (no album call); the
    resolver then yields ``ARTIST_NOT_IN_LIDARR`` for that artist's folders.
    Lookups are deduped and cached for the run (the DISTINCT MBID set + one
    artist map). Raises a classified ``provider_boundary.ProviderError`` on any
    provider failure, so the caller aborts the run."""
    wanted = sorted({_s(m).strip().lower() for m in (artist_mbids or ()) if _s(m).strip()})
    if not wanted:
        return []
    id_map = fetch_artist_id_map(timeout=timeout)
    snapshot: List[Dict[str, Any]] = []
    for mbid in wanted:
        aid = id_map.get(mbid)
        if aid is None:
            continue  # artist absent in Lidarr -> ARTIST_NOT_IN_LIDARR downstream
        snapshot.extend(fetch_albums_for_artist(aid, timeout=timeout))
    return snapshot


class LidarrRgidResolver:
    """One snapshot, many resolves.

    ``LidarrRgidResolver.from_lidarr(artist_mbids)`` fetches only those artists'
    albums (and raises ``provider_boundary.ProviderError`` on a provider
    failure, aborting the run); ``.resolve(...)`` is then called once per
    on-disk album folder against the in-memory index and never touches the
    network."""

    def __init__(self, snapshot: List[Dict[str, Any]]):
        self.index = index_by_artist(snapshot)

    @classmethod
    def from_lidarr(cls, artist_mbids, *, timeout: float = 30.0) -> "LidarrRgidResolver":
        """Fetch albums for only ``artist_mbids`` (an iterable of artist MBIDs,
        deduped) and index them. See ``fetch_album_snapshot`` for the scoped
        fetch contract -- the full album list is never fetched."""
        return cls(fetch_album_snapshot(artist_mbids, timeout=timeout))

    def resolve(self, *, artist_mbid: str, album_title: str, album_year: str = "",
                album_basename: str = "") -> Dict[str, Any]:
        return resolve(self.index, artist_mbid=artist_mbid, album_title=album_title,
                       album_year=album_year, album_basename=album_basename)
