"""An ffprobe timeout is an incomplete inspection, not a rejected download.

Live v0.16.0: both probes of a FLAC timed out at 20 s while the shared pool
was busy after a stack restart. The selected-files import logged it as
"Rejected download", and whole-folder imports quarantined/deleted such files
as if their audio had failed Music Format Preferences.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.ai_service as ai
import backend.import_service as isvc
from backend.audio_preferences import (
    DEFAULT_MUSIC_FORMAT_PREFERENCES,
    inspect_audio_file,
    validate_audio_file,
    validate_audio_tree,
)

_HANG = mock.patch(
    "backend.audio_preferences.subprocess.run",
    side_effect=lambda cmd, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, kw.get("timeout"))),
)


class AudioInspectionTimeoutTests(unittest.TestCase):
    def test_timeout_is_incomplete_not_rejected(self):
        with _HANG as run:
            props = inspect_audio_file("/downloads/a.flac", ffprobe_bin="ffprobe")
            result = validate_audio_file("/downloads/a.flac", DEFAULT_MUSIC_FORMAT_PREFERENCES)
        self.assertEqual(run.call_count, 4)  # full probe + bounded fast probe, twice
        self.assertFalse(props["ok"])
        self.assertTrue(props["inspection_incomplete"])
        self.assertFalse(result["ok"])  # the quality gate is never passed
        self.assertTrue(result["inspection_incomplete"])
        self.assertNotIn("Rejected download", result["message"])
        self.assertTrue(result["message"].startswith("Audio inspection incomplete:"))

    def test_corrupt_file_is_still_rejected(self):
        bad = type("Proc", (), {"returncode": 1, "stdout": "", "stderr": "Invalid data found"})()
        with mock.patch("backend.audio_preferences.subprocess.run", return_value=bad):
            result = validate_audio_file("/downloads/a.flac", DEFAULT_MUSIC_FORMAT_PREFERENCES)
        self.assertFalse(result["ok"])
        self.assertFalse(result["inspection_incomplete"])
        self.assertTrue(result["message"].startswith("Rejected download:"))

    def test_tree_defers_timed_out_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.flac").write_bytes(b"x")
            with _HANG:
                report = validate_audio_tree(tmp, DEFAULT_MUSIC_FORMAT_PREFERENCES, {".flac"})
        self.assertFalse(report["ok"])
        self.assertEqual(report["rejected"], [])
        self.assertEqual(len(report["deferred"]), 1)

    def test_folder_import_keeps_timed_out_file_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "a.flac"
            src.write_bytes(b"x")
            log = []
            with _HANG, \
                    mock.patch.object(ai, "_music_format_preferences", return_value=DEFAULT_MUSIC_FORMAT_PREFERENCES), \
                    mock.patch.object(ai, "_handle_rejected_audio_download") as handle, \
                    mock.patch.object(ai, "_mark_music_format_needs_replacement") as mark:
                with self.assertRaisesRegex(RuntimeError, "inspection did not complete"):
                    ai._validate_import_source_audio(tmp, log)
            self.assertTrue(src.exists())
        handle.assert_not_called()
        mark.assert_not_called()
        self.assertIn("Audio inspection incomplete", "\n".join(log))

    def test_selected_files_import_defers_without_rejected_wording(self):
        log = []
        with _HANG, \
                mock.patch.object(isvc, "_music_format_preferences", return_value=DEFAULT_MUSIC_FORMAT_PREFERENCES), \
                mock.patch.object(isvc, "_handle_rejected_audio_download") as handle:
            with self.assertRaisesRegex(RuntimeError, "retry the import later"):
                isvc._filter_import_review_selected_audio_files([Path("/downloads/x/a.flac")], log)
        handle.assert_not_called()
        joined = "\n".join(log)
        self.assertIn("source kept in review", joined)
        self.assertNotIn("Rejected download", joined)


if __name__ == "__main__":
    unittest.main()
