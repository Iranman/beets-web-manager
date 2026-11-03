"""Characterization of the duplicate scan and AI review jobs (modernization M4, #505).

Each scenario drives the real ``start_dedup_scan`` / ``dedup_ai_review`` route and
its job body over real temp files with a fake Beets library, fake AcoustID and a
fake AI provider. The whole ordered trace -- every log line, progress update,
cancel check, fingerprint call and AI request, then the result, the error chain
and the final job state -- is compared with ``tests/fixtures/dedup_jobs_golden.json``,
which was generated from the code BEFORE the functions were split into steps.

Each trace also runs the job's output through the REAL safety decisions downstream:
the unattended deletion plan (fingerprint or byte proof, same release slot), the
reviewed-cleanup pair selection (an unproven AI suggestion is never a pair), and,
in the ``maintenance_*`` scenarios, the backend/dedup_authorization.py switch.

Regenerate only for a deliberate behavior change:
    UPDATE_DEDUP_JOBS_GOLDEN=1 python -m unittest tests.test_dedup_characterization
"""
import copy
import json
import os
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
import routes_cleanup
import backend.dedup_authorization as dedup_authorization
import backend.dedup_service as dedup_service
import backend.maintenance_service as maintenance_service
import backend.plex_service as plex_service

GOLDEN = Path(__file__).parent / "fixtures" / "dedup_jobs_golden.json"

REC = "5d39b3b9-290a-493e-93fc-30b5ec6c614a"
REC2 = "11111111-2222-4333-8444-555555555555"
REL_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
REL_B = "bbbbbbbb-1111-4111-8111-bbbbbbbbbbbb"
SLOT = {"album_id": 1, "mb_albumid": REL_A, "mb_releasegroupid": REC2, "disc": 1, "track": 1}


def item(item_id, rel, **fields):
    row = {"id": item_id, "rel": rel, "title": f"Library Song {item_id}", "artist": "Library Artist", "album": "Library Album"}
    row.update(fields)
    return row


def src(rel, **fields):
    return dict(rel=rel, **fields)


def many(count, **fields):
    return [src(f"Bulk/{n:03d}.flac", size=9000 + n, tags={"title": ""}, **fields) for n in range(count)]


def ai_files(count):
    return [src(f"Bulk/{n:03d} Song.flac", tags={"artist": "Artist", "title": f"Song {n}"}) for n in range(count)]


LIB1 = item(1, "Art/Alb/01 Song One.flac", title="Song One", artist="Artist", album="Alb", size=1234)
SRC1 = src("New/one.flac", tags={"artist": "Artist", "title": "Song One"})
TRACK_ID = {"mb_trackid": REC, "artist": "Artist", "title": "Song One"}


def tracked(**scan):
    return dict({"payload": {"path": "<TMP>/music", "tracked_only": True}}, **scan)


def pair(first=None, second=None, **common):
    """Two tracked copies; identical size and bytes unless a side overrides them."""
    base = dict(SLOT, size=2222, fill="x")
    base.update(common)
    return [item(1, "Art/Alb/01 Song.flac", **dict(base, **(first or {}))),
            item(2, "Art/Alb/01 Song.1.flac", **dict(base, **(second or {})))]


def fuzzy_case(score, item_id=1, **payload):
    return dict(items=[LIB1], sources=[SRC1], fuzzy={"Song One": dict({"id": item_id, "score": score}, **payload)})


def fp_case(match_tags, fp, **extra):
    return dict({"items": [LIB1], "sources": [src("New/one.flac", tags=match_tags)], "fp_match": {"one.flac": fp}}, **extra)


def m(file_idx=1, lib_id=1, confidence="high", reason="same song"):
    return {"file_idx": file_idx, "lib_id": lib_id, "confidence": confidence, "reason": reason}


def ai(*matches):
    return {"matches": list(matches)}


def ai_case(*replies, **extra):
    return dict({"kind": "ai", "items": [LIB1], "sources": [SRC1], "ai": list(replies)}, **extra)


SCENARIOS = {
    # ── refused before any job exists ──
    "scan_refuse_outside_roots": {"payload": {"path": "/definitely/outside"}},
    "scan_refuse_relative": {"payload": {"path": "relative/path"}},
    "scan_refuse_blank": {"payload": {"path": "   "}},
    "scan_refuse_missing_folder": {"payload": {"path": "<TMP>/downloads/nope"}},
    "scan_default_path_is_downloads": {"payload": {}},
    "scan_blank_path_is_downloads": {"payload": {"path": ""}},
    "scan_tracked_only_needs_true": {"payload": {"path": "<TMP>/downloads", "tracked_only": "true"},
                                     "items": [LIB1], "sources": [SRC1]},
    # ── listing ──
    "scan_empty": {},
    "scan_empty_no_update_state": {"no_update": True},
    "scan_only_non_audio": {"sources": [src("a/readme.txt"), src("a/cover.jpg")]},
    "scan_list_error": {"list_error": True, "sources": [SRC1]},
    "scan_list_error_no_update_state": {"list_error": True, "no_update": True},
    "scan_lib_read_error": {"lib_error": True, "sources": [SRC1]},
    "scan_tracked_lists_only_tracked_under_root": tracked(
        items=[LIB1, item(2, "Art/Alb/notes.txt"), item(3, "Art/Alb/gone.flac", no_file=True)],
        sources=[src("New/untracked.flac")]),
    "scan_tracked_scope_subfolder": {"payload": {"path": "<TMP>/music/Art/Sub", "tracked_only": True},
                                     "items": [LIB1, item(2, "Art/Sub/02.flac")]},
    # ── progress cadence and batch boundaries ──
    "scan_one_file": {"sources": many(1)},
    "scan_two_files": {"sources": many(2)},
    "scan_eleven_files": {"sources": many(11)},
    "scan_twelve_files": {"sources": many(12)},
    "scan_64_files_exact_batch": {"sources": many(64)},
    "scan_65_files_second_batch": {"sources": many(65)},
    "scan_match_in_second_batch": {"items": [item(1, "Art/Alb/one.flac", size=9064)], "sources": many(65)},
    # ── library index ──
    "scan_index_bad_item_id": {"items": [item("x", "Art/Alb/x.flac", size=4444)], "sources": [src("New/x.flac", size=4444)]},
    "scan_index_none_item_id": {"items": [item(None, "Art/Alb/x.flac", size=4444)], "sources": [src("New/x.flac", size=4444)]},
    "scan_index_missing_library_file": {"items": [item(1, "Art/Alb/gone.flac", no_file=True)], "sources": [SRC1]},
    "scan_index_relative_item_path": {"items": [item(1, "Art/Alb/rel.flac", size=4444, relative=True)],
                                      "sources": [src("New/x.flac", size=4444)]},
    "scan_index_variant_error_is_ignored": {"items": [LIB1], "sources": [SRC1], "variants_error": [1]},
    "scan_index_candidate_rows": {"items": [LIB1, item(2, "Art/Alb/02 feat.flac", title="Song Two (feat. Guest)", artist="Artist & Guest")],
                                  "sources": [SRC1], "dump_candidates": True},
    "scan_index_repeated_variants_make_one_row": {
        "items": [LIB1], "sources": [SRC1], "dump_candidates": True,
        "variant_rows": {"1": [["Artist", "Song One"], ["ARTIST", "song one"], ["Artist", ""], ["Other", "Song One"]]}},
    "scan_real_fuzzy_lookup": {"items": [LIB1, item(2, "Art/Alb/02.flac", title="Other Tune", artist="Someone")],
                               "sources": [SRC1], "real_fuzzy": True},
    "scan_real_fuzzy_no_match": {"items": [LIB1], "sources": [src("New/x.flac", tags={"artist": "Nobody", "title": "Nothing Alike"})],
                                 "real_fuzzy": True},
    # ── reading a source file ──
    "scan_source_vanished_keeps_going": {"items": [item(1, "Art/Alb/01.flac", size=4444)],
                                         "sources": [src("New/a.flac", size=1), src("New/b.flac", size=4444)], "vanish": ["a.flac"]},
    "scan_source_stat_error_keeps_going": {"items": [item(1, "Art/Alb/01.flac", size=4444)],
                                           "sources": [src("New/a.flac", size=1), src("New/b.flac", size=4444)], "stat_error": ["a.flac"]},
    "scan_tag_read_error_uses_filename": {"items": [item(1, "Art/Alb/01.flac", size=4444)],
                                          "sources": [src("New/Who - What.flac", size=4444, tags="boom")]},
    "scan_blank_tags_use_filename": {
        "items": [item(1, "Art/Alb/01.flac", size=4444)],
        "sources": [src("One/Who - Album - 03 - The Song.flac", size=4444, tags={}),
                    src("Two/Who - 03 - The Song.flac", size=4444, tags={}),
                    src("Three/Who - Some - The Song.flac", size=4444, tags={}),
                    src("Four/Who – The Song.flac", size=4444, tags={}),
                    src("Five/Who — The Song.flac", size=4444, tags={}),
                    src("Six/07 - The Song.flac", size=4444, tags={}),
                    src("Seven/08. Who - The Song (Official Video).flac", size=4444, tags={}),
                    src("Eight/Who - The Song [2019] (feat. X) (Live at Y).flac", size=4444, tags={}),
                    src("Nine/Plain.flac", size=4444, tags={})]},
    "scan_tag_artist_kept_when_title_missing": {"items": [item(1, "Art/Alb/01.flac", size=4444)],
                                                "sources": [src("New/Who - What.flac", size=4444, tags={"artist": "Tagged"})]},
    "scan_tags_are_stripped": {"items": [item(1, "Art/Alb/01.flac", size=4444)],
                               "sources": [src("New/x.flac", size=4444, tags={"artist": "  A ", "title": " T ", "mb_trackid": " zz "})]},
    "scan_known_item_reads_no_tags": tracked(items=pair()),
    # ── match strategies, in order ──
    "match_mb_track_id": fp_case(TRACK_ID, [None, [], []], items=[item(1, "Art/Alb/01.flac", mb_trackid=REC)]),
    "match_mb_beats_size": fp_case(TRACK_ID, [None, [], []], items=[
        item(1, "Art/Alb/01.flac", size=4444), item(2, "Art/Alb/02.flac", mb_trackid=REC)],
        sources=[src("New/one.flac", size=4444, tags=TRACK_ID)]),
    "match_mb_second_in_bucket": tracked(items=[item(1, "Art/Alb/a.flac", mb_trackid=REC, size=11), item(2, "Art/Alb/b.flac", mb_trackid=REC, size=22)]),
    "match_mb_only_self_falls_to_size": tracked(items=[item(1, "Art/Alb/a.flac", mb_trackid=REC, size=11), item(2, "Art/Alb/b.flac", size=11)]),
    "match_size": {"items": [item(1, "Art/Alb/01.flac", size=4444)], "sources": [src("New/x.flac", size=4444)]},
    "match_size_zero_is_no_match": {"items": [item(1, "Art/Alb/01.flac", size=0)], "sources": [src("New/x.flac", size=0)]},
    "match_size_only_self_is_no_match": tracked(items=[item(1, "Art/Alb/01.flac", size=4444)]),
    "match_size_beats_fuzzy": dict(fuzzy_case(0.99), items=[LIB1], sources=[src("New/one.flac", size=1234, tags={"title": "Song One"})]),
    "match_fuzzy_at_threshold": fuzzy_case(0.88),
    "match_fuzzy_below_threshold": fuzzy_case(0.8799),
    "match_fuzzy_score_over_one_is_clamped": fuzzy_case(1.2),
    "match_fuzzy_score_missing": fuzzy_case(None),
    "match_fuzzy_score_nan_is_no_match": fuzzy_case(float("nan")),
    # On a case-sensitive disk, a path differing only by case still counts as the same file.
    "match_path_key_ignores_case": {"payload": {"path": "<TMP>/music"}, "items": [item(1, "Art/Alb/Song.flac", size=4444)],
                                    "sources": [src("../music/Art/Alb/SONG.flac", size=4444, tags={"title": "Tagged"})]},
    "match_fuzzy_no_title_is_skipped": {"items": [LIB1], "sources": [src("New/.flac", tags={})], "fuzzy": {"": {"id": 1, "score": 1.0}}},
    "match_fuzzy_unknown_id_builds_a_stand_in": fuzzy_case(0.9, item_id=999, path="<TMP>/music/Art/Alb/01 Song One.flac",
                                                           title="Stand In", artist="Who", album="What"),
    "match_fuzzy_bad_id_builds_a_stand_in": fuzzy_case(0.9, item_id="x", path="<TMP>/music/Art/Alb/01 Song One.flac",
                                                       title="Stand In", artist="Who", album="What"),
    "match_fuzzy_stand_in_without_title_fails": fuzzy_case(0.9, item_id=999, path="<TMP>/music/Art/Alb/01 Song One.flac"),
    "match_album_title": {"items": [item(1, "Art/Alb/01.flac", title="Song One", album="Willow")],
                          "sources": [src("Willow (2019)/one.flac", tags={"title": "Song One"})]},
    "match_album_title_year_prefix": {"items": [item(1, "Art/Alb/01.flac", title="Song One", album="Willow")],
                                      "sources": [src("1999 - Willow/one.flac", tags={"title": "Song One"})]},
    "match_album_title_error_fails_the_job": {"items": [LIB1], "sources": [SRC1], "album_error": True},
    "match_fingerprint_fallback": {"items": [item(1, "Art/Alb/01.flac", mb_trackid=REC)],
                                   "sources": [src("New/x.flac", tags={"title": "Nothing Alike"})], "fp_ids": {"x.flac": [REC]}},
    "match_fingerprint_second_id": {"items": [item(1, "Art/Alb/01.flac", mb_trackid=REC)],
                                    "sources": [src("New/x.flac", tags={"title": "Nothing Alike"})], "fp_ids": {"x.flac": [REC2, REC]}},
    "match_fingerprint_self_then_next_id": tracked(
        items=[item(1, "Art/Alb/a.flac", mb_trackid=REC, title="Tune A", album="Album A"),
               item(2, "Art/Alb/b.flac", mb_trackid=REC2, title="Tune B", album="Album B")],
        fp_ids={"a.flac": [REC2]}),
    "match_fingerprint_own_recording_first": tracked(
        items=[item(1, "Art/Alb/a.flac", mb_trackid=REC, title="Tune A", album="Album A"),
               item(2, "Art/Alb/b.flac", mb_trackid=REC2, title="Tune B", album="Album B")],
        fp_ids={"a.flac": [REC, REC2]}),
    "match_fingerprint_self_first_in_bucket": tracked(
        items=[item(1, "Art/Alb/a.flac", mb_trackid=REC, title="Tune A", album="Album A"),
               item(2, "Art/Alb/b.flac", title="Tune B", album="Album B")],
        fp_ids={"b.flac": [REC]}),
    "match_fingerprint_lookup_error_fails_the_job": {"items": [LIB1], "sources": [src("New/x.flac", tags={"title": "Nothing"})],
                                                     "fp_ids": {"x.flac": "boom"}},
    "match_nothing": {"items": [LIB1], "sources": [src("New/x.flac", tags={"title": "Nothing Alike"})]},
    # ── candidate sanity checks ──
    "check_same_file_skipped": tracked(items=[item(1, "Art/Willow/01.flac", title="Song One", album="Willow")]),
    "check_library_file_gone_skipped": {"items": [item(1, "Art/Alb/gone.flac", no_file=True, mb_trackid=REC)],
                                        "sources": [src("New/one.flac", tags=TRACK_ID)]},
    "check_relative_library_path": {"items": [item(1, "Art/Alb/rel.flac", relative=True, mb_trackid=REC)],
                                    "sources": [src("New/one.flac", tags=TRACK_ID)]},
    "check_continues_after_rejection": {"items": [item(1, "Art/Alb/01.flac", mb_trackid=REC), item(2, "Art/Alb/02.flac", size=4444)],
                                        "sources": [src("New/one.flac", tags=TRACK_ID), src("New/two.flac", size=4444)],
                                        "fp_match": {"one.flac": [None, [REC], [REC2]]}},
    # ── fingerprint verification per match type ──
    "verify_mb_shared": fp_case(TRACK_ID, [REC, [REC], [REC]], items=[item(1, "Art/Alb/01.flac", mb_trackid=REC)]),
    "verify_mb_disagrees_rejected": fp_case(TRACK_ID, [None, [REC], [REC2]], items=[item(1, "Art/Alb/01.flac", mb_trackid=REC)]),
    "verify_mb_only_source_heard": fp_case(TRACK_ID, [None, [REC], []], items=[item(1, "Art/Alb/01.flac", mb_trackid=REC)]),
    "verify_mb_only_library_heard": fp_case(TRACK_ID, [None, [], [REC]], items=[item(1, "Art/Alb/01.flac", mb_trackid=REC)]),
    "verify_mb_error_fails_the_job": fp_case(TRACK_ID, "boom", items=[item(1, "Art/Alb/01.flac", mb_trackid=REC)]),
    "verify_fuzzy_shared": dict(fuzzy_case(0.9), fp_match={"one.flac": [REC, [REC], [REC]]}),
    "verify_fuzzy_disagrees_rejected": dict(fuzzy_case(0.9), fp_match={"one.flac": [None, [REC], [REC2]]}),
    "verify_fuzzy_unavailable_is_medium": dict(fuzzy_case(0.9)),
    "verify_album_title_shared": {"items": [item(1, "Art/Alb/01.flac", title="Song One", album="Willow")],
                                  "sources": [src("Willow/one.flac", tags={"title": "Song One"})],
                                  "fp_match": {"one.flac": [REC, [REC], [REC]]}},
    "verify_album_title_disagrees_rejected": {"items": [item(1, "Art/Alb/01.flac", title="Song One", album="Willow")],
                                              "sources": [src("Willow/one.flac", tags={"title": "Song One"})],
                                              "fp_match": {"one.flac": [None, [REC], [REC2]]}},
    "verify_size_makes_no_fingerprint_call": {"items": [item(1, "Art/Alb/01.flac", size=4444)], "sources": [src("New/x.flac", size=4444)],
                                              "fp_match": {"x.flac": "boom"}},
    # ── release relation, evidence and the unattended deletion plan ──
    "gate_byte_identical_same_slot_is_deleted": tracked(items=pair()),
    "gate_same_size_different_bytes_not_deleted": tracked(items=pair(second={"fill": "y"})),
    "gate_embedded_id_alone_not_deleted": tracked(items=pair(first={"size": 11}, second={"size": 22}, mb_trackid=REC)),
    "gate_fingerprint_shared_same_slot_deleted": tracked(items=pair(first={"size": 11}, second={"size": 22}, mb_trackid=REC),
                                                         fp_match={"01 Song.flac": [REC, [REC], [REC]],
                                                                   "01 Song.1.flac": [REC, [REC], [REC]]}),
    "gate_fingerprint_disagrees_not_deleted": tracked(items=pair(first={"size": 11}, second={"size": 22}, mb_trackid=REC),
                                                      fp_match={"01 Song.flac": [None, [REC], [REC2]],
                                                                "01 Song.1.flac": [None, [REC2], [REC]]}),
    "gate_different_release_is_review": tracked(items=pair(second={"album_id": 2, "mb_albumid": REL_B})),
    "gate_different_position_is_review": tracked(items=pair(second={"track": 2})),
    "gate_same_release_split_rows": tracked(items=pair(second={"album_id": 2})),
    "gate_unknown_relation": tracked(items=pair(first={"album_id": 0, "mb_albumid": ""}, second={"album_id": 0, "mb_albumid": ""})),
    "gate_different_release_fingerprint_stays_review": tracked(
        items=pair(first={"size": 11}, second={"size": 22, "album_id": 2, "mb_albumid": REL_B}, mb_trackid=REC),
        fp_match={"01 Song.flac": [REC, [REC], [REC]], "01 Song.1.flac": [REC, [REC], [REC]]}),
    "gate_untracked_source_not_deleted": {"items": [item(1, "Art/Alb/01.flac", size=4444, fill="x")],
                                          "sources": [src("New/x.flac", size=4444, fill="x")]},
    # ── folder grouping ──
    "folders_all_and_part_dupes": {
        "items": [item(1, "Art/Alb/01.flac", size=4444)],
        "sources": [src("All/a.flac", size=4444), src("All/b.flac", size=4444),
                    src("Part/a.flac", size=4444), src("Part/b.flac", size=1), src("None/a.flac", size=2)]},
    "plural_one_dup_one_folder": {"items": [item(1, "Art/Alb/01.flac", size=4444)], "sources": [src("A/a.flac", size=4444)]},
    # ── job shapes ──
    "scan_no_update_state_with_match": {"items": [item(1, "Art/Alb/01.flac", size=4444)], "sources": [src("New/x.flac", size=4444)], "no_update": True},
    "scan_no_cancel_event": {"items": [item(1, "Art/Alb/01.flac", size=4444)], "sources": [src("New/x.flac", size=4444)], "no_cancel": True},
    "scan_rerun_same_job": {"items": [item(1, "Art/Alb/01.flac", size=4444)], "sources": [src("New/x.flac", size=4444)], "runs": 2},
    "scan_rerun_after_library_change": {"items": [item(1, "Art/Alb/01.flac", size=4444)], "sources": [src("New/x.flac", size=4444)],
                                        "runs": 2, "drop_items_between_runs": True},
    "scan_two_jobs_do_not_share_state": {"items": [item(1, "Art/Alb/01.flac", size=4444)], "sources": [src("New/x.flac", size=4444)],
                                         "starts": [{"path": "<TMP>/downloads"}, {"path": "<TMP>/music", "tracked_only": True}]},
    # ── AI review: refused before a job exists ──
    "ai_refuse_no_scan": {"kind": "ai", "no_source": True, "body": {}},
    "ai_refuse_unknown_job": {"kind": "ai", "no_source": True, "body": {"scan_jid": "lost"}},
    "ai_refuse_ai_job_as_source": {"kind": "ai", "source": {"kind": "ai_review"}, "body": {"scan_jid": "scan-1"}},
    "ai_refuse_no_key": {"kind": "ai", "api_key": ""},
    "ai_refuse_scan_running": {"kind": "ai", "source": {"status": "running"}},
    "ai_key_checked_before_scan_status": {"kind": "ai", "api_key": "", "source": {"status": "running"}},
    "ai_null_scan_jid_raises": {"kind": "ai", "body": {"scan_jid": None}},
    "ai_null_scan_path_raises_nothing": {"kind": "ai", "body": {"scan_jid": "scan-1", "scan_path": None}},
    "ai_finds_scan_by_path_hint": {"kind": "ai", "body": {"scan_path": "<TMP>/downloads"}},
    "ai_finds_scan_by_path_alias": {"kind": "ai", "body": {"path": "<TMP>/downloads"}},
    "ai_path_hint_no_scan": {"kind": "ai", "body": {"path": "<TMP>/music"}},
    "ai_source_without_path_uses_downloads": {"kind": "ai", "source": {"scan_path": ""}},
    # ── AI review: listing and early exits ──
    "ai_list_error": {"kind": "ai", "list_error": True, "sources": [SRC1]},
    "ai_list_error_no_update_state": {"kind": "ai", "list_error": True, "no_update": True},
    "ai_nothing_to_review": {"kind": "ai"},
    "ai_nothing_to_review_no_update_state": {"kind": "ai", "no_update": True},
    "ai_all_files_already_matched": {"kind": "ai", "sources": [SRC1], "matched": ["New/one.flac"]},
    "ai_matched_files_are_excluded": ai_case(ai(), sources=[SRC1, src("New/two.flac", tags={"title": "Two"})], matched=["New/one.flac"]),
    "ai_matched_set_is_taken_at_start": ai_case(ai(), matched=["New/one.flac"], source_cleared_after_start=True),
    "ai_lib_error_fails_the_job": ai_case(lib_error=True),
    "ai_tag_read_error_fails_the_job": ai_case(sources=[src("New/one.flac", tags="boom")]),
    # ── AI review: batches ──
    "ai_batch_exact_20": ai_case(ai(), sources=ai_files(20)),
    "ai_batch_21": ai_case(ai(), ai(), sources=ai_files(21)),
    "ai_batch_41": ai_case(ai(), ai(), ai(), sources=ai_files(41)),
    "ai_second_batch_index_is_per_batch": ai_case(ai(), ai(m(1)), sources=ai_files(20) + [src("Bulk/z Song One.flac", tags={"artist": "Artist", "title": "Song One"})]),
    "ai_no_update_state_with_match": ai_case(ai(m()), no_update=True),
    "ai_no_cancel_event": ai_case(ai(m()), no_cancel=True),
    "ai_rerun_same_job": ai_case(ai(m()), ai(m()), runs=2),
    "ai_rerun_after_failure": ai_case("raise:down", ai(m()), runs=2),
    # ── AI review: candidate selection and prompt ──
    "ai_prompt_top3_by_title": ai_case(ai(), items=[
        item(n, f"Art/Alb/{n}.flac", title=title, artist="Artist") for n, title in
        enumerate(["Song One", "Song On", "Song", "Sang Won", "Zzz"], start=1)]),
    "ai_prompt_zero_score_dropped": ai_case(ai(), items=[item(1, "Art/Alb/1.flac", title="zzz", artist="Artist")],
                                            sources=[src("New/one.flac", tags={"artist": "Artist", "title": "qqq"})]),
    "ai_prompt_filters_by_artist_words": ai_case(ai(), items=[LIB1, item(2, "Art/Alb/2.flac", title="Song One", artist="Stranger")]),
    "ai_prompt_album_stands_in_for_artist": ai_case(ai(), items=[item(1, "A/1.flac", title="Song One", artist="Velvet"), item(2, "A/2.flac", title="Song One", artist="Stranger")],
                                                    sources=[src("New/x.flac", tags={"album": "Velvet", "title": "Song One"})]),
    "ai_prompt_artist_from_filename": ai_case(ai(), sources=[src("New/02 - Artist - Song One.flac", tags={})]),
    "ai_prompt_artist_from_filename_en_dash": ai_case(ai(), sources=[src("New/Artist – Song One.flac", tags={})]),
    "ai_prompt_title_from_filename": ai_case(ai(), sources=[src("New/03_Song One.flac", tags={"artist": "Artist"})]),
    "ai_prompt_no_title_uses_first_three_of_pool": ai_case(ai(), sources=[src("New/01 - .flac", tags={"artist": "Artist"})], items=[
        item(n, f"Art/Alb/{n}.flac", artist="Artist") for n in range(1, 6)]),
    "ai_prompt_no_artist_uses_whole_library": ai_case(ai(), sources=[src("New/Song One.flac", tags={"title": "Song One"})],
                                                      items=[LIB1, item(2, "Art/Alb/2.flac", artist="Stranger", title="Song One")]),
    "ai_prompt_shared_candidate_once": ai_case(ai(), sources=[src("New/a.flac", tags={"artist": "Artist", "title": "Song One"}),
                                                              src("New/b.flac", tags={"artist": "Artist", "title": "Song One"})]),
    # ── AI review: replies that fail, and carrying on after each ──
    "ai_reply_refusal_skips_batch_bookkeeping": ai_case("refusal:" + "n" * 150, ai(m(1)), sources=ai_files(20) + [src("Bulk/z.flac", tags={"artist": "Artist", "title": "Song One"})]),
    "ai_reply_network_error_continues": ai_case("raise:down", ai(m(1)), sources=ai_files(20) + [src("Bulk/z.flac", tags={"artist": "Artist", "title": "Song One"})]),
    "ai_reply_bad_json_continues": ai_case("content:not json", ai(m(1)), sources=ai_files(20) + [src("Bulk/z.flac", tags={"artist": "Artist", "title": "Song One"})]),
    "ai_reply_null_matches_is_a_batch_error": ai_case({"matches": None}, ai(m(1)), sources=ai_files(20) + [src("Bulk/z.flac", tags={"artist": "Artist", "title": "Song One"})]),
    "ai_reply_no_choices": ai_case("empty"),
    "ai_reply_not_an_object": ai_case("content:[1]"),
    "ai_reply_bad_match_loses_rest_of_batch": ai_case(ai(m(), {"file_idx": "x"}, m(2)), sources=[SRC1, src("New/two.flac", tags={"artist": "Artist", "title": "Song One"})]),
    "ai_reply_stat_of_library_error_continues": ai_case(ai(m(1, "x")), ai(m(1)), sources=ai_files(20) + [src("Bulk/z.flac", tags={"artist": "Artist", "title": "Song One"})]),
    # ── AI review: every per-match rejection, with the next match still kept ──
    "ai_match_file_idx_zero": ai_case(ai(m(0), m(1))),
    "ai_match_file_idx_past_end": ai_case(ai(m(2), m(1))),
    "ai_match_bad_confidence": ai_case(ai(m(confidence="low"), m(1))),
    "ai_match_confidence_case": ai_case(ai(m(confidence="HIGH"))),
    "ai_match_missing_confidence": ai_case(ai({"file_idx": 1, "lib_id": 1, "reason": "r"})),
    "ai_match_unknown_library_item": ai_case(ai(m(1, 99), m(1))),
    "ai_match_library_file_gone": ai_case(ai(m(1, 2), m(1)), items=[LIB1, item(2, "Art/Alb/gone.flac", no_file=True, title="Song One")]),
    "ai_match_relative_library_path": ai_case(ai(m()), items=[item(1, "Art/Alb/rel.flac", relative=True, title="Song One", artist="Artist")]),
    "ai_match_same_file": ai_case(ai(m()), sources=[], source={"scan_path": "<TMP>/music"}),
    # title guard at its boundary: "abcd"/"abzz" is exactly 0.50, 0.4776 is just under.
    "ai_title_guard_exactly_half_kept": ai_case(ai(m()), items=[item(1, "A/1.flac", title="abzz", artist="Artist")],
                                                sources=[src("New/one.flac", tags={"artist": "Artist", "title": "abcd"})]),
    "ai_title_guard_just_under_rejected": ai_case(ai(m(), m()), items=[item(1, "A/1.flac", title="a" * 16 + "y" * 18, artist="Artist")],
                                                  sources=[src("New/one.flac", tags={"artist": "Artist", "title": "a" * 16 + "x" * 17})]),
    "ai_title_guard_empty_library_title": ai_case(ai(m()), items=[item(1, "A/1.flac", title="", artist="Artist")]),
    "ai_title_guard_punctuation_only_title": ai_case(ai(m()), items=[item(1, "A/1.flac", title="???", artist="Artist")],
                                                     sources=[src("New/one.flac", tags={"artist": "Artist", "title": "!!!"})]),
    "ai_title_guard_falls_back_to_stem": ai_case(ai(m()), items=[item(1, "A/1.flac", title="Song One", artist="Artist")],
                                                 sources=[src("New/Song One.flac", tags={"artist": "Artist"})]),
    "ai_title_guard_stem_when_derived_title_empty": ai_case(ai(m()), items=[item(1, "A/1.flac", title="01", artist="Artist")],
                                                            sources=[src("New/01 - .flac", tags={"artist": "Artist"})]),
    # ── AI review: the fingerprint decides, the AI does not ──
    "ai_fingerprint_shared_upgrades_to_high": ai_case(ai(m(confidence="medium")), fp_match={"one.flac": [REC, [REC], [REC]]}),
    "ai_fingerprint_disagrees_rejected": ai_case(ai(m(), m()), fp_match={"one.flac": [None, [REC], [REC2]]}),
    "ai_fingerprint_unavailable_stays_unverified": ai_case(ai(m())),
    "ai_fingerprint_only_source_heard": ai_case(ai(m()), fp_match={"one.flac": [None, [REC], []]}),
    "ai_fingerprint_error_ends_the_batch": ai_case(ai(m()), fp_match={"one.flac": "boom"}),
    "ai_high_confidence_is_not_a_cleanup_pair": ai_case(ai(m(confidence="high")), cleanup_after=True),
    "ai_fingerprinted_without_source_id_not_a_pair": ai_case(ai(m()), fp_match={"one.flac": [REC, [REC], [REC]]}, cleanup_after=True),
    "ai_source_gone_after_tags": ai_case(ai(m()), vanish_on_fingerprint=["one.flac"]),
    # ── folder grouping ──
    "ai_folders_totals_count_unmatched_only": ai_case(ai(m(1), m(2)), sources=[
        src("All/a.flac", tags={"artist": "Artist", "title": "Song One"}),
        src("All/b.flac", tags={"artist": "Artist", "title": "Song One"}),
        src("All/c.flac", tags={"artist": "Artist", "title": "Song One"}),
        src("Part/d.flac", tags={"artist": "Artist", "title": "Zz"})], matched=["All/c.flac"]),
    # ── the unattended deletion switch, end to end ──
    "maintenance_switch_missing_is_off": tracked(kind="maintenance", items=pair()),
    "maintenance_switch_malformed_is_off": tracked(kind="maintenance", items=pair(), auth="malformed"),
    "maintenance_switch_false_is_off": tracked(kind="maintenance", items=pair(), auth=False),
    "maintenance_switch_on_cleans_proven_pair": tracked(kind="maintenance", items=pair(), auth=True),
    "maintenance_switch_on_nothing_proven": tracked(kind="maintenance", items=pair(second={"fill": "y"}), auth=True),
    "maintenance_switch_on_review_only": tracked(kind="maintenance", items=pair(second={"track": 2}), auth=True),
    "maintenance_switch_on_fingerprint_pair": tracked(kind="maintenance", auth=True,
                                                      items=pair(first={"size": 11}, second={"size": 22}, mb_trackid=REC),
                                                      fp_match={"01 Song.flac": [REC, [REC], [REC]], "01 Song.1.flac": [REC, [REC], [REC]]}),
    "maintenance_skipped_while_scan_runs": tracked(kind="maintenance", items=pair(), auth=True, scan_running=True),
    # ── manual cleanup of a scan's findings ──
    "cleanup_after_scan_uses_proven_pair": tracked(items=pair(), cleanup_after=True),
    "cleanup_after_scan_refuses_untracked": {"items": [item(1, "Art/Alb/01.flac", size=4444)], "sources": [src("New/x.flac", size=4444)],
                                             "cleanup_after": True},
}

# Every cancel check, in order: cancelling at check N stops the job there and nowhere else.
for _n in range(6):
    SCENARIOS[f"cancel_scan_at_check_{_n}"] = {"items": [item(1, "Art/Alb/01.flac", size=4444)],
                                               "sources": [src("New/x.flac", size=4444)], "cancel_after": _n}
for _n in range(4):
    SCENARIOS[f"cancel_ai_at_check_{_n}"] = ai_case(ai(m()), cancel_after=_n)
# Between batches, with the first batch's findings already on the job.
SCENARIOS["cancel_scan_at_second_batch"] = {"items": [item(1, "Art/Alb/one.flac", size=9000)], "sources": many(65), "cancel_after": 68}
SCENARIOS["cancel_ai_at_second_batch"] = ai_case(ai(m(20)), sources=ai_files(21), cancel_after=3)


class Item:
    def __init__(self, fields):
        self.__dict__.update(fields)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def __getitem__(self, key):
        return getattr(self, key)

    def __str__(self):
        return f"<item {self.id}>"


class FakeLib:
    def __init__(self, world):
        self.world = world

    def items(self, query=None):
        self.world.events.append(["lib.items", query])
        if self.world.cfg.get("lib_error"):
            raise OSError("library locked")
        return iter(self.world.items)

    def get_item(self, item_id):
        self.world.events.append(["lib.get_item", item_id])
        return next((it for it in self.world.items if it.id == item_id), None)

    def __str__(self):
        return "<lib>"


class TraceLog(list):
    def __init__(self, world):
        super().__init__()
        self.world = world

    def append(self, line):
        self.world.events.append(["log", line])
        super().append(line)


class Cancel:
    def __init__(self, world, after):
        self.world, self.after, self.checks = world, after, 0

    def is_set(self):
        self.world.events.append(["cancel?"])
        self.checks += 1
        return self.after is not None and self.checks > self.after


class AiResponse:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return self.data


class World:
    """The fake world around the jobs. Everything order-sensitive lands in ``events``."""

    def __init__(self, tmp, cfg):
        self.tmp, self.cfg = tmp, cfg
        self.events = []
        self.tag_reads = []
        self.started = []
        self.armed = False
        self.music = Path(tmp, "music")
        self.downloads = Path(tmp, "downloads")
        self.data_dir = Path(tmp, "data")
        for folder in (self.music, self.downloads, self.data_dir):
            folder.mkdir()
        self.replies = list(cfg.get("ai", []))
        self.items = [self.make_item(row) for row in cfg.get("items", [])]
        self.tags = {}
        for row in cfg.get("sources", []):
            self.make_source(row)

    def write(self, path, size, fill):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((fill or "s").encode() * size)

    def make_item(self, row):
        row = dict(row)
        rel, size, fill, no_file, relative = (row.pop("rel"), row.pop("size", None), row.pop("fill", "l"),
                                              row.pop("no_file", False), row.pop("relative", False))
        path = self.music / rel
        if not no_file:
            size = 1000 + row["id"] if size is None and isinstance(row["id"], int) else (size if size is not None else 1000)
            self.write(path, size, fill)
        return Item(dict({"path": rel if relative else str(path), "mb_trackid": "", "album_id": 0, "mb_albumid": "",
                          "disc": 1, "track": 0, "format": "FLAC", "bitrate": 1000, "length": 180.0, "year": 2024,
                          "albumartist": row.get("artist", "")}, **row))

    def make_source(self, row):
        path = self.downloads / row["rel"]
        self.write(path, row.get("size", 5000 + len(self.tags)), row.get("fill", "s"))
        self.tags[path.name] = row.get("tags", {})

    # ── spies ──
    def clean(self, value):
        if isinstance(value, dict):
            return {str(k): self.clean(v) for k, v in value.items()}
        if isinstance(value, (set, frozenset)):
            return sorted(self.clean(v) for v in value)
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    def read_tags(self, path):
        name = Path(path).name
        self.tag_reads.append(str(path))
        tags = self.tags.get(name, {})
        if tags == "boom":
            raise OSError("unreadable tags")
        return dict(tags)

    def fp_ids(self, path):
        self.events.append(["fp_ids", str(path)])
        result = self.cfg.get("fp_ids", {}).get(Path(path).name, [])
        if result == "boom":
            raise OSError("acoustid down")
        return list(result)

    def fp_match(self, left, right):
        self.events.append(["fp_match", str(left), str(right)])
        for name in self.cfg.get("vanish_on_fingerprint", []):
            for found in self.downloads.rglob(name):
                found.unlink()
        result = self.cfg.get("fp_match", {}).get(Path(left).name, [None, [], []])
        if result == "boom":
            raise OSError("acoustid down")
        return result[0], list(result[1]), list(result[2])

    def fuzzy(self, real):
        def lookup(artist, title, candidates):
            detail = {"count": len(candidates["all"]), "titles": sorted(candidates["by_title"])}
            if self.cfg.get("dump_candidates"):
                detail["rows"] = self.clean(candidates)
            self.events.append(["fuzzy", artist, title, detail])
            if self.cfg.get("real_fuzzy"):
                found = real(artist, title, candidates)
                self.events.append(["fuzzy_real", self.clean(found)])
                return found
            found = self.cfg.get("fuzzy", {}).get(title)
            if not found:
                return None
            return {k: (v.replace("<TMP>", self.tmp) if isinstance(v, str) else v) for k, v in found.items()}
        return lookup

    def variants(self, real):
        def run(row):
            self.arm()
            if row.id in self.cfg.get("variants_error", []):
                raise ValueError("bad variant")
            rows = self.cfg.get("variant_rows", {}).get(str(row.id))
            return [tuple(pair) for pair in rows] if rows else real(row)
        return run

    def arm(self):
        """Files listed earlier vanish or stop being readable once the index build starts."""
        if self.armed:
            return
        self.armed = True
        for name in self.cfg.get("vanish", []):
            for found in self.downloads.rglob(name):
                found.unlink()

    def album_title(self, real):
        def run(library, folder, title, **kwargs):
            self.events.append(["album_title", folder, title, sorted(kwargs)])
            if self.cfg.get("album_error"):
                raise RuntimeError("album lookup exploded")
            return real(library, folder, title, **kwargs)
        return run

    def opened(self, provider, req, timeout=None):
        reply = self.replies.pop(0) if self.replies else {"matches": []}
        body = json.loads(req.data)
        self.events.append(["ai_request", provider, req.full_url, timeout, sorted(req.headers), body])
        if isinstance(reply, str) and reply.startswith("raise:"):
            raise OSError(reply[6:])
        if isinstance(reply, str) and reply.startswith("refusal:"):
            message = {"refusal": reply[8:]}
        elif reply == "empty":
            return AiResponse(json.dumps({"choices": []}).encode())
        elif isinstance(reply, str):
            message = {"content": reply[8:]}
        else:
            message = {"content": json.dumps(reply)}
        return AiResponse(json.dumps({"choices": [{"message": message}]}).encode())

    def start_python(self, fn, **kwargs):
        job_id = f"job-{len(self.started) + 1}"
        self.events.append(["job_start", job_id, self.clean(kwargs)])
        self.started.append((job_id, fn))
        return types.SimpleNamespace(job_id=job_id)

    def update_state(self, payload):
        self.events.append(["update_state", self.clean(payload)])

    def run_job(self, fn):
        cfg = self.cfg
        log = TraceLog(self)
        cancel = None if cfg.get("no_cancel") else Cancel(self, cfg.get("cancel_after"))
        args = (log, cancel) if cfg.get("no_update") else (log, cancel, self.update_state)
        return fn(*args)

    def patches(self):
        time_stub = types.SimpleNamespace(time=lambda: 1700000000.0, sleep=lambda _s: None)
        jobs = mock.Mock()
        jobs.start_python.side_effect = self.start_python
        jobs.get.return_value = None
        lib = FakeLib(self)
        real_stat = Path.stat
        stat_error = set(self.cfg.get("stat_error", []))

        def stat(path, **kwargs):
            if self.armed and path.name in stat_error:
                raise PermissionError(13, "denied", str(path))
            return real_stat(path, **kwargs)

        shared = {"lib": lib, "jobs": jobs, "time": time_stub, "MUSIC_ROOT": self.music,
                  "downloads_root": lambda: self.downloads, "_read_file_media_tags": self.read_tags,
                  "_acoustid_fingerprint_match": self.fp_match, "WEB_MANAGER_DATA_DIR": self.data_dir}
        started = [mock.patch.object(dedup_service, key, value) for key, value in shared.items()]
        started += [mock.patch.object(routes_cleanup, key, value) for key, value in shared.items()]
        started += [
            mock.patch.object(dedup_service, "_BROWSE_ALLOWED_ROOTS", (self.music, self.downloads)),
            # The candidate quality check resolves library paths against this root.
            mock.patch.object(plex_service, "MUSIC_ROOT", self.music),
            mock.patch.object(dedup_service, "_scan_scope_label", lambda path: "scope"),
            mock.patch.object(dedup_service, "_acoustid_fingerprint_ids", self.fp_ids),
            mock.patch.object(dedup_service, "_playlist_match_payload_from_candidates",
                              self.fuzzy(dedup_service._playlist_match_payload_from_candidates)),
            mock.patch.object(dedup_service, "_playlist_item_text_variants",
                              self.variants(dedup_service._playlist_item_text_variants)),
            mock.patch.object(dedup_service, "_resolve_album_title_duplicate_candidate",
                              self.album_title(dedup_service._resolve_album_title_duplicate_candidate)),
            mock.patch.object(dedup_service, "_running_job_of_type", lambda *_a: self.cfg.get("scan_running", False)),
            mock.patch.object(maintenance_service, "_maintenance_load_last_report", lambda: {}),
            mock.patch.object(routes_cleanup, "_ai_api_key", lambda: self.cfg.get("api_key", "synthetic-key")),
            mock.patch.object(routes_cleanup, "_ai_model_and_endpoint", lambda default: (f"model:{default}", "https://ai.invalid/v1")),
            mock.patch.object(routes_cleanup.provider_boundary, "opened", self.opened),
            mock.patch.object(Path, "stat", stat),
            mock.patch.dict(dedup_service._dedup_scans, clear=True),
        ]
        if self.cfg.get("list_error"):
            started.append(mock.patch.object(Path, "rglob", side_effect=OSError("listing denied")))
        return started


def _error_chain(exc):
    """The whole chain: class and text, through __cause__ and __context__ both."""
    chain, seen = [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        chain.append([type(exc).__name__, str(exc), "explicit" if exc.__cause__ is not None else
                      ("implicit" if exc.__context__ is not None and not exc.__suppress_context__ else "end")])
        exc = exc.__cause__ if exc.__cause__ is not None else (None if exc.__suppress_context__ else exc.__context__)
    return chain


def _fill(value, tmp):
    return json.loads(json.dumps(value).replace("<TMP>", tmp))


def _attempt(world, call):
    try:
        return {"result": world.clean(call())}
    except Exception as exc:
        return {"error": _error_chain(exc)}


def _safety_decisions(world, result):
    """What the REAL safety code downstream decides from this job's output."""
    if not isinstance(result, dict):
        return {}
    decisions = {"unattended_plan": _attempt(world, lambda: dedup_service._maintenance_duplicate_plan(result))}
    paths = [d.get("source_path") for d in result.get("duplicates") or []]
    decisions["reviewed_pairs"] = _attempt(world, lambda: dedup_service._dedup_pairs_for_paths(paths))
    return decisions


def _run_jobs(world, cfg, trace):
    """Run every started job body; a rerun calls the same body again."""
    outcomes = []
    for _job_id, fn in list(world.started):
        for run in range(cfg.get("runs", 1)):
            if run and cfg.get("drop_items_between_runs"):
                world.items = []
            outcome = _attempt(world, lambda: world.run_job(fn))
            outcome["safety"] = _safety_decisions(world, outcome.get("result"))
            outcomes.append(outcome)
    trace["outcomes"] = outcomes


def _start_scan(world, cfg, tmp, trace):
    starts = cfg.get("starts") or [cfg.get("payload", {"path": "<TMP>/downloads"})]
    trace["http"] = [_attempt(world, lambda: dedup_service.start_dedup_scan(_fill(body, tmp))) for body in starts]


def _start_ai(world, cfg, tmp, trace):
    source = None
    if not cfg.get("no_source"):
        duplicates = [{"source_path": str(world.downloads / rel)} for rel in cfg.get("matched", [])]
        source = dict({"kind": "scan", "created_at": 1.0, "status": "done", "scan_path": str(world.downloads),
                       "duplicates": duplicates}, **_fill(cfg.get("source", {}), tmp))
        dedup_service._dedup_scans["scan-1"] = source
    body = _fill(cfg.get("body", {"scan_jid": "scan-1"}), tmp)

    def call():
        with app_module.app.test_request_context("/api/dedup/ai-review", method="POST", json=body):
            response = routes_cleanup.dedup_ai_review()
            return [response.get_json(), response.status_code]

    trace["http"] = [_attempt(world, call)]
    if cfg.get("source_cleared_after_start") and source:
        source["duplicates"] = []


def _run_maintenance(world, cfg, trace):
    auth = cfg.get("auth")
    if auth == "malformed":
        (world.data_dir / dedup_authorization.STATE_FILE_NAME).write_text("{not json", encoding="utf-8")
    elif auth is not None:
        dedup_authorization.set_unattended_delete(world.data_dir, auth, actor="test", reason="test")
    real_gate = dedup_authorization.unattended_delete_enabled

    def gate(data_dir):
        allowed = real_gate(data_dir)
        world.events.append(["authorization_gate", str(data_dir), allowed])
        return allowed

    def wait_child(child_id, *_a, **_k):
        world.events.append(["wait_child", child_id])
        return world.run_job(world.started[-1][1])

    def cleanup(proposal, log):
        world.events.append(["unattended_cleanup", world.clean(proposal)])
        return {"ok": True, "deleted": len(proposal), "skipped": 0, "folders_removed": 0, "results": []}

    log = TraceLog(world)
    with mock.patch.object(dedup_service._dedup_authorization, "unattended_delete_enabled", gate), \
            mock.patch.object(dedup_service, "_wait_for_child_job", wait_child), \
            mock.patch.object(dedup_service, "_unattended_reviewed_cleanup", cleanup), \
            mock.patch.object(dedup_service, "_maintenance_save_last_report",
                              lambda report, _log: world.events.append(["save_report", sorted(report)])):
        trace["maintenance"] = _attempt(world, lambda: dedup_service._maintenance_full_duplicate_scan(log, None))


def _run_cleanup_after(world, trace):
    """Live manual cleanup of every source file: only proven pairs may reach the plan."""
    paths = [str(p) for p in sorted(world.downloads.rglob("*")) + sorted(world.music.rglob("*")) if p.is_file()]

    def plan(pairs, reason=""):
        world.events.append(["plan_reviewed_cleanup", world.clean(pairs), reason])
        return {"ok": False, "skipped": []}

    with mock.patch.object(dedup_service._duplicate_cleanup, "plan_reviewed_cleanup", plan):
        trace["cleanup"] = _attempt(world, lambda: dedup_service.run_dedup_cleanup({"paths": paths, "dry_run": False}))


def run_scenario(cfg):
    """One scenario in a fresh world; the JSON-safe trace with paths normalized."""
    tmp = tempfile.mkdtemp(prefix="dedupchar")
    world = World(tmp, cfg)
    trace = {}
    patches = world.patches()
    for patch in patches:
        patch.start()
    try:
        kind = cfg.get("kind", "scan")
        if kind == "maintenance":
            _run_maintenance(world, cfg, trace)
        else:
            (_start_ai if kind == "ai" else _start_scan)(world, cfg, tmp, trace)
            _run_jobs(world, cfg, trace)
        if cfg.get("cleanup_after"):
            _run_cleanup_after(world, trace)
        trace["events"] = world.events
        trace["tag_reads"] = sorted(world.tag_reads)  # read on pool threads, so unordered
        trace["jobs_state"] = {jid: {k: v for k, v in world.clean(state).items() if k != "log"}
                               for jid, state in dedup_service._dedup_scans.items()}
        return _normalize(trace, tmp)
    finally:
        for patch in reversed(patches):
            patch.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def _normalize(trace, tmp):
    text = json.dumps(trace, sort_keys=True, indent=1, ensure_ascii=False)
    for path in {tmp, os.path.realpath(tmp)}:
        text = text.replace(path, "<TMP>")
    return json.loads(text)


class DedupJobsCharacterizationTests(unittest.TestCase):
    maxDiff = None

    def test_golden_traces(self):
        actual = {name: run_scenario(copy.deepcopy(cfg)) for name, cfg in SCENARIOS.items()}
        if os.environ.get("UPDATE_DEDUP_JOBS_GOLDEN") == "1":
            GOLDEN.parent.mkdir(exist_ok=True)
            GOLDEN.write_text(json.dumps(actual, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(sorted(golden), sorted(actual))
        for name in SCENARIOS:
            with self.subTest(scenario=name):
                self.assertEqual(golden[name], actual[name])


if __name__ == "__main__":
    unittest.main()
