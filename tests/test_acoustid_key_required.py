"""IA-12 (user decision: require a user key): AcoustID lookups have no
built-in fallback client key. Without ACOUSTID_API_KEY (or the legacy
ACOUSTID_KEY alias) a lookup reports a typed not_configured outcome -- never
an answer, never cached, never "no match" -- and makes no network call."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers_mb
from backend import provider_boundary as pb

ROOT = Path(__file__).resolve().parents[1]


class AcoustidKeyResolutionTests(unittest.TestCase):
    def test_precedence_and_no_fallback(self):
        cases = [
            ({"ACOUSTID_API_KEY": "primary", "ACOUSTID_KEY": "alias"}, "primary"),
            ({"ACOUSTID_API_KEY": "  ", "ACOUSTID_KEY": "alias"}, "alias"),
            ({"ACOUSTID_API_KEY": "", "ACOUSTID_KEY": ""}, ""),
        ]
        for env, expected in cases:
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                self.assertEqual(helpers_mb.acoustid_api_key(), expected)

    def test_no_hardcoded_client_key_remains(self):
        self.assertNotIn("8XaBELgH", (ROOT / "helpers_mb.py").read_text(encoding="utf-8"))


class NotConfiguredOutcomeTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"ACOUSTID_API_KEY": "", "ACOUSTID_KEY": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_lookup_without_key_is_not_configured_and_offline(self):
        with mock.patch.object(helpers_mb.subprocess, "run", side_effect=AssertionError("no fpcalc")), \
             mock.patch.object(pb, "opened", side_effect=AssertionError("no network")):
            result = helpers_mb.acoustid_lookup_outcome("/music/a.flac")
        self.assertEqual(result.outcome, pb.ProviderOutcome.NOT_CONFIGURED)
        self.assertFalse(result.answered)
        self.assertNotIn(result.outcome, pb.RETRYABLE)
        self.assertEqual(result.data, [])
        self.assertIn("AcoustID not configured", result.message)
        self.assertEqual(helpers_mb._acoustid_lookup("/music/a.flac"), [])

    def test_not_configured_is_never_cached(self):
        from backend import acoustid_service
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "a.flac"
            audio.write_bytes(b"fLaC" + b"\0" * 64)
            cache = Path(tmp) / "cache"
            with mock.patch.object(acoustid_service, "_ACOUSTID_FILE_CACHE_DIR", cache):
                result = acoustid_service._acoustid_lookup_cached_outcome(str(audio))
            self.assertEqual(result.outcome, pb.ProviderOutcome.NOT_CONFIGURED)
            self.assertFalse(cache.exists() and any(cache.rglob("*.json")))


class SetupStatusTests(unittest.TestCase):
    DIAG = {"plugin_failures": [], "plugin_loader_ok": True, "configured_plugins": ["chroma"],
            "loaded_plugins": ["chroma"], "pyacoustid_available": True}

    def status(self, env):
        import app  # noqa: F401
        import routes_setup
        with mock.patch.dict(os.environ, env):
            return routes_setup._acoustid_integration_status(self.DIAG, True)

    def test_status_says_not_configured_without_key(self):
        st = self.status({"ACOUSTID_API_KEY": "", "ACOUSTID_KEY": ""})
        self.assertFalse(st["configured"])
        self.assertEqual(st["state"], "not_configured")
        self.assertIn("AcoustID not configured", st.get("note", ""))

    def test_status_configured_with_key_or_alias(self):
        for env in ({"ACOUSTID_API_KEY": "k", "ACOUSTID_KEY": ""}, {"ACOUSTID_API_KEY": "", "ACOUSTID_KEY": "k"}):
            with self.subTest(env=env):
                self.assertTrue(self.status(env)["configured"])


if __name__ == "__main__":
    unittest.main()
