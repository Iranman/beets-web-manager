"""ARCH-030 missing-track recovery — resume/identity/coverage integrity
residuals in start_album_download (backend/acquisition_service.py).

Reproduce-then-fix regressions for five non-blocking integrity edges in the
security-sensitive resume path:

* F3  _recovery_create_carrier must fail CLOSED on a carrier-read error, never
      fall through to the resetting upsert over a possibly-existing row;
* F4  the auto_import=false Review hand-off must fail closed when the carrier
      cannot be reloaded (the folder is excluded from the AI batch), never
      return a bare success that strands it;
* F5  an unknown-position (title-only) track is UNPROVEN and must never compare
      as COVERED -> no false already-done for same album + Release Group;
* F7  the requested Release Group is resolved from mb_albumid too (a
      release-group reference), not only mb_releasegroupid, so the identity
      cross-check is not silently downgraded to album-only;
* F1  a same-album+same-RG resume for a DIFFERENT track set mints a FRESH
      carrier instead of reusing the sibling's stale expected_tracks.
"""

import os
import unittest
from unittest import mock

import backend.acquisition_service as acq
import backend.recovery_carrier as rc
from tests.test_mtr_backend_complete import _AcqHarness
from tests.test_mtr_acquisition_carrier import RGID, RELEASE

RGID2 = "99999999-6d31-473f-a740-38ffa548f225"


class ResumeCoverageIntegrityTests(_AcqHarness):
    # ───────────────────────── F3: create fails closed ─────────────────────
    def test_create_carrier_read_error_does_not_reset_existing_row(self):
        """F3: during an honored resume, a transient carrier-read error inside
        _recovery_create_carrier must NOT fall through to create_carrier (an
        unconditional upsert that resets checkpoint/evidence). Fail closed:
        skip creation; the existing durable row is untouched."""
        cid = "abcdef0123456789abcdef0123456789"
        create_spy = mock.Mock(wraps=rc.create_carrier)
        self._setup([mock.patch.object(acq.recovery_carrier, "create_carrier", create_spy)])
        rc.create_carrier(carrier_id=cid, existing_album_id=42, mb_releasegroupid=RGID,
                          release_id=RELEASE,
                          wanted_tracks=[{"track": 6}],
                          expected_tracks=[{"recording_id": "rec-6", "track": 6, "disc": 1}],
                          state="verifying")
        rc.advance_state(cid, "verifying", checkpoint="STAGED",
                         evidence={"acoustid": "proven"})
        create_spy.reset_mock()  # ignore the seed create above

        real_get = rc.get_carrier
        calls = {"n": 0}

        def _flaky(carrier_id):
            # 1 = request-handler reuse guard; 2 = resume short-circuit;
            # 3 = _recovery_create_carrier's existence check -> raise there.
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("db locked")
            return real_get(carrier_id)

        with mock.patch.object(acq.recovery_carrier, "get_carrier", side_effect=_flaky):
            self._run({
                "artist": "Artist", "album": "Album", "year": "2024",
                "method": "ytdlp", "auto_import": True,
                "existing_album_id": 42, "mb_releasegroupid": RGID,
                "recovery_carrier_id": cid,
                "missing_tracks": [{"track": 6, "title": "Last", "recording_id": "rec-6"}],
            })
        # Fail closed: create_carrier was NOT called over the existing row.
        create_spy.assert_not_called()
        row = rc.get_carrier(cid)
        self.assertEqual(row["verification_evidence"], {"acoustid": "proven"})

    # ─────────────────── F4: review hand-off reload-None strand ─────────────
    def test_review_handoff_carrier_reload_none_fails_closed(self):
        """F4: with auto_import=false the carrier folder is excluded from the AI
        batch. If the carrier cannot be reloaded for the Review hand-off, the
        job must NOT return a bare success (which would strand the folder); it
        fails closed to a recoverable-failed state."""
        queue = mock.Mock(return_value=True)
        self._setup([mock.patch.object(acq, "_queue_folder_for_manual_review", queue)])
        # get_carrier returns None everywhere this run: the short-circuit and the
        # create-existence check already see None for a fresh carrier, and the
        # Review hand-off reload now also gets None -- the reload-failed edge.
        # create_carrier / advance_state are NOT patched, so the row is really
        # created and really marked failed; we read it back afterwards.
        with mock.patch.object(acq.recovery_carrier, "get_carrier", return_value=None):
            result, _log = self._run({
                "artist": "Artist", "album": "Album", "year": "2024",
                "method": "ytdlp", "auto_import": False,
                "existing_album_id": 42, "mb_releasegroupid": RGID,
                "missing_tracks": [{"track": 6, "title": "Last", "recording_id": "rec-6"}],
            })
        # Not a bare success: a distinct failure the caller can surface/retry.
        self.assertEqual(result.get("error"), "review_handoff_failed")
        self.assertFalse(result.get("queued_for_review"))
        queue.assert_not_called()  # no structured carrier to hand to Review
        cid = self.jobs.start_python.call_args.kwargs["metadata"]["recovery_carrier_id"]
        self.assertEqual(rc.get_carrier(cid)["state"], "failed")

    # ─────────────────── F5: unknown position is not covered ────────────────
    def test_unknown_position_track_is_not_falsely_already_done(self):
        """F5: a completed carrier whose only track is title-only (unknown
        position) must NOT report a DIFFERENT title-only requested track as
        already imported. Unknown position is UNPROVEN, never COVERED."""
        cid = "abcdef0123456789abcdef0123456789"
        self._setup()
        rc.create_carrier(carrier_id=cid, existing_album_id=42, mb_releasegroupid=RGID,
                          wanted_tracks=[{"title": "Some Other Song"}],
                          expected_tracks=[{"title": "Some Other Song"}],
                          state="verifying")
        rc.advance_state(cid, "imported", checkpoint="IMPORTED")
        result, _log = self._run({
            "artist": "Artist", "album": "Album", "year": "2024",
            "method": "ytdlp", "auto_import": True,
            "existing_album_id": 42, "mb_releasegroupid": RGID,
            "recovery_carrier_id": cid,
            "missing_tracks": [{"title": "The Wanted Song"}],
        })
        self.assertFalse(result.get("resumed"))   # not a false already-done
        self.ytdlp.assert_called()                 # proceeded as a real recovery

    # ─────────────────── F7: RG resolved from mb_albumid too ────────────────
    def test_rg_from_mb_albumid_blocks_sibling_reuse(self):
        """F7: the request supplies the canonical Release Group only via
        mb_albumid (a release-group reference), not mb_releasegroupid. The
        identity cross-check must still see it and REJECT a carrier with a
        different RG -- not fall back to album-only and reuse the sibling."""
        cid = "abcdef0123456789abcdef0123456789"
        self._setup()
        rc.create_carrier(carrier_id=cid, existing_album_id=42, mb_releasegroupid=RGID,
                          release_id=RELEASE,
                          wanted_tracks=[{"track": 6}],
                          expected_tracks=[{"recording_id": "rec-6", "track": 6, "disc": 1}],
                          state="verifying")
        rc.advance_state(cid, "verifying", checkpoint="STAGED",
                         evidence={"acoustid": "proven"})
        result, _log = self._run({
            "artist": "Artist", "album": "Album", "year": "2024",
            "method": "ytdlp", "auto_import": True,
            "existing_album_id": 42,
            # RG supplied ONLY through mb_albumid as a release-group reference.
            "mb_albumid": f"https://musicbrainz.org/release-group/{RGID2}",
            "recovery_carrier_id": cid,
            "missing_tracks": [{"track": 6, "title": "Last", "recording_id": "rec-6"}],
        })
        minted = self.jobs.start_python.call_args.kwargs["metadata"]["recovery_carrier_id"]
        self.assertNotEqual(minted, cid)  # RG mismatch -> fresh, not the sibling
        self.assertEqual(rc.get_carrier(cid)["mb_releasegroupid"], RGID)
        self.assertEqual(rc.get_carrier(cid)["verification_evidence"], {"acoustid": "proven"})

    # ─────────────── F1: different track scope mints a fresh carrier ─────────
    def test_different_track_scope_mints_fresh_carrier(self):
        """F1: a same-album + same-RG resume whose requested track set DIFFERS
        from the stored carrier's is a sibling, not this recovery. It must mint
        a FRESH carrier instead of reusing the sibling's stale expected_tracks."""
        cid = "abcdef0123456789abcdef0123456789"
        self._setup()
        rc.create_carrier(carrier_id=cid, existing_album_id=42, mb_releasegroupid=RGID,
                          release_id=RELEASE,
                          wanted_tracks=[{"track": 6}],
                          expected_tracks=[{"recording_id": "rec-6", "track": 6, "disc": 1}],
                          state="verifying")
        rc.advance_state(cid, "verifying", checkpoint="STAGED",
                         evidence={"acoustid": "proven"})
        result, _log = self._run({
            "artist": "Artist", "album": "Album", "year": "2024",
            "method": "ytdlp", "auto_import": True,
            "existing_album_id": 42, "mb_releasegroupid": RGID,
            "recovery_carrier_id": cid,
            # DIFFERENT track than the carrier covers (3 vs 6).
            "missing_tracks": [{"track": 3, "title": "Third", "recording_id": "rec-3"}],
        })
        minted = self.jobs.start_python.call_args.kwargs["metadata"]["recovery_carrier_id"]
        self.assertTrue(minted)
        self.assertNotEqual(minted, cid)  # fresh id, sibling not reused
        # The sibling's durable evidence/checkpoint survive untouched.
        sibling = rc.get_carrier(cid)
        self.assertEqual(sibling["checkpoint"], "STAGED")
        self.assertEqual(sibling["verification_evidence"], {"acoustid": "proven"})


if __name__ == "__main__":
    unittest.main()
