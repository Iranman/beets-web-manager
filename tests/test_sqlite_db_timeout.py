import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")


class SqliteDbTimeoutTests(unittest.TestCase):
    def test_shared_db_helper_waits_for_transient_locks(self):
        start = APP_SOURCE.index("def _sqlite_timeout_seconds")
        end = APP_SOURCE.index("def _stamp_album_release_id", start)
        source = APP_SOURCE[start:end]

        self.assertIn("BEETS_SQLITE_TIMEOUT", source)
        self.assertIn("sqlite3.connect(db_path, timeout=timeout)", source)
        self.assertIn("PRAGMA busy_timeout", source)
        self.assertIn("PRAGMA journal_mode=WAL", source)
        self.assertIn("def _sqlite_write_retry", source)
        self.assertIn("database locked while", source)
        self.assertIn("return 30.0", source)


if __name__ == "__main__":
    unittest.main()
