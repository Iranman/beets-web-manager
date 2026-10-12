"""QA 0.4.0 B2: fpcalc 1.5.1 (the Debian bookworm libchromaprint-tools the
runtime image installs) exits 3 with "Error decoding audio frame (End of
file)" on every file while still printing a valid JSON fingerprint.
Reproduced in the qa040 image for wav/flac/mp3/ogg/m4a; the stock Beets
container's fpcalc 1.6.0 exits 0 on the same input.

acoustid_lookup_outcome used to treat any non-zero exit as "audio could not
be fingerprinted" -> NO_RESULT, which _acoustid_lookup_cached_outcome cached
permanently. Pinned here: the JSON, not the exit code, decides; a real
fingerprint failure is "not checked" (never NO_RESULT, never cached); and
v2 cache entries (possibly poisoned) are no longer served.
"""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import helpers_mb
from backend import acoustid_service
from backend.provider_boundary import ProviderOutcome, ProviderResult

FPCALC_STDOUT = json.dumps({"duration": 40.0, "fingerprint": "AQADtEmUaEkSRZEK" * 8})
EOF_STDERR = "ERROR: Error decoding audio frame (End of file)\n"


def _run_lookup(fpcalc_result, opened=None):
    opened = opened or mock.Mock(side_effect=urllib.error.URLError("connection refused"))
    with mock.patch.object(helpers_mb, "acoustid_api_key", return_value="qa-key"), \
         mock.patch.object(helpers_mb.shutil, "which", return_value=sys.executable), \
         mock.patch.object(helpers_mb.subprocess, "run", return_value=fpcalc_result), \
         mock.patch.object(helpers_mb, "_ACOUSTID_MIN_INTERVAL_SECONDS", 0), \
         mock.patch("backend.provider_boundary.time.sleep"), \
         mock.patch.object(helpers_mb.provider_boundary, "opened", opened):
        return helpers_mb.acoustid_lookup_outcome("/music/x.flac"), opened


class FpcalcExitCodeTests(unittest.TestCase):
    def test_fingerprint_printed_with_exit_3_is_looked_up(self):
        fpcalc = subprocess.CompletedProcess(["fpcalc"], 3, stdout=FPCALC_STDOUT, stderr=EOF_STDERR)
        result, opened = _run_lookup(fpcalc)
        # The lookup reached the (failing) network, so the file was NOT checked.
        self.assertTrue(opened.called)
        self.assertNotEqual(result.outcome, ProviderOutcome.NO_RESULT, result.message)
        self.assertFalse(result.answered)

    def test_exit_3_fingerprint_is_sent_to_acoustid(self):
        fpcalc = subprocess.CompletedProcess(["fpcalc"], 3, stdout=FPCALC_STDOUT, stderr=EOF_STDERR)
        _result, opened = _run_lookup(fpcalc)
        url = opened.call_args.args[1].full_url
        self.assertIn("duration=40", url)
        self.assertIn("fingerprint=AQADtEmUaEkSRZEK", url)

    def test_failed_fpcalc_is_unavailable_not_no_result(self):
        for stdout in ("", "not json", json.dumps({"duration": 40, "fingerprint": ""}),
                       json.dumps({"duration": 0, "fingerprint": "AQAD"}), json.dumps(["x"])):
            with self.subTest(stdout=stdout):
                fpcalc = subprocess.CompletedProcess(["fpcalc"], 3, stdout=stdout, stderr=EOF_STDERR)
                result, opened = _run_lookup(fpcalc)
                self.assertEqual(result.outcome, ProviderOutcome.UNAVAILABLE)
                self.assertFalse(result.answered)
                self.assertFalse(opened.called)

    def test_fpcalc_timeout_is_not_an_answer(self):
        with mock.patch.object(helpers_mb, "acoustid_api_key", return_value="qa-key"), \
             mock.patch.object(helpers_mb.shutil, "which", return_value=sys.executable), \
             mock.patch.object(helpers_mb.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(["fpcalc"], 30)):
            result = helpers_mb.acoustid_lookup_outcome("/music/x.flac")
        self.assertFalse(result.answered)


class FingerprintFailureCacheTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.audio = self.root / "track.flac"
        self.audio.write_bytes(b"synthetic")
        self.cache_dir = self.root / "cache"
        patcher = mock.patch.object(acoustid_service, "_ACOUSTID_FILE_CACHE_DIR", self.cache_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cache_files(self):
        return list(self.cache_dir.rglob("*.json")) if self.cache_dir.exists() else []

    def test_failed_fingerprint_is_not_cached(self):
        fpcalc = subprocess.CompletedProcess(["fpcalc"], 3, stdout="", stderr=EOF_STDERR)
        with mock.patch.object(helpers_mb, "acoustid_api_key", return_value="qa-key"), \
             mock.patch.object(helpers_mb.shutil, "which", return_value=sys.executable), \
             mock.patch.object(helpers_mb.subprocess, "run", return_value=fpcalc):
            result = acoustid_service._acoustid_lookup_cached_outcome(str(self.audio))
        self.assertEqual(result.outcome, ProviderOutcome.UNAVAILABLE)
        self.assertEqual(self._cache_files(), [])

    def test_v2_cache_entry_is_not_served(self):
        path = self.audio.resolve(strict=False)
        st = path.stat()
        old_key = hashlib.sha1(f"v2|{path}|{st.st_size}|{st.st_mtime_ns}".encode("utf-8")).hexdigest()
        old_file = self.cache_dir / old_key[:2] / f"{old_key}.json"
        old_file.parent.mkdir(parents=True)
        old_file.write_text("[]", encoding="utf-8")  # a poisoned "no match"
        _p, new_key = acoustid_service._audio_cache_file_identity(str(self.audio))
        self.assertNotEqual(new_key, old_key)

        fresh = ProviderResult("acoustid", ProviderOutcome.TRANSIENT_ERROR, data=[], message="network down")
        with mock.patch.object(acoustid_service, "acoustid_lookup_outcome", return_value=fresh) as lookup:
            result = acoustid_service._acoustid_lookup_cached_outcome(str(self.audio))
        self.assertTrue(lookup.called)
        self.assertFalse(result.from_cache)
        self.assertNotEqual(result.outcome, ProviderOutcome.NO_RESULT)
        self.assertIsNone(acoustid_service._acoustid_cached_fingerprint_ids(str(self.audio)))
        self.assertTrue(old_file.exists())  # user cache files are ignored, not deleted


if __name__ == "__main__":
    unittest.main()
