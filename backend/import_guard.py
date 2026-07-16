"""Small import-safety decisions shared by the Beets web import flow."""

from __future__ import annotations

import re

from typing import Callable, Dict, Iterable, List, Optional


def existing_track_matches_target(
    *,
    fingerprint_status: str = "",
    exact_mbid: bool = False,
    title_score: float = 0.0,
    repair_threshold: float = 0.72,
    duplicate_threshold: float = 0.90,
) -> bool:
    """Return true only when an existing row really satisfies an MB track.

    This intentionally uses title-only score from the caller. Folder/file-path
    variants can include the album title and must not make a wrong existing row
    look like a duplicate of a newly downloaded correct track.
    """
    status = (fingerprint_status or "").strip().lower()
    if status == "mismatch":
        return False
    if status == "match":
        return True
    if exact_mbid:
        return float(title_score or 0.0) >= float(repair_threshold)
    return float(title_score or 0.0) >= float(duplicate_threshold)


def existing_track_can_block_downloaded_replacement(
    *,
    file_exists: bool,
    fingerprint_status: str = "",
    exact_mbid: bool = False,
    title_score: float = 0.0,
    repair_threshold: float = 0.72,
    duplicate_threshold: float = 0.90,
) -> bool:
    """Return true when an existing row should make a new downloaded row a duplicate."""
    if not file_exists:
        return False
    return existing_track_matches_target(
        fingerprint_status=fingerprint_status,
        exact_mbid=exact_mbid,
        title_score=title_score,
        repair_threshold=repair_threshold,
        duplicate_threshold=duplicate_threshold,
    )


def missing_wanted_tracks_block_retag(missing_tracks: Iterable[object]) -> bool:
    """Existing-album repair must not retag until requested tracks are present."""
    return any(True for _ in (missing_tracks or []))


def _s(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _default_title_norm(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", _s(value).casefold()).split())


def release_track_matches_missing_target(
    release_track: Dict[str, object],
    missing_tracks: Iterable[Dict[str, object]],
    *,
    title_norm_fn: Optional[Callable[[str], str]] = None,
    release_title_counts: Optional[Dict[str, int]] = None,
) -> bool:
    """Return true only when a release track is one of the current missing targets."""
    norm = title_norm_fn or _default_title_norm
    missing = list(missing_tracks or [])
    missing_pairs = {
        (int(t.get("disc") or 1), int(t.get("track") or 0))
        for t in missing if int(t.get("track") or 0)
    }
    missing_ids = {
        _s(t.get("mb_trackid", "")).strip().lower()
        for t in missing if t.get("mb_trackid")
    }
    missing_titles = {norm(_s(t.get("title", ""))) for t in missing if t.get("title")}

    pair = (int(release_track.get("disc") or 1), int(release_track.get("track") or 0))
    mbid = _s(release_track.get("mb_trackid", "")).strip().lower()
    title = norm(_s(release_track.get("title", "")))
    if pair[1] and pair in missing_pairs:
        return True
    if mbid and mbid in missing_ids:
        return True
    return bool(
        title
        and title in missing_titles
        and int((release_title_counts or {}).get(title, 0)) == 1
    )


def filter_wanted_tracks_against_missing(
    wanted_tracks: Iterable[Dict[str, object]],
    missing_tracks: Iterable[Dict[str, object]],
    *,
    title_norm_fn: Optional[Callable[[str], str]] = None,
) -> List[Dict[str, object]]:
    """Keep requested tracks that are still missing without title-only wrong-disc drift."""
    norm = title_norm_fn or _default_title_norm
    missing = list(missing_tracks or [])
    missing_pairs = {
        (int(t.get("disc") or 1), int(t.get("track") or 0))
        for t in missing if int(t.get("track") or 0)
    }
    missing_ids = {
        _s(t.get("mb_trackid", "")).strip().lower()
        for t in missing if t.get("mb_trackid")
    }
    missing_by_title: Dict[str, List[Dict[str, object]]] = {}
    for missing_track in missing:
        title = norm(_s(missing_track.get("title", "")))
        if title:
            missing_by_title.setdefault(title, []).append(missing_track)

    filtered: List[Dict[str, object]] = []
    for trk in wanted_tracks or []:
        pair = (int(trk.get("disc") or 1), int(trk.get("track") or 0))
        mbid = _s(trk.get("mb_trackid", "")).strip().lower()
        title = norm(_s(trk.get("title", "")))
        if pair[1] and pair in missing_pairs:
            filtered.append(trk)
            continue
        if mbid and mbid in missing_ids:
            filtered.append(trk)
            continue
        if not pair[1] and not mbid and title:
            title_matches = missing_by_title.get(title) or []
            if len(title_matches) == 1:
                filtered.append(title_matches[0])
    return filtered
