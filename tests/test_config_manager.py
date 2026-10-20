"""Unit tests for backend/config_manager.py."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.config_manager import (
    ConfigCodeExecutionEditRefused,
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


class TestEmbeddedCodeExecutionEditGuard(unittest.TestCase):
    """ADR 0006: with Beets in-process, the browser editor may not change
    config keys that run code inside Web Manager."""

    BASE = "directory: /music\nplugins: [chroma]\n"

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = Path(self.tmpdir.name) / "config.yaml"
        env = mock.patch.dict(os.environ, {"BEETS_ENGINE": "embedded"})
        env.start()
        self.addCleanup(env.stop)

    def _save(self, stored, new):
        self.path.write_text(stored, encoding="utf-8")
        return save_config(new, expected_revision=compute_revision(stored), config_path=self.path)

    def test_refuses_hook_pluginpath_include_and_hook_plugin(self):
        for new in (
            self.BASE + "hook:\n  hooks:\n    - event: import\n      command: touch /tmp/x\n",
            self.BASE + "pluginpath: [/tmp/evil]\n",
            self.BASE + "include: [other.yaml]\n",
            "directory: /music\nplugins: chroma hook\n",
        ):
            with self.subTest(new=new):
                with self.assertRaises(ConfigCodeExecutionEditRefused) as ctx:
                    self._save(self.BASE, new)
                self.assertEqual(ctx.exception.status_code, 403)
                self.assertIn("Edit config.yaml directly", str(ctx.exception))
                self.assertEqual(self.path.read_text(encoding="utf-8"), self.BASE)

    def test_allows_other_edits_including_files_that_already_have_hooks(self):
        stored = self.BASE + "pluginpath: [/opt/p]\nhook:\n  hooks: []\n"
        self.assertTrue(self._save(stored, stored + "import:\n  copy: yes\n")["ok"])
        self.assertTrue(self._save(self.BASE, self.BASE.replace("[chroma]", "[chroma, fetchart]"))["ok"])

    def test_revert_cannot_reintroduce_a_hook(self):
        self.path.write_text(self.BASE + "hook:\n  hooks: []\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"BEETS_ENGINE": "http"}):
            save_config(self.BASE, config_path=self.path)  # backup now holds the hook
        with self.assertRaises(ConfigCodeExecutionEditRefused):
            revert_config(expected_revision=compute_revision(self.BASE), config_path=self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.BASE)

    def test_http_transport_is_unchanged(self):
        with mock.patch.dict(os.environ, {"BEETS_ENGINE": "http"}):
            self.assertTrue(self._save(self.BASE, self.BASE + "pluginpath: [/opt/p]\n")["ok"])

    def test_route_returns_403_with_reason(self):
        import json
        import app as app_module
        stored = self.BASE
        self.path.write_text(stored, encoding="utf-8")
        body = {"content": stored + "pluginpath: [/tmp/evil]\n", "expected_revision": compute_revision(stored)}
        with mock.patch("backend.config_manager.get_config_path", return_value=self.path), \
             app_module.app.test_request_context("/api/config", method="POST", data=json.dumps(body),
                                                 content_type="application/json"):
            resp, status = app_module.save_config()
        self.assertEqual(status, 403)
        self.assertEqual(resp.get_json()["code"], "CONFIG_CODE_EXECUTION_EDIT_REFUSED")
        self.assertIn("pluginpath", resp.get_json()["error"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), stored)


if __name__ == "__main__":
    unittest.main()
