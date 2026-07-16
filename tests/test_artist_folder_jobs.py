import unittest
from pathlib import Path


class ArtistFolderJobsTests(unittest.TestCase):
    def test_artist_folder_scan_is_jobstore_backed(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "app.py").read_text(encoding="utf-8")
        scan_route = app_source[
            app_source.index('@app.post("/api/clean/artist-folders/scan")'):
            app_source.index('@app.post("/api/clean/artist-folders/merge")')
        ]
        merge_route = app_source[
            app_source.index('@app.post("/api/clean/artist-folders/merge")'):
            app_source.index("def _stamp_artist_folder_candidates")
        ]
        stamp_route = app_source[
            app_source.index('@app.post("/api/clean/artist-folders/stamp-mbid")'):
            app_source.index("# \u2500\u2500 Playlist helpers")
        ]
        panel_source = (
            root / "frontend" / "src" / "features" / "artistfolders" / "ArtistFoldersPanel.tsx"
        ).read_text(encoding="utf-8")
        client_source = (root / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
        client_scan = client_source[
            client_source.index("export function scanArtistFolders"):
            client_source.index("export function mergeArtistFolders")
        ]
        clean_source = (root / "frontend" / "src" / "views" / "Clean.tsx").read_text(encoding="utf-8")
        docs_source = "\n".join(
            [
                (root / "AGENTS.md").read_text(encoding="utf-8"),
                (root / "CLAUDE.md").read_text(encoding="utf-8"),
            ]
        )

        self.assertIn("jobs.start_python(", scan_route)
        self.assertIn('"type": "artist-folder-scan"', scan_route)
        self.assertIn('"groups": groups', scan_route)
        self.assertIn('"name_group_count": name_count', scan_route)
        self.assertIn('"type": "artist-folder-merge"', merge_route)
        self.assertIn('"type": "stamp-mbid-folders"', stamp_route)
        self.assertIn('def _stamp_artist_folder_scan', app_source)
        self.assertIn('def _replace_stamp_db_path_prefixes', app_source)
        self.assertIn('def _replace_stamp_db_exact_paths', app_source)
        self.assertIn('def _mb_artist_lookup_by_id', app_source)
        self.assertIn('_STAMP_UUID_IN_NAME_RE', app_source)
        self.assertIn('def _artist_folder_name_without_mbid', app_source)
        self.assertIn('def _safe_artist_folder_name', app_source)
        self.assertIn('def _stamp_folder_for_item_path', app_source)
        self.assertIn('def _stamp_artist_folder_album_mbid_counts', app_source)
        self.assertIn('SELECT DISTINCT a.id AS album_id, a.mb_albumartistid, i.path', app_source)
        self.assertIn('folder_id_album_sets, folder_album_totals, scan_error = _stamp_artist_folder_album_mbid_counts(root, folders)', app_source)
        self.assertIn('album_total = int(folder_album_totals.get(folder_key) or 0)', app_source)
        self.assertIn('match_ratio = best_count / album_total if album_total else 0.0', app_source)
        self.assertIn('def _append_stamp_candidate_log', app_source)
        self.assertIn('def _append_stamp_skipped_log', app_source)
        self.assertIn('_STAMP_DB_PATH_COLUMNS', app_source)
        self.assertIn('already stamped with the canonical MB artist folder name', app_source)
        self.assertIn('"target_exists": new_path.exists()', app_source)
        self.assertIn('action = "merge into" if c.get("target_exists") else "rename to"', app_source)
        self.assertIn('"skipped_total": len(skipped)', stamp_route)
        self.assertIn('_append_stamp_candidate_log(log, candidates)', stamp_route)
        self.assertIn('_append_stamp_skipped_log(log, skipped)', stamp_route)
        self.assertIn('_append_stamp_skipped_log(log, skipped, include_examples=False)', stamp_route)
        self.assertIn('def _artist_folder_canonical_name', app_source)
        self.assertIn('return f"{base} ({mbid})"', app_source)
        self.assertIn("Example: 'Celia Cruz' -> 'Celia Cruz (7b8e1188-...)'", stamp_route)
        self.assertIn('compact_log = bool(payload.get("compact_log", False))', stamp_route)
        self.assertIn('_merge_artist_dir_contents(', stamp_route)
        self.assertIn('verbose_files=not compact_log', stamp_route)
        self.assertIn('artwork_collisions_resolved', stamp_route)
        self.assertIn('_replace_stamp_db_exact_paths(con, "items", "path", moves)', stamp_route)
        self.assertIn('_replace_stamp_db_exact_paths(con, "albums", "path", moves)', stamp_route)
        self.assertIn('_replace_stamp_db_exact_paths(con, "albums", "artpath", moves)', stamp_route)
        self.assertIn('_replace_stamp_db_path_prefixes(con, "items", "path", prefix_pairs)', stamp_route)
        self.assertIn('_replace_stamp_db_path_prefixes(con, "albums", "path", prefix_pairs)', stamp_route)
        self.assertIn('_replace_stamp_db_path_prefixes(con, "albums", "artpath", prefix_pairs)', stamp_route)
        self.assertIn('DB updated: {updated_items} item path(s), {updated_album_paths} album path(s), {updated_albums} album artpath(s)', stamp_route)
        self.assertIn('"merged": merged', stamp_route)
        self.assertNotIn("Skip (target exists)", stamp_route)

        self.assertIn("Promise<JobStartResponse>", client_scan)
        self.assertIn("useJobPoll(scanJobId)", panel_source)
        self.assertIn("getJobResult<ArtistFolderScanResponse>", panel_source)
        self.assertIn("navigate(jobsUrl(activeJobId))", panel_source)
        self.assertIn("JobStatusCard job={scanJob}", panel_source)
        self.assertIn("beets:jobs-changed", panel_source)
        self.assertIn("CLEAN_JOB_TAB_RULES", clean_source)
        self.assertIn("artist-folder-scan", clean_source)
        self.assertIn("stamp-mbid-folders", clean_source)
        self.assertIn("artist-folder-scan", docs_source)
        self.assertIn("_replace_stamp_db_path_prefixes", docs_source)
        self.assertIn("_replace_stamp_db_exact_paths", docs_source)
        self.assertIn("Same-UUID folders", docs_source)
        self.assertIn("BOBBYVtv", docs_source)
        self.assertIn("_stamp_artist_folder_album_mbid_counts", docs_source)
        self.assertIn("distinct album IDs", docs_source)
        self.assertIn("_append_stamp_candidate_log", docs_source)
        self.assertIn("_append_stamp_skipped_log", docs_source)
        self.assertIn("Same-ID folders merge", panel_source)


if __name__ == "__main__":
    unittest.main()
