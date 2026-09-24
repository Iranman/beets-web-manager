"""Canonical matching primitives for ARCH-002.

This package is intentionally dependency-free so the same evidence logic can
run in the web manager and in the flattened beets control-agent image.
"""

from .evidence import evaluate_release_group_candidate
from .models import (
    AcoustIDStatus,
    ActionScope,
    ConfidenceState,
    DEFAULT_MATCH_POLICY,
    IdentityProof,
    MatchPolicy,
    ReleaseGroupMatchResult,
    ReleaseMatch,
    TrackAlignmentResult,
    TrackAssignment,
    UnmatchedLocalTrack,
)
from .normalize import (
    normalize_artist,
    normalize_title,
    normalize_track_title_for_matching,
    similarity,
    strip_track_filename_id_suffix,
    title_variants,
    track_feature_variants,
    track_filename_has_source_id_suffix,
    track_parenthetical_alias_variants,
    track_path_prefixes,
    track_title_variants_for_matching,
)
from .track_alignment import (
    album_track_score,
    align_tracks_global,
    best_album_track_match,
)

__all__ = [
    "AcoustIDStatus",
    "ActionScope",
    "ConfidenceState",
    "DEFAULT_MATCH_POLICY",
    "IdentityProof",
    "MatchPolicy",
    "ReleaseGroupMatchResult",
    "ReleaseMatch",
    "TrackAlignmentResult",
    "TrackAssignment",
    "UnmatchedLocalTrack",
    "album_track_score",
    "align_tracks_global",
    "best_album_track_match",
    "evaluate_release_group_candidate",
    "normalize_artist",
    "normalize_title",
    "normalize_track_title_for_matching",
    "similarity",
    "strip_track_filename_id_suffix",
    "title_variants",
    "track_feature_variants",
    "track_filename_has_source_id_suffix",
    "track_parenthetical_alias_variants",
    "track_path_prefixes",
    "track_title_variants_for_matching",
]

