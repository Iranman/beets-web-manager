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

## ARCH-002 Duplicated Matching And Confidence Rules

- Affected area: Import review, AI album/recording suggestion, playlist processing, missing-track replacement, duplicate handling, MusicBrainz submission preparation.
- Canonical authority (`backend/matching/`): `evidence.evaluate_release_group_candidate` (album / release group, with `IdentityProof` and `ActionScope`), `recording.evaluate_recording_candidate` (single recording: embedded Recording ID, AcoustID, title/artist/duration/filename/position evidence, `RecordingIdentityProof`, `can_auto_attach()` / `identity_established()`), `recording.verify_audio_against_request` (downloaded-audio verification), and `track_alignment.align_tracks_global` (global track alignment).
- Migrated to canonical authority: `build_album_matching_decision`, `build_recording_matching_decision` (no second decision tree), Import Review / AI-suggest recording candidates (now given the full AcoustID hit set), format-replacement identity (`backend/recording_review.resolve_recording_identity`), playlist/download audio verification (`_audio_identity_decision`), Import Review auto-enqueue (`evaluate_import_eligibility` honors the canonical veto), playlist placement, folder preflight, and confirmed-import alignment (`track_align.align_tracks` -> `align_tracks_global`).
- Caller audit: `scripts/audit_arch002_callers.py` maps every production final-decision pattern hit to its function and checks it against `docs/arch002_caller_audit.json` (CI-enforced by `tests/test_arch002_caller_audit.py`: every hit-bearing unit must be classified and the NEEDS_MIGRATION set may only shrink). Current totals: 130 units / 450 hits — CANONICAL_FINAL_DECISION 32, COMPATIBILITY_WRAPPER 24, CANDIDATE_GENERATION_ONLY 24, DISPLAY_ONLY 38, SAFE_SPECIALIZED_EVIDENCE 8, TEST_ONLY 1, NEEDS_MIGRATION 3.
- Remaining NEEDS_MIGRATION (one workflow): existing-row reconciliation after an import into an existing album — `app._merge_imported_album_into_existing` via `backend.import_guard.existing_track_matches_target` / `existing_track_can_block_downloaded_replacement`. When neither a Recording ID nor a fingerprint decides, a text title score (0.90, or 0.72 with a matching Recording ID) chooses whether the existing row is retired or the new import is discarded. Migration needs a canonical non-destructive outcome for the text-only case (keep both and route to review), validated against live data first.
- Documented candidate-generation / specialized exceptions: provider search ranking (MusicBrainz, Discogs, Soulseek, AI ordering); playlist-entry text resolution (`_playlist_suggestions_for_track`, `_playlist_canonicalize_track`) rewrites only a playlist manifest's requested artist/title text, never library data; veto-only AcoustID checks in the engine (`_mb_track_repair_acoustid_check`, `_confirmed_import_acoustid_conflicts`).
- Desired state: every production final identity/safety decision flows through `backend/matching/`; NEEDS_MIGRATION = 0.
- Priority: P0.
- Status: Open (1 workflow / 3 units remaining).

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

## ARCH-009 Release ID And Release-Group Identity Are Inconsistently Modeled

- Affected area: import review, folder import, repair, cleanup/dedup, album merge, playlist placement, replacement.
- Rule (`docs/adr/0002-release-group-id-is-canonical-album-identity.md`): Release Group ID is canonical album identity; Release ID is edition evidence (tracklist, date, country, media) only.
- Verified correct: canonical folder identity (`_DEFAULT_ALBUM_PATH_TEMPLATE` stamps `$mb_releasegroupid`); canonical album evaluation and `build_album_matching_decision`; playlist placement (skips releases without an RGID); import-with-ID release/RG consistency check; post-import lookups that locate the concrete release just imported (release-level use).
- Fixed: duplicate-album merge identity (`_library_duplicate_merge_safety`) no longer lets a row with an unknown RGID inherit another row's RGID or requires a Release ID when every row shares one RGID (`tests/test_arch009_merge_identity.py`); format-replacement target album requires a Release Group (no Release-ID fallback); unattended duplicate deletion requires the same release slot, not just the same Recording ID (`backend/duplicate_identity.py`, `tests/test_duplicate_resolver_identity.py`).
- Remaining: the reconciliation path listed under ARCH-002 (`_merge_imported_album_into_existing`) matches an imported row to an existing album row by disc/track within an album already chosen upstream. It is not a Release-ID-for-RGID substitution, but it has not been re-audited end to end under ARCH-009, and API/client payload shapes still accept either field during the transition. No confirmed misuse remains open.
- Priority: P0. Status: Open (audit of the reconciliation path and payload-shape tightening outstanding).

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
