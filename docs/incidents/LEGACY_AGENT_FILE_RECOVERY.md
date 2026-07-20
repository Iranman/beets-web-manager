# Legacy Agent File Recovery And Incident Lessons

This document records the sanitized disposition of recovered legacy `AGENTS.md` and `CLAUDE.md` files. The raw recovered backups are intentionally not committed because they contain local operator context, private paths, internal addresses, and security-sensitive references.

## Preservation

Exact byte-for-byte copies of the recovered files were stored outside the Git repository, outside the deployment synchronization path, and outside directories expected to be overwritten by automated deployment. The exact host path remains local/private.

SHA-256 hashes of the preserved recovered files:

- `AGENTS.md.bak-20260720-125359-pre-architecture-hardening-from-live-share`: `49D92EC361CFFE3380E483DB8316C00FAF9AFABFB66976702A619DEE724843DD`.
- `CLAUDE.md.bak-20260720-125359-pre-architecture-hardening-from-live-share`: `9CF5C23CF2EB32F1C907C29DF28CCB33BB4331469EDFA4173600007F43751CD8`.

An additional recovered Claude context copy was also preserved privately for comparison: `E9023F802A352217EEB280FB270ADA6C7991F790CA249A0967D9810157FA409F`.

## Security Inspection

The existing secret scanner was run against `.md` copies in an isolated scan root and passed. A targeted no-values inspection also checked for API-key references, tokens, passwords, authorization headers, cookies, signed/private URLs, internal addresses, and personal information. It found security-sensitive categories and private operator context, so raw backups remain private and were not copied into the repository.

## Disposition

Retained in shared governance:

- Permanent safety rules about Beets remaining the backend, MusicBrainz/AcoustID identity evidence, untrusted AI, release-group identity, mutation safety, job requirements, and secret handling.
- Deployment validation guidance, frontend build/deploy shape, live verification expectations, and deployment-only config validation caveats in `docs/operations/DEVELOPMENT_AND_DEPLOYMENT.md`.
- Sanitized incident lessons below.

Kept only in private backup:

- Exact local paths, deployment paths, private URLs, internal addresses, personal context, and any secret-adjacent operational details.
- Temporary debugging transcripts, dated next-step notes, sample artist names, test marker strings, and one-off helper names.
- Obsolete instructions from earlier frontend/deployment transitions that no longer describe the current architecture.

## Sanitized Incident Lessons

- Security hardening can take the app offline when authentication, health probes, LAN integrations, CSP, and rate limits are changed without end-to-end deploy verification. Future security changes need staged validation, explicit public-health endpoint checks, provider integration checks, and rollback notes.
- Concurrent agent edits against the same large runtime file can corrupt or overwrite active work. Use separate worktrees for independent tasks, inspect dirty state before editing, and avoid editing files another agent is actively modifying.
- Path-normalization changes are high risk. Escaping and backslash handling need targeted tests before deployment, especially in filesystem safety checks, playlist path mapping, and source/destination validation.
- Live deployment verification must distinguish local build success, file copy success, backend restart success, and served-route success.
