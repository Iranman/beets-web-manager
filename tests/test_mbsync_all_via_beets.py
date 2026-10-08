"""MBSync All (LT-18): POST /api/library/mbsync-all starts a job that asks
Beets to run its own mbsync over the library (plugin 1.13.0), follows it,
forwards a cancel, and records what Beets changed on the job's transaction.
There is no rollback; the transaction says so."""

import tempfile
import threading
import time
import unittest
from unittest import mock

import backend.composite_workflows as cw
from backend.beets_adapter import BeetsAdapterError, BeetsAdapterNotFoundError, BeetsAdapterConnectionError
from backend.transaction_engine import TransactionStore
from job_engine import CancelSignal
from tests.test_staging_mutation_hardening import _RouteEnv

CHANGE = {"kind": "album", "id": 5, "artist": "A", "album": "New",
          "album_fields": {"album": ["Old", "New"]},
          "items": [{"item_id": 9, "title": "t1", "fields": {"title": ["x", "t1"]}}]}
DONE = {"status": "succeeded", "result": {"targets": 2, "processed": 2, "changed_albums": 1, "changed_items": 1,
                                          "unchanged": 1, "skipped_no_id": 3, "changes": [CHANGE],
                                          "cancelled": False}}


class ScriptedAdapter:
    def __init__(self, ops, start_error=None):
        self.ops, self.start_error, self.calls = list(ops), start_error, []

    def mbsync_library(self, idempotency_key):
        self.calls.append(("start", idempotency_key))
        if self.start_error:
            raise self.start_error
        return {"operation_id": "op1", "status": "running", "write": True, "move": False}

    def get_operation(self, op_id):
        item = self.ops.pop(0) if len(self.ops) > 1 else self.ops[0]
        if callable(item):
            item = item()
        if isinstance(item, Exception):
            raise item
        return item

    def cancel_mbsync_library(self, op_id):
        self.calls.append(("cancel", op_id))
        return {"operation_id": op_id, "status": "cancelling"}


class MbsyncLibraryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = TransactionStore(self._tmp.name)
        self.tx = self.store.create(operation_type="MusicBrainz Match", status="Running")["id"]

    def run_sync(self, ad, cancel=None, **kw):
        log = []
        res = cw.mbsync_library(log, cancel, transaction_id=self.tx, adapter=ad, store=self.store,
                                sleep=lambda s: None, **kw)
        return res, log

    def test_success_records_the_change_log_and_no_rollback(self):
        ad = ScriptedAdapter([{"status": "running", "result": {"processed": 1, "targets": 2}}, DONE])
        res, log = self.run_sync(ad)
        self.assertTrue(res["ok"])
        self.assertEqual((res["changed"], res["mutated"], res["cancelled"]), (1, True, False))
        self.assertEqual(ad.calls, [("start", f"mbsync-library-{self.tx}")])
        tx = self.store.get(self.tx)
        meta = tx["metadata"]
        self.assertEqual(meta["mutation_family"], cw.MBSYNC_LIBRARY_FAMILY)
        self.assertEqual((meta["engine_operation_id"], meta["write"], meta["move"]), ("op1", True, False))
        self.assertEqual(meta["engine_result"]["changed_albums"], 1)
        self.assertEqual([c["id"] for c in tx["changes"]], ["album:5", "item:9"])
        self.assertEqual(tx["changes"][1]["metadata_diff"],
                         [{"field": "title", "old": "x", "new": "t1", "changed": True}])
        self.assertTrue(any("write tags yes per Beets' import.write; files are never moved" in line for line in log))
        self.assertTrue(any("2 of 2 synced: 1 changed" in line for line in log))

    def test_refusals_change_nothing(self):
        for code, text in (("BEETS_NOT_FOUND", "1.13.0"), ("CAPABILITY_UNAVAILABLE", "mbsync"),
                           ("ALREADY_RUNNING", "already")):
            ad = ScriptedAdapter([], start_error=BeetsAdapterError("x", status_code=409, error_code=code))
            res, _ = self.run_sync(ad)
            self.assertEqual((res["ok"], res["code"], res["mutated"]), (False, code, False))
            self.assertIn(text, res["error"])

    def test_failed_operation_keeps_its_partial_change_log(self):
        failed = {"status": "failed", "error": "Stopped after 10 albums in a row failed", "error_code": "MBSYNC_ABORTED",
                  "result": {**DONE["result"], "aborted": True, "failed_count": 10,
                             "failed": [{"kind": "album", "id": 7, "error": "RuntimeError: down"}]}}
        res, log = self.run_sync(ScriptedAdapter([failed]))
        self.assertEqual((res["ok"], res["code"], res["mutated"]), (False, "MBSYNC_ABORTED", True))
        self.assertTrue(self.store.get(self.tx)["metadata"]["engine_result"]["aborted"])
        self.assertTrue(any("album 7: RuntimeError: down" in line for line in log))

    def test_lost_operation_and_unreachable_beets_are_failures(self):
        res, _ = self.run_sync(ScriptedAdapter([BeetsAdapterNotFoundError("gone")]))
        self.assertEqual((res["ok"], res["code"], res["mutated"]), (False, "operation_lost", None))
        res, _ = self.run_sync(ScriptedAdapter([BeetsAdapterConnectionError("down")]), max_poll_errors=3)
        self.assertEqual((res["ok"], res["code"]), (False, "engine_unreachable"))

    def test_cancel_is_forwarded_and_recorded_only_when_beets_stopped(self):
        cancel = CancelSignal()
        cancel.set()
        stopped = {"status": "succeeded", "result": {**DONE["result"], "cancelled": True, "processed": 1}}
        ad = ScriptedAdapter([{"status": "running", "result": {}}, stopped])
        res, log = self.run_sync(ad, cancel)
        self.assertIn(("cancel", "op1"), ad.calls)
        self.assertTrue(res["cancelled"])
        self.assertTrue(cancel.observed)  # the job ends Cancelled
        self.assertTrue(any("[cancelled]" in line for line in log))

        late = CancelSignal()
        late.set()
        ad = ScriptedAdapter([DONE])
        ad.cancel_mbsync_library = mock.Mock(side_effect=BeetsAdapterError("x", status_code=409, error_code="NOT_RUNNING"))
        res, log = self.run_sync(ad, late)
        self.assertFalse(res["cancelled"])
        self.assertFalse(late.observed)  # Beets had finished: the real outcome stands
        self.assertTrue(any("NOT_RUNNING" in line for line in log))


class MbsyncAllRouteTests(_RouteEnv):
    def setUp(self):
        super().setUp()
        import backend.transaction_service as ts
        import routes_library
        self.routes = routes_library
        for p in (mock.patch.object(ts, "transactions", self.store),
                  mock.patch.object(routes_library, "transactions", self.store),
                  mock.patch.object(routes_library, "_invalidate_lib_cache")):
            p.start()
            self.addCleanup(p.stop)

    def _start(self):
        resp = self.client.post("/api/library/mbsync-all")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        return self.routes.jobs.get(resp.get_json()["job_id"])

    def _wait(self, job):
        deadline = time.time() + 5
        while job.status == "running" and time.time() < deadline:
            time.sleep(0.01)

    def test_runs_the_beets_sync_on_its_own_transaction_and_prunes_nothing(self):
        seen = {}

        def fake(log, cancel_event=None, *, transaction_id="", store=None, **kw):
            seen.update(tx=transaction_id, store=store)
            return {"ok": True, "mutated": True, "changed": 1}

        with mock.patch.object(cw, "mbsync_library", side_effect=fake), \
             mock.patch.object(cw, "find_all_orphan_albums") as orphans, \
             mock.patch.object(cw, "delete_album") as delete:
            job = self._start()
            self._wait(job)
        self.assertEqual(job.status, "success")
        self.assertEqual(seen["tx"], job.metadata["transaction_id"])
        self.assertIs(seen["store"], self.store)
        tx = self.store.get(seen["tx"])
        self.assertEqual((tx["status"], tx["operation_type"], tx["rollback"]["available"]),
                         ("Completed", "MusicBrainz Match", False))
        self.assertIn("cannot be rolled back", tx["rollback"]["reason"])
        self.assertEqual(self.client.post(f"/api/transactions/{seen['tx']}/rollback").status_code, 409)
        orphans.assert_not_called()
        delete.assert_not_called()
        self.routes._invalidate_lib_cache.assert_called_once()

    def test_refusal_fails_the_job_and_transaction(self):
        with mock.patch.object(cw, "mbsync_library", return_value={"ok": False, "code": "BEETS_NOT_FOUND",
                                                                   "mutated": False, "error": "Restart beets"}):
            job = self._start()
            self._wait(job)
        self.assertEqual(job.status, "failed")
        self.assertEqual(self.store.get(job.metadata["transaction_id"])["status"], "Failed")
        self.routes._invalidate_lib_cache.assert_not_called()

    def test_second_start_is_409_and_cancel_ends_cancelled(self):
        started, stop = threading.Event(), threading.Event()

        def fake(log, cancel_event=None, **kw):
            started.set()
            while not cancel_event.is_set():
                time.sleep(0.01)
            stop.wait(5)
            return {"ok": True, "mutated": True, "cancelled": True}

        with mock.patch.object(cw, "mbsync_library", side_effect=fake):
            job = self._start()
            self.assertTrue(started.wait(5))
            again = self.client.post("/api/library/mbsync-all")
            self.assertEqual(again.status_code, 409)
            self.assertEqual((again.get_json()["code"], again.get_json()["job_id"]), ("job_already_running", job.job_id))
            self.assertEqual(self.client.post(f"/api/jobs/{job.job_id}/kill").status_code, 200)
            stop.set()
            self._wait(job)
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(self.store.get(job.metadata["transaction_id"])["status"], "Cancelled")


if __name__ == "__main__":
    unittest.main()
