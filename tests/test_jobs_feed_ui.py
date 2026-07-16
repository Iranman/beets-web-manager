import unittest
from pathlib import Path


class JobsFeedUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.jobs_source = (root / "frontend" / "src" / "views" / "Jobs.tsx").read_text(encoding="utf-8")
        cls.feed_source = (root / "frontend" / "src" / "lib" / "jobFeed.ts").read_text(encoding="utf-8")

    def test_job_feed_mapping_is_centralized(self):
        self.assertIn("export function buildJobFeed", self.feed_source)
        self.assertIn("Syncing playlist to Plex", self.feed_source)
        self.assertIn("Checking the current Beets library paths before syncing to Plex", self.feed_source)
        self.assertIn("scan_started", self.feed_source)
        self.assertIn("track_matched", self.feed_source)
        self.assertIn("plex_sync_started", self.feed_source)
        self.assertIn("job_completed", self.feed_source)
        self.assertIn("Plex sync", self.feed_source)
        self.assertIn("Needs attention", self.feed_source)

    def test_jobs_panel_defaults_to_human_feed(self):
        self.assertIn("buildJobFeed", self.jobs_source)
        self.assertIn("Current phase:", self.jobs_source)
        self.assertIn("Waiting for the next update", self.jobs_source)
        self.assertIn("Cancel job", self.jobs_source)
        self.assertIn("Show raw log", self.jobs_source)
        self.assertIn("Copy raw log", self.jobs_source)
        self.assertIn("Download raw log", self.jobs_source)
        self.assertIn("function copyTextWithFallback", self.jobs_source)
        self.assertIn("document.execCommand('copy')", self.jobs_source)
        self.assertIn("copyFailed ? 'Copy failed'", self.jobs_source)
        self.assertIn("Show technical fields", self.jobs_source)
        self.assertIn("Details", self.jobs_source)
        self.assertIn("View raw log", self.jobs_source)
        self.assertIn("Current activity", self.jobs_source)
        self.assertIn("Show more history", self.jobs_source)
        self.assertIn("HISTORY_STATUS_OPTIONS", self.jobs_source)
        self.assertIn("No running jobs are repeated here", self.jobs_source)
        self.assertIn("Last finished:", self.jobs_source)
        self.assertIn("groupFeedEntries", self.jobs_source)
        self.assertIn("JobDetailsDialog", self.jobs_source)
        self.assertIn("jobs={runningJobs}", self.jobs_source)
        self.assertNotIn("raw line(s) available", self.jobs_source)
        self.assertNotIn("Logs/details", self.jobs_source)
        self.assertNotIn("Expand full log", self.jobs_source)
        self.assertNotIn("View feed", self.jobs_source)
        self.assertNotIn("Open full feed", self.jobs_source)
        self.assertNotIn("Open feed", self.jobs_source)
        self.assertNotIn("Live Feed", self.jobs_source)
        self.assertNotIn("Latest:", self.jobs_source)
        self.assertNotIn("BottomConsole", self.jobs_source)
        self.assertNotIn(">Kill<", self.jobs_source)


if __name__ == "__main__":
    unittest.main()