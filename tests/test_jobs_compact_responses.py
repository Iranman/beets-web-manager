import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTES_SOURCE = (ROOT / "routes_jobs.py").read_text(encoding="utf-8")
TYPES_SOURCE = (ROOT / "frontend" / "src" / "types" / "api.ts").read_text(encoding="utf-8")


class JobsCompactResponseTests(unittest.TestCase):
    def test_job_list_excludes_full_results_by_default(self):
        self.assertIn('include_result = _truthy_arg("include_result", False)', ROUTES_SOURCE)
        self.assertIn('_present_job_row(j, include_result=include_result)', ROUTES_SOURCE)
        self.assertIn('"duration_ms": duration_ms', ROUTES_SOURCE)
        self.assertIn('result_summary?: JobResultSummary', TYPES_SOURCE)

    def test_jobs_page_does_not_autostart_library_health_scan(self):
        jobs_source = (ROOT / "frontend" / "src" / "views" / "Jobs.tsx").read_text(encoding="utf-8")
        self.assertIn('<LibraryHealthPanel active autoLoad={false} />', jobs_source)
        self.assertNotIn('<LibraryHealthPanel active autoLoad />', jobs_source)
    def test_job_feed_builds_only_limited_entries(self):
        self.assertIn('for idx in range(len(job.log) - 1, -1, -1):', ROUTES_SOURCE)
        self.assertIn('if len(entries) >= limit:', ROUTES_SOURCE)
        self.assertNotIn('sliced = entries[-limit:]', ROUTES_SOURCE)


if __name__ == "__main__":
    unittest.main()