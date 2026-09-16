"""Canonical matching primitives for ARCH-002.

This package is intentionally dependency-free so the same evidence logic can
run in the web manager and in the flattened beets control-agent image.
"""

from .evidence import evaluate_release_group_candidate
from .models import (
    AcoustIDStatus,
    ConfidenceState,
    DEFAULT_MATCH_POLICY,
    MatchPolicy,
    ReleaseGroupMatchResult,
    ReleaseMatch,
    TrackAlignmentResult,
    TrackAssignment,
    UnmatchedLocalTrack,
)
from .normalize import normalize_artist, normalize_title, similarity, title_variants
from .track_alignment import align_tracks_global

__all__ = [
    "AcoustIDStatus",
    "ConfidenceState",
    "DEFAULT_MATCH_POLICY",
    "MatchPolicy",
    "ReleaseGroupMatchResult",
    "ReleaseMatch",
    "TrackAlignmentResult",
    "TrackAssignment",
    "UnmatchedLocalTrack",
    "align_tracks_global",
    "evaluate_release_group_candidate",
    "normalize_artist",
    "normalize_title",
    "similarity",
    "title_variants",
]
