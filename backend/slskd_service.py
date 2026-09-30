"""Soulseek (slskd) provider: search, queue and download collection (ARCH-001).
"""

from __future__ import annotations

import backend.provider_boundary as provider_boundary
import difflib, json, math, os, re, time, uuid
import urllib.error
from backend.matching import strip_track_filename_id_suffix as _canonical_strip_track_filename_id_suffix
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional
from backend.app_runtime import AUDIO_EXT, DOWNLOADS_ROOT, SLSKD_URL, _MISSING_TRACK_FILE_MATCH_SCORE, _MISSING_TRACK_TITLE_FUZZY_SCORE, _s, _up, _ur
from backend.slskd import build_album_candidates as _slskd_build_album_candidates, cleanup_failed_candidate_files as _slskd_cleanup_failed_candidate_files_impl, file_remote_name as _slskd_file_remote_name, file_size as _slskd_file_size, slskd_download_candidate_roots as _slskd_download_candidate_roots_impl

# ── ARCH-001 extracted code ──


def _slskd_api_key_from_file() -> str:
    key_path = Path(os.environ.get("SLSKD_API_KEY_FILE", "/config/slskd_api_key"))
    try:
        value = key_path.read_text(encoding="utf-8", errors="ignore").strip()
        return value.splitlines()[0].strip() if value else ""
    except Exception:
        return ""


SLSKD_API_KEY = os.environ.get("SLSKD_API_KEY", "").strip() or _slskd_api_key_from_file()


# ── slskd + yt-dlp Album Download ─────────────────────────────────────────────

def _slskd_req(method: str, path: str, body=None) -> Any:
    """Thin wrapper for slskd REST API using the API key."""
    if not SLSKD_API_KEY:
        raise RuntimeError(
            "slskd API key is not configured; set SLSKD_API_KEY or /config/slskd_api_key"
        )
    url  = f"{SLSKD_URL}/api/v0/{path.lstrip('/')}"
    hdrs = {"X-API-Key": SLSKD_API_KEY, "Accept": "application/json"}
    data = None
    if body is not None:
        hdrs["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = _ur.Request(url, data=data, headers=hdrs, method=method)
    try:
        with provider_boundary.opened("slskd", req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"slskd {method} /{path} → HTTP {e.code}: "
            f"{e.read()[:300].decode('utf-8', 'replace')}")


def _normalise_wanted_tracks(raw) -> List[Dict[str, Any]]:
    """Return a compact list of wanted MB tracks from UI/API payloads."""
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if isinstance(raw, dict):
        raw = raw.get("missing") or raw.get("tracks") or raw.get("wanted") or []
    if not isinstance(raw, list):
        return []

    tracks: List[Dict[str, Any]] = []
    seen: set = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            disc = int(item.get("disc") or item.get("medium") or 1)
        except Exception:
            disc = 1
        try:
            track = int(item.get("track") or item.get("position") or item.get("number") or 0)
        except Exception:
            track = 0
        title = _s(item.get("title") or item.get("name") or "").strip()
        mb_trackid = _s(item.get("mb_trackid") or item.get("recording_id") or "").strip().lower()
        if not track and not title and not mb_trackid:
            continue
        key = (disc, track, _slskd_title_norm(title), mb_trackid)
        if key in seen:
            continue
        seen.add(key)
        tracks.append({
            "disc": max(disc, 1),
            "track": max(track, 0),
            "title": title,
            "mb_trackid": mb_trackid,
        })
    tracks.sort(key=lambda t: (int(t.get("disc") or 1), int(t.get("track") or 0), t.get("title", "")))
    return tracks


def _slskd_title_norm(value: str) -> str:
    text = _strip_track_filename_id_suffix(value).casefold().replace("&", " and ")
    text = re.sub(r"\b(?:feat|ft)\.?\s+.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*[\(\[]\s*(?:feat\.?|ft\.?|with|prod\.?|produced\s+by|remix|edit|version|bonus|clean|explicit).*?[\)\]]\s*", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _slskd_track_numbers_from_name(name: str) -> List[tuple]:
    stem = _strip_track_filename_id_suffix(Path(_s(name).replace("\\", "/")).stem)
    nums: List[tuple] = []

    def _add(disc: int, track: int) -> None:
        if track <= 0:
            return
        val = (max(disc, 0), track)
        if val not in nums:
            nums.append(val)

    m = re.match(r"^\s*(\d{1,2})[\s._-]+(\d{1,3})(?=$|[\s.-])", stem)
    if m:
        _add(int(m.group(1)), int(m.group(2)))
    m = re.match(r"^\s*(\d{1,3})", stem)
    if m:
        raw_num = m.group(1)
        rest = stem[m.end(1):]
        if len(raw_num) >= 2 or not rest.strip() or re.match(r"^\s*[-_.]", rest):
            _add(0, int(raw_num))
    # Scene-style filenames often embed the track number after artist/album:
    # "2 Chainz-Based On A T.R.U. Story (Deluxe)-02-Crack".
    for m in re.finditer(r"(?:^|[\s._-])(\d{1,3})\s*[-_.]\s*(?=\D)", stem):
        if m.start(1) == 0 or len(m.group(1)) < 2:
            continue
        _add(0, int(m.group(1)))
    return nums


def _slskd_title_guess_from_name(name: str) -> str:
    # Cap input length before any regex work below: `name` can come from
    # untrusted Soulseek/slskd search results (no filesystem length limit
    # applies yet), and filenames are never legitimately this long.
    stem = _strip_track_filename_id_suffix(Path(_s(name)[:400].replace("\\", "/")).stem)
    scene_matches = [
        m for m in re.finditer(r"(?:^|[\s._-])(\d{1,3})\s*[-_.]\s*(?=\D)", stem)
        if m.start(1) > 0 and len(m.group(1)) >= 2
    ]
    filtered_scene_matches = []
    for m in scene_matches:
        prefix = stem[:m.start(1)]
        suffix = stem[m.end(1):]
        numeric_artist_alias = (
            re.match(r"^\s*\d{1,3}\s*[-_.]\s*$", prefix)
            and re.match(r"^[_.][A-Za-z][A-Za-z0-9_.]*[-–—]", suffix)
        )
        if not numeric_artist_alias:
            filtered_scene_matches.append(m)
    scene_matches = filtered_scene_matches
    if scene_matches:
        title = stem[scene_matches[-1].end():].strip(" -_.")
        if title:
            return title
    # Strip leading track/disc numbers without eating artist names like
    # "01-2_chainz-intro": that should become "2_chainz-intro" first, then
    # the scene artist prefix is removed below.
    stem = re.sub(r"^\s*\d{1,2}[\s._-]+\d{2,3}(?=[\s.-])[\s._-]+", "", stem)
    stem = re.sub(r"^\s*\d{1,3}\s*[\s._-]+\s*", "", stem)
    parts = [p.strip() for p in re.split(r"\s+-\s+", stem) if p.strip()]
    if len(parts) >= 2:
        stem = parts[-1]
    else:
        compact = stem.strip()
        m = re.match(r"^([a-z0-9]+(?:[_\.][a-z0-9]+){1,5})-(.+)$", compact, flags=re.IGNORECASE)
        if m:
            stem = m.group(2).strip()
        else:
            m = re.match(r"^([a-z0-9]+(?:[\s_\.]+[a-z0-9]+){1,5})-(.+)$", compact, flags=re.IGNORECASE)
            if m:
                stem = m.group(2).strip()
    return _strip_track_filename_id_suffix(stem)


def _wanted_track_key(track: Dict[str, Any]) -> tuple:
    mbid = _s(track.get("mb_trackid", "")).strip().lower()
    if mbid:
        return ("mbid", mbid)
    disc = int(track.get("disc") or 1)
    num = int(track.get("track") or 0)
    if num:
        return ("pos", disc, num)
    title = _slskd_title_norm(track.get("title", ""))
    return ("title", title) if title else ("unknown", "")


def _wanted_track_label(track: Dict[str, Any]) -> str:
    disc = int(track.get("disc") or 1)
    num = int(track.get("track") or 0)
    title = _s(track.get("title", "")).strip() or "Untitled"
    return f"{disc}.{num:02d} {title}" if num else title


def _slskd_file_wanted_match_score(remote_name: str,
                                   wanted_tracks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Score how strongly a remote Soulseek file matches the requested missing tracks."""
    wanted_tracks = _normalise_wanted_tracks(wanted_tracks)
    if not wanted_tracks:
        return {
            "ok": True,
            "score": 1.0,
            "reason": "no wanted-track filter",
            "track": {},
            "key": ("all", ""),
        }

    best: Dict[str, Any] = {
        "ok": False,
        "score": 0.0,
        "reason": "no wanted-track match",
        "track": {},
        "key": ("none", ""),
    }

    def _consider(score: float, reason: str, track: Dict[str, Any]) -> None:
        nonlocal best
        if score > float(best.get("score") or 0.0):
            best = {
                "ok": score >= _MISSING_TRACK_FILE_MATCH_SCORE,
                "score": round(score, 3),
                "reason": reason,
                "track": track,
                "key": _wanted_track_key(track),
            }

    file_title = _slskd_title_norm(_slskd_title_guess_from_name(remote_name))

    def _title_score(want_title: str) -> tuple:
        want_title = _slskd_title_norm(want_title)
        if not file_title or not want_title:
            return 0.0, "no title check"
        if file_title == want_title:
            return 0.98, "exact title"
        ratio = difflib.SequenceMatcher(None, file_title, want_title).ratio()
        if ratio >= _MISSING_TRACK_TITLE_FUZZY_SCORE:
            return ratio, f"title similarity {ratio:.0%}"
        shorter, longer = sorted((file_title, want_title), key=len)
        if len(shorter) >= 5 and shorter in longer:
            coverage = len(shorter) / max(1, len(longer))
            return max(0.86, min(0.93, coverage)), "title containment"
        return ratio, f"title mismatch {ratio:.0%}"

    nums = _slskd_track_numbers_from_name(remote_name)
    for disc, track_num in nums:
        for track in wanted_tracks:
            want_disc = int(track.get("disc") or 1)
            want_num = int(track.get("track") or 0)
            if not want_num or track_num != want_num:
                continue
            want_title = _slskd_title_norm(track.get("title", ""))
            title_score, title_reason = _title_score(want_title)
            if file_title and want_title and title_score < 0.70:
                continue
            if disc and disc == want_disc:
                score = 1.0 if not file_title or not want_title else max(0.92, min(1.0, title_score + 0.04))
                _consider(score, f"exact disc/track {want_disc}.{want_num:02d}, {title_reason}", track)
            elif not disc:
                score = 0.90 if not file_title or not want_title else max(0.86, min(0.94, title_score))
                _consider(score, f"track number {want_num:02d}, {title_reason}", track)

    if file_title:
        for track in wanted_tracks:
            want_title = _slskd_title_norm(track.get("title", ""))
            if not want_title:
                continue
            score, reason = _title_score(want_title)
            if score >= _MISSING_TRACK_TITLE_FUZZY_SCORE:
                _consider(score, reason, track)

    best["ok"] = float(best.get("score") or 0.0) >= _MISSING_TRACK_FILE_MATCH_SCORE
    return best


def _slskd_file_matches_wanted(remote_name: str, wanted_tracks: List[Dict[str, Any]]) -> bool:
    return bool(_slskd_file_wanted_match_score(remote_name, wanted_tracks).get("ok"))


def _wanted_tracks_remaining_after_satisfied(
    wanted_tracks: List[Dict[str, Any]],
    satisfied_tracks: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    wanted_tracks = _normalise_wanted_tracks(wanted_tracks)
    satisfied_keys = {_wanted_track_key(t) for t in _normalise_wanted_tracks(satisfied_tracks)}
    if not satisfied_keys:
        return wanted_tracks
    return [t for t in wanted_tracks if _wanted_track_key(t) not in satisfied_keys]


def _slskd_search_and_queue(artist: str, album: str, year: str,
                             log: list, track_count: int = 0,
                             wanted_tracks: Optional[List[Dict[str, Any]]] = None,
                             skip_candidates: Optional[set] = None,
                             busy_retries: int = 24):
    """
    Search slskd for artist/album, pick best response, queue downloads.
    Returns (username, [queued_remote_filenames], expected_local_dir).
    Raises RuntimeError on failure.
    """
    wanted_tracks = _normalise_wanted_tracks(wanted_tracks)
    skip_candidates = skip_candidates or set()
    search_text = f"{artist} {album}"
    search_id   = str(uuid.uuid4())
    log.append(f"  [slskd] Searching: {search_text!r}")

    # 1. Start search
    search_body = {
        "id": search_id,
        "searchText": search_text,
        "fileLimit": 500,
        # Do not let slskd's UI/search filters hide MP3 folders from the app.
        # The app validates track count and file type itself below.
        "filterResponses": False,
        "minimumResponseFileCount": 1,
    }
    for attempt in range(1, busy_retries + 1):
        try:
            _slskd_req("POST", "searches", search_body)
            break
        except RuntimeError as ex:
            msg = str(ex)
            busy = "HTTP 429" in msg and "Only one concurrent operation" in msg
            if not busy:
                raise
            if attempt >= busy_retries:
                raise RuntimeError(
                    f"slskd stayed busy and would not start a new search after "
                    f"{busy_retries} retry attempt(s). Wait for the current SLSKD operation to "
                    "finish, then retry."
                ) from ex
            if attempt == 1:
                log.append("  [slskd] Search is busy; waiting for the current SLSKD operation to finish...")
            elif attempt % 3 == 0:
                log.append(f"  [slskd] Still waiting for SLSKD search slot ({attempt}/{busy_retries})...")
            time.sleep(10)

    # 2. Poll until we have real response rows, not just a response count.
    #    slskd can expose responseCount before the response objects are
    #    available, then clear the response list when the search finalises.
    #    Cache rows from both includeResponses=true and /responses while the
    #    search is still alive.
    resp_count = 0
    _inline_responses: list = []

    def _normalise_search_responses(raw: Any) -> list:
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for key in ("responses", "results", "items"):
                rows = raw.get(key)
                if isinstance(rows, list):
                    return rows
        return []

    def _cache_responses(raw: Any) -> None:
        nonlocal _inline_responses
        rows = _normalise_search_responses(raw)
        if rows:
            _inline_responses = rows

    missing_rows_logged = False
    for _ in range(45):
        time.sleep(1)
        try:
            st = _slskd_req("GET", f"searches/{search_id}?includeResponses=true")
            resp_count = max(resp_count, int(st.get("responseCount") or 0))
            state = st.get("state", "").lower()
            _cache_responses(st)

            if resp_count and not _inline_responses:
                try:
                    _cache_responses(_slskd_req("GET", f"searches/{search_id}/responses") or [])
                except Exception:
                    pass
                if not _inline_responses and not missing_rows_logged:
                    log.append("  [slskd] Response count is available; waiting for response details...")
                    missing_rows_logged = True

            if _inline_responses and (state in ("completed", "stopped") or resp_count >= 5):
                break
        except Exception:
            break
    log.append(f"  [slskd] {resp_count} response(s) returned")
    if not resp_count:
        raise RuntimeError(f"No Soulseek results for '{search_text}'")

    # 3. Get responses — prefer dedicated endpoint, fall back to cached rows.
    responses = []
    for _ in range(8):
        try:
            responses = _normalise_search_responses(
                _slskd_req("GET", f"searches/{search_id}/responses") or []
            )
        except Exception:
            responses = []
        if responses or _inline_responses:
            break
        time.sleep(1)
    if not responses and _inline_responses:
        log.append(f"  [slskd] Using {len(_inline_responses)} cached inline responses")
        responses = _inline_responses
    if not responses:
        raise RuntimeError(
            f"Got {resp_count} matches but responses list is empty "
            "(search may have expired — try again)")

    # 4. Score every album-like directory in every response. Track-count
    # completeness dominates format: a complete 320 kbps MP3 album is valid.
    candidates, skipped = _slskd_build_album_candidates(
        responses, artist, album, year, track_count, AUDIO_EXT, skip_candidates
    )

    if not candidates:
        if skipped:
            log.append(f"  [slskd] Skipped {skipped} previously failed candidate(s)")
            raise RuntimeError("No remaining Soulseek candidates after previous failed attempts")
        raise RuntimeError("No audio files in Soulseek results")
    if skipped:
        log.append(f"  [slskd] Skipped {skipped} previously failed candidate(s)")

    for cand in candidates[:5]:
        slot = "free" if cand["resp"].get("hasFreeUploadSlot") else "locked"
        log.append(
            f"  [slskd] Candidate @{cand['username'] or '?'} — "
            f"{len(cand['files'])} file(s), {slot}, score {cand['score']}: {cand['dir']}"
        )

    best = candidates[0]
    best_score = best["score"]
    best_dir_remote = best["dir"]
    best_afiles = best["files"]
    best_resp = best["resp"]
    if best_score < 0:
        raise RuntimeError("No audio files in Soulseek results")
    if track_count and not wanted_tracks:
        min_files = max(1, min(track_count, int(track_count * 0.70)))
        if len(best_afiles) < min_files:
            counts = ", ".join(
                f"@{c['username'] or '?'}:{len(c['files'])}" for c in candidates[:5]
            )
            raise RuntimeError(
                f"Best Soulseek candidate only has {len(best_afiles)}/{track_count} "
                f"audio file(s); refusing incomplete album candidate"
                + (f" (top counts: {counts})" if counts else "")
            )

    username = best_resp.get("username", "")
    log.append(f"  [slskd] Best peer: @{username} — {len(best_afiles)} file(s) from '{best_dir_remote}' (score {best_score})")

    queue_afiles = best_afiles
    if wanted_tracks:
        wanted_desc = ", ".join(
            f"{int(t.get('disc') or 1)}.{int(t.get('track') or 0):02d}"
            for t in wanted_tracks if int(t.get("track") or 0)
        ) or f"{len(wanted_tracks)} track(s)"

        def _wanted_matches_for_candidate(cand: Dict[str, Any]) -> List[Dict[str, Any]]:
            chosen: Dict[tuple, Dict[str, Any]] = {}
            resp = cand["resp"]
            for f in cand["files"]:
                remote_name = _slskd_file_remote_name(f, resp)
                match = _slskd_file_wanted_match_score(remote_name, wanted_tracks)
                if not match.get("ok"):
                    continue
                key = match.get("key") or ("file", remote_name)
                current = chosen.get(key)
                if current and float(current["match"].get("score") or 0) >= float(match.get("score") or 0):
                    continue
                chosen[key] = {"file": f, "remote_name": remote_name, "match": match}
            selected = list(chosen.values())
            selected.sort(key=lambda row: (
                int((row["match"].get("track") or {}).get("disc") or 1),
                int((row["match"].get("track") or {}).get("track") or 0),
                -float(row["match"].get("score") or 0),
                Path(row["remote_name"]).name.lower(),
            ))
            return selected

        wanted_ranked: List[tuple] = []
        for cand in candidates:
            matches = _wanted_matches_for_candidate(cand)
            avg_score = (
                sum(float(m["match"].get("score") or 0) for m in matches) / len(matches)
                if matches else 0.0
            )
            has_free_slot = 1 if (cand.get("resp") or {}).get("hasFreeUploadSlot") else 0
            wanted_ranked.append((len(matches), has_free_slot, avg_score, cand["score"], cand, matches))
        wanted_ranked.sort(key=lambda item: (item[0], item[1], item[2], item[3]), reverse=True)

        best_wanted_count, _free_slot, best_wanted_avg, _cand_score, wanted_cand, wanted_matches = wanted_ranked[0]
        if best_wanted_count < 1:
            found = ", ".join(Path(m["remote_name"]).name for m in wanted_matches[:8])
            top_counts = ", ".join(
                f"@{row[4]['username'] or '?'}:{row[0]} match(es)"
                for row in wanted_ranked[:5]
            )
            raise RuntimeError(
                f"Best Soulseek candidate only matched {best_wanted_count}/{len(wanted_tracks)} "
                f"requested missing track(s) ({wanted_desc})"
                + (f": {found}" if found else "")
                + (f" (top wanted matches: {top_counts})" if top_counts else "")
            )

        matched_tracks = [m["match"].get("track") or {} for m in wanted_matches]
        remaining_tracks = _wanted_tracks_remaining_after_satisfied(wanted_tracks, matched_tracks)

        if wanted_cand is not best:
            best = wanted_cand
            best_score = best["score"]
            best_dir_remote = best["dir"]
            best_afiles = best["files"]
            best_resp = best["resp"]
            username = best_resp.get("username", "")
            log.append(
                f"  [slskd] Switched to @{username or '?'} for missing-track score: "
                f"{best_wanted_count}/{len(wanted_tracks)} requested track(s), "
                f"avg match {best_wanted_avg:.2f}"
            )

        queue_afiles = [m["file"] for m in wanted_matches]
        matched_desc = ", ".join(_wanted_track_label(t) for t in matched_tracks[:8])
        log.append(
            f"  [slskd] Filtering candidate to {len(queue_afiles)}/"
            f"{len(wanted_tracks)} requested missing track(s)"
            + (f": {matched_desc}" if matched_desc else f": {wanted_desc}")
        )
        if remaining_tracks:
            remaining_desc = ", ".join(_wanted_track_label(t) for t in remaining_tracks[:8])
            log.append(
                "  [slskd] Candidate is partial; unresolved requested track(s) "
                f"will remain eligible for fallback: {remaining_desc}"
            )
        for row in wanted_matches[:5]:
            mt = row["match"].get("track") or {}
            log.append(
                f"    [slskd] {int(mt.get('disc') or 1)}.{int(mt.get('track') or 0):02d} "
                f"{mt.get('title','') or Path(row['remote_name']).name} "
                f"({float(row['match'].get('score') or 0):.2f}, {row['match'].get('reason','')})"
            )

    # 5. Queue downloads — API expects an array of QueueDownloadRequest objects
    queued = []
    for f in queue_afiles:
        remote_name = _slskd_file_remote_name(f, best_resp)
        try:
            _slskd_req("POST",
                       f"transfers/downloads/{_up.quote(username, safe='')}",
                       [{"filename": remote_name, "size": _slskd_file_size(f)}])
            queued.append(remote_name)
        except Exception as ex:
            log.append(f"  [slskd] WARN: queue failed for {remote_name or '?'}: {ex}")
    if not queued:
        raise RuntimeError("Failed to queue any downloads from slskd")
    log.append(f"  [slskd] Queued {len(queued)} file(s)")

    # Compute expected local directory from the remote best_dir path
    # slskd saves to: DOWNLOADS_ROOT / username / remote_dir (with drive letter stripped)
    rdir = best_dir_remote.replace("\\", "/")
    if len(rdir) > 2 and rdir[1] == ":":
        rdir = rdir[2:]
    rdir = rdir.lstrip("/")
    expected_dir = str(DOWNLOADS_ROOT / username / rdir)
    return username, queued, expected_dir, best_dir_remote


def _slskd_download_candidate_roots(username: str, remote_files: list) -> List[Path]:
    """Return likely local roots for queued SLSKD remote files."""
    return _slskd_download_candidate_roots_impl(DOWNLOADS_ROOT, username, remote_files)


def _slskd_cancel_queued_downloads(username: str, remote_files: list, log: list) -> None:
    """Best-effort cancellation for a failed candidate before retrying another peer."""
    if not username or not remote_files:
        return
    cancelled = 0
    failed = 0
    for remote in remote_files:
        try:
            _slskd_req(
                "DELETE",
                f"transfers/downloads/{_up.quote(username, safe='')}/"
                f"{_up.quote(_s(remote), safe='')}?remove=true",
            )
            cancelled += 1
        except Exception:
            failed += 1
    if cancelled:
        log.append(f"  [slskd] Cancelled {cancelled} queued transfer(s) from failed candidate.")
    if failed:
        log.append(f"  [slskd] WARN: could not cancel {failed} queued transfer(s); continuing cleanup.")


def _slskd_cleanup_failed_candidate_files(username: str, remote_files: list, log: list) -> None:
    """Remove only queued audio files from a failed SLSKD candidate under downloads."""
    _slskd_cleanup_failed_candidate_files_impl(
        DOWNLOADS_ROOT, username, remote_files, AUDIO_EXT, log
    )


def _find_slskd_downloaded_files(username: str, remote_files: list,
                                 expected_dir: str, log: list,
                                 artist: str = "", album: str = "",
                                 track_count: int = 0,
                                 transfer_hints: Optional[List[Any]] = None,
                                 wanted_tracks: Optional[List[Dict[str, Any]]] = None) -> tuple:
    """Find downloaded SLSKD files by exact queued filenames.

    Never falls back to arbitrary recent files because that can import an
    unrelated failed_imports folder when slskd uses an unexpected layout.
    Returns (album_dir, audio_files).
    """
    audio_exts = {
        '.mp3', '.flac', '.m4a', '.ogg', '.opus', '.wav', '.ape', '.wv',
        '.aac', '.alac', '.aif', '.aiff',
    }
    expected = Path(expected_dir)

    queued_names = {
        Path(str(p).replace("\\", "/")).name.lower()
        for p in remote_files if str(p).strip()
    }
    queued_remote_lowers = {
        str(p).replace("\\", "/").lower()
        for p in remote_files if str(p).strip()
    }
    if not queued_names:
        return str(expected), []
    wanted_filter = _normalise_wanted_tracks(wanted_tracks)

    def _root_add(roots: List[Path], raw) -> None:
        if not raw:
            return
        try:
            p = Path(str(raw))
        except Exception:
            return
        if p not in roots:
            roots.append(p)

    def _dirs_from_transfer_result(result: Any) -> list:
        dirs: list = []
        if isinstance(result, list):
            for user_obj in result:
                if (user_obj.get("username") or "").lower() == username.lower():
                    dirs = user_obj.get("directories", [])
                    break
        elif isinstance(result, dict):
            dirs = result.get("directories", [])
        return dirs

    local_file_keys = (
            "localFilename", "localFileName", "localPath", "filePath",
            "downloadPath", "destination", "path",
    )
    local_dir_keys = (
            "localDirectory", "localDir", "downloadDirectory",
            "destinationDirectory", "path",
    )

    def _paths_from_transfer_dirs(dirs: list) -> List[Path]:
        hints: List[Path] = []
        for d in dirs:
            dvals = [_s(d.get(k, "")).strip() for k in local_dir_keys]
            for f in d.get("files", []) or []:
                fname = _s(f.get("filename", "")).replace("\\", "/")
                if Path(fname).name.lower() not in queued_names and fname.lower() not in queued_remote_lowers:
                    continue
                for key in local_file_keys:
                    raw = _s(f.get(key, "")).strip()
                    if raw:
                        hints.append(Path(raw))
                base = Path(fname).name
                for raw_dir in dvals:
                    if raw_dir:
                        hints.append(Path(raw_dir) / base)
        return hints

    def _transfer_hint_paths() -> List[Path]:
        hints: List[Path] = []
        for snapshot in transfer_hints or []:
            hints.extend(_paths_from_transfer_dirs(_dirs_from_transfer_result(snapshot)))
        try:
            result = _slskd_req(
                "GET", f"transfers/downloads/{_up.quote(username, safe='')}")
            hints.extend(_paths_from_transfer_dirs(_dirs_from_transfer_result(result)))
        except Exception as ex:
            log.append(f"  [slskd] WARN: transfer-path lookup failed: {ex}")
        return hints

    def _scan_exact(roots: List[Path]) -> tuple:
        for root in roots:
            try:
                if root.is_file() and root.suffix.lower() in audio_exts and root.name.lower() in queued_names:
                    return str(root.parent), [root]
                if root.is_dir():
                    matches = [
                        f for f in root.rglob("*")
                        if f.is_file()
                        and f.suffix.lower() in audio_exts
                        and f.name.lower() in queued_names
                    ]
                else:
                    continue
            except Exception as ex:
                log.append(f"  [slskd] WARN: queued-file search failed under {root}: {ex}")
                continue
            if not matches:
                continue
            by_parent = Counter(str(f.parent) for f in matches)
            best_parent, count = by_parent.most_common(1)[0]
            files = sorted([f for f in matches if str(f.parent) == best_parent], key=lambda p: p.name.lower())
            filtered = _wanted_filtered_files(best_parent, files, "queued-file match")
            if not filtered:
                continue
            log.append(f"  [slskd] Located {len(filtered)}/{len(queued_names)} queued file(s) at {best_parent}")
            return best_parent, filtered
        return "", []

    def _track_num(name: str) -> int:
        m = re.match(r'^\s*(\d{1,3})[\s._-]+', Path(name).name)
        return int(m.group(1)) if m else 0

    expected_nums = {n for n in (_track_num(x) for x in queued_names) if n}
    album_norm = re.sub(r'[^a-z0-9]', '', album.lower())
    artist_norm = re.sub(r'[^a-z0-9]', '', artist.lower())
    queued_stem_norms = {
        re.sub(r'[^a-z0-9]', '', Path(name).stem.lower())
        for name in queued_names
        if name
    }

    def _safe_dir_name(value: str) -> str:
        return re.sub(r'[\\/:*?"<>|]', '_', str(value or "")).strip()

    def _min_expected_files() -> int:
        base = track_count or len(queued_names)
        return max(1, min(base, int(base * 0.70)))

    def _wanted_filtered_files(folder: Any, files: List[Path], context: str) -> List[Path]:
        if not wanted_filter:
            return files
        selected: Dict[tuple, Dict[str, Any]] = {}
        rejected: List[str] = []
        for fpath in files:
            match = _slskd_file_wanted_match_score(fpath.name, wanted_filter)
            if not match.get("ok"):
                rejected.append(fpath.name)
                continue
            key = match.get("key") or ("file", str(fpath).lower())
            current = selected.get(key)
            if current and float(current["match"].get("score") or 0) >= float(match.get("score") or 0):
                rejected.append(fpath.name)
                continue
            selected[key] = {"file": fpath, "match": match}
        min_wanted = max(1, min(len(wanted_filter), int(math.ceil(len(wanted_filter) * 0.70))))
        if len(selected) < min_wanted:
            sample = ", ".join(rejected[:3])
            log.append(
                f"  [slskd] Ignored {context} at {folder}: "
                f"{len(selected)}/{len(wanted_filter)} file(s) matched requested missing track(s)"
                + (f" ({sample})" if sample else "")
            )
            return []
        filtered = [
            row["file"]
            for row in sorted(
                selected.values(),
                key=lambda row: (
                    int((row["match"].get("track") or {}).get("disc") or 1),
                    int((row["match"].get("track") or {}).get("track") or 0),
                    row["file"].name.lower(),
                ),
            )
        ]
        if len(filtered) != len(files):
            log.append(
                f"  [slskd] Filtered located files to {len(filtered)} requested missing track(s)."
            )
        return filtered

    def _folder_audio_files(folder: Path) -> List[Path]:
        try:
            return sorted(
                [f for f in folder.rglob("*")
                 if f.is_file() and f.suffix.lower() in audio_exts],
                key=lambda p: p.name.lower(),
            )
        except Exception:
            return []

    def _scan_direct_album_dirs() -> tuple:
        guesses: List[Path] = []
        artist_album = f"{artist} - {album}".strip(" -")
        for name in (
            artist_album,
            f"{_safe_dir_name(artist)} - {_safe_dir_name(album)}".strip(" -"),
            album,
            _safe_dir_name(album),
        ):
            if name:
                _root_add(guesses, DOWNLOADS_ROOT / name)
        for folder in guesses:
            if not folder.is_dir():
                continue
            files = _folder_audio_files(folder)
            queued_matches = [f for f in files if f.name.lower() in queued_names]
            if len(queued_matches) >= _min_expected_files():
                filtered = _wanted_filtered_files(folder, queued_matches, "direct album folder")
                if not filtered:
                    continue
                log.append(
                    f"  [slskd] Located direct album folder: "
                    f"{len(filtered)}/{len(queued_names)} queued file(s) at {folder}"
                )
                return str(folder), filtered
            if len(files) < _min_expected_files():
                continue
            nums = {n for n in (_track_num(f.name) for f in files) if n}
            overlap = len(nums & expected_nums) if expected_nums else len(files)
            if expected_nums and overlap < min(len(expected_nums), _min_expected_files()):
                continue
            filtered = _wanted_filtered_files(folder, files, "direct album folder")
            if not filtered:
                continue
            log.append(f"  [slskd] Located direct album folder: {len(filtered)} file(s) at {folder}")
            return str(folder), filtered
        return "", []

    def _scan_album_folder_fallback(roots: List[Path]) -> tuple:
        if not album_norm:
            return "", []
        min_files = _min_expected_files()
        best = None
        for root in roots:
            try:
                if not root.is_dir():
                    continue
                for folder in [root] + [p for p in root.rglob("*") if p.is_dir()]:
                    f_norm = re.sub(r'[^a-z0-9]', '', str(folder).lower())
                    if album_norm not in f_norm:
                        continue
                    if artist_norm and artist_norm not in f_norm:
                        # Album title alone is often enough, but prefer artist hits.
                        artist_hit = False
                    else:
                        artist_hit = True
                    files = _folder_audio_files(folder)
                    queued_matches = [f for f in files if f.name.lower() in queued_names]
                    if len(queued_matches) >= min_files:
                        candidate_files = sorted(queued_matches, key=lambda p: p.name.lower())
                    else:
                        candidate_files = files
                    if len(files) < min_files:
                        continue
                    nums = {n for n in (_track_num(f.name) for f in files) if n}
                    overlap = len(nums & expected_nums) if expected_nums else 0
                    if expected_nums and overlap < min(len(expected_nums), min_files):
                        continue
                    score = (1000 if artist_hit else 0) + len(candidate_files) * 10 + overlap
                    if best is None or score > best[0]:
                        best = (score, folder, candidate_files)
            except Exception as ex:
                log.append(f"  [slskd] WARN: album-folder search failed under {root}: {ex}")
                continue
        if best:
            _, folder, files = best
            filtered = _wanted_filtered_files(folder, files, "album folder fallback")
            if not filtered:
                return "", []
            log.append(f"  [slskd] Located album folder fallback: {len(filtered)} queued file(s) at {folder}")
            return str(folder), filtered
        return "", []

    def _scan_single_track_fallback(roots: List[Path]) -> tuple:
        if len(queued_names) != 1 or (track_count and track_count != 1):
            return "", []
        if not album_norm and not queued_stem_norms:
            return "", []
        newest_allowed = time.time() - (2 * 60 * 60)
        best = None
        for root in roots:
            try:
                if not root.is_dir():
                    continue
                files = [
                    f for f in root.rglob("*")
                    if f.is_file() and f.suffix.lower() in audio_exts
                ]
            except Exception as ex:
                log.append(f"  [slskd] WARN: single-track search failed under {root}: {ex}")
                continue
            for f in files:
                try:
                    if f.stat().st_mtime < newest_allowed:
                        continue
                except Exception:
                    continue
                hay = re.sub(r'[^a-z0-9]', '', f"{f.parent} {f.stem}".lower())
                title_hit = bool(album_norm and album_norm in hay)
                if not title_hit:
                    title_hit = any(stem and (stem in hay or hay in stem) for stem in queued_stem_norms)
                if not title_hit:
                    continue
                artist_hit = bool(artist_norm and artist_norm in hay)
                if artist_norm and not artist_hit and not any(stem and artist_norm in stem for stem in queued_stem_norms):
                    continue
                score = (1000 if artist_hit else 0) + int(f.stat().st_mtime - newest_allowed)
                if best is None or score > best[0]:
                    best = (score, f)
        if best:
            f = best[1]
            filtered = _wanted_filtered_files(f.parent, [f], "single-track fallback")
            if not filtered:
                return "", []
            log.append(f"  [slskd] Located recent single-track fallback at {f.parent}: {f.name}")
            return str(f.parent), filtered
        return "", []

    roots: List[Path] = []
    user_root = DOWNLOADS_ROOT / username
    for raw in (
        expected,
        user_root,
        DOWNLOADS_ROOT,
        DOWNLOADS_ROOT.parent,
        "/data/downloads",
        "/downloads",
        "/download",
        "/tmp",
    ):
        _root_add(roots, raw)

    folder, files = _scan_direct_album_dirs()
    if files:
        return folder, files

    # slskd can mark transfers completed before its final move is visible in
    # the shared volume. Poll briefly for exact queued filenames before falling
    # back to a guarded album-folder search.
    deadline = time.time() + 90
    logged_wait = False
    static_hint_logged = False
    while True:
        hint_paths = _transfer_hint_paths()
        if hint_paths and not static_hint_logged:
            preview = ", ".join(str(p) for p in hint_paths[:3])
            extra = "..." if len(hint_paths) > 3 else ""
            log.append(f"  [slskd] Transfer path hint(s): {preview}{extra}")
            static_hint_logged = True
        hint_roots = roots[:]
        for hp in hint_paths:
            _root_add(hint_roots, hp)
            _root_add(hint_roots, hp.parent)
        folder, files = _scan_exact(hint_roots)
        if files:
            return folder, files
        if not logged_wait:
            log.append("  [slskd] Completed transfers not visible yet; waiting for final file move...")
            logged_wait = True
        time.sleep(3)

    folder, files = _scan_album_folder_fallback(roots)
    if files:
        return folder, files

    folder, files = _scan_single_track_fallback(roots)
    if files:
        return folder, files

    log.append(
        "  [slskd] Could not locate completed queued files. "
        "Checked transfer hints, expected dir, /data/torrents/music, /data/downloads, /downloads, /download, /tmp."
    )
    return str(expected), []


def _slskd_wait_downloads(username: str, remote_files: list, log: list,
                           timeout: int = 600) -> tuple:
    """Poll slskd until all queued files are Completed. Returns local dir."""
    deadline = time.time() + timeout
    pending  = set(remote_files)
    completed_snapshots: List[Any] = []
    rejected_logged: set = set()
    disk_seen_logged: set = set()
    last_progress_at = time.time()
    last_done = 0
    last_transfer_marker = 0
    poll_error_count = 0
    stall_timeout = min(240, max(90, int(timeout * 0.30)))
    # Minimum new bytes required to reset the stall timer. Prevents tiny
    # SLSKD API jitter (handshake overhead, <64KB) from indefinitely deferring
    # stall detection when a peer has queued files but never actually transfers.
    _STALL_BYTE_THRESHOLD = 65536

    def _num(value: Any) -> int:
        try:
            return int(value or 0)
        except Exception:
            return 0

    def _remote_parent(remote_name: str) -> Path:
        raw = _s(remote_name).replace("\\", "/")
        if len(raw) > 2 and raw[1] == ":":
            raw = raw[2:]
        raw = raw.lstrip("/")
        return Path(raw).parent

    def _download_roots_for_pending() -> List[Path]:
        roots: List[Path] = []

        def _add(raw) -> None:
            if not raw:
                return
            try:
                p = Path(str(raw))
            except Exception:
                return
            if p not in roots:
                roots.append(p)

        for remote in remote_files:
            parent = _remote_parent(remote)
            if str(parent) in ("", "."):
                continue
            _add(DOWNLOADS_ROOT / username / parent)
            _add(DOWNLOADS_ROOT / username / parent.name)
            _add(DOWNLOADS_ROOT / parent)
            _add(DOWNLOADS_ROOT / parent.name)
        return roots

    def _already_on_disk() -> set:
        if not pending:
            return set()
        by_name: Dict[str, List[str]] = defaultdict(list)
        for remote in pending:
            by_name[Path(_s(remote).replace("\\", "/")).name.lower()].append(remote)
        if not by_name:
            return set()

        found: set = set()
        for root in _download_roots_for_pending():
            try:
                if root.is_file():
                    files = [root]
                elif root.is_dir():
                    files = [
                        f for f in root.rglob("*")
                        if f.is_file() and f.suffix.lower() in AUDIO_EXT
                    ]
                else:
                    continue
            except Exception:
                continue
            for f in files:
                matches = by_name.get(f.name.lower())
                if not matches:
                    continue
                for remote in matches:
                    if remote in pending and remote not in found:
                        found.add(remote)
                        break
            if len(found) >= len(by_name):
                break
        return found

    while time.time() < deadline and pending:
        time.sleep(10)
        try:
            result = _slskd_req(
                "GET", f"transfers/downloads/{_up.quote(username, safe='')}")
            poll_error_count = 0
            # Response is either a dict with "directories" or a list
            dirs: list = []
            if isinstance(result, list):
                for user_obj in result:
                    if (user_obj.get("username") or "").lower() == username.lower():
                        dirs = user_obj.get("directories", [])
                        break
            elif isinstance(result, dict):
                dirs = result.get("directories", [])

            newly_done: set = set()
            rejected: Dict[str, str] = {}
            transfer_marker = 0
            for d in dirs:
                for f in d.get("files", []):
                    fname = f.get("filename", "")
                    if fname not in pending:
                        continue
                    state = f"{f.get('state', '')} {f.get('stateDescription', '')}".lower()
                    exception = _s(f.get("exception", "")).strip()
                    exception_l = exception.lower()
                    if "rejected" in state or "file not shared" in exception_l:
                        rejected[fname] = exception or f.get("stateDescription") or f.get("state") or "Rejected"
                        continue
                    size = _num(f.get("size"))
                    transferred = _num(f.get("bytesTransferred"))
                    remaining = _num(f.get("bytesRemaining"))
                    pct = _num(f.get("percentComplete"))
                    if transferred > 0:
                        transfer_marker += transferred
                    elif pct > 0 and size > 0:
                        transfer_marker += int(size * min(pct, 100) / 100)
                    complete_enough = (
                        remaining == 0
                        or pct >= 100
                        or (size > 0 and transferred >= size)
                    )
                    if "completed" in state and complete_enough:
                        newly_done.add(fname)
            if newly_done:
                completed_snapshots.append(result)
            disk_done = _already_on_disk()
            if disk_done:
                newly_visible = disk_done - disk_seen_logged
                if newly_visible:
                    disk_seen_logged.update(newly_visible)
                    sample = ", ".join(
                        Path(_s(name).replace("\\", "/")).name
                        for name in sorted(newly_visible)[:3]
                    )
                    log.append(
                        f"  [slskd] Found {len(newly_visible)} already-present "
                        f"queued file(s) on disk"
                        + (f": {sample}" if sample else "")
                    )
                newly_done |= disk_done
            pending -= newly_done
            done = len(remote_files) - len(pending)
            log.append(f"  [slskd] {done}/{len(remote_files)} file(s) downloaded")
            if newly_done or done > last_done:
                last_progress_at = time.time()
                last_done = done
                last_transfer_marker = transfer_marker
            elif transfer_marker >= last_transfer_marker + _STALL_BYTE_THRESHOLD:
                last_progress_at = time.time()
                last_transfer_marker = transfer_marker
            if rejected:
                new_rejected = [name for name in rejected if name not in rejected_logged]
                if new_rejected:
                    rejected_logged.update(new_rejected)
                    samples = "; ".join(
                        f"{Path(name).name}: {rejected[name]}" for name in new_rejected[:3]
                    )
                    log.append(
                        f"  [slskd] Rejected {len(new_rejected)} queued file(s): {samples}"
                    )
                if all(name in rejected for name in pending):
                    reason = next(iter(rejected.values()), "Rejected")
                    raise RuntimeError(
                        f"SLSKD rejected {len(rejected)}/{len(remote_files)} queued file(s) "
                        f"from @{username}: {reason}. This peer's search result is not downloadable; retry to use another source."
                    )
            if pending and time.time() - last_progress_at >= stall_timeout:
                stalled_for = int(time.time() - last_progress_at)
                samples = ", ".join(
                    Path(_s(name).replace("\\", "/")).name
                    for name in sorted(pending)[:5]
                )
                raise RuntimeError(
                    f"Download stalled for {stalled_for}s with {len(pending)}/"
                    f"{len(remote_files)} file(s) still pending from @{username}"
                    + (f": {samples}" if samples else "")
                    + ". Trying another source."
                )
        except Exception as ex:
            msg = str(ex)
            if "SLSKD rejected" in msg or "Download stalled" in msg:
                raise
            poll_error_count += 1
            if (
                poll_error_count >= 3
                and "HTTP 404" in msg
                and "transfers/downloads" in msg
            ):
                raise RuntimeError(
                    f"SLSKD transfer state disappeared while polling @{username} "
                    f"after {poll_error_count} poll error(s). Trying another source."
                ) from ex
            log.append(f"  [slskd] Poll error: {ex}")

    if pending:
        done_pct = int(100 * (len(remote_files) - len(pending)) / max(len(remote_files), 1))
        log.append(f"  [slskd] WARNING: {len(pending)} file(s) still pending at timeout "
                   f"({done_pct}% complete)")
        raise RuntimeError(
            f"Download timeout: only {done_pct}% of files completed "
            f"({len(remote_files) - len(pending)}/{len(remote_files)})"
        )

    # Re-compute local dir from first file
    first = list(remote_files)[0].replace("\\", "/")
    if len(first) > 2 and first[1] == ":":
        first = first[2:]
    first = first.lstrip("/")
    return str(DOWNLOADS_ROOT / username / Path(first).parent), completed_snapshots


_DOWNLOAD_METHOD_ALIASES = {
    "yt": "ytdlp",
    "yt-dlp": "ytdlp",
    "youtube": "ytdlp",
    "youtube-music": "ytdlp",
    "sc": "soundcloud",
    "sound-cloud": "soundcloud",
    "sp": "spotiflac",
    "spotiflac": "spotiflac",
    "spotiflac-cli": "spotiflac",
}


_SLSKD_FALLBACK_METHODS = os.environ.get("SLSKD_FALLBACK_METHODS", "spotiflac,ytdlp,soundcloud")


def _normalise_download_method(value: Any, default: str = "slskd") -> str:
    method = _s(value or default).strip().lower()
    method = _DOWNLOAD_METHOD_ALIASES.get(method, method)
    return method or default


def _download_method_list(value: str) -> List[str]:
    methods: List[str] = []
    for item in re.split(r"[\s,]+", _s(value)):
        method = _normalise_download_method(item, "")
        if method and method not in methods:
            methods.append(method)
    return methods


def _slskd_fallback_methods(requested: str = "") -> List[str]:
    methods: List[str] = []
    for method in [requested] + _download_method_list(_SLSKD_FALLBACK_METHODS):
        method = _normalise_download_method(method, "")
        if method in {"spotiflac", "ytdlp", "soundcloud"} and method not in methods:
            methods.append(method)
    if methods and "ytdlp" not in methods:
        insert_at = methods.index("soundcloud") if "soundcloud" in methods else len(methods)
        methods.insert(insert_at, "ytdlp")
    return methods or ["spotiflac", "ytdlp", "soundcloud"]


def _strip_track_filename_id_suffix(value: Any) -> str:
    try:
        return _canonical_strip_track_filename_id_suffix(value)
    except NameError:
        from backend.matching import strip_track_filename_id_suffix as _fallback_strip
        return _fallback_strip(value)
