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
        self.assertEqual([n for n in os.listdir(self.td) if n.startswith(ops.REGISTRY_FILENAME)], [ops.REGISTRY_FILENAME])

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

    def test_durable_outcomes_have_their_own_cap(self):
        """#308 item 5: a burst of non-durable operations (imports, modifies)
        never evicts a folder-op outcome before its 7-day TTL."""
        ops.bind_durable_registry(self.lib)
        self.finish("fold")
        with mock.patch.object(ops, "MAX_COMPLETED_OPERATIONS", 5):
            for i in range(6):
                self.finish(f"imp{i}", op_type="import")
            with ops._operations_lock:
                ops._prune_operations_locked()
            self.assertIn("fold", ops._operations)
            self.assertEqual(sum(1 for k in ops._operations if k.startswith("imp")), 5)
            for i in range(6):
                self.finish(f"f{i}")
            self.assertNotIn("fold", ops._operations)  # durable entries keep their own bound

    def test_save_never_follows_a_planted_tmp_symlink_and_is_private(self):
        """#308 items 1-2."""
        ops.bind_durable_registry(self.lib)
        victim = os.path.join(self.td, "victim.txt")
        with open(victim, "w") as fh:
            fh.write("keep")
        try:
            os.symlink(victim, self.file + ".tmp")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable here (Linux CI runs this)")
        self.finish("a")
        with open(victim) as fh:
            self.assertEqual(fh.read(), "keep")
        if os.name == "posix":
            self.assertEqual(os.stat(self.file).st_mode & 0o777, 0o600)

    def test_failed_save_removes_its_temp_file(self):
        ops.bind_durable_registry(self.lib)
        with mock.patch.object(ops.json, "dump", side_effect=ValueError("boom")):
            self.finish("a")
        self.assertEqual([n for n in os.listdir(self.td) if n.startswith(ops.REGISTRY_FILENAME)], [])

    def test_deeply_nested_file_is_ignored(self):
        """#308 item 3: a RecursionError on load is caught."""
        with open(self.file, "w", encoding="utf-8") as fh:
            fh.write("[" * 100000 + "]" * 100000)
        ops.bind_durable_registry(self.lib)
        self.assertEqual(ops._operations, {})

    def test_non_finite_timestamps_and_bad_keys_are_dropped_on_load(self):
        """#308 items 4 and 6."""
        now = time.time()
        rows = [{"operation_id": k, "type": "folder_op", "status": "succeeded", "created_at": c, "updated_at": u}
                for k, c, u in (("inf", now, "inf"), ("nan", "nan", now), ("bad key/x", now, now), ("ok", now, now))]
        with open(self.file, "w", encoding="utf-8") as fh:
            json.dump(rows, fh)
        ops.bind_durable_registry(self.lib)
        self.assertEqual(set(ops._operations), {"ok"})


class IdempotencyKeyValidationTests(unittest.TestCase):
    """#308 item 6: the plugin refuses an unbounded or odd Idempotency-Key
    before anything runs, and the adapter maps caller keys onto the rule."""

    def test_plugin_refuses_long_or_odd_keys(self):
        from flask import Flask
        app = Flask(__name__)
        for key, ok in (("txn_1_abc:apply:3", True), ("k" * 200, True), ("k" * 201, False),
                        ("a b", False), ("a/b", False), ("", False)):
            with app.test_request_context("/webmanager/folder-op", method="POST",
                                          headers={"Idempotency-Key": key}):
                with mock.patch.object(ops, "bind_music_dir"), mock.patch.object(ops, "bind_durable_registry"):
                    res = ops._bind_library_music_dir()
                if ok:
                    self.assertIsNone(res, key)
                else:
                    self.assertEqual((res[1], res[0].get_json()["error_code"]), (400, "INVALID_IDEMPOTENCY_KEY"), key)

    def test_backend_keys_match_and_caller_keys_are_mapped(self):
        import backend.beets_adapter as ba
        sent = {}

        class _Resp:
            status = 200
            headers = {"Content-Type": "application/json"}

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            sent["key"] = req.headers.get("Idempotency-key")
            return _Resp()

        ad = ba.BeetsAdapter(base_url="http://beets:8337", api_key="t")
        with mock.patch.object(ba.urllib.request, "urlopen", side_effect=fake_urlopen):
            for key in ("txn_1791493807_5801d3f3b547", "txn_1_a:apply:12", "txn_1_a:rollback",
                        "mbsync-library-txn_1_a", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"):
                ad._request("POST", "/webmanager/folder-op", json_data={}, headers={"Idempotency-Key": key})
                self.assertEqual(sent["key"], key)
            ad._request("POST", "/webmanager/folder-op", json_data={}, headers={"Idempotency-Key": "a user key/é"})
            self.assertRegex(sent["key"], r"^k-[0-9a-f]{64}$")
            first = sent["key"]
            ad._request("POST", "/webmanager/folder-op", json_data={}, headers={"Idempotency-Key": "a user key/é"})
            self.assertEqual(sent["key"], first)  # deterministic: a retry still replays



if __name__ == "__main__":
    unittest.main()
