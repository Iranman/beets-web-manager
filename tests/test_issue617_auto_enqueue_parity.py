"""#617: POST /api/import-review/auto-enqueue applies the ready-batch's
readiness checks (status, mb_valid, trusted stored path), and the reconcile
route resolves the client path against stored pending Review items before it
touches idempotency state or Review status."""
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


class _Base(unittest.TestCase):
    trusted = True

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.folder = root / "downloads" / "Fake Artist - Fake Album"
        self.folder.mkdir(parents=True)
        (self.folder / "01.flac").write_bytes(b"fake")
        self.store = root / "ai_pending_review.json"
        self.write_item()
        patches = [
            (prs, "_AI_PENDING_FILE", {"new": self.store}),
            (isvc, "_pending_review_matches", {"return_value": True}),
            (isvc, "_import_review_selected_source_files", {"return_value": [self.folder / "01.flac"]}),
            (isvc, "_cached_import_target_preview", {"return_value": {"safe": True, "tracks_to_import_count": 1}}),
        ]
        if self.trusted:
            patches.append((routes_import, "_trusted_stored_review_path", {"side_effect": lambda p: str(p)}))
        for target, name, kw in patches:
            patcher = mock.patch.object(target, name, **kw)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.client = app_module.app.test_client()

    def write_item(self, status="review_required", mb_valid=True):
        self.store.write_text(json.dumps([{
            "path": str(self.folder), "status": status,
            "suggestion": {"mb_releasegroupid": RG, "mb_albumid": REL, "mb_valid": mb_valid,
                           "confidence_score": 0.95, "identity_validated": True},
            "evidence": {}}]))

    def enqueue(self):
        with mock.patch.object(routes_import, "_import_review_start_auto_import",
                               return_value={"ok": True, "queued": True}) as started:
            resp = self.client.post("/api/import-review/auto-enqueue", headers=_CSRF,
                                    json={"path": str(self.folder)})
        return resp, started


class AutoEnqueueReadinessParityTests(_Base):
    def test_ready_item_still_queues(self):
        resp, started = self.enqueue()
        self.assertEqual(resp.status_code, 200, resp.get_json())
        started.assert_called_once()

    def test_blocked_status_is_refused(self):
        self.write_item(status="blocked")
        resp, started = self.enqueue()
        self.assertEqual(resp.status_code, 409, resp.get_json())
        self.assertFalse(resp.get_json()["queued"])
        started.assert_not_called()

    def test_mb_invalid_suggestion_is_refused(self):
        self.write_item(mb_valid=False)
        resp, started = self.enqueue()
        self.assertEqual(resp.status_code, 409, resp.get_json())
        started.assert_not_called()


class AutoEnqueueUntrustedPathTests(_Base):
    trusted = False  # the temp folder is outside every approved import root

    def test_untrusted_stored_path_is_refused(self):
        resp, started = self.enqueue()
        self.assertEqual(resp.status_code, 400, resp.get_json())
        self.assertFalse(resp.get_json()["queued"])
        started.assert_not_called()


class ReconcileResolvesStoredItemTests(_Base):
    def test_unknown_path_is_404_and_touches_nothing(self):
        other = self.folder.parent / "Not In Review"
        other.mkdir()
        with mock.patch.object(routes_import, "_mark_pending_review_status") as mark, \
                mock.patch.object(routes_import, "_import_review_auto_update") as update:
            resp = self.client.post("/api/import-review/auto-enqueue/reconcile", headers=_CSRF, json={
                "path": str(other), "idempotency_key": "forged"})
        self.assertEqual(resp.status_code, 404, resp.get_json())
        mark.assert_not_called()
        update.assert_not_called()

    def test_client_idempotency_key_is_ignored(self):
        self.write_item(status="blocked")  # ineligible, so no computed key
        with mock.patch.object(routes_import, "_mark_pending_review_status") as mark, \
                mock.patch.object(routes_import, "_import_review_auto_job_for_submission",
                                  return_value=None) as lookup:
            resp = self.client.post("/api/import-review/auto-enqueue/reconcile", headers=_CSRF, json={
                "path": str(self.folder), "idempotency_key": "forged",
                "selected_source_files": ["/elsewhere/x.flac"]})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        payload, key = lookup.call_args.args
        self.assertNotEqual(key, "forged")
        self.assertNotIn("/elsewhere/x.flac", json.dumps(payload))
        self.assertNotEqual(mark.call_args.kwargs.get("idempotency_key"), "forged")
        self.assertEqual(mark.call_args.args[0], str(self.folder))


if __name__ == "__main__":
    unittest.main()
