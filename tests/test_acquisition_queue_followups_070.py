"""0.7.0 Find Music follow-ups: the GET default page (#385 F3), no GET
refresh, and a finished download patching the snapshot instead of forcing a
full rebuild (F2, ARCH-028). Library and Lidarr are faked."""
import json
import threading
import unittest
from unittest import mock

import tests.test_acquisition_queue_cache as base
from tests.test_acquisition_queue_cache import _CacheCase, _wanted


def setUpModule():
    base.setUpModule()


def _acq():
    return base.acq


def _builds(store):
    return [j for j in store.all() if j.metadata.get("type") == "acquisition-queue-build"]


class GetDefaultsTests(_CacheCase):
    def test_absent_limit_is_one_page_of_500(self):
        self.wanted = [_wanted(i) for i in range(1, 601)]
        self._build()
        status, body = self._get()
        self.assertEqual((status, len(body["items"]), body["limit"], body["total"]), (200, 500, 500, 600))
        _, rest = self._get("?offset=500")
        self.assertEqual(len(rest["items"]), 100)

    def test_get_refresh_param_starts_no_build(self):
        self._build()
        before = len(_builds(self.store))
        status, _ = self._get("?refresh=1&limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(_builds(self.store)), before)


class DownloadPatchTests(_CacheCase):
    def setUp(self):
        super().setUp()
        self.wanted = [_wanted(i) for i in range(1, 6)]
        self._build()
        self.rows = _acq().acquisition_queue_snapshot()[0]["items"]

    def _refresh(self):
        res = self.client.post("/api/acquisition/queue/refresh", json={})
        return res.status_code, res.get_json()

    def test_mark_done_drops_the_row_and_persists(self):
        row = self.rows[1]
        self.assertEqual(_acq().acquisition_queue_mark_done(mbid=row["mbid"].upper()), 1)
        snap = _acq().acquisition_queue_snapshot()[0]
        self.assertNotIn(row["key"], [r["key"] for r in snap["items"]])
        self.assertEqual(snap["counts"]["all"], 4)
        on_disk = json.loads(self.cache_file.read_text(encoding="utf-8"))
        self.assertEqual(len(on_disk["items"]), 4)
        self.assertEqual(self._get("?limit=10")[1]["total"], 4)

    def test_refresh_after_a_download_reuses_the_list_once(self):
        builds = len(_builds(self.store))
        _acq().acquisition_queue_mark_done(keys=[self.rows[0]["key"]])
        status, body = self._refresh()
        self.assertEqual((status, body["reused"], body["job_id"]), (200, True, None))
        self.assertEqual(len(_builds(self.store)), builds)
        # The next Refresh (the user's own) rebuilds.
        status, body = self._refresh()
        self.assertEqual((status, body["reused"]), (202, False))
        self.store.get(body["job_id"])._thread.join(10)

    def test_a_run_of_single_downloads_causes_no_rebuild(self):
        builds = len(_builds(self.store))
        for row in self.rows[:4]:
            _acq().acquisition_queue_mark_done(mbid=row["mbid"])
            self.assertEqual(self._refresh()[1]["reused"], True)
        self.assertEqual(len(_builds(self.store)), builds)
        self.assertEqual(len(_acq().acquisition_queue_snapshot()[0]["items"]), 1)

    def test_no_matching_row_does_not_swallow_a_refresh(self):
        _acq().acquisition_queue_mark_done(mbid="https://musicbrainz.org/release-group/none")
        status, body = self._refresh()
        self.assertEqual((status, body["reused"]), (202, False))
        self.store.get(body["job_id"])._thread.join(10)

    def test_build_running_during_the_download_does_not_bring_the_row_back(self):
        gate, release = threading.Event(), threading.Event()
        _acq().get_library_payload.side_effect = lambda **kw: (gate.set(), release.wait(10), self.library)[2]
        job_id, _ = _acq().start_acquisition_queue_build(force=True)
        gate.wait(5)
        _acq().acquisition_queue_mark_done(mbid=self.rows[2]["mbid"])
        release.set()
        self.store.get(job_id)._thread.join(10)
        keys = [r["key"] for r in _acq().acquisition_queue_snapshot()[0]["items"]]
        self.assertEqual(len(keys), 4)
        self.assertNotIn(self.rows[2]["key"], keys)
        # A build that starts after the download includes whatever the library says.
        self._build(force=True)
        self.assertEqual(len(_acq().acquisition_queue_snapshot()[0]["items"]), 5)

    def test_download_job_wrapper_marks_only_an_import(self):
        row = self.rows[0]
        wrapped = _acq()._mark_queue_done_after(lambda log, cancel=None: {"aldir": "/d"}, mbid=row["mbid"],
                                                album_id=0)
        wrapped([])
        self.assertEqual(len(_acq().acquisition_queue_snapshot()[0]["items"]), 5)
        wrapped = _acq()._mark_queue_done_after(lambda log, cancel=None: {"imported": True}, mbid=row["mbid"],
                                                album_id=0)
        wrapped([])
        self.assertEqual(len(_acq().acquisition_queue_snapshot()[0]["items"]), 4)

    def test_download_all_marks_its_successes(self):
        started = mock.Mock(return_value=mock.Mock(job_id="batch"))
        with mock.patch.object(base.routes.jobs, "start_python", started):
            res = self.client.post("/api/acquisition/download-all",
                                   json={"keys": [self.rows[0]["key"], self.rows[1]["key"]]})
        self.assertEqual(res.status_code, 200, res.get_json())
        job_fn = started.call_args.args[0]
        outcomes = iter([{"aldir": "/a"}, RuntimeError("no source")])

        def wait(*_a, **_kw):
            out = next(outcomes)
            if isinstance(out, Exception):
                raise out
            return out
        with mock.patch.object(base.routes, "_acq_start_download_job", return_value="child"), \
                mock.patch.object(base.routes, "_wait_for_child_job", side_effect=wait), \
                mock.patch.object(base.routes, "_invalidate_lib_cache"), \
                mock.patch.object(base.routes, "_save_acq_download_all_last"), \
                mock.patch.object(base.routes.job_contract, "enter"):
            totals = job_fn([], threading.Event(), lambda **kw: None)
        self.assertEqual((totals["success"], totals["failed"]), (1, 1))
        keys = [r["key"] for r in _acq().acquisition_queue_snapshot()[0]["items"]]
        self.assertNotIn(self.rows[0]["key"], keys)
        self.assertIn(self.rows[1]["key"], keys)
        self.assertEqual(self._refresh()[1]["reused"], True)


if __name__ == "__main__":
    unittest.main()
