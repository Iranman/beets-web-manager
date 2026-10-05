"""Playlists: parsing, matching to the library, staging, sync and exports (ARCH-001).
"""

from __future__ import annotations

import backend.provider_boundary as provider_boundary
import base64, copy, difflib, hashlib, json, os, re, shutil, socket, sqlite3, subprocess, threading, time, unicodedata, uuid
import urllib.error
import backend.job_contract as job_contract
from backend.matching import evaluate_release_group_candidate
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from backend.app_runtime import _app_logger, AUDIO_EXT, LIB_PATH, MUSIC_ROOT, PLAYLIST_AUTO_SYNC_ENABLED, PLAYLIST_AUTO_SYNC_INTERVAL, PLAYLIST_DOWNLOAD_BATCH_SIZE, PLAYLIST_DOWNLOAD_METHODS, PLAYLIST_DOWNLOAD_ROOT, PLAYLIST_EXPORTS_DIR, PLAYLIST_JOB_STATE_DIR, PLAYLIST_MANIFESTS_DIR, PLAYLIST_MEMBERSHIP_DIR, PLAYLIST_MIN_DOWNLOAD_SECONDS, PLAYLIST_PIPELINE_STATES, PLAYLIST_STATE_ROOT, PLEX_API_TIMEOUT, PLEX_PLAYLIST_CHUNK_SIZE, PLEX_SCAN_TIMEOUT, PLEX_SYNC_MAX_UNMATCHED_REPLACE, PLEX_SYNC_MIN_MATCH_RATIO, _MB_UUID_RE, _s, _up, _ur, _ytdlp_ready
from backend.ytdlp_service import _apply_ytdlp_netrc, _download_method_label, _spotiflac_missing_tracks_download, _ytdlp_js_runtime_options, _ytdlp_missing_tracks_download, _ytdlp_remote_components, _ytdlp_source_extractor_args
from backend.app_runtime import _norm, _path_is_under, _redact_security_text, _safe_path_component
from backend.audio_preferences import load_music_format_preferences as _load_music_format_preferences, validate_audio_file as _validate_audio_file_preferences, handle_rejected_download as _handle_rejected_audio_download
from helpers_mb import _fetch_mb_recording_details, _mb_recording_search, _mb_release_search, _clean_for_mb, _resolve_release_group_to_release
from backend.beets_adapter import lib, BeetsUnavailableError
import backend.composite_workflows as composite_workflows
from backend.library_cache import library_cache
from backend.acoustid_service import _acoustid_lookup_cached, _acoustid_verify_match, _album_track_norm, _audio_identity_decision, _normalize_albumartist, _playlist_artist_name_score, _playlist_artist_name_variants, _playlist_title_score, _playlist_token_score, _read_file_media_tags
from backend.slskd_service import SLSKD_API_KEY, _download_method_list, _find_slskd_downloaded_files, _slskd_search_and_queue, _slskd_title_guess_from_name, _slskd_wait_downloads
from backend.matching_service import _MB_VARIOUS_ARTISTS_ID, _artist_folder_name_without_mbid, _best_album_track_match, _fetch_mb_release_tracklist, _invalidate_lib_cache, _playlist_artist_credit_info
from backend.app_runtime import jobs
from backend.app_runtime import WEB_MANAGER_DATA_DIR
from backend.serializers import _json_from_flask_response
from backend.plex_service import _playlist_path_keys, _playlist_resolve_item_path, _playlist_status_id, _plex_find_music_section, _plex_is_final_library_path, _plex_machine_identifier, _plex_request, _plex_settings, _plex_status_payload, _plex_track_file, _plex_track_keys_for_items, _trigger_plex_refresh

# ── ARCH-001 extracted code ──


def _audio_duration_seconds(path_value: str) -> float:
    ffprobe = shutil.which("ffprobe") or "/usr/bin/ffprobe"
    try:
        r = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path_value,
            ],
            timeout=15,
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            return float((r.stdout or "").strip() or 0)
    except Exception:
        pass
    return 0.0


def _playlist_filter_preview_downloads(paths: Iterable[str], log) -> List[str]:
    valid: List[str] = []
    minimum = int(PLAYLIST_MIN_DOWNLOAD_SECONDS or 0)
    for path_value in paths:
        duration = _audio_duration_seconds(path_value)
        if minimum > 0 and duration > 0 and duration < minimum:
            try:
                Path(path_value).unlink()
            except Exception:
                pass
            log(
                f"  rejected preview-length download "
                f"({duration:.1f}s < {minimum}s): {Path(path_value).name}"
            )
            continue
        valid.append(path_value)
    return valid


def _music_format_preferences() -> Dict[str, Any]:
    return _load_music_format_preferences()


def _playlist_identity_log(match: Dict[str, Any]) -> str:
    evidence = match.get("identity") if isinstance(match.get("identity"), dict) else {}
    if not evidence:
        return "AcoustID not checked"
    score = evidence.get("acoustid_match_score") or 0
    try:
        score_text = f"{float(score):.0%}" if float(score) > 0 else "no score"
    except Exception:
        score_text = "no score"
    status = _s(evidence.get("acoustid_status") or evidence.get("fingerprint_status") or "unknown")
    mbid = _s(evidence.get("mb_recording_id_candidate") or "")
    return f"AcoustID {status}, {score_text}" + (f", MB recording {mbid}" if mbid else "")


def _playlist_artist_score(query_artist, item):
    query_norm = _norm(_normalize_albumartist(_s(query_artist)))
    if not query_norm:
        return 0.0
    candidate_names = [
        getattr(item, "artist", ""),
        getattr(item, "albumartist", ""),
        getattr(item, "artist_credit", ""),
    ]
    best = 0.0
    query_words = set(query_norm.split())
    for name in candidate_names:
        cand_norm = _norm(_normalize_albumartist(_s(name)))
        if not cand_norm:
            continue
        cand_words = set(cand_norm.split())
        seq = difflib.SequenceMatcher(None, query_norm, cand_norm).ratio()
        tok = _playlist_token_score(query_norm, cand_norm)
        subset = 0.0
        if query_words and cand_words and (query_words <= cand_words or cand_words <= query_words):
            subset = 0.92
        best = max(best, seq, tok, subset)
        try:
            best = max(best, _playlist_artist_name_score(query_artist, _s(name)))
        except Exception:
            pass
    return best


def _playlist_strip_track_prefix(value):
    text = _s(value).strip()
    text = re.sub(r"^\s*\d{1,2}\s*[-_. ]+\s*\d{2,3}\s*[-_. ]+", "", text)
    text = re.sub(r"^\s*\d{1,3}\s*[-_. ]+", "", text)
    return text.strip()


def _playlist_clean_video_text(value):
    return _s(value).strip().strip(" \t\r\n\"'`“”’")


def _playlist_strip_video_title_suffix(value):
    text = _playlist_clean_video_text(value)
    # PR #109 independent review finding: `if "|" not in text: return text`
    # is a real, load-bearing ReDoS mitigation, not an unrelated scope
    # addition -- the regex below has the same unbounded-whitespace-before-
    # a-required-literal shape as the other Wave 27/28 playlist patterns
    # (empirically confirmed: ~7.3s for a 60,000-char no-"|" adversarial
    # string against the unguarded regex, vs. near-instant with this
    # O(n) `in` check short-circuiting first). But the guard alone is
    # NOT sufficient: a string that DOES contain one "|" somewhere still
    # reaches the regex, and a separate long whitespace run elsewhere in
    # that same string that is not immediately followed by "|" is still
    # quadratic (empirically confirmed: ~8.1s for "a|" + 60,000 spaces +
    # "x") -- the guard only ever protects the entirely-"|"-free case.
    # Bounding the quantifier itself (real "Title | Channel | Extra"
    # separators are always exactly one space on each side) closes that
    # remaining gap the same way every other pattern in this closure
    # effort was fixed, without depending on the guard for full coverage.
    if "|" not in text:
        return text
    parts = [p.strip() for p in re.split(r"\s{1,20}\|\s{1,20}", text) if p.strip()]
    if len(parts) >= 3 and re.search(r"[A-Za-z0-9]", parts[0]):
        return parts[0]
    return text


def _playlist_split_artist_title(value):
    text = _playlist_strip_video_title_suffix(_playlist_strip_track_prefix(value))
    if not text:
        return None
    # Deterministic linear-time parser without regular expression backtracking.
    #
    # Reconciliation note (PR #108 x PR #109 merge): PR #108's own earlier
    # remediation of this same function (SEC-002 repository-wide CodeQL
    # closure, py/polynomial-redos) used a bounded regex split call on the
    # pattern r"\s{1,20}[-–—]\s{1,20}|(?<=[A-Za-z0-9])[-–—](?=[A-Z0-9])"
    # (maxsplit=1) -- which fixed the ReDoS but not the correctness defect
    # below (re's leftmost-match semantics still prefer whichever
    # alternative starts earliest in the string, not the semantically
    # stronger one). PR #109's implementation, kept here, fixes both.
    #
    # PR #109 independent review finding: a single left-to-right scan that
    # decides "spaced dash" vs. "compact dash" independently AT EACH dash
    # position and returns on whichever fired FIRST lets a compact hyphen
    # inside a hyphenated artist name (Jay-Z, Blink-182, T-Pain, A-Ha,
    # Run-D.M.C.) pre-empt the real, much stronger spaced " - " separator
    # that comes later in the same string, e.g. "Jay-Z - Empire State of
    # Mind" split as ("Jay", "Z - Empire State of Mind") instead of
    # ("Jay-Z", "Empire State of Mind"). Not a regression PR #109
    # introduced -- the original pre-both-PRs regex had the identical bug.
    #
    # Fixed with two full linear passes instead of one combined scan: a
    # spaced separator anywhere in the string is a far less ambiguous
    # artist/title boundary than a bare compact hyphen (which is
    # frequently just part of an artist's own name), so the first pass
    # looks for a spaced dash ANYWHERE before ever considering a compact
    # one. Only if the whole string has no spaced separator at all does
    # the second pass fall back to the first compact dash -- preserved
    # for genuinely un-spaced "Artist-Title" pastes, which is still a
    # real, supported input shape (see PlaylistSplitArtistTitleTests).
    # Two O(n) passes is still O(n) overall, not O(n^2): no backtracking,
    # no nested scan restarts, and no regex engine involved at all.
    n = len(text)

    i = 0
    while i < n:
        ch = text[i]
        if (
            ch in ("-", "–", "—")
            and i > 0 and text[i - 1].isspace()
            and i + 1 < n and text[i + 1].isspace()
        ):
            start = i - 1
            while start > 0 and text[start - 1].isspace():
                start -= 1
            end = i + 1
            while end < n and text[end].isspace():
                end += 1
            left = text[:start].strip()
            right = text[end:].strip()
            if left and right:
                return _playlist_clean_video_text(left), _playlist_clean_video_text(right)
            return None
        i += 1

    i = 0
    while i < n:
        ch = text[i]
        if ch in ("-", "–", "—") and i > 0 and i + 1 < n:
            prev = text[i - 1]
            nxt = text[i + 1]
            if (("a" <= prev <= "z" or "A" <= prev <= "Z" or "0" <= prev <= "9") and
                    ("A" <= nxt <= "Z" or "0" <= nxt <= "9")):
                left = text[:i].strip()
                right = text[i + 1:].strip()
                if left and right:
                    return _playlist_clean_video_text(left), _playlist_clean_video_text(right)
                return None
        i += 1
    return None


def _playlist_download_text_candidates(path_value: str) -> Dict[str, List[str]]:
    path = Path(path_value)
    title_candidates: List[str] = []
    artist_candidates: List[str] = []

    def _add(bucket: List[str], value: Any) -> None:
        text = _s(value).strip()
        if text and text not in bucket:
            bucket.append(text)

    def _add_split_variants(value: Any) -> None:
        text = _s(value).strip()
        if not text:
            return
        cleaned = _playlist_strip_track_prefix(text)
        _add(title_candidates, text)
        _add(title_candidates, cleaned)
        split = _playlist_split_artist_title(cleaned)
        if split:
            left, right = split
            _add(artist_candidates, left)
            _add(title_candidates, right)
            # Some SoundCloud/SpotiFLAC names are "Title-ARTIST".
            _add(title_candidates, left)
            _add(artist_candidates, right)

    try:
        tags = _read_file_media_tags(path)
        _add(title_candidates, tags.get("title", ""))
        _add(artist_candidates, tags.get("artist", ""))
        _add(artist_candidates, tags.get("albumartist", ""))
    except Exception:
        pass

    stem = path.stem
    stripped = re.sub(r"^\s*\d{1,2}\s*[-_. ]+\s*\d{1,3}\s*[-_. ]+", "", stem)
    stripped = re.sub(r"^\s*\d{1,3}\s*[-_. ]+", "", stripped)
    guessed = _slskd_title_guess_from_name(path.name)

    request_prefixed = re.match(
        r"^\s*(?:\d{3}|\d{1,2}\s*-\s*\d{1,3})\s+(.+)$",
        stem,
    )
    parts = [p.strip() for p in re.split(r"\s+-\s+", stripped) if p.strip()]
    if request_prefixed and len(parts) >= 2:
        # The first segment came from our yt-dlp requested-title prefix.
        # Treat only the source-provided tail as match evidence.
        source_tail = " - ".join(parts[1:])
        for text in (source_tail, _slskd_title_guess_from_name(source_tail)):
            _add_split_variants(text)
        parts = [p.strip() for p in re.split(r"\s+-\s+", source_tail) if p.strip()]
    else:
        for text in (stem, stripped, guessed):
            _add_split_variants(text)

    if len(parts) >= 2:
        for part in parts:
            _add_split_variants(part)
            _add(artist_candidates, part)
        _add(title_candidates, parts[0])
        _add(title_candidates, parts[-1])
        _add(artist_candidates, parts[0])
        _add(artist_candidates, parts[-1])

    return {"titles": title_candidates, "artists": artist_candidates}


def _playlist_log_line(log, message: str) -> None:
    try:
        if callable(log):
            log(message)
        elif hasattr(log, "append"):
            log.append(message)
    except Exception:
        pass


def _playlist_download_audio_allowed(path_value: str, log) -> bool:
    prefs = _music_format_preferences()
    try:
        result = _validate_audio_file_preferences(path_value, prefs)
    except Exception as ex:
        _playlist_log_line(
            log,
            f"  [audio] Rejected download: audio could not be inspected: "
            f"{Path(path_value).name} ({ex})",
        )
        _handle_rejected_audio_download(path_value, prefs, log=[])
        return False
    if result.get("ok"):
        return True
    msg = result.get("message") or "Rejected download: audio does not match Music Format Preferences"
    _playlist_log_line(log, f"  [audio] {msg}: {Path(path_value).name}")
    handling_log: List[str] = []
    _handle_rejected_audio_download(path_value, prefs, log=handling_log)
    for line in handling_log:
        _playlist_log_line(log, line)
    return False


def _playlist_score_download_candidates(candidates: Dict[str, List[str]], artist: str, title: str) -> Dict[str, Any]:
    """Score pre-extracted title/artist candidates against an expected artist/title.

    Split out from `_playlist_download_match` so callers that need to score one
    file against many tracks (e.g. resume reconciliation) can extract
    candidates via `_playlist_download_text_candidates` (which opens the file
    and reads tags) exactly once per file instead of once per (file, track)
    pair.
    """
    title_scores = [
        _playlist_title_score(title, candidate)
        for candidate in candidates.get("titles", [])
    ]
    title_score = max(title_scores or [0.0])

    artist_norm = _norm(_normalize_albumartist(_s(artist)))
    artist_candidates = [
        candidate for candidate in candidates.get("artists", [])
        if _norm(_normalize_albumartist(candidate))
    ]
    artist_score = 0.0
    if artist_norm and artist_candidates:
        dummy = type(
            "PlaylistDownloadCandidate",
            (),
            {"artist": "", "albumartist": "", "artist_credit": ""},
        )()
        for candidate in artist_candidates:
            dummy.artist = candidate
            dummy.albumartist = candidate
            dummy.artist_credit = candidate
            artist_score = max(artist_score, _playlist_artist_score(artist, dummy))

    title_ok = title_score >= 0.78
    if artist_norm and artist_candidates:
        artist_ok = artist_score >= 0.55
    elif artist_norm:
        # No usable artist tag/name: require a near-exact title before accepting.
        artist_ok = title_score >= 0.96
    else:
        artist_ok = True

    return {
        "ok": bool(title_ok and artist_ok),
        "title_score": round(float(title_score), 3),
        "artist_score": round(float(artist_score), 3),
        "title_candidates": candidates.get("titles", [])[:5],
        "artist_candidates": candidates.get("artists", [])[:5],
    }


def _playlist_download_match(path_value: str, artist: str, title: str,
                             expected_mb_trackid: str = "") -> Dict[str, Any]:
    candidates = _playlist_download_text_candidates(path_value)
    match = _playlist_score_download_candidates(candidates, artist, title)
    identity = _audio_identity_decision(
        path_value,
        expected_artist=artist,
        expected_title=title,
        expected_mb_trackid=expected_mb_trackid,
        text_match=match,
    )
    match["identity"] = identity
    match["identity_status"] = identity.get("identity_status", "review_required")
    match["review_required"] = identity.get("final_action") == "review"
    match["ok"] = identity.get("final_action") == "accept"
    return match


def _playlist_stamp_download_tags(path_value: str, artist: str, title: str, log) -> None:
    try:
        tags = {"title": title}
        if artist:
            tags["artist"] = artist
            tags["albumartist"] = artist
        composite_workflows.write_tags(str(path_value), tags)
    except Exception as ex:
        log(f"  warning: could not stamp playlist tags on {Path(path_value).name}: {ex}")


def _write_playlist_import_beets_config(temp_path: str) -> str:
    """Use the project single-track path format for playlist download imports."""
    return "/config/config.yaml"


def _playlist_identity_status_fields(match: Dict[str, Any]) -> Dict[str, Any]:
    evidence = match.get("identity") if isinstance(match.get("identity"), dict) else {}
    if not evidence:
        return {}
    fields = {
        "identity_status": evidence.get("identity_status"),
        "fingerprint_status": evidence.get("fingerprint_status"),
        "acoustid_status": evidence.get("acoustid_status"),
        "acoustid_score": evidence.get("acoustid_match_score"),
        "identity_reason": evidence.get("decision_reason"),
        "identity_mb_trackid": evidence.get("mb_recording_id_candidate"),
        "identity_mb_releasegroupid": evidence.get("mb_release_group_id_candidate"),
    }
    return {k: v for k, v in fields.items() if v not in (None, "", [])}


def _artist_folder_merge_key(name: str) -> str:
    text = _artist_folder_name_without_mbid(name).casefold().replace("&", "and")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", text)


def _safe_artist_folder_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"\\|?*\x00-\x1f]', "_", _s(name)).strip()
    cleaned = cleaned.replace("/", "_").rstrip(". ")
    return cleaned or "Unknown Artist"


def _playlist_title_modifier_is_noise(value):
    norm = _norm(value)
    if not norm:
        return False
    if re.search(r"\b(?:slowed|reverb|sped up|speed up|nightcore|chopped|screwed)\b", norm):
        return True
    if re.search(r"\b(?:official|video|visualizer|lyrics?|audio|karaoke|instrumental)\b", norm):
        return True
    if re.search(r"\b(?:street|clean|explicit|main|radio|single|album|extended|12|7)\b.*\b(?:version|edit|mix)\b", norm):
        return True
    if norm in {"street", "clean", "explicit", "main", "radio edit", "album version", "single version"}:
        return True
    return False


def _playlist_clean_variant_title(value):
    # SEC-5 (ReDoS): cap free text (1024 chars) before the regexes below.
    text = _playlist_strip_video_title_suffix(_s(value)[:1024])
    changed = False
    while True:
        match = re.search(r"(?:(?<!\s)\s+)?[\(\[]([^()\[\]]+)[\)\]]\s*$", text)
        if not match or not _playlist_title_modifier_is_noise(match.group(1)):
            break
        text = text[:match.start()].strip()
        changed = True
    match = re.search(r"(?<!\s)\s+[-–—]\s+(.+)$", text)
    if match and _playlist_title_modifier_is_noise(match.group(1)):
        text = text[:match.start()].strip()
        changed = True
    return _playlist_clean_video_text(text), changed


def _playlist_item_text_variants(item):
    pairs: List[Tuple[str, str]] = []

    def add(artist_value, title_value):
        title_text = _s(title_value).strip()
        if not title_text:
            return
        artist_values = _playlist_artist_name_variants(artist_value) or [_s(artist_value).strip()]
        for artist_text in artist_values:
            key = (_norm(artist_text), _norm(title_text))
            if key[1] and key not in {(_norm(a), _norm(t)) for a, t in pairs}:
                pairs.append((_s(artist_text).strip(), title_text))

    artist = getattr(item, "artist", "")
    albumartist = getattr(item, "albumartist", "")
    title = getattr(item, "title", "")
    path_text = _s(getattr(item, "path", ""))
    add(artist, title)
    if albumartist and _norm(albumartist) != _norm(artist):
        add(albumartist, title)

    split = _playlist_split_artist_title(title)
    if split:
        add(split[0], split[1])

    stem = Path(path_text).stem if path_text else ""
    for text in (stem, _playlist_strip_track_prefix(stem)):
        split = _playlist_split_artist_title(text)
        if split:
            add(split[0], split[1])
        elif text:
            add(artist, text)
    return pairs


def _playlist_payload_rank(payload: Dict[str, Any]) -> float:
    flags = set(payload.get("quality_flags") or [])
    rank = 0.0
    if payload.get("quality") == "ok":
        rank += 3.0
    elif payload.get("quality") == "review":
        rank += 1.0
    if "preview_risk" in flags or "missing_file" in flags:
        rank -= 20.0
    rank += min(float(payload.get("length") or 0) / 1000.0, 2.0)
    if payload.get("source") == "Library":
        rank += 1.0
    return rank


def _playlist_index_put_text(by_text: Dict[tuple, Dict[str, Any]], artist, title,
                             payload: Dict[str, Any]) -> None:
    key = (_norm(artist), _norm(title))
    if not key[1]:
        return
    current = by_text.get(key)
    if current is None or _playlist_payload_rank(payload) > _playlist_payload_rank(current):
        by_text[key] = payload


_PLAYLIST_CHANNEL_ARTIST_RE = re.compile(
    r"(?i)(?:"
    r"\bvevo\b|"
    r"\bofficial\b|"
    r"\b(?:records?|music|entertainment|media|films?|productions?|tv)\b$|"
    r"\s{1,20}[-–—]\s{0,20}topic$"
    r")"
)


# PR #109 independent review finding: the prior version of the last
# alternative, `[-–—]\s*topic$` (itself a fix for CodeQL's flagged
# `\s+-\s+topic$`, which was linear-scannable but unbounded), dropped the
# leading-whitespace requirement entirely instead of merely bounding it.
# That widened the match to any dash immediately followed by "topic" with
# NO space required before the dash at all -- so a plain artist/title
# string like "Artist-Topic" (no space) was misclassified as a YouTube
# auto-generated "<Artist> - Topic" channel name, which always has a real
# space before the dash (confirmed against real YouTube channel naming).
# Restoring `\s{1,20}` (bounded, not `\s+`) before the dash fixes the false
# positive without reintroducing the unbounded-whitespace-before-a-required-
# literal shape this whole Wave 28 pass exists to close: an unbounded
# `\s+`/`\s*` immediately before a required literal, scanned via an
# unanchored `.search()`, is quadratic for a long non-matching whitespace
# run (the identical class CodeQL flagged and this repo's other Wave 27/28
# playlist-regex fixes already bound the same way). `\s{0,20}` after the
# dash is unchanged in effect (real channel names use exactly one space)
# but bounded for the same reason, for consistency and defense in depth.

def _playlist_artist_looks_like_channel(artist: Any) -> bool:
    text = _s(artist).strip()
    if not text:
        return False
    return bool(_PLAYLIST_CHANNEL_ARTIST_RE.search(text))


def _playlist_artist_looks_like_uploader(artist: Any) -> bool:
    text = _playlist_clean_video_text(artist)
    if not text:
        return False
    norm = _norm(text)
    if _playlist_artist_looks_like_channel(text):
        return True
    if norm in _playlist_channel_artist_aliases():
        return True
    if re.search(r"[A-Za-z]", text) and re.search(r"\d", text) and " " not in text:
        return True
    if re.search(r"(?i)\b(?:muzik|vibes?|archive|uploads?|remaster(?:ed)?|slowed|screwed)\b", text):
        return True
    return False


def _playlist_channel_artist_aliases() -> Dict[str, str]:
    default_aliases = ",".join((
        "StarBoy TV=Wizkid",
        "SnoopDoggTV=Snoop Dogg",
        "BOBBYVtv=Bobby V",
        "112 Rebirth TV=112",
        "Ro James XIX=Ro James",
        "Mike Jones - Money Train LLC=Mike Jones",
    ))
    raw = os.environ.get("PLAYLIST_CHANNEL_ARTIST_ALIASES", default_aliases)
    aliases: Dict[str, str] = {}
    for part in re.split(r"[\n;,]+", raw):
        if "=" not in part:
            continue
        source, target = part.split("=", 1)
        source_key = _norm(source)
        target_name = _s(target).strip()
        if source_key and target_name:
            aliases[source_key] = target_name
    return aliases


def _playlist_primary_artist_name(artist: Any) -> str:
    text = _s(artist).strip()
    if not text:
        return ""
    text = re.split(r"\s*/\s*", text, maxsplit=1)[0].strip()
    text = re.split(r"\s+(?:feat\.?|ft\.?|featuring)\s+", text, maxsplit=1, flags=re.I)[0].strip()
    return text or _s(artist).strip()


def _playlist_canonicalize_track(track: Dict[str, Any]) -> Dict[str, Any]:
    original_artist = _playlist_clean_video_text(track.get("source_artist") or track.get("query_artist") or track.get("artist") or "")
    original_title = _playlist_clean_video_text(track.get("source_title") or track.get("query_title") or track.get("title") or "")
    artist = _playlist_clean_video_text(track.get("artist") or original_artist)
    raw_title = _playlist_clean_video_text(track.get("title") or original_title)
    existing_source = _s(track.get("canonical_source") or "").strip().lower()
    if existing_source in {"manual", "suggestion"} and raw_title:
        title = _playlist_strip_video_title_suffix(raw_title)
        variant_title_cleaned = False
    else:
        title, variant_title_cleaned = _playlist_clean_variant_title(raw_title)
    if not title:
        return dict(track)
    if track.get("canonicalized"):
        cleaned = dict(track)
        cleaned["artist"] = artist
        cleaned["title"] = title
        if variant_title_cleaned:
            cleaned["source_artist"] = cleaned.get("source_artist") or original_artist
            cleaned["source_title"] = cleaned.get("source_title") or original_title
            cleaned["canonicalized"] = True
            cleaned["canonical_source"] = "title-variant"
        return cleaned

    channelish = _playlist_artist_looks_like_channel(artist)
    uploaderish = _playlist_artist_looks_like_uploader(artist)
    split = _playlist_split_artist_title(title)
    split_source = ""
    if split and (channelish or uploaderish or not artist):
        source_artist = original_artist or artist
        source_title = original_title or title
        artist, title = split
        channelish = _playlist_artist_looks_like_channel(artist)
        split_source = "video-title"

    title_cleaned = variant_title_cleaned or title != _playlist_clean_video_text(track.get("title") or original_title)
    if not channelish and artist:
        row = {**track, "artist": artist, "title": title}
        if split_source or title_cleaned:
            row.update({
                "source_artist": source_artist if split_source else original_artist,
                "source_title": source_title if split_source else original_title,
                "canonicalized": True,
                "canonical_source": split_source or ("title-variant" if variant_title_cleaned else "title-cleanup"),
            })
        return row

    aliases = _playlist_channel_artist_aliases()
    alias_artist = aliases.get(_norm(original_artist)) or aliases.get(_norm(artist))
    if alias_artist:
        return {
            **track,
            "artist": alias_artist,
            "title": title,
            "source_artist": original_artist,
            "source_title": original_title,
            "canonicalized": True,
            "canonical_source": "channel-alias",
        }

    library_title_match = _match_track("", title)
    if library_title_match:
        item, score = library_title_match
        if float(score or 0) >= 0.93:
            return {
                **track,
                "artist": _s(getattr(item, "artist", "")).strip(),
                "title": _s(getattr(item, "title", "")).strip() or title,
                "source_artist": original_artist,
                "source_title": original_title,
                "canonicalized": True,
                "canonical_source": "library-title",
            }

    for cand in _mb_recording_search(title, "", limit=8) or []:
        cand_title = _s(cand.get("title") or "").strip()
        cand_artist = _playlist_primary_artist_name(cand.get("artist") or "")
        mb_score = int(cand.get("score") or 0)
        title_score = _playlist_title_score(title, cand_title)
        if cand_artist and cand_title and mb_score >= 90 and title_score >= 0.92:
            return {
                **track,
                "artist": cand_artist,
                "title": cand_title,
                "source_artist": original_artist,
                "source_title": original_title,
                "canonicalized": True,
                "canonical_source": "musicbrainz-title",
            }
    return {
        **track,
        "artist": artist,
        "title": title,
        **({"source_artist": original_artist, "source_title": original_title} if channelish else {}),
    }


def _playlist_canonicalize_tracks(tracks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [_playlist_canonicalize_track(track) for track in tracks or []]


def _playlist_candidate_match_score(artist, title, cand_artist, cand_title,
                                    quality: Dict[str, Any]) -> Optional[float]:
    t_norm = _norm(title)
    a_norm = _norm(artist)
    if not t_norm:
        return None
    ts = _playlist_title_score(title, cand_title)
    if a_norm:
        as_ = _playlist_artist_name_score(artist, cand_artist)
        if ts < 0.78 or as_ < 0.72:
            return None
        score = ts * 0.78 + as_ * 0.22
    else:
        if ts < 0.90:
            return None
        score = ts
    flags = set(quality.get("quality_flags") or [])
    if "preview_risk" in flags or "missing_file" in flags:
        score *= 0.68
    elif quality.get("quality") == "ok":
        score += 0.015
    return score


def _playlist_library_match_candidates() -> Dict[str, Any]:
    try:
        index = _playlist_library_index()
        match_rows = index.get("match_all")
        if match_rows is not None:
            return {
                "all": match_rows,
                "by_title": index.get("match_by_title") or {},
            }
    except Exception:
        pass

    candidates: List[Dict[str, Any]] = []
    by_title: Dict[str, List[Dict[str, Any]]] = {}
    for item in lib.items([]):
        path_text = _s(getattr(item, "path", ""))
        quality = _playlist_quality_for_item(item, path_text)
        payload = {
            "id": getattr(item, "id", 0),
            "title": getattr(item, "title", ""),
            "artist": getattr(item, "artist", ""),
            "album": getattr(item, "album", ""),
            "path": path_text,
        }
        payload.update(quality)
        seen: set = set()
        for cand_artist, cand_title in _playlist_item_text_variants(item):
            key = (_norm(cand_artist), _norm(cand_title))
            if not key[1] or key in seen:
                continue
            seen.add(key)
            row = {
                "artist": cand_artist,
                "title": cand_title,
                "quality": quality,
                "payload": payload,
            }
            candidates.append(row)
            by_title.setdefault(key[1], []).append(row)
    return {"all": candidates, "by_title": by_title}


def _playlist_match_payload_from_candidates(artist, title,
                                            candidates: Any) -> Optional[Dict[str, Any]]:
    best_payload: Optional[Dict[str, Any]] = None
    best_score = 0.0
    rows: List[Dict[str, Any]]
    if isinstance(candidates, dict):
        by_title = candidates.get("by_title") or {}
        rows = list(by_title.get(_norm(title), []) or [])
        if not rows:
            rows = list(candidates.get("all") or [])
    else:
        rows = list(candidates or [])
    for candidate in rows:
        score = _playlist_candidate_match_score(
            artist, title,
            candidate.get("artist", ""),
            candidate.get("title", ""),
            candidate.get("quality") or {})
        if score is None or score <= best_score:
            continue
        best_score = score
        best_payload = dict(candidate.get("payload") or {})
    if best_payload is None or best_score < 0.82:
        return None
    best_payload["query_artist"] = artist
    best_payload["query_title"] = title
    best_payload["score"] = round(best_score, 3)
    return best_payload


def _match_track(artist, title):
    """Fuzzy-match artist+title against the beets library. Returns best item or None."""
    t_norm = _norm(title)
    if not t_norm:
        return None
    payload = _playlist_match_payload_from_candidates(
        artist, title, _playlist_library_match_candidates())
    if not payload:
        return None
    item = type("PlaylistMatchedItem", (), {})()
    for key, value in payload.items():
        setattr(item, key, value)
    return item, round(float(payload.get("score") or 0), 3)


def _playlist_source_guess(item, path_text: str) -> str:
    album = _s(getattr(item, "album", "")).strip().lower()
    path_l = _s(path_text).replace("\\", "/").lower()
    provider_label = _playlist_provider_album_label(album)
    if provider_label:
        return provider_label
    if "soundcloud" in path_l:
        return "SoundCloud"
    if "spotiflac" in path_l:
        return "SpotiFLAC"
    if "playlist downloads" in path_l:
        return "Playlist download"
    if path_l.startswith("non-album/") or "/non-album/" in path_l:
        return "Playlist singleton"
    if "/playlist imports/" in path_l or path_l.startswith("playlist imports/"):
        return "Playlist import"
    if "various artists -  -" in path_l:
        return "Legacy playlist import"
    return "Library"


_PLAYLIST_PROVIDER_ALBUM_LABELS = {
    "soundcloud": "SoundCloud",
    "youtube": "YouTube",
    "spotify": "Spotify",
    "slskd": "slskd",
    "spotiflac": "SpotiFLAC",
    "soulseek": "Soulseek",
}


_PLAYLIST_BAD_ALBUM_VALUES = {
    *_PLAYLIST_PROVIDER_ALBUM_LABELS.keys(),
    "download",
    "downloads",
    "playlist",
    "playlist imports",
    "playlist downloads",
    "non album",
    "unknown",
}


_PLAYLIST_BAD_ALBUM_COMPACT_VALUES = {
    key.replace(" ", "") for key in _PLAYLIST_BAD_ALBUM_VALUES
}


def _playlist_album_value_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _s(value).strip().casefold()).strip()


def _playlist_album_value_is_bad_fallback(value: Any) -> bool:
    key = _playlist_album_value_key(value)
    return bool(
        key
        and (
            key in _PLAYLIST_BAD_ALBUM_VALUES
            or key.replace(" ", "") in _PLAYLIST_BAD_ALBUM_COMPACT_VALUES
        )
    )


def _playlist_provider_album_label(value: Any) -> str:
    key = _playlist_album_value_key(value)
    return _PLAYLIST_PROVIDER_ALBUM_LABELS.get(key, "")


def _playlist_quality_for_item(item, path_text: str) -> Dict[str, Any]:
    album = _s(getattr(item, "album", "") or "").strip()
    albumartist = _s(getattr(item, "albumartist", "") or "").strip()
    length = float(getattr(item, "length", 0) or 0)
    bitrate = int(getattr(item, "bitrate", 0) or 0)
    fmt = _s(getattr(item, "format", "") or Path(path_text).suffix.lstrip(".")).strip()
    path_l = _s(path_text).replace("\\", "/").lower()
    abs_path = _playlist_resolve_item_path(path_text)
    flags: List[str] = []

    if length > 0 and length < PLAYLIST_MIN_DOWNLOAD_SECONDS:
        flags.append("preview_risk")
    if not album:
        flags.append("blank_album")
    if _playlist_album_value_is_bad_fallback(album):
        flags.append("provider_album")
    if (
        "various artists -  -" in path_l
        or ("compilations/ (2016)" in path_l and not album)
        or path_l.startswith("non-album/")
        or "/non-album/" in path_l
        or path_l.startswith("playlist imports/")
        or "/playlist imports/" in path_l
    ):
        flags.append("bad_playlist_path")
    try:
        if path_text and not abs_path.exists():
            flags.append("missing_file")
    except Exception:
        flags.append("path_check_failed")

    quality = "ok"
    if "preview_risk" in flags or "missing_file" in flags:
        quality = "bad"
    elif flags:
        quality = "review"
    return {
        "quality": quality,
        "quality_flags": flags,
        "length": round(length, 1),
        "format": fmt,
        "bitrate": bitrate,
        "albumartist": albumartist,
        "source": _playlist_source_guess(item, path_text),
    }


def _playlist_item_payload(item, query_artist="", query_title="", score=None):
    path_text = _s(item.path)
    payload = {
        "query_artist": query_artist,
        "query_title": query_title,
        "id": item.id,
        "title": item.title,
        "artist": item.artist,
        "album": item.album,
        "path": path_text,
    }
    for field in (
        "albumartist", "year", "mb_albumid", "mb_releasegroupid",
        "mb_albumartistid", "mb_albumartistids", "mb_trackid",
    ):
        try:
            payload[field] = _s(getattr(item, field, "") or "")
        except Exception:
            payload[field] = ""
    payload.update(_playlist_quality_for_item(item, path_text))
    if score is not None:
        payload["score"] = score
    return payload


def _match_playlist_tracks(tracks, verify_acoustid: bool = False):
    matched, missing = [], []
    candidates = _playlist_library_match_candidates()
    for trk in _playlist_canonicalize_tracks(tracks):
        artist = (trk.get("artist") or "").strip()
        title = (trk.get("title") or "").strip()
        payload = _playlist_match_payload_from_candidates(artist, title, candidates)
        if payload:
            for key in ("source_artist", "source_title", "canonicalized", "canonical_source"):
                if key in trk:
                    payload[key] = trk.get(key)
            if verify_acoustid:
                path = _s(payload.get("path") or "").strip()
                if path:
                    payload["acoustid_status"] = _acoustid_verify_match(path, artist, title)
            matched.append(payload)
        else:
            row = {"artist": artist, "title": title}
            for key in ("source_artist", "source_title", "canonicalized", "canonical_source"):
                if key in trk:
                    row[key] = trk.get(key)
            missing.append(row)
    return matched, missing


def _clean_playlist_name(name):
    raw = _s(name).strip()
    raw = re.sub(r"[\x00-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", raw)
    cleaned = re.sub(r'[<>:"/\\|?*\0]', "_", raw).strip()
    while ".." in cleaned:
        cleaned = cleaned.replace("..", "_")
    cleaned = cleaned.strip("._ ")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = "Playlist"
    return cleaned[:120]


def _playlist_slug(value: Any) -> str:
    cleaned = re.sub(r"[\s_]+", "-", re.sub(r"[^\w\s-]", "", _s(value).lower())).strip("-")
    return cleaned or "playlist"


class PlaylistStateError(RuntimeError):
    """Raised when required persistent playlist identity state is unavailable.

    The .args message is for server logs only. It must never be echoed
    directly into a client-facing JSON response (SEC-002 Wave 10 second
    final review) -- use playlist_state_error_response()/
    _PLAYLIST_STATE_ERROR_MESSAGES instead, which map .code to a fixed,
    path-free explanation.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_PLAYLIST_STATE_ERROR_MESSAGES = {
    "playlist_state_unavailable": "Playlist state storage is currently unavailable.",
    "playlist_state_corrupt": "Playlist identity state is corrupt; manual review is required.",
    "checkpoint_corrupt": "Playlist checkpoint state is corrupt; manual review is required.",
    "manifest_corrupt": "Playlist manifest is corrupt; manual review is required.",
    "ambiguous_playlist": "Multiple playlists share this display name; specify playlist_id.",
    "invalid_playlist_id": "The supplied playlist_id is invalid.",
    "invalid_job_id": "The supplied playlist job id is invalid.",
    "playlist_identity_unresolved": "A persisted playlist_id is required for this operation.",
    "migration_conflict": "This playlist identity conflicts with existing state.",
}


def _playlist_state_error_payload(exc: "PlaylistStateError") -> Dict[str, Any]:
    """Safe, path-free client-facing body for a PlaylistStateError. Full
    detail (which may include a concrete state-directory path) goes to the
    server log at the raise/catch site instead, never into the response."""
    return {
        "ok": False,
        "error": _PLAYLIST_STATE_ERROR_MESSAGES.get(exc.code, "Playlist state is currently unavailable."),
        "error_code": exc.code,
    }


def _playlist_state_error_status(exc: "PlaylistStateError") -> int:
    if exc.code == "playlist_state_unavailable":
        return 503
    if exc.code == "ambiguous_playlist":
        return 409
    if exc.code == "migration_conflict":
        return 409
    if exc.code in ("invalid_playlist_id", "invalid_job_id"):
        return 404
    if exc.code in ("playlist_state_corrupt", "manifest_corrupt", "checkpoint_corrupt",
                    "playlist_identity_unresolved"):
        return 409
    return 500


_PLAYLIST_INTERNAL_ID_RE = re.compile(r"^pl_[0-9a-f]{32}$")


_PLAYLIST_STATE_LOCK = threading.RLock()


def _playlist_valid_internal_id(value: Any) -> bool:
    return bool(_PLAYLIST_INTERNAL_ID_RE.match(_s(value).strip()))


def _playlist_new_internal_id() -> str:
    return f"pl_{uuid.uuid4().hex}"


def _playlist_provider_parts(manifest: Optional[Dict[str, Any]] = None,
                             provider_id: Optional[str] = None) -> Tuple[str, str, str]:
    provider = ""
    external_id = ""
    if provider_id:
        raw = _s(provider_id).strip()
        if ":" in raw:
            provider, external_id = raw.split(":", 1)
        else:
            external_id = raw
    if isinstance(manifest, dict):
        provider = _s(manifest.get("provider") or manifest.get("source") or provider).strip().lower()
        external_id = _s(
            manifest.get("provider_playlist_id")
            or manifest.get("provider_id")
            or manifest.get("external_id")
            or external_id
        ).strip()
    provider = _playlist_slug(provider).lower() if provider else ""
    external_id = external_id.strip()
    provider_identity = f"{provider}:{external_id}" if provider and external_id else ""
    return provider, external_id, provider_identity


def _playlist_ensure_state_dirs() -> None:
    for d in (PLAYLIST_STATE_ROOT, PLAYLIST_MANIFESTS_DIR, PLAYLIST_JOB_STATE_DIR, PLAYLIST_EXPORTS_DIR, PLAYLIST_MEMBERSHIP_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            # The concrete path belongs in the server log only -- it must
            # never reach a client-facing response (SEC-002 Wave 10 second
            # final review); the exception itself carries just the code and
            # a generic message.
            _app_logger.warning("Playlist state directory is unavailable: %s (%s)", d, type(exc).__name__)
            raise PlaylistStateError(
                "playlist_state_unavailable",
                "Playlist state directory is unavailable.",
            ) from exc


def _playlist_load_index() -> Dict[str, Dict[str, Any]]:
    _playlist_ensure_state_dirs()
    index_path = PLAYLIST_STATE_ROOT / "index.json"
    if not index_path.exists():
        return {}
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PlaylistStateError(
            "playlist_state_corrupt",
            "Playlist identity state is corrupt; manual review is required.",
        ) from exc
    if not isinstance(data, dict):
        raise PlaylistStateError(
            "playlist_state_corrupt",
            "Playlist identity state has an invalid schema; manual review is required.",
        )

    seen_provider: Dict[str, str] = {}
    out: Dict[str, Dict[str, Any]] = {}
    for raw_pid, raw_entry in data.items():
        pid = _s(raw_pid).strip()
        if not _playlist_valid_internal_id(pid):
            raise PlaylistStateError(
                "playlist_state_corrupt",
                "Playlist identity state contains an invalid playlist_id.",
            )
        if not isinstance(raw_entry, dict):
            raise PlaylistStateError(
                "playlist_state_corrupt",
                "Playlist identity state has an invalid playlist entry.",
            )
        entry = dict(raw_entry)
        entry_pid = _s(entry.get("playlist_id") or pid).strip()
        if entry_pid != pid or not _playlist_valid_internal_id(entry_pid):
            raise PlaylistStateError(
                "playlist_state_corrupt",
                "Playlist identity state contains a mismatched playlist_id.",
            )
        provider_identity = _s(entry.get("provider_identity") or "").strip()
        if not provider_identity:
            provider = _s(entry.get("provider") or "").strip().lower()
            external_id = _s(entry.get("provider_playlist_id") or "").strip()
            provider_identity = f"{provider}:{external_id}" if provider and external_id else ""
        if provider_identity:
            existing = seen_provider.get(provider_identity)
            if existing and existing != pid:
                raise PlaylistStateError(
                    "playlist_state_corrupt",
                    "Playlist identity state contains a duplicate provider identity.",
                )
            seen_provider[provider_identity] = pid
            entry["provider_identity"] = provider_identity
        entry["playlist_id"] = pid
        out[pid] = entry
    if len(out) != len(data):
        raise PlaylistStateError(
            "playlist_state_corrupt",
            "Playlist identity state contains duplicate playlist IDs.",
        )
    return out


def _playlist_save_index(index_data: Dict[str, Dict[str, Any]]) -> None:
    _playlist_ensure_state_dirs()
    index_path = PLAYLIST_STATE_ROOT / "index.json"
    _playlist_atomic_json_replace(index_path, index_data, save_key="playlist-index")


def _playlist_entry_matches_name(entry: Dict[str, Any], name: str) -> bool:
    clean = _clean_playlist_name(name)
    return (
        _s(entry.get("name") or "") == name
        or _s(entry.get("clean_name") or "") == clean
    )


def _playlist_find_id_by_name(index_data: Dict[str, Dict[str, Any]], name: str) -> str:
    matches = [
        pid
        for pid, entry in index_data.items()
        if isinstance(entry, dict) and _playlist_entry_matches_name(entry, name)
    ]
    if len(matches) > 1:
        raise PlaylistStateError(
            "ambiguous_playlist",
            "Multiple playlists share this display name; use playlist_id.",
        )
    return matches[0] if matches else ""


def _playlist_find_id_by_provider(index_data: Dict[str, Dict[str, Any]],
                                  provider_identity: str) -> str:
    if not provider_identity:
        return ""
    for pid, entry in index_data.items():
        if isinstance(entry, dict) and _s(entry.get("provider_identity") or "") == provider_identity:
            return pid
    return ""


def _playlist_index_entry(name: str,
                          pid: str,
                          manifest: Optional[Dict[str, Any]] = None,
                          provider_id: Optional[str] = None) -> Dict[str, Any]:
    provider, external_id, provider_identity = _playlist_provider_parts(manifest, provider_id)
    entry = {
        "playlist_id": pid,
        "clean_name": _clean_playlist_name(name),
        "name": (manifest or {}).get("name") or name,
        "updated_at": time.time(),
    }
    if provider_identity:
        entry.update({
            "provider": provider,
            "provider_playlist_id": external_id,
            "provider_identity": provider_identity,
        })
    return entry


def _playlist_resolve_stable_id(name: str,
                                manifest: Optional[Dict[str, Any]] = None,
                                provider_id: Optional[str] = None,
                                playlist_id: Optional[str] = None) -> str:
    if playlist_id:
        pid = _s(playlist_id).strip()
        if _playlist_valid_internal_id(pid):
            return pid
        raise PlaylistStateError("invalid_playlist_id", "Invalid playlist_id.")

    if isinstance(manifest, dict):
        for field in ("playlist_id", "id", "uuid"):
            val = _s(manifest.get(field)).strip()
            if _playlist_valid_internal_id(val):
                return val

    _provider, _external_id, provider_identity = _playlist_provider_parts(manifest, provider_id)
    with _PLAYLIST_STATE_LOCK:
        index_data = _playlist_load_index()
        provider_match = _playlist_find_id_by_provider(index_data, provider_identity)
        if provider_match:
            return provider_match
        return _playlist_find_id_by_name(index_data, name)


def _playlist_ensure_stable_id(name: str,
                               manifest: Optional[Dict[str, Any]] = None,
                               provider_id: Optional[str] = None,
                               playlist_id: Optional[str] = None) -> str:
    manifest = manifest if isinstance(manifest, dict) else {}
    with _PLAYLIST_STATE_LOCK:
        index_data = _playlist_load_index()

        pid = ""
        if playlist_id:
            candidate = _s(playlist_id).strip()
            if not _playlist_valid_internal_id(candidate):
                raise PlaylistStateError("invalid_playlist_id", "Invalid playlist_id.")
            pid = candidate
        else:
            for field in ("playlist_id", "id", "uuid"):
                candidate = _s(manifest.get(field)).strip()
                if _playlist_valid_internal_id(candidate):
                    pid = candidate
                    break

        provider, external_id, provider_identity = _playlist_provider_parts(manifest, provider_id)
        if not pid:
            pid = _playlist_find_id_by_provider(index_data, provider_identity)
        if not pid:
            pid = _playlist_new_internal_id()

        entry = dict(index_data.get(pid) or {})
        if entry:
            provider_existing = _s(entry.get("provider_identity") or "")
            if provider_identity and provider_existing and provider_existing != provider_identity:
                raise PlaylistStateError(
                    "migration_conflict",
                    "Playlist identity provider mapping conflicts with existing state.",
                )
            entry.update(_playlist_index_entry(name, pid, manifest, provider_id))
            entry.setdefault("created_at", time.time())
        else:
            entry = _playlist_index_entry(name, pid, manifest, provider_id)
            entry["created_at"] = time.time()
        if provider_identity:
            existing = _playlist_find_id_by_provider(index_data, provider_identity)
            if existing and existing != pid:
                raise PlaylistStateError(
                    "migration_conflict",
                    "Provider playlist identity is already mapped to another playlist.",
                )
        index_data[pid] = entry
        _playlist_save_index(index_data)
        manifest["playlist_id"] = pid
        if provider_identity:
            manifest["provider"] = provider
            manifest["provider_playlist_id"] = external_id
        return pid


def _playlist_stable_id(name: str,
                        manifest: Optional[Dict[str, Any]] = None,
                        provider_id: Optional[str] = None,
                        playlist_id: Optional[str] = None) -> str:
    return _playlist_ensure_stable_id(name, manifest=manifest, provider_id=provider_id, playlist_id=playlist_id)


def _playlist_key(name: str,
                  manifest: Optional[Dict[str, Any]] = None,
                  provider_id: Optional[str] = None,
                  playlist_id: Optional[str] = None,
                  *,
                  allocate: bool = True) -> str:
    if playlist_id:
        pid = _s(playlist_id).strip()
    elif allocate:
        pid = _playlist_resolve_stable_id(name, manifest=manifest, provider_id=provider_id)
        if not pid:
            pid = _playlist_ensure_stable_id(name, manifest=manifest, provider_id=provider_id)
    else:
        pid = _playlist_resolve_stable_id(name, manifest=manifest, provider_id=provider_id)
    if not _playlist_valid_internal_id(pid):
        raise PlaylistStateError("invalid_playlist_id", "Invalid playlist_id.")
    safe_pid = re.sub(r"[^a-zA-Z0-9_-]", "_", pid)
    h = hashlib.sha256(pid.encode("utf-8")).hexdigest()[:12]
    return f"{safe_pid}_{h}"


def _playlist_existing_key(name: str,
                           manifest: Optional[Dict[str, Any]] = None,
                           playlist_id: Optional[str] = None) -> str:
    try:
        return _playlist_key(name, manifest=manifest, playlist_id=playlist_id, allocate=False)
    except PlaylistStateError:
        return ""


def _valid_playlist_key(value: Any) -> bool:
    """Format check for valid playlist key. Used to make sure web-manager never
    treats a raw playlist_id (or any other unvalidated string) as if it were already a
    derived, filesystem-safe playlist_key."""
    text = _s(value).strip()
    return bool(text) and bool(re.fullmatch(r"[a-zA-Z0-9_.-]{1,160}", text)) and ".." not in text


def _playlist_resolve_operation_key(playlist_key: Any = "",
                                    playlist_id: Any = "",
                                    clean_name: str = "",
                                    *,
                                    log: Any = None) -> str:
    """Strictly resolve the filesystem playlist_key for a playlist
    operation.

    playlist_key and playlist_id are NOT interchangeable: playlist_key is a
    derived, per-playlist_id filesystem identifier (see _playlist_key()),
    never a raw playlist_id and never a shared "pl_default" bucket. A
    candidate/payload that supplies neither a valid playlist_key nor a
    valid, resolvable playlist_id gets "" back and the caller MUST refuse
    the operation -- silently falling back to a name-only lookup (or a
    shared default key) risks operating on a different playlist's staged
    files or library items than the one the caller actually meant.
    """
    key = _s(playlist_key).strip()
    if key and _valid_playlist_key(key):
        return key

    pid = _s(playlist_id).strip()
    if pid and _playlist_valid_internal_id(pid):
        resolved = _playlist_existing_key(clean_name, playlist_id=pid)
        if resolved and _valid_playlist_key(resolved):
            return resolved
        if log is not None:
            _playlist_log_line(log, f"  [playlist-key] Could not resolve playlist_key for playlist_id {pid}")
        return ""

    if log is not None:
        _playlist_log_line(log, "  [playlist-key] Missing or invalid playlist_key/playlist_id")
    return ""


def get_playlist_staging_root(playlist: Any) -> Path:
    """Stable per-playlist staging root; never includes job/run IDs."""
    if isinstance(playlist, dict):
        manifest = playlist
        name = playlist.get("name") or playlist.get("playlist_name") or playlist.get("playlist") or "Playlist"
    else:
        manifest = None
        name = playlist
    get_key = _playlist_key
    key = get_key(_s(name), manifest) if get_key else _playlist_slug(_clean_playlist_name(_s(name)))
    return PLAYLIST_DOWNLOAD_ROOT / key


def _playlist_staging_root_arg(name: str, playlist_id: Optional[str] = None) -> Any:
    return {"name": name, "playlist_id": playlist_id} if playlist_id else name


def _playlist_staging_dir(name: str, playlist_id: Optional[str] = None) -> Path:
    return get_playlist_staging_root(_playlist_staging_root_arg(name, playlist_id))


def _playlist_downloads_dir(name: str, playlist_id: Optional[str] = None) -> Path:
    return get_playlist_staging_root(_playlist_staging_root_arg(name, playlist_id)) / "downloads"


def _playlist_imports_dir(name: str, playlist_id: Optional[str] = None) -> Path:
    return get_playlist_staging_root(_playlist_staging_root_arg(name, playlist_id)) / "imports"


def _playlist_staging_manifest_path(name: str, playlist_id: Optional[str] = None) -> Path:
    return get_playlist_staging_root(_playlist_staging_root_arg(name, playlist_id)) / "manifest.json"


class PlaylistStagingUnavailableError(RuntimeError):
    """Raised when the Beets engine cannot confirm playlist staging directories exist.

    The web manager owns no writable media/staging filesystem in the supported
    two-service topology (only /web-manager-data is mounted, and it is not
    PLAYLIST_DOWNLOAD_ROOT). There is deliberately no local mkdir/chmod fallback
    here: a fallback that quietly wrote to a path the container cannot actually
    reach in production is not resilience, it is a false "ok" for an operation
    that did nothing (SEC-002 Wave 9 final review).
    """
    code = "staging_unavailable"


class PlaylistQualityCandidatesUnavailableError(RuntimeError):
    """Raised when the Beets engine cannot be queried for playlist quality
    candidates.

    Returning an empty candidate list on engine/IPC failure would be
    indistinguishable from "the engine was queried and the library is
    clean" -- a false negative that could hide real quality issues from an
    operator running a cleanup scan. Callers must surface this as a failed
    scan, not a clean one.
    """
    code = "quality_candidates_unavailable"


def _playlist_ensure_staging_dirs(name: str, playlist_id: Optional[str] = None) -> None:
    clean_name = _clean_playlist_name(name)
    get_key = _playlist_key
    key = get_key(name, {"playlist_id": playlist_id} if playlist_id else None) if get_key else clean_name
    root = get_playlist_staging_root(_playlist_staging_root_arg(name, playlist_id))
    staging_base = PLAYLIST_DOWNLOAD_ROOT.resolve(strict=False)
    resolved_root = root.resolve(strict=False)
    try:
        resolved_root.relative_to(staging_base)
    except ValueError:
        raise ValueError("Playlist staging root is outside allowed staging directory")

    try:
        res = composite_workflows.ensure_playlist_staging(key, playlist_id or "", name)
    except Exception as exc:
        raise PlaylistStagingUnavailableError(
            "Engine is unavailable; cannot ensure playlist staging directories"
        ) from exc
    if not (isinstance(res, dict) and res.get("ok")):
        raise PlaylistStagingUnavailableError(
            "Engine did not confirm playlist staging directories"
        )


def _playlist_legacy_staging_dirs(name: str) -> List[Path]:
    clean_name = _clean_playlist_name(name)
    stable_root = get_playlist_staging_root(clean_name).resolve(strict=False)
    if not PLAYLIST_DOWNLOAD_ROOT.exists():
        return []
    prefixes = {f"{clean_name} - "}
    dirs: List[Path] = []
    for path in PLAYLIST_DOWNLOAD_ROOT.iterdir():
        if not path.is_dir():
            continue
        try:
            if path.resolve(strict=False) == stable_root:
                continue
        except Exception:
            pass
        if any(path.name.startswith(prefix) for prefix in prefixes):
            dirs.append(path)
    return sorted(dirs, key=lambda item: item.name.lower())


def _playlist_unique_staging_destination(dest_dir: Path, filename: str) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    base = dest_dir / filename
    if not base.exists():
        return base
    stem = base.stem
    suffix = base.suffix
    for idx in range(2, 1000):
        candidate = dest_dir / f"{stem} ({idx}){suffix}"
        if not candidate.exists():
            return candidate
    return dest_dir / f"{stem}-{int(time.time())}{suffix}"


def _playlist_normalize_staged_file(name: str, path: Path, log=None) -> Path:
    source = Path(path)
    downloads_dir = _playlist_downloads_dir(name)
    try:
        resolved = source.resolve(strict=False)
        if _path_is_under(resolved, downloads_dir.resolve(strict=False)):
            return source
        if _path_is_under(resolved, MUSIC_ROOT.resolve(strict=False)):
            return source
        if not _path_is_under(resolved, PLAYLIST_DOWNLOAD_ROOT.resolve(strict=False)):
            return source
    except Exception:
        return source
    if not source.exists() or not source.is_file():
        return source
    destination = downloads_dir / source.name
    if destination.exists():
        try:
            if destination.stat().st_size == source.stat().st_size:
                if log:
                    log(f"  Reconcile: skipped duplicate staged file already in downloads/: {source.name}")
                return destination
        except Exception:
            pass
        destination = _playlist_unique_staging_destination(downloads_dir, source.name)
    try:
        downloads_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        if log:
            log(f"  Reconcile: moved staged file into stable downloads/: {destination.name}")
        return destination
    except Exception as exc:
        if log:
            log(f"  Reconcile: could not move staged file {source.name}: {exc}")
        return source


def _playlist_sync_staging_manifest(name: str, manifest: Dict[str, Any], *, log=None) -> None:
    clean_name = _clean_playlist_name(name)
    payload_manifest = dict(manifest or {})
    states = payload_manifest.get("track_states") if isinstance(payload_manifest.get("track_states"), dict) else {}
    desired = _playlist_clean_track_list(payload_manifest.get("desired_tracks") or [])
    rows: Dict[str, Dict[str, Any]] = {}
    for track in desired:
        key = _playlist_status_id(track)
        state = states.get(key) if isinstance(states.get(key), dict) else {}
        row = {**_playlist_track_manifest_payload(track), **state}
        row["id"] = key
        row["status"] = _s(row.get("status") or "missing")
        rows[key] = row
    for key, state in states.items():
        if isinstance(state, dict) and key not in rows:
            row = dict(state)
            row["id"] = _s(row.get("id") or key)
            rows[key] = row
    payload = {
        "version": 1,
        "playlist_id": _playlist_stable_id(clean_name, payload_manifest),
        "playlist_name": clean_name,
        "staging_root": str(get_playlist_staging_root(clean_name)),
        "downloads_dir": str(_playlist_downloads_dir(clean_name)),
        "imports_dir": str(_playlist_imports_dir(clean_name)),
        "updated_at": time.time(),
        "tracks": list(rows.values()),
    }
    try:
        _playlist_ensure_staging_dirs(clean_name)
        _playlist_atomic_json_replace(
            _playlist_staging_manifest_path(clean_name),
            payload,
            save_key=f"{_playlist_slug(clean_name)}.staging",
            label="playlist staging manifest",
            indent=2,
            sort_keys=True,
        )
    except Exception as exc:
        if log is not None:
            log.append(f"  [playlist] staging manifest save failed: {exc}")


def _playlist_staging_payload(name: str, manifest: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    clean_name = _clean_playlist_name(name)
    return {
        "playlist_id": _playlist_stable_id(clean_name, manifest),
        "staging_root": str(get_playlist_staging_root(clean_name)),
        "staging_downloads": str(_playlist_downloads_dir(clean_name)),
        "staging_imports": str(_playlist_imports_dir(clean_name)),
        "staging_manifest": str(_playlist_staging_manifest_path(clean_name)),
        "old_staging_folder_count": len(_playlist_legacy_staging_dirs(clean_name)),
    }


def _plex_playlist_uri(machine_id: str, keys: List[str]) -> str:
    return (
        f"server://{machine_id}/com.plexapp.plugins.library"
        f"/library/metadata/{','.join(keys)}"
    )


def _plex_create_audio_playlist(machine_id: str, title: str, keys: List[str],
                                log=None) -> Tuple[int, str]:
    """Create (or append to) a Plex audio playlist. Returns (tracks_added,
    rating_key) -- the rating key is this Plex playlist's own stable
    identity and must be persisted by the caller (SEC-002 Wave 10 second
    final review) so a later same-titled app playlist cannot be confused
    with this one; discarding it here is what forced every prior delete/
    replace operation to fall back to title-based matching."""
    chunks = [
        keys[index:index + PLEX_PLAYLIST_CHUNK_SIZE]
        for index in range(0, len(keys), PLEX_PLAYLIST_CHUNK_SIZE)
    ]
    if not chunks:
        return 0, ""
    created = _plex_request("/playlists", {
        "type": "audio",
        "title": title,
        "smart": 0,
        "uri": _plex_playlist_uri(machine_id, chunks[0]),
    }, method="POST", timeout=PLEX_API_TIMEOUT)
    playlist_key = ""
    metadata = created.get("MediaContainer", {}).get("Metadata", []) if isinstance(created, dict) else []
    if metadata:
        playlist_key = _s(metadata[0].get("ratingKey") or metadata[0].get("key") or "").strip()
    playlist_key = playlist_key.strip("/").split("/")[-1] if playlist_key else ""
    added = len(chunks[0])
    for chunk in chunks[1:]:
        if not playlist_key:
            raise RuntimeError("Plex playlist was created but no playlist key was returned for chunked append")
        _plex_request(f"/playlists/{playlist_key}/items", {
            "uri": _plex_playlist_uri(machine_id, chunk),
        }, method="PUT", timeout=PLEX_API_TIMEOUT)
        added += len(chunk)
    if log is not None and len(chunks) > 1:
        log.append(f"  [plex] Added playlist tracks in {len(chunks)} chunks")
    return added, playlist_key


def _plex_delete_playlist_by_rating_key(rating_key: str, log=None) -> int:
    """Delete exactly one Plex playlist by its own stable ratingKey -- the
    unambiguous, preferred deletion authority (SEC-002 Wave 10 second final
    review). Unlike title-based matching this cannot collide with another
    app playlist that happens to share a display name."""
    key = _s(rating_key).strip()
    if not key:
        return 0
    path = key.split("?", 1)[0] if key.startswith("/playlists/") else f"/playlists/{key}"
    try:
        _plex_request(path, method="DELETE", timeout=10)
    except Exception as exc:
        if log is not None:
            log.append(f"  [plex] Could not delete playlist by ratingKey {key!r}: {exc}")
        return 0
    if log is not None:
        log.append(f"  [plex] Deleted playlist by ratingKey: {key}")
    return 1


def _plex_playlist_candidates_by_title(title: str) -> List[Tuple[Dict[str, Any], str]]:
    candidates = []
    for pl in _plex_audio_playlists():
        if _norm(pl.get("title", "")) != _norm(title):
            continue
        if str(pl.get("smart", "0")).strip().lower() in {"1", "true", "yes"}:
            continue
        key = str(pl.get("ratingKey") or pl.get("key") or "").strip().strip("/").split("/")[-1]
        if key:
            candidates.append((pl, key))
    return candidates


def _plex_delete_playlist_by_title_unambiguous(title: str, log=None) -> Tuple[int, str]:
    """Delete a Plex playlist by title only if exactly one candidate exists on Plex.
    Returns (deleted_count, error_code). If >1 candidate exists, fails closed
    with (0, 'ambiguous_plex_playlist') to prevent cross-targeting (SEC-002 Wave 11)."""
    candidates = _plex_playlist_candidates_by_title(title)
    if not candidates:
        return 0, ""
    if len(candidates) > 1:
        if log is not None:
            log.append(
                f"  [plex] Refusing title-based delete for {title!r}: "
                f"{len(candidates)} matching Plex playlists found (ambiguous_plex_playlist)"
            )
        return 0, "ambiguous_plex_playlist"

    pl, key = candidates[0]
    return _plex_delete_playlist_by_rating_key(key, log=log), ""


def _plex_delete_playlist_by_title(title, log=None):
    """Legacy/fallback authority only -- deletes a single matching Plex playlist by title
    only if exactly one exists on Plex (SEC-002 Wave 11)."""
    deleted, _ = _plex_delete_playlist_by_title_unambiguous(title, log=log)
    return deleted


def _playlist_other_live_pids_with_name(clean_name: str, exclude_pid: str = "") -> List[str]:
    """Return internal playlist_ids (other than exclude_pid) whose index
    entry currently resolves to this same cleaned display name. Used to
    detect same-name ambiguity before falling back to Plex title-based
    matching (SEC-002 Wave 10 second final review)."""
    try:
        index_data = _playlist_load_index()
    except PlaylistStateError:
        return []
    others = []
    for pid, entry in index_data.items():
        if pid == exclude_pid or not isinstance(entry, dict):
            continue
        if _playlist_entry_matches_name(entry, clean_name):
            others.append(pid)
    return others


def _plex_replace_playlist_safely(name: str,
                                  pid: str,
                                  previous_manifest: Optional[Dict[str, Any]],
                                  log=None) -> Dict[str, Any]:
    """Delete/replace this playlist's own existing Plex playlist before a
    fresh sync, without risking another same-named app playlist's Plex
    playlist (SEC-002 Wave 10 second final review). Prefers a previously
    stored ratingKey; only falls back to legacy title-based replacement
    when no other live app playlist shares this display name and exactly
    one Plex playlist exists with this title (SEC-002 Wave 11)."""
    result = {"replaced": 0, "ambiguous": False}
    prior_rating_key = ""
    if isinstance(previous_manifest, dict):
        prior_rating_key = _s((previous_manifest.get("last_plex") or {}).get("rating_key") or "").strip()
    if prior_rating_key:
        result["replaced"] = _plex_delete_playlist_by_rating_key(prior_rating_key, log=log)
        return result
    others = _playlist_other_live_pids_with_name(_clean_playlist_name(name), exclude_pid=pid)
    if others:
        result["ambiguous"] = True
        if log is not None:
            log.append(
                f"  [plex] Skipping title-based Plex replace for {name!r}: "
                f"{len(others)} other playlist(s) share this name and have no stored ratingKey"
            )
        return result
    deleted, err = _plex_delete_playlist_by_title_unambiguous(name, log=log)
    result["replaced"] = deleted
    if err:
        result["ambiguous"] = err == "ambiguous_plex_playlist"
        result["error"] = err
    return result


def _playlist_manifest_path(name: str,
                            manifest: Optional[Dict[str, Any]] = None,
                            *,
                            allocate: bool = True) -> Path:
    clean_name = _clean_playlist_name(name)
    _playlist_ensure_state_dirs()
    pid = ""
    if isinstance(manifest, dict):
        pid = _s(manifest.get("playlist_id") or manifest.get("id") or manifest.get("uuid") or "").strip()
    if not _playlist_valid_internal_id(pid):
        try:
            pid = _playlist_resolve_stable_id(clean_name, manifest, playlist_id=pid or None)
        except PlaylistStateError as exc:
            if exc.code not in {"invalid_playlist_id", "ambiguous_playlist"}:
                raise
            pid = ""
    if _playlist_valid_internal_id(pid):
        key = _playlist_key(clean_name, playlist_id=pid, allocate=False)
        for candidate in (
            PLAYLIST_MANIFESTS_DIR / f"{pid}.playlist.json",
            PLAYLIST_MANIFESTS_DIR / f"{key}.playlist.json",
            PLAYLIST_MANIFESTS_DIR / f"{clean_name}.playlist.json",
        ):
            if candidate.exists():
                return candidate
        return PLAYLIST_MANIFESTS_DIR / f"{key}.playlist.json"
    legacy = PLAYLIST_MANIFESTS_DIR / f"{clean_name}.playlist.json"
    if legacy.exists() or not allocate:
        return legacy
    pid = _playlist_ensure_stable_id(clean_name, manifest if isinstance(manifest, dict) else {})
    key = _playlist_key(clean_name, playlist_id=pid, allocate=False)
    return PLAYLIST_MANIFESTS_DIR / f"{key}.playlist.json"


def _playlist_manifest_exists_no_create(name: str, manifest: Optional[Dict[str, Any]] = None) -> bool:
    try:
        return _playlist_manifest_path(name, manifest, allocate=False).exists()
    except PlaylistStateError:
        return False


_PLAYLIST_MANIFEST_LOCKS: Dict[str, Any] = {}


_PLAYLIST_MANIFEST_LOCKS_GUARD = threading.Lock()


def _playlist_manifest_lock(name: str):
    clean_name = _clean_playlist_name(name)
    with _PLAYLIST_MANIFEST_LOCKS_GUARD:
        lock = _PLAYLIST_MANIFEST_LOCKS.get(clean_name)
        if lock is None:
            lock = threading.RLock()
            _PLAYLIST_MANIFEST_LOCKS[clean_name] = lock
        return lock


def _playlist_atomic_json_replace(path: Path,
                                  payload: Dict[str, Any],
                                  *,
                                  save_key: str = "",
                                  log=None,
                                  label: str = "playlist JSON",
                                  indent: Optional[int] = 2,
                                  sort_keys: bool = False) -> None:
    path = Path(path)
    resolved_path = path.resolve(strict=False)
    # This JSON helper is for web-manager-owned state only. Engine-owned
    # media/playlist directories are deliberately not allowed here.
    allowed_roots = []
    # ARCH-001: explicit references (a globals() lookup silently lost these
    # roots once the helper left app.py's namespace).
    for val in (WEB_MANAGER_DATA_DIR, PLAYLIST_STATE_ROOT, PLAYLIST_MANIFESTS_DIR, PLAYLIST_JOB_STATE_DIR, PLAYLIST_MEMBERSHIP_DIR):
        if val is not None:
            try:
                allowed_roots.append(Path(val).resolve(strict=False))
            except Exception:
                pass
    safe = False
    for root in allowed_roots:
        try:
            get_is_under = _path_is_under
            if (get_is_under and get_is_under(resolved_path, root)) or resolved_path.parent.resolve(strict=False) == root.resolve(strict=False):
                safe = True
                break
            resolved_path.relative_to(root)
            safe = True
            break
        except ValueError:
            pass
    if not safe:
        raise ValueError(f"Refusing atomic write to unsafe path outside state roots: {path}")

    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    safe_key = re.sub(r"[^a-zA-Z0-9_.-]+", "_", _s(save_key).strip())[:80]
    if not safe_key:
        safe_key = str(threading.get_ident())
    tmp = parent / f"{path.stem}.{safe_key}.{int(time.time() * 1000)}.{uuid.uuid4().hex[:8]}.tmp"
    resolved_tmp = tmp.resolve(strict=False)
    tmp_safe = False
    for root in allowed_roots:
        try:
            resolved_tmp.relative_to(root)
            tmp_safe = True
            break
        except ValueError:
            pass
    if not tmp_safe:
        raise ValueError(f"Refusing atomic write with temporary file outside state roots: {tmp}")

    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=indent, sort_keys=sort_keys, default=str)
            handle.write("\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        if not tmp.exists():
            raise RuntimeError(f"temporary file was not created: {tmp}")
        for attempt in range(8):
            try:
                tmp.replace(path)
                break
            except PermissionError:
                if attempt >= 7:
                    raise
                time.sleep(0.025 * (attempt + 1))
        if not path.exists():
            raise RuntimeError(f"final file was not created: {path}")
    except Exception as exc:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        if log is not None:
            log.append(f"  [playlist] {label} save failed for {path.name}: {exc}")
        raise


def _playlist_track_manifest_payload(track: Dict[str, Any]) -> Dict[str, Any]:
    original_artist = _playlist_clean_video_text(track.get("source_artist") or track.get("query_artist") or track.get("artist") or "")
    original_title = _playlist_clean_video_text(track.get("source_title") or track.get("query_title") or track.get("title") or "")
    artist = _playlist_clean_video_text(track.get("artist") or original_artist)
    raw_title = _playlist_clean_video_text(track.get("title") or original_title)
    source_artist = original_artist
    source_title = original_title
    canonicalized = bool(track.get("canonicalized"))
    canonical_source = _s(track.get("canonical_source") or "").strip()
    if canonical_source.lower() in {"manual", "suggestion"} and raw_title:
        title = _playlist_strip_video_title_suffix(raw_title)
        variant_title_cleaned = False
    else:
        title, variant_title_cleaned = _playlist_clean_variant_title(raw_title)
    split = _playlist_split_artist_title(title)
    if split and (_playlist_artist_looks_like_uploader(artist) or not artist):
        source_artist = original_artist or artist
        source_title = original_title or title
        artist, title = split
        canonicalized = True
        canonical_source = "video-title"
    elif variant_title_cleaned:
        canonicalized = True
        canonical_source = "title-variant"
    elif title != _playlist_clean_video_text(track.get("title") or original_title):
        canonicalized = True
        canonical_source = canonical_source or "title-cleanup"
    aliases = _playlist_channel_artist_aliases()
    alias_artist = aliases.get(_norm(original_artist)) or aliases.get(_norm(artist))
    if alias_artist and title:
        artist = alias_artist
        canonicalized = True
        canonical_source = "channel-alias"
    row = {
        "artist": artist,
        "title": title,
    }
    if canonicalized:
        row["source_artist"] = source_artist
        row["source_title"] = source_title or title
        row["canonicalized"] = True
        row["canonical_source"] = canonical_source
    path = _s(track.get("path") or "").strip()
    if path:
        row["path"] = path
    for key in (
        "source_artist", "source_title", "canonicalized", "canonical_source",
        "source", "source_url", "mb_trackid", "mb_albumid",
        "mb_releasegroupid", "duration",
    ):
        if key in track and key not in row:
            row[key] = track.get(key)
    return row


def _playlist_clean_track_list(tracks: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for track in tracks or []:
        row = _playlist_track_manifest_payload(track)
        if row.get("title"):
            out.append(row)
    return out


def _playlist_manifest_identity(track: Dict[str, Any]) -> tuple:
    artist = _norm(track.get("artist") or track.get("query_artist") or "")
    title = _norm(track.get("title") or track.get("query_title") or "")
    if title:
        return ("text", artist, title)
    path = _s(track.get("path") or "").replace("\\", "/").casefold()
    return ("path", path) if path else ("empty", artist, title)


def _playlist_manifest_match_keys(track: Dict[str, Any]) -> set:
    keys = set()
    row = _playlist_track_manifest_payload(track)
    artist = _norm(row.get("artist") or row.get("query_artist") or "")
    title = _norm(row.get("title") or row.get("query_title") or "")
    if title:
        keys.add(("text", artist, title))
    source_artist = _norm(row.get("source_artist") or "")
    source_title = _norm(row.get("source_title") or "")
    if source_title:
        keys.add(("text", source_artist, source_title))
        keys.add(("source", source_artist, source_title))
    path = _s(row.get("path") or "").replace("\\", "/").casefold()
    if path:
        keys.add(("path", path))
    return keys


def _playlist_tombstone_rows(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for field, status in (("removed_tracks", "removed"), ("excluded_tracks", "excluded")):
        for raw in manifest.get(field) or []:
            if not isinstance(raw, dict):
                continue
            row = _playlist_track_manifest_payload(raw)
            row["status"] = status
            row["reason"] = _s(raw.get("reason") or f"track {status} by user")
            row["updated_at"] = float(raw.get("updated_at") or 0)
            rows.append(row)
    return rows


def _playlist_track_is_tombstoned(track: Dict[str, Any], manifest: Dict[str, Any]) -> bool:
    keys = _playlist_manifest_match_keys(track)
    if not keys:
        return False
    return any(keys & _playlist_manifest_match_keys(row) for row in _playlist_tombstone_rows(manifest))


def _playlist_apply_tombstones(name: str,
                               tracks: Iterable[Dict[str, Any]],
                               manifest: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    current = manifest if isinstance(manifest, dict) else _playlist_read_manifest(name)
    return [
        track for track in _playlist_clean_track_list(tracks)
        if not _playlist_track_is_tombstoned(track, current)
    ]


def _playlist_merge_desired_tracks(*groups: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen = set()
    for group in groups:
        for track in _playlist_clean_track_list(group or []):
            key = _playlist_manifest_identity(track)
            if key in seen:
                continue
            seen.add(key)
            merged.append(track)
    return merged


def _playlist_legacy_tmp_save_error(error: Any) -> bool:
    text = _s(error)
    return (
        "No such file or directory" in text
        and ".playlist.tmp" in text
        and ".playlist.json" in text
    )


def _playlist_sanitize_manifest(data: Dict[str, Any]) -> Dict[str, Any]:
    manifest = dict(data or {})
    last_pipeline = manifest.get("last_pipeline")
    if isinstance(last_pipeline, dict) and (
        _playlist_legacy_tmp_save_error(last_pipeline.get("error"))
        or _playlist_legacy_tmp_save_error(last_pipeline.get("legacy_error"))
    ):
        cleaned = dict(last_pipeline)
        cleaned["error"] = ""
        cleaned.pop("legacy_error", None)
        cleaned["cleared_error_type"] = "legacy_tmp_save"
        cleaned["recovered_error"] = "Cleared legacy predictable temp-file save failure; safe atomic saves are active."
        if _s(cleaned.get("status") or "").lower() == "failed":
            cleaned["status"] = "interrupted"
        manifest["last_pipeline"] = cleaned
    elif isinstance(last_pipeline, dict):
        status = _s(last_pipeline.get("status") or "").strip().lower()
        error = _s(last_pipeline.get("error") or "").strip().lower()
        if status == "done" and "database is locked" in error:
            cleaned = dict(last_pipeline)
            cleaned["error"] = ""
            cleaned["cleared_error_type"] = "stale_sqlite_lock"
            manifest["last_pipeline"] = cleaned
    return manifest


def _playlist_read_manifest(name: str,
                            *,
                            playlist_id: Optional[str] = None,
                            manifest: Optional[Dict[str, Any]] = None,
                            raise_on_corrupt: bool = False) -> Dict[str, Any]:
    """Read this playlist's manifest.

    A manifest file that exists but fails to parse/validate is NOT the same
    as one that was never created -- conflating "corrupt" with "not found"
    here previously meant a write that follows a corrupt read (see
    _playlist_write_manifest) would silently synthesize a brand-new
    manifest from empty defaults, discarding whatever track/import/Plex-
    sync state the corrupt file still held (SEC-002 Wave 10 second final
    review). raise_on_corrupt=True (used by write paths) surfaces that
    distinction as PlaylistStateError instead of swallowing it; read-only/
    display callers default to the old lenient behavior (degrade to empty
    rather than 500 an entire page over one unreadable playlist).
    """
    seed = dict(manifest) if isinstance(manifest, dict) else {}
    if playlist_id:
        seed["playlist_id"] = playlist_id
    try:
        path = _playlist_manifest_path(name, seed or None, allocate=False)
    except PlaylistStateError:
        return {}
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return _playlist_sanitize_manifest(data)
        raise ValueError("manifest root is not an object")
    except Exception as exc:
        if raise_on_corrupt:
            _app_logger.warning("Playlist manifest %s is corrupt: %s", path, type(exc).__name__)
            raise PlaylistStateError(
                "manifest_corrupt",
                "Playlist manifest is corrupt; refusing to overwrite it.",
            ) from exc
        return {}


def _playlist_write_manifest(name: str,
                             desired_tracks: Iterable[Dict[str, Any]],
                             *,
                             matched_tracks: Iterable[Dict[str, Any]] = (),
                             missing_tracks: Iterable[Dict[str, Any]] = (),
                             source: str = "",
                             content: str = "",
                             playlist_id: Optional[str] = None,
                             log=None) -> Dict[str, Any]:
    clean_name = _clean_playlist_name(name)
    with _playlist_manifest_lock(clean_name):
        # raise_on_corrupt=True: a manifest that fails to parse must not be
        # silently treated as "doesn't exist yet" here -- this is the write
        # path, and proceeding would synthesize a fresh manifest over the
        # corrupt one, discarding whatever state it still held (SEC-002
        # Wave 10 second final review).
        previous = _playlist_read_manifest(clean_name, playlist_id=playlist_id, raise_on_corrupt=True)
        pid = playlist_id or previous.get("playlist_id") or _playlist_ensure_stable_id(clean_name, previous)
        desired = _playlist_merge_desired_tracks(desired_tracks)
        desired = _playlist_apply_tombstones(clean_name, desired, previous)
        manifest = {
            "version": 2,
            "playlist_id": pid,
            "name": clean_name,
            "updated_at": time.time(),
            "source": _s(source).strip() or _s(previous.get("source") or "").strip(),
            "content": _s(content).strip() or _s(previous.get("content") or "").strip(),
            "desired_tracks": desired,
            "matched_count": len(_playlist_clean_track_list(matched_tracks)),
            "missing_count": len(_playlist_clean_track_list(missing_tracks)),
            "removed_tracks": list(previous.get("removed_tracks") or []),
            "excluded_tracks": list(previous.get("excluded_tracks") or []),
            "track_states": dict(previous.get("track_states") or {}),
            "last_pipeline": dict(previous.get("last_pipeline") or {}),
            "last_plex": dict(previous.get("last_plex") or {}),
        }
        path = _playlist_manifest_path(clean_name, manifest)
        _playlist_atomic_json_replace(
            path,
            manifest,
            save_key=f"{clean_name}.{uuid.uuid4().hex[:8]}",
            log=log,
            label="desired-track manifest",
        )
        if log is not None:
            log.append(f"  [playlist] Desired-track manifest saved: {path}")
        return manifest


def _playlist_replace_manifest(name: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
    clean_name = _clean_playlist_name(name)
    with _playlist_manifest_lock(clean_name):
        payload = dict(manifest or {})
        payload.update({
            "version": 2,
            "name": clean_name,
            "updated_at": time.time(),
        })
        path = _playlist_manifest_path(clean_name, payload)
        _playlist_atomic_json_replace(
            path,
            payload,
            save_key=f"{clean_name}.{uuid.uuid4().hex[:8]}",
            label="playlist manifest",
        )
        return payload


def _playlist_store_track_state(name: str,
                                track: Dict[str, Any],
                                status: str,
                                *,
                                playlist_id: Optional[str] = None,
                                **updates: Any) -> Dict[str, Any]:
    clean_name = _clean_playlist_name(name)
    with _playlist_manifest_lock(clean_name):
        manifest = _playlist_read_manifest(clean_name, playlist_id=playlist_id)
        states = dict(manifest.get("track_states") or {})
        key = _playlist_status_id(track)
        row = dict(states.get(key) or _playlist_track_manifest_payload(track))
        normalized_status = _s(status).strip().lower()
        if normalized_status not in PLAYLIST_PIPELINE_STATES:
            normalized_status = "failed"
        row.update({
            "id": key,
            "status": normalized_status,
            "updated_at": time.time(),
        })
        for field, value in updates.items():
            if value not in (None, "") or field in {"message", "failure_reason", "staged_path"}:
                row[field] = value
        states[key] = row
        manifest["track_states"] = states
        _playlist_replace_manifest(clean_name, manifest)
        return row


def _playlist_manifest_track_states(name: str,
                                    playlist_id: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    manifest = _playlist_read_manifest(name, playlist_id=playlist_id)
    states = manifest.get("track_states") or {}
    return states if isinstance(states, dict) else {}


def _playlist_pipeline_counts(name: str,
                              matched: Iterable[Dict[str, Any]],
                              missing: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    manifest = _playlist_read_manifest(name)
    states = _playlist_manifest_track_states(name)
    counts = {status: 0 for status in PLAYLIST_PIPELINE_STATES}
    matched_keys = {_playlist_status_id(track) for track in matched or []}
    missing_keys = {_playlist_status_id(track) for track in missing or []}
    for key in matched_keys:
        status = _s((states.get(key) or {}).get("status") or "available").lower()
        if status not in {"imported", "plex_synced"}:
            status = "available"
        counts[status] += 1
    for key in missing_keys:
        status = _s((states.get(key) or {}).get("status") or "missing").lower()
        if status not in PLAYLIST_PIPELINE_STATES or status in {"available", "imported", "plex_synced"}:
            status = "missing"
        counts[status] += 1
    counts["removed"] = len(manifest.get("removed_tracks") or [])
    counts["excluded"] = len(manifest.get("excluded_tracks") or [])
    return counts


def _playlist_waiting_import_count_from_state(state: Dict[str, Any]) -> int:
    statuses = state.get("track_statuses") if isinstance(state.get("track_statuses"), dict) else {}
    count = 0
    for row in statuses.values():
        if not isinstance(row, dict):
            continue
        status = _s(row.get("status") or "").strip().lower()
        has_staged = bool(_s(row.get("staged_path") or row.get("path") or "").strip())
        if status in {"waiting_import", "downloaded"} and has_staged:
            count += 1
    return count


def _playlist_review_required_count_from_state(state: Dict[str, Any]) -> int:
    statuses = state.get("track_statuses") if isinstance(state.get("track_statuses"), dict) else {}
    return sum(
        1 for row in statuses.values()
        if isinstance(row, dict) and _s(row.get("status") or "").strip().lower() == "review_required"
    )


def _playlist_latest_job_desired_tracks(name: str,
                                        min_tracks: int = 0,
                                        states: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    best_at = 0.0
    best_tracks: List[Dict[str, Any]] = []
    source_states = states if states is not None else _playlist_saved_job_states_for_name(
        name, mark_interrupted=True)
    for data in source_states:
        job_key = data.get("job_key") if isinstance(data.get("job_key"), dict) else {}
        state_tracks = data.get("tracks") if isinstance(data.get("tracks"), list) else []
        key_tracks = job_key.get("tracks") if isinstance(job_key.get("tracks"), list) else []
        tracks = state_tracks if len(state_tracks) >= len(key_tracks) else key_tracks
        cleaned = _playlist_clean_track_list(tracks)
        if len(cleaned) <= min_tracks:
            continue
        stamp = _playlist_job_state_stamp(data)
        if stamp >= best_at:
            best_at = stamp
            best_tracks = cleaned
    return best_tracks


def _playlist_desired_tracks_for_name(name: str,
                                      min_tracks: int = 0,
                                      checkpoint_states: Optional[List[Dict[str, Any]]] = None) -> Tuple[List[Dict[str, Any]], str]:
    manifest = _playlist_read_manifest(name)
    manifest_tracks = manifest.get("desired_tracks") or []
    tracks = _playlist_merge_desired_tracks(manifest_tracks)
    if len(tracks) > min_tracks:
        if tracks != manifest_tracks:
            _playlist_write_manifest(
                name,
                tracks,
                source=_s(manifest.get("source") or ""),
                content=_s(manifest.get("content") or ""),
            )
        return tracks, "manifest"
    job_tracks = _playlist_latest_job_desired_tracks(
        name, min_tracks=min_tracks, states=checkpoint_states)
    if job_tracks:
        return job_tracks, "checkpoint"
    return [], "m3u"


def _create_playlist_outputs(name, items, *, log=None, replace_plex=True,
                             wait_for_plex_seconds=0, require_full_plex=False,
                             desired_tracks=None, missing_tracks=None,
                             source: str = "", content: str = "",
                             playlist_id: Optional[str] = None,
                             sync_plex: bool = True):
    name = _clean_playlist_name(name)
    if not items:
        raise RuntimeError("No tracks to add")

    _playlist_ensure_state_dirs()
    if playlist_id:
        pid = _playlist_ensure_stable_id(name, playlist_id=playlist_id)
    else:
        existing_pid = _playlist_resolve_stable_id(name)
        pid = existing_pid or _playlist_ensure_stable_id(name)
    key = _playlist_key(name, playlist_id=pid, allocate=False)
    m3u = f"engine:{key}.m3u"

    try:
        export_result = composite_workflows.export_playlist_m3u(key, name, items)
    except Exception as ex:
        if log is not None:
            log.append(f"  [playlist] Engine M3U export failed: {type(ex).__name__}")
        raise RuntimeError("m3u_export_failed") from ex
    if not (isinstance(export_result, dict) and export_result.get("ok")):
        raise RuntimeError("m3u_export_failed")
    if log is not None:
        log.append(f"  [playlist] Engine M3U exported: {key}")
    if export_result.get("playlist_key"):
        m3u = f"engine:{_s(export_result.get('playlist_key'))}.m3u"

    desired = desired_tracks if desired_tracks is not None else items
    manifest = _playlist_write_manifest(
        name,
        desired,
        matched_tracks=items,
        missing_tracks=missing_tracks or [],
        source=source,
        content=content,
        playlist_id=pid,
        log=log,
    )
    prior_rating_key = ""
    if isinstance(manifest, dict):
        prior_rating_key = _s((manifest.get("last_plex") or {}).get("rating_key") or "").strip()

    plex = {
        "created": False,
        "tracks_added": 0,
        "tracks_requested": len(items),
        "tracks_matched": 0,
        "tracks_unmatched": len(items),
        "complete": False,
        "status": "not_configured",
        "error": None,
        "issue_reason": "",
        "action_needed": "",
        "replaced": 0,
        "rating_key": "",
        "scan_triggered": False,
        "section_key": "",
        "section_title": "",
        "index_status": "not_run",
        "index_error": "",
        "matched_by_path": 0,
        "matched_by_fallback": 0,
        "missing_examples": [],
        "path_mapping_used": "",
        "path_mapping_verified": False,
        "plex_library_locations": [],
        "sample_beets_path": "",
        "sample_mapped_plex_path": "",
        "sample_mapped_exists": False,
        "verified_count": 0,
        "verification_error": "",
        "pending_plex_count": 0,
        "pending_tracks": [],
        "matched_track_ids": [],
        "summary_message": "",
    }
    if sync_plex and _plex_settings().get("token"):
        try:
            machine_id, sec, section_title = _plex_find_music_section()
            if sec:
                plex["section_key"] = _s(sec)
                plex["section_title"] = _s(section_title or "")
                if log is not None:
                    log.append(
                        f"  [plex] Sync section: {plex['section_title'] or plex['section_key']}"
                    )
                machine_id = _s(machine_id or "").strip() or _plex_machine_identifier()
                if not machine_id:
                    raise RuntimeError("Plex machineIdentifier is unavailable")
                if wait_for_plex_seconds:
                    plex["scan_triggered"] = _trigger_plex_refresh(log or [], workflow="playlist")
                keys, match_details = _plex_track_keys_for_items(
                    sec, items, log=log, wait_seconds=wait_for_plex_seconds,
                    return_details=True)
                plex.update(match_details)
                plex["tracks_matched"] = len(keys)
                plex["tracks_unmatched"] = max(
                    int(match_details.get("missing_in_plex_count") or 0),
                    max(len(items) - len(keys), 0),
                )
                plex["status"] = "matching"
                pending_count = int(match_details.get("pending_plex_count") or plex["tracks_unmatched"] or 0)
                plex["pending_plex_count"] = pending_count
                match_ratio = (len(keys) / len(items)) if items else 0.0
                mapping_failed = (
                    not keys
                    and not bool(plex.get("path_mapping_verified"))
                    and match_ratio < PLEX_SYNC_MIN_MATCH_RATIO
                )
                if mapping_failed:
                    plex["issue_reason"] = "path mapping not verified"
                    plex["action_needed"] = (
                        f"Check path mapping: {plex.get('path_mapping_used') or (str(MUSIC_ROOT) + ' -> (none)')}"
                    )
                    plex["error"] = (
                        "Plex cannot see Beets library paths. "
                        f"Check path mapping: {plex.get('path_mapping_used') or (str(MUSIC_ROOT) + ' -> (none)')}."
                    )
                    plex["status"] = "failed"
                    if log is not None:
                        log.append(f"  [plex] {plex['error']}")
                elif require_full_plex and len(keys) < len(items):
                    plex["error"] = (
                        f"Only {len(keys)}/{len(items)} playlist tracks are visible in Plex; "
                        "leaving existing Plex playlist unchanged"
                    )
                    plex["status"] = "failed"
                    plex["issue_reason"] = "playlist tracks missing in Plex"
                    plex["action_needed"] = "Wait for Plex scan or repair path mapping"
                    if log is not None:
                        log.append(f"  [plex] {plex['error']}")
                elif keys:
                    playlist_keys = list(dict.fromkeys(str(key) for key in keys if str(key)))
                    if replace_plex:
                        replace_result = _plex_replace_playlist_safely(name, pid, manifest, log=log)
                        plex["replaced"] = replace_result["replaced"]
                        if replace_result.get("ambiguous"):
                            plex["issue_reason"] = "ambiguous Plex playlist title"
                            plex["action_needed"] = (
                                "Another playlist shares this name with no stored Plex ratingKey; "
                                "sync it once more to safely establish separate Plex identity"
                            )
                    added, new_rating_key = _plex_create_audio_playlist(machine_id, name, playlist_keys, log=log)
                    plex["created"] = True
                    plex["tracks_added"] = added
                    plex["rating_key"] = new_rating_key
                    plex["matched_track_ids"] = list(match_details.get("matched_track_ids") or [])
                    plex["complete"] = pending_count == 0 and len(keys) == len(items)
                    plex["status"] = "success" if plex["complete"] else "partial_success"
                    if pending_count:
                        plex["issue_reason"] = "pending Plex matches"
                        plex["action_needed"] = "Retry pending Plex matches after Plex scans the affected files"
                    plex["summary_message"] = (
                        f"Plex playlist updated with {len(keys)} of {len(items)} track(s); "
                        f"{pending_count} pending Plex match(es)."
                    )
                    try:
                        target_rkey = _s(plex.get("rating_key") or prior_rating_key).strip()
                        # Lookup uses rating_key or unambiguous fallback via _plex_playlist_rating_keys_by_title
                        verified_keys, found_rkey, is_ambig, is_stale = _plex_playlist_rating_keys_by_key_or_title(rating_key=target_rkey, title=name)
                        if found_rkey:
                            plex["rating_key"] = found_rkey
                        verified_unique = {str(key) for key in verified_keys}
                        expected_unique = {str(key) for key in playlist_keys}
                        plex["verified_count"] = len(verified_unique)
                        plex["existing_playlist_count"] = len(verified_unique)
                        missing_verified = expected_unique - verified_unique
                        if log is not None:
                            log.append(f"  [plex] Created/updated Plex playlist: {name}")
                            log.append(f"  [plex] Verified Plex playlist count: {len(verified_unique)}")
                        if is_stale:
                            # target_rkey was just-created above (new_rating_key) and
                            # should always resolve; is_stale here means creation's
                            # own returned key is already gone (race/API oddity) --
                            # do not fall back to prior_rating_key/title, surface it.
                            plex["complete"] = False
                            plex["status"] = "review_required"
                            plex["issue_reason"] = "stale_plex_identity"
                            plex["action_needed"] = "Plex playlist identity could not be re-verified; re-sync to re-establish it"
                        elif is_ambig:
                            plex["complete"] = False
                            plex["status"] = "review_required"
                            plex["issue_reason"] = "ambiguous_plex_playlist"
                        elif missing_verified:
                            plex["complete"] = False
                            plex["status"] = "partial_success"
                            plex["issue_reason"] = "playlist verification count was lower than matched tracks"
                            plex["action_needed"] = "Open Plex and refresh the playlist if the count does not update"
                    except Exception as exc:
                        plex["verification_error"] = str(exc)
                        if log is not None:
                            log.append(f"  [plex] Playlist verification failed: {exc}")
                    if log is not None:
                        log.append(
                            f"  [plex] Playlist partially synced ({len(keys)}/{len(items)} matched; "
                            f"{pending_count} pending)" if pending_count else
                            f"  [plex] Playlist synced ({len(keys)} tracks)"
                        )
                else:
                    plex["error"] = (
                        "Plex sync failed: no Beets library tracks matched Plex. "
                        "Check path mapping or wait for Plex scan."
                    )
                    plex["status"] = "failed"
                    plex["issue_reason"] = "no tracks matched Plex"
                    plex["action_needed"] = "Check Plex scan and path mapping"
                    if log is not None:
                        log.append(f"  [plex] {plex['error']}")
            else:
                plex["error"] = "No Plex music library section found"
                plex["status"] = "failed"
                plex["issue_reason"] = "Plex music section not found"
                plex["action_needed"] = "Configure plex_music_section"
        except Exception as exc:
            plex["error"] = str(exc)
            plex["status"] = "failed"
            plex["issue_reason"] = "filesystem or Plex API operation failed"
            plex["action_needed"] = "Check the Plex job log"
            if log is not None:
                log.append(f"  [plex] Playlist sync error: {exc}")

    if sync_plex and _plex_settings().get("token") and (plex.get("error") or not plex.get("created")):
        try:
            target_rkey = _s(plex.get("rating_key") or prior_rating_key).strip()
            existing_keys, found_rkey, is_ambig, is_stale = _plex_playlist_rating_keys_by_key_or_title(rating_key=target_rkey, title=name)
            if found_rkey:
                plex["rating_key"] = found_rkey
            if is_stale:
                # A previously-stored ratingKey no longer resolves in Plex.
                # Do not silently rebind to whatever else currently shares
                # this title -- report it and leave the stored identity
                # alone (SEC-002 Wave 11 second final review).
                plex["issue_reason"] = "stale_plex_identity"
            elif is_ambig:
                plex["issue_reason"] = "ambiguous_plex_playlist"
            plex["verified_count"] = len(existing_keys)
            plex["existing_playlist_count"] = len(existing_keys)
            if log is not None:
                log.append(f"  [plex] Current Plex playlist count: {len(existing_keys)}")
        except Exception as exc:
            if not plex.get("verification_error"):
                plex["verification_error"] = str(exc)

    if sync_plex:
        latest_manifest = _playlist_read_manifest(name, playlist_id=pid) or dict(manifest)
        latest_manifest["last_plex"] = {
            **plex,
            "status": _s(plex.get("status") or ("synced" if plex.get("complete") else ("failed" if plex.get("error") else "partial"))),
            "synced_at": time.time(),
        }
        _playlist_replace_manifest(name, latest_manifest)
    skip_per_track_plex_stamps = bool(
        sync_plex
        and plex.get("error")
        and int(plex.get("tracks_unmatched") or 0) > PLEX_SYNC_MAX_UNMATCHED_REPLACE
    )
    if skip_per_track_plex_stamps and log is not None:
        log.append("  [plex] Large failed sync recorded in playlist summary; per-track Plex stamps skipped")
    matched_plex_ids = set(_s(value) for value in (plex.get("matched_track_ids") or []))
    pending_plex_by_id = {
        _s(row.get("local_track_id") or ""): row
        for row in (plex.get("pending_tracks") or [])
        if isinstance(row, dict)
    }
    for item in ([] if skip_per_track_plex_stamps else items):
        item_status_id = _playlist_status_id(item)
        if plex.get("complete") or item_status_id in matched_plex_ids:
            _playlist_store_track_state(
                name, item, "plex_synced",
                playlist_id=pid,
                message="matched to Plex library playlist",
                path=_s(item.get("path") or ""),
                plex_issue="",
            )
        elif sync_plex:
            pending_row = pending_plex_by_id.get(item_status_id) or {}
            current_state = _playlist_manifest_track_states(name, playlist_id=pid).get(item_status_id, {})
            current_status = _s(current_state.get("status") or "available")
            if current_status not in {"imported", "available"}:
                current_status = "available"
            issue = _s(
                pending_row.get("reason")
                or plex.get("error")
                or "Plex match pending"
            )
            _playlist_store_track_state(
                name, item, current_status,
                playlist_id=pid,
                message=issue,
                path=_s(item.get("path") or ""),
                plex_issue=issue,
            )

    return {
        "m3u": m3u,
        "manifest": str(_playlist_manifest_path(name, manifest)),
        "playlist_id": pid,
        "playlist_key": key,
        "plex": plex,
        "tracks_in_m3u": len(items),
        "desired_tracks": len(manifest.get("desired_tracks") or []),
        "missing_tracks": int(manifest.get("missing_count") or 0),
    }


_PLAYLIST_SYNC_LOCK = threading.Lock()


_PLAYLIST_SYNC_STATE: Dict[str, Any] = {
    "enabled": PLAYLIST_AUTO_SYNC_ENABLED,
    "interval": PLAYLIST_AUTO_SYNC_INTERVAL,
    "running": False,
    "last_run": 0,
    "last_result": None,
    "last_log": [],
    "last_error": "",
}


def _playlist_item_identity(item: Dict[str, Any]) -> tuple:
    item_id = int(item.get("id") or 0)
    if item_id > 0:
        return ("id", item_id)
    path = _s(item.get("path") or "").strip()
    if path:
        return ("path", next(iter(_playlist_path_keys(path)), path.casefold()))
    return (
        "text",
        _norm(item.get("artist", "")),
        _norm(item.get("title", "")),
    )


def _playlist_library_index() -> Dict[str, Any]:
    gen = getattr(library_cache, "generation", 1)
    with library_cache.playlist_index_lock:
        cached = library_cache.playlist_index.get("index")
        if cached is not None and library_cache.playlist_index.get("generation") == gen:
            return cached
        by_path: Dict[str, Dict[str, Any]] = {}
        by_text: Dict[tuple, Dict[str, Any]] = {}
        by_title: Dict[str, List[Dict[str, Any]]] = {}
        match_all: List[Dict[str, Any]] = []
        match_by_title: Dict[str, List[Dict[str, Any]]] = {}
        for item in lib.items([]):
            payload = _playlist_item_payload(item, "", "", None)
            for key in _playlist_path_keys(payload.get("path", "")):
                by_path.setdefault(key, payload)
            seen_match_keys: set = set()
            for artist_value, title_value in _playlist_item_text_variants(item):
                _playlist_index_put_text(by_text, artist_value, title_value, payload)
                title_key = _norm(title_value)
                if title_key:
                    by_title.setdefault(title_key, []).append(payload)
                    match_key = (_norm(artist_value), title_key)
                    if match_key not in seen_match_keys:
                        seen_match_keys.add(match_key)
                        row = {
                            "artist": artist_value,
                            "title": title_value,
                            "quality": payload,
                            "payload": payload,
                        }
                        match_all.append(row)
                        match_by_title.setdefault(title_key, []).append(row)
        index = {
            "by_path": by_path,
            "by_text": by_text,
            "by_title": by_title,
            "match_all": match_all,
            "match_by_title": match_by_title,
        }
        library_cache.playlist_index["generation"] = gen
        library_cache.playlist_index["mtime"] = float(getattr(library_cache, "ts", 0.0) or 0.0)
        library_cache.playlist_index["index"] = index
        return index


def _playlist_item_from_path(path_value: str, index: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    by_path = index.get("by_path") or {}
    for key in _playlist_path_keys(path_value):
        item = by_path.get(key)
        if item:
            return dict(item)
    return None


def _playlist_item_from_text(artist: str, title: str, index: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    artist = _s(artist).strip()
    title = _s(title).strip()
    if not title:
        return None
    by_text = index.get("by_text") or {}
    item = by_text.get((_norm(artist), _norm(title)))
    if item:
        return dict(item)
    title_key = _norm(title)
    candidates = index.get("by_title", {}).get(title_key, []) if title_key else []
    best_item: Optional[Dict[str, Any]] = None
    best_score = 0.0
    for cand in candidates:
        cand_artist = _s(cand.get("artist") or cand.get("albumartist") or "")
        if artist:
            score = _playlist_artist_name_score(artist, cand_artist)
            if score < 0.72:
                continue
        else:
            score = _playlist_payload_rank(cand)
        if score > best_score:
            best_score = score
            best_item = cand
    if best_item:
        payload = dict(best_item)
        if artist:
            payload["query_artist"] = artist
        payload["query_title"] = title
        if artist:
            payload["score"] = round(best_score, 3)
        return payload
    return None


def _playlist_parse_extinf_label(line: str) -> tuple:
    label = _s(line).split(",", 1)[1].strip() if "," in _s(line) else ""
    if " - " in label:
        artist, title = label.split(" - ", 1)
        return artist.strip(), title.strip()
    return "", label.strip()


def _playlist_m3u_items(name: str, index: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], int]:
    clean_name = _clean_playlist_name(name)
    key = _playlist_existing_key(clean_name)
    raw_items = []

    try:
        res = composite_workflows.read_playlist_m3u(key, fallback_name=clean_name)
        if isinstance(res, dict) and res.get("ok") and res.get("exists"):
            raw_items = res.get("items") or []
    except Exception:
        pass

    items: List[Dict[str, Any]] = []
    skipped = 0
    for raw in raw_items:
        line = _s(raw.get("path") or "").strip()
        item = _playlist_item_from_path(line, index)
        if not item:
            artist = _s(raw.get("artist") or "").strip()
            title = _s(raw.get("title") or "").strip()
            item = _playlist_item_from_text(artist, title, index)
        if item:
            items.append(item)
        else:
            skipped += 1
    return items, skipped


def _plex_audio_playlists() -> List[Dict[str, Any]]:
    d = _plex_request("/playlists", {"playlistType": "audio"}, timeout=12)
    out = []
    for pl in d.get("MediaContainer", {}).get("Metadata", []):
        if str(pl.get("smart", "0")).strip().lower() in {"1", "true", "yes"}:
            continue
        title = _s(pl.get("title") or "").strip()
        key = _s(pl.get("ratingKey") or pl.get("key") or "").strip()
        if title and key:
            out.append(pl)
    return out


def _plex_playlist_items_path(pl: Dict[str, Any]) -> str:
    key = _s(pl.get("key") or "").strip()
    rating_key = _s(pl.get("ratingKey") or "").strip()
    path = key.split("?", 1)[0] if key.startswith("/playlists/") else ""
    if not path and rating_key:
        path = f"/playlists/{rating_key}"
    if not path:
        path = f"/playlists/{key.strip('/')}"
    if not path.endswith("/items"):
        path = path.rstrip("/") + "/items"
    return path


def _plex_playlist_rating_keys(pl: Dict[str, Any]) -> List[str]:
    d = _plex_request(_plex_playlist_items_path(pl), timeout=20)
    keys: List[str] = []
    for track in d.get("MediaContainer", {}).get("Metadata", []) or []:
        key = _s(track.get("ratingKey") or track.get("key") or "").strip()
        if key:
            keys.append(key.strip("/").split("/")[-1])
    return keys


def _plex_playlist_by_rating_key(rating_key: str) -> Optional[Dict[str, Any]]:
    key = _s(rating_key).strip()
    if not key:
        return None
    for pl in _plex_audio_playlists():
        pl_key = _s(pl.get("ratingKey") or pl.get("key") or "").strip().strip("/").split("/")[-1]
        if pl_key == key:
            return pl
    return None


def _plex_playlist_rating_keys_by_key_or_title(rating_key: str = "", title: str = "") -> Tuple[List[str], str, bool, bool]:
    """Look up track ratingKeys for verification from a specific Plex ratingKey,
    or unambiguously by title. Returns (track_keys, target_rating_key, is_ambiguous, is_stale).

    A supplied rating_key that no longer resolves in Plex is STALE identity,
    not "no identity yet" -- it must not silently fall through to a
    same-title match, which could bind to a completely different logical
    Plex playlist that merely happens to share a title (SEC-002 Wave 11
    second final review: the exact vulnerability the title-fallback
    previously reintroduced after Wave 10 fixed the same-name-delete case).
    Title-based lookup is legacy discovery, used only when the caller has
    no rating_key at all -- never as a fallback for one that failed to
    resolve.
    """
    r_key = _s(rating_key).strip()
    if r_key:
        pl = _plex_playlist_by_rating_key(r_key)
        if pl:
            return _plex_playlist_rating_keys(pl), r_key, False, False
        return [], "", False, True
    if title:
        candidates = _plex_playlist_candidates_by_title(title)
        if len(candidates) > 1:
            return [], "", True, False
        if len(candidates) == 1:
            pl, found_key = candidates[0]
            return _plex_playlist_rating_keys(pl), found_key, False, False
    return [], "", False, False


def _plex_playlist_rating_keys_by_title(title: str) -> List[str]:
    keys, _, _, _ = _plex_playlist_rating_keys_by_key_or_title(title=title)
    return keys


def _plex_playlist_items(pl: Dict[str, Any], index: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], int]:
    d = _plex_request(_plex_playlist_items_path(pl), timeout=20)
    items: List[Dict[str, Any]] = []
    skipped = 0
    for track in d.get("MediaContainer", {}).get("Metadata", []):
        item = _playlist_item_from_path(_plex_track_file(track), index)
        if not item:
            item = _playlist_item_from_text(
                _s(track.get("grandparentTitle") or ""),
                _s(track.get("title") or ""),
                index,
            )
        if item:
            items.append(item)
        else:
            skipped += 1
    return items, skipped


def _merge_playlist_items(*groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen = set()
    for group in groups:
        for item in group:
            key = _playlist_item_identity(item)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def _playlist_match_reference_tracks(tracks: Iterable[Dict[str, Any]],
                                     index: Dict[str, Any],
                                     verify_acoustid: bool = False) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    matched: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    for track in tracks or []:
        track_payload, item = _playlist_match_reference_track(track, index, verify_acoustid=verify_acoustid)
        if item:
            item["query_artist"] = track_payload.get("artist") or item.get("artist", "")
            item["query_title"] = track_payload.get("title") or item.get("title", "")
            matched.append(item)
        else:
            missing.append(track_payload)
    return matched, missing


def _playlist_append_external_additions(primary: List[Dict[str, Any]],
                                        external: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = list(primary or [])
    seen = {_playlist_item_identity(item) for item in out}
    for item in external or []:
        key = _playlist_item_identity(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _playlist_sync_all(log: list, names: Optional[List[str]] = None) -> Dict[str, Any]:
    status = _plex_status_payload(force=False)
    if not status.get("configured") or not status.get("connected"):
        raise RuntimeError(status.get("error") or "Plex is not connected")

    wanted_names = {_norm(n) for n in (names or []) if _s(n).strip()}
    index = _playlist_library_index()
    local_names = []
    _playlist_ensure_state_dirs()
    try:
        res = composite_workflows.list_playlist_m3u()
        if isinstance(res, dict) and res.get("ok") and isinstance(res.get("playlists"), list):
            for pl in res.get("playlists") or []:
                n = _s(pl.get("name") or pl.get("key") or "").strip()
                if n and n not in local_names:
                    local_names.append(n)
    except Exception:
        pass
    plex_by_norm: Dict[str, Dict[str, Any]] = {}
    for pl in _plex_audio_playlists():
        plex_by_norm.setdefault(_norm(pl.get("title", "")), pl)

    display_by_norm: Dict[str, str] = {}
    for name in local_names:
        display_by_norm.setdefault(_norm(name), name)
    for key, pl in plex_by_norm.items():
        display_by_norm.setdefault(key, _s(pl.get("title") or "").strip())

    summary = {
        "playlists_seen": 0,
        "playlists_updated": 0,
        "tracks_written": 0,
        "local_added": 0,
        "plex_added": 0,
        "skipped": 0,
        "details": [],
    }
    for norm_name, display_name in sorted(display_by_norm.items(), key=lambda kv: kv[1].casefold()):
        if wanted_names and norm_name not in wanted_names:
            continue
        local_items, local_skipped = _playlist_m3u_items(display_name, index)
        plex_items, plex_skipped = ([], 0)
        plex = plex_by_norm.get(norm_name)
        if plex:
            plex_items, plex_skipped = _plex_playlist_items(plex, index)
        external_items = _merge_playlist_items(local_items, plex_items)
        external_items = _playlist_apply_tombstones(display_name, external_items)
        desired_tracks, desired_source = _playlist_desired_tracks_for_name(
            display_name, min_tracks=len(local_items))
        original_desired_keys = {
            _playlist_manifest_identity(track)
            for track in _playlist_clean_track_list(desired_tracks)
        }
        desired_tracks = _playlist_merge_desired_tracks(desired_tracks, external_items)
        desired_tracks = _playlist_apply_tombstones(display_name, desired_tracks)
        desired_keys = {
            _playlist_manifest_identity(track)
            for track in _playlist_clean_track_list(desired_tracks)
        }
        desired_changed = desired_keys != original_desired_keys
        matched_items, missing_tracks = _playlist_match_reference_tracks(desired_tracks, index)
        playable_items = _playlist_append_external_additions(matched_items, external_items)
        if not playable_items:
            continue

        local_ids = [_playlist_item_identity(item) for item in local_items]
        plex_ids = [_playlist_item_identity(item) for item in plex_items]
        playable_ids = [_playlist_item_identity(item) for item in playable_items]
        local_added = len([key for key in playable_ids if key not in local_ids])
        plex_added = len([key for key in playable_ids if key not in plex_ids])
        changed = desired_changed or local_ids != playable_ids or plex_ids != playable_ids

        summary["playlists_seen"] += 1
        summary["skipped"] += local_skipped + plex_skipped
        detail = {
            "name": display_name,
            "local_tracks": len(local_items),
            "plex_tracks": len(plex_items),
            "merged_tracks": len(playable_items),
            "local_added": local_added,
            "plex_added": plex_added,
            "plex_synced": False,
            "plex_error": "",
            "skipped": local_skipped + plex_skipped,
            "updated": False,
            "desired_tracks": len(desired_tracks),
            "desired_source": desired_source,
            "available_tracks": len(playable_items),
            "missing_tracks": len(missing_tracks),
        }
        if changed:
            log.append(
                f"  [playlist-sync] {display_name}: "
                f"{len(local_items)} local + {len(plex_items)} Plex + "
                f"{len(matched_items)} manifest match(es) -> {len(playable_items)} playable"
            )
            output = _create_playlist_outputs(
                display_name,
                playable_items,
                log=log,
                replace_plex=True,
                require_full_plex=False,
                desired_tracks=desired_tracks,
                missing_tracks=missing_tracks,
            )
            plex_result = output.get("plex") or {}
            plex_synced = bool(plex_result.get("created") and not plex_result.get("error"))
            summary["playlists_updated"] += 1
            summary["tracks_written"] += len(playable_items)
            summary["local_added"] += local_added
            summary["plex_added"] += plex_added if plex_synced else 0
            detail["updated"] = True
            detail["plex_synced"] = plex_synced
            detail["plex_added"] = plex_added if plex_synced else 0
            if plex_synced:
                detail["plex_tracks"] = int(
                    plex_result.get("verified_count")
                    or plex_result.get("existing_playlist_count")
                    or plex_result.get("tracks_added")
                    or detail.get("plex_tracks")
                    or 0
                )
                detail["plex_verified_count"] = int(plex_result.get("verified_count") or 0)
                detail["plex_unique_rating_keys"] = int(plex_result.get("unique_rating_keys") or 0)
            detail["plex_tracks_matched"] = int(plex_result.get("tracks_matched") or 0)
            detail["plex_tracks_unmatched"] = int(plex_result.get("tracks_unmatched") or 0)
            detail["plex_error"] = _s(plex_result.get("error") or "")
        summary["details"].append(detail)
    log.append(
        f"Playlist two-way sync complete: {summary['playlists_updated']} updated, "
        f"{summary['local_added']} added to M3U, {summary['plex_added']} added to Plex"
    )
    return summary


def _playlist_sync_all_locked(log: list, names: Optional[List[str]] = None) -> Dict[str, Any]:
    if not _PLAYLIST_SYNC_LOCK.acquire(blocking=False):
        log.append("Playlist sync already running; skipped this run")
        return {"skipped": True, "reason": "already_running"}
    _PLAYLIST_SYNC_STATE.update({"running": True, "last_error": ""})
    try:
        result = _playlist_sync_all(log, names=names)
        _PLAYLIST_SYNC_STATE.update({
            "last_run": time.time(),
            "last_result": result,
            "last_log": list(log)[-200:],
        })
        return result
    except Exception as ex:
        _PLAYLIST_SYNC_STATE.update({
            "last_run": time.time(),
            "last_error": str(ex),
            "last_log": list(log)[-200:] + [f"Fatal: {ex}"],
        })
        raise
    finally:
        _PLAYLIST_SYNC_STATE["running"] = False
        _PLAYLIST_SYNC_LOCK.release()


class _SpotifyFetchError(RuntimeError):
    pass


def _fetch_spotify_playlist_tracks(pid: str, cid: str, cs: str) -> List[Dict[str, str]]:
    """Fetch all tracks for a Spotify playlist ID via client-credentials auth.

    Raises _SpotifyFetchError with a user-facing message on any network/auth/parse
    failure instead of letting urllib/json exceptions crash the request handler.
    """
    auth = base64.b64encode(f"{cid}:{cs}".encode()).decode()
    tok_req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=b"grant_type=client_credentials",
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with provider_boundary.opened("spotify", tok_req, timeout=10) as r:
            token = json.loads(r.read())["access_token"]
    except (urllib.error.URLError, socket.timeout, TimeoutError,
            json.JSONDecodeError, KeyError) as ex:
        _app_logger.warning("Spotify auth failed: %s", type(ex).__name__)
        raise _SpotifyFetchError("Spotify authentication failed.") from ex

    tracks: List[Dict[str, str]] = []
    offset = 0
    while True:
        url = (f"https://api.spotify.com/v1/playlists/{pid}/tracks"
               f"?limit=100&offset={offset}&fields=items(track(name,artists)),next")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with provider_boundary.opened("spotify", req, timeout=10) as r:
                data = json.loads(r.read())
        except (urllib.error.URLError, socket.timeout, TimeoutError,
                json.JSONDecodeError) as ex:
            _app_logger.warning("Spotify playlist fetch failed: %s", type(ex).__name__)
            raise _SpotifyFetchError("Spotify playlist fetch failed.") from ex
        for item in data.get("items", []):
            trk = item.get("track") or {}
            if not trk.get("name"):
                continue
            artist = ", ".join(a["name"] for a in trk.get("artists", []))
            tracks.append({"artist": artist, "title": trk["name"]})
        if not data.get("next"):
            break
        offset += 100
    return tracks


def _playlist_url_host(value: str) -> str:
    """Parsed hostname of a caller-supplied playlist source URL, or ''
    if it doesn't parse. Used for provider-routing decisions in
    playlist_parse() -- must be real hostname parsing, not substring
    matching, since one branch attaches stored credentials based on the
    result (see the SEC-002 CodeQL repository-wide closure finding
    documented at that call site)."""
    try:
        return (_up.urlsplit(value).hostname or "").lower()
    except Exception:
        return ""


def _playlist_url_host_is(host: str, domain: str) -> bool:
    return bool(host) and (host == domain or host.endswith("." + domain))


def _playlist_url_host_is_any(host: str, domains) -> bool:
    return any(_playlist_url_host_is(host, domain) for domain in domains)


# Service behind POST /api/playlist/parse (ARCH-001): request-free,
# returns (json_body, http_status); the route and in-process callers share it.
def parse_playlist_request(payload_in: Dict[str, Any]) -> Tuple[Any, int]:
    payload = payload_in
    source  = payload.get("source", "text")
    content = (payload.get("content") or "").strip()
    tracks  = []  # [{artist, title}]

    if source == "local_m3u":
        local_name = _clean_playlist_name(content)
        index = _playlist_library_index()
        local_tracks, _matched, _missing = _playlist_m3u_track_rows(local_name, index)
        if not local_tracks:
            return {"ok": False, "error": f"Engine M3U playlist not found: {local_name}"}, 404
        tracks.extend(local_tracks)
    elif source == "text":
        for raw in content.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if " - " in line:
                a, t = line.split(" - ", 1)
                tracks.append({"artist": a.strip(), "title": t.strip()})
            else:
                tracks.append({"artist": "", "title": line})

    elif source == "spotify":
        cid = os.environ.get("SPOTIFY_CLIENT_ID","").strip()
        cs  = os.environ.get("SPOTIFY_CLIENT_SECRET","").strip()
        if not cid or not cs:
            return {"ok": False, "error":
                "Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in the beets environment"}, 200
        m = re.search(r"playlist[/:]([A-Za-z0-9]+)", content)
        if not m:
            return {"ok": False, "error": "Could not parse Spotify playlist URL"}, 200
        try:
            tracks.extend(_fetch_spotify_playlist_tracks(m.group(1), cid, cs))
        except _SpotifyFetchError as ex:
            return {"ok": False, "error": str(ex)}, 200

    elif source == "url":
        # Generic URL: Spotify (if creds set) → Spotify API; everything else → yt-dlp.
        # Also reached when source=="spotify" isn't used.
        #
        # SEC-002 CodeQL repository-wide closure finding
        # (py/incomplete-url-substring-sanitization, 3 alerts): the
        # provider-routing checks below used to be plain substring tests
        # ("spotify.com" in content, "youtube.com"/"soundcloud.com" in
        # parse_lower). Unlike the cosmetic frontend badge findings in the
        # same rule class, this one is a genuine trust decision: the soundcloud.com
        # branch attaches the operator's stored netrc credentials
        # (_apply_ytdlp_netrc) to whatever URL yt-dlp is given. A URL that
        # merely CONTAINS "soundcloud.com" as a substring without actually
        # being a soundcloud.com URL (e.g. "https://evil.example/soundcloud.com/x")
        # would previously have had those credentials attached and sent to
        # the attacker-controlled host -- real credential exfiltration, not
        # just a mislabeled UI badge. Fixed with real hostname parsing
        # (_playlist_url_host()/_playlist_url_host_is(), module-level).
        cid = os.environ.get("SPOTIFY_CLIENT_ID","").strip()
        cs  = os.environ.get("SPOTIFY_CLIENT_SECRET","").strip()
        _content_host = _playlist_url_host(content)
        if _playlist_url_host_is(_content_host, "spotify.com") and cid and cs:
            # Route to Spotify API
            m = re.search(r"playlist[/:]([A-Za-z0-9]+)", content)
            if not m:
                return {"ok": False, "error": "Could not parse Spotify playlist ID"}, 200
            try:
                tracks.extend(_fetch_spotify_playlist_tracks(m.group(1), cid, cs))
            except _SpotifyFetchError as ex:
                return {"ok": False, "error": str(ex)}, 200
        else:
            # yt-dlp handles the supported media hosts (YouTube/YouTube Music,
            # SoundCloud, Bandcamp, Mixcloud, Vimeo, Deezer). SEC-2: anything
            # else -- and any host resolving to a non-public address -- is
            # refused before yt-dlp runs; yt-dlp's own HTTP stack bypasses the
            # application's outbound URL policy, and its generic extractor
            # would otherwise fetch internal URLs.
            from backend.ytdlp_guard import YTDLP_ALLOWED_HOSTS, ytdlp_guarded_options, ytdlp_target_allowed
            if not ytdlp_target_allowed(content) or not _playlist_url_host_is_any(_content_host, YTDLP_ALLOWED_HOSTS):
                return {"ok": False, "error": "Unsupported playlist URL. Use a YouTube, YouTube Music, SoundCloud, Bandcamp, Mixcloud, Vimeo, Deezer or Spotify playlist link."}, 200
            if not _ytdlp_ready.wait(timeout=30):
                return {"ok": False, "error": "yt-dlp is still installing, try again in 30 seconds"}, 200
            try:
                import yt_dlp
            except ImportError:
                return {"ok": False, "error": "yt-dlp could not be installed — check container pip access"}, 200

            # SEC-002 CodeQL repository-wide closure finding
            # (py/polynomial-redos): the same unbounded-content-between-
            # delimiters shape as the playlist title/artist cleaners --
            # not itself CodeQL-flagged at this line, but the identical, already-
            # empirically-proven-quadratic pattern, fixed the same way.
            # PR-scoped re-check (post-Wave-27 rebase) additionally found
            # the leading \s* also needed bounding (see the identical note
            # on _playlist_title_variants()) -- fixed here proactively too.
            _JUNK_RE = re.compile(
                r'\s{0,20}[\(\[]'
                r'(?:official\s+(?:video|audio|lyric|music\s+video|visualizer)|'
                r'lyric(?:s|\s+video)?|audio|video|mv|visualizer|hd|4k|explicit|'
                r'live|acoustic|remix|instrumental|karaoke|cover|ft\.?|feat\.?)[^\)\]]{0,100}'
                r'[\)\]]',
                re.IGNORECASE)

            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": "in_playlist",
                "ignoreerrors": True,
                "socket_timeout": 20,
                "js_runtimes": _ytdlp_js_runtime_options(),
                "remote_components": _ytdlp_remote_components(),
            }
            if _playlist_url_host_is(_content_host, "youtube.com") or _playlist_url_host_is(_content_host, "youtu.be"):
                extractor_args = _ytdlp_source_extractor_args("ytdlp")
                if extractor_args:
                    ydl_opts["extractor_args"] = extractor_args
            elif _playlist_url_host_is(_content_host, "soundcloud.com"):
                _apply_ytdlp_netrc(ydl_opts)
            try:
                with yt_dlp.YoutubeDL(ytdlp_guarded_options(ydl_opts, [content])) as ydl:
                    info = ydl.extract_info(content, download=False)
            except Exception as exc:
                _app_logger.warning("yt-dlp playlist import failed: %s", type(exc).__name__)
                return {"ok": False, "error": "Could not read this playlist URL."}, 200

            if not info:
                return {"ok": False, "error": "yt-dlp returned no data for that URL"}, 200

            entries = []
            if info.get("_type") == "playlist":
                entries = [e for e in (info.get("entries") or []) if e]
            else:
                entries = [info]

            for entry in entries:
                raw_title = (entry.get("title") or "").strip()
                # yt-dlp may expose artist/track metadata directly
                artist = (entry.get("artist") or entry.get("uploader") or
                          entry.get("channel") or "").strip()
                title  = raw_title

                # Strip trailing " - Topic" suffix YouTube Music adds to channel names
                artist = re.sub(r'\s*-\s*Topic$', '', artist, flags=re.IGNORECASE).strip()

                # If title is "Artist - Title" and we have no artist from metadata, split it
                if " - " in raw_title and not entry.get("artist"):
                    parts  = raw_title.split(" - ", 1)
                    artist = parts[0].strip()
                    title  = parts[1].strip()

                # Clean junk suffixes from title
                title = _JUNK_RE.sub("", title).strip()

                if title:
                    tracks.append({"artist": artist, "title": title})

    tracks = _playlist_canonicalize_tracks(tracks)
    # Match each track against beets library
    matched, missing = _match_playlist_tracks(tracks)

    return {
        "ok": True,
        "tracks": tracks,
        "matched": matched,
        "missing": missing,
        "total": len(tracks),
    }, 200


# ── Playlist download (direct sources → beet singleton import) ────────────────

_pl_dl_jobs: Dict[str, Any] = {}


_PL_DL_LOCK = threading.Lock()


_PLAYLIST_PIPELINE_START_GUARD = threading.Lock()


_PLAYLIST_PIPELINE_RUNTIME_LOCKS: Dict[str, threading.Lock] = {}


_PLAYLIST_PIPELINE_RUNTIME_LOCKS_GUARD = threading.Lock()


_PLAYLIST_DUPLICATE_JOB_MESSAGE = "A pipeline is already running for this playlist."


def _playlist_pipeline_runtime_lock(name: str) -> threading.Lock:
    clean_name = _clean_playlist_name(name)
    with _PLAYLIST_PIPELINE_RUNTIME_LOCKS_GUARD:
        lock = _PLAYLIST_PIPELINE_RUNTIME_LOCKS.get(clean_name)
        if lock is None:
            lock = threading.Lock()
            _PLAYLIST_PIPELINE_RUNTIME_LOCKS[clean_name] = lock
        return lock


def _playlist_running_pipeline_job_id(name: str) -> str:
    clean_name = _clean_playlist_name(name)
    with _PL_DL_LOCK:
        for jid, state in _pl_dl_jobs.items():
            if (
                _s(state.get("status") or "") == "running"
                and _s(state.get("playlist_name") or "") == clean_name
            ):
                return _s(jid)
    manifest = _playlist_read_manifest(clean_name)
    pipeline = manifest.get("last_pipeline") if isinstance(manifest.get("last_pipeline"), dict) else {}
    if _s(pipeline.get("status") or "") != "running":
        return ""
    jobs_job_id = _s(pipeline.get("jobs_job_id") or "")
    if not jobs_job_id:
        return ""
    try:
        job = jobs.get(jobs_job_id)
    except Exception:
        job = None
    if job and _s(getattr(job, "status", "")) == "running":
        return jobs_job_id
    return ""


def _playlist_job_track_payload(track: Dict[str, Any]) -> Dict[str, str]:
    return {
        "artist": _norm(track.get("artist") or track.get("query_artist") or ""),
        "title": _norm(track.get("title") or track.get("query_title") or ""),
    }


def _playlist_job_key_payload(name: str, source: str, content: str,
                              tracks: List[Dict[str, Any]],
                              all_tracks: List[Dict[str, Any]],
                              playlist_id: str = "") -> Dict[str, Any]:
    source_text = _s(source or "").strip().lower()
    content_text = _s(content or "").strip()
    track_basis = all_tracks or tracks or []
    clean_name = _clean_playlist_name(name)
    explicit_pid = _s(playlist_id).strip()
    pid = _playlist_resolve_stable_id(clean_name, playlist_id=explicit_pid or None)
    if not _playlist_valid_internal_id(pid):
        raise PlaylistStateError(
            "playlist_identity_unresolved",
            "Persistent playlist_id is required for playlist checkpoint state.",
        )
    playlist_key = _playlist_key(clean_name, playlist_id=pid, allocate=False)
    return {
        "name": clean_name,
        "playlist_id": pid,
        "playlist_key": playlist_key,
        "source": source_text,
        "content": content_text if content_text else "",
        "requested": [_playlist_job_track_payload(track) for track in (tracks or [])],
        "tracks": [_playlist_job_track_payload(track) for track in track_basis],
    }


def _playlist_job_id_for_key(key_payload: Dict[str, Any]) -> str:
    raw = json.dumps(key_payload, sort_keys=True, ensure_ascii=True)
    return "pl-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


_PLAYLIST_JOB_ID_RE = re.compile(r"^pl-[0-9a-f]{20}$")


def _playlist_valid_job_id(value: Any) -> bool:
    return bool(_PLAYLIST_JOB_ID_RE.match(_s(value).strip()))


def _playlist_job_state_path(jid: str) -> Path:
    safe = _s(jid).strip()
    if not _playlist_valid_job_id(safe):
        raise PlaylistStateError("invalid_job_id", "Invalid playlist job id.")
    return PLAYLIST_JOB_STATE_DIR / f"{safe}.json"


def _playlist_job_state_safe_read_text(path: Path) -> str:
    """Read a checkpoint file's text, refusing to follow a symlink leaf.

    glob("pl-*.json") can return a symlink, and plain Path.read_text()
    follows it -- a surprising/malicious state symlink must not cause
    reading an arbitrary file outside the job-state root (SEC-002 Wave 11
    second final review; the write/delete paths already refuse symlinks,
    this closes the same gap on the read side).
    """
    if path.is_symlink():
        raise ValueError("checkpoint path is a symlink")
    return path.read_text(encoding="utf-8")


def _playlist_load_job_state(jid: str, *, strict: bool = False) -> Dict[str, Any]:
    try:
        path = _playlist_job_state_path(jid)
        data = json.loads(_playlist_job_state_safe_read_text(path))
        if isinstance(data, dict):
            return data
        raise ValueError("checkpoint root is not an object")
    except PlaylistStateError:
        if strict:
            raise
        return {}
    except Exception as exc:
        if strict:
            raise PlaylistStateError(
                "checkpoint_corrupt",
                "Playlist checkpoint is corrupt; manual review is required.",
            ) from exc
        return {}


def _playlist_sanitize_job_state_for_persistence(state: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(state, dict):
        return {}
    cleaned = copy.deepcopy(state)
    sensitive_keys = {
        "token", "api_key", "apikey", "api-key", "password", "secret",
        "authorization", "cookie", "set-cookie", "plex_token",
        "lidarr_api_key", "slskd_api_key", "control_agent_token",
        "signed_url", "signed-url",
    }

    def _redact_string(value: str) -> str:
        lowered = value.lower()
        secret_markers = (
            "token=", "access_token=", "bearer ", "authorization:",
            "api_key=", "apikey=", "api-key=", "secret=", "password=",
            "cookie:", "set-cookie:", "x-amz-signature=", "signature=",
            "sig=",
        )
        return _redact_security_text(value) if any(marker in lowered for marker in secret_markers) else value

    def _redact_dict(d: dict):
        for k, v in list(d.items()):
            if any(s in str(k).lower() for s in sensitive_keys):
                d[k] = "[REDACTED]"
            elif isinstance(v, dict):
                _redact_dict(v)
            elif isinstance(v, list):
                _redact_list(v)
            elif isinstance(v, str):
                d[k] = _redact_string(v)

    def _redact_list(l: list):
        for idx, item in enumerate(l):
            if isinstance(item, dict):
                _redact_dict(item)
            elif isinstance(item, list):
                _redact_list(item)
            elif isinstance(item, str):
                l[idx] = _redact_string(item)

    _redact_dict(cleaned)
    return cleaned


def _playlist_save_job_state(state: Dict[str, Any]) -> None:
    jid = _s(state.get("job_id") or "")
    if not jid:
        return
    sanitized_state = _playlist_sanitize_job_state_for_persistence(state)
    try:
        PLAYLIST_JOB_STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = _playlist_job_state_path(jid)
        _playlist_atomic_json_replace(
            path,
            sanitized_state,
            save_key=jid,
            label="playlist checkpoint",
            indent=None,
            sort_keys=True,
        )
    except Exception as exc:
        log = state.get("log")
        if isinstance(log, list):
            message = f"Checkpoint save failed; previous checkpoint left intact: {exc}"
            if message not in log[-5:]:
                log.append(message)


def _playlist_job_state_path_for_delete(jid: str) -> Path:
    path = _playlist_job_state_path(jid)
    root = PLAYLIST_JOB_STATE_DIR
    if root.exists() and root.is_symlink():
        raise PlaylistStateError("playlist_state_unavailable", "Playlist job-state directory is unsafe.")
    resolved_root = root.resolve(strict=False)
    resolved_parent = path.parent.resolve(strict=False)
    try:
        resolved_parent.relative_to(resolved_root)
    except ValueError as exc:
        raise PlaylistStateError("invalid_job_id", "Invalid playlist job id.") from exc
    if path.is_symlink():
        return path
    resolved_path = path.resolve(strict=False)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise PlaylistStateError("invalid_job_id", "Invalid playlist job id.") from exc
    return path


def _playlist_delete_job_state(jid: str) -> bool:
    path = _playlist_job_state_path_for_delete(jid)
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_dir() and not path.is_symlink():
        raise PlaylistStateError("invalid_job_id", "Invalid playlist job id.")
    path.unlink()
    return True


def _playlist_interrupted_saved_job_state(jid: str, saved: Dict[str, Any]) -> Dict[str, Any]:
    state = dict(saved or {})
    state["job_id"] = _s(state.get("job_id") or jid)
    state["status"] = "error"
    state["phase"] = "interrupted"
    state["current"] = "previous playlist job was interrupted; start again to resume"
    state["interrupted"] = True
    state["updated_at"] = time.time()
    log = list(state.get("log") or [])
    message = "Previous playlist download was interrupted by an app restart; use Resume Pipeline to continue from the checkpoint."
    if message not in log[-5:]:
        log.append(message)
    state["log"] = log[-1000:]
    _playlist_save_job_state(state)
    return state


def _playlist_job_state_stamp(state: Dict[str, Any]) -> float:
    stamp = 0.0
    for key in ("updated_at", "created_at", "_state_mtime"):
        try:
            stamp = max(stamp, float(state.get(key) or 0))
        except Exception:
            pass
    return stamp


def _playlist_job_is_live(jid: str) -> bool:
    with _PL_DL_LOCK:
        state = _pl_dl_jobs.get(jid)
        return bool(state and _s(state.get("status") or "").lower() == "running")


def _playlist_job_state_name(state: Dict[str, Any]) -> str:
    job_key = state.get("job_key") if isinstance(state.get("job_key"), dict) else {}
    for value in (
        job_key.get("name"),
        state.get("playlist"),
        state.get("playlist_name"),
        state.get("name"),
    ):
        clean = _clean_playlist_name(_s(value or "").strip())
        if clean:
            return clean
    return ""


def _playlist_saved_job_states_for_name(name: str,
                                        *,
                                        playlist_id: str = "",
                                        mark_interrupted: bool = False,
                                        strict: bool = False) -> List[Dict[str, Any]]:
    if not PLAYLIST_JOB_STATE_DIR.exists():
        return []
    clean_name = _clean_playlist_name(name) if name else ""
    target_pid = _s(playlist_id).strip()
    if target_pid and not _playlist_valid_internal_id(target_pid):
        if strict:
            raise PlaylistStateError("invalid_playlist_id", "Invalid playlist_id.")
        target_pid = ""
    if not target_pid and clean_name:
        try:
            target_pid = _playlist_resolve_stable_id(clean_name)
        except PlaylistStateError:
            if strict:
                raise
            target_pid = ""

    target_norm = _norm(name) if name else ""
    rows: List[Dict[str, Any]] = []
    # Ownership of an unparseable checkpoint is unknowable up front. It
    # must not immediately fail-closed a resume/delete for a *different*
    # playlist that has its own valid, attributable checkpoint elsewhere
    # (SEC-002 Wave 11 second final review: cross-playlist checkpoint-
    # corruption denial of service) -- but if the target ends up with no
    # discoverable checkpoint of its own at all, an unattributed corrupt
    # file might BE the one being looked for, so strict mode still raises
    # in that case rather than silently reporting "nothing to resume".
    # Nothing is deleted either way; a corrupt file just isn't listed.
    had_unattributed_corruption = False
    for path in PLAYLIST_JOB_STATE_DIR.glob("pl-*.json"):
        try:
            data = json.loads(_playlist_job_state_safe_read_text(path))
            if not isinstance(data, dict):
                raise ValueError("checkpoint root is not an object")
        except Exception:
            had_unattributed_corruption = True
            continue

        job_key = data.get("job_key") if isinstance(data.get("job_key"), dict) else {}
        ckpt_pid = _s(data.get("playlist_id") or job_key.get("playlist_id") or "").strip()
        job_name = _playlist_job_state_name(data)

        # Ownership match against the caller's target happens BEFORE any
        # strict format validation below -- a checkpoint that isn't this
        # playlist's own must be skipped regardless of what's wrong with
        # it; strict is a promise about the *target* playlist's own
        # checkpoint state, not about every unrelated file in the directory.
        if target_pid:
            owned_by_target = (
                ckpt_pid == target_pid if ckpt_pid
                else bool(target_norm) and _norm(job_name) == target_norm
            )
        elif target_norm:
            owned_by_target = _norm(job_name) == target_norm
        else:
            owned_by_target = True  # no filter requested: every checkpoint is in scope

        if not owned_by_target:
            continue

        if ckpt_pid and not _playlist_valid_internal_id(ckpt_pid):
            if strict:
                raise PlaylistStateError(
                    "checkpoint_corrupt",
                    "Playlist checkpoint contains an invalid playlist_id.",
                )
            continue

        jid = _s(data.get("job_id") or path.stem)
        if not _playlist_valid_job_id(jid):
            if strict:
                raise PlaylistStateError(
                    "checkpoint_corrupt",
                    "Playlist checkpoint contains an invalid job_id.",
                )
            continue
        data["job_id"] = jid
        try:
            data["_state_mtime"] = path.stat().st_mtime
        except Exception:
            pass
        if (
            mark_interrupted
            and _s(data.get("status") or "").lower() == "running"
            and not _playlist_job_is_live(jid)
        ):
            data = _playlist_interrupted_saved_job_state(jid, data)
        rows.append(data)

    if strict and had_unattributed_corruption and not rows and (target_pid or target_norm):
        raise PlaylistStateError(
            "checkpoint_corrupt",
            "Playlist checkpoint is corrupt; manual review is required.",
        )
    return rows


def _playlist_latest_job_state_summary(name: str,
                                       states: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    states = states if states is not None else _playlist_saved_job_states_for_name(name, mark_interrupted=True)
    if not states:
        return {}
    latest = max(states, key=_playlist_job_state_stamp)
    job_key = latest.get("job_key") if isinstance(latest.get("job_key"), dict) else {}
    state_tracks = latest.get("tracks") if isinstance(latest.get("tracks"), list) else []
    key_tracks = job_key.get("tracks") if isinstance(job_key.get("tracks"), list) else []
    tracks = state_tracks if len(state_tracks) >= len(key_tracks) else key_tracks
    missing = latest.get("missing") if isinstance(latest.get("missing"), list) else []
    return {
        "checkpoint_job_id": _s(latest.get("job_id") or ""),
        "checkpoint_status": _s(latest.get("status") or ""),
        "checkpoint_phase": _s(latest.get("phase") or ""),
        "checkpoint_current": _s(latest.get("current") or ""),
        "checkpoint_interrupted": bool(latest.get("interrupted")),
        "checkpoint_tracks": len(_playlist_clean_track_list(tracks)),
        "checkpoint_missing": len(_playlist_clean_track_list(missing)),
        "checkpoint_updated_at": _playlist_job_state_stamp(latest),
        "checkpoint_waiting_for_import": max(
            int(latest.get("waiting_for_import") or 0),
            _playlist_waiting_import_count_from_state(latest),
        ),
    }


def _playlist_saved_playlist_exists(name: str) -> bool:
    clean_name = _clean_playlist_name(name)
    if not clean_name:
        return False
    if _playlist_manifest_exists_no_create(clean_name):
        return True
    key = _playlist_existing_key(clean_name)
    if key:
        try:
            res = composite_workflows.read_playlist_m3u(key, fallback_name=clean_name)
            if isinstance(res, dict) and res.get("ok") and res.get("exists"):
                return True
        except Exception:
            pass
    return bool(_playlist_saved_job_states_for_name(clean_name, mark_interrupted=True))


def _playlist_missing_track_label(track: Dict[str, Any]) -> str:
    artist = _s(track.get("artist") or "").strip()
    title = _s(track.get("title") or "").strip()
    return " - ".join(part for part in [artist, title] if part) or "Untitled"


def _playlist_requested_track_key(track: Dict[str, Any]) -> tuple:
    return (
        _norm(track.get("artist") or track.get("query_artist") or ""),
        _norm(track.get("title") or track.get("query_title") or ""),
    )


def _playlist_download_methods(raw: Any = "") -> List[str]:
    if isinstance(raw, list):
        raw_value = ",".join(_s(item) for item in raw)
    else:
        raw_value = _s(raw)
    methods: List[str] = []
    for method in _download_method_list(raw_value or PLAYLIST_DOWNLOAD_METHODS):
        if method in {"slskd", "spotiflac", "ytdlp", "soundcloud"} and method not in methods:
            methods.append(method)
    return methods or ["slskd", "spotiflac", "ytdlp", "soundcloud"]


def _playlist_set_track_status(state: Dict[str, Any], track: Dict[str, Any],
                               status: str, *, method: str = "",
                               message: str = "", path: str = "",
                               _no_save: bool = False,
                               **extra: Any) -> None:
    status = {
        "queued": "pending",
        "matched": "available",
        "source_failed": "failed",
    }.get(status, status)
    if status not in PLAYLIST_PIPELINE_STATES:
        status = "failed"
    statuses = state.setdefault("track_statuses", {})
    key = _playlist_status_id(track)
    row = dict(statuses.get(key) or {})
    row.update({
        "id": key,
        "artist": _s(track.get("artist") or track.get("query_artist") or "").strip(),
        "title": _s(track.get("title") or track.get("query_title") or "").strip(),
        "status": status,
        "method": _download_method_label(method) if method else row.get("method", ""),
        "message": message,
        "path": path or row.get("path", ""),
        "updated_at": time.time(),
    })
    for meta_key in ("source_artist", "source_title", "canonicalized", "canonical_source"):
        if meta_key in track:
            row[meta_key] = track.get(meta_key)
    for xk, xv in extra.items():
        if xv not in (None, ""):
            row[xk] = xv
    statuses[key] = row
    state["track_status_list"] = list(statuses.values())
    if not _no_save:
        _playlist_save_job_state(state)
    playlist_name = _s(
        state.get("playlist_name")
        or (state.get("job_key") or {}).get("name")
        or ""
    ).strip()
    if playlist_name and not _no_save:
        _playlist_store_track_state(
            playlist_name,
            track,
            status,
            source=row.get("method", ""),
            message=message,
            failure_reason=message if status in {"failed", "review_required"} else "",
            staged_path=path if status in {"downloaded", "waiting_import", "importing", "failed", "review_required"} else "",
            path=path,
            **extra,
        )


def _playlist_safe_filename(value: str, fallback: str = "track") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", _s(value)).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or fallback)[:140].strip(" ._-") or fallback


def _playlist_validate_downloaded_files(paths: Iterable[str], artist: str, title: str, log,
                                        expected_mb_trackid: str = "",
                                        review_out: Optional[List[Dict[str, Any]]] = None,
                                        playlist_name: str = "",
                                        playlist_id: str = "") -> List[str]:
    valid: List[str] = []
    clean_name = _clean_playlist_name(playlist_name or "Playlist")
    key = _playlist_resolve_operation_key("", playlist_id, clean_name, log=log)

    for path_value in _playlist_filter_preview_downloads(paths, log):
        val_res = _playlist_validate_staged_download(
            path_value, artist, title, expected_mb_trackid,
            playlist_name=playlist_name, playlist_id=playlist_id)
        match = val_res.get("match") or {}
        if not match.get("ok"):
            identity = match.get("identity") if isinstance(match.get("identity"), dict) else {}
            action = _s(identity.get("final_action") or "review")
            if action == "reject":
                if key:
                    try:
                        composite_workflows.delete_playlist_staged_track(key, "", path_value)
                    except Exception:
                        pass
                else:
                    log(f"  warning: could not resolve playlist key; leaving rejected file in place: {Path(path_value).name}")
                log(
                    "  rejected mismatched download "
                    f"({_playlist_identity_log(match)}; "
                    f"title {float(match.get('title_score') or 0):.0%}, "
                    f"artist {float(match.get('artist_score') or 0):.0%}): "
                    f"{Path(path_value).name}"
                )
            else:
                if review_out is not None:
                    review_out.append({"path": path_value, "match": match})
                log(
                    "  review required: unable to verify downloaded audio "
                    f"({_playlist_identity_log(match)}): {Path(path_value).name}"
                )
            continue
        if not val_res.get("audio_allowed", True):
            continue
        _playlist_stamp_download_tags(path_value, artist, title, log)
        log(
            "  accepted fingerprint-verified download "
            f"({_playlist_identity_log(match)}): {Path(path_value).name}"
        )
        valid.append(path_value)
    return valid


def _playlist_inspect_staged_file(name: str,
                                  track_id: str,
                                  path_str: str,
                                  *,
                                  playlist_id: str = "") -> Dict[str, Any]:
    clean_name = _clean_playlist_name(name)
    staged_path = _s(path_str).strip()
    if not staged_path or "\x00" in staged_path:
        return {"ok": True, "exists": False, "authorized": False, "status": "invalid_path"}
    pid = _playlist_resolve_stable_id(clean_name, playlist_id=playlist_id or None)
    if not _playlist_valid_internal_id(pid):
        return {"ok": True, "exists": False, "authorized": False, "status": "missing_playlist_id"}
    playlist_key = _playlist_key(clean_name, playlist_id=pid, allocate=False)
    return composite_workflows.inspect_playlist_staged_track(playlist_key, track_id, staged_path)


def _is_safe_playlist_staged_file(path_str: str,
                                  playlist_name: str,
                                  *,
                                  playlist_id: str = "",
                                  track_id: str = "") -> bool:
    try:
        result = _playlist_inspect_staged_file(
            playlist_name, track_id, path_str, playlist_id=playlist_id)
    except BeetsUnavailableError:
        raise PlaylistStagingUnavailableError(
            "Engine is unavailable; cannot verify playlist staging"
        )
    except Exception:
        return False
    return bool(
        isinstance(result, dict)
        and result.get("ok")
        and result.get("exists")
        and result.get("authorized")
    )


def _playlist_reconcile_staged_files(name: str, dl_dir: Path,
                                      tracks: List[Dict[str, Any]], log,
                                      *,
                                      playlist_id: str = "") -> int:
    """On resume, ask the engine whether checkpointed staged files are still usable."""
    clean_name = _clean_playlist_name(name)
    manifest_states = _playlist_manifest_track_states(clean_name, playlist_id=playlist_id or None)

    stale = 0
    for key, row in list(manifest_states.items()):
        if not isinstance(row, dict):
            continue
        if _s(row.get("status") or "") not in {"downloaded", "waiting_import", "importing", "download_verified"}:
            continue
        staged_str = _s(row.get("staged_path") or row.get("path") or "")
        try:
            if _is_safe_playlist_staged_file(
                staged_str, clean_name, playlist_id=playlist_id, track_id=key):
                continue
        except PlaylistStagingUnavailableError:
            log("  Reconcile: engine staging validation unavailable; leaving checkpoint state unchanged")
            return stale
        trk: Dict[str, Any] = {
            "artist": _s(row.get("artist") or ""),
            "title": _s(row.get("title") or ""),
        }
        _playlist_store_track_state(
            clean_name, trk, "pending",
            message="staged file missing on resume; will re-download",
            staged_path="",
            failure_reason="stale_or_missing",
            playlist_id=playlist_id or None)
        stale += 1
    if stale:
        log(f"  Reconcile: {stale} staged file(s) missing on resume - will re-download")
    return stale


def _playlist_download_missing_tracks(
    tracks: List[Dict[str, Any]],
    dl_dir: Path,
    state: Dict[str, Any],
    log,
    methods_raw: Any = "",
) -> Dict[str, int]:
    # Historical staged paths are untrusted metadata. Resume reconciliation
    methods = _playlist_download_methods(methods_raw)
    state["download_methods"] = methods
    playlist_name = _clean_playlist_name(_s(state.get("playlist_name") or state.get("name") or "Playlist"))
    playlist_id = _s(state.get("playlist_id") or "")
    playlist_key = _playlist_key(playlist_name, playlist_id=playlist_id or None, allocate=False)

    log(
        "Downloading missing playlist tracks via sources: "
        + ", ".join(_download_method_label(method) for method in methods)
    )
    # Fail closed: if the engine cannot confirm playlist staging
    # directories exist and are safe, do not proceed to download tracks
    # into an unconfirmed location.
    _playlist_ensure_staging_dirs(playlist_name, playlist_id=playlist_id or None)

    before_done = int(state.get("done") or 0)
    before_failed = int(state.get("failed") or 0)
    before_review = _playlist_review_required_count_from_state(state)

    def _get_staged_paths() -> List[str]:
        # Engine ownership: playlist staging lives in the engine container.
        # Do not fall back to reading dl_dir directly on IPC failure -- an
        # empty/stale result here just means download methods will be
        # retried, which is the safe default; treating an unreachable
        # engine as "here is the ground truth from a local directory" is
        # not.
        try:
            res = composite_workflows.list_playlist_staged_files(playlist_key, playlist_id)
            if isinstance(res, dict) and res.get("ok"):
                return [f["path"] for f in res.get("files", []) if isinstance(f, dict) and f.get("path")]
        except Exception as ex:
            log(f"  warning: engine staged-file listing IPC failed: {ex}")
        return []

    for idx, trk in enumerate(tracks, start=1):
        artist = _s(trk.get("artist") or "").strip()
        title = _s(trk.get("title") or "").strip()
        label = _playlist_missing_track_label(trk)
        state["current"] = label
        log(f"[{idx}/{len(tracks)}] {label}")
        if not title:
            state["failed"] += 1
            _playlist_set_track_status(state, trk, "failed", message="missing title")
            log("  not downloaded: missing title")
            continue

        reused_files: List[str] = []
        reused_match: Dict[str, Any] = {}
        if not reused_files:
            reused_files = _playlist_reusable_download_files(trk, dl_dir, dl_dir, log, playlist_name=playlist_name, playlist_id=playlist_id)
        if reused_files:
            val_res = _playlist_validate_staged_download(
                reused_files[0], artist, title, _s(trk.get("mb_trackid") or ""),
                playlist_name=playlist_name, playlist_id=playlist_id)
            reused_match = val_res.get("match") or {}
            state["done"] += 1
            _playlist_set_track_status(
                state, trk, "waiting_import", method="resume",
                message="reused fingerprint-verified staged download; waiting for Beets import",
                path=reused_files[0],
                **_playlist_identity_status_fields(reused_match))
            continue

        _playlist_set_track_status(state, trk, "queued", message="waiting to search")
        trk_key = _playlist_status_id(trk)
        prev_row = dict(state.get("track_statuses", {}).get(trk_key) or {})
        attempt_count = int(prev_row.get("download_attempt_count") or 0) + 1
        wanted = [{"title": title}]
        downloaded = False
        for method in methods:
            before_files = set(_get_staged_paths())
            new_files: List[str] = []
            try:
                _playlist_set_track_status(
                    state, trk, "searching", method=method,
                    message=f"searching {_download_method_label(method)}",
                    download_attempt_count=attempt_count)
                log(f"  trying {_download_method_label(method)}")
                if method == "slskd":
                    new_files = _playlist_slskd_download_track(
                        artist, title, dl_dir, log, state["log"])
                elif method == "spotiflac":
                    _spotiflac_missing_tracks_download(
                        artist, "", "", str(dl_dir), state["log"], wanted)
                elif method == "soundcloud":
                    _ytdlp_missing_tracks_download(
                        artist, "", "", str(dl_dir), state["log"], wanted,
                        source="soundcloud")
                elif method == "ytdlp":
                    _ytdlp_missing_tracks_download(
                        artist, "", "", str(dl_dir), state["log"], wanted,
                        source="ytdlp")
                else:
                    continue
            except Exception as ex:
                _playlist_set_track_status(
                    state, trk, "source_failed", method=method, message=str(ex))
                log(f"  {_download_method_label(method)} failed: {ex}")
                continue

            if not new_files:
                after_files = set(_get_staged_paths())
                new_files = sorted(after_files - before_files)

            review_new_files: List[Dict[str, Any]] = []
            valid_new_files = _playlist_validate_downloaded_files(
                new_files, artist, title, log,
                expected_mb_trackid=_s(trk.get("mb_trackid") or ""),
                review_out=review_new_files,
                playlist_name=playlist_name,
                playlist_id=playlist_id)
            if valid_new_files:
                state["done"] += 1
                downloaded = True
                val_res = _playlist_validate_staged_download(
                    valid_new_files[0], artist, title, _s(trk.get("mb_trackid") or ""),
                    playlist_name=playlist_name, playlist_id=playlist_id)
                verified_match = val_res.get("match") or {}
                _file_size = int(val_res.get("size") or 0)
                _playlist_set_track_status(
                    state, trk, "waiting_import", method=method,
                    message="fingerprint verified; waiting for Beets import",
                    path=valid_new_files[0],
                    download_attempt_count=attempt_count,
                    file_size=_file_size,
                    **_playlist_identity_status_fields(verified_match))
                log(f"  downloaded via {_download_method_label(method)}")
                break
            if review_new_files:
                reviewed = review_new_files[0]
                review_match = reviewed.get("match") or {}
                review_path = _s(reviewed.get("path") or "")
                reason = _s((review_match.get("identity") or {}).get("decision_reason") or "downloaded audio needs review")
                _playlist_set_track_status(
                    state, trk, "review_required", method=method,
                    message=reason, path=review_path,
                    download_attempt_count=attempt_count,
                    **_playlist_identity_status_fields(review_match))
                downloaded = True
                log(f"  review required before import: {Path(review_path).name}")
                break
            log(f"  {_download_method_label(method)} produced no new file")

        if not downloaded:
            state["failed"] += 1
            _playlist_set_track_status(state, trk, "failed", message="not downloaded")
            log("  not downloaded")
    downloaded_count = int(state.get("done") or 0) - before_done
    failed_count = int(state.get("failed") or 0) - before_failed
    review_required_count = max(0, _playlist_review_required_count_from_state(state) - before_review)
    return {
        "downloaded": downloaded_count,
        "failed": failed_count,
        "review_required": review_required_count,
        "activity": downloaded_count + review_required_count,
    }


# Service behind POST /api/playlist/download (ARCH-001): request-free,
# returns (json_body, http_status); the route and in-process callers share it.
def start_playlist_download(payload_in: Dict[str, Any]) -> Tuple[Any, int]:
    payload = payload_in
    tracks  = payload.get("tracks") or []   # missing tracks to fetch: [{artist, title}]
    all_tracks = payload.get("all_tracks") or tracks
    tracks = _playlist_canonicalize_tracks(tracks)
    all_tracks = _playlist_canonicalize_tracks(all_tracks)
    sync_after_import = bool(payload.get("sync_after_import"))
    download_only = bool(payload.get("download_only"))
    pipeline_action = _s(payload.get("pipeline_action") or ("download" if download_only else "full" if sync_after_import else "download_import")).strip().lower()
    download_methods = _playlist_download_methods(
        payload.get("methods") or payload.get("download_methods") or "")
    parse_source = _s(payload.get("source") or "").strip()
    parse_content = _s(payload.get("content") or "").strip()
    name    = _clean_playlist_name(payload.get("name") or "Playlist")
    if not tracks and not sync_after_import and not parse_content and not download_only:
        return {"ok": False, "error": "No tracks to download"}, 200

    requested_playlist_id = _s(payload.get("playlist_id") or "").strip()
    try:
        playlist_id = _playlist_resolve_stable_id(name, playlist_id=requested_playlist_id or None)
        if not _playlist_valid_internal_id(playlist_id):
            raise PlaylistStateError(
                "playlist_identity_unresolved",
                "Persistent playlist_id is required for playlist download checkpoints.",
            )
        playlist_key = _playlist_key(name, playlist_id=playlist_id, allocate=False)
        job_key = _playlist_job_key_payload(
            name, parse_source, parse_content, tracks, all_tracks, playlist_id=playlist_id)
    except PlaylistStateError as exc:
        _app_logger.warning("Playlist download identity error: %s (%s)", exc.code, type(exc).__name__)
        return _playlist_state_error_payload(exc), _playlist_state_error_status(exc)
    job_key["action"] = pipeline_action
    jid = _playlist_job_id_for_key(job_key)
    with _PL_DL_LOCK:
        existing = _pl_dl_jobs.get(jid)
        if existing and existing.get("status") == "running":
            return {
                "ok": True,
                "job_id": jid,
                "jobs_job_id": _s(existing.get("jobs_job_id") or ""),
                "resumed": True,
            }, 200
        for _other_jid, _other_st in _pl_dl_jobs.items():
            if (_other_jid != jid
                    and _s(_other_st.get("status") or "") == "running"
                    and _s(_other_st.get("playlist_name") or "") == name):
                return {
                    "ok": False,
                    "error": _PLAYLIST_DUPLICATE_JOB_MESSAGE,
                    "running_job_id": _other_jid,
                }, 409
    running_pipeline = _playlist_running_pipeline_job_id(name)
    if running_pipeline:
        return {
            "ok": False,
            "error": _PLAYLIST_DUPLICATE_JOB_MESSAGE,
            "running_job_id": running_pipeline,
        }, 409
    try:
        _playlist_ensure_staging_dirs(name, playlist_id=playlist_id)
    except PlaylistStagingUnavailableError:
        return {
            "ok": False,
            "error": "staging_unavailable",
            "message": "Engine is unavailable; cannot stage playlist downloads right now",
        }, 503
    dl_dir = _playlist_downloads_dir(name, playlist_id=playlist_id)
    try:
        saved_state = _playlist_load_job_state(jid, strict=True) if _playlist_job_state_path(jid).exists() else {}
    except PlaylistStateError as exc:
        _app_logger.warning("Playlist checkpoint load error: %s (%s)", exc.code, type(exc).__name__)
        return _playlist_state_error_payload(exc), _playlist_state_error_status(exc)
    if not tracks:
        # No tracks provided in the request — try to recover the track list so the
        # pipeline can resume without requiring the caller to re-supply all tracks.
        def _extract_tracks(state_dict):
            t = state_dict.get("tracks") if isinstance(state_dict.get("tracks"), list) else []
            if not t:
                jk = state_dict.get("job_key") if isinstance(state_dict.get("job_key"), dict) else {}
                t = jk.get("tracks") if isinstance(jk.get("tracks"), list) else []
            return t
        _recover_tracks: List[Dict[str, Any]] = []
        if saved_state:
            _recover_tracks = _extract_tracks(saved_state)
        if not _recover_tracks:
            _prior = sorted(
                _playlist_saved_job_states_for_name(name, playlist_id=playlist_id, strict=True),
                key=_playlist_job_state_stamp, reverse=True)
            for _ps in _prior:
                _ps_tracks = _extract_tracks(_ps)
                if _ps_tracks:
                    _old_jid = _s(_ps.get("job_id") or "")
                    if _old_jid and _old_jid != jid:
                        jid = _old_jid
                        saved_state = _ps
                        job_key = _ps.get("job_key") or job_key
                    _recover_tracks = _ps_tracks
                    break
        if _recover_tracks:
            tracks = _recover_tracks
            all_tracks = _recover_tracks
    state: Dict[str, Any] = {
        "status": "running", "log": [], "done": 0, "failed": 0,
        "total": len(tracks), "current": "", "dl_dir": str(dl_dir),
        "phase": "parse" if parse_content else "queued",
        "import_job_id": None, "import_returncode": None,
        "playlist": None, "matched_after_import": 0, "missing_after_import": 0,
        "tracks": [], "matched": [], "missing": [],
        "matched_initial": 0, "missing_initial": 0,
        "round": 0, "max_rounds": 0,
        "download_batch_size": PLAYLIST_DOWNLOAD_BATCH_SIZE,
        "download_batch_remaining": 0,
        "download_methods": download_methods,
        "track_statuses": {}, "track_status_list": [],
        "job_id": jid, "job_key": job_key, "playlist_id": playlist_id,
        "playlist_key": playlist_key, "created_at": time.time(),
        "resumed": False, "playlist_name": name,
        "pipeline_action": pipeline_action,
        "waiting_for_import": 0,
    }
    if saved_state:
        previous_log = list(saved_state.get("log") or [])[-300:]
        previous_statuses = saved_state.get("track_statuses") or {}
        state.update({
            "log": previous_log,
            "done": 0,
            "failed": 0,
            "track_statuses": previous_statuses if isinstance(previous_statuses, dict) else {},
            "track_status_list": list(previous_statuses.values()) if isinstance(previous_statuses, dict) else [],
            "round": int(saved_state.get("round") or 0),
            "created_at": float(saved_state.get("created_at") or state["created_at"]),
            "resumed": True,
        })
        state["log"].append("Resuming previous playlist download checkpoint")
    with _PL_DL_LOCK:
        existing = _pl_dl_jobs.get(jid)
        if existing and existing.get("status") == "running":
            return {
                "ok": True,
                "job_id": jid,
                "jobs_job_id": _s(existing.get("jobs_job_id") or ""),
                "resumed": True,
            }, 200
        for _other_jid, _other_st in _pl_dl_jobs.items():
            if (_other_jid != jid
                    and _s(_other_st.get("status") or "") == "running"
                    and _s(_other_st.get("playlist_name") or "") == name):
                return {
                    "ok": False,
                    "error": _PLAYLIST_DUPLICATE_JOB_MESSAGE,
                    "running_job_id": _other_jid,
                }, 409
        _pl_dl_jobs[jid] = state
    _playlist_save_job_state(state)

    def _log(msg: str):
        state["log"].append(msg)
        if len(state["log"]) > 1000:
            del state["log"][:-1000]
        state["updated_at"] = time.time()
        _playlist_save_job_state(state)

    def _run(job_log: Optional[List[str]] = None, cancel_event=None, update_state=None):
        runtime_lock = _playlist_pipeline_runtime_lock(name)
        if not runtime_lock.acquire(blocking=False):
            state["status"] = "error"
            state["phase"] = "error"
            state["current"] = _PLAYLIST_DUPLICATE_JOB_MESSAGE
            _playlist_save_job_state(state)
            raise RuntimeError(_PLAYLIST_DUPLICATE_JOB_MESSAGE)
        try:
            # One pipeline per playlist across processes and restarts; the
            # durable job record carries the resumable position.
            contract = job_contract.enter(
                "playlist-" + job_contract.slug(_clean_playlist_name(name)), log=job_log, cancel_event=cancel_event,
                update_state=update_state,
                progress=lambda: {"playlist_job_id": jid, "phase": state.get("phase"), "status": state.get("status"),
                                  "done": state.get("done"), "failed": state.get("failed")})
        except Exception as exc:
            runtime_lock.release()
            state["status"] = "error"
            state["phase"] = "error"
            state["current"] = str(exc)
            _playlist_save_job_state(state)
            raise
        if job_log is not None and state.get("log") is not job_log:
            previous_log = list(state.get("log") or [])
            job_log.extend(previous_log)
            state["log"] = job_log
            _playlist_save_job_state(state)
        try:
            active_tracks = list(tracks or [])
            active_all_tracks = list(all_tracks or [])
            if not state.get("resumed"):
                for track in active_tracks:
                    _playlist_set_track_status(state, track, "missing", message="not in Beets", _no_save=True)
                if active_tracks:
                    _playlist_save_job_state(state)
            if parse_content:
                # On resume with a checkpoint, skip the expensive YouTube re-fetch and
                # library scan — use the saved manifest tracks + checkpoint statuses instead.
                _resumed_parse = bool(state.get("resumed") and state.get("track_statuses"))
                state["phase"] = "parse"
                if _resumed_parse:
                    state["current"] = "reading playlist track list"
                    _log("Resume: reading track list from manifest (skipping YouTube fetch and library match)...")
                    _manifest_for_resume = _playlist_read_manifest(_clean_playlist_name(name))
                    active_all_tracks = _playlist_clean_track_list(
                        _manifest_for_resume.get("desired_tracks") or []
                    )
                    if not active_all_tracks:
                        _resumed_parse = False
                        _log("Resume: manifest empty — falling back to full playlist fetch...")
                else:
                    state["current"] = "reading playlist track list"
                    _log("Reading playlist track list and matching against Beets...")
                if not _resumed_parse:
                    _playlist_save_job_state(state)
                    parse_resp = parse_playlist_request({"source": parse_source or "url", "content": parse_content,
                              "skip_match": False},
                    )
                    parsed = _json_from_flask_response(parse_resp)
                    if not parsed.get("ok"):
                        raise RuntimeError(parsed.get("error") or "playlist parse failed")
                    active_all_tracks = list(parsed.get("tracks") or [])
                    active_all_tracks = _playlist_apply_tombstones(name, active_all_tracks)
                    active_tracks = list(parsed.get("missing") or [])
                    active_tracks = _playlist_apply_tombstones(name, active_tracks)
                    matched_initial = list(parsed.get("matched") or [])
                    active_keys = {_playlist_status_id(track) for track in active_all_tracks}
                    matched_initial = [
                        track for track in matched_initial
                        if _playlist_status_id(track) in active_keys
                    ]
                    _playlist_write_manifest(
                        name,
                        active_all_tracks,
                        matched_tracks=matched_initial,
                        missing_tracks=active_tracks,
                        source=parse_source,
                        content=parse_content,
                        log=state["log"],
                    )
                    state["tracks"] = active_all_tracks
                    state["matched"] = matched_initial
                    state["missing"] = active_tracks
                    state["matched_initial"] = len(matched_initial)
                    state["missing_initial"] = len(active_tracks)
                    state["total"] = len(active_tracks)
                    state["track_statuses"] = {}
                    state["track_status_list"] = []
                    for track in matched_initial:
                        _playlist_set_track_status(
                            state, track, "matched", message="already in Beets",
                            path=_s(track.get("path") or ""), _no_save=True)
                    for track in active_tracks:
                        _playlist_set_track_status(state, track, "missing", message="not in Beets", _no_save=True)
                    if active_all_tracks:
                        _playlist_save_job_state(state)
                    _log(
                        f"Playlist read: {len(active_all_tracks)} track(s), "
                        f"{len(matched_initial)} matched, {len(active_tracks)} missing"
                    )
                else:
                    active_all_tracks = _playlist_apply_tombstones(name, active_all_tracks)
                    _done_statuses_r = {"available", "matched"}
                    matched_initial = [
                        t for t in active_all_tracks
                        if state["track_statuses"].get(_playlist_status_id(t), {}).get("status") in _done_statuses_r
                    ]
                    active_tracks = [
                        t for t in active_all_tracks
                        if state["track_statuses"].get(_playlist_status_id(t), {}).get("status") not in _done_statuses_r
                    ]
                    active_tracks = _playlist_apply_tombstones(name, active_tracks)
                    state["tracks"] = active_all_tracks
                    state["matched"] = matched_initial
                    state["missing"] = active_tracks
                    state["matched_initial"] = len(matched_initial)
                    state["missing_initial"] = len(active_tracks)
                    state["total"] = len(active_tracks)
                    for track in matched_initial:
                        _playlist_set_track_status(
                            state, track, "matched", message="already in Beets",
                            path=_s(track.get("path") or ""), _no_save=True)
                    for track in active_tracks:
                        _playlist_set_track_status(state, track, "missing", message="not in Beets", _no_save=True)
                    if active_all_tracks:
                        _playlist_save_job_state(state)
                    _log(
                        f"Resume: {len(matched_initial)} already available, "
                        f"{len(active_tracks)} still need download"
                    )
            else:
                if not active_all_tracks:
                    active_all_tracks = list(active_tracks)
                active_all_tracks = _playlist_apply_tombstones(name, active_all_tracks)
                active_tracks = _playlist_apply_tombstones(name, active_tracks)
                requested_only = bool(active_tracks)
                if active_all_tracks:
                    requested_only = bool(active_tracks)
                    if state.get("resumed") and state.get("track_statuses"):
                        # Resume without re-scanning the library — derive from saved statuses.
                        _done_statuses_r = {"available", "matched", "imported"}
                        matched_initial = [
                            t for t in active_all_tracks
                            if state["track_statuses"].get(_playlist_status_id(t), {}).get("status") in _done_statuses_r
                        ]
                        missing_initial = [
                            t for t in active_all_tracks
                            if state["track_statuses"].get(_playlist_status_id(t), {}).get("status") not in _done_statuses_r
                        ]
                        active_tracks = missing_initial
                        _log(f"Resume: {len(matched_initial)} already available, {len(active_tracks)} still need download")
                    else:
                        state["phase"] = "match"
                        state["current"] = "checking Beets before download"
                        requested_only = bool(active_tracks)
                        precheck_tracks = list(active_tracks if requested_only else active_all_tracks)
                        if requested_only:
                            _log("Checking requested missing playlist tracks against Beets before download...")
                        else:
                            _log("Checking playlist tracks against Beets before sync...")
                        matched_initial, missing_initial = _match_playlist_tracks(precheck_tracks)
                    state["tracks"] = active_all_tracks
                    state["matched"] = matched_initial
                    state["missing"] = missing_initial
                    state["matched_initial"] = len(matched_initial)
                    state["missing_initial"] = len(missing_initial)
                    for track in matched_initial:
                        _playlist_set_track_status(
                            state, track, "matched", message="already in Beets",
                            path=_s(track.get("path") or ""), _no_save=True)
                    for track in missing_initial:
                        _playlist_set_track_status(
                            state, track, "missing", message="not in Beets", _no_save=True)
                    if requested_only:
                        active_tracks = missing_initial
                    else:
                        active_tracks = []
                    state["total"] = len(active_tracks)
                    if requested_only:
                        _log(
                            f"Pre-download requested-track check: {len(matched_initial)} already available, "
                            f"{len(missing_initial)} still need download"
                        )
                    else:
                        _log(
                            f"Pre-sync Beets check: {len(matched_initial)} matched, "
                            f"{len(missing_initial)} missing"
                        )

            # When resuming with staged files: reconcile + import them before new downloads
            if state.get("resumed") and not download_only and active_tracks:
                _log("Resume: reconciling staged downloads from previous checkpoint...")
                state["phase"] = "reconcile"
                state["current"] = "reconciling staged files"
                _playlist_save_job_state(state)
                _playlist_reconcile_staged_files(
                    name, dl_dir, active_tracks, _log, playlist_id=playlist_id)
                staged_entries = _playlist_staged_entries(name, playlist_id=playlist_id)
                active_keys = {_playlist_status_id(t) for t in active_tracks}
                staged_entries = [
                    (t, p) for t, p in staged_entries
                    if _playlist_status_id(t) in active_keys
                ]
                if staged_entries:
                    _n_staged = len(staged_entries)
                    _log(
                        f"Resume: found {_n_staged} previously downloaded file(s); "
                        "importing before continuing downloads"
                    )
                    state["phase"] = "import"
                    state["current"] = f"importing {_n_staged} staged file(s) from previous run"
                    state["waiting_for_import"] = _n_staged
                    _playlist_save_job_state(state)
                    if cancel_event is not None and cancel_event.is_set():
                        paused = bool(state.get("pause_requested"))
                        state["status"] = "paused" if paused else "stopped"
                        state["phase"] = state["status"]
                        state["current"] = "resume from checkpoint" if paused else "stopped by user"
                        _playlist_save_job_state(state)
                        _playlist_record_pipeline(name, status=state["status"], action=pipeline_action)
                        return {"playlist_job_id": jid, "playlist": name, "status": state["status"]}
                    try:
                        _playlist_run_import_downloaded(name, state["log"], cancel_event=cancel_event, playlist_id=playlist_id)
                        state["waiting_for_import"] = 0
                        _log(f"Resume: pre-download import of {_n_staged} staged file(s) complete")
                        # Use manifest states to find newly-available tracks instead of
                        # re-scanning the full 30K-item library (which OOMs over NFS).
                        _manifest_states_post = _playlist_manifest_track_states(_clean_playlist_name(name))
                        _done_post = {"available", "matched"}
                        _matched_pre = [
                            t for t in active_tracks
                            if _manifest_states_post.get(_playlist_status_id(t), {}).get("status") in _done_post
                        ]
                        _missing_pre = [
                            t for t in active_tracks
                            if _manifest_states_post.get(_playlist_status_id(t), {}).get("status") not in _done_post
                        ]
                        active_tracks = _missing_pre
                        state["matched"] = list(state.get("matched") or []) + _matched_pre
                        state["missing"] = active_tracks
                        state["total"] = len(active_tracks)
                        for _trk in _matched_pre:
                            _playlist_set_track_status(
                                state, _trk, "available",
                                message="imported from previous checkpoint",
                                path=_s(_trk.get("path") or ""))
                        _log(
                            f"Resume: {len(_matched_pre)} track(s) imported from checkpoint, "
                            f"{len(active_tracks)} still need download"
                        )
                    except RuntimeError as _ex:
                        state["waiting_for_import"] = 0
                        _log(f"Resume: pre-download import warning — {_ex}")
                    if cancel_event is not None and cancel_event.is_set():
                        paused = bool(state.get("pause_requested"))
                        state["status"] = "paused" if paused else "stopped"
                        state["phase"] = state["status"]
                        state["current"] = "resume from checkpoint" if paused else "stopped by user"
                        _playlist_save_job_state(state)
                        _playlist_record_pipeline(name, status=state["status"], action=pipeline_action)
                        return {"playlist_job_id": jid, "playlist": name, "status": state["status"]}
                else:
                    _log("Resume: no staged downloads found; starting fresh download round")
                    state["waiting_for_import"] = 0

            max_rounds = max(1, int(os.environ.get("PLAYLIST_DOWNLOAD_MAX_ROUNDS", "8") or "8"))
            batch_size = int(PLAYLIST_DOWNLOAD_BATCH_SIZE or 0)
            state["max_rounds"] = max_rounds
            state["download_batch_size"] = batch_size
            round_num = 0
            while active_tracks and round_num < max_rounds:
                if cancel_event is not None and cancel_event.is_set():
                    paused = bool(state.get("pause_requested"))
                    state["status"] = "paused" if paused else "stopped"
                    state["phase"] = state["status"]
                    state["current"] = "resume from checkpoint" if paused else "stopped by user"
                    _playlist_save_job_state(state)
                    _playlist_record_pipeline(name, status=state["status"], action=pipeline_action)
                    _log("Playlist pipeline paused at checkpoint." if paused else "Playlist pipeline stopped by user.")
                    return {"playlist_job_id": jid, "playlist": name, "status": state["status"]}
                round_num += 1
                state["round"] = round_num
                state["phase"] = "download"
                batch_tracks = active_tracks[:batch_size] if batch_size > 0 else list(active_tracks)
                deferred_tracks = active_tracks[len(batch_tracks):] if batch_size > 0 else []
                if not batch_tracks:
                    break
                state["download_batch_remaining"] = len(deferred_tracks)
                state["current"] = (
                    f"download round {round_num}: {len(batch_tracks)} of {len(active_tracks)}"
                    if deferred_tracks else f"download round {round_num}"
                )
                previous_missing = list(batch_tracks)
                previous_missing_keys = {
                    _playlist_requested_track_key(track) for track in previous_missing
                }
                previous_matched = len(state.get("matched") or [])
                round_dl_dir = dl_dir
                _log("")
                _log(
                    f"{'='*40}\n"
                    f"Missing-track round {round_num}/{max_rounds}: "
                    f"{len(active_tracks)} track(s) still missing"
                )
                if deferred_tracks:
                    _log(
                        f"PLAYLIST_DOWNLOAD_BATCH_SIZE={batch_size}: downloading "
                        f"{len(batch_tracks)} of {len(active_tracks)} missing track(s); "
                        f"{len(deferred_tracks)} remain queued"
                    )
                round_result = _playlist_download_missing_tracks(
                    batch_tracks, round_dl_dir, state, _log, download_methods)
                verified_downloaded = int(round_result.get("downloaded") or 0)
                review_downloaded = int(round_result.get("review_required") or 0)
                failed_downloads = int(round_result.get("failed") or 0)
                if review_downloaded:
                    _log(
                        f"Round {round_num} download complete: "
                        f"{verified_downloaded} fingerprint-verified, "
                        f"{review_downloaded} held for review, "
                        f"{failed_downloads} not downloaded"
                    )
                else:
                    _log(
                        f"Round {round_num} download complete: "
                        f"{verified_downloaded} downloaded, "
                        f"{failed_downloads} not downloaded"
                    )
                if cancel_event is not None and cancel_event.is_set():
                    paused = bool(state.get("pause_requested"))
                    state["status"] = "paused" if paused else "stopped"
                    state["phase"] = state["status"]
                    state["current"] = "resume from checkpoint" if paused else "stopped by user"
                    _playlist_save_job_state(state)
                    _playlist_record_pipeline(name, status=state["status"], action=pipeline_action)
                    _log("Playlist pipeline paused at checkpoint." if paused else "Playlist pipeline stopped by user.")
                    return {"playlist_job_id": jid, "playlist": name, "status": state["status"]}

                pause_after_this_round = False
                if review_downloaded > 0:
                    review_total = _playlist_review_required_count_from_state(state)
                    waiting_total = _playlist_waiting_import_count_from_state(state)
                    state["phase"] = "review"
                    state["current"] = f"{review_total} downloaded file(s) need review before import"
                    state["review_required"] = review_total
                    state["waiting_for_import"] = waiting_total
                    _playlist_save_job_state(state)
                    if verified_downloaded <= 0:
                        _log(
                            f"{review_downloaded} downloaded file(s) require review before import; "
                            "stopping automatic import for this round."
                        )
                        break
                    # Some tracks in this round need review, but others were
                    # already fingerprint-verified — import those now instead
                    # of discarding a whole round's verified downloads just
                    # because a sibling track needs review. Stop starting new
                    # rounds after this one so the pending review can be
                    # resolved before more downloads pile up.
                    _log(
                        f"{verified_downloaded} fingerprint-verified file(s) will be imported now; "
                        f"{review_downloaded} other downloaded file(s) require review and are held back. "
                        "Stopping further rounds until review is resolved."
                    )
                    pause_after_this_round = True

                if verified_downloaded <= 0:
                    _log(
                        "No new files were downloaded in this round; "
                        "stopping missing-track loop."
                    )
                    break
                if download_only:
                    state["phase"] = "downloaded"
                    state["current"] = "downloaded files are ready to import"
                    if deferred_tracks:
                        _log(
                            f"Batch limit reached; {len(deferred_tracks)} track(s) "
                            "remain for the next Download Missing run."
                        )
                    _log("Download Missing complete; staged files are ready for Import Downloaded.")
                    break

                state["phase"] = "import"
                state["current"] = f"importing stable staged downloads after round {round_num}"
                state["waiting_for_import"] = _playlist_waiting_import_count_from_state(state)
                _playlist_save_job_state(state)
                _log(
                    "Starting Beets singleton import from stable playlist staging "
                    f"({_playlist_imports_dir(name, playlist_id=playlist_id)}) ..."
                )
                import_summary = _playlist_run_import_downloaded(
                    name, state["log"], cancel_event=cancel_event, playlist_id=playlist_id)
                state["waiting_for_import"] = 0
                state["import_returncode"] = 0
                placement_summary = import_summary.get("placement") or {}
                state["last_placement"] = placement_summary
                _playlist_save_job_state(state)
                _log(
                    f"Beets singleton import round {round_num} complete: "
                    f"{int(import_summary.get('imported_files') or 0)} staged file(s)"
                )
                if placement_summary.get("placed"):
                    _log(
                        f"Placed {placement_summary.get('placed')} playlist import(s) "
                        "into normal album folders"
                    )
                elif placement_summary.get("matched_candidates"):
                    _log(
                        "Playlist import placement found candidates but none could be "
                        "safely resolved to a MusicBrainz album"
                    )
                else:
                    _log("No new playlist singleton rows needed album placement")
                state["phase"] = "match"
                state["current"] = f"matching after round {round_num}"
                _invalidate_lib_cache()
                time.sleep(1)
                matched_round, missing_round = _match_playlist_tracks(active_all_tracks, verify_acoustid=True)
                state["matched"] = matched_round
                state["matched_after_import"] = len(matched_round)
                state["missing_after_import"] = len(missing_round)
                state["missing"] = missing_round
                matched_keys = {_playlist_status_id(track) for track in matched_round}
                for track in previous_missing:
                    if _playlist_status_id(track) in matched_keys:
                        acoustid_st = _s(next(
                            (t.get("acoustid_status") for t in matched_round
                             if _playlist_status_id(t) == _playlist_status_id(track)), ""
                        ))
                        if acoustid_st == "mismatch":
                            _playlist_set_track_status(
                                state, track, "review_required",
                                message="imported but AcoustID fingerprint does not match expected track")
                        elif placement_summary.get("failed") and not placement_summary.get("placed"):
                            _playlist_set_track_status(
                                state,
                                track,
                                "review_required",
                                message="imported, but no MusicBrainz release-group match reached 70% confidence",
                            )
                        else:
                            _playlist_set_track_status(
                                state, track, "imported", message="imported and matched in Beets")
                for track in missing_round:
                    existing_status = _s(
                        (state.get("track_statuses", {}).get(_playlist_status_id(track)) or {}).get("status")
                        or ""
                    )
                    if existing_status == "review_required":
                        continue
                    _playlist_set_track_status(
                        state, track, "missing", message="still missing after import")
                _log(
                    f"After round {round_num}: {len(matched_round)} matched, "
                    f"{len(missing_round)} still missing"
                )

                if sync_after_import and matched_round:
                    state["phase"] = "sync"
                    state["current"] = f"syncing M3U + Plex after round {round_num}"
                    _log(f"Syncing M3U/Plex playlist after round {round_num} ({len(matched_round)} matched tracks)...")
                    _sync_round = [
                        t for t in matched_round
                        if _s((_playlist_state_for_track(name, t) or {}).get("status") or "") != "review_required"
                    ]
                    _create_playlist_outputs(
                        name, _sync_round, log=state["log"], replace_plex=True,
                        wait_for_plex_seconds=PLEX_SCAN_TIMEOUT,
                        desired_tracks=active_all_tracks,
                        missing_tracks=missing_round,
                        source=parse_source,
                        content=parse_content)
                    state["phase"] = "match"

                if not missing_round:
                    active_tracks = []
                    break

                missing_keys = {
                    _playlist_requested_track_key(track) for track in missing_round
                }
                if len(matched_round) <= previous_matched and missing_keys == previous_missing_keys:
                    _log(
                        "Import did not reduce the missing list; "
                        "stopping to avoid retrying the same unresolved tracks forever."
                    )
                    active_tracks = missing_round
                    break
                active_tracks = missing_round
                if pause_after_this_round:
                    _log(
                        "Stopping further download rounds until this round's "
                        "review-required file(s) are resolved."
                    )
                    break

            if active_tracks and round_num >= max_rounds:
                _log(
                    f"Reached PLAYLIST_DOWNLOAD_MAX_ROUNDS={max_rounds}; "
                    f"{len(active_tracks)} track(s) still missing."
                )
            elif active_tracks and state["done"] <= 0:
                review_total = _playlist_review_required_count_from_state(state)
                if review_total:
                    _log(
                        f"{review_total} downloaded file(s) are held for review; "
                        "syncing existing library matches only"
                    )
                else:
                    _log("No missing tracks were downloaded; syncing existing library matches only")

            if sync_after_import:
                state["phase"] = "sync"
                state["current"] = "sync playlist"
                _invalidate_lib_cache()
                time.sleep(1)
                matched, missing = _match_playlist_tracks(active_all_tracks, verify_acoustid=True)
                state["matched"] = matched
                state["matched_after_import"] = len(matched)
                state["missing_after_import"] = len(missing)
                state["missing"] = missing
                for track in matched:
                    existing_status = _s(
                        (state.get("track_statuses", {}).get(_playlist_status_id(track)) or {}).get("status")
                        or ""
                    )
                    acoustid_st = _s(track.get("acoustid_status") or "")
                    if acoustid_st == "mismatch" and existing_status not in {"review_required"}:
                        _playlist_set_track_status(
                            state, track, "review_required",
                            message="AcoustID fingerprint does not match expected track",
                            path=_s(track.get("path") or ""))
                    elif existing_status not in {"imported", "review_required"}:
                        _playlist_set_track_status(
                            state, track, "available", message="matched in Beets",
                            path=_s(track.get("path") or ""))
                for track in missing:
                    existing_status = _s(
                        (state.get("track_statuses", {}).get(_playlist_status_id(track)) or {}).get("status")
                        or ""
                    )
                    if existing_status == "review_required":
                        continue
                    _playlist_set_track_status(
                        state, track, "missing", message="not available in Beets")
                review_total = _playlist_review_required_count_from_state(state)
                state["review_required"] = review_total
                _log(f"Final Beets match: {len(matched)} matched, {len(missing)} still missing")
                if not matched and not review_total:
                    raise RuntimeError("No playlist tracks matched the Beets library after import")
                if not matched and review_total:
                    _log(
                        f"No playlist tracks matched Beets yet; {review_total} downloaded file(s) "
                        "are waiting for manual review."
                    )
                wait_for_plex = PLEX_SCAN_TIMEOUT
                sync_matched = [
                    track for track in matched
                    if _s((_playlist_state_for_track(name, track) or {}).get("status") or "") != "review_required"
                ]
                state["playlist"] = _create_playlist_outputs(
                    name, sync_matched, log=state["log"], replace_plex=True,
                    wait_for_plex_seconds=wait_for_plex,
                    desired_tracks=active_all_tracks,
                    missing_tracks=missing,
                    source=parse_source,
                    content=parse_content)
                plex_state = (state["playlist"].get("plex") or {}) if isinstance(state.get("playlist"), dict) else {}
                if plex_state.get("error"):
                    _log("Playlist files saved; Plex sync failed")
                elif plex_state.get("status") == "partial":
                    _log("Playlist files saved; Plex sync partial")
                else:
                    _log("Playlist sync complete")

            state["status"] = "done"
            state["phase"] = "done"
            state["current"] = ""
            _playlist_save_job_state(state)
            _playlist_record_pipeline(name, status="done", action=pipeline_action)
            return {
                "playlist_job_id": jid,
                "playlist": name,
                "matched_after_import": state.get("matched_after_import", 0),
                "missing_after_import": state.get("missing_after_import", 0),
                "done": state.get("done", 0),
                "failed": state.get("failed", 0),
                "review_required": _playlist_review_required_count_from_state(state),
            }
        except Exception as exc:
            _log(f"Fatal: {exc}")
            state["status"] = "error"
            state["phase"] = "error"
            _playlist_save_job_state(state)
            _playlist_record_pipeline(
                name, status="failed", action=pipeline_action, error=str(exc))
            raise
        finally:
            contract.close()
            runtime_lock.release()

    job = jobs.start_python(
        _run,
        label=f"Playlist Download & Sync: {name}",
        metadata={
            "type": "playlist-download",
            "mutating": True,
            "playlist_job_id": jid,
            "name": name,
            "source": parse_source,
            "track_count": len(all_tracks or tracks or []),
            "missing_count": len(tracks or []),
        },
    )
    state["jobs_job_id"] = job.job_id
    _playlist_save_job_state(state)
    return {"ok": True, "job_id": jid, "jobs_job_id": job.job_id}, 200


def _playlist_quality_row_payload(row: sqlite3.Row) -> Dict[str, Any]:
    path_text = _s(row["path"])
    item = type("PlaylistQualityRow", (), {})()
    row_keys = set(row.keys())
    for field in (
        "id", "title", "artist", "album", "albumartist", "length", "bitrate",
        "format", "path", "mb_trackid", "mb_albumid", "track", "disc", "year",
        "added",
    ):
        try:
            setattr(item, field, row[field] if field in row_keys else "")
        except Exception:
            setattr(item, field, "")
    payload = {
        "id": int(row["id"] or 0),
        "title": _s(row["title"]),
        "artist": _s(row["artist"]),
        "album": _s(row["album"]),
        "albumartist": _s(row["albumartist"]),
        "path": path_text,
    }
    for field in ("mb_trackid", "mb_albumid"):
        if field in row_keys:
            payload[field] = _s(row[field]).strip()
    for field in ("track", "disc", "year"):
        if field in row_keys:
            payload[field] = _playlist_int(row[field], 0)
    if "added" in row_keys:
        try:
            payload["added"] = float(row["added"] or 0)
        except Exception:
            payload["added"] = 0.0
    payload.update(_playlist_quality_for_item(item, path_text))
    album_item_count = 0
    if "album_item_count" in row_keys:
        album_item_count = _playlist_int(row["album_item_count"], 0)
        payload["album_item_count"] = album_item_count
    flags = set(payload.get("quality_flags") or [])
    if (
        album_item_count > 0
        and album_item_count <= 2
        and _playlist_title_score(payload.get("album"), payload.get("title")) >= 0.96
    ):
        flags.add("track_title_album")
        payload["quality_flags"] = sorted(flags)
        if payload.get("quality") == "ok":
            payload["quality"] = "review"
    if "preview_risk" in flags:
        payload["recommended_action"] = "delete_preview"
        payload["repairable"] = False
    elif flags:
        payload["recommended_action"] = "repair"
        payload["repairable"] = bool(payload.get("artist") and payload.get("title"))
    else:
        payload["recommended_action"] = "keep"
        payload["repairable"] = False
    return payload


def _playlist_quality_cleanup_candidates(limit: int = 200,
                                         item_ids: Optional[List[int]] = None,
                                         filter_mode: str = "all") -> List[Dict[str, Any]]:
    try:
        res = composite_workflows.get_playlist_quality_candidates(filter_mode=filter_mode, limit=limit, item_ids=item_ids)
    except Exception as ex:
        raise PlaylistQualityCandidatesUnavailableError(
            f"Engine quality candidates query failed: {ex}"
        ) from ex
    if isinstance(res, dict) and res.get("ok"):
        return res.get("candidates") or []
    reason = (res.get("error") if isinstance(res, dict) else None) or "engine did not confirm the query"
    raise PlaylistQualityCandidatesUnavailableError(f"Engine quality candidates query failed: {reason}")


def _playlist_repair_metadata(candidate: Dict[str, Any]) -> Dict[str, str]:
    artist = _s(candidate.get("query_artist") or "").strip()
    title = _s(candidate.get("query_title") or "").strip()
    if not title:
        title = _s(candidate.get("title") or "").strip()
    if not artist:
        artist = _s(candidate.get("artist") or "").strip()
    split = _playlist_split_artist_title(title)
    if split and candidate.get("source") in {
        "SoundCloud", "Legacy playlist import", "Playlist import", "Playlist singleton",
    }:
        artist, title = split
    album = _s(candidate.get("album") or "").strip()
    if _playlist_album_value_is_bad_fallback(album):
        album = ""
    return {"artist": artist or "Unknown Artist", "title": title or "Untitled", "album": album}


def _playlist_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(_s(value).strip()))
    except Exception:
        return default


def _playlist_duration_seconds(value: Any) -> float:
    text = _s(value).strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except Exception:
        pass
    parts = text.split(":")
    try:
        total = 0
        for part in parts:
            total = total * 60 + int(float(part.strip()))
        return float(total)
    except Exception:
        return 0.0


def _playlist_canonical_placement_evidence(
    local_track_probe: Dict[str, Any],
    target_track: Dict[str, Any],
    mb_releasegroupid: str,
    *,
    acoustid_hits: Optional[List[Dict[str, Any]]] = None,
):
    """Evaluate whether one playlist track may be auto-placed under one
    candidate MusicBrainz recording, using the canonical ARCH-002 evidence
    engine (`backend.matching.evaluate_release_group_candidate`) instead of
    a bare text/MB-search confidence threshold.

    ARCH-002 finding: the previous `_playlist_auto_placement_allowed()`
    authorized a real, unattended tag-write + file-move (via the engine's
    `/playlists/place-imported`) whenever a weighted text confidence score
    crossed 0.70, with no identity-evidence requirement at all -- exactly
    the anti-pattern ARCH-002 exists to eliminate, on a genuinely
    destructive path (`beet write` + `beet move` against the real library).

    This treats the local file being placed and the one specific target
    recording as a single-track "album" for the canonical evaluator:
    passing only that one target track (never the candidate release's
    whole tracklist) makes `complete_alignment` mean exactly what this
    workflow can honestly claim -- "this one recording is confirmed" -- not
    a false claim of full-album coverage a single-track placement has no
    way to prove. `local_track_probe` should carry `acoustid_hits` when a
    fingerprint lookup was performed, so the canonical AcoustID states
    (confirmed/conflict/no_result/unavailable/ambiguous) drive the result
    instead of an ad hoc `_source == "acoustid"` proxy.

    Returns the full `ReleaseGroupMatchResult` so callers can log/report
    the real reason (conflict / insufficient evidence / confirmed) instead
    of a bare bool, per ARCH-002 Part 19.
    """
    probe = dict(local_track_probe or {})
    probe["acoustid_hits"] = list(acoustid_hits or probe.get("acoustid_hits") or [])
    candidate = {
        "release_group_id": _s(mb_releasegroupid).strip().lower(),
        "tracks": [target_track] if target_track else [],
    }
    return evaluate_release_group_candidate(
        {},
        candidate,
        local_tracks=[probe],
        trust_model="existing_library",
    )


def _playlist_log(log: Optional[List[str]], message: str) -> None:
    if log is not None:
        log.append(message)


def _playlist_artist_credit_text(credits: Any) -> str:
    return _playlist_artist_credit_info(credits).get("albumartist", "")


def _playlist_albumartist_info_from_values(albumartist: str,
                                           mb_albumartistid: str = "",
                                           mb_albumartistids: str = "") -> Dict[str, str]:
    artist = _normalize_albumartist(_s(albumartist).strip())
    ids = [
        _s(value).strip().lower()
        for value in re.split(r"[;,]", _s(mb_albumartistids or mb_albumartistid))
        if _MB_UUID_RE.match(_s(value).strip().lower())
    ]
    if not ids and _MB_UUID_RE.match(_s(mb_albumartistid).strip().lower()):
        ids.append(_s(mb_albumartistid).strip().lower())
    if artist.casefold() == "various artists" and _MB_VARIOUS_ARTISTS_ID not in ids:
        ids.insert(0, _MB_VARIOUS_ARTISTS_ID)
    return {
        "albumartist": artist,
        "mb_albumartistid": ids[0] if ids else "",
        "mb_albumartistids": "; ".join(dict.fromkeys(ids)),
    }


def _playlist_release_group_albumartist_info(mb_releasegroupid: str,
                                             log: Optional[List[str]] = None) -> Dict[str, str]:
    rgid = _s(mb_releasegroupid).strip().lower()
    if not _MB_UUID_RE.match(rgid):
        return {}
    try:
        url = f"https://musicbrainz.org/ws/2/release-group/{rgid}?inc=artist-credits&fmt=json"
        req = _ur.Request(
            url,
            headers={"User-Agent": "BeetsWebControl/1.0 (beets-webcontrol)"},
        )
        with provider_boundary.opened("musicbrainz", req, timeout=20) as resp:
            data = json.loads(resp.read())
        info = _playlist_artist_credit_info(data.get("artist-credit") or [])
        if info.get("albumartist"):
            return info
        _playlist_log(log, f"  [playlist-place] Missing album artist for release group ID: {rgid}")
    except Exception as ex:
        _playlist_log(log, f"  [playlist-place] MusicBrainz lookup failed for release group {rgid}: {ex}")
    return {}


def _playlist_release_group_albumartist(mb_releasegroupid: str,
                                        log: Optional[List[str]] = None) -> str:
    return _playlist_release_group_albumartist_info(
        mb_releasegroupid, log=log).get("albumartist", "")


def _playlist_resolve_albumartist_info_for_release_group(
        mb_releasegroupid: str,
        mb_release: Optional[Dict[str, Any]] = None,
        fallback_release_artist: str = "",
        *,
        year: str = "",
        track_count: int = 0,
        log: Optional[List[str]] = None) -> Dict[str, str]:
    rgid = _s(mb_releasegroupid).strip().lower()
    if not _MB_UUID_RE.match(rgid):
        return _playlist_albumartist_info_from_values(fallback_release_artist)
    _playlist_log(log, f"  [playlist-place] Found release group ID: {rgid}")
    mb_release = mb_release or {}
    info = _playlist_albumartist_info_from_values(
        mb_release.get("release_artist") or fallback_release_artist,
        mb_release.get("release_artist_id") or mb_release.get("mb_albumartistid") or "",
        mb_release.get("release_artistids") or mb_release.get("mb_albumartistids") or "",
    )
    if info.get("albumartist") and info.get("mb_albumartistid"):
        _playlist_log(log, f"  [playlist-place] Resolved album artist: {info.get('albumartist')}")
        return info

    info = _playlist_release_group_albumartist_info(rgid, log=log)
    if info.get("albumartist") and info.get("mb_albumartistid"):
        _playlist_log(log, f"  [playlist-place] Resolved album artist: {info.get('albumartist')}")
        return info

    release_id = _resolve_release_group_to_release(
        rgid,
        log if log is not None else [],
        year=year,
        track_count=track_count,
    )
    if _MB_UUID_RE.match(_s(release_id).strip().lower()):
        mb = _fetch_mb_release_tracklist(release_id, log)
        info = _playlist_albumartist_info_from_values(
            mb.get("release_artist") or "",
            mb.get("release_artist_id") or "",
            mb.get("release_artistids") or "",
        )
        if info.get("albumartist") and info.get("mb_albumartistid"):
            _playlist_log(log, f"  [playlist-place] Resolved album artist: {info.get('albumartist')}")
            return info

    _playlist_log(log, f"  [playlist-place] Missing album artist for release group ID: {rgid}")
    return info if info.get("albumartist") else {}


def _playlist_resolve_albumartist_for_release_group(mb_releasegroupid: str,
                                                    mb_release: Optional[Dict[str, Any]] = None,
                                                    fallback_release_artist: str = "",
                                                    *,
                                                    year: str = "",
                                                    track_count: int = 0,
                                                    log: Optional[List[str]] = None) -> str:
    info = _playlist_resolve_albumartist_info_for_release_group(
        mb_releasegroupid,
        mb_release=mb_release,
        fallback_release_artist=fallback_release_artist,
        year=year,
        track_count=track_count,
        log=log,
    )
    return _s(info.get("albumartist") or "")


def _playlist_expected_album_path_hint(placement: Dict[str, Any]) -> str:
    artist = _safe_artist_folder_name(_normalize_albumartist(_s(placement.get("albumartist") or "")))
    mb_albumartistid = _s(placement.get("mb_albumartistid") or "").strip().lower()
    if artist and _MB_UUID_RE.match(mb_albumartistid):
        artist = f"{artist} {{{mb_albumartistid}}}"
    album = _safe_path_component(placement.get("album") or "Unknown Album", "Unknown Album")
    year = _playlist_int(placement.get("year"), 0)
    rgid = _s(placement.get("mb_releasegroupid") or "").strip().lower()
    album_folder = f"{album} ({year:04d})" if year > 0 else f"{album} ()"
    if _MB_UUID_RE.match(rgid):
        album_folder += f" {{{rgid}}}"
    return str(MUSIC_ROOT / artist / album_folder / "<track file>")


def _playlist_validate_final_album_path(path_text: str,
                                        placement: Dict[str, Any],
                                        log: Optional[List[str]] = None) -> Dict[str, Any]:
    albumartist = _normalize_albumartist(_s(placement.get("albumartist") or "").strip())
    album = _s(placement.get("album") or "").strip()
    rgid = _s(placement.get("mb_releasegroupid") or "").strip().lower()
    mb_albumartistids = [
        _s(value).strip().lower()
        for value in re.split(
            r"[;,]",
            _s(placement.get("mb_albumartistids") or placement.get("mb_albumartistid") or ""),
        )
        if _MB_UUID_RE.match(_s(value).strip().lower())
    ]
    expected = _playlist_expected_album_path_hint(placement)
    if not albumartist:
        return {
            "ok": False,
            "reason": f"missing album artist for release group ID {rgid or '(unknown)'}",
            "expected_path": expected,
        }
    if not mb_albumartistids:
        return {
            "ok": False,
            "reason": "missing MusicBrainz album artist ID",
            "expected_path": expected,
        }
    if not _MB_UUID_RE.match(rgid):
        return {
            "ok": False,
            "reason": "missing MusicBrainz release group ID",
            "expected_path": expected,
        }

    final_path = _playlist_resolve_item_path(path_text)
    try:
        rel = final_path.resolve(strict=False).relative_to(MUSIC_ROOT.resolve(strict=False))
    except Exception:
        return {
            "ok": False,
            "reason": "final path is outside the Beets music root",
            "final_path": str(final_path),
            "expected_path": expected,
        }
    parts = rel.parts
    if len(parts) < 3:
        return {
            "ok": False,
            "reason": "artist folder was missing",
            "final_path": str(final_path),
            "expected_path": expected,
        }

    artist_folder, album_folder = parts[0], parts[1]
    actual_artist_ids = set(re.findall(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        artist_folder.lower(),
    ))
    if not actual_artist_ids.intersection(mb_albumartistids):
        return {
            "ok": False,
            "reason": "artist folder did not include the MusicBrainz album artist ID",
            "final_path": str(final_path),
            "expected_path": expected,
        }
    artist_folder_name = re.sub(
        r"\s*[\{\(\[]?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}[\}\)\]]?",
        "",
        artist_folder,
        flags=re.I,
    ).strip()
    expected_artist_key = _artist_folder_merge_key(albumartist)
    actual_artist_key = _artist_folder_merge_key(artist_folder_name)
    if expected_artist_key and actual_artist_key != expected_artist_key:
        return {
            "ok": False,
            "reason": (
                f"artist folder was {artist_folder!r}, expected album artist "
                f"{albumartist!r}"
            ),
            "final_path": str(final_path),
            "expected_path": expected,
        }

    album_key = _album_track_norm(album)
    first_part_key = _album_track_norm(artist_folder)
    if album_key and album_key == first_part_key:
        return {
            "ok": False,
            "reason": "album folder was written directly under the music root",
            "final_path": str(final_path),
            "expected_path": expected,
        }
    if album_key and album_key not in _album_track_norm(album_folder):
        return {
            "ok": False,
            "reason": f"album folder {album_folder!r} did not contain album {album!r}",
            "final_path": str(final_path),
            "expected_path": expected,
        }
    if rgid not in album_folder.lower():
        return {
            "ok": False,
            "reason": "album folder did not include the MusicBrainz release group ID",
            "final_path": str(final_path),
            "expected_path": expected,
        }

    _playlist_log(log, f"  [playlist-place] Final library path: {final_path}")
    return {
        "ok": True,
        "final_path": str(final_path),
        "expected_path": expected,
    }


def _playlist_row_id(row: Any) -> int:
    if row is None:
        return 0
    try:
        if hasattr(row, "keys") and "id" in row.keys():
            return int(row["id"] or 0)
    except Exception:
        pass
    try:
        return int(row[0] or 0)
    except Exception:
        return 0


def _playlist_recording_search_candidates(title: str, artist: str,
                                          current_mbid: str = "") -> List[Dict[str, Any]]:
    clean_title, clean_artist = _clean_for_mb(_s(title).strip(), _s(artist).strip())
    searches = [(clean_title, clean_artist)]
    if clean_artist:
        searches.append((clean_title, ""))
    out: List[Dict[str, Any]] = []
    seen: set = set()
    cur = _s(current_mbid).strip().lower()
    if _MB_UUID_RE.match(cur):
        out.append({
            "mb_trackid": cur,
            "title": clean_title,
            "artist": clean_artist,
            "score": 100,
            "_source": "existing-mbid",
        })
        seen.add(cur)
    for q_title, q_artist in searches:
        if not q_title:
            continue
        for cand in _mb_recording_search(q_title, q_artist, limit=8) or []:
            mbid = _s(cand.get("mb_trackid") or "").strip().lower()
            if not _MB_UUID_RE.match(mbid) or mbid in seen:
                continue
            seen.add(mbid)
            cand["_source"] = "mb-recording-search"
            out.append(cand)
    return out


def _playlist_release_looks_like_track_single(album: str,
                                              title: str,
                                              mb_tracks: List[Dict[str, Any]],
                                              mb_release: Dict[str, Any]) -> bool:
    album_title = _s(album or "").strip()
    track_title = _s(title or "").strip()
    if not album_title or not track_title:
        return False
    primary_type = _s(mb_release.get("release_group_primary_type") or "").strip().casefold()
    track_count = len(mb_tracks or [])
    title_score = _playlist_title_score(album_title, track_title)
    if primary_type == "single" and title_score >= 0.92:
        return True
    return 0 < track_count <= 2 and title_score >= 0.96


def _playlist_album_tag_release_placement(candidate: Dict[str, Any],
                                          metadata: Dict[str, str],
                                          clean_title: str,
                                          clean_artist: str,
                                          path_text: str,
                                          length: float,
                                          min_title: float,
                                          min_artist: float,
                                          log: Optional[List[str]] = None) -> Dict[str, Any]:
    album_hint = _s(metadata.get("album") or candidate.get("album") or "").strip()
    if not album_hint:
        return {}
    if _playlist_album_value_is_bad_fallback(album_hint):
        _playlist_log(
            log,
            "  [playlist-place] Rejected metadata: provider name was incorrectly used as album "
            f"({album_hint})",
        )
        return {}
    if _playlist_title_score(album_hint, clean_title) >= 0.92:
        _playlist_log(
            log,
            f"  [playlist-place] Ignoring album hint {album_hint!r}: "
            "it looks like the track title",
        )
        return {}

    year_hint = _s(candidate.get("year") or "").strip()
    if year_hint in {"0", "0.0"}:
        year_hint = ""
    searches = [(album_hint, clean_artist)]
    if clean_artist:
        searches.append((album_hint, ""))
    seen_releases: set = set()
    for album_query, artist_query in searches:
        for rel in _mb_release_search(
            album_query,
            artist_query,
            limit=8,
            year=year_hint[:4] if year_hint else "",
            log=log,
        ) or []:
            mb_albumid = _s(rel.get("mb_albumid") or "").strip().lower()
            if not _MB_UUID_RE.match(mb_albumid) or mb_albumid in seen_releases:
                continue
            seen_releases.add(mb_albumid)
            selected_releasegroupid = _s(rel.get("mb_releasegroupid") or "").strip().lower()
            if not _MB_UUID_RE.match(selected_releasegroupid):
                _playlist_log(log, f"  [playlist-place] Skip release {mb_albumid}: no release-group ID")
                continue
            rel_album = _s(rel.get("album") or "").strip()
            rel_artist = _s(rel.get("artist") or "").strip()
            album_score = _playlist_title_score(album_hint, rel_album)
            artist_score = (
                _playlist_artist_name_score(clean_artist, rel_artist)
                if clean_artist and rel_artist else 1.0
            )
            if album_score < 0.82 or artist_score < min_artist:
                _playlist_log(
                    log,
                    f"  [playlist-place] Skip album candidate {mb_albumid}: "
                    f"album {album_score:.0%}, artist {artist_score:.0%}",
                )
                continue
            resolved_releaseid = _resolve_release_group_to_release(
                selected_releasegroupid,
                log if log is not None else [],
                year=year_hint[:4] if year_hint else "",
            )
            if _MB_UUID_RE.match(resolved_releaseid):
                mb_albumid = resolved_releaseid
            mb = _fetch_mb_release_tracklist(mb_albumid, log)
            if _s(mb.get("release_group") or "").strip().lower() != selected_releasegroupid:
                _playlist_log(log, f"  [playlist-place] Skip release {mb_albumid}: release-group changed during resolution")
                continue
            mb_tracks = mb.get("tracks") or []
            if not mb_tracks:
                continue
            album = _s(mb.get("release_title") or rel_album or album_hint).strip()
            if _playlist_release_looks_like_track_single(album, clean_title, mb_tracks, mb):
                _playlist_log(
                    log,
                    f"  [playlist-place] Skip album candidate {mb_albumid}: "
                    f"release title {album!r} looks single-like",
                )
                continue
            item_probe = {
                "title": clean_title,
                "path": path_text,
                "length": length,
                "mb_trackid": _s(candidate.get("mb_trackid") or "").strip().lower(),
                "track": _playlist_int(candidate.get("track"), 0),
                "disc": _playlist_int(candidate.get("disc"), 1),
            }
            best = _best_album_track_match(item_probe, mb_tracks)
            best_track = best.get("track") or {}
            best_score = float(best.get("score") or 0.0)
            if not best.get("exact_mbid") and best_score < min_title:
                _playlist_log(
                    log,
                    f"  [playlist-place] Skip album candidate {mb_albumid}: "
                    f"track match {best_score:.0%}",
                )
                continue
            disc = _playlist_int(best_track.get("disc") or candidate.get("disc"), 1)
            tracktotal = len([
                t for t in mb_tracks if _playlist_int(t.get("disc"), 1) == disc
            ])
            artist = _s(metadata.get("artist") or candidate.get("artist") or clean_artist).strip()
            title = _s(best_track.get("title") or metadata.get("title") or clean_title).strip()
            mb_releasegroupid = _s(mb.get("release_group") or "").strip().lower()
            albumartist_info = _playlist_resolve_albumartist_info_for_release_group(
                mb_releasegroupid,
                mb_release=mb,
                fallback_release_artist=rel_artist,
                year=year_hint[:4] if year_hint else "",
                track_count=len(mb_tracks),
                log=log,
            )
            albumartist = _s(albumartist_info.get("albumartist") or "").strip()
            mb_albumartistid = _s(albumartist_info.get("mb_albumartistid") or "").strip().lower()
            if not album or not albumartist or not artist or not title:
                if not albumartist and _MB_UUID_RE.match(mb_releasegroupid):
                    _playlist_log(
                        log,
                        f"  [playlist-place] Review required: missing album artist for release group ID {mb_releasegroupid}",
                    )
                continue
            if not _MB_UUID_RE.match(mb_albumartistid):
                _playlist_log(
                    log,
                    f"  [playlist-place] Review required: missing album artist MusicBrainz ID for release group {mb_releasegroupid}",
                )
                continue
            confidence = (
                (album_score * 0.35)
                + (artist_score * 0.20)
                + (best_score * 0.45)
            )
            if not _MB_UUID_RE.match(mb_releasegroupid):
                _playlist_log(log, f"  [playlist-place] Skip release {mb_albumid}: no release-group ID")
                continue
            # ARCH-002: text/MB-search confidence is reported for logging
            # only -- it is not what authorizes this unattended write. A
            # real INSERT/UPDATE + `beet write`/`beet move` happens on the
            # engine side once this placement is accepted (see
            # backend/beets_control_agent.py's /playlists/place-imported),
            # so the same canonical evidence gate every other production
            # mutation flows through applies here too: no fingerprint
            # confirmation of this specific recording means no unattended
            # write, no matter how high the text score is.
            acoustid_hits = None
            try:
                audio_path_probe = _playlist_resolve_item_path(path_text)
                if audio_path_probe.exists():
                    acoustid_hits = _acoustid_lookup_cached(str(audio_path_probe))
            except Exception as ex:
                _playlist_log(log, f"  [playlist-place] AcoustID lookup skipped: {ex}")
            canonical = _playlist_canonical_placement_evidence(
                item_probe, best_track, mb_releasegroupid, acoustid_hits=acoustid_hits,
            )
            if not canonical.can_auto_accept():
                _playlist_log(
                    log,
                    f"  [playlist-place] Review required for release group {mb_releasegroupid}: "
                    f"confidence {confidence:.0%}, canonical state {canonical.state.value}"
                    + (f", conflicts {canonical.conflicts}" if canonical.conflicts else "")
                    + (f", missing evidence {canonical.missing_evidence}" if canonical.missing_evidence else ""),
                )
                continue
            _playlist_log(
                log,
                f"  [playlist-place] Album-tag release match: {artist} - {title} "
                f"-> {albumartist} - {album} (canonical state {canonical.state.value})",
            )
            return {
                "ok": True,
                "artist": artist,
                "albumartist": albumartist,
                "title": title,
                "album": album,
                "year": _playlist_int(_s(mb.get("date") or rel.get("year") or year_hint)[:4], 0),
                "track": _playlist_int(best_track.get("track") or candidate.get("track"), 0),
                "tracktotal": tracktotal,
                "disc": disc,
                "disctotal": max((_playlist_int(t.get("disc"), 1) for t in mb_tracks), default=1),
                "mb_trackid": _s(best_track.get("mb_trackid") or candidate.get("mb_trackid") or "").strip().lower(),
                "mb_albumid": mb_albumid,
                "mb_releasegroupid": mb_releasegroupid,
                "mb_artistid": "",
                "mb_albumartistid": mb_albumartistid,
                "mb_albumartistids": _s(albumartist_info.get("mb_albumartistids") or mb_albumartistid),
                "country": _s(mb.get("country") or rel.get("country") or "").strip(),
                "label": _s(rel.get("label") or "").strip(),
                "genre": "",
                "match": {
                    "source": "album-tag-release-search",
                    "confidence": round(confidence, 3),
                    "album_score": round(album_score, 3),
                    "artist_score": round(artist_score, 3),
                    "release_track_score": round(best_score, 3),
                    "canonical_state": canonical.state.value,
                    "canonical_positive_evidence": list(canonical.positive_evidence),
                },
            }
    return {}


def _playlist_resolve_album_placement(candidate: Dict[str, Any],
                                      metadata: Dict[str, str],
                                      log: Optional[List[str]] = None) -> Dict[str, Any]:
    query_title = _s(candidate.get("query_title") or metadata.get("title") or "").strip()
    query_artist = _s(candidate.get("query_artist") or metadata.get("artist") or "").strip()
    clean_title, clean_artist = _clean_for_mb(query_title, query_artist)
    if not clean_title:
        return {"ok": False, "reason": "missing title"}

    current_mbid = _s(candidate.get("mb_trackid") or "").strip().lower()
    length = float(candidate.get("length") or 0)
    path_text = _s(candidate.get("path") or "")
    min_title = float(os.environ.get("PLAYLIST_MB_REPAIR_TITLE_SCORE", "0.86") or "0.86")
    min_artist = float(os.environ.get("PLAYLIST_MB_REPAIR_ARTIST_SCORE", "0.68") or "0.68")
    min_mb_score = int(os.environ.get("PLAYLIST_MB_REPAIR_MIN_MB_SCORE", "70") or "70")

    album_tag_placement = _playlist_album_tag_release_placement(
        candidate, metadata, clean_title, clean_artist, path_text, length,
        min_title, min_artist, log)
    if album_tag_placement.get("ok"):
        return album_tag_placement

    recording_candidates: List[Dict[str, Any]] = []
    seen_recording_ids: set = set()

    try:
        audio_path = _playlist_resolve_item_path(path_text)
        if audio_path.exists():
            for cand in _acoustid_lookup_cached(str(audio_path)):
                mbid = _s(cand.get("mb_trackid") or "").strip().lower()
                if not _MB_UUID_RE.match(mbid) or mbid in seen_recording_ids:
                    continue
                next_cand = dict(cand)
                next_cand["_source"] = "acoustid"
                recording_candidates.append(next_cand)
                seen_recording_ids.add(mbid)
            if recording_candidates:
                _playlist_log(
                    log,
                    f"  [playlist-place] AcoustID supplied {len(recording_candidates)} candidate(s)",
                )
    except Exception as ex:
        _playlist_log(log, f"  [playlist-place] AcoustID lookup skipped: {ex}")

    for cand in _playlist_recording_search_candidates(clean_title, clean_artist, current_mbid):
        mbid = _s(cand.get("mb_trackid") or "").strip().lower()
        if not _MB_UUID_RE.match(mbid) or mbid in seen_recording_ids:
            continue
        recording_candidates.append(cand)
        seen_recording_ids.add(mbid)

    if length > 0 and recording_candidates:
        def _recording_rank(rec: Dict[str, Any]) -> Tuple[int, int, float, int]:
            rec_seconds = _playlist_duration_seconds(rec.get("duration"))
            if rec_seconds > 0:
                duration_unknown = 0
                duration_delta = abs(float(length) - rec_seconds)
            else:
                duration_unknown = 1
                duration_delta = 9999.0
            source_rank = 0 if _s(rec.get("_source") or rec.get("source")).lower() == "acoustid" else 1
            return (
                source_rank,
                duration_unknown,
                duration_delta,
                -int(rec.get("score") or 0),
            )

        recording_candidates.sort(key=_recording_rank)

    if not recording_candidates:
        _playlist_log(
            log,
            f"  [playlist-place] No MusicBrainz recording candidates for {clean_artist} - {clean_title}",
        )

    for rec in recording_candidates:
        mb_trackid = _s(rec.get("mb_trackid") or "").strip().lower()
        if not _MB_UUID_RE.match(mb_trackid):
            continue
        rec_title = _s(rec.get("title") or clean_title).strip()
        rec_artist = _s(rec.get("artist") or clean_artist).strip()
        title_score = _playlist_title_score(clean_title, rec_title)
        artist_score = (
            _playlist_artist_name_score(clean_artist, rec_artist)
            if clean_artist and rec_artist else 1.0
        )
        mb_score = int(rec.get("score") or 0)
        if title_score < min_title or artist_score < min_artist or mb_score < min_mb_score:
            _playlist_log(
                log,
                f"  [playlist-place] Skip MB recording {mb_trackid}: "
                f"title {title_score:.0%}, artist {artist_score:.0%}, MB score {mb_score}",
            )
            continue

        preferred_albumid = _s(
            candidate.get("mb_albumid")
            or rec.get("mb_albumid")
            or next(iter(rec.get("mb_albumids") or []), "")
        ).strip().lower()
        details = _fetch_mb_recording_details(
            mb_trackid,
            preferred_albumid if _MB_UUID_RE.match(preferred_albumid) else "",
        )
        if not details and _MB_UUID_RE.match(preferred_albumid):
            details = {}
        if _MB_UUID_RE.match(preferred_albumid) and not details.get("mb_albumid"):
            details["mb_albumid"] = preferred_albumid
        for fallback_key in ("artist", "album", "year", "track", "disc"):
            if not details.get(fallback_key) and candidate.get(fallback_key) not in (None, ""):
                details[fallback_key] = candidate.get(fallback_key)
        mb_albumid = _s(details.get("mb_albumid") or "").strip().lower()
        if not _MB_UUID_RE.match(mb_albumid):
            _playlist_log(
                log,
                f"  [playlist-place] Skip MB recording {mb_trackid}: no release match",
            )
            continue
        mb = _fetch_mb_release_tracklist(mb_albumid, log)
        mb_tracks = mb.get("tracks") or []
        if not mb_tracks:
            _playlist_log(
                log,
                f"  [playlist-place] Skip release {mb_albumid}: no tracklist",
            )
            continue
        selected_releasegroupid = _s(mb.get("release_group") or "").strip().lower()
        if not _MB_UUID_RE.match(selected_releasegroupid):
            _playlist_log(log, f"  [playlist-place] Skip release {mb_albumid}: no release-group ID")
            continue
        resolved_releaseid = _resolve_release_group_to_release(
            selected_releasegroupid,
            log if log is not None else [],
            year=_s(details.get("year") or "")[:4],
            track_count=len(mb_tracks),
        )
        if _MB_UUID_RE.match(resolved_releaseid) and resolved_releaseid != mb_albumid:
            resolved_mb = _fetch_mb_release_tracklist(resolved_releaseid, log)
            if _s(resolved_mb.get("release_group") or "").strip().lower() == selected_releasegroupid:
                mb_albumid = resolved_releaseid
                mb = resolved_mb
                mb_tracks = mb.get("tracks") or []
        item_probe = {
            "title": clean_title,
            "path": path_text,
            "length": length,
            "mb_trackid": mb_trackid,
            "track": _playlist_int(details.get("track") or candidate.get("track"), 0),
            "disc": _playlist_int(details.get("disc") or candidate.get("disc"), 1),
        }
        best = _best_album_track_match(item_probe, mb_tracks)
        best_track = best.get("track") or {}
        best_score = float(best.get("score") or 0.0)
        if not best.get("exact_mbid") and best_score < min_title:
            _playlist_log(
                log,
                f"  [playlist-place] Skip release {mb_albumid}: best track match {best_score:.0%}",
            )
            continue

        year = _playlist_int(details.get("year") or _s(mb.get("date") or "")[:4], 0)
        disctotal = max(
            _playlist_int(details.get("disctotal"), 0),
            max((_playlist_int(t.get("disc"), 1) for t in mb_tracks), default=1),
        )
        disc = _playlist_int(best_track.get("disc") or details.get("disc"), 1)
        tracktotal = max(
            _playlist_int(details.get("tracktotal"), 0),
            len([t for t in mb_tracks if _playlist_int(t.get("disc"), 1) == disc]),
        )
        album = _s(mb.get("release_title") or details.get("album") or metadata.get("album") or "").strip()
        mb_releasegroupid = _s(mb.get("release_group") or "").strip().lower()
        albumartist_info = _playlist_resolve_albumartist_info_for_release_group(
            mb_releasegroupid,
            mb_release=mb,
            fallback_release_artist=_s(details.get("albumartist") or ""),
            year=_s(details.get("year") or "")[:4],
            track_count=len(mb_tracks),
            log=log,
        )
        albumartist = _s(albumartist_info.get("albumartist") or "").strip()
        mb_albumartistid = _s(albumartist_info.get("mb_albumartistid") or "").strip().lower()
        artist = _s(details.get("artist") or rec_artist or metadata.get("artist") or clean_artist).strip()
        title = _s(best_track.get("title") or rec_title or metadata.get("title")).strip()
        if not album or not albumartist or not artist or not title:
            if not albumartist and _MB_UUID_RE.match(mb_releasegroupid):
                _playlist_log(
                    log,
                    f"  [playlist-place] Review required: missing album artist for release group ID {mb_releasegroupid}",
                )
            continue
        if not _MB_UUID_RE.match(mb_albumartistid):
            _playlist_log(
                log,
                f"  [playlist-place] Review required: missing album artist MusicBrainz ID for release group {mb_releasegroupid}",
            )
            continue
        if _playlist_release_looks_like_track_single(album, clean_title, mb_tracks, mb):
            _playlist_log(
                log,
                f"  [playlist-place] Skip release {mb_albumid}: "
                f"release title {album!r} matches track title and looks single-like",
            )
            continue
        confidence = (
            (min(1.0, max(0.0, mb_score / 100.0)) * 0.40)
            + (title_score * 0.20)
            + (artist_score * 0.15)
            + (best_score * 0.25)
        )
        if not _MB_UUID_RE.match(mb_releasegroupid):
            _playlist_log(log, f"  [playlist-place] Skip release {mb_albumid}: no release-group ID")
            continue
        # ARCH-002: as in _playlist_album_tag_release_placement above, the
        # text/MB-search confidence is reported for logging only. This loop
        # already tries AcoustID-sourced recording candidates first (see
        # `_recording_rank`'s `source_rank`), but previously never actually
        # required that evidence to authorize the write -- a plain MB-search
        # hit with a high enough blended score could still win. Feed every
        # AcoustID-sourced candidate actually gathered this call into the
        # canonical evaluator so real confirmed/conflict/ambiguous status
        # (not a bare "_source == acoustid" proxy) decides.
        acoustid_hits_for_probe = [
            rc for rc in recording_candidates
            if _s(rc.get("_source") or rc.get("source") or "").lower() == "acoustid"
        ]
        canonical = _playlist_canonical_placement_evidence(
            item_probe, best_track, mb_releasegroupid, acoustid_hits=acoustid_hits_for_probe,
        )
        if not canonical.can_auto_accept():
            _playlist_log(
                log,
                f"  [playlist-place] Review required for release group {mb_releasegroupid}: "
                f"confidence {confidence:.0%}, canonical state {canonical.state.value}"
                + (f", conflicts {canonical.conflicts}" if canonical.conflicts else "")
                + (f", missing evidence {canonical.missing_evidence}" if canonical.missing_evidence else ""),
            )
            continue
        return {
            "ok": True,
            "artist": artist,
            "albumartist": albumartist,
            "title": title,
            "album": album,
            "year": year,
            "track": _playlist_int(best_track.get("track") or details.get("track"), 0),
            "tracktotal": tracktotal,
            "disc": disc,
            "disctotal": disctotal,
            "mb_trackid": mb_trackid,
            "mb_albumid": mb_albumid,
            "mb_releasegroupid": mb_releasegroupid,
            "mb_artistid": _s(details.get("mb_artistid") or "").strip().lower(),
            "mb_albumartistid": mb_albumartistid,
            "mb_albumartistids": _s(albumartist_info.get("mb_albumartistids") or mb_albumartistid),
            "country": _s(mb.get("country") or "").strip(),
            "label": _s(details.get("label") or "").strip(),
            "genre": _s(details.get("genre") or "").strip(),
            "match": {
                "confidence": round(confidence, 3),
                "recording_score": mb_score,
                "title_score": round(title_score, 3),
                "artist_score": round(artist_score, 3),
                "release_track_score": round(best_score, 3),
                "canonical_state": canonical.state.value,
                "canonical_positive_evidence": list(canonical.positive_evidence),
            },
        }
    return {
        "ok": False,
        "reason": "review required: no MusicBrainz release-group match reached canonical auto-accept evidence",
        "review_required": True,
    }


def _playlist_reusable_download_files(track: Dict[str, Any],
                                      job_dir: Path,
                                      round_dir: Path,
                                      log,
                                      playlist_name: str = "",
                                      playlist_id: str = "") -> List[str]:
    artist = _s(track.get("artist") or "").strip()
    title = _s(track.get("title") or "").strip()
    if not title:
        return []
    clean_name = _clean_playlist_name(playlist_name or "Playlist")
    key = _playlist_resolve_operation_key("", playlist_id, clean_name, log=log)
    if not key:
        log("  warning: could not resolve playlist key; skipping staged-file reuse")
        return []
    candidates: List[str] = []
    try:
        res = composite_workflows.list_playlist_staged_files(key, playlist_id)
        if isinstance(res, dict) and res.get("ok"):
            candidates = [f["path"] for f in res.get("files", []) if isinstance(f, dict) and f.get("path")]
        else:
            log(f"  warning: engine staged-file listing failed for reuse check: {(res or {}).get('error') if isinstance(res, dict) else res}")
    except Exception as ex:
        # Engine ownership: do not fall back to reading the playlist staging
        # directory directly from web-manager. If the engine can't be
        # reached, there is nothing safe to reuse; the caller will
        # re-download instead of trusting an unverified local listing.
        log(f"  warning: engine staged-file listing IPC failed for reuse check: {ex}")
        return []

    for path_value in candidates:
        val_res = _playlist_validate_staged_download(
            path_value, artist, title, _s(track.get("mb_trackid") or ""),
            playlist_name=playlist_name, playlist_id=playlist_id)
        match = val_res.get("match") or {}
        if not match.get("ok") or not val_res.get("audio_allowed", True):
            continue
        _playlist_stamp_download_tags(path_value, artist, title, log)
        log(
            "  reusing fingerprint-verified staged file "
            f"({_playlist_identity_log(match)}): {Path(path_value).name}"
        )
        return [path_value]
    return []


def _playlist_copy_source_files(files: Iterable[Path], dest_dir: Path,
                                artist: str, title: str, log) -> List[str]:
    copied: List[str] = []
    base = _playlist_safe_filename(" - ".join(part for part in [artist, title] if part), "track")
    for idx, src in enumerate(files, start=1):
        source = Path(src)
        suffix = source.suffix or ".mp3"
        dest = dest_dir / f"{base}{suffix}"
        if dest.exists():
            dest = dest_dir / f"{base} ({idx}){suffix}"
        try:
            if source.resolve(strict=False) == dest.resolve(strict=False):
                copied.append(str(dest))
                continue
        except Exception:
            pass
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(source), str(dest))
            copied.append(str(dest))
        except Exception as ex:
            # A failed copy must NOT be reported as a successful one. Do not
            # append the (uncopied) source path here -- it lives outside
            # dest_dir/playlist staging, and downstream validation treats
            # every entry in `copied` as if it were already staged for this
            # playlist. Silently substituting the source path previously let
            # an unstaged, unscoped file be validated/imported as if it were
            # a legitimate staged download.
            log(f"  warning: could not copy downloaded file {source.name} into playlist staging: {ex}")
    return copied


def _playlist_slskd_download_track(artist: str, title: str,
                                   dl_dir: Path, log,
                                   raw_log: Optional[List[str]] = None) -> List[str]:
    if not SLSKD_API_KEY:
        raise RuntimeError("SLSKD API key is not configured")
    wanted = [{"title": title}]
    inner_log: List[str] = raw_log if raw_log is not None else []
    try:
        username, queued, expected, _remote_dir = _slskd_search_and_queue(
            artist, title, "", inner_log, track_count=0, wanted_tracks=wanted,
            busy_retries=int(os.environ.get("PLAYLIST_SLSKD_BUSY_RETRIES", "3") or "3"))
        aldir, transfer_hints = _slskd_wait_downloads(
            username, queued, inner_log,
            timeout=int(os.environ.get("PLAYLIST_SLSKD_TIMEOUT", "90") or "90")
        )
        aldir, afiles = _find_slskd_downloaded_files(
            username, queued, expected or aldir, inner_log,
            artist=artist, album=title, track_count=max(1, len(queued)),
            transfer_hints=transfer_hints, wanted_tracks=wanted)
    finally:
        if raw_log is None:
            for line in inner_log:
                log(line)
    if not afiles:
        raise RuntimeError("SLSKD completed but no queued playlist file was found")
    log(f"  [slskd] Copying {len(afiles)} downloaded file(s) into playlist import staging")
    return _playlist_copy_source_files(afiles, dl_dir, artist, title, log)


def _playlist_validate_staged_download(path_value: str, artist: str, title: str,
                                       expected_mb_trackid: str = "",
                                       playlist_name: str = "",
                                       playlist_id: str = "") -> Dict[str, Any]:
    clean_name = _clean_playlist_name(playlist_name or "Playlist")
    key = _playlist_resolve_operation_key("", playlist_id, clean_name)
    if not key:
        return {
            "ok": False,
            "audio_allowed": False,
            "identity": {"identity_status": "review_required", "final_action": "review"},
            "match": {"ok": False, "review_required": True, "identity_status": "review_required"},
            "size": 0,
            "path": path_value,
            "reason": "could not resolve the playlist for this download",
        }
    try:
        res = composite_workflows.validate_playlist_staged_track(
            playlist_key=key,
            requested_path=path_value,
            artist=artist,
            title=title,
            expected_mb_trackid=expected_mb_trackid,
            preferences=_music_format_preferences(),
        )
        if isinstance(res, dict) and res.get("ok"):
            return res
        reason = (res.get("error") if isinstance(res, dict) else None) or "engine validation failed"
    except Exception as ex:
        _app_logger.warning("Engine validate staged track IPC failed: %s", ex)
        reason = f"engine validation IPC failed: {ex}"

    # Engine ownership: staging/validation lives in the engine container.
    # Do not fingerprint or inspect the file locally on IPC failure or a
    # non-ok engine response -- fail closed to review rather than trusting
    # an unverified local read of what should be an engine-owned path.
    return {
        "ok": False,
        "audio_allowed": False,
        "identity": {"identity_status": "review_required", "final_action": "review"},
        "match": {"ok": False, "review_required": True, "identity_status": "review_required"},
        "size": 0,
        "path": path_value,
        "reason": reason,
    }


def _playlist_apply_album_placement(con, candidate: Dict[str, Any],
                                    placement: Dict[str, Any],
                                    log: Optional[List[str]] = None,
                                    cancel_event=None) -> Dict[str, Any]:
    item_id = int(candidate.get("id") or 0)
    if item_id <= 0:
        return {"id": item_id, "repaired": False, "reason": "missing item id"}

    artist = _s(candidate.get("artist") or candidate.get("query_artist") or "")
    clean_name = _clean_playlist_name(artist or "Playlist")
    key = _playlist_resolve_operation_key(
        candidate.get("playlist_key"), candidate.get("playlist_id"), clean_name, log=log)
    if not key:
        return {
            "id": item_id,
            "repaired": False,
            "reason": "could not resolve the playlist for this item; refusing to place it",
        }

    # Wave 13 Engine Ownership: Placement mutations and post-import validation
    # are executed in the engine container via BeetsClient IPC.
    try:
        res = composite_workflows.place_playlist_imported_item(
            playlist_key=key,
            item_id=item_id,
            placement=placement,
            action="repair",
        )
        if isinstance(res, dict) and res.get("ok"):
            _playlist_log(log, f"  [playlist-place] Engine placed item {item_id}")
            return {
                "id": item_id,
                "repaired": bool(res.get("repaired", True)),
                "old_path": res.get("old_path", candidate.get("path", "")),
                "new_path": res.get("new_path", ""),
                "artist": placement.get("artist", ""),
                "title": placement.get("title", ""),
                "album": placement.get("album", ""),
                "albumartist": placement.get("albumartist", ""),
                "match": placement.get("match") or {},
            }
        return {
            "id": item_id,
            "repaired": False,
            "reason": (res.get("error") if isinstance(res, dict) else None) or "Engine placement failed",
        }
    except Exception as ex:
        return {"id": item_id, "repaired": False, "reason": f"Engine placement IPC failed: {ex}"}


def _playlist_repair_quality_candidate(con, candidate: Dict[str, Any],
                                       log: Optional[List[str]] = None,
                                       cancel_event=None) -> Dict[str, Any]:
    if candidate.get("recommended_action") != "repair":
        return {"id": candidate.get("id"), "repaired": False, "reason": "not repairable"}
    metadata = _playlist_repair_metadata(candidate)
    placement = _playlist_resolve_album_placement(candidate, metadata, log)
    if not placement.get("ok"):
        return {
            "id": int(candidate.get("id") or 0),
            "repaired": False,
            "reason": placement.get("reason") or "MusicBrainz placement failed",
            **metadata,
        }
    return _playlist_apply_album_placement(
        con, candidate, placement, log=log, cancel_event=cancel_event)


def _playlist_manual_placement_from_payload(candidate: Dict[str, Any],
                                            payload: Dict[str, Any]) -> Dict[str, Any]:
    placement_raw = payload.get("placement") if isinstance(payload.get("placement"), dict) else payload
    artist = _playlist_clean_video_text(
        placement_raw.get("artist")
        or candidate.get("artist")
        or candidate.get("query_artist")
        or ""
    )
    title = _playlist_clean_video_text(
        placement_raw.get("title")
        or candidate.get("title")
        or candidate.get("query_title")
        or ""
    )
    album = _playlist_clean_video_text(placement_raw.get("album") or "")
    albumartist = _playlist_clean_video_text(
        placement_raw.get("albumartist")
        or placement_raw.get("album_artist")
        or artist
    )
    if not artist or not title or not album or not albumartist:
        missing = [
            name for name, value in (
                ("artist", artist),
                ("title", title),
                ("album", album),
                ("albumartist", albumartist),
            ) if not value
        ]
        return {"ok": False, "reason": "Missing required field(s): " + ", ".join(missing)}

    year = _playlist_int(placement_raw.get("year") or candidate.get("year"), 0)
    disc = max(1, _playlist_int(placement_raw.get("disc") or candidate.get("disc"), 1))
    track = max(0, _playlist_int(placement_raw.get("track") or candidate.get("track"), 0))
    tracktotal = max(0, _playlist_int(placement_raw.get("tracktotal"), 0))
    disctotal = max(1, _playlist_int(placement_raw.get("disctotal"), 1))
    mb_trackid = _s(placement_raw.get("mb_trackid") or "").strip().lower()
    mb_albumid = _s(placement_raw.get("mb_albumid") or "").strip().lower()
    mb_releasegroupid = _s(placement_raw.get("mb_releasegroupid") or "").strip().lower()
    mb_artistid = _s(placement_raw.get("mb_artistid") or "").strip().lower()
    mb_albumartistid = _s(placement_raw.get("mb_albumartistid") or "").strip().lower()
    mb_albumartistids = _s(placement_raw.get("mb_albumartistids") or mb_albumartistid).strip()

    for label, value in (
        ("mb_trackid", mb_trackid),
        ("mb_albumid", mb_albumid),
        ("mb_releasegroupid", mb_releasegroupid),
        ("mb_artistid", mb_artistid),
        ("mb_albumartistid", mb_albumartistid),
    ):
        if value and not _MB_UUID_RE.match(value):
            return {"ok": False, "reason": f"{label} must be a MusicBrainz UUID"}

    if not mb_releasegroupid and _MB_UUID_RE.match(mb_albumid):
        try:
            release = _fetch_mb_release_tracklist(mb_albumid, None)
            mb_releasegroupid = _s(release.get("release_group") or "").strip().lower()
        except Exception:
            mb_releasegroupid = ""
    if not _MB_UUID_RE.match(mb_releasegroupid):
        return {
            "ok": False,
            "reason": "A MusicBrainz release group ID is required for playlist album placement",
        }
    if not _MB_UUID_RE.match(mb_albumartistid):
        return {
            "ok": False,
            "reason": "A MusicBrainz album artist ID is required for playlist album placement",
        }

    return {
        "ok": True,
        "artist": artist,
        "albumartist": albumartist,
        "title": title,
        "album": album,
        "year": year,
        "track": track,
        "tracktotal": tracktotal,
        "disc": disc,
        "disctotal": disctotal,
        "mb_trackid": mb_trackid,
        "mb_albumid": mb_albumid,
        "mb_releasegroupid": mb_releasegroupid,
        "mb_artistid": mb_artistid,
        "mb_albumartistid": mb_albumartistid,
        "mb_albumartistids": mb_albumartistids,
        "country": _playlist_clean_video_text(placement_raw.get("country") or ""),
        "label": _playlist_clean_video_text(placement_raw.get("label") or ""),
        "genre": _playlist_clean_video_text(placement_raw.get("genre") or ""),
        "match": {
            "source": "manual-playlist-placement",
            "manual": True,
        },
    }


def _playlist_place_quality_candidate_job(item_id: int,
                                          placement: Dict[str, Any],
                                          sync_playlist: str = "") -> str:
    def _do(log, cancel_event=None):
        candidates = _playlist_quality_cleanup_candidates(
            limit=50,
            item_ids=[item_id],
            filter_mode="repair",
        )
        candidate = next((c for c in candidates if int(c.get("id") or 0) == item_id), None)
        if not candidate:
            # Fail closed: an item that is not (or is no longer) a genuine
            # quality review/repair candidate must never be fabricated into
            # one just to let a placement proceed. This can legitimately
            # happen on a race (the item was fixed/removed between the API
            # call's own candidate check and this job actually running).
            raise RuntimeError(f"Item {item_id} is not a playlist review/repair candidate")

        log.append(
            f"[debug] Manual playlist placement id={item_id} "
            f"{placement.get('artist')} - {placement.get('title')} -> "
            f"{placement.get('albumartist')} - {placement.get('album')}"
        )
        result = _playlist_apply_album_placement(
            None,
            candidate,
            placement,
            log=log,
            cancel_event=cancel_event,
        )
        if not result.get("repaired"):
            raise RuntimeError(result.get("reason") or "Manual placement failed")
        log.append(
            f"[debug] Manual placement moved item {item_id}: "
            f"{result.get('old_path')} -> {result.get('new_path')}"
        )
        if sync_playlist:
            try:
                _playlist_sync_all_locked(log, names=[sync_playlist])
            except Exception as ex:
                log.append(f"  WARN: playlist sync after manual placement failed: {ex}")
        try:
            _trigger_plex_refresh(log, workflow="playlist")
        except Exception:
            pass
        return {"ok": True, "backup": "", "result": result}

    job = jobs.start_python(_do, label=f"Playlist manual place: item {item_id}")
    return job.job_id


def _playlist_move_singleton_candidate(candidate: Dict[str, Any],
                                       log: Optional[List[str]] = None,
                                       cancel_event=None) -> Dict[str, Any]:
    item_id = int(candidate.get("id") or 0)
    flags = set(candidate.get("quality_flags") or [])
    if item_id <= 0:
        return {"id": item_id, "moved": False, "reason": "missing item id"}
    if "bad_playlist_path" not in flags:
        return {"id": item_id, "moved": False, "reason": "not a playlist singleton path"}

    artist = _s(candidate.get("artist") or candidate.get("query_artist") or "")
    clean_name = _clean_playlist_name(artist or "Playlist")
    key = _playlist_resolve_operation_key(
        candidate.get("playlist_key"), candidate.get("playlist_id"), clean_name, log=log)
    if not key:
        return {
            "id": item_id,
            "moved": False,
            "reason": "could not resolve the playlist for this item; refusing to move it",
        }

    try:
        res = composite_workflows.place_playlist_imported_item(
            playlist_key=key,
            item_id=item_id,
            placement={},
            action="move_singleton",
        )
        if isinstance(res, dict) and res.get("ok"):
            return {
                "id": item_id,
                "moved": bool(res.get("moved", True)),
                "old_path": res.get("old_path", candidate.get("path", "")),
                "new_path": res.get("new_path", ""),
                "artist": candidate.get("artist", ""),
                "title": candidate.get("title", ""),
                "album": candidate.get("album", ""),
                "returncode": res.get("returncode", 0),
            }
        return {
            "id": item_id,
            "moved": False,
            "reason": (res.get("error") if isinstance(res, dict) else None) or "Engine move failed",
        }
    except Exception as ex:
        return {"id": item_id, "moved": False, "reason": f"Engine move IPC failed: {ex}"}


def _playlist_candidate_text_pairs(candidate: Dict[str, Any]) -> List[Tuple[str, str]]:
    metadata = _playlist_repair_metadata(candidate)
    pairs: List[Tuple[str, str]] = []

    def add(artist_value: Any, title_value: Any) -> None:
        artist = _s(artist_value).strip()
        title = _s(title_value).strip()
        if not title:
            return
        key = (_norm(artist), _norm(title))
        if key not in {(_norm(a), _norm(t)) for a, t in pairs}:
            pairs.append((artist, title))
        split = _playlist_split_artist_title(title)
        if split:
            skey = (_norm(split[0]), _norm(split[1]))
            if skey not in {(_norm(a), _norm(t)) for a, t in pairs}:
                pairs.append(split)

    add(candidate.get("query_artist"), candidate.get("query_title"))
    add(metadata.get("artist"), metadata.get("title"))
    add(candidate.get("artist"), candidate.get("title"))
    return pairs


def _playlist_candidate_matches_track(candidate: Dict[str, Any],
                                      track: Dict[str, Any]) -> bool:
    wanted_artist = _s(track.get("artist") or track.get("query_artist") or "").strip()
    wanted_title = _s(track.get("title") or track.get("query_title") or "").strip()
    if not wanted_title:
        return False
    for cand_artist, cand_title in _playlist_candidate_text_pairs(candidate):
        title_score = _playlist_title_score(wanted_title, cand_title)
        artist_score = (
            _playlist_artist_name_score(wanted_artist, cand_artist)
            if wanted_artist else 1.0
        )
        if title_score >= 0.86 and artist_score >= 0.68:
            return True
    return False


def _playlist_place_recent_imports_for_tracks(tracks: List[Dict[str, Any]],
                                              since_ts: float,
                                              log: Optional[List[str]] = None,
                                              cancel_event=None) -> Dict[str, Any]:
    wanted = [t for t in tracks if _s(t.get("title") or t.get("query_title") or "").strip()]
    summary = {"checked": 0, "matched_candidates": 0, "placed": 0, "failed": 0, "results": []}
    if not wanted:
        return summary
    limit = max(200, min(2000, len(wanted) * 12))
    try:
        candidates = _playlist_quality_cleanup_candidates(limit=limit, filter_mode="repair")
    except PlaylistQualityCandidatesUnavailableError as ex:
        # Best-effort post-import album placement: the import itself has
        # already completed successfully by this point, so treat a failed
        # quality-candidate query as "nothing to place yet" rather than
        # failing the whole playlist sync job.
        _playlist_log(log, f"  [playlist-place] Skipping post-import placement: {ex}")
        summary["error"] = str(ex)
        return summary
    since_floor = float(since_ts or 0) - 120.0
    used_items: set = set()
    for candidate in candidates:
        item_id = int(candidate.get("id") or 0)
        if item_id <= 0 or item_id in used_items:
            continue
        added = float(candidate.get("added") or 0)
        if since_floor > 0 and added > 0 and added < since_floor:
            continue
        summary["checked"] += 1
        matched_track = None
        for track in wanted:
            if _playlist_candidate_matches_track(candidate, track):
                matched_track = track
                break
        if not matched_track:
            continue
        used_items.add(item_id)
        summary["matched_candidates"] += 1
        candidate = dict(candidate)
        candidate["query_artist"] = _s(
            matched_track.get("artist") or matched_track.get("query_artist") or ""
        ).strip()
        candidate["query_title"] = _s(
            matched_track.get("title") or matched_track.get("query_title") or ""
        ).strip()
        result = _playlist_repair_quality_candidate(
            None, candidate, log=log, cancel_event=cancel_event)
        result["playlist_track_id"] = _playlist_status_id(matched_track)
        result["playlist_track"] = _playlist_track_manifest_payload(matched_track)
        summary["results"].append(result)
        if result.get("repaired"):
            summary["placed"] += 1
            _playlist_log(
                log,
                "  [playlist-place] "
                f"{result.get('artist')} - {result.get('title')} -> "
                f"{result.get('albumartist')} - {result.get('album')}",
            )
        else:
            summary["failed"] += 1
            _playlist_log(
                log,
                f"  [playlist-place] Item {item_id} not placed: "
                f"{result.get('reason') or 'unknown reason'}",
            )
    if summary["placed"]:
        _invalidate_lib_cache()
    return summary


def _playlist_run_quality_cleanup_job(action: str,
                                      selected_candidates: List[Dict[str, Any]],
                                      summary: Dict[str, Any],
                                      log: Optional[List[str]] = None,
                                      cancel_event=None) -> Dict[str, Any]:
    wanted_action = action if action in {"repair", "delete_preview", "move_singletons"} else "repair"
    candidate_ids = [int(c.get("id") or 0) for c in selected_candidates if int(c.get("id") or 0) > 0]
    result_payload: Dict[str, Any] = {
        "ok": True,
        "dry_run": False,
        "action": action,
        "summary": summary,
        "backup": "",
        "rows_deleted": 0,
        "files_deleted": 0,
        "rows_repaired": 0,
        "rows_moved": 0,
        "repaired": [],
        "deleted": [],
        "moved": [],
    }
    _playlist_log(log, f"[debug] Playlist quality cleanup action={action} selected={len(candidate_ids)}")
    if not candidate_ids:
        _playlist_log(log, "[debug] No selected playlist quality candidates matched the requested action")
        return result_payload

    backup = f"{LIB_PATH}.bak-{int(time.time())}-playlist-quality-cleanup"
    shutil.copy2(LIB_PATH, backup)
    result_payload["backup"] = backup
    _playlist_log(log, f"[debug] DB backup created: {backup}")

    files_deleted = 0
    rows_deleted = 0
    rows_repaired = 0
    rows_moved = 0
    deleted: List[Dict[str, Any]] = []
    repaired: List[Dict[str, Any]] = []
    moved: List[Dict[str, Any]] = []
    if action == "repair":
        for idx, candidate in enumerate(selected_candidates, start=1):
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("Playlist quality cleanup was cancelled")
            _playlist_log(
                log,
                "[debug] Repair candidate "
                f"{idx}/{len(selected_candidates)} id={candidate.get('id')} "
                f"artist={candidate.get('artist')} title={candidate.get('title')} "
                f"path={candidate.get('path')}",
            )
            result = _playlist_repair_quality_candidate(
                None, candidate, log=log, cancel_event=cancel_event)
            repaired.append(result)
            if result.get("repaired"):
                rows_repaired += 1
            else:
                _playlist_log(
                    log,
                    f"[debug] Candidate id={candidate.get('id')} not repaired: "
                    f"{result.get('reason') or 'unknown reason'}",
                )
    elif action == "move_singletons":
        for idx, candidate in enumerate(selected_candidates, start=1):
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("Playlist quality cleanup was cancelled")
            _playlist_log(
                log,
                "[debug] Move-singleton candidate "
                f"{idx}/{len(selected_candidates)} id={candidate.get('id')} "
                f"artist={candidate.get('artist')} title={candidate.get('title')} "
                f"path={candidate.get('path')}",
            )
            result = _playlist_move_singleton_candidate(
                candidate, log=log, cancel_event=cancel_event)
            moved.append(result)
            if result.get("moved"):
                rows_moved += 1
            else:
                _playlist_log(
                    log,
                    f"[debug] Candidate id={candidate.get('id')} not moved: "
                    f"{result.get('reason') or 'unknown reason'}",
                )
    elif action == "delete_preview":
        try:
            plan_res = composite_workflows.plan_playlist_media_cleanup({"item_ids": candidate_ids})
            if plan_res.get("ok"):
                op_id = plan_res["operation_id"]
                apply_res = composite_workflows.apply_playlist_media_cleanup(op_id)
                if apply_res.get("ok"):
                    files_deleted = int(apply_res.get("deleted_items") or len(candidate_ids))
                    rows_deleted = int(apply_res.get("deleted_items") or len(candidate_ids))
                    _playlist_log(log, f"[playlist] Quality cleanup transaction applied: {op_id}")
        except Exception as ex:
            _playlist_log(log, f"[playlist] Quality cleanup transaction failed: {ex}")

    _invalidate_lib_cache()
    if rows_repaired or rows_deleted or rows_moved:
        try:
            _trigger_plex_refresh(log, workflow="playlist")
        except Exception as ex:
            _playlist_log(log, f"[debug] Plex refresh trigger skipped: {ex}")
    _playlist_log(
        log,
        "[debug] Playlist quality cleanup complete: "
        f"action={wanted_action} repaired={rows_repaired} moved={rows_moved} rows_deleted={rows_deleted} "
        f"files_deleted={files_deleted}",
    )
    result_payload.update({
        "rows_deleted": rows_deleted,
        "files_deleted": files_deleted,
        "rows_repaired": rows_repaired,
        "rows_moved": rows_moved,
        "repaired": repaired,
        "deleted": deleted,
        "moved": moved,
    })
    return result_payload


def _playlist_match_reference_track(track: Dict[str, Any],
                                    index: Dict[str, Any],
                                    verify_acoustid: bool = False) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    artist = _s(track.get("artist") or track.get("query_artist") or "").strip()
    title = _s(track.get("title") or track.get("query_title") or "").strip()
    path = _s(track.get("path") or "").strip()
    item = _playlist_item_from_path(path, index) if path else None
    if not item:
        item = _playlist_item_from_text(artist, title, index)
    payload = {
        "artist": artist or (item or {}).get("artist", ""),
        "title": title or (item or {}).get("title", ""),
    }
    if path:
        payload["path"] = path
    for key in ("source_artist", "source_title", "canonicalized", "canonical_source"):
        if key in track:
            payload[key] = track.get(key)
    if verify_acoustid and item:
        file_path = _s(item.get("path") or "").strip()
        if file_path:
            item["acoustid_status"] = _acoustid_verify_match(
                file_path,
                artist or _s(item.get("artist") or ""),
                title or _s(item.get("title") or ""),
            )
    return payload, item


def _playlist_m3u_track_rows(name: str, index: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    clean_name = _clean_playlist_name(name)
    key = _playlist_existing_key(clean_name)
    raw_items = []

    try:
        res = composite_workflows.read_playlist_m3u(key, fallback_name=clean_name)
        if isinstance(res, dict) and res.get("ok") and res.get("exists"):
            raw_items = res.get("items") or []
    except Exception:
        pass

    tracks: List[Dict[str, Any]] = []
    matched: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []

    for raw in raw_items:
        artist = _s(raw.get("artist") or "").strip()
        title = _s(raw.get("title") or "").strip()
        line = _s(raw.get("path") or "").strip()
        if not title and line:
            title = Path(line.replace("\\", "/")).stem
        track_payload, item = _playlist_match_reference_track(
            {"artist": artist, "title": title, "path": line}, index)
        tracks.append(track_payload)
        if item:
            item["query_artist"] = artist or item.get("artist", "")
            item["query_title"] = title or item.get("title", "")
            matched.append(item)
        else:
            missing.append(track_payload)
    return tracks, matched, missing


def _playlist_rows_for_saved_playlist(name: str,
                                      index: Dict[str, Any],
                                      checkpoint_states: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    m3u_tracks, m3u_matched, m3u_missing = _playlist_m3u_track_rows(name, index)
    desired_tracks, desired_source = _playlist_desired_tracks_for_name(
        name, min_tracks=len(m3u_tracks), checkpoint_states=checkpoint_states)
    if desired_tracks:
        desired_tracks = _playlist_apply_tombstones(name, desired_tracks)
    source_tracks = desired_tracks or m3u_tracks
    if desired_tracks:
        tracks: List[Dict[str, Any]] = []
        matched: List[Dict[str, Any]] = []
        missing: List[Dict[str, Any]] = []
        for track in desired_tracks:
            track_payload, item = _playlist_match_reference_track(track, index)
            tracks.append(track_payload)
            if item:
                item["query_artist"] = track_payload.get("artist") or item.get("artist", "")
                item["query_title"] = track_payload.get("title") or item.get("title", "")
                matched.append(item)
            else:
                missing.append(track_payload)
    else:
        tracks, matched, missing = m3u_tracks, m3u_matched, m3u_missing
    if desired_tracks and desired_source == "checkpoint":
        _playlist_write_manifest(
            name,
            desired_tracks,
            matched_tracks=matched,
            missing_tracks=missing,
            source="checkpoint",
        )
    return {
        "tracks": tracks,
        "matched": matched,
        "missing": missing,
        "m3u_tracks": len(m3u_tracks),
        "desired_source": desired_source,
        "desired_tracks": len(source_tracks),
    }


def _playlist_count_value(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0


def _playlist_manifest_list_summary(name: str,
                                    manifest: Dict[str, Any]) -> Optional[Dict[str, int]]:
    desired_raw = manifest.get("desired_tracks") if isinstance(manifest, dict) else []
    if not isinstance(desired_raw, list) or not desired_raw:
        return None
    desired = _playlist_apply_tombstones(name, _playlist_clean_track_list(desired_raw), manifest)
    states_raw = manifest.get("track_states") if isinstance(manifest.get("track_states"), dict) else {}
    counts = {status: 0 for status in PLAYLIST_PIPELINE_STATES}
    for row in states_raw.values():
        if not isinstance(row, dict):
            continue
        status = _s(row.get("status") or "").strip().lower()
        if status in counts:
            counts[status] += 1
    removed = len(manifest.get("removed_tracks") or [])
    excluded = len(manifest.get("excluded_tracks") or [])
    matched = _playlist_count_value(manifest.get("matched_count"))
    missing = _playlist_count_value(manifest.get("missing_count"))
    if matched == 0 and missing == 0:
        matched = counts.get("available", 0) + counts.get("imported", 0) + counts.get("plex_synced", 0)
        missing = max(0, len(desired) - matched)
    total = max(len(desired), matched + missing)
    return {
        "tracks": total,
        "available": matched,
        "missing": missing,
        "quality_bad": 0,
        "quality_review": counts.get("review_required", 0),
        "m3u_tracks": len(desired),
        "desired_source": _s(manifest.get("source") or "manifest"),
        "downloaded": counts.get("downloaded", 0) + counts.get("waiting_import", 0),
        "imported": counts.get("imported", 0) + counts.get("plex_synced", 0),
        "failed": counts.get("failed", 0),
        "review_required": counts.get("review_required", 0),
        "removed": removed,
        "excluded": excluded,
        "plex_synced_count": counts.get("plex_synced", 0),
    }


def _playlist_summary_waiting_count(summary: Dict[str, Any]) -> int:
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    return max(
        _playlist_count_value(summary.get("downloaded")),
        _playlist_count_value(summary.get("waiting_for_import")),
        _playlist_count_value(counts.get("downloaded"))
        + _playlist_count_value(counts.get("waiting_import"))
        + _playlist_count_value(counts.get("importing")),
    )


def _playlist_checkpoint_is_actionable(summary: Dict[str, Any],
                                       checkpoint: Dict[str, Any]) -> bool:
    if not checkpoint:
        return False
    if not checkpoint.get("checkpoint_interrupted"):
        return True
    if _playlist_count_value(checkpoint.get("checkpoint_waiting_for_import")) > 0:
        return True
    current_missing = _playlist_count_value(summary.get("missing_count", summary.get("missing")))
    if current_missing > 0:
        return True
    if _playlist_summary_waiting_count(summary) > 0:
        return True
    current_tracks = _playlist_count_value(summary.get("total", summary.get("tracks")))
    return current_tracks <= 0 and _playlist_count_value(checkpoint.get("checkpoint_missing")) > 0


def _playlist_visible_checkpoint_summary(summary: Dict[str, Any],
                                         checkpoint: Dict[str, Any]) -> Dict[str, Any]:
    if not checkpoint:
        return {}
    if _playlist_checkpoint_is_actionable(summary, checkpoint):
        return checkpoint
    current_tracks = _playlist_count_value(summary.get("total", summary.get("tracks")))
    visible = dict(checkpoint)
    visible.update({
        "checkpoint_status": "complete",
        "checkpoint_phase": "complete",
        "checkpoint_current": "",
        "checkpoint_interrupted": False,
        "checkpoint_missing": 0,
        "checkpoint_waiting_for_import": 0,
        "checkpoint_stale_complete": True,
    })
    if current_tracks > 0:
        visible["checkpoint_tracks"] = current_tracks
    return visible


def _playlist_apply_checkpoint_summary(summary: Dict[str, Any],
                                       checkpoint: Dict[str, Any]) -> Dict[str, Any]:
    checkpoint = _playlist_visible_checkpoint_summary(summary, checkpoint)
    if not checkpoint:
        return {}
    summary.update(checkpoint)
    if not checkpoint.get("checkpoint_interrupted"):
        return checkpoint
    checkpoint_tracks = _playlist_count_value(checkpoint.get("checkpoint_tracks"))
    checkpoint_missing = _playlist_count_value(checkpoint.get("checkpoint_missing"))
    if checkpoint_tracks <= 0:
        return checkpoint
    summary["tracks"] = checkpoint_tracks
    summary["missing"] = checkpoint_missing
    summary["available"] = max(0, checkpoint_tracks - checkpoint_missing)
    return checkpoint


def _playlist_checkpoint_list_summary(name: str,
                                      checkpoint_states: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    clean_name = _clean_playlist_name(name)
    if not clean_name:
        return None
    key = _playlist_existing_key(clean_name)
    if key:
        try:
            res = composite_workflows.read_playlist_m3u(key, fallback_name=clean_name)
            if isinstance(res, dict) and res.get("ok") and res.get("exists"):
                return None
        except Exception:
            pass
    checkpoint = _playlist_latest_job_state_summary(clean_name, checkpoint_states)
    checkpoint_tracks = _playlist_count_value(checkpoint.get("checkpoint_tracks"))
    if checkpoint_tracks <= 0:
        return None
    checkpoint_missing = _playlist_count_value(checkpoint.get("checkpoint_missing"))
    summary: Dict[str, Any] = {
        "tracks": checkpoint_tracks,
        "available": max(0, checkpoint_tracks - checkpoint_missing),
        "missing": checkpoint_missing,
        "quality_bad": 0,
        "quality_review": 0,
        "m3u_tracks": 0,
        "desired_source": "checkpoint",
        "downloaded": _playlist_count_value(checkpoint.get("checkpoint_waiting_for_import")),
        "imported": 0,
        "failed": 0,
        "review_required": 0,
        "removed": 0,
        "excluded": 0,
        "plex_synced_count": 0,
    }
    _playlist_apply_checkpoint_summary(summary, checkpoint)
    return summary


def _playlist_engine_m3u_count_summary(name: str,
                                       manifest: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, int]]:
    clean_name = _clean_playlist_name(name)
    key = _playlist_existing_key(clean_name, manifest)
    if not key:
        return None
    try:
        res = composite_workflows.read_playlist_m3u(key, fallback_name=clean_name)
    except Exception:
        return None
    if not (isinstance(res, dict) and res.get("ok") and res.get("exists")):
        return None
    raw_items = res.get("items") if isinstance(res.get("items"), list) else []
    tracks = sum(1 for row in raw_items if isinstance(row, dict))
    return {
        "tracks": tracks,
        "available": 0,
        "missing": tracks,
        "quality_bad": 0,
        "quality_review": 0,
        "m3u_tracks": tracks,
        "desired_source": "m3u",
        "downloaded": 0,
        "imported": 0,
        "failed": 0,
        "review_required": 0,
        "removed": 0,
        "excluded": 0,
        "plex_synced_count": 0,
    }


def _playlist_m3u_summary(name: str,
                          index: Optional[Dict[str, Any]],
                          checkpoint_states: Optional[List[Dict[str, Any]]] = None,
                          manifest: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    manifest = manifest if manifest is not None else _playlist_read_manifest(name)
    manifest_summary = _playlist_manifest_list_summary(name, manifest)
    if manifest_summary:
        summary = manifest_summary
        checkpoint = _playlist_latest_job_state_summary(name, checkpoint_states)
        _playlist_apply_checkpoint_summary(summary, checkpoint)
        return summary
    checkpoint_summary = _playlist_checkpoint_list_summary(name, checkpoint_states)
    if checkpoint_summary:
        return checkpoint_summary
    try:
        index = index or _playlist_library_index()
        rows = _playlist_rows_for_saved_playlist(name, index, checkpoint_states)
    except Exception:
        fallback = _playlist_engine_m3u_count_summary(name, manifest)
        if fallback is not None:
            return fallback
        raise
    matched = rows.get("matched") or []
    missing = rows.get("missing") or []
    summary = {
        "tracks": len(rows.get("tracks") or []),
        "available": len(matched),
        "missing": len(missing),
        "quality_bad": 0,
        "quality_review": 0,
        "m3u_tracks": int(rows.get("m3u_tracks") or 0),
        "desired_source": _s(rows.get("desired_source") or "m3u"),
    }
    counts = _playlist_pipeline_counts(name, matched, missing)
    summary.update({
        "downloaded": counts.get("downloaded", 0) + counts.get("waiting_import", 0),
        "imported": counts.get("imported", 0) + counts.get("plex_synced", 0),
        "failed": counts.get("failed", 0),
        "review_required": counts.get("review_required", 0),
        "removed": counts.get("removed", 0),
        "excluded": counts.get("excluded", 0),
        "plex_synced_count": counts.get("plex_synced", 0),
    })
    checkpoint = _playlist_latest_job_state_summary(name, checkpoint_states)
    _playlist_apply_checkpoint_summary(summary, checkpoint)
    for item in matched:
        if item.get("quality") == "bad":
            summary["quality_bad"] += 1
        elif item.get("quality") == "review":
            summary["quality_review"] += 1
    return summary


def _playlist_manifest_name_from_file(path: Path, diagnostics: List[str]) -> Tuple[str, Dict[str, Any]]:
    fallback = path.name[:-len(".playlist.json")] if path.name.lower().endswith(".playlist.json") else path.stem
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            diagnostics.append(f"manifest parse failed: {path.name} is not a JSON object")
            return _clean_playlist_name(fallback), {}
        manifest = _playlist_sanitize_manifest(data)
    except Exception as ex:
        _app_logger.warning("Playlist manifest parse failed for %r: %s", path.name, type(ex).__name__)
        diagnostics.append(f"manifest parse failed: {path.name}")
        return _clean_playlist_name(fallback), {}
    clean_name = _clean_playlist_name(
        _s(manifest.get("name") or manifest.get("playlist") or manifest.get("title") or fallback)
    )
    if not clean_name:
        diagnostics.append(f"missing name/id: {path.name}")
        clean_name = _clean_playlist_name(fallback)
    return clean_name, manifest


def _playlist_saved_playlist_records(checkpoint_states: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    records: Dict[str, Dict[str, Any]] = {}
    diagnostics: List[str] = []

    def record_for(name: str,
                   *,
                   key: str = "",
                   manifest: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        clean_name = _clean_playlist_name(name)
        if not clean_name:
            return None
        record_key = _s(key or "").strip()
        if not record_key:
            record_key = _playlist_existing_key(clean_name, manifest)
        if not record_key:
            record_key = f"legacy_{_playlist_slug(clean_name)}"
        row = records.get(record_key)
        if row is None:
            row = {"name": clean_name, "key": record_key, "has_m3u": False, "has_manifest": False, "has_checkpoint": False}
            records[record_key] = row
        elif manifest is not None:
            row["name"] = clean_name
        return row

    try:
        res = composite_workflows.list_playlist_m3u()
        files = []
        if isinstance(res, dict) and res.get("ok"):
            files = res.get("playlists") if isinstance(res.get("playlists"), list) else res.get("files")
        if isinstance(files, list):
            for pl in files:
                if not isinstance(pl, dict):
                    continue
                pl_key = _s(pl.get("playlist_key") or pl.get("key") or "").strip()
                pl_name = _s(pl.get("display_name") or pl.get("name") or pl_key).strip()
                if pl_name:
                    row = record_for(pl_name, key=pl_key)
                    if row is not None:
                        row["has_m3u"] = True
                        row["playlist_key"] = pl_key
    except Exception as ex:
        diagnostics.append(f"engine m3u list error: {type(ex).__name__}")

    _playlist_ensure_state_dirs()
    if PLAYLIST_MANIFESTS_DIR.exists():
        for path in sorted(PLAYLIST_MANIFESTS_DIR.glob("*.playlist.json"), key=lambda p: p.name.lower()):
            clean_name, manifest = _playlist_manifest_name_from_file(path, diagnostics)
            manifest_key = path.name[:-len(".playlist.json")] if path.name.lower().endswith(".playlist.json") else path.stem
            row = record_for(clean_name, key=manifest_key, manifest=manifest)
            if row is None:
                diagnostics.append(f"missing name/id: {path.name}")
                continue
            if row.get("has_manifest"):
                diagnostics.append(f"duplicate merged: {path.name}")
            row["has_manifest"] = True
            row["manifest_path"] = str(path)
            row["manifest"] = manifest

    for state in checkpoint_states:
        state_name = _playlist_job_state_name(state)
        if not state_name:
            diagnostics.append(f"checkpoint orphaned: {_s(state.get('job_id') or 'unknown')}")
            continue
        row = record_for(state_name)
        if row is None:
            diagnostics.append(f"checkpoint orphaned: {_s(state.get('job_id') or 'unknown')}")
            continue
        row["has_checkpoint"] = True

    return sorted(records.values(), key=lambda row: _s(row.get("name") or "").lower()), diagnostics


def _playlist_find_track_row(rows: Iterable[Dict[str, Any]],
                             target: Dict[str, Any]) -> Tuple[int, Optional[Dict[str, Any]]]:
    target_keys = _playlist_manifest_match_keys(target)
    for index, row in enumerate(rows or []):
        if target_keys and target_keys & _playlist_manifest_match_keys(row):
            return index, dict(row)
    return -1, None


def _playlist_write_local_membership(name: str,
                                     manifest: Dict[str, Any]) -> Dict[str, Any]:
    clean_name = _clean_playlist_name(name)
    desired = _playlist_apply_tombstones(
        clean_name, manifest.get("desired_tracks") or [], manifest)
    manifest["desired_tracks"] = desired
    index = _playlist_library_index()
    matched, missing = _playlist_match_reference_tracks(desired, index)
    manifest["matched_count"] = len(matched)
    manifest["missing_count"] = len(missing)
    _playlist_replace_manifest(clean_name, manifest)
    if matched:
        _create_playlist_outputs(
            clean_name,
            matched,
            desired_tracks=desired,
            missing_tracks=missing,
            source=_s(manifest.get("source") or ""),
            content=_s(manifest.get("content") or ""),
            sync_plex=False,
        )
    else:
        key = _playlist_key(clean_name, manifest)
        export_result = composite_workflows.export_playlist_m3u(key, clean_name, [])
        if not (isinstance(export_result, dict) and export_result.get("ok")):
            raise RuntimeError("m3u_export_failed")
    return _playlist_detail_payload(clean_name, index)


def _playlist_delete_staged_track_file(name: str,
                                       track: Dict[str, Any],
                                       requested_path: str = "") -> Dict[str, Any]:
    clean_name = _clean_playlist_name(name)
    track_key = _playlist_status_id(track)
    states = _playlist_manifest_track_states(clean_name)
    state_row = states.get(track_key) or {}
    # The server-owned manifest state, not a browser-supplied requested_path,
    # is the authority for which file gets deleted -- otherwise a caller
    # could point requested_path at any staged file and use this endpoint's
    # own containment checks to authorize deleting it (SEC-002 Wave 9 second
    # final review: requested-path-as-sole-authority gap). requested_path is
    # accepted only when it canonically matches the path already recorded
    # here for this track; a mismatching value is rejected outright rather
    # than silently ignored.
    authoritative_raw = _s(state_row.get("staged_path") or state_row.get("path") or "").strip()
    if not authoritative_raw:
        raise RuntimeError("No staged download is recorded for this track")
    if requested_path:
        req_resolved = Path(_s(requested_path).strip()).resolve(strict=False)
        auth_resolved = Path(authoritative_raw).resolve(strict=False)
        if req_resolved != auth_resolved:
            raise RuntimeError("requested_path does not match the server-recorded staged path for this track")
    raw_path = authoritative_raw
    path = Path(raw_path)
    resolved_path = path.resolve(strict=False)
    # Deliberately NOT the broad, shared PLAYLIST_DOWNLOAD_ROOT here: every
    # playlist's staged files live under its own get_playlist_staging_root()
    # subdirectory, and containment against the *shared* parent would let a
    # browser-supplied requested_path (or a stale/tampered manifest entry)
    # for playlist A authorize deleting a staged file that actually belongs
    # to playlist B, since both are nested under the same shared root
    # (SEC-002 Wave 9 final review: cross-playlist staged-deletion gap).
    # Authorization is scoped to this playlist's own staging root only.
    library_root = MUSIC_ROOT.resolve(strict=False)
    staging_root = PLAYLIST_DOWNLOAD_ROOT.resolve(strict=False)
    playlist_key = _playlist_existing_key(clean_name)
    if not playlist_key:
        raise RuntimeError("Playlist identity is unavailable; cannot delete staged track")
    playlist_staging = (PLAYLIST_DOWNLOAD_ROOT / playlist_key).resolve(strict=False)

    is_in_library = False
    try:
        resolved_path.relative_to(library_root)
        is_in_library = True
    except ValueError:
        pass
    if is_in_library:
        raise RuntimeError("Refusing to delete a Beets library file; this action only deletes playlist staging")

    try:
        resolved_path.relative_to(playlist_staging)
        is_in_staging = True
    except ValueError:
        is_in_staging = False
    if not is_in_staging:
        raise RuntimeError("Refusing to delete a file outside this playlist's own staging directory")

    if resolved_path in (staging_root, playlist_staging, library_root):
        raise RuntimeError("Refusing to delete staging root directory")
    if path.suffix.lower() not in AUDIO_EXT:
        raise RuntimeError("Refusing to delete a non-audio staging file")

    # Local checks above are defense-in-depth, fast-fail validation only.
    # The actual mutation is engine-owned: the web manager has no writable
    # media/staging filesystem in the supported topology, so deletion is
    # delegated to the control agent, which re-validates containment,
    # symlinks, and root-self on its own side under an OS lock (SEC-002
    # Wave 9 continuation -- no local Path.unlink() fallback here).
    try:
        res = composite_workflows.delete_playlist_staged_track(playlist_key, track_key, str(resolved_path))
    except Exception as exc:
        raise RuntimeError("Engine is unavailable; cannot delete staged track file") from exc
    if not (isinstance(res, dict) and res.get("ok")):
        err = _s(res.get("error")) if isinstance(res, dict) else ""
        raise RuntimeError(err or "Engine refused to delete staged track file")
    deleted = bool(res.get("deleted"))

    _playlist_store_track_state(
        clean_name,
        track,
        "removed",
        message="downloaded staging file deleted by user",
        failure_reason="deleted staged file",
        staged_path="",
        path="",
    )
    return {"deleted": deleted, "path": str(resolved_path)}


def _playlist_apply_track_action(name: str,
                                 action: str,
                                 track: Dict[str, Any],
                                 requested_path: str = "") -> Dict[str, Any]:
    clean_name = _clean_playlist_name(name)
    manifest = _playlist_read_manifest(clean_name)
    desired = list(manifest.get("desired_tracks") or [])
    removed = list(manifest.get("removed_tracks") or [])
    excluded = list(manifest.get("excluded_tracks") or [])
    action = _s(action).strip().lower().replace("-", "_")
    active_index, active_row = _playlist_find_track_row(desired, track)
    removed_index, removed_row = _playlist_find_track_row(removed, track)
    excluded_index, excluded_row = _playlist_find_track_row(excluded, track)
    canonical = active_row or removed_row or excluded_row or _playlist_track_manifest_payload(track)
    now = time.time()
    file_result: Dict[str, Any] = {}

    if action in {"remove", "exclude"}:
        if active_index >= 0:
            desired.pop(active_index)
        if removed_index >= 0:
            removed.pop(removed_index)
        if excluded_index >= 0:
            excluded.pop(excluded_index)
        tombstone = {
            **_playlist_track_manifest_payload(canonical),
            "reason": f"track {action}d by user" if action == "exclude" else "track removed by user",
            "updated_at": now,
        }
        if action == "exclude":
            excluded.append(tombstone)
        else:
            removed.append(tombstone)
        _playlist_store_track_state(
            clean_name, canonical, "excluded" if action == "exclude" else "removed",
            message=tombstone["reason"], failure_reason=tombstone["reason"])
    elif action == "restore":
        if removed_index >= 0:
            removed.pop(removed_index)
        if excluded_index >= 0:
            excluded.pop(excluded_index)
        if active_index < 0:
            desired.append(_playlist_track_manifest_payload(canonical))
        _playlist_store_track_state(
            clean_name, canonical, "missing",
            message="restored to playlist; availability will be checked",
            failure_reason="")
    elif action == "delete_staged":
        file_result = _playlist_delete_staged_track_file(
            clean_name, canonical, requested_path=requested_path)
        if active_index >= 0:
            desired.pop(active_index)
        if removed_index < 0:
            removed.append({
                **_playlist_track_manifest_payload(canonical),
                "reason": "downloaded staging file deleted by user",
                "updated_at": now,
            })
    elif action in {"retry", "retry_download", "retry_import"}:
        existing_state = _playlist_state_for_track(clean_name, canonical)
        retry_path = requested_path or _s(existing_state.get("staged_path") or existing_state.get("path") or "")
        next_status = "waiting_import" if action == "retry_import" and retry_path else "missing"
        _playlist_store_track_state(
            clean_name, canonical, next_status,
            message="retry requested",
            failure_reason="",
            staged_path=retry_path)
    else:
        raise RuntimeError(f"Unsupported playlist track action: {action}")

    manifest = _playlist_read_manifest(clean_name)
    manifest.update({
        "desired_tracks": _playlist_merge_desired_tracks(desired),
        "removed_tracks": removed,
        "excluded_tracks": excluded,
    })
    detail = _playlist_write_local_membership(clean_name, manifest)
    return {"ok": True, "action": action, "track": canonical, **file_result, "playlist": detail}


def _playlist_state_for_track(name: str,
                              track: Dict[str, Any],
                              states: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    states = states if isinstance(states, dict) else _playlist_manifest_track_states(name)
    direct = states.get(_playlist_status_id(track))
    if isinstance(direct, dict):
        return dict(direct)
    keys = _playlist_manifest_match_keys(track)
    for row in states.values():
        if isinstance(row, dict) and keys & _playlist_manifest_match_keys(row):
            return dict(row)
    return {}


def _playlist_track_with_state(name: str,
                               track: Dict[str, Any],
                               default_status: str,
                               states: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    row = dict(track)
    state = _playlist_state_for_track(name, track, states)
    row["pipeline_status"] = _s(state.get("status") or default_status)
    row["pipeline_source"] = _s(state.get("source") or state.get("method") or track.get("source") or "")
    row["pipeline_message"] = _s(state.get("message") or "")
    row["failure_reason"] = _s(state.get("failure_reason") or "")
    row["staged_path"] = _s(state.get("staged_path") or "")
    row["plex_issue"] = _s(state.get("plex_issue") or "")
    row["pipeline_updated_at"] = float(state.get("updated_at") or 0)
    return row


def _playlist_manifest_state_counts(manifest: Dict[str, Any]) -> Dict[str, int]:
    counts = {status: 0 for status in PLAYLIST_PIPELINE_STATES}
    states = manifest.get("track_states") if isinstance(manifest.get("track_states"), dict) else {}
    for row in states.values():
        if not isinstance(row, dict):
            continue
        status = _s(row.get("status") or "").strip().lower()
        if status in counts:
            counts[status] += 1
    counts["removed"] = len(manifest.get("removed_tracks") or [])
    counts["excluded"] = len(manifest.get("excluded_tracks") or [])
    return counts


def _playlist_detail_summary_payload(clean_name: str) -> Dict[str, Any]:
    manifest = _playlist_read_manifest(clean_name)
    manifest_tracks = _playlist_clean_track_list(manifest.get("desired_tracks") or [])
    summary = _playlist_manifest_list_summary(clean_name, manifest)
    if not summary:
        summary = _playlist_checkpoint_list_summary(clean_name) or {}
    checkpoint = _playlist_latest_job_state_summary(clean_name)
    visible_checkpoint = checkpoint
    if summary:
        visible_checkpoint = _playlist_apply_checkpoint_summary(summary, checkpoint)
    counts = _playlist_manifest_state_counts(manifest)
    total = max(
        _playlist_count_value(summary.get("tracks")),
        len(manifest_tracks),
        _playlist_count_value(visible_checkpoint.get("checkpoint_tracks")),
    )
    missing_count = _playlist_count_value(summary.get("missing"))
    available = _playlist_count_value(summary.get("available"))
    if total and available == 0 and missing_count == 0:
        available = max(0, total - missing_count)
    key = _playlist_existing_key(clean_name)
    m3u = f"engine:{key}.m3u" if key else ""
    last_plex = manifest.get("last_plex") if isinstance(manifest.get("last_plex"), dict) else {}
    last_pipeline = manifest.get("last_pipeline") if isinstance(manifest.get("last_pipeline"), dict) else {}
    return {
        "ok": True,
        "name": clean_name,
        "m3u": str(m3u),
        "manifest": str(_playlist_manifest_path(clean_name, allocate=False)) if _playlist_manifest_exists_no_create(clean_name) else "",
        "manifest_tracks": len(manifest_tracks),
        "m3u_tracks": _playlist_count_value(summary.get("m3u_tracks")),
        "desired_source": _s(summary.get("desired_source") or manifest.get("source") or "manifest"),
        "tracks": [],
        "matched": [],
        "missing": [],
        "removed_excluded": [],
        "counts": counts,
        "source": _s(manifest.get("source") or "local_m3u"),
        "source_content": _s(manifest.get("content") or ""),
        "last_plex": last_plex,
        "last_pipeline": last_pipeline,
        "last_sync_status": _s(last_plex.get("status") or "not_run"),
        "available": available,
        "missing_count": missing_count,
        "total": total,
        "detail_mode": "summary",
        "tracks_loaded": False,
        **visible_checkpoint,
    }


def _playlist_m3u_reference_track_rows(name: str) -> List[Dict[str, Any]]:
    clean_name = _clean_playlist_name(name)
    key = _playlist_existing_key(clean_name)
    raw_items = []

    try:
        res = composite_workflows.read_playlist_m3u(key, fallback_name=clean_name)
        if isinstance(res, dict) and res.get("ok") and res.get("exists"):
            raw_items = res.get("items") or []
    except Exception:
        pass

    tracks: List[Dict[str, Any]] = []
    for it in raw_items:
        artist = _s(it.get("artist") or "").strip()
        title = _s(it.get("title") or "").strip()
        line = _s(it.get("path") or "").strip()
        if not title and line:
            title = Path(line.replace("\\", "/")).stem
        row = {"artist": artist, "title": title}
        if line:
            row["path"] = line
        tracks.append(row)
    return tracks


def _playlist_desired_reference_tracks(clean_name: str) -> Tuple[List[Dict[str, Any]], str]:
    desired_tracks, desired_source = _playlist_desired_tracks_for_name(clean_name, min_tracks=0)
    if desired_tracks:
        return _playlist_apply_tombstones(clean_name, desired_tracks), desired_source
    return _playlist_m3u_reference_track_rows(clean_name), "m3u"


def _playlist_state_payload(row: Dict[str, Any], default_status: str) -> Dict[str, Any]:
    out = dict(row)
    out["pipeline_status"] = _s(row.get("status") or row.get("pipeline_status") or default_status)
    out["pipeline_source"] = _s(row.get("source") or row.get("method") or row.get("pipeline_source") or "")
    out["pipeline_message"] = _s(row.get("message") or row.get("pipeline_message") or "")
    out["failure_reason"] = _s(row.get("failure_reason") or "")
    out["staged_path"] = _s(row.get("staged_path") or "")
    out["plex_issue"] = _s(row.get("plex_issue") or "")
    try:
        out["pipeline_updated_at"] = float(row.get("updated_at") or row.get("pipeline_updated_at") or 0)
    except Exception:
        out["pipeline_updated_at"] = 0
    return out


def _playlist_row_group(row: Dict[str, Any], matched: bool) -> str:
    status = _s(row.get("pipeline_status") or row.get("status") or "").strip().lower()
    if status in {"downloaded", "waiting_import", "importing"} or bool(_s(row.get("staged_path") or "").strip()):
        return "waiting"
    if status in {"failed", "source_failed", "review_required"} or bool(_s(row.get("failure_reason") or "").strip()):
        return "failed"
    return "available" if matched else "missing"


def _playlist_page_slice(rows: List[Dict[str, Any]], offset: int, limit: int) -> Tuple[List[Dict[str, Any]], bool]:
    chunk = rows[offset:offset + limit + 1]
    return chunk[:limit], len(chunk) > limit


def _playlist_state_rows_page(clean_name: str, group: str, offset: int, limit: int) -> Tuple[List[Dict[str, Any]], bool, int]:
    manifest = _playlist_read_manifest(clean_name)
    if group == "removed":
        rows = [
            _playlist_state_payload(row, _s(row.get("status") or "removed"))
            for row in _playlist_tombstone_rows(manifest)
        ]
    elif group == "pending_plex":
        last_plex = manifest.get("last_plex") if isinstance(manifest.get("last_plex"), dict) else {}
        rows = []
        for row in last_plex.get("pending_tracks") or []:
            if not isinstance(row, dict):
                continue
            payload = dict(row)
            payload["path"] = _s(payload.get("local_path") or payload.get("path") or "")
            payload["pipeline_status"] = "plex_pending"
            payload["pipeline_message"] = _s(payload.get("reason") or "Plex match pending")
            payload["plex_issue"] = _s(payload.get("reason") or "Plex match pending")
            rows.append(payload)
    else:
        rows = []
        states = manifest.get("track_states") if isinstance(manifest.get("track_states"), dict) else {}
        for row in states.values():
            if not isinstance(row, dict):
                continue
            payload = _playlist_state_payload(row, "missing")
            row_group = _playlist_row_group(payload, matched=False)
            if row_group == group:
                rows.append(payload)
        rows.sort(key=lambda row: float(row.get("pipeline_updated_at") or 0), reverse=True)
    page, has_more = _playlist_page_slice(rows, offset, limit)
    return page, has_more, len(rows)


def _playlist_matched_rows_page(clean_name: str, group: str, offset: int, limit: int) -> Tuple[List[Dict[str, Any]], bool, int]:
    index = _playlist_library_index()
    tracks, _source = _playlist_desired_reference_tracks(clean_name)
    states = _playlist_manifest_track_states(clean_name)
    rows: List[Dict[str, Any]] = []
    seen = 0
    scanned = 0
    for track in tracks:
        scanned += 1
        track_payload, item = _playlist_match_reference_track(track, index)
        if item:
            item["query_artist"] = track_payload.get("artist") or item.get("artist", "")
            item["query_title"] = track_payload.get("title") or item.get("title", "")
            row = _playlist_track_with_state(clean_name, item, "available", states)
            row_group = _playlist_row_group(row, matched=True)
        else:
            row = _playlist_track_with_state(clean_name, track_payload, "missing", states)
            row_group = _playlist_row_group(row, matched=False)
        if row_group != group:
            continue
        if seen < offset:
            seen += 1
            continue
        rows.append(row)
        seen += 1
        if len(rows) > limit:
            break
    has_more = len(rows) > limit
    return rows[:limit], has_more, scanned


def _playlist_group_known_total(summary: Dict[str, Any], group: str) -> int:
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    if group == "available":
        return _playlist_count_value(summary.get("available"))
    if group == "missing":
        return _playlist_count_value(summary.get("missing_count"))
    if group == "waiting":
        return _playlist_count_value(counts.get("downloaded")) + _playlist_count_value(counts.get("waiting_import")) + _playlist_count_value(counts.get("importing"))
    if group == "failed":
        return _playlist_count_value(counts.get("failed")) + _playlist_count_value(counts.get("review_required"))
    if group == "removed":
        return _playlist_count_value(counts.get("removed")) + _playlist_count_value(counts.get("excluded"))
    if group == "pending_plex":
        last_plex = summary.get("last_plex") if isinstance(summary.get("last_plex"), dict) else {}
        return _playlist_count_value(last_plex.get("pending_plex_count") or last_plex.get("tracks_unmatched"))
    return 0


def _playlist_detail_payload(clean_name: str, index: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    index = index or _playlist_library_index()
    key = _playlist_existing_key(clean_name)
    m3u = f"engine:{key}.m3u" if key else ""
    rows = _playlist_rows_for_saved_playlist(clean_name, index)
    tracks = rows.get("tracks") or []
    state_cache = _playlist_manifest_track_states(clean_name)
    matched = [
        _playlist_track_with_state(clean_name, track, "available", state_cache)
        for track in (rows.get("matched") or [])
    ]
    missing = [
        _playlist_track_with_state(clean_name, track, "missing", state_cache)
        for track in (rows.get("missing") or [])
    ]
    manifest = _playlist_read_manifest(clean_name)
    manifest_tracks = _playlist_clean_track_list(manifest.get("desired_tracks") or [])
    checkpoint = _playlist_latest_job_state_summary(clean_name)
    tombstones = _playlist_tombstone_rows(manifest)
    removed = [
        _playlist_track_with_state(clean_name, row, _s(row.get("status") or "removed"), state_cache)
        for row in tombstones
    ]
    counts = _playlist_pipeline_counts(clean_name, matched, missing)
    checkpoint = _playlist_visible_checkpoint_summary({
        "missing_count": len(missing),
        "total": len(tracks),
        "downloaded": counts.get("downloaded", 0) + counts.get("waiting_import", 0) + counts.get("importing", 0),
        "counts": counts,
    }, checkpoint)
    last_plex = manifest.get("last_plex") if isinstance(manifest.get("last_plex"), dict) else {}
    last_pipeline = manifest.get("last_pipeline") if isinstance(manifest.get("last_pipeline"), dict) else {}
    return {
        "ok": True,
        "name": clean_name,
        "m3u": m3u,
        "manifest": str(_playlist_manifest_path(clean_name, allocate=False)) if _playlist_manifest_exists_no_create(clean_name) else "",
        "manifest_tracks": len(manifest_tracks),
        "m3u_tracks": int(rows.get("m3u_tracks") or 0),
        "desired_source": _s(rows.get("desired_source") or "m3u"),
        "tracks": tracks,
        "matched": matched,
        "missing": missing,
        "removed_excluded": removed,
        "counts": counts,
        "source": _s(manifest.get("source") or "local_m3u"),
        "source_content": _s(manifest.get("content") or ""),
        "last_plex": last_plex,
        "last_pipeline": last_pipeline,
        "last_sync_status": _s(last_plex.get("status") or "not_run"),
        "available": len(matched),
        "missing_count": len(missing),
        "total": len(tracks),
        "detail_mode": "full",
        "tracks_loaded": True,
        **checkpoint,
    }


def _playlist_desired_or_m3u_tracks(clean_name: str,
                                    index: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    desired_tracks, desired_source = _playlist_desired_tracks_for_name(clean_name, min_tracks=0)
    if desired_tracks:
        return desired_tracks, desired_source
    return _playlist_m3u_track_rows(clean_name, index)[0], "m3u"


def _playlist_apply_manifest_replacements(clean_name: str,
                                          replacements: List[Dict[str, Any]],
                                          *,
                                          index: Optional[Dict[str, Any]] = None,
                                          source_label: str = "manual") -> Dict[str, Any]:
    index = index or _playlist_library_index()
    desired_tracks, desired_source = _playlist_desired_or_m3u_tracks(clean_name, index)
    manifest = _playlist_read_manifest(clean_name)
    next_tracks = list(desired_tracks)
    resolved_rows: List[Dict[str, Any]] = []
    not_found: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for entry in replacements:
        original = entry.get("track") if isinstance(entry.get("track"), dict) else {}
        replacement = entry.get("replacement") if isinstance(entry.get("replacement"), dict) else {}
        artist = _playlist_clean_video_text(replacement.get("artist") or entry.get("artist") or "")
        title = _playlist_clean_video_text(replacement.get("title") or entry.get("title") or "")
        if not title:
            errors.append({"track": original, "error": "A replacement title is required"})
            continue
        target_keys = _playlist_manifest_match_keys(original)
        found_index = -1
        for idx, track in enumerate(next_tracks):
            if target_keys and target_keys & _playlist_manifest_match_keys(track):
                found_index = idx
                break
        if found_index < 0:
            not_found.append(original)
            continue

        original_row = _playlist_track_manifest_payload(next_tracks[found_index])
        resolved = {
            "artist": artist,
            "title": title,
            "source_artist": original_row.get("source_artist") or original_row.get("artist") or "",
            "source_title": original_row.get("source_title") or original_row.get("title") or "",
            "canonicalized": True,
            "canonical_source": source_label,
        }
        if original_row.get("path"):
            resolved["path"] = original_row.get("path")
        next_tracks[found_index] = resolved
        resolved_rows.append(_playlist_track_manifest_payload(resolved))

    if resolved_rows:
        next_tracks = _playlist_merge_desired_tracks(next_tracks)
        _playlist_write_manifest(
            clean_name,
            next_tracks,
            source=f"{desired_source}:{source_label}",
            content=_s(manifest.get("content") or ""),
        )

    detail = _playlist_detail_payload(clean_name, index)
    return {
        **detail,
        "updated": bool(resolved_rows),
        "resolved": resolved_rows,
        "resolved_count": len(resolved_rows),
        "not_found": not_found,
        "errors": errors,
    }


def _playlist_suggestion_key(artist: str, title: str, source: str) -> tuple:
    return (_norm(artist), _norm(title), _s(source).strip().lower())


def _playlist_add_suggestion(out: List[Dict[str, Any]],
                             seen: set,
                             *,
                             artist: str,
                             title: str,
                             source: str,
                             confidence: float,
                             safe: bool,
                             reason: str,
                             extra: Optional[Dict[str, Any]] = None) -> None:
    artist = _playlist_clean_video_text(artist)
    title = _playlist_clean_video_text(title)
    if not title:
        return
    key = _playlist_suggestion_key(artist, title, source)
    if key in seen:
        return
    seen.add(key)
    row = {
        "artist": artist,
        "title": title,
        "source": source,
        "confidence": round(max(0.0, min(1.0, float(confidence or 0))), 3),
        "safe": bool(safe),
        "reason": reason,
    }
    if extra:
        row.update(extra)
    out.append(row)


def _playlist_suggestions_for_track(track: Dict[str, Any],
                                    index: Dict[str, Any],
                                    *,
                                    include_musicbrainz: bool = True,
                                    limit: int = 5) -> List[Dict[str, Any]]:
    row = _playlist_track_manifest_payload(track)
    artist = _s(row.get("artist") or "").strip()
    title = _s(row.get("title") or "").strip()
    suggestions: List[Dict[str, Any]] = []
    seen: set = set()

    match = _match_track(artist, title)
    if match:
        item, score = match
        cand_artist = _s(getattr(item, "artist", "")).strip()
        cand_title = _s(getattr(item, "title", "")).strip()
        title_score = _playlist_title_score(title, cand_title)
        artist_score = _playlist_artist_name_score(artist, cand_artist) if artist else 1.0
        confidence = min(1.0, float(score or 0))
        _playlist_add_suggestion(
            suggestions,
            seen,
            artist=cand_artist,
            title=cand_title,
            source="beets",
            confidence=confidence,
            safe=confidence >= 0.94 and title_score >= 0.92 and artist_score >= 0.82,
            reason=f"Beets library match ({round(confidence * 100)}%)",
            extra={
                "item_id": int(getattr(item, "id", 0) or 0),
                "album": _s(getattr(item, "album", "")).strip(),
                "title_score": round(title_score, 3),
                "artist_score": round(artist_score, 3),
            },
        )

    title_key = _norm(title)
    if title_key:
        for cand in (index.get("by_title") or {}).get(title_key, [])[:8]:
            cand_artist = _s(cand.get("artist") or cand.get("albumartist") or "").strip()
            cand_title = _s(cand.get("title") or "").strip()
            artist_score = _playlist_artist_name_score(artist, cand_artist) if artist else _playlist_payload_rank(cand)
            confidence = min(1.0, max(0.0, artist_score))
            _playlist_add_suggestion(
                suggestions,
                seen,
                artist=cand_artist,
                title=cand_title,
                source="beets-title",
                confidence=confidence,
                safe=confidence >= 0.9,
                reason=f"Same title in Beets ({round(confidence * 100)}% artist)",
                extra={
                    "item_id": int(cand.get("id") or 0),
                    "album": _s(cand.get("album") or "").strip(),
                    "artist_score": round(artist_score, 3),
                },
            )

    if include_musicbrainz:
        for cand in _playlist_recording_search_candidates(title, artist)[:8]:
            cand_artist = _playlist_primary_artist_name(cand.get("artist") or "")
            cand_title = _s(cand.get("title") or "").strip()
            mb_score = max(0.0, min(1.0, float(cand.get("score") or 0) / 100.0))
            title_score = _playlist_title_score(title, cand_title)
            artist_score = _playlist_artist_name_score(artist, cand_artist) if artist else 1.0
            confidence = (mb_score * 0.45) + (title_score * 0.35) + (artist_score * 0.20)
            _playlist_add_suggestion(
                suggestions,
                seen,
                artist=cand_artist,
                title=cand_title,
                source="musicbrainz",
                confidence=confidence,
                safe=mb_score >= 0.95 and title_score >= 0.95 and artist_score >= 0.82,
                reason=f"MusicBrainz recording ({round(confidence * 100)}%)",
                extra={
                    "mb_trackid": _s(cand.get("mb_trackid") or "").strip(),
                    "mb_url": _s(cand.get("mb_url") or "").strip(),
                    "album": _s(cand.get("album") or "").strip(),
                    "year": _s(cand.get("year") or "").strip(),
                    "title_score": round(title_score, 3),
                    "artist_score": round(artist_score, 3),
                },
            )

    suggestions.sort(key=lambda s: (not bool(s.get("safe")), -float(s.get("confidence") or 0), s.get("source", "")))
    return suggestions[:max(1, min(int(limit or 5), 10))]


def _playlist_record_pipeline(name: str, **updates: Any) -> Dict[str, Any]:
    manifest = _playlist_read_manifest(name)
    current = dict(manifest.get("last_pipeline") or {})
    current.update(updates)
    if _s(updates.get("status") or "").lower() in {"running", "done"} and "error" not in updates:
        current.pop("error", None)
    current["updated_at"] = time.time()
    manifest["last_pipeline"] = current
    _playlist_replace_manifest(name, manifest)
    return current


def _playlist_run_source_sync(name: str, log: List[str]) -> Dict[str, Any]:
    clean_name = _clean_playlist_name(name)
    manifest = _playlist_read_manifest(clean_name)
    source = _s(manifest.get("source") or "local_m3u").strip().lower()
    content = _s(manifest.get("content") or "").strip()
    tracks: List[Dict[str, Any]] = []
    if content and source in {"url", "text", "spotify"}:
        log.append(f"Reading {source} playlist source")
        parsed = _json_from_flask_response(parse_playlist_request({"source": source, "content": content},
        ))
        if not parsed.get("ok"):
            raise RuntimeError(parsed.get("error") or "playlist source sync failed")
        tracks = list(parsed.get("tracks") or [])
    elif content and source == "local_m3u" and _norm(content) != _norm(clean_name):
        index = _playlist_library_index()
        tracks = _playlist_m3u_track_rows(_clean_playlist_name(content), index)[0]
        log.append(f"Reading local M3U playlist source: {content}")
    else:
        index = _playlist_library_index()
        tracks = list(manifest.get("desired_tracks") or [])
        if not tracks:
            tracks = _playlist_m3u_track_rows(clean_name, index)[0]
        source = source if source not in {"", "manual"} else "local_m3u"
        log.append("Reading local M3U playlist source")

    tracks = _playlist_apply_tombstones(clean_name, tracks, manifest)
    index = _playlist_library_index()
    matched, missing = _playlist_match_reference_tracks(tracks, index)
    _playlist_write_manifest(
        clean_name,
        tracks,
        matched_tracks=matched,
        missing_tracks=missing,
        source=source,
        content=content,
        log=log,
    )
    for track in matched:
        current = _playlist_state_for_track(clean_name, track)
        if _s(current.get("status") or "") not in {"imported", "plex_synced"}:
            _playlist_store_track_state(
                clean_name, track, "available",
                message="available in Beets after source sync",
                path=_s(track.get("path") or ""))
    for track in missing:
        _playlist_store_track_state(
            clean_name, track, "missing",
            message="not found in Beets after source sync")
    detail = _playlist_write_local_membership(clean_name, _playlist_read_manifest(clean_name))
    log.append(
        f"Source sync complete: {detail.get('total', 0)} desired, "
        f"{detail.get('available', 0)} available, {detail.get('missing_count', 0)} missing"
    )
    return detail


def _playlist_run_reconcile_state(name: str, log: List[str]) -> Dict[str, Any]:
    clean_name = _clean_playlist_name(name)
    detail = _playlist_detail_payload(clean_name)
    missing_tracks = list(detail.get("missing") or [])
    manifest = _playlist_read_manifest(clean_name)
    playlist_id = _s(manifest.get("playlist_id") or "").strip()
    reconciled = _playlist_reconcile_staged_files(
        clean_name, Path(), missing_tracks, log, playlist_id=playlist_id)
    refreshed = _playlist_write_local_membership(clean_name, manifest)
    log.append(
        f"Reconcile state complete: {reconciled} staged/checkpoint update(s), "
        f"{refreshed.get('available', 0)} available, {refreshed.get('missing_count', 0)} missing"
    )
    return refreshed


def _playlist_staged_entries(name: str,
                            *,
                            playlist_id: str = "") -> List[Tuple[Dict[str, Any], Path]]:
    clean_name = _clean_playlist_name(name)
    detail = _playlist_detail_payload(clean_name)
    tracks = list(detail.get("missing") or []) + list(detail.get("tracks") or [])
    by_key = {_playlist_status_id(track): track for track in tracks}
    rows: Dict[str, Dict[str, Any]] = dict(_playlist_manifest_track_states(clean_name, playlist_id=playlist_id or None))
    for state in _playlist_saved_job_states_for_name(clean_name, playlist_id=playlist_id, mark_interrupted=True):
        for key, row in (state.get("track_statuses") or {}).items():
            if isinstance(row, dict) and row.get("path"):
                rows.setdefault(key, row)
    entries: List[Tuple[Dict[str, Any], Path]] = []
    seen_paths: set = set()
    for key, row in rows.items():
        if not isinstance(row, dict):
            continue
        status = _s(row.get("status") or "").lower()
        if status not in {"downloaded", "waiting_import", "importing", "failed"}:
            continue
        raw_path = _s(row.get("staged_path") or row.get("path") or "").strip()
        if not raw_path:
            continue
        try:
            inspected = _playlist_inspect_staged_file(
                clean_name, key, raw_path, playlist_id=playlist_id)
        except BeetsUnavailableError:
            continue
        except Exception:
            continue
        if not (
            isinstance(inspected, dict)
            and inspected.get("ok")
            and inspected.get("exists")
            and inspected.get("authorized")
        ):
            continue
        path_key = raw_path.replace("\\", "/").casefold()
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        track = by_key.get(key) or {
            "artist": _s(row.get("artist") or ""),
            "title": _s(row.get("title") or ""),
            "album": _s(row.get("album") or ""),
            "albumartist": _s(row.get("albumartist") or ""),
            "year": row.get("year") or 0,
            "mb_trackid": _s(row.get("mb_trackid") or ""),
            "id": key,
        }
        entries.append((track, Path(raw_path)))
    return entries


def _enrich_playlist_file_tags(path: Path, track: Dict[str, Any], log: List[str]) -> None:
    """Write safe playlist singleton tags before beet import via control agent API."""
    try:
        cur_tags = _read_file_media_tags(str(path))
        artist = _s(track.get("artist") or "")
        title = _s(track.get("title") or "")
        tags_to_write = {}

        if artist:
            if not (cur_tags.get("artist") or "").strip():
                tags_to_write["artist"] = artist
            if not (cur_tags.get("albumartist") or "").strip():
                tags_to_write["albumartist"] = artist

        if title and not (cur_tags.get("title") or "").strip():
            tags_to_write["title"] = title

        current_album = (cur_tags.get("album") or "").strip()
        year_only_album = bool(re.match(r'^\d{4}$', current_album))
        bad_album = _playlist_album_value_is_bad_fallback(current_album)
        if not current_album or year_only_album or bad_album:
            tags_to_write["album"] = title or path.stem
            if artist and not (cur_tags.get("albumartist") or "").strip():
                tags_to_write["albumartist"] = artist
            try:
                if int(cur_tags.get("year", 0) or 0) > 0:
                    tags_to_write["year"] = 0
            except Exception:
                pass
            if bad_album:
                log.append(
                    f"  [tag] Rejected metadata: provider name was incorrectly used as album ({current_album})"
                )

        if tags_to_write:
            composite_workflows.write_tags(str(path), tags_to_write)
    except Exception as exc:
        log.append(f"  [tag] Error enriching playlist file tags: {exc}")
        log.append(f"  [tag] Warning: could not enrich tags on {path.name}: {exc}")


def _playlist_run_import_downloaded(name: str,
                                    log: List[str],
                                    cancel_event=None,
                                    playlist_id: str = "") -> Dict[str, Any]:
    clean_name = _clean_playlist_name(name)
    pid = _playlist_resolve_stable_id(clean_name, playlist_id=playlist_id or None) or playlist_id
    key = _playlist_key(clean_name, playlist_id=pid or None, allocate=False)

    entries = _playlist_staged_entries(clean_name, playlist_id=pid)
    if not entries:
        raise RuntimeError("No downloaded playlist staging files are ready to import")

    tracks_payload = []
    imported_tracks = []
    operation_material = []
    for track, source_path in entries:
        track_id = _playlist_status_id(track)
        raw_source = str(source_path)
        _playlist_store_track_state(
            clean_name, track, "waiting_import",
            playlist_id=pid,
            message="waiting for engine-side staged media verification and Beets import",
            staged_path=raw_source, path=raw_source)
        imported_tracks.append(track)
        operation_material.append(f"{track_id}:{raw_source}")
        tracks_payload.append({
            "track_id": track_id,
            "staged_path": raw_source,
            "artist": _s(track.get("artist") or ""),
            "title": _s(track.get("title") or ""),
            "album": _s(track.get("album") or track.get("title") or ""),
            "albumartist": _s(track.get("artist") or ""),
            "year": track.get("year") or 0,
            "mb_trackid": _s(track.get("mb_trackid") or ""),
        })

    if not imported_tracks:
        raise RuntimeError("No downloaded playlist staging files are ready for engine verification")

    operation_digest = hashlib.sha256(
        (f"{pid}|{key}|" + "|".join(sorted(operation_material))).encode("utf-8", "surrogatepass")
    ).hexdigest()[:32]
    operation_id = f"pl-import-{operation_digest}"
    for track in imported_tracks:
        _playlist_store_track_state(
            clean_name, track, "waiting_import",
            playlist_id=pid,
            import_operation_id=operation_id)
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("playlist import stopped by user")

    log.append(f"Starting engine Beets singleton import for {len(imported_tracks)} track(s)...")
    import_started_at = time.time()
    try:
        res = composite_workflows.import_playlist_staged(
            key, pid, tracks_payload, operation_id=operation_id
        )
    except Exception as exc:
        log.append(f"  [playlist] Engine import handoff failed: {exc}")
        for track in imported_tracks:
            _playlist_store_track_state(
                clean_name, track, "failed",
                message=f"Engine import handoff failed: {exc}",
                failure_reason="import_failed")
        raise RuntimeError("engine_unavailable") from exc

    if not (isinstance(res, dict) and res.get("ok")):
        err_msg = res.get("error") or res.get("message") or "Engine playlist import failed"
        log.append(f"  [playlist] Engine import failed: {err_msg}")
        for track in imported_tracks:
            _playlist_store_track_state(
                clean_name, track, "failed",
                message=f"Engine import failed: {err_msg}",
                failure_reason="import_failed")
        raise RuntimeError(res.get("error_code") or "import_failed")

    beets_res = res.get("beets") if isinstance(res.get("beets"), dict) else {}
    beets_message = _s(beets_res.get("message") or "")
    if beets_message:
        log.append("  " + beets_message)

    engine_imported_tracks = []
    track_results = res.get("tracks") if isinstance(res.get("tracks"), list) else []
    for trk_res in track_results:
        if not isinstance(trk_res, dict):
            continue
        tid = _s(trk_res.get("track_id"))
        status = _s(trk_res.get("status"))
        matched_trk = next((t for t in imported_tracks if _playlist_status_id(t) == tid), {"id": tid})
        if status in {"imported", "already_imported"}:
            engine_imported_tracks.append(matched_trk)
            _playlist_store_track_state(
                clean_name, matched_trk, "imported",
                playlist_id=pid,
                message="imported into Beets library by engine",
                failure_reason="",
                staged_path="")
        elif status in {"cross_playlist_target", "staged_track_symlink", "staged_track_not_audio", "staged_track_missing"}:
            _playlist_store_track_state(
                clean_name, matched_trk, "failed",
                playlist_id=pid,
                message=_s(trk_res.get("error") or "engine import refused track"),
                failure_reason=status)

    placed_tracks = engine_imported_tracks or [
        track for track in imported_tracks
        if any(isinstance(row, dict) and _s(row.get("track_id")) == _playlist_status_id(track) and _s(row.get("status")) in {"imported", "already_imported"} for row in track_results)
    ]
    _invalidate_lib_cache()
    placement = _playlist_place_recent_imports_for_tracks(
        placed_tracks, import_started_at - 10, log=log, cancel_event=cancel_event)
    _invalidate_lib_cache()

    detail = _playlist_detail_payload(clean_name)
    matched_by_key = {
        _playlist_status_id(track): track
        for track in (detail.get("matched") or [])
    }
    matched_keys = set(matched_by_key.keys())
    placement_results = [row for row in (placement.get("results") or []) if isinstance(row, dict)]
    placed_keys = {
        _s(row.get("playlist_track_id") or "")
        for row in placement_results
        if row.get("repaired")
    }
    review_keys = {
        _s(row.get("playlist_track_id") or "")
        for row in placement_results
        if row.get("playlist_track_id") and not row.get("repaired")
    }
    placement_failed = int(placement.get("failed") or 0) > 0
    for track in placed_tracks:
        track_key = _playlist_status_id(track)
        if track_key in matched_keys:
            if track_key in review_keys or (placement_failed and track_key not in placed_keys):
                failed_result = next(
                    (row for row in placement_results if _s(row.get("playlist_track_id") or "") == track_key),
                    {},
                )
                reason = _s(
                    failed_result.get("reason")
                    or "MusicBrainz placement did not produce a validated album-artist path"
                )
                _playlist_store_track_state(
                    clean_name, track, "review_required",
                    message=f"imported, but playlist placement needs review: {reason}",
                    failure_reason=reason,
                    final_path=_s(failed_result.get("final_path") or ""),
                    expected_path=_s(failed_result.get("expected_path") or ""),
                    staged_path="")
            else:
                matched_row = matched_by_key.get(track_key) or {}
                if track_key not in placed_keys:
                    path_check = _playlist_validate_final_album_path(
                        _s(matched_row.get("path") or ""),
                        {
                            "albumartist": matched_row.get("albumartist", ""),
                            "album": matched_row.get("album", ""),
                            "year": matched_row.get("year", ""),
                            "mb_releasegroupid": matched_row.get("mb_releasegroupid", ""),
                            "mb_albumartistid": matched_row.get("mb_albumartistid", ""),
                            "mb_albumartistids": matched_row.get("mb_albumartistids", ""),
                        },
                        log=log,
                    )
                    if not path_check.get("ok"):
                        reason = _s(
                            path_check.get("reason")
                            or "MusicBrainz placement did not produce a validated album-artist path"
                        )
                        _playlist_store_track_state(
                            clean_name, track, "review_required",
                            message=f"imported, but playlist placement needs review: {reason}",
                            failure_reason=reason,
                            final_path=_s(path_check.get("final_path") or matched_row.get("path") or ""),
                            expected_path=_s(path_check.get("expected_path") or ""),
                            staged_path="")
                        continue
                _playlist_store_track_state(
                    clean_name, track, "imported",
                    message="imported into Beets and placed in the library",
                    failure_reason="", staged_path="")
                log.append(
                    "Imported successfully: "
                    f"{track.get('artist') or track.get('query_artist') or ''} - "
                    f"{track.get('title') or track.get('query_title') or ''}"
                )
        else:
            _playlist_store_track_state(
                clean_name, track, "failed",
                message="Beets import completed but the track was not found in the library",
                failure_reason="import failed", staged_path="")
    final_detail = _playlist_write_local_membership(clean_name, _playlist_read_manifest(clean_name))
    if "imported_count" in res:
        imported_files = int(res.get("imported_count") or 0)
    else:
        imported_files = len(placed_tracks)
    return {
        "playlist": final_detail,
        "imported_files": imported_files,
        "placement": placement,
    }


def _playlist_sync_items_from_m3u(clean_name: str) -> List[Dict[str, Any]]:
    key = _playlist_existing_key(clean_name)
    if not key:
        return []
    try:
        res = composite_workflows.read_playlist_m3u(key, fallback_name=_clean_playlist_name(clean_name))
    except Exception:
        return []
    if not (isinstance(res, dict) and res.get("ok") and res.get("exists")):
        return []
    out: List[Dict[str, Any]] = []
    for item in res.get("items") or []:
        if not isinstance(item, dict):
            continue
        line = _s(item.get("path") or "").strip()
        if not line or not _plex_is_final_library_path(line):
            continue
        resolved = _playlist_resolve_item_path(line)
        out.append({
            "artist": _s(item.get("artist") or ""),
            "title": _s(item.get("title") or Path(line).stem),
            "path": str(resolved),
            "source": "m3u",
        })
    return out


def _playlist_run_plex_sync(name: str, log: List[str]) -> Dict[str, Any]:
    clean_name = _clean_playlist_name(name)
    log.append("Preparing Plex sync from saved final library paths")
    matched = _playlist_sync_items_from_m3u(clean_name)
    detail: Dict[str, Any] = {}
    if not matched:
        log.append("Saved M3U had no final library paths; checking Beets detail")
        detail = _playlist_detail_payload(clean_name)
        matched = [
            track for track in (detail.get("matched") or [])
            if _s(track.get("pipeline_status") or "available") != "review_required"
        ]
    if not matched:
        raise RuntimeError("No Beets library tracks are available to sync to Plex")
    manifest = _playlist_read_manifest(clean_name)
    settings = _plex_settings()
    try:
        wait_for_plex = int(settings.get("plex_scan_timeout") or PLEX_SCAN_TIMEOUT)
    except Exception:
        wait_for_plex = PLEX_SCAN_TIMEOUT
    result = _create_playlist_outputs(
        clean_name,
        matched,
        log=log,
        replace_plex=True,
        wait_for_plex_seconds=wait_for_plex,
        require_full_plex=False,
        desired_tracks=manifest.get("desired_tracks") or detail.get("tracks") or [],
        missing_tracks=detail.get("missing") or [],
        source=_s(manifest.get("source") or ""),
        content=_s(manifest.get("content") or ""),
        sync_plex=True,
    )
    plex = result.get("plex") or {}
    if plex.get("tracks_unmatched"):
        if plex.get("status") == "partial_success" and int(plex.get("tracks_matched") or 0) > 0:
            log.append(
                "Plex sync partially completed: "
                f"{plex.get('tracks_matched', 0)} of {plex.get('tracks_requested', 0)} tracks added; "
                f"{plex.get('tracks_unmatched')} pending Plex match(es)."
            )
        else:
            log.append(
                "Plex sync issue: "
                f"{plex.get('tracks_unmatched')} missing in Plex; "
                f"{plex.get('matched_by_path', 0)} matched by path, "
                f"{plex.get('matched_by_fallback', 0)} matched by fallback; "
                f"section={plex.get('section_title') or plex.get('section_key')}; "
                f"mapping={plex.get('beets_music_root') or str(MUSIC_ROOT)} -> "
                f"{', '.join(plex.get('plex_music_roots') or []) or '(none)'}"
            )
        examples = plex.get("missing_examples") if isinstance(plex.get("missing_examples"), list) else []
        if examples:
            labels = [
                f"{_s(row.get('artist'))} - {_s(row.get('title'))}".strip(" -")
                for row in examples[:5] if isinstance(row, dict)
            ]
            if labels:
                log.append("Plex missing examples: " + "; ".join(labels))
    return {"playlist": {"ok": True, "name": clean_name}, **result}


def _playlist_start_direct_action(name: str, action: str) -> Dict[str, Any]:
    clean_name = _clean_playlist_name(name)
    running_job_id = _playlist_running_pipeline_job_id(clean_name)
    if running_job_id:
        raise RuntimeError(_PLAYLIST_DUPLICATE_JOB_MESSAGE)

    def _do(log, cancel_event=None):
        runtime_lock = _playlist_pipeline_runtime_lock(clean_name)
        if not runtime_lock.acquire(blocking=False):
            raise RuntimeError(_PLAYLIST_DUPLICATE_JOB_MESSAGE)
        _playlist_record_pipeline(clean_name, action=action, status="running")
        contract = None
        try:
            contract = job_contract.enter("playlist-" + job_contract.slug(clean_name), log=log,
                                          cancel_event=cancel_event)
            if action == "sync_sources":
                result = _playlist_run_source_sync(clean_name, log)
            elif action == "reconcile_state":
                result = _playlist_run_reconcile_state(clean_name, log)
            elif action == "import_downloaded":
                result = _playlist_run_import_downloaded(clean_name, log, cancel_event=cancel_event, playlist_id=_s(_playlist_read_manifest(clean_name).get("playlist_id") or ""))
            elif action == "sync_plex":
                result = _playlist_run_plex_sync(clean_name, log)
            else:
                raise RuntimeError(f"Unsupported playlist pipeline action: {action}")
            _playlist_record_pipeline(clean_name, action=action, status="done")
            return result
        except Exception as ex:
            _playlist_record_pipeline(clean_name, action=action, status="failed", error=str(ex))
            raise
        finally:
            if contract is not None:
                contract.close()
            runtime_lock.release()

    label = {
        "sync_sources": "Playlist source sync",
        "reconcile_state": "Playlist state reconcile",
        "import_downloaded": "Playlist import downloaded",
        "sync_plex": "Playlist Plex sync",
    }[action]
    with _PLAYLIST_PIPELINE_START_GUARD:
        running_job_id = _playlist_running_pipeline_job_id(clean_name)
        if running_job_id:
            raise RuntimeError(_PLAYLIST_DUPLICATE_JOB_MESSAGE)
        job = jobs.start_python(
            _do,
            label=f"{label}: {clean_name}",
            metadata={"type": "playlist-pipeline", "action": action, "name": clean_name},
        )
        _playlist_record_pipeline(
            clean_name,
            action=action,
            status="running",
            jobs_job_id=job.job_id,
            playlist_job_id="",
        )
    return {"ok": True, "job_id": job.job_id, "jobs_job_id": job.job_id, "action": action}


def _playlist_start_download_action(name: str,
                                    action: str,
                                    retry_tracks: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    clean_name = _clean_playlist_name(name)
    running_job_id = _playlist_running_pipeline_job_id(clean_name)
    if running_job_id:
        raise RuntimeError(_PLAYLIST_DUPLICATE_JOB_MESSAGE)
    detail = _playlist_detail_payload(clean_name)
    manifest = _playlist_read_manifest(clean_name)
    all_tracks = list(detail.get("tracks") or [])
    missing = list(retry_tracks or detail.get("missing") or [])
    source = _s(manifest.get("source") or "").strip()
    content = _s(manifest.get("content") or "").strip()
    playlist_id = _s(manifest.get("playlist_id") or detail.get("playlist_id") or "").strip()
    if not _playlist_valid_internal_id(playlist_id):
        playlist_id = _playlist_resolve_stable_id(clean_name)
    if not _playlist_valid_internal_id(playlist_id):
        raise PlaylistStateError("playlist_identity_unresolved", "Persistent playlist_id is required for playlist resume.")
    full = action in {"run_full", "resume"}
    if action == "resume":
        saved_states = _playlist_saved_job_states_for_name(clean_name, playlist_id=playlist_id, mark_interrupted=True, strict=True)
        if saved_states:
            latest = max(saved_states, key=_playlist_job_state_stamp)
            saved_key = latest.get("job_key") if isinstance(latest.get("job_key"), dict) else {}
            saved_tracks = saved_key.get("tracks") if isinstance(saved_key.get("tracks"), list) else []
            saved_requested = saved_key.get("requested") if isinstance(saved_key.get("requested"), list) else []
            all_tracks = list(saved_tracks or latest.get("tracks") or all_tracks)
            missing = list(saved_requested or latest.get("missing") or missing)
            source = _s(saved_key.get("source") or source).strip()
            content = _s(saved_key.get("content") or content).strip()
    refresh_from_source = bool(
        full
        and content
        and (source != "local_m3u" or _norm(content) != _norm(clean_name))
    )
    payload = {
        "name": clean_name,
        "playlist_id": playlist_id,
        "tracks": missing,
        "all_tracks": all_tracks,
        "source": source if refresh_from_source else "",
        "content": content if refresh_from_source else "",
        "download_only": action == "download_missing",
        "sync_after_import": full,
        "pipeline_action": "full" if full else "download_missing",
    }
    with _PLAYLIST_PIPELINE_START_GUARD:
        running_job_id = _playlist_running_pipeline_job_id(clean_name)
        if running_job_id:
            raise RuntimeError(_PLAYLIST_DUPLICATE_JOB_MESSAGE)
        response = start_playlist_download(payload)
        result = _json_from_flask_response(response)
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "Could not start playlist pipeline")
        _playlist_record_pipeline(
            clean_name,
            action="full" if full else "download_missing",
            status="running",
            jobs_job_id=_s(result.get("jobs_job_id") or ""),
            playlist_job_id=_s(result.get("job_id") or ""),
        )
    return {**result, "action": action}


def _playlist_auto_sync_worker():
    time.sleep(min(30, PLAYLIST_AUTO_SYNC_INTERVAL))
    while True:
        try:
            if PLAYLIST_AUTO_SYNC_ENABLED and _plex_settings().get("token"):
                log: List[str] = ["Starting automatic playlist two-way sync"]
                _playlist_sync_all_locked(log)
        except Exception:
            pass
        time.sleep(PLAYLIST_AUTO_SYNC_INTERVAL)


def _start_playlist_auto_sync_worker():
    if not PLAYLIST_AUTO_SYNC_ENABLED:
        return
    threading.Thread(target=_playlist_auto_sync_worker, daemon=True).start()


def _playlist_index_warm_worker():
    time.sleep(2)
    try:
        started = time.time()
        index = _playlist_library_index()
        by_text = len(index.get("by_text") or {})
        by_path = len(index.get("by_path") or {})
        print(f"[playlist] Warmed library index in {time.time() - started:.1f}s ({by_text} text keys, {by_path} path keys)", flush=True)
    except Exception as ex:
        print(f"[playlist] Library index warm-up failed: {ex}", flush=True)


def _start_playlist_index_warm_worker():
    threading.Thread(target=_playlist_index_warm_worker, daemon=True).start()


def playlist_sync_status_payload() -> Dict[str, Any]:
    """Playlist auto-sync status (GET /api/playlists/sync/status body); request-free (ARCH-001)."""
    state = dict(_PLAYLIST_SYNC_STATE)
    state["ok"] = True
    return state
