"""#604: POST /api/import-review/auto-enqueue builds its eligibility input
from the stored pending Review item, never from identity or confidence the
client asserts in the request body."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
import backend.import_service as isvc
import backend.pending_review_store as prs
import routes_import

REL = "11111111-1111-1111-1111-111111111111"
RG = "22222222-2222-2222-2222-222222222222"
_CSRF = {"Origin": "http://localhost", "X-Beets-CSRF": "1"}


class AutoEnqueueIgnoresClientIdentityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.folder = root / "downloads" / "Fake Artist - Fake Album"
        self.folder.mkdir(parents=True)
        (self.folder / "01.flac").write_bytes(b"fake")
        self.store = root / "ai_pending_review.json"
        # Stored identity is unresolved (identity_validated None).
        self.store.write_text(json.dumps([{
            "path": str(self.folder), "status": "review_required",
            "suggestion": {"mb_releasegroupid": RG, "mb_albumid": REL, "mb_valid": True,
                           "confidence_score": 0.95, "identity_validated": None},
            "evidence": {}}]))
        for target, name, kw in (
            (prs, "_AI_PENDING_FILE", {"new": self.store}),
            (isvc, "_pending_review_matches", {"return_value": True}),
            (isvc, "_import_review_selected_source_files", {"return_value": [self.folder / "01.flac"]}),
            (isvc, "_cached_import_target_preview", {"return_value": {"safe": True, "tracks_to_import_count": 1}}),
        ):
            patcher = mock.patch.object(target, name, **kw)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.client = app_module.app.test_client()

    def test_client_asserted_identity_does_not_enqueue_unresolved_item(self):
        forged = {"identity_validated": True, "confidence_score": 99}
        with mock.patch.object(routes_import, "_import_review_start_auto_import") as started:
            resp = self.client.post("/api/import-review/auto-enqueue", headers=_CSRF, json={
                "path": str(self.folder), "review_item_id": f"pending:{self.folder}",
                "mb_albumid": REL, "mb_releasegroupid": RG, **forged,
                "selected_match": {"release_group_id": RG, "representative_release_id": REL, **forged},
            })
        self.assertEqual(resp.status_code, 400, resp.get_json())
        body = resp.get_json()
        self.assertFalse(body["queued"])
        self.assertTrue(any("Artist identity unresolved" in r for r in body["eligibility"]["blocking_reasons"]))
        started.assert_not_called()

    def test_folder_without_stored_review_item_is_refused(self):
        other = self.folder.parent / "Not In Review"
        other.mkdir()
        with mock.patch.object(routes_import, "_import_review_start_auto_import") as started:
            resp = self.client.post("/api/import-review/auto-enqueue", headers=_CSRF, json={
                "path": str(other), "mb_albumid": REL, "mb_releasegroupid": RG,
                "identity_validated": True, "confidence_score": 99})
        self.assertEqual(resp.status_code, 404, resp.get_json())
        started.assert_not_called()


if __name__ == "__main__":
    unittest.main()
