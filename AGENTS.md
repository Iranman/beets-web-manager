# Agent Guide — Beets Web Manager

This is the master prompt for AI coding agents (Claude Code, Codex, and others) working in this repository. `CLAUDE.md` imports this file, so keep the content here and edit only this file.

## What this project is

Beets Web Manager is a self-hosted web UI and workflow layer that runs alongside a **stock** Beets container (`lscr.io/linuxserver/beets`). It handles importing, matching, tagging, cleanup, artwork, playlists, and acquisition. Integrations: MusicBrainz, AcoustID, Lidarr, slskd, Plex, Discogs, ListenBrainz, and an optional AI provider.

**Current goal: public release readiness.** The project is moving from "works on the maintainer's NAS" to "works for anyone who installs it". Two kinds of user need to succeed:

- Experienced self-hosters who already run Beets and want a web UI. Don't limit what they can do or override how they've configured Beets.
- *arr-stack users (Lidarr/Plex/slskd) who are new to Beets. They need clear onboarding, sensible defaults, and error messages that say what to do next.

When a change could go either way, prefer the option that works for an unknown user's setup. Don't build around the maintainer's setup. Never hardcode personal paths, hostnames, or library assumptions. Configuration goes through environment variables or settings, and `.env.example` and `docs/CONFIGURATION.md` stay in sync with it.

## Read before changing code

These docs are the source of truth. This file does not repeat them:

- `docs/ARCHITECTURE.md`: components, data flow, state ownership, and the **Non-Negotiable Rules**. Read that section before any backend change.
- `docs/adr/`: the reasoning behind each rule.
- `docs/TECHNICAL_DEBT.md`: open debt items (ARCH-xxx IDs) and their status. Update it when you close or narrow an item.
- `docs/arch001_service_decomposition.md`: backend layer order. A service imports only lower layers, and nothing under `backend/` imports `app.py`.
- `docs/DEVELOPMENT.md`, `docs/CONFIGURATION.md`, `docs/INSTALLATION.md`, `docs/TROUBLESHOOTING.md`, `docs/TRUENAS_ROLLOUT.md`.

In short, the rules you're most likely to trip over:

1. Web Manager never opens `musiclibrary.blb` and never writes `/music`. All Beets reads and mutations go over HTTP through `backend/beets_adapter.py`.
2. Every library mutation (move, rename, merge, delete, tag write, artwork write) uses the preview/apply/audit/recovery workflow in `backend/transaction_engine.py`.
3. Long-running work uses `job_engine.py`, not ad hoc threads.
4. The release-group ID (`mb_releasegroupid`) is the canonical album identity. Never substitute `mb_albumid` for it.
5. AI is optional and untrusted. It may rank candidates but must never be the source of an identity.
6. Ambiguous evidence goes to the review queue. Destructive actions need stronger evidence than suggestions.

## Scope and autonomy

All of these are in scope: backend (Flask/Python), frontend (Next.js/TypeScript), CI and GitHub workflows, and the deployment stack (compose files, `.env`).

The agent works end to end. It finds the problem, fixes it, tests it, opens a PR, merges it once CI is green, tags a release, and deploys to the maintainer's live instance. When you find a bug, fix it. Don't ask "want me to fix this?" unless the fix is ambiguous or destructive.

**Stop and ask** (as a question with concrete options, never as instructions for the maintainer to carry out) when:

- A step would **delete files, remove library items, or run dedup/merge** against the live library.
- A credential, API key, or account decision is needed.
- Two reasonable product directions conflict and the docs don't settle it.

## Live library safety

The maintainer's real music library is irreplaceable. On the live instance:

- **Back up before any write.** Before a job, deploy, or migration that can write to the Beets DB or media files, record the DB's sha256 and album/item counts, and make a timestamped backup under the stack's `_backups/` folder. Afterwards, check the counts and checksum again and explain any difference.
- **Ask before deletes or dedup**, as above.
- **Unattended dedup/deletion stays opt-in.** It is off by default and turning it on needs the explicit confirmation in `backend/dedup_authorization.py`. Unattended dedup also needs fingerprint or byte proof. Don't weaken either gate. `MUSIC_ROOT` comes from the environment (default `/music`). Keep it configurable and never hardcode a path.
- Verify a change with an out-of-band check: an API call, a DB count, a file listing. A success message in the UI is not proof.

## Git workflow

1. Branch from an up-to-date `origin/main` (`fix/…`, `feat/…`, `docs/…`, `chore/…`). If the clone you're in has someone else's uncommitted work, use a `git worktree` rather than switching branches over it.
2. Commit in small, logical pieces with conventional-commit style messages (`fix(scope): …`).
3. Open a PR so CI runs. When the done-bar below is met, merge it yourself.
4. Never force-push `main` and never rewrite published history.

## Definition of done

A change is finished only when **all** of these hold:

- [ ] **Tests.** New or changed behavior has tests, and the full suites pass:
  - Backend: `python -m unittest discover -s tests -p "test_*.py"` (targeted: `python -m unittest tests.test_name`)
  - Syntax: `python -m py_compile app.py helpers_mb.py job_engine.py routes_*.py backend/*.py`
  - Frontend (in `frontend/`): `npm run typecheck && npm run lint && npm test && npm run build`
- [ ] **CI green, including CodeQL**, with no new alerts. Fix an alert rather than dismissing it. If a dismissal really is correct, write the reason (max 280 characters).
- [ ] **Docs.** `CHANGELOG.md` has an accurate entry under `## Unreleased`. Update `ARCHITECTURE.md`, `TECHNICAL_DEBT.md`, `CONFIGURATION.md`, and `.env.example` whenever the change affects them.
- [ ] **Verified live.** Released and deployed to the maintainer's instance (see below), then checked out-of-band. A docs-only or CI-only change doesn't need a release, so say that explicitly instead.

## Release and deploy

1. Pick the version with the policy in **Versioning** below. In `CHANGELOG.md`, retitle `## Unreleased` to `## vX.Y.Z - YYYY-MM-DD`, bump `VERSION` to `X.Y.Z` in the same PR, and merge to `main`. CI's `release-metadata` job fails if `VERSION` and the newest CHANGELOG heading disagree.
2. Run `git tag -a vX.Y.Z -m "…"` and `git push origin vX.Y.Z`. That triggers `.github/workflows/docker-build.yml`: `release-metadata` checks the tag equals `v` + `VERSION`, `publish-ghcr` publishes `ghcr.io/iranman/beets-web-manager:X.Y.Z`, and `github-release` then creates the GitHub Release with the `## vX.Y.Z` CHANGELOG section as its body. Wait for the whole workflow to finish green, confirm the image's `org.opencontainers.image.{version,revision}` labels match the tag, and confirm the release exists (`gh release view vX.Y.Z`). If `github-release` failed, create it by hand: `python scripts/release_metadata.py notes --tag vX.Y.Z --output notes.md && gh release create vX.Y.Z --verify-tag --title vX.Y.Z --notes-file notes.md`.
3. Deploy with `scripts/deploy_truenas_web_manager.sh` as described in `docs/TRUENAS_ROLLOUT.md`. Always do a `--dry-run` first, then the real run. For rollback, use the same script with `--rollback <backup dir>`; it fails loudly unless the previous image is proven to be running. Read the doc rather than guessing the steps.
4. Recreate only the service you changed. Don't restart unrelated services in the shared compose stack. (The rollout script restarts `beets` itself, only when the webmanager plugin version changed.)

Host-specific details (SSH alias, stack path, live library baseline) are in `CLAUDE.local.md`, which is not committed. If it's missing, ask the maintainer.

### Versioning

The project uses Semantic Versioning. While the version is `0.x`, the MINOR number is the one that signals change an operator must care about:

- **MINOR (`0.Y.0`)** when any of these is true:
  - the CHANGELOG entry has an **Upgrade Notes** section that asks the operator to do something, or something that used to work is now refused (security tightening included);
  - a migration rewrites persisted state or user files (`/web-manager-data`, Beets `config.yaml`, `beetsplug/`);
  - the webmanager plugin's `PLUGIN_VERSION` minor/major or its `PROTOCOL_VERSION` changes (Beets must be restarted);
  - a Compose, environment-variable or mount contract is renamed or removed;
  - a new user-facing feature or endpoint family is added.
- **PATCH (`0.y.Z`)** for bug fixes and dependency or security bumps that need no operator action and no state migration, including plugin patch versions.
- **1.0.0** once fresh-install and upgrade acceptance pass on the public install path and the upgrade-path guarantee is written down.
- Docs-only and CI-only changes need no release; say so in the PR.

When in doubt between two levels, take the higher one.

## Secrets and checks

- Never print, log, commit, or echo a secret, token, or API key, including in command output and PR text. Refer to a key by name, or by the first 8 hex characters of its sha256 hash when you need to show which key is in use.
- Keys are loaded at container start from the stack `.env`, which takes precedence over keys saved in the Settings UI. After a key change, recreate the affected container.
- Never skip hooks (`--no-verify`), disable checks, or loosen a security gate to get a change through. Fix the underlying problem instead.

## Integrations

All of these are release-critical. Each needs tests for success, provider-down, rate-limited, and bad-data responses, plus user-facing docs:

- **MusicBrainz / AcoustID**: core identity. `ACOUSTID_API_KEY` is the *application* key used for lookups. `ACOUSTID_USER_KEY` is the *user* key used for submissions.
- **Lidarr + slskd**: the acquisition pipeline that feeds imports.
- **Plex**: library sync and playlists.
- **Discogs / ListenBrainz / AI provider**: secondary metadata. A failure in any of these must never block MusicBrainz/AcoustID matching.

All outbound HTTP goes through `provider_boundary` (ARCH-006).

## Known quirks

- CI often runs twice per change (once for the push, once for the PR). That's normal.
- The security workflow sometimes segfaults. Re-run it once before investigating.
- Moving code can bring back CodeQL alerts that were dismissed at the old location. Re-check them after a refactor.
- In Git Bash on Windows, paths in `curl`/`docker` arguments get mangled. Prefix the command with `MSYS_NO_PATHCONV=1`.
- `.agents/`, `PROJECT.md`, and similar orchestration scratch files are gitignored working state. Never commit them, and don't treat them as current documentation.

## How to communicate

The maintainer wants **detailed explanations**:

- Say what you changed, **why**, and the trade-offs you considered.
- Say exactly how you verified it (commands, counts, checksums) and what you did not verify.
- Report failures plainly, with the actual output. Never claim something is deployed or passing unless you checked.
- When you need a decision, offer options with a recommendation, and then do the work yourself.
