# Upgrading The Stock Beets Image Version

Beets Web Manager has no custom Beets image and no control agent — the sole
Beets runtime is the published `lscr.io/linuxserver/beets` image, referenced
by tag in `docker-compose.yml` (`:latest` by default; pin an exact tag or
digest for reproducibility). This document describes what actually happens
when that image's Beets version changes on a deployment with an existing
library, and how to do it safely. It is not specific to any one version pair
— the same steps apply to any future Beets version bump.

## What actually happens on first open

Opening an existing `musiclibrary.blb` with a newer Beets version can
trigger Beets' own automatic, one-time schema migration (this is Beets'
own behavior, entirely internal to the `beets` container — Web Manager is
not involved and has no way to see or control it). Verified once against a
real version jump (2.4.0 → 2.13.1, using a synthetic, disposable database):

- Data integrity: item/album/artist counts and total size were identical
  before and after migration in testing.
- The migration is **not free**: it created 11 full-size `.bak` sidecar
  files next to the library (e.g.
  `musiclibrary.blb-before-items-multi_genre_field.bak`), each the same
  size as the database at that point. **Ensure `/config` has free space for
  at least ~11-12x the current `musiclibrary.blb` size before the first
  start of a newer Beets version against a real library.**
- The migration is idempotent: a second run against the same
  already-migrated database creates zero additional backups.
- **This migration is not version-reversible.** Beets itself has no
  "downgrade schema" command. An older Beets binary is not guaranteed to
  open a database migrated by a newer one. Rollback means restoring a
  pre-migration snapshot/backup, not just swapping the image tag back.
- The migration runs on the **first Beets invocation of any kind** against
  the real database under the new version — not only an import. Even a
  read-only status/version check triggers it. Treat starting the new image
  against the real `/config` mount as the point of no return.

## Pre-upgrade checklist

1. Stop Web Manager mutation jobs (imports, cleanup, retag, playlist-repair
   jobs). Confirm no job is `running` or `queued` in the Jobs view.
2. Back up, from the real `/config` mount:
   - `config.yaml`
   - `musiclibrary.blb`
   - any custom plugins under `/config/beetsplug`
   - any transaction/undo state Web Manager keeps under `/web-manager-data`
3. Create a filesystem/volume snapshot (ZFS, LVM, or equivalent) of the
   volume backing `/config` immediately before pulling the new image. This
   is the actual rollback target — the file copies in step 2 are a second,
   independent safety net, not a replacement for it.
4. Record the currently running `beets` image's digest
   (`docker inspect --format '{{.Image}}' beets`) so the exact prior image
   can be re-pulled if needed.
5. Ensure `/config`'s filesystem has free space for the one-time backup
   proliferation described above.

## Upgrade steps

6. Pull the new `beets` image tag/digest and recreate just that service
   (`docker compose pull beets && docker compose up -d beets`). Do not
   re-enable Web Manager mutation jobs yet.
7. This step is the point of no return described above — the schema
   migration, if any, runs here.
8. Verify: container health, `docker compose exec beets beet version`
   reports the expected version, the loaded plugin list matches
   expectations, and `docker compose exec beets beet stats` reports the
   same item/album counts as before the upgrade.
9. Confirm `/api/setup/status` reports stock Beets reachable, the expected
   version, and the `webmanager` integration plugin still compatible.
10. Run read-only checks only: browse the library, review a few
    albums/items.
11. Run exactly one controlled operation against a single test/staging
    item (not the full library) to confirm mutations still work end-to-end.
12. Only after 8-11 all pass, re-enable normal Web Manager jobs.

## Rollback

If any step above fails, or a problem is discovered after cutover:

1. Stop the new `beets` container. Do not delete it yet.
2. If the new image ever opened the real database (step 7 onward), the
   on-disk `musiclibrary.blb` has been migrated and is **not** safely
   readable by the old Beets version. Restore `config.yaml` and
   `musiclibrary.blb` from the pre-upgrade backup/snapshot taken above — do
   not attempt to reuse the migrated file with the old version. If the new
   image was never started against the real database (failure caught
   before step 7), the original files are untouched and no restore is
   needed.
3. Re-pull/re-tag the previous `beets` image digest recorded above and
   start it against the restored config/database.
4. Verify: container health, `beet version` reports the old version,
   `beet stats` reports the same item/album counts as the pre-upgrade
   checklist.
5. Confirm `/api/setup/status` reports the expected (old) version and
   plugin state before re-enabling jobs.

Rollback is only "restore the old image and restart" if the new image
never actually opened the real database. Once it has, rollback requires
restoring the database/config snapshot — the schema change is not
something an older Beets binary can be expected to read.
