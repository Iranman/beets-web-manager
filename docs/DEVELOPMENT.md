# Development

Developer setup, project layout, validation commands, and the engineering constraints that apply to any change in this repository.

## Project Layout

- `app.py`: Flask application glue (app creation, request hooks, security headers, static/SPA serving, route-module loading). Workflows live in layered services under `backend/*_service.py` and HTTP handlers in `routes_*.py`; see `docs/arch001_service_decomposition.md`. `python scripts/audit_arch001_ownership.py` checks that no domain code returns to `app.py`.
- `routes_jobs.py`, `routes_lidarr.py`, `routes_setup.py`, `routes_submissions.py`: split route modules for jobs, Lidarr/wanted endpoints, setup/auth/config, and MusicBrainz/AcoustID submissions.
- `job_engine.py`: in-memory `PythonJob`/`JobStore`, structured state, cooperative cancellation.
- `helpers_mb.py`: MusicBrainz and AcoustID helpers. No `app.py` dependency — the strongest current provider boundary.
- `backend/`: `beets_adapter.py` (the only interface to Beets: in-process through `beets_embedded.py` with `BEETS_ENGINE=embedded`, otherwise HTTP to stock Beets' `web`/`webmanager` plugins), `beets_embedded.py` (the embedded Beets engine and its startup self-test, ADR 0006), `beets_plugins.py` (`webmanager` plugin provisioning/health for the legacy two-container mode), `transaction_engine.py` (controlled mutation boundary, pure Web-Manager-local orchestration), `matching/` (canonical matching evidence engine), plus `album_match.py`, `audio_preferences.py`, `import_guard.py`, `mb_alignment.py`, `security.py`, `slskd.py`, `title_normalize.py`. `config_layers.py` (configuration layers, see `docs/CONFIGURATION.md`). The retired `beets_client.py` control-agent client is deleted (ARCH-010).
- `beetsplug/webmanager/`: the Beets operations. Embedded, loaded from the image inside Web Manager; in the legacy mode, provisioned into stock Beets' `/config/beetsplug`.

To run the app with embedded Beets from source, install `requirements.txt` (it pins `beets==2.14.1`), point `BEETSDIR` at a folder with a `config.yaml` whose `directory:` and `library:` are scratch paths, and set `BEETS_ENGINE=embedded`. Never point it at a live library. `tests/test_beets_embedded.py` shows the full environment; `curl -s http://127.0.0.1:8337/api/health` shows the self-test.
- `frontend/src/`: React/Next/TypeScript frontend; `api/client.ts` centralizes API calls and CSRF headers, `api/types.ts` centralizes response shapes, `views/` and `features/` hold pages and workflow panels.
- `tests/`: Python backend tests (`unittest`/`pytest`-compatible). `frontend/` has its own Vitest suite.
- `scripts/`: CI-invoked security/inventory generators and verifiers, plus deployment helpers. See each script's own docstring; anything not referenced by `.github/workflows/` or a test is not part of the supported toolchain.
- `docs/adr/`: Architecture Decision Records for standing product/architecture invariants. See `ARCHITECTURE.md` for a summary and links.

See `docs/ARCHITECTURE.md` for current system shape and intended dependency direction, and `docs/TECHNICAL_DEBT.md` for known open architecture work.

## Local Validation

Run from the repository root before opening a change:

```bash
python -m py_compile app.py helpers_mb.py job_engine.py routes_*.py backend/*.py scripts/security_secret_scan.py scripts/validate_compose_security.py scripts/release_metadata.py
python scripts/run_tests.py          # the unittest suite, one worker per CPU
python scripts/security_secret_scan.py
python scripts/validate_compose_security.py
python scripts/generate_endpoint_inventory.py --check
python scripts/release_metadata.py check
/bin/bash -n scripts/deploy_truenas_web_manager.sh scripts/backup.sh scripts/restore.sh
```

`generate_endpoint_inventory.py --check` fails if `security/endpoint_inventory.json` is stale relative to the route decorators in `app.py`/`routes_jobs.py`/`routes_lidarr.py`/`routes_setup.py`/`routes_submissions.py`. Run it without `--check` to regenerate after adding/removing a route, then fill in any new `"NEEDS_REVIEW"` field by hand before committing.

Frontend, from `frontend/` (Node.js 24 LTS with its bundled npm 11, the same as CI and the Dockerfile; npm 10 rejects lockfiles written by npm 11):

```bash
npm ci
npm run typecheck
npm run lint
npm test
npm run build
npm audit --audit-level=high
```

### Running the backend tests

`scripts/run_tests.py` runs exactly the tests of `python -m unittest discover -s tests -p "test_*.py"` (which still works, single process) in `os.cpu_count()` worker processes, and prints unittest's summary (`Ran N tests ... OK (skipped=K)`) and exit code. On 12 cores the full suite takes about 4 minutes instead of about 21.

- `python scripts/run_tests.py test_name [test_other ...]`: one or a few modules (module names, `tests.test_name` or file paths). This is the quick way to run one module; it sets up the shared suite environment the same way the full run does.
- `-j N`: worker count. `-v`: per-test output, including every skip reason. `--slowest N`: the N modules that used the most worker time (default 10).
- Each worker first imports `tests/test_ai_batch_retry_race.py`, as discover does at collection: it points `BEETSDIR`, the transaction directory and the other state paths at a temp directory and imports `app.py` there. Some modules rely on that and fail under a bare `python -m unittest tests.test_name`.
- Work is handed out longest-first using `scripts/test_durations.json`. A module recorded as slower than `--split-seconds` (30) runs one TestCase class at a time, so `test_deploy_truenas_rollout` (about 9 CPU-minutes) does not set the wall time. After adding or slowing down tests, refresh the file with `python scripts/run_tests.py --save-durations`. A stale file only costs balance, never correctness.
- Memory: about 0.3 GB per worker. Give the container `-m 4g` for 12 workers, or keep `-m 1500m` with `-j 4`.

### Supported environment for the full backend suite

The suite is supported on what CI runs: Python 3.13 with `requirements-dev.txt` installed (it adds the pinned `beets` fixture package), on Linux. No git binary is needed. Some tests drive the Bash rollout script and use POSIX permissions, so on Windows run them in the test image instead of the host Python:

```bash
docker build -f Dockerfile.test -t bwm-test .
docker run --rm -m 4g -v "$PWD:/src" bwm-test                     # full suite, one worker per CPU
docker run --rm -m 1500m -v "$PWD:/src" bwm-test \
  env BEETS_WEB_AUTH_TOKEN=ci-unit-test-token-not-a-real-secret-0000000000 \
  python scripts/run_tests.py test_name                            # one module
```

On Git Bash, prefix the `docker run` lines with `MSYS_NO_PATHCONV=1`. `Dockerfile.test` is test-only (never published). Rebuild it after `requirements*.txt` changes. CI's `compose-verification` job builds it on every PR.

`tests.test_stock_beets_acceptance.StockBeetsDockerAcceptanceTests` and `tests.test_beets_fresh_install_plugins.BeetsFreshInstallStockDockerSmokeTests` boot the real `lscr.io/linuxserver/beets` image pinned in `docker-compose.yml` (any other `STOCK_BEETS_IMAGE` is refused unless `STOCK_BEETS_ALLOW_FLOATING=1` is also set, as only the `beets-latest-canary` workflow does for `latest`), so they need a Docker CLI and daemon. Inside the test image they report `skipped 'Docker CLI not available on host'`. To run them, use host Python with Docker available (`python -m unittest tests.test_stock_beets_acceptance tests.test_beets_fresh_install_plugins`); CI runs both in the `unit-tests` and `security` jobs, the first again in `stock-beets-acceptance`, and the second in the `unit-tests` gated step below, which fails if it skips. The other expected skips in the image need what the image deliberately lacks: `pwsh` (`setup.ps1` test) and root (`chown needs root`, PUID/PGID ownership). CI's `unit-tests` job runs those in its "Root-, pwsh- and Docker-gated tests" step and fails if any of them skips. The `-v` output prints every skip reason. Any error, and any other skip, is a real failure, in particular `app.py could not be imported`, a missing `bash`/`sqlite3`/`ffmpeg`/`curl`, or a missing plugin dependency. Report it; don't work around it.

## Frontend Runtime Shape

The frontend is a React/Next.js static export under `frontend/`. Local development can use the Next dev server, but production is served from the generated `frontend/dist/` artifacts by the Flask deployment. A working local dev-server build is not proof that the deployed app changed — verify against `frontend/dist/` or a real container for anything deployment-relevant.

## Deployment Files Stay Generic

Repository deployment files (`docker-compose.yml`, `docker-compose.full.yml`, `docker-compose.dev.yml`, `.env.example`) are generic product examples only. Never commit a real host's Compose file, host paths, LAN addresses, credentials, private topology, or any single deployer's own configuration — host paths are driven entirely by environment variables. `tests/test_no_owner_specific_deployment_details.py` enforces this structurally (path/address *classes*, not a denylist of specific former values) and is a good template for extending the check if a new deployment file is added.

For the guarded TrueNAS rollout of a specific tagged release (image pin, mount verification, database/token safety checks, backup, rollback), see `docs/TRUENAS_ROLLOUT.md` and `scripts/deploy_truenas_web_manager.sh`.

## Release and Deployment

Releases are container images, never file copies onto a host:

1. Merge the change to `main` with CI green (including CodeQL).
2. A release PR retitles `## Unreleased` in `CHANGELOG.md` to `## vX.Y.Z - YYYY-MM-DD` and bumps `VERSION` (CI's `release-metadata` job checks they agree). The versioning policy (minor vs patch) is in `CONTRIBUTING.md`.
3. Pushing the annotated tag `vX.Y.Z` makes `.github/workflows/docker-build.yml` publish `ghcr.io/iranman/beets-web-manager:X.Y.Z` (labels `org.opencontainers.image.version`/`revision`) and then create the GitHub Release from the CHANGELOG section.
4. Deploy a numbered version: `VERSION` is always an exact `X.Y.Z`, never `latest`/`stable`/`edge`. The Compose file may name `:latest` (the shipped layout); the rollout script then deploys it only after verifying that its version label is `VERSION`. For the maintainer's TrueNAS stack that is `scripts/deploy_truenas_web_manager.sh` (dry run, then the real run; `--rollback <backup dir>` to undo), described in `docs/TRUENAS_ROLLOUT.md`. For a plain Compose install it is the README's Upgrades section.
5. Verify out of band and report each fact separately: the image labels, `/health/live` version, `/api/setup/status` blocking reasons, library counts. A successful build or a healthy container is not proof that the right version is serving.

Never copy raw local backup files, private config, generated caches, or unrelated dirty work into a deployment target.

## Job And Workflow Operations

Long-running operations use the shared job surface (`JobStore`, `PythonJob`, job status endpoints, frontend job polling) rather than ad hoc background threads. New or changed workflows must preserve visible status, cancellation checks, checkpoint/resume behavior, and idempotency — re-running or resuming a job must not create duplicate staging folders, re-download the same track, re-import the same file, repeat a completed mutation, or restart an unbounded retry loop.

## Library Safety

Normal library repairs rely on Beets for library moves and metadata writes. Any rename, move, merge, metadata replacement, deletion, replacement, artwork write, or Beets database update goes through a controlled mutation workflow (`backend/transaction_engine.py`): inspect current state, produce a plan, validate roots/identities/conflicts/preconditions, record before/after diffs and supporting evidence, apply with an audit record, verify final state, and report recovery information on partial failure. Never mark an operation successful before verification.

Direct deletion, copy, or bulk movement under the music library root requires explicit user intent, root validation, preview/audit evidence, and recovery information where technically possible.

## Security Requirements

- Never expose credentials, tokens, cookies, authorization headers, signed URLs, or secret values in logs, API responses, frontend state, fixtures, or committed files.
- Redact raw exceptions and subprocess output before returning them to the browser when they may contain secrets.
- Store runtime credentials in environment variables, approved config files, or Docker secrets — never source files.
- Keep secret scans and outbound-security tests passing (`scripts/security_secret_scan.py`, `scripts/validate_compose_security.py`).

## Testing Requirements

- Add a regression test before fixing a bug when practical.
- Unit tests for domain decisions; contract tests for provider/adaptor behavior; integration tests for workflows crossing Beets, jobs, database state, and filesystem state; focused end-to-end tests for the highest-risk user paths.
- Do not replace a meaningful test with a mock that only asserts implementation details.
- All tests use temporary directories, synthetic audio, fixtures, or static source inspection — never the real music library.

## Definition Of Done

A change is done only when:

- It follows the documented dependency direction (`docs/ARCHITECTURE.md`) or records explicit debt in `docs/TECHNICAL_DEBT.md`.
- Existing behavior is preserved unless the change intentionally alters it.
- Matching/identity rules and mutation safety are not duplicated or weakened.
- Relevant tests and checks pass, or failures are reported with concrete causes.
- Security and secret-handling rules are respected.
- Documentation describes the actual implementation state, not an aspirational one.

## Contribution Workflow

Stay on a feature branch — do not commit directly to `main`. Inspect dirty/uncommitted state before editing. Prefer existing helpers and documented migration paths over introducing a parallel implementation. Keep changes scoped to what was asked; when a change uncovers a larger design issue, record it in `docs/TECHNICAL_DEBT.md` rather than expanding the current change to fix it. See `CONTRIBUTING.md` for the PR/commit-message process and `REVIEW.md` for the review checklist.

## Known Operational Gotchas

- Security or auth/CSP/rate-limit changes can take the app offline if health probes, LAN integrations, and provider connectivity are not checked end-to-end after the change — verify a real deploy, not just a passing build.
- Path-normalization and escaping changes are high risk (filesystem safety checks, playlist path mapping, source/destination validation) — add targeted adversarial tests (traversal, symlink, backslash/separator handling) before relying on a change here.
- When deploying, distinguish build success, file-copy success, backend-restart success, and served-route success as separate, individually verified facts — do not infer any one from another.
