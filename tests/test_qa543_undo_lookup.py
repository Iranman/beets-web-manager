"""#546 (from the QA of #543): the attach Undo (recording_id_restore) writes
and moves nothing when the item is gone or its lookup raises."""
import os
import tempfile
import unittest
from unittest import mock


class UndoLookupFailsClosed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.addClassCleanup(cls._tmp.cleanup)
        with mock.patch.dict(os.environ, {"WEB_MANAGER_DATA_DIR": cls._tmp.name}):
            import app  # noqa: F401

    def _run(self, get_item):
        from backend import library_service as ls
        log = []
        with mock.patch.object(ls.lib, "get_item", side_effect=get_item), \
                mock.patch.object(ls.composite_workflows, "update_item_metadata",
                                  side_effect=AssertionError("wrote")) as upd, \
                mock.patch.object(ls.composite_workflows, "relocate_album",
                                  side_effect=AssertionError("moved")) as rel:
            ok = ls._run_item_recording_id_restore(7, {"mb_trackid": ""}, log)
        self.assertFalse(ok)
        self.assertEqual((upd.call_count, rel.call_count), (0, 0))
        return "\n".join(log)

    def test_missing_item(self):
        self.assertIn("no longer exists", self._run(lambda i: None))

    def test_lookup_raises(self):
        def boom(i):
            raise OSError("db locked")
        self.assertIn("restore failed", self._run(boom))


if __name__ == "__main__":
    unittest.main()
