"""QA (PR #338): adversarial checks of the operator-path outbound policy with
implicit configured-URL trust, plus upgrade regressions for the
operator-only settings migration. Reuses the fake DNS/socket harness of
tests/test_sec_outbound_configured_trust.py."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from backend import config_layers, provider_boundary, security
from backend.security import OutboundPolicyError
from tests.test_sec_outbound_configured_trust import _Net, _TrustBase


class PolicyBreakAttempts(_TrustBase):
    def blocked(self, net, provider, url):
        with self.assertRaises(OutboundPolicyError):
            self.fetch(provider, url)
        self.assertEqual(net.connected, [])

    def test_https_to_http_downgrade_on_same_host_port_is_refused(self):
        with mock.patch.dict(provider_boundary._CONFIGURED_URL_RESOLVERS, {"plex": lambda: "http://plex.test:32400"}):
            net = _Net(self, {"plex.test": ["192.168.0.250"]})
            net.server.redirects["/start"] = "https://plex.test:32400/next"
            with self.assertRaises(OutboundPolicyError):
                self.fetch("plex", "http://plex.test:32400/start")
            self.assertEqual(len(net.connected), 1)

    def test_redirect_from_trusted_origin_to_allowlisted_loopback_is_refused(self):
        net = _Net(self, {"plex.test": ["192.168.0.250"]})
        net.server.redirects["/start"] = "http://127.0.0.1:8337/api/admin"
        with self.assertRaises(OutboundPolicyError):
            self.fetch("plex", "http://plex.test:32400/start")
        self.assertEqual(len(net.connected), 1)

    def test_redirect_from_trusted_origin_to_other_private_port_is_refused(self):
        net = _Net(self, {"plex.test": ["192.168.0.250"]})
        net.server.redirects["/start"] = "http://plex.test:22/"
        with self.assertRaises(OutboundPolicyError):
            self.fetch("plex", "http://plex.test:32400/start")
        self.assertEqual(len(net.connected), 1)

    def test_embedded_metadata_and_private_forms_are_refused_for_implicit_rule(self):
        for ip in ("64:ff9b::a9fe:a9fe", "2002:a9fe:a9fe::1", "::ffff:100.100.100.200",
                   "64:ff9b::c0a8:fa", "2002:c0a8:fa::1", "::ffff:127.0.0.1", "fe80::1"):
            with self.subTest(ip=ip):
                self.blocked(_Net(self, {"plex.test": [ip]}), "plex", "http://plex.test:32400/")

    def test_mixed_private_and_public_answers_are_refused(self):
        self.blocked(_Net(self, {"plex.test": ["192.168.0.250", "93.184.216.34"]}), "plex",
                     "http://plex.test:32400/")

    def test_userinfo_host_confusion_is_refused(self):
        net = _Net(self, {"plex.test": ["192.168.0.250"], "evil.example.test": ["10.9.9.9"]})
        for url in ("http://plex.test:32400@evil.example.test/", "http://u:p@plex.test:32400/",
                    "http://192.168.0.250:32400@evil.example.test/"):
            with self.subTest(url=url), self.assertRaises(OutboundPolicyError):
                self.fetch("plex", url)
        self.assertEqual(net.connected, [])

    def test_percent_encoded_host_is_not_matched(self):
        self.blocked(_Net(self, {"plex.test": ["192.168.0.250"]}), "plex", "http://plex%2etest:32400/")

    def test_trailing_dot_and_case_reach_only_the_pinned_address(self):
        net = _Net(self, {"PLEX.test.": ["192.168.0.250"], "plex.test.": ["192.168.0.250"],
                          "plex.test": ["192.168.0.250"]})
        self.assertEqual(self.fetch("plex", "http://PLEX.test.:32400/"), b"ok")
        self.assertEqual(net.connected[0][:2], ("192.168.0.250", 32400))

    def test_default_port_matches_explicit_port_80_only(self):
        with mock.patch.dict(provider_boundary._CONFIGURED_URL_RESOLVERS, {"plex": lambda: "http://plex.test"}):
            net = _Net(self, {"plex.test": ["192.168.0.250"]})
            self.assertEqual(self.fetch("plex", "http://plex.test:80/x"), b"ok")
            with self.assertRaises(OutboundPolicyError):
                self.fetch("plex", "http://plex.test:443/x")
            self.assertEqual(len(net.connected), 1)

    def test_trust_does_not_cross_into_another_thread(self):
        net = _Net(self, {"plex.test": ["192.168.0.250"]})
        rule = security.implicit_rule_for_url("http://plex.test:32400")
        errors = []

        def other_provider():
            try:
                security.secure_urlopen("http://plex.test:32400/", timeout=5)
            except OutboundPolicyError as exc:
                errors.append(exc)

        with security.trusted_service_endpoint(rule):
            worker = threading.Thread(target=other_provider)
            worker.start()
            worker.join(10)
        self.assertEqual(len(errors), 1)
        self.assertEqual(net.connected, [])

    def test_trust_is_reset_after_the_call(self):
        net = _Net(self, {"plex.test": ["192.168.0.250"]})
        self.assertEqual(self.fetch("plex", "http://plex.test:32400/"), b"ok")
        self.assertIsNone(security._TRUSTED_RULE.get())
        with self.assertRaises(OutboundPolicyError):
            security.secure_urlopen("http://plex.test:32400/", timeout=5)
        self.assertEqual(len(net.connected), 1)

    def test_rebinding_on_redirect_hop_within_trusted_origin_is_refused(self):
        # first lookup (validation) and second (connect) private; the redirect
        # hop's lookup flips to metadata.
        net = _Net(self, {"plex.test": [["192.168.0.250"], ["192.168.0.250"], ["169.254.169.254"]]})
        net.server.redirects["/start"] = "/next"
        with self.assertRaises(OutboundPolicyError):
            self.fetch("plex", "http://plex.test:32400/start")
        self.assertNotIn("169.254.169.254", [c[0] for c in net.connected])


class DefaultDeploymentRegressions(_TrustBase):
    def test_default_allowlist_still_admits_beets_and_loopback(self):
        with mock.patch.dict("os.environ", {"BEETS_OUTBOUND_ALLOWLIST": security.DEFAULT_OUTBOUND_ALLOWLIST}):
            net = _Net(self, {"beets": ["172.18.0.3"], "localhost": ["127.0.0.1"]})
            for url in ("http://beets:8337/api/x", "http://localhost:8337/api/x"):
                with self.subTest(url=url):
                    self.assertEqual(security.secure_urlopen(url, timeout=5).read(), b"ok")
            self.assertEqual([c[0] for c in net.connected], ["172.18.0.3", "127.0.0.1"])

    def test_public_provider_needs_no_trust(self):
        net = _Net(self, {"musicbrainz.org": ["138.201.227.205"]})
        self.assertEqual(self.fetch("musicbrainz", "http://musicbrainz.org/ws/2/"), b"ok")
        self.assertEqual(net.connected[0][0], "138.201.227.205")


class OperatorKeyMigration(unittest.TestCase):
    def test_auth_disabled_0_saved_and_pinned_keeps_auth_on_and_save_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("BEETS_WEB_AUTH_DISABLED=0\nBEETS_OUTBOUND_ALLOWLIST=10.0.0.0/8:80\n"
                                "PLEX_URL=http://192.168.0.250:32400\n", encoding="utf-8")
            environ = {"BEETS_WEB_AUTH_DISABLED": "0", "BEETS_OUTBOUND_ALLOWLIST": "beets:8337",
                       "AI_BASE_URL": ""}
            saved = config_layers._BOOT_ENV_PINNED
            self.addCleanup(setattr, config_layers, "_BOOT_ENV_PINNED", saved)
            config_layers.record_boot_environment(environ)
            report = config_layers.migrate_saved_env_file(env_file)
            config_layers.load_saved_env_into_environ(env_file, environ=environ)
            self.assertEqual(sorted(report["removed"]), ["BEETS_OUTBOUND_ALLOWLIST", "BEETS_WEB_AUTH_DISABLED"])
            self.assertEqual(environ["BEETS_WEB_AUTH_DISABLED"], "0")
            self.assertEqual(environ["BEETS_OUTBOUND_ALLOWLIST"], "beets:8337")
            self.assertEqual(environ["PLEX_URL"], "http://192.168.0.250:32400")
            self.assertTrue(config_layers.pinned_by_boot_environment("BEETS_WEB_AUTH_DISABLED"))
            self.assertFalse(config_layers.pinned_by_boot_environment("AI_BASE_URL"))  # blank: not pinned
            self.assertFalse(config_layers.pinned_by_boot_environment("PLEX_URL"))
            self.assertEqual(len(list(Path(tmp).glob(".env.bak-migration-*"))), 1)
            # idempotent on the next boot
            self.assertEqual(config_layers.migrate_saved_env_file(env_file)["removed"], [])

    def test_saved_auth_disabled_1_cannot_survive_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("BEETS_WEB_AUTH_DISABLED=1\nSPOTIFLAC_CMD=/bin/sh -c id\n", encoding="utf-8")
            environ = {}
            config_layers.migrate_saved_env_file(env_file)
            config_layers.load_saved_env_into_environ(env_file, environ=environ)
            self.assertNotIn("BEETS_WEB_AUTH_DISABLED", environ)
            self.assertNotIn("SPOTIFLAC_CMD", environ)
            for key in config_layers.OPERATOR_KEYS:
                self.assertFalse(config_layers.is_saveable(key), key)


if __name__ == "__main__":
    unittest.main()
