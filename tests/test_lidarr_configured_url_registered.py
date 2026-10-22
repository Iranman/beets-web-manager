"""Lidarr registers its operator-configured URL with the provider boundary,
so a private LAN LIDARR_URL (e.g. http://192.168.0.250:8686) is trusted for
Lidarr's own outbound calls -- exactly as Plex does. Without the registration
the folder MBID-name resolver's Lidarr call is rejected by the outbound URL
policy ("blocked by outbound URL policy") because the private address earns no
implicit trust.
"""
import unittest
from pathlib import Path
from unittest import mock

from backend import app_runtime, provider_boundary
from backend.security import OutboundPolicyError, implicit_rule_for_url

try:  # reuse the pinned-socket DNS harness from the operator-path trust tests
    from test_sec_outbound_configured_trust import _Net  # discover / run_tests
except ImportError:  # pragma: no cover
    from tests.test_sec_outbound_configured_trust import _Net  # tests.<module> runs

ROOT = Path(__file__).resolve().parents[1]
ROUTES_SOURCE = (ROOT / "routes_lidarr.py").read_text(encoding="utf-8")

PRIVATE_URL = "http://lidarr.test:8686"
PRIVATE_IP = "192.168.0.250"


class LidarrRegistrationWiringTests(unittest.TestCase):
    def test_routes_lidarr_registers_its_configured_url_at_module_scope(self):
        # routes_lidarr is a startup route module (app.ROUTE_MODULES), so a
        # module-scope call runs at boot -- the analog of plex_service's call.
        self.assertIn('provider_boundary.register_configured_url("lidarr"', ROUTES_SOURCE)
        # ...reading the same source the resolver uses (app_runtime.LIDARR_URL).
        self.assertIn('app_runtime, "LIDARR_URL"', ROUTES_SOURCE)


class LidarrConfiguredTrustTests(unittest.TestCase):
    """The registered resolver reads app_runtime.LIDARR_URL live, so only the
    operator's own configured URL is trusted when it is a private address."""

    def setUp(self):
        env = mock.patch.dict("os.environ", {"BEETS_OUTBOUND_ALLOWLIST": "127.0.0.1:8337"})
        env.start()
        self.addCleanup(env.stop)
        # The exact resolver routes_lidarr installs (importing routes_lidarr
        # would boot the whole Flask app; the wiring test proves the real call
        # site). Mirrors backend.lidarr_rgid_resolver._lidarr_base()'s read.
        resolver = mock.patch.dict(
            provider_boundary._CONFIGURED_URL_RESOLVERS,
            {"lidarr": lambda: app_runtime._s(getattr(app_runtime, "LIDARR_URL", ""))},
        )
        resolver.start()
        self.addCleanup(resolver.stop)
        url = mock.patch.object(app_runtime, "LIDARR_URL", PRIVATE_URL)
        url.start()
        self.addCleanup(url.stop)
        provider_boundary.reset_provider_health()

    def fetch(self, provider, target, **kw):
        with provider_boundary.opened(provider, target, timeout=5, max_attempts=1, **kw) as response:
            return response.read()

    def test_configured_url_returns_the_live_lidarr_url_and_earns_trust(self):
        self.assertEqual(provider_boundary.configured_url("lidarr"), PRIVATE_URL)
        self.assertIsNotNone(implicit_rule_for_url(provider_boundary.configured_url("lidarr")))

    def test_private_configured_lidarr_url_is_allowed_and_pinned(self):
        net = _Net(self, {"lidarr.test": [PRIVATE_IP]})
        self.assertEqual(self.fetch("lidarr", "http://lidarr.test:8686/api/v1/album"), b"ok")
        self.assertEqual(net.connected[0][:2], (PRIVATE_IP, 8686))

    def test_a_different_private_url_is_still_rejected_for_lidarr(self):
        net = _Net(self, {"other.test": [PRIVATE_IP]})
        with self.assertRaises(OutboundPolicyError):
            self.fetch("lidarr", "http://other.test:8686/api/v1/album")
        self.assertEqual(net.connected, [])  # blocked before any socket

    def test_call_without_configured_trust_is_still_rejected(self):
        net = _Net(self, {"lidarr.test": [PRIVATE_IP]})
        with self.assertRaises(OutboundPolicyError):
            self.fetch("lidarr", "http://lidarr.test:8686/api/v1/album", trust_configured=False)
        self.assertEqual(net.connected, [])


if __name__ == "__main__":
    unittest.main()
