"""Canonical single-recording evaluator (ARCH-002).

Evaluates ONE local audio track against ONE candidate MusicBrainz recording
and returns the final identity/safety decision. Album-level evaluation lives
in ``evidence.evaluate_release_group_candidate``; this module is its
recording-level counterpart and shares the same vocabulary
(``AcoustIDStatus``, ``ConfidenceState``) and the same AcoustID hit
semantics as ``track_alignment`` (score >= 80, ambiguity window of 3 points).

Permanent identity rules enforced here:

* MusicBrainz Recording ID is canonical track identity.
* An existing embedded Recording ID that equals the candidate is
  deterministic evidence; one that differs is a hard conflict.
* An AcoustID fingerprint confirming the candidate is deterministic
  evidence; one confirming a *different* recording is a hard conflict.
* AcoustID no-result / unavailable are never conflicts.
* An ambiguous AcoustID result requires review unless an embedded
  Recording ID match independently resolves identity.
* Text similarity (title/artist/album/filename/duration/position) is
  supporting evidence only. It never overrides a deterministic conflict and
  never authorizes an unattended attach by itself.
* AI opinion is never identity evidence; disagreement is a warning.

Callers (``backend.matching_contract.build_recording_matching_decision``)
sanitize and collect provider fields; every final decision field
(attach eligibility, safety key, confidence state, conflicts, review
reasons) comes from ``evaluate_recording_candidate``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from .models import AcoustIDStatus, ConfidenceState
from .normalize import similarity as _default_similarity
from .track_alignment import _acoustid_status_for_target, _hit_recording_id, _hit_score

SimilarityFn = Callable[[str, str], float]

#: AcoustID hit score floor (0..100 scale) shared with track_alignment.
ACOUSTID_MIN_SCORE = 80.0

# Text-evidence thresholds. These classify supporting evidence and
# conflicts; none of them authorizes action on its own.
TITLE_STRONG = 0.82
TITLE_CONFLICT_BELOW = 0.68
ARTIST_STRONG = 0.72
ARTIST_CONFLICT_BELOW = 0.68
ALBUM_CONFLICT_BELOW = 0.55
DURATION_MATCH_SECONDS = 4.0
DURATION_TOLERANCE_SECONDS = 10.0


class RecordingIdentityProof(str, Enum):
    """How the candidate Recording ID's identity with the local audio was
    established. Only the three deterministic values can authorize an
    unattended attach; TEXTUAL_SUPPORT is real evidence for ranking and
    review, never proof."""

    INSUFFICIENT = "insufficient"
    TEXTUAL_SUPPORT = "textual_support"
    EMBEDDED_RECORDING_ID = "embedded_recording_id"
    ACOUSTID_RECORDING_ID = "acoustid_recording_id"
    MULTI_SOURCE_DETERMINISTIC = "multi_source_deterministic"

    @property
    def deterministic(self) -> bool:
        return self in _DETERMINISTIC_PROOFS


_DETERMINISTIC_PROOFS = frozenset({
    RecordingIdentityProof.EMBEDDED_RECORDING_ID,
    RecordingIdentityProof.ACOUSTID_RECORDING_ID,
    RecordingIdentityProof.MULTI_SOURCE_DETERMINISTIC,
})


#: Conflicts that make a candidate unusable until resolved ("Conflict").
#: Every other conflict code is review-only ("Needs review").
HARD_CONFLICTS = frozenset({
    "fingerprint_conflict",
    "fingerprint_provenance_conflict",
    "fingerprint_recording_id_conflict",
    "recording_id_source_conflict",
    "recording_id_conflict",
    "title_conflict",
    "artist_conflict",
    "release_group_conflict",
})


@dataclass(frozen=True)
class AcoustIDRecordingEvidence:
    """Fingerprint evidence for one local file relative to one candidate.

    ``status`` is the canonical five-state value. ``recording_id`` is the
    recording AcoustID actually pointed at (the candidate when CONFIRMED,
    the competing recording when CONFLICT). ``provenance`` preserves the
    legacy per-candidate classification label for display/versioning.
    """

    status: AcoustIDStatus = AcoustIDStatus.UNAVAILABLE
    recording_id: str = ""
    score: float = 0.0
    provenance: str = "not_attempted"
    provenance_conflict: bool = False
    source: str = "none"

    @property
    def confirmed(self) -> bool:
        return self.status == AcoustIDStatus.CONFIRMED and not self.provenance_conflict

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "recording_id": self.recording_id,
            "score": round(float(self.score), 3),
            "provenance": self.provenance,
            "provenance_conflict": self.provenance_conflict,
            "source": self.source,
        }


def _norm_id(value: Any) -> str:
    return str(value or "").strip().lower()


def acoustid_evidence_from_hits(
    hits: Optional[Sequence[Mapping[str, Any]]],
    candidate_recording_id: str,
) -> AcoustIDRecordingEvidence:
    """Classify a real AcoustID lookup result set against one candidate.

    ``hits is None`` means no lookup happened (UNAVAILABLE); an empty list
    means the lookup returned nothing (NO_RESULT). Uses exactly the same
    per-target classification as album track alignment.
    """
    if hits is None:
        return AcoustIDRecordingEvidence(status=AcoustIDStatus.UNAVAILABLE, provenance="not_attempted", source="hits")
    rows = [dict(hit) for hit in hits if isinstance(hit, Mapping)]
    target = _norm_id(candidate_recording_id)
    status, _positives, _conflicts = _acoustid_status_for_target({"acoustid_hits": rows}, target)
    scored = sorted(
        ((_hit_recording_id(hit), _hit_score(hit)) for hit in rows if _hit_recording_id(hit)),
        key=lambda row: (-row[1], row[0]),
    )
    recording_id = ""
    score = 0.0
    if status == AcoustIDStatus.CONFIRMED:
        recording_id = target
        score = max((s for rid, s in scored if rid == target), default=0.0)
    elif status in (AcoustIDStatus.CONFLICT, AcoustIDStatus.AMBIGUOUS) and scored:
        recording_id, score = scored[0]
    provenance = {
        AcoustIDStatus.CONFIRMED: "verified",
        AcoustIDStatus.CONFLICT: "mismatch",
        AcoustIDStatus.AMBIGUOUS: "ambiguous",
        AcoustIDStatus.NO_RESULT: "attempted_no_match",
        AcoustIDStatus.UNAVAILABLE: "not_attempted",
    }[status]
    return AcoustIDRecordingEvidence(
        status=status,
        recording_id=recording_id,
        score=round(score / 100.0, 3),
        provenance=provenance,
        source="hits",
    )


_FINGERPRINT_MISMATCH_STATUSES = {"mismatch", "conflict", "rejected"}
_FINGERPRINT_VALID_STATUSES = {"matched", "verified", "confirmed"}


def acoustid_evidence_from_claims(
    *,
    attempted: bool,
    matched: bool,
    status: str,
    mapped_recording_id: str,
    score: float,
    candidate_recording_id: str,
    threshold: float = ACOUSTID_MIN_SCORE / 100.0,
) -> AcoustIDRecordingEvidence:
    """Classify per-candidate fingerprint *claims* (attempted/matched/status/
    mapped_recording_id) as one coherent state. Used when the caller has no
    raw hit set. Contradictory combinations fail closed as a provenance
    conflict -- never as verified evidence."""
    status = (status or "").strip().lower()
    mapped = _norm_id(mapped_recording_id)
    target = _norm_id(candidate_recording_id)
    if status in _FINGERPRINT_MISMATCH_STATUSES:
        return AcoustIDRecordingEvidence(AcoustIDStatus.CONFLICT, mapped, score, "mismatch", False, "claims")
    if status == "ambiguous":
        return AcoustIDRecordingEvidence(AcoustIDStatus.AMBIGUOUS, mapped, score, "ambiguous", False, "claims")
    if attempted and matched and status in _FINGERPRINT_VALID_STATUSES:
        if mapped and score >= threshold:
            if target and mapped != target:
                return AcoustIDRecordingEvidence(AcoustIDStatus.CONFLICT, mapped, score, "verified", False, "claims")
            return AcoustIDRecordingEvidence(AcoustIDStatus.CONFIRMED, mapped, score, "verified", False, "claims")
        return AcoustIDRecordingEvidence(AcoustIDStatus.AMBIGUOUS, mapped, 0.0, "incomplete", False, "claims")
    if (not attempted and matched) or (status in _FINGERPRINT_VALID_STATUSES and not (attempted and matched)):
        return AcoustIDRecordingEvidence(AcoustIDStatus.UNAVAILABLE, "", 0.0, "invalid_provenance", True, "claims")
    if attempted and not matched:
        return AcoustIDRecordingEvidence(AcoustIDStatus.NO_RESULT, "", 0.0, "attempted_no_match", False, "claims")
    return AcoustIDRecordingEvidence(AcoustIDStatus.UNAVAILABLE, "", 0.0, "not_attempted", False, "claims")


_VERSION_MARKERS = {
    "live": "live",
    "unplugged": "live",
    "remix": "remix",
    "mix": "remix",
    "rmx": "remix",
    "demo": "demo",
    "acoustic": "acoustic",
    "instrumental": "instrumental",
    "karaoke": "instrumental",
    "edit": "edit",
    "extended": "edit",
}
_WORD_RE = re.compile(r"[a-z]+")


def version_markers(title: str) -> frozenset:
    """Recording-version qualifiers in a raw title (live/remix/demo/...).
    Title normalization intentionally strips these, so they are compared
    here as separate evidence. Remaster/mono/stereo are ignored: MusicBrainz
    normally keeps those on the same recording."""
    words = _WORD_RE.findall((title or "").casefold())
    return frozenset(_VERSION_MARKERS[w] for w in words if w in _VERSION_MARKERS)


_FILENAME_PREFIX_RE = re.compile(r"^\s*\d{1,3}\s*[-_.)\s]\s*")


def filename_title(filename: str) -> str:
    stem = (filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    stem = _FILENAME_PREFIX_RE.sub("", stem).strip()
    if " - " in stem:
        stem = stem.split(" - ")[-1].strip()
    return stem


@dataclass
class RecordingMatchResult:
    candidate_recording_id: str
    existing_recording_id: str
    acoustid: AcoustIDRecordingEvidence
    identity_proof: RecordingIdentityProof
    state: ConfidenceState
    safety_key: str
    title_score: float = 0.0
    artist_score: float = 0.0
    album_score: float = 0.0
    filename_score: float = 0.0
    duration_status: str = "unknown"
    duration_delta: Optional[float] = None
    position_status: str = "unknown"
    year_status: str = "unknown"
    release_group_status: str = "unknown"
    confidence_score: float = 0.0
    title_mismatch_overridden: bool = False
    positives: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    review_reasons: List[str] = field(default_factory=list)
    eligibility_reason: str = ""

    @property
    def hard_conflicts(self) -> List[str]:
        return [c for c in self.conflicts if c in HARD_CONFLICTS]

    @property
    def attach_eligible(self) -> bool:
        return self.safety_key == "safe"

    def can_auto_attach(self) -> bool:
        """The single authority for attaching this Recording ID to the local
        file without human confirmation. Never true without deterministic
        identity proof and never true with any conflict."""
        return bool(
            self.safety_key == "safe"
            and self.identity_proof.deterministic
            and not self.conflicts
        )

    def identity_established(self) -> bool:
        """The local audio IS this recording: deterministic proof and no
        hard conflict. Independent of attach readiness (release-group
        context, review-only field disagreements), for workflows that need
        only recording identity."""
        return bool(self.candidate_recording_id and self.identity_proof.deterministic and not self.hard_conflicts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_recording_id": self.candidate_recording_id,
            "existing_recording_id": self.existing_recording_id,
            "acoustid": self.acoustid.to_dict(),
            "identity_proof": self.identity_proof.value,
            "confidence_state": self.state.value,
            "safety_key": self.safety_key,
            "confidence_score": round(float(self.confidence_score), 3),
            "positives": list(self.positives),
            "conflicts": list(self.conflicts),
            "hard_conflicts": self.hard_conflicts,
            "warnings": list(self.warnings),
            "review_reasons": list(self.review_reasons),
            "action_eligibility": {
                "attach_without_review": self.can_auto_attach(),
                "destructive_use": False,
            },
            "eligibility_reason": self.eligibility_reason,
        }


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except Exception:
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _add(target: List[str], code: str) -> None:
    if code and code not in target:
        target.append(code)


def evaluate_recording_candidate(
    local: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    acoustid: Optional[AcoustIDRecordingEvidence] = None,
    recording_id_source_conflict: bool = False,
    release_group_source_conflict: bool = False,
    selected_release_not_linked: bool = False,
    ai_recording_id: str = "",
    heuristic_score: float = 0.0,
    similarity_fn: Optional[SimilarityFn] = None,
) -> RecordingMatchResult:
    """Evaluate one local track against one candidate recording.

    ``local`` keys: title, artist, albumartist, album, year, track,
    duration_seconds, filename, recording_id (existing embedded
    mb_trackid), release_group_id (existing embedded).

    ``candidate`` keys: recording_id (resolved from deterministic MB
    sources; "" when sources disagree -- pass recording_id_source_conflict),
    title, artist, release_title, release_year, duration_seconds,
    track_position, release_group_id, release_id.

    ``heuristic_score`` is a 0..1 display/ranking figure only; it never
    changes the decision.
    """
    sim = similarity_fn or _default_similarity
    local = local if isinstance(local, Mapping) else {}
    candidate = candidate if isinstance(candidate, Mapping) else {}
    acoustid = acoustid or AcoustIDRecordingEvidence()

    rec_id = _norm_id(candidate.get("recording_id"))
    existing_id = _norm_id(local.get("recording_id"))
    local_title = str(local.get("title") or "")
    local_artist = str(local.get("artist") or local.get("albumartist") or "")
    local_album_artist = str(local.get("albumartist") or "")
    local_album = str(local.get("album") or "")
    local_year = str(local.get("year") or "")
    local_filename = str(local.get("filename") or "")
    rec_title = str(candidate.get("title") or "")
    rec_artist = str(candidate.get("artist") or "")
    release_title = str(candidate.get("release_title") or "")
    release_year = str(candidate.get("release_year") or "")

    positives: List[str] = []
    conflicts: List[str] = []
    warnings: List[str] = []
    review: List[str] = []

    # ---- text evidence (supporting only) ----
    title_score = sim(local_title, rec_title) if local_title and rec_title else 0.0
    artist_score = max(
        sim(local_artist, rec_artist) if local_artist and rec_artist else 0.0,
        sim(local_album_artist, rec_artist) if local_album_artist and rec_artist else 0.0,
    )
    album_score = sim(local_album, release_title) if local_album and release_title else 0.0
    fn_title = filename_title(local_filename)
    filename_score = sim(fn_title, rec_title) if fn_title and rec_title else 0.0

    local_duration = _num(local.get("duration_seconds"))
    rec_duration = _num(candidate.get("duration_seconds"))
    duration_delta: Optional[float] = None
    duration_status = "unknown"
    if local_duration and rec_duration and local_duration > 0 and rec_duration > 0:
        duration_delta = abs(local_duration - rec_duration)
        if duration_delta <= DURATION_MATCH_SECONDS:
            duration_status = "yes"
        elif duration_delta <= DURATION_TOLERANCE_SECONDS:
            duration_status = "tolerance"
        else:
            duration_status = "conflict"

    year_status = "unknown"
    if local_year and release_year:
        year_status = "yes" if local_year == release_year else "conflict"

    local_rgid = _norm_id(local.get("release_group_id"))
    cand_rgid = _norm_id(candidate.get("release_group_id"))
    release_group_status = "unknown"
    if local_rgid and cand_rgid:
        release_group_status = "yes" if local_rgid == cand_rgid else "conflict"

    local_track = local.get("track")
    cand_track = candidate.get("track_position")
    position_status = "unknown"
    if isinstance(local_track, int) and isinstance(cand_track, int):
        position_status = "yes" if local_track == cand_track else "conflict"

    # ---- deterministic identity evidence ----
    embedded_match = bool(existing_id and rec_id and existing_id == rec_id)
    existing_conflict = bool(existing_id and rec_id and existing_id != rec_id)
    acoustid_confirms = bool(acoustid.confirmed and rec_id and acoustid.recording_id in ("", rec_id))
    acoustid_contradicts = bool(
        acoustid.status == AcoustIDStatus.CONFLICT
        or (acoustid.confirmed and rec_id and acoustid.recording_id and acoustid.recording_id != rec_id)
    )
    if embedded_match:
        positives.append("embedded_recording_id_matches")
    if acoustid_confirms and not acoustid_contradicts:
        positives.append("acoustid_recording_confirmed")

    if embedded_match and acoustid_confirms and not acoustid_contradicts:
        proof = RecordingIdentityProof.MULTI_SOURCE_DETERMINISTIC
    elif embedded_match:
        proof = RecordingIdentityProof.EMBEDDED_RECORDING_ID
    elif acoustid_confirms and not acoustid_contradicts:
        proof = RecordingIdentityProof.ACOUSTID_RECORDING_ID
    elif rec_id and (title_score >= TITLE_STRONG or filename_score >= TITLE_STRONG):
        proof = RecordingIdentityProof.TEXTUAL_SUPPORT
    else:
        proof = RecordingIdentityProof.INSUFFICIENT

    # A deterministic identity may explain away a title difference only when
    # the artist and duration/position independently agree. "unknown" is
    # never agreement.
    corroborated = duration_status in {"yes", "tolerance"} or position_status == "yes"
    title_mismatch_overridden = bool(
        local_title and rec_title and title_score < TITLE_STRONG
        and proof.deterministic
        and artist_score >= ARTIST_STRONG
        and not existing_conflict
        and not acoustid_contradicts
        and corroborated
    )

    # ---- conflicts ----
    if acoustid.provenance_conflict:
        _add(conflicts, "fingerprint_provenance_conflict")
    if acoustid.status == AcoustIDStatus.CONFLICT:
        _add(conflicts, "fingerprint_conflict")
    if acoustid_contradicts and acoustid.recording_id and rec_id and acoustid.recording_id != rec_id:
        _add(conflicts, "fingerprint_recording_id_conflict")
    if recording_id_source_conflict:
        _add(conflicts, "recording_id_source_conflict")
    if existing_conflict:
        _add(conflicts, "recording_id_conflict")
    if local_title and rec_title and title_score < TITLE_CONFLICT_BELOW and not title_mismatch_overridden:
        _add(conflicts, "title_conflict")
    if local_artist and rec_artist and artist_score < ARTIST_CONFLICT_BELOW:
        _add(conflicts, "artist_conflict")
    if local_album and release_title and album_score < ALBUM_CONFLICT_BELOW:
        _add(conflicts, "album_conflict")
    if year_status == "conflict":
        _add(conflicts, "year_conflict")
    if duration_status == "conflict":
        _add(conflicts, "duration_conflict")
    if release_group_status == "conflict":
        _add(conflicts, "release_group_conflict")
    if position_status == "conflict" and not proof.deterministic:
        _add(conflicts, "track_position_conflict")
    if release_group_source_conflict:
        _add(conflicts, "release_group_id_source_conflict")
    if selected_release_not_linked:
        _add(conflicts, "selected_release_not_linked")

    # ---- warnings / review reasons ----
    if not cand_rgid:
        _add(warnings, "release_group_id_missing")
    ai_id = _norm_id(ai_recording_id)
    if ai_id and rec_id and ai_id != rec_id:
        _add(warnings, "ai_recording_conflict")
    if acoustid.provenance == "incomplete" and not acoustid.recording_id:
        _add(warnings, "acoustid_mapped_recording_id_missing")
    if title_mismatch_overridden:
        _add(warnings, "title_mismatch_with_strong_recording_evidence")
    local_versions = version_markers(local_title) | version_markers(fn_title if not local_title else "")
    rec_versions = version_markers(rec_title)
    if local_versions != rec_versions and (local_title or fn_title) and rec_title:
        _add(warnings, "version_qualifier_mismatch")
        if not proof.deterministic:
            _add(review, "version_qualifier_mismatch")
    if acoustid.status == AcoustIDStatus.AMBIGUOUS and not embedded_match:
        _add(warnings, "acoustid_ambiguous")
        _add(review, "acoustid_ambiguous")
    if not local_title and fn_title and rec_id:
        _add(warnings, "filename_only_title_evidence")

    hard = [c for c in conflicts if c in HARD_CONFLICTS]
    # With no title tag, the filename is the only title evidence available.
    effective_title_score = title_score if local_title else filename_score
    text_agrees = (
        (effective_title_score >= TITLE_STRONG and artist_score >= ARTIST_STRONG)
        or title_mismatch_overridden
    )

    if not rec_id:
        safety_key = "none"
        state = ConfidenceState.CONFLICT if hard else ConfidenceState.INSUFFICIENT_EVIDENCE
        eligibility_reason = (
            "Deterministic Recording ID sources disagree; resolve before attaching."
            if recording_id_source_conflict
            else "No MusicBrainz Recording ID is available."
        )
        _add(review, "recording_id_missing")
    elif hard:
        safety_key = "conflict"
        state = ConfidenceState.CONFLICT
        eligibility_reason = "Resolve conflicting deterministic evidence before attaching Recording ID."
    elif conflicts:
        safety_key = "review"
        state = ConfidenceState.REVIEW_RECOMMENDED
        eligibility_reason = "Candidate has review-only conflicts: " + ", ".join(conflicts)
        for code in conflicts:
            _add(review, code)
    elif not cand_rgid:
        safety_key = "review"
        state = ConfidenceState.REVIEW_RECOMMENDED
        eligibility_reason = "Only recording identity is supported; release-group identity is missing."
        _add(review, "release_group_id_missing")
    elif review:
        safety_key = "review"
        state = ConfidenceState.REVIEW_RECOMMENDED
        eligibility_reason = "Candidate needs review: " + ", ".join(review)
    elif not proof.deterministic:
        safety_key = "review"
        state = ConfidenceState.STRONG_MATCH if text_agrees else ConfidenceState.REVIEW_RECOMMENDED
        eligibility_reason = (
            "Text evidence supports this recording, but no embedded Recording ID or AcoustID "
            "fingerprint proves it; confirm before attaching."
        )
        _add(review, "no_deterministic_recording_proof")
    elif not text_agrees:
        safety_key = "review"
        state = ConfidenceState.REVIEW_RECOMMENDED
        eligibility_reason = "Deterministic identity lacks corroborating title/artist evidence; confirm before attaching."
        _add(review, "deterministic_identity_uncorroborated")
    else:
        safety_key = "safe"
        state = ConfidenceState.CONFIRMED
        eligibility_reason = "Recording ID can be attached from deterministic evidence without additional review."

    confidence_score = max(
        0.0,
        min(1.0, max(_num(heuristic_score) or 0.0, acoustid.score if acoustid.confirmed else 0.0)),
    )
    return RecordingMatchResult(
        candidate_recording_id=rec_id,
        existing_recording_id=existing_id,
        acoustid=acoustid,
        identity_proof=proof,
        state=state,
        safety_key=safety_key,
        title_score=title_score,
        artist_score=artist_score,
        album_score=album_score,
        filename_score=filename_score,
        duration_status=duration_status,
        duration_delta=duration_delta,
        position_status=position_status,
        year_status=year_status,
        release_group_status=release_group_status,
        confidence_score=round(confidence_score, 3),
        title_mismatch_overridden=title_mismatch_overridden,
        positives=positives,
        conflicts=conflicts,
        warnings=warnings,
        review_reasons=review,
        eligibility_reason=eligibility_reason,
    )


def verify_audio_against_request(
    hits: Optional[Sequence[Mapping[str, Any]]],
    *,
    expected_title: str = "",
    expected_artist: str = "",
    expected_recording_id: str = "",
    similarity_fn: Optional[SimilarityFn] = None,
) -> Dict[str, Any]:
    """Canonical check that fingerprinted audio is the track that was
    requested (a downloaded file about to be imported).

    Only AcoustID hits at or above the canonical floor count, and only the
    top tier (within the shared 3-point ambiguity window) decides:

    * expected Recording ID in the top tier, every other top-tier recording
      being the same song (title/artist/version) -> accept. Ties between
      duplicate MusicBrainz recordings of one song do not change which song
      the audio is.
    * expected Recording ID only below the top tier -> review (ambiguous).
    * expected Recording ID absent, top tier is the same song by text ->
      review (a different MusicBrainz recording of that song; never auto
      accepted, never deleted).
    * top tier is a different song -> reject (hard conflict).
    * no expected ID: the whole top tier agrees with the requested
      title/artist -> accept; only part of it -> review; none -> reject.
    * no hits / only low-score hits -> review, never a conflict.

    Returns decision (accept|review|reject), status (canonical
    AcoustIDStatus value), recording_id, score (0..1), conflicts, reason.
    """
    sim = similarity_fn or _default_similarity
    expected = _norm_id(expected_recording_id)
    if hits is None:
        return {"decision": "review", "status": AcoustIDStatus.UNAVAILABLE.value, "recording_id": "",
                "score": 0.0, "conflicts": [], "reason": "AcoustID lookup unavailable."}
    rows = [dict(h) for h in hits if isinstance(h, Mapping) and _hit_recording_id(dict(h))]
    if not rows:
        return {"decision": "review", "status": AcoustIDStatus.NO_RESULT.value, "recording_id": "",
                "score": 0.0, "conflicts": [], "reason": "No AcoustID candidate was returned for this audio."}
    high = sorted(
        (r for r in rows if _hit_score(r) >= ACOUSTID_MIN_SCORE),
        key=lambda r: (-_hit_score(r), _hit_recording_id(r)),
    )
    if not high:
        return {"decision": "review", "status": AcoustIDStatus.AMBIGUOUS.value, "recording_id": "",
                "score": round(max(_hit_score(r) for r in rows) / 100.0, 3), "conflicts": [],
                "reason": "AcoustID candidates are below the identity confidence floor."}
    top_score = _hit_score(high[0])
    tier = [r for r in high if _hit_score(r) >= top_score - 3.0]
    tier_ids = {_hit_recording_id(r) for r in tier}

    def _agrees(row: Mapping[str, Any]) -> bool:
        title = str(row.get("title") or "")
        artist = str(row.get("artist") or "")
        if not expected_title or not title:
            return False
        if sim(expected_title, title) < 0.78 or version_markers(expected_title) != version_markers(title):
            return False
        return not expected_artist or not artist or sim(expected_artist, artist) >= ARTIST_STRONG

    def _out(decision: str, status: AcoustIDStatus, row: Mapping[str, Any], reason: str, conflicts=()) -> Dict[str, Any]:
        return {
            "decision": decision,
            "status": status.value,
            "recording_id": _hit_recording_id(dict(row)),
            "score": round(_hit_score(dict(row)) / 100.0, 3),
            "conflicts": list(conflicts),
            "reason": reason,
        }

    if expected:
        if expected in tier_ids:
            chosen = next(r for r in tier if _hit_recording_id(r) == expected)
            others = [r for r in tier if _hit_recording_id(r) != expected]
            if all(_agrees(r) for r in others) and (not others or _agrees(chosen)):
                return _out("accept", AcoustIDStatus.CONFIRMED, chosen,
                            "AcoustID fingerprint confirms the requested MusicBrainz recording ID.")
            return _out("review", AcoustIDStatus.AMBIGUOUS, chosen,
                        "AcoustID also matches a different song at the same confidence.")
        if expected in {_hit_recording_id(r) for r in high}:
            return _out("review", AcoustIDStatus.AMBIGUOUS, high[0],
                        "Requested recording is not AcoustID's strongest match.")
        if all(_agrees(r) for r in tier):
            return _out("review", AcoustIDStatus.CONFLICT, high[0],
                        "AcoustID identifies a different MusicBrainz recording of the same song.",
                        ["acoustid_recording_id_differs"])
        return _out("reject", AcoustIDStatus.CONFLICT, high[0],
                    "AcoustID identified a different recording than the requested track.",
                    ["acoustid_metadata_conflict"])

    agreeing = [r for r in tier if _agrees(r)]
    if agreeing and len(agreeing) == len(tier):
        return _out("accept", AcoustIDStatus.CONFIRMED, agreeing[0],
                    "AcoustID fingerprint identifies the requested title and artist.")
    if agreeing:
        return _out("review", AcoustIDStatus.AMBIGUOUS, agreeing[0],
                    "AcoustID matches the requested track and a different song at the same confidence.")
    return _out("reject", AcoustIDStatus.CONFLICT, high[0],
                "AcoustID identified a different recording than the requested track.",
                ["acoustid_metadata_conflict"])


def best_recording_candidates(results: Iterable[RecordingMatchResult]) -> List[RecordingMatchResult]:
    """Order evaluated candidates for display: safe first, then by proof
    strength, then confidence. Ordering never changes any candidate's own
    eligibility."""
    rank = {"safe": 0, "review": 1, "none": 2, "conflict": 3}
    proof_rank = {
        RecordingIdentityProof.MULTI_SOURCE_DETERMINISTIC: 0,
        RecordingIdentityProof.EMBEDDED_RECORDING_ID: 1,
        RecordingIdentityProof.ACOUSTID_RECORDING_ID: 1,
        RecordingIdentityProof.TEXTUAL_SUPPORT: 2,
        RecordingIdentityProof.INSUFFICIENT: 3,
    }
    return sorted(
        results,
        key=lambda r: (rank.get(r.safety_key, 9), proof_rank[r.identity_proof], -r.confidence_score),
    )
