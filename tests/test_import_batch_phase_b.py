"""0.7.0 deterministic batch import, phase B (#288, T6).

Each folder runs Stage 1 (Beets quiet import), then for a folder Beets
skipped Stage 2 (identify_import_folder + a review row), and Stage 2b (a
pinned re-import) only when auto_import.eligible. Only the Beets/provider
edges are stubbed; the batch loop, stage logic and _ai_import_folder are real.
"""
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
REL = "55555555-5555-5555-5555-555555555555"
RG = "66666666-6666-6666-6666-666666666666"
OTHER_RG = "77777777-7777-7777-7777-777777777777"


def setUpModule():
    # Import at run time: app_runtime reads the environment when first imported.
    global ai, cw
    tmp = Path(tempfile.mkdtemp(prefix="bwm_phase_b_env_"))
    for key, value in (("BEETSDIR", tmp / "config"), ("LIB_PATH", tmp / "config" / "musiclibrary.blb"),
                       ("AI_BATCH_STATE_DIR", tmp / "ai_batch_jobs"), ("METADATA_CACHE_DIR", tmp / "cache"),
                       ("BEETS_TRANSACTION_DIR", tmp / "transactions"), ("WEB_MANAGER_DATA_DIR", tmp / "data")):
        os.environ.setdefault(key, str(value))
    (tmp / "config").mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT))
    import backend.ai_service as ai
    import backend.composite_workflows as cw


def identity(eligible, reasons=()):
    return {
        "folder": "", "skip_reason": "no_strong_match",
        "candidates": [{"release_group_id": RG, "representative_release_id": REL, "deterministic_rank": 1,
                        "ai_rank": None, "evidence": {"preflight": {"release_title": "Album",
                                                                     "release_artist": "Artist"}}}],
        "auto_import": {"eligible": eligible, "release_group_id": RG if eligible else "",
                        "release_id": REL if eligible else "", "reasons": list(reasons),
                        "post_conditions": ["beets_strong_match", "single_new_album_with_release_group"]},
        "conflicts": [], "warnings": [], "ai_state": {"status": "not_run"},
    }


def imported(ids):
    return {"ok": True, "albums_imported": len(ids), "imported_album_ids": ids, "skipped": [],
            "not_matched": [], "not_matched_known": True}


def skipped(folder, reason="no_strong_match"):
    return {"ok": True, "albums_imported": 0, "imported_album_ids": [],
            "skipped": [{"path": folder, "reason": reason}], "not_matched": [folder], "not_matched_known": True}


class _Batch(unittest.TestCase):
    """Two album folders in a throwaway downloads tree and the stubbed edges."""

    chroma = True
    torrent = False

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bwm_phase_b_")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.folders = []
        for name in ("A", "B"):
            (self.tmp / name).mkdir()
            (self.tmp / name / "01.flac").write_bytes(b"x")
            self.folders.append(str(self.tmp / name))
        self.roots = [self.tmp]  # the configured import roots (resolved)
        self.batch_id = f"phase-b-{id(self)}"
        self.cancel = threading.Event()
        self.reimport = {}   # folder -> result (default: imported([7]))
        self.ident = identity(False, ["no_release_group_verified"])
        self.album_rg = {7: RG, 9: RG}
        self.apply_result = {"ok": True, "album_id": 9, "mb_releasegroupid": RG}
        self.history = mock.Mock()
        self.queued = mock.Mock(return_value="rid")
        self.stubs = {
            "reimport_source": mock.Mock(side_effect=lambda f, opts, timeout: self.reimport.get(f, imported([7]))),
            "identify": mock.Mock(side_effect=lambda f, r: self.ident),
            "plan": mock.Mock(return_value={"ok": True, "operation_id": "op"}),
            "apply": mock.Mock(side_effect=lambda op: self.apply_result),
        }
        status = {"capabilities": ["mbsubmit"] if self.chroma else [], "fpcalc_available": self.chroma}
        patches = [
            mock.patch.object(ai, "_ai_batch_find_audio_dirs", side_effect=lambda root, log=None, unreadable=None: list(self.folders)),
            mock.patch.object(ai, "_ai_batch_commit"),
            mock.patch.object(ai, "_ai_batch_already_in_library", return_value=False),
            mock.patch.object(ai, "_pending_review_has_path", return_value=False),
            mock.patch.object(ai, "_preserve_torrent_source_path", return_value=self.torrent),
            mock.patch.object(ai, "_import_review_cleanup_roots", side_effect=lambda **k: list(self.roots)),
            mock.patch.object(ai, "_validate_import_source_audio"),
            mock.patch.object(ai.composite_workflows, "reimport_source", self.stubs["reimport_source"]),
            mock.patch.object(ai.composite_workflows, "get_album",
                              side_effect=lambda aid: {"mb_releasegroupid": self.album_rg.get(aid, "")}),
            mock.patch.object(ai.composite_workflows, "find_all_items_by_album_id", return_value=[]),
            mock.patch.object(ai.composite_workflows, "plan_confirmed_import", self.stubs["plan"]),
            mock.patch.object(ai.composite_workflows, "apply_confirmed_import", self.stubs["apply"]),
            mock.patch.object(ai, "identify_import_folder", self.stubs["identify"]),
            mock.patch.object(ai, "_ai_batch_queue_pending_review", self.queued),
            mock.patch.object(ai, "_record_recent_import", self.history),
            mock.patch.object(ai, "_fetch_mb_release_tracklist",
                              return_value={"ok": True, "release_group": RG, "tracks": []}),
            mock.patch.object(ai, "_fetch_artwork_after_retag", return_value={
                "identity_verified": True, "artwork_status": "fetched", "artwork_retryable": False}),
            mock.patch.object(ai, "_remove_pending_review_for_path"),
            mock.patch.object(ai, "lib"),
            mock.patch.object(ai, "_auto_merge_case_duplicate_artist_folder"),
            mock.patch.object(ai, "_invalidate_lib_cache"),
            mock.patch.object(ai, "_trigger_plex_refresh"),
            mock.patch.object(ai.beets_adapter, "get_plugin_status", return_value=status),
            mock.patch.object(ai, "acoustid_api_key", return_value="configured"),
            mock.patch.object(ai, "_ai_api_key", return_value=""),
            # AI is never consulted, and no folder gets its own thread (F11).
            mock.patch.object(ai, "_ai_suggest_folder_internal", side_effect=AssertionError("AI called")),
            mock.patch.object(ai.provider_boundary, "opened", side_effect=AssertionError("provider called")),
            mock.patch.object(ai.threading, "Thread", side_effect=AssertionError("raw thread started")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_batch(self):
        self.log = []
        out = ai._run_ai_batch_import(self.batch_id, str(self.tmp), self.log, self.cancel, None)
        return out, {f["source_folder"]: f for f in out["folders"]}

    def evidence(self, call):
        return call.kwargs["evidence"]


class Stage1Tests(_Batch):
    def test_beets_strong_match_imports_and_moves_non_torrent_source(self):
        out, folders = self.run_batch()
        a = folders[self.folders[0]]
        self.assertEqual((a["status"], a["album_ids"], a["stage"]), ("imported", [7], "done"), self.log)
        opts = self.stubs["reimport_source"].call_args_list[0].args[1]
        self.assertEqual((opts["quiet_fallback"], opts["copy"], opts["move"]), ("skip", False, True))
        self.stubs["identify"].assert_not_called()
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["warnings"], [])

    def test_history_recorded_once_per_imported_album(self):
        self.reimport[self.folders[0]] = imported([7, 8])
        self.album_rg[8] = RG
        self.run_batch()
        # A: albums 7 and 8; B: album 7 (default stub) -- one row each.
        self.assertEqual(self.history.call_count, 3)

    def test_album_without_release_group_goes_to_review(self):
        self.album_rg[7] = ""
        out, folders = self.run_batch()
        a = folders[self.folders[0]]
        self.assertEqual(a["status"], "review_required")
        self.assertEqual(a["missing_release_group_album_ids"], [7])
        self.assertIn("Library: missing IDs", a["failure_reason"])
        self.assertEqual(self.history.call_count, 2)  # still in History, once
        self.stubs["identify"].assert_not_called()  # never "repaired" through another ID
        self.assertEqual(out["status"], "completed_with_warnings")

    def test_beets_failure_is_retryable_and_batch_continues(self):
        self.reimport[self.folders[0]] = {"ok": False, "error": "Beets unavailable"}
        out, folders = self.run_batch()
        self.assertEqual(folders[self.folders[0]]["status"], "import_failed")
        self.assertEqual(folders[self.folders[1]]["status"], "imported")
        self.assertEqual(out["folders_retryable"], 1)

    def test_partial_import_gets_one_review_row_listing_skipped_paths(self):
        disc2 = str(Path(self.folders[0]) / "CD2")
        self.reimport[self.folders[0]] = {"ok": True, "albums_imported": 1, "imported_album_ids": [7],
                                          "skipped": [{"path": disc2, "reason": "no_strong_match"}],
                                          "not_matched": [disc2], "not_matched_known": True}
        _, folders = self.run_batch()
        a = folders[self.folders[0]]
        self.assertEqual(a["status"], "review_required")
        self.assertEqual(a["review_item_id"], "rid")
        self.queued.assert_called_once()
        call = self.queued.call_args
        self.assertEqual(call.args[1]["kept_album_ids"], [7])
        ev = self.evidence(call)
        self.assertEqual((ev["origin_step"], ev["skipped_paths"], ev["kept_album_ids"]),
                         ("beets_partial", [disc2], [7]))

    def test_symlinked_audio_is_never_handed_to_beets(self):
        target = self.tmp / "elsewhere.flac"
        target.write_bytes(b"x")
        try:
            (Path(self.folders[0]) / "02.flac").symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks here")
        _, folders = self.run_batch()
        a = folders[self.folders[0]]
        self.assertEqual((a["status"], a["failure_reason"]), ("review_created", "symlinked_audio"))
        self.assertEqual([c.args[0] for c in self.stubs["reimport_source"].call_args_list], [self.folders[1]])
        ev = self.evidence(self.queued.call_args)
        self.assertEqual((ev["conflict"], ev["symlinked_paths"]),
                         ("symlinked_audio", [str(Path(self.folders[0]) / "02.flac")]))
        self.assertEqual(folders[self.folders[1]]["status"], "imported")

    def test_folder_without_readable_audio_is_unverifiable(self):
        (Path(self.folders[0]) / "01.flac").unlink()
        (Path(self.folders[0]) / "notes.txt").write_text("x")
        _, folders = self.run_batch()
        a = folders[self.folders[0]]
        self.assertEqual((a["status"], a["failure_reason"]), ("review_created", "unverifiable_folder"))
        self.assertEqual([c.args[0] for c in self.stubs["reimport_source"].call_args_list], [self.folders[1]])
        # The shared helper fails closed when the folder cannot be resolved or listed (QA394-F4).
        from backend.import_identity import _recursive_audio
        self.assertEqual(_recursive_audio(str(self.tmp / "missing")), ([], True))

    def test_older_plugin_without_album_ids_is_not_verified(self):
        self.reimport[self.folders[0]] = {"ok": True, "albums_imported": 1, "imported_album_ids": None,
                                          "skipped": [], "not_matched": [], "not_matched_known": False}
        _, folders = self.run_batch()
        self.assertEqual(folders[self.folders[0]]["status"], "review_required")
        self.assertEqual(self.history.call_count, 1)  # only folder B's album


class PreImportCheckTests(_Batch):
    """Security review of #394 at cce1711: B1, L2, I3."""

    def beets_folders(self):
        return [c.args[0] for c in self.stubs["reimport_source"].call_args_list]

    def link(self, link, target, is_dir=False):
        try:
            link.symlink_to(target, target_is_directory=is_dir)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks here")

    def test_link_with_a_non_audio_name_to_audio_is_refused(self):
        target = self.tmp / "elsewhere.flac"
        target.write_bytes(b"x")
        self.link(Path(self.folders[0]) / "notes.txt", target)  # Beets sniffs content
        _, folders = self.run_batch()
        a = folders[self.folders[0]]
        self.assertEqual(a["failure_reason"], "symlinked_audio")
        self.assertEqual(self.beets_folders(), [self.folders[1]])
        self.assertEqual(self.evidence(self.queued.call_args)["symlinked_paths"],
                         [str(Path(self.folders[0]) / "notes.txt")])

    def test_linked_directory_holding_any_file_is_refused(self):
        other = self.tmp / "elsewhere"
        other.mkdir()
        (other / "track").write_bytes(b"x")  # no extension
        self.link(Path(self.folders[0]) / "extra", other, is_dir=True)
        _, folders = self.run_batch()
        self.assertEqual(folders[self.folders[0]]["failure_reason"], "symlinked_audio")
        self.assertEqual(self.beets_folders(), [self.folders[1]])

    def test_hardlinked_audio_is_copied_not_moved(self):
        try:
            os.link(Path(self.folders[0]) / "01.flac", self.tmp / "seeding.flac")
        except (OSError, NotImplementedError):
            self.skipTest("cannot create hardlinks here")
        _, folders = self.run_batch()
        self.assertEqual(folders[self.folders[0]]["status"], "imported")
        opts = {c.args[0]: c.args[1] for c in self.stubs["reimport_source"].call_args_list}
        self.assertEqual((opts[self.folders[0]]["copy"], opts[self.folders[0]]["move"]), (True, False))
        self.assertEqual((opts[self.folders[1]]["copy"], opts[self.folders[1]]["move"]), (False, True))
        self.assertTrue(any("Hardlinked source" in line for line in self.log))

    def test_hardlinked_audio_is_copied_in_the_pinned_import_too(self):
        try:
            os.link(Path(self.folders[0]) / "01.flac", self.tmp / "seeding.flac")
        except (OSError, NotImplementedError):
            self.skipTest("cannot create hardlinks here")
        self.reimport[self.folders[0]] = skipped(self.folders[0])
        self.ident = identity(True)
        self.run_batch()
        self.assertIs(self.stubs["plan"].call_args.args[0]["use_move"], False)

    def test_linked_empty_directory_is_refused(self):
        # N8: the target can be filled or re-pointed before Beets walks it.
        (self.tmp / "empty").mkdir()
        self.link(Path(self.folders[0]) / "Scans", self.tmp / "empty", is_dir=True)
        _, folders = self.run_batch()
        self.assertEqual(folders[self.folders[0]]["failure_reason"], "symlinked_audio")
        self.assertEqual(self.beets_folders(), [self.folders[1]])

    def test_hardlinked_non_audio_file_means_copy(self):
        # N11: Beets moves every file in the folder, not only audio.
        cover = Path(self.folders[0]) / "cover.jpg"
        cover.write_bytes(b"jpg")
        try:
            os.link(cover, self.tmp / "seeding.jpg")
        except (OSError, NotImplementedError):
            self.skipTest("cannot create hardlinks here")
        self.run_batch()
        opts = {c.args[0]: c.args[1] for c in self.stubs["reimport_source"].call_args_list}
        self.assertEqual((opts[self.folders[0]]["copy"], opts[self.folders[0]]["move"]), (True, False))

    def test_pinned_import_rechecks_hardlinks_itself(self):
        # N10: Stage 2b does not reuse Stage 1's answer; a hardlink made after
        # Stage 1 moved nothing still forces a copy, logged as such.
        def stage1(folder, opts, timeout):
            if folder == self.folders[0]:
                os.link(Path(folder) / "01.flac", self.tmp / "late.flac")
                return skipped(folder)
            return imported([7])
        self.stubs["reimport_source"].side_effect = stage1
        self.ident = identity(True)
        self.folders = self.folders[:1]
        try:
            self.run_batch()
        except (OSError, NotImplementedError):
            self.skipTest("cannot create hardlinks here")
        opts = self.stubs["reimport_source"].call_args.args[1]
        self.assertEqual((opts["copy"], opts["move"]), (False, True))
        self.assertIs(self.stubs["plan"].call_args.args[0]["use_move"], False)
        self.assertTrue(any("Hardlinked source" in line for line in self.log), self.log)
        self.assertFalse(any("[torrent]" in line for line in self.log), self.log)

    def test_non_batch_pinned_import_copies_hardlinked_source(self):
        try:
            os.link(Path(self.folders[0]) / "01.flac", self.tmp / "seeding.flac")
        except (OSError, NotImplementedError):
            self.skipTest("cannot create hardlinks here")
        log = []
        ai._ai_import_folder(self.folders[0], REL, {}, log)
        self.assertIs(self.stubs["plan"].call_args.args[0]["use_move"], False)
        self.assertFalse(any("[torrent]" in line for line in log), log)

    def test_symlinked_import_root_with_normal_folders_imports(self):
        real = self.tmp / "real_downloads"
        (real / "Album").mkdir(parents=True)
        (real / "Album" / "01.flac").write_bytes(b"x")
        mount = self.tmp / "downloads"
        self.link(mount, real, is_dir=True)  # the operator's own mount
        self.roots = [mount]
        self.folders = [str(mount / "Album")]
        _, folders = self.run_batch()
        self.assertEqual(folders[self.folders[0]]["status"], "imported", self.log)

    def test_link_below_the_import_root_is_refused(self):
        real = self.tmp / "real_sub"
        (real / "Album").mkdir(parents=True)
        (real / "Album" / "01.flac").write_bytes(b"x")
        self.link(self.tmp / "sub", real, is_dir=True)
        self.folders = [str(self.tmp / "sub" / "Album")]
        _, folders = self.run_batch()
        a = folders[self.folders[0]]
        self.assertEqual(a["failure_reason"], "symlinked_audio")
        self.assertEqual(self.evidence(self.queued.call_args)["symlinked_paths"], [str(self.tmp / "sub")])
        self.assertEqual(self.beets_folders(), [])

    def test_folder_outside_every_configured_root_is_refused(self):
        self.roots = [self.tmp / "nowhere"]
        _, folders = self.run_batch()
        self.assertEqual({f["failure_reason"] for f in folders.values()}, {"unverifiable_folder"})
        self.assertEqual(self.beets_folders(), [])

    @unittest.skipUnless(hasattr(os, "geteuid"), "POSIX permissions")
    def test_unreadable_part_is_unverifiable_not_symlinked(self):
        if os.geteuid() == 0:
            self.skipTest("root reads everything")
        sub = Path(self.folders[0]) / "CD2"
        sub.mkdir()
        sub.chmod(0)
        self.addCleanup(sub.chmod, 0o755)
        _, folders = self.run_batch()
        self.assertEqual(folders[self.folders[0]]["failure_reason"], "unverifiable_folder")
        self.assertEqual(self.beets_folders(), [self.folders[1]])
        self.assertEqual(self.evidence(self.queued.call_args)["unreadable_paths"], [str(sub)])


class ScanRootSymlinkSpellingTests(unittest.TestCase):
    """N7: the symlinked mount spelling of a configured root is that root;
    containment below a root is unchanged."""

    def test_symlinked_spelling_of_a_root_scans_the_root(self):
        from backend import serializers as s
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        real, other = tmp / "downloads", tmp / "other"
        (real / "Album").mkdir(parents=True)
        other.mkdir()
        try:
            os.symlink(real, tmp / "mount", target_is_directory=True)
            os.symlink(other, tmp / "elsewhere", target_is_directory=True)
            os.symlink(real / "Album", tmp / "album_link", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported here")
        root = real.resolve()
        with mock.patch.object(s, "_import_review_cleanup_roots", side_effect=lambda **k: [root]):
            self.assertEqual(s._resolve_import_scan_root(str(tmp / "mount")), (root, None))
            for refused in ("elsewhere", "album_link"):
                path, err = s._resolve_import_scan_root(str(tmp / refused))
                self.assertIsNone(path, refused)
                self.assertIn("outside the allowed roots", err)


class TorrentSourceTests(_Batch):
    torrent = True

    def test_torrent_source_is_copied_in_both_imports(self):
        self.reimport[self.folders[0]] = skipped(self.folders[0])
        self.ident = identity(True)
        self.run_batch()
        opts = self.stubs["reimport_source"].call_args_list[0].args[1]
        self.assertEqual((opts["copy"], opts["move"]), (True, False))
        self.assertIs(self.stubs["plan"].call_args.args[0]["use_move"], False)


class Stage2Tests(_Batch):
    def test_skipped_folder_gets_review_with_deterministic_evidence(self):
        self.reimport[self.folders[0]] = skipped(self.folders[0], "no_strong_match")
        _, folders = self.run_batch()
        a = folders[self.folders[0]]
        self.assertEqual((a["status"], a["beets_skip_reason"]), ("review_created", "no_strong_match"))
        self.assertEqual(a["auto_import"], {"eligible": False, "reasons": ["no_release_group_verified"]})
        self.stubs["identify"].assert_called_once_with(self.folders[0], "no_strong_match")
        self.stubs["plan"].assert_not_called()
        call = self.queued.call_args
        self.assertEqual(call.args[0], self.folders[0])
        self.assertEqual((call.args[1]["mb_albumid"], call.args[1]["mb_releasegroupid"]), (REL, RG))
        ev = self.evidence(call)
        self.assertEqual((ev["origin_step"], ev["beets_skip_reason"], ev["ai_rank"]),
                         ("beets_skip", "no_strong_match", None))
        self.assertIs(ev["deterministic_decision"], self.ident)

    def test_skip_reason_priority(self):
        res = {"skipped": [{"path": "a", "reason": "no_candidates"}, {"path": "b", "reason": "duplicate"}]}
        self.assertEqual(ai._ai_batch_skip_reason(res), "duplicate")
        self.assertEqual(ai._ai_batch_skip_reason({"skipped": []}), "")


class Stage2bTests(_Batch):
    def setUp(self):
        super().setUp()
        self.reimport[self.folders[0]] = skipped(self.folders[0])
        self.ident = identity(True)

    def test_eligible_folder_is_imported_pinned_and_verified(self):
        _, folders = self.run_batch()
        a = folders[self.folders[0]]
        self.assertEqual((a["status"], a["album_id"], a["pinned_release_id"]), ("imported", 9, REL), self.log)
        self.assertEqual(self.stubs["plan"].call_args.args[0]["mb_albumid"], REL)
        self.assertIs(self.stubs["plan"].call_args.args[0]["use_move"], True)
        self.assertEqual(self.stubs["reimport_source"].call_count, 2)  # Stage 1 for each folder only
        self.queued.assert_not_called()
        self.assertEqual(self.history.call_count, 2)  # A via Stage 2b, B via Stage 1

    def test_beets_not_strong_on_pinned_import_goes_to_review(self):
        self.apply_result = {"ok": False, "code": "not_imported", "error": "no confident match"}
        _, folders = self.run_batch()
        a = folders[self.folders[0]]
        self.assertEqual(a["status"], "review_created")
        self.assertIn("no confident match", self.evidence(self.queued.call_args)["stage2b_error"])
        self.assertEqual(self.history.call_count, 1)  # folder B only

    def test_release_group_post_condition_failure_keeps_rows_for_review(self):
        self.album_rg[9] = OTHER_RG
        _, folders = self.run_batch()
        self.assertEqual(folders[self.folders[0]]["status"], "review_created")
        self.assertEqual(self.queued.call_args.args[1]["kept_album_ids"], [9])

    def test_apply_verification_failure_records_kept_album_ids(self):
        self.apply_result = {"ok": False, "code": "release_group_mismatch", "error": "different Release Group",
                             "album_ids": [9]}
        self.run_batch()
        self.assertEqual(self.queued.call_args.args[1]["kept_album_ids"], [9])


class AiOffTests(_Batch):
    def test_no_ai_key_runs_deterministically(self):
        self.reimport[self.folders[0]] = skipped(self.folders[0])
        out, _ = self.run_batch()
        self.assertIs(out["ai_configured"], False)
        self.assertEqual(out["evidence_capabilities"],
                         {"beets_fingerprinting": True, "acoustid_key": True, "ai_configured": False})

    def test_ai_key_set_still_never_calls_ai(self):
        with mock.patch.object(ai, "_ai_api_key", return_value="configured"):
            self.reimport[self.folders[0]] = skipped(self.folders[0])
            out, folders = self.run_batch()
        self.assertIs(out["ai_configured"], True)
        self.assertEqual(folders[self.folders[0]]["status"], "review_created")


class NoChromaTests(_Batch):
    chroma = False

    def test_strong_match_imports_with_visible_warning(self):
        out, folders = self.run_batch()
        a = folders[self.folders[0]]
        self.assertEqual(a["status"], "imported")
        self.assertEqual(a["warnings"], ["imported_without_fingerprint"])
        self.assertIs(out["evidence_capabilities"]["beets_fingerprinting"], False)
        self.assertTrue(any("fingerprinting" in w for w in out["warnings"]), out["warnings"])
        self.assertTrue(any(line.startswith("WARNING:") and "fingerprinting" in line for line in self.log))
        self.assertTrue(any("without fingerprint evidence" in line for line in self.log))


class InterruptTests(_Batch):
    def test_cancel_between_stage2_and_stage2b(self):
        self.reimport[self.folders[0]] = skipped(self.folders[0])
        eligible = identity(True)

        def identify(folder, reason):
            self.cancel.set()
            return eligible

        self.stubs["identify"].side_effect = identify
        out, folders = self.run_batch()
        self.assertEqual(folders[self.folders[0]]["failure_reason"], "batch canceled")
        self.assertEqual(folders[self.folders[1]]["status"], "skipped")
        self.stubs["plan"].assert_not_called()
        self.assertEqual(self.stubs["reimport_source"].call_count, 1)
        self.assertEqual(out["status"], "canceled")

    def test_skip_current_mid_folder_stops_that_folder_only(self):
        first = self.folders[0]

        def reimport(folder, opts, timeout):
            if folder == first:
                ai._ai_batch_control(self.batch_id)["skip_current"] = True
                return skipped(folder)
            return imported([7])

        self.stubs["reimport_source"].side_effect = reimport
        _, folders = self.run_batch()
        self.assertEqual(folders[first]["status"], "skipped")
        self.assertEqual(folders[first]["failure_reason"], "skipped by user")
        self.stubs["identify"].assert_not_called()
        self.assertEqual(folders[self.folders[1]]["status"], "imported")

    def test_pause_stops_before_the_next_folder(self):
        def reimport(folder, opts, timeout):
            ai._ai_batch_control(self.batch_id)["pause"] = True
            return imported([7])

        self.stubs["reimport_source"].side_effect = reimport
        out, folders = self.run_batch()
        self.assertEqual(out["status"], "paused")
        self.assertEqual(folders[self.folders[1]]["status"], "queued")
        self.assertEqual(self.stubs["reimport_source"].call_count, 1)


if __name__ == "__main__":
    unittest.main()
