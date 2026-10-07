import ast
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import transaction_engine
from backend.beets_adapter import BeetsUnavailableError
try:
    from _folder_ops_local import patch_local_folder_ops
except ImportError:  # pragma: no cover
    from tests._folder_ops_local import patch_local_folder_ops
try:  # ARCH-001: app.py module family (works under discovery and tests.<module> runs)
    from _app_ast_cache import app_family_source  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_ast_cache import app_family_source  # noqa: E402
try:  # ARCH-001: patch app.py and the modules extracted from it
    from _app_family import patch_app_family  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_family import patch_app_family  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = app_family_source()


class LibraryCleanupTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name).resolve()
        self.music_dir = self.root / "music"
        self.music_dir.mkdir()
        self.staging_dir = self.root / "downloads"
        self.staging_dir.mkdir()
        self.quarantine_dir = self.root / "quarantine"
        self.quarantine_dir.mkdir()
        self.store = transaction_engine.TransactionStore(str(self.root / "transactions"))
        patch_local_folder_ops(self, self.music_dir)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_folder_cleanup_blocks_unexpected_content(self):
        empty_dir = self.music_dir / "Empty"
        empty_dir.mkdir()
        plan = transaction_engine.create_folder_cleanup_plan(
            self.store,
            {"action": "remove_empty", "source": str(empty_dir)},
            music_allowed_roots=[str(self.music_dir)],
        )
        self.assertTrue(plan.get("ok"), msg=plan)
        self.store.transition(plan["operation_id"], "Preview", "Approved")
        (empty_dir / "surprise.txt").write_text("unexpected", encoding="utf-8")
        apply = transaction_engine.execute_folder_cleanup_apply(
            self.store,
            plan["operation_id"],
            music_allowed_roots=[str(self.music_dir)],
        )
        self.assertFalse(apply.get("ok"), msg=apply)
        self.assertEqual(apply.get("code"), "folder_cleanup_not_empty")
        self.assertTrue(empty_dir.exists())


    def test_folder_cleanup_removes_and_rolls_back_empty_directory(self):
        empty_dir = self.music_dir / "RollbackEmpty"
        empty_dir.mkdir()
        plan = transaction_engine.create_folder_cleanup_plan(
            self.store,
            {"action": "remove_empty", "source": str(empty_dir)},
            music_allowed_roots=[str(self.music_dir)],
        )
        self.store.transition(plan["operation_id"], "Preview", "Approved")
        apply = transaction_engine.execute_folder_cleanup_apply(
            self.store,
            plan["operation_id"],
            music_allowed_roots=[str(self.music_dir)],
        )
        self.assertTrue(apply.get("ok"), msg=apply)
        self.assertFalse(empty_dir.exists())
        rollback = transaction_engine.rollback_folder_cleanup(
            self.store,
            plan["operation_id"],
            music_allowed_roots=[str(self.music_dir)],
        )
        self.assertTrue(rollback.get("ok"), msg=rollback)
        self.assertTrue(empty_dir.exists())


class LibraryCleanupWebManagerTests(unittest.TestCase):
    def _function_source(self, name: str) -> str:
        tree = ast.parse(APP_SOURCE)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return ast.get_source_segment(APP_SOURCE, node) or ""
        raise AssertionError(f"function {name} not found")

    def test_web_manager_cleanup_functions_have_no_local_media_mutation(self):
        banned = (
            ".unlink(", ".rmdir(", ".rename(", ".replace(",
            "shutil.rmtree", "shutil.move", "shutil.copy", "os.unlink",
            "os.remove", "os.rename", "os.replace", "DELETE FROM items",
        )
        for func in ("run_dedup_cleanup", "_album_cleanup_remove_empty_tree"):
            src = self._function_source(func)
            for needle in banned:
                self.assertNotIn(needle, src, msg=f"{func} still contains {needle}")
        # Dry run previews; a live run goes only through the reviewed-cleanup
        # authority (re-verified pairs, engine quarantine, verification).
        self.assertIn("composite_workflows.plan_library_cleanup", self._function_source("run_dedup_cleanup"))
        self.assertIn("_duplicate_cleanup.plan_reviewed_cleanup", self._function_source("run_dedup_cleanup"))
        self.assertIn("_duplicate_cleanup.apply_reviewed_cleanup", self._function_source("run_dedup_cleanup"))
        self.assertNotIn("apply_library_cleanup", self._function_source("run_dedup_cleanup"))
        self.assertIn("composite_workflows.plan_folder_cleanup", self._function_source("_album_cleanup_remove_empty_tree"))

    def test_dedup_cleanup_engine_unavailable_fails_closed(self):
        import app as app_module

        with tempfile.TemporaryDirectory() as td:
            candidate = Path(td) / "song.mp3"
            candidate.write_bytes(b"audio")
            import backend.dedup_service as dedup_service
            pair = {"delete_item_id": 2, "keep_item_id": 1}
            with mock.patch.object(dedup_service, "_dedup_pairs_for_paths", return_value=([pair], [])), \
                    mock.patch.object(dedup_service._duplicate_cleanup, "plan_reviewed_cleanup",
                                      side_effect=BeetsUnavailableError("offline")):
                with app_module.app.test_request_context(
                    "/api/dedup/cleanup",
                    method="POST",
                    json={"paths": [str(candidate)], "dry_run": False},
                ):
                    response, status = app_module.dedup_cleanup()
            self.assertEqual(status, 503)
            self.assertFalse(response.get_json()["ok"])
            self.assertTrue(candidate.exists())

    def test_non_media_cleanup_reclassification_is_reviewed(self):
        from scripts import generate_arch003_mutation_inventory as generator

        data = generator.generate(write=False)
        self.assertEqual(data.get("unresolved_domain_counts", {}).get("library_cleanup", 0), 0)
        by_function = {entry["function"]: entry for entry in data["inventory"] if entry.get("rule", "").startswith("reviewed-library-cleanup-closure")}
        self.assertEqual(by_function["_cleanup_broken_managed_runtime"]["classification"], "NON_MEDIA_FILESYSTEM")
        self.assertEqual(by_function["_cleanup_initial_browser_password_if_replaced"]["classification"], "CONFIG_STATE")
        self.assertEqual(by_function["_playlist_stamp_download_tags"]["classification"], "STAGING_ONLY")
        self.assertEqual(by_function["_enrich_playlist_file_tags"]["classification"], "STAGING_ONLY")
        for entry in by_function.values():
            self.assertTrue(entry.get("human_reviewed"))

    def test_app_runtime_cleanup_rejects_path_escape(self):
        import app as app_module

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime_dir = root / "runtime"
            runtime_dir.mkdir()
            outside = root / "qjs"
            outside.write_text("outside", encoding="utf-8")
            with patch_app_family(app_module, "_YTDLP_RUNTIME_BIN_DIR", runtime_dir), \
                 patch_app_family(app_module, "_plugin_install_log", []), \
                 patch_app_family(app_module, "_probe_js_runtime", return_value={"ok": False, "error": "bad"}):
                app_module._cleanup_broken_managed_runtime("..\\qjs")
            self.assertTrue(outside.exists())

    def test_initial_browser_password_cleanup_rejects_symlink(self):
        import app as app_module

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "target_secret"
            target.write_text("initial", encoding="utf-8")
            link = root / ".initial_admin_password"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError) as ex:
                self.skipTest(f"symlink creation unavailable: {ex}")
            persisted = root / "persisted_password"
            persisted.write_text("replacement", encoding="utf-8")
            with patch_app_family(app_module, "_INITIAL_BROWSER_PASSWORD_FILE", link), \
                 patch_app_family(app_module, "_PERSISTED_BROWSER_PASSWORD_FILE", persisted), \
                 patch_app_family(app_module, "_first_config_secret", return_value=""), \
                 patch_app_family(app_module, "_browser_password_is_usable", return_value=True):
                app_module._cleanup_initial_browser_password_if_replaced()
            self.assertTrue(link.exists())
            self.assertTrue(target.exists())


if __name__ == "__main__":
    unittest.main()