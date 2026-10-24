"""Plex library coverage audit + auto-heal (ARCH-001 layer 4).

Reconciles three views of the same library:

1. filesystem: every artist/album folder under MUSIC_ROOT — folder naming
   (literal ``{Album MbId}``-style token residue, unresolved template
   tokens, malformed release-group stamps) plus per-file readability for
   the group the Plex media server runs as (PLEX_FS_GID), POSIX ACL masks
   included;
2. Beets: items and albums from the embedded library, reached only through
   ``beets_adapter`` (canonical album identity: ``mb_releasegroupid``);
3. Plex: the music section's indexed track index via ``plex_service``
   (file-path joins only — the audit never guesses identities by text).

The audit is read-only. Healing is three independently opt-in actions
(off by default, per action):

- chmod: add missing group read (and directory traverse) bits, and — only
  where no ACL entry covers Plex's group and a plain-mode fix suffices —
  chgrp the file to PLEX_FS_GID. Bits are only ever added, never removed;
  the same guarantee as the operator's one-time ``setfacl`` pass.
- touch: bump artist/album directory mtimes for repaired paths. Plex's
  incremental scanner decides what to reopen from mtimes, so a permission
  fix alone stays invisible to it (seen live 2026-10-23).
- refresh: trigger one Plex library refresh through the idempotent POST
  in ``plex_service._trigger_plex_refresh`` (workflow ``sync``).

Nothing here deletes, moves or rewrites content, and duplicate files stay
untouched: deletion remains behind the unattended-deletion authorization
(``backend.dedup_authorization``).
"""

from __future__ import annotations

import os
import posixpath
import re
import stat as stat_module
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import backend.plex_service as plex_service
from backend.app_runtime import (
    AUDIO_EXT,
    MUSIC_ROOT,
    PLEX_FS_GID,
    PLEX_SYNC_AUTO_CHMOD,
    PLEX_SYNC_AUTO_REFRESH,
    PLEX_SYNC_AUTO_TOUCH,
    PLEX_SYNC_MAX_REPAIR_PATHS,
    _LITERAL_PLACEHOLDER_RE,
    _MALFORMED_RELEASE_GROUP_STAMP_RE,
    _UNRESOLVED_TEMPLATE_TOKEN_RE,
    _app_logger,
    _norm,
    _redact_security_text,
    _s,
)
from backend.beets_adapter import BeetsError, BeetsUnavailableError, beets_adapter
from backend.cleanup_service import _album_cleanup_canonical_name

# ── ARCH-001 extracted code ──

_ACL_XATTR = "system.posix_acl_access"
# ACL entries and POSIX mode bits use different encodings for read/write/
# execute (ACL: 4/2/1 — mode: 0o40/0o20/0o10). Each domain keeps its own
# "needed bits" so the audit judges exactly what the kernel checks.
_ACL_PERM_READ = 0o4
_ACL_PERM_EXEC = 0o1
_ACL_NEED_DIR = _ACL_PERM_READ | _ACL_PERM_EXEC
_ACL_NEED_FILE = _ACL_PERM_READ
_MODE_NEED_DIR = stat_module.S_IRGRP | stat_module.S_IXGRP
_MODE_NEED_FILE = stat_module.S_IRGRP
_ACL_TAG_GROUP_OBJ = 0x04
_ACL_TAG_GROUP = 0x08
_ACL_TAG_MASK = 0x10
_SAMPLE_FILES_PER_ALBUM = 5
_MAX_REPORT_ROWS = 50

_UUID_SUFFIX_RE = re.compile(
    r"\s+\{([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\}\s*$", re.IGNORECASE)


# ── small injectable wrappers (tests patch these; no hidden side effects) ──

def _stat(path: Path) -> Optional[os.stat_result]:
    try:
        return os.stat(str(path), follow_symlinks=False)
    except OSError:
        return None


def _getxattr(path: Path, attr: str) -> Optional[bytes]:
    getter = getattr(os, "getxattr", None)
    if getter is None:  # platform has no xattr API (e.g. Windows)
        return None
    try:
        return getter(str(path), attr, follow_symlinks=False)
    except OSError:
        return None


def _chmod(path: Path, mode: int) -> None:
    os.chmod(str(path), mode, follow_symlinks=False)


def _chown(path: Path, uid: int, gid: int) -> None:
    chown = getattr(os, "chown", None)
    if chown is None:  # platform has no ownership API (e.g. Windows)
        raise OSError("os.chown is unavailable on this platform")
    os.chown(str(path), uid, gid, follow_symlinks=False)


def _utime(path: Path) -> None:
    os.utime(str(path), None, follow_symlinks=False)


# ── POSIX ACL decoding ──

def _decode_posix_acl(raw: Optional[bytes]) -> Optional[Dict[str, Any]]:
    """Decode ``system.posix_acl_access`` bytes: a u16 version header
    followed by ``(tag, perm, id)`` u16/u16/u32 entries. Unknown version or
    truncated data returns ``None`` (callers then judge plain mode bits)."""
    if not raw or len(raw) < 4:
        return None
    if int.from_bytes(raw[0:2], "little") != 0x0001:
        return None
    named_groups: Dict[int, int] = {}
    group_obj: Optional[int] = None
    mask: Optional[int] = None
    offset = 2
    while offset + 8 <= len(raw):
        tag = int.from_bytes(raw[offset:offset + 2], "little")
        perm = int.from_bytes(raw[offset + 2:offset + 4], "little") & 0o7
        entry_id = int.from_bytes(raw[offset + 4:offset + 8], "little")
        offset += 8
        if tag == _ACL_TAG_GROUP_OBJ:
            group_obj = perm
        elif tag == _ACL_TAG_GROUP:
            named_groups[entry_id] = perm
        elif tag == _ACL_TAG_MASK:
            mask = perm
    return {"named_groups": named_groups, "group_obj": group_obj, "mask": mask}


def _perm_allows(perm: Optional[int], need: int, mask: Optional[int]) -> bool:
    if perm is None:
        return False
    effective = perm & mask if mask is not None else perm
    return (effective & need) == need


def _file_readable_for_gid(st: os.stat_result, acl: Optional[Dict[str, Any]],
                           gid: int, *, is_dir: bool) -> Tuple[bool, str]:
    """Return ``(readable, why)`` judged as the kernel would for a process
    running with primary group ``gid``. Owner-bit checks are deliberately
    skipped: Plex's own uid is neither mapped nor needed (stack players run
    files as root or another account)."""
    acl_need = _ACL_NEED_DIR if is_dir else _ACL_NEED_FILE
    if acl is not None:
        named = acl["named_groups"].get(gid)
        if named is not None:
            if _perm_allows(named, acl_need, acl["mask"]):
                return True, ""
            return False, "posixacl_named_group_denied"
        # No named entry for Plex's group: only the ACL's group-owner entry
        # can help, and only when the file's owning group is Plex's group.
        if st.st_gid == gid and _perm_allows(acl["group_obj"], acl_need, acl["mask"]):
            return True, ""
        return False, "posixacl_no_entry_for_group"
    if st.st_gid != gid:
        return False, "group_owner_is_not_plex_group"
    mode_need = _MODE_NEED_DIR if is_dir else _MODE_NEED_FILE
    if (st.st_mode & mode_need) == mode_need:
        return True, ""
    return False, "group_bits_missing"


def _repair_plan(st: os.stat_result, acl: Optional[Dict[str, Any]], gid: int,
                 *, is_dir: bool) -> Dict[str, Any]:
    """Minimal permission repair for one path. chmod only ever adds bits
    (with a POSIX ACL present, chmod's group bits translate to the ACL mask,
    which re-enables existing named-group entries — exactly the operator's
    ``chmod g+r`` behavior). chgrp applies only on ACL-less filesystems
    whose owning group differs from Plex's."""
    need = _ACL_NEED_DIR if is_dir else _ACL_NEED_FILE
    readable, why = _file_readable_for_gid(st, acl, gid, is_dir=is_dir)
    if readable:
        return {"status": "ok"}
    bits = "rwx" if is_dir else "rw"
    if why == "posixacl_named_group_denied":
        named = acl["named_groups"].get(gid)
        if named is not None and (named & need) == need \
                and not _perm_allows(named, need, acl["mask"]):
            # The entry has the bits but the mask hides them. chmod raises the
            # mask (with a POSIX ACL, chmod's group bits map to the mask) —
            # this is the standard slskd-created-file shape.
            return {
                "status": "repairable",
                "chmod": st.st_mode | (stat_module.S_IRGRP
                                       | (stat_module.S_IXGRP if is_dir else 0)),
            }
        return {
            "status": "blocked",
            "reason": ("file has an ACL named entry for gid %d without the needed "
                       "bits, and chmod cannot change named entries; fix with "
                       "setfacl -m g:%d:%s <path> (operator action)" % (gid, gid, bits)),
        }
    if why == "posixacl_no_entry_for_group":
        return {
            "status": "blocked",
            "reason": ("file has a POSIX ACL but no entry for gid %d; chmod cannot "
                       "add named entries, so fix with setfacl -m g:%d:%s <path> "
                       "(operator action)" % (gid, gid, bits)),
        }
    plan: Dict[str, Any] = {"status": "repairable"}
    plan["chmod"] = st.st_mode | (stat_module.S_IRGRP | (stat_module.S_IXGRP if is_dir else 0))
    if acl is None and st.st_gid != gid:
        plan["chown_gid"] = gid
    return plan


# ── naming classification ──

def _classify_dir_name(name: str) -> Dict[str, Any]:
    """Classify one album folder name. ``ok`` covers both plain
    ``Title (Year)`` and the real-UUID ``Title (Year) {rgid}`` style that
    canonical cleanup (and now Lidarr) emits."""
    text = _s(name).strip()
    if _LITERAL_PLACEHOLDER_RE.search(text):
        status = "literal_token"
    elif _MALFORMED_RELEASE_GROUP_STAMP_RE.search(text):
        # Before unresolved-token: the unresolved template regex also matches
        # the malformed `{uuid,}` stamp, and the stamp is the better verdict.
        status = "malformed_stamp"
    elif _UNRESOLVED_TEMPLATE_TOKEN_RE.search(text):
        status = "unresolved_token"
    else:
        status = "ok"
    guid = ""
    match = _UUID_SUFFIX_RE.search(text)
    if match:
        guid = match.group(1).lower()
    return {"status": status, "guid_suffix": guid, "name": text}


_DIR_NAME_SEVERITY = {"literal_token": 3, "unresolved_token": 2, "malformed_stamp": 1, "ok": 0}


def _worse_name_status(current: str, candidate: str) -> str:
    if _DIR_NAME_SEVERITY.get(candidate, 0) > _DIR_NAME_SEVERITY.get(current, 0):
        return candidate
    return current


# ── filesystem walk ──

def _iter_child_dirs(path: Path) -> List[Path]:
    try:
        children = sorted(Path(path).iterdir())
    except OSError:
        return []
    return [child for child in children if child.is_dir() and not child.is_symlink()]


def _audio_suffixes() -> Tuple[str, ...]:
    """Normalized with-dot audio suffixes from app_runtime's AUDIO_EXT."""
    normalized = set()
    for value in AUDIO_EXT:
        text = _s(value).casefold()
        if not text:
            continue
        if not text.startswith("."):
            text = "." + text
        normalized.add(text)
    return tuple(sorted(normalized))


def _album_audio_scan(album_dir: Path, suffixes: Tuple[str, ...],
                      sample_limit: Optional[int] = None) -> Tuple[int, List[Path]]:
    """One bounded listing pass over an album dir (its own files plus
    files one nested level down — multi-disc subfolders). Returns
    ``(audio_file_count, sampled_files)``; ``sample_limit=None`` returns all
    audio files (used by the heal)."""
    sample: List[Path] = []
    total = 0
    work: List[Path] = []
    try:
        entries = sorted(album_dir.iterdir())
    except OSError:
        return 0, sample
    for entry in entries:
        if entry.is_symlink():
            continue
        if entry.is_file():
            work.append(entry)
        elif entry.is_dir():
            try:
                work.extend(leaf for leaf in sorted(entry.iterdir())
                            if leaf.is_file() and not leaf.is_symlink())
            except OSError:
                pass
    for entry in work:
        if entry.suffix.casefold() in suffixes:
            total += 1
            if sample_limit is None or len(sample) < sample_limit:
                sample.append(entry)
    return total, sample


def filesystem_findings(sample_limit: Optional[int] = _SAMPLE_FILES_PER_ALBUM) -> Dict[str, Any]:
    """Read-only walk of MUSIC_ROOT: naming + Plex-group readability per
    artist/album dir. ``sample_limit=None`` checks every audio file (heal)."""
    suffixes = _audio_suffixes()
    findings: Dict[str, Any] = {
        "music_root": str(MUSIC_ROOT),
        "artists": [],
        "totals": {
            "artist_dirs": 0, "album_dirs": 0, "audio_files": 0,
            "files_checked": 0, "unreadable_files": 0,
            "unreadable_album_dirs": 0, "unreadable_artist_dirs": 0,
            "repairable_paths": 0, "blocked_paths": 0,
            "literal_token_dirs": 0, "unresolved_token_dirs": 0,
            "malformed_stamp_dirs": 0, "noncanonical_dirs": 0,
            "guid_suffix_dirs": 0, "empty_album_dirs": 0,
        },
    }
    totals = findings["totals"]
    for artist_dir in _iter_child_dirs(Path(findings["music_root"])):
        totals["artist_dirs"] += 1
        artist_entry: Dict[str, Any] = {
            "name": artist_dir.name,
            "issues": {},
            "albums": [],
        }
        artist_st = _stat(artist_dir)
        artist_acl = _decode_posix_acl(_getxattr(artist_dir, _ACL_XATTR))
        if artist_st is not None:
            readable, why = _file_readable_for_gid(artist_st, artist_acl, PLEX_FS_GID, is_dir=True)
            artist_entry["readable"] = readable
            if not readable:
                totals["unreadable_artist_dirs"] += 1
                artist_entry["issues"].setdefault("artist_unreadable", [artist_dir.name])
        else:
            artist_entry["readable"] = None
        for album_dir in _iter_child_dirs(artist_dir):
            totals["album_dirs"] += 1
            naming = _classify_dir_name(album_dir.name)
            if naming["status"] != "ok":
                totals[naming["status"] + "_dirs"] += 1
            if naming["guid_suffix"]:
                totals["guid_suffix_dirs"] += 1
            audio_total, samples = _album_audio_scan(album_dir, suffixes,
                                                     sample_limit=sample_limit)
            album_entry: Dict[str, Any] = {
                "name": album_dir.name,
                "path": str(album_dir),
                "status": naming["status"],
                "guid_suffix": naming["guid_suffix"],
                "audio_files": audio_total,
                "unreadable_files": 0,
                "repairs": [],
            }
            totals["audio_files"] += audio_total
            if audio_total == 0:
                totals["empty_album_dirs"] += 1
            album_unreadable = 0
            for audio_file in samples:
                st = _stat(audio_file)
                if st is None:
                    continue
                acl = _decode_posix_acl(_getxattr(audio_file, _ACL_XATTR))
                totals["files_checked"] += 1
                readable, why = _file_readable_for_gid(st, acl, PLEX_FS_GID, is_dir=False)
                if not readable:
                    album_unreadable += 1
                    totals["unreadable_files"] += 1
                    plan = _repair_plan(st, acl, PLEX_FS_GID, is_dir=False)
                    if plan["status"] == "repairable":
                        totals["repairable_paths"] += 1
                    else:
                        totals["blocked_paths"] += 1
                    album_entry["repairs"].append({
                        "file": audio_file.name,
                        "path": str(audio_file),
                        "status": "repairable" if plan["status"] == "repairable" else "blocked",
                        "plan": {"chmod": plan.get("chmod"), "chown_gid": plan.get("chown_gid"),
                                 "reason": plan.get("reason")},
                        "why": why,
                    })
            if album_unreadable:
                album_entry["unreadable_files"] = album_unreadable
                totals["unreadable_album_dirs"] += 1
                artist_entry["issues"].setdefault("album_dirs_unreadable", []).append(album_dir.name)
            if naming["status"] != "ok":
                artist_entry["issues"].setdefault("folder_naming", []).append(album_dir.name)
            artist_entry["albums"].append(album_entry)
        findings["artists"].append(artist_entry)
    return findings


# ── beets + Plex join ──

def _beets_album_dirs() -> Dict[str, Dict[str, Any]]:
    """Map album directory (normalized POSIX path) -> album metadata from the
    embedded library. Paths come only from ``beets_adapter`` (which already
    rejects unsafe relative paths); rows without a usable absolute path are
    skipped. POSIX path semantics on purpose: Beets runs in the container, so
    the stored paths are POSIX paths regardless of where this app runs."""
    by_dir: Dict[str, Dict[str, Any]] = {}
    try:
        items = beets_adapter.get_items()
    except (BeetsError, BeetsUnavailableError) as ex:
        _app_logger.warning("beets item read failed for plex sync audit: %s", type(ex).__name__)
        return by_dir
    for item in items:
        raw_path = item.get("path")
        text = os.fsdecode(raw_path) if isinstance(raw_path, (bytes, bytearray)) else _s(raw_path)
        if not text:
            continue
        parent = posixpath.dirname(text.replace("\\", "/"))
        if not posixpath.isabs(parent):
            continue
        dir_key = posixpath.normpath(parent).casefold()
        row = by_dir.setdefault(dir_key, {
            "album": _s(item.get("album")),
            "albumartist": _s(item.get("albumartist")),
            "year": _s(item.get("year")),
            "mb_releasegroupid": _s(item.get("mb_releasegroupid")),
        })
        if not row.get("mb_releasegroupid") and item.get("mb_releasegroupid"):
            row["mb_releasegroupid"] = _s(item.get("mb_releasegroupid"))
    return by_dir


def _build_plex_presence() -> Optional[Dict[str, Any]]:
    """Build the Plex track-index containment structures once. ``None`` when
    Plex is unconfigured, unreachable or has no music section."""
    try:
        _, section_key, section_title = plex_service._plex_find_music_section()
    except Exception:
        section_key, section_title = None, None
    if not section_key:
        return None
    try:
        index = plex_service._plex_section_track_index(section_key)
    except Exception as ex:
        _app_logger.warning("plex index build failed: %s", _redact_security_text(_s(ex)))
        return None
    return {
        "settings": plex_service._plex_settings(),
        "path_exact": index.get("path_exact") or {},
        "path_case": index.get("path_case") or {},
        "suffix_paths": index.get("suffix_paths") or {},
        "tracks_by_key": index.get("tracks_by_key") or {},
        "roots": index.get("plex_music_roots") or [],
        "fetched": index.get("fetched", 0),
        "section_title": section_title,
        "section_key": section_key,
    }


def _path_in_plex(file_path: str, presence: Dict[str, Any]) -> bool:
    """True when THIS filesystem file maps to any indexed Plex track by the
    playlist matcher's path/suffix rules."""
    for candidate in plex_service._plex_beets_path_candidates(file_path, presence["settings"]):
        normalized = plex_service._plex_norm_path(candidate)
        if normalized and normalized in presence["path_exact"]:
            return True
        if presence["path_case"].get(plex_service._plex_path_case_key(candidate)):
            return True
        for suffix_key in plex_service._plex_suffix_keys_for_path(candidate, presence["roots"]):
            if presence["suffix_paths"].get(suffix_key):
                return True
    return False


def _plex_section_counts(section_key: Any) -> Dict[str, Any]:
    counts: Dict[str, Any] = {}
    try:
        for label, type_number in (("artist", 8), ("album", 9), ("track", 10)):
            container = plex_service._plex_request(
                f"/library/sections/{section_key}/all",
                {"type": type_number, "X-Plex-Container-Size": 1},
                timeout=15, attempts=1).get("MediaContainer", {})
            counts[label] = container.get("size")
    except Exception as ex:
        counts["error"] = _s(ex)[:120]
    return counts


# ── report assembly ──

def _cap(values: List[Any], limit: int = _MAX_REPORT_ROWS) -> Tuple[List[Any], int]:
    return list(values[:limit]), max(0, len(values) - limit)


def audit_plex_sync(log: Optional[List[str]] = None) -> Dict[str, Any]:
    """Full read-only audit: filesystem naming + readability, beets album
    join by directory, Plex index presence, artist coverage both ways."""
    findings = filesystem_findings()
    totals = dict(findings["totals"])
    by_dir = _beets_album_dirs()
    presence = _build_plex_presence()

    plex_artists = set()
    for row in (presence or {}).get("tracks_by_key", {}).values():
        if isinstance(row, dict) and row.get("artist"):
            plex_artists.add(_norm(row.get("artist")))

    artist_rows: List[Dict[str, Any]] = []
    missing_plex_dirs: List[str] = []
    album_status_counts = {"in_plex": 0, "missing_from_plex": 0,
                           "not_in_beets": 0, "plex_unavailable": 0}

    for artist_entry in findings["artists"]:
        row = {
            "artist": artist_entry["name"],
            "albums": len(artist_entry["albums"]),
            "unreadable_album_dirs": len(artist_entry["issues"].get("album_dirs_unreadable") or []),
            "folder_naming_dirs": len(artist_entry["issues"].get("folder_naming") or []),
            "examples": {},
        }
        for issue, names in artist_entry["issues"].items():
            capped, _ = _cap(names, 3)
            row["examples"][issue] = capped
        artist_rows.append(row)

        for album_entry in artist_entry["albums"]:
            album_path = posixpath.normpath(_s(album_entry["path"]).replace("\\", "/"))
            beets_meta = by_dir.get(album_path.casefold())
            if not presence:
                verdict = "plex_unavailable"
                album_entry["plex"] = None
            elif beets_meta is None:
                verdict = "not_in_beets"
                album_entry["plex"] = {"in_plex": None}
            else:
                expected = _album_cleanup_canonical_name(
                    beets_meta.get("album", ""), beets_meta.get("year", ""),
                    beets_meta.get("mb_releasegroupid", ""))
                if expected and expected.casefold() != _s(album_entry["name"]).casefold() \
                        and album_entry["status"] == "ok" and not album_entry["guid_suffix"]:
                    album_entry["canonical_name"] = expected
                    totals["noncanonical_dirs"] += 1
                present = any(
                    _path_in_plex(_s(audio_file), presence)
                    for audio_file in _album_audio_scan(
                        Path(album_entry["path"]), _audio_suffixes(), sample_limit=2)[1]
                )
                album_entry["plex"] = {"in_plex": present}
                if present:
                    verdict = "in_plex"
                else:
                    verdict = "missing_from_plex"
                    missing_plex_dirs.append(album_path)
            album_entry["verdict"] = verdict
            album_status_counts[verdict] = album_status_counts.get(verdict, 0) + 1

    missing_capped, missing_overflow = _cap(missing_plex_dirs)
    artists_capped, artists_overflow = _cap(artist_rows)
    beets_only_artists = [
        artist_entry["name"] for artist_entry in findings["artists"]
        if _norm(artist_entry["name"]) not in plex_artists
    ]
    beets_only_capped, beets_only_overflow = _cap(beets_only_artists)

    report: Dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "music_root": str(MUSIC_ROOT),
        "filesystem": {"totals": totals},
        "artists": artists_capped,
        "artists_reported": len(artist_rows),
        "artists_truncated": artists_overflow,
        "beets_only_artists": beets_only_capped,
        "beets_only_artists_total": len(beets_only_artists),
        "beets_only_artists_truncated": beets_only_overflow,
        "album_status": album_status_counts,
        "missing_from_plex_dirs": missing_capped,
        "missing_from_plex_total": len(missing_plex_dirs),
        "missing_from_plex_truncated": missing_overflow,
        "plex": {
            "configured": presence is not None,
            "section_title": (presence or {}).get("section_title"),
            "indexed_files": (presence or {}).get("fetched", 0),
        },
        "actions": {
            "repairable_paths": totals.get("repairable_paths", 0),
            "blocked_paths": totals.get("blocked_paths", 0),
            "folder_naming_dirs": (totals.get("literal_token_dirs", 0)
                                   + totals.get("unresolved_token_dirs", 0)
                                   + totals.get("malformed_stamp_dirs", 0)),
        },
    }
    section_key = (presence or {}).get("section_key")
    if section_key:
        report["plex"]["section_counts"] = _plex_section_counts(section_key)

    if log is not None:
        log.append("[plex-sync] audit done; filesystem totals=%s" % report["filesystem"]["totals"])
    return report


def plex_sync_audit_job(log: List[str], cancel: Any = None) -> Dict[str, Any]:
    """Job entry for ``jobs.start_python``: the read-only audit itself."""
    del cancel  # read-only walk has no cancellation points
    return {"report": audit_plex_sync(log)}


# ── heal ──

def apply_plex_sync_heal(log: Optional[List[str]] = None, *,
                         allow_chmod: bool = PLEX_SYNC_AUTO_CHMOD,
                         allow_touch: bool = PLEX_SYNC_AUTO_TOUCH,
                         allow_refresh: bool = PLEX_SYNC_AUTO_REFRESH,
                         max_paths: int = PLEX_SYNC_MAX_REPAIR_PATHS) -> Dict[str, Any]:
    """Execute the individually opt-in heals for unreadable files. Works
    over a full (unsampled) filesystem pass; nothing is deleted or moved."""
    log = log if log is not None else []
    findings = filesystem_findings(sample_limit=None)
    results: Dict[str, Any] = {
        "chmod_applied": 0, "chown_applied": 0, "dirs_touched": 0,
        "failures": [], "paths_attempted": 0, "paths_remaining": 0,
        "refresh": False, "blocked_paths": 0,
    }
    if not (allow_chmod or allow_touch):
        results["note"] = "no repair actions enabled"
        if log is not None:
            log.append("[plex-sync] heal requested with all actions disabled; nothing done")
        return results

    dirs_to_touch: set = set()
    failures: List[str] = []
    attempted = 0

    for artist_entry in findings["artists"]:
        artist_dir = artist_entry["issues"].get("artist_unreadable")
        if artist_dir and allow_chmod:
            artist_path = Path(findings["music_root"]) / _s(artist_dir[0])
            st = _stat(artist_path)
            acl = _decode_posix_acl(_getxattr(artist_path, _ACL_XATTR))
            if st is not None:
                plan = _repair_plan(st, acl, PLEX_FS_GID, is_dir=True)
                if plan["status"] == "repairable":
                    attempted += 1
                    try:
                        if plan.get("chown_gid") is not None and allow_chmod:
                            gid = int(plan["chown_gid"])
                            if st.st_gid != gid:
                                _chown(artist_path, -1, gid)
                                results["chown_applied"] += 1
                        _chmod(artist_path, int(plan["chmod"]))
                        results["chmod_applied"] += 1
                        dirs_to_touch.add(artist_path)
                    except OSError as ex:
                        failures.append("artist dir repair failed: errno %s" % (ex.errno,))
                else:
                    results["blocked_paths"] += 1

    for artist_entry in findings["artists"]:
        for album_entry in artist_entry["albums"]:
            # Directory repair is independent of file states: a folder Plex
            # cannot traverse breaks every file inside it, sampled or not.
            album_dir = Path(album_entry["path"])
            album_st = _stat(album_dir)
            album_acl = _decode_posix_acl(_getxattr(album_dir, _ACL_XATTR))
            if album_st is not None and allow_chmod:
                plan = _repair_plan(album_st, album_acl, PLEX_FS_GID, is_dir=True)
                if plan["status"] == "repairable":
                    attempted += 1
                    if attempted <= max_paths:
                        try:
                            if plan.get("chown_gid") is not None:
                                gid = int(plan["chown_gid"])
                                if album_st.st_gid != gid:
                                    _chown(album_dir, -1, gid)
                                    results["chown_applied"] += 1
                            _chmod(album_dir, int(plan["chmod"]))
                            results["chmod_applied"] += 1
                            dirs_to_touch.add(album_dir)
                        except OSError as ex:
                            failures.append("album dir repair failed: errno %s" % (ex.errno,))
                elif plan["status"] == "blocked":
                    results["blocked_paths"] += 1
            for record in album_entry.get("repairs") or []:
                if record["status"] != "repairable":
                    results["blocked_paths"] += 1
                    continue
                attempted += 1
                if attempted > max_paths:
                    continue
                plan = record.get("plan") or {}
                file_path = Path(record.get("path") or "")
                st = _stat(file_path)
                if st is None:
                    failures.append("missing file named for repair: %s" % file_path.name)
                    continue
                try:
                    if allow_chmod:
                        if plan.get("chown_gid") is not None:
                            gid = int(plan["chown_gid"])
                            if st.st_gid != gid:
                                _chown(file_path, -1, gid)
                                results["chown_applied"] += 1
                        target_mode = plan.get("chmod")
                        if target_mode:
                            _chmod(file_path, int(target_mode))
                            results["chmod_applied"] += 1
                    dirs_to_touch.add(album_dir)
                except OSError as ex:
                    failures.append("repair failed: errno %s" % (ex.errno,))

    results["paths_attempted"] = min(attempted, max_paths)
    results["paths_remaining"] = max(0, attempted - max_paths)
    capped_failures, _ = _cap(failures, 20)
    results["failures"] = capped_failures

    if allow_touch and dirs_to_touch:
        touched = 0
        for dir_path in sorted(dirs_to_touch):
            try:
                _utime(dir_path)
                # The scanner also gates on the parent artist dir's mtime.
                _utime(dir_path.parent)
                touched += 1
            except OSError:
                continue
        results["dirs_touched"] = touched

    repaired = results["chmod_applied"] > 0 or results["chown_applied"] > 0
    if allow_refresh and (repaired or results["dirs_touched"]):
        try:
            results["refresh"] = plex_service._trigger_plex_refresh(log, workflow="sync")
        except Exception as ex:
            _app_logger.warning("plex sync refresh failed: %s", _redact_security_text(_s(ex)))

    if log is not None:
        log.append("[plex-sync] heal executed; %s" %
                   {k: v for k, v in results.items() if k != "failures"})
    return results


def plex_sync_heal_job(log: List[str], cancel: Any = None) -> Dict[str, Any]:
    """Job entry for ``jobs.start_python``. Every action inside is opt-in
    (env or route); with all disabled it still returns the report of what
    WOULD happen, so the UI can show a plan first."""
    del cancel
    return {"heal": apply_plex_sync_heal(log)}
