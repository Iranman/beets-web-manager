"""Tests for _repair_legacy_beets_config() -- the startup safety net for
installs whose /config/config.yaml predates the Issue #14 packaging fix
(https://github.com/Iranman/beets-web-manager/issues/14). setup.sh/setup.ps1
only copy config.yaml.example into place when config.yaml does not already
exist, so an install set up before that fix shipped stays stuck on the old
plexsync default across every later image update -- a raw `beet` CLI
invocation reads this file directly. Since plugin 1.6.0 (BI-5) the repair
only drops the never-installed `plexsync` token: it no longer injects
/app/beetsplug into pluginpath or rewrites the replaygain backend, because
this config is read by the stock LinuxServer Beets container.
These tests import the real app.py (same isolated-temp-environment pattern
as tests/test_ai_batch_retry_race.py) and call the actual function against
synthetic config.yaml fixtures on disk.
"""
import atexit
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="beets_legacy_config_migration_"))
# Deliberately atexit, not unittest.addModuleCleanup -- see the full
# explanation in tests/test_post_retag_artwork_integration.py and the real
# failure this caused in tests/test_ai_batch_retry_race.py (Wave 26 review):
# addModuleCleanup drains process-wide, not per-module, so another module
# finishing first can delete this module's own tmp root mid-run.
atexit.register(shutil.rmtree, str(_TMP_ROOT), ignore_errors=True)

_ENV_OVERRIDES = {
    "BEETSDIR": str(_TMP_ROOT / "config"),
    "LIB_PATH": str(_TMP_ROOT / "config" / "musiclibrary.blb"),
    "AI_BATCH_STATE_DIR": str(_TMP_ROOT / "ai_batch_jobs"),
    "METADATA_CACHE_DIR": str(_TMP_ROOT / "cache"),
    "BEETS_TRANSACTION_DIR": str(_TMP_ROOT / "transactions"),
    "BEETS_WEB_AUTH_DISABLED": "1",
}
(_TMP_ROOT / "config").mkdir(parents=True, exist_ok=True)
_env_patcher = mock.patch.dict(os.environ, _ENV_OVERRIDES, clear=False)
_env_patcher.start()
atexit.register(_env_patcher.stop)


def setUpModule():
    os.environ.update(_ENV_OVERRIDES)


def _import_app():
    sys.path.insert(0, str(ROOT))
    import app as app_module
    return app_module


try:
    APP = _import_app()
    _APP_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment-dependent
    APP = None
    _APP_IMPORT_ERROR = exc


OLD_BROKEN_CONFIG = """plugins: fetchart embedart convert scrub replaygain lastgenre chroma lyrics mbsync musicbrainz deezer listenbrainz ftintitle fromfilename duplicates missing smartplaylist mbsubmit unimported discpath plexsync
pluginpath: /config/beetsplug
directory: /data/media/music
library: /config/musiclibrary.blb

replaygain:
    auto: no

scrub:
    auto: yes
"""

ALREADY_CURRENT_CONFIG = """plugins: fetchart embedart replaygain discpath
pluginpath:
  - /config/beetsplug
  - /app/beetsplug
directory: /data/media/music

replaygain:
    auto: no
    backend: ffmpeg
"""


@unittest.skipIf(APP is None, f"app.py could not be imported: {_APP_IMPORT_ERROR}")
class LegacyBeetsConfigMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="legacy_config_case_"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.config_path = self.tmp / "config.yaml"

    def _write(self, text: str) -> None:
        self.config_path.write_text(text, encoding="utf-8")

    def _repair(self, mp3gain_present=False, ffmpeg_present=True):
        def fake_which(name):
            if name == "mp3gain":
                return "/usr/bin/mp3gain" if mp3gain_present else None
            if name == "ffmpeg":
                return "/usr/bin/ffmpeg" if ffmpeg_present else None
            return None
        with mock.patch.object(APP.shutil, "which", side_effect=fake_which):
            APP._repair_legacy_beets_config(str(self.config_path))

    def test_drops_plexsync_from_plugins_line(self):
        self._write(OLD_BROKEN_CONFIG)
        self._repair()
        result = self.config_path.read_text(encoding="utf-8")
        first_line = result.splitlines()[0]
        self.assertNotIn("plexsync", first_line)
        self.assertIn("discpath", first_line)
        self.assertIn("fetchart", first_line)

    def test_leaves_single_string_pluginpath_unchanged(self):
        # BI-5 (plugin 1.6.0): /app/beetsplug does not exist in the stock
        # LinuxServer Beets container, so it is never injected any more.
        self._write(OLD_BROKEN_CONFIG)
        self._repair()
        result = self.config_path.read_text(encoding="utf-8")
        self.assertIn("pluginpath: /config/beetsplug\n", result)
        self.assertNotIn("/app/beetsplug", result)

    def test_does_not_insert_missing_pluginpath(self):
        text = "plugins: fetchart discpath plexsync\ndirectory: /data/media/music\n"
        self._write(text)
        self._repair()
        result = self.config_path.read_text(encoding="utf-8")
        self.assertNotIn("pluginpath", result)
        self.assertNotIn("plexsync", result.splitlines()[0])

    def test_does_not_append_app_beetsplug_to_existing_pluginpath_list(self):
        text = "plugins: fetchart discpath plexsync\npluginpath:\n  - /config/beetsplug\n  - /config/custom-plugins\ndirectory: /x\n"
        self._write(text)
        self._repair()
        result = self.config_path.read_text(encoding="utf-8")
        self.assertIn("pluginpath:\n  - /config/beetsplug\n  - /config/custom-plugins\ndirectory: /x\n", result)
        self.assertNotIn("/app/beetsplug", result)

    def test_does_not_rewrite_mp3gain_backend_even_when_unavailable_here(self):
        # The available replaygain backend is a property of the Beets
        # container, not of the Web Manager image this code runs in.
        text = "plugins: fetchart plexsync\nreplaygain:\n    auto: no\n    backend: mp3gain\n"
        self._write(text)
        self._repair(mp3gain_present=False, ffmpeg_present=True)
        result = self.config_path.read_text(encoding="utf-8")
        self.assertIn("backend: mp3gain", result)
        self.assertNotIn("backend: ffmpeg", result)

    def test_does_not_insert_backend_line_when_replaygain_section_has_none(self):
        self._write(OLD_BROKEN_CONFIG)
        self._repair()
        result = self.config_path.read_text(encoding="utf-8")
        self.assertIn("replaygain:\n    auto: no\n\nscrub:", result)
        self.assertNotIn("backend:", result)

    def test_config_without_plexsync_is_never_rewritten(self):
        text = "plugins: fetchart\npluginpath: /config/beetsplug\nreplaygain:\n    auto: no\n    backend: mp3gain\n"
        self._write(text)
        self._repair(mp3gain_present=False, ffmpeg_present=True)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), text)
        self.assertFalse((self.tmp / "config.yaml.bak-legacy-plugin-migration").exists())

    def test_does_not_touch_working_mp3gain_setup(self):
        text = "plugins: fetchart\nreplaygain:\n    auto: no\n    backend: mp3gain\n"
        self._write(text)
        self._repair(mp3gain_present=True, ffmpeg_present=True)
        result = self.config_path.read_text(encoding="utf-8")
        self.assertIn("backend: mp3gain", result)

    def test_already_current_config_is_left_untouched(self):
        self._write(ALREADY_CURRENT_CONFIG)
        self._repair()
        result = self.config_path.read_text(encoding="utf-8")
        self.assertEqual(result, ALREADY_CURRENT_CONFIG)
        backup = self.tmp / "config.yaml.bak-legacy-plugin-migration"
        self.assertFalse(backup.exists())

    def test_creates_backup_before_first_repair(self):
        self._write(OLD_BROKEN_CONFIG)
        self._repair()
        backup = self.tmp / "config.yaml.bak-legacy-plugin-migration"
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_text(encoding="utf-8"), OLD_BROKEN_CONFIG)

    def test_idempotent_second_run_is_a_no_op(self):
        self._write(OLD_BROKEN_CONFIG)
        self._repair()
        once = self.config_path.read_text(encoding="utf-8")
        backup = self.tmp / "config.yaml.bak-legacy-plugin-migration"
        backup_mtime = backup.stat().st_mtime
        self._repair()
        twice = self.config_path.read_text(encoding="utf-8")
        self.assertEqual(once, twice)
        self.assertEqual(backup.stat().st_mtime, backup_mtime)

    def test_symlinked_config_is_not_written_through(self):
        """F1: a config.yaml symlinked elsewhere is never rewritten."""
        victim = self.tmp / "outside" / "victim.yaml"
        victim.parent.mkdir()
        victim.write_text(OLD_BROKEN_CONFIG, encoding="utf-8")
        os.symlink(victim, self.config_path)
        self._repair()
        self.assertEqual(victim.read_text(encoding="utf-8"), OLD_BROKEN_CONFIG)
        self.assertTrue(self.config_path.is_symlink())
        self.assertEqual(sorted(p.name for p in victim.parent.iterdir()), ["victim.yaml"])

    def test_beets_config_outside_beetsdir_is_not_repaired(self):
        """F1: with no explicit path, BEETS_CONFIG must sit inside BEETSDIR."""
        other = self.tmp / "outside"
        other.mkdir()
        target = other / "other.yaml"
        target.write_text(OLD_BROKEN_CONFIG, encoding="utf-8")
        beetsdir = self.tmp / "beetsdir"
        beetsdir.mkdir()
        with mock.patch.dict(os.environ, {"BEETS_CONFIG": str(target), "BEETSDIR": str(beetsdir)}):
            APP._repair_legacy_beets_config()
        self.assertEqual(target.read_text(encoding="utf-8"), OLD_BROKEN_CONFIG)
        self.assertEqual(sorted(p.name for p in other.iterdir()), ["other.yaml"])

    def test_missing_config_file_does_not_raise(self):
        # No config.yaml written at all -- must not crash startup.
        APP._repair_legacy_beets_config(str(self.tmp / "does-not-exist.yaml"))

    def test_full_reporter_scenario_only_drops_plexsync(self):
        self._write(OLD_BROKEN_CONFIG)
        self._repair(mp3gain_present=False, ffmpeg_present=True)
        result = self.config_path.read_text(encoding="utf-8")
        first_line = result.splitlines()[0]
        self.assertNotIn("plexsync", first_line)
        # Everything after the plugins: line is unchanged (marker appended).
        old_rest = OLD_BROKEN_CONFIG.split("\n", 1)[1]
        self.assertIn(old_rest, result)
        self.assertNotIn("/app/beetsplug", result)
        self.assertNotIn("backend:", result)


if __name__ == "__main__":
    unittest.main()
