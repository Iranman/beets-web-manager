"""Independent QA reproductions of PR #515, plus the reconcile's durable-write cases."""
import json
import os
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import backend.composite_workflows as cw
import backend.library_service as ls
import backend.transaction_service as ts
import job_engine
from job_engine import JobStore
from tests._folder_ops_local import patch_local_folder_ops
from tests.test_folder_mbid_names_apply import ARTIST_MBID, FakeResolver, _resolved
from tests.test_staging_mutation_hardening import _RouteEnv
from tests.test_wave0_s1_containment import FakeAdapter, _Env


class ProcessDied(BaseException):
    """Not an Exception: nothing in the runner may handle it, like a real crash."""


class _Sync(_RouteEnv):
    def setUp(self):
        super().setUp()
        self.stores = []

    def tearDown(self):
        for s in self.stores:
            s.close()
        super().tearDown()

    def jobstore(self):
        s = JobStore(self.data / "jobs")
        self.stores.append(s)
        return s

    def install(self, jobs):
        for p in (mock.patch.object(ts, "jobs", jobs), mock.patch.object(ts, "transactions", self.store)):
            p.start()
            self.addCleanup(p.stop)
        ts._install_transaction_job_hooks()

    def wait(self, job):
        deadline = time.time() + 5
        while job.status == "running" and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)  # done callbacks

    def read_status(self, tx_id):
        self.assertEqual(self.client.get("/api/transactions").status_code, 200)
        return self.client.get(f"/api/transactions/{tx_id}").get_json()["transaction"]["status"]

    def persisted_running(self, tx_id):
        """A job record left as running by a dead process."""
        jobs = self.jobstore()
        job = jobs.start_python(lambda log, cancel_event=None: {"ok": True}, label="Rollback x",
                                metadata={"transaction": False, "transaction_id": tx_id, "type": "transaction-rollback"})
        self.wait(job)
        jobs.close()
        path = self.data / "jobs" / f"{job.job_id}.json"
        rec = json.loads(path.read_text(encoding="utf-8"))
        rec.update(status="running", finished_at=None, returncode=None)
        path.write_text(json.dumps(rec), encoding="utf-8")
        return job.job_id, path


class TerminalStateTests(_Sync):
    def test_batch_apply_job_that_raises_settles_its_transaction(self):
        # The exact metadata of POST /api/library/missing-mb-ids/batch/apply.
        import backend.missing_mb_batch as mb
        tx = self.store.create(operation_type="Metadata Update", status="Preview")
        jobs = self.jobstore()
        self.install(jobs)
        real_update = self.store.update

        def update(tid, **kw):
            if "pre_apply_snapshot" in (kw.get("metadata") or {}):
                raise OSError("disk full")
            return real_update(tid, **kw)

        def body(log, cancel_event=None, update_state=None):
            return mb.run_apply(tx["id"], apply_album=None, apply_item=None, log=log,
                                cancel_event=cancel_event, update_state=update_state, store=self.store)

        with mock.patch.object(mb, "_snapshot", return_value={"albums": 0, "items": 0}), \
                mock.patch.object(self.store, "update", side_effect=update):
            job = jobs.start_python(body, label="Missing IDs batch apply", metadata={
                "type": "missing-mb-ids-batch-apply", "category": "library", "mutating": True,
                "dedupe_key": "missing-mb-ids-batch-apply", "transaction_id": tx["id"]})
            self.wait(job)
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.metadata["transaction_id"], tx["id"])
        self.assertEqual(self.read_status(tx["id"]), "Failed")

    def test_startup_pass_settles_recovered_job(self):
        tx = self.store.create(operation_type="Delete", status="Running")
        self.persisted_running(tx["id"])
        self.install(self.jobstore())
        self.assertEqual(self.read_status(tx["id"]), "Recovery Required")

    def test_crash_between_job_recovery_and_hook_install(self):
        # Boot 1 resolves the record and writes it terminal, then dies before
        # app.py installs the hooks (any import failure). Boot 2 sees a terminal
        # record that is not in jobs.recovered.
        tx = self.store.create(operation_type="Delete", status="Running")
        self.persisted_running(tx["id"])
        boot1 = self.jobstore()
        self.assertEqual(len(boot1.recovered), 1)
        boot1.close()
        boot2 = self.jobstore()
        self.assertEqual(boot2.recovered, [])
        self.install(boot2)
        self.assertEqual(self.read_status(tx["id"]), "Recovery Required")

    def test_transient_failure_of_the_startup_pass_is_retried(self):
        tx = self.store.create(operation_type="Delete", status="Running")
        self.persisted_running(tx["id"])
        with mock.patch.object(self.store, "update_from_job", side_effect=OSError("data dir not ready")):
            self.install(self.jobstore())
        self.assertEqual(self.read_status(tx["id"]), "Recovery Required")

    def test_straggler_resolved_after_startup_pass(self):
        tx = self.store.create(operation_type="Delete", status="Running")
        _jid, path = self.persisted_running(tx["id"])
        os.utime(path, (1, 1))
        (self.data / "jobs" / "newer.json").write_text(json.dumps(
            {"job_id": "newer", "label": "n", "status": "success", "created_at": time.time(),
             "finished_at": time.time(), "returncode": 0, "metadata": {}, "log_tail": []}), encoding="utf-8")
        gate = threading.Event()
        real_tail = JobStore._recover_tail

        def slow_tail(self_, paths, now):
            gate.wait(5)
            real_tail(self_, paths, now)

        with mock.patch.object(job_engine, "JOB_STARTUP_LOAD_LIMIT", 1), \
                mock.patch.object(JobStore, "_recover_tail", slow_tail):
            jobs = self.jobstore()
            self.install(jobs)
            gate.set()
            jobs._recover_thread.join(5)
        self.assertEqual(self.read_status(tx["id"]), "Recovery Required")

    def test_job_done_before_the_callback_is_registered(self):
        jobs = self.jobstore()
        self.install(jobs)
        real = job_engine.PythonJob.add_done_callback

        def late(job_, fn):
            while job_.status == "running":
                time.sleep(0.01)
            real(job_, fn)

        with mock.patch.object(job_engine.PythonJob, "add_done_callback", late):
            job = jobs.start_python(lambda log, cancel_event=None: log.append("x") or {"ok": True, "changed_count": 1},
                                    label="Library cleanup", metadata={"transaction": {"operation_type": "Delete"}})
        self.wait(job)
        tx = self.client.get(f"/api/transactions/{job.metadata['transaction_id']}").get_json()["transaction"]
        self.assertEqual(tx["status"], "Completed")
        self.assertIn("x", tx["logs"])
        self.assertIsNotNone(tx.get("result_summary"))


class RunningReadTests(_Sync):
    def _running(self):
        jobs = self.jobstore()
        self.install(jobs)
        release, seen = threading.Event(), threading.Event()

        def body(log, cancel_event=None):
            log.append("live job line")
            seen.set()
            release.wait(5)
            return {"ok": True}

        job = jobs.start_python(body, label="Library cleanup", metadata={"transaction": {"operation_type": "Delete"}})
        self.assertTrue(seen.wait(5))
        self.addCleanup(lambda: (release.set(), self.wait(job)))
        return job, release

    def test_cancelled_running_job_stays_running_on_read(self):  # PR body item 5
        job, _ = self._running()
        job.kill()
        self.assertEqual(self.read_status(job.metadata["transaction_id"]), "Running")

    def test_detail_shows_live_job_log_while_running(self):  # not listed in the PR body
        job, _ = self._running()
        tx = self.client.get(f"/api/transactions/{job.metadata['transaction_id']}").get_json()["transaction"]
        self.assertIn("live job line", tx["logs"])


class _Crash:
    """Kill the run at a stage, then resume from the last durable checkpoint."""

    def run_until_death(self, folders, die):
        checkpoints = []

        def update_state(u):
            checkpoints.append(json.loads(json.dumps(u["checkpoint"])))

        with mock.patch.object(cw, "safe_rename_library_folder", die):
            with self.assertRaises(ProcessDied):
                self.apply(folders, update_state=update_state)
        return checkpoints[-1]

    def resume(self, folders, cp):
        return self.apply(folders, resume_done=set(cp["done_folders"]), prior_operations=cp["operations"])

    def assert_rollback_restores(self, res, folders):
        rb = ls._rollback_folder_placeholder_apply(res["operations"], store=self.store)
        self.assertTrue(rb["ok"], rb)
        for f in folders:
            self.assertTrue(f.is_dir(), f)

    def test_die_before_rename(self):
        a, b = self.folders()

        def die(*_a, **_k):
            raise ProcessDied()

        cp = self.run_until_death([a, b], die)
        self.assertTrue(any(o.get("pending") and o["source"] == str(a) for o in cp["operations"]), cp)
        self.assertTrue(a.is_dir())
        res = self.resume([a, b], cp)
        self.assertTrue(res["ok"], res)
        self.assertFalse(a.exists() or b.exists())
        self.assert_rollback_restores(res, [a, b])

    def test_die_after_rename_before_checkpoint(self):
        a, b = self.folders()
        real = cw.safe_rename_library_folder

        def die(*args, **kw):
            real(*args, **kw)
            raise ProcessDied()

        cp = self.run_until_death([a, b], die)
        self.assertFalse(a.exists())
        self.assertEqual(cp["done_folders"], [])
        res = self.resume([a, b], cp)
        self.assertEqual(res["skipped"][0]["reason"], "source_missing", res)
        self.assertFalse(b.exists())
        self.assert_rollback_restores(res, [a, b])

    def test_die_on_second_after_first_checkpointed(self):
        a, b = self.folders()
        real = cw.safe_rename_library_folder
        calls = []

        def die(*args, **kw):
            calls.append(args)
            if len(calls) == 2:
                raise ProcessDied()
            return real(*args, **kw)

        cp = self.run_until_death([a, b], die)
        self.assertEqual(cp["done_folders"], [str(a)])
        res = self.resume([a, b], cp)
        self.assertTrue(res["ok"], res)
        ids = [o.get("operation_id") for o in res["operations"] if o.get("operation_id")]
        self.assertEqual(len(ids), len(set(ids)), res["operations"])
        self.assert_rollback_restores(res, [a, b])


class _Disk(_Env):
    def setUp(self):
        super().setUp()
        self.empty = FakeAdapter()
        p = mock.patch.object(cw, "beets_adapter", self.empty)
        p.start()
        self.addCleanup(p.stop)
        patch_local_folder_ops(self, self.music)

    def make(self, rel):
        f = self.music / rel
        f.mkdir(parents=True)
        (f / "01.flac").write_bytes(b"audio")
        return f


class PlaceholderCrash(_Crash, _Disk):
    def folders(self):
        return self.make("A/One (2001){Album MbId}"), self.make("A/Two (2002){Album MbId}")

    def apply(self, folders, **kw):
        return ls._apply_folder_name_placeholders([str(f) for f in folders], dry_run=False,
                                                  adapter=self.empty, store=self.store, **kw)


class MbidCrash(_Crash, _Disk):
    def folders(self):
        return (self.make(f"Art {{{ARTIST_MBID}}}/One (2001)"),
                self.make(f"Art {{{ARTIST_MBID}}}/Two (2002)"))

    def apply(self, folders, **kw):
        resolver = FakeResolver({"One": _resolved("11111111-1111-1111-1111-111111111111"),
                                 "Two": _resolved("22222222-2222-2222-2222-222222222222")})
        return ls._apply_folder_mbid_names([str(f) for f in folders], dry_run=False, resolver=resolver,
                                           adapter=self.empty, store=self.store, **kw)

    def test_relocation_op_is_checkpointed_before_the_move(self):
        tx = self.store.create(operation_type="Move", status="Preview")
        rgid = "11111111-1111-1111-1111-111111111111"
        a = self.make(f"Art {{{ARTIST_MBID}}}/One (2001)")
        checkpoints = []
        with mock.patch.object(ls, "_placeholder_folder_tracked", return_value=([5], 2)), \
                mock.patch.object(ls, "_album_rgid_via_adapter", return_value=rgid), \
                mock.patch.object(ls, "_album_db_paths_stale", return_value=False), \
                mock.patch.object(cw, "plan_album_relocation", return_value={"ok": True, "operation_id": tx["id"]}), \
                mock.patch.object(cw, "apply_album_relocation", side_effect=ProcessDied()):
            with self.assertRaises(ProcessDied):
                self.apply([a], update_state=lambda u: checkpoints.append(json.loads(json.dumps(u["checkpoint"]))))
        self.assertIn({"operation_id": tx["id"], "kind": "album"}, checkpoints[-1]["operations"])


class DedupCancelTests(_Env):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(cw, "_default_store", self.store)
        p.start()
        self.addCleanup(p.stop)

    def _service(self):
        import backend.dedup_service as d
        if not hasattr(d, "_approve_and_apply_reviewed_cleanup"):
            self.skipTest("base has no helper")
        return d

    def test_raise_after_claim_is_not_cancelled(self):
        d = self._service()
        tx = self.store.create(operation_type="Delete", status="Preview")

        def claim_then_raise(op):
            self.store.transition(op, "Approved", "Running")
            raise RuntimeError("mid apply")

        with mock.patch.object(d._duplicate_cleanup, "apply_reviewed_cleanup", claim_then_raise):
            with self.assertRaises(RuntimeError):
                d._approve_and_apply_reviewed_cleanup(tx["id"], "op")
        self.assertEqual(self.store.get(tx["id"])["status"], "Running")

    def test_only_proven_same_slot_copies_are_planned_and_preview_applies_nothing(self):
        d = self._service()
        items = [{"id": 1, "track": 3, "disc": 1, "path": "/m/a.flac"},
                 {"id": 2, "track": 3, "disc": 1, "path": "/m/a.1.flac"},
                 {"id": 3, "track": 3, "disc": 2, "path": "/m/b.flac"},
                 {"id": 4, "track": 0, "disc": 1, "path": "/m/c.flac"},
                 {"id": 5, "track": 0, "disc": 1, "path": "/m/d.flac"},
                 {"id": 6, "track": 4, "disc": 1, "path": "/m/e.flac"},
                 {"id": 7, "track": 4, "disc": 1, "path": "/m/e.2.flac"}]

        def proof(drop, keep, _t):
            ok = drop.endswith("a.1.flac")
            return {"proven": ok, "recording_id": "r" if ok else "", "reason": "" if ok else "unknown",
                    "drop_status": "x", "keep_status": "y"}

        with mock.patch.object(d, "same_recording_proof", side_effect=proof), \
                mock.patch.object(d._duplicate_cleanup, "plan_reviewed_cleanup",
                                  return_value={"ok": True, "operation_id": "op"}) as plan, \
                mock.patch.object(d._duplicate_cleanup, "apply_reviewed_cleanup") as apply_:
            res = d._album_dedup_quarantine_proven(3, items, False, [])
        self.assertEqual(plan.call_args.args[0], [{"delete_item_id": 2, "keep_item_id": 1}])
        apply_.assert_not_called()
        self.assertFalse(res["applied"])
        self.assertEqual({s["item_id"] for s in res["spared"]}, {4, 5, 7})


class TempFileCollisionTests(unittest.TestCase):
    # QA forced the collision through time_ns; the name now comes from uuid4,
    # so the same collision is forced there. The assertion is unchanged.
    def test_same_instant_writer_does_not_delete_the_other_writers_temp(self):
        from backend.json_state import atomic_write_text
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            same = mock.Mock(hex="same")
            other = Path(d) / ".state.json.same.tmp"
            other.write_text("another writer in flight")
            with mock.patch("backend.json_state.uuid.uuid4", return_value=same):
                try:
                    atomic_write_text(p, "x")
                except OSError:
                    pass
            self.assertTrue(other.exists(), "the colliding writer deleted the other writer's temp file")


class ReconcileTests(_Sync):
    """The durable write, not only the overlay a read shows."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(ts, "_RECONCILE_RETRY_SECONDS", 0.02)
        p.start()
        self.addCleanup(p.stop)

    def install(self, jobs):
        super().install(jobs)
        ts.start_transaction_reconcile_schedule()  # the server entrypoint's step (#631)

    def stored_status(self, tx_id, want):
        deadline = time.time() + 5
        while time.time() < deadline and self.store.get(tx_id)["status"] != want:
            time.sleep(0.01)
        return self.store.get(tx_id)["status"]

    def test_batch_apply_restart_is_recorded_as_recovery_required(self):
        # B1's restart case with the route's real metadata.
        tx = self.store.create(operation_type="Metadata Update", status="Running")
        jobs = self.jobstore()
        job = jobs.start_python(lambda log, cancel_event=None: {"ok": True}, label="Missing IDs batch apply", metadata={
            "type": "missing-mb-ids-batch-apply", "category": "library", "mutating": True,
            "dedupe_key": "missing-mb-ids-batch-apply", "transaction_id": tx["id"]})
        self.wait(job)
        jobs.close()
        path = self.data / "jobs" / f"{job.job_id}.json"
        rec = json.loads(path.read_text(encoding="utf-8"))
        rec.update(status="running", finished_at=None, returncode=None)
        path.write_text(json.dumps(rec), encoding="utf-8")
        self.install(self.jobstore())
        self.assertEqual(self.stored_status(tx["id"], "Recovery Required"), "Recovery Required")

    def test_crash_before_hook_install_is_written_not_only_shown(self):
        tx = self.store.create(operation_type="Delete", status="Running")
        self.persisted_running(tx["id"])
        self.jobstore().close()
        self.install(self.jobstore())
        self.assertEqual(self.stored_status(tx["id"], "Recovery Required"), "Recovery Required")

    def test_failed_startup_write_is_retried_and_written(self):
        tx = self.store.create(operation_type="Delete", status="Running")
        self.persisted_running(tx["id"])
        real, calls = self.store.update_from_job, []

        def flaky(*a, **k):
            calls.append(a)
            if len(calls) == 1:
                raise OSError("data dir not ready")
            return real(*a, **k)

        with mock.patch.object(self.store, "update_from_job", flaky):
            self.install(self.jobstore())
            self.assertEqual(self.stored_status(tx["id"], "Recovery Required"), "Recovery Required")
        self.assertGreaterEqual(len(calls), 2)

    def test_straggler_is_written_after_the_tail_sweep(self):
        tx = self.store.create(operation_type="Delete", status="Running")
        _jid, path = self.persisted_running(tx["id"])
        os.utime(path, (1, 1))
        (self.data / "jobs" / "newer.json").write_text(json.dumps(
            {"job_id": "newer", "label": "n", "status": "success", "created_at": time.time(),
             "finished_at": time.time(), "returncode": 0, "metadata": {}, "log_tail": []}), encoding="utf-8")
        gate = threading.Event()
        real_tail = JobStore._recover_tail

        def slow_tail(self_, paths, now):
            gate.wait(5)
            real_tail(self_, paths, now)

        with mock.patch.object(job_engine, "JOB_STARTUP_LOAD_LIMIT", 1), \
                mock.patch.object(JobStore, "_recover_tail", slow_tail):
            jobs = self.jobstore()
            self.install(jobs)
            time.sleep(0.1)  # passes run while the tail is held
            gate.set()
            self.assertEqual(self.stored_status(tx["id"], "Recovery Required"), "Recovery Required")

    def test_failed_done_callback_write_is_retried(self):
        jobs = self.jobstore()
        self.install(jobs)
        tx = self.store.create(operation_type="Delete", status="Running")
        real, calls = self.store.update_from_job, []

        def flaky(*a, **k):
            calls.append(a)
            if len(calls) == 1:
                raise OSError("disk full")
            return real(*a, **k)

        time.sleep(0.1)  # the boot pass has finished; only the callback can re-arm it
        with mock.patch.object(self.store, "update_from_job", flaky):
            job = jobs.start_python(lambda log, cancel_event=None: {"ok": True}, label="x",
                                    metadata={"transaction": False, "transaction_id": tx["id"], "type": "t"})
            self.wait(job)
            self.assertEqual(self.stored_status(tx["id"], "Completed"), "Completed")

    def test_live_job_keeps_its_transaction_running(self):
        jobs = self.jobstore()
        self.install(jobs)
        tx = self.store.create(operation_type="Delete", status="Running")
        release = threading.Event()
        job = jobs.start_python(lambda log, cancel_event=None: release.wait(5) and {"ok": True}, label="x",
                                metadata={"transaction": False, "transaction_id": tx["id"], "type": "t"})
        self.addCleanup(lambda: (release.set(), self.wait(job)))
        self.assertTrue(ts._reconcile_running_transactions(jobs, self.store))
        self.assertEqual(self.store.get(tx["id"])["status"], "Running")
        self.assertEqual(self.read_status(tx["id"]), "Running")

    def _rollback_claimed_after_apply(self):
        """Apply job finished and Completed; a rollback then claims Running."""
        jobs = self.jobstore()
        self.install(jobs)
        tx = self.store.create(operation_type="Metadata Update", status="Running")
        job = jobs.start_python(lambda log, cancel_event=None: {"ok": True}, label="x",
                                metadata={"transaction": False, "transaction_id": tx["id"], "type": "t"})
        self.wait(job)
        self.store.attach_job(tx["id"], job.job_id)
        self.assertEqual(self.store.get(tx["id"])["status"], "Completed")
        time.sleep(0.01)
        self.assertIsNotNone(self.store.transition(tx["id"], "Completed", "Running"))
        return jobs, job, tx["id"]

    def test_finished_apply_job_never_settles_a_later_rollback_claim(self):
        jobs, job, tx_id = self._rollback_claimed_after_apply()
        self.assertTrue(ts._reconcile_running_transactions(jobs, self.store))
        self.store.update_from_job(tx_id, job)
        self.assertEqual(self.store.get(tx_id)["status"], "Running")
        self.assertEqual(self.read_status(tx_id), "Running")

    def test_live_rollback_job_speaks_over_the_finished_apply_job(self):
        jobs, _apply, tx_id = self._rollback_claimed_after_apply()
        release = threading.Event()

        def body(log, cancel_event=None):
            log.append("restoring")
            release.wait(5)
            return {"ok": True}

        job = jobs.start_python(body, label="Rollback x",
                                metadata={"transaction": False, "transaction_id": tx_id, "type": "transaction-rollback"})
        self.addCleanup(lambda: (release.set(), self.wait(job)))
        deadline = time.time() + 5
        while "restoring" not in job.log and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(ts._reconcile_running_transactions(jobs, self.store))
        detail = self.client.get(f"/api/transactions/{tx_id}").get_json()["transaction"]
        self.assertEqual(detail["status"], "Running")
        self.assertIn("restoring", detail["logs"])

    def test_running_transaction_with_no_job_is_left_running_and_logged(self):
        tx = self.store.create(operation_type="Delete", status="Running")
        jobs = self.jobstore()
        # The judging pass (``before`` given) warns; the earlier passes only
        # log at debug while the startup job scan runs (#545).
        with self.assertLogs(ts._app_logger, "WARNING") as logs:
            self.assertTrue(ts._reconcile_running_transactions(jobs, self.store, time.time() + 1))
        self.assertIn(tx["id"], "".join(logs.output))
        self.assertEqual(self.store.get(tx["id"])["status"], "Running")

    def _orphaned_wrapper(self, job_type="playlist-sync", **meta):
        """The live record (txn_1792040789_…): created by the job hook, then
        the process died before the job was attached or its record kept."""
        return self.store.create(operation_type="Playlist Import", status="Running",
                                 summary="Playlist sync (automatic)", reason=ts._JOB_WRAPPER_REASON,
                                 source=job_type, metadata={"type": job_type, "trigger": "auto",
                                                            "dedupe_key": "playlist-sync", **meta})

    def test_orphaned_job_wrapper_from_an_earlier_process_is_recovery_required(self):
        tx = self._orphaned_wrapper()
        time.sleep(0.01)
        self.install(self.jobstore())
        self.assertEqual(self.stored_status(tx["id"], "Recovery Required"), "Recovery Required")
        self.assertIn("job record is gone", " ".join(self.store.get(tx["id"])["logs"]))

    def test_orphaned_read_only_job_wrapper_is_failed(self):
        tx = self._orphaned_wrapper(job_type="dedup-scan", **{ts._JOB_WRAPPER_MARK: True})
        jobs = self.jobstore()
        self.assertTrue(ts._reconcile_running_transactions(jobs, self.store, before=time.time() + 1))
        self.assertEqual(self.store.get(tx["id"])["status"], "Failed")

    def test_job_wrapper_written_in_this_process_is_left_running(self):
        # Between the hook's create and its job start there is no job yet.
        started = time.time()
        time.sleep(0.01)
        tx = self._orphaned_wrapper()
        jobs = self.jobstore()
        self.assertTrue(ts._reconcile_running_transactions(jobs, self.store, before=started))
        self.assertTrue(ts._reconcile_running_transactions(jobs, self.store))
        self.assertEqual(self.store.get(tx["id"])["status"], "Running")

    def test_orphan_with_engine_evidence_is_left_to_the_recovery_sweep(self):
        txs = [self._orphaned_wrapper(**{ts._JOB_WRAPPER_MARK: True, key: value}) for key, value in (
            ("mutation_family", "album_relocation_v1"), ("engine_request", {"operation_id": "x"}),
            ("engine_operation_id", "op1"), ("rollback_request", {"requested_at": 1}),
            ("engine_result", {"mutation_started": True}), ("mutation_started", True),
            ("filesystem_mutated", True))]
        jobs = self.jobstore()
        self.assertTrue(ts._reconcile_running_transactions(jobs, self.store, before=time.time() + 1))
        self.assertEqual({self.store.get(t["id"])["status"] for t in txs}, {"Running"})

    def test_hook_marks_the_transactions_it_creates(self):
        with mock.patch.object(ts, "transactions", self.store):
            tx = ts._transaction_create_for_job("Playlist sync (automatic)", {"type": "playlist-sync"})
        self.assertTrue(tx["metadata"][ts._JOB_WRAPPER_MARK])
        self.assertTrue(ts._is_job_wrapper(tx))

    def test_job_that_does_not_name_the_transaction_never_settles_it(self):
        jobs = self.jobstore()
        job = jobs.start_python(lambda log, cancel_event=None: {"ok": True}, label="x", metadata={"type": "t"})
        self.wait(job)
        tx = self.store.create(operation_type="Delete", status="Running")
        self.store.update(tx["id"], originating_job=job.job_id)
        self.assertTrue(ts._reconcile_running_transactions(jobs, self.store))
        self.assertEqual(self.store.get(tx["id"])["status"], "Running")

    def test_overlaid_read_writes_nothing(self):
        tx = self.store.create(operation_type="Delete", status="Running")
        self.persisted_running(tx["id"])
        jobs = self.jobstore()
        for p in (mock.patch.object(ts, "jobs", jobs), mock.patch.object(ts, "transactions", self.store)):
            p.start()
            self.addCleanup(p.stop)
        path = self.store.root / f"{tx['id']}.json"
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        time.sleep(0.01)
        self.assertEqual(self.read_status(tx["id"]), "Recovery Required")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)


if __name__ == "__main__":
    unittest.main()
