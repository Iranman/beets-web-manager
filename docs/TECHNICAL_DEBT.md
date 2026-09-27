# Technical Debt Register

Current, unresolved architecture and security debt only. Statuses: Open, Accepted Risk. Resolved items are removed from this document once closed — their history lives in Git and in the pull request that closed them, not here.

Each entry: affected area, evidence, current risk, desired state, safe migration approach, priority, status.

## ARCH-001 Monolithic Route/Domain/Mutation Coupling

- Affected area: Backend `app.py` and route modules.
- Evidence: Most operator workflows (import review, library, cleanup, deduplication, playlists, Plex, configuration, transactions, import) still live in `app.py` rather than a thin route calling an owned service.
- Current risk: Changes to one workflow can accidentally alter unrelated behavior; it is hard to tell whether a given call site uses a well-owned service method or a legacy shape.
- Desired state: Routes remain thin. Application services own workflows. Domain modules own matching and safety decisions. Provider adapters own external calls. Repository/client methods own Beets-engine access.
- Safe migration approach: Extract one tested service at a time. Prefer replacing legacy call shapes with explicit `BeetsAdapter` methods while preserving route signatures and responses.
- Priority: P0. Status: Open.

## ARCH-004 Job Persistence And Idempotency Are Uneven

- Affected area: `job_engine.py`, import review jobs, playlist download/sync jobs, AI batch import, acquisition, replacement, maintenance runner.
- Evidence: `JobStore` is in-memory. `PythonJob` supports structured state and cooperative cancellation. Some workflows add checkpoint files and uniqueness checks; others rely on route-local state or result inference.
- Current risk: Process restart, retry, or duplicate starts can repeat completed steps, lose progress, or leave stale active status unless each workflow implemented its own protections correctly.
- Desired state: Shared job requirements for operation identifiers, idempotency, resource locks, bounded retries, checkpoints, heartbeats, cancellation checks, and terminal-state recovery.
- Safe migration approach: Add job contract tests and a reusable idempotency/checkpoint helper. Migrate long-running workflows by risk, starting with import/replacement/playlist mutations.
- Priority: P1. Status: Open.

## ARCH-005 Frontend Decision Logic Can Drift From Backend Authority

- Affected area: `frontend/src/features/importReview/ImportReviewPage.tsx`, other large feature panels, `frontend/src/api/types.ts`.
- Evidence: Large feature components render data, poll jobs, manage local workflow state, and calculate some block/eligibility display. Frontend panels sometimes adapt backend evidence shapes locally rather than displaying them as-is.
- Current risk: UI can enable, hide, or label actions differently than backend eligibility; explanations can diverge from backend safety decisions.
- Desired state: Backend returns authoritative evidence, conflicts, safety result, and action eligibility. Frontend displays those fields and only handles presentation state.
- Safe migration approach: Extend backend contracts first, then simplify frontend helpers as contract consumers. Add static and UI tests for visible evidence and disabled/destructive actions.
- Priority: P1. Status: Open.

## ARCH-006 Provider Boundaries Are Inconsistent

- Affected area: MusicBrainz, AcoustID, OpenAI, Discogs, SLSKD, yt-dlp, Plex, Lidarr.
- Evidence: `helpers_mb.py` and `backend/slskd.py` are extracted boundaries; `app.py` still contains direct OpenAI, Discogs, yt-dlp, Plex, and download orchestration logic.
- Current risk: Retry, rate-limit, secret redaction, and failure representation differ by provider.
- Desired state: Each provider has a small adapter with typed inputs/outputs, explicit transient/permanent failure classification, bounded retries, and redaction.
- Safe migration approach: Extract adapters only when changing a workflow for a real bug. Preserve API responses and add contract tests.
- Priority: P2. Status: Open.

## ARCH-010 Composite Mutation Workflows Still Call The Retired `backend/beets_client.py`

- Affected area: `app.py`'s composite Plan/Apply/Rollback mutation workflows -- merge-album, merge-artist, Clean All, track replacement, folder/album cleanup, artist-folder reconcile, album maintenance/relocation/metadata-repair, album artwork fetch/embed, genre repair, mbsync-all, and move-all.
- Status: Closed / Completed (v0.1.25).
- Resolution: All composite workflows were migrated to `backend/composite_workflows.py` backed by `backend/beets_adapter.py` and `TransactionStore`. `backend/config_manager.py` was introduced for safe atomic configuration updates. `backend/beets_client.py` and all legacy control-agent references were completely eliminated (zero active references). Full 2,675 test suite and architecture invariants verified.

## ARCH-019 Job/Transaction-Status Test Can Be Intermittently Flaky Under CI Load

- Affected area: `tests/test_import_review_attach_enforcement.py::test_undo_restores_previous_identity_values`, which asserts a transaction's `status` is already `"Rolled Back"` immediately after its rollback job reports `success`.
- Evidence: Observed once on real GitHub Actions CI when this repository's own dual-workflow-trigger setup (push + pull_request) ran two independent job instances against the identical commit simultaneously — one passed, the other failed on exactly this assertion under real resource contention. Never reproduced locally or in an unloaded CI run; an immediate re-run of the identical commit passed cleanly.
- Current risk: Low but real — points at a possible race between a rollback job reporting success and the transaction store's own status field committing its terminal value, that only manifests under genuine concurrent load.
- Desired state: The test polls/rechecks status with a short bounded retry instead of asserting immediately after the job reports success, unless a real ordering bug in the production rollback path is confirmed, in which case that path itself should not report the job "success" until the transaction record's status update has actually committed.
- Safe migration approach: Reproduce deliberately under artificial CI-like load before deciding which side needs the fix — do not guess from a single observed instance.
- Priority: P3. Status: Open.

## SEC-001 Retained Plex Credential After Diagnostic Exposure

- Scope: `PLEX_TOKEN`, used only by `beets-web-manager`.
- Risk: the token appeared in private diagnostic session output during earlier development and must be treated as potentially exposed.
- Decision: owner reviewed the exposure and explicitly chose to retain the current token rather than revoke/replace it, accepting the associated risk.
- Existing mitigations: `PLEX_TOKEN` is sent via a request header, never a URL query parameter. A stable, persisted, installation-specific Plex client identifier is sent on every request, so any future rotation is cleanly attributable. Token values are never logged, printed, or included in reports.
- Future recommended action: rotate `PLEX_TOKEN` when convenient (Plex Web → Settings → Account → Authorized Devices → remove the session, then generate a replacement).
- Status: Accepted Risk. Not a release blocker.
