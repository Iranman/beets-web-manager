"""Plugin 1.6 diagnostics and explicit opt-in Beets config edits (BI-4/5/6/11/14).

Covers:
- web.include_paths detection and the explicit fix action (BI-6);
- the recommended-plugins preview/apply opt-in (BI-5): only the selected
  recommended plugins are added, existing settings blocks (replaygain) are
  never rewritten, comments survive, a timestamped backup is taken and the
  response says a Beets restart is required;
- the setup warnings derived from the plugin's library report (BI-11), which
  must stay silent for plugins older than 1.6.0 ("unknown" fields);
- the fpcalc state from the plugin's binary probe (BI-14);
- the typed adapter error when Beets returns items without paths.

Every edit here is to a temporary config.yaml; nothing touches a real Beets
config or library.
"""
import os
import stat
import tempfile
import time
import unittest
import unittest.mock as mock
from pathlib import Path

from tests.test_routes_setup import _load_routes_setup_against_stub_app

_BASE_CONFIG = """\
# Stock LinuxServer Beets config (user comments must survive)
directory: /music
library: /config/musiclibrary.blb
plugins: web webmanager replaygain
pluginpath:
  - /config/beetsplug
web:
  host: 0.0.0.0
  port: 8337
replaygain:
  # user's choice, must not be rewritten
  backend: gstreamer
"""


def _plugin_status(**overrides):
    status = {
        "protocol_version": "1.0",
        "plugin_version": "1.6.0",
        "beets_version": "2.14.1",
        "capabilities": ["import", "modify", "remove", "move", "operations", "status", "mbsubmit"],
        "loaded_plugins": ["musicbrainz", "chroma", "fetchart"],
        "library_ready": True,
        "upstream_web_readonly": True,
        "plugin_mutations_enabled": True,
        "library_directory": "/music",
        "library_path": "/config/musiclibrary.blb",
        "allowed_roots": ["/music", "/downloads"],
        "import_roots": ["/downloads"],
        "web_include_paths": True,
        "fpcalc_available": True,
        "ffmpeg_available": True,
    }
    status.update(overrides)
    return status


def _plugins_report():
    names = ["musicbrainz", "chroma", "fetchart"]
    plugins = [{"name": n, "enabled": True, "loaded": True, "healthy": True} for n in names]
    return {
        "ok": True, "all_required_healthy": True, "required_count": 1, "required_healthy_count": 1,
        "plugins": plugins, "categories": {"required": [], "optional": [], "integration": []},
        "summary": {"total": len(plugins), "healthy": len(plugins), "errors": []},
    }


class _TempConfigMixin:
    def _make_config(self, text=_BASE_CONFIG):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_dir = Path(self.tmp.name)
        self.config_path = self.config_dir / "config.yaml"
        if text is not None:
            self.config_path.write_text(text, encoding="utf-8")
        return self.config_path

    def _backups(self):
        return sorted(p.name for p in self.config_dir.glob("config.yaml.bak-*"))


class ReadWebIncludePathsTests(unittest.TestCase):
    def test_tristate(self):
        from backend.beets_plugins import read_web_include_paths
        self.assertIsNone(read_web_include_paths("plugins: web\n"))
        self.assertIsNone(read_web_include_paths("web:\n  host: 0.0.0.0\n"))
        self.assertTrue(read_web_include_paths("web:\n  include_paths: yes\n"))
        self.assertTrue(read_web_include_paths("web:\n  include_paths: true\n"))
        self.assertFalse(read_web_include_paths("web:\n  include_paths: no\n"))
        self.assertFalse(read_web_include_paths("web:\n  include_paths: false\n"))


class TopLevelBlockReDoSTests(unittest.TestCase):
    """S-1: _find_top_level_block must stay linear on whitespace-only lines."""

    def test_blank_indented_lines_then_crlf_is_fast(self):
        from backend.beets_plugins import _find_top_level_block
        text = "web:\n  host: 0.0.0.0\n" + "  \n" * 5000 + "\r\n  port: 1\n"
        start = time.perf_counter()
        match = _find_top_level_block(text, "web")
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0)
        # A bare "\r" line is neither indented nor a new top-level key, so the
        # old regex found no block here either -- it just took exponential time.
        self.assertIsNone(match)

    def test_block_boundaries_unchanged(self):
        from backend.beets_plugins import _find_top_level_block
        match = _find_top_level_block(_BASE_CONFIG, "web")
        self.assertEqual(match.group(1), "web:")
        self.assertEqual(match.group(2), "\n  host: 0.0.0.0\n  port: 8337")
        match = _find_top_level_block(_BASE_CONFIG, "replaygain")
        self.assertEqual(match.group(2), "\n  # user's choice, must not be rewritten\n  backend: gstreamer\n")
        text = "web: # c\n  host: x\n\n  \t\n  port: 1\nother: 2\n"
        match = _find_top_level_block(text, "web")
        self.assertEqual(match.group(1), "web: # c")
        self.assertEqual(match.group(2), "\n  host: x\n\n  \t\n  port: 1")
        self.assertIsNone(_find_top_level_block("web: {a: 1}\n", "web"))

    def test_include_paths_long_whitespace_line_is_fast(self):
        from backend.beets_plugins import _set_web_include_paths
        text = "web:\n  include_paths: a" + " " * 100_000 + "b\n"
        start = time.perf_counter()
        _, changed = _set_web_include_paths(text, overwrite_false=False)
        self.assertLess(time.perf_counter() - start, 1.0)
        self.assertFalse(changed)

    def test_include_paths_value_and_comment_parsing_unchanged(self):
        from backend.beets_plugins import _set_web_include_paths
        cases = {
            "web:\n  include_paths: yes  # keep\n": ("web:\n  include_paths: yes  # keep\n", False),
            "web:\n\tinclude_paths: no \t\n": ("web:\n\tinclude_paths: yes\n", True),
            "web:\n  include_paths: 'false'   # off\n": ("web:\n  include_paths: yes  # off\n", True),
            "web:\n  include_paths:\n  host: x\n": ("web:\n  include_paths: yes\n  host: x\n", True),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(_set_web_include_paths(text, overwrite_false=True), expected)


@unittest.skipIf(os.name == "nt", "POSIX permission bits")
class ConfigWriteModeTests(_TempConfigMixin, unittest.TestCase):
    """S-2: rewriting config.yaml keeps its mode and leaves no temp file."""

    def test_0600_config_stays_0600(self):
        from backend.beets_plugins import apply_recommended_plugins, ensure_web_include_paths
        path = self._make_config()
        os.chmod(path, 0o600)
        ensure_web_include_paths(path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        apply_recommended_plugins(path, ["fetchart"])
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        leftovers = [p.name for p in self.config_dir.iterdir() if "tmp" in p.name]
        self.assertEqual(leftovers, [])

    def test_fresh_install_config_is_0644(self):
        from backend.beets_plugins import update_config_yaml_plugins
        path = self._make_config(text=None)
        update_config_yaml_plugins(path)
        self.assertTrue(path.exists())
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)


class ConfigBackupTests(_TempConfigMixin, unittest.TestCase):
    """N5/N6/N7: backup name exposure, uniqueness, and fail-closed writes."""

    def test_backup_in_response_is_a_basename(self):
        from backend.beets_plugins import apply_recommended_plugins, ensure_web_include_paths
        path = self._make_config()
        for result in (ensure_web_include_paths(path), apply_recommended_plugins(path, ["fetchart"])):
            self.assertEqual(result["backup"], Path(result["backup"]).name)
            self.assertNotIn("/", result["backup"])
            self.assertTrue((self.config_dir / result["backup"]).is_file())

    def test_same_second_writes_get_distinct_backups(self):
        from backend.beets_plugins import apply_recommended_plugins, ensure_web_include_paths
        path = self._make_config()
        original = path.read_text(encoding="utf-8")
        with mock.patch("backend.beets_plugins._timestamped_backup_prefix",
                        return_value="config.yaml.bak-20260101-000000"):
            first = ensure_web_include_paths(path)["backup"]
            after_first = path.read_text(encoding="utf-8")
            second = apply_recommended_plugins(path, ["fetchart"])["backup"]
        self.assertNotEqual(first, second)
        self.assertEqual(len(self._backups()), 2)
        self.assertEqual((self.config_dir / first).read_text(encoding="utf-8"), original)
        self.assertEqual((self.config_dir / second).read_text(encoding="utf-8"), after_first)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_backup_is_created_0600(self):
        from backend.beets_plugins import ensure_web_include_paths
        path = self._make_config()
        backup = ensure_web_include_paths(path)["backup"]
        self.assertEqual(stat.S_IMODE(os.stat(self.config_dir / backup).st_mode), 0o600)

    def test_plugin_update_fails_closed_when_backup_fails(self):
        from backend.beets_plugins import update_config_yaml_plugins
        path = self._make_config("plugins: fetchart\n")
        with mock.patch("backend.beets_plugins._create_backup", side_effect=PermissionError("denied")):
            with self.assertRaises(RuntimeError):
                update_config_yaml_plugins(path, backup=True)
        self.assertEqual(path.read_text(encoding="utf-8"), "plugins: fetchart\n")
        self.assertEqual([p.name for p in self.config_dir.iterdir()], ["config.yaml"])


class EnsureWebIncludePathsTests(_TempConfigMixin, unittest.TestCase):
    def test_absent_key_is_added_with_backup_and_restart_flag(self):
        from backend.beets_plugins import ensure_web_include_paths, read_web_include_paths
        path = self._make_config()
        result = ensure_web_include_paths(path)
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertTrue(result["restart_required"])
        self.assertTrue(result["backup"])
        self.assertEqual(len(self._backups()), 1)
        text = path.read_text(encoding="utf-8")
        self.assertTrue(read_web_include_paths(text))
        self.assertIn("# user's choice, must not be rewritten", text)
        self.assertIn("backend: gstreamer", text)
        self.assertIn("port: 8337", text)

    def test_explicit_no_is_flipped(self):
        from backend.beets_plugins import ensure_web_include_paths, read_web_include_paths
        path = self._make_config(_BASE_CONFIG.replace("  port: 8337\n", "  port: 8337\n  include_paths: no\n"))
        result = ensure_web_include_paths(path)
        self.assertTrue(result["changed"])
        self.assertTrue(read_web_include_paths(path.read_text(encoding="utf-8")))

    def test_idempotent(self):
        from backend.beets_plugins import ensure_web_include_paths
        path = self._make_config()
        ensure_web_include_paths(path)
        before = path.read_text(encoding="utf-8")
        result = ensure_web_include_paths(path)
        self.assertFalse(result["changed"])
        self.assertFalse(result["restart_required"])
        self.assertIsNone(result["backup"])
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(len(self._backups()), 1)

    def test_flow_style_web_block_is_refused(self):
        from backend.beets_plugins import BeetsConfigEditError, ensure_web_include_paths
        text = _BASE_CONFIG.replace("web:\n  host: 0.0.0.0\n  port: 8337\n", "web: {host: 0.0.0.0, port: 8337}\n")
        path = self._make_config(text)
        with self.assertRaises(BeetsConfigEditError):
            ensure_web_include_paths(path)
        self.assertEqual(path.read_text(encoding="utf-8"), text)
        self.assertEqual(self._backups(), [])

    def test_missing_config_is_refused(self):
        from backend.beets_plugins import BeetsConfigEditError, ensure_web_include_paths
        path = self._make_config(text=None)
        with self.assertRaises(BeetsConfigEditError):
            ensure_web_include_paths(path)
        self.assertFalse(path.exists())


class RecommendedPluginsTests(_TempConfigMixin, unittest.TestCase):
    def test_preview_lists_missing_and_diff_without_writing(self):
        from backend.beets_plugins import RECOMMENDED_CONFIG_PLUGINS, preview_recommended_plugins
        path = self._make_config()
        result = preview_recommended_plugins(path)
        self.assertEqual(result["recommended"], list(RECOMMENDED_CONFIG_PLUGINS))
        self.assertIn("replaygain", result["configured"])
        self.assertNotIn("replaygain", result["missing"])
        self.assertIn("fetchart", result["missing"])
        self.assertTrue(result["would_change"])
        self.assertIn("+", result["diff"])
        self.assertIn("fetchart", result["diff"])
        self.assertEqual(path.read_text(encoding="utf-8"), _BASE_CONFIG)
        self.assertEqual(self._backups(), [])

    def test_apply_adds_only_selected_and_keeps_existing_blocks(self):
        from backend.beets_plugins import apply_recommended_plugins, parse_configured_plugins
        path = self._make_config()
        result = apply_recommended_plugins(path, ["fetchart", "embedart"])
        self.assertTrue(result["changed"])
        self.assertEqual(sorted(result["added"]), ["embedart", "fetchart"])
        self.assertTrue(result["restart_required"])
        self.assertTrue(result["backup"])
        self.assertEqual(len(self._backups()), 1)
        text = path.read_text(encoding="utf-8")
        configured = parse_configured_plugins(text)
        for name in ("web", "webmanager", "replaygain", "fetchart", "embedart"):
            self.assertIn(name, configured)
        self.assertNotIn("lastgenre", configured)
        # The user's replaygain block is untouched (no backend rewrite).
        self.assertIn("replaygain:\n  # user's choice, must not be rewritten\n  backend: gstreamer\n", text)
        self.assertIn("# Stock LinuxServer Beets config (user comments must survive)", text)

    def test_apply_already_configured_is_a_noop(self):
        from backend.beets_plugins import apply_recommended_plugins
        path = self._make_config()
        result = apply_recommended_plugins(path, ["replaygain"])
        self.assertFalse(result["changed"])
        self.assertEqual(result["added"], [])
        self.assertIsNone(result["backup"])
        self.assertFalse(result["restart_required"])
        self.assertEqual(path.read_text(encoding="utf-8"), _BASE_CONFIG)

    def test_apply_rejects_unknown_and_empty_selection(self):
        from backend.beets_plugins import apply_recommended_plugins
        path = self._make_config()
        for bad in (["notaplugin"], ["fetchart", "plexsync"], []):
            with self.assertRaises(ValueError):
                apply_recommended_plugins(path, bad)
        self.assertEqual(path.read_text(encoding="utf-8"), _BASE_CONFIG)
        self.assertEqual(self._backups(), [])


class ConfigEditRouteTests(_TempConfigMixin, unittest.TestCase):
    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
        self.client = self.flask_app.test_client()
        self._make_config()
        if not str(self.config_path).startswith("/"):
            self.skipTest("get_config_path requires POSIX container paths")
        env = mock.patch.dict(
            os.environ,
            {"BEETS_CONFIG": str(self.config_path), "BEETSDIR": str(self.config_dir)},
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)

    def _assert_all_routes_refuse(self):
        outside_before = self.outside.read_text(encoding="utf-8")
        responses = (
            self.client.post("/api/setup/beets-config/include-paths"),
            self.client.get("/api/setup/plugins/recommended"),
            self.client.post("/api/setup/plugins/recommended/apply", json={"plugins": ["fetchart"]}),
        )
        for response in responses:
            self.assertEqual(response.status_code, 500)
            self.assertIn("Beets config directory", response.get_json()["error"])
        self.assertEqual(self.outside.read_text(encoding="utf-8"), outside_before)
        self.assertEqual(list(self.outside.parent.glob("*.bak-*")), [])

    def _make_outside(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        self.outside = Path(other.name) / "config.yaml"
        self.outside.write_text(_BASE_CONFIG, encoding="utf-8")

    def test_routes_refuse_beets_config_outside_config_dir(self):
        """S-3: BEETS_CONFIG pointing outside BEETSDIR is refused, not edited."""
        self._make_outside()
        with mock.patch.dict(os.environ, {"BEETS_CONFIG": str(self.outside)}):
            self._assert_all_routes_refuse()

    def test_routes_refuse_symlink_escaping_config_dir(self):
        """S-3: a config.yaml symlink inside BEETSDIR resolving elsewhere is refused."""
        self._make_outside()
        link = self.config_dir / "linked.yaml"
        os.symlink(self.outside, link)
        with mock.patch.dict(os.environ, {"BEETS_CONFIG": str(link)}):
            self._assert_all_routes_refuse()

    def test_include_paths_route_enables_and_reports_restart(self):
        from backend.beets_plugins import read_web_include_paths
        response = self.client.post("/api/setup/beets-config/include-paths")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["changed"])
        self.assertTrue(body["restart_required"])
        self.assertTrue(read_web_include_paths(self.config_path.read_text(encoding="utf-8")))

    def test_include_paths_route_flow_style_is_409(self):
        text = _BASE_CONFIG.replace("web:\n  host: 0.0.0.0\n  port: 8337\n", "web: {host: 0.0.0.0}\n")
        self.config_path.write_text(text, encoding="utf-8")
        response = self.client.post("/api/setup/beets-config/include-paths")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), text)

    def test_preview_route(self):
        response = self.client.get("/api/setup/plugins/recommended")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertIn("fetchart", body["missing"])
        self.assertTrue(body["diff"])
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), _BASE_CONFIG)

    def test_apply_route_success(self):
        response = self.client.post("/api/setup/plugins/recommended/apply", json={"plugins": ["fetchart"]})
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["added"], ["fetchart"])
        self.assertTrue(body["restart_required"])
        self.assertEqual(len(self._backups()), 1)

    def test_apply_route_bad_requests_are_400_and_write_nothing(self):
        for payload in ({"plugins": ["notaplugin"]}, {"plugins": []}, {"plugins": "fetchart"}, {"x": 1}, [1]):
            response = self.client.post("/api/setup/plugins/recommended/apply", json=payload)
            self.assertEqual(response.status_code, 400, payload)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), _BASE_CONFIG)
        self.assertEqual(self._backups(), [])

    def test_apply_route_missing_config_is_409(self):
        self.config_path.unlink()
        response = self.client.post("/api/setup/plugins/recommended/apply", json={"plugins": ["fetchart"]})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.config_path.exists())

    def test_csrf_failure_blocks_both_writes(self):
        forbidden = self.flask_app.response_class('{"ok": false}', status=403, mimetype="application/json")
        with mock.patch.object(self.module, "_setup_csrf_failure", return_value=forbidden):
            r1 = self.client.post("/api/setup/beets-config/include-paths")
            r2 = self.client.post("/api/setup/plugins/recommended/apply", json={"plugins": ["fetchart"]})
        self.assertEqual(r1.status_code, 403)
        self.assertEqual(r2.status_code, 403)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), _BASE_CONFIG)
        self.assertEqual(self._backups(), [])


class SetupWarningTests(unittest.TestCase):
    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)

    def _diag(self, library=None, compat=None):
        return {
            "beets_library": library if library is not None else {},
            "engine_compatibility": compat or {},
        }

    def _ids(self, warnings):
        return [w["id"] for w in warnings]

    def test_library_report_is_unknown_for_old_plugins(self):
        report = self.module._plugin_library_report({"protocol_version": "1.0", "plugin_version": "1.5.0"})
        for key in ("library_directory", "library_path", "allowed_roots", "import_roots", "web_include_paths"):
            self.assertEqual(report[key], "unknown", key)
        warnings, actions = self.module._beets_setup_warnings(self._diag(report), "/data/music", "/elsewhere")
        self.assertEqual(warnings, [])
        self.assertEqual(actions, [])

    def test_library_report_passes_through_new_fields(self):
        report = self.module._plugin_library_report(_plugin_status())
        self.assertEqual(report["library_directory"], "/music")
        self.assertEqual(report["import_roots"], ["/downloads"])
        self.assertIs(report["web_include_paths"], True)

    def test_consistent_setup_has_no_warnings(self):
        report = self.module._plugin_library_report(_plugin_status())
        warnings, actions = self.module._beets_setup_warnings(self._diag(report), "/music/", "/downloads/incoming")
        self.assertEqual(warnings, [])
        self.assertEqual(actions, [])

    def test_include_paths_disabled_warns_with_fix_action(self):
        report = self.module._plugin_library_report(_plugin_status(web_include_paths=False))
        warnings, actions = self.module._beets_setup_warnings(self._diag(report), "/music", "/downloads")
        self.assertEqual(self._ids(warnings), ["beets_web_include_paths_disabled"])
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["id"], "enable_web_include_paths")
        self.assertEqual(actions[0]["method"], "POST")
        self.assertEqual(actions[0]["endpoint"], "/api/setup/beets-config/include-paths")
        self.assertTrue(actions[0]["restart_required"])

    def test_root_mismatches_warn(self):
        report = self.module._plugin_library_report(_plugin_status(library_directory="/data/media/music"))
        warnings, _ = self.module._beets_setup_warnings(self._diag(report), "/music", "/data/downloads")
        self.assertEqual(self._ids(warnings), ["music_root_mismatch", "downloads_root_not_import_root"])
        for w in warnings:
            self.assertEqual(w["severity"], "warning")

    def test_downloads_prefix_is_not_a_false_match(self):
        report = self.module._plugin_library_report(_plugin_status(import_roots=["/downloads"]))
        warnings, _ = self.module._beets_setup_warnings(self._diag(report), "/music", "/downloads2")
        self.assertEqual(self._ids(warnings), ["downloads_root_not_import_root"])

    def test_restart_required_warning(self):
        warnings, _ = self.module._beets_setup_warnings(
            self._diag({}, {"restart_required": True, "message": "restart beets"}), "/music", "/downloads"
        )
        self.assertEqual(self._ids(warnings), ["beets_restart_required"])
        self.assertEqual(warnings[0]["message"], "restart beets")


class SetupStatusIntegrationTests(unittest.TestCase):
    """End-to-end /api/setup/status with the plugin handshake mocked at the adapter."""

    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
        self.client = self.flask_app.test_client()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "music").mkdir()
        (self.root / "downloads").mkdir()

    def _status(self, plugin_status):
        from backend.beets_adapter import beets_adapter
        env = {"MUSIC_ROOT": str(self.root / "music"), "DOWNLOADS_ROOT": str(self.root / "downloads")}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(beets_adapter, "get_plugin_status", return_value=plugin_status), \
             mock.patch.object(beets_adapter, "get_stats", return_value={"items": 0, "albums": 0}), \
             mock.patch("backend.beets_plugins.verify_all_plugins", return_value=_plugins_report()):
            self.module._invalidate_setup_status_cache()
            response = self.client.get("/api/setup/status")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_fpcalc_missing_when_plugin_probe_says_so(self):
        body = self._status(_plugin_status(fpcalc_available=False))
        self.assertEqual(body["fpcalc"]["state"], "missing")
        self.assertFalse(body["fpcalc"]["available"])

    def test_fpcalc_available_when_probe_and_chroma_agree(self):
        body = self._status(_plugin_status())
        self.assertEqual(body["fpcalc"]["state"], "available")

    def test_old_plugin_without_probe_uses_capability_inference(self):
        status = _plugin_status(plugin_version="1.5.0")
        for key in ("library_directory", "library_path", "allowed_roots", "import_roots",
                    "web_include_paths", "fpcalc_available", "ffmpeg_available"):
            status.pop(key)
        body = self._status(status)
        self.assertEqual(body["fpcalc"]["state"], "available")
        ids = [w["id"] for w in body.get("warnings", [])]
        self.assertNotIn("beets_web_include_paths_disabled", ids)
        self.assertNotIn("music_root_mismatch", ids)

    def test_include_paths_disabled_surfaces_in_status(self):
        body = self._status(_plugin_status(web_include_paths=False))
        ids = [w["id"] for w in body["warnings"]]
        self.assertIn("beets_web_include_paths_disabled", ids)
        self.assertIn("enable_web_include_paths", [a["id"] for a in body["actions"]])


class AdapterPathsUnavailableTests(unittest.TestCase):
    def test_items_without_paths_raise_typed_error(self):
        from backend.beets_adapter import BeetsAdapter, BeetsAdapterPathsUnavailableError, BeetsPathsUnavailableError
        self.assertIs(BeetsPathsUnavailableError, BeetsAdapterPathsUnavailableError)
        with self.assertRaises(BeetsAdapterPathsUnavailableError) as ctx:
            BeetsAdapter._require_item_paths([{"id": 1}, {"id": 2, "path": ""}])
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.error_code, "BEETS_PATHS_UNAVAILABLE")

    def test_empty_library_and_items_with_paths_do_not_raise(self):
        from backend.beets_adapter import BeetsAdapter
        BeetsAdapter._require_item_paths([])
        BeetsAdapter._require_item_paths([{"id": 1, "path": "/music/a.flac"}, {"id": 2}])

    def test_list_distinct_item_paths_raises_instead_of_empty_list(self):
        from backend.beets_adapter import BeetsAdapter, BeetsAdapterPathsUnavailableError
        adapter = BeetsAdapter.__new__(BeetsAdapter)
        with mock.patch.object(BeetsAdapter, "get_items", return_value=[{"id": 1}], create=True):
            with self.assertRaises(BeetsAdapterPathsUnavailableError):
                adapter.list_distinct_item_paths()


if __name__ == "__main__":
    unittest.main()
