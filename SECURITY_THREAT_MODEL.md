# Security Threat Model

Reviewed local staged tree: 2026-07-16. This model covers the Beets Web Control Flask app, React frontend, local Beets library database, mounted music/download storage, AI integrations, Plex/downloader integrations, background jobs, and Docker deployment recipe.

## Trust Boundaries

- Browser to Flask API: all metadata, filenames, job IDs, playlist names, paths, and request bodies are untrusted.
- Flask API to filesystem: configured roots such as `/config`, `/data/media/music`, `/data/torrents/music`, playlist staging, caches, and backup paths must be separately authorized and canonicalized.
- Flask API to subprocesses: Beets, ffmpeg/ffprobe/fpcalc, yt-dlp, Git, package managers, and helper runtimes must treat arguments and environments as untrusted.
- Flask API to external services: MusicBrainz, AcoustID, Plex, Discogs, qBittorrent, Lidarr, SLSKD, Spotify, OpenAI/OpenRouter, and download providers can fail, lie, return hostile text, or leak data.
- Flask API to AI provider: prompts and model outputs are not a security boundary. Model output must be schema-validated and re-authorized by deterministic code.
- Container to host: a compromised app can affect every writable mounted path. Volume scope and UID/GID matter.
- Logs/backups/database/browser storage: assume any value written there may later be read by a lower-trust party.

## Attacker Positions

| Position | Capabilities | Consequences If Controls Fail |
|---|---|---|
| Unauthenticated remote client | Sends HTTP requests through public internet, VPN, LAN, reverse proxy, or Docker network. | Settings theft, destructive jobs, plugin execution, library deletion, credential disclosure. |
| Authenticated low-privilege user | Uses a valid app secret or future lower-privilege account. | Horizontal access, job theft, playlist manipulation, destructive escalation. |
| Malicious or compromised reverse proxy | Can add forwarded headers, alter host/scheme, retry requests, and observe traffic. | Local-trust bypass, CSRF/login confusion, host-header poisoning, credential capture. |
| Compromised peer container | Can reach service ports and Docker DNS names. | API abuse, SSRF amplification, credential discovery, lateral movement. |
| Malicious file/archive/metadata | Controls filenames, tags, artwork, playlists, lyrics, cue sheets, and parser inputs. | Path traversal, XSS, parser DoS, shell-argument injection, log injection. |
| Compromised external API | Returns hostile MusicBrainz, AcoustID, Plex, Discogs, downloader, or metadata payloads. | Wrong matches, prompt injection, SSRF redirects, excessive data, poisoned metadata. |
| Malicious provider response | Controls MusicBrainz IDs, Plex fields, downloader names, AI text, or service errors. | Incorrect deletion/replacement, leaked prompts/secrets, unsafe fallback behavior. |
| Controlled identifier/path/URL | Controls artist, album, track, path, URL, job ID, playlist ID, release ID. | IDOR/BOLA, traversal, arbitrary file access, SSRF, job hijacking. |
| Prompt-injection author | Embeds instructions in tags, lyrics, annotations, errors, logs, playlists, or web results. | AI proposes unsafe actions or attempts to exfiltrate secrets. |
| Log/backup/browser/database reader | Reads logs, old backups, DB files, config exports, localStorage/sessionStorage. | Secret reuse, library privacy loss, operational intelligence, replay of tokens. |
| Supply-chain attacker | Controls dependency, GitHub Action tag, image tag, package install script, runtime download, or model. | Code execution at build/startup/runtime and credential compromise. |
| Accidental destructive AI decision | Model or heuristic confidently picks the wrong match. | Wrong deletion, replacement, move, retag, or cleanup loop. |

## Security Objectives

- Every non-public endpoint requires authenticated owner/admin access.
- State-changing browser requests require CSRF/origin protection.
- Secrets are write-only in UI/API responses and absent from committed source.
- File operations resolve canonical paths under explicit roots before mutation.
- Subprocesses receive arrays, bounded timeouts, limited output, and no shell-concatenated untrusted input.
- Server-side URLs are fixed or allowlisted, with DNS/IP/redirect checks before request.
- AI output is treated as untrusted structured advice, never direct authority.
- Destructive workflows expose targets, revalidate immediately, lock/idempotently execute, quarantine where practical, and audit results.
- Containers run non-root with least-privilege mounts and pinned images.
- CI runs security regression tests, secret scanning, and dependency checks.

## Current Residual Risk

The 2026-07-16 hardening pass added global authentication, CSRF/origin protection, secret redaction, static-route path checks, dependency pinning, and CI security checks. Remaining high-risk areas are runtime package/runtime installation, Docker image/mount hardening, centralized SSRF protection, rate limiting, and deeper destructive workflow race/path review.