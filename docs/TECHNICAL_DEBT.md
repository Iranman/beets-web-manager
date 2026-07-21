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

## ARCH-009 Release ID And Release-Group Identity Are Inconsistently Modeled

- Affected area: `app.py`, `helpers_mb.py`, `frontend/src/api/client.ts`, `frontend/src/api/types.ts`, `frontend/src/features/importReview/ImportReviewPage.tsx`, import review, folder import, repair, cleanup, playlist placement, and replacement workflows.
- Evidence: ADR-0002 defines MusicBrainz release-group ID as canonical album identity, while runtime code still carries both `mb_albumid` and `mb_releasegroupid` with mixed responsibilities. Examples where `mb_albumid` is treated as the primary operational album candidate include `app.py` functions `_resolve_album_release_for_import`, `_folder_release_preflight`, `_start_reimport_disk_job_internal`, `_album_mb_completeness`, and frontend fallback logic such as `initialMbid`/candidate matching in `ImportReviewPage.tsx`. Examples where release ID is legitimately edition-level evidence include `_fetch_mb_release_tracklist(mb_albumid)`, `_resolve_release_group_to_release`, `_playlist_album_tag_release_placement`, and `helpers_mb.py` release/recording candidate payloads that need concrete release tracklists, dates, country, medium, and track positions. Missing or inconsistent `mb_releasegroupid` propagation remains visible across API types and client payloads where some responses require only `mb_albumid`, while newer import-target preview and selected-match paths expose `release_group_id` or `mb_releasegroupid`.
- Diagnostic snapshot: On 2026-07-20, `git grep -I -o` at commit `1e065a6772bc6eed83e2d7be1c71dc3128285907` found 968 `mb_albumid` and 350 `mb_releasegroupid` occurrences across all tracked files in this branch. A source-focused scan over `app.py`, route modules, `helpers_mb.py`, `backend/`, and `frontend/src/` found 853 and 312 respectively. This is a dated diagnostic snapshot; it is not proof that every occurrence is incorrect and it is not a permanent invariant. The reviewer's approximate 572/172 count likely used a narrower or different search scope.
- Current risk: Matching, folder placement, imports, repairs, cleanup, and replacement can disagree about whether a MusicBrainz release ID or release-group ID is the album identity. A release-level candidate may incorrectly drive canonical folder identity, while valid edition-level release evidence is still required for tracklist and date comparison.
- Desired state: Shared matching and import contracts carry both fields with explicit names and semantics: release-group ID for canonical album identity, optional release ID for edition-level evidence, and recording IDs for track identity. No workflow substitutes a release ID where release-group identity is required.
- Safe migration approach: Do not rename fields globally. First add contract tests and typed result shapes around current entry points. Then update one workflow at a time to require/propagate `mb_releasegroupid` for album identity while retaining `mb_albumid` as representative release evidence. Keep API compatibility by accepting existing fields during transition and emitting warnings when only release ID is present for album-level decisions.
- Required tests: Unit tests for release-vs-release-group normalization, contract tests for MusicBrainz release and release-group candidates, import review tests for selected match propagation, playlist placement tests for representative release evidence, repair/replacement tests that keep release-group folder identity stable, cleanup tests that do not merge distinct release groups, and regression tests proving a release ID is never written where a release-group ID is required.
- Priority: P0.
- Status: Open.

## ARCH-010 AI Batch Import State Writes Have No Versioning Or Compare-And-Swap

- Affected area: `app.py` AI Batch Import durable state (`_ai_batch_write_state`, `_ai_batch_commit`, `_AI_BATCH_STATE_DIR/<batch_job_id>.json`) and every route that reads-modifies-writes it outside the active worker thread. Direct writers, confirmed by inspection to call `_ai_batch_commit`/`_ai_batch_write_state` themselves from the route: `ai_batch_pause`, `ai_batch_stop` (also directly kills the JobStore job before its own whole-state write), `ai_batch_import_status`'s reconcile-and-commit path, `ai_batch_import_recover`'s reconcile-and-commit path, and `start_ai_batch_import`'s same-source-path reconnect commit. `ai_batch_skip` is *not* a direct writer -- confirmed by inspection it only mutates the in-memory `_ai_batch_controls` dict and sets `_ai_batch_skip_event`; it never itself calls `_ai_batch_commit`/`_ai_batch_write_state`. It is an indirect trigger: the active worker thread observes the control/event change on its own next iteration and is the one that actually persists the resulting state. An earlier revision of this entry overstated `ai_batch_skip` as a direct writer; corrected here.
- Evidence: `PR #12` fixed the retry/recover *start* path specifically, across three iterations. The first iteration (`_start_ai_batch_job`, `_run_ai_batch_import`, `_ai_batch_recalculate_batch_state` self-heal) added `_ai_batch_try_reserve_start`/`_ai_batch_release_start`, a process-local guard held only while `_start_ai_batch_job` itself was starting a worker. A second, independent review reproduced a remaining duplicate-worker race: that reservation was released as soon as `_start_ai_batch_job()` returned, before the worker it started had made its first commit. A second recover request landing in that post-start/pre-heartbeat window read the still-pre-reconciliation, all-terminal-looking `folder_states` snapshot; `_ai_batch_recalculate_batch_state`'s finalize branch stamped `worker_alive=False` over the correct just-computed value, so the second request didn't recognize the first worker as active and started a second one. Fixed by replacing the startup-only reservation with `_ai_batch_active_workers`, a registry authoritative for the worker's *entire* lifetime (reserve -> promote to job_id -> ownership-safe release in the worker's own `finally`, only after `_run_ai_batch_import` exits), plus making `_ai_batch_reconcile_state` defer entirely to that registry (skip recalculation/finalization) whenever a worker is registered for the batch, instead of trusting a possibly-stale folder snapshot. A third independent review then found the worker-lifetime registry itself had a post-spawn startup-abort gap: the spawned worker thread's `job_id_ready.wait(timeout=10)` ignored its return value, so a failure in `_ai_batch_persist_job_association`/`_ai_batch_promote_worker` *after* the worker thread was already spawned released the registry from the outer function but left the worker blocked, which then proceeded into real batch work unregistered and unvalidated once the 10s wait elapsed. Fixed with an explicit handoff protocol (`handoff_ready`/`startup_aborted` events plus a registry re-check, `_ai_batch_active_worker_job_id(batch_job_id) == owned_job_id`, as the actual proof of a valid handoff) that the worker must positively pass before calling `_run_ai_batch_import`; cleanup responsibility transfers to the worker thread as soon as it is spawned rather than being retained by the outer function. The underlying mechanism all three bugs came from -- `_ai_batch_write_state` does a whole-file JSON replace with no version field, no compare-and-swap, and no check that the writer's in-memory snapshot is still current -- is unchanged and still used by every route listed above. Any of those routes committing a stale snapshot while the worker thread is concurrently committing newer progress can still silently lose data, by the same shape as the bugs PR #12 fixed for the start path.
- Current risk: A pause/stop/status-poll/recover request landing at the wrong moment relative to worker progress can revert folder state, drop a requeue, or misreport batch status -- same failure mode as the original retry-race bug, just via a different route. Risk is currently mitigated only by these routes' commits generally being fast and infrequent relative to worker commits, not by any structural guarantee. `ai_batch_skip` carries a narrower version of this risk indirectly, through the state the worker itself later persists in response to the control/event change, not through a write of its own. The active-worker registry added in this update mitigates the *duplicate-start* instance of this risk (routes now consult `_ai_batch_worker_registered`/`_ai_batch_active_worker_job_id` rather than trusting a raw state-file read for that specific decision), but does not add versioning to the underlying writes themselves.
- Desired state: A single compare-and-swap or optimistic-concurrency primitive for `_AI_BATCH_STATE_DIR` writes (e.g. a monotonic `state["version"]` checked-and-incremented under `_ai_batch_state_lock`, rejecting a stale-based write) that all commit call sites go through, replacing the current unconditional whole-file overwrite.
- Safe migration approach: Add the version field and CAS check to `_ai_batch_write_state`/`_ai_batch_commit` first, in a mode that only logs a warning on a detected stale write (no behavior change), to confirm how often it actually fires against real traffic before making it authoritative. Then migrate call sites incrementally, starting with the routes identified above. Required regression tests for that migration: route-versus-worker stale-write detection (a route's commit using a snapshot older than the worker's latest must be rejected or merged, not silently overwrite), and confirmation that legitimate sequential commits from the same writer are never rejected.
- Note on runtime model: this app's supported deployment is one Waitress process with a thread pool, not multiple worker processes (see `_ai_batch_active_workers`' comment in `app.py`). A version/CAS field is still worth adding for correctness and clarity even in a single-process model, and becomes necessary (not just nice-to-have) if the deployment model ever changes to multiple processes or containers sharing one state directory. The active-worker registry is explicitly process-local only -- it provides no guarantee across multiple Python processes, multiple containers, or multiple hosts sharing the JSON state directory; durable/file-based locking would be required for that, tracked here as future work, not implemented in PR #12.
- Priority: P2.
- Status: Open.
