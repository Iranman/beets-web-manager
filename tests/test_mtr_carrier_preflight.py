"""Single-track-recovery preflight keyed off the durable recovery carrier
(ARCH-030 Phase-2 identity depth). Phase-1 root cause: a one-track download
for a known multi-track album was re-derived from the folder name and judged
against the whole release tracklist, so it failed preflight for "lacking all
album tracks". These tests pin the fix: when a recovery carrier is bound to the
staged folder, `_folder_release_preflight` takes the canonical identity
(existing album + Release Group + expected missing track) from the carrier
store, verifies ONLY the expected track via deterministic per-recording proof,
records the evidence into the carrier, and never lowers a threshold.

Synthetic MusicBrainz/AcoustID data only; no provider or live key is used.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.ai_service as ai
import backend.matching_service as ms
import backend.recovery_carrier as rc
from backend.provider_boundary import ProviderOutcome, ProviderResult

REL = "a0000000-0000-0000-0000-00000000000a"
RG = "a1000000-0000-0000-0000-00000000000a"
OTHER_REL = "b0000000-0000-0000-0000-00000000000b"
OTHER_REC = "d0000000-0000-0000-0000-00000000000d"
# 15-track release (the Phase-1 "9/15" shape): the album already holds 1..14,
# the staged folder holds only the one missing track, #15.
RECS = ["c0000000-0000-0000-0000-0000000000%02d" % i for i in range(1, 16)]
TRACKS = [{"mb_trackid": RECS[i], "title": f"T{i + 1}", "track": i + 1, "disc": 1,
           "duration_ms": 240000} for i in range(15)]
ALBUM_ID = 7
MISSING_N = 15  # the one track being recovered
EXPECTED_MS = 240000  # carrier stores the expected recording duration in MILLISECONDS


def _hit(rec, score=100, duration=None):
    h = {"mb_trackid": rec, "score": score, "mb_albumids": [REL], "mb_releasegroupid": RG}
    if duration is not None:
        h["duration"] = duration  # AcoustID result duration is SECONDS
    return h


class CarrierRecoveryPreflight(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Carrier store isolated to this test (never a live key / live data).
        self._prev_dir = os.environ.get("RECOVERY_CARRIER_STATE_DIR")
        os.environ["RECOVERY_CARRIER_STATE_DIR"] = str(Path(self.tmp) / "rc")
        rc._store_registry.clear()

        self.downloads = Path(self.tmp) / "downloads"
        self.folder = self.downloads / "Some Album - yt missing 1700000000000"
        self.folder.mkdir(parents=True)
        self.file = self.folder / f"{MISSING_N:02d} T{MISSING_N}.flac"
        self.file.write_bytes(b"synthetic")

        # AcoustID answer for the one staged file: by default it confirms the
        # missing recording with an agreeing duration.
        self.hits = {str(self.file): [_hit(RECS[MISSING_N - 1], duration=241.0)]}

        self.album = {"id": ALBUM_ID, "album": "Some Album", "albumartist": "Synthetic Artist",
                      "mb_albumid": REL, "mb_releasegroupid": RG}
        # The album already holds tracks 1..14.
        self.items = [{"id": i + 1, "title": f"T{i + 1}", "track": i + 1, "disc": 1,
                       "mb_trackid": RECS[i], "path": f"/music/a/{i + 1:02d}.flac"}
                      for i in range(14)]

    def tearDown(self):
        rc._store_registry.clear()
        if self._prev_dir is None:
            os.environ.pop("RECOVERY_CARRIER_STATE_DIR", None)
        else:
            os.environ["RECOVERY_CARRIER_STATE_DIR"] = self._prev_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fetch(self, rid, log=None):
        if rid not in (REL, OTHER_REL):
            return {"ok": False, "error": "not found", "tracks": []}
        return {"ok": True, "tracks": [dict(t) for t in TRACKS], "release_title": "Some Album",
                "release_artist": "Synthetic Artist", "release_group": RG}

    def _lookup(self, path):
        hits = self.hits.get(str(path))
        if isinstance(hits, ProviderResult):
            return hits
        if hits is None:
            return ProviderResult("acoustid", ProviderOutcome.UNAVAILABLE, data=[])
        return ProviderResult("acoustid",
                              ProviderOutcome.CONFIRMED if hits else ProviderOutcome.NO_RESULT,
                              data=hits)

    def _make_carrier(self, duration_ms=EXPECTED_MS):
        return rc.create_carrier(
            existing_album_id=ALBUM_ID,
            mb_releasegroupid=RG,
            release_id=REL,
            staged_folder_path=str(self.folder),
            expected_tracks=[{"recording_id": RECS[MISSING_N - 1], "track": MISSING_N,
                              "disc": 1, "title": f"T{MISSING_N}", "duration": duration_ms}],
            state="downloaded",
        )

    def _patches(self):
        cw = ms.composite_workflows
        return [
            mock.patch.object(ms, "DOWNLOADS_ROOT", self.downloads),
            mock.patch.object(ms, "_fetch_mb_release_tracklist", side_effect=self._fetch),
            mock.patch.object(ms, "_acoustid_multi_file", return_value={}),
            mock.patch.object(ms, "_acoustid_lookup_cached_outcome", side_effect=self._lookup),
            mock.patch.object(cw, "get_album", side_effect=lambda aid: dict(self.album)),
            mock.patch.object(cw, "find_all_items_by_album_id",
                              side_effect=lambda aid: [dict(i) for i in self.items]),
        ]

    def _preflight_no_identity(self):
        """Call the preflight the way a folder-only caller does (Phase-1 gap):
        no release id, no existing album, no wanted tracks. The carrier must
        supply all of it."""
        patches = self._patches()
        for p in patches:
            p.start()
        try:
            return ms._folder_release_preflight(str(self.folder), "", log=None)
        finally:
            for p in reversed(patches):
                p.stop()

    # ── the Phase-1 regression ──────────────────────────────────────────────
    def test_single_track_recovery_verified_from_carrier_alone(self):
        cid = self._make_carrier()["carrier_id"]
        pre = self._preflight_no_identity()
        # Identity came entirely from the carrier (nothing was passed in).
        self.assertTrue(pre.get("recovery_mode"))
        self.assertEqual(pre.get("recovery_carrier_id"), cid)
        self.assertTrue(pre["ok"], pre.get("missing_track_proof"))
        # One audio file for a 15-track release is NOT rejected for lacking all
        # tracks: the deterministic per-recording proof authorizes it.
        self.assertEqual(pre["audio_count"], 1)
        self.assertEqual(pre["expected"], 15)
        proof = pre["missing_track_proof"]
        self.assertTrue(proof["ok"])
        self.assertEqual([f["track"] for f in proof["files"]], [MISSING_N])

    def test_verified_recovery_records_carrier_evidence(self):
        cid = self._make_carrier()["carrier_id"]
        self._preflight_no_identity()
        got = rc.get_carrier(cid)
        self.assertEqual(got["state"], "verifying")
        self.assertEqual(got["checkpoint"], "VERIFIED")
        self.assertTrue(got["verification_evidence"]["verified"])
        self.assertEqual(got["verification_evidence"]["stage"], "folder_release_preflight")

    def test_uncertain_recovery_stays_for_review(self):
        cid = self._make_carrier()["carrier_id"]
        self.hits[str(self.file)] = []  # AcoustID answered: no recording confirmed
        pre = self._preflight_no_identity()
        self.assertFalse(pre["ok"])
        self.assertEqual(pre["missing_track_proof"]["reason_code"], "file_unverified")
        got = rc.get_carrier(cid)
        self.assertEqual(got["state"], "review_required")
        self.assertFalse(got["verification_evidence"]["verified"])

    def test_threshold_not_lowered_wrong_recording_refused(self):
        # A file that fingerprints to a recording NOT on the target release is
        # refused even in recovery mode: recovery never bypasses the proof/
        # canonical decision and never relaxes the tracklist bar.
        cid = self._make_carrier()["carrier_id"]
        self.hits[str(self.file)] = [_hit(OTHER_REC, duration=241.0)]
        pre = self._preflight_no_identity()
        self.assertFalse(pre["ok"])
        self.assertIn(pre["missing_track_proof"]["reason_code"],
                      {"recording_not_on_release", "identity_conflict"})
        self.assertEqual(rc.get_carrier(cid)["state"], "review_required")

    # ── duration unit (ms) pin ──────────────────────────────────────────────
    def test_duration_tolerance_unit_is_ms_vs_seconds(self):
        # 240000 ms == 240 s. A measured 240 s file agrees; a 200 s file does
        # not; and a value that forgot ms->s conversion (240000 "seconds")
        # must be rejected, not silently accepted.
        self.assertTrue(ms._recovery_duration_tolerance_ok(240000, 240.0))
        self.assertTrue(ms._recovery_duration_tolerance_ok(240000, 241.0))
        self.assertFalse(ms._recovery_duration_tolerance_ok(240000, 200.0))
        self.assertFalse(ms._recovery_duration_tolerance_ok(240000, 240000.0))
        # Unknown on either side: no opinion (never a manufactured mismatch).
        self.assertTrue(ms._recovery_duration_tolerance_ok(0, 240.0))
        self.assertTrue(ms._recovery_duration_tolerance_ok(240000, 0))

    def test_carrier_stores_expected_duration_in_milliseconds(self):
        # Guards the cross-agent contract: expected_tracks[].duration is ms.
        car = self._make_carrier(duration_ms=EXPECTED_MS)
        self.assertEqual(car["expected_tracks"][0]["duration"], EXPECTED_MS)
        self.assertGreater(car["expected_tracks"][0]["duration"], 1000)  # ms, not seconds

    def test_confirmed_recording_with_wrong_length_goes_to_review(self):
        cid = self._make_carrier()["carrier_id"]
        # AcoustID confirms the right recording, but the staged file is ~40s
        # short (240000 ms expected vs 200 s measured): a truncated/wrong copy.
        self.hits[str(self.file)] = [_hit(RECS[MISSING_N - 1], duration=200.0)]
        pre = self._preflight_no_identity()
        self.assertFalse(pre["ok"])
        self.assertEqual(pre["missing_track_proof"]["reason_code"], "duration_conflict")
        self.assertEqual(rc.get_carrier(cid)["state"], "review_required")

    def test_duration_conflict_is_a_known_refusal_reason(self):
        self.assertIn("duration_conflict", ms.IMPORT_REFUSAL_REASONS)

    def test_no_carrier_no_recovery_mode(self):
        # Without a carrier the folder-only call behaves as before (no identity
        # -> no recovery mode, not ok).
        pre = self._preflight_no_identity()
        self.assertFalse(pre.get("recovery_mode", False))
        self.assertFalse(pre["ok"])


class AiBatchCarrierSkip(unittest.TestCase):
    """HOOK 2: a carrier-bound folder is a single-track recovery for a KNOWN
    album; the generic new-album batch scan must consume the carrier and leave
    the folder out instead of re-deriving identity from the folder name."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = Path(self.tmp) / "scan"
        self.recovery = self.base / "Some Album - yt missing 1700000000000"
        self.new_album = self.base / "Brand New Album"
        for d in (self.recovery, self.new_album):
            d.mkdir(parents=True)
            (d / "01 Track.flac").write_bytes(b"x")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_recovery_carrier_folder_excluded_from_new_album_scan(self):
        carrier = {"carrier_id": "cid1", "existing_album_id": ALBUM_ID, "mb_releasegroupid": RG}

        def _load(folder):
            return carrier if Path(folder) == self.recovery else None

        patches = [
            mock.patch.object(ai, "_resolve_import_scan_root", return_value=(self.base, None)),
            mock.patch.object(ai, "_app_managed_download_path", return_value=False),
            mock.patch.object(ai, "_resolve_import_review_source_path",
                              side_effect=lambda f, **k: (Path(f), None)),
            mock.patch.object(rc, "load_for_folder", side_effect=_load),
        ]
        for p in patches:
            p.start()
        try:
            log = []
            out = ai._ai_batch_find_audio_dirs(str(self.base), log)
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertIn(str(self.new_album), out)
        self.assertNotIn(str(self.recovery), out)
        self.assertTrue(any("recovery carrier folder skipped" in line for line in log))


if __name__ == "__main__":
    unittest.main()
