# Review Checklist

Use this checklist for every change that touches application behavior, jobs, matching, filesystem operations, provider integrations, or frontend decision UI.

## Architecture Boundaries

- Routes parse, authorize, validate, call services/helpers, and serialize responses.
- Domain logic does not depend on Flask request globals.
- External providers are accessed through explicit helpers/adapters.
- Jobs orchestrate services and checkpoints rather than duplicating domain decisions.
- Frontend code displays backend evidence and state; it does not make authoritative identity or mutation decisions.

## Business Rules

- MusicBrainz and AcoustID remain primary identity evidence.
- AI is optional, untrusted, and explicitly represented when unavailable or failed.
- Release-group ID is canonical for album identity; release ID is edition-level only.
- Placeholder or unresolved IDs are never written to tags, database fields, or folder names.
- Shared matching, eligibility, and mutation safety rules have one backend source of truth.

## Matching Correctness

- Candidate output includes local metadata, MusicBrainz release-group ID, optional release ID, recording IDs, AcoustID evidence, tracklist evidence, duration evidence, filename/tag evidence, AI contribution, confidence, conflicts, warnings, explanation, and action eligibility.
- A correct release-group candidate is not rejected only because one title differs when stronger recording, AcoustID, duration, and position evidence agree.
- Track count alone does not override conflicting fingerprint or recording evidence.
- Partial, multi-disc, missing-track-number, singleton, and duplicate-folder cases remain covered.

## Filesystem And Mutation Safety

- Mutating operations have preview or dry run, before/after metadata diff, before/after path diff, supporting evidence, confidence and reason, root validation, audit record, and recovery or rollback information where technically possible.
- Source and destination paths are resolved and constrained to approved roots.
- No operation reports success before filesystem and application state are verified.
- Destructive operations require stronger evidence and explicit confirmation.
- Tests use temporary directories, fixtures, or synthetic data and never modify the real music library.

## Jobs

- Jobs have stable operation identifiers or idempotency keys where reruns are possible.
- Retries are bounded and classified as retryable or terminal.
- Checkpoints prevent duplicate staging folders, repeated downloads, duplicate imports, repeated completed mutations, and unbounded retry loops.
- Cancellation is checked between safe steps and affects backend execution, not just UI state.
- Concurrent jobs cannot mutate the same track, album, folder, or playlist without a lock or explicit conflict response.
- Stale jobs have recovery or terminal-state handling.

## Error Handling And Security

- User-facing errors are clear and actionable; raw debug details stay separate.
- Missing credentials, invalid credentials, 401/403, rate limits, timeouts, and provider outages are represented explicitly.
- Logs, API responses, frontend state, fixtures, and commits do not contain secrets, cookies, tokens, authorization headers, signed URLs, or credential values.
- Security scans and relevant regression tests pass.

## Tests

- Bug fixes include a reproducing regression test when practical.
- Unit tests cover domain decisions.
- Contract tests cover provider/adaptor behavior.
- Integration tests cover high-risk workflows.
- Frontend tests or static assertions cover UI decision visibility when UI behavior changes.
- Existing relevant tests continue to pass.