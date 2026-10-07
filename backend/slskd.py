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


def _has_control_char(text: str) -> bool:
    return any(c < " " or c == "\x7f" for c in text)


def safe_peer_username(value) -> str:
    """The peer username when it is usable as one path segment, else "".

    slskd saves under DOWNLOADS_ROOT/<username>/..., and the username comes
    from the peer: "/", "..", "a/../../srv" must never become a path, "C:"
    must not make a Windows drive-relative join, and control characters
    must not forge job-log lines (#248).
    """
    name = _s(value)
    if not name or name in (".", "..") or any(c in name for c in "/\\:") or _has_control_char(name):
        return ""
    return name


_DRIVE_SEGMENT = re.compile(r"^[A-Za-z]:")


def _remote_path(value) -> Path:
    """A peer-supplied remote path as a relative path that cannot climb.

    Splits on both separators and drops every empty, ".", ".." or drive
    ("C:", anywhere in the path) segment and every segment holding a control
    character (#248): "..\\srv\\x\\01.flac" becomes srv/x/01.flac.
    """
    return Path(*[
        p for p in re.split(r"[\\/]", _s(value))
        if p not in ("", ".", "..") and not _DRIVE_SEGMENT.match(p) and not _has_control_char(p)
    ])


def within_roots(path, allowed_roots) -> bool:
    """True when ``path`` resolves under one of ``allowed_roots``.

    An empty allowlist allows nothing, so an unsafe DOWNLOADS_ROOT (dropped
    from DOWNLOADS_ALLOWED_ROOTS) makes every scan and delete a no-op.
    """
    try:
        resolved = Path(path).resolve(strict=False)
        return any(resolved.is_relative_to(Path(base).resolve(strict=False)) for base in allowed_roots or ())
    except Exception:
        return False


def peer_download_dir(downloads_root: Path, username: str, remote_dir) -> Path:
    """The peer-folder layout: <downloads_root>/<username>/<remote_dir>.

    This is not slskd's default. By default slskd saves completed files to
    <downloads>/<remote folder name>/<file> (Destination.Subdirectory
    "${SOURCE_DIRECTORY}"), with no username folder; the finder also
    searches that layout. Callers pass a username accepted by
    ``safe_peer_username`` and must still check the result with
    ``within_roots`` (symlinks) before using it.
    """
    return Path(downloads_root) / username / _remote_path(remote_dir)


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
                                   remote_files: Iterable,
                                   allowed_roots: Iterable) -> List[Path]:
    """Return likely local roots for queued SLSKD remote files.

    Only roots that resolve under ``allowed_roots``; none for an unsafe
    username or an empty allowlist (#248).
    """
    root = Path(downloads_root)
    roots: List[Path] = []
    username = safe_peer_username(username)
    if not username:
        return roots

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
    allowed = tuple(allowed_roots or ())
    return [path for path in roots if within_roots(path, allowed)]


class QueuedRemote(str):
    """A queued remote filename that carries the evidence cleanup needs (#277).

    ``size`` is the byte size queued with slskd and ``queued_at`` the epoch
    time just before queueing. It is a ``str`` so every caller that treats
    queued files as names keeps working, and list copies keep the evidence.
    """

    size: int = 0
    queued_at: float = 0.0

    def __new__(cls, name, size=0, queued_at=0.0):
        obj = super().__new__(cls, _s(name))
        obj.size = file_size({"size": size})
        obj.queued_at = float(queued_at or 0)
        return obj


# .NET DateTime ticks (100 ns since 0001-01-01) at the Unix epoch.
_DOTNET_UNIX_EPOCH_TICKS = 621355968000000000


def _proven_from_transfer(path: Path, remote) -> bool:
    """True when ``path`` has the queued size and was written at or after
    the queue time. Missing evidence is never proof."""
    size = getattr(remote, "size", 0)
    queued_at = getattr(remote, "queued_at", 0.0)
    if size <= 0 or queued_at <= 0:
        return False
    try:
        st = path.stat()
    except OSError:
        return False
    return st.st_size == size and st.st_mtime >= queued_at


def _renamed_copies(directory: Path, name: str, queued_at: float) -> List[Path]:
    """Files slskd renamed on collision for ``name`` after ``queued_at``.

    With Destination.Exists "rename" (the default) slskd's FileService.MoveFile
    saves <stem>_<DateTime.UtcNow.Ticks><ext> when <stem><ext> exists.
    """
    if queued_at <= 0:
        return []
    stem, ext = Path(name).stem, Path(name).suffix
    pattern = re.compile(re.escape(stem) + r"_(\d{1,20})" + re.escape(ext))
    min_ticks = int(queued_at * 10_000_000) + _DOTNET_UNIX_EPOCH_TICKS
    try:
        entries = list(directory.iterdir()) if directory.is_dir() else []
    except OSError:
        return []
    out = []
    for entry in entries:
        m = pattern.fullmatch(entry.name)
        if m and int(m.group(1)) >= min_ticks:
            out.append(entry)
    return out


def cleanup_failed_candidate_files(downloads_root: Path, username: str,
                                   remote_files: Sequence,
                                   audio_exts: Iterable[str],
                                   log: list,
                                   allowed_roots: Iterable) -> int:
    """Remove only queued audio files from a failed SLSKD candidate.

    Never searches by base name (#277). Candidate paths per queued file:

    - <root>/<peer>/<remote path> and <root>/<peer>/<remote folder>/<file>
      (peer-folder layouts): removed.
    - <root>/<remote folder>/<file> (slskd's default layout) and slskd's
      rename-on-collision copies <stem>_<ticks><ext> in any of these
      folders: removed only when ``_proven_from_transfer`` (queued size and
      mtime at or after the queue time). A same-named file without that
      proof is left and logged.

    Never outside ``allowed_roots`` (#248).
    """
    audio_ext_set = {str(ext).lower() for ext in audio_exts}
    root = Path(downloads_root)
    username = safe_peer_username(username)
    if not username:
        return 0
    allowed = tuple(allowed_roots or ())
    peer_root = root / username

    def rel(path: Path) -> str:
        try:
            return str(path.relative_to(root))
        except ValueError:
            return path.name

    targets: List[Path] = []
    left: List[Path] = []
    for remote in remote_files or []:
        rp = _remote_path(remote)
        if not rp.name or rp.suffix.lower() not in audio_ext_set:
            continue
        peer_dirs = (peer_root / rp.parent, peer_root / rp.parent.name)
        flat_dir = root / rp.parent.name
        for d in peer_dirs:
            targets.append(d / rp.name)
        flat = flat_dir / rp.name
        if flat not in targets and within_roots(flat, allowed) and flat.is_file():
            if _proven_from_transfer(flat, remote):
                targets.append(flat)
            else:
                left.append(flat)
        for d in dict.fromkeys((*peer_dirs, flat_dir)):
            if not within_roots(d, allowed):
                continue
            for copy in _renamed_copies(d, rp.name, getattr(remote, "queued_at", 0.0)):
                if _proven_from_transfer(copy, remote):
                    targets.append(copy)

    removed = 0
    touched_dirs: set[Path] = set()
    for path in dict.fromkeys(targets):
        try:
            if not within_roots(path, allowed) or not path.is_file():
                continue
            path.unlink(missing_ok=True)
            removed += 1
            touched_dirs.add(path.parent)
        except Exception:
            pass
    for path in dict.fromkeys(left):
        log.append(f"  [slskd] Left {rel(path)!r}: no proof it belongs to the failed candidate.")

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
