"""Provider boundary (ARCH-006) and its production callers: the AcoustID
lookup + cache and the MusicBrainz release tracklist. A provider that
cannot be asked is never reported -- or cached -- as "no match"."""

import io
import json
import socket
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import backend.acoustid_service as acoustid_service
import backend.matching_service as matching_service
import helpers_mb
from backend.provider_boundary import (
    ProviderError, ProviderOutcome, ProviderResult, call_with_retry, classify_exception, redact,
)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, body=b"{}", retry_after=None):
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return urllib.error.HTTPError("https://api.acoustid.org/v2/lookup?client=SECRET", code, "err", headers,
                                  io.BytesIO(body))


class BoundaryTests(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(classify_exception(_http_error(401)).outcome, ProviderOutcome.AUTHENTICATION_ERROR)
        self.assertEqual(classify_exception(_http_error(429, retry_after=7)).retry_after, 7.0)
        self.assertEqual(classify_exception(_http_error(503)).outcome, ProviderOutcome.UNAVAILABLE)
        self.assertEqual(classify_exception(socket.timeout()).outcome, ProviderOutcome.TRANSIENT_ERROR)
        self.assertEqual(classify_exception(urllib.error.URLError("dns")).outcome, ProviderOutcome.UNAVAILABLE)
        self.assertEqual(classify_exception(ValueError("bad json")).outcome, ProviderOutcome.TRANSIENT_ERROR)

    def test_retries_are_bounded_and_honour_retry_after(self):
        sleeps, calls = [], []

        def flaky():
            calls.append(1)
            raise _http_error(429, retry_after=5)

        res = call_with_retry("x", flaky, max_attempts=3, sleep=sleeps.append)
        self.assertEqual((res.outcome, res.attempts, len(calls)), (ProviderOutcome.RATE_LIMITED, 3, 3))
        self.assertEqual(sleeps, [5.0, 5.0])

    def test_retry_after_is_capped_and_auth_errors_are_not_retried(self):
        sleeps = []
        call_with_retry("x", lambda: (_ for _ in ()).throw(_http_error(503, retry_after=3600)),
                        max_attempts=2, sleep=sleeps.append)
        self.assertEqual(sleeps, [30.0])
        calls = []

        def auth():
            calls.append(1)
            raise _http_error(401)

        self.assertEqual(call_with_retry("x", auth, sleep=lambda s: None).outcome, ProviderOutcome.AUTHENTICATION_ERROR)
        self.assertEqual(len(calls), 1)

    def test_success_after_a_transient_error(self):
        state = {"n": 0}

        def once():
            state["n"] += 1
            if state["n"] == 1:
                raise socket.timeout()
            return ProviderResult("x", ProviderOutcome.CONFIRMED, data=[1])

        res = call_with_retry("x", once, sleep=lambda s: None)
        self.assertEqual((res.outcome, res.attempts), (ProviderOutcome.CONFIRMED, 2))

    def test_redaction(self):
        self.assertNotIn("SECRET", redact("GET /v2/lookup?client=SECRET&fingerprint=AQAD"))
        self.assertNotIn("abcdef123456", redact("Authorization: Bearer abcdef123456"))
        self.assertEqual(redact({"api_key": "k", "nested": ["token=zzzzzz"]}),
                         {"api_key": "[REDACTED]", "nested": ["token=[REDACTED]"]})
        self.assertNotIn("SECRET", str(ProviderError(ProviderOutcome.UNAVAILABLE, "failed client=SECRET")))


class AcoustIdCallerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.audio = Path(self._tmp.name) / "a.flac"
        self.audio.write_bytes(b"audio")
        fp = mock.MagicMock(returncode=0, stdout=json.dumps({"duration": 120, "fingerprint": "AQAD"}))
        for p in (mock.patch.object(helpers_mb.shutil, "which", return_value=__file__),
                  mock.patch.object(helpers_mb.subprocess, "run", return_value=fp),
                  mock.patch.object(helpers_mb, "_ACOUSTID_MIN_INTERVAL_SECONDS", 0),
                  mock.patch("backend.provider_boundary.time.sleep"),
                  mock.patch.object(acoustid_service, "_ACOUSTID_FILE_CACHE_DIR", Path(self._tmp.name) / "cache")):
            p.start()
            self.addCleanup(p.stop)

    def _lookup(self, side_effect):
        with mock.patch.object(helpers_mb._ur, "urlopen", side_effect=side_effect):
            return acoustid_service._acoustid_lookup_cached_outcome(str(self.audio))

    def _cached(self):
        return acoustid_service._acoustid_cached_fingerprint_ids(str(self.audio))

    def test_outages_are_typed_and_never_cached(self):
        for effect, outcome in (
            (_http_error(503), ProviderOutcome.UNAVAILABLE),
            (_http_error(429, retry_after=1), ProviderOutcome.RATE_LIMITED),
            (_http_error(400, json.dumps({"status": "error", "error": {"code": 4}}).encode()),
             ProviderOutcome.AUTHENTICATION_ERROR),
            (socket.timeout(), ProviderOutcome.TRANSIENT_ERROR),
            (lambda *a, **k: _Resp(json.dumps({"status": "error", "error": {"code": 14}}).encode()),
             ProviderOutcome.RATE_LIMITED),
        ):
            res = self._lookup(effect)
            self.assertEqual(res.outcome, outcome)
            self.assertFalse(res.answered)
            self.assertIsNone(self._cached(), outcome)  # an outage is NOT a cached "no match"

    def test_answers_are_cached_and_served_from_cache(self):
        body = {"status": "ok", "results": [{"score": 0.97, "id": "a", "recordings": [{"id": "rec-1"}]}]}
        res = self._lookup(lambda *a, **k: _Resp(json.dumps(body).encode()))
        self.assertEqual((res.outcome, res.from_cache), (ProviderOutcome.CONFIRMED, False))
        self.assertEqual(self._cached(), ["rec-1"])
        again = self._lookup(AssertionError("must not call the provider again"))
        self.assertEqual((again.outcome, again.from_cache), (ProviderOutcome.CONFIRMED, True))

    def test_a_real_empty_answer_is_no_result(self):
        res = self._lookup(lambda *a, **k: _Resp(json.dumps({"status": "ok", "results": []}).encode()))
        self.assertEqual(res.outcome, ProviderOutcome.NO_RESULT)
        self.assertEqual(self._cached(), [])

    def test_missing_fpcalc_is_unavailable(self):
        with mock.patch.object(helpers_mb.shutil, "which", return_value=None), \
                mock.patch.object(helpers_mb, "Path") as path_cls:
            path_cls.return_value.exists.return_value = False
            res = helpers_mb.acoustid_lookup_outcome(str(self.audio))
        self.assertEqual(res.outcome, ProviderOutcome.UNAVAILABLE)

    def test_compat_list_api_is_unchanged(self):
        body = {"status": "ok", "results": [{"score": 1.0, "id": "a", "recordings": [{"id": "rec-9"}]}]}
        with mock.patch.object(helpers_mb._ur, "urlopen", return_value=_Resp(json.dumps(body).encode())):
            self.assertEqual([c["mb_trackid"] for c in helpers_mb._acoustid_lookup(str(self.audio))], ["rec-9"])


class MusicBrainzCallerTests(unittest.TestCase):
    REL = "45347542-db98-422a-a307-ae95d5371f60"

    def setUp(self):
        for p in (mock.patch.object(matching_service, "_mb_release_tracklist_read_disk", return_value=None),
                  mock.patch.object(matching_service, "_mb_release_tracklist_write_disk"),
                  mock.patch("backend.provider_boundary.time.sleep")):
            p.start()
            self.addCleanup(p.stop)
        matching_service._MB_RELEASE_TRACKLIST_CACHE.clear()

    def test_outage_is_reported_as_an_outage(self):
        with mock.patch.object(matching_service._ur, "urlopen", side_effect=_http_error(503)) as op:
            res = matching_service._fetch_mb_release_tracklist(self.REL)
        self.assertEqual((res["ok"], res["outcome"]), (False, "unavailable"))
        self.assertEqual(op.call_count, 3)  # bounded retries

    def test_missing_release_is_no_result(self):
        with mock.patch.object(matching_service._ur, "urlopen", side_effect=_http_error(404)):
            self.assertEqual(matching_service._fetch_mb_release_tracklist(self.REL)["outcome"], "no_result")

    def test_confirmed_tracklist(self):
        body = {"media": [{"position": 1, "tracks": [{"position": 13, "title": "Parmesan",
                                                      "recording": {"id": "rec-13"}}]}]}
        with mock.patch.object(matching_service._ur, "urlopen", return_value=_Resp(json.dumps(body).encode())):
            res = matching_service._fetch_mb_release_tracklist(self.REL)
        self.assertEqual((res["outcome"], res["tracks"][0]["mb_trackid"]), ("confirmed", "rec-13"))


if __name__ == "__main__":
    unittest.main()
