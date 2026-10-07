"""Reviewed duplicate cleanup: Plan -> Approve -> Apply -> Verify -> Rollback.

The one way a proven duplicate FILE leaves the library. Every pair names the
copy to remove and the copy to keep. Planning re-resolves both items from
live Beets and re-proves the pair with the same rules the duplicate scan
uses (backend.duplicate_identity.plan_unattended_cleanup): both files
tracked and present, a shared AcoustID recording (or byte identity), the
same release slot, the named keeper still winning the keeper policy, the
album-slot gate, and no lossless rival of a lossy keeper. Against the
reviewed proposal it also checks paths, file size and slot evidence have
not drifted. A pair that fails any check is skipped and stays in review;
its validation is never relaxed.

Apply requires an Approved transaction. The Beets engine removes each row
and moves its file into the engine quarantine (never deleted), refusing the
whole request if any file's SHA-256 changed since planning. Afterwards the
keeper rows, files and album slots are verified and the library item count
must have dropped by exactly the number removed. Rollback restores the files
and rows through the engine.

The Web Manager never touches /music itself (it is mounted read-only).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import backend.duplicate_identity as duplicate_identity
from backend.beets_adapter import BeetsAdapter, beets_adapter
from backend.composite_workflows import _decode_path, _get_store, _s
from backend.transaction_engine import TransactionStore

REVIEWED_CLEANUP_FAMILY = "duplicate_cleanup_v1"

#: Identity a keeper must still carry after the cleanup.
KEEPER_IDENTITY_FIELDS = ("album_id", "mb_trackid", "mb_albumid", "mb_releasegroupid", "disc", "track")

FingerprintMatch = Callable[[str, str], Tuple[str, List[str], List[str]]]
AbsPath = Callable[[str], str]


def _item_view(item: Dict[str, Any]) -> SimpleNamespace:
    fields = {k: item.get(k) for k in ("id", "album_id", "mb_trackid", "mb_albumid", "mb_releasegroupid",
                                       "disc", "track", "format", "bitrate", "samplerate", "bitdepth")}
    return SimpleNamespace(**fields)


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _norm(path: Any) -> str:
    return str(Path(_s(path)).resolve(strict=False)) if _s(path) else ""


def _slot(item: Dict[str, Any]) -> Tuple[Any, Any, Any]:
    return (item.get("album_id") or None, item.get("disc"), item.get("track"))


def _identity(item: Dict[str, Any]) -> Dict[str, Any]:
    return {k: item.get(k) for k in KEEPER_IDENTITY_FIELDS}


def sibling_row(adapter: BeetsAdapter, drop: Dict[str, Any], keep: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The duplicate album row ``drop`` would empty, when that is safe to
    retire: ``drop`` is its ONLY item, and it is a different row of the
    keeper's own release (same non-empty Release ID and Release Group ID) with
    the keeper holding the same disc/track. None otherwise. Read-only."""
    drop_album, keep_album = drop.get("album_id"), keep.get("album_id")
    if not drop_album or not keep_album or int(drop_album) == int(keep_album):
        return None
    if (drop.get("disc"), drop.get("track")) != (keep.get("disc"), keep.get("track")):
        return None
    row, keeper_row = adapter.get_album(int(drop_album), expand=False), adapter.get_album(int(keep_album), expand=False)
    if not row or not keeper_row:
        return None
    ident = {k: _s(row.get(k)).lower() for k in ("mb_albumid", "mb_releasegroupid")}
    if not all(ident.values()) or ident != {k: _s(keeper_row.get(k)).lower() for k in ident}:
        return None
    members = [int(i.get("id")) for i in adapter.find_all_items_by_album_id(int(drop_album)) or []]
    if members != [int(drop.get("id"))]:
        return None
    return {"album_id": int(drop_album), **ident, "album": _s(row.get("album")), "keeper_album_id": int(keep_album)}


def verify_pair(
    pair: Dict[str, Any],
    *,
    adapter: BeetsAdapter,
    fingerprint_match: FingerprintMatch,
    abs_path: AbsPath,
    music_root: Path,
    path_under: Callable[[Path, Path], bool],
    allow_sibling_row_retire: bool = False,
) -> Dict[str, Any]:
    """Re-prove one reviewed pair against live Beets. Read-only.

    ``allow_sibling_row_retire`` (operator-reviewed cleanup only) lets the
    album-slot gate accept a copy that is the only item of a duplicate row of
    the keeper's release; that row is then retired with it (see sibling_row).

    Returns {"ok": True, ...evidence} or {"ok": False, "reasons": [...]}.
    """
    try:
        delete_id, keep_id = int(pair.get("delete_item_id")), int(pair.get("keep_item_id"))
    except (TypeError, ValueError):
        return {"ok": False, "reasons": ["invalid_item_ids"]}
    base = {"delete_item_id": delete_id, "keep_item_id": keep_id}
    if delete_id == keep_id:
        return {**base, "ok": False, "reasons": ["same_item"]}
    drop, keep = adapter.get_item(delete_id), adapter.get_item(keep_id)
    reasons: List[str] = []
    if not drop:
        reasons.append("delete_item_missing")
    if not keep:
        reasons.append("keep_item_missing")
    if reasons:
        return {**base, "ok": False, "reasons": reasons}

    drop_path, keep_path = abs_path(_decode_path(drop.get("path"))), abs_path(_decode_path(keep.get("path")))
    for label, path in (("delete", drop_path), ("keep", keep_path)):
        if not path or not Path(path).is_file():
            reasons.append(f"{label}_file_missing")
    if reasons:
        return {**base, "ok": False, "reasons": reasons}

    expected = pair.get("expected") or {}
    exp_drop, exp_keep = expected.get("delete") or {}, expected.get("keep") or {}
    if exp_drop.get("path") and _norm(exp_drop["path"]) != _norm(drop_path):
        reasons.append("delete_path_changed")
    if exp_keep.get("path") and _norm(exp_keep["path"]) != _norm(keep_path):
        reasons.append("keep_path_changed")
    if exp_drop.get("size") is not None and int(exp_drop["size"]) != Path(drop_path).stat().st_size:
        reasons.append("delete_file_changed_since_proposal")
    for label, exp, live in (("delete", exp_drop, drop), ("keep", exp_keep, keep)):
        if exp and "track" in exp and (exp.get("album_id") or None, exp.get("disc"), exp.get("track")) != _slot(live):
            reasons.append(f"{label}_release_slot_changed")

    shared, drop_ids, keep_ids = fingerprint_match(drop_path, keep_path)
    shared = _s(shared).lower()
    if not shared:
        reasons.append("no_shared_fingerprint_recording")
    elif _s((expected.get("fingerprint") or {}).get("shared_recording_id")).lower() not in ("", shared):
        reasons.append("shared_recording_changed")
    embedded = {_s(drop.get("mb_trackid")).lower(), _s(keep.get("mb_trackid")).lower()} - {""}
    if shared and embedded and embedded != {shared}:
        reasons.append("embedded_recording_id_contradicts_fingerprint")

    drop_view, keep_view = _item_view(drop), _item_view(keep)
    relation = duplicate_identity.release_relation(drop_view, keep_view)
    if expected.get("release_relation") and expected["release_relation"] != relation:
        reasons.append("release_relation_changed")
    if reasons:
        return {**base, "ok": False, "reasons": reasons, "release_relation": relation}

    # The scan's own policy decides: proof, same slot, keeper, album-slot gate,
    # lossless rival. Anything but "delete this, keep that" goes back to review.
    record = {
        "source_path": drop_path, "lib_path": keep_path,
        "source_item_id": delete_id, "lib_id": keep_id,
        "source_album_id": drop.get("album_id"), "lib_album_id": keep.get("album_id"),
        "source_meta": duplicate_identity.copy_meta(drop_view), "lib_meta": duplicate_identity.copy_meta(keep_view),
        "source_fingerprint_ids": list(drop_ids or []), "lib_fingerprint_ids": list(keep_ids or []),
        "fingerprint_mbid": shared, "fingerprint_verified": True,
        "release_relation": relation, "match_type": "reviewed pair", "confidence": "high",
    }
    retire_row = sibling_row(adapter, drop, keep) if allow_sibling_row_retire else None
    decisions = duplicate_identity.plan_unattended_cleanup(
        {"duplicates": [record]}, music_root, path_under,
        sibling_row_retire=(lambda _d, _k: True) if retire_row else None)
    if len(decisions) != 1:
        return {**base, "ok": False, "release_relation": relation,
                "reasons": ["policy_no_longer_selects_this_pair (slot, album-slot gate or proof)"]}
    decision = decisions[0]
    if decision.get("action") != "delete":
        return {**base, "ok": False, "release_relation": relation, "reasons": ["replacement_review_required"]}
    if int(decision["keep"]["item_id"]) != keep_id or int(decision["delete"]["item_id"]) != delete_id:
        return {**base, "ok": False, "release_relation": relation, "reasons": ["keeper_policy_now_prefers_the_other_copy"]}

    return {
        **base, "ok": True,
        "delete_path": drop_path, "keep_path": keep_path,
        "delete_size": Path(drop_path).stat().st_size,
        "delete_sha256": _sha256(drop_path),
        "shared_recording_id": shared,
        "release_relation": relation,
        "keep_reason": decision.get("keep_reason") or "",
        "keep_identity": _identity(keep),
        "delete_identity": _identity(drop),
        **({"retire_album": retire_row} if decision.get("retire_album_id") else {}),
    }


def _default_deps() -> Dict[str, Any]:
    from backend.acoustid_service import _acoustid_fingerprint_match, _album_item_abs_path
    from backend.app_runtime import MUSIC_ROOT, _path_under
    return {"fingerprint_match": _acoustid_fingerprint_match, "abs_path": _album_item_abs_path,
            "music_root": MUSIC_ROOT, "path_under": _path_under}


def plan_reviewed_cleanup(
    pairs: Iterable[Dict[str, Any]],
    *,
    reason: str = "",
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    deps: Optional[Dict[str, Any]] = None,
    allow_sibling_row_retire: bool = False,
) -> Dict[str, Any]:
    """Re-verify reviewed pairs and create a Preview transaction. Read-only.

    ``allow_sibling_row_retire`` is for the operator-reviewed plan route only;
    the unattended and bulk paths never pass it (see verify_pair)."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    d = deps or _default_deps()
    accepted: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    removing: set = set()
    keeping: set = set()
    for pair in pairs:
        result = verify_pair(pair, adapter=ad, allow_sibling_row_retire=allow_sibling_row_retire, **d)
        if result.get("ok") and (result["delete_item_id"] in keeping or result["keep_item_id"] in removing
                                 or result["delete_item_id"] in removing):
            result = {**result, "ok": False, "reasons": ["pair_overlaps_another_pair_in_this_plan"]}
        if result.get("ok"):
            accepted.append(result)
            removing.add(result["delete_item_id"])
            keeping.add(result["keep_item_id"])
        else:
            skipped.append(result)
    if not accepted:
        return {"ok": False, "code": "nothing_verified", "error": "No pair passed re-verification.",
                "skipped": skipped}
    changes = [{
        "action": "quarantine_remove",
        "delete_item_id": p["delete_item_id"], "delete_path": p["delete_path"], "delete_size": p["delete_size"],
        "keep_item_id": p["keep_item_id"], "keep_path": p["keep_path"],
        "shared_recording_id": p["shared_recording_id"], "release_relation": p["release_relation"],
        "keep_reason": p["keep_reason"],
        **({"retire_album_id": p["retire_album"]["album_id"]} if p.get("retire_album") else {}),
    } for p in accepted]
    tx = st.create(
        operation_type="Delete",
        status="Preview",
        summary=f"Quarantine {len(accepted)} reviewed duplicate file(s); keep their proven twins",
        changes=changes,
        rollback_available=True,
        metadata={"mutation_family": REVIEWED_CLEANUP_FAMILY, "pairs": accepted, "skipped": skipped,
                  "reason": _s(reason)},
    )
    return {"ok": True, "operation_id": tx["id"], "status": "Preview", "requires_approval": True,
            "pairs": accepted, "skipped": skipped}


def apply_reviewed_cleanup(
    operation_id: str,
    *,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    abs_path: Optional[AbsPath] = None,
) -> Dict[str, Any]:
    """Apply an Approved reviewed cleanup through the engine, then verify."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    to_abs = abs_path or _default_deps()["abs_path"]
    try:
        tx = st.get(operation_id)
    except KeyError:
        return {"ok": False, "code": "not_found", "error": "Transaction not found"}
    meta = tx.get("metadata") or {}
    if meta.get("mutation_family") != REVIEWED_CLEANUP_FAMILY:
        return {"ok": False, "code": "wrong_family", "error": "Not a reviewed duplicate cleanup transaction."}
    if meta.get("engine_result"):
        return {"ok": False, "code": "already_applied", "error": "This cleanup was already applied."}
    if tx.get("status") != "Approved":
        return {"ok": False, "code": "not_approved", "error": "Approve the transaction before applying it."}
    pairs = meta.get("pairs") or []
    ids = sorted({int(p["delete_item_id"]) for p in pairs} | {int(p["keep_item_id"]) for p in pairs})
    rows = sorted({int(p["retire_album"][k]) for p in pairs if p.get("retire_album")
                   for k in ("album_id", "keeper_album_id")})
    from backend.resource_locks import attempt_owner, claim_approved, claim_refusal, locks as resource_locks
    with resource_locks().hold([f"album:{a}" for a in rows] + [f"item:{i}" for i in ids],
                               attempt_owner(operation_id), timeout=10):
        if claim_approved(st, operation_id) is None:
            return {"ok": False, "code": "not_approved", "error": claim_refusal(st, operation_id)}
        stats = ad.get_stats() or {}
        items_before, albums_before = int(stats.get("items") or 0), int(stats.get("albums") or 0)
        # Recorded before the engine call: a restart mid-call is finished from
        # engine evidence by backend/transaction_recovery.py, never replayed.
        st.update(operation_id, status="Running",
                  metadata={"engine_request": {"items_before": items_before, "albums_before": albums_before}})
        try:
            res = ad.quarantine_remove_items(
                [{"item_id": p["delete_item_id"], "sha256": p["delete_sha256"],
                  **({"retire_album_id": p["retire_album"]["album_id"], "sibling_keeper_item_id": p["keep_item_id"]}
                     if p.get("retire_album") else {})} for p in pairs],
                idempotency_key=operation_id,
            )
        except Exception:
            st.update(operation_id, status="Failed", logs=["Engine quarantine-remove failed; nothing was removed."])
            raise
        return finish_reviewed_cleanup(operation_id, res, adapter=ad, store=st, abs_path=to_abs)


def finish_reviewed_cleanup(
    operation_id: str,
    res: Dict[str, Any],
    *,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    abs_path: Optional[AbsPath] = None,
) -> Dict[str, Any]:
    """Verify an applied cleanup from the engine's result and record the
    outcome (also used by restart recovery -- never re-applies)."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    to_abs = abs_path or _default_deps()["abs_path"]
    meta = st.get(operation_id).get("metadata") or {}
    pairs = meta.get("pairs") or []
    items_before = int(((meta.get("engine_request") or {}).get("items_before")) or 0)
    engine = res.get("result") if isinstance(res.get("result"), dict) else res

    problems: List[str] = []
    verified: List[Dict[str, Any]] = []
    for p in pairs:
        keep = ad.get_item(int(p["keep_item_id"])) or {}
        row = {"delete_item_id": p["delete_item_id"], "keep_item_id": p["keep_item_id"]}
        if ad.get_item(int(p["delete_item_id"])):
            problems.append(f"item {p['delete_item_id']} still in library")
        if not keep:
            problems.append(f"keeper {p['keep_item_id']} missing")
        else:
            drift = [k for k in KEEPER_IDENTITY_FIELDS if _s(keep.get(k)) != _s((p.get("keep_identity") or {}).get(k))]
            if drift:
                problems.append(f"keeper {p['keep_item_id']} identity changed: {', '.join(drift)}")
            if not Path(to_abs(_decode_path(keep.get("path")))).is_file():
                problems.append(f"keeper {p['keep_item_id']} file missing")
            if keep.get("album_id"):
                members = {int(i.get("id")) for i in ad.find_all_items_by_album_id(int(keep["album_id"])) or []}
                if int(p["keep_item_id"]) not in members:
                    problems.append(f"album {keep['album_id']} slot lost its keeper")
            row["keep_identity_after"] = _identity(keep)
        verified.append(row)
    retired = [int(p["retire_album"]["album_id"]) for p in pairs if p.get("retire_album")]
    for album_id in retired:
        if ad.get_album(album_id, expand=False):
            problems.append(f"duplicate album row {album_id} was not retired")
    stats = ad.get_stats() or {}
    items_after, albums_after = int(stats.get("items") or 0), int(stats.get("albums") or 0)
    if items_before and items_before - items_after != len(pairs):
        problems.append(f"library item count changed by {items_before - items_after}, expected {len(pairs)}")
    albums_before = int(((meta.get("engine_request") or {}).get("albums_before")) or 0)
    if albums_before and albums_before - albums_after != len(retired):
        problems.append(f"album count changed by {albums_before - albums_after}, expected {len(retired)}")

    status = "Completed" if not problems else "Recovery Required"
    st.update(
        operation_id,
        status=status,
        metadata={**meta, "engine_result": engine, "verified": verified, "verification_problems": problems,
                  "items_before": items_before, "items_after": items_after},
        logs=[f"Quarantined item {r.get('item_id')}: {r.get('quarantine_path')}" for r in engine.get("removed") or []]
        + ([f"Verification problem: {x}" for x in problems] if problems else []),
    )
    return {"ok": not problems, "operation_id": operation_id, "status": status,
            "quarantine_id": engine.get("quarantine_id"), "removed": engine.get("removed") or [],
            "retired_album_ids": retired,
            "items_before": items_before, "items_after": items_after, "verification_problems": problems}


def rollback_reviewed_cleanup(
    operation_id: str,
    *,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
) -> Dict[str, Any]:
    ad = adapter or beets_adapter
    st = _get_store(store)
    try:
        tx = st.get(operation_id)
    except KeyError:
        return {"ok": False, "code": "not_found", "error": "Transaction not found"}
    meta = tx.get("metadata") or {}
    engine = meta.get("engine_result") or {}
    if meta.get("mutation_family") != REVIEWED_CLEANUP_FAMILY or not engine.get("quarantine_id"):
        return {"ok": False, "code": "not_applied", "error": "No applied reviewed cleanup to roll back."}
    if tx.get("status") == "Rolled Back":
        return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}
    res = ad.rollback_quarantine_remove_items(engine["quarantine_id"], idempotency_key=f"{operation_id}:rollback")
    result = res.get("result") if isinstance(res.get("result"), dict) else res
    problems: List[str] = []
    for p in meta.get("pairs") or []:
        row = p.get("retire_album")
        if not row:
            continue
        album = ad.get_album(int(row["album_id"]), expand=False) or {}
        if _s(album.get("mb_albumid")).lower() != row["mb_albumid"]:
            problems.append(f"album row {row['album_id']} was not restored")
        members = [int(i.get("id")) for i in ad.find_all_items_by_album_id(int(row["album_id"])) or []]
        if members != [int(p["delete_item_id"])]:
            problems.append(f"item {p['delete_item_id']} is not back in album row {row['album_id']}")
    status = "Rolled Back" if not problems else "Recovery Required"
    st.update(operation_id, status=status, metadata={**meta, "rollback_result": result, "rollback_problems": problems},
              logs=[f"Restored item {r.get('old_item_id')} as {r.get('new_item_id')} at {r.get('path')}"
                    for r in result.get("restored") or []] + [f"Rollback problem: {x}" for x in problems])
    return {"ok": not problems, "operation_id": operation_id, "status": status,
            "restored": result.get("restored") or [], "rollback_problems": problems}


def pairs_from_proposal(proposal: Iterable[Dict[str, Any]],
                        wanted: Optional[Iterable[Tuple[int, int]]] = None) -> List[Dict[str, Any]]:
    """Reviewed pairs (with their proposal evidence) from a scan proposal.

    Only rows with action "delete" qualify; ``wanted`` narrows to the given
    (delete_item_id, keep_item_id) pairs.
    """
    want = {(int(a), int(b)) for a, b in wanted} if wanted else None
    out = []
    for row in proposal or []:
        if row.get("action", "delete") != "delete":
            continue
        drop, keep = row.get("delete") or {}, row.get("keep") or {}
        try:
            key = (int(drop.get("item_id")), int(keep.get("item_id")))
        except (TypeError, ValueError):
            continue
        if want is not None and key not in want:
            continue
        out.append({"delete_item_id": key[0], "keep_item_id": key[1], "expected": {
            "delete": drop, "keep": keep, "fingerprint": row.get("fingerprint") or {},
            "release_relation": row.get("release_relation") or "",
        }})
    return out
