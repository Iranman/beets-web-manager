"""Comprehensive tests for ARCH-004 Durable Job Model.

Covers:
- Standardized checkpoint schema validation & progress publishing
- Shared bounded retries with backoff, Retry-After, and cancellation support
- Restart recovery matrix:
  - Resumable computation (read-only/deterministic)
  - Engine-backed mutation (query engine evidence / manifest; never replayed blindly)
  - Interrupted ambiguous mutation -> recovery_required
  - Safe pre-apply recovery
"""

import time
import unittest
from unittest.mock import MagicMock

import backend.job_contract as jc


class CheckpointSchemaTests(unittest.TestCase):
    def test_make_checkpoint_contains_all_standard_fields(self):
        cp = jc.make_checkpoint(
            workflow="test-sync",
            workflow_version="2.0",
            job_id="job-123",
            operation_id="op-456",
            resource_keys=["workflow:test-sync", "album:99"],
            stage="running",
            phase="processing_units",
            unit_index=5,
            unit_identity="/music/Artist/Album/track05.flac",
            completed_units=5,
            pending_units=10,
            total_units=15,
            engine_operation_id="engine-op-789",
            retry_count=2,
            provider_retry_state={"mb_retries": 1},
            classification=jc.JOB_CLASSIFICATION_ENGINE_BACKED_MUTATION,
            cancel_requested=False,
            extra={"custom_field": "val"},
        )
        required_fields = [
            "workflow", "workflow_version", "job_id", "operation_id", "resource_keys", "lock_keys",
            "stage", "phase", "unit_index", "unit_identity", "completed_units", "pending_units",
            "total_units", "last_safe_checkpoint", "engine_operation_id", "retry_count",
            "provider_retry_state", "classification", "created_at", "updated_at", "heartbeat",
            "cancel_requested", "extra"
        ]
        for field in required_fields:
            self.assertIn(field, cp, f"Field '{field}' missing from checkpoint schema")

        self.assertEqual(cp["workflow"], "test-sync")
        self.assertEqual(cp["workflow_version"], "2.0")
        self.assertEqual(cp["completed_units"], 5)
        self.assertEqual(cp["pending_units"], 10)
        self.assertEqual(cp["classification"], jc.JOB_CLASSIFICATION_ENGINE_BACKED_MUTATION)

    def test_checkpoint_controller_updates_and_publishes(self):
        published = []
        controller = jc.JobCheckpointController(
            "batch-import",
            update_state=lambda s: published.append(s),
            classification=jc.JOB_CLASSIFICATION_ENGINE_BACKED_MUTATION,
            workflow_version="1.0",
        )
        self.assertTrue(len(published) >= 1)
        self.assertEqual(published[-1]["checkpoint"]["stage"], "running")

        # Record safe checkpoint before risky phase
        controller.update(
            phase="pre_engine_step",
            unit_index=1,
            unit_identity="album-10",
            is_safe_checkpoint=True,
        )
        self.assertIsNotNone(controller.state["last_safe_checkpoint"])
        self.assertEqual(controller.state["last_safe_checkpoint"]["phase"], "pre_engine_step")

        # Record engine operation ID before calling engine
        controller.record_engine_operation_before_request("engine-tx-999")
        self.assertEqual(controller.state["engine_operation_id"], "engine-tx-999")
        self.assertEqual(controller.state["phase"], "dispatching_engine_operation")


class BoundedRetryTests(unittest.TestCase):
    def test_successful_call_no_retry(self):
        calls = []

        def target():
            calls.append(1)
            return "ok"

        res = jc.bounded_retry(target, max_attempts=3)
        self.assertEqual(res, "ok")
        self.assertEqual(calls, [1])

    def test_retries_transient_error_until_success(self):
        calls = []
        delays = []

        def target():
            calls.append(len(calls) + 1)
            if len(calls) < 3:
                raise ConnectionError("temporary connection drop")
            return "success_after_retries"

        res = jc.bounded_retry(
            target,
            max_attempts=4,
            initial_backoff=0.01,
            backoff_factor=1.0,
            sleep_fn=lambda d: delays.append(d),
            on_retry=lambda attempt, exc, delay: None,
        )
        self.assertEqual(res, "success_after_retries")
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(delays), 2)

    def test_permanent_error_not_retried(self):
        calls = []

        def target():
            calls.append(1)
            raise jc.PermanentJobError("fatal database corruption")

        with self.assertRaises(jc.PermanentJobError):
            jc.bounded_retry(target, max_attempts=5)
        self.assertEqual(calls, [1])

    def test_exceeding_max_attempts_raises(self):
        calls = []

        def target():
            calls.append(1)
            raise TimeoutError("timeout")

        with self.assertRaises(TimeoutError):
            jc.bounded_retry(target, max_attempts=3, initial_backoff=0.01, sleep_fn=lambda d: None)
        self.assertEqual(len(calls), 3)

    def test_respects_retry_after(self):
        delays = []

        class RateLimitError(Exception):
            retry_after = 5.5

        calls = []

        def target():
            calls.append(1)
            if len(calls) == 1:
                raise RateLimitError("rate limited")
            return "done"

        jc.bounded_retry(
            target,
            max_attempts=2,
            max_backoff=10.0,
            sleep_fn=lambda d: delays.append(d),
        )
        self.assertEqual(delays, [5.5])


class RestartRecoveryEvaluationTests(unittest.TestCase):
    def test_resumable_computation_evaluates_to_resume(self):
        cp = jc.make_checkpoint(
            workflow="untracked-inventory",
            classification=jc.JOB_CLASSIFICATION_RESUMABLE_COMPUTATION,
            completed_units=1000,
            pending_units=5000,
            last_safe_checkpoint={"completed_units": 1000},
        )
        evaluation = jc.evaluate_restart_recovery(cp)
        self.assertEqual(evaluation["action"], "resume")
        self.assertEqual(evaluation["safe_checkpoint"]["completed_units"], 1000)

    def test_engine_backed_mutation_completed_on_engine_finalizes(self):
        adapter = MagicMock()
        adapter.get_operation.return_value = {
            "status": "succeeded",
            "result": {"modified_items": 4, "album_id": 42},
        }
        cp = jc.make_checkpoint(
            workflow="album-row-merge",
            classification=jc.JOB_CLASSIFICATION_ENGINE_BACKED_MUTATION,
            engine_operation_id="merge-op-123",
        )
        evaluation = jc.evaluate_restart_recovery(cp, adapter=adapter)
        self.assertEqual(evaluation["action"], "finalize_completed")
        self.assertEqual(evaluation["engine_result"]["modified_items"], 4)

    def test_engine_backed_mutation_failed_on_engine_reports_failed(self):
        adapter = MagicMock()
        adapter.get_operation.return_value = {
            "status": "compensated",
            "error_code": "VERIFICATION_FAILED",
        }
        cp = jc.make_checkpoint(
            workflow="album-row-merge",
            classification=jc.JOB_CLASSIFICATION_ENGINE_BACKED_MUTATION,
            engine_operation_id="merge-op-123",
        )
        evaluation = jc.evaluate_restart_recovery(cp, adapter=adapter)
        self.assertEqual(evaluation["action"], "failed")

    def test_engine_backed_mutation_never_sent_resumes_pre_apply_checkpoint(self):
        cp = jc.make_checkpoint(
            workflow="track-replacement",
            classification=jc.JOB_CLASSIFICATION_ENGINE_BACKED_MUTATION,
            engine_operation_id=None,
            last_safe_checkpoint={"phase": "pre_apply", "item_id": 501},
        )
        evaluation = jc.evaluate_restart_recovery(cp)
        self.assertEqual(evaluation["action"], "resume")
        self.assertEqual(evaluation["safe_checkpoint"]["phase"], "pre_apply")

    def test_engine_backed_mutation_ambiguous_outcome_requires_recovery(self):
        adapter = MagicMock()
        adapter.get_operation.side_effect = Exception("network timeout querying engine")
        cp = jc.make_checkpoint(
            workflow="album-row-merge",
            classification=jc.JOB_CLASSIFICATION_ENGINE_BACKED_MUTATION,
            engine_operation_id="merge-op-999",
        )
        evaluation = jc.evaluate_restart_recovery(cp, adapter=adapter)
        self.assertEqual(evaluation["action"], "recovery_required")

    def test_non_resumable_side_effect_requires_recovery(self):
        cp = jc.make_checkpoint(
            workflow="custom-external-sync",
            classification=jc.JOB_CLASSIFICATION_NON_RESUMABLE_SIDE_EFFECT,
        )
        evaluation = jc.evaluate_restart_recovery(cp)
        self.assertEqual(evaluation["action"], "recovery_required")


class CIGuardJobContractTests(unittest.TestCase):
    """Guard ensuring that mutating background jobs adopt job_contract and are not left unprotected."""

    def test_all_mutating_background_jobs_adopt_contract(self):
        from pathlib import Path
        repo_root = Path(__file__).resolve().parent.parent
        src_files = list(repo_root.glob("routes_*.py")) + list((repo_root / "backend").glob("*.py"))
        
        # Whitelisted read-only scans and internal job helpers
        unprotected_mutating_jobs = []
        for f in src_files:
            text = f.read_text(encoding="utf-8")
            if "jobs.start_python(" not in text:
                continue
            lines = text.splitlines()
            for idx, line in enumerate(lines):
                stripped = line.strip()
                if "jobs.start_python(" in stripped and not stripped.startswith("#") and not stripped.startswith(('"""', "'''", "*", "Returns", "job_id")):
                    # Check surrounding block (15 lines before and after)
                    block = "\n".join(lines[max(0, idx - 15):min(len(lines), idx + 15)])
                    has_contract = (
                        "job_contract.guarded" in block
                        or "job_contract.held" in block
                        or "job_contract.enter" in block
                        or "contract_metadata" in block
                        or '"mutating": False' in block
                        or "'mutating': False" in block
                        or "start_python_with_transaction" in block
                        or "untracked-inventory" in block
                        or "discography" in block
                        or "start_reimport_disk" in block
                        or "def _run_internal_job" in block
                    )
                    if not has_contract:
                        unprotected_mutating_jobs.append(f"{f.name}:{idx + 1}: {line.strip()}")

        self.assertEqual(
            unprotected_mutating_jobs,
            [],
            f"Found mutating background jobs not protected by job_contract: {unprotected_mutating_jobs}"
        )


if __name__ == "__main__":
    unittest.main()
