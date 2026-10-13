"""Find Music list snapshot (F7, F8, ACQ-10; QA #379 F3/F6/N3).

The list is built only by the acquisition-queue-build job; GET
/api/acquisition/queue pages the last good snapshot and Download All selects
from it. Library and Lidarr are faked: no network, no Beets.
"""
import ast
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

acq = routes = job_engine = flask_app = None


def setUpModule():
    # Deferred: test_ai_batch_retry_race must be the first to import app.
    global acq, routes, job_engine, flask_app
    import app
    import backend.acquisition_service as acq
    import job_engine
    import routes_acquisition as routes
    flask_app = app.app


def _wanted(i, *, monitored=True, kind="Album", artist=None, year="2001"):
    return {"artist": artist or f"Artist {i:03d}", "album": f"Album {i:03d}", "year": year, "type": kind,
            "lidarr_id": i, "mb_albumid": "", "mb_releasegroupid": f"{i:08d}-0000-4000-8000-000000000000",
            "mb_url": "", "monitored": monitored}


class _CacheCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache_file = Path(tmp.name) / "acquisition_queue_cache.json"
        self.store = job_engine.JobStore()
        self.addCleanup(self.store.close)
        self.wanted = [_wanted(i) for i in range(1, 251)]
        self.wanted_error = ""
        self.library = {"ok": True, "artists": [], "library_version": 3}
        self.fetch_calls = []
        self.library_calls = []
        patches = [
            mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"}),
            mock.patch.object(acq, "jobs", self.store),
            mock.patch.object(acq, "_ACQ_QUEUE_CACHE_FILE", self.cache_file),
            mock.patch.dict(acq._acq_queue, {"snapshot": None, "loaded": False, "job_id": "",
                                             "last_error": "", "last_error_at": 0.0}),
            mock.patch.object(acq, "get_library_payload", side_effect=self._library),
            mock.patch.object(acq, "_acq_fetch_lidarr_wanted", side_effect=self._fetch),
            mock.patch.object(acq, "_download_sources_status", return_value={"fallbacks": {}}),
            mock.patch.object(routes, "_download_sources_status", return_value={"fallbacks": {}}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = flask_app.test_client()

    def _library(self, force=False, include_disk_only=False):
        self.library_calls.append((force, threading.current_thread()))
        return self.library

    def _fetch(self, progress=None, cancel=None, include_unmonitored=False):
        self.fetch_calls.append({"include_unmonitored": include_unmonitored, "thread": threading.current_thread()})
        if cancel is not None and cancel():
            return [], acq.LIDARR_WANTED_CANCELLED
        if self.wanted_error:
            return [], self.wanted_error
        rows = [r for r in self.wanted if include_unmonitored or r["monitored"]]
        if progress:
            progress(len(rows), len(rows))
        return [dict(r) for r in rows], ""

    def _build(self, **kwargs):
        job_id, running = acq.start_acquisition_queue_build(**kwargs)
        job = self.store.get(job_id)
        job._thread.join(10)
        return job

    def _get(self, query=""):
        res = self.client.get("/api/acquisition/queue" + query)
        return res.status_code, res.get_json()


class SnapshotBuildTests(_CacheCase):
    def test_build_publishes_a_snapshot_and_persists_it(self):
        job = self._build()
        self.assertEqual(job.status, "success", job.log)
        self.assertEqual(job.metadata["type"], "acquisition-queue-build")
        self.assertIs(job.metadata["mutating"], False)
        self.assertEqual(job.metadata["dedupe_key"], "acquisition-queue-build")
        self.assertEqual(job.state["phase"], "merge")
        snap, cache = acq.acquisition_queue_snapshot()
        self.assertEqual(len(snap["items"]), 250)
        self.assertEqual(cache["state"], "ready")
        self.assertEqual(cache["max_age_seconds"], 21600)
        self.assertLess(cache["age_seconds"], 5)
        self.assertNotEqual(self.fetch_calls[0]["thread"], threading.main_thread())
        # A restart serves the persisted list at once.
        on_disk = json.loads(self.cache_file.read_text(encoding="utf-8"))
        self.assertEqual(len(on_disk["items"]), 250)
        acq._acq_queue.update(snapshot=None, loaded=False)
        self.assertEqual(len(acq.acquisition_queue_snapshot()[0]["items"]), 250)

    def test_lidarr_error_keeps_the_last_good_snapshot(self):
        self._build()
        built_at = acq.acquisition_queue_snapshot()[1]["built_at"]
        self.wanted = self.wanted[:10]
        self.wanted_error = "Could not reach Lidarr"
        job = self._build(force=True)
        self.assertEqual(job.status, "failed")
        snap, cache = acq.acquisition_queue_snapshot()
        self.assertEqual(len(snap["items"]), 250)  # not the library-only list
        self.assertEqual((cache["state"], cache["last_error"], cache["built_at"]),
                         ("failed", "Could not reach Lidarr", built_at))
        status, body = self._get("?limit=5")
        self.assertEqual(status, 200)
        self.assertEqual((body["total"], body["cache"]["state"]), (250, "failed"))
        self.assertEqual(body["cache"]["last_error"], "Could not reach Lidarr")

    def test_lidarr_error_without_a_snapshot_publishes_nothing(self):
        self.wanted_error = "Lidarr rejected the API key"
        self._build()
        snap, cache = acq.acquisition_queue_snapshot()
        self.assertIsNone(snap)
        self.assertEqual(cache["state"], "failed")
        self.assertFalse(self.cache_file.exists())

    def test_library_error_is_a_failed_build(self):
        self.library = {"ok": False, "error": "Beets unavailable"}
        self.assertEqual(self._build().status, "failed")
        self.assertIsNone(acq.acquisition_queue_snapshot()[0])

    def test_lidarr_not_configured_publishes_library_rows(self):
        self.wanted_error = "LIDARR_API_KEY not configured"
        self.assertEqual(self._build().status, "success")
        snap, cache = acq.acquisition_queue_snapshot()
        self.assertEqual((snap["wanted_error"], cache["state"]), ("LIDARR_API_KEY not configured", "ready"))

    def test_cancel_keeps_the_previous_snapshot(self):
        self._build()
        gate, release = threading.Event(), threading.Event()

        def slow_library(force=False, include_disk_only=False):
            gate.set()
            release.wait(5)
            return self.library
        acq.get_library_payload.side_effect = slow_library
        job_id, _ = acq.start_acquisition_queue_build(force=True)
        job = self.store.get(job_id)
        gate.wait(5)
        job.kill()
        release.set()
        job._thread.join(10)
        self.assertEqual(job.status, "cancelled", job.log)
        self.assertEqual(len(acq.acquisition_queue_snapshot()[0]["items"]), 250)
        self.assertEqual(self.fetch_calls[-1]["include_unmonitored"], False)

    def test_refresh_while_building_joins_the_running_job(self):
        gate, release = threading.Event(), threading.Event()

        def slow_library(force=False, include_disk_only=False):
            gate.set()
            release.wait(5)
            return self.library
        acq.get_library_payload.side_effect = slow_library
        first = self.client.post("/api/acquisition/queue/refresh", json={})
        gate.wait(5)
        second = self.client.post("/api/acquisition/queue/refresh", json={})
        status, body = self._get("?limit=10")
        release.set()
        self.assertEqual((first.status_code, second.status_code), (202, 202))
        self.assertFalse(first.get_json()["already_running"])
        self.assertTrue(second.get_json()["already_running"])
        self.assertEqual(first.get_json()["job_id"], second.get_json()["job_id"])
        self.assertEqual((status, body["items"], body["cache"]["state"]), (200, [], "building"))
        self.assertTrue(body["cache"]["refreshing"])
        builds = [j for j in self.store.all() if j.metadata.get("type") == "acquisition-queue-build"]
        self.assertEqual(len(builds), 1)
        builds[0]._thread.join(10)


class QueueRouteTests(_CacheCase):
    def setUp(self):
        super().setUp()
        self.wanted = ([_wanted(i) for i in range(1, 201)]
                       + [_wanted(i, kind="EP") for i in range(201, 241)]
                       + [_wanted(i, monitored=False) for i in range(241, 246)]
                       + [_wanted(999, artist="Zeta Bach", year="1985", kind="Single")])
        self._build(include_unmonitored=True)

    def test_empty_cache_starts_a_build_and_answers_at_once(self):
        acq._acq_queue.update(snapshot=None)
        gate, release = threading.Event(), threading.Event()
        acq.get_library_payload.side_effect = lambda **kw: (gate.set(), release.wait(5), self.library)[2]
        started = time.monotonic()
        status, body = self._get("?limit=100")
        self.assertLess(time.monotonic() - started, 3)
        gate.wait(5)
        self.assertEqual((status, body["items"], body["total"], body["cache"]["state"]), (200, [], 0, "building"))
        release.set()
        self.store.get(body["cache"]["job_id"])._thread.join(10)

    def test_paging(self):
        _, first = self._get("?limit=100")
        _, second = self._get("?limit=100&offset=200")
        self.assertEqual((first["total"], len(first["items"]), first["offset"], first["limit"]), (241, 100, 0, 100))
        self.assertEqual(len(second["items"]), 41)
        self.assertEqual(first["items"][0]["album"], "Album 001")
        _, counts_only = self._get("?limit=0")
        self.assertEqual((counts_only["items"], counts_only["total"]), ([], 241))
        self.assertEqual(counts_only["counts"]["all"], 246)
        self.assertEqual(counts_only["counts"]["unmonitored"], 5)
        _, everything = self._get()  # no limit: the whole filtered list (back-compat)
        self.assertEqual((len(everything["items"]), everything["limit"]), (241, None))

    def test_search_filters_and_sort(self):
        _, body = self._get("?search=zeta%20BACH&limit=100")
        self.assertEqual([r["artist"] for r in body["items"]], ["Zeta Bach"])
        _, body = self._get("?album_type=ep&limit=100")
        self.assertEqual(body["total"], 40)
        self.assertEqual(body["album_types"], {"album": 200, "ep": 40, "single": 1})
        _, body = self._get("?album_type=ep,single&limit=100")
        self.assertEqual(body["total"], 41)
        _, body = self._get("?include_unmonitored=1&limit=0")
        self.assertEqual(body["total"], 246)
        _, body = self._get("?source=unmonitored&limit=100")
        self.assertEqual(body["total"], 5)
        _, body = self._get("?source=beets&limit=100")
        self.assertEqual(body["total"], 0)
        _, body = self._get("?sort=year_desc&limit=1&search=a")
        self.assertEqual(body["items"][0]["year"], "2001")
        _, body = self._get("?sort=year_desc&search=zeta")
        self.assertEqual(body["items"][0]["year"], "1985")

    def test_bad_params_are_400(self):
        for query in ("?limit=abc", "?limit=501", "?offset=-1", "?source=nope", "?sort=random",
                      "?search=" + "x" * 201):
            with self.subTest(query=query):
                status, body = self._get(query)
                self.assertEqual((status, body["ok"]), (400, False))

    def test_stale_snapshot_is_served_and_rebuilt_in_the_background(self):
        acq._acq_queue["snapshot"] = dict(acq._acq_queue["snapshot"], built_at=time.time() - 7 * 3600)
        status, body = self._get("?limit=5")
        self.assertEqual((status, body["total"], body["cache"]["state"]), (200, 241, "stale"))
        job = self.store.get(body["cache"]["job_id"])
        self.assertIsNotNone(job)
        job._thread.join(10)
        self.assertEqual(acq.acquisition_queue_snapshot()[1]["state"], "ready")

    def test_fresh_snapshot_starts_no_build(self):
        before = len(self.store.all())
        self._get("?limit=5")
        self.assertEqual(len(self.store.all()), before)

    def test_failed_build_is_not_retried_on_every_read(self):
        self.wanted_error = "Could not reach Lidarr"
        acq._acq_queue["snapshot"] = dict(acq._acq_queue["snapshot"], built_at=time.time() - 7 * 3600)
        _, body = self._get("?limit=1")
        self.store.get(body["cache"]["job_id"])._thread.join(10)
        before = len(self.store.all())
        _, body = self._get("?limit=1")
        self.assertEqual(len(self.store.all()), before)
        self.assertEqual(body["cache"]["state"], "failed")

    def test_unmonitored_toggle_reaches_the_build(self):  # QA #379 F3
        self.assertTrue(self.fetch_calls[-1]["include_unmonitored"])
        acq._acq_queue["snapshot"] = dict(acq._acq_queue["snapshot"], include_unmonitored=False)
        _, body = self._get("?include_unmonitored=1&limit=1")
        self.store.get(body["cache"]["job_id"])._thread.join(10)
        self.assertTrue(self.fetch_calls[-1]["include_unmonitored"])
        res = self.client.post("/api/acquisition/queue/refresh", json={"include_unmonitored": True})
        self.store.get(res.get_json()["job_id"])._thread.join(10)
        self.assertTrue(self.fetch_calls[-1]["include_unmonitored"])


class DownloadAllTests(_CacheCase):
    def setUp(self):
        super().setUp()
        self.wanted = [_wanted(i) for i in range(1, 4)]
        self.started = mock.Mock(return_value=mock.Mock(job_id="dl"))
        patcher = mock.patch.object(routes.jobs, "start_python", self.started)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_selects_rows_from_the_snapshot_without_rebuilding(self):
        self._build()
        with mock.patch.object(acq, "_build_acquisition_queue_payload") as build:
            snap = acq.acquisition_queue_snapshot()[0]
            key = snap["items"][1]["key"]
            res = self.client.post("/api/acquisition/download-all", json={"keys": [key]})
        build.assert_not_called()
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["count"], 1)
        self.assertEqual(self.started.call_args.kwargs["metadata"]["count"], 1)

    def test_no_snapshot_is_409_and_starts_a_build(self):
        res = self.client.post("/api/acquisition/download-all", json={})
        body = res.get_json()
        self.assertEqual((res.status_code, body["code"]), (409, "queue_not_ready"))
        job = self.store.get(body["job_id"])
        self.assertEqual(job.metadata["type"], "acquisition-queue-build")
        job._thread.join(10)
        self.started.assert_not_called()


class NoRequestBuildTests(unittest.TestCase):
    def test_no_route_module_calls_the_full_build(self):
        root = Path(__file__).resolve().parents[1]
        for path in sorted(root.glob("routes_*.py")) + [root / "app.py"]:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            names |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                      for alias in node.names}
            with self.subTest(path=path.name):
                self.assertNotIn("_build_acquisition_queue_payload", names)


class SourceProbeCacheTests(unittest.TestCase):  # QA #379 F6
    def test_probes_run_once_per_ttl(self):
        acq._source_probe_cache.update(at=0.0, fallbacks=None)
        self.addCleanup(acq._source_probe_cache.update, at=0.0, fallbacks=None)
        with mock.patch.object(acq, "_direct_source_unavailable_reason", return_value="") as probe:
            acq._download_sources_status()
            acq._download_sources_status()
            self.assertEqual(probe.call_count, 3)
            acq._source_probe_cache["at"] -= acq._SOURCE_PROBE_TTL + 1
            acq._download_sources_status()
            self.assertEqual(probe.call_count, 6)


if __name__ == "__main__":
    unittest.main()
