import unittest
from pathlib import Path
try:  # ARCH-001: app.py module family (works under discovery and tests.<module> runs)
    from _app_ast_cache import app_family_source  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_ast_cache import app_family_source  # noqa: E402



class ArtistFolderJobsTests(unittest.TestCase):
    def test_artist_folder_scan_is_jobstore_backed(self):
        root = Path(__file__).resolve().parents[1]
        app_source = app_family_source()
        scan_route = app_source[
            app_source.index('@app.post("/api/clean/artist-folders/scan")'):
            app_source.index('@app.post("/api/clean/artist-folders/merge")')
        ]
        merge_route = app_source[
            app_source.index('@app.post("/api/clean/artist-folders/merge")'):
            # (was: def _stamp_artist_folder_candidates, removed by the W2
            # dead-code sweep; the merge route is directly followed by the
            # stamp-mbid route in routes_cleanup.py)
            app_source.index('@app.post("/api/clean/artist-folders/stamp-mbid")')
        ]
        stamp_route = app_source[
            app_source.index('@app.post("/api/clean/artist-folders/stamp-mbid")'):
            app_source.index("# \u2500\u2500 Playlist helpers")
        ]
        # The apply job is the shared Clean All step, so the stamp flow is the
        # route plus that step.
        self.assertIn("_maintenance_artist_folder_merge_step(", stamp_route)
        maintenance_source = (root / "backend" / "maintenance_service.py").read_text(encoding="utf-8")
        stamp_route += maintenance_source[
            maintenance_source.index("def _maintenance_artist_folder_merge_step("):
            maintenance_source.index("def _maintenance_running_job(")
        ]
        panel_source = (
            root / "frontend" / "src" / "features" / "artistfolders" / "ArtistFoldersPanel.tsx"
        ).read_text(encoding="utf-8")
        client_source = (root / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
        client_scan = client_source[
            client_source.index("export function scanArtistFolders"):
            client_source.index("export function mergeArtistFolders")
        ]
        jobs_source = (root / "frontend" / "src" / "views" / "Jobs.tsx").read_text(encoding="utf-8")

        self.assertIn("jobs.start_python(", scan_route)
        self.assertIn('"type": "artist-folder-scan"', scan_route)
        self.assertIn('"groups": groups', scan_route)
        self.assertIn('"name_group_count": name_count', scan_route)
        self.assertIn('"type": "artist-folder-merge"', merge_route)
        self.assertIn('"type": "stamp-mbid-folders"', stamp_route)
        self.assertIn('def _stamp_artist_folder_scan', app_source)
        self.assertIn('def _mb_artist_lookup_by_id', app_source)
        self.assertIn('_STAMP_UUID_IN_NAME_RE', app_source)
        self.assertIn('def _artist_folder_name_without_mbid', app_source)
        self.assertIn('def _safe_artist_folder_name', app_source)
        self.assertIn('def _stamp_folder_for_item_path', app_source)
        self.assertIn('def _stamp_artist_folder_album_mbid_counts', app_source)
        self.assertIn('rows = _artist_folder_album_rows()', app_source)
        self.assertIn('folder_id_album_sets, folder_album_totals, scan_error = _stamp_artist_folder_album_mbid_counts(root, folders)', app_source)
        self.assertIn('album_total = int(folder_album_totals.get(folder_key) or 0)', app_source)
        self.assertIn('match_ratio = best_count / album_total if album_total else 0.0', app_source)
        self.assertIn('def _append_stamp_candidate_log', app_source)
        self.assertIn('def _append_stamp_skipped_log', app_source)
        self.assertIn('_STAMP_DB_PATH_COLUMNS', app_source)
        self.assertIn('already stamped with the canonical MB artist folder name', app_source)
        # ARCH-020 follow-up: existence is now checked against the engine's
        # own folder inventory (existing_names, populated via
        # beets_client.get_artist_folder_inventory()) rather than a local
        # Path.exists() call -- Web Manager has no media mount in the
        # supported two-service deployment.
        self.assertIn('"target_exists": new_path.name in existing_names', app_source)
        self.assertIn('composite_workflows.get_artist_folder_inventory', app_source)
        self.assertIn('action = "merge into" if c.get("target_exists") else "rename to"', app_source)
        self.assertIn('"skipped_total": len(skipped)', stamp_route)
        self.assertIn('_append_stamp_candidate_log(log, candidates)', stamp_route)
        self.assertIn('_append_stamp_skipped_log(log, skipped)', stamp_route)
        self.assertIn('_append_stamp_skipped_log(log, skipped, include_examples=False)', stamp_route)
        self.assertIn('def _artist_folder_canonical_name', app_source)
        self.assertIn('return f"{base} ({mbid})"', app_source)
        self.assertIn("Example: 'Celia Cruz' -> 'Celia Cruz (7b8e1188-...)'", stamp_route)
        self.assertIn('compact_log = bool(payload.get("compact_log", False))', stamp_route)
        self.assertIn('composite_workflows.plan_artist_folder_reconcile', stamp_route)
        # Hotfix v0.1.17 (BUG-4): the apply step is now routed through the
        # shared resilient helper (poll-on-lost-response instead of a bare
        # apply call) rather than calling beets_client.apply_artist_folder_reconcile
        # directly here -- confirm the route delegates to it, and
        # separately confirm that helper itself still reaches the engine
        # via beets_client (no local fallback introduced).
        self.assertIn('_apply_artist_folder_reconcile_resilient', stamp_route)
        helper_source = app_source[
            app_source.index("def _apply_artist_folder_reconcile_resilient("):
            app_source.index("def _run_artist_folder_reconcile_for_alias_merge(")
        ]
        self.assertIn('composite_workflows.apply_artist_folder_reconcile', helper_source)
        self.assertNotIn("Skip (target exists)", stamp_route)

        self.assertIn("Promise<JobStartResponse>", client_scan)
        self.assertIn("useJobPoll(scanJobId)", panel_source)
        self.assertIn("getJobResult<ArtistFolderScanResponse>", panel_source)
        self.assertIn("navigate(jobsUrl(activeJobId))", panel_source)
        self.assertIn("JobStatusCard job={scanJob}", panel_source)
        self.assertIn("beets:jobs-changed", panel_source)
        # The panel is only useful if a routed view hosts it: Jobs is the only
        # host now that the legacy Clean view (and its job-to-tab rule map) is
        # gone. The job type strings themselves come from the backend job
        # record the panel polls, so reachability plus the polling wiring
        # above is the contract that has to hold.
        self.assertIn(
            "import { ArtistFoldersPanel } from '../features/artistfolders/ArtistFoldersPanel';",
            jobs_source,
        )
        self.assertIn("<ArtistFoldersPanel />", jobs_source)
        self.assertIn("Same-ID folders merge", panel_source)


if __name__ == "__main__":
    unittest.main()
