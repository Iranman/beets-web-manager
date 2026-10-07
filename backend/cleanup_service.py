"""Album/folder cleanup plans, reports and release-group resolutions (ARCH-001).
"""

from __future__ import annotations

import hashlib, json, os, re, shutil, sqlite3, time, traceback, unicodedata, uuid
from backend.matching import track_filename_has_source_id_suffix as _canonical_track_filename_has_source_id_suffix
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from backend.app_runtime import _app_logger, ALBUM_FOLDER_CLEANUP_LAST_FILE, AUDIO_EXT, DOWNLOADS_ALLOWED_ROOTS, DOWNLOADS_ROOT, METADATA_CACHE_ROOT, MUSIC_ROOT, RGID_RESOLUTION_STATE_FILE, _LITERAL_PLACEHOLDER_RE, _MB_UUID_RE, _UNRESOLVED_TEMPLATE_TOKEN_RE, _s
from backend.app_runtime import _redact_security_text
from backend.app_runtime import _normalize_name, _path_has_symlink_component_under, _path_is_under, _path_lexically_under, _path_under, _safe_path_component, _same_resolved_path
from backend.beets_adapter import BeetsError, BeetsUnavailableError
from backend.resource_locks import ResourceLockConflictError
import backend.composite_workflows as composite_workflows
from backend.acoustid_service import AUDIO_EXTS
from backend.artwork_service import _ART_EXTS
from backend.slskd_service import _strip_track_filename_id_suffix
from backend.auth_service import _INITIAL_BROWSER_PASSWORD_FILE, _PERSISTED_BROWSER_PASSWORD_FILE, _browser_password_is_usable, _first_config_secret
from backend.matching_service import _invalidate_lib_cache
from backend.app_runtime import jobs
from backend.musicbrainz_service import _album_cleanup_embedded_musicbrainz_tags, _album_cleanup_valid_rgid, _clean_malformed_release_group_stamps, _folder_cleanup_known_release_group_id, _folder_cleanup_release_group_from_name
from backend.serializers import _import_review_path_text_error
from backend.plex_service import _trigger_plex_refresh

# ── ARCH-001 extracted code ──


def _track_filename_has_source_id_suffix(value: Any) -> bool:
    try:
        return _canonical_track_filename_has_source_id_suffix(value)
    except NameError:
        from backend.matching import track_filename_has_source_id_suffix as _fallback_has_suffix
        return _fallback_has_suffix(value)


def _unique_dest(path: Path) -> Path:
    if not path.exists():
        return path
    base = path.with_suffix("")
    suffix = path.suffix
    n = 1
    while True:
        cand = Path(f"{base}.{n}{suffix}")
        if not cand.exists():
            return cand
        n += 1


def _album_item_abs_paths(album_id: int) -> List[Path]:
    try:
        aid = int(album_id or 0)
    except Exception:
        aid = 0
    if aid <= 0:
        return []
    paths: List[Path] = []
    try:
        items = composite_workflows.find_all_items_by_album_id(aid)
        for item in items:
            raw = _s(item.get("path")).strip()
            if not raw:
                continue
            p = Path(raw)
            if not p.is_absolute():
                p = MUSIC_ROOT / raw
            paths.append(p)
    except Exception:
        return []
    return paths


def _clean_template_token_stem(stem: str) -> str:
    cleaned = _UNRESOLVED_TEMPLATE_TOKEN_RE.sub("", _s(stem))
    cleaned = re.sub(r"(\s*-\s*){2,}", " - ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" -_")


def _track_filename_row_target(row: sqlite3.Row, current_path: Path) -> Optional[Path]:
    title = _safe_path_component(_strip_track_filename_id_suffix(row["title"]), "")
    if not title:
        return None
    album = _safe_path_component(row["album"], "Unknown Album")
    artist = _safe_path_component(row["albumartist"] or row["artist"], "Unknown Artist")
    try:
        track = int(row["track"] or 0)
    except Exception:
        track = 0
    track_part = f"{track:02d}" if track > 0 else "00"
    target_parent = Path(_clean_malformed_release_group_stamps(str(current_path.parent)))
    target_name = f"{artist} - {album} - {track_part} - {title}{current_path.suffix}"
    return target_parent / target_name


def _template_token_row_target(row: Any, current_path: Path) -> Optional[Path]:
    target = _track_filename_row_target(row, current_path)
    return _unique_dest(target) if target else None


def _resolve_existing_template_token_file(path: Path) -> Path:
    if path.exists():
        return path
    try:
        parent = path.parent
        if not parent.exists() or not parent.is_dir():
            return path
        matches = [
            p for p in parent.iterdir()
            if p.is_file()
            and p.suffix.lower() == path.suffix.lower()
            and _UNRESOLVED_TEMPLATE_TOKEN_RE.search(p.stem)
        ]
        if len(matches) == 1:
            return matches[0]
    except Exception:
        return path
    return path


def _cleanup_filename_conflict(source: Path, target: Path) -> bool:
    try:
        return target.exists() and not _same_resolved_path(source, target)
    except Exception:
        return False


def _album_template_token_cleanup_candidates(album_id: int) -> List[Dict[str, Any]]:
    try:
        aid = int(album_id or 0)
    except Exception:
        aid = 0
    if aid <= 0:
        return []
    candidates: List[Dict[str, Any]] = []
    seen: set[str] = set()
    try:
        rows = composite_workflows.find_all_items_by_album_id(aid)
    except Exception:
        return []
    for row in rows:
        raw_path = _s(row["path"]).strip()
        if not raw_path:
            continue
        current = Path(raw_path)
        if not current.is_absolute():
            current = MUSIC_ROOT / raw_path
        try:
            if not _path_under(current, MUSIC_ROOT):
                continue
        except Exception:
            continue
        has_template_token = bool(
            _UNRESOLVED_TEMPLATE_TOKEN_RE.search(raw_path)
            or _UNRESOLVED_TEMPLATE_TOKEN_RE.search(current.name)
        )
        has_malformed_stamp = _clean_malformed_release_group_stamps(str(current)) != str(current)
        has_source_id = bool(
            _track_filename_has_source_id_suffix(Path(raw_path).stem)
            or _track_filename_has_source_id_suffix(current.stem)
        )
        if not (has_template_token or has_malformed_stamp or has_source_id):
            continue
        key = str(current.resolve(strict=False))
        if key in seen:
            continue
        seen.add(key)
        actual_current = _resolve_existing_template_token_file(current)
        has_source_id = has_source_id or _track_filename_has_source_id_suffix(actual_current.stem)
        target = (
            _track_filename_row_target(row, actual_current)
            if has_source_id
            else _template_token_row_target(row, actual_current)
        )
        if not target or _same_resolved_path(target, actual_current):
            continue
        conflict = _cleanup_filename_conflict(actual_current, target)
        issue_type = "Lidarr ID in track filename" if has_source_id else "Unresolved path-template token"
        candidates.append({
            "item_id": int(row["id"] or 0),
            "path": str(actual_current),
            "db_path": str(current),
            "new_path": str(target),
            "filename": actual_current.name,
            "new_filename": target.name,
            "issue_type": issue_type,
            "action": "Fix filename" if has_source_id else "Fix template-token filename",
            "proposed_repair": "Rename to clean MusicBrainz title" if has_source_id else "Remove unresolved template token",
            "conflict": conflict,
            "duplicate_check_required": conflict,
        })
    return candidates


def _template_token_cleanup_candidates(paths: Iterable[Path],
                                       *,
                                       recursive: bool) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    candidates: List[Dict[str, Any]] = []
    for raw_path in paths:
        roots = []
        try:
            p = Path(raw_path)
            if p.is_file():
                roots = [p]
            elif p.is_dir():
                roots = list(p.rglob("*") if recursive else p.iterdir())
        except Exception:
            continue
        for f in roots:
            try:
                if not f.is_file() or f.suffix.lower() not in AUDIO_EXT:
                    continue
                if not _path_under(f, MUSIC_ROOT):
                    continue
            except Exception:
                continue
            has_template_token = bool(_UNRESOLVED_TEMPLATE_TOKEN_RE.search(f.stem))
            has_source_id = _track_filename_has_source_id_suffix(f.stem)
            if not (has_template_token or has_source_id):
                continue
            key = str(f.resolve(strict=False))
            if key in seen:
                continue
            seen.add(key)
            cleaned_stem = _clean_template_token_stem(f.stem) if has_template_token else f.stem
            new_stem = _strip_track_filename_id_suffix(cleaned_stem) if has_source_id else cleaned_stem
            if not new_stem:
                continue
            target_base = f.parent / f"{new_stem}{f.suffix}"
            target = target_base if has_source_id else _unique_dest(target_base)
            if _same_resolved_path(target, f):
                continue
            conflict = _cleanup_filename_conflict(f, target)
            issue_type = "Lidarr ID in track filename" if has_source_id else "Unresolved path-template token"
            candidates.append({
                "path": str(f),
                "new_path": str(target),
                "filename": f.name,
                "new_filename": target.name,
                "issue_type": issue_type,
                "action": "Fix filename" if has_source_id else "Fix template-token filename",
                "proposed_repair": "Rename to clean MusicBrainz title" if has_source_id else "Remove unresolved template token",
                "conflict": conflict,
                "duplicate_check_required": conflict,
            })
    return candidates


def _apply_filename_cleanup_candidate(rec: Dict[str, Any], *,
                                      dry_run: bool,
                                      log: List[str]) -> Dict[str, int]:
    old_path = Path(rec["path"])
    new_path = Path(rec["new_path"])
    issue = _s(rec.get("issue_type") or "filename cleanup")
    if dry_run:
        action = "would verify duplicate" if rec.get("conflict") else "would rename"
        log.append(f"  {action}: {old_path.name!r} -> {new_path.name!r} ({issue})")
        return {"renamed": 0, "quarantined": 0, "db_updates": 0, "db_deletes": 0, "skipped": 0}
    if not old_path.exists() or not old_path.is_file():
        log.append(f"  WARN skipping missing file: {old_path.name}")
        return {"renamed": 0, "quarantined": 0, "db_updates": 0, "db_deletes": 0, "skipped": 1}
    try:
        plan_payload = {
            "mode": "filename_cleanup",
            "candidates": [{
                "item_id": int(rec.get("item_id") or 0),
                "source": str(old_path),
                "destination": str(new_path),
                "conflict": bool(rec.get("conflict")),
            }]
        }
        plan_res = composite_workflows.plan_album_maintenance(plan_payload)
        if not plan_res.get("ok"):
            log.append(f"  WARN planning filename cleanup for {old_path.name}: {plan_res.get('error')}")
            return {"renamed": 0, "quarantined": 0, "db_updates": 0, "db_deletes": 0, "skipped": 1}

        apply_res = composite_workflows.apply_album_maintenance(plan_res["operation_id"])
        if not apply_res.get("ok"):
            log.append(f"  WARN applying filename cleanup for {old_path.name}: {apply_res.get('error')}")
            return {"renamed": 0, "quarantined": 0, "db_updates": 0, "db_deletes": 0, "skipped": 1}

        label = "filename cleanup file" if issue == "Lidarr ID in track filename" else "template-token file"
        log.append(f"  Renamed {label} (engine controlled): {old_path.name!r} -> {new_path.name!r}")
        return {"renamed": 1, "quarantined": 0, "db_updates": 1, "db_deletes": 0, "skipped": 0}
    except Exception as ex:
        log.append(f"  WARN renaming {old_path.name}: {ex}")
        return {"renamed": 0, "quarantined": 0, "db_updates": 0, "db_deletes": 0, "skipped": 1}


def _cleanup_template_token_files(paths: Iterable[Path], *,
                                  recursive: bool,
                                  dry_run: bool,
                                  log: List[str]) -> Dict[str, Any]:
    candidates = _template_token_cleanup_candidates(paths, recursive=recursive)
    renamed = 0
    quarantined = 0
    db_updates = 0
    db_deletes = 0
    skipped = 0
    for rec in candidates:
        result = _apply_filename_cleanup_candidate(rec, dry_run=dry_run, log=log)
        renamed += int(result.get("renamed") or 0)
        quarantined += int(result.get("quarantined") or 0)
        db_updates += int(result.get("db_updates") or 0)
        db_deletes += int(result.get("db_deletes") or 0)
        skipped += int(result.get("skipped") or 0)
    return {
        "candidates": len(candidates),
        "renamed": renamed,
        "quarantined": quarantined,
        "db_updates": db_updates,
        "db_deletes": db_deletes,
        "skipped": skipped,
        "dry_run": dry_run,
        "items": candidates[:200],
    }


def _cleanup_template_tokens_for_album(album_id: int, log: List[str],
                                       *,
                                       dry_run: bool = False) -> Dict[str, Any]:
    candidates = _album_template_token_cleanup_candidates(album_id)
    if not candidates:
        roots = sorted({p.parent for p in _album_item_abs_paths(album_id) if p.parent})
        if not roots:
            return {
                "candidates": 0,
                "renamed": 0,
                "quarantined": 0,
                "db_updates": 0,
                "db_deletes": 0,
                "skipped": 0,
                "items": [],
            }
        return _cleanup_template_token_files(
            roots,
            recursive=False,
            dry_run=dry_run,
            log=log,
        )
    renamed = 0
    quarantined = 0
    db_updates = 0
    db_deletes = 0
    skipped = 0
    for rec in candidates:
        result = _apply_filename_cleanup_candidate(rec, dry_run=dry_run, log=log)
        renamed += int(result.get("renamed") or 0)
        quarantined += int(result.get("quarantined") or 0)
        db_updates += int(result.get("db_updates") or 0)
        db_deletes += int(result.get("db_deletes") or 0)
        skipped += int(result.get("skipped") or 0)
    result = {
        "candidates": len(candidates),
        "renamed": renamed,
        "quarantined": quarantined,
        "db_updates": db_updates,
        "db_deletes": db_deletes,
        "skipped": skipped,
        "dry_run": dry_run,
        "items": candidates[:200],
    }
    if result.get("renamed") or result.get("quarantined"):
        log.append(
            f"  -> {result['renamed']} filename(s) renamed, "
            f"{result['quarantined']} duplicate filename(s) quarantined; "
            f"DB path updates: {result['db_updates']}; DB rows removed: {result['db_deletes']}"
        )
    return result


def _cleanup_initial_browser_password_if_replaced() -> None:
    """Remove stale initial browser credentials after a usable replacement exists."""
    try:
        initial_file = _INITIAL_BROWSER_PASSWORD_FILE
        if initial_file.name != ".initial_admin_password":
            _app_logger.warning("Refusing initial password cleanup for unexpected file name: %s", initial_file)
            return
        if initial_file.parent.is_symlink() or initial_file.is_symlink():
            _app_logger.warning("Refusing initial password cleanup through symlink: %s", initial_file)
            return
        if not initial_file.exists():
            return
        initial_content = ""
        try:
            initial_content = initial_file.read_text(encoding="utf-8").strip()
        except Exception:
            pass

        env_pwd = _first_config_secret("BEETS_WEB_PASSWORD")
        persisted_pwd = ""
        if _PERSISTED_BROWSER_PASSWORD_FILE.exists() and not _PERSISTED_BROWSER_PASSWORD_FILE.is_symlink():
            try:
                persisted_pwd = _PERSISTED_BROWSER_PASSWORD_FILE.read_text(encoding="utf-8").strip()
            except Exception:
                pass

        if (env_pwd and env_pwd != initial_content and _browser_password_is_usable(env_pwd)) or \
           (persisted_pwd and persisted_pwd != initial_content and _browser_password_is_usable(persisted_pwd)):
            initial_file.unlink(missing_ok=True)
    except Exception as ex:
        try:
            _app_logger.warning("Could not remove initial password file: %s", ex)
        except Exception:
            pass


def _prune_empty_review_dirs(folder: Path, touched_dirs: Iterable[Path]) -> None:
    try:
        folder = folder.resolve(strict=False)
    except Exception:
        pass
    for directory in sorted(set(touched_dirs), key=lambda p: len(p.parts), reverse=True):
        current = directory
        for _ in range(12):
            try:
                resolved = current.resolve(strict=False)
                if resolved == folder or not _path_is_under(resolved, folder):
                    break
                resolved.rmdir()
                current = resolved.parent
            except Exception:
                break
    try:
        folder.rmdir()
    except Exception:
        pass


def _classify_album_cleanup_apply_failure(res: Dict[str, Any]) -> Tuple[str, str]:
    """Classify an Apply failure into (error_kind, user-facing message).

    error_kind is one of:
      - "stale_plan": confirmed nothing changed (engine's "mutated" flag is
        False) and the failure looks like a precondition/staleness refusal.
        Safe to tell the user nothing happened and offer "generate a new
        plan."
      - "partial_mutation": the engine's "mutated" flag is True -- this
        Apply call already changed something before the failure. For a plan
        that deletes files (``delete_files`` true or absent) that may be
        irreversibly deleted files; for a row-only plan (``delete_files``
        false) only library rows changed and no file was deleted. Must never
        be described as "nothing changed."
      - "other": mutated is False but the failure isn't a staleness signal
        (e.g. a missing/unreachable database) -- a genuine operational
        error, not evidence the album changed.

    The one guarantee that must never be violated: "stale_plan" -- and only
    "stale_plan" -- may ever be shown to the user as "nothing was changed."
    That decision is driven by the engine's own "mutated" flag, never by
    guessing from error-string content, because a failure's text can look
    like an early/stale-plan refusal even when it fired mid-Apply, after
    earlier steps in the same call already deleted files (e.g. the
    DB-membership-drift check inside the delete_db_record step).
    """
    raw_error = res.get("error") or "Precondition revalidation failed."
    if res.get("mutated") and res.get("delete_files", True) is False:
        return "partial_mutation", (
            "The cleanup plan could not finish: library rows were partly "
            "changed, but no audio files were removed from disk (this cleanup "
            "keeps files). Check the album's current state carefully before "
            f"retrying. Details: {raw_error}"
        )
    if res.get("mutated"):
        return "partial_mutation", (
            "The cleanup plan could not finish: some file(s) were already "
            "deleted before this error occurred, but the operation did not "
            "complete. Check the album's current state carefully before "
            f"retrying. Details: {raw_error}"
        )
    lowered = raw_error.lower()
    stale_signals = (
        "precondition", "changed", "stale", "symlink",
        "no longer exists", "could not stat",
    )
    if any(signal in lowered for signal in stale_signals):
        return "stale_plan", "The album changed after this cleanup plan was created. Nothing was changed. Generate a new plan to continue."
    return "other", raw_error


def controlled_apply_error(ex: BaseException, operation_id: str, *,
                           refused: str = "The engine refused the operation.",
                           busy: str = "Another operation is using these library items; nothing was changed. "
                                       "Try again shortly.",
                           failed: str = "The operation failed.") -> Tuple[Dict[str, Any], int]:
    """Map an exception raised by a transaction apply (or rollback) executor
    to a controlled (JSON body, HTTP status) -- shared by every engine family
    on the generic /api/transactions/<id>/apply route (#220).

    Beets unavailable -> 503, engine refusal -> 400, a held lock or lock
    timeout on a still-Approved transaction -> 409 ``resource_busy``,
    anything else -> a fixed 500. The body never carries exception text (it
    can carry paths or raw engine bodies); ``mutated: False`` is reported
    only when the transaction is provably untouched (still Approved, so the
    executor never claimed it)."""
    try:
        untouched = composite_workflows.get_default_store().get(operation_id).get("status") == "Approved"
    except Exception:
        untouched = False
    body: Dict[str, Any] = {"ok": False, **({"mutated": False} if untouched else {})}
    if isinstance(ex, BeetsUnavailableError):
        return {**body, "code": getattr(ex, "error_code", "") or "beets_unavailable",
                "error": "Beets engine is unavailable."}, 503
    if isinstance(ex, BeetsError):
        return {**body, "code": getattr(ex, "error_code", "") or "beets_error", "error": refused}, 400
    if isinstance(ex, (ResourceLockConflictError, TimeoutError)) and untouched:
        return {**body, "code": "resource_busy", "error": busy}, 409
    _log_controlled_failure(ex, operation_id)
    return {**body, "code": "apply_failed", "error": failed}, 500


def _log_controlled_failure(ex: BaseException, operation_id: str) -> None:
    """Log an unexpected executor failure the way app._handle_unexpected_error
    does: message and traceback both pass through _redact_security_text."""
    _app_logger.error(
        "transaction %s failed (%s): %s\n%s", operation_id, type(ex).__name__, _redact_security_text(ex),
        _redact_security_text("".join(traceback.format_exception(type(ex), ex, ex.__traceback__))))


def controlled_rollback_error(ex: BaseException, operation_id: str, *,
                              lock_before_write: bool = False) -> Tuple[Dict[str, Any], int]:
    """Map an exception raised by an engine-family rollback executor to a
    controlled (JSON body, HTTP status) (#227 SEC-227-1).

    A rollback never runs on an Approved transaction, so the apply mapping's
    "still Approved" proof never holds. Beets unavailable -> 503, engine
    refusal -> 400, a lock conflict -> 409 ``resource_busy``, anything else
    (a lock-registry timeout included: it can come from the release after the
    engine wrote) -> a fixed 500 ``rollback_failed``. ``mutated: False`` is
    reported only for a lock conflict when the caller states the executor
    takes its locks before any write (``lock_before_write``); otherwise the
    key is omitted. The body never carries exception text."""
    if isinstance(ex, BeetsUnavailableError):
        return {"ok": False, "code": getattr(ex, "error_code", "") or "beets_unavailable",
                "error": "Beets engine is unavailable."}, 503
    if isinstance(ex, BeetsError):
        return {"ok": False, "code": getattr(ex, "error_code", "") or "beets_error",
                "error": "The engine refused the rollback."}, 400
    if isinstance(ex, ResourceLockConflictError):
        if lock_before_write:
            return {"ok": False, "mutated": False, "code": "resource_busy",
                    "error": "Another operation is using these library items; nothing was changed. "
                             "Try again shortly."}, 409
        return {"ok": False, "code": "resource_busy",
                "error": "Another operation is using these library items. Reload the transaction "
                         "to check its state before retrying."}, 409
    _log_controlled_failure(ex, operation_id)
    return {"ok": False, "code": "rollback_failed",
            "error": "The rollback failed. Reload the transaction to check its state."}, 500


def album_cleanup_apply_response(operation_id: str) -> Tuple[Dict[str, Any], int]:
    """Apply an Approved album cleanup and map the outcome to (JSON body,
    HTTP status) for both apply routes (/api/albums/cleanup/apply and the
    generic /api/transactions/<id>/apply, PR #204 QA F-B).

    A failure result carries the classified ``error_kind``/message. A raised
    exception never echoes its text (it can carry paths or raw engine
    bodies); ``mutated: False`` is reported only when the transaction is
    provably untouched (still Approved, so the apply never claimed it)."""
    try:
        res = composite_workflows.apply_album_cleanup(operation_id)
    except Exception as ex:
        body, status = controlled_apply_error(
            ex, operation_id,
            refused="The engine refused the album cleanup.",
            busy="Another operation is using this album; nothing was changed. Try again shortly.",
            failed="Album cleanup apply failed.")
        return {**body, "error_kind": "other"}, status
    if res.get("ok"):
        return res, 200
    refused = res.get("code") in ("not_approved", "already_applied")
    # A state refusal (cancelled, claimed, applied) is reported as is: its
    # text names the status and must not be read as a stale plan. Otherwise
    # error_kind is the authoritative UI signal; "stale_plan" (the only kind
    # ever shown as "nothing was changed") comes only from the engine's own
    # "mutated" flag (see _classify_album_cleanup_apply_failure).
    kind, message = ("other", res.get("error") or "Apply refused.") if refused \
        else _classify_album_cleanup_apply_failure(res)
    status = 409 if refused else 400
    return {"ok": False, "code": res.get("code"), "error": message, "error_kind": kind,
            "mutated": bool(res.get("mutated")), "log": res.get("log", [])}, status


def _artist_alias_key(value: str) -> str:
    return " ".join(_normalize_name(_s(value)).casefold().split())


def _cleanup_artist_alias_source_dirs(source_names: List[str], canonical: str,
                                      log: List[str]) -> int:
    removed = 0
    canonical_key = _artist_alias_key(canonical)
    for src_name in sorted({n for n in source_names if _artist_alias_key(n) != canonical_key},
                           key=lambda s: s.casefold()):
        src_dir = MUSIC_ROOT / src_name
        if not src_dir.is_dir():
            continue
        remaining_audio = [
            p for p in src_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in AUDIO_EXT
        ]
        if remaining_audio:
            log.append(
                f"  WARN: {len(remaining_audio)} audio file(s) still in {src_dir}; "
                "beet move may have failed for some albums."
            )
            continue
        log.append(f"  Preserved empty artist folder for missing-album visibility: {src_dir}")
    return removed


# ── Clean: no-audio folder cleanup ────────────────────────────────────────────

FOLDER_CLEAN_AUDIO_EXTS = set(AUDIO_EXTS) | set(AUDIO_EXT) | {".mp3", ".flac"}


# SEC-13: only the configured library and download roots. /tmp and the
# hardcoded /data/downloads and /download paths were removed: a no-audio
# folder sweep must never range over the container's temp space or a path
# no setting controls. The downloads root comes from config_layers
# (DOWNLOADS_ROOT, deprecated alias DOWNLOAD_PATH) and is left out when it is
# "/" or overlaps the library.
FOLDER_CLEAN_ROOTS = [
    MUSIC_ROOT,
    *DOWNLOADS_ALLOWED_ROOTS,
]


def _resolved_path(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except Exception:
        return path.absolute()


def _folder_clean_root(raw_root: str) -> Path:
    # SEC-002 CodeQL repository-wide closure finding: the exists()/is_dir()
    # probe below and the containment check further down both raise a
    # distinct RuntimeError that reaches the client verbatim
    # (delete_no_audio_folders_api/scan_no_audio_folders_api: `str(ex)` in
    # the JSON error response) -- probing existence before containment let
    # an authenticated caller distinguish "path exists" from "path is
    # outside the allowed roots" for any absolute path on the filesystem,
    # not just ones already known to be in-root. Containment must be
    # checked first so the existence probe only ever runs against a
    # path already confirmed to be inside FOLDER_CLEAN_ROOTS.
    root = Path((raw_root or str(MUSIC_ROOT)).strip())
    root_res = _resolved_path(root)
    allowed = [_resolved_path(p) for p in FOLDER_CLEAN_ROOTS]
    if not any(_path_under(root_res, ar) or root_res == ar for ar in allowed):
        raise RuntimeError(f"Root must be under {MUSIC_ROOT}, /data/torrents/music, or downloads")
    if not root_res.exists() or not root_res.is_dir():
        raise RuntimeError("Path not found.")
    return root_res


def _is_top_level_music_artist_folder(folder: Path) -> bool:
    folder_res = _resolved_path(folder)
    music_res = _resolved_path(MUSIC_ROOT)
    return folder_res.parent == music_res


def _scan_no_audio_folder_candidates(root: Path, log: Optional[List[str]] = None) -> Dict[str, Any]:
    """Find topmost folders whose subtree contains no protected audio files."""
    root = _resolved_path(root)
    candidates: List[Dict[str, Any]] = []
    summary = {
        "folders_checked": 0,
        "files_checked": 0,
        "audio_blockers": 0,
        "mp3_flac_blockers": 0,
        "errors": 0,
    }

    def _walk(folder: Path) -> Dict[str, Any]:
        summary["folders_checked"] += 1
        file_count = 0
        dir_count = 0
        byte_count = 0
        audio_count = 0
        mp3_flac_count = 0
        child_candidates: List[Dict[str, Any]] = []
        try:
            children = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        except Exception as ex:
            summary["errors"] += 1
            _app_logger.warning("Cannot read folder %r: %s", str(folder), ex)
            if log is not None:
                log.append(f"  WARN: cannot read {folder} ({type(ex).__name__})")
            return {
                "has_audio": True,
                "files": 0,
                "dirs": 0,
                "bytes": 0,
                "audio": 1,
                "mp3_flac": 0,
                "candidates": [],
            }

        for child in children:
            if child.is_symlink():
                summary["files_checked"] += 1
                file_count += 1
                try:
                    byte_count += int(child.lstat().st_size or 0)
                except Exception:
                    pass
                ext = child.suffix.lower()
                if ext in FOLDER_CLEAN_AUDIO_EXTS:
                    audio_count += 1
                    summary["audio_blockers"] += 1
                    if ext in {".mp3", ".flac"}:
                        mp3_flac_count += 1
                        summary["mp3_flac_blockers"] += 1
                continue
            if child.is_dir():
                dir_count += 1
                info = _walk(child)
                file_count += int(info.get("files") or 0)
                dir_count += int(info.get("dirs") or 0)
                byte_count += int(info.get("bytes") or 0)
                audio_count += int(info.get("audio") or 0)
                mp3_flac_count += int(info.get("mp3_flac") or 0)
                child_candidates.extend(info.get("candidates") or [])
                continue
            if not child.is_file():
                continue
            summary["files_checked"] += 1
            file_count += 1
            try:
                byte_count += int(child.stat().st_size or 0)
            except Exception:
                pass
            ext = child.suffix.lower()
            if ext in FOLDER_CLEAN_AUDIO_EXTS:
                audio_count += 1
                summary["audio_blockers"] += 1
                if ext in {".mp3", ".flac"}:
                    mp3_flac_count += 1
                    summary["mp3_flac_blockers"] += 1

        if folder != root and audio_count == 0:
            if _is_top_level_music_artist_folder(folder):
                return {
                    "has_audio": False,
                    "files": file_count,
                    "dirs": dir_count,
                    "bytes": byte_count,
                    "audio": 0,
                    "mp3_flac": 0,
                    "candidates": child_candidates,
                }
            rel = str(folder.relative_to(root)) if _path_under(folder, root) else folder.name
            return {
                "has_audio": False,
                "files": file_count,
                "dirs": dir_count,
                "bytes": byte_count,
                "audio": 0,
                "mp3_flac": 0,
                "candidates": [{
                    "path": str(folder),
                    "name": folder.name,
                    "relative": rel,
                    "files": file_count,
                    "subfolders": dir_count,
                    "bytes": byte_count,
                }],
            }

        return {
            "has_audio": audio_count > 0,
            "files": file_count,
            "dirs": dir_count,
            "bytes": byte_count,
            "audio": audio_count,
            "mp3_flac": mp3_flac_count,
            "candidates": child_candidates,
        }

    info = _walk(root)
    candidates = sorted(info.get("candidates") or [], key=lambda r: r["path"].casefold())
    summary["candidates"] = len(candidates)
    if log is not None:
        log.append(
            f"Checked {summary['folders_checked']} folder(s), {summary['files_checked']} file(s). "
            f"Found {len(candidates)} folder(s) with no audio below them."
        )
    return {"ok": True, "root": str(root), "folders": candidates, "summary": summary}


def _no_audio_tree_still_safe(folder: Path) -> Optional[str]:
    """Re-check a folder right before deleting it: no audio file and no
    symlink anywhere below it. Returns the refusal reason, or None."""
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        for name in dirnames + filenames:
            entry = Path(dirpath) / name
            if entry.is_symlink():
                return f"contains a symlink: {entry}"
            if entry.suffix.lower() in FOLDER_CLEAN_AUDIO_EXTS:
                return f"contains audio now: {entry.name}"
    return None


def _delete_no_audio_folders(root: str, paths: List[str], *, dry_run: bool,
                             log: List[str]) -> Dict[str, Any]:
    """Delete selected no-audio folder trees in STAGING roots only (QA-1).

    Folders inside the music library are refused (they must leave the
    library through engine quarantine); every folder must be inside a
    staging/download root with no symlink component, and is re-checked for
    audio and symlinks immediately before deletion. Failures are reported
    (ok=false), never hidden."""
    root_path = _folder_clean_root(root)
    music_res = _resolved_path(MUSIC_ROOT)
    if root_path == music_res or _path_under(root_path, music_res):
        log.append(f"  Refused: {root_path} is inside the music library; nothing was deleted.")
        return {"ok": False, "code": "music_root_not_allowed", "root": str(root_path),
                "error": "Folders inside the music library cannot be deleted here; nothing was deleted.",
                "summary": {"folders_removed": 0, "files_removed": 0, "bytes_removed": 0, "dry_run": dry_run,
                            "selected": len(paths or []), "skipped": len(paths or [])},
                "results": [], "log": log}
    scan = _scan_no_audio_folder_candidates(root_path, log)
    allowed = {str(_resolved_path(Path(f["path"]))): f for f in scan.get("folders", [])}
    selected: List[Dict[str, Any]] = []
    seen: set = set()
    for raw in paths or []:
        p = _resolved_path(Path(_s(raw)))
        key = str(p)
        if key in allowed and key not in seen:
            selected.append(allowed[key])
            seen.add(key)
        elif key not in allowed:
            log.append(f"  Skipping unsafe or no-longer-empty folder: {raw}")

    # Remove nested selections when a parent is already selected.
    selected_paths = [Path(f["path"]) for f in selected]
    filtered: List[Dict[str, Any]] = []
    for rec in selected:
        p = Path(rec["path"])
        if any(p != other and _path_under(p, other) for other in selected_paths):
            continue
        filtered.append(rec)

    removed = 0
    files_removed = 0
    bytes_removed = 0
    failures = 0
    results = []
    for rec in sorted(filtered, key=lambda r: len(Path(r["path"]).parts), reverse=True):
        raw_folder = Path(rec["path"])
        results.append({**rec, "removed": False, "dry_run": dry_run})
        refusal = None
        folder = raw_folder
        # Resolve ONCE (S1/F3); every later check and the delete itself use
        # this resolved path, and the delete re-checks it with lstat.
        try:
            folder = composite_workflows._validated_staging_target(raw_folder, "delete")
        except (ValueError, OSError):
            refusal = "outside the staging roots, a staging root itself, protected data, or behind a symlink"
        if refusal is None and (folder == music_res or _path_under(folder, music_res)):
            refusal = "inside the music library"
        if refusal is None:
            refusal = _no_audio_tree_still_safe(folder)
        if refusal:
            failures += 1
            results[-1]["error"] = f"Refused: {refusal}"
            log.append(f"  Refused {folder}: {refusal}")
            continue
        if dry_run:
            removed += 1
            files_removed += int(rec.get("files") or 0)
            bytes_removed += int(rec.get("bytes") or 0)
            results[-1]["removed"] = True
            log.append(f"  Would delete folder tree: {folder}")
            continue
        try:
            composite_workflows._remove_resolved(folder)
            removed += 1
            files_removed += int(rec.get("files") or 0)
            bytes_removed += int(rec.get("bytes") or 0)
            results[-1]["removed"] = True
            log.append(f"  Deleted folder tree: {folder}")
        except Exception as ex:
            failures += 1
            _app_logger.warning("Could not delete folder tree %r: %s", str(folder), ex)
            results[-1]["error"] = "Could not delete this folder."
            log.append(f"  ERROR deleting {folder} ({type(ex).__name__})")

    summary = {
        "folders_removed": removed,
        "files_removed": files_removed,
        "bytes_removed": bytes_removed,
        "dry_run": dry_run,
        "selected": len(paths or []),
        "skipped": max(0, len(paths or []) - len(filtered)),
        "failed": failures,
    }
    return {"ok": failures == 0, "root": str(root_path), "summary": summary,
            "results": results, "log": log,
            **({"error": f"{failures} folder(s) were refused or could not be deleted."} if failures else {})}


def _load_rgid_resolutions() -> Dict[str, Any]:
    """Persisted per-release-group-ID resolution decisions (e.g. 'keep separate').

    Sidecar JSON file, keyed by lowercase mb_releasegroupid. This is the only
    generic resolution/exception-persistence mechanism in the app; without it
    a "Keep separate" click has no effect on the next scan.
    """
    try:
        if RGID_RESOLUTION_STATE_FILE.exists():
            data = json.loads(RGID_RESOLUTION_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_rgid_resolutions(state: Dict[str, Any]) -> None:
    try:
        RGID_RESOLUTION_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = RGID_RESOLUTION_STATE_FILE.with_suffix(f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        tmp.replace(RGID_RESOLUTION_STATE_FILE)
    except Exception:
        pass


def _set_rgid_resolution(rgid: str, decision: str, reason: str = "",
                         album_ids: Optional[List[int]] = None) -> None:
    rgid = _s(rgid).strip().lower()
    if not rgid:
        return
    state = _load_rgid_resolutions()
    state[rgid] = {
        "decision": decision,
        "reason": reason,
        "album_ids": list(album_ids or []),
        "updated_at": time.time(),
    }
    _save_rgid_resolutions(state)


def _clear_rgid_resolution(rgid: str) -> None:
    rgid = _s(rgid).strip().lower()
    state = _load_rgid_resolutions()
    if rgid in state:
        del state[rgid]
        _save_rgid_resolutions(state)


def _clean_remove_orphaned_items(item_ids: List[int], *,
                                 dry_run: bool,
                                 log: List[str],
                                 trigger_plex: bool = True) -> Dict[str, Any]:
    """Remove the Beets ROWS of the given items whose files are missing.

    Each id is re-verified live (still tracked, file absent now); files are
    never deleted (LT-1). An empty selection is a no-op, never a widening."""
    ids = sorted({int(i) for i in item_ids if str(i).isdigit() and int(i) > 0})
    if not ids:
        return {"ok": True, "dry_run": dry_run, "selected": 0, "removed": 0, "removed_count": 0,
                "skipped": 0, "empty_albums_removed": 0, "orphaned_items": []}

    try:
        res = composite_workflows.clean_orphaned_items(item_ids=ids, dry_run=dry_run)
    except (BeetsUnavailableError, BeetsError) as ex:
        log.append(f"  Engine unavailable/error for orphaned-item cleanup: {ex}")
        raise

    if not res.get("ok"):
        log.append(f"  Orphaned-item cleanup refused: {res.get('error')}")
    selected = int(res.get("selected") or 0)
    removed = selected if dry_run else int(res.get("removed_count") or 0)
    skipped_rows = res.get("skipped") or []
    for item in res.get("orphaned_items") or []:
        iid = item.get("id")
        label = f"{_s(item.get('artist'))} - {_s(item.get('title'))}".strip(" -")
        verb = "Would remove" if dry_run else ("Removed" if res.get("ok") else "Did not remove")
        log.append(f"  {verb} row of missing file id={iid}: {label}")
    for row in skipped_rows:
        log.append(f"  Kept item id={row.get('id')}: {row.get('reason')}")

    if not dry_run and removed > 0:
        _invalidate_lib_cache()
        if trigger_plex:
            _trigger_plex_refresh(log)

    log.append(f"Done: {'would remove' if dry_run else 'removed'} {removed} DB row(s) of missing files; "
               "no audio file was deleted.")
    return {
        "ok": bool(res.get("ok")),
        "error": res.get("error"),
        "code": res.get("code"),
        "dry_run": dry_run,
        "selected": len(ids),
        "removed": removed,
        "removed_count": removed,
        "empty_albums_removed": 0,
        "skipped": len(ids) - selected,
        "skipped_items": skipped_rows,
        "orphaned_items": res.get("orphaned_items", []),
        "operation_id": res.get("operation_id"),
    }


def _clean_remove_empty_albums(album_ids: List[int], *,
                               dry_run: bool,
                               log: List[str]) -> Dict[str, Any]:
    ids = sorted({int(i) for i in album_ids if str(i).isdigit() and int(i) > 0})
    if not ids:
        return {"ok": True, "dry_run": dry_run, "selected": 0, "removed": 0, "skipped": 0}

    rows = []
    for aid in ids:
        try:
            alb = composite_workflows.get_album(aid)
            if alb:
                a_items = composite_workflows.find_all_items_by_album_id(aid)
                rows.append({
                    "id": alb["id"],
                    "albumartist": alb.get("albumartist", ""),
                    "album": alb.get("album", ""),
                    "track_count": len(a_items),
                })
        except Exception:
            pass

    removable = [int(r["id"]) for r in rows if int(r["track_count"] or 0) == 0]
    skipped = len(ids) - len(removable)
    for row in rows:
        if int(row["track_count"] or 0) == 0:
            log.append(
                f"  {'Would remove' if dry_run else 'Removing'} empty album id={int(row['id'])}: "
                f"{_s(row['albumartist'])} - {_s(row['album'])}".strip(" -")
            )

    if dry_run or not removable:
        return {
            "ok": True,
            "dry_run": dry_run,
            "selected": len(ids),
            "removed": len(removable) if dry_run else 0,
            "skipped": skipped,
        }

    removed = 0
    for aid in removable:
        try:
            res = composite_workflows.delete_album(aid, delete_files=False)
        except (BeetsUnavailableError, BeetsError) as ex:
            log.append(f"  Engine unavailable removing empty album_id {aid}: {ex}")
            continue
        if not res.get("ok"):
            log.append(f"  Engine rejected removal of empty album_id {aid}: {res.get('error') or 'unknown error'}")
            continue
        removed += 1

    _invalidate_lib_cache()
    _trigger_plex_refresh(log)
    log.append(f"Done: removed {removed} empty album row(s).")
    return {
        "ok": True,
        "dry_run": False,
        "selected": len(ids),
        "removed": removed,
        "skipped": len(ids) - removed,
    }


def _folder_cleanup_path(raw: Any) -> Tuple[Optional[Path], Optional[str]]:
    text = _s(raw).strip()
    if not text:
        return None, "Path is required"
    try:
        path = Path(text)
        if not path.is_absolute():
            path = MUSIC_ROOT / path
        resolved = path.resolve(strict=False)
    except Exception as exc:
        _app_logger.warning("Invalid folder-cleanup path %r: %s", text, type(exc).__name__)
        return None, "Invalid path."
    if not _path_under(resolved, MUSIC_ROOT):
        return None, "Path is outside the configured music library"
    return resolved, None


def _folder_cleanup_is_approved_root(path: Path) -> bool:
    """True when path IS the approved music library root itself (not merely
    contained within it). _path_under()/relative_to() treat a path and its
    root as "under" each other (relative_to returns '.'), so every
    destructive folder-cleanup operation (remove, rename, merge-then-remove)
    must check this separately and refuse to touch the root itself."""
    try:
        return path.resolve(strict=False) == MUSIC_ROOT.resolve(strict=False)
    except Exception:
        return False


def _folder_cleanup_file_inventory(root: Path) -> Dict[str, Dict[str, Any]]:
    files: Dict[str, Dict[str, Any]] = {}
    if not root.exists() or not root.is_dir():
        return files
    for child in root.rglob("*"):
        try:
            if not child.is_file():
                continue
            rel = child.relative_to(root).as_posix()
            stat = child.stat()
        except Exception:
            continue
        files[rel] = {
            "path": str(child),
            "relative_path": rel,
            "size": int(stat.st_size),
            "mtime": float(stat.st_mtime),
        }
    return files


def _folder_cleanup_is_empty(root: Path) -> bool:
    if not root.exists() or not root.is_dir():
        return False
    try:
        next(root.iterdir())
        return False
    except StopIteration:
        return True
    except Exception:
        return False


def _folder_cleanup_db_items(folder: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    # Every caller already resolves through _folder_cleanup_path()/
    # _album_cleanup_trusted_path() before reaching here, but this function
    # touches the filesystem on `folder` directly, so it re-checks
    # containment itself rather than trusting that every present and future
    # caller keeps doing so correctly.
    if not _path_under(folder, MUSIC_ROOT):
        return rows
    if not folder.exists():
        return rows
    try:
        folder_resolved = folder.resolve(strict=False)
    except Exception:
        folder_resolved = folder
    try:
        items = composite_workflows.get_folder_items([str(folder_resolved)])
        for row in items:
            raw_path = row.get("path")
            path_text = _s(raw_path)
            if not path_text:
                continue
            try:
                item_path = Path(path_text)
                if not item_path.is_absolute():
                    item_path = MUSIC_ROOT / item_path
                item_path = item_path.resolve(strict=False)
            except Exception:
                continue
            if not _path_under(item_path, folder_resolved):
                continue
            rows.append(
                {
                    "item_id": int(row["id"]),
                    "album_id": int(row.get("album_id") or 0),
                    "artist": _s(row.get("artist") or row.get("albumartist")),
                    "album": _s(row.get("album")),
                    "title": _s(row.get("title")),
                    "path": path_text,
                    "resolved_path": str(item_path),
                }
            )
    except Exception:
        pass
    return rows


def _folder_cleanup_compare_files(source: Path, target: Path) -> Dict[str, Any]:
    source_files = _folder_cleanup_file_inventory(source)
    target_files = _folder_cleanup_file_inventory(target)
    moves: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    matching: List[Dict[str, Any]] = []
    for rel, src_info in sorted(source_files.items()):
        dst_info = target_files.get(rel)
        proposed_target = str(target / Path(rel))
        if not dst_info:
            moves.append(
                {
                    "source": src_info["path"],
                    "target": proposed_target,
                    "from": src_info["path"],
                    "to": proposed_target,
                    "relative_path": rel,
                    "size": src_info["size"],
                }
            )
            continue
        if int(src_info["size"]) == int(dst_info["size"]):
            matching.append(
                {
                    "source": src_info["path"],
                    "target": dst_info["path"],
                    "relative_path": rel,
                    "size": src_info["size"],
                }
            )
        else:
            conflicts.append(
                {
                    "filename": rel,
                    "source": src_info["path"],
                    "target": dst_info["path"],
                    "source_path": src_info["path"],
                    "target_path": dst_info["path"],
                    "relative_path": rel,
                    "source_size": src_info["size"],
                    "target_size": dst_info["size"],
                    "same_size": False,
                    "reason": "Target file already exists with different size",
                }
            )
    return {
        "source_files": source_files,
        "target_files": target_files,
        "moves": moves,
        "conflicts": conflicts,
        "matching": matching,
        "source_only_count": len(moves),
        "matching_count": len(matching),
        "conflict_count": len(conflicts),
    }


def _folder_cleanup_token(material: Dict[str, Any]) -> str:
    stable = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _canonical_folder_name(source: Path) -> Optional[Path]:
    """Fallback proposed target when a folder-placeholder review is requested
    without an explicit target_path: strip unresolved placeholder/template text
    from the folder name, same as the proposal shown by the scan that lists
    these folders in the first place (_scan_folder_name_placeholders)."""
    name = source.name
    clean_name = _LITERAL_PLACEHOLDER_RE.sub("", name)
    clean_name = _UNRESOLVED_TEMPLATE_TOKEN_RE.sub("", clean_name)
    clean_name = re.sub(r"\s+", " ", clean_name).strip().strip(" -_")
    if not clean_name or clean_name == name:
        return None
    return source.parent / clean_name


def _folder_cleanup_review_for(source: Path, proposed: Optional[Path] = None) -> Dict[str, Any]:
    if proposed is None:
        proposed = _canonical_folder_name(source)
    source_exists = source.exists()
    target_exists = proposed.exists() if proposed else False
    db_items = _folder_cleanup_db_items(source)
    album_ids = [int(item.get("album_id") or 0) for item in db_items]
    known_rgid = _folder_cleanup_known_release_group_id(album_ids)
    source_rgid = _folder_cleanup_release_group_from_name(source.name)
    target_rgid = _folder_cleanup_release_group_from_name(proposed.name) if proposed else ""
    if proposed and source_exists and target_exists:
        comparison = _folder_cleanup_compare_files(source, proposed)
    else:
        comparison = {
            "moves": [],
            "conflicts": [],
            "matching": [],
            "source_only_count": 0,
            "matching_count": 0,
            "conflict_count": 0,
            "source_files": _folder_cleanup_file_inventory(source),
            "target_files": _folder_cleanup_file_inventory(proposed) if proposed else {},
        }
    source_empty = _folder_cleanup_is_empty(source)
    target_empty = _folder_cleanup_is_empty(proposed) if proposed else False
    source_files = comparison["source_files"]
    target_files = comparison["target_files"]
    source_only_files = [move["relative_path"] for move in comparison["moves"]]
    target_only_files = sorted(set(target_files.keys()) - set(source_files.keys()))
    matching_files = [match["relative_path"] for match in comparison["matching"]]
    conflicting_files = [conflict["relative_path"] for conflict in comparison["conflicts"]]
    source_audio_count = sum(1 for rel in source_files if Path(rel).suffix.lower() in AUDIO_EXT)
    target_audio_count = sum(1 for rel in target_files if Path(rel).suffix.lower() in AUDIO_EXT)
    reasons: List[str] = []
    suggested_actions: List[str] = []
    safety_status = "needs_review"
    if not source_exists:
        reasons.append("Source folder does not exist")
    elif not source.is_dir():
        reasons.append("Source path is not a folder")
    elif db_items:
        reasons.append("Folder contains database-tracked files; use DB-aware repair instead of plain filesystem cleanup")
    elif source_empty:
        safety_status = "safe_empty_source"
        suggested_actions.append("remove_empty_source")
    elif proposed and target_exists and comparison["conflict_count"] > 0:
        reasons.append("Target folder contains files with the same relative names")
    elif proposed and target_exists and comparison["source_only_count"] > 0:
        safety_status = "safe_merge"
        suggested_actions.append("preview_merge")
    elif proposed and not target_exists:
        reasons.append("Canonical target folder does not exist; folder creation/rename is not part of this safe cleanup action")
    else:
        reasons.append("No safe automatic cleanup action is available")
    if not known_rgid and _LITERAL_PLACEHOLDER_RE.search(source.name):
        reasons.append("Release Group ID is missing; cannot replace placeholder with a real ID automatically")

    material = {
        "source": str(source),
        "target": str(proposed) if proposed else "",
        "source_exists": source_exists,
        "target_exists": target_exists,
        "source_empty": source_empty,
        "db_item_ids": [item["item_id"] for item in db_items],
        "moves": comparison["moves"],
        "conflicts": comparison["conflicts"],
        "safety_status": safety_status,
    }
    return {
        "ok": True,
        "source_path": str(source),
        "target_path": str(proposed) if proposed else "",
        "proposed_path": str(proposed) if proposed else None,
        "source_name": source.name,
        "target_name": proposed.name if proposed else "",
        "source_exists": source_exists,
        "target_exists": target_exists,
        "source_empty": source_empty,
        "source_is_empty": source_empty,
        "target_is_empty": target_empty,
        "source_file_count": len(source_files),
        "source_audio_count": source_audio_count,
        "target_file_count": len(target_files),
        "target_audio_count": target_audio_count,
        "source_only_files": source_only_files,
        "target_only_files": target_only_files,
        "matching_files": matching_files,
        "conflicting_files": conflicting_files,
        "db_items_in_source": len(db_items),
        "source_db_items": len(db_items),
        "source_db_album_ids": sorted(set(album_ids)),
        "db_items": db_items[:50],
        "release_group_id": known_rgid or source_rgid or target_rgid,
        "known_release_group_id": known_rgid,
        "source_known_rgid": known_rgid or None,
        "source_release_group_id": source_rgid,
        "source_folder_rgid": source_rgid or None,
        "target_release_group_id": target_rgid,
        "target_folder_rgid": target_rgid or None,
        "has_unresolved_placeholder": bool(_UNRESOLVED_TEMPLATE_TOKEN_RE.search(source.name)),
        "has_literal_placeholder": bool(_LITERAL_PLACEHOLDER_RE.search(source.name)),
        "source_has_unresolved_token": bool(_UNRESOLVED_TEMPLATE_TOKEN_RE.search(source.name)),
        "source_has_placeholder": bool(_LITERAL_PLACEHOLDER_RE.search(source.name)),
        "safety_status": safety_status,
        "action": safety_status,
        "safe": safety_status in {"safe_empty_source", "safe_merge"},
        "suggested_actions": suggested_actions,
        "blocking_reasons": reasons,
        "reasons_blocked": reasons,
        "moves_available": comparison["source_only_count"],
        "conflict_count": comparison["conflict_count"],
        "matching_count": comparison["matching_count"],
        "preview_token": _folder_cleanup_token(material),
    }


def _folder_cleanup_merge_preview(source: Path, target: Path) -> Dict[str, Any]:
    comparison = _folder_cleanup_compare_files(source, target)
    db_items = _folder_cleanup_db_items(source)
    blocking: List[str] = []
    if not source.exists() or not source.is_dir():
        blocking.append("Source folder does not exist")
    if not target.exists() or not target.is_dir():
        blocking.append("Target folder does not exist")
    if db_items:
        blocking.append("Source folder contains DB-tracked items; DB path repair must happen before file merge")
    if comparison["conflict_count"] > 0:
        blocking.append("Target file conflicts would require overwrite")
    for move in comparison["moves"]:
        target_parent = Path(_s(move.get("target"))).parent
        if not target_parent.exists():
            blocking.append("Target subfolder does not exist; cleanup apply will not create folders")
            break
    if comparison["source_only_count"] <= 0:
        blocking.append("No source-only files are available to merge")
    safe = not blocking
    source_will_be_empty_after_move = safe and comparison["source_only_count"] == len(comparison["source_files"])
    material = {
        "source": str(source),
        "target": str(target),
        "moves": comparison["moves"],
        "conflicts": comparison["conflicts"],
        "db_item_ids": [item["item_id"] for item in db_items],
        "safe": safe,
    }
    return {
        "ok": True,
        "source_path": str(source),
        "target_path": str(target),
        "moves": comparison["moves"],
        "conflicts": comparison["conflicts"],
        "matching_files": comparison["matching"],
        "db_items_in_source": len(db_items),
        "db_path_updates_needed": False,
        "safe": safe,
        "blocking_reasons": blocking,
        "source_will_be_empty_after_move": source_will_be_empty_after_move,
        "folders_to_remove_if_empty": [str(source)] if source_will_be_empty_after_move else [],
        "preview_token": _folder_cleanup_token(material),
    }


_ALBUM_FOLDER_UUID_IN_BRACES_RE = re.compile(
    r"\{([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\}",
    re.IGNORECASE,
)


_ALBUM_FOLDER_BAD_MBID_SUFFIX_RE = re.compile(
    r"(?:\s+|\s*\{)\bAlbum\s+MbI?d\b\s*\}?\s*$",
    re.IGNORECASE,
)


_ALBUM_FOLDER_YEAR_RE = re.compile(r"\s*[\(\[](\d{4})[\)\]]\s*$")


_ALBUM_CLEANUP_LOSSLESS_EXTS = frozenset({
    ".flac", ".wav", ".aiff", ".aif", ".alac", ".ape", ".wv", ".dsf", ".dff",
})


def _album_cleanup_save_report(report: Dict[str, Any], log: Optional[List[str]] = None) -> None:
    payload = dict(report)
    payload["updated_at"] = time.time()
    try:
        ALBUM_FOLDER_CLEANUP_LAST_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = ALBUM_FOLDER_CLEANUP_LAST_FILE.with_suffix(f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
        tmp.replace(ALBUM_FOLDER_CLEANUP_LAST_FILE)
    except Exception as exc:
        if log is not None:
            log.append(f"[album-folder-cleanup] WARN: could not save report: {exc}")


def _album_cleanup_running_job() -> Optional[Any]:
    for job in jobs.all():
        if job.status != "running":
            continue
        metadata = getattr(job, "metadata", {}) or {}
        jtype = _s(metadata.get("type") or "")
        if jtype.startswith("album-folder-cleanup"):
            return job
    return None


def _album_cleanup_safe_component(value: Any, fallback: str = "Unknown Album") -> str:
    cleaned = re.sub(r'[<>:"\\|?*\x00-\x1f]', "_", _s(value)).strip()
    cleaned = cleaned.replace("/", "_").rstrip(". ")
    return cleaned or fallback


def _album_cleanup_norm_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _s(value).casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "", text)


def _album_cleanup_parse_folder_name(name: str) -> Dict[str, Any]:
    raw = _s(name).strip()
    uuid_values = [
        m.group(1).lower()
        for m in _ALBUM_FOLDER_UUID_IN_BRACES_RE.finditer(raw)
        if _MB_UUID_RE.match(m.group(1))
    ]
    has_literal_placeholder = bool(_LITERAL_PLACEHOLDER_RE.search(raw))
    has_bad_mbid_suffix = bool(_ALBUM_FOLDER_BAD_MBID_SUFFIX_RE.search(raw))
    has_unresolved_template = bool(_UNRESOLVED_TEMPLATE_TOKEN_RE.search(raw))

    cleaned = _LITERAL_PLACEHOLDER_RE.sub("", raw)
    cleaned = _UNRESOLVED_TEMPLATE_TOKEN_RE.sub("", cleaned)
    cleaned = _ALBUM_FOLDER_BAD_MBID_SUFFIX_RE.sub("", cleaned)
    cleaned = _ALBUM_FOLDER_UUID_IN_BRACES_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(" -_")

    year = ""
    year_match = _ALBUM_FOLDER_YEAR_RE.search(cleaned)
    if year_match:
        year = year_match.group(1)
        cleaned = cleaned[:year_match.start()].strip().strip(" -_")

    return {
        "title": cleaned or raw,
        "year": year,
        "uuid_stamp": uuid_values[-1] if uuid_values else "",
        "uuid_stamps": uuid_values,
        "has_literal_placeholder": has_literal_placeholder,
        "has_bad_mbid_suffix": has_bad_mbid_suffix,
        "has_unresolved_template": has_unresolved_template,
    }


def _album_cleanup_canonical_name(album: str, year: Any, rgid: str) -> str:
    title = _album_cleanup_safe_component(album, "Unknown Album")
    year_text = _s(year).strip()[:4]
    if year_text and year_text.isdigit():
        base = f"{title} ({year_text})"
    else:
        base = f"{title} ()"
    rgid_text = _s(rgid).strip().lower()
    if _MB_UUID_RE.match(rgid_text):
        base += f" {{{rgid_text}}}"
    return base


def _album_cleanup_row_value(row: Any, key: str, default: Any = "") -> Any:
    try:
        if hasattr(row, "keys") and key in row.keys():
            return row[key]
    except Exception:
        pass
    return default


def _album_cleanup_abs_path(raw_path: Any) -> Optional[Path]:
    text = os.fsdecode(raw_path) if isinstance(raw_path, (bytes, bytearray)) else _s(raw_path)
    text = text.replace("\x00", "").strip()
    if not text:
        return None
    path = Path(text)
    if not path.is_absolute():
        path = MUSIC_ROOT / path
    return path.resolve(strict=False)


def _album_cleanup_trusted_path(
    raw: Any,
    root: Path,
    *,
    expected_type: Optional[str] = None,
    require_exists: bool = True,
    allow_missing_leaf: bool = False,
    reject_root: bool = True,
) -> Tuple[Optional[Path], Optional[str]]:
    text = os.fsdecode(raw) if isinstance(raw, (bytes, bytearray)) else _s(raw)
    text = text.strip()
    error = _import_review_path_text_error(text, allow_relative=False)
    if error:
        return None, error
    try:
        root_resolved = root.resolve(strict=False)
    except Exception:
        return None, "Music library root is not available."
    candidate = Path(text)
    if reject_root and candidate == root_resolved:
        return None, "Refusing to operate on the music library root."
    if candidate != root_resolved and not _path_lexically_under(candidate, root_resolved):
        return None, "Album cleanup path is outside the music library."
    if _path_has_symlink_component_under(candidate, root_resolved, include_leaf=not allow_missing_leaf):
        return None, "Album cleanup path cannot contain symlink components."
    if candidate.is_symlink():
        return None, "Album cleanup path cannot be a symlink."
    if require_exists and not candidate.exists():
        return None, "Album cleanup path does not exist."
    if expected_type == "dir" and candidate.exists() and not candidate.is_dir():
        return None, "Album cleanup path is not a folder."
    if expected_type == "file" and candidate.exists() and not candidate.is_file():
        return None, "Album cleanup path is not a file."
    try:
        resolved = candidate.resolve(strict=False)
    except Exception:
        return None, "Invalid album cleanup path."
    if reject_root and resolved == root_resolved:
        return None, "Refusing to operate on the music library root."
    if not _path_is_under(resolved, root_resolved):
        return None, "Album cleanup path is outside the music library."
    if expected_type == "dir" and resolved.exists() and not resolved.is_dir():
        return None, "Album cleanup path is not a folder."
    if expected_type == "file" and resolved.exists() and not resolved.is_file():
        return None, "Album cleanup path is not a file."
    return resolved, None


def _album_cleanup_trusted_destination(raw: Any, root: Path, container: Path) -> Tuple[Optional[Path], Optional[str]]:
    dest, error = _album_cleanup_trusted_path(
        raw,
        root,
        expected_type="file",
        require_exists=False,
        allow_missing_leaf=True,
        reject_root=True,
    )
    if error or dest is None:
        return None, error
    try:
        container_resolved = container.resolve(strict=False)
    except Exception:
        return None, "Album cleanup target is invalid."
    if dest == container_resolved or not _path_is_under(dest, container_resolved):
        return None, "Album cleanup target is outside the approved folder."
    if _path_has_symlink_component_under(dest, container_resolved, include_leaf=False):
        return None, "Album cleanup target cannot contain symlink components."
    return dest, None


def _album_cleanup_db_index(root: Path) -> Dict[str, Any]:
    folder_db: Dict[str, Dict[str, Any]] = {}
    file_db: Dict[str, Dict[str, Any]] = {}
    try:
        rows = composite_workflows.get_album_cleanup_index()
    except BeetsUnavailableError as ex:
        _app_logger.warning("Beets engine unavailable in _album_cleanup_db_index: %s", ex)
        rows = []

    for row in rows:
        item_path = _album_cleanup_abs_path(_album_cleanup_row_value(row, "item_path"))
        if item_path is None or not _path_under(item_path, root):
            continue
        folder = item_path.parent.resolve(strict=False)
        folder_key = str(folder)
        rec = folder_db.setdefault(folder_key, {
            "album_ids": set(),
            "item_count": 0,
            "albums": [],
            "items": [],
        })
        album_id = int(_album_cleanup_row_value(row, "item_album_id", 0) or 0)
        if album_id > 0:
            rec["album_ids"].add(album_id)
        album_info = {
            "id": int(_album_cleanup_row_value(row, "album_id", album_id) or album_id or 0),
            "album": _s(_album_cleanup_row_value(row, "album_album")),
            "albumartist": _s(_album_cleanup_row_value(row, "album_albumartist")),
            "year": _s(_album_cleanup_row_value(row, "album_year")),
            "mb_albumid": _s(_album_cleanup_row_value(row, "album_mb_albumid")).strip().lower(),
            "mb_releasegroupid": _s(_album_cleanup_row_value(row, "album_mb_releasegroupid")).strip().lower(),
        }
        if album_info not in rec["albums"]:
            rec["albums"].append(album_info)
        item_info = {
            "item_id": int(_album_cleanup_row_value(row, "item_id", 0) or 0),
            "album_id": album_id,
            "title": _s(_album_cleanup_row_value(row, "item_title")),
            "track": int(_album_cleanup_row_value(row, "item_track", 0) or 0),
            "disc": int(_album_cleanup_row_value(row, "item_disc", 0) or 0),
            "bitrate": int(float(_album_cleanup_row_value(row, "item_bitrate", 0) or 0)),
            "format": _s(_album_cleanup_row_value(row, "item_format")).lower(),
            "path": str(item_path),
        }
        rec["items"].append(item_info)
        rec["item_count"] += 1
        file_db[str(item_path)] = item_info

    return {"folders": folder_db, "files": file_db}


def _album_cleanup_majority(values: Iterable[Any]) -> str:
    counts: Dict[str, int] = {}
    display: Dict[str, str] = {}
    for value in values:
        text = _s(value).strip()
        if not text:
            continue
        key = text.casefold()
        counts[key] = counts.get(key, 0) + 1
        display.setdefault(key, text)
    if not counts:
        return ""
    key = sorted(counts, key=lambda k: (-counts[k], display[k].casefold()))[0]
    return display[key]


def _album_cleanup_file_inventory(folder: Path) -> Dict[str, Dict[str, Any]]:
    files: Dict[str, Dict[str, Any]] = {}
    if not folder.exists() or not folder.is_dir() or folder.is_symlink():
        return files
    for child in folder.rglob("*"):
        try:
            if child.is_symlink() or not child.is_file():
                continue
            rel = child.relative_to(folder).as_posix()
            stat = child.stat()
        except Exception:
            continue
        ext = child.suffix.lower()
        files[rel] = {
            "path": str(child),
            "relative_path": rel,
            "size": int(stat.st_size),
            "mtime": float(stat.st_mtime),
            "is_audio": ext in AUDIO_EXT,
            "is_artwork": ext in _ART_EXTS,
        }
    return files


def _album_cleanup_file_hash(info: Dict[str, Any]) -> str:
    for key in ("sha1", "hash", "digest"):
        value = _s(info.get(key)).strip().lower()
        if value:
            return value
    raw_path = _s(info.get("path")).strip()
    if not raw_path:
        return ""
    path = Path(raw_path)
    if not path.exists() or not path.is_file():
        return ""
    h = hashlib.sha1()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                h.update(chunk)
    except Exception:
        return ""
    digest = h.hexdigest()
    info["sha1"] = digest
    return digest


def _album_cleanup_verified_same_file(source_info: Dict[str, Any], target_info: Dict[str, Any]) -> bool:
    source_size = int(source_info.get("size") or 0)
    target_size = int(target_info.get("size") or 0)
    if source_size and target_size and source_size != target_size:
        return False
    source_hash = _album_cleanup_file_hash(source_info)
    target_hash = _album_cleanup_file_hash(target_info)
    return bool(source_hash and target_hash and source_hash == target_hash)


def _album_cleanup_safe_artwork_relative(rel: str, occupied: set) -> str:
    clean_rel = _s(rel).replace("\\", "/").strip("/")
    parts = [p for p in clean_rel.split("/") if p]
    filename = parts[-1] if parts else "cover.jpg"
    parent = parts[:-1]
    suffix = Path(filename).suffix
    stem = filename[:-len(suffix)] if suffix else filename
    suffix = suffix or ".jpg"
    for index in range(1, 10000):
        candidate_name = f"{stem}-{index}{suffix}"
        candidate = "/".join(parent + [candidate_name])
        if candidate not in occupied:
            occupied.add(candidate)
            return candidate
    fallback = "/".join(parent + [f"{stem}-{uuid.uuid4().hex[:8]}{suffix}"])
    occupied.add(fallback)
    return fallback


def _album_cleanup_merge_plan(records: List[Dict[str, Any]], canonical_path: str) -> Dict[str, Any]:
    from backend.matching_contract import build_album_matching_decision
    canonical = next((r for r in records if _s(r.get("path")) == canonical_path), None)
    canonical_files: Dict[str, Dict[str, Any]] = dict((canonical or {}).get("files") or {})
    occupied = set(canonical_files.keys())
    planned_targets: Dict[str, Dict[str, Any]] = {}
    audio_to_move: List[Dict[str, Any]] = []
    duplicate_files: List[Dict[str, Any]] = []
    artwork_to_move: List[Dict[str, Any]] = []
    unknown_files: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []

    for rec in records:
        source_path = _s(rec.get("path"))
        if source_path == canonical_path:
            continue
        if canonical:
            c_rg_raw = _s(canonical.get("release_group_id") or canonical.get("mb_releasegroupid")).strip().lower()
            c_rg = c_rg_raw if _MB_UUID_RE.match(c_rg_raw) else ""
            r_rg_raw = _s(rec.get("release_group_id") or rec.get("mb_releasegroupid")).strip().lower()
            r_rg = r_rg_raw if _MB_UUID_RE.match(r_rg_raw) else ""
            if c_rg and r_rg and c_rg != r_rg:
                merge_decision = build_album_matching_decision(
                    current={"mb_releasegroupid": c_rg, "album": canonical.get("album"), "artist": canonical.get("artist")},
                    candidate={"mb_releasegroupid": r_rg, "album": rec.get("album"), "artist": rec.get("artist")},
                )
                d_dict = merge_decision.to_dict()
                if d_dict.get("conflicts"):
                    conflicts.append({
                        "source": source_path,
                        "target": canonical_path,
                        "relative_path": "",
                        "reason": f"Release Group / identity conflict ({d_dict.get('reason_code')}): auto-merge rejected",
                    })
                    continue
        for rel, source_info in sorted((rec.get("files") or {}).items(), key=lambda item: _s(item[0]).casefold()):
            rel_text = _s(rel).replace("\\", "/").strip("/")
            if not rel_text:
                continue
            source_file = _s(source_info.get("path") or str(Path(source_path) / rel_text))
            target_info = canonical_files.get(rel_text) or planned_targets.get(rel_text)
            if source_info.get("is_audio"):
                if target_info:
                    if _album_cleanup_verified_same_file(source_info, target_info):
                        duplicate_files.append({
                            "source": source_file,
                            "target": _s(target_info.get("path") or str(Path(canonical_path) / rel_text)),
                            "relative_path": rel_text,
                            "reason": "verified duplicate audio",
                        })
                    else:
                        conflicts.append({
                            "source": source_file,
                            "target": _s(target_info.get("path") or str(Path(canonical_path) / rel_text)),
                            "relative_path": rel_text,
                            "reason": "target file exists but audio differs or cannot be verified",
                        })
                    continue
                target = str(Path(canonical_path) / rel_text)
                planned = {**source_info, "path": target}
                planned_targets[rel_text] = planned
                occupied.add(rel_text)
                audio_to_move.append({"source": source_file, "target": target, "relative_path": rel_text})
                continue

            if source_info.get("is_artwork"):
                if target_info:
                    if _album_cleanup_verified_same_file(source_info, target_info):
                        duplicate_files.append({
                            "source": source_file,
                            "target": _s(target_info.get("path") or str(Path(canonical_path) / rel_text)),
                            "relative_path": rel_text,
                            "reason": "verified duplicate artwork",
                        })
                        continue
                    target_rel = _album_cleanup_safe_artwork_relative(rel_text, occupied)
                else:
                    target_rel = rel_text
                    occupied.add(target_rel)
                target = str(Path(canonical_path) / target_rel)
                planned = {**source_info, "path": target}
                planned_targets[target_rel] = planned
                artwork_to_move.append({
                    "source": source_file,
                    "target": target,
                    "relative_path": rel_text,
                    "target_relative_path": target_rel,
                })
                continue

            unknown_files.append({
                "source": source_file,
                "relative_path": rel_text,
                "reason": "unknown leftover files",
            })

    return {
        "audio_files_to_move": audio_to_move,
        "duplicate_files_to_quarantine": duplicate_files,
        "artwork_files_to_move": artwork_to_move,
        "unknown_files": unknown_files,
        "conflicts": conflicts,
        "final_folder_layout": sorted(occupied),
    }


def _album_cleanup_folder_record(album_dir: Path, db_index: Dict[str, Any]) -> Dict[str, Any]:
    parsed = _album_cleanup_parse_folder_name(album_dir.name)
    folder_key = str(album_dir.resolve(strict=False))
    db_info = (db_index.get("folders") or {}).get(folder_key, {})
    albums = list(db_info.get("albums") or [])
    inventory = _album_cleanup_file_inventory(album_dir)
    folder_uuid = _s(parsed.get("uuid_stamp")).strip().lower()
    db_rgids = sorted({
        _s(album.get("mb_releasegroupid")).strip().lower()
        for album in albums
        if _MB_UUID_RE.match(_s(album.get("mb_releasegroupid")).strip().lower())
    })
    db_release_ids = sorted({
        _s(album.get("mb_albumid")).strip().lower()
        for album in albums
        if _MB_UUID_RE.match(_s(album.get("mb_albumid")).strip().lower())
    })
    tag_info = _album_cleanup_embedded_musicbrainz_tags(album_dir, inventory) if not db_rgids or not db_release_ids else {}
    tag_rgids = sorted({
        _album_cleanup_valid_rgid(value)
        for value in tag_info.get("mb_releasegroupids", [])
        if _album_cleanup_valid_rgid(value)
    })
    tag_release_ids = sorted({
        _album_cleanup_valid_rgid(value)
        for value in tag_info.get("mb_albumids", [])
        if _album_cleanup_valid_rgid(value)
    })
    known_rgids = db_rgids or (tag_rgids if len(tag_rgids) == 1 else [])
    known_release_ids = sorted(set(db_release_ids + tag_release_ids))
    release_id_stamp = bool(folder_uuid and folder_uuid in known_release_ids and folder_uuid not in known_rgids)
    effective_rgid = known_rgids[0] if known_rgids else ("" if release_id_stamp else folder_uuid)
    album_title = (
        _album_cleanup_majority(album.get("album") for album in albums)
        or _album_cleanup_majority(tag_info.get("albums", []))
        or _s(parsed.get("title"))
    )
    album_year = (
        _album_cleanup_majority(album.get("year") for album in albums)
        or _album_cleanup_majority(tag_info.get("years", []))
        or _s(parsed.get("year"))
    )

    return {
        "path": str(album_dir),
        "name": album_dir.name,
        "artist": album_dir.parent.name,
        "artist_path": str(album_dir.parent),
        "album": album_title,
        "year": _s(album_year).strip()[:4],
        "parsed_album": _s(parsed.get("title")),
        "parsed_year": _s(parsed.get("year")),
        "folder_uuid": folder_uuid,
        "effective_rgid": effective_rgid,
        "db_rgids": db_rgids,
        "db_release_ids": db_release_ids,
        "tag_rgids": tag_rgids,
        "tag_release_ids": tag_release_ids,
        "tag_rgid_conflict": len(tag_rgids) > 1,
        "musicbrainz_tag_files_inspected": int(tag_info.get("inspected") or 0),
        "release_id_stamp": release_id_stamp,
        "has_literal_placeholder": bool(parsed.get("has_literal_placeholder")),
        "has_bad_mbid_suffix": bool(parsed.get("has_bad_mbid_suffix")),
        "has_unresolved_template": bool(parsed.get("has_unresolved_template")),
        "db_item_count": int(db_info.get("item_count") or 0),
        "db_album_ids": sorted(db_info.get("album_ids") or []),
        "file_count": len(inventory),
        "audio_count": sum(1 for item in inventory.values() if item.get("is_audio")),
        "artwork_count": sum(1 for item in inventory.values() if item.get("is_artwork")),
        "is_empty": len(inventory) == 0,
        "files": inventory,
    }


def _album_cleanup_issue_id(paths: Iterable[str], canonical: str, issue_types: Iterable[str]) -> str:
    material = "|".join(sorted(_s(p) for p in paths)) + "|" + _s(canonical) + "|" + ",".join(sorted(issue_types))
    return hashlib.sha1(material.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _album_cleanup_artwork_to_move(records: List[Dict[str, Any]], canonical_path: str) -> int:
    return len(_album_cleanup_merge_plan(records, canonical_path).get("artwork_files_to_move") or [])


def _album_cleanup_source_audio_count(records: List[Dict[str, Any]], canonical_path: str) -> int:
    plan = _album_cleanup_merge_plan(records, canonical_path)
    return len(plan.get("audio_files_to_move") or []) + sum(
        1 for item in plan.get("duplicate_files_to_quarantine") or []
        if _s(item.get("reason")).endswith("audio")
    )


def _album_cleanup_canonical_candidates(records: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    candidates: List[Dict[str, str]] = []
    seen: set = set()
    for rec in records:
        folder_uuid = _album_cleanup_valid_rgid(rec.get("folder_uuid"))
        if not folder_uuid or rec.get("release_id_stamp"):
            continue
        effective = _album_cleanup_valid_rgid(rec.get("effective_rgid"))
        if effective and effective != folder_uuid:
            continue
        path = _s(rec.get("path"))
        key = (folder_uuid, path)
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "rgid": folder_uuid,
            "path": path,
            "album_norm": _album_cleanup_norm_text(rec.get("album") or rec.get("parsed_album")),
        })
    return candidates


def _album_cleanup_existing_canonical_path(candidates: List[Dict[str, str]], rgid: str, canonical_name: str) -> str:
    matches = [candidate for candidate in candidates if candidate.get("rgid") == rgid and candidate.get("path")]
    if len(matches) == 1:
        return _s(matches[0].get("path"))
    for candidate in matches:
        try:
            if Path(_s(candidate.get("path"))).name == canonical_name:
                return _s(candidate.get("path"))
        except Exception:
            continue
    return ""


def _album_cleanup_safe_reason(records: List[Dict[str, Any]], inference: str) -> str:
    if any(rec.get("release_id_stamp") for rec in records):
        return "Safe merge: stale Album MBID maps to target Release Group."
    if any(
        rec.get("has_bad_mbid_suffix") or rec.get("has_literal_placeholder") or rec.get("has_unresolved_template")
        for rec in records
    ):
        return "Safe merge: source resolved to canonical Release Group folder."
    if inference == "missing_rgid_from_target":
        return "Safe merge: missing RGID inferred from target folder."
    return "Safe merge: same album identity, no file conflicts."


def _album_cleanup_classification_reason(safety: str, blockers: List[str],
                                         records: List[Dict[str, Any]], inference: str) -> str:
    if safety == "Safe":
        return _album_cleanup_safe_reason(records, inference)
    reason = blockers[0] if blockers else ""
    if safety == "Blocked":
        if reason == "target file exists but audio differs":
            return "Blocked: target file exists but audio differs."
        if reason == "different MusicBrainz Release Group IDs":
            return "Blocked: Release Group ID conflict."
        if reason == "unknown leftover files":
            return "Blocked: unknown leftover files."
        return f"Blocked: {reason}." if reason else "Blocked."
    if reason == "multiple possible target folders":
        return "Needs review: multiple possible target folders."
    if reason == "different artist/album identity":
        return "Needs review: album identity uncertain."
    if reason == "duplicate audio could not be verified":
        return "Needs review: duplicate audio could not be verified."
    if reason == "Release Group ID cannot be inferred":
        return "Needs review: Release Group ID cannot be inferred."
    if reason == "multiple embedded Release Group IDs":
        return "Needs review: multiple embedded Release Group IDs."
    return f"Needs review: {reason}." if reason else "Needs review."


def _album_cleanup_build_issue(artist_dir: Path, records: List[Dict[str, Any]], issue_types: List[str]) -> Optional[Dict[str, Any]]:
    if not records:
        return None
    current_paths = [_s(rec.get("path")) for rec in records]
    rgids = sorted({
        _album_cleanup_valid_rgid(rec.get("effective_rgid"))
        for rec in records
        if _album_cleanup_valid_rgid(rec.get("effective_rgid"))
    })
    album_norms = sorted({
        _album_cleanup_norm_text(rec.get("album") or rec.get("parsed_album"))
        for rec in records
        if _album_cleanup_norm_text(rec.get("album") or rec.get("parsed_album"))
    })
    canonical_candidates = _album_cleanup_canonical_candidates(records)
    canonical_rgids = sorted({candidate["rgid"] for candidate in canonical_candidates if candidate.get("rgid")})
    missing_effective_rgid = any(not _album_cleanup_valid_rgid(rec.get("effective_rgid")) for rec in records)
    missing_folder_stamp = any(
        _album_cleanup_valid_rgid(rec.get("effective_rgid")) and not _album_cleanup_valid_rgid(rec.get("folder_uuid"))
        for rec in records
    )
    inference = ""
    merge_plan: Dict[str, Any] = {
        "audio_files_to_move": [],
        "duplicate_files_to_quarantine": [],
        "artwork_files_to_move": [],
        "unknown_files": [],
        "conflicts": [],
        "final_folder_layout": [],
    }
    if len(rgids) > 1:
        canonical_path = ""
        if missing_effective_rgid and len(canonical_rgids) > 1 and len(album_norms) <= 1:
            safety = "Needs review"
            action = "Choose the correct canonical Release Group ID folder"
            blockers = ["multiple possible target folders"]
        else:
            safety = "Blocked"
            action = "Different Release Group IDs found; keep separate unless manually confirmed"
            blockers = ["different MusicBrainz Release Group IDs"]
        rgid = ""
    else:
        rgid = rgids[0] if rgids else (canonical_rgids[0] if len(canonical_rgids) == 1 and len(album_norms) <= 1 else "")
        if rgid and (missing_effective_rgid or missing_folder_stamp):
            inference = "missing_rgid_from_target"
        elif rgid:
            inference = "same_album_identity"
        album = _album_cleanup_majority(rec.get("album") for rec in records) or _s(records[0].get("album"))
        year = _album_cleanup_majority(rec.get("year") for rec in records) or _s(records[0].get("year"))
        canonical_name = _album_cleanup_canonical_name(album, year, rgid)
        computed_canonical = artist_dir / canonical_name
        canonical_path = _album_cleanup_existing_canonical_path(canonical_candidates, rgid, canonical_name) or str(computed_canonical)
        blockers = []
        if any(rec.get("tag_rgid_conflict") for rec in records):
            blockers.append("multiple embedded Release Group IDs")
        if not rgid:
            if len(canonical_rgids) > 1:
                blockers.append("multiple possible target folders")
            else:
                blockers.append("Release Group ID cannot be inferred")
        if len(album_norms) > 1 and not rgid:
            blockers.append("different artist/album identity")
        canonical_check = Path(canonical_path).resolve(strict=False)
        if canonical_check.parent.resolve(strict=False) != artist_dir.resolve(strict=False):
            blockers.append("canonical target is outside the artist folder")
        for rec in records:
            rec_path = Path(_s(rec.get("path"))).resolve(strict=False)
            if rec_path.parent.resolve(strict=False) != artist_dir.resolve(strict=False):
                blockers.append("source folder is outside the artist folder")
                break
        merge_plan = _album_cleanup_merge_plan(records, canonical_path)
        if merge_plan.get("conflicts"):
            blockers.append("target file exists but audio differs")
        if merge_plan.get("unknown_files"):
            blockers.append("unknown leftover files")
        hard_blockers = {
            "target file exists but audio differs",
            "unknown leftover files",
            "canonical target is outside the artist folder",
            "source folder is outside the artist folder",
        }
        if any(reason in hard_blockers for reason in blockers):
            safety = "Blocked"
        else:
            safety = "Safe" if not blockers else "Needs review"
        if len(records) > 1:
            action = "Merge duplicate album folders into the canonical Release Group ID folder"
        elif records[0].get("is_empty"):
            action = "Remove empty album folder"
        else:
            action = "Rename album folder to the canonical Release Group ID folder"

    duplicate_tracks = len(merge_plan.get("duplicate_files_to_quarantine") or []) if canonical_path else 0
    artwork_to_move = len(merge_plan.get("artwork_files_to_move") or []) if canonical_path else 0
    source_audio_count = (
        len(merge_plan.get("audio_files_to_move") or []) +
        sum(1 for item in merge_plan.get("duplicate_files_to_quarantine") or [] if "audio" in _s(item.get("reason")))
    ) if canonical_path else 0
    files_to_move_count = len(merge_plan.get("audio_files_to_move") or []) + artwork_to_move
    risk_reason = _album_cleanup_classification_reason(safety, blockers, records, inference)
    return {
        "id": _album_cleanup_issue_id(current_paths, canonical_path, issue_types),
        "artist": artist_dir.name,
        "album": _album_cleanup_majority(rec.get("album") for rec in records) or _s(records[0].get("album")),
        "year": _album_cleanup_majority(rec.get("year") for rec in records) or _s(records[0].get("year")),
        "release_group_id": rgid,
        "release_group_inference": inference,
        "current_folders": current_paths,
        "current_folder_names": [_s(rec.get("name")) for rec in records],
        "proposed_canonical_folder": canonical_path,
        "canonical_folder": canonical_path,
        "proposed_action": action,
        "safety": safety,
        "safe": safety == "Safe",
        "risk_level": safety,
        "risk_reason": risk_reason,
        "classification_reason": risk_reason,
        "status": "Active",
        "issue_types": sorted(set(issue_types)),
        "blocking_reasons": blockers,
        "folders": records,
        "merge_plan": merge_plan,
        "audio_files_to_move": merge_plan.get("audio_files_to_move") or [],
        "duplicate_files_to_quarantine": merge_plan.get("duplicate_files_to_quarantine") or [],
        "artwork_files_to_move": merge_plan.get("artwork_files_to_move") or [],
        "unknown_files": merge_plan.get("unknown_files") or [],
        "conflicts": merge_plan.get("conflicts") or [],
        "final_folder_layout": merge_plan.get("final_folder_layout") or [],
        "files_to_move": files_to_move_count,
        "files_to_safe_delete": duplicate_tracks,
        "folders_to_remove": [path for path in current_paths if path and path != canonical_path],
        "duplicate_tracks": duplicate_tracks,
        "artwork_to_move": artwork_to_move,
        "source_audio_count": source_audio_count,
        "unproven_source_audio": len(merge_plan.get("conflicts") or []),
    }


def _album_folder_cleanup_plan(root: Optional[Path] = None,
                               progress: Optional[Any] = None,
                               cancel_event: Optional[Any] = None) -> Dict[str, Any]:
    scan_root = (root or MUSIC_ROOT).resolve(strict=False)
    if not scan_root.exists() or not scan_root.is_dir():
        raise RuntimeError(f"Music library root is not accessible: {scan_root}")

    db_index = _album_cleanup_db_index(scan_root)
    issues: List[Dict[str, Any]] = []
    errors: List[str] = []
    seen_issue_keys: set = set()
    artist_dirs = [
        p for p in sorted(scan_root.iterdir(), key=lambda x: x.name.casefold())
        if p.is_dir() and not p.name.startswith(".")
    ]
    album_folders_scanned = 0

    if progress:
        progress({
            "category": "Cleanup",
            "current_task": "Scanning artist folders for album-folder cleanup",
            "scan_path": str(scan_root),
            "scanned_count": 0,
            "total_count": len(artist_dirs),
        })

    for idx, artist_dir in enumerate(artist_dirs, start=1):
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("cancelled")
        try:
            album_dirs = [
                p for p in sorted(artist_dir.iterdir(), key=lambda x: x.name.casefold())
                if p.is_dir() and not p.name.startswith(".")
            ]
        except Exception as exc:
            errors.append(f"{artist_dir}: {exc}")
            continue
        records = [_album_cleanup_folder_record(album_dir, db_index) for album_dir in album_dirs]
        album_folders_scanned += len(records)

        for rec in records:
            if rec.get("is_empty") and int(rec.get("db_item_count") or 0) == 0:
                issue = _album_cleanup_build_issue(artist_dir, [rec], ["empty_folder"])
                if issue:
                    issue["proposed_action"] = "Remove empty album folder"
                    issue["safety"] = "Safe"
                    issue["safe"] = True
                    issue["risk_level"] = "Safe"
                    issue["risk_reason"] = "Safe cleanup: empty folder with no DB-tracked items."
                    key = tuple(sorted(issue["current_folders"]))
                    if key not in seen_issue_keys:
                        seen_issue_keys.add(key)
                        issues.append(issue)

        title_groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        rgid_groups: Dict[str, List[Dict[str, Any]]] = {}
        for rec in records:
            if rec.get("is_empty"):
                continue
            title_key = (_album_cleanup_norm_text(rec.get("album")), _s(rec.get("year")))
            if title_key[0]:
                title_groups.setdefault(title_key, []).append(rec)
            rgid = _s(rec.get("effective_rgid")).strip().lower()
            if _MB_UUID_RE.match(rgid):
                rgid_groups.setdefault(rgid, []).append(rec)

        candidate_groups: List[Tuple[List[Dict[str, Any]], List[str]]] = []
        for group in title_groups.values():
            issue_types: List[str] = []
            if len(group) > 1:
                issue_types.append("duplicate_album_folders")
            if any(rec.get("has_bad_mbid_suffix") or rec.get("has_literal_placeholder") or rec.get("has_unresolved_template") for rec in group):
                issue_types.append("bad_folder_name")
            if any(rec.get("release_id_stamp") for rec in group):
                issue_types.append("release_id_used_instead_of_release_group_id")
            if any(not rec.get("folder_uuid") and rec.get("effective_rgid") for rec in group):
                issue_types.append("missing_release_group_id_stamp")
            if issue_types:
                candidate_groups.append((group, issue_types))

        for group in rgid_groups.values():
            if len(group) > 1:
                candidate_groups.append((group, ["same_release_group_id", "duplicate_album_folders"]))

        for group, issue_types in candidate_groups:
            key = tuple(sorted(_s(rec.get("path")) for rec in group))
            if key in seen_issue_keys:
                continue
            issue = _album_cleanup_build_issue(artist_dir, group, issue_types)
            if issue:
                seen_issue_keys.add(key)
                issues.append(issue)

        if progress and (idx == 1 or idx % 25 == 0 or idx == len(artist_dirs)):
            progress({
                "category": "Cleanup",
                "current_task": "Scanning artist folders for album-folder cleanup",
                "current_item": artist_dir.name,
                "current_path": str(artist_dir),
                "scanned_count": idx,
                "total_count": len(artist_dirs),
                "found_count": len(issues),
                "safe_count": sum(1 for issue in issues if issue.get("safe")),
                "needs_review_count": sum(1 for issue in issues if issue.get("safety") == "Needs review"),
            })

    safe_fixes = sum(1 for issue in issues if issue.get("safe"))
    needs_review = sum(1 for issue in issues if issue.get("safety") == "Needs review")
    blocked = sum(1 for issue in issues if issue.get("safety") == "Blocked")
    completed = sum(1 for issue in issues if issue.get("safety") == "Completed" or issue.get("status") == "Completed")
    empty_folders = sum(1 for issue in issues if "empty_folder" in issue.get("issue_types", []))
    duplicate_tracks = sum(int(issue.get("duplicate_tracks") or 0) for issue in issues)
    artwork_to_move = sum(int(issue.get("artwork_to_move") or 0) for issue in issues)
    missing_rgid = sum(1 for issue in issues if "missing_release_group_id_stamp" in issue.get("issue_types", []))
    placeholder_issues = sum(
        1 for issue in issues
        if "bad_folder_name" in issue.get("issue_types", [])
        or "release_id_used_instead_of_release_group_id" in issue.get("issue_types", [])
    )
    summary = {
        "scan_root": str(scan_root),
        "artist_folders_scanned": len(artist_dirs),
        "album_folders_scanned": album_folders_scanned,
        "issues_found": len(issues),
        "total_issues": len(issues),
        "safe_fixes": safe_fixes,
        "needs_review": needs_review,
        "review_needed": needs_review,
        "blocked": blocked,
        "completed": completed,
        "empty_folders": empty_folders,
        "duplicate_tracks": duplicate_tracks,
        "artwork_moved": artwork_to_move,
        "rgid_missing_stamp": missing_rgid,
        "placeholder_issues": placeholder_issues,
        "files_moved": 0,
        "folders_renamed": 0,
        "folders_deleted": 0,
        "errors": len(errors),
    }
    report = {
        "ok": True,
        "dry_run": True,
        "root": str(scan_root),
        "summary": summary,
        "final_summary": summary,
        "issues": sorted(issues, key=lambda r: (_s(r.get("artist")).casefold(), _s(r.get("album")).casefold(), _s(r.get("id")))),
        "errors": errors,
        "rollback": {
            "available": False,
            "note": "Safe apply moves duplicate/rejected files to the cleanup trash before removing empty folders.",
        },
    }
    if progress:
        progress({
            "category": "Cleanup",
            "current_task": "Album folder cleanup scan complete",
            "current_item": None,
            "current_path": None,
            "scanned_count": len(artist_dirs),
            "total_count": len(artist_dirs),
            "found_count": len(issues),
            "safe_count": safe_fixes,
            "needs_review_count": needs_review,
            "blocked_count": blocked,
            "empty_folder_count": empty_folders,
            "duplicate_track_count": duplicate_tracks,
            "current_result": f"{len(issues)} issue(s): {safe_fixes} safe, {needs_review} need review, {blocked} blocked",
            "final_summary": summary,
        })
    return report


def _album_cleanup_quality_tuple(path: Path, info: Optional[Dict[str, Any]] = None) -> Tuple[int, int, int]:
    metadata = info or {}
    ext = path.suffix.lower()
    lossless = 1 if ext in _ALBUM_CLEANUP_LOSSLESS_EXTS or _s(metadata.get("format")).lower() in {"flac", "alac", "wav", "aiff", "ape", "wv"} else 0
    bitrate = int(float(metadata.get("bitrate") or 0))
    try:
        size = int(path.stat().st_size)
    except Exception:
        size = int(metadata.get("size") or 0)
    return (lossless, bitrate, size)


def _album_cleanup_remove_empty_tree(folder: Path, log: List[str]) -> int:
    candidates: List[Path] = []
    try:
        if folder.exists() and folder.is_dir() and not folder.is_symlink():
            child_dirs = [p for p in folder.rglob("*") if p.is_dir() and not p.is_symlink()]
            candidates.extend(sorted(child_dirs, key=lambda p: len(p.parts), reverse=True))
            candidates.append(folder)
    except Exception:
        candidates = [folder]

    removed = 0
    seen: set = set()
    for candidate in candidates:
        key = _s(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            plan_res = composite_workflows.plan_folder_cleanup({"action": "remove_empty", "source": key})
        except (BeetsUnavailableError, BeetsError) as ex:
            log.append(f"  SKIP empty-folder cleanup; engine rejected {candidate}: {ex}")
            continue
        if not plan_res.get("ok") or int(plan_res.get("removals_count") or 0) <= 0:
            continue
        op_id = _s(plan_res.get("operation_id")).strip()
        if not op_id:
            continue
        try:
            apply_res = composite_workflows.apply_folder_cleanup(op_id)
        except (BeetsUnavailableError, BeetsError) as ex:
            log.append(f"  SKIP empty-folder cleanup; engine apply failed for {candidate}: {ex}")
            continue
        if apply_res.get("ok") and apply_res.get("mutated"):
            removed_dirs = apply_res.get("removed_dirs") or [key]
            removed += len(removed_dirs)
            for removed_dir in removed_dirs:
                log.append(f"  Removed empty album folder via engine: {removed_dir}")
    if removed == 0:
        log.append("  SKIP empty-folder candidate that is not empty.")
    return removed


def _album_cleanup_file_info(path: Path) -> Dict[str, Any]:
    try:
        stat = path.stat()
        size = int(stat.st_size)
    except Exception:
        size = 0
    ext = path.suffix.lower()
    return {
        "path": str(path),
        "relative_path": path.name,
        "size": size,
        "is_audio": ext in AUDIO_EXT,
        "is_artwork": ext in _ART_EXTS,
    }


def _album_cleanup_apply_plan(issue: Dict[str, Any], scan_root: Path) -> Dict[str, Any]:
    canonical_raw = _s(issue.get("canonical_folder") or issue.get("proposed_canonical_folder")).strip()
    current_folders = [_s(p).strip() for p in issue.get("current_folders") or [] if _s(p).strip()]
    blockers: List[str] = []
    actions: List[Dict[str, Any]] = []
    if not canonical_raw:
        blockers.append("target folder missing")
        return {"safe": False, "blockers": blockers, "actions": actions}
    canonical, canonical_error = _album_cleanup_trusted_path(
        canonical_raw,
        scan_root,
        expected_type="dir",
        require_exists=False,
        allow_missing_leaf=True,
        reject_root=True,
    )
    if canonical_error or canonical is None:
        blockers.append(canonical_error or "target folder outside library")
        return {"safe": False, "blockers": blockers, "actions": actions}
    parent, parent_error = _album_cleanup_trusted_path(
        str(canonical.parent),
        scan_root,
        expected_type="dir",
        require_exists=True,
        allow_missing_leaf=False,
        reject_root=False,
    )
    if parent_error or parent is None:
        blockers.append(parent_error or "target parent folder missing")
        return {"safe": False, "blockers": blockers, "actions": actions}

    if "empty_folder" in (issue.get("issue_types") or []):
        for folder_raw in current_folders:
            folder, folder_error = _album_cleanup_trusted_path(
                folder_raw,
                scan_root,
                expected_type="dir",
                require_exists=True,
                reject_root=True,
            )
            if folder_error or folder is None:
                blockers.append(folder_error or "source folder outside library")
            elif _folder_cleanup_db_items(folder):
                blockers.append("source folder contains DB-tracked items")
            elif _album_cleanup_file_inventory(folder):
                blockers.append("unknown leftover files")
            else:
                actions.append({"action": "remove_empty_folder", "source": str(folder)})
        return {"safe": not blockers, "blockers": blockers, "actions": actions}

    reserved: set = set()
    if canonical.exists() and canonical.is_dir():
        reserved = set(_album_cleanup_file_inventory(canonical).keys())

    for folder_raw in current_folders:
        source, source_error = _album_cleanup_trusted_path(
            folder_raw,
            scan_root,
            expected_type="dir",
            require_exists=True,
            reject_root=True,
        )
        if source_error or source is None:
            blockers.append(source_error or "source folder outside library")
            continue
        if source == canonical:
            continue
        try:
            source_files = []
            for candidate in source.rglob("*"):
                if candidate.is_symlink():
                    blockers.append("source folder contains symlink components")
                    continue
                if not candidate.is_file():
                    continue
                safe_file, file_error = _album_cleanup_trusted_path(
                    str(candidate),
                    scan_root,
                    expected_type="file",
                    require_exists=True,
                    reject_root=True,
                )
                if file_error or safe_file is None:
                    blockers.append(file_error or "source file outside library")
                    continue
                if not _path_is_under(safe_file, source):
                    blockers.append("source file outside source folder")
                    continue
                source_files.append(safe_file)
        except Exception as exc:
            blockers.append(f"filesystem operation failed: {type(exc).__name__}")
            continue
        for src_file in sorted(source_files, key=lambda p: p.as_posix().casefold()):
            ext = src_file.suffix.lower()
            rel = src_file.relative_to(source)
            rel_text = rel.as_posix()
            dst = canonical / rel
            source_info = _album_cleanup_file_info(src_file)
            if ext in AUDIO_EXT:
                if dst.exists():
                    target_info = _album_cleanup_file_info(dst)
                    if _album_cleanup_verified_same_file(source_info, target_info):
                        actions.append({
                            "action": "quarantine_duplicate",
                            "source": str(src_file),
                            "target": str(dst),
                            "relative_path": rel_text,
                            "reason": "verified duplicate audio",
                        })
                    else:
                        blockers.append(f"target file exists but differs: {rel_text}")
                    continue
                reserved.add(rel_text)
                actions.append({"action": "move_audio", "source": str(src_file), "target": str(dst), "relative_path": rel_text})
                continue
            if ext in _ART_EXTS:
                if dst.exists():
                    target_info = _album_cleanup_file_info(dst)
                    if _album_cleanup_verified_same_file(source_info, target_info):
                        actions.append({
                            "action": "quarantine_duplicate",
                            "source": str(src_file),
                            "target": str(dst),
                            "relative_path": rel_text,
                            "reason": "verified duplicate artwork",
                        })
                        continue
                    target_rel = _album_cleanup_safe_artwork_relative(rel_text, reserved)
                    dst = canonical / Path(target_rel)
                else:
                    reserved.add(rel_text)
                actions.append({"action": "move_artwork", "source": str(src_file), "target": str(dst), "relative_path": rel_text})
                continue
            blockers.append(f"unknown leftover files: {rel_text}")

    return {"safe": not blockers, "blockers": blockers, "actions": actions}


def _album_cleanup_issue_identity_blockers(issue: Dict[str, Any]) -> List[str]:
    blockers: List[str] = []
    issue_types = {
        _s(value).strip().casefold()
        for value in (issue.get("issue_types") or [])
        if _s(value).strip()
    }
    if not _album_cleanup_valid_rgid(issue.get("release_group_id")):
        blockers.append("Release Group identity is required before album cleanup.")
    if _s(issue.get("safety") or issue.get("risk_level") or issue.get("status")).strip().casefold() == "blocked":
        blockers.append("Blocked album cleanup issues cannot be applied.")
    if any("conflict" in value for value in issue_types):
        blockers.append("Conflicting album identity requires manual review.")
    return list(dict.fromkeys(blockers))


def _album_cleanup_item_id_for_path(path: Path) -> int:
    """Resolve the authoritative Beets item id for an exact on-disk path,
    or 0 if the path is not a Beets-tracked item at all.

    SEC-002 / ARCH-003 final closure review, findings #6/#8: engine-owned
    `album_maintenance_v1` requires a real, positive item id and
    independently reloads that row by id -- it never trusts a caller's
    path alone. Passing `item_id=0` (as the previous version of this
    function did) cannot bind to any row, so Apply never actually
    touches anything, which is why the local fallback below always used
    to run in practice."""
    try:
        item = composite_workflows.find_item_by_path(str(path))
        return int(item.get("id") or 0) if item else 0
    except Exception:
        return 0


def _album_cleanup_apply_issue(issue: Dict[str, Any], scan_root: Path, trash_root: Path,
                               log: List[str], summary: Dict[str, Any],
                               operations: List[Dict[str, Any]],
                               verbose_files: bool = True) -> Dict[str, Any]:
    plan = _album_cleanup_apply_plan(issue, scan_root)
    blockers = _album_cleanup_issue_identity_blockers(issue) + list(plan.get("blockers") or [])
    if blockers:
        reason = blockers[0]
        log.append(f"  BLOCKED: {reason}")
        blocked_issue = dict(issue)
        blocked_issue.update({
            "safe": False,
            "safety": "Blocked",
            "risk_level": "Blocked",
            "status": "Blocked",
            "blocking_reasons": blockers,
            "risk_reason": f"Blocked: {reason}.",
        })
        summary["blocked"] = int(summary.get("blocked") or 0) + 1
        return blocked_issue

    changed = 0
    issue_errors: List[str] = []
    canonical_raw = _s(issue.get("canonical_folder") or issue.get("proposed_canonical_folder")).strip()
    canonical, canonical_error = _album_cleanup_trusted_path(
        canonical_raw,
        scan_root,
        expected_type="dir",
        require_exists=False,
        allow_missing_leaf=True,
        reject_root=True,
    ) if canonical_raw else (None, "target folder missing")
    if canonical_error or canonical is None:
        issue_errors.append(canonical_error or "target folder missing")
    else:
        try:
            parent, parent_error = _album_cleanup_trusted_path(
                str(canonical.parent),
                scan_root,
                expected_type="dir",
                require_exists=True,
                allow_missing_leaf=False,
                reject_root=False,
            )
            if parent_error or parent is None:
                raise RuntimeError(parent_error or "target parent folder missing")
            if _path_has_symlink_component_under(canonical, scan_root.resolve(strict=False)):
                raise RuntimeError("target folder contains symlink components")
        except Exception as exc:
            issue_errors.append(f"target folder missing: {type(exc).__name__}")

    if not issue_errors:
        for action in plan.get("actions") or []:
            kind = _s(action.get("action"))
            if kind == "remove_empty_folder":
                folder, folder_error = _album_cleanup_trusted_path(
                    action.get("source"),
                    scan_root,
                    expected_type="dir",
                    require_exists=True,
                    reject_root=True,
                )
                if folder_error or folder is None:
                    issue_errors.append(folder_error or "source folder outside library")
                    break
                # SEC-002 / ARCH-003 final closure review, finding #4: no
                # local `_album_cleanup_remove_empty_tree` fallback -- an
                # engine Plan/Apply failure is a real error, not a signal
                # to mutate the filesystem locally instead.
                try:
                    plan_res = composite_workflows.plan_folder_cleanup({"action": "remove_empty", "source": str(folder)})
                except (BeetsUnavailableError, BeetsError) as ex:
                    issue_errors.append(f"engine unreachable: {ex}")
                    break
                if not plan_res.get("ok"):
                    issue_errors.append(plan_res.get("error") or "folder cleanup plan rejected")
                    break
                op_id = plan_res.get("operation_id")
                if not op_id:
                    continue  # nothing eligible to remove; not an error
                try:
                    apply_res = composite_workflows.apply_folder_cleanup(op_id)
                except (BeetsUnavailableError, BeetsError) as ex:
                    issue_errors.append(f"engine unreachable: {ex}")
                    break
                if not apply_res.get("ok"):
                    issue_errors.append(apply_res.get("error") or "folder cleanup apply failed")
                    break
                summary["folders_deleted"] += 1
                changed += 1
                operations.append({"action": kind, "path": str(folder), "folders_deleted": 1})
                continue

            src, src_error = _album_cleanup_trusted_path(
                action.get("source"),
                scan_root,
                expected_type="file",
                require_exists=True,
                reject_root=True,
            )
            if src_error or src is None:
                issue_errors.append(src_error or "source file outside library")
                break
            try:
                if kind == "quarantine_duplicate":
                    target, target_error = _album_cleanup_trusted_path(
                        action.get("target"),
                        scan_root,
                        expected_type="file",
                        require_exists=True,
                        reject_root=True,
                    )
                    if target_error or target is None:
                        raise RuntimeError(target_error or "target file missing")
                    if not _album_cleanup_verified_same_file(
                        _album_cleanup_file_info(src),
                        _album_cleanup_file_info(target),
                    ):
                        raise RuntimeError("duplicate file is no longer identical")
                    aid = int(issue.get("album_id") or 0)
                    # SEC-002 / ARCH-003 final closure review, findings
                    # #5/#6: album_maintenance_v1 requires a real,
                    # positive item id bound to an authoritative Beets
                    # row -- id=0 cannot bind to anything, so Apply never
                    # actually quarantined the file and the local
                    # shutil.move fallback below always ran in practice.
                    # Resolve the real id; a duplicate file with no
                    # tracked Beets row is out of this family's scope and
                    # is blocked rather than mutated by any local path.
                    src_item_id = _album_cleanup_item_id_for_path(src)
                    if src_item_id <= 0:
                        raise RuntimeError("duplicate file is not a tracked Beets item")
                    try:
                        plan_res = composite_workflows.plan_album_maintenance({
                            "mode": "deduplicate",
                            "album_id": aid,
                            "to_delete": [{"id": src_item_id, "path": str(src)}],
                        })
                    except (BeetsUnavailableError, BeetsError) as ex:
                        raise RuntimeError(f"engine unreachable: {ex}")
                    if not plan_res.get("ok"):
                        raise RuntimeError(plan_res.get("error") or "duplicate cleanup plan rejected")
                    op_id = plan_res.get("operation_id")
                    if not op_id:
                        raise RuntimeError("duplicate cleanup plan produced nothing actionable")
                    try:
                        apply_res = composite_workflows.apply_album_maintenance(op_id)
                    except (BeetsUnavailableError, BeetsError) as ex:
                        raise RuntimeError(f"engine unreachable: {ex}")
                    if not apply_res.get("ok"):
                        raise RuntimeError(apply_res.get("error") or "duplicate cleanup apply failed")
                    summary["duplicate_files_quarantined"] += 1
                    changed += 1
                    if verbose_files:
                        log.append(f"  Quarantined duplicate (engine controlled): {src}")
                    operations.append({"action": kind, "source": str(src), "quarantined": "engine_quarantine", "target": str(target)})
                    continue

                dst, dst_error = _album_cleanup_trusted_destination(action.get("target"), scan_root, canonical)
                if dst_error or dst is None:
                    raise RuntimeError(dst_error or "target file outside approved folder")
                if dst.exists():
                    raise RuntimeError("target file exists")
                if _path_has_symlink_component_under(dst, canonical, include_leaf=False):
                    raise RuntimeError("target parent contains symlink components")

                is_artwork = (kind == "move_artwork" or dst.suffix.lower() in _ART_EXTS)
                aid = int(issue.get("album_id") or 0)
                # SEC-002 / ARCH-003 final closure review, findings
                # #8/#10/#11/#12: filename_cleanup was called with
                # item_id=0 (cannot bind to any row, so Apply never
                # moved anything) and artwork "move" was called without
                # the now-required `target_dir`, so Plan always rejected
                # it -- both meant `engine_handled` stayed False and the
                # local shutil.move + `_album_cleanup_update_db_path`
                # path below always ran in practice. Fixed: artwork gets
                # the real target directory (`canonical`, already
                # resolved above); filename_cleanup resolves the real
                # item id. Engine failure now fails closed -- no local
                # fallback.
                if is_artwork and aid > 0:
                    try:
                        plan_res = composite_workflows.plan_album_artwork({
                            "mode": "move",
                            "album_id": aid,
                            "target_dir": str(canonical),
                            "candidates": [{"source": str(src)}],
                        })
                    except (BeetsUnavailableError, BeetsError) as ex:
                        raise RuntimeError(f"engine unreachable: {ex}")
                    if not plan_res.get("ok"):
                        raise RuntimeError(plan_res.get("error") or "artwork move plan rejected")
                    op_id = plan_res.get("operation_id")
                    if not op_id:
                        raise RuntimeError("artwork move plan produced nothing actionable")
                    try:
                        apply_res = composite_workflows.apply_album_artwork(op_id)
                    except (BeetsUnavailableError, BeetsError) as ex:
                        raise RuntimeError(f"engine unreachable: {ex}")
                    if not apply_res.get("ok"):
                        raise RuntimeError(apply_res.get("error") or "artwork move apply failed")
                    summary["files_moved"] += 1
                    summary["artwork_moved"] += 1
                    changed += 1
                    operations.append({"action": kind, "source": str(src), "target": str(dst)})
                    continue

                src_item_id = _album_cleanup_item_id_for_path(src)
                if src_item_id <= 0:
                    raise RuntimeError("file is not a tracked Beets item")
                try:
                    plan_res = composite_workflows.plan_album_maintenance({
                        "mode": "filename_cleanup",
                        "candidates": [{"item_id": src_item_id, "source": str(src), "destination": str(dst)}],
                    })
                except (BeetsUnavailableError, BeetsError) as ex:
                    raise RuntimeError(f"engine unreachable: {ex}")
                if not plan_res.get("ok"):
                    raise RuntimeError(plan_res.get("error") or "filename cleanup plan rejected")
                op_id = plan_res.get("operation_id")
                if not op_id:
                    raise RuntimeError("filename cleanup plan produced nothing actionable")
                try:
                    apply_res = composite_workflows.apply_album_maintenance(op_id)
                except (BeetsUnavailableError, BeetsError) as ex:
                    raise RuntimeError(f"engine unreachable: {ex}")
                if not apply_res.get("ok"):
                    raise RuntimeError(apply_res.get("error") or "filename cleanup apply failed")
                summary["files_moved"] += 1
                if is_artwork:
                    summary["artwork_moved"] += 1
                changed += 1
                if verbose_files:
                    log.append(f"  Moved (engine controlled): {src} -> {dst}")
                operations.append({"action": kind, "source": str(src), "target": str(dst)})
            except Exception as exc:
                issue_errors.append(f"{src}: {type(exc).__name__}")
                log.append(f"  ERROR applying {kind}: {type(exc).__name__}")
                break

    # SEC-002 / ARCH-003 final closure review: this ancestor-folder
    # cleanup walk previously called local directory removal directly, a local
    # filesystem mutation outside engine control. Each level now goes
    # through folder_cleanup_v1's "remove_empty" action instead; the
    # walk-up-until-non-empty semantics are preserved in app.py (pure
    # traversal, not mutation), but the actual removal is engine-owned.
    for folder_raw in issue.get("current_folders") or []:
        source, source_error = _album_cleanup_trusted_path(
            folder_raw,
            scan_root,
            expected_type="dir",
            require_exists=False,
            reject_root=True,
        )
        if source_error or source is None or source == canonical:
            continue
        stop_res = scan_root.resolve(strict=False)
        current = source
        while current.exists() and _path_under(current, stop_res) and current != stop_res:
            if current.is_symlink() or _path_has_symlink_component_under(current, stop_res):
                break
            try:
                plan_res = composite_workflows.plan_folder_cleanup({"action": "remove_empty", "source": str(current)})
            except (BeetsUnavailableError, BeetsError):
                break
            op_id = plan_res.get("operation_id")
            if not plan_res.get("ok") or not op_id:
                break
            try:
                apply_res = composite_workflows.apply_folder_cleanup(op_id)
            except (BeetsUnavailableError, BeetsError):
                break
            if not apply_res.get("ok"):
                break
            log.append(f"  Removed empty folder (engine controlled): {current}")
            summary["folders_deleted"] += 1
            current = current.parent

    if issue_errors:
        summary["errors"] += len(issue_errors)
        blocked_issue = dict(issue)
        blocked_issue.update({
            "safe": False,
            "safety": "Blocked",
            "risk_level": "Blocked",
            "status": "Blocked",
            "blocking_reasons": issue_errors,
            "risk_reason": f"Blocked: {issue_errors[0]}.",
        })
        return blocked_issue

    completed_issue = dict(issue)
    completed_issue.update({
        "safe": False,
        "safety": "Completed",
        "risk_level": "Completed",
        "status": "Completed",
        "completed": True,
        "changed_count": changed,
        "risk_reason": "Completed.",
    })
    summary["completed"] = int(summary.get("completed") or 0) + 1
    return completed_issue


_ALBUM_CLEANUP_BATCH_SIZE = 75


_ALBUM_CLEANUP_MAX_BATCHES = 100


def _album_folder_cleanup_apply_safe(root: Optional[Path], log: List[str],
                                     cancel_event: Optional[Any] = None,
                                     progress: Optional[Any] = None,
                                     verbose_files: bool = True,
                                     batch_size: int = _ALBUM_CLEANUP_BATCH_SIZE) -> Dict[str, Any]:
    # Runs in batches of `batch_size` safe issues, re-scanning (re-planning)
    # before every batch instead of applying one big plan computed up front.
    # A stale single plan is what let earlier merges in the same run leave
    # later "safe" issues pointing at folders that had already been
    # consolidated/removed (surfaced live as safe issues failing at apply
    # time with "source folder missing"/"unknown leftover files"); a fresh
    # re-plan each batch also picks up any group that only became safe
    # because an earlier batch resolved what was blocking it.
    scan_root = (root or MUSIC_ROOT).resolve(strict=False)
    trash_root = METADATA_CACHE_ROOT / "album-folder-cleanup-trash" / time.strftime("%Y%m%d-%H%M%S")
    summary: Dict[str, Any] = {
        "dry_run": False,
        "safe_issues_selected": 0,
        "files_moved": 0,
        "artwork_moved": 0,
        "duplicate_files_quarantined": 0,
        "folders_renamed": 0,
        "folders_deleted": 0,
        "db_paths_updated": 0,
        "completed": 0,
        "blocked": 0,
        "errors": 0,
    }
    operations: List[Dict[str, Any]] = []
    errors: List[str] = []
    seen_scan_errors: set = set()
    applied_issues: List[Dict[str, Any]] = []
    last_plan_summary: Dict[str, Any] = {}
    total_applied = 0
    batch_num = 0

    while True:
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("cancelled")
        plan = _album_folder_cleanup_plan(root, progress=progress, cancel_event=cancel_event)
        last_plan_summary = dict(plan.get("summary") or {})
        for err in plan.get("errors") or []:
            err_s = _s(err)
            if err_s and err_s not in seen_scan_errors:
                seen_scan_errors.add(err_s)
                errors.append(err_s)
        issues = [issue for issue in plan.get("issues", []) if issue.get("safe")]
        if not issues:
            break
        batch_num += 1
        if batch_num > _ALBUM_CLEANUP_MAX_BATCHES:
            log.append(
                f"[batch] stopping after {_ALBUM_CLEANUP_MAX_BATCHES} batch(es): "
                f"{len(issues)} more safe issue(s) remain for the next run"
            )
            break
        batch = issues[:batch_size]
        log.append(f"[batch {batch_num}] re-scanned: {len(issues)} safe issue(s) found, applying {len(batch)}")

        for offset, issue in enumerate(batch, start=1):
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("cancelled")
            total_applied += 1
            log.append(f"[batch {batch_num} #{offset}/{len(batch)}] {issue.get('artist')} - {issue.get('album')}")
            log.append(f"  Proposed action: {issue.get('proposed_action')}")
            if issue.get("release_group_id"):
                rgid_label = "Inferred Release Group ID" if issue.get("release_group_inference") else "Release Group ID"
                log.append(f"  {rgid_label}: {issue.get('release_group_id')}")
            if issue.get("risk_reason"):
                log.append(f"  {issue.get('risk_reason')}")
            if progress:
                progress({
                    "category": "Cleanup",
                    "current_task": f"Applying safe album folder cleanup (batch {batch_num})",
                    "current_item": f"{issue.get('artist')} - {issue.get('album')}",
                    "scanned_count": total_applied,
                    "current_result": _s(issue.get("proposed_action")),
                })
            applied_issue = _album_cleanup_apply_issue(
                issue, scan_root, trash_root, log, summary, operations,
                verbose_files=verbose_files,
            )
            applied_issues.append(applied_issue)
            if applied_issue.get("status") == "Blocked":
                errors.extend(_s(reason) for reason in applied_issue.get("blocking_reasons") or [])

        if operations:
            _invalidate_lib_cache()

    summary["safe_issues_selected"] = total_applied
    summary["batches"] = batch_num
    # Post-run remaining counts from the final re-scan, kept separate from
    # "blocked" above (which only counts previously-safe issues that failed
    # at apply time) so the two don't get summed into a confusing total.
    summary["needs_review_remaining"] = int(
        last_plan_summary.get("needs_review") or last_plan_summary.get("review_needed") or 0
    )
    summary["scan_blocked_remaining"] = int(last_plan_summary.get("blocked") or 0)
    summary["safe_remaining"] = int(last_plan_summary.get("safe_fixes") or 0)

    report = {
        "ok": True,
        "dry_run": False,
        "root": str(scan_root),
        "batches": batch_num,
        "summary": summary,
        "final_summary": summary,
        "issues": applied_issues,
        "operations": operations,
        "errors": errors,
        "rollback": {
            "available": False,
            "trash_root": str(trash_root) if trash_root.exists() else "",
            "note": "Duplicate/rejected files were moved to cleanup trash instead of being permanently deleted.",
        },
    }
    if progress:
        progress({
            "category": "Cleanup",
            "current_task": "Safe album folder cleanup complete",
            "current_item": None,
            "current_path": None,
            "scanned_count": total_applied,
            "total_count": total_applied,
            "changed_count": int(summary.get("files_moved") or 0) + int(summary.get("folders_deleted") or 0),
            "error_count": int(summary.get("errors") or 0),
            "current_result": (
                f"{summary.get('files_moved', 0)} file(s) moved, "
                f"{summary.get('duplicate_files_quarantined', 0)} duplicate(s) quarantined"
            ),
            "final_summary": summary,
        })
    return report
