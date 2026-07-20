import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MaintenanceRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        cls.jobs_source = (ROOT / "frontend" / "src" / "views" / "Jobs.tsx").read_text(encoding="utf-8")
        cls.client_source = (ROOT / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
        cls.shell_source = (
            ROOT / "frontend" / "src" / "components" / "layout" / "Shell.tsx"
        ).read_text(encoding="utf-8")
        cls.router_source = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        start = cls.app_source.index('@app.post("/api/jobs/maintenance-runner")')
        end = cls.app_source.index("def _folder_cleanup_path", start)
        cls.runner_source = cls.app_source[start:end]

    def test_jobs_renders_compact_maintenance_runner(self):
        self.assertIn("function MaintenanceRunnerBar", self.jobs_source)
        self.assertIn("Clean All", self.jobs_source)
        self.assertIn("<MaintenanceRunnerBar", self.jobs_source)
        self.assertIn("Clean All", self.jobs_source)
        self.assertIn("Stop", self.jobs_source)
        self.assertIn("View Progress", self.jobs_source)
        self.assertIn("Cleanup Report", self.jobs_source)
        self.assertIn("MaintenanceRunnerReportDialog", self.jobs_source)
        self.assertIn("Next scheduled run: Manual only", self.jobs_source)
        self.assertIn("Manual Actions", self.jobs_source)
        self.assertIn("Advanced Maintenance", self.jobs_source)
        self.assertIn("Job History", self.jobs_source)

    def test_run_now_creates_parent_maintenance_job(self):
        self.assertIn("export function startMaintenanceRunner", self.client_source)
        self.assertIn("export function startCleanAll", self.client_source)
        self.assertIn("'/api/jobs/maintenance-runner'", self.client_source)
        self.assertIn("await startCleanAll(options)", self.jobs_source)
        self.assertIn('@app.post("/api/jobs/maintenance-runner")', self.app_source)
        self.assertIn('@app.get("/api/jobs/maintenance-runner/report")', self.app_source)
        self.assertIn('label="Clean All"', self.runner_source)
        self.assertIn('metadata={"type": "maintenance-runner", "workflow": "clean-all"', self.runner_source)
        self.assertIn("def _maintenance_running_job", self.app_source)
        self.assertIn('"already_running": True', self.runner_source)

    def test_progress_and_task_statuses_come_from_job_state(self):
        self.assertIn("maintenanceTasksFromState", self.jobs_source)
        self.assertIn("state?.maintenance_tasks", self.jobs_source)
        self.assertIn("state?.progress_percent", self.jobs_source)
        self.assertIn("clean_all_pipeline", self.runner_source)
        self.assertIn("clean_all_counts", self.runner_source)
        self.assertIn("last_heartbeat_at", self.runner_source)
        self.assertIn('label={`${task.label}: ${task.status}`}', self.jobs_source)
        self.assertIn('"maintenance_tasks": tasks', self.runner_source)
        self.assertIn('"progress_percent"', self.runner_source)
        for task_id in [
            "library_health",
            "missing_files",
            "artist_alias",
            "artist_folder_merge",
            "release_group_merge",
            "duplicates",
            "folder_scan",
            "folder_safe_renames",
            "artwork",
            "genres",
            "final_verification",
        ]:
            self.assertIn(task_id, self.runner_source)

    def test_runner_uses_existing_cleanup_systems_for_real_clean_all_work(self):
        self.assertIn("def _run_with_app_context", self.app_source)
        self.assertIn("_run_with_app_context(_run)", self.runner_source)
        self.assertIn("_maintenance_full_duplicate_scan", self.app_source)
        self.assertIn("_maintenance_full_duplicate_scan(", self.runner_source)
        self.assertIn("_maintenance_same_file_hash", self.app_source)
        self.assertIn('match_type == "identical file size" and _maintenance_same_file_hash', self.app_source)
        self.assertIn("dedup_scan()", self.app_source)
        self.assertIn("dedup_cleanup()", self.app_source)
        self.assertIn('json={"path": str(MUSIC_ROOT)}', self.app_source)
        self.assertIn('"file_duplicate_scan_started": True', self.app_source)

        self.assertIn("_maintenance_release_group_merge", self.app_source)
        self.assertIn("_maintenance_release_group_merge(", self.runner_source)
        self.assertIn("_album_folder_cleanup_apply_safe(", self.app_source)
        self.assertIn("verbose_files=False", self.app_source)
        self.assertIn('"release_group_merge"', self.app_source)

        self.assertIn('json={"force": False, "use_ai": False}', self.runner_source)
        self.assertIn("fetch_missing_art()", self.runner_source)

        self.assertIn("_scan_folder_name_placeholders", self.runner_source)
        self.assertIn("Placeholder-only renames disabled", self.runner_source)
        self.assertNotIn("_maintenance_safe_folder_renames(safe_rename_rows", self.runner_source)

        self.assertIn("_library_health_payload", self.runner_source)
        self.assertIn("_artist_id_alias_groups()", self.runner_source)
        self.assertNotIn("clean_artist_folders_scan()", self.runner_source)
        self.assertIn("clean_artist_folders_stamp_mbid()", self.runner_source)
        self.assertIn('json={"root": str(MUSIC_ROOT), "dry_run": False, "compact_log": True}', self.runner_source)
        self.assertIn('"stamp-mbid-folders"', self.runner_source)
        self.assertIn("_maintenance_final_verification", self.app_source)
        self.assertIn("_maintenance_final_verification(", self.runner_source)
        self.assertIn('"duplicate_release_group_id_groups_remaining"', self.app_source)
        self.assertNotIn("library_sync_deleted()", self.runner_source)
        self.assertNotIn("delete_preview", self.runner_source)
        self.assertNotIn("merge_source_files", self.runner_source)

    def test_clean_all_checkpoints_are_resumable(self):
        self.assertIn("_maintenance_load_last_report", self.app_source)
        self.assertIn("_maintenance_resume_from_report", self.app_source)
        self.assertIn("_maintenance_resume_summary(report)", self.app_source)
        self.assertIn("persist_checkpoint('running')", self.runner_source)
        self.assertIn("persist_checkpoint(\"complete\")", self.runner_source)
        self.assertIn("persist_checkpoint(\"failed\", failed_message)", self.runner_source)
        self.assertIn('"result_task_ids": sorted(str(key) for key in results.keys())', self.runner_source)
        self.assertIn('"results": results', self.app_source)
        self.assertIn('_maintenance_save_last_report({task_id: result}, log)', self.runner_source)
        self.assertNotIn('"tasks": tasks,\n                "results": results', self.runner_source)
        self.assertIn("skip_completed_task(\"artist_folder_merge\")", self.runner_source)
        self.assertIn("skip_completed_task(\"release_group_merge\")", self.runner_source)
        self.assertIn("skip_completed_task(\"duplicates\")", self.runner_source)
        self.assertIn('"resumed": resume_requested', self.runner_source)
        self.assertIn("resume_snapshot=resume_snapshot", self.runner_source)
        self.assertIn("resume_requested=resume_requested", self.runner_source)
        self.assertIn("resume_payload = _maintenance_resume_summary", self.runner_source)
        self.assertIn('if task_id in {"library_health", "missing_files", "artist_alias"} and task_id in results:', self.app_source)
        self.assertIn('explicit_next_task = _s(last_run.get("next_task")).strip()', self.app_source)
        self.assertIn('persist_checkpoint("partial", failed_message)', self.runner_source)
        self.assertIn('return finish_partial(failed_message)', self.runner_source)
        self.assertIn('"maintenance_status": "partial"', self.runner_source)
        for task_id in ["library_health", "missing_files", "artist_alias"]:
            marker = f'if skip_completed_task("{task_id}"):'
            self.assertIn(marker, self.runner_source)
            self.assertLess(
                self.runner_source.index(marker),
                self.runner_source.index(f'set_task("{task_id}", "running"'),
            )
        self.assertEqual(
            self.app_source.count('"resumed": resume_requested'),
            self.runner_source.count('"resumed": resume_requested'),
        )
        self.assertNotIn('"resume": _maintenance_resume_summary({"last_run": {"status": "failed"', self.app_source)

        self.assertIn("Resume Clean All", self.jobs_source)
        self.assertIn("Resume checkpoint ready", self.jobs_source)
        self.assertIn("raw === 'partial'", self.jobs_source)
        self.assertIn("'Partial'", self.jobs_source)

    def test_clean_all_child_wait_preserves_progress_and_uses_idle_duplicate_timeout(self):
        self.assertIn("def _wrapped(log, cancel_event=None, update_state=None):", self.app_source)
        self.assertIn("return fn(log, cancel_event, update_state)", self.app_source)
        self.assertIn("idle_timeout: int = 0, progress: Optional[Any] = None", self.app_source)
        self.assertIn("if progress and child_state:", self.app_source)
        self.assertIn("progress(child_state)", self.app_source)
        self.assertIn('prefix="duplicates"', self.app_source)
        self.assertIn("timeout=0", self.app_source)
        self.assertIn("idle_timeout=1800", self.app_source)
        self.assertIn("progress=progress", self.app_source)
        self.assertNotIn("timeout=7200", self.app_source)
        self.assertIn("def _should_mirror_child_line(line: Any) -> bool:", self.app_source)
        self.assertIn('if prefix != "duplicates":', self.app_source)
        self.assertIn('re.match(r"^\\[\\d+/\\d+\\]", text)', self.app_source)
        self.assertIn('"DUPLICATE [" in text or "REJECTED [" in text', self.app_source)
        self.assertIn("if _should_mirror_child_line(line):", self.app_source)

    def test_clean_all_reconciles_stale_db_paths_before_duplicate_scan(self):
        self.assertIn("def _maintenance_remove_missing_file_rows", self.app_source)
        self.assertIn("_maintenance_remove_missing_file_rows(health, log)", self.runner_source)
        self.assertIn("trigger_plex=False", self.app_source)
        self.assertIn("too many DB rows appear missing; possible mount issue", self.app_source)
        self.assertIn('set_task("missing_files", "complete", "Missing files reconciled", missing_summary)', self.runner_source)
        self.assertLess(
            self.runner_source.index("_maintenance_remove_missing_file_rows(health, log)"),
            self.runner_source.index("_maintenance_full_duplicate_scan("),
        )
    def test_no_automation_or_import_surface_was_added_to_jobs(self):
        combined_frontend = "\n".join([self.jobs_source, self.shell_source, self.router_source])
        self.assertNotIn("/automation", combined_frontend.lower())
        self.assertNotIn("Automation", combined_frontend)
        self.assertNotIn("{ id: 'import', label: 'Import' }", self.jobs_source)
        self.assertNotIn("ImportPanel", self.jobs_source)
        self.assertNotIn("Retry failed imports", self.jobs_source)
        self.assertNotIn("Place playlist imports", self.jobs_source)
        self.assertNotIn("ImportReviewPage", self.jobs_source)
        self.assertNotIn("Cleanup workflow", self.jobs_source)
        self.assertNotIn("Path cleanup view", self.jobs_source)


if __name__ == "__main__":
    unittest.main()

