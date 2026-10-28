"""ARCH-030 Step 2 regressions: stable recovery identity + one-to-many staged
folders + the lifecycle advancer + contract-reconciled review confidence.

Each test reproduces an ORIGINAL defect and proves the fix:
  (i)   two fresh "download missing" requests for the SAME recovery identity
        dedupe to ONE carrier / workflow / folder (the duplicate storm).
  (ii)  a re-stage no longer orphans prior folders: load_for_folder resolves
        EVERY attempt's folder to the one carrier.
  (iii) the advancer drives a review_required carrier whose contract now
        returns action_allowed=True to the insertion entrypoint, and leaves a
        manual-review carrier in review with an accurate, contract reason.
  (iv)  the review ROW confidence equals the matching_decision contract, never
        the stale coarse "low" suggestion string.

Isolated fixtures only (a per-test carrier state dir + temp staged folders);
no production music is touched.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.job_contract as job_contract
import backend.recovery_carrier as rc
import backend.recovery_service as recovery_service

# Subclass the existing acquisition harness for the full start_album_download
# drive (identical mocking of download/import/jobs).
from tests.test_mtr_acquisition_carrier import AcquisitionCarrierTests, RGID, RELEASE


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


class IdentityKeyAndDedupTests(_CarrierStateMixin):
    def test_identity_key_is_stable_and_server_derived(self):
        """Two identical requests (same album + RG + missing-track set) yield
        the SAME server-derived key regardless of track-dict ordering, and a
        different track set yields a different key."""
        a = rc.compute_identity_key(42, RGID, [{"track": 1, "recording_id": "r1"},
                                               {"track": 9, "recording_id": "r9"}])
        b = rc.compute_identity_key(42, RGID, [{"track": 9, "recording_id": "r9"},
                                               {"track": 1, "recording_id": "r1"}])
        self.assertEqual(a, b)
        self.assertNotEqual(a, rc.compute_identity_key(42, RGID, [{"track": 3, "recording_id": "r3"}]))
        self.assertNotEqual(a, rc.compute_identity_key(43, RGID, [{"track": 1, "recording_id": "r1"},
                                                                  {"track": 9, "recording_id": "r9"}]))

    def test_get_by_identity_resumes_non_terminal_skips_terminal(self):
        key = rc.compute_identity_key(42, RGID, [{"track": 6, "recording_id": "r6"}])
        cid = rc.create_carrier(existing_album_id=42, mb_releasegroupid=RGID,
                                wanted_tracks=[{"track": 6, "recording_id": "r6"}],
                                state="downloaded")["carrier_id"]
        self.assertEqual(rc.get_by_identity(identity_key=key)["carrier_id"], cid)
        # A terminal carrier is NOT resumed by default.
        rc.advance_state(cid, "imported", checkpoint="IMPORTED")
        self.assertIsNone(rc.get_by_identity(identity_key=key))
        self.assertEqual(rc.get_by_identity(identity_key=key, include_terminal=True)["carrier_id"], cid)


class AcquisitionDedupTests(AcquisitionCarrierTests):
    def test_duplicate_request_resumes_one_carrier_and_workflow(self):
        """(i) A second identical "download missing" request RESUMES the
        existing non-terminal carrier (same id) and the SAME stable workflow
        slug instead of minting a parallel carrier/workflow -- the storm fix."""
        wanted = [{"disc": 1, "track": 6, "title": "Last Song",
                   "mb_trackid": "rec-6", "duration_ms": 190000}]
        payload = {
            "artist": "Artist", "album": "Album", "year": "2024",
            "method": "ytdlp", "auto_import": True,
            "existing_album_id": 42, "mb_releasegroupid": RGID,
            "missing_tracks": [{"track": 6, "title": "Last Song", "recording_id": "rec-6"}],
        }

        def _seed_first_carrier():
            # Stand in for the FIRST request's carrier (already downloaded,
            # non-terminal), in the same state dir the request will use.
            rc.create_carrier(
                existing_album_id=42, mb_releasegroupid=RGID, release_id=RELEASE,
                wanted_tracks=wanted,
                expected_tracks=[{"recording_id": "rec-6", "track": 6, "disc": 1}],
                state="downloaded")

        res = self._run(payload, wanted_from_mb=wanted, seed=_seed_first_carrier)
        reused = res.metadata["recovery_carrier_id"]
        key = rc.compute_identity_key(42, RGID, payload["missing_tracks"])
        seeded = rc.get_by_identity(identity_key=key)
        # The second request reused the seeded (first) carrier, not a fresh uuid.
        self.assertIsNotNone(seeded)
        self.assertEqual(reused, seeded["carrier_id"])
        # ONE guarded workflow keyed on the stable identity.
        self.assertEqual(res.workflow,
                         rc.recovery_workflow_id(job_contract.slug(key)))


class OneToManyStagedFolderTests(_CarrierStateMixin):
    def test_restage_does_not_orphan_prior_folders(self):
        """(ii) Binding a second staged folder (a re-stage) keeps the FIRST
        folder resolving to the same carrier -- no orphan-on-rebind."""
        first = os.path.join(self.tmp, "dl", "album - soundcloud missing 111")
        second = os.path.join(self.tmp, "dl", "_still_missing_subset")
        os.makedirs(first, exist_ok=True)
        os.makedirs(second, exist_ok=True)
        cid = rc.create_carrier(existing_album_id=42, mb_releasegroupid=RGID,
                                staged_folder_path=first)["carrier_id"]
        rc.bind_staged_folder(cid, second)
        for folder in (first, second):
            got = rc.load_for_folder(folder)
            self.assertIsNotNone(got, f"{folder} was orphaned")
            self.assertEqual(got["carrier_id"], cid)
        self.assertCountEqual(
            [os.path.realpath(p) for p in rc.staged_folders_for(cid)],
            [os.path.realpath(first), os.path.realpath(second)])


class AdvancerTests(_CarrierStateMixin):
    def _staged_with_audio(self, name):
        folder = os.path.join(self.tmp, name)
        os.makedirs(folder, exist_ok=True)
        (Path(folder) / "01.flac").write_bytes(b"x")
        return folder

    def test_advancer_hands_off_gatepass_and_keeps_manual_in_review(self):
        """(iii) The replacement advancer drives a review_required carrier whose
        contract now returns action_allowed=True to the insertion entrypoint,
        and leaves a non-passing one in review with an accurate reason."""
        pass_dir = self._staged_with_audio("pass_dir")
        hold_dir = self._staged_with_audio("hold_dir")
        pass_cid = rc.create_carrier(
            existing_album_id=42, mb_releasegroupid=RGID, release_id=RELEASE,
            wanted_tracks=[{"track": 1, "recording_id": "r1"}],
            expected_tracks=[{"recording_id": "r1", "track": 1, "disc": 1, "title": "Intro"}],
            staged_folder_path=pass_dir, state="review_required")["carrier_id"]
        hold_cid = rc.create_carrier(
            existing_album_id=43, mb_releasegroupid=RGID, release_id=RELEASE,
            wanted_tracks=[{"track": 2, "recording_id": "r2"}],
            expected_tracks=[{"recording_id": "r2", "track": 2, "disc": 1, "title": "Outro"}],
            staged_folder_path=hold_dir, state="review_required")["carrier_id"]

        def _fake_preflight(folder, release, album_id, *, log=None, **kw):
            if folder == pass_dir:
                return {"ok": True, "matching_decision": {
                    "action_allowed": True, "identity_proof": "deterministic_metadata_slot",
                    "confidence": 1.0}}
            return {"ok": False, "error": "no fingerprint evidence for these tracks",
                    "matching_decision": {"action_allowed": False,
                                          "identity_proof": "release_group_id",
                                          "reason_code": "review_required"}}

        handed = []

        def _fake_insert(payload):
            handed.append(payload)
            return {"ok": True, "job_id": "imp-1"}, 200

        import backend.matching_service as matching_service
        import backend.import_service as import_service
        with mock.patch.object(matching_service, "_folder_release_preflight", _fake_preflight), \
             mock.patch.object(matching_service, "_carrier_expected_as_wanted",
                               side_effect=lambda c: list(c.get("expected_tracks") or [])), \
             mock.patch.object(import_service, "start_folder_import_with_id", _fake_insert):
            summary = recovery_service.advance_stuck_carriers(limit=25, log=[])

        self.assertEqual(summary["handed_off"], 1, summary)
        self.assertEqual(summary["kept_review"], 1, summary)
        # The gate-passing carrier was handed to the verified-insertion entrypoint.
        self.assertEqual(len(handed), 1)
        self.assertEqual(handed[0]["recovery_carrier_id"], pass_cid)
        self.assertEqual(handed[0]["existing_album_id"], 42)
        # The non-passing carrier stays in review with an accurate, non-empty reason.
        hold = rc.get_carrier(hold_cid)
        self.assertEqual(hold["state"], "review_required")
        kept = next(c for c in summary["carriers"] if c["carrier_id"] == hold_cid)
        self.assertIn("fingerprint", kept["reason"])
        # Its matching_decision is recorded on the carrier for the review row.
        self.assertFalse(hold["matching_decision"]["action_allowed"])


class ReviewRowConfidenceTests(_CarrierStateMixin):
    def test_row_confidence_derives_from_contract_not_stale_low(self):
        """(iv) The review ROW confidence equals the matching_decision contract
        (here an authorized deterministic subset => "high"), never the stale
        coarse "low" copied into the suggestion at queue time."""
        import routes_import
        from backend.pending_review_store import _refresh_recovery_stage
        cid = rc.create_carrier(
            existing_album_id=42, mb_releasegroupid=RGID, release_id=RELEASE,
            wanted_tracks=[{"track": 1, "recording_id": "r1"}],
            expected_tracks=[{"recording_id": "r1", "track": 1, "disc": 1, "title": "Intro"}],
            state="verifying")["carrier_id"]
        # The advancer/preflight recorded an authorized deterministic contract.
        rc.advance_state(cid, "verifying", payload_extra={"matching_decision": {
            "action_allowed": True, "identity_proof": "deterministic_metadata_slot",
            "confidence": 1.0}})

        stale_suggestion = {"confidence": "low"}  # the queue-time coarse string
        evidence = {"recovery_carrier": {"recovery_carrier_id": cid}}
        refreshed = _refresh_recovery_stage(evidence)
        conf = routes_import._review_row_confidence(stale_suggestion, refreshed)
        self.assertEqual(conf, "high")
        self.assertNotEqual(conf, "low")
        # A non-recovery row keeps its suggestion confidence unchanged.
        self.assertEqual(routes_import._review_row_confidence({"confidence": "medium"}, {}), "medium")


if __name__ == "__main__":
    unittest.main()
