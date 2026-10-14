"""QA #385 adversarial checks for the Find Music list snapshot.

Concurrency, failure/cancel preservation, corrupt cache files, paging
bounds, filter/count semantics, the Download All snapshot path, no build
work on request threads, and private file modes. Library and Lidarr are
faked through the PR's own harness (tests.test_acquisition_queue_cache).
"""
import json
import os
import stat
import threading
import time
import unittest
from unittest import mock

import tests.test_acquisition_queue_cache as base
from tests.test_acquisition_queue_cache import _CacheCase, _wanted


def setUpModule():
    base.setUpModule()  # deferred app import, same as the base module


def _acq():
    return base.acq


class ConcurrencyTests(_CacheCase):
    def _slow_library(self):
        gate, release = threading.Event(), threading.Event()

        def slow(force=False, include_disk_only=False):
            self.library_calls.append((force, threading.current_thread()))
            gate.set()
            release.wait(10)
            return self.library
        _acq().get_library_payload.side_effect = slow
        return gate, release

    def _builds(self):
        return [j for j in self.store.all() if j.metadata.get("type") == "acquisition-queue-build"]

    def test_racing_starts_create_one_job(self):
        gate, release = self._slow_library()
        barrier = threading.Barrier(12)
        results = []

        def start():
            barrier.wait(5)
            results.append(_acq().start_acquisition_queue_build(force=True))
        threads = [threading.Thread(target=start) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        gate.wait(5)
        release.set()
        self.assertEqual(len(results), 12)
        self.assertEqual(len({job_id for job_id, _ in results}), 1)
        self.assertEqual(sum(1 for _, running in results if not running), 1)
        builds = self._builds()
        self.assertEqual(len(builds), 1)
        builds[0]._thread.join(10)
        self.assertEqual(builds[0].status, "success", builds[0].log)

    def test_concurrent_stale_reads_start_one_rebuild(self):
        self._build()
        _acq()._acq_queue["snapshot"] = dict(_acq()._acq_queue["snapshot"], built_at=time.time() - 7 * 3600)
        gate, release = self._slow_library()
        barrier = threading.Barrier(8)
        bodies = []

        def read():
            barrier.wait(5)
            res = base.flask_app.test_client().get("/api/acquisition/queue?limit=10")
            bodies.append((res.status_code, res.get_json()))
        threads = [threading.Thread(target=read) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        gate.wait(5)
        release.set()
        self.assertTrue(all(status == 200 and body["total"] == 250 for status, body in bodies), bodies[:1])
        builds = self._builds()
        self.assertEqual(len(builds), 2)  # the initial build plus exactly one rebuild
        for job in builds:
            job._thread.join(10)

    def test_read_during_rebuild_serves_the_old_snapshot_at_once(self):
        self._build()
        old_built_at = _acq().acquisition_queue_snapshot()[1]["built_at"]
        gate, release = self._slow_library()
        self.wanted = self.wanted[:3]
        refresh = self.client.post("/api/acquisition/queue/refresh", json={})
        gate.wait(5)
        started = time.monotonic()
        status, body = self._get("?limit=100")
        elapsed = time.monotonic() - started
        release.set()
        self.store.get(refresh.get_json()["job_id"])._thread.join(10)
        self.assertLess(elapsed, 3)
        self.assertEqual((status, body["total"], len(body["items"])), (200, 250, 100))
        self.assertTrue(body["cache"]["refreshing"])
        self.assertEqual(body["cache"]["built_at"], old_built_at)
        # The finished rebuild replaces the snapshot as a whole.
        _, after = self._get("?limit=100")
        self.assertEqual((after["total"], after["cache"]["refreshing"]), (3, False))

    def test_refresh_after_a_finished_build_starts_a_new_one(self):
        first = self._build()
        res = self.client.post("/api/acquisition/queue/refresh", json={})
        body = res.get_json()
        self.assertEqual(res.status_code, 202)
        self.assertFalse(body["already_running"])
        self.assertNotEqual(body["job_id"], first.job_id)
        self.store.get(body["job_id"])._thread.join(10)
        self.assertEqual(self.library_calls[-1][0], True)  # Refresh forces a library refresh

    def test_refresh_body_must_be_an_object(self):
        res = self.client.post("/api/acquisition/queue/refresh", json=[1, 2])
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self._builds(), [])


class FailurePreservationTests(_CacheCase):
    def test_exception_in_the_build_keeps_the_last_good_snapshot(self):
        self._build()
        before = _acq().acquisition_queue_snapshot()[0]
        before_disk = self.cache_file.read_text(encoding="utf-8")
        _acq().get_library_payload.side_effect = RuntimeError("boom")
        job = self._build(force=True)
        self.assertEqual(job.status, "failed")
        snap, cache = _acq().acquisition_queue_snapshot()
        self.assertIs(snap, before)
        self.assertEqual(self.cache_file.read_text(encoding="utf-8"), before_disk)
        self.assertEqual(cache["state"], "failed")
        self.assertTrue(cache["last_error"])
        self.assertNotIn("boom", cache["last_error"])  # no raw exception text to the UI

    def test_lidarr_fetch_exception_keeps_the_last_good_snapshot(self):
        self._build()
        before = _acq().acquisition_queue_snapshot()[0]
        _acq()._acq_fetch_lidarr_wanted.side_effect = OSError("socket")
        self.assertEqual(self._build(force=True).status, "failed")
        self.assertIs(_acq().acquisition_queue_snapshot()[0], before)

    def test_cancel_during_lidarr_keeps_the_snapshot_and_is_not_a_failure(self):
        self._build()
        before = _acq().acquisition_queue_snapshot()[0]
        gate, release = threading.Event(), threading.Event()

        def slow_fetch(progress=None, cancel=None, include_unmonitored=False):
            gate.set()
            release.wait(10)
            return ([], _acq().LIDARR_WANTED_CANCELLED) if cancel and cancel() else ([], "")
        _acq()._acq_fetch_lidarr_wanted.side_effect = slow_fetch
        job_id, _ = _acq().start_acquisition_queue_build(force=True)
        job = self.store.get(job_id)
        gate.wait(5)
        job.kill()
        release.set()
        job._thread.join(10)
        self.assertEqual(job.status, "cancelled", job.log)
        snap, cache = _acq().acquisition_queue_snapshot()
        self.assertIs(snap, before)
        self.assertEqual((cache["state"], cache["last_error"]), ("ready", ""))

    def test_success_after_failure_clears_the_error(self):
        self.wanted_error = "Could not reach Lidarr"
        self._build()
        self.assertEqual(_acq().acquisition_queue_snapshot()[1]["state"], "failed")
        self.wanted_error = ""
        self.assertEqual(self._build(force=True).status, "success")
        cache = _acq().acquisition_queue_snapshot()[1]
        self.assertEqual((cache["state"], cache["last_error"], cache["last_error_at"]), ("ready", "", None))

    def test_unwritable_cache_dir_still_publishes_in_memory(self):
        with mock.patch.object(_acq().config_layers, "replace_private_file", side_effect=OSError("ro")):
            job = self._build()
        self.assertEqual(job.status, "success", job.log)
        self.assertEqual(len(_acq().acquisition_queue_snapshot()[0]["items"]), 250)
        self.assertFalse(self.cache_file.exists())


class CacheFileTests(_CacheCase):
    def _reload(self):
        _acq()._acq_queue.update(snapshot=None, loaded=False)

    def test_missing_file_is_empty_and_a_read_starts_a_build(self):
        self._reload()
        release = self._hold_builds()
        status, body = self._get("?limit=10")
        self.assertEqual(status, 200)
        self.assertEqual((body["items"], body["total"]), ([], 0))
        self.assertIn(body["cache"]["state"], ("building", "empty"))
        release.set()
        self.store.get(body["cache"]["job_id"])._thread.join(10)
        self.assertEqual(self._get("?limit=10")[1]["total"], 250)

    def test_corrupt_files_never_500(self):
        for text in ("{not json", "[]", "null", json.dumps({"items": "x", "built_at": 1}),
                     json.dumps({"items": [], "built_at": 0}), json.dumps({"items": []}), "\x00\xff"):
            with self.subTest(text=text[:30]):
                self.cache_file.write_text(text, encoding="utf-8", errors="surrogateescape")
                self._reload()
                with mock.patch.object(base.routes, "ensure_acquisition_queue_fresh"):
                    status, body = self._get("?limit=10")
                self.assertEqual(status, 200, body)
                self.assertEqual(body["total"], 0)
                self.assertIsNone(body["cache"]["built_at"])

    def test_malformed_fields_never_500(self):
        for text in (json.dumps({"items": [], "built_at": "abc"}), json.dumps({"items": [1, "x"], "built_at": 5})):
            with self.subTest(text=text[:30]):
                self.cache_file.write_text(text, encoding="utf-8")
                self._reload()
                status, body = self._get("?limit=10")
                for job in self.store.all():
                    job._thread.join(10)
                self.assertEqual(status, 200, body)

    def test_persisted_snapshot_survives_restart_and_stale_one_rebuilds(self):
        self._build()
        data = json.loads(self.cache_file.read_text(encoding="utf-8"))
        data["built_at"] = time.time() - 7 * 3600
        self.cache_file.write_text(json.dumps(data), encoding="utf-8")
        self._reload()
        release = self._hold_builds()
        status, body = self._get("?limit=1")
        self.assertEqual((status, body["total"], body["cache"]["state"]), (200, 250, "stale"))
        release.set()
        self.store.get(body["cache"]["job_id"])._thread.join(10)
        self.assertEqual(self._get("?limit=1")[1]["cache"]["state"], "ready")

    @unittest.skipIf(os.name != "posix", "POSIX file modes")
    def test_cache_file_is_private_and_leaves_no_temp_files(self):
        self._build()
        self._build(force=True)
        self.assertEqual(stat.S_IMODE(self.cache_file.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.cache_file.parent.iterdir()),
                         ["acquisition_queue_cache.json"])


class PagingBoundsTests(_CacheCase):
    def setUp(self):
        super().setUp()
        self._build()

    def test_offset_past_the_end_is_an_empty_page(self):
        status, body = self._get("?offset=10000&limit=100")
        self.assertEqual((status, body["items"], body["total"], body["offset"]), (200, [], 250, 10000))

    def test_last_partial_page_and_max_limit(self):
        _, body = self._get("?offset=200&limit=100")
        self.assertEqual(len(body["items"]), 50)
        _, body = self._get("?limit=500")
        self.assertEqual((len(body["items"]), body["limit"]), (250, 500))

    def test_pages_tile_the_list_without_gaps_or_overlap(self):
        keys = []
        for offset in range(0, 300, 100):
            keys += [row["key"] for row in self._get(f"?offset={offset}&limit=100")[1]["items"]]
        everything = [row["key"] for row in self._get()[1]["items"]]
        self.assertEqual(keys, everything)
        self.assertEqual(len(set(keys)), 250)

    def test_bad_bounds_are_400(self):
        for query in ("?limit=-1", "?offset=10000001", "?offset=1.5", "?limit=1e2", "?offset=abc",
                      "?album_type=" + ",".join(f"t{i}" for i in range(21))):
            with self.subTest(query=query):
                status, body = self._get(query)
                self.assertEqual((status, body["ok"]), (400, False))

    def test_empty_params_use_defaults(self):
        status, body = self._get("?offset=&limit=&source=&sort=&album_type=")
        self.assertEqual((status, body["offset"], body["limit"], body["total"]), (200, 0, 500, 250))


class FilterCountTests(_CacheCase):
    def setUp(self):
        super().setUp()
        self.wanted = ([_wanted(i) for i in range(1, 11)]
                       + [_wanted(i, kind="EP", artist="Ep Artist") for i in range(11, 14)]
                       + [_wanted(i, monitored=False, kind="EP") for i in range(14, 16)]
                       + [_wanted(16, kind="")])
        self._build(include_unmonitored=True)

    def test_album_type_counts_ignore_the_type_filter_but_honour_the_others(self):
        _, body = self._get("?album_type=ep&limit=100")
        self.assertEqual(body["total"], 3)
        self.assertEqual(body["album_types"], {"album": 10, "ep": 3, "unknown": 1})
        _, body = self._get("?album_type=ep&search=ep%20artist&limit=100")
        self.assertEqual(body["album_types"], {"ep": 3})
        _, body = self._get("?album_type=ep&include_unmonitored=1&limit=100")
        self.assertEqual((body["total"], body["album_types"]["ep"]), (5, 5))

    def test_unknown_type_filter_is_empty_not_everything(self):
        _, body = self._get("?album_type=compilation&limit=100")
        self.assertEqual((body["total"], body["items"]), (0, []))

    def test_missing_type_is_unknown(self):
        _, body = self._get("?album_type=unknown&limit=100")
        self.assertEqual([row["album"] for row in body["items"]], ["Album 016"])

    def test_search_matches_mbid_and_is_case_insensitive(self):
        _, body = self._get("?search=00000012-0000&limit=100")
        self.assertEqual([row["album"] for row in body["items"]], ["Album 012"])
        _, body = self._get("?search=ALBUM%20003&limit=100")
        self.assertEqual(body["total"], 1)

    def test_source_counts_match_the_source_views(self):
        _, body = self._get("?limit=0&include_unmonitored=1")
        counts = body["counts"]
        for source in ("beets", "lidarr", "merged", "unmonitored"):
            with self.subTest(source=source):
                _, view = self._get(f"?source={source}&limit=0")
                self.assertEqual(view["total"], counts[source])
        self.assertEqual(body["total"], counts["all"])


class DownloadAllSnapshotTests(_CacheCase):
    def setUp(self):
        super().setUp()
        self.wanted = [_wanted(i) for i in range(1, 4)] + [_wanted(4, monitored=False)]
        self.started = mock.Mock(return_value=mock.Mock(job_id="dl"))
        patcher = mock.patch.object(base.routes.jobs, "start_python", self.started)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _post(self, body):
        res = self.client.post("/api/acquisition/download-all", json=body)
        return res.status_code, res.get_json()

    def test_failed_rebuild_still_uses_the_last_good_snapshot(self):
        self._build(include_unmonitored=True)
        self.wanted_error = "Could not reach Lidarr"
        self._build(force=True)
        with mock.patch.object(_acq(), "_build_acquisition_queue_payload") as build:
            status, body = self._post({})
        build.assert_not_called()
        self.assertEqual((status, body["count"]), (200, 3), body)

    def test_unmonitored_rows_need_the_flag(self):
        self._build(include_unmonitored=True)
        status, body = self._post({"include_unmonitored": True})
        self.assertEqual((status, body["count"]), (200, 4), body)

    def test_no_snapshot_while_building_joins_the_build(self):
        gate, release = threading.Event(), threading.Event()
        _acq().get_library_payload.side_effect = lambda **kw: (gate.set(), release.wait(10), self.library)[2]
        job_id, _ = _acq().start_acquisition_queue_build()
        gate.wait(5)
        status, body = self._post({})
        release.set()
        self.store.get(job_id)._thread.join(10)
        self.assertEqual((status, body["code"], body["job_id"]), (409, "queue_not_ready", job_id))
        builds = [j for j in self.store.all() if j.metadata.get("type") == "acquisition-queue-build"]
        self.assertEqual(len(builds), 1)
        self.started.assert_not_called()

    def test_keys_not_in_the_snapshot_are_400(self):
        self._build()
        status, body = self._post({"keys": ["no-such-key"]})
        self.assertEqual((status, body["ok"]), (400, False))
        self.started.assert_not_called()


class NoRequestThreadBuildTests(_CacheCase):
    """The build body never runs on the thread serving a request."""

    def setUp(self):
        super().setUp()
        self.build_threads = []
        real = _acq()._build_acquisition_queue_payload

        def spy(*args, **kwargs):
            self.build_threads.append(threading.get_ident())
            return real(*args, **kwargs)
        patcher = mock.patch.object(_acq(), "_build_acquisition_queue_payload", side_effect=spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _join_all(self):
        for job in self.store.all():
            if job._thread is not None:
                job._thread.join(10)

    def test_every_entry_point_builds_off_the_request_thread(self):
        me = threading.get_ident()
        started = mock.Mock(return_value=mock.Mock(job_id="dl"))
        with mock.patch.object(base.routes.jobs, "start_python", started):
            self.client.post("/api/acquisition/download-all", json={})  # no snapshot: starts a build
        self._join_all()

        self.client.post("/api/acquisition/queue/refresh", json={})
        self._join_all()
        _acq()._acq_queue["snapshot"] = dict(_acq()._acq_queue["snapshot"], built_at=1.0)
        self.client.get("/api/acquisition/queue?limit=1")
        self._join_all()
        self.assertEqual(len(self.build_threads), 3)
        self.assertNotIn(me, self.build_threads)
        self.assertNotIn(me, [t.ident for _, t in self.library_calls])


class LidarrShrinkDuringFetchTests(unittest.TestCase):
    """A wanted row removed from an earlier page while paging shifts a still-wanted
    row out of both pages; distinct records then equal the final totalRecords."""

    def test_removal_between_pages_is_not_complete(self):  # QA #385 N2: fixed by the re-read (ARCH-028)
        import tests.test_integrations_reliability as tir
        acq = _acq()
        page1 = [dict(tir._WANTED, id=i) for i in range(1, 4)]  # ids 1-3 of 6
        page2 = [dict(tir._WANTED, id=i) for i in (5, 6)]       # id 2 removed: id 4 shifted onto page 1
        # The total moved during the pass, so it is read again; the re-read is stable.
        reread = [tir._wanted_page([dict(tir._WANTED, id=i) for i in (1, 3, 4)], total=5),
                  tir._wanted_page(page2, total=5)]
        responses = [tir._wanted_page(page1, total=6), tir._wanted_page(page2, total=5)] + reread
        with mock.patch.object(acq, "LIDARR_KEY", "k"), \
                mock.patch.object(acq, "LIDARR_URL", "http://lidarr.test:8686"), \
                mock.patch.object(acq, "_LIDARR_WANTED_PAGE_SIZE", 3), \
                mock.patch.object(tir.urllib.request, "urlopen", side_effect=responses):
            rows, error = acq.fetch_lidarr_wanted()
        self.assertEqual((error, sorted(row["lidarr_id"] for row in rows)), ("", [1, 3, 4, 5, 6]))


if __name__ == "__main__":
    unittest.main()
