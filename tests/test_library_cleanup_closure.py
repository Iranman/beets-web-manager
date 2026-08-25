import ast
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import transaction_engine
from backend.beets_client import BeetsUnavailableError


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (REPO_ROOT / "app.py").read_text(encoding="utf-8")


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
        self.db_path = self.root / "library.db"
        con = sqlite3.connect(self.db_path)
        try:
            con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB, album_id INT, title TEXT, artist TEXT, album TEXT)")
            con.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, album TEXT, albumartist TEXT, artpath BLOB)")
            con.execute("CREATE TABLE item_attributes (id INTEGER PRIMARY KEY, entity_id INT, key TEXT, value TEXT)")
            con.commit()
        finally:
            con.close()
        self.store = transaction_engine.TransactionStore(str(self.root / "transactions"))

    def tearDown(self):
        self.tmpdir.cleanup()

    def _insert_item(self, item_id: int, path: Path, album_id: int = 1) -> None:
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(
                "INSERT INTO items (id, path, album_id, title, artist, album) VALUES (?, ?, ?, ?, ?, ?)",
                (item_id, str(path).encode("utf-8"), album_id, f"Track {item_id}", "Artist", "Album"),
            )
            con.commit()
        finally:
            con.close()

    def _item_count(self, item_id: int) -> int:
        con = sqlite3.connect(self.db_path)
        try:
            return int(con.execute("SELECT COUNT(*) FROM items WHERE id=?", (item_id,)).fetchone()[0])
        finally:
            con.close()

    def test_duplicate_cleanup_plan_apply_and_rollback(self):
        dup = self.music_dir / "Artist" / "Album" / "duplicate.mp3"
        dup.parent.mkdir(parents=True)
        original_bytes = b"duplicate audio"
        dup.write_bytes(original_bytes)
        self._insert_item(11, dup)

        plan = transaction_engine.create_library_cleanup_plan(
            self.store,
            {"action": "dedup_cleanup", "paths": [str(dup)]},
            music_allowed_roots=[str(self.music_dir)],
            staging_allowed_roots=[str(self.staging_dir)],
            db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_dir),
        )
        self.assertTrue(plan.get("ok"), msg=plan)
        self.assertEqual(plan.get("planned_count"), 1)
        self.assertTrue(dup.exists())
        self.assertEqual(self._item_count(11), 1)

        apply = transaction_engine.execute_library_cleanup_apply(
            self.store,
            plan["operation_id"],
            music_allowed_roots=[str(self.music_dir)],
            staging_allowed_roots=[str(self.staging_dir)],
            db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_dir),
        )
        self.assertTrue(apply.get("ok"), msg=apply)
        self.assertFalse(dup.exists())
        self.assertEqual(self._item_count(11), 0)
        quarantined = list((self.quarantine_dir / plan["operation_id"]).iterdir())
        self.assertEqual(len(quarantined), 1)

        rollback = transaction_engine.rollback_library_cleanup(
            self.store,
            plan["operation_id"],
            music_allowed_roots=[str(self.music_dir)],
            staging_allowed_roots=[str(self.staging_dir)],
            db_path=str(self.db_path),
        )
        self.assertTrue(rollback.get("ok"), msg=rollback)
        self.assertEqual(dup.read_bytes(), original_bytes)
        self.assertEqual(self._item_count(11), 1)

    def test_duplicate_cleanup_rejects_outside_root(self):
        outside = self.root / "outside.mp3"
        outside.write_bytes(b"audio")
        plan = transaction_engine.create_library_cleanup_plan(
            self.store,
            {"paths": [str(outside)]},
            music_allowed_roots=[str(self.music_dir)],
            staging_allowed_roots=[str(self.staging_dir)],
            db_path=str(self.db_path),
        )
        self.assertTrue(plan.get("ok"), msg=plan)
        self.assertEqual(plan.get("planned_count"), 0)
        self.assertEqual(plan["results"][0].get("code"), "library_cleanup_path_out_of_root")
        self.assertTrue(outside.exists())

    def test_duplicate_cleanup_rejects_symlink_source(self):
        real_file = self.music_dir / "real.mp3"
        real_file.write_bytes(b"audio")
        link = self.music_dir / "link.mp3"
        try:
            link.symlink_to(real_file)
        except (OSError, NotImplementedError) as ex:
            self.skipTest(f"symlink creation unavailable: {ex}")
        plan = transaction_engine.create_library_cleanup_plan(
            self.store,
            {"paths": [str(link)]},
            music_allowed_roots=[str(self.music_dir)],
            staging_allowed_roots=[str(self.staging_dir)],
            db_path=str(self.db_path),
        )
        self.assertEqual(plan.get("planned_count"), 0)
        self.assertEqual(plan["results"][0].get("code"), "library_cleanup_symlink_rejected")
        self.assertTrue(real_file.exists())

    def test_duplicate_cleanup_apply_detects_stale_file(self):
        dup = self.music_dir / "Artist" / "Album" / "changed.mp3"
        dup.parent.mkdir(parents=True)
        dup.write_bytes(b"before")
        self._insert_item(12, dup)
        plan = transaction_engine.create_library_cleanup_plan(
            self.store,
            {"paths": [str(dup)]},
            music_allowed_roots=[str(self.music_dir)],
            staging_allowed_roots=[str(self.staging_dir)],
            db_path=str(self.db_path),
        )
        dup.write_bytes(b"after")
        apply = transaction_engine.execute_library_cleanup_apply(
            self.store,
            plan["operation_id"],
            music_allowed_roots=[str(self.music_dir)],
            staging_allowed_roots=[str(self.staging_dir)],
            db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_dir),
        )
        self.assertFalse(apply.get("ok"), msg=apply)
        self.assertEqual(apply.get("code"), "library_cleanup_toctou_mismatch")
        self.assertTrue(dup.exists())
        self.assertEqual(self._item_count(12), 1)

    def test_duplicate_cleanup_blocks_ambiguous_db_references(self):
        dup = self.music_dir / "Artist" / "Album" / "ambiguous.mp3"
        dup.parent.mkdir(parents=True)
        dup.write_bytes(b"audio")
        self._insert_item(13, dup)
        self._insert_item(14, dup)
        plan = transaction_engine.create_library_cleanup_plan(
            self.store,
            {"paths": [str(dup)]},
            music_allowed_roots=[str(self.music_dir)],
            staging_allowed_roots=[str(self.staging_dir)],
            db_path=str(self.db_path),
        )
        self.assertEqual(plan.get("planned_count"), 0)
        self.assertEqual(plan["results"][0].get("code"), "library_cleanup_db_reference_mismatch")
        self.assertTrue(dup.exists())

    def test_folder_cleanup_blocks_db_reference_and_unexpected_content(self):
        empty_dir = self.music_dir / "Empty"
        empty_dir.mkdir()
        plan = transaction_engine.create_folder_cleanup_plan(
            self.store,
            {"action": "remove_empty", "source": str(empty_dir)},
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertTrue(plan.get("ok"), msg=plan)
        (empty_dir / "surprise.txt").write_text("unexpected", encoding="utf-8")
        apply = transaction_engine.execute_folder_cleanup_apply(
            self.store,
            plan["operation_id"],
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertFalse(apply.get("ok"), msg=apply)
        self.assertEqual(apply.get("code"), "folder_cleanup_not_empty")
        self.assertTrue(empty_dir.exists())

        referenced = self.music_dir / "Referenced"
        referenced.mkdir()
        self._insert_item(15, referenced / "ghost.mp3")
        blocked = transaction_engine.create_folder_cleanup_plan(
            self.store,
            {"action": "remove_empty", "source": str(referenced)},
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertFalse(blocked.get("ok"), msg=blocked)
        self.assertEqual(blocked.get("code"), "folder_cleanup_db_references")

    def test_folder_cleanup_removes_and_rolls_back_empty_directory(self):
        empty_dir = self.music_dir / "RollbackEmpty"
        empty_dir.mkdir()
        plan = transaction_engine.create_folder_cleanup_plan(
            self.store,
            {"action": "remove_empty", "source": str(empty_dir)},
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        apply = transaction_engine.execute_folder_cleanup_apply(
            self.store,
            plan["operation_id"],
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
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
        for func in ("dedup_cleanup", "_album_cleanup_remove_empty_tree"):
            src = self._function_source(func)
            for needle in banned:
                self.assertNotIn(needle, src, msg=f"{func} still contains {needle}")
        self.assertIn("beets_client.plan_library_cleanup", self._function_source("dedup_cleanup"))
        self.assertIn("beets_client.apply_library_cleanup", self._function_source("dedup_cleanup"))
        self.assertIn("beets_client.plan_folder_cleanup", self._function_source("_album_cleanup_remove_empty_tree"))

    def test_dedup_cleanup_engine_unavailable_fails_closed(self):
        import app as app_module

        with tempfile.TemporaryDirectory() as td:
            candidate = Path(td) / "song.mp3"
            candidate.write_bytes(b"audio")
            with mock.patch.object(app_module.beets_client, "plan_library_cleanup", side_effect=BeetsUnavailableError("offline")):
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
            with mock.patch.object(app_module, "_YTDLP_RUNTIME_BIN_DIR", runtime_dir), \
                 mock.patch.object(app_module, "_plugin_install_log", []), \
                 mock.patch.object(app_module, "_probe_js_runtime", return_value={"ok": False, "error": "bad"}):
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
            with mock.patch.object(app_module, "_INITIAL_BROWSER_PASSWORD_FILE", link), \
                 mock.patch.object(app_module, "_PERSISTED_BROWSER_PASSWORD_FILE", persisted), \
                 mock.patch.object(app_module, "_first_config_secret", return_value=""), \
                 mock.patch.object(app_module, "_browser_password_is_usable", return_value=True):
                app_module._cleanup_initial_browser_password_if_replaced()
            self.assertTrue(link.exists())
            self.assertTrue(target.exists())


if __name__ == "__main__":
    unittest.main()