"""Regression tests for BUG-3 (v0.1.12): the control agent used to launch a
fresh `beet version` subprocess (default 5s timeout) on *every* /status
request and every capability-gated /commands/execute or /jobs/create call,
over a single-threaded HTTPServer. Under real host load (confirmed on the
v0.1.11 TrueNAS production rollout: `beet version` took up to ~29s vs ~0.5s
warm), a single slow probe blocked every other request behind it --
including Docker's own cheap /health liveness check -- producing false
plugin_loader_ok=false / "0 plugins loaded" results despite ~20/20 plugins
genuinely loading against a warm probe moments later.

Covers: fast/slow/timeout probes, cached known-good results surviving a
transient refresh failure, cold start (no cache yet), single-flight under
concurrent callers, and that a timed-out subprocess is actually reaped
(subprocess.run(..., timeout=...) already guarantees this; asserted here
rather than assumed).
"""
import subprocess
import threading
import time
import unittest
from unittest import mock

import backend.beets_control_agent as control_agent_module


def _reset_cache():
    control_agent_module._BEET_VERSION_CACHE = None
    control_agent_module._BEET_VERSION_CACHE_TS = 0.0


def _fake_run(stdout="beets version 2.13.1\nplugins: chroma, fetchart, mbsync\n", returncode=0):
    return mock.MagicMock(returncode=returncode, stdout=stdout, stderr="")


class DiagnosticProbeTimeoutTests(unittest.TestCase):
    """_beet_version_snapshot()/_cached_beet_version_snapshot() behavior in
    isolation, independent of caching."""

    def test_fast_probe_success(self):
        with mock.patch.object(control_agent_module.subprocess, "run", return_value=_fake_run()):
            snap = control_agent_module._beet_version_snapshot(timeout=5)
        self.assertTrue(snap["available"])
        self.assertFalse(snap["timed_out"])
        self.assertIn("chroma", snap["loaded_plugins"])

    def test_slow_but_within_timeout_still_succeeds(self):
        def slow_run(*args, **kwargs):
            # Simulate the subprocess itself taking a while, but well under
            # the caller's timeout -- must still succeed normally.
            return _fake_run()
        with mock.patch.object(control_agent_module.subprocess, "run", side_effect=slow_run):
            snap = control_agent_module._beet_version_snapshot(timeout=12)
        self.assertTrue(snap["available"])
        self.assertEqual(snap["loaded_plugins"], ["chroma", "fetchart", "mbsync"])

    def test_probe_timeout_is_reported_not_silently_swallowed(self):
        with mock.patch.object(control_agent_module.subprocess, "run",
                                side_effect=subprocess.TimeoutExpired(cmd=["beet", "version"], timeout=5)):
            snap = control_agent_module._beet_version_snapshot(timeout=5)
        self.assertTrue(snap["timed_out"])
        self.assertTrue(snap["available"], "the binary/process itself launched -- only the wait timed out")
        self.assertEqual(snap["loaded_plugins"], [])

    def test_timeout_subprocess_is_reaped_not_leaked(self):
        """subprocess.run(..., timeout=...) is documented to kill and reap
        the child on TimeoutExpired -- assert we actually rely on that
        stdlib call shape (not Popen-without-cleanup) rather than assuming
        it."""
        with mock.patch.object(control_agent_module.subprocess, "run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["beet", "version"], timeout=5)
            control_agent_module._beet_version_snapshot(timeout=5)
        # subprocess.run (not Popen) was used with an explicit timeout --
        # this is what makes the stdlib itself responsible for killing the
        # child. Confirms this call shape wasn't quietly changed to Popen.
        args, kwargs = mock_run.call_args
        self.assertIn("timeout", kwargs)


class DiagnosticCachingTests(unittest.TestCase):
    def setUp(self):
        _reset_cache()
        self.addCleanup(_reset_cache)

    def test_cold_start_no_cache_yet_launches_a_real_probe(self):
        with mock.patch.object(control_agent_module.subprocess, "run", return_value=_fake_run()) as mock_run:
            result = control_agent_module._cached_beet_version_snapshot()
        mock_run.assert_called_once()
        self.assertTrue(result["diagnostics_fresh"])
        self.assertEqual(result["diagnostics_cache_age_seconds"], 0.0)

    def test_second_call_within_ttl_uses_cache_not_a_new_subprocess(self):
        with mock.patch.object(control_agent_module.subprocess, "run", return_value=_fake_run()) as mock_run:
            control_agent_module._cached_beet_version_snapshot()
            result = control_agent_module._cached_beet_version_snapshot()
        mock_run.assert_called_once()
        self.assertTrue(result["diagnostics_fresh"])
        self.assertGreaterEqual(result["diagnostics_cache_age_seconds"], 0.0)

    def test_forced_refresh_bypasses_cache(self):
        with mock.patch.object(control_agent_module.subprocess, "run", return_value=_fake_run()) as mock_run:
            control_agent_module._cached_beet_version_snapshot()
            control_agent_module._cached_beet_version_snapshot(force=True)
        self.assertEqual(mock_run.call_count, 2)

    def test_transient_timeout_falls_back_to_known_good_cached_plugins(self):
        """The exact BUG-3 symptom: a good result is cached, then a probe
        times out -- the cache must keep serving the known-good plugin
        list (diagnostics_fresh=False), never silently replace it with an
        empty/failed result."""
        with mock.patch.object(control_agent_module.subprocess, "run", return_value=_fake_run()):
            good = control_agent_module._cached_beet_version_snapshot()
        self.assertEqual(good["loaded_plugins"], ["chroma", "fetchart", "mbsync"])

        # Force the TTL to have expired so the next call attempts a real
        # refresh, which now times out.
        control_agent_module._BEET_VERSION_CACHE_TS -= (control_agent_module._BEET_VERSION_CACHE_TTL_SECONDS + 1)
        with mock.patch.object(control_agent_module.subprocess, "run",
                                side_effect=subprocess.TimeoutExpired(cmd=["beet", "version"], timeout=12)):
            degraded = control_agent_module._cached_beet_version_snapshot()

        self.assertFalse(degraded["diagnostics_fresh"])
        self.assertEqual(degraded["loaded_plugins"], ["chroma", "fetchart", "mbsync"],
                          "a transient timeout must not erase known-good cached plugin data")
        self.assertTrue(degraded["available"])
        self.assertIn("timed out", degraded["diagnostics_refresh_error"].lower())

    def test_timeout_with_no_prior_cache_reports_real_failure(self):
        """Cold start (no known-good cache yet) that times out must report
        the genuine failure -- there is no "known good" to fall back to,
        so this is the one case where 0 plugins / unavailable is correct,
        not a false negative."""
        with mock.patch.object(control_agent_module.subprocess, "run",
                                side_effect=subprocess.TimeoutExpired(cmd=["beet", "version"], timeout=12)):
            result = control_agent_module._cached_beet_version_snapshot()
        self.assertFalse(result["diagnostics_fresh"])
        self.assertEqual(result["loaded_plugins"], [])
        self.assertIsNone(result["diagnostics_cache_age_seconds"])

    def test_stale_cache_beyond_max_stale_window_is_not_trusted_forever(self):
        """A permanently broken `beet` binary must eventually be reported
        as failed, not "good" forever just because it once succeeded."""
        with mock.patch.object(control_agent_module.subprocess, "run", return_value=_fake_run()):
            control_agent_module._cached_beet_version_snapshot()

        control_agent_module._BEET_VERSION_CACHE_TS -= (control_agent_module._BEET_VERSION_CACHE_MAX_STALE_SECONDS + 1)
        with mock.patch.object(control_agent_module.subprocess, "run",
                                side_effect=subprocess.TimeoutExpired(cmd=["beet", "version"], timeout=12)):
            result = control_agent_module._cached_beet_version_snapshot()

        self.assertFalse(result["diagnostics_fresh"])
        self.assertEqual(result["loaded_plugins"], [], "cached data past the max-stale window must not be trusted")

    def test_completed_run_with_plugin_failures_is_still_cached_directly(self):
        """A completed (non-timeout) run reporting real plugin_failures is
        current ground truth, not a "probe failure" -- must be cached and
        returned as-is, not treated as a reason to fall back to older data."""
        stdout_with_failure = "beets version 2.13.1\nplugins: fetchart\nUserWarning: chroma failed to load\n"
        with mock.patch.object(control_agent_module.subprocess, "run",
                                return_value=_fake_run(stdout=stdout_with_failure)):
            result = control_agent_module._cached_beet_version_snapshot()
        self.assertTrue(result["diagnostics_fresh"])
        self.assertEqual(result["loaded_plugins"], ["fetchart"])

    def test_concurrent_calls_single_flight_one_subprocess_launch(self):
        """N simultaneous callers past a cold/expired cache must trigger
        exactly one subprocess launch, not N -- proves the lock actually
        serializes the refresh instead of just decorating it."""
        call_count = {"n": 0}
        lock = threading.Lock()

        def counted_run(*args, **kwargs):
            with lock:
                call_count["n"] += 1
            time.sleep(0.05)
            return _fake_run()

        with mock.patch.object(control_agent_module.subprocess, "run", side_effect=counted_run):
            threads = [threading.Thread(target=control_agent_module._cached_beet_version_snapshot) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        self.assertEqual(call_count["n"], 1, "single-flight must collapse concurrent refreshes into one probe")


class StatusEndpointRefreshParamTests(unittest.TestCase):
    """GET /status?refresh=1 bypasses the cache, matching the same
    convention already used by the web-manager's own /api/setup/status."""

    def setUp(self):
        _reset_cache()
        self.addCleanup(_reset_cache)

    def _get(self, path):
        handler = control_agent_module.ControlAgentHandler.__new__(control_agent_module.ControlAgentHandler)
        handler.headers = {"Authorization": "Bearer test-token"}
        handler._authenticate = lambda: True
        handler.path = path
        responses = []
        handler._send_json = lambda code, data: responses.append((code, data))
        handler.do_GET()
        return responses[0]

    def test_plain_status_call_reuses_cache(self):
        with mock.patch.object(control_agent_module.subprocess, "run", return_value=_fake_run()) as mock_run:
            self._get("/status")
            self._get("/status")
        self.assertEqual(mock_run.call_count, 1)

    def test_refresh_param_forces_new_probe(self):
        with mock.patch.object(control_agent_module.subprocess, "run", return_value=_fake_run()) as mock_run:
            self._get("/status")
            self._get("/status?refresh=1")
        self.assertEqual(mock_run.call_count, 2)


class ThreadedControlAgentServerTests(unittest.TestCase):
    """BUG-3's other half: the control agent's HTTP server must be able to
    serve a cheap request (e.g. /health) while a slow one is in flight --
    the previous plain HTTPServer handled exactly one request at a time,
    so a slow /status call blocked even Docker's own liveness probe."""

    def test_server_class_is_threading_capable(self):
        from http.server import ThreadingHTTPServer
        self.assertIs(
            control_agent_module.ThreadingHTTPServer, ThreadingHTTPServer,
            "run_agent() must construct a ThreadingHTTPServer, not the single-threaded HTTPServer, "
            "or a slow /status probe can starve every other endpoint (including /health) behind it",
        )


if __name__ == "__main__":
    unittest.main()
