"""#300 R3 (PR #307 QA): the plugin's durable folder-op registry under
concurrency, damaged files and eviction."""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import beetsplug.webmanager.operations as ops


class DurableRegistryTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)
        for p in (mock.patch.dict(ops._operations, clear=True), mock.patch.object(ops, "_durable_file", None)):
            p.start()
            self.addCleanup(p.stop)
        self.lib = SimpleNamespace(path=os.path.join(self.td, "library.db").encode())
        self.file = os.path.join(self.td, ops.REGISTRY_FILENAME)

    def finish(self, key, op_type="folder_op"):
        ops.register_operation(op_type, key, fingerprint="fp")
        ops.update_operation(key, "succeeded", result={"success": True})

    def restart(self):
        ops._operations.clear()
        ops._durable_file = None
        ops.bind_durable_registry(self.lib)

    def test_concurrent_saves_leave_a_complete_valid_file(self):
        ops.bind_durable_registry(self.lib)
        threads = [threading.Thread(target=self.finish, args=(f"k{i}",)) for i in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with open(self.file, encoding="utf-8") as fh:
            self.assertEqual({e["operation_id"] for e in json.load(fh)}, {f"k{i}" for i in range(40)})
        self.assertFalse(os.path.exists(self.file + ".tmp"))

    def test_only_final_folder_ops_are_saved_and_reloaded(self):
        ops.bind_durable_registry(self.lib)
        self.finish("done")
        self.finish("imp", op_type="import")
        ops.register_operation("folder_op", "running", fingerprint="fp")
        ops.update_operation("running", "running")
        self.restart()
        self.assertEqual(set(ops._operations), {"done"})

    def test_truncated_file_is_ignored(self):
        ops.bind_durable_registry(self.lib)
        self.finish("a")
        with open(self.file, "r+", encoding="utf-8") as fh:
            fh.truncate(20)
        self.restart()
        self.assertEqual(ops._operations, {})
        self.finish("b")  # the next save rewrites a valid file
        with open(self.file, encoding="utf-8") as fh:
            self.assertEqual([e["operation_id"] for e in json.load(fh)], ["b"])

    def test_expired_entries_are_dropped_on_load(self):
        old = time.time() - ops.DURABLE_RETENTION_SECONDS - 60
        with open(self.file, "w", encoding="utf-8") as fh:
            json.dump([{"operation_id": "old", "type": "folder_op", "status": "succeeded",
                        "created_at": old, "updated_at": old}], fh)
        ops.bind_durable_registry(self.lib)
        self.assertNotIn("old", ops._operations)

    def test_cap_is_shared_with_non_durable_operations(self):
        """Documents a ceiling: the 1000-entry cap counts every completed
        operation, so a burst of non-durable ones (imports, modifies) evicts
        folder-op outcomes before their 7-day TTL."""
        ops.bind_durable_registry(self.lib)
        self.finish("fold")
        with mock.patch.object(ops, "MAX_COMPLETED_OPERATIONS", 5):
            for i in range(6):
                self.finish(f"imp{i}", op_type="import")
        self.assertNotIn("fold", ops._operations)


if __name__ == "__main__":
    unittest.main()
