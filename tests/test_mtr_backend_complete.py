"""ARCH-030 missing-track recovery — backend workflow tail (F3/F7/F8/F9) and
the user-facing status contract.

* F8  a format replacement (replace_existing) is NOT classified as a recovery;
* F7  a resume with the same carrier id short-circuits at IMPORTED (no second
      download, no second import);
* F3  a carrier-bound folder with auto_import=false reaches the Review hand-off
      instead of being stranded (it is excluded from the generic AI batch);
* F9  the best-effort Plex-sync leg advances IMPORTED -> PLEX_SYNCED, records a
      skip when the operator disabled refresh, and never fails the import;
* status contract: the stable state+checkpoint -> 7-stage mapping.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.acquisition_service as acq
import backend.import_service as isvc
import backend.job_contract as job_contract
import backend.recovery_carrier as rc
from tests.test_mtr_acquisition_carrier import AcquisitionCarrierTests, RGID, RELEASE


# ───────────────────────────── F8: replacement ─────────────────────────────
class ReplacementNotRecoveryTests(AcquisitionCarrierTests):
    def test_replace_existing_is_not_a_recovery(self):
        """A replacement carries wanted_tracks for a PRESENT track; it must not
        be minted as a recovery (whose precheck would mis-route it to Review)."""
        res = self._run(
            {
                "artist": "Artist", "album": "Album", "year": "2024",
                "method": "ytdlp", "auto_import": True,
                "existing_album_id": 42,
                "mb_releasegroupid": RGID,
                "replace_existing": True,
                "missing_tracks": [{"track": 6, "title": "Last Song", "recording_id": "rec-6"}],
            },
            wanted_from_mb=[{"disc": 1, "track": 6, "title": "Last Song",
                             "mb_trackid": "rec-6", "duration_ms": 190000}],
        )
        self.assertEqual(res.metadata["recovery_carrier_id"], "")
        self.assertTrue(res.workflow.startswith("download-import-"))
        self.assertEqual(res.checkpoints, [])  # no carrier -> no advances


# ───────────────────────── F3 / F7 acquisition harness ─────────────────────
class _AcqHarness(unittest.TestCase):
    """A trimmed start_album_download harness (no Beets) for the non-auto and
    resume paths."""

    def _setup(self, extra_patches=None):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        downloads = Path(tmp.name, "downloads").resolve()
        downloads.mkdir(parents=True, exist_ok=True)
        self.state_dir = Path(tmp.name, "carrier_state")

        def _fake_ytdlp(artist, album, year, yt_dir, log, tracks=None, *a, **k):
            Path(yt_dir).mkdir(parents=True, exist_ok=True)
            (Path(yt_dir) / "01.flac").write_bytes(b"x")
            return len(tracks) if tracks else int(k.get("track_count") or 1)

        self.ytdlp = mock.Mock(side_effect=_fake_ytdlp)
        env = dict(os.environ)
        env["RECOVERY_CARRIER_STATE_DIR"] = str(self.state_dir)
        patches = [
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(acq, "jobs"),
            mock.patch.object(acq, "validated_downloads_root", return_value=downloads),
            mock.patch.object(acq, "slskd_configured", return_value=False),
            mock.patch.object(acq, "_resolve_album_release_for_import", return_value=RELEASE),
            mock.patch.object(acq, "_fetch_mb_release_tracklist",
                              return_value={"ok": True, "tracks": [1, 2, 3, 4, 5, 6],
                                            "release_group": RGID}),
            mock.patch.object(acq, "_wanted_tracks_not_in_album",
                              side_effect=lambda aid, mbid, w, log: list(w)),
            mock.patch.object(acq, "_album_folder_for_album_id", return_value="/music/Artist/Album"),
            mock.patch.object(acq, "_ytdlp_missing_tracks_download", side_effect=self.ytdlp),
            mock.patch.object(acq, "_ytdlp_album_download", side_effect=self.ytdlp),
            mock.patch.object(acq, "_validate_wanted_download_identity_before_import",
                              side_effect=lambda d, w, log, **k: (list(w), [], [])),
            mock.patch.object(acq, "_validate_import_source_audio"),
            mock.patch.object(acq, "_start_reimport_disk_job_internal", return_value="imp-1"),
            mock.patch.object(acq, "_wait_for_child_job", return_value={"ok": True}),
            mock.patch.object(acq.job_contract, "guarded", wraps=job_contract.guarded),
        ]
        for p in (extra_patches or []):
            patches.append(p)
        started = [p.start() for p in patches]
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])
        rc._store_registry.clear()
        self.addCleanup(rc._store_registry.clear)
        self.jobs = started[1]
        self.jobs.start_python.return_value.job_id = "job-xyz"

    def _run(self, payload):
        body, status = acq.start_album_download(payload)
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get("ok"), body)
        job_fn = self.jobs.start_python.call_args[0][0]
        log = []
        result = job_fn(log)
        return result, log


class NonAutoHandoffTests(_AcqHarness):
    def test_auto_import_false_recovery_reaches_review(self):
        """F3: with auto_import=false the carrier folder is excluded from the AI
        batch, so it must be handed to Review WITH its carrier identity, not
        left stranded; the carrier goes to review_required."""
        queue = mock.Mock(return_value=True)
        self._setup([mock.patch.object(acq, "_queue_folder_for_manual_review", queue)])
        result, _log = self._run({
            "artist": "Artist", "album": "Album", "year": "2024",
            "method": "ytdlp", "auto_import": False,
            "existing_album_id": 42, "mb_releasegroupid": RGID,
            "missing_tracks": [{"track": 6, "title": "Last", "recording_id": "rec-6"}],
        })
        self.assertTrue(result.get("queued_for_review"))
        queue.assert_called_once()
        # Threaded WITH the structured carrier identity (not a bare boolean).
        self.assertIn("carrier", queue.call_args.kwargs)
        self.assertIsNotNone(queue.call_args.kwargs["carrier"])
        cid = self.jobs.start_python.call_args.kwargs["metadata"]["recovery_carrier_id"]
        self.assertEqual(rc.get_carrier(cid)["state"], "review_required")

    def test_review_handoff_failure_fails_closed(self):
        """QA-4: if the Review enqueue fails (returns falsy), the carrier-bound
        folder -- excluded from the AI batch -- must not be stranded under a bare
        success. The carrier goes to a recoverable failed state and the job
        result is non-success / queued-for-review-failed."""
        queue = mock.Mock(return_value=False)
        self._setup([mock.patch.object(acq, "_queue_folder_for_manual_review", queue)])
        result, _log = self._run({
            "artist": "Artist", "album": "Album", "year": "2024",
            "method": "ytdlp", "auto_import": False,
            "existing_album_id": 42, "mb_releasegroupid": RGID,
            "missing_tracks": [{"track": 6, "title": "Last", "recording_id": "rec-6"}],
        })
        queue.assert_called_once()
        self.assertFalse(result.get("ok"))
        self.assertFalse(result.get("queued_for_review"))
        self.assertEqual(result.get("error"), "review_handoff_failed")
        cid = self.jobs.start_python.call_args.kwargs["metadata"]["recovery_carrier_id"]
        self.assertEqual(rc.get_carrier(cid)["state"], "failed")


class ResumeNoDoubleImportTests(_AcqHarness):
    def test_resume_at_imported_does_not_redownload_or_reimport(self):
        """F7: a resume re-triggers the download with the SAME carrier id; when
        the carrier already reached IMPORTED the workflow short-circuits -- no
        second download, no second import."""
        cid = "abcdef0123456789abcdef0123456789"
        self._setup()
        # Seed the carrier as already imported (a previous attempt completed the
        # import but the parent workflow was interrupted before the Plex tail).
        rc.create_carrier(carrier_id=cid, existing_album_id=42, mb_releasegroupid=RGID,
                          wanted_tracks=[{"track": 6}],
                          expected_tracks=[{"recording_id": "rec-6", "track": 6, "disc": 1}],
                          staged_folder_path="", state="verifying")
        rc.advance_state(cid, "imported", checkpoint="IMPORTED")

        result, _log = self._run({
            "artist": "Artist", "album": "Album", "year": "2024",
            "method": "ytdlp", "auto_import": True,
            "existing_album_id": 42, "mb_releasegroupid": RGID,
            "recovery_carrier_id": cid,
            "missing_tracks": [{"track": 6, "title": "Last", "recording_id": "rec-6"}],
        })
        self.assertTrue(result.get("resumed"))
        self.ytdlp.assert_not_called()                         # no re-download
        acq._start_reimport_disk_job_internal.assert_not_called()  # no re-import
        # The one guarded workflow is reused, not split.
        self.assertEqual(self.jobs.start_python.call_args.kwargs["metadata"]["recovery_carrier_id"], cid)

    def test_resume_short_circuit_rejects_uncovered_tracks(self):
        """QA-2: a completed carrier (recovered track 6) must NOT report a
        DIFFERENT requested track (3) as already imported. Same album + Release
        Group, but the requested track is not one this carrier recovered -> no
        false already-done success; the request proceeds as a fresh recovery."""
        cid = "abcdef0123456789abcdef0123456789"
        self._setup()
        rc.create_carrier(carrier_id=cid, existing_album_id=42, mb_releasegroupid=RGID,
                          wanted_tracks=[{"track": 6}],
                          expected_tracks=[{"recording_id": "rec-6", "track": 6, "disc": 1}],
                          staged_folder_path="", state="verifying")
        rc.advance_state(cid, "imported", checkpoint="IMPORTED")
        result, _log = self._run({
            "artist": "Artist", "album": "Album", "year": "2024",
            "method": "ytdlp", "auto_import": True,
            "existing_album_id": 42, "mb_releasegroupid": RGID,
            "recovery_carrier_id": cid,
            "missing_tracks": [{"track": 3, "title": "Third", "recording_id": "rec-3"}],
        })
        self.assertFalse(result.get("resumed"))   # not a false already-done
        self.ytdlp.assert_called()                 # proceeded as a real recovery

    def test_resume_short_circuit_rejects_mismatched_album(self):
        """QA-2 / Finding A: a completed carrier for album 42 must not be
        returned as a success for a request targeting a DIFFERENT album (99)."""
        cid = "abcdef0123456789abcdef0123456789"
        self._setup()
        rc.create_carrier(carrier_id=cid, existing_album_id=42, mb_releasegroupid=RGID,
                          wanted_tracks=[{"track": 6}],
                          expected_tracks=[{"recording_id": "rec-6", "track": 6, "disc": 1}],
                          staged_folder_path="", state="verifying")
        rc.advance_state(cid, "imported", checkpoint="IMPORTED")
        result, _log = self._run({
            "artist": "Artist", "album": "Album", "year": "2024",
            "method": "ytdlp", "auto_import": True,
            "existing_album_id": 99, "mb_releasegroupid": RGID,
            "recovery_carrier_id": cid,
            "missing_tracks": [{"track": 6, "title": "Last", "recording_id": "rec-6"}],
        })
        self.assertFalse(result.get("resumed"))
        # A fresh carrier is minted for the attacker's request; the victim's id
        # is not reused and (seeded album 42) stays untouched.
        minted = self.jobs.start_python.call_args.kwargs["metadata"]["recovery_carrier_id"]
        self.assertNotEqual(minted, cid)
        self.assertEqual(rc.get_carrier(cid)["existing_album_id"], 42)

    def test_resume_carrier_read_error_surfaces_not_redownload(self):
        """QA-3: when a legitimate resume's carrier read fails transiently in the
        short-circuit, surface a retryable error instead of silently
        re-downloading/re-importing a possibly-complete recovery."""
        cid = "abcdef0123456789abcdef0123456789"
        self._setup()
        rc.create_carrier(carrier_id=cid, existing_album_id=42, mb_releasegroupid=RGID,
                          wanted_tracks=[{"track": 6}],
                          expected_tracks=[{"recording_id": "rec-6", "track": 6, "disc": 1}],
                          staged_folder_path="", state="verifying")
        rc.advance_state(cid, "imported", checkpoint="IMPORTED")
        real_get = rc.get_carrier
        calls = {"n": 0}

        def _flaky(carrier_id):
            calls["n"] += 1
            if calls["n"] <= 1:
                return real_get(carrier_id)   # request-handler identity guard read OK
            raise RuntimeError("db locked")   # the short-circuit read then fails

        with mock.patch.object(acq.recovery_carrier, "get_carrier", side_effect=_flaky):
            result, _log = self._run({
                "artist": "Artist", "album": "Album", "year": "2024",
                "method": "ytdlp", "auto_import": True,
                "existing_album_id": 42, "mb_releasegroupid": RGID,
                "recovery_carrier_id": cid,
                "missing_tracks": [{"track": 6, "title": "Last", "recording_id": "rec-6"}],
            })
        self.assertEqual(result.get("error"), "recovery_carrier_unreadable")
        self.assertTrue(result.get("retryable"))
        self.ytdlp.assert_not_called()   # did NOT silently re-download


# ──────────────────────────── F9: the Plex leg ─────────────────────────────
class PlexSyncLegTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"RECOVERY_CARRIER_STATE_DIR": str(Path(tmp.name, "rc"))})
        env.start()
        self.addCleanup(env.stop)
        rc._store_registry.clear()
        self.addCleanup(rc._store_registry.clear)

    def _carrier(self, state="imported", checkpoint="IMPORTED"):
        c = rc.create_carrier(existing_album_id=1, mb_releasegroupid=RGID,
                              staged_folder_path="/dl/x", state="verifying")
        if state != "verifying" or checkpoint:
            rc.advance_state(c["carrier_id"], state, checkpoint=checkpoint)
        return rc.get_carrier(c["carrier_id"])

    def test_synced_advances_to_plex_synced(self):
        carrier = self._carrier()
        with mock.patch.object(isvc, "PLEX_SYNC_AUTO_REFRESH", True), \
             mock.patch.object(isvc, "_plex_configured", return_value=True), \
             mock.patch.object(isvc, "_trigger_plex_refresh", return_value=True) as refresh:
            isvc._recovery_plex_sync_leg(carrier, [])
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.kwargs.get("workflow"), "recovery")
        rec = rc.get_carrier(carrier["carrier_id"])
        self.assertEqual(rec["checkpoint"], "PLEX_SYNCED")
        self.assertEqual(rec["plex_sync"]["status"], "synced")

    def test_auto_refresh_off_records_skip_and_stays_imported(self):
        carrier = self._carrier()
        with mock.patch.object(isvc, "PLEX_SYNC_AUTO_REFRESH", False), \
             mock.patch.object(isvc, "_trigger_plex_refresh") as refresh:
            isvc._recovery_plex_sync_leg(carrier, [])
        refresh.assert_not_called()  # the operator disabled it: never forced
        rec = rc.get_carrier(carrier["carrier_id"])
        self.assertEqual(rec["checkpoint"], "IMPORTED")   # terminal imported
        self.assertEqual(rec["plex_sync"]["status"], "skipped")
        self.assertEqual(rec["plex_sync"]["reason"], "auto_refresh_disabled")

    def test_not_configured_records_skip(self):
        carrier = self._carrier()
        with mock.patch.object(isvc, "PLEX_SYNC_AUTO_REFRESH", True), \
             mock.patch.object(isvc, "_plex_configured", return_value=False), \
             mock.patch.object(isvc, "_trigger_plex_refresh") as refresh:
            isvc._recovery_plex_sync_leg(carrier, [])
        refresh.assert_not_called()
        rec = rc.get_carrier(carrier["carrier_id"])
        self.assertEqual(rec["plex_sync"]["reason"], "plex_not_configured")

    def test_failure_is_best_effort_import_intact(self):
        carrier = self._carrier()
        with mock.patch.object(isvc, "PLEX_SYNC_AUTO_REFRESH", True), \
             mock.patch.object(isvc, "_plex_configured", return_value=True), \
             mock.patch.object(isvc, "_trigger_plex_refresh", side_effect=RuntimeError("boom")):
            isvc._recovery_plex_sync_leg(carrier, [])  # must not raise
        rec = rc.get_carrier(carrier["carrier_id"])
        self.assertEqual(rec["state"], "imported")        # import NOT rolled back
        self.assertEqual(rec["checkpoint"], "IMPORTED")   # retriable, not synced
        self.assertEqual(rec["plex_sync"]["status"], "failed")

    def test_idempotent_when_already_synced(self):
        carrier = self._carrier(checkpoint="PLEX_SYNCED")
        with mock.patch.object(isvc, "PLEX_SYNC_AUTO_REFRESH", True), \
             mock.patch.object(isvc, "_plex_configured", return_value=True), \
             mock.patch.object(isvc, "_trigger_plex_refresh", return_value=True) as refresh:
            isvc._recovery_plex_sync_leg(carrier, [])
        refresh.assert_not_called()

    def test_no_op_when_not_imported(self):
        carrier = self._carrier(state="review_required", checkpoint="")
        with mock.patch.object(isvc, "PLEX_SYNC_AUTO_REFRESH", True), \
             mock.patch.object(isvc, "_trigger_plex_refresh") as refresh:
            isvc._recovery_plex_sync_leg(carrier, [])
        refresh.assert_not_called()

    def test_none_carrier_is_safe(self):
        isvc._recovery_plex_sync_leg(None, [])  # must not raise


# ─────────────────────── status contract: stage mapping ────────────────────
class WorkflowStageMappingTests(unittest.TestCase):
    def _stage(self, state, checkpoint="", plex=None):
        c = {"state": state, "checkpoint": checkpoint}
        if plex is not None:
            c["plex_sync"] = plex
        return rc.workflow_stage(c)["stage"]

    def test_no_carrier_is_missing(self):
        self.assertEqual(rc.workflow_stage(None)["stage"], "missing")

    def test_linear_progression(self):
        self.assertEqual(self._stage("created", "CREATED"), "searching")
        self.assertEqual(self._stage("downloaded", "DOWNLOADED"), "downloading")
        self.assertEqual(self._stage("downloaded", "STAGED"), "verifying")
        self.assertEqual(self._stage("importing", "STAGED"), "importing")
        self.assertEqual(self._stage("imported", "IMPORTED"), "plex_sync")
        self.assertEqual(self._stage("imported", "PLEX_SYNCED"), "complete")

    def test_terminals(self):
        self.assertEqual(self._stage("review_required"), "needs_review")
        self.assertEqual(self._stage("failed"), "failed")
        self.assertEqual(self._stage("rolled_back"), "failed")

    def test_plex_skip_is_complete(self):
        self.assertEqual(
            self._stage("imported", "IMPORTED", plex={"status": "skipped", "reason": "auto_refresh_disabled"}),
            "complete")

    def test_plex_failure_shows_plex_stage(self):
        st = rc.workflow_stage({"state": "imported", "checkpoint": "IMPORTED",
                                "plex_sync": {"status": "failed"}})
        self.assertEqual(st["stage"], "plex_sync")
        self.assertFalse(st["terminal"])

    def test_stage_shape_has_no_secrets(self):
        st = rc.workflow_stage({"state": "imported", "checkpoint": "PLEX_SYNCED"})
        self.assertEqual(set(st), {"stage", "stage_index", "label", "terminal", "note"})
        self.assertEqual(st["stage_index"], 6)
        self.assertTrue(st["terminal"])

    def test_review_identity_carries_stage(self):
        carrier = {"carrier_id": "x", "existing_album_id": 1, "mb_releasegroupid": RGID,
                   "state": "importing", "checkpoint": "STAGED"}
        ident = rc.review_identity(carrier)
        self.assertEqual(ident["recovery_stage"]["stage"], "importing")


# ─────────── QA Finding 7: recovery_stage is recomputed fresh on read ───────────
class RecoveryStageFreshnessTests(unittest.TestCase):
    """The Review queue must show the carrier's CURRENT stage, not the snapshot
    taken when the item was queued (the non-auto hand-off snapshots BEFORE the
    carrier advances to review_required)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {"RECOVERY_CARRIER_STATE_DIR": self.tmp})
        self._env.start()
        rc._store_registry.clear()  # pick up the temp DB path

    def tearDown(self):
        self._env.stop()
        rc._store_registry.clear()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _queued_evidence(self):
        """Create a carrier mid-workflow and snapshot it the way the hand-off
        does: evidence.recovery_carrier = review_identity(carrier)."""
        carrier = rc.create_carrier(
            carrier_id="c-fresh", existing_album_id=7, mb_releasegroupid=RGID,
            state="verifying",
        )
        rc.advance_state(carrier["carrier_id"], "verifying", checkpoint="STAGED")
        snapshot = rc.review_identity(rc.get_carrier("c-fresh"))
        self.assertEqual(snapshot["recovery_stage"]["stage"], "verifying")  # stale stage
        return {"recovery_carrier": snapshot}

    def test_read_recomputes_stage_from_live_carrier(self):
        from backend.pending_review_store import _refresh_recovery_stage
        evidence = self._queued_evidence()
        # The carrier advances AFTER the item was queued.
        rc.advance_state("c-fresh", "review_required")
        fresh = _refresh_recovery_stage(evidence)
        self.assertEqual(fresh["recovery_carrier"]["recovery_stage"]["stage"], "needs_review")
        # Shape + no-secrets invariant preserved.
        self.assertEqual(set(fresh["recovery_carrier"]["recovery_stage"]),
                         {"stage", "stage_index", "label", "terminal", "note"})
        # The input snapshot is not mutated in place.
        self.assertEqual(evidence["recovery_carrier"]["recovery_stage"]["stage"], "verifying")

    def test_missing_carrier_keeps_stored_snapshot(self):
        from backend.pending_review_store import _refresh_recovery_stage
        evidence = self._queued_evidence()
        rc._store_registry.clear()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)  # carrier gone/unreadable
        fresh = _refresh_recovery_stage(evidence)
        self.assertEqual(fresh["recovery_carrier"]["recovery_stage"]["stage"], "verifying")

    def test_non_recovery_evidence_untouched(self):
        from backend.pending_review_store import _refresh_recovery_stage
        self.assertEqual(_refresh_recovery_stage({}), {})
        self.assertEqual(_refresh_recovery_stage({"preflight": {"ok": True}}),
                         {"preflight": {"ok": True}})


if __name__ == "__main__":
    unittest.main()
