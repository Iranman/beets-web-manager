# AI Engineering Rules

This document is the shared source of truth for Codex, Claude Code, and human maintainers. `AGENTS.md` and `CLAUDE.md` must point here instead of carrying separate architecture rule sets.

## Architecture Boundaries

Intended dependency direction:

```text
Frontend
  -> API routes
  -> Application services
  -> Domain decisions
  -> Provider adapters and repositories
  -> Beets, filesystem, database, and external services
```

Current code is still being migrated toward this shape. New work must move in this direction without broad rewrites.

- API routes parse input, authorize, validate, call a service/helper, and serialize output.
- Routes must not accumulate substantial matching, retry, filesystem, Plex, provider, or mutation logic.
- Domain logic must not depend directly on Flask request globals.
- External APIs must be reached through clear provider/helper boundaries.
- Jobs orchestrate services, idempotency keys, checkpoints, and cancellation checks.
- Filesystem mutations go through a controlled mutation boundary.
- Shared business rules have one backend implementation. Frontend code displays decisions and evidence; it does not become the authority.

## Permanent Domain Rules

- Beets remains the underlying library-management system.
- Do not replace Beets with a parallel metadata database or duplicate library implementation.
- MusicBrainz and AcoustID are the primary identity evidence.
- The canonical album identity is the MusicBrainz release-group ID (`mb_releasegroupid`).
- MusicBrainz release IDs are edition-level secondary data and may be retained as supporting context.
- Do not substitute a release ID where a release-group ID is required.
- Never write unresolved placeholder IDs into metadata, database fields, or folder names.
- Ambiguous, incomplete, or conflicting evidence goes to review.
- Destructive actions require stronger evidence than non-destructive suggestions.

## AI Rules

AI is optional and untrusted.

- AI may rank, explain, or adjudicate candidates already found through deterministic sources.
- AI must not invent an identity and treat it as verified.
- AI results must identify the evidence they used.
- Missing, invalid, unauthorized, rate-limited, timed-out, or unavailable AI credentials must not stop MusicBrainz or AcoustID matching.
- AI failure must be represented explicitly in the result.
- AI confidence never overrides MusicBrainz/AcoustID conflicts by itself.

## Matching Rules

Shared matching results should consistently represent:

- Input metadata.
- Candidate identities.
- MusicBrainz release-group ID.
- Optional MusicBrainz release ID.
- Recording IDs.
- AcoustID evidence.
- Tracklist evidence.
- Duration evidence.
- Filename and tag evidence.
- AI availability and AI contribution, when present.
- Confidence score and tier.
- Conflicts and warnings.
- Human-readable explanation.
- Final action eligibility and review requirement.

AcoustID/fingerprinting evidence is preferred over text-only matching when available, but conflicts still require review.

A correct release-group candidate must not be rejected merely because one title differs when AcoustID, recording identity, duration, and track position provide strong evidence. Conversely, a matching track count alone must not override conflicting fingerprint or recording evidence.

## Mutation Safety

Never silently modify the music library.

Any rename, move, merge, metadata replacement, deletion, replacement, artwork write, or Beets database update must use a controlled mutation workflow:

1. Inspect current state.
2. Produce a mutation plan.
3. Validate roots, identities, conflicts, and preconditions.
4. Display or record complete before/after metadata and path diffs.
5. Record supporting evidence, confidence, and reason.
6. Apply each step with an audit record.
7. Verify final filesystem state and relevant application state.
8. Mark completed steps.
9. Recover or clearly report partial completion when a later step fails.

Do not mark an operation successful before verification. Tests must use temporary directories, fixtures, or synthetic data and must never modify the real music library.

## Job Requirements

Jobs must provide:

- Persistent status.
- Human-readable progress.
- Separate raw debugging details.
- Cancellation.
- Bounded retries.
- Checkpoints.
- Resume behavior.
- Idempotency.
- Explicit terminal states.
- Clear failure reasons.

Re-running or resuming a job must not create duplicate staging folders, download the same track repeatedly, import the same file twice, repeat completed mutations, or restart an unbounded retry loop.

## Security Requirements

- Never expose credentials, tokens, cookies, authorization headers, signed URLs, or secret values in logs, API responses, frontend state, fixtures, or committed files.
- Redact raw exceptions and subprocess output before returning them to the browser when they may contain secrets.
- Store runtime credentials in environment variables, approved config files, or Docker secrets, not source files.
- Do not print token values from runtime config, compose files, environment variables, logs, or provider responses.
- Keep secret scans and outbound-security tests passing.

## Testing Requirements

- Add a regression test before fixing a bug when practical.
- Use unit tests for domain decisions.
- Use contract tests for provider/adaptor behavior.
- Use integration tests for workflows that cross Beets, jobs, database state, and filesystem state.
- Use focused end-to-end tests for the highest-risk user paths.
- Do not replace meaningful tests with mocks that only assert implementation details.
- All tests must use temporary directories, synthetic audio, fixtures, or static source inspection. They must not mutate the real music library.

## Definition Of Done

A change is done only when:

- It follows the documented dependency direction or records explicit debt.
- Existing behavior is preserved unless the slice intentionally changes it.
- Matching and identity rules are not duplicated or weakened.
- Mutation safety and job idempotency are not weakened.
- Relevant tests and checks pass or failures are reported with concrete causes.
- Security and secret-handling rules are respected.
- Documentation describes actual implementation state, not a fictional completed architecture.

## Rules For AI Coding Agents

- Inspect the repository and dirty state before editing.
- Keep changes small, reviewable, and scoped to the requested slice.
- Reuse existing abstractions where they are sound.
- Do not rewrite unrelated routes, services, jobs, UI, or deployment behavior.
- Do not commit directly to `main`.
- Do not hide incomplete migration behind vague language.
- Record unresolved architecture debt in `docs/TECHNICAL_DEBT.md`.
