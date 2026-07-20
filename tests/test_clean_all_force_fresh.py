"""Test for a real gap found via live testing: once a maintenance-runner
checkpoint is resumable, start_maintenance_runner() always auto-resumed with
no way to bypass it -- not from the UI, not even via a direct API call. That
made "start a fresh Clean All run" (the correct advice for a checkpoint left
behind by an app restart, since a skipped step is never retried by resuming)
literally impossible to act on. Fixed with an explicit force_fresh flag.
"""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
JOBS_SOURCE = (ROOT / "frontend" / "src" / "views" / "Jobs.tsx").read_text(encoding="utf-8")
CLIENT_SOURCE = (ROOT / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")


def _function_source(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


class BackendForceFreshTests(unittest.TestCase):
    def setUp(self):
        self._fn = _function_source(
            APP_SOURCE, "def start_maintenance_runner():", "def _run(\n"
        )

    def test_force_fresh_flag_read_from_request_body(self):
        self.assertIn('force_fresh = bool((request.get_json(silent=True) or {}).get("force_fresh"))', self._fn)

    def test_force_fresh_skips_the_resume_lookup_entirely(self):
        self.assertIn('resume_snapshot = {} if force_fresh else _maintenance_resume_from_report(', self._fn)

    def test_already_running_guard_still_checked_first(self):
        # force_fresh must not be able to start a second concurrent run.
        running_pos = self._fn.index("running = _maintenance_running_job()")
        force_fresh_pos = self._fn.index("force_fresh =")
        self.assertLess(running_pos, force_fresh_pos)


class FrontendForceFreshTests(unittest.TestCase):
    def test_client_wrapper_sends_force_fresh(self):
        fn = _function_source(CLIENT_SOURCE, "export function startMaintenanceRunner(", "export function startCleanAll(")
        self.assertIn("options?.forceFresh ? { force_fresh: true } : undefined", fn)

    def test_ui_offers_explicit_start_fresh_action_next_to_resume(self):
        self.assertIn("Discard checkpoint and start fresh instead.", JOBS_SOURCE)
        self.assertIn("forceFresh: true", JOBS_SOURCE)

    def test_start_fresh_action_confirms_before_discarding_checkpoint(self):
        block = _function_source(JOBS_SOURCE, "Discard checkpoint and start fresh instead", "</Button>")
        # confirm() call is just before this button's label in the onClick handler
        prior = JOBS_SOURCE[max(0, JOBS_SOURCE.index("Discard checkpoint and start fresh instead") - 400):JOBS_SOURCE.index("Discard checkpoint and start fresh instead")]
        self.assertIn("window.confirm(", prior)


if __name__ == "__main__":
    unittest.main()
