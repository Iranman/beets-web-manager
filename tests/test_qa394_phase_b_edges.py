"""Independent QA for PR #394 (deterministic batch import, phase B).

Reuses the stubbed Beets/provider edges of test_import_batch_phase_b; the
batch loop, stage logic, recovery and _ai_import_folder are real.
"""
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

import tests.test_import_batch_phase_b as pb
from tests.test_import_batch_phase_b import RG, REL, _Batch, identity, imported, skipped


def setUpModule():
    pb.setUpModule()
    global ai
    ai = pb.ai


class SkipControlTests(_Batch):
    def test_skip_pressed_during_a_successful_stage1_does_not_skip_the_next_folder(self):
        first = self.folders[0]

        def reimport(folder, opts, timeout):
            if folder == first:  # user presses "Skip current" while Beets imports A
                ai._ai_batch_control(self.batch_id)["skip_current"] = True
            return imported([7])

        self.stubs["reimport_source"].side_effect = reimport
        _, folders = self.run_batch()
        self.assertEqual(folders[first]["status"], "imported")
        # Folder B was never the "current" folder when Skip was pressed.
        self.assertEqual(folders[self.folders[1]]["status"], "imported", self.log)


class PartialImportTests(_Batch):
    def test_partial_import_is_review_required_with_history_once_and_no_stage2(self):
        disc2 = str(Path(self.folders[0]) / "CD2")
        self.reimport[self.folders[0]] = {"ok": True, "albums_imported": 1, "imported_album_ids": [7],
                                          "skipped": [{"path": disc2, "reason": "no_strong_match"}],
                                          "not_matched": [disc2], "not_matched_known": True}
        _, folders = self.run_batch()
        a = folders[self.folders[0]]
        self.assertEqual(a["status"], "review_required")
        self.assertIn(disc2, a["failure_reason"])
        self.assertEqual(a["album_ids"], [7])
        self.assertEqual(self.history.call_count, 2)  # A's album once, B's album once
        self.stubs["identify"].assert_not_called()
        self.stubs["plan"].assert_not_called()


class RecoveryTests(_Batch):
    def _state_with(self, status, **extra):
        state = ai._ai_batch_initial_state(self.batch_id, str(self.tmp))
        for f in self.folders:
            fs = ai._ai_batch_folder_state(self.batch_id, f)
            fs.update({"status": status, **extra})
            state["folder_states"][fs["folder_id"]] = fs
        return state

    def _recover(self, state):
        self.log = []
        with mock.patch.object(ai, "_ai_batch_load_state", return_value=state), \
                mock.patch.object(ai, "_pending_review_path_set", return_value=set()):
            out = ai._run_ai_batch_import(self.batch_id, str(self.tmp), self.log, self.cancel, None,
                                          recover=True)
        return out, {f["source_folder"]: f for f in out["folders"]}

    def test_cached_ai_decision_is_never_trusted_on_recovery(self):
        cached = {"ok": True, "suggestion": {"confidence": "high", "mb_valid": True, "mb_albumid": REL,
                                             "mb_releasegroupid": RG}}
        state = self._state_with("ai_completed", ai_result=cached)
        self.reimport[self.folders[0]] = skipped(self.folders[0])  # ineligible -> review
        with mock.patch.object(ai, "_ai_import_folder", side_effect=AssertionError("cached AI pick imported")):
            out, folders = self._recover(state)
        self.assertEqual(folders[self.folders[0]]["status"], "review_created", self.log)
        self.assertEqual(folders[self.folders[1]]["status"], "imported")
        self.assertEqual(self.stubs["reimport_source"].call_count, 2)

    def test_folder_interrupted_mid_import_reruns_stages_and_library_check_wins(self):
        state = self._state_with("importing", stage="beets_import")
        # Folder A was imported by Beets before the crash; B was not.
        with mock.patch.object(ai, "_ai_batch_already_in_library", side_effect=lambda f: f == self.folders[0]):
            _, folders = self._recover(state)
        self.assertEqual(folders[self.folders[0]]["failure_reason"], "already in library")
        self.assertEqual(folders[self.folders[1]]["status"], "imported")
        self.assertEqual(self.stubs["reimport_source"].call_count, 1)  # no second import of A


class NoDeletionTests(_Batch):
    def test_no_path_through_the_batch_deletes_anything(self):
        # A: skipped -> Stage 2b with RG mismatch; B: Beets failure.
        self.reimport[self.folders[0]] = skipped(self.folders[0])
        self.reimport[self.folders[1]] = {"ok": False, "error": "Beets unavailable"}
        self.ident = identity(True)
        self.album_rg[9] = "77777777-7777-7777-7777-777777777777"
        boom = AssertionError("delete called")
        with mock.patch.object(shutil, "rmtree", side_effect=boom), \
                mock.patch.object(os, "remove", side_effect=boom), \
                mock.patch.object(os, "unlink", side_effect=boom), \
                mock.patch.object(os, "rmdir", side_effect=boom), \
                mock.patch.object(Path, "unlink", side_effect=boom), \
                mock.patch.object(Path, "rmdir", side_effect=boom):
            _, folders = self.run_batch()
        self.assertEqual(folders[self.folders[0]]["status"], "review_created")
        self.assertEqual(folders[self.folders[1]]["status"], "import_failed")
        for f in self.folders:
            self.assertTrue((Path(f) / "01.flac").exists())


class SymlinkEdgeTests(_Batch):
    """N4 re-verify: no symlinked audio may reach Beets Stage 1."""

    def _link(self, link: Path, target: Path, **kw):
        try:
            link.symlink_to(target, **kw)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks here")

    def _beets_folders(self):
        return [c.args[0] for c in self.stubs["reimport_source"].call_args_list]

    def test_symlink_to_audio_outside_the_folder_is_refused(self):
        outside = self.tmp.parent / f"qa394_out_{id(self)}.flac"
        outside.write_bytes(b"x")
        self.addCleanup(outside.unlink)
        self._link(Path(self.folders[0]) / "02.flac", outside)
        _, folders = self.run_batch()
        self.assertEqual(folders[self.folders[0]]["failure_reason"], "symlinked_audio")
        self.assertNotIn(self.folders[0], self._beets_folders())

    def test_symlinked_directory_of_audio_is_refused(self):
        # Beets' sorted_walk follows a symlinked directory, so its audio
        # would be imported (and moved) without ever being checked here.
        disc = self.tmp / "elsewhere_disc"
        disc.mkdir()
        (disc / "02.flac").write_bytes(b"x")
        self._link(Path(self.folders[0]) / "CD2", disc, target_is_directory=True)
        _, folders = self.run_batch()
        self.assertNotIn(self.folders[0], self._beets_folders(), self.log)
        self.assertEqual(folders[self.folders[0]]["failure_reason"], "symlinked_audio")

    def test_symlink_to_non_audio_also_blocks(self):
        # Security review of #394 (B1): Beets identifies audio by content, so
        # any linked file fails closed, a linked cover.jpg included.
        art = self.tmp / "cover_src.jpg"
        art.write_bytes(b"x")
        self._link(Path(self.folders[0]) / "cover.jpg", art)
        _, folders = self.run_batch()
        self.assertEqual(folders[self.folders[0]]["failure_reason"], "symlinked_audio")
        self.assertNotIn(self.folders[0], self._beets_folders())

    def test_broken_audio_symlink_is_refused(self):
        self._link(Path(self.folders[0]) / "03.flac", self.tmp / "missing.flac")
        _, folders = self.run_batch()
        self.assertEqual(folders[self.folders[0]]["failure_reason"], "symlinked_audio")
        self.assertNotIn(self.folders[0], self._beets_folders())


class SymlinkRootProbeTests(_Batch):
    """Re-verify at e2f53c6: probes (a) and (b) from the coordinator."""

    def _beets_folders(self):
        return [c.args[0] for c in self.stubs["reimport_source"].call_args_list]

    def _unreadable(self, path: Path):
        if os.geteuid() == 0:
            self.skipTest("root reads everything")
        path.chmod(0)
        self.addCleanup(path.chmod, 0o755)

    def test_folder_swapped_for_a_symlink_after_discovery(self):
        # (a) Discovery never yields a symlinked folder; this is the window
        # between discovery and Stage 1 (or a stored folder path on recovery).
        real = self.tmp / "real_album"
        Path(self.folders[0]).rename(real)
        try:
            Path(self.folders[0]).symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks here")
        _, folders = self.run_batch()
        self.assertNotIn(self.folders[0], self._beets_folders(), self.log)

    @unittest.skipUnless(hasattr(os, "geteuid"), "POSIX permissions")
    def test_unreadable_folder_root(self):
        # (b) Web Manager cannot list the folder; Beets (another uid) may.
        self._unreadable(Path(self.folders[0]))
        _, folders = self.run_batch()
        self.assertNotIn(self.folders[0], self._beets_folders(), self.log)

    @unittest.skipUnless(hasattr(os, "geteuid"), "POSIX permissions")
    def test_symlink_inside_an_unreadable_subfolder(self):
        sub = Path(self.folders[0]) / "CD2"
        sub.mkdir()
        disc = self.tmp / "elsewhere_disc2"
        disc.mkdir()
        (disc / "02.flac").write_bytes(b"x")
        try:
            (sub / "linked").symlink_to(disc, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks here")
        self._unreadable(sub)
        _, folders = self.run_batch()
        self.assertNotIn(self.folders[0], self._beets_folders(), self.log)


class LegacyAiRunningTests(RecoveryTests):
    """N6 re-verify: a pre-phase-B "ai_running" folder is never stranded."""

    def test_stale_batch_times_out_ai_running_via_reconcile(self):
        from backend.import_reconciliation_service import _ai_batch_reconcile_state
        state = self._state_with("ai_running", ai_suggest_started_at=1.0)
        state.update({"status": "running", "heartbeat_at": 1.0})
        with mock.patch("backend.import_reconciliation_service._ai_batch_worker_registered", return_value=False), \
                mock.patch("backend.import_reconciliation_service._ai_batch_commit"):
            out = _ai_batch_reconcile_state(state)
        self.assertEqual({f["status"] for f in out["folder_states"].values()}, {"timed_out"})

    def test_recover_with_fresh_heartbeat_reruns_ai_running_deterministically(self):
        state = self._state_with("ai_running", ai_suggest_started_at=1.0)
        with mock.patch.object(ai, "_ai_import_folder", side_effect=AssertionError("AI path")):
            _, folders = self._recover(state)
        self.assertEqual({f["status"] for f in folders.values()}, {"imported"}, self.log)
        self.assertEqual(self.stubs["reimport_source"].call_count, 2)


class SkipRaceTests(_Batch):
    def test_skip_pressed_during_a_skipped_stage1_still_skips_that_folder_only(self):
        first = self.folders[0]

        def reimport(folder, opts, timeout):
            if folder == first:
                ai._ai_batch_control(self.batch_id)["skip_current"] = True
                return skipped(folder)
            return imported([7])

        self.stubs["reimport_source"].side_effect = reimport
        _, folders = self.run_batch()
        self.assertEqual(folders[first]["failure_reason"], "skipped by user")
        self.assertEqual(folders[self.folders[1]]["status"], "imported")


class PartialReviewRowTests(_Batch):
    def test_partial_import_creates_exactly_one_row_including_final_tally(self):
        disc2 = str(Path(self.folders[0]) / "CD2")
        self.reimport[self.folders[0]] = {"ok": True, "albums_imported": 1, "imported_album_ids": [7],
                                          "skipped": [{"path": disc2, "reason": "duplicate"}],
                                          "not_matched": [disc2], "not_matched_known": True}
        _, folders = self.run_batch()
        rows = [c for c in self.queued.call_args_list if c.args[0] == self.folders[0]]
        self.assertEqual(len(rows), 1)  # _ai_batch_process_decisions adds none
        ev = rows[0].kwargs["evidence"]
        self.assertEqual((ev["origin_step"], ev["beets_skip_reason"]), ("beets_partial", "duplicate"))
        self.assertEqual(rows[0].args[1]["mb_releasegroupid"], "")  # no identity invented


class Stage2bCancelTests(_Batch):
    def test_cancel_after_pinned_beets_import_goes_to_review(self):
        # Pre-existing edge (implementer-reported): cancel lands inside
        # _ai_import_folder after Beets imported. Pin what happens today.
        self.reimport[self.folders[0]] = skipped(self.folders[0])
        self.ident = identity(True)

        def apply(op):
            self.cancel.set()
            return {"ok": True, "album_id": 9, "mb_releasegroupid": RG}

        self.stubs["apply"].side_effect = apply
        out, folders = self.run_batch()
        a = folders[self.folders[0]]
        self.assertEqual(a["status"], "review_created")
        self.assertEqual(self.history.call_count, 0)  # album 9 is in Beets but not in History
        self.assertNotIn("kept_album_ids", self.queued.call_args.args[1])
        self.assertEqual(out["status"], "canceled")


if __name__ == "__main__":
    unittest.main()
