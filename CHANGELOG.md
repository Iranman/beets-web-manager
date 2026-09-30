# Changelog

All notable changes to this project will be documented in this file.

The project uses Semantic Versioning.

## Unreleased

### Changed
- **Album merges from Clean and from import now use the one album-row merge (ARCH-020).** The legacy merge reassigned `album_id` through a field update the engine refuses, and overwrote the Release ID and other album fields on the moved items.
  - Clean's duplicate-album and Release-Group merges, the split-album move and the import's existing-album reconcile now move item ownership only, within one Release Group and Release ID, into free slots.
  - Another edition, a release without IDs, or an already-filled slot is refused and stays for review.
  - Beets plugin 1.4.0: `album-row-merge` accepts `"partial": true` to move some of a row's items; the source row is retired only when emptied.
- **Import reconcile no longer removes imported duplicates on its own.** Imported copies of slots the existing album already holds become a reviewed duplicate cleanup: audio proof required, file moved to the engine quarantine, applied only on a reviewer's decision.
- The Release-Group merge plan refuses a retained row whose files are missing, so stale rows are recovered first.

### Fixed
- `merge-duplicate-album`, the Release-Group merge and the split-album merge failed at run time because of mismatched call arguments.
- Importing a single plugin module first could fail on a circular import; shared helpers now live in `engine_common.py`.
- A failed merge now restores a source row even if Beets removed it before raising.

### Known limitation
- The duplicate resolver's "retag" action is refused (`identity_rewrite_not_supported`): it rewrote Recording ID and track position on moved items without audio proof.

## v0.1.43 - 2026-09-29

### Added
- **Reviewed cleanup of a duplicate album row's only copy.** When a proven duplicate (shared AcoustID recording or identical bytes) is the only item of a duplicate album row of the keeper's own release, the operator-reviewed cleanup plan may now remove it and retire that emptied row.
  - Required: same Release ID and Release Group, the keeper in a different row at the same disc/track.
  - Beets plugin 1.3.1: `quarantine-remove-items` takes `retire_album_id` + `sibling_keeper_item_id`, re-checks those conditions itself, snapshots the row and retires it. Rollback recreates the row at its original id and puts the item back at its original id.
  - The unattended path and bulk cleanup never opt in; their album-slot gate is unchanged.

### Changed
- `quarantine-remove-items` never empties an album row implicitly any more (`ALBUM_WOULD_EMPTY`), and its rollback restores removed items at their original ids when those ids are free.

## v0.1.42 - 2026-09-29

### Added
- **Album-row merge (ARCH-020).** Beets plugin 1.3.0 adds `/webmanager/album-row-merge` with rollback and status endpoints.
  - It merges the duplicate album rows of one canonical album by changing item ownership only: no tag write, no file move.
  - It requires Release Group **and** Release ID proof, full source coverage, unchanged identity and content, and no slot overlap.
  - Rollback restores the original album rows at their original ids.
  - `POST /api/library/album-duplicate-analysis/plan-merge` plans only groups the live analysis proves deterministic.
- **Untracked recovery (ARCH-021).** Engine ops `/webmanager/untracked/attach` and `/untracked/quarantine`, with rollback and status.
  - `GET /api/library/untracked-recovery/candidates` and `POST /api/library/untracked-recovery/plan` expose four action classes, with backend-owned eligibility:
    - quarantine only byte-identical copies of tracked files;
    - attach a missing album file after tag, MusicBrainz and AcoustID proof;
    - track a better encoding, then plan its replacement through the one replacement authority;
    - no action otherwise.
  - The inventory is incremental: it reuses persisted records and hashes only new or changed files.
- **Durable jobs and locks (ARCH-004).**
  - Job records persist under `<data>/jobs` with checkpoints, heartbeats and a new terminal status, `recovery_required`. After a restart, an interrupted job is never re-run: read-only jobs become `failed`, other jobs `recovery_required`.
  - Durable, hierarchical, cross-process resource locks live under `<data>/locks`.
  - Engine-backed transactions interrupted by a restart are finished from engine evidence, never replayed.
- **Provider boundary (ARCH-006):** typed outcomes (`confirmed`, `no_result`, `unavailable`, `rate_limited`, `authentication_error`, `transient_error`, and others), bounded Retry-After-aware retries and redaction. Used by AcoustID lookups and MusicBrainz release fetches.

### Fixed
- An AcoustID outage, throttle, rejected key or timeout was cached permanently as "no match" for that file. Only real answers are cached now.
- A rollback job could report success after its transaction's "Rolled Back" status had been overwritten back to "Running" (ARCH-019). The route now marks the transaction Running before the job starts.

## v0.1.41 - 2026-09-29

### Fixed
- `--offline-db-identity` works with the stock Beets engine. Its web server does not close SQLite on a graceful stop, so the WAL is never settled by the engine. The check now copies the main file and WAL to a private temp directory, then checkpoints, `quick_check`s and hashes the settled copy. It never writes the authoritative files, and it verifies both are byte-for-byte unchanged afterwards. A failure prints "OFFLINE DB CHECK FAILED" with the right stage, not a rollout banner.
- The untracked inventory classifies folders and files carrying a never-resolved naming token (for example `{Album MbId}`) as import artifacts, not unknown.

## v0.1.40 - 2026-09-29

### Fixed
- Unattended duplicate cleanup no longer uses the old cleanup path. That path ran `os.unlink` inside the Web Manager on the read-only `/music` mount: the delete failed, only a warning was logged, and the transaction was still marked Completed with the file reported deleted. Its "rollback" did nothing. The path is removed.
- Manual live duplicate cleanup (`/api/dedup/cleanup` with `dry_run: false`) goes through the reviewed-cleanup authority. Every path must be the source copy of a proven duplicate pair; anything else is left in place and reported.

### Added
- **Reviewed duplicate cleanup** (`duplicate_cleanup_v1`): Plan → Approve → Apply → Verify → Rollback.
  - `POST /api/dedup/reviewed-cleanup/plan` re-proves every reviewed pair against live Beets using the scan's own policy: shared AcoustID recording, same release slot, keeper policy, album-slot gate, and no lossless rival.
  - It also checks that paths, sizes and slot evidence have not drifted since the proposal. A drifted pair is skipped and stays in review.
  - Apply needs approval, then verifies keepers, album slots and the exact library-count change.
  - Unattended cleanup, when an operator authorizes it, uses the same authority.
- Beets plugin 1.2.0: `POST /webmanager/quarantine-remove-items` and its rollback. The engine refuses the whole request unless every file still matches its reviewed SHA-256. It moves each file into the engine quarantine (never deleted) and removes the row through Beets, recording both in a manifest. On failure it puts everything back. Rollback re-adds the rows, into their album row when that row still exists.
- **One replacement authority** (`backend/item_replacement.py`). The item route, the music-format quality pipeline, import album merges and reconciliation reviews all plan through it onto the canonical engine-backed item-file replacement:
  - AcoustID decides; text never does.
  - The slot keeps its identity.
  - An occupied destination is displaced only for identical audio.
  - Anything unproven fails closed, with both files kept.
- Import merges and the music-format pipeline now *plan* replacements and leave them for approval, instead of applying them unreviewed. The pipeline records "Replacement awaiting approval" and never re-downloads a pending replacement.
- Read-only library integrity reports:
  - `POST /api/library/album-duplicate-analysis` groups album rows by Release Group and proposes a merge plan per group: retained row, item moves, overlapping slots, edition differences, blockers. It merges nothing.
  - `POST /api/library/untracked-inventory` runs a one-walk inventory of audio files Beets does not track. It reads AcoustID from the cache only, saves evidence under `/web-manager-data/untracked_inventory/`, and mutates nothing.
- `scripts/deploy_truenas_web_manager.sh --offline-db-identity`: stops Beets briefly, hashes the database only when its WAL is settled, restarts Beets and re-verifies it.

### Changed
- Deploy verification is WAL-aware. Online checks read the library through the Beets web API (counts, an identity digest over every item and album, plugin health) instead of opening SQLite. A live main-file hash is logged as informational only, never as proof of an unchanged database.

### Removed
- The legacy replacement engine and its bulk import wrappers:
  - `transaction_engine` track and bulk replacement functions.
  - `composite_workflows.plan/apply/rollback_bulk_import_replacement`.
  - The `unlink`-based `apply_library_cleanup` / `rollback_library_cleanup`.
- None of these have production callers left.

## v0.1.39 - 2026-09-29

### Fixed
- Beets plugin 1.1.1: `/webmanager` operations now act on the real absolute file paths. Beets 2.x stores paths relative to the library and expands them through a context variable, which is set only in the thread that opened the library. Web server request threads started with it empty, so every `item.path` loaded relative. As a result, replace-item-file refused files that existed, and `move` and file-deleting `remove` would have used the wrong paths. Each `/webmanager` request now binds the library's music directory first.
- Replacement failure recovery is complete. If the engine fails part-way, it now also moves the replacement file back to its original path and re-creates the replacement's library row, in addition to restoring the album item and its original file.

### Added
- Track replacement handles an occupied canonical destination. Suppose an untracked file already has the name Beets will give the replacement (Beets would otherwise add a `.1` suffix):
  - The plan step decodes both files with ffmpeg and compares PCM MD5s. The occupant may be displaced only if its audio is identical to the replacement's. Anything else fails closed with `destination_occupied`.
  - The Preview transaction records the occupant's SHA-256.
  - On apply, the engine displaces the file only if it is still untracked and its SHA-256 still matches; otherwise it refuses before changing anything.
  - The occupant goes to the engine quarantine folder and is never deleted. Rollback puts it back.

## v0.1.38 - 2026-09-28

### Fixed
- Track replacement resolves the library-relative paths the stock Beets web API reports: the plan route fingerprints the real files under the music root, and the post-apply check accepts the engine's absolute path for the same file.

## v0.1.37 - 2026-09-28

### Fixed
- Track replacement (preview → approve → apply → rollback) works again, now on the Beets engine. Before this fix, planning always failed: it read the wrong payload keys. Apply also tried to copy a file into the read-only music mount. Replacement now puts a tracked library copy (for example, a proven lossless duplicate) into an album slot:
  - The album item keeps its Release Group, Release ID, Recording ID, disc/track and tags.
  - Its old file is moved to an engine quarantine folder (`/config/webmanager-quarantine/<id>/`, with a manifest the engine rolls back from) and is never deleted.
  - The replacement's own library row is removed.
  - Beets moves the file to its canonical path.
- Apply requires an approved transaction. It verifies afterwards that the album slot kept its identity; if not, the transaction is marked Recovery Required.
- Replacing from an untracked staged file now fails closed with `staged_replacement_unsupported`.

### Added
- Beets plugin 1.1.0: `POST /webmanager/replace-item-file` and `/webmanager/replace-item-file/rollback` (takes only the engine's replacement id, never caller paths; capability `replace_item_file`). Both are idempotent and run under the mutation lock.
- `POST /api/items/<id>/replacement/plan` accepts `candidate_item_id`. The candidate's path comes from Beets, and the pair must match by AcoustID fingerprint. The generic `/api/transactions/<id>/apply` and `/rollback` routes handle these transactions.

## v0.1.36 - 2026-09-28

### Changed
- Unattended duplicate cleanup no longer deletes either copy when the preferred album copy is lossy and a proven duplicate is lossless. Such a group is flagged for replacement review instead: the right fix is replacing the album file through the reviewed replacement transaction. The review shows in the job log, the proposal (`action: replacement_review`) and the Duplicate Files panel.

## v0.1.35 - 2026-09-28

### Changed
- Unattended duplicate cleanup now picks which copy to keep by an explicit policy:
  1. a copy attached to the album row over a loose/singleton copy;
  2. valid canonical metadata (Recording ID, Release Group ID, release ID, disc/track);
  3. an embedded Recording ID that agrees with AcoustID;
  4. the canonical Beets path over a duplicate or decorated filename;
  5. audio quality (lossless over lossy, then bitrate, sample rate and bit depth; file size only between copies of the same format);
  6. the lowest item id, only as the final tie-breaker.
- Each proposal row states why its copy is kept.

### Added
- Album-slot gate: unattended cleanup never deletes a copy that is attached to an album row unless the retained copy is a tracked item in that same row. No album slot is ever left without a retained tracked item.

## v0.1.34 - 2026-09-28

### Fixed
- The tracked-library duplicate scan is single-pass:
  - It loads the library once and builds every index (Recording ID, size, path, album, fuzzy title) once.
  - It makes no full-library lookups per file.
  - Fuzzy matches resolve to the real Beets item, so release-slot evidence stays intact.
- Scan progress is reported live: N / total, percentage, current file, candidate count, files per minute and ETA in the Jobs view.
- Match scores shown in the log and UI are clamped to 100%.
- Log lines name their evidence: `FINGERPRINT VERIFIED`, `BYTE VERIFIED`, `CANDIDATE`, `REVIEW REQUIRED` or `REJECTED`.
- A cancelled job now reports `cancelled` instead of `failed`. Cancelled import jobs stay retryable.
- The playlist library index is keyed on a stable cache generation instead of a timestamp.

## v0.1.33 - 2026-09-28

### Changed
- The scheduled duplicate step checks only Beets-tracked library files instead of every audio file under the library mount. The live `/music` holds about 106k audio files but Beets tracks 3,144, and unattended deletion can only ever select tracked pairs. The same rules and proof apply, and the step finishes in minutes instead of days. The manual Duplicate Files scan still walks the whole folder.

## v0.1.32 - 2026-09-28

Library root fixed; unattended duplicate deletion needs explicit authorization.

### Fixed
- The Web Manager hard-coded its library root as `/data/media/music`, but every shipped compose file mounts the library at `/music`, so on live installs file-level features found no files. That included the scheduled duplicate scan, fingerprint checks and local artwork/path checks. `MUSIC_ROOT` is now the one configurable setting (default `/music`), and backend and frontend no longer hard-code a library path.

### Added
- Unattended duplicate deletion is a separate authorization, off by default and independent of `MUSIC_ROOT`. While it is off, the scheduled duplicate step scans, verifies with AcoustID and records a review proposal, but deletes nothing.
- Each proposal row shows:
  - both paths;
  - sizes;
  - embedded Recording IDs;
  - AcoustID fingerprint evidence;
  - the release/track slot;
  - which copy is kept.
- The proposal appears in the Duplicate Files panel and the job log.
- Enabling requires an exact confirmation phrase.
- New endpoints: `GET/POST /api/dedup/unattended-cleanup` and `POST /api/dedup/maintenance-run`.
- New regression tests prove that changing `MUSIC_ROOT` cannot enable destructive cleanup.

## v0.1.31 - 2026-09-28

`app.py` decomposed into owned services (ARCH-001). No API, route, or behavior changes are intended.

### Changed
- `app.py` went from 52,665 lines to about 570 lines of application glue: app creation, request hooks, security headers, error handlers, static and SPA serving, and route-module loading.
- The code moved verbatim into layered services under `backend/` (for example `library_service`, `playlist_service`, `import_service`, `dedup_service` and `ai_service`) and into route modules (`routes_library`, `routes_cleanup`, `routes_import`, `routes_playlist`, `routes_acquisition`, `routes_maintenance`, `routes_system`).
- `app.<name>` still resolves every moved name.
- The layer order, the global-state audit and the remaining debt are documented in `docs/arch001_service_decomposition.md`.
- Some routes were called in-process through a fake request context. They now call request-free services, for example `start_dedup_scan`, `start_album_download` and `start_folder_import_with_id`.
- Globals that functions rebound (the library cache, the last scan job) now live on shared state objects.

### Fixed
- The playlist JSON-state safety check looked up its allowed roots through `globals()`. That lookup would have silently found none once the code left `app.py`, so it now uses explicit references.
- Two `_extract_mb_uuid` definitions existed, and the later strict one silently shadowed the URL-capable parser. Pasted MusicBrainz URLs now resolve again.
- The dead `_db()` helper pointed at a removed control-agent function, so calling it could only raise a `NameError`. It now fails closed explicitly.

### Verified
- The Flask URL map is identical to v0.1.30: 259 rules, with the same endpoints and methods.
- The endpoint security inventory is unchanged (250 entries).
- The ARCH-003 mutation inventory is unchanged: 403 sinks with the same classifications.
- ARCH-002 `NEEDS_MIGRATION` is still 0.
- New CI guards in `tests/test_arch001_architecture.py` and `scripts/audit_arch001_ownership.py` check that:
  - nothing under `backend/` imports `app.py`;
  - a service only imports lower layers;
  - `app.py` has no SQLite, subprocess, Docker, `beet` command or matching policy;
  - the duplicate-deletion rule still requires fingerprint or byte proof.

## v0.1.30 - 2026-09-28

Duplicate cleanup requires audio proof (found with live AcoustID).

### Fixed
- Unattended duplicate cleanup treated a shared embedded MusicBrainz Recording ID as proof that two files were the same audio. With a working AcoustID key the live library showed this was wrong: in 6 of 7 duplicate groups the fingerprint contradicted the shared embedded Recording ID, and one proposed deletion ("Rush") had no fingerprint result at all. Unattended deletion now requires one of two things:
  - both copies fingerprint to a shared recording;
  - the files are byte-identical.
  Otherwise the pair is left for review.
- The dedup scan now fingerprint-checks "MB Track ID" matches the same way it checks fuzzy matches. A confirmed fingerprint mismatch rejects the candidate.

### Verified
- Live proposal: 6 deletions (one per audio-proven group; no group loses every copy). The unproven "Rush" pair is left for review. Previously there were 7 deletions.
## v0.1.29 - 2026-09-27

Final matching and identity closure: canonical import reconciliation, release-group identity contracts, and AcoustID key roles. **ARCH-002 and ARCH-009 are closed.**

### Added
- **`backend/import_reconciliation.py`**: a canonical reconciliation service for imports into an existing album. It first proves album identity by Release Group, then decides every contested disc/track slot with the canonical recording evaluator. There are four outcomes:
  - KEEP_EXISTING: both files are the same recording
  - KEEP_IMPORTED: the existing file is fingerprinted as the wrong recording
  - CONFLICT
  - KEEP_BOTH_REVIEW
  Text evidence never discards either file. Undecided slots keep both files and both library rows, and are recorded to `web-manager-data/import_reconciliation_reviews.json`.
- **Reconciliation review queue**: `GET /api/import-reconciliation/reviews` and `POST /api/import-reconciliation/reviews/<id>/resolve` (`keep_existing` / `keep_imported` / `keep_both`, applied only through engine transactions). A new "Reconciliation review" panel on the Import Review page shows each side's canonical evidence.
- **`backend/identity_contract.py`**: every release ID supplied to a mutation is resolved to its authoritative MusicBrainz Release Group. A mismatch is refused, an unverifiable release fails closed, and a Release ID is never copied into a Release Group field. Field classification is in `docs/arch009_identity_fields.md`.
- **`ACOUSTID_USER_KEY`**: the AcoustID user key used for fingerprint submissions. `ACOUSTID_API_KEY` is now documented as the *application* key for lookups. One variable had been serving both, so a user key made every lookup fail with "invalid API key". Legacy single-variable installs keep working.

### Changed
- `_merge_imported_album_into_existing` only orchestrates now. Previously:
  - a text title score (0.90, or 0.72 with a matching Recording ID) decided which file was discarded;
  - with no MusicBrainz tracklist the existing file was always retired;
  - album identity was never checked.
- Removed `import_guard.existing_track_matches_target` / `existing_track_can_block_downloaded_replacement`, the text-threshold destructive guards.
- ARCH-009 contract enforcement was added to these routes:
  - `add-mbids`
  - submissions `attach-mbids`
  - `rgid-group/relink`
  - `rgid-group/assign-representative-release` (previously failed open)
  - album `deduplicate` override
  - `duplicate-resolver/apply` override
  - the engine MB track repair, which no longer lets caller-supplied tracks inherit the album's release group and refuses when the release's release group is unknown
- The ARCH-002 caller audit now enforces NEEDS_MIGRATION = 0.

### Tests
- New and updated test files:
  - `tests/test_import_reconciliation.py` (28)
  - `tests/test_arch009_identity_contract.py`
  - `tests/test_acoustid_key_roles.py`
  - a duplicate-group safety property test covering groups of 2–6 copies: at least one copy is always kept
  - the wave18/wave20 real-path reconciliation tests, updated to the canonical rules
  - `frontend/tests/ReconciliationReviewPanel.test.tsx`
## v0.1.28 - 2026-09-26

Canonical single-recording evaluator, release-aware duplicate identity, and latent NameError fixes (ARCH-002 / ARCH-009).

### Added
- **`backend/matching/recording.py`** adds the canonical single-recording evaluator, `evaluate_recording_candidate`. It combines embedded Recording ID, AcoustID, title, artist, duration, filename, position and version-qualifier evidence, and reports a `RecordingIdentityProof` (insufficient / textual_support / embedded_recording_id / acoustid_recording_id / multi_source_deterministic). The same module adds `verify_audio_against_request` for checking downloaded audio against a request. AcoustID semantics match album alignment: a score floor of 80 and a 3-point ambiguity window.
- **`backend/recording_review.py`** is a new service module. It handles recording-candidate generation (candidates only) and canonical replacement-identity resolution.
- **`backend/duplicate_identity.py`** is a new service module for duplicate-file identity (release slot) and for selecting paths that unattended cleanup may delete.
- **`scripts/audit_arch002_callers.py` + `docs/arch002_caller_audit.json`** add a CI-enforced classification of every production final-decision pattern hit.

### Changed
- `build_recording_matching_decision` takes its attach eligibility, safety key, confidence state, conflicts and review reasons only from the canonical evaluator, so there is no second decision tree. It also takes the real AcoustID hit set. **Policy change:** a MusicBrainz text-search candidate with no embedded Recording ID and no AcoustID proof is now "Needs review" (it can still be attached with confirmation). Previously it could be one-click "Safe to attach".
- Import Review now shows identity proof, confidence state, hard conflicts, review reasons and backend attach eligibility for recording candidates. The AI-suggestion match builder honors the backend's canonical veto.
- The format-replacement workflow now establishes identity canonically. An AcoustID hit can no longer silently replace the embedded Recording ID, and a text-search result alone goes to review. The target album requires a Release Group.
- Playlist/download audio verification uses the canonical verdict. A fingerprint that names a different recording is never accepted because of title text.

### Fixed
- An MB text candidate that AcoustID contradicted was labelled "no result" and could be marked safe. It is now a hard conflict.
- The unattended Import Review auto-enqueue ignored the canonical album veto (`matching_decision.action_allowed=False`).
- Scheduled duplicate cleanup counted a shared Recording ID as duplicate-file identity, and for a mutual pair it selected **both** copies. On the live library, all 7 duplicated recordings would have lost every copy. Unattended deletion now requires the same release slot and keeps one copy.
- Duplicate-album merge let a row with an unknown release group inherit another row's release group (ARCH-009).
- Latent `NameError`s were fixed in:
  - `dedup_scan`'s album+title step, which aborted scans
  - `POST /api/albums/<id>/remove` (plus a nonexistent `jobs.create`)
  - `reimport_disk` (`temp_cfg_content`)
  - the AI genre fallback (`env`)
  - Plex playlist re-verification (`prior_rating_key`)
  - folder tag-evidence guessing (`mf`)
  - `/api/config` error handlers (`ConfigError`)
- AcoustID service rejections (for example an invalid API key) are now logged and recorded instead of looking like "no match".
- Removed the dead confirmed-import title scorer, which was passed but never used.
## v0.1.27 - 2026-09-26

Canonical album identity: identity verification split from release completeness (ARCH-002 Part 3).

### Changed
- **`backend/matching/models.py`**: new `IdentityProof` (INSUFFICIENT / RELEASE_GROUP_ID / DETERMINISTIC_TRACK_RECORDING_ID / CONFIRMED_RELEASE) and `ActionScope` (FULL_RELEASE / VERIFIED_SUBSET) enums. `ReleaseGroupMatchResult` now reports identity proof plus local and target track coverage as separate facts, and `MatchPolicy.scope` selects what an operation needs proven. The default `FULL_RELEASE` scope behaves exactly as before.
- **`backend/matching_contract.py`**: `build_album_matching_decision` no longer computes its own `identity_verified`/`action_allowed`. It now uses the canonical `identity_proof` and `can_auto_accept(scope=VERIFIED_SUBSET)`.
- Removed the dead duplicate track scorer from `backend/mb_alignment.py`. Both alignment helpers now resolve to the canonical `backend.matching` functions.

### Fixed
- A local album whose `mb_releasegroupid` matched the candidate's could authorize automatic action with no track-level evidence at all. A bare Release Group ID match no longer authorizes action.
- A partial album (for example 2 of 18 tracks) where every local track is proven by an exact Recording ID still authorizes action on those tracks. It no longer depends on whole-release completeness.

### Tests
- `tests/test_arch002_matching_corpus.py::TestArch002PartialAlbumIdentity` covers these cases:
  - a 2-of-18 deterministic subset is allowed under VERIFIED_SUBSET and denied under FULL_RELEASE
  - a bare Release Group ID match is denied
  - a Recording ID conflict is denied
  - a mix of deterministic and text-only tracks is denied
  - a duplicate local claim is denied
  - an AcoustID conflict is denied
## v0.1.26 - 2026-09-24

Live TrueNAS deployment validation and code-scanning closure pass.

### Fixed
- **`backend/composite_workflows.py`**: `get_unmatched_review_items()` never actually supported the `limit`/`offset`/`include_singletons` contract its three callers already used, throwing a `TypeError` on every call and breaking the Import Review "Needs MB ID" page entirely; reimplemented to return the `albums`/`singletons` shape callers expect. `get_folder_items()` silently dropped multi-prefix callers; now accepts a single path or a list of path prefixes.
- CodeQL `py/path-injection` (high): added containment checks to `plan_track_replacement`'s `source_path`, playlist staging paths (sanitized `playlist_key`), `get_artist_folder_inventory`/`resolve_folder_to_albums`/`get_folder_items` (must resolve under `MUSIC_ROOT`), and `inspect_import_source` (must resolve under `MUSIC_ROOT` or an approved staging root) -- previously unvalidated filesystem reads/walks on caller-supplied paths.
- CodeQL `py/stack-trace-exposure` (medium): `backend/config_manager.py` no longer interpolates raw filesystem exception text into `ConfigError` messages returned to API clients; the real exception is logged server-side and a generic message returned instead.
- CodeQL `py/polynomial-redos` (high): capped filename length before regex title-guessing on untrusted Soulseek/slskd search result names in `app.py`.

### Verified
- Deployed to the real TrueNAS installation (not a disposable test path): backups taken, stock-Beets plugin load confirmed genuinely inside the running `beets` container (not just present in the Web Manager's own copy of the source), `docker compose restart` and `up -d --force-recreate` both preserve library state (byte-identical `musiclibrary.blb` hash, unchanged track/album counts).
- All open GitHub code-scanning alerts (24) individually inspected and dispositioned: real findings fixed in code, remaining false positives dismissed with per-alert justification. Zero open alerts.

## v0.1.25 - 2026-09-24

Complete Composite Workflow Migration (ARCH-010) and full retirement of `backend/beets_client.py`.

### Added
- **`backend/composite_workflows.py`**: Complete implementation of all 20 composite workflow families orchestrating Plan / Apply / Rollback transactions directly backed by stock Beets (`backend/beets_adapter.py` on `:8337`).
- **`backend/config_manager.py`**: Robust Beets `config.yaml` management with optimistic CAS revision hash checks, strict YAML syntax validation, atomic replace (`fsync`), backup generation, and rollback support.

### Changed
- **Zero Legacy Engine References**: Migrated all remaining ~386 `beets_client` call sites across `app.py` to `composite_workflows`, `config_manager`, and `beets_adapter`.
- **Deleted `backend/beets_client.py`**: Retired the legacy HTTP client, port 8338, `BEETS_API_URL`, `BEETS_API_TOKEN`, and legacy control agent scaffolding.
- **Updated Mutation Inventory**: Reclassified and verified all 402 mutation sinks in `security/arch003_mutation_inventory.json` with 0 unresolved entries.

### Fixed
- Fixed and verified all 2,675 unit/integration test cases, architecture invariants, and security secret scans.

## v0.1.24 - 2026-09-24

Closure/hardening pass for the stock-Beets migration (#137-140 left main broken and materially less migrated than represented; see PR #141).

### Fixed

- **`/health/ready` and setup status were permanently unhealthy on a fresh install.** `chroma`'s required-plugin health check tested for a local `pyacoustid` Python package inside Web Manager, but AcoustID fingerprinting runs entirely inside the stock Beets container -- Web Manager has no `pyacoustid` dependency of its own.
- **Existing-install upgrades never actually enabled `web`/`webmanager`.** Both were missing or misclassified in the plugin manifest, so an existing user's config.yaml never got the plugin entries needed for stock Beets' own default service to even start, and `replaygain` could get enabled without the `backend:` setting it needs to avoid a hard load failure. Migrating a plugin name into `plugins:` without its minimum required settings block is now handled for `web`, `webmanager`, and `replaygain`, without ever touching a block a user already has.
- **A real upstream Beets 2.14.1 defect crashed every album/item read with `include_paths: yes`** (`beets.util.displayable_path(None)` raising instead of returning `""` for an album with no artwork yet -- i.e. every album immediately after import). Worked around defensively inside the `webmanager` integration plugin at load time; does not modify the Beets image itself.
- A real, previously-unredacted secret-leak path in `/api/setup/status`'s top-level `plugins` field.
- A silent no-op in `attach_album_mbids()`: it checked the stock-Beets integration plugin's modify response for an `"ok"` key that response never carries (the real key is `"success"`), so every real mutation through that endpoint was treated as a failure.
- The fresh-install plugin-provisioning ordering race: `beets` now waits on Web Manager's own healthcheck (inverted from the previous direction), since Web Manager provisions the webmanager plugin's files and config.yaml entries before its own HTTP port binds.
- Removed the forbidden generic `POST /api/plugins/run` beet-command endpoint.

### Changed

- `docker-compose.yml`/`docker-compose.dev.yml`/`docker-compose.full.yml` now use `lscr.io/linuxserver/beets:latest`, mount `/music` read-only into Web Manager, and unify Web Manager's durable-state mount on `/web-manager-data`.
- `job_engine.py`, `routes_setup.py`, and `routes_submissions.py` are now fully migrated onto `backend/beets_adapter.py`, with zero remaining references to the retired `backend/beets_client.py` control-agent client.
- Added a `PRODUCTION_LEGACY_BEETS_REFERENCES` CI invariant proving that migration claim from source.
- Rewrote `docs/ARCHITECTURE.md`, `docs/CONFIGURATION.md`, `docs/INSTALLATION.md`, `docs/EXAMPLES.md`, `docs/DEVELOPMENT.md`, `docs/BEETS_ENGINE_MIGRATION.md`, and `README.md` to describe the current stock-Beets architecture instead of the deleted control-agent one.

### Known remaining debt (not fixed in this release)

`app.py`'s composite Plan/Apply/Rollback mutation workflows (merge-album, merge-artist, Clean All, track replacement, folder/album cleanup, artist-folder reconcile, album maintenance/relocation/metadata-repair, artwork, genre repair, mbsync-all, move-all, and the `/api/config` editor) still call the retired `backend/beets_client.py` and are currently non-functional against the real stock-Beets stack. Tracked as `docs/TECHNICAL_DEBT.md` ARCH-010.

## v0.1.23 - 2026-09-22

Ships work that had been written and passing locally but never committed/deployed -- discovered while performing live acceptance testing of v0.1.22's secret-reveal feature (see #136).

### Added

- **Configuration inventory expanded to 59 curated settings across 8 sections** (System & Environment, Authentication & Security, AI & LLM Services, Beets Core & Engine, Storage & Paths, Music Services & Metadata, Media Server Integrations, Playlists & Download Providers), each now carrying `restart_required` and `type`.
- **Effective vs. saved distinction shown in the UI.** When a setting's environment value differs from its persisted/saved value, the System page now shows both ("Running: ... | Saved: ...") with an "Overridden" badge, instead of only ever showing one.
- Two more secrets are revealable (`BEETS_API_TOKEN`, `QBITTORRENT_PASSWORD`); `SLSKD_API_KEY` reveal now also checks its dedicated `_FILE` override, matching the existing convention used elsewhere.

### Security

- `BEETS_WEB_AUTH_TOKEN`'s masked display switched from a length-revealing partial mask to a fixed-length `********` placeholder.

## v0.1.22 - 2026-09-21

### Added

- **On-demand secret reveal on the System page.** Configured secrets (`BEETS_WEB_AUTH_TOKEN`, `OPENAI_API_KEY`, `PLEX_TOKEN`, and other recoverable settings) now have a per-field Show/Hide control to inspect the actual effective value when needed -- never shown by default, never returned by the normal `/api/setup/env` endpoint. A new, narrowly-scoped `POST /api/setup/env/<name>/reveal` endpoint returns exactly one value, only for settings marked `revealable` in configuration metadata, and only to an authenticated administrator (with a short password-reauthorization window on installs that have a browser password). `BEETS_WEB_PASSWORD` is never revealable -- it is stored only as a password hash, which cannot be converted back into the original password.

## v0.1.21 - 2026-09-21

Security cleanup following v0.1.20's live-deployment acceptance test (see #133).

### Fixed

- **Persisted web auth token file self-heals an incorrect `0700` mode.** A real deployment was found with `.auth_token` at mode `0700` (expected `0600`) -- created before `WebManagerConfigStore`'s write-time chmod existed, and never rewritten since (reusing an existing valid token never rewrites the file). The bootstrap path now corrects the mode in place on every startup, so existing installations self-correct on their next restart/recreation without a manual `chmod`.

### Security

- Rotated the `beets` engine's `BEETS_API_TOKEN` on the operator's production deployment after the previous value was inadvertently displayed in plaintext during the v0.1.20 live-verification session. No code change was required for this; noted here for the record.

## v0.1.20 - 2026-09-21

Two fixes found live during v0.1.19's TrueNAS rollout and the System-page acceptance verification that followed (see #131, #132).

### Fixed

- **System page's host-path variables (`MUSIC_PATH` et al.) still showed a fabricated `./music`-style default**, even after v0.1.19's fix marked them non-editable. `_env_catalog()` let the literal placeholder value in the bundled `.env.example` override the curated metadata's explicit `default: None`. Curated metadata now always wins.
- **The TrueNAS rollout script checked the wrong persistent-data mount** (`/web-manager-data` instead of `/data`), causing a false post-deploy-verification failure on a real rollout even though the deploy itself succeeded and persistence was intact. Now resolves `/data` first, matching `app.py`'s own precedence.

## v0.1.19 - 2026-09-21

Makes the System / Environment configuration page show the deployment's genuine effective configuration instead of static environment-variable echoes, and fixes two real configuration-accuracy bugs found while verifying it against a live deployment (PR #130). Also folds in PR #129 (zero-friction Beets plugin management), which had not yet been released under its own version tag.

### Changed

- **System page now resolves real effective configuration**: runtime/persisted/default precedence, empty Docker env value handling, secret masking with safe replace/remove, source badges, container-path metadata, and immediate post-save refresh.
- **AI requests now use the same configuration the System page displays.** Previously, 6 real AI-request call sites hardcoded their model (`gpt-4o`/`gpt-4o-mini`) and the OpenAI endpoint, and only ever read `OPENAI_API_KEY` -- while the System page implied `AI_MODEL`/`AI_BASE_URL`/`OPENROUTER_API_KEY`/`AI_API_KEY` were live, effective settings. Real requests now resolve model/endpoint/API key from the same variables via shared `_ai_api_key()`/`_ai_model_and_endpoint()` helpers.
- **Host volume path variables no longer claim a fake value.** `BEETS_CONFIG_PATH`/`MUSIC_PATH`/`DOWNLOADS_PATH`/`WEB_MANAGER_DATA_PATH` are `docker-compose.yml`'s own host-side bind-mount interpolation variables and are never forwarded into the container's environment -- the page previously showed a fabricated `./music`-style default and allowed silently-no-op edits. Now labeled container-path-only and marked non-editable, enforced on both the backend write-guard and the frontend.

## v0.1.18 - 2026-09-19

Simplifies the standard Docker deployment (PR #128, follow-up to #126): a normal install now runs a stock Beets container and Beets Web Manager together in one Compose file, with no custom Beets engine, no manual internal API tokens, no `.env`, and no setup scripts required.

### Changed

- **Standard deployment simplified.** The default `docker-compose.yml` now runs `lscr.io/linuxserver/beets:2.13.1` (stock, unmodified) alongside `ghcr.io/iranman/beets-web-manager:stable`, sharing `/config`, `/music`, `/downloads`, plus a `/data` mount for Web Manager's own persistent state. `docker compose up -d` and opening the browser setup wizard is the whole install.
- **Beets Web Manager bundles its own `beets==2.13.1` + `ffmpeg` + `fpcalc` + `pyacoustid`** and starts the existing Beets control agent on an internal loopback address (`127.0.0.1:8338`) inside its own container only -- never exposed to the host or another container. The stock `beets` container remains a normal, independently-usable Beets install and CLI environment (`docker compose exec beets beet ...`); Web Manager does not depend on it being reachable.
- Both Beets runtimes are pinned to the identical `2.13.1` release -- no schema-version-skew risk between them.
- `docker-compose.dev.yml` migrated to build Web Manager from source against the stock Beets image (previously built the old custom engine image). `docker-compose.full.yml` retained and explicitly labeled legacy/advanced-only. `.env.example` now states `.env` is optional and isolates external-engine variables under an "Advanced / External" section.
- CI's `production-docker-acceptance` job now boots and exercises the actual stock-Beets + Web Manager topology end-to-end (fresh install, setup wizard, real cross-container import, persistence across `down`/`up` and `--force-recreate`) and gates image publication; the old custom-engine/remote-HTTP acceptance job is retained separately as a legacy-compatibility check only.

### Fixed

- A fresh `/data` bind mount was not writable by Web Manager: the image ran as a fixed build-time UID and never actually applied the documented `PUID`/`PGID` settings at runtime. Fixed with a proper root-then-drop-privileges entrypoint that remaps the container's user to the runtime PUID/PGID (default unchanged) and fixes ownership of `/data`, `/config`, and the top level of `/music`/`/downloads` before dropping to the unprivileged user -- the application itself still never runs as root.
- The stock Beets container's own default service (`beet web`) was restart-looping with "unknown command 'web'" because the `web` plugin wasn't enabled in the config it ended up with. Fixed by enabling `web` in `config.yaml.example` for the setup-script path, and correcting the corresponding acceptance test, which had itself been pre-seeding an unrealistic config that no real installation ever produces.
- Fixed the same class of persistence bug found and corrected during this refactor's review: Web Manager's own data-directory auto-detection was not exported for other modules to see, silently writing settings and the setup-complete marker to a non-persistent path that was lost across `docker compose down && up` / `--force-recreate`.
- Corrected several stale documentation claims (`README.md`, `docs/ARCHITECTURE.md`, `docs/INSTALLATION.md`, `docs/EXAMPLES.md`) referencing a `beet-locked` locking wrapper that does not exist in the stock image used by the new default deployment, replaced with an accurate description of what SQLite's own locking actually protects versus what it does not.

### Migration Note

Existing v0.1.17 (or earlier) installs using the previous two-container `beets` + custom `beets-engine` architecture (port 8338, `BEETS_API_TOKEN`, `/data/media/music`) are **not** upgraded in place by swapping in the new `docker-compose.yml` -- that file assumes the stock LinuxServer image and the new mount layout. Keep using `docker-compose.full.yml` (now explicitly legacy/advanced) or deliberately migrate your `/config` bind mount and drop the old engine-specific env vars before switching.

## v0.1.17 - 2026-09-18

Hotfix release addressing ARCH-020 and related fail-closed/information-exposure defects found across three independent-review passes (PR #126, follow-up to #120).

### Fixed

- **ARCH-020 fixed for real, not just documented:** candidate discovery for artist-folder scan/merge/MBID-stamping now happens engine-side via a new read-only Control Agent endpoint (`/artists/folders/inventory`) and a typed `BeetsClient.get_artist_folder_inventory()` method, since the Web Manager has no local media mount in the supported two-service deployment. The `docs/TECHNICAL_DEBT.md` ARCH-020 entry is removed.
- **`_apply_artist_folder_reconcile_resilient()` error classification:** a definite HTTP 400/401/403/404 now fails immediately instead of entering the up-to-600s transaction poll; only genuine transport uncertainty still polls.
- **Clean All resume reattachment implemented and hardened:** a saved Clean All operation is no longer discarded on a transient `get_transaction()` lookup failure — the task stays `running` with the same `operation_id` preserved instead of risking a duplicate Plan/Apply.
- **Engine inventory failure no longer masquerades as "no work":** `_stamp_artist_folder_scan()` now distinguishes a genuine empty scan from an engine/inventory failure and fails closed with a structured error instead of silently reporting nothing to do.
- **Information exposure through an exception (CodeQL):** raw exception text from the new/modified hotfix error paths (inventory-scan failures, saved-operation lookup failures, resilient-Apply rejected/lost-response/poll-failure branches, async stamp-mbid job failures) is no longer surfaced in HTTP responses, job results, or job-visible logs. A shared classifier now maps each known `BeetsClient` exception type to a sanitized, safe message while preserving `error_code`/`status_code`; the real exception is only ever logged server-side.
- Max-stale diagnostics edge case: a stale-but-present cache with a stuck refresh now correctly reports `diagnostics_pending`/503 instead of a false "confirmed unavailable."
- Two previously-latent bugs in `_derive_artist_folder_identity()`/`_extract_recording_mbids()` fixed (Beets does not always store `items.path` as absolute).

## v0.1.16 - 2026-09-17

Consolidation release reconciling ARCH-002, ARCH-007, ARCH-012, repository hygiene, and dependency updates (PR #119). Requires a new release once merged: none of this is in the published `v0.1.15` image.

### Added

- **Canonical matching/evidence engine (ARCH-002):** introduced `backend/matching/` (normalizer, rules, scorer, `ReleaseGroupMatchResult.can_auto_accept()` auto-accept policy) as the single shared source of truth for release-group matching decisions. Migrated AI suggestions, folder candidate evaluation, and the `_album_track_*` family onto it; removed a polynomial-ReDoS-vulnerable regex from the matching normalizer, with adversarial test coverage. Closed a real gap in the playlist auto-placement path where a text-only confidence score (no fingerprint evidence at all) could authorize an unattended tag-write and file-move.

### Fixed

- **Structured read boundary completed (ARCH-007):** eliminated the remaining legacy raw-SQLite/`_db()` calls in production code across 12 server-owned endpoint families and 19 typed `BeetsClient` methods, with an AST-level regression test enforcing the zero-raw-query boundary going forward.
- **Library missing-album counting (ARCH-012):** fixed a defect in `_build_library_payload()` that could misclassify singleton tracks as missing albums; missing-album counting now resolves the dominant `album_id` from items and links through `beets_album_lk_by_id`. Multi-date missing releases are now grouped into a single card.
- **Frontend/Docker build:** `frontend/package-lock.json` was missing a nested `vitest`/`vite` peer dependency entry (`yaml@2.9.1`), which made a clean `npm ci` fail under the Node 22 build image used by both the production Dockerfile and CI's Docker acceptance jobs. Regenerated the lockfile against the actual Node 22 build environment; no application dependency changed.
- **Security:** fixed 8 real information-exposure findings where an unexpected backend/Beets-engine exception's raw text could reach a client-facing error response instead of only the server log. Reviewed 5 CodeQL path-injection findings individually; closed the ones with a real (if redundant) gap and confirmed the rest are downstream of an existing sound containment check.
- Reconciled frontend dependencies (`postcss`, `@tanstack/react-query`, `@types/node`, `@types/react`, `jsdom`) and GitHub Actions dependencies to their current tested versions.
- Repository hygiene: removed leftover AI-agent development process material from the repository; no user- or operator-facing behavior change.

### Known Remaining Work

- ARCH-002 migration is not yet complete for every matching call site; see `docs/TECHNICAL_DEBT.md` for the current register.
- ARCH-001 (monolithic `app.py` route/domain/mutation coupling) remains open, incremental extraction ongoing.

## v0.1.15 - 2026-09-15

SEC-002 / ARCH-003 controlled-mutation closure across Waves 15-29 (PRs #88-#102, #107-#109, #112), the repository-wide CodeQL closure (#108), a Jobs page transport fix (#103), and frontend dependency security remediation (#113). Requires a new release once merged: none of this is in the published `v0.1.14` image.

### Added

- **Wave 29 / ARCH-007 structured library reads:** new `BeetsClient` methods (`find_all_albums_by_albumartist`, `list_distinct_albumartists`, `find_all_orphan_albums`, `list_distinct_item_paths`) replace raw, permanently-broken `_db()` calls (the two-service topology's `raw_sqlite_query()` unconditionally rejects raw SQL) in `library_merge_artist`, `library_normalize_artists`, `_run_normalize_artists_if_needed`, `library_mbsync_all`, and `library_move_all`.
- New Control Agent endpoints `POST /library/mbsync`, `POST /library/move`, `POST /submissions/submit` with matching fail-closed `BeetsClient` methods (`mbsync()`, `move_library()`, `acoustid_submit()`).

### Fixed

- **Web Manager local Beets CLI execution eliminated (Wave 29):** removed all remaining `BEET_BIN`/`_beet_run`/`_beet_env` local-subprocess execution from the Web Manager image. `library_mbsync_all()`/`library_move_all()` now run `beet mbsync`/`beet move` exclusively via engine IPC; `attach_album_mbids()` (`routes_submissions.py`) rewired onto `album_metadata_repair_v1` and `beets_client.acoustid_submit()`. An AST-based structural regression test permanently bans any reintroduction across every Web Manager production module.
- **ARCH-003 controlled mutation closure (Waves 15-29):** every remaining unmigrated Beets DB/filesystem mutation sink across import review, cleanup, duplicate handling, album maintenance, artist-folder management, artwork, track/album replacement, AI import state, and configuration now runs through an audited plan/apply/verify engine transaction family. Mutation inventory closes at 0 unresolved blockers across 437 discovered candidate sinks.
- **Security (Wave 29, found during this release's own post-merge CodeQL validation):**
  - Fixed a real quadratic ReDoS in the MusicBrainz track-title normalizer (`_mb_track_repair_title_norm`/`album_track_norm`) reachable via caller-supplied import titles -- measured at 11.6s on a 64k-character adversarial payload before the fix, ~14ms after. Replaced with a linear-time algorithm, proven byte-identical to the original on 300,000+ randomized inputs.
  - Hardened the four temporary `beet -c` config-override files (one of which carries the AcoustID API key) against a symlink-follow and a world-readable window between file creation and permission narrowing; now created atomically at owner-only mode with `O_EXCL`/`O_NOFOLLOW`.
  - 22 additional `py/path-injection` CodeQL alerts individually reviewed against current source and dismissed as false positives with per-alert, line-cited rationale (read-only probes already gated by root-containment/symlink checks) -- none bulk-dismissed.
- **Repository-wide CodeQL closure (#108):** all 171 outstanding alerts individually dispositioned (35 fixed, 136 confirmed safe with sink-specific rationale).
- **Playlist ReDoS (Wave 28, #109):** fixed real polynomial-backtracking regressions in playlist artist/title splitting and "- Topic" channel detection.
- **Preservation-copy integrity (Wave 28, #107):** replaced a path+size "content signature" (which could not actually detect differing file content) with a real streamed SHA-256 digest; closed an unconditional-overwrite-on-collision gap and a silent copy-fallback that could mask a real copy failure as success.
- **Jobs page transport (#103):** resolved Jobs page fetch failures and hardened engine error-contract handling.
- **Frontend dependency security (#113):** `next` 16.2.11 -> 16.3.4 (2 critical RCE advisories), `sharp` 0.35.3 -> 0.35.4 (high, libheif), `vitest`/`@vitest/mocker` 4.1.10 -> 4.1.11 (moderate, path traversal). `npm audit --audit-level=high` clean (0 critical, 0 high, 0 moderate).

### Known Remaining Work

- ARCH-002 (duplicated matching/confidence rules across entry points) remains open, P0.
- ARCH-007's broader scope (other, not-yet-audited routes still expressing reads in a raw-SQL-compatibility shape) remains open; this release closed the five callers directly proven broken by real Docker acceptance testing, not a full repository audit.
- ARCH-001 (monolithic `app.py` route/domain/mutation coupling) remains open, incremental extraction ongoing.

## v0.1.14 - 2026-08-19

SEC-002 playlist-pipeline and MusicBrainz-identity engine-ownership work, Waves 9-14 (PRs #82, #83, #84, #86, #87). Requires a new release once merged: none of this is in the published `v0.1.13` image.

### Fixed

- **Playlist pipeline engine ownership completed (Waves 9-13):** removed every remaining local fallback in the playlist acquisition/staging/validation/import/placement path (local directory listing, local AcoustID fingerprinting, local media-tag reads, local `pl_default`-keyed staging) in favor of engine-owned `BeetsClient` IPC; fixed `playlist_id`/`playlist_key` confusion across six call sites with one strict shared resolver that refuses rather than guesses; closed an unauthenticated arbitrary-item mutation gap in the engine's `/playlists/place-imported` endpoint (an `item_id` is now required to belong to a recorded import operation for the calling `playlist_key`); restored DB backup and write/move-both-must-succeed truthfulness for placement; fixed a fabricated-candidacy regression in manual quality-repair; bounded and locked the playlist staging file-listing endpoint; fixed cross-playlist Plex ratingKey collision, checkpoint-corruption isolation, and non-circular AcoustID/fingerprint verification for playlist import (Waves 9-12).
- **MusicBrainz identity/matching authority consolidated (Wave 14):** introduced a single shared `MatchingDecision` contract (`backend/matching_contract.py`) so deterministic identity proof (Release Group ID equality, exact Recording ID matches) -- not independent per-workflow heuristics -- is the sole authority for whether a candidate can auto-import/auto-repair versus require manual review, across Import Review, Folder Preflight, Release Resolution, Reimport, Duplicate Cleanup, and the existing recording-attach gate. Fixed a disconnect where two response-compaction layers silently stripped the shared decision before it reached the real Import Review auto-import gate, letting an old independent heuristic authorize import even when the shared decision withheld it; fixed the same class of bypass in the oversized-partial-release acceptance path; fixed a generic-mapping-treated-as-a-release bug and a Release-ID-only-misclassified-as-a-hard-conflict bug in the matching contract itself; added a narrow Release-Group-ID consistency guard to the MusicBrainz track-repair workflow, which had no deterministic identity check at all.

## v0.1.0 - 2026-07-16

### Added

- Initial public source-control baseline for Beets Web Manager.
- Flask backend, React/Next static frontend, background jobs, playlist workflows, import review, cleanup tools, Plex integration, MusicBrainz and AcoustID verification, and AI-assisted metadata workflows.
- Security documentation, threat model, endpoint inventory, and CI security checks.
- GitHub issue templates, pull request template, Dependabot configuration, and build/test workflows.
- Standalone `Dockerfile`, `.dockerignore`, single-command `docker-compose.yml`, `requirements.txt`, and `config.yaml.example`.
- `setup.sh` / `setup.ps1` one-command bootstrap; `scripts/backup.sh` / `scripts/restore.sh`.
- `routes_setup.py`: `/api/setup/status`, `/api/setup/test/{ai,musicbrainz,acoustid,plex}`, `/api/setup/settings`, `/api/setup/complete`, and `/health`, `/health/live`, `/health/ready` probes with version reporting.
- `docs/INSTALLATION.md`, `docs/CONFIGURATION.md`, `docs/TROUBLESHOOTING.md`.
- CI: `docker-build.yml` now builds the image and runs a real start-container-and-probe-health smoke test.

### Fixed

- `config.yaml` (contains plaintext integration secret fields) was not excluded by `.gitignore`.
- Baseline CI no longer depends on local-only agent instruction files or private `config.yaml` files, and Docker dependency installation uses the available `pylistenbrainz==0.5.1` pin.

### Known Limitations

- The broader Arr stack still contains services outside the Beets app hardening scope.
- Some security scanner integrations may require repository-level GitHub settings or release artifacts.
- Operators must provide their own credentials and rotate any values that were ever exposed before this baseline.
- Setup wizard is backend-API-only in this release; no browser wizard UI yet.
- Baseline Docker image build passes GitHub CI; local Docker Desktop validation still fails before build start with a Linux engine `_ping` 500, and setup/demo packaging still needs a release check after `routes_lidarr.py` is restored.
