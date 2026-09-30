"""Reviewed recovery and cleanup of files Beets does not track (ARCH-021).

Action classes, decided here (the frontend only displays them):

* Class A ``quarantine``   -- ONLY a byte-identical copy of a tracked file
  (inventory category ``exact_duplicate_of_tracked``, re-proven by SHA-256
  of both files at plan time). Naming patterns alone ("import artifact")
  are never proof of redundancy and are not eligible.
* Class B ``attach``       -- a canonical-looking album file missing from
  Beets goes into its album row's free slot when identity is deterministic:
  the file's own tags name a Release ID that exactly one album row carries
  and a free (disc, track) slot; MusicBrainz confirms that release has that
  Recording at that position; AcoustID confirms the audio is that Recording.
* Class C ``track_for_replacement`` -- a different encoding of a recording
  the library already has: tracked as a singleton on the same evidence,
  then a replacement is PLANNED through backend/item_replacement.py (the one
  replacement authority) for an operator to approve.
* Class D -- everything else: no action.

Every mutation is Plan -> Approve -> Apply -> Verify -> Rollback through the
engine ops in beetsplug/webmanager/untracked_ops.py, under the durable
``untracked-inventory`` lock (plus ``album:<id>`` for an attach). A provider
that cannot be asked (outage, throttle, rejected key) fails the plan as
exactly that -- never as "no match".
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from backend.beets_adapter import BeetsAdapter, beets_adapter
from backend.composite_workflows import _decode_path, _get_store, _s
from backend.resource_locks import attempt_owner, claim_approved, locks as resource_locks
from backend.transaction_engine import TransactionStore

ATTACH_FAMILY = "untracked_attach_v1"
ATTACH_ALBUM_FAMILY = "untracked_attach_album_v1"
QUARANTINE_FAMILY = "untracked_quarantine_v1"
#: One album per plan; larger folders are not one release.
MAX_ALBUM_FILES = 200
ALBUM_CATEGORY = "canonical_album_file_missing_from_beets"

ACTIONS = {
    "exact_duplicate_of_tracked": ("quarantine", "Byte-identical copy of a tracked file."),
    "canonical_album_file_missing_from_beets": ("attach", "Album file missing from Beets; attach after identity proof."),
    "same_recording_other_encoding": ("track_for_replacement",
                                      "Another encoding of a tracked recording; replacement review after tracking."),
    "import_artifact": (None, "Naming pattern alone is not proof of redundancy; no action."),
    "loose_singleton": (None, "No deterministic identity; no action."),
    "unknown": (None, "No deterministic identity; no action."),
}


def record_id_for(kind: str, operation_id: str) -> str:
    """The engine record id for this transaction (see untracked_ops.record_id_for)."""
    return hashlib.sha256(f"untracked-{kind}|{operation_id}".encode("utf-8")).hexdigest()[:32]


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _deps() -> Dict[str, Any]:
    from backend.acoustid_service import _acoustid_lookup_cached_outcome, _album_item_abs_path
    from backend.app_runtime import MUSIC_ROOT, WEB_MANAGER_DATA_DIR
    from backend.matching_service import _fetch_mb_release_tracklist
    return {"music_root": Path(MUSIC_ROOT), "inventory_dir": Path(WEB_MANAGER_DATA_DIR) / "untracked_inventory",
            "abs_path": _album_item_abs_path, "acoustid": _acoustid_lookup_cached_outcome,
            "mb_tracklist": _fetch_mb_release_tracklist, "read_tags": read_identity_tags}


def read_identity_tags(path: str) -> Dict[str, Any]:
    """Identity tags straight from the file (never a remote call)."""
    from mediafile import MediaFile
    mf = MediaFile(path)
    return {"mb_trackid": _s(mf.mb_trackid).lower(), "mb_albumid": _s(mf.mb_albumid).lower(),
            "mb_releasegroupid": _s(mf.mb_releasegroupid).lower(), "disc": int(mf.disc or 1),
            "track": int(mf.track or 0), "title": _s(mf.title)}


def _safe_music_path(root: Path, rel_or_abs: str) -> Optional[str]:
    base = os.path.realpath(str(root))
    candidate = rel_or_abs if os.path.isabs(rel_or_abs) else os.path.join(base, rel_or_abs)
    real = os.path.realpath(candidate)
    return real if real.startswith(base + os.sep) else None


def candidates(category: Optional[str] = None, *, limit: int = 50, offset: int = 0,
               deps: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Page through the persisted inventory with backend-owned eligibility."""
    d = deps or _deps()
    rows, total = [], 0
    try:
        fh = open(Path(d["inventory_dir"]) / "untracked_inventory.jsonl", encoding="utf-8")
    except OSError:
        return {"ok": False, "code": "no_inventory", "error": "Run the untracked inventory first.", "rows": []}
    with fh:
        for line in fh:
            rec = json.loads(line)
            if category and rec.get("category") != category:
                continue
            total += 1
            if total <= offset or len(rows) >= limit:
                continue
            action, reason = ACTIONS.get(rec.get("category"), (None, "No action."))
            rows.append({
                "path": rec["path"], "size": rec.get("size"), "category": rec.get("category"),
                "action": action,
                "action_eligibility": "plannable" if action else "not_eligible",
                "requires_review": True,
                "safety_result": "identity re-proven at plan time" if action else "no mutation",
                "conflicts": [],
                "reason": reason,
                "evidence": {k: rec.get(k) for k in ("sha256", "duplicate_of", "recording_ids", "acoustid") if rec.get(k)},
            })
    return {"ok": True, "total": total, "offset": offset, "limit": limit, "rows": rows}


def album_candidates(*, limit: int = 50, offset: int = 0, deps: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Folders of untracked album files (from the persisted inventory only --
    no disk or tag read), largest first. Whether a folder can be attached to
    an existing row or becomes a new album row is decided at plan time."""
    d = deps or _deps()
    folders: Dict[str, Dict[str, Any]] = {}
    try:
        fh = open(Path(d["inventory_dir"]) / "untracked_inventory.jsonl", encoding="utf-8")
    except OSError:
        return {"ok": False, "code": "no_inventory", "error": "Run the untracked inventory first.", "rows": []}
    with fh:
        for line in fh:
            rec = json.loads(line)
            if rec.get("category") != ALBUM_CATEGORY:
                continue
            folder = rec["path"].rsplit("/", 1)[0] if "/" in rec["path"] else ""
            row = folders.setdefault(folder, {"folder": folder, "files": 0, "bytes": 0})
            row["files"] += 1
            row["bytes"] += int(rec.get("size") or 0)
    rows = sorted(folders.values(), key=lambda r: (-r["files"], r["folder"]))
    page = [{**r, "action": "attach_album", "action_eligibility": "plannable", "requires_review": True,
             "safety_result": "identity re-proven per file at plan time"} for r in rows[offset:offset + limit]]
    return {"ok": True, "total": len(rows), "files": sum(r["files"] for r in rows), "offset": offset,
            "limit": limit, "rows": page}


def _fail(code: str, error: str, **extra: Any) -> Dict[str, Any]:
    return {"ok": False, "code": code, "error": error, **extra}


def _prove_identity(path: str, *, want_album: bool, adapter: BeetsAdapter, d: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic identity for an untracked file, or a reasoned refusal."""
    tags = d["read_tags"](path)
    if not tags.get("mb_trackid"):
        return _fail("no_recording_id", "The file has no Recording ID tag; tag it before recovery.")
    evidence: Dict[str, Any] = {"tags": tags}
    album = None
    if want_album:
        if not tags.get("mb_albumid") or not tags.get("track"):
            return _fail("no_release_position", "The file has no Release ID or track position tag.", evidence=evidence)
        rows = [a for a in (adapter.find_all_albums_by_mb_albumid(tags["mb_albumid"]) or [])
                if _s(a.get("mb_albumid")).lower() == tags["mb_albumid"]]
        if len(rows) != 1:
            return _fail("album_row_not_unique" if rows else "no_album_row",
                         f"{len(rows)} album rows carry this Release ID; exactly one is required.", evidence=evidence)
        album = rows[0]
        taken = {(int(i.get("disc") or 1), int(i.get("track") or 0))
                 for i in adapter.find_all_items_by_album_id(int(album["id"])) or []}
        if (tags["disc"], tags["track"]) in taken:
            return _fail("slot_occupied", "That album slot is already filled; use duplicate review instead.",
                         evidence=evidence)
        mb = d["mb_tracklist"](tags["mb_albumid"])
        evidence["musicbrainz"] = mb.get("outcome") or ("confirmed" if mb.get("ok") else "unknown")
        if not mb.get("ok"):
            return _fail(f"musicbrainz_{evidence['musicbrainz']}",
                         "MusicBrainz could not confirm the release tracklist.", evidence=evidence)
        slot = next((t for t in mb.get("tracks") or [] if (int(t.get("disc") or 1), int(t.get("track") or 0))
                     == (tags["disc"], tags["track"])), None)
        if not slot or _s(slot.get("mb_trackid")).lower() != tags["mb_trackid"]:
            return _fail("slot_recording_mismatch",
                         "MusicBrainz lists a different recording at that position.", evidence=evidence)
    heard = d["acoustid"](path)
    evidence["acoustid"] = {"outcome": heard.outcome.value, "from_cache": heard.from_cache,
                            "recording_ids": [_s(c.get("mb_trackid")).lower() for c in heard.data or []][:5]}
    if not heard.answered:
        return _fail(f"acoustid_{heard.outcome.value}", "AcoustID could not be asked; nothing was concluded.",
                     evidence=evidence)
    if tags["mb_trackid"] not in evidence["acoustid"]["recording_ids"]:
        return _fail("fingerprint_disagreement", "AcoustID does not confirm the tagged recording.", evidence=evidence)
    return {"ok": True, "tags": tags, "album": album, "evidence": evidence}


def plan_album_attach(folder: str, *, adapter: Optional[BeetsAdapter] = None,
                      store: Optional[TransactionStore] = None, deps: Optional[Dict[str, Any]] = None,
                      progress: Optional[Callable[[Dict[str, Any]], None]] = None,
                      item_path_index: Optional[Dict[str, int]] = None,
                      inventory_records: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Plan step (no mutation): the untracked album files of one folder as a
    NEW album row of the release their tags name.

    One release per plan, and only a release with no album row yet (a file
    for an existing row is an ``attach``). A file is included only with
    deterministic proof: Recording ID, Release ID, Release Group ID and track
    tags; MusicBrainz lists that recording at that position of that release
    and gives the same Release Group; AcoustID confirms the audio. A file
    that fails is excluded with its reason and stays untracked. A provider
    that cannot be asked fails the whole plan as exactly that."""
    from backend.item_replacement import tracked_item_id_for_path
    from backend.untracked_inventory import load_previous
    ad = adapter or beets_adapter
    st = _get_store(store)
    d = deps or _deps()
    rel_folder = _s(folder).replace("\\", "/").strip("/")
    real = _safe_music_path(d["music_root"], rel_folder) if rel_folder else None
    if real is None or not os.path.isdir(real):
        return _fail("folder_missing", "The folder is not under the music root or no longer exists.")
    if inventory_records is not None:
        records = [r for r in inventory_records
                   if r.get("category") == ALBUM_CATEGORY and (r.get("path", "").rsplit("/", 1)[0] == rel_folder if "/" in r.get("path", "") else rel_folder == "")]
    else:
        records = [r for path, r in sorted(load_previous(Path(d["inventory_dir"])).items())
                   if r.get("category") == ALBUM_CATEGORY and path.rsplit("/", 1)[0] == rel_folder]
    if not records:
        return _fail("no_candidates", "The inventory lists no untracked album files directly in that folder.")
    if len(records) > MAX_ALBUM_FILES:
        return _fail("too_many_files", f"More than {MAX_ALBUM_FILES} files; that is not one album.")

    excluded: List[Dict[str, Any]] = []
    tagged: List[Dict[str, Any]] = []
    for rec in records:
        path = _safe_music_path(d["music_root"], rec["path"])
        if path is None or not os.path.isfile(path):
            excluded.append({"path": rec["path"], "reason": "file_missing"})
            continue
        if tracked_item_id_for_path(path, adapter=ad, abs_path=d["abs_path"], item_path_index=item_path_index):
            excluded.append({"path": rec["path"], "reason": "already_tracked"})
            continue
        try:
            tags = d["read_tags"](path)
        except Exception as ex:
            excluded.append({"path": rec["path"], "reason": f"unreadable ({type(ex).__name__})"})
            continue
        if not tags.get("mb_trackid") or not tags.get("mb_albumid") or not tags.get("track"):
            excluded.append({"path": rec["path"], "reason": "no_recording_release_or_position_tag"})
            continue
        if not tags.get("mb_releasegroupid"):
            excluded.append({"path": rec["path"], "reason": "no_release_group_tag"})
            continue
        tagged.append({"rel": rec["path"], "path": path, "tags": tags})
    releases = sorted({(t["tags"]["mb_albumid"], t["tags"]["mb_releasegroupid"]) for t in tagged})
    if not releases:
        return _fail("nothing_tagged", "No file carries Recording, Release, Release Group and track tags.",
                     excluded=excluded)
    if len(releases) != 1:
        return _fail("multiple_releases", "The folder's files name more than one release; plan them separately.",
                     releases=[{"release_id": a, "release_group_id": b} for a, b in releases], excluded=excluded)
    release_id, release_group_id = releases[0]
    if [a for a in (ad.find_all_albums_by_mb_albumid(release_id) or [])
            if _s(a.get("mb_albumid")).lower() == release_id]:
        return _fail("album_row_exists", "This release already has an album row; attach the files to it instead.",
                     release_id=release_id)

    mb = d["mb_tracklist"](release_id)
    mb_outcome = mb.get("outcome") or ("confirmed" if mb.get("ok") else "unknown")
    if not mb.get("ok"):
        return _fail(f"musicbrainz_{mb_outcome}", "MusicBrainz could not confirm the release tracklist.")
    if _s(mb.get("release_group")).lower() != release_group_id:
        return _fail("release_group_mismatch", "MusicBrainz places this release in a different Release Group "
                                               "than the files' tags.", release_id=release_id)
    by_slot = {(int(t.get("disc") or 1), int(t.get("track") or 0)): _s(t.get("mb_trackid")).lower()
               for t in mb.get("tracks") or []}

    slots: Dict[Any, List[Dict[str, Any]]] = {}
    for t in tagged:
        slot = (t["tags"]["disc"], t["tags"]["track"])
        if by_slot.get(slot) != t["tags"]["mb_trackid"]:
            excluded.append({"path": t["rel"], "reason": "slot_recording_mismatch"})
            continue
        slots.setdefault(slot, []).append(t)
    proven: List[Dict[str, Any]] = []
    for index, (slot, group) in enumerate(sorted(slots.items()), 1):
        if len(group) > 1:
            excluded.extend({"path": t["rel"], "reason": "slot_contested"} for t in group)
            continue
        t = group[0]
        heard = d["acoustid"](t["path"])
        if progress:
            progress({"processed": index, "total": len(slots)})
        if not heard.answered:
            return _fail(f"acoustid_{heard.outcome.value}", "AcoustID could not be asked; nothing was concluded.")
        if t["tags"]["mb_trackid"] not in [_s(c.get("mb_trackid")).lower() for c in heard.data or []]:
            excluded.append({"path": t["rel"], "reason": "fingerprint_disagreement"})
            continue
        proven.append({"path": t["path"], "sha256": _sha256(t["path"]),
                       "expected": {"mb_trackid": t["tags"]["mb_trackid"], "disc": slot[0], "track": slot[1]},
                       "title": t["tags"].get("title", ""), "acoustid_from_cache": heard.from_cache})
    if not proven:
        return _fail("nothing_proven", "No file of this release could be proven.", excluded=excluded)

    label = f"{_s(mb.get('release_artist'))} - {_s(mb.get('release_title'))}".strip(" -")
    tx = st.create(
        operation_type="Import", status="Preview",
        summary=f"Track {len(proven)} of {len(by_slot)} track(s) of {label or release_id} as a new album row, in place",
        changes=[{"path": f["path"], "disc": f["expected"]["disc"], "track": f["expected"]["track"],
                  "mb_trackid": f["expected"]["mb_trackid"]} for f in proven],
        rollback_available=True,
        metadata={"mutation_family": ATTACH_ALBUM_FAMILY, "action": "attach_album", "folder": rel_folder,
                  "release_id": release_id, "release_group_id": release_group_id, "files": proven,
                  "excluded": excluded, "release_track_count": len(by_slot),
                  "evidence": {"musicbrainz": mb_outcome, "release_title": _s(mb.get("release_title")),
                               "release_artist": _s(mb.get("release_artist"))}})
    return {"ok": True, "operation_id": tx["id"], "status": "Preview", "requires_approval": True,
            "action": "attach_album", "folder": rel_folder, "release_id": release_id,
            "release_group_id": release_group_id, "release": label, "files": len(proven),
            "release_track_count": len(by_slot), "excluded": excluded}


def plan_recovery(action: str, rel_paths: Iterable[str], *, adapter: Optional[BeetsAdapter] = None,
                  store: Optional[TransactionStore] = None, deps: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    ad = adapter or beets_adapter
    st = _get_store(store)
    d = deps or _deps()
    paths = list(rel_paths or [])
    if not paths:
        return _fail("no_paths", "No files were selected.")
    if action == "attach_album":
        if len(paths) != 1:
            return _fail("one_folder_per_plan", "Plan one album folder at a time.")
        return plan_album_attach(paths[0], adapter=ad, store=st, deps=d)
    if action in ("attach", "track_for_replacement"):
        if len(paths) != 1:
            return _fail("one_file_per_plan", "Plan one recovery per file.")
        path = _safe_music_path(d["music_root"], paths[0])
        if path is None or not os.path.isfile(path):
            return _fail("file_missing", "The file is not under the music root or no longer exists.")
        from backend.item_replacement import tracked_item_id_for_path
        if tracked_item_id_for_path(path, adapter=ad, abs_path=d["abs_path"]):
            return _fail("already_tracked", "The file is already tracked.")
        proof = _prove_identity(path, want_album=(action == "attach"), adapter=ad, d=d)
        if not proof["ok"]:
            return proof
        target = None
        if action == "track_for_replacement":
            same = [i for i in ad.find_all_items_by_mbid(proof["tags"]["mb_trackid"]) or [] if i.get("album_id")]
            if len(same) != 1:
                return _fail("replacement_target_not_unique",
                             f"{len(same)} album items carry this recording; exactly one is required.")
            target = int(same[0]["id"])
        album = proof["album"]
        tags = proof["tags"]
        tx = st.create(
            operation_type="Import", status="Preview",
            summary=(f"Attach {Path(path).name} to album {album['id']} slot {tags['disc']}/{tags['track']}" if album
                     else f"Track {Path(path).name} as a singleton, then plan its replacement review"),
            changes=[{"path": path, "album_id": album["id"] if album else None, "disc": tags["disc"],
                      "track": tags["track"], "mb_trackid": tags["mb_trackid"]}],
            rollback_available=True,
            metadata={"mutation_family": ATTACH_FAMILY, "action": action, "path": path, "sha256": _sha256(path),
                      "album_id": album["id"] if album else None, "replacement_target_item_id": target,
                      "expected": {k: tags[k] for k in ("mb_trackid", "mb_albumid", "disc", "track")},
                      "evidence": proof["evidence"]})
        return {"ok": True, "operation_id": tx["id"], "status": "Preview", "requires_approval": True,
                "action": action, "path": path, "album_id": album["id"] if album else None,
                "evidence": proof["evidence"]}
    if action == "quarantine":
        from backend.untracked_inventory import load_previous
        records = load_previous(Path(d["inventory_dir"]))
        files, refused = [], []
        for rel in paths:
            rec = records.get(rel)
            path = _safe_music_path(d["music_root"], rel)
            if not rec or rec.get("category") != "exact_duplicate_of_tracked" or path is None:
                refused.append({"path": rel, "reason": "only byte-identical copies of tracked files are eligible"})
                continue
            twin = rec.get("duplicate_of") or ""
            if not os.path.isfile(path) or not os.path.isfile(twin):
                refused.append({"path": rel, "reason": "file or its tracked twin is gone"})
                continue
            sha = _sha256(path)
            from backend.item_replacement import tracked_item_id_for_path
            if sha != _sha256(twin) or not tracked_item_id_for_path(twin, adapter=ad, abs_path=d["abs_path"]):
                refused.append({"path": rel, "reason": "no longer byte-identical to a tracked file"})
                continue
            files.append({"path": path, "sha256": sha, "duplicate_of": twin})
        if not files:
            return _fail("nothing_eligible", "No selected file is a proven redundant copy.", refused=refused)
        tx = st.create(operation_type="Delete", status="Preview",
                       summary=f"Quarantine {len(files)} byte-identical untracked cop(ies) of tracked files",
                       changes=[{"path": f["path"], "duplicate_of": f["duplicate_of"]} for f in files],
                       rollback_available=True,
                       metadata={"mutation_family": QUARANTINE_FAMILY, "files": files, "refused": refused})
        return {"ok": True, "operation_id": tx["id"], "status": "Preview", "requires_approval": True,
                "files": files, "refused": refused}
    return _fail("unknown_action", "Unknown recovery action.")


def _lock_keys(meta: Dict[str, Any]) -> List[str]:
    """Durable locks for one recovery: the inventory, plus the album row an
    attach fills or the Release Group a new album row belongs to."""
    keys = ["untracked-inventory"]
    if meta.get("album_id"):
        keys.append(f"album:{meta['album_id']}")
    if meta.get("mutation_family") == ATTACH_ALBUM_FAMILY:
        keys.append(f"album-merge:{meta['release_group_id']}")
    return keys


def apply_recovery(operation_id: str, *, adapter: Optional[BeetsAdapter] = None,
                   store: Optional[TransactionStore] = None) -> Dict[str, Any]:
    ad = adapter or beets_adapter
    st = _get_store(store)
    try:
        tx = st.get(operation_id)
    except KeyError:
        return _fail("not_found", "Transaction not found")
    meta = tx.get("metadata") or {}
    family = meta.get("mutation_family")
    if family not in (ATTACH_FAMILY, ATTACH_ALBUM_FAMILY, QUARANTINE_FAMILY):
        return _fail("wrong_family", "Not an untracked recovery transaction.")
    if meta.get("engine_result"):
        return _fail("already_applied", "This recovery was already applied.")
    if tx.get("status") != "Approved":
        return _fail("not_approved", "Approve the transaction before applying it.")
    keys = _lock_keys(meta)
    with resource_locks().hold(keys, attempt_owner(operation_id), timeout=10):
        if claim_approved(st, operation_id) is None:
            return {"ok": False, "code": "not_approved", "error": "Another attempt already claimed this transaction."}
        stats = ad.get_stats() or {}
        items_before = int(stats.get("items") or 0)
        st.update(operation_id, status="Running",
                  metadata={"engine_request": {"items_before": items_before,
                                               "albums_before": int(stats.get("albums") or 0)}})
        try:
            if family == ATTACH_ALBUM_FAMILY:
                res = ad.untracked_attach_album(
                    meta["release_id"], meta["release_group_id"],
                    [{k: f[k] for k in ("path", "sha256", "expected")} for f in meta["files"]],
                    idempotency_key=operation_id)
            elif family == ATTACH_FAMILY:
                res = ad.untracked_attach(meta["path"], meta["sha256"], meta.get("album_id"), meta["expected"],
                                          idempotency_key=operation_id)
            else:
                res = ad.untracked_quarantine([{"path": f["path"], "sha256": f["sha256"]} for f in meta["files"]],
                                              idempotency_key=operation_id)
        except Exception:
            st.update(operation_id, status="Failed", logs=["Engine refused or failed; it kept nothing."])
            raise
        return finish_recovery(operation_id, res, adapter=ad, store=st)


def finish_recovery(operation_id: str, res: Dict[str, Any], *, adapter: Optional[BeetsAdapter] = None,
                    store: Optional[TransactionStore] = None) -> Dict[str, Any]:
    """Verify an applied recovery from engine evidence (also used by restart
    recovery -- never re-applies)."""
    ad = adapter or beets_adapter
    st = _get_store(store)
    meta = st.get(operation_id).get("metadata") or {}
    engine = res.get("result") if isinstance(res.get("result"), dict) else res
    problems: List[str] = []
    items_before = int(((meta.get("engine_request") or {}).get("items_before")) or 0)
    items_after = int((ad.get_stats() or {}).get("items") or 0)
    next_plan = None
    if meta["mutation_family"] == ATTACH_ALBUM_FAMILY:
        album_id = int(engine.get("album_id") or 0)
        album = ad.get_album(album_id, expand=False) if album_id else None
        if not album:
            problems.append("the new album row was not found")
        else:
            if _s(album.get("mb_albumid")).lower() != meta["release_id"]:
                problems.append("the new album row carries a different Release ID")
            if _s(album.get("mb_releasegroupid")).lower() != meta["release_group_id"]:
                problems.append("the new album row carries a different Release Group ID")
            # The engine returns item ids in the order of the request's files.
            by_path = dict(zip(engine.get("paths") or [], engine.get("item_ids") or []))
            for f in meta["files"]:
                row = ad.get_item(int(by_path.get(f["path"]) or 0)) or {}
                if int(row.get("album_id") or 0) != album_id:
                    problems.append(f"{f['path']} is not in the new album row")
                elif _s(row.get("mb_trackid")).lower() != f["expected"]["mb_trackid"]:
                    problems.append(f"{f['path']} has the wrong Recording ID")
            held = len(ad.find_all_items_by_album_id(album_id) or [])
            if held != len(meta["files"]):
                problems.append(f"the new album row holds {held} item(s), expected {len(meta['files'])}")
        if items_before and items_after - items_before != len(meta["files"]):
            problems.append(f"item count changed by {items_after - items_before}, expected +{len(meta['files'])}")
        albums_before = int(((meta.get("engine_request") or {}).get("albums_before")) or 0)
        albums_after = int((ad.get_stats() or {}).get("albums") or 0)
        if albums_before and albums_after - albums_before != 1:
            problems.append(f"album count changed by {albums_after - albums_before}, expected +1")
    elif meta["mutation_family"] == ATTACH_FAMILY:
        item = ad.get_item(int(engine.get("item_id") or 0)) or {}
        if not item:
            problems.append("attached item not found")
        else:
            if (int(item.get("album_id") or 0) or None) != meta.get("album_id"):
                problems.append("attached item is in the wrong album row")
            if _s(item.get("mb_trackid")).lower() != meta["expected"]["mb_trackid"]:
                problems.append("attached item has the wrong Recording ID")
        if items_before and items_after - items_before != 1:
            problems.append(f"item count changed by {items_after - items_before}, expected +1")
        if not problems and meta.get("replacement_target_item_id"):
            from backend.item_replacement import plan_verified_replacement
            next_plan = plan_verified_replacement(int(meta["replacement_target_item_id"]), int(engine["item_id"]),
                                                  reason="Untracked recovery: better encoding of a tracked recording",
                                                  expected_recording_id=meta["expected"]["mb_trackid"], adapter=ad)
    else:
        for f in meta["files"]:
            if os.path.exists(f["path"]):
                problems.append(f"{f['path']} is still in place")
        if items_before and items_after != items_before:
            problems.append("the library item count changed")
    status = "Completed" if not problems else "Recovery Required"
    st.update(operation_id, status=status,
              metadata={"engine_result": engine, "verification_problems": problems, "items_after": items_after,
                        "replacement_plan": next_plan},
              logs=[f"Engine record {engine.get('record_id')}: {meta['mutation_family']} applied"]
              + [f"Verification problem: {p}" for p in problems])
    return {"ok": not problems, "operation_id": operation_id, "status": status, "record_id": engine.get("record_id"),
            "item_id": engine.get("item_id"), "album_id": engine.get("album_id"),
            "items_before": items_before, "items_after": items_after,
            "verification_problems": problems, "replacement_plan": next_plan}


def rollback_recovery(operation_id: str, *, adapter: Optional[BeetsAdapter] = None,
                      store: Optional[TransactionStore] = None) -> Dict[str, Any]:
    ad = adapter or beets_adapter
    st = _get_store(store)
    try:
        tx = st.get(operation_id)
    except KeyError:
        return _fail("not_found", "Transaction not found")
    meta = tx.get("metadata") or {}
    engine = meta.get("engine_result") or {}
    if (meta.get("mutation_family") not in (ATTACH_FAMILY, ATTACH_ALBUM_FAMILY, QUARANTINE_FAMILY)
            or not engine.get("record_id")):
        return _fail("not_applied", "No applied untracked recovery to roll back.")
    if tx.get("status") == "Rolled Back":
        return {"ok": True, "operation_id": operation_id, "status": "Rolled Back"}
    keys = _lock_keys(meta)
    with resource_locks().hold(keys, attempt_owner(f"{operation_id}:rollback"), timeout=10):
        res = ad.untracked_rollback(engine["record_id"], idempotency_key=f"{operation_id}:rollback")
        problems = []
        if meta["mutation_family"] == ATTACH_ALBUM_FAMILY:
            if ad.get_album(int(engine.get("album_id") or 0), expand=False):
                problems.append("the album row is still in the library")
            for f in meta["files"]:
                if not os.path.isfile(f["path"]) or _sha256(f["path"]) != f["sha256"]:
                    problems.append(f"{f['path']} is missing or changed")
        elif meta["mutation_family"] == ATTACH_FAMILY:
            if ad.get_item(int(engine.get("item_id") or 0)):
                problems.append("the attached item is still in the library")
            if not os.path.isfile(meta["path"]):
                problems.append("the file is missing")
        else:
            for f in meta["files"]:
                if not os.path.isfile(f["path"]) or _sha256(f["path"]) != f["sha256"]:
                    problems.append(f"{f['path']} was not restored intact")
        status = "Rolled Back" if not problems else "Recovery Required"
        st.update(operation_id, status=status, metadata={"rollback_result": res, "rollback_problems": problems},
                  logs=["Rolled back through the engine record"] + [f"Rollback problem: {p}" for p in problems])
    return {"ok": not problems, "operation_id": operation_id, "status": status, "rollback_problems": problems}


SIDECAR_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".cue", ".log", ".nfo", ".sfv", ".m3u", ".m3u8", ".txt", ".accurip",
}


def plan_untracked_batch(
    folders: Optional[List[str]] = None,
    *,
    max_folders: int = 50,
    max_acoustid_lookups: int = 200,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    deps: Optional[Dict[str, Any]] = None,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    cancel_event: Any = None,
) -> Dict[str, Any]:
    """Batch plan untracked album folders into preview transactions under the shared contract.

    - Builds an in-memory item path index once (O(1) path lookup).
    - Pre-indexes inventory records by folder once.
    - Rate-limited AcoustID budgeting across the batch.
    - Partial exclusions/failures of single folders do not fail the entire batch.
    - Outputs a list of generated Preview transactions for human review.
    """
    from backend.item_replacement import build_item_path_index
    from backend.untracked_inventory import load_previous
    ad = adapter or beets_adapter
    st = _get_store(store)
    d = deps or _deps()

    # Build path index once for fast lookups
    item_path_index = build_item_path_index(adapter=ad, abs_path=d["abs_path"])

    # Load inventory once
    try:
        all_inv = load_previous(Path(d["inventory_dir"]))
    except Exception:
        all_inv = {}

    records_by_folder: Dict[str, List[Dict[str, Any]]] = {}
    for path, r in all_inv.items():
        if r.get("category") == ALBUM_CATEGORY:
            folder = path.rsplit("/", 1)[0] if "/" in path else ""
            records_by_folder.setdefault(folder, []).append(r)

    if folders is not None:
        candidate_folders = [_s(f).replace("\\", "/").strip("/") for f in folders if _s(f).strip("/")]
    else:
        # Sort folders by file count descending
        candidate_folders = sorted(records_by_folder.keys(), key=lambda f: (-len(records_by_folder[f]), f))

    if max_folders > 0:
        candidate_folders = candidate_folders[:max_folders]

    # Budget AcoustID lookups
    acoustid_calls = [0]
    orig_acoustid = d.get("acoustid")
    def budgeted_acoustid(path: str):
        if acoustid_calls[0] >= max_acoustid_lookups:
            from backend.provider_boundary import ProviderOutcome, ProviderResult
            return ProviderResult("acoustid", ProviderOutcome.RATE_LIMITED, data=[])
        res = orig_acoustid(path) if orig_acoustid else None
        if res and not getattr(res, "from_cache", False):
            acoustid_calls[0] += 1
        return res

    batch_deps = dict(d, acoustid=budgeted_acoustid)

    planned: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    budget_reached = False

    total = len(candidate_folders)
    for idx, folder in enumerate(candidate_folders, 1):
        if cancel_event and getattr(cancel_event, "is_set", lambda: False)():
            break

        if progress:
            progress({
                "phase": "batch_planning",
                "folder": folder,
                "index": idx,
                "total": total,
                "planned_count": len(planned),
                "skipped_count": len(skipped),
                "acoustid_lookups": acoustid_calls[0],
            })

        res = plan_album_attach(
            folder,
            adapter=ad,
            store=st,
            deps=batch_deps,
            item_path_index=item_path_index,
            inventory_records=records_by_folder.get(folder, []),
        )

        if res.get("ok"):
            planned.append({
                "folder": folder,
                "operation_id": res.get("operation_id"),
                "release": res.get("release"),
                "release_id": res.get("release_id"),
                "release_group_id": res.get("release_group_id"),
                "files": res.get("files"),
                "release_track_count": res.get("release_track_count"),
                "excluded": res.get("excluded", []),
            })
        else:
            code = res.get("code") or "failed"
            if code == "acoustid_rate_limited" and acoustid_calls[0] >= max_acoustid_lookups:
                budget_reached = True
                skipped.append({"folder": folder, "code": "acoustid_budget_exceeded", "error": "AcoustID lookup budget reached for batch."})
                break
            skipped.append({
                "folder": folder,
                "code": code,
                "error": res.get("error", "Plan refused"),
                "excluded": res.get("excluded", []),
            })

    return {
        "ok": True,
        "total_candidates": total,
        "processed_folders": len(planned) + len(skipped),
        "planned_count": len(planned),
        "skipped_count": len(skipped),
        "acoustid_lookups": acoustid_calls[0],
        "budget_reached": budget_reached,
        "planned": planned,
        "skipped": skipped,
    }


def plan_untracked_quarantine_batch(
    paths_or_folders: Optional[Iterable[str]] = None,
    *,
    include_sidecars: bool = True,
    adapter: Optional[BeetsAdapter] = None,
    store: Optional[TransactionStore] = None,
    deps: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Batch plan quarantine for proven duplicate files and safe associated artifacts.

    Policy:
    - Audio files: ONLY byte-identical copies of tracked library items (re-proven by SHA-256).
    - Sidecars in quarantined folders (.jpg, .cue, .log, .nfo):
      If ALL audio files in the folder are proven exact duplicates, sidecars are categorized as
      `associated_artifact` and planned for quarantine with full rollback hashes.
    - If a folder contains unverified audio or other non-sidecar files, sidecars are preserved
      (`unverified_context_kept`) and excluded from quarantine.
    - Unverified / ambiguous files: never deleted or quarantined (Class D / kept).
    - Output: a Preview transaction in TransactionStore requiring explicit human approval.
    """
    from backend.item_replacement import build_item_path_index, tracked_item_id_for_path
    from backend.untracked_inventory import load_previous
    ad = adapter or beets_adapter
    st = _get_store(store)
    d = deps or _deps()

    item_path_index = build_item_path_index(adapter=ad, abs_path=d["abs_path"])
    records = load_previous(Path(d["inventory_dir"]))

    if paths_or_folders is not None:
        target_set = set(paths_or_folders)
    else:
        target_set = {p for p, r in records.items() if r.get("category") == "exact_duplicate_of_tracked"}

    files_to_quarantine: List[Dict[str, Any]] = []
    refused_files: List[Dict[str, Any]] = []
    folders_checked: Dict[str, Dict[str, Any]] = {}

    for rel in sorted(target_set):
        rec = records.get(rel)
        path = _safe_music_path(d["music_root"], rel)
        if not rec or rec.get("category") != "exact_duplicate_of_tracked" or path is None:
            refused_files.append({"path": rel, "reason": "only byte-identical copies of tracked files are eligible"})
            continue
        twin = rec.get("duplicate_of") or ""
        if not os.path.isfile(path) or not os.path.isfile(twin):
            refused_files.append({"path": rel, "reason": "file or its tracked twin is gone"})
            continue
        sha = _sha256(path)
        if sha != _sha256(twin) or not tracked_item_id_for_path(twin, adapter=ad, abs_path=d["abs_path"], item_path_index=item_path_index):
            refused_files.append({"path": rel, "reason": "no longer byte-identical to a tracked file"})
            continue
        files_to_quarantine.append({
            "path": path,
            "rel_path": rel,
            "sha256": sha,
            "duplicate_of": twin,
            "artifact_type": "audio_duplicate",
        })
        parent_folder = os.path.dirname(path)
        folders_checked.setdefault(parent_folder, {"audio_duplicates": set(), "all_audio": set(), "sidecars": set()})
        folders_checked[parent_folder]["audio_duplicates"].add(path)

    # If include_sidecars, inspect folders where duplicates live
    if include_sidecars and folders_checked:
        from backend.acoustid_service import AUDIO_EXTS
        for folder_path, folder_info in list(folders_checked.items()):
            try:
                entries = list(os.scandir(folder_path))
            except OSError:
                continue
            all_audio = set()
            sidecars = set()
            unknown_files = set()
            for entry in entries:
                if not entry.is_file():
                    continue
                ext = Path(entry.name).suffix.lower()
                full_p = entry.path
                if ext in AUDIO_EXTS:
                    all_audio.add(full_p)
                elif ext in SIDECAR_EXTENSIONS:
                    sidecars.add(full_p)
                else:
                    unknown_files.add(full_p)
            folder_info["all_audio"] = all_audio
            folder_info["sidecars"] = sidecars

            # Safe policy: Only quarantine sidecars if ALL audio files in the folder are proven duplicates
            # and there are no unknown non-sidecar files
            if all_audio and all_audio.issubset(folder_info["audio_duplicates"]) and not unknown_files:
                for sc in sorted(sidecars):
                    sc_sha = _sha256(sc)
                    files_to_quarantine.append({
                        "path": sc,
                        "rel_path": os.path.relpath(sc, str(d["music_root"])).replace("\\", "/"),
                        "sha256": sc_sha,
                        "duplicate_of": None,
                        "artifact_type": "associated_artifact",
                    })
            elif sidecars:
                for sc in sorted(sidecars):
                    refused_files.append({
                        "path": os.path.relpath(sc, str(d["music_root"])).replace("\\", "/"),
                        "reason": "unverified_context_kept (folder contains non-duplicate or unverified audio/files)",
                    })

    if not files_to_quarantine:
        return _fail("nothing_eligible", "No selected file is a proven redundant copy.", refused=refused_files)

    audio_count = sum(1 for f in files_to_quarantine if f.get("artifact_type") == "audio_duplicate")
    sidecar_count = sum(1 for f in files_to_quarantine if f.get("artifact_type") == "associated_artifact")

    tx = st.create(
        operation_type="Delete",
        status="Preview",
        summary=f"Quarantine {audio_count} duplicate audio file(s) and {sidecar_count} associated artifact(s)",
        changes=[{"path": f["path"], "sha256": f["sha256"], "artifact_type": f["artifact_type"],
                  "duplicate_of": f.get("duplicate_of")} for f in files_to_quarantine],
        rollback_available=True,
        metadata={
            "mutation_family": QUARANTINE_FAMILY,
            "files": [{"path": f["path"], "sha256": f["sha256"]} for f in files_to_quarantine],
            "file_details": files_to_quarantine,
            "audio_count": audio_count,
            "sidecar_count": sidecar_count,
            "refused": refused_files,
        },
    )

    return {
        "ok": True,
        "operation_id": tx["id"],
        "status": "Preview",
        "requires_approval": True,
        "audio_count": audio_count,
        "sidecar_count": sidecar_count,
        "files": files_to_quarantine,
        "refused": refused_files,
    }

