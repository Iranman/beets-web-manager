import unittest
from pathlib import Path


class ImportReviewOriginFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.app_source = (cls.root / "app.py").read_text(encoding="utf-8")
        cls.types_source = (cls.root / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")
        cls.client_source = (cls.root / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
        cls.review_source = (
            cls.root / "frontend" / "src" / "features" / "importReview" / "ImportReviewPage.tsx"
        ).read_text(encoding="utf-8")

    def test_review_item_origin_model_and_counts(self):
        self.assertIn("export type ReviewOriginType =", self.types_source)
        for value in [
            "'playlist'",
            "'batch_import'",
            "'manual_import'",
            "'downloads'",
            "'missing_track_acquisition'",
            "'cleanup_leftover'",
            "'unknown'",
        ]:
            self.assertIn(value, self.types_source)
        for field in [
            "origin_type?: ReviewOriginType",
            "origin_label?: string",
            "origin_id?: string",
            "source_playlist_id?: string",
            "source_playlist_name?: string",
            "source_batch_id?: string",
            "source_folder?: string",
            "created_by_workflow?: string",
            "origin_counts?: ReviewOriginCounts",
        ]:
            self.assertIn(field, self.types_source)

    def test_backend_supports_origin_filtering_and_counts(self):
        self.assertIn("REVIEW_ORIGIN_TYPES", self.app_source)
        self.assertIn("def _normalize_review_origin_type", self.app_source)
        self.assertIn("def _review_origin_payload", self.app_source)
        self.assertIn("request.args.get(\"origin_type\")", self.app_source)
        self.assertIn("request.args.get(\"status\")", self.app_source)
        self.assertIn("request.args.get(\"search\")", self.app_source)
        self.assertIn("request.args.get(\"evidence_only\")", self.app_source)
        self.assertIn("origin_counts = Counter(_review_item_origin_type(r) for r in status_rows)", self.app_source)
        self.assertIn('"origin_counts": dict(origin_counts)', self.app_source)
        self.assertIn("if origin_filter != \"all\":", self.app_source)

    def test_playlist_and_download_paths_backfill_origin(self):
        self.assertIn("PLAYLIST_DOWNLOAD_ROOT.resolve", self.app_source)
        self.assertIn('"origin_type": "playlist"', self.app_source)
        self.assertIn('"source_playlist_name": rel_name', self.app_source)
        self.assertIn('"created_by_workflow": "playlist_import"', self.app_source)
        self.assertIn('"origin_type": "downloads"', self.app_source)

    def test_batch_and_manual_writers_persist_origin(self):
        self.assertIn('"origin_type": "batch_import"', self.app_source)
        self.assertIn('"source_batch_id": batch_id', self.app_source)
        self.assertIn('"created_by_workflow": "ai_batch_import"', self.app_source)
        self.assertIn('"origin_type": "manual_import"', self.app_source)
        self.assertIn("added = _add_to_pending(folder_path, suggestion, allow_existing=allow_existing, origin=origin)", self.app_source)

    def test_client_accepts_review_queue_origin_params(self):
        self.assertIn("ReviewQueueParams", self.client_source)
        self.assertIn("origin_type", self.client_source)
        self.assertIn("evidence_only", self.client_source)
        self.assertIn("new URLSearchParams", self.client_source)
        self.assertIn("getReviewQueue({ limit: REVIEW_SUMMARY_LIMIT })", (self.root / "frontend" / "src" / "views" / "Import.tsx").read_text(encoding="utf-8"))

    def test_review_ui_has_source_filter_row_and_badges(self):
        self.assertIn("const originFilters", self.review_source)
        self.assertIn("{ id: 'playlist', label: 'Playlist' }", self.review_source)
        self.assertIn("{ id: 'batch_import', label: 'Batch' }", self.review_source)
        self.assertIn("{ id: 'manual_import', label: 'Manual' }", self.review_source)
        self.assertIn("{ id: 'downloads', label: 'Downloads' }", self.review_source)
        self.assertIn("{ id: 'missing_track_acquisition', label: 'Missing Tracks' }", self.review_source)
        self.assertIn("{ id: 'cleanup_leftover', label: 'Cleanup Leftovers' }", self.review_source)
        self.assertIn("function itemMatchesSourceFilter", self.review_source)
        self.assertIn("if (!itemMatchesSourceFilter(item, sourceFilter)) return false", self.review_source)
        self.assertIn("if (entry.id !== 'all' && count === 0 && !active) return null", self.review_source)
        self.assertIn("Active: {activeFilterSummary}", self.review_source)
        self.assertIn("Clear filters", self.review_source)
        self.assertIn("{originDisplay}", self.review_source)

    def test_leftover_actions_preserve_origin_metadata(self):
        self.assertIn("function suggestionWithOrigin", self.review_source)
        self.assertIn("origin_type: itemOriginType(item)", self.review_source)
        self.assertIn("source_playlist_name: item.source_playlist_name", self.review_source)
        self.assertIn("source_batch_id: item.source_batch_id", self.review_source)
        self.assertIn("ai_suggestion: suggestionWithOrigin(item", self.review_source)


if __name__ == "__main__":
    unittest.main()
