import unittest
from pathlib import Path
try:  # ARCH-001: app.py module family (works under discovery and tests.<module> runs)
    from _app_ast_cache import app_family_source  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_ast_cache import app_family_source  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = app_family_source()


class SqliteDbTimeoutTests(unittest.TestCase):
    def test_shared_db_helper_waits_for_transient_locks(self):
        start = APP_SOURCE.index("def _sqlite_timeout_seconds")
        end = APP_SOURCE.index("def _stamp_album_release_id", start)
        source = APP_SOURCE[start:end]

        self.assertIn("BEETS_SQLITE_TIMEOUT", source)
        self.assertIn("get_db_connection(path)", source)
        self.assertNotIn("sqlite3.connect", source)
        self.assertIn("def _sqlite_write_retry", source)
        self.assertIn("database locked while", source)
        self.assertIn("return 30.0", source)


if __name__ == "__main__":
    unittest.main()
