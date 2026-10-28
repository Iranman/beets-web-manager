"""ARCH-030 slot-evidence WIRING: the Option-C corroborators reach the proof on
the REAL production recovery path.

The gap this closes: `_missing_track_recording_proof` already consumes
`file_original_names` / `file_embedded_ids`, but on the production
`_folder_release_preflight` local-scan path NOTHING populated them (they were
built from candidate dict fields that nothing stamps), so every recovery --
including a genuinely corroborated one -- fail-closed to manual review and the
feature never auto-completed. The fix sources those two maps from the
integrations folder readers (`slskd.original_download_names_for_folder` reading
the on-disk sidecar, `acoustid_service.embedded_identity_map_for_folder` reading
each file's embedded MB tags), keyed to the SAME staged abs paths as the proof's
per-file rows.

These tests drive the REAL preflight against a staged folder ON DISK with a REAL
sidecar file and (only) the mutagen tag-read + provider I/O stubbed -- NOT hand-
built candidate dicts. That is deliberate: the isolated-candidate tests are
exactly what let this wiring gap ship green twice. Assertions are at the verdict
level (`pre["ok"]` / `action_allowed`).

Anchor (ADR-0007): 112 "Forever" (RG 37cb4a72), 9 tracks, missing #1 "Forever
(Intro)" and #9 "For Us (Outro)". Synthetic MusicBrainz/AcoustID/tag data only;
no provider or live key is used.
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.acoustid_service as acoustid_service
import backend.matching_service as ms
import backend.recovery_carrier as rc
from backend.matching import IdentityProof
from backend.provider_boundary import ProviderOutcome, ProviderResult
from backend.slskd import RECOVERY_ORIGINAL_NAMES_SIDECAR

RG_112 = "37cb4a72-1111-1111-1111-111111111111"
REL_112 = "083b9009-2222-2222-2222-222222222222"
ART_112 = "a1b2c3d4-4444-4444-4444-444444444444"
RG_FLEUR = "2da91738-3333-3333-3333-333333333333"

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


class SlotEvidenceWiringE2E(unittest.TestCase):
    """Drive `_folder_release_preflight` end to end; the readers run for real
    against the on-disk staged folder (the sidecar is a real file), with only
    the mutagen tag-read and the AcoustID lookup stubbed."""

    STAGED_INTRO = "01 - Forever (Intro).flac"
    STAGED_OUTRO = "09 - For Us (Outro).flac"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._prev_dir = os.environ.get("RECOVERY_CARRIER_STATE_DIR")
        os.environ["RECOVERY_CARRIER_STATE_DIR"] = str(Path(self.tmp) / "rc")
        rc._store_registry.clear()

        self.downloads = Path(self.tmp) / "downloads"
        self.folder = self.downloads / "112 - Forever - yt missing 1700000000000"
        self.folder.mkdir(parents=True)
        self.intro = self.folder / self.STAGED_INTRO
        self.outro = self.folder / self.STAGED_OUTRO
        for f in (self.intro, self.outro):
            f.write_bytes(b"synthetic")

        # Both recovered files answer NO_RESULT with a measured fpcalc duration
        # matching their target slot (the only duration a genuine NO_RESULT file
        # can offer the DETERMINISTIC_METADATA_SLOT tier).
        self.outcomes = {
            str(self.intro): ProviderResult("acoustid", ProviderOutcome.NO_RESULT, data=[],
                                            evidence={"duration": 42.0, "fingerprintable": True}),
            str(self.outro): ProviderResult("acoustid", ProviderOutcome.NO_RESULT, data=[],
                                            evidence={"duration": 55.0, "fingerprintable": True}),
        }

        self.album = {"id": ALBUM_ID, "album": "Forever", "albumartist": "112",
                      "mb_albumid": REL_112, "mb_releasegroupid": RG_112}
        self.items = [{"id": i + 1, "title": _112[i][0], "track": i + 1, "disc": 1,
                       "mb_trackid": REC[i], "path": f"/music/112/{i + 1:02d}.flac"}
                      for i in range(1, 8)]

        # Per-staged-file embedded MB tags, keyed by basename; the mutagen read
        # is the only file I/O stubbed. Default = genuine, fully corroborating.
        self.embedded = {
            self.STAGED_INTRO: {"title": "Forever (Intro)", "mb_releasegroupid": RG_112,
                                "mb_albumartistid": ART_112, "mb_artistid": ART_112},
            self.STAGED_OUTRO: {"title": "For Us (Outro)", "mb_releasegroupid": RG_112,
                                "mb_albumartistid": ART_112, "mb_artistid": ART_112},
        }
        # Real on-disk sidecar: staged relative name -> original pre-rename
        # download basename. Default = genuine, corroborating the target title.
        self.sidecar = {
            self.STAGED_INTRO: "Forever (Intro).flac",
            self.STAGED_OUTRO: "For Us (Outro).flac",
        }

    def tearDown(self):
        rc._store_registry.clear()
        if self._prev_dir is None:
            os.environ.pop("RECOVERY_CARRIER_STATE_DIR", None)
        else:
            os.environ["RECOVERY_CARRIER_STATE_DIR"] = self._prev_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_sidecar(self):
        (self.folder / RECOVERY_ORIGINAL_NAMES_SIDECAR).write_text(
            json.dumps(self.sidecar, ensure_ascii=False), encoding="utf-8")

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

    def _fetch(self, rid, log=None):
        if rid != REL_112:
            return {"ok": False, "error": "not found", "tracks": []}
        return {"ok": True, "tracks": _mb_tracks(), "release_title": "Forever",
                "release_artist": "112", "release_artist_id": ART_112, "release_group": RG_112}

    def _lookup(self, path):
        return self.outcomes.get(str(path),
                                 ProviderResult("acoustid", ProviderOutcome.UNAVAILABLE, data=[]))

    def _read_tags(self, path):
        """Stand-in for the mutagen read: return this file's embedded tags.

        The real `embedded_identity_map_for_folder` / `embedded_identity_for_file`
        wiring runs on top of this -- including the "no MB id -> {}" rule."""
        return dict(self.embedded.get(Path(str(path)).name, {}))

    def _preflight(self):
        cw = ms.composite_workflows
        patches = [
            mock.patch.object(ms, "DOWNLOADS_ROOT", self.downloads),
            mock.patch.object(ms, "_fetch_mb_release_tracklist", side_effect=self._fetch),
            mock.patch.object(ms, "_acoustid_multi_file", return_value={}),
            mock.patch.object(ms, "_acoustid_lookup_cached_outcome", side_effect=self._lookup),
            mock.patch.object(acoustid_service, "_read_file_media_tags", side_effect=self._read_tags),
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

    # (a) genuine recovery: original download name AND embedded tags both
    #     corroborate the target -> slot proven -> auto-accept.
    def test_a_fully_corroborated_recovery_auto_accepts(self):
        self._make_carrier()
        self._write_sidecar()
        pre = self._preflight()
        self.assertTrue(pre.get("recovery_mode"))
        self.assertTrue(pre["ok"], pre.get("missing_track_proof"))
        decision = pre.get("matching_decision") or {}
        self.assertTrue(decision.get("action_allowed"), decision)
        self.assertEqual(decision.get("identity_proof"),
                         IdentityProof.DETERMINISTIC_METADATA_SLOT.value)
        self.assertTrue(pre["missing_track_proof"]["ok"])
        self.assertEqual(sorted(f["track"] for f in pre["missing_track_proof"]["files"]),
                         [1, 9])

    # (b) launder: the authoritative/embedded title carries a version marker the
    #     target lacks ("Forever (Intro) (Live 2019)" vs "Forever (Intro)") -> no
    #     exact-title slot -> REVIEW, never auto-accept.
    def test_b_version_laundered_embedded_title_goes_to_review(self):
        self._make_carrier()
        self.embedded[self.STAGED_INTRO] = {
            "title": "Forever (Intro) (Live 2019)", "mb_releasegroupid": RG_112,
            "mb_albumartistid": ART_112, "mb_artistid": ART_112}
        self._write_sidecar()
        pre = self._preflight()
        self.assertTrue(pre.get("recovery_mode"))
        self.assertFalse(pre["ok"], "a version-marker launder must not auto-accept")
        self.assertFalse((pre.get("matching_decision") or {}).get("action_allowed"))
        self.assertEqual(pre["missing_track_proof"]["reason_code"], "file_unverified")

    # (c) no embedded MB tags (sidecar present): cannot double-corroborate ->
    #     REVIEW. embedded_identity_for_file returns {} when no MB id is readable.
    def test_c_no_embedded_mb_tags_goes_to_review(self):
        self._make_carrier()
        self.embedded = {  # a bare title, no MB identifiers -> reader yields {}
            self.STAGED_INTRO: {"title": "Forever (Intro)"},
            self.STAGED_OUTRO: {"title": "For Us (Outro)"},
        }
        self._write_sidecar()
        pre = self._preflight()
        self.assertTrue(pre.get("recovery_mode"))
        self.assertFalse(pre["ok"], "no embedded MB evidence must fail closed to review")
        self.assertFalse((pre.get("matching_decision") or {}).get("action_allowed"))
        self.assertEqual(pre["missing_track_proof"]["reason_code"], "file_unverified")

    # (d) contradicting embedded Release-Group MBID -> hard conflict -> REVIEW.
    def test_d_contradicting_embedded_rg_is_conflict(self):
        self._make_carrier()
        self.embedded[self.STAGED_INTRO] = {
            "title": "Forever (Intro)", "mb_releasegroupid": RG_FLEUR,
            "mb_albumartistid": ART_112, "mb_artistid": ART_112}
        self._write_sidecar()
        pre = self._preflight()
        self.assertTrue(pre.get("recovery_mode"))
        self.assertFalse(pre["ok"], "a file self-identifying as a foreign RG must not auto-accept")
        self.assertFalse((pre.get("matching_decision") or {}).get("action_allowed"))
        self.assertEqual(pre["missing_track_proof"]["reason_code"], "identity_conflict")

    # Guard: the genuine (a) case only auto-accepts because the readers are wired
    # in. With no sidecar (readers yield {} for the original names), the same
    # corroborating embedded tags are not enough -> REVIEW. This is the exact
    # production symptom the fix removes, asserted from the other direction.
    def test_missing_sidecar_alone_blocks_auto_accept(self):
        self._make_carrier()
        # embedded present & genuine, but NO sidecar written.
        pre = self._preflight()
        self.assertFalse(pre["ok"])
        self.assertEqual(pre["missing_track_proof"]["reason_code"], "file_unverified")


if __name__ == "__main__":
    unittest.main()
