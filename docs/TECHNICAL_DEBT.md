# Technical Debt Register

Current, unresolved architecture and security debt only. Statuses: Open, Accepted Risk. Resolved items are removed from this document once closed — their history lives in Git and in the pull request that closed them, not here.

Each entry: affected area, evidence, current risk, desired state, safe migration approach, priority, status.

## ARCH-001 Monolithic Route/Domain/Mutation Coupling

- Affected area: Backend `app.py` and route modules.
- Evidence: Most operator workflows (import review, library, cleanup, deduplication, playlists, Plex, configuration, transactions, import) still live in `app.py` rather than a thin route calling an owned service.
- Current risk: Changes to one workflow can accidentally alter unrelated behavior; it is hard to tell whether a given call site uses a well-owned service method or a legacy shape.
- Desired state: Routes remain thin. Application services own workflows. Domain modules own matching and safety decisions. Provider adapters own external calls. Repository/client methods own Beets-engine access.
- Safe migration approach: Extract one tested service at a time. Prefer replacing legacy call shapes with explicit `BeetsClient` methods while preserving route signatures and responses.
- Priority: P0. Status: Open.

## ARCH-002 Duplicated Matching And Confidence Rules

- Affected area: Import review, playlist processing, missing-track replacement, duplicate handling, MusicBrainz submission preparation, AI-assisted candidate suggestion.
- Evidence: A canonical matching evidence engine exists (`backend/matching/`: explicit `AcoustIDStatus`/`ConfidenceState` states, Release Group vs. Release separation, global one-to-one track alignment, a single `can_auto_accept(policy)` automation gate). It is fully adopted by `backend.matching_contract.build_album_matching_decision`, `backend.mb_alignment.greedy_album_track_alignment`, `backend.track_align.align_tracks`, and playlist album-tag placement's auto-accept decision. Independent matching/confidence logic still exists elsewhere: the AI Suggest scorer family, `_folder_release_preflight`/`_score_mb_release_candidate`, `_album_track_norm`/`_album_track_score`/`_best_album_track_match` (the highest blast-radius remaining case — a dozen call sites, several of which already implement partial order-dependent duplicate protection but not the canonical engine's global optimum), several AcoustID interpretation helpers with their own status vocabularies, `routes_submissions._compare_release_tracks`, `backend.album_match.build_album_match_plan`, and `backend.beets_control_agent._playlist_import_identity_ok`.
- Current risk: Release-group candidates can be accepted or rejected differently depending on entry point; a fuzzy/numeric score can authorize a decision one workflow's canonical engine would refuse.
- Desired state: Every production matching/confidence decision either uses the canonical engine directly, is a thin wrapper around it, or is proven candidate-generation-only (candidate generation may stay workflow-specific; candidate *evaluation* may not).
- Safe migration approach: Migrate one family at a time behind equivalence tests (old vs. new outcome comparison for changed hard-identity results); do not replace a scoring formula wholesale without re-verifying every threshold tuned against it. See `docs/adr/0002-release-group-id-is-canonical-album-identity.md` and `docs/adr/0003-ai-is-optional-and-not-source-of-truth.md` for the identity/AI rules this must not violate.
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

## ARCH-007 Raw SQL Compatibility Layer Bypasses Repository Boundary

- Affected area: Web-manager legacy `_db()`/`RemoteSQLiteConnection` callers.
- Evidence: The web manager has no local SQLite file handle at all — `_db()` yields `backend.beets_client.RemoteSQLiteConnection`, which routes every `.execute()` call through `BeetsClient.raw_sqlite_query()`. That method is not a compatibility shim with variable behavior; it is a hard, unconditional `raise BeetsError(...)` ("Raw SQL is intentionally unavailable" per its own docstring). A route whose read path still goes through `_db()` is completely non-functional in the real, only-supported two-service deployment, not degraded. Five callers found broken this way by real two-service Docker acceptance testing (`library_merge_artist`, `library_normalize_artists`, `_run_normalize_artists_if_needed`, `library_mbsync_all`, `library_move_all`) have been migrated onto narrow, purpose-built `BeetsClient` read methods (`find_all_albums_by_albumartist`, `list_distinct_albumartists`, `find_all_orphan_albums`, `list_distinct_item_paths`) and reverified against a live container.
- Current risk: Other, not-yet-audited routes may still express a Beets library read as a raw-SQL-shaped compatibility call and be silently non-functional the same way, discovered only when actually exercised.
- Desired state: A small Beets repository/client layer owns common reads/writes; web-manager routes call typed methods rather than raw SQL-shaped compatibility calls. No remaining `_db()` read call sites.
- Safe migration approach: Audit remaining `_db()`/`RemoteSQLiteConnection` call sites one by one; add a narrow, purpose-built `BeetsClient` method per genuinely distinct read shape rather than a general query surface. Verify each with real two-service Docker acceptance, not unit tests alone (a unit test mocking `BeetsClient` cannot catch this class of defect).
- Priority: P2. Status: Open.

## ARCH-009 Release ID And Release-Group Identity Are Inconsistently Modeled

- Affected area: `app.py`, `helpers_mb.py`, `frontend/src/api/client.ts`, `frontend/src/api/types.ts`, `frontend/src/features/importReview/ImportReviewPage.tsx`, import review, folder import, repair, cleanup, playlist placement, and replacement workflows.
- Evidence: `docs/adr/0002-release-group-id-is-canonical-album-identity.md` defines MusicBrainz release-group ID as canonical album identity, but runtime code still carries both `mb_albumid` and `mb_releasegroupid` with mixed responsibilities — some call sites treat `mb_albumid` as the primary operational album candidate; others correctly use it only as edition-level evidence (concrete tracklist, date, country, medium, track positions). API/client payload shapes are not fully consistent about which field a given response requires.
- Current risk: Matching, folder placement, imports, repairs, cleanup, and replacement can disagree about whether a release ID or release-group ID is the album identity; a release-level candidate could incorrectly drive canonical folder identity.
- Desired state: Shared matching and import contracts carry both fields with explicit names and semantics: release-group ID for canonical album identity, optional release ID for edition-level evidence, recording IDs for track identity. No workflow substitutes a release ID where release-group identity is required.
- Safe migration approach: Do not rename fields globally. Add contract tests and typed result shapes around current entry points first, then update one workflow at a time to require/propagate `mb_releasegroupid` for album identity while retaining `mb_albumid` as representative release evidence. Keep API compatibility by accepting existing fields during transition.
- Required tests: Unit tests for release-vs-release-group normalization; contract tests for MusicBrainz release/release-group candidates; import review tests for selected-match propagation; playlist placement tests for representative release evidence; repair/replacement tests that keep release-group folder identity stable; cleanup tests that do not merge distinct release groups; a regression test proving a release ID is never written where a release-group ID is required.
- Priority: P0. Status: Open.

## ARCH-012 Library Disk-Walk Loses Real Albums Whose Folder Is Entirely Missing

- Affected area: `app.py` `_build_library_payload()`, the leftover-`missing_by_bucket` injection pass ("any remaining missing items belong to artists not on disk at all").
- Evidence: When an entire album's on-disk folder is removed but its Beets rows remain, the leftover-injection code's artist/album name-based lookup can fail to match items back to their real `album_id`, landing the album in the singleton bucket instead of being counted as a real album — undercounting `/api/library`'s `albums`/`tracks` totals.
- Current risk: Medium. Read-only display accuracy only (no data mutation risk), but a real and reproducible undercount.
- Desired state: The leftover-missing-item injection reliably resolves every fully-missing album back to its real `album_id`, most likely by keying off each missing item's own `album_id` (already present on the item row) rather than an artist/album name-string lookup, which is fragile to normalization differences between stored fields and on-disk folder names.
- Safe migration approach: This is disk-walk traversal logic — build a side-by-side parity harness (old output vs. new output over a real or realistically-shaped fixture) before changing `_build_library_payload()`, so a fix cannot silently regress a different part of the walk.
- Required tests: A parity/regression test modeling a real album whose entire folder is missing from disk, asserting it is still counted in `albums`/`tracks`, not `singleton_tracks`.
- Priority: P1. Status: Open.

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
