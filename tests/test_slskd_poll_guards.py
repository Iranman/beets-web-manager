import unittest
from pathlib import Path


class SlskdPollGuardSourceTest(unittest.TestCase):
    def test_repeated_transfer_404_fails_candidate_fast(self):
        app_source = (Path(__file__).resolve().parents[1] / "app.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("poll_error_count >= 3", app_source)
        self.assertIn("HTTP 404", app_source)
        self.assertIn("SLSKD transfer state disappeared while polling", app_source)
        self.assertIn("Trying another source", app_source)


if __name__ == "__main__":
    unittest.main()
