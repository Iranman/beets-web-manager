"""SEC-3 / SEC-4 / SEC-11 regression tests.

SEC-3: the auth limiter used to run only *after* a failed password check,
and a correct password was accepted while the client was limited, so it
never slowed a brute force. SEC-4: the leftmost X-Forwarded-For entry
(client-controlled) was trusted. SEC-11: Basic auth skipped the password
check for a wrong username.
"""
import base64
import os
import unittest
from unittest import mock

import app as app_module
from backend import auth_service

_PW = "Aa1!" + ("z" * 32)
_BEARER = "valid_test_bearer_token_32_chars_minimum!"
_CSRF = {"X-Beets-CSRF": "1", "Origin": "http://localhost"}


class _LimitHarness(unittest.TestCase):
    extra_env = {}

    def setUp(self):
        env = {
            "BEETS_WEB_PASSWORD": _PW,
            "BEETS_WEB_USERNAME": "admin",
            "BEETS_WEB_AUTH_TOKEN": _BEARER,
            "BEETS_WEB_AUTH_DISABLED": "0",
            "BEETS_AUTH_RATE_LIMIT": "5",
            "BEETS_AUTH_RATE_WINDOW": "60",
            "BEETS_AUTH_ACCOUNT_RATE_LIMIT": "12",
            "BEETS_AUTH_ACCOUNT_RATE_WINDOW": "300",
            "BEETS_TRUSTED_PROXIES": "",
        }
        env.update(self.extra_env)
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        auth_service._AUTH_RATE_LIMITS.clear()
        self.addCleanup(auth_service._AUTH_RATE_LIMITS.clear)
        self.client = app_module.app.test_client()

    def tearDown(self):
        self.env.stop()

    def login(self, password, ip="81.2.69.9"):
        return self.client.post("/api/login", json={"username": "admin", "password": password},
                                headers=_CSRF, environ_base={"REMOTE_ADDR": ip})


class LoginLimiterTests(_LimitHarness):
    def test_correct_password_is_refused_while_ip_is_limited(self):
        codes = [self.login("wrong-password-xx").status_code for _ in range(5)]
        self.assertEqual(codes, [401] * 5)
        self.assertEqual(self.login("wrong-password-xx").status_code, 429)
        self.assertEqual(self.login(_PW).status_code, 429)

    def test_limited_attempt_does_not_evaluate_the_password(self):
        for _ in range(5):
            self.login("wrong-password-xx")
        with mock.patch.object(auth_service, "_check_password_value", wraps=auth_service._check_password_value) as check:
            self.assertEqual(self.login(_PW).status_code, 429)
        check.assert_not_called()

    def test_login_works_again_after_the_window(self):
        for _ in range(6):
            self.login("wrong-password-xx")
        later = auth_service.time.time() + 61
        with mock.patch.object(auth_service.time, "time", return_value=later):
            self.assertEqual(self.login(_PW).status_code, 200)

    def test_account_bucket_stops_ip_rotation(self):
        codes = [self.login("wrong-password-xx", ip=f"81.2.70.{i + 1}").status_code for i in range(12)]
        self.assertEqual(codes, [401] * 11 + [429])
        self.assertEqual(self.login(_PW, ip="81.2.70.200").status_code, 429)

    def test_account_bucket_does_not_429_credentialless_requests(self):
        for i in range(12):
            self.login("wrong-password-xx", ip=f"81.2.70.{i + 1}")
        resp = self.client.get("/api/library", environ_base={"REMOTE_ADDR": "81.2.70.201"})
        self.assertEqual(resp.status_code, 401)


class BasicAndRevealLimiterTests(_LimitHarness):
    def basic(self, user, password, ip="81.2.69.10"):
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        return self.client.get("/api/jobs", headers={"Authorization": f"Basic {token}"},
                               environ_base={"REMOTE_ADDR": ip})

    def test_basic_auth_correct_credentials_refused_while_limited(self):
        self.assertEqual(self.basic("admin", _PW, ip="81.2.69.99").status_code, 200)
        for _ in range(6):
            self.basic("admin", "wrong-password-xx")
        resp = self.basic("admin", _PW)
        self.assertEqual(resp.status_code, 429)

    def test_basic_auth_wrong_username_still_runs_password_check(self):
        with mock.patch.object(auth_service, "_check_password_value", return_value=False) as check:
            with app_module.app.test_request_context("/", environ_base={"REMOTE_ADDR": "81.2.69.11"}):
                token = base64.b64encode(b"not-admin:whatever").decode()
                self.assertFalse(auth_service._basic_authorized(f"Basic {token}"))
        check.assert_called_once()

    def test_reveal_auth_correct_password_refused_while_limited(self):
        headers = dict(_CSRF, Authorization=f"Bearer {_BEARER}")
        env = {"REMOTE_ADDR": "81.2.69.12"}
        codes = [self.client.post("/api/setup/env/reveal-auth", json={"password": "wrong-password-xx"},
                                  headers=headers, environ_base=env).status_code for _ in range(6)]
        self.assertEqual(codes[:5], [401] * 5)
        self.assertEqual(codes[5], 429)
        resp = self.client.post("/api/setup/env/reveal-auth", json={"password": _PW}, headers=headers, environ_base=env)
        self.assertEqual(resp.status_code, 429)


class ForwardedForTests(unittest.TestCase):
    def identity(self, peer, xff=None, real_ip=None, trusted="10.0.0.0/8"):
        headers = {}
        if xff is not None:
            headers["X-Forwarded-For"] = xff
        if real_ip is not None:
            headers["X-Real-IP"] = real_ip
        with mock.patch.dict(os.environ, {"BEETS_TRUSTED_PROXIES": trusted}):
            with app_module.app.test_request_context("/", headers=headers, environ_base={"REMOTE_ADDR": peer}):
                return auth_service._request_client_identity(), auth_service._client_ip_is_lan()

    def test_spoofed_leftmost_entry_is_ignored(self):
        self.assertEqual(self.identity("10.0.0.2", "10.9.9.9, 81.2.69.7"), ("81.2.69.7", False))

    def test_chain_of_trusted_proxies_is_skipped(self):
        self.assertEqual(self.identity("10.0.0.2", "1.2.3.4, 81.2.69.7, 10.0.0.3")[0], "81.2.69.7")

    def test_all_trusted_hops_returns_leftmost(self):
        self.assertEqual(self.identity("10.0.0.2", "10.1.1.1, 10.0.0.3")[0], "10.1.1.1")

    def test_untrusted_peer_ignores_forwarded_headers(self):
        self.assertEqual(self.identity("81.2.69.50", "10.0.0.9", real_ip="10.0.0.9"), ("81.2.69.50", False))

    def test_malformed_hop_stops_at_peer(self):
        self.assertEqual(self.identity("10.0.0.2", "81.2.69.7, not-an-ip")[0], "10.0.0.2")

    def test_x_real_ip_only_without_xff(self):
        self.assertEqual(self.identity("10.0.0.2", real_ip="81.2.69.8")[0], "81.2.69.8")

    def test_multiple_xff_headers_are_joined(self):
        with mock.patch.dict(os.environ, {"BEETS_TRUSTED_PROXIES": "10.0.0.0/8"}):
            with app_module.app.test_request_context(
                "/", headers=[("X-Forwarded-For", "10.9.9.9"), ("X-Forwarded-For", "81.2.69.7")],
                environ_base={"REMOTE_ADDR": "10.0.0.2"},
            ):
                self.assertEqual(auth_service._request_client_identity(), "81.2.69.7")


if __name__ == "__main__":
    unittest.main()
