from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .models import ConfidenceState, IdentityProof, ReleaseGroupMatchResult, ReleaseMatch, TrackAlignmentResult
from .normalize import normalize_artist, normalize_title, similarity
from .track_alignment import align_tracks_global


def _s(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _id(value: Any) -> str:
    return _s(value).strip().lower()


def _release_group_id(row: Dict[str, Any]) -> str:
    return _id(row.get("release_group_id") or row.get("mb_releasegroupid") or row.get("rgid") or "")


def _release_id(row: Dict[str, Any]) -> str:
    return _id(row.get("release_id") or row.get("mb_albumid") or row.get("album_id") or "")


def _artist_id(row: Dict[str, Any]) -> str:
    return _id(row.get("artist_id") or row.get("mb_artistid") or row.get("albumartist_id") or "")


def _album_title(row: Dict[str, Any]) -> str:
    return _s(row.get("album") or row.get("release_group_title") or row.get("release_title") or row.get("title") or "")


def _candidate_identity(candidate: Dict[str, Any]) -> Dict[str, Any]:
    rg_title = _s(candidate.get("release_group_title") or candidate.get("album") or candidate.get("title") or candidate.get("release_title") or "")
    release_title = _s(candidate.get("release_title") or rg_title)
    artist = _s(candidate.get("artist") or candidate.get("albumartist") or candidate.get("release_artist") or "")
    return {
        "artist": artist,
        "artist_id": _artist_id(candidate),
        "release_group_title": rg_title,
        "release_group_id": _release_group_id(candidate),
        "release_id": _release_id(candidate),
        "release_title": release_title,
        "date": _s(candidate.get("date") or candidate.get("year") or ""),
    }


def _candidate_metadata_mixed(candidate: Dict[str, Any], identity: Dict[str, Any]) -> bool:
    display_album = _s(candidate.get("display_album") or "")
    display_artist = _s(candidate.get("display_artist") or "")
    if display_album and normalize_title(display_album) != normalize_title(identity.get("release_group_title")):
        return True
    if display_artist and normalize_artist(display_artist) != normalize_artist(identity.get("artist")):
        return True
    return False


def _track_coverage_score(alignment: TrackAlignmentResult) -> float:
    total_targets = alignment.total_target_tracks
    if total_targets <= 0:
        return 0.0
    return alignment.matched_count / total_targets


def _acoustid_component(alignment: TrackAlignmentResult) -> Tuple[float, List[str]]:
    statuses = [row.acoustid_status.value for row in alignment.assignments]
    missing: List[str] = []
    if "conflict" in statuses:
        return 0.0, missing
    if "confirmed" in statuses:
        return statuses.count("confirmed") / max(1, len(statuses)), missing
    if "no_result" in statuses:
        missing.append("acoustid_no_result")
    if "unavailable" in statuses:
        missing.append("acoustid_unavailable")
    if "ambiguous" in statuses:
        missing.append("acoustid_ambiguous")
    return 0.0, missing


def _append_unique(target: List[str], values: List[str]) -> None:
    for value in values:
        if value and value not in target:
            target.append(value)


def _release_candidate_alignment(
    local_tracks: List[Dict[str, Any]],
    release: Dict[str, Any],
    *,
    trust_model: str,
) -> Tuple[TrackAlignmentResult, float]:
    tracks = list(release.get("tracks") or [])
    alignment = align_tracks_global(local_tracks, tracks, trust_model=trust_model)
    score = (
        alignment.matched_count * 10.0
        - alignment.missing_count * 1.5
        - alignment.unmatched_local_count * 1.2
        - alignment.conflict_count * 20.0
    )
    if tracks:
        score += alignment.matched_count / len(tracks)
    return alignment, score


def _select_release_candidate(
    candidate: Dict[str, Any],
    local_tracks: List[Dict[str, Any]],
    release_candidates: Optional[List[Dict[str, Any]]],
    *,
    trust_model: str,
) -> Tuple[ReleaseMatch, TrackAlignmentResult, bool]:
    candidate_tracks = list(candidate.get("tracks") or [])
    base_alignment = align_tracks_global(local_tracks, candidate_tracks, trust_model=trust_model) if local_tracks or candidate_tracks else TrackAlignmentResult()
    base_release = ReleaseMatch(
        release_id=_release_id(candidate),
        release_group_id=_release_group_id(candidate),
        state=ConfidenceState.REVIEW_RECOMMENDED,
        score=_track_coverage_score(base_alignment),
        evidence={"source": "selected_candidate"},
    )
    if not release_candidates or not local_tracks:
        return base_release, base_alignment, False

    candidate_rgid = _release_group_id(candidate)
    best_release = candidate
    best_alignment = base_alignment
    best_score = (
        base_alignment.matched_count * 10.0
        - base_alignment.missing_count * 1.5
        - base_alignment.unmatched_local_count * 1.2
        - base_alignment.conflict_count * 20.0
    )
    for release in release_candidates:
        if candidate_rgid and _release_group_id(release) and _release_group_id(release) != candidate_rgid:
            continue
        alignment, score = _release_candidate_alignment(local_tracks, release, trust_model=trust_model)
        if score > best_score:
            best_score = score
            best_release = release
            best_alignment = alignment
    changed = _release_id(best_release) and _release_id(best_release) != _release_id(candidate)
    release_match = ReleaseMatch(
        release_id=_release_id(best_release),
        release_group_id=_release_group_id(best_release) or candidate_rgid,
        state=ConfidenceState.STRONG_MATCH if best_alignment.matched_count else ConfidenceState.REVIEW_RECOMMENDED,
        score=_track_coverage_score(best_alignment),
        evidence={
            "source": "release_candidates" if changed else "selected_candidate",
            "matched_tracks": best_alignment.matched_count,
            "missing_tracks": best_alignment.missing_count,
            "unmatched_local_tracks": best_alignment.unmatched_local_count,
        },
    )
    return release_match, best_alignment, changed


def evaluate_release_group_candidate(
    local_album: Dict[str, Any],
    candidate: Dict[str, Any],
    *,
    local_tracks: Optional[List[Dict[str, Any]]] = None,
    release_candidates: Optional[List[Dict[str, Any]]] = None,
    trust_model: str = "existing_library",
    source_manifest_digest: str = "",
    reviewed: bool = False,
) -> ReleaseGroupMatchResult:
    local_album = dict(local_album or {})
    candidate = dict(candidate or {})
    local_tracks = list(local_tracks or [])
    identity = _candidate_identity(candidate)
    conflicts: List[str] = []
    review_reasons: List[str] = []
    missing_evidence: List[str] = []
    positives: List[str] = []

    candidate_rgid = identity["release_group_id"]
    local_rgid = _id(
        local_album.get("embedded_release_group_id")
        or local_album.get("mb_releasegroupid")
        or local_album.get("release_group_id")
        or ""
    )
    if _candidate_metadata_mixed(candidate, identity):
        conflicts.append("candidate_metadata_mixed")
        review_reasons.append("candidate_metadata_mixed")

    release_group_status = "candidate"
    if not candidate_rgid:
        missing_evidence.append("release_group_id_missing")
        review_reasons.append("release_group_missing")
        release_group_status = "missing"
    elif local_rgid and local_rgid != candidate_rgid:
        conflicts.append("wrong_release_group")
        conflicts.append("release_group_conflict")
        review_reasons.append("wrong_release_group")
        release_group_status = "conflict"
    elif local_rgid and local_rgid == candidate_rgid:
        positives.append("embedded_release_group_id_matches")
        release_group_status = "validated"

    local_artist_id = _artist_id(local_album)
    candidate_artist_id = identity.get("artist_id") or ""
    if local_artist_id and candidate_artist_id:
        if local_artist_id == candidate_artist_id:
            positives.append("artist_id_matches")
        else:
            conflicts.append("artist_id_conflict")

    local_release_id = _id(local_album.get("embedded_release_id") or local_album.get("mb_albumid") or "")
    if local_release_id and _release_id(candidate) and local_release_id == _release_id(candidate):
        positives.append("embedded_release_id_matches_candidate_release")

    local_album_title = _album_title(local_album)
    local_artist = _s(local_album.get("artist") or local_album.get("albumartist") or "")
    album_score = similarity(local_album_title, identity.get("release_group_title") or identity.get("release_title") or "")
    artist_score = similarity(normalize_artist(local_artist), normalize_artist(identity.get("artist") or "")) if local_artist else 0.0

    release_match, alignment, release_changed = _select_release_candidate(
        candidate,
        local_tracks,
        release_candidates,
        trust_model=trust_model,
    )
    if release_changed:
        review_reasons.append("exact_release_ambiguous")
    _append_unique(conflicts, alignment.conflicts)
    _append_unique(review_reasons, alignment.warnings)
    if alignment.matched_count == 0 and (local_tracks and (candidate.get("tracks") or [])):
        review_reasons.append("no_tracks_matched")
    if alignment.missing_count:
        review_reasons.append("missing_canonical_tracks")
    if alignment.unmatched_local_count:
        review_reasons.append("extra_local_tracks")

    acoustid_score, acoustid_missing = _acoustid_component(alignment)
    _append_unique(missing_evidence, acoustid_missing)

    hard_track_positive = any(
        "embedded_recording_id_matches" in row.positives or "acoustid_recording_confirmed" in row.positives
        for row in alignment.assignments
    )
    complete_alignment = bool(alignment.total_target_tracks) and alignment.matched_count == alignment.total_target_tracks and not alignment.unmatched_local_count
    coverage = _track_coverage_score(alignment)

    # Identity-vs-completeness split (ARCH-002 Part 3): local coverage (are
    # all the tracks that exist locally deterministically proven?) and
    # target coverage (does the whole release exist locally?) are separate
    # facts. A partial album can have complete local coverage without
    # complete target coverage -- that's real identity for the tracks
    # present, not the same thing as a fully confirmed release.
    local_tracks_total = len(local_tracks)
    local_tracks_verified = sum(1 for row in alignment.assignments if row.is_deterministic)
    local_coverage_complete = bool(local_tracks_total) and local_tracks_verified == local_tracks_total and not alignment.unmatched_local_count
    target_tracks_total = alignment.total_target_tracks
    target_tracks_matched = alignment.matched_count
    target_coverage_complete = bool(target_tracks_total) and target_tracks_matched == target_tracks_total
    release_complete = target_coverage_complete and not alignment.unmatched_local_count
    identity_score = 1.0 if release_group_status == "validated" else 0.55 if candidate_rgid else 0.0
    if hard_track_positive:
        identity_score = max(identity_score, 0.85)
        if release_group_status == "candidate" and candidate_rgid:
            release_group_status = "validated"
            positives.append("track_recording_identity_supports_release_group")

    if conflicts or not candidate_rgid or release_group_status != "validated":
        identity_proof = IdentityProof.INSUFFICIENT
    elif release_complete:
        identity_proof = IdentityProof.CONFIRMED_RELEASE
    elif local_coverage_complete:
        identity_proof = IdentityProof.DETERMINISTIC_TRACK_RECORDING_ID
    else:
        identity_proof = IdentityProof.RELEASE_GROUP_ID

    score_components = {
        "artist_text": artist_score,
        "album_title": album_score,
        "track_coverage": coverage,
        "identity": identity_score,
        "acoustid": acoustid_score,
    }
    score = (
        artist_score * 0.15
        + album_score * 0.15
        + coverage * 0.35
        + identity_score * 0.25
        + acoustid_score * 0.10
    )

    state = ConfidenceState.REVIEW_RECOMMENDED
    action_allowed = False
    if conflicts:
        state = ConfidenceState.CONFLICT
        release_group_status = "conflict" if release_group_status != "missing" else release_group_status
    elif not candidate_rgid:
        state = ConfidenceState.INSUFFICIENT_EVIDENCE
    elif release_group_status == "validated" and complete_alignment and (local_rgid or hard_track_positive):
        state = ConfidenceState.CONFIRMED
        action_allowed = True
    elif release_group_status == "validated" and (coverage > 0.0 or local_rgid):
        state = ConfidenceState.STRONG_MATCH
    elif trust_model == "fresh_reviewed_import" and reviewed and source_manifest_digest and coverage >= 0.70:
        state = ConfidenceState.STRONG_MATCH
        action_allowed = True
    elif coverage >= 0.70 and album_score >= 0.72 and (artist_score >= 0.68 or not local_artist):
        state = ConfidenceState.REVIEW_RECOMMENDED
    else:
        state = ConfidenceState.INSUFFICIENT_EVIDENCE
        if "insufficient_evidence" not in review_reasons:
            review_reasons.append("insufficient_evidence")

    if state == ConfidenceState.CONFLICT:
        action_allowed = False
    if trust_model == "fresh_reviewed_import" and not local_tracks:
        missing_evidence.append("track_alignment_missing")

    return ReleaseGroupMatchResult(
        suggested_identity=identity,
        state=state,
        release_group_status=release_group_status,
        release_match=release_match,
        track_alignment=alignment,
        score=max(0.0, min(1.0, score)),
        score_components=score_components,
        positive_evidence=positives,
        conflicts=sorted(set(conflicts)),
        missing_evidence=sorted(set(missing_evidence)),
        review_reasons=sorted(set(review_reasons)),
        action_allowed=action_allowed,
        trust_model=trust_model,
        identity_proof=identity_proof,
        local_tracks_total=local_tracks_total,
        local_tracks_verified=local_tracks_verified,
        local_coverage_complete=local_coverage_complete,
        target_tracks_total=target_tracks_total,
        target_tracks_matched=target_tracks_matched,
        target_coverage_complete=target_coverage_complete,
        release_complete=release_complete,
    )
