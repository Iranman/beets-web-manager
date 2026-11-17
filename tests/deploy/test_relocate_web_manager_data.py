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


@unittest.skipUnless(BASH, "needs a POSIX bash (Linux/macOS)")
class RelocateHardcodedComposeTests(unittest.TestCase):
    """The mount source is a HARDCODED host path in the Compose file (no
    ${VAR}), so there is no env key to set. --rewrite-compose rewrites that one
    line in place; without it the script refuses and points at --rewrite-compose.
    """

    MOUNT_LINE = "      - /opt/placeholder/web-manager-data:/web-manager-data\n"
    COMPOSE = (
        "services:\n"
        "  beets-web-manager:\n"
        "    image: ghcr.io/iranman/beets-web-manager:latest\n"
        "    volumes:\n"
        + MOUNT_LINE +
        "      - /opt/placeholder/music:/music:ro\n"
        "      - beets-config:/config\n"
    )

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bwm-relocate-hc-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.old = os.path.join(self.tmp, "old-data")
        self.new = os.path.join(self.tmp, "new-data")
        self.compose = os.path.join(self.tmp, "docker-compose.yml")
        os.makedirs(os.path.join(self.old, "jobs"))
        Path(self.old, "sentinel.txt").write_text("keep-me\n", encoding="utf-8")
        Path(self.old, "jobs", "job_1.json").write_text('{"id": "job_1"}', encoding="utf-8")
        Path(self.compose).write_text(self.COMPOSE, encoding="utf-8")

    def _run(self, *args):
        return subprocess.run(
            [BASH, str(SCRIPT), "--source", self.old, "--dest", self.new,
             "--compose-file", self.compose, "--no-recreate", *args],
            capture_output=True, text=True,
        )

    def test_rewrite_compose_edits_one_line_and_writes_bak(self):
        res = self._run("--rewrite-compose", "--apply")
        self.assertEqual(res.returncode, 0, res.stderr)
        new_dest = os.path.abspath(self.new).replace("\\", "/")

        # The one mount-source line was rewritten to the new path.
        new_text = Path(self.compose).read_text(encoding="utf-8")
        self.assertIn("      - %s:/web-manager-data\n" % new_dest, new_text)
        self.assertNotIn("/opt/placeholder/web-manager-data", new_text)

        # A .bak of the original compose was written first, byte-identical.
        bak = Path(self.compose + ".bak")
        self.assertTrue(bak.is_file(), "expected a <compose>.bak")
        self.assertEqual(bak.read_text(encoding="utf-8"), self.COMPOSE)

        # Every OTHER line is byte-identical to the original (only one changed).
        orig_lines = self.COMPOSE.splitlines(keepends=True)
        new_lines = new_text.splitlines(keepends=True)
        self.assertEqual(len(orig_lines), len(new_lines))
        diffs = [i for i in range(len(orig_lines)) if orig_lines[i] != new_lines[i]]
        self.assertEqual(diffs, [orig_lines.index(self.MOUNT_LINE)],
                         "exactly one line (the mount line) must change")
        # Sibling lines explicitly intact (image tag colon, other mounts).
        self.assertIn("    image: ghcr.io/iranman/beets-web-manager:latest\n", new_text)
        self.assertIn("      - /opt/placeholder/music:/music:ro\n", new_text)
        self.assertIn("      - beets-config:/config\n", new_text)

        # Data was still copied and a data backup archive written.
        self.assertEqual(Path(self.new, "sentinel.txt").read_text(encoding="utf-8"), "keep-me\n")
        backups = list(Path(self.tmp).glob("_web-manager-relocate-backups/*.tar.gz"))
        self.assertEqual(len(backups), 1)

    def test_hardcoded_without_flag_errors_pointing_at_rewrite_compose(self):
        res = self._run("--apply")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("--rewrite-compose", res.stderr)
        self.assertIn("HARDCODED", res.stderr)
        # Nothing was touched: no compose rewrite, no .bak.
        self.assertEqual(Path(self.compose).read_text(encoding="utf-8"), self.COMPOSE)
        self.assertFalse(Path(self.compose + ".bak").exists())

    def test_dry_run_hardcoded_with_flag_plans_rewrite_and_changes_nothing(self):
        res = self._run("--rewrite-compose")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("REWRITE", res.stderr)
        self.assertEqual(Path(self.compose).read_text(encoding="utf-8"), self.COMPOSE)
        self.assertFalse(Path(self.compose + ".bak").exists())


@unittest.skipUnless(BASH, "needs a POSIX bash (Linux/macOS)")
class RelocateEnvVarComposeTests(unittest.TestCase):
    """The default env-var form: the Compose mount interpolates ${VAR}. The
    script detects that key from the Compose file and sets it in the env file;
    the Compose YAML is left untouched (no .bak)."""

    COMPOSE = (
        "services:\n"
        "  beets-web-manager:\n"
        "    volumes:\n"
        "      - ${BEETS_WEB_MANAGER_DATA_PATH:-./web-manager-data}:/web-manager-data\n"
        "      - beets-config:/config\n"
    )

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bwm-relocate-env-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.old = os.path.join(self.tmp, "old-data")
        self.new = os.path.join(self.tmp, "new-data")
        self.compose = os.path.join(self.tmp, "docker-compose.yml")
        self.env = os.path.join(self.tmp, ".env")
        os.makedirs(self.old)
        Path(self.old, "sentinel.txt").write_text("keep-me\n", encoding="utf-8")
        Path(self.compose).write_text(self.COMPOSE, encoding="utf-8")
        Path(self.env).write_text("TZ=UTC\n", encoding="utf-8")

    def test_env_key_detected_from_compose_and_compose_untouched(self):
        res = subprocess.run(
            [BASH, str(SCRIPT), "--source", self.old, "--dest", self.new,
             "--compose-file", self.compose, "--env-file", self.env,
             "--no-recreate", "--apply"],
            capture_output=True, text=True,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        new_dest = os.path.abspath(self.new).replace("\\", "/")
        # The detected key was set in the env file.
        env_text = Path(self.env).read_text(encoding="utf-8")
        self.assertIn("BEETS_WEB_MANAGER_DATA_PATH=" + new_dest, env_text)
        self.assertIn("TZ=UTC", env_text)
        # The Compose YAML was NOT edited and no .bak was written.
        self.assertEqual(Path(self.compose).read_text(encoding="utf-8"), self.COMPOSE)
        self.assertFalse(Path(self.compose + ".bak").exists())


if __name__ == "__main__":
    unittest.main()
