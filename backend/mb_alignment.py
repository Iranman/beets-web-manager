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


_TITLE_BRACKET_OPEN = "([{"
_TITLE_BRACKET_CLOSE = ")]}"


def _strip_bracketed_spans(text: str) -> str:
    """Linear-time, byte-identical replacement for
    re.sub(r"[\\(\\[\\{].*?[\\)\\]\\}]", "", text).

    Mirrors backend.transaction_engine._strip_bracketed_spans (kept
    duplicated for the same reason album_track_norm itself is duplicated
    there -- neither module may import the other). The regex form is
    quadratic on titles carrying many unmatched opening brackets, since
    each one restarts a lazy `.*?` scan; this pass is O(len(text)) and
    produces identical output.
    """
    n = len(text)
    if n == 0:
        return text
    next_close = [-1] * (n + 1)
    for j in range(n - 1, -1, -1):
        ch = text[j]
        if ch in _TITLE_BRACKET_CLOSE:
            next_close[j] = j
        elif ch == "\n":
            next_close[j] = -1
        else:
            next_close[j] = next_close[j + 1]
    out: List[str] = []
    i = 0
    while i < n:
        if text[i] in _TITLE_BRACKET_OPEN:
            close_at = next_close[i + 1]
            if close_at != -1:
                i = close_at + 1
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def album_track_norm(text: Any) -> str:
    import re
    t = _s(text).lower()
    t = _strip_bracketed_spans(t)
    t = re.sub(r"[^\w\s]", "", t)
    return " ".join(t.split())


def album_track_title_variants(title: str, path: str = "") -> List[str]:
    import re
    from pathlib import Path
    variants = [album_track_norm(title)]
    if path:
        stem = Path(_s(path)).stem
        variants.append(album_track_norm(stem))
        cleaned_stem = re.sub(r"^\d+[\s\._\-]+", "", stem)
        variants.append(album_track_norm(cleaned_stem))
    return [v for v in variants if v]


def album_item_position_hints(item: Dict[str, Any]) -> tuple:
    import re
    from pathlib import Path
    disc = int(item.get("disc") or 1)
    track = int(item.get("track") or 0)
    if track > 0:
        return disc, track
    path = _s(item.get("path") or "")
    filename = Path(path).name if path else ""
    m = re.search(r"(\d+)[_\-\s.]+(\d+)", filename)
    if m:
        try:
            return int(m.group(1)), int(m.group(2))
        except Exception:
            pass
    m = re.search(r"(?:track|trk|#)?\s*(\d+)", filename, re.IGNORECASE)
    if m:
        try:
            return disc, int(m.group(1))
        except Exception:
            pass
    return disc, 0


def album_track_score(item: Dict[str, Any], mb_trk: Dict[str, Any]) -> float:
    from difflib import SequenceMatcher
    mb_norm = mb_trk.get("title_norm") or album_track_norm(mb_trk.get("title", ""))
    variants = album_track_title_variants(item.get("title", ""), item.get("path", ""))
    title_score = max(
        (SequenceMatcher(None, v, mb_norm).ratio() for v in variants if v and mb_norm),
        default=0.0,
    )
    pos_bonus = 0.0
    item_disc, item_track = album_item_position_hints(item)
    if item_track == int(mb_trk.get("track") or 0):
        pos_bonus += 0.04
        if item_disc == int(mb_trk.get("disc") or 1):
            pos_bonus += 0.02
    dur_bonus = 0.0
    item_ms = int(float(item.get("length") or 0) * 1000)
    mb_ms = int(mb_trk.get("duration_ms") or 0)
    if item_ms and mb_ms:
        diff_s = abs(item_ms - mb_ms) / 1000.0
        dur_bonus = 0.04 if diff_s <= 4 else (0.02 if diff_s <= 10 else 0.0)
    return min(1.0, title_score + pos_bonus + dur_bonus)


def best_album_track_match(item: Dict[str, Any], mb_tracks: List[Dict[str, Any]]) -> Dict[str, Any]:
    item_mbid = _s(item.get("mb_trackid", "")).strip().lower()
    if item_mbid:
        for idx, trk in enumerate(mb_tracks):
            if item_mbid and item_mbid == _s(trk.get("mb_trackid", "")).strip().lower():
                title_score = album_track_score(item, trk)
                return {
                    "idx": idx,
                    "track": trk,
                    "score": max(0.98, title_score),
                    "title_score": title_score,
                    "exact_mbid": True,
                }
    best_idx = -1
    best_score = -1.0
    best_rank = (-1.0, -1, -1)
    item_disc, item_track = album_item_position_hints(item)
    for idx, trk in enumerate(mb_tracks):
        score = album_track_score(item, trk)
        exact_pos = int(
            bool(item_track and item_track == int(trk.get("track") or 0))
            and bool(item_disc == int(trk.get("disc") or 1))
        )
        track_pos = int(bool(item_track and item_track == int(trk.get("track") or 0)))
        rank = (score, exact_pos, track_pos)
        if rank > best_rank:
            best_rank = rank
            best_score = score
            best_idx = idx
    return {
        "idx": best_idx,
        "track": mb_tracks[best_idx] if best_idx >= 0 else {},
        "score": max(best_score, 0.0),
        "title_score": max(best_score, 0.0),
        "exact_mbid": False,
    }


def summarize_mb_track_alignment(
    items: List[Dict[str, Any]],
    mb_tracks: List[Dict[str, Any]],
    *,
    match_fn: MatchFn = best_album_track_match,
    file_exists_fn: ExistsFn | None = None,
    threshold: float = 0.72,
    repair_threshold: float = 0.65,
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


ScoreFn = Callable[[Dict[str, Any], Dict[str, Any]], float]


def greedy_album_track_alignment(
    items: List[Dict[str, Any]],
    mb_tracks: List[Dict[str, Any]],
    *,
    score_fn: ScoreFn = album_track_score,
    file_exists_fn: "ExistsFn | None" = None,
    threshold: float = 0.72,
) -> Dict[str, Any]:
    """Greedy, item-order-driven track alignment.

    SEC-002 / ARCH-003 Wave 33 continuation: this is app.py's own
    _match_tracks_from_mb_shared() matching loop, ported here VERBATIM (not
    approximated) rather than reusing summarize_mb_track_alignment's
    different rank-based-displacement conflict resolution, so the engine
    and app.py can never again risk silently disagreeing on which specific
    track a file gets permanently relabeled as in an ambiguous case
    (duplicate/near-duplicate titles, multiple candidate files competing
    for one track). album_track_score() (the per-pair scoring function)
    was already shared/identical between the two call sites before this
    change -- only the overall alignment/conflict-resolution shape
    differed, and that is what this replaces.

    For each local item, in the exact order the caller supplies `items`
    (app.py's caller sorts its DB read `ORDER BY disc, track, title, id`;
    callers here must supply that same order to be behaviorally
    identical), claim the single highest-scoring MB track not already
    claimed by an earlier item in this same call. Once a track is
    claimed, no later item can ever take it away, even if the later item
    would have scored higher against it -- this is deliberately NOT
    summarize_mb_track_alignment's rank-based displacement. A tie
    (`score` exactly equal between two mb_tracks against the same item)
    keeps the FIRST mb_track encountered in `mb_tracks` order, matching
    app.py's own `if score > best_score` (strict greater-than) loop
    exactly.

    Deliberately does not special-case an item's own existing
    mb_trackid (no "exact_mbid" score boost) -- app.py's own loop never
    did either; `score_fn` (album_track_score) only ever looks at title/
    position/duration. An item's pre-existing recording ID is still
    honored downstream, by the (unchanged, and deliberately NOT ported --
    a real engine safety improvement over app.py's older code, not a
    behavioral approximation of it) caller-side check that keeps a
    conflicting non-blank existing recording ID out of automatic repair
    and routes it to manual review instead.
    """
    exists = file_exists_fn or (lambda _item: True)
    used_indices: set[int] = set()
    matched_by_idx: Dict[int, Dict[str, Any]] = {}
    matched_item_ids: set[int] = set()
    extra_items: List[Dict[str, Any]] = []

    for item in items:
        if not exists(item):
            extra_items.append(item)
            continue

        best_idx = -1
        best_score = -1.0
        for idx, mb_trk in enumerate(mb_tracks):
            if idx in used_indices:
                continue
            score = score_fn(item, mb_trk)
            if score > best_score:
                best_score = score
                best_idx = idx

        if best_idx >= 0 and best_score >= threshold:
            used_indices.add(best_idx)
            matched_by_idx[best_idx] = {**item, "score": round(best_score, 3)}
            matched_item_ids.add(int(item.get("id") or 0))
        else:
            extra_items.append(item)

    expected: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    in_library = 0
    repairable_count = 0
    missing_recording_id_count = 0
    mismatched_recording_id_count = 0

    for idx, trk in enumerate(mb_tracks):
        matched_item = matched_by_idx.get(idx)
        rec = {
            "disc": int(trk.get("disc") or 1),
            "track": int(trk.get("track") or 0),
            "title": trk.get("title", ""),
            "mb_trackid": trk.get("mb_trackid", ""),
            "duration_ms": int(trk.get("duration_ms") or 0),
            "ok": bool(matched_item),
            "missing": not bool(matched_item),
            "item": matched_item or {},
        }
        if matched_item:
            in_library += 1
            current_mbid = _s(matched_item.get("mb_trackid") or "").strip().lower()
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

