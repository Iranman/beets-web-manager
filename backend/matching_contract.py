from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
import re
import unicodedata


SimilarityFn = Callable[[str, str], float]

_MB_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _s(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace").strip()
        except Exception:
            return ""
    return str(value).strip()


def _uuid(value: Any) -> str:
    text = _s(value).strip().lower()
    return text if _MB_UUID_RE.match(text) else ""


def _year(value: Any) -> str:
    match = re.match(r"^((?:19|20)\d{2})", _s(value))
    return match.group(1) if match else ""


def _number(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _duration_seconds(value: Any) -> Optional[float]:
    number = _number(value)
    if number is not None and number > 0:
        return number / 1000.0 if number > 10000 else number
    text = _s(value)
    if not text:
        return None
    try:
        parts = [float(part) for part in text.split(":")]
    except Exception:
        return None
    if len(parts) == 2:
        return parts[0] * 60.0 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600.0 + parts[1] * 60.0 + parts[2]
    return None


def _norm(value: str) -> str:
    text = unicodedata.normalize("NFKD", _s(value).casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("&", " and ")
    text = re.sub(r"\b(?:feat|ft|featuring)\.?\s+.*$", "", text, flags=re.I)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _similarity(left: str, right: str) -> float:
    a = _norm(left)
    b = _norm(right)
    if not a or not b:
        return 0.0
    score = SequenceMatcher(None, a, b).ratio()
    if set(a.split()) & set(b.split()):
        score = max(score, 0.70)
    return score


def _status(score: float, strong: float = 0.82, fuzzy: float = 0.68) -> str:
    if score >= strong:
        return "yes"
    if score >= fuzzy:
        return "fuzzy"
    return "no"


def _int_or_none(value: Any) -> Optional[int]:
    text = _s(value)
    if not text:
        return None
    try:
        return int(text)
    except Exception:
        return None


def _round_score(value: Any) -> float:
    try:
        return round(max(0.0, min(1.0, float(value or 0))), 3)
    except Exception:
        return 0.0


def _audio_score(candidate: Mapping[str, Any]) -> float:
    try:
        raw = float(candidate.get("score") or 0)
    except Exception:
        return 0.0
    return _round_score(raw / 100.0 if raw > 1 else raw)


def _safe_release(release: Mapping[str, Any]) -> Dict[str, Any]:
    release_id = _uuid(release.get("mb_albumid") or release.get("release_id"))
    release_group_id = _uuid(release.get("mb_releasegroupid") or release.get("release_group_id"))
    title = _s(release.get("album") or release.get("title"))
    year = _year(release.get("year") or release.get("date"))
    medium_position = _int_or_none(release.get("medium_position"))
    track_number = _s(release.get("track_number") or release.get("track"))
    track_count = _int_or_none(release.get("track_count") or release.get("tracks"))
    duration_ms = _int_or_none(release.get("duration_ms"))
    return {
        "release_id": release_id,
        "mb_albumid": release_id,
        "mb_url": f"https://musicbrainz.org/release/{release_id}" if release_id else "",
        "release_group_id": release_group_id,
        "mb_releasegroupid": release_group_id,
        "mb_releasegroupurl": (
            f"https://musicbrainz.org/release-group/{release_group_id}" if release_group_id else ""
        ),
        "title": title,
        "album": title,
        "artist": _s(release.get("artist") or release.get("release_artist")),
        "date": _s(release.get("date")),
        "year": year,
        "country": _s(release.get("country")),
        "status": _s(release.get("status")),
        "label": _s(release.get("label")),
        "medium_format": _s(release.get("medium_format") or release.get("media_format")),
        "media_format": _s(release.get("medium_format") or release.get("media_format")),
        "medium_position": medium_position,
        "disc": _s(release.get("disc") or medium_position or ""),
        "track_number": track_number,
        "track": track_number,
        "track_position": _int_or_none(release.get("track_position")),
        "tracktotal": _s(release.get("tracktotal")),
        "tracks": track_count,
        "track_count": track_count,
        "duration_ms": duration_ms,
        "release_group_primary_type": _s(release.get("release_group_primary_type")),
        "release_group_secondary_types": list(release.get("release_group_secondary_types") or []),
        "local_match": dict(release.get("local_match") or {}) if isinstance(release.get("local_match"), Mapping) else {},
    }


def _safe_linked_releases(releases: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [_safe_release(release) for release in releases if isinstance(release, Mapping)]


def _field(local: str, suggested: str, score: float) -> Dict[str, Any]:
    return {
        "status": _status(score),
        "score": round(score, 3),
        "local": local,
        "suggested": suggested,
    }


@dataclass(frozen=True)
class AiState:
    configured: bool = False
    attempted: bool = False
    available: bool = False
    unavailability_reason: str = ""
    contribution: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        contribution = self.contribution if isinstance(self.contribution, Mapping) else {}
        safe_contribution = {
            "candidate_index": contribution.get("candidate_index"),
            "mb_trackid": _uuid(contribution.get("mb_trackid")),
            "mb_albumid": _uuid(contribution.get("mb_albumid")),
            "mb_releasegroupid": _uuid(contribution.get("mb_releasegroupid")),
            "confidence": _s(contribution.get("confidence")),
            "reason": _s(contribution.get("reason")),
        }
        return {
            "configured": bool(self.configured),
            "attempted": bool(self.attempted),
            "available": bool(self.available),
            "unavailability_reason": _s(self.unavailability_reason),
            "contribution": {k: v for k, v in safe_contribution.items() if v not in (None, "")},
        }


@dataclass(frozen=True)
class MatchingDecision:
    input: Dict[str, Any]
    identity: Dict[str, Any]
    evidence: Dict[str, Any]
    ai: Dict[str, Any]
    decision: Dict[str, Any]
    candidate: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contract_version": 1,
            "input": self.input,
            "identity": self.identity,
            "evidence": self.evidence,
            "ai": self.ai,
            "decision": self.decision,
            "warnings": list(self.decision.get("warnings") or []),
        }

    def to_review_recording_candidate(self) -> Dict[str, Any]:
        recording_id = _s(next(iter(self.identity.get("recording_ids") or []), ""))
        release_id = _s(self.identity.get("release_id") or "")
        release_group_id = _s(self.identity.get("release_group_id") or "")
        recording = self.evidence.get("musicbrainz", {}).get("recording", {})
        release = self.evidence.get("musicbrainz", {}).get("selected_release", {})
        acoustid = self.evidence.get("acoustid", {})
        source = _s(self.candidate.get("source") or self.candidate.get("match_method") or "mb")
        if source == "musicbrainz":
            source = "mb"
        confidence_score = self.decision.get("confidence_score")
        return {
            "candidate_index": self.candidate.get("candidate_index", -1),
            "candidate_type": "recording",
            "mb_trackid": recording_id,
            "mb_url": f"https://musicbrainz.org/recording/{recording_id}" if recording_id else "",
            "musicbrainz_url": f"https://musicbrainz.org/recording/{recording_id}" if recording_id else "",
            "title": _s(recording.get("title")),
            "artist": _s(recording.get("artist_credit")),
            "recording_title": _s(recording.get("title")),
            "recording_artist": _s(recording.get("artist_credit")),
            "album": _s(release.get("title")),
            "year": _s(release.get("year")),
            "release_title": _s(release.get("title")),
            "release_artist": _s(release.get("artist")),
            "release_date": _s(release.get("date")),
            "release_year": _s(release.get("year")),
            "country": _s(release.get("country")),
            "medium_format": _s(release.get("medium_format")),
            "track_number": _s(self.identity.get("track_number") or release.get("track_number")),
            "medium_position": self.identity.get("medium_position"),
            "duration": _s(self.candidate.get("duration")),
            "source": source,
            "match_method": source,
            "score": self.candidate.get("score") or 0,
            "match_total": confidence_score,
            "confidence": _s(self.decision.get("confidence_tier")),
            "confidence_score": confidence_score,
            "score_breakdown": self.evidence.get("score_breakdown", {}),
            "decision": self.decision,
            "conflicts": list(self.decision.get("conflicts") or []),
            "warnings": list(self.decision.get("warnings") or []),
            "recommended_action": _s(self.decision.get("recommended_action")),
            "requires_confirmation": bool(self.decision.get("requires_confirmation")),
            "safety_result": _s(self.decision.get("safety_result")),
            "safety_key": _s(self.decision.get("safety_key")),
            "reason": _s(self.decision.get("reason")),
            "acoustid_score": acoustid.get("score"),
            "mb_albumid": release_id,
            "release_id": release_id,
            "mb_albumids": list(self.candidate.get("mb_albumids") or ([] if not release_id else [release_id])),
            "mb_releasegroupid": release_group_id,
            "release_group_id": release_group_id,
            "mb_releasegroupurl": (
                f"https://musicbrainz.org/release-group/{release_group_id}" if release_group_id else ""
            ),
            "selected_release": release,
            "linked_releases": self.evidence.get("musicbrainz", {}).get("linked_releases", [])[:12],
            "same_recording_release_count": int(
                self.evidence.get("musicbrainz", {}).get("same_recording_release_count") or 0
            ),
            "matching_local_release_found": bool(
                self.evidence.get("release_group", {}).get("matching_local_release_found")
            ),
            "matching_contract": self.to_dict(),
        }


def build_recording_matching_decision(
    *,
    current: Mapping[str, Any],
    candidate: Mapping[str, Any],
    details: Optional[Mapping[str, Any]] = None,
    selected_release: Optional[Mapping[str, Any]] = None,
    linked_releases: Optional[Sequence[Mapping[str, Any]]] = None,
    ai_state: Optional[AiState] = None,
    similarity_fn: Optional[SimilarityFn] = None,
) -> MatchingDecision:
    """Build the authoritative recording-candidate decision for Import Review.

    The serializer is intentionally additive: it preserves existing
    ReviewRecordingCandidate keys while keeping release-group identity distinct
    from release evidence.
    """
    current = current or {}
    candidate = candidate or {}
    details = details or {}
    selected_release = (
        selected_release
        or candidate.get("selected_release")
        or details.get("selected_release")
        or {}
    )
    linked_releases = linked_releases if linked_releases is not None else (
        candidate.get("linked_releases") or details.get("linked_releases") or []
    )
    similarity = similarity_fn or _similarity

    score_breakdown = dict(candidate.get("_match_score") or {})
    source = _s(candidate.get("source") or candidate.get("match_method") or score_breakdown.get("source") or "mb").lower()
    release = _safe_release(selected_release if isinstance(selected_release, Mapping) else {})
    safe_linked = _safe_linked_releases(linked_releases if isinstance(linked_releases, Sequence) else [])

    recording_id = _uuid(candidate.get("mb_trackid") or details.get("recording_id"))
    release_id = _uuid(
        release.get("release_id")
        or details.get("mb_albumid")
        or candidate.get("mb_albumid")
        or next(iter(candidate.get("mb_albumids") or []), "")
    )
    release_group_id = _uuid(
        release.get("release_group_id")
        or details.get("mb_releasegroupid")
        or candidate.get("mb_releasegroupid")
    )

    local_title = _s(current.get("title"))
    local_artist = _s(current.get("artist") or current.get("albumartist"))
    local_album_artist = _s(current.get("albumartist"))
    local_album = _s(current.get("album"))
    local_year = _year(current.get("year"))
    local_track = _int_or_none(current.get("track"))
    local_disc = _int_or_none(current.get("disc")) or 1
    local_duration = _duration_seconds(
        current.get("duration_seconds") or current.get("duration") or current.get("length")
    )
    recording_title = _s(details.get("recording_title") or candidate.get("recording_title") or candidate.get("title"))
    recording_artist = _s(
        details.get("recording_artist")
        or details.get("artist")
        or candidate.get("recording_artist")
        or (selected_release.get("artist") if isinstance(selected_release, Mapping) else "")
        or candidate.get("artist")
    )
    release_title = _s(release.get("title") or details.get("album") or candidate.get("album"))
    release_artist = _s(release.get("artist") or details.get("albumartist") or candidate.get("release_artist"))
    release_year = _year(release.get("year") or release.get("date") or details.get("year") or candidate.get("year"))
    suggested_duration = _duration_seconds(
        release.get("duration_ms")
        or details.get("recording_length_ms")
        or candidate.get("duration_ms")
        or candidate.get("duration")
    )

    title_score = similarity(local_title, recording_title) if local_title and recording_title else 0.0
    artist_score = max(
        similarity(local_artist, recording_artist) if local_artist and recording_artist else 0.0,
        similarity(local_album_artist, recording_artist) if local_album_artist and recording_artist else 0.0,
    )
    album_score = similarity(local_album, release_title) if local_album and release_title else 0.0

    duration_delta = None
    duration_status = "unknown"
    if local_duration and suggested_duration:
        duration_delta = abs(local_duration - suggested_duration)
        duration_status = "yes" if duration_delta <= 4 else ("tolerance" if duration_delta <= 10 else "conflict")

    year_status = "unknown"
    if local_year and release_year:
        year_status = "yes" if local_year == release_year else "conflict"

    local_release_group_id = _uuid(current.get("mb_releasegroupid"))
    release_group_status = "unknown"
    if local_release_group_id and release_group_id:
        release_group_status = "yes" if local_release_group_id == release_group_id else "conflict"

    selected_track_number = selected_release.get("track") if isinstance(selected_release, Mapping) else ""
    track_number = _s(release.get("track_number") or selected_track_number)
    suggested_track = _int_or_none(track_number)
    position_status = "unknown"
    if local_track is not None and suggested_track is not None:
        position_status = "yes" if local_track == suggested_track else "conflict"

    matching_local_release = bool(
        safe_linked
        and any(
            (similarity(local_album, _s(r.get("title"))) >= 0.78 if local_album else False)
            and (not local_year or _year(r.get("year") or r.get("date")) == local_year)
            for r in safe_linked
        )
    )

    missing: List[str] = []
    if not recording_id:
        missing.append("recording_id_missing")
    if not release_group_id:
        missing.append("release_group_id_missing")
    if not release_id:
        missing.append("release_id_missing")
    if not safe_linked:
        missing.append("linked_releases_missing")

    warnings: List[str] = []
    ai_payload = (ai_state or AiState()).to_dict()
    ai_recording = _uuid((ai_payload.get("contribution") or {}).get("mb_trackid"))
    if ai_recording and recording_id and ai_recording != recording_id:
        warnings.append("ai_recording_conflict")

    acoustid_score = _audio_score(candidate) if source == "acoustid" else 0.0
    try:
        match_total = float(score_breakdown.get("total") or 0)
    except Exception:
        match_total = 0.0
    confidence_score = _round_score(match_total or acoustid_score)

    fingerprint_status = _s(
        candidate.get("fingerprint_status")
        or candidate.get("acoustid_status")
        or candidate.get("acoustid_verification")
    ).lower()
    existing_recording_id = _uuid(current.get("mb_trackid"))
    recording_conflict = bool(existing_recording_id and recording_id and existing_recording_id != recording_id)
    fingerprint_conflict = bool(
        fingerprint_status in {"mismatch", "conflict", "rejected"}
        or candidate.get("acoustid_mismatch") is True
    )
    strong_acoustid = bool(source == "acoustid" and recording_id and acoustid_score >= 0.8)
    duration_agrees = duration_status in {"yes", "tolerance", "unknown"}
    position_agrees = position_status in {"yes", "unknown"}
    title_only_strong = bool(
        title_score < 0.82
        and strong_acoustid
        and artist_score >= 0.72
        and duration_agrees
        and position_agrees
        and not recording_conflict
        and not fingerprint_conflict
    )

    conflicts: List[str] = []
    if fingerprint_conflict:
        conflicts.append("fingerprint_conflict")
    if recording_conflict:
        conflicts.append("recording_id_conflict")
    if local_title and recording_title and title_score < 0.68 and not title_only_strong:
        conflicts.append("title_conflict")
    if local_artist and recording_artist and artist_score < 0.68:
        conflicts.append("artist_conflict")
    if local_album and release_title and album_score < 0.55:
        conflicts.append("album_conflict")
    if year_status == "conflict":
        conflicts.append("year_conflict")
    if duration_status == "conflict":
        conflicts.append("duration_conflict")
    if release_group_status == "conflict":
        conflicts.append("release_group_conflict")
    if position_status == "conflict" and not strong_acoustid:
        conflicts.append("track_position_conflict")
    if title_only_strong:
        warnings.append("title_mismatch_with_strong_recording_evidence")
    if len(safe_linked) > 1:
        warnings.append("same_recording_on_multiple_releases")

    evidence_supported = bool(
        strong_acoustid
        or confidence_score >= 0.78
        or _audio_score(candidate) >= 0.9
    )
    hard_conflicts = {
        "fingerprint_conflict",
        "recording_id_conflict",
        "title_conflict",
        "artist_conflict",
        "release_group_conflict",
    }
    has_hard_conflict = any(conflict in hard_conflicts for conflict in conflicts)
    attach_eligible = bool(
        recording_id
        and evidence_supported
        and not conflicts
        and (
            (title_score >= 0.82 and artist_score >= 0.72)
            or title_only_strong
        )
    )

    if not recording_id:
        safety_result = "No verified match"
        safety_key = "none"
        confidence_tier = "low"
        recommended_action = "Search MusicBrainz manually"
        eligibility_reason = "No MusicBrainz Recording ID is available."
    elif has_hard_conflict:
        safety_result = "Conflict"
        safety_key = "conflict"
        confidence_tier = "low"
        recommended_action = "Reject candidate"
        eligibility_reason = "Resolve conflicting deterministic evidence before attaching Recording ID."
    elif conflicts:
        safety_result = "Needs review"
        safety_key = "review"
        confidence_tier = "medium"
        recommended_action = "Confirm conflicts, then attach Recording ID"
        eligibility_reason = "Candidate has review-only conflicts: " + ", ".join(conflicts)
    elif attach_eligible:
        safety_result = "Safe to attach"
        safety_key = "safe"
        confidence_tier = "high"
        recommended_action = "Attach Recording ID"
        eligibility_reason = "Recording ID can be attached from deterministic evidence."
    else:
        safety_result = "Needs review"
        safety_key = "review"
        confidence_tier = "medium"
        recommended_action = "Use this candidate after review"
        eligibility_reason = "Candidate needs human review before attaching Recording ID."

    reason = _s(candidate.get("reason"))
    if not reason:
        if source == "acoustid":
            reason = "AcoustID fingerprint matched this recording; linked release context is ranked against local tags."
        else:
            reason = "MusicBrainz recording search matched the local title and artist clues."
    if matching_local_release and len(safe_linked) > 1:
        suffix = "Same recording appears on multiple releases; local album/year evidence selected the best linked release."
        if suffix not in reason:
            reason = f"{reason} {suffix}".strip()

    input_payload = {
        "local_artist": local_artist,
        "local_album_artist": local_album_artist,
        "local_album": local_album,
        "local_title": local_title,
        "track_number": local_track,
        "disc_number": local_disc,
        "duration_seconds": local_duration,
        "filename": _s(current.get("filename")),
        "source_path": _s(current.get("source_path") or current.get("path")),
        "existing_identifiers": {
            "recording_id": existing_recording_id,
            "release_id": _uuid(current.get("mb_albumid")),
            "release_group_id": local_release_group_id,
        },
    }
    identity_payload = {
        "release_group_id": release_group_id,
        "release_id": release_id,
        "recording_ids": [recording_id] if recording_id else [],
        "medium_position": release.get("medium_position"),
        "track_number": track_number,
    }
    musicbrainz_payload = {
        "recording": {
            "id": recording_id,
            "title": recording_title,
            "artist_credit": recording_artist,
            "url": f"https://musicbrainz.org/recording/{recording_id}" if recording_id else "",
        },
        "selected_release": {
            **release,
            "id": release_id,
            "url": f"https://musicbrainz.org/release/{release_id}" if release_id else "",
            "release_group_url": f"https://musicbrainz.org/release-group/{release_group_id}" if release_group_id else "",
        },
        "linked_releases": safe_linked,
        "same_recording_release_count": len(safe_linked),
    }
    evidence_payload = {
        "musicbrainz": musicbrainz_payload,
        "acoustid": {
            "present": source == "acoustid",
            "score": acoustid_score,
            "acoustid_id": _s(candidate.get("acoustid_id")),
            "status": fingerprint_status or ("candidate" if source == "acoustid" else "not_attempted"),
        },
        "tracklist": {
            "medium_position": release.get("medium_position"),
            "track_number": track_number,
            "position_match": position_status,
            "track_count_agreement": bool(candidate.get("track_count_agreement")),
        },
        "duration": {
            "local_seconds": local_duration,
            "suggested_seconds": suggested_duration,
            "status": duration_status,
            "delta_seconds": round(duration_delta, 1) if duration_delta is not None else None,
        },
        "filename_and_tags": {
            "filename": _s(current.get("filename")),
            "title": local_title,
            "artist": local_artist,
            "album": local_album,
            "year": local_year,
        },
        "release_group": {
            "local_release_group_id": local_release_group_id,
            "candidate_release_group_id": release_group_id,
            "matching_local_release_found": matching_local_release,
        },
        "score_breakdown": score_breakdown,
        "missing": missing,
    }
    decision_payload = {
        "title_match": _field(local_title, recording_title, title_score),
        "artist_match": _field(local_artist, recording_artist, artist_score),
        "album_match": _field(local_album, release_title, album_score),
        "year_match": {"status": year_status, "local": local_year, "suggested": release_year},
        "duration_match": {
            "status": duration_status,
            "delta_seconds": round(duration_delta, 1) if duration_delta is not None else None,
        },
        "release_group_match": {
            "status": release_group_status,
            "local": local_release_group_id,
            "suggested": release_group_id,
        },
        "position_match": {"status": position_status, "local": local_track, "suggested": suggested_track},
        "confidence_score": confidence_score,
        "confidence_tier": confidence_tier,
        "reason": reason,
        "conflicts": conflicts,
        "warnings": warnings,
        "review_required": bool(not attach_eligible or conflicts),
        "action_eligibility": {
            "attach_recording_id": attach_eligible,
            "submit_metadata": True,
            "destructive_use": False,
        },
        "eligibility_reason": eligibility_reason,
        "destructive_use_permitted": False,
        "recommended_action": recommended_action,
        "requires_confirmation": bool(conflicts),
        "safety_result": safety_result,
        "safety_key": safety_key,
    }
    return MatchingDecision(
        input=input_payload,
        identity=identity_payload,
        evidence=evidence_payload,
        ai=ai_payload,
        decision=decision_payload,
        candidate={
            "candidate_index": candidate.get("candidate_index", -1),
            "source": source,
            "match_method": _s(candidate.get("match_method") or source),
            "score": candidate.get("score") or 0,
            "duration": candidate.get("duration", ""),
            "mb_albumids": list(candidate.get("mb_albumids") or ([] if not release_id else [release_id])),
        },
    )
