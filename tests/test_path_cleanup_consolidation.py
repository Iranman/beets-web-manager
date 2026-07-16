import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PathCleanupConsolidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jobs_source = (ROOT / "frontend" / "src" / "views" / "Jobs.tsx").read_text(encoding="utf-8")
        cls.client_source = (ROOT / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
        cls.folder_panel_source = (
            ROOT / "frontend" / "src" / "features" / "leakedPaths" / "FolderPlaceholdersPanel.tsx"
        ).read_text(encoding="utf-8")
        cls.leaked_panel_source = (
            ROOT / "frontend" / "src" / "features" / "leakedPaths" / "LeakedPathsPanel.tsx"
        ).read_text(encoding="utf-8")
        cls.app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        cls.types_source = (ROOT / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")

    def test_files_tab_no_longer_duplicates_path_cleanup_actions(self):
        self.assertNotIn("function FilesPanel", self.jobs_source)
        self.assertNotIn("{ id: 'files', label: 'Files' }", self.jobs_source)
        self.assertNotIn("return 'files'", self.jobs_source)
        for old_label in [
            "File Jobs",
            "Open Path & Folder Cleanup",
            "File and Path Maintenance",
            "Scan paths",
            "Dry-run path cleanup",
            "Clean paths",
            "Move all files",
            "Sync deleted files",
        ]:
            self.assertNotIn(old_label, self.jobs_source)

    def test_jobs_uses_single_advanced_maintenance_section(self):
        self.assertIn("Manual Actions", self.jobs_source)
        self.assertIn("Job History", self.jobs_source)
        self.assertIn("Advanced Maintenance", self.jobs_source)
        self.assertIn(
            "Advanced repair tools for rare library/database issues. Most users should not need these.",
            self.jobs_source,
        )
        self.assertIn("DB Health Check", self.jobs_source)
        self.assertIn('title="Folder Name Issues"', self.jobs_source)
        self.assertIn("Missing Files Scan", self.jobs_source)
        self.assertIn('title="Leaked DB Paths"', self.jobs_source)
        self.assertIn('title="Artist Alias Repair"', self.jobs_source)
        self.assertIn('title="Album Track Repair"', self.jobs_source)
        self.assertIn('title="Advanced Library Move"', self.jobs_source)
        self.assertNotIn("function CleanupPanel", self.jobs_source)
        self.assertNotIn("function PathCleanupPanel", self.jobs_source)
        self.assertNotIn("function PathCleanupOverview", self.jobs_source)
        self.assertNotIn("Cleanup workflow", self.jobs_source)
        self.assertNotIn("Path cleanup view", self.jobs_source)
        self.assertNotIn("'Overview', 'Folder Names', 'Leaked DB Paths'", self.jobs_source)
        self.assertIn("Preview sync", self.jobs_source)
        self.assertIn("Apply confirmed sync", self.jobs_source)
        self.assertIn("not exposed as a one-click cleanup action", self.jobs_source)

    def test_folder_placeholder_apply_requires_confirmation(self):
        self.assertIn('@app.get("/api/clean/folder-placeholder/review")', self.app_source)
        self.assertIn('@app.post("/api/clean/folder-placeholder/preview-merge")', self.app_source)
        self.assertIn('@app.post("/api/clean/folder-placeholder/apply")', self.app_source)
        self.assertIn("Confirmation is required before applying cleanup", self.app_source)
        # preview_token still required for merge actions
        self.assertIn("Preview token is required; rerun preview before applying", self.app_source)
        self.assertIn("Stale preview; rerun preview before applying", self.app_source)
        self.assertIn("Target subfolder does not exist; cleanup apply will not create folders", self.app_source)

    def test_safe_rename_action_exists_in_backend(self):
        self.assertIn('@app.post("/api/clean/folder-placeholder/apply-safe-renames")', self.app_source)
        self.assertIn('"safe_rename"', self.app_source)
        self.assertIn('"rename_folder"', self.app_source)
        self.assertIn("target_path is required for safe_rename", self.app_source)
        self.assertIn("Target folder already exists:", self.app_source)
        # safe_rename validates at apply time — no DB items, target must not exist
        self.assertIn("use beet move instead of plain rename", self.app_source)
        self.assertIn("src.rename(dst)", self.app_source)

    def test_bulk_safe_rename_job_endpoint_exists(self):
        self.assertIn("def apply_safe_folder_placeholder_renames_job", self.app_source)
        self.assertIn("folder-placeholder-apply-safe", self.app_source)
        self.assertIn("Apply safe folder placeholder renames", self.app_source)
        # Bulk apply must re-scan or use provided source_paths
        self.assertIn("source_paths", self.app_source)
        # Bulk apply logs each rename and skip
        self.assertIn("RENAMED:", self.app_source)
        self.assertIn("SKIP (target exists):", self.app_source)
        self.assertIn("SKIP (DB tracked,", self.app_source)
        # Only safe renames are applied
        self.assertIn('r.get("safe") and not r.get("target_exists")', self.app_source)

    def test_target_exists_rows_excluded_from_bulk_safe_rename(self):
        # Bulk apply explicitly filters out target_exists rows
        self.assertIn('not r.get("target_exists")', self.app_source)
        # And DB-tracked rows
        self.assertIn('int(r.get("db_item_count") or 0) == 0', self.app_source)

    def test_safe_rename_button_visible_in_frontend(self):
        self.assertIn("Apply Safe Renames", self.folder_panel_source)
        self.assertIn("applySafeFolderPlaceholderRenames", self.folder_panel_source)
        self.assertIn("bucketCounts.safe_rename > 0", self.folder_panel_source)
        # Button shows count
        self.assertIn("Apply Safe Renames ({bucketCounts.safe_rename})", self.folder_panel_source)

    def test_per_row_rename_button_for_safe_rename_rows(self):
        # Each safe_rename row exposes a Rename button
        self.assertIn("bucket === 'safe_rename'", self.folder_panel_source)
        self.assertIn("doRename", self.folder_panel_source)
        self.assertIn("applyFolderPlaceholderAction('safe_rename'", self.folder_panel_source)
        # Confirm step before applying
        self.assertIn("confirmRename", self.folder_panel_source)
        self.assertIn("setConfirmRename(true)", self.folder_panel_source)

    def test_bulk_select_and_apply_selected(self):
        self.assertIn("Apply Selected", self.folder_panel_source)
        self.assertIn("selectedSafeRows", self.folder_panel_source)
        self.assertIn("Select all safe renames", self.folder_panel_source)
        self.assertIn("selectAllSafe", self.folder_panel_source)
        self.assertIn("clearSelection", self.folder_panel_source)
        # Checkboxes only on safe_rename rows
        self.assertIn("onToggle && bucket === 'safe_rename'", self.folder_panel_source)

    def test_auto_apply_safe_setting_exists(self):
        self.assertIn("autoApplySafe", self.folder_panel_source)
        self.assertIn("Auto-apply safe renames after scan", self.folder_panel_source)
        self.assertIn("AUTO_APPLY_KEY", self.folder_panel_source)
        # Auto-apply only applies safe_rename bucket — never target_exists or others
        self.assertIn("classifyRow(r) === 'safe_rename'", self.folder_panel_source)

    def test_results_refresh_after_bulk_apply(self):
        # After apply job completes, a rescan is triggered
        self.assertIn("doScan", self.folder_panel_source)
        self.assertIn("applyJob.status === 'success'", self.folder_panel_source)
        self.assertIn("applyJobId", self.folder_panel_source)

    def test_api_client_has_bulk_rename_function(self):
        self.assertIn("applySafeFolderPlaceholderRenames", self.client_source)
        self.assertIn("/api/clean/folder-placeholder/apply-safe-renames", self.client_source)
        self.assertIn("source_paths", self.client_source)

    def test_types_include_renamed_fields(self):
        self.assertIn("renamed_from", self.types_source)
        self.assertIn("renamed_to", self.types_source)
        self.assertIn("FolderPlaceholderApplySafeRenamesResult", self.types_source)

    def test_frontend_sends_preview_tokens_for_folder_apply(self):
        self.assertIn("previewToken: review?.preview_token", self.folder_panel_source)
        self.assertIn("previewToken: preview.preview_token", self.folder_panel_source)
        self.assertIn("Apply previewed merge", self.folder_panel_source)
        self.assertIn("Apply merge", self.folder_panel_source)
        self.assertIn("target_path: opts.targetPath", self.client_source)
        self.assertIn("preview_token: opts.previewToken", self.client_source)

    def test_db_cleanup_apply_paths_are_explicitly_confirmed(self):
        self.assertIn("if not dry_run and payload.get(\"confirmed\") is not True", self.app_source)
        self.assertIn("Confirmation is required before syncing deleted files", self.app_source)
        self.assertIn("Confirmation is required before repairing DB paths", self.app_source)
        self.assertIn("syncDeleted({ dryRun: false, confirmed: true })", self.jobs_source)
        self.assertIn("confirmed: !dry_run", self.leaked_panel_source)

    def test_mark_reviewed_is_not_primary_action(self):
        # Mark reviewed must exist but not be the main CTA
        self.assertIn("Mark reviewed", self.folder_panel_source)
        # Primary action for safe_rename is Rename, not Mark reviewed
        self.assertIn("Rename", self.folder_panel_source)
        # Apply Safe Renames bulk button must exist in the main panel action bar
        self.assertIn("Apply Safe Renames ({bucketCounts.safe_rename})", self.folder_panel_source)
        # The main panel (FolderPlaceholdersPanel) defines Apply Safe Renames in its return JSX
        panel_fn_pos = self.folder_panel_source.find("export function FolderPlaceholdersPanel")
        apply_btn_pos = self.folder_panel_source.rfind("Apply Safe Renames")
        self.assertGreater(apply_btn_pos, panel_fn_pos,
                           "Apply Safe Renames button must be inside FolderPlaceholdersPanel")


if __name__ == "__main__":
    unittest.main()
