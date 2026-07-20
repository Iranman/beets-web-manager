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
        self.reconnect_fn = _function_source(
            APP_SOURCE,
            "def _ai_batch_reconnect_response(batch_job_id: str):",
            "\n\ndef _start_ai_batch_job(",
        )
        self.fn = _function_source(
            APP_SOURCE,
            "def _start_ai_batch_job(scan_path: str, recover_batch_job_id: str = \"\", *, retry_failed: bool = False):",
            "\n\n@app.post(\"/api/ai-batch-skip\")",
        )

    def test_reservation_guards_the_whole_start_sequence(self):
        # The registry reservation must span the whole start sequence, and
        # -- unlike the earlier startup-only reservation -- must NOT be
        # unconditionally released in the finally block: only a startup
        # failure (handed_off still False) may release it here, since a
        # successful start hands ownership to the worker thread's own
        # finally (see test_successful_handoff_transfers_release_ownership).
        self.assertIn("if not _ai_batch_reserve_worker(batch_job_id):", self.fn)
        self.assertIn("return _ai_batch_reconnect_response(batch_job_id)", self.fn)
        reserve_pos = self.fn.index("if not _ai_batch_reserve_worker(batch_job_id):")
        try_pos = self.fn.index("try:")
        finally_pos = self.fn.index("finally:")
        release_pos = self.fn.index('_ai_batch_release_worker(batch_job_id, expected=None)')
        self.assertLess(reserve_pos, try_pos)
        self.assertLess(try_pos, finally_pos)
        self.assertLess(finally_pos, release_pos)

    def test_successful_handoff_transfers_release_ownership(self):
        # The bug this replaces: the old reservation was released as soon as
        # _start_ai_batch_job() itself returned, well before the worker it
        # started had committed anything -- leaving a window where a second
        # caller could reserve and start a duplicate worker. The fix must
        # only release-on-exit when startup failed (handed_off is False);
        # once promotion succeeds, the worker wrapper's own finally is the
        # sole owner of the eventual release.
        self.assertIn("handed_off = False", self.fn)
        self.assertIn("handed_off = True", self.fn)
        finally_block = self.fn[self.fn.index("\n    finally:"):]
        self.assertIn("if not handed_off:", finally_block)
        promote_pos = self.fn.index("if not _ai_batch_promote_worker(batch_job_id, job.job_id):")
        handed_off_true_pos = self.fn.index("handed_off = True")
        set_pos = self.fn.index("job_id_ready.set()")
        self.assertLess(promote_pos, handed_off_true_pos)
        self.assertLess(handed_off_true_pos, set_pos)

    def test_worker_wrapper_releases_ownership_safely_in_finally(self):
        do_block = self.fn[self.fn.index("def _do("):self.fn.index("job = jobs.start_python(")]
        self.assertIn("owned_job_id = job_id_holder.get(\"job_id\", \"\")", do_block)
        try_pos = do_block.index("try:")
        finally_pos = do_block.index("finally:")
        release_pos = do_block.index("_ai_batch_release_worker(batch_job_id, expected=owned_job_id)")
        self.assertLess(try_pos, finally_pos)
        self.assertLess(finally_pos, release_pos)

    def test_reconnect_response_never_calls_start_python(self):
        self.assertNotIn("start_python(", self.reconnect_fn)
        self.assertIn("_ai_batch_active_worker_job_id(batch_job_id)", self.reconnect_fn)
        self.assertIn('"reconnected": True,', self.reconnect_fn)

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

    def test_worker_thread_is_unblocked_after_job_association_and_promotion(self):
        # The worker must not be unblocked (and allowed to write batch
        # state) until the job_id association is durably persisted AND the
        # registry has been promoted to the real job_id, or an immediate
        # status poll by job_id -- or a concurrent reconnect -- could miss
        # the batch or observe an unpromoted reservation for too long.
        branch_pos = self.fn.index("if recover_batch_job_id:")
        promote_pos = self.fn.index("if not _ai_batch_promote_worker(batch_job_id, job.job_id):")
        set_pos = self.fn.index("job_id_ready.set()")
        self.assertLess(branch_pos, promote_pos)
        self.assertLess(promote_pos, set_pos)


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


class ReconcileStateRegistryPrecedenceTests(unittest.TestCase):
    """_ai_batch_reconcile_state must defer entirely to the active-worker
    registry instead of recalculating/finalizing against a possibly
    pre-reconciliation folder_states snapshot -- see
    _ai_batch_active_workers' module docstring for the bug this closes."""

    def setUp(self):
        self.fn = _function_source(
            APP_SOURCE,
            "def _ai_batch_reconcile_state(state: Dict[str, Any]) -> Dict[str, Any]:",
            "\n\ndef _ai_batch_queue_pending_review(",
        )

    def test_worker_alive_is_true_when_registry_reports_active(self):
        self.assertIn("registry_active = _ai_batch_worker_registered(batch_job_id)", self.fn)
        self.assertIn("worker_alive = bool(job and job.status == \"running\") or registry_active", self.fn)

    def test_registry_active_short_circuits_before_any_recalculation(self):
        registry_pos = self.fn.index("if registry_active:")
        return_pos = self.fn.index("return state", registry_pos)
        recalc_pos = self.fn.index("changed = _ai_batch_recalculate_batch_state(state)")
        self.assertLess(registry_pos, return_pos)
        self.assertLess(return_pos, recalc_pos)


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
#
# Isolation: app.py is a large module with import-time side effects (opens a
# Beets Library, starts background threads), so it can only safely be
# imported once per process, pointed at a throwaway temp directory via env
# vars set before that one import. Both the temp directory and the env
# overrides are registered with unittest.addModuleCleanup so `python -m
# unittest discover` (which runs many other test modules in the same
# process) is not left with mutated environment variables or an undeleted
# temp tree once this module's tests finish.
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
unittest.addModuleCleanup(shutil.rmtree, str(_BEHAVIORAL_TMP_ROOT), ignore_errors=True)

_BEHAVIORAL_ENV_OVERRIDES = {
    "BEETSDIR": str(_BEHAVIORAL_TMP_ROOT / "config"),
    "LIB_PATH": str(_BEHAVIORAL_TMP_ROOT / "config" / "musiclibrary.blb"),
    "AI_BATCH_STATE_DIR": str(_BEHAVIORAL_TMP_ROOT / "ai_batch_jobs"),
    "METADATA_CACHE_DIR": str(_BEHAVIORAL_TMP_ROOT / "cache"),
    "BEETS_TRANSACTION_DIR": str(_BEHAVIORAL_TMP_ROOT / "transactions"),
    "BEETS_WEB_AUTH_DISABLED": "1",
}
(_BEHAVIORAL_TMP_ROOT / "config").mkdir(parents=True, exist_ok=True)
_behavioral_env_patcher = mock.patch.dict(os.environ, _BEHAVIORAL_ENV_OVERRIDES, clear=False)
_behavioral_env_patcher.start()
unittest.addModuleCleanup(_behavioral_env_patcher.stop)


def _import_app_for_behavioral_tests():
    """Import the real app.py once, with the env overrides above already in
    effect, so its module-level side effects (Beets Library, background
    threads) land in the throwaway temp directory instead of any real path.
    Must not touch the real music library or make network calls."""
    sys.path.insert(0, str(ROOT))
    import app as app_module
    return app_module


def setUpModule():
    """mock.patch.dict's .start() above only merges the overrides into
    os.environ once, at collection time (before any test in any module has
    run). Under `python -m unittest discover`, other test modules that run
    before this one (alphabetically) may mutate shared env vars like
    BEETS_WEB_AUTH_DISABLED / BEETS_WEB_AUTH_TOKEN / BEETS_WEB_PASSWORD
    without reverting them, silently overwriting our collection-time value
    by the time this module's tests actually execute -- observed as
    intermittent "Authentication is required" 503s from
    _enforce_security_boundary purely as a function of full-suite run
    order. Re-assert right before this module's tests run (unittest calls
    setUpModule automatically at that point) so the override always wins
    for our own tests, regardless of what ran earlier."""
    os.environ.update(_BEHAVIORAL_ENV_OVERRIDES)


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


def uuid_hex() -> str:
    import uuid as _uuid
    return _uuid.uuid4().hex[:12]


def _wait_until(predicate, timeout=5.0, interval=0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@unittest.skipIf(APP is None, f"app.py could not be imported for behavioral tests: {_APP_IMPORT_ERROR}")
class BehavioralTestCase(unittest.TestCase):
    """Shared setup: isolate module-level mutable state between tests, and
    make sure nothing a test starts (registry entries, JobStore jobs,
    control dicts, worker threads) survives into the next test."""

    def setUp(self):
        self.batch_job_id = f"behavioral-{uuid_hex()}"
        self._job_ids: list = []
        with APP._ai_batch_worker_lock:
            APP._ai_batch_active_workers.pop(self.batch_job_id, None)

    def track_job(self, job_id: str) -> None:
        """Register a real JobStore job this test started, so tearDown can
        wait for its worker thread to actually finish before removing it --
        the closest available proxy to "no test-created worker thread
        remains alive" without JobStore exposing raw Thread handles."""
        if job_id:
            self._job_ids.append(job_id)

    def tearDown(self):
        for job_id in self._job_ids:
            job = APP.jobs.get(job_id)
            if job is not None:
                _wait_until(lambda j=job: j.status != "running", timeout=10)
                self.assertNotEqual(
                    job.status, "running",
                    f"job {job_id} still running at test teardown -- worker thread leaked",
                )
            with APP.jobs._lock:
                APP.jobs._jobs.pop(job_id, None)

        _wait_until(lambda: not APP._ai_batch_worker_registered(self.batch_job_id), timeout=5)
        with APP._ai_batch_worker_lock:
            APP._ai_batch_active_workers.pop(self.batch_job_id, None)
        with APP._ai_batch_control_lock:
            APP._ai_batch_controls.pop(self.batch_job_id, None)

        state_file = APP._ai_batch_state_file(self.batch_job_id)
        if state_file.exists():
            state_file.unlink()


class SimultaneousRecoverStartsExactlyOneWorkerTests(BehavioralTestCase):
    """Required behavioral test: simultaneous recover/retry (also covers the
    "startup reservation collision" case -- many concurrent callers racing
    _ai_batch_reserve_worker while the batch has no worker registered yet
    -- since the barrier below releases all 6 requests at the same instant)."""

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

            self.assertEqual(len(results), 6, f"not all concurrent requests completed: {results}")
            for r in results:
                self.assertTrue(r.get("ok"), f"request failed: {r}")
            # Identify the winner directly by response shape (the one
            # response without "reconnected") rather than by set-uniqueness
            # over job_id -- a timed-out reconnect poll legitimately falls
            # back to reporting the bare batch_job_id as "job_id", so
            # set-uniqueness alone can't reliably locate the real JobStore key.
            winners = [r for r in results if not r.get("reconnected")]
            self.assertEqual(len(winners), 1, f"expected exactly one non-reconnected winner response: {results}")
            winner_job_id = winners[0].get("job_id")
            self.assertTrue(winner_job_id)
            self.track_job(winner_job_id)
            # Every response must reference the one real job/batch identity.
            job_ids = {r.get("job_id") for r in results}
            self.assertEqual(len(job_ids), 1, f"responses disagree on job_id: {job_ids}")
            # The winner's worker thread call into the mocked
            # _run_ai_batch_import happens asynchronously; wait for it while
            # the patch is still active (see WorkerCompletionCleanupTests).
            winner_job = APP.jobs.get(winner_job_id)
            self.assertIsNotNone(winner_job, f"winner job_id {winner_job_id} not found in JobStore")
            self.assertTrue(
                _wait_until(lambda: winner_job.status != "running", timeout=10),
                "winner worker job never finished",
            )

        self.assertEqual(
            len(start_calls), 1,
            f"jobs.start_python() must be called exactly once for concurrent recover requests, got {len(start_calls)}",
        )
        # The loser responses must say so.
        reconnected_count = sum(1 for r in results if r.get("reconnected"))
        self.assertEqual(reconnected_count, 5, "exactly 5 of 6 requests should reconnect to the existing worker")
        # Registry entry must not be left stuck after the winner finishes.
        _wait_until(lambda: self.batch_job_id not in APP._ai_batch_active_workers, timeout=5)
        with APP._ai_batch_worker_lock:
            self.assertNotIn(self.batch_job_id, APP._ai_batch_active_workers)


class StartupReservationCollisionDuringUnpromotedWindowTests(BehavioralTestCase):
    """Required test: startup reservation collision. Precisely targets the
    window where the registry entry is reserved (None) but not yet promoted
    to a real job_id -- a second caller arriving in exactly that window must
    reconnect, not start a second worker."""

    def test_second_caller_during_reserved_unpromoted_window_reconnects(self):
        folders = {"f1": _make_folder(self.batch_job_id, "f1", status="failed")}
        _write_batch_state(self.batch_job_id, folders)

        entered_start_python = threading.Event()
        proceed = threading.Event()
        start_calls = []
        real_start_python = APP.jobs.start_python

        def blocking_start_python(fn, label="", metadata=None):
            start_calls.append(metadata)
            entered_start_python.set()
            proceed.wait(timeout=10)
            return real_start_python(fn, label=label, metadata=metadata)

        def fake_run_ai_batch_import(batch_job_id, scan_path, log, cancel_event=None, update_state=None, **kwargs):
            return {"status": "completed_with_warnings"}

        first_holder = {}

        def call_first():
            with APP.app.test_request_context():
                first_holder["response"] = APP._start_ai_batch_job(
                    _behavioral_scan_path(), recover_batch_job_id=self.batch_job_id, retry_failed=True,
                )

        with mock.patch.object(APP.jobs, "start_python", side_effect=blocking_start_python), \
             mock.patch.object(APP, "_run_ai_batch_import", side_effect=fake_run_ai_batch_import):
            first_thread = threading.Thread(target=call_first)
            first_thread.start()
            self.assertTrue(entered_start_python.wait(timeout=5), "first call never reached jobs.start_python()")

            # The registry must show a startup reservation (None) here --
            # not yet promoted to a job_id, since jobs.start_python() itself
            # hasn't returned.
            with APP._ai_batch_worker_lock:
                self.assertIn(self.batch_job_id, APP._ai_batch_active_workers)
                self.assertIsNone(APP._ai_batch_active_workers[self.batch_job_id])

            # Let the first call's jobs.start_python() proceed shortly after
            # the second call starts its bounded poll (_ai_batch_reconnect_
            # response waits up to 2s), so the second call observes the real
            # promotion landing instead of exhausting its poll and falling
            # back to the bare batch_job_id.
            threading.Timer(0.3, proceed.set).start()

            with APP.app.test_request_context():
                second_response = APP._start_ai_batch_job(
                    _behavioral_scan_path(), recover_batch_job_id=self.batch_job_id, retry_failed=True,
                )
            second_payload = json.loads(second_response.get_data(as_text=True))

            first_thread.join(timeout=10)
            self.assertFalse(first_thread.is_alive(), "first _start_ai_batch_job call did not complete")

        first_payload = json.loads(first_holder["response"].get_data(as_text=True))
        self.track_job(first_payload.get("job_id"))

        self.assertEqual(
            len(start_calls), 1,
            "exactly one caller may reach jobs.start_python() while a startup reservation is held",
        )
        self.assertTrue(second_payload.get("ok"))
        self.assertTrue(second_payload.get("reconnected"), "second caller must reconnect during the reserved-unpromoted window")
        self.assertEqual(second_payload.get("job_id"), first_payload.get("job_id"))


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

        with APP._ai_batch_worker_lock:
            self.assertNotIn(
                self.batch_job_id, APP._ai_batch_active_workers,
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
            job_id = payload.get("job_id")
            self.track_job(job_id)
            # Must wait for the worker to actually reach (and use) the mock
            # while the patch is still active -- see WorkerCompletionCleanupTests.
            self.assertTrue(
                _wait_until(lambda: APP.jobs.get(job_id).status != "running", timeout=5),
                "worker job never finished",
            )
        self.assertTrue(payload.get("ok"))
        self.assertNotIn(
            "reconnected", payload,
            "the recovered call should start a real worker, not report a stale reservation",
        )


class PostStartPreHeartbeatDuplicateWorkerRegressionTests(BehavioralTestCase):
    """Required regression test: a second recover request arriving after the
    first _start_ai_batch_job() call has already returned, but before the
    worker it started has made its first reconciled commit/heartbeat, must
    reconnect to the existing worker instead of starting a second one.

    This fails against commit 67c7592: the startup-only reservation
    (_ai_batch_active_starts) was released as soon as _start_ai_batch_job()
    itself returned -- well before the worker's first commit -- and the
    second request's own worker_alive read was fed by the still
    pre-reconciliation, all-terminal-looking folder_states snapshot, which
    _ai_batch_recalculate_batch_state's finalize branch stamps back to
    worker_alive=False."""

    def test_second_recover_during_post_start_pre_heartbeat_window_reconnects(self):
        folders = {"f1": _make_folder(self.batch_job_id, "f1", status="failed", failure_reason="boom")}
        _write_batch_state(self.batch_job_id, folders)

        start_calls = []
        real_start_python = APP.jobs.start_python

        def counting_start_python(fn, label="", metadata=None):
            start_calls.append(metadata)
            return real_start_python(fn, label=label, metadata=metadata)

        worker_entered = threading.Event()
        release_worker = threading.Event()
        run_calls = []

        def blocking_run_ai_batch_import(batch_job_id, scan_path, log, cancel_event=None, update_state=None, **kwargs):
            # Simulates the real worker's window between being unblocked and
            # its own first _ai_batch_commit -- no state is written here, so
            # the persisted folder_states remains the pre-retry snapshot.
            run_calls.append(kwargs.get("job_id"))
            worker_entered.set()
            release_worker.wait(timeout=10)
            return {"status": "completed_with_warnings"}

        first_holder = {}

        def call_first():
            with APP.app.test_client() as client:
                resp = client.post(
                    "/api/ai-batch-import/recover",
                    json={"batch_job_id": self.batch_job_id, "retry_failed": True},
                )
            first_holder["response"] = resp.get_json()

        with mock.patch.object(APP.jobs, "start_python", side_effect=counting_start_python), \
             mock.patch.object(APP, "_run_ai_batch_import", side_effect=blocking_run_ai_batch_import):
            first_thread = threading.Thread(target=call_first)
            first_thread.start()
            first_thread.join(timeout=10)
            self.assertFalse(first_thread.is_alive(), "first recover request did not complete")

            first_response = first_holder.get("response")
            self.assertIsNotNone(first_response)
            self.assertTrue(first_response.get("ok"))
            self.assertNotIn("reconnected", first_response, "first request should start a real worker, not reconnect")
            first_job_id = first_response.get("job_id")
            self.assertTrue(first_job_id)
            self.track_job(first_job_id)

            # The first _start_ai_batch_job() call has now fully returned.
            # Confirm the worker it started is running but has not yet
            # committed anything (still blocked before its first commit).
            self.assertTrue(worker_entered.wait(timeout=5), "worker never reached _run_ai_batch_import")

            with APP.app.test_client() as client:
                second_resp = client.post(
                    "/api/ai-batch-import/recover",
                    json={"batch_job_id": self.batch_job_id, "retry_failed": True},
                )
            second_response = second_resp.get_json()

            self.assertEqual(
                len(start_calls), 1,
                f"jobs.start_python() must be called exactly once; got {len(start_calls)} (a second "
                "call means a duplicate worker was started in the post-start/pre-heartbeat window)",
            )
            self.assertTrue(second_response.get("ok"))
            self.assertTrue(second_response.get("reconnected"), "second request must reconnect, not start a new worker")
            self.assertEqual(second_response.get("job_id"), first_job_id)

            matching_jobs = [j for j in APP.jobs.all() if (j.metadata or {}).get("batch_job_id") == self.batch_job_id]
            self.assertEqual(len(matching_jobs), 1, "exactly one JobStore job must exist for this batch")

            self.assertEqual(
                APP._ai_batch_active_worker_job_id(self.batch_job_id), first_job_id,
                "the active-worker registry must still identify the first worker",
            )

            release_worker.set()

        self.assertTrue(
            _wait_until(lambda: not APP._ai_batch_worker_registered(self.batch_job_id), timeout=5),
            "registry entry must be released after the worker completes",
        )


class WorkerCompletionCleanupTests(BehavioralTestCase):
    """Required test: worker completion cleanup."""

    def test_registry_released_after_completion_and_later_recovery_can_start(self):
        folders = {"f1": _make_folder(self.batch_job_id, "f1", status="failed")}
        _write_batch_state(self.batch_job_id, folders)

        def fast_run(batch_job_id, scan_path, log, cancel_event=None, update_state=None, **kwargs):
            return {"status": "completed_with_warnings"}

        with mock.patch.object(APP, "_run_ai_batch_import", side_effect=fast_run):
            with APP.app.test_request_context():
                resp = APP._start_ai_batch_job(_behavioral_scan_path(), recover_batch_job_id=self.batch_job_id, retry_failed=True)
            payload = json.loads(resp.get_data(as_text=True))
            self.assertTrue(payload.get("ok"))
            self.assertNotIn("reconnected", payload)
            job_id = payload.get("job_id")
            self.track_job(job_id)

            # The worker thread's call into the mocked _run_ai_batch_import
            # happens asynchronously in a background thread; the mock patch
            # must still be active when that call actually happens, so wait
            # for the job to finish while still inside the `with` block --
            # otherwise this races the patch being torn down and the real
            # (slow, network-calling) function could run instead.
            self.assertTrue(
                _wait_until(lambda: APP.jobs.get(job_id).status != "running", timeout=5),
                "worker job never finished",
            )

        self.assertTrue(
            _wait_until(lambda: not APP._ai_batch_worker_registered(self.batch_job_id), timeout=5),
            "registry must be released once the worker completes",
        )

        # A legitimate future recovery must be able to reserve/start again.
        self.assertTrue(APP._ai_batch_reserve_worker(self.batch_job_id))
        APP._ai_batch_release_worker(self.batch_job_id, expected=None)


class WorkerExceptionCleanupTests(BehavioralTestCase):
    """Required test: worker exception cleanup."""

    def test_registry_released_and_job_marked_failed_when_run_raises(self):
        folders = {"f1": _make_folder(self.batch_job_id, "f1", status="failed")}
        _write_batch_state(self.batch_job_id, folders)

        def raising_run(batch_job_id, scan_path, log, cancel_event=None, update_state=None, **kwargs):
            raise RuntimeError("simulated worker failure")

        with mock.patch.object(APP, "_run_ai_batch_import", side_effect=raising_run):
            with APP.app.test_request_context():
                resp = APP._start_ai_batch_job(_behavioral_scan_path(), recover_batch_job_id=self.batch_job_id, retry_failed=True)
            payload = json.loads(resp.get_data(as_text=True))
            job_id = payload.get("job_id")
            self.assertTrue(job_id)
            self.track_job(job_id)
            job = APP.jobs.get(job_id)
            self.assertIsNotNone(job)
            # See WorkerCompletionCleanupTests: must wait for the (mocked,
            # raising) worker to actually finish while the patch is still
            # active, or this races the patch teardown and the real function
            # could run instead of raising.
            self.assertTrue(_wait_until(lambda: job.status != "running", timeout=5), "worker job never finished")

        self.assertTrue(
            _wait_until(lambda: not APP._ai_batch_worker_registered(self.batch_job_id), timeout=5),
            "registry must be released even when the worker raises",
        )
        self.assertEqual(job.status, "failed", "the job must record the failure normally, not hang as running")

        # A later valid recover is not permanently blocked.
        self.assertTrue(APP._ai_batch_reserve_worker(self.batch_job_id))
        APP._ai_batch_release_worker(self.batch_job_id, expected=None)


class OwnershipSafeReleaseTests(BehavioralTestCase):
    """Required test: ownership-safe release. A stale worker's cleanup call
    (an old/superseded ownership token) must never remove a different
    worker's current registration."""

    def test_stale_token_release_does_not_remove_a_newer_registration(self):
        self.assertTrue(APP._ai_batch_reserve_worker(self.batch_job_id))
        self.assertTrue(APP._ai_batch_promote_worker(self.batch_job_id, "old-job-id"))

        # Simulate the slot having since been reassigned to a different
        # job_id (e.g. by test setup mimicking a superseding legitimate
        # worker), then an old worker's delayed cleanup arriving late.
        with APP._ai_batch_worker_lock:
            APP._ai_batch_active_workers[self.batch_job_id] = "new-job-id"
        APP._ai_batch_release_worker(self.batch_job_id, expected="old-job-id")

        self.assertEqual(
            APP._ai_batch_active_worker_job_id(self.batch_job_id), "new-job-id",
            "a stale release must not remove a newer worker's registration",
        )
        # The real, current owner's release must still work normally.
        APP._ai_batch_release_worker(self.batch_job_id, expected="new-job-id")
        self.assertFalse(APP._ai_batch_worker_registered(self.batch_job_id))

    def test_none_expected_release_does_not_remove_an_already_promoted_entry(self):
        self.assertTrue(APP._ai_batch_reserve_worker(self.batch_job_id))
        self.assertTrue(APP._ai_batch_promote_worker(self.batch_job_id, "real-job-id"))

        # A startup-failure-path release (expected=None) must not clobber an
        # entry a legitimate worker has already been promoted into.
        APP._ai_batch_release_worker(self.batch_job_id, expected=None)

        self.assertEqual(APP._ai_batch_active_worker_job_id(self.batch_job_id), "real-job-id")
        APP._ai_batch_release_worker(self.batch_job_id, expected="real-job-id")
        self.assertFalse(APP._ai_batch_worker_registered(self.batch_job_id))


class ReconcileStateRegistryBehavioralTests(BehavioralTestCase):
    """Behavioral counterpart to ReconcileStateRegistryPrecedenceTests: a
    registered worker's activity must survive reconciliation even when the
    persisted folder_states snapshot looks fully terminal (the exact stale
    pre-reconciliation shape that triggered the original bug)."""

    def test_worker_alive_stays_true_despite_all_terminal_folder_snapshot(self):
        folders = {"f1": _make_folder(self.batch_job_id, "f1", status="failed")}
        state = _write_batch_state(self.batch_job_id, folders, status="running")
        state["job_id"] = "registered-job-id"
        APP._ai_batch_write_state(state)

        self.assertTrue(APP._ai_batch_reserve_worker(self.batch_job_id))
        self.assertTrue(APP._ai_batch_promote_worker(self.batch_job_id, "registered-job-id"))
        try:
            reconciled = APP._ai_batch_reconcile_state(dict(state))
            self.assertTrue(reconciled.get("worker_alive"), "registry-confirmed activity must outrank a stale terminal snapshot")
            self.assertEqual(reconciled.get("status"), "running", "reconciliation must not finalize/rewrite status while the worker is registered")

            persisted = APP._ai_batch_load_state(self.batch_job_id)
            self.assertEqual(persisted["folder_states"]["f1"]["status"], "failed", "reconciliation must not rewrite folder outcomes while a worker is registered")
        finally:
            APP._ai_batch_release_worker(self.batch_job_id, expected="registered-job-id")


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
