# Runbook: Relocate the Web Manager data directory

Move the Beets Web Manager persistent data directory (the host folder
bind-mounted at the container's `/web-manager-data`) to a new host path and
repoint the mount there, **reversibly and dry-run first**, using
`scripts/relocate_web_manager_data.sh`.

## What this touches (and what it does not)

- **Moves:** only the `/web-manager-data` bind-mount source — Web Manager's
  own state: settings, login, session key, audit trail, `jobs/`,
  `transactions/`.
- **Never touches:** your music files, or the Beets `/config` database
  (`musiclibrary.blb`). Those are different mounts and are left completely
  alone.

## When and why

Use this when the data directory ended up in an awkward place — for example
**nested inside another checkout or folder** — and you want it on a clean path
outside that folder so the old location can later be archived or removed
without disturbing live data.

The shipped Compose files resolve that mount source from an environment
variable with a default, e.g.:

```yaml
# docker-compose.yml
- ${WEB_MANAGER_DATA_PATH:-${BEETS_WEB_MANAGER_DATA_PATH:-./web-manager}}:/web-manager-data
# docker-compose.full.yml
- ${BEETS_WEB_MANAGER_DATA_PATH:-./web-manager-data}:/web-manager-data
```

So, **by default**, the mount is repointed by setting **one key** in the stack
env file — the Compose YAML is never edited. The script detects which key your
Compose file actually interpolates (the outermost `${VAR}` wins) and sets that
one.

> Which key? `docker-compose.yml` reads `WEB_MANAGER_DATA_PATH` first;
> `docker-compose.full.yml` reads `BEETS_WEB_MANAGER_DATA_PATH`. Let the script
> detect it from your live Compose file rather than assuming.

### Hardcoded mount source (no `${VAR}`)

Some real deployments pin the mount source as a **hardcoded host path** instead
of an interpolated variable:

```yaml
# docker-compose.yml  (no ${VAR} -- a literal host path)
- /srv/apps/beets/web-manager-data:/web-manager-data
```

There is no env key to set here, so the env-file repoint cannot work. Two
options:

- **Let the script do it (recommended):** pass `--rewrite-compose`. The script
  rewrites **only that one mount-source line** in the Compose YAML, after
  writing a `<compose>.bak` of the file first and verifying every other line is
  byte-identical. It is **opt-in** — without the flag the script refuses a
  hardcoded source and tells you to add `--rewrite-compose`. The dry-run shows a
  `REWRITE` plan line before anything is touched, exactly like the env-file
  flow.
- **By-hand fallback (the documented equivalent):** back up, move the data,
  edit the one Compose line, recreate, health-check:

  ```bash
  cp -p docker-compose.yml docker-compose.yml.bak           # backup first
  rsync -aHAX /old/web-manager-data/ /new/web-manager-data/  # move (copy) data
  sed -i 's#^\(\s*- \)/old/web-manager-data:/web-manager-data#\1/new/web-manager-data:/web-manager-data#' docker-compose.yml
  docker compose -f docker-compose.yml up -d beets-web-manager   # recreate
  curl -fsS http://127.0.0.1:8337/api/health                     # health-check
  ```

  `--rewrite-compose` performs exactly this rewrite, but with the full
  backup-first / copy-verify / health-gated recreate / printed-rollback safety
  of the script around it.

## Prerequisites

- Run on the Docker host, from (or pointing at) the stack directory.
- `docker` (for source resolution and recreate), `python3`, `tar`; `rsync` is
  used when present, otherwise `cp -a`.
- Enough free space on the destination filesystem for a full copy of the data
  dir plus one `.tar.gz` backup.

## Procedure

Paths below are placeholders — substitute your own absolute paths.

### 1. Dry-run (default — changes nothing)

```bash
bash scripts/relocate_web_manager_data.sh \
    --stack-dir /path/to/stack \
    --dest /path/to/new/web-manager-data
```

Read the printed PLAN block. Confirm:

- **Source (old)** is the directory you expect (resolved from
  `docker inspect` of the service's `/web-manager-data` mount).
- **Dest (new)** is a clean path *outside* the old folder.
- **Env key to set** and **Env file** are correct for your stack.

### 2. Apply

```bash
bash scripts/relocate_web_manager_data.sh \
    --stack-dir /path/to/stack \
    --dest /path/to/new/web-manager-data \
    --apply
```

What `--apply` does, in order, stopping on any failure:

1. **Backup-first** — writes a timestamped `.tar.gz` of the data dir under
   `<dest parent>/_web-manager-relocate-backups/` (mode 600 — it contains
   secrets), and refuses to continue unless it exists and is non-empty.
2. **Copy** — `rsync -aHAX --numeric-ids` (or `cp -a`) from old to new,
   preserving permissions/ownership/timestamps. The **old directory is left in
   place**.
3. **Verify** — file count and total bytes must match between old and new,
   or the script aborts (and leaves both directories intact).
4. **Repoint** — by default sets exactly one key in the stack env file (other
   keys untouched; no values printed). With `--rewrite-compose` (hardcoded
   source), instead rewrites the single mount-source line in the Compose YAML
   after saving `<compose>.bak` and verifying all other lines are byte-identical.
5. **Recreate + verify** — `docker compose up -d <service>` (only that
   service), waits for the container healthcheck, and checks
   `GET /api/health`. On failure it **stops and prints rollback commands**.

### 3. Verify (out of band)

After it reports SUCCESS, independently confirm:

```bash
curl -fsS http://127.0.0.1:8337/api/health        # beets.ok true
docker inspect --format '{{json .Mounts}}' <container> | grep /web-manager-data
```

- `/api/health` responds and reports embedded Beets healthy.
- The container's `/web-manager-data` mount **Source** is now the new path.
- Your settings/jobs/transactions are present in the UI.

## Rollback

The old data dir is never modified, so rollback is just repointing the mount
back and recreating:

1. Point the mount back to the **old** source (the SUCCESS/rollback output
   prints the exact instruction):
   - env-file mode: set the env key back to the old path;
   - `--rewrite-compose` mode: restore the one mount line from the
     `<compose>.bak` the apply wrote (or set it back to the old path).
2. Recreate only the service:

   ```bash
   docker compose -f /path/to/stack/docker-compose.yml up -d beets-web-manager
   ```

3. Re-check `/api/health`.

The `.tar.gz` backup from step 2.1 is an additional safety net.

## Removing the old directory (after you are satisfied)

Removal is **not** automatic. Once the app is confirmed working on the new
path for long enough to trust it:

```bash
rm -rf -- '/path/to/old/web-manager-data'
```

Or let the script do it in the same run (only after health passes) with
`--remove-old`. The backup still remains.

## Idempotency / re-running

- Re-running a completed move refuses because the destination is now
  non-empty; pass `--resume` to re-sync into it.
- `--no-recreate` performs the move and env repoint but leaves the
  `docker compose up` to you (useful when you orchestrate recreation
  separately). The script prints the command to run.

## Useful flags

| Flag | Purpose |
|---|---|
| `--source DIR` | Skip `docker inspect`; name the old dir explicitly. |
| `--env-key NAME` | Override env-key detection. |
| `--env-file FILE` | Env file to edit (default `<compose dir>/.env`). |
| `--compose-file FILE` | Compose file (default `<stack-dir>/docker-compose.yml`). |
| `--service NAME` | Compose service (default `beets-web-manager`). |
| `--resume` | Allow a non-empty destination (re-sync). |
| `--no-recreate` | Do everything except the docker recreate/health check. |
| `--remove-old` | Delete the old dir after health passes. |
| `--rewrite-compose` | Hardcoded mount source: rewrite the one Compose line in place (writes `<compose>.bak` first). Opt-in. |

Run `bash scripts/relocate_web_manager_data.sh --help` for the full list.
