"""Tests for the AI Batch Import retry-race fix (see PR description for the
full incident writeup and design rationale). Source-text tests below give
limited structural protection for a few source-ordering invariants that
are awkward to assert behaviorally; the classes further down import the
real app.py into an isolated temp environment and execute the actual
functions -- that behavioral coverage is the primary proof this fix works,
not the source-text assertions.
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
INTAKE_PANEL_SOURCE = (ROOT / "frontend" / "src" / "features" / "intake" / "IntakePanel.tsx").read_text(encoding="utf-8")
API_TYPES_SOURCE = (ROOT / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")


def _function_source(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


class StartAiBatchJobRaceTests(unittest.TestCase):
    def setUp(self):
        self.fn = _function_source(
            APP_SOURCE,
            "def _start_ai_batch_job(scan_path: str, recover_batch_job_id: str = \"\", *, retry_failed: bool = False):",
            "\n\n@app.post(\"/api/ai-batch-skip\")",
        )

    def test_reservation_guards_the_whole_start_sequence(self):
        self.assertIn("if not _ai_batch_try_reserve_start(batch_job_id):", self.fn)
        self.assertIn('"reconnected": True,', self.fn)
        reserve_pos = self.fn.index("if not _ai_batch_try_reserve_start(batch_job_id):")
        try_pos = self.fn.index("try:")
        finally_pos = self.fn.index("finally:")
        release_pos = self.fn.index("_ai_batch_release_start(batch_job_id)")
        self.assertLess(reserve_pos, try_pos)
        self.assertLess(try_pos, finally_pos)
        self.assertLess(finally_pos, release_pos)

    def test_recover_path_uses_targeted_association_not_full_commit(self):
        # The bug: an unconditional _ai_batch_commit() call after the worker
        # thread was already unblocked, racing that thread's own reconcile
        # + commit cycle. The fix uses a minimal, targeted association write
        # for the recover/retry branch instead of a full commit.
        recover_branch = self.fn[self.fn.index("if recover_batch_job_id:"):self.fn.index("else:")]
        self.assertNotIn("_ai_batch_commit(", recover_branch)
        self.assertIn("_ai_batch_persist_job_association(batch_job_id, job.job_id)", recover_branch)
        self.assertIn("state = _ai_batch_load_state(batch_job_id)", recover_branch)

    def test_fresh_batch_path_still_commits_running_status(self):
        else_branch = self.fn[self.fn.index("else:"):]
        self.assertIn('state["status"] = "running"', else_branch)
        self.assertIn("_ai_batch_commit(state, job.update_state)", else_branch)

    def test_worker_thread_is_unblocked_after_job_association_is_persisted(self):
        # Reversed from the first version of this fix: the worker must not
        # be unblocked (and allowed to write batch state) until the job_id
        # association is durably persisted, or an immediate status poll by
        # job_id could miss the batch. job_id_ready.set() must therefore
        # come AFTER the recover/fresh branch that persists the association,
        # not before it.
        branch_pos = self.fn.index("if recover_batch_job_id:")
        set_pos = self.fn.index("job_id_ready.set()")
        self.assertLess(branch_pos, set_pos)


class RunAiBatchImportReconciliationCommitOrderTests(unittest.TestCase):
    def setUp(self):
        self.fn = _function_source(
            APP_SOURCE,
            "def _run_ai_batch_import(batch_job_id: str, scan_path: str, log: list, cancel_event=None, update_state=None, *, recover: bool = False, retry_failed: bool = False, job_id: str = \"\") -> Dict[str, Any]:",
            "\n\ndef _start_ai_batch_job(",
        )

    def test_no_commit_between_setting_running_and_the_reconciliation_block(self):
        # The bug: an _ai_batch_commit() call (which internally calls
        # _ai_batch_recalculate_batch_state and can finalize state["status"]
        # to a terminal value) used to sit right here, BEFORE the
        # recover/retry reconciliation block that requeues retryable
        # folders -- so the finalize saw stale, unreconciled folder_states
        # and stamped a terminal status onto the shared state dict before
        # reconciliation ever got a chance to prove there was unfinished
        # work. No commit may happen in this gap now.
        running_pos = self.fn.index('state["status"] = "running"')
        reconcile_block_start = self.fn.index("folder_states = state.setdefault(")
        gap = self.fn[running_pos:reconcile_block_start]
        self.assertNotIn("_ai_batch_commit(", gap)

    def test_exactly_one_commit_sits_after_reconciliation_and_before_the_terminal_check(self):
        # The single commit that persists reconciled folder_states must come
        # after the reconciliation log line and before the terminal-status
        # bail-out check, so _ai_batch_recalculate_batch_state sees the
        # correct (non-zero, for a genuine retry) unfinished count.
        reconciled_marker = self.fn.index('log.append(f"Reconciled state:')
        terminal_check_pos = self.fn.index('if state.get("status") in _AI_BATCH_TERMINAL_STATUSES:')
        between = self.fn[reconciled_marker:terminal_check_pos]
        self.assertEqual(between.count("_ai_batch_commit("), 1)

    def test_fresh_batch_branch_still_commits_before_the_slow_disk_walk(self):
        # The fresh-batch path (no existing folder_states) legitimately still
        # needs an early commit so "running" is visible before os.walk runs
        # -- only the recover/retry branch had to lose its premature commit.
        fresh_branch = self.fn[
            self.fn.index("if not recover or not folder_states:"):self.fn.index("else:")
        ]
        self.assertIn("_ai_batch_commit(state, update_state)", fresh_branch)
        walk_pos = fresh_branch.index("_ai_batch_find_audio_dirs(scan_path)")
        commit_pos = fresh_branch.index("_ai_batch_commit(state, update_state)")
        self.assertLess(commit_pos, walk_pos)


class RecalculateBatchStateSelfHealTests(unittest.TestCase):
    def setUp(self):
        self.fn = _function_source(
            APP_SOURCE,
            "def _ai_batch_recalculate_batch_state(state: Dict[str, Any], log: Optional[list] = None) -> bool:",
            "\n\ndef _ai_batch_commit(",
        )

    def test_unfinished_with_terminal_previous_status_reopens_to_running(self):
        # The bug: a contradictory persisted state (unfinished folders exist,
        # but status is stuck terminal from an earlier premature finalize)
        # was left in place forever -- recover/retry both trust status and
        # short-circuit to a no-op reconnect. Must self-heal by reopening.
        self.assertIn("if unfinished > 0:", self.fn)
        heal_block = self.fn[self.fn.index("if unfinished > 0:"):self.fn.index("if total <= 0 or processed < total:")]
        self.assertIn("if previous_status in _AI_BATCH_TERMINAL_STATUSES:", heal_block)
        self.assertIn('state["status"] = "running"', heal_block)
        self.assertIn('state["completed_at"] = None', heal_block)
        self.assertIn("return True", heal_block)

    def test_unfinished_with_non_terminal_previous_status_still_returns_false(self):
        # A batch legitimately mid-run (status already "running") with
        # unfinished folders must not be treated as newly "changed" every
        # single recompute -- only the terminal-status contradiction case
        # should trigger the heal-and-return-True path.
        heal_block = self.fn[self.fn.index("if unfinished > 0:"):self.fn.index("if total <= 0 or processed < total:")]
        self.assertIn("return False", heal_block)


class RecoverRouteConcurrencyGuardTests(unittest.TestCase):
    def setUp(self):
        self.fn = _function_source(
            APP_SOURCE,
            "def ai_batch_import_recover():",
            "\n\ndef _prefer_album_mb_release(",
        )

    def test_worker_alive_check_precedes_starting_a_new_worker(self):
        # The bug: nothing stopped a second recover/retry call from
        # spawning a second concurrent worker for the same batch_job_id
        # while the first was still running.
        self.assertIn('if state.get("worker_alive"):', self.fn)
        guard_pos = self.fn.index('if state.get("worker_alive"):')
        start_pos = self.fn.index("_start_ai_batch_job(")
        self.assertLess(guard_pos, start_pos)

    def test_worker_alive_guard_reconnects_instead_of_erroring(self):
        guard_block = self.fn[self.fn.index('if state.get("worker_alive"):'):]
        guard_block = guard_block[:guard_block.index("\n\n") if "\n\n" in guard_block else len(guard_block)]
        self.assertIn('"reconnected": True', guard_block)

    def test_guard_runs_after_reconcile_so_worker_alive_is_fresh(self):
        reconcile_pos = self.fn.index("state = _ai_batch_reconcile_state(state)")
        guard_pos = self.fn.index('if state.get("worker_alive"):')
        self.assertLess(reconcile_pos, guard_pos)


class BoundedFolderRetryTests(unittest.TestCase):
    def setUp(self):
        self.fn = _function_source(
            APP_SOURCE,
            "def _run_ai_batch_import(",
            "\n\ndef _start_ai_batch_job(",
        )

    def test_max_folder_retries_constant_exists(self):
        self.assertIn("_AI_BATCH_MAX_FOLDER_RETRIES = 3", APP_SOURCE)

    def test_retry_branch_checks_limit_before_requeuing(self):
        retry_branch = self.fn[
            self.fn.index("if retry_failed and status in _AI_BATCH_RETRYABLE_FOLDER_STATUSES:"):
            self.fn.index('if status in _AI_BATCH_UNFINISHED_FOLDER_STATUSES and _pending_review_path_key')
        ]
        self.assertIn("prior_retries = int(folder.get(\"retry_count\") or 0)", retry_branch)
        self.assertIn("if prior_retries >= _AI_BATCH_MAX_FOLDER_RETRIES:", retry_branch)
        limit_pos = retry_branch.index("if prior_retries >= _AI_BATCH_MAX_FOLDER_RETRIES:")
        requeue_pos = retry_branch.index('"status": "ai_queued",')
        self.assertLess(limit_pos, requeue_pos)

    def test_exhausted_folder_marked_distinctly_not_silently_dropped(self):
        # Preferred minimal approach: keep the existing frontend-recognized
        # status (e.g. "failed") rather than introduce a new status value
        # the UI doesn't handle -- distinguish exhaustion via metadata only.
        retry_branch = self.fn[
            self.fn.index("if retry_failed and status in _AI_BATCH_RETRYABLE_FOLDER_STATUSES:"):
            self.fn.index('if status in _AI_BATCH_UNFINISHED_FOLDER_STATUSES and _pending_review_path_key')
        ]
        self.assertIn("retry_exhausted=True", retry_branch)
        self.assertIn("max_retries=_AI_BATCH_MAX_FOLDER_RETRIES", retry_branch)
        self.assertIn("manual_review_required=True", retry_branch)
        self.assertIn("needs manual review", retry_branch)
        self.assertNotIn('status="retry_limit_exceeded"', retry_branch)

    def test_failed_folder_statuses_unchanged_by_exhaustion_metadata(self):
        # No new status value was introduced -- exhaustion is metadata-only.
        self.assertIn(
            '_AI_BATCH_FAILED_FOLDER_STATUSES = {"failed", "import_failed", "ai_failed", "timed_out"}',
            APP_SOURCE,
        )
        self.assertNotIn("retry_limit_exceeded", APP_SOURCE)

    def test_folders_retryable_excludes_exhausted_folders(self):
        fn = _function_source(
            APP_SOURCE,
            "def _ai_batch_recompute_counts(state: Dict[str, Any]) -> None:",
            "\n\ndef _ai_batch_terminal_summary(",
        )
        retryable_block = fn[fn.index('state["folders_retryable"] ='):]
        retryable_block = retryable_block[:retryable_block.index("state[\"folders_attention\"]")]
        self.assertIn("not folder.get(\"retry_exhausted\")", retryable_block)
        # Must no longer be a plain status_counts lookup -- status alone
        # can't distinguish an exhausted folder from a fresh failure since
        # both keep the same status string.
        self.assertNotIn("status_counts.get(status, 0) for status in _AI_BATCH_RETRYABLE_FOLDER_STATUSES", retryable_block)


class RetryExhaustionFrontendVisibilityTests(unittest.TestCase):
    # This repo has no JS test runner configured (package.json has no
    # "test" script; frontend behavior is covered by Python tests reading
    # .tsx source, matching the rest of this test suite's convention) --
    # this is that focused frontend test for retry-exhaustion visibility.
    def test_api_type_declares_exhaustion_fields(self):
        block = API_TYPES_SOURCE[
            API_TYPES_SOURCE.index("export interface AiBatchFolderState {"):
            API_TYPES_SOURCE.index("export interface AiBatchState {")
        ]
        self.assertIn("retry_count?: number;", block)
        self.assertIn("retry_exhausted?: boolean;", block)
        self.assertIn("max_retries?: number;", block)
        self.assertIn("manual_review_required?: boolean;", block)

    def test_attention_list_shows_exhaustion_reason_before_raw_failure_text(self):
        # The existing attention-list rendering showed only
        # failure_reason/ai_suggest_error/current_step -- an exhausted
        # folder's failure_reason is left unchanged by design (the original
        # error is still useful evidence), so without a distinct line an
        # operator can't tell exhaustion apart from an ordinary failure.
        panel = _function_source(
            INTAKE_PANEL_SOURCE,
            "{attentionFolders.length ? (",
            "</div>\n            ) : null}\n          </div>\n        ) : (",
        )
        self.assertIn("folder.retry_exhausted", panel)
        self.assertIn("Retry limit reached", panel)
        self.assertIn("manual review required", panel)
        exhaustion_pos = panel.index("folder.retry_exhausted")
        failure_reason_pos = panel.index("folder.failure_reason")
        self.assertLess(exhaustion_pos, failure_reason_pos)

    def test_manual_review_chip_shown_for_exhausted_folders(self):
        panel = _function_source(
            INTAKE_PANEL_SOURCE,
            "{attentionFolders.length ? (",
            "</div>\n            ) : null}\n          </div>\n        ) : (",
        )
        self.assertIn("folder.retry_exhausted || folder.manual_review_required", panel)
        self.assertIn('label="Manual review"', panel)


# ---------------------------------------------------------------------------
# Behavioral tests: import the real app.py in an isolated environment and
# execute its actual functions, rather than only asserting source shape.
# Source-text tests above give limited structural protection; they cannot
# prove concurrency, persistence, or reconciliation behavior by themselves.
# ---------------------------------------------------------------------------
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest.mock as mock

_BEHAVIORAL_TMP_ROOT = Path(tempfile.mkdtemp(prefix="beets_retry_race_behavioral_"))


def _import_app_for_behavioral_tests():
    """Import the real app.py once, pointed at an isolated temp directory.

    Must not touch the real music library or make network calls. app.py has
    module-level side effects (opens a Beets Library at LIB_PATH, starts an
    _auto_scan_loop background thread, starts a local-only _install_ytdlp
    probe thread) -- all env vars below are set before import specifically
    so those land in a throwaway temp directory instead of any real path.
    """
    config_dir = _BEHAVIORAL_TMP_ROOT / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    os.environ["BEETSDIR"] = str(config_dir)
    os.environ["LIB_PATH"] = str(config_dir / "musiclibrary.blb")
    os.environ["AI_BATCH_STATE_DIR"] = str(_BEHAVIORAL_TMP_ROOT / "ai_batch_jobs")
    os.environ["METADATA_CACHE_DIR"] = str(_BEHAVIORAL_TMP_ROOT / "cache")
    os.environ["BEETS_TRANSACTION_DIR"] = str(_BEHAVIORAL_TMP_ROOT / "transactions")
    os.environ["BEETS_WEB_AUTH_DISABLED"] = "1"
    sys.path.insert(0, str(ROOT))
    import app as app_module
    return app_module


try:
    APP = _import_app_for_behavioral_tests()
    _APP_IMPORT_ERROR = None
except Exception as _exc:  # pragma: no cover - environment-dependent
    APP = None
    _APP_IMPORT_ERROR = _exc


def _behavioral_scan_path() -> str:
    path = _BEHAVIORAL_TMP_ROOT / "scan_source"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _make_folder(batch_job_id: str, name: str, *, status: str, retry_count: int = 0, **extra) -> dict:
    folder = APP._ai_batch_folder_state(batch_job_id, f"{_behavioral_scan_path()}/{name}")
    folder.update({"status": status, "retry_count": retry_count})
    folder.update(extra)
    return folder


def _write_batch_state(batch_job_id: str, folders: dict, *, status: str = "completed_with_warnings") -> dict:
    state = APP._ai_batch_initial_state(batch_job_id, _behavioral_scan_path())
    state["status"] = status
    state["folder_states"] = folders
    APP._ai_batch_recompute_counts(state)
    APP._ai_batch_write_state(state)
    return state


@unittest.skipIf(APP is None, f"app.py could not be imported for behavioral tests: {_APP_IMPORT_ERROR}")
class BehavioralTestCase(unittest.TestCase):
    """Shared setup: isolate module-level mutable state between tests."""

    def setUp(self):
        self.batch_job_id = f"behavioral-{uuid_hex()}"
        with APP._ai_batch_start_lock:
            APP._ai_batch_active_starts.clear()

    def tearDown(self):
        with APP._ai_batch_start_lock:
            APP._ai_batch_active_starts.pop(self.batch_job_id, None)
        state_file = APP._ai_batch_state_file(self.batch_job_id)
        if state_file.exists():
            state_file.unlink()


def uuid_hex() -> str:
    import uuid as _uuid
    return _uuid.uuid4().hex[:12]


class SimultaneousRecoverStartsExactlyOneWorkerTests(BehavioralTestCase):
    """Required behavioral test: simultaneous recover/retry."""

    def test_concurrent_recover_requests_start_exactly_one_worker(self):
        folders = {
            "f1": _make_folder(self.batch_job_id, "f1", status="failed", failure_reason="boom"),
            "f2": _make_folder(self.batch_job_id, "f2", status="timed_out"),
        }
        _write_batch_state(self.batch_job_id, folders)

        start_calls = []
        real_start_python = APP.jobs.start_python

        def counting_start_python(fn, label="", metadata=None):
            start_calls.append(metadata)
            return real_start_python(fn, label=label, metadata=metadata)

        # _run_ai_batch_import would otherwise reach real AI-suggestion /
        # MusicBrainz work for the newly-requeued folders -- irrelevant to
        # what this test measures (worker start count) and would make real
        # network calls. Replaced with a fast, recording stub.
        run_calls = []

        def fake_run_ai_batch_import(batch_job_id, scan_path, log, cancel_event=None, update_state=None, **kwargs):
            run_calls.append(batch_job_id)
            return {"status": "completed_with_warnings"}

        barrier = threading.Barrier(6)
        results = []
        results_lock = threading.Lock()

        def call_recover():
            barrier.wait(timeout=5)
            with APP.app.test_client() as client:
                resp = client.post(
                    "/api/ai-batch-import/recover",
                    json={"batch_job_id": self.batch_job_id, "retry_failed": True},
                )
            with results_lock:
                results.append(resp.get_json())

        with mock.patch.object(APP.jobs, "start_python", side_effect=counting_start_python), \
             mock.patch.object(APP, "_run_ai_batch_import", side_effect=fake_run_ai_batch_import):
            threads = [threading.Thread(target=call_recover) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        self.assertEqual(len(results), 6, "not all concurrent requests completed")
        for r in results:
            self.assertTrue(r.get("ok"))
        self.assertEqual(
            len(start_calls), 1,
            f"jobs.start_python() must be called exactly once for concurrent recover requests, got {len(start_calls)}",
        )
        # Every response must reference the one real job/batch identity.
        job_ids = {r.get("job_id") for r in results}
        self.assertEqual(len(job_ids), 1, f"responses disagree on job_id: {job_ids}")
        # The loser responses must say so.
        reconnected_count = sum(1 for r in results if r.get("reconnected"))
        self.assertEqual(reconnected_count, 5, "exactly 5 of 6 requests should reconnect to the existing worker")
        # Reservation must not be left stuck after the winner finishes.
        deadline = time.time() + 5
        while self.batch_job_id in APP._ai_batch_active_starts and time.time() < deadline:
            time.sleep(0.05)
        with APP._ai_batch_start_lock:
            self.assertNotIn(self.batch_job_id, APP._ai_batch_active_starts)


class StartupFailureReleasesReservationTests(BehavioralTestCase):
    """Required behavioral test: startup failure cleanup."""

    def test_start_python_raising_releases_the_reservation(self):
        folders = {"f1": _make_folder(self.batch_job_id, "f1", status="failed")}
        _write_batch_state(self.batch_job_id, folders)

        def raising_start_python(fn, label="", metadata=None):
            raise RuntimeError("simulated startup failure")

        with mock.patch.object(APP.jobs, "start_python", side_effect=raising_start_python):
            with self.assertRaises(RuntimeError):
                APP._start_ai_batch_job(_behavioral_scan_path(), recover_batch_job_id=self.batch_job_id, retry_failed=True)

        with APP._ai_batch_start_lock:
            self.assertNotIn(
                self.batch_job_id, APP._ai_batch_active_starts,
                "a failed jobs.start_python() call must not leave the batch permanently reserved",
            )

        # A later legitimate call must be able to proceed normally.
        run_calls = []

        def fake_run_ai_batch_import(batch_job_id, scan_path, log, cancel_event=None, update_state=None, **kwargs):
            run_calls.append(batch_job_id)
            return {"status": "completed_with_warnings"}

        with mock.patch.object(APP, "_run_ai_batch_import", side_effect=fake_run_ai_batch_import):
            with APP.app.test_request_context():
                resp = APP._start_ai_batch_job(_behavioral_scan_path(), recover_batch_job_id=self.batch_job_id, retry_failed=True)
        payload = json.loads(resp.get_data(as_text=True))
        self.assertTrue(payload.get("ok"))
        self.assertNotIn(
            "reconnected", payload,
            "the recovered call should start a real worker, not report a stale reservation",
        )


class RetryReconciliationRegressionTests(BehavioralTestCase):
    """Required behavioral tests: premature finalization + stale-writer
    prevention, exercised together as an end-to-end regression test for the
    original live bug (retry logged "N requeued" immediately followed by
    "batch is terminal", with folders reverted rather than reprocessed)."""

    def setUp(self):
        super().setUp()
        self._suggestions_patcher = mock.patch.object(
            APP, "_ai_batch_run_suggestions", return_value="done",
        )
        self._decisions_patcher = mock.patch.object(
            APP, "_ai_batch_process_decisions", return_value={},
        )
        self._suggestions_patcher.start()
        self._decisions_patcher.start()
        self.addCleanup(self._suggestions_patcher.stop)
        self.addCleanup(self._decisions_patcher.stop)

    def test_retry_failed_reconciliation_requeues_and_does_not_report_terminal(self):
        folders = {
            "f1": _make_folder(self.batch_job_id, "f1", status="failed", retry_count=0, failure_reason="boom"),
            "f2": _make_folder(self.batch_job_id, "f2", status="timed_out", retry_count=1),
            "f3": _make_folder(self.batch_job_id, "f3", status="review_created"),  # already terminal, not retryable
        }
        _write_batch_state(self.batch_job_id, folders)

        result = APP._run_ai_batch_import(
            self.batch_job_id, _behavioral_scan_path(), log=[],
            recover=True, retry_failed=True, job_id="test-job",
        )

        self.assertNotEqual(
            result.get("status"), "completed_with_warnings",
            "batch must not immediately report itself terminal when retryable folders exist",
        )
        self.assertNotIn(result.get("status"), APP._AI_BATCH_TERMINAL_STATUSES)
        self.assertGreater(result.get("folders_unfinished", 0), 0)

        persisted = APP._ai_batch_load_state(self.batch_job_id)
        pf1 = persisted["folder_states"]["f1"]
        pf2 = persisted["folder_states"]["f2"]
        self.assertEqual(pf1["status"], "ai_queued")
        self.assertEqual(pf1["retry_count"], 1)
        self.assertEqual(pf2["status"], "ai_queued")
        self.assertEqual(pf2["retry_count"], 2)
        # The already-terminal, non-retryable folder must be untouched.
        self.assertEqual(persisted["folder_states"]["f3"]["status"], "review_created")

    def test_suggestion_processing_is_reached_after_reconciliation(self):
        folders = {"f1": _make_folder(self.batch_job_id, "f1", status="failed")}
        _write_batch_state(self.batch_job_id, folders)

        APP._run_ai_batch_import(
            self.batch_job_id, _behavioral_scan_path(), log=[],
            recover=True, retry_failed=True, job_id="test-job",
        )

        self.assertTrue(APP._ai_batch_run_suggestions.called)
        self.assertTrue(APP._ai_batch_process_decisions.called)


class ContradictoryTerminalStateHealingTests(BehavioralTestCase):
    def test_terminal_status_with_unfinished_folder_reopens(self):
        folders = {"f1": _make_folder(self.batch_job_id, "f1", status="ai_queued")}
        state = _write_batch_state(self.batch_job_id, folders, status="completed_with_warnings")

        changed = APP._ai_batch_recalculate_batch_state(state)

        self.assertTrue(changed)
        self.assertEqual(state["status"], "running")
        self.assertIsNone(state["completed_at"])

    def test_canceled_status_does_not_reopen(self):
        folders = {"f1": _make_folder(self.batch_job_id, "f1", status="ai_queued")}
        state = _write_batch_state(self.batch_job_id, folders, status="canceled")

        changed = APP._ai_batch_recalculate_batch_state(state)

        self.assertFalse(changed)
        self.assertEqual(state["status"], "canceled")

    def test_genuinely_completed_batch_does_not_reopen_and_no_folder_is_requeued(self):
        folders = {
            "f1": _make_folder(self.batch_job_id, "f1", status="review_created"),
            "f2": _make_folder(self.batch_job_id, "f2", status="imported"),
        }
        state = _write_batch_state(self.batch_job_id, folders, status="running")

        changed = APP._ai_batch_recalculate_batch_state(state)

        self.assertTrue(changed)
        self.assertIn(state["status"], APP._AI_BATCH_TERMINAL_STATUSES)
        self.assertEqual(state["folder_states"]["f1"]["status"], "review_created")
        self.assertEqual(state["folder_states"]["f2"]["status"], "imported")


class RetryExhaustionStateTransitionTests(BehavioralTestCase):
    def setUp(self):
        super().setUp()
        self._suggestions_patcher = mock.patch.object(APP, "_ai_batch_run_suggestions", return_value="done")
        self._decisions_patcher = mock.patch.object(APP, "_ai_batch_process_decisions", return_value={})
        self._suggestions_patcher.start()
        self._decisions_patcher.start()
        self.addCleanup(self._suggestions_patcher.stop)
        self.addCleanup(self._decisions_patcher.stop)

    def _retry_once(self):
        return APP._run_ai_batch_import(
            self.batch_job_id, _behavioral_scan_path(), log=[],
            recover=True, retry_failed=True, job_id="test-job",
        )

    def test_retry_counts_zero_one_two_can_retry_three_cannot(self):
        # The original attempt that produced the first failure is not
        # counted as a retry; retry_count tracks *retry* attempts only, so
        # retry_count values 0/1/2 (three prior retries at most) may retry
        # again, and retry_count 3 (== _AI_BATCH_MAX_FOLDER_RETRIES) may not.
        folders = {
            "f0": _make_folder(self.batch_job_id, "f0", status="failed", retry_count=0),
            "f1": _make_folder(self.batch_job_id, "f1", status="failed", retry_count=1),
            "f2": _make_folder(self.batch_job_id, "f2", status="failed", retry_count=2),
            "f3": _make_folder(self.batch_job_id, "f3", status="failed", retry_count=3),
        }
        _write_batch_state(self.batch_job_id, folders)

        self._retry_once()

        persisted = APP._ai_batch_load_state(self.batch_job_id)
        pf = persisted["folder_states"]
        self.assertEqual(pf["f0"]["status"], "ai_queued")
        self.assertEqual(pf["f0"]["retry_count"], 1)
        self.assertEqual(pf["f1"]["status"], "ai_queued")
        self.assertEqual(pf["f1"]["retry_count"], 2)
        self.assertEqual(pf["f2"]["status"], "ai_queued")
        self.assertEqual(pf["f2"]["retry_count"], 3)
        # f3 was already at the cap: must not be requeued.
        self.assertEqual(pf["f3"]["status"], "failed")
        self.assertEqual(pf["f3"]["retry_count"], 3)
        self.assertTrue(pf["f3"].get("retry_exhausted"))
        self.assertTrue(pf["f3"].get("manual_review_required"))
        self.assertEqual(pf["f3"].get("max_retries"), APP._AI_BATCH_MAX_FOLDER_RETRIES)

    def test_exhausted_folder_remains_visible_in_attention_and_correct_counts(self):
        folders = {
            "f_ok": _make_folder(self.batch_job_id, "f_ok", status="failed", retry_count=0),
            "f_exhausted": _make_folder(self.batch_job_id, "f_exhausted", status="failed", retry_count=3),
        }
        _write_batch_state(self.batch_job_id, folders)

        self._retry_once()

        persisted = APP._ai_batch_load_state(self.batch_job_id)
        # Public counts must reflect exactly one still-retryable folder --
        # the exhausted one must not inflate folders_retryable.
        self.assertEqual(persisted["folders_retryable"], 0)  # f_ok just got requeued to ai_queued
        self.assertEqual(persisted["folders_failed"], 1)  # f_exhausted keeps its "failed" status
        self.assertGreater(persisted["folders_attention"], 0)

    def test_a_later_retry_does_not_silently_requeue_an_exhausted_folder(self):
        folders = {"f3": _make_folder(self.batch_job_id, "f3", status="failed", retry_count=3)}
        _write_batch_state(self.batch_job_id, folders)

        self._retry_once()
        first = APP._ai_batch_load_state(self.batch_job_id)["folder_states"]["f3"]
        self._retry_once()
        second = APP._ai_batch_load_state(self.batch_job_id)["folder_states"]["f3"]

        self.assertEqual(first["status"], "failed")
        self.assertEqual(second["status"], "failed")
        self.assertEqual(second["retry_count"], 3, "retry_count must not keep climbing past the cap")


if __name__ == "__main__":
    unittest.main()
