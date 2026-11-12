"""#523: an AI key is only sent to the provider URL it belongs to.

Before the fix, a setup with only OPENROUTER_API_KEY sent that key to
api.openai.com, and with AI_BASE_URL pointing at OpenRouter the OpenAI key
won over the OpenRouter one.
"""
import itertools
import logging
import os
import unittest
import urllib.parse
from unittest import mock

from backend import config_layers
from backend.matching_service import _ai_api_key, _ai_model_and_endpoint

try:
    import test_sec_setup_probe_secrets as probe_tests
except ImportError:  # pragma: no cover
    from tests import test_sec_setup_probe_secrets as probe_tests

KEYS = {"OPENAI_API_KEY": "k-openai", "OPENROUTER_API_KEY": "k-openrouter", "AI_API_KEY": "k-generic"}
BASES = {
    None: None,
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "custom": "https://llm.example/v1",
}
_NAMES = tuple(KEYS) + ("AI_BASE_URL", "AI_MODEL")


def _env(keys, base):
    env = {name: KEYS[name] for name in keys}
    if base:
        env["AI_BASE_URL"] = base
    return env


def _resolve(env):
    clean = {k: v for k, v in os.environ.items() if k not in _NAMES}
    clean.update(env)
    with mock.patch.dict(os.environ, clean, clear=True):
        key = _ai_api_key()
        return key, _ai_model_and_endpoint("m", key)[1]


def _host(url):
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _expected(keys, base_name):
    """(key name, endpoint host) the issue's rule allows, or (None, host)."""
    has = set(keys)
    if base_name is None:
        for name, host in (("OPENAI_API_KEY", "api.openai.com"), ("OPENROUTER_API_KEY", "openrouter.ai"),
                           ("AI_API_KEY", "api.openai.com")):
            if name in has:
                return name, host
        return None, "api.openai.com"
    host = _host(BASES[base_name])
    own = {"openai": ["OPENAI_API_KEY", "AI_API_KEY"],
           "openrouter": ["OPENROUTER_API_KEY", "AI_API_KEY"],
           "custom": ["AI_API_KEY"]}[base_name]
    for name in own:
        if name in has:
            return name, host
    # No legacy OPENAI_API_KEY fallback for non-OpenAI URLs (#535 item 1).
    return None, host


class KeyUrlPairingTests(unittest.TestCase):
    def setUp(self):
        config_layers._warned_aliases.clear()

    def test_openrouter_only_setup_goes_to_openrouter(self):
        key, endpoint = _resolve({"OPENROUTER_API_KEY": "k-openrouter"})
        self.assertEqual(key, "k-openrouter")
        self.assertEqual(endpoint, "https://openrouter.ai/api/v1/chat/completions")

    def test_openrouter_url_prefers_the_openrouter_key(self):
        key, endpoint = _resolve(_env(KEYS, BASES["openrouter"]))
        self.assertEqual(key, "k-openrouter")
        self.assertEqual(_host(endpoint), "openrouter.ai")

    def test_openrouter_key_never_goes_to_openai_or_custom(self):
        for base in ("openai", "custom"):
            with self.subTest(base):
                key, _ = _resolve({"OPENROUTER_API_KEY": "k-openrouter", "AI_BASE_URL": BASES[base]})
                self.assertEqual(key, "")

    def test_every_combination(self):
        for n in range(len(KEYS) + 1):
            for keys in itertools.combinations(KEYS, n):
                for base_name in BASES:
                    with self.subTest(keys=keys, base=base_name):
                        key, endpoint = _resolve(_env(keys, BASES[base_name]))
                        name, host = _expected(keys, base_name)
                        self.assertEqual(key, KEYS[name] if name else "")
                        self.assertEqual(_host(endpoint), host)
                        if key == KEYS["OPENROUTER_API_KEY"]:
                            self.assertEqual(_host(endpoint), "openrouter.ai")

    def test_openai_env_key_never_goes_to_a_non_openai_url(self):
        # #535 item 1: the legacy fallback is gone; no key at all instead.
        for url in (BASES["custom"], BASES["openrouter"], "http://llm.example/v1",
                    "https://api.openai.com.evil.example/v1", "https://llm.example./v1"):
            with self.subTest(url), self.assertNoLogs(config_layers.log, "WARNING"):
                key, endpoint = _resolve({"OPENAI_API_KEY": "k-openai", "AI_BASE_URL": url})
                self.assertEqual(key, "")
                self.assertEqual(endpoint, f"{url}/chat/completions")

    def test_openai_env_key_still_goes_to_official_openai(self):
        for env in ({"OPENAI_API_KEY": "k-openai"},
                    {"OPENAI_API_KEY": "k-openai", "AI_BASE_URL": BASES["openai"]}):
            with self.subTest(env):
                key, endpoint = _resolve(env)
                self.assertEqual(key, "k-openai")
                self.assertEqual(endpoint, "https://api.openai.com/v1/chat/completions")

    def test_custom_url_uses_the_settings_key(self):
        key, endpoint = _resolve({"OPENAI_API_KEY": "k-openai", "AI_API_KEY": "k-generic",
                                  "AI_BASE_URL": BASES["custom"]})
        self.assertEqual(key, "k-generic")
        self.assertEqual(_host(endpoint), "llm.example")

    def test_openai_subdomain_counts_as_openai(self):
        key, _ = _resolve({"OPENAI_API_KEY": "k-openai", "AI_API_KEY": "k-generic",
                           "AI_BASE_URL": "https://eu.api.openai.com/v1"})
        self.assertEqual(key, "k-openai")

    def test_lookalike_hosts_are_custom(self):
        for url in ("https://api.openai.com.evil.example/v1", "https://openrouter.ai.evil.example/v1"):
            with self.subTest(url):
                key, _ = _resolve(_env(("OPENROUTER_API_KEY", "AI_API_KEY"), url))
                self.assertEqual(key, "k-generic")


class PlainHttpAndTrailingDotTests(unittest.TestCase):
    """#535 items 2-3: no provider key over http; "host." is "host"."""

    def setUp(self):
        config_layers._warned_aliases.clear()

    def test_no_key_to_provider_hosts_over_http(self):
        for url in ("http://api.openai.com/v1", "HTTP://eu.api.openai.com/v1", "http://openrouter.ai/api/v1",
                    "http://api.openai.com./v1", "http://openrouter.ai./api/v1"):
            with self.subTest(url):
                key, _ = _resolve(_env(KEYS, url))
                self.assertEqual(key, "")

    def test_custom_http_url_is_unchanged(self):
        # A custom URL keeps its own AI_API_KEY over http.
        self.assertEqual(_resolve(_env(KEYS, "http://llm.example/v1"))[0], "k-generic")

    def test_trailing_dot_host_is_the_provider(self):
        for url, want in (("https://api.openai.com./v1", "k-openai"), ("https://openrouter.ai./api/v1", "k-openrouter"),
                          ("https://API.OpenAI.com./v1", "k-openai")):
            with self.subTest(url):
                key, endpoint = _resolve(_env(KEYS, url))
                self.assertEqual(key, want)
                self.assertTrue(endpoint.startswith(url))

    def test_openai_key_alone_follows_a_trailing_dot_openai_url_without_warning(self):
        with self.assertNoLogs(config_layers.log, "WARNING"):
            key, _ = _resolve({"OPENAI_API_KEY": "k-openai", "AI_BASE_URL": "https://api.openai.com./v1"})
        self.assertEqual(key, "k-openai")


class KeyEndpointBindingTests(unittest.TestCase):
    """F3: callers read the key once and the endpoint later (per album, per
    batch); an AI_BASE_URL saved in between must not get the old key."""

    def test_endpoint_stays_with_the_url_the_key_was_resolved_for(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k-openai", "AI_API_KEY": "",
                                          "OPENROUTER_API_KEY": "", "AI_BASE_URL": BASES["openai"]}):
            key = _ai_api_key()
            os.environ["AI_BASE_URL"] = BASES["custom"]
            _, endpoint = _ai_model_and_endpoint("m", key)
        self.assertEqual(key, "k-openai")
        self.assertEqual(endpoint, "https://api.openai.com/v1/chat/completions")

    def test_the_key_is_required(self):
        with self.assertRaises(TypeError):
            _ai_model_and_endpoint("m")  # pylint: disable=no-value-for-parameter


_FAKE_PASSWORD = "fake-pw-523-not-real"
# U+FF0F in the netloc fails urlsplit's NFKC check; its ValueError quotes the netloc.
_BAD_URL = f"https://user:{_FAKE_PASSWORD}／x@ai.example/v1"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.text = []

    def emit(self, record):
        self.text.append(logging.Formatter().format(record))


class UnparseableBaseUrlTests(probe_tests._ProbeHarness):
    """F1: an AI_BASE_URL urlsplit rejects must not fail setup or leak."""
    claimed = True

    def test_resolver_sends_no_key(self):
        key, _ = _resolve({"OPENAI_API_KEY": "k-openai", "AI_API_KEY": "k-generic", "AI_BASE_URL": _BAD_URL})
        self.assertEqual(key, "")

    def test_setup_endpoints_survive_and_never_log_the_password(self):
        capture = _Capture()
        root = logging.getLogger()
        root.addHandler(capture)
        self.addCleanup(root.removeHandler, capture)
        auth = {"Authorization": f"Bearer {probe_tests._BEARER}"}
        with mock.patch.dict(os.environ, {"AI_BASE_URL": _BAD_URL, "OPENAI_API_KEY": "k-openai"}):
            status = self.client.get("/api/setup/status", headers=auth)
            probe = self.post("/api/setup/test/ai", {}, auth=True)
            probe_body_key = self.post("/api/setup/test/ai", {"api_key": "body-key"}, auth=True)
            env = self.client.get("/api/setup/env", headers=auth)
        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.get_json()["integrations"]["ai"]["configured"])
        self.assertEqual(probe.status_code, 200)
        self.assertEqual(probe.get_json()["status"], "not_configured")
        self.assertEqual(probe_body_key.status_code, 200)
        self.assertEqual(probe_body_key.get_json()["status"], "failed")
        self.assertEqual(env.status_code, 200)
        self.assertEqual(self.sent, [])
        for response in (status, probe, probe_body_key):
            self.assertNotIn(_FAKE_PASSWORD, response.get_data(as_text=True))
        for line in capture.text:
            self.assertNotIn(_FAKE_PASSWORD, line)


class SetupProbeKeyUrlTests(probe_tests._ProbeHarness):
    claimed = True

    def _probe(self, env):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "", "OPENROUTER_API_KEY": "", "AI_API_KEY": "",
                                          "AI_BASE_URL": "", **env}):
            return self.post("/api/setup/test/ai", {}, auth=True).get_json()

    def test_openrouter_only_probe_goes_to_openrouter(self):
        self.assertTrue(self._probe({"OPENROUTER_API_KEY": "k-openrouter"})["ok"])
        self.assertTrue(self.sent[0][1].startswith("https://openrouter.ai/api/v1/"))
        self.assertEqual(self.sent[0][2]["authorization"], "Bearer k-openrouter")

    def test_openrouter_url_probe_uses_the_openrouter_key(self):
        self._probe({**KEYS, "AI_BASE_URL": BASES["openrouter"]})
        self.assertEqual(self.sent[0][2]["authorization"], "Bearer k-openrouter")

    def test_blank_url_body_key_is_probed_where_the_wizard_saves_it(self):
        # The wizard saves a blank-URL key as OPENAI_API_KEY for OpenAI, so
        # the probe must not pass against OpenRouter because OPENROUTER_API_KEY
        # happens to be stored.
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "", "AI_API_KEY": "", "AI_BASE_URL": "",
                                          "OPENROUTER_API_KEY": "k-openrouter"}):
            for body in ({"api_key": "body-key"}, {"api_key": "body-key", "base_url": ""}):
                self.sent.clear()
                self.post("/api/setup/test/ai", body, auth=True)
                self.assertTrue(self.sent[0][1].startswith("https://api.openai.com/v1/"), body)
                self.assertEqual(self.sent[0][2]["authorization"], "Bearer body-key")

    def test_no_stored_key_probed_over_http(self):
        for url in ("http://api.openai.com/v1", "http://openrouter.ai./api/v1"):
            with self.subTest(url):
                self.sent.clear()
                self.assertEqual(self._probe({**KEYS, "AI_BASE_URL": url})["status"], "not_configured")
                self.assertEqual(self.sent, [])

    def test_typed_key_not_probed_against_a_provider_over_http(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "", "OPENROUTER_API_KEY": "", "AI_API_KEY": "",
                                          "AI_BASE_URL": ""}):
            for url in ("http://api.openai.com/v1", "http://openrouter.ai/api/v1", "http://api.openai.com./v1"):
                with self.subTest(url):
                    out = self.post("/api/setup/test/ai", {"api_key": "body-key", "base_url": url}, auth=True)
                    self.assertEqual(out.get_json()["status"], "failed")
                    self.assertNotIn("body-key", out.get_data(as_text=True))
            self.assertEqual(self.sent, [])

    def test_openai_env_key_is_not_probed_against_a_custom_url(self):
        out = self._probe({"OPENAI_API_KEY": "k-openai", "AI_BASE_URL": BASES["custom"]})
        self.assertEqual(out["status"], "not_configured")
        self.assertIn("Settings", out["error"])
        self.assertNotIn("k-openai", str(out))
        self.assertEqual(self.sent, [])

    def test_openai_env_key_is_probed_against_official_openai(self):
        self.assertTrue(self._probe({"OPENAI_API_KEY": "k-openai"})["ok"])
        self.assertTrue(self.sent[0][1].startswith("https://api.openai.com/v1/"))
        self.assertEqual(self.sent[0][2]["authorization"], "Bearer k-openai")

    def test_openrouter_key_is_not_probed_against_openai(self):
        out = self._probe({"OPENROUTER_API_KEY": "k-openrouter", "AI_BASE_URL": BASES["openai"]})
        self.assertEqual(out["status"], "not_configured")
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
