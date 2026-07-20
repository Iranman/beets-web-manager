# CLAUDE.md

Concise instructions for Claude Code working in this repository.

## Required Reading

Read `docs/AI_ENGINEERING_RULES.md` before code changes. It is the single shared source of truth for product invariants, architecture boundaries, matching rules, mutation safety, job requirements, testing requirements, security rules, and AI-agent behavior.

Use `docs/ARCHITECTURE.md` for the current system shape and intended dependency direction. Use `docs/TECHNICAL_DEBT.md` for known migration targets. Use `REVIEW.md` as the review checklist.

## Architecture Summary

- Backend: Python/Flask, with many routes still in `app.py`; selected routes are split into `routes_jobs.py`, `routes_lidarr.py`, `routes_setup.py`, and `routes_submissions.py`.
- Domain/helper modules: `backend/`, `helpers_mb.py`, and `job_engine.py` hold extracted matching, safety, provider, transaction, and job utilities.
- Frontend: React, Next.js static export, TypeScript, Tailwind, MUI, Headless UI, and TanStack Query under `frontend/src/`.
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

## Validation Commands

From repo root:

```powershell
python -m py_compile app.py helpers_mb.py job_engine.py routes_jobs.py routes_lidarr.py routes_setup.py scripts/security_secret_scan.py scripts/validate_compose_security.py scripts/verify_security_config.py
python -m unittest discover -s tests -p "test_*.py"
python scripts/security_secret_scan.py
python scripts/validate_compose_security.py
```

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
npm.cmd audit --audit-level=high
```

## Working Rules

Stay on a feature branch. Inspect dirty files before editing. Do not rewrite broad areas or user-facing behavior unless the task explicitly calls for it. Prefer existing helpers and documented migration paths. When implementation work uncovers larger design issues, record them in `docs/TECHNICAL_DEBT.md` and keep the current slice small.
