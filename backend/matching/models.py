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


class IdentityProof(str, Enum):
    """How album/release-group identity was established, independent of
    whether the release as a whole is complete. Distinct from
    ConfidenceState: a partial album with every present track
    deterministically proven is DETERMINISTIC_TRACK_RECORDING_ID even
    though ConfidenceState may only reach STRONG_MATCH (it is not a
    complete release)."""

    INSUFFICIENT = "insufficient"
    RELEASE_GROUP_ID = "release_group_id"
    DETERMINISTIC_TRACK_RECORDING_ID = "deterministic_track_recording_id"
    CONFIRMED_RELEASE = "confirmed_release"


class ActionScope(str, Enum):
    """What an operation needs proven before can_auto_accept() authorizes
    it. Action eligibility is operation-scoped, not one global rule: a
    metadata update or identity attach touching only the local tracks that
    are actually present only needs those tracks deterministically proven
    (VERIFIED_SUBSET); declaring an album complete, or a destructive merge
    that presumes full track membership, needs the whole release accounted
    for (FULL_RELEASE)."""

    FULL_RELEASE = "full_release"
    VERIFIED_SUBSET = "verified_subset"


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
    def is_deterministic(self) -> bool:
        """True when this assignment is backed by hard per-track identity
        evidence (an embedded Recording ID matching the target, or a
        confirmed AcoustID fingerprint) rather than text/position/duration
        similarity alone."""
        return (
            self.status == "matched"
            and (
                "embedded_recording_id_matches" in self.positives
                or "acoustid_recording_confirmed" in self.positives
            )
        )

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


@dataclass(frozen=True)
class MatchPolicy:
    """Configurable requirements for `ReleaseGroupMatchResult.can_auto_accept()`.

    ARCH-002 Part 14: every production caller that decides whether a match is
    safe to act on automatically must go through this one policy object
    instead of writing its own `if confidence >= 0.70 and ...` check. A
    caller with stricter needs (e.g. "never auto-accept a fresh reviewed
    import without full track coverage") passes a narrower policy; it never
    invents new evaluation logic of its own.

    A hard conflict always blocks acceptance regardless of policy -- that is
    enforced in `can_auto_accept()` itself, not configurable here, because
    "hard conflicts cannot be averaged away" is a non-negotiable rule, not a
    caller preference.
    """

    # States considered acceptable for unattended automation, in addition to
    # CONFIRMED (always acceptable when reached -- it already requires a
    # validated Release Group and complete track alignment or a hard
    # per-track identity positive).
    allow_strong_match: bool = True
    # Minimum track_alignment coverage (matched / total_target_tracks)
    # required when the state is STRONG_MATCH rather than CONFIRMED. Ignored
    # for CONFIRMED, which already implies complete alignment.
    min_coverage_for_strong_match: float = 0.0
    # Trust models this policy accepts automation from at all. A caller that
    # must never auto-accept a fresh, unreviewed import (only ever a
    # validated existing-library match) passes {"existing_library"} here.
    allowed_trust_models: Optional[frozenset] = None
    # What the calling operation needs proven. FULL_RELEASE (default) is
    # the historical strict behavior: action_allowed/state as computed by
    # evaluate_release_group_candidate(), which requires the whole release
    # to be accounted for. VERIFIED_SUBSET is for operations that only act
    # on the local tracks actually present (a metadata update or identity
    # attach touching just those files) -- it accepts deterministic
    # per-track proof for a partial album, without requiring the rest of
    # the release to exist locally. A caller declaring an album complete,
    # or doing a destructive merge that presumes full track membership,
    # must use FULL_RELEASE.
    scope: ActionScope = ActionScope.FULL_RELEASE

    def trust_model_allowed(self, trust_model: str) -> bool:
        return self.allowed_trust_models is None or trust_model in self.allowed_trust_models


#: Default policy: matches the historical inline decision in
#: evaluate_release_group_candidate() -- CONFIRMED or STRONG_MATCH, any
#: trust model, no extra coverage floor beyond what produced STRONG_MATCH in
#: the first place. Kept as the module default so existing callers that
#: already consume `action_allowed` see no behavior change.
DEFAULT_MATCH_POLICY = MatchPolicy()


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
    # Identity-vs-completeness split (ARCH-002 Part 3): these describe
    # release identity, local coverage, and target coverage as three
    # separate facts rather than conflating them into one state. A partial
    # album (2 of 18 target tracks) with both local tracks deterministically
    # proven has local_coverage_complete=True, target_coverage_complete=False
    # -- real identity, verified only for the tracks actually present.
    identity_proof: IdentityProof = IdentityProof.INSUFFICIENT
    local_tracks_total: int = 0
    local_tracks_verified: int = 0
    local_coverage_complete: bool = False
    target_tracks_total: int = 0
    target_tracks_matched: int = 0
    target_coverage_complete: bool = False
    release_complete: bool = False

    def can_auto_accept(self, policy: MatchPolicy = DEFAULT_MATCH_POLICY) -> bool:
        """The one method every production caller should use to decide
        whether this match may authorize an unattended mutation.

        Hard conflicts always block, unconditionally -- not
        policy-configurable, per the standing rule that they cannot be
        averaged away. Beyond that, what's required depends on
        `policy.scope`:

        FULL_RELEASE (default): `action_allowed` (set once, by
        `evaluate_release_group_candidate()`, from real evidence -- hard
        identity, complete alignment, or an explicitly reviewed+bound fresh
        import over threshold) is the ground-truth authorization for
        whole-release operations. This method can only NARROW that
        decision for a caller with stricter needs; it can never grant
        automation `action_allowed` itself withheld (e.g. a STRONG_MATCH
        reached only through a validated embedded Release Group ID with
        incomplete track alignment -- real identity evidence, but not
        enough by itself to mutate the whole release unattended).

        VERIFIED_SUBSET: for operations that only act on the local tracks
        actually present. Deterministic per-track proof for those tracks
        (local_coverage_complete) plus a validated Release Group is
        sufficient authorization -- it does not require target_coverage
        (the rest of the release does not need to exist locally). This is
        deliberately independent of `action_allowed`/`state`, which encode
        whole-release completeness and would otherwise force a partial
        album through the same bar as a complete one.
        """
        if self.conflicts or self.state == ConfidenceState.CONFLICT:
            return False
        if not policy.trust_model_allowed(self.trust_model):
            return False
        if policy.scope == ActionScope.VERIFIED_SUBSET:
            return bool(self.release_group_status == "validated" and self.local_coverage_complete)
        if not self.action_allowed:
            return False
        if self.state == ConfidenceState.STRONG_MATCH:
            if not policy.allow_strong_match:
                return False
            if policy.min_coverage_for_strong_match > 0.0:
                coverage = (
                    self.track_alignment.matched_count / self.track_alignment.total_target_tracks
                    if self.track_alignment.total_target_tracks
                    else 0.0
                )
                if coverage < policy.min_coverage_for_strong_match:
                    return False
        return True

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
            "identity_proof": self.identity_proof.value,
            "local_tracks_total": self.local_tracks_total,
            "local_tracks_verified": self.local_tracks_verified,
            "local_coverage_complete": self.local_coverage_complete,
            "target_tracks_total": self.target_tracks_total,
            "target_tracks_matched": self.target_tracks_matched,
            "target_coverage_complete": self.target_coverage_complete,
            "release_complete": self.release_complete,
        }
