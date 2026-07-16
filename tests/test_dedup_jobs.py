import unittest
from pathlib import Path


class DedupJobsTests(unittest.TestCase):
    def test_dedup_scans_are_jobstore_backed(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "app.py").read_text(encoding="utf-8")
        dedup_source = app_source[app_source.index('@app.post("/api/dedup/scan")'):app_source.index('@app.post("/api/dedup/cleanup")')]
        panel_source = (root / "frontend" / "src" / "features" / "dedup" / "DedupPanel.tsx").read_text(encoding="utf-8")
        clean_source = (root / "frontend" / "src" / "views" / "Clean.tsx").read_text(encoding="utf-8")
        hooks_source = (root / "frontend" / "src" / "lib" / "hooks.ts").read_text(encoding="utf-8")
        docs_source = "\n".join(
            [
                (root / "AGENTS.md").read_text(encoding="utf-8"),
                (root / "CLAUDE.md").read_text(encoding="utf-8"),
            ]
        )

        self.assertIn("jobs.start_python(", dedup_source)
        self.assertIn('"type": "dedup-scan"', dedup_source)
        self.assertIn('"type": "dedup-ai-review"', dedup_source)
        self.assertIn("source_scan_jid", dedup_source)
        self.assertIn("_dedup_raise_if_cancelled(cancel, state)", dedup_source)
        self.assertIn("_dedup_state_response(jid, state, job)", dedup_source)
        self.assertNotIn("threading.Thread(target=_run, daemon=True).start()", dedup_source)

        self.assertIn("job_status", (root / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8"))
        self.assertIn("navigate(jobsUrl(scanJid))", panel_source)
        self.assertIn("beets:jobs-changed", panel_source)
        self.assertIn("beets:jobs-changed", clean_source)
        self.assertIn("beets:jobs-changed", hooks_source)
        self.assertIn("JobStore-backed cleanup jobs", docs_source)


    def test_dedup_scan_uses_musicbrainz_index_for_library_wide_scan(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "app.py").read_text(encoding="utf-8")
        dedup_source = app_source[app_source.index('@app.post("/api/dedup/scan")'):app_source.index('@app.post("/api/dedup/cleanup")')]

        self.assertIn("mb_trackid_index: Dict[str, list] = {}", dedup_source)
        self.assertIn("mb_trackid_index.setdefault(mbid, []).append(item)", dedup_source)
        self.assertIn("results = mb_trackid_index.get(mb_trackid) or []", dedup_source)
        self.assertIn("results = mb_trackid_index.get(fid) or []", dedup_source)
        self.assertNotIn('list(lib.items([f"mb_trackid:{mb_trackid}"]))', dedup_source)
        self.assertNotIn('list(lib.items([f"mb_trackid:{fid}"]))', dedup_source)

    def test_dedup_scan_skips_stale_paths_created_by_folder_merges(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "app.py").read_text(encoding="utf-8")
        dedup_source = app_source[app_source.index('@app.post("/api/dedup/scan")'):app_source.index('@app.post("/api/dedup/cleanup")')]
        cleanup_source = app_source[app_source.index('def _maintenance_duplicate_cleanup_paths'):app_source.index('def _maintenance_full_duplicate_scan')]

        self.assertIn("source_size = src.stat().st_size", dedup_source)
        self.assertIn("except FileNotFoundError:", dedup_source)
        self.assertIn("skipped stale path", dedup_source)
        self.assertIn('"source_size":          source_size', dedup_source)
        self.assertIn("if not source.exists() or not source.is_file():", cleanup_source)
if __name__ == "__main__":
    unittest.main()
