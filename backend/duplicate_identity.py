"""Duplicate-file identity rules for dedup scans and unattended cleanup.

ARCH-009: a shared Recording ID or AcoustID fingerprint proves the same
*recording*, not the same album. The same recording on a studio album and a
compilation is two legitimate library entries. Unattended deletion therefore
requires duplicate-FILE identity: deterministic recording evidence (or a
byte-identical file) AND both copies belonging to the same album.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

def item_album_id(item: Any) -> Optional[int]:
    if item is None:
        return None
    try:
        value = int(getattr(item, "album_id", None) or 0)
    except Exception:
        return None
    return value or None


#: Relations that identify the same release SLOT, i.e. a genuine duplicate file.
SAME_SLOT_RELATIONS = frozenset({"same_album_position", "same_release_position"})


def _position(item: Any) -> tuple:
    def _int(name: str, default: int) -> int:
        try:
            return int(getattr(item, name, None) or default)
        except Exception:
            return default
    return (_int("disc", 1), _int("track", 0))


def _release_id(item: Any) -> str:
    return str(getattr(item, "mb_albumid", "") or "").strip().lower()


def release_relation(source_item: Any, lib_item: Any) -> str:
    """How the two sides of a duplicate candidate relate.

    * "not_library_item": the source is not a tracked Beets item.
    * "same_album_position": one album row, same disc/track -- an in-album
      duplicate file.
    * "same_release_position": same concrete release (mb_albumid) and same
      disc/track, split across album rows or singletons -- still one slot.
    * "different_position": same album/release but a different disc/track
      (one recording legitimately placed twice).
    * "different_release": different releases -- e.g. album + compilation.
    * "unknown": album/release identity or position could not be
      established for one side.
    """
    if source_item is None:
        return "not_library_item"
    source_pos, lib_pos = _position(source_item), _position(lib_item)
    same_position = bool(source_pos[1]) and source_pos == lib_pos
    source_album, lib_album = item_album_id(source_item), item_album_id(lib_item)
    if source_album and lib_album and source_album == lib_album:
        return "same_album_position" if same_position else "different_position"
    source_release, lib_release = _release_id(source_item), _release_id(lib_item)
    if source_release and lib_release:
        if source_release != lib_release:
            return "different_release"
        return "same_release_position" if same_position else "different_position"
    return "unknown"


def same_file_hash(left: Path, right: Path) -> bool:
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        h_left = hashlib.sha256()
        h_right = hashlib.sha256()
        with left.open("rb") as left_fh, right.open("rb") as right_fh:
            while True:
                left_chunk = left_fh.read(1024 * 1024)
                right_chunk = right_fh.read(1024 * 1024)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    break
                h_left.update(left_chunk)
                h_right.update(right_chunk)
        return h_left.digest() == h_right.digest()
    except Exception:
        return False


def _s(value: Any) -> str:
    return "" if value is None else str(value)


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_LOSSLESS_FORMATS = {"flac", "alac", "ape", "wav", "aiff", "aif", "wv", "wavpack", "dsf", "dff"}
# Beets' own duplicate-name suffix ("Song.1.flac") and download/source-id
# decorations ("(01)", "{source-id}") mark a copy as not the canonical path.
_NONCANONICAL_NAME_RE = re.compile(
    r"(\.\d+\.[A-Za-z0-9]+$)|(\{[^{}]+\}\.[A-Za-z0-9]+$)|(\s\(\d{2}\)(\{[^{}]*\})?\.[A-Za-z0-9]+$)"
)


def copy_meta(item: Any) -> Dict[str, Any]:
    """Keeper-ranking facts for one tracked copy (read from the Beets item)."""
    if item is None:
        return {}

    def _get(name: str) -> Any:
        return getattr(item, name, None)

    return {
        "album_id": item_album_id(item),
        "recording_id": _s(_get("mb_trackid")).strip(),
        "release_id": _s(_get("mb_albumid")).strip(),
        "releasegroup_id": _s(_get("mb_releasegroupid")).strip(),
        "disc": _get("disc"),
        "track": _get("track"),
        "format": _s(_get("format")).strip(),
        "bitrate": _get("bitrate"),
        "samplerate": _get("samplerate"),
        "bitdepth": _get("bitdepth"),
    }


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_uuid(value: Any) -> bool:
    return bool(_UUID_RE.match(_s(value).strip().lower()))


def _format(copy: Dict[str, Any]) -> str:
    return _s((copy.get("meta") or {}).get("format")).strip().lower()


def _is_lossless(copy: Dict[str, Any]) -> bool:
    return _format(copy) in _LOSSLESS_FORMATS


def _is_lossy(copy: Dict[str, Any]) -> bool:
    fmt = _format(copy)
    return bool(fmt) and fmt not in _LOSSLESS_FORMATS


def keeper_rank(copy: Dict[str, Any]) -> tuple:
    """Higher sorts better. The order is the policy:

    1. attached to an album row (the album slot) over a loose/singleton copy;
    2. valid canonical metadata (Recording ID, Release Group ID, release ID,
       disc/track position);
    3. embedded Recording ID agrees with AcoustID;
    4. canonical Beets-managed path over a duplicate/decorated filename;
    5. file quality: lossless over lossy, bitrate, sample rate, bit depth;
       file size only between copies of the same known format;
    6. lowest item id -- only as the final deterministic tie-breaker.
    """
    meta = copy.get("meta") or {}
    canonical = sum((
        _is_uuid(meta.get("recording_id")),
        _is_uuid(meta.get("releasegroup_id")),
        _is_uuid(meta.get("release_id")),
        _num(meta.get("disc")) > 0 and _num(meta.get("track")) > 0,
    ))
    fp_ids = {_s(x).strip().lower() for x in copy.get("fingerprint_ids") or []}
    recording = _s(meta.get("recording_id")).strip().lower()
    agrees = bool(recording) and recording in fp_ids
    name = Path(_s(copy.get("path"))).name
    canonical_path = not _NONCANONICAL_NAME_RE.search(name)
    fmt = _s(meta.get("format")).strip().lower()
    lossless = fmt in _LOSSLESS_FORMATS
    return (
        bool(meta.get("album_id")),
        canonical,
        agrees,
        canonical_path,
        (lossless, _num(meta.get("bitrate")), _num(meta.get("samplerate")), _num(meta.get("bitdepth"))),
        (fmt, _num(copy.get("size"))) if fmt else ("", 0.0),
        -int(copy.get("item_id") or 0),
    )


_RANK_REASONS = (
    "attached to the album slot (the other copy is a loose/singleton item)",
    "more complete canonical metadata (Recording/Release Group/release IDs, disc/track)",
    "embedded Recording ID agrees with AcoustID",
    "canonical Beets library path (the other has a duplicate/decorated filename)",
    "better audio quality (lossless/bitrate/sample rate/bit depth)",
    "larger file of the same format",
    "lower item id (deterministic tie-breaker; the copies are otherwise equal)",
)


def keeper_reason(keep: Dict[str, Any], drop: Dict[str, Any]) -> str:
    rk, rd = keeper_rank(keep), keeper_rank(drop)
    for idx, (a, b) in enumerate(zip(rk, rd)):
        if a == b:
            continue
        if idx == 5 and a[0] != b[0]:
            continue  # sizes of different formats are not compared
        return _RANK_REASONS[idx]
    return _RANK_REASONS[-1]


def _copies_from_dup(dup: Dict[str, Any], root: Path, path_under: Callable[[Path, Path], bool]):
    raw_source = _s(dup.get("source_path")).strip()
    raw_lib = _s(dup.get("lib_path")).strip()
    if not raw_source or not raw_lib:
        return None
    try:
        source = Path(raw_source).resolve(strict=False)
        library_copy = Path(raw_lib).resolve(strict=False)
    except Exception:
        return None
    if source == library_copy:
        return None
    for path in (source, library_copy):
        if not path_under(path, root) or not path.exists() or not path.is_file():
            return None
    try:
        source_item_id = int(dup.get("source_item_id") or 0)
        lib_item_id = int(dup.get("lib_id") or 0)
    except Exception:
        return None
    if not source_item_id or not lib_item_id:
        return None

    def _side(prefix: str, path: Path, item_id: int, album_id: Any, fp: Any) -> Dict[str, Any]:
        meta = dict(dup.get(f"{prefix}_meta") or {})
        meta.setdefault("album_id", album_id)
        meta.setdefault("recording_id", _s(dup.get(f"{prefix}_recording_id")))
        meta.setdefault("disc", dup.get(f"{prefix}_disc"))
        meta.setdefault("track", dup.get(f"{prefix}_track"))
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        return {"path": str(path), "item_id": item_id, "meta": meta, "fingerprint_ids": list(fp or []), "size": size}

    return (
        _side("source", source, source_item_id, dup.get("source_album_id"), dup.get("source_fingerprint_ids")),
        _side("lib", library_copy, lib_item_id, dup.get("lib_album_id"), dup.get("lib_fingerprint_ids")),
    )


def plan_unattended_cleanup(
    scan_result: Dict[str, Any],
    music_root: Path,
    path_under: Callable[[Path, Path], bool],
    *,
    same_file: Callable[[Path, Path], bool] = same_file_hash,
    sibling_row_retire: Optional[Callable[[Dict[str, Any], Dict[str, Any]], bool]] = None,
) -> List[Dict[str, Any]]:
    """Deletions a scheduled maintenance run may make without review.

    A pair is eligible only with: high confidence; both copies present,
    tracked and under the music root; audio identity proven (both copies
    fingerprint to a shared recording) or a byte-identical file -- a shared
    embedded Recording ID alone is not proof; and the SAME release slot.

    Eligible pairs form groups; each group keeps its best copy by
    keeper_rank(), and a copy is deleted only with direct proof against that
    keeper. Album-slot gate: a copy attached to an album row is never deleted
    unless the keeper is a tracked item in that same album row, so no album
    slot is ever left without a retained tracked item. The one exception is
    opt-in (``sibling_row_retire``, passed only by an operator-reviewed
    cleanup, never by the unattended path): the copy is the ONLY item of a
    duplicate row of the keeper's own release (same Release ID and Release
    Group, same disc/track), so the release slot keeps its tracked item and
    the emptied duplicate row is retired with it ("retire_album_id").

    Replacement review: when the preferred (album-attached) copy is lossy and
    a proven duplicate is lossless, nothing in that group is deleted; every
    pair is returned with action "replacement_review" instead of "delete".
    """
    root = music_root.resolve(strict=False)
    copies: Dict[str, Dict[str, Any]] = {}
    pairs: Dict[frozenset, Dict[str, Any]] = {}
    for dup in scan_result.get("duplicates") or []:
        if not isinstance(dup, dict) or _s(dup.get("confidence")).lower() != "high":
            continue
        if _s(dup.get("release_relation")) not in SAME_SLOT_RELATIONS:
            continue
        sides = _copies_from_dup(dup, root, path_under)
        if sides is None:
            continue
        a, b = sides
        audio_proven = bool(dup.get("fingerprint_verified"))
        exact_hash = _s(dup.get("match_type")) == "identical file size" and same_file(Path(a["path"]), Path(b["path"]))
        if not (audio_proven or exact_hash):
            continue
        for side in (a, b):
            known = copies.get(side["path"])
            if known is None or (not known["meta"].get("format") and side["meta"].get("format")):
                copies[side["path"]] = side
        key = frozenset((a["path"], b["path"]))
        pairs.setdefault(key, {
            "release_relation": _s(dup.get("release_relation")),
            "match_type": _s(dup.get("match_type")),
            "fingerprint_verified": audio_proven,
            "byte_identical": exact_hash,
            "shared_recording_id": _s(dup.get("fingerprint_mbid")),
        })

    parent: Dict[str, str] = {p: p for p in copies}

    def _find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for key in pairs:
        a, b = tuple(key)
        parent[_find(a)] = _find(b)
    groups: Dict[str, List[str]] = {}
    for path in copies:
        groups.setdefault(_find(path), []).append(path)

    decisions: List[Dict[str, Any]] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        keeper = max(members, key=lambda p: keeper_rank(copies[p]))
        keep = copies[keeper]
        # Lossy album copy vs a proven lossless duplicate: neither is deleted.
        # The right fix is replacing the album file with the lossless copy
        # (a reviewed replacement transaction), so the whole group goes to review.
        lossless_rival = [p for p in members if p != keeper and _is_lossless(copies[p]) and _is_lossy(keep)]
        for path in sorted(members):
            if path == keeper:
                continue
            pair = pairs.get(frozenset((path, keeper)))
            if pair is None:
                continue  # no direct proof against the retained copy: review
            drop = copies[path]
            if lossless_rival:
                decisions.append({
                    "action": "replacement_review", "delete": drop, "keep": keep,
                    "keep_reason": "replacement review required: the album copy is lossy and a proven "
                                   "duplicate is lossless -- replace the album file instead of deleting either",
                    **pair,
                })
                continue
            drop_album = drop["meta"].get("album_id")
            retire: Dict[str, Any] = {}
            if drop_album and drop_album != keep["meta"].get("album_id"):
                # album-slot gate: would leave that album slot without a tracked
                # item -- unless (reviewed cleanup only) the copy is the sole item
                # of a duplicate row of the keeper's release, which then retires.
                if sibling_row_retire is None or not sibling_row_retire(drop, keep):
                    continue
                retire = {"retire_album_id": drop_album}
            decisions.append({"action": "delete", "delete": drop, "keep": keep,
                              "keep_reason": keeper_reason(keep, drop), **pair, **retire})
    decisions.sort(key=lambda d: d["delete"]["path"])
    return decisions


def select_unattended_cleanup_paths(
    scan_result: Dict[str, Any],
    music_root: Path,
    path_under: Callable[[Path, Path], bool],
    *,
    same_file: Callable[[Path, Path], bool] = same_file_hash,
) -> List[str]:
    """Paths plan_unattended_cleanup() would delete (see its rules)."""
    return [
        d["delete"]["path"]
        for d in plan_unattended_cleanup(scan_result, music_root, path_under, same_file=same_file)
        if d.get("action") == "delete"
    ]
