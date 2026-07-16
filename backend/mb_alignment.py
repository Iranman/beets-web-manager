from typing import Any, Callable, Dict, List


MatchFn = Callable[[Dict[str, Any], List[Dict[str, Any]]], Dict[str, Any]]
ExistsFn = Callable[[Dict[str, Any]], bool]


def _s(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _presence_rank(item: Dict[str, Any], trk: Dict[str, Any],
                   best: Dict[str, Any], score: float) -> tuple:
    item_track = int(item.get("track") or 0)
    item_disc = int(item.get("disc") or 1)
    mb_track = int(trk.get("track") or 0)
    mb_disc = int(trk.get("disc") or 1)
    item_mbid = _s(item.get("mb_trackid") or "").strip().lower()
    mb_trackid = _s(trk.get("mb_trackid") or "").strip().lower()
    position_match = 1 if item_track == mb_track and item_disc == mb_disc else 0
    nonzero_position = 1 if item_track > 0 else 0
    exact_mbid = 1 if item_mbid and item_mbid == mb_trackid else 0
    title_score = float(best.get("title_score") or 0)
    return (
        position_match,
        nonzero_position,
        exact_mbid,
        round(float(score or 0), 6),
        round(title_score, 6),
        -int(item.get("id") or 0),
    )


def _compact_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": int(item.get("id") or 0),
        "disc": int(item.get("disc") or 1),
        "track": int(item.get("track") or 0),
        "title": _s(item.get("title") or ""),
        "path": _s(item.get("path") or ""),
        "filename": _s(item.get("filename") or ""),
        "mb_trackid": _s(item.get("mb_trackid") or "").strip().lower(),
        "length": float(item.get("length") or 0),
    }


def _compact_track(track: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "disc": int(track.get("disc") or 1),
        "track": int(track.get("track") or 0),
        "title": _s(track.get("title") or ""),
        "mb_trackid": _s(track.get("mb_trackid") or "").strip().lower(),
    }


def _duplicate_recording_groups(
    items: List[Dict[str, Any]],
    mb_tracks: List[Dict[str, Any]],
    exists: ExistsFn,
) -> List[Dict[str, Any]]:
    local_by_mbid: Dict[str, List[Dict[str, Any]]] = {}
    expected_by_mbid: Dict[str, List[Dict[str, Any]]] = {}

    for track in mb_tracks:
        mbid = _s(track.get("mb_trackid") or "").strip().lower()
        if mbid:
            expected_by_mbid.setdefault(mbid, []).append(track)

    for item in items:
        mbid = _s(item.get("mb_trackid") or "").strip().lower()
        if not mbid:
            continue
        try:
            if not exists(item):
                continue
        except Exception:
            continue
        local_by_mbid.setdefault(mbid, []).append(item)

    groups: List[Dict[str, Any]] = []
    for mbid, rows in sorted(local_by_mbid.items()):
        expected_rows = expected_by_mbid.get(mbid, [])
        allowed_count = max(1, len(expected_rows))
        duplicate_count = max(0, len(rows) - allowed_count)
        if duplicate_count <= 0:
            continue
        rows_sorted = sorted(
            rows,
            key=lambda row: (
                int(row.get("disc") or 1),
                int(row.get("track") or 0),
                int(row.get("id") or 0),
            ),
        )
        groups.append({
            "mb_trackid": mbid,
            "count": len(rows),
            "expected_count": len(expected_rows),
            "duplicate_count": duplicate_count,
            "items": [_compact_item(row) for row in rows_sorted],
            "expected_tracks": [_compact_track(track) for track in expected_rows],
        })
    return groups


def summarize_mb_track_alignment(
    items: List[Dict[str, Any]],
    mb_tracks: List[Dict[str, Any]],
    *,
    match_fn: MatchFn,
    file_exists_fn: ExistsFn | None = None,
    threshold: float,
    repair_threshold: float,
) -> Dict[str, Any]:
    """Align local items to a selected MB release and classify gaps/extras.

    The selected MusicBrainz release is the source of row order. Duplicate local
    items competing for the same MB track prefer the real numbered position over
    track-zero or duplicate rows.
    """
    exists = file_exists_fn or (lambda _item: True)
    present: Dict[int, Dict[str, Any]] = {}
    matched_item_ids: set[int] = set()

    for item in items:
        best = match_fn(item, mb_tracks)
        idx = int(best.get("idx", -1))
        score = float(best.get("score") or 0)
        title_score = float(best.get("title_score") or 0)
        if idx < 0:
            continue
        if (
            (best.get("exact_mbid") and title_score >= repair_threshold)
            or (not best.get("exact_mbid") and score >= threshold)
        ):
            if not exists(item):
                continue
            rank = _presence_rank(item, mb_tracks[idx], best, score)
            existing = present.get(idx)
            existing_rank = existing.get("_rank") if existing else None
            if not existing or rank > existing_rank:
                if existing:
                    matched_item_ids.discard(int(existing.get("id") or 0))
                present[idx] = {**item, "score": round(score, 3), "_rank": rank}
                matched_item_ids.add(int(item.get("id") or 0))

    expected: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    in_library = 0
    repairable_count = 0
    missing_recording_id_count = 0
    mismatched_recording_id_count = 0

    for idx, trk in enumerate(mb_tracks):
        raw_item = present.get(idx)
        item = ({k: v for k, v in raw_item.items() if k != "_rank"}
                if raw_item else None)
        rec = {
            "disc": int(trk.get("disc") or 1),
            "track": int(trk.get("track") or 0),
            "title": trk.get("title", ""),
            "mb_trackid": trk.get("mb_trackid", ""),
            "duration_ms": int(trk.get("duration_ms") or 0),
            "ok": bool(item),
            "missing": not bool(item),
            "item": item or {},
        }
        if item:
            in_library += 1
            current_mbid = _s(item.get("mb_trackid") or "").strip().lower()
            target_mbid = _s(trk.get("mb_trackid") or "").strip().lower()
            if target_mbid and current_mbid != target_mbid:
                repairable_count += 1
                if current_mbid:
                    mismatched_recording_id_count += 1
                else:
                    missing_recording_id_count += 1
        else:
            missing.append(rec)
        expected.append(rec)

    extra_items = [
        item for item in items
        if int(item.get("id") or 0) not in matched_item_ids
    ]
    duplicate_recording_groups = _duplicate_recording_groups(items, mb_tracks, exists)
    duplicate_recording_count = sum(
        int(group.get("duplicate_count") or 0)
        for group in duplicate_recording_groups
    )

    return {
        "actual_count": len(items),
        "expected_count": len(expected),
        "extra_count": len(extra_items),
        "extra_items": extra_items,
        "in_library": in_library,
        "missing_count": len(missing),
        "missing": missing,
        "percent": int(round((in_library / len(expected)) * 100)) if expected else 0,
        "tracks": expected,
        "mb_repairable_count": repairable_count,
        "mb_trackid_missing_count": missing_recording_id_count,
        "mb_trackid_mismatch_count": mismatched_recording_id_count,
        "mb_duplicate_recording_id_count": duplicate_recording_count,
        "duplicate_recording_groups": duplicate_recording_groups,
    }
