# Technical Debt Register

Statuses: Open, In Progress, Blocked, Done.

## ARCH-001 Monolithic Route/Domain/Mutation Coupling

- Affected area: Backend `app.py`.
- Evidence: route scan found most API routes in `app.py`, including import review, library, cleanup, deduplication, playlists, Plex, configuration, transactions, and import endpoints. `app.py` also contains matching helpers, Beets subprocess calls, direct SQLite access, direct filesystem mutations, and job orchestration.
- Current risk: Changes to one workflow can accidentally alter unrelated behavior. AI-assisted edits are prone to conflicts because many responsibilities share one file.
- Desired state: Routes remain thin. Application services own workflows. Domain modules own matching and safety decisions. Provider adapters own external calls. Repositories own Beets/SQLite access.
- Safe migration approach: Extract one tested service at a time. Start with pure functions already represented in tests. Keep route signatures and responses stable.
- Priority: P0.
- Status: Open.

## ARCH-002 Duplicated Matching And Confidence Rules

- Affected area: Import review, playlist processing, missing-track replacement, duplicate handling, MusicBrainz submission preparation.
- Evidence: Matching logic exists in `app.py` (`_track_ai_*`, import review revalidation, `_best_album_track_match`, playlist matching), `helpers_mb.py`, `backend/track_align.py`, `backend/mb_alignment.py`, and `backend/import_guard.py`.
- Current risk: Release-group candidates can be accepted or rejected differently depending on entry point. AI, AcoustID, tracklist, duration, and title evidence can be weighted inconsistently.
- Desired state: One shared matching result contract and one authoritative backend implementation for confidence, conflicts, warnings, and action eligibility.
- Safe migration approach: Define contract around existing strongest structures, add adapter tests for each entry point, then migrate callers one workflow at a time.
- Priority: P0.
- Status: Open.

## ARCH-003 Mutations Do Not All Use One Controlled Boundary

- Affected area: Beets command execution, filesystem cleanup, metadata write, artwork repair, import, playlist placement, deduplication, replacement.
- Evidence: Direct `shutil.move`, `shutil.rmtree`, `Path.unlink`, `os.replace`, Beets `modify/write/move/import`, and direct SQLite writes appear in `app.py` and `routes_submissions.py`. `backend/transaction_engine.py` exists but is not universal.
- Current risk: Preview/audit/rollback behavior varies by workflow. Partial failure can be hard to recover or may be reported inconsistently.
- Desired state: Mutating workflows use shared plan/apply/verify/recover semantics with root validation, diffs, audit records, and recovery information.
- Safe migration approach: Wrap one high-risk mutation family at a time using `TransactionStore` rather than replacing all callers. Start with deletes/moves from Import Review and cleanup paths.
- Priority: P0.
- Status: Open.

## ARCH-004 Job Persistence And Idempotency Are Uneven

- Affected area: `job_engine.py`, import review jobs, playlist download/sync jobs, AI batch import, acquisition, replacement, maintenance runner.
- Evidence: `JobStore` is in-memory. `PythonJob` supports structured state and cooperative cancellation. Some workflows add checkpoint files and uniqueness checks; others rely on route-local state or result inference.
- Current risk: Process restart, retry, or duplicate starts can repeat completed steps, lose progress, or leave stale active status unless each workflow implemented its own protections correctly.
- Desired state: Shared job requirements for operation identifiers, idempotency, resource locks, bounded retries, checkpoints, heartbeats, cancellation checks, and terminal-state recovery.
- Safe migration approach: Add job contract tests and a reusable idempotency/checkpoint helper. Migrate long-running workflows by risk, starting with import/replacement/playlist mutations.
- Priority: P1.
- Status: Open.

## ARCH-005 Frontend Decision Logic Can Drift From Backend Authority

- Affected area: `frontend/src/features/importReview/ImportReviewPage.tsx`, other large feature panels, `frontend/src/api/types.ts`.
- Evidence: Large feature components render data, poll jobs, manage local workflow state, and calculate some block/eligibility display. API types are extensive and frontend panels sometimes adapt backend evidence shapes locally.
- Current risk: UI can enable, hide, or label actions differently than backend eligibility. User-facing explanations can diverge from backend safety decisions.
- Desired state: Backend returns authoritative evidence, conflicts, safety result, and action eligibility. Frontend displays those fields and only handles presentation state.
- Safe migration approach: Extend backend contracts first, then simplify frontend helpers as contract consumers. Add static and UI tests for visible evidence and disabled/destructive actions.
- Priority: P1.
- Status: Open.

## ARCH-006 Provider Boundaries Are Inconsistent

- Affected area: MusicBrainz, AcoustID, OpenAI, Discogs, SLSKD, yt-dlp, Plex, Lidarr.
- Evidence: `helpers_mb.py` and `backend/slskd.py` are extracted boundaries, while `app.py` still contains direct OpenAI, Discogs, yt-dlp, Plex, and download orchestration logic.
- Current risk: Retry, rate-limit, secret redaction, and failure representation differ by provider.
- Desired state: Each provider has a small adapter with typed inputs/outputs, explicit transient/permanent failure classification, bounded retries, and redaction.
- Safe migration approach: Extract adapters only when changing a workflow for a real bug. Preserve API responses and add contract tests.
- Priority: P2.
- Status: Open.

## ARCH-007 Direct SQLite Access Bypasses Repository Boundary

- Affected area: Beets database reads/writes across `app.py`.
- Evidence: `_db()` exists, but direct `sqlite3.connect(LIB_PATH)` calls also appear throughout `app.py`.
- Current risk: Lock handling, row factories, path normalization, and write safety can vary by caller.
- Desired state: A small Beets repository layer owns common reads/writes and lock retry policy.
- Safe migration approach: Consolidate repeated read-only queries first. Move write paths only when covered by mutation tests.
- Priority: P2.
- Status: Open.

## ARCH-008 Agent Instructions Were Too Large And Duplicated

- Affected area: `AGENTS.md`, `CLAUDE.md`.
- Evidence: Both files contained large overlapping operational guidance and product rules, making drift likely.
- Current risk: Different agents can follow different rules or miss key safety constraints in long files.
- Desired state: Concise agent files reference `docs/AI_ENGINEERING_RULES.md` as the shared rule source.
- Safe migration approach: Keep agent files short, add static governance tests, and update shared docs for rule changes.
- Priority: P1.
- Status: In Progress.