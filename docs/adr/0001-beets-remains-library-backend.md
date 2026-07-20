# ADR 0001: Beets Remains The Library Backend

Date: 2026-07-20

## Status

Accepted

## Context

This project is a self-hosted manager for a Beets music library. Repository inspection shows Beets command execution through `BEET_BIN`, `_beet_run()`, direct `beet modify/write/move/import` commands, Beets `Library(LIB_PATH)`, and the Beets SQLite library at `LIB_PATH`.

## Decision

Beets remains the underlying library-management system and source of library mutations. The application may cache, preview, audit, and explain workflow state, but it must not replace Beets with a parallel metadata database or duplicate library implementation.

## Consequences

- Library identity and file placement must stay compatible with Beets.
- New services should wrap Beets operations rather than bypassing them.
- Any direct SQLite write must be treated as high-risk and moved toward a controlled repository/mutation path.