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
ART_112 = "a1b2c3d4-4444-4444-4444-444444444444"
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
                "release_artist": "112", "release_artist_id": ART_112, "release_group": RG_112}

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

    # ── Option C: a local-scan recovery with NO embedded tags fails closed ──
    def test_no_result_local_scan_without_embedded_tags_goes_to_review(self):
        # The pure local-scan path cannot read the files' embedded MB tags and
        # does not know their ORIGINAL pre-rename download names, so Option C's
        # double-corroboration is unmet: a genuine NO_RESULT recovery here must
        # fail closed to manual review -- the intended safe behavior. Auto-
        # accept on this feature now requires integrations to supply
        # file_original_names + file_embedded_ids (see
        # RecoverySlotWiringWithCorroboration below).
        self._make_carrier()
        pre = self._preflight()
        self.assertTrue(pre.get("recovery_mode"))
        self.assertFalse(pre["ok"], pre.get("missing_track_proof"))
        self.assertEqual(pre["missing_track_proof"]["reason_code"], "file_unverified")
        self.assertNotEqual(pre["matching_decision"].get("identity_proof"),
                            IdentityProof.DETERMINISTIC_METADATA_SLOT.value)

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


class EmbeddedContradictionBlocksPreflightSlot(unittest.TestCase):
    """F5/C2 (ARCH-030): the F7 embedded-contradiction guard must be EFFECTIVE
    on the real preflight path, not just the contract. The preflight candidate
    rows used to drop the file's embedded Release-Group/artist identity, so a
    mistagged staged file self-identifying as a DIFFERENT Release Group could
    never trip F7. With the IDs threaded into ``_add_candidate``, an engine-
    inspected file whose embedded RG contradicts the carrier target is a
    conflict and the preflight refuses to auto-accept (identity_conflict),
    never a silent slot.

    Driven through the engine-inspection branch (no local media mount), which
    is where embedded tag identity is actually available; synthetic data only.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._prev_dir = os.environ.get("RECOVERY_CARRIER_STATE_DIR")
        os.environ["RECOVERY_CARRIER_STATE_DIR"] = str(Path(self.tmp) / "rc")
        rc._store_registry.clear()
        # Folder OUTSIDE music/downloads roots => the local walk finds nothing
        # and the preflight falls through to the engine-inspection branch.
        self.empty_downloads = Path(self.tmp) / "downloads_empty"
        self.empty_downloads.mkdir(parents=True)
        self.folder = Path(self.tmp) / "staged" / "112 - Forever - yt missing 170"
        self.folder.mkdir(parents=True)
        self.album = {"id": ALBUM_ID, "album": "Forever", "albumartist": "112",
                      "mb_albumid": REL_112, "mb_releasegroupid": RG_112}
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
                "release_artist": "112", "release_artist_id": ART_112, "release_group": RG_112}

    def _make_carrier(self):
        return rc.create_carrier(
            existing_album_id=ALBUM_ID, mb_releasegroupid=RG_112, release_id=REL_112,
            staged_folder_path=str(self.folder),
            expected_tracks=[
                {"recording_id": REC[0], "track": 1, "disc": 1, "title": "Forever (Intro)", "duration": 42000},
            ],
            state="downloaded",
        )["carrier_id"]

    def _inspect(self, embedded_rg):
        # Engine inspection of the staged intro file: a genuine NO_RESULT slot
        # on title/position/duration, but the file carries an embedded RG tag.
        return {
            "ok": True,
            "path": str(self.folder),
            "audio_files": [
                {"relative_path": "01 - Forever (Intro).flac",
                 "acoustid_hits": [],
                 "properties": {"title": "Forever (Intro)", "track": 1, "disc": 1,
                                "length": 42.0, "mb_releasegroupid": embedded_rg}},
            ],
        }

    def _preflight(self, embedded_rg):
        cw = ms.composite_workflows
        patches = [
            mock.patch.object(ms, "DOWNLOADS_ROOT", self.empty_downloads),
            mock.patch.object(ms, "_fetch_mb_release_tracklist", side_effect=self._fetch),
            mock.patch.object(ms, "_acoustid_multi_file", return_value={}),
            mock.patch.object(cw, "inspect_import_source",
                              side_effect=lambda *a, **k: self._inspect(embedded_rg)),
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

    def test_embedded_rg_contradiction_refuses_auto_accept(self):
        self._make_carrier()
        pre = self._preflight(embedded_rg=RG_FLEUR)
        self.assertTrue(pre.get("recovery_mode"))
        self.assertFalse(pre["ok"], "a file self-identifying as a foreign RG must not auto-accept")
        self.assertIn("file_release_group_conflict",
                      (pre.get("matching_decision") or {}).get("conflicts") or [])

    def test_embedded_rg_agreement_is_not_a_conflict(self):
        # Control: the SAME inspection with an AGREEING embedded RG does not
        # manufacture a conflict (F7 detects contradiction only).
        self._make_carrier()
        pre = self._preflight(embedded_rg=RG_112)
        self.assertNotIn("file_release_group_conflict",
                         (pre.get("matching_decision") or {}).get("conflicts") or [])


class ProofDoubleCorroborationTests(unittest.TestCase):
    """Option C (USER-chosen gate) at the proof level: `_missing_track_
    recording_proof` auto-accepts a genuine NO_RESULT recovery for the wanted
    subset ONLY when integrations supplies BOTH halves of the corroboration,
    keyed by staged path under the pinned contract field names:

      - file_original_names[path]            -> original pre-rename download basename
      - file_embedded_ids[path]              -> {"title","mb_releasegroupid",
                                                 "mb_albumartistid","mb_artistid"}

    The original-name title AND the embedded title must both match the target,
    and the embedded Release-Group + artist MBID must be present and match. If
    either half is absent or disagrees, the slot fails closed to review; a
    contradicting embedded artist MBID is a hard conflict. Synthetic data only.
    """

    ALBUM = {"mb_releasegroupid": RG_112, "mb_albumid": REL_112,
             "album": "Forever", "albumartist": "112"}
    INTRO = "/staged/112 - Forever/01 - Forever (Intro).flac"
    OUTRO = "/staged/112 - Forever/09 - For Us (Outro).flac"

    def _wanted(self):
        return [{"mb_trackid": REC[0], "track": 1, "disc": 1},
                {"mb_trackid": REC[8], "track": 9, "disc": 1}]

    def _existing(self):
        # The album already holds tracks 2..8.
        return {REC[i] for i in range(1, 8)}

    def _proof(self, *, file_original_names, file_embedded_ids):
        return ms._missing_track_recording_proof(
            [(self.INTRO, []), (self.OUTRO, [])],  # both answered empty => NO_RESULT
            mb_tracks=_mb_tracks(),
            wanted_tracks=self._wanted(),
            existing_recordings=self._existing(),
            album_row=dict(self.ALBUM),
            release_id=REL_112,
            release_group_id=RG_112,
            release_title="Forever",
            release_artist="112",
            release_artist_id=ART_112,
            file_durations={self.INTRO: 42.0, self.OUTRO: 55.0},
            file_original_names=file_original_names,
            file_embedded_ids=file_embedded_ids,
        )

    def _genuine_names(self):
        return {self.INTRO: "Forever (Intro).flac", self.OUTRO: "For Us (Outro).flac"}

    def _genuine_embedded(self):
        # Genuine embedded tags carry an INDEPENDENT disc/track (F1-POSITION):
        # a real release file is tagged with its position, which corroborates the
        # target slot WITHOUT trusting the circular staged filename.
        return {
            self.INTRO: {"title": "Forever (Intro)", "mb_releasegroupid": RG_112,
                         "mb_albumartistid": ART_112, "track": "1", "disc": "1"},
            self.OUTRO: {"title": "For Us (Outro)", "mb_releasegroupid": RG_112,
                         "mb_albumartistid": ART_112, "track": "9", "disc": "1"},
        }

    def test_fully_corroborated_auto_accepts_wanted_subset(self):
        proof = self._proof(file_original_names=self._genuine_names(),
                            file_embedded_ids=self._genuine_embedded())
        self.assertTrue(proof["ok"], proof)
        self.assertEqual(sorted(f["track"] for f in proof["files"]), [1, 9])
        self.assertEqual(sorted(f["recording_id"] for f in proof["files"]),
                         sorted([REC[0], REC[8]]))
        self.assertEqual(proof["matching_decision"]["identity_proof"],
                         IdentityProof.DETERMINISTIC_METADATA_SLOT.value)

    def test_absent_embedded_tags_go_to_review(self):
        # Fresh download, no readable MB tags: the intended safe behavior.
        proof = self._proof(file_original_names=self._genuine_names(), file_embedded_ids={})
        self.assertFalse(proof["ok"])
        self.assertEqual(proof["reason_code"], "file_unverified")

    def test_absent_original_name_goes_to_review(self):
        proof = self._proof(file_original_names={}, file_embedded_ids=self._genuine_embedded())
        self.assertFalse(proof["ok"])
        self.assertEqual(proof["reason_code"], "file_unverified")

    def test_version_launder_bare_name_vs_live_embedded_blocked(self):
        # F4: a bare ORIGINAL name but a version-bearing embedded/authoritative
        # title -> the two corroborators disagree on the version marker -> the
        # track-number strip cannot launder the live take into the bare slot.
        names = dict(self._genuine_names())
        names[self.INTRO] = "01 Forever.flac"  # bare, no version marker
        embedded = dict(self._genuine_embedded())
        embedded[self.INTRO] = {"title": "Forever (Intro) (Live 2019)",
                                "mb_releasegroupid": RG_112, "mb_albumartistid": ART_112}
        proof = self._proof(file_original_names=names, file_embedded_ids=embedded)
        self.assertFalse(proof["ok"], "a version-marker disagreement must not auto-accept")
        self.assertEqual(proof["reason_code"], "file_unverified")

    def test_contradicting_embedded_artist_is_conflict(self):
        embedded = dict(self._genuine_embedded())
        embedded[self.INTRO] = {"title": "Forever (Intro)", "mb_releasegroupid": RG_112,
                                "mb_albumartistid": "ffff9999-0000-0000-0000-000000000000",
                                "track": "1", "disc": "1"}
        proof = self._proof(file_original_names=self._genuine_names(), file_embedded_ids=embedded)
        self.assertFalse(proof["ok"])
        self.assertEqual(proof["reason_code"], "identity_conflict")

    def test_contradicting_embedded_rg_is_conflict(self):
        embedded = dict(self._genuine_embedded())
        embedded[self.INTRO] = {"title": "Forever (Intro)", "mb_releasegroupid": RG_FLEUR,
                                "mb_albumartistid": ART_112, "track": "1", "disc": "1"}
        proof = self._proof(file_original_names=self._genuine_names(), file_embedded_ids=embedded)
        self.assertFalse(proof["ok"])
        self.assertEqual(proof["reason_code"], "identity_conflict")


class ContractIdentityGuards(unittest.TestCase):
    """Contract-level foils the recovery authority must agree with."""

    def _current(self):
        return {"album": "Forever", "artist": "112", "mb_releasegroupid": RG_112,
                "mb_albumartistid": ART_112}

    def _candidate(self, rg=RG_112, rel=REL_112):
        return {"mb_releasegroupid": rg, "mb_albumid": rel, "album": "Forever",
                "artist": "112", "mb_albumartistid": ART_112}

    def _intro_item(self, **extra):
        # Option C: a genuine slot carries its ORIGINAL download name and
        # embedded title/artist (embedded RG supplied per-test via extra's
        # mb_releasegroupid), all corroborating the target.
        row = {"title": "Forever (Intro)", "artist": "112", "disc": 1, "track": 1,
               "length": 42.0, "acoustid_hits": [],
               "original_name": "01 Forever (Intro).flac",
               "embedded_title": "Forever (Intro)",
               "embedded_artist_id": ART_112}
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
