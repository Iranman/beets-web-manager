"""QA of #552 (#546): the singleton attach's Undo lives on its Metadata
Update transaction, so that transaction must be rollback-eligible through the
route gate after a write, and a refused (fail-closed) apply must not be."""
import os
import tempfile
import unittest
from unittest import mock

from backend import composite_workflows as cw
import tests.test_issue546_item_metadata_safety as t546


class InnerUndoEligibility(t546.ItemMetadataFailClosedTests):
    # Run only this class's own tests, not the inherited ones.
    test_missing_item_fails_closed = None
    test_lookup_error_fails_closed_and_writes_nothing = None
    test_expected_current_mismatch_refuses_the_write = None
    test_expected_current_match_writes_and_rolls_back = None

    @classmethod
    def setUpClass(cls):
        # routes_maintenance imports the app: keep it off any real data dir.
        cls._data = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.addClassCleanup(cls._data.cleanup)
        with mock.patch.dict(os.environ, {"WEB_MANAGER_DATA_DIR": cls._data.name}):
            import app  # noqa: F401

    def _eligible(self, op_id):
        from routes_maintenance import rollback_eligibility
        return rollback_eligibility(self.store.get(op_id))

    def test_written_metadata_update_is_the_one_undo(self):
        item, _ = self._singleton()
        res = cw.update_item_metadata(item.id, {"mb_trackid": t546.NEW}, write_tags=True,
                                      expected_current={"mb_trackid": t546.OLD},
                                      adapter=self.adapter, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertTrue(self._eligible(res["operation_id"])["allowed"])

    def test_refused_metadata_update_offers_no_undo(self):
        item, _ = self._singleton()
        res = cw.update_item_metadata(item.id, {"mb_trackid": t546.NEW}, write_tags=True,
                                      expected_current={"mb_trackid": t546.OTHER},
                                      adapter=self.adapter, store=self.store)
        self.assertFalse(res["ok"], res)
        self.assertFalse(self._eligible(res["operation_id"])["allowed"])

    def test_missing_item_offers_no_undo(self):
        res = cw.update_item_metadata(987654, {"title": "x"}, adapter=self.adapter, store=self.store)
        self.assertFalse(self._eligible(res["operation_id"])["allowed"])


if __name__ == "__main__":
    unittest.main()
