# TrueNAS Rollout: Beets Web Manager

Guarded rollout procedure for deploying Beets Web Manager onto a TrueNAS
Docker Compose stack, and for cleaning up the stale, web-manager-created
SQLite database left behind by pre-#64 installs. The tooling for this is
`scripts/deploy_truenas_web_manager.sh` -- reusable across releases via its
`VERSION` env var, not tied to one release in its filename.

## Which version to deploy

```text
v0.1.3  Contains PR #64 only (fix(architecture): remove local db fallback,
        fix auth persistence and api performance). Does NOT contain PR
        #65's fixes below.
        Image: ghcr.io/iranman/beets-web-manager:0.1.3
        Revision: 36bfc7554378a9ef6bd8f9c47a7d1be553647503

v0.1.4  Required for PR #65's corrections: the get_db_connection()
        remote-only regression fix, the auth-token log-leak fix (a
        generated token is no longer printed anywhere -- read it from its
        persisted file instead) plus fail-closed startup when persistence
        can't be verified, and the /api/setup/status cache hardening
        (monotonic clock, single-flighted rebuilds, invalidation on config
        change).
        Image (once published): ghcr.io/iranman/beets-web-manager:0.1.4
        NOT tagged/published yet -- do not deploy that image reference
        until it actually exists; VERSION=0.1.4 is this script's default
        precisely so it's ready to run the moment it does.
```

**The hardened rollout in this document targets `v0.1.4`, not `v0.1.3`.**
Deploying `0.1.3` with this tooling gets you the guarded rollout mechanics
(mount discovery, DB/token safety checks, backup, rollback) but not PR
#65's application-level fixes -- those only ship in `0.1.4`. Do not retag or
otherwise modify the existing `v0.1.3` tag/image to "backport" them.

## Release identity

```text
PR #64 merge commit:       4aed0c6af83880afbef4192c8d2e97f605b48b10
                            fix(architecture): remove local db fallback,
                            fix auth persistence and api performance

Follow-up test commit:     36bfc7554378a9ef6bd8f9c47a7d1be553647503
                            test(integration): patch _db in
                            test_post_retag_artwork_integration to use test
                            temp SQLite library

v0.1.3 release tag:        points at 36bfc75, which is 4aed0c6 plus the
                            follow-up test commit on top
v0.1.3 image:               ghcr.io/iranman/beets-web-manager:0.1.3

PR #65:                     fix(deploy): harden v0.1.3 TrueNAS rollout,
                            token persistence, and status cache
v0.1.4:                     not yet tagged -- create only after PR #65 is
                            merged to main AND post-merge CI is green
```

**36bfc75 is not the PR #64 merge commit** -- it is a small follow-up test
correction committed on `main` after the PR merged. `v0.1.3` includes both.
Verify before every rollout, don't trust this file blindly:

```bash
git merge-base --is-ancestor 4aed0c6af83880afbef4192c8d2e97f605b48b10 v0.1.3
git merge-base --is-ancestor 36bfc7554378a9ef6bd8f9c47a7d1be553647503 v0.1.3

docker pull ghcr.io/iranman/beets-web-manager:<VERSION>
docker image inspect ghcr.io/iranman/beets-web-manager:<VERSION> \
  --format '{{json .Config.Labels}}'
# org.opencontainers.image.revision must equal the release commit you expect
# org.opencontainers.image.version  must equal <VERSION>
```

## Expected host layout (verify, don't trust)

```text
/mnt/PLEX/Apps/Arrs                                        stack directory
/mnt/PLEX/Apps/Arrs/beets/musiclibrary.blb                  authoritative DB
/mnt/PLEX/Apps/Arrs/beets-web-manager/                      web-manager state dir
/mnt/PLEX/Apps/Arrs/beets-web-manager/musiclibrary.blb       stale DB (if present)
/mnt/PLEX/Apps/Arrs/beets-web-manager/.auth_token            persistent web token
```

**These paths are never trusted as given.** The script resolves the real
bind-mount sources from `docker inspect beets` (destination `/config`) and
`docker inspect beets-web-manager` (destination `/web-manager-data`) before
touching anything, and refuses to continue if either source can't be
determined, if they're the same path, or if the authoritative/stale DB paths
land somewhere unexpected relative to those sources.

## Dry run first, always

```bash
scripts/deploy_truenas_web_manager.sh --dry-run
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
scripts/deploy_truenas_web_manager.sh
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
   `/mnt/PLEX/Apps/Arrs/_backups/web-manager-rollout-YYYYMMDD-HHMMSS/`,
   `chmod 700`, containing the Compose file, `.env` (if present), resolved
   `docker compose config`, the current container's `docker inspect` output,
   the previous image ID/ref and its labels, the current token (copy +
   metadata, contents never printed), and the authoritative DB's metadata
   (path/size/checksum/counts -- **never a copy of the authoritative DB
   itself**).
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
scripts/deploy_truenas_web_manager.sh --rollback /mnt/PLEX/Apps/Arrs/_backups/web-manager-rollout-YYYYMMDD-HHMMSS
```

Stops only `beets-web-manager`, restores the prior Compose file, restores
the pre-migration token only if its checksum still matches what was
recorded (never silently overwrites a token that changed since), recreates
`beets-web-manager` on the previous image reference, and verifies health and
token checksum. Stale database files are **left archived** by default --
current-architecture code never reads them, so restoring them is a no-op at
best. Pass `RESTORE_STALE_DB=1` to restore them anyway (e.g. rolling back to
a pre-#64 image that still depends on the local-DB fallback). The Beets
engine and its database are never touched or recreated by rollback.

## Configuration knobs

All optional environment variables:

```text
STACK_DIR              default /mnt/PLEX/Apps/Arrs
SERVICE                default beets-web-manager
ENGINE_SERVICE          default beets
VERSION                 default 0.1.4 (not yet released -- see "Which version to deploy" above)
EXPECTED_REVISION       unset by default; when unset the revision-label pin is
                        skipped (warning only) and only the version label is
                        checked. Set explicitly once VERSION's release commit
                        is known, e.g. EXPECTED_REVISION=<v0.1.4's commit sha>
COMPOSE_FILE            auto-detected (docker-compose.arrs.yml, then docker-compose.yml)
MIN_ITEM_COUNT          default 10 -- raise to your real library size
STALE_DB_MAX_ITEMS      default 100000
HEALTH_TIMEOUT_SECONDS  default 120
ENDPOINT_BASE_URL       default http://127.0.0.1:8337
RESTORE_STALE_DB        rollback only, default 0
```

## Testing

```bash
bash -n scripts/deploy_truenas_web_manager.sh
shellcheck scripts/deploy_truenas_web_manager.sh
python -m unittest tests.test_deploy_truenas_rollout -v
```

`tests/test_deploy_truenas_rollout.py` covers the safety checks directly
(by sourcing the script's functions against real temp SQLite files -- no
Docker needed) and the end-to-end dry-run/deploy/rollback flows against a
fake `docker`/`docker compose`/`curl` (`tests/deploy/fake_docker.py`,
`tests/deploy/fake_curl.py`) driven by a JSON world-state file. No real
Docker daemon or TrueNAS host is used. Passing these tests is necessary but
not sufficient for production confidence -- validate against a disposable
two-container Compose environment before touching the real stack.

Requires a working POSIX `bash` on PATH (true on Linux CI/TrueNAS, and in a
correctly configured Git-Bash-first Windows PATH). On a Windows dev machine
where a `C:\WINDOWS\system32\bash.exe` WSL-launcher stub shadows Git for
Windows' real bash (seen from a plain PowerShell session with no Linux
distro registered), point the test suite at the real one:
`$env:ROLLOUT_TEST_BASH = 'C:\Program Files\Git\bin\bash.exe'`.
