"""Tests for /api/library pagination and /api/setup/status caching."""
import threading
import time
import unittest
from unittest import mock
import app as app_module
import routes_setup


class LibraryPaginationTests(unittest.TestCase):
    def setUp(self):
        self.env_patcher = mock.patch.dict("os.environ", {"BEETS_WEB_AUTH_DISABLED": "1"})
        self.env_patcher.start()
        self.client = app_module.app.test_client()

    def tearDown(self):
        self.env_patcher.stop()

    def test_library_with_limit_returns_paginated_payload(self):
        fake_items = [
            {"id": 1, "title": "Track 1", "album_id": 10, "path": "/data/media/music/Artist/Album/01.flac", "length": 200},
            {"id": 2, "title": "Track 2", "album_id": 10, "path": "/data/media/music/Artist/Album/02.flac", "length": 210},
        ]

        def fake_get_items_page(offset=0, limit=50):
            return {
                "items": fake_items[:limit],
                "offset": offset,
                "limit": limit,
                "returned": len(fake_items[:limit]),
                "total": 3144,
            }

        with mock.patch.object(app_module.beets_client, "get_items_page", side_effect=fake_get_items_page):
            t0 = time.time()
            res = self.client.get("/api/library?limit=1")
            elapsed = time.time() - t0

            self.assertEqual(res.status_code, 200)
            self.assertLess(elapsed, 2.0, "GET /api/library?limit=1 must complete in under 2 seconds")

            data = res.get_json()
            self.assertIn("items", data)
            self.assertIn("pagination", data)
            self.assertEqual(data["pagination"]["limit"], 1)
            self.assertEqual(data["pagination"]["returned"], 1)
            self.assertEqual(data["pagination"]["total"], 3144)

    def test_library_limit_is_capped(self):
        def fake_get_items_page(offset=0, limit=50):
            return {
                "items": [],
                "offset": offset,
                "limit": limit,
                "returned": 0,
                "total": 3144,
            }

        with mock.patch.object(app_module.beets_client, "get_items_page", side_effect=fake_get_items_page) as mock_page:
            res = self.client.get("/api/library?limit=9999")
            self.assertEqual(res.status_code, 200)
            mock_page.assert_called_once_with(offset=0, limit=500)


class SetupStatusCacheTests(unittest.TestCase):
    def setUp(self):
        self.env_patcher = mock.patch.dict("os.environ", {"BEETS_WEB_AUTH_DISABLED": "1"})
        self.env_patcher.start()
        self.client = app_module.app.test_client()
        routes_setup._STATUS_CACHE_DATA = None
        routes_setup._STATUS_CACHE_TS = 0.0

    def tearDown(self):
        self.env_patcher.stop()

    def test_setup_status_caches_responses(self):
        fake_payload = {"ok": True, "status": "ready", "version": "1.0", "paths": {}, "beets": {"remote_reachable": True}}

        with mock.patch.object(routes_setup, "_build_setup_status_payload", return_value=fake_payload) as mock_build:
            res1 = self.client.get("/api/setup/status")
            self.assertEqual(res1.status_code, 200)
            data1 = res1.get_json()
            self.assertFalse(data1.get("cached"))

            res2 = self.client.get("/api/setup/status")
            self.assertEqual(res2.status_code, 200)
            data2 = res2.get_json()
            self.assertTrue(data2.get("cached"))

            # Only one underlying build call was made
            self.assertEqual(mock_build.call_count, 1)

    def test_setup_status_force_refresh_bypasses_cache(self):
        fake_payload = {"ok": True, "status": "ready", "version": "1.0", "paths": {}, "beets": {"remote_reachable": True}}

        with mock.patch.object(routes_setup, "_build_setup_status_payload", return_value=fake_payload) as mock_build:
            self.client.get("/api/setup/status")
            res = self.client.get("/api/setup/status?refresh=1")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertFalse(data.get("cached"))
            self.assertEqual(mock_build.call_count, 2)

    def test_setup_status_uses_monotonic_clock_not_wall_clock(self):
        """A backward wall-clock jump must not make a fresh cache look expired
        (or a forward jump make it look valid for too long)."""
        fake_payload = {"ok": True, "status": "ready", "version": "1.0", "paths": {}, "beets": {"remote_reachable": True}}

        with mock.patch.object(routes_setup, "_build_setup_status_payload", return_value=fake_payload) as mock_build, \
             mock.patch("time.time", side_effect=AssertionError("must not read wall-clock time.time() for cache aging")):
            res1 = self.client.get("/api/setup/status")
            self.assertEqual(res1.status_code, 200)
            res2 = self.client.get("/api/setup/status")
            self.assertEqual(res2.status_code, 200)
            self.assertTrue(res2.get_json().get("cached"))
            self.assertEqual(mock_build.call_count, 1)

    def test_setup_status_concurrent_requests_do_not_stampede_the_builder(self):
        """N concurrent requests past TTL must trigger exactly one upstream
        rebuild, not N independent ones (cache-stampede prevention)."""
        fake_payload = {"ok": True, "status": "ready", "version": "1.0", "paths": {}, "beets": {"remote_reachable": True}}
        build_calls = []
        build_lock = threading.Lock()

        def slow_build():
            with build_lock:
                build_calls.append(1)
            time.sleep(0.05)
            return dict(fake_payload)

        with mock.patch.object(routes_setup, "_build_setup_status_payload", side_effect=slow_build):
            threads = [threading.Thread(target=lambda: self.client.get("/api/setup/status")) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        self.assertEqual(len(build_calls), 1, "concurrent requests past TTL must single-flight, not stampede")

    def test_setup_status_serves_stale_snapshot_labeled_on_rebuild_failure(self):
        good_payload = {"ok": True, "status": "ready", "version": "1.0", "paths": {}, "beets": {"remote_reachable": True}}

        with mock.patch.object(routes_setup, "_build_setup_status_payload", return_value=good_payload):
            res1 = self.client.get("/api/setup/status")
            self.assertEqual(res1.status_code, 200)
            self.assertFalse(res1.get_json().get("stale"))

        with mock.patch.object(routes_setup, "_build_setup_status_payload", side_effect=RuntimeError("engine down")):
            res2 = self.client.get("/api/setup/status?refresh=1")
            self.assertEqual(res2.status_code, 200)
            data2 = res2.get_json()
            self.assertTrue(data2.get("stale"))
            self.assertTrue(data2.get("cached"))
            self.assertEqual(data2.get("status"), "ready")  # last-known payload, not silently reclassified

    def test_setup_status_first_request_failure_returns_503_not_masked_200(self):
        """No prior snapshot exists yet -- a failed build has nothing to fall
        back to, so it must fail closed with 503, never a synthesized 200."""
        with mock.patch.object(routes_setup, "_build_setup_status_payload", side_effect=RuntimeError("engine down")):
            res = self.client.get("/api/setup/status")
            self.assertEqual(res.status_code, 503)

    def test_setup_status_stale_window_eventually_expires_to_503(self):
        good_payload = {"ok": True, "status": "ready", "version": "1.0", "paths": {}, "beets": {"remote_reachable": True}}
        with mock.patch.object(routes_setup, "_build_setup_status_payload", return_value=good_payload):
            self.client.get("/api/setup/status")

        # Simulate the stale snapshot being older than the max-stale window.
        routes_setup._STATUS_CACHE_TS -= (routes_setup._STATUS_CACHE_MAX_STALE_SECONDS + 1)
        with mock.patch.object(routes_setup, "_build_setup_status_payload", side_effect=RuntimeError("still down")):
            res = self.client.get("/api/setup/status?refresh=1")
            self.assertEqual(res.status_code, 503)

    def test_env_save_invalidates_status_cache(self):
        fake_payload_v1 = {"ok": True, "status": "ready", "version": "1.0", "paths": {}, "beets": {"remote_reachable": True}}
        fake_payload_v2 = {"ok": True, "status": "warning", "version": "1.0", "paths": {}, "beets": {"remote_reachable": True}}

        with mock.patch.object(routes_setup, "_build_setup_status_payload", return_value=fake_payload_v1):
            res1 = self.client.get("/api/setup/status")
            self.assertEqual(res1.get_json().get("status"), "ready")

        with mock.patch.object(routes_setup, "_write_env_file", return_value="/tmp/backup"):
            resp = self.client.post("/api/setup/env", json={"variables": {"AI_MODEL": "gpt-4o-mini"}, "clear": []})
            self.assertEqual(resp.status_code, 200)

        with mock.patch.object(routes_setup, "_build_setup_status_payload", return_value=fake_payload_v2):
            res2 = self.client.get("/api/setup/status")
            data2 = res2.get_json()
            self.assertFalse(data2.get("cached"), "env save must invalidate the cache so the next read is fresh")
            self.assertEqual(data2.get("status"), "warning")


if __name__ == "__main__":
    unittest.main()
