---
name: fixing-targeted-bugs
description: Diagnoses and fixes one concrete bug, traceback, failing test, or regression with minimal repository context. Use for narrow failures such as NameError, exception traces, broken buttons, one failing workflow step, or a specific reproducible defect.
---

# Fixing Targeted Bugs

Solve one bug without turning it into a repository audit.

## Workflow

1. Inspect `git status --short`.
2. Start from the exact error, traceback, failing test, endpoint, component, or symbol named by the task.
3. Search for that symbol/error and read only:
   - the containing function/class,
   - its direct callers/callees needed to understand the failure,
   - the nearest relevant tests.
4. Reproduce with the smallest practical command or test.
5. State the empirical root cause before editing.
6. Add or update the smallest meaningful regression test when practical.
7. Make the minimum correct fix. Do not refactor unrelated code.
8. Run the targeted test and syntax/type check for changed code.
9. Expand scope only if evidence proves the bug crosses a shared boundary.

## Escalate Context Only When Needed

- Music identity/import matching: use `matching-music`.
- Clean All/checkpoint/merge logic: use `repairing-clean-all`.
- Security/CodeQL: use `handling-codeql`.
- Architecture or mutation-boundary changes: read only the relevant sections of `docs/AI_ENGINEERING_RULES.md`.
- Third-party behavior: check the pinned version and upstream issue/fix before inventing a workaround.

Do not read `docs/AGENT_WORKFLOW.md`, `docs/ARCHITECTURE.md`, or the entire repository for a local bug unless the evidence requires them.

## Validation

Prefer a targeted command such as:

```powershell
python -m unittest tests.test_name
```

For changed Python modules, run an appropriate `python -m py_compile ...` check.

Run the full project validation suite only when explicitly requested, when the fix changes a shared/cross-cutting boundary, or during final review/release verification.

## Report

Return only:
- Root Cause
- Fix Implemented
- Targeted Validation
- Files Changed
- Remaining Risk or `None`
