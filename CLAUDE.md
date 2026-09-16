# CLAUDE.md

Concise always-on instructions for Claude Code in this repository.

## Always-On Invariants

- Work on the requested slice only. Do not perform unrelated refactors or repository-wide audits.
- Beets is the authoritative library manager.
- `mb_releasegroupid` is canonical album identity; Release IDs are supporting edition-level data.
- MusicBrainz and AcoustID are primary identity evidence. AI is optional/untrusted and must not block deterministic matching.
- No silent library mutations. Moves, renames, merges, deletes, tag/artwork writes, replacements, or Beets DB changes require controlled mutation handling.
- Never expose secrets in logs, API responses, frontend state, fixtures, or commits.
- Never copy the project owner's live TrueNAS paths, LAN details, credentials, or private deployment topology into this public repository.
- Do not commit directly to `main`.
- Do not push, open/modify PRs, merge, deploy, or touch production without explicit authorization.

## Use Project Skills

Use the smallest relevant skill under `.claude/skills/`:

- narrow bug/traceback -> `fixing-targeted-bugs`
- scoped feature -> `implementing-feature-slices`
- MB/AcoustID/import matching -> `matching-music`
- Clean All/checkpoint/merge -> `repairing-clean-all`
- final implementation review -> `reviewing-diffs`
- CodeQL/security finding -> `handling-codeql`
- release/full acceptance -> `verifying-release`

Do not load all skills for every task.

## Reference Docs — Load Only When Needed

`docs/AI_ENGINEERING_RULES.md` remains the authoritative detailed source for architecture, matching, mutation, job, security, and testing rules. Read only the sections relevant to a change that crosses one of those boundaries.

`docs/AGENT_WORKFLOW.md` remains authoritative for role/authority, handoff, and stage policy. Read it only when the task involves those questions.

Use `docs/ARCHITECTURE.md` for architecture changes and `docs/TECHNICAL_DEBT.md` when a discovered larger issue should be deferred.

## Working Style

Inspect dirty state before editing. Reproduce before fixing when practical. Prefer targeted tests first. Run the complete validation suite once at final review/release, not repeatedly during a narrow fix.
