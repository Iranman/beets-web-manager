"""SEC-002 / ARCH-003 Wave 27 config-domain correction tests."""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path
from types import SimpleNamespace

from backend import beets_control_agent as agent
from backend import web_manager_config_store as config_store_module
from backend.web_manager_config_store import (
    WebManagerConfigStore,
    WebManagerConfigStoreConflictError,
    WebManagerConfigStoreError,
)
from scripts.verify_arch003_mutation_inventory import verify_mutation_inventory


def _store_write_worker(root: str, rel: str, content: str, expected_revision: str | None, queue) -> None:
    try:
        store = WebManagerConfigStore(root)
        res = store.save_text(rel, content, expected_revision=expected_revision)
        queue.put(("ok", res.get("revision")))
    except WebManagerConfigStoreConflictError as exc:
        queue.put(("conflict", str(exc)))
    except Exception as exc:  # pragma: no cover - diagnostic path
        queue.put(("error", type(exc).__name__, str(exc)))


class TestWave27WebManagerConfigStore(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="wave27_config_store_"))
        self.store = WebManagerConfigStore(data_root=self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_save_and_load_text_returns_revision_record(self):
        saved = self.store.save_text("settings.txt", "hello=world")
        self.assertTrue(saved["ok"])
        self.assertTrue((self.tmp_dir / "settings.txt").exists())
        self.assertEqual(self.store.load_text("settings.txt"), "hello=world")
        self.assertRegex(saved["revision"], r"^[0-9a-f]{64}$")

    def test_save_and_load_json(self):
        payload = {"ui": {"theme": "dark"}, "token_version": 2}
        saved = self.store.save_json("app_settings.json", payload)
        self.assertTrue(saved["ok"])
        self.assertEqual(self.store.load_json("app_settings.json"), payload)

    def test_existing_write_requires_matching_revision(self):
        first = self.store.save_text("settings.txt", "v1")
        with self.assertRaises(WebManagerConfigStoreConflictError):
            self.store.save_text("settings.txt", "lost update")
        second = self.store.save_text("settings.txt", "v2", expected_revision=first["revision"])
        self.assertNotEqual(first["revision"], second["revision"])
        self.assertEqual(self.store.load_text("settings.txt"), "v2")

    def test_cross_process_stale_write_is_rejected(self):
        first = self.store.save_text("settings.txt", "v1")
        queue = multiprocessing.Queue()
        proc_b = multiprocessing.Process(
            target=_store_write_worker,
            args=(str(self.tmp_dir), "settings.txt", "v2", first["revision"], queue),
        )
        proc_b.start(); proc_b.join(10)
        self.assertEqual(proc_b.exitcode, 0)
        self.assertEqual(queue.get(timeout=5)[0], "ok")

        proc_a = multiprocessing.Process(
            target=_store_write_worker,
            args=(str(self.tmp_dir), "settings.txt", "stale-v3", first["revision"], queue),
        )
        proc_a.start(); proc_a.join(10)
        self.assertEqual(proc_a.exitcode, 0)
        self.assertEqual(queue.get(timeout=5)[0], "conflict")
        self.assertEqual(self.store.load_text("settings.txt"), "v2")

    def test_secret_file_permissions(self):
        saved = self.store.save_text(".secret_token", "super-secret-key", is_secret=True)
        self.assertTrue((self.tmp_dir / ".secret_token").exists())
        if os.name == "posix":
            self.assertEqual(os.stat(self.tmp_dir / ".secret_token").st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(self.tmp_dir).st_mode & 0o777, 0o700)
        self.assertTrue(saved["ok"])

    def test_path_traversal_rejection(self):
        for name in ("../outside.json", "/etc/passwd", "nested\\evil", "C:evil"):
            with self.subTest(name=name):
                with self.assertRaises(WebManagerConfigStoreError):
                    self.store.resolve_path(name)

    def test_symlink_target_and_parent_rejected(self):
        if os.name == "nt":
            self.skipTest("Windows symlink permissions vary by runner")
        real_dir = self.tmp_dir / "real"
        real_dir.mkdir()
        link_dir = self.tmp_dir / "linkdir"
        os.symlink(real_dir, link_dir)
        with self.assertRaises(WebManagerConfigStoreError):
            self.store.save_text("linkdir/settings.txt", "blocked")

        real_file = self.tmp_dir / "real.json"
        real_file.write_text("{}", encoding="utf-8")
        symlink_file = self.tmp_dir / "symlink.json"
        os.symlink(real_file, symlink_file)
        with self.assertRaises(WebManagerConfigStoreError):
            self.store.read_text_record("symlink.json")

    def test_symlink_config_root_rejected_before_resolution(self):
        if os.name == "nt":
            self.skipTest("Windows symlink permissions vary by runner")
        real_root = self.tmp_dir / "real-root"
        real_root.mkdir()
        link_root = self.tmp_dir / "link-root"
        os.symlink(real_root, link_root)
        with self.assertRaises(WebManagerConfigStoreError):
            WebManagerConfigStore(link_root)

    def test_remove_requires_current_revision(self):
        saved = self.store.save_text("temp.txt", "delete me")
        with self.assertRaises(WebManagerConfigStoreConflictError):
            self.store.remove("temp.txt")
        self.assertTrue(self.store.remove("temp.txt", expected_revision=saved["revision"]))
        self.assertFalse((self.tmp_dir / "temp.txt").exists())

    def test_default_store_uses_web_manager_data_dir_not_metadata_cache(self):
        root = self.tmp_dir / "webdata"
        cache = self.tmp_dir / "cache"
        with mock.patch.dict(os.environ, {"WEB_MANAGER_DATA_DIR": str(root), "METADATA_CACHE_DIR": str(cache)}, clear=False):
            config_store_module._default_config_store = None
            store = config_store_module.get_config_store()
        self.assertEqual(store.data_root, root.resolve(strict=True))
        self.assertFalse(cache.exists())


class TestWave27EngineConfigValidation(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="wave27_engine_config_"))
        self.config_path = self.tmp_dir / "config.yaml"
        self.backup_path = self.tmp_dir / "config.yaml.bak"
        self.initial = "directory: /music\nlibrary: /config/library.blb\nplugins: []\n"
        self.config_path.write_text(self.initial, encoding="utf-8")
        self.patches = [
            mock.patch.object(agent, "BEETSDIR", str(self.tmp_dir)),
            mock.patch.object(agent, "BEET_BIN", "beet"),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _revision(self) -> str:
        return agent._config_revision(self.config_path)

    def _successful_beets_config(self, *args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="directory: /music\n", stderr="")

    def test_valid_config_write_uses_real_beets_loader_and_cas(self):
        revision = self._revision()
        new_yaml = "directory: /music\nlibrary: /config/library.blb\nplugins: fetchart\n"
        with mock.patch.object(agent.subprocess, "run", side_effect=self._successful_beets_config) as run:
            res = agent._write_agent_config_file(new_yaml, expected_revision=revision)
        self.assertTrue(res["ok"])
        self.assertNotEqual(res["revision"], revision)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), new_yaml)
        self.assertEqual(self.backup_path.read_text(encoding="utf-8"), self.initial)
        self.assertGreaterEqual(run.call_count, 2)

    def test_malformed_yaml_rejected_before_beets_loader_or_replace(self):
        revision = self._revision()
        with mock.patch.object(agent.subprocess, "run") as run:
            with self.assertRaises(agent.ConfigValidationError) as ctx:
                agent._write_agent_config_file("directory: [unterminated\n", expected_revision=revision)
        self.assertEqual(getattr(ctx.exception, "error_code", ""), "config_invalid_yaml")
        run.assert_not_called()
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), self.initial)

    def test_beets_invalid_candidate_is_rejected_before_replace(self):
        revision = self._revision()
        failed = SimpleNamespace(returncode=1, stdout="", stderr="bad plugin secret-token")
        with mock.patch.object(agent.subprocess, "run", return_value=failed):
            with self.assertRaises(agent.ConfigValidationError):
                agent._write_agent_config_file("plugins: definitely_not_real\n", expected_revision=revision)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), self.initial)
        self.assertFalse(self.backup_path.exists())

    def test_beets_plugin_loader_error_is_rejected_even_with_zero_exit(self):
        revision = self._revision()
        failed = SimpleNamespace(
            returncode=0,
            stdout="** error loading plugin definitely_not_real\nTraceback (most recent call last):\n",
            stderr="",
        )
        with mock.patch.object(agent.subprocess, "run", return_value=failed):
            with self.assertRaises(agent.ConfigValidationError):
                agent._write_agent_config_file("plugins: definitely_not_real\n", expected_revision=revision)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), self.initial)
        self.assertFalse(self.backup_path.exists())
    def test_stale_revision_rejected_before_write(self):
        revision = self._revision()
        self.config_path.write_text("directory: /new\n", encoding="utf-8")
        with mock.patch.object(agent.subprocess, "run") as run:
            with self.assertRaises(agent.ConfigRevisionConflict):
                agent._write_agent_config_file("directory: /stale\n", expected_revision=revision)
        run.assert_not_called()
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), "directory: /new\n")

    def test_post_write_validation_failure_restores_backup(self):
        revision = self._revision()
        def fake_run(cmd, **kwargs):
            if str(self.config_path) in cmd:
                return SimpleNamespace(returncode=1, stdout="", stderr="post write failure")
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(agent.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(agent.ConfigPostWriteValidationError):
                agent._write_agent_config_file("directory: /changed\nlibrary: /config/library.blb\n", expected_revision=revision)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), self.initial)

    def test_revert_requires_revision_and_validates_backup(self):
        self.backup_path.write_text("directory: /backup\nlibrary: /config/library.blb\n", encoding="utf-8")
        revision = self._revision()
        with mock.patch.object(agent.subprocess, "run", side_effect=self._successful_beets_config) as run:
            res = agent._revert_agent_config_file(expected_revision=revision)
        self.assertTrue(res["ok"])
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), self.backup_path.read_text(encoding="utf-8"))
        run.assert_called()

    def test_symlink_beetsdir_root_rejected(self):
        if os.name == "nt":
            self.skipTest("Windows symlink permissions vary by runner")
        real_root = self.tmp_dir / "real"
        real_root.mkdir()
        link_root = self.tmp_dir / "link"
        os.symlink(real_root, link_root)
        with mock.patch.object(agent, "BEETSDIR", str(link_root)):
            with self.assertRaises(PermissionError):
                with agent._agent_config_process_lock():
                    pass


class TestWave27RootResolution(unittest.TestCase):
    def test_resolved_music_root_custom_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = str(Path(tmp) / "music")
            with mock.patch.dict(os.environ, {"BEETS_MUSIC_ROOT": root}, clear=False):
                self.assertEqual(agent._resolved_music_root(), str(Path(root).resolve(strict=False)))

    def test_resolved_downloads_root_custom_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = str(Path(tmp) / "downloads")
            with mock.patch.dict(os.environ, {"DOWNLOADS_ROOT": root}, clear=False):
                self.assertEqual(agent._resolved_downloads_root(), str(Path(root).resolve(strict=False)))

    def test_staging_defaults_to_downloads_root_when_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloads = str(Path(tmp) / "downloads")
            with mock.patch.dict(os.environ, {"DOWNLOADS_ROOT": downloads}, clear=False):
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("STAGING_ROOT", None)
                    self.assertEqual(agent._resolved_staging_root(), str(Path(downloads).resolve(strict=False)))


    def test_allowed_roots_use_configured_roots_not_phantom_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_root = root / "config"
            music_root = root / "library"
            downloads_root = root / "downloads"
            staging_root = root / "stage"
            with mock.patch.object(agent, "BEETSDIR", str(config_root)):
                with mock.patch.dict(os.environ, {
                    "MUSIC_ROOT": str(music_root),
                    "DOWNLOADS_ROOT": str(downloads_root),
                    "STAGING_ROOT": str(staging_root),
                }, clear=False):
                    roots = agent._allowed_root_paths(["config", "music", "staging"])
            self.assertIn(str(config_root.resolve(strict=False)), roots)
            self.assertIn(str(music_root.resolve(strict=False)), roots)
            self.assertIn(str(downloads_root.resolve(strict=False)), roots)
            self.assertIn(str(staging_root.resolve(strict=False)), roots)
            self.assertNotIn(str(Path("/data/media/music").resolve(strict=False)), roots)
            self.assertNotIn(str(Path("/data/torrents").resolve(strict=False)), roots)

    def test_status_payload_reports_resolved_staging_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_root = root / "config"
            music_root = root / "library"
            downloads_root = root / "downloads"
            staging_root = root / "stage"
            for directory in (config_root, music_root, downloads_root, staging_root):
                directory.mkdir(parents=True, exist_ok=True)
            (config_root / "config.yaml").write_text("directory: /library\n", encoding="utf-8")
            with mock.patch.object(agent, "BEETSDIR", str(config_root)):
                with mock.patch.dict(os.environ, {
                    "MUSIC_ROOT": str(music_root),
                    "DOWNLOADS_ROOT": str(downloads_root),
                    "STAGING_ROOT": str(staging_root),
                }, clear=False):
                    with mock.patch.object(agent, "_cached_beet_version_snapshot", return_value={"available": True, "loaded_plugins": [], "version": "beets version 2.0.0"}):
                        payload = agent._agent_status_payload(force_refresh=True)
            paths = payload["paths"]
            self.assertEqual(paths["music_library"]["path"], str(music_root.resolve(strict=False)))
            self.assertEqual(paths["downloads"]["path"], str(downloads_root.resolve(strict=False)))
            self.assertEqual(paths["staging"]["path"], str(staging_root.resolve(strict=False)))

class TestWave27InventoryGate(unittest.TestCase):
    def test_config_domain_closure(self):
        repo_root = Path(__file__).resolve().parents[1]
        inventory_path = repo_root / "security" / "arch003_mutation_inventory.json"
        with open(inventory_path, encoding="utf-8") as f:
            data = json.load(f)
        unresolved_counts = data.get("unresolved_domain_counts", {})
        self.assertEqual(unresolved_counts.get("config", 0), 0)
        self.assertEqual(unresolved_counts.get("ai_import", 0), 0)
        self.assertEqual(unresolved_counts.get("import_reconciliation", 0), 0)
        self.assertEqual(unresolved_counts.get("library_cleanup", 0), 0)
        self.assertEqual(sum(1 for e in data.get("inventory", []) if e.get("classification") == "NEEDS_REVIEW"), 0)
        self.assertTrue(verify_mutation_inventory(repo_root, check_mode=True))


if __name__ == "__main__":
    multiprocessing.freeze_support()
    unittest.main()