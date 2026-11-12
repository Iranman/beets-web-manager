"""#556: one reservation covers a recovery's ``rec-<hex>`` staged folder for the
whole time a job restages or reads it -- the acquisition retry (check, restage,
own import), a folder import (Review / advancer hand-off) and the advancer's
re-verification. A busy folder is refused at once; nothing waits forever."""
import os
import shutil
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

acq = imp = rc = recovery_service = resource_locks = job_engine = None
RGID = "37cb4a72-3333-4444-5555-666666666666"
PREFIX = "rec-0123456789abcdef"


def setUpModule():
    global acq, imp, rc, recovery_service, resource_locks, job_engine
    import app  # noqa: F401
    import backend.acquisition_service as acq
    import backend.import_service as imp
    import backend.recovery_carrier as rc
    import backend.recovery_service as recovery_service
    import backend.resource_locks as resource_locks
    import job_engine


class _Reached(Exception):
    pass


class _Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        resource_locks.set_locks(resource_locks.ResourceLocks(os.path.join(self.tmp, "locks")))
        self.addCleanup(resource_locks.set_locks, None)
        self.folder = os.path.join(self.tmp, "_beets_missing_import", f"{PREFIX}-A - B")
        self.store = job_engine.JobStore()

    def _retry(self):
        req = SimpleNamespace(recovery_carrier_id="", forced_albumartist="", existing_album_id=42,
                              replace_existing_item_ids=[], mb_albumid="rel-1", track_count=1,
                              wanted_tracks=[{"track": 1}], recovery_stage_prefix=PREFIX)
        run = acq._AlbumDownloadRun(req, [], None)
        run._recovery_resume_short_circuit = lambda: None
        return run

    def _folder_import(self, folder, do):
        return imp._folder_import_in_slot(do, [], None, folder_path=folder, selected_source_files=[],
                                          auto_import_idempotency_key="", review_item_id="")


class RestageVersusImport(_Env):
    def test_import_started_after_the_retrys_check_is_refused(self):
        # The retry has passed its running-import check and is on its way to
        # restaging; a Review/advancer import of the same folder starts now.
        restaging, finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)
        outcome = {}

        def _restage():
            restaging.set()
            finish.wait(10)
            raise _Reached

        retry = self._retry()
        retry._resolve_release = _restage

        def _run_retry():
            try:
                retry.execute()
            except BaseException as ex:  # noqa: BLE001 -- recorded for the assert
                outcome["retry"] = ex

        with mock.patch.object(acq, "jobs", self.store):
            t = threading.Thread(target=_run_retry)
            t.start()
            self.assertTrue(restaging.wait(10))
            imported = []
            with self.assertRaises(recovery_service.StagingFolderBusy) as caught:
                self._folder_import(self.folder, lambda log, c: imported.append(1))
            self.assertEqual(imported, [])  # the import never read the folder
            self.assertIn(PREFIX, str(caught.exception))
            finish.set()
            t.join(10)
        self.assertIsInstance(outcome.get("retry"), _Reached)
        # Released with the retry: the import now runs.
        self.assertEqual(self._folder_import(self.folder, lambda log, c: "ok"), "ok")

    def test_retry_is_refused_while_an_import_holds_the_folder(self):
        reading, finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)

        def _do(log, cancel):
            reading.set()
            finish.wait(10)
            return "ok"

        t = threading.Thread(target=self._folder_import, args=(self.folder, _do))
        t.start()
        self.assertTrue(reading.wait(10))
        retry = self._retry()
        retry._resolve_release = mock.Mock(side_effect=_Reached)
        with mock.patch.object(acq, "jobs", self.store):
            with self.assertRaises(recovery_service.StagingFolderBusy):
                retry.execute()
        retry._resolve_release.assert_not_called()  # never reached the restage
        finish.set()
        t.join(10)
        with mock.patch.object(acq, "jobs", self.store):
            with self.assertRaises(_Reached):
                retry.execute()

    def test_busy_is_refused_at_once_not_waited_on(self):
        key = f"workflow:staging-{PREFIX}"
        resource_locks.locks().acquire([key], "other-owner", timeout=0)
        self.addCleanup(resource_locks.locks().release, [key], "other-owner")
        started = time.monotonic()
        with self.assertRaises(recovery_service.StagingFolderBusy):
            with recovery_service.staging_reservation(self.folder):
                self.fail("entered a busy reservation")
        self.assertLess(time.monotonic() - started, 5)

    def test_non_recovery_folder_is_not_reserved(self):
        with recovery_service.staging_reservation(self.folder):
            plain = os.path.join(self.tmp, "Artist - Album")
            with mock.patch.object(rc, "load_for_folder", return_value=None):
                self.assertEqual(self._folder_import(plain, lambda log, c: "ok"), "ok")

    def test_carrier_bound_source_folder_maps_to_its_prefix(self):
        source = os.path.join(self.tmp, "downloads", "Artist - Album")
        carrier = {"recovery_identity_key": "0123456789ABCDEF" + "0" * 48}
        with mock.patch.object(rc, "load_for_folder", return_value=carrier):
            self.assertEqual(recovery_service.staging_prefix_for(source), PREFIX)


class AdvancerDefersBusyFolder(_Env):
    def test_advancer_does_not_reverify_a_folder_a_retry_holds(self):
        carrier = {"carrier_id": "c1", "state": "downloading",
                   "recovery_identity_key": "0123456789abcdef" + "0" * 48}
        with mock.patch.object(rc, "list_carriers_by_state", return_value=[carrier]), \
                mock.patch.object(rc, "get_carrier", return_value=carrier), \
                mock.patch.object(recovery_service, "_reevaluate_carrier") as reevaluate, \
                mock.patch.object(recovery_service, "_hand_off_to_insertion") as hand_off:
            with recovery_service.staging_reservation(PREFIX):
                summary = recovery_service.advance_stuck_carriers(limit=5, log=[])
        reevaluate.assert_not_called()
        hand_off.assert_not_called()
        self.assertEqual(summary["carriers"], [{"carrier_id": "c1", "outcome": "skipped_staging_busy"}])


if __name__ == "__main__":
    unittest.main()
