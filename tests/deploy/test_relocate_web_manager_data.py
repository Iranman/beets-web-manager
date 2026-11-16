"""Smoke tests for scripts/relocate_web_manager_data.sh.

Runs the real script with bash against temporary directories that stand in
for the old and new /web-manager-data host folders. No Docker is required:
--no-recreate skips the recreate/health stage, and the env repoint is pointed
at a temp .env with an explicit --env-key.

Linux/macOS only (POSIX bash); skipped on Windows.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "relocate_web_manager_data.sh"
BASH = shutil.which("bash") if os.name != "nt" else None


@unittest.skipUnless(BASH, "needs a POSIX bash (Linux/macOS)")
class RelocateWebManagerDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bwm-relocate-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.old = os.path.join(self.tmp, "old-data")
        self.new = os.path.join(self.tmp, "new-data")
        self.env = os.path.join(self.tmp, ".env")
        os.makedirs(os.path.join(self.old, "jobs"))
        os.makedirs(os.path.join(self.old, "transactions"))
        Path(self.old, "sentinel.txt").write_text("keep-me\n", encoding="utf-8")
        Path(self.old, "jobs", "job_1.json").write_text('{"id": "job_1"}', encoding="utf-8")
        Path(self.env).write_text("TZ=UTC\nWEBCONTROL_PORT=8337\n", encoding="utf-8")

    def _run(self, *args):
        return subprocess.run(
            [BASH, str(SCRIPT), "--source", self.old, "--dest", self.new,
             "--env-file", self.env, "--env-key", "BEETS_WEB_MANAGER_DATA_PATH",
             "--no-recreate", *args],
            capture_output=True, text=True,
        )

    def test_dry_run_plans_move_and_changes_nothing(self):
        res = self._run()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("DRY-RUN", res.stderr)
        self.assertIn(os.path.abspath(self.old).replace("\\", "/"), res.stderr)
        self.assertIn(os.path.abspath(self.new).replace("\\", "/"), res.stderr)
        # No changes: dest not created, env untouched.
        self.assertFalse(os.path.exists(self.new))
        self.assertNotIn("BEETS_WEB_MANAGER_DATA_PATH", Path(self.env).read_text(encoding="utf-8"))

    def test_apply_copies_data_preserves_sentinel_and_sets_one_env_key(self):
        res = self._run("--apply")
        self.assertEqual(res.returncode, 0, res.stderr)
        # Data copied, sentinel + nested file preserved.
        self.assertEqual(Path(self.new, "sentinel.txt").read_text(encoding="utf-8"), "keep-me\n")
        self.assertTrue(os.path.isfile(os.path.join(self.new, "jobs", "job_1.json")))
        # Old dir left intact (copy, not cut).
        self.assertTrue(os.path.isfile(os.path.join(self.old, "sentinel.txt")))
        # Exactly one key added; existing keys preserved.
        env_text = Path(self.env).read_text(encoding="utf-8")
        expected = "BEETS_WEB_MANAGER_DATA_PATH=" + os.path.abspath(self.new).replace("\\", "/")
        self.assertIn(expected, env_text)
        self.assertIn("TZ=UTC", env_text)
        self.assertIn("WEBCONTROL_PORT=8337", env_text)
        self.assertEqual(env_text.count("BEETS_WEB_MANAGER_DATA_PATH="), 1)
        # A backup .tar.gz was written and is non-empty.
        backups = list(Path(self.tmp).glob("_web-manager-relocate-backups/*.tar.gz"))
        self.assertEqual(len(backups), 1, "expected exactly one backup archive")
        self.assertGreater(backups[0].stat().st_size, 0)

    def test_refuses_source_equals_dest(self):
        res = subprocess.run(
            [BASH, str(SCRIPT), "--source", self.old, "--dest", self.old,
             "--env-file", self.env, "--env-key", "X", "--no-recreate"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("same path", res.stderr)

    def test_apply_is_idempotent_with_resume(self):
        self.assertEqual(self._run("--apply").returncode, 0)
        # Re-running without --resume refuses (dest now non-empty).
        self.assertNotEqual(self._run("--apply").returncode, 0)
        # With --resume it succeeds again.
        self.assertEqual(self._run("--apply", "--resume").returncode, 0)


if __name__ == "__main__":
    unittest.main()
