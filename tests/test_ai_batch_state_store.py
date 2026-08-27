"""Unit and concurrency tests for AiBatchStateStore (ARCH-010)."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from backend.ai_batch_state_store import (
    AiBatchStateConflictError,
    AiBatchStateStore,
)


class AiBatchStateStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "ai_batch_state.db"
        self.store = AiBatchStateStore(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_create_and_get_batch_state(self):
        state = {
            "batch_job_id": "batch-101",
            "job_id": "job-202",
            "status": "running",
            "total_folders_found": 5,
        }
        saved = self.store.save_batch_state(state)
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(saved["schema_version"], 1)

        fetched = self.store.get_batch_state("batch-101")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["batch_job_id"], "batch-101")
        self.assertEqual(fetched["job_id"], "job-202")
        self.assertEqual(fetched["status"], "running")
        self.assertEqual(fetched["total_folders_found"], 5)
        self.assertEqual(fetched["revision"], 1)

    def test_cas_increment(self):
        state = {"batch_job_id": "batch-101", "status": "running"}
        saved1 = self.store.save_batch_state(state)
        self.assertEqual(saved1["revision"], 1)

        saved1["status"] = "completed"
        saved2 = self.store.save_batch_state(saved1, expected_revision=1)
        self.assertEqual(saved2["revision"], 2)

        fetched = self.store.get_batch_state("batch-101")
        self.assertEqual(fetched["revision"], 2)
        self.assertEqual(fetched["status"], "completed")

    def test_cas_conflict_detection(self):
        state = {"batch_job_id": "batch-101", "status": "running"}
        saved = self.store.save_batch_state(state)
        self.assertEqual(saved["revision"], 1)

        # Worker A reads revision 1
        worker_a_state = self.store.get_batch_state("batch-101")
        # Worker B reads revision 1
        worker_b_state = self.store.get_batch_state("batch-101")

        # Worker B updates successfully -> revision 2
        worker_b_state["status"] = "completed"
        self.store.save_batch_state(worker_b_state, expected_revision=1)

        # Worker A attempts update with stale expected_revision=1 -> must raise conflict!
        worker_a_state["status"] = "failed"
        with self.assertRaises(AiBatchStateConflictError):
            self.store.save_batch_state(worker_a_state, expected_revision=1)

        # Verify revision 2 remains intact and status is 'completed' (Worker A's update was rejected)
        final_state = self.store.get_batch_state("batch-101")
        self.assertEqual(final_state["revision"], 2)
        self.assertEqual(final_state["status"], "completed")

    def test_multi_connection_concurrency(self):
        store2 = AiBatchStateStore(self.db_path)

        state = {"batch_job_id": "batch-300", "status": "queued"}
        saved = self.store.save_batch_state(state)

        # Connection 2 updates
        saved["status"] = "running"
        saved2 = store2.save_batch_state(saved, expected_revision=1)
        self.assertEqual(saved2["revision"], 2)

        # Connection 1 attempts write using revision 1 -> conflict
        saved["status"] = "cancelled"
        with self.assertRaises(AiBatchStateConflictError):
            self.store.save_batch_state(saved, expected_revision=1)

    def test_one_way_migration(self):
        state_dir = Path(self.tmpdir) / "legacy_jobs"
        state_dir.mkdir(parents=True, exist_ok=True)

        legacy_file = state_dir / "legacy-99.json"
        legacy_data = {
            "batch_job_id": "legacy-99",
            "job_id": "job-99",
            "status": "completed",
            "folders_processed": 3,
        }
        legacy_file.write_text(json.dumps(legacy_data), encoding="utf-8")

        decisions_file = Path(self.tmpdir) / "decisions.json"
        decisions_data = [{"album_id": 10, "mb_albumid": "mbid-10", "decision": "accepted"}]
        decisions_file.write_text(json.dumps(decisions_data), encoding="utf-8")

        migrated = self.store.migrate_legacy_files(state_dir, decisions_file)
        self.assertGreater(migrated, 0)

        migrated_state = self.store.get_batch_state("legacy-99")
        self.assertIsNotNone(migrated_state)
        self.assertEqual(migrated_state["folders_processed"], 3)
        self.assertEqual(migrated_state["revision"], 1)

        # Backup file created
        self.assertTrue((state_dir / "legacy-99.json.migrated.bak").exists())
        self.assertFalse(legacy_file.exists())


if __name__ == "__main__":
    unittest.main()
