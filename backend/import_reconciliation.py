"""Canonical reconciliation of a fresh import into an existing album (ARCH-002/009).

When newly imported tracks land in the same disc/track slot as tracks that
already exist in the target album, something has to decide whether one of
the two files goes away. That decision is destructive, so it is made only
from canonical deterministic evidence:

* The album itself must be proven to be the same album first, by Release
  Group ID (ARCH-009). Disc/track only identifies a slot AFTER album identity
  is established; a Release ID, title or position never substitutes for it,
  and an unknown RGID is never inherited from the other album.
* Each side of a contested slot is evaluated against the MusicBrainz track
  expected there with ``backend.matching.evaluate_recording_candidate``
  (embedded Recording ID + canonical AcoustID evidence).

Outcomes per contested slot:

KEEP_EXISTING     both files are deterministically the expected recording --
                  the imported copy is a true duplicate and may be discarded.
KEEP_IMPORTED     the existing file is deterministically NOT the expected
                  recording (Recording ID / fingerprint conflict) and the
                  imported file deterministically IS -- the existing row may
                  be retired.
CONFLICT          deterministic evidence contradicts itself or the imported
                  file is deterministically the wrong recording -- keep both,
                  review.
KEEP_BOTH_REVIEW  identity is not deterministically established (text,
                  title, position only) -- keep both files and both rows,
                  review.

Text similarity is recorded as explanatory evidence only; it can never cause
either file to be discarded.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

try:
    from matching import acoustid_evidence_from_hits, evaluate_recording_candidate
except ImportError:
    from backend.matching import acoustid_evidence_from_hits, evaluate_recording_candidate

HitsFn = Callable[[Dict[str, Any]], Optional[Sequence[Dict[str, Any]]]]
ExistsFn = Callable[[Dict[str, Any]], bool]
SimilarityFn = Callable[[str, str], float]

#: Only audio evidence may prove a file is NOT the expected recording. A
#: Recording ID mismatch alone is not enough: the same song on another
#: edition of the release group can carry a different Recording ID.
WRONG_RECORDING_CONFLICTS = frozenset({
    "fingerprint_conflict",
    "fingerprint_recording_id_conflict",
})


class ReconciliationOutcome(str, Enum):
    KEEP_EXISTING = "keep_existing"
    KEEP_IMPORTED = "keep_imported"
    KEEP_BOTH_REVIEW = "keep_both_review"
    CONFLICT = "conflict"

    @property
    def destructive(self) -> bool:
        return self in (ReconciliationOutcome.KEEP_EXISTING, ReconciliationOutcome.KEEP_IMPORTED)


def _s(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _id(value: Any) -> str:
    return _s(value).strip().lower()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def slot_key(row: Dict[str, Any]) -> tuple:
    return (_int(row.get("disc"), 1) or 1, _int(row.get("track"), 0))


# ---------------------------------------------------------------------------
# Album identity (ARCH-009)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AlbumIdentity:
    same_album: bool
    release_group_id: str
    reason: str
    existing_release_group_id: str = ""
    imported_release_group_id: str = ""
    target_release_group_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "same_album": self.same_album,
            "release_group_id": self.release_group_id,
            "reason": self.reason,
            "existing_release_group_id": self.existing_release_group_id,
            "imported_release_group_id": self.imported_release_group_id,
            "target_release_group_id": self.target_release_group_id,
        }


def album_identity(existing_album: Dict[str, Any], imported_album: Dict[str, Any],
                   target_release_group_id: str = "") -> AlbumIdentity:
    """Prove both album rows are the same canonical album by Release Group.

    Every known RGID (existing row, imported row, the release's authoritative
    RG from MusicBrainz) must agree and the existing row's RGID must be known.
    A Release ID never stands in for an RGID, and a missing RGID is never
    copied from the other side.
    """
    existing_rg = _id(existing_album.get("mb_releasegroupid"))
    imported_rg = _id(imported_album.get("mb_releasegroupid"))
    target_rg = _id(target_release_group_id)
    known = {rg for rg in (existing_rg, imported_rg, target_rg) if rg}
    base = dict(existing_release_group_id=existing_rg, imported_release_group_id=imported_rg,
                target_release_group_id=target_rg)
    if len(known) > 1:
        return AlbumIdentity(False, "", "release_group_mismatch", **base)
    if not existing_rg:
        return AlbumIdentity(False, "", "existing_album_release_group_unknown", **base)
    if not (imported_rg or target_rg):
        return AlbumIdentity(False, "", "imported_album_release_group_unknown", **base)
    return AlbumIdentity(True, existing_rg, "release_group_verified", **base)


# ---------------------------------------------------------------------------
# Per-slot decision
# ---------------------------------------------------------------------------

@dataclass
class SlotDecision:
    outcome: ReconciliationOutcome
    slot: tuple
    existing_item_id: int
    imported_item_id: int
    target_recording_id: str
    existing: Dict[str, Any] = field(default_factory=dict)
    imported: Dict[str, Any] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    recommended_action: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "disc": self.slot[0],
            "track": self.slot[1],
            "existing_item_id": self.existing_item_id,
            "imported_item_id": self.imported_item_id,
            "target_recording_id": self.target_recording_id,
            "existing": dict(self.existing),
            "imported": dict(self.imported),
            "reasons": list(self.reasons),
            "recommended_action": self.recommended_action,
        }


def _evaluate_side(row: Dict[str, Any], target: Dict[str, Any], hits_fn: Optional[HitsFn],
                   similarity_fn: Optional[SimilarityFn], *, trust_embedded_id: bool) -> Any:
    target_rid = _id(target.get("mb_trackid") or target.get("recording_id"))
    hits = hits_fn(row) if hits_fn is not None else None
    duration_ms = _int(target.get("duration_ms"), 0)
    return evaluate_recording_candidate(
        {
            "title": _s(row.get("title")),
            "artist": _s(row.get("artist")),
            "albumartist": _s(row.get("albumartist")),
            "track": _int(row.get("track"), 0) or None,
            "duration_seconds": float(row.get("length") or 0) or None,
            "filename": Path(_s(row.get("path"))).name,
            # The imported row's Recording ID was written by this very import
            # when it aligned the file to this slot -- circular, not evidence.
            "recording_id": _id(row.get("mb_trackid")) if trust_embedded_id else "",
        },
        {
            "recording_id": target_rid,
            "title": _s(target.get("title")),
            "artist": _s(target.get("artist")),
            "duration_seconds": (duration_ms / 1000.0) if duration_ms else None,
            "track_position": _int(target.get("track"), 0) or None,
        },
        acoustid=acoustid_evidence_from_hits(hits, target_rid),
        similarity_fn=similarity_fn,
    )


def _side_summary(row: Dict[str, Any], result: Any) -> Dict[str, Any]:
    return {
        "item_id": _int(row.get("id")),
        "path": _s(row.get("path")),
        "recording_id": _id(row.get("mb_trackid")),
        "release_id": _id(row.get("mb_albumid")),
        "release_group_id": _id(row.get("mb_releasegroupid")),
        "identity_proof": result.identity_proof.value if result is not None else "insufficient",
        "confidence_state": result.state.value if result is not None else "insufficient_evidence",
        "acoustid": result.acoustid.to_dict() if result is not None else {"status": "unavailable"},
        "hard_conflicts": list(result.hard_conflicts) if result is not None else [],
        "review_reasons": list(result.review_reasons) if result is not None else [],
        "title_score": round(float(result.title_score), 3) if result is not None else None,
    }


def decide_slot(existing: Dict[str, Any], imported: Dict[str, Any], target: Optional[Dict[str, Any]], *,
                hits_fn: Optional[HitsFn] = None,
                exists_fn: Optional[ExistsFn] = None,
                similarity_fn: Optional[SimilarityFn] = None) -> SlotDecision:
    """Decide what happens to one contested disc/track slot."""
    slot = slot_key(imported)
    existing_id, imported_id = _int(existing.get("id")), _int(imported.get("id"))
    target = target or {}
    target_rid = _id(target.get("mb_trackid") or target.get("recording_id"))

    if exists_fn is not None and not exists_fn(existing):
        # The existing row's file is gone: retiring the dangling row loses no
        # audio. Still refuse if the import is fingerprinted as the wrong song.
        i_res = _evaluate_side(imported, target, hits_fn, similarity_fn, trust_embedded_id=False) if target_rid else None
        summary_i = _side_summary(imported, i_res)
        if i_res is not None and WRONG_RECORDING_CONFLICTS & set(i_res.hard_conflicts):
            return SlotDecision(ReconciliationOutcome.CONFLICT, slot, existing_id, imported_id, target_rid,
                                _side_summary(existing, None), summary_i,
                                ["existing_file_missing", "imported_not_expected_recording"],
                                "The imported file is a different recording than this slot expects.")
        return SlotDecision(ReconciliationOutcome.KEEP_IMPORTED, slot, existing_id, imported_id, target_rid,
                            _side_summary(existing, None), summary_i, ["existing_file_missing"],
                            "Replace the dangling library row (its file is missing) with the import.")

    if not target_rid:
        # No expected recording for this slot: only a direct, deterministic
        # Recording ID comparison between the two files can say anything.
        e_rid, i_rid = _id(existing.get("mb_trackid")), _id(imported.get("mb_trackid"))
        summary_e, summary_i = _side_summary(existing, None), _side_summary(imported, None)
        if e_rid and i_rid and e_rid != i_rid:
            return SlotDecision(ReconciliationOutcome.CONFLICT, slot, existing_id, imported_id, "",
                                summary_e, summary_i, ["recording_ids_differ", "no_expected_recording_for_slot"],
                                "Two different recordings occupy one slot; choose which belongs here.")
        return SlotDecision(ReconciliationOutcome.KEEP_BOTH_REVIEW, slot, existing_id, imported_id, "",
                            summary_e, summary_i, ["no_expected_recording_for_slot"],
                            "Compare both files and keep the correct one.")

    e_res = _evaluate_side(existing, target, hits_fn, similarity_fn, trust_embedded_id=True)
    i_res = _evaluate_side(imported, target, hits_fn, similarity_fn, trust_embedded_id=False)
    summary_e, summary_i = _side_summary(existing, e_res), _side_summary(imported, i_res)
    e_ok, i_ok = e_res.identity_established(), i_res.identity_established()
    e_wrong = bool(WRONG_RECORDING_CONFLICTS & set(e_res.hard_conflicts))
    i_wrong = bool(WRONG_RECORDING_CONFLICTS & set(i_res.hard_conflicts))

    def _decision(outcome, reasons, action):
        return SlotDecision(outcome, slot, existing_id, imported_id, target_rid, summary_e, summary_i, reasons, action)

    if i_wrong and e_wrong:
        return _decision(ReconciliationOutcome.CONFLICT, ["existing_not_expected_recording", "imported_not_expected_recording"],
                         "Neither file is the expected recording; review both.")
    if i_wrong:
        return _decision(ReconciliationOutcome.CONFLICT, ["imported_not_expected_recording"],
                         "The imported file is a different recording than this slot expects; keep existing.")
    if e_ok and i_ok:
        return _decision(ReconciliationOutcome.KEEP_EXISTING, ["both_deterministically_expected_recording"],
                         "Imported file is a verified duplicate of the existing track.")
    if e_wrong and i_ok:
        return _decision(ReconciliationOutcome.KEEP_IMPORTED, ["existing_not_expected_recording",
                                                                "imported_deterministically_expected_recording"],
                         "Replace the existing file with the verified import.")
    reasons = []
    if "recording_id_conflict" in e_res.hard_conflicts:
        reasons.append("existing_recording_id_differs_from_expected")
    if not e_ok:
        reasons.append("existing_identity_not_deterministic")
    if not i_ok:
        reasons.append("imported_identity_not_deterministic")
    if e_wrong:
        reasons.append("existing_not_expected_recording")
    return _decision(ReconciliationOutcome.KEEP_BOTH_REVIEW, reasons,
                     "Keep both until a reviewer confirms which file is the expected recording.")


# ---------------------------------------------------------------------------
# Whole-album plan
# ---------------------------------------------------------------------------

@dataclass
class ReconciliationPlan:
    album: AlbumIdentity
    move_ids: List[int] = field(default_factory=list)
    duplicate_rows: List[Dict[str, Any]] = field(default_factory=list)
    replace_rows: List[Dict[str, Any]] = field(default_factory=list)
    mapping_pairs: List[Dict[str, Any]] = field(default_factory=list)
    survivors_by_slot: Dict[tuple, List[int]] = field(default_factory=dict)
    decisions: List[SlotDecision] = field(default_factory=list)
    held_item_ids: List[int] = field(default_factory=list)

    @property
    def review_decisions(self) -> List[SlotDecision]:
        return [d for d in self.decisions if not d.outcome.destructive]

    def counts(self) -> Dict[str, int]:
        out = {o.value: 0 for o in ReconciliationOutcome}
        for d in self.decisions:
            out[d.outcome.value] += 1
        out["moved"] = len(self.move_ids)
        out["held_for_review"] = len(self.held_item_ids)
        return out


def plan_reconciliation(existing_items: Iterable[Dict[str, Any]], imported_items: Iterable[Dict[str, Any]],
                        target_by_slot: Dict[tuple, Dict[str, Any]], album: AlbumIdentity, *,
                        forced_replace_ids: Iterable[int] = (),
                        hits_fn: Optional[HitsFn] = None,
                        exists_fn: Optional[ExistsFn] = None,
                        similarity_fn: Optional[SimilarityFn] = None) -> ReconciliationPlan:
    """Plan moving imported rows onto the existing album.

    Nothing moves unless album identity is proven. Imported rows in empty
    slots move. Contested slots follow ``decide_slot``; any non-destructive
    outcome holds the imported row where it is (both files and both rows
    preserved) and records a review decision.
    """
    plan = ReconciliationPlan(album=album)
    imported_rows = sorted(imported_items, key=lambda r: (*slot_key(r), _int(r.get("id"))))
    if not album.same_album:
        plan.held_item_ids = [_int(r.get("id")) for r in imported_rows]
        return plan
    forced = {_int(v) for v in forced_replace_ids if _int(v)}
    by_slot: Dict[tuple, List[Dict[str, Any]]] = {}
    for row in sorted(existing_items, key=lambda r: (*slot_key(r), _int(r.get("id")))):
        key = slot_key(row)
        if key[1]:
            by_slot.setdefault(key, []).append(row)

    for row in imported_rows:
        key = slot_key(row)
        occupants = by_slot.get(key, []) if key[1] else []
        if occupants:
            forced_rows = [ex for ex in occupants if _int(ex.get("id")) in forced]
            if forced_rows:
                # The format-replacement pipeline retires these rows through its
                # own verified transaction after this import; bookkeeping only.
                by_slot[key] = [ex for ex in occupants if _int(ex.get("id")) not in forced]
            else:
                decisions = [decide_slot(ex, row, target_by_slot.get(key), hits_fn=hits_fn,
                                         exists_fn=exists_fn, similarity_fn=similarity_fn) for ex in occupants]
                plan.decisions.extend(decisions)
                outcomes = {d.outcome for d in decisions}
                if outcomes == {ReconciliationOutcome.KEEP_EXISTING}:
                    plan.duplicate_rows.append(row)
                    plan.survivors_by_slot[key] = [_int(ex.get("id")) for ex in occupants]
                    continue
                if outcomes == {ReconciliationOutcome.KEEP_IMPORTED}:
                    plan.replace_rows.extend(occupants)
                    for ex in occupants:
                        plan.mapping_pairs.append({
                            "old_item_id": _int(ex.get("id")),
                            "new_item_id": _int(row.get("id")),
                            "identity_source": "canonical_recording_identity",
                        })
                    by_slot[key] = []
                else:
                    plan.held_item_ids.append(_int(row.get("id")))
                    continue
        plan.move_ids.append(_int(row.get("id")))
        if key[1]:
            by_slot.setdefault(key, []).append(row)
    return plan


# ---------------------------------------------------------------------------
# Review queue
# ---------------------------------------------------------------------------

_REVIEW_LOCK = threading.Lock()
_MAX_REVIEW_RECORDS = 2000


def review_store_path() -> Path:
    base = os.environ.get("WEB_MANAGER_DATA_DIR") or "/web-manager-data"
    return Path(base) / "import_reconciliation_reviews.json"


def load_reviews(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    target = path or review_store_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def record_reviews(plan: ReconciliationPlan, *, existing_album_id: int, imported_album_id: int,
                   release_id: str, source_folder: str, path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Persist one review record per non-destructive decision (and one for an
    unproven album identity). Atomic write; never raises into the import."""
    now = time.time()
    records: List[Dict[str, Any]] = []
    common = {
        "review_id": "",
        "created_at": now,
        "status": "open",
        "existing_album_id": int(existing_album_id or 0),
        "imported_album_id": int(imported_album_id or 0),
        "release_group_id": plan.album.release_group_id,
        "release_id": _id(release_id),
        "source_folder": _s(source_folder),
        "album_identity": plan.album.to_dict(),
    }
    if not plan.album.same_album:
        records.append({**common, "kind": "album_identity_unproven",
                        "held_item_ids": list(plan.held_item_ids),
                        "reasons": [plan.album.reason],
                        "recommended_action": "Confirm the release group of both albums before merging."})
    for decision in plan.review_decisions:
        records.append({**common, "kind": "contested_slot", **decision.to_dict()})
    if not records:
        return []
    for record in records:
        record["review_id"] = uuid.uuid4().hex
    target = path or review_store_path()
    try:
        with _REVIEW_LOCK:
            existing = load_reviews(target)
            merged = (existing + records)[-_MAX_REVIEW_RECORDS:]
            _write_reviews(merged, target)
    except Exception:
        return records
    return records


def _write_reviews(records: List[Dict[str, Any]], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".reconcile-", dir=str(target.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(records, fh, indent=1)
    os.replace(tmp, target)


RESOLUTION_CHOICES = ("keep_existing", "keep_imported", "keep_both")


def resolve_review(review_id: str, choice: str, engine: Any, *, path: Optional[Path] = None) -> Dict[str, Any]:
    """Apply a reviewer's decision for one open reconciliation review.

    ``keep_both`` records the decision and changes nothing. ``keep_existing``
    retires the imported copy and ``keep_imported`` retires the existing row
    and moves the import onto the existing album -- both only through the
    engine's controlled, rollback-capable transactions (existing album
    reconcile / bulk import replacement), never by direct file or DB access.
    An album-identity review can only be dismissed (keep both): merging
    albums whose Release Group is unproven is not offered.
    """
    choice = _s(choice).strip().lower()
    if choice not in RESOLUTION_CHOICES:
        return {"ok": False, "error": "choice must be keep_existing, keep_imported or keep_both", "code": "invalid_choice"}
    target = path or review_store_path()
    with _REVIEW_LOCK:
        records = load_reviews(target)
        record = next((r for r in records if r.get("review_id") == review_id), None)
        if record is None:
            return {"ok": False, "error": "review not found", "code": "not_found"}
        if record.get("status") != "open":
            return {"ok": False, "error": "review is already resolved", "code": "already_resolved"}
        if record.get("kind") != "contested_slot" and choice != "keep_both":
            return {"ok": False, "error": "album identity is unproven; only keep_both is allowed",
                    "code": "album_identity_unproven"}
        existing_id = _int(record.get("existing_item_id"))
        imported_id = _int(record.get("imported_item_id"))
        existing_album = _int(record.get("existing_album_id"))
        imported_album = _int(record.get("imported_album_id"))
        operations: List[str] = []
        moved_into_existing = False
        if choice == "keep_existing":
            plan = engine.plan_existing_album_reconcile({
                "imported_album_id": imported_album,
                "existing_album_id": existing_album,
                "dup_item_ids": [imported_id],
                "dup_details": [{"dup_item_id": imported_id, "survivor_item_ids": [existing_id]}],
                "move_item_ids": [],
                "source_folder": _s(record.get("source_folder")),
                "reason": "Reconciliation review: keep existing",
            })
            if not plan.get("ok"):
                return {"ok": False, "error": plan.get("error") or "reconcile plan rejected", "code": "engine_plan_failed"}
            applied = engine.apply_existing_album_reconcile(plan.get("operation_id"))
            if not applied.get("ok"):
                return {"ok": False, "error": applied.get("error") or "reconcile apply failed", "code": "engine_apply_failed"}
            operations.append(_s(plan.get("operation_id")))
        elif choice == "keep_imported":
            plan = engine.plan_bulk_import_replacement({
                "existing_album_id": existing_album,
                "old_item_ids": [existing_id],
                "mappings": [{"old_item_id": existing_id, "new_item_id": imported_id,
                              "identity_source": "user_reviewed_reconciliation"}],
                "source_folder": _s(record.get("source_folder")),
                "mb_albumid": _s(record.get("release_id")),
                "reason": "Reconciliation review: keep imported",
            })
            if not plan.get("ok"):
                return {"ok": False, "error": plan.get("error") or "replacement plan rejected", "code": "engine_plan_failed"}
            applied = engine.apply_bulk_import_replacement(plan.get("operation_id"))
            if not applied.get("ok"):
                return {"ok": False, "error": applied.get("error") or "replacement apply failed", "code": "engine_apply_failed"}
            operations.append(_s(plan.get("operation_id")))
            move = engine.plan_existing_album_reconcile({
                "imported_album_id": imported_album,
                "existing_album_id": existing_album,
                "dup_item_ids": [],
                "dup_details": [],
                "move_item_ids": [imported_id],
                "source_folder": _s(record.get("source_folder")),
                "reason": "Reconciliation review: move kept import",
            })
            if move.get("ok"):
                moved = engine.apply_existing_album_reconcile(move.get("operation_id"))
                if moved.get("ok"):
                    operations.append(_s(move.get("operation_id")))
                    moved_into_existing = True
        record.update({"status": "resolved", "resolution": choice, "resolved_at": time.time(),
                       "operation_ids": operations, "moved_into_existing_album": moved_into_existing})
        _write_reviews(records, target)
    return {"ok": True, "review_id": review_id, "resolution": choice, "operation_ids": operations,
            "moved_into_existing_album": moved_into_existing}
