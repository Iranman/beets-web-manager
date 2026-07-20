"""Tests for the Root Folder Repair maintenance step (misplaced album/singleton
folders sitting directly under MUSIC_ROOT instead of inside an artist folder)."""
import unittest
from pathlib import Path


def _app_source() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "app.py").read_text(encoding="utf-8")


def _function_source(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


class RootFolderRepairScanTests(unittest.TestCase):
    def setUp(self):
        self._src = _app_source()
        self._scan = _function_source(
            self._src,
            "def _root_folder_repair_scan(",
            "def _root_folder_repair_apply_safe(",
        )

    def test_scan_is_defined(self):
        self.assertIn(
            "def _root_folder_repair_scan(root: Optional[Path] = None) -> Dict[str, Any]:",
            self._src,
        )

    def test_classifies_by_zero_subdirectories(self):
        # A real artist folder always has >=1 album subfolder; anything with
        # zero subdirs directly under root is either junk or misplaced.
        self.assertIn("if subdirs:", self._scan)
        self.assertIn("continue  # real artist folder", self._scan)

    def test_shallow_items_detected_by_single_path_separator(self):
        # Normal items are Artist/Album/File (2 separators); a shallow item
        # missing its artist-folder wrapper is Album/File (1 separator).
        self.assertIn('parts = path.split("/")', self._scan)
        self.assertIn("if len(parts) == 2:", self._scan)

    def test_uses_beets_library_api_not_raw_sql(self):
        self.assertIn("lib.items([])", self._scan)

    def test_three_way_classification(self):
        self.assertIn("empty_folders", self._scan)
        self.assertIn("shallow_folders", self._scan)
        self.assertIn("orphaned_folders", self._scan)


class RootFolderRepairApplySafeTests(unittest.TestCase):
    def setUp(self):
        self._src = _app_source()
        self._apply = _function_source(
            self._src,
            "def _root_folder_repair_apply_safe(",
            "def _album_folder_cleanup_plan(",
        )

    def test_apply_safe_is_defined(self):
        self.assertIn(
            "def _root_folder_repair_apply_safe(log: List[str], cancel_event: Optional[Any] = None,",
            self._src,
        )

    def test_empty_folders_removed_via_shared_helper(self):
        # Reuse the same empty-tree remover the album-folder cleanup uses,
        # rather than a second bespoke rmdir implementation.
        self.assertIn("_album_cleanup_remove_empty_tree(folder, log)", self._apply)

    def test_shallow_items_use_beet_write_then_move_per_item(self):
        self.assertIn('base + ["write", f"id:{iid}"]', self._apply)
        self.assertIn('base + ["move", f"id:{iid}"]', self._apply)

    def test_mbsync_only_when_item_has_mb_ids(self):
        self.assertIn("has_mb", self._apply)
        self.assertIn('base + ["mbsync", f"id:{iid}"]', self._apply)

    def test_orphaned_folders_are_queued_not_deleted(self):
        # Untracked audio must never be silently removed; queue for review.
        self.assertIn("_queue_folder_for_manual_review(", self._apply)
        self.assertNotIn("shutil.rmtree", self._apply)

    def test_uses_dedicated_lock(self):
        self.assertIn("_ROOT_FOLDER_REPAIR_LOCK", self._src)


class RootFolderRepairWiringTests(unittest.TestCase):
    def setUp(self):
        self._src = _app_source()

    def test_registered_in_maintenance_runner_tasks(self):
        self.assertIn(
            '{"id": "root_folder_repair", "label": "Root Folder Repair"}',
            self._src,
        )

    def test_registered_in_task_phases(self):
        self.assertIn('"root_folder_repair": "Organizing"', self._src)

    def test_runs_between_missing_files_and_artist_alias(self):
        pipeline = _function_source(
            self._src,
            'set_task("missing_files", "complete"',
            'set_task("artist_alias", "running"',
        )
        self.assertIn("_maintenance_root_folder_repair(", pipeline)

    def test_manual_routes_registered(self):
        self.assertIn('@app.post("/api/clean/root-folders/scan")', self._src)
        self.assertIn('@app.post("/api/clean/root-folders/apply-safe")', self._src)
        self.assertIn('@app.get("/api/clean/root-folders/report")', self._src)

    def test_report_persisted_to_dedicated_cache_file(self):
        self.assertIn(
            'ROOT_FOLDER_REPAIR_LAST_FILE = METADATA_CACHE_ROOT / "root-folder-repair-last.json"',
            self._src,
        )


if __name__ == "__main__":
    unittest.main()
