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
STACK_DIR=/path/to/docker-stack VERSION=<VERSION> EXPECTED_REVISION=<RELEASE_COMMIT_SHA> /bin/bash /path/to/deploy_truenas_web_manager.sh --dry-run
```

Performs every safety check (mount discovery, authoritative + stale DB
inspection, token inspection, Compose/image resolution and label
verification, backup-destination planning, endpoint reachability, and the
current `/api/setup/status` blocking reasons). It stops, recreates, moves,
copies and edits nothing in the stack: no container is stopped or
recreated, no file under `STACK_DIR` is written, and neither the Compose
file nor `.env` is touched. What it does do outside the stack:

- `docker compose pull` of the web-manager service, so the image's labels
  can be checked. The new image lands in the host's local image store.
- Short-lived `mktemp` files for HTTP probe bodies and the planned backup
  path. They are removed or left in the system temp directory, never in
  `STACK_DIR`.
- Read-only `docker exec` into the Beets container for the semantic
  snapshot, and authenticated GETs against the web manager.

`tests/test_deploy_truenas_rollout.py` exercises it.

## Real rollout

```bash
STACK_DIR=/path/to/docker-stack VERSION=<VERSION> EXPECTED_REVISION=<RELEASE_COMMIT_SHA> /bin/bash /path/to/deploy_truenas_web_manager.sh
```

Order of operations -- nothing in the second half runs unless every check in
the first half passes:

1. **Pre-flight (read-only):** resolve Compose file and container mounts;
   verify Compose resolves the `beets-web-manager` service to exactly
   `ghcr.io/iranman/beets-web-manager:${VERSION}`; take the **online
   semantic snapshot** of the running library (see "Database integrity"
   below: counts not suspiciously low, identity digest, plugin healthy) and
   check the authoritative DB file is distinct in path/inode/checksum from
   any stale DB;
   inspect the stale DB if present (same checks, plus refusing anything that
   looks like real library data); inspect the persistent auth token; record
   `/api/setup/status` (status and `blocking_reasons`); plan (not create)
   the backup directory.
2. **Backup:** timestamped directory under
   `$STACK_DIR/_backups/web-manager-rollout-YYYYMMDD-HHMMSS/`. Directories
   are mode 700 and files mode 600. See "Backup contents" below.
3. **Stop** only `beets-web-manager`, confirm it stopped, then copy its
   state files and the Beets config into the backup (they are quiescent now).
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
   exported for this run's own `docker compose` calls), check its
   `org.opencontainers.image.{version,revision}` labels before recreating
   anything.
7. **Recreate only `beets-web-manager`** (`--no-deps --force-recreate`);
   every other Compose service's container ID is snapshotted before and
   after and asserted unchanged -- Plex, Lidarr, etc. are never touched.
8. **Post-deploy verification:**
   - take the online semantic snapshot again and assert counts and the
     identity digest are unchanged (the live main-file hash is logged for
     reference only -- see below);
   - **plugin refresh:** Web Manager has just copied its bundled webmanager
     plugin into the engine's `/config/beetsplug`, but the running Beets
     process still has the plugin it imported when it started. If
     `PLUGIN_VERSION` in the provisioned `beetsplug/webmanager/version.py`
     differs from the `plugin_version` the engine reports on
     `/webmanager/status`, the script restarts the `beets` service (same
     container, never recreated), waits for the plugin to answer again,
     asserts items, albums and the identity digest are unchanged and the
     engine now reports the provisioned version, and records it in
     `engine-plugin.txt`. If the versions match, Beets is not touched;
   - restart `beets-web-manager` a second time and confirm the token
     checksum survives the restart; confirm no `musiclibrary.blb*` file
     reappeared under `/web-manager-data`; run the endpoint checks below;
   - **setup readiness:** fetch `/api/setup/status` again. A blocking reason
     that was not there before the deploy fails the rollout (the new
     version is left running; the failure block prints the `--rollback`
     command). If the "before" status could not be read, any blocking
     reason fails it.
9. **Persist the version:** only after every check above passed, the
   script **edits the stack `.env`**: it rewrites the
   `BEETS_WEB_MANAGER_VERSION=` line to the deployed version (or appends
   one), leaving every other line as it was, and sets the file to mode 600.
   Without this, the next `docker compose up -d` that does not go through
   this script (a host reboot, a stack-wide refresh) would bring back the
   old version. A copy of the original `.env` is in the backup. With no
   `.env` next to the Compose file, nothing is written and a warning says so.

### Backup contents

| Path in the backup | What | Used by rollback |
|---|---|---|
| `docker-compose.yml.bak` | the Compose file | restored as-is |
| `.env.bak` | the stack `.env`, verbatim (contains secrets) | its `BEETS_WEB_MANAGER_VERSION` line |
| `previous-image.txt`, `previous-image-labels.json` | image ref, image ID and labels that were running | the target and the proof |
| `auth_token.bak`, `token-metadata.txt` | the web auth token and its checksum metadata | guarded token restore |
| `web-manager-data/` | `.env` (Settings), `.browser_username`, `.browser_password`, `.flask_secret_key`, `.setup_complete`, `.browser_setup_state`, `transactions/` | restored (see Rollback) |
| `beets-config/` | Beets `config.yaml` and `beetsplug/` | restored |
| `state-manifest.txt` | sha256 / file counts of the two folders above | presence check |
| `setup-status-before.json` | status and blocking reasons before the deploy | diagnostics |
| `authoritative-db-metadata.txt` | DB path, size, live main-file hash, counts | diagnostics |
| `container-inspect-before.json`, `resolved-compose-config.json` | `docker inspect` and `docker compose config` with every environment **value redacted** except a short allowlist of non-secret keys (`PUID`, `TZ`, `BEETS_WEB_URL`, ...); key names are kept | diagnostics only |
| `stale-database/` | archived pre-#64 database, if one existed | `RESTORE_STALE_DB=1` |
| `engine-plugin.txt` | written when Beets was restarted for a plugin change | diagnostics |

The authoritative library database (`musiclibrary.blb` and its `-wal`/`-shm`)
is **never** copied into the backup. Use `--offline-db-identity` for its byte
identity and `scripts/backup.sh` (or storage snapshots) for a copy.

## Database integrity: online semantics vs. offline bytes

Beets runs its SQLite library in WAL mode: committed changes can sit in
`musiclibrary.blb-wal` until a checkpoint copies them into
`musiclibrary.blb`. A hash of the main file taken while Beets runs therefore
proves nothing either way -- it can change with no logical change (a
checkpoint) and stay identical while the WAL holds new data. The rollout
never calls a live main-file hash "unchanged database"; it logs it as
informational only.

**Online semantic integrity** (every dry run and deploy): inside the Beets
container, through its own web API and never by opening SQLite, the script
reads `/stats` (item/album counts), `/item/` and `/album/` (an identity
digest over every item's id, album row, Recording/Release/Release Group
IDs, disc, track and path, and every album's IDs), and `/webmanager/status`
(plugin health; the plugin key is read inside the container and never
printed). Deploy fails if counts or the digest change.

**Offline byte identity** (explicit, separate mode; briefly stops Beets):

```bash
STACK_DIR=/path/to/docker-stack /bin/bash deploy_truenas_web_manager.sh --offline-db-identity
# optionally BASELINE_DB_SHA256=<hex> to compare with an earlier run
```

It takes a semantic snapshot, stops only the `beets` service gracefully so
SQLite closes and settles its WAL, records whether `-wal`/`-shm` exist and
their sizes, and only when the WAL is absent or empty runs `PRAGMA
quick_check` and hashes `musiclibrary.blb` -- that hash is the database's
byte identity. If the WAL still holds data it refuses to claim identity.
Beets is always restarted (also on any failure) and the plugin health and
identity digest are verified again before it reports.

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
STACK_DIR=/path/to/docker-stack /bin/bash /path/to/deploy_truenas_web_manager.sh --rollback "$STACK_DIR/_backups/web-manager-rollout-YYYYMMDD-HHMMSS"
```

1. Stops only `beets-web-manager` and restores the prior Compose file.
2. Restores or removes the persistent token according to the token
   migration metadata and recorded checksums.
3. Restores `web-manager-data/` and `beets-config/` from the backup. Every
   file it replaces is first kept under `<backup>/pre-rollback-<timestamp>/`.
   A state file that did not exist before the deploy is moved there, not
   deleted. `transactions/` is the mutation audit trail: missing records are
   added back, but records written after the deploy are never overwritten or
   removed. Backups made by older versions of this script have no state
   folders; the script warns and leaves the state as it is.
4. **Edits the stack `.env`**: sets `BEETS_WEB_MANAGER_VERSION=` back to the
   value in `.env.bak` (or, if that had none, to the tag of the previous
   image). No other line changes.
5. Recreates `beets-web-manager` on the previous image reference through a
   temporary Compose override file. A failure here is fatal and printed.
6. **Proves the result**, failing loudly on any mismatch:
   - the container's running image ID equals the recorded previous image ID,
     and its configured image is the previous reference;
   - `docker compose config` (the files on disk, nothing exported) resolves
     the service to the previous reference, so a later plain
     `docker compose up -d` keeps it;
   - `/health/live` reports the previous image's version label.
7. Runs the same plugin refresh as a deploy: if the engine still has the
   newer plugin loaded, `beets` is restarted (same container) with semantic
   snapshots before and after.

Stale database files are **left archived** by default -- current-architecture
code never reads them. Pass `RESTORE_STALE_DB=1` to restore them anyway (e.g.
rolling back to a pre-#64 image that still depends on the local-DB fallback).
The library database is never touched or recreated by rollback.

## Backup retention (opt-in)

Rollout backups are never deleted automatically. To delete this script's own
backup directories older than a number of days:

```bash
STACK_DIR=/path/to/docker-stack /bin/bash deploy_truenas_web_manager.sh --prune-backups-older-than 90 --dry-run   # list only
STACK_DIR=/path/to/docker-stack /bin/bash deploy_truenas_web_manager.sh --prune-backups-older-than 90
```

Only directories named exactly `web-manager-rollout-YYYYMMDD-HHMMSS` under
`BACKUP_ROOT` are candidates, and their age comes from the timestamp in the
name. The newest backup is always kept, and a backup that holds an archived
stale database (`stale-database/`) is never deleted by this command (review
and remove it by hand). These backups contain `.env.bak` and token copies, so
pruning old ones also limits how long old secrets stay on disk.

## Configuration knobs

`STACK_DIR` is required for every mode except `--help`. `VERSION` is required and validated only for `--dry-run` and the real rollout -- `--rollback` restores whatever image reference the backup itself recorded, so it never needs one. All other environment variables are optional:

```text
STACK_DIR              required (your deployment's stack directory), no default
VERSION                required for --dry-run/rollout only, no default (e.g. 1.0.0)
EXPECTED_REVISION       strongly recommended; when set, validates exact Git commit revision label
SERVICE                default beets-web-manager
ENGINE_SERVICE          default beets
COMPOSE_FILE            auto-detected: tries a legacy `docker-compose.arrs.yml` name first (a combined-stack convention from one earlier deployment, not a Beets Web Manager requirement), then `docker-compose.yml`. Set this explicitly -- to whatever your own Compose file is actually named -- rather than relying on auto-detection.
MIN_ITEM_COUNT          default 10 -- raise to your real library size
STALE_DB_MAX_ITEMS      default 100000
HEALTH_TIMEOUT_SECONDS  default 120
ENDPOINT_BASE_URL       default http://127.0.0.1:8337
BACKUP_ROOT             default $STACK_DIR/_backups
RESTORE_STALE_DB        rollback only, default 0
```

The Compose file must pin the web manager through the version variable, for
example `image: ghcr.io/iranman/beets-web-manager:${BEETS_WEB_MANAGER_VERSION:-stable}`;
a hard-coded tag makes the pre-flight image check fail.

## Testing

```bash
/bin/bash -n scripts/deploy_truenas_web_manager.sh
python -m unittest tests.test_deploy_truenas_rollout -v
```

`tests/test_deploy_truenas_rollout.py` covers safety checks directly
(by sourcing the script's functions against real temp SQLite files) and
end-to-end dry-run/deploy/rollback flows against a fake `docker`/`docker compose`/`curl`
(`tests/deploy/fake_docker.py`, `tests/deploy/fake_curl.py`) driven by a JSON world-state file. No real Docker daemon or TrueNAS host is used. Passing these tests is necessary but not sufficient for production confidence -- validate against a disposable two-container Compose environment before touching the real stack.
