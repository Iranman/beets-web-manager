# Development

Developer setup, project layout, validation commands, and the engineering constraints that apply to any change in this repository.

## Project Layout

- `app.py`: primary Flask application — routes, import workflows, matching adjudication, and job tracking. Talks to stock Beets only through `backend/beets_adapter.py`.
- `routes_jobs.py`, `routes_lidarr.py`, `routes_setup.py`, `routes_submissions.py`: split route modules for jobs, Lidarr/wanted endpoints, setup/auth/config, and MusicBrainz/AcoustID submissions.
- `job_engine.py`: in-memory `PythonJob`/`JobStore`, structured state, cooperative cancellation.
- `helpers_mb.py`: MusicBrainz and AcoustID helpers. No `app.py` dependency — the strongest current provider boundary.
- `backend/`: `beets_adapter.py` (the sole HTTP client to stock Beets' `web`/`webmanager` plugins), `beets_plugins.py` (`webmanager` plugin provisioning/health), `transaction_engine.py` (controlled mutation boundary, pure Web-Manager-local orchestration), `matching/` (canonical matching evidence engine), plus `album_match.py`, `audio_preferences.py`, `import_guard.py`, `mb_alignment.py`, `security.py`, `slskd.py`, `title_normalize.py`, `track_align.py`. `beets_client.py` (the retired control-agent HTTP client) is not yet deleted — see `docs/TECHNICAL_DEBT.md` (ARCH-010).
- `beetsplug/webmanager/`: the integration plugin itself, provisioned into stock Beets' `/config/beetsplug`.
- `frontend/src/`: React/Next/TypeScript frontend; `api/client.ts` centralizes API calls and CSRF headers, `api/types.ts` centralizes response shapes, `views/` and `features/` hold pages and workflow panels.
- `tests/`: Python backend tests (`unittest`/`pytest`-compatible). `frontend/` has its own Vitest suite.
- `scripts/`: CI-invoked security/inventory generators and verifiers, plus deployment helpers. See each script's own docstring; anything not referenced by `.github/workflows/` or a test is not part of the supported toolchain.
- `docs/adr/`: Architecture Decision Records for standing product/architecture invariants. See `ARCHITECTURE.md` for a summary and links.

See `docs/ARCHITECTURE.md` for current system shape and intended dependency direction, and `docs/TECHNICAL_DEBT.md` for known open architecture work.

## Local Validation

Run from the repository root before opening a change:

```bash
python -m py_compile app.py helpers_mb.py job_engine.py routes_jobs.py routes_lidarr.py routes_setup.py scripts/security_secret_scan.py scripts/validate_compose_security.py scripts/verify_security_config.py
python -m unittest discover -s tests -p "test_*.py"
python scripts/security_secret_scan.py
python scripts/validate_compose_security.py
python scripts/generate_endpoint_inventory.py --check
```

`generate_endpoint_inventory.py --check` fails if `security/endpoint_inventory.json` is stale relative to the route decorators in `app.py`/`routes_jobs.py`/`routes_lidarr.py`/`routes_setup.py`/`routes_submissions.py`. Run it without `--check` to regenerate after adding/removing a route, then fill in any new `"NEEDS_REVIEW"` field by hand before committing.

Frontend, from `frontend/`:

```bash
npm ci
npm run typecheck
npm run lint
npm test
npm run build
npm audit --audit-level=high
```

Deployment configuration validation, only against a configured deployment environment (it will fail on a bare checkout where required environment values are unset — that is expected, not a bug; never add real credentials to make it pass locally):

```bash
python scripts/verify_security_config.py
```

For a targeted Python test: `python -m unittest tests.test_name`.

## Frontend Runtime Shape

The frontend is a React/Next.js static export under `frontend/`. Local development can use the Next dev server, but production is served from the generated `frontend/dist/` artifacts by the Flask deployment. A working local dev-server build is not proof that the deployed app changed — verify against `frontend/dist/` or a real container for anything deployment-relevant.

## Deployment Files Stay Generic

Repository deployment files (`docker-compose.yml`, `docker-compose.full.yml`, `docker-compose.dev.yml`, `.env.example`) are generic product examples only. Never commit a real host's Compose file, host paths, LAN addresses, credentials, private topology, or any single deployer's own configuration — host paths are driven entirely by environment variables. `tests/test_no_owner_specific_deployment_details.py` enforces this structurally (path/address *classes*, not a denylist of specific former values) and is a good template for extending the check if a new deployment file is added.

For the guarded TrueNAS rollout of a specific tagged release (image pin, mount verification, database/token safety checks, backup, rollback), see `docs/TRUENAS_ROLLOUT.md` and `scripts/deploy_truenas_web_manager.sh`.

## Deployment Procedure

When deploying an already-validated build:

1. Validate the exact source state locally (commands above).
2. Build frontend artifacts when frontend files changed.
3. Back up existing live files before replacing them.
4. Copy only the intended backend files or built frontend artifacts.
5. Restart or reload only through the approved app mechanism.
6. Verify health endpoints and the affected served route after restart.
7. Report file-copy, restart/reload, and live-verification results as separate facts — do not infer a successful deploy from a successful build.

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
