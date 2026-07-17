import unittest
from pathlib import Path

from project_docs import read_operator_docs


class LibraryHealthJobsTests(unittest.TestCase):
    def test_library_health_scan_is_jobstore_backed(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "app.py").read_text(encoding="utf-8")
        route_source = app_source[
            app_source.index('@app.post("/api/clean/library-health/scan")'):
            app_source.index("def _clean_remove_orphaned_items")
        ]
        panel_source = (
            root / "frontend" / "src" / "features" / "libraryHealth" / "LibraryHealthPanel.tsx"
        ).read_text(encoding="utf-8")
        clean_source = (root / "frontend" / "src" / "views" / "Clean.tsx").read_text(encoding="utf-8")
        docs_source = read_operator_docs(root)

        self.assertIn("jobs.start_python(", route_source)
        self.assertIn('"type": "library-health-scan"', route_source)
        self.assertIn("result = _library_health_payload(progress=update_state)", route_source)
        self.assertIn('"current_task": "Library DB Health scan complete"', route_source)
        self.assertIn('"same_release_group_id_groups"', route_source)
        self.assertIn('"final_summary": final_summary', route_source)
        self.assertIn("return result", route_source)

        self.assertIn("useJobPoll(scanJobId)", panel_source)
        self.assertIn("getJobResult<LibraryHealthResponse>", panel_source)
        self.assertIn("'/api/clean/library-health/scan'", panel_source)
        self.assertIn("navigate(jobsUrl(scanJobId))", panel_source)
        self.assertIn("JobStatusCard job={scanJob}", panel_source)
        self.assertIn("beets:jobs-changed", panel_source)
        self.assertIn("CLEAN_JOB_TAB_RULES", clean_source)
        self.assertIn("library-health-scan", clean_source)
        self.assertIn("CLEAN_JOB_TAB_RULES", docs_source)
        self.assertIn("library-health-scan", docs_source)


if __name__ == "__main__":
    unittest.main()
