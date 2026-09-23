"""Validation schemas, allowed fields, and path containment guards for webmanager plugin."""

import os
from typing import List, Set, Optional

DEFAULT_API_KEY_FILE = "/config/.webmanager_api_key"
DEFAULT_ALLOWED_ROOTS = ["/music", "/downloads", "/web-manager-data"]

# Import sources are intentionally a *separate, narrower* concept from
# allowed_roots: allowed_roots also covers /music (a Beets-managed
# destination, not a valid import intake point) and /web-manager-data
# (unrelated app state). Only import_roots may be used as the source of a
# POST /webmanager/import -- do not fall back to allowed_roots for import
# path validation.
DEFAULT_IMPORT_ROOTS = ["/downloads"]

ALLOWED_ITEM_FIELDS: Set[str] = {
    "title",
    "artist",
    "album",
    "albumartist",
    "genre",
    "year",
    "month",
    "day",
    "track",
    "tracktotal",
    "disc",
    "disctotal",
    "lyrics",
    "comments",
    "bpm",
    "comp",
    "mb_trackid",
    "mb_albumid",
    "mb_artistid",
    "mb_albumartistid",
    "mb_releasegroupid",
    "acoustid_fingerprint",
    "acoustid_id",
    "data_source",
    "original_year",
    "original_month",
    "original_day",
    "country",
    "media",
    "label",
    "catalognum",
    "barcode",
    "isrc",
    "format",
    "bitdepth",
    "bitrate",
    "samplerate",
}

ALLOWED_ALBUM_FIELDS: Set[str] = {
    "album",
    "albumartist",
    "genre",
    "year",
    "month",
    "day",
    "disctotal",
    "comp",
    "mb_albumid",
    "mb_artistid",
    "mb_albumartistid",
    "mb_releasegroupid",
    "original_year",
    "original_month",
    "original_day",
    "country",
    "media",
    "label",
    "catalognum",
    "barcode",
    "artpath",
    "albumtype",
    "albumstatus",
    "data_source",
}

ALLOWED_DUPLICATE_ACTIONS: Set[str] = {
    "skip",
    "keep",
    "merge",
    "remove",
}


def is_path_safe_and_allowed(target_path: str, allowed_roots: List[str]) -> bool:
    """Check if target_path resolves cleanly inside or equal to one of allowed_roots.

    Rejects:
    - Null bytes, control characters, or non-string inputs
    - Path traversal (e.g. /downloads/../config)
    - Symlinks pointing outside allowed roots
    - Prefix confusion (e.g. /music-old matching /music)
    """
    if not target_path or not isinstance(target_path, str):
        return False
    if "\x00" in target_path:
        return False

    try:
        norm_target = os.path.realpath(os.path.abspath(target_path))
        for root in allowed_roots:
            if not root or not isinstance(root, str):
                continue
            if "\x00" in root:
                continue
            norm_root = os.path.realpath(os.path.abspath(root))
            root_prefix = norm_root if norm_root.endswith(os.sep) else norm_root + os.sep
            if norm_target == norm_root or norm_target.startswith(root_prefix):
                try:
                    if os.path.commonpath([norm_target, norm_root]) == norm_root:
                        return True
                except ValueError:
                    continue
        return False
    except Exception:
        return False


def resolve_safe_descendant(target_path: str, allowed_roots: List[str]) -> Optional[str]:
    """Validate that target_path is a strict child of one of allowed_roots and return canonical path.

    Rejects:
    - The root directory itself (e.g. /downloads)
    - Path traversal (e.g. /downloads/../config)
    - Symlinks pointing outside allowed roots
    - Prefix confusion (e.g. /downloads2 matching /downloads)
    - Null bytes or non-string inputs
    """
    if not target_path or not isinstance(target_path, str):
        return None
    if "\x00" in target_path:
        return None

    try:
        norm_target = os.path.realpath(os.path.abspath(target_path))
        for root in allowed_roots:
            if not root or not isinstance(root, str):
                continue
            if "\x00" in root:
                continue
            norm_root = os.path.realpath(os.path.abspath(root))
            root_prefix = norm_root if norm_root.endswith(os.sep) else norm_root + os.sep
            if norm_target.startswith(root_prefix) and norm_target != norm_root:
                try:
                    if os.path.commonpath([norm_target, norm_root]) == norm_root:
                        rel = os.path.relpath(norm_target, norm_root)
                        if not rel.startswith("..") and rel != ".":
                            return norm_target
                except ValueError:
                    continue
        return None
    except Exception:
        return None


def is_strict_descendant(target_path: str, allowed_roots: List[str]) -> bool:
    """Check if target_path is a strict child/descendant of one of allowed_roots."""
    return resolve_safe_descendant(target_path, allowed_roots) is not None


def validate_fields(fields: dict, is_album: bool = False) -> dict:
    """Filter dictionary of fields against allowlist and return safe fields."""
    if not isinstance(fields, dict):
        return {}
    allowed = ALLOWED_ALBUM_FIELDS if is_album else ALLOWED_ITEM_FIELDS
    safe = {}
    for k, v in fields.items():
        if k in allowed:
            safe[k] = v
    return safe
