"""Format-replacement identity must come from the canonical recording
evaluator (ARCH-002): the original file is removed once a replacement
verifies against this identity."""

import unittest
from unittest import mock

import app as app_module
from backend.recording_review import resolve_recording_identity
try:  # ARCH-001: patch app.py and the modules extracted from it
    from _app_family import patch_app_family  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_family import patch_app_family  # noqa: E402

REC = "33333333-3333-3333-3333-333333333333"
OTHER = "66666666-6666-6666-6666-666666666666"
RGID = "11111111-1111-1111-1111-111111111111"


def _local(**o):
    data = {"title": "Song", "artist": "Band", "album": "Album", "duration_seconds": 200.0,
            "filename": "01 Song.mp3", "recording_id": ""}
    data.update(o)
    return data


def _hit(rid, score, **o):
    data = {"mb_trackid": rid, "score": score, "title": "Song", "artist": "Band", "mb_releasegroupid": RGID}
    data.update(o)
    return data


class ResolveRecordingIdentityTests(unittest.TestCase):
    def test_embedded_id_without_acoustid_is_established(self):
        r = resolve_recording_identity(_local(recording_id=REC), acoustid_hits=None)
        self.assertTrue(r["ok"])
        self.assertEqual(r["recording_id"], REC)
        self.assertEqual(r["identity_proof"], "embedded_recording_id")

    def test_acoustid_top_hit_never_silently_overrides_embedded_id(self):
        r = resolve_recording_identity(_local(recording_id=REC), acoustid_hits=[_hit(OTHER, 92)])
        self.assertFalse(r["ok"])
        self.assertIn("conflicts", r["reason"])

    def test_single_confirmed_acoustid_is_established(self):
        r = resolve_recording_identity(_local(), acoustid_hits=[_hit(REC, 95)])
        self.assertTrue(r["ok"])
        self.assertEqual(r["identity_proof"], "acoustid_recording_id")

    def test_ambiguous_acoustid_needs_review(self):
        r = resolve_recording_identity(_local(), acoustid_hits=[_hit(REC, 95), _hit(OTHER, 94)])
        self.assertFalse(r["ok"])

    def test_below_canonical_acoustid_floor_is_not_identity(self):
        r = resolve_recording_identity(_local(), acoustid_hits=[_hit(REC, 72)])
        self.assertFalse(r["ok"])

    def test_text_search_alone_never_establishes_identity(self):
        text = [{"mb_trackid": REC, "title": "Song", "artist": "Band", "score": 100}]
        r = resolve_recording_identity(_local(), acoustid_hits=[], text_candidates=text)
        self.assertFalse(r["ok"])
        self.assertIn("text-similar", r["reason"])


class ReplacementWorkflowTests(unittest.TestCase):
    def _run(self, row, *, hits, text=()):
        log = []
        with patch_app_family(app_module, "_music_format_read_embedded_identity", return_value={}), \
                patch_app_family(app_module, "_acoustid_lookup_cached", return_value=hits), \
                patch_app_family(app_module, "_mb_recording_search", return_value=list(text)) as search, \
                patch_app_family(app_module, "_fetch_mb_recording_details", return_value={}), \
                patch_app_family(app_module, "_fetch_mb_release_candidate", return_value={}), \
                patch_app_family(app_module, "_resolve_release_group_to_release", return_value=""), \
                mock.patch("pathlib.Path.is_file", return_value=True):
            result = app_module._music_format_resolve_replacement_identity(row, log)
        return result, search, log

    def test_text_only_candidate_goes_to_review_not_replacement(self):
        row = {"artist": "Band", "title": "Song", "path": "/music/Band/Album/01 Song.mp3"}
        text = [{"mb_trackid": REC, "title": "Song", "artist": "Band", "score": 100}]
        result, search, _ = self._run(row, hits=[], text=text)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_stage"], "identity_resolution")
        search.assert_called_once()

    def test_embedded_id_contradicted_by_fingerprint_goes_to_review(self):
        row = {"artist": "Band", "title": "Song", "mb_trackid": REC, "path": "/music/Band/Album/01 Song.mp3"}
        result, search, _ = self._run(row, hits=[_hit(OTHER, 93)])
        self.assertFalse(result["ok"])
        search.assert_not_called()

    def test_fingerprint_confirmed_identity_proceeds(self):
        row = {"artist": "Band", "title": "Song", "path": "/music/Band/Album/01 Song.mp3"}
        result, _, _ = self._run(row, hits=[_hit(REC, 96)])
        self.assertTrue(result["ok"])
        self.assertEqual(result["mb_trackid"], REC)
        self.assertEqual(result["acoustid_mb_trackid"], REC)
        self.assertEqual(result["identity_proof"], "acoustid_recording_id")


if __name__ == "__main__":
    unittest.main()
