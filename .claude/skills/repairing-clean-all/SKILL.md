---
name: repairing-clean-all
description: Diagnoses and fixes Clean All scans, task sequencing, checkpoints, resume behavior, artist/album folder merge logic, root repair, and idempotent library maintenance jobs. Use for Clean All failures, repeated steps, resume bugs, merge bugs, or partial-completion errors.
---

# Repairing Clean All

Treat Clean All as a resumable state machine, not a batch script.

## Required Behavior

- Completed checkpointed tasks are not repeated on resume.
- A resumed task starts from a deterministic saved state.
- Re-running a completed mutation is safe and does not duplicate work.
- Failures record the task and actionable reason without falsely marking success.
- Retry loops are bounded.
- Tests use temporary directories/synthetic data, never the live library.

## Identity Rules For Merge Operations

- Artist folders may merge only when identity evidence establishes the same MusicBrainz Artist ID.
- Album folders may merge only when identity evidence establishes the same MusicBrainz Release Group ID.
- Ambiguous or conflicting identity goes to review.
- Path/name similarity alone is insufficient for destructive merge/delete behavior.

## Debug Workflow

1. Start from the first failing task and exact exception.
2. Locate that task's implementation and its checkpoint/resume boundary.
3. Inspect only the state values and helpers used by that task.
4. Verify variables are defined on every resumed and fresh-entry path.
5. Reproduce with a small temporary fixture.
6. Add a regression test covering both:
   - fresh execution,
   - resume after prior tasks are marked complete.
7. Fix the narrow fault.
8. Verify running the same fixture twice does not duplicate or repeat completed mutations.

For a local exception such as an undefined variable, do not audit every Clean All task unless the fix exposes a shared state-contract defect.

Read the mutation/job sections of `docs/AI_ENGINEERING_RULES.md` only when changing the shared job or mutation contract.

## Report

Return:
- Failing Task
- Resume/Fresh Path Root Cause
- Fix
- Idempotency Check
- Tests
