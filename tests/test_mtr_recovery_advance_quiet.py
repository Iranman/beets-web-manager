"""#762: quiet no-change recovery sweeps + progress-aware re-check.

Owner-approved behavior under test:
  A) A no-change scheduler tick updates the rolling status sidecar but records
     NO user-facing job; a tick that hands off / newly stalls a carrier DOES.
  B) A carrier whose staged inputs are unchanged since its last full re-check is
     SKIPPED (no ~35-40s preflight) until the re-check floor elapses; new files
     or a revision change invalidate the checkpoint and force an immediate
     re-verify; checkpoints survive a restart.
  C/D) Single-flight still refuses a concurrent run; the read-only status
     endpoint returns the structured counts.

Isolated fixtures only: a per-test carrier state dir AND advance cache dir; no
production music/library is touched and no real sweep job thread is started
(start_python is mocked / driven synchronously).
"""

import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

import app as app_module  # noqa: F401 -- loads the module family + registers routes
import backend.job_contract as job_contract
import backend.recovery_carrier as rc
import backend.recovery_service as recovery_service
import backend.import_service as import_service
import backend.matching_service as matching_service
from backend.app_runtime import jobs
from job_engine import DuplicateJobError
from tests._app_family import patch_app_family

RGID = "37cb4a72-1111-2222-3333-444444444444"
RELEASE = "55555555-6666-7777-8888-999999999999"


class _Mixin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._prev = {k: os.environ.get(k) for k in
                      ("RECOVERY_CARRIER_STATE_DIR", "RECOVERY_ADVANCE_CACHE_DIR")}
        os.environ["RECOVERY_CARRIER_STATE_DIR"] = self.tmp
        os.environ["RECOVERY_ADVANCE_CACHE_DIR"] = self.tmp
        rc._store_registry.clear()

    def tearDown(self):
        rc._store_registry.clear()
        for key, prev in self._prev.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _staged_dir(self, name, tracks=1):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        for i in range(tracks):
            with open(os.path.join(d, f"{i + 1:02d} track.flac"), "wb") as fh:
                fh.write(b"\x00" * 16)
        return d

    def _carrier(self, name, state, album_id=42):
        folder = self._staged_dir(name)
        cid = rc.create_carrier(
            existing_album_id=album_id, mb_releasegroupid=RGID, release_id=RELEASE,
            wanted_tracks=[{"track": 1, "recording_id": "r1"}],
            expected_tracks=[{"recording_id": "r1", "track": 1, "disc": 1, "title": "Intro"}],
            staged_folder_path=folder, state=state)["carrier_id"]
        return cid, folder

    @staticmethod
    def _failing_preflight():
        calls = []

        def _pf(folder, release, album_id, *, log=None, **kw):
            calls.append(folder)
            return {"ok": False, "error": "no fingerprint evidence for these tracks",
                    "matching_decision": {"action_allowed": False, "identity_proof": "release_group_id"}}
        return _pf, calls

    @staticmethod
    def _passing_preflight():
        calls = []

        def _pf(folder, release, album_id, *, log=None, **kw):
            calls.append(folder)
            return {"ok": True, "matching_decision": {
                "action_allowed": True, "identity_proof": "deterministic_metadata_slot"}}
        return _pf, calls

    def _tick(self, preflight, insert=None):
        """Drive one scheduler tick; returns the (mocked) jobs.start_python so the
        caller can assert whether a visible job was recorded."""
        insert = insert or mock.Mock(return_value=({"ok": True, "job_id": "imp"}, 200))
        start = mock.Mock(return_value=mock.MagicMock(job_id="rec", status="success"))
        with mock.patch.object(recovery_service.jobs, "start_python", start), \
             mock.patch.object(matching_service, "_folder_release_preflight", preflight), \
             mock.patch.object(matching_service, "_carrier_expected_as_wanted",
                               side_effect=lambda c: list(c.get("expected_tracks") or [])), \
             mock.patch.object(import_service, "start_folder_import_with_id", insert):
            recovery_service._advance_schedule_tick()
        return start


class QuietSweepTests(_Mixin):
    def test_no_change_tick_records_no_job_but_updates_status(self):
        """(1) A review_required carrier that still fails the gate is steady
        state: the tick records NO job, but writes the rolling status."""
        self._carrier("stuck", "review_required")
        pf, _ = self._failing_preflight()
        start = self._tick(pf)
        start.assert_not_called()
        status = recovery_service.read_advance_status()
        self.assertTrue(status, "rolling status must be written even for a quiet sweep")
        self.assertFalse(status["changes_made"])
        self.assertFalse(status["needs_attention"])
        self.assertEqual(status["counts"]["kept_review"], 1)
        self.assertEqual(status["counts"]["recovered"], 0)

    def test_handoff_tick_records_a_visible_job(self):
        """(2) A downloaded, gate-passing carrier is handed off -> changes_made,
        so the tick records exactly one visible job."""
        self._carrier("pass", "downloaded")
        pf, _ = self._passing_preflight()
        start = self._tick(pf)
        self.assertEqual(start.call_count, 1)
        self.assertEqual(start.call_args.kwargs["label"], "Advance missing-track recovery")
        status = recovery_service.read_advance_status()
        self.assertTrue(status["changes_made"])
        self.assertEqual(status["counts"]["recovered"], 1)

    def test_newly_review_tick_records_a_visible_job(self):
        """(2b) A carrier that NEWLY falls into review this run needs attention,
        so it records a job even though nothing was handed off."""
        self._carrier("fresh", "downloaded")  # downloaded -> review_required this run
        pf, _ = self._failing_preflight()
        start = self._tick(pf)
        self.assertEqual(start.call_count, 1)
        status = recovery_service.read_advance_status()
        self.assertFalse(status["changes_made"])
        self.assertTrue(status["needs_attention"])
        self.assertEqual(status["counts"]["newly_review"], 1)


class ReCheckCheckpointTests(_Mixin):
    def test_unchanged_carrier_is_not_reverified_next_tick(self):
        """(3) Checkpoint skip: an unchanged stuck carrier is re-verified once,
        then skipped on the next tick (no second preflight)."""
        self._carrier("stuck", "review_required")
        pf, calls = self._failing_preflight()
        self._tick(pf)
        self._tick(pf)
        self.assertEqual(len(calls), 1, "unchanged carrier must not be re-verified")

    def test_recheck_floor_forces_reverify_once_elapsed(self):
        """(4) Hourly floor: once the stored checkpoint is older than the floor,
        the carrier IS re-verified again."""
        self._carrier("stuck", "review_required")
        pf, calls = self._failing_preflight()
        self._tick(pf)
        self.assertEqual(len(calls), 1)
        # Age the checkpoint past the floor (no sleep).
        cps = recovery_service._load_checkpoints()
        for cp in cps.values():
            cp["ts"] = time.time() - (recovery_service.ADVANCER_RECHECK_FLOOR_SECONDS + 10)
        recovery_service._save_checkpoints(cps, time.time())
        self._tick(pf)
        self.assertEqual(len(calls), 2, "a checkpoint past the floor must re-verify")

    def test_recheck_floor_disabled_always_reverifies(self):
        """(C) Floor <= 0 disables the skip entirely (old always-verify behavior)."""
        self._carrier("stuck", "review_required")
        pf, calls = self._failing_preflight()
        with mock.patch.object(recovery_service, "ADVANCER_RECHECK_FLOOR_SECONDS", 0):
            self._tick(pf)
            self._tick(pf)
        self.assertEqual(len(calls), 2)

    def test_new_files_invalidate_checkpoint_immediately(self):
        """(5) A new download (changed staged-file fingerprint) invalidates the
        checkpoint and forces an immediate re-verify, preserving auto-resume."""
        cid, folder = self._carrier("stuck", "review_required")
        pf, calls = self._failing_preflight()
        self._tick(pf)
        self.assertEqual(len(calls), 1)
        # A freshly downloaded extra track lands in the staged folder.
        with open(os.path.join(folder, "02 new.flac"), "wb") as fh:
            fh.write(b"\x01" * 32)
        self._tick(pf)
        self.assertEqual(len(calls), 2, "new staged files must force a re-verify")

    def test_revision_change_invalidates_checkpoint(self):
        """(5b) An external carrier write (revision bump) invalidates the
        checkpoint even when the staged files are unchanged."""
        cid, _ = self._carrier("stuck", "review_required")
        pf, calls = self._failing_preflight()
        self._tick(pf)
        self.assertEqual(len(calls), 1)
        # An external writer touches the carrier (stays review_required, rev++).
        rc.advance_state(cid, "review_required", payload_extra={"note": "external"})
        self._tick(pf)
        self.assertEqual(len(calls), 2)

    def test_checkpoints_survive_restart(self):
        """(8) Checkpoints are durable: after a process restart (store registry
        cleared, DB re-opened) an unchanged carrier is still skipped."""
        cid, _ = self._carrier("stuck", "review_required")
        pf, calls = self._failing_preflight()
        self._tick(pf)
        self.assertIn(cid, recovery_service._load_checkpoints())
        # Simulate a restart: drop every in-memory store handle.
        rc._store_registry.clear()
        self._tick(pf)
        self.assertEqual(len(calls), 1, "checkpoint must survive restart and still skip")


class SingleFlightTests(_Mixin):
    def test_tick_is_a_noop_while_the_workflow_lock_is_held(self):
        """(6) The scheduled tick is a quiet no-op while the advancer workflow
        lock is held (a manual POST or a prior tick) -- no job recorded."""
        handle = job_contract.enter(recovery_service.ADVANCER_WORKFLOW,
                                    wait_seconds=0, fail_fast_in_process=True)
        try:
            pf, calls = self._failing_preflight()
            start = self._tick(pf)
            start.assert_not_called()
            self.assertEqual(len(calls), 0, "the sweep must not run while the lock is held")
        finally:
            handle.close()

    def test_manual_start_is_refused_while_a_sweep_job_runs(self):
        """(6b) The manual POST path's dedupe_key still refuses a concurrent
        start while an advancer job is running."""
        ev = threading.Event()

        def _block(log):
            ev.wait(5)

        busy = jobs.start_python(_block, label="busy-advance", metadata={
            "type": recovery_service.ADVANCER_JOB_TYPE, "mutating": True,
            "dedupe_key": recovery_service.ADVANCER_JOB_TYPE})
        try:
            with self.assertRaises(DuplicateJobError):
                recovery_service.start_recovery_advance_job()
        finally:
            ev.set()
            for _ in range(100):
                if busy.status != "running":
                    break
                time.sleep(0.05)


class StatusEndpointTests(_Mixin):
    def test_status_endpoint_returns_structured_counts(self):
        """(7) GET /api/import/recovery/status returns the structured counts,
        last_run, next_scheduled_check and changes_made."""
        self._carrier("stuck", "review_required")
        pf, _ = self._failing_preflight()
        with mock.patch.object(matching_service, "_folder_release_preflight", pf), \
             mock.patch.object(matching_service, "_carrier_expected_as_wanted",
                               side_effect=lambda c: list(c.get("expected_tracks") or [])):
            recovery_service.run_advance_sweep(log=[], trigger="manual")
        with patch_app_family("app", "_security_auth_disabled", return_value=True):
            res = app_module.app.test_client().get("/api/import/recovery/status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["has_run"])
        status = data["status"]
        self.assertIn("last_run", status)
        self.assertIn("next_scheduled_check", status)
        self.assertFalse(status["changes_made"])
        counts = status["counts"]
        for key in ("examined", "recovery_attempted", "recovered",
                    "still_unresolved", "skipped", "newly_review"):
            self.assertIn(key, counts)
        self.assertEqual(counts["still_unresolved"], 1)

    def test_status_endpoint_before_any_run(self):
        """Before any sweep, the endpoint reports has_run False with an empty
        status rather than erroring."""
        with patch_app_family("app", "_security_auth_disabled", return_value=True):
            res = app_module.app.test_client().get("/api/import/recovery/status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["has_run"])


class StatusShapeTests(_Mixin):
    """QA regression (#762): the orphan-reconcile branch of _build_advance_status
    gates a visible job just like a hand-off. Covered at the unit level because
    the integration ticks above only exercise the hand-off / newly-review paths,
    leaving adopted/removed/orphan-error untested."""

    def test_orphan_adopt_counts_as_a_change(self):
        report = recovery_service._build_advance_status(
            {"scanned": 3, "handed_off": 0, "kept_review": 0, "newly_review": 0,
             "skipped": 3, "orphan_reconcile": {"adopted": 1, "removed": 0}},
            trigger="scheduled")
        self.assertTrue(report["changes_made"], "an adopted orphan is a change")
        self.assertFalse(report["needs_attention"])
        self.assertEqual(report["counts"]["adopted"], 1)

    def test_orphan_remove_counts_as_a_change(self):
        report = recovery_service._build_advance_status(
            {"scanned": 1, "handed_off": 0, "kept_review": 0, "newly_review": 0,
             "skipped": 1, "orphan_reconcile": {"adopted": 0, "removed": 2}},
            trigger="scheduled")
        self.assertTrue(report["changes_made"], "a removed orphan is a change")
        self.assertEqual(report["counts"]["removed"], 2)

    def test_orphan_error_needs_attention(self):
        report = recovery_service._build_advance_status(
            {"scanned": 1, "handed_off": 0, "kept_review": 1, "newly_review": 0,
             "skipped": 0, "orphan_reconcile": {"error": "OSError disk full"}},
            trigger="scheduled")
        self.assertTrue(report["needs_attention"], "an orphan-reconcile error needs attention")
        self.assertIn("OSError", report["error"])

    def test_pure_quiet_sweep_is_neither_change_nor_attention(self):
        report = recovery_service._build_advance_status(
            {"scanned": 2, "handed_off": 0, "kept_review": 2, "newly_review": 0,
             "skipped": 0},
            trigger="scheduled")
        self.assertFalse(report["changes_made"])
        self.assertFalse(report["needs_attention"])


if __name__ == "__main__":
    unittest.main()
