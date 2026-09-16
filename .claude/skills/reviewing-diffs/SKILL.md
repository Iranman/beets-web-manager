---
name: reviewing-diffs
description: Performs the final technical review-and-fix pass on an existing implementation by inspecting the actual diff and immediate blast radius. Use after an implementation is complete and needs one final review, correction, validation, and approval without a repository-wide re-audit.
---

# Reviewing Diffs

This is one final review-and-fix pass, not an iterative findings loop.

## Scope

1. Inspect `git status`, branch state, and the actual source diff.
2. Review changed files plus only the immediate callers, contracts, tests, and architecture boundaries needed to evaluate the change.
3. Verify the stated root cause/reason for the change.
4. Fix in-scope defects directly instead of returning findings for another agent.
5. Add/correct tests for issues found.
6. Do not reopen already accepted unrelated areas.

Read `docs/AI_ENGINEERING_RULES.md` only for boundaries actually touched by the diff. Read `docs/AGENT_WORKFLOW.md` only if authority, handoff, or stage behavior is ambiguous.

## Validation Order

1. Targeted tests/checks for changed behavior.
2. Any required runtime validation for Docker/plugin/binary/config behavior.
3. Full required validation suite once, after corrections are complete.

Avoid repeatedly running the full suite after each small edit.

## Review Priorities

- correctness/regressions,
- identity and matching rules,
- mutation/idempotency safety,
- security/secret exposure,
- API/frontend contract mismatch,
- missing failure handling,
- tests that prove behavior rather than implementation details.

Do not perform style-only refactors unless they are necessary for correctness or maintainability of the changed slice.

## Final Report

Keep concise:
- Corrections Made
- Validation Results
- Files Changed
- Remaining Blockers
- Final State: `approved`, `blocked`, or `needs owner decision`
