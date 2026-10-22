"""Why a correctly-configured Lidarr call is REJECTED in the live server.

Live v0.13.3 diagnostic from the running folder-MBID job thread showed
configured_url("lidarr") correct and would_trust_rule VALID, yet the call was
REJECTED ("blocked by outbound URL policy"). Two things are pinned here:

1. NOT a thread/contextvar problem. The trusted rule set by
   provider_boundary.opened() IS visible to secure_urlopen when the call runs
   on a worker thread (the job-engine model), so an operator-path call to the
   private configured URL is ADMITTED from a worker thread. This disproves the
   "trust set on one thread, urlopen on another, contextvar not inherited"
   hypothesis.

2. The one path that rejects a correctly-configured call is a CROSS-ORIGIN
   REDIRECT from the service endpoint: the implicit rule pins scheme+host+port,
   so a redirect to a different origin (e.g. http -> https, or a different
   port) is refused by SafeRedirectHandler -- surfaced to the resolver as
   ProviderOutcome.REJECTED. A same-origin redirect (e.g. a trailing slash) is
   still followed. The refusal now names the redacted redirect TARGET origin so
   one live deploy pinpoints exactly where the operator's Lidarr redirects.
"""
import threading
import unittest
from unittest import mock

from backend import app_runtime, provider_boundary
from backend.security import OutboundPolicyError

try:
    from test_sec_outbound_configured_trust import _Net
except ImportError:  # pragma: no cover
    from tests.test_sec_outbound_configured_trust import _Net

PRIVATE_URL = "http://lidarr.test:8686"
PRIVATE_IP = "192.168.0.250"

APP = None


def setUpModule():
    global APP
    import test_ai_batch_retry_race  # noqa: F401  temp env + imports real app.py (installs secure_urlopen)
    import app as app_module
    import routes_lidarr  # noqa: F401  registers the lidarr configured-URL resolver
    APP = app_module


class ProviderTrustMechanismTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(APP, "real app failed to import")
        env = mock.patch.dict("os.environ", {"BEETS_OUTBOUND_ALLOWLIST": "127.0.0.1:8337"})
        env.start()
        self.addCleanup(env.stop)
        url = mock.patch.object(app_runtime, "LIDARR_URL", PRIVATE_URL)
        url.start()
        self.addCleanup(url.stop)
        provider_boundary.reset_provider_health()

    def _fetch(self):
        with provider_boundary.opened("lidarr", "http://lidarr.test:8686/api/v1/artist",
                                      timeout=5, max_attempts=1) as resp:
            return resp.read()

    # 1. Thread/contextvar hypothesis is false: worker-thread call is admitted.
    def test_configured_url_admitted_on_worker_thread(self):
        _Net(self, {"lidarr.test": [PRIVATE_IP]})
        out = {}

        def work():
            try:
                out["body"] = self._fetch()
            except Exception as exc:  # pragma: no cover - failure path
                out["error"] = exc

        t = threading.Thread(target=work)
        t.start()
        t.join()
        self.assertNotIn("error", out, f"worker-thread call rejected: {out.get('error')!r}")
        self.assertEqual(out.get("body"), b"ok")

    # 2. Same-origin redirect (e.g. trailing slash) is still followed.
    def test_same_origin_redirect_admitted(self):
        net = _Net(self, {"lidarr.test": [PRIVATE_IP]})
        net.server.redirects["/api/v1/artist"] = "http://lidarr.test:8686/api/v1/artist/"
        self.assertEqual(self._fetch(), b"ok")

    # 3. Cross-origin redirect (http -> https, same host:port) is refused, and
    #    the refusal names the redacted target origin (no secret).
    def test_cross_origin_redirect_refused_and_names_target(self):
        net = _Net(self, {"lidarr.test": [PRIVATE_IP]})
        net.server.redirects["/api/v1/artist"] = "https://lidarr.test:8686/api/v1/artist"
        with self.assertRaises(OutboundPolicyError) as cm:
            self._fetch()
        msg = str(cm.exception)
        self.assertIn("cross-origin redirect", msg)
        self.assertIn("https://lidarr.test:8686", msg)  # redacted target origin

    # 4. Cross-origin redirect to a different PORT is refused (SSRF boundary).
    def test_cross_origin_redirect_other_port_refused(self):
        net = _Net(self, {"lidarr.test": [PRIVATE_IP]})
        net.server.redirects["/api/v1/artist"] = "http://lidarr.test:9999/api/v1/artist"
        with self.assertRaises(OutboundPolicyError):
            self._fetch()


class RedirectDiagnosticTests(unittest.TestCase):
    """The temporary diagnostic surfaces the RAW policy error (with the redirect
    target) on a rejection, and never logs the API key."""

    API_KEY = "redirect-diagnostic-secret-key-DO-NOT-LOG-9f8e"

    def setUp(self):
        from backend import lidarr_rgid_resolver as rr
        self.rr = rr
        rr._rejection_diagnostic_logged = False
        self.addCleanup(setattr, rr, "_rejection_diagnostic_logged", False)
        env = mock.patch.dict("os.environ", {"BEETS_OUTBOUND_ALLOWLIST": "127.0.0.1:8337"})
        env.start()
        self.addCleanup(env.stop)
        for name, val in (("LIDARR_URL", PRIVATE_URL), ("LIDARR_KEY", self.API_KEY)):
            p = mock.patch.object(app_runtime, name, val)
            p.start()
            self.addCleanup(p.stop)
        res = mock.patch.dict(provider_boundary._CONFIGURED_URL_RESOLVERS,
                              {"lidarr": lambda: app_runtime._s(getattr(app_runtime, "LIDARR_URL", ""))})
        res.start()
        self.addCleanup(res.stop)
        provider_boundary.reset_provider_health()

    def test_diagnostic_names_redirect_target_without_secret(self):
        net = _Net(self, {"lidarr.test": [PRIVATE_IP]})
        net.server.redirects["/api/v1/artist"] = "https://lidarr.test:8686/api/v1/artist"
        with self.assertLogs("app", level="WARNING") as cm:
            with self.assertRaises(provider_boundary.ProviderError):
                self.rr._lidarr_get_list("/api/v1/artist", timeout=5)
        line = "\n".join(cm.output)
        self.assertIn("LIDARR TRUST DIAGNOSTIC", line)
        self.assertIn("cross-origin redirect", line)              # the real mechanism
        self.assertIn("https://lidarr.test:8686", line)           # the redirect target origin
        self.assertNotIn(self.API_KEY, line)                      # never the key
        self.assertNotIn("DO-NOT-LOG", line)


if __name__ == "__main__":
    unittest.main()
