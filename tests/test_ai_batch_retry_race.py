"""Regression test for a real AI Batch Import retry bug found live on the
TrueNAS deployment, 2026-07-20.

Reported symptom: a batch retry logged a normal-looking reconciliation --
"Reconciled state: 1875 terminal, 151 requeued, 0 cached decision(s) ready"
-- immediately followed by "No unfinished folder work remains; batch is
terminal." with none of the 151 requeued folders actually reprocessed.
Live inspection of the persisted batch state file
(`ai_batch_jobs/<batch_job_id>.json`) showed all 151 folders still sitting
at their pre-retry `status` ("failed"/"timed_out") with `retry_count: 0`
-- proof the reconciliation's requeue-to-"ai_queued" update (which does
increment `retry_count`) never survived to disk, even though the log
showed it happening.

Root cause: `_start_ai_batch_job` (app.py) starts the background worker via
`jobs.start_python(_do, ...)`, which begins running `_run_ai_batch_import`
concurrently the moment `job_id_ready.set()` fires. The very next lines in
`_start_ai_batch_job` independently did their own
load -> mutate("status"="running") -> `_ai_batch_commit(...)` on a second,
unrelated in-memory copy of the same durable state -- with no
synchronization against the worker thread doing the exact same
load -> reconcile -> commit cycle on its own copy. `_ai_batch_commit`
unconditionally calls `_ai_batch_recalculate_batch_state`, which finalizes
the batch to a terminal status the instant it sees zero *unfinished*
folders -- true of the pre-reconciliation snapshot, since "failed" and
"timed_out" are terminal (not unfinished) folder statuses. When this
second, stale commit landed after the worker's own reconciliation write,
it silently clobbered the just-requeued "ai_queued" folder data back to
the old failed/timed_out state with a terminal batch status -- exactly
what was observed live.

Fixed by only doing the eager main-thread state commit for a genuinely new
batch (`recover_batch_job_id` falsy, no worker race possible since
`folder_states` is still empty at that point). For a recover/retry
reconnect, the main thread now just reads the current on-disk state for
the HTTP response and leaves every write to the worker thread.

That fix alone turned out not to be enough -- live-verifying it (triggering
a real retry against the still-stuck batch) surfaced a second, deeper bug
with the same root shape, entirely inside `_run_ai_batch_import` itself,
no threading required. That function used to call `_ai_batch_commit(state,
update_state)` right after setting `state["status"] = "running"` but
*before* the recover/retry reconciliation block that requeues retryable
folders. `_ai_batch_commit` unconditionally calls
`_ai_batch_recalculate_batch_state`, which mutates `state["status"]` in
place -- and on that first commit, `folder_states` still held the old,
unreconciled data (151 folders at "failed"/"timed_out", both terminal, not
unfinished), so recalculate immediately re-finalized `state["status"]`
back to a terminal value on the *same shared dict* the reconciliation loop
was about to run against. Reconciliation then correctly set 151 folders to
"ai_queued" and persisted that via the second commit, but never restored
`state["status"]` to "running" -- so the terminal check right after
(`if state.get("status") in _AI_BATCH_TERMINAL_STATUSES`) still saw the
stale terminal value and bailed out immediately. Net effect, confirmed
live: the persisted state ended up with `folder_status_counts` correctly
showing `ai_queued: 151`, but `status` stuck at `completed_with_warnings`
and `folders_unfinished: 151` -- an internally contradictory state that
explains the reported symptom exactly (reconciliation log line printed,
folders genuinely requeued, but the batch still declared itself terminal
and never called `_ai_batch_run_suggestions` to actually reprocess them).

Fixed by moving that first commit inside the fresh-batch branch only (it's
still needed there, to make "running" visible before a potentially slow
`os.walk` disk scan) -- the recover/retry branch now performs its
reconciliation first and commits exactly once, after `folder_states`
reflects the reconciled reality, so `_ai_batch_recalculate_batch_state`
sees the correct non-zero `unfinished` count and leaves `state["status"]`
alone.

A third issue surfaced live-verifying the second fix: the real stuck batch
had already been corrupted by the second bug on an earlier attempt, so its
persisted `status` was stuck at `completed_with_warnings` even though
`folder_status_counts` genuinely showed `ai_queued: 151` (unfinished).
Nothing previously ever walked `state["status"]` back off a terminal value
once set, so both `/api/ai-batch-import/recover` (plain and
`retry_failed=true`) short-circuited to a no-op "reconnected" response
trusting the stale terminal status, permanently blocking recovery.
`_ai_batch_recalculate_batch_state` now self-heals this: whenever it finds
`unfinished > 0` but the previously-persisted status is terminal, it
reopens the batch to "running" (and clears `completed_at`) instead of
leaving the contradiction in place, letting the existing stale-heartbeat
detection in `_ai_batch_reconcile_state` take it from there.

Two further gaps found while hardening the retry path against concurrent
use, neither reproduced live but both directly reachable from the fixed
code above:

1. `ai_batch_import_recover()` (`/api/ai-batch-import/recover`) had no
   check for an already-running worker before calling
   `_start_ai_batch_job()`. Two near-simultaneous recover/retry requests
   for the same `batch_job_id` (a double-click, a retried frontend
   request) could both pass the terminal-status/retryable-count checks
   -- neither request's read reflects the other's not-yet-started write
   -- and both spawn a `jobs.start_python()` worker running
   `_run_ai_batch_import` concurrently against the same durable state
   file. Fixed by checking `state["worker_alive"]` (already computed by
   `_ai_batch_reconcile_state`, called immediately above) and returning a
   `reconnected` response instead of starting a second worker.
2. Per-folder retries were unbounded: `retry_count` was tracked but never
   checked against a limit, so a folder that will never succeed (e.g. a
   permanently corrupt file) could be requeued by `retry_failed=true`
   forever. Fixed with `_AI_BATCH_MAX_FOLDER_RETRIES` (3): the
   reconciliation loop's retry branch now marks a folder
   `"retry_limit_exceeded"` instead of requeuing it once its retry count
   reaches the cap -- a status included in `_AI_BATCH_FAILED_FOLDER_STATUSES`
   (so it still counts as terminal/failed) but deliberately excluded from
   `_AI_BATCH_RETRYABLE_FOLDER_STATUSES` (so further `retry_failed=true`
   calls leave it alone instead of retrying indefinitely).
"""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")


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

    def test_recover_path_does_not_independently_commit_state(self):
        # The bug: an unconditional _ai_batch_commit() call after the worker
        # thread was already unblocked, racing that thread's own reconcile
        # + commit cycle. The fix scopes the eager commit to the
        # brand-new-batch branch only.
        recover_branch = self.fn[self.fn.index("if recover_batch_job_id:"):self.fn.index("else:")]
        self.assertNotIn("_ai_batch_commit(", recover_branch)
        self.assertIn("state = _ai_batch_load_state(batch_job_id)", recover_branch)
        self.assertIn('state["job_id"] = job.job_id', recover_branch)

    def test_fresh_batch_path_still_commits_running_status(self):
        else_branch = self.fn[self.fn.index("else:"):]
        self.assertIn('state["status"] = "running"', else_branch)
        self.assertIn("_ai_batch_commit(state, job.update_state)", else_branch)

    def test_worker_thread_is_unblocked_before_the_branch_runs(self):
        # job_id_ready.set() must precede the recover/fresh branch so the
        # worker's own load/reconcile/commit cycle is already free to run
        # concurrently -- the exact condition the race depends on, and the
        # reason this code cannot safely do a second unconditional commit
        # for the recover/retry case.
        set_pos = self.fn.index("job_id_ready.set()")
        branch_pos = self.fn.index("if recover_batch_job_id:")
        self.assertLess(set_pos, branch_pos)


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

    def test_limit_exceeded_folder_marked_distinctly_not_silently_dropped(self):
        retry_branch = self.fn[
            self.fn.index("if retry_failed and status in _AI_BATCH_RETRYABLE_FOLDER_STATUSES:"):
            self.fn.index('if status in _AI_BATCH_UNFINISHED_FOLDER_STATUSES and _pending_review_path_key')
        ]
        self.assertIn('status="retry_limit_exceeded"', retry_branch)
        self.assertIn("needs manual review", retry_branch)

    def test_retry_limit_exceeded_status_is_terminal_and_failed_but_not_retryable(self):
        self.assertIn(
            '_AI_BATCH_FAILED_FOLDER_STATUSES = {"failed", "import_failed", "ai_failed", "timed_out", "retry_limit_exceeded"}',
            APP_SOURCE,
        )
        retryable_line = next(
            line for line in APP_SOURCE.splitlines() if line.startswith("_AI_BATCH_RETRYABLE_FOLDER_STATUSES =")
        )
        self.assertNotIn("retry_limit_exceeded", retryable_line)


if __name__ == "__main__":
    unittest.main()
