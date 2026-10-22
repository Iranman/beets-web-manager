"""Lidarr's configured-URL trust must be active in the REAL running server,
not only in a fresh ``import app`` probe.

Live incident (image 0.13.2): the ``/api/library/folder-mbid-names/apply``
job's Lidarr call to a private-LAN LIDARR_URL (http://192.168.0.250:8686) was
rejected by the outbound URL policy ("blocked by outbound URL policy",
ProviderOutcome.REJECTED) in the serving process, while the identical code and
env admitted the same call in a fresh ``python -c "import app"`` process.

Two guarantees are asserted here:

1. A registered configured-URL resolver that yields "" must NOT shadow the
   robust ``_RUNTIME_URL_CONSTANTS`` / ``sys.modules["backend.app_runtime"]``
   fallback. Registering a resolver (routes_lidarr does, mirroring plex) may
   only ADD a source of trust, never remove the one lidarr already had -- so a
   resolver that comes back empty in the long-lived server (a stale closure, a
   setting not yet present at the owner module's load) still leaves the
   configured URL trusted. This is the running-server gap and FAILS before the
   provider_boundary.configured_url fall-through fix.

2. Booting the app the way the server does (the real app module, which runs the
   ROUTE_MODULES loop) registers lidarr and admits an ``opened("lidarr", ...)``
   call to the operator's private configured URL end to end, on a worker thread
   (the job engine runs jobs on threads), with DNS/socket faked exactly as the
   operator-path trust tests do.
"""
import threading
import unittest
from unittest import mock

from backend import app_runtime, provider_boundary
from backend.security import OutboundPolicyError, implicit_rule_for_url

try:  # reuse the pinned-socket DNS harness from the operator-path trust tests
    from test_sec_outbound_configured_trust import _Net
except ImportError:  # pragma: no cover
    from tests.test_sec_outbound_configured_trust import _Net

PRIVATE_URL = "http://lidarr.test:8686"
PRIVATE_IP = "192.168.0.250"


class ConfiguredUrlFallthroughTests(unittest.TestCase):
    """A registered resolver that yields "" falls through to the app_runtime
    constant, so the configured URL keeps its trust in the serving process.
    FAILS on main (the empty resolver result shadows the fallback)."""

    def setUp(self):
        # Only loopback is allowlisted: the private IP earns trust ONLY via the
        # configured-URL path, never via BEETS_OUTBOUND_ALLOWLIST.
        env = mock.patch.dict("os.environ", {"BEETS_OUTBOUND_ALLOWLIST": "127.0.0.1:8337"})
        env.start()
        self.addCleanup(env.stop)
        url = mock.patch.object(app_runtime, "LIDARR_URL", PRIVATE_URL)
        url.start()
        self.addCleanup(url.stop)
        provider_boundary.reset_provider_health()

    def test_empty_resolver_falls_through_to_runtime_constant(self):
        # Simulates the serving process: a resolver is registered (as
        # routes_lidarr does) but comes back empty for this call.
        with mock.patch.dict(provider_boundary._CONFIGURED_URL_RESOLVERS,
                             {"lidarr": lambda: ""}):
            self.assertEqual(provider_boundary.configured_url("lidarr"), PRIVATE_URL)
            self.assertIsNotNone(implicit_rule_for_url(provider_boundary.configured_url("lidarr")))

    def test_nonempty_resolver_still_wins(self):
        other = "http://other.test:9999"
        with mock.patch.dict(provider_boundary._CONFIGURED_URL_RESOLVERS,
                             {"lidarr": lambda: other}):
            self.assertEqual(provider_boundary.configured_url("lidarr"), other)


# ── Real-app-context boot (the server's own path, not a bare import) ──────────
APP = routes_lidarr = None


def setUpModule():
    global APP, routes_lidarr
    import test_ai_batch_retry_race  # noqa: F401  temp env + imports real app.py
    import app as app_module
    import routes_lidarr as rl
    APP = app_module
    routes_lidarr = rl


class LidarrTrustRealBootTests(unittest.TestCase):
    """The running server boots through app.py's ROUTE_MODULES loop; this test
    boots the same way and proves the Lidarr configured-URL trust is active
    there -- including on a worker thread, as the job engine runs it."""

    def setUp(self):
        self.assertIsNotNone(APP, "real app failed to import")
        env = mock.patch.dict("os.environ", {"BEETS_OUTBOUND_ALLOWLIST": "127.0.0.1:8337"})
        env.start()
        self.addCleanup(env.stop)
        url = mock.patch.object(app_runtime, "LIDARR_URL", PRIVATE_URL)
        url.start()
        self.addCleanup(url.stop)
        provider_boundary.reset_provider_health()

    def test_route_module_registered_lidarr_at_boot(self):
        # The module-scope register_configured_url("lidarr", ...) ran as part of
        # the real app boot (routes_lidarr is an app.ROUTE_MODULES entry).
        self.assertIn("lidarr", provider_boundary._CONFIGURED_URL_RESOLVERS)
        self.assertEqual(provider_boundary.configured_url("lidarr"), PRIVATE_URL)

    def _fetch(self):
        with provider_boundary.opened("lidarr", "http://lidarr.test:8686/api/v1/artist",
                                      timeout=5, max_attempts=1) as resp:
            return resp.read()

    def test_opened_admits_configured_url_on_request_thread(self):
        net = _Net(self, {"lidarr.test": [PRIVATE_IP]})
        self.assertEqual(self._fetch(), b"ok")
        self.assertEqual(net.connected[0][:2], (PRIVATE_IP, 8686))

    def test_opened_admits_configured_url_on_worker_thread(self):
        net = _Net(self, {"lidarr.test": [PRIVATE_IP]})
        out = {}

        def run():
            try:
                out["body"] = self._fetch()
            except Exception as exc:  # pragma: no cover - failure path
                out["error"] = exc

        t = threading.Thread(target=run)
        t.start()
        t.join()
        self.assertNotIn("error", out, f"worker-thread call rejected: {out.get('error')!r}")
        self.assertEqual(out["body"], b"ok")
        self.assertEqual(net.connected[0][:2], (PRIVATE_IP, 8686))


if __name__ == "__main__":
    unittest.main()
