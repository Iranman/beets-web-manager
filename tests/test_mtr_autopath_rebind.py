"""Missing-track recovery AUTO path: the carrier is rebound across a
subset-narrowing restage (ARCH-030, QA round-2 Finding 1).

End-to-end integration over the real ``start_reimport_disk`` auto progression
(acquisition binds the carrier -> the download job restages the still-missing
subset -> merge -> Plex-sync leg), driven through the existing ``_JobHarness``
that exercises the real job body.

The defect: ``start_reimport_disk`` restages the still-missing subset to a NEW
path and reassigns ``aldir`` (#427 narrowing branch), but the merge and the
Plex-sync leg look the carrier up by the reassigned ``aldir``. Without a
``_recovery_rebind_restaged`` call between the restage and those lookups the
carrier -- still bound to the pre-restage folder -- is orphaned: the merge runs
carrier-less and the carrier never advances past its staged state, so the
Download-Missing UI stalls at "Verifying".

These tests use the REAL merge (``_merge_imported_album_into_existing``) with a
mocked reconcile plan/apply so the carrier wiring (claim -> precheck ->
outcome) runs exactly as in production; only the Beets/engine boundary is
faked.  ``test_auto_path_without_rebind_orphans_the_carrier`` pins the pre-fix
mechanism by calling the merge on the restaged path directly (carrier-less),
and ``test_auto_path_rebinds_and_drives_the_carrier_to_imported`` is the
fail-before/pass-after end-to-end proof.
"""
import os
import shutil
import tempfile
from unittest import mock

import backend.import_reconciliation as ir
import backend.import_service as isvc
import backend.recovery_carrier as rc
from tests.test_fill_merge_fail_closed import _JobHarness, EXISTING, IMPORTED, REL, RG, WANTED

cw = isvc.composite_workflows
EXPECTED = [{"recording_id": "rec-2", "track": 2, "disc": 1, "title": "Two"}]


class AutoPathRebindTests(_JobHarness):
    def setUp(self):
        super().setUp()
        self.original_dir = str(isvc.DOWNLOADS_ROOT) + "/Artist/Album"  # bound at acquisition
        self.staged_dir = self.original_dir + "/_still_missing_subset"  # restage narrows the subset

        rc_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, rc_dir, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"RECOVERY_CARRIER_STATE_DIR": rc_dir})
        env.start()
        rc._store_registry.clear()
        self.addCleanup(rc._store_registry.clear)
        self.addCleanup(env.stop)

        # The second source scan narrows the subset; staging it yields a NEW
        # path (NOT the #427 in-place branch), reassigning aldir.
        scan = {"ok": True, "useful_files": [self.staged_dir + "/02 Two.flac"],
                "duplicate_files": [], "unknown_files": [], "missing_count": 1,
                "wanted_tracks": WANTED}
        plan = ir.ReconciliationPlan(
            album=ir.AlbumIdentity(same_album=True, release_group_id=RG,
                                   reason="test fixture: proven same album"),
            move_ids=[92])

        def apply_recon(op_id):
            # The engine move the merge delegated: item 92 lands on EXISTING so
            # the job's read-back verifies the fill.
            self.items[EXISTING] += self.items.pop(IMPORTED, [])
            return {"ok": True}

        for target, name, kw in (
            (isvc, "_source_audio_missing_track_scan", {"return_value": scan}),
            (isvc, "_stage_selected_audio_files", {"return_value": self.staged_dir}),
            (ir, "plan_reconciliation", {"return_value": plan}),
            (cw, "plan_existing_album_reconcile", {"return_value": {"ok": True, "operation_id": "op-rec"}}),
            (cw, "apply_existing_album_reconcile", {"side_effect": apply_recon}),
            (isvc, "_plex_configured", {"return_value": True}),
            (isvc, "PLEX_SYNC_AUTO_REFRESH", {"new": True}),
        ):
            patcher = mock.patch.object(target, name, **kw)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _make_carrier(self, folder):
        return rc.create_carrier(
            existing_album_id=EXISTING, mb_releasegroupid=RG, release_id=REL,
            expected_tracks=EXPECTED, staged_folder_path=folder, state="verifying")["carrier_id"]

    def test_auto_path_without_rebind_orphans_the_carrier(self):
        """Pre-fix mechanism: the merge keyed on the restaged path finds no
        carrier (it is still bound to the original folder) and runs
        carrier-less, leaving the carrier stuck at 'verifying'."""
        cid = self._make_carrier(self.original_dir)
        with mock.patch.object(ir, "plan_reconciliation", return_value=ir.ReconciliationPlan(
                album=ir.AlbumIdentity(same_album=True, release_group_id=RG, reason="same"),
                move_ids=[92])):
            res = isvc._merge_imported_album_into_existing(
                IMPORTED, EXISTING, self.staged_dir, [], mb_albumid="")
        self.assertEqual(res, EXISTING)
        self.assertEqual(rc.get_carrier(cid)["state"], "verifying")  # orphaned

    def test_auto_path_rebinds_and_drives_the_carrier_to_imported(self):
        """Fail-before/pass-after: the real auto progression rebinds the
        carrier across the restage, so the merge runs under carrier ownership
        and the Plex-sync leg drives it to imported+PLEX_SYNCED."""
        cid = self._make_carrier(self.original_dir)
        res = self._run()  # real merge (merge=None)
        self.assertEqual(res["status"], "completed", res)
        self.assertEqual(res["result"]["album_ids"], [EXISTING])

        # The carrier was rebound to the restaged path and is findable there.
        rebound = rc.load_for_folder(self.staged_dir)
        self.assertIsNotNone(rebound, "carrier was not rebound to the restaged path")
        self.assertEqual(rebound["carrier_id"], cid)
        self.assertIsNone(rc.load_for_folder(self.original_dir))

        # Merge ran under carrier ownership and the Plex-sync leg advanced it.
        rec = rc.get_carrier(cid)
        self.assertEqual(rec["state"], "imported")
        self.assertEqual(rec["checkpoint"], "PLEX_SYNCED")
        self.assertTrue(any("rebound to the restaged path" in line for line in res["log"]), res["log"])
