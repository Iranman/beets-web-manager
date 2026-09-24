"""Comprehensive test suite for Beets Plugin Management.

Tests:
1. Manifest completeness and categorization (REQUIRED, OPTIONAL, INTEGRATION).
2. Bundled plugin provisioning to `/config/beetsplug` and preservation of user plugins.
3. Safe YAML config updates, backups, idempotence, and custom plugin preservation.
4. Plugin health verification engine across dependencies and runtimes.
5. Setup wizard and plugin management REST API endpoints.
6. Setup completion gating on required plugin health.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.beets_plugins import (
    BEETS_PLUGIN_MANIFEST,
    INTEGRATION_PLUGIN_NAMES,
    OPTIONAL_PLUGIN_NAMES,
    REQUIRED_PLUGIN_NAMES,
    PluginCategory,
    PluginType,
    parse_configured_pluginpath,
    parse_configured_plugins,
    provision_and_verify,
    provision_bundled_plugins,
    update_config_yaml_plugins,
    verify_all_plugins,
    verify_plugin,
)
from tests.test_routes_setup import _load_routes_setup_against_stub_app


class BeetsPluginManifestTests(unittest.TestCase):
    def test_manifest_contains_all_required_plugins(self):
        self.assertEqual(len(REQUIRED_PLUGIN_NAMES), 13)
        for req in (
            "musicbrainz", "chroma", "fetchart", "embedart", "scrub",
            "zero", "ftintitle", "fromfilename", "mbsync", "mbsubmit",
            "replaygain", "lastgenre", "discpath"
        ):
            self.assertIn(req, REQUIRED_PLUGIN_NAMES)
            pdef = BEETS_PLUGIN_MANIFEST[req]
            self.assertEqual(pdef.category, PluginCategory.REQUIRED)

    def test_bundled_discpath_definition(self):
        discpath = BEETS_PLUGIN_MANIFEST["discpath"]
        self.assertEqual(discpath.plugin_type, PluginType.BUNDLED)
        self.assertEqual(discpath.bundled_file, "discpath.py")
        self.assertIn("disc_subfolder", discpath.template_fields)

    def test_chroma_dependencies(self):
        # pyacoustid/fpcalc run inside the stock Beets container, never
        # inside Web Manager -- chroma's health must come from stock
        # Beets' own live loaded_plugins signal, not a local Python
        # package check against the wrong process (Web Manager has no
        # pyacoustid dependency of its own; see requirements.txt).
        chroma = BEETS_PLUGIN_MANIFEST["chroma"]
        self.assertNotIn("pyacoustid==1.3.1", chroma.python_packages)
        self.assertIn("fpcalc", chroma.binary_dependencies)

    def test_optional_and_integration_plugins(self):
        for opt in ("convert", "duplicates", "missing", "smartplaylist", "unimported", "lyrics", "parentwork", "edit", "web", "hook"):
            self.assertIn(opt, OPTIONAL_PLUGIN_NAMES)

        for integ in ("listenbrainz", "deezer", "discogs", "spotify", "plexsync", "bpsync"):
            self.assertIn(integ, INTEGRATION_PLUGIN_NAMES)


class BeetsBundledProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.config_dir = Path(self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_provision_copies_discpath(self):
        provisioned = provision_bundled_plugins(self.config_dir)
        self.assertIn("discpath.py", provisioned)
        target_file = self.config_dir / "beetsplug" / "discpath.py"
        self.assertTrue(target_file.exists())
        content = target_file.read_text(encoding="utf-8")
        self.assertIn("disc_subfolder", content)

    def test_provision_is_idempotent(self):
        first_run = provision_bundled_plugins(self.config_dir)
        self.assertIn("discpath.py", first_run)
        second_run = provision_bundled_plugins(self.config_dir)
        self.assertIn("discpath.py", second_run)
        target_file = self.config_dir / "beetsplug" / "discpath.py"
        self.assertTrue(target_file.exists())

    def test_user_plugins_in_beetsplug_are_preserved(self):
        beetsplug_dir = self.config_dir / "beetsplug"
        beetsplug_dir.mkdir(parents=True, exist_ok=True)
        user_plugin = beetsplug_dir / "my_custom_plugin.py"
        user_plugin.write_text("# custom user code\n", encoding="utf-8")

        provision_bundled_plugins(self.config_dir)

        self.assertTrue(user_plugin.exists())
        self.assertEqual(user_plugin.read_text(encoding="utf-8"), "# custom user code\n")
        self.assertTrue((beetsplug_dir / "discpath.py").exists())


class BeetsConfigYamlManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.config_dir = Path(self.tmp_dir)
        self.config_path = self.config_dir / "config.yaml"

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_parse_configured_plugins_inline_and_multiline(self):
        yaml_inline = "plugins: fetchart embedart scrub discpath\n"
        self.assertEqual(
            parse_configured_plugins(yaml_inline),
            ["fetchart", "embedart", "scrub", "discpath"]
        )

        yaml_multiline = (
            "plugins:\n"
            "  - fetchart\n"
            "  - chroma\n"
            "  - custom_user_plugin\n"
        )
        self.assertEqual(
            parse_configured_plugins(yaml_multiline),
            ["fetchart", "chroma", "custom_user_plugin"]
        )

    def test_parse_configured_pluginpath(self):
        yaml_path = (
            "pluginpath:\n"
            "  - /config/beetsplug\n"
            "  - /custom/plugins\n"
        )
        self.assertEqual(
            parse_configured_pluginpath(yaml_path),
            ["/config/beetsplug", "/custom/plugins"]
        )

    def test_update_config_creates_default_if_missing(self):
        changed, msg = update_config_yaml_plugins(self.config_path)
        self.assertTrue(changed)
        self.assertTrue(self.config_path.exists())
        content = self.config_path.read_text(encoding="utf-8")
        plugins = parse_configured_plugins(content)
        for req in ("fetchart", "chroma", "discpath", "scrub"):
            self.assertIn(req, plugins)
        pluginpath = parse_configured_pluginpath(content)
        self.assertIn("/config/beetsplug", pluginpath)

    def test_update_config_preserves_custom_plugins_and_settings(self):
        initial_yaml = (
            "plugins: fetchart my_awesome_custom_plugin embedart\n"
            "pluginpath:\n"
            "  - /config/beetsplug\n"
            "directory: /custom/music/path\n"
            "custom_user_setting: true\n"
        )
        self.config_path.write_text(initial_yaml, encoding="utf-8")

        changed, msg = update_config_yaml_plugins(self.config_path)
        self.assertTrue(changed)
        content = self.config_path.read_text(encoding="utf-8")

        # Custom plugin preserved
        self.assertIn("my_awesome_custom_plugin", content)
        # Custom setting preserved
        self.assertIn("custom_user_setting: true", content)
        self.assertIn("directory: /custom/music/path", content)

        # All required plugins now present
        plugins = parse_configured_plugins(content)
        for req in ("chroma", "discpath", "scrub", "mbsync"):
            self.assertIn(req, plugins)
        self.assertIn("my_awesome_custom_plugin", plugins)

    def test_update_config_creates_backup(self):
        initial_yaml = "plugins: fetchart\n"
        self.config_path.write_text(initial_yaml, encoding="utf-8")

        changed, _ = update_config_yaml_plugins(self.config_path, backup=True)
        self.assertTrue(changed)

        backups = list(self.config_dir.glob("config.yaml.bak-plugins-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), initial_yaml)

    def test_update_config_idempotence(self):
        # First update
        update_config_yaml_plugins(self.config_path)
        first_content = self.config_path.read_text(encoding="utf-8")

        # Second update
        changed, msg = update_config_yaml_plugins(self.config_path)
        self.assertFalse(changed)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), first_content)


class BeetsPluginVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.config_dir = Path(self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_verify_plugin_healthy_when_all_dependencies_met(self):
        pdef = BEETS_PLUGIN_MANIFEST["fetchart"]
        configured = {"fetchart"}
        loaded = {"fetchart"}
        status = verify_plugin(pdef, configured, loaded, self.config_dir)
        self.assertTrue(status.healthy)
        self.assertTrue(status.enabled)
        self.assertTrue(status.loaded)
        self.assertEqual(status.note, "Ready")

    def test_verify_plugin_unhealthy_when_not_enabled(self):
        pdef = BEETS_PLUGIN_MANIFEST["fetchart"]
        configured = set()  # not in config
        loaded = set()
        status = verify_plugin(pdef, configured, loaded, self.config_dir)
        self.assertFalse(status.healthy)
        self.assertFalse(status.enabled)
        self.assertTrue(any("not enabled in config.yaml" in err for err in status.errors))

    def test_verify_all_plugins_returns_comprehensive_report(self):
        provision_bundled_plugins(self.config_dir)
        update_config_yaml_plugins(self.config_dir / "config.yaml")

        report = verify_all_plugins(self.config_dir)
        self.assertIn("all_required_healthy", report)
        self.assertIn("required_count", report)
        self.assertIn("categories", report)
        self.assertIn("required", report["categories"])
        self.assertIn("optional", report["categories"])
        self.assertIn("integration", report["categories"])
        self.assertEqual(report["required_count"], 13)

    def test_unconfigured_integration_does_not_block_health(self):
        pdef = BEETS_PLUGIN_MANIFEST["discogs"]
        configured = set()  # not enabled
        loaded = set()
        status = verify_plugin(pdef, configured, loaded, self.config_dir)
        self.assertTrue(status.healthy)
        self.assertFalse(status.enabled)
        self.assertEqual(len(status.errors), 0)

    def test_provision_and_verify_end_to_end(self):
        result = provision_and_verify(self.config_dir)
        self.assertTrue(result.get("config_updated"))
        self.assertIn("discpath.py", result.get("provisioned_files", []))
        self.assertIn("summary", result)

class BeetsPluginApiRoutesTests(unittest.TestCase):
    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
        self.client = self.flask_app.test_client()
        self.tmp_dir = tempfile.mkdtemp()
        self.config_dir = Path(self.tmp_dir)
        self.config_path = self.config_dir / "config.yaml"
        self.old_config_env = os.environ.get("BEETS_CONFIG")
        os.environ["BEETS_CONFIG"] = str(self.config_path)

    def tearDown(self):
        if self.old_config_env is not None:
            os.environ["BEETS_CONFIG"] = self.old_config_env
        else:
            os.environ.pop("BEETS_CONFIG", None)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_get_plugins_status_endpoint(self):
        res = self.client.get("/api/plugins/status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("all_required_healthy", data)
        self.assertIn("categories", data)

    def test_get_setup_plugins_alias_endpoint(self):
        res = self.client.get("/api/setup/plugins")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("all_required_healthy", data)

    def test_post_plugins_provision_endpoint(self):
        res = self.client.post("/api/plugins/provision")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("config_updated"))
        self.assertIn("discpath.py", data.get("provisioned_files", []))
        self.assertTrue(self.config_path.exists())

    def test_post_plugins_verify_endpoint(self):
        res = self.client.post("/api/plugins/verify")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("categories", data)

    def test_setup_status_includes_plugins_report(self):
        res = self.client.get("/api/setup/status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("plugins", data)
        self.assertIn("plugins_ready", data)


if __name__ == "__main__":
    unittest.main()
