import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict

import app


class TestSec002AppPathLibraryDuplicates(unittest.TestCase):
    """Security and behavioral tests for SEC-002 Wave 7:

    Library Duplicate / AI Scan Path Security & Authority Scoping.
    """

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name).resolve()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_resolve_source_path_rejects_null_bytes(self):
        raw_path = str(self.tmp_path / "valid\x00folder")
        resolved, err = app._resolve_import_review_source_path(raw_path, allow_music=True)
        self.assertIsNone(resolved)
        self.assertIn("unsafe", err.lower())

    def test_resolve_source_path_rejects_traversal(self):
        raw_path = str(self.tmp_path / ".." / ".." / "etc" / "passwd")
        resolved, err = app._resolve_import_review_source_path(raw_path, allow_music=True)
        self.assertIsNone(resolved)
        self.assertIsNotNone(err)

    def test_resolve_source_path_accepts_valid_temp_dir(self):
        valid_dir = self.tmp_path / "test_album"
        valid_dir.mkdir(parents=True, exist_ok=True)
        resolved, err = app._resolve_import_review_source_path(str(valid_dir), allow_music=True, expected_type="dir")
        self.assertIsNone(err)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved, valid_dir.resolve())

    def test_path_origin_hint_handles_invalid_paths_safely(self):
        hint = app._path_origin_hint("../../etc/passwd")
        self.assertEqual(hint, {"source_folder": "../../etc/passwd"})

        hint_null = app._path_origin_hint("/data/downloads/test\x00dir")
        self.assertEqual(hint_null, {})

    def test_build_folder_evidence_returns_empty_on_invalid_path(self):
        ev = app._build_folder_evidence("../../etc/passwd")
        self.assertEqual(ev["audio_files"], [])
        self.assertEqual(ev["folder_track_count"], 0)

    def test_build_folder_evidence_collects_valid_audio_files(self):
        folder = self.tmp_path / "album_folder"
        folder.mkdir(parents=True, exist_ok=True)
        track1 = folder / "01 - Track.flac"
        track2 = folder / "02 - Track.mp3"
        track1.write_bytes(b"FLACDATA")
        track2.write_bytes(b"MP3DATA")

        ev = app._build_folder_evidence(str(folder))
        self.assertEqual(ev["folder_track_count"], 2)
        self.assertEqual(len(ev["audio_files"]), 2)

    def test_import_review_folder_signature_returns_hash(self):
        folder = self.tmp_path / "sig_folder"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "01.flac").write_bytes(b"HEADER")

        sig1 = app._import_review_folder_signature(str(folder))
        self.assertTrue(bool(sig1))
        self.assertEqual(len(sig1), 16)

        # Modifying file changes signature
        (folder / "01.flac").write_bytes(b"HEADER_MODIFIED")
        sig2 = app._import_review_folder_signature(str(folder))
        self.assertNotEqual(sig1, sig2)

    def test_candidate_track_local_candidates_ignores_traversal(self):
        candidates = app._candidate_track_local_candidates("../../../etc")
        self.assertEqual(candidates, [])

    def test_ai_suggest_folder_internal_rejects_invalid_path(self):
        res = app._ai_suggest_folder_internal("../../../etc/passwd")
        self.assertFalse(res.get("ok"))
        self.assertIn("error", res)


if __name__ == "__main__":
    unittest.main()
