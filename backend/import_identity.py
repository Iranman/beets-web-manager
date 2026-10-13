"""Deterministic identification of an import folder Beets skipped (Stage 2).

``identify_import_folder`` gathers evidence for one folder, evaluates every
candidate release with the canonical album decision, ranks Release Groups
deterministically and decides whether a Stage 2b pinned re-import is
allowed. It reads files and asks MusicBrainz/AcoustID, but never writes and
never calls AI: ``ai_rank`` is always ``None`` here and AI may only fill it
later, on the review row.

Layering: a workflow helper above ``library_service`` (it reuses the folder
evidence builder there). Only layer-5 workflows and routes may import it.
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.acoustid_service import _acoustid_lookup_cached_outcome, acoustid_failure_status
from backend.app_runtime import AUDIO_EXT, _MB_UUID_RE, _app_logger, _s
from backend.library_service import _build_folder_evidence
from backend.matching.folder_identity import (
    HARD_CONFLICTS, confirmed_recording, decide_auto_import, file_conflicts, rank_folder_candidates,
)
from backend.matching.track_alignment import _hit_recording_id
from backend.matching_service import _compact_preflight, _folder_release_preflight
from backend.musicbrainz_service import _mb_release_search_by_folder_tracks
from helpers_mb import _mb_release_search, _resolve_release_group_to_release

#: Upper bound on releases evaluated per folder (each costs one cached
#: MusicBrainz tracklist fetch and a preflight).
MAX_RELEASES = 10

# Directory entries the symlink-target audio probe may visit per folder (#381).
_LINKED_AUDIO_PROBE_MAX_ENTRIES = 20_000

_EMBEDDED_KEYS = {
    "mb_trackid": "musicbrainz_trackid",
    "mb_albumid": "musicbrainz_albumid",
    "mb_releasegroupid": "musicbrainz_releasegroupid",
}


def _uuid(value: Any) -> str:
    text = _s(value).strip().lower()
    return text if _MB_UUID_RE.match(text) else ""


def _embedded_mb_ids(path: str) -> Dict[str, str]:
    """Embedded MusicBrainz IDs (tier-1 evidence) from one audio file."""
    try:
        import mutagen
        media = mutagen.File(path, easy=True)
    except Exception:
        media = None
    out = {}
    for field, key in _EMBEDDED_KEYS.items():
        try:
            values = media.get(key) if media is not None else None
        except Exception:
            values = None
        out[field] = _uuid(values[0]) if values else ""
    return out


def _recursive_audio(folder: str) -> Tuple[List[str], bool]:
    """Audio files under the resolved folder, recursively, so conflict scans
    cover nested files too. Like ``_build_folder_evidence``, a symlinked file
    (or one reached through a symlinked directory) is not evidence; the flag
    reports that one exists, because a pinned Beets import would still pick
    it up unproven (``beets.util.sorted_walk`` follows symlinked directories)."""
    def is_link(p: Path) -> bool:
        return p.is_symlink() or os.path.isjunction(p) or p.resolve() != p

    budget = [_LINKED_AUDIO_PROBE_MAX_ENTRIES]

    def has_audio(top: Path) -> bool:  # follows links; `seen` guards loops
        # Fails closed: a link target too big to scan within the shared entry
        # budget, or one that cannot be read, counts as linked audio (#381).
        seen = set()

        def _raise(exc: OSError) -> None:
            raise exc

        try:
            for dirpath, dirnames, filenames in os.walk(top, followlinks=True, onerror=_raise):
                real = os.path.realpath(dirpath)
                if real in seen:
                    dirnames[:] = []
                    continue
                seen.add(real)
                budget[0] -= 1 + len(dirnames) + len(filenames)
                if budget[0] < 0:
                    return True
                if any(Path(n).suffix.lower() in AUDIO_EXT for n in filenames):
                    return True
        except OSError:
            return True
        return False

    try:
        root = Path(folder).resolve(strict=True)
        files, linked = [], False
        for dirpath, dirnames, filenames in os.walk(root):  # never descends links
            here = Path(dirpath)
            for name in list(dirnames):
                if is_link(here / name):
                    dirnames.remove(name)
                    linked = linked or has_audio(here / name)
            for name in filenames:
                p = here / name
                if p.suffix.lower() not in AUDIO_EXT:
                    continue
                if is_link(p):
                    linked = True
                elif p.is_file():
                    files.append(str(p))
        return sorted(files, key=str.lower), linked
    except OSError:
        return [], False


def _acoustid_capability(statuses: Counter, files: int) -> str:
    if not files:
        return "no_audio"
    for failure in ("not_configured", "auth_failed"):
        if statuses.get(failure):
            return failure
    if statuses.get("lookup_failed") == files:
        return "lookup_failed"
    return "partial" if statuses.get("lookup_failed") else "available"


def _top(counter: Counter, limit: int) -> List[str]:
    return [key for key, _ in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


def _text_search(ev: Dict[str, Any], folder: str, log: List[str]) -> List[str]:
    """MusicBrainz text search, same cascade as the folder suggestion path
    (tier-5 evidence: it only proposes releases; the decision comes from
    the per-release evaluation)."""
    album, artist = ev.get("guessed_album", ""), ev.get("guessed_artist", "")
    kwargs = dict(limit=8, year=ev.get("guessed_year", ""), track_count=int(ev.get("folder_track_count") or 0),
                  artist_mbid=ev.get("guessed_artist_mbid", ""), log=log)
    found = _mb_release_search(album, artist, **kwargs)
    if not found and artist:
        found = _mb_release_search(album, "", **kwargs)
    if not found and album:
        found = _mb_release_search(" ".join(album.split()[:3]), artist, **kwargs)
    if not found:
        found = _mb_release_search_by_folder_tracks(folder, artist=artist, log=log, limit=8)
    return [_uuid(c.get("mb_albumid")) for c in found or [] if _uuid(c.get("mb_albumid"))]


def identify_import_folder(folder: str, skip_reason: str = "") -> Dict[str, Any]:
    """Identify one folder Beets skipped. Returns a FolderIdentity dict::

        {
          "folder": str, "skip_reason": str,
          "candidates": [{"release_group_id", "representative_release_id",
                          "matching_decision", "deterministic_rank", "ai_rank": None,
                          "evidence": {...}}],
          "auto_import": {"eligible", "release_group_id", "release_id",
                          "reasons": [...], "post_conditions": [...]},
          "conflicts": [...],
          "evidence_capabilities": {"acoustid", "fingerprinted_files", "audio_files",
                                    "embedded_id_files", "musicbrainz"},
          "warnings": [...],
          "ai_state": {"status": "not_run"},
        }

    ``auto_import.eligible`` is the Stage 2b gate; the caller must still
    honor ``post_conditions`` after the pinned import.
    """
    folder = _s(folder).strip()
    skip_reason = _s(skip_reason).strip()
    log: List[str] = []
    warnings: List[str] = []

    ev = _build_folder_evidence(folder)
    audio_files: List[str] = list(ev.get("audio_files") or [])
    linked_audio = False
    if audio_files:  # the evidence builder already validated the folder
        scanned, linked_audio = _recursive_audio(folder)
        audio_files = scanned or audio_files

    # Per-file evidence: embedded IDs (tier 1) and AcoustID (tier 3).
    files: List[Dict[str, Any]] = []
    statuses: Counter = Counter()
    release_hits: Counter = Counter()
    group_hits: Counter = Counter()
    confirmed_files = 0
    for path in audio_files:
        try:
            outcome = _acoustid_lookup_cached_outcome(path)
            hits = list(outcome.data or []) if outcome.answered else None
            statuses[acoustid_failure_status(outcome) or "answered"] += 1
        except Exception as ex:
            _app_logger.warning("AcoustID lookup failed during folder identification: %s", type(ex).__name__)
            hits = None
            statuses["lookup_failed"] += 1
        row = {"path": path, "acoustid_hits": hits, **_embedded_mb_ids(path)}
        files.append(row)
        recording = confirmed_recording(hits)
        if not recording:
            continue
        file_releases, file_groups = set(), set()
        for hit in hits or []:
            if _hit_recording_id(hit) == recording:
                file_releases |= {_uuid(r) for r in hit.get("mb_albumids") or []}
                file_groups |= {_uuid(g) for g in hit.get("mb_releasegroupids") or [hit.get("mb_releasegroupid")]}
        confirmed_files += 1
        release_hits.update(file_releases - {""})  # counts files, not hits
        group_hits.update(file_groups - {""})

    embedded_releases = Counter(f["mb_albumid"] for f in files if f["mb_albumid"])
    embedded_groups = Counter(f["mb_releasegroupid"] for f in files if f["mb_releasegroupid"])
    capability = _acoustid_capability(statuses, len(audio_files))
    if capability != "available" and audio_files:
        warnings.append(f"fingerprint_{capability}")

    # Release seeds, strongest evidence first; each source is recorded.
    seeds: Dict[str, List[str]] = {}

    def _seed(release_id: str, source: str) -> None:
        if release_id and (release_id in seeds or len(seeds) < MAX_RELEASES):
            seeds.setdefault(release_id, [])
            if source not in seeds[release_id]:
                seeds[release_id].append(source)

    track_count = int(ev.get("folder_track_count") or len(audio_files))
    year = _s(ev.get("guessed_year"))
    for rid in _top(embedded_releases, MAX_RELEASES):  # every one; any left out blocks Stage 2b
        _seed(rid, "embedded_release_id")
    for rg in _top(embedded_groups, 2):
        _seed(_resolve_release_group_to_release(rg, log, year=year, track_count=track_count), "embedded_release_group_id")
    for rg in _top(group_hits, 3):
        _seed(_resolve_release_group_to_release(rg, log, year=year, track_count=track_count), "acoustid_release_group")
    for rid in _top(release_hits, 2):
        _seed(rid, "acoustid_release")
    musicbrainz = "ok"
    if audio_files:
        try:
            for rid in _text_search(ev, folder, log):
                _seed(rid, "musicbrainz_search")
        except Exception as exc:  # any search failure fails closed with a warning
            musicbrainz = "unavailable"
            warnings.append("musicbrainz_unavailable")
            _app_logger.warning("MusicBrainz search failed during folder identification: %s", type(exc).__name__)
    if any("musicbrainz" in line.lower() and "fail" in line.lower() for line in log):
        musicbrainz = "unavailable"
        if "musicbrainz_unavailable" not in warnings:
            warnings.append("musicbrainz_unavailable")

    releases: List[Dict[str, Any]] = []
    audio_count = len(audio_files)
    for release_id, sources in seeds.items():
        try:
            pre = _folder_release_preflight(folder, release_id, log=log, fingerprint_evidence=True)
        except Exception as ex:
            _app_logger.warning("Folder preflight failed: %s", type(ex).__name__)
            pre = {"error": "Preflight failed to run."}
        audio_count = max(audio_count, int(pre.get("audio_count") or 0))
        decision = pre.get("matching_decision") or {}
        releases.append({
            "release_id": release_id,
            "release_group_id": _uuid(pre.get("release_group") or decision.get("release_group_id")),
            "track_count": int(pre.get("expected") or 0),
            "sources": sources,
            "preflight": _compact_preflight(pre) or {},
            "matching_decision": decision,
            "error": _s(pre.get("error")) if not decision else "",
        })

    # An embedded Release ID names its Release Group too.
    tagged_groups = set(embedded_groups) | {
        r["release_group_id"] for r in releases
        if "embedded_release_id" in r["sources"] and r["release_group_id"]}
    candidates = rank_folder_candidates(releases, track_count, tagged_groups)
    folder_level = file_conflicts(files)
    if len(tagged_groups) > 1:
        folder_level.append("embedded_release_group_conflict")
    if linked_audio:
        folder_level.append("symlinked_audio_file")
    failed = sum(1 for r in releases if r["error"])
    auto = decide_auto_import(candidates, skip_reason=skip_reason, audio_count=audio_count,
                              folder_conflicts=folder_level, fingerprint_groups=group_hits,
                              unconfirmed_files=audio_count - confirmed_files,
                              failed_lookups=failed, embedded_releases=embedded_releases.keys())
    conflicts = sorted(set(folder_level) | {c for cand in candidates[:1] for c in cand["evidence"]["conflicts"] if c in HARD_CONFLICTS})
    if failed:
        warnings.append(f"release_lookup_failed:{failed}")
    if not audio_files:
        warnings.append("no_audio_files")
    return {
        "folder": folder,
        "skip_reason": skip_reason,
        "candidates": candidates,
        "auto_import": auto,
        "conflicts": conflicts,
        "evidence_capabilities": {
            "acoustid": capability,
            "fingerprinted_files": statuses.get("answered", 0),
            "audio_files": len(audio_files),
            "embedded_id_files": sum(1 for f in files if f["mb_trackid"] or f["mb_albumid"] or f["mb_releasegroupid"]),
            "musicbrainz": musicbrainz,
        },
        "warnings": warnings,
        "ai_state": {"status": "not_run"},
    }
