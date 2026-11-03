"""#523: an AI key is only sent to the provider URL it belongs to.

Before the fix, a setup with only OPENROUTER_API_KEY sent that key to
api.openai.com, and with AI_BASE_URL pointing at OpenRouter the OpenAI key
won over the OpenRouter one.
"""
import itertools
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
        return _ai_api_key(), _ai_model_and_endpoint("m")[1]


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
    # Legacy: the first-run wizard saved every provider's key as
    # OPENAI_API_KEY next to the AI_BASE_URL the operator typed (#519 policy:
    # old setups keep working, with a warning).
    if "OPENAI_API_KEY" in has:
        return "OPENAI_API_KEY", host
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

    def test_legacy_openai_key_on_a_non_openai_url_warns_once_by_name(self):
        with self.assertLogs(config_layers.log, "WARNING") as logs:
            for _ in range(3):
                key, _ = _resolve({"OPENAI_API_KEY": "k-openai", "AI_BASE_URL": BASES["openrouter"]})
        self.assertEqual(key, "k-openai")
        self.assertEqual(len(logs.output), 1)
        self.assertIn("OPENROUTER_API_KEY", logs.output[0])
        self.assertNotIn("k-openai", logs.output[0])

    def test_openai_subdomain_counts_as_openai(self):
        key, _ = _resolve({"OPENAI_API_KEY": "k-openai", "AI_API_KEY": "k-generic",
                           "AI_BASE_URL": "https://eu.api.openai.com/v1"})
        self.assertEqual(key, "k-openai")

    def test_lookalike_hosts_are_custom(self):
        for url in ("https://api.openai.com.evil.example/v1", "https://openrouter.ai.evil.example/v1"):
            with self.subTest(url):
                key, _ = _resolve(_env(("OPENROUTER_API_KEY", "AI_API_KEY"), url))
                self.assertEqual(key, "k-generic")


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

    def test_openrouter_key_is_not_probed_against_openai(self):
        out = self._probe({"OPENROUTER_API_KEY": "k-openrouter", "AI_BASE_URL": BASES["openai"]})
        self.assertEqual(out["status"], "not_configured")
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
