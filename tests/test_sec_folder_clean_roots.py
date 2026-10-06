"""SEC-13: the no-audio folder cleanup may only range over the configured
library and download roots -- never /tmp or hardcoded paths."""
import unittest
from pathlib import Path

import app as app_module  # noqa: F401
from backend import cleanup_service


class FolderCleanRootsTests(unittest.TestCase):
    def test_no_temp_or_hardcoded_roots(self):
        roots = {Path(p).as_posix() for p in cleanup_service.FOLDER_CLEAN_ROOTS}
        for banned in ("/tmp", "/data/downloads", "/download"):
            self.assertNotIn(banned, roots)
        self.assertIn(cleanup_service.MUSIC_ROOT.as_posix(), roots)

    def test_tmp_is_refused(self):
        with self.assertRaises(RuntimeError) as ctx:
            cleanup_service._folder_clean_root("/tmp")
        self.assertIn("must be under", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
