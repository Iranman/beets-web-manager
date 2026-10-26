"""Missing-track recovery (ARCH-030 Phase 2) -- HOOK(library-transaction).

The verified single-track INSERTION/MERGE into an existing album is driven by
a durable recovery carrier (backend.recovery_carrier) but reuses the existing,
already transaction-backed merge in backend/import_service.py. These tests
cover the carrier wiring the HOOK adds AROUND that merge:

* a carrier-less folder leaves the merge exactly as it was (strict no-op);
* the carrier is claimed into 'importing' before any change, and a second
  job (or a crash that left it 'importing') is refused -- no duplicate import;
* fail-closed preconditions: the target album must still exist and the
  expected (missing) track must still be absent, else the folder is routed to
  Review WITH its identity and nothing is imported/deleted;
* success drives the carrier to 'imported' + checkpoint 'IMPORTED'; a merge
  that moved nothing goes to 'review_required'; a merge that raised leaves a
  recoverable 'failed' state with the imported row untouched.

The merge itself goes through composite_workflows.{plan,apply}_existing_album_
reconcile -> the transaction engine (preview/apply/audit/recovery + rollback),
so the recovery inherits that durable rollback rather than adding a new path.
"""
import os
import unittest
from unittest import mock

import backend.import_service as isvc
import backend.recovery_carrier as rc
from tests.test_sec002_wave20_existing_album_reconcile import (
    Wave20FixtureBase, RG_A, RG_B, REL_A, REC_1, REC_2, REC_3,
)

cw = isvc.composite_workflows


class RecoveryCarrierMergeTests(Wave20FixtureBase):
    def setUp(self):
        super().setUp()
        rc_dir = self.tmp_path / "carriers"
        env = mock.patch.dict(os.environ, {"RECOVERY_CARRIER_STATE_DIR": str(rc_dir)})
        env.start()
        rc._store_registry.clear()
        self.addCleanup(rc._store_registry.clear)
        self.addCleanup(env.stop)
        self.staged = str(self.staging_root)

    # --- fixtures ---------------------------------------------------------
    def _two_albums(self, imported_rg=RG_A):
        """Existing album 1 holds track (1,1); imported album 2 holds the
        verified missing track (1,2) in staging."""
        self._create_album(1, "Existing Album", rg_id=RG_A, rel_id=REL_A)
        self._create_album(2, "Imported Temp Album", rg_id=imported_rg, rel_id=REL_A)
        self._create_item(10, 1, "Track 1", 1, 1, "t1_existing.wav", rec_id=REC_1, rg_id=RG_A)
        self._create_item(20, 2, "Track 2", 1, 2, "t2_imported.wav", staging=True,
                          rec_id=REC_2, rg_id=imported_rg)

    def _carrier(self, *, existing_album_id=1, expected=None, state="verifying"):
        return rc.create_carrier(
            existing_album_id=existing_album_id, mb_releasegroupid=RG_A,
            expected_tracks=expected if expected is not None
            else [{"recording_id": REC_2, "track": 2, "disc": 1, "title": "Track 2"}],
            staged_folder_path=self.staged, state=state)

    def _reconcile_ok(self):
        return (
            mock.patch.object(cw, "plan_existing_album_reconcile",
                              return_value={"ok": True, "operation_id": "op-rec"}),
            mock.patch.object(cw, "apply_existing_album_reconcile",
                              return_value={"ok": True}),
        )

    # --- no-op off the recovery path -------------------------------------
    def test_carrier_less_folder_is_a_strict_no_op(self):
        """No carrier bound to the folder: behaviour is the ordinary merge."""
        self._two_albums()
        plan, apply = self._reconcile_ok()
        with plan, apply:
            res = isvc._merge_imported_album_into_existing(2, 1, self.staged, [], mb_albumid="")
        self.assertEqual(res, 1)  # moved onto the existing album as before
        self.assertIsNone(rc.load_for_folder(self.staged))  # no carrier created

    def test_carrier_for_a_different_album_does_not_engage(self):
        self._two_albums()
        self._carrier(existing_album_id=999)  # names a different album
        plan, apply = self._reconcile_ok()
        with plan, apply:
            res = isvc._merge_imported_album_into_existing(2, 1, self.staged, [], mb_albumid="")
        self.assertEqual(res, 1)
        # State untouched: the carrier never claimed this merge.
        self.assertEqual(rc.load_for_folder(self.staged)["state"], "verifying")

    # --- happy path -------------------------------------------------------
    def test_verified_track_insertion_drives_carrier_to_imported(self):
        self._two_albums()
        cid = self._carrier()["carrier_id"]
        plan, apply = self._reconcile_ok()
        with plan as plan_m, apply as apply_m:
            res = isvc._merge_imported_album_into_existing(2, 1, self.staged, [], mb_albumid="")
        self.assertEqual(res, 1)
        plan_m.assert_called_once()
        apply_m.assert_called_once_with("op-rec")
        rec = rc.get_carrier(cid)
        self.assertEqual(rec["state"], "imported")
        self.assertEqual(rec["checkpoint"], "IMPORTED")

    # --- partial vs full delivery (QA-2-r / Finding F-2) -----------------
    def _multi_track_albums(self, imported_tracks):
        """Existing album 1 holds (1,1); imported album 2 holds the given
        (track#, recording_id) tuples as staged items."""
        self._create_album(1, "Existing Album", rg_id=RG_A, rel_id=REL_A)
        self._create_album(2, "Imported Temp Album", rg_id=RG_A, rel_id=REL_A)
        self._create_item(10, 1, "Track 1", 1, 1, "t1_existing.wav", rec_id=REC_1, rg_id=RG_A)
        for tno, rec_id in imported_tracks:
            self._create_item(10 + tno, 2, f"Track {tno}", 1, tno,
                              f"t{tno}_imported.wav", staging=True, rec_id=rec_id, rg_id=RG_A)

    def test_partial_delivery_is_not_marked_imported(self):
        """Reproduce F-2: the recovery wants tracks 2 AND 3 but only track 2
        was actually delivered. The merge moves track 2, but the carrier must
        NOT reach 'imported'/IMPORTED -- otherwise a resubmission of the
        undelivered track 3 is short-circuited as already-imported and
        stranded. It stays recoverable (review_required)."""
        self._multi_track_albums([(2, REC_2)])  # track 3 never arrived
        cid = self._carrier(expected=[
            {"recording_id": REC_2, "track": 2, "disc": 1, "title": "Track 2"},
            {"recording_id": REC_3, "track": 3, "disc": 1, "title": "Track 3"},
        ])["carrier_id"]
        plan, apply = self._reconcile_ok()
        with plan, apply:
            res = isvc._merge_imported_album_into_existing(2, 1, self.staged, [], mb_albumid="")
        self.assertEqual(res, 1)  # track 2 was moved onto the existing album
        rec = rc.get_carrier(cid)
        self.assertEqual(rec["state"], "review_required")
        # The acquisition resume short-circuit fires ONLY on IMPORTED/PLEX_SYNCED
        # (acquisition_service ~L295); a partial delivery must not reach it.
        self.assertNotEqual(rec["checkpoint"], "IMPORTED")

    def test_full_multi_track_delivery_reaches_imported(self):
        """Control: when every wanted track (2 AND 3) is actually delivered,
        the carrier still reaches 'imported' + checkpoint IMPORTED."""
        self._multi_track_albums([(2, REC_2), (3, REC_3)])
        cid = self._carrier(expected=[
            {"recording_id": REC_2, "track": 2, "disc": 1, "title": "Track 2"},
            {"recording_id": REC_3, "track": 3, "disc": 1, "title": "Track 3"},
        ])["carrier_id"]
        plan, apply = self._reconcile_ok()
        with plan, apply:
            res = isvc._merge_imported_album_into_existing(2, 1, self.staged, [], mb_albumid="")
        self.assertEqual(res, 1)
        rec = rc.get_carrier(cid)
        self.assertEqual(rec["state"], "imported")
        self.assertEqual(rec["checkpoint"], "IMPORTED")

    # --- concurrency / crash recovery ------------------------------------
    def test_concurrent_recovery_in_importing_is_refused(self):
        """A carrier already 'importing' (a second job, or a crash that left
        it mid-import) is refused: no second import of the same track."""
        self._two_albums()
        self._carrier(state="importing")
        plan, apply = self._reconcile_ok()
        with plan as plan_m, apply:
            with self.assertRaises(isvc._ExistingAlbumMergeFailed) as cm:
                isvc._merge_imported_album_into_existing(2, 1, self.staged, [], mb_albumid="")
        self.assertIn("concurrent recovery job", str(cm.exception))
        plan_m.assert_not_called()  # the library was never touched

    def test_already_imported_carrier_is_refused(self):
        self._two_albums()
        self._carrier(state="imported")
        with self.assertRaises(isvc._ExistingAlbumMergeFailed):
            isvc._merge_imported_album_into_existing(2, 1, self.staged, [], mb_albumid="")

    def test_claim_loses_the_cas_race(self):
        """Two jobs read the same revision; the first wins 'importing', the
        second's compare-and-swap loses and is refused."""
        rec = self._carrier()
        rc.advance_state(rec["carrier_id"], "importing")  # the winner moved first
        with self.assertRaises(isvc._ExistingAlbumMergeFailed):
            isvc._recovery_claim_importing(rec, [])  # stale revision -> conflict

    # --- fail-closed preconditions ---------------------------------------
    def test_expected_track_already_present_routes_to_review(self):
        """The slot is already filled on the existing album -> the track is
        not missing -> Review, never a duplicate import."""
        self._two_albums()
        # Existing album already has (1,2): the 'missing' track is present.
        self._create_item(11, 1, "Track 2 already here", 1, 2, "t2_existing.wav",
                          rec_id=REC_2, rg_id=RG_A)
        cid = self._carrier()["carrier_id"]
        plan, apply = self._reconcile_ok()
        with plan as plan_m, apply:
            with self.assertRaises(isvc._ExistingAlbumMergeFailed) as cm:
                isvc._merge_imported_album_into_existing(2, 1, self.staged, [], mb_albumid="")
        self.assertIn("already on album_id 1", str(cm.exception))
        plan_m.assert_not_called()
        self.assertEqual(rc.get_carrier(cid)["state"], "review_required")

    def test_vanished_target_album_routes_to_review(self):
        # Only the imported album exists; the recovery target is gone.
        self._create_album(2, "Imported Temp Album", rg_id=RG_A, rel_id=REL_A)
        self._create_item(20, 2, "Track 2", 1, 2, "t2_imported.wav", staging=True,
                          rec_id=REC_2, rg_id=RG_A)
        cid = self._carrier(existing_album_id=1)["carrier_id"]
        with self.assertRaises(isvc._ExistingAlbumMergeFailed) as cm:
            isvc._merge_imported_album_into_existing(2, 1, self.staged, [], mb_albumid="")
        self.assertIn("no longer exists", str(cm.exception))
        self.assertEqual(rc.get_carrier(cid)["state"], "review_required")

    # --- failure leaves a recoverable state ------------------------------
    def test_reconcile_apply_failure_marks_carrier_failed(self):
        """The reconcile Apply fails: the merge raises, the imported row is
        kept, and the carrier is left 'failed' (recoverable) -- not stuck
        'importing' and never reported 'imported'."""
        self._two_albums()
        cid = self._carrier()["carrier_id"]
        with mock.patch.object(cw, "plan_existing_album_reconcile",
                               return_value={"ok": True, "operation_id": "op-rec"}), \
             mock.patch.object(cw, "apply_existing_album_reconcile",
                               return_value={"ok": False, "error": "simulated engine failure"}):
            with self.assertRaises(isvc._ExistingAlbumMergeFailed):
                isvc._merge_imported_album_into_existing(2, 1, self.staged, [], mb_albumid="")
        self.assertEqual(rc.get_carrier(cid)["state"], "failed")

    def test_different_identity_moves_nothing_and_routes_to_review(self):
        """The imported album is a different Release Group: the merge moves
        nothing and the carrier goes to 'review_required', not 'imported'."""
        self._two_albums(imported_rg=RG_B)
        cid = self._carrier()["carrier_id"]
        plan, apply = self._reconcile_ok()
        with plan as plan_m, apply:
            res = isvc._merge_imported_album_into_existing(2, 1, self.staged, [], mb_albumid="")
        self.assertEqual(res, 2)  # kept as a separate imported album row
        plan_m.assert_not_called()
        self.assertEqual(rc.get_carrier(cid)["state"], "review_required")


class RecoveryRestageRebindTests(Wave20FixtureBase):
    """F5/F6: a recovery carrier is bound to the ORIGINAL review folder; when
    the still-missing subset is restaged to a NEW path before the merge's
    load_for_folder lookup, the merge must still run under carrier ownership."""

    def setUp(self):
        super().setUp()
        rc_dir = self.tmp_path / "carriers"
        env = mock.patch.dict(os.environ, {"RECOVERY_CARRIER_STATE_DIR": str(rc_dir)})
        env.start()
        rc._store_registry.clear()
        self.addCleanup(rc._store_registry.clear)
        self.addCleanup(env.stop)
        self.original = str(self.tmp_path / "review_folder")
        self.restaged = str(self.staging_root)  # where _stage_selected_audio_files put it

    def _two_albums(self):
        self._create_album(1, "Existing Album", rg_id=RG_A, rel_id=REL_A)
        self._create_album(2, "Imported Temp Album", rg_id=RG_A, rel_id=REL_A)
        self._create_item(10, 1, "Track 1", 1, 1, "t1_existing.wav", rec_id=REC_1, rg_id=RG_A)
        self._create_item(20, 2, "Track 2", 1, 2, "t2_imported.wav", staging=True,
                          rec_id=REC_2, rg_id=RG_A)

    def _carrier_on_original(self):
        return rc.create_carrier(
            existing_album_id=1, mb_releasegroupid=RG_A,
            expected_tracks=[{"recording_id": REC_2, "track": 2, "disc": 1, "title": "Track 2"}],
            staged_folder_path=self.original, state="verifying")["carrier_id"]

    def _reconcile_ok(self):
        return (
            mock.patch.object(cw, "plan_existing_album_reconcile",
                              return_value={"ok": True, "operation_id": "op-rec"}),
            mock.patch.object(cw, "apply_existing_album_reconcile",
                              return_value={"ok": True}),
        )

    def test_restage_without_rebind_runs_carrier_less(self):
        """Reproduce the gap: the merge keyed on the restaged path finds no
        carrier (it is bound to the original folder) -> ordinary merge, the
        carrier is never claimed/driven."""
        self._two_albums()
        cid = self._carrier_on_original()
        plan, apply = self._reconcile_ok()
        with plan, apply:
            res = isvc._merge_imported_album_into_existing(2, 1, self.restaged, [], mb_albumid="")
        self.assertEqual(res, 1)
        # Carrier untouched: still 'verifying', never reached IMPORTED.
        self.assertEqual(rc.get_carrier(cid)["state"], "verifying")

    def test_rebind_then_merge_engages_carrier(self):
        """Fix: rebinding the carrier to the restaged path lets the merge claim
        and drive it to imported+IMPORTED under carrier ownership/audit."""
        self._two_albums()
        cid = self._carrier_on_original()
        log = []
        isvc._recovery_rebind_restaged(self.original, self.restaged, 1, log)
        self.assertIsNotNone(rc.load_for_folder(self.restaged))
        plan, apply = self._reconcile_ok()
        with plan, apply:
            res = isvc._merge_imported_album_into_existing(2, 1, self.restaged, log, mb_albumid="")
        self.assertEqual(res, 1)
        rec = rc.get_carrier(cid)
        self.assertEqual(rec["state"], "imported")
        self.assertEqual(rec["checkpoint"], "IMPORTED")

    def test_rebind_ignores_a_carrier_for_a_different_album(self):
        self._two_albums()
        rc.create_carrier(existing_album_id=999, mb_releasegroupid=RG_A,
                          staged_folder_path=self.original, state="verifying")
        log = []
        isvc._recovery_rebind_restaged(self.original, self.restaged, 1, log)
        self.assertIsNone(rc.load_for_folder(self.restaged))  # not rebound


class RecoveryMergeHelperUnitTests(unittest.TestCase):
    """Pure helpers, no store/engine."""

    def test_slot_present_matches_recording_id(self):
        rows = [{"disc": 1, "track": 7, "mb_trackid": "REC-X"}]
        self.assertTrue(isvc._recovery_slot_present(rows, {"recording_id": "rec-x"}))

    def test_slot_present_matches_disc_track_slot(self):
        rows = [{"disc": 2, "track": 3, "mb_trackid": ""}]
        self.assertTrue(isvc._recovery_slot_present(rows, {"disc": 2, "track": 3}))

    def test_slot_absent_when_neither_matches(self):
        rows = [{"disc": 1, "track": 1, "mb_trackid": "rec-1"}]
        self.assertFalse(isvc._recovery_slot_present(rows, {"disc": 1, "track": 2, "recording_id": "rec-2"}))

    def test_mark_failed_is_a_no_op_without_a_carrier(self):
        log = []
        isvc._recovery_mark_failed(None, log)  # must not raise
        self.assertEqual(log, [])


if __name__ == "__main__":
    unittest.main()
