"""Is a folder Beets skipped as ``duplicate`` really already in the library?

Beets' duplicate check is name-only (albumartist + album), so a ``duplicate``
skip alone proves nothing. The webmanager plugin attaches
``duplicate_evidence`` (what the task matched, and the recordings of the
library albums it collided with). The folder counts as already in the
library only when Beets matched it strongly to a MusicBrainz release, every
file is mapped to a track with a Recording ID, and every one of those
recordings is already in a library album of the same Release Group.
Thresholds are owned by music identity; they are fixed, not read from the
user's Beets config. Pure: no I/O. Any missing or malformed field is "no".
"""

from __future__ import annotations

from typing import Any, Mapping, Tuple

from .library_gaps import _has_id

STRONG_DISTANCE = 0.04


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("not a count")
    return value


def duplicate_is_already_in_library(ev: Any) -> Tuple[bool, str]:
    if not isinstance(ev, Mapping):
        return False, "no_duplicate_evidence"
    if ev.get("choice") != "APPLY":
        return False, "not_a_beets_match"
    if ev.get("data_source") != "MusicBrainz":
        return False, "non_musicbrainz_match"
    distance = ev.get("distance")
    if isinstance(distance, bool) or not isinstance(distance, (int, float)) or not 0 <= distance <= STRONG_DISTANCE:
        return False, "match_not_strong"
    rg = _lower(ev.get("release_group_id"))
    if not _has_id(rg):
        return False, "match_has_no_release_group"
    try:
        extra_items, extra_tracks = _count(ev.get("extra_items")), _count(ev.get("extra_tracks"))
        mapped, items = _count(ev.get("mapped_count")), _count(ev.get("task_item_count"))
    except ValueError:
        return False, "torrent_not_fully_mapped"
    if extra_items or extra_tracks or mapped != items or not mapped:
        return False, "torrent_not_fully_mapped"
    raw = ev.get("recording_ids")
    recs = {_lower(r) for r in raw} if isinstance(raw, list) else set()
    if len(recs) != mapped or not all(_has_id(r) for r in recs):
        return False, "recording_ids_incomplete"
    dups = ev.get("duplicates") if isinstance(ev.get("duplicates"), list) else []
    same_rg = [d for d in dups if isinstance(d, Mapping) and _lower(d.get("mb_releasegroupid")) == rg]
    if not same_rg:
        return False, "name_collision_other_release_group"
    lib_recs = {_lower(r) for d in same_rg if isinstance(d.get("recording_ids"), list)
                for r in d["recording_ids"] if _has_id(_lower(r))}
    if recs <= lib_recs:
        return True, "recordings_present_in_release_group"
    return False, "torrent_has_recordings_not_in_library"
