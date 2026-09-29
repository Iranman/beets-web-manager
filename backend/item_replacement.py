"""The one authority for replacing an album slot's audio file.

Every production path that swaps a library item's file -- the manual item
replacement route, the music-format quality pipeline, import album merges
and reconciliation reviews -- plans through ``plan_verified_replacement``
and applies through the canonical item-file replacement transaction
(backend.composite_workflows, engine op /webmanager/replace-item-file):

    Plan (this module, read-only) -> Approve -> Apply -> Verify -> Rollback

Identity rules (never relaxed):

* The replacement must be a tracked library item; untracked/staged files
  are not supported (that would need a local write into /music).
* Audio decides. The replacement is accepted only when AcoustID shows it is
  the same recording as the slot: a recording shared with the current file,
  or -- when the current file is missing or is itself the wrong recording --
  the slot's expected Recording ID among the replacement's fingerprint
  recordings. Text/title similarity never counts.
* A fingerprint disagreement, or no fingerprint at all, fails closed.
* The slot keeps its identity (Recording, Release, Release Group, disc,
  track); if the caller names an expected recording and the slot row
  carries a different one, the plan fails closed.
* An occupied canonical destination may only be displaced when its decoded
  audio is identical (backend.replacement_service), else fail closed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.beets_adapter import BeetsAdapter, beets_adapter
import backend.composite_workflows as composite_workflows
from backend.composite_workflows import _decode_path, _s
from backend.transaction_engine import TransactionStore

FingerprintMatch = Callable[[str, str], Tuple[str, List[str], List[str]]]
FingerprintIds = Callable[[str], List[str]]


def _default_deps() -> Dict[str, Any]:
    from backend.acoustid_service import _acoustid_fingerprint_ids, _acoustid_fingerprint_match, _album_item_abs_path
    from backend.replacement_service import _music_format_replacement_matching_contract, _replacement_destination_check
    return {
        "fingerprint_match": _acoustid_fingerprint_match,
        "fingerprint_ids": _acoustid_fingerprint_ids,
        "abs_path": _album_item_abs_path,
        "destination_check": _replacement_destination_check,
        "matching_contract": _music_format_replacement_matching_contract,
    }


def _fail(code: str, error: str, **extra: Any) -> Dict[str, Any]:
    return {"ok": False, "code": code, "error": error, **extra}


def plan_verified_replacement(
    target_item_id: int,
    source_item_id: int,
    *,
    reason: str,
    expected_recording_id: str = "",
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    deps: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Prove and preview replacing ``target_item_id``'s file with
    ``source_item_id``'s. Read-only; returns the Preview transaction."""
    ad = adapter or beets_adapter
    d = deps or _default_deps()
    try:
        target_id, source_id = int(target_item_id), int(source_item_id)
    except (TypeError, ValueError):
        return _fail("invalid_items", "Item ids must be integers.")
    if not target_id or not source_id or target_id == source_id:
        return _fail("invalid_items", "A distinct original item and replacement item are required.")
    target, source = ad.get_item(target_id), ad.get_item(source_id)
    if not target:
        return _fail("item_not_found", f"Item {target_id} not found in library")
    if not source:
        return _fail("item_not_found", f"Replacement item {source_id} not found in library")
    if not target.get("album_id"):
        return _fail("target_not_in_album", f"Item {target_id} is not attached to an album slot")

    target_path = d["abs_path"](_decode_path(target.get("path")))
    source_path = d["abs_path"](_decode_path(source.get("path")))
    if not source_path or not Path(source_path).is_file():
        return _fail("replacement_file_missing", "The replacement item's file is not accessible.")
    target_present = bool(target_path) and Path(target_path).is_file()

    slot_rid = _s(target.get("mb_trackid")).strip().lower()
    expected = _s(expected_recording_id).strip().lower()
    if expected and slot_rid and expected != slot_rid:
        return _fail("target_identity_mismatch",
                     "The slot carries a different Recording ID than the expected recording; review it first.",
                     slot_recording_id=slot_rid, expected_recording_id=expected)
    expected = expected or slot_rid

    shared, source_ids, target_ids = "", [], []
    if target_present:
        shared, source_ids, target_ids = d["fingerprint_match"](source_path, target_path)
    else:
        source_ids = d["fingerprint_ids"](source_path)
    shared = _s(shared).lower()
    source_set = {_s(x).lower() for x in source_ids or []}
    if shared:
        validation = {"fingerprint_status": "matched", "mb_recording_id_candidate": shared,
                      "decision_reason": f"Replacement AcoustID fingerprint matches original recording {shared}."}
    elif expected and expected in source_set:
        validation = {"fingerprint_status": "matched", "mb_recording_id_candidate": expected,
                      "decision_reason": "Replacement AcoustID fingerprint matches the slot's expected recording."}
    else:
        code = "fingerprint_disagreement" if source_set else "fingerprint_unavailable"
        return _fail(code, "Could not verify by AcoustID that the replacement is the slot's recording; "
                           "refusing to plan (both files stay).",
                     fingerprint={"replacement_recording_ids": sorted(source_set)[:5],
                                  "current_recording_ids": [_s(x).lower() for x in (target_ids or [])][:5],
                                  "expected_recording_id": expected})

    displace = None
    if target_present:
        destination = d["destination_check"](target_path, source_path)
        if not destination.get("ok"):
            return _fail(destination.get("code") or "destination_occupied", destination.get("error") or "",
                         destination=destination.get("destination"))
        displace = destination.get("displace")

    contract = d["matching_contract"](
        {"mb_trackid": _s(target.get("mb_trackid")),
         "mb_releasegroupid": _s(target.get("mb_releasegroupid"))},
        {"fingerprint_validation": validation},
    )
    res = composite_workflows.plan_track_replacement({
        "original_item_id": target_id,
        "replacement_item_id": source_id,
        "displace_destination": displace,
        "fingerprint_validation": validation,
        "matching_contract": contract,
        "reason": _s(reason),
    }, adapter=ad, store=store)
    if res.get("ok"):
        res["fingerprint_validation"] = validation
    return res


def tracked_item_id_for_path(path: str, *, adapter: Optional[BeetsAdapter] = None,
                             abs_path: Optional[Callable[[str], str]] = None) -> int:
    """The id of the tracked item whose file is ``path`` (0 if untracked)."""
    ad = adapter or beets_adapter
    to_abs = abs_path or _default_deps()["abs_path"]
    wanted = str(Path(_s(path)).resolve(strict=False)) if _s(path) else ""
    if not wanted:
        return 0
    for item in ad.get_items() or []:
        candidate = to_abs(_decode_path(item.get("path")))
        if candidate and str(Path(candidate).resolve(strict=False)) == wanted:
            return int(item.get("id") or 0)
    return 0


def approve_and_apply(operation_id: str, *, approved_by: str,
                      adapter: Optional[BeetsAdapter] = None,
                      store: Optional[TransactionStore] = None) -> Dict[str, Any]:
    """Record an explicit human approval, then apply and verify.

    Only for callers that already hold a human decision for this exact
    replacement (e.g. a reviewer resolving a reconciliation review)."""
    st = composite_workflows._get_store(store)
    st.update(operation_id, status="Approved", metadata={"approved_by": _s(approved_by)})
    return composite_workflows.apply_track_replacement(operation_id, adapter=adapter, store=store)
