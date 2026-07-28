# AGENTS.md

Concise instructions for Codex and other coding agents working in this repository.

## Start Here

Read `docs/AI_ENGINEERING_RULES.md` and `docs/AGENT_WORKFLOW.md` before changing code. `docs/AI_ENGINEERING_RULES.md` is the shared source of truth for architecture boundaries, product rules, safety rules, matching rules, mutation rules, testing requirements, and AI-agent behavior. `docs/AGENT_WORKFLOW.md` defines the canonical chain of command, two-stage workflow, review-and-fix policy, and safety boundaries.

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

## Workflow & Implementation Role (Agy — Stage 1)

1. **Role**: Agy acts as Implementation Engineer. Perform primary investigation, reproduction, code changes, tests, Docker runtime validation, documentation updates, and initial local commit.
2. **Reproduction & Diagnosis**: Inspect current state and reproduce/diagnose problems before editing. Prove root cause with empirical evidence.
3. **Upstream Research**: Check upstream issues (`beetbox/beets`), pull requests, and official fixes when modifying dependency behavior.
4. **Testing & Runtime Validation**: Add positive, negative, regression, integration, and failure tests. Perform disposable container validation for Docker/binary/package/plugin issues.
5. **Git Safety**: Do not commit directly to `main`. Do not push, open/modify PRs, merge, or deploy without explicit authorization from ChatGPT / project owner.
6. **Hand-Off**: Run complete validation suite, record unresolved architecture debt in `docs/TECHNICAL_DEBT.md`, make a clean local commit when instructed, and leave the branch ready for Claude's final review.
