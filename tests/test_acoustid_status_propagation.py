"""D5 / #252 NF-2: an AcoustID lookup that was never answered -- no key
(not_configured), a rejected key (auth_failed) or an outage (lookup_failed)
-- must reach the fingerprint comparison as its own status, never as
no_result / "AcoustID lookup returned no recording", and is never cached.

Runs the real provider path (helpers_mb.acoustid_lookup_outcome) with fpcalc
and HTTP stubbed. Synthetic data only; the key is a placeholder."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import helpers_mb

acs = irs = None


def setUpModule():
    # Import the app family at run time, not collection time: modules that
    # sort later (test_ai_batch_retry_race) must be first to import it.
    global acs, irs
    import backend.acoustid_service as acs
    import backend.import_review_service as irs

TRACKS = {"release_group": "rg-1", "tracks": [
    {"mb_trackid": "rec-1", "title": "Crossfire", "title_norm": "crossfire", "track": 1},
    {"mb_trackid": "rec-2", "title": "Space and Time", "title_norm": "space and time", "track": 2},
]}


def _http_error(code, body=None):
    """side_effect raising a fresh HTTPError per call (its body is a stream)."""
    def _raise(*_a, **_k):
        raise urllib.error.HTTPError("https://api.acoustid.org/v2/lookup", code, "err", {},
                                     io.BytesIO(json.dumps(body or {}).encode()))
    return _raise


class _Ok:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.body).encode()


class AcoustIDStatusPropagationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.cache = self.tmp / "cache"
        self.files = []
        for name in ("01 zzz.flac", "02 yyy.flac"):
            p = self.tmp / name
            p.write_bytes(b"fLaC" + os.urandom(64))
            self.files.append(str(p))
        fp = mock.Mock(returncode=0, stdout=json.dumps({"duration": 200, "fingerprint": "AQAA"}))
        for patcher in (
            mock.patch.object(acs, "_ACOUSTID_FILE_CACHE_DIR", self.cache),
            mock.patch.object(helpers_mb, "_ACOUSTID_MIN_INTERVAL_SECONDS", 0.0),
            mock.patch.object(helpers_mb.shutil, "which", return_value=self.files[0]),
            mock.patch.object(helpers_mb.subprocess, "run", return_value=fp),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _env(self, key):
        return mock.patch.dict(os.environ, {"ACOUSTID_API_KEY": key, "ACOUSTID_KEY": ""})

    def _compare(self):
        cands = [{"title": "Unrelated %d" % i, "path": p} for i, p in enumerate(self.files)]
        return irs._candidate_track_build_comparison("rel-1", TRACKS, cands)

    def _assert_not_checked(self, status):
        result = self._compare()
        self.assertEqual(result["fingerprint_status_counts"], {status: 2})
        error = result["preflight"]["error"]
        self.assertNotIn("returned no recording", error)
        self.assertIn("not checked", error)
        self.assertFalse(self.cache.exists() and any(self.cache.rglob("*.json")), "failure was cached")
        return error

    def test_no_key_is_not_configured(self):
        with self._env(""), mock.patch.object(helpers_mb.provider_boundary, "opened",
                                              side_effect=AssertionError("no network without a key")):
            self.assertIn("not configured", self._assert_not_checked("not_configured"))

    def test_alias_key_is_used_when_primary_is_unset(self):
        with mock.patch.dict(os.environ, {"ACOUSTID_API_KEY": "", "ACOUSTID_KEY": "placeholder-alias"}), \
                mock.patch.object(helpers_mb.provider_boundary, "opened", side_effect=_http_error(401)):
            self._assert_not_checked("auth_failed")

    def test_http_401_is_auth_failed(self):
        with self._env("placeholder-key"), \
                mock.patch.object(helpers_mb.provider_boundary, "opened", side_effect=_http_error(401)):
            self.assertIn("rejected the API key", self._assert_not_checked("auth_failed"))

    def test_invalid_key_error_code_is_auth_failed(self):
        body = {"status": "error", "error": {"code": 4, "message": "invalid API key"}}
        with self._env("placeholder-key"), \
                mock.patch.object(helpers_mb.provider_boundary, "opened", side_effect=_http_error(400, body)):
            self._assert_not_checked("auth_failed")

    def test_outage_is_lookup_failed(self):
        with self._env("placeholder-key"), \
                mock.patch.object(helpers_mb.provider_boundary, "opened", side_effect=_http_error(503)):
            self.assertIn("lookup failed", self._assert_not_checked("lookup_failed"))

    def test_real_empty_answer_is_still_no_result_and_cached(self):
        with self._env("placeholder-key"), \
                mock.patch.object(helpers_mb.provider_boundary, "opened",
                                  return_value=_Ok({"status": "ok", "results": []})):
            result = self._compare()
        self.assertEqual(result["fingerprint_status_counts"], {"no_result": 2})
        self.assertIn("returned no recording", result["preflight"]["error"])
        self.assertEqual(len(list(self.cache.rglob("*.json"))), 2)

    def test_audio_identity_decision_reports_not_checked_for_review(self):
        with self._env(""):
            decision = acs._audio_identity_decision(self.files[0], expected_title="Crossfire")
        self.assertEqual(decision["fingerprint_status"], "not_configured")
        self.assertEqual(decision["acoustid_status"], "not_configured")
        self.assertEqual(decision["final_action"], "review")
        self.assertNotIn("no_acoustid_result", decision["conflicts"])


if __name__ == "__main__":
    unittest.main()
