from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List

try:
    from matching import align_tracks_global
except ImportError:
    from backend.matching import align_tracks_global


SimilarityFn = Callable[[str, str], float]
AcoustidLookupFn = Callable[[str], List[Dict[str, Any]]]


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def _local_title(file_path: str) -> str:
    return Path(_s(file_path)).stem


def _track_num(track: Dict[str, Any]) -> int:
    try:
        return int(track.get("track") or track.get("num") or 0)
    except Exception:
        return 0


def _mb_title(track: Dict[str, Any]) -> str:
    return _s(track.get("title") or track.get("mb_title") or "")


def _mb_norm(track: Dict[str, Any]) -> str:
    return _s(track.get("title_norm") or _mb_title(track).casefold())


def align_tracks(local_files: List[str], mb_tracks: List[Dict[str, Any]], similarity_fn: SimilarityFn) -> List[Dict[str, Any]]:
    """Align local files to MusicBrainz tracks by global one-to-one evidence.

    This intentionally avoids positional matching so a missing first track does
    not shift every subsequent local file onto the wrong MusicBrainz row. The
    public row shape is retained for confirmed_import_v1 callers.
    """
    local_tracks = [
        {
            "path": _s(file_path),
            "title": _local_title(file_path),
            "track": idx + 1,
        }
        for idx, file_path in enumerate(local_files or [])
    ]
    canonical = align_tracks_global(
        local_tracks,
        list(mb_tracks or []),
        threshold=0.72,
        trust_model="fresh_reviewed_import",
    )
    by_target_index = {
        row.target_index: row
        for row in canonical.assignments
        if row.status == "matched"
    }
    used_files: set[int] = set()
    rows: List[Dict[str, Any]] = []
    for target_index, track in enumerate(mb_tracks or []):
        assignment = by_target_index.get(target_index)
        if assignment:
            used_files.add(assignment.local_index)
            file_path = _s(assignment.local_track.get("path") or "")
            local_title = _local_title(file_path)
            status = "matched" if assignment.title_similarity >= 0.96 else "fuzzy"
            best_score = assignment.title_similarity
        else:
            status = "missing"
            file_path = ""
            local_title = ""
            best_score = 0.0
        rows.append({
            "num": _track_num(track),
            "mb_title": _mb_title(track),
            "mb_trackid": _s(track.get("mb_trackid") or track.get("recording_id") or ""),
            "duration_ms": int(track.get("duration_ms") or 0),
            "local_title": local_title,
            "file_path": file_path,
            "status": status,
            "sim_score": round(best_score, 3),
        })
    for idx, file_path in enumerate(local_files or []):
        if idx in used_files:
            continue
        rows.append({
            "num": 0,
            "mb_title": "",
            "mb_trackid": "",
            "local_title": _local_title(file_path),
            "file_path": _s(file_path),
            "status": "extra",
            "sim_score": 0.0,
        })
    return rows

def resolve_unmatched_via_acoustid(comparison: List[Dict[str, Any]], acoustid_lookup_fn: AcoustidLookupFn,
                                   *, fpcalc_available: bool = True, min_score: int = 80) -> None:
    """Promote missing rows when AcoustID verifies an extra file's recording ID."""
    if not fpcalc_available:
        return
    missing_by_mbid = {
        _s(row.get("mb_trackid")).strip().lower(): row
        for row in comparison
        if row.get("status") == "missing" and _s(row.get("mb_trackid")).strip()
    }
    if not missing_by_mbid:
        return
    extras = [row for row in list(comparison) if row.get("status") == "extra" and row.get("file_path")]
    resolved_extras: List[Dict[str, Any]] = []
    for extra in extras:
        try:
            hits = acoustid_lookup_fn(_s(extra.get("file_path"))) or []
        except Exception:
            hits = []
        for hit in hits:
            mbid = _s(hit.get("mb_trackid") or hit.get("recording_id") or "").strip().lower()
            try:
                score = int(hit.get("score") or 0)
            except Exception:
                score = 0
            target = missing_by_mbid.get(mbid)
            if not target or score < min_score:
                continue
            target["status"] = "acoustid_verified"
            target["file_path"] = _s(extra.get("file_path"))
            target["local_title"] = _s(extra.get("local_title"))
            target["sim_score"] = max(float(target.get("sim_score") or 0.0), score / 100.0)
            resolved_extras.append(extra)
            missing_by_mbid.pop(mbid, None)
            break
    for extra in resolved_extras:
        try:
            comparison.remove(extra)
        except ValueError:
            pass