"""Job reliability (fix/jobs-reliability-0.4): duplicate-start protection for
retried/lost requests, restart truthfulness, central redaction of persisted
job records (#272), the scheduler hook, done callbacks, the ad hoc thread
inventory, and the one build-identity source."""

import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import job_engine
from job_engine import DuplicateJobError, IdempotencyKeyReuseError, JobStore
from tests.test_staging_mutation_hardening import _RouteEnv

ROOT = Path(__file__).resolve().parent.parent


def _wait(job, timeout=5.0):
    deadline = time.time() + timeout
    while job.status == "running" and time.time() < deadline:
        time.sleep(0.01)


class _Scope:
    """A request's start scope, as routes_jobs opens it."""

    def __init__(self, fingerprint="fp-1", key=""):
        self.args = (lambda: fingerprint, key)

    def __enter__(self):
        self.token = job_engine.enter_start_scope(*self.args)

    def __exit__(self, *exc):
        job_engine.exit_start_scope(self.token)


class PersistedRecordRedactionTests(unittest.TestCase):
    """#272: secrets a producer forgot to redact never reach the job file."""

    def test_log_tail_and_result_are_redacted_on_disk_only(self):
        lines = ["calling https://admin:hunter2pass@lidarr.local/api", "token=abcd1234secretvalue",
                 "Authorization: Bearer sk-livelivelive123"]
        with tempfile.TemporaryDirectory() as d:
            store = JobStore(d)
            job = store.start_python(lambda log: (log.extend(lines),
                                                  {"api_key": "plainkey123", "note": "password=hunter2pass"})[1],
                                     label="leaky")
            _wait(job)
            store.close()
            text = (Path(d) / f"{job.job_id}.json").read_text(encoding="utf-8")
        for secret in ("hunter2pass", "abcd1234secretvalue", "sk-livelivelive123", "plainkey123"):
            self.assertNotIn(secret, text)
        self.assertIn("[REDACTED]", text)
        self.assertEqual(job.log[:3], lines)  # the live job is untouched

    def test_redaction_keeps_shape_and_plain_text(self):
        value = {"items": ["ok", "https://u:p4ss@h/x"], "count": 3, "nested": {"token": "t0k3nvalue"}}
        out = job_engine.redact_for_persistence(value)
        self.assertEqual(out["count"], 3)
        self.assertEqual(out["items"][0], "ok")
        self.assertNotIn("p4ss", json.dumps(out))
        self.assertNotIn("t0k3nvalue", json.dumps(out))


class RequestScopeDuplicateTests(unittest.TestCase):
    """A lost response or a retry never runs the same operation twice."""

    def setUp(self):
        self.go = threading.Event()
        self.addCleanup(self.go.set)
        self.store = JobStore()

    def _blocking(self, log):
        self.go.wait(5)

    def test_identical_request_is_refused_while_running(self):
        with _Scope("same"):
            first = self.store.start_python(self._blocking, label="x")
        with _Scope("same"), self.assertRaises(DuplicateJobError) as ctx:
            self.store.start_python(self._blocking, label="x")
        self.assertIs(ctx.exception.job, first)
        self.assertFalse(ctx.exception.replay)
        with _Scope("different body"):
            self.store.start_python(self._blocking, label="x")
        self.assertEqual(len(self.store.all()), 2)

    def test_identical_request_after_finish_starts_again_without_a_key(self):
        self.go.set()
        with _Scope("same"):
            first = self.store.start_python(self._blocking, label="x")
        _wait(first)
        with _Scope("same"):
            self.store.start_python(self._blocking, label="x")
        self.assertEqual(len(self.store.all()), 2)

    def test_idempotency_key_replays_finished_job_and_never_reruns(self):
        calls = []
        with _Scope("req", "key-00000001"):
            first = self.store.start_python(lambda log: calls.append(1), label="x")
        _wait(first)
        with _Scope("req", "key-00000001"), self.assertRaises(DuplicateJobError) as ctx:
            self.store.start_python(lambda log: calls.append(2), label="x")
        self.assertTrue(ctx.exception.replay)
        self.assertIs(ctx.exception.job, first)
        self.assertEqual(calls, [1])

    def test_idempotency_key_with_a_different_request_is_rejected(self):
        with _Scope("req-a", "key-00000002"):
            self.store.start_python(self._blocking, label="x")
        with _Scope("req-b", "key-00000002"), self.assertRaises(IdempotencyKeyReuseError):
            self.store.start_python(self._blocking, label="x")

    def test_read_only_jobs_are_not_refused(self):
        meta = {"type": "dedup-scan"}
        for _ in range(2):
            with _Scope("same"):
                self.store.start_python(self._blocking, label="scan", metadata=dict(meta))
        self.assertEqual(len(self.store.all()), 2)

    def test_only_the_first_job_of_a_request_is_guarded(self):
        with _Scope("batch"):
            self.store.start_python(self._blocking, label="a")
            self.store.start_python(self._blocking, label="b")  # same request, second job
        self.assertEqual(len(self.store.all()), 2)

    def test_explicit_dedupe_key_still_wins(self):
        with _Scope("one"):
            self.store.start_python(self._blocking, label="x", metadata={"dedupe_key": "k"})
        with _Scope("two"), self.assertRaises(DuplicateJobError):
            self.store.start_python(self._blocking, label="x", metadata={"dedupe_key": "k"})

    def test_racing_identical_starts_run_once(self):
        results = []
        barrier = threading.Barrier(8)

        def start():
            with _Scope("race"):
                barrier.wait(5)
                try:
                    results.append(self.store.start_python(self._blocking, label="x").job_id)
                except DuplicateJobError as exc:
                    results.append(("dup", exc.job.job_id))
        threads = [threading.Thread(target=start) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        started = [r for r in results if isinstance(r, str)]
        self.assertEqual(len(started), 1)
        self.assertEqual({r[1] for r in results if isinstance(r, tuple)}, set(started))


class RestartTruthfulnessTests(unittest.TestCase):
    """Jobs survive a container restart truthfully: none stays running, the
    interrupted ones say so, keep their checkpoint, and a retried start of
    the same request is not re-run."""

    def test_interrupted_jobs_after_restart(self):
        go = threading.Event()
        with tempfile.TemporaryDirectory() as d:
            store = JobStore(d)

            def body(log, cancel, update_state):
                update_state({"checkpoint": {"done": 3, "total": 10}})
                log.append("halfway")
                go.wait(5)
            with _Scope("apply", "key-restart-1"):
                mutating = store.start_python(body, label="Apply", metadata={"type": "move-all"})
            reader = store.start_python(lambda log: go.wait(5), label="Scan", metadata={"type": "dedup-scan"})
            deadline = time.time() + 5
            while time.time() < deadline and not (mutating.state.get("checkpoint") and reader.started_at):
                time.sleep(0.01)
            time.sleep(0.05)
            # The "process" dies here: a second store reads what is on disk.
            restarted = JobStore(d)
            go.set()
            store.close()
            m, r = restarted.get(mutating.job_id), restarted.get(reader.job_id)
            self.assertEqual(m.status, "recovery_required")
            self.assertEqual(r.status, "failed")
            self.assertTrue(all(j.status != "running" for j in restarted.all()))
            row = m.to_dict()
            self.assertTrue(row["interrupted"])
            self.assertEqual(row["recovery"]["checkpoint"], {"done": 3, "total": 10})
            self.assertTrue(any("restarted" in line for line in m.log))
            # The client retries the lost request after the restart: not re-run.
            with _Scope("apply", "key-restart-1"), self.assertRaises(DuplicateJobError) as ctx:
                restarted.start_python(body, label="Apply", metadata={"type": "move-all"})
            self.assertEqual(ctx.exception.job.status, "recovery_required")
            restarted.close()

    def test_thread_start_failure_is_persisted_failed(self):
        """#229, durable: the record on disk is not left running either."""
        with tempfile.TemporaryDirectory() as d:
            store = JobStore(d)
            with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("no threads")):
                with self.assertRaises(RuntimeError):
                    store.start_python(lambda log: None, label="x")
            (job,) = store.all()
            store.close()
            rec = json.loads((Path(d) / f"{job.job_id}.json").read_text(encoding="utf-8"))
            self.assertEqual(rec["status"], "failed")
            self.assertEqual(JobStore(d).get(job.job_id).status, "failed")


class DoneCallbackTests(unittest.TestCase):
    def test_called_once_on_finish_and_at_once_when_already_done(self):
        go, seen = threading.Event(), []
        job = JobStore().start_python(lambda log: go.wait(5), label="x")
        job.add_done_callback(lambda j: seen.append(("late", j.status)))
        job.add_done_callback(lambda j: 1 / 0)  # a failing callback harms nothing
        with self.assertLogs("app.job_engine", "ERROR"):
            go.set()
            _wait(job)
            time.sleep(0.05)
        job.add_done_callback(lambda j: seen.append(("after", j.status)))
        self.assertEqual(seen, [("late", "success"), ("after", "success")])


class SchedulerTests(unittest.TestCase):
    def test_every_ticks_skips_while_its_job_runs_and_stops_on_close(self):
        store = JobStore()
        go = threading.Event()
        ticks = []

        def tick():
            ticks.append(time.time())
            if len(ticks) == 2:
                raise RuntimeError("one bad tick")  # logged; the schedule continues
            if len(ticks) == 3:
                return store.start_python(lambda log: go.wait(5), label="recurring")
            return None
        with self.assertLogs("app.job_engine", "ERROR"):
            store.every("t", 0.02, tick)
            deadline = time.time() + 5
            while len(ticks) < 3 and time.time() < deadline:
                time.sleep(0.01)
        time.sleep(0.2)
        self.assertEqual(len(ticks), 3)  # skipped while the job it started runs
        go.set()
        deadline = time.time() + 5
        while len(ticks) < 4 and time.time() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(len(ticks), 4)
        with self.assertRaises(ValueError):
            store.every("t", 1, tick)
        self.assertTrue(store.close(timeout=5))
        count = len(ticks)
        time.sleep(0.1)
        self.assertEqual(len(ticks), count)


class JobRouteDuplicateStartTests(_RouteEnv):
    """End to end through a real mutating start route and routes_jobs' hooks."""

    URL = "/api/clean/remove-orphaned-items"
    BODY = {"item_ids": [7, 8], "dry_run": False}

    def setUp(self):
        super().setUp()
        import routes_cleanup
        self.go = threading.Event()
        self.calls = []

        def fake(item_ids, dry_run, log):
            self.calls.append((tuple(item_ids), dry_run))
            self.go.wait(5)
            return {"ok": True}
        p = mock.patch.object(routes_cleanup, "_clean_remove_orphaned_items", fake)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.go.set)

    def _job(self, job_id):
        from backend.app_runtime import jobs
        return jobs.get(job_id)

    def test_retry_while_running_returns_the_running_job(self):
        first = self.client.post(self.URL, json=self.BODY)
        self.assertEqual(first.status_code, 200, first.get_json())
        retry = self.client.post(self.URL, json=self.BODY)
        self.assertEqual(retry.status_code, 409)
        body = retry.get_json()
        self.assertEqual((body["code"], body["job_id"], body["status"]),
                         ("job_already_running", first.get_json()["job_id"], "running"))
        other = self.client.post(self.URL, json={"item_ids": [9], "dry_run": False})
        self.assertEqual(other.status_code, 200)
        self.go.set()
        _wait(self._job(first.get_json()["job_id"]))
        _wait(self._job(other.get_json()["job_id"]))
        self.assertEqual(sorted(self.calls), [((7, 8), False), ((9,), False)])

    def test_idempotency_key_after_the_job_finished(self):
        headers = {"Idempotency-Key": "ui-click-5f2a91"}
        self.go.set()
        first = self.client.post(self.URL, json=self.BODY, headers=headers)
        job_id = first.get_json()["job_id"]
        _wait(self._job(job_id))
        retry = self.client.post(self.URL, json=self.BODY, headers=headers)
        self.assertEqual(retry.status_code, 409)
        self.assertEqual((retry.get_json()["code"], retry.get_json()["job_id"], retry.get_json()["replay"]),
                         ("job_already_started", job_id, True))
        reused = self.client.post(self.URL, json={"item_ids": [1], "dry_run": False}, headers=headers)
        self.assertEqual((reused.status_code, reused.get_json()["code"]), (422, "idempotency_key_reused"))
        bad = self.client.post(self.URL, json=self.BODY, headers={"Idempotency-Key": "bad key!"})
        self.assertEqual((bad.status_code, bad.get_json()["code"]), (400, "invalid_idempotency_key"))
        self.assertEqual(self.calls, [((7, 8), False)])

    def test_concurrent_maintenance_runner_starts_one_job(self):
        """D1: six racing Clean All starts (different bodies, so the request
        guard alone does not match) start exactly one mutating run."""
        import routes_maintenance
        from app import app
        runs = []

        def fake_wrap(fn):
            def run(log, cancel_event=None, update_state=None):
                runs.append(1)
                self.go.wait(5)
                return {"ok": True}
            return run
        for name, value in (("_run_with_app_context", fake_wrap),
                            # Widen the TOCTOU window: the fast-path check never sees the others.
                            ("_maintenance_running_job", lambda: None)):
            p = mock.patch.object(routes_maintenance, name, value)
            p.start()
            self.addCleanup(p.stop)
        barrier, responses = threading.Barrier(6), []

        def post(n):
            client = app.test_client()
            barrier.wait()
            responses.append(client.post("/api/jobs/maintenance-runner", json={"force_fresh": bool(n % 2), "n": n}))
        threads = [threading.Thread(target=post, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        bodies = [r.get_json() for r in responses]
        self.assertEqual([r.status_code for r in responses], [200] * 6, bodies)
        self.assertEqual(len({b["job_id"] for b in bodies}), 1, bodies)
        self.assertEqual(sum(1 for b in bodies if b.get("already_running")), 5, bodies)
        self.go.set()
        _wait(self._job(bodies[0]["job_id"]))
        self.assertEqual(len(runs), 1)

    def test_second_import_of_the_same_source_is_refused_while_running(self):
        """D2: the album-count delta of a second concurrent import of one
        folder counted the first import's album as its own."""
        import routes_import
        src = Path(tempfile.mkdtemp())
        imports = []

        def fake_reimport(path, beets_options=None, timeout=None):
            imports.append(path)
            self.go.wait(5)
            return {"ok": True, "albums_imported": 1, "not_matched": [], "skipped": [], "not_matched_known": True}
        for target, name, value in (
                (routes_import, "_resolve_import_source_path", lambda raw: (Path(raw), None)),
                (routes_import, "_validate_import_source_audio", lambda *a, **k: None),
                (routes_import, "_delete_if_already_in_library", lambda *a, **k: None),
                (routes_import, "_record_import_skips", lambda *a, **k: None),
                (routes_import, "_invalidate_lib_cache", lambda *a, **k: None),
                (routes_import.composite_workflows, "reimport_source", fake_reimport)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        first = self.client.post("/api/import", json={"path": str(src)})
        self.assertEqual(first.status_code, 200, first.get_json())
        second = self.client.post("/api/import", json={"path": str(src), "write": False})
        self.assertEqual((second.status_code, second.get_json()["code"], second.get_json()["job_id"]),
                         (409, "job_already_running", first.get_json()["job_id"]))
        other = Path(tempfile.mkdtemp())
        self.assertEqual(self.client.post("/api/import", json={"path": str(other)}).status_code, 200)
        self.go.set()
        _wait(self._job(first.get_json()["job_id"]))
        self.assertEqual(sorted(imports), sorted([str(src), str(other)]))

    def test_album_mb_routes_return_the_actionable_reason_only(self):
        """D15: a missing Release ID is actionable; other failures stay generic."""
        import routes_library
        cases = (
            ("Album does not have a MusicBrainz release ID", 400, "no_release_id"),
            ("Album does not have a selected MusicBrainz release ID", 400, "no_release_id"),
            ("Album 41 not found", 404, "album_not_found"),
            ("Could not read album 41: /internal/db/path secret=abc", 400, None),
        )
        for fn, url in (("_album_mb_completeness", "/api/albums/41/mb-completeness"),
                        ("_album_duplicate_resolver_plan", "/api/albums/41/duplicate-resolver")):
            for text, status, code in cases:
                with mock.patch.object(routes_library, fn, side_effect=RuntimeError(text)):
                    resp = self.client.get(url)
                body = resp.get_json()
                self.assertEqual((resp.status_code, body.get("code")), (status, code), (url, text, body))
                self.assertNotIn("/internal/db/path", resp.get_data(as_text=True))
                if code == "no_release_id":
                    self.assertIn("does not have a MusicBrainz release ID", body["error"])

    def test_reads_are_not_scoped(self):
        self.assertEqual(self.client.get("/api/jobs").status_code, 200)
        self.assertIsNone(job_engine._start_scope.get())


class AdHocThreadInventoryTests(unittest.TestCase):
    """Long-running work runs on job_engine. Every remaining raw thread is
    listed here with its reason; a new one fails this test (add it to the
    job engine instead, or justify it here)."""

    ALLOWED = {
        "job_engine.py": 4,                    # the infrastructure: job threads, heartbeat, schedules
        "backend/job_contract.py": 1,          # lock heartbeat inside a running job, joined
        "backend/resource_locks.py": 1,        # lock heartbeat, joined
        "backend/transaction_recovery.py": 1,  # startup recovery sweep (library-transaction)
        "backend/ai_service.py": 1,            # per-folder AI calls inside an AI batch job
        "backend/playlist_service.py": 2,      # auto-sync worker + index warm (integrations)
        "backend/plex_service.py": 1,          # Plex request wall timeout (integrations)
        "routes_maintenance.py": 1,            # /api/restart: re-exec after the response
        "app.py": 1,                           # yt-dlp availability probe at boot (security)
    }

    def test_no_new_ad_hoc_threads(self):
        found = {}
        files = [ROOT / "app.py", ROOT / "job_engine.py", ROOT / "helpers_mb.py",
                 *ROOT.glob("routes_*.py"), *(ROOT / "backend").rglob("*.py")]
        for path in files:
            n = len(re.findall(r"threading\.Thread\(|(?<![.\w])Thread\(", path.read_text(encoding="utf-8")))
            if n:
                found[path.relative_to(ROOT).as_posix()] = n
        self.assertEqual(found, self.ALLOWED)


class BuildInfoTests(unittest.TestCase):
    """One source for the running version: BUILD_INFO.json (written by the
    image build from its OCI label values), else VERSION. Never the env."""

    def _info(self, root):
        import backend.build_info as bi
        bi.build_info.cache_clear()
        self.addCleanup(bi.build_info.cache_clear)
        with mock.patch.object(bi, "_ROOT", Path(root)):
            return bi.build_info()

    def test_image_build_info_wins(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "VERSION").write_text("0.3.0\n", encoding="utf-8")
            Path(d, "BUILD_INFO.json").write_text(json.dumps(
                {"version": "0.4.0", "revision": "5ebf1d0abc", "created": "2026-10-11T00:00:00Z"}), encoding="utf-8")
            with mock.patch.dict("os.environ", {"BEETS_WEB_MANAGER_VERSION": "0.1.49"}):
                info = self._info(d)
        self.assertEqual(info, {"version": "0.4.0", "revision": "5ebf1d0abc",
                                "created": "2026-10-11T00:00:00Z", "source": "image"})

    def test_source_tree_and_bad_input_fall_back(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "VERSION").write_text("0.3.0\n", encoding="utf-8")
            Path(d, "BUILD_INFO.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(self._info(d), {"version": "0.3.0", "revision": "unknown", "created": "",
                                             "source": "source-tree"})
            Path(d, "BUILD_INFO.json").write_text(json.dumps({"version": "<script>"}), encoding="utf-8")
            self.assertEqual(self._info(d)["version"], "0.3.0")
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self._info(d)["source"], "none")

    def test_repo_runtime_never_reads_the_deploy_variable(self):
        for path in [ROOT / "app.py", *ROOT.glob("routes_*.py"), *(ROOT / "backend").rglob("*.py")]:
            text = path.read_text(encoding="utf-8")
            if "BEETS_WEB_MANAGER_VERSION" in text:
                # config_layers lists it as Compose-only; build_info names it as never read.
                self.assertIn(path.name, {"config_layers.py", "build_info.py"}, path)
                self.assertNotRegex(text, r"(environ|getenv)[^\n]*BEETS_WEB_MANAGER_VERSION")


class VersionEndpointTests(_RouteEnv):
    def test_liveness_and_status_report_the_same_build(self):
        from backend.build_info import build_info
        live = self.client.get("/health/live").get_json()
        self.assertEqual((live["version"], live["revision"]),
                         (build_info()["version"], build_info()["revision"]))


if __name__ == "__main__":
    unittest.main()
