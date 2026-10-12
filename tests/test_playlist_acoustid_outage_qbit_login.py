"""#297 N4: playlist placement tells an AcoustID outage from an empty answer.
#252 NF-3: a refused qBittorrent login is recorded in provider health."""
import io
import os
import unittest
from unittest import mock

os.environ.setdefault("WEB_MANAGER_DATA_DIR", "/tmp/bwm-test-data")

import backend.acquisition_service as acquisition_service  # noqa: E402
import backend.playlist_service as playlist_service  # noqa: E402
import backend.provider_boundary as provider_boundary  # noqa: E402

RG = "11111111-1111-1111-1111-111111111111"
REC = "22222222-2222-2222-2222-222222222222"


class PlaylistAcoustidOutageTests(unittest.TestCase):
    def _probe_seen(self, acoustid_hits):
        with mock.patch.object(playlist_service, "evaluate_release_group_candidate") as ev:
            playlist_service._playlist_canonical_placement_evidence(
                {"title": "Song"}, {"mb_trackid": REC, "title": "Song"}, RG, acoustid_hits=acoustid_hits)
        return ev.call_args.kwargs["local_tracks"][0]["acoustid_hits"]

    def test_outage_reaches_evaluator_as_none(self):
        self.assertIsNone(self._probe_seen(None))

    def test_empty_answer_reaches_evaluator_as_empty_list(self):
        self.assertEqual(self._probe_seen([]), [])

    def test_real_evaluator_states_differ(self):
        outage = playlist_service._playlist_canonical_placement_evidence(
            {"title": "Song"}, {"mb_trackid": REC, "title": "Song"}, RG, acoustid_hits=None)
        empty = playlist_service._playlist_canonical_placement_evidence(
            {"title": "Song"}, {"mb_trackid": REC, "title": "Song"}, RG, acoustid_hits=[])
        self.assertFalse(outage.can_auto_accept())
        self.assertFalse(empty.can_auto_accept())
        self.assertNotEqual(
            (outage.state, outage.missing_evidence, outage.conflicts),
            (empty.state, empty.missing_evidence, empty.conflicts),
        )

    def test_placement_lookups_use_outage_aware_helper(self):
        self.assertFalse(hasattr(playlist_service, "_acoustid_lookup_cached"))
        self.assertTrue(callable(playlist_service._acoustid_hits_or_none))


class _Resp:
    def __init__(self, body, status=200, cookie=""):
        self._body, self.status = body, status
        self.headers = {"Set-Cookie": cookie} if cookie else {}

    def read(self, *_a):
        b, self._body = self._body, b""
        return b

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class QbitLoginHealthTests(unittest.TestCase):
    def setUp(self):
        provider_boundary.reset_provider_health()
        self.addCleanup(provider_boundary.reset_provider_health)
        for name, value in (("QBIT_URL", "http://qbit.test:8080"), ("QBIT_USERNAME", "u"),
                            ("QBIT_PASSWORD", "p")):
            patcher = mock.patch.object(acquisition_service, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _login(self, resp):
        with mock.patch("urllib.request.urlopen", return_value=resp):
            return acquisition_service._qbit_login_cookie()

    def test_refused_login_is_recorded_as_auth_failure(self):
        with self.assertRaises(provider_boundary.ProviderError) as ctx:
            self._login(_Resp(b"Fails. SECRET_BODY"))
        self.assertEqual(ctx.exception.outcome, provider_boundary.ProviderOutcome.AUTHENTICATION_ERROR)
        self.assertNotIn("SECRET_BODY", str(ctx.exception))
        row = provider_boundary.provider_health()["qbittorrent"]
        self.assertEqual(row["last_outcome"], provider_boundary.ProviderOutcome.AUTHENTICATION_ERROR.value)
        self.assertEqual(row["failures"], 1)
        status = acquisition_service._qbit_status_payload()
        self.assertEqual(status["health"]["last_outcome"], row["last_outcome"])
        self.assertNotIn("SECRET_BODY", repr(status))

    def test_good_login_is_recorded_as_success(self):
        cookie = self._login(_Resp(b"Ok.", cookie="SID=abc; path=/"))
        self.assertEqual(cookie, "SID=abc")
        row = provider_boundary.provider_health()["qbittorrent"]
        self.assertEqual(row["failures"], 0)
        self.assertIsNotNone(row["last_success_at"])


if __name__ == "__main__":
    unittest.main()
