# Security Best Practices Review

Target: `C:\Users\irand\beets-art-fix`
Date: 2026-07-17
Scope: Flask backend, React/Next static frontend, Docker/deployment config, staged `frontend/dist` assets.

## Executive Summary

The app has a stronger security baseline than a typical single-operator local tool: global backend authentication is enforced by default, CSRF checks exist, outbound HTTP is restricted against SSRF-style targets, container hardening is present, static file serving is path-checked, and album-art upload validation is server-side.

The main issues are consistency gaps and operational exposure risks, not obvious full bypasses. The highest-priority fix is to make every mutating frontend request use the same CSRF convention. Secondary work should tighten public health output, restrict authenticated filesystem browsing, harden disk-art serving, and move production runtime away from Flask's built-in server if this is exposed beyond the loopback-bound compose service.

Not applicable to this repo: Supabase/Firebase rules, payment verification, subscriptions, credits, multi-account tenant boundaries, or browser-controlled paid access.

## Findings

### Medium: Mutating frontend requests do not consistently send the CSRF header

The backend expects browser-origin state-changing requests to include `X-Beets-CSRF: 1`, but many frontend POST/DELETE calls bypass the shared helpers that add it.

Evidence:

- Shared helpers add the header in `frontend/src/api/client.ts` and `frontend/src/lib/api.ts`.
- Bare mutating calls remain in `frontend/src/api/client.ts`, including restart, job kill, recent import clear, library scans, cleanup actions, album-art upload/delete, Plex refresh, config revert, suggestions, and maintenance runner calls.
- `frontend/src/features/libraryHealth/LibraryHealthPanel.tsx` defines a local `jsonPost` helper that only sends `Content-Type`.

Impact:

These calls may fail when using browser-origin auth flows without an explicit `Authorization` header, and they make the CSRF model inconsistent. If deployment auth later changes to a cookie or reverse-proxy session, this becomes easier to misconfigure into a real CSRF exposure.

Recommended fix:

Centralize mutating requests so all non-GET/HEAD calls automatically include `X-Beets-CSRF: 1`. For `FormData` uploads, add only the CSRF header and let the browser set `Content-Type`. Add a regression test or lint-style scan that fails on raw `fetch(..., { method: 'POST' })` or `DELETE` calls without the header or approved wrapper.

### Medium: Runtime uses Flask's built-in server

`app.py` runs the Flask app directly with `app.run(...)`, and the Docker runtime starts `python app.py`.

Evidence:

- `Dockerfile` ends with `CMD ["python", "app.py"]`.
- `app.py` binds `HOST = "0.0.0.0"` and starts with `app.run(host=HOST, port=PORT, threaded=True, use_reloader=False)`.
- `docker-compose.arrs.yml` mitigates exposure by binding the service to `127.0.0.1:8337:8337`.

Impact:

The loopback compose binding lowers practical exposure, but Flask's built-in server is not a production WSGI server. If this app is reverse-proxied or exposed outside the host, runtime behavior and request handling are less robust than expected for production.

Recommended fix:

Run the app under a production WSGI server such as Waitress or Gunicorn, depending on the deployment platform. Keep the loopback/private bind unless external exposure is explicitly required.

### Low: Public health endpoint reveals integration configuration status

`/api/health` is public and returns booleans for configured paths and secrets such as Lidarr, Discogs, SLSKD, and OpenAI.

Evidence:

- `_AUTH_PUBLIC_ENDPOINTS` includes the Flask endpoint named `health`.
- `/api/health` returns `library_path`, `beet_bin`, `music_root`, `lidarr_key`, `discogs_token`, `slskd_key`, and `openai_key` booleans.

Impact:

No secret values are disclosed, but an unauthenticated observer can fingerprint which integrations are configured and whether expected paths are available.

Recommended fix:

Keep public health minimal, for example `{ "ok": true }` or readiness status only. Move detailed integration and path diagnostics to an authenticated endpoint.

### Low: Authenticated browse endpoint can enumerate arbitrary container paths

`/api/browse` accepts a caller-supplied path and lists directories without restricting the path to expected music/download roots.

Evidence:

- The route accepts `path` from the query string and calls `os.listdir(path)`.
- Errors return `str(exc)` to the client.

Impact:

Authentication is required, so this is not an anonymous file browse. Still, any authenticated caller, or any deployment with auth disabled, can enumerate container filesystem structure outside the intended library/download picker roots.

Recommended fix:

Restrict browse roots to an allowlist such as the configured downloads root and music root. Use a shared path containment helper and return generic errors.

### Low: Disk-art serving can return non-image files under the music root

`/api/disk-art` checks that the requested path is under `/data/media/music/`, but it does not require the target file to be an image. Unknown extensions are served as `image/jpeg`.

Evidence:

- The route accepts `path`, checks the real path string prefix, infers MIME type from the extension, and serves the file.
- Album-art uploads have stronger byte-level validation, but this serving endpoint does not reuse that validation.

Impact:

An authenticated caller that knows a path under the music root can retrieve non-image files from that tree. The risk is constrained by auth and by the music-root boundary.

Recommended fix:

Use the same path containment helper used elsewhere and allow only image extensions or image byte signatures. Return 404/400 for non-image content.

### Low: Frontend dependency audit reports moderate advisories

`npm audit --json` in `frontend` reports two moderate findings: `next` through bundled `postcss`, specifically PostCSS CSS stringification output handling.

Impact:

Exploitability appears limited for this static-export, operator-facing app unless attacker-controlled CSS is processed by the build/server pipeline. It should still be tracked because it is in a direct frontend dependency.

Recommended fix:

Track and upgrade to a patched Next/PostCSS combination when available. Do not blindly apply npm's suggested downgrade; verify compatibility with `npm run typecheck` and `npm run build`.

## Positive Controls Observed

- Global auth boundary is enforced by `before_request` unless explicitly disabled by `BEETS_WEB_AUTH_DISABLED`.
- Runtime rejects short or placeholder auth secrets.
- CSRF validation checks origin, referer, fetch metadata, and `X-Beets-CSRF` for browser-origin mutating requests.
- API responses get security headers including `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`, and a CSP.
- Outbound HTTP protections block localhost, private, link-local, metadata, and internal hostnames unless allowlisted.
- Container configuration drops capabilities, sets `no-new-privileges`, uses a non-root user, uses a read-only root filesystem, and binds the port to loopback.
- Album-art uploads use server-side size limits, generated filenames, image byte validation, and path containment under the album directory.
- Secret redaction exists for config views, and the staged build did not expose obvious secret values or source maps.

## Verification Performed

- Ran backend security tests:
  - `python -m unittest tests.test_security_hardening tests.test_outbound_security`
  - Result: 15 tests passed.
- Ran frontend dependency audit:
  - `npm.cmd audit --json`
  - Result: 2 moderate findings, no high or critical findings.
- Scanned staged frontend assets for common secret names and token patterns:
  - Result: identifiers and UI strings only; no obvious private token values found.
- Checked for public source maps in `frontend/dist`:
  - Result: no `.map` files found.

## Gaps Not Covered

- Live deployment was not probed over HTTP in this pass.
- Python dependency audit was not completed because `pip_audit` is not installed in the current environment.
- Cross-account tests are not applicable because this is a single-operator app without tenant accounts.
- Payment, subscription, credit, Supabase, and Firebase checks are not applicable to this repo.
