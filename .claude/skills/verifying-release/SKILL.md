---
name: verifying-release
description: Runs comprehensive release/milestone verification after implementation and final review are complete. Use only when explicitly preparing a release, merge milestone, or full repository acceptance pass; do not use for ordinary single-bug work.
disable-model-invocation: true
---

# Verifying Release

Use this skill only for a deliberate full validation pass.

## Backend

From repository root:

```powershell
python -m py_compile app.py helpers_mb.py job_engine.py routes_jobs.py routes_lidarr.py routes_setup.py scripts/security_secret_scan.py scripts/validate_compose_security.py scripts/verify_security_config.py
python -m unittest discover -s tests -p "test_*.py"
python scripts/security_secret_scan.py
python scripts/validate_compose_security.py
python scripts/generate_endpoint_inventory.py --check
```

If endpoint inventory is stale because routes intentionally changed, regenerate it, review all new judgment fields, then rerun `--check`.

Do not insert real credentials to make `verify_security_config.py` pass in a bare checkout.

## Frontend

```powershell
cd frontend
npm.cmd run typecheck
npm.cmd run build
npm.cmd run lint
npm.cmd run test
npm.cmd audit --audit-level=high
```

## Runtime Validation

Use disposable containers/volumes/config/data when behavior depends on:
- Docker image contents,
- installed packages/plugins,
- binaries,
- mounts/permissions,
- networking,
- config parsing,
- database locking,
- cancellation/signals.

Never validate destructive behavior against the production music library unless explicitly authorized.

## Release Gate

Confirm:
- requested behavior is implemented,
- no unresolved targeted regressions,
- no secret leakage,
- mutation/idempotency rules remain intact,
- generic deployment files contain no owner-specific TrueNAS details,
- docs describe actual current behavior,
- git state is understood.

Do not push, merge, tag, publish, or deploy unless explicitly authorized.

## Report

Return concise pass/fail results by validation group, blockers, and final readiness state.
