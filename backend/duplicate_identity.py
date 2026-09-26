"""Duplicate-file identity rules for dedup scans and unattended cleanup.

ARCH-009: a shared Recording ID or AcoustID fingerprint proves the same
*recording*, not the same album. The same recording on a studio album and a
compilation is two legitimate library entries. Unattended deletion therefore
requires duplicate-FILE identity: deterministic recording evidence (or a
byte-identical file) AND both copies belonging to the same album.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

#: Match types that already carry deterministic recording identity.
DETERMINISTIC_MATCH_TYPES = frozenset({"MB Track ID", "AcoustID fingerprint"})


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


def select_unattended_cleanup_paths(
    scan_result: Dict[str, Any],
    music_root: Path,
    path_under: Callable[[Path, Path], bool],
    *,
    same_file: Callable[[Path, Path], bool] = same_file_hash,
) -> List[str]:
    """Source paths a scheduled maintenance run may delete without review.

    Requires: high confidence; source under the music root; both files
    present; deterministic identity (Recording ID / AcoustID / fingerprint
    verified) or a byte-identical file; both copies tracked items occupying
    the SAME release slot (same album row or same release, same disc/track);
    and only the higher item id of a pair -- so a mutual A<->B pair can never
    select both copies. Everything else stays for review.
    """
    selected: List[str] = []
    seen: set = set()
    root = music_root.resolve(strict=False)
    for dup in scan_result.get("duplicates") or []:
        if not isinstance(dup, dict):
            continue
        if _s(dup.get("confidence")).lower() != "high":
            continue
        match_type = _s(dup.get("match_type"))
        raw_source = _s(dup.get("source_path")).strip()
        raw_lib = _s(dup.get("lib_path")).strip()
        if not raw_source or not raw_lib:
            continue
        try:
            source = Path(raw_source).resolve(strict=False)
            library_copy = Path(raw_lib).resolve(strict=False)
        except Exception:
            continue
        if source == library_copy:
            continue
        if not path_under(source, root):
            continue
        if not source.exists() or not source.is_file():
            continue
        if not library_copy.exists() or not library_copy.is_file():
            continue
        strong_identity = bool(dup.get("fingerprint_verified") or match_type in DETERMINISTIC_MATCH_TYPES)
        exact_hash = match_type == "identical file size" and same_file(source, library_copy)
        if not (strong_identity or exact_hash):
            continue
        if _s(dup.get("release_relation")) not in SAME_SLOT_RELATIONS:
            continue
        try:
            source_item_id = int(dup.get("source_item_id") or 0)
            lib_item_id = int(dup.get("lib_id") or 0)
        except Exception:
            continue
        if not source_item_id or not lib_item_id or source_item_id <= lib_item_id:
            continue
        key = str(source).casefold()
        if key in seen:
            continue
        seen.add(key)
        selected.append(str(source))
    return selected
