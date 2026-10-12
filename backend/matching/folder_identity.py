"""Deterministic ranking and the Stage 2b auto-import decision for one
import folder that Beets skipped.

Pure: no I/O and no AI. ``backend.import_identity.identify_import_folder``
gathers the evidence (embedded IDs, per-file AcoustID, MusicBrainz releases,
one ``_folder_release_preflight`` per release) and calls these functions.

A release candidate ("evaluated release") is a dict::

    {"release_id", "release_group_id", "track_count", "sources": [...],
     "preflight": <compact preflight>, "matching_decision": <MatchingDecision.to_dict()>,
     "error": ""}

``matching_decision["action_allowed"]`` is ``can_auto_accept(VERIFIED_SUBSET)``
plus no conflicts and a resolved Release Group (see
``matching_contract.build_album_matching_decision``).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from .track_alignment import _hit_recording_id, _hit_score

#: Codes that block an unattended import outright (audit condition 3).
HARD_CONFLICTS = frozenset({
    "recording_id_conflict",
    "fingerprint_recording_id_conflict",
    "acoustid_conflict",
    "acoustid_ambiguous",
    "acoustid_mismatch",
    "artist_id_conflict",
    "release_group_conflict",
    "wrong_release_group",
    "embedded_release_group_conflict",
    "candidate_metadata_mixed",
})

#: Checked by the import job after the pinned re-import, not here (condition 7).
POST_CONDITIONS = ("beets_strong_match", "single_new_album_with_release_group")

_CONFIRMED_SCORE = 80.0


def _s(value: Any) -> str:
    return str(value or "").strip().lower()


def confirmed_recording(hits: Optional[List[Dict[str, Any]]]) -> str:
    """The one recording a file's AcoustID hits confirm, or "".

    Same rule as the canonical evaluator: a hit scoring >= 80 with no other
    recording within 3 points of it."""
    if not hits:
        return ""
    high = sorted(
        ((_hit_recording_id(h), _hit_score(h)) for h in hits if isinstance(h, dict)),
        key=lambda row: (-row[1], row[0]),
    )
    high = [row for row in high if row[0] and row[1] >= _CONFIRMED_SCORE]
    if not high:
        return ""
    top, score = high[0]
    if any(rec != top and s >= score - 3.0 for rec, s in high[1:]):
        return ""
    return top


def file_conflicts(files: Iterable[Dict[str, Any]]) -> List[str]:
    """Folder-level conflicts that do not depend on a candidate.

    ``files`` rows: ``{"path", "mb_trackid", "acoustid_hits"}``. An embedded
    Recording ID that a confirmed fingerprint contradicts is a conflict
    between the two strongest evidence tiers; it is surfaced, never resolved.
    """
    out: List[str] = []
    for row in files:
        embedded = _s(row.get("mb_trackid"))
        fingerprint = confirmed_recording(row.get("acoustid_hits"))
        if embedded and fingerprint and embedded != fingerprint:
            out.append("fingerprint_recording_id_conflict")
            break
    return out


def _canonical(release: Dict[str, Any]) -> Dict[str, Any]:
    decision = release.get("matching_decision") or {}
    return ((decision.get("evidence") or {}).get("canonical_match") or {})


def release_conflicts(release: Dict[str, Any], embedded_release_groups: Iterable[str] = ()) -> List[str]:
    """Conflicts for one evaluated release, including evidence the
    preflight decision does not see (release-level AcoustID mismatch,
    embedded Release Group tags)."""
    decision = release.get("matching_decision") or {}
    codes = set(decision.get("conflicts") or [])
    if (release.get("preflight") or {}).get("acoustid_mismatch"):
        codes.add("acoustid_mismatch")
    embedded = {_s(rg) for rg in embedded_release_groups if _s(rg)}
    rgid = _s(release.get("release_group_id"))
    if embedded and rgid and rgid not in embedded:
        codes.add("embedded_release_group_conflict")
    return sorted(codes)


def track_proof(release: Dict[str, Any]) -> Tuple[int, int]:
    """(local tracks deterministically proven, local tracks total)."""
    canonical = _canonical(release)
    return int(canonical.get("local_tracks_verified") or 0), int(canonical.get("local_tracks_total") or 0)


def _release_key(release: Dict[str, Any], folder_track_count: int) -> tuple:
    """Representative release inside one Release Group (condition 6): same
    track count first, then alignment, then a stable ID tiebreak."""
    canonical = _canonical(release)
    verified, _ = track_proof(release)
    pre = release.get("preflight") or {}
    return (
        0 if int(release.get("track_count") or 0) == int(folder_track_count or -1) else 1,
        0 if (release.get("matching_decision") or {}).get("action_allowed") else 1,
        -verified,
        -int(canonical.get("target_tracks_matched") or 0),
        -int(pre.get("acoustid_target_hits") or 0),
        -float(canonical.get("score") or 0.0),
        _s(release.get("release_id")),
    )


def rank_folder_candidates(releases: Iterable[Dict[str, Any]], folder_track_count: int,
                           embedded_release_groups: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """Group evaluated releases by Release Group, pick each group's
    representative release, and rank the groups deterministically:
    ``can_auto_accept`` first, then verified local tracks, then canonical
    score. Releases with no Release Group are dropped (no canonical album
    identity to offer).

    Returns ``[{release_group_id, representative_release_id,
    matching_decision, deterministic_rank, ai_rank, evidence}]``.
    """
    embedded = [rg for rg in embedded_release_groups]
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for release in releases:
        rgid = _s(release.get("release_group_id"))
        if rgid and not release.get("error"):
            groups.setdefault(rgid, []).append(release)

    rows: List[Dict[str, Any]] = []
    for rgid, members in groups.items():
        members.sort(key=lambda r: _release_key(r, folder_track_count))
        rep = members[0]
        conflicts = release_conflicts(rep, embedded)
        verified, total = track_proof(rep)
        decision = rep.get("matching_decision") or {}
        rows.append({
            "release_group_id": rgid,
            "representative_release_id": _s(rep.get("release_id")),
            "matching_decision": decision,
            "deterministic_rank": 0,
            "ai_rank": None,
            "evidence": {
                "sources": sorted({src for m in members for src in (m.get("sources") or [])}),
                "conflicts": conflicts,
                "action_allowed": bool(decision.get("action_allowed")) and not conflicts,
                "local_tracks_verified": verified,
                "local_tracks_total": total,
                "any_track_proof": any(track_proof(m)[0] > 0 for m in members),
                "preflight": rep.get("preflight") or {},
                "releases_evaluated": [
                    {"release_id": _s(m.get("release_id")), "track_count": int(m.get("track_count") or 0),
                     "action_allowed": bool((m.get("matching_decision") or {}).get("action_allowed")),
                     "local_tracks_verified": track_proof(m)[0]}
                    for m in members
                ],
            },
        })

    rows.sort(key=lambda r: (
        0 if r["evidence"]["action_allowed"] else 1,
        -r["evidence"]["local_tracks_verified"],
        -float(_canonical({"matching_decision": r["matching_decision"]}).get("score") or 0.0),
        r["release_group_id"],
    ))
    for index, row in enumerate(rows, start=1):
        row["deterministic_rank"] = index
    return rows


def decide_auto_import(candidates: List[Dict[str, Any]], *, skip_reason: str,
                       audio_count: int, folder_conflicts: Iterable[str] = (),
                       fingerprint_groups: Iterable[str] = (), failed_lookups: int = 0) -> Dict[str, Any]:
    """Stage 2b: may Web Manager re-import this folder pinned to one release?

    Eligible only when every condition holds (audit conditions 1-6;
    condition 7 is ``POST_CONDITIONS``, checked by the import job):

    1. exactly one Release Group passes ``can_auto_accept(VERIFIED_SUBSET)``;
    2. no other Release Group has any deterministic track proof, either on an
       evaluated candidate or directly in confirmed AcoustID hits
       (`fingerprint_groups`); a fingerprint-indicated group that was never
       evaluated, or any release lookup that went unanswered
       (`failed_lookups`), is unknown evidence and blocks;
    3. no hard conflict (folder-level or on the chosen candidate);
    4. every local audio file is proven (fingerprint CONFIRMED or embedded
       Recording ID matched);
    5. the skip reason was not ``duplicate``;
    6. a representative release was chosen deterministically.

    AI never feeds this decision.
    """
    reasons: List[str] = []
    folder_conflicts = sorted(set(folder_conflicts))
    if _s(skip_reason) == "duplicate":
        reasons.append("skip_reason_duplicate")
    hard_folder = [c for c in folder_conflicts if c in HARD_CONFLICTS]
    if hard_folder:
        reasons.append("folder_conflict:" + ",".join(hard_folder))
    if failed_lookups:
        reasons.append(f"release_lookup_failed:{failed_lookups}")
    fingerprint_groups = set(fingerprint_groups) - {""}
    if fingerprint_groups - {c["release_group_id"] for c in candidates}:
        reasons.append("fingerprint_group_not_evaluated")

    accepted = [c for c in candidates if c["evidence"]["action_allowed"]]
    chosen = accepted[0] if len(accepted) == 1 else None
    if not candidates:
        reasons.append("no_candidates")
    elif not accepted:
        reasons.append("no_release_group_verified")
    elif len(accepted) > 1:
        reasons.append("multiple_release_groups_verified")

    if chosen is not None:
        others = [c for c in candidates if c is not chosen and c["evidence"]["any_track_proof"]]
        if others or fingerprint_groups - {chosen["release_group_id"]}:
            reasons.append("other_release_group_has_track_proof")
        hard = [c for c in chosen["evidence"]["conflicts"] if c in HARD_CONFLICTS]
        if hard:
            reasons.append("candidate_conflict:" + ",".join(hard))
        verified, total = chosen["evidence"]["local_tracks_verified"], chosen["evidence"]["local_tracks_total"]
        if not audio_count or total != audio_count or verified != total:
            reasons.append(f"files_not_all_proven:{verified}/{audio_count}")
        if not chosen["representative_release_id"]:
            reasons.append("no_representative_release")

    eligible = chosen is not None and not reasons
    return {
        "eligible": eligible,
        "release_group_id": chosen["release_group_id"] if eligible else "",
        "release_id": chosen["representative_release_id"] if eligible else "",
        "reasons": reasons,
        "post_conditions": list(POST_CONDITIONS),
    }
