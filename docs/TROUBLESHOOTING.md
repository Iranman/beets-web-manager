# Troubleshooting

## `beets` is unhealthy or exits on startup

Check `BEETS_API_TOKEN`. The control agent rejects blank, weak, and placeholder tokens. Generate a strong value, update `.env`, then recreate both services.

```bash
docker compose logs beets
docker compose config
docker compose up -d --force-recreate beets beets-web-manager
```

## `/api/setup/status` reports Beets unavailable

`/api/setup/status` fails closed when the web manager cannot query the authenticated internal control agent. Check:

- `BEETS_API_URL=http://beets:8338` in the web-manager environment
- matching `BEETS_API_TOKEN` values in both services
- `beets` service health
- no host publication of port `8338`

The web-manager container must not fall back to a local `beet` executable.

## Chroma or AcoustID readiness is unavailable

Chroma readiness comes from the Beets engine's loaded-plugin list, not local Python importability. Confirm the engine can load Chroma and that `fpcalc` is available:

```bash
docker compose exec beets /lsiopy/bin/beet version
curl -s http://127.0.0.1:8337/api/setup/status
```

AcoustID lookup readiness requires Chroma loaded, `fpcalc`, and pyacoustid. AcoustID submission readiness also requires the `submit` command to be registered and appropriate submission configuration. `mbsubmit` is a separate MusicBrainz capability.

## FetchArt, ReplayGain, Discpath, or other plugin readiness looks wrong

Setup status reports configured plugins, loaded plugins, plugin failures, and capability flags from the remote Beets engine. If a plugin is configured but not loaded, inspect the Beets engine logs and `beet version` output. Do not treat a Python `find_spec()` result in the web-manager process as proof that a Beets plugin is operational.

## The web UI is unreachable

By default the web port binds to loopback only:

```text
127.0.0.1:${WEBCONTROL_PORT:-8337}:8337
```

Use a reverse proxy for remote access. Configure authentication, TLS, trusted proxies, and outbound allowlist entries before exposing the UI beyond localhost.

## Plex or Lidarr integration is not configured

The example files intentionally ship with blank `PLEX_URL` and `LIDARR_URL`. Set deployment-specific URLs and tokens in `.env`. Private LAN addresses must not be committed as active defaults.

## MusicBrainz, AcoustID, AI, or Plex connectivity tests fail

`GET /api/setup/status` is a readiness snapshot. It does not contact external providers. Use `POST /api/setup/test/{ai,musicbrainz,acoustid,plex}` or the System page connection tests for live provider connectivity.

## Migration or rollback

Do not test migration against a production library first. Use disposable volumes and synthetic config, then back up `/config/config.yaml`, `/config/musiclibrary.blb`, plugin files, and web-manager state before touching production data. Passing tests is not deployment approval.