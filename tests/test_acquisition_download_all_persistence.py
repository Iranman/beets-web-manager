import unittest
from pathlib import Path


class AcquisitionDownloadAllPersistenceTests(unittest.TestCase):
    def test_download_all_last_batch_is_persisted_and_returned(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        source = app_path.read_text(encoding="utf-8")

        self.assertIn("_ACQ_DOWNLOAD_ALL_LAST_FILE", source)
        self.assertIn("def _save_acq_download_all_last", source)
        self.assertIn("def _load_acq_download_all_last", source)
        self.assertIn("last_job", source)
        self.assertIn("_persist(\"success\", totals, log)", source)
        self.assertIn("_persist(\"failed\", totals, log", source)


if __name__ == "__main__":
    unittest.main()
