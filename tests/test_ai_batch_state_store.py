"""Unit and concurrency tests for AiBatchStateStore (ARCH-010)."""

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from backend.ai_batch_state_store import (
    AiBatchStateConflictError,
    AiBatchStateStore,
    AiBatchStateUnsupportedSchemaError,
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

    def test_update_without_expected_revision_is_rejected(self):
        """Wave 26 / ARCH-010 correction: omitting expected_revision against
        an existing row used to silently trust whatever revision the store
        had just read internally, defeating CAS for a caller holding a
        genuinely stale snapshot. It must now be a hard error, not a
        successful overwrite."""
        state = {"batch_job_id": "batch-101", "status": "running"}
        self.store.save_batch_state(state)

        stale_state = {"batch_job_id": "batch-101", "status": "stale-overwrite-attempt"}
        with self.assertRaises(ValueError):
            self.store.save_batch_state(stale_state)

        # And the real row must be completely untouched by the rejected call.
        final_state = self.store.get_batch_state("batch-101")
        self.assertEqual(final_state["revision"], 1)
        self.assertEqual(final_state["status"], "running")

    def test_concurrent_create_race_normalizes_to_conflict_error(self):
        """Two callers both observing 'row does not exist' and both racing
        to INSERT must both receive AiBatchStateConflictError -- never a raw
        sqlite3.IntegrityError leaking out of the store's own contract."""
        store_a = self.store
        store_b = AiBatchStateStore(self.db_path)
        state = {"batch_job_id": "batch-race", "status": "queued"}

        store_a.save_batch_state(state, expected_revision=0)
        with self.assertRaises(AiBatchStateConflictError):
            store_b.save_batch_state(state, expected_revision=0)

        final_state = self.store.get_batch_state("batch-race")
        self.assertEqual(final_state["revision"], 1)

    def test_create_batch_state_rejects_existing_row(self):
        state = {"batch_job_id": "batch-created-once", "status": "queued"}
        self.store.create_batch_state(state)
        with self.assertRaises(AiBatchStateConflictError):
            self.store.create_batch_state(state)

    def test_unsupported_future_schema_version_is_rejected_not_loaded(self):
        """Storing an integer in schema_version is not schema-version
        management by itself -- the reader must actively reject a row whose
        schema_version it does not recognize, rather than silently decoding
        the payload under the current schema's assumptions."""
        state = {"batch_job_id": "batch-future-schema", "status": "queued"}
        self.store.save_batch_state(state)

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "UPDATE ai_batch_state SET schema_version = 999 WHERE id = ?",
                ("batch-future-schema",),
            )
            conn.commit()

        with self.assertRaises(AiBatchStateUnsupportedSchemaError):
            self.store.get_batch_state("batch-future-schema")

        # A listing spanning many batches must exclude the unreadable row
        # rather than crash entirely or silently decode it.
        other_state = {"batch_job_id": "batch-normal-schema", "status": "queued"}
        self.store.save_batch_state(other_state)
        listed_ids = {s["batch_job_id"] for s in self.store.list_batch_states()}
        self.assertIn("batch-normal-schema", listed_ids)
        self.assertNotIn("batch-future-schema", listed_ids)

    def test_real_cross_store_concurrent_cas_update(self):
        """Required scenario (not the sequential test above): two
        independently-opened stores (simulating two workers/processes) both
        read the same revision, then race an update against it using a real
        synchronization barrier so the attempts are genuinely concurrent,
        not sequential. Exactly one must succeed, the other must receive
        AiBatchStateConflictError, the final revision must be N+1 (not N+2
        -- proving the loser never partially applied), and the winning
        payload -- not the loser's -- must be what persists."""
        store_a = self.store
        store_b = AiBatchStateStore(self.db_path)

        initial = {"batch_job_id": "batch-cross-worker", "status": "queued", "writer": "init"}
        store_a.save_batch_state(initial, expected_revision=0)

        state_at_a = store_a.get_batch_state("batch-cross-worker")
        state_at_b = store_b.get_batch_state("batch-cross-worker")
        self.assertEqual(state_at_a["revision"], state_at_b["revision"])
        n = state_at_a["revision"]

        barrier = threading.Barrier(2)
        results: dict = {}

        def _attempt(name, store, state):
            state = dict(state)
            state["writer"] = name
            barrier.wait(timeout=5)
            try:
                results[name] = ("ok", store.save_batch_state(state, expected_revision=n))
            except AiBatchStateConflictError as ex:
                results[name] = ("conflict", ex)

        t_a = threading.Thread(target=_attempt, args=("worker-a", store_a, state_at_a))
        t_b = threading.Thread(target=_attempt, args=("worker-b", store_b, state_at_b))
        t_a.start()
        t_b.start()
        t_a.join(timeout=10)
        t_b.join(timeout=10)

        outcomes = [results["worker-a"][0], results["worker-b"][0]]
        self.assertEqual(sorted(outcomes), ["conflict", "ok"], results)

        winner = "worker-a" if results["worker-a"][0] == "ok" else "worker-b"
        loser = "worker-b" if winner == "worker-a" else "worker-a"

        final_state = self.store.get_batch_state("batch-cross-worker")
        self.assertEqual(final_state["revision"], n + 1)
        self.assertEqual(final_state["writer"], winner)
        self.assertNotEqual(final_state["writer"], loser)

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

        result = self.store.migrate_legacy_files(state_dir, decisions_file)
        self.assertGreater(result["migrated"], 0)
        self.assertEqual(result["errors"], [])

        migrated_state = self.store.get_batch_state("legacy-99")
        self.assertIsNotNone(migrated_state)
        self.assertEqual(migrated_state["folders_processed"], 3)
        self.assertEqual(migrated_state["revision"], 1)

        self.assertEqual(len(self.store.list_review_decisions()), 1)

        # Backup file created
        self.assertTrue((state_dir / "legacy-99.json.migrated.bak").exists())
        self.assertFalse(legacy_file.exists())

    def test_migration_is_idempotent_across_a_simulated_crash(self):
        """Wave 26 / ARCH-010 (section 9): the decisions migration used to
        insert every decision and only afterward rename the source file --
        a crash between those two steps meant re-running migration would
        insert the exact same decisions again. Simulates exactly that
        crash window: migrate once, then (without ever renaming, as if the
        process died right after the inserts) call migrate_legacy_files
        again against the same still-present source file, and require no
        duplicate rows."""
        state_dir = Path(self.tmpdir) / "legacy_jobs"
        state_dir.mkdir(parents=True, exist_ok=True)
        decisions_file = Path(self.tmpdir) / "decisions.json"
        decisions_data = [
            {"album_id": 10, "mb_albumid": "mbid-10", "decision": "accepted"},
            {"album_id": 11, "mb_albumid": "mbid-11", "decision": "rejected"},
        ]
        decisions_file.write_text(json.dumps(decisions_data), encoding="utf-8")

        first = self.store.migrate_legacy_files(state_dir, decisions_file)
        self.assertEqual(first["migrated"], 2)
        self.assertEqual(len(self.store.list_review_decisions()), 2)

        # Simulate "crashed before rename": recreate the un-renamed source
        # file exactly as it was, and migrate again.
        decisions_file.write_text(json.dumps(decisions_data), encoding="utf-8")
        second = self.store.migrate_legacy_files(state_dir, decisions_file)

        self.assertEqual(second["migrated"], 0, "already-migrated decisions must not be re-inserted")
        self.assertEqual(second["errors"], [])
        self.assertEqual(
            len(self.store.list_review_decisions()), 2,
            "re-running migration after a simulated crash must not create duplicate review decisions",
        )

    def test_corrupt_legacy_file_is_preserved_and_reported_not_discarded(self):
        """Corrupt legacy data must remain on disk (never silently deleted
        or discarded) and must produce a visible error entry."""
        state_dir = Path(self.tmpdir) / "legacy_jobs"
        state_dir.mkdir(parents=True, exist_ok=True)
        corrupt_file = state_dir / "corrupt.json"
        corrupt_file.write_text("{not valid json at all", encoding="utf-8")

        result = self.store.migrate_legacy_files(state_dir)

        self.assertEqual(result["migrated"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("corrupt.json", result["errors"][0]["file"])
        # Preserved, not renamed to .migrated.bak and not deleted.
        self.assertTrue(corrupt_file.exists())
        self.assertFalse((state_dir / "corrupt.json.migrated.bak").exists())


if __name__ == "__main__":
    unittest.main()
