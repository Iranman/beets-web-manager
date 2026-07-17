import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
ROUTES_SOURCE = (ROOT / "routes_submissions.py").read_text(encoding="utf-8")
CLIENT_SOURCE = (ROOT / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
APP_TSX_SOURCE = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
REVIEW_SOURCE = (
    ROOT / "frontend" / "src" / "features" / "importReview" / "ImportReviewPage.tsx"
).read_text(encoding="utf-8")
SUBMISSIONS_SOURCE = (ROOT / "frontend" / "src" / "views" / "Submissions.tsx").read_text(encoding="utf-8")


class MetadataSubmissionsStaticTests(unittest.TestCase):
    def test_submission_route_module_is_loaded(self):
        self.assertIn("import routes_submissions", APP_SOURCE)

    def test_acoustid_submit_routes_use_beet_submit_jobs(self):
        self.assertIn('@app.post("/api/albums/<int:aid>/acoustid-submit")', ROUTES_SOURCE)
        self.assertIn('@app.post("/api/items/<int:iid>/acoustid-submit")', ROUTES_SOURCE)
        self.assertIn('"submit"', ROUTES_SOURCE)
        self.assertIn("ACOUSTID_API_KEY", ROUTES_SOURCE)

    def test_frontend_route_and_review_handoff_exist(self):
        self.assertIn('path="submissions"', APP_TSX_SOURCE)
        self.assertIn("Submit Metadata", REVIEW_SOURCE)
        self.assertIn("navigate(`/submissions?", REVIEW_SOURCE)

    def test_submission_page_exposes_musicbrainz_and_acoustid_actions(self):
        self.assertIn("MusicBrainz and AcoustID", SUBMISSIONS_SOURCE)
        self.assertIn("Prepare Submission", SUBMISSIONS_SOURCE)
        self.assertIn("Submit Fingerprints", SUBMISSIONS_SOURCE)
        self.assertIn("Apply MBIDs", SUBMISSIONS_SOURCE)
        self.assertIn("getReviewQueue({ limit: REVIEW_LIMIT })", SUBMISSIONS_SOURCE)

    def test_client_wrappers_cover_submission_endpoints(self):
        self.assertIn("albumAcoustidSubmit", CLIENT_SOURCE)
        self.assertIn("itemAcoustidSubmit", CLIENT_SOURCE)
        self.assertIn("getAlbumMbFormat", CLIENT_SOURCE)
        self.assertIn("itemMbsubmit", CLIENT_SOURCE)

    def test_submission_post_wrappers_use_csrf_helper(self):
        for endpoint in (
            "/api/albums/${albumId}/mbsubmit",
            "/api/items/${itemId}/mbsubmit",
            "/api/albums/${albumId}/acoustid-submit",
            "/api/items/${itemId}/acoustid-submit",
        ):
            self.assertIn(f"`{endpoint}`, jsonRequest('POST')", CLIENT_SOURCE)


if __name__ == "__main__":
    unittest.main()
