"""ARCH-030 F7ai (req-4/req-6): the AcoustID release-id shortcut in
``_ai_suggest_folder_internal`` must never surface a candidate UNRELATED to the
carrier's target Release Group as the PRIMARY suggestion.

The relatedness veto above the shortcut only SORTS the candidate pool; the
shortcut looks its winner up by release id, so without the guard a foreign
fingerprint-matched release (a cross-RG collision / injected fingerprint) could
still become primary. These tests prove the guard refuses the shortcut to an
unrelated winner (falling back to the related candidate) while still taking it
for a related winner.

Synthetic identity only; no network, no OpenAI call (AI key forced empty).
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.ai_service as ai_service
import backend.recovery_carrier as recovery_carrier

RG_112 = "37cb4a72-1111-1111-1111-111111111111"       # carrier target RG
REL_112 = "083b9009-2222-2222-2222-222222222222"
RG_FLEUR = "2da91738-3333-3333-3333-333333333333"      # unrelated RG
REL_FLEUR = "4e4e4e4e-5555-5555-5555-555555555555"

_CAND_112 = {"mb_albumid": REL_112, "mb_releasegroupid": RG_112, "album": "Forever",
             "artist": "112", "year": "1996", "tracks": 9, "country": "US",
             "score": 100, "label": "Bad Boy", "formats": ["CD"]}
_CAND_FLEUR = {"mb_albumid": REL_FLEUR, "mb_releasegroupid": RG_FLEUR,
               "album": "FOREVER FOREVER", "artist": "FLEUR", "year": "2021",
               "tracks": 9, "country": "US", "score": 100, "label": "x",
               "formats": ["CD"]}


class F7aiUnrelatedShortcutVeto(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def _run(self, *, candidates, acoustid_hits, carrier_rg=RG_112):
        ev = {"audio_files": [str(self.folder / "01.flac")], "folder_track_count": 2,
              "nested_audio_count": 0, "guessed_artist": "112", "guessed_artist_mbid": "",
              "guessed_album": "Forever", "guessed_year": "1996",
              "track_lines": [], "filenames": ["01.flac"]}
        carrier = {"mb_releasegroupid": carrier_rg} if carrier_rg else None
        patches = [
            mock.patch.object(ai_service, "_ai_api_key", return_value=""),  # no OpenAI call
            mock.patch.object(ai_service, "_build_folder_evidence", return_value=ev),
            mock.patch.object(ai_service, "_resolve_import_review_source_path",
                              return_value=(self.folder, None)),
            mock.patch.object(ai_service, "_acoustid_multi_file", return_value=dict(acoustid_hits)),
            mock.patch.object(ai_service, "_acoustid_lookup_cached_outcome", return_value=None),
            mock.patch.object(ai_service, "_mb_release_search",
                              return_value=[dict(c) for c in candidates]),
            mock.patch.object(ai_service, "_mb_release_search_by_folder_tracks", return_value=[]),
            mock.patch.object(ai_service, "_fetch_mb_release_candidate", return_value={}),
            mock.patch.object(ai_service, "_score_mb_release_candidate",
                              side_effect=lambda ev, c: {"total": 0.9}),
            mock.patch.object(ai_service, "_run_ai_release_preflight", return_value=None),
            mock.patch.object(ai_service, "_apply_ai_preflight_to_suggestion",
                              side_effect=lambda sug, pf: None),
            mock.patch.object(ai_service, "_ai_match_evidence_packet", return_value={}),
            mock.patch.object(recovery_carrier, "load_for_folder", return_value=carrier),
        ]
        for p in patches:
            p.start()
        try:
            return ai_service._ai_suggest_folder_internal(str(self.folder))
        finally:
            for p in reversed(patches):
                p.stop()

    def test_unrelated_fingerprint_winner_is_not_primary(self):
        """FLEUR has the top fingerprint hits, but it is UNRELATED to the carrier
        RG -> it must not win the shortcut and become primary."""
        out = self._run(candidates=[_CAND_FLEUR, _CAND_112],
                        acoustid_hits={REL_FLEUR: 3})
        self.assertTrue(out["ok"], out)
        sug = out["suggestion"]
        self.assertNotEqual(sug.get("mb_albumid"), REL_FLEUR,
                            "an unrelated fingerprint winner must never be the primary suggestion")
        self.assertEqual(sug.get("mb_albumid"), REL_112,
                         "primary should fall back to the related carrier-RG candidate")

    def test_related_fingerprint_winner_still_takes_the_shortcut(self):
        """A related winner is unchanged: the shortcut still surfaces it."""
        out = self._run(candidates=[_CAND_112, _CAND_FLEUR],
                        acoustid_hits={REL_112: 3})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["suggestion"].get("mb_albumid"), REL_112)
        self.assertIn("AcoustID", out["suggestion"].get("reason", ""))

    def test_no_carrier_leaves_shortcut_unchanged(self):
        """Without a carrier RG the veto is a no-op; the fingerprint winner
        takes the shortcut as before (text-only ranking unchanged)."""
        out = self._run(candidates=[_CAND_FLEUR, _CAND_112],
                        acoustid_hits={REL_FLEUR: 3}, carrier_rg="")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["suggestion"].get("mb_albumid"), REL_FLEUR)


if __name__ == "__main__":
    unittest.main()
