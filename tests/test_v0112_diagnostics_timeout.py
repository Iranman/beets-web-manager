"""Regression tests for BUG-3 (v0.1.12) and its hotfix v0.1.17 follow-on
(BUG-1/BUG-2/BUG-3 of the v0.1.16 TrueNAS production incident).

BUG-3 (v0.1.12) history: the control agent used to launch a fresh `beet
version` subprocess (default 5s timeout) on *every* /status request and
every capability-gated /commands/execute or /jobs/create call, over a
single-threaded HTTPServer. Under real host load (v0.1.11 TrueNAS
rollout: `beet version` took up to ~29s vs ~0.5s warm), a single slow
probe blocked every other request behind it, including Docker's own
cheap /health liveness check.

Hotfix v0.1.17 history: a real v0.1.16 TrueNAS deployment showed this was
not fully fixed -- `beet version` measured at 47.06s (21.6s even with
every plugin disabled) under real host load. The v0.1.12 fix cached the
probe result but still ran it *synchronously*, under the same lock, on
whichever request thread missed the cache -- so /status (and /version,
which used to call the same cached-but-synchronous path) could still
block for up to the probe timeout. This file now covers the fully
non-blocking, background-single-flight redesign:

- /version never launches `beet version` at all (BUG-1).
- /status/get_loaded_beet_plugins never run the probe on the calling
  thread; a stale/missing cache schedules exactly one background refresh
  and returns immediately (BUG-2).
- A capability check that hasn't yet gotten a definitive answer reports
  "diagnostics pending" (retryable) rather than misreporting a plugin as
  confirmed unavailable (BUG-3).
"""
import http.client
import json
import subprocess
import threading
import time
import unittest
from unittest import mock

import backend.beets_control_agent as control_agent_module


def _reset_diagnostics_state():
    """Reset the module-level diagnostics cache/refresh state between tests.

    Must first WAIT for any background refresh thread still in flight from
    a previous test (every test in this file that starts one bounds its
    own mocked subprocess.run to at most a few seconds, specifically so
    this wait is never unbounded) rather than merely overwriting the
    bookkeeping variables -- a leaked thread that is still running when
    this resets the cache to None will later finish and write its own
    (from that earlier test's mock) result into the freshly-reset cache,
    corrupting whichever test runs next. This is a real flake observed on
    CI (not locally): test_completed_run_with_plugin_failures_is_still_cached_directly
    and test_cold_status_returns_promptly_even_if_probe_would_take_47_seconds
    both intermittently saw stale data from an earlier test's leaked
    thread before this fix.
    """
    control_agent_module._BEET_VERSION_REFRESH_DONE_EVENT.wait(timeout=12)
    control_agent_module._BEET_VERSION_CACHE = None
    control_agent_module._BEET_VERSION_CACHE_TS = 0.0
    control_agent_module._BEET_VERSION_LAST_REFRESH_ERROR = ""
    control_agent_module._BEET_VERSION_REFRESH_ACTIVE = False
    control_agent_module._BEET_VERSION_REFRESH_DONE_EVENT.set()


def _fake_run(stdout="beets version 2.13.1\nplugins: chroma, fetchart, mbsync\n", returncode=0):
    return mock.MagicMock(returncode=returncode, stdout=stdout, stderr="")


def _wait_until(predicate, *, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class DiagnosticProbeTimeoutTests(unittest.TestCase):
    """_beet_version_snapshot() behavior in isolation, independent of
    caching -- unaffected by the async redesign, since this is the
    function the background worker calls."""

    def test_fast_probe_success(self):
        with mock.patch.object(control_agent_module.subprocess, "run", return_value=_fake_run()):
            snap = control_agent_module._beet_version_snapshot(timeout=5)
        self.assertTrue(snap["available"])
        self.assertFalse(snap["timed_out"])
        self.assertIn("chroma", snap["loaded_plugins"])

    def test_slow_but_within_timeout_still_succeeds(self):
        def slow_run(*args, **kwargs):
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
        with mock.patch.object(control_agent_module.subprocess, "run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["beet", "version"], timeout=5)
            control_agent_module._beet_version_snapshot(timeout=5)
        args, kwargs = mock_run.call_args
        self.assertIn("timeout", kwargs)


class BackgroundRefreshCachingTests(unittest.TestCase):
    """_cached_beet_version_snapshot()'s non-blocking, background-single-
    flight contract (hotfix v0.1.17, BUG-2)."""

    def setUp(self):
        _reset_diagnostics_state()
        self.addCleanup(_reset_diagnostics_state)

    def test_cold_start_returns_immediately_without_running_the_probe_on_this_thread(self):
        """The exact production symptom: a probe that would take 47s must
        not block the caller at all -- it only gets scheduled in the
        background."""
        release = threading.Event()

        def blocking_run(*args, **kwargs):
            release.wait(timeout=5)
            return _fake_run()

        with mock.patch.object(control_agent_module.subprocess, "run", side_effect=blocking_run):
            t0 = time.monotonic()
            result = control_agent_module._cached_beet_version_snapshot()
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 0.5, "cold call must return near-instantly, not wait for the probe")
            self.assertTrue(result["diagnostics_pending"])
            self.assertEqual(result["loaded_plugins"], [])
            release.set()

    def test_cold_start_probe_eventually_populates_the_cache(self):
        with mock.patch.object(control_agent_module.subprocess, "run", return_value=_fake_run()) as mock_run:
            control_agent_module._cached_beet_version_snapshot()
            self.assertTrue(_wait_until(lambda: control_agent_module._BEET_VERSION_CACHE is not None))
            result = control_agent_module._cached_beet_version_snapshot()
            self.assertTrue(result["diagnostics_fresh"])
            self.assertEqual(result["loaded_plugins"], ["chroma", "fetchart", "mbsync"])
        mock_run.assert_called_once()

    def test_second_call_within_ttl_uses_cache_not_a_new_subprocess(self):
        with mock.patch.object(control_agent_module.subprocess, "run", return_value=_fake_run()) as mock_run:
            control_agent_module._cached_beet_version_snapshot(max_wait_seconds=2.0)
            result = control_agent_module._cached_beet_version_snapshot()
        mock_run.assert_called_once()
        self.assertTrue(result["diagnostics_fresh"])
        self.assertGreaterEqual(result["diagnostics_cache_age_seconds"], 0.0)

    def test_forced_refresh_schedules_a_new_background_probe(self):
        with mock.patch.object(control_agent_module.subprocess, "run", return_value=_fake_run()) as mock_run:
            control_agent_module._cached_beet_version_snapshot(max_wait_seconds=2.0)
            control_agent_module._cached_beet_version_snapshot(force=True, max_wait_seconds=2.0)
        self.assertEqual(mock_run.call_count, 2)

    def test_forced_refresh_does_not_block_even_though_it_schedules_a_probe(self):
        release = threading.Event()

        def blocking_run(*args, **kwargs):
            release.wait(timeout=5)
            return _fake_run()

        with mock.patch.object(control_agent_module.subprocess, "run", return_value=_fake_run()):
            control_agent_module._cached_beet_version_snapshot(max_wait_seconds=2.0)

        with mock.patch.object(control_agent_module.subprocess, "run", side_effect=blocking_run):
            t0 = time.monotonic()
            result = control_agent_module._cached_beet_version_snapshot(force=True)
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 0.5, "force=True must schedule, not wait for, the refresh")
            # The previous known-good cache is still what's served while the
            # forced refresh runs in the background.
            self.assertEqual(result["loaded_plugins"], ["chroma", "fetchart", "mbsync"])
            self.assertTrue(result["diagnostics_pending"])
            release.set()

    def test_transient_timeout_falls_back_to_known_good_cached_plugins(self):
        """The exact BUG-3 symptom: a good result is cached, then a probe
        times out -- the cache must keep serving the known-good plugin
        list, never silently replace it with an empty/failed result."""
        with mock.patch.object(control_agent_module.subprocess, "run", return_value=_fake_run()):
            good = control_agent_module._cached_beet_version_snapshot(max_wait_seconds=2.0)
        self.assertEqual(good["loaded_plugins"], ["chroma", "fetchart", "mbsync"])

        control_agent_module._BEET_VERSION_CACHE_TS -= (control_agent_module._BEET_VERSION_CACHE_TTL_SECONDS + 1)
        with mock.patch.object(control_agent_module.subprocess, "run",
                                side_effect=subprocess.TimeoutExpired(cmd=["beet", "version"], timeout=90)):
            degraded = control_agent_module._cached_beet_version_snapshot(max_wait_seconds=2.0)

        self.assertFalse(degraded["diagnostics_fresh"])
        self.assertEqual(degraded["loaded_plugins"], ["chroma", "fetchart", "mbsync"],
                          "a transient timeout must not erase known-good cached plugin data")
        self.assertTrue(degraded["available"])
        self.assertIn("timed out", degraded["diagnostics_refresh_error"].lower())

    def test_timeout_with_no_prior_cache_reports_real_failure(self):
        """Cold start (no known-good cache yet) whose one and only probe
        times out must report the genuine failure once resolved -- there
        is no known-good data to fall back to."""
        with mock.patch.object(control_agent_module.subprocess, "run",
                                side_effect=subprocess.TimeoutExpired(cmd=["beet", "version"], timeout=90)):
            result = control_agent_module._cached_beet_version_snapshot(max_wait_seconds=2.0)
        self.assertFalse(result["diagnostics_fresh"])
        self.assertEqual(result["loaded_plugins"], [])

    def test_stale_cache_beyond_max_stale_window_is_not_trusted_forever(self):
        with mock.patch.object(control_agent_module.subprocess, "run", return_value=_fake_run()):
            control_agent_module._cached_beet_version_snapshot(max_wait_seconds=2.0)

        control_agent_module._BEET_VERSION_CACHE_TS -= (control_agent_module._BEET_VERSION_CACHE_MAX_STALE_SECONDS + 1)
        with mock.patch.object(control_agent_module.subprocess, "run",
                                side_effect=subprocess.TimeoutExpired(cmd=["beet", "version"], timeout=90)):
            result = control_agent_module._cached_beet_version_snapshot(max_wait_seconds=2.0)

        self.assertFalse(result["diagnostics_fresh"])
        self.assertEqual(result["loaded_plugins"], [], "cached data past the max-stale window must not be trusted")

    def test_completed_run_with_plugin_failures_is_still_cached_directly(self):
        stdout_with_failure = "beets version 2.13.1\nplugins: fetchart\nUserWarning: chroma failed to load\n"
        with mock.patch.object(control_agent_module.subprocess, "run",
                                return_value=_fake_run(stdout=stdout_with_failure)):
            result = control_agent_module._cached_beet_version_snapshot(max_wait_seconds=2.0)
        self.assertTrue(result["diagnostics_fresh"])
        self.assertEqual(result["loaded_plugins"], ["fetchart"])

    def test_ten_simultaneous_status_calls_trigger_exactly_one_background_probe(self):
        """N simultaneous callers past a cold/expired cache must trigger
        exactly one subprocess launch, not N -- proves single-flight
        collapses concurrent background refreshes into one probe, not
        just one *synchronous* refresh as the old test asserted."""
        call_count = {"n": 0}
        lock = threading.Lock()

        def counted_run(*args, **kwargs):
            with lock:
                call_count["n"] += 1
            time.sleep(0.1)
            return _fake_run()

        with mock.patch.object(control_agent_module.subprocess, "run", side_effect=counted_run):
            threads = [threading.Thread(target=control_agent_module._cached_beet_version_snapshot) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
            self.assertTrue(_wait_until(lambda: call_count["n"] >= 1, timeout=3))
            # Give any accidental extra probe a moment to have started too.
            time.sleep(0.3)

        self.assertEqual(call_count["n"], 1, "single-flight must collapse concurrent refreshes into one probe")

    def test_refresh_does_not_falsely_report_plugin_as_disabled_while_pending(self):
        """BUG-3: an empty loaded_plugins list while diagnostics_pending is
        True must not be read as "no plugins are loaded" -- it means
        "we don't know yet." get_loaded_beet_plugins()/status consumers
        must have diagnostics_pending available to make that distinction."""
        release = threading.Event()

        def blocking_run(*args, **kwargs):
            release.wait(timeout=5)
            return _fake_run()

        with mock.patch.object(control_agent_module.subprocess, "run", side_effect=blocking_run):
            result = control_agent_module._cached_beet_version_snapshot()
            self.assertEqual(result["loaded_plugins"], [])
            self.assertTrue(result["diagnostics_pending"], "must be flagged as pending, not a confirmed empty result")
            release.set()


class CapabilityGateDiagnosticsPendingTests(unittest.TestCase):
    """BUG-3 (hotfix v0.1.17): require_command_capability()/
    get_loaded_beet_plugins() must not convert "diagnostics still
    initializing" into "capability confirmed unavailable"."""

    def setUp(self):
        _reset_diagnostics_state()
        self.addCleanup(_reset_diagnostics_state)

    def test_capability_check_briefly_awaits_an_in_flight_refresh_then_succeeds(self):
        def quick_run(*args, **kwargs):
            time.sleep(0.2)
            return _fake_run(stdout="beets version 2.13.1\nplugins: chroma\n")

        with mock.patch.object(control_agent_module.subprocess, "run", side_effect=quick_run):
            error = control_agent_module.require_command_capability("submit")
        self.assertIsNone(error, "a refresh that finishes within the bounded wait must resolve the capability check")

    def test_capability_check_reports_pending_not_genuinely_unavailable_when_still_cold(self):
        """A probe slower than the bounded capability wait must not cause
        a false 409 "capability unavailable" -- it must report a distinct,
        retryable "diagnostics pending" outcome instead."""
        release = threading.Event()

        def blocking_run(*args, **kwargs):
            release.wait(timeout=5)
            return _fake_run(stdout="beets version 2.13.1\nplugins: chroma\n")

        try:
            with mock.patch.object(control_agent_module, "_BEET_VERSION_CAPABILITY_WAIT_SECONDS", 0.05), \
                 mock.patch.object(control_agent_module.subprocess, "run", side_effect=blocking_run):
                error = control_agent_module.require_command_capability("submit")
                self.assertIsNotNone(error)
                self.assertEqual(error.get("reason"), "diagnostics_pending")
                self.assertEqual(error.get("status_code"), 503)
        finally:
            release.set()

    def test_capability_check_reports_pending_not_unavailable_for_stale_cache_with_stuck_refresh(self):
        """hotfix v0.1.17 follow-up (requirement #4): a cache OBJECT
        existing is not the same as a TRUSTWORTHY cache. If the last known
        snapshot has aged past _BEET_VERSION_CACHE_MAX_STALE_SECONDS and a
        refresh trying to replace it is actively stuck/in-flight, the
        capability gate must still report "diagnostics_pending"/503 -- not
        silently fall through to treating the stale snapshot's (now
        untrustworthy) plugin list as confirmed ground truth and reporting
        a false "capability unavailable"."""
        with mock.patch.object(control_agent_module.subprocess, "run",
                                return_value=_fake_run(stdout="beets version 2.13.1\nplugins: chroma\n")):
            control_agent_module._cached_beet_version_snapshot(max_wait_seconds=2.0)
        self.assertIn("chroma", control_agent_module._BEET_VERSION_CACHE.get("loaded_plugins") or [])

        control_agent_module._BEET_VERSION_CACHE_TS -= (control_agent_module._BEET_VERSION_CACHE_MAX_STALE_SECONDS + 1)

        release = threading.Event()

        def blocking_run(*args, **kwargs):
            release.wait(timeout=5)
            return _fake_run(stdout="beets version 2.13.1\nplugins: chroma\n")

        try:
            with mock.patch.object(control_agent_module, "_BEET_VERSION_CAPABILITY_WAIT_SECONDS", 0.05), \
                 mock.patch.object(control_agent_module.subprocess, "run", side_effect=blocking_run):
                error = control_agent_module.require_command_capability("submit")
                self.assertIsNotNone(error, "a stale-past-max-stale cache with a stuck refresh must not silently pass as available")
                self.assertEqual(error.get("reason"), "diagnostics_pending")
                self.assertEqual(error.get("status_code"), 503)
        finally:
            release.set()

    def test_capability_check_reports_genuinely_unavailable_once_diagnostics_resolve(self):
        """Once a real probe has completed and the plugin truly is not in
        the loaded list, the original 409-style "capability unavailable"
        outcome must still be reported -- diagnostics_pending must not
        mask a real, confirmed unsupported capability forever."""
        with mock.patch.object(control_agent_module.subprocess, "run",
                                return_value=_fake_run(stdout="beets version 2.13.1\nplugins: fetchart\n")):
            error = control_agent_module.require_command_capability("submit")
        self.assertIsNotNone(error)
        self.assertNotEqual(error.get("reason"), "diagnostics_pending")
        self.assertNotIn("status_code", error)


class VersionEndpointNeverRunsBeetTests(unittest.TestCase):
    """BUG-1 (hotfix v0.1.17): GET /version must never launch `beet
    version` and must respond well under a second even if a hypothetical
    `beet version` call would block indefinitely."""

    def setUp(self):
        _reset_diagnostics_state()
        self.addCleanup(_reset_diagnostics_state)

    def _get(self, path):
        handler = control_agent_module.ControlAgentHandler.__new__(control_agent_module.ControlAgentHandler)
        handler.headers = {"Authorization": "Bearer test-token"}
        handler._authenticate = lambda: True
        handler.path = path
        responses = []
        handler._send_json = lambda code, data: responses.append((code, data))
        handler.do_GET()
        return responses[0]

    def test_version_never_invokes_subprocess_run(self):
        def hangs_forever(*args, **kwargs):  # pragma: no cover - must never be called
            raise AssertionError("subprocess.run must never be invoked by /version")

        with mock.patch.object(control_agent_module.subprocess, "run", side_effect=hangs_forever):
            code, data = self._get("/version")
        self.assertEqual(code, 200)
        self.assertIn("agent_version", data)
        self.assertIn("beets_version", data)

    def test_version_responds_well_under_one_second(self):
        def hangs_forever(*args, **kwargs):  # pragma: no cover
            raise AssertionError("subprocess.run must never be invoked by /version")

        with mock.patch.object(control_agent_module.subprocess, "run", side_effect=hangs_forever):
            t0 = time.monotonic()
            self._get("/version")
            elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0)

    def test_version_reports_real_installed_package_version(self):
        with mock.patch.object(control_agent_module.importlib.metadata, "version", return_value="2.13.1"):
            code, data = self._get("/version")
        self.assertEqual(code, 200)
        self.assertEqual(data["beets_version"], "2.13.1")

    def test_installed_beets_package_version_falls_back_safely(self):
        with mock.patch.object(control_agent_module.importlib.metadata, "version", side_effect=Exception("boom")):
            result = control_agent_module._installed_beets_package_version()
        self.assertEqual(result, "")


class StatusEndpointNeverBlocksTests(unittest.TestCase):
    """BUG-2 (hotfix v0.1.17): GET /status (with or without ?refresh=1)
    must return promptly even when the underlying `beet version` probe
    would take the full measured production duration (47s)."""

    def setUp(self):
        _reset_diagnostics_state()
        self.addCleanup(_reset_diagnostics_state)

    def _get(self, path):
        handler = control_agent_module.ControlAgentHandler.__new__(control_agent_module.ControlAgentHandler)
        handler.headers = {"Authorization": "Bearer test-token"}
        handler._authenticate = lambda: True
        handler.path = path
        responses = []
        handler._send_json = lambda code, data: responses.append((code, data))
        handler.do_GET()
        return responses[0]

    def test_cold_status_returns_promptly_even_if_probe_would_take_47_seconds(self):
        release = threading.Event()

        def slow_like_production(*args, **kwargs):
            release.wait(timeout=10)
            return _fake_run()

        with mock.patch.object(control_agent_module.subprocess, "run", side_effect=slow_like_production):
            t0 = time.monotonic()
            code, data = self._get("/status")
            elapsed = time.monotonic() - t0
            self.assertEqual(code, 200)
            self.assertLess(elapsed, 1.0, "/status must not wait for the 47s-class production probe")
            self.assertTrue(data.get("diagnostics_pending"))
            release.set()

    def test_plain_status_call_reuses_cache_not_a_new_probe(self):
        with mock.patch.object(control_agent_module.subprocess, "run", return_value=_fake_run()) as mock_run:
            self._get("/status")
            self.assertTrue(_wait_until(lambda: control_agent_module._BEET_VERSION_CACHE is not None))
            self._get("/status")
        mock_run.assert_called_once()

    def test_refresh_param_schedules_but_does_not_block(self):
        with mock.patch.object(control_agent_module.subprocess, "run", return_value=_fake_run()):
            self._get("/status")
            self.assertTrue(_wait_until(lambda: control_agent_module._BEET_VERSION_CACHE is not None))

        release = threading.Event()

        def blocking_run(*args, **kwargs):
            release.wait(timeout=5)
            return _fake_run()

        with mock.patch.object(control_agent_module.subprocess, "run", side_effect=blocking_run) as mock_run:
            t0 = time.monotonic()
            code, data = self._get("/status?refresh=1")
            elapsed = time.monotonic() - t0
            self.assertEqual(code, 200)
            self.assertLess(elapsed, 0.5, "?refresh=1 must schedule a refresh, not wait for it")
            release.set()
            self.assertTrue(_wait_until(lambda: mock_run.call_count >= 1))

    def test_diagnostics_eventually_update_after_background_probe_completes(self):
        with mock.patch.object(control_agent_module.subprocess, "run",
                                return_value=_fake_run(stdout="beets version 2.13.1\nplugins: chroma\n")):
            self._get("/status")
            self.assertTrue(_wait_until(lambda: control_agent_module._BEET_VERSION_CACHE is not None))

        with mock.patch.object(control_agent_module.subprocess, "run",
                                return_value=_fake_run(stdout="beets version 2.13.1\nplugins: chroma, fetchart, mbsync\n")):
            control_agent_module._BEET_VERSION_CACHE_TS -= (control_agent_module._BEET_VERSION_CACHE_TTL_SECONDS + 1)
            _, first = self._get("/status?refresh=1")
            self.assertTrue(_wait_until(lambda: control_agent_module._BEET_VERSION_CACHE.get("loaded_plugins") == ["chroma", "fetchart", "mbsync"]))
            _, second = self._get("/status")
        self.assertEqual(second["loaded_plugins"], ["chroma", "fetchart", "mbsync"])
        self.assertTrue(second["diagnostics_fresh"])

    def test_pending_diagnostics_do_not_falsely_report_plugin_disabled(self):
        release = threading.Event()

        def blocking_run(*args, **kwargs):
            release.wait(timeout=5)
            return _fake_run()

        with mock.patch.object(control_agent_module.subprocess, "run", side_effect=blocking_run):
            code, data = self._get("/status")
            self.assertTrue(data["diagnostics_pending"])
            self.assertFalse(data["plugins"]["chroma"], "flag itself may be False while pending")
            # But the payload must make it possible to distinguish "not
            # confirmed yet" from "confirmed disabled" -- diagnostics_pending
            # is that signal, and it must be True here.
            release.set()


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

    def test_health_responds_while_status_handler_is_slow(self):
        started = threading.Event()

        def slow_status(*, force_refresh=False):
            started.set()
            time.sleep(0.75)
            return {"status": "ok", "service": "beets-control-agent"}

        token = "threaded-health-test-token-000000000000"
        httpd = control_agent_module.ThreadingHTTPServer(("127.0.0.1", 0), control_agent_module.ControlAgentHandler)
        httpd.daemon_threads = True
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()
        port = httpd.server_address[1]
        status_errors = []

        def call_status():
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                try:
                    conn.request("GET", "/status", headers={"Authorization": f"Bearer {token}"})
                    resp = conn.getresponse()
                    resp.read()
                finally:
                    conn.close()
            except Exception as exc:  # pragma: no cover - failure path only
                status_errors.append(exc)

        try:
            with mock.patch.object(control_agent_module, "BEETS_API_TOKEN", token), \
                 mock.patch.object(control_agent_module, "_agent_status_payload", side_effect=slow_status):
                status_thread = threading.Thread(target=call_status)
                status_thread.start()
                self.assertTrue(started.wait(1), "slow /status request did not start")

                t0 = time.monotonic()
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                try:
                    conn.request("GET", "/health")
                    resp = conn.getresponse()
                    body = resp.read().decode("utf-8")
                finally:
                    conn.close()
                elapsed = time.monotonic() - t0

                status_thread.join(timeout=3)

            self.assertIn("beets-control-agent", body)
            self.assertLess(elapsed, 0.5, "/health should not queue behind a slow /status request")
            self.assertEqual(status_errors, [])
        finally:
            httpd.shutdown()
            httpd.server_close()
            server_thread.join(timeout=3)


class StartupPrewarmTests(unittest.TestCase):
    """Hotfix v0.1.17: the control agent should kick off one background
    diagnostics refresh at startup, without delaying server startup."""

    def setUp(self):
        _reset_diagnostics_state()
        self.addCleanup(_reset_diagnostics_state)

    def test_prewarm_schedules_a_background_refresh_without_blocking(self):
        release = threading.Event()

        def blocking_run(*args, **kwargs):
            release.wait(timeout=5)
            return _fake_run()

        with mock.patch.object(control_agent_module.subprocess, "run", side_effect=blocking_run) as mock_run:
            t0 = time.monotonic()
            control_agent_module.prewarm_beet_version_cache()
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 0.5)
            release.set()
            self.assertTrue(_wait_until(lambda: mock_run.call_count >= 1))


class BrokenPipeSendJsonTests(unittest.TestCase):
    """BUG-6 (hotfix v0.1.17): _send_json() must handle an expected client
    disconnect (BrokenPipeError/ConnectionResetError) without an unhandled
    traceback escaping the request thread, while still letting a genuine,
    unrelated bug surface normally."""

    def _handler(self):
        handler = control_agent_module.ControlAgentHandler.__new__(control_agent_module.ControlAgentHandler)
        handler.send_response = mock.MagicMock()
        handler.send_header = mock.MagicMock()
        handler.end_headers = mock.MagicMock()
        return handler

    def test_broken_pipe_during_write_does_not_raise(self):
        handler = self._handler()
        handler.wfile = mock.MagicMock()
        handler.wfile.write.side_effect = BrokenPipeError(32, "Broken pipe")
        try:
            handler._send_json(200, {"ok": True})
        except BrokenPipeError:
            self.fail("_send_json() must catch an expected BrokenPipeError, not let it propagate")

    def test_connection_reset_during_write_does_not_raise(self):
        handler = self._handler()
        handler.wfile = mock.MagicMock()
        handler.wfile.write.side_effect = ConnectionResetError(104, "Connection reset by peer")
        try:
            handler._send_json(200, {"ok": True})
        except ConnectionResetError:
            self.fail("_send_json() must catch an expected ConnectionResetError, not let it propagate")

    def test_broken_pipe_is_logged_concisely(self):
        handler = self._handler()
        handler.wfile = mock.MagicMock()
        handler.wfile.write.side_effect = BrokenPipeError(32, "Broken pipe")
        with mock.patch("builtins.print") as mock_print:
            handler._send_json(200, {"ok": True})
        self.assertTrue(mock_print.called)
        logged = " ".join(str(call.args[0]) for call in mock_print.call_args_list)
        self.assertIn("disconnected", logged.lower())

    def test_unrelated_exception_during_write_still_propagates(self):
        """A genuine server bug must remain visible -- only the two
        expected disconnect exception types are caught."""
        handler = self._handler()
        handler.wfile = mock.MagicMock()
        handler.wfile.write.side_effect = ValueError("something actually broke")
        with self.assertRaises(ValueError):
            handler._send_json(200, {"ok": True})

    def test_successful_write_is_unaffected(self):
        handler = self._handler()
        handler.wfile = mock.MagicMock()
        handler._send_json(200, {"ok": True})
        self.assertTrue(handler.wfile.write.called)
        written = handler.wfile.write.call_args[0][0]
        self.assertEqual(json.loads(written.decode("utf-8")), {"ok": True})


if __name__ == "__main__":
    unittest.main()
