# Development And Deployment Operations

This document records current, durable operator procedures extracted from recovered legacy agent files. It intentionally omits private paths, hostnames, tokens, internal addresses, and temporary debugging transcripts.

## Local Validation

Run backend and frontend checks from the repository root before any deployable change:

```powershell
python -m py_compile app.py helpers_mb.py job_engine.py routes_jobs.py routes_lidarr.py routes_setup.py scripts/security_secret_scan.py scripts/validate_compose_security.py scripts/verify_security_config.py
python -m unittest discover -s tests -p "test_*.py"
python scripts/security_secret_scan.py
python scripts/validate_compose_security.py
cd frontend
npm.cmd run typecheck
npm.cmd run lint
npm.cmd run build
npm.cmd audit --audit-level=high
```

Treat `scripts/verify_security_config.py` as deployment-configuration validation, not a bare-checkout smoke test. It may fail locally when required environment values are unset or example files intentionally contain placeholders. Do not add real credentials or weaken the validator to make a local checkout pass.

## Frontend Runtime Shape

The frontend is a React/Next.js static export under `frontend/`. Local development can use the Next dev server, but production is served from the generated `frontend/dist/` artifacts by the existing Flask deployment. Do not treat the dev server URL as proof that the deployed app changed.

## Deployment Procedure

When deploying to the configured live share or host:

1. Validate the exact source state locally.
2. Build frontend artifacts when frontend files changed.
3. Back up existing live files before replacing them.
4. Copy only the intended backend files or built frontend artifacts.
5. Restart or reload only through the approved app mechanism.
6. Verify health endpoints and the affected served route after restart.
7. Report file copy, restart/reload, and live verification as separate facts.

Never copy raw local backup files, private config, generated caches, or unrelated dirty work into the deployment target.

## Job And Workflow Operations

Long-running operations should use the shared job surface (`JobStore`, `PythonJob`, job status endpoints, and frontend job polling) rather than ad hoc background threads. New or changed workflows should preserve visible status, cancellation checks, checkpoint/resume behavior, and idempotency.

## Library Safety

Normal library repairs should rely on Beets for library moves and metadata writes. Direct deletion, copy, or bulk movement under the music library root must require explicit user intent, root validation, preview or audit evidence, and recovery information where technically possible.
