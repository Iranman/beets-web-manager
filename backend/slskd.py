import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


def _s(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _remote_path(value) -> Path:
    raw = _s(value).replace("\\", "/")
    if len(raw) > 2 and raw[1] == ":":
        raw = raw[2:]
    return Path(raw.lstrip("/"))


def compact_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _s(value).lower())


def file_remote_name(file_info: Dict[str, Any],
                     response: Optional[Dict[str, Any]] = None) -> str:
    """Return the exact SLSKD remote filename to queue."""
    name = _s(
        file_info.get("filename")
        or file_info.get("fileName")
        or file_info.get("name")
        or ""
    ).strip()
    resp = response or {}
    directory = _s(
        file_info.get("directory")
        or file_info.get("folder")
        or resp.get("directory")
        or resp.get("folder")
        or resp.get("path")
        or ""
    ).strip()
    if directory and name and "/" not in name and "\\" not in name:
        sep = "\\" if "\\" in directory else "/"
        return directory.rstrip("\\/") + sep + name
    return name


def file_size(file_info: Dict[str, Any]) -> int:
    try:
        return int(file_info.get("size") or file_info.get("length") or 0)
    except Exception:
        return 0


def dir_for_file(file_info: Dict[str, Any],
                 response: Optional[Dict[str, Any]] = None) -> str:
    path = file_remote_name(file_info, response).replace("\\", "/")
    return path.rsplit("/", 1)[0] if "/" in path else ""


def is_audio_file(file_info: Dict[str, Any], audio_exts: Iterable[str],
                  response: Optional[Dict[str, Any]] = None) -> bool:
    return Path(file_remote_name(file_info, response)).suffix.lower() in {
        str(ext).lower() for ext in audio_exts
    }


def candidate_key(username: str, remote_dir: str) -> tuple[str, str]:
    return (_s(username).strip().lower(), _s(remote_dir).replace("\\", "/").strip().lower())


def candidate_score(response: Dict[str, Any], remote_dir: str,
                    audio_files: Sequence[Dict[str, Any]],
                    artist: str, album: str, year: str,
                    track_count: int, audio_exts: Iterable[str]) -> int:
    count = len(audio_files)
    album_norm = compact_key(album)
    artist_norm = compact_key(artist)
    dir_norm = compact_key(remote_dir)
    dir_path = _s(remote_dir).replace("\\", "/").lower()
    score = 0

    if track_count:
        min_files = max(1, min(track_count, int(track_count * 0.70)))
        if count == track_count:
            score += 10000
        elif count > track_count:
            score += max(8000, 9400 - min(count - track_count, 20) * 70)
        elif count >= min_files:
            score += 6000 + count * 40
        else:
            score += count * 100
    if album_norm and album_norm in dir_norm:
        score += 20
    if artist_norm and artist_norm in dir_norm:
        score += 10
    if year and year in remote_dir:
        score += 5
    if all(is_audio_file(file_info, audio_exts, response) for file_info in audio_files):
        score += 8
    if response.get("hasFreeUploadSlot"):
        score += 5
    score += min(count, 20)
    if re.search(r"(^|/)\[(?:include|exclude)\]($|/)", dir_path):
        score -= 20000
    return score


def build_album_candidates(responses: Sequence[Dict[str, Any]],
                           artist: str, album: str, year: str,
                           track_count: int, audio_exts: Iterable[str],
                           skip_candidates: Optional[set] = None) -> tuple[List[Dict[str, Any]], int]:
    """Build and score album-like SLSKD candidates from response rows."""
    candidates: List[Dict[str, Any]] = []
    for response in responses or []:
        files = response.get("files", []) or []
        audio_files = [f for f in files if is_audio_file(f, audio_exts, response)]
        if not audio_files:
            continue
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for file_info in audio_files:
            grouped.setdefault(dir_for_file(file_info, response), []).append(file_info)
        for remote_dir, dir_files in grouped.items():
            candidates.append({
                "score": candidate_score(
                    response, remote_dir, dir_files, artist, album, year, track_count, audio_exts
                ),
                "dir": remote_dir,
                "files": sorted(dir_files, key=lambda f: file_remote_name(f, response).lower()),
                "resp": response,
                "username": _s(response.get("username", "")),
            })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    skipped = 0
    if skip_candidates:
        before = len(candidates)
        candidates = [
            candidate for candidate in candidates
            if candidate_key(candidate.get("username", ""), candidate.get("dir", "")) not in skip_candidates
        ]
        skipped = before - len(candidates)
    return candidates, skipped


def slskd_download_candidate_roots(downloads_root: Path, username: str,
                                   remote_files: Iterable) -> List[Path]:
    """Return likely local roots for queued SLSKD remote files."""
    root = Path(downloads_root)
    roots: List[Path] = []

    def add(raw) -> None:
        if not raw:
            return
        try:
            path = Path(str(raw))
        except Exception:
            return
        if path not in roots:
            roots.append(path)

    for remote in remote_files or []:
        parent = _remote_path(remote).parent
        if str(parent) in ("", "."):
            continue
        add(root / username / parent)
        add(root / username / parent.name)
        add(root / parent)
        add(root / parent.name)
    return roots


def cleanup_failed_candidate_files(downloads_root: Path, username: str,
                                   remote_files: Sequence,
                                   audio_exts: Iterable[str],
                                   log: list) -> int:
    """Remove only queued audio files from a failed SLSKD candidate."""
    audio_ext_set = {str(ext).lower() for ext in audio_exts}
    queued_names = {
        _remote_path(name).name.lower()
        for name in remote_files or []
        if _s(name).strip()
    }
    if not queued_names:
        return 0

    root = Path(downloads_root)
    removed = 0
    touched_dirs: set[Path] = set()
    for candidate_root in slskd_download_candidate_roots(root, username, remote_files):
        try:
            if candidate_root.is_file():
                files = [candidate_root]
            elif candidate_root.is_dir():
                files = [p for p in candidate_root.rglob("*") if p.is_file()]
            else:
                continue
        except Exception:
            continue

        for path in files:
            if path.name.lower() not in queued_names:
                continue
            if path.suffix.lower() not in audio_ext_set:
                continue
            try:
                path.unlink(missing_ok=True)
                removed += 1
                touched_dirs.add(path.parent)
            except Exception:
                pass

    for start in sorted(touched_dirs, key=lambda p: len(str(p)), reverse=True):
        cur = start
        while cur != root and root in cur.parents:
            try:
                cur.rmdir()
            except Exception:
                break
            cur = cur.parent

    if removed:
        log.append(f"  [slskd] Removed {removed} partial file(s) from failed candidate.")
    return removed


def stage_selected_audio_files(downloads_root: Path, audio_exts: Iterable[str],
                               aldir: str, audio_files: Sequence[Path],
                               artist: str, album: str, log: list,
                               stage_prefix: str | None = None,
                               force_stage: bool = False,
                               target_tracks: Optional[Sequence[Dict[str, Any]]] = None) -> str:
    """Copy a selected subset to a clean import folder when the source has extras."""
    selected = [Path(p) for p in audio_files]
    if not selected:
        return aldir
    targets = list(target_tracks or [])

    audio_ext_set = {str(ext).lower() for ext in audio_exts}
    source = Path(aldir)
    try:
        all_audio = sorted(
            [p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in audio_ext_set],
            key=lambda p: p.name.lower(),
        ) if source.is_dir() else []
        selected_resolved = {p.resolve(strict=False) for p in selected}
        all_resolved = {p.resolve(strict=False) for p in all_audio}
        if not force_stage and not targets and all_audio and all_resolved == selected_resolved:
            return aldir
    except Exception:
        pass

    safe_album = re.sub(r'[\\/:*?"<>|]', "_", f"{artist} - {album}").strip(" _-") or "missing-tracks"
    prefix = stage_prefix if stage_prefix is not None else uuid.uuid4().hex[:10]
    stage = Path(downloads_root) / "_beets_missing_import" / f"{prefix}-{safe_album}"
    stage.mkdir(parents=True, exist_ok=True)

    def _target_dest(src: Path, target: Dict[str, Any]) -> Path:
        try:
            disc = int(target.get("disc") or 1)
        except Exception:
            disc = 1
        try:
            track = int(target.get("track") or 0)
        except Exception:
            track = 0
        title = _s(target.get("title", "")).strip() or src.stem
        safe_title = re.sub(r'[\\/:*?"<>|]', "_", title).strip(" ._-") or src.stem
        name = f"{track:02d} {safe_title}{src.suffix}" if track else f"{safe_title}{src.suffix}"
        return (Path(f"CD {disc:02d}") / name) if disc > 1 else Path(name)

    copied = 0
    for idx, src in enumerate(selected):
        if not src.is_file():
            continue
        target = targets[idx] if idx < len(targets) else None
        if target:
            dest = stage / _target_dest(src, target)
        else:
            try:
                rel = src.resolve(strict=False).relative_to(source.resolve(strict=False))
                dest = stage / rel
            except Exception:
                dest = stage / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            base, ext = dest.stem, dest.suffix
            n = 1
            while dest.exists():
                dest = dest.parent / f"{base}.{n}{ext}"
                n += 1
        shutil.copy2(src, dest)
        copied += 1

    if copied:
        log.append(f"  [import] Staged {copied} selected audio file(s) at {stage}")
        return str(stage)
    return aldir
