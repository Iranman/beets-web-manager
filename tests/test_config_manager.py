"""Unit tests for backend/config_manager.py."""

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
        initial_yaml = "va_name: Various\nimport:\n  copy: yes\n"
        res = save_config(initial_yaml, config_path=self.config_path)
        self.assertTrue(res["ok"])
        self.assertTrue(res["backed_up"])
        self.assertEqual(res["revision"], compute_revision(initial_yaml))

        read_res = get_config(self.config_path)
        self.assertEqual(read_res["content"], initial_yaml)
        self.assertEqual(read_res["revision"], compute_revision(initial_yaml))

    def test_cas_conflict_protection(self):
        v1 = "va_name: v1\n"
        save_config(v1, config_path=self.config_path)
        rev1 = compute_revision(v1)

        # Update with wrong expected_revision
        with self.assertRaises(ConfigConflictError):
            save_config("va_name: v2\n", expected_revision="wrong_rev", config_path=self.config_path)

        # Update with correct expected_revision succeeds
        v2 = "va_name: v2\n"
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

    def _save(self, stored, new):
        self.path.write_text(stored, encoding="utf-8")
        return save_config(new, expected_revision=compute_revision(stored), config_path=self.path)

    def _refused(self, stored, new):
        with self.assertRaises(ConfigCodeExecutionEditRefused) as ctx:
            self._save(stored, new)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(self.path.read_text(encoding="utf-8"), stored)
        return str(ctx.exception)

    def test_refuses_hook_pluginpath_include_and_hook_plugin(self):
        for new in (
            self.BASE + "hook:\n  hooks:\n    - event: import\n      command: touch /tmp/x\n",
            self.BASE + "pluginpath: [/tmp/evil]\n",
            self.BASE + "include: [other.yaml]\n",
            "directory: /music\nplugins: chroma hook\n",
        ):
            with self.subTest(new=new):
                self.assertIn("Edit config.yaml directly", self._refused(self.BASE, new))

    def test_refuses_inline_expression_fields(self):
        msg = self._refused(self.BASE, "directory: /music\nplugins: [chroma, inline]\n"
                                       "item_fields:\n  x: '1'\n")
        self.assertIn("the inline plugin", msg)
        self.assertIn("item_fields.x", msg)
        # Even with inline already enabled, new expressions are refused.
        stored = "plugins: [inline]\nitem_fields:\n  x: '1'\n"
        self.assertIn("album_fields.y", self._refused(stored, stored + "album_fields:\n  y: '2'\n"))

    def test_refuses_plugin_command_options(self):
        stored = "plugins: [convert, chroma]\nconvert:\n  format: mp3\n"
        self.assertIn("convert.command", self._refused(stored, stored + "  command: x\n"))
        for plugin in ("badfiles", "keyfinder", "replaygain", "duplicates", "mbsubmit", "convert"):
            with self.subTest(plugin=plugin):
                self.assertIn(f"the {plugin} plugin",
                              self._refused(self.BASE, self.BASE.replace("[chroma]", f"[chroma, {plugin}]")))

    def test_refuses_locked_path_inside_a_new_editable_section(self):
        self.assertIn("import.log", self._refused(self.BASE, self.BASE + "import:\n  log: /tmp/x\n"))
        self.assertIn("fetchart.fallback", self._refused(self.BASE, self.BASE + "fetchart:\n  fallback: /tmp/x\n"))

    def test_refuses_import_set_fields_and_replace(self):
        # #461 F1/F2: set_fields can set `path`; `replace` holds the rules that
        # keep path templates inside `directory`.
        self.assertIn("import.set_fields",
                      self._refused(self.BASE, self.BASE + "import:\n  set_fields:\n    genre: Rock\n"))
        self.assertIn("replace", self._refused(self.BASE, self.BASE + "replace:\n  'x': y\n"))
        stored = self.BASE + "replace:\n  '^\\.': _\n"
        self.assertIn("replace", self._refused(stored, self.BASE))

    def test_path_templates_stay_editable(self):
        self.assertTrue(self._save(self.BASE, self.BASE + "paths:\n  default: $albumartist/$album/$title\n"
                                   "art_filename: folder\nimport:\n  move: yes\n")["ok"])

    def test_path_templates_are_locked_with_a_custom_replace_or_include(self):
        # #461 F2: only Beets' default `replace` rules are known to keep
        # templates inside `directory`.
        for stored in (self.BASE + "replace:\n  'x': y\n", self.BASE + "include: [more.yaml]\n"):
            with self.subTest(stored=stored):
                self.assertIn("paths.default", self._refused(stored, stored + "paths:\n  default: $album/$title\n"))
                self.assertIn("art_filename", self._refused(stored, stored + "art_filename: folder\n"))
                self.assertTrue(self._save(stored, stored + "va_name: VA\n")["ok"])

    def test_validation_refuses_aliases_non_text_keys_and_oversized_text(self):
        # #461 F3: decided on the node graph, before construction, in every mode.
        from backend.config_manager import validate_config_yaml
        cases = {
            "match: &m {}\nui: *m\n": "aliases",
            "match: &m {x: 1}\nui:\n  <<: *m\n": "aliases",
            "match:\n  1: x\n": "keys must be text",
            "a: " + "x" * (1024 * 1024) + "\n": "larger than 1 MiB",
            "a: " + "[" * 5000 + "]" * 5000 + "\n": "nested too deeply",
            "match: 2026-99-99\n": "could not be read",
        }
        for text, reason in cases.items():
            with self.subTest(reason=reason):
                ok, msg = validate_config_yaml(text)
                self.assertFalse(ok)
                self.assertIn(reason, msg)
        self.assertEqual(validate_config_yaml("match:\n  '1': x\n<<: {a: 1}\n"), (True, ""))
        with self.assertRaises(ConfigValidationError):
            self._save(self.BASE, self.BASE + "match: &m {}\nui: *m\n")
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.BASE)

    def test_refuses_unparseable_or_non_mapping_text(self):
        from backend.config_manager import refuse_code_execution_edits
        for new in ("- just\n- a list\n", "a: [\n"):
            with self.subTest(new=new), self.assertRaises(ConfigCodeExecutionEditRefused):
                refuse_code_execution_edits(self.BASE, new)

    def test_allows_safe_edits_including_files_that_already_have_hooks(self):
        stored = self.BASE + "pluginpath: [/opt/p]\nhook:\n  hooks: []\n"
        self.assertTrue(self._save(stored, stored + "import:\n  copy: yes\n")["ok"])
        self.assertTrue(self._save(self.BASE, self.BASE.replace("[chroma]", "[chroma, fetchart]")
                                   + "match:\n  strong_rec_thresh: 0.1\nfetchart:\n  minwidth: 500\n")["ok"])
        # Removing a plugin is always allowed.
        self.assertTrue(self._save(self.BASE, "directory: /music\nplugins: []\n")["ok"])

    def test_revert_cannot_reintroduce_a_hook(self):
        from backend.config_manager import get_config_backup_path
        self.path.write_text(self.BASE, encoding="utf-8")
        # A backup left from the two-container layout may still hold a hook.
        get_config_backup_path(self.path).write_text(self.BASE + "hook:\n  hooks: []\n", encoding="utf-8")
        with self.assertRaises(ConfigCodeExecutionEditRefused):
            revert_config(expected_revision=compute_revision(self.BASE), config_path=self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.BASE)

    def test_apply_recommended_plugins_cannot_add_pluginpath(self):
        from backend.beets_plugins import apply_recommended_plugins
        self.path.write_text(self.BASE, encoding="utf-8")
        with self.assertRaises(ConfigCodeExecutionEditRefused) as ctx:
            apply_recommended_plugins(self.path, ["fetchart"])
        self.assertIn("pluginpath", str(ctx.exception))
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.BASE)

    def test_provision_route_is_refused(self):
        import app as app_module
        self.path.write_text(self.BASE, encoding="utf-8")
        view = app_module.app.view_functions["plugins_provision"]
        with mock.patch("backend.config_manager.get_config_path", return_value=self.path), \
             mock.patch.dict(view.__globals__, {"_setup_csrf_failure": lambda: None}), \
             app_module.app.test_request_context("/api/setup/plugins/provision", method="POST"):
            resp, status = view()
        self.assertEqual(status, 403)
        self.assertEqual(resp.get_json()["code"], "CONFIG_CODE_EXECUTION_EDIT_REFUSED")
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.BASE)
        self.assertFalse((self.path.parent / "beetsplug").exists())

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
