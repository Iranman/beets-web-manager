"""Recording-candidate generation service for Import Review / AI suggest.

CANDIDATE_GENERATION_ONLY: this module merges AcoustID and MusicBrainz
text-search results into one de-duplicated candidate list, stamps each with
explicit fingerprint provenance, and orders them for display/prompting. It
never decides whether a candidate is safe -- every final decision comes from
``backend.matching.evaluate_recording_candidate`` via
``build_recording_matching_decision``, which receives the full AcoustID hit
set (``acoustid_hits_for``) so a text candidate the fingerprint contradicts
is a conflict, not "no result".
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

ScoreFn = Callable[[Dict[str, Any]], Dict[str, Any]]
EnrichFn = Callable[[Dict[str, Any], Optional[Sequence[Dict[str, Any]]]], Any]

MAX_RECORDING_CANDIDATES = 8


def acoustid_hits_for(item_path: str, acoustid_candidates: Sequence[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """The AcoustID hit set to hand the canonical evaluator: ``None`` when no
    lookup could run (no local file -> UNAVAILABLE), otherwise the lookup's
    result list (possibly empty -> NO_RESULT)."""
    if not item_path:
        return None
    return [dict(c) for c in acoustid_candidates if isinstance(c, dict)]


def merge_recording_candidates(
    acoustid_candidates: Sequence[Dict[str, Any]],
    text_candidates: Sequence[Dict[str, Any]],
    *,
    item_path: str,
    score_fn: ScoreFn,
    limit: int = MAX_RECORDING_CANDIDATES,
) -> List[Dict[str, Any]]:
    """De-duplicate AcoustID + MusicBrainz text candidates by Recording ID,
    stamp explicit fingerprint provenance, attach the ranking-only
    ``_match_score``, and return the top ``limit`` by that score."""
    acoustid_ids = {id(c) for c in acoustid_candidates}
    seen: set = set()
    merged: List[Dict[str, Any]] = []
    for c in list(acoustid_candidates) + list(text_candidates):
        mid = c.get("mb_trackid", "")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        is_acoustid = id(c) in acoustid_ids
        c["source"] = c.get("source") or ("acoustid" if is_acoustid else "mb")
        # Explicit fingerprint provenance: true only for candidates that
        # actually came back from a successful AcoustID lookup, never
        # inferred later from source string or score alone.
        c["fingerprint_attempted"] = bool(item_path)
        c["fingerprint_matched"] = bool(is_acoustid)
        c["fingerprint_status"] = c.get("fingerprint_status") or (
            "matched" if is_acoustid else ("no_result" if item_path else "not_attempted")
        )
        c["mapped_recording_id"] = mid if is_acoustid else ""
        c["_match_score"] = score_fn(c)
        merged.append(c)
    merged.sort(key=_ranking_total, reverse=True)
    return merged[:limit]


def enrich_and_index(
    candidates: List[Dict[str, Any]],
    enrich: EnrichFn,
    acoustid_hits: Optional[Sequence[Dict[str, Any]]],
    *,
    swallow_errors: bool = False,
) -> List[Dict[str, Any]]:
    """Run the canonical decision serializer on every candidate, then
    re-order for display and assign stable candidate indexes."""
    for c in candidates:
        if swallow_errors:
            try:
                enrich(c, acoustid_hits)
            except Exception as exc:
                c["enrichment_error"] = str(exc)
        else:
            enrich(c, acoustid_hits)
    candidates.sort(key=_ranking_total, reverse=True)
    for idx, c in enumerate(candidates):
        c["candidate_index"] = idx
    return candidates


def resolve_recording_identity(
    local: Dict[str, Any],
    *,
    acoustid_hits: Optional[Sequence[Dict[str, Any]]],
    text_candidates: Sequence[Dict[str, Any]] = (),
    similarity_fn: Optional[Callable[[str, str], float]] = None,
) -> Dict[str, Any]:
    """Establish which MusicBrainz recording a local file IS, for workflows
    that act on that answer without a human (format replacement).

    Every candidate Recording ID -- the embedded one, every AcoustID hit,
    every text-search result -- is evaluated by the canonical single-recording
    evaluator. Identity is established only by deterministic proof (embedded
    Recording ID and/or confirmed AcoustID) with no hard conflict, and only
    when exactly one recording qualifies. Text-search results are never
    enough on their own.

    ``local``: title, artist, album, duration_seconds, filename,
    recording_id (embedded mb_trackid, may be "").
    """
    from backend.matching import acoustid_evidence_from_hits, evaluate_recording_candidate

    embedded = str(local.get("recording_id") or "").strip().lower()
    candidates: Dict[str, Dict[str, Any]] = {}

    def _add(rid: Any, meta: Dict[str, Any], origin: str) -> None:
        rid = str(rid or "").strip().lower()
        if not rid:
            return
        row = candidates.setdefault(rid, {"recording_id": rid, "origins": []})
        if origin not in row["origins"]:
            row["origins"].append(origin)
        for key in ("title", "artist", "album", "mb_albumid", "mb_releasegroupid", "year"):
            if meta.get(key) and not row.get(key):
                row[key] = meta.get(key)

    if embedded:
        _add(embedded, {"title": local.get("title"), "artist": local.get("artist")}, "embedded")
    for hit in acoustid_hits or []:
        if isinstance(hit, dict):
            _add(hit.get("mb_trackid") or hit.get("recording_id"), hit, "acoustid")
    for cand in text_candidates or []:
        if isinstance(cand, dict):
            _add(cand.get("mb_trackid"), cand, "musicbrainz_search")

    evaluations: List[Dict[str, Any]] = []
    established: List[Any] = []
    for rid, cand in candidates.items():
        result = evaluate_recording_candidate(
            local,
            {
                "recording_id": rid,
                "title": cand.get("title") or "",
                "artist": cand.get("artist") or "",
                "release_group_id": cand.get("mb_releasegroupid") or "",
            },
            acoustid=acoustid_evidence_from_hits(acoustid_hits, rid),
            similarity_fn=similarity_fn,
        )
        evaluations.append({**result.to_dict(), "origins": list(cand["origins"])})
        if result.identity_established():
            established.append((rid, cand, result))

    base = {"evaluations": evaluations, "candidate_count": len(candidates)}
    if len(established) == 1:
        rid, cand, result = established[0]
        return {
            **base,
            "ok": True,
            "recording_id": rid,
            "identity_proof": result.identity_proof.value,
            "candidate": cand,
            "reason": "recording identity established by " + result.identity_proof.value,
        }
    if len(established) > 1:
        return {**base, "ok": False, "recording_id": "", "identity_proof": "insufficient", "candidate": {},
                "reason": "multiple recordings are deterministically plausible; review required"}
    embedded_eval = next((e for e in evaluations if e["candidate_recording_id"] == embedded), None)
    if embedded_eval and embedded_eval["hard_conflicts"]:
        reason = "embedded Recording ID conflicts with other evidence: " + ", ".join(embedded_eval["hard_conflicts"])
    elif candidates:
        reason = "only text-similar MusicBrainz recordings found; no embedded Recording ID or AcoustID proof"
    else:
        reason = "unable to resolve MusicBrainz recording identity"
    return {**base, "ok": False, "recording_id": "", "identity_proof": "insufficient", "candidate": {},
            "reason": reason}


def apply_replacement_identity(
    resolved: Dict[str, Any],
    *,
    title: str,
    artist: str,
    filename: str,
    acoustid_hits: Optional[Sequence[Dict[str, Any]]],
    search_text: Callable[[str, str], Sequence[Dict[str, Any]]],
    similarity_fn: Optional[Callable[[str, str], float]] = None,
    log: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Resolve the recording a format-replacement row IS and write it into
    ``resolved``. The original file is removed once a replacement verifies
    against this identity, so only canonical deterministic proof counts:
    an AcoustID hit never silently overrides the embedded Recording ID, and a
    text-search result is never enough on its own. On failure
    ``resolved["mb_trackid"]`` is cleared and ``review_reason`` set."""
    log = log if log is not None else []
    local = {
        "title": title,
        "artist": artist,
        "album": str(resolved.get("album") or ""),
        "duration_seconds": resolved.get("length") or resolved.get("duration_seconds"),
        "filename": filename,
        "recording_id": str(resolved.get("mb_trackid") or "").strip().lower(),
    }
    identity = resolve_recording_identity(local, acoustid_hits=acoustid_hits, similarity_fn=similarity_fn)
    if not identity.get("ok") and title and not local["recording_id"]:
        log.append("  Searching for metadata")
        identity = resolve_recording_identity(
            local,
            acoustid_hits=acoustid_hits,
            text_candidates=search_text(title, artist),
            similarity_fn=similarity_fn,
        )
    resolved["identity_proof"] = identity.get("identity_proof") or "insufficient"
    if not identity.get("ok"):
        resolved["mb_trackid"] = ""
        resolved["review_reason"] = str(identity.get("reason") or "")
        log.append(f"  Recording identity needs review: {identity.get('reason')}")
        return resolved
    chosen = identity.get("candidate") or {}
    resolved["mb_trackid"] = identity["recording_id"]
    resolved["identity_mb_trackid"] = identity["recording_id"]
    if "acoustid" in (chosen.get("origins") or []):
        resolved["acoustid_mb_trackid"] = identity["recording_id"]
    for key in ("title", "artist"):
        if chosen.get(key):
            resolved[key] = str(chosen.get(key))
    for key in ("album", "year"):
        if chosen.get(key) and not str(resolved.get(key) or "").strip():
            resolved[key] = str(chosen.get(key))
    if chosen.get("mb_releasegroupid"):
        resolved["mb_releasegroupid"] = str(chosen.get("mb_releasegroupid")).strip().lower()
    if chosen.get("mb_albumid") and not str(resolved.get("mb_albumid") or "").strip():
        resolved["mb_albumid"] = str(chosen.get("mb_albumid")).strip().lower()
    log.append(f"  Recording identity: {identity.get('reason')}")
    return resolved


def audio_identity_fields(
    verdict: Dict[str, Any],
    chosen: Dict[str, Any],
    text_match: Dict[str, Any],
    *,
    acoustid_score: float,
) -> Dict[str, Any]:
    """Map a canonical verify_audio_against_request verdict onto the
    file-level identity result schema (_audio_identity_decision)."""
    decision = verdict["decision"]
    fields: Dict[str, Any] = {
        "acoustid_canonical_status": verdict["status"],
        "acoustid_match_score": acoustid_score,
        "acoustid_id": str(chosen.get("acoustid_id") or ""),
        "mb_recording_id_candidate": str(chosen.get("mb_trackid") or ""),
        "mb_release_group_id_candidate": str(chosen.get("mb_releasegroupid") or ""),
        "decision_reason": verdict["reason"],
        "conflicts": list(verdict.get("conflicts") or []),
        "final_action": decision,
    }
    if decision == "accept":
        fields.update({
            "acoustid_status": "confirmed",
            "metadata_agreement": "match",
            "ai_assessment": "Fingerprint evidence agrees with the requested recording context.",
            "final_confidence": "high",
            "identity_status": "verified",
        })
    elif decision == "reject":
        fields.update({
            "acoustid_status": "mismatch",
            "metadata_agreement": "conflict",
            "ai_assessment": "Fingerprint evidence conflicts with the requested metadata; do not trust filename or tags alone.",
            "final_confidence": "high",
            "identity_status": "conflict",
        })
        if (text_match.get("ok") or float(text_match.get("title_score") or 0) >= 0.90
                or float(text_match.get("artist_score") or 0) >= 0.90):
            fields["conflicts"].append("text_metadata_disagrees_with_fingerprint")
    else:
        fields.update({
            "acoustid_status": "ambiguous" if verdict["status"] == "ambiguous" else "candidate",
            "metadata_agreement": "degraded",
            "ai_assessment": "Review required; fingerprint evidence does not uniquely confirm the requested track.",
            "final_confidence": "medium",
            "identity_status": "review_required",
        })
    return fields


def _ranking_total(candidate: Dict[str, Any]) -> float:
    score = candidate.get("_match_score")
    if not isinstance(score, dict):
        return 0.0
    try:
        return float(score.get("total") or 0)
    except Exception:
        return 0.0
