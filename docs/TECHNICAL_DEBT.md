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

- Affected area: Import review, AI album/recording suggestion, playlist processing, missing-track replacement, duplicate handling, MusicBrainz submission preparation.
- Original evidence: Matching logic existed independently in `app.py` (`_track_ai_*`, album/folder AI release scoring, import review revalidation/preflight, `_best_album_track_match`, candidate-track comparison, library cleanup/reconcile, playlist matching/placement), `helpers_mb.py`, `backend/track_align.py`, `backend/mb_alignment.py`, `backend/import_guard.py`, `backend/album_match.py`, `backend/beets_control_agent.py`, `backend/transaction_engine.py`, and `routes_submissions.py`. See `docs/arch002_matching_inventory.md` for the pre-consolidation table.
- Current progress: Introduced `backend/matching/` as the canonical evidence service:
  - `normalize.py`: deterministic Unicode-aware normalization/title variants with no nested-risk regex dependency.
  - `models.py`: explicit `AcoustIDStatus`, `ConfidenceState`, release-group result, release result, track assignment, missing/unmatched structures.
  - `track_alignment.py`: global one-to-one assignment, AcoustID confirmed/conflict/no_result/unavailable/ambiguous distinction, existing-recording hard-conflict preservation.
  - `evidence.py`: release-group candidate evaluation, Release ID vs Release Group ID separation, explainable score components, conflicts, missing evidence, review reasons.
- Production callers migrated so far: `backend.track_align.align_tracks` (`confirmed_import_v1`) delegates to canonical global alignment; `backend.mb_alignment.greedy_album_track_alignment` (`album_mb_track_repair_v1`) is now a compatibility wrapper around canonical global alignment; `backend.matching_contract.build_album_matching_decision` derives album/RG matching decisions from canonical release-group evidence while preserving its outward contract; the beets Docker image now copies `backend/matching/` into the flattened agent runtime; Import Review's safety panel displays canonical state/RGID/alignment counts/conflicts from `matching_contract.evidence.canonical_match`; `_album_track_norm`, `_album_track_score`, `_best_album_track_match` delegate to canonical matching normalizers; AI suggestions (`_track_ai_norm`, `_track_ai_similarity`, `_score_track_ai_candidate`, `_score_mb_release_candidate`) and folder release preflight (`_folder_release_preflight`) evaluate through canonical engine.
- AcoustID policy implemented in the canonical service: confirmed, conflict, no_result, unavailable, and ambiguous are separate states; missing/no-result evidence is not a conflict; confirmed fingerprint evidence can carry a title-differs warning; conflicting fingerprint evidence blocks acceptance.
- Confidence policy implemented in the canonical service: semantic states (`confirmed`, `strong_match`, `review_recommended`, `conflict`, `insufficient_evidence`) are separated from score components. Hard conflicts force `conflict` regardless of soft title/AI confidence.
- Test coverage added/updated: `tests/test_arch002_matching_corpus.py` covers wrong mixed RG metadata, correct RG/no tracks, title mismatch plus AcoustID confirmation, AcoustID conflict, missing AcoustID, duplicate assignment, bonus/deluxe, missing/extra tracks, same-title songs, hard recording conflicts, fresh untagged imports, ordering invariants, and adversarial normalization. Existing engine/import suites now exercise the canonical wrappers. `frontend/tests/ImportReviewMatchingSafety.test.tsx` covers canonical evidence display.
- Current remaining risk: ARCH-002 is **not closed** yet. Some remaining caller paths (candidate comparison payload ranking, playlist library candidate scoring, library cleanup scoring, `backend.import_guard` boolean guards, `backend.album_match`, submission release-track comparison) still need caller-by-caller migration/audit proving final identity/confidence/action decisions pass through the canonical service.
- Desired state: Candidate generation may use provider-specific search/ranking, but every production candidate that can affect import, repair, reconciliation, playlist placement, or submission readiness must be evaluated through the canonical evidence service before action or user-facing confidence is reported.
- Safe migration approach: Continue migrating one caller family at a time, keeping response-shape compatibility tests at each boundary. Add structural regression tests after migration to prevent new normalization/confidence/AcoustID-policy implementations from reappearing outside `backend/matching/` and documented provider candidate-generation code.
- Priority: P0.
- Status: Open.

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

- Affected area: `app.py`, `helpers_mb.py`, `frontend/src/api/client.ts`, `frontend/src/api/types.ts`, `frontend/src/features/importReview/ImportReviewPage.tsx`, import review, folder import, repair, cleanup, playlist placement, and replacement workflows.
- Evidence: `docs/adr/0002-release-group-id-is-canonical-album-identity.md` defines MusicBrainz release-group ID as canonical album identity, but runtime code still carries both `mb_albumid` and `mb_releasegroupid` with mixed responsibilities — some call sites treat `mb_albumid` as the primary operational album candidate; others correctly use it only as edition-level evidence (concrete tracklist, date, country, medium, track positions). API/client payload shapes are not fully consistent about which field a given response requires.
- Current risk: Matching, folder placement, imports, repairs, cleanup, and replacement can disagree about whether a release ID or release-group ID is the album identity; a release-level candidate could incorrectly drive canonical folder identity.
- Desired state: Shared matching and import contracts carry both fields with explicit names and semantics: release-group ID for canonical album identity, optional release ID for edition-level evidence, recording IDs for track identity. No workflow substitutes a release ID where release-group identity is required.
- Safe migration approach: Do not rename fields globally. Add contract tests and typed result shapes around current entry points first, then update one workflow at a time to require/propagate `mb_releasegroupid` for album identity while retaining `mb_albumid` as representative release evidence. Keep API compatibility by accepting existing fields during transition.
- Required tests: Unit tests for release-vs-release-group normalization; contract tests for MusicBrainz release/release-group candidates; import review tests for selected-match propagation; playlist placement tests for representative release evidence; repair/replacement tests that keep release-group folder identity stable; cleanup tests that do not merge distinct release groups; a regression test proving a release ID is never written where a release-group ID is required.
- Priority: P0. Status: Open.

## ARCH-019 Job/Transaction-Status Test Can Be Intermittently Flaky Under CI Load

- Affected area: `tests/test_import_review_attach_enforcement.py::test_undo_restores_previous_identity_values`, which asserts a transaction's `status` is already `"Rolled Back"` immediately after its rollback job reports `success`.
- Evidence: Observed once on real GitHub Actions CI when this repository's own dual-workflow-trigger setup (push + pull_request) ran two independent job instances against the identical commit simultaneously — one passed, the other failed on exactly this assertion under real resource contention. Never reproduced locally or in an unloaded CI run; an immediate re-run of the identical commit passed cleanly.
- Current risk: Low but real — points at a possible race between a rollback job reporting success and the transaction store's own status field committing its terminal value, that only manifests under genuine concurrent load.
- Desired state: The test polls/rechecks status with a short bounded retry instead of asserting immediately after the job reports success, unless a real ordering bug in the production rollback path is confirmed, in which case that path itself should not report the job "success" until the transaction record's status update has actually committed.
- Safe migration approach: Reproduce deliberately under artificial CI-like load before deciding which side needs the fix — do not guess from a single observed instance.
- Priority: P3. Status: Open.

## ARCH-020 Artist-Folder Merge/Stamp Scanning Requires a Local Media Mount Web Manager Does Not Have

- Affected area: `app.py`'s `_stamp_artist_folder_scan()` and `_scan_artist_folder_groups()` (backing `/api/clean/artist-folders/stamp-mbid` and `/api/clean/artist-folders/scan` + `/merge`).
- Evidence: found while writing a real two-service Docker acceptance scenario for hotfix v0.1.17 (BUG-4/5/6). Both functions call `root.iterdir()`/an equivalent local directory walk directly against `MUSIC_ROOT` (`/data/media/music`) from inside the `beets-web-manager` process. Neither `docker-compose.yml` nor `docker-compose.full.yml` mounts the music library into `beets-web-manager` at all (by design -- see `docs/ARCHITECTURE.md`'s non-negotiable "Web Manager does not need a local media mount" rule) -- `beets-web-manager` only ever mounts `/web-manager-data`. In the shipped two-service topology, `Path("/data/media/music")` does not exist inside that container, so `_artist_folder_repair_root()`'s own `candidate.exists()` check (a separate, correctly-written guard) fails closed with "Music library root does not exist" before either scan function is ever reached.
- Current risk: the entire artist-folder merge and MBID-stamping feature family (`clean_artist_folders_stamp_mbid`, `_apply_artist_folder_groups`, and their HTTP routes) cannot complete via its real route in the shipped two-service deployment -- it fails closed (400) rather than silently doing the wrong thing, but it does not work at all as shipped. `_run_artist_folder_reconcile_for_alias_merge()` (used by the artist-alias-merge workflow) is unaffected -- it takes explicit `candidates` and never calls either local-scan function.
- Desired state: candidate discovery for these two routes queries the Beets Engine via `BeetsClient` (a new, narrow "list artist folders with their `mb_albumartistid` distribution" engine endpoint, mirroring how `_engine_stamp_artist_folder_scan()`/`_engine_scan_artist_folder_groups()` already do the equivalent work engine-side for Plan) instead of walking the filesystem from the Web Manager process.
- Safe migration approach: add a single new engine endpoint (or reuse the existing `_engine_stamp_artist_folder_scan`/`_engine_scan_artist_folder_groups` transaction-engine functions via a thin new read-only Control Agent route) that returns the same candidate/skipped shape these two Web Manager functions currently compute locally; swap the Web Manager functions to call it via `beets_client` instead of `Path.iterdir()`; add real two-service Docker acceptance coverage proving the route now succeeds against the shipped (unmounted) topology, not just a workaround-mounted test container.
- Priority: P1 (a real, currently non-functional operator-facing feature in the supported deployment, but not a security/data-integrity issue since it fails closed rather than mutating incorrectly). Status: Open.

## SEC-001 Retained Plex Credential After Diagnostic Exposure

- Scope: `PLEX_TOKEN`, used only by `beets-web-manager`.
- Risk: the token appeared in private diagnostic session output during earlier development and must be treated as potentially exposed.
- Decision: owner reviewed the exposure and explicitly chose to retain the current token rather than revoke/replace it, accepting the associated risk.
- Existing mitigations: `PLEX_TOKEN` is sent via a request header, never a URL query parameter. A stable, persisted, installation-specific Plex client identifier is sent on every request, so any future rotation is cleanly attributable. Token values are never logged, printed, or included in reports.
- Future recommended action: rotate `PLEX_TOKEN` when convenient (Plex Web → Settings → Account → Authorized Devices → remove the session, then generate a replacement).
- Status: Accepted Risk. Not a release blocker.
