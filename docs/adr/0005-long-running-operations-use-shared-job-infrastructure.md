# ADR 0005: Long-Running Operations Use Shared Job Infrastructure

Date: 2026-07-20

## Status

Accepted

## Context

`job_engine.py` provides `Job`, `PythonJob`, `JobStore`, subprocess log capture, structured state, and cooperative cancellation. Some workflows add durable checkpoints and idempotency, but behavior is not uniform across import, playlist, replacement, cleanup, and provider workflows.

## Decision

Long-running operations must use the shared job infrastructure and move toward a common job contract with persistent status, human-readable progress, raw debug details, cancellation, bounded retries, checkpoints, resume behavior, idempotency, and explicit terminal states.

## Consequences

- Jobs should orchestrate services rather than duplicate domain rules.
- Re-running or resuming must not duplicate staging folders, downloads, imports, or completed mutations.
- Retry loops must be bounded and classified.
- Cancellation must be checked between safe backend steps.