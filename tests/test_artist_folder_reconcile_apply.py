"""The artist-folder reconcile apply path, run against the real functions.

Mocks of apply_artist_folder_reconcile pinned a call its signature rejected, so
every real call raised TypeError while the tests stayed green.
"""
import tempfile
import unittest
from unittest import mock

from backend import composite_workflows as cw
from backend import import_reconciliation_service as svc
from backend.beets_adapter import BeetsAdapter
from backend.transaction_engine import TransactionStore


class RealApplyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict("os.environ", {"WEB_MANAGER_DATA_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.store = TransactionStore(tmp.name + "/transactions")
        self.adapter = mock.MagicMock(spec=BeetsAdapter)
        self.adapter.get_album.return_value = {"id": 10, "album": "A", "albumartist": "Old", "artist": "Old"}
        for name, value in (("_get_store", lambda _store=None: self.store), ("beets_adapter", self.adapter)):
            patcher = mock.patch.object(cw, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_resilient_apply_runs_the_real_function_without_polling(self):
        op_id = cw.plan_artist_folder_reconcile({"album_ids": [10], "canonical_name": "New"})["operation_id"]
        log = []
        with mock.patch.object(cw, "get_transaction") as poll:
            res = svc._apply_artist_folder_reconcile_resilient(op_id, log)
        self.assertTrue(res["ok"], (res, log))
        poll.assert_not_called()
        self.assertEqual(self.store.get(op_id)["status"], "Completed")
        self.adapter.modify.assert_called_once_with(
            fields={"albumartist": "New"}, album_ids=[10], write=True, move=True)

    def test_programming_error_fails_at_once_and_is_not_called_a_lost_response(self):
        log = []
        with mock.patch.object(cw, "apply_artist_folder_reconcile", side_effect=TypeError("bad call /secret/path")), \
                mock.patch.object(cw, "get_transaction") as poll:
            res = svc._apply_artist_folder_reconcile_resilient("op-1", log)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "apply_internal_error")
        poll.assert_not_called()
        joined = "\n".join(log) + res["error"]
        self.assertNotIn("lost", joined)
        self.assertNotIn("/secret/path", joined)

    def test_plan_refuses_folder_modes_it_cannot_carry_out(self):
        create = mock.patch.object(self.store, "create", wraps=self.store.create)
        with create as created:
            for payload in ({"root": "/music", "mode": "stamp_mbid"},
                            {"root": "/music", "mode": "scan_merge", "candidates": [{"source_path": "/music/A"}]}):
                res = cw.plan_artist_folder_reconcile(payload)
                self.assertEqual((res["ok"], res["code"]), (False, "artist_folder_mode_unsupported"))
        created.assert_not_called()


if __name__ == "__main__":
    unittest.main()
