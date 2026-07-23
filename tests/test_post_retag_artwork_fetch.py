"""Tests for _fetch_artwork_after_retag() -- the post-retag artwork stage
added to _ai_import_folder(). Production log showed a successful as-is
import + MusicBrainz retag repeatedly hit "fetchart plugin load error" and
never actually fetched artwork; the import still reported a plain "Done".
This function verifies the persisted MusicBrainz identity before ever
calling FetchArt (an as-is import initially lacks finalized identity), then
reuses the existing single-album _repair_album_art() repair path (the same
one POST /api/albums/<aid>/fetch-art uses) so success/failure/idempotency
verification lives in one place. These tests import the real app.py (same
isolated-temp-environment pattern as tests/test_ai_batch_retry_race.py) and
call the actual function, with only _repair_album_art and lib.get_album
mocked -- the identity-verification and status-classification logic runs
unmocked.
"""
import os
import shutil
import sys
import tempfile
import threading
import unittest
import unittest.mock as mock
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="beets_post_retag_artwork_"))
unittest.addModuleCleanup(shutil.rmtree, str(_TMP_ROOT), ignore_errors=True)

_ENV_OVERRIDES = {
    "BEETSDIR": str(_TMP_ROOT / "config"),
    "LIB_PATH": str(_TMP_ROOT / "config" / "musiclibrary.blb"),
    "AI_BATCH_STATE_DIR": str(_TMP_ROOT / "ai_batch_jobs"),
    "METADATA_CACHE_DIR": str(_TMP_ROOT / "cache"),
    "BEETS_TRANSACTION_DIR": str(_TMP_ROOT / "transactions"),
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


MB_ALBUMID = "5d2be019-9a93-4dd2-abfd-acd31b1b1e12"
OTHER_ALBUMID = "11111111-1111-1111-1111-111111111111"


@unittest.skipIf(APP is None, f"app.py could not be imported: {_APP_IMPORT_ERROR}")
class FetchArtworkAfterRetagTests(unittest.TestCase):
    def setUp(self):
        self.log = []
        self.album = SimpleNamespace(mb_albumid=MB_ALBUMID)
        self._patch(mock.patch.object(APP.lib, "get_album", side_effect=lambda aid: self.album))

    def _patch(self, patcher):
        obj = patcher.start()
        self.addCleanup(patcher.stop)
        return obj

    # ---- identity verification gate ---------------------------------------

    def test_skips_fetchart_when_identity_not_verified(self):
        self.album.mb_albumid = OTHER_ALBUMID  # persisted ID differs from what was just imported
        with mock.patch.object(APP, "_repair_album_art") as repair:
            result = APP._fetch_artwork_after_retag(1, MB_ALBUMID, self.log)
        repair.assert_not_called()
        self.assertFalse(result["identity_verified"])
        self.assertEqual(result["artwork_status"], "skipped_identity_unverified")
        self.assertFalse(result["artwork_retryable"])

    def test_skips_fetchart_when_album_missing(self):
        with mock.patch.object(APP.lib, "get_album", side_effect=lambda aid: None), \
             mock.patch.object(APP, "_repair_album_art") as repair:
            result = APP._fetch_artwork_after_retag(1, MB_ALBUMID, self.log)
        repair.assert_not_called()
        self.assertFalse(result["identity_verified"])
        self.assertEqual(result["artwork_status"], "skipped_identity_unverified")

    def test_runs_fetchart_scoped_to_album_id_when_identity_verified(self):
        with mock.patch.object(APP, "_repair_album_art", return_value={"status": "saved", "saved_path": "/x/cover.jpg"}) as repair:
            result = APP._fetch_artwork_after_retag(42, MB_ALBUMID, self.log)
        repair.assert_called_once()
        called_aid = repair.call_args[0][0]
        self.assertEqual(called_aid, 42)
        self.assertTrue(result["identity_verified"])
        self.assertEqual(result["artwork_status"], "fetched")
        self.assertFalse(result["artwork_retryable"])

    # ---- status classification ---------------------------------------------

    def test_already_present_is_not_retryable(self):
        with mock.patch.object(APP, "_repair_album_art", return_value={"status": "skipped", "source": "local"}):
            result = APP._fetch_artwork_after_retag(1, MB_ALBUMID, self.log)
        self.assertEqual(result["artwork_status"], "already_present")
        self.assertFalse(result["artwork_retryable"])

    def test_fetchart_failure_is_truthful_and_retryable(self):
        with mock.patch.object(APP, "_repair_album_art", return_value={"status": "failed", "error": "No art found by fetchart or Discogs fallback"}):
            result = APP._fetch_artwork_after_retag(1, MB_ALBUMID, self.log)
        self.assertTrue(result["identity_verified"])
        self.assertEqual(result["artwork_status"], "failed")
        self.assertTrue(result["artwork_retryable"])
        self.assertTrue(any("Artwork: failed" in line for line in self.log))

    def test_unresolved_status_is_treated_as_failed_and_retryable(self):
        with mock.patch.object(APP, "_repair_album_art", return_value={"status": "unresolved", "error": "Album folder no longer exists on disk"}):
            result = APP._fetch_artwork_after_retag(1, MB_ALBUMID, self.log)
        self.assertEqual(result["artwork_status"], "failed")
        self.assertTrue(result["artwork_retryable"])

    def test_unexpected_exception_from_repair_is_caught_and_reported_as_failed(self):
        with mock.patch.object(APP, "_repair_album_art", side_effect=RuntimeError("boom")):
            result = APP._fetch_artwork_after_retag(1, MB_ALBUMID, self.log)
        self.assertEqual(result["artwork_status"], "failed")
        self.assertTrue(result["artwork_retryable"])

    # ---- cancellation --------------------------------------------------------

    def test_cancellation_propagates_instead_of_being_swallowed(self):
        cancel_event = threading.Event()

        def fake_repair(aid, log, cancel_event=None, **kwargs):
            cancel_event.set()
            return {"status": "failed", "error": "cancelled"}

        with mock.patch.object(APP, "_repair_album_art", side_effect=fake_repair):
            with self.assertRaises(RuntimeError):
                APP._fetch_artwork_after_retag(1, MB_ALBUMID, self.log, cancel_event=cancel_event)

    # ---- redaction -------------------------------------------------------

    def test_error_text_is_redacted_before_logging(self):
        secret_error = "api_key: sk-should-not-appear"
        with mock.patch.object(APP, "_repair_album_art", return_value={"status": "failed", "error": secret_error}):
            APP._fetch_artwork_after_retag(1, MB_ALBUMID, self.log)
        combined = "\n".join(self.log)
        self.assertNotIn("sk-should-not-appear", combined)

    def test_identity_verification_exception_is_redacted(self):
        class _Boom:
            def __getattr__(self, name):
                raise RuntimeError("token=sk-must-not-leak")

        with mock.patch.object(APP.lib, "get_album", side_effect=lambda aid: _Boom()), \
             mock.patch.object(APP, "_repair_album_art") as repair:
            result = APP._fetch_artwork_after_retag(1, MB_ALBUMID, self.log)
        repair.assert_not_called()
        self.assertFalse(result["identity_verified"])
        combined = "\n".join(self.log)
        self.assertNotIn("sk-must-not-leak", combined)


if __name__ == "__main__":
    unittest.main()
