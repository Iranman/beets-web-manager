import unittest
from pathlib import Path
try:  # ARCH-001: app.py module family
    from _app_ast_cache import app_family_source  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_ast_cache import app_family_source  # noqa: E402


class SlskdPollGuardSourceTest(unittest.TestCase):
    def test_repeated_transfer_404_fails_candidate_fast(self):
        app_source = app_family_source()

        self.assertIn("poll_error_count >= 3", app_source)
        self.assertIn("HTTP 404", app_source)
        self.assertIn("SLSKD transfer state disappeared while polling", app_source)
        self.assertIn("Trying another source", app_source)


if __name__ == "__main__":
    unittest.main()
