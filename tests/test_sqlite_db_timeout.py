import unittest
from pathlib import Path
try:  # ARCH-001: app.py module family (works under discovery and tests.<module> runs)
    from _app_ast_cache import app_family_source  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_ast_cache import app_family_source  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = app_family_source()


class SqliteDbTimeoutTests(unittest.TestCase):
    def test_shared_db_helper_still_fails_closed(self):
        start = APP_SOURCE.index("def _db(")
        end = APP_SOURCE.index("def _stamp_album_release_id", start)
        source = APP_SOURCE[start:end]

        # ARCH-001: _db() referenced a control-agent helper removed with the
        # custom engine (it could only raise NameError). It now fails closed
        # explicitly; the Web Manager never opens the Beets database. The
        # transient-lock retry helpers that used to follow it were swept in
        # the W2 dead-code pass (nothing called them any more).
        self.assertIn("direct Beets database access is not available in the Web Manager", source)
        self.assertNotIn("get_db_connection", source)
        self.assertNotIn("sqlite3.connect", source)


if __name__ == "__main__":
    unittest.main()
