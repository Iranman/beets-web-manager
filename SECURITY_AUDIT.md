# Security Audit Report

## Executive Summary

This review treated Beets Web Control as an internet-exposed self-hosted application. The most important verified issues were unauthenticated access to nearly every API route, plaintext secret exposure through committed config/Compose files and `/api/config`, missing CSRF protection for destructive browser requests, public static route path traversal risk, and mutable dependency defaults.

Critical and high findings that were fixed in this pass now have focused regression tests. The application is not declared secure: significant open work remains around Docker hardening, runtime package downloads, SSRF controls, rate limiting, object-level authorization for any future multi-user mode, and deeper destructive workflow race/path validation.

## Scope and Reviewed Snapshot

- Workspace: `C:\Users\irand\beets-art-fix`
- Date: 2026-07-16
- Git status: this staged directory is not a Git checkout, so no reviewed commit SHA was available.
- Reviewed files: `app.py`, `routes_jobs.py`, `job_engine.py`, `helpers_mb.py`, `backend/`, `frontend/src/`, `frontend/package.json`, `frontend/package-lock.json`, `config.yaml`, `docker-compose.arrs.yml`, `AGENTS.md`, `CLAUDE.md`, and existing tests.
- Out of scope: production/live service testing, real public endpoints, destructive tests against the real music library, and credential validation.
- Detailed endpoint inventory: `security/endpoint_inventory.json` contains 187 decorator-derived routes with auth, CSRF, state-change, file, external-service, job, and rate-limit classifications.

## Architecture and Trust Boundaries

The backend is a large Flask app in `app.py` plus job routes in `routes_jobs.py`. The production frontend is a React/Next static export in `frontend/` served by Flask from the same origin. The app reads/writes Beets config and database files, mounted music/download roots, playlist staging directories, caches, and job state. It invokes Beets, yt-dlp, ffmpeg/ffprobe/fpcalc/chromaprint, package managers, and helper runtimes. It calls external providers including MusicBrainz, AcoustID, Discogs, Plex, qBittorrent, Lidarr, SLSKD, Spotify, and OpenAI/OpenRouter-style AI endpoints.

Primary trust boundaries are browser-to-Flask, Flask-to-filesystem, Flask-to-subprocess, Flask-to-external-provider, Flask-to-AI-provider, container-to-host mounts, and logs/backups/database/browser storage.

## Attack-Surface Inventory

The machine-readable inventory is the source of truth for route-by-route fields. Summary:

- Public allowlist after hardening: `GET/HEAD /api/health`, `GET/HEAD /assets/<path:filename>`, and `GET/HEAD /_next/static/<path:filename>`.
- Root `/`, catch-all SPA route, settings/config, jobs, plugins, Plex, playlist, import, cleanup, AI, deduplication, music-format replacement, and library mutation routes require owner/admin application authentication.
- State-changing methods require CSRF/origin protection through global middleware.
- 187 routes were inventoried statically from decorators in `app.py` and `routes_jobs.py`.

Inventory limitations: static decorator analysis does not prove object-level authorization or every helper called by a route, and rate-limit classifications are requirements, not implemented controls.

## Severity-Ranked Findings

### SEC-001 - Unauthenticated API, settings, plugin, job, and destructive operation access

- Severity: Critical
- Confidence: High
- CWE: CWE-306, CWE-862
- OWASP: A01 Broken Access Control, A07 Identification and Authentication Failures, API1/API5
- Affected files/lines: `app.py` routes throughout, examples `app.py:45376`, `app.py:45386`, `app.py:45528`, `routes_jobs.py:40`, `routes_jobs.py:110`
- Affected routes/workflows: `/api/config`, `/api/plugins/run`, `/api/library/music-format/replace`, `/api/import/review-folder/delete`, `/api/jobs/*`, most `/api/*`
- Required attacker access: unauthenticated HTTP client
- Root cause: no global authentication/authorization boundary and no exact public route policy
- Reproduction: before fix, anonymous requests could reach sensitive routes such as config read/write, plugin execution, cleanup, import, jobs, and playlist mutations.
- Impact: complete app control, destructive library operations, secret disclosure, expensive job creation, subprocess/plugin abuse
- Secrets/destructive actions: both
- Recommended fix: global fail-closed auth, exact public allowlist, no local/proxy bypass, route inventory, regression tests
- Fix status: Fixed with `app.py:1829`, `app.py:1966`; auth supports bearer token or basic password and refuses protected routes with 503 if no secret is configured unless explicitly disabled
- Regression test: `tests/test_security_hardening.py::test_flask_auth_boundary_is_global_and_exact_allowlist`
- Remaining limitations: shared owner/admin secret only; no per-user RBAC or object ownership model

### SEC-002 - Credential and configuration disclosure

- Severity: Critical
- Confidence: High
- CWE: CWE-200, CWE-312, CWE-798
- OWASP: A02 Cryptographic Failures, A05 Security Misconfiguration, API3 Excessive Data Exposure
- Affected files/lines: `config.yaml:77`, `config.yaml:98`, `config.yaml:133`, `config.yaml:145`, `docker-compose.arrs.yml:160`, `docker-compose.arrs.yml:161`, `docker-compose.arrs.yml:217`, `docker-compose.arrs.yml:219`, `docker-compose.arrs.yml:221`, `docker-compose.arrs.yml:226`, `docker-compose.arrs.yml:303`, `docker-compose.arrs.yml:342`, `app.py:45368`, `app.py:45376`
- Affected routes/workflows: `/api/config`, Compose deployments, tracked Beets config
- Required attacker access: repo/read access; before SEC-001, unauthenticated HTTP access to `/api/config`
- Root cause: plaintext reusable credentials in source/config and raw config API response
- Reproduction: read committed config/compose files or call `/api/config` before auth hardening.
- Impact: provider account compromise, Plex/control-plane access, downloader/service account takeover
- Secrets/destructive actions: secrets directly; possible destructive provider actions
- Recommended fix: remove committed secrets, require env placeholders, redact API config responses, reject saving redacted placeholders, add secret scan
- Fix status: Fixed in current source. Sensitive config values are blank or env-required; `/api/config` returns redacted content and refuses redacted placeholder saves.
- Regression test: `tests/test_security_hardening.py::test_committed_configs_do_not_contain_known_leaked_secret_patterns`, `tests/test_security_hardening.py::test_config_and_status_responses_are_redacted`, `scripts/security_secret_scan.py`
- Remaining limitations: real deployed `.env`, old logs, old container layers, private backups, and provider dashboards must be checked/rotated outside this repo

### SEC-003 - Missing CSRF/origin protection on destructive browser requests

- Severity: High
- Confidence: High
- CWE: CWE-352
- OWASP: A01 Broken Access Control, API8 Security Misconfiguration
- Affected files/lines: `app.py:1926`, `app.py:1966`, `frontend/src/api/client.ts:107`, `frontend/src/lib/api.ts:32`
- Affected routes/workflows: every POST/PUT/PATCH/DELETE route, including cleanup, import, plugin, playlist, settings, and job actions
- Required attacker access: victim browser authenticated to app and attacker-controlled page
- Root cause: state-changing routes had no CSRF token/header or origin enforcement
- Reproduction: before fix, a cross-site form/fetch-style request could target destructive routes if browser credentials were sent.
- Impact: forced destructive operations, job starts, settings changes, config writes
- Secrets/destructive actions: destructive actions; some settings/secrets workflows
- Recommended fix: global CSRF/origin middleware and frontend header on same-origin API writes
- Fix status: Fixed with `X-Beets-CSRF: 1`, same-origin Origin/Referer/Sec-Fetch checks, and frontend header updates
- Regression test: `tests/test_security_hardening.py::test_csrf_and_security_headers_are_enforced`
- Remaining limitations: no per-route nonce/session CSRF token because the app currently uses shared token/basic auth rather than server sessions

### SEC-004 - Public static route path traversal risk

- Severity: High
- Confidence: High
- CWE: CWE-22
- OWASP: A01 Broken Access Control
- Affected files/lines: fixed route code at `app.py:2005`, `app.py:2050`, `app.py:2059`
- Affected routes/workflows: `/assets/<path:filename>`, `/_next/static/<path:filename>`
- Required attacker access: unauthenticated HTTP client
- Root cause: route path parameter was joined directly to a base directory
- Reproduction: attempt traversal with `../`, absolute paths, backslashes, or encoded variants against public asset routes.
- Impact: arbitrary file read if traversal resolved outside the asset root
- Secrets/destructive actions: possible secret disclosure
- Recommended fix: reject NUL, absolute paths, backslashes, `..`, and any resolved path outside the static root
- Fix status: Fixed by `_safe_static_file()` and route use of that helper
- Regression test: `tests/test_security_hardening.py::test_public_static_routes_cannot_traverse`
- Remaining limitations: static test covers code shape; add runtime Flask client tests when app import is made lightweight enough for dynamic route tests

### SEC-005 - yt-dlp cookie/auth status disclosure

- Severity: High
- Confidence: High
- CWE: CWE-200
- OWASP: API3 Excessive Data Exposure
- Affected files/lines: `app.py:2065`, `app.py:2074`, `app.py:2088`, `app.py:2134`
- Affected route/workflow: `/api/ytdlp/status`
- Required attacker access: before SEC-001, unauthenticated HTTP client; after SEC-001, authenticated owner/admin only
- Root cause: status payload exposed cookie/auth paths and rejection details that can reveal sensitive host layout and credential material locations
- Reproduction: call `/api/ytdlp/status` and inspect cookie/auth fields before fix.
- Impact: credential path disclosure and easier theft by any reader with file/log access
- Secrets/destructive actions: secrets-adjacent credential material
- Recommended fix: return configured/unconfigured status and redacted labels only
- Fix status: Fixed with redacted status helpers and blank `cookie_file`/`cookies_from_browser` response fields
- Regression test: `tests/test_security_hardening.py::test_config_and_status_responses_are_redacted`
- Remaining limitations: local filesystem permissions for actual cookie files remain deployment responsibility

### SEC-006 - Mutable frontend dependencies

- Severity: High
- Confidence: High
- CWE: CWE-1104, CWE-829
- OWASP: A08 Software and Data Integrity Failures
- Affected files/lines: `frontend/package.json:13` through dependency blocks
- Affected workflow: frontend install/build
- Required attacker access: supply-chain or registry compromise, or time-based dependency drift
- Root cause: direct dependencies used `latest` and some loose ranges
- Reproduction: run install at different times and receive different package versions.
- Impact: unreviewed frontend code in production bundle, possible XSS/build compromise
- Secrets/destructive actions: indirect; frontend can call authenticated destructive APIs
- Recommended fix: pin direct dependencies and use lockfile/`npm ci`
- Fix status: Fixed by exact versions in `frontend/package.json` and CI `npm ci`
- Regression test: `tests/test_security_hardening.py::test_frontend_dependencies_are_pinned`
- Remaining limitations: transitive dependencies still rely on lockfile integrity and registry trust

## Huntarr Regression Checklist

| Class | Result |
|---|---|
| 3.1 Unauthenticated settings access | Fixed. Settings/config routes are protected by global auth; `/api/config` is redacted and write rejects redacted placeholders. |
| 3.2 Credential/config disclosure | Fixed for current source and key API responses. Rotation still required for exposed values. |
| 3.3 Client-controlled setup state | No setup route/state flow found. Global auth does not trust client setup flags. |
| 3.4 Account linking/external auth | No Plex OAuth/linking login flow found; Plex token operations are protected API calls. |
| 3.5 Password reset/recovery/2FA | No password reset, recovery key, or 2FA routes found. Shared secret auth remains a limitation. |
| 3.6 Authentication bypass matching | Fixed. Public access is exact method+endpoint allowlist, not path substring/prefix/local IP matching. |
| 3.7 Proxy/local-network trust | Fixed for bypass class: no forwarded-header/local-IP trust grant is used. Host allowlisting remains open. |
| 3.8 Archive extraction/backup/restore | No user archive restore surface found. Runtime Deno zip download remains an open supply-chain risk. |
| 3.9 Path traversal/arbitrary deletion | Static public routes fixed. Many destructive filesystem workflows still need deeper race/path tests beyond existing targeted tests. |

## AI-Specific Security Assessment

Observed positives: several AI paths use candidate indexes rather than free-form IDs, MusicBrainz/AcoustID evidence is used in import/replacement decisions, and existing tests cover AcoustID-first matching. Remaining risk: there is no central AI request broker enforcing redaction, request caps, concurrency limits, circuit breakers, prompt-injection markers, or a uniform schema/authorization gate for every AI call. OpenAI errors are sometimes returned with short provider body excerpts; these should continue to be redacted and bounded.

AI must remain advisory only. Any destructive delete, replace, merge, retag, rename, move, or cleanup should require deterministic evidence and server-side confirmation independent of model confidence.

## Destructive-Action Assessment

High-impact workflows include Clean All, duplicate cleanup, artist/album folder cleanup, music-format replacement, import-review folder deletion, playlist deletion, Plex sync, qBittorrent hardlink repair, and library import/retag/move operations. Global auth and CSRF now cover these routes. Existing code has multiple specific root checks and confirmations, but the audit did not fully prove every TOCTOU, symlink, race, recursive delete, and cross-root case across the whole app. This remains open work.

The music-format replacement route is protected by auth/CSRF and existing tests assert replacement is verified before removing the original, but the workflow still deserves runtime tests with temporary directories for rejected-format review-state transitions.

## Docker and Deployment Assessment

Fixed: Beets/SLSKD/Cleanuparr PUID/PGID defaults no longer default to root; sensitive Compose credentials now require environment variables; `BEETS_WEB_AUTH_TOKEN` is required by the Beets service recipe.

Open: many images still use `:latest`, broad `/mnt/PLEX/data:/data` mounts remain, and services do not consistently set `cap_drop`, `security_opt: no-new-privileges`, `read_only`, tmpfs, or narrow writable mounts. A compromised Beets app can still affect any writable mounted dataset.

## Dependency and Supply-Chain Assessment

Fixed: frontend direct dependencies are exact-pinned and CI uses `npm ci`; a custom secret scan and `npm audit --audit-level=high` were added to CI. Open: Python dependencies are mostly implicit in the container/app environment, runtime installation/download of Deno/Node helpers remains (`app.py:254` through `app.py:420`), Docker images remain mutable, and no full SBOM was generated in this pass.

## Tests and Scanners Run

- `python -m py_compile app.py tests\test_security_hardening.py scripts\security_secret_scan.py` - passed
- `python -m unittest tests.test_security_hardening` - passed, 7 tests
- `python -m unittest discover -s tests -p "test_*.py"` - passed, 547 tests
- `npm.cmd run typecheck` - passed
- `npm.cmd run build` - passed
- `python scripts\security_secret_scan.py` - passed after fixes
- `python scripts\security_secret_scan.py --include-local-artifacts` - passed after fixes
- `npm.cmd audit --audit-level=high` - passed high threshold; reported two moderate Next/PostCSS advisories that remain open
- `python -m bandit --version` and `python -m pip_audit --version` - tools not installed locally; not run

Not yet run in this pass: Bandit, pip-audit, osv-scanner, trivy, gitleaks, full container scanning, or a generated SBOM.

## Fixed Findings

- SEC-001 global authentication boundary and exact public allowlist
- SEC-002 secret/config redaction in source, Compose, and `/api/config`
- SEC-003 CSRF/origin protection for state-changing routes
- SEC-004 public static route path traversal guard
- SEC-005 yt-dlp status redaction
- SEC-006 frontend dependency pinning
- CI security workflow and custom secret scanner added
- Security policy, threat model, endpoint inventory, and regression tests added

## Unfixed Findings

### SEC-007 - Runtime package and binary installation

- Severity: High
- Confidence: High
- CWE: CWE-494, CWE-829
- OWASP: A08 Software and Data Integrity Failures
- Affected files/lines: `app.py:254` through `app.py:420`
- Workflow: yt-dlp JavaScript runtime/plugin helper installation
- Attacker access: supply-chain, compromised release endpoint, network attacker where TLS trust fails, or mutable latest release
- Root cause: app can download/install runtimes at startup/runtime, including `latest` release selection
- Reproduction: start app with managed runtime installation enabled and observe external package/runtime retrieval
- Impact: code execution in app container
- Secrets/destructive actions: could expose all app secrets and mounted library files
- Recommended fix: bake pinned, checksum-verified runtimes into the image; disable runtime install by default; verify signatures/checksums
- Fix status: Open
- Regression test: none yet
- Limitations: left open to avoid breaking current downloader/provider flow without a replacement image build

### SEC-008 - Incomplete container least privilege

- Severity: High
- Confidence: High
- CWE: CWE-250, CWE-732
- OWASP: A05 Security Misconfiguration
- Affected files/lines: `docker-compose.arrs.yml:33`, `docker-compose.arrs.yml:69`, `docker-compose.arrs.yml:207`, `docker-compose.arrs.yml:232`, and other `:latest`/broad mount lines
- Workflow: Docker deployment
- Attacker access: app compromise or container escape/lateral movement
- Root cause: mutable images, broad writable mounts, no consistent capability drop/no-new-privileges/read-only filesystem
- Impact: host dataset modification beyond intended app scope
- Secrets/destructive actions: destructive host file writes/deletes possible depending on mount permissions
- Recommended fix: pin images by digest, narrow volumes, set non-root UID/GID, drop caps, set `no-new-privileges`, consider read-only rootfs/tmpfs, document required write paths
- Fix status: Partially fixed for some UID/GID and secret env defaults; still open
- Regression test: static assertions for non-root defaults; no full Compose policy test yet
- Limitations: digest pinning requires choosing compatible image digests

### SEC-009 - Central SSRF protection missing

- Severity: High
- Confidence: Medium
- CWE: CWE-918
- OWASP: API7 SSRF
- Affected files/lines: examples `app.py:8704`, `app.py:36382`, `app.py:36638`, multiple `urllib.request.urlopen` call sites
- Workflow: album-art URL, configured service URLs, external metadata/provider calls
- Attacker access: authenticated user, compromised metadata/provider response, or attacker-controlled URL/config field
- Root cause: no central URL fetcher that validates scheme/host, resolves DNS, blocks loopback/private/link-local/metadata/Docker destinations, and revalidates redirects
- Impact: internal service probing/access and credential forwarding risk
- Secrets/destructive actions: possible internal credential exposure
- Recommended fix: fixed provider endpoints where possible; central safe HTTP client with DNS/IP/redirect checks, size/time limits, and no credential forwarding to redirected hosts
- Fix status: Open
- Regression test: none yet
- Limitations: needs careful allowlist design for legitimate self-hosted Plex/Lidarr/qBittorrent addresses

### SEC-010 - Rate limiting and abuse controls missing

- Severity: Medium
- Confidence: High
- CWE: CWE-307, CWE-400
- OWASP: API4 Unrestricted Resource Consumption
- Affected files/lines: global auth at `app.py:1966`; no rate limiter found
- Workflow: auth attempts, expensive scans, AI calls, downloads, fingerprinting, logs, jobs
- Attacker access: unauthenticated for auth attempts; authenticated for expensive jobs
- Root cause: no account/client/job rate limits or global concurrency budget
- Impact: brute-force attempts, cost abuse, CPU/disk/network exhaustion
- Fix status: Open
- Regression test: none yet

### SEC-012 - Moderate Next/PostCSS advisory remains

- Severity: Medium
- Confidence: High
- CWE: CWE-79
- OWASP: A03 Injection / frontend XSS risk
- Affected files/lines: `frontend/package-lock.json` transitive dependency under Next; direct root `postcss` is pinned above the affected range
- Workflow: frontend build/runtime dependency tree
- Attacker access: attacker able to influence CSS processed by the vulnerable transitive path
- Root cause: current `next` package pulls a vulnerable transitive `postcss` range according to `npm audit`
- Impact: possible XSS in CSS stringify output under affected conditions
- Secrets/destructive actions: indirect through authenticated frontend session
- Recommended fix: wait for or select a compatible Next release that resolves the advisory; do not apply npm's forced breaking downgrade without compatibility testing
- Fix status: Open
- Regression test: `npm.cmd audit --audit-level=high` in CI catches high or critical advisories; moderate remains documented here
- Limitations: npm's suggested forced fix would install an incompatible/breaking Next version, so it was not applied in this pass
### SEC-011 - No multi-user object-level authorization model

- Severity: Medium
- Confidence: High
- CWE: CWE-639, CWE-862
- OWASP: API1 BOLA, API5 BFLA
- Affected files/lines: global owner/admin auth at `app.py:1966`; jobs in `routes_jobs.py`
- Workflow: jobs, playlists, logs, settings, files
- Attacker access: authenticated shared-secret user or future lower-privilege account
- Root cause: app has shared owner/admin access, not per-user ownership or RBAC
- Impact: any authenticated user can access/control all jobs, playlists, settings, and destructive workflows
- Fix status: Open by design; document as single-owner model until RBAC exists
- Regression test: not applicable until user model exists

## False Positives and Dismissed Items

- Password reset, 2FA, recovery keys: no such routes or account flows were found in this app.
- Plex OAuth/account linking: no external login/linking flow was found; Plex usage is token/config based.
- Local-IP/proxy-header authentication bypass: no `request.remote_addr`, `X-Forwarded-For`, or ProxyFix trust grant was found in the auth path after hardening.
- User archive restore/extractall: no user-uploaded backup restore/archive extraction route was found. Runtime Deno zip download is tracked separately as SEC-007.
- Health endpoint: intentionally public and documented in the exact allowlist.

## Remaining Risk

The app is materially safer after this pass, but it is not fully audited or secure. The largest remaining risks are supply-chain/runtime installation, Docker least privilege, SSRF, rate limiting, and deep destructive workflow validation across every path/delete/move/replace case. Automated scanners are supporting evidence only; scanner success must not be treated as proof of security.

## Credential-Rotation Recommendation

Rotate every credential that was present in committed `config.yaml`, `docker-compose.arrs.yml`, logs, old backups, live `.env` files, or previously returned by `/api/config`. At minimum, rotate Plex, Discogs, ListenBrainz, OpenAI/OpenRouter/AI provider keys, Lidarr, SLSKD/Soulseek, Digarr initial password, Postgres password, and the Beets web auth token/password. Also invalidate any yt-dlp/browser cookie file that may have been exposed by status/log output.