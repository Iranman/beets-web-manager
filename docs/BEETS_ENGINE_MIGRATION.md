# Beets Engine Version Migration (2.4.0 → 2.13.1)

This documents the production migration path from the previously pinned Beets
2.4.0 engine to the tested 2.13.1 production candidate, and how to roll back.
It applies to any future engine version bump, not just this one.

## Background

Beets 2.4.0 was pinned because of a plugin-resolution defect
(`beetbox/beets#6033`): `_get_plugin()` selected the first concrete
`BeetsPlugin` subclass in a module's `__dict__`, so `beetsplug.chroma`
(which imports `MusicBrainzPlugin`) resolved to the wrong class. This was
fixed upstream in Beets 2.5.0 (`beetbox/beets#6039`) with different source
text than the local backport patch. `docker/beets/apply_patches.py` now
applies that narrow patch only against exactly Beets 2.4.0, skips it (and
verifies the upstream fix directly) on Beets >= 2.5.0, and fails the build
for any other version. The patch must never be applied to a non-2.4.0
image — the modern source it would be applied to doesn't even match the
patch's expected block text.

## What actually happens on first open (verified)

Opening an existing 2.4.0-era `musiclibrary.blb` with the modern engine
triggers Beets' own automatic, one-time schema migration. This was verified
against a real (synthetic, disposable) database created with the 2.4.0
image and then opened with 2.13.1:

- Data integrity: item/album/artist counts and total size were identical
  before and after migration in testing.
- The migration is **not free**: it created 11 full-size `.bak` sidecar
  files next to the library (e.g.
  `musiclibrary.blb-before-items-multi_genre_field.bak`), each the same
  size as the database at that point. **Ensure `/config` has free space for
  at least ~11-12x the current `musiclibrary.blb` size before the first
  start of the modern engine against a real library.**
- The migration is idempotent: a second run against the same
  already-migrated database creates zero additional backups.
- The startup guard (`backend/beets_startup_guard.py`) is version-independent
  (pure SQLite checks) and passed against both the pre- and post-migration
  database in testing without modification.
- **This migration is not Beets-version-reversible.** Beets itself has no
  "downgrade schema" command. The 2.4.0 binary is not guaranteed to open a
  database migrated by 2.13.1. Rollback means restoring the pre-migration
  snapshot/backup, not just swapping the image back.
- The migration runs on the **first Beets CLI invocation of any kind**
  against the real database under the new engine — not only `beet import`.
  Even a read-only `beet version` or `beet stats` triggers it. Treat
  starting the new engine's control agent (which shells out to `beet
  version` for health/status) against the real `/config` mount as the
  point of no return, not merely running `beet import`.

## Pre-migration checklist

1. Stop web-manager mutation jobs (imports, cleanup, retag, playlist-repair
   jobs). Confirm no job is `running` or `queued` in the Jobs view.
2. Confirm no Beets CLI/import process is active on the host (`beet-locked`
   uses `flock` on `/config/.beet_db.lock` for this — check nothing holds it).
3. Back up, from the real `/config` mount:
   - `config.yaml`
   - `musiclibrary.blb`
   - any custom plugins under `/config/beetsplug`
   - any transaction/undo state the web manager keeps under
     `/web-manager-data`
4. Create a ZFS snapshot (or equivalent) of the Beets configuration dataset
   (the volume backing `/config`) immediately before starting the new
   engine. This is the actual rollback target — the ad hoc file copies in
   step 3 are a second, independent safety net, not a replacement for it.
5. Record the currently running engine image ID and digest
   (`docker inspect --format '{{.Image}}' <container>` and, if published,
   the registry digest) so the exact prior image can be reconstructed if
   the local one is later pruned.
6. Ensure `/config`'s filesystem has free space for the one-time backup
   proliferation described above.

## Migration steps

7. Pull or build the modern engine
   (`docker build -f Dockerfile.beets --build-arg
   BEETS_BASE_IMAGE=lscr.io/linuxserver/beets:2.13.1 --build-arg
   VCS_REF=<commit> -t beets-engine:2.13.1 .`).
8. Start the modern engine against the real `/config` mount with
   `BEETS_EXPECT_EXISTING_LIBRARY=1` (the Dockerfile's default), but do
   **not** start or enable any web-manager mutation job yet. This step is
   the point of no return described above — the schema migration runs here.
9. Verify: container health, `beet version` reports 2.13.1, the loaded
   plugin list matches expectations (chroma, musicbrainz, mbsubmit,
   fetchart, etc. — see `scripts/verify_beets_engine_image.py`), the
   startup guard passed (check container logs for
   `[beets-startup-guard] OK: existing_library_verified`), and
   `beet stats` reports the same item/album counts as before migration.
10. Start (or point) the web manager at the new engine
    (`BEETS_API_URL=http://beets:8338`). Confirm `/api/setup/status`
    reports `"available": true"`, the expected Beets version, and
    `plugin_loader_ok: true`.
11. Run read-only checks only: browse the library, review a few
    albums/items, check `/api/setup/status` integrations.
12. Run exactly one controlled dry-run operation (e.g. an import
    `--pretend` against a single test/staging path, not the full library)
    to confirm command execution end-to-end without mutating anything.
13. Only after 9-12 all pass, re-enable normal web-manager jobs.

## Rollback

If any step above fails, or a problem is discovered after cutover:

1. Stop the new (2.13.1) engine container. Do not delete it yet.
2. If the new engine ever opened the real database (step 8 onward), the
   on-disk `musiclibrary.blb` has been migrated and is **not** safely
   readable by the old 2.4.0 binary. Restore `config.yaml` and
   `musiclibrary.blb` from the pre-migration backup/ZFS snapshot taken in
   step 4 — do not attempt to reuse the migrated file with the old engine.
   If the new engine was never started against the real database (failure
   caught before step 8), the original files are untouched and no restore
   is needed.
3. Restore the previous engine image: rebuild or re-pull the recorded prior
   image ID/digest from the pre-migration checklist
   (`docker build --build-arg BEETS_BASE_IMAGE=lscr.io/linuxserver/beets:2.4.0@<recorded digest> ...`
   or re-tag the still-present local image if it was not pruned).
4. Start the restored old engine against the restored config/database.
5. Verify: container health, `beet version` reports 2.4.0, `beet stats`
   reports the same item/album counts as the pre-migration checklist, and
   the startup guard passes.
6. Point the web manager back at the restored engine and confirm
   `/api/setup/status` reports the expected (2.4.0) version and plugin
   state before re-enabling jobs.

Rollback is only "restore the old image and restart" if the new engine
never actually opened the real database. Once it has, rollback requires
restoring the database/config snapshot — the schema change is not
something the old Beets binary can be expected to read.

## Retaining Beets 2.4.0 compatibility

The 2.4.0 patch path and pin remain explicitly supported and tested (see
the `beets-engine-verification` CI matrix in
`.github/workflows/docker-build.yml`), specifically so a deployment that
has not yet migrated can still build and verify a working 2.4.0 engine.
This is a deliberate choice, not leftover dead code — remove it only when
no supported deployment still needs it, and note that removal here as its
own repository change.
