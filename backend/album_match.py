from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List


MatchFn = Callable[[Dict[str, Any], List[Dict[str, Any]]], Dict[str, Any]]
FileExistsFn = Callable[[str], bool]


def build_album_match_plan(
    *,
    album_id: int,
    mb_albumid: str,
    release_title: str,
    items: Iterable[Dict[str, Any]],
    mb_tracks: List[Dict[str, Any]],
    match_fn: MatchFn,
    file_exists_fn: FileExistsFn,
    threshold: float,
) -> Dict[str, Any]:
    """Plan which local album items belong to a selected MusicBrainz release.

    The caller supplies the scoring function so this helper stays framework-free
    and testable while app.py can keep its existing fuzzy matcher.
    """
    item_list = [dict(item) for item in items]
    present: Dict[int, Dict[str, Any]] = {}
    matched_item_ids: set[int] = set()

    for item in item_list:
        best = match_fn(item, mb_tracks) or {}
        idx = int(best.get("idx", -1))
        score = float(best.get("score") or 0)
        if idx < 0:
            continue
        if not (best.get("exact_mbid") or score >= threshold):
            continue

        path = str(item.get("abs_path") or item.get("path") or "")
        if path and not file_exists_fn(path):
            continue

        item_id = int(item.get("id") or 0)
        existing = present.get(idx)
        if existing and score <= float(existing.get("score") or 0):
            continue
        if existing:
            matched_item_ids.discard(int(existing.get("id") or 0))
        present[idx] = {**item, "score": round(score, 3)}
        matched_item_ids.add(item_id)

    matched_items = [
        item for item in item_list if int(item.get("id") or 0) in matched_item_ids
    ]
    unmatched_items = [
        item for item in item_list if int(item.get("id") or 0) not in matched_item_ids
    ]
    for item in item_list:
        if not item.get("filename"):
            item["filename"] = Path(str(item.get("path") or "")).name

    return {
        "ok": True,
        "album_id": int(album_id),
        "mb_albumid": str(mb_albumid or "").strip().lower(),
        "release_title": release_title,
        "expected_count": len(mb_tracks),
        "actual_count": len(item_list),
        "matched_count": len(matched_items),
        "unmatched_count": len(unmatched_items),
        "matched_items": matched_items,
        "unmatched_items": unmatched_items,
    }
