# Changelog

All notable changes to this project will be documented in this file.

The project uses Semantic Versioning.

## Unreleased

### Added

- `scripts/deploy_truenas_web_manager_0_1_3.sh`: guarded, fail-closed TrueNAS rollout for v0.1.3 -- dynamic mount discovery, authoritative/stale database safety checks (checksum/inode/quick_check, never a wildcard, never `rm`), guarded auth-token migration, timestamped backup, pinned-image + label verification, endpoint verification, and `--dry-run`/`--rollback` modes. See `docs/operations/TRUENAS_ROLLOUT_0_1_3.md`.
- `tests/test_deploy_truenas_rollout.py`, `tests/deploy/fake_docker.py`, `tests/deploy/fake_curl.py`: rollout-script test suite against real temp SQLite files and a fake Docker/Compose/curl world (no real Docker daemon or TrueNAS host required).

### Fixed

- Issue #14: fresh Docker installs now use a coherent Beets/plugin dependency set, load bundled plugins from `/app/beetsplug`, keep optional Discogs non-interactive until configured, and initialize ReplayGain with the installed `ffmpeg` backend.
- `_persist_generated_auth_token` (app.py) now writes the token via a proper write-fsync-chmod-atomic-rename sequence and cleans up its temp file on any failure, instead of a redundant `chmod` on a destination that may not exist yet and a silently swallowed failure path.
- `/api/setup/status` cache now uses a monotonic clock, single-flights concurrent rebuilds (no cache-stampede thundering herd against the control agent), serves a bounded, explicitly-labeled `stale: true` snapshot on rebuild failure instead of silently masking or serving forever, and is invalidated after `/api/setup/env`, `/api/setup/auth-token/regenerate`, and `/api/setup/complete`. `/api/setup/diagnostics` now actually bypasses the cache (previously called straight through to the cached path despite its docstring).

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
- Baseline CI no longer depends on local-only `AGENTS.md`, `CLAUDE.md`, or private `config.yaml` files, and Docker dependency installation uses the available `pylistenbrainz==0.5.1` pin.

### Known Limitations

- The broader Arr stack still contains services outside the Beets app hardening scope.
- Some security scanner integrations may require repository-level GitHub settings or release artifacts.
- Operators must provide their own credentials and rotate any values that were ever exposed before this baseline.
- Setup wizard is backend-API-only in this release; no browser wizard UI yet.
- Baseline Docker image build passes GitHub CI; local Docker Desktop validation still fails before build start with a Linux engine `_ping` 500, and setup/demo packaging still needs a release check after `routes_lidarr.py` is restored.
