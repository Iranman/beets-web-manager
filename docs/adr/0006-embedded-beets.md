# ADR 0006: Beets Runs In-Process Inside Web Manager

Date: 2026-10-20

## Status

Accepted. Supersedes the "stock `lscr.io/linuxserver/beets` container, HTTP only" rule of
`docs/ARCHITECTURE.md` and the transport assumptions of ADR 0001. ADR 0001's core decision
(Beets is the library backend and the source of library mutations; no parallel library
database) still holds. Beets now runs inside the Web Manager process.

## Context

Web Manager has stopped working with Beets three times. Each time the cause was the
two-container split, not the Beets library itself:

- **Plugin version drift.** The `webmanager` plugin is copied into the Beets container's
  `/config/beetsplug` and loaded only when that container restarts. When Web Manager upgrades
  first, it talks to an older plugin: capabilities are missing, a protocol mismatch occurs, or an
  import fails with `autotag_not_allowed`. The rollout script has to restart `beets` whenever
  `PLUGIN_VERSION` changes.
- **Provisioning and config coupling.** Web Manager writes `plugins:`, `pluginpath:` and
  `web.include_paths` into the user's `config.yaml`, provisions a bearer key, and waits on
  Compose health ordering so the Beets container reads the edited config on its first boot.
  Any of those steps can fail on its own (a read-only `/config`, a missing key, a stale copy in
  `pluginpath`, `include_paths: no`).
- **The HTTP bridge.** Every read is serialized to JSON and parsed again, so `/item/` stats
  every file. Long operations need an async operation registry plus polling. A timeout on the
  wire does not stop Beets. Beets 2.14 also expands relative paths through a ContextVar that
  Werkzeug threads do not have, which needed a workaround.

The plugin's operation code is not the problem. It is ~6,000 lines of tested, Beets-native
mutation logic (import sessions, relocation, quarantine, rollback, durable records, the pinned
match rules from #454). It already runs in-process in the test suite (`beetsplug.web.app.test_client()`
over a temporary `Library`).

The user chose to embed Beets in Web Manager and asked for real improvements, not just a port:
"dont just copy or embed, make it better".

## Decision

1. **Beets becomes a pinned Python dependency of Web Manager**: `beets==2.14.1`, the version the
   live stock container runs, plus `pyacoustid` for `chroma`. One image, one process, one Beets
   version, chosen at build time. No separate Beets container, no plugin provisioning, no bearer
   key, no HTTP between Web Manager and Beets in the end state.
2. **`backend/beets_embedded.py` owns the Beets runtime.** It is the only module that loads
   Beets' configuration, loads Beets plugins and constructs `beets.library.Library`. It
   exposes a startup self-test (see improvement 4).
3. **`backend/beets_adapter.BeetsAdapter` stays the only interface callers use.** Its public
   methods, return shapes and exception types do not change. Only its transport changes.
4. **The plugin's operation code is reused, not rewritten.** `beetsplug/webmanager/*` becomes
   Web Manager's Beets operations package and is loaded from the image, never from `/config`.
   - Phase 1 dispatches adapter calls to the existing handlers through an in-process WSGI call
     (`werkzeug.test.Client` on `beetsplug.web.app`). There is no socket and no network: it is
     a function call with the handler's existing request/response contract, so every refusal
     code, idempotency rule and error mapping stays byte-for-byte the same.
   - Later phases turn the hot paths into plain function calls (see Phases).
5. **Library access rule (replaces "never opens `musiclibrary.blb`").** Web Manager opens the
   Beets library database only through Beets' own Library API:
   - The library is opened only by the `beets.library.Library` instance created in
     `backend/beets_embedded.py`, from the database path and `directory` in Beets'
     configuration.
   - Reads and writes go only through Beets' model and transaction API (`lib.items()`,
     `lib.albums()`, `Item`/`Album` `store()`/`move()`/`write()`/`remove()`,
     `lib.transaction()`, `ImportSession`) in `beetsplug/webmanager/*` or the engine module.
   - Nothing calls `sqlite3.connect` on the library file. No other module imports
     `beets.library` and no raw SQL runs outside a Beets `Transaction`.
   - The adapter stays the only entry point for the rest of the backend.
   - Beets keeps its own schema, migrations and path formats. Web Manager adds no tables and no
     columns, so the old Beets container can still open the same database (rollback).
6. **Mutation rules are unchanged.** `backend/transaction_engine.py` (preview, apply, audit,
   recovery) stays the only path that may mutate the library. The review queue still gets
   ambiguous evidence. `mb_releasegroupid` is still the canonical album identity. AI is still
   untrusted. The plugin's `mutation_lock` is now a real process-wide lock, so it is stronger
   than before.

## Consequences

- **What goes away.** Plugin/Web Manager version drift can no longer happen, because both ship
  in one image. A plugin change no longer needs a Beets restart. Provisioning, the bearer key
  and `include_paths` setup stop being requirements.
- **Mounts.** Web Manager must mount the music folder read-write, because it now writes `/music`
  through Beets.
- **Beets upgrades.** These become `requirements.txt` PRs. The schema-migration caution in
  `docs/BEETS_ENGINE_MIGRATION.md` still applies.
- **Image contents.** The image carries Beets and its metadata-source dependencies.
  `security-dependency-engineer` reviews the added packages.
- **Users with their own Beets.** Self-hosters who run Beets themselves keep their
  `config.yaml`, plugins and `pluginpath`. Web Manager reads the config and never rewrites it
  in embedded mode.

## Endpoint inventory and mapping

Every adapter call goes to the same handler. "P1" is the Phase 1 in-process dispatch, and
"Target" is the plain call the handler becomes in a later phase. The handler module is
unchanged in Phase 1.

| Adapter method | Route (handler module) | P1 | Target |
|---|---|---|---|
| `get_stats` | `GET /stats` (stock `beetsplug.web`) | in-process WSGI | P2: `len(lib.items())`, `len(lib.albums())` |
| `get_artists`, `get_unique_field_values` | `GET /artist/`, `GET /<e>/values/<k>` (stock web) | in-process WSGI | P2: `lib.items()` distinct |
| `get_items`, `get_item`, `get_albums`, `get_album`, query paths | `GET /item/…`, `/album/…` (stock web) | in-process WSGI | P2: `lib.items(query)` with the same row serializer |
| `get_fields` | `GET /webmanager/fields/<e>` (`read_ops`) | in-process WSGI | P2: `read_ops` without Flask |
| `open_item_file`, `open_album_art` | `GET /item/<id>/file`, `/album/<id>/art` (stock web) | in-process WSGI, streamed | P2: open the item's path through Beets |
| `get_plugin_status` | `GET /webmanager/status` (`operations`) | in-process WSGI | P5: replaced by `beets_embedded.status()` |
| `get_operation` | `GET /webmanager/operations/<id>` | in-process WSGI | P4: jobs replace plugin async threads |
| `run_import` | `POST /webmanager/import` (`operations`) | in-process WSGI | P3: matcher improvements live here |
| `modify` | `POST /webmanager/modify` | in-process WSGI | P5 |
| `remove`, `move` | `POST /webmanager/remove`, `/move` | in-process WSGI | P5 |
| `replace_item_file` (+ rollback) | `/webmanager/replace-item-file[/rollback]` (`replace_ops`) | in-process WSGI | P5 |
| `set_album_art`, `remove_album_art`, `rollback_album_art` | `/webmanager/album-art[/remove,/rollback]` (`art_ops`) | in-process WSGI | P5 |
| `relocate_album` (+ rollback) | `/webmanager/album-relocation[/rollback]` (`relocation_ops`) | in-process WSGI | P5 |
| `quarantine_remove_items` (+ rollback) | `/webmanager/quarantine-remove-items[/rollback]` (`remove_ops`) | in-process WSGI | P5 |
| `album_row_merge`, `get_album_row_merge`, rollback | `/webmanager/album-row-merge…` (`merge_ops`) | in-process WSGI | P3: fill-ins stop needing it |
| `untracked_attach[_album]`, `untracked_quarantine`, `untracked_rollback`, `get_untracked_record` | `/webmanager/untracked/…` (`untracked_ops`) | in-process WSGI | P5 |
| `folder_op` | `POST /webmanager/folder-op` (`folder_ops`) | in-process WSGI | P5 |
| `mbsync`, `mbsync_library`, `cancel_mbsync_library` | `/webmanager/mbsync[/library…]` | in-process WSGI | P4: job_engine cancel event |
| `fetch_art`, `embed_art`, `lastgenre`, `mbsubmit` | `/webmanager/fetchart` etc. (`plugin_ops`) | in-process WSGI | P5 |

The plugin code from #454 (pinned imports ignore `data_source` under a `max_rec` cap, the
fingerprint-proven artist relaxation, and stale chroma result eviction) runs unchanged on the
in-process path. Chroma's `_matches` cache now lives in the Web Manager process, and the
eviction in `on_import_task_created` covers it.

## Version pin, configuration and library discovery

- **Pin.** `requirements.txt` pins `beets==2.14.1` and `pyacoustid==1.3.1`. The pin
  `requirements-dev.txt` already uses becomes the runtime pin. A Beets upgrade is a
  reviewed PR that bumps `requirements.txt`. The self-test reports a running version that
  differs from `PINNED_BEETS_VERSION`.
- **Config.** `BEETSDIR` (already `/config` in the image) locates `config.yaml`, exactly as for
  the stock container. The user's file is read as is and never rewritten by the engine. The
  engine applies only in-memory overlays:
  - `plugins` gets `web` and `webmanager` added, so stock web's read routes and the operations
    are registered.
  - `web.include_paths: yes`, because Web Manager always needs paths.
  - Stock web `READONLY`, because its write routes are never used.
- **Plugins.** Everything else in `plugins:` (musicbrainz, chroma, deezer, fetchart,
  embedart, lastgenre, mbsync, ...) loads through Beets' own `plugins.load_plugins()`,
  including the user's `pluginpath`. Before Beets extends `beetsplug.__path__` with
  `pluginpath`, the engine imports every plugin the image ships (`beetsplug/webmanager`,
  `beetsplug/discpath.py`) from the image, so a stale copy provisioned into
  `/config/beetsplug` can never shadow it. If one still resolves elsewhere, the engine
  refuses to start (`operations_shadowed`, fail closed). The self-test names any configured
  plugin that failed to load.
- **Trust consolidation.** Everything `config.yaml` can make Beets execute now runs inside
  the Web Manager process, with its environment and its access to `/web-manager-data`:
  `pluginpath` modules, the `hook` plugin's commands, and plugin-configured commands such as
  `convert`, `play` and `badfiles`. Before, they ran in the Beets container. `config.yaml` is
  therefore part of Web Manager's trusted configuration, as the image is. With the embedded
  engine every Web Manager writer of `config.yaml` reachable from HTTP (editor save and
  revert, Setup's recommended-plugin and `web.include_paths` fixes, plugin provisioning) is
  fail-closed: a change passes only if every changed key is on an allowlist of settings and
  plugin names that run no command and no user-written expression taken from `config.yaml`
  (`backend/config_manager.py`, `EMBEDDED_*`); anything else gets
  `CONFIG_CODE_EXECUTION_EDIT_REFUSED` (403). The allowlist is not a sandbox: `chroma` runs
  the image's fixed `fpcalc` helper, and `fetchart`'s `cover_art_url` source fetches the URL
  stored in a file's tag, outside `provider_boundary`. Keys that choose which files are read
  or where they are written stay locked: `import.log`, `import.set_fields` (it can set
  `path` before the import moves the file), and `replace`, whose rules keep `paths` and
  `art_filename` results inside `directory` (#461). A save that adds a YAML alias is refused
  before the diff is walked, because aliases load as shared references and multiply the walk. An earlier denylist (`hook`, `pluginpath`,
  `include`) missed `inline` fields and plugin command options. Such changes are made by
  editing the file on the server; a file that already has them still loads.
- **Egress.** Beets plugins make their own outbound requests (MusicBrainz, AcoustID,
  Deezer, cover-art sources, Last.fm) with their own clients. That traffic does not go
  through `provider_boundary`, as it did not when it left the Beets container. It now leaves
  from the Web Manager container. Rate limits and retries are Beets'. Routing plugin egress
  through `provider_boundary` is out of scope until a phase needs it.
- **Library.** The database path comes from `library` and the music root from `directory` in
  Beets' config, as `beet` resolves them. The default `/config/musiclibrary.blb` and
  `/music` therefore keep working unchanged, and so do custom locations. A database file that
  did not exist before the open is reported as a self-test warning (`library_created`),
  because it usually means the `/config` mount is wrong.
- **Media.** Paths in the database are unchanged. The Web Manager container must mount the
  music folder **read-write** at the same container path Beets used (`/music` in every
  shipped Compose file).

## Concurrency

- **One process writes.** After cutover, Web Manager is the only process that opens the
  library for writing. The Beets container must be stopped, and the Phase 2 self-test refuses
  to start the embedded engine for writes while a `webmanager` plugin still answers at
  `BEETS_WEB_URL`. Two processes on one SQLite file are safe at the SQLite level, but not for
  the plugin's process-local `mutation_lock` and durable registries.
- **SQLite.** Beets gives each thread its own connection, with Beets' `timeout` setting as
  the busy timeout. Writes go through `lib.transaction()`. The plugin's `mutation_lock`
  serializes mutations and imports across threads in the process, as it did inside the Beets
  container's threaded web server.
- **Jobs.** Imports and other long work already run in `job_engine` jobs that call the adapter.
  In-process, a synchronous call blocks the job thread, not a request thread, and it can no
  longer "time out while Beets carries on". The plugin's own `async` threads still exist in
  Phase 1, because callers poll them. Phase 4 removes them in favour of the calling job and
  its cancel event (ADR 0005).

## Image and Compose

- **Dockerfile.** `ffmpeg` and `libchromaprint-tools` (`fpcalc`) are already installed. The
  only change is the Python dependencies.
- **Compose (Phase 2).** The `beets` service is removed. `beets-web-manager` mounts
  `/config` (rw), `/music` (rw, was `:ro`), `/downloads` and `/web-manager-data`, and sets
  `BEETS_ENGINE=embedded`. `BEETS_WEB_URL`, `BEETS_OUTBOUND_ALLOWLIST=beets:8337` and the
  `depends_on` ordering go away. Migration for an operator:
  1. Back up `musiclibrary.blb` and `config.yaml`, and record the sha256 and album/item counts.
  2. Run `docker compose stop beets`.
  3. Pull the new image and switch to the new Compose file.
  4. Check `/api/health` (`beets.ok`) and that the counts match.
  The same PUID/PGID as the Beets container keeps file ownership unchanged.

## Migration setting

`BEETS_ENGINE=http|embedded`.

- **Phase 1 (default `http`).** Embedded is opt-in, so the shipped stack and every test keep
  working while the engine is validated.
- **Phase 2 (default `embedded`).** The shipped Compose sets embedded. An operator can fall
  back to the Beets container without changing image by setting `BEETS_ENGINE=http` and
  starting the old `beets` service.
- **Phase 5 (removed).** The setting is deleted with the HTTP transport, which leaves a
  single code path.

This setting is justified only by rollback during the transition. It is not a long-term
choice.

## Rollback

- **Neither the schema nor the files change.** Web Manager adds no tables and runs the same Beets
  version (2.14.1) as the live container, so the database is never migrated by the switch.
  Rollback is:
  1. Stop Web Manager.
  2. Start the old Beets container and the previous Web Manager image (or set
     `BEETS_ENGINE=http`).
  3. Check the counts.
- **Exception.** If a later PR bumps the Beets pin, Beets may migrate the schema on first open,
  and that is not reversible. `docs/BEETS_ENGINE_MIGRATION.md`'s backup/snapshot rule applies
  unchanged: rollback then means restoring the snapshot.
- **Plugin records.** Durable records (`webmanager_operations.json`, quarantine manifests)
  stay in Beets' config directory with the same format, so either side can read them.

## Improvements over the two-container setup

Each improvement is tied to a problem we hit, and has a phase and an acceptance test. Nothing
else is built.

### 1. Evidence-first, explainable matching (Phase 3)

- **Problem.** Imports were refused as "could not confidently match" with no reason. Beets'
  `strong_rec_thresh` (0.04) blocked fill-ins because of constant penalties (`data_source`)
  and collaborative artist credits.
- **Change.** Inside the import session (`operations._import_session_class`), a decision is
  built from components:
  - Primary evidence is AcoustID per-track recording proof plus the MusicBrainz release in the
    expected release group.
  - Beets' text distance is secondary evidence, with its per-penalty breakdown
    (`Distance.keys()`).
  - Each component reports `pass`, `fail` or `not_checked` with a reason.
  - Every import result carries `decision.components[]` and, on refusal,
    `decision.blocking_component`. The UI shows them.
  - The thresholds stay Beets'. Proof can only replace text distance when every track is
    proven; it never lowers the bar for unproven tracks.
- **Acceptance.** These tests run on a temporary library with recorded AcoustID/MB fixtures:
  1. A folder whose only distance penalties are `data_source` and an artist-credit mismatch,
     with every track fingerprint-proven on the pinned release in the expected release group,
     imports.
  2. The same folder with one track's AcoustID naming another recording is refused with
     `blocking_component == "track_recording:<n>"`.
  3. A refusal with AcoustID unavailable reports `not_checked`, never `fail`.
  4. Every refusal has a non-empty `blocking_component`.

### 2. First-class fill-ins and partial imports (Phase 3)

- **Problem.** Adding missing tracks to an album imports a temporary album and then merges it
  (`album_row_merge`). Partly verified folders are refused whole.
- **Change.** An `add_tracks(existing_album_id, files, requested_recordings)` operation:
  - It verifies each file against its requested recording, adds the verified items to the
    existing album row with Beets' `Item` API (`lib.add`, `item.album_id`, `item.move()`,
    `write`), and sends the rest to review.
  - It goes through the transaction engine, with a rollback that removes the added items and
    files.
  - A mixed folder imports its verified tracks only.
- **Acceptance.**
  1. An album with 8 of 10 tracks plus a folder with the 2 missing, both proven, ends with the
     same album id, 10 items, and an unchanged album count before and after the apply. No
     `album_row_merge` record is created.
  2. A folder with 1 proven and 1 unproven track adds 1 item and creates 1 review row.
  3. Rollback restores 8 items and removes the 2 files.

### 3. Stage once, idempotently (Phase 4)

- **Problem.** Download folders are copied twice into staging, and orphans are left behind.
- **Change.** Staging keys each file by its content sha256. Staging the same source again is a
  no-op. Byte-identical files inside staging are collapsed to one copy, because their bytes
  are identical and nothing unique is lost. Unique audio is never deleted automatically (user
  decision).
- **Acceptance.**
  1. Staging the same source twice leaves one copy of each file, with an unchanged staging
     file count and a stable manifest.
  2. Two byte-identical files collapse to one, and two files that differ by one byte are both
     kept.
  3. No code path deletes a staging file whose sha256 has no other copy (an AST test plus a
     unit test).

### 4. One runtime and a startup self-test (Phase 1)

- **Problem.** Breakage was silent: a plugin that failed to load, a version mismatch, a
  missing `fpcalc`, or a library that did not open showed up only as failed imports.
- **Change.** `beets_embedded.status()` runs once at engine start. Its checks are:
  - the Beets version against the pin;
  - `config.yaml` parses;
  - the library opens, with `library_created` when the file is new;
  - the library directory exists and is writable;
  - every configured plugin loaded;
  - the operations package came from the image;
  - `fpcalc` is present (an error when `chroma` is configured);
  - `ffmpeg` is present.

  Each failure has a stable code and an actionable message. The public `/api/health` gets a
  minimal `beets` block (`engine`, `ok`, failing `codes`). The authenticated
  `/api/health/detail` gets the full checks with messages. Adapter calls fail with that message
  instead of a generic "Beets unreachable". Every check is error level except
  `config_missing`, `library_created`, `ffmpeg_missing` and `fpcalc_missing` without
  `chroma`. While any error-level check fails, the engine keeps serving reads but refuses
  every write (any non-GET operation) with the failing check's message
  (`BEETS_EMBEDDED_DEGRADED`, 503). `config_invalid`, `library_open_failed` and
  `operations_shadowed` stop the engine altogether.
- **Acceptance (Phase 1, `tests/test_beets_embedded.py`).**
  1. A valid temporary config and library give `ok: true` with every check passing.
  2. Each failure gives `ok: false` with exactly its code: invalid YAML (`config_invalid`), an
     unloadable plugin (`plugins_failed`), a missing `fpcalc` with `chroma`
     (`fpcalc_missing`), an unwritable library directory (`library_dir_not_writable`), and a
     version mismatch (`beets_version_mismatch`).
  4. With an error-level failure, a read succeeds and a mutation is refused with the
     check's message.
  3. `/api/health` exposes the codes and never a path.

### 5. Performance (Phase 2 reads, Phase 4 fingerprints)

- **Problem.** Each read is an HTTP round trip with JSON in both directions. Retries fingerprint
  the same files again.
- **Change.**
  - Phase 2: `get_fields`, `get_items`, `get_albums` and `get_item` call the Library API
    directly and keep the same row shape.
  - Phase 4: fingerprint results are cached in `<WEB_MANAGER_DATA_DIR>` keyed by (sha256,
    size), shared by chroma and `helpers_mb`.
- **Acceptance.**
  1. The embedded read path opens no socket: a test patches `socket.socket` to raise and reads
     items, albums and fields from a temporary library.
  2. Rows equal the HTTP-path rows for the same library (golden comparison).
  3. A second lookup of the same file runs `fpcalc` zero times (a call counter).

### 6. Observability (Phase 4)

- **Problem.** Some Beets work runs in plugin-internal threads that are invisible to Jobs and
  are lost on restart.
- **Change.** Every import and mutation is a `job_engine` job with progress, a structured
  result and a `TransactionStore` entry. The plugin's internal `threading.Thread` uses are
  removed. Long operations run in the calling job and observe its cancel event.
- **Acceptance.**
  1. An AST test finds no `threading.Thread` in `beetsplug/webmanager`.
  2. Every mutating route test asserts a `job_id` and a transaction id.
  3. Cancelling MBSync All through the job's cancel endpoint stops it after the current album.

## Phases

| Phase | Delivers | Done when |
|---|---|---|
| 0 | This ADR; ARCHITECTURE rules updated | Committed |
| 1 | `backend/beets_embedded.py`, the adapter's in-process transport behind `BEETS_ENGINE=embedded`, the self-test (4) in `/api/health`, and the Beets pin in `requirements.txt` | Tests on a temporary library cover reads, an import and a mutation through the adapter, plus the improvement 4 acceptance tests. The full suite passes. |
| 2 | Cutover: Compose without the `beets` service and with `/music` rw, `BEETS_ENGINE=embedded` by default, setup and diagnostics from the self-test, second-writer guard, direct reads (5, reads), docs, an `embedded-acceptance` CI job, and a MINOR release | A fresh install and an upgrade from the two-container stack pass acceptance, and counts and checksum are unchanged on a copy of a real library. Embedded autotag and MusicBrainz tests run on recorded provider fixtures, including a refused match and a provider-down case. A second writer is detected by a lock or marker file in the Beets config folder (not only by probing `BEETS_WEB_URL`), and writes are refused while it is held by another process. |
| 3 | Improvements 1 and 2 | Their acceptance tests pass |
| 4 | Improvements 3, 5 (fingerprints) and 6 | Their acceptance tests pass. No request thread calls a long synchronous adapter operation (import, mbsync, fetchart, relocation): those callers use the async or job path, enforced by a test. |
| 5 | Remove the HTTP transport, `BEETS_WEB_URL`, the API key, provisioning (`beets_plugins.py` provisioning, `_bootstrap_beets_plugins`), the stock-Beets Compose pin and `stock-beets-acceptance`. Handlers become plain functions. | A single code path remains. `BEETS_ENGINE` is gone. |

## Docs, tests and CI that change

- **Docs.**
  - Phase 0: `docs/ARCHITECTURE.md` (Non-Negotiable Rules, components, boundaries, state
    ownership) and `AGENTS.md` rule 1.
  - Phase 2: `docs/INSTALLATION.md`, `docs/CONFIGURATION.md` and `.env.example`
    (`BEETS_ENGINE`; deprecate `BEETS_WEB_URL`, `BEETS_WEBMANAGER_API_KEY` and
    `BEETS_OUTBOUND_ALLOWLIST=beets:8337`), `docs/TROUBLESHOOTING.md`,
    `docs/TRUENAS_ROLLOUT.md` (no Beets restart on plugin change),
    `docs/BEETS_ENGINE_MIGRATION.md` (the pin moves to `requirements.txt`), and
    `CHANGELOG.md` Upgrade Notes.
- **Tests.**
  - `tests/test_migration_invariants.py` (Beets is now in `requirements.txt`).
  - `tests/test_no_local_beets_db.py` and `tests/test_architecture_invariants.py` (the stock
    pin in Compose, the `ro` music mount) are updated in Phase 2 and Phase 5.
  - `tests/test_arch003_boundary_enforcement.py` keeps the adapter free of filesystem calls.
    The engine module is the only `beets.library` importer under `backend/`; this is enforced
    by a new AST test in Phase 1.
- **CI.**
  - `stock-beets-acceptance` stays until Phase 5, because it covers the rollback transport.
  - `embedded-acceptance` (Phase 2) runs the production image with no Beets container against
    a disposable library and does import, modify and relocate with rollback.
  - `production-docker-acceptance` switches to the single-service Compose.
  - `beets-latest-canary.yml` tests the next Beets as a pip pin instead of an image tag.
