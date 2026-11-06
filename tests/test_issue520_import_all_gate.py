"""#520: Import All must not import a discovered match whose release
preflight failed, and the AcoustID fingerprint shortcut states its identity
decision explicitly instead of leaving ``identity_validated`` unset.

Synthetic identities only; no network, no OpenAI call.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.ai_service as ai_service
import backend.recovery_carrier as recovery_carrier
import routes_library

REL = "083b9009-2222-2222-2222-222222222222"
RG = "37cb4a72-1111-1111-1111-111111111111"
_CAND = {"mb_albumid": REL, "mb_releasegroupid": RG, "album": "Forever",
         "artist": "Someone Else", "year": "1996", "tracks": 9, "country": "US",
         "score": 100, "label": "x", "formats": ["CD"]}

# Not "severe" (matches >= min_required), so confidence only drops high -> medium
# and the old Import All gate accepted it.
_FAILED_PF = {"ok": False, "matches": 7, "expected": 9, "audio_count": 9,
              "min_required": 6, "artist_ok": False, "artist_score": 0.1,
              "error": "", "examples": []}
_OK_PF = dict(_FAILED_PF, ok=True, matches=9, artist_ok=True, artist_score=1.0)


def _accepted(sug, ok=True):
    return routes_library._import_all_discovery_accepted({"ok": ok, "suggestion": sug})


class FingerprintShortcutThroughImportAllGate(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)

    def _run(self, preflight):
        ev = {"audio_files": [str(self.folder / "01.flac")], "folder_track_count": 9,
              "nested_audio_count": 0, "guessed_artist": "Folder Artist",
              "guessed_artist_mbid": "", "guessed_album": "Forever",
              "guessed_year": "1996", "track_lines": [], "filenames": ["01.flac"]}
        patches = [
            mock.patch.object(ai_service, "_ai_api_key", return_value=""),
            mock.patch.object(ai_service, "_build_folder_evidence", return_value=ev),
            mock.patch.object(ai_service, "_resolve_import_review_source_path",
                              return_value=(self.folder, None)),
            mock.patch.object(ai_service, "_acoustid_multi_file", return_value={REL: 3}),
            mock.patch.object(ai_service, "_acoustid_lookup_cached_outcome", return_value=None),
            mock.patch.object(ai_service, "_mb_release_search", return_value=[dict(_CAND)]),
            mock.patch.object(ai_service, "_mb_release_search_by_folder_tracks", return_value=[]),
            mock.patch.object(ai_service, "_fetch_mb_release_candidate", return_value={}),
            mock.patch.object(ai_service, "_score_mb_release_candidate",
                              side_effect=lambda ev, c: {"total": 0.9}),
            mock.patch.object(ai_service, "_run_ai_release_preflight", return_value=dict(preflight)),
            mock.patch.object(ai_service, "_ai_match_evidence_packet", return_value={}),
            mock.patch.object(recovery_carrier, "load_for_folder", return_value=None),
        ]
        for p in patches:
            p.start()
        try:
            return ai_service._ai_suggest_folder_internal(str(self.folder))
        finally:
            for p in reversed(patches):
                p.stop()

    def test_shortcut_sets_identity_fields_explicitly(self):
        sug = self._run(_OK_PF)["suggestion"]
        self.assertIn("AcoustID", sug["reason"])
        self.assertIs(sug["identity_validated"], True)
        self.assertEqual(sug["candidate_identity_error"], "")

    def test_failed_preflight_is_refused_by_import_all(self):
        out = self._run(_FAILED_PF)
        sug = out["suggestion"]
        self.assertEqual(sug["confidence"], "medium")  # was enough to pass before #520
        self.assertIs(sug["preflight"]["ok"], False)
        self.assertFalse(routes_library._import_all_discovery_accepted(out))

    def test_passing_preflight_is_accepted(self):
        out = self._run(_OK_PF)
        self.assertEqual(out["suggestion"]["confidence"], "high")
        self.assertTrue(routes_library._import_all_discovery_accepted(out))


class ImportAllDiscoveryGate(unittest.TestCase):
    BASE = {"mb_albumid": REL, "mb_valid": True, "confidence": "high",
            "identity_validated": True, "preflight": {"ok": True}}

    def test_accepts_clean_match(self):
        self.assertTrue(_accepted(dict(self.BASE)))

    def test_missing_preflight_left_to_job(self):
        sug = dict(self.BASE)
        sug.pop("preflight")
        self.assertTrue(_accepted(sug))

    def test_refusals(self):
        for change in ({"preflight": {"ok": False}}, {"identity_validated": False},
                       {"mb_valid": False}, {"confidence": "low"}, {"mb_albumid": " "}):
            with self.subTest(change=change):
                self.assertFalse(_accepted({**self.BASE, **change}))
        self.assertFalse(_accepted(dict(self.BASE), ok=False))
        self.assertFalse(routes_library._import_all_discovery_accepted({"ok": True}))


if __name__ == "__main__":
    unittest.main()
