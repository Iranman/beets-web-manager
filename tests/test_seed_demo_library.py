"""Tests for scripts/seed_demo_library.py — demo mode audio generation."""
import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import seed_demo_library  # noqa: E402


class SeedDemoLibraryTests(unittest.TestCase):
    def test_seed_creates_expected_number_of_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "demo"
            seed_demo_library.seed(target)
            wavs = sorted(target.glob("*.wav"))
            self.assertEqual(len(wavs), len(seed_demo_library.DEMO_TRACKS))

    def test_generated_files_are_valid_wav_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "demo"
            seed_demo_library.seed(target)
            for wav_path in target.glob("*.wav"):
                with wave.open(str(wav_path), "rb") as w:
                    self.assertEqual(w.getnchannels(), 1)
                    self.assertEqual(w.getframerate(), seed_demo_library.SAMPLE_RATE)
                    duration = w.getnframes() / w.getframerate()
                    self.assertAlmostEqual(duration, seed_demo_library.DURATION_SECONDS, delta=0.01)

    def test_filenames_include_track_number_and_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "demo"
            seed_demo_library.seed(target)
            names = {p.name for p in target.glob("*.wav")}
            for track in seed_demo_library.DEMO_TRACKS:
                expected = f"{track['track']:02d} - {track['title']}.wav"
                self.assertIn(expected, names)

    def test_artist_and_album_names_are_clearly_marked_as_demo(self):
        self.assertIn("Demo", seed_demo_library.DEMO_ARTIST)
        self.assertIn("Demo", seed_demo_library.DEMO_ALBUM)

    def test_seed_is_idempotent(self):
        """Running twice must not error or duplicate files unexpectedly."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "demo"
            seed_demo_library.seed(target)
            seed_demo_library.seed(target)
            wavs = sorted(target.glob("*.wav"))
            self.assertEqual(len(wavs), len(seed_demo_library.DEMO_TRACKS))


if __name__ == "__main__":
    unittest.main()
