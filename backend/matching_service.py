"""Application adapters over backend.matching: preflight and album/track alignment (ARCH-001).

No new matching policy lives here; final decisions stay in backend.matching.
"""

from __future__ import annotations

import backend.provider_boundary as provider_boundary
import copy, difflib, json, math, os, re, threading, time
import urllib.error
from backend.matching import AcoustIDStatus, album_track_score as _canonical_album_track_score, best_album_track_match as _canonical_best_album_track_match, normalize_artist as _canonical_normalize_artist, similarity as _canonical_similarity, track_feature_variants as _canonical_track_feature_variants, track_parenthetical_alias_variants as _canonical_track_parenthetical_alias_variants
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from backend.app_runtime import _app_logger, AUDIO_EXT, DOWNLOADS_ROOT, MUSIC_ROOT, _MB_RELEASE_TRACKLIST_CACHE, _MB_RELEASE_TRACKLIST_CACHE_DIR, _MB_RELEASE_TRACKLIST_CACHE_LOCK, _MB_RELEASE_TRACKLIST_CACHE_TTL, _MB_RELEASE_TRACKLIST_DISK_CACHE_TTL, _MB_TRACK_PREFLIGHT_MATCH_THRESHOLD, _MB_TRACK_REPAIR_MATCH_THRESHOLD, _MB_UUID_RE, _is_valid_mb_uuid, _s, _ur
from backend.app_runtime import _path_is_under, _redact_security_text, _safe_inventory_error_message
from backend.album_match import build_album_match_plan
from backend.mb_alignment import summarize_mb_track_alignment
from backend.matching_contract import build_album_matching_decision
from backend.title_normalize import restore_time_colon_title as _restore_time_colon_title
from helpers_mb import _resolve_release_group_to_release, acoustid_api_key
from backend.beets_adapter import BeetsAdapter, BeetsUnavailableError, beets_adapter
import backend.composite_workflows as composite_workflows
from backend.library_cache import library_cache
from backend.acoustid_service import _acoustid_multi_file, _album_item_abs_path, _album_track_fingerprint_check, _album_track_norm, _normalize_albumartist
from backend.artwork_service import _get_album_item_dir
from backend.slskd_service import _slskd_title_guess_from_name, _slskd_track_numbers_from_name, _strip_track_filename_id_suffix
from backend.plex_service import _trigger_plex_refresh

# ── ARCH-001 extracted code ──


def _preflight_match_ratio(preflight: Optional[Dict[str, Any]]) -> float:
    if not preflight:
        return 0.0
    return float(preflight.get("matches") or 0) / max(1, int(preflight.get("expected") or 0))


def _preflight_oversized_subset_complete(preflight: Dict[str, Any]) -> bool:
    """True when a small source folder cleanly matches part of an oversized MB release."""
    preflight = preflight or {}
    audio_count = int(preflight.get("audio_count") or 0)
    expected = int(preflight.get("expected") or 0)
    matches = int(preflight.get("matches") or 0)
    if audio_count < 6 or expected <= audio_count + max(3, audio_count // 2):
        return False
    if not bool(preflight.get("artist_ok", True)):
        return False
    # SEC-002 Wave 14: this is an independent tracklist-ratio heuristic that
    # callers (_resolve_album_release_for_import's _source_accepts_release/
    # _same_group_source_sized_release) use to accept a release candidate
    # WITHOUT checking preflight["ok"] at all -- so it must not itself
    # become a way to bypass the shared MatchingDecision authority. When a
    # matching_decision was computed and explicitly marks the candidate
    # action_allowed=False (unresolved Release Group ID, identity
    # conflict, ...), an oversized-subset track-count match alone is not
    # sufficient.
    matching_decision = preflight.get("matching_decision")
    if isinstance(matching_decision, dict) and matching_decision and not matching_decision.get("action_allowed", True):
        return False
    return matches >= min(audio_count, max(6, int(math.ceil(audio_count * 0.90))))


def _preflight_oversized_subset_summary(preflight: Optional[Dict[str, Any]]) -> str:
    preflight = preflight or {}
    audio_count = int(preflight.get("audio_count") or 0)
    expected = int(preflight.get("expected") or 0)
    matches = int(preflight.get("matches") or 0)
    release_title = _s(preflight.get("release_title")).strip()
    release_artist = _s(preflight.get("release_artist")).strip()
    release_name = " - ".join([v for v in (release_artist, release_title) if v])
    label = f" ({release_name})" if release_name else ""
    return (
        f"MusicBrainz release{label} looks like an oversized edition: "
        f"{matches}/{audio_count or '?'} local source track(s) matched, "
        f"but the release has {expected or '?'} track(s)."
    )


def _preflight_tracklist_gate_ok(matches: int, min_required: int,
                                 artist_gate: bool, too_many_extras: bool,
                                 oversized_subset: bool) -> bool:
    """Shared pass/fail gate for selected MusicBrainz release preflight."""
    if not artist_gate or too_many_extras:
        return False
    return bool(oversized_subset) or int(matches or 0) >= int(min_required or 0)


def _repair_album_mbid_sticking_once(album_id: int, mb_albumid: str,
                                     log: Optional[List[str]] = None, *,
                                     repair_tracks: bool = True,
                                     write_tags: bool = False,
                                     cancel_event=None) -> Dict[str, Any]:
    """Repair MB release/recording IDs for one already-validated album."""
    try:
        aid = int(album_id or 0)
    except Exception:
        aid = 0
    mbid = _s(mb_albumid).strip().lower()
    summary = {
        "album_id": aid,
        "changed": False,
        "release_item_rows": 0,
        "track_rows": 0,
        "write_returncode": None,
    }
    if aid <= 0 or not _MB_UUID_RE.match(mbid):
        return summary

    album_changed = False
    if repair_tracks:
        try:
            plan_res = composite_workflows.plan_album_mb_track_repair({"album_id": aid, "mb_albumid": mbid})
            if plan_res.get("ok"):
                op_id = plan_res.get("operation_id")
                updated_count = int(plan_res.get("updated") or 0)
                # SEC-002 Wave 19 final review: a Plan with zero recording-ID
                # repairs can still have pending release-only stamping (the
                # engine no longer returns a "release_rows_updated" field on
                # Plan at all -- that check always silently discarded a real,
                # unapplied transaction). Apply whenever the engine says
                # there is anything to do, not just when updated_count > 0.
                needs_apply = bool(op_id) and (
                    updated_count > 0
                    or bool(plan_res.get("release_stamping_needed"))
                    or int(plan_res.get("release_stamp_rows") or 0) > 0
                )
                if needs_apply:
                    apply_res = composite_workflows.apply_album_mb_track_repair(op_id, write_tags=write_tags)
                    if apply_res.get("ok"):
                        summary["track_rows"] = updated_count
                        summary["release_item_rows"] = int(apply_res.get("release_stamp_rows") or 0)
                        album_changed = True
                        if log is not None:
                            log.append(
                                f"  [mbid] Auto-repaired {updated_count} recording ID(s), "
                                f"stamped release ID on {summary['release_item_rows']} row(s) for album_id {aid}."
                            )
        except Exception as ex:
            if log is not None:
                log.append(f"  [mbid] WARN auto recording-ID repair skipped for album_id {aid}: {ex}")

    summary["changed"] = bool(album_changed)
    return summary


# ── Discography / Wanted ──────────────────────────────────────────────────────

def _normalize_album(s: str) -> str:
    """Normalise album title for fuzzy disk-vs-discography comparison."""
    s = _restore_time_colon_title(s or "").lower()
    s = s.replace("&", " and ")
    # Strip common edition suffixes
    s = re.sub(r'\s*[\(\[](deluxe|remaster(?:ed)?|expanded|anniversary|super|special|bonus|explicit|clean|edition|version|mono|stereo)[^\)\]]*[\)\]]', '', s, flags=re.I)
    s = re.sub(r'\s*-\s*(deluxe|remaster(?:ed)?|expanded|anniversary|super|special|bonus|explicit|clean|mono|stereo)\s*(edition|version)?\s*$', '', s, flags=re.I)
    s = re.sub(r'\s*\(\d{4}\)\s*$', '', s)       # strip trailing year
    s = re.sub(r'[^\w\s]', ' ', s)               # punct → space
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _album_key(s: str) -> str:
    """Compact album key used for fuzzy cross-source title matching."""
    key = re.sub(r'[^a-z0-9]', '', _normalize_album(s))
    if key:
        return key
    # Punctuation-only album titles like "+", "=", "÷", and "-" are real
    # releases. Preserve a symbol key so discography/Lidarr matching does not
    # collapse them all into an empty string.
    raw = (s or "").lower().replace("&", " and ")
    raw = re.sub(r'\s*[\(\[]\d{4,8}[\)\]]\s*$', '', raw).strip()
    raw = re.sub(r'\s+', '', raw)
    return ("sym_" + urllib.parse.quote(raw, safe="")) if raw else ""


def _album_title_match(title: str, index: Dict[str, set], *, rgid: str = "") -> tuple:
    """Return (matched, reason) for a remote release title against local albums."""
    if rgid and rgid.lower() in index.get("rgids", set()):
        return True, "release-group-id"
    norm = _normalize_album(title)
    key = _album_key(title)
    if norm and norm in index.get("norms", set()):
        return True, "title"
    if key and key in index.get("keys", set()):
        return True, "title"
    if key:
        for other in index.get("keys", set()):
            if len(key) >= 8 and len(other) >= 8 and (key in other or other in key):
                return True, "fuzzy-title"
            if len(key) >= 8 and len(other) >= 8 and difflib.SequenceMatcher(None, key, other).ratio() >= 0.92:
                return True, "fuzzy-title"
    return False, ""


def _disc_number_from_path(path: str) -> int:
    try:
        parts = Path(_s(path).replace("\\", "/")).parts[:-1]
    except Exception:
        return 0
    for part in reversed(parts):
        match = re.search(
            r"(?i)(?:^|[^a-z0-9])(?:cd|disc|disk)\s*0*(\d{1,2})(?:[^a-z0-9]|$)",
            _s(part),
        )
        if match:
            try:
                return int(match.group(1))
            except Exception:
                return 0
    return 0


def _audio_position_from_path(path: str) -> tuple:
    nums = _slskd_track_numbers_from_name(Path(_s(path).replace("\\", "/")).name)
    disc, track = (nums[0] if nums else (0, 0))
    path_disc = _disc_number_from_path(path)
    if path_disc and (not disc or disc == 1):
        disc = path_disc
    return max(int(disc or 1), 1), max(int(track or 0), 0)


def _is_configured_root(path: Path) -> bool:
    """True when ``path`` is the music library, the downloads mount, a staging
    root or a configured download mount (playlist, torrent, qBittorrent)."""
    from backend.app_runtime import PLAYLIST_DOWNLOAD_ROOT, QBIT_REPAIR_ALLOWED_ROOTS, TORRENT_SOURCE_ROOTS
    roots = (MUSIC_ROOT, DOWNLOADS_ROOT, PLAYLIST_DOWNLOAD_ROOT, *TORRENT_SOURCE_ROOTS,
             *QBIT_REPAIR_ALLOWED_ROOTS, *composite_workflows._get_staging_roots())
    here = os.path.realpath(str(path))
    return any(here == os.path.realpath(str(root)) for root in roots)


def _folder_release_preflight(folder_path: str, mb_albumid: str,
                              existing_album_id: int = 0,
                              log: Optional[list] = None) -> Dict[str, Any]:
    """Check a folder/current album against an MB release before destructive repair."""
    result: Dict[str, Any] = {
        "ok": False,
        "matches": 0,
        "expected": 0,
        "audio_count": 0,
        "examples": [],
        "release_title": "",
        "release_artist": "",
        "release_group": "",
        "folder_artist": "",
        "artist_score": 0.0,
        "artist_ok": True,
        "acoustid_release_hits": {},
        "acoustid_target_hits": 0,
        "acoustid_top_release": "",
        "acoustid_top_hits": 0,
        "acoustid_mismatch": False,
        "error": "",
        # Distinguishes "we could not even determine confidence because the
        # scan/inspection itself failed" (an infrastructure/connectivity
        # problem -- retryable) from every other preflight failure below
        # (genuine evidence gaps: low track-match ratio, artist conflict,
        # no MusicBrainz match -- correctly needs human review, not a
        # retry). Found live (Wave 26 Docker acceptance round, engine-
        # offline scenario): both cases previously landed in the same
        # terminal "review_created" status, permanently stranding a
        # transient engine-connectivity failure with no automated retry
        # path at all.
        "scan_unavailable": False,
    }
    mb = _fetch_mb_release_tracklist(mb_albumid, log)
    if not mb.get("ok"):
        result["error"] = mb.get("error") or "MusicBrainz release lookup failed"
        return result
    mb_tracks = mb.get("tracks") or []
    result["expected"] = len(mb_tracks)
    result["release_title"] = mb.get("release_title", "")
    result["release_artist"] = mb.get("release_artist", "")
    result["release_group"] = mb.get("release_group", "")
    if not mb_tracks:
        result["error"] = "MusicBrainz release has no tracks"
        return result

    source = Path(folder_path)
    # Only the Beets library has a known Artist/Album layout. Outside it the
    # parent ("batch", "FLAC", a slskd user folder) is a container, not an
    # artist, and a configured root's own name never is one either (#299 F1):
    # neither is artist evidence.
    folder_artist = _artist_folder_name_without_mbid(source.parent.name) if (
        _path_is_under(source, MUSIC_ROOT) and not _is_configured_root(source.parent)
    ) else ""
    result["folder_artist"] = folder_artist
    try:
        folder_key = _canonical_normalize_artist(folder_artist)
        release_key = _canonical_normalize_artist(result["release_artist"])
        if folder_key and release_key:
            folder_tokens = set(folder_key.split())
            release_tokens = set(release_key.split())
            score = _canonical_similarity(folder_key, release_key)
            result["artist_score"] = round(score, 3)
            result["artist_ok"] = bool(folder_tokens & release_tokens) or score >= 0.48
    except Exception:
        result["artist_ok"] = True

    audio_files: List[Path] = []
    inspect_evidence: Optional[Dict[str, Any]] = None
    try:
        # SEC-002 CodeQL repository-wide closure finding (alerts #6093/#6095):
        # this walk had no containment check at all, unlike the sibling
        # _folder_import_track_count() (fixed in Wave 1, SEC-002 main
        # backlog #334/#335) which checks the exact same pattern.
        #
        # Scoped to just this local-enumeration block (not the whole
        # function, and not aborting the preflight) -- a folder outside
        # MUSIC_ROOT/DOWNLOADS_ROOT is a normal, expected case here, not
        # necessarily an attack: this container frequently has no local
        # media mount at all (see composite_workflows.inspect_import_source's own
        # docstring), and the code a few lines below already has a real
        # fallback for exactly that case -- when the local scan finds
        # nothing, it asks the *engine* to inspect the source instead
        # (composite_workflows.inspect_import_source), which independently
        # re-validates folder_path against its own resolve_safe_path()
        # allowed-root policy on the engine side (backend/beets_control_agent.py
        # inspect_import_source()) regardless of what this function passes
        # it. Gating the local shortcut here does not weaken security --
        # it removes an unauthenticated local-disk side channel that
        # bypassed the engine's own authoritative check entirely, and lets
        # every caller safely fall through to that already-validated path.
        if (
            (_path_is_under(source, MUSIC_ROOT) or _path_is_under(source, DOWNLOADS_ROOT))
            and source.is_dir()
        ):
            audio_files = sorted(
                [p for p in source.rglob("*")
                 if p.is_file() and p.suffix.lower() in AUDIO_EXT],
                key=lambda p: str(p).lower(),
            )
    except Exception as ex:
        _app_logger.warning("Could not scan source folder locally: %s", type(ex).__name__)

    scan_exception_reason = ""
    if not audio_files and folder_path:
        try:
            inspect_res = composite_workflows.inspect_import_source(folder_path, "reimport")
            if inspect_res.get("ok"):
                inspect_evidence = inspect_res
            else:
                scan_exception_reason = _s(inspect_res.get("error") or "").strip()
        except Exception as scan_ex:
            # Previously a bare `except: pass` -- silently discarded the
            # real reason (engine offline, auth failure, timeout, path
            # rejection) and reported the same generic message for all of
            # them. Preserved now so scan_unavailable's caller can log a
            # truthful, specific reason instead of just "Could not scan".
            scan_exception_reason = str(scan_ex)

    if not audio_files and not inspect_evidence:
        result["error"] = (
            f"Could not scan source folder: {scan_exception_reason}" if scan_exception_reason
            else "Could not scan source folder."
        )
        result["scan_unavailable"] = True
        return result

    result["audio_count"] = len(inspect_evidence.get("audio_files") or []) if inspect_evidence else len(audio_files)

    acoustid_release_hits: Dict[str, int] = {}
    try:
        if audio_files:
            acoustid_release_hits = _acoustid_multi_file([str(p) for p in audio_files])
    except Exception as ex:
        if log is not None:
            log.append(f"  [preflight] AcoustID warning: {ex}")
    if acoustid_release_hits:
        target_mbid = _s(mb_albumid).strip().lower()
        target_hits = sum(
            int(hits or 0)
            for rid, hits in acoustid_release_hits.items()
            if _s(rid).strip().lower() == target_mbid
        )
        top_release, top_hits = max(
            acoustid_release_hits.items(),
            key=lambda item: int(item[1] or 0),
        )
        result.update({
            "acoustid_release_hits": acoustid_release_hits,
            "acoustid_target_hits": int(target_hits or 0),
            "acoustid_top_release": top_release,
            "acoustid_top_hits": int(top_hits or 0),
            "acoustid_mismatch": bool(
                target_mbid
                and top_release
                and _s(top_release).strip().lower() != target_mbid
                and int(target_hits or 0) == 0
            ),
        })

    candidates: List[Dict[str, Any]] = []
    seen_keys: set = set()

    def _add_candidate(title: str, path: str = "", track: int = 0,
                       disc: int = 1, mb_trackid: str = "",
                       length: float = 0.0) -> None:
        key = (_album_track_norm(title), _s(path), int(track or 0), int(disc or 1))
        if key in seen_keys:
            return
        seen_keys.add(key)
        candidates.append({
            "title": _s(title) or Path(_s(path)).stem,
            "path": _s(path),
            "track": int(track or 0),
            "disc": int(disc or 1),
            "mb_trackid": _s(mb_trackid).strip().lower(),
            "length": float(length or 0),
        })

    if inspect_evidence:
        for entry in inspect_evidence.get("audio_files") or []:
            rel_path = _s(entry.get("relative_path"))
            props = entry.get("properties") if isinstance(entry.get("properties"), dict) else {}
            disc = int(props.get("disc") or 1)
            track = int(props.get("track") or 0)
            if not track or not disc:
                path_disc, path_track = _audio_position_from_path(rel_path)
                if not track:
                    track = path_track
                if not disc or disc == 1:
                    disc = path_disc or 1
            title = _s(props.get("title")) or _slskd_title_guess_from_name(Path(rel_path).name) or Path(rel_path).stem
            mb_trackid = _s(props.get("mb_trackid"))
            length = float(props.get("length") or 0.0)
            _add_candidate(title, rel_path, track=track, disc=disc, mb_trackid=mb_trackid, length=length)
    else:
        for fpath in audio_files:
            disc, track = _audio_position_from_path(str(fpath))
            _add_candidate(
                _slskd_title_guess_from_name(fpath.name) or fpath.stem,
                str(fpath),
                track=track,
                disc=disc or 1,
            )

    if existing_album_id:
        try:
            rows = composite_workflows.find_all_items_by_album_id(int(existing_album_id))
            sorted_rows = sorted(
                rows,
                key=lambda r: (
                    int(r.get("disc") or 1),
                    int(r.get("track") or 0),
                    _s(r.get("title") or ""),
                    int(r.get("id") or 0),
                ),
            )
            for row in sorted_rows:
                _add_candidate(
                    _s(row.get("title")),
                    _s(row.get("path")),
                    track=int(row.get("track") or 0),
                    disc=int(row.get("disc") or 1),
                    mb_trackid=_s(row.get("mb_trackid")),
                    length=float(row.get("length") or 0),
                )
        except BeetsUnavailableError as ex:
            result["scan_unavailable"] = True
            result["error"] = f"Beets engine unavailable: {ex}"
            if log is not None:
                log.append(f"  [preflight] Engine unavailable reading existing album {existing_album_id}: {ex}")
            return result
        except Exception as ex:
            if log is not None:
                log.append(f"  [preflight] Existing item read warning: {ex}")

    matched_indices: set = set()
    best_lines: List[str] = []
    for item in candidates:
        best = _best_album_track_match(item, mb_tracks)
        idx = int(best.get("idx", -1))
        score = float(best.get("score") or 0.0)
        title_score = float(best.get("title_score") or 0.0)
        if idx >= 0 and (
            (best.get("exact_mbid") and title_score >= _MB_TRACK_REPAIR_MATCH_THRESHOLD)
            or (not best.get("exact_mbid") and score >= _MB_TRACK_PREFLIGHT_MATCH_THRESHOLD)
        ):
            matched_indices.add(idx)
        if len(best_lines) < 5:
            mbt = best.get("track") or {}
            best_lines.append(
                f"    {item.get('title','')!r} -> {mbt.get('title','?')!r} ({score:.0%})"
            )

    matches = len(matched_indices)
    expected = len(mb_tracks)
    min_required = max(1, min(expected, int(expected * 0.60)))
    # Do not demand 60% when importing a small missing subset into an existing album.
    if existing_album_id and audio_files and len(audio_files) < expected:
        min_required = max(1, min(len(audio_files), int(len(audio_files) * 0.60)))
    max_audio_allowed = expected + max(1, expected // 4)
    too_many_extras = (
        bool(audio_files)
        and len(audio_files) > max_audio_allowed
        and matches < max(1, int(len(audio_files) * 0.80))
    )
    if too_many_extras:
        result["error"] = (
            f"Folder has {len(audio_files)} audio file(s) but release has "
            f"{expected} track(s)"
        )
    match_ratio = matches / max(1, expected)
    source_match_ratio = matches / max(1, len(audio_files))
    # High track-match confidence overrides a failed artist check (handles artist renames, e.g. Kanye → Ye)
    artist_gate = bool(result.get("artist_ok", True)) or match_ratio >= 0.80
    oversized_subset = _preflight_oversized_subset_complete({
        **result,
        "matches": matches,
        "expected": expected,
        "audio_count": len(audio_files),
    })
    gate_ok = _preflight_tracklist_gate_ok(
        matches,
        min_required,
        artist_gate,
        too_many_extras,
        oversized_subset,
    )
    if (
        not gate_ok
        and not too_many_extras
        and audio_files
        and len(audio_files) <= 3
        and matches >= 1
        and source_match_ratio >= 1.0
    ):
        gate_ok = True
    local_album_title = ""
    local_artist_name = result.get("folder_artist") or ""
    local_rgid = ""

    # Engine ownership (SEC-002 Wave 14 final review): local evidence for the
    # album-matching decision must come from BeetsClient IPC, never from a
    # local Beets DB connection or a local media-tag read. The web manager
    # has no /data mount in the supported topology and must not depend on
    # one -- if the engine can't supply this evidence, local_album_title/
    # local_rgid simply stay unresolved and the decision below falls
    # through to review_required rather than silently reading local state.
    if existing_album_id:
        try:
            album_row = composite_workflows.get_album(existing_album_id)
        except Exception:
            album_row = None
        if album_row:
            local_album_title = _s(album_row.get("album"))
            if _s(album_row.get("albumartist")):
                local_artist_name = _s(album_row.get("albumartist"))
            if _s(album_row.get("mb_releasegroupid")):
                local_rgid = _s(album_row.get("mb_releasegroupid"))

    if not local_album_title and inspect_evidence:
        for entry in inspect_evidence.get("audio_files") or []:
            props = entry.get("properties") if isinstance(entry.get("properties"), dict) else {}
            if _s(props.get("album")):
                local_album_title = _s(props.get("album"))
            if _s(props.get("mb_releasegroupid")):
                local_rgid = _s(props.get("mb_releasegroupid"))
            if local_album_title and local_rgid:
                break

    if not local_album_title and folder_path:
        local_album_title = Path(folder_path).name

    matching_decision = build_album_matching_decision(
        current={
            "album": local_album_title,
            "artist": local_artist_name,
            "mb_releasegroupid": local_rgid,
        },
        candidate={
            "mb_releasegroupid": result.get("release_group"),
            "mb_albumid": mb_albumid,
            "album": result.get("release_title"),
            "artist": result.get("release_artist"),
        },
        items=candidates,
        mb_tracks=mb_tracks,
    )
    decision_dict = matching_decision.to_dict()
    # The old tracklist-heuristic gate_ok and the new MatchingDecision are
    # both required, not either/or: a candidate that only satisfies the
    # tracklist heuristic but that MatchingDecision marks review_required
    # (e.g. no resolved Release Group ID, or track-identity conflicts) must
    # not authorize the destructive action just because `conflicts` happens
    # to be empty. `action_allowed` (never `conflicts` alone) is the single
    # field that may ever authorize automatic/destructive action here; a
    # review-required candidate stays visible in the result (via
    # `matching_decision`) but `ok` reflects automatic-action authority
    # only.
    tracklist_ok = gate_ok
    if not decision_dict.get("action_allowed"):
        gate_ok = False

    result.update({
        "ok": gate_ok,
        "tracklist_ok": bool(tracklist_ok),
        "matches": matches,
        "min_required": min_required,
        "max_audio_allowed": max_audio_allowed,
        "too_many_extras": too_many_extras,
        "match_ratio": round(match_ratio, 3),
        "source_match_ratio": round(source_match_ratio, 3),
        "oversized_subset_complete": bool(oversized_subset),
        "examples": best_lines,
        "release_group_id": decision_dict.get("release_group_id", ""),
        "matching_decision": decision_dict,
    })
    return result


def _preflight_rejection_reason(pre: Optional[Dict[str, Any]]) -> str:
    """The real reason a folder preflight refused a release, for logs and review.

    A tracklist that matched can still be refused by the identity decision
    (artist conflict, no fingerprint evidence, ...): say that, never
    "rejected by folder tracklist: 2/2 matched"."""
    pre = pre or {}
    if pre.get("ok"):
        return ""
    if _s(pre.get("error")).strip():
        return _s(pre.get("error")).strip()
    counts = f"{pre.get('matches', 0)}/{pre.get('expected', 0)} track(s) matched"
    if not pre.get("tracklist_ok"):
        return f"folder tracklist: {counts}"
    decision = pre.get("matching_decision") or {}
    codes = [*(decision.get("conflicts") or []),
             *[w for w in (decision.get("warnings") or []) if _s(w).startswith("acoustid_")]]
    reason = (f"identity not verified "
              f"({', '.join(codes) or decision.get('reason_code') or 'review_required'}), {counts}")
    if "acoustid_unavailable" in codes:
        reason += "; no fingerprint evidence for these tracks"
        if not acoustid_api_key():
            reason += " (AcoustID not configured: set ACOUSTID_API_KEY)"
    return reason


def _preflight_review_reason(preflight: Optional[Dict[str, Any]],
                             default_reason: str) -> str:
    if not preflight:
        return default_reason

    matches = int(preflight.get("matches") or 0)
    expected = int(preflight.get("expected") or 0)
    audio_count = int(preflight.get("audio_count") or 0)
    min_required = int(preflight.get("min_required") or 0)
    release_title = _s(preflight.get("release_title")).strip()
    release_artist = _s(preflight.get("release_artist")).strip()
    folder_artist = _s(preflight.get("folder_artist")).strip()
    error = _s(preflight.get("error")).strip()

    release_label = "selected MusicBrainz release"
    if release_title or release_artist:
        release_name = " - ".join([v for v in (release_artist, release_title) if v])
        release_label = f"selected MusicBrainz release ({release_name})"

    if preflight.get("tracklist_ok") and not preflight.get("ok") and not error:
        # The tracklist passed; the identity decision refused it (F2).
        return (f"The folder tracklist matched the {release_label}, but it was refused: "
                f"{_preflight_rejection_reason(preflight)}. Review the identity "
                "evidence before importing.")

    parts: List[str] = []
    if preflight.get("oversized_subset_complete"):
        parts.append(_preflight_oversized_subset_summary(preflight))
    if expected or audio_count:
        parts.append(
            f"Rejected {release_label}: only {matches}/{expected or '?'} "
            f"release track(s) matched {audio_count} folder audio file(s)."
        )
    if min_required:
        parts.append(f"At least {min_required} matching track(s) were required.")
    if error:
        parts.append(error)
    if preflight.get("artist_ok") is False:
        artist_label = " / ".join([v for v in (folder_artist, release_artist) if v])
        if artist_label:
            parts.append(f"Artist check also failed ({artist_label}).")
        else:
            parts.append("Artist check also failed.")
    if preflight.get("acoustid_mismatch"):
        top_release = _s(preflight.get("acoustid_top_release")).strip()
        top_hits = int(preflight.get("acoustid_top_hits") or 0)
        target_hits = int(preflight.get("acoustid_target_hits") or 0)
        if top_release:
            parts.append(
                "AcoustID fingerprint mismatch: "
                f"selected release had {target_hits} hit(s), while release "
                f"{top_release} had {top_hits} hit(s)."
            )
        else:
            parts.append("AcoustID fingerprint mismatch against the selected release.")
    parts.append(
        "This looks like a mixed, mislabeled, or wrong-release folder. "
        "Choose a release whose tracklist matches the folder before retagging."
    )
    return " ".join(parts) if parts else default_reason


_disc_cache: Dict[str, Any] = {}        # artist_name → MB discography result


_disc_cache_lock = threading.Lock()


_disc_cache_discogs: Dict[str, Any] = {}    # artist → Discogs discography result (TTL: _DISC_CACHE_TTL)


_disc_cache_discogs_lock = threading.Lock()


def _invalidate_lib_cache():
    try:
        library_cache.invalidate()
    except Exception:
        pass
    try:
        with _disc_cache_lock:
            _disc_cache.clear()
    except Exception:
        pass
    try:
        with _disc_cache_discogs_lock:
            _disc_cache_discogs.clear()
    except Exception:
        pass


def _fast_album_mb_health_fields(tracks: List[Dict[str, Any]],
                                 expected_track_count: int,
                                 missing_count: int = 0) -> Dict[str, Any]:
    """Local-only MB health fields for /api/library.

    This avoids hundreds of MusicBrainz requests during a library load. Exact
    selected-release alignment still comes from /api/albums/<id>/mb-completeness.
    """
    imported_rows = [
        t for t in tracks
        if bool(t.get("ok")) and not bool(t.get("missing"))
    ]
    imported_count = len(imported_rows)
    expected = int(expected_track_count or 0)
    derived_missing = max(0, expected - imported_count) if expected else 0
    mbids = [
        _s(t.get("mb_trackid") or "").strip().lower()
        for t in imported_rows
        if _s(t.get("mb_trackid") or "").strip()
    ]
    duplicate_rows = sum(max(0, count - 1) for count in Counter(mbids).values())
    blanks = sum(
        1 for t in imported_rows
        if not _s(t.get("mb_trackid") or "").strip()
    )
    extra_count = max(0, imported_count - expected) if expected else 0
    duplicates = max(0, duplicate_rows - extra_count)
    return {
        "mb_missing_count": max(int(missing_count or 0), derived_missing),
        "extra_track_count": extra_count,
        "mb_trackid_missing_count": blanks,
        "mb_trackid_mismatch_count": 0,
        "mb_duplicate_recording_id_count": duplicates,
        "mb_repairable_count": blanks,
        "mb_health_source": "local",
    }


def _ai_api_key() -> str:
    """Resolve the outbound AI request API key from the same three
    variables the setup wizard's own `/api/setup/status` integrations
    readiness check (OPENAI_API_KEY or OPENROUTER_API_KEY or AI_API_KEY)
    already treats as equivalent "AI is configured" signals -- every real
    AI call site previously checked OPENAI_API_KEY only, so a user who set
    just OPENROUTER_API_KEY/AI_API_KEY saw "AI configured" during setup but
    every actual AI request silently failed. OPENAI_API_KEY still takes
    priority when more than one is set."""
    return (
        os.environ.get("OPENAI_API_KEY", "").strip()
        or os.environ.get("OPENROUTER_API_KEY", "").strip()
        or os.environ.get("AI_API_KEY", "").strip()
    )


def _ai_model_and_endpoint(default_model: str) -> Tuple[str, str]:
    """Resolve (model, chat-completions URL) for an outbound AI request from
    the same AI_MODEL/AI_BASE_URL environment variables the System page's
    "AI & LLM Services" section displays as live, effective settings.

    Before this, every AI call site hardcoded its own literal model string
    and "https://api.openai.com/v1/chat/completions" URL -- the System page
    could show AI_MODEL=some-other-model / AI_BASE_URL=https://openrouter.ai/...
    as "configured" while every real AI request silently ignored both and
    always talked to OpenAI's gpt-4o/gpt-4o-mini. This closes that gap
    without touching API-key resolution/gating (still OPENAI_API_KEY only,
    exactly as before) or any prompt/schema/error-handling logic.

    `default_model` preserves each call site's own historical choice
    (gpt-4o for higher-stakes album/folder matching, gpt-4o-mini for
    cheaper lookups like genre) when AI_MODEL is not set.
    """
    model = os.environ.get("AI_MODEL", "").strip() or default_model
    base_url = (os.environ.get("AI_BASE_URL", "").strip() or "https://api.openai.com/v1").rstrip("/")
    return model, f"{base_url}/chat/completions"


def _load_album_mb_suggestions() -> Dict[str, Any]:
    try:
        if _ALBUM_MB_SUGGESTIONS_FILE.exists():
            data = json.loads(_ALBUM_MB_SUGGESTIONS_FILE.read_text())
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _compact_preflight(preflight: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not preflight:
        return None
    return {
        "ok": preflight.get("ok", False),
        "matches": int(preflight.get("matches") or 0),
        "expected": int(preflight.get("expected") or 0),
        "audio_count": int(preflight.get("audio_count") or 0),
        "min_required": int(preflight.get("min_required") or 0),
        "match_ratio": round(_preflight_match_ratio(preflight), 3),
        "source_match_ratio": preflight.get("source_match_ratio", 0),
        "artist_ok": bool(preflight.get("artist_ok", True)),
        "artist_score": preflight.get("artist_score", 0),
        "too_many_extras": bool(preflight.get("too_many_extras")),
        "oversized_subset_complete": bool(preflight.get("oversized_subset_complete")),
        "acoustid_mismatch": bool(preflight.get("acoustid_mismatch")),
        "acoustid_target_hits": int(preflight.get("acoustid_target_hits") or 0),
        "acoustid_top_release": preflight.get("acoustid_top_release", ""),
        "acoustid_top_hits": int(preflight.get("acoustid_top_hits") or 0),
        "acoustid_release_hits": dict(preflight.get("acoustid_release_hits") or {}),
        "release_title": preflight.get("release_title", ""),
        "release_artist": preflight.get("release_artist", ""),
        "release_group": preflight.get("release_group", ""),
        "error": preflight.get("error", ""),
        "examples": (preflight.get("examples") or [])[:5],
        # SEC-002 Wave 14: without this, the authoritative MatchingDecision
        # computed by _folder_release_preflight is silently dropped here and
        # never reaches the Import Review auto-import gate downstream.
        "release_group_id": preflight.get("release_group_id", ""),
        "matching_decision": preflight.get("matching_decision") or {},
    }


def _album_preflight_folder(album, tracks: List[Any]) -> str:
    try:
        album_dir = _get_album_item_dir(album).strip()
        if album_dir:
            album_path = Path(album_dir)
            if not album_path.is_absolute():
                album_path = MUSIC_ROOT / album_path
            return str(album_path)
    except Exception:
        pass
    for track in tracks:
        path_text = _s(getattr(track, "path", "")).strip()
        if not path_text:
            continue
        try:
            path = Path(path_text)
            if not path.is_absolute():
                path = MUSIC_ROOT / path
            return str(path.parent)
        except Exception:
            continue
    return ""


def _match_tracks_from_mb(mb_albumid: str, album_db_id, log: list,
                          zero_unmatched: bool = False,
                          target_tracks: Optional[List[Dict[str, Any]]] = None) -> int:
    """Fetch the MB release and match each item in the album to a recording by title similarity.
    Directly updates items.mb_trackid, items.track, items.disc, items.title in the beets SQLite DB.
    If zero_unmatched=True, items that cannot be matched to any MB track have their track set to 0
    so the caller's dedup logic will flag them for deletion.
    Returns the number of items successfully matched."""
    return _match_tracks_from_mb_shared(
        mb_albumid,
        album_db_id,
        log,
        zero_unmatched,
        target_tracks=target_tracks,
    )


def _match_tracks_from_mb_shared(mb_albumid: str, album_db_id, log: list,
                                 zero_unmatched: bool = False,
                                 target_tracks: Optional[List[Dict[str, Any]]] = None) -> int:
    """Shared MB track matcher used by import/repair after preflight.

    ARCH-003 Wave 33 continuation: migrated from raw local
    UPDATE items/UPDATE albums SQL onto album_mb_track_repair_v1 (through
    composite_workflows), using the target_tracks/acoustid_verify/zero_unmatched/
    allow_establish_release_group/stamp_release_metadata options built
    this same wave specifically to close this migration, and the family's
    alignment procedure (backend/mb_alignment.greedy_album_track_alignment),
    ported verbatim from this function's own former matching loop so the
    engine and this call site can never again risk disagreeing on which
    specific track a file gets permanently relabeled as.

    acoustid_verify/allow_establish_release_group/stamp_release_metadata
    are always requested here (not opt-in like they are for other
    callers of the family): this function's own pre-migration behavior
    always ran the AcoustID cross-check unconditionally, always stamped
    mb_releasegroupid whenever the release had one (with no conflict
    check at all -- the engine's repair_identity_mismatch guard is a
    strict, real improvement over that), and always stamped year/country.

    Return-value contract preserved for this function's 6 real callers,
    3 of which check `matched < 0` and raise/abort on it: -1 means the
    MusicBrainz lookup itself genuinely failed (network/API error, no
    engine "code" at all -- the same signal this function's own MB fetch
    failure used to produce); 0-or-more means the engine ran the request
    to completion, whatever it decided (matched some tracks, decided
    there was nothing to do, or refused for a specific, named reason --
    e.g. target_tracks entirely absent from the release, a real identity
    conflict). None of app.py's other 3 callers key off any value beyond
    `< 0`, only log it.
    """
    payload: Dict[str, Any] = {
        "album_id": int(album_db_id or 0),
        "mb_albumid": mb_albumid,
        "acoustid_verify": True,
        "allow_establish_release_group": True,
        "stamp_release_metadata": True,
    }
    if target_tracks:
        payload["target_tracks"] = target_tracks
    if zero_unmatched:
        payload["zero_unmatched"] = True

    try:
        plan_res = composite_workflows.plan_album_mb_track_repair(payload)
    except Exception as ex:
        log.append(f"  MB fetch warning: {ex}")
        return -1

    if not plan_res.get("ok"):
        code = plan_res.get("code") or ""
        error_text = str(plan_res.get("error") or "")
        if not code:
            # The engine's MB-lookup-failure path returns {"ok": False,
            # "error": ...} with no "code" at all -- the one case that
            # must still map to -1 (refuse to proceed), matching every
            # real caller's own `matched < 0` check.
            log.append(f"  MB fetch warning: {error_text or 'MusicBrainz release lookup failed'}")
            return -1
        log.append(f"  {error_text or 'MB track repair planning failed'}")
        return 0

    op_id = plan_res.get("operation_id")
    if not op_id:
        log.append("  " + str(plan_res.get("message") or "No MusicBrainz recording IDs needed safe repair."))
        return 0

    updated = int(plan_res.get("updated") or 0)
    conflicts = int(plan_res.get("conflicts") or 0)
    acoustid_rejected = int(plan_res.get("acoustid_rejected") or 0)
    zero_planned = int(plan_res.get("zero_unmatched_rows") or 0)
    log.append(
        f"  MB release match: {updated} track(s) to repair, {conflicts} conflict(s) "
        f"requiring review, {acoustid_rejected} AcoustID-rejected fuzzy match(es)"
        + (f", {zero_planned} unmatched item(s) to zero" if zero_unmatched else "")
    )

    try:
        apply_res = composite_workflows.apply_album_mb_track_repair(op_id, write_tags=True)
    except Exception as ex:
        log.append(f"  DB write warning: {ex}")
        return 0
    if not apply_res.get("ok"):
        log.append(f"  DB write warning: {apply_res.get('error') or 'apply failed'}")
        return 0

    log.append(f"  Updated {updated} item(s) in DB with MB track data.")
    return updated


_ALBUM_MB_SUGGESTIONS_FILE = Path("/config/album_mb_suggestions.json")


# ── Clean: album track validation ────────────────────────────────────────────

_ALBUM_TRACK_PREFIX_RE = re.compile(
    r'^(?:.*?\s+[-–—]\s+)?(?:\d+|%\w+\{[^}]+\})\s*[-–—\.]\s*',
    re.IGNORECASE,
)


_ALBUM_TRACK_ANNOT_RE = re.compile(
    r'\s*[\(\[]\s*(?:ft\.|feat\.|with\b|prod\.?|produced\s+by|remix|edit|remaster|radio|live|'
    r'acoustic|album\s+version|single\s+version|original\s+version|explicit\s+album\s+version|'
    r'clean\s+version|version|bonus|instrumental|deluxe|explicit|clean|official|'
    r'lyrics?|letra\s+oficial|hq(?:\s+audio)?|hd|audio|video|mv).*?[\)\]]\s*',
    re.IGNORECASE,
)


_ALBUM_TRACK_UNCLOSED_RE = re.compile(r'\s*[\(\[](?!.*[\)\]]).*$')


def _album_track_feature_variants(value: str) -> List[str]:
    """Return title candidates with normal and glued feature suffixes removed."""
    try:
        return _canonical_track_feature_variants(value)
    except NameError:
        from backend.matching import track_feature_variants as _fallback_features
        return _fallback_features(value)


def _album_track_parenthetical_alias_variants(value: str) -> List[str]:
    """Return conservative title aliases such as "Money (That's What I Want)" -> "Money"."""
    try:
        return _canonical_track_parenthetical_alias_variants(value)
    except NameError:
        from backend.matching import track_parenthetical_alias_variants as _fallback_alias
        return _fallback_alias(value)


def _album_track_path_prefixes(path: str) -> List[str]:
    """Normalized artist/album prefixes that may be embedded in track titles."""
    raw_path = _s(path).replace("\\", "/")
    if not raw_path:
        return []
    for marker in (
        str(MUSIC_ROOT).rstrip("/") + "/",
        # Layout suffixes, not roots: "/media/music/" also matches any
        # "<prefix>/media/music/" path and splits it at the same place.
        "/media/music/",
        "/torrents/music/",
        "/downloads/music/",
        "/download/music/",
    ):
        if marker in raw_path:
            raw_path = raw_path.split(marker, 1)[1]
            break
    parts = [p for p in raw_path.split("/") if p]
    prefixes: List[str] = []

    def _prefix_candidates(value: str) -> List[str]:
        text = _s(value).strip()
        if not text:
            return []
        candidates = [text]
        no_year = re.sub(r"\s*[\(\[]\d{4}[\)\]]\s*$", "", text).strip()
        if no_year and no_year != text:
            candidates.append(no_year)
        no_mbid = re.sub(
            r"\s*[\{\(\[]\s*[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\s*[\}\)\]]\s*$",
            "",
            text,
            flags=re.I,
        ).strip()
        if no_mbid and no_mbid != text:
            candidates.append(no_mbid)
        numeric_alias_base = no_mbid or no_year or text
        no_leading_number = re.sub(r"^\s*\d+[\s._-]+", "", numeric_alias_base).strip()
        if no_leading_number and no_leading_number != numeric_alias_base:
            candidates.append(no_leading_number)
        try:
            bare_artist = _artist_folder_name_without_mbid(text)
        except Exception:
            bare_artist = ""
        if bare_artist and bare_artist != text:
            candidates.append(bare_artist)
        return candidates

    if len(parts) >= 2:
        prefixes.extend(_prefix_candidates(parts[0]))
        prefixes.extend(_prefix_candidates(parts[1]))
    if len(parts) >= 1:
        stem = Path(parts[-1]).stem
        # A path template like "Artist - Album - 00 - Artist Title" can still
        # leave the artist at the start of the title after numeric prefixes are stripped.
        file_parts = [p.strip() for p in re.split(r"\s+[-–—]\s*|\s*[-–—]\s+", stem) if p.strip()]
        if file_parts:
            prefixes.extend(_prefix_candidates(file_parts[0]))
    out: List[str] = []
    seen: set = set()
    for prefix in prefixes:
        norm = _album_track_norm(prefix)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def _album_track_title_variants(title: str, path: str = "") -> List[str]:
    raw_values = [_s(title).strip()]
    if path:
        raw_values.append(Path(_s(path)).stem)
    for raw in list(raw_values):
        cleaned = _strip_track_filename_id_suffix(raw)
        if cleaned and cleaned not in raw_values:
            raw_values.append(cleaned)
    variants: List[str] = []
    seen: set = set()
    path_prefixes = _album_track_path_prefixes(path)
    for raw in raw_values:
        if not raw:
            continue
        candidates = [raw]
        parts = [p.strip() for p in re.split(r"\s+[-–—]\s*|\s*[-–—]\s+", raw) if p.strip()]
        if len(parts) > 1:
            for idx in range(1, len(parts)):
                candidates.append(" - ".join(parts[idx:]))
            candidates.append(parts[-1])
        stripped = raw
        for _ in range(3):
            nxt = _ALBUM_TRACK_PREFIX_RE.sub("", stripped).strip()
            if nxt == stripped:
                break
            stripped = nxt
            candidates.append(stripped)
        expanded_candidates: List[str] = []
        for cand in candidates:
            expanded_candidates.append(cand)
            stripped_cand = cand
            for _ in range(3):
                nxt = _ALBUM_TRACK_PREFIX_RE.sub("", stripped_cand).strip()
                if nxt == stripped_cand:
                    break
                stripped_cand = nxt
                expanded_candidates.append(stripped_cand)
        feature_candidates: List[str] = []
        for cand in expanded_candidates:
            feature_candidates.extend(_album_track_feature_variants(cand) or [cand])
        alias_candidates: List[str] = []
        for cand in feature_candidates:
            alias_candidates.append(cand)
            alias_candidates.extend(_album_track_parenthetical_alias_variants(cand))
        for cand in alias_candidates:
            bare = _ALBUM_TRACK_UNCLOSED_RE.sub(
                "", _ALBUM_TRACK_ANNOT_RE.sub("", cand)
            ).strip()
            for val in (cand, bare):
                norm = _album_track_norm(val)
                norm_options = [norm] if norm else []
                for prefix in path_prefixes:
                    if norm == prefix:
                        continue
                    if norm.startswith(prefix + " "):
                        norm_options.append(norm[len(prefix):].strip())
                for opt in norm_options:
                    if opt and opt not in seen:
                        seen.add(opt)
                        variants.append(opt)
    return variants


def _mb_release_tracklist_cache_path(mb_albumid: str) -> Optional[Path]:
    # SEC-002 CodeQL repository-wide closure finding: this previously
    # lowercased/stripped mb_albumid with no format check at all before
    # joining it onto _MB_RELEASE_TRACKLIST_CACHE_DIR -- unlike the sibling
    # release-art cache (_release_art_cache_info/_release_art_download),
    # which anchors on _RELEASE_ART_MBID_RE before ever touching a path.
    # _fetch_mb_release_tracklist() has ~28 call sites across this file;
    # rather than audit every one for whether its mb_albumid ultimately
    # traces to attacker-controlled text, validate here at the single
    # shared cache-path builder both the read and write sides funnel
    # through -- a value that isn't a real MusicBrainz UUID can never
    # reach the filesystem, and a real MBID has an identical resolved
    # path either way, so this is a pure hardening with no behavior
    # change for any legitimate caller.
    if not _is_valid_mb_uuid(mb_albumid):
        return None
    release_id = _s(mb_albumid).strip().lower()
    return _MB_RELEASE_TRACKLIST_CACHE_DIR / release_id[:2] / f"{release_id}.json"


def _mb_release_tracklist_read_disk(mb_albumid: str, now: float) -> Optional[Dict[str, Any]]:
    if _MB_RELEASE_TRACKLIST_DISK_CACHE_TTL <= 0:
        return None
    cache_path = _mb_release_tracklist_cache_path(mb_albumid)
    if cache_path is None:
        return None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        payload = cached.get("payload")
        ts = float(cached.get("ts") or 0)
        # Entries without release_artist were fetched without artist-credits
        # (before that inc was added) and are refetched.
        if (isinstance(payload, dict) and payload.get("release_artist")
                and (now - ts) < _MB_RELEASE_TRACKLIST_DISK_CACHE_TTL):
            return copy.deepcopy(payload)
    except Exception:
        pass
    return None


def _mb_release_tracklist_write_disk(mb_albumid: str, payload: Dict[str, Any]) -> None:
    if _MB_RELEASE_TRACKLIST_DISK_CACHE_TTL <= 0:
        return
    cache_path = _mb_release_tracklist_cache_path(mb_albumid)
    if cache_path is None:
        return
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_suffix(f".{os.getpid()}.tmp")
        tmp_path.write_text(
            json.dumps({"ts": time.time(), "payload": payload}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp_path.replace(cache_path)
    except Exception:
        pass


def _fetch_mb_release_tracklist(mb_albumid: str, log: Optional[List[str]] = None) -> Dict[str, Any]:
    mb_albumid = (mb_albumid or "").strip().lower()
    if not _MB_UUID_RE.match(mb_albumid):
        return {"ok": False, "error": "Invalid MusicBrainz release ID", "tracks": []}
    now = time.time()
    with _MB_RELEASE_TRACKLIST_CACHE_LOCK:
        cached = _MB_RELEASE_TRACKLIST_CACHE.get(mb_albumid)
        if cached and (now - float(cached.get("ts") or 0)) < _MB_RELEASE_TRACKLIST_CACHE_TTL:
            return copy.deepcopy(cached.get("payload") or {})
    disk_cached = _mb_release_tracklist_read_disk(mb_albumid, now)
    if disk_cached is not None:
        with _MB_RELEASE_TRACKLIST_CACHE_LOCK:
            _MB_RELEASE_TRACKLIST_CACHE[mb_albumid] = {
                "ts": now,
                "payload": copy.deepcopy(disk_cached),
            }
        return disk_cached
    mb_url = (f"https://musicbrainz.org/ws/2/release/{mb_albumid}"
              "?inc=recordings+release-groups+artist-credits&fmt=json")
    req = _ur.Request(
        mb_url,
        headers={"User-Agent": "BeetsWebControl/1.0 (beets-webcontrol)"}
    )
    from backend.provider_boundary import ProviderOutcome, ProviderResult, call_with_retry

    def _once():
        with provider_boundary.opened("musicbrainz", req, timeout=30, max_attempts=1) as resp:
            return ProviderResult("musicbrainz", ProviderOutcome.CONFIRMED, data=json.loads(resp.read()))

    fetched = call_with_retry("musicbrainz", _once, max_attempts=3, base_backoff=1.5)
    if fetched.outcome != ProviderOutcome.CONFIRMED:
        # 404 is an answer (no such release); everything else means MusicBrainz
        # could not be asked -- reported as such, never as "no tracks".
        outcome = ProviderOutcome.NO_RESULT if fetched.status_code == 404 else fetched.outcome
        if log is not None:
            log.append(f"  MB fetch failed: MusicBrainz lookup {outcome.value}.")
        _app_logger.error("MusicBrainz release lookup failed: %s", outcome.value)
        return {"ok": False, "error": "MusicBrainz lookup failed.", "tracks": [], "outcome": outcome.value}
    mb_data: Dict[str, Any] = fetched.data or {}

    release_artist_info = _playlist_artist_credit_info(mb_data.get("artist-credit") or [])
    release_artist = release_artist_info.get("albumartist", "")

    tracks: List[Dict[str, Any]] = []
    for medium in mb_data.get("media", []) or []:
        disc_num = int(medium.get("position") or 1)
        for trk in medium.get("tracks", []) or []:
            rec = trk.get("recording") or {}
            title = _s(trk.get("title") or rec.get("title") or "").strip()
            tracks.append({
                "track": int(trk.get("position") or 0),
                "disc": disc_num,
                "title": title,
                "title_norm": _album_track_norm(title),
                "mb_trackid": _s(rec.get("id", "")).strip().lower(),
                "duration_ms": int(trk.get("length") or rec.get("length") or 0),
            })
    release_group = mb_data.get("release-group") or {}
    result = {
        "ok": bool(tracks),
        "error": "" if tracks else "MusicBrainz release has no tracks",
        "outcome": "confirmed" if tracks else "no_result",
        "tracks": tracks,
        "release_title": _s(mb_data.get("title", "")).strip(),
        "release_artist": release_artist,
        "release_artist_id": release_artist_info.get("mb_albumartistid", ""),
        "release_artistids": release_artist_info.get("mb_albumartistids", ""),
        "release_group": release_group.get("id", ""),
        "release_group_primary_type": _s(release_group.get("primary-type", "")).strip(),
        "release_group_secondary_types": [
            _s(value).strip() for value in (release_group.get("secondary-types") or []) if _s(value).strip()
        ],
        "country": _s(mb_data.get("country", "")).strip(),
        "date": _s(mb_data.get("date", "")).strip(),
    }
    if result.get("ok"):
        payload_copy = copy.deepcopy(result)
        with _MB_RELEASE_TRACKLIST_CACHE_LOCK:
            _MB_RELEASE_TRACKLIST_CACHE[mb_albumid] = {
                "ts": time.time(),
                "payload": payload_copy,
            }
        _mb_release_tracklist_write_disk(mb_albumid, payload_copy)
    return result


def _album_track_score(item: Dict[str, Any], mb_trk: Dict[str, Any]) -> float:
    try:
        return _canonical_album_track_score(item, mb_trk)
    except NameError:
        from backend.matching import album_track_score as _fallback_score
        return _fallback_score(item, mb_trk)


def _best_album_track_match(item: Dict[str, Any], mb_tracks: List[Dict[str, Any]]) -> Dict[str, Any]:
    try:
        return _canonical_best_album_track_match(item, mb_tracks)
    except NameError:
        from backend.matching import best_album_track_match as _fallback_best
        return _fallback_best(item, mb_tracks)


def _ai_review_album_track_candidates(album_info: Dict[str, Any],
                                      mb_tracks: List[Dict[str, Any]],
                                      candidates: List[Dict[str, Any]],
                                      log: Optional[List[str]] = None) -> Dict[str, Any]:
    api_key = _ai_api_key()
    if not api_key:
        return {"status": "skipped", "error": "OPENAI_API_KEY not configured"}
    if not candidates:
        return {"status": "skipped", "error": "No candidates"}

    mb_line_parts: List[str] = []
    for t in mb_tracks[:80]:
        disc_num = int(t.get("disc") or 1)
        track_num = int(t.get("track") or 0)
        title_text = _s(t.get("title") or "")
        mb_track_id = _s(t.get("mb_trackid") or "")
        mb_line_parts.append(f"{disc_num}.{track_num:02d} {title_text} [{mb_track_id}]")
    mb_lines = "\n".join(mb_line_parts)
    cand_lines = "\n".join(
        f'id={c["id"]} track={c.get("disc", 1)}.{int(c.get("track") or 0):02d} '
        f'title={c.get("title", "")!r} file={c.get("filename", "")!r} '
        f'best_mb={((c.get("best_mb") or {}).get("disc", 1))}.'
        f'{int((c.get("best_mb") or {}).get("track") or 0):02d} '
        f'{(c.get("best_mb") or {}).get("title", "")!r} score={c.get("score", 0)} '
        f'fingerprint={(c.get("fingerprint") or {}).get("status", "")}'
        for c in candidates
    )
    prompt = (
        "You are checking whether files in a Beets album belong to the exact "
        "MusicBrainz release. Use the MusicBrainz track list as the source of truth.\n\n"
        f"Album artist: {album_info.get('artist','')}\n"
        f"Album: {album_info.get('album','')}\n"
        f"MusicBrainz release ID: {album_info.get('mb_albumid','')}\n\n"
        f"MUSICBRAINZ TRACK LIST:\n{mb_lines}\n\n"
        f"QUESTIONABLE LIBRARY ITEMS:\n{cand_lines}\n\n"
        "For each questionable item, choose:\n"
        "- remove: clearly from another album or not on this release\n"
        "- keep: clearly a valid version of a listed MusicBrainz track\n"
        "- review: uncertain\n"
        "Do not remove only because punctuation, featured-artist text, or spelling differs.\n"
        'Return ONLY JSON: {"decisions":[{"id":123,"action":"remove|keep|review",'
        '"confidence":"high|medium|low","reason":"short reason",'
        '"matched_track":"disc.track or empty"}]}'
    )
    _track_review_schema = {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id":            {"type": "integer"},
                        "action":        {"type": "string", "enum": ["remove", "keep", "review"]},
                        "confidence":    {"type": "string", "enum": ["high", "medium", "low"]},
                        "reason":        {"type": "string"},
                        "matched_track": {"type": "string"},
                    },
                    "required": ["id", "action", "confidence", "reason", "matched_track"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["decisions"],
        "additionalProperties": False,
    }
    _ai_model, _ai_endpoint = _ai_model_and_endpoint("gpt-4o-mini")
    req_body = json.dumps({
        "model": _ai_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "track_review", "strict": True, "schema": _track_review_schema},
        },
    }).encode()
    try:
        req = _ur.Request(
            _ai_endpoint,
            data=req_body,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
        )
        with provider_boundary.opened("ai", req, timeout=45) as resp:
            data = json.loads(resp.read())
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        if msg.get("refusal"):
            return {"status": "error", "error": f"AI refusal: {msg['refusal'][:100]}"}
        parsed = json.loads(msg["content"])
        decisions = parsed.get("decisions", []) if isinstance(parsed, dict) else []
        return {"status": "used", "decisions": decisions}
    except urllib.error.HTTPError as exc:
        # IA-15: provider error bodies can echo (partial) API keys; report the
        # status only and never read the body into logs or the job result.
        exc.close()
        if log is not None:
            log.append(f"  AI review failed: AI provider HTTP {exc.code}")
        return {"status": "error", "error": f"AI provider HTTP {exc.code}"}
    except Exception as ex:
        safe = _redact_security_text(ex)
        if log is not None:
            log.append(f"  AI review failed: {safe}")
        return {"status": "error", "error": safe}


#: MI-4: the only evidence that may propose removing a track from an album is a
#: canonical AcoustID CONFLICT whose top hit scores at least this (0-100).
_INTEGRITY_REMOVE_MIN_FINGERPRINT_SCORE = 80.0


def _scan_album_track_integrity(album_row: Dict[str, Any], *,
                                use_ai: bool,
                                use_fingerprint: bool,
                                fingerprint_limit: int,
                                log: List[str]) -> Optional[Dict[str, Any]]:
    aid = int(album_row["id"])
    album_title = _s(album_row.get("album", "")).strip()
    album_artist = _s(album_row.get("albumartist", "")).strip()
    mb_albumid = _s(album_row.get("mb_albumid", "")).strip().lower()
    mb_rg = _s(album_row.get("mb_releasegroupid", "")).strip().lower()
    if not mb_albumid and mb_rg:
        mb_albumid = _resolve_release_group_to_release(mb_rg, log)
    if not mb_albumid:
        return None

    mb = _fetch_mb_release_tracklist(mb_albumid, log)
    if not mb.get("ok"):
        return {
            "album_id": aid, "artist": album_artist, "album": album_title,
            "mb_albumid": mb_albumid, "error": mb.get("error") or "MB lookup failed",
            "remove_candidates": [], "review_candidates": [],
        }
    mb_tracks = mb["tracks"]

    try:
        raw_items = composite_workflows.find_all_items_by_album_id(aid)
        rows = sorted(
            raw_items,
            key=lambda it: (
                int(it.get("disc") or 1),
                int(it.get("track") or 0),
                _s(it.get("title") or ""),
                int(it.get("id") or 0),
            )
        )
    except Exception as ex:
        log.append(f"  DB read failed for album_id {aid}: {ex}")
        return None

    items: List[Dict[str, Any]] = []
    for row in rows:
        path = _s(row["path"])
        items.append({
            "id": int(row["id"]),
            "title": _s(row["title"]),
            "track": int(row["track"] or 0),
            "disc": int(row["disc"] or 1),
            "path": path,
            "abs_path": _album_item_abs_path(path),
            "filename": Path(_s(path)).name,
            "mb_trackid": _s(row["mb_trackid"]).strip().lower(),
            "length": float(row["length"] or 0),
        })

    if not items:
        return None

    expected_count = len(mb_tracks)
    actual_count = len(items)
    do_fingerprint = bool(use_fingerprint and actual_count <= max(1, fingerprint_limit))
    if use_fingerprint and not do_fingerprint:
        log.append(f"  Skipping fingerprint pass: {actual_count} tracks exceeds limit {fingerprint_limit}")

    records: List[Dict[str, Any]] = []
    for item in items:
        best = _best_album_track_match(item, mb_tracks)
        mb_best = best.get("track") or {}
        score = round(float(best.get("score") or 0.0), 3)
        fp: Dict[str, Any] = {"status": "skipped"}
        decision = "keep"
        reason = "Matched MusicBrainz track list"
        # MI-4: title similarity alone never proposes removal.
        if score < 0.62:
            decision = "review"
            reason = "No MusicBrainz track title is close enough (review; titles alone never remove a track)"
        elif score < 0.90:
            decision = "review"
            reason = "Fuzzy MusicBrainz match needs review"

        if do_fingerprint:
            fp = _album_track_fingerprint_check(item, mb_tracks)
            if fp.get("status") == AcoustIDStatus.CONFLICT:
                cand = fp.get("candidate") or {}
                cand_score = float(cand.get("score") or 0)
                if cand_score <= 1.0:
                    cand_score *= 100.0
                points_to = ("Audio fingerprint points to "
                             f"{cand.get('artist','')} - {cand.get('title','')}".strip(" -"))
                # MI-4: only a canonical fingerprint CONFLICT at score >= 80
                # may propose removal; a weaker conflict goes to review.
                if cand_score >= _INTEGRITY_REMOVE_MIN_FINGERPRINT_SCORE:
                    decision = "remove"
                    reason = points_to
                else:
                    decision = "review"
                    reason = f"{points_to} (fingerprint score {cand_score:.0f} below "                             f"{_INTEGRITY_REMOVE_MIN_FINGERPRINT_SCORE:.0f}; review)"
            elif fp.get("status") == AcoustIDStatus.AMBIGUOUS and decision == "keep" and score < 0.96:
                decision = "review"
                reason = "Fingerprint did not confirm the MusicBrainz recording"

        records.append({
            **item,
            "decision": decision,
            "reason": reason,
            "score": score,
            "exact_mbid": bool(best.get("exact_mbid")),
            "best_idx": int(best.get("idx", -1)),
            "best_mb": {
                "disc": int(mb_best.get("disc") or 1),
                "track": int(mb_best.get("track") or 0),
                "title": mb_best.get("title", ""),
                "mb_trackid": mb_best.get("mb_trackid", ""),
            } if mb_best else {},
            "fingerprint": fp,
        })

    grouped: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for rec in records:
        if rec["best_idx"] >= 0 and rec["decision"] != "remove" and rec["score"] >= 0.74:
            grouped[rec["best_idx"]].append(rec)

    def _collision_rank(path: str) -> int:
        m = re.search(r"\.(\d+)$", Path(path).stem)
        return int(m.group(1)) if m else 0

    for group in grouped.values():
        if len(group) <= 1:
            continue
        group.sort(key=lambda r: (
            0 if (r.get("fingerprint") or {}).get("status") == AcoustIDStatus.CONFIRMED else 1,
            0 if r.get("exact_mbid") else 1,
            -float(r.get("score") or 0),
            _collision_rank(r.get("path", "")),
            int(r.get("id") or 0),
        ))
        for extra in group[1:]:
            # Mapping to the same MB track is not same-recording proof:
            # duplicates leave only through the reviewed duplicate cleanup.
            extra["decision"] = "review"
            extra["reason"] = "Possible duplicate of the same MusicBrainz track (review; use duplicate cleanup)"

    review_records = [r for r in records if r["decision"] == "review"]
    ai_status: Dict[str, Any] = {"status": "skipped", "error": ""}
    if use_ai and review_records:
        ai_status = _ai_review_album_track_candidates(
            {"artist": album_artist, "album": album_title, "mb_albumid": mb_albumid},
            mb_tracks,
            review_records[:40],
            log,
        )
        decisions = {
            int(d.get("id")): d for d in ai_status.get("decisions", [])
            if str(d.get("id", "")).isdigit()
        }
        for rec in review_records:
            d = decisions.get(rec["id"])
            if not d:
                continue
            action = _s(d.get("action", "")).lower()
            conf = _s(d.get("confidence", "")).lower()
            if action == "remove" and conf in {"high", "medium"}:
                # MI-4: AI is untrusted; it can only annotate a review.
                rec["decision"] = "review"
                rec["reason"] = "AI suggests removal (review): " + _s(d.get("reason", "")).strip()
            elif action == "keep" and conf == "high":
                rec["decision"] = "keep"
                rec["reason"] = "AI review kept: " + _s(d.get("reason", "")).strip()
            else:
                rec["reason"] = "AI review uncertain: " + _s(d.get("reason", "")).strip()
            rec["ai"] = d

    keep_records = [r for r in records if r["decision"] == "keep"]
    local_match_ratio = (len(keep_records) / actual_count) if actual_count else 0.0
    low_album_match = actual_count >= 3 and local_match_ratio < 0.25
    if low_album_match:
        log.append(
            f"  Low album-level MB match: {len(keep_records)}/{actual_count} "
            "local track(s) confidently match; uncertain tracks stay in review (never auto-removed)."
        )
        for rec in records:
            if rec["decision"] == "review":
                rec["reason"] = (
                    rec["reason"] + "; album-level MusicBrainz match is also low -- check the selected release"
                )

    remove_candidates = [r for r in records if r["decision"] == "remove"]
    review_candidates = [r for r in records if r["decision"] == "review"]
    if not remove_candidates and not review_candidates and actual_count == expected_count:
        return None

    return {
        "album_id": aid,
        "artist": album_artist,
        "album": album_title,
        "mb_albumid": mb_albumid,
        "mb_url": f"https://musicbrainz.org/release/{mb_albumid}",
        "expected_count": expected_count,
        "actual_count": actual_count,
        "keep_count": len([r for r in records if r["decision"] == "keep"]),
        "local_match_ratio": round(local_match_ratio, 3),
        "low_album_match": low_album_match,
        "remove_candidates": remove_candidates,
        "review_candidates": review_candidates,
        "ai_status": ai_status,
        "fingerprint_checked": do_fingerprint,
        "release_title": mb.get("release_title", ""),
    }


def _album_mb_match_plan(album_id: int, mb_albumid: str,
                         log: Optional[List[str]] = None) -> Dict[str, Any]:
    mb_albumid = _s(mb_albumid).strip().lower()
    mb = _fetch_mb_release_tracklist(mb_albumid, log)
    if not mb.get("ok"):
        raise RuntimeError(mb.get("error") or "MusicBrainz release lookup failed")
    mb_tracks = mb.get("tracks") or []
    if not mb_tracks:
        raise RuntimeError("MusicBrainz release has no tracks")

    try:
        raw_items = composite_workflows.find_all_items_by_album_id(album_id)
        rows = sorted(
            raw_items,
            key=lambda it: (
                int(it.get("disc") or 1),
                int(it.get("track") or 0),
                _s(it.get("title") or ""),
                int(it.get("id") or 0),
            )
        )
    except Exception as ex:
        raise RuntimeError(f"Could not read album tracks: {ex}")

    items: List[Dict[str, Any]] = []
    for row in rows:
        path = _s(row["path"])
        items.append({
            "id": int(row["id"]),
            "title": _s(row["title"]),
            "track": int(row["track"] or 0),
            "disc": int(row["disc"] or 1),
            "path": path,
            "filename": Path(path).name,
            "mb_trackid": _s(row["mb_trackid"]).strip().lower(),
            "length": float(row["length"] or 0),
        })

    def _match_with_fingerprint_guard(item: Dict[str, Any],
                                      tracks: List[Dict[str, Any]]) -> Dict[str, Any]:
        best = _best_album_track_match(item, tracks)
        if int(best.get("idx", -1)) >= 0:
            fp = _album_track_fingerprint_check(item, tracks)
            if fp.get("status") == AcoustIDStatus.CONFIRMED:
                return best
            if fp.get("status") == AcoustIDStatus.CONFLICT:
                return {
                    "idx": -1,
                    "track": {},
                    "score": 0.0,
                    "title_score": 0.0,
                    "exact_mbid": False,
                }
            if (
                best.get("exact_mbid")
                and float(best.get("title_score") or 0) < _MB_TRACK_REPAIR_MATCH_THRESHOLD
            ):
                return {
                    "idx": -1,
                    "track": {},
                    "score": 0.0,
                    "title_score": 0.0,
                    "exact_mbid": False,
                }
        return best

    return build_album_match_plan(
        album_id=int(album_id),
        mb_albumid=mb_albumid,
        release_title=mb.get("release_title", ""),
        items=items,
        mb_tracks=mb_tracks,
        match_fn=_match_with_fingerprint_guard,
        file_exists_fn=lambda path: Path(_album_item_abs_path(path)).exists(),
        threshold=_MB_TRACK_PREFLIGHT_MATCH_THRESHOLD,
    )


def _album_mb_completeness(album_id: int, mb_override: str = "",
                           log: Optional[List[str]] = None) -> Dict[str, Any]:
    try:
        album_row = composite_workflows.get_album(album_id)
    except Exception as ex:
        raise RuntimeError(f"Could not read album {album_id}: {ex}")
    if not album_row:
        raise RuntimeError(f"Album {album_id} not found")

    album_title = _s(album_row["album"]).strip()
    album_artist = _s(album_row["albumartist"]).strip()
    caller_override = _s(mb_override).strip().lower()
    mb_albumid = (caller_override or _s(album_row["mb_albumid"])).strip().lower()
    mb_rg = _s(album_row["mb_releasegroupid"]).strip().lower()
    if not mb_albumid and mb_rg:
        mb_albumid = _resolve_release_group_to_release(
            mb_rg, log if log is not None else [], year=str(album_row["year"] or ""))
    if not mb_albumid:
        raise RuntimeError("Album does not have a MusicBrainz release ID")

    mb = _fetch_mb_release_tracklist(mb_albumid, log)
    if not mb.get("ok"):
        raise RuntimeError(mb.get("error") or "MusicBrainz release lookup failed")
    # SEC-002 Wave 14: a caller-supplied mb_albumid override is release-edition
    # evidence only, not album-family identity -- reject it outright rather
    # than silently repairing tracks against a different release family than
    # the one already established for this album (mirrors the RGID-equality
    # guard build_album_matching_decision/_album_cleanup_merge_plan already
    # enforce elsewhere; deliberately narrow rather than a full retrofit of
    # this pre-existing, unrelated repair workflow).
    if caller_override and mb_rg:
        candidate_rg = _s(mb.get("release_group") or "").strip().lower()
        if candidate_rg and candidate_rg != mb_rg:
            raise RuntimeError(
                "Requested release belongs to a different Release Group than "
                "this album's established identity; refusing repair."
            )
    mb_tracks = mb["tracks"]

    try:
        raw_items = composite_workflows.find_all_items_by_album_id(album_id)
        rows = sorted(
            raw_items,
            key=lambda it: (
                int(it.get("disc") or 1),
                int(it.get("track") or 0),
                _s(it.get("title") or ""),
                int(it.get("id") or 0),
            )
        )
    except Exception as ex:
        raise RuntimeError(f"Could not read album tracks: {ex}")

    items: List[Dict[str, Any]] = []
    for row in rows:
        path = _s(row["path"])
        items.append({
            "id": int(row["id"]),
            "title": _s(row["title"]),
            "track": int(row["track"] or 0),
            "disc": int(row["disc"] or 1),
            "path": path,
            "filename": Path(path).name,
            "mb_trackid": _s(row["mb_trackid"]).strip().lower(),
            "length": float(row["length"] or 0),
        })

    alignment = summarize_mb_track_alignment(
        items,
        mb_tracks,
        match_fn=_best_album_track_match,
        file_exists_fn=lambda item: (
            not _album_item_abs_path(item.get("path", ""))
            or Path(_album_item_abs_path(item.get("path", ""))).exists()
        ),
        threshold=_MB_TRACK_PREFLIGHT_MATCH_THRESHOLD,
        repair_threshold=_MB_TRACK_REPAIR_MATCH_THRESHOLD,
    )

    return {
        "ok": True,
        "album_id": album_id,
        "album": album_title or mb.get("release_title", ""),
        "artist": album_artist,
        "year": int(str(album_row["year"] or "0")[:4] or 0),
        "mb_albumid": mb_albumid,
        "mb_url": f"https://musicbrainz.org/release/{mb_albumid}",
        "expected_count": alignment["expected_count"],
        "actual_count": alignment["actual_count"],
        "extra_count": alignment["extra_count"],
        "extra_track_count": alignment["extra_count"],
        "extra_items": alignment["extra_items"][:25],
        "in_library": alignment["in_library"],
        "missing_count": alignment["missing_count"],
        "percent": alignment["percent"],
        "tracks": alignment["tracks"],
        "missing": alignment["missing"],
        "mb_missing_count": alignment["missing_count"],
        "mb_trackid_missing_count": alignment["mb_trackid_missing_count"],
        "mb_trackid_mismatch_count": alignment["mb_trackid_mismatch_count"],
        "mb_duplicate_recording_id_count": alignment.get("mb_duplicate_recording_id_count", 0),
        "duplicate_recording_groups": alignment.get("duplicate_recording_groups", [])[:25],
        "mb_repairable_count": alignment["mb_repairable_count"],
        "mb_health_source": "musicbrainz",
        "release_title": mb.get("release_title", ""),
    }


def _remove_album_track_items(album_id: int, item_ids: List[int], *,
                              dry_run: bool,
                              delete_files: bool = False,
                              clean_empty_folders: bool = False,
                              log: List[str],
                              approved_by: str = "") -> Dict[str, Any]:
    """Take operator-selected tracks out of one album through the engine
    quarantine (track_quarantine_v1): the rows leave the library, the files
    are KEPT in the engine quarantine and a rollback restores both.

    Files are never deleted here, whatever ``delete_files`` says (the flag is
    accepted for API compatibility and reported as quarantine). A live run
    needs ``approved_by`` -- the caller's statement of the operator's explicit
    confirmation -- and is otherwise refused; the dry run only validates."""
    if not item_ids:
        return {"removed_db": 0, "deleted_files": 0, "quarantined_files": 0, "folders_removed": 0,
                "dry_run": dry_run, "file_action": "quarantine"}
    if not dry_run and not _s(approved_by).strip():
        raise RuntimeError("Track removal needs the operator's explicit confirmation; nothing was removed.")
    plan = composite_workflows.plan_track_quarantine(int(album_id), list(item_ids), create=not dry_run,
                                                     reason=_s(approved_by))
    if not plan.get("ok"):
        log.append(f"Track removal refused: {plan.get('error')}")
        for problem in plan.get("problems") or []:
            log.append(f"  item {problem.get('item_id')}: {problem.get('reason')}")
        raise RuntimeError(f"Track removal refused ({plan.get('code')}): {plan.get('error')}")
    entries = plan.get("items") or []
    if dry_run:
        for e in entries:
            log.append(f"  Would quarantine item {e['item_id']}: {Path(e['path']).name}")
        return {"removed_db": len(entries), "deleted_files": 0, "quarantined_files": len(entries),
                "folders_removed": 0, "dry_run": True, "album_deleted": False, "file_action": "quarantine"}
    store = composite_workflows.get_default_store()
    if store.transition(plan["operation_id"], "Preview", "Approved", metadata={"approved_by": _s(approved_by)}) is None:
        raise RuntimeError("Could not approve the track quarantine plan; nothing was removed.")
    applied = composite_workflows.apply_track_quarantine(plan["operation_id"])
    if not applied.get("ok"):
        raise RuntimeError(applied.get("error") or "; ".join(applied.get("verification_problems") or [])
                           or "Track quarantine did not verify.")
    _invalidate_lib_cache()
    _trigger_plex_refresh(log)
    log.append(f"Quarantined {len(entries)} track(s) of album {album_id} (files kept; rollback "
               f"available on transaction {plan['operation_id']}).")
    return {"removed_db": len(entries), "deleted_files": 0, "quarantined_files": len(entries),
            "folders_removed": 0, "dry_run": False, "album_deleted": False, "file_action": "quarantine",
            "operation_id": plan["operation_id"], "quarantine_id": applied.get("quarantine_id")}


_STAMP_UUID_IN_NAME_RE = re.compile(
    r'\s*(?:\{|\()[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:\}|\))\s*$',
    re.IGNORECASE,
)


def _artist_folder_name_without_mbid(name: str) -> str:
    return _STAMP_UUID_IN_NAME_RE.sub("", _s(name)).strip() or _s(name).strip()


def _stamp_folder_for_item_path(
    raw_path: Any,
    root_abs: Path,
    music_root_abs: Path,
    folder_by_name: Dict[str, Path],
) -> Optional[Path]:
    text = _s(raw_path).replace("\\", "/").strip()
    if not text:
        return None
    path_value = Path(text)
    candidates = [path_value] if path_value.is_absolute() else [
        music_root_abs / text,
        root_abs / text,
    ]
    for candidate in candidates:
        try:
            rel = candidate.resolve(strict=False).relative_to(root_abs)
        except Exception:
            continue
        if rel.parts:
            folder = folder_by_name.get(rel.parts[0])
            if folder is not None:
                return folder
    if not path_value.is_absolute():
        parts = [p for p in text.split("/") if p]
        if parts:
            return folder_by_name.get(parts[0])
    return None


def _artist_folder_album_rows() -> List[Dict[str, Any]]:
    """Every item's path, album id and album-artist MBID, read through the
    adapter. composite_workflows.get_artist_folder_album_mbids() takes one
    folder and returns no paths, so this whole-library caller raised
    TypeError there (Clean All logged it on every run)."""
    items = beets_adapter.get_items()
    BeetsAdapter._require_item_paths(items)
    return [{"path": BeetsAdapter._decode_path(it.get("path")), "album_id": it.get("album_id"),
             "mb_albumartistid": it.get("mb_albumartistid")} for it in items]


def _stamp_artist_folder_album_mbid_counts(
    root: Path,
    folders: List[Path],
) -> Tuple[Dict[str, Dict[str, set]], Dict[str, int], str]:
    """Return distinct album counts by immediate artist folder and MB artist ID."""
    folder_by_name = {folder.name: folder for folder in folders}
    root_abs = root.resolve(strict=False)
    music_root_abs = Path(MUSIC_ROOT).resolve(strict=False)
    album_ids_by_folder: Dict[str, set] = {}
    mbid_album_ids_by_folder: Dict[str, Dict[str, set]] = {}
    try:
        rows = _artist_folder_album_rows()
    except Exception as ex:
        _app_logger.error("Artist folder MBID counts: engine call failed: %s", ex, exc_info=True)
        return {}, {}, _safe_inventory_error_message(ex)

    for row in rows:
        folder = _stamp_folder_for_item_path(
            row["path"],
            root_abs,
            music_root_abs,
            folder_by_name,
        )
        if folder is None:
            continue
        try:
            album_id = int(row["album_id"] or 0)
        except Exception:
            album_id = 0
        if not album_id:
            continue
        folder_key = str(folder)
        album_ids_by_folder.setdefault(folder_key, set()).add(album_id)
        aid = _s(row["mb_albumartistid"]).strip().lower()
        if _MB_UUID_RE.match(aid):
            mbid_album_ids_by_folder.setdefault(folder_key, {}).setdefault(aid, set()).add(album_id)

    album_totals = {
        folder_key: len(album_ids)
        for folder_key, album_ids in album_ids_by_folder.items()
    }
    return mbid_album_ids_by_folder, album_totals, ""


_MB_VARIOUS_ARTISTS_ID = "89ad4ac3-39f7-470e-963a-56509c546377"


def _playlist_artist_credit_info(credits: Any) -> Dict[str, str]:
    parts: List[str] = []
    ids: List[str] = []
    for credit in credits or []:
        if isinstance(credit, str):
            parts.append(credit)
            continue
        if not isinstance(credit, dict):
            continue
        artist_data = credit.get("artist") or {}
        name = _s(credit.get("name") or artist_data.get("name", "")).strip()
        artist_id = _s(artist_data.get("id") or "").strip().lower()
        joinphrase = _s(credit.get("joinphrase") or "")
        if name:
            parts.append(name)
        if _MB_UUID_RE.match(artist_id) and artist_id not in ids:
            ids.append(artist_id)
        if joinphrase:
            parts.append(joinphrase)
    albumartist = _normalize_albumartist("".join(parts).strip())
    if albumartist.casefold() == "various artists" and _MB_VARIOUS_ARTISTS_ID not in ids:
        ids.insert(0, _MB_VARIOUS_ARTISTS_ID)
    return {
        "albumartist": albumartist,
        "mb_albumartistid": ids[0] if ids else "",
        "mb_albumartistids": "; ".join(ids),
    }
