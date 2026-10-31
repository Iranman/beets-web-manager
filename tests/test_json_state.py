"""The canonical atomic state writer, against a real filesystem."""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from backend.json_state import atomic_write_json, load_json_object
from backend.transaction_engine import TransactionStore


class AtomicWriteJsonTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "nested" / "state.json"

    def test_creates_parents_and_round_trips(self):
        atomic_write_json(self.path, {"b": 1, "a": "é"}, ensure_ascii=False, sort_keys=True)
        self.assertEqual(self.path.read_text(encoding="utf-8"), '{"a": "é", "b": 1}')
        self.assertEqual(load_json_object(self.path), {"a": "é", "b": 1})

    def test_unserializable_payload_keeps_the_old_document(self):
        atomic_write_json(self.path, {"v": 1})
        with self.assertRaises(TypeError):
            atomic_write_json(self.path, {"v": object()})
        self.assertEqual(load_json_object(self.path), {"v": 1})
        self.assertEqual(os.listdir(self.path.parent), ["state.json"])

    def test_refused_replace_keeps_the_old_document_and_leaves_no_temp(self):
        atomic_write_json(self.path, {"v": 1})
        with mock.patch("backend.json_state.os.replace", side_effect=PermissionError("in use")):
            with self.assertRaises(PermissionError):
                atomic_write_json(self.path, {"v": 2})
        self.assertEqual(load_json_object(self.path), {"v": 1})
        self.assertEqual(os.listdir(self.path.parent), ["state.json"])

    def test_concurrent_writers_never_share_a_temp_file(self):
        # The shared fixed ".tmp" name this replaced let one writer's replace
        # consume another's temp file. Replace can hit a transient sharing error
        # on Windows, which is not what this asserts, so only temp collisions
        # (FileExistsError, FileNotFoundError) count as failures.
        failures = []

        def write(n):
            for i in range(25):
                try:
                    atomic_write_json(self.path, {"writer": n, "i": i})
                except PermissionError:
                    pass
                except OSError as exc:
                    failures.append(exc)

        threads = [threading.Thread(target=write, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(failures, [])
        self.assertEqual(os.listdir(self.path.parent), ["state.json"])
        self.assertIn("writer", json.loads(self.path.read_text(encoding="utf-8")))


class LoadJsonObjectTests(unittest.TestCase):
    def test_every_bad_document_loads_as_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            self.assertEqual(load_json_object(p), {})  # missing
            for bad in ("{not json", "[1, 2]", ""):
                p.write_text(bad, encoding="utf-8")
                self.assertEqual(load_json_object(p), {}, bad)
            p.write_bytes(b"\xff\xfe\x00")
            self.assertEqual(load_json_object(p), {})  # undecodable


class TransactionStoreDurabilityTests(unittest.TestCase):
    def test_transition_is_fsynced_and_cas_still_refuses_a_stale_status(self):
        with tempfile.TemporaryDirectory() as d:
            store = TransactionStore(Path(d))
            tx = store.create(operation_type="Delete", status="Preview")
            with mock.patch("backend.json_state.os.fsync", wraps=os.fsync) as fsync:
                self.assertIsNotNone(store.transition(tx["id"], tx["status"], "Approved"))
            self.assertGreaterEqual(fsync.call_count, 1)
            self.assertIsNone(store.transition(tx["id"], tx["status"], "Approved"))
            self.assertEqual(store.get(tx["id"])["status"], "Approved")
            self.assertFalse([n for n in os.listdir(d) if n.endswith(".tmp")])


if __name__ == "__main__":
    unittest.main()
