"""QA for PR #296: folder_cleanup_v1 apply CAS matrix, repeat apply,
rollback from a terminal status, and the plugin's tracked-file refusal."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from beets.library import Item, Library

import backend.composite_workflows as cw
from backend import transaction_engine as te
from beetsplug.webmanager import folder_ops
from tests._folder_ops_local import patch_local_folder_ops
from tests.test_wave0_s1_containment import FakeAdapter, _Env


class ApplyCasMatrixTests(_Env):
    def plan_rename(self):
        src = self.music / "Albm"
        src.mkdir()
        plan = te.create_folder_cleanup_plan(self.store, {"action": "safe_rename", "source": str(src),
                                                          "target": str(self.music / "Album")})
        self.assertTrue(plan["ok"], plan)
        return plan["operation_id"], src

    def test_refused_statuses_change_nothing(self):
        local = patch_local_folder_ops(self, self.music)
        cases = [
            ("Failed", {}),
            ("Cancelled", {}),
            ("Rolled Back", {}),
            ("Partially Rolled Back", {}),
            ("Running", {"mutation_started": True}),  # an apply already in progress
            ("Running", {"engine_result": {"moved_records": [], "removed_dirs": []}}),
        ]
        for status, meta in cases:
            op, src = self.plan_rename()
            self.store.update(op, status=status, metadata=meta)
            res = te.execute_folder_cleanup_apply(self.store, op)
            self.assertEqual((res["ok"], res["code"], res["mutated"]), (False, "not_approved", False), (status, meta))
            self.assertEqual(self.store.get(op)["status"], status)
            shutil.rmtree(src)
        self.assertEqual(local.calls, [])

    def test_claimed_running_applies(self):
        local = patch_local_folder_ops(self, self.music)
        op, src = self.plan_rename()
        self.store.transition(op, "Preview", "Approved")
        self.store.transition(op, "Approved", "Running")  # claim_approved
        res = te.execute_folder_cleanup_apply(self.store, op)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.store.get(op)["status"], "Completed")
        self.assertEqual(len(local.calls), 1)
        self.assertTrue((self.music / "Album").is_dir() and not src.exists())

    def test_repeat_apply_of_completed_calls_beets_once(self):
        local = patch_local_folder_ops(self, self.music)
        op, _src = self.plan_rename()
        with mock.patch.object(cw, "beets_adapter", FakeAdapter()):
            first = cw.apply_folder_cleanup(op, store=self.store)
            again = cw.apply_folder_cleanup(op, store=self.store)
        self.assertTrue(first["ok"], first)
        self.assertEqual((again["ok"], again.get("idempotent"), again["mutated"]), (True, True, True))
        self.assertEqual(len(local.calls), 1)

    def test_rollback_from_partially_rolled_back_is_refused(self):
        local = patch_local_folder_ops(self, self.music)
        op, _src = self.plan_rename()
        self.store.update(op, status="Partially Rolled Back",
                          metadata={"engine_result": {"moved_records": [], "removed_dirs": []}})
        res = te.rollback_folder_cleanup(self.store, op)
        self.assertEqual((res["ok"], res["code"]), (False, "rollback_not_eligible"))
        self.assertEqual(self.store.get(op)["status"], "Partially Rolled Back")
        self.assertEqual(local.calls, [])


@unittest.skipIf(os.name == "nt", "Beets' PathQuery does not match on Windows")
class PluginTrackedFileTests(unittest.TestCase):
    def test_tracked_file_is_never_moved(self):
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, True)
        music = os.path.join(td, "music")
        os.makedirs(os.path.join(music, "A"))
        lib = Library(os.path.join(td, "l.blb"), directory=music)
        self.addCleanup(lambda: lib._connection().close())
        path = os.path.join(music, "A", "01.mp3")
        with open(path, "wb") as fh:
            fh.write(b"x")
        lib.add(Item(path=path.encode(), title="t"))
        with self.assertRaises(folder_ops._Refused) as ctx:
            folder_ops._step(lib, music, {"op": "move_file", "source": path, "target": os.path.join(music, "02.mp3")})
        self.assertEqual(ctx.exception.code, "PATH_IS_TRACKED")
        self.assertTrue(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
