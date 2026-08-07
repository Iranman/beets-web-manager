# TrueNAS Rollout: Beets Web Manager

Guarded rollout procedure for deploying Beets Web Manager onto a TrueNAS
Docker Compose stack, and for cleaning up the stale, web-manager-created
SQLite database left behind by pre-#64 installs. The tooling for this is
`scripts/deploy_truenas_web_manager.sh` -- reusable across releases via its
`VERSION` env var, not tied to one release in its filename.

## Which version to deploy

Always deploy a tagged, published release of `beets-web-manager` (e.g. `0.1.5`, `0.1.6`, `1.0.0`).

```bash
docker pull ghcr.io/iranman/beets-web-manager:<VERSION>
docker image inspect ghcr.io/iranman/beets-web-manager:<VERSION> \
  --format '{{json .Config.Labels}}'
# org.opencontainers.image.revision must equal the release commit you expect
# org.opencontainers.image.version  must equal <VERSION>
```

Do not deploy moving aliases such as `latest`, `stable`, or `edge`. `VERSION` must be an explicit numbered release.

## Expected host layout (verify, don't trust)

```text
$STACK_DIR                                                  stack directory
$STACK_DIR/beets/musiclibrary.blb                            authoritative DB
$STACK_DIR/beets-web-manager/                                web-manager state dir
$STACK_DIR/beets-web-manager/musiclibrary.blb                 stale DB (if present)
$STACK_DIR/beets-web-manager/.auth_token                      persistent web token
```

**These paths are never trusted as given.** The script resolves the real
bind-mount sources from `docker inspect beets` (destination `/config`) and
`docker inspect beets-web-manager` (destination `/web-manager-data`) before
touching anything, and refuses to continue if either source can't be
determined, if they're the same path, or if the authoritative/stale DB paths
land somewhere unexpected relative to those sources.

## Dry run first, always

To execute on TrueNAS or any host with a `noexec` filesystem policy on `/tmp`, invoke the script explicitly via `/bin/bash`:

```bash
STACK_DIR=/mnt/PLEX/Apps/Arrs VERSION=0.1.5 EXPECTED_REVISION=8ae9bf0cc742d036d5ca460cf47db7e6edbda989 /bin/bash /path/to/deploy_truenas_web_manager.sh --dry-run
```

Performs every safety check (mount discovery, authoritative + stale DB
inspection, token inspection, Compose/image resolution and label
verification, backup-destination planning, endpoint reachability) and
**changes nothing**: no container is stopped or recreated, no file is moved,
no token is copied, the Compose file is never modified, and nothing is
written outside a `mktemp -d` temp directory. Use this in CI/automated
checks; it's what `tests/test_deploy_truenas_rollout.py` exercises.

## Real rollout

```bash
STACK_DIR=/mnt/PLEX/Apps/Arrs VERSION=0.1.5 EXPECTED_REVISION=8ae9bf0cc742d036d5ca460cf47db7e6edbda989 /bin/bash /path/to/deploy_truenas_web_manager.sh
```

Order of operations -- nothing in the second half runs unless every check in
the first half passes:

1. **Pre-flight (read-only):** resolve Compose file and container mounts;
   verify Compose resolves the `beets-web-manager` service to exactly
   `ghcr.io/iranman/beets-web-manager:${VERSION}`; verify the authoritative
   database (`PRAGMA quick_check` = `ok`, readable item/album counts, not
   suspiciously low, distinct path/inode/checksum from any stale DB);
   inspect the stale DB if present (same checks, plus refusing anything that
   looks like real library data); inspect the persistent auth token; plan
   (not create) the backup directory.
2. **Backup:** timestamped directory under
   `$STACK_DIR/_backups/web-manager-rollout-YYYYMMDD-HHMMSS/`,
   `chmod 700`, containing the Compose file, `.env` (if present), resolved
   `docker compose config`, the current container's `docker inspect` output,
   the previous image ID/ref and its labels, token metadata (contents never
   printed), and the authoritative DB's metadata (path/size/checksum/counts --
   **never a copy of the authoritative DB itself**).
3. **Stop** only `beets-web-manager`, confirm it stopped.
4. **Archive the stale DB** (only after confirming, via `lsof`/`fuser`, that
   the exact files `musiclibrary.blb`, `musiclibrary.blb-wal`,
   `musiclibrary.blb-shm` are not open by anything) -- moved into the backup
   directory's `stale-database/` subfolder. Never `rm`, never a
   `musiclibrary.blb*` wildcard. Missing WAL/SHM is fine.
5. **Migrate the auth token** only if the persistent token is missing *and*
   a legacy token is found under an old `/config`-style mount the
   web-manager container still has -- guarded: refuses to overwrite an
   existing destination, refuses if the legacy value equals
   `BEETS_API_TOKEN` (never uses the Beets engine's API token as the web
   auth token), copies atomically with a checksum check, `chmod 600`.
6. **Pull and verify** the pinned image (`BEETS_WEB_MANAGER_VERSION` is
   overridden in-process for this run only -- your `.env` is never edited),
   check its `org.opencontainers.image.{version,revision}` labels before
   recreating anything.
7. **Recreate only `beets-web-manager`** (`--no-deps --force-recreate`);
   every other Compose service's container ID is snapshotted before and
   after and asserted unchanged -- the Beets engine, Plex, Lidarr, etc. are
   never touched.
8. **Post-deploy verification:** re-run the authoritative DB's
   quick_check/counts/checksum and assert byte-for-byte unchanged; restart
   `beets-web-manager` a second time and confirm the token checksum survives
   the restart; confirm no `musiclibrary.blb*` file reappeared under
   `/web-manager-data`; run the endpoint checks below.

## Endpoint verification

Each of `/api/health`, `/api/setup/status`, `/api/library?limit=1`,
`/api/library?limit=50` is hit three times. Status, response time, and
response size are logged for every attempt -- slow responses are reported,
never hidden. `pagination.total` from `/api/library?limit=1` is compared
against the authoritative item count recorded in step 1 (never
hard-coded). Token values are read into a shell variable only for the
`Authorization` header and immediately `unset`; never written to stdout,
logs, or shell history.

## Rollback

```bash
/bin/bash /path/to/deploy_truenas_web_manager.sh --rollback "$STACK_DIR/_backups/web-manager-rollout-YYYYMMDD-HHMMSS"
```

Stops only `beets-web-manager`, restores the prior Compose file, restores or
removes the persistent token according to token migration metadata and recorded
checksums, recreates `beets-web-manager` on the previous image reference, and
verifies health and token checksum. Stale database files are **left archived**
by default -- current-architecture code never reads them, so restoring them is
a no-op at best. Pass `RESTORE_STALE_DB=1` to restore them anyway (e.g. rolling
back to a pre-#64 image that still depends on the local-DB fallback). The Beets
engine and its database are never touched or recreated by rollback.

## Configuration knobs

`STACK_DIR` and `VERSION` are required (no generic default makes sense for a host-specific path or version). All other environment variables are optional:

```text
STACK_DIR              required, no default (your deployment's stack directory)
VERSION                required, no default (e.g. 0.1.5, 0.1.6, 1.0.0)
EXPECTED_REVISION       strongly recommended; when set, validates exact Git commit revision label
SERVICE                default beets-web-manager
ENGINE_SERVICE          default beets
COMPOSE_FILE            auto-detected (docker-compose.arrs.yml, then docker-compose.yml)
MIN_ITEM_COUNT          default 10 -- raise to your real library size
STALE_DB_MAX_ITEMS      default 100000
HEALTH_TIMEOUT_SECONDS  default 120
ENDPOINT_BASE_URL       default http://127.0.0.1:8337
RESTORE_STALE_DB        rollback only, default 0
```

## Testing

```bash
/bin/bash -n scripts/deploy_truenas_web_manager.sh
python -m unittest tests.test_deploy_truenas_rollout -v
```

`tests/test_deploy_truenas_rollout.py` covers safety checks directly
(by sourcing the script's functions against real temp SQLite files) and
end-to-end dry-run/deploy/rollback flows against a fake `docker`/`docker compose`/`curl`
(`tests/deploy/fake_docker.py`, `tests/deploy/fake_curl.py`) driven by a JSON world-state file. No real Docker daemon or TrueNAS host is used. Passing these tests is necessary but not sufficient for production confidence -- validate against a disposable two-container Compose environment before touching the real stack.
