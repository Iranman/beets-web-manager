# Configuration

Configuration is split between environment variables for the web-manager and control-agent boundary, and `/config/config.yaml` for Beets itself.

## Core variables

| Variable | Service | Required | Meaning |
|---|---|---:|---|
| `BEETS_API_TOKEN` | both | yes | Strong shared secret for authenticated internal HTTP from `beets-web-manager` to `beets`. Blank, weak, and placeholder values are rejected by the control agent. |
| `BEETS_API_URL` | web | yes | Internal control-agent URL, normally `http://beets:8338`. |
| `WEBCONTROL_PORT` | web | no | Web port inside the container, default `8337`; Compose binds it to loopback by default. |
| `BEETS_WEB_AUTH_TOKEN` | web | yes | Owner API/script token. The app can auto-generate a secure token when only the `changeme` placeholder or no usable owner credential is present. |
| `BEETS_WEB_PASSWORD` | web | optional | Browser Basic Auth password. Must satisfy the configured strength rules. |
| `BEETS_WEB_USERNAME` | web | optional | Browser Basic Auth username, default `admin`. |
| `BEETS_OUTBOUND_ALLOWLIST` | web | optional | Comma-separated host:port or CIDR:port entries for private services the web manager may contact. Defaults to the internal Beets control agent, `beets:8338`; add Plex, Lidarr, SLSKD, or helper hosts explicitly. |
| `BEETS_TRUSTED_PROXIES` | web | optional | Proxy CIDRs whose forwarded client IP headers may be trusted. |
| `BEETS_ENABLE_LEGACY_LOCAL_SCAN` | web | optional | Opt-in guard for the old local `/api/library/scan` filesystem walk. Keep `0` in the external-engine architecture unless using a legacy single-process development setup. |
| `BEETS_SCAN_STATE_FILE` | web | optional | State file for the legacy local scan guard, default `/web-manager-data/last_scan.txt`. |

## Optional integrations

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `AI_API_KEY`, `AI_BASE_URL`, `AI_MODEL` | Optional AI-assisted candidate ranking. Matching still uses MusicBrainz and AcoustID evidence without AI. |
| `ACOUSTID_API_KEY` | Set on the Beets engine service. Required for the engine's `/audio/acoustid-lookup` adapter (runs `fpcalc` and queries `api.acoustid.org` directly -- not a Chroma plugin API call; see ARCH-013). Without it, fingerprint lookups report `unavailable`, not a false no-match. |
| `ACOUSTID_KEY` | Set on the web-manager service. Used only for `beet submit` fingerprint-submission config, not for lookups. |
| `DISCOGS_TOKEN`, `DISCOGS_USER_TOKEN` | Optional Discogs metadata. |
| `LISTENBRAINZ_TOKEN` | Optional ListenBrainz integration. |
| `PLEX_URL`, `PLEX_TOKEN` | Optional Plex sync and refresh. Use deployment-specific URLs, not committed private LAN defaults. |
| `LIDARR_URL`, `LIDARR_API_KEY` | Optional wanted-album integration. |
| `SLSKD_URL`, `SLSKD_API_KEY`, `SLSKD_API_KEY_FILE`, `SLSKD_SLSK_USERNAME`, `SLSKD_SLSK_PASSWORD` | Optional SLSKD/Soulseek acquisition. |
| `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` | Optional Spotify playlist parsing. |
| `YTDLP_PO_PROVIDER_URL`, `YTDLP_COOKIE_FILE`, `YTDLP_ALLOW_BROWSER_COOKIES`, `YTDLP_NETRC_FILE` | Optional direct-source helper configuration. |
| `DEMO_MODE` | Marks setup/status responses as demo data when using the synthetic demo library. |

## Beets config

The authoritative Beets config is `/config/config.yaml` in the `beets` engine container. The web manager does not own a second Beets config.

The engine image includes the bundled `discpath` plugin under `/opt/beets-web-manager-agent/beetsplug`; user plugins can be mounted under `/config/beetsplug`. Configure `pluginpath` so `/config/beetsplug` is searched before the bundled path.

`/api/health` is a web-manager liveness check. `/health/ready` and `/api/setup/status` query the remote control agent for Beets readiness and fail closed when the engine is unreachable or rejects authentication.

## Submission and fingerprinting terms

- Fingerprinting: local audio fingerprint generation through Chroma/fpcalc in the Beets engine.
- AcoustID lookup: querying AcoustID for candidate recordings from a fingerprint.
- `submit`: Beets/Chroma command for AcoustID submission readiness. Requires Chroma loaded and the command registered.
- `mbsubmit`: MusicBrainz submission command readiness. It is independent of `submit`.

The Beets 2.4.0 engine image carries a temporary Chroma/BPSync plugin-loader compatibility patch. It must remain pinned to the affected version and be removed after upgrading to a fixed upstream Beets release.
