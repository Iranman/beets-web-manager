"""Characterization of the album download job (modernization M4, #505).

Each scenario drives the real ``start_album_download`` and its job body with
fake providers, a fake import pipeline and an in-memory recovery carrier store.
The complete trace -- every provider/import/carrier call in order with its
arguments, the job log, the job metadata and workflow, the result or error -- is
compared with ``tests/fixtures/album_download_golden.json``, which was generated
from the code BEFORE the function was split into steps.

Regenerate only for a deliberate behavior change:
    UPDATE_ALBUM_DOWNLOAD_GOLDEN=1 python -m unittest tests.test_album_download_characterization
"""
import copy
import json
import os
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import backend.acquisition_service as acq

GOLDEN = Path(__file__).parent / "fixtures" / "album_download_golden.json"

RG = "17a64abf-6d31-473f-a740-38ffa548f225"
RG_OTHER = "99999999-6d31-473f-a740-38ffa548f225"
RELEASE = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
RELEASE_ALT = "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee"
CARRIER = "c" * 32

T1 = {"track": 1, "disc": 1, "title": "Song One", "mb_trackid": "rec-1", "duration_ms": 1000}
T2 = {"track": 2, "disc": 1, "title": "Song Two", "mb_trackid": "rec-2", "duration_ms": 2000}
T3 = {"track": 3, "disc": 1, "title": "Song Three", "mb_trackid": "rec-3", "duration_ms": 3000}

BASE = {"artist": "Artist", "album": "Album", "year": "2024"}
FULL = dict(BASE, mb_albumid=RELEASE, track_count=2)
SLSKD = dict(FULL, method="slskd")
YTDLP = dict(FULL, method="ytdlp")
RECOVERY = dict(BASE, existing_album_id=42, mb_releasegroupid=RG, method="ytdlp", missing_tracks=[T1, T2])
SLSKD_RECOVERY = dict(RECOVERY, method="slskd")

PEER_REFUSAL = "import job failed: Could not resolve a MusicBrainz release ID for import [reason_code=file_unverified]"
GLOBAL_REFUSAL = "import job failed: Could not resolve a MusicBrainz release ID for import [reason_code=acoustid_unavailable]"
MERGE_FAILED = "import job failed: Could not merge the imported track(s) into the existing album [reason_code=existing_album_merge_failed]"
GOOD = {"user": "peer", "dir": "share/Album", "queued": ["01 Song One.flac", "02 Song Two.flac"]}
GOOD2 = {"user": "peer2", "dir": "share2/Album", "queued": ["01 Song One.flac", "02 Song Two.flac"]}
EMPTY = {"user": "bad", "dir": "share/none", "queued": ["01 Song One.flac"], "files": 0}
SHORT = {"user": "short", "dir": "share/short", "queued": ["01 Song One.flac"]}
ONLY_T1 = {"user": "one", "dir": "share/one", "queued": ["01 Song One.flac"]}


def _carrier(**fields):
    row = {"carrier_id": CARRIER, "existing_album_id": 42, "mb_releasegroupid": RG,
           "expected_tracks": [T1, T2], "wanted_tracks": [], "checkpoint": "CREATED", "source": {}}
    row.update(fields)
    return row


SCENARIOS = {
    # ── refused before any job exists ──
    "refuse_missing_artist": {"payload": {"album": "Album"}},
    "refuse_missing_album": {"payload": {"artist": "Artist"}},
    "refuse_unknown_method": {"payload": dict(BASE, method="ftp")},
    "refuse_bad_root": {"payload": YTDLP, "root_error": "downloads root unusable"},
    "refuse_dot_artist": {"payload": dict(YTDLP, artist="..")},
    "refuse_dot_album": {"payload": dict(YTDLP, album=".")},
    "refuse_slskd_unconfigured": {"payload": SLSKD},
    "raise_bad_track_count": {"payload": dict(YTDLP, track_count="many")},
    # ── request parsing, recorded in the job metadata ──
    "parse_coercions": {"payload": dict(
        artist="  A/B ", album=" C$D ", year=2020, track_count="3", method="ytdlp", albumartist="Forced [mbid-x]",
        mb_releasegroupid=RG, replace_existing_item_ids=["7", 0, "x", None], fallback_method="soundcloud",
        album_id="9", wanted_tracks=[T1]), "not_in_album": [[]]},
    "parse_bad_existing_album_id": {"payload": dict(YTDLP, existing_album_id="x", auto_import=False)},
    "parse_fallback_flags": {"payload": dict(YTDLP, try_ytdlp_fallback=True, auto_import=False)},
    "parse_invalid_fallback_method": {"payload": dict(YTDLP, fallback_method="ftp", auto_import=False)},
    # ── release resolution ──
    "resolve_fails_auto_import": {"payload": YTDLP, "resolve": ""},
    "resolve_fails_no_auto_import": {"payload": dict(YTDLP, auto_import=False), "resolve": ""},
    "resolve_skipped_without_id_or_import": {"payload": dict(BASE, method="ytdlp", auto_import=False)},
    "resolve_track_count_from_mb": {"payload": dict(YTDLP, track_count=9)},
    "resolve_tracklist_not_ok": {"payload": YTDLP, "tracklist": {"ok": False}},
    # ── direct sources ──
    "direct_ytdlp_album": {"payload": YTDLP},
    "direct_soundcloud_album": {"payload": dict(FULL, method="soundcloud")},
    "direct_spotiflac_album": {"payload": dict(FULL, method="spotiflac")},
    "direct_spotiflac_tracks": {"payload": dict(RECOVERY, method="spotiflac")},
    "direct_failure_raises": {"payload": YTDLP, "direct": {"ytdlp": ["boom"]}},
    "direct_no_import": {"payload": dict(YTDLP, auto_import=False)},
    "import_no_album_ids_not_marked_done": {"payload": YTDLP, "imports": [{"ok": True}]},
    "import_child_failure_raises": {"payload": YTDLP, "imports": ["import job failed: bad"]},
    "source_audio_rejected": {"payload": YTDLP, "source_audio_error": "not audio"},
    # ── recovery identity and resume ──
    "recovery_fresh": {"payload": RECOVERY},
    "recovery_stored_tracks_from_queue": {"payload": RECOVERY, "not_in_album": [[T2]]},
    "recovery_bare_albumid_resolves_group": {
        "payload": dict(BASE, existing_album_id=42, mb_albumid=RELEASE, method="ytdlp", missing_tracks=[T1]),
        "rg_strict": RG},
    "recovery_bare_albumid_mb_down": {
        "payload": dict(BASE, existing_album_id=42, mb_albumid=RELEASE, method="ytdlp", missing_tracks=[T1]),
        "rg_strict": "boom"},
    "recovery_group_url_in_albumid": {
        "payload": dict(BASE, existing_album_id=42, mb_albumid=f"https://musicbrainz.org/release-group/{RG}",
                        method="ytdlp", missing_tracks=[T1])},
    "recovery_crafted_release_url": {
        "payload": dict(BASE, existing_album_id=42, mb_albumid=f"https://musicbrainz.org/release/{RELEASE}#release-group",
                        method="ytdlp", missing_tracks=[T1])},
    "resume_id_honored": {"payload": dict(RECOVERY, recovery_carrier_id=CARRIER), "carriers": [_carrier()]},
    "resume_id_uppercase_honored": {"payload": dict(RECOVERY, recovery_carrier_id=CARRIER.upper()), "carriers": [_carrier()]},
    "resume_id_malformed_ignored": {"payload": dict(RECOVERY, recovery_carrier_id="nope")},
    "resume_id_unknown": {"payload": dict(RECOVERY, recovery_carrier_id=CARRIER)},
    "resume_id_other_album": {"payload": dict(RECOVERY, recovery_carrier_id=CARRIER),
                              "carriers": [_carrier(existing_album_id=7)]},
    "resume_id_other_group": {"payload": dict(RECOVERY, recovery_carrier_id=CARRIER),
                              "carriers": [_carrier(mb_releasegroupid=RG_OTHER)]},
    "resume_id_sibling_tracks": {"payload": dict(RECOVERY, recovery_carrier_id=CARRIER),
                                 "carriers": [_carrier(expected_tracks=[T3])]},
    "resume_id_read_error_at_start": {"payload": dict(RECOVERY, recovery_carrier_id=CARRIER),
                                      "carriers": [_carrier()], "carrier_read_errors": [0]},
    "resume_id_on_non_recovery_ignored": {"payload": dict(YTDLP, recovery_carrier_id=CARRIER)},
    "resume_by_identity": {"payload": RECOVERY, "carriers": [_carrier()], "by_identity": CARRIER},
    "resume_by_identity_sibling": {"payload": RECOVERY, "carriers": [_carrier(expected_tracks=[T3])],
                                   "by_identity": CARRIER},
    "resume_by_identity_error": {"payload": RECOVERY, "by_identity": "boom"},
    "resume_imported_runs_plex": {"payload": dict(RECOVERY, recovery_carrier_id=CARRIER),
                                  "carriers": [_carrier(checkpoint="IMPORTED")]},
    "resume_plex_synced": {"payload": dict(RECOVERY, recovery_carrier_id=CARRIER),
                           "carriers": [_carrier(checkpoint="PLEX_SYNCED")]},
    "resume_read_failure_in_job": {"payload": dict(RECOVERY, recovery_carrier_id=CARRIER),
                                   "carriers": [_carrier()], "carrier_read_errors": [1]},
    "fresh_read_failure_in_job": {"payload": RECOVERY, "carrier_read_errors": [0]},
    "create_read_failure_fails_closed": {"payload": RECOVERY, "carrier_read_errors": [1]},
    "create_write_failure_tolerated": {"payload": RECOVERY, "carrier_write_errors": ["create"]},
    "advance_write_failure_tolerated": {"payload": RECOVERY, "carrier_write_errors": ["advance", "bind"]},
    "provenance_read_error": {"payload": RECOVERY, "carrier_read_errors": [2]},
    "provenance_without_carrier": {"payload": RECOVERY, "carrier_write_errors": ["create", "bind"]},
    "recovery_without_release_group": {"payload": dict(BASE, existing_album_id=42, method="ytdlp", missing_tracks=[T1]),
                                       "tracklist": {"ok": True, "tracks": [1, 2]}},
    "track_position_not_covered": {
        "payload": dict(RECOVERY, recovery_carrier_id=CARRIER, missing_tracks=[{"track": 9, "title": "Song Nine"}]),
        "carriers": [_carrier()]},
    "track_without_identity_not_covered": {
        "payload": dict(RECOVERY, recovery_carrier_id=CARRIER, missing_tracks=[{"title": "Song One"}]),
        "carriers": [_carrier()]},
    "track_position_covered_without_rid": {
        "payload": dict(RECOVERY, recovery_carrier_id=CARRIER, missing_tracks=[{"track": 1, "title": "Song One"}]),
        "carriers": [_carrier(checkpoint="IMPORTED")]},
    "track_position_conflicts_with_rid": {
        "payload": dict(RECOVERY, recovery_carrier_id=CARRIER,
                        missing_tracks=[{"track": 1, "title": "Song One", "mb_trackid": "rec-9"}]),
        "carriers": [_carrier()]},
    "track_position_fallback_unidentified_slot": {
        "payload": dict(RECOVERY, recovery_carrier_id=CARRIER,
                        missing_tracks=[{"track": 1, "title": "Song One", "mb_trackid": "rec-9"}]),
        "carriers": [_carrier(expected_tracks=[{"track": 1, "disc": 1}])]},
    "track_bad_position_is_unproven": {
        "payload": dict(RECOVERY, recovery_carrier_id=CARRIER),
        "carriers": [_carrier(expected_tracks=[{"track": "x"}, "junk"])]},
    # ── existing album reconciliation ──
    "replace_existing_keeps_tracks": {
        "payload": dict(RECOVERY, replace_existing=True, replace_existing_item_ids=[5])},
    "replace_existing_ids_imply_mode": {"payload": dict(RECOVERY, replace_existing_item_ids=[5])},
    "existing_album_without_tracks": {"payload": dict(BASE, existing_album_id=42, mb_albumid=RELEASE, method="ytdlp")},
    "already_complete_queues_review": {"payload": RECOVERY, "not_in_album": [[]],
                                       "completeness": {"missing": [T3]}},
    "already_complete_completeness_error": {"payload": RECOVERY, "not_in_album": [[]], "completeness": "boom"},
    "already_complete_review_refused": {"payload": RECOVERY, "not_in_album": [[]], "queue_review": False},
    "already_complete_no_folder": {"payload": RECOVERY, "not_in_album": [[]], "album_folder": ""},
    # ── wanted-track identity gate ──
    "validate_partial_undelivered": {"payload": RECOVERY, "validate": "partial"},
    "validate_all_deferred": {"payload": RECOVERY, "validate": "deferred_all"},
    "validate_mixed_separated": {"payload": RECOVERY, "validate": "mixed"},
    "validate_mixed_nothing_to_separate": {"payload": RECOVERY, "validate": "mixed", "separation": ["", True]},
    "validate_mixed_incomplete": {"payload": RECOVERY, "validate": "mixed", "separation": ["", False]},
    "validate_refused": {"payload": RECOVERY, "validate": "refuse"},
    # ── no auto-import hand-off ──
    "handoff_recovery_queued": {"payload": dict(RECOVERY, auto_import=False)},
    "handoff_refused": {"payload": dict(RECOVERY, auto_import=False), "queue_review": False},
    "handoff_raises": {"payload": dict(RECOVERY, auto_import=False), "queue_review": "boom"},
    "handoff_carrier_unavailable": {"payload": dict(RECOVERY, auto_import=False), "carrier_write_errors": ["create"]},
    "handoff_carrier_read_error": {"payload": dict(RECOVERY, auto_import=False), "carrier_read_errors": [2]},
    "handoff_state_write_error": {"payload": dict(RECOVERY, auto_import=False), "carrier_write_errors": ["advance"]},
    "handoff_failed_state_write_error": {"payload": dict(RECOVERY, auto_import=False), "queue_review": False,
                                         "carrier_write_errors": ["advance"]},
    # ── slskd ──
    "slskd_unconfigured_goes_direct": {"payload": dict(SLSKD, try_source_fallback=True)},
    "slskd_unconfigured_direct_tracks": {"payload": dict(SLSKD_RECOVERY, try_source_fallback=True)},
    "slskd_unconfigured_no_auto_import": {"payload": dict(SLSKD, try_source_fallback=True, auto_import=False)},
    "slskd_ok": {"payload": SLSKD, "slskd": True, "candidates": [GOOD]},
    "slskd_ok_without_cancel_event": {"payload": SLSKD, "slskd": True, "candidates": [GOOD], "no_cancel": True},
    "slskd_no_import": {"payload": dict(SLSKD, auto_import=False), "slskd": True, "candidates": [GOOD]},
    "slskd_no_import_download_fails": {"payload": dict(SLSKD, auto_import=False), "slskd": True,
                                       "candidates": [{"search_error": "no peers"}]},
    "slskd_no_import_download_fails_fallback": {"payload": dict(SLSKD, auto_import=False, try_source_fallback=True),
                                                "slskd": True, "candidates": [{"search_error": "no peers"}]},
    "slskd_no_import_recovery_handoff": {"payload": dict(SLSKD_RECOVERY, auto_import=False), "slskd": True,
                                         "candidates": [GOOD]},
    "slskd_search_error_no_candidates": {"payload": SLSKD, "slskd": True, "candidates": [{"search_error": "no peers"}]},
    "slskd_retry_after_empty_candidate": {"payload": SLSKD, "slskd": True, "candidates": [EMPTY, GOOD]},
    "slskd_retry_after_wait_error": {"payload": SLSKD, "slskd": True, "candidates": [dict(GOOD, wait_error="stalled"), GOOD2]},
    "slskd_search_error_after_failures": {"payload": SLSKD, "slskd": True,
                                          "candidates": [EMPTY, {"search_error": "no peers"}]},
    "slskd_attempts_exhausted": {"payload": SLSKD, "slskd": True, "candidates": [EMPTY] * 12},
    "slskd_attempts_exhausted_fallback": {"payload": dict(SLSKD, try_source_fallback=True), "slskd": True,
                                          "candidates": [EMPTY] * 12},
    "slskd_candidate_without_name": {"payload": SLSKD, "slskd": True,
                                     "candidates": [{"user": "", "dir": "", "queued": [], "files": 0}]},
    "slskd_cancel_during_wait": {"payload": dict(SLSKD, try_source_fallback=True), "slskd": True,
                                 "candidates": [dict(GOOD, wait_error="stalled", cancel=True)]},
    "slskd_cancel_during_settle": {"payload": SLSKD, "slskd": True, "candidates": [dict(GOOD, cancel=True)]},
    "slskd_settle_cancel_no_fallback_no_retry": {"payload": dict(SLSKD, auto_import=False), "slskd": True,
                                                 "candidates": [dict(GOOD, cancel=True), GOOD2]},
    "slskd_extra_files_kept": {"payload": SLSKD, "slskd": True,
                               "candidates": [dict(GOOD, queued=GOOD["queued"] + ["03 Song Three.flac"])],
                               "rg_replacement": ""},
    # release switching inside the Release Group
    "switch_release_ok": {"payload": dict(BASE, mb_releasegroupid=RG, method="slskd", track_count=2), "slskd": True,
                          "candidates": [SHORT], "tracklist": {"ok": True, "tracks": [1, 2, 3], "release_group": RG},
                          "rg_replacement": RELEASE_ALT,
                          "tracklists": {RELEASE_ALT: {"ok": True, "tracks": [1]}}},
    "switch_release_reinvoked_starts_over": {
        "payload": dict(BASE, mb_releasegroupid=RG, method="slskd", track_count=2), "slskd": True,
        "candidates": [SHORT, SHORT], "tracklist": {"ok": True, "tracks": [1, 2], "release_group": RG},
        "rg_replacement": RELEASE_ALT, "tracklists": {RELEASE_ALT: {"ok": True, "tracks": [1]}}, "runs": 2},
    "switch_release_supplied_id_differs": {"payload": SLSKD, "slskd": True, "resolve": RELEASE_ALT, "candidates": [SHORT],
                                           "tracklist": {"ok": True, "tracks": [1, 2], "release_group": RG},
                                           "rg_replacement": RELEASE,
                                           "tracklists": {RELEASE: {"ok": True, "tracks": [1]}}},
    "switch_release_too_few_files": {"payload": dict(BASE, mb_releasegroupid=RG, method="slskd"), "slskd": True,
                                     "candidates": [SHORT, GOOD],
                                     "tracklist": {"ok": True, "tracks": [1, 2, 3, 4, 5], "release_group": RG}},
    "switch_release_same_release": {"payload": dict(BASE, mb_releasegroupid=RG, method="slskd"), "slskd": True,
                                    "candidates": [SHORT, GOOD], "rg_replacement": RELEASE},
    "switch_release_no_replacement": {"payload": dict(BASE, mb_releasegroupid=RG, method="slskd"), "slskd": True,
                                      "candidates": [SHORT, GOOD], "rg_replacement": ""},
    "switch_release_count_mismatch": {"payload": dict(BASE, mb_releasegroupid=RG, method="slskd"), "slskd": True,
                                      "candidates": [SHORT, GOOD], "rg_replacement": RELEASE_ALT,
                                      "tracklists": {RELEASE_ALT: {"ok": True, "tracks": [1, 2]}}},
    "switch_release_preflight_rejects": {"payload": dict(BASE, mb_releasegroupid=RG, method="slskd"), "slskd": True,
                                         "candidates": [SHORT, GOOD], "rg_replacement": RELEASE_ALT,
                                         "tracklists": {RELEASE_ALT: {"ok": True, "tracks": [1]}},
                                         "preflight": {"ok": False, "matches": 0, "expected": 1}},
    "no_switch_without_group_request": {"payload": SLSKD, "slskd": True, "candidates": [SHORT, GOOD]},
    "no_switch_without_group_id": {"payload": dict(BASE, mb_releasegroupid=RG, method="slskd"), "slskd": True,
                                   "candidates": [SHORT, GOOD], "tracklist": {"ok": True, "tracks": [1, 2]}},
    # import validation decides whether another peer is tried
    "slskd_import_fail_next_peer": {"payload": SLSKD, "slskd": True, "candidates": [GOOD, GOOD2],
                                    "imports": [PEER_REFUSAL, {"ok": True, "album_ids": [7]}]},
    "slskd_import_fail_unknown_error_next_peer": {"payload": SLSKD, "slskd": True, "candidates": [GOOD, GOOD2],
                                                  "imports": ["import job failed: odd", {"ok": True}]},
    "slskd_import_fail_all_attempts": {"payload": SLSKD, "slskd": True, "candidates": [GOOD] * 12,
                                       "imports": [PEER_REFUSAL] * 12},
    "slskd_import_fail_all_attempts_fallback": {"payload": dict(SLSKD, try_source_fallback=True), "slskd": True,
                                                "candidates": [GOOD] * 12, "imports": [PEER_REFUSAL] * 12 + ["ok"]},
    "slskd_import_global_refusal_stops": {"payload": SLSKD, "slskd": True, "candidates": [GOOD, GOOD2],
                                          "imports": [GLOBAL_REFUSAL]},
    "slskd_import_global_refusal_falls_back": {"payload": dict(SLSKD, try_source_fallback=True), "slskd": True,
                                               "candidates": [GOOD, GOOD2], "imports": [GLOBAL_REFUSAL, "ok"]},
    "slskd_import_merge_failed_stops_all": {"payload": dict(SLSKD, try_source_fallback=True), "slskd": True,
                                            "candidates": [GOOD, GOOD2], "imports": [MERGE_FAILED]},
    "slskd_import_fail_after_exhausting_search": {"payload": dict(SLSKD, try_source_fallback=True), "slskd": True,
                                                  "candidates": [GOOD], "imports": [PEER_REFUSAL, "ok"]},
    "slskd_stage_error_propagates": {"payload": dict(SLSKD, try_source_fallback=True), "slskd": True,
                                     "candidates": [GOOD], "stage_error": "disk full"},
    # missing tracks over slskd
    "slskd_tracks_ok": {"payload": SLSKD_RECOVERY, "slskd": True, "candidates": [GOOD]},
    "slskd_tracks_partial_no_fallback": {"payload": SLSKD_RECOVERY, "slskd": True, "candidates": [ONLY_T1],
                                         "not_in_album": [[T1, T2], [T2]]},
    "slskd_tracks_partial_fallback_ok": {"payload": dict(SLSKD_RECOVERY, try_source_fallback=True), "slskd": True,
                                         "candidates": [ONLY_T1], "not_in_album": [[T1, T2], [T2]]},
    "slskd_tracks_partial_fallback_fails": {"payload": dict(SLSKD_RECOVERY, try_source_fallback=True), "slskd": True,
                                            "candidates": [ONLY_T1], "not_in_album": [[T1, T2], [T2]],
                                            "direct": {"soundcloud": ["boom"], "ytdlp": ["boom"]}},
    "slskd_tracks_partial_nothing_remains": {"payload": SLSKD_RECOVERY, "slskd": True, "candidates": [ONLY_T1],
                                             "not_in_album": [[T1, T2], []]},
    "slskd_tracks_undelivered_triggers_recheck": {"payload": dict(SLSKD_RECOVERY, try_source_fallback=True), "slskd": True,
                                                  "candidates": [GOOD], "validate": "partial",
                                                  "not_in_album": [[T1, T2], [T2]]},
    "slskd_tracks_staged_for_slot_gate": {"payload": SLSKD_RECOVERY, "slskd": True, "candidates": [GOOD],
                                          "validate": "deferred_all", "not_in_album": [[T1, T2], [T1, T2]]},
    # direct-source fallback
    "fallback_all_unavailable": {"payload": dict(SLSKD, try_source_fallback=True), "slskd": True,
                                 "candidates": [{"search_error": "no peers"}],
                                 "unavailable": {"soundcloud": "no binary", "ytdlp": "no cookies"}},
    "fallback_first_fails_second_ok": {"payload": dict(SLSKD, try_source_fallback=True), "slskd": True,
                                       "candidates": [{"search_error": "no peers"}], "direct": {"soundcloud": ["boom"]}},
    "fallback_one_unavailable": {"payload": dict(SLSKD, try_source_fallback=True), "slskd": True,
                                 "candidates": [{"search_error": "no peers"}], "unavailable": {"soundcloud": "no binary"}},
    "fallback_all_fail": {"payload": dict(SLSKD, try_source_fallback=True), "slskd": True,
                          "candidates": [{"search_error": "no peers"}],
                          "direct": {"soundcloud": ["boom"], "ytdlp": ["boom"]}},
    "fallback_import_fail_deletes_and_continues": {"payload": dict(SLSKD, try_source_fallback=True), "slskd": True,
                                                   "candidates": [{"search_error": "no peers"}],
                                                   "imports": ["import job failed: bad", "ok"]},
    "fallback_merge_failed_stops": {"payload": dict(SLSKD, try_source_fallback=True), "slskd": True,
                                    "candidates": [{"search_error": "no peers"}], "imports": [MERGE_FAILED]},
    "fallback_cancelled_before_trying": {"payload": dict(SLSKD, try_source_fallback=True), "cancel_at_start": True},
    "fallback_redacts_error": {"payload": dict(SLSKD, try_source_fallback=True), "slskd": True,
                               "candidates": [{"search_error": "failed apikey=abc123 token=abc"}],
                               "direct": {"soundcloud": ["boom"], "ytdlp": ["boom"]}},
}


class _Cancel(threading.Event):
    def wait(self, timeout=None):
        return self.is_set()


class World:
    """The fake world around the job. Every fake appends to ``events`` in call order."""

    def __init__(self, tmp, cfg):
        self.tmp = str(tmp)
        self.cfg = cfg
        self.events = []
        self.log = []
        self.cancel = _Cancel()
        self.carriers = {row["carrier_id"]: copy.deepcopy(row) for row in cfg.get("carriers", [])}
        self.candidates = list(cfg.get("candidates", []))
        self.imports = list(cfg.get("imports", []))
        self.direct = {k: list(v) for k, v in cfg.get("direct", {}).items()}
        self.not_in_album = list(cfg.get("not_in_album", []))
        self.carrier_reads = 0
        self.stages = 0
        self.guard_kwargs = {}
        self.downloads = Path(self.tmp, "downloads")
        self.downloads.mkdir()
        self.current = {}

    def clean(self, value):
        if value is self.log:
            return "<log>"
        if value is self.cancel:
            return "<cancel>"
        if isinstance(value, dict):
            return {str(k): self.clean(v) for k, v in value.items()}
        if isinstance(value, (set, frozenset)):
            return sorted(self.clean(v) for v in value)
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    def spy(self, name, fake=None):
        def call(*args, **kwargs):
            self.events.append([name, self.clean(args), self.clean(kwargs)])
            return fake(*args, **kwargs) if fake else None
        return call

    # ── carrier store ──
    def get_carrier(self, carrier_id):
        index = self.carrier_reads
        self.carrier_reads += 1
        if index in self.cfg.get("carrier_read_errors", []):
            raise OSError("store locked")
        row = self.carriers.get(carrier_id)
        return copy.deepcopy(row) if row else None

    def get_by_identity(self, identity_key=""):
        target = self.cfg.get("by_identity")
        if target == "boom":
            raise OSError("store locked")
        return copy.deepcopy(self.carriers.get(target)) if target else None

    def _write(self, kind):
        if kind in self.cfg.get("carrier_write_errors", []):
            raise OSError("store locked")

    def create_carrier(self, **fields):
        self._write("create")
        self.carriers[fields["carrier_id"]] = dict(copy.deepcopy(fields), checkpoint="CREATED")

    def advance_state(self, carrier_id, state, checkpoint=None, payload_extra=None):
        self._write("advance")
        row = self.carriers.setdefault(carrier_id, {"carrier_id": carrier_id})
        row["state"] = state
        if checkpoint:
            row["checkpoint"] = checkpoint
        row.update(copy.deepcopy(payload_extra or {}))

    def bind_staged_folder(self, carrier_id, folder):
        self._write("bind")
        self.carriers.setdefault(carrier_id, {"carrier_id": carrier_id}).setdefault("folders", []).append(str(folder))

    def plex_leg(self, carrier, log):
        self.carriers[carrier["carrier_id"]]["checkpoint"] = "PLEX_SYNCED"

    # ── resolution ──
    def resolve_release(self, *_a, **_k):
        return self.cfg.get("resolve", RELEASE)

    def tracklist(self, release, log):
        special = self.cfg.get("tracklists", {})
        if release in special:
            return special[release]
        return self.cfg.get("tracklist", {"ok": True, "tracks": [1, 2], "release_group": RG})

    def wanted_not_in_album(self, album_id, release, wanted, log):
        return self.not_in_album.pop(0) if self.not_in_album else wanted

    def rg_strict(self, value):
        result = self.cfg.get("rg_strict", value)
        if result == "boom":
            raise OSError("MusicBrainz down")
        self.events.append(["rg_strict", value])
        return result

    def completeness(self, *_a, **_k):
        result = self.cfg.get("completeness", {"missing": []})
        if result == "boom":
            raise OSError("no scan")
        return result

    def queue_review(self, *_a, **_k):
        result = self.cfg.get("queue_review", True)
        if result == "boom":
            raise OSError("queue down")
        return result

    # ── direct sources ──
    def _download(self, source, target):
        script = self.direct.get(source)
        if script and script.pop(0) == "boom":
            raise RuntimeError(f"{source} failed")
        Path(target).mkdir(parents=True, exist_ok=True)
        (Path(target) / "01.flac").write_bytes(b"x")

    def direct_tracks(self, source):
        def run(artist, album, year, target, log, tracks, **kwargs):
            self._download(kwargs.get("source") or source, target)
            return len(tracks)
        return run

    def direct_album(self, source):
        def run(artist, album, year, target, log, **kwargs):
            self._download(kwargs.get("source") or source, target)
            return kwargs.get("track_count") or 1
        return run

    # ── slskd ──
    def slskd_search(self, *_a, **_k):
        if not self.candidates:
            raise RuntimeError("no more candidates")
        self.current = dict(self.candidates.pop(0))
        if self.current.get("search_error"):
            raise RuntimeError(self.current["search_error"])
        name = self.current["user"]
        return name, list(self.current["queued"]), f"{self.tmp}/expected/{name}", self.current["dir"]

    def slskd_wait(self, *_a, **_k):
        if self.current.get("cancel"):
            self.cancel.set()
        if self.current.get("wait_error"):
            raise RuntimeError(self.current["wait_error"])
        return f"{self.tmp}/slskd/{self.current['user']}", {"hint": 1}

    def slskd_find(self, username, queued, expected, log, **_k):
        count = self.current.get("files", len(queued))
        folder = f"{self.tmp}/slskd/{username}"
        return folder, [f"{folder}/{name}" for name in queued[:count]]

    # ── import pipeline ──
    def stage(self, *_a, **_k):
        if self.cfg.get("stage_error"):
            raise OSError(self.cfg["stage_error"])
        self.stages += 1
        folder = Path(self.tmp, f"staged-{self.stages}")
        folder.mkdir()
        (folder / "track.flac").write_bytes(b"x")
        return str(folder)

    def validate_identity(self, import_dir, wanted, log, **_k):
        mode = self.cfg.get("validate", "all")
        wanted = list(wanted)
        if mode == "refuse":
            raise RuntimeError("Downloaded audio did not fingerprint-verify")
        if mode == "partial":
            return wanted[:1], wanted[1:], []
        if mode == "deferred_all":
            return [], wanted, [f"{import_dir}/deferred.flac"]
        if mode == "mixed":
            return wanted[:1], wanted[1:], [f"{import_dir}/deferred.flac"]
        return wanted, [], []

    def source_audio(self, *_a, **_k):
        if self.cfg.get("source_audio_error"):
            raise RuntimeError(self.cfg["source_audio_error"])

    def wait_child(self, *_a, **_k):
        outcome = self.imports.pop(0) if self.imports else "ok"
        if outcome == "ok":
            return {"ok": True, "album_ids": [7]}
        if isinstance(outcome, str):
            raise RuntimeError(outcome)
        return outcome

    def root(self):
        if self.cfg.get("root_error"):
            raise RuntimeError(self.cfg["root_error"])
        return self.downloads

    def guarded(self, fn, **kwargs):
        self.guard_kwargs = kwargs
        return fn

    def patches(self):
        spy = self.spy
        separation = self.cfg.get("separation", [f"{self.tmp}/slot", True])
        unavailable = self.cfg.get("unavailable", {})
        module = {
            "validated_downloads_root": self.root,
            "slskd_configured": lambda: bool(self.cfg.get("slskd")),
            "_slskd_fallback_methods": lambda: ["soundcloud", "ytdlp"],
            "_resolve_album_release_for_import": spy("resolve_release", self.resolve_release),
            "_fetch_mb_release_tracklist": spy("tracklist", self.tracklist),
            "_wanted_tracks_not_in_album": spy("not_in_album", self.wanted_not_in_album),
            "_album_folder_for_album_id": spy("album_folder", lambda *_a: self.cfg.get("album_folder", "/music/Artist/Album")),
            "_album_mb_completeness": spy("completeness", self.completeness),
            "_ai_match_evidence_packet": spy("evidence", lambda *_a, **_k: {"packet": True}),
            "_build_folder_evidence": spy("folder_evidence", lambda *_a: {"folder": True}),
            "_folder_release_preflight": spy("preflight", lambda *_a, **_k: self.cfg.get("preflight", {"ok": True})),
            "_queue_folder_for_manual_review": spy("queue_review", self.queue_review),
            "_resolve_release_group_to_release": spy("rg_to_release", lambda *_a, **_k: self.cfg.get("rg_replacement", RELEASE_ALT)),
            "_direct_source_unavailable_reason": spy("source_check", lambda name: unavailable.get(name, "")),
            "_ytdlp_album_download": spy("ytdlp_album", self.direct_album("ytdlp")),
            "_ytdlp_missing_tracks_download": spy("ytdlp_tracks", self.direct_tracks("ytdlp")),
            "_spotiflac_album_download": spy("spotiflac_album", self.direct_album("spotiflac")),
            "_spotiflac_missing_tracks_download": spy("spotiflac_tracks", self.direct_tracks("spotiflac")),
            "_slskd_search_and_queue": spy("slskd_search", self.slskd_search),
            "_slskd_wait_downloads": spy("slskd_wait", self.slskd_wait),
            "_find_slskd_downloaded_files": spy("slskd_find", self.slskd_find),
            "_slskd_cancel_queued_downloads": spy("slskd_cancel"),
            "_slskd_cleanup_failed_candidate_files": spy("slskd_cleanup"),
            "_stage_selected_audio_files": spy("stage", self.stage),
            "_delete_staged_import_folder": spy("delete_staged"),
            "_validate_wanted_download_identity_before_import": spy("validate_identity", self.validate_identity),
            "_separate_deferred_slot_files": spy("separate", lambda *_a: tuple(separation)),
            "_validate_import_source_audio": spy("source_audio", self.source_audio),
            "_start_reimport_disk_job_internal": spy("start_import", lambda *_a, **_k: "import-1"),
            "_wait_for_child_job": spy("wait_import", self.wait_child),
            "_recovery_plex_sync_leg": spy("plex_leg", self.plex_leg),
            "acquisition_queue_mark_done": spy("queue_mark_done"),
            "time": types.SimpleNamespace(time=lambda: 1700000000.0, sleep=spy("sleep")),
            "uuid": types.SimpleNamespace(uuid4=lambda: types.SimpleNamespace(hex="f" * 32)),
        }
        carrier = {
            "get_carrier": spy("carrier_get", self.get_carrier),
            "load_for_job": spy("carrier_load_for_job", lambda *_a: None),
            "get_by_identity": spy("carrier_by_identity", self.get_by_identity),
            "create_carrier": spy("carrier_create", self.create_carrier),
            "advance_state": spy("carrier_advance", self.advance_state),
            "bind_staged_folder": spy("carrier_bind", self.bind_staged_folder),
        }
        started = [mock.patch.object(acq, key, value) for key, value in module.items()]
        started += [mock.patch.object(acq.recovery_carrier, key, value) for key, value in carrier.items()]
        started += [mock.patch.object(acq.job_contract, "guarded", self.guarded),
                    mock.patch("helpers_mb._fetch_mb_release_group_strict", self.rg_strict)]
        return started


def _error_chain(exc):
    chain = []
    while exc is not None:
        chain.append([type(exc).__name__, str(exc)])
        exc = exc.__cause__
    return chain


def run_scenario(cfg):
    """Run one scenario in a fresh world; the JSON-safe trace with paths normalized."""
    tmp = tempfile.mkdtemp(prefix="acqchar")
    world = World(tmp, cfg)
    jobs = mock.Mock()
    jobs.start_python.return_value.job_id = "job-1"
    trace = {}
    patches = world.patches() + [mock.patch.object(acq, "jobs", jobs)]
    for patch in patches:
        patch.start()
    try:
        if cfg.get("cancel_at_start"):
            world.cancel.set()
        try:
            body, status = acq.start_album_download(copy.deepcopy(cfg["payload"]))
            trace["http"] = [body, status]
        except Exception as exc:
            trace["start_error"] = _error_chain(exc)
            return _normalize(trace, tmp)
        if jobs.start_python.called:
            args, kwargs = jobs.start_python.call_args
            trace["job"] = world.clean(kwargs)
            trace["guard"] = world.clean(world.guard_kwargs)
            cancel = None if cfg.get("no_cancel") else world.cancel
            outcomes = []
            for _run in range(cfg.get("runs", 1)):
                try:
                    outcomes.append({"result": world.clean(args[0](world.log, cancel))})
                except Exception as exc:
                    outcomes.append({"error": _error_chain(exc)})
            trace["outcomes"] = outcomes
        trace["events"] = world.events
        trace["log"] = world.log
        trace["carriers"] = world.clean(world.carriers)
        return _normalize(trace, tmp)
    finally:
        for patch in reversed(patches):
            patch.stop()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def _normalize(trace, tmp):
    text = json.dumps(trace, sort_keys=True, indent=1, ensure_ascii=False)
    for path in {tmp, os.path.realpath(tmp)}:
        text = text.replace(path.replace("\\", "\\\\"), "<TMP>")
    return json.loads(text)


class AlbumDownloadCharacterizationTests(unittest.TestCase):
    maxDiff = None

    def test_golden_traces(self):
        actual = {name: run_scenario(cfg) for name, cfg in SCENARIOS.items()}
        if os.environ.get("UPDATE_ALBUM_DOWNLOAD_GOLDEN") == "1":
            GOLDEN.parent.mkdir(exist_ok=True)
            GOLDEN.write_text(json.dumps(actual, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(sorted(golden), sorted(actual))
        for name in SCENARIOS:
            with self.subTest(scenario=name):
                self.assertEqual(golden[name], actual[name])


if __name__ == "__main__":
    unittest.main()
