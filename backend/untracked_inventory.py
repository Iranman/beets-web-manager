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
  duplicate suffix, download ``(NN)`` / ``{source-id}`` decorations, a
  never-resolved naming token such as ``{Album MbId}``, or a staging/temp
  folder.
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
# A naming-template token an old import never resolved, e.g. "{Album MbId}"
# or "{Track ArtistMbId}" (Title Case words; real "{mbid}" suffixes are hex).
_UNRESOLVED_NAMING_TOKEN = re.compile(r"\{[A-Z][A-Za-z]*(?: [A-Z][A-Za-z]*)*\}")

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
             hash_cache: Dict[str, str], sha_of: Optional[Callable[[str], str]] = None) -> Dict[str, Any]:
    """Category and evidence for one untracked file (read-only).

    ``sha_of`` hashes a path (memoized by the caller, so a file unchanged
    since the last inventory is not re-read); tracked files' hashes are
    memoized in ``hash_cache``."""
    evidence: Dict[str, Any] = {}
    same_size = size_index.get(size) or []
    if same_size:
        mine = (sha_of or _sha256)(path)
        for other in same_size:
            if other not in hash_cache:
                try:
                    hash_cache[other] = _sha256(other)
                except OSError:
                    hash_cache[other] = ""
            if hash_cache[other] == mine:
                return {"category": "exact_duplicate_of_tracked", "sha256": mine, "duplicate_of": other}
        evidence["same_size_tracked_files"] = len(same_size)
        evidence["sha256"] = mine

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
    if (_BEETS_DUP_SUFFIX.search(name) or _DOWNLOAD_DECORATION.search(name) or _STAGING_DIR.search(rel)
            or _UNRESOLVED_NAMING_TOKEN.search(rel)):
        return {"category": "import_artifact", **evidence}
    if len(parts) >= 3 and _ARTIST_DIR.search(parts[-3]) and _ALBUM_DIR.search(parts[-2]) and _CANONICAL_NAME.match(name):
        return {"category": "canonical_album_file_missing_from_beets", **evidence}
    if len(parts) <= 2:
        return {"category": "loose_singleton", **evidence}
    return {"category": "unknown", **evidence}


EVIDENCE_FILE = "untracked_inventory.jsonl"
SUMMARY_FILE = "untracked_inventory_summary.json"


def load_previous(out_dir: Path) -> Dict[str, Dict[str, Any]]:
    """The last inventory's records by relative path ({} if none)."""
    records: Dict[str, Dict[str, Any]] = {}
    try:
        with open(out_dir / EVIDENCE_FILE, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                    records[rec["path"]] = rec
                except (ValueError, KeyError):
                    continue
    except OSError:
        pass
    return records


def _tracked_fingerprint(tracked_paths: Set[str], sizes: Dict[str, int], recordings: Set[str]) -> str:
    digest = hashlib.sha256()
    for path in sorted(tracked_paths):
        digest.update(f"{path}|{sizes.get(path, -1)}\n".encode("utf-8", "surrogateescape"))
    digest.update(b"|recordings|")
    for rid in sorted(recordings):
        digest.update(rid.encode() + b"\n")
    return digest.hexdigest()


def _peak_memory_mb() -> Optional[float]:
    try:
        import resource
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
    except (ImportError, OSError):
        return None


def build_inventory(music_root: Path, tracked_items: Iterable[Dict[str, Any]], *,
                    abs_path: Callable[[str], str], cached_ids: Optional[CachedIds],
                    out_dir: Path, progress: Optional[Callable[[Dict[str, Any]], None]] = None,
                    cancel: Optional[Callable[[], bool]] = None, sample_size: int = 25,
                    incremental: bool = True, checkpoint_every: int = 5000) -> Dict[str, Any]:
    """Walk once, classify every untracked audio file, persist evidence.

    Incremental: a record from the last inventory is reused as-is when the
    file's size and mtime are unchanged and the tracked library's fingerprint
    is unchanged; when only the library changed, the file is re-classified
    but its recorded SHA-256 is reused. Only new or changed files are hashed
    or looked up. Progress is published as checkpoints (the job persists them)."""
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

    previous = load_previous(out_dir) if incremental else {}
    try:
        prev_summary = json.loads((out_dir / SUMMARY_FILE).read_text(encoding="utf-8")) if incremental else {}
    except (OSError, ValueError):
        prev_summary = {}

    all_files: List[os.DirEntry] = list(walk_audio(root))
    walk_seconds = time.time() - started
    size_index: Dict[int, List[str]] = defaultdict(list)
    tracked_sizes: Dict[str, int] = {}
    untracked: List[Any] = []
    restatted = 0
    for entry in all_files:
        full = str(Path(entry.path).resolve(strict=False))
        try:
            st = entry.stat(follow_symlinks=False)
            restatted += 1
        except OSError:
            continue
        if full in tracked_paths:
            size_index[st.st_size].append(full)
            tracked_sizes[full] = st.st_size
        else:
            untracked.append((entry, st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))))
    fingerprint = _tracked_fingerprint(tracked_paths, tracked_sizes, tracked_recordings)
    library_unchanged = bool(previous) and prev_summary.get("tracked_fingerprint") == fingerprint

    out_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = out_dir / EVIDENCE_FILE
    tmp_path = out_dir / ".untracked_inventory.jsonl.tmp"
    counts: Counter = Counter()
    by_top: Dict[str, Counter] = defaultdict(Counter)
    samples: Dict[str, List[str]] = defaultdict(list)
    hash_cache: Dict[str, str] = {}
    stats = Counter()

    with open(tmp_path, "w", encoding="utf-8") as fh:
        for index, (entry, size, mtime_ns) in enumerate(untracked, 1):
            if cancel and cancel():
                raise RuntimeError("cancelled")
            rel = os.path.relpath(entry.path, root).replace(os.sep, "/")
            prev = previous.get(rel)
            unchanged = bool(prev) and prev.get("size") == size and prev.get("mtime_ns") == mtime_ns
            if unchanged and library_unchanged:
                record = prev
                stats["records_reused"] += 1
            else:
                known = (prev or {}).get("sha256") if unchanged else None

                def sha_of(path, _known=known):
                    if _known:
                        stats["hashes_reused"] += 1
                        return _known
                    stats["files_rehashed"] += 1
                    return _sha256(path)

                try:
                    result = classify(rel, size=size, path=entry.path, size_index=size_index,
                                      tracked_recordings=tracked_recordings, cached_ids=cached_ids,
                                      hash_cache=hash_cache, sha_of=sha_of)
                except OSError as ex:
                    result = {"category": "unknown", "error": type(ex).__name__}
                if result.get("acoustid") == "not_cached":
                    stats["acoustid_cache_misses"] += 1
                elif cached_ids is not None:
                    stats["acoustid_cache_hits"] += 1
                stats["files_reclassified"] += 1
                record = {"path": rel, "size": size, "mtime_ns": mtime_ns, **result}
            category = record["category"]
            counts[category] += 1
            by_top[rel.split("/")[0]][category] += 1
            if len(samples[category]) < sample_size:
                samples[category].append(rel)
            fh.write(json.dumps(record) + "\n")
            if progress and index % checkpoint_every == 0:
                stats["checkpoints"] += 1
                progress({"checkpoint": {"processed": index, "total": len(untracked)},
                          "processed": index, "total": len(untracked), "counts": dict(counts)})
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
        "tracked_fingerprint": fingerprint,
        "stats": {
            "incremental": bool(previous),
            "library_unchanged_since_last_run": library_unchanged,
            "runtime_seconds": round(time.time() - started, 1),
            "walk_seconds": round(walk_seconds, 1),
            "files_restatted": restatted,
            "records_reused": stats["records_reused"],
            "files_reclassified": stats["files_reclassified"],
            "files_rehashed": stats["files_rehashed"] + len(hash_cache),
            "hashes_reused": stats["hashes_reused"],
            "acoustid_cache_hits": stats["acoustid_cache_hits"],
            "acoustid_cache_misses": stats["acoustid_cache_misses"],
            "acoustid_api_calls": 0,
            "musicbrainz_calls": 0,
            "checkpoints": stats["checkpoints"],
            "peak_memory_mb": _peak_memory_mb(),
        },
        "evidence_file": str(evidence_path),
        "mutations_performed": 0,
    }
    (out_dir / SUMMARY_FILE).write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary
