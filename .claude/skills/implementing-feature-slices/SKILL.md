---
name: implementing-feature-slices
description: Implements one scoped feature or behavior change in beets-web-manager without broad rewrites. Use for new endpoints, UI behavior, job steps, provider integration changes, or contained architecture migration slices.
---

# Implementing Feature Slices

Implement the requested slice and nothing broader.

## Start

1. Inspect `git status --short`.
2. Identify the smallest backend/frontend path that owns the requested behavior.
3. Read existing tests and nearby implementations before designing new abstractions.
4. Read only the relevant sections of `docs/AI_ENGINEERING_RULES.md` if the slice touches identity, mutations, jobs, security, or architecture.

Do not automatically read every project document.

## Project Invariants

- Beets remains the authoritative library manager.
- `mb_releasegroupid` is canonical album identity.
- MusicBrainz and AcoustID are primary identity evidence.
- AI is optional/untrusted and must not block deterministic matching.
- No silent file/library mutation.
- No secrets in logs, responses, frontend state, tests, or commits.
- Keep routes thin and shared business rules in backend/domain helpers.
- Do not copy owner-specific TrueNAS configuration into the repository.

## Implementation Discipline

- Reuse a sound existing helper before creating another.
- Keep routes/components focused on orchestration and presentation.
- Do not redesign unrelated UI or refactor unrelated code.
- Add tests at the lowest level that proves the behavior.
- Use disposable test data for filesystem/library behavior.
- If a larger architectural issue is discovered, record it as debt instead of expanding this slice unless it blocks correctness.

## Validation

Run targeted tests first. Run broader backend/frontend checks once after the targeted tests pass if the slice affects shared behavior.

Do not push, modify a PR, merge, deploy, or touch production without explicit authorization.

## Report

Return:
- Scope Implemented
- Key Design Choice
- Tests/Validation
- Files Changed
- Deferred Debt, if any
