import inspect
import json
import threading
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import app as app_module


class InlineJob:
    def __init__(self, job_id, fn, label="", metadata=None):
        self.job_id = job_id
        self._fn = fn
        self.label = label or ""
        self.metadata = metadata or {}
        self.log = []
        self.result = None
        self.state = {}
        self.created_at = time.time()
        self.started_at = None
        self.finished_at = None
        self.returncode = None
        self._cancel = threading.Event()

    @property
    def status(self):
        if self.finished_at is None:
            return "running"
        return "success" if self.returncode == 0 else "failed"

    def update_state(self, updates=None, **kwargs):
        payload = {}
        if updates:
            payload.update(updates)
        payload.update(kwargs)
        self.state.update(payload)

    def kill(self):
        self._cancel.set()
        self.log.append("[cancel requested]")
        if self.finished_at is None:
            self.returncode = 1
            self.finished_at = time.time()

    def run(self):
        self.started_at = time.time()
        try:
            sig = inspect.signature(self._fn)
            if len(sig.parameters) >= 3:
                ret = self._fn(self.log, self._cancel, self.update_state)
            elif len(sig.parameters) >= 2:
                ret = self._fn(self.log, self._cancel)
            else:
                ret = self._fn(self.log)
            if ret is not None:
                self.result = ret
            self.returncode = 0
        except Exception as exc:
            self.log.append(f"ERROR: {exc}")
            self.returncode = 1
        finally:
            self.finished_at = time.time()
        return self


class InlineJobStore:
    def __init__(self):
        self._jobs = {}
        self.started = []
        self._next_id = 1

    def start_python(self, fn, label="", metadata=None):
        job = InlineJob(f"inline-{self._next_id}", fn, label=label, metadata=metadata)
        self._next_id += 1
        self._jobs[job.job_id] = job
        self.started.append(job)
        return job.run()

    def get(self, job_id):
        return self._jobs.get(job_id)

    def all(self):
        return list(self.started)

    def prune_finished(self, **_kwargs):
        return None


def _task_results():
    return {
        "library_health": {
            "ok": True,
            "duplicate_album_count": 0,
            "rgid_duplicate_group_count": 0,
            "orphaned_item_count": 0,
            "empty_album_count": 0,
        },
        "missing_files": {
            "ok": True,
            "missing_files": 0,
            "removed_db_rows": 0,
            "final_summary": {"missing_files": 0, "removed_db_rows": 0},
        },
        "root_folder_repair": {
            "ok": True,
            "summary": {"items_moved": 0},
            "final_summary": {"items_moved": 0},
        },
        "artist_alias": {
            "ok": True,
            "count": 0,
            "groups": [],
            "final_summary": {"count": 0},
        },
    }


def _checkpoint_with_first_four_complete(path: Path):
    completed = set(_task_results())
    tasks = []
    for task in app_module._maintenance_initial_task_state():
        item = dict(task)
        if item["id"] in completed:
            item["status"] = "complete"
            item["detail"] = "restored from checkpoint"
        tasks.append(item)
    results = _task_results()
    report = dict(results)
    report["last_run"] = {
        "status": "partial",
        "workflow": "clean-all",
        "started_at": time.time() - 60,
        "updated_at": time.time() - 30,
        "completed_task_ids": list(completed),
        "next_task": "artist_folder_merge",
        "next_task_label": "Artist Folder Merge",
        "tasks": tasks,
        "results": results,
        "result_task_ids": sorted(results),
    }
    path.write_text(json.dumps(report), encoding="utf-8")


def _last_run(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))["last_run"]


class CleanAllResumeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.checkpoint = Path(self.tmpdir.name) / "maintenance-runner-last.json"
        self.music_root = Path(self.tmpdir.name) / "music"
        self.music_root.mkdir()
        self.store = InlineJobStore()
        self.call_order = []
        self.client = app_module.app.test_client()
        self.patchers = []
        self._patch(mock.patch.object(app_module, "MAINTENANCE_RUNNER_LAST_FILE", self.checkpoint))
        self._patch(mock.patch.object(app_module, "MUSIC_ROOT", self.music_root))
        self._patch(mock.patch.object(app_module, "_security_auth_disabled", return_value=True))
        self._patch(mock.patch.object(app_module, "jobs", self.store))
        self._patch(mock.patch.object(app_module, "_scan_folder_name_placeholders", side_effect=self._scan_placeholders))
        self._patch(mock.patch.object(app_module, "_maintenance_release_group_merge", side_effect=self._release_group_merge))
        self._patch(mock.patch.object(app_module, "_maintenance_full_duplicate_scan", side_effect=self._duplicates))
        self._patch(mock.patch.object(app_module, "_maintenance_final_verification", side_effect=self._final_verification))
        self._patch(mock.patch.object(app_module, "fetch_missing_art", side_effect=self._child_route("artwork", {"saved": 0})))
        self._patch(mock.patch.object(app_module, "library_fix_genres", side_effect=self._child_route("genres", {"changed_count": 0})))
        self._patch(mock.patch.object(
            app_module,
            "playlist_sync_status",
            return_value=SimpleNamespace(get_json=lambda silent=True: {"enabled": False, "running": False}),
        ))

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()

    def _patch(self, patcher):
        self.patchers.append(patcher)
        return patcher.start()

    def _child_route(self, name, result):
        def route():
            job = app_module.jobs.start_python(lambda log, cancel_event=None: result, label=f"fake {name}", metadata={"type": name})
            return app_module.jsonify({"ok": True, "job_id": job.job_id})
        return route

    def _scan_placeholders(self, *args, **kwargs):
        self.call_order.append("folder_scan")
        scan_meta = kwargs.get("scan_meta")
        if isinstance(scan_meta, dict):
            scan_meta["total_folders_scanned"] = 0
        return []

    def _release_group_merge(self, *args, **kwargs):
        self.call_order.append("release_group_merge")
        return {"ok": True, "skipped": False, "final_summary": {"files_moved": 0, "folders_deleted": 0}}

    def _duplicates(self, *args, **kwargs):
        self.call_order.append("duplicates")
        return {"ok": True, "skipped": False, "final_summary": {"deleted_files": 0}}

    def _final_verification(self, *args, **kwargs):
        self.call_order.append("final_verification")
        return {"ok": True, "final_summary": {"manual_review_items": 0, "submission_queue_items": 0}}

    def _post_clean_all(self, force_fresh=False):
        payload = {"force_fresh": True} if force_fresh else {}
        response = self.client.post("/api/jobs/maintenance-runner", headers={"X-Beets-CSRF": "1"}, json=payload)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        self.assertTrue(data.get("ok"), data)
        parent = self.store.get(data["job_id"])
        self.assertIsNotNone(parent)
        return parent, data

    def test_resume_from_artist_folder_merge_checkpoint_does_not_reference_undefined_root_str(self):
        _checkpoint_with_first_four_complete(self.checkpoint)
        library_health = self._patch(mock.patch.object(app_module, "_library_health_payload", side_effect=AssertionError("library health reran")))
        missing_files = self._patch(mock.patch.object(app_module, "_maintenance_remove_missing_file_rows", side_effect=AssertionError("missing files reran")))
        root_repair = self._patch(mock.patch.object(app_module, "_maintenance_root_folder_repair", side_effect=AssertionError("root repair reran")))
        artist_alias = self._patch(mock.patch.object(app_module, "_artist_id_alias_groups", side_effect=AssertionError("artist alias reran")))
        self._patch(mock.patch.object(app_module, "_stamp_artist_folder_scan", side_effect=self._artist_scan_success))

        parent, data = self._post_clean_all()

        self.assertEqual(parent.returncode, 0, parent.log)
        self.assertTrue(data.get("resumed"), data)
        self.assertNotIn("root_str", "\n".join(parent.log))
        self.assertIn("artist_folder_merge", self._completed_ids(parent.result["tasks"]))
        self.assertEqual(library_health.call_count, 0)
        self.assertEqual(missing_files.call_count, 0)
        self.assertEqual(root_repair.call_count, 0)
        self.assertEqual(artist_alias.call_count, 0)
        self.assertEqual(self.call_order[:4], ["artist_folder_merge", "release_group_merge", "duplicates", "folder_scan"])
        self.assertIn("final_verification", self.call_order)
        # hotfix v0.1.17 follow-up (ARCH-020 / Clean All resume reattachment):
        # the artist_folder_merge step now calls
        # _maintenance_artist_folder_merge_step() directly instead of
        # spawning a child "stamp-mbid-folders" job via the HTTP route, so
        # it can persist the engine operation_id to the Clean All checkpoint
        # itself and detect/reattach to it on a later resume. No child job
        # of that type is expected anymore; _stamp_artist_folder_scan() (the
        # mocked call this test asserts on) is still invoked exactly once,
        # confirmed by self.call_order above.
        stamp_jobs = [job for job in self.store.started if job.metadata.get("type") == "stamp-mbid-folders"]
        self.assertEqual(len(stamp_jobs), 0)

    def test_fresh_clean_all_still_runs_all_phases(self):
        self._patch(mock.patch.object(app_module, "_library_health_payload", return_value=_task_results()["library_health"]))
        self._patch(mock.patch.object(app_module, "_maintenance_remove_missing_file_rows", return_value=_task_results()["missing_files"]))
        self._patch(mock.patch.object(app_module, "_maintenance_root_folder_repair", return_value=_task_results()["root_folder_repair"]))
        self._patch(mock.patch.object(app_module, "_artist_id_alias_groups", return_value=[]))
        self._patch(mock.patch.object(app_module, "_stamp_artist_folder_scan", side_effect=self._artist_scan_success))

        parent, data = self._post_clean_all(force_fresh=True)

        self.assertEqual(parent.returncode, 0, parent.log)
        self.assertFalse(data.get("resumed"), data)
        completed = self._completed_ids(parent.result["tasks"])
        for task_id in ["library_health", "missing_files", "root_folder_repair", "artist_alias", "artist_folder_merge", "release_group_merge", "duplicates", "final_verification"]:
            self.assertIn(task_id, completed)
        self.assertEqual(self.call_order[:4], ["artist_folder_merge", "release_group_merge", "duplicates", "folder_scan"])

    def test_artist_folder_merge_failure_preserves_checkpoint_and_resume_retries_only_that_phase(self):
        _checkpoint_with_first_four_complete(self.checkpoint)
        self._patch(mock.patch.object(app_module, "_library_health_payload", side_effect=AssertionError("library health reran")))
        self._patch(mock.patch.object(app_module, "_maintenance_remove_missing_file_rows", side_effect=AssertionError("missing files reran")))
        self._patch(mock.patch.object(app_module, "_maintenance_root_folder_repair", side_effect=AssertionError("root repair reran")))
        self._patch(mock.patch.object(app_module, "_artist_id_alias_groups", side_effect=AssertionError("artist alias reran")))
        artist_scan_attempts = []

        def fail_once_then_succeed(*_args, **_kwargs):
            self.call_order.append("artist_folder_merge")
            artist_scan_attempts.append(1)
            if len(artist_scan_attempts) == 1:
                raise RuntimeError("artist folder merge boom")
            return {"candidates": [], "skipped": []}

        scan = self._patch(mock.patch.object(
            app_module,
            "_stamp_artist_folder_scan",
            side_effect=fail_once_then_succeed,
        ))

        failed_parent, _data = self._post_clean_all()
        failed_last_run = _last_run(self.checkpoint)

        self.assertEqual(failed_parent.returncode, 0, failed_parent.log)
        self.assertTrue(failed_parent.result.get("partial"), failed_parent.result)
        self.assertEqual(failed_last_run["status"], "partial")
        self.assertEqual(failed_last_run["next_task"], "artist_folder_merge")
        failed_tasks = {task["id"]: task for task in failed_last_run["tasks"]}
        self.assertEqual(failed_tasks["artist_folder_merge"]["status"], "failed")
        self.assertNotIn("artist_folder_merge", failed_last_run.get("result_task_ids", []))
        for task_id in _task_results():
            self.assertEqual(failed_tasks[task_id]["status"], "complete")

        self.call_order.clear()
        recovered_parent, recovered_data = self._post_clean_all()
        recovered_last_run = _last_run(self.checkpoint)

        self.assertEqual(scan.call_count, 2)
        self.assertTrue(recovered_data.get("resumed"), recovered_data)
        self.assertEqual(recovered_parent.returncode, 0, recovered_parent.log)
        self.assertEqual(recovered_last_run["status"], "complete")
        self.assertIn("artist_folder_merge", recovered_last_run.get("result_task_ids", []))
        self.assertEqual(self.call_order[:4], ["artist_folder_merge", "release_group_merge", "duplicates", "folder_scan"])

    def _artist_scan_success(self, *args, **kwargs):
        self.call_order.append("artist_folder_merge")
        return {"candidates": [], "skipped": []}

    def _completed_ids(self, tasks):
        return {task["id"] for task in tasks if task.get("status") == "complete"}

    def test_artist_folder_merge_survives_interrupted_apply_and_resumes_via_operation_id(self):
        """hotfix v0.1.17 follow-up (requirement #3, exact 13-step spec):

        1. Clean All starts.
        2. Artist-folder Plan produces op-123.
        3. Apply starts.
        4. Web Manager loses the Apply HTTP response.
        5. Engine transaction remains Running.
        6. First job exits/is interrupted (Web Manager's own bounded poll
           gives up locally while the engine is still Running).
        7. Resume Clean All from persisted checkpoint.
        8. Resume finds op-123.
        9. Resume polls op-123.
        10. It does NOT create another Apply.
        11. op-123 becomes Completed.
        12. Clean All marks the task complete and proceeds.
        13. Apply call count == 1 across BOTH executions.

        This is distinct from test_resume_from_artist_folder_merge_checkpoint_does_not_reference_undefined_root_str
        above (PR #118's `root_str` regression, a different bug: an
        undefined-variable crash on resume) -- that test proves the
        interrupted run's SECOND invocation of the step doesn't crash. This
        test proves it doesn't create a REDUNDANT engine operation/Apply
        call for work a prior, interrupted run already started.
        """
        _checkpoint_with_first_four_complete(self.checkpoint)
        self._patch(mock.patch.object(app_module, "_library_health_payload", side_effect=AssertionError("library health reran")))
        self._patch(mock.patch.object(app_module, "_maintenance_remove_missing_file_rows", side_effect=AssertionError("missing files reran")))
        self._patch(mock.patch.object(app_module, "_maintenance_root_folder_repair", side_effect=AssertionError("root repair reran")))
        self._patch(mock.patch.object(app_module, "_artist_id_alias_groups", side_effect=AssertionError("artist alias reran")))
        self._patch(mock.patch.object(app_module, "BEETS_LONG_OPERATION_POLL_SECONDS", 0.01))
        self._patch(mock.patch.object(app_module, "BEETS_LONG_OPERATION_MAX_SECONDS", 0.05))
        self._patch(mock.patch.object(
            app_module, "_stamp_artist_folder_scan",
            return_value={"candidates": [{"source_path": "/x", "target_path": "/y"}], "skipped": []},
        ))
        plan_mock = self._patch(mock.patch.object(
            app_module.beets_client, "plan_artist_folder_reconcile",
            return_value={"ok": True, "operation_id": "op-123"},
        ))
        apply_mock = self._patch(mock.patch.object(
            app_module.beets_client, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsUnavailableError("Timed out communicating with Beets Control Agent"),
        ))
        tx_status = {"value": "Running"}
        get_tx_mock = self._patch(mock.patch.object(
            app_module.beets_client, "get_transaction",
            side_effect=lambda op_id: {"ok": True, "transaction": {"status": tx_status["value"], "operation_id": op_id}},
        ))

        # Steps 1-6: Plan produces op-123, Apply's response is lost, the
        # engine transaction stays Running, and the bounded poll deadline
        # is reached while it is still Running -- this run gives up locally
        # (does not fabricate success or failure) and ends "partial".
        first_parent, _first_data = self._post_clean_all()
        first_last_run = _last_run(self.checkpoint)

        self.assertEqual(first_parent.returncode, 0, first_parent.log)
        self.assertTrue(first_parent.result.get("partial"), first_parent.result)
        self.assertEqual(first_last_run["status"], "partial")
        first_tasks = {task["id"]: task for task in first_last_run["tasks"]}
        self.assertEqual(first_tasks["artist_folder_merge"]["status"], "running")
        self.assertEqual(first_tasks["artist_folder_merge"].get("operation_id"), "op-123")
        plan_mock.assert_called_once()
        apply_mock.assert_called_once()

        # Steps 7-11: resume finds op-123 (from the persisted checkpoint)
        # and polls its authoritative status instead of creating a new Plan
        # or calling Apply again. This time the engine reports Completed.
        tx_status["value"] = "Completed"
        self.call_order.clear()
        second_parent, second_data = self._post_clean_all()
        second_last_run = _last_run(self.checkpoint)

        # Steps 12-13: the task is marked complete and Clean All proceeds;
        # Apply (and Plan) were called exactly once across BOTH executions.
        self.assertEqual(second_parent.returncode, 0, second_parent.log)
        self.assertTrue(second_data.get("resumed"), second_data)
        self.assertEqual(second_last_run["status"], "complete")
        second_tasks = {task["id"]: task for task in second_last_run["tasks"]}
        self.assertEqual(second_tasks["artist_folder_merge"]["status"], "complete")
        self.assertNotIn("operation_id", second_tasks["artist_folder_merge"])
        self.assertEqual(plan_mock.call_count, 1, "Plan must not be re-created for an already-planned operation")
        self.assertEqual(apply_mock.call_count, 1, "Apply must never be called a second time for op-123")
        self.assertGreaterEqual(get_tx_mock.call_count, 2, "resume must have polled op-123's authoritative status")
        self.assertEqual(self.call_order[:3], ["release_group_merge", "duplicates", "folder_scan"])

    def test_saved_operation_survives_a_transaction_lookup_failure_on_resume(self):
        """Independent review follow-up: a saved Clean All operation must
        never be discarded just because a resume's own status lookup
        failed.

        1. First execution persists op-123 (Plan succeeds, Apply's response
           is lost, engine transaction is Running).
        2. Process is interrupted (first run ends "partial", running).
        3. Second execution resumes with op-123 -- but
           beets_client.get_transaction(op-123) itself raises
           BeetsUnavailableError (a transport failure, not a definitive
           answer).
        4. Assert: Plan call count remains 1, Apply call count remains 1 --
           the lookup failure must NOT be treated as "the operation is gone,
           start fresh".
        5. Assert: the checkpoint still contains op-123, and the task
           remains running/unresolved (not complete, not failed, not
           silently dropped).
        6. Third execution resumes again; this time get_transaction(op-123)
           succeeds and reports Completed.
        7. Assert: the task completes, still without a second Plan or a
           second Apply call across all three executions.
        """
        _checkpoint_with_first_four_complete(self.checkpoint)
        self._patch(mock.patch.object(app_module, "_library_health_payload", side_effect=AssertionError("library health reran")))
        self._patch(mock.patch.object(app_module, "_maintenance_remove_missing_file_rows", side_effect=AssertionError("missing files reran")))
        self._patch(mock.patch.object(app_module, "_maintenance_root_folder_repair", side_effect=AssertionError("root repair reran")))
        self._patch(mock.patch.object(app_module, "_artist_id_alias_groups", side_effect=AssertionError("artist alias reran")))
        self._patch(mock.patch.object(app_module, "BEETS_LONG_OPERATION_POLL_SECONDS", 0.01))
        self._patch(mock.patch.object(app_module, "BEETS_LONG_OPERATION_MAX_SECONDS", 0.05))
        self._patch(mock.patch.object(
            app_module, "_stamp_artist_folder_scan",
            return_value={"ok": True, "candidates": [{"source_path": "/x", "target_path": "/y"}], "skipped": []},
        ))
        plan_mock = self._patch(mock.patch.object(
            app_module.beets_client, "plan_artist_folder_reconcile",
            return_value={"ok": True, "operation_id": "op-123"},
        ))
        apply_mock = self._patch(mock.patch.object(
            app_module.beets_client, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsUnavailableError("Timed out communicating with Beets Control Agent"),
        ))

        # Execution 1: Plan -> op-123, Apply's response lost, engine stays
        # Running until the bounded poll deadline -- ends "partial".
        get_tx_patcher = mock.patch.object(
            app_module.beets_client, "get_transaction",
            return_value={"ok": True, "transaction": {"status": "Running", "operation_id": "op-123"}},
        )
        get_tx_patcher.start()
        first_parent, _first_data = self._post_clean_all()
        first_last_run = _last_run(self.checkpoint)
        self.assertEqual(first_parent.returncode, 0, first_parent.log)
        self.assertTrue(first_parent.result.get("partial"), first_parent.result)
        first_tasks = {task["id"]: task for task in first_last_run["tasks"]}
        self.assertEqual(first_tasks["artist_folder_merge"]["status"], "running")
        self.assertEqual(first_tasks["artist_folder_merge"].get("operation_id"), "op-123")
        plan_mock.assert_called_once()
        apply_mock.assert_called_once()
        get_tx_patcher.stop()

        # Execution 2: resume finds op-123, but the status lookup itself
        # fails (transport uncertainty, not a definitive answer). Must NOT
        # create a new Plan or call Apply again; must NOT drop op-123.
        get_tx_patcher = mock.patch.object(
            app_module.beets_client, "get_transaction",
            side_effect=app_module.BeetsUnavailableError("Timed out communicating with Beets Control Agent"),
        )
        get_tx_patcher.start()
        second_parent, second_data = self._post_clean_all()
        second_last_run = _last_run(self.checkpoint)

        self.assertEqual(second_parent.returncode, 0, second_parent.log)
        self.assertTrue(second_data.get("resumed"), second_data)
        self.assertEqual(second_last_run["status"], "partial")
        second_tasks = {task["id"]: task for task in second_last_run["tasks"]}
        self.assertEqual(
            second_tasks["artist_folder_merge"]["status"], "running",
            "a failed status lookup must leave the task running/unresolved, not failed or complete",
        )
        self.assertEqual(
            second_tasks["artist_folder_merge"].get("operation_id"), "op-123",
            "a failed status lookup must never discard the saved operation_id",
        )
        self.assertEqual(plan_mock.call_count, 1, "a lookup failure must never trigger a second Plan")
        self.assertEqual(apply_mock.call_count, 1, "a lookup failure must never trigger a second Apply")
        get_tx_patcher.stop()

        # Execution 3: resume finds op-123 again, and this time the status
        # lookup succeeds and reports Completed.
        get_tx_completed = self._patch(mock.patch.object(
            app_module.beets_client, "get_transaction",
            return_value={"ok": True, "transaction": {"status": "Completed", "operation_id": "op-123"}},
        ))
        self.call_order.clear()
        third_parent, third_data = self._post_clean_all()
        third_last_run = _last_run(self.checkpoint)

        self.assertEqual(third_parent.returncode, 0, third_parent.log)
        self.assertTrue(third_data.get("resumed"), third_data)
        self.assertEqual(third_last_run["status"], "complete")
        third_tasks = {task["id"]: task for task in third_last_run["tasks"]}
        self.assertEqual(third_tasks["artist_folder_merge"]["status"], "complete")
        self.assertNotIn("operation_id", third_tasks["artist_folder_merge"])
        self.assertEqual(plan_mock.call_count, 1, "Plan must still have been called exactly once across all three executions")
        self.assertEqual(apply_mock.call_count, 1, "Apply must still have been called exactly once across all three executions")
        get_tx_completed.assert_called()


if __name__ == "__main__":
    unittest.main()
