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

    def test_rewrite_is_idempotent_and_keeps_original_bak_on_resume(self):
        """F3: re-running --rewrite-compose --apply --resume must succeed as a
        no-op and must NOT clobber <compose>.bak with the already-rewritten
        file (otherwise rollback is lost and the second run aborts)."""
        self.assertEqual(self._run("--rewrite-compose", "--apply").returncode, 0)
        bak = Path(self.compose + ".bak")
        self.assertEqual(bak.read_text(encoding="utf-8"), self.COMPOSE)
        # Second run (safe-to-re-run claim): no-op success, .bak still original.
        res2 = self._run("--rewrite-compose", "--apply", "--resume")
        self.assertEqual(res2.returncode, 0, res2.stderr)
        self.assertEqual(bak.read_text(encoding="utf-8"), self.COMPOSE,
                         "the ORIGINAL compose backup must be preserved across re-runs")
        new_dest = os.path.abspath(self.new).replace("\\", "/")
        self.assertIn("      - %s:/web-manager-data\n" % new_dest,
                      Path(self.compose).read_text(encoding="utf-8"))


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


@unittest.skipUnless(BASH, "needs a POSIX bash (Linux/macOS)")
class RelocateComposeHardeningTests(unittest.TestCase):
    """Regression tests for the --rewrite-compose hardening (F1, F2, F5):
    detection and rewrite must be scoped to the TARGET --service, must refuse
    (not corrupt) variable and long-form sources, and must never silently exit
    without a plan or a helpful error.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bwm-relocate-harden-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.old = os.path.join(self.tmp, "old-data")
        self.new = os.path.join(self.tmp, "new-data")
        self.compose = os.path.join(self.tmp, "docker-compose.yml")
        os.makedirs(os.path.join(self.old, "jobs"))
        Path(self.old, "sentinel.txt").write_text("keep-me\n", encoding="utf-8")
        Path(self.old, "jobs", "job_1.json").write_text('{"id": "job_1"}', encoding="utf-8")

    def _write(self, text):
        Path(self.compose).write_text(text, encoding="utf-8")

    def _run(self, *args):
        return subprocess.run(
            [BASH, str(SCRIPT), "--source", self.old, "--dest", self.new,
             "--compose-file", self.compose, "--no-recreate", *args],
            capture_output=True, text=True,
        )

    def test_f1_does_not_rewrite_another_services_mount_line(self):
        """F1 (data loss): the TARGET service uses a long-form mount at
        /web-manager-data; a DIFFERENT service has a short-form literal bind at
        the same container path. Rewrite must NOT touch the other service's
        line (whole-file matching rewrote it, reported SUCCESS, and left the
        real service on the old path)."""
        other_literal = "      - /opt/other/web-manager-data:/web-manager-data\n"
        compose = (
            "services:\n"
            "  beets-web-manager:\n"           # target service: long-form mount
            "    image: ghcr.io/iranman/beets-web-manager:latest\n"
            "    volumes:\n"
            "      - type: bind\n"
            "        source: /opt/placeholder/web-manager-data\n"
            "        target: /web-manager-data\n"
            "      - beets-config:/config\n"
            "  sidecar:\n"                      # unrelated service, short literal
            "    image: busybox\n"
            "    volumes:\n"
            + other_literal
        )
        self._write(compose)
        res = self._run("--rewrite-compose", "--apply")
        # The target mount is long-form (this tool rewrites only short-form), so
        # it must refuse -- and crucially must NOT have rewritten the sidecar.
        self.assertNotEqual(res.returncode, 0, res.stderr)
        text = Path(self.compose).read_text(encoding="utf-8")
        self.assertIn(other_literal, text,
                      "the OTHER service's mount line must be left untouched")
        self.assertNotIn(os.path.abspath(self.new).replace("\\", "/"), text)
        self.assertFalse(Path(self.compose + ".bak").exists())

    def test_f2_longform_source_falls_through_to_helpful_error(self):
        """F2 (silent regression): a long-form / undetectable mount must not
        make detect_literal_source return 1 and silently kill the script under
        set -e. It must reach the helpful 'could not determine' error."""
        compose = (
            "services:\n"
            "  beets-web-manager:\n"
            "    volumes:\n"
            "      - type: bind\n"
            "        source: /opt/placeholder/web-manager-data\n"
            "        target: /web-manager-data\n"
            "      - beets-config:/config\n"
        )
        self._write(compose)
        res = self._run("--apply")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("could not determine", res.stderr,
                      "must print the helpful error, not exit silently")

    def test_f5_unbraced_variable_source_is_not_treated_as_literal(self):
        """F5 (false SUCCESS on corruption): a bare `$VAR:/web-manager-data` is
        a Compose interpolation, not a literal. It must be refused, never
        rewritten into corrupt YAML that still reports SUCCESS."""
        compose = (
            "services:\n"
            "  beets-web-manager:\n"
            "    volumes:\n"
            "      - $DATA:/web-manager-data\n"
            "      - beets-config:/config\n"
        )
        self._write(compose)
        res = self._run("--rewrite-compose", "--apply")
        self.assertNotEqual(res.returncode, 0, res.stderr)
        # The compose must be untouched (no corrupt, separator-less line).
        self.assertEqual(Path(self.compose).read_text(encoding="utf-8"), compose)
        self.assertFalse(Path(self.compose + ".bak").exists())

    def test_f4_dest_with_colon_is_rejected(self):
        """F4 (unvalidated insertion): a --dest containing ':' (or '$'/quotes)
        would re-delimit a short-form bind; it must be rejected up front."""
        compose = (
            "services:\n"
            "  beets-web-manager:\n"
            "    volumes:\n"
            "      - /opt/placeholder/web-manager-data:/web-manager-data\n"
        )
        self._write(compose)
        res = subprocess.run(
            [BASH, str(SCRIPT), "--source", self.old,
             "--dest", "/opt/new:web-manager-data",
             "--compose-file", self.compose, "--no-recreate",
             "--rewrite-compose", "--apply"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(Path(self.compose).read_text(encoding="utf-8"), compose)


if __name__ == "__main__":
    unittest.main()
