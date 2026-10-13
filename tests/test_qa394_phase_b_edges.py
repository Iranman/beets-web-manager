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
