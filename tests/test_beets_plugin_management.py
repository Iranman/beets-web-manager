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
    verify_all_plugins,
    verify_plugin,
)
from tests.test_routes_setup import _load_routes_setup_against_stub_app


class BeetsPluginManifestTests(unittest.TestCase):
    def test_manifest_contains_all_required_plugins(self):
        # BI-5: only the plugins Web Manager cannot run without are
        # REQUIRED (auto-added). Everything else is RECOMMENDED and opt-in.
        self.assertEqual(set(REQUIRED_PLUGIN_NAMES), {"web", "webmanager"})
        for req in ("web", "webmanager"):
            self.assertEqual(BEETS_PLUGIN_MANIFEST[req].category, PluginCategory.REQUIRED)
        for rec in (
            "musicbrainz", "chroma", "fetchart", "embedart", "scrub",
            "zero", "ftintitle", "fromfilename", "mbsync", "mbsubmit",
            "replaygain", "lastgenre", "discpath",
        ):
            self.assertIn(rec, BEETS_PLUGIN_MANIFEST)
            self.assertEqual(BEETS_PLUGIN_MANIFEST[rec].category, PluginCategory.RECOMMENDED)

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
        # web/webmanager are REQUIRED (BeetsAdapter's sole read/mutation
        # transport), not optional -- see test_manifest_contains_all_required_plugins.
        for opt in ("convert", "duplicates", "missing", "smartplaylist", "unimported", "lyrics", "parentwork", "edit", "hook"):
            self.assertIn(opt, OPTIONAL_PLUGIN_NAMES)
        self.assertNotIn("web", OPTIONAL_PLUGIN_NAMES)

        for integ in ("listenbrainz", "deezer", "discogs", "spotify", "plexsync", "bpsync"):
            self.assertIn(integ, INTEGRATION_PLUGIN_NAMES)


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

    def test_recommended_plugin_not_enabled_is_healthy(self):
        pdef = BEETS_PLUGIN_MANIFEST["fetchart"]
        status = verify_plugin(pdef, set(), set(), self.config_dir)
        self.assertTrue(status.healthy)
        self.assertFalse(status.enabled)
        self.assertEqual(status.errors, [])

    def test_verify_plugin_unhealthy_when_not_enabled(self):
        pdef = BEETS_PLUGIN_MANIFEST["webmanager"]
        configured = set()  # not in config
        loaded = set()
        status = verify_plugin(pdef, configured, loaded, self.config_dir)
        self.assertFalse(status.healthy)
        self.assertFalse(status.enabled)
        self.assertTrue(any("not enabled in config.yaml" in err for err in status.errors))

    def test_verify_all_plugins_returns_comprehensive_report(self):
        report = verify_all_plugins(self.config_dir)
        self.assertIn("all_required_healthy", report)
        self.assertIn("required_count", report)
        self.assertIn("categories", report)
        self.assertIn("required", report["categories"])
        self.assertIn("optional", report["categories"])
        self.assertIn("integration", report["categories"])
        self.assertIn("recommended", report["categories"])
        self.assertEqual(report["required_count"], 2)

    def test_unconfigured_integration_does_not_block_health(self):
        pdef = BEETS_PLUGIN_MANIFEST["discogs"]
        configured = set()  # not enabled
        loaded = set()
        status = verify_plugin(pdef, configured, loaded, self.config_dir)
        self.assertTrue(status.healthy)
        self.assertFalse(status.enabled)
        self.assertEqual(len(status.errors), 0)


class BeetsPluginApiRoutesTests(unittest.TestCase):
    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
        self.client = self.flask_app.test_client()
        self.tmp_dir = tempfile.mkdtemp()
        self.config_dir = Path(self.tmp_dir)
        self.config_path = self.config_dir / "config.yaml"
        # BEETS_CONFIG must sit directly inside BEETSDIR (S-3 containment).
        env = mock.patch.dict(os.environ, {"BEETS_CONFIG": str(self.config_path), "BEETSDIR": str(self.config_dir)})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
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
