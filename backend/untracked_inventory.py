"""Read-only inventory of audio files under the music root that Beets does
not track. Discovery only: nothing is deleted, imported, moved, renamed or
retagged, and no AcoustID API call or fingerprint is made.

One walk of the tree and one fetch of the tracked library build every index;
content is hashed only for untracked files whose exact size matches a
tracked file (the only way they can be byte duplicates). AcoustID evidence
is read from the existing file cache only.

Categories (first that applies):

* ``exact_duplicate_of_tracked`` -- same size and same SHA-256 as a tracked file.
* ``same_recording_other_encoding`` -- the cached AcoustID recordings of the
  file include a Recording ID a tracked item carries.
* ``import_artifact`` -- staged/historical import naming: Beets ``.N.ext``
  duplicate suffix, download ``(NN)`` / ``{source-id}`` decorations, or a
  staging/temp folder.
* ``canonical_album_file_missing_from_beets`` -- sits in an album folder
  laid out like the library (``Artist (mbid)/Album (year) {rgid}/``) with a
  canonical ``Artist - Album - NN - Title.ext`` name.
* ``loose_singleton`` -- directly in an artist folder or the root.
* ``unknown`` -- none of the above; needs review.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".aiff", ".aif", ".wma",
                    ".alac", ".ape", ".wv", ".dsf", ".dff"}
CATEGORIES = ("exact_duplicate_of_tracked", "same_recording_other_encoding", "import_artifact",
              "canonical_album_file_missing_from_beets", "loose_singleton", "unknown")

_BEETS_DUP_SUFFIX = re.compile(r"\.\d+\.[A-Za-z0-9]+$")
_DOWNLOAD_DECORATION = re.compile(r"(\s\(\d{2}\)(\{[^{}]*\})?\.[A-Za-z0-9]+$)|(\{[^{}]+\}\.[A-Za-z0-9]+$)")
_STAGING_DIR = re.compile(r"(^|/)(_?staging|\.?tmp|temp|incomplete|downloads?|slskd|_import\w*|\.trash\w*)(/|$)", re.I)
_ARTIST_DIR = re.compile(r"\([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\)$", re.I)
_ALBUM_DIR = re.compile(r"\{[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\}$", re.I)
_CANONICAL_NAME = re.compile(r"^.+ - .+ - \d{2,3} - .+\.[A-Za-z0-9]+$")

CachedIds = Callable[[str], Optional[List[str]]]


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def walk_audio(root: Path) -> Iterable[os.DirEntry]:
    """Every audio file under ``root``, one pass, no symlink following."""
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False) and \
                                os.path.splitext(entry.name)[1].lower() in AUDIO_EXTENSIONS:
                            yield entry
                    except OSError:
                        continue
        except OSError:
            continue


def classify(rel: str, *, size: int, path: str, size_index: Dict[int, List[str]],
             tracked_recordings: Set[str], cached_ids: Optional[CachedIds],
             hash_cache: Dict[str, str]) -> Dict[str, Any]:
    """Category and evidence for one untracked file (read-only)."""
    evidence: Dict[str, Any] = {}
    same_size = size_index.get(size) or []
    if same_size:
        mine = _sha256(path)
        for other in same_size:
            if other not in hash_cache:
                try:
                    hash_cache[other] = _sha256(other)
                except OSError:
                    hash_cache[other] = ""
            if hash_cache[other] == mine:
                return {"category": "exact_duplicate_of_tracked", "sha256": mine, "duplicate_of": other}
        evidence["same_size_tracked_files"] = len(same_size)

    if cached_ids is not None:
        heard = cached_ids(path)
        if heard is None:
            evidence["acoustid"] = "not_cached"
        else:
            shared = sorted(set(heard) & tracked_recordings)
            if shared:
                return {"category": "same_recording_other_encoding", "recording_ids": shared, **evidence}
            evidence["acoustid"] = heard or "no_match"

    name = os.path.basename(rel)
    parts = rel.split("/")
    if _BEETS_DUP_SUFFIX.search(name) or _DOWNLOAD_DECORATION.search(name) or _STAGING_DIR.search(rel):
        return {"category": "import_artifact", **evidence}
    if len(parts) >= 3 and _ARTIST_DIR.search(parts[-3]) and _ALBUM_DIR.search(parts[-2]) and _CANONICAL_NAME.match(name):
        return {"category": "canonical_album_file_missing_from_beets", **evidence}
    if len(parts) <= 2:
        return {"category": "loose_singleton", **evidence}
    return {"category": "unknown", **evidence}


def build_inventory(music_root: Path, tracked_items: Iterable[Dict[str, Any]], *,
                    abs_path: Callable[[str], str], cached_ids: Optional[CachedIds],
                    out_dir: Path, progress: Optional[Callable[[Dict[str, Any]], None]] = None,
                    cancel: Optional[Callable[[], bool]] = None, sample_size: int = 25) -> Dict[str, Any]:
    """Walk once, classify every untracked audio file, persist evidence."""
    started = time.time()
    root = Path(music_root).resolve(strict=False)
    tracked_paths: Set[str] = set()
    tracked_recordings: Set[str] = set()
    for it in tracked_items:
        p = abs_path(str(it.get("path") or ""))
        if p:
            tracked_paths.add(str(Path(p).resolve(strict=False)))
        rid = str(it.get("mb_trackid") or "").strip().lower()
        if rid:
            tracked_recordings.add(rid)

    all_files: List[os.DirEntry] = list(walk_audio(root))
    walk_seconds = time.time() - started
    size_index: Dict[int, List[str]] = defaultdict(list)
    untracked: List[os.DirEntry] = []
    for entry in all_files:
        full = str(Path(entry.path).resolve(strict=False))
        try:
            size = entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
        if full in tracked_paths:
            size_index[size].append(full)
        else:
            untracked.append(entry)

    out_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = out_dir / "untracked_inventory.jsonl"
    tmp_path = out_dir / ".untracked_inventory.jsonl.tmp"
    counts: Counter = Counter()
    by_top: Dict[str, Counter] = defaultdict(Counter)
    samples: Dict[str, List[str]] = defaultdict(list)
    hash_cache: Dict[str, str] = {}
    hashed = cache_hits = cache_misses = 0
    with open(tmp_path, "w", encoding="utf-8") as fh:
        for index, entry in enumerate(untracked, 1):
            if cancel and cancel():
                raise RuntimeError("cancelled")
            rel = os.path.relpath(entry.path, root).replace(os.sep, "/")
            try:
                size = entry.stat(follow_symlinks=False).st_size
                result = classify(rel, size=size, path=entry.path, size_index=size_index,
                                  tracked_recordings=tracked_recordings, cached_ids=cached_ids,
                                  hash_cache=hash_cache)
            except OSError as ex:
                size, result = -1, {"category": "unknown", "error": type(ex).__name__}
            if "sha256" in result or "same_size_tracked_files" in result:
                hashed += 1
            if result.get("acoustid") == "not_cached":
                cache_misses += 1
            elif cached_ids is not None:
                cache_hits += 1
            category = result["category"]
            counts[category] += 1
            by_top[rel.split("/")[0]][category] += 1
            if len(samples[category]) < sample_size:
                samples[category].append(rel)
            fh.write(json.dumps({"path": rel, "size": size, **result}) + "\n")
            if progress and index % 2000 == 0:
                progress({"processed": index, "total": len(untracked), "counts": dict(counts)})
    os.replace(tmp_path, evidence_path)

    summary = {
        "generated_at": time.time(),
        "music_root": str(root),
        "audio_files_on_disk": len(all_files),
        "tracked_audio_files_on_disk": len(all_files) - len(untracked),
        "tracked_items_in_beets": len(tracked_paths),
        "untracked_audio_files": len(untracked),
        "counts": {c: counts.get(c, 0) for c in CATEGORIES},
        "top_level_folders_with_untracked": len(by_top),
        "largest_top_level_folders": sorted(
            ({"folder": k, "untracked": sum(v.values()), **{c: v.get(c, 0) for c in CATEGORIES}} for k, v in by_top.items()),
            key=lambda r: -r["untracked"])[:50],
        "samples": dict(samples),
        "stats": {
            "runtime_seconds": round(time.time() - started, 1),
            "walk_seconds": round(walk_seconds, 1),
            "files_content_hashed": hashed + len(hash_cache),
            "acoustid_cache_hits": cache_hits,
            "acoustid_cache_misses": cache_misses,
            "acoustid_api_calls": 0,
        },
        "evidence_file": str(evidence_path),
        "mutations_performed": 0,
    }
    (out_dir / "untracked_inventory_summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary
