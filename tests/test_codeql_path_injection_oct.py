"""py/path-injection regressions: CodeQL #1415/#1416 (transaction store
file path) and #1422 (review-folder delete route)."""

import re
import tempfile
import unittest
from unittest import mock

try:
    import test_sec002_app_path_import_review_cleanup as _route_mod
except ImportError:  # pragma: no cover
    from tests import test_sec002_app_path_import_review_cleanup as _route_mod

import backend.transaction_engine as te
import routes_import

_Route = _route_mod.ImportReviewCleanupPathBoundaryTests


class TransactionStorePathContainmentTests(unittest.TestCase):
    def test_path_stays_in_root_even_if_the_id_regex_is_loosened(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = te.TransactionStore(tmp)
            with mock.patch.object(te, "_TRANSACTION_ID_RE", re.compile(r".+")):
                for bad in ("../escape", "../../etc/passwd", "/etc/passwd"):
                    with self.subTest(bad=bad), self.assertRaises(KeyError):
                        store._path(bad)


class ReviewFolderDeleteRootConfinementTests(unittest.TestCase):
    setUp = _Route.setUp
    tearDown = _Route.tearDown
    _write_audio = _Route._write_audio
    _write_pending = _Route._write_pending

    def test_pending_folder_outside_cleanup_roots_never_reaches_delete(self):
        folder = self.outside / "album"
        self._write_audio(folder / "01.flac")
        self._write_pending(folder)
        with mock.patch.object(routes_import, "_delete_review_source_folder") as delete:
            resp = self.client.post("/api/import/review-folder/delete", json={"path": str(folder)})
        self.assertEqual(resp.status_code, 400, resp.get_json())
        self.assertEqual(resp.get_json()["error"], routes_import._REVIEW_FOLDER_NOT_PENDING)
        delete.assert_not_called()
        self.assertTrue((folder / "01.flac").exists())

    def test_pending_folder_inside_downloads_still_deletes(self):
        folder = self.downloads / "album"
        self._write_audio(folder / "01.flac")
        self._write_pending(folder)
        resp = self.client.post("/api/import/review-folder/delete", json={"path": str(folder)})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertFalse((folder / "01.flac").exists())


del _Route

if __name__ == "__main__":
    unittest.main()
