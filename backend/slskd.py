import errno
import json
import os
import re
import shutil
import stat
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

#: ARCH-030 B1: sidecar written into a staged recovery folder recording each
#: staged file's ORIGINAL pre-rename download basename, before ``_target_dest``
#: renamed it to ``{track:02d} {title}``. Dot-prefixed so it is ignored by the
#: audio-extension scans and by Beets import; keys are POSIX paths relative to
#: the staged folder, values the original basenames.
RECOVERY_ORIGINAL_NAMES_SIDECAR = ".bwm_recovery_original_names.json"


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


def _unlink_proven_under(root: Path, path: Path, remote) -> bool:
    """Delete ``path`` (lexically under ``root``) relative to directory file
    descriptors: every folder below ``root`` is opened O_NOFOLLOW and the
    file itself is checked with lstat, so a folder or file swapped for a
    symlink after the containment check cannot redirect the delete (#263,
    #281). The transfer proof (regular file, queued size, mtime) is checked
    again on the entry actually deleted. Returns False, deleting nothing,
    when the platform lacks dir_fd support."""
    try:
        parts = Path(path).relative_to(root).parts
    except ValueError:
        return False
    if not parts or any(p in ("", ".", "..") for p in parts):
        return False
    if not (hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")
            and os.unlink in os.supports_dir_fd and os.stat in os.supports_dir_fd):
        return False
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        st = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        size = getattr(remote, "size", 0)
        queued_at = getattr(remote, "queued_at", 0.0)
        if not stat.S_ISREG(st.st_mode) or size <= 0 or st.st_size != size or st.st_mtime < queued_at:
            return False
        os.unlink(parts[-1], dir_fd=fd)
        return True
    finally:
        os.close(fd)


def cleanup_failed_candidate_files(downloads_root: Path, username: str,
                                   remote_files: Sequence,
                                   audio_exts: Iterable[str],
                                   log: list,
                                   allowed_roots: Iterable) -> int:
    """Remove only queued audio files from a failed SLSKD candidate.

    Never searches by base name (#277). Candidate paths per queued file:
    <root>/<remote folder>/<file> (slskd's default layout),
    <root>/<peer>/<remote path> and <root>/<peer>/<remote folder>/<file>
    (peer-folder layouts), and slskd's rename-on-collision copies
    <stem>_<ticks><ext> in those folders. Every one is removed only when
    ``_proven_from_transfer`` (queued size, mtime at or after the queue
    time): the peer name is peer-chosen, so <root>/<peer> can be another
    download's album folder (peer "CD1", remote "01.flac"). A same-named
    file without that proof is left and logged.

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

    targets: Dict[Path, Any] = {}
    left: List[Path] = []
    for remote in remote_files or []:
        rp = _remote_path(remote)
        if not rp.name or rp.suffix.lower() not in audio_ext_set:
            continue
        candidates = [root / rp.parent.name, peer_root / rp.parent]
        # #281: the flattened peer path <peer>/<remote folder name> is only a
        # fallback. When the file exists at the full peer path, slskd used
        # that layout, and the flattened folder can be another album of this
        # peer; the transfer proof still guards the fallback.
        if not os.path.lexists(peer_root / rp.parent / rp.name):
            candidates.append(peer_root / rp.parent.name)
        for d in dict.fromkeys(candidates):
            if not within_roots(d, allowed):
                continue
            exact = d / rp.name
            if exact.is_file():
                if _proven_from_transfer(exact, remote):
                    targets.setdefault(exact, remote)
                else:
                    left.append(exact)
            for copy in _renamed_copies(d, rp.name, getattr(remote, "queued_at", 0.0)):
                if _proven_from_transfer(copy, remote):
                    targets.setdefault(copy, remote)

    removed = 0
    touched_dirs: set[Path] = set()
    for path, remote in targets.items():
        try:
            if not within_roots(path, allowed):
                continue
            if _unlink_proven_under(root, path, remote):
                removed += 1
                touched_dirs.add(path.parent)
        except Exception:
            pass
    for path in dict.fromkeys(p for p in left if p not in targets):
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


def _staging_fd_supported() -> bool:
    """Whether this host can do the fd-relative, symlink-safe staging copy
    (SEC-007 / #325). Staging fails closed where it cannot -- never a
    path-based fallback -- matching the rest of the barrier
    (``composite_workflows._require_dir_fd_support``, ``_unlink_proven_under``)."""
    return (hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")
            and os.open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd
            and os.stat in os.supports_dir_fd and os.rename in os.supports_dir_fd
            and os.unlink in os.supports_dir_fd)


def _staged_copy_nofollow(downloads_root: Path, stage: Path,
                          rel_parts: Sequence[str], src: Path,
                          overwrite: bool) -> None:
    """Copy ``src`` into ``stage``/``rel_parts`` fd-relative from the trusted
    ``downloads_root`` (SEC-007 / #325), mirroring the barrier discipline in
    ``composite_workflows`` (``_staging_dir_fd`` / ``_copy_file_across_fs``).

    EVERY directory below ``downloads_root`` -- the intermediate
    ``_beets_missing_import`` staging dir, the ``<prefix>-album`` folder AND each
    ``CD NN`` disc subdir -- is opened, or created, one component at a time with
    O_DIRECTORY|O_NOFOLLOW, so a component pre-planted as a symlink is refused,
    never followed out of the staged folder. O_NOFOLLOW only guards the FINAL
    component of a path, so opening the full ``stage`` path in a single
    ``os.open`` would FOLLOW a symlinked ``_beets_missing_import`` intermediate
    (F1/C5); descending fd-relative from the root closes that gap. The bytes are
    written to an O_CREAT|O_EXCL|O_NOFOLLOW temp and then atomically ``rename``d
    onto the final name: the rename replaces a dest planted on a deterministic
    REUSE -- symlink or file -- WITHOUT following it, so downloaded bytes can
    never land on the symlink's target (config, Beets DB, media). ``stage`` must
    be ``downloads_root`` joined with one or more fixed staging components."""
    parts = [p for p in rel_parts]
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise RuntimeError("refusing to stage a file with an unsafe relative path")
    try:
        stage_parts = list(Path(stage).relative_to(Path(downloads_root)).parts)
    except ValueError as exc:
        raise RuntimeError("staged import folder is not under the downloads root") from exc
    if not stage_parts or any(p in ("", ".", "..") for p in stage_parts):
        raise RuntimeError("refusing to stage through an unsafe staging path component")
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    # Open the trusted downloads root WITHOUT O_NOFOLLOW (it is the configured
    # mount, mirroring ``_unlink_proven_under``); every component beneath it,
    # intermediate staging dirs included, is descended with O_NOFOLLOW below.
    fds = [os.open(str(downloads_root), os.O_RDONLY | os.O_DIRECTORY)]
    try:
        for name in stage_parts + parts[:-1]:
            try:
                fd = os.open(name, dir_flags, dir_fd=fds[-1])
            except FileNotFoundError:
                try:
                    os.mkdir(name, dir_fd=fds[-1])
                except FileExistsError:  # a concurrent retry made it first
                    pass
                fd = os.open(name, dir_flags, dir_fd=fds[-1])
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise RuntimeError(
                        "refusing to stage through a symlinked folder component") from exc
                raise
            fds.append(fd)
        parent_fd = fds[-1]
        leaf = parts[-1]
        if not overwrite:
            # Fresh (random-prefix) passes never share a folder, but keep the old
            # de-dupe so a re-run into an existing folder does not clobber a
            # distinct file: pick a free name by fd-relative lstat.
            base, ext = os.path.splitext(leaf)
            n = 1
            while True:
                try:
                    os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    break
                leaf = f"{base}.{n}{ext}"
                n += 1
        tmp = f".{uuid.uuid4().hex[:12]}.part"
        dfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                      0o600, dir_fd=parent_fd)
        try:
            try:
                with open(src, "rb") as fin, open(dfd, "wb", closefd=False) as fout:
                    shutil.copyfileobj(fin, fout, 1 << 20)
                try:  # best-effort copy2-style metadata preservation
                    sst = os.stat(src)
                    os.fchmod(dfd, stat.S_IMODE(sst.st_mode))
                    os.utime(dfd, ns=(sst.st_atime_ns, sst.st_mtime_ns))
                except OSError:
                    pass
                os.fsync(dfd)
            finally:
                os.close(dfd)
            # rename operates on the name itself: it REPLACES a pre-planted
            # symlink at ``leaf`` with our regular file rather than following it.
            os.rename(tmp, leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=parent_fd)
            except OSError:
                pass
            raise
    finally:
        for fd in reversed(fds):
            os.close(fd)


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
    # SEC-007 / #325: a caller-supplied ``stage_prefix`` (the DETERMINISTIC
    # recovery-identity prefix that lets retries REUSE one staged folder instead
    # of minting a fresh uuid family each attempt) is sanitised to a bounded
    # [0-9A-Za-z._-] token, so it can never carry a path separator, a drive
    # segment or a ``..`` component into the staged path -- it is a fixed-length
    # hex-of-a-hash, never raw identity text. An empty/degenerate prefix falls
    # back to the previous random token.
    reuse = stage_prefix is not None
    if reuse:
        prefix = re.sub(r"[^0-9A-Za-z._-]", "", str(stage_prefix)).strip("._-")[:40]
    else:
        prefix = ""
    if not prefix:
        prefix = uuid.uuid4().hex[:10]
    stage = Path(downloads_root) / "_beets_missing_import" / f"{prefix}-{safe_album}"
    stage.mkdir(parents=True, exist_ok=True)
    # SEC-007 / #325 containment: the staged-folder name is now PREDICTABLE when
    # a deterministic prefix is used, so verify the real path is a non-symlink
    # directory that still resolves under the downloads root before copying into
    # it. This resolve()-based check is a SEPARATE syscall from the copy, so it
    # does NOT by itself close the TOCTOU: the intermediate
    # ``_beets_missing_import`` dir, the ``<prefix>-album`` folder, each dest
    # file and every ``CD NN`` disc subdir could still be swapped for a symlink
    # (by a local writer to this app-private dir, or via a race between recovery
    # retries). The per-file copy loop below (``_staged_copy_nofollow``) is what
    # closes that: it descends fd-relative from the downloads root -- intermediate
    # staging components included -- with O_NOFOLLOW and writes each dest with
    # O_CREAT|O_EXCL then atomic rename. Enforced for BOTH callers (defence in
    # depth); never trusts the name alone.
    root_resolved = Path(downloads_root).resolve(strict=False)
    stage_resolved = stage.resolve(strict=False)
    if (stage.is_symlink() or not stage_resolved.is_dir()
            or not stage_resolved.is_relative_to(root_resolved)):
        raise RuntimeError("staged import folder is not safely contained in the downloads root")
    # Fail closed where the host cannot do the fd-relative symlink-safe copy,
    # rather than fall back to a symlink-following path open (SEC-007 / #325).
    if not _staging_fd_supported():
        raise RuntimeError(
            "refusing to stage recovery files: fd-relative, symlink-safe copy is "
            "unavailable on this host (needs O_NOFOLLOW and os.supports_dir_fd)")

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
    # ARCH-030 B1: map each staged file's path (relative to ``stage``) to the
    # ORIGINAL pre-rename download basename, so the slot gate can corroborate the
    # target against the genuine downloaded name instead of the ``{track:02d}
    # {title}`` name this loop manufactures from the REQUESTED track.
    original_names: Dict[str, str] = {}
    # ARCH-030 F2: when the SOURCE is itself an already app-staged/renamed folder
    # (a restage of the still-missing subset), its files no longer carry their
    # genuine download basename -- ``src.name`` is the laundered ``{track:02d}
    # {title}`` staged name. Recording that as the "original" would launder the
    # real original away and collapse Option-C's two independent sources to
    # embedded-only. Carry the GENUINE original forward from the source folder's
    # sidecar instead (same relocation discipline as _separate_deferred_slot_files).
    prior_original_names = original_download_names_for_folder(source)
    for idx, src in enumerate(selected):
        if not src.is_file():
            continue
        target = targets[idx] if idx < len(targets) else None
        if target:
            rel = _target_dest(src, target)
        else:
            try:
                rel = src.resolve(strict=False).relative_to(source.resolve(strict=False))
            except Exception:
                rel = Path(src.name)
        # Copy fd-relative from the validated ``stage`` dir so no parent
        # component (e.g. a planted ``CD NN`` symlink) and no pre-existing dest
        # is followed out of the staged folder (SEC-007 / #325). On a
        # deterministic REUSE across retries the same track maps to the same
        # name and must REPLACE the prior copy (``overwrite``) rather than
        # accumulate ``name.1``/``name.2`` siblings that would collide at import;
        # a fresh random-prefix pass keeps the de-dupe suffixing.
        _staged_copy_nofollow(downloads_root, stage, rel.parts, src, overwrite=reuse)
        original_names[rel.as_posix()] = (
            prior_original_names.get(str(src))
            or prior_original_names.get(str(src.resolve(strict=False)))
            or src.name)
        copied += 1

    if copied:
        _write_original_names_sidecar(stage, original_names)
        log.append(f"  [import] Staged {copied} selected audio file(s) at {stage}")
        return str(stage)
    return aldir


def _read_text_nofollow(path: Path) -> str:
    """Read a file's text refusing to follow a symlink -- at the FINAL component
    AND at its immediate parent dir (SEC-007/#325). The staged folder is
    app-private, but a symlink pre-planted at the sidecar, or a swap of the
    sidecar's parent dir to a symlink, must never redirect this read to an
    arbitrary file. ``O_NOFOLLOW`` alone guards only a path's final component, so
    the parent is opened fd-relative with ``O_DIRECTORY | O_NOFOLLOW`` and the
    leaf opened under it, mirroring the sibling ``_staged_copy_nofollow``. Falls
    back to a plain read only where dir_fd / ``O_NOFOLLOW`` are unavailable
    (non-POSIX hosts, where staging never actually runs)."""
    path = Path(path)
    if not _staging_fd_supported():
        return path.read_text(encoding="utf-8")
    dir_fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    with os.fdopen(fd, "r", encoding="utf-8") as fh:
        return fh.read()


def _write_text_nofollow(path: Path, text: str) -> None:
    """Write text without following a symlink at the sidecar OR at its immediate
    parent dir, and without truncating a planted hardlink (SEC-007/#325). The
    bytes go to an ``O_CREAT | O_EXCL | O_NOFOLLOW`` temp opened fd-relative under
    the parent dir (itself opened ``O_DIRECTORY | O_NOFOLLOW``) and are atomically
    renamed onto the sidecar name: the rename REPLACES any pre-planted symlink or
    hardlink at the leaf without following or truncating it -- so no ``O_TRUNC``
    ever touches the real name. Plain-write fallback only where dir_fd /
    ``O_NOFOLLOW`` are unavailable (non-POSIX hosts, where staging never runs)."""
    path = Path(path)
    if not _staging_fd_supported():
        path.write_text(text, encoding="utf-8")
        return
    dir_fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        tmp = f".{uuid.uuid4().hex[:12]}.part"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644,
                     dir_fd=dir_fd)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.rename(tmp, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError:
                pass
            raise
    finally:
        os.close(dir_fd)


def _write_original_names_sidecar(stage: Path, original_names: Dict[str, str]) -> None:
    """ARCH-030 B1: persist ``{staged-relative-path -> original basename}`` in the
    staged folder. Merges with any prior sidecar (deterministic-prefix retries
    REUSE one folder). Best-effort: a sidecar write failure never breaks staging
    -- the gate then falls back to the on-disk name for those files. Both the
    merge-read and the write refuse to follow a symlink at the sidecar
    (SEC-007/#325) so they can never be redirected to an arbitrary file."""
    if not original_names:
        return
    sidecar = stage / RECOVERY_ORIGINAL_NAMES_SIDECAR
    merged: Dict[str, str] = {}
    try:
        prior = json.loads(_read_text_nofollow(sidecar))
        if isinstance(prior, dict):
            merged.update({str(k): _s(v) for k, v in prior.items()})
    except Exception:
        pass
    merged.update(original_names)
    try:
        _write_text_nofollow(sidecar, json.dumps(merged, ensure_ascii=False))
    except Exception:
        pass


def original_download_names_for_folder(folder: Any) -> Dict[str, str]:
    """ARCH-030 B1 producer read-side: ``{staged_abs_path -> original pre-rename
    download basename}`` for a staged recovery folder, from the sidecar
    ``stage_selected_audio_files`` wrote at rename time.

    One of Option C's two independent corroboration sources (the other being the
    file's embedded tags, see ``acoustid_service.embedded_identity_for_file``).
    Returns ``{}`` when no sidecar is present -- e.g. a non-slskd path that never
    renamed the files -- in which case the on-disk name already IS the original
    and the gate may read it directly."""
    try:
        base = Path(folder)
        raw = json.loads(_read_text_nofollow(base / RECOVERY_ORIGINAL_NAMES_SIDECAR))
    except Exception:
        return {}
    out: Dict[str, str] = {}
    if isinstance(raw, dict):
        for rel, orig in raw.items():
            try:
                out[str(base / Path(str(rel)))] = _s(orig)
            except Exception:
                continue
    return out
