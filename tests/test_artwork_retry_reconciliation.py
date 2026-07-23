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
import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="beets_artwork_retry_reconciliation_"))
# Deliberately atexit, not unittest.addModuleCleanup -- see the full
# explanation in tests/test_post_retag_artwork_integration.py: addModuleCleanup
# stores callbacks in a single process-wide list that gets drained in full
# whenever ANY module's tests finish, not scoped to this module, which can
# delete this module's tmp root before its own tests ever run.
atexit.register(shutil.rmtree, str(_TMP_ROOT), ignore_errors=True)

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
atexit.register(_env_patcher.stop)


def setUpModule():
    os.environ.update(_ENV_OVERRIDES)


def _import_app():
    sys.path.insert(0, str(ROOT))
    import app as app_module
    return app_module


def _bind_app_globals_to_this_test_module(app_module, tmp_root: Path) -> str:
    """app.py's LIB_PATH/lib/_AI_BATCH_STATE_DIR are computed once at import
    time from os.environ. app.py is a process-wide singleton
    (sys.modules['app']): under `unittest discover`, another test module may
    import it first with its own env-var overrides, in which case env vars
    set here have zero effect on the already-bound globals -- this module's
    AI-batch state would then be written to whatever _AI_BATCH_STATE_DIR the
    first importer set up (possibly already removed by that module's own
    cleanup). Explicitly rebinding these globals to this module's own tmp
    root, regardless of import order, makes this module's behavior
    independent of what any other test module already did to the shared
    `app` singleton -- see the identical fix and full explanation in
    tests/test_post_retag_artwork_integration.py. Called again in setUp()
    before every test (not just once at import) for the same reason: a
    one-time, import-time-only rebind can still be overwritten by a sibling
    module using this identical pattern.
    """
    from beets.library import Library
    old_lib = getattr(app_module, "lib", None)
    if old_lib is not None:
        try:
            old_lib._close()
        except Exception:
            pass
    lib_path = str(tmp_root / "config" / "musiclibrary.blb")
    app_module.LIB_PATH = lib_path
    app_module.lib = Library(lib_path)
    state_dir = tmp_root / "ai_batch_jobs"
    state_dir.mkdir(parents=True, exist_ok=True)
    app_module._AI_BATCH_STATE_DIR = state_dir
    return lib_path


try:
    APP = _import_app()
    _APP_IMPORT_ERROR = None
    _bind_app_globals_to_this_test_module(APP, _TMP_ROOT)
except Exception as exc:  # pragma: no cover - environment-dependent
    APP = None
    _APP_IMPORT_ERROR = exc


@unittest.skipIf(APP is None, f"app.py could not be imported: {_APP_IMPORT_ERROR}")
class ArtworkRetryReconciliationTests(unittest.TestCase):
    def setUp(self):
        # Rebind immediately before every test -- see the identical reasoning
        # in tests/test_post_retag_artwork_integration.py's setUp: an
        # import-time-only rebind can still be overwritten by whichever
        # sibling module using this same pattern happens to import last in
        # this process, regardless of which module's tests actually run
        # first.
        _bind_app_globals_to_this_test_module(APP, _TMP_ROOT)
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
        # Default: a terminal, correctly-bound artwork job for album_id=1,
        # matching what POST /api/albums/1/fetch-art would register. Tests
        # that need a different outcome/status/album/type register their own.
        self.artwork_job_id = self._register_job(album_id=1, status="success", terminal_outcome="success")

    def _cleanup_state_file(self):
        try:
            (APP._AI_BATCH_STATE_DIR / f"{self.batch_job_id}.json").unlink()
        except Exception:
            pass

    def _register_job(self, *, album_id=1, status="success", terminal_outcome="success", job_type="album_art_repair"):
        """Directly inserts a fake terminal job into the real, shared
        JobStore -- avoids depending on PythonJob's background-thread timing
        to reach a terminal state, while still exercising the real
        jobs.get()-based lookup/validation in the route under test."""
        job_id = f"fake-artwork-job-{uuid.uuid4().hex}"
        fake_job = SimpleNamespace(
            metadata={"type": job_type, "album_id": album_id},
            status=status,
            state={"terminal_outcome": terminal_outcome},
        )
        APP.jobs._jobs[job_id] = fake_job
        self.addCleanup(APP.jobs._jobs.pop, job_id, None)
        return job_id

    def _post(self, **payload):
        body = {"batch_job_id": self.batch_job_id, "folder_id": "f1", "artwork_job_id": self.artwork_job_id}
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

    def test_reconciles_to_cancelled_when_job_was_cancelled_and_no_art(self):
        job_id = self._register_job(album_id=1, status="failed", terminal_outcome="cancelled")
        with mock.patch.object(APP, "_album_art_status", return_value={"has_local_art": False}):
            resp = self._post(artwork_job_id=job_id)
        body = resp.get_json()
        folder = next(f for f in body["state"]["folders"] if f["folder_id"] == "f1")
        self.assertEqual(folder["artwork_status"], "cancelled")
        self.assertTrue(folder["artwork_retryable"])

    def test_reconciles_to_timed_out_when_job_timed_out_and_no_art(self):
        job_id = self._register_job(album_id=1, status="failed", terminal_outcome="timed_out")
        with mock.patch.object(APP, "_album_art_status", return_value={"has_local_art": False}):
            resp = self._post(artwork_job_id=job_id)
        body = resp.get_json()
        folder = next(f for f in body["state"]["folders"] if f["folder_id"] == "f1")
        self.assertEqual(folder["artwork_status"], "timed_out")
        self.assertTrue(folder["artwork_retryable"])

    def test_art_actually_present_wins_over_a_job_that_claims_failure(self):
        # The job reports cancelled/failed, but real on-disk art verification
        # says otherwise -- the actual persisted state must always win.
        job_id = self._register_job(album_id=1, status="failed", terminal_outcome="cancelled")
        with mock.patch.object(APP, "_album_art_status", return_value={"has_local_art": True}):
            resp = self._post(artwork_job_id=job_id)
        body = resp.get_json()
        folder = next(f for f in body["state"]["folders"] if f["folder_id"] == "f1")
        self.assertEqual(folder["artwork_status"], "fetched")
        self.assertFalse(folder["artwork_retryable"])

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
        resp = self.client.post("/api/ai-batch/reconcile-artwork", json={
            "batch_job_id": "no-such-batch", "folder_id": "f1", "artwork_job_id": self.artwork_job_id,
        })
        self.assertEqual(resp.status_code, 404)

    def test_missing_batch_job_id_is_a_400(self):
        resp = self.client.post("/api/ai-batch/reconcile-artwork", json={
            "folder_id": "f1", "artwork_job_id": self.artwork_job_id,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json().get("code"), "batch_job_id_required")

    def test_missing_folder_id_is_a_400(self):
        resp = self.client.post("/api/ai-batch/reconcile-artwork", json={
            "batch_job_id": self.batch_job_id, "artwork_job_id": self.artwork_job_id,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json().get("code"), "folder_id_required")

    def test_missing_artwork_job_id_is_a_400(self):
        resp = self.client.post("/api/ai-batch/reconcile-artwork", json={
            "batch_job_id": self.batch_job_id, "folder_id": "f1",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json().get("code"), "artwork_job_id_required")

    def test_no_latest_batch_fallback_when_batch_job_id_omitted_but_present_elsewhere(self):
        # There must be no "use the latest batch" fallback at all -- an
        # explicit, correct batch_job_id is always required.
        resp = self.client.post("/api/ai-batch/reconcile-artwork", json={
            "job_id": self.batch_job_id, "folder_id": "f1", "artwork_job_id": self.artwork_job_id,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json().get("code"), "batch_job_id_required")

    def test_unknown_artwork_job_id_returns_404(self):
        resp = self._post(artwork_job_id="no-such-job")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json().get("code"), "artwork_job_not_found")

    def test_artwork_job_for_a_different_album_is_rejected(self):
        job_id = self._register_job(album_id=999, status="success", terminal_outcome="success")
        resp = self._post(artwork_job_id=job_id)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json().get("code"), "artwork_job_mismatch")

    def test_artwork_job_of_the_wrong_type_is_rejected(self):
        job_id = self._register_job(album_id=1, status="success", terminal_outcome="success", job_type="album-art-replace")
        resp = self._post(artwork_job_id=job_id)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json().get("code"), "artwork_job_mismatch")

    def test_still_running_artwork_job_is_not_yet_reconcilable(self):
        job_id = self._register_job(album_id=1, status="running", terminal_outcome="")
        with mock.patch.object(APP, "_album_art_status") as status_check:
            resp = self._post(artwork_job_id=job_id)
        status_check.assert_not_called()
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json().get("code"), "artwork_job_not_terminal")

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
