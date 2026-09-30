# Technical Debt Register

Current, unresolved architecture and security debt only. Statuses: Open, Accepted Risk. Resolved items are removed from this document once closed — their history lives in Git and in the pull request that closed them, not here.

Status stages (never collapsed): IMPLEMENTED (code merged-ready with tests) -> CI VERIFIED (all repository gates green) -> LIVE VERIFIED (production acceptance on TrueNAS) -> CLOSED (desired state reached).

Each entry: affected area, evidence, current risk, desired state, safe migration approach, priority, status.

## Closed Architecture Milestones (Summary)

- **ARCH-001 (Route Handlers Service Decomposition)**: Closed. Decomposed routes into layered domain services (`library_service`, `cleanup_service`, `import_service`, `dedup_service`, `artwork_service`, `maintenance_service`, `transaction_service`, etc.) with strict architectural boundaries, ownership inventories, and AST CI tests.
- **ARCH-004 (Shared Durable Job Model & Persistence Contract)**: Closed. Centralized durable execution contract in `backend/job_contract.py` with 18-field checkpoint schema, bounded retries with backoff, restart recovery classification (read-only resumable vs. engine-backed mutation vs. non-resumable side effect), lock key normalization, and 100% adoption across all mutating and long-running background jobs.
- **ARCH-005 (Frontend Decision Authority Alignment)**: Closed. Backend attaches authoritative decisions (`decision`, `action_eligibility`, `conflicts`, `warnings`) directly to all review items. Frontend interfaces consume backend verdicts directly, eliminating client-side drift.
- **ARCH-006 (Provider Transport & Result Interpretation Boundary)**: Closed. Outbound provider requests routed through `provider_boundary.opened` with typed `ProviderResult` and `ProviderOutcome` interpretation, Retry-After handling, secret redaction, and probe classification.
- **ARCH-010 (Elimination of Retired `backend/beets_client.py`)**: Closed (v0.1.25). Migrated to `composite_workflows` and `beets_adapter`.
- **ARCH-020 (Duplicate Resolver Retag Identity Repair & Merge)**: Closed. Rebuilt the duplicate resolver retag action to prove audio identity via AcoustID and MusicBrainz tracklist verification, repair recording identity through canonical item metadata update, and transfer ownership via canonical `album_row_merge`. Refuses identity rewrites through merge and eliminates all legacy merge callers.
- **ARCH-021 (Durable Batch Planner for Untracked Files & Albums)**: Closed. Introduced O(1) item path indexing, durable resumable batch planning (`plan_untracked_batch`) with rate-limited AcoustID budgeting and per-folder checkpointing, and safe sidecar/artifact quarantine batch planning (`plan_untracked_quarantine_batch`).

## SEC-001 Retained Plex Credential After Diagnostic Exposure

- Scope: `PLEX_TOKEN`, used only by `beets-web-manager`.
- Risk: the token appeared in private diagnostic session output during earlier development and must be treated as potentially exposed.
- Decision: owner reviewed the exposure and explicitly chose to retain the current token rather than revoke/replace it, accepting the associated risk.
- Existing mitigations: `PLEX_TOKEN` is sent via a request header, never a URL query parameter. A stable, persisted, installation-specific Plex client identifier is sent on every request, so any future rotation is cleanly attributable. Token values are never logged, printed, or included in reports.
- Future recommended action: rotate `PLEX_TOKEN` when convenient (Plex Web → Settings → Account → Authorized Devices → remove the session, then generate a replacement).
- Status: Accepted Risk. Not a release blocker.
