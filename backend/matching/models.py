from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class AcoustIDStatus(str, Enum):
    CONFIRMED = "confirmed"
    CONFLICT = "conflict"
    NO_RESULT = "no_result"
    UNAVAILABLE = "unavailable"
    AMBIGUOUS = "ambiguous"


class ConfidenceState(str, Enum):
    CONFIRMED = "confirmed"
    STRONG_MATCH = "strong_match"
    REVIEW_RECOMMENDED = "review_recommended"
    CONFLICT = "conflict"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass
class TrackAssignment:
    local_index: int
    target_index: int
    local_track: Dict[str, Any]
    target_track: Dict[str, Any]
    score: float
    title_similarity: float
    artist_similarity: float
    duration_delta: Optional[float]
    position_match: bool
    status: str
    acoustid_status: AcoustIDStatus = AcoustIDStatus.UNAVAILABLE
    positives: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)

    @property
    def local_title(self) -> str:
        return str(self.local_track.get("title") or "")

    @property
    def target_title(self) -> str:
        return str(self.target_track.get("title") or "")

    @property
    def target_recording_id(self) -> str:
        return str(
            self.target_track.get("recording_id")
            or self.target_track.get("mb_recording_id")
            or self.target_track.get("mb_trackid")
            or ""
        )

    @property
    def target_track_id(self) -> str:
        return str(
            self.target_track.get("track_id")
            or self.target_track.get("mb_track_id")
            or self.target_track.get("mb_releasetrackid")
            or self.target_recording_id
            or f"{self.target_track.get('disc') or 1}:{self.target_track.get('track') or self.target_index + 1}"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "local_index": self.local_index,
            "target_index": self.target_index,
            "local_title": self.local_title,
            "target_title": self.target_title,
            "target_recording_id": self.target_recording_id,
            "target_track_id": self.target_track_id,
            "score": round(float(self.score), 6),
            "title_similarity": round(float(self.title_similarity), 6),
            "artist_similarity": round(float(self.artist_similarity), 6),
            "duration_delta": self.duration_delta,
            "position_match": self.position_match,
            "status": self.status,
            "acoustid_status": self.acoustid_status.value,
            "positives": list(self.positives),
            "warnings": list(self.warnings),
            "conflicts": list(self.conflicts),
        }


@dataclass
class UnmatchedLocalTrack:
    local_index: int
    local_track: Dict[str, Any]
    reason: str
    best_target_index: Optional[int] = None
    best_score: float = 0.0

    @property
    def local_title(self) -> str:
        return str(self.local_track.get("title") or "")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "local_index": self.local_index,
            "local_title": self.local_title,
            "reason": self.reason,
            "best_target_index": self.best_target_index,
            "best_score": round(float(self.best_score), 6),
        }


@dataclass
class TrackAlignmentResult:
    assignments: List[TrackAssignment] = field(default_factory=list)
    unmatched_local: List[UnmatchedLocalTrack] = field(default_factory=list)
    missing_tracks: List[Dict[str, Any]] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def matched_count(self) -> int:
        return sum(1 for row in self.assignments if row.status == "matched")

    @property
    def conflict_count(self) -> int:
        return sum(1 for row in self.assignments if row.status == "conflict")

    @property
    def missing_count(self) -> int:
        return len(self.missing_tracks)

    @property
    def unmatched_local_count(self) -> int:
        return len(self.unmatched_local)

    @property
    def total_target_tracks(self) -> int:
        return len(self.assignments) + len(self.missing_tracks)

    def as_pairs(self) -> List[tuple]:
        pairs = []
        for row in self.assignments:
            pairs.append((
                int(row.target_track.get("disc") or 1),
                int(row.target_track.get("track") or row.target_index + 1),
                row.target_recording_id,
                int(row.local_track.get("disc") or 1),
                int(row.local_track.get("track") or row.local_index + 1),
                row.local_title,
            ))
        return sorted(pairs)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "assignments": [row.to_dict() for row in self.assignments],
            "unmatched_local": [row.to_dict() for row in self.unmatched_local],
            "missing_tracks": list(self.missing_tracks),
            "matched_count": self.matched_count,
            "conflict_count": self.conflict_count,
            "missing_count": self.missing_count,
            "unmatched_local_count": self.unmatched_local_count,
            "total_target_tracks": self.total_target_tracks,
            "conflicts": list(self.conflicts),
            "warnings": list(self.warnings),
        }


@dataclass
class ReleaseMatch:
    release_id: str = ""
    release_group_id: str = ""
    state: ConfidenceState = ConfidenceState.INSUFFICIENT_EVIDENCE
    score: float = 0.0
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "release_id": self.release_id,
            "release_group_id": self.release_group_id,
            "state": self.state.value,
            "score": round(float(self.score), 6),
            "evidence": dict(self.evidence),
        }


@dataclass
class ReleaseGroupMatchResult:
    suggested_identity: Dict[str, Any]
    state: ConfidenceState
    release_group_status: str
    release_match: ReleaseMatch = field(default_factory=ReleaseMatch)
    track_alignment: TrackAlignmentResult = field(default_factory=TrackAlignmentResult)
    score: float = 0.0
    score_components: Dict[str, float] = field(default_factory=dict)
    positive_evidence: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    missing_evidence: List[str] = field(default_factory=list)
    review_reasons: List[str] = field(default_factory=list)
    action_allowed: bool = False
    trust_model: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "suggested_identity": dict(self.suggested_identity),
            "state": self.state.value,
            "release_group_status": self.release_group_status,
            "release_match": self.release_match.to_dict(),
            "track_alignment": self.track_alignment.to_dict(),
            "score": round(float(self.score), 6),
            "score_components": {k: round(float(v), 6) for k, v in self.score_components.items()},
            "positive_evidence": list(self.positive_evidence),
            "conflicts": list(self.conflicts),
            "missing_evidence": list(self.missing_evidence),
            "review_reasons": list(self.review_reasons),
            "action_allowed": self.action_allowed,
            "trust_model": self.trust_model,
        }
