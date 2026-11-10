"""Characterization of the two import jobs (modernization M4, #505).

Each scenario drives the real ``start_reimport_disk`` / ``start_folder_import_with_id``
with every collaborator replaced by a recording spy. The HTTP result, job
label and metadata, job result or error, and one interleaved trace of every
collaborator call and log line are compared with
``tests/fixtures/import_jobs_golden.json``, which was generated from the code
BEFORE the jobs were split into steps. The trace order is the point: identity,
transaction and review gates must run in exactly this order.

Regenerate only for a deliberate behavior change:
    UPDATE_IMPORT_GOLDEN=1 python -m unittest tests.test_import_jobs_characterization
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

import backend.import_service as isvc
from backend.beets_adapter import BeetsAuthError, BeetsError, BeetsUnavailableError

GOLDEN = Path(__file__).parent / "fixtures" / "import_jobs_golden.json"
REL = "11111111-1111-1111-1111-111111111111"
REL2 = "44444444-4444-4444-4444-444444444444"
RG = "22222222-2222-2222-2222-222222222222"
RG2 = "33333333-3333-3333-3333-333333333333"
WANTED = [{"disc": 1, "track": 2, "title": "Two", "mb_trackid": "rec-2"}]
WANTED2 = WANTED + [{"disc": 1, "track": 3, "title": "Three", "mb_trackid": "rec-3"}]
LOOSE = {"ok": True, "matches": 1, "expected": 3}
FULL = {"ok": True, "matches": 3, "expected": 3}
BAD = {"ok": False, "matches": 1, "expected": 3}


def _sorted_listing(original):
    """The filesystem picks the listing order, and the jobs log names in that order."""
    return lambda self, *args, **kwargs: iter(sorted(original(self, *args, **kwargs)))


class Seq(list):
    """Successive return values of a spy; the last one repeats."""


class P(str):
    """A path return value; {SRC} and {TMP} are filled in."""


class Log(list):
    def __init__(self, world):
        super().__init__()
        self.world = world

    def append(self, line):
        super().append(line)
        self.world.trace.append(["log", self.world.clean(line)])
        if self.world.cancel_on_log and self.world.cancel_on_log in str(line):
            self.world.cancel.set()


class World:
    """A fake world around one job: every collaborator records and answers."""

    def __init__(self, tmp, source="downloads", files=(), overrides=None, cancel=False, cancel_on_log="",
                 no_dir=False):
        self.tmp = str(tmp)
        root = Path(tmp) / ("music" if source == "music" else "downloads")
        self.music_root = Path(tmp) / "music"
        self.src = str(root / "Artist" / "Album (2001)")
        if not no_dir:
            Path(self.src).mkdir(parents=True)
            for name in files:
                target = Path(self.src) / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"x")
        self.overrides = overrides or {}
        self.trace = []
        self.cancel = threading.Event()
        if cancel:
            self.cancel.set()
        self.cancel_on_log = cancel_on_log
        self.job = {}
        self.thread = threading.current_thread()

    def clean(self, value):
        if isinstance(value, Log):
            return "<log>"
        if isinstance(value, threading.Event):
            return "<cancel>"
        if isinstance(value, Path):
            return self.clean(str(value))
        if isinstance(value, str):
            return value.replace(self.tmp, "<TMP>")
        if isinstance(value, dict):
            return {str(k): ("<time>" if k == "completed_at" else self.clean(v)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        if isinstance(value, (set, frozenset)):
            return sorted(self.clean(v) for v in value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return f"<{type(value).__name__}>"

    def fill(self, value):
        if isinstance(value, P):
            return Path(value.replace("{SRC}", self.src).replace("{TMP}", self.tmp))
        if isinstance(value, str):
            return value.replace("{SRC}", self.src).replace("{TMP}", self.tmp)
        if isinstance(value, dict):
            return {k: self.fill(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.fill(v) for v in value]
        return value

    def spy(self, name, default=None):
        state = {"calls": 0}

        def call(*args, **kwargs):
            self.trace.append(["call", name, self.clean(list(args)), self.clean(kwargs)])
            answer = self.overrides.get(name, default)
            if isinstance(answer, Seq):
                answer = answer[min(state["calls"], len(answer) - 1)]
                state["calls"] += 1
            if isinstance(answer, BaseException):
                raise answer
            if callable(answer):
                return answer(*args, **kwargs)
            return self.fill(copy.deepcopy(answer))
        return call

    def held(self, name, **_kwargs):
        world = self

        class _Held:
            def __enter__(self):
                world.trace.append(["held-enter", name])

            def __exit__(self, *_exc):
                world.trace.append(["held-exit", name])
                return False
        return _Held()

    def start_python(self, fn, label="", metadata=None):
        # jobs is shared: the app's recovery schedule can tick while this patch is on.
        if threading.current_thread() is not self.thread:
            return None
        self.job = {"fn": fn, "label": self.clean(label), "metadata": self.clean(metadata)}
        return types.SimpleNamespace(job_id="job-1")

    def patches(self):
        cw_defaults = {
            "inspect_import_source": lambda path, operation=None: {"ok": True, "path": path, "audio_file_count": 2},
            "get_album": lambda aid: {"id": aid, "albumartist": "Artist", "mb_albumid": REL, "mb_releasegroupid": RG},
            "find_all_albums_by_mb_albumid": [],
            "find_albums_by_query": [],
            "find_all_items_by_album_id": [],
            "plan_album_mb_track_repair": {"ok": True, "operation_id": "repair-1"},
            "apply_album_mb_track_repair": {"ok": True},
            "update_album_metadata": {"ok": True},
            "relocate_album": {"ok": True},
            "plan_confirmed_import": {"ok": True, "operation_id": "op-1"},
            "apply_confirmed_import": {"ok": True, "album_id": 9, "mb_releasegroupid": RG},
            "fetch_and_embed_album_art": {"ok": True},
            "delete_file": {"ok": True},
            "plan_folder_cleanup": {"ok": True, "operation_id": "clean-1"},
            "apply_folder_cleanup": {"ok": True},
        }
        svc_defaults = {
            "_library_album_ids_for_folder": [],
            "_resolve_album_release_for_import": REL,
            "_folder_release_preflight": dict(LOOSE),
            "_ai_match_evidence_packet": {"packet": True},
            "_build_folder_evidence": {"folder": True},
            "_queue_folder_for_manual_review": True,
            "_source_audio_missing_track_scan": {"ok": False},
            "_stage_selected_audio_files": "{TMP}/staged",
            "_recovery_rebind_restaged": None,
            "_mb_release_track_count": 3,
            "_match_tracks_from_mb": 3,
            "_strip_year_from_album_name": None,
            "_repair_album_mbid_sticking_once": None,
            "_remove_pending_review_for_path": None,
            "_invalidate_lib_cache": None,
            "_trigger_plex_refresh": None,
            "_album_mb_completeness": {"missing_count": 0, "in_library": 3, "expected_count": 3},
            "_prune_stale_wanted_rows_before_import": None,
            "_validate_import_source_audio": None,
            "_fetch_mb_release_tracklist": {"ok": True, "release_group": RG, "artist": "Artist",
                                            "title": "Album", "tracks": []},
            "_preserve_torrent_source_path": False,
            "_validate_wanted_album_items_with_acoustid": {"ok": True},
            "_recovery_slot_authorized": False,
            "_merge_imported_album_into_existing": 5,
            "_record_recent_import_for_album": None,
            "_recovery_plex_sync_leg": None,
            "_resolve_import_review_source_path": lambda raw, **_k: (Path(raw), None),
            "_recovery_auto_insert_authorized": {"ok": False, "reason": "refused", "code": "not_allowed"},
            "_import_review_selected_source_files": [],
            "_delete_source_if_proven_in_library": None,
            "_start_reimport_disk_job_internal": "child-1",
            "_wait_for_child_job": None,
            "_filter_import_review_selected_audio_files": lambda files, _log, **_k: list(files),
            "_beet_import_timeout": 300,
            "_remaining_audio_files": [],
            "_move_artwork_to_target": None,
            "_mark_pending_review_status": None,
            "_auto_merge_case_duplicate_artist_folder": None,
            "_record_ai_match": None,
            "_import_review_auto_update": None,
            "_is_music_format_policy_handled_error": False,
            "_finalize_pending_review_format_policy_rejection": {"status": "format_policy_rejected", "note": "n"},
        }
        patches = [mock.patch.object(isvc.composite_workflows, name, self.spy(f"cw.{name}", default))
                   for name, default in cw_defaults.items()]
        patches += [mock.patch.object(isvc, name, self.spy(name, default)) for name, default in svc_defaults.items()]
        patches += [
            mock.patch.object(Path, "iterdir", _sorted_listing(Path.iterdir)),
            mock.patch.object(Path, "rglob", _sorted_listing(Path.rglob)),
            mock.patch.object(isvc, "_IMPORT_JOB_LOCK", self.held("import-job-lock")),
            mock.patch.object(isvc, "MUSIC_ROOT", self.music_root),
            # The selected files here are not on disk; F8 is covered by
            # test_review_partial_import_torrent_preserve.
            mock.patch.object(isvc, "_selected_sources_need_copy", return_value=False),
            mock.patch.object(isvc.jobs, "start_python", self.start_python),
            mock.patch.object(isvc.job_contract, "held", self.held),
            mock.patch.object(isvc.recovery_carrier, "load_for_folder", self.spy("carrier.load_for_folder")),
            mock.patch.object(isvc, "lib", types.SimpleNamespace(get_album=self.spy(
                "lib.get_album", lambda _aid: types.SimpleNamespace(albumartist="Artist")))),
        ]
        return patches

    def run(self, kind, payload):
        entry = isvc.start_reimport_disk if kind == "reimport" else isvc.start_folder_import_with_id
        patchers = self.patches()
        for patcher in patchers:
            patcher.start()
        try:
            body, status = entry(self.fill(payload))
            record = {"http": [self.clean(body), status]}
            if self.job:
                record.update(label=self.job["label"], metadata=self.job["metadata"])
                log = Log(self)
                try:
                    record["result"] = self.clean(self.job["fn"](log, self.cancel))
                except Exception as ex:
                    record["error"] = [type(ex).__name__, self.clean(str(ex))]
            record["trace"] = self.trace
            return record
        finally:
            for patcher in patchers:
                patcher.stop()


def _reimport(**payload):
    return {"aldir": "{SRC}", "mb_albumid": REL, "skip_import_lock": True, **payload}


def _existing(**payload):
    return _reimport(existing_album_id=5, **payload)


def _fill(**payload):
    return _existing(wanted_tracks=WANTED, **payload)


def _with_id(**payload):
    return {"path": "{SRC}", "mb_albumid": REL, "mb_releasegroupid": RG, **payload}


_DIAG = BeetsError("apply rejected")
_DIAG.diagnostics = {"returncode": 2, "stdout_excerpt": "out", "stderr_excerpt": "err"}
_ITEM92 = [{"id": 92, "disc": 1, "track": 2, "mb_trackid": "rec-2"}]
_ITEM51 = [{"id": 51, "disc": 1, "track": 1, "mb_trackid": "rec-1"}]
_SCAN_USEFUL = {"ok": True, "useful_files": ["{SRC}/02.flac"], "duplicate_files": [], "unknown_files": ["{SRC}/x.flac"],
                "missing_count": 1, "wanted_tracks": WANTED, "artist": "Scan Artist", "album": "Scan Album"}
_SCAN_GAP = {"ok": True, "useful_files": [], "duplicate_files": [], "unknown_files": [], "missing_count": 2,
             "in_library": 8, "expected_count": 10}
_SCAN_GAP_UNKNOWN = dict(_SCAN_GAP, unknown_files=["{SRC}/x.flac"])
_NOFILES = {"ok": True, "useful_files": [], "duplicate_files": [], "unknown_files": [], "missing_count": 0}

REIMPORT_SCENARIOS = {
    # ── request validation ──
    "missing_aldir": dict(payload={"mb_albumid": REL}),
    "missing_mbid": dict(payload={"aldir": "{SRC}"}),
    "rg_only_payload": dict(payload={"aldir": "{SRC}", "mb_releasegroupid": RG, "skip_import_lock": True}),
    "engine_auth_failed": dict(overrides={"cw.inspect_import_source": BeetsAuthError("no")}),
    "engine_unavailable": dict(overrides={"cw.inspect_import_source": BeetsUnavailableError("no")}),
    "source_known_error": dict(overrides={"cw.inspect_import_source": BeetsError("invalid_path")}),
    "source_root_rejected": dict(overrides={"cw.inspect_import_source": BeetsError("root_self_rejected")}),
    "source_unknown_error": dict(overrides={"cw.inspect_import_source": BeetsError("<html>trace</html>")}),
    "source_not_ok": dict(overrides={"cw.inspect_import_source": {"ok": False}}),
    "folder_owner_single": dict(overrides={"_library_album_ids_for_folder": [7]}),
    "folder_owner_ambiguous": dict(overrides={"_library_album_ids_for_folder": [7, 8]}),
    "folder_owner_lookup_fails": dict(overrides={"_library_album_ids_for_folder": RuntimeError("db")}),
    "payload_flags_and_ids": dict(payload=_existing(replace_existing_item_ids=[3, "x", 0, "4"], albumartist="Other [mb]",
                                                    strict_edition_guard=True)),
    "bad_existing_id": dict(payload=_reimport(existing_album_id="nope")),
    # ── locking ──
    "lock_default_path": dict(payload={"aldir": "{SRC}", "mb_albumid": REL}),
    "lock_cancelled_in_slot": dict(payload={"aldir": "{SRC}", "mb_albumid": REL}, cancel=True),
    "skip_lock_cancelled": dict(cancel=True),
    # ── fresh import ──
    "fresh_download": dict(files=["01.flac"]),
    "fresh_torrent_source": dict(overrides={"_preserve_torrent_source_path": True}),
    "fresh_template_tokens": dict(files=["A - %02i{$track} - T.flac", "ok.flac", "note.txt"]),
    "token_scan_fails": dict(no_dir=True),
    "cancel_before_import": dict(cancel_on_log="[1/3] Importing"),
    "planned_rg_missing": dict(overrides={"_fetch_mb_release_tracklist": {"ok": True, "release_group": ""}}),
    "source_audio_rejected": dict(overrides={"_validate_import_source_audio": RuntimeError("policy")}),
    "not_imported_review": dict(overrides={"cw.apply_confirmed_import": {"ok": False, "code": "not_imported"}}),
    "not_imported_no_review": dict(payload=_reimport(queue_review=False),
                                   overrides={"cw.apply_confirmed_import": {"ok": False, "code": "not_imported"}}),
    "verify_failed_rows_kept": dict(overrides={"cw.apply_confirmed_import": {
        "ok": False, "code": "release_group_mismatch", "error": "wrong RG", "album_ids": [9, 10]}}),
    "verify_failed_no_entry": dict(overrides={"_queue_folder_for_manual_review": False, "cw.apply_confirmed_import": {
        "ok": False, "album_ids": [9]}}),
    "import_failed_error": dict(overrides={"cw.apply_confirmed_import": {"ok": False, "error": "boom"}}),
    "import_failed_silent": dict(overrides={"cw.apply_confirmed_import": {"ok": False}}),
    "plan_without_operation": dict(overrides={"cw.plan_confirmed_import": {"ok": False}}),
    "art_not_ok": dict(overrides={"cw.fetch_and_embed_album_art": {"ok": False, "error": "no art"}}),
    "art_raises": dict(overrides={"cw.fetch_and_embed_album_art": RuntimeError("art down")}),
    "plex_leg_carrier_fails": dict(overrides={"carrier.load_for_folder": RuntimeError("store")}),
    "plex_leg_carrier_present": dict(overrides={"carrier.load_for_folder": {"id": "c1"}}),
    # ── forced album artist ──
    "forced_artist_set": dict(payload=_reimport(albumartist="New Artist")),
    "forced_artist_same": dict(payload=_reimport(albumartist="Artist")),
    "forced_artist_update_fails": dict(payload=_reimport(albumartist="New Artist"),
                                       overrides={"cw.update_album_metadata": {"ok": False}}),
    "forced_artist_relocate_fails": dict(payload=_reimport(albumartist="New Artist"),
                                         overrides={"cw.relocate_album": {"ok": False}}),
    # ── existing album lookup ──
    "existing_gone_replaced_by_mbid": dict(payload=_existing(), overrides={
        "cw.get_album": Seq([None, {"id": 6, "mb_albumid": REL, "mb_releasegroupid": RG}]),
        "cw.find_all_albums_by_mb_albumid": [{"id": 6}]}),
    "existing_gone_replaced_by_query": dict(payload=_existing(), overrides={
        "cw.get_album": Seq([None, {"id": 6, "mb_albumid": REL, "mb_releasegroupid": RG}]),
        "cw.find_albums_by_query": [{"id": 6}]}),
    "existing_gone_new_import": dict(payload=_existing(), overrides={"cw.get_album": None}),
    "existing_lookup_raises": dict(payload=_existing(), overrides={"cw.get_album": Seq([
        RuntimeError("engine"), {"id": 5, "mb_albumid": REL, "mb_releasegroupid": RG}])}),
    # ── release resolution ──
    "resolve_changes_release": dict(overrides={"_resolve_album_release_for_import": REL2}),
    "resolve_failed_review": dict(overrides={"_resolve_album_release_for_import": ""}),
    "resolve_failed_no_review": dict(payload=_reimport(queue_review=False),
                                     overrides={"_resolve_album_release_for_import": ""}),
    "resolve_failed_light_confirm": dict(payload=_reimport(queue_review=False, light_confirm=True),
                                         overrides={"_resolve_album_release_for_import": ""}),
    "resolve_failed_carrier_lookup_fails": dict(overrides={"_resolve_album_release_for_import": "",
                                                           "carrier.load_for_folder": RuntimeError("store")}),
    "resolve_failed_non_uuid": dict(payload=_reimport(mb_albumid="not-a-uuid"),
                                    overrides={"_resolve_album_release_for_import": ""}),
    "resolve_failed_preflight_reason": dict(payload=_fill(), overrides={
        "_resolve_album_release_for_import": "",
        "_folder_release_preflight": {"ok": False, "missing_track_proof": {"reason_code": "proof_failed"},
                                      "review_reason": "Proof failed."}}),
    "resolve_failed_preflight_raises": dict(overrides={"_resolve_album_release_for_import": "",
                                                       "_folder_release_preflight": RuntimeError("mb")}),
    # ── preflight ──
    "preflight_logs_everything": dict(payload=_existing(), overrides={"_folder_release_preflight": {
        "ok": True, "matches": 1, "expected": 3, "release_title": "T", "release_artist": "A", "folder_artist": "B",
        "artist_score": 0.5, "acoustid_top_release": "R", "acoustid_mismatch": True, "acoustid_target_hits": 1,
        "acoustid_top_hits": 4, "examples": ["  ex1", "  ex2"]}}),
    "preflight_acoustid_checked": dict(source="music", overrides={"_folder_release_preflight": {
        "ok": True, "matches": 1, "expected": 3, "acoustid_top_release": "R"}}),
    "preflight_failed_review": dict(payload=_existing(), overrides={"_folder_release_preflight": BAD}),
    "preflight_failed_wanted_review": dict(payload=_fill(), overrides={"_folder_release_preflight": BAD}),
    "preflight_failed_artist_review": dict(source="music", overrides={"_folder_release_preflight": dict(
        BAD, artist_ok=False, release_artist="A", folder_artist="B", artist_score=0.1)}),
    "preflight_failed_no_review": dict(payload=_existing(queue_review=False),
                                       overrides={"_folder_release_preflight": BAD}),
    "manual_override_repairs_existing": dict(payload=_existing(manual_mbid_override=True), overrides={
        "_folder_release_preflight": BAD, "_match_tracks_from_mb": 1}),
    "manual_override_library_import": dict(source="music", payload=_reimport(force_mbid=True),
                                           overrides={"_folder_release_preflight": BAD}),
    "music_library_fresh_in_place": dict(source="music", files=["01.flac"]),
    # ── staged missing-track fill scan ──
    "replacement_mode_logs": dict(payload=_fill(replace_existing=True)),
    "fill_scan_useful_restaged": dict(payload=_fill(), overrides={"_source_audio_missing_track_scan": _SCAN_USEFUL,
                                                                  "cw.find_all_items_by_album_id": Seq([_ITEM92, _ITEM51 + _ITEM92])}),
    "fill_scan_same_dir": dict(payload=_fill(), overrides={
        "_source_audio_missing_track_scan": _SCAN_USEFUL, "_stage_selected_audio_files": "{SRC}",
        "cw.find_all_items_by_album_id": Seq([_ITEM92, _ITEM51 + _ITEM92])}),
    "fill_scan_useful_saves_failed_preflight": dict(payload=_fill(), overrides={
        "_folder_release_preflight": BAD, "_source_audio_missing_track_scan": dict(_SCAN_USEFUL, unknown_files=[]),
        "cw.find_all_items_by_album_id": Seq([_ITEM92, _ITEM51 + _ITEM92])}),
    "fill_scan_no_useful": dict(payload=_fill(), overrides={"_source_audio_missing_track_scan": _NOFILES,
                                                            "cw.find_all_items_by_album_id": Seq([_ITEM92, _ITEM51 + _ITEM92])}),
    # ── complete existing album repair ──
    "complete_repair": dict(payload=_existing(), overrides={"_folder_release_preflight": FULL}),
    "complete_repair_unimported_files": dict(
        payload=_existing(), files=["a.flac", "b.mp3", "c.flac", "d.flac", "note.txt"],
        overrides={"_folder_release_preflight": FULL, "cw.find_all_items_by_album_id": [{"path": "/elsewhere/z.flac"}]}),
    "complete_repair_few_unimported": dict(payload=_existing(), files=["a.flac"],
                                           overrides={"_folder_release_preflight": FULL}),
    "complete_repair_all_in_db": dict(payload=_existing(), source="music", files=["a.flac"], overrides={
        "_folder_release_preflight": FULL, "_source_audio_missing_track_scan": _NOFILES,
        "cw.find_all_items_by_album_id": lambda _aid: [{"path": str(isvc.MUSIC_ROOT / "Artist/Album (2001)/a.flac")}]}),
    "complete_repair_relative_item_path": dict(payload=_existing(), source="music", files=["a.flac"], overrides={
        "_folder_release_preflight": FULL, "cw.find_all_items_by_album_id": [{"path": "Artist/Album (2001)/a.flac"}]}),
    "complete_repair_items_lookup_fails": dict(payload=_existing(), files=["a.flac"], overrides={
        "_folder_release_preflight": FULL, "cw.find_all_items_by_album_id": RuntimeError("engine")}),
    "complete_repair_scan_fails": dict(payload=_existing(), no_dir=True, overrides={"_folder_release_preflight": FULL}),
    "complete_album_incomplete": dict(payload=_existing(), overrides={
        "_folder_release_preflight": FULL,
        "_album_mb_completeness": {"missing_count": 2, "in_library": 1, "expected_count": 3}}),
    "complete_check_raises": dict(payload=_existing(), overrides={
        "_folder_release_preflight": FULL, "_album_mb_completeness": RuntimeError("mb")}),
    "replace_skips_repair_shortcut": dict(payload=_existing(replace_existing=True),
                                          overrides={"_folder_release_preflight": FULL}),
    # ── in-place repair failures (the completeness try keeps them as warnings) ──
    "repair_no_expected_tracks": dict(payload=_existing(), overrides={
        "_folder_release_preflight": FULL, "_mb_release_track_count": 0}),
    "repair_match_lookup_failed": dict(payload=_existing(), overrides={
        "_folder_release_preflight": FULL, "_match_tracks_from_mb": -1}),
    "repair_too_few_matched": dict(payload=_existing(), overrides={
        "_folder_release_preflight": FULL, "_match_tracks_from_mb": 1}),
    "repair_plan_fails": dict(payload=_existing(), overrides={
        "_folder_release_preflight": FULL, "cw.plan_album_mb_track_repair": {"ok": False}}),
    "repair_apply_fails": dict(payload=_existing(), overrides={
        "_folder_release_preflight": FULL, "cw.apply_album_mb_track_repair": {"ok": False}}),
    "repair_update_fails": dict(payload=_existing(), overrides={
        "_folder_release_preflight": FULL, "cw.update_album_metadata": {"ok": False}}),
    "repair_relocate_fails": dict(payload=_existing(), overrides={
        "_folder_release_preflight": FULL, "cw.relocate_album": {"ok": False}}),
    "manual_repair_few_matched_continues": dict(payload=_existing(manual_mbid_override=True), overrides={
        "_folder_release_preflight": BAD, "_match_tracks_from_mb": 0}),
    "manual_repair_failure_is_fatal": dict(payload=_existing(manual_mbid_override=True), overrides={
        "_folder_release_preflight": BAD, "_mb_release_track_count": 0}),
    # ── existing album inside the library ──
    "library_scan_useful_restaged": dict(source="music", payload=_existing(), overrides={
        "_source_audio_missing_track_scan": _SCAN_USEFUL}),
    "library_scan_stage_refused": dict(source="music", payload=_existing(), overrides={
        "_source_audio_missing_track_scan": _SCAN_USEFUL, "_stage_selected_audio_files": "{SRC}"}),
    "library_scan_failed": dict(source="music", payload=_existing()),
    "library_scan_nothing_missing": dict(source="music", payload=_existing(), overrides={
        "_source_audio_missing_track_scan": _NOFILES}),
    "library_partial_repair": dict(source="music", payload=_existing(), overrides={
        "_folder_release_preflight": {"ok": True, "matches": 8, "expected": 10},
        "_source_audio_missing_track_scan": _SCAN_GAP}),
    "library_partial_repair_unknown": dict(source="music", payload=_existing(), overrides={
        "_folder_release_preflight": {"ok": True, "matches": 8, "expected": 10},
        "_source_audio_missing_track_scan": _SCAN_GAP_UNKNOWN}),
    "library_partial_at_boundary": dict(source="music", payload=_existing(), overrides={
        "_folder_release_preflight": {"ok": True, "matches": 80, "expected": 100},
        "_source_audio_missing_track_scan": _SCAN_GAP}),
    "library_partial_just_below_boundary": dict(source="music", payload=_existing(), overrides={
        "_folder_release_preflight": {"ok": True, "matches": 79, "expected": 100},
        "_source_audio_missing_track_scan": _SCAN_GAP}),
    "library_partial_repair_too_few": dict(source="music", payload=_existing(), overrides={
        "_folder_release_preflight": {"ok": True, "matches": 8, "expected": 10}, "_match_tracks_from_mb": 7,
        "_mb_release_track_count": 10, "_source_audio_missing_track_scan": _SCAN_GAP}),
    "library_gap_unknown_review": dict(source="music", payload=_existing(), overrides={
        "_source_audio_missing_track_scan": _SCAN_GAP_UNKNOWN}),
    "library_gap_nothing_useful": dict(source="music", payload=_existing(), overrides={
        "_source_audio_missing_track_scan": _SCAN_GAP}),
    "library_gap_zero_expected": dict(source="music", payload=_existing(), overrides={
        "_folder_release_preflight": {"ok": True, "matches": 0, "expected": 0},
        "_source_audio_missing_track_scan": _SCAN_GAP}),
    "library_wanted_scan_targets": dict(source="music", payload=_fill(), overrides={
        "_source_audio_missing_track_scan": _SCAN_USEFUL,
        "cw.find_all_items_by_album_id": Seq([_ITEM92, _ITEM51 + _ITEM92])}),
    # ── merge after import ──
    "merge_plain_existing": dict(payload=_existing()),
    "merge_replace_keeps_row": dict(payload=_fill(replace_existing_item_ids=[51]), overrides={
        "_merge_imported_album_into_existing": 9}),
    "merge_slot_authorized": dict(payload=_fill(), overrides={
        "_recovery_slot_authorized": True, "cw.find_all_items_by_album_id": Seq([_ITEM92, _ITEM51 + _ITEM92])}),
    "fill_verified": dict(payload=_fill(), overrides={
        "cw.find_all_items_by_album_id": Seq([_ITEM92, _ITEM51 + _ITEM92])}),
    "fill_undelivered_reported": dict(payload=_existing(wanted_tracks=WANTED2), overrides={
        "cw.find_all_items_by_album_id": Seq([_ITEM92, _ITEM51 + _ITEM92])}),
    "fill_undelivered_after_scan": dict(payload=_existing(wanted_tracks=WANTED2), overrides={
        "_source_audio_missing_track_scan": _SCAN_USEFUL,
        "cw.find_all_items_by_album_id": Seq([_ITEM92, _ITEM51 + _ITEM92])}),
    "fill_match_by_recording_only": dict(
        payload=_existing(wanted_tracks=[{"title": "Two", "mb_trackid": "rec-2"}]),
        overrides={"cw.find_all_items_by_album_id": Seq([_ITEM92, _ITEM51 + _ITEM92])}),
    "fill_acoustid_refused": dict(payload=_fill(), overrides={
        "_validate_wanted_album_items_with_acoustid": {"ok": False, "reason": "fingerprint mismatch"}}),
    "fill_acoustid_refused_no_reason": dict(payload=_fill(), overrides={
        "_validate_wanted_album_items_with_acoustid": {"ok": False}}),
    "fill_imported_read_fails": dict(payload=_fill(), overrides={"cw.find_all_items_by_album_id": RuntimeError("io")}),
    "fill_merge_fails": dict(payload=_fill(), overrides={
        "cw.find_all_items_by_album_id": _ITEM92,
        "_merge_imported_album_into_existing": isvc._ExistingAlbumMergeFailed("tx failed")}),
    "fill_readback_fails": dict(payload=_fill(), overrides={
        "cw.find_all_items_by_album_id": Seq([_ITEM92, RuntimeError("io")])}),
    "fill_not_on_existing": dict(payload=_fill(), overrides={"cw.find_all_items_by_album_id": Seq([_ITEM92, _ITEM51])}),
    "fill_merge_returns_other_album": dict(payload=_fill(), overrides={
        "_merge_imported_album_into_existing": 9, "cw.find_all_items_by_album_id": Seq([_ITEM92, _ITEM51 + _ITEM92])}),
    "fill_nothing_delivered": dict(payload=_fill(), overrides={"cw.find_all_items_by_album_id": Seq([_ITEM51, _ITEM51])}),
    "merged_identity_mismatch": dict(payload=_existing(), overrides={
        "cw.get_album": lambda aid: {"id": aid, "mb_albumid": REL2, "mb_releasegroupid": RG}}),
    "merged_rg_mismatch": dict(payload=_existing(), overrides={
        "cw.get_album": lambda aid: {"id": aid, "mb_albumid": REL, "mb_releasegroupid": RG2}}),
    "merged_album_missing": dict(payload=_existing(), overrides={"cw.get_album": Seq([{"id": 5}, None])}),
    "merge_forced_artist": dict(payload=_existing(albumartist="New Artist")),
}

_SELECT = {"_import_review_selected_source_files": [P("{SRC}/a.flac"), P("{SRC}/b.flac")]}
_MAPPED = [{"status": "matched", "source": "a.flac"}]
_AI = {"mb_albumid": REL, "mb_releasegroupid": RG, "confidence": "high", "album": "Sug", "albumartist": "Sug Artist"}
_AUTO = {"auto_import_idempotency_key": "key-1", "review_item_id": "r1"}
_HANDOFF_PRE = {"_library_album_ids_for_folder": [5], "_folder_release_preflight": {"ok": False, "matches": 0, "expected": 3}}
_SCAN_DUP = {"ok": True, "useful_files": [], "duplicate_files": ["{SRC}/a.flac"], "unknown_files": ["{SRC}/x.flac"]}
_CARRIER = {"id": "c1", "existing_album_id": 5, "wanted_tracks": WANTED}
_GATE_OK = {"ok": True, "carrier": {"mb_releasegroupid": RG, "release_id": REL, "existing_album_id": 5,
                                    "expected_tracks": WANTED}}

WITH_ID_SCENARIOS = {
    # ── request validation ──
    "missing_path": dict(payload={"mb_albumid": REL}),
    "non_dict_payload": dict(payload=[]),
    "path_refused": dict(overrides={"_resolve_import_review_source_path": (None, "Not allowed.")}),
    "path_refused_silent": dict(overrides={"_resolve_import_review_source_path": (None, None)}),
    "missing_id": dict(payload={"path": "{SRC}"}),
    "invalid_id": dict(payload={"path": "{SRC}", "mb_albumid": "junk"}),
    "mapping_without_files": dict(payload=_with_id(track_mapping=_MAPPED)),
    "mapping_with_files": dict(payload=_with_id(track_mapping=_MAPPED), overrides=_SELECT),
    "mapping_unmatched_rows": dict(payload=_with_id(track_mapping=[{"status": "extra"}, "bad"])),
    "bad_existing_id": dict(payload=_with_id(existing_album_id="nope")),
    "replace_ids_ignored_in_job": dict(payload=_with_id(replace_existing_item_ids=[3, "x", 0])),
    "metadata_flags": dict(payload=_with_id(trigger_plex=True, plex_context="Playlist", auto_import=True, move=True,
                                            **_AUTO)),
    "folder_owner_single": dict(overrides={"_library_album_ids_for_folder": [7]}),
    "folder_owner_lookup_fails": dict(overrides={"_library_album_ids_for_folder": RuntimeError("db")}),
    # ── recovery gate and re-entry ──
    "recovery_refused": dict(payload=_with_id(recovery_carrier_id="c1")),
    "recovery_refused_silent": dict(payload=_with_id(recovery_carrier_id="c1"),
                                    overrides={"_recovery_auto_insert_authorized": {"ok": False}}),
    "recovery_authorized_rebinds": dict(payload=_with_id(recovery_carrier_id="c1", mb_albumid=REL2, existing_album_id=8),
                                        overrides={"_recovery_auto_insert_authorized": _GATE_OK,
                                                   "_source_audio_missing_track_scan": _NOFILES}),
    "recovery_authorized_rg_only": dict(payload=_with_id(recovery_carrier_id="c1"), overrides={
        "_recovery_auto_insert_authorized": {"ok": True, "carrier": {"mb_releasegroupid": RG, "existing_album_id": 0}}}),
    "recovery_authorized_rg_url": dict(payload=_with_id(recovery_carrier_id="c1"), overrides={
        "_recovery_auto_insert_authorized": {"ok": True, "carrier": {
            "mb_releasegroupid": "", "release_id": f"https://musicbrainz.org/release-group/{RG}"}}}),
    "recovery_authorized_rg_url_only": dict(payload={"path": "{SRC}", "mb_albumid": REL, "recovery_carrier_id": "c1"}, overrides={
        "_recovery_auto_insert_authorized": {"ok": True, "carrier": {
            "mb_releasegroupid": "", "release_id": f"https://musicbrainz.org/release-group/{RG}"}}}),
    "reentry_merge_lookup_fails": dict(payload=_with_id(existing_album_id=5, wanted_tracks=WANTED),
                                       overrides={"carrier.load_for_folder": RuntimeError("store")}),
    "reentry_recovers_identity": dict(overrides={"carrier.load_for_folder": _CARRIER,
                                                 "_source_audio_missing_track_scan": _SCAN_USEFUL}),
    "reentry_lookup_fails": dict(overrides={"carrier.load_for_folder": RuntimeError("store")}),
    "reentry_other_album_keeps_generic": dict(payload=_with_id(existing_album_id=6),
                                              overrides={"carrier.load_for_folder": _CARRIER}),
    "reentry_handoff_declines": dict(overrides={"carrier.load_for_folder": _CARRIER}),
    "reentry_keeps_given_tracks": dict(payload=_with_id(wanted_tracks=WANTED2), overrides={
        "carrier.load_for_folder": _CARRIER, "_source_audio_missing_track_scan": _NOFILES}),
    # ── release-group input ──
    "rg_only_resolved": dict(payload={"path": "{SRC}", "mb_releasegroupid": RG}),
    "rg_url_in_release_field": dict(payload={"path": "{SRC}", "mb_albumid": f"https://musicbrainz.org/release-group/{RG}"}),
    "rg_unresolved_review": dict(payload={"path": "{SRC}", "mb_releasegroupid": RG}, overrides={
        "_resolve_album_release_for_import": lambda *a, **_k: a[5].extend(["x REFUSED one", "plain", "y WARN two"]) or ""}),
    "rg_unresolved_no_review": dict(payload={"path": "{SRC}", "mb_releasegroupid": RG, "queue_review": False},
                                    overrides={"_resolve_album_release_for_import": ""}),
    "rg_unresolved_light_confirm": dict(payload={"path": "{SRC}", "mb_releasegroupid": RG, "queue_review": False,
                                                 "light_confirm": True, "ai_suggestion": _AI},
                                        overrides={"_resolve_album_release_for_import": ""}),
    # ── identity gates before import ──
    "release_load_fails": dict(overrides={"_fetch_mb_release_tracklist": {"ok": False}}),
    "release_in_other_group": dict(overrides={"_fetch_mb_release_tracklist": {"ok": True, "release_group": RG2}}),
    "release_group_adopted": dict(payload={"path": "{SRC}", "mb_albumid": REL}),
    "release_group_unknown": dict(payload={"path": "{SRC}", "mb_albumid": REL},
                                  overrides={"_fetch_mb_release_tracklist": {"ok": True, "release_group": ""}}),
    "release_group_given_mb_silent": dict(overrides={"_fetch_mb_release_tracklist": {"ok": True, "release_group": ""}}),
    # ── library source and handoff ──
    "library_no_candidates": dict(source="music"),
    "library_preflight_ok": dict(source="music", overrides={"_library_album_ids_for_folder": [5],
                                                            "_folder_release_preflight": FULL}),
    "library_preflight_raises_refused": dict(source="music", overrides={
        "_library_album_ids_for_folder": [5], "_folder_release_preflight": RuntimeError("mb")}),
    "library_preflight_failed_scan_failed": dict(source="music", overrides=_HANDOFF_PRE),
    "handoff_unknown_files_review": dict(source="music", overrides={**_HANDOFF_PRE, "_source_audio_missing_track_scan": {
        "ok": True, "useful_files": [], "unknown_files": ["{SRC}/x.flac"]}}),
    "handoff_unknown_files_not_queued": dict(source="music", overrides={
        **_HANDOFF_PRE, "_queue_folder_for_manual_review": False, "_source_audio_missing_track_scan": {
            "ok": True, "useful_files": [], "unknown_files": ["{SRC}/x.flac"]}}),
    "handoff_review_preflight_raises": dict(source="music", overrides={
        "_library_album_ids_for_folder": [5], "_folder_release_preflight": Seq([
            {"ok": False, "matches": 0, "expected": 3}, RuntimeError("mb")]),
        "_source_audio_missing_track_scan": {"ok": True, "useful_files": [], "unknown_files": ["{SRC}/x.flac"],
                                             "wanted_tracks": WANTED}}),
    "handoff_library_stage_refused": dict(source="music", overrides={
        **_HANDOFF_PRE, "_source_audio_missing_track_scan": dict(_SCAN_USEFUL, unknown_files=[]),
        "_stage_selected_audio_files": "{SRC}"}),
    "handoff_library_useful": dict(source="music", overrides={
        **_HANDOFF_PRE, "_source_audio_missing_track_scan": dict(_SCAN_USEFUL, unknown_files=[])}),
    "handoff_child_fails": dict(source="music", overrides={
        **_HANDOFF_PRE, "_source_audio_missing_track_scan": dict(_SCAN_USEFUL, unknown_files=[]),
        "_wait_for_child_job": RuntimeError("child failed")}),
    "handoff_download_useful_unknown": dict(payload=_with_id(existing_album_id=5, wanted_tracks=WANTED), overrides={
        "carrier.load_for_folder": _CARRIER, "_source_audio_missing_track_scan": _SCAN_USEFUL}),
    "handoff_given_tracks_when_scan_has_none": dict(payload=_with_id(existing_album_id=5, wanted_tracks=WANTED2), overrides={
        "carrier.load_for_folder": dict(_CARRIER, wanted_tracks=WANTED2),
        "_source_audio_missing_track_scan": dict(_SCAN_USEFUL, wanted_tracks=[])}),
    "handoff_missing_not_in_source": dict(source="music", overrides={
        **_HANDOFF_PRE, "_source_audio_missing_track_scan": {"ok": True, "useful_files": [], "unknown_files": [],
                                                             "audio_count": 3, "missing_count": 2}}),
    "handoff_already_complete": dict(source="music", overrides={
        **_HANDOFF_PRE, "_source_audio_missing_track_scan": {"ok": True, "useful_files": [], "unknown_files": [],
                                                             "audio_count": 3, "missing_count": 0}}),
    "handoff_selected_already_present": dict(overrides={
        **_SELECT, "carrier.load_for_folder": _CARRIER, "_source_audio_missing_track_scan": _SCAN_DUP}),
    "handoff_library_no_audio_review": dict(source="music", overrides={
        **_HANDOFF_PRE, "_source_audio_missing_track_scan": {"ok": True, "useful_files": [], "unknown_files": []}}),
    "handoff_library_no_audio_not_queued": dict(source="music", overrides={
        **_HANDOFF_PRE, "_queue_folder_for_manual_review": False,
        "_source_audio_missing_track_scan": {"ok": True, "useful_files": [], "unknown_files": []}}),
    "handoff_download_no_audio_declines": dict(overrides={
        "carrier.load_for_folder": _CARRIER,
        "_source_audio_missing_track_scan": {"ok": True, "useful_files": [], "unknown_files": []}}),
    "handoff_ai_suggestion_kept": dict(source="music", payload=_with_id(ai_suggestion=_AI), overrides={
        **_HANDOFF_PRE, "_source_audio_missing_track_scan": {"ok": True, "useful_files": [], "unknown_files": ["{SRC}/x"]}}),
    "handoff_invalid_uuid_release": dict(source="music", payload={"path": "{SRC}", "mb_albumid": f"https://musicbrainz.org/release/{REL}"},
                                         overrides={**_HANDOFF_PRE, "_source_audio_missing_track_scan": {
                                             "ok": True, "useful_files": [], "unknown_files": []}}),
    # ── import ──
    "fresh_copy": dict(files=["01.flac"]),
    "fresh_move": dict(payload=_with_id(move=True), files=["01.flac"]),
    "torrent_move_downgraded": dict(payload=_with_id(move=True), overrides={"_preserve_torrent_source_path": True}),
    "existing_keep_duplicates": dict(payload=_with_id(existing_album_id=5)),
    "library_in_place": dict(source="music", files=["01.flac"]),
    "audio_rejected": dict(overrides={"_validate_import_source_audio": RuntimeError("policy")}),
    "plan_refused": dict(overrides={"cw.plan_confirmed_import": {"ok": False, "error": "manifest"}}),
    "plan_refused_silent": dict(overrides={"cw.plan_confirmed_import": {"ok": False}}),
    "apply_raises_with_diagnostics": dict(overrides={"cw.apply_confirmed_import": _DIAG}),
    "apply_raises_unavailable": dict(overrides={"cw.apply_confirmed_import": BeetsUnavailableError("down")}),
    "apply_kept_rows": dict(overrides={"cw.apply_confirmed_import": {"ok": False, "error": "wrong", "album_ids": [9, 10]}}),
    "apply_kept_rows_silent": dict(overrides={"cw.apply_confirmed_import": {"ok": False, "album_ids": [9]}}),
    "apply_not_imported": dict(overrides={"cw.apply_confirmed_import": {"ok": False, "code": "not_imported", "error": "skip"}}),
    "apply_other_failure": dict(overrides={"cw.apply_confirmed_import": {"ok": False, "error": "boom"}}),
    "apply_no_rg_echo": dict(overrides={"cw.apply_confirmed_import": {"ok": True, "album_id": 9}}),
    "acceptance_failpoint": dict(payload=_with_id(_acceptance_failpoint="after_apply")),
    # ── partial subset ──
    "subset_all_removed": dict(overrides=_SELECT),
    "subset_staging_cleanup_fails": dict(overrides={**_SELECT, "cw.delete_file": RuntimeError("busy")}),
    "subset_remaining_review": dict(overrides={**_SELECT, "_remaining_audio_files": [P("{SRC}/x.flac")]}),
    "subset_remaining_auto": dict(payload=_with_id(**_AUTO),
                                  overrides={**_SELECT, "_remaining_audio_files": [P("{SRC}/x.flac")]}),
    "subset_library_source_kept": dict(source="music", overrides={"_import_review_selected_source_files": [
        P("{SRC}/a.flac")]}),
    "subset_same_folder_no_stage": dict(overrides={**_SELECT, "_stage_selected_audio_files": "{SRC}"}),
    "subset_filter_rejects": dict(overrides={**_SELECT, "_filter_import_review_selected_audio_files": RuntimeError("all rejected")}),
    # ── source cleanup ──
    "cleanup_removes_source": dict(files=["cover.jpg"]),
    "cleanup_unknown_files": dict(files=["a.txt", "b.nfo", "c.x", "d.y", "e.z", "f.q", "cover.jpg"]),
    "cleanup_plan_fails": dict(overrides={"cw.plan_folder_cleanup": {"ok": False, "error": "nope"}}),
    "cleanup_plan_fails_silent": dict(overrides={"cw.plan_folder_cleanup": {"ok": False}}),
    "cleanup_apply_fails": dict(overrides={"cw.apply_folder_cleanup": {"ok": False, "error": "nope"}}),
    "cleanup_apply_fails_silent": dict(overrides={"cw.apply_folder_cleanup": {"ok": False}}),
    "cleanup_artwork_raises": dict(overrides={"_move_artwork_to_target": RuntimeError("art")}),
    "cleanup_source_gone": dict(no_dir=True),
    # ── after import ──
    "artist_dedup_album_gone": dict(overrides={"lib.get_album": None}),
    "artist_dedup_raises": dict(overrides={"lib.get_album": RuntimeError("db")}),
    "plex_refresh_requested": dict(payload=_with_id(trigger_plex=True, trigger_plex_context="Batch")),
    "ai_match_recorded": dict(payload=_with_id(ai_suggestion=_AI)),
    "ai_match_record_fails": dict(payload=_with_id(ai_suggestion=_AI), overrides={"_record_ai_match": OSError("ro")}),
    "ai_other_identity_not_recorded": dict(payload=_with_id(ai_suggestion=dict(_AI, mb_albumid=REL2, mb_releasegroupid=RG2))),
    "ai_low_confidence_label": dict(payload=_with_id(ai_suggestion=dict(_AI, confidence="LOW"))),
    "ai_bad_confidence_label": dict(payload=_with_id(ai_suggestion=dict(_AI, confidence="certain"))),
    # ── slot and auto-import bookkeeping ──
    "cancelled_in_slot": dict(cancel=True),
    "cancelled_auto": dict(payload=_with_id(**_AUTO), cancel=True),
    "auto_completed": dict(payload=_with_id(**_AUTO)),
    "auto_failed": dict(payload=_with_id(**_AUTO), overrides={"_fetch_mb_release_tracklist": {"ok": False}}),
    "auto_format_policy": dict(payload=_with_id(**_AUTO), overrides={
        "_validate_import_source_audio": RuntimeError("format policy"), "_is_music_format_policy_handled_error": True}),
}


def _run(kind, spec):
    with tempfile.TemporaryDirectory() as tmp:
        default = _reimport() if kind == "reimport" else _with_id()
        world = World(tmp, source=spec.get("source", "downloads"), files=spec.get("files", ()),
                      overrides=spec.get("overrides"), cancel=spec.get("cancel", False),
                      cancel_on_log=spec.get("cancel_on_log", ""), no_dir=spec.get("no_dir", False))
        return world.run(kind, spec.get("payload", default))


def _all_records():
    records = {f"reimport/{n}": _run("reimport", s) for n, s in REIMPORT_SCENARIOS.items()}
    records.update({f"with_id/{n}": _run("with_id", s) for n, s in WITH_ID_SCENARIOS.items()})
    return records


class ImportJobGoldenTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        if os.environ.get("UPDATE_IMPORT_GOLDEN"):
            GOLDEN.parent.mkdir(exist_ok=True)
            GOLDEN.write_text(json.dumps(_all_records(), indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                              encoding="utf-8", newline="\n")
        cls.golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    def _check(self, kind, scenarios):
        for name, spec in scenarios.items():
            with self.subTest(scenario=name):
                actual = json.loads(json.dumps(_run(kind, spec)))
                self.assertEqual(actual, self.golden[f"{kind}/{name}"])

    def test_reimport_disk(self):
        self._check("reimport", REIMPORT_SCENARIOS)

    def test_folder_import_with_id(self):
        self._check("with_id", WITH_ID_SCENARIOS)

    def test_every_golden_entry_has_a_scenario(self):
        names = {f"reimport/{n}" for n in REIMPORT_SCENARIOS} | {f"with_id/{n}" for n in WITH_ID_SCENARIOS}
        self.assertEqual(set(self.golden), names)

    def test_both_jobs_hold_the_process_lock_around_the_durable_slot(self):
        order = [["held-enter", "import-job-lock"], ["held-enter", "import-slot"],
                 ["held-exit", "import-slot"], ["held-exit", "import-job-lock"]]
        for key in ("reimport/lock_default_path", "with_id/fresh_copy"):
            with self.subTest(key=key):
                held = [e for e in self.golden[key]["trace"] if e[0] in ("held-enter", "held-exit")]
                self.assertEqual(held, order)
        self.assertIsInstance(isvc._IMPORT_JOB_LOCK, type(threading.Lock()))


class SharedJobStoreTests(unittest.TestCase):
    def test_a_job_started_from_another_thread_does_not_replace_the_import_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = World(tmp)
            tick = threading.Thread(target=world.start_python, args=(print,), kwargs={"label": "tick"})
            tick.start()
            tick.join()
            self.assertEqual(world.job, {})


class StepsRunAloneTests(unittest.TestCase):
    """The steps work without a job around them."""

    def test_parse_ids_tells_a_release_group_from_a_release(self):
        self.assertEqual(isvc._folder_import_parse_ids(REL, RG), (REL, REL, RG, False))
        self.assertEqual(isvc._folder_import_parse_ids("", RG), (RG, RG, RG, True))

    def test_carrier_identity_ignores_the_client_album_and_tracks(self):
        carrier = {"mb_releasegroupid": RG, "release_id": REL, "existing_album_id": 5, "expected_tracks": WANTED}
        got = isvc._folder_import_carrier_identity(carrier, RG2, [])
        self.assertEqual(got[:5], (RG, REL, REL, RG, False))
        self.assertEqual(got[5], 5)
        self.assertEqual([t["title"] for t in got[6]], ["Two"])

    def test_handoff_without_a_target_album_declines(self):
        ctx = isvc._FolderImportContext(
            log=[], cancel_event=None, folder_path="/x", ai_suggestion={}, raw_mb_input=REL,
            input_looks_like_release_group=False, queue_review_on_uncertain=True, light_confirm=False,
            existing_album_id=0, wanted_tracks=[], source_is_library=False, selected_subset_import=False)
        self.assertFalse(isvc._folder_import_handoff(ctx, REL, RG, []))


if __name__ == "__main__":
    unittest.main()
