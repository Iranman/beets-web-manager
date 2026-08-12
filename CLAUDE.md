# CLAUDE.md

Concise instructions for Claude Code working in this repository.

## Required Reading

Read `docs/AI_ENGINEERING_RULES.md` and `docs/AGENT_WORKFLOW.md` before code changes. `docs/AI_ENGINEERING_RULES.md` is the single shared source of truth for product invariants, architecture boundaries, matching rules, mutation safety, job requirements, testing requirements, security rules, and AI-agent behavior. `docs/AGENT_WORKFLOW.md` defines the canonical chain of command, two-stage workflow, review-and-fix policy, and safety boundaries.

Use `docs/ARCHITECTURE.md` for the current system shape and intended dependency direction. Use `docs/TECHNICAL_DEBT.md` for known migration targets. Use `REVIEW.md` as the review checklist.

## Technical Lead & Final Reviewer Role

Claude acts as Technical Lead and Final Reviewer for all engineering tasks (Stage 2):
- Inspect Agy's implementation, source diff, and surrounding architecture.
- Verify root cause and check upstream issues/fixes (`beetbox/beets`).
- **Fix every issue identified directly** in code, tests, docs, and configuration. Do not return findings-only reports when fixes are safe and in scope.
- Standing Instruction: `Do not only report findings. Fix every issue you identify directly, add or correct tests, rerun all required validation, and leave the branch in a clean final state. Escalate only genuine product, architecture, dependency, or safety decisions that cannot be resolved responsibly from the repository and task requirements.`
- Rerun full validation suite after making corrections.
- Respect Git safety boundaries: do not push, open/modify PRs, merge, or deploy without explicit authorization.

## Architecture Summary

- Backend: Python/Flask, with many routes still in `app.py`; selected routes are split into `routes_jobs.py`, `routes_lidarr.py`, `routes_setup.py`, and `routes_submissions.py`.
- Domain/helper modules: `backend/`, `helpers_mb.py`, and `job_engine.py` hold extracted matching, safety, provider, transaction, and job utilities.
- Frontend: React, Next.js static export, TypeScript, Tailwind, MUI, Headless UI, TanStack Query, and React Router v8 (`react-router`; the `react-router-dom` package was removed in v8 -- all declarative routing imports come from `react-router` itself) under `frontend/src/`.
- Beets is the library backend and source of library mutations. Do not replace it with a parallel library implementation.

## Non-Negotiable Rules

- MusicBrainz and AcoustID are primary identity evidence.
- AI is optional and untrusted. AI failure must not stop deterministic MusicBrainz or AcoustID matching.
- Album-level identity is the MusicBrainz release-group ID. Do not substitute a release ID where `mb_releasegroupid` is required.
- No silent library mutations. Any move, rename, merge, delete, tag write, replacement, or artwork write needs controlled preview/apply/audit/recovery handling.
- Ambiguous or conflicting evidence goes to review.
- Destructive actions require stronger evidence than suggestions.
- Jobs need persistent status, readable progress, raw debug detail, cancellation, bounded retries, checkpoints, resume behavior, and idempotency.
- Never expose secrets in logs, API responses, frontend state, or committed files.

## Deployment Configuration

Repository deployment files are generic product examples only. The project owner's live TrueNAS, Docker Compose, media paths, service topology, credentials, LAN addresses, and private deployment configuration must never be copied into the public repository. Production configuration remains outside Git and is validated separately from public examples.

Never copy a live server's Compose file, host paths, LAN addresses, credentials, private topology, or user-specific configuration into Git. Use `docker-compose.yml` (generic, web-manager only, connects to an existing Beets control agent) and `docker-compose.full.yml` (generic, bundled `beets` + `beets-web-manager`, built from source) instead, with host paths driven entirely by environment variables (see `.env.example`). `tests/test_no_owner_specific_deployment_details.py` enforces this.

## Validation Commands

From repo root:

```powershell
python -m py_compile app.py helpers_mb.py job_engine.py routes_jobs.py routes_lidarr.py routes_setup.py scripts/security_secret_scan.py scripts/validate_compose_security.py scripts/verify_security_config.py
python -m unittest discover -s tests -p "test_*.py"
python scripts/security_secret_scan.py
python scripts/validate_compose_security.py
python scripts/generate_endpoint_inventory.py --check
```

`generate_endpoint_inventory.py --check` fails if `security/endpoint_inventory.json` is stale relative to the actual route decorators in `app.py`/`routes_jobs.py`/`routes_lidarr.py`/`routes_setup.py`/`routes_submissions.py`. Run it without `--check` to regenerate after adding/removing a route, then fill in any new `"NEEDS_REVIEW"` judgment fields by hand before committing.

Deployment configuration validation, when checking a configured deployment environment:

```powershell
python scripts/verify_security_config.py
```

`verify_security_config.py` may fail in a bare checkout when required environment values are unset or example files intentionally contain placeholders. Do not add real credentials to make this pass locally.

Frontend:

```powershell
cd frontend
npm.cmd run typecheck
npm.cmd run build
npm.cmd run lint
npm.cmd run test
npm.cmd audit --audit-level=high
```

## Working Rules

Stay on a feature branch. Inspect dirty files before editing. Do not rewrite broad areas or user-facing behavior unless the task explicitly calls for it. Prefer existing helpers and documented migration paths. When implementation work uncovers larger design issues, record them in `docs/TECHNICAL_DEBT.md` and keep the current slice small.
