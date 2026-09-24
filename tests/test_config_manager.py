"""Unit tests for backend/config_manager.py."""

import tempfile
import unittest
from pathlib import Path

from backend.config_manager import (
    ConfigConflictError,
    ConfigError,
    ConfigValidationError,
    compute_revision,
    get_config,
    revert_config,
    save_config,
    validate_config_yaml,
)


class TestConfigManager(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.tmpdir.name) / "config.yaml"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_get_config_nonexistent(self):
        res = get_config(self.config_path)
        self.assertEqual(res["content"], "")
        self.assertEqual(res["revision"], compute_revision(""))
        self.assertFalse(res["has_backup"])
        self.assertIsNone(res["backup_ts"])

    def test_save_and_get_config(self):
        initial_yaml = "library: /music/library.db\ndirectory: /music\n"
        res = save_config(initial_yaml, config_path=self.config_path)
        self.assertTrue(res["ok"])
        self.assertTrue(res["backed_up"])
        self.assertEqual(res["revision"], compute_revision(initial_yaml))

        read_res = get_config(self.config_path)
        self.assertEqual(read_res["content"], initial_yaml)
        self.assertEqual(read_res["revision"], compute_revision(initial_yaml))

    def test_cas_conflict_protection(self):
        v1 = "library: /v1\n"
        save_config(v1, config_path=self.config_path)
        rev1 = compute_revision(v1)

        # Update with wrong expected_revision
        with self.assertRaises(ConfigConflictError):
            save_config("library: /v2\n", expected_revision="wrong_rev", config_path=self.config_path)

        # Update with correct expected_revision succeeds
        v2 = "library: /v2\n"
        res2 = save_config(v2, expected_revision=rev1, config_path=self.config_path)
        self.assertTrue(res2["ok"])
        self.assertEqual(res2["revision"], compute_revision(v2))

    def test_invalid_yaml_rejection(self):
        with self.assertRaises(ConfigValidationError):
            save_config("invalid: [yaml: unclosed", config_path=self.config_path)

        with self.assertRaises(ConfigValidationError):
            save_config("   \n\n", config_path=self.config_path)

    def test_revert_config(self):
        v1 = "plugins: [web, webmanager]\n"
        save_config(v1, config_path=self.config_path)
        rev1 = compute_revision(v1)

        v2 = "plugins: [web, webmanager, fetchart]\n"
        save_config(v2, expected_revision=rev1, config_path=self.config_path)
        rev2 = compute_revision(v2)

        # Check that backup exists
        read_res = get_config(self.config_path)
        self.assertTrue(read_res["has_backup"])

        # Revert to backup with correct revision
        revert_res = revert_config(expected_revision=rev2, config_path=self.config_path)
        self.assertTrue(revert_res["ok"])
        self.assertEqual(revert_res["revision"], rev1)

        # Verify content is back to v1
        read_res_after = get_config(self.config_path)
        self.assertEqual(read_res_after["content"], v1)


if __name__ == "__main__":
    unittest.main()
