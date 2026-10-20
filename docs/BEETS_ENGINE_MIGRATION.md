# Upgrading The Pinned Beets Version

Beets runs inside the Web Manager image as a pinned Python dependency
(`beets==2.14.1` in `requirements.txt`, ADR 0006). There is no separate Beets
container and no custom Beets image. This document describes what happens
when a Web Manager release moves that pin on a deployment with an existing
library, and how to do it safely. It is not specific to any one version pair.

## Upgrading the pinned Beets (maintainers)

The pin changes only through a reviewed PR:

1. Dependabot's weekly `pip` run proposes the new `beets==` version, or a
   maintainer opens the PR by hand. The `beets-latest-canary` workflow runs
   the embedded-Beets acceptance against the newest Beets on PyPI every week,
   so upstream breakage is usually known before the PR.
2. Make the PR consistent: the same version in `requirements.txt`,
   `requirements-dev.txt`, `PINNED_BEETS_VERSION` in
   `backend/beets_embedded.py` and `BEETS_TEST_FIXTURE_VERSION` in
   `.github/workflows/docker-build.yml`. `tests/test_architecture_invariants.py`
   fails until all of these agree, and the engine refuses any other version at
   startup (`beets_version_mismatch`).
3. CI's `stock-beets-acceptance` and `production-docker-acceptance` jobs run
   the built image against the new pin. Read the upstream Beets release notes
   for changed fields, path handling or plugin APIs that the `webmanager`
   plugin uses.
4. Add a CHANGELOG entry naming the old and new Beets version and telling
   operators to follow the checklist below before they upgrade.

## What actually happens on first open

Opening an existing `musiclibrary.blb` with a newer Beets version can
trigger Beets' own automatic, one-time schema migration (Beets' own
behavior; Web Manager does not control it). Verified once against a real
version jump (2.4.0 -> 2.13.1, using a synthetic, disposable database):

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
  "downgrade schema" command. An older Beets is not guaranteed to open a
  database migrated by a newer one. Rollback means restoring a pre-migration
  backup, not just going back to the previous Web Manager image.
- The migration runs when Web Manager starts on the new image, before any
  job. Treat starting the new image against the real `/config` mount as the
  point of no return.

## Pre-upgrade checklist

1. Stop Web Manager mutation jobs (imports, cleanup, retag, playlist-repair
   jobs). Confirm no job is `running` or `queued` in the Jobs view.
2. Back up (`./backup.sh`, see the README's Backups section), which covers
   `config.yaml`, `musiclibrary.blb`, any custom plugins under
   `/config/beetsplug`, and `/web-manager-data`.
3. Create a filesystem/volume snapshot (ZFS, LVM, or equivalent) of the
   volume backing `/config` immediately before pulling the new image. This
   is the actual rollback target; the backup in step 2 is a second,
   independent safety net.
4. Record the running Web Manager version (`curl -s
   http://127.0.0.1:8337/health/live`) and the album and item counts.
5. Ensure `/config`'s filesystem has free space for the one-time backup
   proliferation described above.

## Upgrade steps

6. `docker compose pull && docker compose up -d`. This is the point of no
   return described above.
7. Verify: `curl -s http://127.0.0.1:8337/api/health` shows `"beets":
   {"ok": true, ...}`, `docker compose exec -u <PUID>:<PGID>
   beets-web-manager beet version` reports the expected version, and
   `beet stats` reports the same album and item counts as before.
8. Run read-only checks only: browse the library, review a few albums.
9. Run exactly one controlled operation against a single test item to
   confirm mutations still work end-to-end.
10. Only after 7-9 pass, re-enable normal Web Manager jobs.

## Rollback

1. Stop Web Manager (`docker compose stop`).
2. If the new image ever started against the real database, the on-disk
   `musiclibrary.blb` may have been migrated and is **not** safely readable
   by the old Beets. Restore `config.yaml` and `musiclibrary.blb` from the
   snapshot or backup taken above. If the new image never started against
   the real database, nothing needs restoring.
3. Pin the previous Web Manager release in the `image:` line and
   `docker compose up -d`.
4. Verify as in step 7, with the old version and the pre-upgrade counts.
