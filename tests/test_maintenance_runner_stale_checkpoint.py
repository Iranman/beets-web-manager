"""Test for a real bug found via live investigation: a Clean All run gets
killed by an app restart mid-execution (routine here -- JobStore is
in-memory only and every backend deploy this session required one), but the
maintenance-runner checkpoint file was never updated to reflect that, so
/api/jobs/maintenance-runner/report kept reporting last_run.status
"running" indefinitely with no real job behind it -- exactly the class of
stale-checkpoint bug already handled for playlist download jobs
(_playlist_job_is_live / mark_interrupted), just missing here. This is also
why a real symptom report ("Clean All doesn't seem to run in batches")
traced back to nothing: the interrupted run had skipped release_group_merge
(the batched step) before dying, and skip status is treated as already-done
for resume purposes, so resuming it would never retry that step either --
only a fresh run would.
"""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")


def _function_source(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


class MaintenanceRunnerStaleCheckpointTests(unittest.TestCase):
    def setUp(self):
        self._fn = _function_source(
            APP_SOURCE, "def maintenance_runner_report():", "def _run_with_app_context("
        )

    def test_relabels_running_status_when_no_live_job_backs_it(self):
        self.assertIn('_s(last_run.get("status")).strip().lower() == "running"', self._fn)
        self.assertIn("not _maintenance_running_job()", self._fn)
        self.assertIn('last_run["status"] = "interrupted"', self._fn)

    def test_checks_liveness_via_the_real_job_store_not_the_checkpoint_file(self):
        # The bug is specifically that the checkpoint file and the JobStore
        # can disagree after a restart; the fix must consult the live
        # JobStore (_maintenance_running_job), not just trust the file.
        self.assertIn("_maintenance_running_job()", self._fn)

    def test_only_touches_the_response_not_the_saved_checkpoint_file(self):
        # Must not call _maintenance_save_last_report or otherwise persist
        # the relabel -- this is a read-time correction of what gets
        # reported, not a rewrite of on-disk history.
        self.assertNotIn("_maintenance_save_last_report", self._fn)
        self.assertNotIn(".write_text(", self._fn)


if __name__ == "__main__":
    unittest.main()
