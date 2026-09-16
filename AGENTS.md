# AGENTS.md

Concise always-on instructions for implementation agents working in this repository.

## Always-On Invariants

- Keep changes small, reviewable, and scoped to the requested task.
- Beets remains the authoritative library manager.
- `mb_releasegroupid` is canonical album identity.
- MusicBrainz and AcoustID are primary identity evidence; AI is optional/untrusted.
- No silent library mutations.
- Never expose secrets or owner-specific production/TrueNAS configuration in the repository.
- Do not commit directly to `main`.
- Do not push, open/modify PRs, merge, deploy, or touch production without explicit authorization.

## Task Skills

Use the project's Agent Skills when available:
- `fixing-targeted-bugs`
- `implementing-feature-slices`
- `matching-music`
- `repairing-clean-all`
- `reviewing-diffs`
- `handling-codeql`
- `verifying-release`

Load only the skill relevant to the current task.

## Detailed References

`docs/AI_ENGINEERING_RULES.md` is authoritative for detailed architecture, matching, mutation, job, security, and testing rules. Read only the relevant sections when the task crosses those boundaries.

`docs/AGENT_WORKFLOW.md` is authoritative for project roles, two-stage handoff, authority, and review policy. Read it when stage/authority/handoff behavior matters.

## Implementation Discipline

Inspect current/dirty state, reproduce the defect when practical, prove root cause with evidence, make the smallest correct change, add focused tests, and run targeted validation first. Use disposable runtime environments for Docker/plugin/binary/config issues.

Do not repeatedly run full-repository audits or full validation during a narrow change. Full acceptance belongs at final review/release.
