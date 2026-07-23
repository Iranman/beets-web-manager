"""Tests for _repair_legacy_beets_config() -- the startup safety net for
installs whose /config/config.yaml predates the Issue #14 packaging fix
(https://github.com/Iranman/beets-web-manager/issues/14). setup.sh/setup.ps1
only copy config.yaml.example into place when config.yaml does not already
exist, so an install set up before that fix shipped stays stuck on the old
plexsync/pluginpath/mp3gain defaults across every later image update -- a
raw `beet` CLI invocation inside the container reads this file directly.
These tests import the real app.py (same isolated-temp-environment pattern
as tests/test_ai_batch_retry_race.py) and call the actual function against
synthetic config.yaml fixtures on disk.
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="beets_legacy_config_migration_"))
unittest.addModuleCleanup(shutil.rmtree, str(_TMP_ROOT), ignore_errors=True)

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
unittest.addModuleCleanup(_env_patcher.stop)


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

    def test_upgrades_single_string_pluginpath_to_list_with_app_beetsplug(self):
        self._write(OLD_BROKEN_CONFIG)
        self._repair()
        result = self.config_path.read_text(encoding="utf-8")
        self.assertIn("pluginpath:\n  - /config/beetsplug\n  - /app/beetsplug\n", result)

    def test_inserts_missing_pluginpath_after_plugins_line(self):
        text = "plugins: fetchart discpath\ndirectory: /data/media/music\n"
        self._write(text)
        self._repair()
        result = self.config_path.read_text(encoding="utf-8")
        self.assertIn("pluginpath:\n  - /config/beetsplug\n  - /app/beetsplug\n", result)
        self.assertLess(result.index("pluginpath:"), result.index("directory:"))

    def test_appends_missing_app_beetsplug_to_existing_pluginpath_list(self):
        text = "plugins: fetchart discpath\npluginpath:\n  - /config/beetsplug\n  - /config/custom-plugins\ndirectory: /x\n"
        self._write(text)
        self._repair()
        result = self.config_path.read_text(encoding="utf-8")
        self.assertIn("/config/custom-plugins", result)
        self.assertIn("/app/beetsplug", result)

    def test_switches_mp3gain_backend_to_ffmpeg_when_mp3gain_unavailable(self):
        self._write(OLD_BROKEN_CONFIG)
        self._repair(mp3gain_present=False, ffmpeg_present=True)
        result = self.config_path.read_text(encoding="utf-8")
        self.assertIn("backend: ffmpeg", result)

    def test_inserts_backend_line_when_replaygain_section_has_none(self):
        self._write(OLD_BROKEN_CONFIG)
        self._repair()
        result = self.config_path.read_text(encoding="utf-8")
        self.assertIn("replaygain:\n    auto: no\n    backend: ffmpeg\n", result)

    def test_replaces_explicit_mp3gain_backend_value(self):
        text = "plugins: fetchart\nreplaygain:\n    auto: no\n    backend: mp3gain\n"
        self._write(text)
        self._repair(mp3gain_present=False, ffmpeg_present=True)
        result = self.config_path.read_text(encoding="utf-8")
        self.assertIn("backend: ffmpeg", result)
        self.assertNotIn("backend: mp3gain", result)

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

    def test_missing_config_file_does_not_raise(self):
        # No config.yaml written at all -- must not crash startup.
        APP._repair_legacy_beets_config(str(self.tmp / "does-not-exist.yaml"))

    def test_full_reporter_scenario_ends_up_fully_repaired(self):
        self._write(OLD_BROKEN_CONFIG)
        self._repair(mp3gain_present=False, ffmpeg_present=True)
        result = self.config_path.read_text(encoding="utf-8")
        first_line = result.splitlines()[0]
        self.assertNotIn("plexsync", first_line)
        self.assertIn("pluginpath:\n  - /config/beetsplug\n  - /app/beetsplug\n", result)
        self.assertIn("backend: ffmpeg", result)
        self.assertNotIn("backend: mp3gain", result)


if __name__ == "__main__":
    unittest.main()
