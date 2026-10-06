"""QA regression tests for PR #205 (#183 S-4/S-8, #185 N2/N4, #186 N2).

Covers what tests/test_config_secrets_hygiene.py does not: symlinks at the
target, umask 0, same-second backup-name collisions, the POST /api/setup/env
response shape, IPv6 userinfo redaction in status and diagnostics, and the
remaining invalid timeout values.
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
    return stat.S_IMODE(path.lstat().st_mode)


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.victim = self.root / "outside" / "victim.txt"
        self.victim.parent.mkdir()
        self.victim.write_text("VICTIM", encoding="utf-8")


@unittest.skipUnless(POSIX, "symlinks and modes are POSIX-only here")
class SymlinkTargetTests(_Tmp):
    def test_create_private_file_refuses_symlink_at_target(self):
        link = self.root / ".env.bak-x"
        link.symlink_to(self.victim)
        with self.assertRaises(FileExistsError):
            cl.create_private_file(link, "SECRET")
        made = cl.create_private_file(link, "SECRET", unique=True)
        self.assertEqual(made.name, ".env.bak-x-1")
        self.assertEqual(self.victim.read_text(encoding="utf-8"), "VICTIM")
        self.assertTrue(link.is_symlink())

    def test_dangling_symlink_at_target_is_not_followed(self):
        link = self.root / "dangling"
        target = self.root / "outside" / "created-through-link"
        link.symlink_to(target)
        with self.assertRaises(FileExistsError):
            cl.create_private_file(link, "SECRET")
        self.assertFalse(target.exists())

    def test_replace_private_file_ignores_symlinked_tmp(self):
        env = self.root / ".env"
        env.write_text("A=1\n", encoding="utf-8")
        (self.root / ".env.tmp").symlink_to(self.victim)
        cl.replace_private_file(env, "A=2\n")
        self.assertEqual(self.victim.read_text(encoding="utf-8"), "VICTIM")
        self.assertFalse(env.is_symlink())
        self.assertEqual(env.read_text(encoding="utf-8"), "A=2\n")
        self.assertEqual(_mode(env), 0o600)

    def test_replace_private_file_does_not_write_through_symlinked_target(self):
        env = self.root / ".env"
        env.symlink_to(self.victim)
        os.chmod(self.victim, 0o644)
        cl.replace_private_file(env, "A=2\n")
        self.assertEqual(self.victim.read_text(encoding="utf-8"), "VICTIM")
        self.assertEqual(_mode(self.victim), 0o644)
        self.assertFalse(env.is_symlink())
        self.assertEqual(_mode(env), 0o600)

    def test_mode_is_0600_even_with_umask_0(self):
        old = os.umask(0)
        self.addCleanup(os.umask, old)
        made = cl.create_private_file(self.root / "f", "x")
        self.assertEqual(_mode(made), 0o600)


class SetupEnvRouteTests(_Tmp):
    def setUp(self):
        super().setUp()
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
        self.client = self.flask_app.test_client()
        self.env_file = self.root / ".env"
        self.module._SETUP_ENV_FILE = self.env_file
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))

    def test_post_env_returns_backup_basename(self):
        self.env_file.write_text("PLEX_URL=http://old:32400\n", encoding="utf-8")
        r = self.client.post("/api/setup/env", json={"variables": {"PLEX_URL": "http://new:32400"}})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        name = body["backup_path"]
        self.assertTrue(name.startswith(".env.bak-"), name)
        self.assertEqual(name, Path(name).name)
        self.assertNotIn("/", body["migration"]["backup"])
        self.assertTrue((self.root / name).is_file())

    def test_post_env_without_existing_file_returns_empty_backup(self):
        r = self.client.post("/api/setup/env", json={"variables": {"PLEX_URL": "http://new:32400"}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["backup_path"], "")

    def test_same_second_saves_keep_both_backups(self):
        self.env_file.write_text("PLEX_URL=http://v1:32400\n", encoding="utf-8")
        with mock.patch.object(self.module.time, "strftime", return_value="20260101-000000"):
            first = self.module._write_env_file({"PLEX_URL": "http://v2:32400"}, [])
            second = self.module._write_env_file({"PLEX_URL": "http://v3:32400"}, [])
        self.assertEqual(first, ".env.bak-20260101-000000")
        self.assertEqual(second, ".env.bak-20260101-000000-1")
        self.assertIn("v1", (self.root / first).read_text(encoding="utf-8"))
        self.assertIn("v2", (self.root / second).read_text(encoding="utf-8"))
        self.assertIn("v3", self.env_file.read_text(encoding="utf-8"))
        if POSIX:
            for name in (first, second, ".env"):
                self.assertEqual(_mode(self.root / name), 0o600, name)


class RedactionTests(unittest.TestCase):
    CASES = {
        "http://alice:pw@beets:8337": "http://beets:8337",
        "http://alice:pw@[fd00::5]:8337/": "http://[fd00::5]:8337/",
        "http://u%40x:p%3Aw@[::1]:8337": "http://[::1]:8337",
        "http://a@b:pw@beets:8337": "http://beets:8337",
        "http://beets:8337?token=t": "http://beets:8337",
        "http://[::1": "<invalid-url>",
    }

    def test_redact_url_userinfo_cases(self):
        for raw, want in self.CASES.items():
            self.assertEqual(cl.redact_url_userinfo(raw), want, raw)

    def test_status_and_diagnostics_hide_ipv6_userinfo(self):
        flask_app, module = _load_routes_setup_against_stub_app(self)
        client = flask_app.test_client()
        saved = os.environ.get("BEETS_WEB_URL")
        os.environ["BEETS_WEB_URL"] = "http://alice:hunter2pw@[fd00::5]:8337"
        self.addCleanup(lambda: os.environ.pop("BEETS_WEB_URL", None) if saved is None
                        else os.environ.__setitem__("BEETS_WEB_URL", saved))
        from backend.beets_adapter import beets_adapter
        with mock.patch.object(beets_adapter, "get_plugin_status", side_effect=RuntimeError("down")):
            for url in ("/api/setup/status", "/api/setup/diagnostics"):
                r = client.get(url)
                text = r.get_data(as_text=True)
                self.assertNotIn("hunter2pw", text, url)
                self.assertNotIn("alice", text, url)
                if r.status_code == 200:
                    self.assertIn("[fd00::5]:8337", text, url)


class TimeoutValueTests(unittest.TestCase):
    def _run(self, raw):
        env = dict(os.environ, BEETS_OUTBOUND_TOTAL_TIMEOUT_SECONDS=raw)
        return subprocess.run(
            [sys.executable, "-c", "from backend import security as s; print(s._DEFAULT_TOTAL_TIMEOUT)"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
        )

    def test_invalid_values_fall_back_to_60(self):
        for raw in ("inf", "-inf", "1e400", "0", "-0", " ", "NaN", "60s"):
            proc = self._run(raw)
            self.assertEqual(proc.returncode, 0, (raw, proc.stderr))
            self.assertEqual(proc.stdout.strip().splitlines()[-1], "60.0", raw)
            self.assertIn("BEETS_OUTBOUND_TOTAL_TIMEOUT_SECONDS", proc.stderr, raw)

    def test_empty_and_valid_values(self):
        for raw, want in (("", "60.0"), ("0.5", "0.5"), (" 15 ", "15.0")):
            proc = self._run(raw)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip().splitlines()[-1], want, raw)
            self.assertNotIn("must be a positive number", proc.stderr, raw)


if __name__ == "__main__":
    unittest.main()
