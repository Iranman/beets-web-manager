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
        """The backup is never observable with a wider mode than 0600: it is
        created 0600 (os.open), not copied with the source's 0644 and then
        chmod-ed. Every chmod of a backup records the mode it had before."""
        import backend.beets_plugins as bp
        from backend.beets_plugins import ensure_web_include_paths
        path = self._make_config()
        os.chmod(path, 0o644)
        observed = []
        real_chmod = os.chmod

        def spy_chmod(target, mode, *args, **kwargs):
            if ".bak-" in os.fspath(target):
                observed.append(stat.S_IMODE(os.stat(target).st_mode))
            return real_chmod(target, mode, *args, **kwargs)

        old_umask = os.umask(0)
        try:
            with mock.patch.object(bp.os, "chmod", spy_chmod):
                backup = ensure_web_include_paths(path)["backup"]
        finally:
            os.umask(old_umask)
        observed.append(stat.S_IMODE(os.stat(self.config_dir / backup).st_mode))
        self.assertEqual(set(observed), {0o600})

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
    def test_preview_diff_masks_secret_values(self):
        """S-7: config.yaml context lines in the preview never echo secrets."""
        from backend.beets_plugins import preview_recommended_plugins
        text = (
            "acoustid:\n  apikey: SECRETVALUE1\n"
            "plugins: web webmanager\n"
            "discogs:\n  user_token: SECRETVALUE2\n  password: SECRETVALUE3\n"
        )
        path = self._make_config(text)
        diff = preview_recommended_plugins(path)["diff"]
        # n=0: unchanged lines (the leak vector) are not sent at all.
        self.assertNotIn("acoustid", diff)
        self.assertIn("fetchart", diff)
        self.assertNotIn("SECRETVALUE", diff)
        self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_changed_line_masking_covers_yaml_forms(self):
        """F2/F6: flow mappings, quoted keys, list items, block scalars,
        nested mappings under a secret key and URL userinfo are masked."""
        from backend.beets_plugins import _mask_config_diff
        lines = [
            "--- config.yaml\n", "+++ config.yaml (proposed)\n", "@@ -1,0 +1,12 @@\n",
            "+plex: {host: h, apikey: SECRET01, port: 1}\n",
            '+"api_key": SECRET02\n',
            "+  - token: SECRET03\n",
            "+lastfm_passwd: SECRET04\n",
            "+Authorization: Bearer SECRET05\n",
            "+client_secret: |\n", "+    SECRET06\n", "+\n", "+    SECRET07\n",
            "+credentials:\n", "+  user: SECRET08\n",
            "+url: http://user:SECRET09@host:32400/x\n",
            "-plugins: web keyfinder\n",
            "+directory: /music\n",
        ]
        out = _mask_config_diff(lines)
        # QA probe cases (#202), each as its own changed hunk.
        for probe in ("password: |\n    X1X\n", "apikey:\n    X1X\n", '"api_key": X1X\n',
                      "discogs: {user_token: X1X}\n", "pass: X1X\n", "musicbrainz:\n  pass: X1X\n"):
            probe_lines = ["+" + ln for ln in probe.splitlines(keepends=True)]
            self.assertNotIn("X1X", _mask_config_diff(["@@ -0,0 +1 @@\n"] + probe_lines), probe)
        for n in range(1, 10):
            self.assertNotIn(f"SECRET0{n}", out)
        self.assertIn("+url: http://user:********@host:32400/x\n", out)
        self.assertIn("-plugins: web keyfinder\n", out)
        self.assertIn("+directory: /music\n", out)
        self.assertIn("@@ -1,0 +1,12 @@\n", out)

    def test_preview_is_linear_on_pathological_lines(self):
        """F2: no polynomial regex; a 100k-char line takes well under 1 s."""
        from backend.beets_plugins import _mask_config_diff, preview_recommended_plugins
        for bad in (" " * 100_000, "key" * 33_334, "key:" * 25_000, "{," * 50_000, "://" * 33_334):
            path = self._make_config("plugins: web webmanager\n" + bad + "\n")
            start = time.perf_counter()
            preview_recommended_plugins(path)
            _mask_config_diff(["+" + bad + "\n", "-" + bad + "\n"])
            self.assertLess(time.perf_counter() - start, 1.0, bad[:8])

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


class ConfigPathContainmentTests(_TempConfigMixin, unittest.TestCase):
    """S-3 residual: every config.yaml writer gets the containment check."""

    def _make_outside(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        self.outside = Path(other.name) / "config.yaml"
        self.outside.write_text(_BASE_CONFIG, encoding="utf-8")

    def test_shared_write_path_refuses_symlink_escape(self):
        from backend.beets_plugins import update_config_yaml_plugins
        self._make_config(text=None)
        self._make_outside()
        os.symlink(self.outside, self.config_path)
        with self.assertRaises(RuntimeError):
            update_config_yaml_plugins(self.config_path)
        self.assertTrue(self.config_path.is_symlink())
        self.assertEqual(self.outside.read_text(encoding="utf-8"), _BASE_CONFIG)
        self.assertEqual(sorted(p.name for p in self.config_dir.iterdir()), ["config.yaml"])

    def test_startup_auto_provision_refuses_beets_config_outside_beetsdir(self):
        from backend.config_service import _bootstrap_beets_plugins
        self._make_config()
        if not str(self.config_path).startswith("/"):
            self.skipTest("get_config_path requires POSIX container paths")
        self._make_outside()
        with mock.patch.dict(os.environ, {"BEETS_CONFIG": str(self.outside), "BEETSDIR": str(self.config_dir)}):
            _bootstrap_beets_plugins()
        self.assertEqual(self.outside.read_text(encoding="utf-8"), _BASE_CONFIG)
        self.assertEqual(sorted(p.name for p in self.outside.parent.iterdir()), ["config.yaml"])

    def test_startup_auto_provision_edits_contained_config(self):
        from backend.beets_plugins import read_web_include_paths
        from backend.config_service import _bootstrap_beets_plugins
        self._make_config()
        if not str(self.config_path).startswith("/"):
            self.skipTest("get_config_path requires POSIX container paths")
        with mock.patch.dict(os.environ, {"BEETS_CONFIG": str(self.config_path), "BEETSDIR": str(self.config_dir)}):
            _bootstrap_beets_plugins()
        self.assertTrue(read_web_include_paths(self.config_path.read_text(encoding="utf-8")))
        self.assertTrue((self.config_dir / "beetsplug").is_dir())


class ConfigSnapshotRaceTests(_TempConfigMixin, unittest.TestCase):
    """F4: config.yaml is read once (O_NOFOLLOW); the backup and the rewrite
    both come from that snapshot, never from a file swapped in afterwards."""

    def _make_outside(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        self.outside = Path(other.name) / "victim.yaml"
        self.outside.write_text("victim: OUTSIDECONTENT\n", encoding="utf-8")

    def test_symlink_swap_after_read_never_leaks_outside_content(self):
        import backend.beets_plugins as bp
        path = self._make_config()
        self._make_outside()
        real_plan = bp._plan_config_yaml_plugins

        def plan_then_swap(*args, **kwargs):
            result = real_plan(*args, **kwargs)
            os.unlink(path)
            os.symlink(self.outside, path)
            return result

        with mock.patch.object(bp, "_plan_config_yaml_plugins", plan_then_swap):
            backup = bp.apply_recommended_plugins(path, ["fetchart"])["backup"]
        self.assertEqual((self.config_dir / backup).read_text(encoding="utf-8"), _BASE_CONFIG)
        self.assertNotIn("OUTSIDECONTENT", path.read_text(encoding="utf-8"))
        self.assertFalse(path.is_symlink())
        self.assertEqual(self.outside.read_text(encoding="utf-8"), "victim: OUTSIDECONTENT\n")

    def test_symlinked_config_is_refused_by_every_editor(self):
        from backend.beets_plugins import (
            BeetsConfigEditError, apply_recommended_plugins, ensure_web_include_paths,
            preview_recommended_plugins, update_config_yaml_plugins,
        )
        self._make_config(text=None)
        real = self.config_dir / "real.yaml"
        real.write_text(_BASE_CONFIG, encoding="utf-8")
        os.symlink(real, self.config_path)
        for call in (
            lambda: ensure_web_include_paths(self.config_path),
            lambda: apply_recommended_plugins(self.config_path, ["fetchart"]),
            lambda: preview_recommended_plugins(self.config_path),
        ):
            with self.assertRaises(BeetsConfigEditError):
                call()
        with self.assertRaises(RuntimeError):
            update_config_yaml_plugins(self.config_path)
        self.assertTrue(self.config_path.is_symlink())
        self.assertEqual(real.read_text(encoding="utf-8"), _BASE_CONFIG)


class ProvisionSymlinkTests(unittest.TestCase):
    """F5: provisioning replaces, never writes through, symlinked targets."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config_dir = Path(tmp.name) / "config"
        self.outside = Path(tmp.name) / "outside"
        self.outside.mkdir()
        self.victim = self.outside / "victim.py"
        self.victim.write_text("VICTIM\n", encoding="utf-8")

    def test_symlinked_subpackage_file_is_replaced_not_written_through(self):
        from backend.beets_plugins import provision_bundled_plugins
        dest = self.config_dir / "beetsplug" / "webmanager" / "version.py"
        dest.parent.mkdir(parents=True)
        os.symlink(self.victim, dest)
        provision_bundled_plugins(self.config_dir)
        self.assertEqual(self.victim.read_text(encoding="utf-8"), "VICTIM\n")
        self.assertFalse(dest.is_symlink())
        self.assertIn("PLUGIN_VERSION", dest.read_text(encoding="utf-8"))

    def test_symlinked_top_level_plugin_and_planted_tmp_are_not_written_through(self):
        # F5b: the top-level .py branch used a predictable tmp name and
        # write_bytes, so a planted symlink overwrote and chmodded the victim.
        from backend.beets_plugins import provision_bundled_plugins
        beetsplug = self.config_dir / "beetsplug"
        beetsplug.mkdir(parents=True)
        os.chmod(self.victim, 0o600)
        os.symlink(self.victim, beetsplug / "discpath.tmp..py")
        os.symlink(self.victim, beetsplug / "discpath.py")
        provision_bundled_plugins(self.config_dir)
        self.assertEqual(self.victim.read_text(encoding="utf-8"), "VICTIM\n")
        self.assertEqual(self.victim.stat().st_mode & 0o777, 0o600)
        self.assertFalse((beetsplug / "discpath.py").is_symlink())
        self.assertNotEqual((beetsplug / "discpath.py").read_text(encoding="utf-8"), "VICTIM\n")

    def test_symlinked_subpackage_dir_is_refused(self):
        from backend.beets_plugins import provision_bundled_plugins
        (self.config_dir / "beetsplug").mkdir(parents=True)
        os.symlink(self.outside, self.config_dir / "beetsplug" / "webmanager")
        with self.assertRaises(RuntimeError):
            provision_bundled_plugins(self.config_dir)
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["victim.py"])


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
            self.client.post("/api/setup/plugins/provision"),
            # #222 QA F2: status/verify use the same check as provisioning.
            self.client.get("/api/plugins/status"),
            self.client.post("/api/plugins/verify"),
        )
        for response in responses:
            self.assertEqual(response.status_code, 409)
            error = response.get_json()["error"]
            self.assertIn("Beets config directory", error)
            # F8: the browser-facing message never names BEETSDIR.
            self.assertNotIn(str(self.config_dir), error)
            self.assertNotIn(self.config_dir.name, error)
        self.assertEqual(self.outside.read_text(encoding="utf-8"), outside_before)
        self.assertEqual(list(self.outside.parent.glob("*.bak-*")), [])
        self.assertFalse((self.outside.parent / "beetsplug").exists())

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


class ConfigFollowupTests(_TempConfigMixin, unittest.TestCase):
    """#222 / #183 F7-F9 follow-ups."""

    def _masked(self, *changed):
        from backend.beets_plugins import _mask_config_diff
        head = ["--- config.yaml\n", "+++ config.yaml (proposed)\n", "@@ -1 +1 @@\n"]
        return _mask_config_diff(head + list(changed))

    def test_masker_bypasses_f2r(self):
        probes = {
            "pwd key": ["+pwd: X1X\n"],
            "passphrase key": ["+  passphrase: X1X\n"],
            "plain continuation": ["+apikey: abc\n", "+  X1X\n"],
            "same-indent list item": ["+apikey:\n", "+- X1X\n"],
            "explicit key pair": ["+? apikey\n", "+: X1X\n"],
            "removed --- line": ["--- {apikey: X1X}\n"],
            "added +++ line": ["+++ {token: X1X}\n"],
            "url password with @": ["+url: http://user:X1X@Y1Y@host/x\n"],
            "url password with space": ["+url: http://user:X1X Y1Y@host/x\n"],
        }
        for name, lines in probes.items():
            out = self._masked(*lines)
            self.assertNotIn("X1X", out, name)
            self.assertNotIn("Y1Y", out, name)
        self.assertIn("+url: http://user:********@host/x\n", self._masked("+url: http://user:X1X@Y1Y@host/x\n"))
        # Changed lines with a key stay readable; the two file headers are kept.
        out = self._masked("+plugins: web webmanager fetchart\n")
        self.assertIn("+plugins: web webmanager fetchart\n", out)
        self.assertTrue(out.startswith("--- config.yaml\n+++ config.yaml (proposed)\n@@"))

    def test_masker_and_editor_share_one_secret_key_list(self):
        from backend import config_layers
        from backend.config_service import _CONFIG_SECRET_KEYS, _redact_config_content
        self.assertIs(_CONFIG_SECRET_KEYS, config_layers.SECRET_CONFIG_KEYS)
        for key in ("pwd", "passphrase", "apisecret", "google_key", "lastfm_key", "fanarttv_key"):
            self.assertIn(key, _CONFIG_SECRET_KEYS)
            self.assertNotIn("X1X", _redact_config_content(f"  {key}: X1X\n"), key)
            self.assertNotIn("X1X", self._masked(f"+  {key}: X1X\n"), key)

    def test_preview_without_trailing_newline_f2n(self):
        from backend.beets_plugins import preview_recommended_plugins
        path = self._make_config("plugins: web webmanager\nmyplugin:\n  password: |\n    X1X")
        diff = preview_recommended_plugins(path)["diff"]
        self.assertIn("fetchart", diff)
        self.assertNotIn("X1X", diff)
        self.assertEqual(diff.count("\n-"), 1, diff)  # only the plugins: line

    def test_symlinked_beetsplug_dir_is_refused_before_mkdir_f5c(self):
        from backend.beets_plugins import provision_bundled_plugins
        self._make_config()
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        os.symlink(outside.name, self.config_dir / "beetsplug")
        with self.assertRaises(RuntimeError):
            provision_bundled_plugins(self.config_dir)
        self.assertEqual(os.listdir(outside.name), [])
        self.assertFalse((self.config_dir / ".webmanager_api_key").exists())

    def test_plugin_dirs_are_appended_to_sys_path_f9(self):
        import sys
        from backend.beets_plugins import ensure_plugin_sys_path
        self._make_config()
        (self.config_dir / "beetsplug").mkdir()
        before = list(sys.path)
        self.addCleanup(setattr, sys, "path", before)
        ensure_plugin_sys_path(self.config_dir)
        self.assertEqual(sys.path[: len(before)], before)
        self.assertEqual(sys.path[-1], str(self.config_dir / "beetsplug"))

    def test_provision_edits_the_beets_config_file_f7(self):
        from backend.beets_plugins import provision_and_verify, read_web_include_paths
        self._make_config(text=None)
        other = self.config_dir / "beets.yaml"
        other.write_text(_BASE_CONFIG, encoding="utf-8")
        with mock.patch("backend.beets_adapter.beets_adapter.get_plugin_status", side_effect=RuntimeError("down")):
            result = provision_and_verify(self.config_dir, config_file=other)
        self.assertTrue(result["config_updated"])
        self.assertTrue(read_web_include_paths(other.read_text(encoding="utf-8")))
        self.assertFalse(self.config_path.exists())

    def test_startup_provision_edits_the_beets_config_file_f7(self):
        from backend.beets_plugins import read_web_include_paths
        from backend.config_service import _bootstrap_beets_plugins
        self._make_config(text=None)
        if not str(self.config_dir).startswith("/"):
            self.skipTest("get_config_path requires POSIX container paths")
        other = self.config_dir / "beets.yaml"
        other.write_text(_BASE_CONFIG, encoding="utf-8")
        with mock.patch.dict(os.environ, {"BEETS_CONFIG": str(other), "BEETSDIR": str(self.config_dir)}):
            _bootstrap_beets_plugins()
        self.assertTrue(read_web_include_paths(other.read_text(encoding="utf-8")))
        self.assertFalse(self.config_path.exists())


class ConfigPathStatusTests(_TempConfigMixin, unittest.TestCase):
    """#222 QA F2: setup status and readiness flag a BEETS_CONFIG that
    provisioning refuses instead of reading it as if it were fine."""

    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
        self.client = self.flask_app.test_client()
        self._make_config()
        if not str(self.config_path).startswith("/"):
            self.skipTest("get_config_path requires POSIX container paths")
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        self.outside = Path(other.name) / "config.yaml"
        self.outside.write_text(_BASE_CONFIG, encoding="utf-8")
        (self.config_dir / "music").mkdir()
        (self.config_dir / "downloads").mkdir()

    def _get(self, url, beets_config):
        from backend.beets_adapter import beets_adapter
        env = {
            "BEETS_CONFIG": str(beets_config), "BEETSDIR": str(self.config_dir),
            "MUSIC_ROOT": str(self.config_dir / "music"), "DOWNLOADS_ROOT": str(self.config_dir / "downloads"),
        }
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(beets_adapter, "get_plugin_status", return_value=_plugin_status()), \
             mock.patch.object(beets_adapter, "get_stats", return_value={"items": 0, "albums": 0}), \
             mock.patch("backend.beets_plugins.verify_all_plugins", return_value=_plugins_report()):
            self.module._invalidate_setup_status_cache()
            return self.client.get(url)

    def test_setup_status_warns_and_blocks_on_refused_path(self):
        body = self._get("/api/setup/status", self.outside).get_json()
        warning = next(w for w in body["warnings"] if w["id"] == "beets_config_path_invalid")
        self.assertNotIn(str(self.config_dir), warning["message"])
        self.assertTrue(any("Beets config directory" in b for b in body["blocking_reasons"]), body["blocking_reasons"])

    def test_setup_status_has_no_path_warning_for_contained_config(self):
        body = self._get("/api/setup/status", self.config_path).get_json()
        self.assertNotIn("beets_config_path_invalid", [w["id"] for w in body.get("warnings", [])])

    def test_health_ready_blocks_on_refused_path(self):
        response = self._get("/health/ready", self.outside)
        self.assertEqual(response.status_code, 503)
        self.assertIn("beets config path invalid", response.get_json()["blocking_reasons"])
        response = self._get("/health/ready", self.config_path)
        self.assertNotIn("beets config path invalid", response.get_json()["blocking_reasons"])


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
