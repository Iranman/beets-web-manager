import unittest
from pathlib import Path



ROOT = Path(__file__).resolve().parents[1]


class ImportPageNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.import_source = (ROOT / "frontend" / "src" / "views" / "Import.tsx").read_text(encoding="utf-8")
        cls.acquire_source = (
            ROOT / "frontend" / "src" / "features" / "acquisition" / "AcquisitionPanel.tsx"
        ).read_text(encoding="utf-8")
        cls.intake_source = (
            ROOT / "frontend" / "src" / "features" / "intake" / "IntakePanel.tsx"
        ).read_text(encoding="utf-8")
        cls.review_source = (
            ROOT / "frontend" / "src" / "features" / "importReview" / "ImportReviewPage.tsx"
        ).read_text(encoding="utf-8")
        cls.jobs_source = (ROOT / "frontend" / "src" / "views" / "Jobs.tsx").read_text(encoding="utf-8")

    def test_import_overview_metrics_drive_url_filters(self):
        self.assertIn("type SourceFilter", self.import_source)
        self.assertIn("function sourceFilterFromParam", self.import_source)
        self.assertIn("function reviewFilterFromParam", self.import_source)
        self.assertIn("function navigateTo(tabId: TabId", self.import_source)
        self.assertIn("function syncAcquireSource(nextSource: SourceFilter)", self.import_source)
        self.assertIn("nextParams.set('tab', 'acquire')", self.import_source)
        self.assertIn("nextParams.set('source', nextSource)", self.import_source)
        self.assertIn("function syncReviewFilter(nextFilter: QueueFilter)", self.import_source)
        self.assertIn("nextParams.set('filter', nextFilter)", self.import_source)
        self.assertIn("{ id: 'intake', label: 'Import Source' }", self.import_source)
        self.assertIn("<IntakePanel onJobStarted={() => loadSummary(false, true)} />", self.import_source)
        self.assertIn(
            "<AcquisitionPanel initialSourceFilter={sourceParam} onSourceFilterChange={syncAcquireSource} />",
            self.import_source,
        )
        self.assertIn(
            "<ImportReviewPage active={TABS[selectedIndex]?.id === 'review'} initialFilter={filterParam} onFilterChange={syncReviewFilter} />",
            self.import_source,
        )

    def test_mounted_import_panels_sync_when_url_focus_changes(self):
        self.assertIn("export type SourceFilter = 'all' | QueueSource", self.acquire_source)
        self.assertIn("initialSourceFilter = 'all'", self.acquire_source)
        self.assertIn("setSourceFilter(initialSourceFilter)", self.acquire_source)
        self.assertIn("}, [initialSourceFilter]);", self.acquire_source)
        self.assertIn("setFilter(initialFilter)", self.review_source)
        self.assertIn("}, [initialFilter]);", self.review_source)

    def test_top_level_import_owns_source_preview_and_review_queue(self):
        self.assertIn("Preview Import All", self.intake_source)
        self.assertIn("Import All", self.intake_source)
        self.assertIn("Preview is read-only", self.intake_source)
        self.assertIn("Unsafe matches stay in Review", self.intake_source)
        self.assertNotIn("Import Job Actions", self.import_source)
        self.assertIn("Retry failed imports", self.intake_source)
        self.assertIn("retryLibraryImportAllFailed", self.intake_source)
        self.assertNotIn("Retry failed imports", self.import_source)
        self.assertNotIn("Place playlist imports", self.import_source)
        self.assertNotIn("cleanupPlaylistQuality", self.import_source)

    def test_jobs_does_not_render_import_workflow_controls(self):
        self.assertNotIn("{ id: 'import', label: 'Import' }", self.jobs_source)
        self.assertNotIn("ImportPanel", self.jobs_source)
        self.assertNotIn("Import Review is on the Import page", self.jobs_source)
        self.assertNotIn("Open Import Source", self.jobs_source)
        self.assertNotIn("Open Import Review", self.jobs_source)
        self.assertIn("Submission Queue", self.jobs_source)
        self.assertIn('href="/import?tab=review"', self.jobs_source)

        self.assertNotIn("Retry failed imports", self.jobs_source)
        self.assertNotIn("Place playlist imports", self.jobs_source)
        self.assertNotIn("retryLibraryImportAllFailed", self.jobs_source)
        self.assertNotIn("/api/library/import-all/retry-failed", self.jobs_source)
        self.assertNotIn("/api/playlists/quality-cleanup", self.jobs_source)
        self.assertNotIn("ImportReviewPage", self.jobs_source)


class RouterDependencyPolicyTests(unittest.TestCase):
    """Governance-only: encodes the deliberate package-structure decision
    (react-router v8, no react-router-dom, no npm alias) as a policy
    assertion. This class intentionally does NOT claim to verify routing
    behavior -- real route matching, redirects, query-param propagation,
    and NavLink destinations are covered by
    frontend/tests/router.test.tsx (Vitest + @testing-library/react,
    exercising the actual AppRoutes component under MemoryRouter), which a
    source-text assertion cannot meaningfully substitute for."""

    @classmethod
    def setUpClass(cls):
        import json
        cls.pkg = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
        cls.lockfile_text = (ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8")

    def test_react_router_dom_is_not_a_direct_dependency(self):
        self.assertNotIn("react-router-dom", self.pkg.get("dependencies", {}))
        self.assertNotIn("react-router-dom", self.pkg.get("devDependencies", {}))

    def test_no_npm_alias_for_react_router_dom_remains(self):
        # The unsupported migration shortcut this policy rejects: aliasing
        # react-router-dom to the react-router package instead of removing
        # it and updating imports.
        self.assertNotIn('"react-router-dom": "npm:react-router', self.lockfile_text)

    def test_react_router_is_pinned_to_the_patched_v8_release(self):
        self.assertEqual(self.pkg["dependencies"]["react-router"], "8.3.0")

    def test_postcss_patched_version_and_override_are_present(self):
        self.assertEqual(self.pkg["dependencies"]["postcss"], "8.5.24")
        self.assertEqual(self.pkg["overrides"]["postcss"], "8.5.24")


if __name__ == "__main__":
    unittest.main()
