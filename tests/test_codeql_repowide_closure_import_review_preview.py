"""Regression coverage for a real (low-severity, defense-in-depth) gap found
while dispositioning CodeQL alerts 208/209/207 (py/path-injection,
app.py:19672-19673 at baseline commit 7b05844d58d657ce6f1ae6c5b1724f5ba70ee257)
during the repository-wide CodeQL closure pass.

_build_import_target_preview() (POST /api/folders/import-target-preview) took
track_mapping[i]["source_path"] straight from the request body and built a
Path from it with no containment check -- unlike every sibling Import Review
helper (_resolve_import_review_selected_audio_file,
_remaining_audio_files, _target_preview_source_files), which all route
through _resolve_import_review_source_path()/_resolve_import_review_cleanup_file()
first. This function performs no filesystem writes (see its own docstring)
so the practical impact was narrow -- an unvalidated .resolve()/.exists()
comparison, not a read/write/enumerate primitive -- but it was still a real,
CodeQL-flagged exception to the codebase's own established pattern. Fixed by
routing row["source_path"] through the same _resolve_import_review_cleanup_file()
helper its siblings already use, scoped to the request's own validated
trusted_source_path.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module


class ImportTargetPreviewSourcePathContainmentTests(unittest.TestCase):
    """Uses real tempdir-based Path objects throughout (not POSIX-literal
    strings like "/etc/passwd") so the containment property being tested is
    meaningful on whatever platform runs this suite -- pathlib's
    is_absolute() semantics for a bare "/..." string differ on Windows."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.trusted_source = self.root / "trusted" / "source"
        self.trusted_source.mkdir(parents=True)
        self.outside = self.root / "outside"
        self.outside.mkdir(parents=True)

    def _payload(self, source_path_value):
        return {
            "path": str(self.trusted_source),
            "release_group_id": "11111111-1111-1111-1111-111111111111",
            "artist": "Some Artist",
            "album": "Some Album",
            "year": 2020,
            "identity_validated": True,
            "track_mapping": [
                {"status": "matched", "num": 1, "source_path": source_path_value},
            ],
        }

    def _run(self, source_path_value):
        with mock.patch.object(
            app_module, "_resolve_import_review_source_path",
            return_value=(self.trusted_source, None),
        ), mock.patch.object(
            app_module, "_target_preview_source_files", return_value=[],
        ), mock.patch.object(
            app_module, "_target_preview_artist_folder", return_value="Some Artist",
        ):
            return app_module._build_import_target_preview(self._payload(source_path_value))

    def test_malicious_absolute_row_source_path_is_rejected_not_used_verbatim(self):
        outside_secret = self.outside / "secret.flac"
        outside_secret.write_bytes(b"")
        result = self._run(str(outside_secret))
        track_source_paths = [t.get("source_path", "") for t in result.get("tracks", [])]
        self.assertNotIn(str(outside_secret), track_source_paths)
        self.assertTrue(
            any("source file missing" in b for b in result.get("blocked_reasons", [])),
            result.get("blocked_reasons"),
        )

    def test_relative_traversal_row_source_path_is_rejected(self):
        result = self._run("../../outside/secret.flac")
        track_source_paths = [t.get("source_path", "") for t in result.get("tracks", [])]
        self.assertFalse(any("outside" in p for p in track_source_paths))
        self.assertTrue(
            any("source file missing" in b for b in result.get("blocked_reasons", [])),
            result.get("blocked_reasons"),
        )

    def test_in_root_row_source_path_is_still_accepted(self):
        # The fix must not break the legitimate case: a source_path the
        # client echoes back that genuinely is under the validated root.
        in_root_file = self.trusted_source / "01 - Track.flac"
        result = self._run(str(in_root_file))
        track_source_paths = [t.get("source_path", "") for t in result.get("tracks", [])]
        self.assertIn(str(in_root_file.resolve()), track_source_paths)


if __name__ == "__main__":
    unittest.main()
