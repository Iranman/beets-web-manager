import unittest
from pathlib import Path
try:  # ARCH-001: app.py module family (works under discovery and tests.<module> runs)
    from _app_ast_cache import app_family_source, app_unit_source  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_ast_cache import app_family_source, app_unit_source  # noqa: E402



class DedupJobsTests(unittest.TestCase):
    def test_dedup_scans_are_jobstore_backed(self):
        root = Path(__file__).resolve().parents[1]
        app_source = app_family_source()
        dedup_source = "".join(app_unit_source(n) for n in ("start_dedup_scan", "dedup_ai_review", "dedup_scan_status"))
        panel_source = (root / "frontend" / "src" / "features" / "dedup" / "DedupPanel.tsx").read_text(encoding="utf-8")
        clean_source = (root / "frontend" / "src" / "views" / "Clean.tsx").read_text(encoding="utf-8")
        hooks_source = (root / "frontend" / "src" / "lib" / "hooks.ts").read_text(encoding="utf-8")

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


    def test_dedup_scan_uses_musicbrainz_index_for_library_wide_scan(self):
        root = Path(__file__).resolve().parents[1]
        app_source = app_family_source()
        dedup_source = "".join(app_unit_source(n) for n in ("start_dedup_scan", "dedup_ai_review", "dedup_scan_status"))

        self.assertIn("mb_trackid_index: Dict[str, list] = {}", dedup_source)
        self.assertIn("mb_trackid_index.setdefault(mbid, []).append(item)", dedup_source)
        self.assertIn("results = mb_trackid_index.get(mb_trackid) or []", dedup_source)
        self.assertIn("results = mb_trackid_index.get(fid) or []", dedup_source)
        self.assertNotIn('list(lib.items([f"mb_trackid:{mb_trackid}"]))', dedup_source)
        self.assertNotIn('list(lib.items([f"mb_trackid:{fid}"]))', dedup_source)

    def test_dedup_scan_skips_stale_paths_created_by_folder_merges(self):
        root = Path(__file__).resolve().parents[1]
        app_source = app_family_source()
        dedup_source = "".join(app_unit_source(n) for n in ("start_dedup_scan", "dedup_ai_review", "dedup_scan_status"))
        cleanup_source = (root / "backend" / "duplicate_identity.py").read_text(encoding="utf-8")

        self.assertIn("size = src.stat().st_size", dedup_source)
        self.assertIn("except FileNotFoundError:", dedup_source)
        self.assertIn('"error": "stale path"', dedup_source)
        self.assertIn("skipped {info['error']}", dedup_source)
        self.assertIn('"source_size":          source_size', dedup_source)
        # Both copies must still exist as files (stale paths after folder merges are skipped).
        self.assertIn("if not path_under(path, root) or not path.exists() or not path.is_file():", cleanup_source)

    def test_dedup_scan_batches_io_with_a_thread_pool(self):
        # Perf fix: scanning the library against itself ("Clean All") was
        # doing a stat() + MediaFile() re-read per file even though those
        # files are already known Beets items with tags already loaded from
        # the DB. Batched, concurrent I/O for files that DO need a fresh
        # read is the other half of the fix.
        root = Path(__file__).resolve().parents[1]
        app_source = app_family_source()
        dedup_source = "".join(app_unit_source(n) for n in ("start_dedup_scan", "dedup_ai_review", "dedup_scan_status"))

        self.assertIn("from concurrent.futures import ThreadPoolExecutor", app_source)
        self.assertIn("ThreadPoolExecutor(max_workers=_DEDUP_IO_WORKERS)", dedup_source)
        self.assertIn("path_to_item: Dict[str, Any] = {}", dedup_source)
        self.assertIn("known = path_to_item.get(_path_key(src))", dedup_source)
        self.assertIn("prepared = list(pool.map(_prepare_source, batch))", dedup_source)

    def test_dedup_scan_checks_every_same_key_candidate_not_just_the_first(self):
        # Correctness fix found while batching this: MB-track-ID and
        # identical-file-size matching only ever looked at candidates[0] in
        # the index bucket. If the source file itself happened to be first
        # in its own bucket (common during a library self-scan), a real
        # duplicate elsewhere in that same bucket was silently never found.
        root = Path(__file__).resolve().parents[1]
        app_source = app_family_source()
        dedup_source = "".join(app_unit_source(n) for n in ("start_dedup_scan", "dedup_ai_review", "dedup_scan_status"))

        self.assertIn("for canonical in results:", dedup_source)
        self.assertIn("for canonical in size_index[src_size]:", dedup_source)


if __name__ == "__main__":
    unittest.main()
