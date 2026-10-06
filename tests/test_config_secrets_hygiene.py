"""Secret-file hygiene and config follow-ups (#183 S-4/S-8, #185 N1/N2/N4, #186 N2).

Files that can hold secrets are created at mode 0600 (never at umask mode
then chmod'ed), a failed chmod is logged, API responses and status text
carry no BEETS_WEB_URL userinfo or absolute backup paths, and a bad
BEETS_OUTBOUND_TOTAL_TIMEOUT_SECONDS no longer aborts startup.
"""

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import config_layers as cl  # noqa: E402

try:
    from test_routes_setup import _load_routes_setup_against_stub_app  # noqa: E402
except ImportError:  # pragma: no cover
    from tests.test_routes_setup import _load_routes_setup_against_stub_app  # noqa: E402

POSIX = os.name == "posix"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _failing_chmod(*_args, **_kwargs):
    raise PermissionError("chmod not permitted")


class _PrivateFileCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        if POSIX:
            old = os.umask(0o022)
            self.addCleanup(os.umask, old)


@unittest.skipUnless(POSIX, "file modes are POSIX-only")
class EnvMigrationModeTests(_PrivateFileCase):
    def test_backup_env_and_report_are_0600_even_when_chmod_fails(self):
        env_file = self.root / ".env"
        env_file.write_text("PUID=1000\nOPENAI_API_KEY=sk-not-real\n", encoding="utf-8")
        with mock.patch("os.chmod", _failing_chmod), \
                self.assertLogs("beets.config_layers", "WARNING") as logs:
            report = cl.migrate_saved_env_file(env_file)
        self.assertEqual(report["removed"], ["PUID"])
        backup = self.root / report["backup"]
        self.assertEqual(report["backup"], backup.name)
        for path in (backup, env_file, cl.migration_report_path(env_file)):
            self.assertEqual(_mode(path), 0o600, path.name)
        self.assertTrue(any("Could not set mode 0600" in line for line in logs.output))

    def test_create_private_file_never_reuses_an_existing_path(self):
        target = self.root / "x.bak"
        target.write_text("old", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            cl.create_private_file(target, "new")
        other = cl.create_private_file(target, "new", unique=True)
        self.assertEqual(other.name, "x.bak-1")
        self.assertEqual(target.read_text(encoding="utf-8"), "old")


class SetupEnvWriteTests(_PrivateFileCase):
    def setUp(self):
        super().setUp()
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
        self.env_file = self.root / ".env"
        self.module._SETUP_ENV_FILE = self.env_file
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))

    def test_backup_is_0600_and_only_its_name_is_returned(self):
        self.env_file.write_text("PLEX_TOKEN=oldsecretvalue\n", encoding="utf-8")
        with mock.patch("os.chmod", _failing_chmod):
            backup_path = self.module._write_env_file({"PLEX_URL": "http://plex:32400"}, [])
        self.assertTrue(backup_path.startswith(".env.bak-"))
        self.assertNotIn(os.sep, backup_path)
        self.assertNotIn("/", backup_path)
        backup = self.root / backup_path
        self.assertEqual(backup.read_text(encoding="utf-8"), "PLEX_TOKEN=oldsecretvalue\n")
        if POSIX:
            self.assertEqual(_mode(backup), 0o600)
            self.assertEqual(_mode(self.env_file), 0o600)


class SetupStatusRedactionTests(unittest.TestCase):
    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
        self.client = self.flask_app.test_client()
        saved = os.environ.get("BEETS_WEB_URL")
        os.environ["BEETS_WEB_URL"] = "http://alice:hunter2pw@beets:8337"
        self.addCleanup(lambda: os.environ.pop("BEETS_WEB_URL", None) if saved is None
                        else os.environ.__setitem__("BEETS_WEB_URL", saved))

    def test_status_does_not_echo_beets_web_url_userinfo(self):
        from backend.beets_adapter import beets_adapter
        with mock.patch.object(beets_adapter, "get_plugin_status", side_effect=RuntimeError("down")):
            response = self.client.get("/api/setup/status")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("http://beets:8337", text)
        self.assertNotIn("hunter2pw", text)
        self.assertNotIn("alice", text)

    def test_redact_url_userinfo(self):
        self.assertEqual(cl.redact_url_userinfo("https://u:p@h:1/x?token=t#f"), "https://h:1/x")
        self.assertEqual(cl.redact_url_userinfo("http://[::1]:8337"), "http://[::1]:8337")


class ContainerPathLogTests(unittest.TestCase):
    def test_relative_value_is_not_logged(self):
        cl._warned_aliases.clear()
        self.addCleanup(cl._warned_aliases.clear)
        with self.assertLogs("beets.config_layers", "WARNING") as logs:
            self.assertEqual(cl.container_path("MUSIC_ROOT", "/music", environ={"MUSIC_ROOT": "./private-share"}), "/music")
        self.assertNotIn("private-share", "\n".join(logs.output))
        self.assertIn("MUSIC_ROOT", "\n".join(logs.output))


@unittest.skipUnless(POSIX, "file modes are POSIX-only")
class LegacyConfigBackupModeTests(_PrivateFileCase):
    def test_legacy_plugin_migration_backup_is_0600(self):
        from backend import config_service
        cfg = self.root / "config.yaml"
        cfg.write_text("plugins: plexsync fetchart\nplex:\n  token: not-real\n", encoding="utf-8")
        os.chmod(cfg, 0o644)
        config_service._repair_legacy_beets_config(str(cfg))
        backup = self.root / "config.yaml.bak-legacy-plugin-migration"
        self.assertIn("plexsync", backup.read_text(encoding="utf-8"))
        self.assertEqual(_mode(backup), 0o600)
        self.assertNotIn("plexsync", cfg.read_text(encoding="utf-8"))


class RuntimeDeadKeyTests(unittest.TestCase):
    def test_app_runtime_does_not_read_dead_keys(self):
        source = (ROOT / "backend" / "app_runtime.py").read_text(encoding="utf-8")
        for dead in ("PLAYLIST_DIR", "BEETS_SQLITE_TIMEOUT"):
            self.assertNotIn(dead, source)


class OutboundTotalTimeoutTests(unittest.TestCase):
    def _timeout_for(self, raw):
        env = dict(os.environ, BEETS_OUTBOUND_TOTAL_TIMEOUT_SECONDS=raw)
        return subprocess.run(
            [sys.executable, "-c", "from backend import security as s; print(s._DEFAULT_TOTAL_TIMEOUT)"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
        )

    def test_non_numeric_value_falls_back_to_60_with_warning(self):
        for raw in ("abc", "-5", "nan"):
            proc = self._timeout_for(raw)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip().splitlines()[-1], "60.0")
            self.assertIn("BEETS_OUTBOUND_TOTAL_TIMEOUT_SECONDS", proc.stderr)

    def test_valid_value_is_used(self):
        proc = self._timeout_for("15")
        self.assertEqual(proc.stdout.strip().splitlines()[-1], "15.0")


if __name__ == "__main__":
    unittest.main()
