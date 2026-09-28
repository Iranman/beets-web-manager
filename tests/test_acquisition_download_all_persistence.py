import unittest
from pathlib import Path
try:  # ARCH-001: app.py module family (works under discovery and tests.<module> runs)
    from _app_ast_cache import app_family_source  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_ast_cache import app_family_source  # noqa: E402


class AcquisitionDownloadAllPersistenceTests(unittest.TestCase):
    def test_download_all_last_batch_is_persisted_and_returned(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        source = app_family_source()

        self.assertIn("_ACQ_DOWNLOAD_ALL_LAST_FILE", source)
        self.assertIn("def _save_acq_download_all_last", source)
        self.assertIn("def _load_acq_download_all_last", source)
        self.assertIn("last_job", source)
        self.assertIn("_persist(\"success\", totals, log)", source)
        self.assertIn("_persist(\"failed\", totals, log", source)


if __name__ == "__main__":
    unittest.main()
