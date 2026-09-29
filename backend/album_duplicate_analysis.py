"""Read-only duplicate-album analysis (review input, never a mutation).

Canonical identity rules:

* Release Group ID (mb_releasegroupid) proves two album rows are the same
  canonical album. Rows without one are never grouped for merging.
* Release ID (mb_albumid) is edition evidence only; differing editions in a
  group are reported, and block a deterministic merge.
* Slots are (disc, track). A slot filled by one row is complementary; a slot
  filled by several rows overlaps. Overlapping copies are compared by
  Recording ID and -- where that is contested or missing -- by AcoustID,
  read from the file cache only (no fingerprinting, no API calls).

For every group the analysis proposes which album row to retain and which
item rows would move or need reconciling, and lists every blocker. A merge
is marked deterministic only for a single edition with no overlapping slots
and no items lacking a track position; everything else is review-only.
Nothing here writes to Beets.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

CachedIds = Callable[[str], Optional[List[str]]]


def _s(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _slot(item: Dict[str, Any]) -> Tuple[int, int]:
    return (_int(item.get("disc")) or 1, _int(item.get("track")))


def _retained_row(rows: List[Dict[str, Any]], majority_release: str) -> Dict[str, Any]:
    """Most tracks, then the majority edition, then the lowest album id."""
    return max(rows, key=lambda r: (len(r["items"]), _s(r["mb_albumid"]).lower() == majority_release, -r["id"]))


def _overlap_verdict(copies: List[Dict[str, Any]], cached_ids: Optional[CachedIds],
                     abs_path: Callable[[str], str]) -> Dict[str, Any]:
    rids = {_s(c.get("mb_trackid")).lower() for c in copies} - {""}
    verdict: Dict[str, Any] = {"recording_ids": sorted(rids)}
    if len(rids) == 1 and len(copies) == sum(1 for c in copies if _s(c.get("mb_trackid"))):
        verdict["relation"] = "same_recording_by_recording_id"
    elif len(rids) > 1:
        verdict["relation"] = "recording_ids_differ"
    else:
        verdict["relation"] = "recording_identity_missing"
    if verdict["relation"] != "same_recording_by_recording_id" and cached_ids is not None:
        heard = [cached_ids(abs_path(_s(c.get("path")))) for c in copies]
        verdict["acoustid_cached"] = [h if h is not None else "not_cached" for h in heard]
        known = [set(h) for h in heard if h]
        if len(known) == len(copies) and set.intersection(*known):
            verdict["relation"] = "same_recording_by_acoustid"
            verdict["acoustid_shared"] = sorted(set.intersection(*known))
        elif len(known) == len(copies):
            verdict["relation"] = "different_recordings_by_acoustid"
    return verdict


def analyze(albums: Iterable[Dict[str, Any]], items: Iterable[Dict[str, Any]], *,
            cached_ids: Optional[CachedIds] = None,
            abs_path: Callable[[str], str] = lambda p: p) -> Dict[str, Any]:
    """Group album rows by Release Group and propose a merge plan per group."""
    album_rows = {_int(a.get("id")): a for a in albums if _int(a.get("id"))}
    items_by_album: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for it in items:
        if _int(it.get("album_id")):
            items_by_album[_int(it["album_id"])].append(it)

    by_rg: Dict[str, List[int]] = defaultdict(list)
    no_rg = 0
    for aid, album in album_rows.items():
        rg = _s(album.get("mb_releasegroupid")).lower()
        if rg:
            by_rg[rg].append(aid)
        else:
            no_rg += 1

    groups: List[Dict[str, Any]] = []
    for rg, ids in sorted(by_rg.items()):
        if len(ids) < 2:
            continue
        rows = [{
            "id": aid,
            "album": _s(album_rows[aid].get("album")),
            "albumartist": _s(album_rows[aid].get("albumartist")),
            "mb_albumid": _s(album_rows[aid].get("mb_albumid")),
            "year": album_rows[aid].get("year"),
            "items": sorted(items_by_album.get(aid, []), key=lambda i: (_slot(i), _int(i.get("id")))),
        } for aid in sorted(ids)]
        editions: Dict[str, int] = defaultdict(int)
        for row in rows:
            editions[row["mb_albumid"].lower()] += len(row["items"]) or 1
        majority_release = max(editions, key=lambda k: (editions[k], k))
        keep = _retained_row(rows, majority_release)

        slots: Dict[Tuple[int, int], List[Dict[str, Any]]] = defaultdict(list)
        unpositioned: List[int] = []
        for row in rows:
            for it in row["items"]:
                if not _slot(it)[1]:
                    unpositioned.append(_int(it.get("id")))
                    continue
                slots[_slot(it)].append({"album_id": row["id"], "item_id": _int(it.get("id")),
                                         "mb_trackid": _s(it.get("mb_trackid")), "format": _s(it.get("format")),
                                         "path": _s(it.get("path"))})

        moves, overlaps, complementary = [], [], 0
        for slot, copies in sorted(slots.items()):
            rows_in_slot = {c["album_id"] for c in copies}
            if len(copies) == 1:
                complementary += 1
                if copies[0]["album_id"] != keep["id"]:
                    moves.append({"item_id": copies[0]["item_id"], "from_album_id": copies[0]["album_id"],
                                  "to_album_id": keep["id"], "disc": slot[0], "track": slot[1],
                                  "path": copies[0]["path"]})
                continue
            overlaps.append({"disc": slot[0], "track": slot[1], "album_ids": sorted(rows_in_slot),
                             "copies": copies, **_overlap_verdict(copies, cached_ids, abs_path)})

        blockers: List[str] = []
        distinct_editions = sorted(k for k in editions if k)
        if len(distinct_editions) > 1:
            blockers.append(f"differing editions (Release IDs): {', '.join(distinct_editions)}")
        if "" in editions:
            blockers.append("an album row has no Release ID")
        if overlaps:
            conflict = sum(1 for o in overlaps if o["relation"] in ("recording_ids_differ", "different_recordings_by_acoustid"))
            blockers.append(f"{len(overlaps)} overlapping slot(s)"
                            + (f", {conflict} with conflicting recordings" if conflict else ""))
        if unpositioned:
            blockers.append(f"{len(unpositioned)} item(s) without a track position")
        empty_rows = [r["id"] for r in rows if not r["items"]]

        groups.append({
            "release_group_id": rg,
            "albumartist": keep["albumartist"],
            "album": keep["album"],
            "album_rows": [{k: r[k] for k in ("id", "album", "albumartist", "mb_albumid", "year")}
                           | {"item_count": len(r["items"]),
                              "slots": [list(_slot(i)) for i in r["items"]]} for r in rows],
            "editions": {k or "(none)": v for k, v in editions.items()},
            "retain_album_id": keep["id"],
            "retain_reason": "most tracks, then majority edition, then lowest album id",
            "complementary_slots": complementary,
            "overlapping_slots": overlaps,
            "proposed_moves": moves,
            "empty_album_rows_to_remove_after_merge": [aid for aid in empty_rows if aid != keep["id"]],
            "unpositioned_item_ids": unpositioned,
            "deterministic": not blockers,
            "blockers": blockers,
            "recommendation": ("deterministic merge: move the listed items into the retained row"
                               if not blockers else "review only: resolve the blockers first"),
        })
    return {
        "generated_at": time.time(),
        "album_rows": len(album_rows),
        "album_rows_without_release_group": no_rg,
        "duplicate_groups": len(groups),
        "deterministic_groups": sum(1 for g in groups if g["deterministic"]),
        "groups": groups,
    }
