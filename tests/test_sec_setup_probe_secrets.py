"""SEC-1 regression tests: setup connectivity probes must never send a stored
credential to a caller-supplied URL, and never use a stored credential at all
for an anonymous first-run caller.

Before the fix, an anonymous first-run POST to /api/setup/test/ai with only
{"base_url": "https://attacker.example/v1"} made the server send
"Authorization: Bearer <OPENAI_API_KEY>" to that host; /api/setup/test/plex
did the same with X-Plex-Token.
"""
import contextlib
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import app as app_module
from backend import provider_boundary
from backend.security import same_endpoint_url, strip_cross_origin_sensitive_headers

try:  # ARCH-001: patch app.py and the modules extracted from it
    from _app_family import patch_app_family  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_family import patch_app_family  # noqa: E402

_CSRF = {"X-Beets-CSRF": "1", "Origin": "http://localhost"}
_STORED_AI = "stored-ai-key-fixture-not-a-real-secret-00"
_STORED_PLEX = "stored-plex-token-fixture-0000"
_BEARER = "valid_test_bearer_token_32_chars_minimum!"
_PASSWORD = "Aa1!" + ("y" * 32)


class _ProbeHarness(unittest.TestCase):
    claimed = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = Path(self.tmp.name)
        env = {
            "BEETS_WEB_PASSWORD": _PASSWORD if self.claimed else "",
            "BEETS_WEB_PASSWORD_FILE": "",
            "BEETS_WEB_AUTH_TOKEN": _BEARER,
            "BEETS_WEB_AUTH_DISABLED": "0",
            "OPENAI_API_KEY": _STORED_AI,
            "AI_API_KEY": "",
            "AI_BASE_URL": "https://api.openai.com/v1",
            "PLEX_TOKEN": _STORED_PLEX,
            "PLEX_URL": "http://plex.example:32400",
        }
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.patches = [
            patch_app_family(app_module, "WEB_MANAGER_DATA_DIR", data),
            patch_app_family(app_module, "_INITIAL_BROWSER_PASSWORD_FILE", data / ".initial_admin_password"),
            patch_app_family(app_module, "_PERSISTED_BROWSER_PASSWORD_FILE", data / ".browser_password"),
            patch_app_family(app_module, "_PERSISTED_BROWSER_USERNAME_FILE", data / ".browser_username"),
            patch_app_family(app_module, "_BROWSER_SETUP_STATE_FILE", data / ".browser_setup_state"),
            patch_app_family(app_module, "_GENERATED_AUTH_TOKEN_FILE", data / ".auth_token"),
        ]
        for p in self.patches:
            p.start()
        app_module._migrate_or_initialize_setup_state()
        self.assertEqual(app_module._first_run_setup_required(), not self.claimed)
        self.sent = []

        @contextlib.contextmanager
        def fake_opened(provider, req, **_kw):
            self.sent.append((provider, req.full_url, {k.lower(): v for k, v in req.header_items()}))

            class _Resp:
                def read(self, *_a):
                    return b"<MediaContainer/>"
            yield _Resp()

        self.opened = mock.patch.object(provider_boundary, "opened", fake_opened)
        self.opened.start()
        self.client = app_module.app.test_client()

    def tearDown(self):
        self.opened.stop()
        for p in reversed(self.patches):
            p.stop()
        self.env.stop()
        self.tmp.cleanup()

    def post(self, path, body, *, auth=False):
        headers = dict(_CSRF)
        if auth:
            headers["Authorization"] = f"Bearer {_BEARER}"
        return self.client.post(path, json=body, headers=headers)

    def assert_no_stored_secret_sent(self):
        for _provider, _url, headers in self.sent:
            blob = repr(headers)
            self.assertNotIn(_STORED_AI, blob)
            self.assertNotIn(_STORED_PLEX, blob)


class FirstRunAnonymousProbeTests(_ProbeHarness):
    claimed = False

    def test_ai_attacker_base_url_without_key_sends_nothing(self):
        resp = self.post("/api/setup/test/ai", {"base_url": "https://attacker.example/v1"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "not_configured")
        self.assertEqual(self.sent, [])

    def test_ai_no_url_no_key_does_not_use_stored_key_anonymously(self):
        resp = self.post("/api/setup/test/ai", {})
        self.assertEqual(resp.get_json()["status"], "not_configured")
        self.assertEqual(self.sent, [])

    def test_ai_body_key_is_the_only_key_sent(self):
        resp = self.post("/api/setup/test/ai", {"base_url": "https://provider.example/v1", "api_key": "body-key"})
        self.assertTrue(resp.get_json()["ok"])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][2].get("authorization"), "Bearer body-key")
        self.assert_no_stored_secret_sent()

    def test_plex_attacker_url_without_token_sends_nothing(self):
        resp = self.post("/api/setup/test/plex", {"url": "https://attacker.example"})
        self.assertEqual(resp.get_json()["status"], "not_configured")
        self.assertEqual(self.sent, [])

    def test_plex_configured_url_still_needs_body_token_anonymously(self):
        resp = self.post("/api/setup/test/plex", {})
        self.assertEqual(resp.get_json()["status"], "not_configured")
        self.assertEqual(self.sent, [])


class ClaimedAuthenticatedProbeTests(_ProbeHarness):
    claimed = True

    def test_anonymous_probe_is_rejected_after_claim(self):
        resp = self.post("/api/setup/test/ai", {"base_url": "https://attacker.example/v1"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.sent, [])

    def test_ai_attacker_base_url_never_gets_stored_key(self):
        resp = self.post("/api/setup/test/ai", {"base_url": "https://attacker.example/v1"}, auth=True)
        self.assertEqual(resp.get_json()["status"], "not_configured")
        self.assertEqual(self.sent, [])

    def test_ai_lookalike_urls_never_get_stored_key(self):
        for url in (
            "https://api.openai.com.attacker.example/v1",
            "http://api.openai.com/v1",
            "https://api.openai.com:8443/v1",
            "https://user@api.openai.com/v1",
            "https://api.openai.com/v2",
        ):
            with self.subTest(url=url):
                self.sent.clear()
                self.post("/api/setup/test/ai", {"base_url": url}, auth=True)
                self.assertEqual(self.sent, [])

    def test_ai_configured_endpoint_uses_stored_key(self):
        resp = self.post("/api/setup/test/ai", {"base_url": "https://API.openai.com/v1/"}, auth=True)
        self.assertTrue(resp.get_json()["ok"])
        self.assertEqual(self.sent[0][2].get("authorization"), f"Bearer {_STORED_AI}")
        self.assertTrue(self.sent[0][1].startswith("https://API.openai.com/v1"))

    def test_ai_no_body_uses_configured_endpoint_and_key(self):
        resp = self.post("/api/setup/test/ai", {}, auth=True)
        self.assertTrue(resp.get_json()["ok"])
        self.assertTrue(self.sent[0][1].startswith("https://api.openai.com/v1/"))

    def test_plex_attacker_url_never_gets_stored_token(self):
        resp = self.post("/api/setup/test/plex", {"url": "https://attacker.example"}, auth=True)
        self.assertEqual(resp.get_json()["status"], "not_configured")
        self.assertEqual(self.sent, [])

    def test_plex_configured_url_uses_stored_token(self):
        resp = self.post("/api/setup/test/plex", {}, auth=True)
        self.assertTrue(resp.get_json()["ok"])
        self.assertEqual(self.sent[0][1], "http://plex.example:32400/library/sections")
        self.assertEqual(self.sent[0][2].get("x-plex-token"), _STORED_PLEX)


class SameEndpointAndRedirectHeaderTests(unittest.TestCase):
    def test_same_endpoint_url(self):
        self.assertTrue(same_endpoint_url("https://api.openai.com/v1/", "https://api.openai.com/v1"))
        self.assertTrue(same_endpoint_url("https://API.OpenAI.com:443/v1", "https://api.openai.com/v1"))
        self.assertTrue(same_endpoint_url("http://plex:32400", "http://plex:32400/"))
        self.assertFalse(same_endpoint_url("https://evil.example/v1", "https://api.openai.com/v1"))
        self.assertFalse(same_endpoint_url("https://api.openai.com/v1?x=1", "https://api.openai.com/v1"))
        self.assertFalse(same_endpoint_url("https://a:b@api.openai.com/v1", "https://api.openai.com/v1"))
        self.assertFalse(same_endpoint_url("", "https://api.openai.com/v1"))
        self.assertFalse(same_endpoint_url("https://api.openai.com:99999/v1", "https://api.openai.com/v1"))

    def test_provider_auth_headers_stripped_on_cross_origin_redirect(self):
        req = urllib.request.Request("http://plex.example:32400/library/sections", headers={
            "X-Plex-Token": "t", "X-Api-Key": "k", "Authorization": "Bearer b", "Accept": "x",
        })
        strip_cross_origin_sensitive_headers(req, "http://plex.example:32400/a", "https://other.example/b")
        names = {k.lower() for k in req.headers}
        self.assertNotIn("x-plex-token", names)
        self.assertNotIn("x-api-key", names)
        self.assertNotIn("authorization", names)
        self.assertIn("accept", names)


if __name__ == "__main__":
    unittest.main()
