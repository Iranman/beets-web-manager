"""ARCH-030 Phase 2: start_album_download enriches and drives the recovery
carrier.

A missing-track recovery (existing album + requested missing tracks):
  * creates a carrier populated with recording-level ``expected_tracks`` (from
    the missing-track queue) and ``source`` {method, provenance}, keeping the
    canonical Release Group distinct from the supporting Release;
  * advances the carrier's durable checkpoints CREATED -> DOWNLOADED -> STAGED
    as the download/stage progresses; and
  * runs under ONE guarded workflow keyed on ``recovery_workflow_id(carrier)``
    so a restart resumes the same workflow instead of splitting jobs.

A plain full-album download is NOT a recovery: no carrier, and the per-album
download-import workflow is kept.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.acquisition_service as acq
import backend.job_contract as job_contract
import backend.recovery_carrier as recovery_carrier


RGID = "17a64abf-6d31-473f-a740-38ffa548f225"
RGID2 = "99999999-6d31-473f-a740-38ffa548f225"
RELEASE = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class _RunResult:
    def __init__(self, body, jobs, guarded, advance, downloads):
        self.body = body
        self.jobs = jobs
        self.guarded = guarded
        self.advance = advance
        self.downloads = downloads

    @property
    def metadata(self):
        return self.jobs.start_python.call_args.kwargs["metadata"]

    @property
    def workflow(self):
        return self.guarded.call_args.kwargs["workflow"]

    @property
    def checkpoints(self):
        return [c.kwargs.get("checkpoint") for c in self.advance.call_args_list]


class AcquisitionCarrierTests(unittest.TestCase):
    def _run(self, payload, wanted_from_mb, seed=None):
        # ignore_cleanup_errors: the carrier SQLite store keeps its WAL handle
        # open, which blocks rmtree on Windows (teardown-only artifact).
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        downloads = Path(tmp.name, "downloads").resolve()
        downloads.mkdir(parents=True, exist_ok=True)
        state_dir = Path(tmp.name, "carrier_state")

        def _fake_ytdlp(artist, album, year, yt_dir, log, tracks=None, *a, **k):
            Path(yt_dir).mkdir(parents=True, exist_ok=True)
            (Path(yt_dir) / "01.flac").write_bytes(b"x")
            return len(tracks) if tracks else int(k.get("track_count") or 1)

        env = dict(os.environ)
        env["RECOVERY_CARRIER_STATE_DIR"] = str(state_dir)

        patches = [
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(acq, "jobs"),
            mock.patch.object(acq, "validated_downloads_root", return_value=downloads),
            mock.patch.object(acq, "slskd_configured", return_value=False),
            mock.patch.object(acq, "_resolve_album_release_for_import", return_value=RELEASE),
            mock.patch.object(acq, "_fetch_mb_release_tracklist",
                              return_value={"ok": True, "tracks": [1, 2, 3, 4, 5, 6],
                                            "release_group": RGID}),
            mock.patch.object(acq, "_wanted_tracks_not_in_album", return_value=wanted_from_mb),
            mock.patch.object(acq, "_album_folder_for_album_id",
                              return_value="/music/Artist/Album"),
            mock.patch.object(acq, "_ytdlp_missing_tracks_download", side_effect=_fake_ytdlp),
            mock.patch.object(acq, "_ytdlp_album_download", side_effect=_fake_ytdlp),
            mock.patch.object(acq, "_validate_wanted_download_identity_before_import",
                              side_effect=lambda d, w, log, **k: (list(w), [])),
            mock.patch.object(acq, "_validate_import_source_audio"),
            mock.patch.object(acq, "_start_reimport_disk_job_internal", return_value="imp-1"),
            mock.patch.object(acq, "_wait_for_child_job", return_value={"ok": True}),
            mock.patch.object(acq.job_contract, "guarded", wraps=job_contract.guarded),
            mock.patch.object(acq.recovery_carrier, "advance_state",
                              wraps=recovery_carrier.advance_state),
        ]
        started = [p.start() for p in patches]
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])
        jobs = started[1]
        jobs.start_python.return_value.job_id = "job-xyz"
        # The two wraps-spies, by identity rather than brittle list indices.
        guarded = acq.job_contract.guarded
        advance = acq.recovery_carrier.advance_state

        # Seed runs with the env patch (RECOVERY_CARRIER_STATE_DIR) active so a
        # pre-existing carrier lands in the same store the request will use.
        if seed is not None:
            seed()

        body, status = acq.start_album_download(payload)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)

        # Run the guarded job body exactly as the engine would.
        job_fn = jobs.start_python.call_args[0][0]
        log = []
        job_fn(log)
        return _RunResult(body, jobs, guarded, advance, downloads)

    def test_recovery_populates_enriches_and_drives_carrier(self):
        wanted = [{
            "disc": 1, "track": 6, "title": "Last Song",
            "mb_trackid": "rec-6", "duration_ms": 190000,
        }]
        res = self._run(
            {
                "artist": "Artist", "album": "Album", "year": "2024",
                "method": "ytdlp", "auto_import": True,
                "existing_album_id": 42,
                "mb_releasegroupid": RGID,
                "missing_tracks": [{"track": 6, "title": "Last Song", "recording_id": "rec-6"}],
            },
            wanted_from_mb=wanted,
        )

        cid = res.metadata["recovery_carrier_id"]
        self.assertTrue(cid)

        # (3) ONE guarded workflow keyed on the carrier -> resumable, not split.
        self.assertEqual(res.workflow, recovery_carrier.recovery_workflow_id(cid))

        carrier = recovery_carrier.get_carrier(cid)
        self.assertIsNotNone(carrier)

        # (1) canonical RG stays distinct from the supporting Release.
        self.assertEqual(carrier["mb_releasegroupid"], RGID)
        self.assertEqual(carrier["release_id"], RELEASE)
        self.assertNotEqual(carrier["mb_releasegroupid"], carrier["release_id"])

        # (1) expected_tracks carry recording-level evidence from the queue.
        exp = carrier["expected_tracks"]
        self.assertEqual(len(exp), 1)
        self.assertEqual(exp[0]["recording_id"], "rec-6")
        self.assertEqual(exp[0]["track"], 6)
        self.assertEqual(exp[0]["disc"], 1)
        self.assertEqual(exp[0]["duration"], 190000)
        self.assertEqual(exp[0]["title"], "Last Song")

        # (1) source {method, provenance}.
        self.assertEqual(carrier["source"]["method"], "ytdlp")
        self.assertEqual(carrier["source"]["provenance"], "download-import")

        # (2) checkpoints advanced in order; STAGED is the last this layer owns.
        self.assertEqual(res.checkpoints, ["DOWNLOADED", "STAGED"])
        self.assertEqual(carrier["checkpoint"], "STAGED")

    def _seed_victim(self, carrier_id, *, album_id, rgid):
        """A victim recovery already in flight: mid-workflow (STAGED) with
        durable verification evidence that a clobber would destroy."""
        def _seed():
            recovery_carrier.create_carrier(
                carrier_id=carrier_id, existing_album_id=album_id,
                mb_releasegroupid=rgid, release_id=RELEASE,
                wanted_tracks=[{"track": 6}],
            )
            recovery_carrier.advance_state(
                carrier_id, "verifying", checkpoint="STAGED",
                evidence={"acoustid": "proven"})
        return _seed

    def _assert_victim_intact(self, carrier_id, *, album_id, rgid):
        victim = recovery_carrier.get_carrier(carrier_id)
        self.assertIsNotNone(victim)
        self.assertEqual(victim["existing_album_id"], album_id)
        self.assertEqual(victim["mb_releasegroupid"], rgid)
        self.assertEqual(victim["checkpoint"], "STAGED")  # not reset to CREATED
        self.assertEqual(victim["verification_evidence"], {"acoustid": "proven"})

    def test_forged_carrier_id_wrong_album_does_not_clobber(self):
        # Finding A: a crafted request for a DIFFERENT album (99) carries the
        # victim's (album 42) carrier id. It must not clobber/reset the victim;
        # a fresh carrier is minted for the attacker's own recovery instead.
        victim_id = "a" * 32
        res = self._run(
            {
                "artist": "Attacker", "album": "Other", "year": "2024",
                "method": "ytdlp", "auto_import": True,
                "existing_album_id": 99,
                "mb_releasegroupid": RGID,
                "missing_tracks": [{"track": 3, "recording_id": "rec-3"}],
                "recovery_carrier_id": victim_id,
            },
            wanted_from_mb=[{"disc": 1, "track": 3, "title": "X",
                             "mb_trackid": "rec-3", "duration_ms": 1000}],
            seed=self._seed_victim(victim_id, album_id=42, rgid=RGID),
        )
        self._assert_victim_intact(victim_id, album_id=42, rgid=RGID)
        cid = res.metadata["recovery_carrier_id"]
        self.assertTrue(cid)
        self.assertNotEqual(cid, victim_id)  # fresh id, not the stolen one
        self.assertEqual(recovery_carrier.get_carrier(cid)["existing_album_id"], 99)

    def test_forged_carrier_id_wrong_release_group_does_not_clobber(self):
        # Finding A: same existing album (42) but a different requested Release
        # Group than the stored carrier -> still a mismatch, id is ignored.
        victim_id = "c" * 32
        res = self._run(
            {
                "artist": "Attacker", "album": "Other", "year": "2024",
                "method": "ytdlp", "auto_import": True,
                "existing_album_id": 42,
                "mb_releasegroupid": RGID2,
                "missing_tracks": [{"track": 3, "recording_id": "rec-3"}],
                "recovery_carrier_id": victim_id,
            },
            wanted_from_mb=[{"disc": 1, "track": 3, "title": "X",
                             "mb_trackid": "rec-3", "duration_ms": 1000}],
            seed=self._seed_victim(victim_id, album_id=42, rgid=RGID),
        )
        self._assert_victim_intact(victim_id, album_id=42, rgid=RGID)
        cid = res.metadata["recovery_carrier_id"]
        self.assertTrue(cid)
        self.assertNotEqual(cid, victim_id)

    def test_legitimate_resume_preserves_checkpoint_and_evidence(self):
        # Finding A: a genuine resume (same album + same Release Group, matching
        # the stored carrier) reuses the carrier WITHOUT the resetting upsert, so
        # its durable verification evidence survives. The id is honored.
        carrier_id = "b" * 32
        res = self._run(
            {
                "artist": "Artist", "album": "Album", "year": "2024",
                "method": "ytdlp", "auto_import": True,
                "existing_album_id": 42,
                "mb_releasegroupid": RGID,
                "missing_tracks": [{"track": 6, "title": "Last Song",
                                    "recording_id": "rec-6"}],
                "recovery_carrier_id": carrier_id,
            },
            wanted_from_mb=[{"disc": 1, "track": 6, "title": "Last Song",
                             "mb_trackid": "rec-6", "duration_ms": 190000}],
            seed=self._seed_victim(carrier_id, album_id=42, rgid=RGID),
        )
        # The same carrier is reused, not a fresh one.
        self.assertEqual(res.metadata["recovery_carrier_id"], carrier_id)
        carrier = recovery_carrier.get_carrier(carrier_id)
        self.assertEqual(carrier["existing_album_id"], 42)
        self.assertEqual(carrier["mb_releasegroupid"], RGID)
        # Evidence from before the resume is preserved (NOT reset to {}).
        self.assertEqual(carrier["verification_evidence"], {"acoustid": "proven"})

    def test_full_album_download_is_not_a_recovery(self):
        res = self._run(
            {
                "artist": "Artist", "album": "Album", "year": "2024",
                "method": "ytdlp", "auto_import": True,
            },
            wanted_from_mb=[],
        )
        self.assertEqual(res.metadata["recovery_carrier_id"], "")
        self.assertTrue(res.workflow.startswith("download-import-"))
        self.assertEqual(res.checkpoints, [])  # no carrier -> no advances


if __name__ == "__main__":
    unittest.main()
