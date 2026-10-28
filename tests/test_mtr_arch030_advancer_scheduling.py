"""ARCH-030 F6 + F9 regressions: the recovery advancer runs AUTOMATICALLY and
its keep-in-review fallback is a compare-and-set.

F6 (inert advancer): before the fix the lifecycle advancer ran ONLY when
something POSTed /api/import/recovery/advance -- no scheduler, no handoff -- so
a carrier a download left stuck in DOWNLOADED/verifying/review_required never
advanced on its own (requirement 8 was not met end-to-end). These tests prove:
  (i)   a downloaded, gate-passing carrier is handed to the verified-insertion
        entrypoint via the SCHEDULER TICK -- no manual POST -- and only because
        the matching contract's action_allowed=True; a non-passing sibling stays
        in review.
  (ii)  repeat ticks do NOT create duplicate hand-offs: once the insertion claims
        the carrier out of an advanceable state, the next sweep skips it.
  (iii) start_recovery_advance_schedule wires the sweep onto jobs.every, and is a
        no-op when disabled; a tick refused as a duplicate is a quiet no-op.

F9 (CAS race): the keep-in-review fallback used a blind advance_state with no
expected_revision, so a concurrent CLAIM (-> importing) that landed during the
slow preflight was clobbered back to review_required. The fallback now re-reads,
skips a carrier that is no longer advanceable, and compare-and-sets.

Isolated fixtures only (a per-test carrier state dir); no production music or
library is touched, and no real job thread is started (start_python is driven
synchronously / the guard is bypassed).
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

import backend.job_contract as job_contract
import backend.recovery_carrier as rc
import backend.recovery_service as recovery_service
import backend.import_service as import_service
import backend.matching_service as matching_service

RGID = "37cb4a72-7777-8888-9999-aaaaaaaaaaaa"
RELEASE = "99999999-8888-7777-6666-555555555555"


class _CarrierStateMixin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("RECOVERY_CARRIER_STATE_DIR")
        os.environ["RECOVERY_CARRIER_STATE_DIR"] = self.tmp
        rc._store_registry.clear()

    def tearDown(self):
        rc._store_registry.clear()
        if self._prev is None:
            os.environ.pop("RECOVERY_CARRIER_STATE_DIR", None)
        else:
            os.environ["RECOVERY_CARRIER_STATE_DIR"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _staged_dir(self, name):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "01 track.flac"), "wb") as fh:
            fh.write(b"\x00" * 16)
        return d


def _sync_start_python(fn, label="", metadata=None):
    """Run the advancer job body synchronously instead of on a thread, so the
    scheduled tick's effect is deterministic in-test."""
    fn([])
    return mock.MagicMock(job_id="sync", label=label, status="success")


class ScheduledAdvanceTests(_CarrierStateMixin):
    def _drive_tick(self, preflight, insert):
        """Drive one scheduler tick with the advancer guard bypassed and
        start_python run synchronously; returns nothing (asserts on side effects
        happen via the mocks the caller passes)."""
        with mock.patch.object(recovery_service.job_contract, "guarded",
                               side_effect=lambda fn, **kw: fn), \
             mock.patch.object(recovery_service.jobs, "start_python", _sync_start_python), \
             mock.patch.object(matching_service, "_folder_release_preflight", preflight), \
             mock.patch.object(matching_service, "_carrier_expected_as_wanted",
                               side_effect=lambda c: list(c.get("expected_tracks") or [])), \
             mock.patch.object(import_service, "start_folder_import_with_id", insert):
            return recovery_service._advance_schedule_tick()

    def test_scheduled_tick_auto_inserts_only_gate_passing_carrier(self):
        """(i) A downloaded carrier whose contract authorizes action is handed to
        insertion by the SCHEDULER TICK (no manual POST); a non-authorized
        sibling stays in review with a contract reason."""
        pass_dir = self._staged_dir("pass")
        hold_dir = self._staged_dir("hold")
        pass_cid = rc.create_carrier(
            existing_album_id=42, mb_releasegroupid=RGID, release_id=RELEASE,
            wanted_tracks=[{"track": 1, "recording_id": "r1"}],
            expected_tracks=[{"recording_id": "r1", "track": 1, "disc": 1, "title": "Intro"}],
            staged_folder_path=pass_dir, state="downloaded")["carrier_id"]
        hold_cid = rc.create_carrier(
            existing_album_id=43, mb_releasegroupid=RGID, release_id=RELEASE,
            wanted_tracks=[{"track": 2, "recording_id": "r2"}],
            expected_tracks=[{"recording_id": "r2", "track": 2, "disc": 1, "title": "Outro"}],
            staged_folder_path=hold_dir, state="downloaded")["carrier_id"]

        def _preflight(folder, release, album_id, *, log=None, **kw):
            if folder == pass_dir:
                return {"ok": True, "matching_decision": {
                    "action_allowed": True, "identity_proof": "deterministic_metadata_slot"}}
            return {"ok": False, "error": "no fingerprint evidence for these tracks",
                    "matching_decision": {"action_allowed": False,
                                          "identity_proof": "release_group_id"}}

        handed = []

        def _insert(payload):
            handed.append(payload)
            return {"ok": True, "job_id": "imp-1"}, 200

        self._drive_tick(_preflight, _insert)

        # Auto-inserted via the scheduled sweep, with NO call to the manual POST.
        self.assertEqual(len(handed), 1, handed)
        self.assertEqual(handed[0]["recovery_carrier_id"], pass_cid)
        self.assertEqual(handed[0]["existing_album_id"], 42)
        # The non-authorized carrier stays in review with an accurate reason.
        hold = rc.get_carrier(hold_cid)
        self.assertEqual(hold["state"], "review_required")
        self.assertFalse(hold["matching_decision"]["action_allowed"])

    def test_repeat_ticks_do_not_duplicate_handoff(self):
        """(ii) Once insertion claims the carrier out of an advanceable state, a
        second scheduler tick skips it -- no duplicate import / retry storm."""
        pass_dir = self._staged_dir("pass")
        pass_cid = rc.create_carrier(
            existing_album_id=42, mb_releasegroupid=RGID, release_id=RELEASE,
            wanted_tracks=[{"track": 1, "recording_id": "r1"}],
            expected_tracks=[{"recording_id": "r1", "track": 1, "disc": 1, "title": "Intro"}],
            staged_folder_path=pass_dir, state="downloaded")["carrier_id"]

        def _preflight(folder, release, album_id, *, log=None, **kw):
            return {"ok": True, "matching_decision": {
                "action_allowed": True, "identity_proof": "deterministic_metadata_slot"}}

        handed = []

        def _insert(payload):
            # The real verified-insertion entrypoint claims the carrier (CAS) out
            # of the advanceable set; mirror that so the next sweep skips it.
            rc.advance_state(payload["recovery_carrier_id"], "importing")
            handed.append(payload)
            return {"ok": True, "job_id": "imp-1"}, 200

        self._drive_tick(_preflight, _insert)
        self._drive_tick(_preflight, _insert)

        self.assertEqual(len(handed), 1, handed)
        self.assertEqual(rc.get_carrier(pass_cid)["state"], "importing")

    def test_start_schedule_wires_jobs_every_and_respects_disable(self):
        """(iii) The schedule registers the tick on jobs.every; interval <= 0
        disables it; a duplicate tick is a quiet no-op."""
        with mock.patch.object(recovery_service, "jobs") as mjobs:
            recovery_service.start_recovery_advance_schedule()
            mjobs.every.assert_called_once()
            args, kwargs = mjobs.every.call_args
            self.assertEqual(args[0], recovery_service.ADVANCER_SCHEDULE_NAME)
            self.assertIs(args[2], recovery_service._advance_schedule_tick)

        with mock.patch.object(recovery_service, "ADVANCER_INTERVAL_SECONDS", 0), \
             mock.patch.object(recovery_service, "jobs") as mjobs:
            self.assertIsNone(recovery_service.start_recovery_advance_schedule())
            mjobs.every.assert_not_called()

        # A sweep already running: the tick swallows DuplicateJobError, no storm.
        from job_engine import DuplicateJobError
        busy = mock.MagicMock(label="x", job_id="j", status="running")
        with mock.patch.object(recovery_service, "start_recovery_advance_job",
                               side_effect=DuplicateJobError(busy)):
            self.assertIsNone(recovery_service._advance_schedule_tick())


class FallbackCasRaceTests(_CarrierStateMixin):
    def test_fallback_does_not_clobber_concurrent_claim(self):
        """F9: a concurrent job CLAIMS the carrier (-> importing) during the slow
        preflight. The keep-in-review fallback must NOT clobber that claim back
        to review_required (the pre-fix blind advance_state did)."""
        folder = self._staged_dir("race")
        cid = rc.create_carrier(
            existing_album_id=42, mb_releasegroupid=RGID, release_id=RELEASE,
            wanted_tracks=[{"track": 1, "recording_id": "r1"}],
            expected_tracks=[{"recording_id": "r1", "track": 1, "disc": 1, "title": "Intro"}],
            staged_folder_path=folder, state="downloaded")["carrier_id"]

        def _racing_preflight(f, release, album_id, *, log=None, **kw):
            # Another job claims the carrier mid-preflight (the real race): the
            # fingerprint/insertion owner transitions it to 'importing'.
            rc.advance_state(cid, "importing")
            return {"ok": False, "error": "no fingerprint evidence for these tracks",
                    "matching_decision": {"action_allowed": False,
                                          "identity_proof": "release_group_id"}}

        with mock.patch.object(matching_service, "_folder_release_preflight", _racing_preflight), \
             mock.patch.object(matching_service, "_carrier_expected_as_wanted",
                               side_effect=lambda c: list(c.get("expected_tracks") or [])):
            recovery_service.advance_stuck_carriers(limit=25, log=[])

        # The concurrent claim survives: NOT clobbered back to review_required.
        self.assertEqual(rc.get_carrier(cid)["state"], "importing")

    def test_stale_expected_revision_is_a_cas_miss_not_an_overwrite(self):
        """F9 (unit): a review_required transition issued with a STALE revision
        raises instead of clobbering -- the contract the fallback relies on."""
        cid = rc.create_carrier(
            existing_album_id=42, mb_releasegroupid=RGID, release_id=RELEASE,
            wanted_tracks=[{"track": 1, "recording_id": "r1"}],
            staged_folder_path=self._staged_dir("stale"), state="downloaded")["carrier_id"]
        stale_rev = int(rc.get_carrier(cid)["revision"])
        # A concurrent writer advances the carrier, bumping its revision.
        rc.advance_state(cid, "verifying")
        with self.assertRaises(rc.RecoveryCarrierConflictError):
            rc.advance_state(cid, "review_required", expected_revision=stale_rev)
        # The concurrent writer's state is intact (never overwritten).
        self.assertEqual(rc.get_carrier(cid)["state"], "verifying")


if __name__ == "__main__":
    unittest.main()
