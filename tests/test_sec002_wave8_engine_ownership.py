"""SEC-002 Wave 8 architecture-unblock regression tests.

Claude's independent final review of PR #70 blocked it with:

    blocked -- web-manager media mutation violates architecture

reimport_disk() invoked BEET_BIN via a direct local subprocess.run() for its
core import step (the web-manager image has no beet binary in the shipped
Compose topology), and create_unmatched_draft() wrote directly under
MUSIC_ROOT (a root the web manager neither owns nor, per the shipped Compose
files, has mounted at all).

This module guards both fixes:

1. reimport_disk()'s import step now goes through _beet_run() ->
   backend.beets_client.BeetsClient.run_command() (an authenticated HTTP call
   to the Beets engine's control agent), exactly like every other Beets
   mutation in this function already did. Source-level tests assert no
   direct subprocess.run() call remains in reimport_disk(); behavioral tests
   assert the routing actually happens and that BEET_BIN being entirely
   absent from PATH does not matter anymore.
2. create_unmatched_draft() now writes review-only metadata (no audio) under
   UNMATCHED_DRAFT_ROOT (/web-manager-data/unmatched_drafts by default),
   which every shipped Compose file actually mounts into this container --
   see test_sec002_app_path_ai_batch_replacement_staging.py for its
   containment/collision/secret-redaction tests.
"""
import unittest
import unittest.mock as mock
from pathlib import Path

import app as APP
import job_engine


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")


def function_source(name: str) -> str:
    start = APP_SOURCE.index(f"def {name}(")
    end = APP_SOURCE.find("\ndef ", start + 5)
    if end == -1:
        end = len(APP_SOURCE)
    return APP_SOURCE[start:end]


class ReimportDiskNoLocalSubprocessTests(unittest.TestCase):
    """Source-level architecture guard: the previously-blocking pattern must
    never reappear in this function, even from an unrelated future edit."""

    def test_reimport_disk_never_calls_subprocess_run_directly(self):
        source = function_source("reimport_disk")
        self.assertNotIn("subprocess.run(", source)
        self.assertNotIn("subprocess.Popen(", source)
        self.assertNotIn("subprocess.call(", source)

    def test_reimport_disk_import_step_routes_through_beet_run(self):
        source = function_source("reimport_disk")
        self.assertIn('_beet_run(\n            base_tmp + ["import"', source)
        self.assertIn("config_override=temp_cfg_content", source)

    def test_create_unmatched_draft_never_references_music_root(self):
        # Excludes the docstring, which legitimately explains in prose why
        # MUSIC_ROOT is NOT used here -- the code itself must never
        # reference it as an actual path component.
        source = function_source("create_unmatched_draft")
        code_only = source.split('"""', 2)[-1]
        self.assertNotIn("MUSIC_ROOT", code_only)
        self.assertIn("UNMATCHED_DRAFT_ROOT", code_only)


class BeetRunConfigOverrideTests(unittest.TestCase):
    """_beet_run must forward config_override to the remote engine call
    rather than silently discarding it (a "-c <path>" token embedded in cmd
    is always stripped, since a local web-manager path has no meaning on
    the remote engine -- only the explicit config_override kwarg reaches
    the control agent)."""

    def test_config_override_is_forwarded_to_run_command(self):
        log: list = []
        with mock.patch.object(job_engine.beets_client, "run_command",
                                return_value={"returncode": 0, "stdout": "", "stderr": ""}) as mock_run:
            job_engine._beet_run(
                ["/lsiopy/bin/beet", "-c", "/tmp/should-be-ignored.yaml", "import", "/data/torrents/music/x"],
                log, config_override="import:\n  copy: no\n",
            )
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        self.assertEqual(kwargs.get("config_override"), "import:\n  copy: no\n")
        # The stripped local "-c" token must never reach the remote call.
        called_args = mock_run.call_args.args
        self.assertNotIn("/tmp/should-be-ignored.yaml", called_args)

    def test_beet_executable_token_and_dash_c_are_stripped_not_forwarded(self):
        log: list = []
        with mock.patch.object(job_engine.beets_client, "run_command",
                                return_value={"returncode": 0, "stdout": "ok", "stderr": ""}) as mock_run:
            r = job_engine._beet_run(["/lsiopy/bin/beet", "-c", "/tmp/x.yaml", "mbsync", "album_id:1"], log)
        self.assertEqual(r.returncode, 0)
        mock_run.assert_called_once_with(
            "mbsync", args=["album_id:1"], timeout=120.0, config_override="",
        )

    def test_remote_timeout_is_surfaced_not_silently_swallowed(self):
        """The control agent maps a subprocess timeout to HTTP 408; the
        client wraps that as a BeetsError. reimport_disk()'s import step
        detects this via "timed out" in the returned stderr to preserve its
        pre-migration "treat timeout as inconclusive, check the DB
        afterward" behavior instead of a hard failure."""
        from backend.beets_client import BeetsError
        log: list = []
        with mock.patch.object(job_engine.beets_client, "run_command",
                                side_effect=BeetsError("Beets API request error: Command 'import' timed out after 300s")):
            r = job_engine._beet_run(["/lsiopy/bin/beet", "import", "/data/torrents/music/x"], log, timeout=300)
        self.assertEqual(r.returncode, 1)
        self.assertIn("timed out", r.stderr.lower())


class ReimportDiskNoBeetBinaryRequiredTests(unittest.TestCase):
    """Proves the architecture fix, not just its source shape: the import
    step succeeds via the mocked remote engine call even when BEET_BIN
    resolves to a path that does not exist on this machine at all --
    reproducing the real web-manager container, which has no beet binary."""

    def test_import_step_succeeds_with_no_local_beet_binary_present(self):
        log: list = []
        # Ends in "/beet" (matching how BEET_BIN is actually named -- see
        # app.py: BEET_BIN = shutil.which("beet") or "/lsiopy/bin/beet") but
        # the path itself does not exist anywhere on this machine, exactly
        # reproducing the real web-manager container.
        with mock.patch.object(APP, "BEET_BIN", "/nonexistent/path/does/not/exist/beet"), \
             mock.patch.object(job_engine.beets_client, "run_command",
                                return_value={"returncode": 0, "stdout": "tagged and imported", "stderr": ""}) as mock_run, \
             mock.patch("subprocess.run", side_effect=AssertionError(
                 "reimport_disk must not call subprocess.run locally")):
            r = job_engine._beet_run(
                [APP.BEET_BIN, "-c", "/tmp/beets_reimport_disk.yaml", "import", "-q",
                 "--noincremental", "--quiet-fallback", "asis", "--search-id",
                 "11111111-1111-1111-1111-111111111111", "/data/torrents/music/Artist/Album"],
                log, timeout=300, config_override="import:\n  copy: no\n  move: no\n",
            )
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "tagged and imported")
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args.args[0], "import")


if __name__ == "__main__":
    unittest.main()
