"""Merge duplicate album rows of one canonical album (ARCH-020).

Plan -> Approve -> Apply -> Verify -> Rollback, backed by the engine op
/webmanager/album-row-merge (beetsplug/webmanager/merge_ops.py).

Plan re-runs the read-only duplicate-album analysis for one Release Group
against live Beets and refuses anything but a deterministic group: one
edition (Release ID) across the rows, complementary slots only, every item
positioned. It records each moving item's identity and file SHA-256. Apply
needs an Approved transaction, holds the durable locks
``album-merge:<rg>`` and ``album:<id>`` for every row, records the engine
request before calling the engine (so a crash is recoverable, never
replayed blindly) and verifies afterwards: every item in the retained row
with unchanged identity, the source rows retired, the album count down by
exactly that many. Rollback asks the engine to restore the original rows
(same album ids) and verifies the restoration.

plan_rows_merge() is the same contract for an explicit target row and source
rows (the Clean and import callers), optionally for only some of a source
row's items ("partial": the row is retired only if all its items move). It
never rewrites Release, Release Group or Recording IDs, tags or paths; rows
of another edition, an unproven release or an already-filled slot are refused
and stay for review.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import backend.album_duplicate_analysis as album_duplicate_analysis
from backend.beets_adapter import BeetsAdapter, beets_adapter
from backend.composite_workflows import _decode_path, _get_store, _s
from backend.resource_locks import approve_preview, attempt_owner, claim_approved, claim_refusal, locks as resource_locks
from backend.transaction_engine import TransactionStore

ALBUM_ROW_MERGE_FAMILY = "album_row_merge_v1"
IDENTITY_FIELDS = ("mb_trackid", "mb_albumid", "mb_releasegroupid", "disc", "track")


def merge_id_for(operation_id: str) -> str:
    """The engine's manifest id for this transaction (see merge_ops.merge_id_for)."""
    return hashlib.sha256(("album-row-merge|" + operation_id).encode("utf-8")).hexdigest()[:32]


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identity(item: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _s(item.get(k)).lower() if k.startswith("mb_") else item.get(k) for k in IDENTITY_FIELDS}


def _default_abs_path() -> Callable[[str], str]:
    from backend.acoustid_service import _album_item_abs_path
    return _album_item_abs_path


def _default_cached_ids():
    from backend.acoustid_service import _acoustid_cached_fingerprint_ids
    return _acoustid_cached_fingerprint_ids


def plan_album_row_merge(release_group_id: str, *, adapter: Optional[BeetsAdapter] = None,
                         store: Optional[TransactionStore] = None,
                         abs_path: Optional[Callable[[str], str]] = None, cached_ids=None) -> Dict[str, Any]:
    ad = adapter or beets_adapter
    st = _get_store(store)
    to_abs = abs_path or _default_abs_path()
    rg = _s(release_group_id).lower()
    albums = [a for a in (ad.find_all_albums_by_releasegroupid(rg) or []) if _s(a.get("mb_releasegroupid")).lower() == rg]
    if len(albums) < 2:
        return {"ok": False, "code": "no_duplicate_rows", "error": "This Release Group has no duplicate album rows."}
    ids = {int(a["id"]) for a in albums}
    items = [i for i in (ad.get_items() or []) if int(i.get("album_id") or 0) in ids]
    report = album_duplicate_analysis.analyze(albums, items, cached_ids=cached_ids or _default_cached_ids(),
                                              abs_path=to_abs)
    group = next((g for g in report["groups"] if g["release_group_id"] == rg), None)
    if group is None:
        return {"ok": False, "code": "no_duplicate_rows", "error": "This Release Group has no duplicate album rows."}
    if not group["deterministic"]:
        return {"ok": False, "code": "review_only", "error": "Not a deterministic merge; review the blockers.",
                "blockers": group["blockers"]}
    target_id = int(group["retain_album_id"])
    source_ids = sorted(i for i in ids if i != target_id)
    release_id = _s(next(a for a in albums if int(a["id"]) == target_id).get("mb_albumid")).lower()
    # A retained row whose files are gone is stale: recover it before merging
    # more into it (the analysis itself does not read the disk).
    stale = sorted(int(i["id"]) for i in items if int(i.get("album_id") or 0) == target_id
                   and not Path(to_abs(_decode_path(i.get("path"))) or "").is_file())
    if stale:
        return {"ok": False, "code": "retained_row_file_missing", "item_ids": stale,
                "error": f"{len(stale)} item(s) of the retained row have no file on disk; recover them first."}
    moving = []
    for it in sorted((i for i in items if int(i.get("album_id") or 0) in source_ids), key=lambda r: int(r["id"])):
        path = to_abs(_decode_path(it.get("path")))
        if not path or not Path(path).is_file():
            return {"ok": False, "code": "file_missing", "error": f"Item {it['id']}'s file is not accessible."}
        moving.append({"item_id": int(it["id"]), "source_album_id": int(it["album_id"]), "sha256": _sha256(path),
                       "mb_trackid": _s(it.get("mb_trackid")), "disc": it.get("disc"), "track": it.get("track"),
                       "identity": _identity(it), "path": path})
    tx = st.create(
        operation_type="Merge Album",
        status="Preview",
        summary=f"Merge {len(source_ids)} duplicate album row(s) into album {target_id} "
                f"({group['albumartist']} - {group['album']})",
        changes=[{"item_id": m["item_id"], "from_album_id": m["source_album_id"], "to_album_id": target_id,
                  "disc": m["disc"], "track": m["track"], "path": m["path"]} for m in moving]
        + [{"retire_album_id": sid} for sid in source_ids],
        rollback_available=True,
        metadata={"mutation_family": ALBUM_ROW_MERGE_FAMILY, "release_group_id": rg, "release_id": release_id,
                  "target_album_id": target_id, "source_album_ids": source_ids, "items": moving,
                  "analysis": {k: group[k] for k in ("complementary_slots", "editions", "retain_reason")}},
    )
    return {"ok": True, "operation_id": tx["id"], "status": "Preview", "requires_approval": True,
            "target_album_id": target_id, "source_album_ids": source_ids,
            "moves": [{k: m[k] for k in ("item_id", "source_album_id", "disc", "track", "path")} for m in moving]}


def _fail(code: str, error: str, **extra: Any) -> Dict[str, Any]:
    return {"ok": False, "code": code, "error": error, **extra}


def plan_rows_merge(target_album_id: int, source_album_ids: Iterable[int], item_ids: Optional[Iterable[int]] = None, *,
                    reason: str = "", adapter: Optional[BeetsAdapter] = None,
                    store: Optional[TransactionStore] = None,
                    abs_path: Optional[Callable[[str], str]] = None) -> Dict[str, Any]:
    """Plan step (no mutation) for an explicit target row and source rows.

    ``item_ids`` limits the move to those items of the source rows."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    to_abs = abs_path or _default_abs_path()
    try:
        target_id = int(target_album_id)
        source_ids = sorted({int(x) for x in source_album_ids or []} - {target_id})
        wanted = None if item_ids is None else {int(x) for x in item_ids}
    except (TypeError, ValueError):
        return _fail("invalid_ids", "Album and item ids must be integers.")
    if not target_id or not source_ids:
        return _fail("invalid_ids", "A target album row and at least one other source row are required.")
    rows = {aid: ad.get_album(aid, expand=False) for aid in [target_id] + source_ids}
    missing = [aid for aid, row in rows.items() if not row]
    if missing:
        return _fail("album_missing", f"Album row(s) not found: {missing}")
    rg = _s(rows[target_id].get("mb_releasegroupid")).lower()
    release_id = _s(rows[target_id].get("mb_albumid")).lower()
    if not rg or not release_id:
        return _fail("identity_required", "The retained row has no Release Group ID and Release ID; rows are only "
                                          "merged within one proven release.")
    for sid in source_ids:
        if _s(rows[sid].get("mb_releasegroupid")).lower() != rg:
            return _fail("release_group_mismatch", f"Album row {sid} is not in the retained row's Release Group.")
        if _s(rows[sid].get("mb_albumid")).lower() != release_id:
            return _fail("edition_differs", f"Album row {sid} is a different edition (Release ID); not merged.")

    by_source = {sid: list(ad.find_all_items_by_album_id(sid) or []) for sid in source_ids}
    in_sources = {int(i["id"]): i for rows_ in by_source.values() for i in rows_}
    if wanted is None:
        wanted = set(in_sources)
    elif not wanted <= set(in_sources):
        return _fail("item_not_in_source", f"Item(s) {sorted(wanted - set(in_sources))} are not in the source rows.")
    if not wanted:
        return _fail("nothing_to_move", "No items to move.")
    partial = wanted != set(in_sources)
    retire = [sid for sid in source_ids if by_source[sid] and all(int(i["id"]) in wanted for i in by_source[sid])]

    slots = {(int(i.get("disc") or 1), int(i.get("track") or 0))
             for i in ad.find_all_items_by_album_id(target_id) or []}
    moving = []
    for item_id in sorted(wanted):
        it = in_sources[item_id]
        slot = (int(it.get("disc") or 1), int(it.get("track") or 0))
        if not slot[1]:
            return _fail("unpositioned_item", f"Item {item_id} has no track position.")
        if slot in slots:
            return _fail("slot_overlap", f"Slot {slot[0]}/{slot[1]} is already filled in the retained row; "
                                         "that is a duplicate-review case, not a merge.", item_id=item_id)
        slots.add(slot)
        if _s(it.get("mb_albumid")).lower() not in ("", release_id):
            return _fail("edition_differs", f"Item {item_id} carries a different Release ID.")
        path = to_abs(_decode_path(it.get("path")))
        if not path or not Path(path).is_file():
            return _fail("file_missing", f"Item {item_id}'s file is not accessible.")
        moving.append({"item_id": item_id, "source_album_id": int(it["album_id"]), "sha256": _sha256(path),
                       "mb_trackid": _s(it.get("mb_trackid")), "disc": it.get("disc"), "track": it.get("track"),
                       "identity": _identity(it), "path": path})
    tx = st.create(
        operation_type="Merge Album",
        status="Preview",
        summary=f"Move {len(moving)} item(s) from album row(s) {source_ids} into album {target_id} "
                f"({_s(rows[target_id].get('albumartist'))} - {_s(rows[target_id].get('album'))})",
        changes=[{"item_id": m["item_id"], "from_album_id": m["source_album_id"], "to_album_id": target_id,
                  "disc": m["disc"], "track": m["track"], "path": m["path"]} for m in moving]
        + [{"retire_album_id": sid} for sid in retire],
        rollback_available=True,
        metadata={"mutation_family": ALBUM_ROW_MERGE_FAMILY, "release_group_id": rg, "release_id": release_id,
                  "target_album_id": target_id, "source_album_ids": source_ids, "items": moving,
                  "partial": partial, "retire_album_ids": retire, "reason": _s(reason)},
    )
    return {"ok": True, "operation_id": tx["id"], "status": "Preview", "requires_approval": True,
            "target_album_id": target_id, "source_album_ids": source_ids, "retire_album_ids": retire,
            "partial": partial,
            "moves": [{k: m[k] for k in ("item_id", "source_album_id", "disc", "track", "path")} for m in moving]}


def approve_and_apply(operation_id: str, *, approved_by: str, adapter: Optional[BeetsAdapter] = None,
                      store: Optional[TransactionStore] = None) -> Dict[str, Any]:
    """Record an explicit approval, then apply and verify. Only for callers
    that already hold a human decision for this exact move (an operator's
    merge request, an import the operator started)."""
    st = _get_store(store)
    try:
        if approve_preview(st, operation_id, _s(approved_by)) is None:
            return _fail("not_preview", "Only a Preview transaction can be approved; it was cancelled or already finished.")
    except KeyError:
        return _fail("not_found", "Transaction not found")
    return apply_album_row_merge(operation_id, adapter=adapter, store=store)


def apply_album_row_merge(operation_id: str, *, adapter: Optional[BeetsAdapter] = None,
                          store: Optional[TransactionStore] = None) -> Dict[str, Any]:
    ad = adapter or beets_adapter
    st = _get_store(store)
    try:
        tx = st.get(operation_id)
    except KeyError:
        return {"ok": False, "code": "not_found", "error": "Transaction not found"}
    meta = tx.get("metadata") or {}
    if meta.get("mutation_family") != ALBUM_ROW_MERGE_FAMILY:
        return {"ok": False, "code": "wrong_family", "error": "Not an album row merge transaction."}
    if meta.get("engine_result"):
        return {"ok": False, "code": "already_applied", "error": "This merge was already applied."}
    if tx.get("status") != "Approved":
        return {"ok": False, "code": "not_approved", "error": "Approve the transaction before applying it."}
    keys = [f"album-merge:{meta['release_group_id']}"] + [f"album:{a}" for a in
                                                           sorted([meta["target_album_id"]] + meta["source_album_ids"])]
    with resource_locks().hold(keys, attempt_owner(operation_id), timeout=10):
        if claim_approved(st, operation_id) is None:
            return {"ok": False, "code": "not_approved", "error": claim_refusal(st, operation_id)}
        albums_before = int((ad.get_stats() or {}).get("albums") or 0)
        st.update(operation_id, status="Running", metadata={"engine_request": {"merge_id": merge_id_for(operation_id),
                                                                               "albums_before": albums_before}})
        try:
            res = ad.album_row_merge(
                meta["target_album_id"], meta["source_album_ids"],
                [{k: m[k] for k in ("item_id", "source_album_id", "sha256", "mb_trackid", "disc", "track")}
                 for m in meta["items"]],
                meta["release_group_id"], meta["release_id"], idempotency_key=operation_id,
                **({"partial": True} if meta.get("partial") else {}))
        except Exception:
            st.update(operation_id, status="Failed", logs=["Engine album-row-merge refused or failed; the engine "
                                                           "restored anything it changed."])
            raise
        return finish_album_row_merge(operation_id, res, adapter=ad, store=st)


def finish_album_row_merge(operation_id: str, engine: Dict[str, Any], *, adapter: Optional[BeetsAdapter] = None,
                           store: Optional[TransactionStore] = None) -> Dict[str, Any]:
    """Verify an applied merge from engine evidence and record the outcome
    (also used by restart recovery -- never re-applies)."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    tx = st.get(operation_id)
    meta = tx.get("metadata") or {}
    engine = engine.get("result") if isinstance(engine.get("result"), dict) else engine
    target = int(meta["target_album_id"])
    problems: List[str] = []
    for m in meta["items"]:
        item = ad.get_item(int(m["item_id"])) or {}
        if int(item.get("album_id") or 0) != target:
            problems.append(f"item {m['item_id']} is not in album {target}")
        elif _identity(item) != m["identity"]:
            problems.append(f"item {m['item_id']} identity changed")
    # A partial move retires only the rows it emptied; the others must remain.
    retire = [int(x) for x in meta.get("retire_album_ids", meta["source_album_ids"])]
    for sid in meta["source_album_ids"]:
        exists = bool(ad.get_album(int(sid)))
        if int(sid) in retire and exists:
            problems.append(f"source album {sid} still exists")
        elif int(sid) not in retire and not exists:
            problems.append(f"source album {sid} was retired but still had items")
    albums_before = int(((meta.get("engine_request") or {}).get("albums_before")) or 0)
    albums_after = int((ad.get_stats() or {}).get("albums") or 0)
    if albums_before and albums_before - albums_after != len(retire):
        problems.append(f"album count changed by {albums_before - albums_after}, expected {len(retire)}")
    status = "Completed" if not problems else "Recovery Required"
    st.update(operation_id, status=status,
              metadata={"engine_result": engine, "verification_problems": problems, "albums_after": albums_after},
              logs=[f"Merged {len(meta['items'])} item(s) into album {target}; retired {engine.get('retired_album_ids')}"]
              + [f"Verification problem: {p}" for p in problems])
    return {"ok": not problems, "operation_id": operation_id, "status": status, "target_album_id": target,
            "retired_album_ids": engine.get("retired_album_ids") or [], "merge_id": engine.get("merge_id"),
            "albums_before": albums_before, "albums_after": albums_after, "verification_problems": problems}


def rollback_album_row_merge(operation_id: str, *, adapter: Optional[BeetsAdapter] = None,
                             store: Optional[TransactionStore] = None) -> Dict[str, Any]:
    ad = adapter or beets_adapter
    st = _get_store(store)
    try:
        tx = st.get(operation_id)
    except KeyError:
        return {"ok": False, "code": "not_found", "error": "Transaction not found"}
    meta = tx.get("metadata") or {}
    engine = meta.get("engine_result") or {}
    if meta.get("mutation_family") != ALBUM_ROW_MERGE_FAMILY or not engine.get("merge_id"):
        return {"ok": False, "code": "not_applied", "error": "No applied album row merge to roll back."}
    if tx.get("status") == "Rolled Back":
        return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}
    keys = [f"album-merge:{meta['release_group_id']}"] + [f"album:{a}" for a in
                                                           sorted([meta["target_album_id"]] + meta["source_album_ids"])]
    with resource_locks().hold(keys, attempt_owner(f"{operation_id}:rollback"), timeout=10):
        res = ad.rollback_album_row_merge(engine["merge_id"], idempotency_key=f"{operation_id}:rollback")
        problems = []
        for m in meta["items"]:
            item = ad.get_item(int(m["item_id"])) or {}
            if int(item.get("album_id") or 0) != int(m["source_album_id"]) or _identity(item) != m["identity"]:
                problems.append(f"item {m['item_id']} not restored to album {m['source_album_id']}")
        for sid in meta["source_album_ids"]:
            if not ad.get_album(int(sid)):
                problems.append(f"album {sid} not restored")
        status = "Rolled Back" if not problems else "Recovery Required"
        st.update(operation_id, status=status, metadata={"rollback_result": res, "rollback_problems": problems},
                  logs=[f"Restored album rows {meta['source_album_ids']} and item ownership"]
                  + [f"Rollback problem: {p}" for p in problems])
    return {"ok": not problems, "operation_id": operation_id, "status": status, "rollback_problems": problems}
