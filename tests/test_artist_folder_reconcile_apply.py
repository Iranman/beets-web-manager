"""The artist-folder reconcile apply path, run against the real functions.

Mocks of apply_artist_folder_reconcile pinned a call its signature rejected, so
every real call raised TypeError while the tests stayed green.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import ai_service
from backend import composite_workflows as cw
from backend import import_reconciliation_service as svc
from backend import library_service
from backend import maintenance_service as ms
from backend.beets_adapter import BeetsAdapter, BeetsAdapterError
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

    def test_empty_stored_plan_is_refused_and_stays_unapplied(self):
        tx = self.store.create(operation_type="Artist Folder Reconcile", status="Preview", metadata={})
        res = svc._apply_artist_folder_reconcile_resilient(tx["id"], [])
        self.assertEqual((res["ok"], res["code"]), (False, "artist_folder_plan_empty"))
        self.assertEqual(self.store.get(tx["id"])["status"], "Preview")
        self.adapter.modify.assert_not_called()

    def test_resume_of_an_empty_plan_rescans_and_reports_the_refusal(self):
        tx = self.store.create(operation_type="Artist Folder Reconcile", status="Preview", metadata={})
        log = []
        with mock.patch.object(ms, "_stamp_artist_folder_scan",
                               return_value={"ok": True, "candidates": [{"name": "A"}], "skipped": []}):
            res = ms._maintenance_artist_folder_merge_step(log, None, "/music", resume_operation_id=tx["id"])
        self.assertEqual((res["ok"], res["error_code"]), (False, "artist_folder_mode_unsupported"))
        self.assertEqual((res["renamed"], res["merged"]), (0, 0))
        self.assertEqual(self.store.get(tx["id"])["status"], "Preview")
        self.adapter.modify.assert_not_called()

    def test_adapter_error_is_not_called_a_lost_response(self):
        log = []
        with mock.patch.object(cw, "apply_artist_folder_reconcile", side_effect=BeetsAdapterError("boom")), \
                mock.patch.object(cw, "get_transaction",
                                  return_value={"transaction": {"status": "Failed"}}):
            res = svc._apply_artist_folder_reconcile_resilient("op-1", log)
        self.assertFalse(res["ok"])
        self.assertNotIn("lost", "\n".join(log))
        self.assertTrue(any("outcome recorded on its transaction" in line for line in log), log)


class RefusedMergeTests(unittest.TestCase):
    """A real plan refusal, through the merge route and the import auto-merge."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.music = Path(tmp.name).resolve()
        self.source, self.target = self.music / "Al  Campbell", self.music / "Al Campbell"
        self.source.mkdir()
        self.target.mkdir()
        (self.source / "track.mp3").write_bytes(b"audio")
        group = {"key": "alcampbell", "canonical": {"path": str(self.target), "name": "Al Campbell"},
                 "sources": [{"path": str(self.source), "name": "Al  Campbell"}]}
        for target, name, kwargs in (
                (library_service, "_artist_folder_repair_root", {"return_value": (self.music, "")}),
                (library_service, "_scan_artist_folder_groups", {"return_value": [group]}),
                (library_service, "_artist_folder_fingerprint_confirms", {"return_value": True}),
                (ai_service, "_scan_artist_folder_groups", {"return_value": [group]})):
            patcher = mock.patch.object(target, name, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _assert_refused(self, summary):
        self.assertIs(summary["ok"], False)
        self.assertIn("not available", summary["error"])
        self.assertEqual({k: summary[k] for k in ("groups", "folders", "files")},
                         {"groups": 0, "folders": 0, "files": 0})
        self.assertTrue((self.source / "track.mp3").exists())

    def test_merge_route_job_reports_the_refusal_with_zero_counts(self):
        import app as app_module
        captured = {}
        with mock.patch.object(app_module.jobs, "start_python",
                               side_effect=lambda fn, label="", metadata=None:
                               captured.update(fn=fn) or mock.MagicMock(job_id="j")), \
                mock.patch("routes_cleanup._artist_folder_repair_root", return_value=(self.music, "")):
            with app_module.app.test_request_context(
                    "/api/clean/artist-folders/merge", method="POST", json={"dry_run": False}):
                app_module.clean_artist_folders_merge()
        self._assert_refused(captured["fn"]([]))

    def test_import_auto_merge_does_not_claim_a_merge(self):
        log = []
        self.assertFalse(ai_service._auto_merge_case_duplicate_artist_folder(str(self.music), "Al Campbell", log))
        joined = "\n".join(log)
        self.assertNotIn("Merged", joined)
        self.assertIn("not performed", joined)


if __name__ == "__main__":
    unittest.main()
