# ADR 0004: Library Mutations Use A Controlled Workflow

Date: 2026-07-20

## Status

Accepted

## Context

Repository inspection found direct Beets `modify/write/move/import` commands, direct filesystem moves/deletes/copies, and direct SQLite writes across `app.py` and `routes_submissions.py`. `backend.transaction_engine.TransactionStore` exists and records transaction status, changes, rollback metadata, settings, and job linkage, but it is not universal yet.

## Decision

All library-changing operations must move toward one controlled mutation workflow: inspect, plan, validate, diff, apply, audit, verify, checkpoint, and recover or report partial completion.

## Consequences

- New mutating routes must not silently change the library.
- Destructive operations require stronger evidence and explicit confirmation.
- Root validation, before/after metadata diff, before/after path diff, evidence, confidence, reason, audit record, and recovery information are required where technically possible.
- Existing mutation paths should be migrated incrementally through tested slices.