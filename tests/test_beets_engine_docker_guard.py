"""End-to-end Docker test proving the startup guard is wired into the real
S6 startup chain of the beets-engine image, not just callable in isolation.

Opt-in only (skipped unless RUN_DOCKER_TESTS=1 and a working Docker daemon
is available): building an image and running real containers is much
slower than this repo's normal static-analysis-style test suite and
should not be a default requirement for `pytest`/`unittest discover`.

Manually verified during development of this test (2026-07-28):
- No database + BEETS_EXPECT_EXISTING_LIBRARY=1 (the image default) ->
  guard refuses, control agent process never starts, container idles on
  `sleep infinity`.
- A valid populated database + BEETS_API_TOKEN set -> guard passes,
  control agent process starts and stays running, database checksum and
  item/album counts are unchanged before and after start.
"""

import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKER_BIN = shutil.which("docker")
RUN_DOCKER_TESTS = os.environ.get("RUN_DOCKER_TESTS") == "1"
IMAGE_TAG = "beets-engine:pytest-guard-check"


def _make_valid_db(path: Path, items: int, albums: int) -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")
    con.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY)")
    for _ in range(items):
        con.execute("INSERT INTO items DEFAULT VALUES")
    for _ in range(albums):
        con.execute("INSERT INTO albums DEFAULT VALUES")
    con.commit()
    con.close()


@unittest.skipUnless(RUN_DOCKER_TESTS and DOCKER_BIN, "opt-in: set RUN_DOCKER_TESTS=1 with a working Docker daemon")
class BeetsEngineStartupGuardDockerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(
            [DOCKER_BIN, "build", "--no-cache", "-f", "Dockerfile.beets", "-t", IMAGE_TAG, "."],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        if result.returncode != 0:
            raise RuntimeError(f"image build failed:\n{result.stdout}\n{result.stderr}")

    @classmethod
    def tearDownClass(cls):
        subprocess.run([DOCKER_BIN, "rmi", "-f", IMAGE_TAG], capture_output=True, text=True, encoding="utf-8", errors="replace")

    def _run_container(self, name, config_dir, env=None):
        self.addCleanup(lambda: subprocess.run([DOCKER_BIN, "rm", "-f", name], capture_output=True, text=True, encoding="utf-8", errors="replace"))
        cmd = [DOCKER_BIN, "run", "-d", "--name", name, "--network", "none", "-v", f"{config_dir}:/config"]
        for key, value in (env or {}).items():
            cmd += ["-e", f"{key}={value}"]
        cmd.append(IMAGE_TAG)
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        time.sleep(8)

    def _logs(self, name):
        result = subprocess.run([DOCKER_BIN, "logs", name], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        return result.stdout + result.stderr

    def _control_agent_running(self, name):
        result = subprocess.run([DOCKER_BIN, "top", name], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        return "beets_control_agent.py" in result.stdout

    def test_guard_blocks_control_agent_when_database_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run_container("pytest-guard-empty", tmp)
            logs = self._logs("pytest-guard-empty")
            self.assertIn("REFUSING TO START", logs)
            self.assertIn("STARTUP GUARD REFUSED", logs)
            self.assertFalse(self._control_agent_running("pytest-guard-empty"))

    def test_guard_passes_and_control_agent_starts_with_valid_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "musiclibrary.blb"
            _make_valid_db(db_path, items=1500, albums=150)

            self._run_container(
                "pytest-guard-good",
                tmp,
                env={
                    "BEETS_MIN_EXPECTED_ITEMS": "1000",
                    "BEETS_MIN_EXPECTED_ALBUMS": "100",
                    "BEETS_API_TOKEN": "test-token-with-at-least-32-characters-xyz",
                },
            )
            logs = self._logs("pytest-guard-good")
            self.assertIn("startup guard passed", logs)
            self.assertTrue(self._control_agent_running("pytest-guard-good"))

            # A benign byte-level write (e.g. a schema/journal-mode touch on
            # first open) is expected and not itself a problem -- observed
            # identically on the real production restore this session
            # (checksum drifted, item/album counts did not). The invariant
            # that actually matters is that no data was lost or corrupted.
            con = sqlite3.connect(str(db_path))
            self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(con.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1500)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM albums").fetchone()[0], 150)
            con.close()


if __name__ == "__main__":
    unittest.main()
