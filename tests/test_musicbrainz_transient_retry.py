"""Regression coverage for transient MusicBrainz recording-search failures."""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")


def _function_source(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


class MusicBrainzTransientRetryTests(unittest.TestCase):
    def setUp(self):
        self.helper = _function_source(
            APP_SOURCE,
            "def _musicbrainz_transient_error(",
            "def _mb_release_search_by_folder_tracks(",
        )
        self.recording_search = _function_source(
            APP_SOURCE,
            "def _mb_release_search_by_folder_tracks(",
            "def _resolve_album_release_for_import(",
        )

    def test_ssl_eof_from_musicbrainz_is_treated_as_transient(self):
        self.assertIn('"unexpected_eof"', self.helper)
        self.assertIn('"eof occurred in violation of protocol"', self.helper)
        self.assertIn('"connection reset"', self.helper)
        self.assertIn('"remote disconnected"', self.helper)

    def test_recording_search_retries_transient_errors_three_times(self):
        self.assertIn("transient = _musicbrainz_transient_error(ex)", self.recording_search)
        self.assertIn("for attempt in range(1, 4):", self.recording_search)
        self.assertIn("if transient and attempt < 3:", self.recording_search)
        self.assertIn('retrying ({attempt}/3)', self.recording_search)
        self.assertNotIn("transient_codes", self.recording_search)

    def test_final_warning_only_after_retry_window(self):
        retry_pos = self.recording_search.index("if transient and attempt < 3:")
        warn_pos = self.recording_search.index("WARN: MB recording search failed")
        self.assertLess(retry_pos, warn_pos)


if __name__ == "__main__":
    unittest.main()
