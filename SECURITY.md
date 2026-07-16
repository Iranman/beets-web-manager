# Security Policy

## Supported Versions

This project currently supports the latest maintained source tree and container deployment recipe only. Older local copies, backup folders, and unpublished container builds are not supported unless a maintainer explicitly says otherwise.

## Reporting a Vulnerability

Please report suspected vulnerabilities privately first. Use GitHub private vulnerability reporting if it is enabled for the repository. If private reporting is not available, contact the maintainer through a non-public channel and include enough detail to reproduce the issue safely.

Do not include real API keys, session cookies, Plex tokens, OpenAI/OpenRouter keys, downloader credentials, database files, or music-library contents in the report. Use fake credentials and temporary paths in proof-of-concept material.

Useful report details:

- Affected version, branch, container image, or source snapshot.
- The affected route, workflow, or file.
- Exact reproduction steps against a local test instance.
- Whether authentication is required.
- Whether secrets, AI actions, subprocesses, or filesystem changes are involved.
- Impact and any safe mitigation you have already tested.

## Response Process

A maintainer should acknowledge a private report within 7 days when possible. Valid issues are triaged by severity, patched in the supported version, and documented in release notes once a fix is available. Security fixes should identify affected versions and tell users when credential rotation is required.

## Disclosure

Please avoid public disclosure until a patch or mitigation is available, unless there is active exploitation or an unreasonable delay. Coordinated disclosure timelines can be adjusted based on severity and maintainer availability.

## Credit and Safe Harbor

Good-faith security research is welcome. The project will not threaten or pursue action against researchers who avoid privacy violations, service disruption, credential exposure, and destructive testing, and who report findings responsibly.

Credit is available for reporters who want it. Anonymous or no-credit reports are also accepted.

## Deployment Security Notes

- Expose the app only after setting `BEETS_WEB_AUTH_TOKEN` or `BEETS_WEB_PASSWORD`.
- Rotate any credential that was ever committed, logged, pasted into an issue, or exposed by `/api/config` before the 2026-07-16 hardening pass.
- Prefer least-privilege Docker volume mounts and non-root UID/GID values.
- Do not rely on LAN-only access, Docker networking, VPNs, or a reverse proxy as the only security boundary.