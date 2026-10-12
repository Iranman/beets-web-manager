"""Failure injection between transaction stages on the Clean All folder
cleanup family (folder_cleanup_v1), steps run by the plugin's folder-op code
on a temp library: after preview, mid-apply, after a file op but before its
result is recorded, a process crash mid-apply, and during rollback. At every
point no file is lost or duplicated, the transaction says what happened, and
a rollback (or retry) restores the original layout."""

import time
from pathlib import Path
from unittest import mock

import backend.composite_workflows as cw
import backend.transaction_recovery as recovery
from backend import transaction_engine as te
from tests.test_transaction_safety_04 import _Beets, _FolderEnv


class _Crash(BaseException):
    """The process dies: nothing after this point runs."""


class FailureInjectionMatrixTests(_FolderEnv):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(te, "_FOLDER_STEP_RETRY_DELAY", 0),
                  mock.patch.object(cw, "_library_refs_under", return_value=[])):  # no Beets items here
            p.start()
            self.addCleanup(p.stop)
        self.src, self.dst = self.music / "Old", self.music / "New"
        self.src.mkdir()
        self.dst.mkdir()
        for name in ("a.txt", "b.txt", "c.txt"):
            (self.src / name).write_text(name)
        self.original = self.layout()
        plan = te.create_folder_cleanup_plan(self.store, {"action": "merge", "source": str(self.src),
                                                          "target": str(self.dst)})
        self.op = plan["operation_id"]

    def layout(self):
        return {p.relative_to(self.music).as_posix(): p.read_text()
                for p in self.music.rglob("*.txt") if p.name != "keep.txt"}

    def assert_nothing_lost(self):
        self.assertEqual(sorted(self.layout().values()), sorted(self.original.values()))

    def use(self, beets):
        p = mock.patch("backend.beets_adapter.beets_adapter.folder_op", beets.folder_op)
        p.start()
        self.addCleanup(p.stop)
        return beets

    def roll_back_until_done(self):
        for _ in range(3):
            res = cw.rollback_folder_cleanup(self.op, store=self.store)
            if res.get("status") == "Rolled Back":
                break
        self.assertEqual(self.store.get(self.op)["status"], "Rolled Back", res)
        self.assertEqual(self.layout(), self.original)

    def test_after_preview_a_cancel_wins_and_nothing_moves(self):
        self.use(_Beets(self.music))
        self.store.transition(self.op, "Preview", "Cancelled")
        res = cw.apply_folder_cleanup(self.op, store=self.store, approved_by="operator")
        self.assertFalse(res.get("ok"), res)
        self.assertEqual(self.store.get(self.op)["status"], "Cancelled")
        self.assertEqual(self.layout(), self.original)

    def test_mid_apply_refusal_records_what_moved_and_rolls_back(self):
        beets = _Beets(self.music)
        real = beets.folder_op

        def refuse_second(op, key, **paths):
            if key.endswith(":apply:1"):
                beets.fail = {str(paths.get("target"))}
                self.refused = paths
            return real(op, key, **paths)
        self.use(mock.Mock(folder_op=refuse_second))
        self.store.transition(self.op, "Preview", "Approved")
        res = te.execute_folder_cleanup_apply(self.store, self.op)
        self.assertEqual((res["status"], res["mutated"]), ("Failed", True), res)
        self.assert_nothing_lost()
        self.assertTrue(Path(self.refused["source"]).exists())  # the source stays until its move is proven
        beets.fail = set()
        self.roll_back_until_done()

    def test_file_moved_but_reply_lost_is_unconfirmed_and_rolls_back(self):
        beets = self.use(_Beets(self.music, silent={":apply:1"}))
        self.store.transition(self.op, "Preview", "Approved")
        res = te.execute_folder_cleanup_apply(self.store, self.op)
        self.assertEqual(res["status"], "Failed", res)
        self.assertEqual(len(self.store.get(self.op)["metadata"]["unconfirmed_steps"]), 1)
        self.assert_nothing_lost()
        beets.silent = set()
        self.roll_back_until_done()

    def test_process_crash_mid_apply_is_recovery_required_after_restart(self):
        beets = _Beets(self.music)
        real = beets.folder_op

        def die_on_second(op, key, **paths):
            if key.endswith(":apply:1"):
                raise _Crash()
            return real(op, key, **paths)
        self.use(mock.Mock(folder_op=die_on_second))
        self.store.transition(self.op, "Preview", "Approved")
        with self.assertRaises(_Crash):
            cw.apply_folder_cleanup(self.op, store=self.store, approved_by="operator")
        self.assertEqual(self.store.get(self.op)["status"], "Running")
        recovery.sweep(store=self.store, before=time.time() + 1)
        tx = self.store.get(self.op)
        self.assertEqual(tx["status"], "Recovery Required", tx["logs"])
        self.assertIn("Recovered after restart", str(tx["logs"]))
        self.assert_nothing_lost()

    def test_failure_during_rollback_is_partial_and_a_retry_finishes(self):
        beets = self.use(_Beets(self.music))
        self.store.transition(self.op, "Preview", "Approved")
        self.assertEqual(te.execute_folder_cleanup_apply(self.store, self.op)["status"], "Completed")
        beets.fail = {"a.txt"}
        first = cw.rollback_folder_cleanup(self.op, store=self.store)
        self.assertEqual(first["status"], "Partially Rolled Back", first)
        self.assert_nothing_lost()
        recovery.sweep(store=self.store, before=time.time() + 1)  # a restart changes nothing
        self.assertEqual(self.store.get(self.op)["status"], "Partially Rolled Back")
        beets.fail = set()
        self.roll_back_until_done()
