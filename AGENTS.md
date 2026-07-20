# AGENTS.md

Concise instructions for Codex and other coding agents working in this repository.

## Start Here

Read `docs/AI_ENGINEERING_RULES.md` before changing code. It is the shared source of truth for architecture boundaries, product rules, safety rules, matching rules, mutation rules, testing requirements, and AI-agent behavior.

Use `docs/ARCHITECTURE.md` for the current architecture and intended dependency direction. Use `docs/TECHNICAL_DEBT.md` for known migration targets. Use `REVIEW.md` before opening or summarizing a change.

## Repository Shape

- Backend: Flask routes are mostly in `app.py`; additional route modules include `routes_jobs.py`, `routes_lidarr.py`, `routes_setup.py`, and `routes_submissions.py`.
- Backend helpers: `backend/`, `helpers_mb.py`, and `job_engine.py` contain extracted domain, provider, transaction, and job utilities.
- Frontend: Next.js static export with React and TypeScript under `frontend/src/`; generated output syncs to `frontend/dist/`.
- Beets remains the library backend. The app must not grow a parallel music-library database.

## Non-Negotiable Rules

- MusicBrainz and AcoustID are the primary identity evidence.
- AI is optional and untrusted; it may rank or explain deterministic candidates but must not invent verified identity.
- The canonical album identity is `mb_releasegroupid`; release IDs are edition-level secondary data.
- Do not silently modify the library. Moves, renames, merges, tag writes, replacements, artwork writes, and deletes require controlled preview/apply/audit/recovery handling.
- Never expose credentials, cookies, tokens, authorization headers, signed URLs, or secrets in logs, API responses, frontend state, or commits.
- Preserve the compact existing UI direction and current stack; do not add a component library or redesign unrelated pages.

## Validation Commands

From the repository root:

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

Frontend checks:

```powershell
cd frontend
npm.cmd run typecheck
npm.cmd run build
npm.cmd run lint
npm.cmd audit --audit-level=high
```

For a targeted Python test:

```powershell
python -m unittest tests.test_name
```

## Workflow

1. Inspect current files and dirty state before editing.
2. Keep changes small and scoped to the requested slice.
3. Do not refactor behavior during documentation/governance work.
4. Add or update tests for enforceable behavior when code changes are made.
5. Run relevant checks and report actual results.
6. Record unresolved architecture debt in `docs/TECHNICAL_DEBT.md` instead of hiding it.
7. Do not commit directly to `main`; use a dedicated branch.
