"""Behavior the M2a consolidations must keep or newly guarantee: reading a
transaction writes nothing, a job's end reaches its transaction once, the shared
folder runner holds a tracked refusal, the stamp job reports failure truthfully,
and every reviewed-cleanup route cancels what it cannot start."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import backend.composite_workflows as cw
import backend.dedup_service as dedup_service
import backend.library_service as ls
import backend.maintenance_service as ms
import backend.transaction_service as ts
from job_engine import JobStore
from tests.test_staging_mutation_hardening import _RouteEnv
from tests.test_wave0_s1_containment import FakeAdapter


class TransactionReadsAreReadOnlyTests(_RouteEnv):
    def _record_state(self):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.store.root.glob("*.json")}

    def test_get_routes_leave_records_byte_identical_and_mtime_unchanged(self):
        running = self.store.create(operation_type="Delete", status="Running")
        done = self.store.create(operation_type="Delete", status="Completed")
        # A finished job for each: the old GET sync rewrote both records.
        finished = [SimpleNamespace(status="success", job_id=f"j{i}", result={"ok": True}, log=["line"],
                                    metadata={"transaction_id": tx["id"]})
                    for i, tx in enumerate((running, done))]
        before = self._record_state()
        self.assertEqual(len(before), 2)
        time.sleep(0.01)
        with mock.patch.object(ts.jobs, "all", return_value=finished):
            listed = self.client.get("/api/transactions")
            detail = self.client.get(f"/api/transactions/{running['id']}")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()["total"], 2)
        self.assertEqual(detail.get_json()["transaction"]["status"], "Running")
        self.assertIn("cancel", detail.get_json()["transaction"])
        self.assertEqual(self._record_state(), before)


class JobEndReachesTransactionTests(_RouteEnv):
    def _run_job(self, body, jobs):
        with mock.patch.object(ts, "jobs", jobs), mock.patch.object(ts, "transactions", self.store):
            ts._install_transaction_job_hooks()
            job = jobs.start_python(body, label="Library cleanup",
                                    metadata={"transaction": {"operation_type": "Delete"}})
            deadline = time.time() + 5
            tx_id = job.metadata["transaction_id"]
            while time.time() < deadline:
                # The callback is the last thing the job thread does.
                if job.status != "running" and self.store.get(tx_id).get("result_summary") is not None:
                    break
                time.sleep(0.01)
        return job, tx_id

    def test_final_job_state_is_recorded_without_any_read(self):
        def body(log, cancel_event=None):
            log.append("did the work")
            return {"ok": True, "changed_count": 2}

        job, tx_id = self._run_job(body, JobStore(self.data / "jobs"))
        tx = self.store.get(tx_id)
        self.assertEqual(tx["status"], "Completed")
        self.assertIn("did the work", tx["logs"])
        self.assertEqual(tx["originating_job"], job.job_id)

    def test_settled_sync_keeps_transaction_notes_and_rewrites_nothing(self):
        job, tx_id = self._run_job(lambda log, cancel_event=None: {"ok": True},
                                   JobStore(self.data / "jobs"))
        self.store.append_log(tx_id, "audit note from the transaction")
        self.store.update_from_job(tx_id, job)
        self.assertIn("audit note from the transaction", self.store.get(tx_id)["logs"])
        path = self.store.root / f"{tx_id}.json"
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        time.sleep(0.01)
        self.store.update_from_job(tx_id, job)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_job_interrupted_by_a_restart_is_settled_at_startup(self):
        tx = self.store.create(operation_type="Delete", status="Running")
        job = SimpleNamespace(status="recovery_required", job_id="j1", result=None, log=[],
                              metadata={"transaction_id": tx["id"]})
        jobs = mock.MagicMock(_transaction_hooks_installed=False, recovered=[{"job_id": "j1"}])
        jobs.get.return_value = job
        with mock.patch.object(ts, "jobs", jobs), mock.patch.object(ts, "transactions", self.store):
            ts._install_transaction_job_hooks()
        self.assertEqual(self.store.get(tx["id"])["status"], "Recovery Required")


class OrphanRowRemovalAuditTests(unittest.TestCase):
    def test_record_keeps_every_field_needed_to_reattach_a_row(self):
        from tests.test_album_relocation_transaction import _Engine
        eng = _Engine()
        eng.setUp()
        self.addCleanup(eng.doCleanups)
        album = eng.make_album(os.path.join(eng.music, "Artist", "Album"))
        item = next(iter(album.items()))
        item.mb_trackid = "11111111-1111-1111-1111-111111111111"
        item.store()
        item_id, path = item.id, os.fsdecode(item.path)
        (Path(eng.music) / "keep.txt").write_text("mount is not empty")
        os.remove(path)
        with mock.patch.dict(os.environ, {"MUSIC_ROOT": eng.music}), \
                mock.patch.object(cw, "MISSING_ROW_RATIO_CAP", 1.1):  # a two-row fixture library
            res = cw.clean_orphaned_items([item_id], dry_run=False, adapter=eng.ad, store=eng.store)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["removed_count"], 1)
        self.assertIsNone(eng.ad.get_item(item_id))
        tx = eng.store.get(res["operation_id"])
        self.assertEqual(tx["changes"][0]["item_id"], item_id)
        self.assertFalse(tx["rollback"]["available"])
        snap = tx["metadata"]["row_snapshots"][0]
        for field in ("id", "album_id", "path", "title", "artist", "album", "track", "disc", "mb_trackid"):
            self.assertIn(field, snap)
        self.assertEqual(snap["mb_trackid"], "11111111-1111-1111-1111-111111111111")


class PlaceholderTrackedRefusalTests(unittest.TestCase):
    def test_plugin_refusal_is_held_and_refunds_the_batch_budget(self):
        with tempfile.TemporaryDirectory() as d:
            first, second = Path(d) / "A{Album MbId}", Path(d) / "B{Album MbId}"
            first.mkdir()
            second.mkdir()
            results = [{"ok": False, "code": "folder_cleanup_rename_failed", "error": "tracked",
                        "step_error_code": "PATH_IS_TRACKED"},
                       {"ok": True, "operation_id": "op-2"}]
            with mock.patch.object(cw, "safe_rename_library_folder", side_effect=results) as rename:
                res = ls._apply_folder_name_placeholders(
                    [str(first), str(second)], dry_run=False, batch_limit=1,
                    adapter=FakeAdapter(), store=mock.MagicMock())
        self.assertTrue(res["ok"], res)
        self.assertEqual(rename.call_count, 2)  # the refusal consumed no budget
        self.assertEqual(res["skipped"][0]["reason"], "became_tracked_needs_relocation")
        self.assertEqual(res["operations"], [{"operation_id": "op-2", "kind": "folder"}])
        self.assertEqual(res["done_folders"], [str(second)])


class StampJobOutcomeTests(unittest.TestCase):
    """The route's job IS the Clean All step; a failure must not look like success."""

    def _run_job(self, **route_json):
        import app as app_module
        with tempfile.TemporaryDirectory() as d:
            captured = {}
            with mock.patch("routes_cleanup._artist_folder_repair_root", return_value=(Path(d), "")), \
                    mock.patch.object(app_module.jobs, "start_python",
                                      side_effect=lambda fn, label="", metadata=None:
                                      captured.update(fn=fn) or mock.MagicMock(job_id="j")):
                with app_module.app.test_request_context(
                        "/api/clean/artist-folders/stamp-mbid", method="POST",
                        json={"dry_run": False, **route_json}):
                    app_module.clean_artist_folders_stamp_mbid()
            return captured["fn"]

    def _scan(self):
        return mock.patch.object(ms, "_stamp_artist_folder_scan",
                                 return_value={"ok": True, "candidates": [{"name": "A"}], "skipped": []})

    def test_refused_plan_fails_the_job(self):
        fn = self._run_job()
        with self._scan(), mock.patch.object(cw, "plan_artist_folder_reconcile",
                                             return_value={"ok": False, "error": "refused"}):
            with self.assertRaisesRegex(RuntimeError, "refused"):
                fn([])

    def test_failed_apply_returns_the_operation_on_a_failed_result(self):
        fn = self._run_job()
        with self._scan(), mock.patch.object(cw, "plan_artist_folder_reconcile",
                                             return_value={"ok": True, "operation_id": "op-1"}), \
                mock.patch.object(ms, "_apply_artist_folder_reconcile_resilient",
                                  return_value={"ok": False, "error": "lost", "still_running": True}):
            res = fn([])
        self.assertIs(res["ok"], False)
        self.assertEqual(res["operation_id"], "op-1")
        self.assertTrue(res["still_running"])

    def test_success_keeps_the_route_summary_and_passes_the_failpoint(self):
        fn = self._run_job(_acceptance_failpoint="fp")
        log = []
        with self._scan(), mock.patch.object(cw, "plan_artist_folder_reconcile",
                                             return_value={"ok": True, "operation_id": "op-1"}), \
                mock.patch.object(ms, "_invalidate_lib_cache"), \
                mock.patch.object(ms, "_apply_artist_folder_reconcile_resilient",
                                  return_value={"ok": True, "moved_files": 3, "quarantined_files": 1,
                                                "removed_dirs": 2}) as apply_:
            res = fn(log)
        self.assertEqual(apply_.call_args.kwargs["_acceptance_failpoint"], "fp")
        self.assertEqual((res["renamed"], res["merged"], res["files_moved"], res["folders_removed"]), (3, 1, 3, 2))
        self.assertTrue(any(line.startswith("Done: 3 renamed") for line in log), log)


class ReviewedCleanupApprovalTests(unittest.TestCase):
    def setUp(self):
        from backend.transaction_engine import TransactionStore
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = TransactionStore(tmp.name)
        p = mock.patch.object(cw, "_default_store", self.store)
        p.start()
        self.addCleanup(p.stop)

    def test_apply_that_raises_before_claiming_cancels_the_plan(self):
        tx = self.store.create(operation_type="Delete", status="Preview")
        with mock.patch.object(dedup_service._duplicate_cleanup, "apply_reviewed_cleanup",
                               side_effect=RuntimeError("engine down")):
            with self.assertRaises(RuntimeError):
                dedup_service._approve_and_apply_reviewed_cleanup(tx["id"], "operator")
        self.assertEqual(self.store.get(tx["id"])["status"], "Cancelled")

    def test_cancelled_plan_is_never_applied(self):
        tx = self.store.create(operation_type="Delete", status="Cancelled")
        with mock.patch.object(dedup_service._duplicate_cleanup, "apply_reviewed_cleanup") as apply_:
            self.assertIsNone(dedup_service._approve_and_apply_reviewed_cleanup(tx["id"], "operator"))
        apply_.assert_not_called()

    def test_album_dedup_refuses_with_nothing_applied_when_the_plan_was_cancelled(self):
        items = [{"id": 1, "track": 3, "disc": 1, "path": "/m/a.flac"},
                 {"id": 2, "track": 3, "disc": 1, "path": "/m/a.1.flac"}]
        tx = self.store.create(operation_type="Delete", status="Cancelled")
        with mock.patch.object(dedup_service, "same_recording_proof",
                               return_value={"proven": True, "recording_id": "r", "reason": ""}), \
                mock.patch.object(dedup_service._duplicate_cleanup, "plan_reviewed_cleanup",
                                  return_value={"ok": True, "operation_id": tx["id"]}), \
                mock.patch.object(dedup_service._duplicate_cleanup, "apply_reviewed_cleanup") as apply_:
            with self.assertRaisesRegex(RuntimeError, "nothing was quarantined"):
                dedup_service._album_dedup_quarantine_proven(3, items, True, [])
        apply_.assert_not_called()


if __name__ == "__main__":
    unittest.main()
