"""Tests for POST /api/ai-batch/reconcile-artwork -- the backend
reconciliation endpoint the Intake UI calls after a manual artwork-retry job
(POST /api/albums/<aid>/fetch-art) reaches a terminal state. Job creation
alone never proves success: this endpoint re-reads the album and verifies
real on-disk art via _album_art_status(), the same check the rest of the
art-repair system uses, before ever clearing artwork_retryable on the
persisted AI-batch folder state.

Same isolated-temp-environment pattern as
tests/test_post_retag_artwork_integration.py (correct env var names --
BEETS_LIBRARY, not the never-read LIB_PATH -- so importing app.py cannot
touch any real path on this machine).
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="beets_artwork_retry_reconciliation_"))
unittest.addModuleCleanup(shutil.rmtree, str(_TMP_ROOT), ignore_errors=True)

_ENV_OVERRIDES = {
    "BEETSDIR": str(_TMP_ROOT / "config"),
    "BEETS_CONFIG": str(_TMP_ROOT / "config" / "config.yaml"),
    "BEETS_LIBRARY": str(_TMP_ROOT / "config" / "musiclibrary.blb"),
    "BEETS_LOG": str(_TMP_ROOT / "config" / "beet.log"),
    "AI_BATCH_STATE_DIR": str(_TMP_ROOT / "ai_batch_jobs"),
    "METADATA_CACHE_DIR": str(_TMP_ROOT / "cache"),
    "BEETS_TRANSACTION_DIR": str(_TMP_ROOT / "transactions"),
    "BEETS_WEB_AUTH_TOKEN_FILE": str(_TMP_ROOT / "config" / ".auth_token"),
    "BEETS_WEB_AUTH_DISABLED": "1",
}
(_TMP_ROOT / "config").mkdir(parents=True, exist_ok=True)
_env_patcher = mock.patch.dict(os.environ, _ENV_OVERRIDES, clear=False)
_env_patcher.start()
unittest.addModuleCleanup(_env_patcher.stop)


def setUpModule():
    os.environ.update(_ENV_OVERRIDES)


def _import_app():
    sys.path.insert(0, str(ROOT))
    import app as app_module
    return app_module


try:
    APP = _import_app()
    _APP_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment-dependent
    APP = None
    _APP_IMPORT_ERROR = exc


@unittest.skipIf(APP is None, f"app.py could not be imported: {_APP_IMPORT_ERROR}")
class ArtworkRetryReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.client = APP.app.test_client()
        self.batch_job_id = "reconcile-test-batch"
        self.state = APP._ai_batch_initial_state(self.batch_job_id, "/data/torrents/music")
        self.state["folder_states"] = {
            "f1": {
                "folder_id": "f1", "batch_job_id": self.batch_job_id,
                "source_folder": "/data/torrents/music/incidents",
                "status": "imported", "current_step": "imported",
                "album_id": 1,
                "metadata_imported": True, "identity_verified": True,
                "artwork_status": "failed", "artwork_retryable": True,
            },
        }
        APP._ai_batch_write_state(self.state)
        self.addCleanup(self._cleanup_state_file)

    def _cleanup_state_file(self):
        try:
            (APP._AI_BATCH_STATE_DIR / f"{self.batch_job_id}.json").unlink()
        except Exception:
            pass

    def _post(self, **payload):
        body = {"batch_job_id": self.batch_job_id, "folder_id": "f1"}
        body.update(payload)
        return self.client.post("/api/ai-batch/reconcile-artwork", json=body)

    def test_reconciles_to_fetched_when_art_now_present(self):
        with mock.patch.object(APP, "_album_art_status", return_value={"has_local_art": True, "local_art_path": "/x/cover.jpg"}):
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        folder = next(f for f in body["state"]["folders"] if f["folder_id"] == "f1")
        self.assertEqual(folder["artwork_status"], "fetched")
        self.assertFalse(folder["artwork_retryable"])

    def test_reconciles_to_failed_when_art_still_missing(self):
        with mock.patch.object(APP, "_album_art_status", return_value={"has_local_art": False}):
            resp = self._post()
        body = resp.get_json()
        folder = next(f for f in body["state"]["folders"] if f["folder_id"] == "f1")
        self.assertEqual(folder["artwork_status"], "failed")
        self.assertTrue(folder["artwork_retryable"])

    def test_does_not_trust_a_client_claimed_outcome(self):
        # A client-supplied "status"/"artwork_present" field must be
        # completely ignored -- only the real _album_art_status() re-read
        # decides the outcome.
        with mock.patch.object(APP, "_album_art_status", return_value={"has_local_art": False}):
            resp = self._post(status="fetched", artwork_present=True, artwork_status="fetched")
        body = resp.get_json()
        folder = next(f for f in body["state"]["folders"] if f["folder_id"] == "f1")
        self.assertEqual(folder["artwork_status"], "failed")
        self.assertTrue(folder["artwork_retryable"])

    def test_missing_album_id_reports_skipped_no_album(self):
        self.state["folder_states"]["f1"]["album_id"] = None
        APP._ai_batch_write_state(self.state)
        with mock.patch.object(APP, "_album_art_status") as status_check:
            resp = self._post()
        status_check.assert_not_called()
        body = resp.get_json()
        folder = next(f for f in body["state"]["folders"] if f["folder_id"] == "f1")
        self.assertEqual(folder["artwork_status"], "skipped_no_album")
        self.assertFalse(folder["artwork_retryable"])

    def test_unknown_folder_id_returns_404(self):
        resp = self._post(folder_id="does-not-exist")
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(resp.get_json()["ok"])

    def test_unknown_batch_returns_404(self):
        resp = self.client.post("/api/ai-batch/reconcile-artwork", json={"batch_job_id": "no-such-batch", "folder_id": "f1"})
        self.assertEqual(resp.status_code, 404)

    def test_missing_folder_id_is_a_400(self):
        resp = self.client.post("/api/ai-batch/reconcile-artwork", json={"batch_job_id": self.batch_job_id})
        self.assertEqual(resp.status_code, 400)

    def test_reconciled_state_persists_across_reload(self):
        with mock.patch.object(APP, "_album_art_status", return_value={"has_local_art": True}):
            self._post()
        reloaded = APP._ai_batch_find_state(self.batch_job_id)
        folder = reloaded["folder_states"]["f1"]
        self.assertEqual(folder["artwork_status"], "fetched")
        self.assertFalse(folder["artwork_retryable"])

    # ---- security / redaction ------------------------------------------

    def test_response_never_leaks_injected_secret_values(self):
        secret = "sk-should-not-appear"
        with mock.patch.object(APP, "_album_art_status", side_effect=RuntimeError(f"api_key={secret}")):
            resp = self._post()
        rendered = json.dumps(resp.get_json())
        self.assertNotIn(secret, rendered)
        reloaded = APP._ai_batch_find_state(self.batch_job_id)
        self.assertNotIn(secret, json.dumps(reloaded))


if __name__ == "__main__":
    unittest.main()
