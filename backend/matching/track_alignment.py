from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from .models import AcoustIDStatus, TrackAlignmentResult, TrackAssignment, UnmatchedLocalTrack
from .normalize import normalize_artist, normalize_title, similarity, title_variants


def _s(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _recording_id(row: Dict[str, Any]) -> str:
    return _s(row.get("recording_id") or row.get("mb_recording_id") or row.get("mb_trackid") or "").strip().lower()


def _track_id(row: Dict[str, Any], index: int) -> str:
    return _s(
        row.get("track_id")
        or row.get("mb_track_id")
        or row.get("mb_releasetrackid")
        or _recording_id(row)
        or f"{_int(row.get('disc'), 1)}:{_int(row.get('track'), index + 1)}"
    ).strip().lower()


def _duration_seconds(row: Dict[str, Any]) -> Optional[float]:
    for key in ("duration_seconds", "length", "duration"):
        if row.get(key) not in (None, ""):
            return _float(row.get(key), 0.0)
    if row.get("duration_ms") not in (None, ""):
        return _float(row.get("duration_ms"), 0.0) / 1000.0
    return None


def _duration_score(local: Dict[str, Any], target: Dict[str, Any]) -> Tuple[float, Optional[float]]:
    left = _duration_seconds(local)
    right = _duration_seconds(target)
    if not left or not right:
        return 0.5, None
    delta = abs(left - right)
    if delta <= 2:
        return 1.0, delta
    if delta <= 6:
        return 0.9, delta
    if delta <= 12:
        return 0.72, delta
    if delta <= 30:
        return 0.45, delta
    return 0.0, delta


def _position_score(local: Dict[str, Any], target: Dict[str, Any]) -> Tuple[float, bool]:
    local_disc = _int(local.get("disc"), 1)
    target_disc = _int(target.get("disc"), 1)
    local_track = _int(local.get("track"), 0)
    target_track = _int(target.get("track"), 0)
    if local_disc == target_disc and local_track and target_track and local_track == target_track:
        return 1.0, True
    if local_track and target_track and local_track == target_track:
        return 0.82, False
    return 0.0, False


def _title_similarity(local: Dict[str, Any], target: Dict[str, Any]) -> float:
    target_title = target.get("title") or target.get("mb_title") or ""
    target_norm = normalize_title(target_title)
    if not target_norm:
        return 0.0
    scores: List[float] = []
    for variant in title_variants(local.get("title") or "", local.get("path") or local.get("filename") or ""):
        scores.append(SequenceMatcher(None, variant, target_norm).ratio())
        if variant == target_norm:
            scores.append(1.0)
        local_tokens = set(variant.split())
        target_tokens = set(target_norm.split())
        if local_tokens and target_tokens:
            overlap = len(local_tokens & target_tokens) / max(len(local_tokens), len(target_tokens))
            if overlap >= 0.75:
                scores.append(0.88)
            elif overlap >= 0.5:
                scores.append(0.72)
    return max(scores) if scores else 0.0


def _artist_similarity(local: Dict[str, Any], target: Dict[str, Any]) -> float:
    local_artist = local.get("artist") or local.get("albumartist") or ""
    target_artist = target.get("artist") or target.get("artist_credit") or ""
    if not local_artist or not target_artist:
        return 0.5
    return similarity(normalize_artist(local_artist), normalize_artist(target_artist))


def _acoustid_hits(local: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    if "acoustid_hits" not in local and "acoustid_candidates" not in local:
        return None
    hits = local.get("acoustid_hits")
    if hits is None:
        hits = local.get("acoustid_candidates")
    if hits is None:
        return None
    if isinstance(hits, list):
        return [hit for hit in hits if isinstance(hit, dict)]
    return []


def _hit_recording_id(hit: Dict[str, Any]) -> str:
    return _s(hit.get("recording_id") or hit.get("mb_recording_id") or hit.get("mb_trackid") or "").strip().lower()


def _hit_score(hit: Dict[str, Any]) -> float:
    score = _float(hit.get("score") or hit.get("confidence") or 0.0)
    if score <= 1.0:
        score *= 100.0
    return score


def _acoustid_status_for_target(local: Dict[str, Any], target_recording_id: str) -> Tuple[AcoustIDStatus, List[str], List[str]]:
    positives: List[str] = []
    conflicts: List[str] = []
    hits = _acoustid_hits(local)
    if hits is None:
        return AcoustIDStatus.UNAVAILABLE, positives, conflicts
    if not hits:
        return AcoustIDStatus.NO_RESULT, positives, conflicts
    high_hits = [
        (_hit_recording_id(hit), _hit_score(hit), hit)
        for hit in hits
        if _hit_recording_id(hit) and _hit_score(hit) >= 80.0
    ]
    if not high_hits:
        return AcoustIDStatus.AMBIGUOUS, positives, conflicts
    high_hits.sort(key=lambda row: (-row[1], row[0]))
    target_hits = [row for row in high_hits if row[0] == target_recording_id]
    if target_hits:
        target_score = target_hits[0][1]
        close_conflicts = [row for row in high_hits if row[0] != target_recording_id and row[1] >= target_score - 3.0]
        if close_conflicts:
            conflicts.append("acoustid_ambiguous")
            return AcoustIDStatus.AMBIGUOUS, positives, conflicts
        positives.append("acoustid_recording_confirmed")
        return AcoustIDStatus.CONFIRMED, positives, conflicts
    conflicts.append("acoustid_conflict")
    return AcoustIDStatus.CONFLICT, positives, conflicts


@dataclass
class _CandidateEdge:
    local_index: int
    target_index: int
    score: float
    title_similarity: float
    artist_similarity: float
    duration_delta: Optional[float]
    position_match: bool
    acoustid_status: AcoustIDStatus
    positives: List[str]
    warnings: List[str]
    conflicts: List[str]
    hard_positive: bool = False


def _edge_for(local_index: int, target_index: int, local: Dict[str, Any], target: Dict[str, Any], *, trust_model: str) -> _CandidateEdge:
    target_rec = _recording_id(target)
    local_rec = _recording_id(local)
    title_score = _title_similarity(local, target)
    artist_score = _artist_similarity(local, target)
    duration_score, duration_delta = _duration_score(local, target)
    pos_score, position_match = _position_score(local, target)
    acoustid_status, positives, conflicts = _acoustid_status_for_target(local, target_rec)
    warnings: List[str] = []
    hard_positive = False

    if local_rec and target_rec:
        if local_rec == target_rec:
            positives.append("embedded_recording_id_matches")
            hard_positive = True
        elif trust_model == "existing_library":
            conflicts.append("recording_id_conflict")

    if acoustid_status == AcoustIDStatus.CONFIRMED:
        hard_positive = True
    elif acoustid_status == AcoustIDStatus.CONFLICT:
        conflicts.append("acoustid_conflict")
    elif acoustid_status == AcoustIDStatus.AMBIGUOUS:
        warnings.append("acoustid_ambiguous")

    if title_score < 0.72 and hard_positive:
        warnings.append("title_differs")

    score = (
        title_score * 0.72
        + artist_score * 0.06
        + pos_score * 0.14
        + duration_score * 0.08
    )
    if hard_positive:
        score = max(score, 1.12)
    if conflicts:
        score = min(score, 0.05)
    return _CandidateEdge(
        local_index=local_index,
        target_index=target_index,
        score=max(0.0, min(1.25, score)),
        title_similarity=title_score,
        artist_similarity=artist_score,
        duration_delta=duration_delta,
        position_match=position_match,
        acoustid_status=acoustid_status,
        positives=positives,
        warnings=warnings,
        conflicts=conflicts,
        hard_positive=hard_positive,
    )


def _best_text_targets(local_tracks: List[Dict[str, Any]], mb_tracks: List[Dict[str, Any]]) -> Dict[int, Tuple[int, float]]:
    result: Dict[int, Tuple[int, float]] = {}
    for local_index, local in enumerate(local_tracks):
        best_index = -1
        best_score = 0.0
        for target_index, target in enumerate(mb_tracks):
            title_score = _title_similarity(local, target)
            pos_score, _ = _position_score(local, target)
            score = title_score * 0.82 + pos_score * 0.18
            if score > best_score:
                best_index = target_index
                best_score = score
        result[local_index] = (best_index, best_score)
    return result


def _acoustid_confirmed_target(local: Dict[str, Any], recording_to_target: Dict[str, int]) -> Optional[int]:
    hits = _acoustid_hits(local)
    if hits is None or not hits:
        return None
    high = [
        (_hit_recording_id(hit), _hit_score(hit))
        for hit in hits
        if _hit_recording_id(hit) in recording_to_target and _hit_score(hit) >= 80.0
    ]
    if not high:
        return None
    high.sort(key=lambda row: (-row[1], row[0]))
    top_rec, top_score = high[0]
    close = [row for row in high if row[0] != top_rec and row[1] >= top_score - 3.0]
    if close:
        return None
    return recording_to_target[top_rec]


def _max_weight_pairs(edges: List[_CandidateEdge], local_count: int, target_count: int) -> List[_CandidateEdge]:
    if not edges:
        return []
    source = 0
    local_offset = 1
    target_offset = local_offset + local_count
    sink = target_offset + target_count
    node_count = sink + 1
    graph: List[List[Dict[str, Any]]] = [[] for _ in range(node_count)]

    def add_edge(u: int, v: int, cap: int, cost: int, ref: Optional[_CandidateEdge] = None) -> None:
        graph[u].append({"v": v, "rev": len(graph[v]), "cap": cap, "cost": cost, "ref": ref})
        graph[v].append({"v": u, "rev": len(graph[u]) - 1, "cap": 0, "cost": -cost, "ref": None})

    for index in range(local_count):
        add_edge(source, local_offset + index, 1, 0)
    for index in range(target_count):
        add_edge(target_offset + index, sink, 1, 0)
    for edge in sorted(edges, key=lambda e: (-e.score, e.local_index, e.target_index)):
        cost = -int(round(edge.score * 100000))
        add_edge(local_offset + edge.local_index, target_offset + edge.target_index, 1, cost, edge)

    selected: List[_CandidateEdge] = []
    while True:
        dist = [10**18] * node_count
        parent: List[Optional[Tuple[int, int]]] = [None] * node_count
        in_queue = [False] * node_count
        dist[source] = 0
        queue: deque[int] = deque([source])
        in_queue[source] = True
        while queue:
            u = queue.popleft()
            in_queue[u] = False
            for edge_index, edge in enumerate(graph[u]):
                if edge["cap"] <= 0:
                    continue
                v = edge["v"]
                nd = dist[u] + edge["cost"]
                if nd < dist[v]:
                    dist[v] = nd
                    parent[v] = (u, edge_index)
                    if not in_queue[v]:
                        queue.append(v)
                        in_queue[v] = True
        if parent[sink] is None or dist[sink] >= 0:
            break
        v = sink
        path_refs: List[_CandidateEdge] = []
        while v != source:
            u, edge_index = parent[v]  # type: ignore[misc]
            edge = graph[u][edge_index]
            edge["cap"] -= 1
            graph[v][edge["rev"]]["cap"] += 1
            if edge.get("ref") is not None:
                path_refs.append(edge["ref"])
            v = u
        for ref in path_refs:
            if ref in selected:
                selected.remove(ref)
            else:
                selected.append(ref)
    return sorted(selected, key=lambda e: (e.target_index, e.local_index))


def align_tracks_global(
    local_tracks: List[Dict[str, Any]],
    mb_tracks: List[Dict[str, Any]],
    *,
    threshold: float = 0.72,
    trust_model: str = "existing_library",
) -> TrackAlignmentResult:
    local_tracks = list(local_tracks or [])
    mb_tracks = list(mb_tracks or [])
    text_best = _best_text_targets(local_tracks, mb_tracks)
    recording_to_target = {
        _recording_id(track): index
        for index, track in enumerate(mb_tracks)
        if _recording_id(track)
    }
    conflict_assignments: List[TrackAssignment] = []
    blocked_locals: set[int] = set()
    global_conflicts: List[str] = []

    for local_index, local in enumerate(local_tracks):
        best_index, best_score = text_best.get(local_index, (-1, 0.0))
        if trust_model == "existing_library" and best_index >= 0 and best_score >= threshold:
            local_rec = _recording_id(local)
            target_rec = _recording_id(mb_tracks[best_index])
            if local_rec and target_rec and local_rec != target_rec:
                edge = _edge_for(local_index, best_index, local, mb_tracks[best_index], trust_model=trust_model)
                conflicts = sorted(set(edge.conflicts + ["recording_id_conflict"]))
                assignment = TrackAssignment(
                    local_index=local_index,
                    target_index=best_index,
                    local_track=local,
                    target_track=mb_tracks[best_index],
                    score=edge.score,
                    title_similarity=edge.title_similarity,
                    artist_similarity=edge.artist_similarity,
                    duration_delta=edge.duration_delta,
                    position_match=edge.position_match,
                    status="conflict",
                    acoustid_status=edge.acoustid_status,
                    positives=edge.positives,
                    warnings=edge.warnings,
                    conflicts=conflicts,
                )
                conflict_assignments.append(assignment)
                blocked_locals.add(local_index)
                global_conflicts.extend(conflicts)
                continue
        acoustid_target = _acoustid_confirmed_target(local, recording_to_target)
        if acoustid_target is not None and best_index >= 0 and acoustid_target != best_index and best_score >= 0.88:
            edge = _edge_for(local_index, best_index, local, mb_tracks[best_index], trust_model=trust_model)
            conflicts = sorted(set(edge.conflicts + ["acoustid_conflict"]))
            assignment = TrackAssignment(
                local_index=local_index,
                target_index=best_index,
                local_track=local,
                target_track=mb_tracks[best_index],
                score=edge.score,
                title_similarity=edge.title_similarity,
                artist_similarity=edge.artist_similarity,
                duration_delta=edge.duration_delta,
                position_match=edge.position_match,
                status="conflict",
                acoustid_status=AcoustIDStatus.CONFLICT,
                positives=edge.positives,
                warnings=edge.warnings,
                conflicts=conflicts,
            )
            conflict_assignments.append(assignment)
            blocked_locals.add(local_index)
            global_conflicts.extend(conflicts)

    edges: List[_CandidateEdge] = []
    for local_index, local in enumerate(local_tracks):
        if local_index in blocked_locals:
            continue
        for target_index, target in enumerate(mb_tracks):
            edge = _edge_for(local_index, target_index, local, target, trust_model=trust_model)
            if edge.conflicts:
                if edge.title_similarity >= 0.90 or edge.hard_positive:
                    global_conflicts.extend(edge.conflicts)
                continue
            if edge.hard_positive or edge.score >= threshold:
                edges.append(edge)

    selected_edges = _max_weight_pairs(edges, len(local_tracks), len(mb_tracks))
    selected_locals = {edge.local_index for edge in selected_edges} | blocked_locals
    selected_targets = {edge.target_index for edge in selected_edges} | {row.target_index for row in conflict_assignments}

    assignments: List[TrackAssignment] = list(conflict_assignments)
    for edge in selected_edges:
        status = "matched"
        assignments.append(
            TrackAssignment(
                local_index=edge.local_index,
                target_index=edge.target_index,
                local_track=local_tracks[edge.local_index],
                target_track=mb_tracks[edge.target_index],
                score=edge.score,
                title_similarity=edge.title_similarity,
                artist_similarity=edge.artist_similarity,
                duration_delta=edge.duration_delta,
                position_match=edge.position_match,
                status=status,
                acoustid_status=edge.acoustid_status,
                positives=edge.positives,
                warnings=edge.warnings,
                conflicts=edge.conflicts,
            )
        )
    assignments.sort(key=lambda row: (row.target_index, row.local_index))

    unmatched: List[UnmatchedLocalTrack] = []
    for local_index, local in enumerate(local_tracks):
        if local_index in selected_locals:
            continue
        best_index, best_score = text_best.get(local_index, (None, 0.0))
        unmatched.append(
            UnmatchedLocalTrack(
                local_index=local_index,
                local_track=local,
                reason="below_threshold" if best_score else "no_candidate",
                best_target_index=best_index if best_index is not None and best_index >= 0 else None,
                best_score=best_score,
            )
        )

    missing = [track for index, track in enumerate(mb_tracks) if index not in selected_targets]
    warnings: List[str] = []
    if unmatched:
        warnings.append("extra_local_tracks")
    if missing:
        warnings.append("missing_canonical_tracks")
    unique_conflicts = sorted(set(global_conflicts))
    return TrackAlignmentResult(
        assignments=assignments,
        unmatched_local=unmatched,
        missing_tracks=missing,
        conflicts=unique_conflicts,
        warnings=warnings,
    )
