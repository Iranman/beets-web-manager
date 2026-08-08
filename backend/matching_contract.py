from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
import hashlib
import json
import math
import re
import unicodedata


SimilarityFn = Callable[[str, str], float]

_MB_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Real field names produced by app.py's _score_track_ai_candidate(). Nothing
# outside this set is ever copied into browser-visible JSON, regardless of
# what a caller stuffs into candidate["_match_score"].
_ALLOWED_SCORE_FIELDS = {
    "title_score",
    "artist_score",
    "album_score",
    "year_score",
    "mb_score",
    "acoustid_bonus",
    "total",
    "source",
}
_ALLOWED_SCORE_STRING_FIELDS = {"source"}


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


def _finite_number(value: Any) -> Optional[float]:
    """Reject NaN/Infinity/non-numeric input so it can never reach JSON."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except Exception:
        return None
    if not math.isfinite(number):
        return None
    return number


def _number(value: Any) -> Optional[float]:
    return _finite_number(value)


def _duration_seconds(value: Any) -> Optional[float]:
    number = _finite_number(value)
    if number is not None and number > 0:
        return number / 1000.0 if number > 10000 else number
    text = _s(value)
    if not text:
        return None
    try:
        parts = [float(part) for part in text.split(":")]
    except Exception:
        return None
    if any(not math.isfinite(part) for part in parts):
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


def _safe_plain_text(value: Any, *, max_length: int = 256) -> str:
    """Strict plain-text sanitizer for externally sourced release fields.

    Only a real `str` is ever accepted -- mappings, lists, tuples, sets,
    bytes, numbers, booleans, and custom objects are dropped outright.
    Never calls str()/repr() on a rejected value, so a malformed object can
    never reach browser-visible JSON as its stringified or repr'd form.
    """
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    return text[:max_length]


# Practical, conservative bounds per release numeric field. Zero fails
# closed for all four -- no producer in this codebase legitimately emits a
# zero medium/track position, track count, or duration.
_MIN_MEDIUM_POSITION = 1
_MAX_MEDIUM_POSITION = 999
_MIN_TRACK_POSITION = 1
_MAX_TRACK_POSITION = 100_000
_MIN_TRACK_COUNT = 1
_MAX_TRACK_COUNT = 100_000
_MIN_DURATION_MS = 1
_MAX_DURATION_MS = 86_400_000  # 24 hours: generous bound for one audio track


def _safe_release_int(value: Any, *, minimum: int, maximum: int) -> Optional[int]:
    """Strict, field-bounded integer sanitizer for externally sourced
    release fields.

    Accepts only plain ints, finite integer-valued floats, and clean
    digit-only strings (optionally signed). Never calls str()/repr() on a
    rejected value -- mappings, lists, booleans, NaN/Infinity, and custom
    objects fail closed to None without ever being stringified. A value
    parsed outside [minimum, maximum] is also rejected to None; it is never
    clamped to the nearest bound.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        parsed = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text or not re.match(r"^-?\d+$", text):
            return None
        try:
            parsed = int(text)
        except Exception:
            return None
    else:
        return None
    if parsed < minimum or parsed > maximum:
        return None
    return parsed


def _round_score(value: Any) -> float:
    number = _finite_number(value)
    if number is None:
        return 0.0
    return round(max(0.0, min(1.0, number)), 3)


def _normalized_raw_score(candidate: Mapping[str, Any]) -> float:
    """Normalize candidate['score'] to 0..1. Callers decide whether this raw
    figure is allowed to mean anything -- see the fingerprint-provenance
    gating in build_recording_matching_decision. Never treat this alone as
    fingerprint evidence."""
    number = _finite_number(candidate.get("score"))
    if number is None:
        return 0.0
    return _round_score(number / 100.0 if number > 1 else number)


def _sanitize_score_breakdown(raw: Any) -> Dict[str, Any]:
    """Allowlist-only copy of candidate['_match_score']. Unknown keys
    (secrets, provider payloads, headers, nested objects) are dropped."""
    if not isinstance(raw, Mapping):
        return {}
    out: Dict[str, Any] = {}
    for key in _ALLOWED_SCORE_FIELDS:
        if key not in raw:
            continue
        value = raw[key]
        if key in _ALLOWED_SCORE_STRING_FIELDS:
            text = _s(value)
            if text and len(text) <= 32 and re.match(r"^[a-zA-Z0-9_.\-]+$", text):
                out[key] = text
            continue
        number = _finite_number(value)
        if number is not None:
            out[key] = round(number, 6)
    return out


_FINGERPRINT_MISMATCH_STATUSES = {"mismatch", "conflict", "rejected"}
_FINGERPRINT_VALID_STATUSES = {"matched", "verified"}


def _classify_fingerprint_provenance(
    *, attempted: bool, matched: bool, status: str, mapped_recording_id: str,
    acoustid_score: float, threshold: float = 0.8,
) -> str:
    """Classify AcoustID fingerprint evidence as one coherent state instead of
    trusting attempted/matched/status as independent truthy fields.
    Contradictory combinations (e.g. matched=True with attempted=False) fail
    closed -- they are never treated as verified evidence, regardless of
    what any individual field claims. Returns one of: verified,
    not_attempted, attempted_no_match, mismatch, invalid_provenance,
    incomplete."""
    if status in _FINGERPRINT_MISMATCH_STATUSES:
        return "mismatch"
    if attempted and matched and status in _FINGERPRINT_VALID_STATUSES:
        if mapped_recording_id and acoustid_score >= threshold:
            return "verified"
        return "incomplete"
    if not attempted and matched:
        return "invalid_provenance"
    if attempted and not matched and status in _FINGERPRINT_VALID_STATUSES:
        return "invalid_provenance"
    if not attempted and not matched and status in _FINGERPRINT_VALID_STATUSES:
        return "invalid_provenance"
    if attempted and not matched:
        return "attempted_no_match"
    return "not_attempted"


def _safe_string_list(value: Any, *, max_items: int = 12, max_length: int = 80) -> List[str]:
    """Allow only bounded, deduplicated scalar strings. Mappings, lists,
    bytes, numbers, and any other nested/non-string entry are dropped
    entirely rather than coerced."""
    if not isinstance(value, (list, tuple)):
        return []
    out: List[str] = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()[:max_length]
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= max_items:
            break
    return out


# Exact fields produced by app.py's _track_ai_release_match_score() -- the
# only real producer of release["local_match"]. Never a general "copy all
# scalar values" rule: unknown keys (secrets, provider payloads) are dropped.
_ALLOWED_LOCAL_MATCH_NUMERIC_FIELDS = {"album_score", "artist_score", "year_score", "total"}


def _safe_local_match(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    out: Dict[str, Any] = {}
    for key in _ALLOWED_LOCAL_MATCH_NUMERIC_FIELDS:
        number = _finite_number(value.get(key))
        if number is not None:
            out[key] = round(number, 4)
    if isinstance(value.get("year_match"), bool):
        out["year_match"] = value["year_match"]
    if "year_delta" in value:
        year_delta = value.get("year_delta")
        if year_delta is None:
            out["year_delta"] = None
        else:
            number = _finite_number(year_delta)
            if number is not None:
                out["year_delta"] = int(number)
    return out


def _safe_release(release: Mapping[str, Any]) -> Dict[str, Any]:
    release_id = _uuid(release.get("mb_albumid") or release.get("release_id"))
    release_group_id = _uuid(release.get("mb_releasegroupid") or release.get("release_group_id"))
    title = _safe_plain_text(release.get("album") or release.get("title"))
    safe_date = _safe_plain_text(release.get("date"), max_length=32)
    safe_year_text = _safe_plain_text(release.get("year"), max_length=16)
    year = _year(safe_year_text or safe_date)
    medium_position = _safe_release_int(
        release.get("medium_position"), minimum=_MIN_MEDIUM_POSITION, maximum=_MAX_MEDIUM_POSITION
    )
    track_number = _safe_plain_text(release.get("track_number") or release.get("track"), max_length=16)
    track_count = _safe_release_int(
        release.get("track_count") or release.get("tracks"), minimum=_MIN_TRACK_COUNT, maximum=_MAX_TRACK_COUNT
    )
    duration_ms = _safe_release_int(
        release.get("duration_ms"), minimum=_MIN_DURATION_MS, maximum=_MAX_DURATION_MS
    )
    disc_text = _safe_plain_text(release.get("disc"), max_length=16)
    if not disc_text and medium_position:
        disc_text = str(medium_position)
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
        "artist": _safe_plain_text(release.get("artist") or release.get("release_artist")),
        "date": safe_date,
        "year": year,
        "country": _safe_plain_text(release.get("country"), max_length=64),
        "status": _safe_plain_text(release.get("status"), max_length=64),
        "label": _safe_plain_text(release.get("label")),
        "medium_format": _safe_plain_text(release.get("medium_format") or release.get("media_format"), max_length=64),
        "media_format": _safe_plain_text(release.get("medium_format") or release.get("media_format"), max_length=64),
        "medium_position": medium_position,
        "disc": disc_text,
        "track_number": track_number,
        "track": track_number,
        "track_position": _safe_release_int(
            release.get("track_position"), minimum=_MIN_TRACK_POSITION, maximum=_MAX_TRACK_POSITION
        ),
        "tracktotal": _safe_plain_text(release.get("tracktotal"), max_length=16),
        "tracks": track_count,
        "track_count": track_count,
        "duration_ms": duration_ms,
        "release_group_primary_type": _safe_plain_text(release.get("release_group_primary_type"), max_length=64),
        "release_group_secondary_types": _safe_string_list(release.get("release_group_secondary_types")),
        "local_match": _safe_local_match(release.get("local_match")),
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
    """AI provenance for a single enrichment call.

    ``state_known=False`` (the default) means the caller did not evaluate AI
    at this boundary -- e.g. Import Review enriches every AcoustID/MusicBrainz
    candidate *before* the AI request has even been sent. Serializing
    configured/attempted/available as False in that situation would assert a
    fact ("AI is not configured") that is not actually known and may be
    false. Only pass a real AiState when configured/attempted/available and
    the AI contribution are genuinely known at the call site.
    """

    state_known: bool = False
    configured: bool = False
    attempted: bool = False
    available: bool = False
    unavailability_reason: str = ""
    contribution: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        if not self.state_known:
            return {
                "state_known": False,
                "status": "not_evaluated_at_this_boundary",
                "configured": None,
                "attempted": None,
                "available": None,
                "unavailability_reason": "",
                "contribution": {},
            }
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
            "state_known": True,
            "status": "evaluated",
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
            "contract_version": 2,
            "input": self.input,
            "identity": self.identity,
            "evidence": self.evidence,
            "ai": self.ai,
            "decision": self.decision,
            "warnings": list(self.decision.get("warnings") or []),
        }

    def to_review_recording_candidate(self) -> Dict[str, Any]:
        identity = self.identity
        decision = self.decision
        evidence = self.evidence
        recording = evidence.get("musicbrainz", {}).get("recording", {})
        release = evidence.get("musicbrainz", {}).get("selected_release", {})
        acoustid = evidence.get("acoustid", {})
        # Display continuity: show whatever recording ID the candidate
        # evaluated to even when identity is in conflict/unresolved -- the
        # *decision* fields (safety_key, conflicts, requires_confirmation)
        # are what prevent unsafe use, not hiding the ID.
        display_recording_id = _s(
            identity.get("resolved_recording_id")
            or identity.get("evaluated_candidate_recording_id")
            or next(iter(identity.get("recording_ids") or []), "")
        )
        release_id = _s(identity.get("release_id") or "")
        release_group_id = _s(identity.get("release_group_id") or "")
        source = _s(self.candidate.get("source") or self.candidate.get("match_method") or "mb")
        if source == "musicbrainz":
            source = "mb"
        confidence_score = decision.get("confidence_score")
        return {
            "candidate_index": self.candidate.get("candidate_index", -1),
            "candidate_type": "recording",
            "mb_trackid": display_recording_id,
            "mb_url": f"https://musicbrainz.org/recording/{display_recording_id}" if display_recording_id else "",
            "musicbrainz_url": f"https://musicbrainz.org/recording/{display_recording_id}" if display_recording_id else "",
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
            "track_number": _s(identity.get("track_number") or release.get("track_number")),
            "medium_position": identity.get("medium_position"),
            "duration": _s(self.candidate.get("duration")),
            "source": source,
            "match_method": source,
            "score": self.candidate.get("score") or 0,
            "match_total": confidence_score,
            "confidence": _s(decision.get("confidence_tier")),
            "confidence_score": confidence_score,
            "score_breakdown": evidence.get("score_breakdown", {}),
            "decision": decision,
            "conflicts": list(decision.get("conflicts") or []),
            "warnings": list(decision.get("warnings") or []),
            "review_required": bool(decision.get("review_required")),
            "action_eligibility": decision.get("action_eligibility") or {},
            "eligibility_reason": _s(decision.get("eligibility_reason") or ""),
            "recommended_action": _s(decision.get("recommended_action")),
            "requires_confirmation": bool(decision.get("requires_confirmation")),
            "safety_result": _s(decision.get("safety_result")),
            "safety_key": _s(decision.get("safety_key")),
            "reason": _s(decision.get("reason")),
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
            "linked_releases": evidence.get("musicbrainz", {}).get("linked_releases", [])[:12],
            "same_recording_release_count": int(
                evidence.get("musicbrainz", {}).get("same_recording_release_count") or 0
            ),
            "matching_local_release_found": bool(
                evidence.get("release_group", {}).get("matching_local_release_found")
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
    library_identity_verified: bool = False,
    extra_conflicts: Sequence[str] = (),
) -> MatchingDecision:
    """Build the authoritative recording-candidate decision for Import Review.

    The serializer is intentionally additive: it preserves existing
    ReviewRecordingCandidate keys while keeping release-group identity distinct
    from release evidence, deterministic Recording ID sources distinct from
    AI's opinion, and verified AcoustID evidence distinct from any other
    numeric score.
    """
    current = current if isinstance(current, Mapping) else {}
    candidate = candidate if isinstance(candidate, Mapping) else {}
    details = details if isinstance(details, Mapping) else {}
    raw_selected_release = (
        selected_release
        if selected_release is not None
        else (candidate.get("selected_release") or details.get("selected_release") or {})
    )
    raw_linked_releases = (
        linked_releases
        if linked_releases is not None
        else (candidate.get("linked_releases") or details.get("linked_releases") or [])
    )
    if isinstance(raw_linked_releases, (str, bytes)) or not isinstance(raw_linked_releases, Sequence):
        raw_linked_releases = []
    similarity = similarity_fn or _similarity

    score_breakdown = _sanitize_score_breakdown(candidate.get("_match_score"))
    source = _s(candidate.get("source") or candidate.get("match_method") or score_breakdown.get("source") or "mb").lower()
    release = _safe_release(raw_selected_release if isinstance(raw_selected_release, Mapping) else {})
    safe_linked = _safe_linked_releases(raw_linked_releases)

    # ---- Recording ID provenance: every source collected independently ----
    candidate_recording_id = _uuid(candidate.get("mb_trackid"))
    details_recording_id = _uuid(details.get("recording_id"))
    existing_recording_id = _uuid(current.get("mb_trackid"))

    fingerprint_attempted = bool(candidate.get("fingerprint_attempted"))
    fingerprint_matched = bool(candidate.get("fingerprint_matched"))
    fingerprint_status = _s(
        candidate.get("fingerprint_status")
        or candidate.get("acoustid_status")
        or candidate.get("acoustid_verification")
    ).lower()
    # Never synthesized from candidate/details/local/AI Recording IDs -- only
    # the explicit AcoustID lookup result may populate this.
    acoustid_mapped_recording_id = _uuid(candidate.get("mapped_recording_id"))
    acoustid_id_value = _s(candidate.get("acoustid_id"))

    ai_state = ai_state if ai_state is not None else AiState()
    ai_payload = ai_state.to_dict()
    ai_recording_id = _uuid((ai_payload.get("contribution") or {}).get("mb_trackid"))

    deterministic_ids = {
        name: rid
        for name, rid in (("candidate", candidate_recording_id), ("musicbrainz_details", details_recording_id))
        if rid
    }
    distinct_deterministic = set(deterministic_ids.values())
    recording_id_source_conflict = len(distinct_deterministic) > 1
    resolved_recording_id = "" if recording_id_source_conflict else next(iter(distinct_deterministic), "")
    recording_id = resolved_recording_id

    # ---- Release / release-group ID provenance: independent sources ----
    candidate_release_id = _uuid(candidate.get("mb_albumid") or next(iter(candidate.get("mb_albumids") or []), ""))
    details_release_id = _uuid(details.get("mb_albumid"))
    selected_release_id = _uuid(release.get("release_id"))
    candidate_rg_id = _uuid(candidate.get("mb_releasegroupid"))
    details_rg_id = _uuid(details.get("mb_releasegroupid"))
    selected_rg_id = _uuid(release.get("release_group_id"))

    rg_values = {v for v in (candidate_rg_id, details_rg_id, selected_rg_id) if v}
    release_group_source_conflict = len(rg_values) > 1
    release_group_id = "" if release_group_source_conflict else next(iter(rg_values), "")
    # Release IDs may legitimately differ across sources (same recording on
    # several editions) -- prefer the most specific (selected) value for
    # display/decision, but expose every source untouched for provenance.
    release_id = selected_release_id or details_release_id or candidate_release_id

    linked_release_ids = {r.get("release_id") for r in safe_linked if r.get("release_id")}
    selected_release_not_linked = bool(
        selected_release_id and linked_release_ids and selected_release_id not in linked_release_ids
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
        or release.get("artist")
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

    selected_track_number = release.get("track") or release.get("track_number")
    track_number = _s(release.get("track_number") or selected_track_number)
    # Prefer the dedicated, bounded numeric field; fall back to parsing the
    # display track_number string when no explicit position was supplied.
    # A malformed track_position (e.g. negative or absurdly large) is
    # already None here -- _safe_release() rejected it -- so it correctly
    # leaves position evidence "unknown" rather than fabricating a conflict.
    suggested_track = release.get("track_position")
    if suggested_track is None:
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
    if not resolved_recording_id:
        missing.append("recording_id_missing")
    if not release_group_id:
        missing.append("release_group_id_missing")
    if not release_id:
        missing.append("release_id_missing")
    if not safe_linked:
        missing.append("linked_releases_missing")

    warnings: List[str] = []
    if not release_group_id:
        warnings.append("release_group_id_missing")
    identity_recording_id_for_ai_check = resolved_recording_id or candidate_recording_id
    if ai_recording_id and identity_recording_id_for_ai_check and ai_recording_id != identity_recording_id_for_ai_check:
        warnings.append("ai_recording_conflict")

    # ---- Fingerprint (AcoustID) evidence: coherent state, not independent
    # truthy fields. A candidate claiming matched=True without attempted=True
    # (or any other internally contradictory combination) fails closed --
    # it is classified, never trusted at face value.
    raw_normalized_score = _normalized_raw_score(candidate)
    fingerprint_state = _classify_fingerprint_provenance(
        attempted=fingerprint_attempted,
        matched=fingerprint_matched,
        status=fingerprint_status,
        mapped_recording_id=acoustid_mapped_recording_id,
        acoustid_score=raw_normalized_score,
    )
    if fingerprint_state == "verified":
        acoustid_score = raw_normalized_score
        musicbrainz_search_score = 0.0
    elif source == "acoustid":
        # Claimed AcoustID origin without confirmed verification: never
        # treated as fingerprint evidence, and not repurposed as MB
        # relevance either -- the number's meaning is unconfirmed.
        acoustid_score = 0.0
        musicbrainz_search_score = 0.0
    else:
        acoustid_score = 0.0
        musicbrainz_search_score = raw_normalized_score
    heuristic_score = _round_score(score_breakdown.get("total"))
    confidence_score = _round_score(max(heuristic_score, acoustid_score))

    # The AcoustID mapping is compared against the full resolved deterministic
    # Recording ID (candidate + MusicBrainz details), not the candidate alone.
    # When candidate/details already disagree, resolved_recording_id is ""
    # and this comparison is skipped -- AcoustID must never pick a winner
    # for that disagreement.
    fingerprint_recording_id_conflict = bool(
        fingerprint_state == "verified"
        and resolved_recording_id
        and acoustid_mapped_recording_id
        and acoustid_mapped_recording_id != resolved_recording_id
    )
    existing_recording_conflict = bool(
        existing_recording_id and resolved_recording_id and existing_recording_id != resolved_recording_id
    )

    strong_acoustid = bool(
        fingerprint_state == "verified"
        and not fingerprint_recording_id_conflict
        and not recording_id_source_conflict
        and resolved_recording_id
    )

    # Missing evidence is neither positive nor negative corroboration --
    # only "yes"/"tolerance" (duration) or "yes" (position) may downgrade a
    # title mismatch. "unknown" must never substitute for real agreement.
    duration_supports = duration_status in {"yes", "tolerance"}
    position_supports = position_status == "yes"
    title_only_strong = bool(
        title_score < 0.82
        and strong_acoustid
        and artist_score >= 0.72
        and not existing_recording_conflict
        and (duration_supports or position_supports)
    )

    conflicts: List[str] = []
    if fingerprint_state == "mismatch":
        conflicts.append("fingerprint_conflict")
    if fingerprint_state == "invalid_provenance":
        conflicts.append("fingerprint_provenance_conflict")
    if fingerprint_recording_id_conflict:
        conflicts.append("fingerprint_recording_id_conflict")
    if recording_id_source_conflict:
        conflicts.append("recording_id_source_conflict")
    if existing_recording_conflict:
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
    if release_group_source_conflict:
        conflicts.append("release_group_id_source_conflict")
    if selected_release_not_linked:
        conflicts.append("selected_release_not_linked")
    # Caller-supplied conflicts (e.g. a playlist adapter's own competing-
    # candidate-tie check) fold into the same conflicts list the rest of
    # this function reasons about, so they participate in the safety-key
    # ladder and the decision-version hash identically to every built-in
    # conflict -- never bypassed or applied after the fact.
    for name in extra_conflicts:
        name = _s(name).strip()
        if name and name not in conflicts:
            conflicts.append(name)
    if fingerprint_state == "incomplete" and fingerprint_attempted and fingerprint_matched and not acoustid_mapped_recording_id:
        warnings.append("acoustid_mapped_recording_id_missing")
    if title_only_strong:
        warnings.append("title_mismatch_with_strong_recording_evidence")
    if len(safe_linked) > 1:
        warnings.append("same_recording_on_multiple_releases")

    evidence_supported = bool(strong_acoustid or confidence_score >= 0.78)
    hard_conflict_names = {
        "fingerprint_conflict",
        "fingerprint_provenance_conflict",
        "fingerprint_recording_id_conflict",
        "recording_id_source_conflict",
        "recording_id_conflict",
        "title_conflict",
        "artist_conflict",
        "release_group_conflict",
    }
    has_hard_conflict = any(c in hard_conflict_names for c in conflicts)
    attach_eligible = bool(
        resolved_recording_id
        and evidence_supported
        and not conflicts
        and release_group_id
        and not selected_release_not_linked
        and ((title_score >= 0.82 and artist_score >= 0.72) or title_only_strong)
    )
    # Playlist-only bypass: an existing Beets library item can be safely
    # bound to a playlist slot purely on a deterministic artist+title match
    # -- it never mutates the item's own tags, so it doesn't need a
    # MusicBrainz Recording ID or release-group evidence the way attaching
    # one does. `library_identity_verified` is only ever True when the
    # caller (the playlist adapter) has already confirmed the candidate
    # item still exists in the library; it defaults False and is never set
    # by Import Review, so `attach_eligible`/`attach_without_review` below
    # are computed exactly as before for every existing caller.
    library_eligible = bool(
        library_identity_verified
        and not conflicts
        and title_score >= 0.94
        and artist_score >= 0.90
    )

    if not resolved_recording_id and not library_eligible:
        safety_result = "No verified match"
        safety_key = "none"
        confidence_tier = "low"
        recommended_action = "Search MusicBrainz manually"
        eligibility_reason = (
            "Deterministic Recording ID sources disagree; resolve before attaching."
            if recording_id_source_conflict
            else "No MusicBrainz Recording ID is available."
        )
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
    elif resolved_recording_id and not release_group_id and not library_eligible:
        safety_result = "Needs review"
        safety_key = "review"
        confidence_tier = "medium"
        recommended_action = "Attach Recording ID only after review"
        eligibility_reason = "Only recording identity is supported; release-group identity is missing."
    elif attach_eligible or library_eligible:
        safety_result = "Safe to attach" if attach_eligible else "Safe (existing library identity)"
        safety_key = "safe"
        confidence_tier = "high"
        recommended_action = "Attach Recording ID" if attach_eligible else "Use existing library item"
        eligibility_reason = (
            "Recording ID can be attached from deterministic evidence without additional review."
            if attach_eligible else
            "Existing Beets library item deterministically matches artist and title; "
            "no MusicBrainz Recording ID change is required."
        )
    else:
        safety_result = "Needs review"
        safety_key = "review"
        confidence_tier = "medium"
        recommended_action = "Use this candidate after review"
        eligibility_reason = "Candidate needs human review before attaching Recording ID."

    review_required = safety_key != "safe"
    requires_confirmation = review_required
    # Scoped to the recording-ID path specifically (never to the
    # playlist-only library-identity bypass) -- safety_key can reach "safe"
    # via `library_eligible` with no resolved Recording ID at all, and
    # "attach_without_review" must never assert it's safe to attach an ID
    # that doesn't exist. For every existing (non-playlist) caller this is
    # exactly equivalent to the prior `safety_key == "safe"` computation,
    # since library_eligible is always False there.
    attach_without_review = bool(attach_eligible)

    reason = _s(candidate.get("reason"))
    if not reason:
        if source == "acoustid" and fingerprint_matched:
            reason = "AcoustID fingerprint matched this recording; linked release context is ranked against local tags."
        elif source == "acoustid":
            reason = "AcoustID-labeled candidate without confirmed fingerprint verification."
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
        "recording_ids": [resolved_recording_id] if resolved_recording_id else [],
        "resolved_recording_id": resolved_recording_id,
        "evaluated_candidate_recording_id": candidate_recording_id,
        "recording_id_sources": {
            "candidate": candidate_recording_id,
            "musicbrainz_details": details_recording_id,
            "local_existing": existing_recording_id,
            "acoustid": acoustid_mapped_recording_id,
            "ai": ai_recording_id,
        },
        "recording_id_source_conflict": recording_id_source_conflict,
        "release_identity_sources": {
            "candidate_release_id": candidate_release_id,
            "details_release_id": details_release_id,
            "selected_release_id": selected_release_id,
            "candidate_release_group_id": candidate_rg_id,
            "details_release_group_id": details_rg_id,
            "selected_release_group_id": selected_rg_id,
        },
        "release_group_source_conflict": release_group_source_conflict,
        "selected_release_not_linked": selected_release_not_linked,
        "medium_position": release.get("medium_position"),
        "track_number": track_number,
    }
    musicbrainz_payload = {
        "recording": {
            "id": resolved_recording_id,
            "title": recording_title,
            "artist_credit": recording_artist,
            "url": f"https://musicbrainz.org/recording/{resolved_recording_id}" if resolved_recording_id else "",
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
            "fingerprint_attempted": fingerprint_attempted,
            "fingerprint_matched": fingerprint_matched,
            "status": fingerprint_status or ("not_attempted" if not fingerprint_attempted else "no_result"),
            "provenance_state": fingerprint_state,
            "score": acoustid_score,
            "acoustid_id": acoustid_id_value,
            "mapped_recording_id": acoustid_mapped_recording_id,
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
        "acoustid_score": acoustid_score,
        "musicbrainz_search_score": musicbrainz_search_score,
        "heuristic_score": heuristic_score,
        "confidence_score": confidence_score,
        "confidence_tier": confidence_tier,
        "reason": reason,
        "conflicts": conflicts,
        "warnings": warnings,
        "review_required": review_required,
        "action_eligibility": {
            "attach_without_review": attach_without_review,
            "playlist_resolve_without_review": bool(attach_without_review or library_eligible),
            "destructive_use": False,
        },
        "eligibility_reason": eligibility_reason,
        "destructive_use_permitted": False,
        "recommended_action": recommended_action,
        "requires_confirmation": requires_confirmation,
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
            "score": _finite_number(candidate.get("score")) or 0,
            "duration": candidate.get("duration", ""),
            "mb_albumids": list(candidate.get("mb_albumids") or ([] if not release_id else [release_id])),
        },
    )


def compute_decision_version(item_id: Any, decision: "MatchingDecision") -> str:
    """Server-generated fingerprint of one candidate's matching decision.

    Derived only from stable, already-sanitized decision fields (never
    secrets, raw provider payloads, unstable timestamps, candidate display
    order, or transient UI-only state) so a caller can detect that the
    displayed decision is stale by the time an attach request arrives --
    it proves the decision hasn't changed, it never grants authority on
    its own.

    Schema 2 (`drv2:`) widens the fingerprinted surface to (at least) the
    full set of trusted evidence actually shown to the user and relied on
    by the confirmed-review attach path: identity fields, conflicts/
    warnings, review/confirmation flags, safety key, confidence score,
    action eligibility, eligibility reason, candidate source, and
    deterministic AcoustID fingerprint provenance. Schema 1 (`drv1:`)
    omitted enough of that evidence that a stale decision could still pass
    the version check. A `drv1:` value submitted against this schema will
    simply never equal a freshly computed `drv2:` value, so it is
    correctly rejected as stale by the caller's own version comparison --
    no separate schema-detection code is required.
    """
    identity = decision.identity
    d = decision.decision
    evidence = decision.evidence if isinstance(decision.evidence, dict) else {}
    acoustid = evidence.get("acoustid") or {}
    candidate = decision.candidate if isinstance(decision.candidate, dict) else {}
    action_eligibility = d.get("action_eligibility") or {}
    payload = {
        "schema": 2,
        "contract_version": 2,
        "item_id": _s(item_id),
        "resolved_recording_id": _s(identity.get("resolved_recording_id")),
        "evaluated_candidate_recording_id": _s(identity.get("evaluated_candidate_recording_id")),
        "release_id": _s(identity.get("release_id")),
        "release_group_id": _s(identity.get("release_group_id")),
        "conflicts": sorted(d.get("conflicts") or []),
        "warnings": sorted(d.get("warnings") or []),
        "review_required": bool(d.get("review_required")),
        "requires_confirmation": bool(d.get("requires_confirmation")),
        "safety_key": _s(d.get("safety_key")),
        "confidence_score": _finite_number(d.get("confidence_score")),
        "attach_without_review": bool(action_eligibility.get("attach_without_review")),
        "playlist_resolve_without_review": bool(action_eligibility.get("playlist_resolve_without_review")),
        "destructive_use": bool(action_eligibility.get("destructive_use")),
        "eligibility_reason": _s(d.get("eligibility_reason")),
        "candidate_source": _s(candidate.get("source")),
        "fingerprint_attempted": bool(acoustid.get("fingerprint_attempted")),
        "fingerprint_matched": bool(acoustid.get("fingerprint_matched")),
        "fingerprint_status": _s(acoustid.get("status")),
        "mapped_recording_id": _s(acoustid.get("mapped_recording_id")),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]
    return f"drv2:{digest}"
