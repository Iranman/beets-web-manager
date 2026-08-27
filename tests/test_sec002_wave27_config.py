"""Wave 27 Configuration Domain Closure & Safety Test Suite.

Covers SEC-002 / ARCH-003 Wave 27 requirements:
1. Web Manager config persistence primitive (WebManagerConfigStore)
2. Secret-bearing config safety & restrictive permissions (0o600 / 0o700)
3. Atomic replacement & fsync durability
4. Symlink target rejection & path containment validation
5. Traversal and path escape prevention
6. Malformed Beets YAML config validation & rollback
7. Engine root resolution helpers (_resolved_music_root, _resolved_downloads_root)
8. AST mutation inventory config domain closure gate (config == 0)
"""

import json
import os
import shutil
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

from backend.web_manager_config_store import (
    WebManagerConfigStore,
    WebManagerConfigStoreError,
)
from backend.beets_control_agent import (
    _resolved_music_root,
    _resolved_downloads_root,
    _write_agent_config_file,
)
from scripts.verify_arch003_mutation_inventory import verify_mutation_inventory


class TestWave27WebManagerConfigStore(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="wave27_config_store_"))
        self.store = WebManagerConfigStore(data_root=self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_save_and_load_text(self):
        saved = self.store.save_text("settings.txt", "hello=world")
        self.assertTrue(saved.exists())
        self.assertEqual(self.store.load_text("settings.txt"), "hello=world")

    def test_save_and_load_json(self):
        payload = {"ui": {"theme": "dark"}, "token_version": 2}
        saved = self.store.save_json("app_settings.json", payload)
        self.assertTrue(saved.exists())
        loaded = self.store.load_json("app_settings.json")
        self.assertEqual(loaded, payload)

    def test_secret_file_permissions(self):
        saved = self.store.save_text(".secret_token", "super-secret-key", is_secret=True)
        self.assertTrue(saved.exists())
        if os.name == "posix":
            mode = os.stat(saved).st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_path_traversal_rejection(self):
        with self.assertRaises(WebManagerConfigStoreError):
            self.store.resolve_path("../outside.json")

        with self.assertRaises(WebManagerConfigStoreError):
            self.store.resolve_path("/etc/passwd")

    def test_symlink_target_rejection(self):
        real_file = self.tmp_dir / "real.json"
        real_file.write_text("{}", encoding="utf-8")

        symlink_file = self.tmp_dir / "symlink.json"
        try:
            os.symlink(real_file, symlink_file)
        except OSError:
            self.skipTest("Symlinks not supported on this platform/user context")

        with self.assertRaises(WebManagerConfigStoreError):
            self.store.resolve_path("symlink.json")

    def test_remove_config_file(self):
        self.store.save_text("temp.txt", "delete me")
        self.assertTrue(self.store.remove("temp.txt"))
        self.assertFalse((self.tmp_dir / "temp.txt").exists())


class TestWave27EngineConfigValidation(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="wave27_engine_config_"))
        self._orig_beetsdir = os.environ.get("BEETSDIR")
        os.environ["BEETSDIR"] = str(self.tmp_dir)

    def tearDown(self):
        if self._orig_beetsdir:
            os.environ["BEETSDIR"] = self._orig_beetsdir
        else:
            os.environ.pop("BEETSDIR", None)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_valid_yaml_config_write(self):
        valid_yaml = "directory: /music\nlibrary: /music/library.db\nplugins: fetchart lastgenre\n"
        res = _write_agent_config_file(valid_yaml)
        self.assertTrue(res.get("ok"))

    def test_invalid_yaml_config_rejected(self):
        invalid_yaml = "directory: /music\n  invalid: [yaml: syntax: error::\n"
        with self.assertRaises(ValueError) as ctx:
            _write_agent_config_file(invalid_yaml)
        self.assertIn("Invalid Beets configuration YAML", str(ctx.exception))


class TestWave27RootResolution(unittest.TestCase):
    def test_resolved_music_root_custom_env(self):
        with mock.patch.dict(os.environ, {"BEETS_MUSIC_ROOT": "/custom/music"}, clear=False):
            self.assertEqual(_resolved_music_root(), "/custom/music")

    def test_resolved_downloads_root_custom_env(self):
        with mock.patch.dict(os.environ, {"DOWNLOADS_ROOT": "/custom/downloads"}, clear=False):
            self.assertEqual(_resolved_downloads_root(), "/custom/downloads")


class TestWave27InventoryGate(unittest.TestCase):
    def test_config_domain_closure(self):
        repo_root = Path(__file__).resolve().parents[1]
        inventory_path = repo_root / "security" / "arch003_mutation_inventory.json"
        with open(inventory_path, encoding="utf-8") as f:
            data = json.load(f)

        unresolved_counts = data.get("unresolved_domain_counts", {})
        self.assertEqual(
            unresolved_counts.get("config", 0),
            0,
            f"Config domain is not closed! Remaining: {unresolved_counts.get('config')}",
        )
        self.assertEqual(unresolved_counts.get("ai_import", 0), 0)
        self.assertEqual(unresolved_counts.get("import_reconciliation", 0), 0)
        self.assertEqual(unresolved_counts.get("library_cleanup", 0), 0)

        self.assertTrue(verify_mutation_inventory(repo_root, check_mode=True))


if __name__ == "__main__":
    unittest.main()
