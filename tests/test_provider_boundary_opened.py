"""provider_boundary.opened(): the one way application code opens an HTTP
connection to a provider (ARCH-006). A drop-in for ``urlopen`` as a context
manager that adds the provider's policy, bounded retries of repeatable
requests, and a classified, redacted health record -- and re-raises the
original exception so call sites keep their own error handling."""

import ast
import io
import socket
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import backend.provider_boundary as pb

ROOT = Path(__file__).resolve().parent.parent


class _Response(io.BytesIO):
    status = 200
    headers = {"Content-Type": "application/json"}


def _http_error(code, headers=None):
    return urllib.error.HTTPError("https://musicbrainz.org/ws/2/x", code, "err", headers or {}, None)


class OpenedTests(unittest.TestCase):
    def setUp(self):
        pb.reset_provider_health()
        self.addCleanup(pb.reset_provider_health)
        self.sleeps = []
        env = mock.patch.dict("os.environ", {"PROVIDER_MAX_ATTEMPTS": ""})
        env.start()
        self.addCleanup(env.stop)

    def _open(self, provider, request, **kwargs):
        with pb.opened(provider, request, timeout=5, sleep=self.sleeps.append, **kwargs) as response:
            return response.read()

    def test_success_yields_the_real_response_and_records_confirmed(self):
        with mock.patch.object(urllib.request, "urlopen", return_value=_Response(b'{"ok": 1}')) as urlopen:
            self.assertEqual(self._open("musicbrainz", "https://musicbrainz.org/ws/2/x"), b'{"ok": 1}')
        urlopen.assert_called_once_with("https://musicbrainz.org/ws/2/x", timeout=5)
        row = pb.provider_health()["musicbrainz"]
        self.assertEqual((row["last_outcome"], row["calls"], row["failures"], row["last_attempts"]),
                         ("confirmed", 1, 0, 1))

    def test_a_retryable_failure_is_retried_within_the_policy_then_succeeds(self):
        effects = [_http_error(503), socket.timeout("slow"), _Response(b"ok")]
        with mock.patch.object(urllib.request, "urlopen", side_effect=effects) as urlopen:
            self.assertEqual(self._open("musicbrainz", "https://musicbrainz.org/ws/2/x"), b"ok")
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(self.sleeps, [1.0, 2.0])  # bounded, exponential
        row = pb.provider_health()["musicbrainz"]
        self.assertEqual((row["last_outcome"], row["retries"], row["last_attempts"]), ("confirmed", 2, 3))

    def test_attempts_are_bounded_and_the_original_exception_is_raised(self):
        error = _http_error(503)
        with mock.patch.object(urllib.request, "urlopen", side_effect=error) as urlopen:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self._open("musicbrainz", "https://musicbrainz.org/ws/2/x")
        self.assertIs(caught.exception, error)  # call sites keep their `except HTTPError`
        self.assertEqual(urlopen.call_count, pb.POLICIES["musicbrainz"].max_attempts)
        row = pb.provider_health()["musicbrainz"]
        self.assertEqual((row["last_outcome"], row["last_status_code"], row["failures"]), ("unavailable", 503, 1))

    def test_retry_after_is_honoured_and_capped(self):
        effects = [_http_error(429, {"Retry-After": "7"}), _http_error(429, {"Retry-After": "900"}), _Response(b"ok")]
        with mock.patch.object(urllib.request, "urlopen", side_effect=effects):
            self._open("musicbrainz", "https://musicbrainz.org/ws/2/x")
        self.assertEqual(self.sleeps, [7.0, pb.MAX_RETRY_AFTER_SECONDS])

    def test_client_errors_and_auth_failures_are_never_retried(self):
        for code, outcome in ((404, "rejected"), (400, "rejected"), (401, "authentication_error"),
                              (403, "authentication_error")):
            with self.subTest(code=code):
                pb.reset_provider_health()
                with mock.patch.object(urllib.request, "urlopen", side_effect=_http_error(code)) as urlopen:
                    with self.assertRaises(urllib.error.HTTPError):
                        self._open("musicbrainz", "https://musicbrainz.org/ws/2/x")
                self.assertEqual(urlopen.call_count, 1)
                self.assertEqual(pb.provider_health()["musicbrainz"]["last_outcome"], outcome)
        self.assertEqual(self.sleeps, [])

    def test_a_post_is_never_repeated(self):
        request = urllib.request.Request("https://lidarr.local/api/v1/command", data=b"{}", method="POST")
        with mock.patch.object(urllib.request, "urlopen", side_effect=_http_error(503)) as urlopen:
            with self.assertRaises(urllib.error.HTTPError):
                self._open("lidarr", request)
        self.assertEqual(urlopen.call_count, 1)
        get = urllib.request.Request("https://lidarr.local/api/v1/wanted/missing")
        with mock.patch.object(urllib.request, "urlopen", side_effect=_http_error(503)) as urlopen:
            with self.assertRaises(urllib.error.HTTPError):
                self._open("lidarr", get)
        self.assertEqual(urlopen.call_count, pb.POLICIES["lidarr"].max_attempts)

    def test_the_ai_provider_and_explicit_single_attempts_do_not_retry(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=socket.timeout("slow")) as urlopen:
            with self.assertRaises(socket.timeout):
                self._open("ai", "https://api.openai.com/v1/x")
        self.assertEqual(urlopen.call_count, 1)
        with mock.patch.object(urllib.request, "urlopen", side_effect=_http_error(503)) as urlopen:
            with self.assertRaises(urllib.error.HTTPError):
                self._open("musicbrainz", "https://musicbrainz.org/ws/2/x", max_attempts=1)
        self.assertEqual(urlopen.call_count, 1)

    def test_the_operator_cap_turns_retries_off(self):
        with mock.patch.dict("os.environ", {"PROVIDER_MAX_ATTEMPTS": "1"}):
            self.assertEqual(pb.policy_for("musicbrainz").max_attempts, 1)
            with mock.patch.object(urllib.request, "urlopen", side_effect=_http_error(503)) as urlopen:
                with self.assertRaises(urllib.error.HTTPError):
                    self._open("musicbrainz", "https://musicbrainz.org/ws/2/x")
            self.assertEqual(urlopen.call_count, 1)

    def test_an_unknown_provider_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            pb.policy_for("some-new-service")

    def test_health_is_redacted_and_lists_every_provider(self):
        secret = urllib.error.URLError("https://api.example/x?api_key=SUPERSECRETKEY12345 refused")
        with mock.patch.object(urllib.request, "urlopen", side_effect=secret):
            with self.assertRaises(urllib.error.URLError):
                self._open("discogs", "https://api.discogs.com/x", max_attempts=1)
        health = pb.provider_health()
        self.assertEqual(set(pb.POLICIES) - set(health), set())
        self.assertEqual(health["discogs"]["last_outcome"], "unavailable")
        self.assertNotIn("SUPERSECRETKEY12345", str(health))
        self.assertEqual(health["plex"]["calls"], 0)

    def test_classification_of_client_errors(self):
        self.assertEqual(pb.classify_http(404), pb.ProviderOutcome.REJECTED)
        self.assertEqual(pb.classify_http(422), pb.ProviderOutcome.REJECTED)
        self.assertEqual(pb.classify_http(408), pb.ProviderOutcome.TRANSIENT_ERROR)
        self.assertNotIn(pb.ProviderOutcome.REJECTED, pb.RETRYABLE)
        self.assertNotIn(pb.ProviderOutcome.REJECTED, pb.ANSWERS)  # a refusal is not "no match"


class NoRawProviderCallsTests(unittest.TestCase):
    """Application code reaches a provider only through the boundary."""

    ALLOWED = {"backend/provider_boundary.py",   # the boundary itself
               "backend/beets_adapter.py",       # the Beets engine client (its own typed errors)
               "backend/security.py"}            # installs the outbound URL policy on urlopen

    def _modules(self):
        files = [ROOT / "app.py", ROOT / "helpers_mb.py", ROOT / "job_engine.py"]
        files += sorted(ROOT.glob("routes_*.py")) + sorted((ROOT / "backend").glob("*.py"))
        return [f for f in files if f.relative_to(ROOT).as_posix() not in self.ALLOWED]

    def test_no_module_calls_urlopen_directly(self):
        offenders = []
        for path in self._modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    fn = node.func
                    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                    if name == "urlopen":
                        offenders.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
        self.assertEqual(offenders, [], "open provider connections with provider_boundary.opened(provider, request, ...)")

    def test_no_other_http_client_is_used(self):
        offenders = []
        for path in self._modules():
            source = path.read_text(encoding="utf-8")
            for needle in ("import requests", "http.client.HTTP", "HTTPSConnection(", "build_opener("):
                if needle in source:
                    offenders.append(f"{path.relative_to(ROOT).as_posix()}: {needle}")
        self.assertEqual(offenders, [])

    def test_every_call_names_a_known_provider(self):
        seen = set()
        for path in self._modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("opened", "opened_public")
                        and getattr(node.func.value, "id", "") == "provider_boundary"):
                    first = node.args[0]
                    self.assertIsInstance(first, ast.Constant, f"{path.name}:{node.lineno} provider must be a literal")
                    self.assertIn(first.value, pb.POLICIES, f"{path.name}:{node.lineno}")
                    seen.add(first.value)
        self.assertGreaterEqual(len(seen), 10)  # MusicBrainz, AcoustID, Discogs, Spotify, Plex, Lidarr, SLSKD, ...
        self.assertEqual(set(pb.POLICIES) - seen, set(), "a policy with no caller is dead configuration")


class ProvidersHealthRouteTests(unittest.TestCase):
    def test_route_reports_outcomes_and_policies(self):
        import app as app_module
        import routes_jobs
        pb.reset_provider_health()
        self.addCleanup(pb.reset_provider_health)
        with mock.patch.object(urllib.request, "urlopen", return_value=_Response(b"{}")):
            with pb.opened("plex", "http://plex.local/identity", timeout=3) as response:
                response.read()
        with app_module.app.test_request_context("/api/providers/health"):
            body = routes_jobs.providers_health().get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["providers"]["plex"]["last_outcome"], "confirmed")
        self.assertEqual(body["policies"]["musicbrainz"]["max_attempts"], pb.POLICIES["musicbrainz"].max_attempts)
        self.assertNotIn("plex.local", str(body))  # no URLs in the health record


if __name__ == "__main__":
    unittest.main()
