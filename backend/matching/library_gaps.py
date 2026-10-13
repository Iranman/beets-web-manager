"""Which Beets library rows are missing canonical MusicBrainz identity (I-7).

Pure: takes the adapter's album and item rows, returns the gap lists. Album
identity is keyed on ``mb_releasegroupid`` (ADR-0002); a Release ID alone is
edition evidence, so an album with ``mb_albumid`` but no Release Group is a
gap. The caller decides which rows are in scope (for example, only rows under
MUSIC_ROOT) and how to page them.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

#: Gap kinds, in the audit's subtype order.
ALBUM_RELEASE_WITHOUT_GROUP = "album_release_without_group"  # (a) mb_albumid, no mb_releasegroupid
ALBUM_WITHOUT_IDS = "album_without_ids"  # (b) neither ID
ALBUM_GROUP_WITHOUT_RELEASE = "album_group_without_release"  # (b2) Release Group only; informational
ALBUM_TRACK_WITHOUT_RECORDING = "album_track_without_recording"  # (c) album item, no mb_trackid
SINGLETON_WITHOUT_RECORDING = "singleton_without_recording"  # (d) singleton, no mb_trackid

GAP_KINDS = (
    ALBUM_RELEASE_WITHOUT_GROUP,
    ALBUM_WITHOUT_IDS,
    ALBUM_GROUP_WITHOUT_RELEASE,
    ALBUM_TRACK_WITHOUT_RECORDING,
    SINGLETON_WITHOUT_RECORDING,
)

#: Kinds where canonical identity is actually missing. (b2) has its Release
#: Group, so it is listed for information only.
IDENTITY_MISSING_KINDS = frozenset(GAP_KINDS) - {ALBUM_GROUP_WITHOUT_RELEASE}


def _has_id(value: Any) -> bool:
    """A MusicBrainz ID counts only when it is a well-formed UUID."""
    return bool(_UUID_RE.match(str(value or "").strip().lower()))


def _int(row: Dict[str, Any], key: str) -> int:
    try:
        return int(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _added(row: Dict[str, Any]) -> float:
    try:
        return float(row.get("added") or 0)
    except (TypeError, ValueError):
        return 0.0


def library_identity_gaps(albums: Iterable[Dict[str, Any]],
                          items: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Return ``{kind: [row, ...]}`` for every kind in ``GAP_KINDS``.

    Album rows are copies of the album row plus ``first_item_id``,
    ``first_item_path`` and ``tracks``. Album-track rows (c) are copies of
    the item row plus the parent album's ``album_mb_albumid`` and
    ``album_mb_releasegroupid``. Each list is ordered by ``added`` then id.
    """
    items = list(items or [])
    albums = list(albums or [])
    by_album: Dict[int, List[Dict[str, Any]]] = {}
    for item in items:
        aid = _int(item, "album_id")
        if aid:
            by_album.setdefault(aid, []).append(item)
    albums_by_id = {}
    for album in albums:
        try:
            albums_by_id[int(album.get("id") or 0)] = album
        except (TypeError, ValueError):
            continue

    gaps: Dict[str, List[Dict[str, Any]]] = {kind: [] for kind in GAP_KINDS}

    for aid, album in albums_by_id.items():
        has_release = _has_id(album.get("mb_albumid"))
        has_group = _has_id(album.get("mb_releasegroupid"))
        if has_group and has_release:
            continue
        if has_group:
            kind = ALBUM_GROUP_WITHOUT_RELEASE
        elif has_release:
            kind = ALBUM_RELEASE_WITHOUT_GROUP
        else:
            kind = ALBUM_WITHOUT_IDS
        tracks = sorted(by_album.get(aid, []), key=lambda it: (_int(it, "disc"), _int(it, "track"), _int(it, "id")))
        first = tracks[0] if tracks else {}
        row = dict(album)
        row.update({"first_item_id": first.get("id", 0), "first_item_path": first.get("path", ""), "tracks": len(tracks)})
        gaps[kind].append(row)

    for item in items:
        if _has_id(item.get("mb_trackid")):
            continue
        aid = _int(item, "album_id")
        if aid:
            album = albums_by_id.get(aid, {})
            row = dict(item)
            row["album_mb_albumid"] = str(album.get("mb_albumid") or "")
            row["album_mb_releasegroupid"] = str(album.get("mb_releasegroupid") or "")
            gaps[ALBUM_TRACK_WITHOUT_RECORDING].append(row)
        else:
            gaps[SINGLETON_WITHOUT_RECORDING].append(dict(item))

    for rows in gaps.values():
        rows.sort(key=lambda r: (_added(r), _int(r, "id")))
    return gaps
