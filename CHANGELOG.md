# Changelog

All notable changes to this project will be documented in this file.

The project uses Semantic Versioning.

## Unreleased

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
