"""ARCH-030 Step 6: the DETERMINISTIC_METADATA_SLOT tier is CONSUMED by the real
recovery insert authority (`_folder_release_preflight` -> `_missing_track_
recording_proof`), not just by the contract.

Anchor (ADR-0007): 112 "Forever" (RG 37cb4a72), 9 tracks, missing #1
"Forever (Intro)" and #9 "For Us (Outro)". The two recovered files fingerprint
to AcoustID NO_RESULT (genuinely absent, not an outage) with a measured
duration matching the target slot. Before Step 6 the proof required a confirmed
AcoustID recording per file, so a genuine NO_RESULT recovery stalled in review
forever (QA FAIL F2). These tests drive the REAL preflight (identity/matching
logic unmocked; only provider I/O and the Beets DB are synthetic) and prove:

- the NO_RESULT recovery is authorized for ONLY the wanted subset (F2);
- UNAVAILABLE / CONFLICT / AMBIGUOUS each still refuse, with no slot (fingerprint
  gate not weakened);
- a file self-identifying as an unrelated Release Group (FLEUR) is a conflict,
  never action_allowed=True (F7).

Synthetic MusicBrainz/AcoustID data only; no provider or live key is used.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.matching_service as ms
import backend.recovery_carrier as rc
from backend.matching import IdentityProof
from backend.matching_contract import build_album_matching_decision
from backend.provider_boundary import ProviderOutcome, ProviderResult

RG_112 = "37cb4a72-1111-1111-1111-111111111111"
REL_112 = "083b9009-2222-2222-2222-222222222222"
RG_FLEUR = "2da91738-3333-3333-3333-333333333333"
OTHER_REC = "d0000000-0000-0000-0000-00000000000d"

# 112 "Forever": (title, seconds). #1 intro and #9 outro are the recovered slots.
_112 = [
    ("Forever (Intro)", 42),
    ("Track 2", 255), ("Track 3", 240), ("Track 4", 233), ("Track 5", 262),
    ("Track 6", 228), ("Track 7", 219), ("Track 8", 250),
    ("For Us (Outro)", 55),
]
REC = [f"11200000-0000-0000-0000-0000000000{i:02d}" for i in range(1, 10)]
ALBUM_ID = 11


def _mb_tracks():
    return [{"mb_trackid": REC[i], "title": t, "track": i + 1, "disc": 1,
             "duration_ms": s * 1000} for i, (t, s) in enumerate(_112)]


def _hit(rec, score=100):
    return {"mb_trackid": rec, "score": score, "mb_albumids": [REL_112], "mb_releasegroupid": RG_112}


class RecoverySlotWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._prev_dir = os.environ.get("RECOVERY_CARRIER_STATE_DIR")
        os.environ["RECOVERY_CARRIER_STATE_DIR"] = str(Path(self.tmp) / "rc")
        rc._store_registry.clear()

        self.downloads = Path(self.tmp) / "downloads"
        self.folder = self.downloads / "112 - Forever - yt missing 1700000000000"
        self.folder.mkdir(parents=True)
        self.intro = self.folder / "01 - Forever (Intro).flac"
        self.outro = self.folder / "09 - For Us (Outro).flac"
        for f in (self.intro, self.outro):
            f.write_bytes(b"synthetic")

        # By default both recovered files answer NO_RESULT with a measured
        # fpcalc duration that matches their target slot.
        self.outcomes = {
            str(self.intro): ProviderResult("acoustid", ProviderOutcome.NO_RESULT, data=[],
                                            evidence={"duration": 42.0, "fingerprintable": True}),
            str(self.outro): ProviderResult("acoustid", ProviderOutcome.NO_RESULT, data=[],
                                            evidence={"duration": 55.0, "fingerprintable": True}),
        }

        self.album = {"id": ALBUM_ID, "album": "Forever", "albumartist": "112",
                      "mb_albumid": REL_112, "mb_releasegroupid": RG_112}
        # The album already holds tracks 2..8 (missing #1 intro and #9 outro).
        self.items = [{"id": i + 1, "title": _112[i][0], "track": i + 1, "disc": 1,
                       "mb_trackid": REC[i], "path": f"/music/112/{i + 1:02d}.flac"}
                      for i in range(1, 8)]

    def tearDown(self):
        rc._store_registry.clear()
        if self._prev_dir is None:
            os.environ.pop("RECOVERY_CARRIER_STATE_DIR", None)
        else:
            os.environ["RECOVERY_CARRIER_STATE_DIR"] = self._prev_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fetch(self, rid, log=None):
        if rid != REL_112:
            return {"ok": False, "error": "not found", "tracks": []}
        return {"ok": True, "tracks": _mb_tracks(), "release_title": "Forever",
                "release_artist": "112", "release_group": RG_112}

    def _lookup(self, path):
        return self.outcomes.get(str(path),
                                 ProviderResult("acoustid", ProviderOutcome.UNAVAILABLE, data=[]))

    def _make_carrier(self):
        return rc.create_carrier(
            existing_album_id=ALBUM_ID, mb_releasegroupid=RG_112, release_id=REL_112,
            staged_folder_path=str(self.folder),
            expected_tracks=[
                {"recording_id": REC[0], "track": 1, "disc": 1, "title": "Forever (Intro)", "duration": 42000},
                {"recording_id": REC[8], "track": 9, "disc": 1, "title": "For Us (Outro)", "duration": 55000},
            ],
            state="downloaded",
        )["carrier_id"]

    def _preflight(self):
        cw = ms.composite_workflows
        patches = [
            mock.patch.object(ms, "DOWNLOADS_ROOT", self.downloads),
            mock.patch.object(ms, "_fetch_mb_release_tracklist", side_effect=self._fetch),
            mock.patch.object(ms, "_acoustid_multi_file", return_value={}),
            mock.patch.object(ms, "_acoustid_lookup_cached_outcome", side_effect=self._lookup),
            mock.patch.object(cw, "get_album", side_effect=lambda aid: dict(self.album)),
            mock.patch.object(cw, "find_all_items_by_album_id",
                              side_effect=lambda aid: [dict(i) for i in self.items]),
        ]
        for p in patches:
            p.start()
        try:
            return ms._folder_release_preflight(str(self.folder), "", log=None)
        finally:
            for p in reversed(patches):
                p.stop()

    # ── F2: the bug this feature was supposed to fix ────────────────────────
    def test_no_result_recovery_authorized_for_wanted_subset_only(self):
        self._make_carrier()
        pre = self._preflight()
        self.assertTrue(pre.get("recovery_mode"))
        self.assertTrue(pre["ok"], pre.get("missing_track_proof"))
        proof = pre["missing_track_proof"]
        self.assertTrue(proof["ok"])
        # ONLY the two wanted missing slots are authorized -- nothing else.
        self.assertEqual(sorted(f["track"] for f in proof["files"]), [1, 9])
        self.assertEqual(sorted(f["recording_id"] for f in proof["files"]),
                         sorted([REC[0], REC[8]]))
        # The proof agrees with the contract's VERIFIED_SUBSET authority and the
        # new tier, and never claims a FULL_RELEASE.
        decision = pre["matching_decision"]
        self.assertTrue(decision["action_allowed"])
        self.assertEqual(decision["identity_proof"], IdentityProof.DETERMINISTIC_METADATA_SLOT.value)

    def test_unavailable_still_refused(self):
        self._make_carrier()
        self.outcomes[str(self.intro)] = ProviderResult("acoustid", ProviderOutcome.UNAVAILABLE, data=[])
        pre = self._preflight()
        self.assertFalse(pre["ok"])
        self.assertEqual(pre["missing_track_proof"]["reason_code"], "acoustid_unavailable")

    def test_conflict_off_release_still_refused(self):
        self._make_carrier()
        # intro file fingerprints to a recording that is NOT on the 112 release.
        self.outcomes[str(self.intro)] = ProviderResult(
            "acoustid", ProviderOutcome.CONFIRMED,
            data=[{"mb_trackid": OTHER_REC, "score": 100, "mb_releasegroupid": RG_FLEUR}],
            evidence={"duration": 42.0, "fingerprintable": True})
        pre = self._preflight()
        self.assertFalse(pre["ok"])
        self.assertIn(pre["missing_track_proof"]["reason_code"],
                      {"recording_not_on_release", "identity_conflict"})

    def test_ambiguous_still_refused(self):
        self._make_carrier()
        # Two close high hits for different on-release recordings => AMBIGUOUS.
        self.outcomes[str(self.intro)] = ProviderResult(
            "acoustid", ProviderOutcome.CONFIRMED,
            data=[_hit(REC[2], 96), _hit(REC[3], 95)],
            evidence={"duration": 42.0, "fingerprintable": True})
        pre = self._preflight()
        self.assertFalse(pre["ok"])
        # An ambiguous fingerprint is refused either at the per-file proof
        # (file_unverified) or, because the colliding title makes the whole-
        # album decision conflict first, as identity_conflict -- never a slot.
        self.assertIn(pre["missing_track_proof"]["reason_code"],
                      {"file_unverified", "identity_conflict"})

    def test_no_result_without_duration_is_not_a_slot(self):
        # A NO_RESULT file with no measured duration fails the ADR-0007 duration
        # signal (fail-closed) and stays in review -- the gate is not loosened.
        self._make_carrier()
        self.outcomes[str(self.intro)] = ProviderResult("acoustid", ProviderOutcome.NO_RESULT, data=[])
        pre = self._preflight()
        self.assertFalse(pre["ok"])
        self.assertEqual(pre["missing_track_proof"]["reason_code"], "file_unverified")


class ContractIdentityGuards(unittest.TestCase):
    """Contract-level foils the recovery authority must agree with."""

    def _current(self):
        return {"album": "Forever", "artist": "112", "mb_releasegroupid": RG_112}

    def _candidate(self, rg=RG_112, rel=REL_112):
        return {"mb_releasegroupid": rg, "mb_albumid": rel, "album": "Forever", "artist": "112"}

    def _intro_item(self, **extra):
        row = {"title": "Forever (Intro)", "artist": "112", "disc": 1, "track": 1,
               "length": 42.0, "acoustid_hits": []}
        row.update(extra)
        return row

    def test_fleur_unrelated_rg_refused(self):
        # FLEUR "FOREVER FOREVER" (RG 2da91738) vs the 112 album: wrong RG.
        decision = build_album_matching_decision(
            current=self._current(),
            candidate=self._candidate(rg=RG_FLEUR, rel="2da91738-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            items=[self._intro_item()], mb_tracks=_mb_tracks(),
        ).to_dict()
        self.assertFalse(decision["action_allowed"])
        self.assertTrue(decision["review_required"])

    def test_f7_file_embedded_unrelated_rg_is_conflict(self):
        # The file self-identifies (embedded tag) as FLEUR while being proposed
        # into the 112 destination: a conflict, never a silent slot.
        decision = build_album_matching_decision(
            current=self._current(), candidate=self._candidate(),
            items=[self._intro_item(mb_releasegroupid=RG_FLEUR)], mb_tracks=_mb_tracks(),
        ).to_dict()
        self.assertFalse(decision["action_allowed"])
        self.assertTrue(decision["review_required"])
        self.assertIn("file_release_group_conflict", decision["conflicts"])

    def test_f7_matching_embedded_rg_still_proves_slot(self):
        # A file whose embedded RG AGREES with the target is unaffected: the
        # NO_RESULT slot still proves (F7 detects contradiction only).
        decision = build_album_matching_decision(
            current=self._current(), candidate=self._candidate(),
            items=[self._intro_item(mb_releasegroupid=RG_112)], mb_tracks=_mb_tracks(),
        ).to_dict()
        self.assertTrue(decision["action_allowed"])
        self.assertEqual(decision["identity_proof"], IdentityProof.DETERMINISTIC_METADATA_SLOT.value)
        self.assertNotIn("file_release_group_conflict", decision["conflicts"])


if __name__ == "__main__":
    unittest.main()
